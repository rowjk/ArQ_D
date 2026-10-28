"""
 ticket 可搜尋、可改內文，並由程式攔截重複開單 (不連真實 GitHub，全部走 FakeGitHub)

    - search：本地 bigram Jaccard 比對、排除 PR、預設 state=all、空 query 不連網
    - update body：整份取代、第一行 metadata 逐字保留、剝除新內文的 metadata、自動留言差異、已關閉的單拒絕
    - create：未帶 force_new 先比對 state=all 的單，有相似單就不開單；force_new 需確認卡；rd_dispatch 不受影響
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tests.test_ticket_tools import ENV, FAKE_TOKEN, FakeGitHub, issue, run_with  # noqa: E402
from tools import ticket_tools as tt  # noqa: E402

ISSUES = "/repos/owner/repo/issues"


def story(text, items=("做 A",), scope="無", dep="無"):
    checks = "\n".join(f"- [ ] {i}" for i in items)
    return f"**使用者故事**\n{text}\n\n**驗收標準**\n{checks}\n\n**範圍外**\n{scope}\n\n**相依**\n{dep}"


def pr_item(number=99, title="心跳訊息精簡"):
    return issue(number, title=title, pull_request={"url": "x"})


class TestBigram(unittest.TestCase):
    def test_identical_is_one_and_disjoint_is_zero(self):
        self.assertEqual(tt._jaccard("心跳訊息精簡", "心跳訊息精簡"), 1.0)
        self.assertEqual(tt._jaccard("心跳訊息", "報價排行"), 0.0)

    def test_ignores_case_whitespace_punctuation(self):
        self.assertEqual(tt._jaccard("Heart Beat！", "heartbeat"), 1.0)

    def test_empty_is_zero(self):
        self.assertEqual(tt._jaccard("", "abc"), 0.0)
        self.assertEqual(tt._jaccard("", ""), 0.0)

    def test_single_char_still_comparable(self):
        self.assertEqual(tt._jaccard("心", "心"), 1.0)


class TestSearch(unittest.TestCase):
    def _fake(self, items, status=200):
        return FakeGitHub({("GET", ISSUES): (status, items)})

    def test_finds_by_title_keyword_including_closed(self):
        items = [issue(1, title="心跳訊息精簡", state="closed"), issue(2, title="永豐報價排行"), pr_item()]
        fake = self._fake(items)
        res = run_with(fake, tt.search_tickets, "心跳")
        self.assertTrue(res["success"])
        self.assertEqual([t["number"] for t in res["tickets"]], [1])
        self.assertEqual(fake.calls[0]["url"].count("state=all"), 1)

    def test_finds_by_body_and_ranks_title_first(self):
        items = [issue(1, title="雜項", body="內容提到心跳設定"), issue(2, title="心跳訊息精簡")]
        res = run_with(self._fake(items), tt.search_tickets, "心跳")
        self.assertEqual([t["number"] for t in res["tickets"]], [2, 1])

    def test_excludes_pull_requests(self):
        res = run_with(self._fake([pr_item(5, "心跳訊息精簡")]), tt.search_tickets, "心跳訊息精簡")
        self.assertEqual(res["tickets"], [])

    def test_compact_fields_and_cap(self):
        items = [issue(n, title="心跳訊息精簡") for n in range(1, 40)]
        res = run_with(self._fake(items), tt.search_tickets, "心跳訊息精簡")
        self.assertEqual(len(res["tickets"]), tt.MAX_LIST)
        self.assertEqual(set(res["tickets"][0]), {"number", "title", "state", "status", "url", "score"})

    def test_state_filter_passed_and_validated(self):
        fake = self._fake([])
        run_with(fake, tt.search_tickets, "心跳", state="closed")
        self.assertIn("state=closed", fake.calls[0]["url"])
        res = run_with(self._fake([]), tt.search_tickets, "心跳", state="weird")
        self.assertFalse(res["success"])

    def test_empty_query_fails_without_network(self):
        fake = self._fake([])
        for q in ("", "   ", None):
            res = run_with(fake, tt.search_tickets, q)
            self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])

    def test_not_configured_fails_without_network(self):
        with patch.dict(os.environ, {"TICKETS_REPO": "", "TICKETS_GITHUB_TOKEN": ""}), patch.object(tt, "_send") as send:
            res = tt.search_tickets("心跳")
        self.assertFalse(res["success"])
        self.assertIn("尚未設定", res["error"])
        send.assert_not_called()

    def test_api_error_returns_failure_without_token(self):
        res = run_with(self._fake({"message": f"Bad credentials {FAKE_TOKEN}"}, status=401), tt.search_tickets, "心跳")
        self.assertFalse(res["success"])
        self.assertNotIn(FAKE_TOKEN, json.dumps(res, ensure_ascii=False))

    def test_paginates_up_to_three_pages(self):
        page = [issue(n, title=f"無關 {n}") for n in range(100)]
        fake = self._fake(page)
        run_with(fake, tt.search_tickets, "心跳")
        self.assertEqual(len(fake.calls), tt.MAX_PAGES)
        self.assertIn("page=3", fake.calls[-1]["url"])

    def test_stops_when_page_not_full(self):
        fake = self._fake([issue(1, title="心跳")])
        run_with(fake, tt.search_tickets, "心跳")
        self.assertEqual(len(fake.calls), 1)


class TestFindSimilar(unittest.TestCase):
    def _fake(self, items):
        return FakeGitHub({("GET", ISSUES): (200, items)})

    def test_similar_title_is_candidate_including_closed(self):
        items = [issue(1, title="心跳訊息精簡", state="closed"), issue(2, title="永豐報價排行")]
        fake = self._fake(items)
        res = run_with(fake, tt.find_similar, "心跳訊息精簡化", "")
        self.assertTrue(res["success"])
        self.assertEqual([c["number"] for c in res["candidates"]], [1])
        self.assertEqual(res["candidates"][0]["state"], "closed")
        self.assertIn("state=all", fake.calls[0]["url"])

    def test_threshold_is_025(self):
        self.assertEqual(tt.SIMILARITY_THRESHOLD, 0.25)
        self.assertEqual(tt.MAX_CANDIDATES, 3)

    def test_below_threshold_not_candidate(self):
        res = run_with(self._fake([issue(1, title="永豐報價排行")]), tt.find_similar, "心跳訊息精簡", "")
        self.assertEqual(res["candidates"], [])

    def test_at_most_three_sorted_by_score(self):
        items = [issue(n, title="心跳訊息精簡" + "延伸" * n) for n in range(1, 8)]
        res = run_with(self._fake(items), tt.find_similar, "心跳訊息精簡", "")
        self.assertEqual(len(res["candidates"]), 3)
        scores = [c["score"] for c in res["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_body_contributes_and_metadata_ignored(self):
        old = tt.build_metadata("pm") + "\n" + story("讓 ticket 可以搜尋並修改內文", ("支援關鍵字搜尋", "可修改內文"))
        items = [issue(1, title="完全不同的標題", body=old)]
        same_body = story("讓 ticket 可以搜尋並修改內文", ("支援關鍵字搜尋", "可修改內文"))
        res = run_with(self._fake(items), tt.find_similar, "另一個說法", same_body)
        self.assertEqual([c["number"] for c in res["candidates"]], [1])

    def test_excludes_pull_requests(self):
        res = run_with(self._fake([pr_item(5, "心跳訊息精簡")]), tt.find_similar, "心跳訊息精簡", "")
        self.assertEqual(res["candidates"], [])

    def test_api_error_is_failure(self):
        fake = FakeGitHub({("GET", ISSUES): (500, {"message": "boom"})})
        res = run_with(fake, tt.find_similar, "心跳", "")
        self.assertFalse(res["success"])


class TestUpdateBody(unittest.TestCase):
    META = tt.build_metadata("pm", engine="claude", session_id="abc")

    def _fake(self, current):
        return FakeGitHub({
            ("GET", f"{ISSUES}/7"): (200, current),
            ("PATCH", f"{ISSUES}/7"): lambda body: (200, issue(7)),
            ("POST", f"{ISSUES}/7/comments"): (201, {"id": 1}),
            ("PUT", f"{ISSUES}/7/labels"): lambda body: (200, []),
        })

    def _old(self):
        return self.META + "\n" + story("舊的故事", ("做 A", "做 B"))

    def test_replaces_body_and_keeps_metadata_line_verbatim(self):
        fake = self._fake(issue(7, body=self._old()))
        res = run_with(fake, tt.update_ticket, 7, body=story("新的故事", ("做 A", "做 C")))
        self.assertTrue(res["success"])
        sent = fake.find("PATCH", f"{ISSUES}/7")[0]["body"]["body"]
        self.assertEqual(sent.split("\n", 1)[0], self.META)
        self.assertIn("新的故事", sent)
        self.assertNotIn("舊的故事", sent)
        self.assertEqual(sent.count("<!-- arq:"), 1)
        self.assertNotIn("state", fake.find("PATCH", f"{ISSUES}/7")[0]["body"])

    def test_incoming_metadata_line_is_stripped(self):
        forged = tt.build_metadata("user", engine="agy")
        fake = self._fake(issue(7, body=self._old()))
        run_with(fake, tt.update_ticket, 7, body=forged + "\n" + story("新的故事"))
        sent = fake.find("PATCH", f"{ISSUES}/7")[0]["body"]["body"]
        self.assertEqual(sent.count("<!-- arq:"), 1)
        self.assertEqual(sent.split("\n", 1)[0], self.META)
        self.assertNotIn("agy", sent)

    def test_incoming_malformed_metadata_prefix_also_stripped(self):
        fake = self._fake(issue(7, body=self._old()))
        run_with(fake, tt.update_ticket, 7, body='<!-- arq:{"opened_by": "x"\n' + story("新的故事"))
        sent = fake.find("PATCH", f"{ISSUES}/7")[0]["body"]["body"]
        self.assertEqual(sent.count("<!-- arq:"), 1)

    def test_ticket_without_metadata_gets_none(self):
        fake = self._fake(issue(7, body=story("舊的")))
        run_with(fake, tt.update_ticket, 7, body=story("新的"))
        sent = fake.find("PATCH", f"{ISSUES}/7")[0]["body"]["body"]
        self.assertNotIn("<!-- arq:", sent)
        self.assertTrue(sent.startswith("**使用者故事**"))

    def test_missing_sections_filled_with_tbd(self):
        fake = self._fake(issue(7, body=self._old()))
        run_with(fake, tt.update_ticket, 7, body="只有一段新的描述")
        sent = fake.find("PATCH", f"{ISSUES}/7")[0]["body"]["body"]
        for section in tt.SECTIONS:
            self.assertIn(f"**{section}**", sent)
        self.assertIn(tt.TBD, sent)

    def test_body_redacted(self):
        secret = "sk-SECRETVALUE-1234567890"
        fake = self._fake(issue(7, body=self._old()))
        with patch.dict(os.environ, {"SOME_API_KEY": secret}):
            run_with(fake, tt.update_ticket, 7, body=story(f"key={secret}"))
        self.assertNotIn(secret, json.dumps(fake.calls, ensure_ascii=False))

    def test_auto_comment_lists_changes_and_ai_comment(self):
        fake = self._fake(issue(7, body=self._old()))
        res = run_with(fake, tt.update_ticket, 7, body=story("新的故事", ("做 A", "做 C")), comment="補上 C")
        self.assertTrue(res["success"])
        comments = fake.find("POST", f"{ISSUES}/7/comments")
        self.assertEqual(len(comments), 1)
        text = comments[0]["body"]["body"]
        self.assertIn("做 C", text)
        self.assertIn("做 B", text)
        self.assertIn("使用者故事", text)
        self.assertIn("補上 C", text)
        self.assertNotIn("範圍外", text)
        self.assertLessEqual(len(text), tt.MAX_DIFF_COMMENT_CHARS + 1100)

    def test_auto_comment_redacted(self):
        secret = "sk-SECRETVALUE-1234567890"
        fake = self._fake(issue(7, body=self._old()))
        with patch.dict(os.environ, {"SOME_API_KEY": secret}):
            run_with(fake, tt.update_ticket, 7, body=story("新的", (f"見 {secret}",)), comment=f"k={secret}")
        for c in fake.find("POST", f"{ISSUES}/7/comments"):
            self.assertNotIn(secret, c["body"]["body"])

    def test_body_with_status_updates_both(self):
        fake = self._fake(issue(7, body=self._old()))
        res = run_with(fake, tt.update_ticket, 7, body=story("新的"), status="in-progress")
        self.assertTrue(res["success"])
        self.assertEqual(len(fake.find("PUT", f"{ISSUES}/7/labels")), 1)
        self.assertEqual(len(fake.find("PATCH", f"{ISSUES}/7")), 1)

    def test_closed_ticket_body_rejected_and_not_reopened(self):
        fake = self._fake(issue(7, state="closed", body=self._old()))
        res = run_with(fake, tt.update_ticket, 7, body=story("新的"), comment="補充", status="in-progress")
        self.assertFalse(res["success"])
        self.assertIn("#7", res["error"])
        self.assertIn("新單", res["error"])
        self.assertEqual(fake.find("PATCH", ISSUES), [])
        self.assertEqual(fake.find("POST", f"{ISSUES}/7/comments"), [])
        self.assertEqual(fake.find("PUT", f"{ISSUES}/7/labels"), [])

    def test_truncated_marker_body_rejected(self):
        fake = self._fake(issue(7, body=self._old()))
        res = run_with(fake, tt.update_ticket, 7, body=story("新的") + "\n…(已截斷，原長 9999 字；完整內容在 GitHub)")
        self.assertFalse(res["success"])
        self.assertEqual(fake.find("PATCH", f"{ISSUES}/7"), [])

    def test_body_alone_counts_as_something_to_update(self):
        fake = self._fake(issue(7, body=self._old()))
        self.assertTrue(run_with(fake, tt.update_ticket, 7, body=story("新的"))["success"])
        res = run_with(self._fake(issue(7)), tt.update_ticket, 7, body="   ")
        self.assertFalse(res["success"])

    def test_pull_request_rejected(self):
        fake = self._fake(issue(7, pull_request={"url": "x"}))
        self.assertFalse(run_with(fake, tt.update_ticket, 7, body=story("新的"))["success"])
        self.assertEqual(fake.find("PATCH", f"{ISSUES}/7"), [])

    def test_patch_failure_returns_failure_without_comment(self):
        fake = self._fake(issue(7, body=self._old()))
        fake.routes[("PATCH", f"{ISSUES}/7")] = (500, {"message": "boom"})
        res = run_with(fake, tt.update_ticket, 7, body=story("新的"))
        self.assertFalse(res["success"])
        self.assertEqual(fake.find("POST", f"{ISSUES}/7/comments"), [])


class TestBodyDiffSummary(unittest.TestCase):
    def test_reports_added_removed_items_and_changed_sections(self):
        old = story("舊", ("A", "B"))
        new = story("新", ("A", "C"))
        text = tt._body_diff_summary(old, new)
        self.assertIn("新增", text)
        self.assertIn("C", text)
        self.assertIn("移除", text)
        self.assertIn("B", text)
        self.assertIn("使用者故事", text)

    def test_checkbox_state_change_is_not_a_new_item(self):
        text = tt._body_diff_summary(story("同", ("A",)), story("同", ("A",)).replace("- [ ] A", "- [x] A"))
        self.assertNotIn("新增", text)
        self.assertNotIn("移除", text)

    def test_length_capped(self):
        new = story("同", tuple(f"項目{n}" * 20 for n in range(100)))
        self.assertLessEqual(len(tt._body_diff_summary("", new)), tt.MAX_DIFF_COMMENT_CHARS)


class TestDuplicateGuard(unittest.TestCase):
    def _candidates(self, *numbers):
        return {"success": True, "candidates": [
            {"number": n, "title": f"舊單 {n}", "state": "open", "status": "todo", "url": f"u{n}", "score": 0.5}
            for n in numbers]}

    def test_similar_ticket_blocks_create(self):
        with patch.object(tt, "find_similar", return_value=self._candidates(3, 4)) as f, \
                patch.object(tt, "create_ticket") as create:
            res = tt.ticket("create", title="心跳訊息", body="內容")
        create.assert_not_called()
        f.assert_called_once_with("心跳訊息", "內容")
        self.assertFalse(res["success"])
        self.assertEqual([c["number"] for c in res["duplicate_candidates"]], [3, 4])
        self.assertEqual(set(res["duplicate_candidates"][0]), {"number", "title", "state", "status", "url"})
        self.assertIn("更新舊單", res["instruction"])
        self.assertIn("開新單", res["instruction"])

    def test_no_similar_creates_normally(self):
        with patch.object(tt, "find_similar", return_value=self._candidates()), \
                patch.object(tt, "create_ticket", return_value={"success": True, "number": 9}) as create:
            res = tt.ticket("create", title="全新想法", body="內容", ticket_type="chore", priority="p3")
        self.assertTrue(res["success"])
        create.assert_called_once_with(title="全新想法", body="內容", opened_by="pm", type_="chore", priority="p3")

    def test_force_new_skips_check(self):
        with patch.object(tt, "find_similar") as f, \
                patch.object(tt, "create_ticket", return_value={"success": True, "number": 9}) as create:
            res = tt.ticket("create", title="心跳訊息", body="內容", force_new=True)
        f.assert_not_called()
        create.assert_called_once()
        self.assertTrue(res["success"])

    def test_check_failure_does_not_create(self):
        with patch.object(tt, "find_similar", return_value={"success": False, "error": "GitHub API 錯誤 500"}), \
                patch.object(tt, "create_ticket") as create:
            res = tt.ticket("create", title="心跳訊息", body="內容")
        create.assert_not_called()
        self.assertFalse(res["success"])
        self.assertIn("500", res["error"])

    def test_end_to_end_with_fake_github(self):
        fake = FakeGitHub({
            ("GET", ISSUES): (200, [issue(3, title="心跳訊息精簡", state="closed")]),
            ("POST", ISSUES): (201, {"number": 10, "html_url": "u"}),
        })
        res = run_with(fake, tt.ticket, "create", title="心跳訊息精簡化", body="")
        self.assertFalse(res["success"])
        self.assertEqual(fake.find("POST", ISSUES), [])
        self.assertEqual(res["duplicate_candidates"][0]["state"], "closed")
        res = run_with(fake, tt.ticket, "create", title="心跳訊息精簡化", body="", force_new=True)
        self.assertTrue(res["success"])
        self.assertEqual(len(fake.find("POST", ISSUES)), 1)

    def test_rd_dispatch_create_ticket_is_not_guarded(self):
        fake = FakeGitHub({("POST", ISSUES): (201, {"number": 10, "html_url": "u"})})
        res = run_with(fake, tt.create_ticket, title="心跳訊息精簡", body="x", opened_by="pm")
        self.assertTrue(res["success"])
        self.assertEqual(fake.find("GET", ISSUES), [])


class TestForceNewRisk(unittest.TestCase):
    def test_force_new_create_needs_confirmation(self):
        self.assertIsNotNone(tt.ticket_risk({"action": "create", "title": "t", "force_new": True}))
        self.assertIsNotNone(tt.ticket_risk({"action": "CREATE", "title": "t", "force_new": "true"}))

    def test_ordinary_calls_need_no_confirmation(self):
        for args in ({"action": "create", "title": "t"}, {"action": "create", "title": "t", "force_new": False},
                     {"action": "search", "query": "q"}, {"action": "update", "number": 3, "body": "x"},
                     {"action": "list"}, {"action": "get", "number": 3}):
            with self.subTest(args=args):
                self.assertIsNone(tt.ticket_risk(args))

    def test_registered_spec_uses_risk(self):
        import agent
        spec = agent.TOOL_REGISTRY["ticket"]
        self.assertIsNotNone(spec.risk({"action": "create", "title": "t", "force_new": True}))

    def test_card_shows_title(self):
        import agent
        args = {"action": "create", "title": "心跳訊息精簡", "force_new": True}
        text = agent.describe_tool_call("ticket", args, tt.ticket_risk(args))
        self.assertIn("心跳訊息精簡", text)


class TestGeminiSearchAction(unittest.TestCase):
    def test_search_defaults_to_state_all(self):
        with patch.object(tt, "search_tickets", return_value={"success": True, "tickets": []}) as m:
            tt.ticket("search", query="心跳")
        m.assert_called_once_with("心跳", state="all")

    def test_search_passes_explicit_state(self):
        with patch.object(tt, "search_tickets", return_value={"success": True, "tickets": []}) as m:
            tt.ticket("search", query="心跳", state="closed")
        m.assert_called_once_with("心跳", state="closed")

    def test_list_default_state_still_open(self):
        with patch.object(tt, "list_tickets", return_value={"success": True, "total": 0, "tickets": []}) as m:
            tt.ticket("list")
        m.assert_called_once_with(state="open", opened_by="", status="")

    def test_search_without_query_fails_without_network(self):
        with patch.object(tt, "_send") as send:
            res = tt.ticket("search")
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_update_passes_body(self):
        with patch.object(tt, "update_ticket", return_value={"success": True}) as u:
            tt.ticket("update", number=7, body="新內文", comment="c")
        u.assert_called_once_with(7, status="", comment="c", close=False, close_reason="completed", body="新內文")

    def test_unknown_action_lists_search(self):
        self.assertIn("search", tt.ticket("delete", number=3)["error"])

    def test_system_instruction_mentions_search_first_and_has_no_blank_lines(self):
        import agent
        text = agent.SYSTEM_INSTRUCTION
        self.assertIn("search", text)
        self.assertNotIn("\n\n", text.split("1.【問答】", 1)[1])


if __name__ == "__main__":
    unittest.main()
