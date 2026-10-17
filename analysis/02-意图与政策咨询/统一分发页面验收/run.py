"""统一分发页面验收（DISPATCH-PAGE-001）：隔离 Python→Java→页面。

复用 BC032-LIVE 的环境组织（内存 H2 Java／Vite／Playwright／guard）；
P01/P03/P04/P02-T1 无模型调用：隔离进程按消息回放 DISPATCH-LIVE-001 的真实响应；
P02-T2 真实模型续办（用户已确认的唯一付费轮）。
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
PYTHON = ROOT / ".venv/bin/python"
FRONTEND = ROOT / "ecommerce-backend/frontend"
LIB = ROOT / "analysis/08-候选订单绑定/候选订单验收/.runtime/lib"
JAVA_IMAGE = "maven:3.9-eclipse-temurin-21"
JAVA_CONTAINER = "dispatch-page-java"
JAVA = "http://127.0.0.1:18112"
PY = "http://127.0.0.1:18101"
SITE = "http://127.0.0.1:18103"
RUN_ID = "DISPATCH-PAGE-001"
LIVE_DIR = ROOT / "analysis/02-意图与政策咨询/统一分发真实验收"

for name in (".env.model.local", ".env.local"):
    path = ROOT / name
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
os.environ.setdefault("ANSWER_JUDGE_MODE", "enforce")
os.environ.setdefault("ANSWER_JUDGE_TIMEOUT_SECONDS", "5")

sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(OUT))
from guard import self_check as guard_self_check  # noqa: E402
from settings import answer_judge_settings, openai_base_url, openai_model_name  # noqa: E402
from urllib.parse import urlparse  # noqa: E402


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint():
    paths = [p for p in (ROOT / "backend").rglob("*") if p.is_file()
             and p.suffix in {".py", ".md", ".json", ".yml"} and "__pycache__" not in p.parts]
    paths += sorted((ROOT / "ecommerce-backend/src/main/java").rglob("*.java"))
    paths += [ROOT / "ecommerce-backend/frontend/src/shop/App.tsx", OUT / "cases.json", OUT / "serve.py",
              OUT / "guard.py", OUT / "CheckServer.java", OUT / "run.py", LIVE_DIR / "run.py",
              LIVE_DIR / "frozen.json"]
    return {str(p.relative_to(ROOT)): sha(p) for p in sorted(set(paths))}


def config(cases):
    mode, judge_model, timeout = answer_judge_settings()
    budgets = {case["id"]: 6 for case in cases["cases"]}
    return {
        "model": openai_model_name(), "host": urlparse(openai_base_url()).hostname,
        "judge_mode": mode, "judge_model": judge_model, "judge_timeout_seconds": timeout,
        "automatic_retries": 0, "offline_rag": True, "offline_business_fixtures": True,
        "max_model_http": cases["budget"]["max_model_http"],
        "stop_before_next_request_at_tokens": cases["budget"]["stop_before_next_request_at_tokens"],
        "case_budgets": budgets,
        "replay_source": "DISPATCH-LIVE-001 responses.jsonl（真实响应，2026-10-17）",
        "live_turns": [t["id"] for case in cases["cases"] for t in case["turns"] if t.get("live_model")],
    }


def validate(cases):
    turns = [turn for case in cases["cases"] for turn in case["turns"]]
    for turn in turns:
        assert turn.get("page", {}).get("type") == "HOME", turn["id"]
        assert turn.get("check"), turn["id"]
        assert "confirm" not in turn["message"], "禁止把确认提交写进自然输入"
        assert turn.get("live_model") or turn.get("reply_from"), turn["id"]
    return len(turns)


def load_replies():
    """把 DISPATCH-LIVE-001 的真实响应按消息原文索引。"""
    replies = {}
    for line in (LIVE_DIR / "responses.jsonl").read_text().splitlines():
        row = json.loads(line)
        replies[row["question"]] = row["response"]
    return replies


def call(path, body=None, base=JAVA, client=None, method=None):
    opener = client or urllib.request.build_opener()
    request = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json"},
                                     method=method or ("POST" if body is not None else "GET"))
    try:
        response = opener.open(request, timeout=200)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.read().decode()
    parsed = json.loads(raw) if raw else {}
    return response.status, parsed.get("data", parsed)


def wait_http(url, processes):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("隔离服务提前退出，见 .runtime 日志")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(url)


def port_free(port):
    import socket
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def start_services():
    assert all(port_free(port) for port in (18101, 18112, 18103)), "验收端口已被占用，不碰其他进程"
    runtime = OUT / ".runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir()
    subprocess.run(["docker", "rm", "-f", JAVA_CONTAINER], capture_output=True)
    sources = ["/workspace/" + str(p.relative_to(ROOT))
               for p in sorted((ROOT / "ecommerce-backend/src/main/java").rglob("*.java"))]
    sources.append("/check/CheckServer.java")
    (OUT / "sources.txt").write_text("\n".join(sources) + "\n")
    lib = "/workspace/analysis/08-候选订单绑定/候选订单验收/.runtime/lib/*"
    script = (
        "set -e; mkdir -p /tmp/dispatch-classes; "
        "cp -R /workspace/ecommerce-backend/src/main/resources/. /tmp/dispatch-classes/; "
        f"javac --release 17 -parameters -encoding UTF-8 -cp \"{lib}\" -d /tmp/dispatch-classes @/check/sources.txt; "
        f"exec java -cp \"/tmp/dispatch-classes:{lib}\" "
        "com.teachdemo.ecommerce.customer_service.CheckServer"
    )
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(ROOT / "backend"), "BC032_OUT": str(OUT), "BC032_MODE": "real",
                "AGENT_COURSE_ENV": "/dev/null", "AGENT_COURSE_OFFLINE_RAG": "1",
                "AGENT_COURSE_OFFLINE_FACTS": "1", "ANSWER_JUDGE_MODE": "enforce",
                "ANSWER_JUDGE_TIMEOUT_SECONDS": "5", "ECOMMERCE_BASE_URL": JAVA,
                "AGENT_SERVICE_AUTH_TOKEN": "course-debug-agent-service"})
    vite_config = runtime / "vite.config.mjs"
    vite_config.write_text(
        f"import react from '{(FRONTEND / 'node_modules/@vitejs/plugin-react/dist/index.js').as_uri()}';\n"
        f"export default {{root:{json.dumps(str(FRONTEND))}, plugins:[react()], server:{{host:'127.0.0.1', "
        f"port:18103, strictPort:true, proxy:{{'/api':'http://127.0.0.1:18112'}}}}}};\n")
    commands = [
        ("java", ["docker", "run", "--rm", "--name", JAVA_CONTAINER, "-p", "127.0.0.1:18112:18102",
                  "-v", f"{ROOT}:/workspace:ro", "-v", f"{OUT}:/check", "-w", "/check",
                  JAVA_IMAGE, "bash", "-c", script]),
        ("python", [str(PYTHON), str(OUT / "serve.py")]),
        ("vite", ["node", str(FRONTEND / "node_modules/vite/bin/vite.js"), "--config", str(vite_config)]),
    ]
    processes, logs = [], []
    try:
        for name, command in commands:
            log = (runtime / f"{name}.log").open("w")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
        wait_http(PY + "/health", processes)
        wait_http(JAVA + "/api/check/ready", processes)
        wait_http(SITE, processes)
        deadline = time.monotonic() + 30
        while "BC032_CHECK_READY" not in (runtime / "java.log").read_text():
            assert time.monotonic() < deadline, "Java夹具未就绪"
            time.sleep(0.2)
        return processes, logs
    except Exception:
        stop_services(processes, logs)
        raise


def stop_services(processes, logs):
    subprocess.run(["docker", "rm", "-f", JAVA_CONTAINER], capture_output=True)
    for process in reversed(processes):
        process.terminate()
    for process in reversed(processes):
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for log in logs:
        log.close()


def login(client):
    status, data = call("/api/auth/login", {"username": "zhangsan", "password": "123456"}, client=client)
    assert status == 200, (status, data)


def java_state(client, session):
    status, data = call("/api/check/state?session=" + urllib.request.quote(session), client=client)
    assert status == 200, (status, data)
    return data


def py_memory(session):
    status, data = call("/check/memory/" + urllib.request.quote(session), base=PY)
    assert status == 200
    return data


def prepare():
    assert not (OUT / "frozen.json").exists(), "冻结已存在，不覆盖"
    cases = json.loads((OUT / "cases.json").read_text())
    turns = validate(cases)
    replies = load_replies()
    for case in cases["cases"]:
        for turn in case["turns"]:
            if turn.get("reply_from"):
                assert turn["message"] in replies, f"回放源缺少消息：{turn['message']}"
    assert urlparse(openai_base_url()).hostname == "api.deepseek.com", "模型host不是预期"
    cfg = config(cases)
    frozen = {"run_id": RUN_ID, "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "cases": cases, "config": cfg, "turns": turns,
              "source_sha256": fingerprint(),
              "runner_sha256": sha(OUT / "run.py"), "serve_sha256": sha(OUT / "serve.py"),
              "guard_sha256": sha(OUT / "guard.py"), "java_check_sha256": sha(OUT / "CheckServer.java"),
              "cases_sha256": sha(OUT / "cases.json"),
              "replay_source_sha256": {"responses.jsonl": sha(LIVE_DIR / "responses.jsonl"),
                                       "frozen.json": sha(LIVE_DIR / "frozen.json")},
              "judging": "回放轮模型不外呼（真实响应来自DISPATCH-LIVE-001）；P02-T2为用户确认的真实模型轮。逐轮检查答复完整/状态保留/页面展示/卡片行为。"}
    selfcheck = guard_self_check()
    dump("frozen.json", frozen)
    (OUT / "frozen.sha256").write_text(sha(OUT / "frozen.json") + "\n")
    print(json.dumps({"prepared": True, "run_id": RUN_ID, "turns": turns, "cases": len(cases["cases"]),
                      "guard": selfcheck, "real_http": 0, "config": cfg}, ensure_ascii=False))


def verify_frozen():
    frozen = json.loads((OUT / "frozen.json").read_text())
    assert sha(OUT / "frozen.json") == (OUT / "frozen.sha256").read_text().strip()
    assert frozen["runner_sha256"] == sha(OUT / "run.py") and frozen["serve_sha256"] == sha(OUT / "serve.py")
    assert frozen["guard_sha256"] == sha(OUT / "guard.py") and frozen["java_check_sha256"] == sha(OUT / "CheckServer.java")
    assert frozen["cases_sha256"] == sha(OUT / "cases.json")
    assert frozen["source_sha256"] == fingerprint(), "生产源码已变化，重新冻结"
    assert config(frozen["cases"]) == frozen["config"], "模型配置已变化"
    return frozen


def subset(open_task, expected):
    if open_task is None:
        return False
    return all(open_task.get(key) == value for key, value in expected.items())


def evaluate(turn, memory, before_memory, java_before, java_after, response, page_text, cards):
    checks, failed, blocking = [], [], []
    spec = turn["check"]
    answer = response.get("answer") or ""

    def add(name, ok, detail=None):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        if not ok:
            failed.append(name)

    if "python_open_task" in spec:
        expected = spec["python_open_task"]
        actual = memory.get("open_task")
        if expected == "unchanged":
            ok = actual == before_memory.get("open_task")
        else:
            ok = subset(actual, expected)
        add("Python原待办=" + str(expected), ok, {"actual": actual})

    if spec.get("java_same"):
        keys = ("pending", "handled", "actionType", "order", "reason", "amount", "item", "expiresAt",
                "revision", "tokenSha256", "workflowSha256")
        diff = {key: [java_before.get(key), java_after.get(key)]
                for key in keys if (java_before or {}).get(key) != (java_after or {}).get(key)}
        add("Java售后业务字段不变", not diff, diff)

    card = spec.get("java_card")
    if card == "none":
        ok = not response.get("confirmRequired")
        add("未重发售后确认卡", ok, {"pendingAction": response.get("pendingAction")})
        if not ok:
            blocking.append("read_only_turn_created_confirm_card")
    elif isinstance(card, dict):
        ok = (response.get("confirmRequired") and response.get("relatedOrderNo") == card["order"])
        add("确认卡对象", ok, {"title": response.get("confirmationTitle"),
                               "order": response.get("relatedOrderNo")})
        if not ok:
            blocking.append("wrong_confirm_card")

    terms = spec.get("answer_terms")
    if terms:
        for mode, items in terms.items():
            ok = all(item in answer for item in items) if mode == "all" else any(item in answer for item in items)
            add("答复包含" + str(items), ok, {"answer": answer[:300]})

    absent = spec.get("answer_absent", [])
    if absent:
        hits = [item for item in absent if item in answer]
        add("答复无禁止表述", not hits, {"hits": hits})

    page_spec = spec.get("page_display_terms")
    if page_spec:
        for mode, items in page_spec.items():
            ok = all(item in page_text for item in items) if mode == "all" else any(item in page_text for item in items)
            add("页面展示包含" + str(items), ok, {"page": page_text[:300]})

    page_absent = spec.get("page_display_absent", [])
    if page_absent:
        hits = [item for item in page_absent if item in page_text]
        add("页面无禁止内容", not hits, {"hits": hits})

    if spec.get("page_no_confirm_card"):
        add("页面无确认卡", cards == 0, {"cards": cards})

    return checks, failed, blocking


def main():
    frozen = verify_frozen()
    assert not (OUT / "results.json").exists(), "本次执行已有结果，不覆盖"
    replies = load_replies()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    processes, logs = [], []
    rows = []
    try:
        processes, logs = start_services()
        login(client)
        before_tables = java_state(client, "")["tables"]
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            for case in frozen["cases"]["cases"]:
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                page.goto(SITE, wait_until="networkidle")
                page.fill("input[autocomplete='username']", "zhangsan")
                page.fill("input[autocomplete='current-password']", "123456")
                page.get_by_role("button", name="登录商城", exact=True).click()
                page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
                page.get_by_role("button", name="客服", exact=True).click()
                page.get_by_placeholder("请输入您的问题").wait_for(state="visible")
                session = f"dispatch-page-{case['id'].lower()}"
                java_seed = (case.get("seed") or {}).get("java")
                python_seed = (case.get("seed") or {}).get("python")
                if java_seed:
                    status, data = call("/api/check/seed", {"session": session, **java_seed}, client=client)
                    assert status == 200, (status, data)
                if python_seed is not None:
                    status, data = call("/check/seed/" + urllib.request.quote(session), python_seed, base=PY, method="POST")
                    assert status == 200, (status, data)
                # P02：浏览器会话id由前端生成，T1（回放轮，不写状态）后取实际会话，
                # T2 真实轮之前把 Java/Python 初态补种到实际会话。
                p02_reseed = case["id"] == "P02"
                try:
                    for turn in case["turns"]:
                        before_java = (java_state(client, session).get("session")
                                       if (case.get("seed") or {}).get("java") else None)
                        before_memory = py_memory(session)
                        call("/check/case/" + case["id"], base=PY, method="POST")
                        call("/check/live/" + ("on" if turn.get("live_model") else "off"), base=PY, method="POST")
                        page.get_by_placeholder("请输入您的问题").fill(turn["message"])
                        with page.expect_response(
                                lambda response: "/api/customer-service/chat" in response.url, timeout=200000) as pending:
                            page.get_by_role("button", name="发送", exact=True).click()
                        chat_response = pending.value
                        data = chat_response.json().get("data", {})
                        assert chat_response.status == 200, (turn["id"], chat_response.status)
                        session = data.get("sessionId") or session
                        page.get_by_role("button", name="发送", exact=True).wait_for(state="visible")
                        page.wait_for_timeout(400)
                        cards = page.locator(".confirm-card").count()
                        page_text = page.locator("body").inner_text()
                        if p02_reseed and turn.get("live_model"):
                            session = data.get("sessionId") or session
                            status, data2 = call("/api/check/seed", {"session": session, **java_seed}, client=client)
                            assert status == 200, (status, data2)
                            status, data2 = call("/check/seed/" + urllib.request.quote(session), python_seed, base=PY, method="POST")
                            assert status == 200, (status, data2)
                        after_java = (java_state(client, session).get("session")
                                      if (case.get("seed") or {}).get("java") else None)
                        memory = py_memory(session)
                        if turn.get("reply_from"):
                            expected = replies[turn["message"]]
                            same = (data.get("answer") == expected.get("answer"))
                        else:
                            same = None
                        checks, failed, blocking = evaluate(turn, memory, before_memory, before_java,
                                                            after_java, data, page_text, cards)
                        verdict = "BLOCKED" if blocking else ("FAIL" if failed else "PASS")
                        row = {"case": case["id"], "turn": turn["id"], "session": session,
                               "message": turn["message"], "answer": data.get("answer"),
                               "confirmRequired": data.get("confirmRequired"),
                               "relatedOrderNo": data.get("relatedOrderNo"),
                               "replay_answer_identical": same, "memory": memory,
                               "java": {"before": before_java, "after": after_java},
                               "page": {"cards": cards, "text": page_text[:1500]},
                               "checks": checks, "failed": failed, "blocking": blocking,
                               "verdict": verdict}
                        rows.append(row)
                        with (OUT / "turns.jsonl").open("a") as stream:
                            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                        page.screenshot(path=str(OUT / f"{RUN_ID}-{turn['id']}.png"), full_page=True)
                        print(json.dumps({"turn": turn["id"], "verdict": verdict, "failed": failed,
                                          "blocking": blocking, "replay_same": same}, ensure_ascii=False), flush=True)
                        if blocking:
                            break
                finally:
                    context.close()
                if any(row["blocking"] for row in rows):
                    break
            browser.close()
        after_tables = java_state(client, "")["tables"]
        status = call("/check/status", base=PY)[1]
        result = {"run_id": RUN_ID, "rows": rows,
                  "passed": [r["turn"] for r in rows if r["verdict"] == "PASS"],
                  "failed": [r["turn"] for r in rows if r["verdict"] == "FAIL"],
                  "blocked": [r["turn"] for r in rows if r["verdict"] == "BLOCKED"],
                  "tables_unchanged": before_tables == after_tables,
                  "model_calls": status.get("calls"), "tokens": status.get("tokens"),
                  "stop": status.get("stop")}
        result["verdict"] = ("PASS" if not result["failed"] and not result["blocked"]
                             and not result["stop"] and result["tables_unchanged"] else "FAIL")
        dump("results.json", result)
        print(json.dumps({k: result[k] for k in ("passed", "failed", "blocked", "tables_unchanged",
                                                 "model_calls", "tokens", "stop", "verdict")},
                         ensure_ascii=False), flush=True)
    finally:
        stop_services(processes, logs)
        print(json.dumps({"ports_free_after_stop": all(port_free(p) for p in (18101, 18112, 18103))}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepare()
    elif args.run:
        main()
    else:
        raise SystemExit("use --prepare or --run")
