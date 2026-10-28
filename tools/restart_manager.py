"""
ArQ 重啟管理器 (Restart Manager)
負責 ArQ 服務重啟 (Bot Restart / 程式本體重開機) 的狀態持久化、
行程繁衍重啟、守護進程聯動、防死循環保護以及開機後的「主動回報 (Boot Wakeup Notice / 我開機了！)」機制。

重要架構規範：
ArQ 的「重開機」(/reboot) 是指 ArQ 程式本體重啟與服務重載，並非實體 PC 主機重新開機。
真正的電腦重新開機另走 /pc-reboot (見 tools/pc_power.py)：確認卡片 → 延遲 `shutdown /r` → 可取消 → 開機後回報。
"""

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from tools.config import PROJECT_ROOT, TEMP_DIR

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


@dataclass
class RestartState:
    """持久化保存的重啟上下文資料結構"""
    type: str = "bot"             # 重啟類型：固定為 "bot" (代表 ArQ 程式本體重開機)
    channel_id: Optional[int] = None # 觸發重啟的 Discord 頻道 ID
    user_id: int = 0              # 觸發重啟的使用者 ID
    initiated_at: float = 0.0     # 觸發時間戳記 (time.time())
    reason: str = "手動重啟"       # 重啟原因或指令來源
    pid: int = 0                  # 執行重啟時的當前行程 ID
    guild_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RestartState":
        return cls(
            type=data.get("type", "bot"),
            channel_id=data.get("channel_id"),
            user_id=data.get("user_id", 0),
            initiated_at=data.get("initiated_at", time.time()),
            reason=data.get("reason", "手動重啟"),
            pid=data.get("pid", 0),
            guild_id=data.get("guild_id"),
        )


