"""
Ticket 管理 (GitHub Issues)：讓 ArQ 的 PM / RD 開單、查單、改單，需求與進度長期留存

Ticket 放在 .env 的 TICKETS_REPO (例如 owner/ArQ) 的 Issues；看板 (GitHub Projects) 由使用者在網頁上操作。

安全設計 (修改時必須維持)：
    1. 金鑰：TICKETS_GITHUB_TOKEN 建議用 fine-grained PAT，只授權該 repo 的 Issues 讀寫與 Pull requests 唯讀 (merge 確認卡讀 PR)。
       只走 Authorization Header、只送 https://api.github.com、不跟隨重新導向；
       名稱結尾為 _TOKEN，secret_guard 會自動從子程序環境移除；並列入 http_tools._CORE_SECRETS，call_api 不能注入。
    2. 開單者 (opened_by) 由呼叫端程式決定，只接受 OPENERS 中的角色；給 Gemini 的工具不得開放這個參數，
       避免 AI 冒充其他角色。可信紀錄是內文第一行的 metadata，label 只供顯示與篩選。
    3. 寫入 GitHub 前一律 redact()；回傳結果經 redact_obj()；任何錯誤回傳 success=False，不拋例外。
    4. 狀態以 GitHub 為準：改狀態前先讀目前 label，只替換 status:*；已關閉的單絕不重新打開。

CLI 用法 (在專案根目錄)：
    uv run python -m tools.ticket_tools --init-labels
"""

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tools.secret_guard import redact, redact_obj

log = logging.getLogger("arq.ticket")

API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 20
USER_AGENT = "ArQ/0.1 (+ticket_tools)"

MAX_LIST = 20          # list_tickets 最多回傳筆數 (控制 Gemini Token)
MAX_COMMENTS = 10      # get_ticket 只取最後幾則留言
MAX_BODY_CHARS = 4000  # get_ticket 回傳的內文長度上限
MAX_COMMENT_CHARS = 1000  # get_ticket 回傳的一般留言長度上限 (最新規劃另外完整回傳)
MAX_PLAN_CHARS = 8000  # 最新只讀規劃留言的上限 (rd_dispatch 寫入上限約 60,000 字，超過才帶截斷標記)
PLAN_COMMENT_PREFIX = "📝 只讀規劃"  # rd_dispatch 只讀規劃留言的開頭，用來找回規劃
MAX_PAGES = 3          # 搜尋 / 相似單比對最多抓幾頁 (每頁 100 筆；issues 端點連 PR 一起算)
SIMILARITY_THRESHOLD = 0.25  # bigram Jaccard 達此分數才算相似 / 命中
MAX_CANDIDATES = 3     # 相似單候選上限
MAX_DIFF_COMMENT_CHARS = 800  # 改內文時自動留言的差異摘要上限
_TRUNCATED_MARK = "…(已截斷，原長"  # _clip_marked 的截斷標記；內文含此標記代表是被截斷的版本，不可寫回

OPENERS = ("user", "pm", "rd-claude", "rd-agy")
ENGINES = ("claude", "agy")
TYPES = ("feature", "bug", "chore")
STATUSES = ("todo", "in-progress", "blocked", "review", "done")
PRIORITIES = ("p0", "p1", "p2", "p3")
SIZES = ("s", "m", "l")
STATES = ("open", "closed", "all")

# (名稱, 顏色, 說明)：init_labels() 依此建立，也是 label 定義的唯一來源
LABELS: List[Tuple[str, str, str]] = [
    ("by:user", "1d4ed8", "開單者：使用者"),
    ("by:pm", "3b82f6", "開單者：PM（Gemini）"),
    ("by:rd-claude", "60a5fa", "開單者：RD Claude Code"),
    ("by:rd-agy", "93c5fd", "開單者：RD Antigravity"),
    ("rd:claude", "f97316", "負責實作：Claude Code"),
    ("rd:agy", "a855f7", "負責實作：Antigravity"),
    ("type:feature", "16a34a", "類型：新功能"),
    ("type:bug", "dc2626", "類型：錯誤"),
    ("type:chore", "6b7280", "類型：雜務、設定、文件"),
    ("status:todo", "d1d5db", "狀態：待辦"),
    ("status:in-progress", "facc15", "狀態：開發中"),
    ("status:blocked", "ef4444", "狀態：卡住，等待決策或外部因素"),
    ("status:review", "8b5cf6", "狀態：等待 review"),
    ("status:done", "22c55e", "狀態：完成"),
    ("priority:p0", "7f1d1d", "優先：立刻"),
    ("priority:p1", "b91c1c", "優先：本週"),
    ("priority:p2", "f87171", "優先：之後"),
    ("priority:p3", "fecaca", "優先：有空再說"),
    ("size:s", "e5e7eb", "大小：少於半天"),
    ("size:m", "d1d5db", "大小：約 1 天"),
    ("size:l", "9ca3af", "大小：需要再拆"),
]

# Ticket 固定格式 (與 .github/ISSUE_TEMPLATE/ 一致)：開單時缺的段落補上 TBD，等細化 (refinement) 時再填
SECTIONS = ("使用者故事", "驗收標準", "範圍外", "相依")
TBD = "（待細化）"
_SECTION_DEFAULTS = {"使用者故事": TBD, "驗收標準": f"- [ ] {TBD}", "範圍外": TBD, "相依": "無"}

# 拆單規則：ticket get 讀到 size:l 且還沒有子單時附上 (不放系統提示詞，每次呼叫都送太貴)
SPLIT_RULES = (
    "要拆：size:l、改動跨多個不相關模組、可以先定好介面再分開做。"
    "不拆：size:s / size:m、改動集中在同一個檔案、後一步需要前一步的結果。"
    "拆法：先把「介面約定」（函式簽名、資料格式）寫進母單（update body），子單各自可獨立驗收，"
    "最後一張子單固定是「整合與全部測試」。每張子單內文精簡寫明：屬於哪張母單、依哪段介面約定、與其他子單的分工"
    "（派工時系統也會把母單、兄弟子單與相依附給 RD）。拆單結果（標題、範圍、相依）先列給使用者確認，確認後才逐張 create，"
    "帶 parent=母單號、blocked_by=要先完成的單號。子單依相依順序一張一張派工（被擋住的單系統不會派工）。"
)

