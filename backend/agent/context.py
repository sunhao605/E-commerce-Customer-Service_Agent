"""把 Runtime Context、短期记忆和历史消息整理成受控模型上下文。"""

from __future__ import annotations

import re
from typing import Any

from api.schemas import ChatRequest, HistoryMessage, Intent, ClarificationRequest, RoutePlan
from agent.source_guard import inspect_source
from agent.tools.planning import estimate_tokens


SESSION_MEMORIES: dict[str, dict[str, Any]] = {}
RECENT_WINDOW_SIZE = 4
MAX_HISTORY_TOKENS = 80
_PHONE_PATTERN = re.compile(r"\b1[3-9]\d{9}\b")
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def current_memory(session_id: str, runtime_user_id: str | None = None) -> dict[str, Any]:
    """短期记忆只保存当前会话内已验证的最近订单和最近意图。"""
    memory = SESSION_MEMORIES.get(session_id)
    if memory is None or (runtime_user_id and memory.get("runtime_user_id") not in {None, runtime_user_id}):
        memory = {
            "runtime_user_id": runtime_user_id,
            "last_order_id": None,
            "last_product_name": None,
            # 商品待选与已选编号独立保存，不共享退款 open_task，也不写入 Java 会话。
            "product_query": None,
            "recent_intent": None,
            "open_task": None,
            "low_risk_preferences": {},
            "write_decisions": [],
            "excluded_items": [],
            "ttl": "session",
        }
        SESSION_MEMORIES[session_id] = memory
    elif runtime_user_id and memory.get("runtime_user_id") is None:
        memory["runtime_user_id"] = runtime_user_id
    old_task = memory.get('open_task') or {}
    if old_task.get('intent') in {'order_query', 'refund_status_query'}:
        memory.setdefault('order_query', old_task)
        memory['open_task'] = None
        memory.setdefault('clarification_focus', 'order_query')
    return memory


def focused_order_task(memory: dict[str, Any], open_task: dict[str, Any] | None,
                       eligibility_query: dict[str, Any] | None) -> dict[str, Any] | None:
    tasks = {'open_task': open_task, 'order_query': memory.get('order_query'),
             'eligibility_query': eligibility_query}
    # 旧会话没有焦点字段；显式None表示上一项已完成，不自动恢复后台售后。
    focus = memory.get('clarification_focus', 'eligibility_query'
                       if (eligibility_query or {}).get('pending_slot') else 'open_task')
    task = tasks.get(focus)
    return task if task and task.get('pending_slot') else None


def update_pending_tasks(memory: dict[str, Any], *, plan: RoutePlan,
                         clarification: ClarificationRequest | None, next_action: str,
                         verified_order_id: str | None, workflow: dict[str, Any] | None,
                         eligibility: dict[str, Any] | None, order_id: str | None) -> None:
    """只由目标组件更新其状态；没有合法进展事件就保留原售后。"""
    if plan.requires_workflow:
        key = 'open_task'
    elif plan.intent in {'order_query', 'refund_status_query', 'after_sale_eligibility_query'}:
        key = 'eligibility_query' if plan.intent == 'after_sale_eligibility_query' else 'order_query'
    elif plan.intent == 'product_query':
        key = 'product_query'
        if next_action == 'ask_clarification':
            memory['clarification_focus'] = key
        elif memory.get('clarification_focus') == key:
            memory['clarification_focus'] = None
        return
    elif plan.intent == 'promotion_query':
        if next_action == 'ask_clarification':
            memory['clarification_focus'] = 'promotion_query'
        elif memory.get('clarification_focus') == 'promotion_query':
            memory['clarification_focus'] = None
        return
    else:
        return
    if clarification is not None and next_action == 'ask_clarification':
        previous = memory.get(key) or {}
        previous = previous if previous.get('intent') == plan.intent else {}
        memory[key] = {**previous, 'intent': plan.intent, 'pending_slot': clarification.clarification_field,
                       'candidates': [{'value': c.value, 'label': c.label} for c in clarification.candidates]}
        if key != 'open_task':
            memory[key]['order_id'] = None
        if key == 'eligibility_query':
            memory[key].update(order_id=None, application_type=None)
        memory['clarification_focus'] = key
    elif key == 'open_task':
        if workflow is not None and verified_order_id:
            memory[key] = None
            if memory.get('clarification_focus') == key:
                memory['clarification_focus'] = None
    elif verified_order_id or eligibility is not None:
        memory[key] = {'intent': plan.intent, 'order_id': order_id,
                       'pending_slot': None, 'candidates': []}
        if key == 'eligibility_query':
            memory[key]['application_type'] = (eligibility or {}).get('applicationType')
        if memory.get('clarification_focus') == key:
            memory['clarification_focus'] = None


