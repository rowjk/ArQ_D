"""
測試用假 MCP stdio server（只用標準函式庫，不連外）

用法：python fake_mcp_server.py [模式...]
模式：
    --paginate       tools/list 分兩頁
    --hang-init      收到 initialize 不回應
    --exit-after-init  握手完成後立即結束
    --stderr-flood   啟動時對 stderr 狂寫
    --huge-line      tools/list 回傳超長的一行
"""

import json
import os
import subprocess
import sys
import threading
import time

MODES = set(sys.argv[1:])

TOOLS = [
    {"name": "echo", "description": "回傳訊息", "inputSchema": {
        "$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
        "properties": {"message": {"type": "string"}}, "required": ["message"], "additionalProperties": False}},
    {"name": "slow", "description": "睡幾秒再回", "inputSchema": {
        "type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "big", "description": "回傳很長的文字", "inputSchema": {
        "type": "object", "properties": {"n": {"type": "integer"}}}},
    {"name": "leak", "description": "回傳環境變數 FAKE_LEAK_TOKEN 的值", "inputSchema": {"type": "object"}},
    {"name": "image", "description": "回傳圖片", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "回傳 isError", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "呼叫時直接結束", "inputSchema": {"type": "object"}},
    {"name": "env", "description": "回報環境變數是否存在", "inputSchema": {
        "type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "spawn_child", "description": "產生一個長時間執行的孫行程並回傳 PID", "inputSchema": {"type": "object"}},
]
EXTRA_TOOL = {"name": "extra-tool.v2", "description": "第二頁的工具", "inputSchema": {"type": "object"}}


_WRITE_LOCK = threading.Lock()


def send(obj):
    with _WRITE_LOCK:
        sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


def reply(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name, args):
    if name == "echo":
        return text_result(f"echo: {args.get('message', '')}")
    if name == "slow":
        time.sleep(float(args.get("seconds", 1)))
        return text_result("slow done")
    if name == "big":
        return text_result("x" * int(args.get("n", 20000)))
    if name == "leak":
        return text_result("the secret is " + os.environ.get("FAKE_LEAK_TOKEN", "(unset)"))
    if name == "image":
        return {"content": [{"type": "image", "data": "QUJDRA==", "mimeType": "image/png"},
                            {"type": "text", "text": "caption"}], "isError": False}
    if name == "fail":
        return text_result("something went wrong", True)
    if name == "crash":
        os._exit(3)
    if name == "env":
        key = str(args.get("name", ""))
        return text_result(f"{key}={'present' if key in os.environ else 'absent'}")
    if name == "spawn_child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return text_result(str(child.pid))
    return None


def main():
    if "--stderr-flood" in MODES:
        for i in range(2000):
            sys.stderr.write(f"flood line {i} " + "y" * 200 + "\n")
        sys.stderr.flush()
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        method, req_id = msg.get("method"), msg.get("id")
        if method is None:
            # 對我們（server → client）的請求的回應：記到 stderr 供測試檢查
            sys.stderr.write("CLIENT_RESPONSE " + json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stderr.flush()
            continue
        if method == "initialize":
            if "--hang-init" in MODES:
                continue
            reply(req_id, {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                           "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}})
        elif method == "notifications/initialized":
            if "--exit-after-init" in MODES:
                return
            send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
        elif method == "notifications/cancelled":
            sys.stderr.write("CANCELLED " + json.dumps(msg["params"]) + "\n")
            sys.stderr.flush()
        elif method == "tools/list":
            if "--huge-line" in MODES:
                sys.stdout.buffer.write(b"A" * 3_000_000 + b"\n")
                sys.stdout.buffer.flush()
                continue
            cursor = (msg.get("params") or {}).get("cursor")
            if "--paginate" in MODES:
                if not cursor:
                    reply(req_id, {"tools": TOOLS, "nextCursor": "page2"})
                else:
                    reply(req_id, {"tools": [EXTRA_TOOL]})
            else:
                reply(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            if params.get("name") == "slow":  # 慢工具在背景執行緒回應，server 其餘時間仍可服務
                threading.Thread(target=lambda: reply(req_id, call_tool("slow", params.get("arguments") or {})),
                                 daemon=True).start()
                continue
            result = call_tool(params.get("name"), params.get("arguments") or {})
            if result is None:
                send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "unknown tool"}})
            else:
                reply(req_id, result)
        elif req_id is not None:
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
