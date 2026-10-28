"""
 強制停止 RD 開發 (🛑 按鈕與 /stop)。不實際啟動 RD、不連 Discord / GitHub。

驗收標準：
    - 執行中的派工登記在 tools/rd_jobs.py (引擎、任務摘要、單號、已執行時間)
    - 停止會結束整個程序樹，結果標為「已取消」，不觸發 failover
    - RD 正在等待使用者同意時也能停止：等待中的同意視為拒絕並結束
    - 停止後的回報：已停止、最後輸出摘要；scope=core 附 git status 與 diff 摘要 (不自動還原)；附 session_id
    - 經 Gemini 派工被停止時，PM 只收到精簡的「已取消」結果
    - 有 ticket 時：狀態改為 blocked，並留言停止原因與時間
"""

import contextvars
import io
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch, rd_jobs  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE, compact_dev_result, format_dev_report, run_dev_task  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}

# 本檔任何測試都不得連到 GitHub：曾因漏 mock 開單，每跑一次測試就真的開出一張標題「t」的單。
# load_dotenv() 會往上層找到 ArQ 的 .env，所以清空 ticket 設定，並在底層送出函式記錄任何嘗試。
_GITHUB_ATTEMPTS = []
_MODULE_PATCHES = []


def setUpModule():
    from tools import ticket_tools
    _MODULE_PATCHES.extend([
        patch.dict(os.environ, {"TICKETS_REPO": "", "TICKETS_GITHUB_TOKEN": ""}),
        patch.object(ticket_tools, "_send", lambda method, url, *a: _GITHUB_ATTEMPTS.append((method, url)) or (599, None)),
        # scope=core 會在該單的 worktree 開發；這裡測的是派工 / 驗收 / 停止流程，用假的 worktree
        patch.object(rd_dispatch, "_prepare_worktree", return_value=({"path": "fake-worktrees/ticket-1", "branch": "ticket-1-test", "created": False}, None)),
    ])
    for p in _MODULE_PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_MODULE_PATCHES):
        p.stop()
    if _GITHUB_ATTEMPTS:
        raise AssertionError(f"測試試圖連到 GitHub：{_GITHUB_ATTEMPTS}")


def cancelled_result(**extra):
    return {"success": False, "cancelled": True, "error": "使用者已停止開發", "stdout": "", "stderr": "",
            "returncode": -1, "session_id": "sess-1", "last_output": "正在寫測試…",
            "install_commands": [], "blocked_commands": [], **extra}


