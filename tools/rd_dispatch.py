"""
RD 工程師派工與互相接手 (Failover)

使用者規則 (修改時必須維持)：
    1. 寫程式任務若沒有指定工程師，優先交給 Claude Code。
    2. Claude Code 沒反應或出錯 (例如額度 / Token 用完、未登入、程序異常) 時，由 Antigravity (agy) 接手；
       逾時但逾時前已有進展時不換人：帶 session_id 交回同一位收尾一次，仍逾時就停下回報；
       反之 agy 失敗時由 Claude Code 接手。
    3. 工程師提問經使用者裁決後，帶 session_id 交回「原工程師」接續原對話 (除非它失敗才換人)。
    4. scope="core" (修改 ArQ 本身) 完成後附上 acceptance 驗收佐證，由使用者確認後才 commit。

流程 (run_dev_task)：
    依序嘗試 [首選引擎, 另一個引擎]：
      1. 先檢查認證，未就緒就直接換下一個，不浪費時間。
      2. 執行任務 (首選引擎帶入 session_id 接續原對話)；成功就回傳。
      3. 引擎層級失敗 (success=False) → 通知使用者後換下一個引擎，任務描述附上「接手說明」
         (接手者是全新對話，不帶 session_id)。
    「引擎正常回應但任務本身做不到」不算失敗，避免同一個任務被兩個引擎重複做。

強制停止 (見 tools/rd_jobs.py)：每次派工登記為 DevJob；使用者停止時結束程序樹，
結果標為 cancelled，不觸發 failover；有 ticket 時改為 blocked 並留言；scope=core 附 git 狀態 (不還原)。
"""

import contextvars
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from tools.antigravity_tools import DEFAULT_MODEL as AGY_DEFAULT_MODEL
from tools.antigravity_tools import check_antigravity_auth, run_antigravity_task
from tools.claude_tools import check_claude_auth, run_claude_task
from tools.config import temp_path
from tools import rd_jobs, ticket_tools, worktree
from tools.rd_common import (ANTIGRAVITY, CLAUDE, ENGINE_LABELS, SCOPE_CORE, SCOPE_TASK, ApproveFn, NotifyFn,
                             _safe_notify, collect_acceptance, collect_worktree_state, job_tag, parse_subagent_report)
from tools.rd_context import (MAX_DEPENDENCY_LOOKUPS, PLAN_COMMENT_CHARS, PLAN_COMMENT_PREFIX, PLAN_CONTEXT_CHARS,
                              TICKET_CONTEXT_CHARS, _clip_plan, blocked_result, dependency_conflict, extract_ticket_ref,
                              ticket_relation_context, ticket_title_from_task, with_ticket_context)
from tools.rd_jobs import DevJob
from tools.rd_report import (MAX_MODEL_RESULT_CHARS, _MAX_LIST, _clip, commit_note, compact_dev_result,
                             format_cancel_report, format_dev_report, format_plan_report)

logger = logging.getLogger(__name__)



def _check_auth(engine: str) -> Dict[str, Any]:
    return check_claude_auth() if engine == CLAUDE else check_antigravity_auth()


def _run_engine(engine: str, task: str, agy_model: str, notify: Optional[NotifyFn],
                approve: Optional[ApproveFn], scope: str, session_id: str, job: DevJob,
                plan: bool = False, workdir: str = "") -> Dict[str, Any]:
    extra: Dict[str, Any] = {"plan": True} if plan else {}  # 只讀規劃
    if workdir:
        extra["workdir"] = workdir  # scope=core 在該單的 worktree
    if engine == CLAUDE:
        return run_claude_task(task, notify=notify, approve=approve, scope=scope, session_id=session_id, job=job, **extra)
    return run_antigravity_task(task, model=agy_model, notify=notify, approve=approve,
                                scope=scope, session_id=session_id, job=job, **extra)


# 逾時但已有進展時：帶 session_id 交回同一位 RD 收尾的次數上限，以及給它的接續說明
MAX_TIMEOUT_RESUMES = 1
TIMEOUT_RESUME_NOTE = (
    "你上一輪因逾時被系統中止，工作目錄中的改動都還在。請先檢查目前狀態（git status、測試），"
    "完成剩下的部分，不要重做已完成的工作；完成後照原格式回報。"
)


