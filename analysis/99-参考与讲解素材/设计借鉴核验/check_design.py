"""隔离核验当前实现的设计缺口；不调用模型、HTTP 或业务数据库。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.update({
    "AGENT_COURSE_DISABLE_LLM": "1",
    "AGENT_COURSE_OFFLINE_RAG": "1",
    "AGENT_COURSE_OFFLINE_FACTS": "0",
    "AGENT_COURSE_ENV": "/tmp/huangye-design-no-model.env",
    "LANGSMITH_TRACING": "false",
    "LANGCHAIN_TRACING_V2": "false",
})

from api.schemas import ChatRequest, ChatResumeRequest
from agent.state import SUBMITTED_ACTIONS, WORKFLOW_CHECKPOINTS
from agent.tools.planning import build_order_clarification, build_route_plan, classify_intent
from agent.tools.tool_runtime import get_order_detail, search_products
from agent.trace import trace_store
from agent.workflows.after_sale_graph import REFUND_APPROVAL_GRAPH
from agent.workflows.resume import build_refund_workflow_checkpoint, business_recheck, resume_from_checkpoint
from evals.runner import EvalRunner
from pydantic import ValidationError


def run() -> dict:
    records = []
    def record(name, expected, actual):
        records.append({"probe": name, "expected_product_behavior": expected, "observed_current_behavior": actual})

    # 禁止任何意外网络调用；所有业务结果由下面的局部 Mock 明确注入。
    with patch("httpx.Client.request", side_effect=AssertionError("network_forbidden")):
        message = "退款政策是什么？我先了解一下，不申请退款。"
        rule = classify_intent(message)
        model_intent = "faq_query"
        record("policy_consultation_route", "咨询规则，不创建申请流程", {
            "input": message, "rule_intent": rule, "model_intent": model_intent, "final_intent": model_intent,
        })
        record("clarification_followup_route", "补订单号后继续上一轮退款任务", {
            "input": "ORD1234", "current_message_only_intent": classify_intent("ORD1234"),
        })

        products = [{"id": 1, "name": "家庭帐篷", "price": 599, "stock": 3},
                    {"id": 2, "name": "轻量帐篷", "price": 399, "stock": 8}]
        with patch("agent.tools.tool_runtime.products_from_ecommerce", return_value=products):
            values, call = search_products("帐篷多少钱")
        record("multiple_products", "列出候选或先明确商品，不能默选第一条", {
            "returned_count": len(values), "summary": call.output_summary,
            "next_action": call.next_action, "candidates": call.candidates,
        })
        errors = []
        for mode in ("empty_search", "service_timeout"):
            with patch("agent.ecommerce_client.ecommerce_get", return_value=[] if mode == "empty_search" else None,
                       side_effect=None if mode == "empty_search" else TimeoutError("isolated")):
                _, call = search_products("无匹配商品")
            errors.append({"injected": mode, "error_type": call.error_type, "fact_source": call.arguments.get("fact_source")})
        record("empty_vs_unavailable", "无匹配与系统故障使用不同结果", errors)

        request = ChatRequest(session_id="design-probe", runtime_user_id="U1001", user_message="帮我退款")
        order = {"orderNo": "ORD1234", "paymentStatus": "PAID", "fulfillmentStatus": "PAID_PENDING_SHIPMENT"}
        context = {"currentUserOrders": [order]}
        verified, call = get_order_detail("ORD1234", "U1001", context)
        plan = build_route_plan(intent="refund_request", user_message="帮我退款", order_id=None, model_used=False)
        clarification = build_order_clarification(request.model_copy(update={"runtime_context": context}), plan)
        record("java_order_summary_without_owner", "可信列表契约一致，候选可显示且归属可验证", {
            "order_read_status": call.status, "order_returned": verified is not None,
            "clarification_candidate_count": len(clarification.candidates),
        })
        record("missing_payment_status", "缺少已支付证据时阻断退款资格", {
            "eligibility": REFUND_APPROVAL_GRAPH.assess_eligibility({"fulfillmentStatus": "PAID_PENDING_SHIPMENT"}),
        })
        workflow = build_refund_workflow_checkpoint(request=request, order=order, order_id="ORD1234", citations=[])
        checkpoint = WORKFLOW_CHECKPOINTS[(request.session_id, workflow["workflow_id"])]
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=None):
            record("recheck_unavailable", "最新事实不可得则 passed=false", business_recheck(checkpoint))
        resume = ChatResumeRequest(session_id=request.session_id, workflow_id=workflow["workflow_id"],
            resume_token=workflow["resume_token"], reviewer_id="ordinary-user", reviewer_role="customer", decision="approved")
        with patch("agent.workflows.resume.order_fact_from_ecommerce", return_value=order):
            approved = resume_from_checkpoint(resume, trace_store)
            rejected = resume_from_checkpoint(resume.model_copy(update={"decision": "rejected"}), trace_store)
        record("reviewer_and_terminal_state", "普通客户不能审批；已批准结果不能反向改为拒绝", {
            "customer_role_result": approved.status, "later_rejected_result": rejected.status,
            "stored_checkpoint_status": checkpoint["workflow"]["status"],
            "simulated_submission_count": len(SUBMITTED_ACTIONS),
        })
        try:
            ChatResumeRequest.model_validate({"session_id": "s", "workflow_id": "w", "resume_token": "t",
                                              "decision": "approved", "reviewer_note": "用户确认"})
        except ValidationError as error:
            record("java_resume_payload", "网关请求应符合 Python 契约且不伪造审批身份", {
                "missing_fields": [item["loc"][0] for item in error.errors()],
            })
        answer = "可以准备退货申请；当前还不是退货成功，仍需人工复核。"
        normalized = EvalRunner._normalize_answer_text(answer)
        record("answer_matcher_boundary", "人工复核应识别为正向审核边界", {
            "answer": answer, "normalized": normalized,
            "human_review_matches": EvalRunner._answer_phrase_matches(normalized, "人工复核"),
        })
    paths = ["backend/agent/customer_service_agent.py", "backend/agent/context.py", "backend/agent/tools/planning.py",
             "backend/agent/tools/tool_runtime.py", "backend/agent/ecommerce_client.py", "backend/agent/workflows/resume.py",
             "backend/agent/workflows/after_sale_graph.py", "backend/evals/runner.py", "backend/api/schemas.py",
             "ecommerce-backend/frontend/src/shop/App.tsx",
             "ecommerce-backend/src/main/java/com/teachdemo/ecommerce/customer_service/CustomerServiceGatewayService.java"]
    return {"date": "2026-10-07", "mode": "isolated_function_probes_with_synthetic_data",
            "model_calls": 0, "business_http_calls": 0, "database_writes": 0,
            "boundary": "记录现状缺口，不是修复验收或真实模型/商城端到端评测",
            "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths},
            "records": records}


if __name__ == "__main__":
    result = run()
    destination = Path(__file__).with_name("result.json")
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
