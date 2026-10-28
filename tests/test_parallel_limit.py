"""
 並行上限與引擎占用。不啟動 RD、不連網。

驗收標準：
    - RD_MAX_PARALLEL 設定同時執行上限，預設 2
    - rd_jobs.reserve(job, engine)：超過上限、引擎忙碌、同一張單已有執行中的任務時回傳拒絕理由；finish(job) 釋放
    - busy_engines()、running_tickets()；DevJob.engine 在換人時更新
    - 接手時避開忙碌中的引擎：另一位 RD 正在做別張單時不接手，回報失敗並說明
"""

import os
import sys
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch, rd_jobs  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}


def job(ticket=None):
    j = rd_jobs.DevJob(task="任務", engine_label="RD", scope="core", ticket=ticket)
    rd_jobs.register(j)
    return j


class TestReserve(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()
        self.env = patch.dict(os.environ, {"RD_MAX_PARALLEL": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        rd_jobs._reset_for_tests()

    def test_default_limit_is_two(self):
        self.assertEqual(rd_jobs.max_parallel(), 2)
        self.assertEqual(rd_jobs.MAX_PARALLEL, 2)
        with patch.dict(os.environ, {"RD_MAX_PARALLEL": "1"}):
            self.assertEqual(rd_jobs.max_parallel(), 1)
        with patch.dict(os.environ, {"RD_MAX_PARALLEL": "abc"}):
            self.assertEqual(rd_jobs.max_parallel(), 2)

    def test_reserve_and_release(self):
        a = job(75)
        self.assertIsNone(rd_jobs.reserve(a, CLAUDE))
        self.assertEqual(a.engine, CLAUDE)
        self.assertEqual(rd_jobs.busy_engines(), {CLAUDE})
        self.assertEqual(rd_jobs.running_tickets(), {75})
        rd_jobs.finish(a)
        self.assertEqual(rd_jobs.busy_engines(), set())
        self.assertEqual(rd_jobs.running_tickets(), set())

    def test_busy_engine_refused(self):
        a, b = job(75), job(77)
        rd_jobs.reserve(a, CLAUDE)
        reason = rd_jobs.reserve(b, CLAUDE)
        self.assertIn("#75", reason)
        self.assertIsNone(b.engine)
        self.assertIsNone(rd_jobs.reserve(b, ANTIGRAVITY))
        self.assertEqual(rd_jobs.busy_engines(), {CLAUDE, ANTIGRAVITY})

    def test_same_ticket_refused(self):
        a, b = job(75), job(75)
        rd_jobs.reserve(a, CLAUDE)
        reason = rd_jobs.reserve(b, ANTIGRAVITY)
        self.assertIn("#75", reason)
        self.assertIn("同一張單", reason)

    def test_limit_refused(self):
        with patch.dict(os.environ, {"RD_MAX_PARALLEL": "1"}):
            a, b = job(75), job(77)
            rd_jobs.reserve(a, CLAUDE)
            reason = rd_jobs.reserve(b, ANTIGRAVITY)
        self.assertIn("上限", reason)

    def test_switching_engine_releases_previous(self):
        a = job(75)
        rd_jobs.reserve(a, CLAUDE)
        self.assertIsNone(rd_jobs.reserve(a, ANTIGRAVITY))  # 接手換人：自己原本佔的不算衝突
        self.assertEqual(rd_jobs.busy_engines(), {ANTIGRAVITY})
        self.assertEqual(a.engine, ANTIGRAVITY)

    def test_jobs_without_ticket_do_not_conflict_on_ticket(self):
        a, b = job(None), job(None)
        self.assertIsNone(rd_jobs.reserve(a, CLAUDE))
        self.assertIsNone(rd_jobs.reserve(b, ANTIGRAVITY))

    def test_concurrent_reserve_only_one_wins(self):
        jobs = [job(100 + i) for i in range(8)]
        results = []
        barrier = threading.Barrier(len(jobs))

        def go(j):
            barrier.wait()
            results.append(rd_jobs.reserve(j, CLAUDE))

        threads = [threading.Thread(target=go, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(r is None for r in results), 1)


class TestFailoverAvoidsBusyEngine(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def tearDown(self):
        rd_jobs._reset_for_tests()

    def _run(self, preferred, claude_result=None, agy_result=None, ticket=0):
        notes = []
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", return_value=claude_result) as claude, \
                patch.object(rd_dispatch, "run_antigravity_task", return_value=agy_result) as agy, \
                patch.object(rd_dispatch, "collect_worktree_state", return_value={}), \
                patch.object(rd_dispatch.ticket_tools, "get_ticket",
                             return_value={"success": True, "ticket": {"number": ticket, "open_blockers": [], "body": ""}}), \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}), \
                patch("builtins.print"):
            res = rd_dispatch.run_dev_task("任務", preferred, ticket=ticket, opened_by="pm" if ticket else "",
                                           notify=notes.append)
        return res, claude, agy, notes

    def test_failed_engine_not_taken_over_by_busy_engine(self):
        other = job(77)
        rd_jobs.reserve(other, ANTIGRAVITY)  # agy 正在做
        failed = {"success": False, "error": "額度用完", "stdout": "", "stderr": ""}
        res, claude, agy, notes = self._run(CLAUDE, claude_result=failed, ticket=75)
        self.assertFalse(res["success"])
        agy.assert_not_called()
        self.assertIn("#77", res["error"])
        self.assertTrue(any("#77" in n for n in notes), notes)
        self.assertEqual(rd_jobs.busy_engines(), {ANTIGRAVITY})  # 本單結束後只剩 的佔用

    def test_busy_preferred_engine_goes_to_other(self):
        other = job(77)
        rd_jobs.reserve(other, CLAUDE)  # Claude 正在做
        res, claude, agy, notes = self._run(CLAUDE, agy_result={"success": True, "stdout": "完成"}, ticket=75)
        claude.assert_not_called()
        agy.assert_called_once()
        self.assertTrue(res["success"])
        self.assertTrue(any("#77" in n for n in notes), notes)

    def test_same_ticket_already_running_refused(self):
        other = job(75)
        rd_jobs.reserve(other, ANTIGRAVITY)
        res, claude, agy, _ = self._run(CLAUDE, claude_result={"success": True}, ticket=75)
        self.assertFalse(res["success"])
        self.assertIn("同一張單", res["error"])
        claude.assert_not_called()
        agy.assert_not_called()

    def test_engine_released_after_dispatch(self):
        self._run(CLAUDE, claude_result={"success": True, "stdout": "完成"})
        self.assertEqual(rd_jobs.busy_engines(), set())


if __name__ == "__main__":
    unittest.main()