def build_context(request: ChatRequest, explicit_order_id: str | None, *, query_task: dict[str, Any] | None = None,
                  eligibility_query: bool = False, selection_task: dict[str, Any] | None = None,
                  allow_embedded_selection: bool = False) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """明确订单或候选选择优先；无效候选选择不得回落到页面或历史订单。"""
    memory = current_memory(request.session_id, request.runtime_user_id)
    page = request.runtime_context or {}
    page_order_id = page.get("current_order_id") or page.get("relatedOrderNo")
    remembered_order = (query_task or {}).get("order_id") if eligibility_query else memory.get("last_order_id")
    chosen_order_id = explicit_order_id or page_order_id or remembered_order
    selection_message = page.get("userMessage") or request.user_message
    selection = re.fullmatch(r"(?:我选择\s*)?第([0-9一二三四五六七八九十]{1,3})笔[。！!？?\s]*", selection_message.strip())
    if not selection and allow_embedded_selection:
        selections = list(re.finditer(r"第([0-9一二三四五六七八九十]{1,3})笔", selection_message))
        selection = selections[0] if len(selections) == 1 else None
    if selection:
        numeral = selection.group(1)
        # shortcut: 中文序号只支持一至十（商城默认展示十笔），扩容候选时再扩展。
        index = int(numeral) if numeral.isascii() and numeral.isdigit() else {word: i for i, word in enumerate("一二三四五六七八九十", 1)}.get(numeral, 0)
        task = selection_task if selection_task is not None else ((query_task or {}) if eligibility_query else memory.get("open_task") or {})
        candidates = task.get("candidates") or []
        chosen_order_id = candidates[index - 1]["value"] if 1 <= index <= len(candidates) else None
    elif allow_embedded_selection and not explicit_order_id and (selection_task or {}).get('pending_slot') == 'order_id':
        # 明确恢复仍待选单的任务，不能用页面或最近订单代替用户选择。
        chosen_order_id = selection_task.get('order_id')
    conflicts: list[str] = []
    if selection:
        conflicts.append("order_id: 按本会话已展示候选解析序号，不使用页面或历史订单兜底。")
    elif page_order_id and chosen_order_id == page_order_id and memory.get("last_order_id") and page_order_id != memory["last_order_id"] and not explicit_order_id:
        conflicts.append("order_id: 页面 Runtime Context 与 Session Memory 冲突，采用页面订单。")
    if any(term in request.user_message for term in ("我是VIP", "我是 VIP", "我是黑卡")):
        if (request.runtime_member_level or "unknown").lower() not in {"vip", "black", "黑卡"}:
            conflicts.append("member_level: 用户自称与 Runtime Context 冲突，采用系统会员等级。")
    if any(term in request.user_message for term in ("已经批准", "主管同意", "客服说可以退")):
        conflicts.append("refund_approval: 用户说法不能覆盖 Workflow 审批状态。")

    kept_history, dropped_history = _compress_history(request.history_messages, chosen_order_id)
    # user_id 只在服务端工具层使用；模型只看到做过最小化处理的非敏感运行标签。
    model_context = [
        f"[runtime_context/trusted] member_level={request.runtime_member_level or 'unknown'}, risk_level={request.runtime_risk_level or 'unknown'}",
    ]
    source_reports: list[dict[str, Any]] = []
    user_report = inspect_source("user_message", request.user_message)
    source_reports.append({key: value for key, value in user_report.items() if key != "sanitized_content"})
    for item in kept_history:
        report = inspect_source("history_messages", _redact_history(item.content))
        source_reports.append({key: value for key, value in report.items() if key != "sanitized_content"})
        model_context.append(f"[history/session] {item.role}: {report['sanitized_content']}")
    if chosen_order_id:
        model_context.append(f"[order_reference/session] chosen_order_id={chosen_order_id}")
    context_report = {
        "schema_version": "context_build_report_v1",
        "sources": ["runtime_context", "session_memory", "history_messages", "user_message"],
        "trust_order": ["runtime_context", "verified_tool_fact", "session_memory", "history_messages", "user_message"],
        "chosen_order_id": chosen_order_id,
        'candidate_selection': bool(selection),
        "conflict_resolutions": conflicts,
        "model_context": model_context,
        "source_safety": {
            "tainted": any(report["tainted"] for report in source_reports),
            "tainted_sources": sorted({report["source"] for report in source_reports if report["tainted"]}),
            "reports": source_reports,
        },
    }
    compression_report = {
        "schema_version": "context_compression_v1",
        "recent_window_size": RECENT_WINDOW_SIZE,
        "token_budget": MAX_HISTORY_TOKENS,
        "input_count": len(request.history_messages),
        "kept_count": len(kept_history),
        "dropped_count": len(dropped_history),
        "kept_indexes": [request.history_messages.index(item) for item in kept_history],
        "strategy": "recent_window_plus_order_relevance",
        "relevance_score": {"matching_order_reference": 100, "recent_window": 80, "older_history": 20},
    }
    return str(chosen_order_id) if chosen_order_id else None, context_report, compression_report


