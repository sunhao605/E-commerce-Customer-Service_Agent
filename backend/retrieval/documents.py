"""读取第 41 课真实知识文件，并转换成公开 Citation 契约。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from api.schemas import Citation


KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "resources" / "knowledge"
KNOWLEDGE_FILES = (
    "after_sale_policy.md", "payment_invoice_policy.md", "promotion_policy.md",
    "member_coupon_policy.md", "product_knowledge.md", "received_return_policy.md",
)
POLICY_FIELDS = ("policy_id", "version", "publication_status", "effective_from", "effective_to")
SHANGHAI = ZoneInfo("Asia/Shanghai")
_REQUEST_TIME: ContextVar[datetime | None] = ContextVar("knowledge_request_time", default=None)


@dataclass(frozen=True)
class KnowledgeSnapshot:
    version: str
    citations: dict[str, Citation]
    manifest: dict[str, dict[str, str]]


def policy_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Policy time must be a timezone-aware ISO 8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Policy time must include a timezone")
    return parsed.astimezone(SHANGHAI)


def knowledge_time() -> datetime:
    return _REQUEST_TIME.get() or datetime.now(SHANGHAI)


@contextmanager
def knowledge_request_scope():
    token = _REQUEST_TIME.set(knowledge_time())
    try:
        yield
    finally:
        _REQUEST_TIME.reset(token)


def _parse_citation(name: str, raw: str) -> Citation:
    """解析材料，不把加载时刻固化为政策可用性。"""
    if not raw.startswith("---\n"):
        raise ValueError(f"{name}: knowledge must start with YAML frontmatter")
    frontmatter, separator, body = raw[4:].partition("\n---\n")
    if not separator:
        raise ValueError(f"{name}: incomplete YAML frontmatter")
    metadata: dict[str, Any] = yaml.safe_load(frontmatter) or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{name}: frontmatter must be a mapping")
    for key in POLICY_FIELDS:
        if key not in metadata:
            raise ValueError(f"{name}: missing {key}")
    for key in ("policy_id", "version"):
        if not isinstance(metadata[key], str) or not metadata[key].strip():
            raise ValueError(f"{name}: invalid {key}")
    if metadata["publication_status"] not in ("draft", "published", "withdrawn"):
        raise ValueError(f"{name}: invalid publication_status")
    try:
        start = policy_datetime(metadata["effective_from"]) if metadata["effective_from"] is not None else None
        end = policy_datetime(metadata["effective_to"]) if metadata["effective_to"] is not None else None
        if metadata["publication_status"] == "published" and start is None:
            raise ValueError("published policy requires effective_from")
        if end is not None and (start is None or end <= start):
            raise ValueError("effective_to must be later than effective_from")
    except ValueError as error:
        raise ValueError(f"{name}: {error}") from error
    return Citation(
        source=f"knowledge/{name}",
        title=str(metadata["title"]),
        snippet=body.strip(),
        score=float(metadata["score"]),
        retrieval_stage=metadata.get("retrieval_stage"),
        metadata={
            "policy_id": str(metadata["policy_id"]),
            "scene_key": str(metadata["scene_key"]),
            **{key: metadata[key] for key in POLICY_FIELDS},
        },
    )


@lru_cache(maxsize=1)
def get_knowledge_snapshot() -> KnowledgeSnapshot:
    """整批校验后才暴露快照；磁盘编辑和索引 reset 不构成发布。"""
    citations: dict[str, Citation] = {}
    manifest: dict[str, dict[str, str]] = {}
    policy_ids: set[str] = set()
    for name in KNOWLEDGE_FILES:
        path = (KNOWLEDGE_DIR / name).resolve()
        if path.parent != KNOWLEDGE_DIR.resolve():
            raise ValueError(f"Invalid knowledge name: {name}")
        raw = path.read_text(encoding="utf-8")
        citation = _parse_citation(name, raw)
        policy_id = citation.metadata["policy_id"]
        if policy_id in policy_ids:
            raise ValueError(f"{name}: duplicate policy_id {policy_id}")
        policy_ids.add(policy_id)
        citations[name] = citation
        manifest[name] = {"policy_id": policy_id, "version": citation.metadata["version"],
                          "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
    release_path = KNOWLEDGE_DIR / "release-manifest.json"
    if release_path.exists():
        previous = json.loads(release_path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            raise ValueError("release-manifest.json: expected a file/version/hash mapping")
        for name, current in manifest.items():
            old = previous.get(name)
            if old is not None and (not isinstance(old, dict) or not all(
                isinstance(old.get(key), str) and old[key] for key in ("policy_id", "version", "sha256")
            )):
                raise ValueError(f"release-manifest.json: invalid record for {name}")
            if old and old["sha256"] != current["sha256"] and old["version"] == current["version"]:
                raise ValueError(f"{name}: changed content requires a new version")
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    version = f"knowledge-{fingerprint}"
    for citation in citations.values():
        citation.metadata["snapshot_version"] = version
    return KnowledgeSnapshot(version, citations, manifest)


def load_knowledge_citation(name: str) -> Citation:
    if name not in KNOWLEDGE_FILES:
        raise ValueError(f"Invalid knowledge name: {name}")
    return get_knowledge_snapshot().citations[name].model_copy(deep=True)


def policy_block_reason(metadata: dict[str, Any], at: datetime) -> str | None:
    snapshot = get_knowledge_snapshot()
    expected = next((c.metadata for c in snapshot.citations.values()
                     if c.metadata["policy_id"] == metadata.get("policy_id")), None)
    if expected is None or any(metadata.get(key) != expected[key] for key in (*POLICY_FIELDS, "snapshot_version")):
        return "metadata_invalid"
    status = metadata["publication_status"]
    if status != "published":
        return status
    if at < policy_datetime(metadata["effective_from"]):
        return "not_yet_effective"
    if metadata["effective_to"] is not None and at >= policy_datetime(metadata["effective_to"]):
        return "expired"
    return None


def knowledge_admission(at: datetime | None = None) -> dict[str, Any]:
    at = at or knowledge_time()
    snapshot = get_knowledge_snapshot()
    blocked = {c.metadata["policy_id"]: reason for c in snapshot.citations.values()
               if (reason := policy_block_reason(c.metadata, at))}
    available = {c.metadata["policy_id"]: c.metadata["version"] for c in snapshot.citations.values()
                 if c.metadata["policy_id"] not in blocked}
    signature = hashlib.sha256(json.dumps(available, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return {"snapshot_version": snapshot.version, "judged_at": at.isoformat(),
            "available_policy_signature": signature, "available_policies": available,
            "blocked_policies": blocked}


def citation_is_current(citation: Citation, at: datetime) -> bool:
    canonical = get_knowledge_snapshot().citations.get(citation.source.removeprefix("knowledge/"))
    return canonical is not None and citation.source == canonical.source and (
        not policy_block_reason(citation.metadata or {}, at)
        and (citation.metadata or {}).get("policy_id") == canonical.metadata["policy_id"]
    )
