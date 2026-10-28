"""
 MCP 與 agent / bot 的整合（fake server，不連外；Gemini 與 Discord 皆以假物件取代）

驗收標準：
    - 沒有設定時：工具宣告與快取內容和改動前逐字相同，且不 import `mcp` 套件
    - 有設定時：宣告併入同一個 Tool；連線完成後既有對話會以相同歷史重建
    - 每次呼叫預設跳同意卡（拒絕就不執行）；auto_approve 只看工具名稱；卡片上的參數已遮蔽
    - MCP server 斷線時原生工具照常運作；結果有外部資料標示且經遮蔽
    - 修改 mcp_servers.json 需要同意；/mcp 指令需授權；結束與 /reboot 前關閉所有子程序
"""

import asyncio
import inspect
import json
import os
import subprocess
import sys
import tempfile
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
from tools import gemini_cache, mcp_config, mcp_manager, risk_policy, secret_guard  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def fake_server(*modes, **extra):
    return {"command": sys.executable, "args": [FAKE, *modes], **extra}


def run(coro):
    return asyncio.run(coro)


class McpAgentCase(unittest.TestCase):
    """建立一個連到 fake server 的管理員並暫時設為全域單例"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch("builtins.print")
        p.start()
        self.addCleanup(p.stop)
        self.manager = None
        self.addCleanup(self._restore)
        self._saved_default = mcp_manager._default

    def _restore(self):
        if self.manager is not None:
            self.manager.shutdown()
        mcp_manager._default = self._saved_default
        secret_guard.register_extra_secrets([])

    def attach(self, servers, environ=None):
        config = os.path.join(self.tmp.name, "mcp_servers.json")
        approved = os.path.join(self.tmp.name, "approved.json")
        with open(config, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f)
        res = mcp_config.load_config(config, environ=environ or {})
        mcp_config.save_approved(approved, {n: c.fingerprint for n, c in res.servers.items()})
        self.manager = mcp_manager.McpManager(config, approved, os.path.join(self.tmp.name, "pids.json"),
                                              environ=environ or {})
        mcp_manager._default = self.manager
        self.manager.reload()
        return self.manager


def bare_agent():
    bot = agent.ArQAgent.__new__(agent.ArQAgent)
    bot.model_name = "test-model"
    bot.thinking_level = "LOW"
    bot.deep_thinking_level = "HIGH"
    bot.client = SimpleNamespace(vertexai=False)
    bot.prefix_cache = None
    return bot


# ------------------------------------------------------------------ 沒有設定：逐字相同

class TestNoConfigIsIdentical(unittest.TestCase):
    def setUp(self):
        self._saved = mcp_manager._default
        mcp_manager._default = None
        self.addCleanup(setattr, mcp_manager, "_default", self._saved)

    def test_chat_config_tools_are_exactly_the_native_callables(self):
        config = bare_agent()._chat_config("LOW")
        self.assertEqual(config.tools, [spec.func for spec in agent._TOOL_SPECS])
        no_tools = bare_agent()._chat_config("LOW", allow_tools=False)
        self.assertEqual(no_tools.tools, [spec.func for spec in agent._TOOL_SPECS])

    def test_prefix_cache_content_and_hash_match_the_old_construction(self):
        from google.genai import types
        client = SimpleNamespace(vertexai=False)
        funcs = [spec.func for spec in agent._TOOL_SPECS]
        old_tools = [types.Tool(function_declarations=[
            types.FunctionDeclaration.from_callable(client=client, callable=f) for f in funcs])]
        plain = gemini_cache.PrefixCache(client, "m", lambda: agent.SYSTEM_INSTRUCTION, lambda: funcs)
        with_hook = gemini_cache.PrefixCache(client, "m", lambda: agent.SYSTEM_INSTRUCTION, lambda: funcs,
                                             extra_declarations=lambda: mcp_manager.declarations())
        expected = plain._key(agent.SYSTEM_INSTRUCTION, old_tools, True)
        self.assertEqual(with_hook._key(agent.SYSTEM_INSTRUCTION, with_hook._build_tools(), True), expected)
        self.assertEqual(plain._key(agent.SYSTEM_INSTRUCTION, plain._build_tools(), True), expected)

    def test_agent_wires_the_cache_to_mcp_declarations(self):
        with mock.patch.object(agent, "HAS_GENAI", True), \
             mock.patch.object(agent.genai, "Client", return_value=SimpleNamespace(vertexai=False)), \
             mock.patch.dict(os.environ, {"GEMINI_EXPLICIT_CACHE": "true"}):
            bot = agent.ArQAgent(api_key="test-key")
        self.addCleanup(bot.close)
        self.assertIsNotNone(bot.prefix_cache)
        self.assertEqual(len(bot.prefix_cache._build_tools()[0].function_declarations), len(agent._TOOL_SPECS))

    def test_tool_registry_and_native_budget_untouched(self):
        self.assertEqual(len(agent._TOOL_SPECS), 13)
        self.assertFalse(any(name.startswith("mcp__") for name in agent.TOOL_REGISTRY))

    def test_importing_agent_does_not_import_mcp_package_or_start_anything(self):
        code = ("import sys, threading\n"
                "import agent\n"
                "import bot  # noqa\n"
                "assert 'mcp' not in sys.modules, 'mcp imported'\n"
                "assert not any(t.name.startswith('mcp-') for t in threading.enumerate())\n"
                "print('ok')\n")
        env = {**os.environ, "ARQ_TESTING": "1"}
        env.pop("ARQ_MCP_CONFIG", None)
        out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env, capture_output=True,
                             text=True, encoding="utf-8", timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-800:])
        self.assertIn("ok", out.stdout)


# ------------------------------------------------------------------ 有設定：宣告與對話重建

class TestDeclarationsAndChatRebuild(McpAgentCase):
    def test_mcp_declarations_merge_into_one_tool_after_native_ones(self):
        self.attach({"fake": fake_server()})
        config = bare_agent()._chat_config("LOW")
        self.assertEqual(len(config.tools), 1)
        names = [d.name for d in config.tools[0].function_declarations]
        native = [spec.func.__name__ for spec in agent._TOOL_SPECS]
        self.assertEqual(names[:len(native)], native)
        self.assertIn("mcp__fake__echo", names[len(native):])

    def test_prefix_cache_includes_mcp_declarations_and_rebuilds_on_change(self):
        self.attach({"fake": fake_server()})
        client = SimpleNamespace(vertexai=False)
        funcs = [spec.func for spec in agent._TOOL_SPECS]
        cache = gemini_cache.PrefixCache(client, "m", lambda: "p", lambda: funcs,
                                         extra_declarations=lambda: mcp_manager.declarations())
        plain = gemini_cache.PrefixCache(client, "m", lambda: "p", lambda: funcs)
        self.assertNotEqual(cache._key("p", cache._build_tools(), True), plain._key("p", plain._build_tools(), True))
        names = [d.name for d in cache._build_tools()[0].function_declarations]
        self.assertIn("mcp__fake__echo", names)

    def test_existing_chat_is_rebuilt_with_same_history_when_catalog_changes(self):
        created = []

        class FakeChat:
            def __init__(self, history, config):
                self.history, self.config = history, config

            def get_history(self, curated=True):
                return self.history

        def create(model, history, config):
            chat = FakeChat(list(history), config)
            created.append(chat)
            return chat

        bot = bare_agent()
        bot.chats, bot._locks = {}, {}
        bot._pending, bot._pending_lock = [], __import__("threading").Lock()
        bot.client = SimpleNamespace(vertexai=False, chats=SimpleNamespace(create=create))
        mcp_manager._default = None
        with mock.patch.object(agent.ArQAgent, "_run_tool_loop", return_value={"text": "ok", "files": []}):
            run(bot.chat("第一則", user_id=1))
            self.assertEqual(len(created), 1)
            run(bot.chat("第二則", user_id=1))
            self.assertEqual(len(created), 1)               # 沒有 MCP 時不重建
            from google.genai import types
            history = [types.Content(role="user", parts=[types.Part(text="舊問題")]),
                       types.Content(role="model", parts=[types.Part(text="舊回答")])]
            created[0].history = history
            self.attach({"fake": fake_server()})            # 連線完成，工具清單改變
            run(bot.chat("第三則", user_id=1))
            self.assertEqual(len(created), 2)
            self.assertEqual(created[1].history, history)
            names = [d.name for d in created[1].config.tools[0].function_declarations]
            self.assertIn("mcp__fake__echo", names)
            run(bot.chat("第四則", user_id=1))
            self.assertEqual(len(created), 2)               # 版本沒變就不再重建


# ------------------------------------------------------------------ 呼叫：同意卡、守門、遮蔽

class TestExecuteMcpTool(McpAgentCase):
    def execute(self, name, args, confirm):
        return run(agent.ArQAgent._execute_tool(bare_agent(), name, args, confirm, None))

    def test_every_call_asks_for_consent_and_runs_when_approved(self):
        self.attach({"fake": fake_server()})
        cards = []

        async def confirm(title, detail):
            cards.append((title, detail))
            return True

        result, files = self.execute("mcp__fake__echo", {"message": "hello"}, confirm)
        self.assertEqual(len(cards), 1)
        self.assertIn("fake", cards[0][0] + cards[0][1])
        self.assertIn("echo", cards[0][0] + cards[0][1])
        self.assertIn("hello", cards[0][1])
        self.assertEqual(result["content"], "echo: hello")
        self.assertEqual(result["source"], "external_mcp")
        self.assertEqual(files, [])

    def test_denied_or_missing_confirm_does_not_call_the_server(self):
        self.attach({"fake": fake_server()})
        calls = []
        real = self.manager.call
        self.manager.call = lambda *a, **k: calls.append(a) or real(*a, **k)

        async def deny(title, detail):
            return False

        for confirm in (deny, None):
            result, _ = self.execute("mcp__fake__echo", {"message": "x"}, confirm)
            self.assertIn("拒絕", result["error"])
        self.assertEqual(calls, [])

    def test_auto_approve_skips_the_card_by_tool_name_only(self):
        self.attach({"fake": fake_server(auto_approve=["echo"])})

        async def confirm(title, detail):
            raise AssertionError("auto_approve 的工具不應跳卡")

        result, _ = self.execute("mcp__fake__echo", {"message": "rm -rf /"}, confirm)
        self.assertEqual(result["content"], "echo: rm -rf /")
        asked = []

        async def confirm2(title, detail):
            asked.append(title)
            return True

        self.execute("mcp__fake__slow", {"seconds": 0}, confirm2)
        self.assertEqual(len(asked), 1)

    def test_card_arguments_are_redacted(self):
        secret = "sk-card-" + "K" * 16
        os.environ["CARD_TEST_API_KEY"] = secret
        self.addCleanup(os.environ.pop, "CARD_TEST_API_KEY", None)
        self.attach({"fake": fake_server()})
        shown = []

        async def confirm(title, detail):
            shown.append(title + detail)
            return True

        result, _ = self.execute("mcp__fake__echo", {"message": secret}, confirm)
        self.assertNotIn(secret, shown[0])
        self.assertNotIn(secret, json.dumps(result))

    def test_unknown_mcp_tool_is_rejected(self):
        self.attach({"fake": fake_server()})
        result, _ = self.execute("mcp__fake__nope", {}, None)
        self.assertIn("未知", result["error"])
        result, _ = self.execute("mcp__ghost__echo", {}, None)
        self.assertIn("未知", result["error"])

    def test_native_tools_keep_working_when_server_is_down(self):
        self.attach({"fake": fake_server(call_timeout=1)})

        async def approve(title, detail):
            return True

        crashed, _ = self.execute("mcp__fake__crash", {}, approve)
        self.assertIn("error", crashed)
        down, _ = self.execute("mcp__fake__echo", {"message": "x"}, approve)
        self.assertIn("reload", down["error"])
        native, _ = self.execute("get_system_status", {}, None)
        self.assertNotIn("error", native)
        self.assertIn("os", native)

    def test_timeout_does_not_hang_the_tool_loop(self):
        self.attach({"fake": fake_server(call_timeout=1)})

        async def approve(title, detail):
            return True

        t0 = time.time()
        result, _ = self.execute("mcp__fake__slow", {"seconds": 5}, approve)
        self.assertLess(time.time() - t0, 4)
        self.assertIn("逾時", result["error"])


class TestCardText(unittest.TestCase):
    def test_describe_tool_call_for_mcp(self):
        text = agent.describe_tool_call("mcp__drive__search", {"q": "報告"}, "呼叫外部 MCP 工具 drive.search")
        self.assertIn("drive", text)
        self.assertIn("search", text)
        self.assertIn("報告", text)

    def test_describe_redacts_arguments(self):
        secret = "sk-desc-" + "D" * 16
        with mock.patch.dict(os.environ, {"DESC_TEST_API_KEY": secret}):
            text = agent.describe_tool_call("mcp__drive__search", {"q": secret}, "x")
        self.assertNotIn(secret, text)


# ------------------------------------------------------------------ 風險規則、斜線指令、關閉

class TestConfigEditNeedsConsent(unittest.TestCase):
    def test_shell_and_python_edits_of_the_real_config_are_risky(self):
        for kind, text in (
            ("shell", "notepad mcp_servers.json"),
            ("shell", "Set-Content -Path .\\mcp_servers.json -Value '{}'"),
            ("shell", "echo x >> C:/repo/mcp_servers.json"),
            ("python", "open('mcp_servers.json', 'w').write('{}')"),
            ("python", "pathlib.Path('mcp_servers.json').write_text('{}')"),
        ):
            with self.subTest(text=text):
                self.assertIn("MCP", risk_policy.classify_risk(text, kind) or "")

    def test_example_file_and_unrelated_text_are_not_flagged(self):
        for text in ("Get-Content mcp_servers.example.json", "echo hello", "dir"):
            with self.subTest(text=text):
                self.assertIsNone(risk_policy.classify_risk(text, "shell"))


class TestBotCommand(unittest.TestCase):
    def test_mcp_command_is_registered_with_status_and_reload(self):
        import bot
        cmd = bot.bot.tree.get_command("mcp")
        self.assertIsNotNone(cmd)
        action = next(p for p in cmd.parameters if p.name == "action")
        self.assertEqual({c.value for c in action.choices}, {"status", "reload"})

    def test_mcp_command_requires_authorization_first(self):
        import bot
        src = inspect.getsource(bot.cmd_mcp.callback)
        self.assertIn("is_authorized(interaction.user.id)", src)
        self.assertLess(src.index("is_authorized"), src.index("mcp_manager"))

    def test_help_mentions_mcp(self):
        import bot
        self.assertIn("/mcp", inspect.getsource(bot.cmd_help.callback))

    def test_startup_starts_manager_and_reboot_closes_agent(self):
        import bot
        self.assertIn("mcp_manager.start_default", inspect.getsource(bot.on_ready))
        self.assertIn("agent.close", inspect.getsource(bot.perform_reboot))


class TestCloseKillsServers(McpAgentCase):
    def test_agent_close_shuts_down_every_server_process(self):
        self.attach({"fake": fake_server()})
        with open(os.path.join(self.tmp.name, "pids.json"), encoding="utf-8") as f:
            pid = json.load(f)["pids"][0]["pid"]
        self.assertTrue(psutil.pid_exists(pid))
        bare_agent().close()
        deadline = time.time() + 10
        while time.time() < deadline and psutil.pid_exists(pid):
            time.sleep(0.1)
        self.assertFalse(psutil.pid_exists(pid))


if __name__ == "__main__":
    unittest.main()
