"""
 review 修正：MCP 加固（fake server，不連外；Gemini 以假物件取代）

驗收標準：
    - 等待核可卡或 server 連線途中，shutdown (/reboot) 不會被卡住，且之後不會留下新啟動的 server
    - reload 等待核可期間，既有連線照常可用
    - Gemini 拒絕 MCP 宣告 (400) 時，停用 MCP 宣告後重送，原生工具照常；其他 400 照原樣拋出
    - 修改核可紀錄 mcp_approved.json 與 mcp_servers.json 一樣需要同意
    - 名稱以 _KEY 等結尾的 env 一律只能整段引用 ${VAR}，展開值納入遮蔽
    - 沒有設定檔時 start_default 不啟動任何執行緒
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import psutil  # noqa: E402

import agent  # noqa: E402
from tools import mcp_config, mcp_manager, risk_policy, secret_guard  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def fake_server(*modes, **extra):
    return {"command": sys.executable, "args": [FAKE, *modes], **extra}


class HardeningCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "mcp_servers.json")
        self.approved = os.path.join(self.tmp.name, "approved.json")
        self.pids = os.path.join(self.tmp.name, "pids.json")
        self._saved_default = mcp_manager._default
        self.managers = []
        self.addCleanup(self._cleanup)
        p = mock.patch("builtins.print")
        p.start()
        self.addCleanup(p.stop)

    def _cleanup(self):
        for m in self.managers:
            m.shutdown()
        mcp_manager._default = self._saved_default
        secret_guard.register_extra_secrets([])

    def write(self, servers):
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f)

    def approve_names(self, *names):
        res = mcp_config.load_config(self.config, environ={})
        mcp_config.save_approved(self.approved, {n: c.fingerprint for n, c in res.servers.items() if n in names})

    def manager(self):
        m = mcp_manager.McpManager(self.config, self.approved, self.pids, environ={})
        self.managers.append(m)
        return m

    @staticmethod
    def live_pids(m):
        return [s.conn.pid for s in m._states.values() if s.conn is not None and s.conn.alive]


# ------------------------------------------------------------------ 1. shutdown 不被核可卡 / 連線卡住

class TestShutdownIsNeverBlocked(HardeningCase):
    def test_shutdown_returns_while_reload_waits_for_approval(self):
        self.write({"fake": fake_server()})
        m = self.manager()
        asked, release = threading.Event(), threading.Event()

        def approve(title, detail):
            asked.set()
            release.wait(30)
            return True

        reloader = threading.Thread(target=m.reload, args=(approve,), daemon=True)
        reloader.start()
        self.assertTrue(asked.wait(10))
        t0 = time.time()
        stopper = threading.Thread(target=m.shutdown, daemon=True)
        stopper.start()
        stopper.join(5)
        self.assertFalse(stopper.is_alive(), "shutdown 被等待中的核可卡擋住")
        self.assertLess(time.time() - t0, 5)
        release.set()
        reloader.join(30)
        self.assertEqual(self.live_pids(m), [], "shutdown 之後不應再啟動 server")
        self.assertFalse(os.path.exists(self.pids))

    def test_shutdown_interrupts_a_server_stuck_in_handshake(self):
        self.write({"slow": fake_server("--hang-init", startup_timeout=60)})
        self.approve_names("slow")
        m = self.manager()
        reloader = threading.Thread(target=m.reload, daemon=True)
        reloader.start()
        deadline = time.time() + 10
        pid = 0
        while time.time() < deadline and not pid:
            state = m._states.get("slow")
            pid = state.conn.pid if state is not None and state.conn is not None else 0
            time.sleep(0.05)
        self.assertTrue(pid, "連線中的 server 應該登記在狀態裡，shutdown 才關得到")
        t0 = time.time()
        m.shutdown()
        self.assertLess(time.time() - t0, 10)
        reloader.join(10)
        self.assertFalse(reloader.is_alive(), "reload 應隨 shutdown 結束，不必等 startup_timeout")
        deadline = time.time() + 10
        while time.time() < deadline and psutil.pid_exists(pid):
            time.sleep(0.1)
        self.assertFalse(psutil.pid_exists(pid))

    def test_reload_after_shutdown_still_works(self):
        self.write({"fake": fake_server()})
        self.approve_names("fake")
        m = self.manager()
        m.reload()
        m.shutdown()
        m.reload()
        self.assertEqual(len(self.live_pids(m)), 1)


class TestReloadKeepsOldConnectionsUntilApproved(HardeningCase):
    def test_existing_server_stays_usable_while_card_is_pending(self):
        self.write({"old": fake_server()})
        self.approve_names("old")
        m = self.manager()
        m.reload()
        self.write({"old": fake_server(), "new": fake_server()})
        seen = {}

        def approve(title, detail):
            seen["result"] = m.call("mcp__old__echo", {"message": "hi"})
            return False

        m.reload(approve)
        self.assertIn("hi", seen["result"].get("content", ""), seen["result"])
        self.assertIn("mcp__old__echo", [d.name for d in m.declarations()])


# ------------------------------------------------------------------ 2. Gemini 拒絕 MCP 宣告

class _BadRequest(Exception):
    code = 400


class _FakeChat:
    """config=None (預設工具含 MCP) 或明確帶 MCP 宣告時回 400；不含 MCP 時成功"""

    def __init__(self, reject_always=False):
        self.reject_always = reject_always
        self.sent = []

    @staticmethod
    def _has_mcp(config):
        if config is None:
            return bool(mcp_manager.declarations())
        for tool in config.tools or []:
            for decl in getattr(tool, "function_declarations", None) or []:
                if decl.name.startswith(mcp_manager.TOOL_PREFIX):
                    return True
        return False

    def send_message(self, payload, config=None):
        self.sent.append(config)
        if self.reject_always or self._has_mcp(config):
            raise _BadRequest("400 INVALID_ARGUMENT: function_declarations[14].parameters")
        return SimpleNamespace(text="ok")


def bare_agent():
    bot = agent.ArQAgent.__new__(agent.ArQAgent)
    bot.model_name = "test-model"
    bot.thinking_level = "LOW"
    bot.deep_thinking_level = "HIGH"
    bot.client = SimpleNamespace(vertexai=False)
    bot.prefix_cache = None
    return bot


class TestRejectedDeclarationsFallBack(HardeningCase):
    def attach(self):
        self.write({"fake": fake_server()})
        self.approve_names("fake")
        m = self.manager()
        mcp_manager._default = m
        m.reload()
        self.assertTrue(m.declarations())
        return m

    def test_400_with_mcp_suspends_declarations_and_resends_without_them(self):
        m = self.attach()
        version = m.version
        chat = _FakeChat()
        response = asyncio.run(bare_agent()._send_with_cache(chat, "hi", "LOW"))
        self.assertEqual(response.text, "ok")
        self.assertEqual(len(chat.sent), 2)
        self.assertEqual(m.declarations(), [])
        self.assertIsNone(mcp_manager.lookup("mcp__fake__echo"))
        self.assertGreater(m.version, version, "版本要變，既有對話下一則訊息才會重建")
        self.assertIn("停用", m.status_text())
        # 原生工具宣告仍完整
        names = [getattr(f, "__name__", "") for f in chat.sent[-1].tools]
        self.assertEqual(names, [spec.func.__name__ for spec in agent._TOOL_SPECS])

    def test_reload_restores_suspended_declarations(self):
        m = self.attach()
        asyncio.run(bare_agent()._send_with_cache(_FakeChat(), "hi", "LOW"))
        self.assertEqual(m.declarations(), [])
        m.reload()
        self.assertIn("mcp__fake__echo", [d.name for d in m.declarations()])
        self.assertNotIn("停用", m.status_text())

    def test_unrelated_400_is_raised_and_mcp_stays(self):
        m = self.attach()
        with self.assertRaises(_BadRequest):
            asyncio.run(bare_agent()._send_with_cache(_FakeChat(reject_always=True), "hi", "LOW"))
        self.assertTrue(m.declarations())

    def test_400_without_mcp_is_raised_unchanged(self):
        mcp_manager._default = None
        chat = _FakeChat(reject_always=True)
        with self.assertRaises(_BadRequest):
            asyncio.run(bare_agent()._send_with_cache(chat, "hi", "LOW"))
        self.assertEqual(len(chat.sent), 1)


# ------------------------------------------------------------------ 3. 核可紀錄也要同意

class TestApprovalRecordIsProtected(unittest.TestCase):
    def test_editing_the_approval_record_is_risky(self):
        for kind, text in (
            ("shell", "Set-Content _temp\\mcp_approved.json '{}'"),
            ("python", "open('_temp/mcp_approved.json', 'w').write('{}')"),
        ):
            with self.subTest(text=text):
                self.assertIn("MCP", risk_policy.classify_risk(text, kind) or "")


# ------------------------------------------------------------------ 4. _KEY 結尾的金鑰

class TestKeyNamedSecrets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, env_cfg, environ):
        path = os.path.join(self.tmp.name, "mcp_servers.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"a": {"command": "x", "env": env_cfg}}}, f)
        return mcp_config.load_config(path, environ=environ)

    def test_key_suffixed_names_cannot_be_hardcoded(self):
        for name in ("PRIVATE_KEY", "ACCESS_KEY", "CLIENT_SECRET_KEY", "DB_PASSWD", "SERVICE_CREDENTIALS"):
            with self.subTest(name=name):
                res = self.load({name: "hardcoded-value-123"}, {})
                self.assertIn("a", res.errors)

    def test_key_suffixed_reference_is_redacted(self):
        value = "pk-" + "q" * 20
        res = self.load({"PRIVATE_KEY": "${MY_PK}"}, {"MY_PK": value})
        self.assertEqual(res.servers["a"].env["PRIVATE_KEY"], value)
        self.assertIn(value, res.secret_values)

    def test_ordinary_names_are_not_treated_as_secrets(self):
        res = self.load({"MODE": "fast", "KEYBOARD_LAYOUT": "us", "MONKEY_COUNT": "3"}, {})
        self.assertEqual(res.errors, {})


# ------------------------------------------------------------------ 5. 沒有設定檔時不開執行緒

class TestStartDefaultWithoutConfig(HardeningCase):
    def test_no_config_file_means_no_thread_and_false(self):
        mcp_manager._default = None
        env = {"ARQ_MCP_CONFIG": self.config, "ARQ_MCP_APPROVED": self.approved, "ARQ_MCP_PIDS": self.pids}
        with mock.patch.dict(os.environ, env):
            self.assertFalse(mcp_manager.start_default())
        self.assertFalse(any(t.name == "mcp-startup" for t in threading.enumerate()))
        self.assertIsNone(mcp_manager._default)

    def test_no_config_still_cleans_up_stale_processes(self):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        with open(self.pids, "w", encoding="utf-8") as f:
            json.dump({"pids": [{"pid": proc.pid, "create_time": psutil.Process(proc.pid).create_time(),
                                 "server": "old"}]}, f)
        mcp_manager._default = None
        env = {"ARQ_MCP_CONFIG": self.config, "ARQ_MCP_APPROVED": self.approved, "ARQ_MCP_PIDS": self.pids}
        with mock.patch.dict(os.environ, env):
            self.assertFalse(mcp_manager.start_default())
        proc.wait(10)
        self.assertFalse(os.path.exists(self.pids))


# ------------------------------------------------------------------ 6. 額外機密清單的替換是原子的

class TestExtraSecretsAreSwappedAtomically(unittest.TestCase):
    def test_registering_does_not_break_concurrent_redaction(self):
        self.addCleanup(secret_guard.register_extra_secrets, [])
        stop = threading.Event()
        errors = []

        def churn():
            i = 0
            while not stop.is_set():
                secret_guard.register_extra_secrets([f"value-{i}-" + "x" * 10 for _ in range(50)] + [f"v{i}" * 8])
                i += 1

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            for _ in range(2000):
                try:
                    secret_guard.secret_values()
                except RuntimeError as e:
                    errors.append(e)
                    break
        finally:
            stop.set()
            t.join(5)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
