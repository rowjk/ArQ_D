"""
 Worktree 隔離 —— ArQ 執行資料夾與開發分開，一張單一個 worktree。用暫存 git repo，不連網。

驗收標準：
    - scope=core 派工時為該 ticket 建立 worktree (<repo>-worktrees/ticket-N)，分支 ticket-N-<slug>，從 main 開
    - RD 只在 worktree 內工作；ArQ 執行中的資料夾保持在 main、不被修改
    - 驗收 (collect_acceptance) 在 worktree 內跑測試與 diff
    - PR merge 後清除 worktree；失敗或中止時保留並通知使用者
    - 同一張單再次派工時沿用原本的 worktree
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_common, rd_dispatch, rd_jobs, worktree  # noqa: E402

GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=test"]


def git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class TempRepo(unittest.TestCase):
    """每個測試一個暫存 repo (main 分支、一個 commit)，worktree 放在旁邊的 repo-worktrees/"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arq_wt_")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        with open(os.path.join(self.repo, "app.py"), "w", encoding="utf-8") as f:
            f.write("print('v1')\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        self.env = patch.dict(os.environ, {"ARQ_WORKTREES_DIR": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestNaming(unittest.TestCase):
    def test_branch_name_slug(self):
        self.assertEqual(worktree.branch_name(32, "Worktree 隔離：ArQ 執行資料夾"), "ticket-32-worktree-arq")
        self.assertEqual(worktree.branch_name(7, "全中文標題"), "ticket-7-work")
        self.assertLessEqual(len(worktree.branch_name(9, "a " * 80)), len("ticket-9-") + 30)

    def test_default_dir_is_sibling_of_repo(self):
        with patch.dict(os.environ, {"ARQ_WORKTREES_DIR": ""}):
            self.assertEqual(worktree.worktrees_dir(os.path.join("C:\\x", "ArQ")), os.path.join("C:\\x", "ArQ-worktrees"))

    def test_dir_can_be_configured(self):
        with patch.dict(os.environ, {"ARQ_WORKTREES_DIR": "D:\\wt"}):
            self.assertEqual(worktree.worktrees_dir("C:\\x\\ArQ"), "D:\\wt")


class TestTestingGuard(unittest.TestCase):
    def test_refuses_arq_repo_during_tests(self):
        self.assertEqual(os.environ.get("ARQ_TESTING"), "1")
        with patch.object(worktree, "_git") as g:
            with self.assertRaises(worktree.WorktreeError):
                worktree.ensure_worktree(17, "x")
        g.assert_not_called()


class TestEnsure(TempRepo):
    def test_creates_worktree_from_main(self):
        info = worktree.ensure_worktree(32, "Worktree 隔離", repo=self.repo)
        self.assertTrue(info["created"])
        self.assertEqual(info["branch"], "ticket-32-worktree")
        # ensure_worktree 回傳長路徑；self.tmp 在 CI 上是 8.3 短路徑 (RUNNER~1)，預期值也要展開
        self.assertEqual(os.path.normcase(info["path"]),
                         os.path.normcase(os.path.realpath(os.path.join(self.tmp, "repo-worktrees", "ticket-32"))))
        self.assertTrue(os.path.exists(os.path.join(info["path"], "app.py")))
        self.assertEqual(git(info["path"], "branch", "--show-current"), "ticket-32-worktree")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")  # 執行資料夾不動

    def test_reuses_existing_worktree(self):
        first = worktree.ensure_worktree(32, "Worktree", repo=self.repo)
        with open(os.path.join(first["path"], "app.py"), "a", encoding="utf-8") as f:
            f.write("# 未完成的改動\n")
        again = worktree.ensure_worktree(32, "改了標題也一樣", repo=self.repo)
        self.assertFalse(again["created"])
        self.assertEqual(os.path.normcase(again["path"]), os.path.normcase(first["path"]))
        self.assertEqual(again["branch"], first["branch"])
        with open(os.path.join(again["path"], "app.py"), encoding="utf-8") as f:
            self.assertIn("未完成的改動", f.read())

    @unittest.skipUnless(sys.platform == "win32", "8.3 短路徑只在 Windows")
    def test_same_path_for_short_and_long_repo_path(self):
        """repo 以 8.3 短路徑傳入 (GitHub Actions 的 TEMP 是 C:\\Users\\RUNNER~1)：新建與沿用回傳同一個長路徑"""
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(self.repo, buf, len(buf))
        short_repo = buf.value
        if os.path.normcase(short_repo) == os.path.normcase(self.repo):
            self.skipTest("這台電腦的磁碟沒有 8.3 短檔名")
        first = worktree.ensure_worktree(32, "Worktree", repo=short_repo)
        again = worktree.ensure_worktree(32, "Worktree", repo=short_repo)
        self.assertFalse(again["created"])
        self.assertEqual(first["path"], again["path"])
        self.assertEqual(first["path"], os.path.realpath(first["path"]))  # 回傳長路徑
        self.assertEqual(worktree.find_worktree(32, short_repo)["path"], first["path"])

    def test_reuses_existing_branch_without_worktree(self):
        git(self.repo, "branch", "ticket-40-old", "main")
        info = worktree.ensure_worktree(40, "新標題", repo=self.repo)
        self.assertEqual(info["branch"], "ticket-40-old")

    def test_changes_in_worktree_do_not_touch_repo(self):
        info = worktree.ensure_worktree(5, "x", repo=self.repo)
        with open(os.path.join(info["path"], "app.py"), "w", encoding="utf-8") as f:
            f.write("print('v2')\n")
        self.assertEqual(git(self.repo, "status", "--short"), "")

    def test_find(self):
        self.assertIsNone(worktree.find_worktree(8, repo=self.repo))
        worktree.ensure_worktree(8, "x", repo=self.repo)
        self.assertEqual(worktree.find_worktree(8, repo=self.repo)["branch"], "ticket-8-x")
        self.assertIsNone(worktree.find_worktree(80, repo=self.repo))  # 不會把 當成


class TestRemove(TempRepo):
    def test_clean_worktree_removed(self):
        info = worktree.ensure_worktree(3, "x", repo=self.repo)
        os.makedirs(os.path.join(info["path"], "__pycache__"))  # 被忽略的檔案不影響
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("__pycache__/\n")
        res = worktree.remove_worktree(3, repo=self.repo)
        self.assertTrue(res["removed"], res)
        self.assertFalse(os.path.exists(info["path"]))
        self.assertIsNone(worktree.find_worktree(3, repo=self.repo))

    def test_dirty_worktree_kept(self):
        info = worktree.ensure_worktree(4, "x", repo=self.repo)
        with open(os.path.join(info["path"], "new.py"), "w") as f:
            f.write("x = 1\n")
        res = worktree.remove_worktree(4, repo=self.repo)
        self.assertFalse(res["removed"])
        self.assertIn("保留", res["message"])
        self.assertTrue(os.path.exists(os.path.join(info["path"], "new.py")))

    def test_missing_worktree_is_noop(self):
        res = worktree.remove_worktree(99, repo=self.repo)
        self.assertFalse(res["removed"])
        self.assertTrue(res["success"])


class TestRunInWorkdir(TempRepo):
    def test_acceptance_runs_in_given_dir(self):
        info = worktree.ensure_worktree(6, "x", repo=self.repo)
        os.makedirs(os.path.join(info["path"], "tests"))
        with open(os.path.join(info["path"], "tests", "test_ok.py"), "w") as f:
            f.write("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        with open(os.path.join(info["path"], "app.py"), "a") as f:
            f.write("# changed\n")
        acc = rd_common.collect_acceptance(info["path"])
        self.assertTrue(acc["tests_passed"], acc)
        self.assertIn("app.py", acc["diff_stat"])
        self.assertIn("tests/", acc["status"])

    def test_workspace_for_core_uses_workdir(self):
        self.assertEqual(rd_common.workspace_for("core", "D:\\wt\\ticket-1"), "D:\\wt\\ticket-1")
        self.assertEqual(rd_common.workspace_for("core"), rd_common.PROJECT_ROOT)
        self.assertEqual(rd_common.workspace_for("task", "D:\\wt\\ticket-1"),
                         os.path.join(rd_common.PROJECT_ROOT, "task"))

    def test_engine_env_points_uv_to_main_venv(self):
        env = rd_common.workdir_env("D:\\wt\\ticket-1")
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], os.path.join(rd_common.PROJECT_ROOT, ".venv"))
        self.assertEqual(rd_common.workdir_env(""), {})
        self.assertEqual(rd_common.workdir_env(rd_common.PROJECT_ROOT), {})


