"""BC032-LIVE：真实模型与隔离 Python→Java→页面验收。先冻结，再自检，确认预算后才可付费运行。"""
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
from unittest.mock import patch

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
PYTHON = ROOT / ".venv/bin/python"
FRONTEND = ROOT / "ecommerce-backend/frontend"
LIB = ROOT / "analysis/08-候选订单绑定/候选订单验收/.runtime/lib"
JAVA_IMAGE = "maven:3.9-eclipse-temurin-21"
JAVA_CONTAINER = "bc032-live-java"
JAVA = "http://127.0.0.1:18112"
PY = "http://127.0.0.1:18101"
SITE = "http://127.0.0.1:18103"
RUN_ID_DEFAULT = "BC032-LIVE-001"

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
              OUT / "guard.py", OUT / "CheckServer.java", OUT / "run.py"]
    return {str(p.relative_to(ROOT)): sha(p) for p in sorted(set(paths))}


def knowledge_and_prompts():
    knowledge = {str(p.relative_to(ROOT)): sha(p) for p in sorted((ROOT / "backend/resources/knowledge").rglob("*"))
                 if p.is_file()}
    prompts = {str(p.relative_to(ROOT)): sha(p) for p in sorted((ROOT / "backend/resources/prompts").rglob("*"))
               if p.is_file()}
    return knowledge, prompts


def config(cases):
    mode, judge_model, timeout = answer_judge_settings()
    budgets = {case["id"]: len(case["turns"]) * 4 + 2 for case in cases["cases"]}
    return {
        "model": openai_model_name(), "host": urlparse(openai_base_url()).hostname,
        "judge_mode": mode, "judge_model": judge_model, "judge_timeout_seconds": timeout,
        "route_temperature": 0, "route_max_tokens": 512,
        "generation_temperature": 0.2, "generation_max_tokens": 1024,
        "judge_temperature": 0, "judge_max_tokens": 1536,
        "automatic_retries": 0, "offline_rag": True, "offline_business_fixtures": True,
        "max_model_http": cases["budget"]["max_model_http"],
        "stop_before_next_request_at_tokens": cases["budget"]["stop_before_next_request_at_tokens"],
        "case_budgets": budgets,
    }


def validate(cases):
    turns = [turn for case in cases["cases"] for turn in case["turns"]]
    assert len(turns) <= cases["budget"]["max_natural_turns"], "轮数超预算"
    assert len({turn["id"] for turn in turns}) == len(turns), "轮次ID重复"
    for turn in turns:
        assert turn.get("page", {}).get("type") in {"HOME", "ORDER_DETAIL"}, turn["id"]
        assert turn.get("inject") in {None, "route", "tool", "generation"}
        assert turn.get("check"), turn["id"]
        assert "confirm" not in turn["message"], "禁止把确认提交写进自然输入"
    for case in cases["cases"]:
        assert case["driver"] in {"api", "browser"}
        assert "forbidden" in case and "allowed" in case
    return len(turns)


def latest_python_turn(session, message, mode="real"):
    path = OUT / f"python-turns-{mode}.jsonl"
    if not path.exists():
        return {}
    lines = path.read_text().strip().splitlines()
    if not lines:
        return {}
    row = json.loads(lines[-1])
    request = row.get("request") or {}
    if request.get("session_id") != session or not str(request.get("user_message", "")).startswith(message):
        return {}
    return row


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


def wait_http(url, processes, expected=200):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("隔离服务提前退出，见 .runtime 日志")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == expected:
                    return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(url)


