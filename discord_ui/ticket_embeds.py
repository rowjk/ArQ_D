"""
Ticket 卡片排版與參數轉換：包含 list, view, new, similar hint 等 embed 組裝函式
"""

from typing import List, Optional
import discord
from discord_ui.text import clip_text, _clip_text
from tools.secret_guard import redact

_TICKET_STATUS_ICONS = {"todo": "⚪", "in-progress": "🟡", "blocked": "🔴", "review": "🟣", "done": "🟢"}


def _ticket_error_embed(title: str, res: dict) -> discord.Embed:
    return discord.Embed(title=title, description=clip_text(redact(str(res.get("error", "未知錯誤"))), 2000), color=0xe74c3c)


# /ticket list 的分組順序：依工作流程排列，沒有 status label 的放最後
_TICKET_STATUS_GROUPS = [("in-progress", "進行中"), ("review", "待 review"), ("blocked", "卡住"),
                         ("todo", "待辦"), ("done", "完成"), (None, "未標狀態")]
_FIELD_LIMIT = 1000   # Discord embed 欄位上限 1,024 字 (連結網址也算)，留一點餘裕
_EMBED_BUDGET = 5500  # 整個 embed 上限 6,000 字，留給標題、摘要與 footer


def _ticket_line(t: dict) -> str:
    return (f"[#{t['number']}]({t.get('url', '')}) `{t.get('priority') or '-'}` "
            f"{clip_text(t.get('title', ''), 60)}｜{t.get('opened_by') or '-'}")


def _add_ticket_group(embed: discord.Embed, name: str, tickets: List[dict], used: int) -> int:
    """
    把一組 ticket 加進 embed：超過單一欄位上限時接續在「（續）」欄位；
    整個 embed 快滿時以「還有 N 張」收尾。回傳目前已使用的字數。
    """
    lines: List[str] = []
    field_name = name
    for i, t in enumerate(tickets):
        line = _ticket_line(t)
        if used + len(field_name) + len("\n".join(lines + [line])) > _EMBED_BUDGET or len(embed.fields) >= 24:
            lines.append(f"…還有 {len(tickets) - i} 張（用 status 或 priority 篩選）")
            break
        if len("\n".join(lines + [line])) > _FIELD_LIMIT:
            embed.add_field(name=field_name, value="\n".join(lines), inline=False)
            used += len(field_name) + len("\n".join(lines))
            field_name, lines = f"{name.split(' (')[0]}（續）", []
        lines.append(line)
    if lines:
        embed.add_field(name=field_name, value="\n".join(lines), inline=False)
        used += len(field_name) + len("\n".join(lines))
    return used


def build_ticket_list_embed(res: dict, state: str = "open", by: str = "", priority: str = "",
                            status: str = "") -> discord.Embed:
    """把 ticket_tools.list_tickets 的結果依狀態分組成 embed；組內保持其優先順序排序"""
    if not res.get("success"):
        return _ticket_error_embed("❌ 無法取得 ticket", res)
    tickets = res.get("tickets") or []
    total = res.get("total", len(tickets))
    known = {s for s, _ in _TICKET_STATUS_GROUPS if s}
    embed = discord.Embed(title=f"📋 Tickets（{len(tickets)} 筆）", color=0x3498db)
    summary, used = [], len(embed.title) + 200
    for key, label in _TICKET_STATUS_GROUPS:
        group = [t for t in tickets if (t.get("status") if t.get("status") in known else None) == key]
        if not group:
            continue
        icon = _TICKET_STATUS_ICONS.get(key or "", "▫️")
        summary.append(f"{icon} {label} {len(group)}")
        used = _add_ticket_group(embed, f"{icon} {label} ({len(group)})", group, used)
    embed.description = "｜".join(summary) if summary else "沒有符合條件的 ticket。"
    filters = [f"state:{state}"] + [f"{k}:{v}" for k, v in (("by", by), ("status", status), ("priority", priority)) if v]
    footer = "篩選：" + " ".join(filters)
    if total > len(tickets):
        footer += f"｜顯示 {len(tickets)} / {total} 筆"
    embed.set_footer(text=footer)
    return embed


