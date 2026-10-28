"""
 RD 逾時但有進展時不要換人從頭做

驗收標準：
    - 改看「多久沒動靜」：連續 N 秒沒有任何事件才算沒反應；總時長另設寬鬆上限；等使用者同意的時間兩者都不計
    - 逾時前有進展：不換人，帶 session_id 交回同一位 RD 接續收尾一次；仍逾時就停下回報 (附 session_id)，不換人
    - 一開始就沒有任何輸出 (真的沒反應)、認證、額度、程序異常：照舊換人
    - Claude 與 Antigravity 行為一致；通知與回報寫清楚是哪種逾時
"""

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

from tools import rd_common, rd_dispatch, ticket_tools  # noqa: E402
from tools.rd_common import Watchdog, rd_timeouts  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE, run_dev_task  # noqa: E402

_no_github = patch.object(ticket_tools, "_send", side_effect=AssertionError("單元測試不得連線 GitHub"))


def setUpModule():
    _no_github.start()


def tearDownModule():
    _no_github.stop()


def wait_for(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ------------------------------------------------------------------ 計時器

class TestWatchdog(unittest.TestCase):
    def make(self, idle, total):
        fired = []
        dog = Watchdog(idle, total, fired.append, interval=0.01)
        self.addCleanup(dog.cancel)
        dog.start()
        return dog, fired

    def test_idle_fires_when_nothing_happens(self):
        dog, fired = self.make(0.15, 10)
        self.assertTrue(wait_for(lambda: fired))
        self.assertEqual(fired, [rd_common.TIMEOUT_IDLE])
        self.assertEqual(dog.fired, rd_common.TIMEOUT_IDLE)

    def test_activity_keeps_it_alive_until_total(self):
        dog, fired = self.make(0.15, 0.6)
        t0 = time.time()
        while not fired and time.time() - t0 < 3:
            dog.touch(progress=True)
            time.sleep(0.03)
        self.assertEqual(fired, [rd_common.TIMEOUT_TOTAL])
        self.assertGreaterEqual(time.time() - t0, 0.55)

    def test_paused_time_does_not_count(self):
        dog, fired = self.make(0.15, 0.3)
        dog.pause()
        time.sleep(0.5)  # 等使用者同意：超過 idle 與 total 也不觸發
        self.assertEqual(fired, [])
        dog.resume()
        self.assertTrue(wait_for(lambda: fired))
        self.assertEqual(fired, [rd_common.TIMEOUT_IDLE])

    def test_fires_only_once_and_cancel_stops_it(self):
        dog, fired = self.make(0.05, 10)
        self.assertTrue(wait_for(lambda: fired))
        time.sleep(0.2)
        self.assertEqual(len(fired), 1)
        other, other_fired = self.make(0.05, 10)
        other.cancel()
        time.sleep(0.2)
        self.assertEqual(other_fired, [])

    def test_progress_only_counts_meaningful_events(self):
        dog, _ = self.make(5, 10)
        dog.touch()  # 例如 init 事件：有動靜但不算進展
        self.assertFalse(dog.progressed)
        dog.touch(progress=True)
        self.assertTrue(dog.progressed)


class TestTimeoutSettings(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in ("CLAUDE_IDLE_TIMEOUT", "CLAUDE_TIMEOUT"):
                os.environ.pop(k, None)
            self.assertEqual(rd_timeouts("CLAUDE"), (rd_common.DEFAULT_IDLE_TIMEOUT, rd_common.DEFAULT_TOTAL_TIMEOUT))
        self.assertEqual((rd_common.DEFAULT_IDLE_TIMEOUT, rd_common.DEFAULT_TOTAL_TIMEOUT), (600, 3600))

    def test_overrides_and_bad_values(self):
        with patch.dict(os.environ, {"AGY_IDLE_TIMEOUT": "120", "AGY_TIMEOUT": "abc"}):
            self.assertEqual(rd_timeouts("AGY"), (120, rd_common.DEFAULT_TOTAL_TIMEOUT))
        with patch.dict(os.environ, {"AGY_IDLE_TIMEOUT": "0", "AGY_TIMEOUT": "-5"}):
            self.assertEqual(rd_timeouts("AGY"), (rd_common.DEFAULT_IDLE_TIMEOUT, rd_common.DEFAULT_TOTAL_TIMEOUT))

    def test_idle_never_exceeds_total(self):
        with patch.dict(os.environ, {"CLAUDE_IDLE_TIMEOUT": "900", "CLAUDE_TIMEOUT": "300"}):
            self.assertEqual(rd_timeouts("CLAUDE"), (300, 300))


# ------------------------------------------------------------------ 派工：有進展的逾時不換人

def ok(text="完成", session="sess-1"):
    return {"success": True, "stdout": text, "stderr": "", "returncode": 0, "session_id": session}


def timed_out(kind="idle", progressed=True, session="sess-1"):
    return {"success": False, "stdout": "", "stderr": "", "returncode": -1, "session_id": session,
            "timed_out": kind, "progressed": progressed, "error": f"逾時（{kind}）", "last_output": "正在補文件"}


OK_AUTH = {"authenticated": True, "message": "ok"}


class TestDispatchOnTimeout(unittest.TestCase):
    def run_task(self, claude_results, agy_results=None, preferred=CLAUDE):
        notes = []
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
             patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
             patch.object(rd_dispatch, "run_claude_task", side_effect=claude_results) as m_claude, \
             patch.object(rd_dispatch, "run_antigravity_task", side_effect=agy_results or []) as m_agy, \
             patch("builtins.print"):
            res = run_dev_task("寫一個爬蟲", preferred, notify=notes.append)
        return res, notes, m_claude, m_agy

    def test_progressed_timeout_resumes_same_engine_with_session(self):
        res, notes, m_claude, m_agy = self.run_task([timed_out(), ok("收尾完成")])
        self.assertTrue(res["success"])
        self.assertEqual(res["engine"], CLAUDE)
        m_agy.assert_not_called()
        self.assertEqual(m_claude.call_count, 2)
        second = m_claude.call_args_list[1]
        self.assertEqual(second.kwargs["session_id"], "sess-1")
        self.assertIn("逾時", second.args[0])
        self.assertNotIn("接手說明", second.args[0])
        self.assertTrue(any("接續" in n and "Claude Code" in n for n in notes), notes)
        self.assertFalse(any("改由 **Antigravity** 接手" in n for n in notes))

    def test_second_timeout_stops_without_failover(self):
        res, notes, m_claude, m_agy = self.run_task([timed_out(), timed_out("total", session="sess-2")])
        self.assertFalse(res["success"])
        m_agy.assert_not_called()
        self.assertEqual(res["engine"], CLAUDE)
        self.assertEqual(res["timed_out"], "total")
        self.assertEqual(res["session_id"], "sess-2")
        self.assertIn("未改派", res["error"])
        report = rd_dispatch.format_dev_report(res)
        self.assertIn("sess-2", report)
        compact = rd_dispatch.compact_dev_result(res)
        self.assertEqual(compact["timed_out"], "total")
        self.assertIn("不要改派", compact["note"])

    def test_progressed_timeout_without_session_stops_without_failover(self):
        res, _, m_claude, m_agy = self.run_task([timed_out(session="")])
        self.assertFalse(res["success"])
        m_agy.assert_not_called()
        self.assertEqual(m_claude.call_count, 1)

    def test_timeout_without_any_progress_still_fails_over(self):
        res, notes, m_claude, m_agy = self.run_task([timed_out(progressed=False)], [ok("agy 完成")])
        self.assertTrue(res["success"])
        self.assertEqual(res["engine"], ANTIGRAVITY)
        self.assertIn("接手說明", m_agy.call_args.args[0])
        self.assertTrue(any("改由 **Antigravity** 接手" in n for n in notes))

    def test_agy_progressed_timeout_resumes_agy(self):
        res, _, m_claude, m_agy = self.run_task([], [timed_out(session="conv-1"), ok("agy 收尾")], preferred=ANTIGRAVITY)
        self.assertTrue(res["success"])
        self.assertEqual(res["engine"], ANTIGRAVITY)
        m_claude.assert_not_called()
        self.assertEqual(m_agy.call_args_list[1].kwargs["session_id"], "conv-1")

    def test_ordinary_failure_still_fails_over(self):
        fail = {"success": False, "stdout": "", "stderr": "", "returncode": 1, "error": "usage limit reached"}
        res, _, _, m_agy = self.run_task([fail], [ok("agy 完成")])
        self.assertTrue(res["success"])
        m_agy.assert_called_once()


# ------------------------------------------------------------------ 引擎：Claude / Antigravity 回報逾時種類與進展

class _SlowProc:
    """取代 subprocess.Popen：逐行送出 (每行之間 delay 秒)，被 kill 後停止輸出"""

    def __init__(self, lines, delay=0.0, hang_after=True, pid=4321):
        self.pid = pid
        self.stdin = io.StringIO()
        self._lines = lines
        self._delay = delay
        self._hang_after = hang_after
        self.killed = threading.Event()
        self.returncode = None

    @property
    def stdout(self):
        for line in self._lines:
            if self.killed.wait(self._delay):
                return
            yield line + "\n"
        if self._hang_after:
            self.killed.wait(10)

    def wait(self):
        self.returncode = 1
        return 1


def _claude_run(proc, env):
    from tools import claude_tools
    with patch.dict(os.environ, env), \
            patch.object(claude_tools, "_resolve_claude_executable", return_value="claude"), \
            patch.object(claude_tools.subprocess, "Popen", return_value=proc), \
            patch.object(claude_tools, "_kill_process_tree", lambda pid: proc.killed.set()), \
            patch.object(claude_tools, "report_new_packages") as rnp:
        rnp.return_value.__enter__ = lambda s: None
        rnp.return_value.__exit__ = lambda s, *a: False
        return claude_tools.run_claude_task("t")


INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-9"})
WORK = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "正在寫測試…"}]}})


