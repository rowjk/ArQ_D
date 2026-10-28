"""
 PR merge 守門 (不連網，mock 底層送出函式與確認回呼)

事件：PM 開 PR 後 32 秒就 merge (0 個 review)，並在 merge 前就把 ticket 關成 done。原因：
    - classify_risk 抓不到 gh pr merge，merge 夾帶在「Git 提交、推送」那張卡裡過關
    - ticket 工具允許 Gemini 隨時 close=true / status=done

驗收標準：
    - risk_policy 偵測 gh pr merge / git merge / gh api .../merge，理由「合併到 main」
    - merge 必須單獨一張卡：同一段指令還有其他操作時直接拒絕，不跳卡
    - merge 確認卡顯示 PR 編號、標題、連結、對應 ticket、review 數量；0 個 review 加註警告
    - ticket：只有對應 PR 已 merge 才能 done / close；not planned 關單要確認卡
    - SYSTEM_INSTRUCTION 規則 9 改為 PR 流程：使用者明確說 merge 才 merge
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tests.test_ticket_tools import ENV, FakeGitHub, issue, run_with  # noqa: E402
from tools import http_tools  # noqa: E402
from tools import ticket_tools as tt  # noqa: E402
from tools.risk_policy import (  # noqa: E402
    MERGE_REASON, classify_risk, extract_pr_number, looks_like_merge, merge_violation,
)


class TestMergeDetection(unittest.TestCase):
    def test_merge_commands_detected(self):
        for cmd in [
            "gh pr merge 28", "gh pr merge 28 --squash --delete-branch", "GH PR MERGE 28",
            "gh pr merge https://github.com/owner/repo/pull/28", "git merge feature/x",
            "git merge --no-ff origin/ticket-3", "gh api -X PUT repos/owner/repo/pulls/28/merge",
            "& 'C:\\Program Files\\GitHub CLI\\gh.exe' pr merge 28",
        ]:
            with self.subTest(cmd=cmd):
                self.assertTrue(looks_like_merge(cmd))
                self.assertIn(MERGE_REASON, classify_risk(cmd, "shell"))

    def test_non_merge_commands(self):
        for cmd in ["git merge-base main HEAD", "gh pr view 28", "gh pr list", "gh pr create --fill",
                    "echo merge", "git log --merges", "git status"]:
            with self.subTest(cmd=cmd):
                self.assertFalse(looks_like_merge(cmd))

    def test_extract_pr_number(self):
        self.assertEqual(extract_pr_number("gh pr merge 28 --squash"), 28)
        self.assertEqual(extract_pr_number("gh pr merge https://github.com/o/r/pull/31"), 31)
        self.assertEqual(extract_pr_number("gh api -X PUT repos/o/r/pulls/12/merge"), 12)
        self.assertIsNone(extract_pr_number("git merge feature/x"))
        self.assertIsNone(extract_pr_number("gh pr merge"))


class TestMergeMustBeAlone(unittest.TestCase):
    def test_single_merge_allowed(self):
        for cmd in ["gh pr merge 28 --squash", "  gh pr merge 28  ", "gh pr merge 28 --squash --delete-branch"]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(merge_violation(cmd, "shell"))

    def test_merge_combined_with_other_commands_rejected(self):
        for cmd in [
            "git add . && git commit -m x && git push && gh pr merge 28",
            "gh pr merge 28; git checkout main",
            "git push\ngh pr merge 28",
            "gh pr merge 28 | Out-Null",
            "gh pr create --fill; gh pr merge 28",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIn("單獨", merge_violation(cmd, "shell"))

    def test_merge_in_python_rejected(self):
        code = 'import subprocess\nsubprocess.run(["gh", "pr", "merge", "28"])'
        self.assertIn("run_powershell_sync", merge_violation(code, "python"))

    def test_no_merge_no_violation(self):
        self.assertIsNone(merge_violation("git add . && git commit -m x", "shell"))
        # commit 訊息裡提到 pr merge 不算 merge
        cmd = 'git add . && git commit -m "fix: pr merge 守門" && git push'
        self.assertIsNone(merge_violation(cmd, "shell"))
        self.assertNotIn(MERGE_REASON, classify_risk(cmd, "shell"))
        self.assertIsNone(merge_violation("print('hi')", "python"))

    def test_call_api_merge_endpoint(self):
        reason = http_tools.call_api_risk({"method": "PUT", "url": "https://api.github.com/repos/o/r/pulls/5/merge"})
        self.assertIn(MERGE_REASON, reason)
        self.assertNotIn(MERGE_REASON, http_tools.call_api_risk({"method": "POST", "url": "https://api.github.com/repos/o/r/issues"}))


_LOG_CASE = unittest.TestCase()   # 只借用 assertLogs（run_tool 不在 TestCase 內）


def run_tool(tool_name, args, confirm, summary=None):
    """執行 agent._execute_tool；不真的執行指令，記錄 confirm 收到的內容"""
    import agent
    executed = []
    spec = agent.TOOL_REGISTRY[tool_name]
    fake_spec = spec.__class__(func=lambda **kw: executed.append(kw) or {"success": True}, label=spec.label,
                               risk=spec.risk, guard=spec.guard)
    a = agent.ArQAgent(api_key="")
    with patch.dict(agent.TOOL_REGISTRY, {tool_name: fake_spec}), \
            patch.object(tt, "pr_merge_summary", return_value=summary or {"success": False, "error": "x"}), \
            _LOG_CASE.assertLogs(level="INFO"):
        result, _ = asyncio.run(a._execute_tool(tool_name, args, confirm, None))
    return result, executed


class TestAgentMergeGate(unittest.TestCase):
    def _confirm(self, answer=True):
        cards = []

        async def confirm(title, detail):
            cards.append((title, detail))
            return answer
        return confirm, cards

    def test_combined_command_refused_without_card(self):
        confirm, cards = self._confirm()
        result, executed = run_tool("run_powershell_sync",
                                    {"command": "git commit -m x && git push && gh pr merge 28"}, confirm)
        self.assertEqual(cards, [])
        self.assertEqual(executed, [])
        self.assertIn("單獨", result["error"])

    def test_single_merge_gets_its_own_card_with_pr_details(self):
        confirm, cards = self._confirm()
        summary = {"success": True, "number": 28, "title": "精簡工具", "url": "https://github.com/o/r/pull/28",
                   "tickets": [15], "reviews": 2, "approved": 1, "merged": False, "state": "open"}
        result, executed = run_tool("run_powershell_sync", {"command": "gh pr merge 28 --squash"}, confirm, summary)
        self.assertEqual(len(cards), 1)
        title, detail = cards[0]
        self.assertIn(MERGE_REASON, title)
        for text in ("#28", "精簡工具", "https://github.com/o/r/pull/28", "#15", "2"):
            self.assertIn(text, detail)
        self.assertEqual(len(executed), 1)

    def test_zero_reviews_warned(self):
        confirm, cards = self._confirm()
        summary = {"success": True, "number": 28, "title": "t", "url": "u", "tickets": [], "reviews": 0,
                   "approved": 0, "merged": False, "state": "open"}
        run_tool("run_powershell_sync", {"command": "gh pr merge 28"}, confirm, summary)
        self.assertIn("沒有任何 review", cards[0][1])

    def test_reviews_unknown_warned(self):
        confirm, cards = self._confirm()
        summary = {"success": True, "number": 28, "title": "t", "url": "u", "tickets": [15], "reviews": None,
                   "approved": None, "merged": False, "state": "open"}
        run_tool("run_powershell_sync", {"command": "gh pr merge 28"}, confirm, summary)
        self.assertIn("無法取得", cards[0][1])
        self.assertIn("看不到 review", cards[0][1])

    def test_pr_info_unavailable_still_asks(self):
        confirm, cards = self._confirm(answer=False)
        result, executed = run_tool("run_powershell_sync", {"command": "gh pr merge 28"}, confirm)
        self.assertIn("無法取得 PR", cards[0][1])
        self.assertEqual(executed, [])
        self.assertIn("error", result)

    def test_pr_info_forbidden_hints_token_permission(self):
        """fine-grained token 只開 Issues 時讀 PR 會 403：卡片要說清楚缺哪個權限 (事件)"""
        confirm, cards = self._confirm(answer=False)
        summary = {"success": False, "error": "GitHub API 錯誤 403：Resource not accessible by personal access token"}
        run_tool("run_powershell_sync", {"command": "gh pr merge 28"}, confirm, summary)
        self.assertIn("Pull requests", cards[0][1])
        self.assertIn("TICKETS_GITHUB_TOKEN", cards[0][1])

    def test_python_merge_refused(self):
        confirm, cards = self._confirm()
        result, executed = run_tool("execute_python_code",
                                    {"code": 'import os\nos.system("gh pr merge 28")'}, confirm)
        self.assertEqual(cards, [])
        self.assertEqual(executed, [])


def merged_pr_event(issue_number, pr_number=40, merged=True, keyword="Closes"):
    return {"event": "cross-referenced", "source": {"type": "issue", "issue": {
        "number": pr_number, "body": f"{keyword} #{issue_number}\n\n說明",
        "pull_request": {"merged_at": "2026-10-19T05:00:00Z" if merged else None}}}}


class TestTicketDoneRule(unittest.TestCase):
    def _fake(self, current, timeline=()):
        # timeline 路徑放前面：FakeGitHub 用前綴比對，/issues/7 也會比對到 /issues/7/timeline
        return FakeGitHub({
            ("GET", "/repos/owner/repo/issues/7/timeline"): (200, list(timeline)),
            ("GET", "/repos/owner/repo/issues/7"): (200, current),
            ("PUT", "/repos/owner/repo/issues/7/labels"): lambda body: (200, [{"name": n} for n in body["labels"]]),
            ("POST", "/repos/owner/repo/issues/7/comments"): (201, {"id": 1}),
            ("PATCH", "/repos/owner/repo/issues/7"): lambda body: (200, issue(7, state=body.get("state", "open"))),
        })

    def test_close_without_merged_pr_refused(self):
        fake = self._fake(issue(7, labels=("by:pm", "status:review")), [merged_pr_event(7, merged=False)])
        res = run_with(fake, tt.update_ticket, 7, close=True)
        self.assertFalse(res["success"])
        self.assertIn("merge", res["error"])
        self.assertEqual(fake.find("PUT", "/repos/owner/repo/issues/7/labels"), [])
        self.assertEqual(fake.find("PATCH", "/repos/owner/repo/issues/7"), [])

    def test_status_done_without_merged_pr_refused(self):
        fake = self._fake(issue(7, labels=("by:pm", "status:review")))
        res = run_with(fake, tt.update_ticket, 7, status="done")
        self.assertFalse(res["success"])
        self.assertEqual(fake.find("PUT", "/repos/owner/repo/issues/7/labels"), [])

    def test_mention_without_closing_keyword_does_not_count(self):
        fake = self._fake(issue(7, labels=("by:pm", "status:review")), [merged_pr_event(7, keyword="Refs")])
        res = run_with(fake, tt.update_ticket, 7, close=True)
        self.assertFalse(res["success"])

    def test_close_with_merged_pr_allowed(self):
        fake = self._fake(issue(7, labels=("by:pm", "status:review")), [merged_pr_event(7, pr_number=40)])
        res = run_with(fake, tt.update_ticket, 7, close=True)
        self.assertTrue(res["success"])
        self.assertEqual(res["merged_pr"], 40)
        labels = set(fake.find("PUT", "/repos/owner/repo/issues/7/labels")[0]["body"]["labels"])
        self.assertIn("status:done", labels)
        self.assertEqual(fake.find("PATCH", "/repos/owner/repo/issues/7")[0]["body"]["state"], "closed")

    def test_auto_closed_by_merge_label_synced_to_done(self):
        # PR 內文 Closes → GitHub merge 時自動關單，但 label 還停在 review；之後設 done 只改 label、不重開
        fake = self._fake(issue(7, state="closed", labels=("by:pm", "status:review")), [merged_pr_event(7)])
        res = run_with(fake, tt.update_ticket, 7, status="done")
        self.assertTrue(res["success"])
        labels = set(fake.find("PUT", "/repos/owner/repo/issues/7/labels")[0]["body"]["labels"])
        self.assertIn("status:done", labels)
        self.assertNotIn("status:review", labels)
        for call in fake.find("PATCH", "/repos/owner/repo/issues/7"):
            self.assertNotEqual(call["body"].get("state"), "open")

    def test_not_planned_close_without_pr(self):
        fake = self._fake(issue(7, labels=("by:pm", "status:todo")))
        res = run_with(fake, tt.update_ticket, 7, close=True, close_reason="not_planned", comment="不做了")
        self.assertTrue(res["success"])
        patch_call = fake.find("PATCH", "/repos/owner/repo/issues/7")[0]
        self.assertEqual(patch_call["body"], {"state": "closed", "state_reason": "not_planned"})
        self.assertEqual(fake.find("PUT", "/repos/owner/repo/issues/7/labels"), [])

    def test_invalid_close_reason_rejected(self):
        fake = self._fake(issue(7))
        res = run_with(fake, tt.update_ticket, 7, close=True, close_reason="whatever")
        self.assertFalse(res["success"])
        self.assertEqual(fake.calls, [])


class TestTicketToolRisk(unittest.TestCase):
    def test_not_planned_needs_card_others_do_not(self):
        import agent
        risk = agent.TOOL_REGISTRY["ticket"].risk
        self.assertIsNotNone(risk)
        self.assertTrue(risk({"action": "update", "number": 7, "close": True, "close_reason": "not_planned"}))
        for args in ({"action": "list"}, {"action": "create", "title": "t"},
                     {"action": "update", "number": 7, "status": "review"},
                     {"action": "update", "number": 7, "close": True}):
            with self.subTest(args=args):
                self.assertIsNone(risk(args))

    def test_tool_passes_close_reason(self):
        with patch.object(tt, "update_ticket", return_value={"success": True}) as u:
            tt.ticket("update", number=7, close=True, close_reason="not_planned")
        u.assert_called_once_with(7, status="", comment="", close=True, close_reason="not_planned", body="")


class TestPrMergeSummary(unittest.TestCase):
    PR_AS_ISSUE = {"number": 28, "title": "精簡工具", "html_url": "https://github.com/owner/repo/pull/28",
                   "body": "Closes #15\nfixes #16", "state": "open", "pull_request": {"merged_at": None}}

    def test_summary_fields(self):
        # 基本資料走 issues 端點 (只需 Issues 權限)
        fake = FakeGitHub({
            ("GET", "/repos/owner/repo/pulls/28/reviews"): (200, [{"state": "APPROVED"}, {"state": "COMMENTED"}]),
            ("GET", "/repos/owner/repo/issues/28"): (200, self.PR_AS_ISSUE),
        })
        res = run_with(fake, tt.pr_merge_summary, 28)
        self.assertTrue(res["success"])
        self.assertEqual(res["tickets"], [15, 16])
        self.assertEqual(res["reviews"], 2)
        self.assertEqual(res["approved"], 1)
        self.assertFalse(res["merged"])
        self.assertEqual(res["url"], "https://github.com/owner/repo/pull/28")

    def test_reviews_unreadable_is_reported_not_fatal(self):
        fake = FakeGitHub({
            ("GET", "/repos/owner/repo/pulls/28/reviews"): (403, {"message": "Resource not accessible"}),
            ("GET", "/repos/owner/repo/issues/28"): (200, self.PR_AS_ISSUE),
        })
        res = run_with(fake, tt.pr_merge_summary, 28)
        self.assertTrue(res["success"])
        self.assertIsNone(res["reviews"])

    def test_plain_issue_rejected(self):
        fake = FakeGitHub({("GET", "/repos/owner/repo/issues/7"): (200, issue(7))})
        self.assertFalse(run_with(fake, tt.pr_merge_summary, 7)["success"])

    def test_summary_failure_is_soft(self):
        res = run_with(FakeGitHub(), tt.pr_merge_summary, 99)
        self.assertFalse(res["success"])


class TestRule9(unittest.TestCase):
    def test_system_instruction_describes_pr_flow(self):
        import agent
        text = agent.SYSTEM_INSTRUCTION
        for phrase in ("gh pr create", "Closes #", "review", "明確說「merge」"):
            self.assertIn(phrase, text)
        self.assertNotIn("執行 git add 與 git commit（系統會再跳卡片確認）；工程師不得自行 commit。改動需重啟", text)


if __name__ == "__main__":
    unittest.main()
