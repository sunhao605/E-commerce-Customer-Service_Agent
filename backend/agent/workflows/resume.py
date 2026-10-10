"""HITL 恢复与 checkpoint 层，保护高风险售后动作的恢复、复核和幂等。"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Literal

import httpx

from api.schemas import *
from settings import TRACE_SCHEMA_VERSION, ecommerce_base_url, load_course_env
from agent.ecommerce_client import order_fact_from_ecommerce
from agent.trace import public_trace_summary
from agent.state import SUBMITTED_ACTIONS, WORKFLOW_CHECKPOINTS
from agent.tools.runtime_context import logistics_status_from_order, order_no, order_status

# ponytail: one process-wide lock. Ceiling: decisions are serialized only inside this process.
# Upgrade: persist the checkpoint and lock the application row in the database.
_RESUME_LOCK = threading.Lock()

UNAVAILABLE_ANSWER = "无法核实最新订单，本次未继续处理。"
UNVERIFIED_REVIEWER_ANSWER = "无法核实审核身份，本次未继续处理。"
CONFLICT_ANSWER = "该申请已处理，原决定保持不变，不能改为相反结果。"
FACT_LABELS = {
    "PENDING_SHIPMENT": "待发货",
    "NOT_SHIPPED": "未发货",
    "SHIPPED": "已发货",
    "IN_TRANSIT": "运输中",
    "DELIVERED": "已送达",
    "SIGNED": "已签收",
    "CANCELED": "已取消",
    "CANCELLED": "已取消",
    "REFUNDED": "已退款",
}
FIELD_LABELS = {"order_status": "订单状态", "logistics_status": "物流状态"}


@dataclass(frozen=True)
class TrustedReviewer:
    """服务端已核实的审核身份。请求体里的 reviewer 字段不能构造这个对象。"""

    reviewer_id: str
    reviewer_role: str
    source: Literal["admin_session", "offline_simulated"]


SIMULATED_REVIEWER = TrustedReviewer(
    "course-simulated-reviewer",
    "simulated_after_sale_manager",
    "offline_simulated",
)


def build_resume_token(session_id: str, workflow_id: str, order_id: str | None) -> str:
    """生成 HITL 恢复令牌，说明高风险 workflow 不能靠普通聊天继续执行。"""
    return f"resume-{session_id}-{workflow_id}-{order_id or 'missing'}"


def freeze_workflow_fields(request: ChatRequest, order: dict[str, Any] | None) -> dict[str, Any]:
    """冻结审批前的关键业务事实，恢复时用来检查订单状态是否漂移。"""
    return {
        "runtime_user_id": request.runtime_user_id,
        "order_id": order_no(order) if order else None,
        "order_status": order_status(order) if order else None,
        "logistics_status": logistics_status_from_order(order) if order else None,
    }


def _fact_label(value: Any) -> str:
    text = "" if value is None else str(value)
    return FACT_LABELS.get(text, text or "空")


def _drift_message(mismatches: dict[str, Any]) -> str:
    parts = [
        f"{FIELD_LABELS.get(field, field)}由{_fact_label(diff.get('frozen'))}变为{_fact_label(diff.get('current'))}"
        for field, diff in mismatches.items()
    ]
    change = "，".join(parts) if parts else "关键状态已变化"
    return f"订单关键状态已变化（{change}），原未发货退款流程已停止，本次未批准。"


def _unavailable(detail: str, order_id: str | None) -> dict[str, Any]:
    mismatches = {} if detail == "missing_status" else {"order_id": {"frozen": order_id, "current": None}}
    return {
        "passed": False,
        "reason": "latest_order_unavailable",
        "detail": detail,
        "mismatches": mismatches,
        "fact_source": None,
        "simulated": False,
        "real_business_recheck": False,
        "message": UNAVAILABLE_ANSWER,
    }


def _status_pair(order: dict[str, Any]) -> tuple[str, str] | None:
    has_order_status = any(
        order.get(key)
        for key in ("fulfillmentStatus", "fulfillment_status", "orderStatus", "order_status", "status")
    )
    has_logistics = has_order_status or any(order.get(key) for key in ("logisticsStatus", "logistics_status"))
    status = order_status(order) if has_order_status else ""
    logistics = logistics_status_from_order(order) if has_logistics else ""
    if not status or not logistics or logistics == "UNKNOWN":
        return None
    return status, logistics


def business_recheck(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """恢复前核对本人订单的最新事实。查不到、查询失败或状态字段缺失时不能用旧快照放行。"""
    frozen = checkpoint["frozen_fields"]
    order_id = frozen.get("order_id")
    user_id = str(frozen.get("runtime_user_id") or "")
    if not order_id or not user_id:
        return _unavailable("missing_order_id" if not order_id else "missing_user", order_id)
    try:
        current_order = order_fact_from_ecommerce(order_id, user_id)
    except Exception:
        return _unavailable("query_failed", order_id)
    if not isinstance(current_order, dict):
        return _unavailable("order_not_found", order_id)
    pair = _status_pair(current_order)
    if pair is None:
        return _unavailable("missing_status", order_id)
    status, logistics = pair
    mismatches: dict[str, Any] = {}
    current_values = {"order_status": status, "logistics_status": logistics}
    for field in ("order_status", "logistics_status"):
        if frozen.get(field) != current_values[field]:
            mismatches[field] = {"frozen": frozen.get(field), "current": current_values[field]}
    source = current_order.get("_fact_source")
    simulated = source == "course_seed_mirror"
    if mismatches:
        return {
            "passed": False,
            "reason": "business_fact_drift",
            "detail": "status_changed",
            "mismatches": mismatches,
            "fact_source": source,
            "simulated": simulated,
            "real_business_recheck": False,
            "message": _drift_message(mismatches),
        }
    return {
        "passed": True,
        "reason": None,
        "detail": None,
        "mismatches": {},
        "fact_source": source,
        "simulated": simulated,
        "real_business_recheck": not simulated,
        "message": "离线模拟复核" if simulated else None,
    }


def build_refund_workflow_checkpoint(
    *,
    request: ChatRequest,
    order: dict[str, Any] | None,
    order_id: str | None,
    citations: list[Citation],
) -> dict[str, Any]:
    """组装并保存未发货退款 HITL checkpoint，Agent 只关心 workflow 摘要。"""
    workflow = {
        "workflow_id": f"wf-{request.session_id}",
        "workflow_type": "unshipped_refund",
        "status": "paused",
        "pending_action": "require_approval",
        "order_id": order_no(order) if order else order_id,
        "resume_token": build_resume_token(request.session_id, f"wf-{request.session_id}", order_id),
        "approval_id": f"appr-{request.session_id}",
        "idempotency_key": f"hitl:{request.session_id}:{order_id}",
        "frozen_fields": freeze_workflow_fields(request, order),
    }
    WORKFLOW_CHECKPOINTS[(request.session_id, workflow["workflow_id"])] = {
        "workflow": workflow,
        "frozen_fields": workflow["frozen_fields"],
        "resume_token": workflow["resume_token"],
        "idempotency_key": workflow["idempotency_key"],
        "citations": [citation.model_dump() for citation in citations],
        "order_snapshot": order,
    }
    return workflow


def identify_session(cookie_header: str | None) -> tuple[str, TrustedReviewer | None]:
    """用商城现有登录会话识别调用者。只接受管理员；客户会话明确拒绝。"""
    if not cookie_header:
        return "unknown", None
    load_course_env()
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get(
                f"{ecommerce_base_url()}/api/auth/me",
                headers={"Cookie": cookie_header},
            )
        payload = response.json()
    except Exception:
        return "unknown", None
    if response.status_code == 401:
        return "unknown", None
    data = payload.get("data") if isinstance(payload, dict) else None
    if response.status_code != 200 or not isinstance(data, dict) or payload.get("success") is not True:
        return "unknown", None
    username = str(data.get("username") or "").strip()
    if data.get("role") == "ADMIN" and username:
        return "admin", TrustedReviewer(username, "ADMIN", "admin_session")
    if data.get("role"):
        return "rejected", None
    return "unknown", None


def resolve_trusted_reviewer(
    cookie_header: str | None,
    *,
    identify: Callable[[str | None], tuple[str, TrustedReviewer | None]] | None = None,
) -> TrustedReviewer | None:
    """决定谁可以审核。自填角色无效；已确认的客户会话即使开了离线开关也不能审。

    ponytail: AGENT_COURSE_SIMULATED_REVIEWER=1 让整个进程共用一个模拟审核员。
    Ceiling: 能调用这个进程、且没有客户会话的人都会变成该审核员。
    Upgrade: 去掉这个开关，只保留商城管理员会话。
    """
    kind, reviewer = (identify or identify_session)(cookie_header)
    if kind == "admin" and reviewer is not None:
        return reviewer
    if kind == "rejected":
        return None
    if os.getenv("AGENT_COURSE_SIMULATED_REVIEWER") == "1":
        return SIMULATED_REVIEWER
    return None


def blocked_resume_response(
    request: ChatResumeRequest,
    reason: str,
    trace_store: Any,
    business_recheck_payload: dict[str, Any] | None = None,
    *,
    answer: str | None = None,
    workflow: dict[str, Any] | None = None,
) -> ChatResumeResponse:
    """构造被阻断的恢复响应。不写入终态，也不新增模拟提交。"""
    recheck = business_recheck_payload or {"passed": False, "reason": reason, "mismatches": {}}
    if answer is None:
        if reason == "checkpoint_not_found":
            answer = "审批恢复没有通过校验，恢复凭证已失效，不能继续执行高风险售后动作。"
        elif reason == "reviewer_unverified":
            answer = UNVERIFIED_REVIEWER_ANSWER
        elif reason == "decision_conflict":
            answer = CONFLICT_ANSWER
        else:
            answer = str(recheck.get("message") or "审批恢复没有通过校验，不能继续执行高风险售后动作。")
    return ChatResumeResponse(
        session_id=request.session_id,
        workflow_id=request.workflow_id,
        status="blocked",
        answer=answer,
        resume_result={
            "accepted": False,
            "decision": request.decision,
            "idempotent_replay": False,
            "request_id": None,
            "reason": reason,
        },
        workflow=workflow,
        business_recheck=recheck,
        session_state={
            "agent_version": "lesson-41-final-rehearsal",
            "workflow": workflow,
            "trace": {
                "schema_version": TRACE_SCHEMA_VERSION,
                "event_count": len(trace_store.list(request.session_id)),
                "public_trace_only": True,
            },
        },
    )


def _review_note(reviewer: TrustedReviewer, recheck: dict[str, Any]) -> str:
    notes = []
    if reviewer.source == "offline_simulated":
        notes.append("本次为离线模拟审核，不是商城管理员登录后的审批。")
    if recheck.get("simulated"):
        notes.append("订单核对使用了离线模拟复核。")
    return "".join(notes)


def _cost_summary(workflow_id: str, status: str) -> dict[str, Any]:
    return {
        "schema_version": "cost_summary_v1",
        "path_type": "hitl_resume_path",
        "model_calls": {"route_planner": 0, "final_answer": 0, "extra_reasoning": 0},
        "tool_call_count": 0,
        "business_tool_call_count": 0,
        "rag": {"needs_rag": False, "hit_count": 0, "cache_hit": False},
        "workflow": {"used_langgraph": True, "workflow_id": workflow_id, "hitl_required": False, "status": status},
        "safety_boundary": {
            "cost_control_does_not_skip_business_facts": True,
            "cost_control_does_not_skip_hitl": True,
            "not_finops_or_billing_system": True,
        },
    }


def _resume_response(
    request: ChatResumeRequest,
    *,
    status: Literal["completed", "paused", "rejected", "blocked"],
    answer: str,
    workflow: dict[str, Any],
    recheck: dict[str, Any],
    accepted: bool,
    reason: str,
    replay: bool,
    request_id: str | None,
    reviewer: TrustedReviewer,
    trace_store: Any,
    record_trace: bool,
) -> ChatResumeResponse:
    if record_trace:
        trace_store.add(
            request.session_id,
            "workflow_resumed",
            {
                "session_id": request.session_id,
                "workflow_id": request.workflow_id,
                "pending_action": workflow["pending_action"],
                "status": status,
            },
        )
        trace_store.add(
            request.session_id,
            "human_approval_resolved",
            {
                "session_id": request.session_id,
                "workflow_id": request.workflow_id,
                "decision": request.decision,
                "reviewer_role": reviewer.reviewer_role,
                "status": status,
            },
        )
        trace_store.add(request.session_id, "cost_recorded", _cost_summary(request.workflow_id, status))
    cost_summary = _cost_summary(request.workflow_id, status)
    return ChatResumeResponse(
        session_id=request.session_id,
        workflow_id=request.workflow_id,
        status=status,
        answer=answer,
        resume_result={
            "accepted": accepted,
            "decision": request.decision,
            "idempotent_replay": replay,
            "request_id": request_id,
            "reason": reason,
            "reviewer_id": reviewer.reviewer_id,
            "review_mode": reviewer.source,
        },
        workflow=workflow,
        business_recheck=recheck,
        session_state={
            "agent_version": "lesson-41-final-rehearsal",
            "workflow": workflow,
            "cost_summary": cost_summary,
            "trace": public_trace_summary(request.session_id),
        },
    )


def _apply_decision(
    request: ChatResumeRequest,
    checkpoint: dict[str, Any],
    reviewer: TrustedReviewer,
    recheck: dict[str, Any],
    trace_store: Any,
) -> ChatResumeResponse:
    workflow = checkpoint["workflow"]
    workflow["reviewer_id"] = reviewer.reviewer_id
    workflow["reviewer_role"] = reviewer.reviewer_role
    workflow["review_mode"] = reviewer.source
    note = _review_note(reviewer, recheck)
    if request.decision == "approved":
        workflow["status"] = "completed"
        workflow["pending_action"] = "approval_accepted"
        answer = "售后审核员已批准模拟退款申请，系统记录审批通过；本演示环境不执行真实资金退款。" + note
        status: Literal["completed", "paused", "rejected"] = "completed"
        request_id = SUBMITTED_ACTIONS.setdefault(
            checkpoint["idempotency_key"],
            {"request_id": f"refund-{request.workflow_id}"},
        )["request_id"]
        reason = "approval_recorded"
    elif request.decision == "rejected":
        workflow["status"] = "rejected"
        workflow["pending_action"] = "approval_rejected"
        answer = "售后审核员已拒绝本次模拟退款申请，Agent 只能把结果告知用户，不能绕过人工审批。" + note
        status = "rejected"
        request_id = None
        reason = "rejection_recorded"
    else:
        workflow["status"] = "paused"
        workflow["pending_action"] = "need_more_info"
        answer = "售后审核员要求补充信息，workflow 继续暂停，等待用户或客服补齐材料。" + note
        status = "paused"
        request_id = None
        reason = "need_more_info_recorded"
    if status != "paused":
        checkpoint["decision_record"] = {
            "decision": request.decision,
            "reviewer_id": reviewer.reviewer_id,
            "reviewer_role": reviewer.reviewer_role,
            "review_mode": reviewer.source,
            "status": status,
            "request_id": request_id,
            "answer": answer,
            "recheck": recheck,
        }
    return _resume_response(
        request,
        status=status,
        answer=answer,
        workflow=workflow,
        recheck=recheck,
        accepted=True,
        reason=reason,
        replay=False,
        request_id=request_id,
        reviewer=reviewer,
        trace_store=trace_store,
        record_trace=True,
    )


def resume_from_checkpoint(
    request: ChatResumeRequest,
    trace_store: Any,
    reviewer: TrustedReviewer | None = None,
) -> ChatResumeResponse:
    """恢复暂停的 HITL workflow。身份和凭证通过后，才核对事实并写入同一份检查点。"""
    if not isinstance(reviewer, TrustedReviewer) or reviewer.source not in {"admin_session", "offline_simulated"}:
        return blocked_resume_response(request, "reviewer_unverified", trace_store)
    key = (request.session_id, request.workflow_id)
    with _RESUME_LOCK:
        checkpoint = WORKFLOW_CHECKPOINTS.get(key)
        if checkpoint is None:
            return blocked_resume_response(request, "checkpoint_not_found", trace_store)
        if checkpoint["resume_token"] != request.resume_token:
            return blocked_resume_response(request, "invalid_resume_token", trace_store)
        record = checkpoint.get("decision_record")
        if record and record.get("status") in {"completed", "rejected"}:
            if request.decision == record["decision"]:
                stored_reviewer = TrustedReviewer(
                    record["reviewer_id"], record["reviewer_role"], record["review_mode"]
                )
                return _resume_response(
                    request,
                    status=record["status"],
                    answer=record["answer"],
                    workflow=checkpoint["workflow"],
                    recheck=record["recheck"],
                    accepted=True,
                    reason="approval_recorded" if record["decision"] == "approved" else "rejection_recorded",
                    replay=True,
                    request_id=record.get("request_id"),
                    reviewer=stored_reviewer,
                    trace_store=trace_store,
                    record_trace=False,
                )
            return blocked_resume_response(
                request,
                "decision_conflict",
                trace_store,
                workflow=checkpoint["workflow"],
            )
        recheck = business_recheck(checkpoint)
        if not recheck["passed"]:
            return blocked_resume_response(
                request,
                recheck["reason"],
                trace_store,
                business_recheck_payload=recheck,
                workflow=checkpoint["workflow"],
            )
        return _apply_decision(request, checkpoint, reviewer, recheck, trace_store)