class TestJobRegistry(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def test_register_list_and_finish(self):
        job = rd_jobs.DevJob(task="寫一個匯率爬蟲\n細節", engine_label="Claude Code", scope="task", ticket=12)
        rd_jobs.register(job)
        listed = rd_jobs.list_jobs()
        self.assertEqual([j.id for j in listed], [job.id])
        info = job.summary()
        self.assertEqual(info["engine_label"], "Claude Code")
        self.assertEqual(info["task"], "寫一個匯率爬蟲")
        self.assertEqual(info["ticket"], 12)
        self.assertGreaterEqual(info["elapsed_seconds"], 0)
        rd_jobs.finish(job)
        self.assertEqual(rd_jobs.list_jobs(), [])
        self.assertIsNone(rd_jobs.get(job.id))

    def test_cancel_kills_attached_process_trees(self):
        killed = []
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        with patch.object(rd_jobs, "_kill_tree", killed.append):
            job.attach(111)
            job.attach(222)
            job.detach(222)
            self.assertTrue(job.cancel(by="user"))
            self.assertFalse(job.cancel(by="user"))  # 第二次不重複執行
        self.assertEqual(killed, [111])
        self.assertTrue(job.cancelled)

    def test_attach_after_cancel_kills_immediately(self):
        killed = []
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        with patch.object(rd_jobs, "_kill_tree", killed.append):
            job.cancel()
            job.attach(333)  # 停止與啟動程序幾乎同時發生
        self.assertEqual(killed, [333])

    def test_cancel_job_by_id(self):
        job = rd_jobs.DevJob(task="t", engine_label="Antigravity", scope="task")
        rd_jobs.register(job)
        with patch.object(rd_jobs, "_kill_tree", lambda pid: None):
            self.assertTrue(rd_jobs.cancel_job(job.id, by="user")["success"])
            self.assertFalse(rd_jobs.cancel_job(job.id, by="user")["success"])  # 已在停止中
        self.assertFalse(rd_jobs.cancel_job("nope")["success"])

    def test_finish_callbacks_and_announcer(self):
        announced, finished = [], []
        rd_jobs.set_announcer(announced.append)
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        job.add_finish_callback(lambda: finished.append(job.id))
        rd_jobs.register(job, announce=True)
        rd_jobs.finish(job)
        self.assertEqual(announced, [job])
        self.assertEqual(finished, [job.id])
        rd_jobs.set_announcer(None)

    def test_origin_contextvar_is_captured(self):
        def make():
            rd_jobs.ORIGIN.set({"owner_id": 5, "channel_id": 9})
            return rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        job = contextvars.copy_context().run(make)
        self.assertEqual((job.owner_id, job.channel_id), (5, 9))


class TestCancellableApprove(unittest.TestCase):
    def test_pending_approval_returns_false_when_cancelled(self):
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        never = threading.Event()

        def slow_approve(title, detail):
            never.wait(5)
            return True

        approve = rd_jobs.cancellable_approve(slow_approve, job)
        threading.Timer(0.1, job.cancel).start()
        start = time.time()
        with patch.object(rd_jobs, "_kill_tree", lambda pid: None):
            self.assertFalse(approve("安裝套件", "pip install x"))
        self.assertLess(time.time() - start, 2)
        never.set()

    def test_already_cancelled_never_asks(self):
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        job.cancel()
        asked = []
        approve = rd_jobs.cancellable_approve(lambda t, d: asked.append(t) or True, job)
        self.assertFalse(approve("t", "d"))
        self.assertEqual(asked, [])

    def test_normal_answer_passes_through(self):
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        self.assertTrue(rd_jobs.cancellable_approve(lambda t, d: True, job)("t", "d"))
        self.assertFalse(rd_jobs.cancellable_approve(None, job)("t", "d"))  # fail-closed


class TestDispatchCancel(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def _run(self, scope="task", ticket=0, opened_by=""):
        def claude(task, notify=None, approve=None, scope="task", session_id="", job=None, **kw):
            self.seen_job = job
            job.cancel()
            return cancelled_result()

        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", side_effect=claude), \
                patch.object(rd_dispatch, "run_antigravity_task") as agy, \
                patch.object(rd_dispatch, "collect_acceptance") as acceptance, \
                patch.object(rd_dispatch, "collect_worktree_state",
                             return_value={"status": " M agent.py", "diff_stat": " agent.py | 3 ++-"}) as state, \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}) as upd, \
                patch.object(rd_jobs, "_kill_tree", lambda pid: None), \
                patch("builtins.print"):  # rd_dispatch 會 print emoji，避免 cp950 終端機編碼錯誤
            res = run_dev_task("寫爬蟲", CLAUDE, scope=scope, ticket=ticket, opened_by=opened_by)
        return res, agy, acceptance, state, upd

    def test_cancel_does_not_fail_over(self):
        res, agy, acceptance, _, _ = self._run()
        self.assertTrue(res["cancelled"])
        self.assertFalse(res["success"])
        self.assertEqual(res["engine"], CLAUDE)
        agy.assert_not_called()
        acceptance.assert_not_called()
        self.assertEqual(rd_jobs.list_jobs(), [])  # 結束後從登記表移除

    def test_core_scope_reports_worktree_state_without_revert(self):
        res, _, acceptance, state, _ = self._run(scope="core")
        state.assert_called_once()
        acceptance.assert_not_called()  # 不跑全部測試、不還原
        self.assertIn("agent.py", res["worktree"]["diff_stat"])

    def test_ticket_set_blocked_with_reason_and_time(self):
        _, _, _, _, upd = self._run(ticket=25, opened_by="user")
        calls = [c for c in upd.call_args_list if c.kwargs.get("status") == "blocked"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], 25)
        self.assertIn("停止", calls[0].kwargs["comment"])
        self.assertRegex(calls[0].kwargs["comment"], r"\d{2}:\d{2}")

    def test_cancel_before_start_skips_engines(self):
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        job.cancel()
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task") as claude, \
                patch.object(rd_dispatch, "run_antigravity_task") as agy:
            res = run_dev_task("t", CLAUDE, job=job)
        self.assertTrue(res["cancelled"])
        claude.assert_not_called()
        agy.assert_not_called()

    def test_job_engine_label_follows_failover(self):
        labels = []

        def claude(*a, job=None, **k):
            labels.append(job.engine_label)
            return {"success": False, "error": "usage limit", "stdout": "", "stderr": ""}

        def agy(*a, job=None, **k):
            labels.append(job.engine_label)
            return {"success": True, "stdout": "ok", "stderr": ""}

        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
                patch.object(rd_dispatch, "run_claude_task", side_effect=claude), \
                patch.object(rd_dispatch, "run_antigravity_task", side_effect=agy):
            res = run_dev_task("t", CLAUDE)
        self.assertTrue(res["success"])
        self.assertEqual(labels, ["Claude Code", "Antigravity"])

    def test_gemini_path_announces_job(self):
        announced = []
        rd_jobs.set_announcer(announced.append)
        try:
            with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
                    patch.object(rd_dispatch, "run_claude_task", return_value={"success": True, "stdout": "ok"}), \
                    patch.object(rd_dispatch.ticket_tools, "create_ticket", return_value={"success": False, "error": "尚未設定"}) as create:
                rd_dispatch.run_claude_first("t")
            create.assert_called_once()  # Gemini 派工會開單 (opened_by=pm)；這裡只驗證按鈕通知，開單一律 mock
        finally:
            rd_jobs.set_announcer(None)
        self.assertEqual(len(announced), 1)


