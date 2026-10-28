"""
Gemini 呼叫的共用設定 (agent.py 對話與 search_tools.py 搜尋共用)

thinking 等級 (thinking Token 以輸出價格計費)：
    GEMINI_THINKING_LEVEL       一般訊息，預設 low：多數訊息是查詢與操作，不需要深度推理。
    GEMINI_DEEP_THINKING_LEVEL  需要規劃的訊息，預設 high：PM 拆解工作、寫規格時想得夠深 (見 wants_deep_thinking)。
    可用值：low / medium / high；default = 不設定，交給模型預設。
    不接受 minimal：gemini-3.8-flash 實測回 400「Thinking level MINIMAL is not supported」。其他值印警告並改用預設。
"""

import logging
import os
from typing import Optional

from tools.config import env_str

logger = logging.getLogger(__name__)

try:
    from google.genai import types
except ImportError:
    types = None

DEFAULT_THINKING_LEVEL = "low"
DEFAULT_DEEP_THINKING_LEVEL = "high"
_VALID_LEVELS = {"low", "medium", "high"}

# 使用者在訊息開頭加上這個詞，強制本則訊息用深度思考
DEEP_THINKING_PREFIX = "深思"
# 訊息含這些詞時視為需要規劃 / 拆解工作 (誤判只會讓該則訊息多花一些 Token)
PLANNING_KEYWORDS = ("拆解", "規劃", "規格", "設計", "架構", "開發")


def _level_from_env(name: str, fallback: str) -> Optional[str]:
    raw = env_str(name, fallback).lower() or fallback
    if raw == "default":
        return None
    if raw not in _VALID_LEVELS:
        logger.warning(f"⚠️ {name}={raw!r} 無效 (可用 low / medium / high / default)，改用 {fallback}")
        raw = fallback
    return raw.upper()


def thinking_level() -> Optional[str]:
    """一般訊息的 thinking 等級 (大寫，如 "LOW")；None 表示交給模型預設"""
    return _level_from_env("GEMINI_THINKING_LEVEL", DEFAULT_THINKING_LEVEL)


def deep_thinking_level() -> Optional[str]:
    """需要規劃的訊息的 thinking 等級；None 表示交給模型預設"""
    return _level_from_env("GEMINI_DEEP_THINKING_LEVEL", DEFAULT_DEEP_THINKING_LEVEL)


def make_thinking_config(level: Optional[str]):
    """依等級產生 GenerateContentConfig 的 thinking_config；None 表示交給模型預設"""
    if level is None or types is None:
        return None
    return types.ThinkingConfig(thinking_level=level)


def thinking_config():
    """一般訊息的 thinking_config"""
    return make_thinking_config(thinking_level())


def wants_deep_thinking(prompt: str) -> bool:
    """訊息開頭為「深思」，或含規劃 / 拆解工作的關鍵字"""
    text = (prompt or "").strip()
    return text.startswith(DEEP_THINKING_PREFIX) or any(k in text for k in PLANNING_KEYWORDS)
