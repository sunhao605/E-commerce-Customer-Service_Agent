"""隔离商城上的一条链路：商品查询失败后恢复，旧退款确认卡保持不变。"""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
RUNTIME = OUT / ".runtime"
SITE = "http://127.0.0.1:18103"
PYTHON = "http://127.0.0.1:18101"
PROXY = "http://127.0.0.1:18114"
UPSTREAM = "http://127.0.0.1:18102"
FAIL_LEFT = {"n": 0}
LOCK = threading.Lock()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.path == "/__fail_next":
            with LOCK:
                FAIL_LEFT["n"] += 1
            self._send(200, b'{"ok":true}')
            return
        self._forward()

    def do_GET(self):
        with LOCK:
            fail = self.path.startswith("/api/products") and FAIL_LEFT["n"] > 0
            if fail:
                FAIL_LEFT["n"] -= 1
        if fail:
            body = b'{"success":false,"code":"INJECTED","message":"isolated product read failure"}'
            self._send(500, body)
            return
        self._forward()

    def _send(self, status, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        payload = self.rfile.read(length) if length else None
        headers = {key: value for key, value in self.headers.items() if key.lower() not in {"host", "content-length"}}
        request = urllib.request.Request(UPSTREAM + self.path, data=payload, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                self._send(response.status, response.read())
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read())

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


def serve_proxy():
    ThreadingHTTPServer(("127.0.0.1", 18114), ProxyHandler).serve_forever()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def state(sid=""):
    response = httpx.get(SITE + "/api/check/state", params={"session": sid}, timeout=10)
    response.raise_for_status()
    return response.json()


def run_check():
    from playwright.sync_api import expect, sync_playwright

    deadline = time.monotonic() + 30
    while "CANCEL_CHECK_READY" not in (RUNTIME / "java.log").read_text(encoding="utf-8", errors="replace"):
        assert time.monotonic() < deadline, "隔离 fixture 未就绪"
        time.sleep(0.2)
    products = httpx.get(SITE + "/api/products", params={"keyword": "轻量露营折叠椅"}, timeout=10).json()["data"]
    chair = next(item for item in products if item["name"] == "轻量露营折叠椅")
    price = str(int(chair["price"])) if float(chair["price"]).is_integer() else str(chair["price"])
    before = state()["tables"]
    turns = []

    def shot(page, data, text):
        path = OUT / "对话截图" / f"{len(turns) + 1:02d}.png"
        path.parent.mkdir(exist_ok=True)
        page.locator(".service-drawer").screenshot(path=str(path), style="""
            .service-drawer { position: absolute !important; height: auto !important; max-height: none !important;
                overflow: visible !important; grid-template-rows: auto auto auto !important; }
            .messages { overflow: visible !important; height: auto !important; max-height: none !important; }
        """)
        turns.append({"input": text, "answer": data["answer"], "confirmRequired": data["confirmRequired"],
                      "pendingAction": data.get("pendingAction"), "screenshot": str(path.relative_to(OUT))})
        save("page-dialogue.json", turns)
        return data

    def turn(page, text):
        with page.expect_response(lambda item: "/api/customer-service/chat" in item.url) as caught:
            page.get_by_placeholder("请输入您的问题").fill(text)
            page.get_by_role("button", name="发送", exact=True).click()
        assert caught.value.status == 200, caught.value.status
        expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=20000)
        return shot(page, caught.value.json()["data"], text)

    checks = []

    def check(ok, label):
        checks.append({"check": label, "passed": bool(ok)})
        save("page-checks.json", checks)
        print(("PASS " if ok else "FAIL ") + label, flush=True)
        assert ok, label

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            page.goto(SITE + "/login", wait_until="networkidle")
            page.get_by_role("button", name="登录商城", exact=True).click()
            page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
            page.get_by_role("button", name="客服", exact=True).click()
            card = turn(page, "ORD9801 我要申请退款，原因：买错了")
            sid = card["sessionId"]
            check(card["confirmRequired"] and card["relatedOrderNo"] == "ORD9801", "失败前退款确认卡存在，停在提交前")
            draft = state(sid)["session"]
            expect(page.get_by_role("button", name="确认提交申请", exact=True)).to_be_enabled()
            found = turn(page, "轻量露营折叠椅多少钱？")
            check(chair["name"] in found["answer"] and price in found["answer"] and not found["confirmRequired"], "先读到这款商品的实时价格")
            check(state(sid)["session"] == draft, "成功查询不改变退款草稿")
            httpx.post(PROXY + "/__fail_next", timeout=5).raise_for_status()
            failed = turn(page, "刚才那款还有货吗？")
            check("没有成功" in failed["answer"] and "稍后重试" in failed["answer"], "详情读取失败时说明这次查询没有成功")
            check("缺货" not in failed["answer"] and "不存在" not in failed["answer"] and price not in failed["answer"], "失败回复不说缺货、不存在，也不重报旧价格")
            check(not failed["confirmRequired"] and not failed.get("actions") and state(sid)["session"] == draft, "失败回复不重发确认卡，草稿不变")
            expect(page.get_by_role("button", name="确认提交申请", exact=True)).to_be_enabled()
            recovered = turn(page, "刚才那款多少钱？")
            check(chair["name"] in recovered["answer"] and price in recovered["answer"] and "没有成功" not in recovered["answer"], "恢复后仍查原商品并读到当前价格")
            check(state(sid)["session"] == draft and not recovered["confirmRequired"], "恢复后再查询仍不改变退款草稿")
            expect(page.get_by_role("button", name="确认提交申请", exact=True)).to_be_enabled()
        finally:
            context.close()
            browser.close()
    after = state()["tables"]
    save("page-business.json", {"before": before, "after": after})
    check(before == after, "失败和恢复期间8张H2业务表未变")
    save("page-result.json", {"passed": True, "checks": len(checks), "screenshots": len(turns),
                               "online_models": False, "submitted": False, "database": "isolated H2"})
    print(f"页面链路完成 {len(checks)} 项、{len(turns)} 张截图。", flush=True)


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def run_stack():
    for port in (18101, 18102, 18103, 18114):
        if _port_open(port):
            raise SystemExit(f"端口 {port} 已被占用，先停掉占用进程再跑页面链路")
    RUNTIME.mkdir(exist_ok=True)
    jdk = next(Path("/tmp/jdk17").glob("*/Contents/Home/bin/java"))
    lib = ROOT / "analysis/08-候选订单绑定/候选订单验收/.runtime/lib"
    classes = RUNTIME / "classes"
    if classes.exists():
        shutil.rmtree(classes)
    shutil.copytree(ROOT / "ecommerce-backend/src/main/resources", classes)
    sources = sorted((ROOT / "ecommerce-backend/src/main/java").rglob("*.java")) + [
        ROOT / "analysis/03-取消与续办/取消意愿隔离验收/IsolatedCancelCheck.java",
        ROOT / "analysis/03-取消与续办/取消闭环验收/CheckServer.java",
    ]
    (RUNTIME / "sources.txt").write_text("\n".join(map(str, sources)), encoding="utf-8")
    with (RUNTIME / "compile.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(jdk.parent / "javac"), "--release", "17", "-parameters", "-encoding", "UTF-8",
                        "-cp", str(lib / "*"), "-d", str(classes), "@" + str(RUNTIME / "sources.txt")],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "backend"), "AGENT_COURSE_DISABLE_LLM": "1", "AGENT_COURSE_OFFLINE_RAG": "1",
        "AGENT_COURSE_OFFLINE_FACTS": "0", "AGENT_COURSE_ENV": str(RUNTIME / "empty.env"),
        "CANCEL_CHECK_MODE": "scripted", "CANCEL_INTENT_EXECUTE": "0",
        "ECOMMERCE_BASE_URL": PROXY, "AGENT_SERVICE_AUTH_TOKEN": "course-debug-agent-service",
    })
    (RUNTIME / "empty.env").write_text("", encoding="utf-8")
    frontend = ROOT / "ecommerce-backend/frontend"
    config = RUNTIME / "vite.config.mjs"
    config.write_text(
        f"import react from '{(frontend / 'node_modules/@vitejs/plugin-react/dist/index.js').as_uri()}';\n"
        f"export default {{root:{json.dumps(str(frontend))},plugins:[react()],server:{{host:'127.0.0.1',port:18103,strictPort:true,proxy:{{'/api':'http://127.0.0.1:18102'}}}}}};\n",
        encoding="utf-8",
    )
    node = "/Users/sunhao/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
    commands = [
        ("python", 18101, [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "api.routes:app", "--host", "127.0.0.1", "--port", "18101", "--log-level", "warning"]),
        ("java", 18102, [str(jdk), "-cp", f"{classes}:{lib}/*", "com.teachdemo.ecommerce.customer_service.CheckServer"]),
        ("vite", 18103, [node, str(frontend / "node_modules/vite/bin/vite.js"), "--config", str(config)]),
        ("proxy", 18114, [str(ROOT / ".venv/bin/python"), str(OUT / "page_check.py"), "serve-proxy"]),
    ]
    processes = []
    logs = []
    try:
        for name, port, command in commands:
            log = (RUNTIME / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            processes.append(subprocess.Popen(
                command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            ))
            deadline = time.monotonic() + 90
            while not _port_open(port):
                if processes[-1].poll() is not None:
                    raise RuntimeError(f"{name} 退出了，见 {RUNTIME / f'{name}.log'}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{name} 在 90 秒内没有监听 {port}")
                time.sleep(0.2)
        run_check()
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(0.5)
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        for log in logs:
            log.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"serve-proxy": serve_proxy, "check": run_check, "run": run_stack}[mode]()
