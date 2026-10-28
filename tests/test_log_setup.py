"""
Ticket 單元測試：改用 logging 並寫入輪替紀錄檔 (tools/log_setup.py)

驗收標準：
- setup_logging()：主控台 + 輪替檔，可重複呼叫不重複掛 handler
- 所有 handler 掛遮蔽過濾器：金鑰（訊息與 traceback）不進檔案
- ARQ_LOG_LEVEL 可調（預設 INFO）
- bot.main() 一開始呼叫 setup_logging、bot.run 帶 log_handler=None
- bot.py、agent.py、tools/ 不再有 print（CLI 與 hook 例外）
- 紀錄檔位置被 git 忽略
"""

import ast
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import log_setup  # noqa: E402

FAKE_KEY = "FAKE-test-key-1234567890abcdef"


def _own_handlers():
    return [h for h in logging.getLogger().handlers if getattr(h, log_setup._MARK, False)]


class LogSetupTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "logs", "arq.log")
        root = logging.getLogger()
        self._root_level = root.level
        self._discord_level = logging.getLogger("discord").level
        self.addCleanup(self._restore_logging)

    def _restore_logging(self):
        log_setup.shutdown_logging()
        logging.getLogger().setLevel(self._root_level)
        logging.getLogger("discord").setLevel(self._discord_level)

    def _read(self):
        for h in _own_handlers():
            h.flush()
        with open(self.path, encoding="utf-8") as f:
            return f.read()


class FileOutputTests(LogSetupTestCase):
    def test_info_and_exception_written_with_traceback(self):
        log_setup.setup_logging(level="INFO", path=self.path)
        logger = logging.getLogger("arq.test")
        logger.info("📩 一般訊息")
        try:
            raise ValueError("壞掉了")
        except ValueError:
            logger.exception("❌ 發生錯誤")
        text = self._read()
        self.assertIn("INFO", text)
        self.assertIn("arq.test: 📩 一般訊息", text)
        self.assertIn("ERROR", text)
        self.assertIn("❌ 發生錯誤", text)
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("ValueError: 壞掉了", text)

    def test_creates_log_directory(self):
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))
        log_setup.setup_logging(path=self.path)
        self.assertTrue(os.path.isdir(os.path.dirname(self.path)))

    def test_file_handler_rotates_1mb_x5(self):
        log_setup.setup_logging(path=self.path)
        files = [h for h in _own_handlers() if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].maxBytes, 1_000_000)
        self.assertEqual(files[0].backupCount, 5)
        self.assertEqual(files[0].encoding.lower().replace("-", ""), "utf8")

    def test_default_path_is_under_temp_logs(self):
        from tools.config import temp_path
        self.assertEqual(log_setup.LOG_PATH, temp_path("logs", "arq.log"))

    def test_discord_logger_quiet_but_warning_reaches_file(self):
        log_setup.setup_logging(level="INFO", path=self.path)
        discord_logger = logging.getLogger("discord.client")
        discord_logger.info("discord 的 info 不該進來")
        discord_logger.warning("discord 警告要進來")
        text = self._read()
        self.assertNotIn("discord 的 info", text)
        self.assertIn("discord 警告要進來", text)


class ConsoleOutputTests(LogSetupTestCase):
    def test_console_format_is_bare_message(self):
        log_setup.setup_logging(path=self.path)
        console = [h for h in _own_handlers() if not isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(console), 1)
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "🔧 執行工具: %s", ("demo",), None)
        self.assertEqual(console[0].format(record), "🔧 執行工具: demo")


class RedactionTests(LogSetupTestCase):
    def test_secret_in_message_and_traceback_is_masked_in_file(self):
        with mock.patch.dict(os.environ, {"FAKE_SERVICE_API_KEY": FAKE_KEY}):
            log_setup.setup_logging(path=self.path)
            logger = logging.getLogger("arq.test")
            logger.warning("金鑰是 %s", FAKE_KEY)
            try:
                raise RuntimeError(f"連線失敗 key={FAKE_KEY}")
            except RuntimeError:
                logger.exception("❌ 例外 %s", FAKE_KEY)
            text = self._read()
        self.assertNotIn(FAKE_KEY, text)
        self.assertIn("***", text)
        self.assertIn("RuntimeError: 連線失敗 key=***", text)

    def test_secret_in_message_masked_on_console(self):
        with mock.patch.dict(os.environ, {"FAKE_SERVICE_API_KEY": FAKE_KEY}):
            log_setup.setup_logging(path=self.path)
            console = [h for h in _own_handlers() if not isinstance(h, RotatingFileHandler)][0]
            records = []
            console.emit = records.append
            logging.getLogger("arq.test").error("token %s", FAKE_KEY)
            # 過濾器在 handle() 內執行；直接以 emit 攔截後的紀錄驗證
            self.assertEqual(len(records), 1)
            self.assertNotIn(FAKE_KEY, console.format(records[0]))

    def test_every_own_handler_has_redact_filter(self):
        log_setup.setup_logging(path=self.path)
        handlers = _own_handlers()
        self.assertEqual(len(handlers), 2)
        for h in handlers:
            self.assertTrue(any(isinstance(f, log_setup.RedactFilter) for f in h.filters))

    def test_filter_does_not_choke_on_bad_format_args(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "需要兩個 %s %s", ("只給一個",), None)
        self.assertTrue(log_setup.RedactFilter().filter(record))


