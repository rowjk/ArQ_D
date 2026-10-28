"""
ArQ AI 代理核心 (Google GenAI SDK + 手動 Function Calling 迴圈)

整體流程 (一則使用者訊息)：
    bot.py on_message
      └─> ArQAgent.chat(prompt, user_id, confirm, notify)
            ├─ 取得該使用者的 per-user Lock (同一人同時只處理一則訊息)
            ├─ chat_session.send_message(prompt)
            └─ 工具迴圈 (最多 MAX_TOOL_ROUNDS 輪)：
                 模型回傳 function_calls
                   → 查 TOOL_REGISTRY
                   → 以 ToolSpec.risk 判斷這次呼叫的風險 (tools/risk_policy.py)：
                       · 無風險 → 直接執行
                       · 安裝套件 / 高風險 → confirm() 在 Discord 跳按鈕，使用者同意才執行
                   → 執行工具 (丟到執行緒，不阻塞事件迴圈)
                       · Claude Code 開發途中要安裝套件 / 執行高風險指令時，也會即時透過 confirm() 詢問
                   → 將結果以 FunctionResponse 回傳給模型
                 直到模型只回文字為止

使用者規則 (修改時必須維持)：
    1. 只要需要安裝新套件，一定要先獲得使用者同意。
    2. 其他任務若沒有風險，直接執行，不要打擾使用者。
    3. 有風險的操作 (刪除、關機、改系統設定、git push…) 需先獲得同意。

為什麼不用 SDK 的「自動函式呼叫 (Automatic Function Calling, AFC)」？
    1. AFC 會在 SDK 內部直接執行工具，我們無法在執行前插入「使用者同意」步驟。
       網頁搜尋結果或檔案內容可能夾帶惡意指示 (Prompt Injection)，必須保留攔截點。
    2. 舊版程式同時依賴 AFC 又手動處理 function_calls，AFC 達呼叫上限時
       會導致同一個工具被執行兩次。
    因此設定 AutomaticFunctionCallingConfig(disable=True)，全部由本模組控制。

新增工具的方式：
    1. 在 tools/ 下寫一個「同步」函式，參數加上型別註記、docstring 寫清楚用途
       (SDK 會把函式簽名與 docstring 轉成 Gemini 看得到的工具說明)。
    2. 回傳值請用可 JSON 序列化的 dict (或字串/數字)。
    3. 在 tool_registry.py 的 _TOOL_SPECS 加一筆 ToolSpec；會執行指令 / 程式碼的工具務必提供 risk 判斷函式，
       並視需要在 describe_tool_call() 補上顯示格式。
"""

import asyncio
import atexit
import json
import logging
import threading
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# 暫時性錯誤的 HTTP 狀態碼：429 (額度/速率限制)、5xx (伺服器端暫時問題)，先退避重試，不要馬上重置對話
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 2

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from tools.config import PROJECT_ROOT, env_int
from tools.system_tools import get_system_status, run_powershell
from tools.screen_tools import take_screenshot
from tools import chat_store, mcp_manager, ticket_tools, worktree
from tools.secret_guard import redact, redact_obj
from tools.risk_policy import MERGE_REASON, extract_pr_number, report_new_packages
from tools.gemini_cache import PrefixCache, explicit_cache_enabled
from tools.gemini_settings import deep_thinking_level, make_thinking_config, thinking_level, wants_deep_thinking
from tools.usage_log import add_usage, empty_usage, record_usage, usage_of
from tools.thread_bridge import ThreadBridge

try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False


# -------------------------------------------------------------
# 工具註冊表 (拆到 tool_registry.py，這裡重新匯出讓既有的 agent.xxx 引用不會壞)
# -------------------------------------------------------------

from tool_registry import ToolSpec, _TOOL_SPECS, TOOL_REGISTRY, _DISPATCH_TOOLS  # noqa: E402,F401
from tool_registry import (  # noqa: E402,F401
    _shell_risk, _python_risk, _shell_guard, _python_guard, _read_guard, _dispatch_risk)

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """讀取正整數環境變數；未設定、格式錯誤或 ≤ 0 時用預設值"""
    return env_int(name, default, minimum=1)


# 單則訊息內，模型最多可連續呼叫工具的輪數 (防止無限迴圈)。
# 用量紀錄中一般訊息最多 5 次呼叫、commit + 開 PR 約 6 輪；曾有一則跑 15 輪用掉 9 萬 Token
MAX_TOOL_ROUNDS = _int_env("GEMINI_MAX_TOOL_ROUNDS", 12)

# 單則訊息累計 Token 預算 (輸入 + 輸出)：超過且模型還要呼叫工具時，請使用者決定是否繼續
MESSAGE_TOKEN_BUDGET = _int_env("GEMINI_MESSAGE_TOKEN_BUDGET", 60000)

# 回傳給模型的單一字串欄位長度上限 (避免 Claude 長篇輸出灌爆 context)
MAX_TOOL_RESULT_CHARS = 8000

