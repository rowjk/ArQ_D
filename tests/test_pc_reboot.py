"""
電腦重新開機 (/pc-reboot) 單元測試：不會真的重開機，shutdown 指令一律 mock

規則：
    - 重開機前要先確認；拒絕就什麼都不做
    - 同意後先記錄狀態 (開機後回報) 再排定延遲重開機，並提供取消按鈕
    - 排定失敗或取消時要清掉狀態，避免下次啟動誤報「電腦重新開機完成」
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import pc_power  # noqa: E402
from tools.restart_manager import RestartManager, RestartState  # noqa: E402


def completed(code=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


class TestPcPower(unittest.TestCase):
    def test_schedule_uses_shutdown_r_with_delay(self):
        with patch.object(pc_power, "_run", return_value=completed()) as m:
            res = pc_power.schedule_reboot(60)
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[:4], ["shutdown", "/r", "/t", "60"])
        self.assertTrue(res["success"])

    def test_delay_is_clamped(self):
        with patch.object(pc_power, "_run", return_value=completed()) as m:
            self.assertEqual(pc_power.schedule_reboot(1)["delay_seconds"], pc_power.MIN_DELAY_SECONDS)
            self.assertEqual(pc_power.schedule_reboot(99999)["delay_seconds"], pc_power.MAX_DELAY_SECONDS)

    def test_schedule_failure(self):
        with patch.object(pc_power, "_run", return_value=completed(5, err="Access is denied.")):
            res = pc_power.schedule_reboot(60)
        self.assertFalse(res["success"])
        self.assertIn("denied", res["error"])

    def test_cancel(self):
        with patch.object(pc_power, "_run", return_value=completed()) as m:
            self.assertTrue(pc_power.cancel_reboot()["success"])
        self.assertEqual(m.call_args[0][0], ["shutdown", "/a"])
        with patch.object(pc_power, "_run", return_value=completed(1116)):
            res = pc_power.cancel_reboot()
        self.assertFalse(res["success"])
        self.assertIn("沒有排定", res["message"])

    def test_preflight_description_warns(self):
        text = pc_power.describe_preflight({"autologon": False, "autostart": False, "open_apps": ["WINWORD"]}, 60)
        self.assertIn("未設定 Windows 自動登入", text)
        self.assertIn("未啟用 ArQ 開機自動啟動", text)
        self.assertIn("WINWORD", text)
        ok = pc_power.describe_preflight({"autologon": True, "autostart": True, "open_apps": []}, 60)
        self.assertNotIn("⚠️", ok)

    def test_preflight_is_read_only(self):
        info = pc_power.preflight()  # 真實查詢，只讀
        self.assertIn("autologon", info)
        self.assertIsInstance(info["open_apps"], list)
        self.assertNotIn("explorer", [a.lower() for a in info["open_apps"]])


class TestPcRebootState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="arq_pc_reboot_")
        self.mgr = RestartManager(state_dir=self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_pc_state_has_longer_expiry(self):
        # 電腦重開機 + 登入可能超過 30 分鐘，仍應回報
        self.mgr.record_restart_intent(restart_type="pc", user_id=1, channel_id=2, reason="test")
        import json
        path = self.mgr.state_file_path
        data = json.load(open(path, encoding="utf-8"))
        data["initiated_at"] = time.time() - 2 * 3600
        json.dump(data, open(path, "w", encoding="utf-8"))
        state = self.mgr.consume_restart_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.type, "pc")


class TestPcRebootFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="arq_pc_flow_")
        self.mgr = RestartManager(state_dir=self.dir)
        self.channel = MagicMock()
        self.channel.send = AsyncMock(return_value=MagicMock())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _flow(self, approved, schedule_result=None):
        import bot
        with patch("bot.restart_mgr", self.mgr), \
             patch("bot.ask_confirmation", new_callable=AsyncMock, return_value=approved) as m_ask, \
             patch.object(pc_power, "preflight", return_value={"autologon": True, "autostart": True, "open_apps": []}), \
             patch.object(self.mgr, "execute_pc_reboot",
                          return_value=schedule_result or {"success": True, "delay_seconds": 60}) as m_exec:
            await bot.perform_pc_reboot(self.channel, 555, 111, None, "測試")
        return m_ask, m_exec

    async def test_denied_does_nothing(self):
        m_ask, m_exec = await self._flow(False)
        m_ask.assert_called_once()
        m_exec.assert_not_called()
        self.assertIsNone(self.mgr.consume_restart_state())

    async def test_approved_records_state_then_schedules(self):
        m_ask, m_exec = await self._flow(True)
        m_exec.assert_called_once_with(60)
        state = self.mgr.consume_restart_state()
        self.assertEqual((state.type, state.channel_id, state.user_id), ("pc", 555, 111))
        kwargs = self.channel.send.call_args.kwargs
        self.assertIn("view", kwargs)  # 倒數訊息附取消按鈕

    async def test_schedule_failure_clears_state(self):
        await self._flow(True, {"success": False, "delay_seconds": 60, "error": "denied"})
        self.assertIsNone(self.mgr.consume_restart_state())

    async def test_cancel_clears_state(self):
        import bot
        self.mgr.record_restart_intent(restart_type="pc", user_id=1, channel_id=2, reason="test")
        with patch("bot.restart_mgr", self.mgr), \
             patch.object(pc_power, "cancel_reboot", return_value={"success": True, "message": "ok"}):
            text = await bot._cancel_pc_reboot_text()
        self.assertIn("已取消", text)
        self.assertIsNone(self.mgr.consume_restart_state())

    async def test_slash_command_registered(self):
        import bot
        self.assertIn("pc-reboot", {c.name for c in bot.bot.tree.get_commands()})


if __name__ == "__main__":
    unittest.main()
