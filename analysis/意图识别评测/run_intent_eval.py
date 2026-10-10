"""调用运行中的客服 Agent /chat，记录最终意图标签。

入口是已经在听的进程，不是把当前源码再导入一遍。
当前源码另跑 classify → plan_intent。模型给出合法标签后不再做规则覆盖。
对照里的模型原文不是那一次 /chat 的返回。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASE_PATH = ROOT / "docs" / "缺口二意图识别" / "意图识别评测用例.md"
OUT_PATH = Path(__file__).resolve().parent / "result.json"
BACKEND = ROOT / "backend"
AGENT_URL = os.environ.get("INTENT_EVAL_AGENT_URL", "http://127.0.0.1:8000/chat")
ALLOWED = {
    "general_chat",
    "order_query",
    "refund_status_query",
    "refund_policy_query",
    "refund_request",
    "return_policy_query",
    "return_request",
    "invoice_query",
    "promotion_query",
    "product_query",
    "low_confidence_query",
    "degradation_request",
    "security_request",
    "unknown",
}


def score(actual: str | None, expected: list[str]) -> int | None:
    """相同为 1，不同为 0。没有实际标签时不打分。"""
    if actual is None:
        return None
    return 1 if actual in expected else 0


def parse_cases(text: str) -> list[dict]:
    cases = []
    for line in text.splitlines():
        if not re.match(r"^\| [NBAM]\d{2} \|", line):
            continue
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        case_id = parts[0]
        if case_id.startswith("M"):
            context, message, expected_cell, baseline_cell = parts[1], parts[2], parts[3], parts[5]
        else:
            context, message, expected_cell, baseline_cell = "", parts[1], parts[2], parts[4]
        expected = re.findall(r"`([a-z_]+)`", expected_cell)
        baseline = re.findall(r"`([a-z_]+)`", baseline_cell)
        gap = "设计缺口" in expected_cell
        if gap and expected:
            raise SystemExit(f"{case_id} 同时标了设计缺口和标签")
        if not gap and not expected:
            raise SystemExit(f"{case_id} 没有预期标签")
        if any(label not in ALLOWED for label in expected):
            raise SystemExit(f"{case_id} 预期标签不在允许集合")
        prior = []
        if context and not context.startswith("系统前提"):
            for part in context.split("→"):
                part = part.strip()
                if part.startswith("用户："):
                    prior.append(part.removeprefix("用户：").strip())
        cases.append(
            {
                "id": case_id,
                "group": {"N": "正常", "B": "边界", "A": "对抗", "M": "多轮"}[case_id[0]],
                "message": message,
                "prior_user_messages": prior,
                "context": context,
                "expected": expected,
                "gap": gap,
                "doc_baseline": baseline[0] if baseline else None,
            }
        )
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise SystemExit("用例编号重复")
    if len(cases) != 48:
        raise SystemExit(f"用例数是 {len(cases)}，不是 48")
    return cases


def check_cases(cases: list[dict]) -> None:
    assert score("order_query", ["order_query"]) == 1
    assert score("refund_request", ["order_query"]) == 0
    assert score("unknown", ["unknown", "general_chat"]) == 1
    assert score("product_query", ["unknown", "general_chat"]) == 0
    assert score(None, ["general_chat"]) is None
    by_group = {}
    for case in cases:
        by_group[case["group"]] = by_group.get(case["group"], 0) + 1
    assert by_group == {"正常": 12, "边界": 16, "对抗": 12, "多轮": 8}
    gaps = [case["id"] for case in cases if case["gap"]]
    assert gaps == ["B15", "M07"]
    a11 = next(case for case in cases if case["id"] == "A11")
    assert "\u200b" in a11["message"]
    scorable = [case for case in cases if not case["gap"]]
    single = [case for case in scorable if case["group"] != "多轮"]
    multi = [case for case in scorable if case["group"] == "多轮"]
    assert len(single) == 39 and len(multi) == 7
    by_id = {case["id"]: case for case in cases}
    assert by_id["B01"]["expected"] == ["refund_policy_query"]
    assert by_id["B02"]["expected"] == ["return_policy_query"]
    assert by_id["N09"]["expected"] == ["invoice_query"]
    print(f"parsed 48; scorable single {len(single)}; scorable multi {len(multi)}; gaps {gaps}")


def redact(text: str) -> str:
    text = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-<redacted>", text)
    return text[:500]


def post_chat(session_id: str, message: str) -> dict:
    body = {
        "session_id": session_id,
        "runtime_user_id": "intent-eval",
        "user_message": message,
        "debug": False,
    }
    request = urllib.request.Request(
        AGENT_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
            error = None
    except urllib.error.HTTPError as exc:
        payload = {}
        status = exc.code
        error = redact(exc.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        payload = {}
        status = None
        error = redact(f"{exc.__class__.__name__}: {exc}")
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    state = payload.get("session_state") or {}
    route_planner = ((state.get("model") or {}).get("route_planner") or {})
    route_plan = state.get("route_plan") or {}
    intent = state.get("intent")
    if intent not in ALLOWED:
        intent = None
    return {
        "http_status": status,
        "error": error,
        "elapsed_ms": elapsed_ms,
        "final_intent": intent,
        "used_model": route_planner.get("used_model"),
        "model_name": route_planner.get("model_name"),
        "fallback_reason": route_planner.get("fallback_reason"),
        "route_source": route_plan.get("source"),
        "route_plan_intent": route_plan.get("intent"),
        "next_action": state.get("next_action"),
        "answer_excerpt": str(payload.get("answer") or "")[:180],
    }


def run_live(case: dict, run_id: str) -> dict:
    session_id = f"intent-eval-{run_id}-{case['id']}"
    prior_results = []
    for index, message in enumerate(case["prior_user_messages"], start=1):
        prior_results.append({"turn": index, "message": message, **post_chat(session_id, message)})
    final = post_chat(session_id, case["message"])
    return {"session_id": session_id, "prior_turns": prior_results, **final}


_source_lock = threading.Lock()
_source_ready = False
_source_agent = None
_source_original_invoke = None
_source_raw = threading.local()


def _capturing_invoke(model, user_message, system_prompt):
    content = _source_original_invoke(model, user_message, system_prompt)
    _source_raw.text = content
    return content


def _ensure_source_agent():
    global _source_ready, _source_agent, _source_original_invoke
    if _source_ready:
        return _source_agent
    os.environ["AGENT_COURSE_ENV"] = str(ROOT / ".env.model.local")
    os.environ["AGENT_COURSE_DISABLE_LLM"] = "0"
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from agent.customer_service_agent import Lesson41Agent
    from agent.models.router_client import RouteModelClient

    _source_original_invoke = RouteModelClient._invoke_chain
    RouteModelClient._invoke_chain = staticmethod(_capturing_invoke)
    _source_agent = Lesson41Agent()
    _source_ready = True
    return _source_agent


def source_route(message: str) -> dict:
    """当前磁盘源码的路由。同一时刻只跑一条，避免覆盖模型原文。"""
    with _source_lock:
        agent = _ensure_source_agent()
        from agent.tools.planning import classify_intent

        fallback = classify_intent(message)
        _source_raw.text = None
        try:
            raw_result = agent.route_model_client.plan_intent(message, fallback_intent=fallback)
            raw_text = _source_raw.text
            parsed = agent.route_model_client._extract_intent(raw_text) if raw_text else None
            return {
                "rule_intent": fallback,
                "raw_text": raw_text,
                "raw_intent": parsed,
                "raw_error": None if raw_text is not None else raw_result.fallback_reason,
                "source_final_intent": raw_result.intent,
                "source_fallback_reason": raw_result.fallback_reason,
                "source_used_model": raw_result.used_model,
                "source_model_name": raw_result.model_name,
            }
        except Exception as exc:
            return {
                "rule_intent": fallback,
                "raw_text": getattr(_source_raw, "text", None),
                "raw_intent": None,
                "raw_error": redact(f"{exc.__class__.__name__}: {exc}"),
                "source_final_intent": None,
                "source_fallback_reason": None,
                "source_used_model": None,
                "source_model_name": None,
            }


def local_rule(message: str) -> str:
    os.environ["AGENT_COURSE_ENV"] = str(ROOT / ".env.model.local")
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from agent.tools.planning import classify_intent

    return classify_intent(message)


def accuracy(rows: list[dict], predicate) -> dict:
    chosen = [row for row in rows if predicate(row) and row["label_score"] is not None]
    correct = sum(row["label_score"] for row in chosen)
    return {"correct": correct, "total": len(chosen), "rate": (correct / len(chosen) if chosen else None)}


def main() -> None:
    cases = parse_cases(CASE_PATH.read_text(encoding="utf-8"))
    check_cases(cases)
    if "--check" in sys.argv:
        for case in cases:
            print(case["id"], "GAP" if case["gap"] else ",".join(case["expected"]), repr(case["message"][:40]))
        return

    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    selected = None
    if "--ids" in sys.argv:
        selected = set(sys.argv[sys.argv.index("--ids") + 1].split(","))
        cases = [case for case in cases if case["id"] in selected]

    print(f"live {AGENT_URL} run {run_id} cases {len(cases)}", flush=True)
    live_by_id: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_live, case, run_id): case["id"] for case in cases}
        for future in as_completed(futures):
            case_id = futures[future]
            live_by_id[case_id] = future.result()
            final = live_by_id[case_id].get("final_intent")
            print(f"live {case_id} {final} {live_by_id[case_id].get('elapsed_ms')}ms", flush=True)

    print("source route replay", flush=True)
    source_by_id: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(source_route, case["message"]): case["id"] for case in cases}
        for future in as_completed(futures):
            case_id = futures[future]
            source_by_id[case_id] = future.result()
            print(f"source {case_id} {source_by_id[case_id].get('source_final_intent')}", flush=True)

    rows = []
    for case in cases:
        live = live_by_id[case["id"]]
        source = source_by_id[case["id"]]
        rule = local_rule(case["message"])
        label_score = None if case["gap"] else score(live["final_intent"], case["expected"])
        rows.append(
            {
                **case,
                "rule_intent": rule,
                "rule_matches_doc_baseline": rule == case["doc_baseline"],
                "label_score": label_score,
                "live": live,
                "source_replay": {
                    "raw_text": source.get("raw_text"),
                    "raw_intent": source.get("raw_intent"),
                    "raw_error": source.get("raw_error"),
                    "final_intent": source.get("source_final_intent"),
                    "fallback_reason": source.get("source_fallback_reason"),
                    "used_model": source.get("source_used_model"),
                    "model_name": source.get("source_model_name"),
                    "same_as_live_final": source.get("source_final_intent") == live.get("final_intent"),
                },
            }
        )

    summary = {
        "single_scorable": accuracy(rows, lambda row: row["group"] != "多轮" and not row["gap"]),
        "multi_scorable": accuracy(rows, lambda row: row["group"] == "多轮" and not row["gap"]),
        "all_scorable": accuracy(rows, lambda row: not row["gap"]),
        "by_group": {
            group: accuracy(rows, lambda row, group=group: row["group"] == group and not row["gap"])
            for group in ("正常", "边界", "对抗", "多轮")
        },
        "live_source_mismatch": [row["id"] for row in rows if row["source_replay"]["same_as_live_final"] is False],
        "unscored": [row["id"] for row in rows if row["label_score"] is None],
    }
    payload = {
        "started_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "run_id": run_id,
        "agent_url": AGENT_URL,
        "entry": "running_python_agent_chat",
        "summary": summary,
        "rows": rows,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
