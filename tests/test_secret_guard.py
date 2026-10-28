"""
tools/secret_guard.py 與相關防護的單元測試：金鑰 / Token 不得外洩

涵蓋：子程序環境移除機密、輸出遮蔽、機密檔拒絕讀取、讀取 .env 的指令需同意。
測試一律使用假的金鑰值，不讀取真正的 .env。
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import secret_guard  # noqa: E402
from tools.file_tools import read_text_file  # noqa: E402
from tools.risk_policy import classify_risk  # noqa: E402

FAKE_ENV = {
    "CWA_API_KEY": "CWA-FAKE0000-1111-2222-3333-444455556666",
    "DISCORD_BOT_TOKEN": "fake.discord.token.value.123456",
    "SOME_SERVICE_TOKEN": "another-fake-secret-value",
    "PATH": "C:\\Windows",
    "GEMINI_MODEL": "gemini-3.8-flash",
}


class TestSecretGuard(unittest.TestCase):
    def test_child_env_strips_secrets(self):
        env = secret_guard.child_env(base=FAKE_ENV)
        self.assertNotIn("CWA_API_KEY", env)
        self.assertNotIn("DISCORD_BOT_TOKEN", env)
        self.assertNotIn("SOME_SERVICE_TOKEN", env)  # 名稱以 TOKEN 結尾自動視為機密
        self.assertEqual(env["PATH"], "C:\\Windows")
        self.assertEqual(env["GEMINI_MODEL"], "gemini-3.8-flash")

    def test_child_env_keep(self):
        env = secret_guard.child_env(keep=["CWA_API_KEY"], base=FAKE_ENV)
        self.assertIn("CWA_API_KEY", env)
        self.assertNotIn("DISCORD_BOT_TOKEN", env)

    def test_redact(self):
        with patch.dict(os.environ, FAKE_ENV):
            text = f"url?Authorization={FAKE_ENV['CWA_API_KEY']} token={FAKE_ENV['DISCORD_BOT_TOKEN']}"
            out = secret_guard.redact(text)
            self.assertNotIn(FAKE_ENV["CWA_API_KEY"], out)
            self.assertNotIn(FAKE_ENV["DISCORD_BOT_TOKEN"], out)
            self.assertIn("***", out)
            nested = secret_guard.redact_obj({"a": [FAKE_ENV["CWA_API_KEY"], 1], "b": None})
            self.assertEqual(nested, {"a": ["***", 1], "b": None})

    def test_secret_files(self):
        for path in [".env", "C:\\x\\ArQ\\.env", ".env.local", "credentials.json", "server.pem", "id_rsa", '".env"']:
            with self.subTest(path=path):
                self.assertTrue(secret_guard.is_secret_file(path))
        for path in [".env.example", "agent.py", "README.md", "environment.yml", ".venv"]:
            with self.subTest(path=path):
                self.assertFalse(secret_guard.is_secret_file(path))

    def test_read_text_file_refuses_env(self):
        res = read_text_file(os.path.join(PROJECT_ROOT, ".env"))
        self.assertIn("error", res)
        self.assertNotIn("content", res)

    def test_reading_env_requires_consent(self):
        for cmd in ["Get-Content .env", "type C:\\Users\\someone\\project\\.env", "cat ../.env"]:
            with self.subTest(cmd=cmd):
                self.assertIn("機密", classify_risk(cmd, "shell") or "")
        self.assertIn("機密", classify_risk("open('.env').read()", "python") or "")
        # 不應誤判
        for cmd in ["Get-Content .env.example", "python -c \"import os; print(os.environ.get('PATH'))\"", "ls .venv"]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(classify_risk(cmd, "shell"))


if __name__ == "__main__":
    unittest.main()
