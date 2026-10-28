"""
ticket 內容精簡講重點：PM 與 RD 的提示詞都要帶這條規則。不啟動 RD、不連網。

背景： 的只讀規劃長 6,000 多字，原樣留言到 ticket 後難以閱讀；使用者要求 ticket 內容精簡講重點。

驗收標準：
    - PM 系統提示詞要求 ticket 與規格精簡講重點，且仍在 2,500 字上限內
    - RD 只讀規劃 prompt 要求精簡、限長，並說明會原樣留言到 ticket
    - 拆單規則要求子單內文精簡
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import agent  # noqa: E402
from tools import rd_common, ticket_tools  # noqa: E402


class ConciseTicketTests(unittest.TestCase):
    def test_pm_prompt_requires_concise_tickets(self):
        self.assertIn("ticket 與規格精簡講重點", agent.SYSTEM_INSTRUCTION)
        self.assertLess(len(agent.SYSTEM_INSTRUCTION), 2500)

    def test_plan_prompt_requires_concise_plan(self):
        prompt = rd_common.build_plan_prompt("研究 MCP", rd_common.SCOPE_CORE)
        for text in ("精簡講重點", "1,500 字內", "留言到 ticket", "不用表格"):
            self.assertIn(text, prompt)

    def test_split_rules_require_concise_child_tickets(self):
        self.assertIn("內文精簡", ticket_tools.SPLIT_RULES)


if __name__ == "__main__":
    unittest.main()