def _handoff_note(previous: str, reason: str) -> str:
    """附在任務描述後，提醒接手的工程師前一位留下的狀況"""
    return (
        "\n\n【接手說明】\n"
        f"前一位工程師 {ENGINE_LABELS[previous]} 未能完成此任務（原因：{reason[:300]}）。\n"
        "工作目錄中可能已有前一位留下的部分程式碼或成果，請先檢查現有檔案，能沿用就沿用，再接續完成任務。"
    )


# -------------------------------------------------------------
# 派工自動開單：開單是程式保證的步驟，不靠 PM 提示詞 (模型可能略過)
# -------------------------------------------------------------

_TICKET_ENGINES = {CLAUDE: "claude", ANTIGRAVITY: "agy"}   # rd:<engine> label 的值


def _tag(job: DevJob) -> str:
    return f"【{job_tag(job.ticket, job.engine_label)}】"


def _tagged_notify(notify: Optional[NotifyFn], job: DevJob) -> Optional[NotifyFn]:
    if notify is None:
        return None
    return lambda text: notify(_tag(job) + text)


def _tagged_approve(approve: Optional[ApproveFn], job: DevJob) -> Optional[ApproveFn]:
    if approve is None:
        return None
    return lambda title, detail: approve(_tag(job) + title, detail)


def _prepare_worktree(number: Optional[int], preferred: str,
                      notify: Optional[NotifyFn]) -> "tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]":
    """
    scope=core 的 worktree：回傳 (worktree 資訊, None) 或 (None, 拒絕派工的結果)。
    沒有單號 (ticket 未設定或開單失敗) 無法命名 worktree，也不退回在執行資料夾裡改，直接拒絕。
    """
    def refuse(message: str) -> Dict[str, Any]:
        return {"success": False, "scope": SCOPE_CORE, "ticket": number, "engine_label": ENGINE_LABELS[preferred],
                "stdout": "", "stderr": "", "error": message}

    if not number:
        return None, refuse("scope=core 需要單號才能建立 worktree（ticket 功能未設定或開單失敗），未派工。"
                            "請先設定 TICKETS_REPO / TICKETS_GITHUB_TOKEN 或帶入既有單號。")
    res = ticket_tools.get_ticket(int(number))
    title = (res.get("ticket") or {}).get("title", "") if res.get("success") else ""
    try:
        wt = worktree.ensure_worktree(int(number), title)
    except worktree.WorktreeError as e:
        return None, refuse(f"無法建立 #{number} 的 worktree，未派工：{e}")
    if wt.get("created"):
        _safe_notify(notify, f"🌿 已為 #{number} 建立 worktree `{wt['path']}`（分支 `{wt['branch']}`），ArQ 執行資料夾不會被修改。")
    return wt, None


# -------------------------------------------------------------
# session → 單號：只讀規劃或派工結束時記下 RD 對話對應的單，之後只帶 session_id 接續也找得回同一張單。
# 寫到 _temp 讓重啟後仍查得到；單元測試 (ARQ_TESTING) 不寫真正的檔案
# -------------------------------------------------------------

_DEFAULT_SESSION_TICKETS_PATH = temp_path("session_tickets.json")
SESSION_TICKETS_PATH = _DEFAULT_SESSION_TICKETS_PATH
MAX_SESSION_TICKETS = 200
_SESSION_TICKETS: Dict[str, int] = {}
_SESSION_TICKETS_LOCK = threading.Lock()
_SESSION_TICKETS_LOADED = False


def _persist_session_tickets() -> bool:
    return not (os.getenv("ARQ_TESTING") and SESSION_TICKETS_PATH == _DEFAULT_SESSION_TICKETS_PATH)


