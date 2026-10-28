"""
Discord 同意按鈕 UI

當 AI 要「安裝套件」或執行高風險操作時 (判定見 tools/risk_policy.py)，
bot.py 會用 ask_confirmation() 在頻道送出一張卡片，附「✅ 同意 / ❌ 拒絕」按鈕，
並等待「發出原始請求的使用者」按下。逾時未回應視同拒絕 (fail-closed)。
無風險的操作不會走到這裡，會直接執行。
"""

from typing import Optional

import discord

# 等待使用者按按鈕的秒數 (15 分鐘；Discord View 的計時從卡片送出時開始)
CONFIRM_TIMEOUT_SECONDS = 900


class ConfirmView(discord.ui.View):
    """只有 owner_id 本人能按的「同意 / 拒絕」按鈕組"""

    def __init__(self, owner_id: int, timeout: float = CONFIRM_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.approved: bool = False  # 預設 (含逾時) 為拒絕
        self.message: Optional[discord.Message] = None  # 由 ask_confirmation 於送出後設定

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 頻道中其他人看得到按鈕，但只有發出請求的人可以決定
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("⛔ 只有發出此請求的使用者可以決定。", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, approved: bool) -> None:
        self.approved = approved
        for item in self.children:
            item.disabled = True
        status = "✅ 已同意，開始執行…" if approved else "❌ 已拒絕"
        await interaction.response.edit_message(content=status, view=self)
        self.stop()

    @discord.ui.button(label="同意執行", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, True)

    @discord.ui.button(label="拒絕", emoji="❌", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, False)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(content="⌛ 逾時未回應，已自動拒絕", view=self)
            except discord.HTTPException:
                pass


async def ask_confirmation(
    channel: discord.abc.Messageable,
    owner_id: int,
    title: str,
    detail: str,
    reference: Optional[discord.Message] = None,
) -> bool:
    """
    在 channel 送出一張同意卡片並等待結果；回傳 True 代表使用者同意，拒絕或逾時皆回傳 False。
    reference：若提供，卡片會以「回覆」該訊息的形式顯示。
    """
    view = ConfirmView(owner_id=owner_id)
    embed = discord.Embed(
        title=f"🔐 需要您的同意：{title}"[:256],  # Embed title 上限 256 字元
        description=detail[:4000],               # Embed description 上限 4096 字元
        color=0xf1c40f,
    )
    embed.set_footer(text=f"請在 {CONFIRM_TIMEOUT_SECONDS // 60} 分鐘內決定，逾時將自動拒絕")
    view.message = await channel.send(embed=embed, view=view, reference=reference)
    await view.wait()
    return view.approved


class CancelButtonView(discord.ui.View):
    """
    只有 owner_id 本人能按的單一「取消」按鈕 (例如電腦重開機倒數期間)。
    on_cancel：按下時執行的 async 函式，回傳要顯示在訊息上的結果文字。逾時後按鈕自動停用。
    """

    def __init__(self, owner_id: int, label: str, on_cancel, timeout: float):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.on_cancel = on_cancel
        self.message: Optional[discord.Message] = None
        self.cancel_button.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("⛔ 只有發出此請求的使用者可以操作。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="取消", emoji="🛑", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.edit_message(content=await self.on_cancel(), view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
