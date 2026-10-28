"""
 PM 系統提示詞拆成 prompts/pm_system.md，工具註冊表拆成 tool_registry.py

驗收標準：
    - prompts/pm_system.md 與拆出前的 SYSTEM_INSTRUCTION 逐字相同 (sha256 鎖定)
    - agent.SYSTEM_INSTRUCTION 仍是 str；檔案讀不到或是空的就啟動失敗，不得默默用空提示詞
    - tool_registry.py 提供 ToolSpec / _TOOL_SPECS / TOOL_REGISTRY / _DISPATCH_TOOLS 與風險、防護函式，agent 重新匯出
    - 顯式快取鍵與拆出前相同 (內容相同就不會重建)
    - risk_policy：指令寫入 prompts/*.md 需要同意
"""

import hashlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import agent  # noqa: E402
import tool_registry  # noqa: E402
from tools import gemini_cache, risk_policy  # noqa: E402

# 拆出前 agent.SYSTEM_INSTRUCTION 的 sha256 與顯式快取鍵 (model="m"、含工具)；改提示詞或工具宣告時請同步更新
PROMPT_SHA256 = "58bc0714b7c21e6033e0645ec73edde59a098261f2fa6e7bc83c99862f5b08d6"
CACHE_KEY = "3726303a2797673c81d83e75d991db0cccd9f646f6e3d6ca2c77a742df72731b"


class TestPromptFile(unittest.TestCase):
    def test_prompt_is_identical_to_the_original(self):
        self.assertIsInstance(agent.SYSTEM_INSTRUCTION, str)
        self.assertEqual(hashlib.sha256(agent.SYSTEM_INSTRUCTION.encode("utf-8")).hexdigest(), PROMPT_SHA256)

    def test_file_is_lf_only_and_matches_the_constant(self):
        with open(agent.PROMPT_PATH, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"\r", raw)
        self.assertEqual(raw.decode("utf-8"), agent.SYSTEM_INSTRUCTION)

    def test_prompt_path_is_prompts_pm_system_md(self):
        self.assertEqual(os.path.normpath(agent.PROMPT_PATH),
                         os.path.normpath(os.path.join(PROJECT_ROOT, "prompts", "pm_system.md")))

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                agent._load_system_instruction(os.path.join(tmp, "nope.md"))

    def test_empty_file_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            for content in ("", "  \n\n"):
                path = os.path.join(tmp, "empty.md")
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
                with self.assertRaises(RuntimeError) as ctx:
                    agent._load_system_instruction(path)
                self.assertIn(path, str(ctx.exception))

    def test_crlf_is_normalized_to_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            lf, crlf = os.path.join(tmp, "lf.md"), os.path.join(tmp, "crlf.md")
            with open(lf, "w", encoding="utf-8", newline="") as f:
                f.write("第一行\n\n第二行\n")
            with open(crlf, "w", encoding="utf-8", newline="") as f:
                f.write("第一行\r\n\r\n第二行\r\n")
            self.assertEqual(agent._load_system_instruction(crlf), agent._load_system_instruction(lf))

    def test_gitattributes_forces_lf(self):
        with open(os.path.join(PROJECT_ROOT, ".gitattributes"), encoding="utf-8") as f:
            self.assertIn("prompts/*.md text eol=lf", f.read())


class TestToolRegistryModule(unittest.TestCase):
    NAMES = ("ToolSpec", "_TOOL_SPECS", "TOOL_REGISTRY", "_DISPATCH_TOOLS", "_shell_risk", "_python_risk",
             "_shell_guard", "_python_guard", "_read_guard", "_dispatch_risk")

    def test_agent_reexports_the_same_objects(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(agent, name), getattr(tool_registry, name))

    def test_registry_is_keyed_by_function_name(self):
        self.assertEqual(list(tool_registry.TOOL_REGISTRY), [s.func.__name__ for s in tool_registry._TOOL_SPECS])
        self.assertEqual(tool_registry._DISPATCH_TOOLS,
                         {"dispatch_claude_engineer_sync", "dispatch_antigravity_engineer_sync"})

    def test_tool_registry_does_not_import_agent(self):
        with open(os.path.join(PROJECT_ROOT, "tool_registry.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertNotRegex(source, r"(?m)^\s*(import agent|from agent import)")

    def test_risk_and_guard_functions_use_tool_registry_names(self):
        from unittest import mock
        with mock.patch.object(tool_registry, "classify_risk", return_value="X") as m:
            self.assertEqual(tool_registry._shell_risk({"command": "echo"}), "X")
            m.assert_called_once_with("echo", "shell")
        self.assertIsNone(tool_registry._dispatch_risk({"scope": "core", "plan": True}))
        self.assertTrue(tool_registry._dispatch_risk({"scope": "core"}))


class TestCacheKeyUnchanged(unittest.TestCase):
    def test_prefix_cache_key_matches_the_pre_split_value(self):
        client = SimpleNamespace(vertexai=False)
        cache = gemini_cache.PrefixCache(client, "m", lambda: agent.SYSTEM_INSTRUCTION,
                                         lambda: [spec.func for spec in agent._TOOL_SPECS])
        self.assertEqual(cache._key(agent.SYSTEM_INSTRUCTION, cache._build_tools(), True), CACHE_KEY)


class TestPromptWriteNeedsConsent(unittest.TestCase):
    def test_writing_prompt_files_needs_consent(self):
        for command in (r"Set-Content prompts\pm_system.md 'x'",
                        "echo hi > prompts/pm_system.md",
                        r"Add-Content .\prompts\other.md 'x'"):
            with self.subTest(command=command):
                self.assertIn("提示詞", risk_policy.classify_risk(command, "shell") or "")
        self.assertIn("提示詞", risk_policy.classify_risk(
            "open('prompts/pm_system.md','w').write('x')", "python") or "")

    def test_other_prompts_folders_and_non_md_do_not_trigger(self):
        for command in (r"Set-Content task\prompts\x.txt 'x'", "Get-Content prompts.txt", "echo prompts"):
            with self.subTest(command=command):
                self.assertIsNone(risk_policy.classify_risk(command, "shell"))


if __name__ == "__main__":
    unittest.main()
