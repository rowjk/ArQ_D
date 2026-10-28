"""
JobTracker_104 爬蟲管理器 (Job Tracker Manager)
提供 ArQ 呼叫 104 職缺爬蟲 (main_104_tracker.py) 的統一管理介面：
背景非阻塞啟動、互斥防重疊、執行狀態查詢、最新執行結果摘要讀取。

重要架構規範：
爬蟲本體 main_104_tracker.py 單次執行約需 16.5~18 分鐘，且會開啟有頭 Chrome 視窗
(需桌面已登入環境)，因此 ArQ 絕對不可同步阻塞等待，必須用 start() / --start 背景
啟動後立即回應，並以 status() / last_log() 事後查詢結果。

位置說明：
本模組原本放在 task/job_tracker_manager.py，但 task/ 被 .gitignore 忽略、
且是 Claude Code / Antigravity 自主寫檔的任務區，bot.py 啟動時依賴它很脆弱
(重新 clone 會缺檔、被 AI 改壞會導致 bot 起不來)，因此移到 tools/ 納入版本控制。
狀態檔與 log 仍放在 task/artifact/ (執行期資料，不進版本控制)。

CLI 用法 (在專案根目錄)：
    uv run python -m tools.job_tracker --start | --status | --last-log
"""

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

from tools.config import PROJECT_ROOT
from tools.secret_guard import child_env

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==========================================
# 路徑與常數設定
# ==========================================
# 爬蟲專案位置：.env 的 JOB_TRACKER_DIR，未設定則為「使用者家目錄\\Documents\\JobTracker_104」
# (repo 預設會公開，不寫死個人路徑)
TRACKER_DIR = os.getenv("JOB_TRACKER_DIR", "").strip() or os.path.join(os.path.expanduser("~"), "Documents", "JobTracker_104")
TRACKER_SCRIPT = os.path.join(TRACKER_DIR, "main_104_tracker.py")
CREDENTIALS_FILE = os.path.join(TRACKER_DIR, "credentials.json")
WORKBOOK_NAME = os.getenv("JOB_TRACKER_WORKBOOK", "").strip() or "JobTracker(104)"
RUN_LOGS_SHEET = "Run_Logs"

# 與現有 Windows 工作排程器 (JobTracker_104_Daily) 採用相同的直譯器，
# 確保 gspread / google-auth / undetected_chromedriver 等套件皆已就緒。
PY_LAUNCHER_CANDIDATES = [r"C:\WINDOWS\py.exe", "py"]
PYTHON_VERSION_FLAG = "-3.13"

ARTIFACT_DIR = os.path.join(PROJECT_ROOT, "task", "artifact")
STATE_FILE = os.path.join(ARTIFACT_DIR, "crawler_state.json")
LOG_FILE = os.path.join(ARTIFACT_DIR, "crawler_current.log")

EXPECTED_DURATION_MIN = "16.5~18"

# 比對行程建立時間的容許誤差 (秒)
_CREATE_TIME_TOLERANCE = 2.0


# ==========================================
# 行程存活檢查 (不強制依賴 psutil，缺少時自動退化)
# ==========================================

def _get_create_time(pid: int) -> Optional[float]:
    """取得行程建立時間 (需要 psutil；取不到回傳 None)"""
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        return None


