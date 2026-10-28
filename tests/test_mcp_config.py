"""
 MCP 設定檔載入與驗證（不連外、不啟動任何程序）

驗收標準：
    - mcp_servers.json 在 .gitignore，mcp_servers.example.json 入版控且只用 ${ENV_VAR}
    - 金鑰類 env 只能整段寫成 ${NAME}；不得引用核心金鑰；未設定的變數視為該 server 設定錯誤
    - 錯誤只影響該 server；指紋隨 command / args / env / auto_approve / include 改變
"""

import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import mcp_config  # noqa: E402


def _write(tmp, data):
    path = os.path.join(tmp, "mcp_servers.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    return path


def _servers(entries):
    return {"mcpServers": entries}


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, data, environ=None):
        return mcp_config.load_config(_write(self.tmp.name, data), environ=environ or {})

    def test_missing_file_means_no_servers(self):
        res = mcp_config.load_config(os.path.join(self.tmp.name, "nope.json"), environ={})
        self.assertEqual(res.servers, {})
        self.assertEqual(res.errors, {})
        self.assertIsNone(res.file_error)

    def test_valid_server_with_defaults(self):
        res = self.load(_servers({"everything": {"command": "npx", "args": ["-y", "pkg@1.0.0"]}}))
        cfg = res.servers["everything"]
        self.assertEqual((cfg.command, cfg.args), ("npx", ["-y", "pkg@1.0.0"]))
        self.assertEqual(cfg.call_timeout, mcp_config.DEFAULT_CALL_TIMEOUT)
        self.assertEqual(cfg.startup_timeout, mcp_config.DEFAULT_STARTUP_TIMEOUT)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.auto_approve, frozenset())

    def test_broken_json_is_file_error(self):
        res = self.load("{not json")
        self.assertEqual(res.servers, {})
        self.assertIn("JSON", res.file_error)

    def test_bad_top_level(self):
        self.assertIn("mcpServers", self.load({"servers": {}}).file_error)

    def test_unknown_field_rejected_only_for_that_server(self):
        res = self.load(_servers({"good": {"command": "x"}, "bad": {"command": "x", "shell": True}}))
        self.assertIn("good", res.servers)
        self.assertIn("shell", res.errors["bad"])

    def test_invalid_server_name(self):
        for name in ("Bad", "a_b", "x y", "a__b", ""):
            with self.subTest(name=name):
                res = self.load(_servers({name: {"command": "x"}}))
                self.assertEqual(res.servers, {})
                self.assertEqual(len(res.errors), 1)

    def test_command_required_and_args_must_be_strings(self):
        self.assertIn("command", self.load(_servers({"a": {}})).errors["a"])
        self.assertIn("args", self.load(_servers({"a": {"command": "x", "args": "-y"}})).errors["a"])
        self.assertIn("args", self.load(_servers({"a": {"command": "x", "args": [1]}})).errors["a"])

    def test_args_cannot_carry_variable_references(self):
        res = self.load(_servers({"a": {"command": "x", "args": ["--token=${FOO_TOKEN}"]}}), {"FOO_TOKEN": "v" * 12})
        self.assertIn("env", res.errors["a"])

    def test_timeouts_validated(self):
        res = self.load(_servers({"a": {"command": "x", "call_timeout": 5, "startup_timeout": 7}}))
        self.assertEqual((res.servers["a"].call_timeout, res.servers["a"].startup_timeout), (5, 7))
        for bad in (0, -1, "10", True, 99999):
            with self.subTest(bad=bad):
                self.assertIn("a", self.load(_servers({"a": {"command": "x", "call_timeout": bad}})).errors)

    def test_secret_env_must_be_exact_reference(self):
        env = {"MY_DRIVE_TOKEN": "tok-" + "z" * 12}
        ok = self.load(_servers({"a": {"command": "x", "env": {"DRIVE_TOKEN": "${MY_DRIVE_TOKEN}"}}}), env)
        self.assertEqual(ok.servers["a"].env["DRIVE_TOKEN"], env["MY_DRIVE_TOKEN"])
        for literal in ("sk-hardcoded-value", "prefix-${MY_DRIVE_TOKEN}", ""):
            with self.subTest(value=literal):
                res = self.load(_servers({"a": {"command": "x", "env": {"DRIVE_TOKEN": literal}}}), env)
                self.assertIn("a", res.errors)
                self.assertNotIn("sk-hardcoded-value", res.errors["a"])

    def test_non_secret_env_may_be_literal_or_interpolated(self):
        res = self.load(_servers({"a": {"command": "x", "env": {"MODE": "fast", "DIR": "${BASE}/x"}}}), {"BASE": "/data"})
        self.assertEqual(res.servers["a"].env, {"MODE": "fast", "DIR": "/data/x"})

    def test_core_secrets_cannot_be_referenced_or_named(self):
        env = {name: "v" * 20 for name in ("DISCORD_BOT_TOKEN", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "TICKETS_GITHUB_TOKEN",
                                           "TICKETS_PROJECT_TOKEN", "TICKETS_REPO")}
        for ref in env:
            with self.subTest(ref=ref):
                res = self.load(_servers({"a": {"command": "x", "env": {"SOME_TOKEN": "${%s}" % ref}}}), env)
                self.assertIn("核心", res.errors["a"])
        res = self.load(_servers({"a": {"command": "x", "env": {"GEMINI_API_KEY": "${OTHER_API_KEY}"}}}),
                        {"OTHER_API_KEY": "v" * 20})
        self.assertIn("核心", res.errors["a"])

    def test_unset_variable_is_server_error(self):
        res = self.load(_servers({"a": {"command": "x", "env": {"X_TOKEN": "${MISSING_TOKEN}"}}}), {})
        self.assertIn("MISSING_TOKEN", res.errors["a"])

    def test_disabled_server_is_listed_but_flagged(self):
        res = self.load(_servers({"a": {"command": "x", "enabled": False}}))
        self.assertFalse(res.servers["a"].enabled)

    def test_filters_and_auto_approve_must_be_string_lists(self):
        res = self.load(_servers({"a": {"command": "x", "include": ["search"], "exclude": ["rm"], "auto_approve": ["search"]}}))
        cfg = res.servers["a"]
        self.assertEqual((cfg.include, cfg.exclude, cfg.auto_approve), (("search",), ("rm",), frozenset({"search"})))
        self.assertIn("a", self.load(_servers({"a": {"command": "x", "auto_approve": "all"}})).errors)

    def test_secret_env_values_are_exposed_for_redaction(self):
        env = {"MY_DRIVE_TOKEN": "tok-" + "z" * 12}
        res = self.load(_servers({"a": {"command": "x", "env": {"DRIVE_TOKEN": "${MY_DRIVE_TOKEN}"}}}), env)
        self.assertEqual(res.secret_values, [env["MY_DRIVE_TOKEN"]])


