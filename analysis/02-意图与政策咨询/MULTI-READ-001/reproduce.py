"""MULTI-READ-001 离线复现与 MR 冻结验收。

离线构造 RoutePlan（不调用真实模型），验证"多个只读主题不被主意图互斥过滤"。
改造前：跨域组合（发票+退款、优惠+退款、退款政策+发票）应被判 incompatible 并丢主题。
改造后：全部合法主题保留、knowledge_domains 取并集、answer_order 完整。

运行：
  AGENT_COURSE_ENV=/dev/null AGENT_COURSE_DISABLE_LLM=1 AGENT_COURSE_OFFLINE_RAG=1 \
  AGENT_COURSE_OFFLINE_FACTS=1 PYTHONPATH=backend .venv/bin/python \
  analysis/02-意图与政策咨询/MULTI-READ-001/reproduce.py
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("AGENT_COURSE_ENV", "/dev/null")
os.environ.setdefault("AGENT_COURSE_DISABLE_LLM", "1")
os.environ.setdefault("AGENT_COURSE_OFFLINE_RAG", "1")
os.environ.setdefault("AGENT_COURSE_OFFLINE_FACTS", "1")
sys.path.insert(0, "backend")

from agent.tools.planning import build_route_plan  # noqa: E402

# MR 冻结用例：固定模型输出（模拟路由已识别多主题），断言计划是否保留全部主题。
# expected_topics = 本轮应保留的合法主题；pre_fix_drop = 改造前是否会被过滤。
CASES = [
    # MR02 发票＋退款到账，主意图分别固定为两入口
    {"id": "MR02a", "intent": "invoice_query", "topics": ["invoice_issue", "refund_arrival"],
     "expect_all": True, "note": "发票为主意图仍须保留退款到账主题"},
    {"id": "MR02b", "intent": "refund_policy_query", "topics": ["refund_arrival", "invoice_issue"],
     "expect_all": True, "note": "退款政策为主意图仍须保留发票主题"},
    # MR03 优惠＋退款到账＋发票三项
    {"id": "MR03", "intent": "promotion_query",
     "topics": ["promotion_stacking", "refund_arrival", "invoice_issue"],
     "expect_all": True, "note": "三项跨域需求不能截断或丢失"},
    # MR01 能力＋优惠（service_scope_query 已被 BC-031 修复，作对照）
    {"id": "MR01", "intent": "service_scope_query", "topics": ["promotion_stacking"],
     "expect_all": True, "note": "能力入口已保留知识主题（对照组）"},
]


def run() -> dict:
    rows = []
    for case in CASES:
        plan = build_route_plan(
            intent=case["intent"],
            user_message="离线构造多主题",
            order_id=None,
            model_used=True,
            capability_ids=[],
            inquiry_topics=list(case["topics"]),
            topic_status="valid",
            answer_order=["topic:" + t for t in case["topics"]],
        )
        kept = list(plan.inquiry_topics)
        all_kept = kept == case["topics"]
        rows.append({
            "id": case["id"],
            "intent": case["intent"],
            "input_topics": case["topics"],
            "kept_topics": kept,
            "topic_status": plan.topic_status,
            "knowledge_domains": plan.knowledge_domains,
            "answer_order": plan.answer_order,
            "all_topics_preserved": all_kept,
            "expect_all": case["expect_all"],
            "pass": (all_kept == case["expect_all"]),
            "note": case["note"],
        })
    return {
        "id": "MULTI-READ-001-REPRO",
        "kind": "offline_plan_construction_fixed_inputs",
        "real_model_calls": 0,
        "business_calls": 0,
        "all_pass": all(r["pass"] for r in rows),
        "rows": rows,
    }


if __name__ == "__main__":
    result = run()
    out = os.path.join(os.path.dirname(__file__), "reproduce-result.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    for row in result["rows"]:
        mark = "PASS" if row["pass"] else "FAIL"
        print(f"[{mark}] {row['id']:6} intent={row['intent']:20} kept={row['kept_topics']} status={row['topic_status']}")
    print(f"\noverall: {'ALL PASS' if result['all_pass'] else 'HAS FAIL'} -> {out}")
