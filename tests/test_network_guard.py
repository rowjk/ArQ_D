"""
測試全面禁止連外網路

對應驗收標準：
    - 測試啟動時統一封鎖對外連線 (patch socket)，只放行 localhost
    - 有測試嘗試連外時立刻失敗，錯誤訊息指出是哪個測試、要連到哪裡；
      即使程式把例外吞掉 (except Exception)，該測試仍判定失敗
    - 不論用 unittest 或 pytest 執行都有效：每個測試檔都要匯入 tests.netguard，
      tests/conftest.py 也要匯入 (pytest 專用)
"""

import asyncio
import os
import re
import socket
import sys
import unittest
import urllib.request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

TESTS_DIR = os.path.join(PROJECT_ROOT, "tests")
GUARD_IMPORT = re.compile(r"^from tests import netguard\b", re.MULTILINE)


class TestBlocksExternal(unittest.TestCase):
    def test_socket_connect_to_external_ip_blocked(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with netguard.expect_blocked():
                with self.assertRaises(netguard.NetworkAccessBlocked) as cm:
                    s.connect(("8.8.8.8", 53))
        finally:
            s.close()
        msg = str(cm.exception)
        self.assertIn("8.8.8.8:53", msg)
        self.assertIn("test_socket_connect_to_external_ip_blocked", msg)

    def test_connect_ex_blocked(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with netguard.expect_blocked():
                with self.assertRaises(netguard.NetworkAccessBlocked):
                    s.connect_ex(("1.1.1.1", 443))
        finally:
            s.close()

    def test_dns_lookup_of_external_host_blocked(self):
        with netguard.expect_blocked():
            with self.assertRaises(netguard.NetworkAccessBlocked) as cm:
                socket.getaddrinfo("api.github.com", 443)
        self.assertIn("api.github.com:443", str(cm.exception))

    def test_urllib_blocked(self):
        with netguard.expect_blocked():
            with self.assertRaises(OSError) as cm:
                urllib.request.urlopen("https://api.github.com/", timeout=5)
        self.assertIn("api.github.com", str(cm.exception))

    def test_asyncio_open_connection_to_external_ip_blocked(self):
        async def go():
            await asyncio.open_connection("8.8.8.8", 53)

        with netguard.expect_blocked():
            with self.assertRaises(netguard.NetworkAccessBlocked):
                asyncio.run(go())

    def test_blocked_error_is_oserror(self):
        """程式碼照常把它當成網路失敗處理，不會在奇怪的地方崩潰"""
        self.assertTrue(issubclass(netguard.NetworkAccessBlocked, OSError))


class TestAllowsLocalhost(unittest.TestCase):
    def test_loopback_connect_allowed(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(server.getsockname())
        finally:
            client.close()
            server.close()

    def test_localhost_name_allowed(self):
        self.assertTrue(socket.getaddrinfo("localhost", 80))

    def test_bind_lookup_allowed(self):
        self.assertTrue(socket.getaddrinfo(None, 0, flags=socket.AI_PASSIVE))

    def test_asyncio_event_loop_still_works(self):
        async def go():
            await asyncio.sleep(0)
            return 42

        self.assertEqual(asyncio.run(go()), 42)


class TestSwallowedErrorStillFails(unittest.TestCase):
    def test_swallowed_violation_marks_test_failed(self):
        class Inner(unittest.TestCase):
            def test_swallow(self):
                try:
                    socket.getaddrinfo("example.com", 80)
                except Exception:
                    pass

        result = unittest.TestResult()
        Inner("test_swallow").run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("example.com:80", result.failures[0][1])
        self.assertIn("test_swallow", result.failures[0][1])

    def test_raised_violation_reported_once(self):
        class Inner(unittest.TestCase):
            def test_raise(self):
                socket.getaddrinfo("example.com", 80)

        result = unittest.TestResult()
        Inner("test_raise").run(result)
        self.assertEqual(len(result.failures) + len(result.errors), 1)

    def test_clean_test_passes(self):
        class Inner(unittest.TestCase):
            def test_ok(self):
                socket.getaddrinfo("localhost", 80)

        result = unittest.TestResult()
        Inner("test_ok").run(result)
        self.assertTrue(result.wasSuccessful())


class TestEveryTestFileImportsGuard(unittest.TestCase):
    def test_all_test_files_import_guard(self):
        missing = []
        for name in sorted(os.listdir(TESTS_DIR)):
            if name.startswith("test_") and name.endswith(".py"):
                with open(os.path.join(TESTS_DIR, name), encoding="utf-8") as f:
                    if not GUARD_IMPORT.search(f.read()):
                        missing.append(name)
        self.assertEqual(missing, [], "這些測試檔沒有匯入 tests.netguard (單獨執行時不會封鎖連外)")

    def test_conftest_imports_guard(self):
        path = os.path.join(TESTS_DIR, "conftest.py")
        self.assertTrue(os.path.exists(path), "pytest 需要 tests/conftest.py")
        with open(path, encoding="utf-8") as f:
            self.assertRegex(f.read(), GUARD_IMPORT)


if __name__ == "__main__":
    unittest.main()
