"""
RD 派工相關 UI 元件：停止按鈕、執行中清單 embed、開工通知字串、驗收卡 embed、開發完成 embed
"""

import asyncio
from typing import List
import discord
from confirm_view import CancelButtonView
from tools import rd_jobs
from tools.rd_common import SCOPE_TASK, job_tag
from tools.rd_dispatch import ANTIGRAVITY, ENGINE_LABELS, extract_ticket_ref


def _format_elapsed(seconds: int) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes} 分 {secs} 秒" if minutes else f"{secs} 秒"


def build_jobs_embed(jobs: List[dict]) -> discord.Embed:
    """/stop 的執行中派工清單 (jobs 為 DevJob.summary())"""
    embed = discord.Embed(title="🛠️ 執行中的 RD 派工", color=0xe67e22)
    if not jobs:
        embed.description = "目前沒有執行中的派工。"
        return embed
    lines = []
    for i, j in enumerate(jobs, 1):
        ticket = f"#{j['ticket']}" if j.get("ticket") else "無單號"
        state = "（停止中）" if j.get("cancelled") else ""
        lines.append(f"**{i}. {j['engine_label']}**{state}｜{ticket}｜scope `{j['scope']}`｜已執行 "
                     f"{_format_elapsed(j['elapsed_seconds'])}\n> {j['task']}")
    embed.description = "\n".join(lines)[:4000]
    embed.set_footer(text="按下方按鈕停止；停止不會改派給另一位工程師，也不會自動還原改動")
    return embed


def make_stop_view(job: rd_jobs.DevJob, owner_id: int) -> CancelButtonView:
    """派工訊息的「🛑 停止開發」按鈕：只有發出者能按；派工結束後由 attach_stop_button 停用"""
    async def stop() -> str:
        res = await asyncio.to_thread(rd_jobs.cancel_job, job.id, str(owner_id))
        return res["message"]
    return CancelButtonView(owner_id=owner_id, label="停止開發", on_cancel=stop, timeout=None)


def attach_stop_button(job: rd_jobs.DevJob, view: CancelButtonView, loop: asyncio.AbstractEventLoop) -> None:
    """派工結束 (完成、失敗或停止) 時停用按鈕 (回呼在背景執行緒中執行)"""
    async def disable() -> None:
        for item in view.children:
            item.disabled = True
        view.stop()
        if view.message:
            try:
                await view.message.edit(view=view)
            except discord.HTTPException:
                pass
    job.add_finish_callback(lambda: asyncio.run_coroutine_threadsafe(disable(), loop))


def job_announcement(job: rd_jobs.DevJob) -> str:
    """派工訊息 (附停止按鈕)：標明「#單號｜RD」，同時有多位 RD 時分得出是誰"""
    return (f"👷 **【{job_tag(job.ticket, job.engine_label)}】** 開始開發：{rd_jobs._first_line(job.task)}\n"
            "需要時可按下方按鈕停止（或使用 `/stop`）。")


class StopJobsView(discord.ui.View):
    """/stop 清單的停止按鈕 (每個派工一顆，最多 5 個)；只有下指令的使用者能按"""

    def __init__(self, owner_id: int, jobs: List[dict]):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        for i, j in enumerate(jobs[:5], 1):
            button = discord.ui.Button(label=f"停止 {i}. {j['engine_label']}", emoji="🛑",
                                       style=discord.ButtonStyle.danger)
            button.callback = self._make_callback(j["id"], button)
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("⛔ 只有下指令的使用者可以操作。", ephemeral=True)
            return False
        return True

    def _make_callback(self, job_id: str, button: discord.ui.Button):
        async def callback(interaction: discord.Interaction) -> None:
            res = await asyncio.to_thread(rd_jobs.cancel_job, job_id, str(interaction.user.id))
            button.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(res["message"])
        return callback


