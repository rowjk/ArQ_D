"""
公開 repo 安全檢查：repo 預設會公開，進版控的檔案不得含個人資訊或金鑰

檢查所有 git 追蹤中 (含即將提交的新檔) 的檔案：
    - 不得含 .env 中任何機密的實際值 (本機有 .env 時才檢查)
    - 不得含目前使用者的家目錄路徑 (例如 C:\\Users\\<名字>)
    - 不得含常見金鑰樣式 (Anthropic / Google / 中央氣象署 / Discord Bot Token)
發現問題時請改用 .env 設定，並在 .env.example 放佔位說明 (見 GEMINI.md 資安不變量)。
"""

import os
import re
import subprocess
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

SKIP_SUFFIXES = (".lock", ".ico", ".png", ".jpg")

KEY_PATTERNS = {
    "Anthropic 金鑰": r"sk-ant-[\w-]{20,}",
    "Google API 金鑰": r"AIza[0-9A-Za-z_-]{30,}",
    "中央氣象署授權碼": r"CWA-[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
    "Discord Bot Token": r"[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27,}",
}


def _repo_files():
    """git 追蹤的檔案 + 未被忽略的新檔 (即將提交的內容)"""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    for rel in out.splitlines():
        path = os.path.join(PROJECT_ROOT, rel)
        if os.path.isfile(path) and not rel.endswith(SKIP_SUFFIXES):
            with open(path, encoding="utf-8", errors="ignore") as f:
                yield rel, f.read()


def _env_secrets():
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.isfile(env_path):
        return {}
    from dotenv import dotenv_values
    return {
        k: v for k, v in dotenv_values(env_path).items()
        if v and len(v) >= 8 and re.search(r"(KEY|TOKEN|SECRET|PASSWORD|IDS)$", k)
    }


class TestPublicSafety(unittest.TestCase):
    def test_runtime_log_is_git_ignored(self):
        """：紀錄含使用者訊息內容，只能寫在被忽略的 _temp/logs/"""
        for rel in ("_temp/logs/arq.log", "_temp/logs/arq.log.1"):
            result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=PROJECT_ROOT)
            self.assertEqual(result.returncode, 0, f"{rel} 沒有被 git 忽略")

    def test_env_values_not_in_repo(self):
        secrets = _env_secrets()
        leaks = [(rel, k) for rel, text in _repo_files() for k, v in secrets.items() if v in text]
        self.assertEqual(leaks, [], f".env 的值出現在會進版控的檔案：{leaks}")

    def test_no_personal_home_path(self):
        home = os.path.expanduser("~")
        variants = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
        leaks = [rel for rel, text in _repo_files() if any(v in text for v in variants)]
        self.assertEqual(leaks, [], f"檔案中寫死了個人家目錄路徑，請改用 .env 或 os.path.expanduser：{leaks}")

    def test_no_key_patterns(self):
        leaks = [(rel, name) for rel, text in _repo_files()
                 for name, pattern in KEY_PATTERNS.items() if re.search(pattern, text)]
        self.assertEqual(leaks, [], f"檔案中出現疑似金鑰：{leaks}")

    def test_chat_memory_files_are_ignored_and_untracked(self):
        """對話記憶可能含私人訊息：只能存在被忽略的 _temp/，git 不得追蹤任何 _temp/ 檔案"""
        for rel in ("_temp/chat_history/1.json", "_temp/pending_reports.json"):
            r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=PROJECT_ROOT)
            self.assertEqual(r.returncode, 0, f"{rel} 沒有被 .gitignore 忽略")
        tracked = subprocess.run(["git", "ls-files", "--", "_temp"], cwd=PROJECT_ROOT,
                                 capture_output=True, text=True, encoding="utf-8").stdout.split()
        self.assertEqual(tracked, [], f"_temp/ 底下有檔案進了版控：{tracked}")


if __name__ == "__main__":
    unittest.main()
