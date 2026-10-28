"""
Ticket 單元測試：統一背景執行緒回到事件迴圈的通知與同意 (tools/thread_bridge.py)

驗收標準：
- ThreadBridge(loop).notifier(send) / .approver(confirm)
- fail-closed：沒有 confirm、事件迴圈已關閉、confirm 拋例外 → approver 回傳 False
- 在事件迴圈執行緒上呼叫 approver → RuntimeError（否則死結）
- notifier 送出失敗只記警告，不影響背景任務
- bot.make_thread_notifier 長通知以 split_message 分段，不在 2,000 字截斷
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import thread_bridge  # noqa: E402
from tools.thread_bridge import ThreadBridge  # noqa: E402


async def _flush():
    """讓事件迴圈把已排程的 callback 跑完"""
    for _ in range(5):
        await asyncio.sleep(0)


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_notify_from_thread_reaches_send(self):
        received = []

        async def send(text):
            received.append(text)

        notify = ThreadBridge(asyncio.get_running_loop()).notifier(send)
        await asyncio.to_thread(notify, "你好")
        await _flush()
        self.assertEqual(received, ["你好"])

    async def test_notify_without_send_is_noop(self):
        notify = ThreadBridge(asyncio.get_running_loop()).notifier(None)
        await asyncio.to_thread(notify, "x")

    async def test_send_failure_only_warns(self):
        async def send(text):
            raise ConnectionError("discord down")

        notify = ThreadBridge(asyncio.get_running_loop()).notifier(send)
        with self.assertLogs("tools.thread_bridge", level="WARNING") as logs:
            await asyncio.to_thread(notify, "x")
            await _flush()
        self.assertIn("discord down", " ".join(logs.output))

    async def test_notify_on_closed_loop_is_noop(self):
        loop = asyncio.new_event_loop()
        notify = ThreadBridge(loop).notifier(lambda t: asyncio.sleep(0))
        loop.close()
        await asyncio.to_thread(notify, "x")


def _broken_warning(*args, **kwargs):
    raise UnicodeEncodeError("cp950", "⚠", 0, 1, "illegal multibyte sequence")


class LoggingFailureTests(unittest.IsolatedAsyncioTestCase):
    """紀錄輸出失敗（如主控台 cp950 不支援 emoji）時，記警告本身也不能讓橋接失效"""

    async def test_approver_still_denies_when_print_fails(self):
        async def boom(title, detail):
            raise RuntimeError("卡片送出失敗")

        approve = ThreadBridge(asyncio.get_running_loop()).approver(boom)
        with mock.patch.object(thread_bridge.logger, "warning", _broken_warning):
            self.assertIs(await asyncio.to_thread(approve, "t", "d"), False)

    async def test_notifier_still_returns_when_print_fails(self):
        async def send(text):
            raise ConnectionError("discord down")

        notify = ThreadBridge(asyncio.get_running_loop()).notifier(send)
        with mock.patch.object(thread_bridge.logger, "warning", _broken_warning):
            await asyncio.to_thread(notify, "x")
            await _flush()


class ApproverTests(unittest.IsolatedAsyncioTestCase):
    async def _approve(self, confirm):
        approve = ThreadBridge(asyncio.get_running_loop()).approver(confirm)
        return await asyncio.to_thread(approve, "標題", "內容")

    async def test_true_and_false_are_returned(self):
        async def yes(title, detail):
            return True

        async def no(title, detail):
            return False

        self.assertIs(await self._approve(yes), True)
        self.assertIs(await self._approve(no), False)

    async def test_confirm_receives_title_and_detail(self):
        seen = []

        async def confirm(title, detail):
            seen.append((title, detail))
            return True

        await self._approve(confirm)
        self.assertEqual(seen, [("標題", "內容")])

    async def test_no_confirm_is_denied(self):
        self.assertIs(await self._approve(None), False)

    async def test_confirm_exception_is_denied_not_raised(self):
        async def boom(title, detail):
            raise RuntimeError("卡片送出失敗")

        self.assertIs(await self._approve(boom), False)

    async def test_closed_loop_is_denied(self):
        loop = asyncio.new_event_loop()
        approve = ThreadBridge(loop).approver(lambda t, d: asyncio.sleep(0, result=True))
        loop.close()
        self.assertIs(await asyncio.to_thread(approve, "t", "d"), False)

    async def test_calling_on_loop_thread_raises(self):
        async def yes(title, detail):
            return True

        approve = ThreadBridge(asyncio.get_running_loop()).approver(yes)
        with self.assertRaises(RuntimeError):
            approve("t", "d")


class BotWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_make_thread_notifier_splits_long_text(self):
        import bot

        sent = []

        class FakeChannel:
            async def send(self, text):
                sent.append(text)

        lines = [f"第{i:03d}行 " + "字" * 40 for i in range(100)]
        text = "\n".join(lines)
        self.assertGreater(len(text), 4500)

        notify = bot.make_thread_notifier(FakeChannel())
        await asyncio.to_thread(notify, text)
        await _flush()

        self.assertGreaterEqual(len(sent), 3)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in sent))
        for line in lines:
            self.assertTrue(any(line in chunk for chunk in sent), f"遺失: {line}")

    async def test_make_thread_approver_uses_ask_confirmation(self):
        import bot

        channel, calls = object(), []

        async def fake_ask(ch, owner_id, title, detail):
            calls.append((ch, owner_id, title, detail))
            return True

        with mock.patch.object(bot, "ask_confirmation", fake_ask):
            approve = bot.make_thread_approver(channel, 42)
            result = await asyncio.to_thread(approve, "T", "D")

        self.assertIs(result, True)
        self.assertEqual(calls, [(channel, 42, "T", "D")])


class AgentWiringTests(unittest.TestCase):
    def test_agent_no_longer_defines_its_own_bridge(self):
        with open(os.path.join(PROJECT_ROOT, "agent.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertIn("ThreadBridge", source)
        self.assertNotIn("run_coroutine_threadsafe", source)

    def test_thread_bridge_has_no_forbidden_imports(self):
        with open(os.path.join(PROJECT_ROOT, "tools", "thread_bridge.py"), encoding="utf-8") as f:
            source = f.read()
        for name in ("discord", "bot", "agent"):
            self.assertNotRegex(source, rf"(?m)^\s*(import|from)\s+{name}\b")


if __name__ == "__main__":
    unittest.main()
