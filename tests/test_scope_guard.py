"""
 scope=task 派工時偵測 RD 改到 ArQ 核心檔案並補驗收卡。不啟動 RD、不連網。

事件：/claude 用預設 scope=task，RD 卻直接改了 bot.py 等核心檔案；scope=task 只靠提示詞約束，
所以沒有核心確認卡、完成後也沒有驗收卡。

驗收標準：
    - scope=task 派工前後比對 ArQ 的 git 狀態；task/ 以外有變動時，結果標示 scope_violation
    - 越界時回報醒目警告 (列出檔案)，並照 scope=core 附上驗收卡
    - 斜線派工的需求含 #N 但 scope 是 task 時，開工訊息提示改用 scope: core
    - 只讀規劃 (plan=true) 被停止時，ticket 不改為 blocked (只留言)；實作派工被停止時維持 blocked
"""

import inspect
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
from tools.rd_common import SCOPE_CORE, SCOPE_TASK  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}
ACCEPTANCE = {"diff_stat": " bot.py | 20 ++++", "status": " M bot.py", "tests_passed": True, "tests_summary": "OK"}
_PATCHES = []


def setUpModule():
    from tools import ticket_tools
    _PATCHES.extend([patch.dict(os.environ, {"TICKETS_REPO": "", "TICKETS_GITHUB_TOKEN": ""}),
                     patch.object(ticket_tools, "_send", side_effect=AssertionError("測試不得連到 GitHub")),
                     # scope=core 會在該單的 worktree 開發；這裡測的是派工 / 驗收 / 停止流程，用假的 worktree
                     patch.object(rd_dispatch, "_prepare_worktree", return_value=({"path": "fake-worktrees/ticket-1", "branch": "ticket-1-test", "created": False}, None))])
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()


def run(states, scope=SCOPE_TASK, plan=False, engine_result=None):
    """states：collect_worktree_state 依序回傳的值 (派工前、派工後)"""
    rd_jobs._reset_for_tests()
    seq = iter(states)
    with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
            patch.object(rd_dispatch, "run_claude_task",
                         return_value=engine_result or {"success": True, "stdout": "完成"}), \
            patch.object(rd_dispatch, "run_antigravity_task",
                         return_value=engine_result or {"success": True, "stdout": "完成"}), \
            patch.object(rd_dispatch, "collect_worktree_state", side_effect=lambda: next(seq)) as state, \
            patch.object(rd_dispatch, "collect_acceptance", return_value=ACCEPTANCE) as acceptance, \
            patch("builtins.print"):
        res = rd_dispatch.run_dev_task("做 #17", rd_dispatch.CLAUDE, scope=scope, plan=plan)
    return res, state, acceptance


CLEAN = {"status": "", "diff_stat": ""}
TOUCHED = {"status": " M bot.py\n M README.md\n?? tests/test_ticket_new.py", "diff_stat": " bot.py | 20 ++++"}


class TestScopeViolation(unittest.TestCase):
    def test_task_scope_touching_core_is_flagged_with_acceptance(self):
        res, _, acceptance = run([CLEAN, TOUCHED])
        self.assertEqual(res["scope_violation"], ["bot.py", "README.md", "tests/test_ticket_new.py"])
        acceptance.assert_called_once()
        self.assertEqual(res["acceptance"], ACCEPTANCE)

    def test_task_scope_without_core_changes_not_flagged(self):
        res, _, acceptance = run([CLEAN, CLEAN])
        self.assertNotIn("scope_violation", res)
        acceptance.assert_not_called()

    def test_preexisting_changes_are_not_blamed_on_rd(self):
        before = {"status": " M GEMINI.md", "diff_stat": " GEMINI.md | 1 +"}
        after = {"status": " M GEMINI.md\n M bot.py", "diff_stat": " GEMINI.md | 1 +\n bot.py | 3 ++"}
        res, _, _ = run([before, after])
        self.assertEqual(res["scope_violation"], ["bot.py"])

    def test_core_scope_not_checked_twice(self):
        res, state, acceptance = run([CLEAN, TOUCHED], scope=SCOPE_CORE)
        state.assert_not_called()                  # core 本來就跑驗收，不需要越界比對
        acceptance.assert_called_once()
        self.assertNotIn("scope_violation", res)

    def test_plan_mode_not_checked(self):
        res, state, _ = run([CLEAN, TOUCHED], plan=True)
        state.assert_not_called()                  # plan 有自己的檢查
        self.assertNotIn("scope_violation", res)

    def test_failed_dispatch_still_reports_violation(self):
        res, _, acceptance = run([CLEAN, TOUCHED], engine_result={"success": False, "error": "x", "stdout": ""})
        self.assertEqual(res["scope_violation"][0], "bot.py")


class TestReports(unittest.TestCase):
    RES = {"success": True, "engine_label": "Claude Code", "scope": "task", "stdout": "完成", "attempts": [],
           "scope_violation": ["bot.py", "README.md"], "acceptance": ACCEPTANCE, "ticket": 17}

    def test_user_report_warns_and_lists_files(self):
        text = rd_dispatch.format_dev_report(self.RES)
        self.assertIn("越界", text)
        self.assertIn("bot.py", text)
        self.assertIn("驗收卡", text)

    def test_model_view_flags_violation(self):
        compact = rd_dispatch.compact_dev_result(self.RES)
        self.assertEqual(compact["scope_violation"], ["bot.py", "README.md"])
        self.assertIn("acceptance", compact)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), rd_dispatch.MAX_MODEL_RESULT_CHARS)


class TestSlashHint(unittest.TestCase):
    def test_hint_text(self):
        import bot
        self.assertIn("scope: core", bot.scope_hint("做 #17", SCOPE_TASK))
        self.assertEqual(bot.scope_hint("做 #17", SCOPE_CORE), "")
        self.assertEqual(bot.scope_hint("寫一個匯率爬蟲", SCOPE_TASK), "")

    def test_slash_flow_uses_hint_and_reports_violation(self):
        import bot
        src = inspect.getsource(bot.run_slash_dev_task)
        self.assertIn("scope_hint(task, scope)", src)
        self.assertIn("scope_violation", src)


class TestPlanStopKeepsStatus(unittest.TestCase):
    def _cancel(self, plan):
        rd_jobs._reset_for_tests()

        def engine(*a, job=None, **k):
            job.cancel()
            return {"success": False, "cancelled": True, "error": "使用者已停止開發", "stdout": ""}

        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", side_effect=engine), \
                patch.object(rd_dispatch, "collect_worktree_state", return_value=CLEAN), \
                patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value={"success": False, "error": "尚未設定"}), \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}) as update, \
                patch.object(rd_jobs, "_kill_tree", lambda pid: None), patch("builtins.print"):
            rd_dispatch.run_dev_task("研究 #17", rd_dispatch.CLAUDE, scope=SCOPE_CORE, ticket=17, plan=plan,
                                     opened_by="pm")
        return update

    def test_plan_stop_comments_without_blocking(self):
        update = self._cancel(plan=True)
        calls = [c for c in update.call_args_list if "停止" in c.kwargs.get("comment", "")]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("status", calls[0].kwargs)

    def test_build_stop_still_blocks(self):
        update = self._cancel(plan=False)
        self.assertTrue(any(c.kwargs.get("status") == "blocked" for c in update.call_args_list))


if __name__ == "__main__":
    unittest.main()
