"""业务工具执行层。这里封装订单详情和物流查询，并返回可观察 ToolCallTrace。"""

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
from agent.ecommerce_client import after_sale_requests_from_ecommerce, ecommerce_get, has_product_term, order_fact_from_ecommerce, product_detail_from_ecommerce, product_query_keyword, products_from_ecommerce
from agent.product_query import MAX_SHOWN_CANDIDATES, candidate_prompt, normalize_product_text, product_name, unique_name_match
from agent.tools.runtime_context import *

def get_after_sale_eligibility(order_id: str | None, runtime_user_id: str) -> tuple[dict[str, Any] | None, ToolCallTrace]:
    """资格仅来自商城严格规则，不回退到课程镜像或页面摘要。"""
    arguments = {"order_id": order_id, "fact_source": "business_api"}
    result = None
    error = "business_api_unavailable"
    summary = "暂时无法核实这笔订单的售后资格，请稍后重试。本轮没有准备申请。"
    if not order_id:
        error, summary = "missing_order_id", "请补充本人的订单号，这轮只查询资格，不准备申请。"
    else:
        try:
            result = ecommerce_get(f"/api/orders/{order_id}/after-sale-eligibility", delegated_user_id=runtime_user_id)
            if not isinstance(result, dict) or result.get("orderNo") != order_id or result.get("status") not in {
                "ELIGIBLE", "INELIGIBLE", "UNKNOWN"
            }:
                result = None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 404}:
                error, summary = "order_unavailable", "无法查询该订单，请确认订单号且订单属于当前账号。本轮没有准备申请。"
        except (httpx.HTTPError, ValueError):
            pass
    if result:
        state = result["status"]
        if state == "ELIGIBLE" and result.get("applicationType") in {"REFUND", "RETURN"} and result.get("amount") is not None:
            returning = result["applicationType"] == "RETURN"
            label = "七天无理由整单退货退款" if returning else "未发货整单退款"
            apply = "申请七天无理由退货" if returning else "申请退款"
            summary = f"按当前查询结果，订单 {order_id} 可以申请{label}，金额 {result['amount']} 元。{result.get('reason') or ''}如需办理，请明确回复‘{apply}’。这轮只查资格，没有准备申请；可以申请不代表审核通过。"
        else:
            prefix = "暂时无法核实" if state in {"UNKNOWN", "ELIGIBLE"} else "不满足当前自助办理条件"
            reason = str(result.get('reason') or '资格事实不完整，请稍后重试').rstrip('。；')
            summary = f"订单 {order_id} {prefix}：{reason}。这轮只查资格，没有准备申请。"
        return result, ToolCallTrace(tool_name="get_after_sale_eligibility", arguments=arguments,
            output_summary=summary, status="success", risk_level="low", next_action="answer_user")
    return None, ToolCallTrace(tool_name="get_after_sale_eligibility", arguments=arguments, output_summary=summary,
        status="error", error_type=error, risk_level="low",
        next_action="ask_clarification" if error == "missing_order_id" else "answer_user")

def get_order_detail(
    order_id: str | None,
    runtime_user_id: str,
    runtime_context: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, ToolCallTrace]:
    """执行订单详情工具，同时校验订单归属，防止越权售后操作。"""
    arguments = {"order_id": order_id}
    if order_id is None:
        return None, ToolCallTrace(
            tool_name="get_order_detail",
            arguments=arguments,
            output_summary="缺少订单号，不能查询订单。",
            status="error",
            error_type="missing_order_id",
            next_action="ask_clarification",
        )
    order = find_context_order(runtime_context, order_id) or order_fact_from_ecommerce(order_id, runtime_user_id)
    if order is None:
        return None, ToolCallTrace(
            tool_name="get_order_detail",
            arguments=arguments,
            output_summary=f"订单 {order_id} 不存在或当前业务系统暂未返回该订单。",
            status="error",
            error_type="not_found",
            risk_level="medium",
            next_action="ask_clarification",
        )
    if order_user_id(order) and order_user_id(order) != runtime_user_id:
        return None, ToolCallTrace(
            tool_name="get_order_detail",
            arguments=arguments,
            output_summary=f"订单 {order_id} 没有通过当前用户归属校验。",
            status="error",
            error_type="owner_mismatch",
            risk_level="medium",
            next_action="transfer_to_human",
        )
    arguments["fact_source"] = order.get("_fact_source", "runtime_context")
    summary = f"订单 {order_no(order)} 当前状态为{order_status_label(order)}，物流状态为{logistics_status_label(order)}，商品 {item_names(order)[0]}。"
    return order, ToolCallTrace(tool_name="get_order_detail", arguments=arguments, output_summary=summary, status="success", risk_level="low")


