"""
ArQ 重開機概念澄清與回歸測試套件 (task/test_reboot_clarification.py)
專門驗證：
1. 「重開機」意指 ArQ 機器人服務/程式本體重啟 (Bot Restart)，非電腦 PC 實體關機/重開機。
2. tools/restart_manager.py 中 execute_pc_reboot 已安全停用實體關機，重定向至程式重啟。
3. bot.py 的 /reboot 斜線指令執行 ArQ 服務重啟 (/restart 已移除，只保留 /reboot)。
4. 自然語言「重開機」、「重新開機」、「重啟」等皆觸發 ArQ 程式重啟。
5. 自然語言「重開電腦」提供溫馨提醒指引，不執行主機重開機。
6. agent.py 提示詞明確記載架構規範。
8. 「重開電腦」走 /pc-reboot 的電腦重開機流程 (先確認、可取消)，與 /reboot 分開。
7. 全專案 Python 語法驗證通過。
"""

import asyncio
import os
import py_compile
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools.restart_manager import RestartManager, RestartState


class TestRebootClarification(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="arq_clarification_test_")
        self.mgr = RestartManager(state_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_restart_state_default_and_types(self):
        """驗證 RestartState 預設為 bot 重啟類型"""
        state = RestartState()
        self.assertEqual(state.type, "bot")

        recorded = self.mgr.record_restart_intent(
            restart_type="bot",
            user_id=12345,
            channel_id=67890,
            reason="測試程式重啟",
        )
        self.assertEqual(recorded.type, "bot")
        consumed = self.mgr.consume_restart_state()
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.type, "bot")

    def test_02_pc_reboot_is_separate_from_bot_restart(self):
        """電腦重開機 (execute_pc_reboot) 走 shutdown 排程，不會呼叫 ArQ 程式重啟"""
        with patch("tools.pc_power.schedule_reboot", return_value={"success": True, "delay_seconds": 30}) as mock_sched, \
             patch.object(RestartManager, "execute_bot_restart") as mock_bot_exec:
            RestartManager.execute_pc_reboot(delay_seconds=30)
            mock_sched.assert_called_once_with(30)
            mock_bot_exec.assert_not_called()

    def test_03_boot_report_mentions_arq_online(self):
        """驗證開機回報 Embed 清楚標示 ArQ 服務重啟成功「我開機了！」"""
        state = RestartState(
            type="bot",
            channel_id=1001,
            user_id=2002,
            initiated_at=time.time() - 3.2,
            reason="使用者透過 /reboot 指令重啟 ArQ 服務",
        )
        sys_status = {
            "cpu_usage": "18.5%",
            "memory_used": "45.0%",
            "uptime": "5 小時 20 分",
        }
        report = RestartManager.build_boot_report(state, sys_status)

        self.assertIn("我開機了", report["title"])
        self.assertIn("ArQ 服務已成功重新啟動完畢", report["description"])
        self.assertIn("18.5%", report["description"])
        self.assertIn("5 小時 20 分", report["description"])

    def test_04_agent_system_instruction_clarification(self):
        """驗證 agent.py SYSTEM_INSTRUCTION 包含重開機指程式重啟之說明"""
        import agent
        self.assertIn("是指重新啟動 ArQ 程式本體", agent.SYSTEM_INSTRUCTION)
        self.assertIn("不是重開電腦", agent.SYSTEM_INSTRUCTION)


