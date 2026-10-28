"""
 PM 需求對焦不再自己研究程式碼，改派 RD 做只讀規劃 (plan=true)。不啟動 RD、不連網。

驗收標準：
    - 派工工具新增只讀規劃模式：RD 只讀不改檔，回傳建議規格
    - Claude Code 以唯讀權限模式 (--permission-mode plan) 執行；Antigravity 以提示詞約束，結束後檢查工作目錄，有改動就警告
    - 只讀規劃不需核心修改確認卡、不另開新單；有帶單號時把規劃結果留言到該單
    - 規則 7：PM 不得自己讀程式碼或執行程式研究實作；改派只讀規劃
    - 工具說明維持在 6,500 字元預算內
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

from tools import rd_dispatch, rd_jobs  # noqa: E402
from tools.rd_common import SCOPE_CORE, build_plan_prompt  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}
PLAN_TEXT = "## 目標\n心跳改一行\n## 驗收標準\n- [ ] 一行\n## 影響檔案\nbot.py"
_PATCHES = []


def setUpModule():
    from tools import ticket_tools
    # 不得連到 GitHub (見)：清空 ticket 設定，底層送出一律失敗
    _PATCHES.extend([patch.dict(os.environ, {"TICKETS_REPO": "", "TICKETS_GITHUB_TOKEN": ""}),
                     patch.object(ticket_tools, "_send", side_effect=AssertionError("測試不得連到 GitHub")),
                     # scope=core 會在該單的 worktree 開發；這裡測的是派工 / 驗收 / 停止流程，用假的 worktree
                     patch.object(rd_dispatch, "_prepare_worktree", return_value=({"path": "fake-worktrees/ticket-1", "branch": "ticket-1-test", "created": False}, None))])
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()


class TestPlanPrompt(unittest.TestCase):
    def test_plan_prompt_is_read_only_and_asks_for_spec(self):
        text = build_plan_prompt("研究心跳怎麼改", SCOPE_CORE)
        for phrase in ("研究心跳怎麼改", "只讀", "不得修改", "驗收標準", "影響檔案", "未決問題"):
            self.assertIn(phrase, text)


class TestClaudePlanMode(unittest.TestCase):
    def test_command_uses_plan_permission_mode(self):
        from tools import claude_tools
        cmd = claude_tools._build_claude_command("claude", "s.json", permission_mode="plan")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        normal = claude_tools._build_claude_command("claude", "s.json")
        self.assertNotEqual(normal[normal.index("--permission-mode") + 1], "plan")

    def test_run_claude_task_plan_sends_plan_prompt(self):
        from tools import claude_tools
        sent, stdin_text = {}, {}

        class KeepingStdin(io.StringIO):
            def close(self):
                stdin_text["v"] = self.getvalue()
                super().close()

        class Proc:
            pid = 1
            returncode = None

            def __init__(self, cmd, **kw):
                sent["cmd"] = cmd
                self.stdin = KeepingStdin()
                self.stdout = iter([json.dumps({"type": "result", "result": PLAN_TEXT, "is_error": False}) + "\n"])

            def wait(self):
                self.returncode = 0

        with patch.object(claude_tools, "_resolve_claude_executable", return_value="claude"), \
                patch.object(claude_tools.subprocess, "Popen", Proc), \
                patch.object(claude_tools, "report_new_packages") as rnp:
            rnp.return_value.__enter__ = lambda s: None
            rnp.return_value.__exit__ = lambda s, *a: False
            res = claude_tools.run_claude_task("研究心跳", scope=SCOPE_CORE, plan=True)
        self.assertTrue(res["success"])
        self.assertEqual(sent["cmd"][sent["cmd"].index("--permission-mode") + 1], "plan")
        self.assertIn("只讀", stdin_text["v"])
        self.assertIn("心跳改一行", res["stdout"])


class TestDispatchPlan(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()
        rd_dispatch._reset_session_tickets_for_tests()
        self.addCleanup(rd_dispatch._reset_session_tickets_for_tests)

    def _run(self, ticket=0, opened_by="pm"):
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task",
                             return_value={"success": True, "stdout": PLAN_TEXT, "session_id": "s-plan"}) as claude, \
                patch.object(rd_dispatch, "collect_acceptance") as acceptance, \
                patch.object(rd_dispatch.ticket_tools, "create_ticket",
                             return_value={"success": True, "number": 88, "url": "u"}) as create, \
                patch.object(rd_dispatch, "_prepare_worktree", return_value=(None, None)), \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}) as update, \
                patch("builtins.print"):  # rd_dispatch 會 print emoji，避免 cp950 終端機編碼錯誤
            res = rd_dispatch.run_dev_task("研究心跳", rd_dispatch.CLAUDE, scope=SCOPE_CORE,
                                           opened_by=opened_by, ticket=ticket, plan=True)
        return res, claude, acceptance, create, update

    def test_plan_passes_flag_and_skips_acceptance(self):
        res, claude, acceptance, _, _ = self._run()
        self.assertTrue(claude.call_args.kwargs["plan"])
        self.assertTrue(res["plan"])
        acceptance.assert_not_called()

    def test_plan_without_ticket_opens_todo_ticket(self):
        # 規劃沒有單時先開 todo 的單，結果帶單號、記下 session，接續實作沿用
        res, _, _, create, update = self._run()
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["status"], "todo")
        self.assertEqual(res["ticket"], 88)
        self.assertEqual(rd_dispatch.session_ticket("s-plan"), 88)
        self.assertNotIn("status", update.call_args.kwargs)  # 只留言規劃，不改狀態

    def test_plan_without_opener_opens_no_ticket(self):
        res, _, _, create, update = self._run(opened_by="")
        create.assert_not_called()
        update.assert_not_called()
        self.assertIsNone(res["ticket"])

    def test_plan_result_commented_on_ticket_without_status_change(self):
        res, _, _, create, update = self._run(ticket=27)
        create.assert_not_called()
        update.assert_called_once()
        self.assertEqual(update.call_args.args[0], 27)
        self.assertNotIn("status", update.call_args.kwargs)
        self.assertIn("心跳改一行", update.call_args.kwargs["comment"])
        self.assertIn("只讀規劃", update.call_args.kwargs["comment"])

    def test_agy_plan_warns_when_files_changed(self):
        from tools import antigravity_tools as at
        run = {"conversation_id": "c1", "response": PLAN_TEXT, "status": "SUCCESS", "interrupted": None,
               "timed_out": False, "returncode": 0, "other_output": ""}
        states = iter([{"status": "", "diff_stat": ""}, {"status": " M bot.py", "diff_stat": " bot.py | 2 +-"}])
        with patch.object(at, "resolve_agy_executable", return_value="agy"), \
                patch.object(at, "_run_agy_once", return_value=run) as once, \
                patch.object(at, "collect_worktree_state", side_effect=lambda *a: next(states)):
            res = at.run_antigravity_task("研究", scope=SCOPE_CORE, plan=True)
        self.assertIn("只讀", once.call_args.args[1])
        self.assertIn("bot.py", res["plan_violation"])


class TestReportsAndRisk(unittest.TestCase):
    RES = {"success": True, "plan": True, "engine_label": "Claude Code", "scope": "core", "ticket": 27,
           "session_id": "s-plan", "stdout": PLAN_TEXT, "attempts": []}

    def test_user_report(self):
        text = rd_dispatch.format_dev_report(self.RES)
        for phrase in ("規劃", "只讀", "心跳改一行", "#27"):
            self.assertIn(phrase, text)
        self.assertNotIn("驗收卡", text)

    def test_model_view_tells_pm_how_to_continue(self):
        compact = rd_dispatch.compact_dev_result(self.RES)
        self.assertTrue(compact["plan"])
        self.assertEqual(compact["session_id"], "s-plan")
        self.assertIn("session_id", compact["note"])
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), rd_dispatch.MAX_MODEL_RESULT_CHARS)

    def test_plan_dispatch_needs_no_core_confirmation(self):
        import agent
        self.assertIsNone(agent._dispatch_risk({"scope": "core", "plan": True}))
        self.assertTrue(agent._dispatch_risk({"scope": "core"}))

    def test_tools_expose_plan_param(self):
        import inspect
        import agent
        for name in ("dispatch_claude_engineer_sync", "dispatch_antigravity_engineer_sync"):
            self.assertIn("plan", inspect.signature(agent.TOOL_REGISTRY[name].func).parameters)

    def test_rule7_forbids_pm_code_research(self):
        import agent
        text = agent.SYSTEM_INSTRUCTION
        self.assertIn("plan=true", text)
        self.assertIn("不得自己讀程式碼", text)
        self.assertNotIn("能用工具查到的事實自己查", text)


if __name__ == "__main__":
    unittest.main()