OK_AUTH = {"authenticated": True, "message": "ok"}
WT = {"path": "D:\\wt\\ticket-32", "branch": "ticket-32-worktree", "created": True}


class TestDispatchUsesWorktree(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def _run(self, engine_result, scope="core", ticket=32, plan=False, session_id="", ensure=None):
        notes = []
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", return_value=engine_result) as claude, \
                patch.object(rd_dispatch, "collect_acceptance", return_value={"tests_passed": True}) as acc, \
                patch.object(rd_dispatch, "collect_worktree_state", return_value={}), \
                patch.object(rd_dispatch.worktree, "ensure_worktree", side_effect=ensure or (lambda n, t: WT)) as ens, \
                patch.object(rd_dispatch.ticket_tools, "get_ticket",
                             return_value={"success": True, "ticket": {"number": ticket, "title": "Worktree",
                                                                       "body": "x", "open_blockers": []}}), \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}), \
                patch.object(rd_dispatch.ticket_tools, "create_ticket",
                             return_value={"success": False, "error": "Ticket 功能尚未設定"}), \
                patch("builtins.print"):
            res = rd_dispatch.run_dev_task("做 #32", rd_dispatch.CLAUDE, scope=scope, opened_by="pm",
                                           ticket=ticket, plan=plan, session_id=session_id, notify=notes.append)
        return res, claude, acc, ens, notes

    def test_core_dispatch_runs_in_worktree(self):
        res, claude, acc, ens, _ = self._run({"success": True, "stdout": "完成"})
        self.assertEqual(claude.call_args.kwargs["workdir"], WT["path"])
        acc.assert_called_once_with(WT["path"])
        self.assertEqual(res["worktree_info"], {"path": WT["path"], "branch": WT["branch"]})
        self.assertEqual(ens.call_args.args[0], 32)

    def test_resume_reuses_worktree(self):
        # 接續原對話一樣走 ensure_worktree (它負責沿用既有的 worktree)，RD 回到同一個資料夾
        _, claude, _, ens, _ = self._run({"success": True, "stdout": "完成"}, session_id="s-1")
        ens.assert_called_once()
        self.assertEqual(claude.call_args.kwargs["workdir"], WT["path"])

    def test_task_scope_not_isolated(self):
        res, claude, _, ens, _ = self._run({"success": True, "stdout": "完成"}, scope="task")
        ens.assert_not_called()
        self.assertEqual(claude.call_args.kwargs.get("workdir", ""), "")

    def test_plan_with_ticket_runs_in_worktree(self):
        # Claude 的 --resume 綁定工作目錄：規劃與之後的實作必須在同一個 worktree，session_id 才接得上
        _, claude, acc, ens, _ = self._run({"success": True, "stdout": "規格"}, plan=True)
        ens.assert_called_once()
        self.assertEqual(claude.call_args.kwargs["workdir"], WT["path"])
        acc.assert_not_called()

    def test_plan_without_ticket_reads_project_root(self):
        _, claude, _, ens, _ = self._run({"success": True, "stdout": "規格"}, plan=True, ticket=0)
        ens.assert_not_called()
        self.assertEqual(claude.call_args.kwargs.get("workdir", ""), "")

    def test_failure_keeps_worktree_and_notifies(self):
        failed = {"success": False, "error": "quota", "stdout": "", "stderr": ""}
        with patch.object(rd_dispatch, "check_antigravity_auth", return_value={"authenticated": False, "message": "x"}):
            res, _, _, _, notes = self._run(failed)
        self.assertEqual(res["worktree_info"]["path"], WT["path"])
        self.assertTrue(any(WT["path"] in n and "保留" in n for n in notes), notes)

    def test_worktree_creation_failure_stops_dispatch(self):
        def boom(n, t):
            raise worktree.WorktreeError("磁碟滿了")
        res, claude, _, _, _ = self._run({"success": True}, ensure=boom)
        self.assertFalse(res["success"])
        self.assertIn("worktree", res["error"])
        claude.assert_not_called()

    def test_core_without_ticket_number_refuses(self):
        # 沒有單號 (ticket 功能未設定、開單失敗) 無法命名 worktree：不得退回在執行資料夾裡改
        res, claude, _, ens, _ = self._run({"success": True}, ticket=0)
        self.assertFalse(res["success"])
        self.assertIn("單號", res["error"])
        claude.assert_not_called()
        ens.assert_not_called()

    def test_reports_show_worktree(self):
        res = {"success": True, "engine_label": "Claude Code", "stdout": "完成", "scope": "core",
               "ticket": 32, "worktree_info": {"path": WT["path"], "branch": WT["branch"]}}
        self.assertIn(WT["path"], rd_dispatch.format_dev_report(res))
        compact = rd_dispatch.compact_dev_result(res)
        self.assertEqual(compact["worktree_info"], res["worktree_info"])
        self.assertIn(WT["branch"], compact["commit_note"])
        self.assertIn("Closes #32", compact["commit_note"])
        self.assertIn("git -C", compact["commit_note"])


