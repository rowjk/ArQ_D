"""
Ticket 單元測試：bot.py 拆出 Discord UI 元件（discord_ui/）

驗收標準：
- 新增套件 discord_ui/：__init__.py、text.py、ticket_embeds.py、jobs.py
- 只搬「不依賴 bot／agent／restart_mgr 全域」的純 UI 函式與類別；內容逐字搬移，行為不變
- bot.py 以 import 重新匯出搬走的名稱，既有 bot.build_ticket_list_embed 等引用照常可用
- bot.py <= 1,000 行
- discord_ui 不得 import bot、agent
- 全部測試通過；測試只允許改 patch 目標
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外


class TestDiscordUIIsolation(unittest.TestCase):
    """驗證 discord_ui 模組的獨立性（不依賴 bot / agent）"""

    def test_no_forbidden_imports_in_discord_ui(self):
        """discord_ui 套件下所有 .py 檔皆不得 import bot 或 agent"""
        discord_ui_dir = os.path.join(PROJECT_ROOT, "discord_ui")
        self.assertTrue(os.path.isdir(discord_ui_dir), "discord_ui 目錄應存在")

        py_files = [
            f for f in os.listdir(discord_ui_dir)
            if f.endswith(".py")
        ]
        self.assertIn("__init__.py", py_files)
        self.assertIn("text.py", py_files)
        self.assertIn("ticket_embeds.py", py_files)
        self.assertIn("jobs.py", py_files)

        for fname in py_files:
            fpath = os.path.join(discord_ui_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                self.assertNotIn("import bot", stripped, f"{fname} 不得 import bot")
                self.assertNotIn("from bot", stripped, f"{fname} 不得 from bot")
                self.assertNotIn("import agent", stripped, f"{fname} 不得 import agent")
                self.assertNotIn("from agent", stripped, f"{fname} 不得 from agent")

    def test_direct_import_without_bot(self):
        """證明不需要 import bot 也能正常使用 discord_ui 的 UI 函式"""
        from discord_ui.text import split_message, clip_text, _clip_text
        from discord_ui.ticket_embeds import (
            build_ticket_list_embed,
            build_ticket_view_embed,
            build_ticket_new_args,
            build_ticket_new_embed,
            build_similar_hint,
        )
        from discord_ui.jobs import (
            build_jobs_embed,
            build_acceptance_embed,
            build_dev_done_embed,
            scope_hint,
            _format_elapsed,
            job_announcement,
        )

        # 測試 text 模組
        chunks = split_message("hello world", limit=100)
        self.assertEqual(chunks, ["hello world"])
        self.assertEqual(clip_text("1234567890", 5), "1234…")
        self.assertEqual(_clip_text("12345", 10), "12345")

        # 測試 ticket_embeds 模組
        list_res = {
            "success": True,
            "tickets": [
                {
                    "number": 1,
                    "title": "測試",
                    "state": "open",
                    "status": "todo",
                    "priority": "p1",
                    "opened_by": "pm",
                }
            ],
            "total": 1,
        }
        embed_list = build_ticket_list_embed(list_res)
        self.assertIn("Tickets", embed_list.title)

        view_res = {
            "success": True,
            "ticket": {
                "number": 1,
                "title": "測試",
                "state": "open",
                "status": "in-progress",
                "priority": "p1",
                "opened_by": "user",
                "body": "內容說明",
            },
        }
        embed_view = build_ticket_view_embed(view_res)
        self.assertIn("#1", embed_view.title)

        args = build_ticket_new_args("新功能", "描述", "feature", "p2")
        self.assertEqual(args["title"], "新功能")
        self.assertEqual(args["opened_by"], "user")

        new_res = {"success": True, "number": 2, "url": "https://example.com/2"}
        embed_new = build_ticket_new_embed(new_res, args)
        self.assertIn("#2", embed_new.title)

        hint = build_similar_hint([{"number": 1, "title": "相似單"}], new_number=2)
        self.assertIsNotNone(hint)
        self.assertIn("可能重複", hint)

        # 測試 jobs 模組
        jobs_embed = build_jobs_embed([])
        self.assertIn("目前沒有執行中的派工", jobs_embed.description)

        acceptance_embed = build_acceptance_embed({
            "tests_passed": True,
            "diff_stat": " 1 file changed",
            "status": "",
            "tests_summary": "OK",
        })
        self.assertIn("驗收卡", acceptance_embed.title)
        self.assertEqual(_format_elapsed(65), "1 分 5 秒")

        hint = scope_hint("修復 #101 bug", "task")
        self.assertIn("scope: task", hint)

        dev_embed = build_dev_done_embed({"success": True, "stdout": "ok"}, "需求", "Claude Code")
        self.assertIn("開發任務完成", dev_embed.title)


class TestBotReExportsAndLineCount(unittest.TestCase):
    """驗證 bot.py 的重新匯出與行數限制"""

    def test_bot_re_exports(self):
        """bot.py 應重新匯出搬到 discord_ui 的所有名稱，維持向後相容"""
        import bot
        import discord_ui.text as ui_text
        import discord_ui.ticket_embeds as ui_ticket
        import discord_ui.jobs as ui_jobs

        self.assertIs(bot.split_message, ui_text.split_message)
        self.assertIs(bot._clip_text, ui_text._clip_text)

        self.assertIs(bot.build_ticket_list_embed, ui_ticket.build_ticket_list_embed)
        self.assertIs(bot.build_ticket_view_embed, ui_ticket.build_ticket_view_embed)
        self.assertIs(bot.build_ticket_new_args, ui_ticket.build_ticket_new_args)
        self.assertIs(bot.build_ticket_new_embed, ui_ticket.build_ticket_new_embed)
        self.assertIs(bot.build_similar_hint, ui_ticket.build_similar_hint)
        self.assertIs(bot._ticket_error_embed, ui_ticket._ticket_error_embed)
        self.assertIs(bot._choice_value, ui_ticket._choice_value)

        self.assertIs(bot.build_jobs_embed, ui_jobs.build_jobs_embed)
        self.assertIs(bot.make_stop_view, ui_jobs.make_stop_view)
        self.assertIs(bot.attach_stop_button, ui_jobs.attach_stop_button)
        self.assertIs(bot.job_announcement, ui_jobs.job_announcement)
        self.assertIs(bot.StopJobsView, ui_jobs.StopJobsView)
        self.assertIs(bot.build_acceptance_embed, ui_jobs.build_acceptance_embed)
        self.assertIs(bot.build_dev_done_embed, ui_jobs.build_dev_done_embed)
        self.assertIs(bot.scope_hint, ui_jobs.scope_hint)
        self.assertIs(bot._format_elapsed, ui_jobs._format_elapsed)

    def test_bot_py_line_budget(self):
        """驗證 bot.py <= 1,000 行"""
        bot_path = os.path.join(PROJECT_ROOT, "bot.py")
        with open(bot_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        line_count = len(lines)
        self.assertLessEqual(line_count, 1000, f"bot.py 應 <= 1,000 行，目前為 {line_count} 行")


if __name__ == "__main__":
    unittest.main()
