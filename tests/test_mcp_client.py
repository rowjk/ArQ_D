"""
 stdio MCP client（用 stdlib 寫的 fake server，不連外）

驗收標準：
    - 握手、tools/list 分頁、tools/call；對 server 發來的請求回 method-not-found
    - 呼叫逾時送 notifications/cancelled；崩潰 / 斷線 / 握手不回應 / 超長行都以例外回報，不卡住
    - 關閉時結束整個程序樹（含孫行程）
"""

import json
import os
import sys
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import psutil  # noqa: E402

from tools import mcp_client  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def connect(*modes, env=None, startup_timeout=15, **kwargs):
    conn = mcp_client.McpConnection(
        "fake", sys.executable, [FAKE, *modes], env={**os.environ, **(env or {})},
        startup_timeout=startup_timeout, **kwargs)
    return conn


def text_of(result):
    return "".join(c.get("text", "") for c in result["content"])


class ClientTestCase(unittest.TestCase):
    def open(self, *modes, **kwargs):
        conn = connect(*modes, **kwargs)
        self.addCleanup(conn.close)
        conn.start()
        return conn


class TestHandshakeAndCalls(ClientTestCase):
    def test_lists_tools_and_calls_one(self):
        conn = self.open()
        names = [t["name"] for t in conn.list_tools()]
        self.assertIn("echo", names)
        self.assertTrue(conn.alive)
        self.assertEqual(text_of(conn.call_tool("echo", {"message": "hi"}, timeout=10)), "echo: hi")

    def test_tools_list_follows_pagination(self):
        conn = self.open("--paginate")
        names = [t["name"] for t in conn.list_tools()]
        self.assertIn("extra-tool.v2", names)
        self.assertIn("echo", names)

    def test_unicode_roundtrip(self):
        conn = self.open()
        self.assertEqual(text_of(conn.call_tool("echo", {"message": "你好🙂"}, timeout=10)), "echo: 你好🙂")

    def test_server_requests_get_method_not_found(self):
        conn = self.open()
        deadline = time.time() + 5
        while time.time() < deadline and not any("CLIENT_RESPONSE" in l for l in conn.stderr_tail()):
            time.sleep(0.05)
        line = next(l for l in conn.stderr_tail() if "CLIENT_RESPONSE" in l)
        msg = json.loads(line.split("CLIENT_RESPONSE ", 1)[1])
        self.assertEqual(msg["id"], "srv-1")
        self.assertEqual(msg["error"]["code"], -32601)

    def test_json_rpc_error_raises_mcp_error(self):
        conn = self.open()
        with self.assertRaises(mcp_client.McpError):
            conn.call_tool("no-such-tool", {}, timeout=10)
        self.assertTrue(conn.alive)

    def test_concurrent_calls_are_matched_by_id(self):
        conn = self.open()
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(conn.call_tool, "echo", {"message": str(i)}, 10) for i in range(8)]
            results = sorted(text_of(f.result()) for f in futures)
        self.assertEqual(results, sorted(f"echo: {i}" for i in range(8)))