# 對話歷史上限：每次呼叫 Gemini 都會把整段歷史重送一次 (工具迴圈的每一輪也是)，歷史越長越貴。
# 送出新訊息前，若超過「輪數」或「字元數」任一上限，就從最舊的一輪開始丟棄。
# 一輪 = 一則使用者訊息 + 其後的工具呼叫與回覆；以整輪為單位裁切，不會留下沒有回應的 function_call。
# 需要舊脈絡時 PM 會用 ticket 工具查 (實測不需改提示詞)，所以歷史只保留近期對話。
# 實測同一組 10 則問答：10 輪 / 30,000 字時輸入持續成長 (第 10 則 6,317)；6 輪 / 15,000 字穩定在約 5,100。
MAX_HISTORY_TURNS = _int_env("GEMINI_HISTORY_TURNS", 6)
MAX_HISTORY_CHARS = _int_env("GEMINI_HISTORY_CHARS", 15000)
# 歷史中每個工具結果最多保留的字數：模型用完的大型輸出 (讀檔、指令結果) 縮成摘要，
# 避免之後每則訊息重送、也避免單一輪超過上限時整段歷史被清空
MAX_HISTORY_TOOL_RESULT_CHARS = _int_env("GEMINI_HISTORY_TOOL_RESULT_CHARS", 600)

# 確認回呼：接收 (標題, 詳細內容 Markdown)，回傳使用者是否同意。
# 由 bot.py 提供 (Discord 按鈕)；若為 None，所有需同意的操作一律拒絕 (fail-closed)。
ConfirmCallback = Callable[[str, str], Awaitable[bool]]

# 通知回呼：送一則「不需回應」的訊息給使用者 (Discord)，例如偵測到漏網的套件變動。
NotifyCallback = Callable[[str], Awaitable[None]]


# 系統提示詞 (prompts/pm_system.md)：每次呼叫 Gemini (含工具迴圈的每一輪) 都會送出，請保持精簡以節省 Token。
# 給開發者看的規範 (技能庫同步、程式架構等) 放 GEMINI.md，不要放這裡。改提示詞後需 /reboot 才生效。
PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "pm_system.md")


def _load_system_instruction(path: str = PROMPT_PATH) -> str:
    """讀取 PM 系統提示詞；讀不到或是空的就丟例外，讓 ArQ 啟動失敗 (沒有提示詞不能默默運作)"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("\r\n", "\n")  # Windows 換行統一，避免快取指紋因換行改變
    if not text.strip():
        raise RuntimeError(f"PM 系統提示詞是空的：{path}")
    return text


SYSTEM_INSTRUCTION = _load_system_instruction()


# -------------------------------------------------------------
# 小工具函式
# -------------------------------------------------------------

def _is_user_turn_start(content: Any) -> bool:
    """使用者輸入的文字訊息 (而非回傳給模型的 function_response) 才算一輪的開頭"""
    if getattr(content, "role", None) != "user":
        return False
    parts = getattr(content, "parts", None) or []
    return any(getattr(p, "text", None) for p in parts) and not any(getattr(p, "function_response", None) for p in parts)


def _content_chars(content: Any) -> int:
    try:
        return len(json.dumps(content.model_dump(mode="json", exclude_none=True), ensure_ascii=False))
    except Exception:
        return len(str(content))


def _compact_tool_results(content: Any, limit: int) -> Any:
    """
    把歷史中過長的工具結果 (function_response) 縮成摘要；沒有需要縮減時回傳原物件。
    只處理使用者端的工具結果，模型的 function_call 與 thought_signature 一律不動。
    """
    parts = getattr(content, "parts", None) or []
    if not any(getattr(p, "function_response", None) for p in parts):
        return content
    new_parts, changed = [], False
    for p in parts:
        fr = getattr(p, "function_response", None)
        if fr is not None:
            text = json.dumps(fr.response, ensure_ascii=False, default=str) if fr.response is not None else ""
            if len(text) > limit:
                p = types.Part(function_response=types.FunctionResponse(
                    id=fr.id, name=fr.name,
                    response={"summary": text[:limit] + f"…(歷史中已縮減，原長 {len(text)} 字)"}))
                changed = True
        new_parts.append(p)
    return types.Content(role=content.role, parts=new_parts) if changed else content


def trim_history(history: List[Any], max_turns: int, max_chars: int,
                 tool_result_chars: Optional[int] = None) -> List[Any]:
    """
    裁切對話歷史：先把過長的工具結果縮成摘要，再保留最近的完整輪次，使輪數 ≤ max_turns、總字元數 ≤ max_chars。
    以「使用者文字訊息」為切點；最舊一輪之前若有不完整的片段也會一併丟棄。
    最近一輪一定保留 (即使本身超過字數上限)，不會整段清空。沒有任何變動時回傳原 list 物件。
    """
    limit = MAX_HISTORY_TOOL_RESULT_CHARS if tool_result_chars is None else tool_result_chars
    compacted = [_compact_tool_results(c, limit) for c in history]
    changed = any(a is not b for a, b in zip(compacted, history))
    starts = [i for i, c in enumerate(compacted) if _is_user_turn_start(c)]
    if not starts:
        return [] if history else history
    sizes = [_content_chars(c) for c in compacted]
    keep = 0                                   # 保留 starts[keep:] 開始的輪次；最多丟到只剩最近一輪
    while keep < len(starts) - 1 and (
        len(starts) - keep > max_turns or sum(sizes[starts[keep]:]) > max_chars
    ):
        keep += 1
    if keep == 0 and starts[0] == 0 and not changed:
        return history
    return compacted[starts[keep]:]

def _clip(text: str, limit: int) -> str:
    """截斷過長字串並標註"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(已截斷，原長度 {len(text)} 字元)"


def _clip_for_model(result: Dict[str, Any]) -> Dict[str, Any]:
    """截斷工具結果中過長的字串欄位，再回傳給模型"""
    return {
        k: _clip(v, MAX_TOOL_RESULT_CHARS) if isinstance(v, str) else v
        for k, v in result.items()
    }