class RestartManager:
    """
    重啟生命週期管理器：
    1. 記錄重啟意圖並寫入磁碟 (狀態持久化)
    2. 執行 Windows 行程重生 (Bot 重啟)
    3. 在開機 (on_ready) 時檢查並消費狀態，組裝「主動回報我開機了」訊息
    4. 提供重啟頻率過高/死循環保護 (Safety Guard)
    """

    # 預設狀態檔檔名
    DEFAULT_STATE_FILENAME = "restart_state.json"

    # 重啟有效時間 (30 分鐘，超過此時間的狀態檔視為無效或逾時崩潰)
    STATE_EXPIRY_SECONDS = 1800

    # 電腦重開機的有效時間較長：開機、Windows 更新與登入可能要一段時間 (6 小時)
    PC_STATE_EXPIRY_SECONDS = 6 * 3600

    # 最小重啟間隔 (60 秒內禁止連續自動重啟，防止死循環)
    MIN_RESTART_INTERVAL_SECONDS = 60

    # 服務守護重啟 Exit Code
    RESTART_EXIT_CODE = 42

    def __init__(self, state_dir: Optional[str] = None):
        if state_dir is None:
            # 一律放在專案根目錄的 _temp/ (已被 .gitignore 忽略)；不存在就建立，
            # 避免退回 tools/ 目錄導致執行期狀態檔混進原始碼
            self.state_dir = TEMP_DIR
            os.makedirs(self.state_dir, exist_ok=True)
        else:
            self.state_dir = state_dir

        self.state_file_path = os.path.join(self.state_dir, self.DEFAULT_STATE_FILENAME)

    def record_restart_intent(
        self,
        restart_type: str = "bot",
        user_id: int = 0,
        channel_id: Optional[int] = None,
        reason: str = "手動重啟",
        guild_id: Optional[int] = None,
    ) -> RestartState:
        """
        記錄重啟意圖並原子性寫入檔案。
        restart_type: "bot" (重啟 ArQ 程式本體) 或 "pc" (電腦重新開機)
        """
        os.makedirs(self.state_dir, exist_ok=True)
        state = RestartState(
            type=restart_type,
            channel_id=channel_id,
            user_id=user_id,
            initiated_at=time.time(),
            reason=reason,
            pid=os.getpid(),
            guild_id=guild_id,
        )

        temp_path = self.state_file_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)

        # 在 Windows 上進行不可分割取代
        os.replace(temp_path, self.state_file_path)
        return state

    def consume_restart_state(self) -> Optional[RestartState]:
        """
        檢查是否有待處理的重啟狀態。
        若有且在有效時間內，讀取後立即刪除狀態檔案 (消費)，避免二次重複發送通知。
        """
        if not os.path.isfile(self.state_file_path):
            return None

        try:
            with open(self.state_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            state = RestartState.from_dict(data)
        except Exception as e:
            # 檔案損毀或解析錯誤，清理檔案避免阻礙後續運作
            self.clear_state()
            logger.warning(f"⚠️ 解析重啟狀態檔案失敗，已自動清理: {e}", exc_info=True)
            return None

        # 消費完畢，立即刪除狀態檔案
        self.clear_state()

        # 檢查是否已過期 (例如關機數天後才開機)
        now = time.time()
        elapsed = now - state.initiated_at
        expiry = self.PC_STATE_EXPIRY_SECONDS if state.type == "pc" else self.STATE_EXPIRY_SECONDS
        if elapsed < 0 or elapsed > expiry:
            logger.warning(f"⚠️ 偵測到過期的重啟狀態 ({elapsed:.1f} 秒前)，略過主動通知")
            return None

        return state

    def clear_state(self) -> None:
        """安全刪除狀態檔案"""
        try:
            if os.path.isfile(self.state_file_path):
                os.remove(self.state_file_path)
        except Exception:
            pass

    def is_restart_looping(self, history_file: Optional[str] = None) -> bool:
        """
        死循環檢測：檢查是否在極短時間內發生頻繁重啟。
        若 10 分鐘內重啟超過 5 次，視為死循環風險。
        """
        hist_path = history_file or os.path.join(self.state_dir, "restart_history.json")
        now = time.time()
        history: List[float] = []

        if os.path.isfile(hist_path):
            try:
                with open(hist_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []

        # 保留最近 10 次且在 10 分鐘內的重啟時間戳
        recent = [t for t in history if now - t < 600]
        recent.append(now)

        try:
            with open(hist_path, "w", encoding="utf-8") as f:
                json.dump(recent[-10:], f)
        except Exception:
            pass

        return len(recent) >= 5

    def execute_bot_restart(self, delay_seconds: float = 1.0) -> None:
        """
        執行 ArQ Bot 重啟：
        1. 若處於 run.ps1 守護環境 (ARQ_SUPERVISED=1)，則以 exit code 42 退出，外層腳本乾淨重生。
        2. 若為獨立手動啟動模式，則透過 Windows DETACHED_PROCESS 繁衍新程序後退出目前程序。
        """
        # 1. 守護進程模式 (最推薦，維持同一個控制台終端)
        is_supervised = os.environ.get("ARQ_SUPERVISED", "").strip() in ("1", "true", "yes")
        if is_supervised:
            logger.info("🔄 守護模式生效中，即將退出本程序以供外層守護腳本 (run.ps1) 重新啟動...")
            time.sleep(0.5)
            os._exit(self.RESTART_EXIT_CODE)
            return

        # 2. 獨立手動執行模式 (方案 A：繁衍子行程後退場)
        project_root = PROJECT_ROOT
        logger.info(f"🔄 獨立進程模式：正在繁衍背景程序重啟 ArQ ({project_root})...")

        if sys.platform == "win32":
            creation_flags = 0
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creation_flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "DETACHED_PROCESS"):
                creation_flags |= subprocess.DETACHED_PROCESS

            cmd = (
                f'powershell.exe -NoProfile -Command "'
                f"Start-Sleep -Seconds {delay_seconds}; "
                f"Set-Location '{project_root}'; "
                f'uv run python bot.py"'
            )
            subprocess.Popen(
                cmd,
                cwd=project_root,
                creationflags=creation_flags,
                shell=True,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                ["sh", "-c", f"sleep {delay_seconds} && cd '{project_root}' && uv run python bot.py"],
                cwd=project_root,
                close_fds=True,
            )

        time.sleep(0.5)
        os._exit(0)
        return

    @staticmethod
    def execute_pc_reboot(delay_seconds: int = 60) -> Dict[str, Any]:
        """
        排定電腦在 delay_seconds 秒後重新開機 (`shutdown /r`)，期間可用 pc_power.cancel_reboot() 取消。
        呼叫前必須已取得使用者確認，並先以 record_restart_intent(restart_type="pc") 記錄狀態，開機後才會回報。
        """
        from tools.pc_power import schedule_reboot
        return schedule_reboot(delay_seconds)

    @staticmethod
    def build_boot_report(
        state: RestartState,
        sys_status: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        組裝開機完成的通知文字與 Discord Embed 資料。
        type="pc" 回報「電腦重新開機完成」，其餘回報 ArQ 服務重新啟動（「我開機了！」）。
        """
        now = time.time()
        elapsed_sec = max(0.1, round(now - state.initiated_at, 1))

        cpu = sys_status.get("cpu_usage", "正常")
        ram = sys_status.get("memory_used", "正常")
        uptime = sys_status.get("uptime", "正常運行中")

        if state.type == "pc":
            minutes, seconds = divmod(int(elapsed_sec), 60)
            return {
                "title": "🖥️ 電腦重新開機完成，我回來了！",
                "description": (
                    "報告主人！**電腦已重新開機，ArQ 也已自動上線。** 🤖\n\n"
                    f"⏱️ **從下指令到重新上線**：約 `{minutes} 分 {seconds} 秒`\n"
                    f"📊 **目前主機狀態**：CPU `{cpu}` | 記憶體 `{ram}`\n"
                    f"🖥️ **本次開機已運行**：`{uptime}`\n"
                    f"📝 **觸發原因**：{state.reason}"
                ),
                "color": 0x9b59b6,  # 紫色，與程式重啟區分
                "elapsed_seconds": elapsed_sec,
                "channel_id": state.channel_id,
                "user_id": state.user_id,
            }

        title = "🚀 ArQ 重新啟動完成！我開機了！"
        color = 0x3498db  # 藍色
        desc = (
            "報告主人！**ArQ 服務已成功重新啟動完畢，我開機了！** 🤖\n\n"
            f"⏱️ **重啟耗時**：約 `{elapsed_sec}` 秒\n"
            f"📊 **目前主機狀態**：CPU `{cpu}` | 記憶體 `{ram}`\n"
            f"🖥️ **主機系統運行**：`{uptime}`\n"
            f"📝 **觸發原因**：{state.reason}\n\n"
            "連線與 AI 模組已全數重載完畢，有任何指令隨時吩咐！✨"
        )

        return {
            "title": title,
            "description": desc,
            "color": color,
            "elapsed_seconds": elapsed_sec,
            "channel_id": state.channel_id,
            "user_id": state.user_id,
        }
