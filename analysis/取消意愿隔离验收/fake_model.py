"""隔离验收用的固定模型。按原句返回判断，不访问外网。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JEV = {
    "我想了想，还是不退了": ("stop", "current", "no", 0.99, 0.99, 0.99),
    "算了，不退了。": ("stop", "current", "no", 0.99, 0.99, 0.99),
    "这次退款我还要办，不能取消。": ("continue", "ambiguous", "no", 0.99, 0.2, 0.99),
    "要是退货运费得自己承担，那就别退了。": ("stop", "current", "no", 0.99, 0.99, 0.99),
    "取消订单吧": ("stop", "ambiguous", "no", 0.99, 0.4, 0.99),
    "我有点想取消这单退款": ("stop", "current", "no", 0.5, 0.99, 0.99),
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/systemone"):
            message = payload.get("state", {}).get("latest_user_message", "")
            decision, target, extra, ds, ts, es = JEV.get(message, ("continue", "ambiguous", "no", 0.99, 0.2, 0.99))
            body = {"model": "jev-1.13.0", "answers": {
                key: {"choice": choice, "confidence": score,
                      "probabilities": {choice: score}}
                for key, choice, score in (
                    ("decision", decision, ds), ("target", target, ts), ("extra_task", extra, es))
            }, "usage": {"input_tokens": 1, "output_tokens": 1}}
        else:
            text = json.dumps({"decision": "continue", "target": "current", "extra_task": "no", "reason": "none"},
                              ensure_ascii=False)
            body = {"choices": [{"finish_reason": "stop", "message": {"content": text}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18015), Handler).serve_forever()
