"""
tools/rd_dispatch.py 單元測試：RD 工程師派工與互相接手 (Failover)

使用者規則：寫程式任務未指定時優先給 Claude Code；Claude Code 沒反應或出錯 (例如 Token 用完)
由 Antigravity 接手，反之亦然。不實際呼叫 CLI，以 mock 驗證派工順序與接手行為。
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
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE, run_dev_task  # noqa: E402

# 派工入口 (run_claude_first / run_antigravity_first) 會自動開單：本模組一律擋下 GitHub 連線，
# 避免 .env 已設定 TICKETS_* 時測試在真實 repo 開單。
_no_github = patch.object(ticket_tools, "_send", side_effect=AssertionError("單元測試不得連線 GitHub"))


def setUpModule():
    _no_github.start()


def tearDownModule():
    _no_github.stop()

OK_AUTH = {"authenticated": True, "message": "ok"}
NO_AUTH = {"authenticated": False, "message": "尚未登入"}


def ok(text="完成"):
    return {"success": True, "stdout": text, "stderr": "", "returncode": 0}


def fail(error="Claude AI usage limit reached"):
    return {"success": False, "stdout": "", "stderr": "", "returncode": 1, "error": error}


class TestRunDevTask(unittest.TestCase):
    def _run(self, preferred, claude_result=None, agy_result=None, claude_auth=OK_AUTH, agy_auth=OK_AUTH):
        notes = []
        with patch.object(rd_dispatch, "check_claude_auth", return_value=claude_auth), \
             patch.object(rd_dispatch, "check_antigravity_auth", return_value=agy_auth), \
             patch.object(rd_dispatch, "run_claude_task", return_value=claude_result) as m_claude, \
             patch.object(rd_dispatch, "run_antigravity_task", return_value=agy_result) as m_agy:
            res = run_dev_task("寫一個爬蟲", preferred, notify=notes.append)
        return res, notes, m_claude, m_agy

    def test_default_prefers_claude(self):
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
             patch.object(rd_dispatch, "run_claude_task", return_value=ok()) as m_claude, \
             patch.object(rd_dispatch, "run_antigravity_task") as m_agy:
            res = run_dev_task("寫一個爬蟲")
        self.assertTrue(res["success"])
        self.assertEqual(res["engine"], CLAUDE)
        m_claude.assert_called_once()
        m_agy.assert_not_called()

    def test_claude_fails_agy_takes_over(self):
        res, notes, m_claude, m_agy = self._run(CLAUDE, claude_result=fail(), agy_result=ok("agy 完成"))
        self.assertTrue(res["success"])
        self.assertEqual(res["engine"], ANTIGRAVITY)
        self.assertEqual(res["engine_label"], "Antigravity")
        self.assertEqual(len(res["attempts"]), 1)
        self.assertEqual(res["attempts"][0]["engine"], CLAUDE)
        self.assertIn("usage limit", res["attempts"][0]["error"])
        # 接手者收到的任務描述附有接手說明
        handed_task = m_agy.call_args[0][0]
        self.assertIn("寫一個爬蟲", handed_task)
        self.assertIn("接手說明", handed_task)
        self.assertIn("Claude Code", handed_task)
        self.assertTrue(any("改由 **Antigravity** 接手" in n for n in notes))

    def test_agy_fails_claude_takes_over(self):
        res, notes, m_claude, m_agy = self._run(ANTIGRAVITY, claude_result=ok("claude 完成"), agy_result=fail("quota exceeded"))
        self.assertTrue(res["success"])
        self.assertEqual(res["engine"], CLAUDE)
        m_agy.assert_called_once()
        self.assertIn("接手說明", m_claude.call_args[0][0])

    def test_auth_not_ready_skips_without_running(self):
        res, notes, m_claude, m_agy = self._run(CLAUDE, agy_result=ok(), claude_auth=NO_AUTH)
        self.assertTrue(res["success"])
        self.assertEqual(res["engine"], ANTIGRAVITY)
        m_claude.assert_not_called()  # 未登入就不浪費時間執行
        # 認證問題不是任務失敗，接手者不需要接手說明 (沒有半成品)
        self.assertNotIn("接手說明", m_agy.call_args[0][0])

    def test_both_fail(self):
        res, notes, m_claude, m_agy = self._run(CLAUDE, claude_result=fail("A 掛了"), agy_result=fail("B 掛了"))
        self.assertFalse(res["success"])
        self.assertEqual(len(res["attempts"]), 2)
        self.assertIn("A 掛了", res["error"])
        self.assertIn("B 掛了", res["error"])

    def test_both_unavailable(self):
        res, notes, m_claude, m_agy = self._run(CLAUDE, claude_auth=NO_AUTH, agy_auth=NO_AUTH)
        self.assertFalse(res["success"])
        self.assertEqual(len(res["attempts"]), 2)
        m_claude.assert_not_called()
        m_agy.assert_not_called()

    def test_agy_model_passed_through(self):
        with patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
             patch.object(rd_dispatch, "run_antigravity_task", return_value=ok()) as m_agy:
            rd_dispatch.run_antigravity_first("任務", model="claude-sonnet-4-6")
        self.assertEqual(m_agy.call_args.kwargs["model"], "claude-sonnet-4-6")


class TestAgentWiring(unittest.TestCase):
    def test_tools_use_failover_dispatcher(self):
        import agent
        # 起改為非同步進入點 (背景跑 run_dev_task，一樣是 failover 派工)
        self.assertIs(agent.TOOL_REGISTRY["dispatch_claude_engineer_sync"].impl, rd_dispatch.start_claude_first)
        self.assertIs(agent.TOOL_REGISTRY["dispatch_antigravity_engineer_sync"].impl, rd_dispatch.start_antigravity_first)

    def test_system_instruction_pm_rule(self):
        import agent
        self.assertIn("你不寫程式", agent.SYSTEM_INSTRUCTION)
        self.assertIn("預設調用 `dispatch_claude_engineer_sync`", agent.SYSTEM_INSTRUCTION)

    def test_default_claude_model(self):
        from tools import claude_tools
        self.assertEqual(claude_tools.DEFAULT_CLAUDE_MODEL, "claude-sonnet-5-5")


if __name__ == "__main__":
    unittest.main()
