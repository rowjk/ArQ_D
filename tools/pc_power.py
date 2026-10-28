"""
電腦 (Windows) 重新開機

與 /reboot (只重啟 ArQ 程式) 不同，這裡是真的重開整台電腦，因此設計上比較保守：
    1. preflight()：重開機前先檢查風險，顯示在確認卡片上
         - 是否設定 Windows 自動登入：沒有的話，重開機後要有人登入，ArQ 才會上線 (遠端會失聯)
         - 是否啟用 ArQ 開機自動啟動 (scripts/autostart.ps1)
         - 目前開著哪些有視窗的程式 (只列程式名稱，不列視窗標題以保護隱私)
    2. schedule_reboot()：以 `shutdown /r /t <秒數>` 延遲重開機，期間可用 cancel_reboot() (`shutdown /a`) 取消。
       ⚠️ Windows 在 /t 大於 0 時會自動強制關閉程式 (隱含 /f)，未存檔的資料會遺失。
"""

import os
import subprocess
from typing import Any, Dict, List

DEFAULT_DELAY_SECONDS = 60
MIN_DELAY_SECONDS = 10
MAX_DELAY_SECONDS = 3600

_STARTUP_SHORTCUT = os.path.join(
    os.getenv("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "ArQ.lnk"
)
_NO_SHUTDOWN_IN_PROGRESS = 1116  # shutdown /a 時沒有排定的關機

# Windows 殼層 / 系統程序，重開機不會遺失資料，不列在提醒中
_SHELL_PROCESSES = {
    "explorer", "applicationframehost", "textinputhost", "shellexperiencehost", "searchhost",
    "startmenuexperiencehost", "lockapp", "systemsettings", "shellhost",
}


def _run(cmd: List[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL)


def autologon_enabled() -> bool:
    """是否設定了 Windows 自動登入 (唯讀查詢登錄檔)"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon") as key:
            value, _ = winreg.QueryValueEx(key, "AutoAdminLogon")
            return str(value).strip() == "1"
    except OSError:
        return False


def autostart_enabled() -> bool:
    """是否已啟用 ArQ 開機自動啟動 (啟動資料夾中的 ArQ.lnk)"""
    return os.path.isfile(_STARTUP_SHORTCUT)


def open_apps(limit: int = 15) -> List[str]:
    """目前開著視窗的程式名稱 (去重、排序；不含視窗標題)"""
    ps = ("Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | "
          "Select-Object -ExpandProperty ProcessName -Unique | Sort-Object")
    try:
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]).stdout
    except Exception:
        return []
    names = [line.strip() for line in out.splitlines()
             if line.strip() and line.strip().lower() not in _SHELL_PROCESSES]
    return names[:limit]


def preflight() -> Dict[str, Any]:
    """重開機前的風險檢查，給確認卡片顯示用"""
    return {"autologon": autologon_enabled(), "autostart": autostart_enabled(), "open_apps": open_apps()}


def schedule_reboot(delay_seconds: int = DEFAULT_DELAY_SECONDS) -> Dict[str, Any]:
    """排定 delay_seconds 秒後重新開機；回傳 {"success", "delay_seconds"[, "error"]}"""
    delay = max(MIN_DELAY_SECONDS, min(int(delay_seconds), MAX_DELAY_SECONDS))
    try:
        proc = _run(["shutdown", "/r", "/t", str(delay), "/c", f"ArQ 遠端重新開機：{delay} 秒後重新啟動電腦"])
    except Exception as e:
        return {"success": False, "delay_seconds": delay, "error": str(e)}
    if proc.returncode != 0:
        return {"success": False, "delay_seconds": delay,
                "error": (proc.stderr or proc.stdout).strip() or f"shutdown 失敗 (code {proc.returncode})"}
    return {"success": True, "delay_seconds": delay}


def cancel_reboot() -> Dict[str, Any]:
    """取消已排定的重開機；回傳 {"success", "message"}"""
    try:
        proc = _run(["shutdown", "/a"])
    except Exception as e:
        return {"success": False, "message": str(e)}
    if proc.returncode == 0:
        return {"success": True, "message": "已取消重新開機。"}
    if proc.returncode == _NO_SHUTDOWN_IN_PROGRESS:
        return {"success": False, "message": "目前沒有排定的重新開機（可能已經取消，或已開始重開）。"}
    return {"success": False, "message": (proc.stderr or proc.stdout).strip() or f"取消失敗 (code {proc.returncode})"}


def describe_preflight(info: Dict[str, Any], delay_seconds: int) -> str:
    """把風險檢查轉成確認卡片的 Markdown"""
    lines = [f"將在同意後 **{delay_seconds} 秒** 重新啟動整台電腦（期間可按按鈕取消）。", ""]
    if info.get("autologon"):
        lines.append("✅ 已設定 Windows 自動登入：重開機後會自動登入。")
    else:
        lines.append("⚠️ **未設定 Windows 自動登入**：重開機後要有人在電腦前登入，ArQ 才會上線，遠端會暫時失聯。")
    if info.get("autostart"):
        lines.append("✅ 已啟用 ArQ 開機自動啟動：登入後會自動上線並回報。")
    else:
        lines.append("⚠️ **未啟用 ArQ 開機自動啟動**：重開機後 ArQ 不會自己上線"
                     "（可執行 `scripts\\autostart.ps1 install` 啟用）。")
    apps = info.get("open_apps") or []
    lines.append("")
    if apps:
        lines.append("🪟 目前開著的程式（**未存檔的資料會遺失**，Windows 會強制關閉）：")
        lines.append("、".join(f"`{a}`" for a in apps))
    else:
        lines.append("🪟 目前沒有偵測到開著視窗的程式。")
    return "\n".join(lines)