class TestClaudeEngineTimeout(unittest.TestCase):
    def test_idle_after_progress(self):
        res = _claude_run(_SlowProc([INIT, WORK]), {"CLAUDE_IDLE_TIMEOUT": "0.2", "CLAUDE_TIMEOUT": "30"})
        self.assertFalse(res["success"])
        self.assertEqual(res["timed_out"], rd_common.TIMEOUT_IDLE)
        self.assertTrue(res["progressed"])
        self.assertEqual(res["session_id"], "sess-9")
        self.assertIn("沒有任何動靜", res["error"])

    def test_no_progress_when_only_init(self):
        res = _claude_run(_SlowProc([INIT]), {"CLAUDE_IDLE_TIMEOUT": "0.2", "CLAUDE_TIMEOUT": "30"})
        self.assertEqual(res["timed_out"], rd_common.TIMEOUT_IDLE)
        self.assertFalse(res["progressed"])

    def test_steady_output_is_not_idle_but_hits_total(self):
        res = _claude_run(_SlowProc([INIT] + [WORK] * 200, delay=0.05),
                          {"CLAUDE_IDLE_TIMEOUT": "0.3", "CLAUDE_TIMEOUT": "0.8"})
        self.assertEqual(res["timed_out"], rd_common.TIMEOUT_TOTAL)
        self.assertTrue(res["progressed"])
        self.assertIn("總時長", res["error"])