class TestFingerprint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fp(self, entry, environ=None):
        res = mcp_config.load_config(_write(self.tmp.name, _servers({"a": entry})), environ=environ or {})
        return res.servers["a"].fingerprint

    def test_stable_and_sensitive_to_execution_fields(self):
        base = {"command": "npx", "args": ["-y", "p@1"]}
        self.assertEqual(self.fp(base), self.fp(dict(base)))
        for change in ({"command": "node"}, {"args": ["-y", "p@2"]}, {"env": {"A": "1"}},
                       {"auto_approve": ["echo"]}, {"include": ["echo"]}, {"exclude": ["x"]}):
            with self.subTest(change=change):
                self.assertNotEqual(self.fp(base), self.fp({**base, **change}))

    def test_timeouts_do_not_change_fingerprint(self):
        base = {"command": "npx"}
        self.assertEqual(self.fp(base), self.fp({**base, "call_timeout": 99, "startup_timeout": 99}))

    def test_fingerprint_does_not_contain_secret_values(self):
        env = {"MY_DRIVE_TOKEN": "tok-" + "z" * 12}
        entry = {"command": "x", "env": {"DRIVE_TOKEN": "${MY_DRIVE_TOKEN}"}}
        self.assertEqual(self.fp(entry, env), self.fp(entry, {"MY_DRIVE_TOKEN": "tok-" + "q" * 12}))


class TestApprovalStore(unittest.TestCase):
    def test_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "approved.json")
            self.assertEqual(mcp_config.load_approved(path), {})
            mcp_config.save_approved(path, {"a": "f1"})
            self.assertEqual(mcp_config.load_approved(path), {"a": "f1"})
            with open(path, "w", encoding="utf-8") as f:
                f.write("garbage")
            self.assertEqual(mcp_config.load_approved(path), {})


class TestRepoFiles(unittest.TestCase):
    def test_gitignore_has_real_config_but_not_example(self):
        with open(os.path.join(PROJECT_ROOT, ".gitignore"), encoding="utf-8") as f:
            lines = [line.strip() for line in f]
        self.assertIn("mcp_servers.json", lines)
        self.assertNotIn("mcp_servers.example.json", lines)

    def test_example_is_valid_and_uses_references_only(self):
        path = os.path.join(PROJECT_ROOT, "mcp_servers.example.json")
        res = mcp_config.load_config(path, environ={"EXAMPLE_API_KEY": "v" * 20})
        self.assertIsNone(res.file_error)
        self.assertEqual(res.errors, {})
        self.assertTrue(res.servers)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for entry in data["mcpServers"].values():
            for key, value in (entry.get("env") or {}).items():
                if mcp_config.is_secret_env_name(key):
                    self.assertRegex(value, r"^\$\{[A-Z0-9_]+\}$")
            for arg in entry.get("args", []):
                if "@" in arg and not arg.startswith("-"):
                    self.assertRegex(arg, r"@\d")  # 範例要鎖版本


if __name__ == "__main__":
    unittest.main()
