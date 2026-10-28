"""
對話記憶與派工結果跨重啟保存

    - 歷史：_temp/chat_history/<user_id>.json；待轉達的派工結果：_temp/pending_reports.json
    - 只存在 _temp/ (不進版控)：對話內容可能含私人訊息。存檔前遮蔽金鑰，並去掉圖片 (inline_data)
    - 原子寫入 (暫存檔 + os.replace)；檔案壞掉、版本不符、驗證失敗、過期一律當成空的，不影響回覆
    - GEMINI_HISTORY_PERSIST=false 關閉；GEMINI_HISTORY_MAX_AGE_HOURS (預設 24) 以上的歷史不讀回 (待轉達結果不過期)
    - 不 import agent。讀回後的裁切由呼叫端 (agent.trim_history) 負責
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List

from google.genai import types

from tools import config
from tools.config import env_bool, env_float, temp_path
from tools.secret_guard import redact, redact_obj

logger = logging.getLogger(__name__)

SCHEMA = 1


def enabled() -> bool:
    return env_bool("GEMINI_HISTORY_PERSIST", True)


def _warn(action: str, error: Exception) -> None:
    logger.warning(f"⚠️ 對話記憶{action}失敗（略過，不影響回覆）: {redact(str(error))[:200]}")


def _history_path(user_id: int) -> str:
    return temp_path("chat_history", f"{int(user_id)}.json")  # int() 防路徑注入


def _pending_path() -> str:
    return temp_path("pending_reports.json")


def _skip_real_files(path: str) -> bool:
    """單元測試 (ARQ_TESTING) 不碰真正的 _temp/，避免測試把假資料留在開發機的對話記憶裡；測試用 patch temp_path 改寫目錄"""
    if not os.getenv("ARQ_TESTING"):
        return False
    real = os.path.normcase(os.path.abspath(config.TEMP_DIR))
    return os.path.normcase(os.path.abspath(path)).startswith(real + os.sep)


def _atomic_write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(content: Any) -> Dict[str, Any]:
    data = content.model_dump(mode="json", exclude_none=True)  # bytes (thought_signature) 轉成 base64
    data["parts"] = [p for p in data.get("parts") or [] if "inline_data" not in p]
    return data


def _valid_payload(path: str, check_age: bool) -> Any:
    """讀檔並檢查版本 (與時效)；任何問題回傳 None (壞檔另印警告)。待轉達結果不看時效，免得漏掉久未開機期間完成的回報"""
    try:
        payload = _read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        _warn("讀取", e)
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    if check_age:
        max_age = env_float("GEMINI_HISTORY_MAX_AGE_HOURS", 24, minimum=0.01) * 3600
        saved_at = payload.get("saved_at")
        if not isinstance(saved_at, (int, float)) or time.time() - saved_at > max_age:
            return None
    return payload


# -------------------------------------------------------------
# 對話歷史
# -------------------------------------------------------------

def save_history(user_id: int, history: List[Any]) -> None:
    path = _history_path(user_id)
    if not enabled() or _skip_real_files(path):
        return
    try:
        items = [d for d in (_dump(c) for c in history) if d["parts"]]
        _atomic_write_json(path, {"schema": SCHEMA, "saved_at": time.time(), "history": redact_obj(items)})
    except Exception as e:
        _warn("存檔", e)


def load_history(user_id: int) -> List[Any]:
    path = _history_path(user_id)
    if not enabled() or _skip_real_files(path):
        return []
    payload = _valid_payload(path, check_age=True)
    if payload is None:
        return []
    try:
        items = payload.get("history")
        if not isinstance(items, list):
            return []
        # 用 JSON 模式驗證：bytes 欄位 (thought_signature) 才會從 base64 還原
        return [types.Content.model_validate_json(json.dumps(d)) for d in items]
    except Exception as e:  # ValidationError 等
        _warn("驗證", e)
        return []


def clear_history(user_id: int) -> None:
    path = _history_path(user_id)
    if _skip_real_files(path):
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        _warn("刪除", e)


# -------------------------------------------------------------
# 待轉達的派工結果
# -------------------------------------------------------------

def save_pending(items: List[Dict[str, Any]]) -> None:
    path = _pending_path()
    if not enabled() or _skip_real_files(path):
        return
    try:
        _atomic_write_json(path, {"schema": SCHEMA, "saved_at": time.time(), "items": redact_obj(list(items))})
    except Exception as e:
        _warn("存檔", e)


def load_pending() -> List[Dict[str, Any]]:
    path = _pending_path()
    if not enabled() or _skip_real_files(path):
        return []
    payload = _valid_payload(path, check_age=False)
    items = payload.get("items") if payload else None
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        return []
    return items
