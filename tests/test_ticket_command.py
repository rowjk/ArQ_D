"""
Discord /ticket 指令單元測試：不連 Discord / GitHub，只測 embed 組裝與指令註冊

驗收標準：
    - /ticket list [by] [state] [priority]：embed 列出，依優先順序排序
    - /ticket view <N>：內文摘要、最近留言；label 與 metadata 的開單者不一致時顯示警告
    - 只有白名單使用者可用
    - embed 組裝邏輯抽成可測的函式
"""

import inspect
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外


def _t(number, priority="p1", status="todo", opened_by="pm", title=None):
    return {"number": number, "title": title or f"票 {number}", "state": "open", "status": status,
            "priority": priority, "type": "feature", "rd": None, "opened_by": opened_by,
            "opened_by_label": opened_by, "opener_mismatch": False,
            "url": f"https://github.com/owner/repo/issues/{number}", "updated_at": ""}


def _embed_text(embed):
    parts = [embed.title or "", embed.description or "", (embed.footer.text if embed.footer else "") or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    return "\n".join(parts)


class TestListEmbed(unittest.TestCase):
    def test_lists_in_given_order_with_links(self):
        import bot
        res = {"success": True, "total": 2, "tickets": [_t(4, "p0", "todo"), _t(7, "p2", "todo", "rd-claude")]}
        embed = bot.build_ticket_list_embed(res, state="open")
        text = _embed_text(embed)
        self.assertLess(text.index("#4"), text.index("#7"))  # 同一組內保持 list_tickets 的優先順序
        self.assertIn("https://github.com/owner/repo/issues/4", text)
        self.assertIn("p0", text)
        self.assertIn("rd-claude", text)
        self.assertIn("2", embed.title)

    def test_grouped_by_status_in_workflow_order(self):
        """：依狀態分組，順序 進行中 → review → 卡住 → 待辦 → 完成；沒有狀態的另列；空組不顯示"""
        import bot
        res = {"success": True, "total": 6, "tickets": [
            _t(1, "p0", "todo"), _t(2, "p1", "review"), _t(3, "p1", "in-progress"),
            _t(4, "p2", "done"), _t(5, "p3", "todo"), _t(6, "p2", None)]}
        embed = bot.build_ticket_list_embed(res, state="all")
        names = [f.name for f in embed.fields]
        self.assertEqual(len(names), 5)                     # 沒有 blocked → 不顯示該組
        for name, word in zip(names, ("進行中", "review", "待辦", "完成", "未標狀態")):
            self.assertIn(word, name)
        todo = next(f for f in embed.fields if "待辦" in f.name)
        self.assertIn("(2)", todo.name)                     # 組名附數量
        self.assertLess(todo.value.index("#1"), todo.value.index("#5"))
        self.assertIn("進行中 1", embed.description)        # 開頭的數量摘要
        self.assertIn("待辦 2", embed.description)

    def test_group_over_field_limit_continues_instead_of_hiding(self):
        import bot
        res = {"success": True, "total": 12, "tickets": [_t(i, title="標題" * 30) for i in range(1, 13)]}
        embed = bot.build_ticket_list_embed(res, state="open")
        text = _embed_text(embed)
        for i in range(1, 13):
            self.assertIn(f"#{i}]", text)                    # 12 張全部顯示
        self.assertTrue(any("（續）" in f.name for f in embed.fields))
        self.assertNotIn("還有", text)
        for f in embed.fields:
            self.assertLessEqual(len(f.value), 1024)

    def test_huge_list_truncated_within_embed_limit(self):
        import bot
        res = {"success": True, "total": 80, "tickets": [_t(i, title="標題" * 30) for i in range(1, 81)]}
        embed = bot.build_ticket_list_embed(res, state="open")
        for f in embed.fields:
            self.assertLessEqual(len(f.value), 1024)
        self.assertIn("還有", _embed_text(embed))
        self.assertLessEqual(len(embed), 6000)

    def test_shows_filters_and_truncation_footer(self):
        import bot
        res = {"success": True, "total": 35, "tickets": [_t(i) for i in range(1, 21)]}
        embed = bot.build_ticket_list_embed(res, state="all", by="pm", priority="p1")
        text = _embed_text(embed)
        self.assertIn("by:pm", text)
        self.assertIn("p1", text)
        self.assertIn("20 / 35", embed.footer.text)

    def test_empty_list(self):
        import bot
        embed = bot.build_ticket_list_embed({"success": True, "total": 0, "tickets": []}, state="open")
        self.assertIn("沒有", embed.description)
        self.assertEqual(embed.fields, [])

    def test_status_filter_shown_in_footer(self):
        import bot
        res = {"success": True, "total": 1, "tickets": [_t(3, status="blocked")]}
        embed = bot.build_ticket_list_embed(res, state="open", status="blocked")
        self.assertIn("status:blocked", embed.footer.text)

    def test_error(self):
        import bot
        embed = bot.build_ticket_list_embed({"success": False, "error": "Ticket 功能尚未設定"}, state="open")
        self.assertIn("尚未設定", _embed_text(embed))

    def test_respects_discord_limits(self):
        import bot
        long_title = "很長的標題" * 60
        res = {"success": True, "total": 20, "tickets": [_t(i, title=long_title) for i in range(1, 21)]}
        embed = bot.build_ticket_list_embed(res, state="open")
        self.assertLessEqual(len(embed.title), 256)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertLessEqual(len(embed), 6000)


class TestViewEmbed(unittest.TestCase):
    def _ticket(self, **extra):
        t = _t(5, "p2", "in-progress")
        t.update({"metadata": {"opened_by": "pm"}, "body": "**使用者故事**\n想在手機上看 backlog",
                  "comments": [{"author": "owner", "created_at": "2026-10-19T00:00:00Z", "body": f"留言 {i}"} for i in range(5)]})
        t.update(extra)
        return {"success": True, "ticket": t}

    def test_view_contains_summary_body_and_recent_comments(self):
        import bot
        embed = bot.build_ticket_view_embed(self._ticket())
        text = _embed_text(embed)
        self.assertIn("#5", embed.title)
        self.assertEqual(embed.url, "https://github.com/owner/repo/issues/5")
        self.assertIn("想在手機上看 backlog", text)
        self.assertIn("in-progress", text)
        self.assertIn("p2", text)
        self.assertIn("留言 4", text)       # 最新的留言
        self.assertNotIn("留言 0", text)    # 只顯示最近幾則
        self.assertNotIn("不一致", text)

    def test_mismatch_warning(self):
        import bot
        res = self._ticket(opened_by="rd-agy", opened_by_label="pm", opener_mismatch=True)
        text = _embed_text(bot.build_ticket_view_embed(res))
        self.assertIn("不一致", text)
        self.assertIn("rd-agy", text)

    def test_long_body_and_comments_within_limits(self):
        import bot
        res = self._ticket(body="長" * 5000, comments=[{"author": "a", "created_at": "", "body": "留" * 2000}] * 3,
                           title="標" * 400)
        embed = bot.build_ticket_view_embed(res)
        self.assertLessEqual(len(embed.title), 256)
        self.assertLessEqual(len(embed.description), 4096)
        for f in embed.fields:
            self.assertLessEqual(len(f.value), 1024)
        self.assertLessEqual(len(embed), 6000)

    def test_error(self):
        import bot
        embed = bot.build_ticket_view_embed({"success": False, "error": "GitHub API 錯誤 404：Not Found"})
        self.assertIn("404", _embed_text(embed))


class TestCommandRegistration(unittest.TestCase):
    def test_ticket_group_has_list_and_view(self):
        import bot
        group = bot.bot.tree.get_command("ticket")
        self.assertIsNotNone(group)
        names = {c.name for c in group.commands}
        self.assertEqual(names, {"list", "view", "new"})  # new：
        list_cmd = next(c for c in group.commands if c.name == "list")
        self.assertIn("status", [p.name for p in list_cmd.parameters])  # 可依狀態篩選

    def test_commands_check_whitelist(self):
        import bot
        for name in ("cmd_ticket_list", "cmd_ticket_view"):
            src = inspect.getsource(getattr(bot, name).callback)
            self.assertIn("is_authorized(interaction.user.id)", src)

    def test_help_and_readme_mention_ticket(self):
        import bot
        self.assertIn("/ticket", inspect.getsource(bot.cmd_help.callback))
        with open(os.path.join(PROJECT_ROOT, "docs", "USAGE.md"), encoding="utf-8") as f:
            self.assertIn("`/ticket list", f.read())


if __name__ == "__main__":
    unittest.main()
