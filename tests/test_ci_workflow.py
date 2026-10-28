"""
Ticket：GitHub Actions 自動跑全部測試

驗收標準：
- 新增 .github/workflows/tests.yml：pull_request 與 push 到 main 時觸發，在 windows-latest 執行
- 步驟：checkout → astral-sh/setup-uv（固定版本 tag）→ uv sync --frozen → uv run python -m unittest discover -s tests
- Python 版本取自 .python-version (3.13)；workflow 不需要任何 secret，也不讀 .env
- pyproject.toml 的 requires-python 與 README 徽章統一 (實際驗證過的最低版本)
- README「🧪 測試」段補一句 CI 說明
"""

import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外


class TestCIWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = os.path.join(PROJECT_ROOT, ".github", "workflows", "tests.yml")
        cls.pyproject_path = os.path.join(PROJECT_ROOT, "pyproject.toml")
        cls.readme_path = os.path.join(PROJECT_ROOT, "README.md")
        cls.python_version_path = os.path.join(PROJECT_ROOT, ".python-version")

        with open(cls.workflow_path, "r", encoding="utf-8") as f:
            cls.workflow_content = f.read()

        with open(cls.pyproject_path, "r", encoding="utf-8") as f:
            cls.pyproject_content = f.read()

        with open(cls.readme_path, "r", encoding="utf-8") as f:
            cls.readme_content = f.read()

        with open(cls.python_version_path, "r", encoding="utf-8") as f:
            cls.python_version = f.read().strip()

    def test_workflow_file_exists(self):
        self.assertTrue(os.path.isfile(self.workflow_path), "Workflow 檔案不存在")

    def test_workflow_triggers(self):
        # 觸發條件包含 pull_request 與 push 到 main
        self.assertIn("pull_request:", self.workflow_content)
        self.assertIn("push:", self.workflow_content)
        self.assertIn("main", self.workflow_content)

    def test_workflow_runs_on_windows_latest(self):
        self.assertIn("windows-latest", self.workflow_content)

    def test_workflow_steps_and_pinned_tags(self):
        # 步驟包含 checkout 與 setup-uv (固定版本 tag)
        # checkout v4 跑在 Node 20 (已淘汰，CI 會出警告)，改用固定版本的 v7
        self.assertRegex(self.workflow_content, r"actions/checkout@v7\.\d+\.\d+")
        self.assertRegex(self.workflow_content, r"astral-sh/setup-uv@v\d+\.\d+\.\d+")
        self.assertIn("uv sync --frozen", self.workflow_content)
        self.assertIn("uv run python -m unittest discover -s tests", self.workflow_content)
        self.assertIn("PYTHONUTF8", self.workflow_content)

    def test_workflow_no_secrets_and_no_env(self):
        self.assertNotIn("${{ secrets.", self.workflow_content)
        self.assertNotIn(".env", self.workflow_content)

    def test_python_version_consistency(self):
        # .python-version 為 3.13
        self.assertEqual(self.python_version, "3.13")

        # pyproject.toml requires-python 與 README 徽章一致 (3.13)
        self.assertIn('requires-python = ">=3.13"', self.pyproject_content)
        self.assertIn("badge/Python-3.13%2B-blue", self.readme_content)

    def test_readme_ci_mention(self):
        # README「🧪 測試」段落包含 CI 說明
        match = re.search(r"## 🧪 測試(.*?)(?=## |\Z)", self.readme_content, re.DOTALL)
        self.assertIsNotNone(match, "README 中找不到 🧪 測試 段落")
        section = match.group(1)
        self.assertTrue("CI" in section or "GitHub Actions" in section, "README 測試段落應提及 CI 說明")


if __name__ == "__main__":
    unittest.main()
