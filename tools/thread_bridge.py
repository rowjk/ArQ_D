"""
背景執行緒 → 事件迴圈橋接：RD 等在執行緒中長時間運行的任務，靠這裡把訊息送回 Discord、
或阻塞等待使用者按同意。只依賴標準函式庫（不 import discord / bot / agent）。
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def _warn(message: str) -> None:
    """記警告；紀錄系統本身出錯（如編碼）時也不得拋例外。"""
    try:
        logger.warning(message)
    except Exception:
        pass


def _warn_on_error(fut) -> None:
    if not fut.cancelled() and fut.exception() is not None:
        _warn(f"⚠️ 背景通知送出失敗: {fut.exception()}")


def _on_loop_thread(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


class ThreadBridge:
    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.loop = loop or asyncio.get_running_loop()   # 未傳入時必須在事件迴圈內建立

    def notifier(self, send: Optional[Callable[[str], Awaitable[Any]]]) -> Callable[[str], None]:
        """回傳可在背景執行緒呼叫的通知函式；送出失敗只記警告。"""

        def notify(text: str) -> None:
            if send is None or self.loop.is_closed():
                return
            coro = None
            try:
                coro = send(text)
                fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
            except Exception as e:
                if asyncio.iscoroutine(coro):
                    coro.close()
                _warn(f"⚠️ 背景通知送出失敗: {e}")
                return
            fut.add_done_callback(_warn_on_error)

        return notify

    def approver(self, confirm: Optional[Callable[[str, str], Awaitable[bool]]]) -> Callable[[str, str], bool]:
        """回傳可在背景執行緒呼叫的同意函式（阻塞等待）；任何失敗一律視為拒絕 (fail-closed)。"""

        def approve(title: str, detail: str) -> bool:
            if _on_loop_thread(self.loop):
                raise RuntimeError("approver 不能在事件迴圈執行緒上呼叫（會死結）")
            if confirm is None or self.loop.is_closed():
                return False
            coro = None
            try:
                coro = confirm(title, detail)
                return bool(asyncio.run_coroutine_threadsafe(coro, self.loop).result())   # confirm 自身有逾時
            except Exception as e:
                if asyncio.iscoroutine(coro):
                    coro.close()
                _warn(f"⚠️ 同意流程失敗，視為拒絕: {e}")
                return False

        return approve
