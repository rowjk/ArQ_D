"""
 PM 平行派工規則。mock GitHub，不啟動 RD、不連網。

驗收標準：
    - 規則：只有沒有相依、不改同一批檔案的子單才平行；派工前向使用者說明依據
    - 程式檢查：要派的單與執行中的單互為 blocked_by（或同一母單的相依鏈）時拒絕平行派工並說明
    - 系統提示詞維持在 2,500 字元內（細節放 ticket get 的規則）
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
from tools import ticket_tools as tt  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE  # noqa: E402
from tests.test_ticket_tools import FakeGitHub, issue, run_with  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}


def ref(n, state="open"):
    return {"number": n, "title": f"票 {n}", "state": state, "status": "todo"}


def graph(edges, closed=()):
    """edges: [(擋住者, 被擋者)] → get_ticket 的假實作 (只回傳相依欄位)"""
    numbers = {n for e in edges for n in e}

    def get(n):
        n = int(n)
        if n not in numbers and n < 1000:
            numbers.add(n)
        return {"success": True, "ticket": {
            "number": n, "body": "", "open_blockers": [],
            "blocked_by": [ref(a, "closed" if a in closed else "open") for a, b in edges if b == n],
            "blocking": [ref(b, "closed" if b in closed else "open") for a, b in edges if a == n]}}
    return get


def running(*tickets):
    for i, t in enumerate(tickets):
        j = rd_jobs.DevJob(task="x", engine_label="RD", scope="core", ticket=t)
        rd_jobs.register(j)
        rd_jobs.reserve(j, (CLAUDE, ANTIGRAVITY)[i % 2])


class TestDependencyConflict(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def tearDown(self):
        rd_jobs._reset_for_tests()

    def test_nothing_running_needs_no_lookup(self):
        with patch.object(rd_dispatch.ticket_tools, "get_ticket") as get:
            self.assertIsNone(rd_dispatch.dependency_conflict(78))
        get.assert_not_called()

    def test_unrelated_running_ticket_ok(self):
        running(90)
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=graph([(75, 76), (76, 78)])):
            self.assertIsNone(rd_dispatch.dependency_conflict(77))

    def test_running_ticket_that_depends_on_this_one(self):
        running(79)
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=graph([(78, 79)])):
            reason = rd_dispatch.dependency_conflict(78)
        self.assertIn("#79", reason)
        self.assertIn("#78 → #79", reason)

    def test_chain_through_sibling(self):
        # 執行中； 被 擋住、 被 擋住 → 與 在同一條相依鏈上
        running(76)
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=graph([(76, 77), (77, 78)])):
            reason = rd_dispatch.dependency_conflict(78)
        self.assertIn("#76", reason)
        self.assertIn("#78 → #77 → #76", reason)

    def test_lookup_bounded(self):
        running(999)
        chain = [(n, n + 1) for n in range(100, 200)]
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=graph(chain)) as get:
            self.assertIsNone(rd_dispatch.dependency_conflict(100))
        self.assertLessEqual(get.call_count, rd_dispatch.MAX_DEPENDENCY_LOOKUPS)

    def test_unreadable_ticket_does_not_block(self):
        running(79)
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value={"success": False, "error": "x"}):
            self.assertIsNone(rd_dispatch.dependency_conflict(78))


class TestDispatchRefusesDependentParallel(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()
        self.patches = [
            patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH),
            patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH),
            patch.object(rd_dispatch, "collect_worktree_state", return_value={}),
            patch.object(rd_dispatch, "collect_acceptance", return_value={"tests_passed": True}),
            patch.object(rd_dispatch, "_prepare_worktree",
                         return_value=({"path": "fake-worktrees/ticket-78", "branch": "ticket-78-x"}, None)),
            patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}),
            patch("builtins.print"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        rd_jobs._reset_for_tests()

    def test_async_dispatch_refused_with_reason(self):
        running(76)
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=graph([(76, 77), (77, 78)])), \
                patch.object(rd_dispatch, "run_claude_task") as claude:
            res = rd_dispatch.start_dev_task("做 #78", ANTIGRAVITY, ticket=78, opened_by="pm", on_done=lambda r: None)
        self.assertFalse(res["accepted"])
        self.assertIn("相依", res["error"])
        claude.assert_not_called()
        self.assertEqual(rd_jobs.running_tickets(), {76})

    def test_async_independent_ticket_accepted(self):
        running(76)
        done = threading.Event()
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=graph([(76, 79), (77, 79)])), \
                patch.object(rd_dispatch, "run_antigravity_task", return_value={"success": True, "stdout": "完成"}):
            res = rd_dispatch.start_dev_task("做 #77", CLAUDE, ticket=77, opened_by="pm", on_done=lambda r: done.set())
            self.assertTrue(res["accepted"], res)
            self.assertTrue(done.wait(5))

    def test_sync_dispatch_refused_too(self):
        running(76)
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=graph([(76, 78)])), \
                patch.object(rd_dispatch, "run_claude_task") as claude:
            res = rd_dispatch.run_dev_task("做 #78", ANTIGRAVITY, ticket=78, opened_by="user", scope="core")
        self.assertFalse(res["success"])
        self.assertIn("相依", res["error"])
        claude.assert_not_called()


class TestParallelRulesForPM(unittest.TestCase):
    P = "/repos/owner/repo"

    def test_sub_ticket_get_includes_parallel_rules(self):
        child = issue(78, parent_issue_url=f"https://api.github.com{self.P}/issues/34")
        fake = FakeGitHub({
            ("GET", f"{self.P}/issues/78/comments"): (200, []),
            ("GET", f"{self.P}/issues/78/parent"): (200, issue(34)),
            ("GET", f"{self.P}/issues/78"): (200, child),
        })
        rules = run_with(fake, tt.get_ticket, 78)["ticket"]["parallel_rules"]
        for phrase in ("沒有相依", "同一批檔案", "說明依據", "上限"):
            self.assertIn(phrase, rules)

    def test_plain_ticket_has_no_parallel_rules(self):
        fake = FakeGitHub({("GET", f"{self.P}/issues/7/comments"): (200, []),
                           ("GET", f"{self.P}/issues/7"): (200, issue(7))})
        self.assertNotIn("parallel_rules", run_with(fake, tt.get_ticket, 7)["ticket"])

    def test_system_instruction_mentions_basis_within_budget(self):
        import agent
        self.assertIn("說明依據", agent.SYSTEM_INSTRUCTION)
        self.assertLess(len(agent.SYSTEM_INSTRUCTION), 2500)


if __name__ == "__main__":
    unittest.main()