class TestAgyEngineTimeout(unittest.TestCase):
    def run_once(self, lines, env):
        from tools import antigravity_tools as at
        proc = _SlowProc(lines)
        with patch.dict(os.environ, env), \
                patch.object(at.subprocess, "Popen", return_value=proc), \
                patch.object(at, "_kill_process_tree", lambda pid: proc.killed.set()):
            idle, total = rd_timeouts("AGY")
            return at._run_agy_once("agy", "p", "m", None, (idle, total), set())

    def test_idle_after_step_is_progress(self):
        step = json.dumps({"event": "step_update", "step_update": {"state": "DONE"}})
        init = json.dumps({"event": "init", "conversation_id": "conv-1"})
        run = self.run_once([init, step], {"AGY_IDLE_TIMEOUT": "0.2", "AGY_TIMEOUT": "30"})
        self.assertEqual(run["timed_out"], rd_common.TIMEOUT_IDLE)
        self.assertTrue(run["progressed"])
        self.assertEqual(run["conversation_id"], "conv-1")

    def test_only_init_is_not_progress(self):
        init = json.dumps({"event": "init", "conversation_id": "conv-1"})
        run = self.run_once([init], {"AGY_IDLE_TIMEOUT": "0.2", "AGY_TIMEOUT": "30"})
        self.assertEqual(run["timed_out"], rd_common.TIMEOUT_IDLE)
        self.assertFalse(run["progressed"])

    def test_task_result_carries_kind_and_progress(self):
        from tools import antigravity_tools as at
        once = {"conversation_id": "conv-1", "response": "", "status": "", "interrupted": None,
                "timed_out": rd_common.TIMEOUT_TOTAL, "progressed": True, "returncode": 1, "other_output": ""}
        with patch.object(at, "resolve_agy_executable", return_value="agy"), \
                patch.object(at, "_run_agy_once", return_value=once), \
                patch("builtins.print"):
            res = at.run_antigravity_task("t")
        self.assertFalse(res["success"])
        self.assertEqual(res["timed_out"], rd_common.TIMEOUT_TOTAL)
        self.assertTrue(res["progressed"])
        self.assertEqual(res["session_id"], "conv-1")
        self.assertIn("總時長", res["error"])


if __name__ == "__main__":
    unittest.main()
