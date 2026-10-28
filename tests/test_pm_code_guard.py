"""
 硬性攔截 PM 自己讀程式碼 (read_text_file / PowerShell / Python)。不連網、不執行指令。

驗收標準：
    - PM 讀取 ArQ 專案的程式碼檔 (.py、.ps1、.bat、.js、.ts…) 時直接拒絕 (不跳卡、不執行)，提示改派 plan=true
    - git diff / git show 這類輸出程式碼內容的指令一併攔截；git status、log、commit / push / PR 不受影響
    - task/ 下的檔案與非程式碼檔照常可讀；一般指令不得被擋
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools.risk_policy import code_read_violation, path_read_violation  # noqa: E402


class TestShell(unittest.TestCase):
    def test_blocked(self):
        for cmd in [
            "Get-Content bot.py", "gc .\\agent.py -TotalCount 50", "cat tools/rd_dispatch.py",
            "type tools\\claude_tools.py", "Select-String -Path *.py -Pattern heartbeat",
            "Get-Content 'tools/system_tools.py'", "git diff", "git diff HEAD~1 -- agent.py",
            "git show HEAD:agent.py", "findstr /s heartbeat *.py", "git grep heartbeat",
            "Get-ChildItem tools | Get-Content", "(Get-Content run.ps1).Count",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIn("plan=true", code_read_violation(cmd, "shell"))

    def test_allowed(self):
        for cmd in [
            "git status", "git log --oneline -5", "git add -A", 'git commit -m "fix: 修改 bot.py 的心跳"',
            "git push -u origin ticket-10", "gh pr create --fill", "Get-Process python", "Get-Content task\\report.py",
            "Get-Content task/artifact/result.csv", "cat _temp/gemini_usage.jsonl", "Get-Content README.md",
            "python -m unittest discover -s tests", "uv run python task/crawler.py", "dir tools",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(code_read_violation(cmd, "shell"))


class TestPython(unittest.TestCase):
    def test_blocked(self):
        for code in [
            "print(open('bot.py', encoding='utf-8').read()[:500])",
            "from pathlib import Path\nprint(Path('agent.py').read_text())",
            "import inspect, agent\nprint(inspect.getsource(agent))",
            "import subprocess\nprint(subprocess.run(['git', 'diff'], capture_output=True).stdout)",
        ]:
            with self.subTest(code=code):
                self.assertIn("plan=true", code_read_violation(code, "python"))

    def test_allowed(self):
        for code in [
            "import psutil\nprint(psutil.cpu_percent())",
            "open('task/artifact/out.csv', 'w').write('a,b')",
            "print(open('task/report.py').read())",
            "import json\nprint(json.load(open('_temp/restart_history.json')))",
        ]:
            with self.subTest(code=code):
                self.assertIsNone(code_read_violation(code, "python"))


class TestReadTextFile(unittest.TestCase):
    def test_blocked(self):
        for path in ["bot.py", "tools/rd_dispatch.py", "C:\\Users\\x\\ArQ\\agent.py", "run.ps1", "scripts\\sync_skills.py"]:
            with self.subTest(path=path):
                self.assertIn("plan=true", path_read_violation(path))

    def test_allowed(self):
        for path in ["task/report.py", "task\\artifact\\out.csv", "README.md", "GEMINI.md", "_temp/gemini_usage.jsonl"]:
            with self.subTest(path=path):
                self.assertIsNone(path_read_violation(path))


class TestAgentWiring(unittest.TestCase):
    def _run(self, tool, args):
        import agent
        executed = []
        spec = agent.TOOL_REGISTRY[tool]
        fake = spec.__class__(func=lambda **kw: executed.append(kw) or {"ok": True}, label=spec.label,
                              risk=spec.risk, guard=spec.guard)
        a = agent.ArQAgent(api_key="")
        with patch.dict(agent.TOOL_REGISTRY, {tool: fake}), self.assertLogs(level="INFO"):
            result, _ = asyncio.run(a._execute_tool(tool, args, None, None))
        return result, executed

    def test_guards_registered_and_refuse_without_running(self):
        for tool, args in (("read_text_file", {"filepath": "bot.py"}),
                           ("run_powershell_sync", {"command": "Get-Content agent.py"}),
                           ("execute_python_code", {"code": "print(open('bot.py').read())"})):
            with self.subTest(tool=tool):
                result, executed = self._run(tool, args)
                self.assertEqual(executed, [])
                self.assertIn("plan=true", result["error"])

    def test_merge_guard_still_works(self):
        result, executed = self._run("run_powershell_sync", {"command": "git push && gh pr merge 3"})
        self.assertEqual(executed, [])
        self.assertIn("單獨", result["error"])

    def test_normal_commands_run(self):
        result, executed = self._run("run_powershell_sync", {"command": "git status"})
        self.assertEqual(len(executed), 1)


if __name__ == "__main__":
    unittest.main()