def port_free(port):
    import socket
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def start_services(mode):
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
        "set -e; mkdir -p /tmp/bc032-classes; "
        "cp -R /workspace/ecommerce-backend/src/main/resources/. /tmp/bc032-classes/; "
        f"javac --release 17 -parameters -encoding UTF-8 -cp \"{lib}\" -d /tmp/bc032-classes @/check/sources.txt; "
        f"exec java -cp \"/tmp/bc032-classes:{lib}\" "
        "com.teachdemo.ecommerce.customer_service.CheckServer"
    )
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(ROOT / "backend"), "BC032_OUT": str(OUT), "BC032_MODE": mode,
                "AGENT_COURSE_ENV": "/dev/null", "AGENT_COURSE_OFFLINE_RAG": "1",
                "AGENT_COURSE_OFFLINE_FACTS": "1", "ANSWER_JUDGE_MODE": "enforce",
                "ANSWER_JUDGE_TIMEOUT_SECONDS": "5", "ECOMMERCE_BASE_URL": JAVA,
                "AGENT_SERVICE_AUTH_TOKEN": "course-debug-agent-service"})
    if mode == "offline":
        env["AGENT_COURSE_DISABLE_LLM"] = "1"
    frontend = FRONTEND
    vite_config = runtime / "vite.config.mjs"
    vite_config.write_text(
        f"import react from '{(frontend / 'node_modules/@vitejs/plugin-react/dist/index.js').as_uri()}';\n"
        f"export default {{root:{json.dumps(str(frontend))}, plugins:[react()], server:{{host:'127.0.0.1', "
        f"port:18103, strictPort:true, proxy:{{'/api':'http://127.0.0.1:18112'}}}}}};\n")
    commands = [
        ("java", ["docker", "run", "--rm", "--name", JAVA_CONTAINER, "-p", "127.0.0.1:18112:18102",
                  "-v", f"{ROOT}:/workspace:ro", "-v", f"{OUT}:/check", "-w", "/check",
                  JAVA_IMAGE, "bash", "-c", script]),
        ("python", [str(PYTHON), str(OUT / "serve.py")]),
        ("vite", ["node", str(frontend / "node_modules/vite/bin/vite.js"), "--config", str(vite_config)]),
    ]
    processes, logs = [], []
    try:
        for name, command in commands:
            log = (runtime / f"{name}-{mode}.log").open("w")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
        wait_http(PY + "/health", processes)
        wait_http(JAVA + "/api/check/ready", processes)
        wait_http(SITE, processes)
        deadline = time.monotonic() + 30
        while "BC032_CHECK_READY" not in (runtime / f"java-{mode}.log").read_text():
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


def seed(run_id, session, case):
    java_seed = case.get("seed", {}).get("java")
    python_seed = case.get("seed", {}).get("python")
    if java_seed:
        status, data = call("/api/check/seed", {"session": session, **java_seed}, client=CLIENT)
        assert status == 200, (status, data)
    if python_seed is not None:
        status, data = call("/check/seed/" + urllib.request.quote(session), python_seed, base=PY)
        assert status == 200, (status, data)


def redact_response(response):
    if isinstance(response, dict) and response.get("resumeToken"):
        return {**response, "resumeTokenSha256": hashlib.sha256(str(response["resumeToken"]).encode()).hexdigest(),
                "resumeToken": None}
    return response


def check_business_same(before, after):
    keys = ("pending", "handled", "actionType", "order", "reason", "amount", "item", "expiresAt", "revision",
            "tokenSha256", "workflowSha256")
    diff = {key: [before.get(key), after.get(key)] for key in keys if before.get(key) != after.get(key)}
    return not diff, diff


def subset(open_task, expected):
    if open_task is None:
        return False
    if "intent" in expected and open_task.get("intent") != expected["intent"]:
        return False
    if "pending_slot" in expected and open_task.get("pending_slot") != expected["pending_slot"]:
        return False
    if "order_id" in expected and open_task.get("order_id") != expected["order_id"]:
        return False
    if "candidates" in expected:
        values = [item.get("value") for item in open_task.get("candidates") or []]
        if values != expected["candidates"]:
            return False
    return True