class TestBotRebootCommandsAsync(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="arq_bot_clarification_")
        self.mgr = RestartManager(state_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_05_slash_reboot_executes_bot_restart(self):
        """驗證 /reboot 指令執行的是 ArQ 服務重啟 (execute_bot_restart)，而非主機重開機"""
        import bot

        interaction = MagicMock()
        interaction.user.id = 9999
        interaction.channel_id = 8888
        interaction.channel = MagicMock()
        interaction.channel.send = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.guild_id = None

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        with patch("bot.is_authorized", return_value=True), \
             patch("bot.ask_confirmation", new_callable=AsyncMock) as mock_ask, \
             patch("bot.restart_mgr", self.mgr), \
             patch("bot.bot", mock_bot), \
             patch.object(self.mgr, "execute_bot_restart") as mock_exec, \
             patch.object(self.mgr, "execute_pc_reboot") as mock_pc_exec:

            mock_ask.return_value = True

            await bot.cmd_reboot.callback(interaction)

            # 驗證觸發二次確認
            mock_ask.assert_called_once()
            # 驗證狀態寫入 type="bot"
            state = self.mgr.consume_restart_state()
            self.assertIsNotNone(state)
            self.assertEqual(state.type, "bot")
            self.assertIn("/reboot", state.reason)

            # 驗證執行了 execute_bot_restart，且未執行 execute_pc_reboot
            mock_exec.assert_called_once()
            mock_pc_exec.assert_not_called()

    async def test_05b_restart_runs_even_if_task_cancelled_after_bot_close(self):
        """bot.close() 後 bot.run() 會取消執行中的工作；之後若還有 await，重啟就不會發生 (程式 exit 0 死掉)"""
        import bot

        channel = MagicMock()
        channel.send = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_agent = MagicMock()
        mock_agent.close.side_effect = lambda: time.sleep(0.2)  # 真實的刪除快取要打網路，需要時間

        with patch("bot.restart_mgr", self.mgr), \
             patch("bot.bot", mock_bot), \
             patch("bot.agent", mock_agent), \
             patch.object(self.mgr, "execute_bot_restart") as mock_exec:
            task = asyncio.create_task(bot.perform_reboot(channel, 1, 2, None, "/reboot"))
            while not mock_bot.close.await_count and not task.done():
                await asyncio.sleep(0.01)
            task.cancel()  # 模擬 bot.run() 在 bot.close() 後結束、asyncio.run 取消剩餘工作
            try:
                await task
            except asyncio.CancelledError:
                pass

            mock_agent.close.assert_called_once()
            mock_exec.assert_called_once()

    async def test_06_natural_language_reboot_triggers_bot_restart(self):
        """驗證使用者在文字對話輸入「重開機」或「重新開機」，觸發 ArQ 程式重啟"""
        import bot

        for trigger in ["重開機", "重新開機", "重啟", "重開 arq", "重啟 arq", "reboot", "/reboot"]:
            message = MagicMock()
            message.author.bot = False
            message.author.id = 9999
            message.channel.id = 8888
            message.guild = None
            message.content = trigger
            message.clean_content = trigger
            message.reference = None
            message.mentions = []
            message.reply = AsyncMock()
            message.channel.send = AsyncMock()

            mock_bot = MagicMock()
            mock_bot.user.name = "ArQ"
            mock_bot.user.id = 1111
            mock_bot.close = AsyncMock()

            # 同一個 mgr 連續觸發多次會命中防死循環門檻；此測試驗證的是觸發詞，故停用該保護
            with patch("bot.is_authorized", return_value=True), \
                 patch("bot.ask_confirmation", new_callable=AsyncMock) as mock_ask, \
                 patch("bot.restart_mgr", self.mgr), \
                 patch("bot.bot", mock_bot), \
                 patch.object(self.mgr, "execute_bot_restart") as mock_exec, \
                 patch.object(self.mgr, "execute_pc_reboot") as mock_pc_exec, \
                 patch.object(self.mgr, "is_restart_looping", return_value=False):

                mock_ask.return_value = True

                await bot.on_message(message)

                mock_ask.assert_called_once()
                mock_exec.assert_called_once()
                mock_pc_exec.assert_not_called()

                # 消費清理狀態以便下一輪迴圈
                self.mgr.consume_restart_state()

    async def test_07_natural_language_pc_reboot_goes_to_pc_flow(self):
        """「重開電腦」走電腦重開機流程 (先確認)；單說「重開機」不會重開電腦"""
        import bot

        for pc_phrase in ["重開電腦", "重啟電腦", "重開主機", "重啟主機"]:
            message = MagicMock()
            message.author.bot = False
            message.author.id = 9999
            message.channel.id = 8888
            message.guild = None
            message.content = pc_phrase
            message.clean_content = pc_phrase
            message.reference = None
            message.mentions = []
            message.reply = AsyncMock()

            mock_bot = MagicMock()
            mock_bot.user.name = "ArQ"
            mock_bot.user.id = 1111

            with patch("bot.is_authorized", return_value=True), \
                 patch("bot.bot", mock_bot), \
                 patch("bot.perform_pc_reboot", new_callable=AsyncMock) as mock_pc, \
                 patch("bot.perform_reboot", new_callable=AsyncMock) as mock_bot_restart:
                await bot.on_message(message)
                mock_pc.assert_called_once()
                mock_bot_restart.assert_not_called()

    def test_08_syntax_check_all_core_files(self):
        """驗證所有核心檔案語法正常"""
        files = [
            os.path.join(PROJECT_ROOT, "bot.py"),
            os.path.join(PROJECT_ROOT, "agent.py"),
            os.path.join(PROJECT_ROOT, "tools", "restart_manager.py"),
            os.path.join(PROJECT_ROOT, "tools", "risk_policy.py"),
        ]
        for f in files:
            self.assertTrue(os.path.isfile(f), f"檔案不存在: {f}")
            py_compile.compile(f, doraise=True)


if __name__ == "__main__":
    unittest.main()
