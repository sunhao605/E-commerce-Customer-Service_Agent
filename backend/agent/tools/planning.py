"""轻量规划工具。这里展示课程版路由、订单号抽取和 token 估算。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.schemas import *

_POLICY_CUES = ("政策", "规则", "条件", "流程", "怎么寄", "如何寄", "了解一下", "只是咨询", "只了解", "咨询一下", "寄回")
_ELIGIBILITY_CUES = ("能退", "可退", "能否退", "能不能退", "还能退", "可以退", "资格", "能申请退款", "能申请退货")
_APPLY_NEGATIONS = ("不要申请", "先别申请", "暂不申请", "不申请", "别申请")


def _without_apply_negation(user_message: str) -> str:
    text = user_message
    for phrase in _APPLY_NEGATIONS:
        text = text.replace(phrase, "")
    return text


def _explicit_apply(user_message: str) -> bool:
    """明确要办理。否定申请（不申请、先别申请）不算。"""
    text = _without_apply_negation(user_message)
    return bool(re.search(r"(?:我要|我想|帮我|给我|现在|直接|那就)?(?:申请|办理|退款吧|退货吧)", text))


def read_only_after_sale_intent(user_message: str) -> Intent | None:
    """模型故障时把通用政策咨询留在只读标签。"""
    if _explicit_apply(user_message) or is_specific_eligibility_question(user_message):
        return None
    policy = any(term in user_message for term in _POLICY_CUES)
    eligibility = any(term in user_message for term in _ELIGIBILITY_CUES)
    if not policy and not eligibility:
        return None
    if any(term in user_message for term in ("退货", "七天无理由", "寄回")) or (
        eligibility and "退款" not in user_message and "退钱" not in user_message
    ):
        return "return_policy_query"
    if any(term in user_message for term in ("退款", "退钱", "取消订单")):
        return "refund_policy_query"
    return None


def is_specific_eligibility_question(user_message: str) -> bool:
    """资格问题及条件式办理先查资格，不把“帮我看看”当成申请。"""
    if not any(term in user_message for term in _ELIGIBILITY_CUES):
        return False
    specific = extract_order_id(user_message) is not None or any(
        term in user_message for term in ("这笔", "这单", "这个订单", "这张订单")
    ) or "如果" in user_message or bool(re.search(r"查.*(?:退款|退货).*资格", user_message))
    return specific and (not _explicit_apply(user_message) or any(
        term in user_message for term in (*_APPLY_NEGATIONS, "只查", "能不能", "能否", "如果", "吗")
    ))


def classify_intent(user_message: str) -> Intent:
    """用可读规则做总演习路由，展示 Tool、RAG、Workflow、降级和安全分流入口。"""
    if any(term in user_message for term in ["SERVICE_TIMEOUT", "服务抽风", "工具超时", "接口不可用"]):
        return "degradation_request"
    if any(term in user_message for term in ["系统提示词", "hidden reasoning", "隐藏推理", "工具 schema", "内部策略"]):
        return "security_request"
    if any(term in user_message for term in ["退款", "退货"]) and any(term in user_message for term in ["进度", "状态", "处理到哪", "什么时候到账"]):
        return "refund_status_query"
    if is_specific_eligibility_question(user_message):
        return "after_sale_eligibility_query"
    read_only = read_only_after_sale_intent(user_message)
    if read_only:
        return read_only
    if any(term in user_message for term in ["退货", "七天无理由"]):
        return "return_request"
    if any(term in user_message for term in ["退款", "退钱", "取消订单"]):
        return "refund_request"
    if any(term in user_message for term in ["订单", "物流", "快递"]):
        return "order_query"
    if "发票" in user_message:
        return "invoice_query"
    if any(term in user_message for term in ["火星会员", "隐藏券", "不存在的活动", "未知活动"]):
        return "low_confidence_query"
    if any(term in user_message for term in ["活动", "满减", "会员券", "优惠券", "会员规则", "大促"]):
        if any(term in user_message for term in ["商品", "折叠椅", "露营椅", "折叠桌", "野餐垫", "帐篷", "天幕", "营地灯", "头灯", "背包", "登山杖", "睡袋", "收纳车", "收纳柜", "炊具", "防水袋", "营地旗", "营地标识", "库存", "价格", "多少钱", "有货"]):
            return "product_query"
        return "promotion_query"
    if any(term in user_message for term in ["商品", "折叠椅", "露营椅", "折叠桌", "野餐垫", "帐篷", "天幕", "营地灯", "头灯", "背包", "登山杖", "睡袋", "收纳车", "收纳柜", "炊具", "防水袋", "营地旗", "营地标识", "库存", "价格", "多少钱", "有货", "推荐"]):
        return "product_query"
    return "general_chat"


_SLOT_REPLY = re.compile(
    r"^(?:我选择\s*)?(?:SO[A-Za-z0-9_-]{6,}|ORD\d{4,}|第[0-9一二三四五六七八九十]{1,3}笔)[。！!？?\s]*$",
    re.IGNORECASE,
)


def is_slot_reply(user_message: str) -> bool:
    """本句只补订单号或候选序号。供模型失败时保留待办，不代替模型判断改口。"""
    return bool(_SLOT_REPLY.match(user_message.strip()))


def format_open_task(open_task: dict[str, Any] | None) -> str | None:
    """把未完成待办收成短状态。没有待办时不附加，分类仍只看本句。"""
    if not open_task or not open_task.get("intent"):
        return None
    candidates = open_task.get("candidates") or []
    shown = "；".join(f"{index}. {item.get('value')}" for index, item in enumerate(candidates, start=1)) or "无"
    return (
        "未完成待办（系统状态，不是用户原话）：\n"
        f"任务：{open_task['intent']}\n"
        f"缺少：{open_task.get('pending_slot') or '无'}\n"
        f"已展示候选：{shown}"
    )


def extract_order_id(user_message: str) -> str | None:
    """从用户问题中抽取荒野户外订单号，供工具调用前参数校验。"""
    match = re.search(r"\b(?:SO[A-Za-z0-9_-]{6,}|ORD\d{4,})\b", user_message, flags=re.IGNORECASE)
    return match.group(0) if match else None


def extract_return_reason(user_message: str) -> str | None:
    """无理由是申请类型，无需另填原因；明确的其他售后原因保留原义。"""
    reason_terms = ("质量问题", "商品破损", "发错货", "少件", "与描述不符")
    reason = next((term for term in reason_terms if term in user_message), None)
    return reason or ("七天无理由" if "退货" in user_message or "七天无理由" in user_message else None)


def build_route_plan(
    *,
    intent: Intent,
    user_message: str,
    order_id: str | None,
    model_used: bool,
) -> RoutePlan:
    """把意图收敛成白名单 RoutePlan，模型不能自由增加工具或高风险动作。"""
    candidate_catalog = {
        "get_order_detail": ToolCandidate(
            name="get_order_detail",
            domain="order",
            risk_level="low",
            reason="读取当前用户订单事实，不执行业务写操作。",
        ),
        "get_order_logistics": ToolCandidate(
            name="get_order_logistics",
            domain="logistics",
            risk_level="low",
            reason="读取当前用户订单及物流状态。",
        ),
        "get_refund_status": ToolCandidate(
            name="get_refund_status",
            domain="after_sale",
            risk_level="low",
            reason="只读查询当前用户订单的售后申请状态，不创建退款申请。",
        ),
        "get_after_sale_eligibility": ToolCandidate(
            name="get_after_sale_eligibility",
            domain="after_sale",
            risk_level="low",
            reason="复用商城严格规则，只读核验本人订单的售后资格。",
        ),
        "search_products": ToolCandidate(
            name="search_products",
            domain="product",
            risk_level="low",
            reason="查询商品价格、库存和活动等实时事实。",
        ),
    }
    required_tools: list[str] = []
    knowledge_domains: list[str] = []
    risk_level: RiskLevel = "low"
    requires_workflow = False
    if intent == "order_query":
        required_tools = ["get_order_logistics"]
    elif intent == "refund_status_query":
        required_tools = ["get_refund_status"]
    elif intent == "after_sale_eligibility_query":
        required_tools = ["get_after_sale_eligibility"]
    elif intent == "refund_request":
        required_tools = ["get_order_detail"]
        knowledge_domains = ["after_sale_policy"]
        risk_level = "high"
        requires_workflow = True
    elif intent == "return_request":
        required_tools = ["get_order_detail"]
        knowledge_domains = ["received_return_policy"]
        risk_level = "high"
        requires_workflow = True
    elif intent == "refund_policy_query":
        knowledge_domains = ["after_sale_policy"]
    elif intent == "return_policy_query":
        knowledge_domains = ["received_return_policy"]
    elif intent == "product_query":
        required_tools = ["search_products"]
        knowledge_domains = ["promotion_and_member_policy"] if any(term in user_message for term in ["活动", "优惠", "满减", "会员"]) else []
    elif intent in {"invoice_query", "promotion_query", "low_confidence_query"}:
        knowledge_domains = ["invoice"] if intent == "invoice_query" else ["promotion_and_member_policy"]
    elif intent in {"security_request", "degradation_request"}:
        risk_level = "high" if intent == "security_request" else "medium"

    # 缺少订单号时保留候选工具，但不允许模型凭空生成参数并执行。
    order_bound_tools = {"get_order_detail", "get_order_logistics", "get_refund_status", "get_after_sale_eligibility"}
    executable_tools = required_tools if order_id or not any(name in order_bound_tools for name in required_tools) else []
    return RoutePlan(
        intent=intent,
        needs_rag=bool(knowledge_domains),
        needs_business_tools=bool(required_tools),
        required_tools=executable_tools,
        tool_candidates=[candidate_catalog[name] for name in required_tools],
        knowledge_domains=knowledge_domains,
        entity_refs=[order_id] if order_id else [],
        risk_level=risk_level,
        requires_workflow=requires_workflow,
        confidence=0.9 if model_used else 0.75,
        source="llm_with_policy_constraints" if model_used else "deterministic_fallback",
        fallback_policy="ask_order_id" if required_tools and not order_id and any(name in order_bound_tools for name in required_tools) else "safe_deterministic_path",
    )


def build_order_clarification(request: ChatRequest, route_plan: RoutePlan) -> ClarificationRequest | None:
    """后端根据 RoutePlan 必填参数和可信 Runtime Context 生成候选，不让模型代选订单。"""
    if route_plan.fallback_policy != "ask_order_id" or not route_plan.tool_candidates:
        return None
    orders = (request.runtime_context or {}).get("currentUserOrders", [])
    candidates: list[ClarificationCandidate] = []
    for order in orders:
        if str(order.get("userId")) != request.runtime_user_id:
            continue
        order_id = str(order.get("orderNo") or "").strip()
        if not order_id:
            continue
        items = order.get("items") or []
        product_names = "、".join(str(item.get("productName")) for item in items[:2] if item.get("productName"))
        candidates.append(
            ClarificationCandidate(
                value=order_id,
                label=order_id,
                hint=product_names or "当前账号订单",
            )
        )
    action = "退款" if route_plan.intent == "refund_request" else "查询"
    return ClarificationRequest(
        clarification_field="order_id",
        message=f"你要{action}哪一个订单？请选择订单号，或直接补充订单号。",
        candidates=candidates,
    )


def estimate_tokens(text: str) -> int:
    """用近似 token 估算服务成本治理和上下文预算展示。"""
    return max(1, len(text) // 2)