def _load_session_tickets_locked() -> None:
    global _SESSION_TICKETS_LOADED
    if _SESSION_TICKETS_LOADED:
        return
    _SESSION_TICKETS_LOADED = True
    if not _persist_session_tickets():
        return
    try:
        with open(SESSION_TICKETS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _SESSION_TICKETS.update({str(k): int(v) for k, v in data.items() if str(v).isdigit()})
    except (OSError, ValueError):
        pass


def remember_session_ticket(session_id: str, number: Optional[int]) -> None:
    if not session_id or not number:
        return
    with _SESSION_TICKETS_LOCK:
        _load_session_tickets_locked()
        _SESSION_TICKETS.pop(session_id, None)
        _SESSION_TICKETS[session_id] = int(number)
        while len(_SESSION_TICKETS) > MAX_SESSION_TICKETS:
            _SESSION_TICKETS.pop(next(iter(_SESSION_TICKETS)))
        if not _persist_session_tickets():
            return
        try:
            os.makedirs(os.path.dirname(SESSION_TICKETS_PATH), exist_ok=True)
            with open(SESSION_TICKETS_PATH, "w", encoding="utf-8") as f:
                json.dump(_SESSION_TICKETS, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"⚠️ 寫入 session 單號對照失敗: {e}", exc_info=True)


def session_ticket(session_id: str) -> Optional[int]:
    if not session_id:
        return None
    with _SESSION_TICKETS_LOCK:
        _load_session_tickets_locked()
        return _SESSION_TICKETS.get(session_id)


def _reset_session_tickets_for_tests() -> None:
    global _SESSION_TICKETS_LOADED
    with _SESSION_TICKETS_LOCK:
        _SESSION_TICKETS.clear()
        _SESSION_TICKETS_LOADED = False


def _notify_board(res: Dict[str, Any], notify: Optional[NotifyFn]) -> None:
    """看板未設定或同步失敗時 ticket_tools 只回傳 board_note，派工流程要轉給使用者才看得到"""
    if res.get("board_note"):
        _safe_notify(notify, str(res["board_note"]))


def _prepare_ticket(task: str, preferred: str, opened_by: str, ticket: int, session_id: str,
                    notify: Optional[NotifyFn], plan: bool = False) -> Optional[int]:
    """
    回傳本次派工對應的單號 (None = 不開單或開單失敗)。開單失敗只通知，不影響派工；
    ticket 功能未設定 (沒有 TICKETS_REPO) 時靜默略過。
    只讀規劃 不改既有單的狀態；沒有單時開一張 todo 的單，接續實作時沿用。
    接續原對話卻沒帶單號時，先找該 session 對應的單，找不到才開新單。
    """
    if plan and ticket:
        return int(ticket)
    if not opened_by:
        return None
    label = ENGINE_LABELS[preferred]
    if not ticket and session_id:
        ticket = session_ticket(session_id) or 0
    if ticket:
        if plan:
            return int(ticket)
        res = ticket_tools.update_ticket(int(ticket), status="in-progress", comment=f"🔧 派工給 {label}")
        if not res.get("success") and "尚未設定" not in str(res.get("error", "")):
            _safe_notify(notify, f"⚠️ 更新 ticket #{ticket} 失敗（不影響派工）：{str(res.get('error'))[:200]}")
        _notify_board(res, notify)
        return int(ticket)
    res = ticket_tools.create_ticket(title=ticket_title_from_task(task), body=task, opened_by=opened_by,
                                     engine=_TICKET_ENGINES[preferred], status="todo" if plan else "in-progress")
    if res.get("success"):
        _safe_notify(notify, f"🎫 已開單 #{res['number']}：{res.get('url', '')}")
        _notify_board(res, notify)
        return res["number"]
    if "尚未設定" not in str(res.get("error", "")):
        _safe_notify(notify, f"⚠️ 開單失敗（不影響派工）：{str(res.get('error'))[:200]}")
    return None


def run_dev_task(
    task_description: str,
    preferred: str = CLAUDE,
    agy_model: str = AGY_DEFAULT_MODEL,
    notify: Optional[NotifyFn] = None,
    approve: Optional[ApproveFn] = None,
    scope: str = SCOPE_TASK,
    session_id: str = "",
    opened_by: str = "",
    ticket: int = 0,
    job: Optional[DevJob] = None,
    plan: bool = False,
) -> Dict[str, Any]:
    """
    派工給 RD 工程師，首選引擎失敗時自動由另一個引擎接手 (同步、會阻塞數分鐘，請在執行緒中呼叫)。

    preferred ：首選引擎 "claude" (預設) 或 "antigravity"。
    scope     ："task" (預設) 或 "core" (修改 ArQ 本身)。
    session_id：接續首選引擎的原對話 (工程師提問 → 使用者裁決後交回原工程師)。
    opened_by ：派工入口的開單者 ("user" = 斜線指令、"pm" = Gemini)；有值才自動開單，空字串不開單。
    ticket    ：已有的單號 (接續原對話時)，沿用而不重複開單。
    job       ：呼叫端已建立的 DevJob (斜線指令先送出附停止按鈕的訊息)；None 時自動建立並通知 bot 送出按鈕。
    回傳：最後一個執行引擎的結果，並加上 "engine" / "engine_label" (實際完成或最後嘗試的引擎)、
          "attempts" (失敗紀錄 [{"engine", "error"}])、"scope"、"ticket"；scope=core 成功時另含 "acceptance"；
          被使用者停止時 "cancelled"=True，scope=core 另含 "worktree" (git status / diff --stat)。
    """
    if ticket and not plan and not session_id:
        blocked = blocked_result(int(ticket), preferred, scope)
        if blocked:
            return blocked
    if ticket and not plan and job is None:
        # 同步派工 (斜線指令) 也不和相依的單平行；非同步派工已在 start_dev_task 檢查
        conflict = dependency_conflict(int(ticket))
        if conflict:
            return {"success": False, "ticket": int(ticket), "scope": scope, "engine_label": ENGINE_LABELS[preferred],
                    "stdout": "", "stderr": "", "error": conflict}
    announce = job is None
    if job is None:
        # 已知單號先帶上，派工訊息才標得出是哪張單；新開的單在開單後補上
        job = DevJob(task=task_description, engine_label=ENGINE_LABELS[preferred], scope=scope,
                     ticket=int(ticket) if ticket else None)
    elif ticket and not job.ticket:
        job.ticket = int(ticket)
    # 通知與同意卡加上「#單號｜RD」前綴；呼叫當下才組，換人接手後自動跟著變
    notify = _tagged_notify(notify, job)
    approve = _tagged_approve(approve, job)
    rd_jobs.register(job, announce=announce)
    try:
        # 只讀規劃不改既有單的狀態；沒有單時開 todo 的單，接續實作才對得到同一張
        number = _prepare_ticket(task_description, preferred, opened_by, ticket, session_id, notify, plan=plan)
        job.ticket = number
        # scope=core：在該單的 worktree 開發，ArQ 執行資料夾保持在 main
        wt = None
        if scope == SCOPE_CORE and (number or not plan):
            wt, refused = _prepare_worktree(number, preferred, notify)
            if refused:
                return refused
        workdir = wt["path"] if wt else ""
        if ticket and not session_id:
            # 既有的單：附上 ticket 內容，PM 或使用者不必把整張單重抄一遍；接續原對話時工程師已有脈絡
            task_description = with_ticket_context(task_description, int(ticket), notify, include_plan=not plan)
        # scope=task 只靠提示詞約束 RD：前後比對 git 狀態，抓出改到 ArQ 本身的情況
        check_scope = scope == SCOPE_TASK and not plan
        state_before = collect_worktree_state() if check_scope else None
        result = _dispatch_with_failover(task_description, preferred, agy_model, notify,
                                         rd_jobs.cancellable_approve(approve, job), scope, session_id, job, plan,
                                         workdir=workdir)
        result = {**result, "ticket": number}
        remember_session_ticket(str(result.get("session_id") or ""), number)
        if wt:
            result["worktree_info"] = {"path": wt["path"], "branch": wt["branch"]}
            if not result.get("success") and not plan:
                _safe_notify(notify, f"📂 改動保留在 worktree `{wt['path']}`（分支 `{wt['branch']}`），"
                                     "可用 session_id 接續原工程師，或由使用者決定是否清除。")
        if check_scope:
            violations = scope_violations(state_before or {}, collect_worktree_state())
            if violations:
                result["scope_violation"] = violations
                if not result.get("cancelled"):
                    result["acceptance"] = collect_acceptance()  # 照 scope=core 附驗收卡
        if plan:
            result["plan"] = True
            if result.get("success") and number:
                _comment_plan(result, number, notify)
        if result.get("cancelled"):
            result = _after_cancel(result, job, number, scope, notify, plan=plan, workdir=workdir)
        return result
    finally:
        rd_jobs.finish(job)


def _status_paths(status: str) -> Dict[str, str]:
    """git status --short 的每一行 → {路徑: 整行}；改名 (a -> b) 取新路徑"""
    paths: Dict[str, str] = {}
    for line in (status or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths[path.replace("\\", "/")] = line
    return paths


def scope_violations(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """
    scope=task 派工前後的 git 狀態比對：回傳 task/ 以外「新出現或狀態改變」的檔案。
    task/ 不進版控，本來就不會出現在 git status；派工前就有的改動不算在 RD 頭上。
    """
    old, new = _status_paths(before.get("status", "")), _status_paths(after.get("status", ""))
    return [p for p, line in new.items() if old.get(p) != line and not p.startswith("task/")]


def _subagent_record(result: Dict[str, Any]) -> str:
    """規劃留言的 sub agent 紀錄：一律寫，沒有也寫「無」，回顧時才量得到 的效果"""
    subagents = result.get("subagents") or []
    if not subagents:
        return "🧩 Sub agent：無"
    return f"🧩 Sub agent：{len(subagents)} 個\n" + "\n".join(f"- {_clip(s, 150)}" for s in subagents[:_MAX_LIST])


def _comment_plan(result: Dict[str, Any], number: int, notify: Optional[NotifyFn]) -> None:
    """
    把只讀規劃結果留言到 ticket，成為長期脈絡 (之後實作或回顧都查得到)。
    規劃整則留言、不拆 (get_ticket 只認最新一則規劃留言)；只有超過 GitHub 留言上限才截斷，
    並明確標示原長度、通知使用者。曾以 6,000 字無聲截斷， 的未決問題因此全部遺失。
    """
    plan = str(result.get("stdout") or "")
    if len(plan) > PLAN_COMMENT_CHARS:
        _safe_notify(notify, f"⚠️ ticket #{number} 的規劃長 {len(plan)} 字，超過 GitHub 留言上限，"
                             f"留言只保留前 {PLAN_COMMENT_CHARS} 字（已標示截斷）。")
        plan = plan[:PLAN_COMMENT_CHARS] + f"\n{ticket_tools._TRUNCATED_MARK} {len(plan)} 字；超過 GitHub 留言上限)"
    comment = (f"{PLAN_COMMENT_PREFIX}（{result.get('engine_label') or 'RD'}，未修改檔案）\n\n"
               f"{plan}\n\n{_subagent_record(result)}")
    res = ticket_tools.update_ticket(int(number), comment=comment)
    if not res.get("success") and "尚未設定" not in str(res.get("error", "")):
        _safe_notify(notify, f"⚠️ 規劃留言到 ticket #{number} 失敗：{str(res.get('error'))[:200]}")


def _cancelled_result(job: DevJob, engine: str, scope: str, attempts: List[Dict[str, str]],
                      result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {**(result or {}), "success": False, "cancelled": True, "error": "使用者已停止開發",
            "engine": engine, "engine_label": ENGINE_LABELS[engine], "attempts": attempts, "scope": scope}


def _after_cancel(result: Dict[str, Any], job: DevJob, number: Optional[int], scope: str,
                  notify: Optional[NotifyFn], plan: bool = False, workdir: str = "") -> Dict[str, Any]:
    """
    停止後：scope=core 附 git 狀態 (不還原)；有 ticket 時留言停止原因與時間。
    實作派工改為 blocked；只讀規劃被停止時單本來就沒有開工，只留言不改狀態。
    """
    if scope == SCOPE_CORE and not plan:
        result["worktree"] = collect_worktree_state(workdir) if workdir else collect_worktree_state()
    if number:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.cancelled_at or time.time()))
        what = "只讀規劃" if plan else "開發"
        comment = (f"🛑 使用者於 {when} 停止 {result.get('engine_label') or job.engine_label} 的{what}"
                   + ("。" if plan else "（改動未自動還原；需要時可用 session_id 接續原工程師）。"))
        res = (ticket_tools.update_ticket(int(number), comment=comment) if plan
               else ticket_tools.update_ticket(int(number), status="blocked", comment=comment))
        if not res.get("success") and "尚未設定" not in str(res.get("error", "")):
            _safe_notify(notify, f"⚠️ 更新 ticket #{number} 失敗：{str(res.get('error'))[:200]}")
    return result


def _dispatch_with_failover(task_description: str, preferred: str, agy_model: str, notify: Optional[NotifyFn],
                            approve: Optional[ApproveFn], scope: str, session_id: str,
                            job: DevJob, plan: bool = False, workdir: str = "") -> Dict[str, Any]:
    """依序嘗試 [首選引擎, 另一個]，回傳成功的結果或兩者都失敗的彙整 (見 run_dev_task)；使用者停止時不換人"""
    order: List[str] = [preferred, ANTIGRAVITY if preferred == CLAUDE else CLAUDE]
    attempts: List[Dict[str, str]] = []
    task = task_description
    resume = session_id
    result: Dict[str, Any] = {}

    for idx, engine in enumerate(order):
        label = ENGINE_LABELS[engine]
        next_label = ENGINE_LABELS[order[idx + 1]] if idx + 1 < len(order) else None
        if job.cancelled:
            return _cancelled_result(job, engine, scope, attempts, result)
        # 引擎占用：忙碌中的引擎不接手 (改試下一位)；同一張單已有人在做或達到上限時直接失敗
        busy = rd_jobs.reserve(job, engine)
        if busy:
            if rd_jobs.engine_holder(engine, exclude=job) is None:
                return {**result, "success": False, "error": busy, "engine": engine, "engine_label": label,
                        "attempts": attempts, "scope": scope, "stdout": result.get("stdout", ""),
                        "stderr": result.get("stderr", "")}
            attempts.append({"engine": engine, "error": busy})
            _safe_notify(notify, f"⏳ {busy}" + (f"改由 **{next_label}** 接手。" if next_label else "不接手這張單。"))
            resume = ""
            continue
        job.engine_label = label

        auth = _check_auth(engine)
        if not auth.get("authenticated"):
            reason = f"認證未就緒：{auth.get('message', '')}"
            attempts.append({"engine": engine, "error": reason})
            if next_label:
                _safe_notify(notify, f"⚠️ **{label} 無法使用**（{reason[:200]}），改由 **{next_label}** 接手。")
            resume = ""  # 原對話屬於這個引擎，換人後無法接續
            continue

        run_task, resumes_left = task, MAX_TIMEOUT_RESUMES
        while True:
            logger.info(f"👷 派工給 {label} (scope={scope}{', 接續對話' if resume else ''})")
            result = _run_engine(engine, run_task, agy_model, notify, approve, scope, resume, job, plan, workdir=workdir)
            if job.cancelled or result.get("cancelled"):
                return _cancelled_result(job, engine, scope, attempts, result)  # 使用者停止：不 failover
            if result.get("success"):
                done = {**result, "engine": engine, "engine_label": label, "attempts": attempts, "scope": scope}
                # sub agent 紀錄：Claude 由事件流抓；抓不到 (Antigravity) 時改解析回報的「Sub agent：」段落
                done["subagents"] = result.get("subagents") or parse_subagent_report(result.get("stdout"))
                if scope == SCOPE_CORE and not plan:  # 只讀規劃沒有改動，不必跑驗收
                    done["acceptance"] = collect_acceptance(workdir) if workdir else collect_acceptance()
                return done
            if not (result.get("timed_out") and result.get("progressed")):
                break  # 一般失敗或一開始就沒反應：照舊換人

            # 逾時前已有進展：換人等於讓另一位從頭再做。帶原對話交回同一位收尾，仍逾時就停下
            reason = result.get("error") or "逾時"
            attempts.append({"engine": engine, "error": reason[:500]})
            session = result.get("session_id") or ""
            if session and resumes_left > 0:
                resumes_left -= 1
                _safe_notify(notify, f"⏱️ **{label}** {reason}，但逾時前已有進展：帶原對話交回 **{label}** 接續收尾，不換人。")
                run_task, resume = TIMEOUT_RESUME_NOTE, session
                continue
            hint = f"可帶 session_id `{session}` 接續原工程師" if session else "沒有可接續的對話，請使用者決定下一步"
            _safe_notify(notify, f"⏱️ **{label}** {reason}。逾時前已有進展，未改派給其他 RD（避免從頭重做）；{hint}。")
            return {**result, "success": False, "engine": engine, "engine_label": label, "attempts": attempts,
                    "scope": scope, "error": f"{reason}。逾時前已有進展，未改派給其他 RD（避免從頭重做）；{hint}。"}

        reason = result.get("error") or result.get("stderr") or "未知錯誤"
        attempts.append({"engine": engine, "error": reason[:500]})
        if next_label:
            _safe_notify(notify, f"⚠️ **{label} 執行失敗**（{reason[:200]}），改由 **{next_label}** 接手。")
            task = task_description + _handoff_note(engine, reason)
            resume = ""

    # 兩位工程師都失敗
    summary = "；".join(f"{ENGINE_LABELS[a['engine']]}：{a['error'][:200]}" for a in attempts)
    return {
        **result,
        "success": False,
        "error": f"所有 RD 工程師都無法完成任務。{summary}",
        "engine": order[-1] if result else None,
        "engine_label": ENGINE_LABELS[order[-1]] if result else None,
        "attempts": attempts,
        "scope": scope,
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
    }


# -------------------------------------------------------------
# 給 agent.py 的 ToolSpec(impl=...) 使用的進入點
# (參數需與對應的 Gemini 工具外觀一致，再加上 notify / approve)
# -------------------------------------------------------------

def run_claude_first(task_description: str, scope: str = SCOPE_TASK, session_id: str = "", ticket: int = 0,
                     plan: bool = False, notify: Optional[NotifyFn] = None,
                     approve: Optional[ApproveFn] = None) -> Dict[str, Any]:
    """dispatch_claude_engineer_sync 的實作：Claude Code 優先，失敗由 Antigravity 接手；經 Gemini 派工，開單者為 pm"""
    return run_dev_task(task_description, CLAUDE, notify=notify, approve=approve, scope=scope, session_id=session_id,
                        opened_by="pm", ticket=ticket, plan=bool(plan))


def run_antigravity_first(task_description: str, model: str = AGY_DEFAULT_MODEL, scope: str = SCOPE_TASK,
                          session_id: str = "", ticket: int = 0, plan: bool = False,
                          notify: Optional[NotifyFn] = None, approve: Optional[ApproveFn] = None) -> Dict[str, Any]:
    """dispatch_antigravity_engineer_sync 的實作：Antigravity 優先，失敗由 Claude Code 接手；經 Gemini 派工，開單者為 pm"""
    return run_dev_task(task_description, ANTIGRAVITY, agy_model=model, notify=notify, approve=approve,
                        scope=scope, session_id=session_id, opened_by="pm", ticket=ticket, plan=bool(plan))


# -------------------------------------------------------------
# 非同步派工 (拆單 34-2)：PM 派出後不必等待，RD 在背景開發
# -------------------------------------------------------------

DoneFn = Callable[[Dict[str, Any]], None]


def start_dev_task(
    task_description: str,
    preferred: str = CLAUDE,
    agy_model: str = AGY_DEFAULT_MODEL,
    notify: Optional[NotifyFn] = None,
    approve: Optional[ApproveFn] = None,
    scope: str = SCOPE_TASK,
    session_id: str = "",
    opened_by: str = "",
    ticket: int = 0,
    plan: bool = False,
    on_done: Optional[DoneFn] = None,
) -> Dict[str, Any]:
    """
    非同步派工：先檢查相依、用 rd_jobs.reserve 佔位 (首選引擎忙碌時改佔另一位)，被拒絕時立即回傳理由；
    成功時在背景執行緒跑 run_dev_task，結束 (含例外) 時呼叫 on_done(result)。
    回傳 {"accepted": bool, "job_id", "ticket", "engine_label"[, "error"][, "blocked_by"]}
    """
    number = int(ticket) if ticket else None
    if number and not plan and not session_id:
        blocked = blocked_result(number, preferred, scope)
        if blocked:
            return {"accepted": False, "ticket": number, "error": blocked["error"], "blocked_by": blocked["blocked_by"]}
    if number and not plan:
        conflict = dependency_conflict(number)  # 與執行中的單有相依就不平行
        if conflict:
            return {"accepted": False, "ticket": number, "error": conflict}
    job = DevJob(task=task_description, engine_label=ENGINE_LABELS[preferred], scope=scope, ticket=number)
    rd_jobs.register(job)
    reason = rd_jobs.reserve(job, preferred)
    if reason and rd_jobs.engine_holder(preferred, exclude=job) is not None:
        other = ANTIGRAVITY if preferred == CLAUDE else CLAUDE
        other_reason = rd_jobs.reserve(job, other)
        if other_reason is None:
            reason = None
            job.engine_label = ENGINE_LABELS[other]
        else:
            reason = f"{reason}；{other_reason}"
    if reason:
        rd_jobs.finish(job)
        return {"accepted": False, "ticket": number, "engine_label": job.engine_label, "error": reason}
    rd_jobs.register(job, announce=True)  # 送出附停止按鈕的派工訊息 (只送這一次)

    def work() -> None:
        try:
            result = run_dev_task(task_description, preferred, agy_model=agy_model, notify=notify, approve=approve,
                                  scope=scope, session_id=session_id, opened_by=opened_by, ticket=ticket,
                                  job=job, plan=plan)
        except Exception as e:  # 背景執行緒的例外不會傳回呼叫端：轉成失敗結果，照常回報
            rd_jobs.finish(job)
            result = {"success": False, "error": f"派工執行時發生例外：{type(e).__name__}: {e}",
                      "ticket": job.ticket, "engine_label": job.engine_label, "scope": scope}
        if on_done is not None:
            try:
                on_done(result)
            except Exception as e:
                logger.warning(f"⚠️ 派工完成回呼失敗: {e}", exc_info=True)

    # 複製 context：ORIGIN (回報頻道) 等 contextvars 要帶進背景執行緒
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(work,), daemon=True, name=f"rd-job-{job.id}").start()
    return {"accepted": True, "job_id": job.id, "ticket": number, "engine_label": job.engine_label}


def start_claude_first(task_description: str, scope: str = SCOPE_TASK, session_id: str = "", ticket: int = 0,
                       plan: bool = False, notify: Optional[NotifyFn] = None, approve: Optional[ApproveFn] = None,
                       on_done: Optional[DoneFn] = None) -> Dict[str, Any]:
    """dispatch_claude_engineer_sync 的非同步實作：Claude Code 優先；經 Gemini 派工，開單者為 pm"""
    return start_dev_task(task_description, CLAUDE, notify=notify, approve=approve, scope=scope,
                          session_id=session_id, opened_by="pm", ticket=ticket, plan=bool(plan), on_done=on_done)


def start_antigravity_first(task_description: str, model: str = AGY_DEFAULT_MODEL, scope: str = SCOPE_TASK,
                            session_id: str = "", ticket: int = 0, plan: bool = False,
                            notify: Optional[NotifyFn] = None, approve: Optional[ApproveFn] = None,
                            on_done: Optional[DoneFn] = None) -> Dict[str, Any]:
    """dispatch_antigravity_engineer_sync 的非同步實作：Antigravity 優先；經 Gemini 派工，開單者為 pm"""
    return start_dev_task(task_description, ANTIGRAVITY, agy_model=model, notify=notify, approve=approve,
                          scope=scope, session_id=session_id, opened_by="pm", ticket=ticket, plan=bool(plan),
                          on_done=on_done)
