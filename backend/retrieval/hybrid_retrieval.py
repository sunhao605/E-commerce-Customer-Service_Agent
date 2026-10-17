"""第 41 课可离线运行的 LangChain Hybrid RAG、Query Rewrite 与索引缓存。"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from api.schemas import Citation, Intent
from settings import (
    api_key_is_missing, embedding_api_key, embedding_base_url, embedding_model_name, load_course_env,
)
from retrieval.documents import (
    citation_is_current, get_knowledge_snapshot, knowledge_admission, knowledge_time,
    load_knowledge_citation, policy_block_reason,
)


KNOWLEDGE_FILES = (
    "after_sale_policy.md",
    "payment_invoice_policy.md",
    "promotion_policy.md",
    "member_coupon_policy.md",
    "product_knowledge.md",
    "received_return_policy.md",
)
RAG_RETRIEVAL_CACHE: dict[str, "HybridRetrievalResult"] = {}
_KNOWLEDGE_INDEX: "KnowledgeIndex | None" = None


@dataclass
class KnowledgeIndex:
    version: str
    snapshot_version: str
    vector_store: InMemoryVectorStore
    documents: list[Document]
    embedding_mode: str


@dataclass
class HybridRetrievalResult:
    citations: list[Citation]
    debug: dict[str, Any]


class LocalTokenEmbeddings(Embeddings):
    """确定性字符/双字向量，只用于课程离线演示，不冒充商用语义 embedding。"""

    dimensions = 256

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        normalized = normalize_query(text)
        tokens = [*normalized, *[normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))]]
        vector = [0.0] * self.dimensions
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:2], "big") % self.dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def build_course_embeddings() -> tuple[Embeddings, str]:
    """在线默认使用真实语义 Embedding；离线替身必须由课程测试显式开启。"""
    load_course_env()
    if os.getenv("AGENT_COURSE_OFFLINE_RAG") == "1":
        return LocalTokenEmbeddings(), "local_token_embedding_for_explicit_offline_course"

    api_key = embedding_api_key()
    if api_key_is_missing(api_key):
        raise RuntimeError(
            "在线检索需要在 .env.model.local 填写 AGENT_EMBEDDING_API_KEY（硅基流动）；"
            "只做离线测试时设置 AGENT_COURSE_OFFLINE_RAG=1"
        )
    from langchain_openai import OpenAIEmbeddings

    model_name = embedding_model_name()
    return (
        OpenAIEmbeddings(
            model=model_name,
            api_key=api_key,
            base_url=embedding_base_url(),
            request_timeout=30,
            max_retries=0,
            check_embedding_ctx_length=False,
        ),
        f"openai_compatible_embedding:{model_name}",
    )


def normalize_query(text: str) -> str:
    """把常见口语别名改写成知识文件里的稳定业务词。"""
    return (
        re.sub(r"\s+", "", text.lower())
        .replace("叠券", "叠加会员券")
        .replace("优惠卷", "优惠券")
        .replace("开票", "电子发票")
        .replace("退钱", "退款")
        .replace("那个", "")
        .replace("这个", "")
    )


def build_retrieval_plan(query: str, intent: Intent) -> dict[str, Any]:
    rewritten = normalize_query(query)
    if intent == "product_query":
        for term in ("保养", "清洗", "打理"):
            rewritten = rewritten.replace(term, "清洁收纳")
        rewritten = rewritten.replace("里面有水", "帐内有水")
    domains = {
        "promotion_query": ["promotion", "member_coupon"],
        "invoice_query": ["invoice"],
        "refund_policy_query": ["after_sale_refund"],
        "refund_request": ["after_sale_refund"],
        "return_policy_query": ["after_sale_return"],
        "return_request": ["after_sale_return"],
        "product_query": ["product_knowledge"],
    }.get(intent, [])
    return {
        "original_query": query,
        "rewritten_query": rewritten,
        "intent": intent,
        "knowledge_domains": domains,
        "reason": "口语归一后按 RoutePlan 知识域执行向量与关键词双路召回。",
    }


def get_knowledge_index() -> tuple[KnowledgeIndex, bool]:
    """按真实 Markdown 内容构建一次切块和向量索引，后续请求复用。"""
    global _KNOWLEDGE_INDEX
    if _KNOWLEDGE_INDEX is not None and _KNOWLEDGE_INDEX.snapshot_version == get_knowledge_snapshot().version:
        return _KNOWLEDGE_INDEX, True

    source_documents: list[Document] = []
    snapshot = get_knowledge_snapshot()
    fingerprint_parts = [snapshot.version]
    for filename in KNOWLEDGE_FILES:
        citation = snapshot.citations[filename]
        if citation.metadata["publication_status"] != "published":
            continue
        source_documents.append(
            Document(
                page_content=citation.snippet,
                metadata={
                    "filename": filename,
                    "source": citation.source,
                    "title": citation.title,
                    "policy_id": (citation.metadata or {}).get("policy_id"),
                    "scene_key": (citation.metadata or {}).get("scene_key"),
                    "base_score": citation.score,
                    "version": (citation.metadata or {}).get("version"),
                    **(citation.metadata or {}),
                },
            )
        )
    splitter = RecursiveCharacterTextSplitter(chunk_size=220, chunk_overlap=30)
    chunks = []
    for document in source_documents:
        # 有标题的材料先按小节切开，再切成约 220 字的小块；没有标题的短规则仍是一块。
        for section in _section_documents(document):
            chunks.extend(splitter.split_documents([section]))
    for index, chunk in enumerate(chunks):
        chunk.metadata["chunk_id"] = f"{chunk.metadata['policy_id']}-chunk-{index + 1}"
    embeddings, embedding_mode = build_course_embeddings() if chunks else (LocalTokenEmbeddings(), "no_published_knowledge")
    vector_store = InMemoryVectorStore(embeddings)
    if chunks:
        vector_store.add_documents(chunks)
    fingerprint_parts.append(f"embedding:{embedding_mode}")
    fingerprint_parts.append("splitter:recursive-220-30|complete-rule-v1")
    fingerprint = hashlib.sha256("\n".join(fingerprint_parts).encode("utf-8")).hexdigest()[:12]
    _KNOWLEDGE_INDEX = KnowledgeIndex(
        version=f"lesson41-idx-{fingerprint}",
        snapshot_version=snapshot.version,
        vector_store=vector_store,
        documents=chunks,
        embedding_mode=embedding_mode,
    )
    return _KNOWLEDGE_INDEX, False


# ponytail: RRF 的 k=60、每路最多 20 块，是这块小知识库上的固定融合。离线字符向量会把字面更贴的块挤后；升级路径是换真语义向量，不再写焦点词表。
_RRF_K = 60
_FUSION_POOL = 20
_BM25_K1 = 1.5
_BM25_B = 0.75


def _section_documents(document: Document) -> list[Document]:
    rules = document.metadata.get("rules", [])
    if rules:
        return [Document(page_content=rule["text"], metadata={
            **{k: v for k, v in document.metadata.items() if k != "rules"},
            "rule_id": rule["id"], "section_id": rule["id"], "section_text": rule["text"],
            "rule_topics": rule["topics"], "scope": rule["scope"],
            "section": rule["text"].split("\n", 1)[0],
        }) for rule in rules if not rule.get("ref")]
    text = document.page_content
    if "## " not in text and not text.startswith("##"):
        return [document]
    sections = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("##", "section"), ("###", "subsection")], strip_headers=False,
    ).split_text(text)
    located: list[Document] = []
    for number, section in enumerate(sections, 1):
        section.metadata = {
            **document.metadata, **section.metadata,
            "section_id": f"{document.metadata['policy_id']}-section-{number}",
            "section_text": section.page_content,
        }
        located.append(section)
    return located


def _literal_terms(text: str) -> list[str]:
    normalized = normalize_query(text)
    terms = [
        normalized[index:index + 2]
        for index in range(len(normalized) - 1)
        if re.fullmatch(r"[\u4e00-\u9fff]{2}", normalized[index:index + 2])
    ]
    terms.extend(re.findall(r"[a-z0-9]+", normalized))
    return terms


def _bm25_scores(query: str, documents: list[Document]) -> dict[str, float]:
    """Okapi BM25。中文按连续二字找字面，英文和数字按整词。"""
    query_terms = list(dict.fromkeys(_literal_terms(query)))
    if not query_terms or not documents:
        return {}
    counts = [Counter(_literal_terms(document.page_content)) for document in documents]
    total = len(documents)
    document_frequency = {term: sum(term in counted for counted in counts) for term in query_terms}
    average = sum(sum(counted.values()) for counted in counts) / total
    scores: dict[str, float] = {}
    for document, counted in zip(documents, counts):
        length = max(1, sum(counted.values()))
        score = 0.0
        for term in query_terms:
            frequency = counted.get(term, 0)
            seen = document_frequency[term]
            if frequency <= 0 or seen <= 0:
                continue
            idf = math.log((total - seen + 0.5) / (seen + 0.5) + 1.0)
            norm = frequency + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average)
            score += idf * (frequency * (_BM25_K1 + 1)) / norm
        if score > 0:
            scores[str(document.metadata["chunk_id"])] = score
    return scores


def retrieve_knowledge(query: str, intent: Intent, *, top_k: int = 4) -> HybridRetrievalResult:
    """Query Rewrite 后，向量和 BM25 各取一批小块，按名次合并，把前 top_k 块放进引用。"""
    plan = build_retrieval_plan(query, intent)
    at = knowledge_time()
    admission = knowledge_admission(at)
    index, index_cache_hit = get_knowledge_index()
    cache_key = hashlib.sha256(f"{index.version}|{admission['available_policy_signature']}|{intent}|{top_k}|{plan['rewritten_query']}".encode("utf-8")).hexdigest()[:16]
    if cache_key in RAG_RETRIEVAL_CACHE:
        cached = RAG_RETRIEVAL_CACHE[cache_key]
        if all(citation_is_current(citation, at) for citation in cached.citations):
            return HybridRetrievalResult(
                citations=[citation.model_copy(deep=True) for citation in cached.citations],
                debug={**cached.debug, **admission, "retrieval_cache_hit": True, "index_cache_hit": True},
            )
        del RAG_RETRIEVAL_CACHE[cache_key]

    domains = set(plan["knowledge_domains"])
    def allowed(document: Document) -> bool:
        return not policy_block_reason(document.metadata, at) and _domain_allowed(str(document.metadata.get("scene_key")), domains) and (
            intent != "product_query" or _product_section_allowed(document, plan["rewritten_query"])
        )
    # InMemoryVectorStore 返回的就是余弦相似度。候选池比最终 top_k 宽，避免对的小块没进向量名单。
    allowed_documents = [document for document in index.documents if allowed(document)]
    vector_pairs = index.vector_store.similarity_search_with_score(
        plan["rewritten_query"], k=max(top_k, _FUSION_POOL), filter=allowed,
    ) if allowed_documents else []
    by_chunk = {str(document.metadata["chunk_id"]): document for document in allowed_documents}
    vector_scores: dict[str, float] = {}
    for document, score in vector_pairs:
        chunk_id = str(document.metadata["chunk_id"])
        if chunk_id in by_chunk:
            vector_scores[chunk_id] = max(vector_scores.get(chunk_id, -1.0), float(score))
    bm25_scores = _bm25_scores(plan["rewritten_query"], allowed_documents)
    vector_order = sorted(vector_scores, key=lambda chunk_id: (-vector_scores[chunk_id], chunk_id))[:_FUSION_POOL]
    bm25_order = sorted(bm25_scores, key=lambda chunk_id: (-bm25_scores[chunk_id], chunk_id))[:_FUSION_POOL]
    vector_rank = {chunk_id: rank for rank, chunk_id in enumerate(vector_order, 1)}
    bm25_rank = {chunk_id: rank for rank, chunk_id in enumerate(bm25_order, 1)}
    fused: list[tuple[str, float, int, int]] = []
    for chunk_id in set(vector_rank) | set(bm25_rank):
        score = 0.0
        if chunk_id in vector_rank:
            score += 1.0 / (_RRF_K + vector_rank[chunk_id])
        if chunk_id in bm25_rank:
            score += 1.0 / (_RRF_K + bm25_rank[chunk_id])
        fused.append((chunk_id, score, bm25_rank.get(chunk_id, 10**9), vector_rank.get(chunk_id, 10**9)))
    # 名次分打平时让字面更靠前的块赢，避免离线向量单独把不对的块顶到第一。
    fused.sort(key=lambda item: (-item[1], item[2], item[3], item[0]))
    chosen = fused[:top_k]
    top_rrf = chosen[0][1] if chosen else 1.0

    def policy_of(chunk_id: str) -> str:
        return str(by_chunk[chunk_id].metadata["policy_id"])

    policy_vector: dict[str, float] = {}
    policy_bm25: dict[str, float] = {}
    for chunk_id, score in vector_scores.items():
        policy_id = policy_of(chunk_id)
        policy_vector[policy_id] = max(policy_vector.get(policy_id, 0.0), score)
    for chunk_id, score in bm25_scores.items():
        policy_id = policy_of(chunk_id)
        policy_bm25[policy_id] = max(policy_bm25.get(policy_id, 0.0), score)
    citations: list[Citation] = []
    source_scores: dict[str, Any] = {}
    for chunk_id, rrf, _bm25_place, _vector_place in chosen:
        document = by_chunk[chunk_id]
        policy_id = policy_of(chunk_id)
        citation = load_knowledge_citation(str(document.metadata["filename"]))
        metadata = {**{k: v for k, v in (citation.metadata or {}).items() if k != "rules"}, "chunk_ids": [chunk_id]}
        for key in ("rule_id", "rule_topics", "scope"):
            if key in document.metadata:
                metadata[key] = document.metadata[key]
        section_id = document.metadata.get("section_id")
        if section_id:
            metadata["section_id"] = section_id
            metadata["section"] = document.metadata.get("subsection") or document.metadata.get("section") or "文首说明"
        display = round(rrf / top_rrf, 3)
        citations.append(citation.model_copy(update={"score": display, "snippet": document.metadata.get("section_text", document.page_content).strip(), "metadata": metadata}))
        current = source_scores.get(policy_id)
        if current:
            current["final"] = max(current["final"], display)
            continue
        reasons = []
        if policy_vector.get(policy_id):
            reasons.append("vector召回")
        if policy_bm25.get(policy_id):
            reasons.append("bm25召回")
        source_scores[policy_id] = {
            "vector": round(policy_vector.get(policy_id, 0.0), 3),
            "bm25": round(policy_bm25.get(policy_id, 0.0), 3),
            "final": display,
            "sources": reasons,
        }
    result = HybridRetrievalResult(
        citations=citations,
        debug={
            **admission,
            "mode": "langchain_inmemory_hybrid",
            "fusion": "rrf",
            "embedding": index.embedding_mode,
            "plan": plan,
            "index_version": index.version,
            "index_chunk_count": len(index.documents),
            "index_cache_hit": index_cache_hit,
            "retrieval_cache_hit": False,
            "cache_key": cache_key,
            "vector_policy_ids": list(dict.fromkeys(policy_of(chunk_id) for chunk_id in vector_order)),
            "keyword_policy_ids": list(dict.fromkeys(policy_of(chunk_id) for chunk_id in bm25_order)),
            "source_scores": source_scores,
        },
    )
    RAG_RETRIEVAL_CACHE[cache_key] = HybridRetrievalResult(
        citations=[citation.model_copy(deep=True) for citation in citations], debug=dict(result.debug),
    )
    return result


def knowledge_index_status() -> dict[str, Any]:
    index = _KNOWLEDGE_INDEX
    return {"loaded": index is not None, "version": index.version if index else None,
            "chunk_count": len(index.documents) if index else 0,
            "files": list(KNOWLEDGE_FILES) if index else [],
            "embedding": index.embedding_mode if index else None,
            "governed_files": list(get_knowledge_snapshot().citations),
            **knowledge_admission()}


def reset_hybrid_index_and_cache() -> None:
    global _KNOWLEDGE_INDEX
    _KNOWLEDGE_INDEX = None
    RAG_RETRIEVAL_CACHE.clear()


def _product_section_allowed(document: Document, query: str) -> bool:
    groups = (("帐篷", "帐内", "天幕"), ("折叠椅", "露营椅", "折叠桌", "桌椅"),
              ("收纳车", "收纳柜"), ("野餐垫", "防水袋", "收纳袋"),
              ("背包", "登山杖"), ("睡袋",), ("照明", "头灯", "营地灯"),
              ("炊具",), ("定制", "营地旗", "营地标识"))
    requested = [group for group in groups if any(term in query for term in group)]
    if not requested:
        return True
    heading = str(document.metadata.get("subsection") or document.metadata.get("section") or "")
    if not any(term in heading for group in groups for term in group):
        heading = str(document.metadata.get("section") or "")
    return any(term in heading for group in requested for term in group)


def _domain_allowed(scene_key: str, domains: set[str]) -> bool:
    if not domains:
        return True
    mapping = {
        "promotion": "promotion",
        "member_coupon": "member_coupon",
        "invoice": "invoice",
        "payment_invoice": "invoice",
        "after_sale": "after_sale",
    }
    return mapping.get(scene_key, scene_key) in domains
