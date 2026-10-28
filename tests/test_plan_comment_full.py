"""
只讀規劃留言不可無聲截斷。不啟動 RD、不連網 (mock ticket 呼叫)。

背景：_comment_plan 曾以 _clip(stdout, 6000) 截斷規劃，結尾只剩「…」。
 的規劃長 6,028 字，在「未決問題 1」中途被截斷，未決問題全部遺失。

驗收標準：
    - 一般長度 (含 這種 6,000 多字) 的規劃完整留言，未決問題都在
    - 整則留言不超過 GitHub 留言上限 65,536 字
    - 真的超過上限時才截斷，並帶 ticket_tools._TRUNCATED_MARK 標記原長度、通知使用者
    - 留言仍以 PLAN_COMMENT_PREFIX 開頭 (get_ticket 靠它找最新規劃)
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch, ticket_tools  # noqa: E402

GITHUB_COMMENT_LIMIT = 65536
OPEN_QUESTIONS = "## 未決問題\n1. 是否安裝 mcp 套件？\n6. 工具名稱前綴怎麼定？"


def _comment(stdout, subagents=None):
    notes = []
    result = {"success": True, "engine_label": "Claude Code", "stdout": stdout,
              "subagents": subagents if subagents is not None else ["Explore：讀 agent.py"] * 8}
    with patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}) as update:
        rd_dispatch._comment_plan(result, 91, notes.append)
    update.assert_called_once()
    return update.call_args.kwargs["comment"], notes


class TestPlanCommentNotClipped(unittest.TestCase):
    def test_issue_91_length_plan_keeps_open_questions(self):
        plan = "## 規格\n" + "內容" * 3000 + "\n" + OPEN_QUESTIONS  # 6,000 多字，舊版會截掉未決問題
        comment, notes = _comment(plan)
        self.assertIn(plan, comment)
        self.assertIn("6. 工具名稱前綴怎麼定？", comment)
        self.assertNotIn(ticket_tools._TRUNCATED_MARK, comment)
        self.assertEqual(notes, [])
        self.assertTrue(comment.startswith(rd_dispatch.PLAN_COMMENT_PREFIX))

    def test_plan_at_limit_is_kept_whole_within_github_limit(self):
        plan = "字" * rd_dispatch.PLAN_COMMENT_CHARS
        comment, notes = _comment(plan)
        self.assertIn(plan, comment)
        self.assertNotIn(ticket_tools._TRUNCATED_MARK, comment)
        self.assertLessEqual(len(comment), GITHUB_COMMENT_LIMIT)
        self.assertEqual(notes, [])

    def test_oversized_plan_is_marked_and_user_notified(self):
        plan = "字" * (rd_dispatch.PLAN_COMMENT_CHARS + 5000)
        comment, notes = _comment(plan)
        self.assertLessEqual(len(comment), GITHUB_COMMENT_LIMIT)
        self.assertIn(f"{ticket_tools._TRUNCATED_MARK} {len(plan)} 字", comment)
        self.assertIn("🧩 Sub agent", comment)  # 截斷後 sub agent 紀錄仍在
        self.assertEqual(len(notes), 1)
        self.assertIn("#91", notes[0])
        self.assertIn(str(len(plan)), notes[0])

    def test_marked_plan_still_found_as_latest_plan(self):
        # 截斷標記不影響 get_ticket 以開頭辨識規劃留言
        comment, _ = _comment("字" * (rd_dispatch.PLAN_COMMENT_CHARS + 1))
        self.assertTrue(comment.startswith(ticket_tools.PLAN_COMMENT_PREFIX))


if __name__ == "__main__":
    unittest.main()
