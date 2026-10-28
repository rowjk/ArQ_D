"""
核心修改 (scope=core)、接續原工程師對話 (session_id) 與人工 commit 閘門的單元測試

使用者規則：
    - RD 可以修改 ArQ 本身，但經 Gemini 派工需先同意；完成後附系統實際產生的驗收佐證。
    - 工程師提問經使用者裁決後，交回原工程師接續原對話。
    - 只有使用者確認後才能 commit：git commit 列入風險規則。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_common, rd_dispatch, ticket_tools  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE, run_dev_task  # noqa: E402
from tools.risk_policy import classify_risk  # noqa: E402

# 派工入口 (run_claude_first / run_antigravity_first) 會自動開單：本模組一律擋下 GitHub 連線，
# 避免 .env 已設定 TICKETS_* 時測試在真實 repo 開單。
_no_github = patch.object(ticket_tools, "_send", side_effect=AssertionError("單元測試不得連線 GitHub"))
# scope=core 會在該單的 worktree 開發；這裡測的是派工 / 驗收 / 停止流程，用假的 worktree
_fake_worktree = patch.object(rd_dispatch, "_prepare_worktree", return_value=({"path": "fake-worktrees/ticket-1", "branch": "ticket-1-test", "created": False}, None))


def setUpModule():
    _no_github.start()
    _fake_worktree.start()


def tearDownModule():
    _fake_worktree.stop()
    _no_github.stop()

OK_AUTH = {"authenticated": True, "message": "ok"}
ACCEPTANCE = {"diff_stat": " bot.py | 2 +-", "status": " M bot.py", "tests_passed": True, "tests_summary": "Ran 3 tests\nOK"}


def ok(**extra):
    return {"success": True, "stdout": "完成", "stderr": "", "returncode": 0, **extra}


class TestScopeAndPrompts(unittest.TestCase):
    def test_workspace(self):
        self.assertEqual(rd_common.workspace_for("core"), rd_common.PROJECT_ROOT)
        self.assertEqual(rd_common.workspace_for("task"), os.path.join(rd_common.PROJECT_ROOT, "task"))

    def test_core_prompt(self):
        prompt = rd_common.build_pm_prompt("改 bot.py", "core", "INSTALL_RULE")
        self.assertIn("GEMINI.md", prompt)
        self.assertIn("unittest discover", prompt)
        self.assertIn("不得執行 git commit", prompt)
        self.assertIn("INSTALL_RULE", prompt)

    def test_task_prompt(self):
        prompt = rd_common.build_pm_prompt("寫爬蟲", "task", "INSTALL_RULE")
        self.assertIn("artifact/", prompt)
        self.assertIn("TDD", prompt)

    def test_invalid_scope_rejected(self):
        from tools.claude_tools import run_claude_task
        from tools.antigravity_tools import run_antigravity_task
        self.assertFalse(run_claude_task("x", scope="/etc")["success"])
        self.assertFalse(run_antigravity_task("x", scope="..")["success"])


class TestDispatch(unittest.TestCase):
    def _run(self, preferred=CLAUDE, claude_result=None, agy_result=None, **kwargs):
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
             patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
             patch.object(rd_dispatch, "run_claude_task", return_value=claude_result) as m_claude, \
             patch.object(rd_dispatch, "run_antigravity_task", return_value=agy_result) as m_agy, \
             patch.object(rd_dispatch, "collect_acceptance", return_value=ACCEPTANCE) as m_acc:
            res = run_dev_task("任務", preferred, **kwargs)
        return res, m_claude, m_agy, m_acc

    def test_core_attaches_acceptance(self):
        res, m_claude, _, m_acc = self._run(claude_result=ok(), scope="core")
        self.assertEqual(res["acceptance"], ACCEPTANCE)
        self.assertEqual(m_claude.call_args.kwargs["scope"], "core")
        m_acc.assert_called_once()

    def test_task_has_no_acceptance(self):
        res, _, _, m_acc = self._run(claude_result=ok())
        self.assertNotIn("acceptance", res)
        m_acc.assert_not_called()

    def test_session_id_resumes_original_engineer(self):
        res, m_claude, m_agy, _ = self._run(ANTIGRAVITY, agy_result=ok(session_id="conv-9"), session_id="conv-9")
        self.assertEqual(m_agy.call_args.kwargs["session_id"], "conv-9")
        self.assertEqual(res["session_id"], "conv-9")
        m_claude.assert_not_called()

    def test_failover_starts_fresh_session(self):
        failed = {"success": False, "error": "quota", "stdout": "", "stderr": "", "returncode": 1}
        res, m_claude, m_agy, _ = self._run(CLAUDE, claude_result=failed, agy_result=ok(), session_id="sess-1")
        self.assertEqual(m_claude.call_args.kwargs["session_id"], "sess-1")
        self.assertEqual(m_agy.call_args.kwargs["session_id"], "")  # 接手者是全新對話
        self.assertEqual(res["engine"], ANTIGRAVITY)


class TestGates(unittest.TestCase):
    def test_git_commit_requires_consent(self):
        for cmd in ["git commit -m wip", "git add -A && git commit -am x", "git push origin main"]:
            with self.subTest(cmd=cmd):
                self.assertIn("Git", classify_risk(cmd, "shell") or "")
        self.assertIsNone(classify_risk("git status", "shell"))
        self.assertIsNone(classify_risk("git diff --stat", "shell"))

    def test_core_dispatch_requires_consent_task_does_not(self):
        import agent
        spec = agent.TOOL_REGISTRY["dispatch_claude_engineer_sync"]
        self.assertIsNotNone(spec.risk({"task_description": "x", "scope": "core"}))
        self.assertIsNone(spec.risk({"task_description": "x"}))
        self.assertIsNone(spec.risk({"task_description": "x", "scope": "task"}))
        agy_spec = agent.TOOL_REGISTRY["dispatch_antigravity_engineer_sync"]
        self.assertIsNotNone(agy_spec.risk({"task_description": "x", "scope": "core"}))

    def test_system_instruction_is_compact(self):
        import agent
        self.assertLess(len(agent.SYSTEM_INSTRUCTION), 2500, "系統提示詞每次呼叫都會送出，請保持精簡")
        self.assertNotIn("skills", agent.SYSTEM_INSTRUCTION)  # 技能庫同步規範屬於開發文件，不放提示詞

    def test_system_instruction_keeps_style_rules(self):
        import agent
        text = agent.SYSTEM_INSTRUCTION
        self.assertIn(
            "嚴格禁止輸出任何空行與分隔線（如 ---），所有文字、標題與清單必須緊密相連無任何空行，內容精簡扼要，避免訊息過長。",
            text,
        )
        self.assertNotIn("回覆排版盡量緊湊並減少空行", text)
        for kept in ("繁體中文（台灣用語）", "Markdown 條列與粗體"):
            self.assertIn(kept, text)

    def test_acceptance_embed(self):
        import bot
        embed = bot.build_acceptance_embed(ACCEPTANCE)
        names = [f.name for f in embed.fields]
        self.assertTrue(any("全數通過" in n for n in names))
        self.assertTrue(any("commit" in f.value for f in embed.fields))


if __name__ == "__main__":
    unittest.main()
