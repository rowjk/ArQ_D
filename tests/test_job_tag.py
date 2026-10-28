"""
 通知標示單號與 RD。不啟動 RD、不連網。

驗收標準：
    - rd_common.job_tag(ticket, engine_label) 產生「#77｜Claude Code」(沒有單號時只有 RD 名稱)
    - 同意卡標題、RD 提問、完成 / 失敗 / 停止通知、派工訊息都加上前綴
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch, rd_jobs  # noqa: E402
from tools.rd_common import job_tag  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}
FAKE_WT = ({"path": "fake-worktrees/ticket-77", "branch": "ticket-77-x", "created": False}, None)


class TestJobTag(unittest.TestCase):
    def test_format(self):
        self.assertEqual(job_tag(77, "Claude Code"), "#77｜Claude Code")
        self.assertEqual(job_tag(None, "Antigravity"), "Antigravity")
        self.assertEqual(job_tag(0, "Claude Code"), "Claude Code")
        self.assertEqual(job_tag(77, ""), "#77｜RD 工程師")


class TestDispatchNotificationsTagged(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def tearDown(self):
        rd_jobs._reset_for_tests()

    def _run(self, claude, agy=None, ticket=77, scope="core"):
        notes, cards = [], []

        def approve(title, detail):
            cards.append(title)
            return True

        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", side_effect=claude), \
                patch.object(rd_dispatch, "run_antigravity_task", side_effect=agy or (lambda *a, **k: {"success": True, "stdout": "完成"})), \
                patch.object(rd_dispatch, "collect_acceptance", return_value={"tests_passed": True}), \
                patch.object(rd_dispatch, "collect_worktree_state", return_value={}), \
                patch.object(rd_dispatch, "_prepare_worktree", return_value=FAKE_WT), \
                patch.object(rd_dispatch.ticket_tools, "get_ticket",
                             return_value={"success": True, "ticket": {"number": ticket, "open_blockers": [], "body": ""}}), \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}), \
                patch("builtins.print"):
            res = rd_dispatch.run_dev_task("做 #77", CLAUDE, scope=scope, ticket=ticket, opened_by="pm",
                                           notify=notes.append, approve=approve)
        return res, notes, cards

    def test_engine_approval_card_and_notify_tagged(self):
        def claude(task, notify=None, approve=None, **kw):
            notify("📦 偵測到套件變動")
            approve("讓 Claude Code 執行：安裝套件", "pip install x")
            return {"success": True, "stdout": "完成"}

        _, notes, cards = self._run(claude)
        self.assertTrue(cards and cards[0].startswith("【#77｜Claude Code】"), cards)
        self.assertIn("【#77｜Claude Code】📦 偵測到套件變動", notes)

    def test_failover_notice_uses_new_engine_after_switch(self):
        def claude(task, **kw):
            return {"success": False, "error": "額度用完", "stdout": "", "stderr": ""}

        def agy(task, notify=None, approve=None, **kw):
            approve("讓 Antigravity 執行：刪除檔案", "rm x")
            return {"success": True, "stdout": "完成"}

        _, notes, cards = self._run(claude, agy)
        self.assertTrue(any(n.startswith("【#77｜Claude Code】") and "接手" in n for n in notes), notes)
        self.assertTrue(cards[0].startswith("【#77｜Antigravity】"), cards)


class TestReportsTagged(unittest.TestCase):
    def test_success_report(self):
        text = rd_dispatch.format_dev_report({"success": True, "engine_label": "Claude Code", "ticket": 77, "stdout": "完成"})
        self.assertIn("#77｜Claude Code", text.splitlines()[0])

    def test_failure_report(self):
        text = rd_dispatch.format_dev_report({"success": False, "engine_label": "Antigravity", "ticket": 77, "error": "x"})
        self.assertIn("#77｜Antigravity", text.splitlines()[0])

    def test_question_in_report_is_tagged(self):
        # RD 提問寫在回報裡：回報第一行標明單號與 RD，使用者知道是誰在問
        text = rd_dispatch.format_dev_report({"success": True, "engine_label": "Claude Code", "ticket": 75,
                                              "stdout": "完成。\n問題：1. 要不要改預設值？"})
        self.assertIn("#75｜Claude Code", text.splitlines()[0])

    def test_plan_report(self):
        text = rd_dispatch.format_dev_report({"success": True, "plan": True, "engine_label": "Claude Code",
                                              "ticket": 77, "stdout": "規格"})
        self.assertIn("#77｜Claude Code", text.splitlines()[0])

    def test_cancel_report(self):
        text = rd_dispatch.format_dev_report({"cancelled": True, "engine_label": "Claude Code", "ticket": 77})
        self.assertIn("#77｜Claude Code", text.splitlines()[0])

    def test_cancel_report_names_worktree(self):
        text = rd_dispatch.format_dev_report({"cancelled": True, "engine_label": "Claude Code", "ticket": 77,
                                              "worktree": {"diff_stat": " a.py | 1 +", "status": ""},
                                              "worktree_info": {"path": "fake-worktrees/ticket-77", "branch": "b"}})
        self.assertIn("fake-worktrees/ticket-77", text)
        self.assertNotIn("ArQ 工作目錄目前的改動", text)

    def test_no_ticket_shows_engine_only(self):
        text = rd_dispatch.format_dev_report({"success": True, "engine_label": "Claude Code", "stdout": "完成"})
        self.assertIn("Claude Code", text.splitlines()[0])
        self.assertNotIn("#", text.splitlines()[0])


class TestAnnouncement(unittest.TestCase):
    def test_bot_dispatch_message_tagged(self):
        import bot
        job = rd_jobs.DevJob(task="做 #77", engine_label="Claude Code", scope="core", ticket=77)
        self.assertIn("#77｜Claude Code", bot.job_announcement(job))

    def test_known_ticket_set_before_announce(self):
        rd_jobs._reset_for_tests()
        seen = []
        rd_jobs.set_announcer(lambda job: seen.append(job.ticket))
        try:
            with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                    patch.object(rd_dispatch, "run_claude_task", return_value={"success": True, "stdout": "完成"}), \
                    patch.object(rd_dispatch, "collect_worktree_state", return_value={}), \
                    patch.object(rd_dispatch.ticket_tools, "get_ticket",
                                 return_value={"success": True, "ticket": {"number": 77, "open_blockers": [], "body": ""}}), \
                    patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}), \
                    patch("builtins.print"):
                rd_dispatch.run_dev_task("做 #77", CLAUDE, ticket=77, opened_by="pm")
        finally:
            rd_jobs._reset_for_tests()
        self.assertEqual(seen, [77])

    def test_slash_command_job_carries_ticket(self):
        # /claude、/agy 先建立 DevJob 再送出派工訊息：需求含 #N 時 job 要帶單號，訊息才標得出來
        import inspect
        import re
        import bot
        calls = re.findall(r"rd_jobs\.DevJob\((.*?)\)\n", inspect.getsource(bot), re.DOTALL)
        self.assertTrue(calls)
        self.assertTrue(all("ticket=" in c for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