class SetupBehaviourTests(LogSetupTestCase):
    def test_calling_twice_keeps_two_handlers(self):
        log_setup.setup_logging(path=self.path)
        log_setup.setup_logging(path=self.path)
        self.assertEqual(len(_own_handlers()), 2)

    def test_foreign_handlers_untouched(self):
        foreign = logging.NullHandler()
        logging.getLogger().addHandler(foreign)
        self.addCleanup(logging.getLogger().removeHandler, foreign)
        log_setup.setup_logging(path=self.path)
        log_setup.setup_logging(path=self.path)
        self.assertIn(foreign, logging.getLogger().handlers)

    def test_env_level_warning_hides_info(self):
        with mock.patch.dict(os.environ, {"ARQ_LOG_LEVEL": "WARNING"}):
            log_setup.setup_logging(path=self.path)
        logger = logging.getLogger("arq.test")
        logger.info("不該寫入的 info")
        logger.warning("該寫入的 warning")
        text = self._read()
        self.assertNotIn("不該寫入的 info", text)
        self.assertIn("該寫入的 warning", text)

    def test_default_level_is_info(self):
        env = {k: v for k, v in os.environ.items() if k != "ARQ_LOG_LEVEL"}
        with mock.patch.dict(os.environ, env, clear=True):
            log_setup.setup_logging(path=self.path)
        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_invalid_level_falls_back_to_info(self):
        with mock.patch.dict(os.environ, {"ARQ_LOG_LEVEL": "不存在"}):
            log_setup.setup_logging(path=self.path)
        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_level_argument_beats_env(self):
        with mock.patch.dict(os.environ, {"ARQ_LOG_LEVEL": "WARNING"}):
            log_setup.setup_logging(level="debug", path=self.path)
        self.assertEqual(logging.getLogger().level, logging.DEBUG)


class BotWiringTests(unittest.TestCase):
    def test_main_sets_up_logging_first_and_disables_discord_handler(self):
        import bot
        calls = []
        with mock.patch.object(bot, "setup_logging", side_effect=lambda *a, **k: calls.append("setup")), \
                mock.patch.object(bot.single_instance, "acquire", side_effect=lambda *a, **k: calls.append("lock") or True), \
                mock.patch.object(bot, "DISCORD_TOKEN", "x"), \
                mock.patch.object(bot, "ALLOWED_USER_IDS", [1]), \
                mock.patch.object(bot.bot, "run", side_effect=lambda *a, **k: calls.append(("run", k))):
            bot.main()
        self.assertEqual(calls[0], "setup")
        run_call = [c for c in calls if isinstance(c, tuple)][0]
        self.assertIsNone(run_call[1].get("log_handler", "missing"))


_ALLOWED_PRINT_FUNCS = {
    # CLI 輸出（子程序 / 命令列介面），不改（範圍外）
    ("tools/job_tracker.py", "_print_start_result"),
    ("tools/job_tracker.py", "_print_status_result"),
    ("tools/job_tracker.py", "_print_last_log_result"),
    ("tools/ticket_tools.py", "_main"),
}
_SKIP_FILES = {"tools/claude_gate_hook.py"}   # stdout / stderr 是 hook 協定


def _print_calls(rel: str):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = []

    def visit(node, func):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            found.append((node.lineno, func))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, None)
    return found


class NoPrintTests(unittest.TestCase):
    def test_no_print_in_bot_agent_and_tools(self):
        files = ["bot.py", "agent.py"] + sorted(
            f"tools/{n}" for n in os.listdir(os.path.join(PROJECT_ROOT, "tools")) if n.endswith(".py"))
        offenders = [
            f"{rel}:{line}" for rel in files if rel not in _SKIP_FILES
            for line, func in _print_calls(rel) if (rel, func) not in _ALLOWED_PRINT_FUNCS
        ]
        self.assertEqual(offenders, [], f"請改用 logging.getLogger(__name__)：{offenders}")


class GitIgnoreTests(unittest.TestCase):
    def test_log_file_is_git_ignored(self):
        rel = "_temp/logs/arq.log"
        result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=PROJECT_ROOT)
        self.assertEqual(result.returncode, 0, f"{rel} 沒有被 git 忽略（紀錄含使用者訊息，不得進版控）")

    def test_log_path_inside_ignored_temp_dir(self):
        from tools.config import TEMP_DIR
        self.assertTrue(os.path.abspath(log_setup.LOG_PATH).startswith(os.path.abspath(TEMP_DIR) + os.sep))


if __name__ == "__main__":
    unittest.main()
