"""六条冻结只读咨询：真实商城网关、模型和业务读取；不调用resume。"""
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

OUT = Path(__file__).resolve().parent
cases = json.loads((OUT / "frozen-cases.json").read_text())["cases"]
expected = json.loads((OUT / "preflight.json").read_text())
results = []
with httpx.Client(base_url="http://127.0.0.1:8081", timeout=120, trust_env=False) as mall, \
     httpx.Client(base_url="http://127.0.0.1:8000", timeout=10, trust_env=False) as agent:
    # 已有本地演示账户；不记录登录请求、Cookie或令牌。
    login = mall.post("/api/auth/login", json={"username": "zhangsan", "password": "123456"})
    login.raise_for_status()
    assert login.json()["success"] is True
    catalog = mall.get("/api/products")
    catalog.raise_for_status()
    products = catalog.json()["data"]
    (OUT / "business-products-before.json").write_text(json.dumps(products, ensure_ascii=False, indent=2))
    for case in cases:
        session = "live-meta-" + uuid4().hex
        request = {"message": case["message"], "sessionId": session}
        started = time.perf_counter()
        response = mall.post("/api/customer-service/chat", json=request)
        elapsed = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        trace_response = agent.get(f"/sessions/{session}/trace")
        trace_response.raise_for_status()
        events = trace_response.json()
        route = next((event["payload"] for event in events if event["event_type"] == "model_route_planned"), {})
        plans = [event["payload"] for event in events if event["event_type"] == "route_plan_built"]
        citations = [citation for event in events if event["event_type"] == "evidence_domain_completed"
                     for citation in event["payload"].get("citations", [])]
        policies = sorted({citation.get("metadata", {}).get("policy_id") for citation in citations})
        admissions = [event["payload"] for event in events if event["event_type"] == "rag_policy_admission"]
        tools = [event["payload"] for event in events if event["event_type"] == "tool_finished"]
        final_models = [event["payload"] for event in events if event["event_type"] == "model_answer_generated"]
        costs = [event["payload"] for event in events if event["event_type"] == "cost_recorded"]
        checks = {
            "gateway_success": payload.get("success") is True and not data.get("fallback"),
            "no_confirmation": not data.get("confirmRequired") and not data.get("resumeToken"),
            "no_workflow": not any("workflow" in event["event_type"] or event["event_type"] == "human_approval_required" for event in events),
            "route_model_used": route.get("used_model") is True,
            "expected_intent": bool(plans) and plans[-1].get("intent") == case["expected_intent"],
            "expected_policies": set(policies) == set(case["policies"]),
            "current_metadata": all(c.get("metadata", {}).get("version") == "2026-10-14.2"
                                    and c["metadata"].get("publication_status") == "published"
                                    and c["metadata"].get("effective_from") == "2025-01-01T00:00:00+08:00"
                                    and c["metadata"].get("effective_to") == "2028-01-01T00:00:00+08:00"
                                    and c["metadata"].get("snapshot_version") == expected["snapshot_version"] for c in citations),
            "correct_snapshot": bool(admissions) and admissions[0].get("snapshot_version") == expected["snapshot_version"],
            "tool_path": bool(tools) == case["business_tools"] and all(t.get("status") == "success" for t in tools),
            "final_model_path": any(m.get("used_model") is True for m in final_models) == case["final_model_expected"],
            "no_invoice_final_cache": all(not c.get("cache", {}).get("common_hit_cache") for c in costs),
        }
        result = {"case": case, "request": request, "response": payload, "trace": events,
                  "elapsed_ms": elapsed, "checks": checks, "passed_structural_checks": all(checks.values()),
                  "route_model": route.get("model_name"), "route_usage": route.get("usage"),
                  "citation_ids": policies, "tool_results": tools, "final_model_results": final_models,
                  "costs": costs, "answer_human_review": "pending"}
        (OUT / f"{case['id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        results.append(result)
        print(json.dumps({"id": case["id"], "elapsed_ms": round(elapsed, 1),
                          "structural_pass": all(checks.values()), "failed": [key for key, ok in checks.items() if not ok],
                          "route_model": route.get("model_name"), "policies": policies,
                          "answer": data.get("answer")}, ensure_ascii=False), flush=True)
(OUT / "live-cases.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
print(f"保存{len(results)}条实际回复与Trace；判定结果保留，不自动关闭失败。", flush=True)