def get_order_logistics(order: dict[str, Any]) -> ToolCallTrace:
    """基于订单事实生成物流工具结果，不让模型凭空猜测实时状态。"""
    return ToolCallTrace(
        tool_name="get_order_logistics",
        arguments={"order_id": order_no(order)},
        output_summary=f"物流状态为{logistics_status_label(order)}，订单状态为{order_status_label(order)}。",
        status="success",
        risk_level="low",
        next_action="answer_user",
    )


def get_refund_status(order: dict[str, Any], runtime_user_id: str) -> ToolCallTrace:
    """读取已存在的售后申请状态；查询进度绝不能隐式创建新退款。"""
    requests = after_sale_requests_from_ecommerce(order_no(order), runtime_user_id)
    if requests is None:
        return ToolCallTrace(
            tool_name="get_refund_status",
            arguments={"order_id": order_no(order), "fact_source": "business_api_unavailable"},
            output_summary=f"订单 {order_no(order)} 的退款进度暂时无法从业务系统核实，本轮不会猜测退款状态，建议稍后重试或转人工客服。",
            status="error",
            risk_level="medium",
            next_action="transfer_to_human",
            error_type="business_api_unavailable",
        )
    refund_requests = [item for item in requests if str(item.get("requestType") or item.get("type") or "").upper() in {"REFUND", "RETURN", "CANCEL_ORDER"}]
    if not refund_requests:
        return ToolCallTrace(
            tool_name="get_refund_status",
            arguments={"order_id": order_no(order), "fact_source": order.get("_fact_source", "runtime_context")},
            output_summary=f"订单 {order_no(order)} 暂未查询到退款申请记录。",
            status="success",
            risk_level="low",
            next_action="answer_user",
        )
    latest = max(
        refund_requests,
        key=lambda item: str(item.get("updatedAt") or item.get("createdAt") or ""),
    )
    request_id = str(latest.get("requestId") or latest.get("requestNo") or "未知申请号")
    status = str(latest.get("status") or "UNKNOWN").upper()
    status_label = {"PENDING": "待处理", "PENDING_REVIEW": "待审核", "NEED_MORE_INFO": "待补充材料", "REVIEWING": "审核中", "APPROVED": "已通过", "REJECTED": "未通过", "COMPLETED": "已完成"}.get(status, status)
    status_label = {"WAITING_RETURN": "待买家寄回", "RETURN_IN_TRANSIT": "待商家收货验收", "RETURN_INSPECTION_FAILED": "验收未通过，待人工处理", "REFUNDED": "已退款"}.get(status, status_label)
    return ToolCallTrace(
        tool_name="get_refund_status",
        arguments={"order_id": order_no(order), "fact_source": latest.get("_fact_source", "business_api")},
        output_summary=f"退款申请 {request_id} 当前状态为{status_label}。",
        status="success",
        risk_level="low",
        next_action="answer_user",
    )


