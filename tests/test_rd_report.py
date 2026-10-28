"""
 RD 回報直接送 Discord，Gemini 只拿精簡摘要

    - format_dev_report()：給使用者的完整回報 (成果、檔案、安裝 / 阻擋 / 中斷指令、驗收卡)
    - compact_dev_result()：給 Gemini 的精簡結果，總長有上限，PM 需要的決策資訊不得遺失
    - agent._execute_tool：派工工具執行後，完整回報經 notify 送出，模型只收到精簡結果
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch  # noqa: E402
from tools.rd_dispatch import MAX_MODEL_RESULT_CHARS, compact_dev_result, format_dev_report  # noqa: E402

LONG_OUT = "編譯與測試紀錄\n" * 600 + "全部完成。\n請問匯率要用哪個資料來源？A) 台銀 B) 央行"


def full_result(**extra):
    res = {
        "success": True, "engine": "claude", "engine_label": "Claude Code", "scope": "core",
        "stdout": LONG_OUT, "stderr": "很長的 stderr" * 500, "returncode": 0, "session_id": "sess-123",
        "install_commands": ["uv add requests"], "blocked_commands": ["git push"],
        "interrupted_commands": ["pip install x"], "artifacts": ["artifact/rate.csv"],
        "attempts": [{"engine": "antigravity", "error": "quota exceeded " * 50}], "cost_usd": 0.12,
        "acceptance": {"diff_stat": " tools/x.py | 10 +++\n" * 80, "status": "?? tools/new.py",
                       "tests_passed": True, "tests_summary": "....\n" * 200 + "Ran 240 tests\nOK"},
    }
    res.update(extra)
    return res


class TestCompactResult(unittest.TestCase):
    def test_size_capped(self):
        compact = compact_dev_result(full_result())
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), MAX_MODEL_RESULT_CHARS)
        self.assertLessEqual(MAX_MODEL_RESULT_CHARS, 1500)

    def test_keeps_decision_info(self):
        c = compact_dev_result(full_result())
        self.assertTrue(c["success"])
        self.assertEqual(c["engine_label"], "Claude Code")
        self.assertEqual(c["session_id"], "sess-123")
        self.assertEqual(c["scope"], "core")
        self.assertEqual(c["install_commands"], ["uv add requests"])
        self.assertEqual(c["blocked_commands"], ["git push"])
        self.assertEqual(c["interrupted_commands"], ["pip install x"])
        self.assertTrue(c["acceptance"]["tests_passed"])
        self.assertIn("Ran 240 tests", c["acceptance"]["tests_summary"])
        # 工程師提問通常在最後，必須保留給 PM 轉達
        self.assertIn("請問匯率要用哪個資料來源", c["report_tail"])
        self.assertIn("已直接顯示", c["note"])

    def test_drops_bulky_fields(self):
        c = compact_dev_result(full_result())
        self.assertNotIn("stdout", c)
        self.assertNotIn("stderr", c)
        self.assertNotIn("編譯與測試紀錄\n編譯與測試紀錄", json.dumps(c, ensure_ascii=False))

    def test_failure_keeps_error(self):
        c = compact_dev_result({"success": False, "error": "所有 RD 工程師都無法完成任務。" + "x" * 3000,
                                "attempts": [{"engine": "claude", "error": "e"}], "scope": "task"})
        self.assertFalse(c["success"])
        self.assertIn("所有 RD 工程師都無法完成任務", c["error"])
        self.assertLessEqual(len(json.dumps(c, ensure_ascii=False)), MAX_MODEL_RESULT_CHARS)


class TestUserReport(unittest.TestCase):
    def test_success_report_contents(self):
        text = format_dev_report(full_result())
        self.assertIn("Claude Code", text)
        self.assertIn("全部完成", text)
        self.assertIn("請問匯率要用哪個資料來源", text)
        self.assertIn("artifact/rate.csv", text)
        self.assertIn("uv add requests", text)
        self.assertIn("git push", text)
        self.assertIn("pip install x", text)
        self.assertIn("Ran 240 tests", text)
        self.assertIn("Antigravity", text)  # 接手紀錄
        self.assertNotIn("\n\n\n", text)

    def test_report_length_reasonable(self):
        # 由 bot 分段送出；仍限制總長，避免洗版
        self.assertLessEqual(len(format_dev_report(full_result())), 6000)

    def test_failure_report(self):
        text = format_dev_report({"success": False, "error": "認證未就緒", "attempts": []})
        self.assertIn("失敗", text)
        self.assertIn("認證未就緒", text)


class TestAgentWiring(unittest.TestCase):
    def test_dispatch_specs_use_report_and_compact(self):
        import agent
        for name in ("dispatch_claude_engineer_sync", "dispatch_antigravity_engineer_sync"):
            spec = agent.TOOL_REGISTRY[name]
            self.assertIs(spec.model_view, compact_dev_result)
            self.assertIs(spec.user_report, format_dev_report)

    def test_execute_tool_sends_report_and_returns_compact(self):
        import agent
        sent = []

        async def notify(text):
            sent.append(text)

        spec = agent.TOOL_REGISTRY["dispatch_claude_engineer_sync"]
        fake_impl = lambda **kwargs: full_result()  # noqa: E731
        with patch.dict(agent.TOOL_REGISTRY, {"dispatch_claude_engineer_sync": spec.__class__(
                func=spec.func, label=spec.label, risk=None, impl=fake_impl,
                model_view=spec.model_view, user_report=spec.user_report)}):
            a = agent.ArQAgent(api_key="")
            with self.assertLogs(level="INFO"):
                result, files = asyncio.run(a._execute_tool(
                    "dispatch_claude_engineer_sync", {"task_description": "寫匯率爬蟲"}, None, notify))
        self.assertEqual(len(sent), 1)
        self.assertIn("全部完成", sent[0])
        self.assertNotIn("stdout", result)
        self.assertEqual(result["session_id"], "sess-123")
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), MAX_MODEL_RESULT_CHARS)

    def test_prompt_tells_pm_not_to_repeat_report(self):
        import agent
        self.assertIn("完整回報系統已直接顯示", agent.SYSTEM_INSTRUCTION)
        self.assertLess(len(agent.SYSTEM_INSTRUCTION), 2500)


class TestBotNotifySplits(unittest.TestCase):
    def test_long_notify_is_split_not_truncated(self):
        import inspect
        import bot
        src = inspect.getsource(bot.on_message)
        self.assertIn("split_message(redact(text)", src)


if __name__ == "__main__":
    unittest.main()
