"""
 /claude、/agy 提到 #N 時自動帶入 ticket 內容並沿用該單。不啟動 RD、不連網。

驗收標準：
    - 斜線派工的需求文字含 #N 時，讀取該 ticket 內容附加到給 RD 的規格，並以該單號派工 (不另開新單)
    - 讀取失敗時照原文派工並提示
    - 經 Gemini 派工帶 ticket 參數時，同樣附上 ticket 內容
    - 只附 ticket 內文 (使用者故事、驗收標準、範圍外、相依)，限制長度
"""

import inspect
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch, rd_jobs  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}
BODY = "**使用者故事**\n想搜尋 ticket\n\n**驗收標準**\n- [ ] 可以用關鍵字搜尋\n\n**範圍外**\n無\n\n**相依**\n無"
_PATCHES = []


def setUpModule():
    from tools import ticket_tools
    _PATCHES.extend([patch.dict(os.environ, {"TICKETS_REPO": "", "TICKETS_GITHUB_TOKEN": ""}),
                     patch.object(ticket_tools, "_send", side_effect=AssertionError("測試不得連到 GitHub"))])
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()


def got(body=BODY, title="記下的想法可被搜尋"):
    return {"success": True, "ticket": {"number": 27, "title": title, "body": body}}


class TestExtractRef(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(rd_dispatch.extract_ticket_ref("做 #27 的修改"), 27)
        self.assertEqual(rd_dispatch.extract_ticket_ref("#27 和 #30"), 27)
        self.assertIsNone(rd_dispatch.extract_ticket_ref("寫一個匯率爬蟲"))
        self.assertIsNone(rd_dispatch.extract_ticket_ref("顏色 #fff"))

    def test_extract_next_to_chinese(self):
        """中文緊貼單號也要認得：Python 的 \\w 含中文字，「做」曾被當成沒有單號而自動開新單"""
        self.assertEqual(rd_dispatch.extract_ticket_ref("做#111"), 111)
        self.assertEqual(rd_dispatch.extract_ticket_ref("從project上執行#111"), 111)
        self.assertEqual(rd_dispatch.extract_ticket_ref("#27的修改"), 27)
        self.assertEqual(rd_dispatch.extract_ticket_ref("（#27）"), 27)

    def test_not_ticket_ref(self):
        """英數字緊貼的 # 不是單號：網址錨點、HTML 實體、色碼、超過 6 位數"""
        self.assertIsNone(rd_dispatch.extract_ticket_ref("page#12"))
        self.assertIsNone(rd_dispatch.extract_ticket_ref("&#123;"))
        self.assertIsNone(rd_dispatch.extract_ticket_ref("#12abc"))
        self.assertIsNone(rd_dispatch.extract_ticket_ref("#1234567"))


class TestTicketContext(unittest.TestCase):
    def test_context_appended(self):
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=got()):
            task = rd_dispatch.with_ticket_context("做 #27", 27, None)
        self.assertTrue(task.startswith("做 #27"))
        for phrase in ("#27", "記下的想法可被搜尋", "可以用關鍵字搜尋"):
            self.assertIn(phrase, task)

    def test_long_body_clipped(self):
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=got(body="很長" * 5000)):
            task = rd_dispatch.with_ticket_context("做 #27", 27, None)
        self.assertLess(len(task), rd_dispatch.TICKET_CONTEXT_CHARS + 300)

    def test_failure_keeps_original_and_notifies(self):
        notes = []
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value={"success": False, "error": "Not Found"}):
            task = rd_dispatch.with_ticket_context("做 #99", 99, notes.append)
        self.assertEqual(task, "做 #99")
        self.assertTrue(notes and "#99" in notes[0])


def got_with_comments(*bodies):
    res = got()
    res["ticket"]["comments"] = [{"author": "owner", "created_at": f"2026-10-19T0{i}:00:00Z", "body": b}
                                 for i, b in enumerate(bodies)]
    return res


