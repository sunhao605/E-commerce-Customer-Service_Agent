"""同一知识快照中的完整规则；主题、身份和条件只在知识源维护。"""
from __future__ import annotations

import json
import re
from typing import Any

from api.schemas import Citation
from retrieval.documents import get_knowledge_snapshot, citation_is_current, knowledge_time

RULE_PATTERN = re.compile(r'<!-- rule (.*?) -->\s*(.*?)\s*<!-- /rule -->', re.S)


def parse_rules(body: str) -> tuple[list[dict[str, Any]], list[str]]:
    rules, issues = [], []
    for match in RULE_PATTERN.finditer(body):
        try:
            item = json.loads(match[1])
            if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k]
                                               for k in ('id', 'domain', 'scope', 'label')):
                raise ValueError('identity')
            for key in ('topics', 'requires'):
                if not isinstance(item.get(key), list) or any(not isinstance(v, str) or not v for v in item[key]):
                    raise ValueError('bindings')
            if not item['topics'] or (not match[2].strip() and item.get('ref') != item['id']):
                raise ValueError('incomplete')
            rules.append({**item, 'text': match[2].strip()})
        except (ValueError, TypeError):
            issues.append('invalid_rule_annotation')
    if '<!-- rule ' in RULE_PATTERN.sub('', body):
        issues.append('unclosed_rule_annotation')
    return rules, list(dict.fromkeys(issues))


def topic_catalog() -> dict[str, str]:
    return {topic: rule['domain'] for citation in get_knowledge_snapshot().citations.values()
            for rule in (citation.metadata or {}).get('rules', []) for topic in rule['topics']}


def validate_topics(value: Any, *, present: bool = True) -> tuple[list[str], str]:
    if not present:
        return [], 'missing'
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return [], 'invalid_type'
    if len(value) > 12:
        return [], 'too_many'
    catalog = topic_catalog()
    topics = list(dict.fromkeys(v for v in value if v in catalog))
    if any(v not in catalog for v in value):
        return topics, 'unknown_id'
    if len(value) != len(set(value)):
        return topics, 'duplicate'
    return topics, 'valid' if topics else 'empty'


def select_rules(candidates: list[Citation], topics: list[str], domains: list[str],
                 topic_status: str = 'valid') -> tuple[list[Citation], dict[str, Any]]:
    """按受控主题查有效快照；依赖闭包保留完整条件，冲突规则整项留空。"""
    at = knowledge_time()
    available: dict[str, list[tuple[Citation, dict[str, Any]]]] = {}
    excluded = []
    invalid_candidates = [c.source for c in candidates if not citation_is_current(c, at)]
    admitted_domains = {r["domain"] for candidate in candidates if citation_is_current(candidate, at)
        for r in get_knowledge_snapshot().citations[candidate.source.removeprefix("knowledge/")].metadata.get("rules", [])}
    for citation in get_knowledge_snapshot().citations.values():
        if not citation_is_current(citation, at):
            continue
        for rule in (citation.metadata or {}).get('rules', []):
            if rule['domain'] in domains and rule['domain'] in admitted_domains:
                available.setdefault(rule['id'], []).append((citation, rule))
    chosen: dict[tuple[str, str, str], Citation] = {}
    gaps: list[str] = ['rule_annotation_invalid:' + c.source for c in candidates
        if citation_is_current(c, at) and get_knowledge_snapshot().citations[
            c.source.removeprefix('knowledge/')].metadata.get('rule_issues')]

    def include(rid: str, visiting: frozenset[str] = frozenset(), required_scope: str | None = None) -> bool:
        entries = available.get(rid, [])
        canonical = [(c, r) for c, r in entries if not r.get('ref')
                     and (required_scope is None or r['scope'] == required_scope)]
        if rid in visiting or not canonical:
            gaps.append('rule_missing:' + rid)
            return False
        # Different scopes remain separate. Conflicting versions or conditions never take first wins.
        scopes = sorted({r['scope'] for _, r in canonical})
        prepared = []
        for scope in scopes:
            group = [(c, r) for c, r in canonical if r['scope'] == scope]
            signatures = {(c.metadata['version'], c.metadata['effective_from'], c.metadata['effective_to'],
                           r['text'], tuple(r['requires'])) for c, r in group}
            if len(signatures) != 1:
                gaps.append('rule_conflict:' + rid)
                return False
            c, r = group[0]
            dependencies_ok = [include(dep, visiting | {rid}, scope) for dep in r['requires']]
            if not all(dependencies_ok):
                gaps.append('rule_conditions_missing:' + rid)
                return False
            sources = [{'source': src.source, 'policy_id': src.metadata['policy_id'],
                        'version': src.metadata['version']} for src, rule in entries if rule['scope'] == scope and src.metadata['version'] == c.metadata['version']]
            metadata = {k: v for k, v in c.metadata.items() if k != 'rules'}
            metadata.update(rule_id=rid, rule_topics=r['topics'], required_rules=r['requires'], scope=scope, rule_sources=sources,
                            section_id=rid, section=r["text"].split("\n", 1)[0] if r["text"].startswith("#") else r.get("label", rid))
            prepared.append(((rid, scope, c.metadata['version']), c.model_copy(update={
                'snippet': r['text'], 'metadata': metadata})))
        for key, citation in prepared:
            chosen[key] = citation
        return True

    roots = []
    root_topics = {}
    for topic in topics:
        ids = list(dict.fromkeys(r['id'] for entries in available.values() for _, r in entries
                                 if topic in r['topics']))
        if not ids:
            gaps.append('topic_unconfirmed:' + topic)
        for rid in ids:
            roots.append(rid)
            root_topics.setdefault(rid, topic)
            include(rid)
    if topic_status != 'valid':
        gaps.append('topics_' + topic_status)
    for candidate in candidates:
        excluded.append({'source': candidate.source, 'section_id': (candidate.metadata or {}).get('section_id'),
                         'reason': 'selected_by_governed_topic_and_complete_rule'})
    selected: list[Citation] = []
    emitted: set[str] = set()
    def emit(rid: str, topic: str) -> None:
        if rid in emitted:
            return
        emitted.add(rid)
        for citation in chosen.values():
            if citation.metadata["rule_id"] == rid:
                citation.metadata["answer_topic"] = topic
                selected.append(citation)
                for dependency in citation.metadata["required_rules"]:
                    emit(dependency, topic)
    for rid in [*roots, *(c.metadata["rule_id"] for c in chosen.values())]:
        emit(rid, root_topics.get(rid, ""))
    return selected, {'topics': topics, 'topic_status': topic_status,
        'selected_rules': [c.metadata['rule_id'] for c in selected],
        'gaps': list(dict.fromkeys(gaps)), 'invalid_candidates': invalid_candidates, 'retrieved_candidates': excluded,
        'snapshot_version': get_knowledge_snapshot().version}


def rules_answer(citations: list[Citation], selection: dict[str, Any]) -> str:
    texts = [re.sub(r'(?m)^#{1,6}[^\n]*(?:\n|$)', '', c.snippet).strip() for c in citations]
    labels = {r['id']: r.get('label', '所问事项') for c in get_knowledge_snapshot().citations.values() for r in c.metadata.get('rules', [])}
    for gap in selection['gaps']:
        if gap.startswith('topics_'):
            texts.append('本轮尚未明确您要咨询的具体规则，请补充想了解的事项；已核实的其他结果仍然有效。')
        else:
            texts.append(f"关于{labels.get(gap.split(':', 1)[-1], '所问事项')}，本轮未取得完整且一致的有效规则，暂不能确认。")
    return '\n\n'.join(dict.fromkeys(t for t in texts if t))
