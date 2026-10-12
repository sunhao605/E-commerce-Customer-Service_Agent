"""第 41 课综合演练 Agent 编排层。它复用 Tool、RAG、Workflow、Trace、Eval 和成本治理能力。"""

from __future__ import annotations

import os
from typing import Any

from api.schemas import *
from settings import load_course_env
from agent.cancellation import observe_cancellation
from agent.context import SESSION_MEMORIES, build_context, current_memory, update_memory
from agent.cost import build_cost_summary
from agent.hooks import HookManager
from agent.ecommerce_client import has_product_term, product_query_keyword
from agent.mcp_catalog import MCP_CATALOG
from agent.models.answer_client import FinalAnswerModelClient, FinalAnswerModelResult
from agent.models.router_client import RouteModelClient
from agent.product_query import (
    CLARIFY_MESSAGES,
    STOP_PENDING_PATTERN,
    candidate_prompt,
    product_selection_index,
    resolve_product_target,
)
from agent.trace import public_trace_summary, record_initial_chat_trace, record_trace_events, trace_store
from retrieval.knowledge import (
    REFUND_POLICY,
    RETURN_POLICY,
    invoice_faq_result,
    low_confidence_result,
    policy_query_result,
    promotion_policy_result,
)
from agent.source_guard import inspect_source
from agent.state import COMMON_HIT_CACHE, MESSAGE_COUNT_BY_SESSION
from agent.tools.langchain_runtime import LangChainToolRunner
from agent.tools.planning import (
    build_order_clarification,
    build_route_plan,
    classify_intent,
    estimate_tokens,
    extract_order_id,
    extract_return_reason,
    format_open_task,
    is_slot_reply,
    is_specific_eligibility_question,
)
from agent.tools.runtime_context import (
    general_chat_answer,
    is_runtime_identity_query,
    logistics_status_label,
    order_no,
    order_status_label,
    runtime_context_summary,
    runtime_identity_answer,
)
from agent.tools.tool_runtime import get_after_sale_eligibility, get_order_detail, get_order_logistics, get_refund_status, search_products
from agent.workflows.after_sale_graph import RECEIVED_RETURN_GRAPH, REFUND_APPROVAL_GRAPH
from agent.workflows.resume import TrustedReviewer, resume_from_checkpoint

