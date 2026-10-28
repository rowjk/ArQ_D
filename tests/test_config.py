"""
 集中路徑與環境設定 tools/config.py (不連網)

驗收標準：
    - PROJECT_ROOT、TEMP_DIR、temp_path()、env_str/env_int/env_float/env_bool、feature_summary()
    - 環境變數在呼叫時才讀 (不在 import 時快取)
    - feature_summary() 只輸出功能名稱與 ✅/⚪，絕不輸出值
    - 各模組不再自行計算專案根目錄，也不再自行組 _temp 路徑
"""

import glob
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import config  # noqa: E402


class PathTests(unittest.TestCase):
    def test_project_root_contains_agent(self):
        self.assertTrue(os.path.isabs(config.PROJECT_ROOT))
        self.assertTrue(os.path.isfile(os.path.join(config.PROJECT_ROOT, "agent.py")))
        self.assertEqual(os.path.normcase(config.PROJECT_ROOT), os.path.normcase(PROJECT_ROOT))

    def test_temp_dir_under_root(self):
        self.assertEqual(config.TEMP_DIR, os.path.join(config.PROJECT_ROOT, "_temp"))

    def test_temp_path_joins_parts(self):
        self.assertEqual(config.temp_path("a", "b.json"), os.path.join(config.TEMP_DIR, "a", "b.json"))
        self.assertEqual(config.temp_path(), config.TEMP_DIR)

    def test_temp_path_does_not_create_directories(self):
        name = "never_created_%d" % os.getpid()
        target = config.temp_path(name, "x.json")
        self.assertFalse(os.path.exists(os.path.dirname(target)))


