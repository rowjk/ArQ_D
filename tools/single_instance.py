"""
單一實例保護：同一台電腦同時只允許一個 ArQ bot 在跑

開機自動啟動後，若使用者又手動雙擊啟動，會有兩個 bot 同時連上 Discord，
每則訊息都被回覆兩次、工具也被執行兩次。因此 bot.py 啟動時先取得 Windows「具名 Mutex」：
    - 取得成功 → 繼續啟動；Mutex 會在程序結束時由 Windows 自動釋放 (含當機、os._exit)。
    - 已被佔用 → 代表已有 ArQ 在執行，直接結束。
/reboot 重啟時舊程序會先結束 (釋放 Mutex) 再啟動新程序，不受影響。
"""

import sys
from typing import Optional

MUTEX_NAME = "Local\\ArQ-Discord-Bot"  # Local\ = 目前登入的使用者工作階段
_ERROR_ALREADY_EXISTS = 183

_handle: Optional[int] = None  # 保存 handle，程序存活期間不可釋放


def acquire(name: str = MUTEX_NAME) -> bool:
    """嘗試成為唯一的 ArQ 實例；已有其他實例在執行時回傳 False (非 Windows 一律回傳 True)"""
    global _handle
    if sys.platform != "win32":
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True  # 建立失敗時不擋啟動 (寧可多開也不要完全起不來)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True
