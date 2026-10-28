"""
 只讀規劃回報 sub agent 使用情況。不啟動 RD、不連網。

驗收標準：
    - 只讀規劃的 prompt 加入 sub agent 規則：研究可以開 sub agent 平行，sub agent 一樣只讀；回覆最後附「Sub agent：」段落
    - 規劃回報 (Discord) 顯示系統偵測到的 sub agent
    - 規劃留言到 ticket 時一律記錄 sub agent (沒有就寫「無」)
    - 給 PM 的精簡結果有 sub agent 時帶上
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
from tools.rd_common import SCOPE_CORE, SCOPE_TASK, build_plan_prompt, parse_subagent_report  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}
PLAN = "## 目標\n導入 MCP\n## 未決問題\n無\n\nSub agent：\n- Explore：讀 agent.py 工具註冊\n- Explore：讀 gemini_cache"


class TestPlanPrompt(unittest.TestCase):
    def test_plan_prompt_has_read_only_subagent_rule(self):
        for scope in (SCOPE_CORE, SCOPE_TASK):
            text = build_plan_prompt("研究 MCP", scope)
            for phrase in ("sub agent", "平行", "一樣只讀", "Sub agent：", "不支援"):
                self.assertIn(phrase, text, f"{scope} 缺少「{phrase}」")

    def test_plan_prompt_still_read_only(self):
        text = build_plan_prompt("研究 MCP", SCOPE_CORE)
        self.assertIn("不得修改", text)
        self.assertIn("未決問題", text)

    def test_report_section_parses(self):
        self.assertEqual(parse_subagent_report(PLAN), ["Explore：讀 agent.py 工具註冊", "Explore：讀 gemini_cache"])


class TestPlanReports(unittest.TestCase):
    def _res(self, **extra):
        return {"success": True, "plan": True, "engine_label": "Claude Code", "ticket": 91, "stdout": "規格",
                "session_id": "s-1", **extra}

    def test_discord_report_shows_detected_subagents(self):
        text = rd_dispatch.format_dev_report(self._res(subagents=["Explore：讀 agent.py", "Explore：讀 cache"]))
        self.assertIn("Sub agent", text)
        self.assertIn("Explore：讀 agent.py", text)

    def test_discord_report_without_subagents_has_no_section(self):
        self.assertNotIn("🧩", rd_dispatch.format_dev_report(self._res(subagents=[])))

    def test_pm_view_carries_subagents(self):
        view = rd_dispatch.compact_dev_result(self._res(subagents=["Explore：讀 agent.py"]))
        self.assertEqual(view["subagents"], ["Explore：讀 agent.py"])
        self.assertNotIn("subagents", rd_dispatch.compact_dev_result(self._res(subagents=[])))


class TestPlanCommentRecordsSubagents(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def tearDown(self):
        rd_jobs._reset_for_tests()

    def _plan(self, engine, result):
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", return_value=result), \
                patch.object(rd_dispatch, "run_antigravity_task", return_value=result), \
                patch.object(rd_dispatch, "_prepare_worktree",
                             return_value=({"path": "fake-worktrees/ticket-91", "branch": "ticket-91-x"}, None)), \
                patch.object(rd_dispatch.ticket_tools, "get_ticket",
                             return_value={"success": True, "ticket": {"number": 91, "body": "x", "open_blockers": []}}), \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}) as update, \
                patch("builtins.print"):
            rd_dispatch.run_dev_task("研究 #91", engine, scope="core", ticket=91, plan=True)
        return update.call_args.kwargs["comment"]

    def test_comment_lists_detected_subagents(self):
        comment = self._plan(CLAUDE, {"success": True, "stdout": "規格", "subagents": ["Explore：讀 agent.py"]})
        self.assertIn("🧩 Sub agent：1 個", comment)
        self.assertIn("Explore：讀 agent.py", comment)

    def test_comment_records_none(self):
        comment = self._plan(CLAUDE, {"success": True, "stdout": "規格", "subagents": []})
        self.assertIn("🧩 Sub agent：無", comment)

    def test_agy_plan_falls_back_to_report_text(self):
        comment = self._plan(ANTIGRAVITY, {"success": True, "stdout": PLAN})
        self.assertIn("🧩 Sub agent：2 個", comment)
        self.assertIn("讀 gemini_cache", comment)


if __name__ == "__main__":
    unittest.main()
