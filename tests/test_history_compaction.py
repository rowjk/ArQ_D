"""
 對話歷史在單一輪過大時整段被清空 (PM 回「yes」就失憶)。不連網。

驗收標準：
    - 裁切永遠保留最近一個完整輪次，不會整段清空
    - 歷史中的工具結果 (function_response) 縮成摘要；模型的 function_call 與 thought_signature 不變
    - 縮減後仍超過上限才丟最舊的輪次
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from google.genai import types

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import agent  # noqa: E402


def user(text):
    return types.Content(role="user", parts=[types.Part(text=text)])


def model_call(name="run_powershell_sync", sig=b"signature-bytes"):
    return types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(id=f"id-{name}", name=name, args={"command": "git status"}),
        thought_signature=sig)])


def tool_result(name="run_powershell_sync", size=50):
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=f"id-{name}", name=name, response={"stdout": "x" * size}))])


def model_text(text):
    return types.Content(role="model", parts=[types.Part(text=text)])


def turn(question, tool_size=50, answer="好"):
    return [user(question), model_call(), tool_result(size=tool_size), model_text(answer)]


class TestCompaction(unittest.TestCase):
    def test_large_tool_result_is_summarized_and_ids_kept(self):
        history = turn("查狀態", tool_size=20000)
        out = agent.trim_history(history, max_turns=6, max_chars=100000, tool_result_chars=600)
        fr = out[2].parts[0].function_response
        self.assertEqual((fr.id, fr.name), ("id-run_powershell_sync", "run_powershell_sync"))
        text = str(fr.response)
        self.assertLess(len(text), 800)
        self.assertIn("縮減", text)

    def test_model_parts_untouched(self):
        history = turn("查狀態", tool_size=20000)
        out = agent.trim_history(history, max_turns=6, max_chars=100000, tool_result_chars=600)
        self.assertIs(out[1], history[1])                     # 模型的 function_call (含 thought_signature) 原物件
        self.assertEqual(out[1].parts[0].thought_signature, b"signature-bytes")
        self.assertIs(out[3], history[3])

    def test_small_results_and_unchanged_history_return_same_object(self):
        history = turn("一") + turn("二")
        self.assertIs(agent.trim_history(history, max_turns=6, max_chars=100000, tool_result_chars=600), history)

    def test_compaction_avoids_dropping_turns(self):
        history = turn("一", tool_size=20000) + turn("二", tool_size=20000) + turn("yes 之前的對焦")
        out = agent.trim_history(history, max_turns=6, max_chars=15000, tool_result_chars=600)
        self.assertEqual(len(out), len(history))              # 縮減後就在上限內，不必丟輪次
        self.assertEqual(out[0].parts[0].text, "一")


class TestNeverWipeLatestTurn(unittest.TestCase):
    def test_latest_turn_kept_even_if_over_limit(self):
        huge_answer = "對焦內容" * 10000                       # 模型回覆本身就超過上限 (無法縮減)
        history = turn("舊問題") + turn("開始進行#10的修改", answer=huge_answer)
        out = agent.trim_history(history, max_turns=6, max_chars=15000, tool_result_chars=600)
        self.assertTrue(out)                                  # 不會整段清空
        self.assertEqual(out[0].parts[0].text, "開始進行#10的修改")
        self.assertEqual(len(out), 4)                         # 只丟較舊的那一輪

    def test_turn_limit_still_applies(self):
        history = sum((turn(f"問題{i}") for i in range(10)), [])
        out = agent.trim_history(history, max_turns=6, max_chars=10 ** 9, tool_result_chars=600)
        self.assertEqual(out[0].parts[0].text, "問題4")


class TestTrimChatRebuild(unittest.TestCase):
    def _agent(self):
        a = agent.ArQAgent.__new__(agent.ArQAgent)
        a.chats = {}
        return a

    def test_rebuilds_when_compaction_changed_history(self):
        a = self._agent()
        chat = SimpleNamespace(get_history=lambda curated=False: turn("查", tool_size=20000))
        rebuilt = object()
        with mock.patch.object(agent.ArQAgent, "get_or_create_chat", return_value=rebuilt) as create, \
                mock.patch("builtins.print"):
            self.assertIs(a._trim_chat(1, chat), rebuilt)
        self.assertEqual(len(create.call_args.kwargs["history"]), 4)

    def test_no_rebuild_when_nothing_changed(self):
        a = self._agent()
        chat = SimpleNamespace(get_history=lambda curated=False: turn("查"))
        with mock.patch.object(agent.ArQAgent, "get_or_create_chat") as create:
            self.assertIs(a._trim_chat(1, chat), chat)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
