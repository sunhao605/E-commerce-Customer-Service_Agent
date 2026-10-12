"""只核验当前路由函数；不调用模型、HTTP 或业务数据库。"""
import hashlib
import json
from pathlib import Path
import socket


def deny_network(*args, **kwargs):
    raise AssertionError("本轮设计核验禁止网络调用")


socket.socket.connect = deny_network
socket.create_connection = deny_network

from agent.models.router_client import RouteModelClient
from agent.tools.planning import build_route_plan, classify_intent, extract_return_reason
from agent.tools.runtime_context import general_chat_answer


root = Path(__file__).resolve().parents[2]
cases = [
    ("U01", "ORD1234 收到时就破了，我只要换一件，不要退款。", "refund_request"),
    ("U02", "ORD1234 两件里只退一件，给我部分退款。", "refund_request"),
    ("U03", "ORD1234 的椅子坏了，帮我维修。", "general_chat"),
    ("C01", "ORD1234 还没发货，我现在要申请整单退款。", "refund_request"),
]
results = []
for case_id, message, expected_current in cases:
    intent = classify_intent(message)
    assert intent == expected_current, (case_id, intent)
    plan = build_route_plan(intent=intent, user_message=message, order_id="ORD1234", model_used=False)
    results.append({"id": case_id, "input": message, "observed_intent": intent,
                    "route_plan": plan.model_dump(), "kind": "isolated_function_probe"})

unsupported_label = RouteModelClient._extract_intent('{"intent":"unsupported_service"}')
assert unsupported_label is None
reason = extract_return_reason(cases[0][1])
assert reason is None
paths = ["backend/agent/tools/planning.py", "backend/agent/tools/runtime_context.py",
         "backend/agent/models/router_client.py", "backend/api/schemas.py",
         "backend/resources/prompts/route_contract.md",
         "ecommerce-backend/src/main/java/com/teachdemo/ecommerce/customer_service/CustomerServiceGatewayService.java",
         "ecommerce-backend/src/main/java/com/teachdemo/ecommerce/config/DataInitializer.java",
         "ecommerce-backend/frontend/src/shop/App.tsx"]
record = {"date": "2026-10-12", "scope": "current_function_behavior_not_end_to_end",
          "online_model_calls": 0, "business_http_calls": 0, "business_database_writes": 0,
          "cases": results, "unaccepted_label": "unsupported_service",
          "label_parse_result": unsupported_label, "B15_return_reason": reason,
          "general_chat_reply": general_chat_answer("我要换货"),
          "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}}
out = Path(__file__).with_name("current.json")
out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"checks": len(cases) + 2, "record": str(out), "intents": [r["observed_intent"] for r in results]}, ensure_ascii=False))
