"""
心跳訊息格式 單元測試：鎖定「一行、80 字內、含 CPU / RAM / 開機時間」

規則：
    - 閒置心跳與 /heartbeat 指令使用同一個一行格式
    - 無換行、長度 80 字以內，且保留 CPU、記憶體、開機時間三項數據
    - 取得系統狀態失敗時仍是同一個一行格式（不能因此多出說明文字）
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools.system_tools import format_heartbeat_line, get_heartbeat_line  # noqa: E402

GB = 1024 ** 3
HOUR = 3600
DAY = 86400


def assert_one_line(testcase: unittest.TestCase, text: str):
    testcase.assertNotIn("\n", text)
    testcase.assertNotIn("\r", text)
    testcase.assertEqual(len(text.splitlines()), 1)
    testcase.assertEqual(text, text.strip())
    testcase.assertLessEqual(len(text), 80)
    testcase.assertIn("CPU", text)
    testcase.assertIn("RAM", text)
    testcase.assertIn("開機", text)


class FormatHeartbeatLineTests(unittest.TestCase):
    def test_example_from_ticket(self):
        line = format_heartbeat_line(12, 8.1 * GB, 16 * GB, 3 * DAY + 4 * HOUR + 5)
        self.assertEqual(line, "💓 ArQ 在線｜CPU 12%｜RAM 8.1/16 GB｜開機 3 天 4 小時")

    def test_one_line_and_contains_three_metrics(self):
        assert_one_line(self, format_heartbeat_line(12.3, 8 * GB, 16 * GB, 3 * DAY))

    def test_uptime_under_one_hour_uses_minutes(self):
        line = format_heartbeat_line(5, 4 * GB, 16 * GB, 25 * 60)
        self.assertTrue(line.endswith("開機 25 分鐘"), line)

    def test_uptime_under_one_day_uses_hours_and_minutes(self):
        line = format_heartbeat_line(5, 4 * GB, 16 * GB, 5 * HOUR + 7 * 60)
        self.assertTrue(line.endswith("開機 5 小時 7 分"), line)

    def test_uptime_over_one_day_uses_days_and_hours(self):
        line = format_heartbeat_line(5, 4 * GB, 16 * GB, 12 * DAY + 23 * HOUR + 59 * 60)
        self.assertTrue(line.endswith("開機 12 天 23 小時"), line)

    def test_cpu_is_rounded_to_integer_percent(self):
        self.assertIn("CPU 13%", format_heartbeat_line(12.6, GB, 16 * GB, 60))

    def test_worst_case_still_within_80_chars(self):
        line = format_heartbeat_line(100, 1023.9 * GB, 1024 * GB, 9999 * DAY + 23 * HOUR)
        assert_one_line(self, line)


class GetHeartbeatLineTests(unittest.TestCase):
    def test_reads_psutil_and_formats(self):
        mem = MagicMock(used=8 * GB, total=16 * GB)
        with patch("tools.system_tools.psutil.cpu_percent", return_value=12.0), \
             patch("tools.system_tools.psutil.virtual_memory", return_value=mem), \
             patch("tools.system_tools.psutil.boot_time", return_value=1000.0), \
             patch("tools.system_tools.time.time", return_value=1000.0 + 2 * DAY + 3 * HOUR):
            line = get_heartbeat_line()
        self.assertEqual(line, "💓 ArQ 在線｜CPU 12%｜RAM 8.0/16 GB｜開機 2 天 3 小時")

    def test_failure_falls_back_to_same_one_line_format(self):
        with patch("tools.system_tools.psutil.cpu_percent", side_effect=RuntimeError("boom")):
            line = get_heartbeat_line()
        assert_one_line(self, line)
        self.assertNotIn("boom", line)


class HeartbeatSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """閒置心跳與 /heartbeat 指令實際送出的內容"""

    LINE = "💓 ArQ 在線｜CPU 12%｜RAM 8.1/16 GB｜開機 3 天 4 小時"

    async def test_idle_heartbeat_sends_only_the_one_line(self):
        import bot
        channel = MagicMock()
        channel.send = AsyncMock()
        fake_bot = MagicMock()
        fake_bot.get_channel.return_value = channel
        fake_dt = MagicMock()
        fake_dt.now.return_value.hour = 12  # 非勿擾時段

        with patch("bot.bot", fake_bot), \
             patch("bot.datetime", fake_dt), \
             patch("bot.get_heartbeat_line", return_value=self.LINE), \
             patch("bot.last_interaction_time", 0.0), \
             patch("bot.last_interaction_channel_id", 111):
            await bot.idle_heartbeat_loop.coro()

        channel.send.assert_awaited_once_with(self.LINE)

    async def test_idle_heartbeat_skipped_in_quiet_hours(self):
        import bot
        channel = MagicMock()
        channel.send = AsyncMock()
        fake_bot = MagicMock()
        fake_bot.get_channel.return_value = channel
        fake_dt = MagicMock()
        fake_dt.now.return_value.hour = 2

        with patch("bot.bot", fake_bot), \
             patch("bot.datetime", fake_dt), \
             patch("bot.last_interaction_time", 0.0), \
             patch("bot.last_interaction_channel_id", 111):
            await bot.idle_heartbeat_loop.coro()

        channel.send.assert_not_awaited()

    async def test_heartbeat_command_replies_with_the_same_one_line(self):
        import bot
        interaction = MagicMock()
        interaction.user.id = 12345
        interaction.channel_id = 67890
        interaction.response.send_message = AsyncMock()

        with patch("bot.is_authorized", return_value=True), \
             patch("bot.get_heartbeat_line", return_value=self.LINE):
            await bot.cmd_heartbeat.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(self.LINE)

    async def test_heartbeat_command_unauthorized_unchanged(self):
        import bot
        interaction = MagicMock()
        interaction.user.id = 999
        interaction.response.send_message = AsyncMock()

        with patch("bot.is_authorized", return_value=False):
            await bot.cmd_heartbeat.callback(interaction)

        args, kwargs = interaction.response.send_message.call_args
        self.assertIn("沒有權限", args[0])
        self.assertTrue(kwargs.get("ephemeral"))


if __name__ == "__main__":
    unittest.main()
