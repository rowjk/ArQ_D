"""
系統監控與終端機執行工具

⚠️ 編碼注意事項 (中文 Windows)：
    子程序的 stdout/stderr 被管線 (pipe) 擷取時，預設會用系統 ANSI/OEM 編碼 (cp950)
    輸出，但本模組統一用 UTF-8 解碼，若不處理中文會變成亂碼。因此：
    - PowerShell：在指令前加上 `_PS_UTF8_PREFIX`，強制主控台輸出 UTF-8。
    - Python：透過環境變數 PYTHONUTF8 / PYTHONIOENCODING 強制子程序使用 UTF-8。
"""

import asyncio
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import psutil

from tools.config import PROJECT_ROOT, TEMP_DIR  # 專案根目錄 (ArQ/)，task/ 與 _temp/ 皆以此為基準
from tools.secret_guard import child_env

# 讓 PowerShell 本身與其呼叫的原生程式 (ipconfig, git...) 都以 UTF-8 輸出
_PS_UTF8_PREFIX = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$OutputEncoding = [System.Text.Encoding]::UTF8; "
)

# 回傳給 AI / Discord 的輸出長度上限 (避免訊息與 token 爆量)
MAX_STDOUT_CHARS = 3000
MAX_STDERR_CHARS = 1500


def _utf8_child_env() -> Dict[str, str]:
    """建立讓 Python 子程序以 UTF-8 讀寫 stdio 與檔案的環境變數 (已移除 .env 載入的機密)"""
    env = child_env()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _error_result(message: str) -> Dict[str, Any]:
    """統一的失敗回傳格式 (與成功時的欄位一致，呼叫端可放心取值)"""
    return {"success": False, "error": message, "stdout": "", "stderr": "", "returncode": -1}


def get_system_status() -> Dict[str, Any]:
    """獲取電腦硬體資源使用狀態與運行時間"""
    cpu_percent = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    boot_time = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot_time

    # 磁碟資訊 (C 槽)
    disk = psutil.disk_usage("C:\\")

    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu_usage": f"{cpu_percent}%",
        "memory_used": f"{mem.used / (1024**3):.2f} GB / {mem.total / (1024**3):.2f} GB ({mem.percent}%)",
        "disk_c": f"{disk.used / (1024**3):.1f} GB / {disk.total / (1024**3):.1f} GB ({disk.percent}%)",
        "boot_time": boot_time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": str(timedelta(seconds=int(uptime.total_seconds()))),
    }


def _format_uptime(seconds: float) -> str:
    """開機時間：1 天以上「X 天 Y 小時」、1 小時以上「Y 小時 M 分」、其餘「M 分鐘」"""
    total_minutes = int(seconds // 60)
    days, rest = divmod(total_minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {minutes} 分"
    return f"{minutes} 分鐘"


def format_heartbeat_line(cpu_percent: float, mem_used_bytes: float, mem_total_bytes: float, uptime_seconds: float) -> str:
    """心跳訊息：固定一行、80 字內，例：💓 ArQ 在線｜CPU 12%｜RAM 8.1/16 GB｜開機 3 天 4 小時"""
    return (
        f"💓 ArQ 在線｜CPU {cpu_percent:.0f}%"
        f"｜RAM {mem_used_bytes / 1024**3:.1f}/{mem_total_bytes / 1024**3:.0f} GB"
        f"｜開機 {_format_uptime(uptime_seconds)}"
    )


def get_heartbeat_line() -> str:
    """讀取目前主機狀態並組成一行心跳訊息；讀取失敗時各項顯示「?」，仍維持同一格式"""
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        return format_heartbeat_line(cpu, mem.used, mem.total, time.time() - psutil.boot_time())
    except Exception:
        return "💓 ArQ 在線｜CPU ?｜RAM ?｜開機 ?"


def run_powershell_sync(command: str, timeout: int = 30) -> Dict[str, Any]:
    """在 Windows 上同步執行 PowerShell 指令，回傳 stdout / stderr / returncode"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_UTF8_PREFIX + command],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=child_env(),  # 移除機密環境變數，避免 `echo $env:XXX_API_KEY` 讀到金鑰
        )
        return {
            "success": proc.returncode == 0,
            "stdout": proc.stdout.strip()[:MAX_STDOUT_CHARS],
            "stderr": proc.stderr.strip()[:MAX_STDERR_CHARS],
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return _error_result(f"指令執行逾時 (超過 {timeout} 秒被強制中止)")
    except Exception as e:
        return _error_result(str(e))


async def run_powershell(command: str, timeout: int = 30) -> Dict[str, Any]:
    """在 Windows 上非同步執行 PowerShell 指令 (丟到執行緒，不阻塞 Discord 事件迴圈)"""
    return await asyncio.to_thread(run_powershell_sync, command, timeout)


def execute_python_code(code: str, timeout: int = 60, task_script_name: Optional[str] = None) -> Dict[str, Any]:
    """
    在電腦本機執行「簡單」的 Python 程式碼 (約 20 行以內、一次就能跑完的小段程式)，
    例如簡單計算、讀取單一檔案做統計、格式轉換。能避開命令列引號跳脫問題，直接回傳執行標準輸出與結果。
    稍有規模的開發 (完整腳本、爬蟲、報表 / 簡報產生、除錯重構) 請改用 dispatch_claude_engineer_sync 派給 RD 工程師。

    規則：
    - 產出結果與成果物請一律寫入 'task/artifact/' 目錄（例如 'task/artifact/output.pptx'）。
    - 若指定 task_script_name (如 'create_chart.py')，該程式碼會保留於 'task/' 資料夾供日後追蹤與調用。
    """
    # 註：上方 docstring 會被 Gemini 當成工具說明，修改時請保持對 AI 友善的描述。
    task_dir = os.path.join(PROJECT_ROOT, "task")
    artifact_dir = os.path.join(task_dir, "artifact")
    os.makedirs(artifact_dir, exist_ok=True)

    if task_script_name:
        # basename 防止路徑穿越 (例如 "../../evil.py")
        clean_name = os.path.basename(task_script_name)
        if not clean_name.endswith(".py"):
            clean_name += ".py"
        exec_file = os.path.join(task_dir, clean_name)
        is_temp = False
    else:
        temp_dir = TEMP_DIR
        os.makedirs(temp_dir, exist_ok=True)
        exec_file = os.path.join(temp_dir, f"_exec_{int(time.time()*1000)}.py")
        is_temp = True

    try:
        with open(exec_file, "w", encoding="utf-8") as f:
            f.write(code)

        proc = subprocess.run(
            [sys.executable, exec_file],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=_utf8_child_env(),
        )
        return {
            "success": proc.returncode == 0,
            "stdout": proc.stdout.strip()[:MAX_STDOUT_CHARS],
            "stderr": proc.stderr.strip()[:MAX_STDERR_CHARS],
            "returncode": proc.returncode,
            "script_path": exec_file if not is_temp else None,
        }
    except subprocess.TimeoutExpired:
        return _error_result(f"Python 執行逾時 (超過 {timeout} 秒被強制中止)")
    except Exception as e:
        return _error_result(str(e))
    finally:
        if is_temp and os.path.exists(exec_file):
            try:
                os.remove(exec_file)
            except Exception:
                pass
