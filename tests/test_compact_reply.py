"""
 Gemini 回覆由程式壓掉空行與分隔線 (保留程式碼區塊)。不連網。

事件：「列出你現在的開發流程」的回覆，每個大標題後面都多了一個空行，違反 SYSTEM_INSTRUCTION 的排版規範。
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


class TestCompactReply(unittest.TestCase):
    def test_blank_lines_removed(self):
        text = "1. **需求分流**\n\n• 簡單任務\n\n\n• 複雜開發\n   \n2. **對焦**"
        self.assertEqual(agent.compact_reply(text), "1. **需求分流**\n• 簡單任務\n• 複雜開發\n2. **對焦**")

    def test_separator_lines_removed(self):
        text = "上半\n---\n下半\n***\n再下\n___\n結尾\n- 清單項目不受影響"
        self.assertEqual(agent.compact_reply(text), "上半\n下半\n再下\n結尾\n- 清單項目不受影響")

    def test_code_block_kept_verbatim(self):
        code = "```python\ndef a():\n    pass\n\n\ndef b():\n    return '---'\n```"
        text = f"說明\n\n{code}\n\n結尾"
        self.assertEqual(agent.compact_reply(text), f"說明\n{code}\n結尾")

    def test_unclosed_code_block_kept(self):
        text = "說明\n\n```\nx = 1\n\ny = 2"
        self.assertEqual(agent.compact_reply(text), "說明\n```\nx = 1\n\ny = 2")

    def test_plain_text_unchanged_and_edges_trimmed(self):
        self.assertEqual(agent.compact_reply("好"), "好")
        self.assertEqual(agent.compact_reply("\n\n內容\n\n"), "內容")
        self.assertEqual(agent.compact_reply(""), "")


def response(text="", calls=None):
    parts = [SimpleNamespace(text=text, thought=None, function_call=None)] if text else []
    return SimpleNamespace(function_calls=calls, candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
                           usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=1,
                                                          total_token_count=11))


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)

    def send_message(self, payload, config=None):
        return self.replies.pop(0)


def bare_agent():
    a = agent.ArQAgent.__new__(agent.ArQAgent)
    a.model_name, a.thinking_level, a.deep_thinking_level, a.chats = "m", "LOW", "HIGH", {1: "chat"}
    return a


class TestAppliedToReplies(unittest.TestCase):
    def _run(self, replies, rounds=None):
        patches = [mock.patch.object(agent.ArQAgent, "_execute_tool", return_value=({"ok": True}, [])),
                   mock.patch.object(agent, "record_usage"), mock.patch("builtins.print")]
        if rounds is not None:
            patches.append(mock.patch.object(agent, "MAX_TOOL_ROUNDS", rounds))
        for p in patches:
            p.start()
        try:
            return asyncio.run(bare_agent()._run_tool_loop(FakeChat(replies), "列出開發流程", 1, None, None))
        finally:
            for p in reversed(patches):
                p.stop()

    def test_final_reply_compacted(self):
        res = self._run([response("1. **分流**\n\n• 簡單\n---\n2. **對焦**")])
        self.assertEqual(res["text"], "1. **分流**\n• 簡單\n2. **對焦**")

    def test_wrap_up_reply_compacted(self):
        calls = [SimpleNamespace(id="c", name="get_system_status", args={})]
        res = self._run([response(calls=calls), response(calls=calls), response("進度\n\n還差一步")], rounds=1)
        self.assertNotIn("\n\n", res["text"])
        self.assertIn("進度\n還差一步", res["text"])


if __name__ == "__main__":
    unittest.main()
