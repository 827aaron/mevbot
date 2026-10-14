"""假的 OpenAI 协议 API（只监听 127.0.0.1，只用 Python 标准库），用来测真实 API 才会遇到的出错情况：
Key 无效、没有余额、回答格式不对、很慢。软件里按“自定义 API”添加即可（协议选 OpenAI）。

    python3 -I tests/e2e_data/tools/fake_api.py --port 8860

API 地址写法（同一个服务可以同时当几个不同的 API 用）：
    http://127.0.0.1:8860/v1              → 用当前的全局模式（默认 ok）
    http://127.0.0.1:8860/m/<模式>/v1      → 固定某个模式，例如 /m/junk/v1、/m/nobalance/v1
模式：
    ok         正常：按请求里的 JSON 格式要求回一个最简单的合法 JSON（数组为空、字符串为空）；没要求格式就回 "OK"
    junk       回一句不是 JSON 的话（测判断模型「测试」：“这个模型不能按格式回答选择题”）
    nokey      401 Key 无效
    nobalance  402 余额不足（"Insufficient balance"）
    slow       等 90 秒再回（测超时 / 不卡界面）
中途切换全局模式（模拟“用着用着没钱了”）：
    curl -s -X POST http://127.0.0.1:8860/_mode -d '{"mode":"nobalance"}'
Key 随便填（例如 qa-fake-key）。模型列表：qa-fake-small、qa-fake-large。每次请求都记在 /_log（最近 50 条）。
"""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("ok", "junk", "nokey", "nobalance", "slow")
STATE = {"mode": "ok", "log": []}
_lock = threading.Lock()


def minimal(schema):
    """按 JSON Schema 造一个最简单的合法值。"""
    if not isinstance(schema, dict):
        return None
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {k: minimal(props.get(k, {})) for k in (schema.get("required") or list(props))}
    return {"array": [], "string": "", "integer": 0, "number": 0, "boolean": False, "null": None}.get(t, "")


def schema_from(messages):
    """软件把 JSON 格式要求写在 system 提示词里（"JSON Schema:\\n{...}"）。"""
    for m in messages or []:
        c = m.get("content")
        c = c if isinstance(c, str) else json.dumps(c)
        i = c.find("JSON Schema:")
        if i >= 0:
            try:
                return json.JSONDecoder().raw_decode(c[i + len("JSON Schema:"):].lstrip())[0]
            except ValueError:
                return {}
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _mode(self):
        m = re.match(r"^/m/([a-z]+)/", self.path)
        return (m.group(1) if m and m.group(1) in MODES else STATE["mode"]), (self.path[m.end() - 1:] if m else self.path)

    def _record(self, mode, path, extra=""):
        with _lock:
            STATE["log"] = (STATE["log"] + [{"ts": time.strftime("%H:%M:%S"), "mode": mode, "path": path, "x": extra}])[-50:]

    def _error(self, mode):
        if mode == "nokey":
            self._send(401, {"error": {"message": "Incorrect API key provided (qa fake)", "type": "invalid_request_error",
                                       "code": "invalid_api_key"}})
            return True
        if mode == "nobalance":
            self._send(402, {"error": {"message": "Insufficient balance (qa fake)", "type": "insufficient_quota",
                                       "code": "insufficient_balance"}})
            return True
        return False

    def do_GET(self):
        mode, path = self._mode()
        if path.startswith("/_log"):
            return self._send(200, STATE)
        self._record(mode, path)
        if path.rstrip("/").endswith("/models"):
            if mode == "nokey":
                return self._error(mode)
            return self._send(200, {"object": "list", "data": [{"id": "qa-fake-small", "object": "model"},
                                                                {"id": "qa-fake-large", "object": "model"}]})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            req = {}
        if self.path.startswith("/_mode"):
            if req.get("mode") in MODES:
                STATE["mode"] = req["mode"]
            return self._send(200, {"mode": STATE["mode"]})
        mode, path = self._mode()
        self._record(mode, path, (req.get("model") or ""))
        if not path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": {"message": "not found"}})
        if self._error(mode):
            return
        if mode == "slow":
            time.sleep(90)
        schema = schema_from(req.get("messages"))
        if mode == "junk":
            text = "Sure! I think the answer is the first option."
        elif schema is not None:
            text = json.dumps(minimal(schema) if schema else {}, ensure_ascii=False)
        else:
            text = "OK"
        p = sum(len(str(m.get("content", ""))) for m in req.get("messages") or []) // 4 + 1
        self._send(200, {"id": "qa-fake", "object": "chat.completion", "created": int(time.time()),
                         "model": req.get("model") or "qa-fake-small",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": p, "completion_tokens": len(text) // 4 + 1,
                                   "total_tokens": p + len(text) // 4 + 1}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8860)
    ap.add_argument("--mode", default="ok", choices=MODES)
    a = ap.parse_args()
    STATE["mode"] = a.mode
    print(f"假 API：http://127.0.0.1:{a.port}/v1（全局模式 {a.mode}；固定模式用 /m/<模式>/v1）", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
