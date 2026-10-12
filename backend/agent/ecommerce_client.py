"""荒野户外业务后端集成层。课程代码通过这里获取实时订单事实。"""

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

from settings import ecommerce_base_url, load_course_env

COURSE_SEED_ORDER_MIRRORS: dict[str, dict[str, Any]] = {
    "SO20260601090000008-a1000008": {
        "orderNo": "SO20260601090000008-a1000008",
        "userId": "U1001",
        "paymentStatus": "PAID",
        "fulfillmentStatus": "PAID_PENDING_SHIPMENT",
        "items": [{"productName": "露营折叠椅"}],
    },
    "SO20260602103000009-a1000009": {
        "orderNo": "SO20260602103000009-a1000009",
        "userId": "U1001",
        "paymentStatus": "PAID",
        "fulfillmentStatus": "SHIPPED",
        "logisticsStatus": "IN_TRANSIT",
        "items": [{"productName": "户外野餐垫"}],
    },
    "SO20260712090000010-a1000010": {
        "orderNo": "SO20260712090000010-a1000010",
        "userId": "U1001",
        "paymentStatus": "PAID",
        "fulfillmentStatus": "DELIVERED",
        "logisticsStatus": "SIGNED",
        "deliveredAt": "2026-07-12T09:00:00",
        "returnable": True,
        "items": [{"productName": "露营折叠椅", "returnable": True}],
    },
}

COURSE_SEED_PRODUCT_MIRRORS: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "露营折叠椅",
        "code": "SKU-AUD-101",
        "category": "营地家具",
        "price": 599.0,
        "stock": 520,
        "active": True,
        "returnable": True,
        "highlights": "便携收纳；营地休闲；参加会员满减活动",
        "promotion": {
            "promotionName": "营地家具会员会场",
            "promotionType": "member_discount",
            "discountSummary": "折叠椅、折叠桌和野餐垫进入露营季营地家具会场，活动价和会员条件以结算页为准。",
            "promotionPrice": 529.0,
            "requiredMemberLevel": "gold",
            "conditionSummary": "山友会员专享",
        },
    }
]

COURSE_SEED_AFTER_SALE_MIRRORS: dict[str, list[dict[str, Any]]] = {
    "SO20260602103000009-a1000009": [
        {
            "requestId": "AS-STORY-REFUND-0009",
            "orderNo": "SO20260602103000009-a1000009",
            "userId": "U1001",
            "requestType": "REFUND",
            "status": "REVIEWING",
        }
    ]
}


def course_seed_mirror_enabled() -> bool:
    """业务种子镜像只供显式离线课程测试使用，不能掩盖在线接口故障。"""
    return os.getenv("AGENT_COURSE_OFFLINE_FACTS") == "1"


def _with_fact_source(value: dict[str, Any], source: str) -> dict[str, Any]:
    return {**value, "_fact_source": source}

def delegated_service_headers(current_user_id: str | None) -> dict[str, str]:
    """总演习只用业务事实中的当前用户构造已认证 Agent 服务身份。"""
    load_course_env()
    user_id = str(current_user_id or "").strip()
    token = os.getenv(
        "AGENT_ECOMMERCE_SERVICE_TOKEN",
        os.getenv("AGENT_SERVICE_AUTH_TOKEN", "course-debug-agent-service"),
    ).strip()
    if not user_id or not token:
        return {}
    return {"X-Agent-Service-Token": token, "X-Agent-User-Id": user_id}

