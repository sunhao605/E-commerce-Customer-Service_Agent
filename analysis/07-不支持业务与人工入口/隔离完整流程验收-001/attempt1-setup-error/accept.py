"""十条隔离后端完整链路；仅DeepSeek模型真实外连，业务API为本进程只读fixture。"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import httpx

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[2]
USER = "ISOLATED_USER"
PRODUCTS = [{"id": 1, "name": "露营折叠椅", "price": 299.0, "stock": 5, "active": True, "returnable": True},
            {"id": 4, "name": "轻量露营折叠椅", "price": 199.0, "stock": 0, "active": True, "returnable": True}]
ORDER = {"orderNo": "ORD9001", "userId": USER, "fulfillmentStatus": "DELIVERED", "paymentStatus": "PAID",
         "logisticsStatus": "SIGNED", "paidAmount": 199.0, "items": [{"productId": 4, "productName": "轻量露营折叠椅"}]}
REQUESTS = [{"requestId": "FIXTURE-RF-A", "orderNo": "ORD9001", "requestType": "REFUND",
             "status": "PENDING_REVIEW", "createdAt": "2026-10-15T10:00:00"}]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_config():
    for name in (".env.model.local", ".env.local"):
        path = REPO / name
        if path.exists():
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    from settings import api_key_is_missing, openai_base_url, openai_model_name
    result = {"host": urlparse(openai_base_url()).hostname, "model": openai_model_name(),
              "key_present": not api_key_is_missing(os.getenv("AGENT_OPENAI_API_KEY")), "judge_timeout": 3,
              "max_remote_calls": 60, "automatic_retries": 0, "mode": "enforce", "offline_rag": True}
    assert result["host"] == "api.deepseek.com" and result["model"] == "deepseek-flash" and result["key_present"]
    return result


def cases():
    draft = {"intent": "refund_request", "order_id": "ORD8001", "pending_slot": "return_reason"}
    candidates = {"pending_candidates": [{"id": 1, "name": "露营折叠椅"}, {"id": 4, "name": "轻量露营折叠椅"}],
                  "original_question": "折叠椅多少钱？"}
    seed = {"open_task": draft, "product_query": candidates, "promotion_query": {"question": "会员券可以叠加吗？"}}
    mix = "轻量露营折叠椅目前多少钱，还有库存吗，能登记到货提醒吗？"
    promo = "露营季满300减40能和平台会员券叠加吗？另外支持到货提醒吗？"
    return [
        {"id": "ISO01", "kind": "natural", "question": "目前能提供补货后主动提醒的服务吗？",
         "seed": seed, "expected": "正确说明提醒不支持；保留已有退款待办、商品候选和政策待补；不重发申请或清状态。"},
        {"id": "ISO02", "kind": "natural", "question": mix,
         "expected": "完整回答199元、无库存、提醒不支持；只查询不登记，judge实际运行并给出判定。"},
        {"id": "ISO03", "kind": "natural", "question": promo,
         "expected": "回答可叠加与同类券、本人有效期等必要限制；不保证个人已适用；拒绝到货提醒，无盯库存承诺。"},
        {"id": "ISO04", "kind": "natural", "question": "请查订单 ORD9001 之前退款申请的当前状态。另外客服支持未发货退款咨询吗？这次只问，不重新申请。",
         "history": [{"role": "user", "content": "我想了解订单 ORD9001 之前的退款申请。"}],
         "expected": "通过fixture查询历史申请，说明待审核与未退款；介绍退款范围与具体资格边界，不冒认本轮新提交。"},
        {"id": "ISO05", "kind": "natural", "question": "目前能提供到货提醒服务吗？", "fixture_mode": "capability_unknown",
         "expected": "本次能力证据为unknown，说明暂不能确认；不能编成支持或明确不支持，judge检查正确未知表达。"},
        {"id": "ISO06", "kind": "natural", "question": mix, "fixture_mode": "capability_failure",
         "expected": "价格库存成功但提醒查询失败，保留199元和无库存，提醒支持状态暂不能确认；不把失败说成业务不支持。"},
        {"id": "ISO07", "kind": "candidate_injection", "question": mix, "injected_candidate": "这款199元，目前无库存。",
         "expected": "真实生成后在出口注入漏提醒候选；真实judge应拦截，完整备用稿同时保留价格库存与提醒状态。"},
        {"id": "ISO08", "kind": "candidate_injection", "question": promo,
         "injected_candidate": "露营季满300减40可与平台会员券叠加，同类满减不可重复，本人券和商品适用仍需核对。暂不支持到货提醒，但我们会一直盯着库存，到货主动通知您。",
         "expected": "真实生成后注入与能力冲突的未来代办承诺；真实judge拦截，备用稿保留政策与提醒范围，不承诺盯库存。"},
        {"id": "ISO09", "kind": "judge_fault_injection", "question": mix, "fault": "timeout",
         "expected": "真实路由与生成后在judge网络边界注入超过3秒的等待，实际截止取消；使用完整备用稿，不把judge超时说成业务超时。"},
        {"id": "ISO10", "kind": "judge_fault_injection", "question": mix, "fault": "invalid_json_contract",
         "expected": "真实路由与生成后注入judge空JSON；严格解析报错并完整降级，不丢价格和提醒，不声称业务失败。"},
    ]


def self_check(rows):
    assert len(rows) == 10 and len({r["id"] for r in rows}) == 10
    assert sum(r["kind"] == "natural" for r in rows) == 6
    assert sum(r["kind"] == "candidate_injection" for r in rows) == 2
    assert sum(r["kind"] == "judge_fault_injection" for r in rows) == 2
    assert not any("申请退款" in r["question"] or "帮我提交" in r["question"] for r in rows)
    text = json.dumps([PRODUCTS, ORDER, REQUESTS, rows], ensure_ascii=False)
    assert "ORD9001" in text and "ORD8001" in text and not any(x in text for x in ("cookie", "api_key", "resume_token"))


def freeze():
    assert not (OUT / "frozen.json").exists(), "不覆盖冻结材料"
    rows = cases(); self_check(rows)
    config = load_config()
    source = read(OUT / "source-before.json")
    assert all(sha(REPO / p) == h for p,h in source.items())
    document = {"id": "GUARD-ISO-001", "cases": rows, "config": config, "source_sha256": source,
                "runner_sha256": sha(Path(__file__)), "fixture": {"products": PRODUCTS, "order": ORDER, "requests": REQUESTS},
                "scope": "独立子进程FastAPI /chat入口，真实模型路由/工具选择/生成/judge；业务API是只读构造HTTP接口，知识用本地离线检索。无Java/MySQL/UI联调或业务写入。",
                "judgment": "技术断言与助手逐条语义复核分开；不是独立人工认证。自然路径与注入路径分开报告。"}
    dump(OUT / "frozen.json", document)
    (OUT / "frozen.sha256").write_text(sha(OUT / "frozen.json") + "\n")
    dump(OUT / "outbound-safety-check.json", {"destination": "https://api.deepseek.com/chat/completions",
        "authorization": "用户明确同意8—10条隔离完整流程验收，真实模型调用属于该验收链路。",
        "max_remote_calls": 60, "cases": 10, "payload_origin": "本地教学Prompt、户外演示知识、构造商品价格库存和历史申请；无真实客户业务数据。",
        "synthetic_order_ids": ["ORD9001", "ORD8001"], "synthetic_user": USER,
        "business_destination": "仅本进程127.0.0.1随机端口只读fixture；禁止业务POST/PUT/PATCH/DELETE，禁止外连其他主机。",
        "excluded": "不发送真实账号、姓名、手机号、地址、真实订单或业务令牌；模型认证仅在请求头使用，不记录。",
        "daily_services": "不连接日常8000/8081/数据库，不修改.env，不启用日常enforce。"})
    print(json.dumps({"frozen_cases": 10, "natural": 6, "injected": 4, "max_remote_calls": 60}, ensure_ascii=False))


STATE = {"case": None, "phase": "setup", "calls": 0, "remote": [], "business": [], "blocked": [], "stop": None,
         "timings": [], "generated": [], "judge_contexts": [], "cancellation": False}
LOCK = threading.Lock()


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        from agent.service_capabilities import catalog_payload
        parsed = urlparse(self.path)
        path = parsed.path
        code, data = 200, None
        if path.startswith("/api/customer-service/capabilities/"):
            cid = path.rsplit("/",1)[1]
            data = catalog_payload(cid)
            if STATE["case"].get("fixture_mode") == "capability_unknown" and cid == "stock_notification":
                data = {**data, "supportStatus": "unknown", "explanation": "目前还不能确认是否提供到货提醒服务。"}
            elif STATE["case"].get("fixture_mode") == "capability_failure":
                code, data = 503, None
        elif path == "/api/products":
            query = parse_qs(parsed.query).get("keyword", [""])[0]
            data = [p for p in PRODUCTS if ("轻量" in query and p["id"] == 4) or ("轻量" not in query and "椅" in query)]
        elif path.startswith("/api/products/"):
            data = next((p for p in PRODUCTS if str(p["id"]) == path.rsplit("/",1)[1]), None)
            if data is None: code = 404
        elif path == "/api/orders/ORD9001":
            data = ORDER
        elif path == "/api/after-sale/requests" and parse_qs(parsed.query).get("orderNo") == ["ORD9001"]:
            data = REQUESTS
        else:
            code = 404
        payload = {"success": code == 200, "data": copy.deepcopy(data), "code": "NOT_FOUND" if code == 404 else "FIXTURE"}
        STATE["business"].append({"case": STATE["case"]["id"], "method": "GET", "path": self.path,
                                  "status": code, "response": payload})
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)


@contextmanager
def phase(name):
    previous = STATE["phase"]; STATE["phase"] = name
    started = time.monotonic()
    try:
        yield
    finally:
        STATE["timings"].append({"case": STATE["case"]["id"], "phase": name, "elapsed_ms": round((time.monotonic()-started)*1000,3)})
        STATE["phase"] = previous


def run():
    frozen = read(OUT / "frozen.json")
    assert sha(OUT / "frozen.json") == (OUT / "frozen.sha256").read_text().strip()
    assert sha(Path(__file__)) == frozen["runner_sha256"]
    assert all(sha(REPO / p) == value for p,value in frozen["source_sha256"].items())
    assert load_config() == frozen["config"]
    assert not (OUT / "responses.jsonl").exists(), "不覆盖或自动重跑"
    os.environ.update({"AGENT_COURSE_DISABLE_LLM": "0", "AGENT_COURSE_OFFLINE_FACTS": "0", "AGENT_COURSE_OFFLINE_RAG": "1",
        "CANCEL_INTENT_EXECUTE": "0", "CANCEL_INTENT_OBSERVE": "0", "ANSWER_JUDGE_MODE": "enforce",
        "ANSWER_JUDGE_MODEL": "deepseek-flash", "ANSWER_JUDGE_TIMEOUT_SECONDS": "3",
        "AGENT_ECOMMERCE_SERVICE_TOKEN": "isolated-fixture-token", "AGENT_COURSE_ENV": "/tmp/nonexistent-guard-iso-env"})
    server = ThreadingHTTPServer(("127.0.0.1",0), FixtureHandler)
    fixture_port = server.server_address[1]
    os.environ["ECOMMERCE_BASE_URL"] = f"http://127.0.0.1:{fixture_port}"
    os.environ["AGENT_ECOMMERCE_BASE_URL"] = os.environ["ECOMMERCE_BASE_URL"]
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    from fastapi.testclient import TestClient
    from api.routes import app, agent
    from agent.context import SESSION_MEMORIES, current_memory
    from agent.trace import trace_store
    from agent.answer_review import review_audit, redact_review
    from agent.workflows.after_sale_graph import REFUND_APPROVAL_GRAPH, RECEIVED_RETURN_GRAPH
    fixture_hash = digest([PRODUCTS,ORDER,REQUESTS])
    original_sync = httpx.Client.send; original_async = httpx.AsyncClient.send

    def check_request(request):
        host, port, path = request.url.host, request.url.port, request.url.path
        if host == "testserver": return False
        if host == "127.0.0.1" and port == fixture_port and request.method == "GET": return False
        if host != "api.deepseek.com" or request.method != "POST" or not path.endswith("/chat/completions"):
            STATE["blocked"].append({"method": request.method,"host":host,"path":path})
            raise RuntimeError("outbound_not_allowed")
        if STATE["stop"]: raise RuntimeError("provider_stopped")
        body = json.loads(request.content)
        assert body.get("model") == "deepseek-flash" and not body.get("stream"), "unexpected_model_or_stream"
        with LOCK:
            if STATE["calls"] >= 60: raise RuntimeError("remote_call_budget_exceeded")
            STATE["calls"] += 1
        return True

    def record_packet(request, response, started):
        data = response.json() if response.status_code == 200 else {}
        STATE["remote"].append({"case":STATE["case"]["id"],"phase":STATE["phase"], "status":response.status_code,
            "elapsed_ms":round((time.monotonic()-started)*1000,3),"request_sha256":hashlib.sha256(request.content).hexdigest(),
            "response_model":data.get("model"), "usage":data.get("usage"),
            "public_messages":[{"finish_reason":x.get("finish_reason"),"content":x.get("message",{}).get("content"),
                                "tool_calls":x.get("message",{}).get("tool_calls")} for x in data.get("choices",[])]})
        if response.status_code in {401,402,403,429}: STATE["stop"] = f"provider_http_{response.status_code}"

    def sync_send(client, request, **kwargs):
        remote = check_request(request); start = time.monotonic()
        response = original_sync(client, request, **kwargs)
        if remote:
            response.read(); record_packet(request,response,start)
        return response

    async def async_send(client, request, **kwargs):
        remote = check_request(request); start = time.monotonic()
        response = await original_async(client, request, **kwargs)
        if remote:
            await response.aread(); record_packet(request,response,start)
        return response

    real_route = agent.route_model_client.plan_intent
    real_tools = agent.langchain_tool_runner.run
    real_answer = agent.answer_model_client.compose_answer
    real_judge = agent.answer_judge_client.judge

    def route(*args,**kwargs):
        with phase("route"): return real_route(*args,**kwargs)

    def tools(*args,**kwargs):
        with phase("tool_agent"): return real_tools(*args,**kwargs)

    def answer(*args,**kwargs):
        with phase("final_generation"): result = real_answer(*args,**kwargs)
        original = result.answer
        if STATE["case"].get("injected_candidate") and result.used_model:
            result = result.model_copy(update={"answer":STATE["case"]["injected_candidate"]})
        STATE["generated"].append({"case":STATE["case"]["id"],"natural_candidate":original,
            "candidate_used":result.answer,"injected":result.answer != original,"model_used":result.used_model,
            "generation_context":kwargs.get("review_context")})
        return result

    def judge(context,candidate,**kwargs):
        STATE["judge_contexts"].append({"case":STATE["case"]["id"],"context":context,"candidate":candidate})
        with phase("judge"): return real_judge(context,candidate,**kwargs)

    def no_workflow(*args,**kwargs):
        STATE["blocked"].append({"target":"workflow","case":STATE["case"]["id"]})
        raise RuntimeError("workflow_forbidden_in_readonly_acceptance")

    async def slow_judge(body, timeout):
        try:
            await asyncio.sleep(timeout + 1)
        finally:
            STATE["cancellation"] = True

    async def invalid_judge(body, timeout):
        return {"model":"injected-invalid-output","choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}

    outputs = []
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(httpx.Client,"send",sync_send))
            stack.enter_context(patch.object(httpx.AsyncClient,"send",async_send))
            stack.enter_context(patch.object(agent.route_model_client,"plan_intent",route))
            stack.enter_context(patch.object(agent.langchain_tool_runner,"run",tools))
            stack.enter_context(patch.object(agent.answer_model_client,"compose_answer",answer))
            stack.enter_context(patch.object(agent.answer_judge_client,"judge",judge))
            stack.enter_context(patch.object(REFUND_APPROVAL_GRAPH,"invoke",no_workflow))
            stack.enter_context(patch.object(RECEIVED_RETURN_GRAPH,"invoke",no_workflow))
            client = stack.enter_context(TestClient(app, raise_server_exceptions=False))
            with (OUT / "responses.jsonl").open("x") as output:
                for case in frozen["cases"]:
                    STATE["case"] = case; STATE["cancellation"] = False
                    if STATE["stop"]:
                        record = {"id":case["id"],"status":"not_run","reason":STATE["stop"]}
                    else:
                        SESSION_MEMORIES.clear(); trace_store.clear(); review_audit.clear()
                        session_id = "guard-iso-" + case["id"]
                        memory = current_memory(session_id,USER); memory.update(copy.deepcopy(case.get("seed",{})))
                        before = copy.deepcopy(memory); calls_before=STATE["calls"]
                        start = time.monotonic()
                        with ExitStack() as fault:
                            if case.get("fault") == "timeout": fault.enter_context(patch.object(agent.answer_judge_client,"_request",slow_judge))
                            if case.get("fault") == "invalid_json_contract": fault.enter_context(patch.object(agent.answer_judge_client,"_request",invalid_judge))
                            response = client.post("/chat",json={"session_id":session_id,"runtime_user_id":USER,
                                "user_message":case["question"],"history_messages":case.get("history",[])})
                        elapsed = round((time.monotonic()-start)*1000,3)
                        body = response.json()
                        actual_review = body.get("session_state",{}).get("model",{}).get("answer_judge",{})
                        after = copy.deepcopy(SESSION_MEMORIES.get(session_id))
                        state_ok = all(after.get(key) == value for key,value in case.get("seed",{}).items()) if after else False
                        records = [entry for entry in STATE["remote"] if entry["case"]==case["id"]]
                        generated = next((entry for entry in STATE["generated"] if entry["case"]==case["id"]),None)
                        flags = {"http_200":response.status_code==200,"no_workflow":body.get("session_state",{}).get("workflow") is None,
                            "fixture_unchanged":digest([PRODUCTS,ORDER,REQUESTS])==fixture_hash,"seed_state_preserved":state_ok,
                            "no_forbidden_actions":not STATE["blocked"], "actual_route_api":any(x["phase"]=="route" for x in records),
                            "actual_final_api":any(x["phase"]=="final_generation" for x in records),
                            "judge_path_executed":bool(STATE["judge_contexts"] and STATE["judge_contexts"][-1]["case"]==case["id"]),
                            "candidate_injection_applied":bool(generated and generated["injected"]) if case.get("injected_candidate") else True}
                        if case.get("fault"):
                            flags["fault_detected"] = actual_review.get("reason")==("timeout" if case["fault"]=="timeout" else "ValueError")
                            flags["fallback_selected"] = actual_review.get("final_source")=="fallback"
                            if case["fault"]=="timeout": flags["timeout_cancelled"] = STATE["cancellation"]
                        elif case.get("injected_candidate"):
                            flags["candidate_rejected"] = actual_review.get("decision") in {"fallback","review"}
                            flags["fallback_selected"] = actual_review.get("final_source")=="fallback"
                        record = {"id":case["id"],"kind":case["kind"],"question":case["question"],"expected":case["expected"],
                            "status":"completed","http_status":response.status_code,"total_elapsed_ms":elapsed,
                            "remote_calls":STATE["calls"]-calls_before,"checks":flags,"technical_pass":all(flags.values()),
                            "memory_before":before,"memory_after":after,"response":body,
                            "timings":[x for x in STATE["timings"] if x["case"]==case["id"]],
                            "business_calls":[x for x in STATE["business"] if x["case"]==case["id"]],
                            "generated":generated,"private_review":list(review_audit)}
                    output.write(json.dumps(redact_review(record),ensure_ascii=False)+"\n"); output.flush()
                    outputs.append(record)
                    print(json.dumps({"id":case["id"],"status":record["status"],"technical_pass":record.get("technical_pass"),
                        "elapsed_ms":record.get("total_elapsed_ms"),"calls":record.get("remote_calls"),
                        "review":record.get("response",{}).get("session_state",{}).get("model",{}).get("answer_judge",{})},ensure_ascii=False),flush=True)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        dump(OUT / "network-observations.json",redact_review({"remote_calls":STATE["calls"],"packets":STATE["remote"],
            "business":STATE["business"],"blocked":STATE["blocked"],"stop":STATE["stop"]}))
        dump(OUT / "verification.json",{"source_files_unchanged":all(sha(REPO / p)==h for p,h in frozen["source_sha256"].items()),
            "runner_unchanged":sha(Path(__file__))==frozen["runner_sha256"],"frozen_unchanged":sha(OUT / "frozen.json")== (OUT / "frozen.sha256").read_text().strip(),
            "fixture_unchanged":digest([PRODUCTS,ORDER,REQUESTS])==fixture_hash,"fixture_server_stopped":not thread.is_alive(),
            "daily_services_touched":False,"remote_calls":STATE["calls"],"blocked":STATE["blocked"],"stop":STATE["stop"]})
    print(json.dumps({"cases":len(outputs),"technical_pass":sum(r.get("technical_pass",False) for r in outputs),"remote_calls":STATE["calls"]},ensure_ascii=False))


if __name__ == "__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("action",choices=["self-check","freeze","run"])
    action=parser.parse_args().action
    if action=="self-check": self_check(cases()); print("self-check passed; no API or server")
    elif action=="freeze": freeze()
    else: run()
