"""
ArQ 遠端重啟、二次確認與開機主動回報完整整合測試套件
測試範疇：
1. tools/restart_manager.py 核心邏輯 (記錄、消費、逾時、防死循環、守護進程聯動、報表格式化)
2. bot.py /reboot Slash 指令 (/restart 已移除) 二次確認、防死循環與執行鏈路
3. bot.py 自然語言重啟與重開機指令攔截鏈路
4. bot.py on_ready 開機主動回報通知推播與回退機制 (DM/Channel fallback)
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 將專案根目錄加入 sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools.restart_manager import RestartManager, RestartState


class TestRestartManagerCore(unittest.TestCase):
    """測試 tools/restart_manager.py 核心模組功能"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mgr = RestartManager(state_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_and_consume_state_bot(self):
        """測試 Bot 重啟狀態記錄與單次消費"""
        channel_id = 987654321
        user_id = 123456789
        state = self.mgr.record_restart_intent(
            restart_type="bot",
            user_id=user_id,
            channel_id=channel_id,
            reason="測試手動重啟",
        )

        self.assertEqual(state.type, "bot")
        self.assertEqual(state.channel_id, channel_id)
        self.assertEqual(state.user_id, user_id)
        self.assertTrue(os.path.isfile(self.mgr.state_file_path))

        # 第一次消費：應該成功取得
        consumed = self.mgr.consume_restart_state()
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.channel_id, channel_id)
        self.assertEqual(consumed.user_id, user_id)
        self.assertEqual(consumed.type, "bot")

        # 消費後檔案應已刪除
        self.assertFalse(os.path.isfile(self.mgr.state_file_path))

        # 第二次消費：檔案已不在，應回傳 None
        consumed_again = self.mgr.consume_restart_state()
        self.assertIsNone(consumed_again)

    def test_record_and_consume_state_pc(self):
        """測試 PC 重開機狀態記錄與消費"""
        state = self.mgr.record_restart_intent(
            restart_type="pc",
            user_id=111,
            channel_id=222,
            reason="使用者要求重開電腦",
        )
        self.assertEqual(state.type, "pc")
        consumed = self.mgr.consume_restart_state()
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.type, "pc")

    def test_expired_state_is_ignored(self):
        """測試過期狀態檔 (超過 30 分鐘) 自動拋棄"""
        state = self.mgr.record_restart_intent(
            restart_type="bot",
            user_id=123,
            channel_id=456,
        )
        # 人工竄改時間戳記為 2 小時前
        state.initiated_at = time.time() - 7200
        import json
        with open(self.mgr.state_file_path, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f)

        consumed = self.mgr.consume_restart_state()
        self.assertIsNone(consumed)
        # 逾時檔案應被清理
        self.assertFalse(os.path.isfile(self.mgr.state_file_path))

    def test_corrupted_json_handling(self):
        """測試損毀的狀態檔安全清理且不崩潰"""
        with open(self.mgr.state_file_path, "w", encoding="utf-8") as f:
            f.write("{invalid_json: true,")

        consumed = self.mgr.consume_restart_state()
        self.assertIsNone(consumed)
        self.assertFalse(os.path.isfile(self.mgr.state_file_path))

    def test_loop_detection(self):
        """測試防死循環偵測"""
        hist_file = os.path.join(self.test_dir, "restart_history.json")
        for _ in range(4):
            self.assertFalse(self.mgr.is_restart_looping(history_file=hist_file))

        # 第 5 次時觸發死循環警示
        self.assertTrue(self.mgr.is_restart_looping(history_file=hist_file))

    def test_execute_bot_restart_supervised(self):
        """測試守護模式 (ARQ_SUPERVISED=1) 下調用 os._exit(42)"""
        with patch.dict(os.environ, {"ARQ_SUPERVISED": "1"}):
            with patch("os._exit") as mock_exit:
                self.mgr.execute_bot_restart(delay_seconds=0.01)
                mock_exit.assert_called_once_with(42)

    def test_execute_bot_restart_detached(self):
        """測試獨立模式 (ARQ_SUPERVISED 未設定) 下繁衍背景行程並呼叫 os._exit(0)"""
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.Popen") as mock_popen, patch("os._exit") as mock_exit:
                self.mgr.execute_bot_restart(delay_seconds=0.01)
                mock_popen.assert_called_once()
                mock_exit.assert_called_once_with(0)

    def test_execute_pc_reboot(self):
        """execute_pc_reboot 交給 pc_power.schedule_reboot 排定延遲重開機 (不會重啟 ArQ 程式)"""
        with patch("tools.pc_power.schedule_reboot", return_value={"success": True, "delay_seconds": 60}) as mock_sched, \
             patch.object(RestartManager, "execute_bot_restart") as mock_bot_exec:
            res = self.mgr.execute_pc_reboot(delay_seconds=60)
            mock_sched.assert_called_once_with(60)
            mock_bot_exec.assert_not_called()
            self.assertTrue(res["success"])

    def test_build_boot_report_bot_and_pc(self):
        """測試開機報告 Embed 結構與文字"""
        sys_status = {
            "cpu_usage": "15.0%",
            "memory_used": "32.0%",
            "uptime": "2 分鐘",
        }

        # Bot 重啟報告
        bot_state = RestartState(
            type="bot",
            channel_id=123,
            user_id=456,
            initiated_at=time.time() - 3.5,
            reason="測試重啟",
        )
        bot_report = RestartManager.build_boot_report(bot_state, sys_status)
        self.assertIn("我開機了", bot_report["title"])
        self.assertIn("ArQ 重新啟動完成", bot_report["title"])
        self.assertIn("3.5", str(bot_report["elapsed_seconds"]))
        self.assertEqual(bot_report["color"], 0x3498db)

        # 電腦重開機回報 (與程式重啟區分：標題、顏色不同，耗時以分秒顯示)
        pc_state = RestartState(
            type="pc",
            channel_id=123,
            user_id=456,
            initiated_at=time.time() - 45.2,
            reason="重開機指令",
        )
        pc_report = RestartManager.build_boot_report(pc_state, sys_status)
        self.assertIn("電腦重新開機完成", pc_report["title"])
        self.assertIn("0 分 45 秒", pc_report["description"])
        self.assertIn("45.2", str(pc_report["elapsed_seconds"]))
        self.assertEqual(pc_report["color"], 0x9b59b6)


