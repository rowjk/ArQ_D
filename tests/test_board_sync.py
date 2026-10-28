"""
 ticket 狀態變更同步到 ArQ Board 欄位 (Projects v2 Status)。不連網 (mock _send)。

驗收標準：
    - update_ticket 改狀態時，同步把看板上該單的 Status 設成對應選項
    - create_ticket 開單後確保在看板上並設好初始 Status (不在看板上就先加入)
    - 設定走 .env：TICKETS_PROJECT (owner/編號) 與 TICKETS_PROJECT_TOKEN；未設定時維持只改 label，不報錯
    - 欄位與選項 ID 以名稱查詢後快取，不寫死
    - 看板更新失敗不影響 label 更新：結果附 board_note
    - 新 token 列入 http_tools._CORE_SECRETS；輸出不含 token
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

from tests.test_ticket_tools import ENV, FakeGitHub, issue  # noqa: E402
from tools import http_tools  # noqa: E402
from tools import ticket_tools as tt  # noqa: E402

BOARD_TOKEN = "ghp_BOARDFAKE0000111122223333"
BOARD_ENV = {**ENV, "TICKETS_PROJECT": "owner/2", "TICKETS_PROJECT_TOKEN": BOARD_TOKEN}
META = {"data": {"user": {"projectV2": {"id": "PVT_1", "field": {"id": "FIELD_STATUS", "options": [
    {"id": "opt-todo", "name": "Todo"}, {"id": "opt-prog", "name": "In Progress"}, {"id": "opt-block", "name": "Blocked"},
    {"id": "opt-review", "name": "Review"}, {"id": "opt-done", "name": "Done"}]}}}}}


class FakeBoard(FakeGitHub):
    """REST 照 FakeGitHub 的路由；/graphql 依查詢內容回應，並記錄每次 GraphQL 呼叫"""

    def __init__(self, routes=None, on_board=True, fail_set=False):
        super().__init__(routes)
        self.on_board, self.fail_set = on_board, fail_set
        self.gql = []

    def __call__(self, method, url, headers, data):
        if url.endswith("/graphql"):
            body = json.loads(data.decode("utf-8"))
            q = body["query"]
            self.gql.append((q, body.get("variables") or {}, headers))
            if "projectV2(number" in q:
                return 200, META
            if "projectItems" in q:
                nodes = [{"id": "ITEM_7", "project": {"id": "PVT_1"}}] if self.on_board else []
                return 200, {"data": {"node": {"projectItems": {"nodes": nodes}}}}
            if "addProjectV2ItemById" in q:
                return 200, {"data": {"addProjectV2ItemById": {"item": {"id": "ITEM_NEW"}}}}
            if "updateProjectV2ItemFieldValue" in q:
                if self.fail_set:
                    return 200, {"errors": [{"message": f"Resource not accessible {BOARD_TOKEN}"}]}
                return 200, {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}}
            return 404, {"message": "unknown query"}
        return super().__call__(method, url, headers, data)

    def kinds(self):
        out = []
        for q, _, _ in self.gql:
            for key in ("projectV2(number", "projectItems", "addProjectV2ItemById", "updateProjectV2ItemFieldValue"):
                if key in q:
                    out.append(key)
        return out


def update_routes():
    return {
        ("GET", "/repos/owner/repo/issues/7"): (200, issue(7, labels=("by:pm", "status:todo"), node_id="I_7")),
        ("PUT", "/repos/owner/repo/issues/7/labels"): lambda body: (200, [{"name": n} for n in body["labels"]]),
        ("POST", "/repos/owner/repo/issues/7/comments"): (201, {"id": 1}),
    }


def run(fake, func, *args, env=BOARD_ENV, **kwargs):
    tt._reset_board_cache()
    with patch.dict(os.environ, env, clear=False), patch.object(tt, "_send", fake):
        return func(*args, **kwargs)


class TestUpdateSync(unittest.TestCase):
    def test_status_change_moves_board_column(self):
        fake = FakeBoard(update_routes())
        res = run(fake, tt.update_ticket, 7, status="review")
        self.assertTrue(res["success"])
        self.assertNotIn("board_note", res)
        self.assertEqual(fake.kinds(), ["projectV2(number", "projectItems", "updateProjectV2ItemFieldValue"])
        _, variables, headers = fake.gql[-1]
        self.assertEqual(variables["option"], "opt-review")
        self.assertEqual(variables["item"], "ITEM_7")
        self.assertEqual(headers["Authorization"], f"Bearer {BOARD_TOKEN}")   # 看板用自己的 token

    def test_ticket_not_on_board_is_added_first(self):
        fake = FakeBoard(update_routes(), on_board=False)
        run(fake, tt.update_ticket, 7, status="in-progress")
        self.assertEqual(fake.kinds(), ["projectV2(number", "projectItems", "addProjectV2ItemById",
                                        "updateProjectV2ItemFieldValue"])
        self.assertEqual(fake.gql[-1][1]["item"], "ITEM_NEW")
        self.assertEqual(fake.gql[-1][1]["option"], "opt-prog")

    def test_board_failure_does_not_break_label_update(self):
        fake = FakeBoard(update_routes(), fail_set=True)
        res = run(fake, tt.update_ticket, 7, status="blocked")
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "blocked")
        self.assertIn("看板", res["board_note"])
        self.assertNotIn(BOARD_TOKEN, json.dumps(res, ensure_ascii=False))

    def test_comment_only_does_not_touch_board(self):
        fake = FakeBoard(update_routes())
        run(fake, tt.update_ticket, 7, comment="開工")
        self.assertEqual(fake.gql, [])

    def test_not_configured_keeps_label_only_but_warns(self):
        fake = FakeBoard(update_routes())
        env = {**ENV, "TICKETS_PROJECT": "", "TICKETS_PROJECT_TOKEN": ""}
        with self.assertLogs("arq.ticket", level="WARNING") as logs:
            res = run(fake, tt.update_ticket, 7, status="review", env=env)
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "review")
        self.assertEqual(fake.gql, [])
        self.assertIn("TICKETS_PROJECT", res["board_note"])
        self.assertIn("跳過", res["board_note"])
        self.assertTrue(any("TICKETS_PROJECT" in m for m in logs.output))

    def test_only_token_missing_project_is_reported(self):
        fake = FakeBoard(update_routes())
        env = {**ENV, "TICKETS_PROJECT": "owner/2", "TICKETS_PROJECT_TOKEN": ""}
        with self.assertLogs("arq.ticket", level="WARNING"):
            res = run(fake, tt.update_ticket, 7, status="review", env=env)
        self.assertTrue(res["success"])
        self.assertIn("TICKETS_PROJECT_TOKEN", res["board_note"])
        self.assertEqual(fake.gql, [])

    def test_board_failure_is_logged_without_token(self):
        fake = FakeBoard(update_routes(), fail_set=True)
        with self.assertLogs("arq.ticket", level="WARNING") as logs:
            run(fake, tt.update_ticket, 7, status="blocked")
        self.assertTrue(any("看板同步失敗" in m for m in logs.output))
        self.assertNotIn(BOARD_TOKEN, "\n".join(logs.output))

    def test_success_does_not_warn(self):
        fake = FakeBoard(update_routes())
        with self.assertNoLogs("arq.ticket", level="WARNING"):
            res = run(fake, tt.update_ticket, 7, status="review")
        self.assertNotIn("board_note", res)

    def test_comment_only_does_not_warn_when_not_configured(self):
        fake = FakeBoard(update_routes())
        env = {**ENV, "TICKETS_PROJECT": "", "TICKETS_PROJECT_TOKEN": ""}
        with self.assertNoLogs("arq.ticket", level="WARNING"):
            res = run(fake, tt.update_ticket, 7, comment="開工", env=env)
        self.assertNotIn("board_note", res)

    def test_bad_project_format_reported_without_calls(self):
        fake = FakeBoard(update_routes())
        res = run(fake, tt.update_ticket, 7, status="review", env={**BOARD_ENV, "TICKETS_PROJECT": "owner"})
        self.assertTrue(res["success"])
        self.assertIn("TICKETS_PROJECT", res["board_note"])
        self.assertEqual(fake.gql, [])

    def test_meta_is_cached(self):
        fake = FakeBoard(update_routes())
        tt._reset_board_cache()
        with patch.dict(os.environ, BOARD_ENV, clear=False), patch.object(tt, "_send", fake):
            tt.update_ticket(7, status="review")
            tt.update_ticket(7, status="blocked")
        self.assertEqual(fake.kinds().count("projectV2(number"), 1)


class TestCreateSync(unittest.TestCase):
    def test_new_ticket_added_with_initial_status(self):
        fake = FakeBoard({("POST", "/repos/owner/repo/issues"): (201, {
            "number": 70, "html_url": "https://github.com/owner/repo/issues/70", "node_id": "I_70"})},
            on_board=False)
        res = run(fake, tt.create_ticket, title="想法", body="內容", opened_by="user", status="in-progress")
        self.assertTrue(res["success"])
        self.assertIn("addProjectV2ItemById", fake.kinds())
        self.assertEqual(fake.gql[-1][1]["option"], "opt-prog")


    def test_create_not_configured_warns_with_note(self):
        fake = FakeBoard({("POST", "/repos/owner/repo/issues"): (201, {
            "number": 71, "html_url": "https://github.com/owner/repo/issues/71", "node_id": "I_71"})})
        env = {**ENV, "TICKETS_PROJECT": "", "TICKETS_PROJECT_TOKEN": ""}
        with self.assertLogs("arq.ticket", level="WARNING"):
            res = run(fake, tt.create_ticket, title="想法", body="內容", opened_by="user", env=env)
        self.assertTrue(res["success"])
        self.assertIn("TICKETS_PROJECT", res["board_note"])
        self.assertEqual(fake.gql, [])


class TestPromptRule(unittest.TestCase):
    def test_pr_rule_names_review_status_update(self):
        import agent
        self.assertIn("status='review'", agent.SYSTEM_INSTRUCTION)
        self.assertIn("看板", agent.SYSTEM_INSTRUCTION)


class TestSecrets(unittest.TestCase):
    def test_board_token_is_core_secret(self):
        self.assertIn("TICKETS_PROJECT_TOKEN", http_tools._CORE_SECRETS)

    def test_status_option_names_cover_all_statuses(self):
        self.assertEqual(set(tt.BOARD_STATUS_OPTIONS), set(tt.STATUSES))


if __name__ == "__main__":
    unittest.main()
