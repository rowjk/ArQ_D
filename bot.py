"""
ArQ Discord 橋接主程式
讓你在手機端隨時與 PC 端 AI 助理對話、監控與控制。
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from typing import Callable, List, Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks

# 確保 Windows 終端輸出 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 讀取本地 .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agent import ArQAgent
from confirm_view import CancelButtonView, ask_confirmation
from tools.system_tools import get_heartbeat_line, get_system_status, run_powershell
from tools.screen_tools import take_screenshot
from tools.antigravity_tools import DEFAULT_MODEL as AGY_DEFAULT_MODEL
from tools.rd_dispatch import (ANTIGRAVITY, CLAUDE, ENGINE_LABELS, extract_ticket_ref, format_cancel_report,
                               run_dev_task)
from tools import rd_jobs
from tools.thread_bridge import ThreadBridge
from tools.rd_common import SCOPE_CORE, SCOPE_TASK, job_tag
from tools.restart_manager import RestartManager
from tools.job_tracker import JobTrackerManager
from tools.config import feature_summary
from tools.log_setup import setup_logging
from tools.secret_guard import redact, redact_obj
from tools import mcp_manager, pc_power, single_instance, ticket_tools
from discord_ui.text import clip_text, split_message, _clip_text  # noqa: F401
from discord_ui.ticket_embeds import (  # noqa: F401
    _TICKET_STATUS_ICONS, _choice_value, _ticket_error_embed, build_similar_hint,
    build_ticket_list_embed, build_ticket_new_args, build_ticket_new_embed, build_ticket_view_embed,
)
from discord_ui.jobs import (  # noqa: F401
    _format_elapsed, attach_stop_button, build_acceptance_embed, build_dev_done_embed,
    build_jobs_embed, job_announcement, make_stop_view, scope_hint, StopJobsView,
)

logger = logging.getLogger(__name__)

# 讀取環境變數
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: List[int] = [
    int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip().isdigit()
]

# 初始化 Discord Client
intents = discord.Intents.default()
intents.message_content = True  # 讀取訊息內容以支援自然語言溝通
bot = commands.Bot(command_prefix="!", intents=intents)

# 初始化 AI 代理核心
agent = ArQAgent()

# 初始化重啟管理器
restart_mgr = RestartManager()

# 初始化 104 職缺爬蟲 (JobTracker_104) 背景管理器
job_tracker_mgr = JobTrackerManager()

# 閒置互動時間追蹤 (用於 1 小時心跳打招呼)
last_interaction_time: float = time.time()
last_interaction_channel_id: Optional[int] = None

def record_interaction(channel_id: Optional[int] = None):
    """更新最後互動時間與頻道"""
    global last_interaction_time, last_interaction_channel_id
    last_interaction_time = time.time()
    if channel_id:
        last_interaction_channel_id = channel_id

def make_thread_notifier(channel) -> Callable[[str], None]:
    """
    產生一個可在「背景執行緒」呼叫的通知函式，把訊息送到指定頻道。
    用於 Claude Code 等在執行緒中長時間運行的任務。
    """
    async def send(text: str) -> None:
        for chunk in split_message(redact(text), limit=1900):
            await channel.send(chunk)

    return ThreadBridge().notifier(send)

def make_thread_approver(channel, owner_id: int) -> Callable[[str, str], bool]:
    """
    產生一個可在「背景執行緒」呼叫的同意函式：在頻道跳出同意卡片，並阻塞等待使用者決定。
    用於 Claude Code 開發途中要安裝套件 / 執行高風險指令時。
    """
    return ThreadBridge().approver(lambda title, detail: ask_confirmation(channel, owner_id, title, detail))

def is_authorized(user_id: int) -> bool:
    """
    【資安守門員】確保只有白名單的使用者能下指令。

    採「預設拒絕 (fail-closed)」：若 ALLOWED_USER_IDS 未設定或格式錯誤 (例如仍是 .env.example
    的佔位字串)，白名單為空，此時「所有人」都會被拒絕，而不是全部放行。
    本 bot 可以遠端執行 PowerShell，放行陌生人等同交出整台電腦的控制權。
    """
    return user_id in ALLOWED_USER_IDS

@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="手機指令 | /help"
        )
    )
    logger.info("=" * 60)
    logger.info(f"🚀 ArQ Discord Bridge 已啟動: {bot.user}")
    logger.info(f"🔒 授權使用者 ID: {ALLOWED_USER_IDS}")
    logger.info(f"🧠 AI 引擎狀態: {'✅ Gemini 啟用 (' + agent.model_name + ')' if agent.is_ai_ready else '⚡ 本地快捷模式 (無 API Key)'}")
    logger.info(f"🧩 功能：{feature_summary()}")
    logger.info("=" * 60)
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ 成功同步 {len(synced)} 個 Slash 指令 (/)")
    except Exception as e:
        logger.warning(f"⚠️ 同步 Slash 指令失敗: {e}", exc_info=True)

    # 啟動 1 小時閒置心跳檢查任務
    if not idle_heartbeat_loop.is_running():
        idle_heartbeat_loop.start()
        logger.info("💓 1 小時閒置心跳監聽已啟動")

    # 啟動 104 爬蟲 (JobTracker_104) 完成監看任務 (主動通知)
    if not crawler_watch_loop.is_running():
        crawler_watch_loop.start()
        logger.info("🕷️ 104 爬蟲完成主動通知監聽已啟動")

    # 背景掛載 MCP server：沒有 mcp_servers.json 時什麼都不做；連線不阻塞上線
    if mcp_manager.start_default():
        logger.info("🔌 MCP 外部工具背景連線中 (/mcp status 查看)")

    # 重啟前還在執行的背景派工：執行緒已隨程式結束，告知使用者哪些任務被中斷
    await report_interrupted_jobs()

    # 檢查是否有未完成的重啟狀態 (主動向使用者說我開機了)
    state = restart_mgr.consume_restart_state()
    if state and state.channel_id:
        try:
            target_dest = bot.get_channel(state.channel_id)
            if not target_dest:
                try:
                    target_dest = await bot.fetch_channel(state.channel_id)
                except Exception:
                    pass
            # 若為私訊 (DM) 或頻道無法取得，嘗試由 user_id 取得使用者物件發送
            if not target_dest and state.user_id:
                try:
                    target_dest = await bot.fetch_user(state.user_id)
                except Exception:
                    pass

            if target_dest:
                report = RestartManager.build_boot_report(state, get_system_status())
                embed = discord.Embed(
                    title=report["title"],
                    description=report["description"],
                    color=report["color"]
                )
                embed.set_footer(text="ArQ • 開機主動回報中樞")
                await target_dest.send(embed=embed)
                logger.info(f"📢 已成功向目標 {state.channel_id} 發送開機主動回報！")
        except Exception as e:
            logger.warning(f"⚠️ 發送重啟開機通知失敗: {e}", exc_info=True)

# -------------------------------------------------------------
# 背景心跳打招呼任務 (閒置滿 1 小時主動傳訊息)
# -------------------------------------------------------------

@tasks.loop(minutes=1)
async def idle_heartbeat_loop():
    """每分鐘檢查一次，若無任何互動超過 1 小時 (3600 秒) 且不在勿擾時段 (23:00-06:00) 則主動打招呼"""
    global last_interaction_time
    now = time.time()
    idle_seconds = now - last_interaction_time

    if idle_seconds >= 3600:
        # 勿擾時段 (23:00 ~ 06:00) 不主動發送心跳
        current_hour = datetime.now().hour
        if current_hour >= 23 or current_hour < 6:
            last_interaction_time = now  # 處於勿擾時段，順延計時避免半夜打擾
            return

        last_interaction_time = now  # 重置計時器，避免重複發送

        greeting = await asyncio.to_thread(get_heartbeat_line)

        sent = False
        # 優先發送至上次互動的頻道
        if last_interaction_channel_id:
            channel = bot.get_channel(last_interaction_channel_id)
            if channel:
                try:
                    await channel.send(greeting)
                    sent = True
                    logger.info(f"💓 已在頻道發送 1 小時閒置心跳打招呼")
                except Exception as e:
                    logger.warning(f"⚠️ 在頻道發送心跳失敗: {e}", exc_info=True)

        # 若無上次頻道，發送至授權擁有者的私訊 (DM)
        if not sent and ALLOWED_USER_IDS:
            try:
                user = await bot.fetch_user(ALLOWED_USER_IDS[0])
                if user:
                    await user.send(greeting)
                    logger.info(f"💓 已向使用者 {user} 發送 1 小時閒置心跳私訊 (DM)")
            except Exception as e:
                logger.warning(f"⚠️ 私訊發送心跳失敗: {e}", exc_info=True)

# -------------------------------------------------------------
# 背景監看任務：104 爬蟲 (JobTracker_104) 完成後主動通知
# -------------------------------------------------------------
# 設計理由：爬蟲本體單次執行約 16.5~18 分鐘，且可能由 CLI 手動啟動、Windows
# 工作排程器、或未來 /job104 start 等不同管道觸發，ArQ 事件迴圈不應也不能同步
# 阻塞等待。改用「常駐 tasks.loop 週期輪詢 crawler_state.json」的被動監看模式：
# 只要該次執行是透過 JobTrackerManager.start() (tools/job_tracker.py) 啟動 (會寫入 pid/notified 狀態)，
# 無論觸發來源為何，行程結束後下一次輪詢就會被偵測到並主動推播一次 (notified
# 旗標確保冪等，不會重複通知)。即使 ArQ 服務中途重啟，只要狀態檔仍標記
# notified=False，重啟後的下一次輪詢一樣會補發通知，不會遺漏。
@tasks.loop(seconds=90)
async def crawler_watch_loop():
    """每 90 秒檢查一次 104 爬蟲是否剛結束且尚未主動通知，若是則主動推播結果"""
    try:
        result = await asyncio.to_thread(job_tracker_mgr.check_completion_for_notify)
    except Exception as e:
        logger.warning(f"⚠️ 104 爬蟲完成狀態檢查失敗: {e}", exc_info=True)
        return

    if not result:
        return

    last_log = result.get("last_log") or {}
    record = last_log.get("record") or {}
    source = last_log.get("source")

    if record:
        summary_lines = "\n".join(f"• {k}：{v}" for k, v in record.items())
    else:
        summary_lines = "⚠️ 無法解析執行摘要，請用 `/job104 lastlog` 查看細節。"

    source_note = "Google Sheets Run_Logs" if source == "google_sheets" else "本地 Log 備援"
    msg = (
        "🎉 **104 職缺爬蟲 (JobTracker_104) 已完成！**\n\n"
        f"🕒 啟動時間：`{result.get('started_at_str')}`\n"
        f"📊 執行摘要 (來源: {source_note})：\n{summary_lines}\n\n"
        "需要更多細節可用 `/job104 status` 或 `/job104 lastlog` 查詢。"
    )

    sent = False
    if last_interaction_channel_id:
        channel = bot.get_channel(last_interaction_channel_id)
        if channel:
            try:
                await channel.send(msg)
                sent = True
                logger.info("📢 已於頻道發送 104 爬蟲完成主動通知")
            except Exception as e:
                logger.warning(f"⚠️ 頻道發送 104 爬蟲完成通知失敗: {e}", exc_info=True)

    if not sent and ALLOWED_USER_IDS:
        try:
            user = await bot.fetch_user(ALLOWED_USER_IDS[0])
            if user:
                await user.send(msg)
                logger.info(f"📢 已私訊使用者 {user} 104 爬蟲完成通知")
        except Exception as e:
            logger.warning(f"⚠️ 私訊 104 爬蟲完成通知失敗: {e}", exc_info=True)

# -------------------------------------------------------------
# Slash Commands (斜線快捷指令)
# -------------------------------------------------------------

@bot.tree.command(name="status", description="🖥️ 查看 PC 系統資源 (CPU, 記憶體, 磁碟, 運行時間)")
async def cmd_status(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return

    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)
    s = get_system_status()
    embed = discord.Embed(
        title="🖥️ PC 系統健康與運行狀態",
        color=0x2b82d9
    )
    embed.add_field(name="作業系統", value=f"`{s['os']}`", inline=False)
    embed.add_field(name="CPU 使用率", value=f"`{s['cpu_usage']}`", inline=True)
    embed.add_field(name="記憶體使用", value=f"`{s['memory_used']}`", inline=True)
    embed.add_field(name="C 槽空間", value=f"`{s['disk_c']}`", inline=False)
    embed.add_field(name="開機時間 / 累計", value=f"`{s['boot_time']}`\n(已開機 {s['uptime']})", inline=False)
    embed.set_footer(text="ArQ • 手機遠端中樞")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="screenshot", description="📸 截取電腦當前螢幕畫面並回傳到手機")
async def cmd_screenshot(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return

    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)
    try:
        img_path = await asyncio.to_thread(take_screenshot)
        # 截圖過大時 take_screenshot 會轉成 JPEG，附件檔名需沿用實際副檔名，否則與內容格式不符
        attach_name = "screen" + os.path.splitext(img_path)[1].lower()
        file = discord.File(img_path, filename=attach_name)
        embed = discord.Embed(title="📸 電腦螢幕即時畫面", color=0x2ecc71)
        embed.set_image(url=f"attachment://{attach_name}")
        embed.set_footer(text="ArQ • 螢幕即時擷取")
        await interaction.followup.send(file=file, embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ 截圖失敗: {e}")

@bot.tree.command(name="cmd", description="💻 在電腦上執行 PowerShell 指令")
@app_commands.describe(command="要執行的 PowerShell 指令 (例: ipconfig, dir, git status)")
async def cmd_run(interaction: discord.Interaction, command: str):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return

    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)
    res = await run_powershell(command)
    if res["success"]:
        out = res["stdout"] or "(執行完成，無輸出內容)"
        msg = f"✅ **指令執行成功**: `{command}`\n```text\n{out}\n```"
    else:
        err = res["stderr"] or res.get("error", "未知錯誤")
        msg = f"❌ **指令執行失敗**: `{command}`\n```text\n{err}\n```"
    msg = redact(msg)  # 指令輸出若帶到 .env 的金鑰 / Token，送出前遮蔽

    if len(msg) > 2000:
        msg = msg[:1990] + "\n...(內容過長已截斷)```"
    await interaction.followup.send(msg)

@bot.tree.command(name="heartbeat", description="💓 查看當前心跳狀態或立即測試打招呼")
async def cmd_heartbeat(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return

    record_interaction(interaction.channel_id)
    await interaction.response.send_message(await asyncio.to_thread(get_heartbeat_line))

@bot.tree.command(name="job104", description="🕷️ 管理 104 職缺爬蟲 (JobTracker_104)：啟動 / 查詢狀態 / 最新結果")
@app_commands.describe(action="要執行的動作")
@app_commands.choices(action=[
    app_commands.Choice(name="start - 背景啟動爬蟲 (完成後會主動通知)", value="start"),
    app_commands.Choice(name="status - 查詢目前是否在執行", value="status"),
    app_commands.Choice(name="lastlog - 查看最新一次執行結果", value="lastlog"),
])
async def cmd_job104(interaction: discord.Interaction, action: app_commands.Choice[str]):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return

    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)

    if action.value == "start":
        res = await asyncio.to_thread(job_tracker_mgr.start)
        if res.get("ok"):
            await interaction.followup.send(
                f"✅ **104 爬蟲已於背景啟動！**\n"
                f"PID: `{res['pid']}` | 啟動時間: `{res['started_at']}`\n"
                f"預計耗時：約 {res['expected_duration_min']} 分鐘，完成後我會主動回報結果，不需要再手動查詢！"
            )
        else:
            await interaction.followup.send(f"❌ 啟動失敗：{res.get('error')}")
        return

    if action.value == "status":
        res = await asyncio.to_thread(job_tracker_mgr.status)
        if res.get("running"):
            await interaction.followup.send(f"🟢 **爬蟲執行中**\nPID: `{res['pid']}` | 已執行時間: `{res['elapsed_str']}` (mm:ss)")
        elif "pid" in res:
            await interaction.followup.send(f"🔴 **爬蟲未在執行**（上次 PID `{res['pid']}`，啟動於 `{res['started_at']}`）")
        else:
            await interaction.followup.send(f"⚪ {res.get('message')}")
        return

    # lastlog
    res = await asyncio.to_thread(job_tracker_mgr.last_log)
    record = res.get("record") or {}
    if res.get("ok") and record:
        lines = "\n".join(f"• {k}：{v}" for k, v in record.items())
        await interaction.followup.send(f"📊 **最新執行摘要** (來源: {res.get('source')})\n{lines}")
    else:
        await interaction.followup.send(f"❌ 無法取得最新執行摘要：{res.get('error')}")

# -------------------------------------------------------------
# /ticket：直接查 GitHub Issues 上的 ticket (不經 Gemini、不花 Token)；改單請用自然語言或 GitHub App
# -------------------------------------------------------------

ticket_group = app_commands.Group(name="ticket", description="📋 查看 / 開 ticket（GitHub Issues），不花 Gemini Token")

@ticket_group.command(name="list", description="列出 ticket（依優先順序）")
@app_commands.describe(by="開單者", state="開啟 / 關閉（預設 open；要看已完成的單選 all 或 closed）",
                       status="工作狀態（todo / in-progress / blocked / review / done）", priority="優先順序")
@app_commands.choices(
    by=[app_commands.Choice(name=v, value=v) for v in ticket_tools.OPENERS],
    state=[app_commands.Choice(name=v, value=v) for v in ticket_tools.STATES],
    status=[app_commands.Choice(name=v, value=v) for v in ticket_tools.STATUSES],
    priority=[app_commands.Choice(name=v, value=v) for v in ticket_tools.PRIORITIES],
)
async def cmd_ticket_list(
    interaction: discord.Interaction,
    by: Optional[app_commands.Choice[str]] = None,
    state: Optional[app_commands.Choice[str]] = None,
    status: Optional[app_commands.Choice[str]] = None,
    priority: Optional[app_commands.Choice[str]] = None,
):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return
    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)
    args = {
        "state": state.value if state else "open",
        "opened_by": by.value if by else "",
        "status": status.value if status else "",
        "priority": priority.value if priority else "",
    }
    res = await asyncio.to_thread(ticket_tools.list_tickets, **args)
    await interaction.followup.send(embed=build_ticket_list_embed(
        res, state=args["state"], by=args["opened_by"], priority=args["priority"], status=args["status"]))

@ticket_group.command(name="view", description="查看單張 ticket 的內容與最近留言")
@app_commands.describe(number="ticket 編號，例如 5")
async def cmd_ticket_view(interaction: discord.Interaction, number: app_commands.Range[int, 1]):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return
    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)
    res = await asyncio.to_thread(ticket_tools.get_ticket, number)
    await interaction.followup.send(embed=build_ticket_view_embed(res))

_BACKGROUND_TASKS: set = set()  # 背景任務的參照，避免被回收

async def _send_similar_hint(interaction: discord.Interaction, args: dict, new_number) -> None:
    """背景比對相似單並補送提示；任何失敗都只記錄，不影響已完成的開單"""
    try:
        res = await asyncio.to_thread(ticket_tools.find_similar, args.get("title", ""), args.get("body", ""))
        text = build_similar_hint(res.get("candidates") or [], new_number) if res.get("success") else None
        if text:
            await interaction.followup.send(text)
    except Exception as e:
        logger.warning(f"⚠️ 相似單提示失敗: {e}", exc_info=True)

@ticket_group.command(name="new", description="直接開單記下想法（不經 Gemini、不花 Token）")
@app_commands.describe(title="標題", content="內容（可省略；用 \\n 換行；會放進「使用者故事」，其餘段落標為待細化）",
                       type="類型（預設 feature）", priority="優先順序（預設 p2）")
@app_commands.choices(
    type=[app_commands.Choice(name=v, value=v) for v in ticket_tools.TYPES],
    priority=[app_commands.Choice(name=v, value=v) for v in ticket_tools.PRIORITIES],
)
async def cmd_ticket_new(
    interaction: discord.Interaction,
    title: app_commands.Range[str, 1, 200],
    content: Optional[app_commands.Range[str, 1, 3000]] = None,
    type: Optional[app_commands.Choice[str]] = None,
    priority: Optional[app_commands.Choice[str]] = None,
):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return
    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)
    args = build_ticket_new_args(title, content, type, priority)
    res = await asyncio.to_thread(ticket_tools.create_ticket, **args)
    await interaction.followup.send(embed=build_ticket_new_embed(res, args))
    if res.get("success"):
        # 不攔截、不拖慢開單：回覆送出後才在背景比對相似單 (使用者決定)
        task = asyncio.create_task(_send_similar_hint(interaction, args, res.get("number")))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

bot.tree.add_command(ticket_group)

@bot.tree.command(name="mcp", description="🔌 查看 MCP 外部工具狀態，或重新載入 mcp_servers.json")
@app_commands.describe(action="status 查看各 server 狀態；reload 重新讀檔並重連（新增或變更的 server 會先請你核可）")
@app_commands.choices(action=[
    app_commands.Choice(name="status - 查看狀態", value="status"),
    app_commands.Choice(name="reload - 重新載入並重連", value="reload"),
])
async def cmd_mcp(interaction: discord.Interaction, action: app_commands.Choice[str]):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return
    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)
    manager = mcp_manager.get_manager()
    if action.value == "reload":
        approve = make_thread_approver(interaction.channel, interaction.user.id)
        await asyncio.to_thread(manager.reload, approve)  # 對話的工具清單會在下一則訊息自動重建
    text = redact(manager.status_text())[:1900]
    try:
        await interaction.followup.send(text)
    except discord.HTTPException:
        # 等核可卡加上連線超過 15 分鐘時 interaction token 已過期，改直接送到頻道
        await interaction.channel.send(text)

@bot.tree.command(name="help", description="📖 顯示 ArQ 說明手冊與操作提示")
async def cmd_help(interaction: discord.Interaction):
    record_interaction(interaction.channel_id)
    embed = discord.Embed(
        title="🤖 ArQ 遠端 AI 中樞使用手冊",
        description="您可以透過本頻道或直接與我 **私人私訊 (DM)** 進行交流！",
        color=0x9b59b6
    )
    embed.add_field(name="💬 自然語言對話", value="直接輸入文字發問（例：「幫我截圖」、「查一下現在電腦狀態」、「跑一下 git status」、「讓 Claude Code 開發一個爬蟲」），AI 大腦會自動判斷並執行對應工具！", inline=False)
    embed.add_field(name="⚡ 常用斜線快捷指令", value="• `/screenshot` - 立即截取螢幕\n• `/status` - 查看硬體狀態\n• `/cmd <指令>` - 執行 PowerShell\n• `/claude <需求> [scope]` - 指派 Claude Code 開發 (scope=core 可改 ArQ 本身)，失敗由 agy 接手\n• `/agy <需求> [model] [scope]` - 指派 Antigravity 開發，失敗由 Claude 接手\n• `/stop` - 列出執行中的派工並強制停止 (派工訊息也有 🛑 按鈕)\n• `/mcp status|reload` - 查看 / 重新載入 MCP 外部工具\n• `/job104 <start/status/lastlog>` - 管理 104 爬蟲，完成後會主動通知\n• `/ticket list [by] [state] [status] [priority]`、`/ticket view <編號>` - 查看 ticket，依狀態分組（不花 Gemini Token）\n• `/ticket new <標題> [內容] [type] [priority]` - 直接開單記想法，開單者 by:user（不經 Gemini、不花 Token）\n• `/reboot` - 重新啟動 ArQ 程式 (不會重開電腦，需確認)\n• `/pc-reboot` - 重新啟動整台電腦 (需確認，倒數期間可取消)\n• `/heartbeat` - 心跳狀態與測試\n• `/reset` - 清空對話記憶\n• `/help` - 顯示此說明", inline=False)
    embed.add_field(name="🔒 安全防護", value="本機器人已啟用使用者白名單，只有您的帳號可操控。\n一般操作會直接執行；**安裝套件**或高風險操作（刪除檔案、關機、改系統設定、git push…）會先跳出按鈕請您同意。", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------------------------------------------------
# 強制停止 RD 開發：派工訊息的 🛑 按鈕、/stop 指令 (登記表與停止邏輯見 tools/rd_jobs.py)
# -------------------------------------------------------------

async def _resolve_job_channel(job: rd_jobs.DevJob):
    """
    找出派工的原頻道：優先用 ORIGIN 保存的頻道物件，其次快取 (get_channel)，最後向 Discord 查詢 (fetch_channel)。
    DM 頻道不在快取中，只用 get_channel 會回傳 None。都找不到時回傳 None。
    """
    if job.channel is not None:
        return job.channel
    if not job.channel_id:
        return None
    channel = bot.get_channel(job.channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(job.channel_id)
    except (discord.HTTPException, discord.InvalidData) as e:
        logger.warning(f"⚠️ 找不到派工的原頻道 {job.channel_id}: {e}", exc_info=True)
        return None

def _announce_job(job: rd_jobs.DevJob) -> None:
    """Gemini 派工 (rd_dispatch 自動建立的 DevJob)：在原頻道送出附停止按鈕的訊息 (背景執行緒中呼叫)"""
    if job.owner_id is None or (job.channel is None and not job.channel_id):
        logger.warning("⚠️ 派工沒有來源頻道，無法送出停止按鈕 (可用 /stop)")
        return
    loop = bot.loop

    async def send() -> None:
        channel = await _resolve_job_channel(job)
        if channel is None:
            logger.warning("⚠️ 無法送出派工停止按鈕 (可用 /stop)")
            return
        view = make_stop_view(job, job.owner_id)
        view.message = await channel.send(job_announcement(job), view=view)
        attach_stop_button(job, view, loop)
    asyncio.run_coroutine_threadsafe(send(), loop).result(timeout=15)

async def _fetch_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    return channel

async def report_interrupted_jobs() -> None:
    """開機時回報上次結束時還在執行的派工，依原頻道分組各送一則"""
    by_channel: dict = {}
    for entry in rd_jobs.take_interrupted():
        by_channel.setdefault(entry.get("channel_id"), []).append(entry)
    for channel_id, entries in by_channel.items():
        channel = await _fetch_channel(channel_id) if channel_id else None
        if channel is None:
            logger.warning(f"⚠️ 有 {len(entries)} 個派工因重啟中斷，但找不到原頻道 {channel_id}")
            continue
        try:
            await channel.send(rd_jobs.format_interrupted(entries))
        except Exception as e:
            logger.warning(f"⚠️ 回報中斷的派工失敗: {e}", exc_info=True)

rd_jobs.set_announcer(_announce_job)

@bot.tree.command(name="stop", description="🛑 列出執行中的 RD 派工，並可強制停止")
async def cmd_stop(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return
    record_interaction(interaction.channel_id)
    jobs = [j.summary() for j in rd_jobs.list_jobs()]
    embed = build_jobs_embed(jobs)
    if jobs:
        await interaction.response.send_message(embed=embed, view=StopJobsView(interaction.user.id, jobs))
    else:
        await interaction.response.send_message(embed=embed)

async def run_slash_dev_task(
    interaction: discord.Interaction,
    task: str,
    preferred: str,
    agy_model: str = AGY_DEFAULT_MODEL,
    scope: str = SCOPE_TASK,
) -> None:
    """
    /claude 與 /agy 共用的派工流程 (使用者親自下的指令，派工本身不再確認，scope=core 也一樣)。
    scope=core 完成後會附上驗收卡 (git diff --stat + 全部單元測試結果)，使用者確認後再自行 commit。

    透過 tools/rd_dispatch.run_dev_task 執行：首選工程師沒反應或出錯 (額度 / Token 用完、未登入、逾時…)
    時，自動改由另一位工程師接手，並在頻道通知。開發途中的安裝套件 / 高風險指令會在此頻道跳出同意卡片。
    """
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return

    record_interaction(interaction.channel_id)
    await interaction.response.defer(thinking=True)

    label = ENGINE_LABELS[preferred] + (f" ({agy_model})" if preferred == ANTIGRAVITY else "")
    backup = ENGINE_LABELS[ANTIGRAVITY if preferred == CLAUDE else CLAUDE]
    scope_note = "🛠️ 範圍：**ArQ 核心程式碼**（完成後附驗收卡，確認後再 commit）\n" if scope == SCOPE_CORE else ""
    job = rd_jobs.DevJob(task=task, engine_label=ENGINE_LABELS[preferred], scope=scope,
                         ticket=extract_ticket_ref(task),  # 需求含 #N 時派工訊息標出單號
                         owner_id=interaction.user.id, channel_id=interaction.channel_id)
    stop_view = make_stop_view(job, interaction.user.id)
    stop_view.message = await interaction.followup.send(
        f"📋 **ArQ (PM) 任務派發通知**：\n{scope_note}{scope_hint(task, scope)}"
        f"已指派工程師 **【{job_tag(job.ticket, label)}】** 進行開發（若無法完成將由 **{backup}** 接手）：\n> {task[:1500]}\n"
        f"⏳ 正在本機終端機中自動編碼、驗證與測試，完成後將即時回報成果...（需要時可按下方按鈕停止）",
        view=stop_view, wait=True,
    )
    attach_stop_button(job, stop_view, asyncio.get_running_loop())

    res = await asyncio.to_thread(
        run_dev_task,
        task,
        preferred,
        agy_model=agy_model,
        notify=make_thread_notifier(interaction.channel),
        approve=make_thread_approver(interaction.channel, interaction.user.id),
        scope=scope,
        opened_by="user",  # 使用者親自下的斜線指令：自動開單，開單者為 user
        ticket=extract_ticket_ref(task) or 0,  # 需求提到 #N 時沿用該單並帶入內容
        job=job,
    )
    res = redact_obj(res)  # 工程師輸出若帶到 .env 的金鑰 / Token，送出前遮蔽

    if res.get("cancelled"):
        for chunk in split_message(format_cancel_report(res), limit=1900):
            await interaction.channel.send(chunk)
        return

    if res.get("scope_violation"):
        files = "\n".join(f"• `{p[:150]}`" for p in res["scope_violation"][:8])
        await interaction.channel.send(
            "⚠️ **範圍越界**：以 scope=task 派工，但工程師改到了 ArQ 本身的檔案"
            f"（已照 scope=core 附上驗收卡，請確認後再 commit）：\n{files}"[:2000])

    if res.get("blocked_commands"):
        blocked = "\n".join(f"• `{c[:150]}`" for c in res["blocked_commands"][:8])
        await interaction.channel.send(f"🚫 **以下指令未獲您同意，已被阻擋**：\n{blocked}"[:2000])

    if not res.get("success"):
        err = res.get("error") or res.get("stderr") or "未知錯誤"
        await interaction.channel.send(f"❌ **開發失敗**：\n```text\n{err[:1800]}\n```")
        return

    embed = build_dev_done_embed(res, task, label, agy_model)
    await interaction.channel.send(embed=embed)

    acceptance = res.get("acceptance")
    if acceptance:
        await interaction.channel.send(embed=build_acceptance_embed(acceptance))

@bot.tree.command(name="claude", description="👨‍💻 指派 Claude Code (RD 工程師) 開發；無法完成時由 Antigravity 接手")
@app_commands.describe(
    task="要讓 Claude Code 開發的任務需求 (例: 在 task/ 下寫一個抓取即時匯率並存成 CSV 的腳本)",
    scope="工作範圍：task (預設，只動 task/) 或 core (修改 ArQ 本身)",
)
@app_commands.choices(scope=[
    app_commands.Choice(name="task - 一般任務 (只動 task/，預設)", value="task"),
    app_commands.Choice(name="core - 修改 ArQ 本身 (完成後附驗收卡)", value="core"),
])
async def cmd_claude(interaction: discord.Interaction, task: str, scope: Optional[app_commands.Choice[str]] = None):
    await run_slash_dev_task(interaction, task, CLAUDE, scope=scope.value if scope else SCOPE_TASK)

@bot.tree.command(name="agy", description="🪐 指派 Antigravity CLI (第二位 RD 工程師) 開發；無法完成時由 Claude Code 接手")
@app_commands.describe(
    task="要讓 Antigravity 開發的任務需求 (例: 在 task/ 下寫一個抓取即時匯率並存成 CSV 的腳本)",
    model="使用的模型 (預設 gemini-3.8-flash-high，可選 gemini-3.8-flash-medium/low、claude-sonnet-4-6、claude-opus-4-6-thinking、gpt-oss-120b-medium)",
    scope="工作範圍：task (預設，只動 task/) 或 core (修改 ArQ 本身)",
)
@app_commands.choices(scope=[
    app_commands.Choice(name="task - 一般任務 (只動 task/，預設)", value="task"),
    app_commands.Choice(name="core - 修改 ArQ 本身 (完成後附驗收卡)", value="core"),
])
async def cmd_agy(interaction: discord.Interaction, task: str, model: str = AGY_DEFAULT_MODEL,
                  scope: Optional[app_commands.Choice[str]] = None):
    await run_slash_dev_task(interaction, task, ANTIGRAVITY, agy_model=model,
                             scope=scope.value if scope else SCOPE_TASK)

# 自然語言觸發詞：明確提到「電腦 / 主機」才重開整台電腦
PC_REBOOT_TRIGGERS = {
    "重開電腦", "重啟電腦", "重開主機", "重啟主機", "電腦重開機", "重新啟動電腦", "幫我重開電腦", "請幫我重開電腦",
}
PC_REBOOT_CANCEL_TRIGGERS = {"取消重開機", "取消重開電腦", "取消重啟電腦", "不要重開機"}

REBOOT_CONFIRM_DETAIL = "確定要重新啟動 ArQ 機器人服務嗎？\n重啟大約需要 2~5 秒，完成後我會在此頻道主動回報「我開機了」！"

async def perform_reboot(channel, channel_id: int, user_id: int, guild_id: Optional[int], reason: str) -> None:
    """
    /reboot 與自然語言「重開機」共用的重啟流程 (呼叫前須已取得使用者確認)。
    「重開機」一律指重啟 ArQ 程式本體，不會重開實體 PC。
    """
    # 防死循環：10 分鐘內重啟達 5 次即拒絕，避免異常狀況下無限重啟 (見 RestartManager.is_restart_looping)
    if restart_mgr.is_restart_looping():
        await channel.send(
            "⚠️ **偵測到短時間內重啟過於頻繁（10 分鐘內 5 次），已暫停本次重啟以防死循環。**\n"
            "請稍候幾分鐘再試，或到電腦前確認 ArQ 的終端機輸出。"
        )
        return

    restart_mgr.record_restart_intent(
        restart_type="bot",
        user_id=user_id,
        channel_id=channel_id,
        reason=reason,
        guild_id=guild_id,
    )
    await channel.send("🔄 **正在重新啟動 ArQ 服務，請稍候...**")
    await asyncio.to_thread(agent.close)  # 刪除 Gemini 顯式快取 (重啟走 os._exit，不會觸發 atexit)
    await bot.close()
    # bot.close() 之後不可再 await：bot.run() 會隨即結束並取消本工作，程式以 exit 0 退出，run.ps1 就不會重啟
    restart_mgr.execute_bot_restart()

@bot.tree.command(name="reboot", description="🔄 重新啟動 ArQ 機器人服務 (俗稱重開機，不會重開電腦；需要二次確認)")
async def cmd_reboot(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return

    record_interaction(interaction.channel_id)
    # Discord 要求斜線指令 3 秒內必須回應，否則會顯示「此互動失敗」；先回一則僅自己可見的提示，再送確認卡片
    await interaction.response.send_message("🔐 請在下方卡片確認是否重新啟動 ArQ 服務。", ephemeral=True)

    approved = await ask_confirmation(
        channel=interaction.channel,
        owner_id=interaction.user.id,
        title="🔄 重新啟動 ArQ 服務",
        detail=REBOOT_CONFIRM_DETAIL,
    )
    if not approved:
        await interaction.channel.send("❌ 已取消重啟操作。")
        return

    await perform_reboot(
        interaction.channel, interaction.channel_id, interaction.user.id, interaction.guild_id,
        "使用者透過 /reboot 指令重啟",
    )

# -------------------------------------------------------------
# 電腦重新開機 (/pc-reboot)：真的重開整台電腦，與 /reboot (只重啟 ArQ 程式) 不同
# 流程：風險檢查 → 確認卡片 → 記錄狀態 → 延遲 shutdown /r → 倒數期間可按按鈕取消 → 開機上線後回報
# -------------------------------------------------------------

async def _cancel_pc_reboot_text() -> str:
    res = await asyncio.to_thread(pc_power.cancel_reboot)
    if res["success"]:
        restart_mgr.clear_state()  # 取消了就不該在下次啟動時回報「電腦重新開機完成」
        return "✅ **已取消電腦重新開機。**"
    return f"⚠️ {res['message']}"

async def perform_pc_reboot(channel, channel_id: int, user_id: int, guild_id: Optional[int], reason: str,
                            delay_seconds: int = pc_power.DEFAULT_DELAY_SECONDS,
                            reference: Optional[discord.Message] = None) -> None:
    """/pc-reboot 與自然語言「重開電腦」共用的流程 (本函式內會先跳確認卡片)"""
    info = await asyncio.to_thread(pc_power.preflight)
    approved = await ask_confirmation(
        channel, user_id, "🖥️ 重新啟動整台電腦",
        pc_power.describe_preflight(info, delay_seconds), reference=reference,
    )
    if not approved:
        await channel.send("❌ 已取消電腦重新開機。")
        return

    # 先記錄狀態 (開機上線後 on_ready 會讀取並回報)，再排定重開機
    restart_mgr.record_restart_intent(restart_type="pc", user_id=user_id, channel_id=channel_id,
                                      reason=reason, guild_id=guild_id)
    res = await asyncio.to_thread(restart_mgr.execute_pc_reboot, delay_seconds)
    if not res["success"]:
        restart_mgr.clear_state()
        await channel.send(f"❌ 無法排定重新開機：{res.get('error')}")
        return

    delay = res["delay_seconds"]
    view = CancelButtonView(owner_id=user_id, label="取消重開機", on_cancel=_cancel_pc_reboot_text, timeout=delay)
    view.message = await channel.send(
        f"🖥️ **電腦將在 {delay} 秒後重新開機。** 需要的話請按下方按鈕取消（或傳「取消重開機」）。\n"
        "開機並自動上線後，我會在這裡回報。",
        view=view,
    )

@bot.tree.command(name="pc-reboot", description="🖥️ 重新啟動整台電腦 (會先檢查風險並請你確認，倒數期間可取消)")
@app_commands.describe(action="要執行的動作", delay="幾秒後重開機 (10~3600，預設 60)")
@app_commands.choices(action=[
    app_commands.Choice(name="reboot - 重新開機 (會先跳確認卡片)", value="reboot"),
    app_commands.Choice(name="cancel - 取消已排定的重新開機", value="cancel"),
])
async def cmd_pc_reboot(interaction: discord.Interaction,
                        action: Optional[app_commands.Choice[str]] = None,
                        delay: app_commands.Range[int, pc_power.MIN_DELAY_SECONDS, pc_power.MAX_DELAY_SECONDS] = pc_power.DEFAULT_DELAY_SECONDS):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return
    record_interaction(interaction.channel_id)

    if action and action.value == "cancel":
        await interaction.response.send_message(await _cancel_pc_reboot_text())
        return

    # 先回應互動 (3 秒內)，風險檢查與確認卡片接著送出
    await interaction.response.send_message("🔍 正在檢查重開機風險，請在下方卡片確認。", ephemeral=True)
    await perform_pc_reboot(interaction.channel, interaction.channel_id, interaction.user.id,
                            interaction.guild_id, "使用者透過 /pc-reboot 重新開機", delay)

@bot.tree.command(name="reset", description="🧹 清空並重置與 ArQ 的對話記憶 Context")
async def cmd_reset(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(f"⛔ 您沒有權限操作此 PC 助理 (您的 ID: `{interaction.user.id}`)。", ephemeral=True)
        return
    record_interaction(interaction.channel_id)
    agent.reset_chat(interaction.user.id)
    await interaction.response.send_message("🧹 **已清除您的對話歷史記憶！**\n現在可以開始全新話題囉！", ephemeral=True)

# -------------------------------------------------------------
# 自然語言對話 (DM 私訊或頻道內 @ 機器人)
# -------------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    # 忽略機器人自己的發言
    if message.author.bot:
        return

    is_dm = message.guild is None
    bot_mentioned = bool(bot.user and bot.user in message.mentions)
    replied_to_bot = bool(
        message.reference
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author == bot.user
    )

    # 是否強制伺服器頻道中必須 @mention 或回覆 (預設 false：私人群組直接說話即可回應；若要防大群洗版可在 .env 設 REQUIRE_MENTION=true)
    require_mention = os.getenv("REQUIRE_MENTION", "false").strip().lower() in ("true", "1", "yes")
    if require_mention and not is_dm and not bot_mentioned and not replied_to_bot and not message.content.startswith("!"):
        return

    logger.info(f"📩 收到來自 {message.author} (ID: {message.author.id}) 的訊息: {message.clean_content}")

    # 身分檢驗 (未授權者在頻道內只私訊拒絕通知，不在頻道內回覆，避免洗版；DM 內則直接回覆)
    if not is_authorized(message.author.id):
        logger.error(f"⛔ 未授權的使用者呼叫: {message.author} (ID: {message.author.id})")
        rejection = (
            f"⛔ 您的 Discord 使用者 ID 是 `{message.author.id}`，尚未加入授權白名單。\n"
            f"💡 請在 `.env` 的 `ALLOWED_USER_IDS` 中填入此 ID，並重新啟動機器人即可！"
        )
        if is_dm:
            await message.reply(rejection)
        else:
            try:
                await message.author.send(rejection)
            except discord.Forbidden:
                pass  # 使用者關閉私訊：不在頻道內回覆，直接靜默略過
        return

    # 記錄最後互動時間與頻道 (用於 1 小時閒置判定)
    record_interaction(message.channel.id)

    # 若是前綴指令 (! 開頭)，交給 commands 處理
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    # 去除 mention 標籤 (若有 @ 機器人)
    clean_prompt = message.clean_content
    if bot.user:
        clean_prompt = clean_prompt.replace(f"@{bot.user.name}", "").replace(f"<@{bot.user.id}>", "").strip()

    if not clean_prompt:
        await message.reply("嗨！有什麼我可以幫您的？隨時跟我說！")
        return

    # 自然語言重啟 ArQ 服務 (「重開機」亦指重啟 ArQ 程式本體，含二次確認)
    lower_content = clean_prompt.strip().lower()

    # 明確說「電腦 / 主機」才是重開整台電腦 (會先跳風險檢查與確認卡片)；單說「重開機」仍是重啟 ArQ 程式
    if lower_content in PC_REBOOT_TRIGGERS:
        await perform_pc_reboot(message.channel, message.channel.id, message.author.id,
                                message.guild.id if message.guild else None,
                                f"使用者透過文字訊息要求重開電腦 ({clean_prompt.strip()})", reference=message)
        return
    if lower_content in PC_REBOOT_CANCEL_TRIGGERS:
        await message.reply(await _cancel_pc_reboot_text())
        return

    bot_restart_triggers = {
        "重啟", "重啟 arq", "重開 arq", "重啟機器人", "重開機器人", "重啟服務",
        "重新啟動", "重開機", "重新開機", "重開", "reboot", "/reboot", "restart",
        "幫我重啟", "幫我重開機", "請幫我重啟", "請幫我重開機", "重啟arq", "重開arq",
    }
    if lower_content in bot_restart_triggers:
        approved = await ask_confirmation(
            channel=message.channel,
            owner_id=message.author.id,
            title="🔄 重新啟動 ArQ 服務",
            detail=REBOOT_CONFIRM_DETAIL,
            reference=message,
        )
        if not approved:
            await message.reply("❌ 已取消重啟操作。")
            return

        await perform_reboot(
            message.channel,
            message.channel.id,
            message.author.id,
            message.guild.id if message.guild else None,
            f"使用者透過文字訊息要求重啟 ({clean_prompt.strip()})",
        )
        return

    # AI 要安裝套件或執行高風險操作時，會呼叫此函式在 Discord 跳出同意按鈕 (無風險的操作直接執行)
    async def confirm(title: str, detail: str) -> bool:
        return await ask_confirmation(message.channel, message.author.id, title, detail, reference=message)

    # 不需回應的通知 (例如偵測到未經事前攔截的套件變動)
    async def notify(text: str) -> None:
        # RD 完整回報等長訊息：分段送出，不截斷 (見 rd_dispatch.format_dev_report)
        for chunk in split_message(redact(text), limit=1900):
            await message.channel.send(chunk)

    # Gemini 派工時 rd_dispatch 依此在原頻道送出 🛑 停止按鈕 (asyncio.to_thread 會帶入 context，見 tools/rd_jobs.py)
    rd_jobs.ORIGIN.set({"owner_id": message.author.id, "channel_id": message.channel.id, "channel": message.channel})

    # 觸發自然語言 Agent (授權使用者直接對話或 DM 皆會回應，支援多輪 Context)
    async with message.channel.typing():
        res = await agent.chat(clean_prompt, user_id=message.author.id, confirm=confirm, notify=notify)
        reply_text = redact(res.get("text", ""))  # 最後一道防線：遮蔽任何金鑰 / Token
        files_to_send = [discord.File(f) for f in res.get("files", []) if os.path.exists(f)]

        # 處理文字過長自動分段
        if len(reply_text) <= 2000:
            await message.reply(content=reply_text or None, files=files_to_send)
        else:
            chunks = split_message(reply_text, limit=1900)
            for idx, chunk in enumerate(chunks):
                if idx == 0:
                    await message.reply(chunk, files=files_to_send)
                else:
                    await message.channel.send(chunk)

def main():
    setup_logging()   # 最先執行：背景（開機自動啟動、最小化）出錯時才有紀錄可查
    # 開機自動啟動後又手動啟動時，避免兩個 bot 同時回覆 (見 tools/single_instance.py)
    if not single_instance.acquire():
        logger.info("ℹ️ ArQ 已經在執行中（可能是開機自動啟動），本次不重複啟動。")
        return
    if not DISCORD_TOKEN:
        logger.error("❌ 錯誤：未在 .env 中找到 DISCORD_BOT_TOKEN，請先確認設定！")
        return
    if not ALLOWED_USER_IDS:
        # 不中止啟動：讓使用者能私訊 bot 取得自己的 ID，但在設定前所有人都會被拒絕
        logger.warning("⚠️ 警告：ALLOWED_USER_IDS 未設定或格式錯誤，所有使用者都將被拒絕！")
        logger.warning("   請私訊機器人取得您的 Discord ID，填入 .env 後重新啟動。")
    bot.run(DISCORD_TOKEN, log_handler=None)   # discord.py 不自己掛 handler，交給 root logger

if __name__ == "__main__":
    main()
