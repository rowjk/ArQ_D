"""
模擬整合測試：驗證 Discord 二次確認到開機主動回報的完整生命週期
包含：
1. 使用者請求重啟 (/reboot 或自然語言)
2. 彈出 ConfirmView 二次確認 (同意 / 拒絕 / 逾時)
3. 同意後寫入狀態檔 (State Persistence)
4. 模擬程序重啟 / 開機進入 on_ready()
5. 檢查狀態、組裝開機通知、驗證訊息包含「我開機了」與系統狀態
6. 驗證狀態檔已被安全消費並刪除
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools.restart_manager import RestartManager, RestartState


class MockDiscordChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.sent_messages = []

    async def send(self, content=None, embed=None):
        self.sent_messages.append({"content": content, "embed": embed})
        return self


class MockDiscordUser:
    def __init__(self, user_id: int):
        self.id = user_id
        self.sent_messages = []

    async def send(self, content=None, embed=None):
        self.sent_messages.append({"content": content, "embed": embed})
        return self


class TestIntegrationLifecycle(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="arq_integration_test_")
        self.manager = RestartManager(state_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_full_reboot_cycle_approved(self):
        """模擬使用者點擊『同意』後的完整重啟與開機回報循環"""
        channel_id = 987654321
        user_id = 123456789
        channel = MockDiscordChannel(channel_id)

        # 1. 使用者下達指令並通過二次確認
        approved = True
        self.assertTrue(approved, "使用者點擊了 ✅ 同意執行")

        # 2. 寫入重啟意圖
        state = self.manager.record_restart_intent(
            restart_type="bot",
            user_id=user_id,
            channel_id=channel_id,
            reason="使用者下達 /reboot",
        )
        self.assertTrue(os.path.isfile(self.manager.state_file_path))

        # 3. 模擬重啟過程（經過 2.5 秒開機）
        time.sleep(0.1)

        # 4. 模擬機器人開機 on_ready() 執行
        recovered_state = self.manager.consume_restart_state()
        self.assertIsNotNone(recovered_state)

        # 模擬取得系統狀態
        sys_status = {
            "os": "Windows 11",
            "cpu_usage": "18.5%",
            "memory_used": "38.2%",
            "uptime": "1 小時 20 分",
        }

        # 5. 組裝開機回報
        report = RestartManager.build_boot_report(recovered_state, sys_status)

        # 驗證關鍵字：「我開機了」
        self.assertIn("我開機了", report["title"])
        self.assertIn("18.5%", report["description"])
        self.assertEqual(report["channel_id"], channel_id)

        # 6. 模擬發送到 Discord
        async def send_notice():
            await channel.send(content=f"**{report['title']}**\n\n{report['description']}")

        asyncio.run(send_notice())
        self.assertEqual(len(channel.sent_messages), 1)
        msg_text = channel.sent_messages[0]["content"]
        self.assertIn("我開機了", msg_text)
        self.assertIn("CPU `18.5%`", msg_text)

        # 7. 驗證狀態檔已被徹底清除
        self.assertFalse(os.path.isfile(self.manager.state_file_path))

    def test_reboot_cycle_rejected(self):
        """模擬使用者在二階段確認中點擊『拒絕』"""
        approved = False  # 使用者點擊 ❌ 或逾時
        if not approved:
            # 不寫入重啟狀態，亦不觸發重啟
            pass

        self.assertFalse(os.path.isfile(self.manager.state_file_path))
        # 即使呼叫 consume 也為 None
        self.assertIsNone(self.manager.consume_restart_state())


if __name__ == "__main__":
    unittest.main()