def describe_tool_call(tool_name: str, args: Dict[str, Any], reason: Optional[str] = None) -> str:
    """把工具呼叫轉成適合顯示在 Discord 確認訊息中的 Markdown (開頭標示需要同意的理由)"""
    header = f"⚠️ **{reason}**\n" if reason else ""
    if tool_name == "run_powershell_sync":
        return header + f"```powershell\n{_clip(str(args.get('command', '')), 3000)}\n```"
    if tool_name == "execute_python_code":
        script = args.get("task_script_name")
        saved = f"將儲存為 `task/{os.path.basename(script)}`\n" if script else ""
        return header + saved + f"```python\n{_clip(str(args.get('code', '')), 3000)}\n```"
    if tool_name == "call_api":
        line = f"`{str(args.get('method') or 'GET').upper()} {args.get('url', '')}`"
        if args.get("credential"):
            line += f"｜金鑰：`{args.get('credential')}`"
        body = str(args.get("json_body") or "").strip()
        return header + line + (f"\n```json\n{_clip(body, 3000)}\n```" if body else "")
    if tool_name in ("dispatch_claude_engineer_sync", "dispatch_antigravity_engineer_sync"):
        meta = f"範圍：`{args.get('scope') or 'task'}`"
        if tool_name == "dispatch_antigravity_engineer_sync":
            meta += f"｜模型：`{args.get('model') or 'gemini-3.8-flash-high'}`"
        if args.get("session_id"):
            meta += "｜接續原對話"
        return header + meta + "\n" + _clip(str(args.get("task_description", "")), 3500)
    if tool_name.startswith(mcp_manager.TOOL_PREFIX):
        # 外部 MCP 工具：參數先遮蔽再顯示；工具與 server 以原始名稱標示
        found = mcp_manager.lookup(tool_name)
        parts = tool_name.split("__", 2)
        server, tool = (found.server, found.tool) if found else (parts[1] if len(parts) > 1 else "?", parts[-1])
        body = _clip(json.dumps(redact_obj(args), ensure_ascii=False, indent=2), 3000)
        return header + f"外部 MCP server `{server}`｜工具 `{tool}`\n```json\n{body}\n```"
    return header + f"```json\n{_clip(json.dumps(args, ensure_ascii=False, indent=2), 3000)}\n```"


def _merge_card_detail(args: Dict[str, Any]) -> str:
    """merge 確認卡的 PR 摘要：編號、標題、連結、對應 ticket、review 數量；0 個 review 加註警告"""
    text = str(args.get("command") or args.get("code") or args.get("url") or "")
    number = extract_pr_number(text)
    if number is None:
        return "🔀 **合併分支**（不是透過 PR，無法取得 PR 資訊；請確認這是你要的操作）"
    pr = ticket_tools.pr_merge_summary(number)
    if not pr.get("success"):
        error = pr.get("error", "")
        # fine-grained token 只開 Issues 時，issues 端點讀 PR 也會 403 (事件)
        hint = "\n💡 `TICKETS_GITHUB_TOKEN` 需要 Pull requests 唯讀權限" if "403" in error else ""
        return f"🔀 **合併 PR #{number}**\n⚠️ 無法取得 PR 資訊（{error[:150]}），請先到 GitHub 確認再同意。{hint}"
    tickets = "、".join(f"#{n}" for n in pr.get("tickets") or []) or "（PR 內文沒有 Closes #N）"
    reviews = pr.get("reviews")
    review_text = "無法取得（token 需要 Pull requests 讀取權限）" if reviews is None else f"{reviews} 個（approve {pr.get('approved', 0)}）"
    lines = [f"🔀 **合併 PR #{pr['number']}：{pr.get('title', '')}**", pr.get("url", ""),
             f"對應 ticket：{tickets}｜review：{review_text}"]
    if reviews == 0:
        lines.append("🛑 **這個 PR 沒有任何 review**，確定你已經看過再同意。")
    elif reviews is None:
        lines.append("⚠️ 看不到 review 狀態，請先到 GitHub 確認你已經 review 過再同意。")
    if pr.get("merged") or pr.get("state") == "closed":
        lines.append(f"⚠️ PR 目前狀態：{'已 merge' if pr.get('merged') else '已關閉'}")
    return "\n".join(lines)


