"""
 派工時由程式自動開單 (不再靠 PM 提示詞)

    - run_dev_task(opened_by=...)：開工前以 task_description 開單 (by:<開單者>、rd:<引擎>、status:in-progress)
    - 已帶 ticket 單號 → 沿用，不重複開單；接續原對話 (session_id) 且沒帶單號 → 不開新單
    - 開單失敗只通知、不影響派工；未設定 ticket 功能時靜默略過
    - 斜線指令 by:user、Gemini by:pm；結果、精簡結果與完整回報都帶單號
    - SYSTEM_INSTRUCTION 不再要求 PM 自己開單
"""

import inspect
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch, ticket_tools  # noqa: E402
from tools.rd_dispatch import ANTIGRAVITY, CLAUDE, run_dev_task  # noqa: E402

OK_AUTH = {"authenticated": True, "message": "ok"}
TASK = "寫一個匯率爬蟲\n輸入：無\n驗收標準：\n- [ ] 存成 CSV"


def ok():
    return {"success": True, "stdout": "完成", "stderr": "", "returncode": 0, "session_id": "s1"}


_LOG_CASE = unittest.TestCase()   # 只借用 assertLogs（Run 不是 TestCase）


class Run:
    """執行 run_dev_task，mock 掉工程師與 ticket API，記錄呼叫"""

    def __init__(self, create_result=None, update_result=None):
        self.create_result = create_result or {"success": True, "number": 42, "url": "https://github.com/o/r/issues/42"}
        self.update_result = update_result or {"success": True}

    def __call__(self, preferred=CLAUDE, **kwargs):
        notes = []
        with patch.object(rd_dispatch, "check_claude_auth", return_value=OK_AUTH), \
             patch.object(rd_dispatch, "check_antigravity_auth", return_value=OK_AUTH), \
             patch.object(rd_dispatch, "run_claude_task", return_value=ok()), \
             patch.object(rd_dispatch, "run_antigravity_task", return_value=ok()), \
             patch.object(ticket_tools, "create_ticket", return_value=self.create_result) as m_create, \
             patch.object(ticket_tools, "update_ticket", return_value=self.update_result) as m_update, \
             patch.object(ticket_tools, "get_ticket", return_value={"success": False, "error": "尚未設定"}), \
             _LOG_CASE.assertLogs(level="INFO"):
            # get_ticket： 起帶既有單號派工會讀取 ticket 內容，測試不得連到 GitHub
            res = run_dev_task(TASK, preferred, notify=notes.append, **kwargs)
        return res, notes, m_create, m_update


class TestAutoTicket(unittest.TestCase):
    def setUp(self):
        rd_dispatch._reset_session_tickets_for_tests()
        self.addCleanup(rd_dispatch._reset_session_tickets_for_tests)

    def test_opens_ticket_before_dispatch(self):
        res, notes, m_create, _ = Run()(opened_by="pm")
        m_create.assert_called_once()
        kw = m_create.call_args.kwargs
        self.assertEqual(kw["title"], "寫一個匯率爬蟲")
        self.assertEqual(kw["body"], TASK)
        self.assertEqual(kw["opened_by"], "pm")
        self.assertEqual(kw["engine"], "claude")
        self.assertEqual(kw["status"], "in-progress")
        self.assertEqual(res["ticket"], 42)
        self.assertTrue(any("#42" in n for n in notes))

    def test_engine_label_for_antigravity(self):
        _, _, m_create, _ = Run()(ANTIGRAVITY, opened_by="user")
        self.assertEqual(m_create.call_args.kwargs["engine"], "agy")
        self.assertEqual(m_create.call_args.kwargs["opened_by"], "user")

    def test_existing_ticket_reused(self):
        res, _, m_create, m_update = Run()(opened_by="pm", ticket=7)
        m_create.assert_not_called()
        m_update.assert_called_once()
        self.assertEqual(m_update.call_args.args[0], 7)
        self.assertEqual(res["ticket"], 7)

    def test_resume_without_ticket_reuses_session_ticket(self):
        # 接續原對話沒帶單號時找回該 session 的單，不重複開單、改 in-progress
        rd_dispatch.remember_session_ticket("s-old", 31)
        res, _, m_create, m_update = Run()(opened_by="pm", session_id="s-old")
        m_create.assert_not_called()
        self.assertEqual(m_update.call_args.args[0], 31)
        self.assertEqual(m_update.call_args.kwargs["status"], "in-progress")
        self.assertEqual(res["ticket"], 31)

    def test_resume_unknown_session_opens_ticket(self):
        # 找不到對應的單時開新單，不再整串沒有單
        res, _, m_create, _ = Run()(opened_by="pm", session_id="s-unknown")
        m_create.assert_called_once()
        self.assertEqual(res["ticket"], 42)

    def test_dispatch_remembers_session_ticket(self):
        Run()(opened_by="pm")
        self.assertEqual(rd_dispatch.session_ticket("s1"), 42)

    def test_board_note_is_notified(self):
        # 看板未設定 / 同步失敗的提示要送到 Discord
        note = "⚠️ 看板未設定，跳過看板同步"
        _, notes, _, _ = Run(create_result={"success": True, "number": 42, "url": "u", "board_note": note})(
            opened_by="pm")
        self.assertTrue(any(note in n for n in notes))
        _, notes, _, _ = Run(update_result={"success": True, "board_note": note})(opened_by="pm", ticket=7)
        self.assertTrue(any(note in n for n in notes))

    def test_no_opener_no_ticket(self):
        # 直接呼叫 run_dev_task (例如測試或內部用途) 不開單
        _, _, m_create, m_update = Run()()
        m_create.assert_not_called()
        m_update.assert_not_called()

    def test_ticket_failure_does_not_block_dispatch(self):
        res, notes, _, _ = Run(create_result={"success": False, "error": "GitHub API 錯誤 500"})(opened_by="pm")
        self.assertTrue(res["success"])
        self.assertIsNone(res.get("ticket"))
        self.assertTrue(any("開單失敗" in n for n in notes))

    def test_not_configured_is_silent(self):
        res, notes, _, _ = Run(create_result={"success": False, "error": "Ticket 功能尚未設定：請在 .env 填入"})(opened_by="pm")
        self.assertTrue(res["success"])
        self.assertFalse(any("開單" in n for n in notes))

    def test_title_from_first_meaningful_line(self):
        self.assertEqual(rd_dispatch.ticket_title_from_task("\n\n## 目標：做匯率爬蟲\n細節"), "目標：做匯率爬蟲")
        self.assertLessEqual(len(rd_dispatch.ticket_title_from_task("長" * 300)), 80)
        self.assertEqual(rd_dispatch.ticket_title_from_task("   "), "RD 開發任務")


