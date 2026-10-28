"""
日誌設定：主控台（UTF-8，格式同原本的 print）＋輪替檔 _temp/logs/arq.log（1 MB × 5 份）。

- 所有 handler 都掛 RedactFilter（secret_guard.redact），金鑰不會進主控台或檔案。
- 紀錄會包含使用者訊息內容，只能寫在 _temp/（不進版控）。
- 只依賴標準函式庫與 config / secret_guard。
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

from tools.config import env_str, temp_path
from tools.secret_guard import redact

LOG_PATH = temp_path("logs", "arq.log")

_FORMAT_CONSOLE = "%(message)s"
_FORMAT_FILE = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MARK = "_arq_handler"   # 標記自己掛的 handler，重複呼叫 setup_logging 時只換掉這些


class RedactFilter(logging.Filter):
    """先把訊息格式化完再遮蔽（金鑰可能在參數裡），traceback 與 stack 一併遮蔽"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def _level(name: Optional[str]) -> int:
    value = logging.getLevelNamesMapping().get((name or "").strip().upper())
    return value if value is not None else logging.INFO


def shutdown_logging() -> None:
    """移除並關閉 setup_logging 掛上的 handler（釋放檔案；測試與重複呼叫用）"""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(handler)
        handler.close()


def setup_logging(level: Optional[str] = None, path: Optional[str] = None) -> None:
    """設定 root logger；可重複呼叫、不重複掛 handler。level 預設讀 ARQ_LOG_LEVEL（INFO）"""
    shutdown_logging()
    root = logging.getLogger()
    root.setLevel(_level(level or env_str("ARQ_LOG_LEVEL", "INFO")))

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FORMAT_CONSOLE))

    path = path or LOG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    file = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(logging.Formatter(_FORMAT_FILE))

    for handler in (console, file):
        handler.addFilter(RedactFilter())
        setattr(handler, _MARK, True)
        root.addHandler(handler)

    logging.getLogger("discord").setLevel(logging.WARNING)
