"""
測試期間封鎖對外連線

匯入本模組即生效 (重複匯入不會重複安裝)：
    - patch socket.getaddrinfo、socket.socket.connect / connect_ex，
      以及 Windows Proactor 事件迴圈的 sock_connect (它走 ConnectEx，不經過 socket.connect)
    - 只放行 localhost / 127.0.0.0/8 / ::1 / 綁定用的位址；其餘一律丟 NetworkAccessBlocked
    - 連外嘗試會記錄下來；即使程式把例外吞掉，該測試仍會被判定失敗
      (unittest：包住 TestCase.run；pytest：tests/conftest.py 的 autouse fixture)

每個 tests/test_*.py 都要在 sys.path 設定之後加上：
    from tests import netguard  # noqa: E402,F401  測試禁止連外
test_network_guard.py 會檢查有沒有漏掉。

測試本身要驗證「會被擋」時，用 `with netguard.expect_blocked():` 包住，不計入違規。
"""

import contextlib
import ipaddress
import os
import socket
import sys
import threading
import unittest

_STATE_ATTR = "_arq_netguard_state"


class NetworkAccessBlocked(ConnectionError):
    """測試嘗試連外。繼承 OSError，程式會照常當成網路失敗處理"""


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.violations = []  # [(test_id, message)]
        self.current_test = None
        self.expect_depth = 0


def _state() -> _State:
    # 存在 socket 模組上：同一個 process 不論模組被匯入幾次 (tests.netguard / netguard) 都共用
    return getattr(socket, _STATE_ATTR)


def _current_test() -> str:
    return _state().current_test or os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0] or "未知測試"


def _is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).strip().strip("[]").lower()
    if host in ("", "localhost") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _check(host, port) -> None:
    if _is_local_host(host):
        return
    test_id = _current_test()
    message = f"測試禁止連外：{test_id} 嘗試連到 {host}:{port}（請改用 mock，見 GEMINI.md 測試規範）"
    st = _state()
    with st.lock:
        if st.expect_depth == 0:
            st.violations.append((test_id, message))
    raise NetworkAccessBlocked(message)


def _check_address(address) -> None:
    if isinstance(address, tuple) and len(address) >= 2:
        _check(address[0], address[1])


@contextlib.contextmanager
def expect_blocked():
    """測試本身就是要驗證連外會被擋時使用：照樣丟例外，但不計入違規"""
    st = _state()
    with st.lock:
        st.expect_depth += 1
    try:
        yield
    finally:
        with st.lock:
            st.expect_depth -= 1


def pop_violations(test_id: str = None) -> list:
    """取出 (並清除) 已記錄的違規訊息；給 conftest / TestCase.run 包裝使用"""
    st = _state()
    with st.lock:
        taken = [m for t, m in st.violations if test_id is None or t == test_id]
        st.violations = [(t, m) for t, m in st.violations if not (test_id is None or t == test_id)]
    return taken


def _install() -> None:
    if hasattr(socket, _STATE_ATTR):
        return
    setattr(socket, _STATE_ATTR, _State())
    # 測試模式旗標：tools/worktree.py 看到它就拒絕在 ArQ 本身建立 worktree (測試曾因此建出真的 worktree)
    os.environ["ARQ_TESTING"] = "1"

    orig_getaddrinfo = socket.getaddrinfo
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, port, *args, **kwargs):
        _check(host, port)
        return orig_getaddrinfo(host, port, *args, **kwargs)

    def connect(self, address):
        _check_address(address)
        return orig_connect(self, address)

    def connect_ex(self, address):
        _check_address(address)
        return orig_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex

    if sys.platform == "win32":
        from asyncio import proactor_events

        orig_sock_connect = proactor_events.BaseProactorEventLoop.sock_connect

        async def sock_connect(self, sock, address):
            _check_address(address)
            return await orig_sock_connect(self, sock, address)

        proactor_events.BaseProactorEventLoop.sock_connect = sock_connect

    orig_run = unittest.TestCase.run

    def run(self, result=None):
        st = _state()
        test_id = self.id()
        previous, st.current_test = st.current_test, test_id
        try:
            out = orig_run(self, result)
        finally:
            st.current_test = previous
        violations = pop_violations(test_id)
        result = out if result is None else result
        if violations and result is not None:
            # pytest 的 result 轉接物件沒有 failures / errors
            reported = list(getattr(result, "failures", [])) + list(getattr(result, "errors", []))
            already_failed = any(t is self for t, _ in reported)
            if not already_failed:
                try:
                    raise AssertionError("\n".join(violations))
                except AssertionError:
                    result.addFailure(self, sys.exc_info())
        return out

    unittest.TestCase.run = run


_install()
