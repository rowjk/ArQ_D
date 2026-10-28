"""
Claude Code (RD 工程師) 無頭調度工具

ArQ (Gemini, PM 角色) 透過本模組呼叫本機的 Claude Code CLI (`claude -p`) 進行實際開發。

設計說明 (修改前請先理解)：
1. 自主執行：權限模式預設 `bypassPermissions`，Claude 可自行編輯檔案、執行一般指令，
   中途不會停下來詢問。這是使用者刻意的選擇 (讓 RD 工程師自主開發，不要縮太緊)。
   - 若日後想收緊，可在 .env 設 CLAUDE_PERMISSION_MODE=acceptEdits 並以 CLAUDE_ALLOWED_TOOLS
     指定放行的工具，例如 "Bash(python:*),Bash(pytest:*),WebSearch,WebFetch"。
2. 例外：安裝套件與高風險指令必須先經使用者同意 (使用者規則，判定見 tools/risk_policy.py)。
   實作為「PreToolUse hook + 檔案交握」，bypass 模式下 hook 依然有效：
     a. 以 `--settings` 掛載 tools/claude_gate_hook.py；Claude 每次執行 shell 指令前會先跑它。
     b. 本模組以 `--output-format stream-json` 逐行讀取 Claude 的動作，看到有風險的 tool_use 時，
        呼叫 approve() 在 Discord 詢問使用者，並把結果寫入 <approval_dir>/<tool_use_id>.json。
     c. hook 在該檔案出現前會等待；同意則放行，拒絕 / 逾時則以 exit 2 阻擋並告知 Claude。
   等待使用者決定的時間不計入逾時 (閒置與總時長都不計)。
3. 工作範圍 scope (見 tools/rd_common.py)：預設 "task" 在 task/ 工作；"core" 在專案根目錄修改 ArQ 本身。
   session_id 可接續先前的 Claude 對話 (`--resume`)，用於把使用者對工程師提問的裁決交回原工程師。
4. 任務描述透過 stdin 傳入，而非命令列參數：
   - 經由 claude.cmd (cmd.exe) 傳遞時，多行參數只有第一行會送達 (已實測)，& | 等字元也會被解譯。
5. 逾時 (見 rd_common.Watchdog) 時以 `taskkill /T` 終止整個程序樹，避免留下孤兒 node 程序。
   回傳 timed_out ("idle" / "total") 與 progressed (逾時前是否有進展)，rd_dispatch 依此決定接續原對話或換人。
6. 任務結束後比對 ArQ 環境的套件快照，抓出繞過指令比對的安裝並通知使用者。

可用環境變數 (皆為選填)：
    CLAUDE_MODEL            Claude 模型 ID，預設 claude-sonnet-5-5 (Sonnet 5.5)
    CLAUDE_PERMISSION_MODE  權限模式，預設 bypassPermissions
    CLAUDE_ALLOWED_TOOLS    額外放行的工具 (逗號分隔)，預設不設定 (bypass 模式下不需要)
    CLAUDE_IDLE_TIMEOUT     連續幾秒沒有任何事件就中止 (真的沒反應)，預設 600
    CLAUDE_TIMEOUT          單次任務總時長上限 (最後保險)，預設 3600；兩者都不含等待使用者同意的時間
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.config import PROJECT_ROOT, temp_path
from tools.rd_common import (SCOPE_TASK, SCOPES, Watchdog, build_followup_prompt, build_plan_prompt, build_pm_prompt,
                             extract_subagents, rd_timeouts, timeout_message, workdir_env, workspace_for)
from tools.rd_jobs import DevJob
from tools.risk_policy import INSTALL_REASON, classify_risk, report_new_packages
from tools.secret_guard import child_env as secret_child_env

logger = logging.getLogger(__name__)

# Claude 的安裝套件規則 (有 hook，執行前會自動暫停詢問使用者)
_INSTALL_RULE = (
    "安裝套件、刪除檔案、git commit / push 等高風險指令執行前，系統會自動暫停並請使用者同意："
    "請直接使用 pip / uv / npm 等指令安裝（不要包在腳本裡間接安裝）；若被拒絕，請勿重試或繞過，改在回報中說明需求與原因。"
)

# 預設模型：Sonnet 5.5 (使用者指定；可用 .env 的 CLAUDE_MODEL 覆寫)
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"

# 預設權限模式：完全自主 (見檔案開頭說明 1)
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# 預設不額外指定工具白名單；bypassPermissions 模式下所有工具皆可用。
# 只有改用 acceptEdits / default 等較嚴格模式時，CLAUDE_ALLOWED_TOOLS 才有意義。
DEFAULT_ALLOWED_TOOLS = ""

# .env.example 中的佔位字串，視同未設定
_PLACEHOLDER_KEYS = {"", "your_anthropic_api_key_here"}

# PreToolUse hook 腳本 (安裝套件 / 高風險指令的同意關卡)
GATE_HOOK_SCRIPT = os.path.join(PROJECT_ROOT, "tools", "claude_gate_hook.py")

# hook 的逾時秒數 (需大於 hook 內部等待使用者的 ARQ_APPROVAL_WAIT 預設 930 秒)
GATE_HOOK_TIMEOUT = 960

# 通知回呼：接收一段要給使用者看的 Markdown 文字。會在「背景執行緒」中被呼叫。
NotifyFn = Callable[[str], None]

# 同意回呼：接收 (標題, 詳細內容 Markdown)，「阻塞」直到使用者決定，回傳是否同意。
# 會在背景執行緒中被呼叫；呼叫端需自行把詢問轉送到 Discord。
ApproveFn = Callable[[str, str], bool]


def _anthropic_api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    return "" if key in _PLACEHOLDER_KEYS else key


def _resolve_claude_executable() -> Optional[str]:
    """找出 claude 執行檔完整路徑 (npm 安裝為 claude.cmd，原生安裝為 claude.exe)"""
    return shutil.which("claude")


def _kill_process_tree(pid: int) -> None:
    """強制終止整個程序樹 (claude.cmd → node → 其子程序)"""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def check_claude_auth() -> Dict[str, Any]:
    """檢查本機 Claude Code 的認證登入狀態"""
    if _anthropic_api_key():
        return {"authenticated": True, "method": "api_key", "message": "已偵測到 ANTHROPIC_API_KEY"}

    exe = _resolve_claude_executable()
    if not exe:
        return {
            "authenticated": False,
            "method": "none",
            "message": "找不到 claude 指令，請先安裝 Claude Code 並確認已加入 PATH。",
        }

    try:
        proc = subprocess.run(
            [exe, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
        # `claude auth status` 會輸出 JSON，例如 {"loggedIn": true, "authMethod": "claude.ai", ...}
        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError:
            info = {}
        if info.get("loggedIn") is True:
            return {
                "authenticated": True,
                "method": info.get("authMethod", "oauth"),
                "message": "Claude Code OAuth 登入中",
            }
        return {
            "authenticated": False,
            "method": "none",
            "message": "Claude Code 尚未登入或授權已過期。請在本機終端機執行 `claude auth login` 登入，或在 .env 設定 ANTHROPIC_API_KEY。",
        }
    except Exception as e:
        return {"authenticated": False, "method": "unknown", "message": f"檢查 Claude Code 狀態失敗: {e}"}


def _write_gate_settings(approval_dir: str) -> str:
    """產生掛載 PreToolUse hook 的 settings 檔，回傳其路徑"""
    # hook 指令由 Claude Code 透過 shell 執行；路徑用正斜線並加引號，在 Git Bash 與 cmd 皆可用
    python = sys.executable.replace("\\", "/")
    script = GATE_HOOK_SCRIPT.replace("\\", "/")
    settings = {
        "hooks": {
            "PreToolUse": [{
                "matcher": "Bash|PowerShell",
                "hooks": [{"type": "command", "command": f'"{python}" "{script}"', "timeout": GATE_HOOK_TIMEOUT}],
            }]
        }
    }
    path = os.path.join(approval_dir, "settings.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f)
    return path


def _build_claude_command(exe: str, settings_path: str, session_id: str = "",
                          permission_mode: str = "") -> List[str]:
    """
    組出 Claude Code CLI 參數 (任務內容不在此，改由 stdin 傳入)；session_id 非空時接續該對話。
    permission_mode：指定時覆寫 .env 設定，例如只讀規劃用 "plan" (實測 -p 模式下只讀不改檔，規格在最終結果)。
    """
    model_name = os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL).strip() or DEFAULT_CLAUDE_MODEL
    permission_mode = (permission_mode or os.getenv("CLAUDE_PERMISSION_MODE", DEFAULT_PERMISSION_MODE).strip()
                       or DEFAULT_PERMISSION_MODE)
    allowed_tools = os.getenv("CLAUDE_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS).strip()

    cmd = [
        exe, "-p",
        "--model", model_name,
        "--permission-mode", permission_mode,
        # 掛載同意關卡 hook (會與使用者自己的 Claude 設定合併，不會覆蓋)
        "--settings", settings_path,
        # stream-json 會逐行輸出每個事件 (含 Claude 執行的每個指令)，-p 模式下必須搭配 --verbose
        "--output-format", "stream-json", "--verbose",
    ]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def _safe_notify(notify: Optional[NotifyFn], text: str) -> None:
    """通知失敗不應中斷開發流程"""
    if notify is None:
        return
    try:
        notify(text)
    except Exception as e:
        logger.warning(f"⚠️ 發送通知失敗: {e}", exc_info=True)


def _iter_tool_commands(event: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    從 stream-json 的 assistant 事件中取出 Claude 要執行的 shell 指令，回傳 [(tool_use_id, command)]。
    事件格式：{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_...",
              "name": "Bash", "input": {"command": "pip install x"}}, ...]}}
    Windows 上工具名稱可能是 Bash 或 PowerShell，因此只看 input.command 是否存在。
    tool_use_id 與 PreToolUse hook 收到的 tool_use_id 相同 (已實測)，用來配對同意結果。
    """
    if event.get("type") != "assistant":
        return []
    content = (event.get("message") or {}).get("content") or []
    commands = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            command = (block.get("input") or {}).get("command")
            if isinstance(command, str) and block.get("id"):
                commands.append((block["id"], command))
    return commands