class TestPlanCommentIncluded(unittest.TestCase):
    """：除了內文，也附上最新一則「📝 只讀規劃」留言"""

    def test_latest_plan_comment_appended(self):
        res = got_with_comments("📝 只讀規劃（Claude Code，未修改檔案）\n\n舊規劃 A",
                                "開工了",
                                "📝 只讀規劃（Claude Code，未修改檔案）\n\n新規劃 B：用 bigram 比對")
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=res):
            task = rd_dispatch.with_ticket_context("做 #27", 27, None)
        self.assertIn("新規劃 B", task)
        self.assertNotIn("舊規劃 A", task)
        self.assertIn("可以用關鍵字搜尋", task)              # 內文照常附上
        self.assertLess(task.index("可以用關鍵字搜尋"), task.index("新規劃 B"))

    def test_no_plan_comment_body_only(self):
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=got_with_comments("開工了")):
            task = rd_dispatch.with_ticket_context("做 #27", 27, None)
        self.assertNotIn("只讀規劃", task)

    def test_plan_comment_clipped(self):
        res = got_with_comments("📝 只讀規劃（Claude Code，未修改檔案）\n\n" + "規" * 10000)
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=res):
            task = rd_dispatch.with_ticket_context("做 #27", 27, None)
        self.assertLess(len(task), rd_dispatch.TICKET_CONTEXT_CHARS + rd_dispatch.PLAN_CONTEXT_CHARS + 400)
        self.assertIn(rd_dispatch.ticket_tools._TRUNCATED_MARK, task)
        self.assertIn("gh issue view 27 --comments", task)

    def test_plan_limit_matches_pm_limit(self):
        self.assertEqual(rd_dispatch.PLAN_CONTEXT_CHARS, rd_dispatch.ticket_tools.MAX_PLAN_CHARS)

    def test_six_thousand_char_plan_kept_whole(self):
        # 的規劃約 6,028 字，結尾的驗收標準不能被砍掉
        plan = "📝 只讀規劃（Claude Code，未修改檔案）\n\n" + "規" * 6000 + "\n驗收標準：結尾標記"
        res = got()
        res["ticket"]["latest_plan"] = plan
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=res):
            task = rd_dispatch.with_ticket_context("做 #27", 27, None)
        self.assertIn("驗收標準：結尾標記", task)
        self.assertNotIn(rd_dispatch.ticket_tools._TRUNCATED_MARK, task)
        self.assertNotIn("gh issue view", task)

    def test_latest_plan_already_marked_not_clipped_twice(self):
        # get_ticket 已截斷並加標記的 latest_plan：保留原標記、不再截一次，仍提示去 GitHub
        marked = rd_dispatch.ticket_tools._clip_marked("規" * 9000, rd_dispatch.ticket_tools.MAX_PLAN_CHARS)
        res = got()
        res["ticket"]["latest_plan"] = marked
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=res):
            task = rd_dispatch.with_ticket_context("做 #27", 27, None)
        self.assertIn(marked, task)
        self.assertEqual(task.count(rd_dispatch.ticket_tools._TRUNCATED_MARK), 1)
        self.assertIn("gh issue view 27 --comments", task)

    def test_plan_dispatch_does_not_feed_old_plan(self):
        res = got_with_comments("📝 只讀規劃（Claude Code，未修改檔案）\n\n舊規劃 A")
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=res):
            task = rd_dispatch.with_ticket_context("研究 #27", 27, None, include_plan=False)
        self.assertNotIn("舊規劃 A", task)

    def test_run_dev_task_plan_mode_excludes_old_plan(self):
        rd_jobs._reset_for_tests()
        res = got_with_comments("📝 只讀規劃（Claude Code，未修改檔案）\n\n舊規劃 A")
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", return_value={"success": True, "stdout": "新規格"}) as claude, \
                patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=res), \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}), \
                patch("builtins.print"):
            rd_dispatch.run_dev_task("研究 #27", rd_dispatch.CLAUDE, ticket=27, plan=True)
        self.assertNotIn("舊規劃 A", claude.call_args.args[0])
        self.assertIn("可以用關鍵字搜尋", claude.call_args.args[0])


class TestDispatchWithTicket(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def _run(self, **kwargs):
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", return_value={"success": True, "stdout": "ok"}) as claude, \
                patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=got()) as get, \
                patch.object(rd_dispatch.ticket_tools, "create_ticket") as create, \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}), \
                patch("builtins.print"):
            res = rd_dispatch.run_dev_task("做 #27", rd_dispatch.CLAUDE, opened_by="user", **kwargs)
        return res, claude, get, create

    def test_existing_ticket_enriches_task_and_reuses_number(self):
        res, claude, get, create = self._run(ticket=27)
        self.assertIn("可以用關鍵字搜尋", claude.call_args.args[0])
        create.assert_not_called()
        self.assertEqual(res["ticket"], 27)

    def test_resume_session_not_enriched(self):
        _, claude, get, _ = self._run(ticket=27, session_id="s1")
        get.assert_not_called()
        self.assertEqual(claude.call_args.args[0], "做 #27")

    def test_new_ticket_not_enriched(self):
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", return_value={"success": True, "stdout": "ok"}), \
                patch.object(rd_dispatch.ticket_tools, "get_ticket") as get, \
                patch.object(rd_dispatch.ticket_tools, "create_ticket", return_value={"success": True, "number": 60}), \
                patch("builtins.print"):
            rd_dispatch.run_dev_task("寫一個匯率爬蟲", rd_dispatch.CLAUDE, opened_by="user")
        get.assert_not_called()


class TestSlashCommand(unittest.TestCase):
    def test_slash_dispatch_uses_ticket_ref(self):
        import bot
        src = inspect.getsource(bot.run_slash_dev_task)
        self.assertIn("extract_ticket_ref(task)", src)
        self.assertIn("ticket=", src)


if __name__ == "__main__":
    unittest.main()
