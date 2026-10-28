"""
 RD 可自行使用 sub agent 平行處理 (層級 A)。不啟動 RD、不連網。

驗收標準：
    - build_pm_prompt 加入規則：互相獨立的子任務可用 sub agent 平行處理；有相依或改到同一批檔案的必須依序
    - 回報要列出開了哪些 sub agent、各自負責什麼；程式也從 Claude 的事件流記下 sub agent，
      引擎沒有事件可抓時 (Antigravity) 改解析回報文字
    - Claude Code 與 Antigravity 都適用；引擎不支援時照常依序執行，不算失敗
    - 安裝 / commit / push 守門對 sub agent 一樣有效：sub agent 的指令 (帶 parent_tool_use_id) 一樣會詢問使用者
"""

import io
import json
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import antigravity_tools, claude_tools, rd_common, rd_dispatch  # noqa: E402
from tools.rd_common import (SCOPE_CORE, SCOPE_TASK, build_pm_prompt, extract_subagents,  # noqa: E402
                             parse_subagent_report)
from tools.rd_dispatch import CLAUDE, ANTIGRAVITY, compact_dev_result, format_dev_report  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}


def agent_event(tool_id, description, subagent_type="general-purpose", name="Agent"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": name,
         "input": {"description": description, "subagent_type": subagent_type, "prompt": "細節"}}]}}


def bash_event(tool_id, command, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}]}}


class TestPrompt(unittest.TestCase):
    def test_core_and_task_prompts_include_subagent_rule(self):
        for scope in (SCOPE_CORE, SCOPE_TASK):
            prompt = build_pm_prompt("需求", scope, "INSTALL_RULE")
            for phrase in ("sub agent", "互相獨立", "依序", "同一批檔案", "不支援", "Sub agent："):
                self.assertIn(phrase, prompt, f"{scope} 缺少「{phrase}」")

    def test_rules_apply_to_subagents(self):
        prompt = build_pm_prompt("需求", SCOPE_CORE, "INSTALL_RULE")
        self.assertIn("sub agent 一樣適用", prompt)

    def test_both_engines_get_rule(self):
        self.assertIn("sub agent", antigravity_tools._build_pm_prompt("需求", SCOPE_CORE))
        self.assertIn("sub agent", build_pm_prompt("需求", SCOPE_CORE, claude_tools._INSTALL_RULE))


class TestExtractSubagents(unittest.TestCase):
    def test_agent_and_task_tool_detected(self):
        self.assertEqual(extract_subagents(agent_event("t1", "查 ticket 工具", "Explore")),
                         ["Explore：查 ticket 工具"])
        self.assertEqual(extract_subagents(agent_event("t2", "寫測試", name="Task")),
                         ["general-purpose：寫測試"])

    def test_other_events_ignored(self):
        self.assertEqual(extract_subagents(bash_event("t3", "ls")), [])
        self.assertEqual(extract_subagents({"type": "result", "result": "x"}), [])


class TestParseReport(unittest.TestCase):
    def test_bullets(self):
        text = "完成。\n\n**Sub agent：**\n- Explore：查 rd_common\n• general-purpose：寫測試\n\n其他"
        self.assertEqual(parse_subagent_report(text), ["Explore：查 rd_common", "general-purpose：寫測試"])

    def test_inline_none(self):
        self.assertEqual(parse_subagent_report("Sub agent：無（依序處理）"), [])

    def test_inline_single(self):
        self.assertEqual(parse_subagent_report("Sub agent: 1 個，查程式碼"), ["1 個，查程式碼"])

    def test_missing_section(self):
        self.assertEqual(parse_subagent_report("修改了 bot.py"), [])
        self.assertEqual(parse_subagent_report(""), [])


class TestClaudeStream(unittest.TestCase):
    def _run(self, events, approve):
        class Proc:
            pid = 1
            returncode = None

            def __init__(self, cmd, **kw):
                self.stdin = io.StringIO()
                self.stdout = iter(json.dumps(e) + "\n" for e in events)

            def wait(self):
                self.returncode = 0

        with patch.object(claude_tools, "_resolve_claude_executable", return_value="claude"), \
                patch.object(claude_tools.subprocess, "Popen", Proc), \
                patch.object(claude_tools, "report_new_packages") as rnp:
            rnp.return_value.__enter__ = lambda s: None
            rnp.return_value.__exit__ = lambda s, *a: False
            return claude_tools.run_claude_task("需求", approve=approve, scope=SCOPE_TASK)

    def test_subagents_recorded(self):
        res = self._run([agent_event("a1", "查程式碼", "Explore"), agent_event("a2", "寫測試"),
                         {"type": "result", "result": "完成", "is_error": False}], approve=None)
        self.assertTrue(res["success"])
        self.assertEqual(res["subagents"], ["Explore：查程式碼", "general-purpose：寫測試"])

    def test_subagent_install_still_gated(self):
        asked = []

        def approve(title, detail):
            asked.append(detail)
            return False

        res = self._run([agent_event("a1", "裝套件"),
                         bash_event("b1", "pip install requests", parent="a1"),
                         {"type": "result", "result": "完成", "is_error": False}], approve)
        self.assertEqual(len(asked), 1)
        self.assertIn("pip install requests", asked[0])
        self.assertEqual(res["blocked_commands"], ["pip install requests"])


class TestDispatchAndReport(unittest.TestCase):
    def _dispatch(self, preferred, result):
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", return_value=result), \
                patch.object(rd_dispatch, "run_antigravity_task", return_value=result), \
                patch("builtins.print"):
            return rd_dispatch.run_dev_task("任務", preferred)

    def test_engine_detected_subagents_kept(self):
        res = self._dispatch(CLAUDE, {"success": True, "stdout": "Sub agent：無", "subagents": ["Explore：查"]})
        self.assertEqual(res["subagents"], ["Explore：查"])

    def test_agy_falls_back_to_report_text(self):
        res = self._dispatch(ANTIGRAVITY, {"success": True, "stdout": "完成\nSub agent：\n- 寫測試\n- 改 bot.py"})
        self.assertEqual(res["subagents"], ["寫測試", "改 bot.py"])

    def test_no_subagent_is_not_failure(self):
        res = self._dispatch(ANTIGRAVITY, {"success": True, "stdout": "完成\nSub agent：無"})
        self.assertTrue(res["success"])
        self.assertEqual(res["subagents"], [])

    def test_reports_show_subagents(self):
        res = {"success": True, "engine_label": "Claude Code", "stdout": "完成",
               "subagents": ["Explore：查程式碼", "general-purpose：寫測試"]}
        report = format_dev_report(res)
        self.assertIn("Sub agent", report)
        self.assertIn("Explore：查程式碼", report)
        self.assertEqual(compact_dev_result(res)["subagents"], ["Explore：查程式碼", "general-purpose：寫測試"])

    def test_report_omits_section_without_subagents(self):
        res = {"success": True, "engine_label": "Claude Code", "stdout": "完成", "subagents": []}
        self.assertNotIn("Sub agent", format_dev_report(res))


if __name__ == "__main__":
    unittest.main()