# 平行派工規則：讀到子單或母單時附上 (要不要同時派工只在拆單後才會遇到)
PARALLEL_RULES = (
    "可以同時派給兩位 RD 的單：沒有相依（不在彼此的 blocked_by / blocking 鏈上）、不改同一批檔案（看各子單的分工與介面約定）。"
    "同時派工前先向使用者說明依據（哪幾張、為什麼互相獨立、各改哪些檔案）。"
    "上限預設 2（Claude 一個、agy 一個）；系統會拒絕與執行中的單有相依的派工、同一張單派兩次、或超過上限，被拒絕時照原因告訴使用者，不要重試。"
)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_META_PREFIX = "<!-- arq:"
_META_SUFFIX = " -->"


# -------------------------------------------------------------
# 底層：設定與送出請求
# -------------------------------------------------------------

def _fail(message: str, **extra: Any) -> Dict[str, Any]:
    return redact_obj({"success": False, "error": redact(message), **extra})


def _config() -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    """回傳 ((repo, token), 錯誤)"""
    repo = os.getenv("TICKETS_REPO", "").strip()
    token = os.getenv("TICKETS_GITHUB_TOKEN", "").strip()
    if not repo or not token:
        return None, "Ticket 功能尚未設定：請在 .env 填入 TICKETS_REPO 與 TICKETS_GITHUB_TOKEN。"
    if not _REPO_RE.match(repo) or ".." in repo:
        return None, "TICKETS_REPO 格式錯誤，應為 owner/repo。"
    return (repo, token), None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不跟隨，避免 Authorization Header 被帶到其他主機


_OPENER = urllib.request.build_opener(_NoRedirect)


def _send(method: str, url: str, headers: Dict[str, str], data: Optional[bytes]) -> Tuple[int, Any]:
    """實際送出請求 (測試時 mock 這個函式)；回傳 (狀態碼, 解析後的 JSON 或文字)"""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=REQUEST_TIMEOUT) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, (e.read() if e.fp else b"")
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return status, text


def _api(method: str, path: str, body: Optional[Dict[str, Any]] = None,
         query: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Any], Optional[str], int]:
    """呼叫 GitHub REST；回傳 (資料, 錯誤, 狀態碼)。不拋例外。"""
    cfg, err = _config()
    if err:
        return None, err, 0
    repo, token = cfg
    url = f"{API_BASE}/repos/{repo}{path}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        status, payload = _send(method, url, headers, data)
    except Exception as e:
        return None, f"連線 GitHub 失敗：{type(e).__name__}: {e}", 0
    if 200 <= status < 300:
        return payload, None, status
    message = payload.get("message", "") if isinstance(payload, dict) else str(payload or "")
    return payload, f"GitHub API 錯誤 {status}：{message[:300]}", status


# -------------------------------------------------------------
# Metadata 與 label 解析
# -------------------------------------------------------------