def ecommerce_get(
    path: str,
    *,
    delegated_user_id: str | None = None,
) -> dict[str, Any] | list[Any] | None:
    """封装业务后端 GET 调用，统一处理响应结构和错误边界。"""
    # 课程业务后端通常运行在 localhost；不继承宿主机 HTTP 代理，避免本地请求被代理劫持。
    with httpx.Client(timeout=5, trust_env=False) as client:
        response = client.get(
            f"{ecommerce_base_url()}{path}",
            headers=delegated_service_headers(delegated_user_id),
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    return payload.get("data")

def order_fact_from_ecommerce(target_order_no: str, current_user_id: str) -> dict[str, Any] | None:
    """从业务后端读取订单事实；失败时返回空值，交给 Agent 降级或转人工。"""
    try:
        order = ecommerce_get(f"/api/orders/{target_order_no}", delegated_user_id=current_user_id)
    except Exception:
        order = None
    if isinstance(order, dict):
        enriched = _with_fact_source(order, "business_api")
        items = enriched.get("items") or []
        product_facts: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or item.get("productId") is None:
                product_facts.append(item)
                continue
            try:
                product = ecommerce_get(f"/api/products/{item['productId']}")
            except Exception:
                product = None
            product_facts.append({**item, "returnable": product.get("returnable")} if isinstance(product, dict) else item)
        if product_facts:
            enriched["items"] = product_facts
            returnability = [item.get("returnable") for item in product_facts if isinstance(item, dict)]
            if returnability and all(value is True for value in returnability):
                enriched["returnable"] = True
            elif any(value is False for value in returnability):
                enriched["returnable"] = False
            else:
                enriched["returnable"] = None
        return enriched
    if not course_seed_mirror_enabled():
        return None
    mirror = COURSE_SEED_ORDER_MIRRORS.get(target_order_no)
    return _with_fact_source(mirror, "course_seed_mirror") if mirror else None


def _detail_confirms_missing(exc: httpx.HTTPStatusError) -> bool:
    """只有详情接口 404 且业务码 NOT_FOUND 才表示这个编号不存在。"""
    if exc.response.status_code != 404:
        return False
    try:
        body = exc.response.json()
    except Exception:
        return False
    return isinstance(body, dict) and body.get("code") == "NOT_FOUND"


def _named_product(product: Any, product_id: int | str) -> dict[str, Any] | None:
    if not isinstance(product, dict) or str(product.get("id")) != str(product_id):
        return None
    if not str(product.get("name") or product.get("productName") or "").strip():
        return None
    return product


def products_from_ecommerce(keyword: str) -> tuple[list[dict[str, Any]] | None, str]:
    """查询实时商品。(列表, business_api|course_seed_mirror) 成功，空列表是零匹配；(None, timeout|unavailable) 是读取失败。"""
    products: Any = None
    error: str | None = None
    try:
        query_keyword = product_query_keyword(keyword)
        products = ecommerce_get(f"/api/products?{httpx.QueryParams({'keyword': query_keyword})}")
    except httpx.TimeoutException:
        error = "timeout"
    except Exception:
        error = "unavailable"
    if error is None:
        rows = [item for item in products if isinstance(item, dict)] if isinstance(products, list) else None
        if rows is not None and (not products or rows):
            return [_with_fact_source(item, "business_api") for item in rows], "business_api"
        error = "unavailable"
    if not course_seed_mirror_enabled():
        return None, error
    normalized = keyword.replace(" ", "")
    return [
        _with_fact_source(item, "course_seed_mirror")
        for item in COURSE_SEED_PRODUCT_MIRRORS
        if any(term in normalized for term in ("折叠椅", "露营椅")) and "折叠椅" in str(item.get("name"))
    ], "course_seed_mirror"


def product_detail_from_ecommerce(product_id: int | str) -> tuple[dict[str, Any] | None, str | None]:
    """按编号读取详情。(商品, None) 成功；(None, not_found|timeout|unavailable) 区分不存在与读取失败。"""
    product: Any = None
    error: str | None = None
    try:
        product = ecommerce_get(f"/api/products/{product_id}")
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.HTTPStatusError as exc:
        error = "not_found" if _detail_confirms_missing(exc) else "unavailable"
    except Exception:
        error = "unavailable"
    if error is None:
        named = _named_product(product, product_id)
        if named is not None:
            return _with_fact_source(named, "business_api"), None
        error = "unavailable"
    if error != "not_found" and course_seed_mirror_enabled():
        mirror = next((item for item in COURSE_SEED_PRODUCT_MIRRORS if str(item.get("id")) == str(product_id)), None)
        if mirror:
            return _with_fact_source(mirror, "course_seed_mirror"), None
    return None, error


PRODUCT_TERMS = ("露营折叠椅", "露营椅", "折叠椅", "折叠桌", "野餐垫", "帐篷", "天幕", "营地灯", "背包", "登山杖", "睡袋", "收纳车", "收纳柜", "炊具", "防水袋", "头灯", "营地旗", "营地标识")


def has_product_term(user_message: str) -> bool:
    """客户原句是否带有明确商品名称或类别词，用于区分新对象与追问。"""
    return any(term in user_message for term in PRODUCT_TERMS)


def product_query_keyword(user_message: str) -> str:
    """提取商品名称，保留款式区别；仅类别别名规范为搜索词。"""
    for term in PRODUCT_TERMS:
        if term in user_message:
            start = user_message.index(term)
            prefix = re.split(r"[\n，,。；;！？!?]", user_message[:start])[-1]
            # shortcut: 只裁掉常见询问前缀，复杂描述无法匹配时澄清，不删款式兜底。
            prefix = re.sub(r"^.*(?:请问|帮我查一下|查一下|查询|看看|看一下|我要问|我想问|我想买|咨询|了解|换看|查)", "", prefix)
            prefix = re.sub(r"^(?:这件|这款|这个|那件|那款|商品|一款|一个|的|\s)+", "", prefix)
            name = (prefix + term).strip()
            return "折叠椅" if name in {"露营折叠椅", "露营椅"} else name
    return user_message.strip()


def after_sale_requests_from_ecommerce(order_id: str, current_user_id: str) -> list[dict[str, Any]] | None:
    """按订单号读取售后进度；None 表示业务接口不可用，空列表表示确认没有记录。"""
    try:
        order = order_fact_from_ecommerce(order_id, current_user_id)
        if not current_user_id:
            raise ValueError("missing_current_user_id")
        requests = ecommerce_get(
            f"/api/after-sale/requests?orderNo={order_id}",
            delegated_user_id=current_user_id,
        )
    except Exception:
        requests = None
    if isinstance(requests, list):
        return [_with_fact_source(item, "business_api") for item in requests if isinstance(item, dict)]
    if not course_seed_mirror_enabled():
        return None
    return [_with_fact_source(item, "course_seed_mirror") for item in COURSE_SEED_AFTER_SALE_MIRRORS.get(order_id, [])]
