"""只读查询当前客服渠道的业务能力。支持状态来自 Java 目录，查询失败不是不支持。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from agent.ecommerce_client import has_product_term
from agent.product_query import wants_stock_reminder
from agent.tools.planning import _explicit_apply, extract_order_id, is_specific_eligibility_question
from settings import ecommerce_base_url, load_course_env

CATALOG_PATH = Path(__file__).resolve().parents[2] / "ecommerce-backend/src/main/resources/service-capabilities.json"
CHANNEL = "customer_service_agent"
_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SUPPORT_ASK = re.compile(r"支不支持|是否支持|能不能|能否|可不可以|支持[^。！？?\n]{0,16}吗")
_REMINDER_PHRASES = (
    "有货后能提醒我", "有货后能通知我", "有货后提醒我", "有货后通知我",
    "到货提醒", "补货提醒", "到货通知", "补货通知", "有货后提醒", "有货后通知",
    "提醒我", "通知我",
)
_FACT_CUES = ("多少钱", "价格", "标价", "库存", "有货", "没货", "几元", "多少元")
_ITEM_CUES = ("这件", "这款", "此商品", "该商品")
_SCOPE_HINTS = {
    "inventory_query": "库存查询",
    "stock_notification": "到货与补货提醒",
    "exchange_handling": "换货办理",
    "human_takeover": "真人接管",
    "refund_consultation": "退款咨询",
    "return_consultation": "退货咨询",
}


def catalog_payload(capability_id: str) -> dict[str, Any]:
    """读取 Java 同一份目录文件，只给测试构造查询结果。线上答复必须走 query_service_capability。"""
    document = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    for item in document["capabilities"]:
        if item["capabilityId"] == capability_id:
            return {
                "capabilityId": item["capabilityId"],
                "channel": document["channel"],
                "supportStatus": item["supportStatus"],
                "scope": item["scope"],
                "explanation": item["explanation"],
                "alternatives": list(item.get("alternatives") or []),
                "version": document["version"],
            }
    return {
        "capabilityId": capability_id,
        "channel": document["channel"],
        "supportStatus": "unknown",
        "scope": "未登记的业务能力",
        "explanation": "目前还不能确认是否提供这项服务。",
        "alternatives": [],
        "version": document["version"],
    }


def asked_capability_ids(message: str) -> list[str]:
    """本轮被问到的能力。只识别明确咨询，价格或否定提醒不带出额外能力。"""
    # ponytail: 只认这些明确说法，换一种问法会漏查。升级路径是把能力 id 放进现有那一次路由结果，而不是再加一个模型。
    if is_specific_eligibility_question(message) or extract_order_id(message):
        return ["stock_notification"] if wants_stock_reminder(message) else []
    found: list[str] = []
    if wants_stock_reminder(message):
        found.append("stock_notification")
    if _SUPPORT_ASK.search(message):
        if any(term in message for term in ("查库存", "库存查询", "查询库存", "支持库存", "能不能查库存", "能否查库存")):
            found.append("inventory_query")
        if any(term in message for term in ("换货", "换一件", "换新", "换一个")):
            found.append("exchange_handling")
        if any(term in message for term in ("转人工", "转接人工", "人工客服", "真人客服", "找人工", "真人接管", "真人")):
            found.append("human_takeover")
        if "退款" in message or "退钱" in message:
            found.append("refund_consultation")
        if "退货" in message and "换货" not in message:
            found.append("return_consultation")
    return list(dict.fromkeys(found))


def is_service_scope_question(message: str, capability_ids: list[str]) -> bool:
    """规则路由的服务范围识别，不决定执行和答复方式。"""
    if not capability_ids or _explicit_apply(message) or is_specific_eligibility_question(message) or extract_order_id(message):
        return False
    if has_product_term(message) or any(cue in message for cue in _ITEM_CUES):
        return False
    remainder = message
    for phrase in _REMINDER_PHRASES:
        remainder = remainder.replace(phrase, "")
    if not any(cue in remainder for cue in _FACT_CUES):
        return True
    # “能不能查库存”没有点名商品，问的是能力本身，不是某一件的库存。
    return capability_ids == ["inventory_query"] and not any(
        cue in message for cue in ("多少钱", "价格", "标价", "几元", "多少元", "有货", "没货")
    )


def query_service_capability(capability_id: str) -> dict[str, Any]:
    """调用 Java 只读接口。超时、错误和未登记都不写成 unsupported。"""
    if not _ID.match(capability_id or ""):
        return _failed(capability_id, "error")
    load_course_env()
    token = os.getenv(
        "AGENT_ECOMMERCE_SERVICE_TOKEN",
        os.getenv("AGENT_SERVICE_AUTH_TOKEN", "course-debug-agent-service"),
    ).strip()
    headers = {"X-Agent-Service-Token": token} if token else {}
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get(
                f"{ecommerce_base_url()}/api/customer-service/capabilities/{capability_id}",
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        return _failed(capability_id, "timeout")
    except Exception:
        return _failed(capability_id, "error")
    data = payload.get("data") if isinstance(payload, dict) and payload.get("success") is True else None
    if not isinstance(data, dict) or data.get("capabilityId") != capability_id:
        return _failed(capability_id, "error")
    if data.get("channel") != CHANNEL or data.get("supportStatus") not in {"supported", "unsupported", "unknown"}:
        return _failed(capability_id, "error")
    return data


def capability_answer_text(result: dict[str, Any]) -> str:
    """把一次查询结果收成答复项。失败句不判断该能力是否支持。"""
    scope = str(result.get("scope") or _SCOPE_HINTS.get(str(result.get("capabilityId")), "这项服务"))
    if result.get("failure") or result.get("supportStatus") not in {"supported", "unsupported", "unknown"}:
        reason = "查询超时" if result.get("failure") == "timeout" else "没有查到支持状态"
        return f"{scope}这次{reason}，暂时不能确认是否提供。请稍后再问。"
    text = str(result.get("explanation") or "").strip()
    if not text:
        return f"{scope}这次没有查到支持状态，暂时不能确认是否提供。请稍后再问。"
    steps = [str(item).strip() for item in result.get("alternatives") or [] if str(item).strip()]
    if steps:
        extra = "你可以" + "，或".join(steps) + "。"
        if extra not in text:
            text = text.rstrip("。") + "。" + extra
    return text


def compose_capability_fallback(answer: str, results: list[dict[str, Any]]) -> str:
    """生成前组织可信草稿，供直接答复、跳过生成或整条降级使用。"""
    current = answer or ""
    blocks: list[str] = []
    for result in results:
        text = capability_answer_text(result)
        if text and text not in current and text not in "\n".join(blocks):
            blocks.append(text)
    if not blocks:
        return current
    return "\n".join(part for part in (current.strip(), *blocks) if part)


_ANSWER_TOPICS = {
    "stock_notification": r"提醒|通知|到货|补货",
    "refund_consultation": r"退款",
    "return_consultation": r"退货",
    "inventory_query": r"价格|库存|查商品",
    "exchange_handling": r"换货",
    "human_takeover": r"人工|真人",
}
_UNKNOWN_ANSWER = re.compile(r"(?:不能|无法).{0,3}(?:确认|确定)|不确定|不清楚|不知道|(?:没|未|没有)查到|查询(?:失败|超时)")
_UNSUPPORTED_ANSWER = re.compile(r"不支持|不提供|暂不|(?:不能|无法)(?:提供|办理|查询|登记|使用|自动|通知|提醒|接管|转接)|(?:没有|暂无|不具备).{0,10}(?:功能|服务|能力|接管)")
_SUPPORTED_ANSWER = re.compile(r"支持|(?:可以|能够|可|能)(?:办理|查询|登记|使用|提供)|(?:可以|能够|可|能).{0,6}(?:退款|退货|换货)|提供.{0,8}(?:服务|功能)")
_CLAIMS_DONE = re.compile(r"已(?:经)?(?:为你|帮你|为您|帮您)?(?:提交|登记|创建|开启|转接|接入|申请|接手)")
_NOT_APPLIED = re.compile(
    r"(?:未|没|没有|不曾)(?:为你|帮你|为您|帮您)?(?:提交|发起|申请|登记)"
    r"|不(?:等于|代表).{0,8}已(?:经)?(?:提交|申请|登记)"
    r"|(?:本次|这次|本轮).{0,5}(?:仅|只).{0,4}(?:咨询|查询)"
)


def capability_answer_issue(answer: str, results: list[dict[str, Any]]) -> str | None:
    """检查本轮能力的明确状态和已知漏答，不改写或插入模型文字。"""
    if not answer.strip():
        return "empty_model_answer"
    if "[[capability:" in answer:
        return "legacy_capability_reference"
    # shortcut: 只识别明确能力主题和状态表达；任意语义重复、范围及资格一致性仍靠任务评测。
    clauses = [part.strip() for part in re.split(r"[。！？\n；;，,]", answer) if part.strip()]
    if results and any(_CLAIMS_DONE.search(part) and not re.search(
                           r"不等于|不代表|并非|不是|(?:未|没有|没)(?:为你|帮你|为您|帮您)?(?:提交|登记|创建|开启|转接|接入|申请|接手)", part)
                       for part in clauses):
        return "unperformed_capability_action"
    for result in results:
        expected = "unknown" if result.get("failure") else result.get("supportStatus", "unknown")
        topic = _ANSWER_TOPICS.get(result.get("capabilityId"))
        related = [part for part in clauses if topic and re.search(topic, part)]
        if len(results) == 1:
            related += [part for part in clauses if re.search(r"这项(?:服务|功能)|该服务|支持状态", part)]
        if not topic and len(results) == 1:
            related = clauses
        statuses = set()
        for part in related:
            if re.search(r"订单|资格|这笔|该单", part) and not re.search(r"客服|渠道|支持", part):
                continue
            if _UNKNOWN_ANSWER.search(part):
                # 具体订单待核验不能否定渠道支持范围。
                if not re.search(r"订单|资格|这笔|该单", part):
                    statuses.add("unknown")
            elif _UNSUPPORTED_ANSWER.search(part):
                statuses.add("unsupported")
            elif _SUPPORTED_ANSWER.search(part):
                statuses.add("supported")
        if not statuses:
            return "capability_answer_incomplete"
        if statuses != {expected}:
            return "capability_answer_conflict"
        if result.get("capabilityId") in {"refund_consultation", "return_consultation"} \
                and expected == "supported" and not _NOT_APPLIED.search(answer):
            return "capability_answer_incomplete"
    return None


def _failed(capability_id: str, failure: str) -> dict[str, Any]:
    return {
        "capabilityId": capability_id,
        "channel": CHANNEL,
        "supportStatus": None,
        "scope": _SCOPE_HINTS.get(capability_id),
        "explanation": None,
        "alternatives": [],
        "version": None,
        "failure": failure,
    }
