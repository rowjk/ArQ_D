"""
派工前的 ticket 脈絡：標題、單號解析、相依檢查、附加給 RD 的 ticket／規劃脈絡
"""

import re
from typing import Any, Dict, List, Optional

from tools import rd_jobs, ticket_tools
from tools.rd_common import ENGINE_LABELS, NotifyFn, _safe_notify


def _clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def ticket_title_from_task(task: str) -> str:
    """以規格第一個非空行 (去掉 Markdown 標題符號) 作為 ticket 標題，最多 80 字"""
    for line in (task or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return "RD 開發任務"


MAX_DEPENDENCY_LOOKUPS = 10  # 相依鏈最多讀幾張單 (每張一次 GitHub 呼叫)


def dependency_conflict(number: int) -> Optional[str]:
    """
    平行派工的相依檢查：要派的單與執行中的單在同一條相依鏈上時回傳拒絕理由，否則 None。
    往上只走 blocked_by (被誰擋住)、往下只走 blocking (擋住誰)，方向固定：
    兩張單同時擋住同一張整合單 (兄弟) 不算相依。沒有其他任務在執行時不查。
    """
    start = int(number)
    running = rd_jobs.running_tickets() - {start}
    if not running:
        return None
    lookups = 0
    for key in ("blocked_by", "blocking"):
        prev: Dict[int, Optional[int]] = {start: None}
        queue = [start]
        while queue and lookups < MAX_DEPENDENCY_LOOKUPS:
            current = queue.pop(0)
            res = ticket_tools.get_ticket(current)
            lookups += 1
            if not res.get("success"):
                continue  # 讀不到就不擋 (ticket 未設定或連線失敗)
            for rel in (res.get("ticket") or {}).get(key) or []:
                other = rel.get("number")
                if not other or other in prev:
                    continue
                prev[other] = current
                if other in running:
                    path, node = [], other
                    while node is not None:
                        path.append(node)
                        node = prev[node]
                    chain = " → ".join(f"#{n}" for n in reversed(path))
                    return (f"#{start} 與執行中的 #{other} 有相依（{chain}），不能同時派工；"
                            f"等 #{other} 完成後再派，或請使用者決定。")
                queue.append(other)
    return None


def blocked_result(number: int, preferred: str, scope: str) -> Optional[Dict[str, Any]]:
    """
    派工前檢查：該單還被未關閉的單擋住時不派工、不改狀態，回傳失敗結果告知被誰擋住；
    沒被擋住或讀不到單 (未設定、連線失敗) 時回傳 None 照常派工。只讀規劃與接續原對話不檢查。
    """
    res = ticket_tools.get_ticket(number)
    blockers = (res.get("ticket") or {}).get("open_blockers") or [] if res.get("success") else []
    if not blockers:
        return None
    refs = "、".join(f"#{n}" for n in blockers)
    return {"success": False, "blocked_by": blockers, "ticket": number, "scope": scope,
            "engine_label": ENGINE_LABELS[preferred], "stdout": "", "stderr": "",
            "error": f"Ticket #{number} 還被 {refs} 擋住（尚未完成），未派工。請先完成 {refs}，或請使用者決定是否解除相依。"}


# -------------------------------------------------------------
# 派工帶入 ticket 內容：/claude 做 這類派工，RD 自動拿到該單的使用者故事與驗收標準
# -------------------------------------------------------------

TICKET_CONTEXT_CHARS = 2500                     # 附加給 RD 的 ticket 內文上限
PLAN_CONTEXT_CHARS = ticket_tools.MAX_PLAN_CHARS  # 附加給 RD 的最新只讀規劃上限，與給 PM 的 latest_plan 一致
PLAN_COMMENT_PREFIX = ticket_tools.PLAN_COMMENT_PREFIX  # _comment_plan() 留言的開頭 (定義在 ticket_tools)
PLAN_COMMENT_CHARS = 60000                      # 規劃留言內文上限：GitHub 留言上限 65,536 字，預留標頭與 sub agent 紀錄
# 前後只排除英數字 (與 HTML 實體的 &)：Python 的 \w 含中文字，「做」「的修改」要認得
_TICKET_REF_RE = re.compile(r"(?<![A-Za-z0-9_&])#(\d{1,6})(?![A-Za-z0-9_])")


def extract_ticket_ref(text: str) -> Optional[int]:
    """取出需求文字中第一個 #N 單號 (#fff 這類色碼不算)；沒有就回傳 None"""
    m = _TICKET_REF_RE.search(text or "")
    return int(m.group(1)) if m else None


PARENT_CONTEXT_CHARS = 1500                     # 附加給 RD 的母單內文 (介面約定) 上限
_MAX_RELATED = 10


def _ref_line(item: Dict[str, Any]) -> str:
    state = "已完成" if item.get("state") == "closed" else (item.get("status") or "未完成")
    return f"#{item.get('number')} {item.get('title', '')}（{state}）"


def ticket_relation_context(t: Dict[str, Any], number: int) -> str:
    """
    拆單後的關聯段落：讓 RD 清楚知道母單與介面約定、同一母單的其他子單、被誰擋住、擋住誰，
    避免拆開後各做各的、超出範圍或對不上介面。沒有任何關聯時回傳空字串。
    """
    parent = t.get("parent") or None
    subs = t.get("sub_issues") or []
    blocked_by = t.get("blocked_by") or []
    blocking = t.get("blocking") or []
    if not (parent or subs or blocked_by or blocking):
        return ""
    lines = [f"【Ticket #{number} 的關聯（拆單）】"]
    if parent:
        lines.append(f"母單 #{parent.get('number')} {parent.get('title', '')}")
        res = ticket_tools.get_ticket(int(parent["number"]))
        p = res.get("ticket") or {} if res.get("success") else {}
        if p.get("body"):
            lines.append(f"母單內文（含介面約定，必須遵守）：\n{_clip(p['body'], PARENT_CONTEXT_CHARS)}")
        siblings = [s for s in p.get("sub_issues") or [] if s.get("number") != number]
        if siblings:
            lines.append("同一母單的其他子單（由別張單負責，不要做它們的範圍）：")
            lines += [f"- {_ref_line(s)}" for s in siblings[:_MAX_RELATED]]
    if subs:
        lines.append("本單是母單，底下的子單：")
        lines += [f"- {_ref_line(s)}" for s in subs[:_MAX_RELATED]]
    if blocked_by:
        lines.append("被擋住（要先完成的單，可以依賴它們的成果）：")
        lines += [f"- {_ref_line(b)}" for b in blocked_by[:_MAX_RELATED]]
    if blocking:
        lines.append("擋住（本單完成後它們才能開始，介面要照約定交付）：")
        lines += [f"- {_ref_line(b)}" for b in blocking[:_MAX_RELATED]]
    lines.append("規則：只做本單範圍，依母單的介面約定實作；介面約定不夠或需要修改時，列為回報中的問題，不要自行改其他單的範圍。")
    return "\n".join(lines)


def with_ticket_context(task: str, number: int, notify: Optional[NotifyFn], include_plan: bool = True) -> str:
    """
    在規格後附上 ticket 標題與內文 (限制長度)；讀不到時照原文並提示使用者。
    include_plan：一併附上最新一則只讀規劃留言。規劃本身是留言不是內文，實作時 RD 需要它；
    只讀規劃派工 (plan=true) 不附，避免把過時的結論餵回去。
    """
    res = ticket_tools.get_ticket(int(number))
    if not res.get("success"):
        if "尚未設定" not in str(res.get("error", "")):
            _safe_notify(notify, f"⚠️ 讀不到 ticket #{number}（{str(res.get('error'))[:150]}），照原文派工。")
        return task
    t = res.get("ticket") or {}
    text = (f"{task}\n\n【Ticket #{number}：{t.get('title', '')}】\n"
            f"{_clip(t.get('body') or '（無內文）', TICKET_CONTEXT_CHARS)}")
    relations = ticket_relation_context(t, number)
    if relations:
        text += "\n\n" + relations
    if include_plan:
        # get_ticket 會把最新規劃完整放在 latest_plan；舊格式則從留言中找
        plans = [t["latest_plan"]] if t.get("latest_plan") else [
            c.get("body") or "" for c in t.get("comments") or [] if (c.get("body") or "").startswith(PLAN_COMMENT_PREFIX)]
        if plans:
            text += f"\n\n【Ticket #{number} 最新的只讀規劃（請依此實作；上方派工內容中的調整優先）】\n{_clip_plan(plans[-1], number)}"
    return text


def _clip_plan(plan: str, number: int) -> str:
    """規劃超過上限時用 _TRUNCATED_MARK 明確標記並請 RD 到 GitHub 讀完整版，不再無聲截斷；
    get_ticket 已截斷過 (帶標記) 的 latest_plan 不重複截斷"""
    if ticket_tools._TRUNCATED_MARK not in plan:
        plan = ticket_tools._clip_marked(plan, PLAN_CONTEXT_CHARS)
    if ticket_tools._TRUNCATED_MARK in plan:
        plan += (f"\n⚠️ 規劃未完整附上（後段可能含未決問題與驗收標準），"
                 f"請先用 `gh issue view {number} --comments` 讀完整規劃再實作。")
    return plan