class TestFailureIsolation(ClientTestCase):
    def test_timeout_raises_and_sends_cancelled(self):
        conn = self.open()
        start = time.time()
        with self.assertRaises(mcp_client.McpTimeout):
            conn.call_tool("slow", {"seconds": 5}, timeout=0.5)
        self.assertLess(time.time() - start, 3)
        deadline = time.time() + 5
        while time.time() < deadline and not any(l.startswith("CANCELLED") for l in conn.stderr_tail()):
            time.sleep(0.05)
        self.assertTrue(any(l.startswith("CANCELLED") for l in conn.stderr_tail()))

    def test_crash_during_call_raises_disconnected_and_marks_dead(self):
        conn = self.open()
        with self.assertRaises(mcp_client.McpDisconnected):
            conn.call_tool("crash", {}, timeout=10)
        self.assertFalse(conn.alive)
        with self.assertRaises(mcp_client.McpDisconnected):
            conn.call_tool("echo", {"message": "x"}, timeout=1)

    def test_handshake_timeout(self):
        conn = connect("--hang-init", startup_timeout=1)
        self.addCleanup(conn.close)
        start = time.time()
        with self.assertRaises(mcp_client.McpTimeout):
            conn.start()
        self.assertLess(time.time() - start, 5)
        self.assertFalse(conn.alive)

    def test_exit_after_init_surfaces_on_list(self):
        conn = connect("--exit-after-init")
        self.addCleanup(conn.close)
        with self.assertRaises(mcp_client.McpError):
            conn.start()
            conn.list_tools()

    def test_missing_command(self):
        conn = mcp_client.McpConnection("x", "definitely-not-a-real-command-91", [], env={}, startup_timeout=2)
        with self.assertRaises(mcp_client.McpError):
            conn.start()

    def test_huge_line_drops_the_connection(self):
        conn = connect("--huge-line", max_line_bytes=100_000)
        self.addCleanup(conn.close)
        conn.start()
        with self.assertRaises(mcp_client.McpDisconnected):
            conn.list_tools()
        self.assertFalse(conn.alive)

    def test_stderr_flood_does_not_block_and_tail_is_bounded(self):
        conn = self.open("--stderr-flood")
        self.assertEqual(text_of(conn.call_tool("echo", {"message": "ok"}, timeout=10)), "echo: ok")
        tail = conn.stderr_tail()
        self.assertLessEqual(len(tail), mcp_client.STDERR_TAIL_LINES)
        self.assertTrue(all(len(line) <= mcp_client.STDERR_LINE_CHARS for line in tail))


class TestProcessLifecycle(ClientTestCase):
    def test_child_env_is_exactly_what_was_passed(self):
        os.environ["GEMINI_API_KEY"] = "g" * 20
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)
        from tools.secret_guard import child_env
        conn = mcp_client.McpConnection("fake", sys.executable, [FAKE], env=child_env(), startup_timeout=15)
        self.addCleanup(conn.close)
        conn.start()
        self.assertEqual(text_of(conn.call_tool("env", {"name": "GEMINI_API_KEY"}, timeout=10)), "GEMINI_API_KEY=absent")

    def test_close_kills_whole_process_tree(self):
        conn = self.open()
        grandchild = int(text_of(conn.call_tool("spawn_child", {}, timeout=10)))
        self.assertTrue(psutil.pid_exists(grandchild))
        server_pid = conn.pid
        conn.close()
        deadline = time.time() + 10
        while time.time() < deadline and (psutil.pid_exists(grandchild) or psutil.pid_exists(server_pid)):
            time.sleep(0.1)
        self.assertFalse(psutil.pid_exists(grandchild))
        self.assertFalse(psutil.pid_exists(server_pid))
        self.assertFalse(conn.alive)

    def test_close_is_idempotent(self):
        conn = self.open()
        conn.close()
        conn.close()

    def test_pid_and_create_time_are_exposed(self):
        conn = self.open()
        self.assertGreater(conn.pid, 0)
        self.assertAlmostEqual(conn.create_time, psutil.Process(conn.pid).create_time(), delta=1)


@unittest.skipUnless(sys.platform == "win32", "npx.cmd 這類批次檔只在 Windows 上需要")
class TestWindowsCmdWrapper(unittest.TestCase):
    def test_cmd_wrapper_works_without_shell_and_close_kills_the_whole_chain(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = os.path.join(tmp, "fakenpx.cmd")
            with open(wrapper, "w", encoding="ascii") as f:
                f.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, FAKE))
            env = {**os.environ, "PATH": tmp + os.pathsep + os.environ.get("PATH", "")}
            conn = mcp_client.McpConnection("wrap", "fakenpx", [], env=env, startup_timeout=20)
            self.addCleanup(conn.close)
            conn.start()
            self.assertEqual(text_of(conn.call_tool("echo", {"message": "via cmd"}, timeout=10)), "echo: via cmd")
            grandchild = int(text_of(conn.call_tool("spawn_child", {}, timeout=10)))
            chain = [p.pid for p in psutil.Process(conn.pid).children(recursive=True)]
            self.assertIn(grandchild, chain)
            self.assertGreaterEqual(len(chain), 2)  # cmd.exe → python server → 孫行程
            tree = [conn.pid] + chain
            conn.close()
            deadline = time.time() + 10
            while time.time() < deadline and any(psutil.pid_exists(pid) for pid in tree):
                time.sleep(0.1)
            self.assertFalse([pid for pid in tree if psutil.pid_exists(pid)])


if __name__ == "__main__":
    unittest.main()
