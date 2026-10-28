"""
檔案與目錄管理工具

read_text_file 會拒絕讀取機密檔 (.env、credentials.json、私鑰…，判定見 tools/secret_guard.py)，
避免金鑰經由 Gemini 或 Discord 外洩。
"""

import os
from datetime import datetime
from typing import Dict, Any, List

from tools.secret_guard import is_secret_file


def list_directory(target_path: str = ".") -> Dict[str, Any]:
    """列出指定路徑下的所有檔案與資料夾資訊"""
    try:
        resolved_path = os.path.abspath(target_path)
        if not os.path.exists(resolved_path):
            return {"error": f"路徑不存在: {target_path}"}

        entries = []
        for entry in os.scandir(resolved_path):
            stat = entry.stat()
            entries.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size_bytes": stat.st_size if entry.is_file() else None,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            })

        # 資料夾排前面，檔案排後面，依名稱排序
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

        return {
            "current_dir": resolved_path,
            "total_items": len(entries),
            "items": entries[:50]  # 最多返回前 50 筆避免過載
        }
    except Exception as e:
        return {"error": str(e)}


def read_text_file(filepath: str, max_lines: int = 100) -> Dict[str, Any]:
    """讀取本地文字檔案內容 (機密檔如 .env、憑證、私鑰不提供讀取)"""
    try:
        if is_secret_file(filepath):
            return {"error": "此檔案含有金鑰或憑證等機密資訊，基於安全考量不提供讀取。"}
        resolved_path = os.path.abspath(filepath)
        if not os.path.exists(resolved_path):
            return {"error": f"檔案不存在: {filepath}"}

        with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [f.readline() for _ in range(max_lines)]
            has_more = bool(f.readline())

        return {
            "filepath": resolved_path,
            "lines_read": len(lines),
            "has_more": has_more,
            "content": "".join(lines)
        }
    except Exception as e:
        return {"error": str(e)}