class TestCancelledReports(unittest.TestCase):
    RES = {**cancelled_result(), "cancelled": True, "engine": CLAUDE, "engine_label": "Claude Code",
           "scope": "core", "ticket": 25, "attempts": [],
           "worktree": {"status": " M agent.py", "diff_stat": " agent.py | 3 ++-"}}

    def test_user_report(self):
        text = format_dev_report(self.RES)
        for phrase in ("已停止", "Claude Code", "#25", "正在寫測試", "agent.py", "sess-1", "不會自動還原"):
            self.assertIn(phrase, text)
        self.assertNotIn("開發失敗", text)

    def test_model_view_is_compact(self):
        compact = compact_dev_result(self.RES)
        self.assertTrue(compact["cancelled"])
        self.assertFalse(compact["success"])
        self.assertEqual(compact["session_id"], "sess-1")
        self.assertIn("不要重新派工", compact["note"])
        self.assertNotIn("report_tail", compact)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), 600)


class _FakeProc:
    """取代 subprocess.Popen：stdout 逐行送出，讀到 cancel_at 那一行時呼叫 on_line (模擬使用者按停止)"""

    def __init__(self, lines, on_line=None, pid=4321):
        self.pid = pid
        self.stdin = io.StringIO()
        self._lines = lines
        self._on_line = on_line
        self.returncode = None

    @property
    def stdout(self):
        for i, line in enumerate(self._lines):
            if self._on_line:
                self._on_line(i)
            yield line + "\n"

    def wait(self):
        self.returncode = 1
        return 1


