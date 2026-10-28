"""
子單改狀態時同步母單 (事件：子單開工後母單一直停在 todo，要使用者叫 PM 手動移)

規則 (只往前推，不往回拉；同步失敗不影響子單)：
    - 子單改為 in-progress / review / blocked：母單是 todo 就改 in-progress
    - 子單改為 done 且其他子單都已關閉：母單改 review 並留言請使用者確認
    - 母單沒有 PR：所有子單都已關閉時可以設 done (取代「需要已 merge 的 PR」)
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外
from tests.test_ticket_tools import FakeGitHub, issue, run_with  # noqa: E402

from tools import ticket_tools as tt  # noqa: E402

P = "/repos/owner/repo"
PARENT_URL = f"https://api.github.com{P}/issues/9"
MERGED_TIMELINE = [{"event": "cross-referenced", "source": {"issue": {
    "number": 40, "body": "Closes #7", "pull_request": {"merged_at": "2026-10-28T05:00:00Z"}}}}]


def labels_put(body):
    return 200, [{"name": n} for n in body["labels"]]


def fake(child, parent=None, subs=None, parent_status=200, timeline=None):
    """child =、parent =；路由以前綴比對，較長的路徑放前面"""
    routes = {
        ("GET", f"{P}/issues/9/sub_issues"): (200, subs or []),
        ("GET", f"{P}/issues/9/timeline"): (200, []),
        ("PUT", f"{P}/issues/9/labels"): labels_put,
        ("POST", f"{P}/issues/9/comments"): (201, {"id": 2}),
        ("GET", f"{P}/issues/9"): (parent_status, parent if parent_status == 200 else {"message": "Server Error"}),
        ("GET", f"{P}/issues/7/timeline"): (200, timeline or []),
        ("PUT", f"{P}/issues/7/labels"): labels_put,
        ("POST", f"{P}/issues/7/comments"): (201, {"id": 1}),
        ("PATCH", f"{P}/issues/7"): lambda body: (200, issue(7, state=body.get("state", "open"))),
        ("GET", f"{P}/issues/7"): (200, child),
    }
    return FakeGitHub(routes)


def child(status="todo", state="open"):
    return issue(7, labels=("by:pm", f"status:{status}"), state=state, parent_issue_url=PARENT_URL)


def parent(status="todo", state="open", subs_total=2):
    return issue(9, labels=("by:pm", f"status:{status}"), state=state,
                 sub_issues_summary={"total": subs_total, "completed": 0})


def parent_labels(f):
    puts = f.find("PUT", f"{P}/issues/9/labels")
    return set(puts[0]["body"]["labels"]) if puts else None


class TestChildStartsParent(unittest.TestCase):
    def test_child_in_progress_moves_todo_parent(self):
        f = fake(child(), parent())
        res = run_with(f, tt.update_ticket, 7, status="in-progress")
        self.assertTrue(res["success"])
        self.assertEqual(parent_labels(f), {"by:pm", "status:in-progress"})
        self.assertEqual(res["parent_status"], {"number": 9, "status": "in-progress"})

    def test_child_review_or_blocked_also_starts_parent(self):
        for status in ("review", "blocked"):
            f = fake(child("in-progress"), parent())
            run_with(f, tt.update_ticket, 7, status=status)
            self.assertEqual(parent_labels(f), {"by:pm", "status:in-progress"}, status)

    def test_parent_already_moving_is_not_touched(self):
        for status in ("in-progress", "review", "blocked", "done"):
            f = fake(child(), parent(status))
            res = run_with(f, tt.update_ticket, 7, status="in-progress")
            self.assertTrue(res["success"])
            self.assertIsNone(parent_labels(f), status)
            self.assertNotIn("parent_status", res)

    def test_child_back_to_todo_does_not_pull_parent_back(self):
        f = fake(child("in-progress"), parent("in-progress"))
        run_with(f, tt.update_ticket, 7, status="todo")
        self.assertEqual(f.find("GET", f"{P}/issues/9"), [])

    def test_no_parent_no_extra_calls(self):
        f = fake(issue(7, labels=("by:pm", "status:todo")), parent())
        run_with(f, tt.update_ticket, 7, status="in-progress")
        self.assertEqual([c for c in f.calls if "/issues/9" in c["url"]], [])

    def test_comment_only_does_not_touch_parent(self):
        f = fake(child(), parent())
        run_with(f, tt.update_ticket, 7, comment="進度")
        self.assertEqual([c for c in f.calls if "/issues/9" in c["url"]], [])

    def test_closed_parent_is_not_touched(self):
        f = fake(child(), parent("done", state="closed"))
        run_with(f, tt.update_ticket, 7, status="in-progress")
        self.assertIsNone(parent_labels(f))

    def test_parent_failure_does_not_fail_child(self):
        f = fake(child(), parent_status=500)
        res = run_with(f, tt.update_ticket, 7, status="in-progress")
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "in-progress")
        self.assertIn("#9", res["parent_note"])


class TestChildrenFinishParent(unittest.TestCase):
    def test_last_child_done_moves_parent_to_review(self):
        subs = [issue(7, labels=("status:review",), state="open"), issue(8, labels=("status:done",), state="closed")]
        f = fake(child("review"), parent("in-progress"), subs=subs, timeline=MERGED_TIMELINE)
        res = run_with(f, tt.update_ticket, 7, status="done")
        self.assertTrue(res["success"])
        self.assertEqual(parent_labels(f), {"by:pm", "status:review"})
        self.assertEqual(res["parent_status"], {"number": 9, "status": "review"})
        note = f.find("POST", f"{P}/issues/9/comments")[0]["body"]["body"]
        self.assertIn("子單全部完成", note)

    def test_open_sibling_keeps_parent(self):
        subs = [issue(7, labels=("status:review",)), issue(8, labels=("status:todo",))]
        f = fake(child("review"), parent("in-progress"), subs=subs, timeline=MERGED_TIMELINE)
        res = run_with(f, tt.update_ticket, 7, status="done")
        self.assertTrue(res["success"])
        self.assertIsNone(parent_labels(f))
        self.assertEqual(f.find("POST", f"{P}/issues/9/comments"), [])

    def test_parent_already_in_review_not_commented_again(self):
        subs = [issue(7, state="closed"), issue(8, state="closed")]
        f = fake(child("review", state="closed"), parent("review"), subs=subs, timeline=MERGED_TIMELINE)
        run_with(f, tt.update_ticket, 7, status="done")
        self.assertIsNone(parent_labels(f))
        self.assertEqual(f.find("POST", f"{P}/issues/9/comments"), [])


class TestParentDoneGate(unittest.TestCase):
    """母單沒有 PR：子單全部關閉時可以設 done；有子單沒關就照舊擋下"""

    def _parent_fake(self, subs):
        return FakeGitHub({
            ("GET", f"{P}/issues/9/sub_issues"): (200, subs),
            ("GET", f"{P}/issues/9/timeline"): (200, []),
            ("PUT", f"{P}/issues/9/labels"): labels_put,
            ("PATCH", f"{P}/issues/9"): lambda body: (200, issue(9, state=body.get("state", "open"))),
            ("GET", f"{P}/issues/9"): (200, parent("review")),
        })

    def test_parent_done_when_all_subs_closed(self):
        f = self._parent_fake([issue(10, state="closed"), issue(11, state="closed")])
        res = run_with(f, tt.update_ticket, 9, status="done", close=True)
        self.assertTrue(res["success"], res)
        self.assertTrue(res["sub_issues_done"])
        self.assertEqual(f.find("PATCH", f"{P}/issues/9")[0]["body"]["state"], "closed")

    def test_parent_done_blocked_by_open_sub(self):
        f = self._parent_fake([issue(10, state="closed"), issue(11, state="open")])
        res = run_with(f, tt.update_ticket, 9, status="done")
        self.assertFalse(res["success"])
        self.assertIn("子單", res["error"])
        self.assertEqual(f.find("PUT", f"{P}/issues/9/labels"), [])

    def test_ticket_without_subs_still_needs_pr(self):
        f = fake(issue(7, labels=("status:review",)))
        res = run_with(f, tt.update_ticket, 7, status="done")
        self.assertFalse(res["success"])
        self.assertEqual([c for c in f.calls if "sub_issues" in c["url"]], [])


if __name__ == "__main__":
    unittest.main()
