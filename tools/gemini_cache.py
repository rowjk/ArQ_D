"""
Gemini 顯式快取：把每次呼叫都固定送出的前綴 (系統提示詞 + 工具說明) 放進 CachedContent

為什麼要顯式快取：
    ArQ 的對話是追加式的，隱式快取永遠不會命中 (實測)。改用 caches.create 把前綴存起來，
    之後每次呼叫只帶快取名稱，這約 3,800 Token 以快取價格計費 (便宜 90%)。

運作方式：
    - 第一次需要時才建立；以「模型 + 系統提示詞 + 工具宣告 + 是否允許呼叫工具」的雜湊為鍵，內容改變就刪掉舊的重建。
    - 用 cached_content 時，請求不能再帶 system_instruction / tools / tool_config (API 回 400)，
      所以禁止呼叫工具的總結 另建一份 tool_config=NONE 的快取，需要時才建立。
    - 只在使用中保留：TTL 短 (預設 15 分鐘)，使用時延長 (同一分鐘內只延長一次，避免每輪工具迴圈都多一次 API 呼叫)；
      閒置超過 TTL 由伺服器自動刪除，下次使用時重建；ArQ 結束時 close() 全部刪除。
    - 任何失敗都回傳 None，呼叫端改用不帶快取的完整設定送出；建立失敗後退避一段時間才重試。

設定 (.env)：
    GEMINI_EXPLICIT_CACHE              預設 true；false / 0 / no / off 關閉
    GEMINI_EXPLICIT_CACHE_TTL_MINUTES  快取存活分鐘數，預設 15
"""

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from tools.config import env_int

logger = logging.getLogger(__name__)

try:
    from google.genai import types
except ImportError:
    types = None

DEFAULT_TTL_MINUTES = 15
# 延長 TTL 的最短間隔：同一則訊息的工具迴圈會連續呼叫好幾次，不必每次都延長
EXTEND_INTERVAL_SECONDS = 60
# 快取快到期時不再使用 (避免送出途中過期)，直接重建
EXPIRY_MARGIN_SECONDS = 30
# 建立失敗後多久才重試 (期間直接不用快取)
CREATE_RETRY_SECONDS = 300


def explicit_cache_enabled() -> bool:
    return os.getenv("GEMINI_EXPLICIT_CACHE", "true").strip().lower() not in ("false", "0", "no", "off")


def cache_ttl_seconds() -> int:
    return env_int("GEMINI_EXPLICIT_CACHE_TTL_MINUTES", DEFAULT_TTL_MINUTES, minimum=1) * 60


class PrefixCache:
    """管理固定前綴的顯式快取；name() 會呼叫 Gemini API (阻塞)，請在執行緒中呼叫"""

    def __init__(self, client: Any, model: str, system_instruction: Callable[[], str],
                 tool_funcs: Callable[[], List[Callable[..., Any]]], ttl_seconds: Optional[int] = None,
                 clock: Callable[[], float] = time.monotonic,
                 extra_declarations: Optional[Callable[[], List[Any]]] = None):
        self.client = client
        self.model = model
        self._system_instruction = system_instruction
        self._tool_funcs = tool_funcs
        # 額外的工具宣告 (例如 MCP 工具)：沒有或為空時，快取內容與雜湊跟沒有這個參數時完全相同
        self._extra_declarations = extra_declarations
        self.ttl = ttl_seconds or cache_ttl_seconds()
        self._clock = clock
        self._lock = threading.Lock()
        # allow_tools -> {"key", "name", "expires_at", "extended_at"}
        self._entries: Dict[bool, Dict[str, Any]] = {}
        self._retry_after: Dict[bool, float] = {}

    def name(self, allow_tools: bool = True) -> Optional[str]:
        """回傳可用的快取名稱 (必要時建立、重建或延長)；無法使用快取時回傳 None，不會拋出例外"""
        with self._lock:
            try:
                return self._name(allow_tools)
            except Exception as e:
                logger.warning(f"⚠️ Gemini 顯式快取發生錯誤，本次不用快取: {e}", exc_info=True)
                return None

    def _name(self, allow_tools: bool) -> Optional[str]:
        prompt = self._system_instruction()
        tools = self._build_tools()
        key = self._key(prompt, tools, allow_tools)
        now = self._clock()
        entry = self._entries.get(allow_tools)
        if entry and entry["key"] == key and now < entry["expires_at"] - EXPIRY_MARGIN_SECONDS:
            if now - entry["extended_at"] < EXTEND_INTERVAL_SECONDS or self._extend(entry, now):
                return entry["name"]
        if entry:
            self._drop(allow_tools)
        if now < self._retry_after.get(allow_tools, 0):
            return None
        return self._create(prompt, tools, allow_tools, key, now)

    def _build_tools(self) -> List[Any]:
        declarations = [types.FunctionDeclaration.from_callable(client=self.client, callable=f)
                        for f in self._tool_funcs()]
        if self._extra_declarations is not None:
            declarations += list(self._extra_declarations())
        return [types.Tool(function_declarations=declarations)]

    def invalidate(self, allow_tools: bool = True) -> None:
        """送出時快取出錯 (例如已過期)：忘掉並嘗試刪除，下次使用時重建"""
        with self._lock:
            self._drop(allow_tools)

    def close(self) -> None:
        """刪除所有快取 (ArQ 結束時呼叫)"""
        with self._lock:
            for allow_tools in list(self._entries):
                self._drop(allow_tools)

    def _key(self, prompt: str, tools: List[Any], allow_tools: bool) -> str:
        raw = json.dumps({"model": self.model, "prompt": prompt, "allow_tools": allow_tools,
                          "tools": [t.model_dump(mode="json", exclude_none=True) for t in tools]},
                         ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _create(self, prompt: str, tools: List[Any], allow_tools: bool, key: str, now: float) -> Optional[str]:
        try:
            cache = self.client.caches.create(model=self.model, config=types.CreateCachedContentConfig(
                system_instruction=prompt,
                tools=tools,
                tool_config=None if allow_tools else types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="NONE")),
                ttl=f"{self.ttl}s",
                display_name="arq-prefix" if allow_tools else "arq-prefix-no-tools",
            ))
        except Exception as e:
            self._retry_after[allow_tools] = now + CREATE_RETRY_SECONDS
            logger.warning(f"⚠️ 建立 Gemini 顯式快取失敗，{CREATE_RETRY_SECONDS // 60} 分鐘內改用一般送法: {e}", exc_info=True)
            return None
        self._entries[allow_tools] = {"key": key, "name": cache.name, "expires_at": now + self.ttl, "extended_at": now}
        logger.info(f"🗄️ 已建立 Gemini 顯式快取 {cache.name} (TTL {self.ttl // 60} 分鐘)")
        return cache.name

    def _extend(self, entry: Dict[str, Any], now: float) -> bool:
        try:
            self.client.caches.update(name=entry["name"], config=types.UpdateCachedContentConfig(ttl=f"{self.ttl}s"))
        except Exception as e:
            logger.warning(f"⚠️ 延長 Gemini 顯式快取失敗，改為重建: {e}", exc_info=True)
            return False
        entry["expires_at"] = now + self.ttl
        entry["extended_at"] = now
        return True

    def _drop(self, allow_tools: bool) -> None:
        entry = self._entries.pop(allow_tools, None)
        if not entry:
            return
        try:
            self.client.caches.delete(name=entry["name"])
        except Exception:
            pass  # 已過期或已刪除
