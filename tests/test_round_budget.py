"""
 工具迴圈輪數上限與單則訊息 Token 預算 (不連網，mock Gemini 回應與確認回呼)

驗收標準：
    - MAX_TOOL_ROUNDS 下修 (預設 12)，可由 .env 的 GEMINI_MAX_TOOL_ROUNDS 設定
    - 單則訊息累計 Token 超過預算 (GEMINI_MESSAGE_TOKEN_BUDGET，預設 60,000) 時，停止並請使用者決定是否繼續
    - 停下時讓模型以文字總結進度 (禁止再呼叫工具)，不再清空對話記憶；總結失敗才重置
    - 用量紀錄附上每輪呼叫的工具名稱，之後可追查失控的訊息
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


def response(calls=None, total=1000, text=""):
    parts = [SimpleNamespace(text=text, thought=None, function_call=None)] if text else []
    return SimpleNamespace(
        function_calls=calls,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=total - 10, candidates_token_count=10, total_token_count=total),
    )


def tool_call(name="get_system_status", i=0):
    return [SimpleNamespace(id=f"c{i}", name=name, args={})]


class FakeChat:
    """依序回傳預設回應，並記錄每次送出的 payload 與 config"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def send_message(self, payload, config=None):
        self.sent.append((payload, config))
        return self.replies.pop(0)


def bare_agent():
    a = agent.ArQAgent.__new__(agent.ArQAgent)
    a.model_name = "test-model"
    a.thinking_level = "LOW"
    a.deep_thinking_level = "HIGH"
    a.chats = {1: "chat"}
    return a


def run(a, chat, confirm=None, rounds=None, budget=None, recorded=None):
    patches = [mock.patch.object(agent.ArQAgent, "_execute_tool", return_value=({"ok": True}, [])),
               mock.patch.object(agent, "record_usage",
                                 side_effect=lambda src, u, **kw: (recorded if recorded is not None else {}).update(kw)),
               mock.patch("builtins.print")]
    if rounds is not None:
        patches.append(mock.patch.object(agent, "MAX_TOOL_ROUNDS", rounds))
    if budget is not None:
        patches.append(mock.patch.object(agent, "MESSAGE_TOKEN_BUDGET", budget))
    for p in patches:
        p.start()
    try:
        return asyncio.run(a._run_tool_loop(chat, "幫我查", 1, confirm, None))
    finally:
        for p in reversed(patches):
            p.stop()


def tools_disabled(config):
    fcc = getattr(getattr(config, "tool_config", None), "function_calling_config", None)
    return str(getattr(fcc, "mode", "")).upper().endswith("NONE")


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(agent.MAX_TOOL_ROUNDS, 12)
        self.assertEqual(agent.MESSAGE_TOKEN_BUDGET, 60000)

    def test_env_parsing(self):
        with mock.patch.dict(os.environ, {"X_TEST_INT": "7"}):
            self.assertEqual(agent._int_env("X_TEST_INT", 3), 7)
        for bad in ("", "abc", "-5", "0"):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {"X_TEST_INT": bad}):
                self.assertEqual(agent._int_env("X_TEST_INT", 3), 3)


class TestRoundLimit(unittest.TestCase):
    def test_rounds_exhausted_asks_model_to_summarize_without_tools(self):
        a = bare_agent()
        chat = FakeChat([response(tool_call(i=i)) for i in range(4)] + [response(text="已查到一半，目前狀態…")])
        res = run(a, chat, rounds=3)
        self.assertEqual(len(chat.sent), 5)                 # 1 + 3 輪 + 1 次總結
        payload, config = chat.sent[-1]
        self.assertTrue(tools_disabled(config))
        self.assertIn("上限", str(payload[0].function_response.response))
        self.assertIn("已查到一半", res["text"])
        self.assertIn("輪", res["text"])                    # 告知使用者是因為輪數上限停下
        self.assertIn(1, a.chats)                           # 對話記憶保留

    def test_summary_failure_falls_back_to_reset(self):
        a = bare_agent()
        chat = FakeChat([response(tool_call(i=i)) for i in range(5)])
        res = run(a, chat, rounds=3)
        self.assertNotIn(1, a.chats)
        self.assertIn("重置", res["text"])

    def test_normal_message_unaffected(self):
        a = bare_agent()
        chat = FakeChat([response(tool_call()), response(text="CPU 20%")])
        res = run(a, chat, rounds=3)
        self.assertEqual(res["text"], "CPU 20%")
        self.assertEqual(len(chat.sent), 2)


class TestTokenBudget(unittest.TestCase):
    def _confirm(self, answers):
        asked = []

        async def confirm(title, detail):
            asked.append((title, detail))
            return answers.pop(0)
        return confirm, asked

    def test_over_budget_asks_and_stops_when_declined(self):
        a = bare_agent()
        chat = FakeChat([response(tool_call(), total=4000), response(tool_call(i=1), total=4000),
                         response(text="先停在這裡")])
        confirm, asked = self._confirm([False])
        res = run(a, chat, confirm=confirm, budget=5000)
        self.assertEqual(len(asked), 1)
        self.assertIn("8000", asked[0][0] + asked[0][1])    # 已用 Token
        self.assertIn("5000", asked[0][0] + asked[0][1])    # 預算
        self.assertTrue(tools_disabled(chat.sent[-1][1]))
        self.assertIn("先停在這裡", res["text"])
        self.assertIn("預算", res["text"])

    def test_approved_extends_budget(self):
        a = bare_agent()
        chat = FakeChat([response(tool_call(), total=4000), response(tool_call(i=1), total=4000),
                         response(tool_call(i=2), total=1000), response(text="完成")])
        confirm, asked = self._confirm([True])
        res = run(a, chat, confirm=confirm, budget=5000)
        self.assertEqual(len(asked), 1)                      # 9,000 < 5,000 + 5,000，不再詢問
        self.assertEqual(res["text"], "完成")

    def test_no_confirm_callback_stops(self):
        a = bare_agent()
        chat = FakeChat([response(tool_call(), total=6000), response(text="摘要")])
        res = run(a, chat, confirm=None, budget=5000)
        self.assertTrue(tools_disabled(chat.sent[-1][1]))
        self.assertIn("摘要", res["text"])

    def test_final_answer_over_budget_is_not_blocked(self):
        a = bare_agent()
        chat = FakeChat([response(text="答案", total=9000)])
        confirm, asked = self._confirm([])
        res = run(a, chat, confirm=confirm, budget=5000)
        self.assertEqual(res["text"], "答案")
        self.assertEqual(asked, [])


class TestToolTrace(unittest.TestCase):
    def test_usage_record_lists_tools_per_round(self):
        a = bare_agent()
        chat = FakeChat([response(tool_call("get_system_status")), response(tool_call("search_web", 1)),
                         response(text="ok")])
        recorded = {}
        run(a, chat, rounds=5, recorded=recorded)
        self.assertEqual(recorded["tools"], [["get_system_status"], ["search_web"]])


if __name__ == "__main__":
    unittest.main()
