"""
 平行派工整合測試。RD 引擎與 GitHub 全部 mock，不連網；
worktree 在暫存 git repo 裡真的建立，驗證「各自在自己的 worktree」與主資料夾不被修改。

情境：
    - 兩張獨立子單同時派給 Claude 與 agy：都立即受理、各在自己的 worktree、各自完成通知、PM 下一輪各收到一筆
    - 一位失敗時不會接手另一位正在做的單；同一張單不會被派兩次；超過上限 / 有相依時拒絕
    - 每個 PR 各自走 的 merge 守門 (各一張確認卡)，merge 後只清掉該單的 worktree
"""

import asyncio
import functools
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import agent  # noqa: E402
from tools import rd_dispatch, rd_jobs, worktree  # noqa: E402

GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=test"]
PARENT = 100
TICKETS = {101: "實作 A 模組", 102: "實作 B 模組", 103: "整合與全部測試"}
EDGES = [(101, 103), (102, 103)]  # 101、102 互相獨立，都擋住 103


def git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def fake_get_ticket(n):
    n = int(n)
    if n not in TICKETS:
        return {"success": False, "error": "404"}
    ref = lambda m: {"number": m, "title": TICKETS[m], "state": "open", "status": "todo"}  # noqa: E731
    return {"success": True, "ticket": {
        "number": n, "title": TICKETS[n], "body": f"{TICKETS[n]}（母單 #{PARENT} 的子單）",
        "parent": {"number": PARENT, "title": "平行開發"}, "sub_issues": [],
        "blocked_by": [ref(a) for a, b in EDGES if b == n], "blocking": [ref(b) for a, b in EDGES if a == n],
        "open_blockers": [a for a, b in EDGES if b == n]}}


class FakeEngine:
    """假的 RD：在拿到的 workdir 寫一個檔案、等測試放行、回報完成 (或失敗)"""

    def __init__(self, name, result=None):
        self.name = name
        self.result = result
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, task, notify=None, approve=None, scope="task", session_id="", job=None, workdir="", **kw):
        self.calls.append({"workdir": workdir, "ticket": job.ticket if job else None})
        if workdir:
            with open(os.path.join(workdir, f"{self.name}.txt"), "w", encoding="utf-8") as f:
                f.write(task[:50])
        self.started.set()
        self.release.wait(10)
        return self.result or {"success": True, "stdout": f"{self.name} 完成"}


class ParallelScenario(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="arq_e2e_")
        self.repo = os.path.join(self.tmp, "ArQ")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("print('v1')\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        self.claude, self.agy = FakeEngine("claude"), FakeEngine("agy")
        self.notes, self.cards = [], []
        real_ensure, real_cleanup = worktree.ensure_worktree, worktree.cleanup_after_merge
        self.patches = [
            patch.dict(os.environ, {"ARQ_WORKTREES_DIR": "", "RD_MAX_PARALLEL": ""}),
            patch.object(rd_dispatch, "check_claude_auth", return_value={"authenticated": True}),
            patch.object(rd_dispatch, "check_antigravity_auth", return_value={"authenticated": True}),
            patch.object(rd_dispatch, "run_claude_task", side_effect=self.claude),
            patch.object(rd_dispatch, "run_antigravity_task", side_effect=self.agy),
            patch.object(rd_dispatch.worktree, "ensure_worktree",
                         side_effect=lambda n, t: real_ensure(n, t, repo=self.repo)),
            patch.object(agent.worktree, "cleanup_after_merge", side_effect=functools.partial(real_cleanup, repo=self.repo)),
            patch.object(rd_dispatch, "collect_acceptance", return_value={"tests_passed": True, "diff_stat": "", "tests_summary": "OK"}),
            patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=fake_get_ticket),
            patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}),
            patch("builtins.print"),
        ]
        for p in self.patches:
            p.start()
        self.agent = agent.ArQAgent(api_key="")
        # 正式環境中 bot 的事件迴圈一直存在：背景任務完成時的通知送回這個迴圈
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.claude.release.set()
        self.agy.release.set()
        # 先等背景任務收尾 (含完成回呼與通知)，才能拆掉 patch：晚啟動的任務否則會打到真的 GitHub
        for _ in range(200):
            if not any(t.name.startswith("rd-job-") and t.is_alive() for t in threading.enumerate()):
                break
            self.pump(0.05)
        self.pump(0.1)
        for p in reversed(self.patches):
            p.stop()
        self.loop.close()
        rd_jobs._reset_for_tests()
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- 工具呼叫 (走 agent 的真實流程) ---

    async def _notify(self, text):
        self.notes.append(text)

    async def _confirm(self, title, detail):
        self.cards.append(title)
        return True

    def dispatch(self, tool, ticket):
        async def go():
            with self.assertLogs(level="INFO"):
                result, _ = await self.agent._execute_tool(
                    tool, {"task_description": f"做 #{ticket}", "ticket": ticket, "scope": "core"},
                    self._confirm, self._notify)
            return result
        return self.loop.run_until_complete(go())

    def pump(self, seconds):
        self.loop.run_until_complete(asyncio.sleep(seconds))

    def wait_reports(self, count, timeout=10):
        for _ in range(int(timeout / 0.05)):
            self.pump(0.05)
            if len(self.agent.pending_reports()) >= count:
                self.pump(0.1)  # 完成通知在入列之後送出
                return True
        return False

    def claude_first(self, ticket):
        return self.dispatch("dispatch_claude_engineer_sync", ticket)

    def agy_first(self, ticket):
        return self.dispatch("dispatch_antigravity_engineer_sync", ticket)