def build_ticket_view_embed(res: dict) -> discord.Embed:
    """把 ticket_tools.get_ticket() 的結果組成 embed：摘要、內文、最近 3 則留言、開單者不一致警告"""
    if not res.get("success"):
        return _ticket_error_embed("❌ 無法讀取 ticket", res)
    t = res["ticket"]
    embed = discord.Embed(
        title=clip_text(f"#{t['number']} {t.get('title', '')}", 256),
        url=t.get("url") or None,
        description=clip_text(t.get("body") or "（無內文）", 1500),
        color=0x3498db,
    )
    status = t.get("status") or "-"
    embed.add_field(name="狀態", value=f"{_TICKET_STATUS_ICONS.get(status, '▫️')} {status}", inline=True)
    embed.add_field(name="優先", value=t.get("priority") or "-", inline=True)
    embed.add_field(name="開單者", value=t.get("opened_by") or "-", inline=True)
    if t.get("opener_mismatch"):
        embed.add_field(
            name="⚠️ 開單者不一致",
            value=f"metadata：`{t.get('opened_by')}`｜label：`by:{t.get('opened_by_label')}`（以 metadata 為準）",
            inline=False,
        )
    # 母單、子單進度、相依
    if t.get("parent"):
        p = t["parent"]
        embed.add_field(name="🧭 母單", value=clip_text(f"#{p.get('number')} {p.get('title', '')}", 256), inline=False)
    subs = t.get("sub_issues") or []
    if subs:
        lines = [f"{'✅' if s.get('state') == 'closed' else _TICKET_STATUS_ICONS.get(s.get('status') or '', '▫️')} "
                 f"#{s.get('number')} {s.get('title', '')}" for s in subs[:10]]
        embed.add_field(name=f"🧩 子單 {t.get('sub_progress') or ''}", value=clip_text("\n".join(lines), 1024), inline=False)
    for key, title in (("blocked_by", "⛔ 被擋住（要先完成）"), ("blocking", "🚧 擋住")):
        items = t.get(key) or []
        if items:
            value = "\n".join(f"{'✅' if b.get('state') == 'closed' else '⏳'} #{b.get('number')} {b.get('title', '')}"
                              for b in items[:10])
            embed.add_field(name=title, value=clip_text(value, 1024), inline=False)
    comments = t.get("comments") or []
    recent = comments[-3:]
    if recent:
        value = "\n".join(
            f"**{c.get('author', '')}** {str(c.get('created_at', ''))[:10]}：{clip_text(c.get('body', ''), 280)}"
            for c in recent
        )
        embed.add_field(name=f"💬 最近留言（{len(recent)} / {len(comments)}）", value=clip_text(value, 1024), inline=False)
    if t.get("latest_plan"):  # 最新只讀規劃另外回傳，不在一般留言中
        embed.add_field(name="📝 最新只讀規劃（節錄）", value=clip_text(t["latest_plan"], 600), inline=False)
    embed.set_footer(text=f"{t.get('state', '')}｜在 GitHub 開啟可看完整內容")
    return embed


def _choice_value(choice, default: str) -> str:
    return getattr(choice, "value", choice) or default


def build_ticket_new_args(title: str, content: Optional[str] = None, type_=None, priority=None) -> dict:
    """/ticket new 的參數轉換：開單者固定為 user，type / priority 接受 Choice 或字串，缺的用預設值"""
    return {
        "title": (title or "").strip(),
        # Discord 的指令欄位不能輸入換行：內容中的 \n 轉成換行 (使用者決定)
        "body": (content or "").strip().replace("\\n", "\n"),
        "opened_by": "user",
        "type_": _choice_value(type_, "feature"),
        "priority": _choice_value(priority, "p2"),
    }


def build_ticket_new_embed(res: dict, args: dict) -> discord.Embed:
    """把 ticket_tools.create_ticket() 的結果組成回覆 embed：單號、連結與開單內容"""
    if not res.get("success"):
        return _ticket_error_embed("❌ 開單失敗", res)
    embed = discord.Embed(
        title=clip_text(f"✅ 已開單 #{res.get('number')}", 256),
        url=res.get("url") or None,
        description=clip_text(f"**{args.get('title', '')}**\n{res.get('url', '')}", 2000),
        color=0x2ecc71,
    )
    embed.add_field(name="開單者", value="by:user", inline=True)
    embed.add_field(name="類型", value=args.get("type_", ""), inline=True)
    embed.add_field(name="優先", value=args.get("priority", ""), inline=True)
    embed.set_footer(text="未填的段落標為「待細化」，之後可請 PM 細化")
    return embed


def build_similar_hint(candidates: List[dict], new_number) -> Optional[str]:
    """開單後的「可能重複」提示 (不攔截)；排除剛開的這張單，沒有其他候選時回傳 None"""
    others = [c for c in candidates if c.get("number") != new_number]
    if not others:
        return None
    lines = [f"• [#{c['number']}]({c.get('url', '')}) {clip_text(c.get('title', ''), 60)}"
             f"（{'已關閉' if c.get('state') == 'closed' else c.get('status') or '進行中'}）" for c in others]
    return clip_text("💡 **可能重複的單**（僅供參考，已照常開單）：\n" + "\n".join(lines)
                      + "\n需要合併時可以請 PM 處理，或關閉剛開的單。", 2000)
