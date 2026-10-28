"""
tools/job_tracker.py 單元測試：行程存活判斷 (PID 重複使用) 與完成通知冪等性

背景：Windows 會很快重複使用 PID。爬蟲結束後若同一個 PID 被其他程式拿走，
只看 PID 會誤判「仍在執行」，完成通知就永遠不會發出。因此狀態檔會記錄 create_time 一併比對。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import job_tracker  # noqa: E402
from tools.job_tracker import JobTrackerManager, _get_create_time, _is_pid_alive  # noqa: E402


class TestPidAliveness(unittest.TestCase):
    def test_current_process_alive_with_matching_create_time(self):
        pid = os.getpid()
        self.assertTrue(_is_pid_alive(pid, _get_create_time(pid)))

    def test_pid_reused_by_other_process_is_not_alive(self):
        # 同一個 PID 但建立時間不同 → 代表是被重複使用的 PID，原行程已結束
        pid = os.getpid()
        self.assertFalse(_is_pid_alive(pid, _get_create_time(pid) - 3600))

    def test_legacy_state_without_create_time(self):
        # 舊版狀態檔沒有 create_time：退回單看 PID
        self.assertTrue(_is_pid_alive(os.getpid(), None))

    def test_dead_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        create_time = _get_create_time(proc.pid)
        proc.wait()
        self.assertFalse(_is_pid_alive(proc.pid, create_time))

    def test_invalid_pid(self):
        self.assertFalse(_is_pid_alive(0))
        self.assertFalse(_is_pid_alive(-1))


class TestCompletionNotify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arq_job_tracker_test_")
        self.patches = [
            patch.object(job_tracker, "ARTIFACT_DIR", self.tmp),
            patch.object(job_tracker, "STATE_FILE", os.path.join(self.tmp, "crawler_state.json")),
            patch.object(job_tracker, "LOG_FILE", os.path.join(self.tmp, "crawler_current.log")),
        ]
        for p in self.patches:
            p.start()
        self.mgr = JobTrackerManager()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_state_dir_is_task_artifact(self):
        # 執行期狀態仍放在 task/artifact/ (不進版本控制)，與搬家前相同
        self.assertEqual(
            os.path.normcase(job_tracker.PROJECT_ROOT),
            os.path.normcase(PROJECT_ROOT),
        )

    def test_notify_once_after_process_ended_even_if_pid_reused(self):
        pid = os.getpid()  # 模擬 PID 被目前這個 (仍存活的) 行程重複使用
        job_tracker._save_state({
            "pid": pid,
            "create_time": _get_create_time(pid) - 3600,  # 原行程的建立時間與現在這個不同
            "started_at": time.time() - 1200,
            "started_at_str": "2026-10-15 10:00:00",
            "notified": False,
        })
        with patch.object(JobTrackerManager, "last_log", return_value={"ok": False}):
            first = self.mgr.check_completion_for_notify()
            second = self.mgr.check_completion_for_notify()
        self.assertIsNotNone(first, "PID 被重複使用時仍應判定原行程已結束並發出通知")
        self.assertIsNone(second, "同一次執行只應通知一次")

    def test_no_notify_while_running(self):
        pid = os.getpid()
        job_tracker._save_state({
            "pid": pid,
            "create_time": _get_create_time(pid),
            "started_at": time.time(),
            "started_at_str": "2026-10-15 10:00:00",
            "notified": False,
        })
        self.assertIsNone(self.mgr.check_completion_for_notify())
        self.assertTrue(self.mgr.status()["running"])


if __name__ == "__main__":
    unittest.main()
