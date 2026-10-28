"""
RD 結果的兩種呈現：給使用者的完整回報、給 PM 的精簡結果
"""

import json
from typing import Any, Dict, List, Optional

from tools.rd_common import ENGINE_LABELS, job_tag

# -------------------------------------------------------------
# 回報分流：完整回報直接給使用者，Gemini 只拿精簡結果
# 工具迴圈每一輪都會重送整段歷史，RD 的長篇輸出若進到 Gemini 會被反覆計費。
# -------------------------------------------------------------

MAX_MODEL_RESULT_CHARS = 1500   # compact_dev_result() 序列化後的總長上限
_REPORT_TAIL_CHARS = 600        # 保留給 PM 的成果結尾 (工程師提問通常在最後)
_MAX_LIST = 8


def _clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _tail(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _commands(res: Dict[str, Any], key: str) -> List[str]:
    return [_clip(c, 150) for c in (res.get(key) or [])[:_MAX_LIST]]


def commit_note(wt: Dict[str, str], number: Optional[int]) -> str:
    """使用者同意 commit 後 PM 要執行的指令：分支已由系統建立，全部在 worktree 內操作"""
    path, branch = wt.get("path", ""), wt.get("branch", "")
    closes = f"Closes #{number}" if number else "Closes #N"
    return (f'使用者同意後依序執行（分支 {branch} 已存在，不要再開分支）：git -C "{path}" add -A → '
            f'git -C "{path}" commit -m "<訊息>" → git -C "{path}" push -u origin {branch} → '
            f'gh pr create --head {branch}（內文第一行 {closes}）。')


def compact_dev_result(res: Dict[str, Any]) -> Dict[str, Any]:
    """給 Gemini 的派工結果：只留決策需要的欄位，序列化後不超過 MAX_MODEL_RESULT_CHARS"""
    if "accepted" in res:
        # 非同步派工的立即回應：RD 在背景開發，完成時完整回報直接送使用者，結果在下一輪轉達給 PM
        if not res["accepted"]:
            view = {"accepted": False, "ticket": res.get("ticket"), "error": _clip(res.get("error"), 300),
                    "note": "未派工。把原因告訴使用者，不要自行重試或改派。"}
            if res.get("blocked_by"):
                view["blocked_by"] = res["blocked_by"]
            return view
        return {"accepted": True, "job_id": res.get("job_id"), "ticket": res.get("ticket"),
                "engine_label": res.get("engine_label"),
                "note": "已在背景開發。完成時系統會把完整回報送給使用者，並在下一輪對話開頭告訴你結果；"
                        "現在用一句話告訴使用者已派工（單號、RD），不要等待、查詢進度或重複派工。"}
    if res.get("plan") and res.get("success"):
        # 只讀規劃：規格已完整顯示給使用者；PM 只需請使用者確認，確認後帶 session_id 實作
        view = {"success": True, "plan": True, "engine_label": res.get("engine_label"), "scope": res.get("scope"),
                "session_id": res.get("session_id"), "ticket": res.get("ticket"),
                "plan_violation": _clip(res.get("plan_violation"), 200) if res.get("plan_violation") else None,
                "plan_excerpt": _clip(res.get("stdout"), 700),
                "note": "規劃已完整顯示給使用者，不要重複轉述；用一兩句請使用者確認或調整未決問題。"
                        "確認後用同一個派工工具、plan=false，帶 session_id 與 ticket 實作"
                        "（task_description 寫「依剛才的規劃實作」加上使用者的調整）。"}
        if res.get("subagents"):
            view["subagents"] = [_clip(s, 100) for s in res["subagents"][:_MAX_LIST]]
        return view
    if res.get("cancelled"):
        # 使用者停止：只給 PM 最少的資訊，避免它自動重新派工
        return {"success": False, "cancelled": True, "engine_label": res.get("engine_label"),
                "scope": res.get("scope"), "session_id": res.get("session_id"), "ticket": res.get("ticket"),
                "note": "使用者已停止開發，完整狀態已顯示給使用者。不要重新派工或改派，只需一句確認並詢問使用者下一步。"}
    compact: Dict[str, Any] = {
        "success": bool(res.get("success")),
        "engine_label": res.get("engine_label"),
        "scope": res.get("scope"),
        "session_id": res.get("session_id"),
        "ticket": res.get("ticket"),
        "handoffs": [ENGINE_LABELS.get(a.get("engine"), a.get("engine")) for a in res.get("attempts") or []],
        "artifacts": [_clip(a, 120) for a in (res.get("artifacts") or [])[:_MAX_LIST]],
        "install_commands": _commands(res, "install_commands"),
        "blocked_commands": _commands(res, "blocked_commands"),
        "interrupted_commands": _commands(res, "interrupted_commands"),
        "note": "完整回報已直接顯示給使用者，不要重複轉述；只需一兩句摘要、轉達工程師提問與下一步。",
    }
    if res.get("subagents"):
        compact["subagents"] = [_clip(s, 100) for s in res["subagents"][:_MAX_LIST]]
    if not res.get("success"):
        compact["error"] = _clip(res.get("error") or res.get("stderr") or "未知錯誤", 300)
        if res.get("timed_out") and res.get("progressed"):  # 有進展的逾時已停下，不換人
            compact["timed_out"] = res["timed_out"]
            compact["note"] = ("RD 逾時但已有進展，系統已停下：不要改派或重新派工，"
                               "請使用者決定是否帶 session_id 接續原工程師。")
    if res.get("blocked_by"):  # 派工前被擋下：沒有工程師執行，轉告使用者被誰擋住
        compact["blocked_by"] = res["blocked_by"]
    wt = res.get("worktree_info")
    if wt:  # 改動在該單的 worktree：commit / push / 開 PR 都要在 worktree 內，不是 ArQ 執行資料夾
        compact["worktree_info"] = wt
        if res.get("success"):
            compact["commit_note"] = commit_note(wt, res.get("ticket"))
    if res.get("scope_violation"):
        # 以 task 派工卻改到 ArQ 本身：這其實是核心修改，後續照規則 9 的驗收與 PR 流程
        compact["scope_violation"] = [_clip(p, 120) for p in res["scope_violation"][:_MAX_LIST]]
        compact["scope_note"] = "工程師改到了 ArQ 本身的檔案（以 scope=task 派工），請當成核心修改，依規則 9 請使用者確認驗收卡。"
    acceptance = res.get("acceptance")
    if acceptance:
        compact["acceptance"] = {
            "tests_passed": acceptance.get("tests_passed"),
            "tests_summary": _tail(acceptance.get("tests_summary"), 150),
            "diff_stat": _tail(acceptance.get("diff_stat"), 200),
        }
    compact["report_tail"] = _tail(res.get("stdout"), _REPORT_TAIL_CHARS)
    # 仍超過上限時，從 report_tail 開始縮 (保留結尾)
    overflow = len(json.dumps(compact, ensure_ascii=False)) - MAX_MODEL_RESULT_CHARS
    if overflow > 0:
        compact["report_tail"] = _tail(compact["report_tail"], max(len(compact["report_tail"]) - overflow - 2, 0))
    return compact


def format_dev_report(res: Dict[str, Any]) -> str:
    """給使用者的完整回報 (Markdown，由 bot 分段送出)；與 /claude、/agy 斜線指令的回報內容一致"""
    label = res.get("engine_label") or "RD 工程師"
    if "accepted" in res:
        return ""  # 非同步派工的立即回應：派工訊息由停止按鈕訊息顯示，完成時才有完整回報
    if res.get("cancelled"):
        return format_cancel_report(res)
    if res.get("plan") and res.get("success"):
        return format_plan_report(res)
    tag = job_tag(res.get("ticket"), label)  # 「#77｜Claude Code」
    lines: List[str] = []
    if res.get("success"):
        lines.append(f"🚀 **【{tag}】開發任務完成**")
    else:
        lines.append(f"❌ **【{tag}】開發失敗**：{_clip(res.get('error') or res.get('stderr') or '未知錯誤', 800)}")
    if res.get("scope_violation"):
        files = (res.get("scope_violation") or [])[:_MAX_LIST]
        lines.append("⚠️ **範圍越界**：以 scope=task 派工，但工程師改到了 ArQ 本身的檔案（已照 scope=core 附上驗收卡，請確認後再 commit）：")
        lines += [f"• `{_clip(p, 150)}`" for p in files]
    if res.get("ticket"):
        lines.append(f"🎫 Ticket #{res['ticket']}")
    wt = res.get("worktree_info")
    if wt:
        lines.append(f"🌿 Worktree：`{wt.get('path')}`（分支 `{wt.get('branch')}`）")
    attempts = res.get("attempts") or []
    if attempts:
        lines.append("🔁 **接手紀錄**")
        lines += [f"• {ENGINE_LABELS.get(a.get('engine'), a.get('engine'))}：{_clip(a.get('error'), 150)}" for a in attempts]
    if res.get("success"):
        out = _tail(res.get("stdout"), 3000) or "（無額外輸出）"
        lines += ["**執行成果**", "```text", out.replace("```", "'''"), "```"]
    subagents = (res.get("subagents") or [])[:_MAX_LIST]
    if subagents:
        lines.append("🧩 **Sub agent**（平行處理的子任務）")
        lines += [f"• {_clip(s, 150)}" for s in subagents]
    for key, title in (("artifacts", "📁 **新增 / 變更的成果物**"),
                       ("install_commands", "📦 **經您同意執行的安裝指令**"),
                       ("blocked_commands", "🚫 **未獲您同意、已被阻擋的指令**"),
                       ("interrupted_commands", "⛔ **未經同意即開始執行而被中斷的指令**")):
        items = (res.get(key) or [])[:_MAX_LIST]
        if items:
            lines.append(title)
            lines += [f"• `{_clip(c, 150)}`" for c in items]
    acceptance = res.get("acceptance")
    if acceptance:
        passed = acceptance.get("tests_passed")
        lines += [
            f"🧾 **驗收卡**：單元測試 {'✅ 全數通過' if passed else '❌ 未通過'}",
            "```text", _tail(acceptance.get("diff_stat"), 600) or "(沒有偵測到已追蹤檔案的變更)",
            _tail(acceptance.get("tests_summary"), 300), "```",
            "確認無誤請回覆「commit」（或指定 commit 訊息）；改動需 `/reboot` 才生效。",
        ]
    return "\n".join(line for line in lines if line is not None)


def format_plan_report(res: Dict[str, Any]) -> str:
    """只讀規劃的回報：完整規格給使用者看，PM 接著請使用者確認未決問題"""
    label = res.get("engine_label") or "RD 工程師"
    lines = [f"📝 **【{job_tag(res.get('ticket'), label)}】規劃完成（只讀，未修改檔案）**"]
    if res.get("ticket"):
        lines.append(f"🎫 規劃已留言到 ticket #{res['ticket']}")
    if res.get("plan_violation"):
        lines.append(f"⚠️ **工作目錄在規劃期間有變動**，請檢查：\n```text\n{_clip(res['plan_violation'], 400)}\n```")
    lines.append(_clip(res.get("stdout"), 3500) or "（沒有輸出）")
    subagents = (res.get("subagents") or [])[:_MAX_LIST]
    if subagents:  # 系統偵測到的 sub agent
        lines.append("🧩 **Sub agent**（平行研究的部分）")
        lines += [f"• {_clip(s, 150)}" for s in subagents]
    lines.append("確認規格（或回覆要調整的地方）後，PM 會請同一位工程師接著實作。")
    return "\n".join(lines)


def format_cancel_report(res: Dict[str, Any]) -> str:
    """使用者停止後的回報：做到哪裡、留下哪些改動 (不自動還原)、如何接續"""
    label = res.get("engine_label") or "RD 工程師"
    lines: List[str] = [f"🛑 **【{job_tag(res.get('ticket'), label)}】已停止開發**（未改派給其他工程師）"]
    if res.get("ticket"):
        lines.append(f"🎫 Ticket #{res['ticket']} " + ("已留言（只讀規劃，不改狀態）" if res.get("plan") else "已改為 blocked"))
    last = _tail(res.get("last_output") or res.get("stdout"), 1200)
    lines += ["**停止前的最後輸出**", "```text", (last or "（沒有輸出）").replace("```", "'''"), "```"]
    worktree = res.get("worktree")
    if worktree is not None:
        diff = _tail(worktree.get("diff_stat"), 700) or "(沒有已追蹤檔案的變更)"
        untracked = [l for l in (worktree.get("status") or "").splitlines() if l.startswith("??")]
        where = (f"worktree `{res['worktree_info'].get('path')}`" if res.get("worktree_info")
                 else "ArQ 工作目錄")  # 起 scope=core 在該單的 worktree
        lines += [f"📄 **{where} 目前的改動**（不會自動還原，請自行決定保留或還原）", "```text", diff]
        if untracked:
            lines.append(_tail("\n".join(untracked), 400))
        lines.append("```")
    for key, title in (("install_commands", "📦 **停止前經您同意執行的安裝指令**"),
                       ("blocked_commands", "🚫 **未獲您同意、已被阻擋的指令**")):
        items = (res.get(key) or [])[:_MAX_LIST]
        if items:
            lines.append(title)
            lines += [f"• `{_clip(c, 150)}`" for c in items]
    if res.get("session_id"):
        lines.append(f"↩️ 之後要接續可以告訴 PM「接續剛才的開發」，session_id：`{res['session_id']}`")
    return "\n".join(lines)
