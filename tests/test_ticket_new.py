"""
Discord /ticket new 單元測試：記想法不經 Gemini，不連 Discord / GitHub

驗收標準：
    - /ticket new <標題> [內容] [type] [priority]：直接開單，開單者 by:user，內文套用固定格式
    - 只有白名單使用者可用
    - 回覆單號與連結
    - /help、README 補上說明
    - 參數轉換與回覆內容有測試
"""

import asyncio
import inspect
import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外


def _embed_text(embed):
    parts = [embed.title or "", embed.description or "", (embed.footer.text if embed.footer else "") or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    return "\n".join(parts)


def _choice(value):
    import discord
    return discord.app_commands.Choice(name=value, value=value)


class TestBuildArgs(unittest.TestCase):
    def test_defaults(self):
        import bot
        args = bot.build_ticket_new_args("買牛奶")
        self.assertEqual(args, {"title": "買牛奶", "body": "", "opened_by": "user",
                                "type_": "feature", "priority": "p2"})

    def test_opener_is_always_user(self):
        import bot
        self.assertEqual(bot.build_ticket_new_args("x", "y", "bug", "p0")["opened_by"], "user")

    def test_choices_converted_to_values(self):
        import bot
        args = bot.build_ticket_new_args("修登入", "按鈕無反應", _choice("bug"), _choice("p0"))
        self.assertEqual((args["type_"], args["priority"], args["body"]), ("bug", "p0", "按鈕無反應"))

    def test_plain_strings_accepted(self):
        import bot
        args = bot.build_ticket_new_args("整理文件", None, "chore", "p3")
        self.assertEqual((args["type_"], args["priority"], args["body"]), ("chore", "p3", ""))

    def test_title_and_content_trimmed(self):
        import bot
        args = bot.build_ticket_new_args("  想法  ", "  內容\n", None, None)
        self.assertEqual((args["title"], args["body"]), ("想法", "內容"))


class TestNewEmbed(unittest.TestCase):
    def test_success_shows_number_and_link(self):
        import bot
        res = {"success": True, "number": 70, "url": "https://github.com/owner/repo/issues/70"}
        embed = bot.build_ticket_new_embed(res, bot.build_ticket_new_args("買牛奶", "", "chore", "p3"))
        text = _embed_text(embed)
        self.assertIn("#70", embed.title)
        self.assertEqual(embed.url, "https://github.com/owner/repo/issues/70")
        self.assertIn("https://github.com/owner/repo/issues/70", text)
        self.assertIn("買牛奶", text)
        self.assertIn("by:user", text)
        self.assertIn("chore", text)
        self.assertIn("p3", text)

    def test_error(self):
        import bot
        embed = bot.build_ticket_new_embed({"success": False, "error": "Ticket 功能尚未設定"},
                                           bot.build_ticket_new_args("x"))
        self.assertIn("尚未設定", _embed_text(embed))
        self.assertIsNone(embed.url)

    def test_long_title_within_discord_limits(self):
        import bot
        res = {"success": True, "number": 1, "url": "https://github.com/o/r/issues/1"}
        embed = bot.build_ticket_new_embed(res, bot.build_ticket_new_args("長" * 500))
        self.assertLessEqual(len(embed.title), 256)
        self.assertLessEqual(len(embed), 6000)


class TestCommand(unittest.TestCase):
    def _interaction(self):
        it = mock.MagicMock()
        it.user.id = 123
        it.channel_id = 1
        it.response.defer = mock.AsyncMock()
        it.response.send_message = mock.AsyncMock()
        it.followup.send = mock.AsyncMock()
        return it

    def _run(self, authorized, res=None, **kwargs):
        import bot
        it = self._interaction()
        res = res or {"success": True, "number": 70, "url": "https://github.com/owner/repo/issues/70"}
        order = []
        it.followup.send.side_effect = lambda *a, **k: order.append("reply")
        hint = mock.AsyncMock(side_effect=lambda *a, **k: order.append("hint"))
        with mock.patch.object(bot, "is_authorized", return_value=authorized), \
                mock.patch.object(bot.ticket_tools, "create_ticket", return_value=res) as create, \
                mock.patch.object(bot, "_send_similar_hint", new=hint):
            asyncio.run(bot.cmd_ticket_new.callback(it, **kwargs))
        self.order = order
        return it, create, hint

    def test_registered_in_ticket_group(self):
        import bot
        group = bot.bot.tree.get_command("ticket")
        self.assertIn("new", {c.name for c in group.commands})
        new_cmd = next(c for c in group.commands if c.name == "new")
        self.assertEqual([p.name for p in new_cmd.parameters], ["title", "content", "type", "priority"])
        self.assertTrue(new_cmd.parameters[0].required)
        self.assertFalse(any(p.required for p in new_cmd.parameters[1:]))

    def test_creates_ticket_as_user_and_replies_with_link(self):
        it, create, hint = self._run(True, title="買牛奶", content="低脂", type=_choice("chore"), priority=_choice("p3"))
        create.assert_called_once_with(title="買牛奶", body="低脂", opened_by="user", type_="chore", priority="p3")
        embed = it.followup.send.call_args_list[0].kwargs["embed"]
        self.assertIn("#70", embed.title)
        self.assertEqual(embed.url, "https://github.com/owner/repo/issues/70")
        # 不攔截：先開單並回覆，相似單提示在背景補送 (使用者決定)
        hint.assert_called_once()
        self.assertEqual(hint.call_args.args[2], 70)
        self.assertEqual(self.order[0], "reply")

    def test_no_hint_when_create_failed(self):
        _, _, hint = self._run(True, res={"success": False, "error": "GitHub API 錯誤 500"}, title="想法")
        hint.assert_not_called()

    def test_defaults_when_optional_omitted(self):
        _, create, _ = self._run(True, title="想法")
        create.assert_called_once_with(title="想法", body="", opened_by="user", type_="feature", priority="p2")

    def test_unauthorized_user_cannot_create(self):
        it, create, _ = self._run(False, title="想法")
        create.assert_not_called()
        self.assertTrue(it.response.send_message.call_args.kwargs.get("ephemeral"))

    def test_checks_whitelist_first(self):
        import bot
        src = inspect.getsource(bot.cmd_ticket_new.callback)
        self.assertIn("is_authorized(interaction.user.id)", src)

    def test_body_gets_fixed_format(self):
        """內文經 create_ticket 套用固定格式：只有想法時放進「使用者故事」，其餘段落補待細化"""
        import bot
        sent = {}

        def fake_api(method, path, body=None, query=None):
            sent.update(body or {})
            return {"number": 9, "html_url": "https://github.com/o/r/issues/9"}, None, 201

        with mock.patch("tools.ticket_tools._api", side_effect=fake_api):
            res = bot.ticket_tools.create_ticket(**bot.build_ticket_new_args("想法", "週末去爬山"))
        self.assertTrue(res["success"])
        self.assertIn("by:user", sent["labels"])
        self.assertIn("**使用者故事**\n週末去爬山", sent["body"])
        self.assertIn("**驗收標準**", sent["body"])
        self.assertIn('"opened_by": "user"', sent["body"].split("\n", 1)[0])


class TestNewlineAndSimilarHint(unittest.TestCase):
    """使用者決定：內容支援 \\n 換行；開單後非阻塞地提示可能重複的單"""

    def test_backslash_n_becomes_newline(self):
        import bot
        args = bot.build_ticket_new_args("想法", "第一行\\n第二行\\n- [ ] 驗收")
        self.assertEqual(args["body"], "第一行\n第二行\n- [ ] 驗收")

    def test_hint_excludes_new_ticket_and_marks_closed(self):
        import bot
        candidates = [{"number": 70, "title": "買牛奶", "state": "open", "status": "todo", "url": "u70"},
                      {"number": 10, "title": "心跳訊息改短一點", "state": "closed", "status": "done", "url": "u10"}]
        text = bot.build_similar_hint(candidates, new_number=70)
        self.assertIn("#10", text)
        self.assertNotIn("#70", text)
        self.assertIn("已關閉", text)
        self.assertLessEqual(len(text), 2000)

    def test_no_hint_when_only_itself(self):
        import bot
        self.assertIsNone(bot.build_similar_hint([{"number": 70, "title": "t", "state": "open", "url": ""}], 70))
        self.assertIsNone(bot.build_similar_hint([], 70))

    def _hint(self, similar_result=None, side_effect=None):
        import bot
        it = mock.MagicMock()
        it.followup.send = mock.AsyncMock()
        with mock.patch.object(bot.ticket_tools, "find_similar", return_value=similar_result,
                               side_effect=side_effect), mock.patch("builtins.print"):
            asyncio.run(bot._send_similar_hint(it, {"title": "心跳太長", "body": ""}, 70))
        return it

    def test_send_similar_hint_posts_candidates(self):
        it = self._hint({"success": True, "candidates": [
            {"number": 10, "title": "心跳訊息改短一點", "state": "closed", "status": "done", "url": "u10"}]})
        it.followup.send.assert_awaited_once()
        self.assertIn("#10", it.followup.send.call_args.args[0])

    def test_send_similar_hint_silent_on_failure(self):
        self.assertFalse(self._hint({"success": False, "error": "x"}).followup.send.called)
        self.assertFalse(self._hint(side_effect=OSError("network")).followup.send.called)


class TestDocs(unittest.TestCase):
    def test_help_mentions_ticket_new(self):
        import bot
        self.assertIn("/ticket new", inspect.getsource(bot.cmd_help.callback))

    def test_readme_mentions_ticket_new(self):
        with open(os.path.join(PROJECT_ROOT, "docs", "USAGE.md"), encoding="utf-8") as f:
            self.assertIn("`/ticket new", f.read())


if __name__ == "__main__":
    unittest.main()