class TestCreateTicketExtras(unittest.TestCase):
    def test_create_ticket_engine_label_and_status(self):
        sent = {}

        def fake_api(method, path, body=None, query=None):
            sent.update(body or {})
            return {"number": 1, "html_url": "u"}, None, 201

        with patch.object(ticket_tools, "_api", fake_api):
            ticket_tools.create_ticket(title="t", body="b", opened_by="pm", engine="claude", status="in-progress")
        self.assertIn("rd:claude", sent["labels"])
        self.assertIn("status:in-progress", sent["labels"])
        self.assertNotIn("status:todo", sent["labels"])

    def test_create_ticket_rejects_bad_status(self):
        res = ticket_tools.create_ticket(title="t", body="b", opened_by="pm", status="shipped")
        self.assertFalse(res["success"])


class TestEntryPoints(unittest.TestCase):
    def test_gemini_entry_points_open_as_pm_and_accept_ticket(self):
        for fn, engine_mock in ((rd_dispatch.run_claude_first, "run_claude_task"),
                                (rd_dispatch.run_antigravity_first, "run_antigravity_task")):
            with patch.object(rd_dispatch, "run_dev_task", return_value={"success": True}) as m:
                fn("任務", ticket=9)
            self.assertEqual(m.call_args.kwargs["opened_by"], "pm")
            self.assertEqual(m.call_args.kwargs["ticket"], 9)

    def test_gemini_tool_facades_expose_ticket_param(self):
        import agent
        for name in ("dispatch_claude_engineer_sync", "dispatch_antigravity_engineer_sync"):
            params = inspect.signature(agent.TOOL_REGISTRY[name].func).parameters
            self.assertIn("ticket", params)
            self.assertNotIn("opened_by", params)

    def test_slash_commands_open_as_user(self):
        import bot
        src = inspect.getsource(bot.run_slash_dev_task)
        self.assertIn('opened_by="user"', src)


class TestReportsCarryTicket(unittest.TestCase):
    def test_compact_and_report_include_ticket(self):
        res = {**ok(), "engine_label": "Claude Code", "ticket": 42}
        self.assertEqual(rd_dispatch.compact_dev_result(res)["ticket"], 42)
        self.assertIn("#42", rd_dispatch.format_dev_report(res))


class TestPrompt(unittest.TestCase):
    def test_pm_no_longer_opens_ticket_before_dispatch(self):
        import agent
        text = agent.SYSTEM_INSTRUCTION
        self.assertNotIn("派工前先用 ticket 開單", text)
        self.assertIn("派工時系統會自動開單", text)
        self.assertIn("ticket", text.split("session_id", 1)[1][:80])  # 接續原對話時一併帶入單號


if __name__ == "__main__":
    unittest.main()