def update_memory(
    *,
    session_id: str,
    runtime_user_id: str,
    intent: Intent,
    verified_order_id: str | None,
    user_message: str,
    verified_product_name: str | None = None,
    verified_product_id: str | None = None,
) -> dict[str, Any]:
    """只有通过工具归属校验的订单能写入记忆，审批令牌和隐私不会写入。"""
    memory = current_memory(session_id, runtime_user_id)
    excluded = memory["excluded_items"]
    decisions: list[dict[str, Any]] = []
    if re.search(r"1[3-9]\d{9}", user_message) and "phone_number" not in excluded:
        excluded.append("phone_number")
        decisions.append({"field": "phone_number", "accepted": False, "reason": "privacy_data"})
    if any(term in user_message for term in ("resume-", "审批令牌", "系统提示词", "hidden reasoning")):
        if "high_risk_or_internal_text" not in excluded:
            excluded.append("high_risk_or_internal_text")
        decisions.append({"field": "internal_or_high_risk_text", "accepted": False, "reason": "unsafe_for_memory"})
    if verified_order_id:
        memory["last_order_id"] = verified_order_id
        decisions.append({"field": "last_order_id", "accepted": True, "reason": "verified_tool_fact"})
    if verified_product_name:
        memory["last_product_name"] = verified_product_name
        decisions.append({"field": "last_product_name", "accepted": True, "reason": "verified_tool_fact"})
    if verified_product_id:
        # 最近明确选定的商品只在有效详情成功返回后更新；追问凭编号重新读实时事实。
        product_query = memory.get("product_query") or {}
        product_query["product_id"] = verified_product_id
        if verified_product_name:
            product_query["product_name"] = verified_product_name
        memory["product_query"] = product_query
        decisions.append({"field": "product_query.product_id", "accepted": True, "reason": "verified_tool_fact"})
    color_match = re.search(r"(?:喜欢|偏好|想要)(黑色|白色|蓝色|红色)", user_message)
    if color_match:
        memory["low_risk_preferences"]["preferred_color"] = color_match.group(1)
        decisions.append({"field": "preferred_color", "accepted": True, "reason": "explicit_low_risk_preference"})
    memory["recent_intent"] = intent
    memory["write_decisions"] = decisions
    return dict(memory)


def _compress_history(history: list[HistoryMessage], order_id: str | None) -> tuple[list[HistoryMessage], list[HistoryMessage]]:
    recent_start = max(0, len(history) - RECENT_WINDOW_SIZE)
    selected_indexes = set(range(recent_start, len(history)))
    if order_id:
        selected_indexes.update(index for index, item in enumerate(history) if order_id in item.content)
    kept: list[HistoryMessage] = []
    used_tokens = 0
    for index in sorted(selected_indexes, reverse=True):
        item = history[index]
        tokens = estimate_tokens(item.content)
        if used_tokens + tokens <= MAX_HISTORY_TOKENS or (order_id and order_id in item.content):
            kept.append(item)
            used_tokens += tokens
    kept.reverse()
    kept_ids = {id(item) for item in kept}
    return kept, [item for item in history if id(item) not in kept_ids]


def _redact_history(content: str) -> str:
    """历史消息进入模型上下文前先做基础隐私脱敏。"""
    return _EMAIL_PATTERN.sub("[email-redacted]", _PHONE_PATTERN.sub("[phone-redacted]", content))
