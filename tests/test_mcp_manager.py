"""
 MCP 管理員（設定 → 連線 → 宣告 → 呼叫 → 關閉；用 fake server，不連外）

驗收標準：
    - 工具轉成 mcp__<server>__<tool> 宣告，MCP 宣告另有預算；include / exclude 過濾
    - 每次呼叫預設要同意；auto_approve 只看該 server 的完整工具名稱
    - 未核可（新增 / 指紋變更）的 server 不啟動；reload 時跳卡核可
    - 子程序環境去除核心金鑰；結果先遮蔽再截斷並標示外部資料；圖片等以佔位文字取代
    - 逾時 / 崩潰 / 啟動失敗只影響該 server；shutdown 與下次啟動清掉殘留程序
    - 沒有設定時什麼都不做
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import psutil  # noqa: E402

from tools import mcp_config, mcp_manager, secret_guard  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def fake_server(*modes, **extra):
    return {"command": sys.executable, "args": [FAKE, *modes], **extra}


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "mcp_servers.json")
        self.approved = os.path.join(self.tmp.name, "approved.json")
        self.pids = os.path.join(self.tmp.name, "pids.json")
        self.managers = []
        self.addCleanup(self._shutdown_all)

    def _shutdown_all(self):
        for m in self.managers:
            m.shutdown()
        secret_guard.register_extra_secrets([])

    def write(self, servers):
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f)

    def approve_all(self, environ=None):
        res = mcp_config.load_config(self.config, environ=environ or {})
        mcp_config.save_approved(self.approved, {n: c.fingerprint for n, c in res.servers.items()})

    def manager(self, servers=None, approve=True, environ=None, **kwargs):
        if servers is not None:
            self.write(servers)
        if approve:
            self.approve_all(environ)
        m = mcp_manager.McpManager(self.config, self.approved, self.pids, environ=environ or {}, **kwargs)
        self.managers.append(m)
        return m

    def started(self, servers, **kwargs):
        m = self.manager(servers, **kwargs)
        m.reload()
        return m


def result_text(res):
    return res.get("content") or res.get("error") or ""


class TestNaming(unittest.TestCase):
    def test_basic_and_sanitized(self):
        self.assertEqual(mcp_manager.make_tool_name("fake", "echo"), "mcp__fake__echo")
        self.assertEqual(mcp_manager.make_tool_name("fake", "a b/c:d"), "mcp__fake__a_b_c_d")
        self.assertEqual(mcp_manager.make_tool_name("fake", "extra-tool.v2"), "mcp__fake__extra-tool.v2")

    def test_length_is_capped_and_unique(self):
        a = mcp_manager.make_tool_name("fake", "x" * 100 + "A")
        b = mcp_manager.make_tool_name("fake", "x" * 100 + "B")
        self.assertLessEqual(len(a), 64)
        self.assertLessEqual(len(b), 64)
        self.assertNotEqual(a, b)

    def test_collisions_after_sanitizing_get_distinct_names(self):
        names = mcp_manager.assign_names("fake", ["a b", "a_b", "a/b"])
        self.assertEqual(len(set(names.values())), 3)
        self.assertEqual(names["a_b"], "mcp__fake__a_b")


class TestNoConfig(ManagerTestCase):
    def test_nothing_happens_without_config(self):
        m = self.manager(approve=False)
        m.start()
        m.wait_ready(5)
        self.assertEqual(m.declarations(), [])
        self.assertEqual(m.version, 0)
        self.assertFalse(m.active)
        self.assertFalse(os.path.exists(self.pids))

    def test_module_helpers_are_inert_when_never_started(self):
        self.assertEqual(mcp_manager.declarations(), [])
        self.assertEqual(mcp_manager.current_version(), 0)
        self.assertIsNone(mcp_manager.lookup("mcp__x__y"))

    def test_start_default_skips_in_tests_without_explicit_config(self):
        os.environ.pop("ARQ_MCP_CONFIG", None)
        self.assertFalse(mcp_manager.start_default())

    def test_start_default_with_explicit_paths_connects_in_background(self):
        self.write({"fake": fake_server()})
        self.approve_all()
        env = {"ARQ_MCP_CONFIG": self.config, "ARQ_MCP_APPROVED": self.approved, "ARQ_MCP_PIDS": self.pids}
        saved = mcp_manager._default
        mcp_manager._default = None
        self.addCleanup(setattr, mcp_manager, "_default", saved)
        with mock.patch.dict(os.environ, env):
            self.assertTrue(mcp_manager.start_default())
            manager = mcp_manager.get_manager()
            self.addCleanup(manager.shutdown)
            self.assertTrue(manager.wait_ready(30))
            self.assertIn("mcp__fake__echo", [d.name for d in mcp_manager.declarations()])
            self.assertGreater(mcp_manager.current_version(), 0)
            self.assertEqual(mcp_manager.lookup("mcp__fake__echo").tool, "echo")
            mcp_manager.shutdown_default()


class TestConnectAndDeclare(ManagerTestCase):
    def test_declares_tools_with_prefixed_names(self):
        m = self.started({"fake": fake_server("--paginate")})
        names = [d.name for d in m.declarations()]
        self.assertIn("mcp__fake__echo", names)
        self.assertIn("mcp__fake__extra-tool.v2", names)
        decl = next(d for d in m.declarations() if d.name == "mcp__fake__echo")
        self.assertIn("fake", decl.description)
        self.assertNotIn("$schema", json.dumps(decl.model_dump(mode="json", exclude_none=True)))
        self.assertEqual(decl.parameters_json_schema["required"], ["message"])
        self.assertTrue(m.active)

    def test_version_bumps_only_when_declarations_change(self):
        m = self.manager({"fake": fake_server()})
        self.assertEqual(m.version, 0)
        m.reload()
        first = m.version
        self.assertGreater(first, 0)
        m.reload()
        self.assertEqual(m.version, first)  # 宣告沒變，不必重建對話
        self.write({"fake": fake_server(include=["echo"])})
        self.approve_all()
        m.reload()
        self.assertGreater(m.version, first)

    def test_background_start_does_not_block(self):
        m = self.manager({"fake": fake_server()})
        t0 = time.time()
        m.start()
        self.assertLess(time.time() - t0, 1.0)
        m.start()  # 冪等
        self.assertTrue(m.wait_ready(20))
        self.assertTrue(m.declarations())

    def test_budget_limits_mcp_declarations_and_reports_it(self):
        m = self.manager({"fake": fake_server()}, decl_budget=600)
        m.reload()
        declared = m.declarations()
        total = sum(len(json.dumps(d.model_dump(mode="json", exclude_none=True), ensure_ascii=False)) for d in declared)
        self.assertLessEqual(total, 600)
        self.assertTrue(0 < len(declared) < 9)
        status = m.status()
        self.assertEqual(status["servers"][0]["tools"], 9)
        self.assertEqual(status["servers"][0]["declared"], len(declared))
        self.assertIn("預算", m.status_text())

    def test_long_description_is_truncated_and_control_chars_removed(self):
        text = mcp_manager.clean_description("fake", "line1\n\x00" + "d" * 500)
        self.assertLessEqual(len(text), mcp_manager.MAX_DESCRIPTION_CHARS + 40)
        self.assertNotIn("\n", text)
        self.assertNotIn("\x00", text)

    def test_include_and_exclude_filter_by_original_tool_name(self):
        m = self.started({"fake": fake_server(include=["echo", "slow", "crash"], exclude=["crash"])})
        names = {d.name for d in m.declarations()}
        self.assertEqual(names, {"mcp__fake__echo", "mcp__fake__slow"})

    def test_unapproved_server_is_not_started(self):
        m = self.manager({"fake": fake_server()}, approve=False)
        m.reload()
        self.assertEqual(m.declarations(), [])
        self.assertEqual(m.status()["servers"][0]["status"], "pending_approval")
        self.assertFalse(os.path.exists(self.pids))

    def test_reload_prompts_for_approval_and_remembers(self):
        m = self.manager({"fake": fake_server()}, approve=False)
        prompts = []
        m.reload(approve=lambda title, detail: prompts.append((title, detail)) or True)
        self.assertEqual(len(prompts), 1)
        self.assertIn("fake", prompts[0][0])
        self.assertIn(FAKE, prompts[0][1])
        self.assertTrue(m.declarations())
        self.assertEqual(mcp_config.load_approved(self.approved).get("fake"),
                         mcp_config.load_config(self.config).servers["fake"].fingerprint)
        m.reload(approve=lambda *a: self.fail("已核可的 server 不應再次詢問"))

    def test_rejected_approval_keeps_server_pending(self):
        m = self.manager({"fake": fake_server()}, approve=False)
        m.reload(approve=lambda title, detail: False)
        self.assertEqual(m.declarations(), [])
        self.assertEqual(m.status()["servers"][0]["status"], "pending_approval")
        self.assertEqual(mcp_config.load_approved(self.approved), {})

    def test_changed_config_requires_new_approval(self):
        m = self.manager({"fake": fake_server()})
        m.reload()
        self.assertTrue(m.declarations())
        self.write({"fake": fake_server("--paginate", auto_approve=["echo"])})
        m.reload()
        self.assertEqual(m.declarations(), [])
        self.assertEqual(m.status()["servers"][0]["status"], "pending_approval")

    def test_disabled_and_config_error_servers_do_not_start(self):
        m = self.started({"off": fake_server(enabled=False), "bad": {"command": "x", "shell": True},
                          "ok": fake_server()})
        by = {s["name"]: s for s in m.status()["servers"]}
        self.assertEqual(by["off"]["status"], "disabled")
        self.assertEqual(by["bad"]["status"], "config_error")
        self.assertEqual(by["ok"]["status"], "connected")

    def test_file_error_is_reported(self):
        with open(self.config, "w", encoding="utf-8") as f:
            f.write("{oops")
        m = self.manager()
        m.reload()
        self.assertIn("JSON", m.status()["file_error"])
        self.assertIn("JSON", m.status_text())


class TestRiskAndLookup(ManagerTestCase):
    def test_every_call_needs_approval_by_default(self):
        m = self.started({"fake": fake_server()})
        reason = m.risk("mcp__fake__echo", {"message": "x"})
        self.assertIn("MCP", reason)
        self.assertIn("fake", reason)

    def test_auto_approve_matches_exact_tool_name_only(self):
        m = self.started({"fake": fake_server(auto_approve=["echo"])})
        self.assertIsNone(m.risk("mcp__fake__echo", {"message": "x"}))
        self.assertIsNone(m.risk("mcp__fake__echo", {"message": "rm -rf /"}))  # 不看參數
        self.assertIsNotNone(m.risk("mcp__fake__slow", {}))
        self.assertIsNone(m.lookup("mcp__fake__echo2"))  # 不存在的工具不會被執行

    def test_auto_approve_has_no_wildcards(self):
        m = self.started({"fake": fake_server(auto_approve=["*", "e*"])})
        self.assertIsNotNone(m.risk("mcp__fake__echo", {}))

    def test_lookup(self):
        m = self.started({"fake": fake_server()})
        entry = m.lookup("mcp__fake__echo")
        self.assertEqual((entry.server, entry.tool), ("fake", "echo"))
        self.assertIsNone(m.lookup("echo"))
        self.assertIsNone(m.lookup("mcp__nope__echo"))

    def test_undeclared_tools_cannot_be_called(self):
        m = self.manager({"fake": fake_server()}, decl_budget=300)
        m.reload()
        undeclared = [e for e in m.entries() if not e.declared]
        self.assertTrue(undeclared)
        self.assertIsNone(m.lookup(undeclared[0].full_name))

    def test_describe_for_card(self):
        m = self.started({"fake": fake_server()})
        server, tool = m.describe("mcp__fake__echo")
        self.assertEqual((server, tool), ("fake", "echo"))


class TestCalls(ManagerTestCase):
    def test_result_is_wrapped_as_external_data(self):
        m = self.started({"fake": fake_server()})
        res = m.call("mcp__fake__echo", {"message": "hi"})
        self.assertEqual(res["source"], "external_mcp")
        self.assertEqual((res["server"], res["tool"]), ("fake", "echo"))
        self.assertEqual(res["content"], "echo: hi")
        self.assertIn("外部", res["notice"])
        self.assertNotIn("error", res)

    def test_is_error_becomes_error_field(self):
        m = self.started({"fake": fake_server()})
        res = m.call("mcp__fake__fail", {})
        self.assertIn("something went wrong", res["error"])
        self.assertEqual(res["source"], "external_mcp")

    def test_non_text_content_is_replaced_by_placeholder(self):
        m = self.started({"fake": fake_server()})
        res = m.call("mcp__fake__image", {})
        self.assertIn("[image omitted: image/png, 4 bytes]", res["content"])
        self.assertIn("caption", res["content"])
        self.assertNotIn("QUJDRA", res["content"])

    def test_secret_from_config_env_is_redacted(self):
        secret = "tok-" + "Q" * 16
        environ = {"LEAK_SRC_TOKEN": secret}
        m = self.started({"fake": fake_server(env={"FAKE_LEAK_TOKEN": "${LEAK_SRC_TOKEN}"})}, environ=environ)
        res = m.call("mcp__fake__leak", {})
        self.assertNotIn(secret, json.dumps(res))
        self.assertIn("***", res["content"])

    def test_secret_in_process_env_is_redacted(self):
        secret = "sk-test-" + "R" * 16
        os.environ["SOME_OTHER_API_KEY"] = secret
        self.addCleanup(os.environ.pop, "SOME_OTHER_API_KEY", None)
        m = self.started({"fake": fake_server()})
        res = m.call("mcp__fake__echo", {"message": secret})
        self.assertNotIn(secret, json.dumps(res))

    def test_redaction_happens_before_truncation(self):
        secret = "tok-" + "Z" * 16
        environ = {"LEAK_SRC_TOKEN": secret}
        m = self.started({"fake": fake_server(env={"FAKE_LEAK_TOKEN": "${LEAK_SRC_TOKEN}"})},
                         environ=environ, result_limit=len("the secret is ") + 6)
        res = m.call("mcp__fake__leak", {})
        self.assertNotIn("tok-", res["content"])  # 不得露出被截斷的金鑰前綴

    def test_long_result_is_truncated_with_marker(self):
        m = self.started({"fake": fake_server()}, result_limit=1000)
        res = m.call("mcp__fake__big", {"n": 50000})
        self.assertLess(len(res["content"]), 1200)
        self.assertIn("50000", res["content"])
        self.assertIn("截斷", res["content"])

    def test_timeout_returns_error_and_connection_survives(self):
        m = self.started({"fake": fake_server(call_timeout=1)})
        t0 = time.time()
        res = m.call("mcp__fake__slow", {"seconds": 5})
        self.assertLess(time.time() - t0, 4)
        self.assertIn("逾時", res["error"])
        self.assertEqual(m.call("mcp__fake__echo", {"message": "still"})["content"], "echo: still")

    def test_crash_is_isolated_to_that_server(self):
        m = self.started({"a": fake_server(), "b": fake_server()})
        res = m.call("mcp__a__crash", {})
        self.assertIn("error", res)
        again = m.call("mcp__a__echo", {"message": "x"})
        self.assertIn("error", again)
        self.assertIn("reload", again["error"])
        self.assertEqual(m.call("mcp__b__echo", {"message": "ok"})["content"], "echo: ok")
        by = {s["name"]: s["status"] for s in m.status()["servers"]}
        self.assertEqual(by, {"a": "disconnected", "b": "connected"})

    def test_unknown_tool_and_bad_args(self):
        m = self.started({"fake": fake_server()})
        self.assertIn("error", m.call("mcp__fake__nope", {}))
        self.assertIn("error", m.call("mcp__fake__echo", "not a dict"))


class TestFailureIsolation(ManagerTestCase):
    def test_bad_command_and_hanging_server_do_not_break_good_one(self):
        m = self.manager({
            "missing": {"command": "definitely-not-a-command-91", "args": []},
            "hang": fake_server("--hang-init", startup_timeout=1),
            "good": fake_server(),
        })
        t0 = time.time()
        m.reload()
        self.assertLess(time.time() - t0, 15)
        by = {s["name"]: s for s in m.status()["servers"]}
        self.assertEqual(by["missing"]["status"], "failed")
        self.assertEqual(by["hang"]["status"], "failed")
        self.assertEqual(by["good"]["status"], "connected")
        self.assertIn("找不到", by["missing"]["error"])
        self.assertEqual(m.call("mcp__good__echo", {"message": "x"})["content"], "echo: x")

    def test_child_env_has_no_core_or_other_secrets(self):
        os.environ["GEMINI_API_KEY"] = "g" * 24
        os.environ["TICKETS_GITHUB_TOKEN"] = "t" * 24
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)
        self.addCleanup(os.environ.pop, "TICKETS_GITHUB_TOKEN", None)
        m = self.started({"fake": fake_server()})
        for name in ("GEMINI_API_KEY", "TICKETS_GITHUB_TOKEN"):
            self.assertEqual(m.call("mcp__fake__env", {"name": name})["content"], f"{name}=absent")

    def test_only_declared_env_reaches_child(self):
        environ = {"WANTED_API_KEY": "w" * 20}
        os.environ["WANTED_API_KEY"] = environ["WANTED_API_KEY"]
        self.addCleanup(os.environ.pop, "WANTED_API_KEY", None)
        m = self.started({"fake": fake_server(env={"CHILD_API_KEY": "${WANTED_API_KEY}"})}, environ=environ)
        self.assertEqual(m.call("mcp__fake__env", {"name": "CHILD_API_KEY"})["content"], "CHILD_API_KEY=present")
        self.assertEqual(m.call("mcp__fake__env", {"name": "WANTED_API_KEY"})["content"], "WANTED_API_KEY=absent")

    def test_status_text_never_shows_env_values(self):
        secret = "tok-" + "S" * 16
        environ = {"LEAK_SRC_TOKEN": secret}
        m = self.started({"fake": fake_server(env={"FAKE_LEAK_TOKEN": "${LEAK_SRC_TOKEN}"})}, environ=environ)
        text = m.status_text()
        self.assertNotIn(secret, text)
        self.assertIn("fake", text)
        self.assertIn("connected", text)


class TestShutdownAndOrphans(ManagerTestCase):
    def test_shutdown_kills_servers_and_grandchildren_and_clears_pid_file(self):
        m = self.started({"fake": fake_server()})
        grandchild = int(m.call("mcp__fake__spawn_child", {})["content"])
        with open(self.pids, encoding="utf-8") as f:
            recorded = json.load(f)["pids"]
        server_pid = recorded[0]["pid"]
        self.assertTrue(psutil.pid_exists(server_pid))
        m.shutdown()
        deadline = time.time() + 10
        while time.time() < deadline and (psutil.pid_exists(server_pid) or psutil.pid_exists(grandchild)):
            time.sleep(0.1)
        self.assertFalse(psutil.pid_exists(server_pid))
        self.assertFalse(psutil.pid_exists(grandchild))
        self.assertFalse(os.path.exists(self.pids))
        self.assertIn("error", m.call("mcp__fake__echo", {"message": "x"}))
        m.shutdown()  # 冪等

    def test_stale_processes_from_previous_run_are_cleaned_up(self):
        stale = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(lambda: (stale.kill(), stale.wait()))
        proc = psutil.Process(stale.pid)
        with open(self.pids, "w", encoding="utf-8") as f:
            json.dump({"pids": [{"pid": stale.pid, "create_time": proc.create_time(), "server": "old"}]}, f)
        m = self.manager({"fake": fake_server()})
        m.reload()
        deadline = time.time() + 10
        while time.time() < deadline and stale.poll() is None:
            time.sleep(0.1)
        self.assertIsNotNone(stale.poll())

    def test_pid_reuse_is_not_killed(self):
        innocent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(lambda: (innocent.kill(), innocent.wait()))
        with open(self.pids, "w", encoding="utf-8") as f:
            json.dump({"pids": [{"pid": innocent.pid, "create_time": 1.0, "server": "old"}]}, f)
        m = self.manager({"fake": fake_server()})
        m.reload()
        self.assertIsNone(innocent.poll())

    def test_reload_replaces_connections_without_leaking(self):
        m = self.started({"fake": fake_server()})
        with open(self.pids, encoding="utf-8") as f:
            old_pid = json.load(f)["pids"][0]["pid"]
        m.reload()
        deadline = time.time() + 10
        while time.time() < deadline and psutil.pid_exists(old_pid):
            time.sleep(0.1)
        self.assertFalse(psutil.pid_exists(old_pid))
        self.assertEqual(m.call("mcp__fake__echo", {"message": "new"})["content"], "echo: new")


if __name__ == "__main__":
    unittest.main()