class TestTwoIndependentTicketsInParallel(ParallelScenario):
    def test_both_run_in_own_worktree_and_report_separately(self):
        a = self.claude_first(101)
        b = self.agy_first(102)
        self.assertTrue(a["accepted"], a)
        self.assertTrue(b["accepted"], b)
        self.assertTrue(self.claude.started.wait(5) and self.agy.started.wait(5))
        self.assertEqual(rd_jobs.busy_engines(), {"claude", "antigravity"})  # 真的同時在跑
        self.assertEqual(rd_jobs.running_tickets(), {101, 102})

        dirs = {self.claude.calls[0]["workdir"], self.agy.calls[0]["workdir"]}
        self.assertEqual(len(dirs), 2)
        self.assertTrue(self.claude.calls[0]["workdir"].endswith("ticket-101"))
        self.assertTrue(self.agy.calls[0]["workdir"].endswith("ticket-102"))
        self.assertEqual(git(self.claude.calls[0]["workdir"], "branch", "--show-current"), "ticket-101-a")
        self.assertEqual(git(self.repo, "status", "--short"), "")          # 主資料夾不動
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")

        self.claude.release.set()
        self.agy.release.set()
        self.assertTrue(self.wait_reports(2))
        reports = {r.get("ticket"): r for r in self.agent.pending_reports()}
        self.assertEqual(set(reports), {101, 102})
        self.assertTrue(all(r["success"] for r in reports.values()))
        self.assertTrue(reports[101]["commit_note"].count("ticket-101") >= 2)
        self.assertTrue(any(n.startswith("🚀 **【#101｜Claude Code】") for n in self.notes), self.notes)
        self.assertTrue(any(n.startswith("🚀 **【#102｜Antigravity】") for n in self.notes), self.notes)
        self.assertEqual(rd_jobs.busy_engines(), set())

        prompt = self.agent._with_pending_reports("好了嗎？")
        self.assertIn('"ticket": 101', prompt)
        self.assertIn('"ticket": 102', prompt)

    def test_dependent_ticket_not_dispatched_alongside(self):
        self.assertTrue(self.claude_first(101)["accepted"])
        self.assertTrue(self.claude.started.wait(5))
        res = self.agy_first(103)  # 103 被 101 擋住
        self.assertFalse(res["accepted"])
        self.assertIn("#101", res["error"])
        self.assertEqual(self.agy.calls, [])