# Claude 的 stream-json 中代表「有在做事」的事件：模型輸出 (assistant) 與工具結果 (user)；init 等系統事件不算進展
_PROGRESS_EVENTS = {"assistant", "user"}


def run_claude_task(
    task_description: str,
    notify: Optional[NotifyFn] = None,
    approve: Optional[ApproveFn] = None,
    scope: str = SCOPE_TASK,
    session_id: str = "",
    job: Optional[DevJob] = None,
    plan: bool = False,
    workdir: str = "",
) -> Dict[str, Any]:
    """
    實際執行 Claude Code 任務的核心函式 (同步、會阻塞數分鐘，請在執行緒中呼叫)。
    plan=True：只讀規劃，以 --permission-mode plan 執行，回傳的 stdout 是建議規格。

    approve   ：Claude 要執行安裝套件 / 高風險指令時呼叫，阻塞等待使用者決定。為 None 時一律拒絕 (fail-closed)。
    notify    ：事後偵測到 ArQ 環境套件變動等「知會」用途。
    scope     ："task" (task/) 或 "core" (專案根目錄)，見 tools/rd_common.py。
    workdir   ：scope=core 時該單的 worktree；空字串時在專案根目錄。
    session_id：非空時以 `--resume` 接續該對話，task_description 視為給工程師的補充說明 / 使用者裁決。
    job       ：rd_jobs.DevJob；使用者停止時會結束程序樹，回傳 cancelled=True。
    回傳：{"success", "stdout"(最終成果), "stderr", "returncode", "session_id",
           "install_commands"(已同意的安裝指令), "blocked_commands"(被拒絕的指令), "subagents"(開出的 sub agent), "cost_usd",
           "last_output"(最後一段文字輸出)[, "cancelled"][, "error"]}
    """
    if scope not in SCOPES:
        return {"success": False, "error": f"不支援的 scope「{scope}」，只能是 task 或 core。",
                "stdout": "", "stderr": "", "returncode": -1, "install_commands": [], "blocked_commands": []}
    workspace = workspace_for(scope, workdir)
    exe = _resolve_claude_executable()
    if not exe:
        return {
            "success": False,
            "error": "找不到 claude 指令，請先安裝 Claude Code 並確認已加入 PATH。",
            "stdout": "", "stderr": "", "returncode": -1, "install_commands": [], "blocked_commands": [],
        }

    if scope == SCOPE_TASK:
        os.makedirs(os.path.join(workspace, "artifact"), exist_ok=True)
    idle_timeout, total_timeout = rd_timeouts("CLAUDE")

    # 每次任務一個交握目錄：hook 在此等待 <tool_use_id>.json
    approval_dir = temp_path("approvals", uuid.uuid4().hex)
    os.makedirs(approval_dir, exist_ok=True)
    settings_path = _write_gate_settings(approval_dir)

    # 移除 .env 載入的機密 (Discord Token、Gemini / CWA 金鑰…)，避免 Claude 能讀到；
    # 只有在設定了有效 ANTHROPIC_API_KEY 時才保留它 (佔位字串不傳，否則會蓋掉有效的 OAuth 登入)
    child_env = secret_child_env(keep=["ANTHROPIC_API_KEY"] if _anthropic_api_key() else [])
    child_env["ARQ_APPROVAL_DIR"] = approval_dir
    child_env.update(workdir_env(workdir))  # worktree 內 uv 沿用 ArQ 的 .venv

    result_text = ""
    last_text = ""                      # 最後一段 assistant 文字 (停止時回報做到哪裡)
    is_error = False
    cost_usd = None
    new_session_id = session_id
    other_lines: List[str] = []         # 非 JSON 的輸出 (錯誤訊息等)
    install_commands: List[str] = []    # 已獲同意的安裝指令
    blocked_commands: List[str] = []    # 被拒絕 / 逾時的高風險指令
    subagents: List[str] = []           # 開出的 sub agent
    watchdog: Optional[Watchdog] = None

    def ask_user(command: str, reason: str) -> bool:
        """詢問使用者是否同意執行高風險指令 (阻塞)"""
        if approve is None:
            return False
        detail = f"⚠️ **{reason}**\n```\n{command[:3000]}\n```"
        try:
            return bool(approve(f"讓 Claude Code 執行：{reason}", detail))
        except Exception as e:
            logger.warning(f"⚠️ 詢問使用者同意失敗，視為拒絕: {e}", exc_info=True)
            return False

    try:
        with report_new_packages(lambda msg: _safe_notify(notify, msg), "Claude Code"):
            try:
                # stderr 併入 stdout：只讀一條管線，避免兩條管線互相塞滿造成死結
                proc = subprocess.Popen(
                    _build_claude_command(exe, settings_path, session_id, permission_mode="plan" if plan else ""),
                    cwd=workspace,
                    env=child_env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as e:
                return {"success": False, "error": str(e), "stdout": "", "stderr": "", "returncode": -1,
                        "install_commands": [], "blocked_commands": []}

            if job is not None:
                job.attach(proc.pid)  # 使用者按停止時結束整個程序樹

            # 逐行讀取時無法用 communicate(timeout)，改用 Watchdog 在閒置或超過總時長時砍掉整個程序樹
            watchdog = Watchdog(idle_timeout, total_timeout, lambda kind: _kill_process_tree(proc.pid))
            watchdog.start()
            try:
                if plan:
                    prompt = build_plan_prompt(task_description, scope)
                elif session_id:
                    prompt = build_followup_prompt(task_description)
                else:
                    prompt = build_pm_prompt(task_description, scope, _INSTALL_RULE)
                proc.stdin.write(prompt)
                proc.stdin.close()

                for raw in proc.stdout:
                    if job is not None and job.cancelled:
                        break  # 程序樹已被結束；不再處理剩下的輸出
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        other_lines.append(line)
                        watchdog.touch()
                        continue
                    watchdog.touch(progress=event.get("type") in _PROGRESS_EVENTS)

                    # 記下對話 ID (init 與 result 事件都會帶)，之後可用 --resume 接續
                    if event.get("session_id"):
                        new_session_id = event["session_id"]
                    if event.get("type") == "assistant":
                        texts = [b.get("text", "") for b in (event.get("message") or {}).get("content") or []
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        if any(t.strip() for t in texts):
                            last_text = "\n".join(t for t in texts if t.strip())

                    subagents.extend(extract_subagents(event))
                    # sub agent 的指令事件帶 parent_tool_use_id，一樣走同意交握 (hook 也會攔到)
                    for tool_use_id, command in _iter_tool_commands(event):
                        reason = classify_risk(command, "shell")
                        if not reason:
                            continue  # 無風險：hook 會直接放行，不需交握
                        watchdog.pause()
                        approved = ask_user(command, reason)
                        watchdog.resume()
                        with open(os.path.join(approval_dir, f"{tool_use_id}.json"), "w", encoding="utf-8") as f:
                            json.dump({"approved": approved}, f)
                        if not approved:
                            blocked_commands.append(command)
                        elif INSTALL_REASON in reason:
                            install_commands.append(command)

                    if event.get("type") == "result":
                        result_text = event.get("result") or ""
                        is_error = bool(event.get("is_error"))
                        cost_usd = event.get("total_cost_usd")
                proc.wait()
            finally:
                watchdog.cancel()
                if job is not None:
                    job.detach(proc.pid)
    finally:
        shutil.rmtree(approval_dir, ignore_errors=True)

    stderr = "\n".join(other_lines).strip()
    base = {
        "stdout": result_text.strip(),
        "stderr": stderr[-1500:],
        "returncode": proc.returncode,
        "session_id": new_session_id,
        "install_commands": install_commands,
        "blocked_commands": blocked_commands,
        "subagents": subagents,
        "cost_usd": cost_usd,
        "last_output": (result_text or last_text).strip()[-1500:],
    }

    if job is not None and job.cancelled:
        return {**base, "success": False, "cancelled": True, "returncode": -1, "error": "使用者已停止開發"}

    if watchdog is not None and watchdog.fired:
        # timed_out / progressed：rd_dispatch 依此決定帶 session_id 接續原對話，或真的沒反應才換人
        return {**base, "success": False, "returncode": -1, "timed_out": watchdog.fired,
                "progressed": watchdog.progressed,
                "error": timeout_message("Claude Code", watchdog.fired, idle_timeout, total_timeout)}

    if "Failed to authenticate" in stderr or "Failed to authenticate" in result_text:
        return {**base, "success": False,
                "error": "Claude Code 認證過期或尚未登入。請在本機終端機執行 `claude auth login` 或在 .env 設定 ANTHROPIC_API_KEY。"}

    success = proc.returncode == 0 and not is_error
    if not success:
        # is_error=True 時 (例如額度用完、模型不存在、API 錯誤)，錯誤原因放在 result 文字裡；
        # 一定要填 error，rd_dispatch 會用它通知使用者並寫進接手說明
        base["error"] = (
            (base["stdout"][-1500:] if is_error else "")
            or stderr[-1500:]
            or base["stdout"][-1500:]
            or f"Claude Code 異常結束 (returncode={proc.returncode})"
        )
    return {**base, "success": success}


def dispatch_claude_engineer_sync(task_description: str, scope: str = "task", session_id: str = "",
                                  ticket: int = 0, plan: bool = False) -> Dict[str, Any]:
    """
    調派 Claude Code（RD，寫程式的預設工程師）開發、除錯與測試。

    Args:
        task_description: 開發規格（目標、輸入輸出、邊界、驗收標準、TDD）；plan 時填要研究的需求；接續對話時填使用者的裁決。
        scope: "task"（預設，只動 task/）或 "core"（修改 ArQ 本身；實作前需使用者同意，完成附 acceptance）。
        session_id: 選填，接續同一位工程師的原對話（例如規劃確認後實作）。
        ticket: 選填，對應的單號；新派工不必填，系統會自動開單。
        plan: true＝只讀規劃：工程師只研究不改檔，回傳建議規格（不需同意、沒單開 todo）。
    """
    # 註：本函式只是給 Gemini 看的「工具外觀」(docstring 與參數會變成工具說明)。
    # 實際由 agent.py 改呼叫 tools/rd_dispatch.run_claude_first (失敗由 Antigravity 接手)。
    # 直接呼叫本函式時沒有 approve，高風險指令一律被拒絕 (fail-closed)。
    return run_claude_task(task_description, scope=scope, session_id=session_id, plan=plan)