async def _send_message_with_retry(send_fn: Callable[..., Any], *args: Any) -> Any:
    """
    呼叫 chat_session.send_message，遇到暫時性錯誤 (429 額度限制 / 5xx 伺服器錯誤) 時
    先以指數退避重試幾次，而不是直接讓例外往上丟導致對話記憶被重置。
    其他非暫時性錯誤 (例如 400 參數錯誤) 直接拋出，交給呼叫端處理。
    """
    delay = _RETRY_BASE_DELAY_SECONDS
    for attempt in range(_RETRY_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(send_fn, *args)
        except Exception as e:
            status_code = getattr(e, "code", None)
            if status_code in _TRANSIENT_STATUS_CODES and attempt < _RETRY_ATTEMPTS:
                logger.warning(f"⚠️ Gemini API 暫時性錯誤 (code={status_code})，{delay} 秒後重試 "
                               f"({attempt + 1}/{_RETRY_ATTEMPTS})...", exc_info=True)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise


_SEPARATOR_LINE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def compact_reply(text: str) -> str:
    """
    回覆排版由程式保證：SYSTEM_INSTRUCTION 禁止空行與分隔線，但 Gemini 不一定照做。
    移除空行與只有 --- / *** / ___ 的分隔線；``` 程式碼區塊內原樣保留 (程式碼可能需要空行)。
    """
    lines, in_code = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            lines.append(line)
            continue
        if in_code:
            lines.append(line)
        elif line.strip() and not _SEPARATOR_LINE_RE.match(line):
            lines.append(line)
    return "\n".join(lines).strip()


def _extract_text(response: Any) -> str:
    """安全取出回應中的純文字 (略過 function_call / thought 等非文字 part)"""
    try:
        candidates = response.candidates or []
        if candidates and candidates[0].content and candidates[0].content.parts:
            texts = [
                p.text for p in candidates[0].content.parts
                if getattr(p, "text", None) and not getattr(p, "thought", False)
            ]
            return "\n".join(texts).strip()
    except Exception:
        pass
    return ""


# -------------------------------------------------------------
# 代理主體
# -------------------------------------------------------------

class ArQAgent:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
        self.client = None
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
        self.chats: Dict[int, Any] = {}             # user_id -> Gemini chat session (多輪 Context)
        # thinking 等級 (啟動時讀一次)：一般訊息用 low 省錢；需要規劃 / 派工的訊息改用 deep (見 _run_tool_loop)
        self.thinking_level = thinking_level()
        self.deep_thinking_level = deep_thinking_level()
        self._locks: Dict[int, asyncio.Lock] = {}   # user_id -> 處理中鎖，避免同一對話被並行寫入
        # 背景派工完成的精簡結果：在下一輪對話開頭轉達給 PM；背景執行緒寫入，需加鎖
        self._pending: List[Dict[str, Any]] = chat_store.load_pending()  # 重啟期間完成的派工結果
        self._pending_lock = threading.Lock()
        # 固定前綴 (系統提示詞 + 工具) 的顯式快取；None = 關閉或沒有 Client
        self.prefix_cache: Optional[PrefixCache] = None

        if HAS_GENAI and self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                logger.warning(f"⚠️ 初始化 Gemini Client 失敗: {e}", exc_info=True)
        if self.client is not None and explicit_cache_enabled():
            self.prefix_cache = PrefixCache(self.client, self.model_name, lambda: SYSTEM_INSTRUCTION,
                                            lambda: [spec.func for spec in _TOOL_SPECS],
                                            extra_declarations=mcp_manager.declarations)  # MCP 工具
            atexit.register(self.close)  # 正常結束時刪除快取；/reboot 走 os._exit，由 bot.py 先呼叫 close()

    @property
    def is_ai_ready(self) -> bool:
        return self.client is not None

    def get_or_create_chat(self, user_id: int = 0, history: Optional[List[Any]] = None):
        """獲取或建立特定使用者的多輪對話會話 (Context)；history 用於以裁切後的歷史重建"""
        if not self.client:
            return None
        if user_id not in self.chats and history is None:  # 重啟後第一次：讀回存檔的歷史，再裁切一次
            history = trim_history(chat_store.load_history(user_id), MAX_HISTORY_TURNS, MAX_HISTORY_CHARS) or None
        if user_id not in self.chats or history is not None:
            # 先記錄 MCP 工具清單版本再建立：清單之後改變時 chat 會以相同歷史重建
            self.__dict__.setdefault("_chat_versions", {})[user_id] = mcp_manager.current_version()
            self.chats[user_id] = self.client.chats.create(
                model=self.model_name,
                history=history or [],
                config=self._chat_config(self.thinking_level),
            )
        return self.chats[user_id]

    def _refresh_for_mcp(self, user_id: int, chat_session: Any) -> Any:
        """MCP 工具清單自對話建立後有變動 (連線完成、/mcp reload)：以相同歷史重建，新宣告才會生效"""
        versions = self.__dict__.setdefault("_chat_versions", {})
        if versions.get(user_id, mcp_manager.current_version()) == mcp_manager.current_version():
            return chat_session
        try:
            history = chat_session.get_history(curated=True)
        except Exception:
            history = []
        return self.get_or_create_chat(user_id, history=history)

    def close(self) -> None:
        """ArQ 結束前呼叫：刪除顯式快取 (失敗也沒關係，TTL 到期會自動刪除)"""
        cache = getattr(self, "prefix_cache", None)
        if cache is not None:
            cache.close()
        mcp_manager.shutdown_default()  # 關閉所有 MCP server 子程序 (/reboot 走 os._exit，atexit 不會跑)

    def _chat_config(self, level: Optional[str], allow_tools: bool = True, cache_name: Optional[str] = None,
                     include_mcp: bool = True) -> Any:
        """
        對話用的完整設定；送出時傳入的 config 會整個取代 chat 的預設設定，所以每次都要完整產生。
        allow_tools=False：仍宣告工具 (歷史中有 function_call)，但禁止模型再呼叫，用於停下時的文字總結。
        cache_name：顯式快取。系統提示詞、工具與 tool_config 都在快取裡，請求不能再帶 (API 會回 400)。
        include_mcp=False：只宣告原生工具 (Gemini 拒絕 MCP 宣告時重送用)。
        """
        if cache_name:
            return types.GenerateContentConfig(
                cached_content=cache_name,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                temperature=0.7,
                thinking_config=make_thinking_config(level),
            )
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            # SDK 會依函式簽名與 docstring 自動產生 FunctionDeclaration
            tools=self._tool_declarations(include_mcp),
            # 關閉自動執行，改由 _run_tool_loop 手動執行 (理由見檔案開頭說明)
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tool_config=None if allow_tools else types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="NONE")),
            temperature=0.7,
            thinking_config=make_thinking_config(level),
        )

    def _tool_declarations(self, include_mcp: bool = True) -> List[Any]:
        """
        工具清單。沒有 MCP 工具時與改動前完全相同 (callable 清單)；有時把原生與 MCP 宣告併成同一個 Tool。
        """
        funcs = [spec.func for spec in _TOOL_SPECS]
        extra = mcp_manager.declarations() if include_mcp else []
        if not extra:
            return funcs
        declarations = [types.FunctionDeclaration.from_callable(client=self.client, callable=f) for f in funcs]
        return [types.Tool(function_declarations=declarations + extra)]

    def _trim_chat(self, user_id: int, chat_session: Any) -> Any:
        """歷史有工具結果需要縮減或超過上限時，以處理後的歷史重建對話，回傳要使用的 chat session"""
        try:
            history = chat_session.get_history(curated=True)
        except Exception:
            return chat_session
        trimmed = trim_history(history, MAX_HISTORY_TURNS, MAX_HISTORY_CHARS)
        if trimmed is history:
            return chat_session
        dropped = len(history) - len(trimmed)
        logger.info(f"✂️ 整理對話歷史：工具結果縮成摘要" + (f"，丟棄最舊的 {dropped} 筆" if dropped else "")
                    + f" (保留 {len(trimmed)} 筆)")
        return self.get_or_create_chat(user_id, history=trimmed)

    def reset_chat(self, user_id: int = 0):
        """清除特定使用者的對話記憶"""
        self.chats.pop(user_id, None)
        chat_store.clear_history(user_id)

    async def _persist_history(self, user_id: int) -> None:
        """訊息處理完後把 (裁切後的) 歷史存檔；失敗只印警告，不影響回覆"""
        try:
            history = trim_history(self.chats[user_id].get_history(curated=True), MAX_HISTORY_TURNS, MAX_HISTORY_CHARS)
            await asyncio.to_thread(chat_store.save_history, user_id, history)
        except Exception as e:
            logger.warning(f"⚠️ 對話記憶存檔失敗（略過）: {redact(str(e))[:200]}", exc_info=True)

    # -------------------------------------------------------------
    # 背景派工結果轉達
    # -------------------------------------------------------------

    def _add_pending_report(self, view: Dict[str, Any]) -> None:
        with self._pending_lock:
            self._pending.append(view)
            chat_store.save_pending(self._pending)

    def pending_reports(self) -> List[Dict[str, Any]]:
        with self._pending_lock:
            return list(self._pending)

    def _with_pending_reports(self, user_prompt: str) -> str:
        """把背景派工的完成結果放在使用者訊息前面 (只轉達一次)；完整回報使用者已經看過"""
        with self._pending_lock:
            pending, self._pending = self._pending, []
            if not pending:
                return user_prompt
            chat_store.save_pending([])
        body = "\n".join(json.dumps(redact_obj(v), ensure_ascii=False) for v in pending)
        return ("【系統：背景派工已完成（完整回報已直接顯示給使用者，不要重複轉述；需要時接著處理驗收、工程師提問或下一步）】\n"
                f"{body}\n【使用者訊息】\n{user_prompt}")

    async def chat(
        self,
        user_prompt: str,
        user_id: int = 0,
        confirm: Optional[ConfirmCallback] = None,
        notify: Optional[NotifyCallback] = None,
    ) -> Dict[str, Any]:
        """
        處理使用者提問，回傳 {"text": 回覆文字, "files": [要附加的檔案路徑]}。

        - 有 Gemini Client：多輪對話 + 手動工具迴圈。
        - 無 API Key 或 API 失敗：退回 _local_fallback 的關鍵字規則。
        - confirm：安裝套件 / 高風險操作前詢問使用者 (見 ConfirmCallback)。
        - notify：不需回應的通知 (見 NotifyCallback)。
        """
        if user_prompt.strip().lower() in ("重置對話", "清除記憶", "忘記對話", "記憶重置", "重置記憶", "清除對話", "/reset", "reset", "clear"):
            self.reset_chat(user_id)
            return {
                "text": "🧹 **對話歷史已重置！**\n我已經清空了先前的對話記憶，現在可以開始全新話題囉！",
                "files": []
            }

        # 同一使用者同時只處理一則訊息：Gemini chat session 不是並行安全的，
        # 且 Claude 任務可能跑好幾分鐘，期間的新訊息直接請使用者稍候，而不是默默排隊。
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            return {
                "text": "⏳ 上一個請求仍在處理中（可能正在等待您同意，或 Claude Code 正在開發），請稍候再傳送新訊息。",
                "files": []
            }

        async with lock:
            chat_session = self.get_or_create_chat(user_id)
            if chat_session:
                chat_session = self._refresh_for_mcp(user_id, chat_session)
                chat_session = self._trim_chat(user_id, chat_session)
                try:
                    result = await self._run_tool_loop(chat_session, self._with_pending_reports(user_prompt),
                                                       user_id, confirm, notify)
                    await self._persist_history(user_id)
                    return result
                except Exception as e:
                    # 例外可能發生在模型送出 function_call 之後、我們回傳結果之前，
                    # 此時對話歷史缺少對應的 function_response，之後每次呼叫都會失敗，
                    # 因此必須重置該使用者的對話。
                    self.reset_chat(user_id)
                    fallback = await self._local_fallback(user_prompt)
                    if fallback:
                        return fallback
                    return {
                        "text": f"⚠️ Gemini API 處理時發生異常 (可能額度用盡或連線問題，對話記憶已重置): {e}\n\n💡 您仍可直接使用 `/screenshot`、`/status` 或輸入「截圖」、「狀態」等快捷指令！",
                        "files": []
                    }

        # 降級規則模式 (未設定 GEMINI_API_KEY 時)
        fallback = await self._local_fallback(user_prompt)
        if fallback:
            return fallback

        return {
            "text": (
                "👋 我是 **ArQ** 本地助理！\n\n"
                "💡 目前處於本機快捷模式。\n"
                "您可以直接嘗試以下指令：\n"
                "• 輸入「**截圖**」：立即回傳電腦螢幕照片至手機\n"
                "• 輸入「**狀態**」：回傳 CPU、RAM 與磁碟容量\n"
                "• 輸入 `run <指令>`：執行 PowerShell 指令（例：`run ipconfig`）\n"
                "• 或使用斜線指令 `/help`、`/screenshot`、`/status`！"
            ),
            "files": []
        }

    async def _run_tool_loop(
        self,
        chat_session: Any,
        user_prompt: str,
        user_id: int,
        confirm: Optional[ConfirmCallback],
        notify: Optional[NotifyCallback],
    ) -> Dict[str, Any]:
        """
        送出訊息並反覆執行模型要求的工具，直到模型給出最終文字回覆。
        thinking：訊息需要規劃 (wants_deep_thinking) 時整則用 deep；否則用一般等級，
        但模型一旦派工給 RD，之後的每一輪 (撰寫回報) 改用 deep。
        """
        files: List[str] = []
        usage = empty_usage()
        state = {"level": self.deep_thinking_level if wants_deep_thinking(user_prompt) else self.thinking_level,
                 "tools": []}
        try:
            return await self._tool_loop_body(chat_session, user_prompt, user_id, confirm, notify, files, usage, state)
        finally:
            # tools：每輪呼叫的工具名稱，用來追查跑了很多輪的訊息
            record_usage("chat", usage, model=self.model_name, thinking=state["level"] or "default",
                         tools=state["tools"])

    async def _send(self, chat_session: Any, payload: Any, usage: Dict[str, int], level: Optional[str]) -> Any:
        """送出一次並累加用量；level 與一般等級不同時，本次以覆寫設定送出 (不影響對話記憶)"""
        response = await self._send_with_cache(chat_session, payload, level)
        add_usage(usage, usage_of(response))
        usage["calls"] = usage.get("calls", 0) + 1
        return response

    async def _send_with_cache(self, chat_session: Any, payload: Any, level: Optional[str],
                               allow_tools: bool = True) -> Any:
        """
        優先以顯式快取送出；快取無法使用或送出失敗 (過期、被刪除等) 時，改用不帶快取的完整設定再送一次。
        送出失敗時 SDK 不會寫入對話歷史，所以重送是安全的。
        """
        cache = getattr(self, "prefix_cache", None)
        cache_name = await asyncio.to_thread(cache.name, allow_tools) if cache is not None else None
        if cache_name:
            try:
                return await _send_message_with_retry(
                    chat_session.send_message, payload, self._chat_config(level, allow_tools, cache_name))
            except Exception as e:
                logger.warning(f"⚠️ 以顯式快取送出失敗，改用一般送法: {e}", exc_info=True)
                await asyncio.to_thread(cache.invalidate, allow_tools)
        config = None if allow_tools and level == self.thinking_level else self._chat_config(level, allow_tools)
        try:
            return await _send_message_with_retry(chat_session.send_message, payload, config)
        except Exception as e:
            return await self._resend_without_mcp(chat_session, payload, level, allow_tools, e)

    async def _resend_without_mcp(self, chat_session: Any, payload: Any, level: Optional[str],
                                  allow_tools: bool, error: Exception) -> Any:
        """
        Gemini 回 400 且有宣告 MCP 工具時：外部 server 的 JSON Schema 可能不被接受，整個請求都會失敗，
        連原生工具也不能用。改以只含原生工具的設定重送；成功就停用 MCP 宣告 (/mcp reload 恢復)，
        仍失敗代表與 MCP 無關，拋出原本的錯誤。對話 chat 的預設設定仍含 MCP，
        所以停用後同一輪的後續送出也走這裡 (suspended)，下一則訊息起對話會依版本號重建。
        """
        if getattr(error, "code", None) != 400 or not (mcp_manager.declarations() or mcp_manager.suspended()):
            raise error
        try:
            response = await _send_message_with_retry(
                chat_session.send_message, payload, self._chat_config(level, allow_tools, include_mcp=False))
        except Exception:
            raise error
        if mcp_manager.suspend_declarations(redact(str(error))[:300]):
            logger.warning(f"⚠️ Gemini 拒絕 MCP 工具宣告，已停用 MCP 工具（原生工具照常）: {redact(str(error))[:200]}")
        return response

    async def _tool_loop_body(
        self,
        chat_session: Any,
        user_prompt: str,
        user_id: int,
        confirm: Optional[ConfirmCallback],
        notify: Optional[NotifyCallback],
        files: List[str],
        usage: Dict[str, int],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        response = await self._send(chat_session, user_prompt, usage, state["level"])
        budget = MESSAGE_TOKEN_BUDGET
        stop_reason = ""

        for _ in range(MAX_TOOL_ROUNDS):
            calls = response.function_calls
            if not calls:
                break
            # Token 預算：已超過且模型還要呼叫工具 → 請使用者決定 (同意就再給一份預算)
            if usage.get("total", 0) > budget:
                if not await self._ask_continue(confirm, usage, budget, calls):
                    stop_reason = f"本則訊息已用 {usage.get('total', 0):,} Token，超過預算 {budget:,}"
                    break
                budget += MESSAGE_TOKEN_BUDGET

            # 模型可能一次要求多個工具 (平行呼叫)，全部執行後一起回傳
            state["tools"].append([call.name for call in calls])
            response_parts = []
            for call in calls:
                result, new_files = await self._execute_tool(call.name, dict(call.args or {}), confirm, notify)
                files.extend(new_files)
                response_parts.append(types.Part(
                    function_response=types.FunctionResponse(id=call.id, name=call.name, response=result)
                ))
            if any(call.name in _DISPATCH_TOOLS for call in calls):
                state["level"] = self.deep_thinking_level
            response = await self._send(chat_session, response_parts, usage, state["level"])

        if response.function_calls:
            stop_reason = stop_reason or f"工具呼叫已達 {MAX_TOOL_ROUNDS} 輪上限"
            return await self._wrap_up(chat_session, user_id, response.function_calls, stop_reason,
                                       usage, state, files)

        text = compact_reply(_extract_text(response))  # 排版由程式保證
        if not text and files:
            text = "📸 已為您擷取當前電腦螢幕畫面！"
        return {"text": text or "指令已完成。", "files": files}

    async def _ask_continue(self, confirm: Optional[ConfirmCallback], usage: Dict[str, int], budget: int,
                            calls: Any) -> bool:
        """單則訊息超過 Token 預算時請使用者決定是否繼續 (沒有確認回呼時一律停止)"""
        if confirm is None:
            return False
        total = usage.get("total", 0)
        names = "、".join(sorted({c.name for c in calls}))
        return await confirm(
            f"繼續執行工具（已用 {total} Token，預算 {budget}）",
            f"⚠️ 本則訊息已呼叫 Gemini {usage.get('calls', 0)} 次，累計 **{total}** Token，超過預算 **{budget}**。\n"
            f"模型接下來要呼叫：`{names}`\n同意則再給一份預算繼續；拒絕則停止並請模型總結目前進度。",
        )

    async def _wrap_up(self, chat_session: Any, user_id: int, calls: Any, reason: str,
                       usage: Dict[str, int], state: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
        """
        停止工具迴圈 (輪數上限或 Token 預算)：回應尚未執行的 function_call，並禁止再呼叫工具，
        請模型用文字總結進度，對話記憶得以保留。總結失敗才重置對話 (歷史中不能留下未回應的 function_call)。
        """
        parts = [types.Part(function_response=types.FunctionResponse(
            id=call.id, name=call.name,
            response={"error": f"{reason}，系統已停止執行工具。請不要再呼叫工具，直接用文字向使用者總結目前進度與尚未完成的部分。"},
        )) for call in calls]
        try:
            response = await self._send_with_cache(chat_session, parts, state["level"], allow_tools=False)
            add_usage(usage, usage_of(response))
            usage["calls"] = usage.get("calls", 0) + 1
            text = "" if response.function_calls else compact_reply(_extract_text(response))
        except Exception as e:
            logger.warning(f"⚠️ 停止時請模型總結失敗: {e}", exc_info=True)
            text = ""
        if not text:
            self.reset_chat(user_id)
            return {"text": f"⚠️ {reason}，已中止並重置對話記憶。請把需求拆小一點再試一次。", "files": files}
        return {"text": f"⚠️ {reason}，已停止執行工具。\n{text}", "files": files}

    async def _execute_tool(
        self,
        tool_name: str,
        args: Dict[str, Any],
        confirm: Optional[ConfirmCallback],
        notify: Optional[NotifyCallback],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        執行單一工具，回傳 (給模型的結果 dict, 要附加到 Discord 的檔案清單)。
        任何錯誤都轉成 {"error": ...} 回傳給模型，讓模型自行向使用者說明。
        """
        spec = TOOL_REGISTRY.get(tool_name) or self._mcp_spec(tool_name)
        if spec is None:
            return {"error": f"未知的工具: {tool_name}"}, []

        # 0. 硬性拒絕：例如 merge 夾帶其他指令。不跳卡、不執行任何部分
        refusal = spec.guard(args) if spec.guard else None
        if refusal:
            logger.info(f"🚫 拒絕工具呼叫: {tool_name} ({refusal[:60]})")
            return {"error": refusal}, []

        # 1. 風險判斷：無風險直接執行；安裝套件 / 高風險需使用者同意
        reason = spec.risk(args) if spec.risk else None
        if reason:
            detail = describe_tool_call(tool_name, args, reason)
            if MERGE_REASON in reason:
                detail = await asyncio.to_thread(_merge_card_detail, args) + "\n" + detail
            approved = confirm is not None and await confirm(f"{spec.label}（{reason}）", detail)
            if not approved:
                logger.info(f"🚫 使用者未同意工具呼叫: {tool_name} ({reason})")
                return {"error": f"使用者拒絕或未在時限內同意此操作（{reason}）。請勿重試或繞過，改為詢問使用者想如何調整。"}, []

        logger.info(f"🔧 執行工具: {tool_name}")

        # 2. 執行 (背景執行緒)。以下兩個函式讓執行緒內的長任務能回到事件迴圈通知 / 詢問使用者。
        bridge = ThreadBridge(asyncio.get_running_loop())
        thread_notify = bridge.notifier((lambda text: self._notify(notify, text)) if notify is not None else None)
        thread_approve = bridge.approver(confirm)

        def on_done(final: Dict[str, Any]) -> None:
            """背景任務完成 (在背景執行緒中)：完整回報送使用者、精簡結果排進待轉達佇列"""
            # 先排進佇列：通知送不出去 (事件迴圈已關閉、Discord 斷線) 時，PM 仍要知道結果 (整合測試發現)
            self._add_pending_report(spec.model_view(final) if spec.model_view is not None else final)
            report = spec.user_report(redact_obj(final)) if spec.user_report is not None else ""
            if report:
                try:
                    thread_notify(report)
                except Exception as e:
                    logger.warning(f"⚠️ 背景派工完成通知送出失敗（結果已排進 PM 佇列）: {e}", exc_info=True)

        def run() -> Any:
            if spec.impl is not None and spec.background:
                # 背景任務：impl 立即回傳「已受理」，完成時在背景執行緒呼叫 on_done
                return spec.impl(**args, notify=thread_notify, approve=thread_approve, on_done=on_done)
            if spec.impl is not None:
                # impl 自行負責途中的同意關卡與套件變動通知 (例如 run_claude_task)
                return spec.impl(**args, notify=thread_notify, approve=thread_approve)
            if spec.watch_packages:
                # 保險機制：執行後比對 ArQ 環境套件，抓出未被指令比對攔截的安裝
                with report_new_packages(thread_notify, spec.label):
                    return spec.func(**args)
            return spec.func(**args)

        try:
            result = await asyncio.to_thread(run)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}, []

        files: List[str] = []
        if tool_name == "take_screenshot":
            files.append(result)
            result = {"result": result, "note": "截圖已自動附加於回覆訊息中"}
        elif not isinstance(result, dict):
            result = {"result": result}

        if reason and MERGE_REASON in reason:
            # PR merge 後清除對應 ticket 的 worktree；未 merge 成功時 cleanup_after_merge 不會動
            pr_number = extract_pr_number(str(args.get("command") or args.get("code") or args.get("url") or ""))
            if pr_number is not None:
                for message in await asyncio.to_thread(worktree.cleanup_after_merge, pr_number):
                    await self._notify(notify, message)
        if spec.user_report is not None:
            report = spec.user_report(redact_obj(result))
            if report:  # 背景任務的「已受理」沒有完整回報
                await self._notify(notify, report)
        if spec.model_view is not None:
            result = spec.model_view(result)

        # 最後一道防線：工具輸出若意外帶到 .env 的金鑰 / Token，送進 Gemini 前先遮蔽
        return redact_obj(_clip_for_model(result)), files

    @staticmethod
    def _mcp_spec(tool_name: str) -> Optional[ToolSpec]:
        """外部 MCP 工具 的 ToolSpec：每次呼叫預設要同意 (auto_approve 見 mcp_manager.risk)，結果標示為外部資料"""
        entry = mcp_manager.lookup(tool_name)
        if entry is None:
            return None
        manager = mcp_manager.get_manager()

        def call(**tool_args: Any) -> Dict[str, Any]:
            return manager.call(tool_name, tool_args)

        call.__name__ = tool_name
        return ToolSpec(call, f"MCP {entry.server}", risk=lambda a: manager.risk(tool_name, a))

    @staticmethod
    async def _notify(notify: Optional[NotifyCallback], text: str) -> None:
        """送出通知；失敗只記錄，不中斷工具執行"""
        if notify is None:
            return
        try:
            await notify(text)
        except Exception as e:
            logger.warning(f"⚠️ 發送通知失敗: {e}", exc_info=True)

    async def _local_fallback(self, user_prompt: str) -> Optional[Dict[str, Any]]:
        """本機快捷規則匹配 (無需 AI 即可運作)。這些是使用者明確下達的指令，因此不需再確認。"""
        lower = user_prompt.lower().strip()

        if any(kw in lower for kw in ["截圖", "畫面", "screenshot", "螢幕", "看螢幕"]):
            img_path = await asyncio.to_thread(take_screenshot)
            return {
                "text": "📸 已為您擷取當前電腦主螢幕畫面！",
                "files": [img_path]
            }

        if any(kw in lower for kw in ["狀態", "硬體", "cpu", "ram", "記憶體", "status"]):
            s = await asyncio.to_thread(get_system_status)
            text = (
                f"🖥️ **主機系統狀態回報**：\n"
                f"• **作業系統**: {s['os']}\n"
                f"• **CPU 使用率**: {s['cpu_usage']}\n"
                f"• **記憶體用量**: {s['memory_used']}\n"
                f"• **C 槽可用空間**: {s['disk_c']}\n"
                f"• **開機時間**: {s['boot_time']} (已開機 {s['uptime']})"
            )
            return {"text": text, "files": []}

        if lower.startswith("run ") or lower.startswith("cmd ") or lower.startswith("ps "):
            cmd = user_prompt.strip().split(" ", 1)[1]
            res = await run_powershell(cmd)
            if res["success"]:
                out = res["stdout"] or "(執行成功，無文字輸出)"
                return {"text": f"💻 **執行結果**: `{cmd}`\n```text\n{out}\n```", "files": []}
            else:
                err = res["stderr"] or res.get("error", "執行失敗")
                return {"text": f"❌ **執行失敗**: `{cmd}`\n```text\n{err}\n```", "files": []}

        return None
