"""回复评审的共享证据、受控替代稿和内部审阅记录。"""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from api.schemas import ChatRequest, Citation, RoutePlan, ToolCallTrace
from agent.service_capabilities import CHANNEL, capability_answer_text
from retrieval.documents import knowledge_time, citation_is_current
from retrieval.knowledge import _plain_evidence

REVIEW_INTENTS = {"service_scope_query", "product_query", "promotion_query", "invoice_query",
                  "refund_policy_query", "return_policy_query", "order_query", "refund_status_query"}
READ_TOOLS = {"search_products", "get_order_detail", "get_order_logistics", "get_refund_status"}
# shortcut: 内部脱敏审阅只保留最近100条进程内记录；真实灰度需持久留存时再接有权限的审计存储。
review_audit: deque[dict[str, Any]] = deque(maxlen=100)


def redact_review(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_review(item) for key, item in value.items()
                if key.endswith("_tokens") or not re.search(r"token|secret|password|cookie|authorization|nickname|user_id|address|phone|email", key, re.I)}
    if isinstance(value, list):
        return [redact_review(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[已脱敏]", value)
        value = re.sub(r"(?i)\bBearer\s+\S+|\bsk-[a-z0-9_-]{8,}", "[已脱敏凭据]", value)
        value = re.sub(r"(?i)(?:api[_ -]?key|resume[_ -]?token|access[_ -]?token|password|cookie)\s*[:=]\s*[^\s,，;；]+", "[已脱敏凭据]", value)
        return re.sub(r"(?:收货地址|地址)\s*[:：]\s*[^，。;；\n]+", "[已脱敏地址]", value)
    return value


def build_review_context(*, request: ChatRequest, plan: RoutePlan, answer: str,
                         tool_calls: list[ToolCallTrace], citations: list[Citation],
                         capabilities: list[dict[str, Any]], history: list[str],
                         interaction_policy: dict[str, Any] | None = None) -> tuple[dict[str, Any], str, list[str]]:
    """只整理已经取得的事实，不补查、不从候选或模型结论反推事实。"""
    evidence: list[dict[str, Any]] = [
        {"id": "request", "kind": "request", "text": request.user_message},
        {"id": "history", "kind": "unverified_conversation", "text": history},
    ]
    blocks: list[str] = []
    issues: list[str] = []
    reads: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        facts = call.review_facts
        object_key = "product_id" if call.tool_name == "search_products" else "order_id"
        object_id = call.arguments.get(object_key)
        mismatch = facts is not None and object_id is not None and str(facts.get(object_key)) != str(object_id)
        supported = call.tool_name in READ_TOOLS
        valid = supported and not mismatch
        if not valid:
            issues.append("tool_object_mismatch" if mismatch else "unsupported_review_tool")
        if valid and call.status == "success" and facts is None:
            issues.append("structured_tool_evidence_missing")
            valid = False
        summary = call.output_summary if valid else "本轮部分业务证据未通过完整性或对象核对，暂不能确认对应结果。"
        item = {"id": f"tool:{index}", "kind": "tool", "tool": call.tool_name,
                "object_id": object_id, "source": call.arguments.get("fact_source", "controlled_tool"),
                "status": call.status if valid else "invalid", "error": call.error_type,
                "facts": facts if valid and call.status == "success" else None,
                "summary": summary, "complete": valid and facts is not None}
        evidence.append(item)
        blocks.append(summary)
        reads.append({"evidence_id": item["id"], "tool": call.tool_name, "object_id": object_id,
                      "status": item["status"]})
        if call.tool_name == "get_refund_status":
            historical.append({"object_id": object_id, "evidence_id": item["id"],
                "records": facts.get("historical") if valid and facts else None,
                "complete": bool(valid and facts and facts.get("complete")),
                "scope": (facts or {}).get("scope", "queried_order")})
    safe_capabilities = []
    for item in capabilities:
        cid = item.get("capabilityId")
        valid = cid in plan.capability_ids and cid not in [x.get("capabilityId") for x in safe_capabilities]
        valid = valid and (bool(item.get("failure")) or (
            item.get("channel") == CHANNEL and bool(item.get("version"))
            and item.get("supportStatus") in {"supported", "unsupported", "unknown"}))
        if not valid:
            issues.append("invalid_capability_evidence")
            continue
        safe = {key: item.get(key) for key in ("capabilityId", "channel", "supportStatus", "scope",
                                             "explanation", "alternatives", "version", "failure")}
        safe_capabilities.append(safe)
    for cid in plan.capability_ids:
        if not any(item["capabilityId"] == cid for item in safe_capabilities):
            issues.append("capability_evidence_missing")
            safe_capabilities.append({"capabilityId": cid, "supportStatus": None, "failure": "evidence_missing"})
    for item in safe_capabilities:
        evidence.append({"id": f"capability:{item['capabilityId']}", "kind": "capability", "facts": item})
        blocks.append(capability_answer_text(item))
    valid_citations = []
    for index, citation in enumerate(citations):
        metadata = citation.metadata or {}
        if not metadata or not citation_is_current(citation, knowledge_time()):
            issues.append("invalid_policy_evidence")
            continue
        valid_citations.append(citation)
        evidence.append({"id": f"policy:{index}", "kind": "policy", "text": citation.snippet,
            "source": citation.source, "metadata": {key: metadata.get(key) for key in (
                "policy_id", "version", "section_id", "effective_from", "effective_to", "publication_status",
                "snapshot_version", "scope")}})
        blocks.append(_plain_evidence(citation.snippet))
    policy_ids = {(item.metadata or {}).get("policy_id") for item in valid_citations}
    if policy_ids & {"promotion_camping_season_stack_rule", "member_coupon_gold_rule"}:
        blocks.append("以上为平台通用规则，不代表当前商品参与活动或本人优惠券已适用，具体组合以商品页和结算条件为准。")
    if "outdoor_product_guide" in policy_ids:
        blocks.append("以上为通用使用知识，具体型号参数以说明书为准。")
    if plan.intent in {"refund_policy_query", "return_policy_query"} and valid_citations:
        blocks.append("以上为通用售后规则，具体订单资格仍需核验，不代表已经提交申请。")
    if plan.needs_product_facts and not any(call.tool_name == "search_products" for call in tool_calls):
        issues.append("product_evidence_missing")
        blocks.append("本轮尚未核实商品价格和库存。")
    if plan.needs_rag and not valid_citations:
        blocks.append("本轮尚未取得能回答该问题的有效知识依据，暂不能确认相关规则。")
    evidence.append({"id": "execution", "kind": "execution", "this_turn": {
        "write_actions": [], "read_checks": reads, "complete": True,
        "scope": "本轮受控只读编排，未提交申请或登记通知；不证明历史无申请"},
        "historical": historical, "historical_complete": False})
    allowed_next_steps: list[dict[str, Any]] = []
    if interaction_policy is not None:
        if interaction_policy.get("failure") or interaction_policy.get("source") != "controlled_http":
            evidence.append({"id": "interaction_policy", "kind": "interaction_policy",
                "facts": {"failure": interaction_policy.get("failure") or "untrusted_source",
                          "channel": interaction_policy.get("channel")}})
            blocks.append("本轮客服可引导的入口信息尚未确认，不能据此说明有无可用入口。")
        else:
            policy_facts = {"schemaVersion": interaction_policy.get("schemaVersion"),
                            "channel": interaction_policy.get("channel"),
                            "version": interaction_policy.get("version"),
                            "entries": interaction_policy.get("entries") or []}
            evidence.append({"id": "interaction_policy", "kind": "interaction_policy", "facts": policy_facts})
            for entry in policy_facts["entries"]:
                if entry.get("status") != "available" or entry.get("actor") != "user" \
                        or entry.get("completionEffect") != "open_entry":
                    continue
                allowed_next_steps.append({
                    "id": f"entry:{entry['id']}",
                    "actor": entry["actor"],
                    "stage": "request_entry",
                    "surface": entry["surface"],
                    "requires_user_confirmation": bool(entry.get("confirmationRequired")),
                    "permitted_effect": entry["completionEffect"],
                    "evidence_ids": ["interaction_policy"],
                })
    context = {"user_message": request.user_message, "intent": plan.intent,
        "route_plan": {key: getattr(plan, key) for key in (
            "intent", "required_tools", "knowledge_domains", "capability_ids", "needs_product_facts")},
        "required_points_source": "原请求与受控计划，不是人工金标；必须回看原请求检查计划漏项",
        "capability_results": safe_capabilities, "evidence": evidence,
        "allowed_next_steps": allowed_next_steps,
        "allowed_next_steps_source": "由代码依据受控交互策略与已注册只读路径组装，不接受模型自报的步骤列表",
        "verified_summary": answer if not issues else "证据有缺口，仅依据下列有效证据说明能确认的部分。",
        "generation_evidence_complete": True, "observed_at": datetime.now(timezone.utc).isoformat()}
    fallback = "\n\n".join(dict.fromkeys(block.strip() for block in blocks if block.strip()))
    return redact_review(context), fallback or "本轮没有取得足够的业务依据，暂不能确认所问结果。", list(dict.fromkeys(issues))


def record_review(context: dict[str, Any], candidate: str, final: str, result: dict[str, Any]) -> str:
    review_id = uuid4().hex
    entry = {"review_id": review_id, "context": context, "candidate_reply": candidate,
             "final_reply": final, "result": result}
    # 同一记录中订单及申请使用一致代号，保留对象核对价值，不把真实编号送公开Trace。
    text_ids = []
    for item in context.get("evidence", []):
        if item.get("kind") == "tool" and item.get("tool") != "search_products" and item.get("object_id"):
            text_ids.append(str(item["object_id"]))
        for historical in (item.get("facts") or {}).get("historical", []):
            if historical.get("request_id"):
                text_ids.append(str(historical["request_id"]))
    def anonymize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: anonymize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [anonymize(item) for item in value]
        if isinstance(value, str):
            for index, identifier in enumerate(dict.fromkeys(text_ids), 1):
                value = value.replace(identifier, f"业务对象{index}")
        return value
    review_audit.append(anonymize(redact_review(entry)))
    return review_id
