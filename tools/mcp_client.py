"""
最小的 MCP stdio client：只用標準函式庫 + psutil，不依賴官方 `mcp` 套件

協定：以換行分隔的 JSON-RPC 2.0 走子程序的 stdin / stdout。
    initialize → notifications/initialized → tools/list (分頁) → tools/call

設計重點：
    - 一條 reader 執行緒讀 stdout、一條 drain 執行緒讀 stderr (只留最後幾行)，呼叫端用 Future 等待，
      因此任何卡住 / 崩潰 / 狂寫都不會拖住 Discord 事件迴圈或工具迴圈
    - 逾時送 notifications/cancelled 並丟 McpTimeout；斷線、崩潰、超長行一律丟 McpDisconnected，
      所有等待中的請求同時失敗
    - server 發來的請求 (ping、roots/list、sampling…) 一律回 method-not-found，不讓對方卡住
    - close() 結束整個程序樹 (npx.cmd → node 的孫行程也要收乾淨)
"""

import collections
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any, Deque, Dict, List, Mapping, Optional

import psutil

PROTOCOL_VERSION = "2025-06-18"
MAX_LINE_BYTES = 2_000_000
STDERR_TAIL_LINES = 40
STDERR_LINE_CHARS = 300
MAX_LIST_PAGES = 20


class McpError(Exception):
    """MCP 錯誤 (server 回傳 JSON-RPC error、協定錯誤、啟動失敗…)"""


class McpTimeout(McpError):
    pass


class McpDisconnected(McpError):
    pass


def kill_process_tree(pid: int) -> None:
    """結束程序與它的所有子孫 (先收集再殺，避免父程序死後找不到孫行程)"""
    victims: List[psutil.Process] = []
    try:
        root = psutil.Process(pid)
        victims = root.children(recursive=True) + [root]
    except psutil.Error:
        return
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=5)


class McpConnection:
    def __init__(self, name: str, command: str, args: List[str], env: Mapping[str, str],
                 cwd: Optional[str] = None, startup_timeout: float = 60, call_timeout: float = 30,
                 max_line_bytes: int = MAX_LINE_BYTES):
        self.name = name
        self.command = command
        self.args = list(args)
        self.env = dict(env)
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        self.max_line_bytes = max_line_bytes
        self.pid: int = 0
        self.create_time: float = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._pending: Dict[int, Future] = {}
        self._lock = threading.Lock()          # 保護 _pending / _next_id / _dead
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._dead: Optional[str] = None
        self._closed = False
        self._stderr: Deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._threads: List[threading.Thread] = []

    # ---------------------------------------------------------------- 狀態

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._dead is None and self._proc.poll() is None

    def stderr_tail(self) -> List[str]:
        return list(self._stderr)

    # ---------------------------------------------------------------- 啟動

    def start(self) -> None:
        """啟動子程序並完成握手；失敗時關閉並丟 McpError / McpTimeout"""
        exe = shutil.which(self.command, path=self.env.get("PATH")) or shutil.which(self.command)
        if exe is None:
            raise McpError(f"找不到指令：{self.command}")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(
                [exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.env, cwd=self.cwd, creationflags=flags)
        except OSError as e:
            raise McpError(f"無法啟動 {self.command}：{type(e).__name__}") from e
        self.pid = self._proc.pid
        if self._closed:  # 啟動途中被 close() (例如 /reboot)：當時還沒有程序可殺，這裡補殺並中止
            self._kill_tree()
            raise McpDisconnected("連線已關閉")
        try:
            self.create_time = psutil.Process(self.pid).create_time()
        except psutil.Error:
            self.create_time = 0.0
        for target in (self._read_stdout, self._drain_stderr):
            t = threading.Thread(target=target, name=f"mcp-{self.name}-{target.__name__}", daemon=True)
            t.start()
            self._threads.append(t)
        try:
            self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "arq", "version": "1"}}, timeout=self.startup_timeout)
            self.notify("notifications/initialized")
        except McpError:
            self.close()
            raise

    # ---------------------------------------------------------------- 傳輸

    def _send(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as e:
            self._mark_dead("無法寫入 server (程序已結束)")
            raise McpDisconnected("無法寫入 server (程序已結束)") from e

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
        future: Future = Future()
        with self._lock:
            if self._dead:
                raise McpDisconnected(self._dead)
            self._next_id += 1
            req_id = self._next_id
            self._pending[req_id] = future
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._send(msg)
            return future.result(timeout=timeout)
        except FutureTimeout:
            with self._lock:
                self._pending.pop(req_id, None)
            try:
                self.notify("notifications/cancelled", {"requestId": req_id, "reason": "timeout"})
            except McpError:
                pass
            raise McpTimeout(f"{method} 逾時 ({timeout} 秒)") from None
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

    def _mark_dead(self, reason: str) -> None:
        with self._lock:
            if self._dead is None:
                self._dead = reason
            pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(McpDisconnected(reason))

    def _read_stdout(self) -> None:
        stream = self._proc.stdout
        try:
            while True:
                line = stream.readline(self.max_line_bytes + 1)
                if not line:
                    break
                if len(line) > self.max_line_bytes:
                    self._mark_dead(f"server 回傳超過 {self.max_line_bytes} 位元組的單行訊息，已中斷")
                    self._kill_tree()
                    return
                self._handle_line(line)
        except (OSError, ValueError):
            pass
        self._mark_dead("server 已斷線或結束" if not self._closed else "連線已關閉")

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            return  # 非 JSON 的輸出 (例如 server 把 log 印到 stdout) 直接忽略
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            if "id" in msg:  # server → client 請求：不支援任何能力
                try:
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "method not found"}})
                except McpError:
                    pass
            return
        with self._lock:
            future = self._pending.pop(msg.get("id"), None)
        if future is None or future.done():
            return
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            future.set_exception(McpError(f"server 回報錯誤 {err.get('code', '')}: {str(err.get('message', ''))[:300]}"))
        else:
            future.set_result(msg.get("result"))

    def _drain_stderr(self) -> None:
        try:
            for raw in iter(lambda: self._proc.stderr.readline(4096), b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr.append(text[:STDERR_LINE_CHARS])
        except (OSError, ValueError):
            pass

    # ---------------------------------------------------------------- MCP 操作

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(MAX_LIST_PAGES):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=self.startup_timeout)
            result = result if isinstance(result, dict) else {}
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments},
                              timeout=self.call_timeout if timeout is None else timeout)
        if not isinstance(result, dict):
            raise McpError("tools/call 回傳格式不正確")
        return result

    # ---------------------------------------------------------------- 關閉

    def _kill_tree(self) -> None:
        if self._proc is not None:
            kill_process_tree(self._proc.pid)
            try:
                self._proc.kill()
            except OSError:
                pass

    def close(self) -> None:
        """結束整個程序樹；可重複呼叫"""
        self._closed = True
        self._mark_dead("連線已關閉")
        self._kill_tree()
        proc = self._proc
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError, AttributeError):
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2)