def evaluate(turn, python_turn, memory, before_memory, java_before, java_after, response, page):
    checks, failed, blocking = [], [], []
    spec = turn["check"]
    state = (python_turn or {}).get("response", {}).get("session_state", {})
    # 顾客看到的是 Java 网关答复；Python 答复可能是跳过最终生成时的内部占位。
    answer = response.get("answer") or (python_turn or {}).get("response", {}).get("answer") or ""

    def add(name, ok, detail=None):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        if not ok:
            failed.append(name)

    if "python_open_task" in spec:
        expected = spec["python_open_task"]
        actual = memory.get("open_task")
        if expected == "unchanged":
            ok = actual == before_memory.get("open_task")
        elif expected is None:
            ok = actual is None
        else:
            ok = subset(actual, expected)
        add("Python原待办=" + str(expected), ok, {"actual": actual})

    if "python_focus" in spec:
        expected = spec["python_focus"]
        actual = memory.get("clarification_focus")
        ok = actual == before_memory.get("clarification_focus") if expected == "unchanged" else actual == expected
        add("Python澄清焦点", ok, {"expected": expected, "actual": actual})

    if "python_product" in spec:
        expected = spec["python_product"]
        product = memory.get("product_query") or {}
        pending = product.get("pending_candidates") or []
        if expected == "unchanged":
            ok = product == (before_memory.get("product_query") or {})
            add("Python商品候选保留", ok, {"actual": len(pending)})
        else:
            ok = len(pending) == expected.get("candidates", len(pending))
            names = [item.get("name") for item in pending]
            ok = ok and all(name in names for name in expected.get("names", []))
            if "selected_name" in expected:
                ok = ok and product.get("product_name") == expected["selected_name"] and not pending
            add("Python商品咨询状态", ok, {"names": names, "product": product.get("product_name")})

    if "python_promotion" in spec:
        present = bool(memory.get("promotion_query"))
        ok = present if spec["python_promotion"] == "present" else not present
        add("Python优惠待补条件", ok, {"present": present, "value": memory.get("promotion_query")})

    if spec.get("java_same"):
        ok, diff = check_business_same(java_before.get("session") or {}, java_after.get("session") or {})
        add("Java售后业务字段不变", ok, diff)

    card = spec.get("java_card")
    if card == "none":
        ok = not response.get("confirmRequired")
        add("未重发售后确认卡", ok, {"pendingAction": response.get("pendingAction")})
        if not ok:
            blocking.append("read_only_turn_created_confirm_card")
    elif isinstance(card, dict):
        ok = (response.get("confirmRequired") and response.get("relatedOrderNo") == card["order"]
              and ("退货" in (response.get("confirmationTitle") or "")) == (card["type"] == "RETURN"))
        add("确认卡对象与类型", ok, {"title": response.get("confirmationTitle"),
                                      "order": response.get("relatedOrderNo")})
        if not ok:
            blocking.append("wrong_confirm_card")

    if spec.get("java_cancelled"):
        session = java_after.get("session") or {}
        ok = session.get("pending") == "CANCELLED"
        add("Java原明确取消已记录", ok, {"pending": session.get("pending")})
    else:
        session = java_after.get("session") or {}
        if session.get("pending") == "CANCELLED" and turn["id"] != "G05-T3":
            blocking.append("unexpected_cancellation")

    if "intent" in spec:
        actual = state.get("intent")
        add("Python意图", actual in spec["intent"], {"expected": spec["intent"], "actual": actual})

    if "chosen" in spec:
        actual = (state.get("context_report") or {}).get("chosen_order_id")
        ok = actual == spec["chosen"]
        add("绑定订单", ok, {"expected": spec["chosen"], "actual": actual})
        if not ok and spec["chosen"] is not None:
            blocking.append("wrong_order_binding")

    terms = spec.get("answer_terms")
    if terms:
        for mode, items in terms.items():
            ok = all(item in answer for item in items) if mode == "all" else any(item in answer for item in items)
            add("答复包含" + str(items), ok, {"answer": answer[:300]})

    absent = spec.get("answer_absent", [])
    if absent:
        hits = [item for item in absent if item in answer]
        add("答复无禁止表述", not hits, {"hits": hits})

    if "page" in spec:
        page_spec = spec["page"]
        if page_spec.get("no_confirm_card"):
            ok = page.get("confirm_cards", 0) == 0
            add("页面未重发确认卡", ok, {"cards": page.get("confirm_cards")})
        if page_spec.get("candidates"):
            text = page.get("answer") or ""
            positions = [text.find(item) for item in page_spec["candidates"]]
            ok = all(position >= 0 for position in positions) and positions == sorted(positions)
            add("页面候选顺序", ok, {"candidates": page_spec["candidates"], "answer": text[:300]})

    recorded = {key: state.get(key) for key in spec.get("record", [])}
    if "injection" in spec.get("record", []):
        recorded["injection"] = turn.get("inject")
    return checks, failed, blocking, recorded


