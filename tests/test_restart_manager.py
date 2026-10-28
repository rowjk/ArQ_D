"""
RestartManager 單元測試套件
測試重啟狀態持久化、原子性消費、開機通知產生、逾時與防死循環保護。
使用 Python 內建 unittest 模組，不依賴任何外部套件。
"""

import json
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


class TestRestartManager(unittest.TestCase):
    def setUp(self):
        # 建立獨立的暫存資料夾避免干擾主專案
        self.test_dir = tempfile.mkdtemp(prefix="arq_restart_test_")
        self.manager = RestartManager(state_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_and_consume_state_bot(self):
        """測試 Bot 重啟狀態記錄與成功消費"""
        channel_id = 9988776655
        user_id = 11223344
        reason = "手動 /reboot 指令"

        # 1. 記錄狀態
        state = self.manager.record_restart_intent(
            restart_type="bot",
            user_id=user_id,
            channel_id=channel_id,
            reason=reason,
        )
        self.assertEqual(state.type, "bot")
        self.assertEqual(state.channel_id, channel_id)
        self.assertEqual(state.user_id, user_id)
        self.assertTrue(os.path.isfile(self.manager.state_file_path))

        # 2. 消費狀態 (模擬重啟後開機 on_ready)
        consumed = self.manager.consume_restart_state()
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.type, "bot")
        self.assertEqual(consumed.channel_id, channel_id)
        self.assertEqual(consumed.user_id, user_id)
        self.assertEqual(consumed.reason, reason)

        # 3. 確保檔案在消費後已被自動刪除 (避免重複通知)
        self.assertFalse(os.path.isfile(self.manager.state_file_path))

        # 4. 二次消費應回傳 None
        second_consumed = self.manager.consume_restart_state()
        self.assertIsNone(second_consumed)

    def test_record_and_consume_state_pc(self):
        """測試 PC 重開機狀態記錄與成功消費"""
        channel_id = 12345678
        user_id = 87654321
        reason = "手動 /reboot 電腦指令"

        state = self.manager.record_restart_intent(
            restart_type="pc",
            user_id=user_id,
            channel_id=channel_id,
            reason=reason,
        )
        self.assertEqual(state.type, "pc")

        consumed = self.manager.consume_restart_state()
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.type, "pc")
        self.assertFalse(os.path.isfile(self.manager.state_file_path))

    def test_expired_state_is_ignored(self):
        """測試過期狀態（如關機超過30分鐘才開機）會被安全忽略並清理"""
        state = self.manager.record_restart_intent(
            restart_type="bot",
            user_id=123,
            channel_id=456,
        )
        # 人工篡改時間戳為 2 小時前
        with open(self.manager.state_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["initiated_at"] = time.time() - 7200
        with open(self.manager.state_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        # 消費時應判定過期回傳 None，且檔案已被清理
        consumed = self.manager.consume_restart_state()
        self.assertIsNone(consumed)
        self.assertFalse(os.path.isfile(self.manager.state_file_path))

    def test_corrupted_json_handling(self):
        """測試狀態檔內容損毀時能夠容錯並不拋出例外"""
        with open(self.manager.state_file_path, "w", encoding="utf-8") as f:
            f.write("INVALID JSON CONTENT {[[")

        consumed = self.manager.consume_restart_state()
        self.assertIsNone(consumed)
        self.assertFalse(os.path.isfile(self.manager.state_file_path))

    def test_loop_detection(self):
        """測試頻繁重啟死循環偵測機制"""
        hist_file = os.path.join(self.test_dir, "test_loop.json")

        # 連續 4 次應尚未觸發死循環警示
        for _ in range(4):
            is_loop = self.manager.is_restart_looping(history_file=hist_file)
            self.assertFalse(is_loop)

        # 第 5 次應觸發警示
        is_loop = self.manager.is_restart_looping(history_file=hist_file)
        self.assertTrue(is_loop)

    def test_build_boot_report(self):
        """測試開機回報 Embed 內容生成"""
        state = RestartState(
            type="bot",
            channel_id=111,
            user_id=222,
            initiated_at=time.time() - 4.5,
            reason="測試指令",
        )
        sys_status = {
            "cpu_usage": "15.2%",
            "memory_used": "42.0%",
            "uptime": "2 小時 15 分",
        }

        report = RestartManager.build_boot_report(state, sys_status)
        self.assertIn("我開機了", report["title"])
        self.assertIn("15.2%", report["description"])
        self.assertIn("42.0%", report["description"])
        self.assertEqual(report["channel_id"], 111)
        self.assertEqual(report["user_id"], 222)
        self.assertGreaterEqual(report["elapsed_seconds"], 4.0)

        # 電腦重開機 (type="pc") 回報「電腦重新開機完成」，與 ArQ 程式重啟區分
        state_pc = RestartState(
            type="pc",
            channel_id=111,
            user_id=222,
            initiated_at=time.time() - 45.0,
            reason="重開電腦指令",
        )
        report_pc = RestartManager.build_boot_report(state_pc, sys_status)
        self.assertIn("電腦重新開機完成", report_pc["title"])
        self.assertIn("2 小時 15 分", report_pc["description"])


if __name__ == "__main__":
    unittest.main()
