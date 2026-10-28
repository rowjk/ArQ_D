"""
Gemini 用量紀錄

每次呼叫 Gemini 後，從回應的 usage_metadata 取出 Token 數，累加成「一則使用者訊息」的總用量，
寫入 _temp/gemini_usage.jsonl (一行一筆 JSON，不進版控)，並在主控台印出一行摘要。
用途：找出哪類訊息最花 Token，作為調整對話歷史上限、thinking 等級的依據。

欄位 (皆為 Token 數)：
    prompt      ：輸入 (系統提示詞 + 工具說明 + 對話歷史 + 本次訊息)
    cached      ：輸入中命中快取的部分 (計費較便宜)
    output      ：模型輸出的文字 / 工具呼叫
    thoughts    ：thinking (以輸出價格計費)
    tool_prompt ：Google 搜尋等內建工具帶入的輸入
    total       ：以上合計
另有 calls (本則訊息呼叫 Gemini 次數)、model、thinking (chat 本則訊息最後使用的 thinking 等級)。
紀錄失敗只印警告，不影響對話。
"""

import datetime as dt
import json
import logging
import os
from typing import Any, Dict

from tools.config import PROJECT_ROOT, temp_path  # noqa: F401

logger = logging.getLogger(__name__)

USAGE_LOG_PATH = temp_path("gemini_usage.jsonl")

# 紀錄欄位 → usage_metadata 屬性名稱
_FIELDS = {
    "prompt": "prompt_token_count",
    "cached": "cached_content_token_count",
    "output": "candidates_token_count",
    "thoughts": "thoughts_token_count",
    "tool_prompt": "tool_use_prompt_token_count",
    "total": "total_token_count",
}


def empty_usage() -> Dict[str, int]:
    return {key: 0 for key in _FIELDS}


def usage_of(response: Any) -> Dict[str, int]:
    """取出單次回應的 Token 用量；沒有 usage_metadata 時全部為 0"""
    meta = getattr(response, "usage_metadata", None)
    return {key: int(getattr(meta, attr, None) or 0) for key, attr in _FIELDS.items()}


def add_usage(total: Dict[str, int], usage: Dict[str, int]) -> None:
    """把 usage 累加到 total (就地修改)"""
    for key in _FIELDS:
        total[key] = total.get(key, 0) + usage.get(key, 0)


def format_usage(usage: Dict[str, int]) -> str:
    calls = f"{usage['calls']} 次呼叫｜" if usage.get("calls") else ""
    return calls + (
        f"輸入 {usage['prompt']}（快取 {usage['cached']}）｜輸出 {usage['output']}｜"
        f"thinking {usage['thoughts']}｜合計 {usage['total']}"
    )


def record_usage(source: str, usage: Dict[str, int], **extra: Any) -> None:
    """寫入一筆用量紀錄並印出摘要；source 為來源 (chat / search_web)"""
    entry = {"time": dt.datetime.now().isoformat(timespec="seconds"), "source": source, **usage, **extra}
    logger.info(f"📊 Gemini 用量 [{source}] {format_usage(usage)}")
    try:
        os.makedirs(os.path.dirname(USAGE_LOG_PATH), exist_ok=True)
        with open(USAGE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"⚠️ 寫入 Gemini 用量紀錄失敗: {e}", exc_info=True)
