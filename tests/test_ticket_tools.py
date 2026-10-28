"""
tools/ticket_tools.py 單元測試 (不連網，mock 底層送出函式 _send)

對應 的驗收標準：
    - create / list / get / update 四個函式，以 urllib 呼叫 GitHub REST，不新增套件
    - 開單時內文第一行寫入隱藏 metadata <!-- arq:{...} -->；開單者只接受已定義的角色
    - 改狀態時只替換 status:* label，其他 label 不動；已關閉的單不會被重新打開
    - 寫入前經過 redact()；回傳結果與錯誤訊息不含 token；失敗回傳 success=False 不拋例外
    - init_labels() 建立所有 label，已存在的略過
    - TICKETS_GITHUB_TOKEN 列入 http_tools 的 _CORE_SECRETS (call_api 不能注入)
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

from tools import http_tools  # noqa: E402
from tools import ticket_tools as tt  # noqa: E402

FAKE_TOKEN = "github_pat_FAKE000011112222333344445555"
ENV = {"TICKETS_REPO": "owner/repo", "TICKETS_GITHUB_TOKEN": FAKE_TOKEN}


def issue(number=7, labels=("by:pm", "status:todo", "priority:p1"), state="open", body="", **extra):
    data = {
        "number": number, "title": f"票 {number}", "state": state, "body": body,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "labels": [{"name": n} for n in labels],
        "created_at": "2026-10-19T00:00:00Z", "updated_at": "2026-10-19T00:00:00Z",
    }
    data.update(extra)
    return data


class FakeGitHub:
    """取代 ticket_tools._send：依 (method, path 前綴) 回傳預設回應，並記錄所有請求"""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, method, url, headers, data):
        body = json.loads(data.decode("utf-8")) if data else None
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        path = url.split("api.github.com", 1)[-1]
        for (m, prefix), resp in self.routes.items():
            if m == method and path.startswith(prefix):
                return resp(body) if callable(resp) else resp
        return 404, {"message": "Not Found"}

    def find(self, method, prefix):
        return [c for c in self.calls if c["method"] == method and c["url"].split("api.github.com", 1)[-1].startswith(prefix)]


def run_with(fake, func, *args, env=ENV, **kwargs):
    with patch.dict(os.environ, env, clear=False), patch.object(tt, "_send", fake):
        return func(*args, **kwargs)


class TestConfig(unittest.TestCase):
    def test_not_configured_returns_clear_error(self):
        fake = FakeGitHub()
        with patch.dict(os.environ, {"TICKETS_REPO": "", "TICKETS_GITHUB_TOKEN": ""}), patch.object(tt, "_send", fake):
            res = tt.list_tickets()
        self.assertFalse(res["success"])
        self.assertIn("尚未設定", res["error"])
        self.assertEqual(fake.calls, [])

    def test_invalid_repo_format_rejected(self):
        fake = FakeGitHub()
        res = run_with(fake, tt.list_tickets, env={**ENV, "TICKETS_REPO": "../../evil"})
        self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])

    def test_requests_use_https_github_and_bearer_header(self):
        fake = FakeGitHub({("GET", "/repos/owner/repo/issues"): (200, [])})
        run_with(fake, tt.list_tickets)
        call = fake.calls[0]
        self.assertTrue(call["url"].startswith("https://api.github.com/repos/owner/repo/issues"))
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {FAKE_TOKEN}")
        self.assertNotIn(FAKE_TOKEN, call["url"])


class TestCreate(unittest.TestCase):
    def _create(self, **kwargs):
        fake = FakeGitHub({("GET", "/repos/owner/repo/issues/5"): (200, issue(5, id=500)),  # parent=5 會先確認母單存在
                           ("POST", "/repos/owner/repo/issues/5/sub_issues"): (201, {}),
                           ("POST", "/repos/owner/repo/issues"): lambda body: (201, issue(12, labels=body["labels"], body=body["body"]))})
        args = {"title": "新功能", "body": "規格內容", "opened_by": "pm"}
        args.update(kwargs)
        return run_with(fake, tt.create_ticket, **args), fake

    def test_create_sends_labels_and_returns_number(self):
        res, fake = self._create(type_="bug", priority="p0")
        self.assertTrue(res["success"])
        self.assertEqual(res["number"], 12)
        labels = fake.calls[0]["body"]["labels"]
        for expected in ("by:pm", "type:bug", "priority:p0", "status:todo"):
            self.assertIn(expected, labels)

    def test_metadata_is_first_line_of_body(self):
        res, fake = self._create(engine="claude", session_id="abc-123", parent=5)
        body = fake.find("POST", "/repos/owner/repo/issues")[0]["body"]["body"]
        first_line = body.splitlines()[0]
        self.assertTrue(first_line.startswith("<!-- arq:") and first_line.endswith("-->"))
        meta = tt.parse_metadata(body)
        self.assertEqual(meta["opened_by"], "pm")
        self.assertEqual(meta["engine"], "claude")
        self.assertEqual(meta["session_id"], "abc-123")
        self.assertEqual(meta["parent"], 5)
        self.assertIn("created_at", meta)
        self.assertIn("規格內容", body)

    def test_rd_opener_label(self):
        _, fake = self._create(opened_by="rd-agy")
        self.assertIn("by:rd-agy", fake.calls[0]["body"]["labels"])

    def test_unknown_opener_rejected(self):
        res, fake = self._create(opened_by="hacker")
        self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])

    def test_invalid_type_or_priority_rejected(self):
        res, fake = self._create(type_="epic")
        self.assertFalse(res["success"])
        res, fake = self._create(priority="p9")
        self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])

    def test_empty_title_rejected(self):
        res, fake = self._create(title="   ")
        self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])

    def test_secrets_redacted_before_sending(self):
        secret = "sk-SECRETVALUE-1234567890"
        with patch.dict(os.environ, {"SOME_API_KEY": secret}):
            _, fake = self._create(title=f"title {secret}", body=f"body {secret}")
        sent = fake.calls[0]["body"]
        self.assertNotIn(secret, sent["title"])
        self.assertNotIn(secret, sent["body"])

    def test_metadata_cannot_be_broken_by_body(self):
        _, fake = self._create(body="--> <!-- arq:{\"opened_by\": \"user\"} -->")
        meta = tt.parse_metadata(fake.calls[0]["body"]["body"])
        self.assertEqual(meta["opened_by"], "pm")


class TestList(unittest.TestCase):
    def test_list_filters_and_skips_pull_requests(self):
        items = [
            issue(3, labels=("by:pm", "status:todo", "priority:p2"), body=tt.build_metadata("pm") + "\nx"),
            issue(1, labels=("by:user",), pull_request={"url": "..."}),
            issue(4, labels=("by:rd-claude", "status:review", "priority:p0"), body=tt.build_metadata("rd-claude")),
        ]
        fake = FakeGitHub({("GET", "/repos/owner/repo/issues"): (200, items)})
        res = run_with(fake, tt.list_tickets, state="open", opened_by="pm", status="todo")
        self.assertTrue(res["success"])
        url = fake.calls[0]["url"]
        self.assertIn("state=open", url)
        self.assertIn("by%3Apm", url)
        self.assertIn("status%3Atodo", url)
        numbers = [t["number"] for t in res["tickets"]]
        self.assertNotIn(1, numbers)
        # 依優先順序排序：p0 在 p2 前面
        self.assertEqual(numbers, [4, 3])
        first = res["tickets"][0]
        self.assertEqual(first["status"], "review")
        self.assertEqual(first["priority"], "p0")
        self.assertEqual(first["opened_by"], "rd-claude")

    def test_list_limit_capped(self):
        fake = FakeGitHub({("GET", "/repos/owner/repo/issues"): (200, [issue(i) for i in range(1, 40)])})
        res = run_with(fake, tt.list_tickets, limit=500)
        self.assertLessEqual(len(res["tickets"]), tt.MAX_LIST)

    def test_list_rejects_unknown_filters(self):
        fake = FakeGitHub()
        res = run_with(fake, tt.list_tickets, status="whatever")
        self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])

    def test_opened_by_prefers_metadata_and_flags_mismatch(self):
        items = [issue(5, labels=("by:pm",), body=tt.build_metadata("rd-agy"))]
        fake = FakeGitHub({("GET", "/repos/owner/repo/issues"): (200, items)})
        res = run_with(fake, tt.list_tickets)
        t = res["tickets"][0]
        self.assertEqual(t["opened_by"], "rd-agy")
        self.assertEqual(t["opened_by_label"], "pm")
        self.assertTrue(t["opener_mismatch"])


class TestGet(unittest.TestCase):
    def test_get_returns_body_and_last_comments(self):
        comments = [{"user": {"login": "owner"}, "body": f"留言 {i}", "created_at": "2026-10-19T00:00:00Z"} for i in range(15)]
        fake = FakeGitHub({
            ("GET", "/repos/owner/repo/issues/7/comments"): (200, comments),
            ("GET", "/repos/owner/repo/issues/7"): (200, issue(7, body=tt.build_metadata("pm") + "\n內容")),
        })
        res = run_with(fake, tt.get_ticket, 7)
        self.assertTrue(res["success"])
        self.assertEqual(res["ticket"]["number"], 7)
        self.assertIn("內容", res["ticket"]["body"])
        self.assertNotIn("<!-- arq:", res["ticket"]["body"])
        self.assertEqual(len(res["ticket"]["comments"]), tt.MAX_COMMENTS)
        self.assertEqual(res["ticket"]["comments"][-1]["body"], "留言 14")

    def test_get_pull_request_rejected(self):
        fake = FakeGitHub({("GET", "/repos/owner/repo/issues/1"): (200, issue(1, pull_request={"url": "..."}))})
        res = run_with(fake, tt.get_ticket, 1)
        self.assertFalse(res["success"])

    def test_get_not_found(self):
        res = run_with(FakeGitHub(), tt.get_ticket, 999)
        self.assertFalse(res["success"])
        self.assertIn("404", res["error"])


class TestUpdate(unittest.TestCase):
    def _fake(self, current):
        return FakeGitHub({
            ("GET", "/repos/owner/repo/issues/7"): (200, current),
            ("PUT", "/repos/owner/repo/issues/7/labels"): lambda body: (200, [{"name": n} for n in body["labels"]]),
            ("POST", "/repos/owner/repo/issues/7/comments"): (201, {"id": 1}),
            ("PATCH", "/repos/owner/repo/issues/7"): lambda body: (200, issue(7, state=body.get("state", "open"))),
        })

    def test_status_change_only_replaces_status_label(self):
        fake = self._fake(issue(7, labels=("by:pm", "status:todo", "priority:p1", "rd:claude")))
        res = run_with(fake, tt.update_ticket, 7, status="in-progress")
        self.assertTrue(res["success"])
        sent = set(fake.find("PUT", "/repos/owner/repo/issues/7/labels")[0]["body"]["labels"])
        self.assertEqual(sent, {"by:pm", "status:in-progress", "priority:p1", "rd:claude"})
        self.assertEqual(fake.find("PATCH", "/repos/owner/repo/issues/7"), [])

    def test_comment_only_does_not_touch_labels(self):
        fake = self._fake(issue(7))
        res = run_with(fake, tt.update_ticket, 7, comment="開工")
        self.assertTrue(res["success"])
        self.assertEqual(fake.find("PUT", "/repos/owner/repo/issues/7/labels"), [])
        self.assertEqual(fake.find("POST", "/repos/owner/repo/issues/7/comments")[0]["body"]["body"], "開工")

    def test_comment_redacted(self):
        secret = "sk-SECRETVALUE-1234567890"
        fake = self._fake(issue(7))
        with patch.dict(os.environ, {"SOME_API_KEY": secret}):
            run_with(fake, tt.update_ticket, 7, comment=f"key={secret}")
        sent = fake.find("POST", "/repos/owner/repo/issues/7/comments")[0]["body"]["body"]
        self.assertNotIn(secret, sent)

    def test_closed_ticket_is_never_reopened(self):
        fake = self._fake(issue(7, state="closed", labels=("by:pm", "status:done")))
        res = run_with(fake, tt.update_ticket, 7, status="in-progress", comment="還有問題")
        self.assertTrue(res["success"])
        for call in fake.find("PATCH", "/repos/owner/repo/issues/7"):
            self.assertNotEqual(call["body"].get("state"), "open")
        self.assertEqual(fake.find("PUT", "/repos/owner/repo/issues/7/labels"), [])
        self.assertIn("已關閉", res["note"])
        # 留言照常寫入
        self.assertEqual(len(fake.find("POST", "/repos/owner/repo/issues/7/comments")), 1)

    def test_close_sets_done(self):
        # 起只有對應 PR 已 merge 才能關單 (其他規則見 tests/test_merge_gate.py)
        fake = self._fake(issue(7, labels=("by:pm", "status:review")))
        fake.routes = {("GET", "/repos/owner/repo/issues/7/timeline"): (200, [{
            "event": "cross-referenced", "source": {"issue": {
                "number": 40, "body": "Closes #7", "pull_request": {"merged_at": "2026-10-19T05:00:00Z"}}}}]),
            **fake.routes}
        res = run_with(fake, tt.update_ticket, 7, close=True)
        self.assertTrue(res["success"])
        labels = set(fake.find("PUT", "/repos/owner/repo/issues/7/labels")[0]["body"]["labels"])
        self.assertIn("status:done", labels)
        self.assertNotIn("status:review", labels)
        patch_call = fake.find("PATCH", "/repos/owner/repo/issues/7")[0]
        self.assertEqual(patch_call["body"]["state"], "closed")

    def test_invalid_status_rejected(self):
        fake = self._fake(issue(7))
        res = run_with(fake, tt.update_ticket, 7, status="shipped")
        self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])

    def test_nothing_to_update_rejected(self):
        fake = self._fake(issue(7))
        res = run_with(fake, tt.update_ticket, 7)
        self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])


class TestFailureSafety(unittest.TestCase):
    def test_network_exception_returns_failure(self):
        def boom(*args):
            raise OSError(f"connection reset, token={FAKE_TOKEN}")
        res = run_with(boom, tt.list_tickets)
        self.assertFalse(res["success"])
        self.assertNotIn(FAKE_TOKEN, json.dumps(res, ensure_ascii=False))

    def test_api_error_message_has_no_token(self):
        fake = FakeGitHub({("POST", "/repos/owner/repo/issues"): (401, {"message": f"Bad credentials {FAKE_TOKEN}"})})
        res = run_with(fake, tt.create_ticket, title="t", body="b", opened_by="pm")
        self.assertFalse(res["success"])
        self.assertIn("401", res["error"])
        self.assertNotIn(FAKE_TOKEN, json.dumps(res, ensure_ascii=False))


class TestInitLabels(unittest.TestCase):
    def test_creates_all_and_skips_existing(self):
        def create(body):
            if body["name"] == "by:pm":
                return 422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]}
            return 201, body
        fake = FakeGitHub({("POST", "/repos/owner/repo/labels"): create})
        res = run_with(fake, tt.init_labels)
        self.assertTrue(res["success"])
        names = [c["body"]["name"] for c in fake.calls]
        self.assertEqual(len(names), len(tt.LABELS))
        self.assertIn("by:pm", res["skipped"])
        self.assertEqual(len(res["created"]), len(tt.LABELS) - 1)

    def test_label_set_covers_all_values(self):
        names = {name for name, _, _ in tt.LABELS}
        for group, values in (("by", tt.OPENERS), ("type", tt.TYPES), ("status", tt.STATUSES),
                              ("priority", tt.PRIORITIES), ("size", tt.SIZES), ("rd", tt.ENGINES)):
            for v in values:
                self.assertIn(f"{group}:{v}", names)


class TestGeminiTool(unittest.TestCase):
    """：給 Gemini 的單一工具 ticket(action, ...)"""

    def test_registered_and_runs_without_confirmation(self):
        import agent
        spec = agent.TOOL_REGISTRY.get("ticket")
        self.assertIsNotNone(spec)
        # 一般開單、查單、改單直接執行；只有以 not planned 關單需要確認卡
        self.assertIsNone(spec.risk({"action": "create", "title": "t"}))

    def test_docstring_is_short(self):
        doc = [line for line in (tt.ticket.__doc__ or "").strip().splitlines() if line.strip()]
        self.assertTrue(1 <= len(doc) <= 6, f"docstring {len(doc)} 行，每次呼叫 Gemini 都會送出，請保持約 5 行")

    def test_ai_cannot_choose_opener(self):
        import inspect
        params = inspect.signature(tt.ticket).parameters
        self.assertNotIn("opened_by", params)
        self.assertNotIn("engine", params)
        self.assertNotIn("session_id", params)

    def test_create_always_by_pm(self):
        with patch.object(tt, "find_similar", return_value={"success": True, "candidates": []}), \
                patch.object(tt, "create_ticket", return_value={"success": True, "number": 9}) as m:
            res = tt.ticket("create", title="記下這個想法", body="內容", ticket_type="chore", priority="p3")
        self.assertTrue(res["success"])
        m.assert_called_once_with(title="記下這個想法", body="內容", opened_by="pm", type_="chore", priority="p3")

    def test_list_returns_compact_fields(self):
        full = {"success": True, "total": 1, "tickets": [{
            "number": 3, "title": "t", "state": "open", "status": "todo", "priority": "p1", "type": "feature",
            "rd": None, "opened_by": "pm", "opened_by_label": "pm", "opener_mismatch": False,
            "url": "https://github.com/owner/repo/issues/3", "updated_at": "x"}]}
        with patch.object(tt, "list_tickets", return_value=full) as m:
            res = tt.ticket("list", state="all", by="rd-claude", status="review")
        m.assert_called_once_with(state="all", opened_by="rd-claude", status="review")
        self.assertEqual(res["tickets"], [{"number": 3, "title": "t", "status": "todo", "priority": "p1", "opened_by": "pm"}])
        self.assertEqual(res["total"], 1)

    def test_get_and_update_pass_through(self):
        with patch.object(tt, "get_ticket", return_value={"success": True}) as g:
            tt.ticket("get", number=7)
        g.assert_called_once_with(7)
        with patch.object(tt, "update_ticket", return_value={"success": True}) as u:
            tt.ticket("update", number=7, status="review", comment="完成", close=False)
        u.assert_called_once_with(7, status="review", comment="完成", close=False, close_reason="completed", body="")

    def test_number_required_for_get_and_update(self):
        for action in ("get", "update"):
            res = tt.ticket(action)
            self.assertFalse(res["success"])
            self.assertIn("number", res["error"])

    def test_unknown_action_rejected(self):
        res = tt.ticket("delete", number=3)
        self.assertFalse(res["success"])

    def test_not_configured_message(self):
        with patch.dict(os.environ, {"TICKETS_REPO": "", "TICKETS_GITHUB_TOKEN": ""}), patch.object(tt, "_send") as send:
            res = tt.ticket("list")
        self.assertFalse(res["success"])
        self.assertIn("尚未設定", res["error"])
        send.assert_not_called()

    def test_pm_rule_in_system_instruction(self):
        import agent
        text = agent.SYSTEM_INSTRUCTION
        self.assertIn("派工時系統會自動開單", text)  # 起開單改由程式保證，PM 不必自己開
        rules = text.split("1.【問答】", 1)[1]
        self.assertNotIn("\n\n", rules)  # 新增規則不得引入空行 (開頭段落與 1. 之間原本就有一個空行，不在本單範圍)


class TestTicketFormat(unittest.TestCase):
    """：固定 ticket 格式 (範本 + 開單時套用) 與 Definition of Done"""

    SECTIONS = ("使用者故事", "驗收標準", "範圍外", "相依")

    def _positions(self, text):
        return [text.find(f"**{s}**") for s in self.SECTIONS]

    def test_plain_body_wrapped_into_sections(self):
        text = tt.format_ticket_body("心跳訊息太長，想改短一點")
        pos = self._positions(text)
        self.assertTrue(all(p >= 0 for p in pos), text)
        self.assertEqual(pos, sorted(pos))
        story = text[pos[0]:pos[1]]
        self.assertIn("心跳訊息太長", story)
        self.assertIn(tt.TBD, text[pos[1]:pos[2]])

    def test_complete_body_unchanged(self):
        body = "**使用者故事**\nA\n\n**驗收標準**\n- [ ] B\n\n**範圍外**\nC\n\n**相依**：無"
        self.assertEqual(tt.format_ticket_body(body), body)

    def test_markdown_heading_style_accepted(self):
        body = "## 使用者故事\nA\n## 驗收標準\n- [ ] B\n## 範圍外\nC\n## 相依\n無"
        self.assertEqual(tt.format_ticket_body(body), body)

    def test_partial_body_gets_missing_sections(self):
        text = tt.format_ticket_body("**使用者故事**\nA\n\n**驗收標準**\n- [ ] B")
        self.assertIn("- [ ] B", text)
        self.assertIn("**範圍外**", text)
        self.assertIn("**相依**", text)
        self.assertEqual(text.count("**驗收標準**"), 1)

    def test_create_ticket_applies_format(self):
        fake = FakeGitHub({("POST", "/repos/owner/repo/issues"): lambda body: (201, issue(12, body=body["body"]))})
        run_with(fake, tt.create_ticket, title="t", body="只有一句話", opened_by="pm")
        sent = fake.calls[0]["body"]["body"]
        self.assertTrue(sent.startswith("<!-- arq:"))
        for s in self.SECTIONS:
            self.assertIn(f"**{s}**", sent)

    def test_issue_templates_exist(self):
        base = os.path.join(PROJECT_ROOT, ".github", "ISSUE_TEMPLATE")
        label_names = {name for name, _, _ in tt.LABELS}
        for name, type_label in (("feature.md", "type:feature"), ("bug.md", "type:bug")):
            with open(os.path.join(base, name), encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(text.startswith("---\n"), f"{name} 需要 front matter")
            front = text.split("---", 2)[1]
            labels_line = next(line for line in front.splitlines() if line.startswith("labels:"))
            labels = [x.strip().strip("'\"") for x in labels_line.split(":", 1)[1].split(",")]
            self.assertIn(type_label, labels)
            self.assertIn("by:user", labels)
            for lb in labels:
                self.assertIn(lb, label_names, f"{name} 用了未定義的 label {lb}")
            for s in self.SECTIONS:
                self.assertIn(f"**{s}**", text, f"{name} 缺少 {s}")
        with open(os.path.join(base, "bug.md"), encoding="utf-8") as f:
            self.assertIn("重現步驟", f.read())

    def test_definition_of_done_written(self):
        with open(os.path.join(PROJECT_ROOT, ".github", "pull_request_template.md"), encoding="utf-8") as f:
            pr = f.read()
        with open(os.path.join(PROJECT_ROOT, "GEMINI.md"), encoding="utf-8") as f:
            guide = f.read()
        self.assertIn("Definition of Done", guide)
        for item in ("驗收標準全部勾選", "全部測試通過", "使用者 review", "文件已更新"):
            self.assertIn(item, guide)
            self.assertIn(item, pr)
        self.assertIn("Closes #", pr)


class TestCoreSecret(unittest.TestCase):
    def test_ticket_token_cannot_be_used_by_call_api(self):
        self.assertIn("TICKETS_GITHUB_TOKEN", http_tools._CORE_SECRETS)


if __name__ == "__main__":
    unittest.main()
