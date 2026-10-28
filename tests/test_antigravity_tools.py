"""
tools/antigravity_tools.py 單元測試：安裝 / 高風險指令的「中斷 → 詢問 → 接續」流程

不實際呼叫 agy：以 mock 取代 _run_agy_once，驗證 run_antigravity_task 的決策邏輯。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import antigravity_tools as agy  # noqa: E402


def _run(response="完成", interrupted=None, conversation_id="conv-1", status="SUCCESS"):
    """組出 _run_agy_once 的假回傳值"""
    return {
        "conversation_id": conversation_id, "response": response, "status": status,
        "interrupted": interrupted, "timed_out": False, "returncode": 0, "other_output": "",
    }


class TestHelpers(unittest.TestCase):
    def test_extract_tool_command(self):
        step = {"step_type": "tool", "state": "ACTIVE", "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "pip install six"}}}
        self.assertEqual(agy._extract_tool_command(step), "pip install six")
        self.assertIsNone(agy._extract_tool_command({"step_type": "agent_response"}))
        self.assertIsNone(agy._extract_tool_command(
            {"step_type": "tool", "tool_info": {"parameters": {"AbsolutePath": "x.py"}}}))

    def test_install_request_marker(self):
        text = "需要套件\nARQ_INSTALL_REQUEST: pip install openpyxl\n  ARQ_INSTALL_REQUEST: npm i lodash  \n其他"
        self.assertEqual(agy._INSTALL_REQUEST_RE.findall(text), ["pip install openpyxl", "npm i lodash"])

    def test_prompt_forbids_self_install(self):
        prompt = agy._build_pm_prompt("寫一個爬蟲")
        self.assertIn("嚴禁自行安裝套件", prompt)
        self.assertIn("ARQ_INSTALL_REQUEST", prompt)


@patch.object(agy, "resolve_agy_executable", return_value="agy.exe")
@patch.object(agy, "report_new_packages")
class TestRunAntigravityTask(unittest.TestCase):
    def _call(self, runs, approve_answer):
        asked = []

        def approve(title, detail):
            asked.append(title)
            return approve_answer

        with patch.object(agy, "_run_agy_once", side_effect=runs) as mock_once:
            res = agy.run_antigravity_task("任務", notify=lambda m: None, approve=approve)
        return res, asked, mock_once

    def test_normal_run_no_questions(self, mock_report, _exe):
        res, asked, mock_once = self._call([_run("完成")], True)
        self.assertTrue(res["success"])
        self.assertEqual(asked, [])
        self.assertEqual(mock_once.call_count, 1)

    def test_interrupted_install_approved_then_resumed(self, mock_report, _exe):
        runs = [_run("", interrupted=("pip install six", "安裝新套件")), _run("完成")]
        res, asked, mock_once = self._call(runs, True)
        self.assertTrue(res["success"])
        self.assertEqual(len(asked), 1)
        self.assertEqual(res["install_commands"], ["pip install six"])
        self.assertEqual(res["interrupted_commands"], ["pip install six"])
        # 第二次呼叫要接續同一段對話，且已同意的指令列入白名單
        second_args = mock_once.call_args_list[1][0]
        self.assertEqual(second_args[3], "conv-1")
        self.assertIn("pip install six", second_args[5])

    def test_interrupted_install_denied(self, mock_report, _exe):
        runs = [_run("", interrupted=("pip install six", "安裝新套件")), _run("已略過安裝")]
        res, asked, mock_once = self._call(runs, False)
        self.assertEqual(res["blocked_commands"], ["pip install six"])
        self.assertEqual(res["install_commands"], [])
        self.assertIn("拒絕", mock_once.call_args_list[1][0][1])

    def test_install_request_marker_asks_user(self, mock_report, _exe):
        runs = [_run("需要套件\nARQ_INSTALL_REQUEST: pip install openpyxl"), _run("完成")]
        res, asked, mock_once = self._call(runs, True)
        self.assertEqual(len(asked), 1)
        self.assertEqual(res["install_commands"], ["pip install openpyxl"])
        self.assertIn("同意：pip install openpyxl", mock_once.call_args_list[1][0][1])

    def test_no_approver_means_deny(self, mock_report, _exe):
        runs = [_run("", interrupted=("pip install six", "安裝新套件")), _run("完成")]
        with patch.object(agy, "_run_agy_once", side_effect=runs):
            res = agy.run_antigravity_task("任務")
        self.assertEqual(res["blocked_commands"], ["pip install six"])

    def test_resume_round_limit(self, mock_report, _exe):
        # agy 一直嘗試被拒絕的不同指令：達到輪數上限後必須停止
        runs = [_run("", interrupted=(f"pip install p{i}", "安裝新套件")) for i in range(agy.MAX_RESUME_ROUNDS + 1)]
        res, asked, mock_once = self._call(runs, False)
        self.assertFalse(res["success"])
        self.assertEqual(mock_once.call_count, agy.MAX_RESUME_ROUNDS + 1)


if __name__ == "__main__":
    unittest.main()