class EnvStrTests(unittest.TestCase):
    def test_default_and_strip(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_CFG_STR", None)
            self.assertEqual(config.env_str("X_CFG_STR"), "")
            self.assertEqual(config.env_str("X_CFG_STR", "fallback"), "fallback")
            os.environ["X_CFG_STR"] = "  hello  "
            self.assertEqual(config.env_str("X_CFG_STR", "fallback"), "hello")

    def test_empty_string_returns_empty_not_default(self):
        with mock.patch.dict(os.environ, {"X_CFG_STR": ""}):
            self.assertEqual(config.env_str("X_CFG_STR", "fallback"), "")


class EnvIntTests(unittest.TestCase):
    def test_cases(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_CFG_INT", None)
            self.assertEqual(config.env_int("X_CFG_INT", 3), 3)
            for raw, minimum, expected in [
                ("abc", None, 3), ("", None, 3), ("7", None, 7), (" 7 ", None, 7), ("0", 1, 3),
                ("0", None, 0), ("-5", 1, 3), ("-5", None, -5), ("1", 1, 1), ("1.5", None, 3),
            ]:
                os.environ["X_CFG_INT"] = raw
                self.assertEqual(config.env_int("X_CFG_INT", 3, minimum=minimum), expected, (raw, minimum))


class EnvFloatTests(unittest.TestCase):
    def test_cases(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_CFG_FLOAT", None)
            self.assertEqual(config.env_float("X_CFG_FLOAT", 2.5), 2.5)
            for raw, minimum, expected in [
                ("abc", None, 2.5), ("1.5", None, 1.5), ("0", 1e-9, 2.5), ("-1", 1e-9, 2.5),
                ("0", None, 0.0), ("600", 1e-9, 600.0), ("0.001", 1e-9, 0.001),
            ]:
                os.environ["X_CFG_FLOAT"] = raw
                self.assertEqual(config.env_float("X_CFG_FLOAT", 2.5, minimum=minimum), expected, (raw, minimum))


class EnvBoolTests(unittest.TestCase):
    def test_cases(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_CFG_BOOL", None)
            self.assertFalse(config.env_bool("X_CFG_BOOL"))
            self.assertTrue(config.env_bool("X_CFG_BOOL", True))
            for raw in ("1", "true", "TRUE", "Yes", "on", " ON "):
                os.environ["X_CFG_BOOL"] = raw
                self.assertTrue(config.env_bool("X_CFG_BOOL"), raw)
            for raw in ("0", "false", "no", "off", "maybe"):
                os.environ["X_CFG_BOOL"] = raw
                self.assertFalse(config.env_bool("X_CFG_BOOL", True), raw)
            os.environ["X_CFG_BOOL"] = ""
            self.assertTrue(config.env_bool("X_CFG_BOOL", True))


class NoCachingTests(unittest.TestCase):
    def test_reads_new_value_immediately(self):
        with mock.patch.dict(os.environ, {"X_CFG_LIVE": "1"}):
            self.assertEqual(config.env_int("X_CFG_LIVE", 0), 1)
            os.environ["X_CFG_LIVE"] = "2"
            self.assertEqual(config.env_int("X_CFG_LIVE", 0), 2)
            self.assertEqual(config.env_str("X_CFG_LIVE"), "2")
        self.assertEqual(config.env_int("X_CFG_LIVE", 99), 99)


class FeatureSummaryTests(unittest.TestCase):
    NAMES = ("CWA_API_KEY", "SJ_API_KEY", "SJ_SEC_KEY", "TICKETS_REPO", "TICKETS_GITHUB_TOKEN",
             "TICKETS_PROJECT", "TICKETS_PROJECT_TOKEN", "HTTP_API_CREDENTIALS")

    def _clean_env(self, **extra):
        env = {n: "" for n in self.NAMES}
        env.update(extra)
        return mock.patch.dict(os.environ, env)

    def test_all_off(self):
        with self._clean_env():
            text = config.feature_summary()
        self.assertNotIn("✅", text)
        self.assertEqual(text.count("⚪"), 5)

    def test_partial_requirements_stay_off(self):
        with self._clean_env(SJ_API_KEY="only-one"):
            text = config.feature_summary()
        self.assertIn("⚪永豐", text)

    def test_enabled_and_never_leaks_values(self):
        secrets = {"CWA_API_KEY": "CWA-SECRET-VALUE", "SJ_API_KEY": "SJ-SECRET-A", "SJ_SEC_KEY": "SJ-SECRET-B",
                   "TICKETS_REPO": "owner/private-repo", "TICKETS_GITHUB_TOKEN": "ghp_tokenvalue"}
        with self._clean_env(**secrets):
            text = config.feature_summary()
        self.assertIn("✅天氣", text)
        self.assertIn("✅永豐", text)
        self.assertIn("✅Ticket", text)
        self.assertIn("⚪看板", text)
        for value in secrets.values():
            self.assertNotIn(value, text)


class NoSelfComputedRootTests(unittest.TestCase):
    """除了 config.py，tools/ 不再自行計算專案根目錄或 _temp 路徑"""

    ROOT_PATTERNS = [
        re.compile(r"""__file__[^\n]*["']\.\.["']"""),
        re.compile(r"dirname\(\s*(?:os\.path\.)?dirname\("),
    ]
    TEMP_PATTERN = re.compile(r"""["']_temp["']""")

    def _sources(self):
        for path in sorted(glob.glob(os.path.join(PROJECT_ROOT, "tools", "*.py"))):
            # claude_gate_hook.py 是獨立子程序，要先補 sys.path 才能 import tools，所以不能用 config
            if os.path.basename(path) in ("config.py", "claude_gate_hook.py"):
                continue
            with open(path, encoding="utf-8") as f:
                yield os.path.basename(path), f.read()

    def test_no_self_computed_root(self):
        offenders = []
        for name, text in self._sources():
            for line in text.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if any(p.search(line) for p in self.ROOT_PATTERNS):
                    offenders.append(f"{name}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_no_hardcoded_temp_dir(self):
        offenders = []
        for name, text in self._sources():
            for line in text.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if self.TEMP_PATTERN.search(line):
                    offenders.append(f"{name}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_modules_share_config_root(self):
        from tools import (antigravity_tools, claude_tools, job_tracker, mcp_config, rd_common, system_tools,
                           usage_log, worktree)
        for module in (antigravity_tools, claude_tools, job_tracker, rd_common, system_tools, usage_log, worktree):
            self.assertEqual(module.PROJECT_ROOT, config.PROJECT_ROOT, module.__name__)
        self.assertEqual(mcp_config._ROOT, config.PROJECT_ROOT)


class RefactoredPathsTests(unittest.TestCase):
    def test_state_files_use_temp_path(self):
        from tools import mcp_config, mcp_manager, rd_jobs, usage_log
        with mock.patch.dict(os.environ, {"ARQ_MCP_APPROVED": "", "ARQ_MCP_PIDS": ""}):
            self.assertEqual(mcp_config.approved_path(), config.temp_path("mcp_approved.json"))
            self.assertEqual(mcp_manager._pid_path(), config.temp_path("mcp_pids.json"))
        self.assertEqual(rd_jobs._DEFAULT_STATE_PATH, config.temp_path("rd_jobs_running.json"))
        self.assertEqual(usage_log.USAGE_LOG_PATH, config.temp_path("gemini_usage.jsonl"))

    def test_mcp_path_overrides_still_win(self):
        from tools import mcp_config, mcp_manager
        with mock.patch.dict(os.environ, {"ARQ_MCP_APPROVED": "/x/approved.json", "ARQ_MCP_PIDS": "/x/pids.json"}):
            self.assertEqual(mcp_config.approved_path(), "/x/approved.json")
            self.assertEqual(mcp_manager._pid_path(), "/x/pids.json")

    def test_restart_manager_default_state_dir(self):
        from tools import restart_manager
        with mock.patch("tools.restart_manager.os.makedirs") as makedirs:
            manager = restart_manager.RestartManager()
        self.assertEqual(manager.state_dir, config.TEMP_DIR)
        makedirs.assert_called_once_with(config.TEMP_DIR, exist_ok=True)

    def test_restart_manager_explicit_state_dir(self):
        from tools import restart_manager
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(restart_manager.RestartManager(state_dir=tmp).state_dir, tmp)


class NumericHelpersKeepBehaviorTests(unittest.TestCase):
    def test_agent_int_env(self):
        import agent
        with mock.patch.dict(os.environ, {"X_CFG_A": "7"}):
            self.assertEqual(agent._int_env("X_CFG_A", 3), 7)
            for raw in ("abc", "0", "-2", ""):
                os.environ["X_CFG_A"] = raw
                self.assertEqual(agent._int_env("X_CFG_A", 3), 3, raw)

    def test_rd_common_positive_seconds(self):
        from tools import rd_common
        with mock.patch.dict(os.environ, {"X_CFG_S": "12.5"}):
            self.assertEqual(rd_common._positive_seconds("X_CFG_S", 600), 12.5)
            for raw in ("abc", "0", "-1", ""):
                os.environ["X_CFG_S"] = raw
                self.assertEqual(rd_common._positive_seconds("X_CFG_S", 600), 600, raw)

    def test_mcp_manager_int_env(self):
        from tools import mcp_manager
        with mock.patch.dict(os.environ, {"X_CFG_M": "9"}):
            self.assertEqual(mcp_manager._int_env("X_CFG_M", 4), 9)
            for raw in ("abc", "0", "-3", ""):
                os.environ["X_CFG_M"] = raw
                self.assertEqual(mcp_manager._int_env("X_CFG_M", 4), 4, raw)

    def test_gemini_cache_ttl(self):
        from tools import gemini_cache
        with mock.patch.dict(os.environ, {"GEMINI_EXPLICIT_CACHE_TTL_MINUTES": "5"}):
            self.assertEqual(gemini_cache.cache_ttl_seconds(), 300)
            for raw in ("abc", "0", "-1", ""):
                os.environ["GEMINI_EXPLICIT_CACHE_TTL_MINUTES"] = raw
                self.assertEqual(gemini_cache.cache_ttl_seconds(), gemini_cache.DEFAULT_TTL_MINUTES * 60, raw)

    def test_gemini_settings_levels(self):
        from tools import gemini_settings
        with mock.patch.dict(os.environ, {"GEMINI_THINKING_LEVEL": "Medium", "GEMINI_DEEP_THINKING_LEVEL": "default"}):
            self.assertEqual(gemini_settings.thinking_level(), "MEDIUM")
            self.assertIsNone(gemini_settings.deep_thinking_level())
        with mock.patch.dict(os.environ, {"GEMINI_THINKING_LEVEL": "", "GEMINI_DEEP_THINKING_LEVEL": ""}):
            self.assertEqual(gemini_settings.thinking_level(), "LOW")
            self.assertEqual(gemini_settings.deep_thinking_level(), "HIGH")


if __name__ == "__main__":
    unittest.main()
