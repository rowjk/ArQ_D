"""
Google Antigravity CLI (`agy`，RD 工程師) 無頭調度工具

ArQ (Gemini, PM 角色) 透過本模組呼叫本機的 Google Antigravity CLI (`agy -p`) 進行開發，
與 tools/claude_tools.py 的 Claude Code 並行，作為第二個開發引擎
(Antigravity 使用獨立配額池，用來分散高耗 Token 任務)。

設計說明 (修改前請先理解，與 claude_tools.py 的差異)：
1. 自主執行：固定帶 `--dangerously-skip-permissions`，一般步驟 agy 全程自主完成。
2. 使用者鐵則「安裝套件一定要先同意 / 高風險操作要先同意」的落實方式：
   agy 沒有 Claude Code 那種可掛載的 PreToolUse hook，無法「執行前暫停」，因此改用三層機制：
     a. 事前約定：prompt 明確禁止 agy 自行安裝套件；需要時在回覆中輸出
        `ARQ_INSTALL_REQUEST: <完整安裝指令>` 並停下該步驟。
     b. 即時中斷：以 `--output-format stream-json` 逐行監看。agy 開始執行工具時會送出
        step_update (state=ACTIVE, tool_info.parameters.CommandLine)，若該指令經
        risk_policy.classify_risk() 判定為安裝套件 / 高風險且未獲同意，立即 taskkill 中斷整個程序樹，
        再詢問使用者。⚠️ 由於指令已經開始執行，中斷前可能已部分生效，這是盡力而為的防線。
     c. 詢問後續跑：使用者同意 / 拒絕後，以 `--conversation <id>` 接續同一段對話，
        告知 agy 結果 (同意的指令加入白名單，不會再被中斷)。
     另外整段執行包在 report_new_packages() 中，事後比對 ArQ 環境套件變動作為保險。
3. 工作範圍 scope 與 Claude Code 相同 (見 tools/rd_common.py)："task" 在 task/，"core" 在專案根目錄修改 ArQ 本身。
   session_id (= agy 的 conversation_id) 可接續先前對話，用於把使用者對工程師提問的裁決交回原工程師。
4. prompt 以 `-p` 命令列參數傳遞 (agy.exe 是原生執行檔，不經 cmd.exe，多行參數不會被截斷)。
5. 逾時 (見 rd_common.Watchdog：閒置或超過總時長) 時以 Windows `taskkill /F /T /PID <pid>` 強制終止整個程序樹；
   等待使用者決定的時間不計入逾時 (詢問發生在兩次執行之間)。回傳 timed_out ("idle" / "total") 與 progressed，
   rd_dispatch 依此決定接續原對話或換人。

stream-json 事件格式 (2026-10 實測)：
    {"event": "init", "conversation_id": "..."}
    {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE",
        "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": "pip install x"}}}}
    {"event": "result", "result": {"status": "SUCCESS", "response": "最終回覆文字", ...}}

可用環境變數 (皆為選填)：
    AGY_MODE      傳給 `--mode` 的值 (accept-edits / auto / plan)；預設不傳 (部分帳號不支援 auto)
    AGY_IDLE_TIMEOUT  連續幾秒沒有任何事件就中止 (真的沒反應)，預設 600
    AGY_TIMEOUT       單次執行總時長上限 (最後保險)，預設 3600
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.config import PROJECT_ROOT
from tools.rd_common import (SCOPE_TASK, SCOPES, Watchdog, build_followup_prompt, build_plan_prompt, build_pm_prompt,
                             collect_worktree_state, rd_timeouts, timeout_message, workdir_env, workspace_for)
from tools.rd_jobs import DevJob
from tools.risk_policy import INSTALL_REASON, classify_risk, report_new_packages
from tools.secret_guard import child_env

logger = logging.getLogger(__name__)

# Antigravity 的工作目錄 (= ArQ 的任務隔離區，與 Claude Code 共用，已被 .gitignore 忽略)
ANTIGRAVITY_WORKSPACE = os.path.join(PROJECT_ROOT, "task")

# 預設安裝位置 (PATH 找不到時的備援)；可用 AGY_EXE 環境變數指定。repo 預設會公開，不寫死個人路徑
_DEFAULT_AGY_EXE = os.path.join(os.getenv("LOCALAPPDATA", ""), "agy", "bin", "agy.exe")

DEFAULT_MODEL = "gemini-3.8-flash-high"

# 一次任務最多「中斷 / 詢問 / 接續」的輪數，防止 agy 一直嘗試被拒絕的操作
MAX_RESUME_ROUNDS = 6

# agy 用來請求安裝套件的約定標記 (見 _build_pm_prompt)
_INSTALL_REQUEST_RE = re.compile(r"^\s*ARQ_INSTALL_REQUEST:\s*(.+?)\s*$", re.MULTILINE)

# tool_info.parameters 中可能代表 shell 指令的欄位名稱 (目前實測為 CommandLine)
_COMMAND_KEYS = {"commandline", "command", "cmd", "script"}

# 通知回呼：接收一段要給使用者看的 Markdown 文字。會在「背景執行緒」中被呼叫。
NotifyFn = Callable[[str], None]

# 同意回呼：接收 (標題, 詳細內容 Markdown)，「阻塞」直到使用者決定，回傳是否同意。會在背景執行緒中被呼叫。
ApproveFn = Callable[[str, str], bool]


def resolve_agy_executable() -> Optional[str]:
    """找出 agy 執行檔完整路徑：AGY_EXE 環境變數 → PATH → %LOCALAPPDATA%\\agy\\bin\\agy.exe"""
    configured = os.getenv("AGY_EXE", "").strip()
    if configured and os.path.isfile(configured):
        return configured
    exe = shutil.which("agy")
    if exe:
        return exe
    return _DEFAULT_AGY_EXE if os.path.isfile(_DEFAULT_AGY_EXE) else None


def _kill_process_tree(pid: int) -> None:
    """強制終止整個程序樹 (agy.exe 及其子程序)"""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def _safe_notify(notify: Optional[NotifyFn], text: str) -> None:
    """通知失敗不應中斷開發流程"""
    if notify is None:
        return
    try:
        notify(text)
    except Exception as e:
        logger.warning(f"⚠️ 發送通知失敗: {e}", exc_info=True)


def check_antigravity_auth() -> Dict[str, Any]:
    """檢查本機 Antigravity CLI (agy) 的認證登入狀態與可用模型清單 (執行 `agy models`)"""
    exe = resolve_agy_executable()
    if not exe:
        return {
            "authenticated": False,
            "models": [],
            "message": "找不到 agy 指令，請先安裝 Google Antigravity CLI 並確認已加入 PATH "
                       "(或安裝於 %LOCALAPPDATA%\\agy\\bin\\agy.exe，也可用 .env 的 AGY_EXE 指定)。",
        }

    try:
        proc = subprocess.run(
            [exe, "models"],
            cwd=ANTIGRAVITY_WORKSPACE,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        return {"authenticated": False, "models": [], "message": f"檢查 Antigravity CLI 狀態失敗: {e}"}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        message = "Antigravity CLI 尚未登入或授權已過期。請在本機終端機執行 `agy` 完成登入。"
        if detail:
            message += f"\n詳細資訊: {detail[:300]}"
        return {"authenticated": False, "models": [], "message": message}

    models: List[Dict[str, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("fetching"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            models.append({"id": parts[0].strip(), "name": parts[1].strip()})
        else:
            models.append({"id": line, "name": line})

    if not models:
        return {
            "authenticated": False,
            "models": [],
            "message": "Antigravity CLI 未回傳可用模型清單，可能尚未登入。請在本機終端機執行 `agy` 完成登入。",
        }

    return {
        "authenticated": True,
        "models": models,
        "message": f"Antigravity CLI 已就緒，共 {len(models)} 個可用模型。",
    }


# agy 的安裝套件規則：沒有 hook 可在執行前暫停，因此禁止自行安裝，改以約定標記提出請求
_INSTALL_RULE = (
    "【嚴禁自行安裝套件】：不得執行 pip / uv / npm / winget 等任何安裝指令，也不得包在腳本裡間接安裝。"
    "若需要新套件，請在回覆中獨立一行輸出 `ARQ_INSTALL_REQUEST: <完整安裝指令>`（每個指令一行），"
    "然後停下需要該套件的步驟；系統會詢問使用者，並在接續對話中告訴你結果。"
    "刪除檔案、結束程序、變更系統設定、git commit / push 等高風險指令會被系統即時中斷並詢問使用者；"
    "被拒絕時請勿重試或繞過，改在回報中說明需求與原因。"
)


def _build_pm_prompt(task_description: str, scope: str = SCOPE_TASK) -> str:
    """把使用者需求包裝成給 Antigravity CLI 的 PM 結構化指派 Prompt"""
    return build_pm_prompt(task_description, scope, _INSTALL_RULE)


def _snapshot_artifact_mtimes(artifact_dir: str) -> Dict[str, float]:
    if not os.path.isdir(artifact_dir):
        return {}
    return {name: os.path.getmtime(os.path.join(artifact_dir, name)) for name in os.listdir(artifact_dir)}


def _extract_tool_command(step: Dict[str, Any]) -> Optional[str]:
    """從 step_update 中取出 agy 正要執行的 shell 指令 (非 shell 類工具回傳 None)"""
    if step.get("step_type") != "tool":
        return None
    params = (step.get("tool_info") or {}).get("parameters") or {}
    for key, value in params.items():
        if key.lower() in _COMMAND_KEYS and isinstance(value, str):
            return value
    return None


def _run_agy_once(
    exe: str,
    prompt: str,
    model: str,
    conversation_id: Optional[str],
    timeouts: Tuple[float, float],
    approved_commands: set,
    workspace: str = "",
    job: Optional[DevJob] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    執行一次 agy (新對話或接續對話)，逐行監看 stream-json。timeouts = (閒置上限, 總時長上限) 秒數。
    遇到「未獲同意的安裝套件 / 高風險指令」時立即中斷整個程序樹並回傳 interrupted。
    回傳：{"conversation_id", "response", "status", "interrupted": (command, reason) | None,
           "timed_out": None | "idle" | "total", "progressed", "returncode", "other_output"}
    """
    cmd = [exe, "-p", prompt, "--model", model, "--dangerously-skip-permissions", "--output-format", "stream-json"]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    mode = os.getenv("AGY_MODE", "").strip()
    if mode:
        cmd += ["--mode", mode]

    # stderr 併入 stdout：只讀一條管線，避免兩條管線互相塞滿造成死結
    proc = subprocess.Popen(
        cmd,
        cwd=workspace or ANTIGRAVITY_WORKSPACE,
        env={**child_env(), **(extra_env or {})},  # 移除 .env 載入的機密，避免 agy 讀到 Discord Token / API 金鑰
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if job is not None:
        job.attach(proc.pid)  # 使用者按停止時結束整個程序樹
    watchdog = Watchdog(*timeouts, lambda kind: _kill_process_tree(proc.pid))
    watchdog.start()

    result: Dict[str, Any] = {
        "conversation_id": conversation_id, "response": "", "status": "",
        "interrupted": None, "timed_out": None, "progressed": False, "returncode": None, "other_output": "",
    }
    other_lines: List[str] = []
    try:
        for raw in proc.stdout:
            if job is not None and job.cancelled:
                break
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                other_lines.append(line)  # 例如 "warning: ..." 等非 JSON 訊息
                watchdog.touch()
                continue

            kind = event.get("event")
            watchdog.touch(progress=kind in ("step_update", "result"))  # init 不算進展
            if kind == "init" and event.get("conversation_id"):
                result["conversation_id"] = event["conversation_id"]
            elif kind == "step_update":
                step = event.get("step_update") or {}
                command = _extract_tool_command(step)
                if command and step.get("state") == "ACTIVE" and command not in approved_commands:
                    reason = classify_risk(command, "shell")
                    if reason:
                        _kill_process_tree(proc.pid)
                        result["interrupted"] = (command, reason)
                        break
            elif kind == "result":
                res = event.get("result") or {}
                result["response"] = res.get("response") or ""
                result["status"] = res.get("status") or ""
                result["conversation_id"] = res.get("conversation_id") or result["conversation_id"]
        proc.wait()
    finally:
        watchdog.cancel()
        if job is not None:
            job.detach(proc.pid)

    result["timed_out"] = watchdog.fired
    result["progressed"] = watchdog.progressed
    result["returncode"] = proc.returncode
    result["other_output"] = "\n".join(other_lines)
    return result


def run_antigravity_task(
    task_description: str,
    model: str = DEFAULT_MODEL,
    notify: Optional[NotifyFn] = None,
    approve: Optional[ApproveFn] = None,
    scope: str = SCOPE_TASK,
    session_id: str = "",
    job: Optional[DevJob] = None,
    plan: bool = False,
    workdir: str = "",
) -> Dict[str, Any]:
    """
    實際執行 Antigravity CLI 任務的核心函式 (同步、會阻塞數分鐘，請在執行緒中呼叫)。
    plan=True：只讀規劃。agy 沒有唯讀模式，只能以提示詞約束；結束後比對 git 狀態，有改動就回傳 plan_violation。
    job：rd_jobs.DevJob；使用者停止時結束程序樹、不再接續對話，回傳 cancelled=True。

    approve   ：agy 要安裝套件 / 執行高風險指令時呼叫，阻塞等待使用者決定。為 None 時一律拒絕 (fail-closed)。
    notify    ：中斷通知、事後套件變動等「知會」用途。
    scope     ："task" (task/) 或 "core" (專案根目錄)，見 tools/rd_common.py。
    workdir   ：scope=core 時該單的 worktree；空字串時在專案根目錄。
    session_id：非空時以 `--conversation` 接續該對話，task_description 視為給工程師的補充說明 / 使用者裁決。
    回傳：{"success", "stdout"(最終回覆), "stderr", "returncode", "artifacts"(task/artifact/ 新增或變更的檔案),
           "install_commands"(經同意的安裝指令), "blocked_commands"(被拒絕的指令),
           "interrupted_commands"(未經同意即開始執行而被中斷的指令), "session_id"[, "error"]}
    """
    if scope not in SCOPES:
        return {"success": False, "error": f"不支援的 scope「{scope}」，只能是 task 或 core。",
                "stdout": "", "stderr": "", "returncode": -1, "artifacts": [],
                "install_commands": [], "blocked_commands": [], "interrupted_commands": []}
    workspace = workspace_for(scope, workdir)
    exe = resolve_agy_executable()
    if not exe:
        return {
            "success": False,
            "error": "找不到 agy 指令，請先安裝 Google Antigravity CLI 並確認已加入 PATH。",
            "stdout": "", "stderr": "", "returncode": -1, "artifacts": [],
            "install_commands": [], "blocked_commands": [], "interrupted_commands": [],
        }

    model = (model or DEFAULT_MODEL).strip()
    timeouts = rd_timeouts("AGY")
    progressed = False  # 任一輪有進展就算
    artifact_dir = os.path.join(workspace_for(SCOPE_TASK), "artifact")
    os.makedirs(artifact_dir, exist_ok=True)
    before = _snapshot_artifact_mtimes(artifact_dir)

    approved_commands: set = set()       # 使用者已同意的指令 (之後執行不再中斷)
    install_commands: List[str] = []
    blocked_commands: List[str] = []
    interrupted_commands: List[str] = []

    def ask_user(command: str, reason: str) -> bool:
        if approve is None:
            return False
        detail = f"⚠️ **{reason}**\n```\n{command[:3000]}\n```"
        try:
            return bool(approve(f"讓 Antigravity 執行：{reason}", detail))
        except Exception as e:
            logger.warning(f"⚠️ 詢問使用者同意失敗，視為拒絕: {e}", exc_info=True)
            return False

    def decide(command: str, reason: str) -> bool:
        """詢問並記錄結果"""
        ok = ask_user(command, reason)
        if ok:
            approved_commands.add(command)
            if INSTALL_REASON in reason:
                install_commands.append(command)
        else:
            blocked_commands.append(command)
        return ok

    if plan:
        prompt = build_plan_prompt(task_description, scope)
        state_before = collect_worktree_state(workdir or PROJECT_ROOT)
    else:
        prompt = build_followup_prompt(task_description) if session_id else _build_pm_prompt(task_description, scope)
    conversation_id: Optional[str] = session_id or None
    run: Dict[str, Any] = {}
    other_output: List[str] = []

    with report_new_packages(lambda msg: _safe_notify(notify, msg), "Antigravity CLI"):
        for _ in range(MAX_RESUME_ROUNDS + 1):
            try:
                if job is not None and job.cancelled:
                    break
                run = _run_agy_once(exe, prompt, model, conversation_id, timeouts, approved_commands, workspace, job=job,
                                    extra_env=workdir_env(workdir))
                progressed = progressed or bool(run.get("progressed"))
            except Exception as e:
                return {"success": False, "error": str(e), "stdout": "", "stderr": "", "returncode": -1,
                        "artifacts": [], "install_commands": install_commands,
                        "blocked_commands": blocked_commands, "interrupted_commands": interrupted_commands}
            conversation_id = run["conversation_id"] or conversation_id
            if run["other_output"]:
                other_output.append(run["other_output"])
            if run["timed_out"] or (job is not None and job.cancelled):
                break

            # b. 未經同意就開始執行有風險的指令 → 已中斷，詢問使用者後接續
            if run["interrupted"]:
                command, reason = run["interrupted"]
                interrupted_commands.append(command)
                _safe_notify(notify, (
                    f"⛔ **Antigravity 未經同意嘗試執行（{reason}），已立即中斷**\n```\n{command[:800]}\n```"
                    "中斷前指令可能已開始執行，接下來會詢問您是否同意。"
                ))
                if not conversation_id:
                    break  # 沒有對話 ID 無法接續
                if decide(command, reason):
                    prompt = (f"使用者已同意執行：{command}\n此指令剛才被系統中斷，請先確認它的狀態，"
                              "必要時重新執行，然後繼續完成原本的任務。")
                else:
                    prompt = (f"使用者拒絕執行：{command}\n請勿重試或用其他方式繞過，"
                              "完成其餘可做的部分，並在最終回報中說明需要什麼以及原因。")
                continue

            # a. agy 依約定提出的安裝請求 → 逐一詢問後接續
            requests = [c for c in _INSTALL_REQUEST_RE.findall(run["response"])
                        if c not in approved_commands and c not in blocked_commands]
            if requests and conversation_id:
                lines = []
                for command in requests:
                    reason = classify_risk(command, "shell") or INSTALL_REASON
                    lines.append(f"- {'同意' if decide(command, reason) else '拒絕'}：{command}")
                prompt = ("使用者對你的安裝請求決定如下：\n" + "\n".join(lines) +
                          "\n同意的指令請直接執行；被拒絕的請勿重試或繞過。完成後繼續原本的任務並回報。")
                continue

            break  # 正常完成

    after = _snapshot_artifact_mtimes(artifact_dir)
    new_files = sorted(name for name, mtime in after.items() if before.get(name) != mtime)

    stdout = (run.get("response") or "").strip()
    stderr = "\n".join(other_output).strip()
    base = {
        "stdout": stdout[-8000:],
        "stderr": stderr[-1500:],
        "returncode": run.get("returncode", -1),
        "artifacts": [f"artifact/{name}" for name in new_files],
        "install_commands": install_commands,
        "blocked_commands": blocked_commands,
        "interrupted_commands": interrupted_commands,
        "session_id": conversation_id,
        "last_output": (stdout or stderr)[-1500:],
    }
    if plan:
        state_after = collect_worktree_state(workdir or PROJECT_ROOT)
        if state_after.get("status") != state_before.get("status"):
            base["plan_violation"] = state_after.get("status") or state_after.get("diff_stat") or "工作目錄有變動"

    if job is not None and job.cancelled:
        return {**base, "success": False, "cancelled": True, "returncode": -1, "error": "使用者已停止開發"}
    if run.get("timed_out"):
        # timed_out / progressed：rd_dispatch 依此決定帶 session_id 接續原對話，或真的沒反應才換人
        return {**base, "success": False, "returncode": -1, "timed_out": run["timed_out"], "progressed": progressed,
                "error": timeout_message("Antigravity CLI", run["timed_out"], *timeouts)}
    if run.get("interrupted"):
        return {**base, "success": False,
                "error": "Antigravity 執行了未經同意的指令，且無法接續對話或已超過接續輪數上限，任務已中止。"}

    success = run.get("returncode") == 0 and run.get("status", "SUCCESS") == "SUCCESS"
    if not success:
        base["error"] = stderr[-1500:] or stdout[-1500:] or f"Antigravity CLI 異常結束 (returncode={run.get('returncode')})"
    return {**base, "success": success}


def dispatch_antigravity_engineer_sync(
    task_description: str, model: str = DEFAULT_MODEL, scope: str = "task", session_id: str = "", ticket: int = 0,
    plan: bool = False,
) -> Dict[str, Any]:
    """
    調派 Antigravity CLI（agy，第二位 RD，獨立配額）。只在使用者明確指定 Antigravity / agy 時使用。

    Args:
        task_description: 同 Claude 派工（開發規格；plan 時填要研究的需求；接續時填裁決）。
        model: 預設 gemini-3.8-flash-high；另有 gemini-3.8-flash-medium / low、claude-sonnet-4-6、claude-opus-4-6-thinking、gpt-oss-120b-medium。
        scope: "task"（預設）或 "core"（修改 ArQ 本身，實作前需同意）。
        session_id: 選填，接續原對話。
        ticket: 選填，對應的單號。
        plan: true＝只讀規劃，回傳建議規格（不需同意、沒單開 todo）。
    """
    # 註：本函式只是給 Gemini 看的「工具外觀」。實際由 agent.py 改呼叫 tools/rd_dispatch.run_antigravity_first
    # (失敗由 Claude Code 接手)。直接呼叫本函式時沒有 approve，安裝 / 高風險指令一律被拒絕 (fail-closed)。
    return run_antigravity_task(task_description, model=model, scope=scope, session_id=session_id, plan=plan)