def finish_row(run, case, turn, session, message, response, python_turn, memory, before_memory,
               java_before, java_after, page, injection_missing=False):
    checks, failed, blocking, recorded = evaluate(turn, python_turn, memory, before_memory,
                                                  java_before, java_after, response, page)
    if injection_missing:
        failed.append("injection_not_triggered")
        checks.append({"check": "故障注入实际触发", "passed": False, "detail": turn.get("inject")})
    row = {"run": run, "case": case["id"], "turn": turn["id"], "session": session, "message": message,
           "request_page": turn.get("page"), "response": redact_response(response),
           "python": {"intent": (python_turn or {}).get("response", {}).get("session_state", {}).get("intent"),
                      "task_relation": (python_turn or {}).get("response", {}).get("session_state", {}).get("task_relation"),
                      "chosen_order_id": ((python_turn or {}).get("response", {}).get("session_state", {})
                                          .get("context_report") or {}).get("chosen_order_id"),
                      "answer": (python_turn or {}).get("response", {}).get("answer")},
           "memory": memory, "before_memory": before_memory,
           "java": {"before": java_before.get("session"), "after": java_after.get("session")},
           "page": page, "checks": checks, "failed": failed, "blocking": blocking, "recorded": recorded,
           "verdict": "NOT_RUN" if injection_missing else ("BLOCKED" if blocking else ("FAIL" if failed else "PASS"))}
    with (OUT / f"turns-{run}.jsonl").open("a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"turn": turn["id"], "verdict": row["verdict"], "failed": failed,
                      "blocking": blocking, "intent": row["python"]["intent"],
                      "answer": (row["python"]["answer"] or "")[:80]}, ensure_ascii=False), flush=True)
    return row


def run_api(run, client, case, turn, session):
    before_java = java_state(client, session)
    before_memory = py_memory(session)
    call("/check/case/" + case["id"], base=PY, method="POST")
    call("/check/inject/" + (turn.get("inject") or "none"), base=PY, method="POST")
    status, response = call("/api/customer-service/chat",
                            {"sessionId": session, "message": turn["message"], "pageContext": turn.get("page")},
                            client=client)
    python_turn = latest_python_turn(session, turn["message"])
    if status != 200:
        response = {"answer": "", "confirmRequired": False, "httpStatus": status}
    after_java = java_state(client, session)
    memory = py_memory(session)
    page = {"driver": "api"}
    status_py = call("/check/status", base=PY)[1]
    injection_missing = bool(turn.get("inject") and status_py.get("inject_pending"))
    if injection_missing:
        call("/check/inject/none", base=PY, method="POST")
    return finish_row(run, case, turn, session, turn["message"], response, python_turn, memory,
                      before_memory, before_java, after_java, page, injection_missing)