def build_metadata(opened_by: str, engine: str = "", session_id: str = "", parent: Optional[int] = None) -> str:
    """內文第一行的隱藏 metadata (ArQ 寫入的可信紀錄)"""
    meta = {
        "opened_by": opened_by,
        "engine": engine or None,
        "session_id": session_id or None,
        "parent": parent,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # JSON 字串值中的 > 改用 >，確保內容無法提前結束 HTML 註解
    payload = json.dumps(meta, ensure_ascii=False).replace(">", "\\u003e")
    return f"{_META_PREFIX}{payload}{_META_SUFFIX}"


def parse_metadata(body: str) -> Dict[str, Any]:
    """只解析內文第一行；不是 ArQ 寫入的格式就回傳空 dict"""
    first = (body or "").lstrip("﻿").split("\n", 1)[0].strip()
    if not (first.startswith(_META_PREFIX) and first.endswith(_META_SUFFIX)):
        return {}
    try:
        meta = json.loads(first[len(_META_PREFIX):-len(_META_SUFFIX)])
    except json.JSONDecodeError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _strip_metadata(body: str) -> str:
    body = body or ""
    if parse_metadata(body):
        return body.split("\n", 1)[1].lstrip("\n") if "\n" in body else ""
    return body


def _has_section(body: str, name: str) -> bool:
    """段落標題可用 **名稱** 或 Markdown 標題 (## 名稱)"""
    return bool(re.search(rf"^\s*(\*\*{name}\*\*|#+\s*{name})", body, re.MULTILINE))


def format_ticket_body(body: str) -> str:
    """
    套用固定格式：四個段落都在就原樣回傳；完全沒有段落時，原文放進「使用者故事」；
    只缺部分段落時補在最後。補上的內容為 TBD，代表需要細化。
    """
    body = (body or "").strip()
    present = [s for s in SECTIONS if _has_section(body, s)]
    if len(present) == len(SECTIONS):
        return body
    if present:
        parts, missing = [body], [s for s in SECTIONS if s not in present]
    else:
        parts, missing = [f"**{SECTIONS[0]}**\n{body or TBD}"], list(SECTIONS[1:])
    parts += [f"**{s}**\n{_SECTION_DEFAULTS[s]}" for s in missing]
    return "\n\n".join(parts)


def _label_names(item: Dict[str, Any]) -> List[str]:
    return [lb.get("name", "") for lb in item.get("labels") or [] if isinstance(lb, dict)]


def _label_value(labels: List[str], group: str) -> Optional[str]:
    for name in labels:
        if name.startswith(group + ":"):
            return name.split(":", 1)[1]
    return None


def _summary(item: Dict[str, Any]) -> Dict[str, Any]:
    labels = _label_names(item)
    meta = parse_metadata(item.get("body") or "")
    label_opener = _label_value(labels, "by")
    opener = meta.get("opened_by") or label_opener
    return {
        "number": item.get("number"),
        "title": item.get("title", ""),
        "state": item.get("state", ""),
        "status": _label_value(labels, "status"),
        "priority": _label_value(labels, "priority"),
        "type": _label_value(labels, "type"),
        "rd": _label_value(labels, "rd"),
        "opened_by": opener,
        "opened_by_label": label_opener,
        "opener_mismatch": bool(meta.get("opened_by") and label_opener and meta["opened_by"] != label_opener),
        "url": item.get("html_url", ""),
        "updated_at": item.get("updated_at", ""),
        "parent": _number_from_url(item.get("parent_issue_url")),
        "blocked": bool((item.get("issue_dependencies_summary") or {}).get("blocked_by")),
        "sub_progress": _sub_progress(item),
    }


def _number_from_url(url: Optional[str]) -> Optional[int]:
    m = re.search(r"/issues/(\d+)$", url or "")
    return int(m.group(1)) if m else None


def _sub_progress(item: Dict[str, Any]) -> Optional[str]:
    """子單進度「完成數/總數」；沒有子單回傳 None"""
    summary = item.get("sub_issues_summary") or {}
    return f"{summary.get('completed', 0)}/{summary['total']}" if summary.get("total") else None


def _brief(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"number": item.get("number"), "title": item.get("title", ""), "state": item.get("state", ""),
            "status": _label_value(_label_names(item), "status")}


def _relations(number: int, item: Dict[str, Any]) -> Dict[str, Any]:
    """母單、子單、相依；issue 摘要欄位顯示沒有關聯時不多打 API。讀取失敗只記在 relation_errors"""
    rel: Dict[str, Any] = {"parent": None, "sub_issues": [], "sub_progress": _sub_progress(item),
                           "blocked_by": [], "blocking": [], "open_blockers": []}
    errors = []
    deps = item.get("issue_dependencies_summary") or {}
    lookups = [("parent", "/parent", bool(item.get("parent_issue_url"))),
               ("sub_issues", "/sub_issues", bool((item.get("sub_issues_summary") or {}).get("total"))),
               ("blocked_by", "/dependencies/blocked_by", bool(deps.get("total_blocked_by"))),
               ("blocking", "/dependencies/blocking", bool(deps.get("total_blocking")))]
    for key, suffix, needed in lookups:
        if not needed:
            continue
        data, err, _ = _api("GET", f"/issues/{number}{suffix}", query=None if key == "parent" else {"per_page": 50})
        if err:
            errors.append(f"{key}：{err}")
        elif key == "parent":
            rel["parent"] = {"number": data.get("number"), "title": data.get("title", "")}
        else:
            rel[key] = [_brief(i) for i in data or [] if isinstance(i, dict)]
    rel["open_blockers"] = [b["number"] for b in rel["blocked_by"] if b.get("state") == "open"]
    if errors:
        rel["relation_errors"] = errors
    return rel


def _with_blockers(body: str, blocked_by: List[int]) -> str:
    """把「被擋住」寫進內文的「相依」段落 (dependencies API 失敗時仍看得到)；原本只寫「無」或待細化時取代"""
    if not blocked_by:
        return body
    note = "被擋住（要先完成）：" + "、".join(f"#{n}" for n in blocked_by)
    m = re.search(r"^[ \t]*(\*\*相依\*\*|#+[ \t]*相依)[^\n]*\n?", body, re.MULTILINE)
    if not m:
        return f"{body}\n\n**相依**\n{note}"
    head, rest = body[:m.end()].rstrip("\n"), body[m.end():]
    first, _, after = rest.partition("\n")
    if first.strip() in ("無", TBD, ""):
        rest = after
    return f"{head}\n{note}" + (f"\n{rest}" if rest else "")


def _priority_rank(t: Dict[str, Any]) -> int:
    return PRIORITIES.index(t["priority"]) if t.get("priority") in PRIORITIES else len(PRIORITIES)


# -------------------------------------------------------------
# 公開函式
# -------------------------------------------------------------

def create_ticket(title: str, body: str, opened_by: str, type_: str = "feature", priority: str = "p2",
                  engine: str = "", session_id: str = "", parent: Optional[int] = None,
                  status: str = "todo", blocked_by: Optional[List[int]] = None) -> Dict[str, Any]:
    """
    開單。opened_by 由呼叫端程式決定 (OPENERS)，不可開放給 AI 自行填寫。
    engine 有值時加上 rd:<engine> (負責實作的工程師)；status 預設 todo，派工時為 in-progress。
    parent：母單編號，用 sub-issues API 掛上去；blocked_by：擋住這張的單號，用 issue dependencies 標示，
    並寫進內文「相依」段落。母單 / 擋住的單不存在時不開單；掛載失敗只記在 link_notes，單照開。
    """
    if status not in STATUSES:
        return _fail(f"不支援的狀態「{status}」，可用：{'、'.join(STATUSES)}")
    if opened_by not in OPENERS:
        return _fail(f"不支援的開單者「{opened_by}」，可用：{'、'.join(OPENERS)}")
    if type_ not in TYPES:
        return _fail(f"不支援的類型「{type_}」，可用：{'、'.join(TYPES)}")
    if priority not in PRIORITIES:
        return _fail(f"不支援的優先順序「{priority}」，可用：{'、'.join(PRIORITIES)}")
    if engine and engine not in ENGINES:
        return _fail(f"不支援的工程師「{engine}」，可用：{'、'.join(ENGINES)}")
    title = redact((title or "").strip())
    if not title:
        return _fail("標題不可為空。")
    blocked_by = sorted({int(n) for n in blocked_by or [] if int(n) > 0})
    linked: Dict[int, Any] = {}  # 單號 → GitHub 內部 id (sub-issues / dependencies API 要用 id，不是單號)
    for n in ([int(parent)] if parent else []) + blocked_by:
        found, err, _ = _api("GET", f"/issues/{n}")
        if err or not isinstance(found, dict) or "pull_request" in found:
            return _fail(f"找不到 ticket #{n}（{err or '是 PR 不是 ticket'}），未開單。")
        linked[n] = found.get("id")
    full_body = (build_metadata(opened_by, engine, session_id, int(parent) if parent else None) + "\n"
                 + _with_blockers(format_ticket_body(redact(body or "")), blocked_by))
    labels = [f"by:{opened_by}", f"type:{type_}", f"priority:{priority}", f"status:{status}"]
    if engine:
        labels.append(f"rd:{engine}")
    data, err, _ = _api("POST", "/issues", {"title": title[:256], "body": full_body, "labels": labels})
    if err:
        return _fail(err)
    result = {"success": True, "number": data.get("number"), "url": data.get("html_url", "")}
    notes = []
    if parent:
        _, err, _ = _api("POST", f"/issues/{int(parent)}/sub_issues", {"sub_issue_id": data.get("id")})
        if err:
            notes.append(f"掛到母單 #{parent} 失敗（metadata 已記錄母單）：{err}")
        result["parent"] = int(parent)
    for n in blocked_by:
        _, err, _ = _api("POST", f"/issues/{data.get('number')}/dependencies/blocked_by", {"issue_id": linked[n]})
        if err:
            notes.append(f"標示被 #{n} 擋住失敗（已寫在內文「相依」段落）：{err}")
    if blocked_by:
        result["blocked_by"] = blocked_by
    if notes:
        result["link_notes"] = notes
    board_note = sync_board_status(data.get("node_id") or "", status)  # 開單後放上看板並設初始狀態
    if board_note:
        result["board_note"] = board_note
    return redact_obj(result)


def list_tickets(state: str = "open", opened_by: str = "", status: str = "", priority: str = "",
                 limit: int = MAX_LIST) -> Dict[str, Any]:
    """列出 ticket (排除 PR)，依優先順序排序，最多 MAX_LIST 筆"""
    if state not in STATES:
        return _fail(f"state 必須是 {'、'.join(STATES)} 其中之一。")
    filters = [("by", opened_by, OPENERS), ("status", status, STATUSES), ("priority", priority, PRIORITIES)]
    labels = []
    for group, value, allowed in filters:
        if value:
            if value not in allowed:
                return _fail(f"{group} 必須是 {'、'.join(allowed)} 其中之一。")
            labels.append(f"{group}:{value}")
    data, err, _ = _api("GET", "/issues", query={"state": state, "labels": ",".join(labels), "per_page": 100})
    if err:
        return _fail(err)
    tickets = [_summary(item) for item in data or [] if isinstance(item, dict) and "pull_request" not in item]
    tickets.sort(key=lambda t: (_priority_rank(t), t["number"] or 0))
    limit = max(1, min(int(limit or MAX_LIST), MAX_LIST))
    return redact_obj({"success": True, "total": len(tickets), "tickets": tickets[:limit]})


def get_ticket(number: int) -> Dict[str, Any]:
    """讀取單張 ticket：摘要、內文 (不含 metadata) 與最後幾則留言"""
    data, err, _ = _api("GET", f"/issues/{int(number)}")
    if err:
        return _fail(err)
    if "pull_request" in data:
        return _fail(f"#{number} 是 Pull Request，不是 ticket。")
    comments, err, _ = _api("GET", f"/issues/{int(number)}/comments", query={"per_page": 100})
    if err:
        return _fail(err)
    ticket = _summary(data)
    ticket["metadata"] = parse_metadata(data.get("body") or "")
    ticket.update(_relations(int(number), data))
    if _label_value(_label_names(data), "size") == "l" and not ticket["sub_issues"]:
        ticket["split_rules"] = SPLIT_RULES  # 只在需要拆的單附上，系統提示詞只放一行重點
    if ticket["parent"] or ticket["sub_issues"]:
        ticket["parallel_rules"] = PARALLEL_RULES  # 拆單後才有平行派工的問題
    ticket["body"] = _clip_marked(_strip_metadata(data.get("body") or ""), MAX_BODY_CHARS)
    # 最新一則只讀規劃完整回傳：無聲截斷曾讓 PM 以為規劃不完整而到處抓留言
    plans = [c for c in comments or [] if (c.get("body") or "").startswith(PLAN_COMMENT_PREFIX)]
    latest = plans[-1] if plans else None
    ticket["latest_plan"] = _clip_marked(latest.get("body") or "", MAX_PLAN_CHARS) if latest else None
    ticket["comments"] = [
        {"author": (c.get("user") or {}).get("login", ""), "created_at": c.get("created_at", ""),
         "body": _clip_marked(c.get("body") or "", MAX_COMMENT_CHARS)}
        for c in (comments or []) if not (c.get("body") or "").startswith(PLAN_COMMENT_PREFIX)
    ][-MAX_COMMENTS:]
    return redact_obj({"success": True, "ticket": ticket})


def _clip_marked(text: str, limit: int) -> str:
    """截斷時明確標示原長度 (不再無聲截斷)"""
    return text if len(text) <= limit else text[:limit] + f"\n{_TRUNCATED_MARK} {len(text)} 字；完整內容在 GitHub)"


# -------------------------------------------------------------
# 搜尋與相似單比對：全部在本地對 issues 清單做字元 bigram Jaccard，中文不需分詞
# -------------------------------------------------------------

def _bigrams(text: str) -> set:
    chars = [c for c in (text or "").lower() if c.isalnum()]
    if len(chars) < 2:
        return set(chars)
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def _jaccard(a: str, b: str) -> float:
    sa, sb = _bigrams(a), _bigrams(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _plain_body(body: str) -> str:
    """比對用內文：去掉 metadata、段落標題與「待細化」，避免每張單共有的格式字樣墊高分數"""
    text = _strip_metadata(body)
    for word in (*SECTIONS, "待細化"):
        text = text.replace(word, "")
    return text


def _fetch_issues(state: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """抓最多 MAX_PAGES 頁 issues (排除 PR)；回傳 (清單, 錯誤)"""
    items: List[Dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        data, err, _ = _api("GET", "/issues", query={"state": state, "per_page": 100, "page": page})
        if err:
            return [], err
        if not isinstance(data, list):
            break
        items += [i for i in data if isinstance(i, dict) and "pull_request" not in i]
        if len(data) < 100:
            break
    return items, None


def _search_score(query: str, item: Dict[str, Any]) -> float:
    title, body = item.get("title", ""), _plain_body(item.get("body") or "")
    q = "".join(c for c in query.lower() if c.isalnum())
    if q and q in "".join(c for c in title.lower() if c.isalnum()):
        return 1.0
    if q and q in "".join(c for c in body.lower() if c.isalnum()):
        return 0.5
    return max(_jaccard(query, title), 0.5 * _jaccard(query, body))


def search_tickets(query: str, state: str = "all", limit: int = MAX_LIST) -> Dict[str, Any]:
    """關鍵字搜尋 ticket (標題與內文，排除 PR)；回傳精簡欄位與相似度，依分數排序，最多 MAX_LIST 筆"""
    query = (query or "").strip()
    if not query:
        return _fail("query 不可為空。")
    if state not in STATES:
        return _fail(f"state 必須是 {'、'.join(STATES)} 其中之一。")
    items, err = _fetch_issues(state)
    if err:
        return _fail(err)
    hits = []
    for item in items:
        score = _search_score(query, item)
        if score >= SIMILARITY_THRESHOLD:
            s = _summary(item)
            hits.append({"number": s["number"], "title": s["title"], "state": s["state"],
                         "status": s["status"], "url": s["url"], "score": round(score, 2)})
    hits.sort(key=lambda t: (-t["score"], t["number"] or 0))
    limit = max(1, min(int(limit or MAX_LIST), MAX_LIST))
    return redact_obj({"success": True, "total": len(hits), "tickets": hits[:limit]})


def find_similar(title: str, body: str = "") -> Dict[str, Any]:
    """開單前比對 state=all 的單；回傳分數 >= SIMILARITY_THRESHOLD 的前 MAX_CANDIDATES 筆候選 (標題權重較高)"""
    items, err = _fetch_issues("all")
    if err:
        return _fail(err)
    new_body = _plain_body(body)
    candidates = []
    for item in items:
        score = _jaccard(title, item.get("title", ""))
        if _bigrams(new_body):
            score = 0.6 * score + 0.4 * _jaccard(new_body, _plain_body(item.get("body") or ""))
        if score >= SIMILARITY_THRESHOLD:
            s = _summary(item)
            candidates.append({"number": s["number"], "title": s["title"], "state": s["state"],
                               "status": s["status"], "url": s["url"], "score": round(score, 2)})
    candidates.sort(key=lambda c: (-c["score"], c["number"] or 0))
    return redact_obj({"success": True, "candidates": candidates[:MAX_CANDIDATES]})


# -------------------------------------------------------------
# 改內文：整份取代、第一行 metadata 由程式保留、差異自動留言
# -------------------------------------------------------------

_ITEM_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$", re.MULTILINE)
_SECTION_HEAD_RE = re.compile(
    r"^\s*(?:\*\*(%s)\*\*|#+\s*(%s))" % ("|".join(SECTIONS), "|".join(SECTIONS)), re.MULTILINE)


def _checklist(body: str) -> List[str]:
    return list(dict.fromkeys(_ITEM_RE.findall(body)))


def _sections(body: str) -> Dict[str, str]:
    marks = list(_SECTION_HEAD_RE.finditer(body))
    return {(m.group(1) or m.group(2)): body[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(body)].strip()
            for i, m in enumerate(marks)}


def _body_diff_summary(old: str, new: str) -> str:
    """改內文留言用：驗收標準新增 / 移除的條目與有變動的段落名稱 (限長 MAX_DIFF_COMMENT_CHARS)"""
    old_items, new_items = _checklist(old), _checklist(new)
    added = [i for i in new_items if i not in old_items]
    removed = [i for i in old_items if i not in new_items]
    old_secs, new_secs = _sections(old), _sections(new)
    changed = [s for s in SECTIONS if old_secs.get(s, "") != new_secs.get(s, "")]
    lines = ["📝 內文已更新"]
    if added:
        lines.append("驗收標準新增：" + "；".join(added))
    if removed:
        lines.append("驗收標準移除：" + "；".join(removed))
    if changed:
        lines.append("有變動的段落：" + "、".join(changed))
    text = "\n".join(lines)
    return text if len(text) <= MAX_DIFF_COMMENT_CHARS else text[:MAX_DIFF_COMMENT_CHARS - 1] + "…"


def _compose_body(current_body: str, new_body: str) -> str:
    """保留現有第一行 metadata (逐字)；新內文開頭的 <!-- arq: 行一律剝除，經 redact 與 format_ticket_body"""
    new_body = redact(new_body or "").lstrip("﻿\n ")
    while new_body.startswith(_META_PREFIX):
        new_body = new_body.split("\n", 1)[1].lstrip("\n ") if "\n" in new_body else ""
    formatted = format_ticket_body(new_body)
    if parse_metadata(current_body):
        return (current_body or "").split("\n", 1)[0] + "\n" + formatted
    return formatted


# -------------------------------------------------------------
# PR merge 守門：ticket 只有在對應 PR 已 merge 後才能 done；merge 確認卡的 PR 資訊
# -------------------------------------------------------------

CLOSE_REASONS = ("completed", "not_planned")
# PR 內文的關單關鍵字 (GitHub 定義：close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved)
_CLOSING_RE_TEMPLATE = r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#{n}\b"
_ANY_CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.IGNORECASE)


def merged_closing_pr(number: int) -> Optional[int]:
    """回傳「內文以 Closes/Fixes/Resolves #number 指向本單、且已 merge」的 PR 編號；沒有就回傳 None (查詢失敗也是 None)"""
    events, err, _ = _api("GET", f"/issues/{int(number)}/timeline", query={"per_page": 100})
    if err or not isinstance(events, list):
        return None
    closing = re.compile(_CLOSING_RE_TEMPLATE.format(n=int(number)), re.IGNORECASE)
    for ev in events:
        src = ((ev or {}).get("source") or {}).get("issue") or {}
        pr = src.get("pull_request") or {}
        if ev.get("event") == "cross-referenced" and pr.get("merged_at") and closing.search(src.get("body") or ""):
            return src.get("number")
    return None


def pr_merge_summary(number: int) -> Dict[str, Any]:
    """
    merge 確認卡用的 PR 摘要：標題、連結、對應 ticket、review 數量。失敗回傳 success=False (不拋例外)。
    基本資料走 issues 端點，但 fine-grained token 讀 PR 仍需要 Pull requests 唯讀權限 (只開 Issues 會 403)；
    review 也需要同一個權限，讀不到時 reviews=None，由確認卡標示「無法取得」。
    """
    pr, err, _ = _api("GET", f"/issues/{int(number)}")
    if err or not isinstance(pr, dict):
        return _fail(err or "讀取 PR 失敗")
    if "pull_request" not in pr:
        return _fail(f"#{number} 不是 Pull Request。")
    reviews, err, _ = _api("GET", f"/pulls/{int(number)}/reviews", query={"per_page": 100})
    if err or not isinstance(reviews, list):
        reviews = None
    tickets = sorted({int(n) for n in _ANY_CLOSING_RE.findall(pr.get("body") or "")})
    return redact_obj({
        "success": True, "number": pr.get("number"), "title": pr.get("title", ""), "url": pr.get("html_url", ""),
        "state": pr.get("state", ""), "merged": bool((pr.get("pull_request") or {}).get("merged_at")),
        "tickets": tickets, "reviews": None if reviews is None else len(reviews),
        "approved": None if reviews is None else sum(1 for r in reviews if r.get("state") == "APPROVED"),
    })


# -------------------------------------------------------------
# 看板同步：狀態 label 改變時，一併移動 GitHub Projects v2 看板的 Status 欄位
# 設定：TICKETS_PROJECT=owner/編號、TICKETS_PROJECT_TOKEN (classic PAT，project scope；private repo 還要 repo scope；fine-grained 不支援個人帳號的 Projects)
# 未設定時只改 label；看板更新失敗只回傳 board_note，不影響 label。
# -------------------------------------------------------------

BOARD_STATUS_OPTIONS = {"todo": "Todo", "in-progress": "In Progress", "blocked": "Blocked",
                        "review": "Review", "done": "Done"}
_BOARD_CACHE: Dict[str, Any] = {}

_Q_META = ("query($login:String!,$number:Int!){ %s(login:$login){ projectV2(number:$number){ id "
           "field(name:\"Status\"){ ... on ProjectV2SingleSelectField { id options { id name } } } } } }")
_Q_ITEMS = ("query($id:ID!){ node(id:$id){ ... on Issue { projectItems(first:50){ nodes { id project { id } } } } } }")
_M_ADD = ("mutation($project:ID!,$content:ID!){ addProjectV2ItemById(input:{projectId:$project,contentId:$content})"
          "{ item { id } } }")
_M_SET = ("mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){ updateProjectV2ItemFieldValue(input:"
          "{projectId:$project,itemId:$item,fieldId:$field,value:{singleSelectOptionId:$option}}){ projectV2Item { id } } }")


def _reset_board_cache() -> None:
    _BOARD_CACHE.clear()


def _board_config() -> Tuple[Optional[Tuple[str, int, str]], Optional[str]]:
    """回傳 ((owner, 編號, token), 錯誤)；設定缺漏或格式錯誤時回傳 (None, 說明)，不再靜默跳過"""
    project = os.getenv("TICKETS_PROJECT", "").strip()
    token = os.getenv("TICKETS_PROJECT_TOKEN", "").strip()
    if not project and not token:
        return None, "⚠️ 看板未設定 TICKETS_PROJECT / TICKETS_PROJECT_TOKEN，跳過看板同步（只更新 label）。"
    m = re.fullmatch(r"([A-Za-z0-9-]+)/(\d+)", project)
    if not m:
        return None, ("⚠️ 看板設定錯誤：TICKETS_PROJECT 應為 owner/編號"
                      + ("，且尚未設定 TICKETS_PROJECT_TOKEN" if not token else "") + "，跳過看板同步（只更新 label）。")
    if not token:
        return None, "⚠️ 看板未設定 TICKETS_PROJECT_TOKEN，跳過看板同步（只更新 label）。"
    return (m.group(1), int(m.group(2)), token), None


def _graphql(query: str, variables: Dict[str, Any], token: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "Content-Type": "application/json", "User-Agent": USER_AGENT}
    data = json.dumps({"query": query, "variables": variables}, ensure_ascii=False).encode("utf-8")
    try:
        status, payload = _send("POST", f"{API_BASE}/graphql", headers, data)
    except Exception as e:
        return None, f"連線 GitHub 失敗：{type(e).__name__}: {e}"
    if not 200 <= status < 300 or not isinstance(payload, dict):
        return None, f"GraphQL 錯誤 {status}"
    if payload.get("errors"):
        return None, "GraphQL 錯誤：" + "；".join(str(e.get("message", ""))[:150] for e in payload["errors"][:2])
    return payload.get("data") or {}, None


def _board_meta(owner: str, number: int, token: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """看板 ID、Status 欄位 ID 與選項 ID (依名稱查詢後快取，不寫死)；個人帳號找不到再試組織"""
    key = f"{owner}/{number}"
    if key in _BOARD_CACHE:
        return _BOARD_CACHE[key], None
    err = None
    for kind in ("user", "organization"):
        data, err = _graphql(_Q_META % kind, {"login": owner, "number": number}, token)
        project = ((data or {}).get(kind) or {}).get("projectV2")
        if project and (project.get("field") or {}).get("id"):
            meta = {"project": project["id"], "field": project["field"]["id"],
                    "options": {o["name"]: o["id"] for o in project["field"].get("options") or []}}
            _BOARD_CACHE[key] = meta
            return meta, None
    return None, err or f"找不到看板 {key} 或它沒有 Status 欄位。"


def _board_warn(note: str) -> str:
    """遮蔽後記 WARNING log 並回傳同一段文字 (給 board_note)；同步失敗不丟例外、不阻斷主流程"""
    note = redact(note)
    log.warning("ticket 看板同步：%s", note)
    return note


def sync_board_status(issue_node_id: str, status: str) -> Optional[str]:
    """把看板上這張單的 Status 設成 status；回傳 None (成功) 或給使用者看的 board_note (未設定 / 失敗，同時記 WARNING)"""
    if status not in BOARD_STATUS_OPTIONS:
        return None
    cfg, err = _board_config()
    if cfg is None:
        return _board_warn(err or "看板設定無法使用，跳過同步。")
    if not issue_node_id:
        return _board_warn("看板同步失敗：取不到 issue 的 node_id。")
    owner, number, token = cfg
    meta, err = _board_meta(owner, number, token)
    if err:
        return _board_warn(f"看板同步失敗：{err}")
    option = meta["options"].get(BOARD_STATUS_OPTIONS[status])
    if not option:
        return _board_warn(f"看板同步失敗：Status 欄位沒有「{BOARD_STATUS_OPTIONS[status]}」選項。")
    data, err = _graphql(_Q_ITEMS, {"id": issue_node_id}, token)
    if err:
        return _board_warn(f"看板同步失敗：{err}")
    nodes = (((data or {}).get("node") or {}).get("projectItems") or {}).get("nodes") or []
    item = next((n["id"] for n in nodes if (n.get("project") or {}).get("id") == meta["project"]), None)
    if item is None:
        data, err = _graphql(_M_ADD, {"project": meta["project"], "content": issue_node_id}, token)
        item = (((data or {}).get("addProjectV2ItemById") or {}).get("item") or {}).get("id")
        if err or not item:
            return _board_warn(f"看板同步失敗（加入看板）：{err or '沒有回傳 item'}")
    _, err = _graphql(_M_SET, {"project": meta["project"], "item": item, "field": meta["field"], "option": option}, token)
    return _board_warn(f"看板同步失敗：{err}") if err else None


def _set_status(number: int, item: Dict[str, Any], status: str) -> Tuple[Optional[str], Optional[str]]:
    """只替換 status:* label 並同步看板；回傳 (錯誤, 看板提示)"""
    labels = [n for n in _label_names(item) if not n.startswith("status:")] + [f"status:{status}"]
    _, err, _ = _api("PUT", f"/issues/{number}/labels", {"labels": labels})
    if err:
        return err, None
    return None, sync_board_status(item.get("node_id") or "", status)


def _subs_finished(parent: int, child: Optional[int] = None) -> Tuple[bool, Optional[str]]:
    """母單的子單是否全部完成 (已關閉；child 是剛設為 done 的子單，視為完成)；沒有子單回傳 False"""
    subs, err, _ = _api("GET", f"/issues/{parent}/sub_issues", query={"per_page": 100})
    if err:
        return False, err
    subs = [s for s in subs or [] if isinstance(s, dict)]
    return bool(subs) and all(s.get("state") == "closed" or s.get("number") == child for s in subs), None


_PARENT_START = ("in-progress", "review", "blocked")  # 子單進入這些狀態代表母單已開工


def _sync_parent(child: Dict[str, Any], number: int, status: str) -> Dict[str, Any]:
    """
    子單改狀態後同步母單，只往前推、不往回拉：
    子單開工 → 母單 todo 改 in-progress；子單 done 且其他子單都已關閉 → 母單改 review 並留言請使用者確認。
    回傳要併入結果的欄位 (parent_status / parent_board_note / parent_note)；失敗只提示，不影響子單。
    """
    parent = _number_from_url(child.get("parent_issue_url"))
    if not parent or status not in _PARENT_START + ("done",):
        return {}
    item, err, _ = _api("GET", f"/issues/{parent}")
    if err:
        return {"parent_note": f"⚠️ 母單 #{parent} 狀態未同步：{err}"}
    current = _label_value(_label_names(item), "status") or "todo"
    if item.get("state") == "closed":
        return {}
    if status == "done":
        if current in ("review", "done"):
            return {}
        finished, err = _subs_finished(parent, child=number)
        if err:
            return {"parent_note": f"⚠️ 母單 #{parent} 狀態未同步：{err}"}
        if not finished:
            return {}
        target = "review"
    elif current == "todo":
        target = "in-progress"
    else:
        return {}
    err, board_note = _set_status(parent, item, target)
    if err:
        return {"parent_note": f"⚠️ 母單 #{parent} 狀態未同步：{err}"}
    out: Dict[str, Any] = {"parent_status": {"number": parent, "status": target}}
    if board_note:
        out["parent_board_note"] = board_note
    if target == "review":
        _api("POST", f"/issues/{parent}/comments",
             {"body": f"✅ 子單全部完成（最後一張 #{number}），請確認後把母單設為 done（母單不需要 PR）。"})
    return out


def update_ticket(number: int, status: str = "", comment: str = "", close: bool = False,
                  close_reason: str = "completed", body: str = "") -> Dict[str, Any]:
    """
    改狀態 / 留言 / 關單 / 改內文。只替換 status:* label，其他 label 不動。
    子單改狀態時同步母單 (見 _sync_parent)。
    設為 done 或 close=True (completed) 時，必須有已 merge 的 PR 以 Closes #N 指向本單；
    母單 (有子單) 沒有 PR 時，改為要求所有子單都已關閉；
    close_reason="not_planned" 不需要 PR，只關單、不改狀態 label (Gemini 工具會先跳確認卡)。
    已關閉的單不會被重新打開；PR merge 自動關單後設 done，只同步 label。
    body 為整份取代：第一行 metadata 由程式保留，並自動留言記錄差異 (附 comment)；已關閉的單不允許改內文。
    """
    if status and status not in STATUSES:
        return _fail(f"status 必須是 {'、'.join(STATUSES)} 其中之一。")
    if close_reason not in CLOSE_REASONS:
        return _fail(f"close_reason 必須是 {'、'.join(CLOSE_REASONS)} 其中之一。")
    has_body = bool((body or "").strip())
    if not (status or (comment or "").strip() or close or has_body):
        return _fail("沒有要更新的內容：請提供 status、comment、body 或 close。")
    if has_body and _TRUNCATED_MARK in body:
        return _fail("body 含有「已截斷」標記，是 get 回傳的被截斷版本，寫回會遺失內容；"
                     "請改用完整內容（可只更新其中一段後整份送出，或改用 comment 補充）。")
    number = int(number)
    current, err, _ = _api("GET", f"/issues/{number}")
    if err:
        return _fail(err)
    if "pull_request" in current:
        return _fail(f"#{number} 是 Pull Request，不是 ticket。")
    if has_body and current.get("state") == "closed":
        return _fail(f"#{number} 已關閉，不允許修改內文（也不會重新打開）。"
                     f"請向使用者確認後開新單，並在新單內文引用 #{number}。")

    not_planned = close and close_reason == "not_planned"
    result: Dict[str, Any] = {"success": True, "number": number}
    new_status = "" if not_planned else ("done" if close else status)
    if new_status == "done":
        merged_pr = merged_closing_pr(number)
        if merged_pr is not None:
            result["merged_pr"] = merged_pr
        elif not (current.get("sub_issues_summary") or {}).get("total"):
            return _fail(f"#{number} 還沒有已 merge 的 PR（PR 內文需含 Closes #{number}），不能設為 done 或關單。"
                         "請先把 ticket 設為 review，等使用者 review 並 merge PR。")
        else:
            # 母單沒有自己的 PR：佐證是子單全部關閉
            finished, err = _subs_finished(number)
            if not finished:
                return _fail(f"#{number} 是母單，要所有子單都已關閉才能設為 done 或關單"
                             + (f"（讀取子單失敗：{err}）" if err else "；還有子單沒完成。"))
            result["sub_issues_done"] = True
    diff_note = ""
    if has_body:
        full_body = _compose_body(current.get("body") or "", body)
        _, err, _ = _api("PATCH", f"/issues/{number}", {"body": full_body})
        if err:
            return _fail(err)
        result["body_updated"] = True
        diff_note = _body_diff_summary(_strip_metadata(current.get("body") or ""), _strip_metadata(full_body))
    if current.get("state") == "closed" and new_status != "done":
        if new_status:
            result["note"] = f"#{number} 已關閉，不會重新打開，狀態維持不變。"
    elif new_status:
        err, board_note = _set_status(number, current, new_status)
        if err:
            return _fail(err)
        result["status"] = new_status
        if board_note:
            result["board_note"] = board_note
        result.update(_sync_parent(current, number, new_status))

    ai_comment = (comment or "").strip()
    if diff_note and ai_comment:
        ai_comment = f"補充：{ai_comment}"
    comment_text = "\n".join(p for p in (diff_note, ai_comment) if p)
    if comment_text:
        _, err, _ = _api("POST", f"/issues/{number}/comments", {"body": redact(comment_text)})
        if err:
            return _fail(err, **{k: v for k, v in result.items() if k != "success"})
        result["commented"] = True

    if close and current.get("state") != "closed":
        _, err, _ = _api("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": close_reason})
        if err:
            return _fail(err)
        result["closed"] = True
    return redact_obj(result)


def init_labels() -> Dict[str, Any]:
    """建立 LABELS 中所有 label；已存在的略過"""
    created, skipped, failed = [], [], []
    for name, color, description in LABELS:
        data, err, status = _api("POST", "/labels", {"name": name, "color": color, "description": description})
        if not err:
            created.append(name)
        elif status == 422:
            skipped.append(name)
        elif status == 0:
            return _fail(err)
        else:
            failed.append(name)
    return redact_obj({"success": not failed, "created": created, "skipped": skipped, "failed": failed})


# -------------------------------------------------------------
# 給 Gemini 的工具 (agent.py 的 ToolSpec)：單一工具省 Token；開單者固定為 pm，不開放 opened_by / engine / session_id
# -------------------------------------------------------------

_LIST_FIELDS = ("number", "title", "status", "priority", "opened_by", "parent", "blocked")
_LIST_FIELDS_ALWAYS = _LIST_FIELDS[:5]  # parent / blocked 有值才帶，省 Gemini Token
_CANDIDATE_FIELDS = ("number", "title", "state", "status", "url")


def ticket_risk(args: Dict[str, Any]) -> Optional[str]:
    """ticket 工具的風險判斷：以 not planned 關單、force_new 強制開新單 需要使用者同意，其他直接執行"""
    action = str(args.get("action") or "").strip().lower()
    if action == "update" and args.get("close") and str(args.get("close_reason") or "").strip().lower() == "not_planned":
        return "以 not planned 關閉 ticket"
    if action == "create" and args.get("force_new"):
        return "略過相似單檢查，強制開新 ticket"
    return None


def ticket(action: str, number: int = 0, title: str = "", body: str = "", ticket_type: str = "feature",
           priority: str = "p2", status: str = "", comment: str = "", close: bool = False,
           close_reason: str = "completed", state: str = "", by: str = "", query: str = "",
           force_new: bool = False, parent: int = 0, blocked_by: str = "") -> Dict[str, Any]:
    """
    ticket：create（title、body；ticket_type feature/bug/chore；priority p0~p3；子單帶 parent、blocked_by 如"31,32"；相似單會被攔下，使用者確認才 force_new=true）；
    search（query）；list（state、by user/pm、status）；get（number）；
    update（number，status todo/in-progress/blocked/review/done、comment、close、body 整份需先 get）；done/關單需 PR 已 merge，放棄用 close_reason=not_planned。
    """
    action = (action or "").strip().lower()
    if action == "create":
        if not force_new:
            dup = find_similar(title, body)
            if not dup.get("success"):
                return _fail(f"無法檢查是否有相似的單，未開單：{dup.get('error', '')}")
            if dup["candidates"]:
                return {"success": False, "error": "找到相似的 ticket，未開單。",
                        "duplicate_candidates": [{k: c.get(k) for k in _CANDIDATE_FIELDS} for c in dup["candidates"]],
                        "instruction": "向使用者確認：更新舊單（update，帶 number）或確定開新單（force_new=true）。"}
        links: Dict[str, Any] = {}  # 子單才帶
        if parent:
            links["parent"] = int(parent)
        blockers = [int(n) for n in re.findall(r"\d+", str(blocked_by or ""))]
        if blockers:
            links["blocked_by"] = blockers
        return create_ticket(title=title, body=body, opened_by="pm", type_=ticket_type, priority=priority, **links)
    if action == "search":
        return search_tickets(query, state=state or "all")
    if action == "list":
        res = list_tickets(state=state or "open", opened_by=by, status=status)
        if not res.get("success"):
            return res
        return {"success": True, "total": res["total"],
                "tickets": [{k: t.get(k) for k in _LIST_FIELDS if k in _LIST_FIELDS_ALWAYS or t.get(k)}
                            for t in res["tickets"]]}
    if action in ("get", "update"):
        if not number or int(number) <= 0:
            return _fail(f"{action} 需要 number（ticket 編號）。")
        if action == "get":
            return get_ticket(int(number))
        return update_ticket(int(number), status=status, comment=comment, close=bool(close),
                             close_reason=(close_reason or "completed").strip().lower(), body=body)
    return _fail(f"不支援的 action「{action}」，可用：create、search、list、get、update。")


def _main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="ArQ ticket 工具 (GitHub Issues)")
    parser.add_argument("--init-labels", action="store_true", help="建立所有 ticket label (已存在的略過)")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    if args.init_labels:
        print(json.dumps(init_labels(), ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
