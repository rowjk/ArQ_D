"""
 非同步派工 —— PM 派出後不必等待。不啟動 RD、不連網。

驗收標準：
    - start_dev_task(..., on_done) 先用 rd_jobs.reserve 佔位，被拒絕時立即回傳理由；成功時背景執行並回傳 job_id
    - 派工工具立即回傳 accepted / job_id / 單號 / RD；完成時完整回報送 Discord
    - 精簡結果放進待轉達佇列，PM 下一輪對話開頭讀到
    - 背景任務的同意卡、停止按鈕照常運作；ArQ 重啟時告知有任務中斷
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch, rd_jobs  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}


class Gate:
    """讓假的 RD 卡在執行中，測試可以檢查「派工已回傳但 RD 還沒做完」的狀態"""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def engine(self, result):
        def run(task, **kw):
            self.started.set()
            self.release.wait(5)
            return result
        return run


class DispatchPatches(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()
        self.patches = [
            patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH),
            patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH),
            patch.object(rd_dispatch, "collect_worktree_state", return_value={}),
            patch.object(rd_dispatch, "collect_acceptance", return_value={"tests_passed": True}),
            patch.object(rd_dispatch, "_prepare_worktree",
                         return_value=({"path": "fake-worktrees/ticket-76", "branch": "ticket-76-x"}, None)),
            patch.object(rd_dispatch.ticket_tools, "get_ticket",
                         return_value={"success": True, "ticket": {"number": 76, "open_blockers": [], "body": ""}}),
            patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}),
            patch("builtins.print"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        rd_jobs._reset_for_tests()


class TestStartDevTask(DispatchPatches):
    def test_returns_immediately_then_calls_on_done(self):
        gate, done = Gate(), threading.Event()
        results = []
        with patch.object(rd_dispatch, "run_claude_task", side_effect=gate.engine({"success": True, "stdout": "完成"})):
            res = rd_dispatch.start_dev_task("做 #76", CLAUDE, ticket=76, opened_by="pm",
                                             on_done=lambda r: (results.append(r), done.set()))
            self.assertTrue(res["accepted"])
            self.assertEqual(res["ticket"], 76)
            self.assertEqual(res["engine_label"], "Claude Code")
            self.assertTrue(gate.started.wait(5))
            self.assertEqual(results, [])                       # RD 還在做，派工已回傳
            self.assertEqual(rd_jobs.busy_engines(), {CLAUDE})
            self.assertIn(res["job_id"], [j.id for j in rd_jobs.list_jobs()])
            gate.release.set()
            self.assertTrue(done.wait(5))
        self.assertTrue(results[0]["success"])
        self.assertEqual(rd_jobs.busy_engines(), set())

    def test_refused_when_both_engines_busy(self):
        for t, engine in ((90, CLAUDE), (91, ANTIGRAVITY)):
            j = rd_jobs.DevJob(task="x", engine_label="RD", scope="core", ticket=t)
            rd_jobs.register(j)
            rd_jobs.reserve(j, engine)
        on_done = []
        with patch.object(rd_dispatch, "run_claude_task") as claude:
            res = rd_dispatch.start_dev_task("做 #76", CLAUDE, ticket=76, opened_by="pm", on_done=on_done.append)
        self.assertFalse(res["accepted"])
        self.assertIn("#90", res["error"])
        claude.assert_not_called()
        self.assertEqual(on_done, [])
        self.assertNotIn(76, {j.ticket for j in rd_jobs.list_jobs()})

    def test_busy_preferred_engine_accepted_on_other(self):
        j = rd_jobs.DevJob(task="x", engine_label="RD", scope="core", ticket=90)
        rd_jobs.register(j)
        rd_jobs.reserve(j, CLAUDE)
        gate, done = Gate(), threading.Event()
        with patch.object(rd_dispatch, "run_antigravity_task", side_effect=gate.engine({"success": True, "stdout": "完成"})):
            res = rd_dispatch.start_dev_task("做 #76", CLAUDE, ticket=76, opened_by="pm", on_done=lambda r: done.set())
            self.assertTrue(res["accepted"])
            self.assertEqual(res["engine_label"], "Antigravity")
            gate.release.set()
            self.assertTrue(done.wait(5))

    def test_same_ticket_refused(self):
        j = rd_jobs.DevJob(task="x", engine_label="Claude Code", scope="core", ticket=76)
        rd_jobs.register(j)
        rd_jobs.reserve(j, CLAUDE)
        res = rd_dispatch.start_dev_task("做 #76", ANTIGRAVITY, ticket=76, opened_by="pm", on_done=lambda r: None)
        self.assertFalse(res["accepted"])
        self.assertIn("同一張單", res["error"])

    def test_blocked_ticket_refused_immediately(self):
        with patch.object(rd_dispatch.ticket_tools, "get_ticket",
                          return_value={"success": True, "ticket": {"number": 76, "open_blockers": [75], "body": ""}}):
            res = rd_dispatch.start_dev_task("做 #76", CLAUDE, ticket=76, opened_by="pm", on_done=lambda r: None)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["blocked_by"], [75])

    def test_engine_crash_still_calls_on_done(self):
        done, results = threading.Event(), []
        with patch.object(rd_dispatch, "run_dev_task", side_effect=RuntimeError("boom")):
            res = rd_dispatch.start_dev_task("x", CLAUDE, ticket=76, opened_by="pm",
                                             on_done=lambda r: (results.append(r), done.set()))
            self.assertTrue(res["accepted"])
            self.assertTrue(done.wait(5))
        self.assertFalse(results[0]["success"])
        self.assertIn("boom", results[0]["error"])
        self.assertEqual(rd_jobs.list_jobs(), [])

    def test_announcer_called_once_with_ticket(self):
        seen = []
        rd_jobs.set_announcer(lambda job: seen.append(job.ticket))
        done = threading.Event()
        with patch.object(rd_dispatch, "run_claude_task", return_value={"success": True, "stdout": "完成"}):
            rd_dispatch.start_dev_task("做 #76", CLAUDE, ticket=76, opened_by="pm", on_done=lambda r: done.set())
            self.assertTrue(done.wait(5))
        self.assertEqual(seen, [76])  # 停止按鈕訊息只送一次


class TestCompactAccepted(unittest.TestCase):
    def test_accepted_view_tells_pm_not_to_wait(self):
        view = rd_dispatch.compact_dev_result({"accepted": True, "job_id": "abc", "ticket": 76, "engine_label": "Claude Code"})
        self.assertTrue(view["accepted"])
        self.assertEqual(view["job_id"], "abc")
        self.assertIn("背景", view["note"])

    def test_refused_view(self):
        view = rd_dispatch.compact_dev_result({"accepted": False, "error": "Claude Code 正在做 #90", "ticket": 76})
        self.assertFalse(view["accepted"])
        self.assertIn("#90", view["error"])

    def test_accepted_has_no_user_report(self):
        self.assertEqual(rd_dispatch.format_dev_report({"accepted": True, "job_id": "abc"}), "")


class TestAgentBackgroundDispatch(DispatchPatches):
    def _agent(self):
        import agent
        return agent, agent.ArQAgent(api_key="")

    def test_tool_returns_immediately_and_report_queued_for_next_turn(self):
        agent, a = self._agent()
        gate = Gate()
        notes = []

        async def notify(text):
            notes.append(text)

        async def confirm(title, detail):
            return True

        async def scenario():
            with patch.object(rd_dispatch, "run_claude_task", side_effect=gate.engine({"success": True, "stdout": "做完了"})), \
                    self.assertLogs(level="INFO"):
                result, _ = await a._execute_tool("dispatch_claude_engineer_sync",
                                                  {"task_description": "做 #76", "ticket": 76}, confirm, notify)
                self.assertTrue(result["accepted"])
                self.assertEqual(a.pending_reports(), [])
                gate.release.set()
                for _ in range(100):
                    if a.pending_reports():
                        break
                    await asyncio.sleep(0.05)
            return result

        asyncio.run(scenario())
        reports = a.pending_reports()
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]["success"])
        self.assertTrue(any("開發任務完成" in n for n in notes), notes)

    def test_pending_reports_prepended_to_next_prompt_once(self):
        agent, a = self._agent()
        a._add_pending_report({"success": True, "ticket": 76, "engine_label": "Claude Code"})
        prompt = a._with_pending_reports("接下來呢？")
        self.assertIn("背景派工", prompt)
        self.assertIn('"ticket": 76', prompt)
        self.assertTrue(prompt.endswith("接下來呢？"))
        self.assertEqual(a._with_pending_reports("再問一次"), "再問一次")

    def test_report_queued_even_if_notify_fails(self):
        # 整合測試發現：完成通知送不出去 (事件迴圈已關閉、Discord 斷線) 時，結果不能跟著丟掉
        agent, a = self._agent()
        done = threading.Event()

        async def scenario():
            async def notify(text):
                pass

            def closed_loop(coro, loop):
                coro.close()
                raise RuntimeError("Event loop is closed")

            with patch.object(rd_dispatch, "run_claude_task", return_value={"success": True, "stdout": "完成"}), \
                    patch.object(a, "_add_pending_report", side_effect=lambda v: (a._pending.append(v), done.set())), \
                    patch.object(agent.asyncio, "run_coroutine_threadsafe", side_effect=closed_loop), \
                    self.assertLogs(level="INFO"):
                await a._execute_tool("dispatch_claude_engineer_sync", {"task_description": "做 #76", "ticket": 76},
                                      None, notify)
                for _ in range(100):
                    if done.is_set():
                        break
                    await asyncio.sleep(0.05)

        asyncio.run(scenario())
        self.assertTrue(done.is_set())
        self.assertEqual(len(a.pending_reports()), 1)

    def test_dispatch_tools_are_background(self):
        agent, _ = self._agent()
        for name in ("dispatch_claude_engineer_sync", "dispatch_antigravity_engineer_sync"):
            self.assertTrue(agent.TOOL_REGISTRY[name].background, name)


class TestInterruptedOnRestart(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="arq_jobs_")
        self.path = os.path.join(self.tmp, "running.json")
        self.p = patch.object(rd_jobs, "STATE_PATH", self.path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        rd_jobs._reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_running_jobs_persisted_and_reported_after_restart(self):
        job = rd_jobs.DevJob(task="做 #76 非同步", engine_label="Claude Code", scope="core", ticket=76, channel_id=123)
        rd_jobs.register(job)
        with open(self.path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved[0]["ticket"], 76)
        rd_jobs._reset_for_tests(keep_state=True)  # 模擬程式被結束：記憶體清空，檔案還在
        entries = rd_jobs.take_interrupted()
        self.assertEqual([e["ticket"] for e in entries], [76])
        self.assertEqual(entries[0]["channel_id"], 123)
        self.assertEqual(rd_jobs.take_interrupted(), [])  # 只回報一次
        text = rd_jobs.format_interrupted(entries)
        self.assertIn("#76｜Claude Code", text)
        self.assertIn("中斷", text)

    def test_bot_reports_interrupted_jobs_per_channel(self):
        import bot
        for ticket, channel in ((76, 123), (77, 123), (78, 456)):
            rd_jobs.register(rd_jobs.DevJob(task=f"做 #{ticket}", engine_label="Claude Code", scope="core",
                                            ticket=ticket, channel_id=channel))
        rd_jobs._reset_for_tests(keep_state=True)
        sent = {}

        class FakeChannel:
            def __init__(self, cid):
                self.cid = cid

            async def send(self, text):
                sent.setdefault(self.cid, []).append(text)

        async def fetch(cid):
            return FakeChannel(cid)

        with patch.object(bot, "_fetch_channel", side_effect=fetch), patch("builtins.print"):
            asyncio.run(bot.report_interrupted_jobs())
        self.assertEqual(sorted(sent), [123, 456])
        self.assertIn("#76｜Claude Code", sent[123][0])
        self.assertIn("#77｜Claude Code", sent[123][0])
        self.assertIn("#78｜Claude Code", sent[456][0])
        self.assertEqual(rd_jobs.take_interrupted(), [])

    def test_finished_jobs_not_reported(self):
        job = rd_jobs.DevJob(task="x", engine_label="Claude Code", scope="core", ticket=76)
        rd_jobs.register(job)
        rd_jobs.finish(job)
        self.assertEqual(rd_jobs.take_interrupted(), [])

    def test_tests_do_not_write_real_state_file(self):
        self.p.stop()
        try:
            self.assertNotEqual(rd_jobs.STATE_PATH, self.path)
            before = os.path.exists(rd_jobs.STATE_PATH) and os.path.getmtime(rd_jobs.STATE_PATH)
            job = rd_jobs.DevJob(task="x", engine_label="RD", scope="task")
            rd_jobs.register(job)
            rd_jobs.finish(job)
            after = os.path.exists(rd_jobs.STATE_PATH) and os.path.getmtime(rd_jobs.STATE_PATH)
            self.assertEqual(before, after)
        finally:
            self.p.start()


if __name__ == "__main__":
    unittest.main()
