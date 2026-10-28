"""
通用 HTTP API 呼叫工具 (call_api)

讓 Gemini 能呼叫任意 REST API (公開 API、本機服務等)。

安全設計 (修改時必須維持)：
    1. 風險：GET / HEAD 直接執行；POST / PUT / PATCH / DELETE 會修改遠端資源，需使用者同意 (call_api_risk)。
    2. 金鑰：Gemini 看不到也不能自帶金鑰 (headers 不得含 Authorization / Cookie 等)。
       需要金鑰時只能填 credential=<環境變數名稱>，且該名稱必須在 .env 的 HTTP_API_CREDENTIALS 綁定主機：
           HTTP_API_CREDENTIALS=GITHUB_TOKEN@api.github.com,WX_API_KEY@api.wx.example.com#X-API-Key
       `#Header` 省略時用 `Authorization: Bearer <值>`。金鑰只走 Header、只送 https、主機必須完全相符，
       避免被 Prompt Injection 誘導把金鑰送到其他網站。ArQ 自身的核心金鑰 (_CORE_SECRETS) 一律不可注入。
    3. 不跟隨重新導向：3xx 直接回傳 Location，避免金鑰 Header 被帶到別的主機。
    4. 唯讀交易：本機 (localhost) Shioaji HTTP 伺服器的下單、改單、刪單端點一律拒絕 (永豐查詢請用 tools/shioaji_tools.py)。
    5. 回傳結果經 redact_obj() 遮蔽，錯誤訊息經 redact()。
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from tools.risk_policy import MERGE_REASON
from tools.secret_guard import _is_secret_name, redact, redact_obj

REQUEST_TIMEOUT = 20
MAX_RESPONSE_BYTES = 1_000_000     # 最多讀取 1MB
MAX_TEXT_CHARS = 6000              # 非 JSON (或過大 JSON) 回應回傳給 Gemini 的字元上限
MAX_JSON_CHARS = 20000             # JSON 回應超過此長度改以截斷文字回傳，避免灌爆 Gemini context
USER_AGENT = "ArQ/0.1 (+call_api)"

ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
READ_METHODS = {"GET", "HEAD"}

# Gemini 不得自行設定的 Header (金鑰 / 身分 / 連線控制)
_FORBIDDEN_HEADERS = {"authorization", "proxy-authorization", "cookie", "host", "x-api-key", "api-key"}

# ArQ 自身的核心金鑰：就算 .env 綁定了也不注入
_CORE_SECRETS = {"DISCORD_BOT_TOKEN", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "TICKETS_GITHUB_TOKEN", "TICKETS_PROJECT_TOKEN"}

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# GitHub PR merge 端點 (PUT /repos/{o}/{r}/pulls/{n}/merge)
_MERGE_ENDPOINT_RE = re.compile(r"/pulls/\d+/merge\b", re.IGNORECASE)
# 本機 Shioaji HTTP 伺服器的交易端點 (含任何反向代理前綴)
_BLOCKED_LOCAL_PATH_RE = re.compile(
    r"/order/(place_order|update_price|update_qty|update_order|cancel_order|place_comboorder|cancel_comboorder)\b",
    re.IGNORECASE,
)


def call_api_risk(args: Dict[str, Any]) -> Optional[str]:
    """GET / HEAD 直接執行；其他方法會修改遠端資源，需使用者同意"""
    method = str(args.get("method") or "GET").strip().upper()
    if method in READ_METHODS:
        return None
    if _MERGE_ENDPOINT_RE.search(str(args.get("url") or "")):
        return f"{MERGE_REASON}（{method} PR merge 端點）"  # 見 risk_policy 的 merge 守門
    return f"呼叫 API 修改遠端資源（{method}）"


def _fail(message: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": redact(message), **extra}


def _parse_json_object(text: str, field: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not text or not text.strip():
        return {}, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"{field} 不是合法的 JSON：{e}"
    if not isinstance(value, dict):
        return None, f"{field} 必須是 JSON 物件 (例如 {{\"key\": \"value\"}})"
    return value, None


def _credential_bindings() -> Dict[str, Tuple[str, str]]:
    """解析 HTTP_API_CREDENTIALS → {變數名稱: (主機, Header 名稱)}"""
    bindings: Dict[str, Tuple[str, str]] = {}
    for item in os.getenv("HTTP_API_CREDENTIALS", "").split(","):
        item = item.strip()
        if "@" not in item:
            continue
        name, _, rest = item.partition("@")
        host, _, header = rest.partition("#")
        if name.strip() and host.strip():
            bindings[name.strip()] = (host.strip().lower(), header.strip() or "Authorization")
    return bindings


def _resolve_credential(name: str, parsed: urllib.parse.ParseResult) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    """回傳 ((Header 名稱, Header 值), 錯誤)；不合規一律拒絕 (fail-closed)"""
    name = name.strip()
    if name in _CORE_SECRETS:
        return None, f"{name} 是 ArQ 自身的核心金鑰，不能用於 call_api。"
    if not _is_secret_name(name):
        return None, "credential 名稱必須以 _API_KEY / _TOKEN / _SECRET 結尾。"
    binding = _credential_bindings().get(name)
    if not binding:
        return None, f"{name} 尚未在 .env 的 HTTP_API_CREDENTIALS 綁定主機，請使用者先設定。"
    host, header = binding
    if parsed.scheme != "https":
        return None, "帶金鑰的請求只能使用 https。"
    if (parsed.hostname or "").lower() != host:
        return None, f"{name} 只能送往綁定的主機，不能送往 {parsed.hostname}。"
    value = os.getenv(name, "").strip()
    if not value:
        return None, f".env 中沒有設定 {name} 的值。"
    if header.lower() == "authorization":
        return (header, f"Bearer {value}"), None
    return (header, value), None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不跟隨，讓 3xx 以 HTTPError 回到 _send


_OPENER = urllib.request.build_opener(_NoRedirect)


def _send(req: urllib.request.Request, timeout: int) -> Tuple[int, Dict[str, str], bytes]:
    """實際送出請求 (測試時 mock 這個函式)；非 2xx 也回傳狀態碼與內容，不拋例外"""
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        body = e.read(MAX_RESPONSE_BYTES + 1) if e.fp else b""
        return e.code, dict(e.headers.items()) if e.headers else {}, body


def call_api(url: str, method: str = "GET", query: str = "", json_body: str = "",
             headers: str = "", credential: str = "") -> Dict[str, Any]:
    """
    呼叫 HTTP / REST API 並回傳結果 (JSON 會自動解析)。GET / HEAD 直接執行；POST / PUT / PATCH / DELETE 需使用者同意。
    永豐帳務 / 行情請改用 shioaji 工具；本機 Shioaji 下單、改單、刪單端點一律不開放。

    Args:
        url: 完整網址 (http 或 https)，例如「https://api.github.com/repos/python/cpython」。
        method: HTTP 方法：GET (預設)、HEAD、POST、PUT、PATCH、DELETE。
        query: 查詢參數，JSON 物件字串，例如 '{"q": "taipei", "page": 1}'；留空則不加。
        json_body: 請求內容，JSON 物件字串 (POST / PUT / PATCH 用)；留空則不送。
        headers: 額外的 Header，JSON 物件字串，例如 '{"Accept": "application/json"}'。不得放金鑰。
        credential: 需要金鑰時填 .env 中的金鑰名稱 (例如「GITHUB_TOKEN」)，系統會自動放進 Header；不需要就留空。
    """
    # 註：上方 docstring 會被 Gemini 當成工具說明。
    method = (method or "GET").strip().upper()
    if method not in ALLOWED_METHODS:
        return _fail(f"不支援的 HTTP 方法「{method}」，可用：{'、'.join(sorted(ALLOWED_METHODS))}")

    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return _fail("url 必須是完整的 http:// 或 https:// 網址。")

    if (parsed.hostname or "").lower() in _LOCAL_HOSTS and _BLOCKED_LOCAL_PATH_RE.search(parsed.path):
        return _fail("此端點涉及下單 / 改單 / 刪單，ArQ 僅開放唯讀查詢，請使用者自行在券商系統操作。")

    query_obj, err = _parse_json_object(query, "query")
    if err:
        return _fail(err)
    body_obj, err = _parse_json_object(json_body, "json_body") if json_body and json_body.strip() else (None, None)
    if err:
        return _fail(err)
    header_obj, err = _parse_json_object(headers, "headers")
    if err:
        return _fail(err)
    bad = [k for k in header_obj if str(k).strip().lower() in _FORBIDDEN_HEADERS]
    if bad:
        return _fail(f"headers 不得包含 {', '.join(bad)}；需要金鑰請改用 credential 參數。")

    final_url = url.strip()
    if query_obj:
        sep = "&" if parsed.query else "?"
        final_url += sep + urllib.parse.urlencode({k: str(v) for k, v in query_obj.items()})

    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    req_headers.update({str(k): str(v) for k, v in header_obj.items()})
    data = None
    if body_obj is not None:
        data = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    if credential and credential.strip():
        cred, err = _resolve_credential(credential, parsed)
        if err:
            return _fail(err)
        req_headers[cred[0]] = cred[1]

    req = urllib.request.Request(final_url, data=data, headers=req_headers, method=method)
    try:
        status, resp_headers, raw = _send(req, REQUEST_TIMEOUT)
    except Exception as e:
        return _fail(f"連線失敗：{type(e).__name__}: {e}")

    lower_headers = {k.lower(): v for k, v in resp_headers.items()}
    truncated = len(raw) > MAX_RESPONSE_BYTES
    raw = raw[:MAX_RESPONSE_BYTES]
    result: Dict[str, Any] = {
        "success": 200 <= status < 300,
        "status": status,
        "content_type": lower_headers.get("content-type", ""),
    }
    if 300 <= status < 400 and lower_headers.get("location"):
        result["location"] = lower_headers["location"]
        result["note"] = "未自動跟隨重新導向；需要的話請以 location 重新呼叫。"

    text = raw.decode("utf-8", errors="replace")
    parsed_json = None
    if text.strip() and not truncated:
        try:
            parsed_json = json.loads(text)
        except json.JSONDecodeError:
            parsed_json = None
    if parsed_json is not None and len(text) <= MAX_JSON_CHARS:
        result["data"] = parsed_json
    elif text:
        if len(text) > MAX_TEXT_CHARS:
            text, truncated = text[:MAX_TEXT_CHARS], True
        result["text"] = text
    result["truncated"] = truncated
    return redact_obj(result)