def _is_pid_alive(pid: int, create_time: Optional[float] = None) -> bool:
    """
    判斷「當初啟動的那個行程」是否還活著。

    Windows 會很快重複使用 PID：爬蟲結束後，同一個 PID 可能被其他程式拿走，
    單看 PID 存在會誤判「仍在執行」，導致完成通知永遠不發。因此有記錄 create_time 時，
    會同時比對行程建立時間，不一致即視為原行程已結束。
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # 若執行環境已安裝 psutil (ArQ 本身依賴)，優先使用，較準確
        if create_time is None:
            return psutil.pid_exists(pid)  # 舊版狀態檔沒有 create_time，只能退回單看 PID
        try:
            proc = psutil.Process(pid)
            return proc.is_running() and abs(proc.create_time() - create_time) <= _CREATE_TIME_TOLERANCE
        except psutil.NoSuchProcess:
            return False
    except ImportError:
        pass

    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _find_stray_tracker_pid() -> Optional[int]:
    """
    最佳努力 (best-effort) 掃描：檢查是否有「非本管理器啟動」的 main_104_tracker.py 行程
    (例如使用者手動雙擊 .bat、或 Windows 工作排程器同時觸發)。
    僅在環境已安裝 psutil 時生效，未安裝則略過此項檢查 (不影響核心啟動/狀態功能)。
    """
    try:
        import psutil
    except ImportError:
        return None

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any("main_104_tracker.py" in str(part) for part in cmdline):
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _resolve_py_launcher() -> list:
    for candidate in PY_LAUNCHER_CANDIDATES:
        if os.path.isabs(candidate):
            if os.path.isfile(candidate):
                return [candidate, PYTHON_VERSION_FLAG]
            continue
        return [candidate, PYTHON_VERSION_FLAG]
    return ["py", PYTHON_VERSION_FLAG]


# ==========================================
# 狀態檔讀寫
# ==========================================

def _load_state() -> Optional[Dict[str, Any]]:
    if not os.path.isfile(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_state(state: Dict[str, Any]) -> None:
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_FILE)  # Windows 上的不可分割取代


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


class JobTrackerManager:
    """104 職缺爬蟲 (JobTracker_104) 背景執行管理器"""

    def start(self) -> Dict[str, Any]:
        """以背景非阻塞方式啟動爬蟲，具互斥防重疊機制"""
        if not os.path.isfile(TRACKER_SCRIPT):
            return {"ok": False, "error": f"找不到爬蟲主程式：{TRACKER_SCRIPT}"}

        running = self.status()
        if running.get("running"):
            return {
                "ok": False,
                "error": (
                    f"爬蟲已在執行中 (PID={running['pid']}，"
                    f"已執行 {running['elapsed_str']})，拒絕重複啟動。"
                ),
                **running,
            }

        stray_pid = _find_stray_tracker_pid()
        if stray_pid:
            return {
                "ok": False,
                "error": (
                    f"偵測到非本管理器啟動的爬蟲行程 (PID={stray_pid})，"
                    "可能是手動執行 .bat 或 Windows 工作排程器同時觸發，拒絕重複啟動。"
                ),
            }

        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        if os.path.isfile(LOG_FILE):
            try:
                os.replace(LOG_FILE, LOG_FILE + ".bak")
            except Exception:
                pass

        launcher = _resolve_py_launcher()
        cmd = launcher + [TRACKER_SCRIPT]

        creation_flags = 0
        if sys.platform == "win32":
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creation_flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "DETACHED_PROCESS"):
                creation_flags |= subprocess.DETACHED_PROCESS

        # 強制子行程以 UTF-8 寫出 stdout/stderr，避免中文/emoji 在重導向至檔案時
        # 因系統預設編碼 (如 cp950) 無法編碼而噴 UnicodeEncodeError 中斷爬蟲。
        # PYTHONUNBUFFERED：stdout 重導向至檔案時預設為區塊緩衝，不設定的話
        # log 檔會長時間空白 (直到緩衝區填滿或行程結束)，--status 就讀不到即時進度。
        env = child_env()  # 爬蟲不需要 ArQ 的金鑰，移除 .env 載入的機密
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")

        log_fh = open(LOG_FILE, "wb")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=TRACKER_DIR,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creation_flags,
                close_fds=True,
                env=env,
            )
        except Exception as e:
            return {"ok": False, "error": f"啟動失敗：{e}"}
        finally:
            log_fh.close()

        started_at = time.time()
        state = {
            "pid": proc.pid,
            "create_time": _get_create_time(proc.pid),  # 用來排除 PID 被重複使用的誤判

            "started_at": started_at,
            "started_at_str": datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S"),
            "command": cmd,
            "cwd": TRACKER_DIR,
            "log_file": LOG_FILE,
            "notified": False,  # 是否已被 check_completion_for_notify() 主動通知過，避免重複推播
        }
        _save_state(state)

        return {
            "ok": True,
            "pid": proc.pid,
            "started_at": state["started_at_str"],
            "log_file": LOG_FILE,
            "expected_duration_min": EXPECTED_DURATION_MIN,
        }

    def status(self) -> Dict[str, Any]:
        """查詢爬蟲目前是否在執行、已執行時間、PID"""
        state = _load_state()
        if not state:
            return {"running": False, "message": "目前沒有啟動紀錄 (從未透過本管理器啟動過，或狀態檔已清除)"}

        pid = state.get("pid", 0)
        started_at = state.get("started_at", 0)
        elapsed = time.time() - started_at

        if _is_pid_alive(pid, state.get("create_time")):
            return {
                "running": True,
                "pid": pid,
                "started_at": state.get("started_at_str"),
                "elapsed_seconds": round(elapsed, 1),
                "elapsed_str": _format_elapsed(elapsed),
                "log_file": state.get("log_file"),
            }

        return {
            "running": False,
            "pid": pid,
            "started_at": state.get("started_at_str"),
            "elapsed_seconds": round(elapsed, 1),
            "elapsed_str": _format_elapsed(elapsed),
            "message": "行程已結束 (下次 --start 會自動覆蓋此狀態紀錄)",
            "log_tail": self._read_log_tail(state.get("log_file")),
        }

    def check_completion_for_notify(self) -> Optional[Dict[str, Any]]:
        """
        供「主動通知」背景監看迴圈 (例如 ArQ Discord Bot 的 tasks.loop) 週期性呼叫。

        與 status() 的差異：status() 是「被動查詢」，每次呼叫都忠實回報目前狀態，
        不會更動任何東西；本方法是「主動通知觸發點」，一旦偵測到「本管理器啟動過的
        行程已經結束，且尚未通知過」，就會立刻將 notified 標記為 True 並寫回狀態檔
        (冪等、防止同一次執行被重複推播多次)，再回傳摘要給呼叫端發送通知。

        回傳 None 代表「目前沒有新的完成事件需要通知」(例如仍在執行中、或已通知過)。
        """
        state = _load_state()
        if not state:
            return None
        if state.get("notified"):
            return None
        if _is_pid_alive(state.get("pid", 0), state.get("create_time")):
            return None  # 仍在執行中，尚未結束，暫不通知

        # 行程已結束且尚未通知過 -> 立即標記已通知 (先寫回再回傳，避免呼叫端發送失敗導致重複觸發)
        state["notified"] = True
        _save_state(state)

        return {
            "pid": state.get("pid"),
            "started_at_str": state.get("started_at_str"),
            "last_log": self.last_log(),
        }

    @staticmethod
    def _read_log_tail(log_file: Optional[str], lines: int = 15) -> str:
        if not log_file or not os.path.isfile(log_file):
            return ""
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
            return "".join(content[-lines:]).strip()
        except Exception:
            return ""

    def last_log(self) -> Dict[str, Any]:
        """讀取最新一筆執行結果摘要 (優先 Google Sheets Run_Logs，備援本地 Log)"""
        sheet_result = self._read_last_log_from_sheets()
        if sheet_result.get("ok"):
            return sheet_result

        local_result = self._read_last_log_from_local()
        local_result["sheet_error"] = sheet_result.get("error")
        return local_result

    @staticmethod
    def _read_last_log_from_sheets() -> Dict[str, Any]:
        if not os.path.isfile(CREDENTIALS_FILE):
            return {"ok": False, "error": f"找不到憑證檔：{CREDENTIALS_FILE}"}

        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as e:
            return {"ok": False, "error": f"目前執行環境缺少套件 (gspread/google-auth)：{e}"}

        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            credentials = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
            gc = gspread.authorize(credentials)
            sh = gc.open(WORKBOOK_NAME)
            log_sheet = sh.worksheet(RUN_LOGS_SHEET)
            rows = log_sheet.get_all_values()
        except Exception as e:
            return {"ok": False, "error": f"連線 Google Sheets 失敗：{e}"}

        if len(rows) <= 1:
            return {"ok": False, "error": "Run_Logs 工作表目前沒有任何執行紀錄"}

        header, last_row = rows[0], rows[-1]
        record = dict(zip(header, last_row))
        return {"ok": True, "source": "google_sheets", "record": record}

    def _read_last_log_from_local(self) -> Dict[str, Any]:
        tail = self._read_log_tail(LOG_FILE, lines=200)
        if not tail:
            return {"ok": False, "source": "local_log", "error": "本地 Log 檔案不存在或無內容", "record": None}

        summary_match = re.search(
            r"新增：(\d+)\s*筆\s*\|.*?更新：(\d+)\s*筆\s*\|.*?關閉：(\d+)\s*筆\s*\|.*?封存：(\d+)\s*筆\s*\|.*?耗時：([\d.]+)\s*秒",
            tail,
        )
        status = "SUCCESS" if "任務達成" in tail else "UNKNOWN"

        record = None
        if summary_match:
            record = {
                "新增筆數": summary_match.group(1),
                "更新筆數": summary_match.group(2),
                "下架筆數": summary_match.group(3),
                "封存筆數": summary_match.group(4),
                "執行耗時(秒)": summary_match.group(5),
                "執行狀態": status,
            }

        return {
            "ok": record is not None,
            "source": "local_log",
            "record": record,
            "log_tail": tail,
            "error": None if record else "本地 Log 中找不到完整的執行摘要 (可能仍在執行中或異常中斷)",
        }


# ==========================================
# CLI 介面
# ==========================================

def _print_start_result(result: Dict[str, Any]) -> None:
    if result.get("ok"):
        print("✅ 爬蟲已成功於背景啟動！")
        print(f"   PID: {result['pid']}")
        print(f"   啟動時間: {result['started_at']}")
        print(f"   Log 檔案: {result['log_file']}")
        print(f"   預計耗時: 約 {result['expected_duration_min']} 分鐘")
    else:
        print(f"❌ 啟動失敗：{result.get('error')}")


def _print_status_result(result: Dict[str, Any]) -> None:
    if result.get("running"):
        print("🟢 爬蟲執行中")
        print(f"   PID: {result['pid']}")
        print(f"   啟動時間: {result['started_at']}")
        print(f"   已執行時間: {result['elapsed_str']} (mm:ss)")
    elif "pid" in result:
        print("🔴 爬蟲未在執行 (上次啟動行程已結束)")
        print(f"   上次 PID: {result['pid']}")
        print(f"   上次啟動時間: {result['started_at']}")
        if result.get("log_tail"):
            print("   ---- Log 結尾 ----")
            print("   " + result["log_tail"].replace("\n", "\n   "))
    else:
        print(f"⚪ {result.get('message')}")


def _print_last_log_result(result: Dict[str, Any]) -> None:
    source = result.get("source")
    if result.get("ok") and source == "google_sheets":
        record = result["record"]
        print("📊 最新執行摘要 (來源: Google Sheets Run_Logs)")
        for key in ["執行時間", "執行耗時(秒)", "網頁掃描總數", "新增筆數", "更新筆數", "下架筆數", "執行狀態", "執行順序與細節", "錯誤日誌"]:
            if key in record:
                print(f"   {key}: {record[key]}")
    elif result.get("ok") and source == "local_log":
        record = result["record"]
        print("📊 最新執行摘要 (來源: 本地 Log 備援，Google Sheets 無法讀取)")
        print(f"   ⚠️ Sheets 讀取失敗原因: {result.get('sheet_error')}")
        for key, value in record.items():
            print(f"   {key}: {value}")
    else:
        print("❌ 無法取得最新執行摘要")
        if result.get("sheet_error"):
            print(f"   Google Sheets 錯誤: {result['sheet_error']}")
        print(f"   本地 Log 錯誤: {result.get('error')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="JobTracker_104 爬蟲背景管理器 (ArQ 整合介面)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", action="store_true", help="背景非阻塞啟動爬蟲 (具互斥防重疊機制)")
    group.add_argument("--status", action="store_true", help="查詢目前執行狀態、已執行時間、PID")
    group.add_argument("--last-log", action="store_true", help="讀取最新一筆執行結果摘要")

    args = parser.parse_args()
    manager = JobTrackerManager()

    if args.start:
        result = manager.start()
        _print_start_result(result)
        return 0 if result.get("ok") else 1

    if args.status:
        result = manager.status()
        _print_status_result(result)
        return 0

    if args.last_log:
        result = manager.last_log()
        _print_last_log_result(result)
        return 0 if result.get("ok") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
