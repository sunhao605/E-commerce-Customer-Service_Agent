"""课程内置知识片段。总演习把售后、发票、活动和会员规则集中成可引用 Citation。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from api.schemas import *
from retrieval.documents import knowledge_admission, knowledge_time, load_knowledge_citation, policy_block_reason
from retrieval.hybrid_retrieval import retrieve_knowledge


@dataclass(frozen=True)
class KnowledgePathResult:
    """稳定知识路径的确定性结果，供 Agent 编排层直接拼装响应。"""

    answer: str
    citations: list[Citation]
    risk_level: RiskLevel
    next_action: NextAction
    needs_human_approval: bool
    cache_hit: bool = False
    rerank: dict[str, Any] | None = None
    retrieval_debug: dict[str, Any] | None = None
    trace_events: tuple[tuple[str, dict[str, Any]], ...] = ()


def low_confidence_result(session_id: str, intent: Intent) -> KnowledgePathResult:
    """纯知识低置信场景保守兜底，不让模型编造隐藏规则。"""
    return KnowledgePathResult(
        answer="没有检索到荒野户外已发布的可信活动或会员规则，我不能编造隐藏券规则。建议以活动页和结算页展示为准，或转人工客服进一步核实。",
        citations=[],
        risk_level="medium",
        next_action="transfer_to_human",
        needs_human_approval=False,
        trace_events=(
            (
                "rag_low_confidence_fallback",
                {
                    "session_id": session_id,
                    "intent": intent,
                    "hit_count": 0,
                    "retrieval_stage": "pre_retrieval",
                    "pending_action": "transfer_to_human",
                    "status": "low_confidence",
                },
            ),
        ),
    )


def _plain_evidence(snippet: str) -> str:
    text = re.sub(r"(?m)^#{1,6}[^\n]*(?:\n|$)", "", snippet).strip()
    return re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)


def policy_query_result(session_id: str, intent: Intent, user_message: str = "") -> KnowledgePathResult:
    """退款、退货先确认这份政策可用，再把合并后的前几块放进引用。不读、不写发票缓存，也不准备申请。"""
    filename = "after_sale_policy.md" if intent in {"refund_policy_query", "refund_request"} else "received_return_policy.md"
    citation = load_knowledge_citation(filename)
    at = knowledge_time()
    admission = knowledge_admission(at)
    reason = policy_block_reason(citation.metadata, at)
    if reason:
        return unavailable_policy_result(session_id, intent, {
            **admission, "mode": "hybrid_chunk_fusion", "reason": reason,
            "policy_id": citation.metadata["policy_id"],
        })
    retrieval = retrieve_knowledge(user_message or str(citation.title), intent)
    policy_id = citation.metadata["policy_id"]
    located = [item for item in retrieval.citations if (item.metadata or {}).get("policy_id") == policy_id]
    if not located:
        return unavailable_policy_result(session_id, intent, {
            **retrieval.debug, "mode": "hybrid_chunk_fusion", "reason": "no_matching_section",
            "policy_id": policy_id,
        })
    text = "\n".join(piece for piece in (_plain_evidence(item.snippet) for item in located) if piece)
    return KnowledgePathResult(
        answer=text or located[0].snippet,
        citations=located,
        risk_level="low",
        next_action="answer_user",
        needs_human_approval=False,
        retrieval_debug={**retrieval.debug, "mode": "hybrid_chunk_fusion"},
        trace_events=(
            (
                "rag_pre_retrieved",
                {
                    "session_id": session_id,
                    "intent": intent,
                    "hit_count": len(located),
                    "retrieval_stage": "pre_retrieval",
                    "policy_id": policy_id,
                    "section_id": (located[0].metadata or {}).get("section_id"),
                },
            ),
        ),
    )


def unavailable_policy_result(session_id: str, intent: Intent, debug: dict[str, Any]) -> KnowledgePathResult:
    label = {"promotion_query": "活动或会员规则", "invoice_query": "发票规则",
             "refund_policy_query": "退款政策", "refund_request": "退款政策"}.get(intent, "退货政策")
    return KnowledgePathResult(
        answer=f"目前没有核实到可用于回答的有效{label}，暂不能说明相应政策条件。已核实的商品或订单事实仍以业务查询结果为准。",
        citations=[], risk_level="medium", next_action="answer_user", needs_human_approval=False,
        retrieval_debug=debug,
        trace_events=(("rag_policy_blocked", {"session_id": session_id, "intent": intent, **debug}),),
    )


def designated_policy_result(session_id: str, filename: str, intent: Intent) -> KnowledgePathResult:
    citation = load_knowledge_citation(filename)
    at = knowledge_time()
    debug = {**knowledge_admission(at), "mode": "designated_policy"}
    reason = policy_block_reason(citation.metadata, at)
    if reason:
        return unavailable_policy_result(session_id, intent, {**debug, "reason": reason,
            "policy_id": citation.metadata["policy_id"]})
    policy_id = (citation.metadata or {}).get("policy_id")
    return KnowledgePathResult(
        answer=citation.snippet,
        citations=[citation],
        risk_level="low",
        next_action="answer_user",
        needs_human_approval=False,
        retrieval_debug=debug,
        trace_events=(
            (
                "rag_pre_retrieved",
                {
                    "session_id": session_id,
                    "intent": intent,
                    "hit_count": 1,
                    "retrieval_stage": "pre_retrieval",
                    "policy_id": policy_id,
                    **debug,
                },
            ),
        ),
    )


def invoice_faq_result(session_id: str) -> KnowledgePathResult:
    """每轮先检查政策；停用最终答案缓存，保留底层检索缓存。"""
    policy = designated_policy_result(session_id, "payment_invoice_policy.md", "invoice_query")
    if not policy.citations:
        return policy
    retrieval = retrieve_knowledge("电子发票通常多久能准备好", "invoice_query")
    citation = next(
        (item for item in retrieval.citations if (item.metadata or {}).get("policy_id") == "invoice_issue"),
        policy.citations[0],
    )
    answer = citation.snippet
    return KnowledgePathResult(
        answer=answer,
        citations=[citation],
        risk_level="low",
        next_action="answer_user",
        needs_human_approval=False,
        retrieval_debug=retrieval.debug,
        trace_events=(
            (
                "rag_pre_retrieved",
                {
                    "session_id": session_id,
                    "hit_count": 1,
                    "retrieval_stage": "pre_retrieval",
                    "policy_id": "invoice_issue",
                },
            ),
        ),
    )


def promotion_policy_result(session_id: str, user_message: str, *, clarification_required: bool | None = None) -> KnowledgePathResult:
    """活动和会员券问题先过证据门，再用课程版 reranker 决定最终引用顺序。"""
    normalized = user_message.replace(" ", "").lower()
    if clarification_required is True:
        return KnowledgePathResult(
            answer="会员券想和哪项优惠叠加？例如露营季满减、商品活动价，还是另一张券？",
            citations=[], risk_level="low", next_action="ask_clarification", needs_human_approval=False,
            retrieval_debug={"mode": "clarification_required", "reason": "promotion_combination"},
            trace_events=(("rag_clarification_required", {"session_id": session_id, "clarification_field": "promotion_combination"}),),
        )
    if any(term in normalized for term in ("隐藏券", "火星会员", "不存在的活动", "未知活动", "未发布")):
        candidates: list[tuple[Citation, float, list[str]]] = []
        retrieval_debug = {"mode": "evidence_gate_blocked_before_retrieval", "reason": "unsupported_policy_claim"}
    else:
        retrieval = retrieve_knowledge(user_message, "promotion_query")
        retrieval_debug = retrieval.debug
        allowed_policy_ids = _promotion_scope_policy_ids(normalized)
        candidates = [
            (
                citation,
                citation.score,
                list(
                    retrieval.debug.get("source_scores", {})
                    .get((citation.metadata or {}).get("policy_id"), {})
                    .get("sources", [])
                ),
            )
            for citation in retrieval.citations
            if (citation.metadata or {}).get("policy_id") in allowed_policy_ids
        ]
        if "叠加" in normalized and len(candidates) < 2:
            candidates = []
    reranked = _rerank_promotion_candidates(user_message, candidates)
    citations = [citation for citation, _score, _reasons in reranked]
    if not citations:
        if any(policy_id in retrieval_debug.get("blocked_policies", {})
               for policy_id in _promotion_scope_policy_ids(normalized)):
            return unavailable_policy_result(session_id, "promotion_query", retrieval_debug)
        result = low_confidence_result(session_id, "promotion_query")
        return KnowledgePathResult(
            answer=result.answer,
            citations=result.citations,
            risk_level=result.risk_level,
            next_action=result.next_action,
            needs_human_approval=result.needs_human_approval,
            retrieval_debug=retrieval_debug,
            trace_events=(
                (
                    "rag_evidence_gate_blocked",
                    {
                        "session_id": session_id,
                        "intent": "promotion_query",
                        "hit_count": 0,
                        "retrieval_stage": "pre_retrieval",
                        "status": "low_confidence",
                        "reason": "no_trusted_policy_citation",
                    },
                ),
                *result.trace_events,
            ),
        )
    rerank_debug = _build_rerank_debug(reranked)
    rerank_debug["retrieval"] = retrieval_debug
    return KnowledgePathResult(
        answer=_promotion_policy_answer(citations),
        citations=citations,
        risk_level="low",
        next_action="answer_user",
        needs_human_approval=False,
        rerank=rerank_debug,
        retrieval_debug=retrieval_debug,
        trace_events=(
            (
                "rag_pre_retrieved",
                {
                    "session_id": session_id,
                    "hit_count": len(citations),
                    "retrieval_stage": "pre_retrieval",
                    "policy_id": "promotion_camping_season_stack_rule",
                    "candidate_policy_ids": [
                        citation.metadata.get("policy_id") for citation, _score, _reasons in candidates if citation.metadata
                    ],
                },
            ),
            (
                "rag_reranked",
                {
                    "session_id": session_id,
                    "mode": rerank_debug["mode"],
                    "reranked_policy_ids": rerank_debug["policy_ids"],
                    "top_policy_id": rerank_debug["policy_ids"][0] if rerank_debug["policy_ids"] else None,
                    "rerank_reasons": rerank_debug["reasons"],
                },
            ),
        ),
    )


def product_knowledge_result(session_id: str, user_message: str) -> KnowledgePathResult:
    retrieval = retrieve_knowledge(user_message, "product_query", top_k=3)
    citations = retrieval.citations
    if citations:
        # 回复保留事实与限制；原始 Markdown、来源和版本留在 Citation 中。模型开启时这些小块进入提示词。
        text = "\n".join(piece for piece in (_plain_evidence(item.snippet) for item in citations) if piece)
        answer = "通用使用建议（具体型号以说明书为准）：\n" + text
    else:
        answer = "没有找到支持这个问题的有效商品使用知识，请补充商品类别和具体问题。"
    return KnowledgePathResult(
        answer=answer, citations=citations, risk_level="low", next_action="answer_user",
        needs_human_approval=False, retrieval_debug=retrieval.debug,
        trace_events=(("rag_pre_retrieved", {"session_id": session_id, "intent": "product_query",
            "policy_id": "outdoor_product_guide", "hit_count": len(citations),
            "section_ids": [(c.metadata or {}).get("section_id") for c in citations]}),),
    )


def _promotion_scope_policy_ids(normalized_query: str) -> set[str]:
    """单一问题只引用对应规则；明确问叠加时才联合两类证据。"""
    asks_promotion = any(term in normalized_query for term in ("露营季", "大促", "活动", "满减", "300减40"))
    asks_member = any(term in normalized_query for term in ("会员", "会员券", "山友", "优惠券"))
    if asks_promotion and not asks_member:
        return {"promotion_camping_season_stack_rule"}
    if asks_member and not asks_promotion:
        return {"member_coupon_gold_rule"}
    return {"promotion_camping_season_stack_rule", "member_coupon_gold_rule"}


def _rerank_promotion_candidates(
    user_message: str,
    candidates: list[tuple[Citation, float, list[str]]],
) -> list[tuple[Citation, float, list[str]]]:
    """课程版轻量 reranker：在候选池里按当前问题的业务约束重新排序。

    这里刻意不调用外部商业模型，避免第 41 课总演习依赖网络；但保留 reranker 的核心闭环：
    初召回候选、按问题重排、citation 跟随最终排序。
    """
    normalized = user_message.replace(" ", "").lower()
    reranked: list[tuple[Citation, float, list[str]]] = []
    for citation, score, reasons in candidates:
        policy_id = citation.metadata.get("policy_id") if citation.metadata else ""
        final_score = score
        final_reasons = list(reasons)
        if policy_id == "promotion_camping_season_stack_rule" and any(term in normalized for term in ("露营季", "满减", "300减40", "大促")):
            final_score += 0.18
            final_reasons.append("当前大促规则加权")
        if policy_id == "member_coupon_gold_rule" and any(term in normalized for term in ("山友", "会员券")):
            final_score += 0.16
            final_reasons.append("会员券条件匹配")
        if "叠加" in normalized:
            final_score += 0.08
            final_reasons.append("叠加问题需要联合引用")
        reranked.append((citation, round(min(1.0, final_score), 3), final_reasons))
    return sorted(reranked, key=lambda item: item[1], reverse=True)


def _build_rerank_debug(reranked: list[tuple[Citation, float, list[str]]]) -> dict[str, Any]:
    """把 rerank 结果压成公开调试状态，方便第 41 课观察最终闭环。"""
    policy_ids = [citation.metadata.get("policy_id") for citation, _score, _reasons in reranked if citation.metadata]
    return {
        "mode": "lesson41_lightweight",
        "policy_ids": policy_ids,
        "scores": {citation.metadata.get("policy_id"): score for citation, score, _reasons in reranked if citation.metadata},
        "reasons": {citation.metadata.get("policy_id"): reasons for citation, _score, reasons in reranked if citation.metadata},
    }


def _promotion_policy_answer(citations: list[Citation]) -> str:
    """按实际命中的 citation 组织回答，避免单一证据问题被迫套完整叠加规则。"""
    policy_ids = {citation.metadata.get("policy_id") for citation in citations if citation.metadata}
    if {"promotion_camping_season_stack_rule", "member_coupon_gold_rule"}.issubset(policy_ids):
        return (
            "根据荒野户外 露营季活动规则，满 300 减 40 可以与平台会员券叠加，"
            "但不能与同类型满减券重复叠加；山友会员券需要在有效期内由本人账号使用。"
        )
    if "promotion_camping_season_stack_rule" in policy_ids:
        return "根据荒野户外 露营季活动规则，满 300 减 40 活动可用，但不能与同类型满减券重复叠加。"
    if "member_coupon_gold_rule" in policy_ids:
        return "根据荒野户外会员券使用规则，山友会员可领取平台会员券；会员券需在有效期内由本人账号使用，不能转让。"
    return "没有检索到荒野户外已发布的可信活动或会员规则，我不能编造隐藏券规则。建议以活动页和结算页展示为准，或转人工客服进一步核实。"
