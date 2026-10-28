"""Token 節省機制：對話歷史裁切、thinking 等級、用量紀錄"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from google.genai import types

import agent
from tools import gemini_settings, usage_log

from tests import netguard  # noqa: E402,F401  測試禁止連外


def _user(text):
    return types.Content(role="user", parts=[types.Part(text=text)])


def _model(text):
    return types.Content(role="model", parts=[types.Part(text=text)])


def _call(name):
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args={}))])


def _result(name, payload):
    return types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(name=name, response={"result": payload}))])


def _turns(n, size=10):
    history = []
    for i in range(n):
        history += [_user(f"問題{i}"), _call("search_web"), _result("search_web", "x" * size), _model(f"答案{i}")]
    return history


class TrimHistoryTest(unittest.TestCase):
    def test_under_limits_returns_same_object(self):
        history = _turns(3)
        self.assertIs(agent.trim_history(history, 10, 100000), history)

    def test_keeps_latest_turns_by_count(self):
        trimmed = agent.trim_history(_turns(5), 2, 100000)
        self.assertEqual(len(trimmed), 8)
        self.assertEqual(trimmed[0].parts[0].text, "問題3")

    def test_function_response_is_not_a_turn_start(self):
        # 只剩 1 輪時必須從使用者文字訊息開始，不能從 function_response 開始 (否則 function_call 沒有配對)
        trimmed = agent.trim_history(_turns(3), 1, 100000)
        self.assertEqual(trimmed[0].parts[0].text, "問題2")
        self.assertEqual(trimmed[0].role, "user")

    def test_drops_old_turns_by_chars(self):
        history = _turns(4, size=5000)
        # 關閉工具結果縮減，單獨驗證依字數丟最舊輪次 (縮減見 tests/test_history_compaction.py)
        trimmed = agent.trim_history(history, 10, 12000, tool_result_chars=10 ** 9)
        self.assertLess(sum(agent._content_chars(c) for c in trimmed), 12000)
        self.assertEqual(trimmed[-1].parts[0].text, "答案3")
        self.assertEqual(trimmed[0].parts[0].text, "問題2")

    def test_single_oversized_turn_is_kept(self):
        # 之前會回傳 []，下一則訊息 PM 就失憶；現在最近一輪一定保留 (工具結果縮成摘要)
        trimmed = agent.trim_history(_turns(1, size=50000), 10, 1000)
        self.assertEqual(len(trimmed), 4)
        self.assertEqual(trimmed[0].parts[0].text, "問題0")

    def test_leading_fragment_without_user_text_is_dropped(self):
        history = [_result("search_web", "殘片"), _model("殘片回覆")] + _turns(2)
        trimmed = agent.trim_history(history, 10, 100000)
        self.assertEqual(trimmed[0].parts[0].text, "問題0")

    def test_trim_chat_rebuilds_with_trimmed_history(self):
        bot = agent.ArQAgent.__new__(agent.ArQAgent)
        bot.chats = {}
        chat = SimpleNamespace(get_history=lambda curated=False: _turns(15))
        rebuilt = object()
        with mock.patch.object(agent.ArQAgent, "get_or_create_chat", return_value=rebuilt) as create, \
                mock.patch("builtins.print"):
            self.assertIs(bot._trim_chat(7, chat), rebuilt)
        history = create.call_args.kwargs["history"]
        self.assertEqual(len(history), agent.MAX_HISTORY_TURNS * 4)

    def test_history_defaults_lowered(self):
        """：舊脈絡改由 ticket 查詢，歷史上限下修為 6 輪 / 15,000 字元 (實測穩定後每則約省 1,200 Token)"""
        self.assertEqual(agent.MAX_HISTORY_TURNS, 6)
        self.assertEqual(agent.MAX_HISTORY_CHARS, 15000)


class ThinkingLevelTest(unittest.TestCase):
    def _level(self, value):
        env = {} if value is None else {"GEMINI_THINKING_LEVEL": value}
        with mock.patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop("GEMINI_THINKING_LEVEL", None)
            return gemini_settings.thinking_level()

    def test_default_is_low(self):
        self.assertEqual(self._level(None), "LOW")

    def test_valid_levels(self):
        self.assertEqual(self._level("High"), "HIGH")
        self.assertEqual(self._level("medium"), "MEDIUM")

    def test_default_keyword_leaves_model_default(self):
        self.assertIsNone(self._level("default"))
        with mock.patch.dict(os.environ, {"GEMINI_THINKING_LEVEL": "default"}):
            self.assertIsNone(gemini_settings.thinking_config())

    def test_invalid_or_minimal_falls_back_to_low(self):
        with mock.patch("builtins.print"):
            self.assertEqual(self._level("minimal"), "LOW")
            self.assertEqual(self._level("super"), "LOW")

    def test_config_object(self):
        with mock.patch.dict(os.environ, {"GEMINI_THINKING_LEVEL": "low"}):
            config = gemini_settings.thinking_config()
        self.assertEqual(config.thinking_level, types.ThinkingLevel.LOW)


class UsageLogTest(unittest.TestCase):
    def test_usage_of_handles_missing_fields(self):
        response = SimpleNamespace(usage_metadata=SimpleNamespace(
            prompt_token_count=100, cached_content_token_count=None, candidates_token_count=5,
            thoughts_token_count=None, tool_use_prompt_token_count=None, total_token_count=105))
        usage = usage_log.usage_of(response)
        self.assertEqual(usage["prompt"], 100)
        self.assertEqual(usage["cached"], 0)
        self.assertEqual(usage_log.usage_of(SimpleNamespace())["total"], 0)

    def test_record_appends_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "usage.jsonl")
            with mock.patch.object(usage_log, "USAGE_LOG_PATH", path), mock.patch("builtins.print"):
                total = usage_log.empty_usage()
                usage_log.add_usage(total, {"prompt": 10, "total": 12})
                usage_log.add_usage(total, {"prompt": 20, "total": 25})
                usage_log.record_usage("chat", total, model="m")
                usage_log.record_usage("search_web", usage_log.empty_usage())
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["prompt"], 30)
        self.assertEqual(rows[0]["total"], 37)
        self.assertEqual(rows[0]["model"], "m")
        self.assertEqual(rows[1]["source"], "search_web")

    def test_tool_loop_records_summed_usage(self):
        """一則訊息內每次呼叫 Gemini 的用量會加總成一筆紀錄 (含工具迴圈)"""
        def response(calls, prompt):
            return SimpleNamespace(
                function_calls=calls, candidates=[],
                usage_metadata=SimpleNamespace(prompt_token_count=prompt, candidates_token_count=1, total_token_count=prompt + 1))

        replies = iter([
            response([SimpleNamespace(id="1", name="get_system_status", args={})], 1000),
            response(None, 1200),
        ])
        chat = SimpleNamespace(send_message=lambda payload, config=None: next(replies))
        bot = _bare_agent()
        recorded = {}
        with mock.patch.object(agent, "record_usage", side_effect=lambda src, u, **kw: recorded.update(u, src=src, **kw)), \
                mock.patch.object(agent.ArQAgent, "_execute_tool", return_value=({"ok": True}, [])), \
                mock.patch("builtins.print"):
            asyncio.run(bot._run_tool_loop(chat, "狀態", 1, None, None))
        self.assertEqual(recorded["src"], "chat")
        self.assertEqual(recorded["prompt"], 2200)
        self.assertEqual(recorded["calls"], 2)
        self.assertEqual(recorded["thinking"], "LOW")


def _bare_agent():
    bot = agent.ArQAgent.__new__(agent.ArQAgent)
    bot.model_name = "test-model"
    bot.thinking_level = "LOW"
    bot.deep_thinking_level = "HIGH"
    return bot


def _fake_response(calls=None):
    return SimpleNamespace(function_calls=calls, candidates=[], usage_metadata=None)


class DeepThinkingTest(unittest.TestCase):
    def test_wants_deep_thinking(self):
        self.assertTrue(gemini_settings.wants_deep_thinking("深思 這個 bug 怎麼修"))
        self.assertTrue(gemini_settings.wants_deep_thinking("幫我拆解這個專案"))
        self.assertTrue(gemini_settings.wants_deep_thinking("寫一份匯率爬蟲的規格"))
        self.assertFalse(gemini_settings.wants_deep_thinking("台北明天會下雨嗎"))
        self.assertFalse(gemini_settings.wants_deep_thinking(""))

    def test_deep_level_env(self):
        with mock.patch.dict(os.environ, {"GEMINI_DEEP_THINKING_LEVEL": "medium"}):
            self.assertEqual(gemini_settings.deep_thinking_level(), "MEDIUM")
        with mock.patch.dict(os.environ, {"GEMINI_DEEP_THINKING_LEVEL": "default"}):
            self.assertIsNone(gemini_settings.deep_thinking_level())
        env = dict(os.environ)
        env.pop("GEMINI_DEEP_THINKING_LEVEL", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(gemini_settings.deep_thinking_level(), "HIGH")

    def _run(self, prompt, replies):
        """執行工具迴圈，回傳每次送出時的 thinking 等級 (None = 沿用 chat 預設的一般等級)"""
        levels = []
        replies = iter(replies)

        def send(payload, config=None):
            levels.append(config.thinking_config.thinking_level.value if config else None)
            return next(replies)

        bot = _bare_agent()
        with mock.patch.object(agent, "record_usage"),                 mock.patch.object(agent.ArQAgent, "_execute_tool", return_value=({"ok": True}, [])),                 mock.patch("builtins.print"):
            asyncio.run(bot._run_tool_loop(SimpleNamespace(send_message=send), prompt, 1, None, None))
        return levels

    def test_normal_message_uses_chat_default(self):
        self.assertEqual(self._run("現在幾點", [_fake_response()]), [None])

    def test_planning_message_uses_deep_for_every_round(self):
        calls = [SimpleNamespace(id="1", name="search_web", args={})]
        levels = self._run("幫我規劃一個記帳 App", [_fake_response(calls), _fake_response()])
        self.assertEqual(levels, ["HIGH", "HIGH"])

    def test_dispatch_switches_to_deep_for_later_rounds(self):
        calls = [SimpleNamespace(id="1", name="dispatch_claude_engineer_sync", args={})]
        levels = self._run("幫我寫個匯率爬蟲", [_fake_response(calls), _fake_response()])
        self.assertEqual(levels, [None, "HIGH"])

    def test_override_config_keeps_tools_and_system_prompt(self):
        bot = _bare_agent()
        config = bot._chat_config("HIGH")
        self.assertEqual(config.system_instruction, agent.SYSTEM_INSTRUCTION)
        self.assertEqual(len(config.tools), len(agent._TOOL_SPECS))
        self.assertTrue(config.automatic_function_calling.disable)


if __name__ == "__main__":
    unittest.main()