class TestEngineCancel(unittest.TestCase):
    def test_claude_returns_cancelled_with_session_and_last_output(self):
        from tools import claude_tools
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-9"}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "正在寫測試…"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "不該讀到"}]}}),
        ]
        killed = []
        proc = _FakeProc(lines, on_line=lambda i: job.cancel() if i == 2 else None)
        with patch.object(claude_tools, "_resolve_claude_executable", return_value="claude"), \
                patch.object(claude_tools.subprocess, "Popen", return_value=proc), \
                patch.object(rd_jobs, "_kill_tree", killed.append), \
                patch.object(claude_tools, "report_new_packages") as rnp:
            rnp.return_value.__enter__ = lambda s: None
            rnp.return_value.__exit__ = lambda s, *a: False
            res = claude_tools.run_claude_task("t", job=job)
        self.assertTrue(res["cancelled"])
        self.assertFalse(res["success"])
        self.assertEqual(res["session_id"], "sess-9")
        self.assertIn("正在寫測試", res["last_output"])
        self.assertEqual(killed, [4321])

    def test_agy_does_not_resume_after_cancel(self):
        from tools import antigravity_tools as at
        job = rd_jobs.DevJob(task="t", engine_label="Antigravity", scope="task")
        calls = []

        def run_once(*args, **kwargs):
            calls.append(1)
            job.cancel()
            # 正常情況下 interrupted 會觸發詢問後接續；停止後不得再接續
            return {"conversation_id": "conv-1", "response": "做到一半", "status": "", "interrupted": ("pip install x", "安裝新套件"),
                    "timed_out": False, "returncode": 1, "other_output": ""}

        asked = []
        with patch.object(at, "resolve_agy_executable", return_value="agy"), \
                patch.object(at, "_run_agy_once", side_effect=run_once), \
                patch.object(rd_jobs, "_kill_tree", lambda pid: None), \
                patch("builtins.print"):  # rd_dispatch 會 print emoji，避免 cp950 終端機編碼錯誤
            res = at.run_antigravity_task("t", approve=lambda t, d: asked.append(t) or True, job=job)
        self.assertEqual(len(calls), 1)
        self.assertEqual(asked, [])
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["session_id"], "conv-1")


class TestAnnounceChannel(unittest.TestCase):
    """：DM 頻道不在 discord.py 快取中，get_channel 會回傳 None；改為保存頻道物件並退回 fetch_channel"""

    def test_job_keeps_channel_object_from_origin(self):
        channel = object()

        def make():
            rd_jobs.ORIGIN.set({"owner_id": 5, "channel_id": 9, "channel": channel})
            return rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task")
        job = contextvars.copy_context().run(make)
        self.assertIs(job.channel, channel)

    def test_resolve_prefers_channel_object(self):
        import asyncio
        import bot
        channel = object()
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task", owner_id=1, channel_id=9)
        job.channel = channel
        with patch.object(bot.bot, "get_channel", return_value=None) as get:
            self.assertIs(asyncio.run(bot._resolve_job_channel(job)), channel)
        get.assert_not_called()

    def test_resolve_falls_back_to_fetch_channel_for_dm(self):
        import asyncio
        from unittest.mock import AsyncMock
        import bot
        dm = object()
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task", owner_id=1, channel_id=9)
        with patch.object(bot.bot, "get_channel", return_value=None), \
                patch.object(bot.bot, "fetch_channel", new=AsyncMock(return_value=dm)) as fetch:
            self.assertIs(asyncio.run(bot._resolve_job_channel(job)), dm)
        fetch.assert_awaited_once_with(9)

    def test_resolve_returns_none_when_unreachable(self):
        import asyncio
        from unittest.mock import AsyncMock
        import discord
        import bot
        job = rd_jobs.DevJob(task="t", engine_label="Claude Code", scope="task", owner_id=1, channel_id=9)
        err = discord.NotFound(type("R", (), {"status": 404, "reason": "Not Found"})(), "unknown channel")
        with patch.object(bot.bot, "get_channel", return_value=None), \
                patch.object(bot.bot, "fetch_channel", new=AsyncMock(side_effect=err)):
            self.assertIsNone(asyncio.run(bot._resolve_job_channel(job)))

    def test_on_message_passes_channel_object(self):
        import inspect
        import bot
        self.assertIn('"channel": message.channel', inspect.getsource(bot.on_message))


class TestBotStopCommand(unittest.TestCase):
    def test_stop_command_registered(self):
        import bot
        self.assertIn("stop", [c.name for c in bot.bot.tree.get_commands()])

    def test_jobs_embed(self):
        import bot
        rd_jobs._reset_for_tests()
        job = rd_jobs.DevJob(task="寫一個匯率爬蟲", engine_label="Claude Code", scope="core", ticket=25)
        embed = bot.build_jobs_embed([job.summary()])
        text = (embed.title or "") + (embed.description or "")
        for phrase in ("Claude Code", "寫一個匯率爬蟲", "#25", "core"):
            self.assertIn(phrase, text)
        self.assertIn("沒有", bot.build_jobs_embed([]).description)


if __name__ == "__main__":
    unittest.main()