class TestDiscordBotWorkflow(unittest.IsolatedAsyncioTestCase):
    """測試 bot.py 內 Slash 指令、自然語言與 on_ready 開機回報整合鏈路"""

    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mgr = RestartManager(state_dir=self.test_dir)

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_interaction(self):
        """建立假的 Interaction (response.send_message 與 channel.send 皆為 async)"""
        interaction = MagicMock()
        interaction.user.id = 12345
        interaction.channel_id = 67890
        interaction.channel = MagicMock()
        interaction.channel.send = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.guild_id = None
        return interaction

    async def test_slash_reboot_flow_approved(self):
        """模擬 /reboot 斜線指令經二次確認同意後執行完整重啟流程"""
        interaction = self._make_interaction()
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        with patch("bot.is_authorized", return_value=True), \
             patch("bot.ask_confirmation", new_callable=AsyncMock) as mock_ask, \
             patch("bot.restart_mgr", self.mgr), \
             patch("bot.bot", mock_bot):

            mock_ask.return_value = True

            import bot
            with patch.object(self.mgr, "execute_bot_restart") as mock_exec:
                await bot.cmd_reboot.callback(interaction)

                # Discord 要求 3 秒內回應互動，否則顯示「此互動失敗」
                interaction.response.send_message.assert_called_once()
                mock_ask.assert_called_once()
                state = self.mgr.consume_restart_state()
                self.assertIsNotNone(state)
                self.assertEqual(state.type, "bot")
                self.assertEqual(state.user_id, 12345)
                self.assertEqual(state.channel_id, 67890)
                mock_bot.close.assert_called_once()
                mock_exec.assert_called_once()

    async def test_slash_reboot_flow_rejected(self):
        """模擬 /reboot 斜線指令經二次確認拒絕後取消重啟"""
        interaction = self._make_interaction()
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        with patch("bot.is_authorized", return_value=True), \
             patch("bot.ask_confirmation", new_callable=AsyncMock) as mock_ask, \
             patch("bot.restart_mgr", self.mgr), \
             patch("bot.bot", mock_bot):

            mock_ask.return_value = False

            import bot
            with patch.object(self.mgr, "execute_bot_restart") as mock_exec:
                await bot.cmd_reboot.callback(interaction)

                interaction.response.send_message.assert_called_once()
                mock_ask.assert_called_once()
                self.assertIsNone(self.mgr.consume_restart_state())
                mock_bot.close.assert_not_called()
                mock_exec.assert_not_called()
                interaction.channel.send.assert_called_once_with("❌ 已取消重啟操作。")

    async def test_slash_reboot_blocked_when_looping(self):
        """短時間內重啟過於頻繁時，/reboot 即使已同意也不應執行 (防死循環)"""
        interaction = self._make_interaction()
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        with patch("bot.is_authorized", return_value=True), \
             patch("bot.ask_confirmation", new_callable=AsyncMock, return_value=True), \
             patch("bot.restart_mgr", self.mgr), \
             patch("bot.bot", mock_bot), \
             patch.object(self.mgr, "is_restart_looping", return_value=True):

            import bot
            with patch.object(self.mgr, "execute_bot_restart") as mock_exec:
                await bot.cmd_reboot.callback(interaction)

                mock_exec.assert_not_called()
                mock_bot.close.assert_not_called()
                self.assertIsNone(self.mgr.consume_restart_state())
                sent = interaction.channel.send.call_args[0][0]
                self.assertIn("過於頻繁", sent)

    async def test_restart_command_removed(self):
        """只保留 /reboot，/restart 指令已移除"""
        import bot
        names = {c.name for c in bot.bot.tree.get_commands()}
        self.assertIn("reboot", names)
        self.assertNotIn("restart", names)

    async def test_natural_language_restart_flow(self):
        """模擬自然語言訊息「重啟 ArQ」觸發確認並重啟"""
        message = MagicMock()
        message.author.bot = False
        message.author.id = 12345
        message.channel.id = 67890
        message.guild = None
        message.content = "重啟 ArQ"
        message.clean_content = "重啟 ArQ"
        message.reference = None
        message.mentions = []
        message.reply = AsyncMock()
        message.channel.send = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.user.name = "ArQ"
        mock_bot.user.id = 99999
        mock_bot.close = AsyncMock()
        mock_bot.process_commands = AsyncMock()

        with patch("bot.is_authorized", return_value=True), \
             patch("bot.ask_confirmation", new_callable=AsyncMock) as mock_ask, \
             patch("bot.restart_mgr", self.mgr), \
             patch("bot.bot", mock_bot):

            mock_ask.return_value = True

            import bot
            with patch.object(self.mgr, "execute_bot_restart") as mock_exec:
                await bot.on_message(message)

                mock_ask.assert_called_once()
                state = self.mgr.consume_restart_state()
                self.assertIsNotNone(state)
                self.assertEqual(state.type, "bot")
                mock_bot.close.assert_called_once()
                mock_exec.assert_called_once()

    async def test_on_ready_boot_broadcast_success(self):
        """模擬 on_ready 開機主動回報推播流程"""
        # 先注入重啟狀態
        self.mgr.record_restart_intent(
            restart_type="bot",
            user_id=12345,
            channel_id=67890,
            reason="測試開機廣播",
        )

        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.tree.sync = AsyncMock(return_value=[])
        mock_bot.change_presence = AsyncMock()

        with patch("bot.restart_mgr", self.mgr), \
             patch("bot.bot", mock_bot), \
             patch("bot.idle_heartbeat_loop.is_running", return_value=True):

            import bot
            await bot.on_ready()

            # 驗證推播已被發送
            mock_channel.send.assert_called_once()
            call_kwargs = mock_channel.send.call_args[1]
            embed = call_kwargs["embed"]
            self.assertIn("我開機了", embed.title)
            # 狀態應已被單次消費
            self.assertIsNone(self.mgr.consume_restart_state())


if __name__ == "__main__":
    unittest.main()
