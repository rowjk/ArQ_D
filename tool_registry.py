"""
Gemini 工具註冊表 (從 agent.py 拆出)：ToolSpec、風險 / 防護函式與 _TOOL_SPECS。

只依賴 tools/*，不得 import agent (避免循環)。agent.py 會重新匯出這裡的名稱。

新增工具的方式：
    1. 在 tools/ 下寫一個「同步」函式，參數加上型別註記、docstring 寫清楚用途
       (SDK 會把函式簽名與 docstring 轉成 Gemini 看得到的工具說明)。
    2. 回傳值請用可 JSON 序列化的 dict (或字串/數字)。
    3. 在下方 _TOOL_SPECS 加一筆 ToolSpec；會執行指令 / 程式碼的工具務必提供 risk 判斷函式，
       並視需要在 agent.describe_tool_call() 補上顯示格式。
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from tools.system_tools import get_system_status, run_powershell_sync, execute_python_code
from tools.screen_tools import take_screenshot
from tools.file_tools import list_directory, read_text_file
from tools.search_tools import search_web
from tools.weather_tools import weather
from tools.ticket_tools import ticket, ticket_risk
from tools.http_tools import call_api, call_api_risk
from tools.shioaji_tools import shioaji
from tools.claude_tools import dispatch_claude_engineer_sync
from tools.antigravity_tools import dispatch_antigravity_engineer_sync
from tools.rd_dispatch import compact_dev_result, format_dev_report, start_antigravity_first, start_claude_first
from tools.risk_policy import classify_risk, code_read_violation, merge_violation, path_read_violation

# 風險判斷函式：接收工具參數，回傳「需要使用者同意的理由」；None = 無風險，直接執行
RiskFn = Callable[[Dict[str, Any]], Optional[str]]


@dataclass(frozen=True)
class ToolSpec:
    """一個可供 Gemini 呼叫的本地工具"""
    func: Callable[..., Any]            # 同步函式；名稱、參數與 docstring 即為 Gemini 看到的工具說明
    label: str                          # 顯示給使用者看的中文名稱 (用於確認訊息)
    risk: Optional[RiskFn] = None       # 風險判斷；None = 永遠直接執行 (唯讀工具)
    watch_packages: bool = False        # True = 執行後比對 ArQ 環境套件，有變動就通知 (保險機制)
    # 硬性拒絕 (選填)：回傳理由時不跳卡、不執行，直接把理由回給 Gemini。例如 merge 夾帶其他指令
    guard: Optional[RiskFn] = None
    # 實際執行的函式 (選填)。會以 func 的參數再加上 notify= / approve= 呼叫，
    # 讓長時間任務能在執行中途通知或詢問使用者。notify / approve 不能暴露給 Gemini，所以不放進 func。
    impl: Optional[Callable[..., Any]] = None
    # 回報分流 (選填)：user_report 把完整結果轉成文字，經 notify 直接送給使用者；
    # model_view 把結果縮成給 Gemini 的精簡版，避免長篇輸出在工具迴圈中被反覆重送
    user_report: Optional[Callable[[Dict[str, Any]], str]] = None
    model_view: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    # 背景執行：impl 另收 on_done=，立即回傳「已受理」；完成時 user_report 送使用者、
    # model_view 的結果放進待轉達佇列，PM 下一輪對話開頭讀到
    background: bool = False


def _shell_risk(args: Dict[str, Any]) -> Optional[str]:
    return classify_risk(str(args.get("command", "")), "shell")


def _python_risk(args: Dict[str, Any]) -> Optional[str]:
    return classify_risk(str(args.get("code", "")), "python")


def _shell_guard(args: Dict[str, Any]) -> Optional[str]:
    command = str(args.get("command", ""))
    return merge_violation(command, "shell") or code_read_violation(command, "shell")


def _python_guard(args: Dict[str, Any]) -> Optional[str]:
    code = str(args.get("code", ""))
    return merge_violation(code, "python") or code_read_violation(code, "python")


def _read_guard(args: Dict[str, Any]) -> Optional[str]:
    """PM 不得自己讀程式碼：task/ 以外的程式碼檔改派 plan=true"""
    return path_read_violation(str(args.get("filepath", "")))


def _dispatch_risk(args: Dict[str, Any]) -> Optional[str]:
    """派工給 RD：一般任務 (scope=task) 與只讀規劃 (plan=true) 直接執行；修改 ArQ 本身 (scope=core) 需使用者同意"""
    if args.get("plan") in (True, "true", "True", 1):
        return None
    return "修改 ArQ 核心程式碼" if str(args.get("scope", "task")).strip() == "core" else None


_TOOL_SPECS: List[ToolSpec] = [
    # 唯讀工具：一律直接執行
    ToolSpec(get_system_status, "查看系統狀態"),
    ToolSpec(take_screenshot, "螢幕截圖"),
    ToolSpec(list_directory, "列出目錄"),
    ToolSpec(read_text_file, "讀取文字檔", guard=_read_guard),  # 程式碼檔不讓 PM 自己讀
    ToolSpec(search_web, "上網搜尋"),
    ToolSpec(weather, "查詢台灣天氣"),
    ToolSpec(shioaji, "查詢永豐帳務與行情"),
    # 執行指令 / 程式碼：依內容判斷風險，安裝套件或高風險才需同意
    # merge 必須單獨一行執行 (guard 直接拒絕夾帶)，並跳出附 PR 資訊的獨立確認卡
    ToolSpec(run_powershell_sync, "執行 PowerShell 指令", risk=_shell_risk, guard=_shell_guard, watch_packages=True),
    ToolSpec(execute_python_code, "執行 Python 程式碼", risk=_python_risk, guard=_python_guard, watch_packages=True),
    # HTTP API：GET / HEAD 直接執行；POST / PUT / PATCH / DELETE 需同意 (金鑰綁定與唯讀交易見 tools/http_tools.py)
    ToolSpec(call_api, "呼叫 API", risk=call_api_risk),
    # Ticket (GitHub Issues)：只寫入使用者自己的 ticket repo，直接執行；開單者固定為 pm (見 tools/ticket_tools.py)
    # done / 關單需對應 PR 已 merge (程式檢查)；以 not planned 關單需同意
    ToolSpec(ticket, "管理 ticket", risk=ticket_risk),
    # RD 工程師派工 (寫程式任務的出口，見 prompts/pm_system.md 第 6~9 點)：
    # 兩個工具都走 tools/rd_dispatch.py，首選引擎失敗時自動由另一個接手；scope=core 派工前需使用者同意。
    # 開發途中的安裝套件 / 高風險指令：Claude 由 hook 逐一詢問；agy 由 stream-json 即時中斷後詢問。
    # 完整回報直接送 Discord，Gemini 只拿精簡結果 (見 rd_dispatch.format_dev_report / compact_dev_result)
    # 非同步：派出後立即回傳，RD 在背景開發；完成的精簡結果在 PM 下一輪對話開頭轉達
    ToolSpec(dispatch_claude_engineer_sync, "指派 Claude Code 開發", risk=_dispatch_risk, impl=start_claude_first,
             user_report=format_dev_report, model_view=compact_dev_result, background=True),
    ToolSpec(dispatch_antigravity_engineer_sync, "調派 Antigravity CLI", risk=_dispatch_risk, impl=start_antigravity_first,
             user_report=format_dev_report, model_view=compact_dev_result, background=True),
]

TOOL_REGISTRY: Dict[str, ToolSpec] = {spec.func.__name__: spec for spec in _TOOL_SPECS}

# 派工給 RD 的工具：呼叫後本則訊息改用深度思考 (驗收與回報需要推理)
_DISPATCH_TOOLS = {"dispatch_claude_engineer_sync", "dispatch_antigravity_engineer_sync"}
