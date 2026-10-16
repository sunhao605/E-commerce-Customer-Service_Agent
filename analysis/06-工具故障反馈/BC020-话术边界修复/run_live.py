"""真实模型仍负责理解与工具选择，最终商品回复只能使用受控话术。"""
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

OUT = Path(__file__).resolve().parent
cases = json.loads((OUT / "frozen-cases.json").read_text())["cases"]
results = []
with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120, trust_env=False) as agent, \
     httpx.Client(base_url="http://127.0.0.1:8081", timeout=15, trust_env=False) as business:
    products_response = business.get("/api/products")
    products_response.raise_for_status()
    products = products_response.json()["data"]
    for case in cases:
        request = {"session_id": "bc20-" + uuid4().hex, "runtime_user_id": "U1001", "user_message": case["message"]}
        started = time.perf_counter()
        response = agent.post("/chat", json=request)
        response.raise_for_status()
        data = response.json()
        elapsed = (time.perf_counter() - started) * 1000
        trace = agent.get(f"/sessions/{request['session_id']}/trace").json()
        calls = data["tool_calls"]
        product = next(p for p in products if p["name"] == case["target"])
        final = data["session_state"]["model"]["final_answer"]
        checks = {
            "real_route_model": data["session_state"]["model"]["route_planner"]["used_model"] is True,
            "real_business_facts": bool(calls) and all(c["status"] == "success" and c["arguments"].get("fact_source") == "business_api" for c in calls),
            "correct_product": bool(calls) and str(calls[-1]["arguments"].get("product_id")) == str(product["id"]),
            "no_final_model_rewrite": not final["used_model"] and final["fallback_reason"] == "verified_tool_wording",
            "no_workflow": data["session_state"]["workflow"] is None,
            "no_unsupported_promise": not any(word in data["answer"] for word in ("已开启", "已登记", "帮您留意", "帮你留意", "到货后通知你", "到货后提醒你")),
        }
        if case["reminder_requested"]:
            checks["controlled_wording"] = data["answer"] == calls[-1]["output_summary"] + "\n目前不支持到货或补货提醒，你可以稍后再查询库存。"
        else:
            checks["controlled_wording"] = data["answer"] == calls[-1]["output_summary"]
        row = {"case": case, "request": request, "response": data, "trace": trace,
               "business_fact": product, "elapsed_ms": elapsed, "checks": checks, "passed": all(checks.values())}
        (OUT / f"{case['id']}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2))
        results.append(row)
        print(json.dumps({"id": case["id"], "passed": row["passed"], "failed": [k for k, ok in checks.items() if not ok],
                          "elapsed_ms": round(elapsed, 1), "answer": data["answer"]}, ensure_ascii=False), flush=True)
(OUT / "live-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
assert len(results) == 5 and all(row["passed"] for row in results), "Read saved failures; do not hide or auto-retry"