def open_chat(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page.goto(SITE, wait_until="networkidle")
    page.fill("input[autocomplete='username']", "zhangsan")
    page.fill("input[autocomplete='current-password']", "123456")
    page.get_by_role("button", name="登录商城", exact=True).click()
    page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
    page.get_by_role("button", name="客服", exact=True).click()
    page.get_by_placeholder("请输入您的问题").wait_for(state="visible")
    return context, page


def browser_send(page, message):
    page.get_by_placeholder("请输入您的问题").fill(message)
    with page.expect_response(lambda response: "/api/customer-service/chat" in response.url, timeout=200000) as pending:
        page.get_by_role("button", name="发送", exact=True).click()
    response = pending.value
    data = response.json().get("data", {})
    page.get_by_role("button", name="发送", exact=True).wait_for(state="visible")
    page.wait_for_timeout(400)
    return response.status, data, page.locator(".confirm-card").count()


def run_browser_case(run, playwright, case):
    browser = playwright.chromium.launch(headless=True,
                                         executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    context, page = open_chat(browser)
    rows, session, before_memory = [], None, {}
    try:
        for index, turn in enumerate(case["turns"]):
            before_java = java_state(CLIENT, session) if session else {"session": None}
            before_memory = py_memory(session) if session else {}
            call("/check/case/" + case["id"], base=PY, method="POST")
            status, response, cards = browser_send(page, turn["message"])
            assert status == 200, (turn["id"], status, response)
            session = response.get("sessionId") or session
            python_turn = latest_python_turn(session, turn["message"])
            after_java = java_state(CLIENT, session)
            memory = py_memory(session)
            if index in (1,):
                page.screenshot(path=str(OUT / f"{run}-{turn['id']}.png"), full_page=True)
            rows.append(finish_row(run, case, turn, session, turn["message"], response, python_turn, memory,
                                   before_memory, before_java, after_java,
                                   {"driver": "browser", "confirm_cards": cards,
                                    "answer": response.get("answer", "")}))
    finally:
        context.close()
        browser.close()
    return rows


def prepare(run_id):
    assert not (OUT / "frozen.json").exists(), "冻结已存在，不覆盖；换新执行编号并另建目录"
    cases = json.loads((OUT / "cases.json").read_text())
    turns = validate(cases)
    assert urlparse(openai_base_url()).hostname == "api.deepseek.com", "模型host不是预期"
    cfg = config(cases)
    assert cfg["judge_mode"] == "enforce", "judge模式未固定为enforce"
    knowledge, prompts = knowledge_and_prompts()
    frozen = {"run_id": run_id, "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "cases": cases, "config": cfg, "turns": turns,
              "source_sha256": fingerprint(), "knowledge_sha256": knowledge, "prompt_sha256": prompts,
              "runner_sha256": sha(OUT / "run.py"), "serve_sha256": sha(OUT / "serve.py"),
              "guard_sha256": sha(OUT / "guard.py"), "java_check_sha256": sha(OUT / "CheckServer.java"),
              "cases_sha256": sha(OUT / "cases.json"),
              "judging": "运行前由助手按cases.json冻结；模型不看expected或标签；逐轮检查状态保留/焦点/权限/答复，judge正文pass不替代状态判定。"}
    selfcheck = guard_self_check()
    dump("frozen.json", frozen)
    (OUT / "frozen.sha256").write_text(sha(OUT / "frozen.json") + "\n")
    dump("budget-request.json", {"run_id": run_id, "max_model_http": cfg["max_model_http"],
                                 "stop_before_next_request_at_tokens": cfg["stop_before_next_request_at_tokens"],
                                 "natural_turns": turns, "frozen_sha256": sha(OUT / "frozen.json"),
                                 "status": "未确认；用户在本轮聊天明确确认前不得发付费请求"})
    print(json.dumps({"prepared": True, "run_id": run_id, "turns": turns, "cases": len(cases["cases"]),
                      "guard": selfcheck, "real_http": 0, "config": cfg}, ensure_ascii=False))


def verify_frozen():
    frozen = json.loads((OUT / "frozen.json").read_text())
    assert sha(OUT / "frozen.json") == (OUT / "frozen.sha256").read_text().strip()
    assert frozen["runner_sha256"] == sha(OUT / "run.py") and frozen["serve_sha256"] == sha(OUT / "serve.py")
    assert frozen["guard_sha256"] == sha(OUT / "guard.py") and frozen["java_check_sha256"] == sha(OUT / "CheckServer.java")
    assert frozen["cases_sha256"] == sha(OUT / "cases.json")
    assert frozen["source_sha256"] == fingerprint(), "生产源码已变化，重新冻结"
    knowledge, prompts = knowledge_and_prompts()
    assert frozen["knowledge_sha256"] == knowledge and frozen["prompt_sha256"] == prompts, "知识或Prompt已变化"
    assert config(frozen["cases"]) == frozen["config"], "模型配置已变化"
    return frozen


def offline_smoke():
    frozen = verify_frozen()
    assert not (OUT / "selfcheck.json").exists(), "自检结果已存在，不覆盖"
    processes, logs = [], []
    try:
        processes, logs = start_services("offline")
        login(CLIENT)
        before = java_state(CLIENT, "")["tables"]
        session = "bc032-selfcheck"
        case = frozen["cases"]["cases"][0]
        seed(frozen["run_id"], session, case)
        revision = java_state(CLIENT, session)["session"]["revision"]
        call("/check/case/" + case["id"], base=PY, method="POST")
        call("/check/inject/route", base=PY, method="POST")
        inject_set = call("/check/status", base=PY)[1]["inject_pending"]
        call("/check/inject/none", base=PY, method="POST")
        status, response = call("/api/customer-service/chat",
                                {"sessionId": session, "message": "谢谢，我再想想。", "pageContext": {"type": "HOME"}},
                                client=CLIENT)
        memory = py_memory(session)
        python_turn = latest_python_turn(session, "谢谢，我再想想。", mode="offline")
        after = java_state(CLIENT, "")["tables"]
        model_lines = (OUT / "models-offline.jsonl").read_text().splitlines() if (OUT / "models-offline.jsonl").exists() else []
        result = {"mode": "offline", "model_calls": len(model_lines), "http_status": status,
                  "seed_revision": revision, "pending": java_state(CLIENT, session)["session"]["pending"],
                  "open_task_present": memory.get("open_task") is not None,
                  "python_turn_captured": bool(python_turn),
                  "inject_set": inject_set,
                  "tables_unchanged": before == after,
                  "no_after_sale_row_written": before == after,
                  "browser": None,
                  "ports_free_after_stop": None}
        assert status == 200 and result["pending"] == "REFUND_ORDER_REQUIRED" and result["tables_unchanged"]
        assert inject_set == "route" and python_turn
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            context, page = open_chat(browser)
            try:
                browser_status, browser_data, cards = browser_send(page, "你好")
            finally:
                context.close()
                browser.close()
        result["browser"] = {"status": browser_status, "answer": browser_data.get("answer", "")[:80],
                             "confirm_cards": cards}
        assert browser_status == 200
    finally:
        stop_services(processes, logs)
    result["ports_free_after_stop"] = all(port_free(port) for port in (18101, 18112, 18103))
    dump("selfcheck.json", result)
    print(json.dumps(result, ensure_ascii=False))


def run(run_id):
    frozen = verify_frozen()
    assert frozen["run_id"] == run_id
    budget = json.loads((OUT / "budget.json").read_text())
    assert budget["run_id"] == run_id and budget.get("confirmed_by")
    assert budget["max_model_http"] == frozen["config"]["max_model_http"]
    assert budget["stop_before_next_request_at_tokens"] == frozen["config"]["stop_before_next_request_at_tokens"]
    assert not (OUT / f"results-{run_id}.json").exists(), "本次执行已有结果，不覆盖"
    for path in (OUT / f"turns-{run_id}.jsonl", OUT / "models-real.jsonl", OUT / "python-turns-real.jsonl"):
        path.unlink(missing_ok=True)
    processes, logs = [], []
    rows, stop_reason = [], None
    try:
        processes, logs = start_services("real")
        login(CLIENT)
        before_tables = java_state(CLIENT, "")["tables"]
        dump(f"business-before-{run_id}.json", before_tables)
        sessions = {}
        playwright = None
        with ExitStack() as stack:
            if any(case["driver"] == "browser" for case in frozen["cases"]["cases"]):
                from playwright.sync_api import sync_playwright
                playwright = stack.enter_context(sync_playwright())
            for case in frozen["cases"]["cases"]:
                if case["driver"] == "browser":
                    case_rows = run_browser_case(run_id, playwright, case)
                    rows.extend(case_rows)
                    if any(row["blocking"] for row in case_rows):
                        stop_reason = "blocking:" + case["id"]
                        break
                    continue
                base_session = f"bc032-{case['id'].lower()}-{run_id[-3:]}"
                session = base_session
                for turn in case["turns"]:
                    if turn.get("reset"):
                        session = base_session + "-" + turn["id"].lower()
                        seed(run_id, session, case)
                        sessions[session] = True
                    elif session not in sessions:
                        seed(run_id, session, case)
                        sessions[session] = True
                    row = run_api(run_id, CLIENT, case, turn, session)
                    rows.append(row)
                    status = call("/check/status", base=PY)[1]
                    if status.get("stop"):
                        stop_reason = "guard:" + str(status["stop"])
                        break
                    if row["blocking"]:
                        stop_reason = "blocking:" + turn["id"]
                        break
                if stop_reason:
                    break
        after_tables = java_state(CLIENT, "")["tables"]
        dump(f"business-after-{run_id}.json", after_tables)
        result = {"run_id": run_id, "rows": rows, "stop_reason": stop_reason,
                  "tables_unchanged": before_tables == after_tables,
                  "model_calls": len((OUT / "models-real.jsonl").read_text().splitlines())
                  if (OUT / "models-real.jsonl").exists() else 0,
                  "tokens": sum(json.loads(line).get("usage", {}).get("total_tokens", 0)
                                for line in (OUT / "models-real.jsonl").read_text().splitlines())
                  if (OUT / "models-real.jsonl").exists() else 0}
        blocked = [row["turn"] for row in rows if row["verdict"] == "BLOCKED"]
        failed = [row["turn"] for row in rows if row["verdict"] == "FAIL"]
        result.update(blocked=blocked, failed=failed,
                      passed=[row["turn"] for row in rows if row["verdict"] == "PASS"])
        dump(f"results-{run_id}.json", result)
        dump(f"summary-{run_id}.json", {
            "run_id": run_id, "frozen": {key: frozen[key] for key in ("frozen_at", "turns", "config",
                                                                     "source_sha256", "knowledge_sha256",
                                                                     "prompt_sha256")},
            "rows": len(rows), "passed": len(result["passed"]), "failed": len(failed), "blocked": len(blocked),
            "stop_reason": stop_reason, "tables_unchanged": before_tables == after_tables,
            "model_calls": result["model_calls"], "tokens": result["tokens"],
            "verdict": "PASS" if not blocked and not failed and not stop_reason and before_tables == after_tables
                       else "FAIL"})
        print(json.dumps({key: result[key] for key in ("blocked", "failed", "passed", "stop_reason",
                                                       "tables_unchanged", "model_calls", "tokens")},
                         ensure_ascii=False))
    finally:
        stop_services(processes, logs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    args = parser.parse_args()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    CLIENT = client
    if args.prepare:
        prepare(args.run_id)
    elif args.self_check:
        offline_smoke()
    elif args.run:
        run(args.run_id)
    else:
        raise SystemExit("use --prepare, --self-check or --run")
