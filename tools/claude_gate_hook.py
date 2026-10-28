"""
Claude Code PreToolUse Hook：安裝套件 / 高風險指令需經使用者同意

由 tools/claude_tools.py 透過 `claude --settings <json>` 掛載，Claude 每次執行 Bash / PowerShell
指令前，Claude Code 會先執行本腳本 (stdin 為 JSON)：
    {"tool_name": "Bash", "tool_use_id": "toolu_...", "tool_input": {"command": "pip install x"}, ...}

流程 (與 claude_tools.run_claude_task 以「檔案交握」協作)：
    1. 用 risk_policy.classify_risk() 判斷指令；無風險 → exit 0 直接放行。
    2. 有風險 → 在 $ARQ_APPROVAL_DIR 等待 `<tool_use_id>.json` 出現。
       這個檔案由 ArQ 寫入：ArQ 從 stream-json 看到同一個 tool_use 後，在 Discord 詢問使用者，
       再把結果 {"approved": true/false} 寫進來。
    3. 同意 → exit 0 放行；拒絕或逾時 → exit 2 阻擋 (stderr 內容會回饋給 Claude)。

注意：bypassPermissions 模式下 hook 仍會執行，這是我們能在「自主模式」中插入同意關卡的唯一位置。
"""

import json
import os
import sys
import time

# 讓本腳本可以 import ArQ 的 tools 套件 (Claude Code 以獨立程序執行本檔)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tools.risk_policy import classify_risk  # noqa: E402

# 等待使用者決定的秒數，需略短於 claude_tools 設定的 hook timeout
WAIT_SECONDS = int(os.getenv("ARQ_APPROVAL_WAIT", "930"))
POLL_INTERVAL = 0.5


def _block(message: str) -> None:
    """exit 2 = 阻擋此工具呼叫；stderr 會成為 Claude 看到的回饋"""
    print(message, file=sys.stderr)
    sys.exit(2)


def main() -> None:
    # Claude Code 以 UTF-8 讀取 hook 的 stdin/stderr；中文 Windows 預設 cp950，不設定會變亂碼 (已實測)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # 無法解析就不干預 (非 shell 指令)

    command = (data.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        sys.exit(0)

    reason = classify_risk(command, "shell")
    if not reason:
        sys.exit(0)

    approval_dir = os.getenv("ARQ_APPROVAL_DIR")
    tool_use_id = data.get("tool_use_id")
    if not approval_dir or not tool_use_id:
        _block(f"此指令涉及「{reason}」，需要使用者同意，但目前無法向使用者詢問，已阻擋。請改用其他方式或在最終回報中說明需求。")

    decision_file = os.path.join(approval_dir, f"{tool_use_id}.json")
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if os.path.exists(decision_file):
            try:
                with open(decision_file, encoding="utf-8") as f:
                    approved = bool(json.load(f).get("approved"))
            except Exception:
                time.sleep(POLL_INTERVAL)  # 檔案可能還在寫入
                continue
            if approved:
                sys.exit(0)
            _block(
                f"使用者拒絕了此指令（{reason}）。請不要重試或改用其他方式繞過；"
                "若此步驟是必要的，請在最終回報中說明需要什麼以及原因。"
            )
        time.sleep(POLL_INTERVAL)

    _block(f"等待使用者同意逾時（{reason}），此指令已被阻擋。請在最終回報中說明需要什麼以及原因。")


if __name__ == "__main__":
    main()
