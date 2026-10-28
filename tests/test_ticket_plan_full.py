"""
 ticket get 回傳完整的最新規劃，截斷時明確標示 (PM 不再到處抓留言)。不連網。

事件： 的規劃留言 4,697 字，get_ticket 把每則留言無聲截斷在 4,000 字 (切在未決問題中間)，
PM 以為內容不完整，改用 gh / call_api / Python 抓留言 (7 次呼叫、58,784 Token)。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tests.test_ticket_tools import FakeGitHub, issue, run_with  # noqa: E402
from tools import rd_dispatch  # noqa: E402
from tools import ticket_tools as tt  # noqa: E402

PLAN_OLD = tt.PLAN_COMMENT_PREFIX + "（Claude Code，未修改檔案）\n\n舊規劃"
PLAN_NEW = tt.PLAN_COMMENT_PREFIX + "（Claude Code，未修改檔案）\n\n# 規格\n" + "內容" * 2300 + "\n## 未決問題\n1. force_new 要不要跳卡？"


def fake(comments, body="**使用者故事**\n想搜尋"):
    return FakeGitHub({
        ("GET", "/repos/owner/repo/issues/27/comments"): (200, [
            {"user": {"login": "owner"}, "created_at": f"2026-10-19T0{i}:00:00Z", "body": b} for i, b in enumerate(comments)]),
        ("GET", "/repos/owner/repo/issues/27"): (200, issue(27, body=body)),
    })


class TestLatestPlan(unittest.TestCase):
    def test_latest_plan_returned_in_full(self):
        res = run_with(fake([PLAN_OLD, "開工了", PLAN_NEW]), tt.get_ticket, 27)
        t = res["ticket"]
        self.assertEqual(t["latest_plan"], PLAN_NEW)                 # 完整，不截斷 (4,600+ 字)
        self.assertIn("force_new 要不要跳卡", t["latest_plan"])
        self.assertTrue(all(not c["body"].startswith(tt.PLAN_COMMENT_PREFIX) for c in t["comments"]))
        self.assertEqual([c["body"] for c in t["comments"]], ["開工了"])

    def test_no_plan(self):
        res = run_with(fake(["開工了"]), tt.get_ticket, 27)
        self.assertIsNone(res["ticket"]["latest_plan"])

    def test_truncation_is_marked(self):
        long_comment = "討論" * 2000
        res = run_with(fake([long_comment], body="內文" * 3000), tt.get_ticket, 27)
        t = res["ticket"]
        self.assertIn("已截斷", t["comments"][0]["body"])
        self.assertIn(str(len(long_comment)), t["comments"][0]["body"])
        self.assertLessEqual(len(t["comments"][0]["body"]), tt.MAX_COMMENT_CHARS + 60)
        self.assertIn("已截斷", t["body"])

    def test_short_text_not_marked(self):
        res = run_with(fake(["開工了"]), tt.get_ticket, 27)
        self.assertEqual(res["ticket"]["comments"][0]["body"], "開工了")


class TestTicketViewShowsPlan(unittest.TestCase):
    def test_view_embed_has_plan_excerpt(self):
        import bot
        t = {"number": 27, "title": "t", "url": "", "body": "內文", "status": "todo", "priority": "p2",
             "opened_by": "rd-claude", "opener_mismatch": False, "state": "open", "comments": [],
             "latest_plan": PLAN_NEW, "metadata": {}}
        embed = bot.build_ticket_view_embed({"success": True, "ticket": t})
        field = next(f for f in embed.fields if "規劃" in f.name)
        self.assertLessEqual(len(field.value), 1024)
        self.assertIn("# 規格", field.value)


class TestDispatchUsesLatestPlan(unittest.TestCase):
    def test_with_ticket_context_reads_latest_plan_field(self):
        got = {"success": True, "ticket": {"number": 27, "title": "t", "body": "內文", "comments": [],
                                            "latest_plan": PLAN_NEW}}
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=got):
            task = rd_dispatch.with_ticket_context("做 #27", 27, None)
        self.assertIn("# 規格", task)


if __name__ == "__main__":
    unittest.main()