class TestNoDoubleWork(ParallelScenario):
    def test_failure_not_taken_over_by_busy_engine(self):
        self.claude.result = {"success": False, "error": "額度用完", "stdout": "", "stderr": ""}
        self.assertTrue(self.agy_first(102)["accepted"])
        self.assertTrue(self.agy.started.wait(5))
        self.assertTrue(self.claude_first(101)["accepted"])
        self.assertTrue(self.claude.started.wait(5))
        self.claude.release.set()
        self.assertTrue(self.wait_reports(1))
        failed = self.agent.pending_reports()[0]
        self.assertFalse(failed["success"])
        self.assertEqual(failed["ticket"], 101)
        self.assertEqual([c["ticket"] for c in self.agy.calls], [102])     # agy 沒有接手
        self.assertTrue(any("#102" in n and "#101" in n for n in self.notes), self.notes)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "ArQ-worktrees", "ticket-101")))  # 失敗保留
        self.agy.release.set()
        self.assertTrue(self.wait_reports(2))

    def test_same_ticket_never_dispatched_twice(self):
        self.assertTrue(self.claude_first(101)["accepted"])
        self.assertTrue(self.claude.started.wait(5))
        again = self.agy_first(101)
        self.assertFalse(again["accepted"])
        self.assertIn("同一張單", again["error"])
        self.assertEqual(self.agy.calls, [])

    def test_third_dispatch_refused_while_both_busy(self):
        TICKETS[104] = "實作 C 模組"
        try:
            self.assertTrue(self.claude_first(101)["accepted"])
            self.assertTrue(self.agy_first(102)["accepted"])
            res = self.claude_first(104)
            self.assertFalse(res["accepted"])
            self.assertIn("正在做", res["error"])
        finally:
            del TICKETS[104]


class TestEachPrGoesThroughMergeGate(ParallelScenario):
    def _merge(self, pr, tickets):
        spec = agent.TOOL_REGISTRY["run_powershell_sync"]
        fake = spec.__class__(func=lambda **kw: {"success": True}, label=spec.label, risk=spec.risk, guard=spec.guard)
        summary = {"success": True, "number": pr, "title": f"PR {pr}", "url": f"https://x/pull/{pr}",
                   "tickets": tickets, "reviews": 1, "approved": 1, "merged": True, "state": "closed"}

        async def go():
            with patch.dict(agent.TOOL_REGISTRY, {"run_powershell_sync": fake}), \
                    patch.object(agent.ticket_tools, "pr_merge_summary", return_value=summary), \
                    patch.object(worktree.ticket_tools, "pr_merge_summary", return_value=summary), \
                    self.assertLogs(level="INFO"):
                return await self.agent._execute_tool("run_powershell_sync", {"command": f"gh pr merge {pr} --merge"},
                                                      self._confirm, self._notify)
        return self.loop.run_until_complete(go())

    def test_each_merge_has_own_card_and_cleans_only_its_worktree(self):
        self.claude_first(101)
        self.agy_first(102)
        self.claude.release.set()
        self.agy.release.set()
        self.assertTrue(self.wait_reports(2))
        # RD 的改動要先 commit 才算乾淨 (實際流程由使用者同意後 PM 依 commit_note 執行)
        for n in (101, 102):
            path = os.path.join(self.tmp, "ArQ-worktrees", f"ticket-{n}")
            git(path, "add", "-A")
            git(path, "commit", "-qm", f"ticket {n}")

        self._merge(201, [101])
        self.assertEqual(sum("merge" in c.lower() or "合併" in c for c in self.cards), 1)
        self.assertIsNone(worktree.find_worktree(101, repo=self.repo))
        self.assertIsNotNone(worktree.find_worktree(102, repo=self.repo))  # 另一張還沒 merge，保留

        self._merge(202, [102])
        self.assertEqual(sum("merge" in c.lower() or "合併" in c for c in self.cards), 2)
        self.assertIsNone(worktree.find_worktree(102, repo=self.repo))
        self.assertTrue(any("已清除 #101" in n for n in self.notes))
        self.assertTrue(any("已清除 #102" in n for n in self.notes))


if __name__ == "__main__":
    unittest.main()
