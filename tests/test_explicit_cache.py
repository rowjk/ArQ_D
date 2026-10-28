"""
 Gemini 顯式快取 (不連網，mock Gemini client)

驗收標準：
    - 第一次需要時建立顯式快取 (system_instruction + 全部工具)，以內容雜湊為鍵；提示詞或工具改變時自動重建
    - 使用中才保留：短 TTL (GEMINI_EXPLICIT_CACHE_TTL_MINUTES，預設 15 分鐘)，使用時延長；ArQ 結束時刪除
    - 建立失敗、過期或回傳錯誤時，自動退回不用快取的方式送出，不影響對話
    - 動態 thinking 的覆寫設定 (含禁止呼叫工具的總結) 也帶上快取
    - 可用 GEMINI_EXPLICIT_CACHE=false 關閉
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import agent  # noqa: E402
from tools import gemini_cache  # noqa: E402


def tool_a(city: str) -> dict:
    """查詢天氣"""
    return {}


def tool_b(command: str) -> dict:
    """執行指令"""
    return {}


class FakeCaches:
    """模擬 client.caches：記錄 create / update / delete 呼叫"""

    def __init__(self):
        self.created, self.updated, self.deleted = [], [], []
        self.fail_create = self.fail_update = self.fail_delete = False

    def create(self, model, config):
        if self.fail_create:
            raise RuntimeError("create failed")
        self.created.append((model, config))
        return SimpleNamespace(name=f"cachedContents/c{len(self.created)}")

    def update(self, name, config):
        if self.fail_update:
            raise RuntimeError("404 not found")
        self.updated.append((name, config))
        return SimpleNamespace(name=name)

    def delete(self, name):
        self.deleted.append(name)
        if self.fail_delete:
            raise RuntimeError("delete failed")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_cache(prompt="系統提示詞", funcs=(tool_a, tool_b), ttl=900):
    client = SimpleNamespace(vertexai=False, caches=FakeCaches())
    clock = Clock()
    state = {"prompt": prompt, "funcs": list(funcs)}
    cache = gemini_cache.PrefixCache(client, "test-model", lambda: state["prompt"], lambda: state["funcs"],
                                     ttl_seconds=ttl, clock=clock)
    return cache, client.caches, clock, state


class TestPrefixCache(unittest.TestCase):
    def setUp(self):
        p = mock.patch("builtins.print")
        p.start()
        self.addCleanup(p.stop)

    def test_creates_on_first_use_with_prompt_and_all_tools(self):
        cache, caches, _, _ = make_cache()
        self.assertEqual(cache.name(), "cachedContents/c1")
        model, config = caches.created[0]
        self.assertEqual(model, "test-model")
        self.assertEqual(config.system_instruction, "系統提示詞")
        names = [d.name for d in config.tools[0].function_declarations]
        self.assertEqual(names, ["tool_a", "tool_b"])
        self.assertIsNone(config.tool_config)
        self.assertEqual(config.ttl, "900s")

    def test_reuses_cache_while_content_unchanged(self):
        cache, caches, clock, _ = make_cache()
        cache.name()
        clock.now += 10
        self.assertEqual(cache.name(), "cachedContents/c1")
        self.assertEqual(len(caches.created), 1)

    def test_rebuilds_when_prompt_changes(self):
        cache, caches, _, state = make_cache()
        cache.name()
        state["prompt"] = "新的提示詞"
        self.assertEqual(cache.name(), "cachedContents/c2")
        self.assertEqual(caches.created[1][1].system_instruction, "新的提示詞")
        self.assertEqual(caches.deleted, ["cachedContents/c1"])

    def test_rebuilds_when_tools_change(self):
        cache, caches, _, state = make_cache()
        cache.name()
        state["funcs"] = [tool_a]
        self.assertEqual(cache.name(), "cachedContents/c2")
        self.assertEqual(caches.deleted, ["cachedContents/c1"])

    def test_extends_ttl_when_used(self):
        cache, caches, clock, _ = make_cache()
        cache.name()
        clock.now += 30                     # 剛延長過 (建立時)，不重複呼叫 API
        cache.name()
        self.assertEqual(caches.updated, [])
        clock.now += 60
        self.assertEqual(cache.name(), "cachedContents/c1")
        self.assertEqual(len(caches.updated), 1)
        name, config = caches.updated[0]
        self.assertEqual(name, "cachedContents/c1")
        self.assertEqual(config.ttl, "900s")
        # 延長後的到期時間以延長時刻起算：再過 800 秒 (距建立已超過 900 秒) 仍可用
        clock.now += 800
        self.assertEqual(cache.name(), "cachedContents/c1")
        self.assertEqual(len(caches.created), 1)

    def test_recreates_after_local_expiry(self):
        cache, caches, clock, _ = make_cache()
        cache.name()
        clock.now += 901                    # 閒置超過 TTL：伺服器端已刪除，直接重建，不嘗試延長
        self.assertEqual(cache.name(), "cachedContents/c2")
        self.assertEqual(caches.updated, [])

    def test_recreates_when_extend_fails(self):
        cache, caches, clock, _ = make_cache()
        cache.name()
        clock.now += 120
        caches.fail_update = True
        self.assertEqual(cache.name(), "cachedContents/c2")

    def test_create_failure_returns_none_and_backs_off(self):
        cache, caches, clock, _ = make_cache()
        caches.fail_create = True
        self.assertIsNone(cache.name())
        caches.fail_create = False
        clock.now += 10                     # 退避期間不重試，避免每則訊息都多一次失敗的 API 呼叫
        self.assertIsNone(cache.name())
        self.assertEqual(caches.created, [])
        clock.now += gemini_cache.CREATE_RETRY_SECONDS
        self.assertEqual(cache.name(), "cachedContents/c1")

    def test_no_tools_variant_is_separate_cache_with_tool_config_none(self):
        cache, caches, _, _ = make_cache()
        self.assertEqual(cache.name(), "cachedContents/c1")
        self.assertEqual(cache.name(allow_tools=False), "cachedContents/c2")
        config = caches.created[1][1]
        self.assertEqual(config.tool_config.function_calling_config.mode.value, "NONE")
        self.assertEqual(cache.name(), "cachedContents/c1")

    def test_invalidate_forgets_and_deletes(self):
        cache, caches, _, _ = make_cache()
        cache.name()
        caches.fail_delete = True           # 已過期的快取刪除會失敗，忽略即可
        cache.invalidate()
        self.assertEqual(caches.deleted, ["cachedContents/c1"])
        self.assertEqual(cache.name(), "cachedContents/c2")

    def test_close_deletes_all_caches(self):
        cache, caches, _, _ = make_cache()
        cache.name()
        cache.name(allow_tools=False)
        cache.close()
        self.assertEqual(sorted(caches.deleted), ["cachedContents/c1", "cachedContents/c2"])
        cache.close()                       # 重複呼叫不會再刪
        self.assertEqual(len(caches.deleted), 2)


class TestSettings(unittest.TestCase):
    def test_enabled_by_default_and_can_be_disabled(self):
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_EXPLICIT_CACHE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(gemini_cache.explicit_cache_enabled())
        for off in ("false", "0", "no", "off", "FALSE"):
            with self.subTest(off=off), mock.patch.dict(os.environ, {"GEMINI_EXPLICIT_CACHE": off}):
                self.assertFalse(gemini_cache.explicit_cache_enabled())

    def test_ttl_minutes(self):
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_EXPLICIT_CACHE_TTL_MINUTES"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(gemini_cache.cache_ttl_seconds(), 900)
        with mock.patch.dict(os.environ, {"GEMINI_EXPLICIT_CACHE_TTL_MINUTES": "5"}):
            self.assertEqual(gemini_cache.cache_ttl_seconds(), 300)
        with mock.patch.dict(os.environ, {"GEMINI_EXPLICIT_CACHE_TTL_MINUTES": "abc"}):
            self.assertEqual(gemini_cache.cache_ttl_seconds(), 900)

    def test_agent_skips_cache_when_disabled(self):
        with mock.patch.dict(os.environ, {"GEMINI_EXPLICIT_CACHE": "false"}), \
                mock.patch.object(agent.genai, "Client", return_value=SimpleNamespace()):
            a = agent.ArQAgent(api_key="dummy")
        self.assertIsNone(a.prefix_cache)

    def test_agent_creates_cache_manager_when_enabled(self):
        with mock.patch.dict(os.environ, {"GEMINI_EXPLICIT_CACHE": "true"}), \
                mock.patch.object(agent.genai, "Client", return_value=SimpleNamespace()), \
                mock.patch.object(agent.atexit, "register"):
            a = agent.ArQAgent(api_key="dummy")
        self.assertIsInstance(a.prefix_cache, gemini_cache.PrefixCache)


# ---------- agent 整合：送出時帶上快取、失敗時退回 ----------

class FakePrefixCache:
    def __init__(self, fail_names=False):
        self.requests, self.invalidated, self.closed = [], [], 0

    def name(self, allow_tools=True):
        self.requests.append(allow_tools)
        return "cachedContents/main" if allow_tools else "cachedContents/notools"

    def invalidate(self, allow_tools=True):
        self.invalidated.append(allow_tools)

    def close(self):
        self.closed += 1


class FakeChat:
    def __init__(self, replies, fail_cached=False):
        self.replies = list(replies)
        self.sent = []
        self.fail_cached = fail_cached

    def send_message(self, payload, config=None):
        self.sent.append(config)
        if self.fail_cached and config is not None and config.cached_content:
            raise RuntimeError("403 CachedContent not found")
        return self.replies.pop(0)


def response(calls=None, text="", cached=0):
    parts = [SimpleNamespace(text=text, thought=None, function_call=None)] if text else []
    return SimpleNamespace(
        function_calls=calls,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
        usage_metadata=SimpleNamespace(prompt_token_count=5000, cached_content_token_count=cached,
                                       candidates_token_count=10, total_token_count=5010),
    )


def bare_agent(prefix_cache):
    a = agent.ArQAgent.__new__(agent.ArQAgent)
    a.model_name = "test-model"
    a.thinking_level = "LOW"
    a.deep_thinking_level = "HIGH"
    a.chats = {1: "chat"}
    a.prefix_cache = prefix_cache
    return a


def run(a, chat, prompt="現在幾點", rounds=None):
    patches = [mock.patch.object(agent.ArQAgent, "_execute_tool", return_value=({"ok": True}, [])),
               mock.patch.object(agent, "record_usage"),
               mock.patch("builtins.print")]
    if rounds is not None:
        patches.append(mock.patch.object(agent, "MAX_TOOL_ROUNDS", rounds))
    for p in patches:
        p.start()
    try:
        return asyncio.run(a._run_tool_loop(chat, prompt, 1, None, None))
    finally:
        for p in reversed(patches):
            p.stop()


class TestAgentUsesCache(unittest.TestCase):
    def test_config_with_cache_moves_prefix_out_of_request(self):
        a = bare_agent(None)
        config = a._chat_config("HIGH", cache_name="cachedContents/main")
        self.assertEqual(config.cached_content, "cachedContents/main")
        self.assertIsNone(config.system_instruction)
        self.assertIsNone(config.tools)
        self.assertIsNone(config.tool_config)
        self.assertEqual(config.thinking_config.thinking_level.value, "HIGH")
        self.assertTrue(config.automatic_function_calling.disable)

    def test_normal_message_sends_with_cache(self):
        cache = FakePrefixCache()
        chat = FakeChat([response(text="十點", cached=3757)])
        res = run(bare_agent(cache), chat)
        self.assertEqual(res["text"], "十點")
        self.assertEqual(chat.sent[0].cached_content, "cachedContents/main")
        self.assertEqual(chat.sent[0].thinking_config.thinking_level.value, "LOW")

    def test_deep_thinking_override_also_uses_cache(self):
        cache = FakePrefixCache()
        chat = FakeChat([response(text="規劃好了")])
        run(bare_agent(cache), chat, prompt="幫我規劃一個記帳 App")
        self.assertEqual(chat.sent[0].cached_content, "cachedContents/main")
        self.assertEqual(chat.sent[0].thinking_config.thinking_level.value, "HIGH")

    def test_wrap_up_uses_no_tools_cache(self):
        cache = FakePrefixCache()
        calls = [SimpleNamespace(id="c", name="get_system_status", args={})]
        chat = FakeChat([response(calls), response(calls), response(text="先做到這裡")])
        res = run(bare_agent(cache), chat, rounds=1)
        self.assertIn("先做到這裡", res["text"])
        last = chat.sent[-1]
        self.assertEqual(last.cached_content, "cachedContents/notools")
        self.assertIsNone(last.tool_config)
        self.assertEqual(cache.requests[-1], False)

    def test_falls_back_without_cache_on_error(self):
        cache = FakePrefixCache()
        chat = FakeChat([response(text="十點")], fail_cached=True)
        res = run(bare_agent(cache), chat)
        self.assertEqual(res["text"], "十點")
        self.assertEqual(cache.invalidated, [True])
        self.assertEqual(len(chat.sent), 2)
        self.assertIsNone(chat.sent[1])     # 一般等級：退回 chat 預設設定 (含提示詞與工具)

    def test_wrap_up_falls_back_with_full_no_tools_config(self):
        cache = FakePrefixCache()
        calls = [SimpleNamespace(id="c", name="get_system_status", args={})]

        class Chat(FakeChat):
            def send_message(self, payload, config=None):
                if config is not None and config.cached_content == "cachedContents/notools":
                    self.sent.append(config)
                    raise RuntimeError("expired")
                return super().send_message(payload, config)

        chat = Chat([response(calls), response(calls), response(text="總結")])
        res = run(bare_agent(cache), chat, rounds=1)
        self.assertIn("總結", res["text"])
        self.assertEqual(cache.invalidated, [False])
        last = chat.sent[-1]
        self.assertIsNone(last.cached_content)
        self.assertEqual(last.tool_config.function_calling_config.mode.value, "NONE")
        self.assertEqual(last.system_instruction, agent.SYSTEM_INSTRUCTION)

    def test_cache_unavailable_keeps_old_behavior(self):
        class NoCache(FakePrefixCache):
            def name(self, allow_tools=True):
                return None

        chat = FakeChat([response(text="十點")])
        run(bare_agent(NoCache()), chat)
        self.assertEqual(chat.sent, [None])

    def test_close_deletes_cache(self):
        cache = FakePrefixCache()
        bare_agent(cache).close()
        self.assertEqual(cache.closed, 1)
        bare_agent(None).close()            # 沒有快取時不出錯


if __name__ == "__main__":
    unittest.main()