def _amount(value: int | float) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def _valid_price(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value


def _valid_stock(value: Any) -> int | None:
    if isinstance(value, bool) or type(value) is not int or value < 0:
        return None
    return value


def _identity_ok(product: Any) -> bool:
    return isinstance(product, dict) and product.get("id") is not None and bool(product_name(product))


def _interpret_product_load(loaded: Any) -> tuple[list[dict[str, Any]] | None, str | None, str]:
    """真实读取返回 (数据, 失败类别或来源)；既有调用仍可返回 list 或 None。"""
    if isinstance(loaded, tuple) and len(loaded) == 2:
        payload, note = loaded
        if note in {"timeout", "unavailable"}:
            return None, note, note
        if isinstance(payload, list):
            source = note if note in {"business_api", "course_seed_mirror"} else "business_api"
            return payload, None, source
    if isinstance(loaded, list):
        return loaded, None, "business_api"
    return None, "unavailable", "unavailable"


def _product_read_failure(arguments: dict[str, Any], error: str | None, *, product_id: int | str | None = None) -> ToolCallTrace:
    if error == "not_found":
        summary = f"编号 {product_id} 对应的商品不存在或已删除，没有改查其他商品。"
        error_type = "product_not_found"
    else:
        summary = "这次商品信息查询没有成功，暂时无法核实。请稍后重试。"
        error_type = "timeout" if error == "timeout" else "business_api_unavailable"
    return ToolCallTrace(
        tool_name="search_products",
        arguments={**arguments, "read_error": error_type},
        output_summary=summary,
        status="error",
        risk_level="low",
        next_action="answer_user",
        error_type=error_type,
    )


def _product_summary(product: dict[str, Any]) -> tuple[str, list[str]]:
    """只写入通过类型校验的价格、库存和活动。0 是有效值，缺失、null 和无效值不能显示成事实。"""
    name = product_name(product) or "商品"
    missing: list[str] = []
    price = _valid_price(product.get("price"))
    stock = _valid_stock(product.get("stock"))
    facts: list[str] = []
    if price is None:
        missing.append("price")
    else:
        facts.append(f"商品标价 {_amount(price)} 元")
    if stock is None:
        missing.append("stock")
    elif stock == 0:
        facts.append("当前无库存")
    else:
        facts.append(f"库存 {stock} 件")
    summary = f"{name} {'，'.join(facts)}。" if facts else f"{name}。"
    if "price" in missing:
        summary += "价格暂未核实。"
    if "stock" in missing:
        summary += "库存暂未核实。"
    if product.get("active") is False:
        summary += " 该商品当前已下架，暂不可购买。"
    promotion = product.get("promotion")
    if isinstance(promotion, dict):
        promo_price = _valid_price(promotion.get("promotionPrice"))
        promotion_facts = [
            f"当前活动为{promotion.get('promotionName')}" if isinstance(promotion.get("promotionName"), str) and promotion.get("promotionName").strip() else None,
            f"活动价 {_amount(promo_price)} 元" if promo_price is not None else None,
            f"适用条件为{promotion.get('conditionSummary')}" if isinstance(promotion.get("conditionSummary"), str) and promotion.get("conditionSummary").strip() else None,
            str(promotion.get("discountSummary")).rstrip("。") if isinstance(promotion.get("discountSummary"), str) and promotion.get("discountSummary").strip() else None,
        ]
        shown = "，".join(fact for fact in promotion_facts if fact)
        if shown:
            summary += f" {shown}。"
    return summary, missing


def search_products(
    keyword: str | None = None,
    *,
    product_id: int | str | None = None,
    match_text: str | None = None,
) -> tuple[list[dict[str, Any]], ToolCallTrace]:
    """查询商品实时事实。传编号时只读该商品；按关键词时多结果列候选，不默选第一款。"""
    if product_id is not None:
        arguments = {"product_id": product_id}
        product, error = product_detail_from_ecommerce(product_id)
        if not isinstance(product, dict) or not _identity_ok(product):
            return [], _product_read_failure(arguments, error or "unavailable", product_id=product_id)
        arguments.update({"product_name": product_name(product), "fact_source": product.get("_fact_source", "business_api")})
        summary, missing = _product_summary(product)
        if missing:
            arguments["missing_fields"] = missing
        return [product], ToolCallTrace(
            tool_name="search_products",
            arguments=arguments,
            output_summary=summary,
            status="success",
            risk_level="low",
            next_action="answer_user",
        )
    products, failure, source = _interpret_product_load(products_from_ecommerce(keyword or ""))
    if failure or products is None:
        return [], _product_read_failure({"keyword": keyword}, failure or "unavailable")
    if products and not any(_identity_ok(item) for item in products):
        return [], _product_read_failure({"keyword": keyword}, "unavailable")
    products = [item for item in products if _identity_ok(item)]
    original = match_text or keyword or ""
    if has_product_term(original):
        requested_name = normalize_product_text(product_query_keyword(original))
        products = [item for item in products if requested_name in normalize_product_text(product_name(item))]
    if not products:
        return [], ToolCallTrace(
            tool_name="search_products",
            arguments={"keyword": keyword, "result": "no_match", "fact_source": source},
            output_summary=f"没有找到与“{keyword}”匹配的商品。可以补充更完整的商品名称或款式再试。",
            status="success",
            risk_level="low",
            next_action="ask_clarification",
        )
    selected = unique_name_match(products, match_text)
    if selected is None and len(products) == 1:
        selected = products[0]
    if selected is not None:
        summary, missing = _product_summary(selected)
        arguments = {
            "keyword": keyword,
            "product_id": selected.get("id"),
            "product_name": product_name(selected),
            "fact_source": selected.get("_fact_source", source),
        }
        if missing:
            arguments["missing_fields"] = missing
        return [selected], ToolCallTrace(
            tool_name="search_products",
            arguments=arguments,
            output_summary=summary,
            status="success",
            risk_level="low",
            next_action="answer_user",
        )
    shown = products[:MAX_SHOWN_CANDIDATES]
    prompt = candidate_prompt(shown, more_count=len(products) - len(shown), keyword=keyword or "商品")
    return shown, ToolCallTrace(
        tool_name="search_products",
        arguments={"keyword": keyword, "fact_source": shown[0].get("_fact_source", "business_api")},
        output_summary=prompt,
        status="success",
        risk_level="low",
        next_action="ask_clarification",
        candidates=[{"value": str(item.get("id")), "label": product_name(item)} for item in shown],
        clarification_field="product_id",
        clarification_prompt=prompt,
    )
