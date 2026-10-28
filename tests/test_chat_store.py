"""
 對話記憶與派工結果跨重啟保存。不連網，檔案都寫在暫存目錄。

驗收標準：
    - 歷史存 _temp/chat_history/<user_id>.json、待轉達結果存 _temp/pending_reports.json，原子寫入
    - round-trip 後 model_dump 相同（含 thought_signature bytes）；圖片（inline_data）不存
    - 過期、版本不符、壞檔、驗證失敗 → 空歷史且不拋例外
    - 存檔前遮蔽金鑰；GEMINI_HISTORY_PERSIST=false 完全不讀寫
    - agent：重啟後帶入舊歷史（讀回後再裁切）、/reset 刪檔、待轉達結果重啟後仍在
    - risk_policy：改寫 chat_history / pending_reports.json 需要同意
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from google.genai import types

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

import agent  # noqa: E402
from tools import chat_store, risk_policy  # noqa: E402

SECRET = "sk-test-secret-value-1234567890"


def user(text):
    return types.Content(role="user", parts=[types.Part(text=text)])


def model_call(sig=b"\x00\xffsignature-bytes"):
    return types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(id="id-1", name="run_powershell_sync", args={"command": "git status"}),
        thought_signature=sig)])


def tool_result():
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id="id-1", name="run_powershell_sync", response={"stdout": "clean"}))])


def model_text(text):
    return types.Content(role="model", parts=[types.Part(text=text)])


def image_part():
    return types.Part(inline_data=types.Blob(data=b"\x89PNG", mime_type="image/png"))


def full_turn(question="查狀態", answer="乾淨"):
    return [user(question), model_call(), tool_result(), model_text(answer)]


def dump(history):
    return [c.model_dump(mode="json", exclude_none=True) for c in history]


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(chat_store, "temp_path", lambda *p: os.path.join(self.tmp.name, *p))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GEMINI_HISTORY_PERSIST", None)
        os.environ.pop("GEMINI_HISTORY_MAX_AGE_HOURS", None)
        p = mock.patch.object(chat_store, "logger")
        self.printed = p.start().warning
        self.addCleanup(p.stop)

    def path(self, *parts):
        return os.path.join(self.tmp.name, *parts)

    def history_file(self, uid=1):
        return self.path("chat_history", f"{uid}.json")

    def read_payload(self, uid=1):
        with open(self.history_file(uid), encoding="utf-8") as f:
            return json.load(f)

    def write_payload(self, payload, uid=1):
        os.makedirs(os.path.dirname(self.history_file(uid)), exist_ok=True)
        with open(self.history_file(uid), "w", encoding="utf-8") as f:
            json.dump(payload, f)


class TestRoundTrip(StoreCase):
    def test_round_trip_keeps_function_call_signature_and_response(self):
        history = full_turn()
        chat_store.save_history(1, history)
        loaded = chat_store.load_history(1)
        self.assertEqual(dump(loaded), dump(history))
        self.assertEqual(loaded[1].parts[0].thought_signature, b"\x00\xffsignature-bytes")
        self.assertEqual(loaded[2].parts[0].function_response.response, {"stdout": "clean"})

    def test_missing_file_is_empty(self):
        self.assertEqual(chat_store.load_history(99), [])

    def test_inline_data_parts_removed_and_image_only_content_dropped(self):
        history = [
            types.Content(role="user", parts=[types.Part(text="看這張"), image_part()]),
            types.Content(role="user", parts=[image_part()]),
            model_text("收到"),
        ]
        chat_store.save_history(1, history)
        self.assertNotIn("inline_data", json.dumps(self.read_payload()))
        loaded = chat_store.load_history(1)
        self.assertEqual([c.role for c in loaded], ["user", "model"])
        self.assertEqual(loaded[0].parts[0].text, "看這張")
        self.assertEqual(len(loaded[0].parts), 1)

    def test_secret_is_redacted_in_file(self):
        with mock.patch.dict(os.environ, {"DEMO_API_KEY": SECRET}):
            chat_store.save_history(1, [user(f"我的金鑰是 {SECRET}"), model_text("好")])
        with open(self.history_file(), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(SECRET, raw)
        self.assertIn("***", raw)

    def test_user_id_cannot_escape_directory(self):
        with self.assertRaises((ValueError, TypeError)):
            chat_store.save_history("1/../../evil", [user("x")])

    def test_no_tmp_file_left_behind(self):
        chat_store.save_history(1, full_turn())
        self.assertEqual(os.listdir(self.path("chat_history")), ["1.json"])


class TestInvalidFiles(StoreCase):
    def test_expired_history_not_loaded(self):
        chat_store.save_history(1, full_turn())
        payload = self.read_payload()
        payload["saved_at"] = time.time() - 25 * 3600
        self.write_payload(payload)
        self.assertEqual(chat_store.load_history(1), [])

    def test_max_age_env_is_respected(self):
        chat_store.save_history(1, full_turn())
        payload = self.read_payload()
        payload["saved_at"] = time.time() - 25 * 3600
        self.write_payload(payload)
        with mock.patch.dict(os.environ, {"GEMINI_HISTORY_MAX_AGE_HOURS": "48"}):
            self.assertEqual(len(chat_store.load_history(1)), 4)

    def test_schema_mismatch(self):
        chat_store.save_history(1, full_turn())
        payload = self.read_payload()
        payload["schema"] = chat_store.SCHEMA + 1
        self.write_payload(payload)
        self.assertEqual(chat_store.load_history(1), [])

    def test_corrupt_json_warns_and_returns_empty(self):
        os.makedirs(self.path("chat_history"))
        with open(self.history_file(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(chat_store.load_history(1), [])
        self.assertTrue(self.printed.called)

    def test_non_dict_payload_and_validation_failure(self):
        for payload in ([1, 2], {"schema": chat_store.SCHEMA, "saved_at": time.time(), "history": [{"role": 5, "parts": "x"}]},
                        {"schema": chat_store.SCHEMA, "saved_at": time.time(), "history": "oops"}):
            with self.subTest(payload=payload):
                self.write_payload(payload)
                self.assertEqual(chat_store.load_history(1), [])

    def test_save_failure_only_warns(self):
        with mock.patch.object(chat_store.os, "replace", side_effect=OSError("disk full")):
            chat_store.save_history(1, full_turn())  # 不拋例外
        self.assertTrue(self.printed.called)


class TestDisabled(StoreCase):
    def test_persist_false_writes_and_reads_nothing(self):
        with mock.patch.dict(os.environ, {"GEMINI_HISTORY_PERSIST": "false"}):
            chat_store.save_history(1, full_turn())
            chat_store.save_pending([{"a": 1}])
            self.assertFalse(os.path.exists(self.path("chat_history")))
            self.assertFalse(os.path.exists(self.path("pending_reports.json")))
        chat_store.save_history(1, full_turn())
        chat_store.save_pending([{"a": 1}])
        with mock.patch.dict(os.environ, {"GEMINI_HISTORY_PERSIST": "false"}):
            self.assertEqual(chat_store.load_history(1), [])
            self.assertEqual(chat_store.load_pending(), [])

    def test_default_is_enabled(self):
        self.assertTrue(chat_store.enabled())


class TestClear(StoreCase):
    def test_clear_removes_file_and_tolerates_missing(self):
        chat_store.save_history(1, full_turn())
        chat_store.clear_history(1)
        self.assertFalse(os.path.exists(self.history_file()))
        chat_store.clear_history(1)  # 已不存在也不拋例外


class TestPending(StoreCase):
    def test_round_trip_and_empty_list(self):
        chat_store.save_pending([{"job_id": "a", "text": "完成"}])
        self.assertEqual(chat_store.load_pending(), [{"job_id": "a", "text": "完成"}])
        chat_store.save_pending([])
        self.assertEqual(chat_store.load_pending(), [])

    def test_secret_redacted(self):
        with mock.patch.dict(os.environ, {"DEMO_API_KEY": SECRET}):
            chat_store.save_pending([{"text": SECRET}])
        with open(self.path("pending_reports.json"), encoding="utf-8") as f:
            self.assertNotIn(SECRET, f.read())

    def test_bad_files_return_empty(self):
        for raw in ("{bad", json.dumps({"schema": 99, "items": [{"a": 1}]}),
                    json.dumps({"schema": chat_store.SCHEMA, "items": "x"}),
                    json.dumps({"schema": chat_store.SCHEMA, "items": [1, "a"]})):
            with self.subTest(raw=raw):
                with open(self.path("pending_reports.json"), "w", encoding="utf-8") as f:
                    f.write(raw)
                self.assertEqual(chat_store.load_pending(), [])


# ------------------------------------------------------------------ agent 接點

class FakeChat:
    def __init__(self, history):
        self._history = list(history)

    def get_history(self, curated=False):
        return self._history


class FakeChats:
    def __init__(self):
        self.created = []

    def create(self, model, history, config):
        self.created.append(list(history))
        return FakeChat(history)


class AgentCase(StoreCase):
    def make_agent(self):
        a = agent.ArQAgent(api_key="")
        a.client = mock.Mock()
        a.client.chats = FakeChats()
        return a


class TestAgentRestart(AgentCase):
    def test_new_instance_restores_history(self):
        chat_store.save_history(7, full_turn("舊問題", "舊答案"))
        a = self.make_agent()
        a.get_or_create_chat(7)
        restored = a.client.chats.created[-1]
        self.assertEqual(dump(restored), dump(full_turn("舊問題", "舊答案")))

    def test_restored_history_is_trimmed_again(self):
        old = []
        for i in range(10):
            old += full_turn(f"問題{i}", f"答案{i}")
        chat_store.save_history(7, old)
        a = self.make_agent()
        a.get_or_create_chat(7)
        restored = a.client.chats.created[-1]
        self.assertLessEqual(sum(1 for c in restored if agent._is_user_turn_start(c)), agent.MAX_HISTORY_TURNS)
        self.assertEqual(restored[-1].parts[0].text, "答案9")

    def test_explicit_history_does_not_read_file(self):
        chat_store.save_history(7, full_turn("舊問題"))
        a = self.make_agent()
        a.get_or_create_chat(7, history=[user("新"), model_text("好")])
        self.assertEqual(len(a.client.chats.created[-1]), 2)

    def test_no_saved_history_starts_empty(self):
        a = self.make_agent()
        a.get_or_create_chat(7)
        self.assertEqual(a.client.chats.created[-1], [])

    def test_reset_chat_deletes_file(self):
        chat_store.save_history(7, full_turn())
        a = self.make_agent()
        a.get_or_create_chat(7)
        a.reset_chat(7)
        self.assertFalse(os.path.exists(self.history_file(7)))
        a.get_or_create_chat(7)
        self.assertEqual(a.client.chats.created[-1], [])

    def test_reset_command_deletes_file(self):
        chat_store.save_history(7, full_turn())
        a = self.make_agent()
        result = asyncio.run(a.chat("/reset", user_id=7))
        self.assertIn("重置", result["text"])
        self.assertFalse(os.path.exists(self.history_file(7)))


class TestAgentSavesAfterChat(AgentCase):
    def _chat(self, a, loop_result=None, error=None):
        async def fake_loop(chat_session, prompt, user_id, confirm, notify):
            if error:
                raise error
            chat_session._history += [user(prompt), model_text("回覆")]
            return {"text": "回覆", "files": []}
        a._run_tool_loop = fake_loop
        return asyncio.run(a.chat("你好", user_id=7))

    def test_history_saved_after_message(self):
        a = self.make_agent()
        self._chat(a)
        saved = chat_store.load_history(7)
        self.assertEqual([c.role for c in saved], ["user", "model"])
        self.assertEqual(saved[0].parts[0].text, "你好")

    def test_saved_history_is_trimmed(self):
        old = []
        for i in range(10):
            old += full_turn(f"問題{i}", f"答案{i}")
        a = self.make_agent()
        a.get_or_create_chat(7, history=old)
        self._chat(a)
        saved = chat_store.load_history(7)
        self.assertLessEqual(sum(1 for c in saved if agent._is_user_turn_start(c)), agent.MAX_HISTORY_TURNS)

    def test_exception_resets_and_removes_file(self):
        chat_store.save_history(7, full_turn())
        a = self.make_agent()
        self._chat(a, error=RuntimeError("boom"))
        self.assertFalse(os.path.exists(self.history_file(7)))

    def test_save_failure_does_not_break_reply(self):
        a = self.make_agent()
        with mock.patch.object(chat_store, "save_history", side_effect=OSError("denied")):
            result = self._chat(a)
        self.assertEqual(result["text"], "回覆")


class TestAgentPending(AgentCase):
    def test_pending_survives_restart_and_is_cleared_when_relayed(self):
        a = self.make_agent()
        a._add_pending_report({"job_id": "j1", "status": "done"})
        b = self.make_agent()
        self.assertEqual(b.pending_reports(), [{"job_id": "j1", "status": "done"}])
        prompt = b._with_pending_reports("嗨")
        self.assertIn("j1", prompt)
        self.assertEqual(chat_store.load_pending(), [])
        self.assertEqual(self.make_agent().pending_reports(), [])

    def test_pending_save_failure_does_not_raise(self):
        a = self.make_agent()
        with mock.patch.object(chat_store.os, "replace", side_effect=OSError("denied")):
            a._add_pending_report({"x": 1})
            self.assertIn("x", a._with_pending_reports("嗨"))


# ------------------------------------------------------------------ 風險規則 (隱私檢查在 test_public_safety)

class TestRiskPolicy(unittest.TestCase):
    def test_editing_chat_memory_needs_approval(self):
        for kind, text in (
            ("shell", "Remove-Item _temp\\chat_history\\1.json"),
            ("shell", "Set-Content _temp/pending_reports.json '[]'"),
            ("python", "open('_temp/chat_history/1.json', 'w').write('{}')"),
            ("python", "open('_temp/pending_reports.json', 'w').write('[]')"),
        ):
            with self.subTest(text=text):
                self.assertIn("對話記憶", risk_policy.classify_risk(text, kind) or "")

    def test_unrelated_commands_unaffected(self):
        self.assertIsNone(risk_policy.classify_risk("Get-Date", "shell"))


if __name__ == "__main__":
    unittest.main()
