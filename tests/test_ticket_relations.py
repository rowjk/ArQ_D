"""
 PM 拆單規則與 sub-issue 支援 (層級 B)。mock GitHub，不連網。

驗收標準：
    - create 可帶 parent：用 GitHub sub-issues API 掛到母單底下
    - create 可帶 blocked_by：用 GitHub issue dependencies 標示；內文「相依」段落一併寫明 (API 失敗時仍看得到)
    - get 回傳 parent、sub_issues、blocked_by、blocking (標出未關閉的)
    - list 每筆帶 parent 與是否還被未完成的單擋住
    - 派工前檢查：blocked_by 裡還有未關閉的單時不派工，告知被誰擋住
    - SYSTEM_INSTRUCTION 有拆單規則；Discord /ticket view 顯示母單、子單進度、被誰擋住
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import rd_dispatch, rd_jobs  # noqa: E402
from tools import ticket_tools as tt  # noqa: E402
from tests.test_ticket_tools import ENV, FakeGitHub, issue, run_with  # noqa: E402

P = "/repos/owner/repo"


def created(number=50, id_=5000):
    return 201, {"number": number, "id": id_, "html_url": f"https://github.com/owner/repo/issues/{number}",
                 "node_id": ""}


class TestCreateWithRelations(unittest.TestCase):
    def _fake(self, sub_status=201, dep_status=201):
        return FakeGitHub({
            ("GET", f"{P}/issues?"): (200, []),  # 相似單比對用不到 (直接呼叫 create_ticket)
            ("GET", f"{P}/issues/35"): (200, issue(35, id=3500)),
            ("GET", f"{P}/issues/31"): (200, issue(31, id=3100)),
            ("GET", f"{P}/issues/32"): (200, issue(32, id=3200)),
            ("POST", f"{P}/issues/35/sub_issues"): (sub_status, {} if sub_status < 300 else {"message": "nope"}),
            ("POST", f"{P}/issues/50/dependencies/blocked_by"): (dep_status, {} if dep_status < 300 else {"message": "nope"}),
            ("POST", f"{P}/issues"): created(),
        })

    def test_parent_and_blockers_linked(self):
        fake = self._fake()
        res = run_with(fake, tt.create_ticket, "子單", "做事", "pm", parent=35, blocked_by=[31, 32])
        self.assertTrue(res["success"], res)
        self.assertEqual(fake.find("POST", f"{P}/issues/35/sub_issues")[0]["body"], {"sub_issue_id": 5000})
        deps = [c["body"] for c in fake.find("POST", f"{P}/issues/50/dependencies/blocked_by")]
        self.assertEqual(deps, [{"issue_id": 3100}, {"issue_id": 3200}])
        body = fake.find("POST", f"{P}/issues")[0]["body"]["body"]  # 第一個 POST 是開單本身
        self.assertIn('"parent": 35', body)
        self.assertRegex(body, r"\*\*相依\*\*\n被擋住（要先完成）：#31、#32")
        self.assertNotIn("link_notes", res)

    def test_missing_parent_fails_before_creating(self):
        fake = self._fake()
        res = run_with(fake, tt.create_ticket, "子單", "做事", "pm", parent=99)
        self.assertFalse(res["success"])
        self.assertIn("#99", res["error"])
        self.assertEqual([c for c in fake.calls if c["method"] == "POST"], [])

    def test_api_failure_keeps_ticket_and_notes(self):
        fake = self._fake(sub_status=422, dep_status=404)
        res = run_with(fake, tt.create_ticket, "子單", "做事", "pm", parent=35, blocked_by=[31])
        self.assertTrue(res["success"])
        notes = "\n".join(res["link_notes"])
        self.assertIn("母單 #35", notes)
        self.assertIn("#31", notes)
        self.assertIn("相依", notes)

    def test_dependency_note_replaces_none(self):
        body = tt._with_blockers(tt.format_ticket_body("故事"), [31])
        self.assertIn("**相依**\n被擋住（要先完成）：#31", body)
        self.assertNotIn("**相依**\n被擋住（要先完成）：#31\n無", body)

    def test_gemini_tool_passes_parent_and_blockers(self):
        with patch.object(tt, "find_similar", return_value={"success": True, "candidates": []}), \
                patch.object(tt, "create_ticket", return_value={"success": True}) as create:
            tt.ticket("create", title="子單", body="做事", parent=35, blocked_by="31, #32")
        self.assertEqual(create.call_args.kwargs["parent"], 35)
        self.assertEqual(create.call_args.kwargs["blocked_by"], [31, 32])


def related(number, state="open", title=None):
    return issue(number, state=state, title=title or f"票 {number}")


class TestGetRelations(unittest.TestCase):
    def test_get_returns_parent_children_and_dependencies(self):
        main = issue(32, parent_issue_url=f"https://api.github.com{P}/issues/35",
                     sub_issues_summary={"total": 2, "completed": 1, "percent_completed": 50},
                     issue_dependencies_summary={"blocked_by": 1, "total_blocked_by": 2,
                                                 "blocking": 1, "total_blocking": 1})
        fake = FakeGitHub({
            ("GET", f"{P}/issues/32/comments"): (200, []),
            ("GET", f"{P}/issues/32/parent"): (200, related(35, title="Epic")),
            ("GET", f"{P}/issues/32/sub_issues"): (200, [related(40, "closed"), related(41)]),
            ("GET", f"{P}/issues/32/dependencies/blocked_by"): (200, [related(31), related(29, "closed")]),
            ("GET", f"{P}/issues/32/dependencies/blocking"): (200, [related(34)]),
            ("GET", f"{P}/issues/32"): (200, main),
        })
        t = run_with(fake, tt.get_ticket, 32)["ticket"]
        self.assertEqual(t["parent"], {"number": 35, "title": "Epic"})
        self.assertEqual([s["number"] for s in t["sub_issues"]], [40, 41])
        self.assertEqual(t["sub_progress"], "1/2")
        self.assertEqual([(b["number"], b["state"]) for b in t["blocked_by"]], [(31, "open"), (29, "closed")])
        self.assertEqual(t["open_blockers"], [31])
        self.assertEqual([b["number"] for b in t["blocking"]], [34])

    def test_large_ticket_without_children_gets_split_rules(self):
        fake = FakeGitHub({("GET", f"{P}/issues/9/comments"): (200, []),
                           ("GET", f"{P}/issues/9"): (200, issue(9, labels=("by:user", "status:todo", "size:l")))})
        rules = run_with(fake, tt.get_ticket, 9)["ticket"]["split_rules"]
        for phrase in ("size:s", "同一個檔案", "介面約定", "整合與全部測試", "確認", "parent", "blocked_by",
                       "屬於哪張母單", "分工"):
            self.assertIn(phrase, rules)

    def test_small_ticket_has_no_split_rules(self):
        fake = FakeGitHub({("GET", f"{P}/issues/7/comments"): (200, []), ("GET", f"{P}/issues/7"): (200, issue(7))})
        self.assertNotIn("split_rules", run_with(fake, tt.get_ticket, 7)["ticket"])

    def test_no_relations_no_extra_calls(self):
        fake = FakeGitHub({("GET", f"{P}/issues/7/comments"): (200, []), ("GET", f"{P}/issues/7"): (200, issue(7))})
        t = run_with(fake, tt.get_ticket, 7)["ticket"]
        self.assertEqual(len(fake.calls), 2)
        self.assertIsNone(t["parent"])
        self.assertEqual(t["open_blockers"], [])

    def test_list_marks_parent_and_blocked(self):
        items = [issue(32, parent_issue_url=f"https://api.github.com{P}/issues/35",
                       issue_dependencies_summary={"blocked_by": 1, "total_blocked_by": 1}),
                 issue(30)]
        fake = FakeGitHub({("GET", f"{P}/issues"): (200, items)})
        tickets = {t["number"]: t for t in run_with(fake, tt.ticket, "list")["tickets"]}
        self.assertEqual(tickets[32]["parent"], 35)
        self.assertTrue(tickets[32]["blocked"])
        self.assertNotIn("parent", tickets[30])  # 沒有值就不帶，省 Token
        self.assertNotIn("blocked", tickets[30])


class TestDispatchGate(unittest.TestCase):
    def setUp(self):
        rd_jobs._reset_for_tests()

    def _run(self, ticket_res, **kwargs):
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", return_value=ticket_res), \
                patch.object(rd_dispatch.ticket_tools, "update_ticket", return_value={"success": True}) as update, \
                patch.object(rd_dispatch, "_dispatch_with_failover",
                             return_value={"success": True, "stdout": "完成"}) as dispatch, \
                patch.object(rd_dispatch, "collect_acceptance", return_value={}), \
                patch.object(rd_dispatch, "_prepare_worktree",  # 假 worktree
                             return_value=({"path": "D:\\wt\\ticket-32", "branch": "ticket-32-x"}, None)), \
                patch("builtins.print"):
            res = rd_dispatch.run_dev_task("做 #32", rd_dispatch.CLAUDE, scope="core", opened_by="pm",
                                           ticket=32, **kwargs)
        return res, dispatch, update

    def test_blocked_ticket_not_dispatched(self):
        res, dispatch, update = self._run({"success": True, "ticket": {"number": 32, "open_blockers": [31],
                                                                        "body": "x"}})
        self.assertFalse(res["success"])
        self.assertEqual(res["blocked_by"], [31])
        self.assertIn("#31", res["error"])
        dispatch.assert_not_called()
        update.assert_not_called()

    def test_unblocked_ticket_dispatched(self):
        res, dispatch, _ = self._run({"success": True, "ticket": {"number": 32, "open_blockers": [], "body": "x"}})
        self.assertTrue(res["success"])
        dispatch.assert_called_once()

    def test_plan_mode_not_gated(self):
        res, dispatch, _ = self._run({"success": True, "ticket": {"number": 32, "open_blockers": [31],
                                                                   "body": "x"}}, plan=True)
        dispatch.assert_called_once()

    def test_compact_result_tells_pm_who_blocks(self):
        compact = rd_dispatch.compact_dev_result({"success": False, "blocked_by": [31], "ticket": 32,
                                                  "error": "Ticket #32 被 #31 擋住"})
        self.assertEqual(compact["blocked_by"], [31])


def _t(number, title, state="open", status="todo", **extra):
    base = {"number": number, "title": title, "state": state, "status": status, "body": f"{title} 的內文",
            "parent": None, "sub_issues": [], "blocked_by": [], "blocking": [], "open_blockers": []}
    base.update(extra)
    return base


class TestRelationContextForRD(unittest.TestCase):
    """拆單後 RD 要清楚知道關聯：母單與介面約定、兄弟子單、被誰擋住、擋住誰 (使用者追加要求)"""

    def _context(self, tickets, number):
        def get(n):
            return {"success": True, "ticket": tickets[n]} if n in tickets else {"success": False, "error": "404"}
        with patch.object(rd_dispatch.ticket_tools, "get_ticket", side_effect=get):
            return rd_dispatch.with_ticket_context("做 #%d" % number, number, None)

    def test_child_ticket_context_has_parent_contract_siblings_and_dependencies(self):
        epic = _t(35, "Epic 平行開發", body="**介面約定**\ncreate_worktree(n) -> str",
                  sub_issues=[{"number": 31, "title": "測試禁止連外", "state": "closed", "status": "done"},
                              {"number": 32, "title": "Worktree", "state": "open", "status": "todo"},
                              {"number": 34, "title": "平行派工", "state": "open", "status": "todo"}])
        child = _t(32, "Worktree", parent={"number": 35, "title": "Epic 平行開發"},
                   blocked_by=[{"number": 31, "title": "測試禁止連外", "state": "closed", "status": "done"}],
                   blocking=[{"number": 34, "title": "平行派工", "state": "open", "status": "todo"}])
        text = self._context({35: epic, 32: child}, 32)
        self.assertIn("母單 #35 Epic 平行開發", text)
        self.assertIn("create_worktree(n) -> str", text)        # 母單的介面約定
        self.assertIn("#34 平行派工", text)                      # 兄弟子單 / 擋住誰
        self.assertIn("#31 測試禁止連外（已完成）", text)          # 被誰擋住與狀態
        self.assertNotIn("#32 Worktree（", text)                 # 兄弟清單不列自己
        self.assertIn("只做本單範圍", text)

    def test_parent_ticket_context_lists_children(self):
        epic = _t(35, "Epic", sub_issues=[{"number": 30, "title": "A", "state": "open", "status": "review"}])
        text = self._context({35: epic}, 35)
        self.assertIn("子單", text)
        self.assertIn("#30 A", text)

    def test_unrelated_ticket_has_no_relation_section(self):
        text = self._context({7: _t(7, "單獨的單")}, 7)
        self.assertNotIn("關聯", text)

    def test_parent_unreadable_still_shows_reference(self):
        child = _t(32, "Worktree", parent={"number": 35, "title": "Epic"})
        text = self._context({32: child}, 32)
        self.assertIn("母單 #35 Epic", text)


class TestInstructionAndView(unittest.TestCase):
    def test_system_instruction_has_split_rules(self):
        import agent
        text = agent.SYSTEM_INSTRUCTION
        for phrase in ("拆單", "size:l", "介面約定", "整合與全部測試", "parent", "blocked_by", "確認後"):
            self.assertIn(phrase, text)

    def test_ticket_view_shows_relations(self):
        import bot
        from tests.test_ticket_command import _embed_text
        res = {"success": True, "ticket": {
            "number": 32, "title": "Worktree", "state": "open", "status": "todo", "body": "x", "comments": [],
            "parent": {"number": 35, "title": "Epic"}, "sub_issues": [], "sub_progress": None,
            "blocked_by": [{"number": 31, "title": "測試禁止連外", "state": "open"}], "open_blockers": [31],
            "blocking": [{"number": 34, "title": "平行派工", "state": "open"}]}}
        text = _embed_text(bot.build_ticket_view_embed(res))
        self.assertIn("#35", text)
        self.assertIn("#31", text)
        self.assertIn("#34", text)

    def test_ticket_view_shows_child_progress(self):
        import bot
        from tests.test_ticket_command import _embed_text
        res = {"success": True, "ticket": {
            "number": 35, "title": "Epic", "state": "open", "status": "todo", "body": "x", "comments": [],
            "parent": None, "sub_issues": [{"number": 30, "title": "A", "state": "closed", "status": "done"},
                                           {"number": 31, "title": "B", "state": "open", "status": "review"}],
            "sub_progress": "1/2", "blocked_by": [], "open_blockers": [], "blocking": []}}
        text = _embed_text(bot.build_ticket_view_embed(res))
        self.assertIn("1/2", text)
        self.assertIn("#31", text)


if __name__ == "__main__":
    unittest.main()