class Lesson41Agent:
    """第十幕综合演练版：复用前面能力，服务大促总演习和项目答辩验证。"""

    def __init__(self) -> None:
        self.route_model_client = RouteModelClient()
        self.answer_model_client = FinalAnswerModelClient()
        self.langchain_tool_runner = LangChainToolRunner()

    def chat(self, request: ChatRequest) -> ChatResponse:
        """编排总演习聊天链路，串联 Tool、RAG、Workflow/HITL、降级、安全、Trace 和成本治理。"""
        load_course_env()
        MESSAGE_COUNT_BY_SESSION[request.session_id] = MESSAGE_COUNT_BY_SESSION.get(request.session_id, 0) + 1
        message_count = MESSAGE_COUNT_BY_SESSION[request.session_id]
        memory = current_memory(request.session_id, request.runtime_user_id)
        # 商城在网关取消草稿；下一轮先清理Python待办，避免旧候选复活。
        if (request.runtime_context or {}).get("cancelledAfterSaleDraft") is True:
            memory["open_task"] = None
        open_task = memory.get("open_task")
        runtime = request.runtime_context or {}
        if runtime.get("mallAfterSaleRouting"):
            open_task = runtime.get("afterSaleOpenTask")
        query_task = runtime.get("afterSaleQuery") if "afterSaleQuery" in runtime else memory.get("eligibility_query")
        selection_message = (request.runtime_context or {}).get("userMessage") or request.user_message
        active_task = query_task if query_task and query_task.get("pending_slot") else open_task
        task_note = format_open_task(active_task)
        if query_task and query_task.get("order_id"):
            task_note = (task_note or "") + f"\n上一轮只读资格查询的订单：{query_task['order_id']}，业务类型：{query_task.get('application_type') or '尚未核实'}。只有明确申请才办理，好的或知道了不算申请。"
        fallback_intent = classify_intent(request.user_message)
        route_result = self.route_model_client.plan_intent(
            request.user_message,
            fallback_intent=fallback_intent,
            task_note=task_note,
        )
        if not route_result.used_model and active_task and is_slot_reply(selection_message):
            route_result = route_result.model_copy(
                update={"intent": active_task["intent"], "fallback_reason": "open_task_slot"}
            )
        intent = route_result.intent
        if is_specific_eligibility_question(selection_message) or (
            query_task and is_slot_reply(selection_message)
        ):
            intent = "after_sale_eligibility_query"
            route_result = route_result.model_copy(update={"intent": intent})
        product_state = memory.get("product_query") or {}
        # 商品待选期间的纯序号选择即使被模型判成闲聊也续接商品任务；“先不看了”只结束商品待选。
        if intent != "product_query" and (
            product_selection_index(selection_message) is not None
            or (product_state.get("pending_candidates") and STOP_PENDING_PATTERN.fullmatch(selection_message.strip()))
        ):
            intent = "product_query"
            route_result = route_result.model_copy(update={"intent": intent, "fallback_reason": "product_pending_selection"})
        if intent not in {"product_query", "general_chat", "unknown"}:
            product_state["pending_candidates"] = None
            product_state["original_question"] = None
        if intent != "after_sale_eligibility_query" and not (request.runtime_context or {}).get("mallAfterSaleRouting"):
            observation = observe_cancellation(request, open_task)
            if observation is not None:
                trace_store.add(request.session_id, "cancellation_intent_observed", observation)
        explicit_order_id = extract_order_id(selection_message)
        order_id, context_report, compression_report = build_context(
            request, explicit_order_id, query_task=query_task, eligibility_query=intent == "after_sale_eligibility_query"
        )
        if intent in {"refund_request", "return_request"} and query_task and query_task.get("order_id") and not explicit_order_id:
            page_order = runtime.get("current_order_id") or runtime.get("relatedOrderNo")
            if not page_order or page_order == query_task["order_id"]:
                order_id = query_task["order_id"]
                context_report["chosen_order_id"] = order_id
        draft = runtime.get("afterSaleDraft") or {}
        if runtime.get("mallAfterSaleRouting") and intent in {"refund_request", "return_request"} \
                and not order_id and not query_task and not is_slot_reply(selection_message) \
                and draft.get("valid") is True and draft.get("ownerVerified") is True and draft.get("order"):
            order_id = str(draft["order"])
            context_report["chosen_order_id"] = order_id
            context_report["conflict_resolutions"].append("order_id: 续接商城已校验且有效的当前申请草稿。")
        route_plan = build_route_plan(
            intent=intent,
            user_message=request.user_message,
            order_id=order_id,
            model_used=route_result.used_model,
        )
        runtime_context = runtime_context_summary(request)
        uses_runtime_identity = is_runtime_identity_query(request.user_message)
        citations: list[Citation] = []
        tool_calls: list[ToolCallTrace] = []
        workflow: dict[str, Any] | None = None
        eligibility: dict[str, Any] | None = None
        cache_hit = False
        degraded = False
        degradation_reason: str | None = None
        preserve_product_wording = False
        rag_rerank: dict[str, Any] | None = None
        rag_retrieval: dict[str, Any] | None = None
        verified_order_id: str | None = None
        verified_product_name: str | None = None
        verified_product_id: str | None = None
        clarification = build_order_clarification(request, route_plan)
        hooks = HookManager()
        tool_calling_state: dict[str, Any] = {
            "create_agent": False,
            "skip_reason": "route_does_not_need_business_tools",
            "available_tools": route_plan.required_tools,
            "selected_tools": [],
            "message_types": [],
            "tool_message_count": 0,
            "fallback_used": False,
        }
        tool_agent_prompt_fragments: list[dict[str, Any]] = []
        tool_agent_model_calls = 0

        record_initial_chat_trace(
            session_id=request.session_id,
            runtime_user_id=request.runtime_user_id,
            runtime_nickname=request.runtime_nickname,
            runtime_member_level=request.runtime_member_level,
            runtime_risk_level=request.runtime_risk_level,
            intent=intent,
            estimated_tokens=estimate_tokens(request.user_message),
            route_result=route_result,
            context_report=context_report,
            compression_report=compression_report,
        )
        trace_store.add(
            request.session_id,
            "route_plan_built",
            {
                "session_id": request.session_id,
                "intent": route_plan.intent,
                "source": route_plan.source,
                "required_tools": route_plan.required_tools,
                "needs_rag": route_plan.needs_rag,
                "needs_business_tools": route_plan.needs_business_tools,
                "requires_workflow": route_plan.requires_workflow,
                "risk_level": route_plan.risk_level,
            },
        )
        if clarification:
            trace_store.add(
                request.session_id,
                "tool_clarification_required",
                {
                    "session_id": request.session_id,
                    "clarification_field": clarification.clarification_field,
                    "candidate_count": len(clarification.candidates),
                    "candidate_order_ids": [candidate.value for candidate in clarification.candidates],
                    "status": "waiting_for_user",
                },
            )

        if uses_runtime_identity:
            answer = runtime_identity_answer(request)
            risk_level = "low"
            next_action = "answer_user"
            needs_human_approval = False
            trace_store.add(
                request.session_id,
                "runtime_identity_answered",
                {
                    "session_id": request.session_id,
                    "intent": intent,
                    "runtime_user_id": request.runtime_user_id,
                    "used_runtime_context": True,
                },
            )
        elif intent == "degradation_request":
            degraded = True
            injected = "故障注入演示" in request.user_message
            degradation_reason = "business_tool_unavailable" if injected else "user_reported_unverified"
            degradation_source = "explicit_course_fault_injection" if injected else "user_reported_service_failure"
            trace_store.add(
                request.session_id,
                "degradation_triggered",
                {
                    "session_id": request.session_id,
                    "intent": intent,
                    "degraded": True,
                    "reason": degradation_reason,
                    "source": degradation_source,
                },
            )
            answer = (
                "订单或物流服务暂时不可用，本轮不继续猜测业务事实。建议稍后重试，或转人工客服继续核验。"
                if injected
                else "你这次是在报告故障：页面提示查不了，而且现在仍然查不了。我还没有核实服务是否中断，所以不把你的描述当成已经查证的系统事实，本轮也不继续判断订单或物流。你可以稍后重试，或转人工客服核验。"
            )
            risk_level = "medium"
            next_action = "transfer_to_human"
            needs_human_approval = False
        elif intent == "security_request":
            trace_store.add(
                request.session_id,
                "prompt_security_blocked",
                {"session_id": request.session_id, "intent": intent, "risk_level": "high", "status": "blocked"},
            )
            answer = "我不能提供受保护系统信息、受保护推理摘要、工具细节或内部策略。"
            risk_level: RiskLevel = "high"
            next_action: NextAction = "answer_user"
            needs_human_approval = False
        elif intent == "low_confidence_query":
            knowledge_result = low_confidence_result(request.session_id, intent)
            record_trace_events(request.session_id, knowledge_result.trace_events)
            answer = knowledge_result.answer
            citations = knowledge_result.citations
            risk_level = knowledge_result.risk_level
            next_action = knowledge_result.next_action
            needs_human_approval = knowledge_result.needs_human_approval
        elif intent == "invoice_query":
            knowledge_result = invoice_faq_result(request.session_id)
            record_trace_events(request.session_id, knowledge_result.trace_events)
            answer = knowledge_result.answer
            citations = knowledge_result.citations
            risk_level = knowledge_result.risk_level
            next_action = knowledge_result.next_action
            needs_human_approval = knowledge_result.needs_human_approval
            cache_hit = knowledge_result.cache_hit
            rag_retrieval = knowledge_result.retrieval_debug
        elif intent in {"refund_policy_query", "return_policy_query"}:
            knowledge_result = policy_query_result(request.session_id, intent)
            record_trace_events(request.session_id, knowledge_result.trace_events)
            answer = knowledge_result.answer
            if order_id:
                answer = f"{answer} 这轮只说明通用规则，没有核验这笔订单，也没有准备申请。"
            citations = knowledge_result.citations
            risk_level = knowledge_result.risk_level
            next_action = knowledge_result.next_action
            needs_human_approval = knowledge_result.needs_human_approval
        elif intent == "promotion_query":
            knowledge_result = promotion_policy_result(request.session_id, request.user_message)
            if not knowledge_result.citations:
                intent = "low_confidence_query"
            record_trace_events(request.session_id, knowledge_result.trace_events)
            answer = knowledge_result.answer
            citations = knowledge_result.citations
            rag_rerank = knowledge_result.rerank
            rag_retrieval = knowledge_result.retrieval_debug
            risk_level = knowledge_result.risk_level
            next_action = knowledge_result.next_action
            needs_human_approval = knowledge_result.needs_human_approval
        elif intent == "after_sale_eligibility_query":
            if clarification:
                answer = clarification.message + "这轮只查资格，不准备申请。"
                if clarification.candidates:
                    answer += "\n" + "\n".join(f"{index}. {item.label}（{item.hint}）" for index, item in enumerate(clarification.candidates, 1))
                next_action = "ask_clarification"
            else:
                hooks.pre_tool_call("get_after_sale_eligibility", {"order_id": order_id}, request.runtime_user_id)
                eligibility, call = get_after_sale_eligibility(order_id, request.runtime_user_id)
                hooks.post_tool_call(call)
                tool_calls.append(call)
                answer = call.output_summary
                next_action = "answer_user"
                trace_store.add(request.session_id, "after_sale_eligibility_checked", {
                    "session_id": request.session_id, "order_id": order_id,
                    "status": eligibility.get("status") if eligibility else "UNKNOWN", "read_only": True,
                    "error_type": call.error_type,
                })
            risk_level = "low"
            needs_human_approval = False
        elif (request.runtime_context or {}).get("mallAfterSaleRouting") and intent in {"refund_request", "return_request"}:
            # 商城先分流，再由Java核验与准备；路由阶段不生成模拟审批草稿。
            answer = clarification.message if clarification else "已识别申请诉求，待商城核验最新资格并准备确认。"
            risk_level = "high"
            next_action = "ask_clarification" if clarification else "answer_user"
            needs_human_approval = False
        elif intent == "return_request":
            if clarification:
                answer = clarification.message
                risk_level = "high"
                next_action = "ask_clarification"
                needs_human_approval = False
            else:
                hooks.pre_tool_call("get_order_detail", {"order_id": order_id}, request.runtime_user_id)
                trace_store.add(
                    request.session_id,
                    "tool_started",
                    {"session_id": request.session_id, "tool_name": "get_order_detail", "order_id": order_id},
                )
                order, detail_call = get_order_detail(order_id, request.runtime_user_id, request.runtime_context)
                hooks.post_tool_call(detail_call)
                tool_calls.append(detail_call)
                trace_store.add(
                    request.session_id,
                    "tool_finished",
                    {
                        "session_id": request.session_id,
                        "tool_name": detail_call.tool_name,
                        "order_id": order_id,
                        "status": detail_call.status,
                        "risk_level": detail_call.risk_level,
                    },
                )
                if order is None:
                    answer = detail_call.output_summary
                    risk_level = "high"
                    next_action = "transfer_to_human"
                    needs_human_approval = False
                else:
                    verified_order_id = order_no(order)
                    return_reason = extract_return_reason(request.user_message)
                    citations.append(RETURN_POLICY)
                    trace_store.add(
                        request.session_id,
                        "rag_pre_retrieved",
                        {
                            "session_id": request.session_id,
                            "hit_count": 1,
                            "retrieval_stage": "pre_retrieval",
                            "policy_id": "return_after_delivery",
                        },
                    )
                    workflow = RECEIVED_RETURN_GRAPH.run(order, return_reason=return_reason, citations=citations)
                    eligible = workflow["eligibility_status"] == "eligible_for_application"
                    reason_labels = {
                        "order_not_received": "订单尚未签收",
                        "product_not_returnable": "商品不支持无理由退货",
                        "product_returnability_unknown": "暂未核实商品可退属性",
                        "signed_time_missing": "缺少可信签收时间",
                        "signed_time_in_future": "签收时间晚于当前业务日期，需要核实",
                        "return_window_expired": "签收已超过 7 天",
                        "return_reason_missing": "还需要补充明确的退货原因",
                        "other_after_sale_type": "该诉求属于其他售后类型，需要人工核验",
                    }
                    answer = (
                        f"订单 {order_no(order)} 已检查签收时间、商品可退属性和七天窗口，可以准备退货申请；商城确认提交后等待后台审核，同意退货后寄回，实际收到且验收通过才退款。"
                        if eligible
                        else f"订单 {order_no(order)} 暂不满足七天无理由退货条件：{reason_labels.get(workflow['eligibility_reason'], workflow['eligibility_reason'])}；其他售后请联系人工核验。"
                    )
                    risk_level = "high"
                    next_action = "transfer_to_human" if eligible else "answer_user"
                    needs_human_approval = eligible
                    trace_store.add(request.session_id, "received_return_workflow_completed", {"session_id": request.session_id, **workflow})
        elif intent == "product_query":
            outcome = self._run_product_query(request, memory, hooks)
            tool_calls.extend(outcome["tool_calls"])
            tool_calling_state = outcome["tool_calling_state"]
            tool_agent_prompt_fragments = outcome["prompt_fragments"]
            tool_agent_model_calls = outcome["model_calls"]
            answer = outcome["answer"]
            risk_level = outcome["risk_level"]
            next_action = outcome["next_action"]
            needs_human_approval = False
            verified_product_id = outcome.get("verified_product_id")
            verified_product_name = outcome.get("verified_product_name")
            preserve_product_wording = bool(outcome.get("preserve_wording"))
            if outcome.get("degraded"):
                degraded = True
                degradation_reason = outcome.get("degradation_reason")
                trace_store.add(
                    request.session_id,
                    "degradation_triggered",
                    {
                        "session_id": request.session_id,
                        "intent": intent,
                        "degraded": True,
                        "reason": degradation_reason,
                        "source": "product_tool_read_failure",
                    },
                )
            citations.extend(outcome["citations"])
            rag_rerank = outcome.get("rag_rerank")
            rag_retrieval = outcome.get("rag_retrieval")
        elif intent in {"order_query", "refund_status_query", "refund_request"}:
            tool_result = self.langchain_tool_runner.run(
                request=request,
                intent=intent,
                required_tools=route_plan.required_tools,
                hooks=hooks,
                expected_order_id=order_id,
            )
            tool_calling_state = tool_result.state
            tool_agent_prompt_fragments = tool_result.prompt_fragments
            tool_agent_model_calls = tool_result.model_calls
            trace_store.add(
                request.session_id,
                "langchain_tool_agent_completed",
                {
                    "session_id": request.session_id,
                    "create_agent": tool_calling_state.get("create_agent"),
                    "selected_tools": tool_calling_state.get("selected_tools", []),
                    "message_types": tool_calling_state.get("message_types", []),
                    "fallback_used": tool_calling_state.get("fallback_used"),
                    "status": "success" if tool_result.executed else "fallback",
                },
            )
            if tool_result.executed:
                order = tool_result.order
                tool_calls.extend(tool_result.tool_calls)
                detail_call = tool_calls[0]
                for call in tool_result.tool_calls:
                    trace_store.add(
                        request.session_id,
                        "tool_started",
                        {"session_id": request.session_id, "tool_name": call.tool_name, "order_id": order_id},
                    )
                    trace_store.add(
                        request.session_id,
                        "tool_finished",
                        {
                            "session_id": request.session_id,
                            "tool_name": call.tool_name,
                            "order_id": order_id,
                            "status": call.status,
                            "risk_level": call.risk_level,
                            "next_action": call.next_action,
                        },
                    )
            elif clarification is None:
                hooks.pre_tool_call("get_order_detail", {"order_id": order_id}, request.runtime_user_id)
                trace_store.add(
                    request.session_id,
                    "tool_started",
                    {"session_id": request.session_id, "tool_name": "get_order_detail", "order_id": order_id},
                )
                order, detail_call = get_order_detail(order_id, request.runtime_user_id, request.runtime_context)
                tool_calls.append(detail_call)
                hooks.post_tool_call(detail_call)
                trace_store.add(
                    request.session_id,
                    "tool_finished",
                    {
                        "session_id": request.session_id,
                        "tool_name": detail_call.tool_name,
                        "order_id": order_id,
                        "status": detail_call.status,
                        "risk_level": detail_call.risk_level,
                        "next_action": detail_call.next_action,
                    },
                )
            else:
                order = None
                detail_call = None
            if order is not None:
                verified_order_id = order_no(order)
            if clarification:
                answer = clarification.message
                if clarification.candidates:
                    choices = "；".join(f"{candidate.value}（{candidate.hint}）" for candidate in clarification.candidates)
                    answer = f"{answer} 当前账号下可选订单：{choices}。"
                risk_level = "medium"
                next_action = "ask_clarification"
                needs_human_approval = False
            elif intent == "order_query" and order:
                logistics_call = next((call for call in tool_calls if call.tool_name == "get_order_logistics"), None)
                if logistics_call is None:
                    hooks.pre_tool_call("get_order_logistics", {"order_id": order_id}, request.runtime_user_id)
                    logistics_call = get_order_logistics(order)
                    tool_calls.append(logistics_call)
                    hooks.post_tool_call(logistics_call)
                    trace_store.add(
                        request.session_id,
                        "tool_finished",
                        {
                            "session_id": request.session_id,
                            "tool_name": logistics_call.tool_name,
                            "order_id": order_id,
                            "status": logistics_call.status,
                            "risk_level": logistics_call.risk_level,
                        },
                    )
                answer = f"我帮你查到了，订单 {order_no(order)} 目前{order_status_label(order)}，物流状态是{logistics_status_label(order)}。"
                risk_level = "low"
                next_action = "answer_user"
                needs_human_approval = False
            elif intent == "refund_status_query" and order:
                status_call = next((call for call in tool_calls if call.tool_name == "get_refund_status"), None)
                if status_call is None:
                    hooks.pre_tool_call("get_refund_status", {"order_id": order_id}, request.runtime_user_id)
                    trace_store.add(
                        request.session_id,
                        "tool_started",
                        {"session_id": request.session_id, "tool_name": "get_refund_status", "order_id": order_id},
                    )
                    status_call = get_refund_status(order, request.runtime_user_id)
                    tool_calls.append(status_call)
                    hooks.post_tool_call(status_call)
                    trace_store.add(
                        request.session_id,
                        "tool_finished",
                        {
                            "session_id": request.session_id,
                            "tool_name": status_call.tool_name,
                            "order_id": order_id,
                            "status": status_call.status,
                            "risk_level": status_call.risk_level,
                        },
                    )
                answer = status_call.output_summary
                risk_level = status_call.risk_level
                next_action = status_call.next_action or "answer_user"
                needs_human_approval = False
                if status_call.status == "error":
                    degraded = True
                    degradation_reason = status_call.error_type or "refund_status_unavailable"
            elif intent == "refund_request":
                citations.append(REFUND_POLICY)
                trace_store.add(
                    request.session_id,
                    "rag_pre_retrieved",
                    {
                        "session_id": request.session_id,
                        "hit_count": 1,
                        "retrieval_stage": "pre_retrieval",
                        "policy_id": "refund_before_shipping",
                    },
                )
                eligible, eligibility_reason = REFUND_APPROVAL_GRAPH.assess_eligibility(order)
                if not eligible:
                    workflow = None
                    answer = (
                        "该订单已经发货，不能进入未发货退款审批；请按签收后的退货或售后流程处理。"
                        if eligibility_reason == "order_already_shipped_or_not_eligible"
                        else "订单事实或支付状态没有通过退款资格校验，暂不能创建退款审批，请转人工核验。"
                    )
                    risk_level = "high"
                    next_action = "transfer_to_human"
                    needs_human_approval = False
                    trace_store.add(
                        request.session_id,
                        "refund_eligibility_blocked",
                        {"session_id": request.session_id, "order_id": order_id, "reason": eligibility_reason, "status": "blocked"},
                    )
                else:
                    workflow = REFUND_APPROVAL_GRAPH.run(
                        request=request,
                        order=order,
                        order_id=order_id,
                        citations=citations,
                    )
                if workflow is None:
                    pass
                else:
                    for node_name in workflow.get("node_history", []):
                        trace_store.add(
                            request.session_id,
                            "workflow_node_finished",
                            {
                                "session_id": request.session_id,
                                "workflow_id": workflow["workflow_id"],
                                "graph_name": workflow.get("graph_name"),
                                "node_name": node_name,
                                "status": "completed",
                            },
                        )
                    trace_store.add(
                        request.session_id,
                        "workflow_completed",
                        {
                            "session_id": request.session_id,
                            **workflow,
                            "risk_level": "high",
                            "needs_human_approval": True,
                        },
                    )
                    trace_store.add(
                        request.session_id,
                        "human_approval_required",
                        {
                            "session_id": request.session_id,
                            "workflow_id": workflow["workflow_id"],
                            "pending_action": "require_approval",
                            "risk_level": "high",
                            "needs_human_approval": True,
                        },
                    )
                    answer = f"{order_no(order)} 可以进入未发货退款申请判断，但资金动作必须等待人工审批。"
                    risk_level = "high"
                    next_action = "transfer_to_human"
                    needs_human_approval = True
            else:
                answer = detail_call.output_summary
                risk_level = "medium"
                next_action = "ask_clarification" if detail_call.error_type == "missing_order_id" else "transfer_to_human"
                needs_human_approval = False
        elif intent == "unknown":
            answer = (
                f"我收到了订单号 {order_id}，但你还没有说明要办什么。请告诉我是查物流、查进度，还是申请售后。在你说清楚之前，我不会替你选定，也不会查询这笔订单。"
                if order_id
                else "我还不知道你要办哪一件事。如果是订单物流、商品、活动规则或售后，请直接说具体需求。"
            )
            risk_level = "low"
            next_action = "answer_user"
            needs_human_approval = False
        else:
            answer = general_chat_answer(request.user_message)
            risk_level = "low"
            next_action = "answer_user"
            needs_human_approval = False

        # Tool/RAG 文本也是外部数据，进入最终模型前必须按来源做污染检查。
        external_reports: list[dict[str, Any]] = []
        for call in tool_calls:
            report = inspect_source("tool_result", call.output_summary)
            if report["tainted"]:
                call.output_summary = str(report["sanitized_content"])
            external_reports.append({key: value for key, value in report.items() if key != "sanitized_content"})
        for citation in citations:
            report = inspect_source("rag_document", citation.snippet)
            if report["tainted"]:
                citation.snippet = str(report["sanitized_content"])
            external_reports.append({key: value for key, value in report.items() if key != "sanitized_content"})
        if external_reports:
            source_safety = context_report["source_safety"]
            source_safety["reports"].extend(external_reports)
            source_safety["tainted"] = source_safety["tainted"] or any(report["tainted"] for report in external_reports)
            source_safety["tainted_sources"] = sorted(
                set(source_safety["tainted_sources"])
                | {report["source"] for report in external_reports if report["tainted"]}
            )
            trace_store.add(
                request.session_id,
                "context_source_safety_checked",
                {
                    "session_id": request.session_id,
                    "tainted": source_safety["tainted"],
                    "tainted_sources": source_safety["tainted_sources"],
                    "source_count": len(source_safety["reports"]),
                },
            )

        if rag_retrieval:
            trace_store.add(
                request.session_id,
                "rag_hybrid_retrieved",
                {
                    "session_id": request.session_id,
                    "mode": rag_retrieval.get("mode"),
                    "rewritten_query": (rag_retrieval.get("plan") or {}).get("rewritten_query"),
                    "index_version": rag_retrieval.get("index_version"),
                    "index_chunk_count": rag_retrieval.get("index_chunk_count"),
                    "index_cache_hit": rag_retrieval.get("index_cache_hit"),
                    "retrieval_cache_hit": rag_retrieval.get("retrieval_cache_hit"),
                    "vector_policy_ids": rag_retrieval.get("vector_policy_ids", []),
                    "keyword_policy_ids": rag_retrieval.get("keyword_policy_ids", []),
                    "hit_count": len(citations),
                    "retrieval_stage": "hybrid_retrieval",
                },
            )

        model_answer = self._compose_final_answer(
            request=request,
            intent=intent,
            answer=answer,
            risk_level=risk_level,
            next_action=next_action,
            tool_calls=tool_calls,
            citations=citations,
            workflow=workflow,
            cache_hit=cache_hit,
            degraded=degraded,
            skip_final_model=uses_runtime_identity or intent == "after_sale_eligibility_query"
                or bool((request.runtime_context or {}).get("mallAfterSaleRouting") and intent in {"refund_request", "return_request"}),
            preserve_tool_wording=preserve_product_wording,
            enable_reasoning=request.reasoning_view == "teaching",
            context_report=context_report,
        )
        answer = model_answer.answer
        if (
            intent == "invoice_query"
            and not cache_hit
            and (model_answer.used_model or os.getenv("AGENT_COURSE_DISABLE_LLM") == "1")
        ):
            # 在线缓存模型基于证据生成的最终话术；离线测试则缓存显式兜底话术。
            COMMON_HIT_CACHE["faq:invoice_issue"] = {
                "answer": answer,
                "citation": "invoice_issue",
                "source": "model_final_answer" if model_answer.used_model else "explicit_offline_fallback",
            }
        reasoning_content = model_answer.reasoning_content if request.reasoning_view == "teaching" else None
        prompt_fragments = [
            *route_result.prompt_fragments,
            *tool_agent_prompt_fragments,
            *model_answer.prompt_fragments,
        ]
        selected_mcp_tool = next((call.tool_name for call in reversed(tool_calls) if call.status == "success"), None)
        mcp_binding = MCP_CATALOG.binding_summary(selected_mcp_tool, risk_level)
        trace_store.add(request.session_id, "mcp_binding_resolved", {"session_id": request.session_id, **mcp_binding})

        if prompt_fragments:
            trace_store.add(
                request.session_id,
                "prompt_context_built",
                {
                    "session_id": request.session_id,
                    "registry_schema": "prompt_registry_v1",
                    "selected_fragments": prompt_fragments,
                    "prompt_body_exposed": False,
                },
            )

        stored = current_memory(request.session_id, request.runtime_user_id)
        if intent == "after_sale_eligibility_query":
            queried = next((call for call in tool_calls if call.tool_name == "get_after_sale_eligibility" and call.status == "success"), None)
            stored["eligibility_query"] = {
                "intent": intent, "order_id": order_id if queried else None,
                "application_type": eligibility.get("applicationType") if eligibility else None,
                "pending_slot": "order_id" if clarification else None,
                "candidates": [{"value": item.value, "label": item.label} for item in (clarification.candidates if clarification else [])],
            }
        elif intent == "product_query":
            # 商品任务状态独立保存在 product_query，不新建、也不清掉退款待办。
            pass
        elif next_action == "ask_clarification" and intent not in {"unknown", "general_chat", "product_query"}:
            stored["open_task"] = {
                "intent": intent,
                "pending_slot": clarification.clarification_field if clarification else "order_id",
                "candidates": [
                    {"value": item.value, "label": item.label}
                    for item in (clarification.candidates if clarification else [])
                ],
            }
        else:
            stored["open_task"] = None
        memory = update_memory(
            session_id=request.session_id,
            runtime_user_id=request.runtime_user_id,
            intent=intent,
            verified_order_id=verified_order_id,
            user_message=request.user_message,
            verified_product_name=verified_product_name,
            verified_product_id=verified_product_id,
        )
        hook_completion = hooks.on_completion(risk_level=risk_level, next_action=next_action, degraded=degraded)
        for hook_event in hooks.events:
            trace_store.add(request.session_id, "hook_executed", {"session_id": request.session_id, **hook_event})
        cost_summary = build_cost_summary(
            request=request,
            intent=intent,
            tool_calls=tool_calls,
            citations=citations,
            workflow=workflow,
            answer=answer,
            cache_hit=cache_hit,
            route_model_used=route_result.used_model,
            answer_model_used=model_answer.used_model,
            reasoning_content_returned=bool(reasoning_content),
            reasoning_source=model_answer.reasoning_source,
            degraded=degraded,
            degradation_reason=degradation_reason,
            prompt_fragments=prompt_fragments,
            tool_agent_model_calls=tool_agent_model_calls,
        )
        trace_store.add(
            request.session_id,
            "cost_recorded",
            cost_summary,
        )
        trace_store.add(
            request.session_id,
            "final_answer_generated",
            {
                "session_id": request.session_id,
                "intent": intent,
                "status": "success",
                "risk_level": risk_level,
                "used_model": model_answer.used_model,
                "reasoning_content_returned": bool(reasoning_content),
            },
        )

        return ChatResponse(
            session_id=request.session_id,
            answer=answer,
            citations=citations,
            tool_calls=tool_calls,
            clarification=clarification,
            reasoning_summary=[
                "Trace 记录的是公开执行摘要：Runtime Context、Context、Tool、RAG、Workflow/HITL、Hooks 和 Cost。",
                "tool_calls 与 citations 是可观察证据，不是 hidden CoT。",
                "评测视图会尝试展示主链路最终模型返回的 reasoning_content；系统提示词、密钥、隐私原文和内部堆栈不会写入公开 trace。",
            ],
            reasoning_content=reasoning_content,
            session_state={
                "agent_version": "lesson-41-final-rehearsal",
                "message_count": message_count,
                "intent": intent,
                "eligibility_query": stored.get("eligibility_query"),
                "product_query": stored.get("product_query"),
                "model": {
                    "route_planner": {
                        "used_model": route_result.used_model,
                        "model_name": route_result.model_name,
                        "fallback_reason": route_result.fallback_reason,
                        "prompt_fragments": route_result.prompt_fragments,
                    },
                    "final_answer": {
                        "used_model": model_answer.used_model,
                        "model_name": model_answer.model_name,
                        "fallback_reason": model_answer.fallback_reason,
                        "prompt_fragments": model_answer.prompt_fragments,
                    },
                },
                "prompt_registry": {
                    "schema_version": "prompt_registry_v1",
                    "selected_fragments": prompt_fragments,
                    "selected_fragment_ids": [fragment["name"] for fragment in prompt_fragments],
                    "prompt_body_exposed": False,
                },
                "route_plan": route_plan.model_dump(),
                "tool_calling": {
                    **tool_calling_state,
                    "clarification": clarification.model_dump() if clarification else None,
                },
                "mcp": mcp_binding,
                "frameworks": {
                    "langchain": {
                        "used": route_result.used_model or model_answer.used_model,
                        "route_chain": route_result.framework,
                        "final_answer_chain": model_answer.framework,
                        "prompt_registry": "prompts/prompt_registry.yml",
                        "selected_fragment_ids": [fragment["name"] for fragment in prompt_fragments],
                        "create_agent": bool(tool_calling_state.get("create_agent")),
                    },
                    "langgraph": {
                        "used": bool(workflow and workflow.get("used_langgraph")),
                        "graph_name": workflow.get("graph_name") if workflow else None,
                        "current_node": workflow.get("current_node") if workflow else None,
                        "node_history": workflow.get("node_history", []) if workflow else [],
                    },
                },
                "risk_level": risk_level,
                "next_action": next_action,
                "needs_human_approval": needs_human_approval,
                "runtime_context": runtime_context,
                "memory": memory,
                "context_report": context_report,
                "compression_report": compression_report,
                "hook_events": hooks.events,
                "hook_completion": hook_completion,
                "workflow": workflow,
                "rag": {
                    "low_confidence": intent == "low_confidence_query",
                    "hit_count": len(citations),
                    "citation_ids": [citation.metadata.get("policy_id") for citation in citations if citation.metadata],
                    "rerank_mode": rag_rerank["mode"] if rag_rerank else None,
                    "reranked_policy_ids": rag_rerank["policy_ids"] if rag_rerank else [],
                    "rerank_scores": rag_rerank["scores"] if rag_rerank else {},
                    "rerank_reasons": rag_rerank["reasons"] if rag_rerank else {},
                    "retrieval_mode": rag_retrieval.get("mode") if rag_retrieval else None,
                    "rewritten_query": (rag_retrieval.get("plan") or {}).get("rewritten_query") if rag_retrieval else None,
                    "index_version": rag_retrieval.get("index_version") if rag_retrieval else None,
                    "index_chunk_count": rag_retrieval.get("index_chunk_count") if rag_retrieval else 0,
                    "index_cache_hit": rag_retrieval.get("index_cache_hit") if rag_retrieval else False,
                    "retrieval_cache_hit": rag_retrieval.get("retrieval_cache_hit") if rag_retrieval else False,
                    "vector_policy_ids": rag_retrieval.get("vector_policy_ids", []) if rag_retrieval else [],
                    "keyword_policy_ids": rag_retrieval.get("keyword_policy_ids", []) if rag_retrieval else [],
                    "source_scores": rag_retrieval.get("source_scores", {}) if rag_retrieval else {},
                    "embedding": rag_retrieval.get("embedding") if rag_retrieval else None,
                },
                "degraded": degraded,
                "cost_summary": cost_summary,
                "trace": public_trace_summary(request.session_id),
                "next_gap": "第十幕用同一阶段 Agent 做大促总演习，并把证据整理成项目答辩表达。",
            },
        )

    def _call_product_tool(
        self,
        request: ChatRequest,
        hooks: HookManager,
        arguments: dict[str, Any],
        *,
        product_id: int | str | None = None,
        keyword: str | None = None,
        match_text: str | None = None,
    ) -> ToolCallTrace:
        """统一商品工具的 hooks 与 trace 记录，所有商品查询都经过这里。"""
        hooks.pre_tool_call("search_products", arguments, request.runtime_user_id)
        trace_store.add(
            request.session_id,
            "tool_started",
            {"session_id": request.session_id, "tool_name": "search_products"},
        )
        _products, call = search_products(keyword, product_id=product_id, match_text=match_text)
        hooks.post_tool_call(call)
        trace_store.add(
            request.session_id,
            "tool_finished",
            {
                "session_id": request.session_id,
                "tool_name": call.tool_name,
                "status": call.status,
                "risk_level": call.risk_level,
                "next_action": call.next_action,
            },
        )
        return call

    def _run_product_query(self, request: ChatRequest, memory: dict[str, Any], hooks: HookManager) -> dict[str, Any]:
        """商品对象定位：代码绑定编号，模型只在需要提取名称时参与搜索词生成。"""
        runtime = request.runtime_context or {}
        message = str(runtime.get("userMessage") or request.user_message)
        pstate = memory.get("product_query") or {}
        memory["product_query"] = pstate
        for key in ("product_id", "product_name", "pending_candidates", "original_question"):
            pstate.setdefault(key, None)
        keyword_present = has_product_term(message)
        model_available = self.langchain_tool_runner.can_call_model()
        resolution = resolve_product_target(
            message=message,
            page_product_id=runtime.get("relatedProductId"),
            product_state=pstate,
            keyword_present=keyword_present,
            model_available=model_available,
        )
        kind = resolution["kind"]
        question = pstate.get("original_question") if resolution.get("source") == "candidate" else message
        question = question or message
        outcome: dict[str, Any] = {
            "answer": "",
            "risk_level": "low",
            "next_action": "answer_user",
            "tool_calls": [],
            "citations": [],
            "verified_product_id": None,
            "verified_product_name": None,
            "degraded": False,
            "degradation_reason": None,
            "preserve_wording": False,
            "tool_calling_state": {
                "create_agent": False,
                "skip_reason": "code_bound_product_object",
                "available_tools": ["search_products"],
                "selected_tools": [],
                "message_types": [],
                "tool_message_count": 0,
                "fallback_used": False,
            },
            "prompt_fragments": [],
            "model_calls": 0,
            "rag_rerank": None,
            "rag_retrieval": None,
        }

        def _finish_product_call(call: ToolCallTrace) -> None:
            if call.status == "error" and call.error_type in {"timeout", "business_api_unavailable"}:
                outcome["degraded"] = True
                outcome["degradation_reason"] = call.error_type
            elif call.arguments.get("missing_fields"):
                outcome["preserve_wording"] = True

        status = "clarified"
        if kind == "clarify":
            outcome["answer"] = CLARIFY_MESSAGES[resolution["reason"]]
            outcome["next_action"] = "ask_clarification"
        elif kind == "end_pending":
            pstate["pending_candidates"] = None
            pstate["original_question"] = None
            outcome["answer"] = "好的，已结束这次商品选择，没有为你选定任何商品。其他办理不受影响。"
        elif kind == "reshow":
            outcome["answer"] = candidate_prompt(pstate["pending_candidates"])
            outcome["next_action"] = "ask_clarification"
        elif kind == "detail":
            product_id = resolution["product_id"]
            call = self._call_product_tool(request, hooks, {"product_id": product_id}, product_id=product_id)
            outcome["tool_calls"].append(call)
            _finish_product_call(call)
            if call.status == "success":
                status = "answered"
                pstate["pending_candidates"] = None
                pstate["original_question"] = None
                outcome["answer"] = call.output_summary
                outcome["verified_product_id"] = str(product_id)
                outcome["verified_product_name"] = str(call.arguments.get("product_name") or "") or None
            else:
                status = call.error_type or "error"
                if call.error_type == "product_not_found":
                    # 已选编号不存在时清掉失效对象，不能默选其他款兜底。
                    pstate["pending_candidates"] = None
                    pstate["original_question"] = None
                    if pstate.get("product_id") is not None and str(pstate["product_id"]) == str(product_id):
                        pstate["product_id"] = None
                        pstate["product_name"] = None
                outcome["answer"] = call.output_summary
                outcome["next_action"] = call.next_action or "answer_user"
                outcome["risk_level"] = "medium"
        else:
            # 新查询先作废旧候选，避免旧序号复活。
            pstate["pending_candidates"] = None
            pstate["original_question"] = None
            call = None
            if model_available:
                tool_result = self.langchain_tool_runner.run(
                    request=request,
                    intent="product_query",
                    required_tools=["search_products"],
                    hooks=hooks,
                    expected_product_keyword=product_query_keyword(message) if keyword_present else None,
                )
                outcome["tool_calling_state"] = tool_result.state
                outcome["prompt_fragments"] = tool_result.prompt_fragments
                outcome["model_calls"] = tool_result.model_calls
                outcome["tool_calls"].extend(tool_result.tool_calls)
                trace_store.add(
                    request.session_id,
                    "langchain_tool_agent_completed",
                    {
                        "session_id": request.session_id,
                        "create_agent": tool_result.state.get("create_agent"),
                        "selected_tools": tool_result.state.get("selected_tools", []),
                        "message_types": tool_result.state.get("message_types", []),
                        "fallback_used": tool_result.state.get("fallback_used"),
                        "status": "success" if tool_result.executed else "fallback",
                    },
                )
                call = next(
                    (item for item in tool_result.tool_calls
                     if item.tool_name == "search_products" and item.error_type != "route_plan_argument_mismatch"),
                    None,
                )
            if call is None:
                keyword = product_query_keyword(message)
                call = self._call_product_tool(request, hooks, {"keyword": keyword}, keyword=keyword, match_text=message)
                outcome["tool_calls"].append(call)
            if call.status == "success" and call.candidates:
                status = "candidates"
                pstate["pending_candidates"] = [
                    {"id": item["value"], "name": item["label"]} for item in call.candidates
                ]
                pstate["original_question"] = message
                outcome["answer"] = call.clarification_prompt or call.output_summary
                outcome["next_action"] = "ask_clarification"
            elif call.status == "success" and call.next_action == "ask_clarification":
                status = "no_match"
                outcome["answer"] = call.output_summary
                outcome["next_action"] = "ask_clarification"
            elif call.status == "success":
                status = "answered"
                outcome["answer"] = call.output_summary
                outcome["verified_product_id"] = str(call.arguments.get("product_id") or "") or None
                outcome["verified_product_name"] = str(call.arguments.get("product_name") or "") or None
            else:
                status = call.error_type or "error"
                outcome["answer"] = call.output_summary
                outcome["next_action"] = call.next_action or "answer_user"
                outcome["risk_level"] = "medium"
            _finish_product_call(call)
        # 单商品事实成立后活动类问题才补 RAG 通用规则；候选澄清阶段不补。
        if status == "answered" and any(term in question for term in ["活动", "优惠", "满减", "会员"]):
            knowledge_result = promotion_policy_result(request.session_id, question)
            record_trace_events(request.session_id, knowledge_result.trace_events)
            outcome["citations"] = knowledge_result.citations
            outcome["rag_retrieval"] = knowledge_result.retrieval_debug
            outcome["rag_rerank"] = knowledge_result.rerank
            policy_answer = (
                f"平台通用规则：{knowledge_result.answer} "
                "这不代表该规则一定适用于当前商品活动，具体组合以商品页和结算页为准。"
                if knowledge_result.citations
                else "活动规则请以商品页与结算页为准。"
            )
            outcome["answer"] = f"{outcome['answer']} {policy_answer}"
        trace_store.add(
            request.session_id,
            "product_target_resolved",
            {
                "session_id": request.session_id,
                "resolution": kind,
                "source": resolution.get("source"),
                "product_id": outcome["verified_product_id"]
                    or (str(resolution["product_id"]) if kind == "detail" else None),
                "status": status,
                "pending_candidates": [item["name"] for item in pstate.get("pending_candidates") or []],
                "model_available": model_available,
            },
        )
        return outcome

    def _compose_final_answer(
        self,
        *,
        request: ChatRequest,
        intent: Intent,
        answer: str,
        risk_level: str,
        next_action: str,
        tool_calls: list[ToolCallTrace],
        citations: list[Citation],
        workflow: dict[str, Any] | None,
        cache_hit: bool,
        degraded: bool,
        skip_final_model: bool = False,
        preserve_tool_wording: bool = False,
        enable_reasoning: bool = False,
        context_report: dict[str, Any],
    ) -> FinalAnswerModelResult:
        """让真实模型生成最终话术，但安全、低置信、降级和缓存命中保留确定性边界。"""
        skip_reason: str | None = None
        if skip_final_model:
            skip_reason = "runtime_context_direct_answer"
        elif degraded:
            skip_reason = "degraded_path"
        elif preserve_tool_wording:
            skip_reason = "verified_tool_wording"
        elif next_action == "ask_clarification":
            skip_reason = "clarification_required"
        elif intent in {"security_request", "low_confidence_query"}:
            skip_reason = "safety_or_low_confidence_boundary"
        elif intent == "unknown":
            skip_reason = "unclear_task"
        elif cache_hit:
            skip_reason = "common_hit_cache"
        if skip_reason:
            result = FinalAnswerModelResult(answer=answer, fallback_reason=skip_reason)
            trace_store.add(
                request.session_id,
                "model_answer_skipped",
                {"session_id": request.session_id, "intent": intent, "reason": skip_reason},
            )
            return result

        result = self.answer_model_client.compose_answer(
            request=request,
            intent=intent,
            deterministic_answer=answer,
            risk_level=risk_level,
            next_action=next_action,
            tool_calls=tool_calls,
            citations=citations,
            workflow=workflow,
            enable_reasoning=enable_reasoning,
            model_context=context_report["model_context"],
        )
        trace_store.add(
            request.session_id,
            "model_answer_generated",
            {
                "session_id": request.session_id,
                "intent": intent,
                "used_model": result.used_model,
                "model_name": result.model_name,
                "fallback_reason": result.fallback_reason,
            },
        )
        return result

    def resume(self, request: ChatResumeRequest, reviewer: TrustedReviewer | None = None) -> ChatResumeResponse:
        """恢复暂停的 HITL workflow。调用方必须传入服务端已核实的审核身份。"""
        return resume_from_checkpoint(request, trace_store, reviewer)
