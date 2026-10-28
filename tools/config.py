"""
集中路徑與環境設定

只依賴標準函式庫，任何模組都能 import。
    - PROJECT_ROOT / TEMP_DIR / temp_path()：專案根目錄與執行期狀態目錄 (_temp/，不進版控)
    - env_str / env_int / env_float / env_bool：呼叫時才讀環境變數 (不快取)，格式錯誤或低於下限一律用預設值
    - feature_summary()：已設定哪些選用功能；只輸出功能名稱與 ✅/⚪，絕不輸出值
"""

import os
from typing import Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMP_DIR = os.path.join(PROJECT_ROOT, "_temp")


def temp_path(*parts: str) -> str:
    """_temp/ 底下的路徑；只組路徑、不建目錄 (模組常數在 import 時就會呼叫)，寫檔的地方自己 makedirs"""
    return os.path.join(TEMP_DIR, *parts)


def env_str(name: str, default: str = "") -> str:
    """去除前後空白的環境變數；設為空字串時回傳 ''（不是預設值）"""
    return os.getenv(name, default).strip()


def env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    """整數環境變數；未設定、格式錯誤或小於 minimum 時回傳預設值"""
    try:
        value = int(env_str(name))
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def env_float(name: str, default: float, minimum: Optional[float] = None) -> float:
    """浮點數環境變數；未設定、格式錯誤 (含 nan) 或小於 minimum 時回傳預設值"""
    try:
        value = float(env_str(name))
    except ValueError:
        return default
    if value != value or (minimum is not None and value < minimum):
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    """布林環境變數；1 / true / yes / on 為真，未設定或空字串用預設值，其餘為假"""
    raw = env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# (顯示名稱, 需要的環境變數)：全部有值才算啟用
_FEATURES = [
    ("天氣", ("CWA_API_KEY",)),
    ("永豐", ("SJ_API_KEY", "SJ_SEC_KEY")),
    ("Ticket", ("TICKETS_REPO", "TICKETS_GITHUB_TOKEN")),
    ("看板", ("TICKETS_PROJECT", "TICKETS_PROJECT_TOKEN")),
    ("call_api 金鑰", ("HTTP_API_CREDENTIALS",)),
]


def feature_summary() -> str:
    """例如「✅天氣、⚪永豐、…」；只看有沒有設定，不輸出任何值"""
    return "、".join(("✅" if all(env_str(n) for n in names) else "⚪") + label for label, names in _FEATURES)
