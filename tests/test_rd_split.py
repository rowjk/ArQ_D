"""
Ticket 單元測試：rd_dispatch 拆出回報格式與 ticket 脈絡

驗收標準：
- [ ] 新增 tools/rd_report.py：精簡結果與各種回報排版
- [ ] 新增 tools/rd_context.py：ticket 標題、單號解析、相依檢查、附加給 RD 的 ticket／規劃脈絡
- [ ] CLAUDE、ANTIGRAVITY、ENGINE_LABELS、_safe_notify 移到 tools/rd_common.py（三個新模組共用，避免循環 import）
- [ ] rd_report／rd_context 不得 import rd_dispatch；rd_dispatch 重新匯出所有搬走的公開名稱（bot.py、agent.py 的 import 不用改）
- [ ] rd_dispatch.py <= 620 行（由 520 修正）；內容逐字搬移，行為不變
- [ ] 全部測試通過；測試只允許改 patch 目標
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外


class TestRdModulesIsolation(unittest.TestCase):
    """驗證 rd_report 與 rd_context 模組的獨立性（不依賴 rd_dispatch）"""

    def test_no_rd_dispatch_in_rd_report_and_rd_context(self):
        """tools/rd_report.py 與 tools/rd_context.py 原始碼不得含 rd_dispatch"""
        tools_dir = os.path.join(PROJECT_ROOT, "tools")
        for fname in ("rd_report.py", "rd_context.py"):
            fpath = os.path.join(tools_dir, fname)
            self.assertTrue(os.path.isfile(fpath), f"{fname} 檔案應存在")
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            for line_no, line in enumerate(content.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                self.assertNotIn("rd_dispatch", stripped, f"{fname}:{line_no} 不得包含 rd_dispatch")

    def test_rd_common_has_shared_items(self):
        """驗證 CLAUDE、ANTIGRAVITY、ENGINE_LABELS、_safe_notify 已在 tools/rd_common.py"""
        from tools import rd_common
        self.assertTrue(hasattr(rd_common, "CLAUDE"))
        self.assertTrue(hasattr(rd_common, "ANTIGRAVITY"))
        self.assertTrue(hasattr(rd_common, "ENGINE_LABELS"))
        self.assertTrue(hasattr(rd_common, "_safe_notify"))
        self.assertEqual(rd_common.CLAUDE, "claude")
        self.assertEqual(rd_common.ANTIGRAVITY, "antigravity")
        self.assertEqual(rd_common.ENGINE_LABELS[rd_common.CLAUDE], "Claude Code")
        self.assertEqual(rd_common.ENGINE_LABELS[rd_common.ANTIGRAVITY], "Antigravity")
        self.assertTrue(callable(rd_common._safe_notify))

    def test_direct_import_rd_context(self):
        """證明不需要 import rd_dispatch 也能正常使用 rd_context 的功能"""
        from tools import rd_context
        self.assertTrue(callable(rd_context.ticket_title_from_task))
        self.assertTrue(callable(rd_context.dependency_conflict))
        self.assertTrue(callable(rd_context.blocked_result))
        self.assertTrue(callable(rd_context.extract_ticket_ref))
        self.assertTrue(callable(rd_context.ticket_relation_context))
        self.assertTrue(callable(rd_context.with_ticket_context))
        self.assertTrue(callable(rd_context._clip_plan))
        self.assertEqual(rd_context.ticket_title_from_task("# 任務標題"), "任務標題")
        self.assertEqual(rd_context.extract_ticket_ref("請實作 #113 需求"), 113)

    def test_direct_import_rd_report(self):
        """證明不需要 import rd_dispatch 也能正常使用 rd_report 的功能"""
        from tools import rd_report
        self.assertTrue(callable(rd_report.compact_dev_result))
        self.assertTrue(callable(rd_report.format_dev_report))
        self.assertTrue(callable(rd_report.format_plan_report))
        self.assertTrue(callable(rd_report.format_cancel_report))
        self.assertTrue(callable(rd_report.commit_note))
        self.assertEqual(rd_report.MAX_MODEL_RESULT_CHARS, 1500)


class TestRdDispatchReExportsAndBudget(unittest.TestCase):
    """驗證 rd_dispatch.py 的重新匯出與行數限制"""

    def test_rd_dispatch_re_exports(self):
        """rd_dispatch 應重新匯出搬到 rd_common, rd_context, rd_report 的名稱以維持向後相容"""
        from tools import rd_common, rd_context, rd_dispatch, rd_report

        # 來自 rd_common
        self.assertIs(rd_dispatch.CLAUDE, rd_common.CLAUDE)
        self.assertIs(rd_dispatch.ANTIGRAVITY, rd_common.ANTIGRAVITY)
        self.assertIs(rd_dispatch.ENGINE_LABELS, rd_common.ENGINE_LABELS)
        self.assertIs(rd_dispatch._safe_notify, rd_common._safe_notify)

        # 來自 rd_context
        self.assertIs(rd_dispatch.ticket_title_from_task, rd_context.ticket_title_from_task)
        self.assertIs(rd_dispatch.dependency_conflict, rd_context.dependency_conflict)
        self.assertIs(rd_dispatch.blocked_result, rd_context.blocked_result)
        self.assertIs(rd_dispatch.extract_ticket_ref, rd_context.extract_ticket_ref)
        self.assertIs(rd_dispatch.ticket_relation_context, rd_context.ticket_relation_context)
        self.assertIs(rd_dispatch.with_ticket_context, rd_context.with_ticket_context)
        self.assertIs(rd_dispatch._clip_plan, rd_context._clip_plan)
        self.assertIs(rd_dispatch.TICKET_CONTEXT_CHARS, rd_context.TICKET_CONTEXT_CHARS)
        self.assertIs(rd_dispatch.PLAN_CONTEXT_CHARS, rd_context.PLAN_CONTEXT_CHARS)
        self.assertIs(rd_dispatch.PLAN_COMMENT_PREFIX, rd_context.PLAN_COMMENT_PREFIX)
        self.assertIs(rd_dispatch.PLAN_COMMENT_CHARS, rd_context.PLAN_COMMENT_CHARS)

        # 來自 rd_report
        self.assertIs(rd_dispatch.compact_dev_result, rd_report.compact_dev_result)
        self.assertIs(rd_dispatch.format_dev_report, rd_report.format_dev_report)
        self.assertIs(rd_dispatch.format_plan_report, rd_report.format_plan_report)
        self.assertIs(rd_dispatch.format_cancel_report, rd_report.format_cancel_report)
        self.assertIs(rd_dispatch.commit_note, rd_report.commit_note)
        self.assertIs(rd_dispatch.MAX_MODEL_RESULT_CHARS, rd_report.MAX_MODEL_RESULT_CHARS)

    def test_rd_dispatch_line_budget(self):
        """驗證 rd_dispatch.py <= 620 行 (原 520 行預算算錯，拆分後實測約 595 行)"""
        dispatch_path = os.path.join(PROJECT_ROOT, "tools", "rd_dispatch.py")
        with open(dispatch_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        line_count = len(lines)
        self.assertLessEqual(line_count, 620, f"rd_dispatch.py 應 <= 620 行，目前為 {line_count} 行")


if __name__ == "__main__":
    unittest.main()