def scope_hint(task: str, scope: str) -> str:
    """斜線派工提到 #N 卻用 scope=task 時的提醒：ticket 多半要改 ArQ 本身"""
    if scope == SCOPE_TASK and extract_ticket_ref(task):
        return "⚠️ 這次是 scope: task（只動 task/）。這張單若要改 ArQ 本身，請改用 scope: core，才會有核心確認與驗收卡。\n"
    return ""


def build_dev_done_embed(
    res: dict,
    task: str,
    label: str,
    agy_model: str = "",
) -> discord.Embed:
    """RD 開發完成的成果卡片 Embed"""
    engine_label = res.get("engine_label") or label
    out = res.get("stdout") or f"({engine_label} 開發完成，無額外輸出)"
    # Embed description 上限 4096 字元：需求與成果各自截斷，合計留有餘裕
    task_preview = task if len(task) <= 500 else task[:500] + "…"
    model_line = f"**模型**：{agy_model}\n" if res.get("engine") == ANTIGRAVITY else ""
    embed = discord.Embed(
        title=f"🚀 {engine_label} 開發任務完成！",
        description=f"{model_line}**需求**：{task_preview}\n\n**執行成果**：\n```text\n{out[:3200]}\n```",
        color=0x2ecc71,
    )
    if res.get("attempts"):
        handoff = "\n".join(f"• {ENGINE_LABELS.get(a['engine'], a['engine'])}：{a['error'][:150]}" for a in res["attempts"])
        embed.add_field(name="🔁 接手紀錄 (先前工程師的失敗原因)", value=handoff[:1024], inline=False)
    if res.get("artifacts"):
        artifacts = "\n".join(f"`{a}`" for a in res["artifacts"][:8])
        embed.add_field(name="📁 新增/變更的成果物", value=artifacts[:1024], inline=False)
    if res.get("install_commands"):
        installs = "\n".join(f"`{c[:150]}`" for c in res["install_commands"][:8])
        embed.add_field(name="📦 經您同意執行的安裝指令", value=installs[:1024], inline=False)
    if res.get("interrupted_commands"):
        interrupted = "\n".join(f"`{c[:150]}`" for c in res["interrupted_commands"][:8])
        embed.add_field(name="⛔ 未經同意即開始執行而被中斷的指令", value=interrupted[:1024], inline=False)
    embed.set_footer(text=f"ArQ • PM & {engine_label} 自動開發體系")
    return embed


def build_acceptance_embed(acceptance: dict) -> discord.Embed:
    """核心修改 (scope=core) 的驗收卡：實際的 git 變更與單元測試結果，由使用者決定是否 commit"""
    passed = acceptance.get("tests_passed")
    embed = discord.Embed(
        title="🧾 驗收卡：ArQ 核心程式碼變更",
        description="以下為系統實際檢查結果（非工程師自述）。確認無誤後再 commit；改動需 `/reboot` 才生效。",
        color=0x2ecc71 if passed else 0xe74c3c,
    )
    diff = (acceptance.get("diff_stat") or "").strip() or "(沒有偵測到已追蹤檔案的變更)"
    embed.add_field(name="📄 變更檔案 (git diff --stat)", value=f"```text\n{diff[-950:]}\n```", inline=False)
    untracked = [l for l in (acceptance.get("status") or "").splitlines() if l.startswith("??")]
    if untracked:
        embed.add_field(name="🆕 新增檔案", value=f"```text\n{chr(10).join(untracked)[-950:]}\n```", inline=False)
    embed.add_field(
        name=f"🧪 單元測試：{'✅ 全數通過' if passed else '❌ 未通過'}",
        value=f"```text\n{(acceptance.get('tests_summary') or '')[-950:]}\n```",
        inline=False,
    )
    embed.add_field(
        name="➡️ 下一步",
        value="確認無誤請對 ArQ 說「commit」（或指定 commit 訊息），系統會再跳卡片確認後提交。",
        inline=False,
    )
    return embed