class TestMergeCleanup(unittest.TestCase):
    def test_cleanup_after_merge_removes_ticket_worktrees(self):
        with patch.object(worktree.ticket_tools, "pr_merge_summary",
                          return_value={"success": True, "merged": True, "tickets": [32, 33]}), \
                patch.object(worktree, "remove_worktree",
                             side_effect=lambda n, **kw: {"success": True, "removed": n == 32,
                                                    "message": f"#{n} 訊息"}) as rm:
            messages = worktree.cleanup_after_merge(77)
        self.assertEqual([c.args[0] for c in rm.call_args_list], [32, 33])
        self.assertEqual(messages, ["#32 訊息", "#33 訊息"])

    def test_not_merged_keeps_worktrees(self):
        with patch.object(worktree.ticket_tools, "pr_merge_summary",
                          return_value={"success": True, "merged": False, "tickets": [32]}), \
                patch.object(worktree, "remove_worktree") as rm:
            self.assertEqual(worktree.cleanup_after_merge(77), [])
        rm.assert_not_called()


class TestAgentMergeCleanup(unittest.TestCase):
    """使用者同意的 gh pr merge 執行後，agent 清除對應 ticket 的 worktree 並通知"""

    def _merge(self, command="gh pr merge 77 --squash", messages=("🧹 已清除 #32 的 worktree",)):
        import asyncio
        import contextlib
        import io
        import agent
        spec = agent.TOOL_REGISTRY["run_powershell_sync"]
        fake_spec = spec.__class__(func=lambda **kw: {"success": True}, label=spec.label, risk=spec.risk, guard=spec.guard)
        notes = []

        async def confirm(title, detail):
            return True

        async def notify(text):
            notes.append(text)

        with patch.dict(agent.TOOL_REGISTRY, {"run_powershell_sync": fake_spec}), \
                patch.object(agent.ticket_tools, "pr_merge_summary", return_value={"success": False, "error": "x"}), \
                patch.object(agent.worktree, "cleanup_after_merge", return_value=list(messages)) as cleanup, \
                self.assertLogs(level="INFO"):
            asyncio.run(agent.ArQAgent(api_key="")._execute_tool("run_powershell_sync", {"command": command},
                                                                 confirm, notify))
        return cleanup, notes

    def test_merge_triggers_cleanup_and_notifies(self):
        cleanup, notes = self._merge()
        cleanup.assert_called_once_with(77)
        self.assertIn("🧹 已清除 #32 的 worktree", notes)

    def test_other_commands_do_not_cleanup(self):
        import agent
        with patch.object(agent.worktree, "cleanup_after_merge") as cleanup, \
                patch("builtins.print"):
            import asyncio
            spec = agent.TOOL_REGISTRY["run_powershell_sync"]
            fake = spec.__class__(func=lambda **kw: {"success": True}, label=spec.label, risk=spec.risk, guard=spec.guard)
            with patch.dict(agent.TOOL_REGISTRY, {"run_powershell_sync": fake}):
                asyncio.run(agent.ArQAgent(api_key="")._execute_tool("run_powershell_sync", {"command": "git status"},
                                                                     None, None))
        cleanup.assert_not_called()

    def test_rule9_commits_in_worktree(self):
        import agent
        self.assertIn("worktree", agent.SYSTEM_INSTRUCTION)
        self.assertIn("commit_note", agent.SYSTEM_INSTRUCTION)
        self.assertLess(len(agent.SYSTEM_INSTRUCTION), 2500)


if __name__ == "__main__":
    unittest.main()
