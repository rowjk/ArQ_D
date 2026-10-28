"""
Worktree 隔離：ArQ 執行資料夾與開發分開，一張單一個 git worktree

scope=core 派工時，RD 不再直接改 ArQ 執行中的資料夾 (保持在 main)，而是在
<ArQ>-worktrees/ticket-N (可用 ARQ_WORKTREES_DIR 指定上層目錄) 裡、分支 ticket-N-<slug> 上工作：
    - ensure_worktree：沒有就從 main 開；同一張單再次派工 (接續對話) 沿用原本的 worktree / 分支
    - remove_worktree：只刪乾淨的 worktree (被 .gitignore 忽略的檔案不算)；有未提交改動就保留
    - cleanup_after_merge：PR merge 後清掉對應 ticket 的 worktree (分支保留，GitHub 上已 merge)
"""

import os
import re
import subprocess
from typing import Any, Dict, List, Optional

from tools import ticket_tools
from tools.config import PROJECT_ROOT

BASE_BRANCH = "main"
GIT_TIMEOUT = 60
MAX_SLUG = 30


class WorktreeError(RuntimeError):
    """建立 worktree 失敗；呼叫端不得退回在執行資料夾裡開發"""


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)


def worktrees_dir(repo: str = PROJECT_ROOT) -> str:
    """worktree 的上層目錄：ARQ_WORKTREES_DIR，預設與 repo 同層的 <repo 名稱>-worktrees"""
    configured = os.getenv("ARQ_WORKTREES_DIR", "").strip()
    if configured:
        return configured
    repo = os.path.normpath(repo)
    return os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktrees")


def branch_name(number: int, title: str) -> str:
    """ticket-N-<slug>：取標題中的英數字詞 (中文標題沒有英數字時用 work)"""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    slug = "-".join(words)[:MAX_SLUG].strip("-") or "work"
    return f"ticket-{int(number)}-{slug}"


def _canonical(path: str) -> str:
    """完整長路徑：Windows 的 8.3 短路徑 (例如 GitHub Actions 的 C:\\Users\\RUNNER~1) 也展開，
    新建與沿用的 worktree 才會回傳同一個字串"""
    return os.path.realpath(path)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(_canonical(a)) == os.path.normcase(_canonical(b))


def _list_worktrees(repo: str) -> List[Dict[str, str]]:
    proc = _git(repo, "worktree", "list", "--porcelain")
    items, current = [], {}
    for line in proc.stdout.splitlines() + [""]:
        if not line.strip():
            if current:
                items.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = _canonical(line[len("worktree "):])
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line[len("branch refs/heads/"):]
    return items


def _ticket_branch(branch: str, number: int) -> bool:
    return branch == f"ticket-{int(number)}" or branch.startswith(f"ticket-{int(number)}-")


def find_worktree(number: int, repo: str = PROJECT_ROOT) -> Optional[Dict[str, str]]:
    """這張單的 worktree {"path", "branch"}；沒有回傳 None"""
    expected = os.path.join(worktrees_dir(repo), f"ticket-{int(number)}")
    for item in _list_worktrees(repo)[1:]:  # 第一筆是 repo 本身
        if _same_path(item.get("path", ""), expected) or _ticket_branch(item.get("branch", ""), number):
            return {"path": item["path"], "branch": item.get("branch", "")}
    return None


def _existing_branch(number: int, repo: str) -> Optional[str]:
    proc = _git(repo, "branch", "--list", "--format=%(refname:short)", f"ticket-{int(number)}*")
    names = [n.strip() for n in proc.stdout.splitlines() if _ticket_branch(n.strip(), number)]
    return names[0] if names else None


def ensure_worktree(number: int, title: str, repo: str = PROJECT_ROOT, base: str = BASE_BRANCH) -> Dict[str, Any]:
    """
    取得 (或建立) 這張單的 worktree，回傳 {"path", "branch", "created"}。
    已有 worktree 就沿用；已有 ticket-N-* 分支 (例如之前清掉 worktree) 就用該分支；否則從 base 開新分支。
    失敗丟 WorktreeError。
    """
    if os.getenv("ARQ_TESTING") and _same_path(repo, PROJECT_ROOT):
        # 單元測試不得在 ArQ 本身建立 worktree / 分支 (tests/netguard.py 會設定 ARQ_TESTING)；請 mock 或用暫存 repo
        raise WorktreeError("測試中不得在 ArQ 本身建立 worktree，請 mock ensure_worktree 或改用暫存 repo。")
    found = find_worktree(number, repo)
    if found:
        return {**found, "created": False}
    _git(repo, "worktree", "prune")  # 清掉資料夾已被刪除的殘留登記
    path = os.path.join(worktrees_dir(repo), f"ticket-{int(number)}")
    if os.path.exists(path) and os.listdir(path):
        raise WorktreeError(f"{path} 已存在但不是這個 repo 的 worktree，請先確認後手動移除。")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    branch = _existing_branch(number, repo)
    if branch:
        proc = _git(repo, "worktree", "add", path, branch)
    else:
        branch = branch_name(number, title)
        proc = _git(repo, "worktree", "add", "-b", branch, path, base)
    if proc.returncode != 0:
        raise WorktreeError(f"建立 worktree 失敗：{(proc.stderr or proc.stdout).strip()[:300]}")
    return {"path": _canonical(path), "branch": branch, "created": True}


def remove_worktree(number: int, repo: str = PROJECT_ROOT) -> Dict[str, Any]:
    """
    清除這張單的 worktree (不加 --force)：有未提交或未追蹤的檔案時保留並說明。分支保留。
    回傳 {"success", "removed", "message"[, "path"]}
    """
    found = find_worktree(number, repo)
    if not found:
        return {"success": True, "removed": False, "message": f"#{number} 沒有 worktree。"}
    proc = _git(repo, "worktree", "remove", found["path"])
    if proc.returncode == 0:
        return {"success": True, "removed": True, "path": found["path"],
                "message": f"🧹 已清除 #{number} 的 worktree（{found['path']}）。"}
    return {"success": False, "removed": False, "path": found["path"],
            "message": f"⚠️ #{number} 的 worktree 有未提交的改動，已保留：{found['path']}（分支 {found['branch']}）。"
                       f"確認不需要後可手動執行 git worktree remove --force。"}


def cleanup_after_merge(pr_number: int, repo: str = PROJECT_ROOT) -> List[str]:
    """PR merge 後清除對應 ticket 的 worktree；PR 未 merge 或讀不到時不動。回傳要通知使用者的訊息"""
    pr = ticket_tools.pr_merge_summary(int(pr_number))
    if not pr.get("success") or not pr.get("merged"):
        return []
    return [remove_worktree(int(n), repo=repo)["message"] for n in pr.get("tickets") or []]
