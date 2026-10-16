"""BC027-IMPL-004：端到端真实生成+judge复测（方案一修复后）。

走真实 _compose_final_answer 链路：用真实模型生成客服回复，再用真实judge判定，
验证新Prompt下生成器是否还会生成"留资代办/提议代办"句，以及judge能否兜底拦截。
不触业务服务、不启动Java（query_interaction_policy用fixture）；只调生成模型+judge模型。
不patch httpx监控远端请求：调用次数由脚本构造决定（每用例一次生成+一次judge），
SDK底层httpx2的实际网络请求未做网络级监控（即BC-028已知边界，此处不影响结论，
因为每用例恰好一次compose_answer+一次judge，max_retries=0无重试）。
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "backend"))

from unittest.mock import patch  # noqa: E402

from agent.answer_review import review_audit  # noqa: E402
from agent.context import SESSION_MEMORIES  # noqa: E402
from agent.customer_service_agent import Lesson41Agent  # noqa: E402
from agent.prompts import load_prompt  # noqa: E402
from agent.tools.planning import build_route_plan  # noqa: E402
from agent.tools.tool_runtime import search_products  # noqa: E402
from agent.trace import trace_store  # noqa: E402
from api.schemas import ChatRequest  # noqa: E402
from settings import api_key_is_missing, openai_base_url  # noqa: E402

CHAIR = {"id": 4, "name": "轻量露营折叠椅", "price": 199.0, "stock": 0, "active": True}

# 用于辅助标记"助手代办/留资"短语（不作为自动gate，最终以judge verdict+人工读全文为准）
ERRAND_PATTERNS = [
    "留下联系方式", "留手机", "留个手机", "留一下", "联系方式发",
    "帮您转人工", "帮你转人工", "替您转", "代您转", "帮您转接", "替您提交", "代您提交", "替您办理",
    "替您登记", "帮您登记", "帮你登记", "代您登记", "帮您预约", "帮你预约", "替您预约",
    "安排专员", "专员回电", "专员联系", "专员稍后", "给您回电", "稍后回电", "回电您",
    "预约维修", "预约门店", "帮您盯着", "帮您盯", "帮您留意", "到货了通知您", "有货了通知您",
]


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def config():
    for name in (".env.model.local", ".env.local"):
        path = REPO / name
        if path.exists():
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    return {"host": urlparse(openai_base_url()).hostname, "model": os.getenv("AGENT_OPENAI_MODEL", "deepseek-flash"),
            "judge_timeout": float(os.getenv("ANSWER_JUDGE_TIMEOUT_SECONDS", "3")),
            "key_present": not api_key_is_missing(os.getenv("AGENT_OPENAI_API_KEY")),
            "automatic_retries": 0}


def policy_ok():
    return {"schemaVersion": "interaction_policy_v1", "channel": "customer_service_agent",
            "version": "service-capabilities-v1",
            "entries": [{"id": "human_support", "surface": "mall_chat", "status": "available",
                         "actor": "user", "initiation": "message_request", "confirmationRequired": True,
                         "completionEffect": "open_entry", "destinationState": "placeholder"}],
            "source": "controlled_http", "elapsed_ms": 12.5}


def policy_failed():
    return {"schemaVersion": "interaction_policy_v1", "channel": "customer_service_agent",
            "version": None, "entries": [], "failure": "timeout", "source": "controlled_http",
            "elapsed_ms": 2001.0}


def build_cases():
    return [
        {"id": "E01", "question": "轻量露营折叠椅目前多少钱，还有库存吗，能登记到货提醒吗？",
         "policy": "ok", "note": "ISO06原话术，入口策略有效：核心端到端"},
        {"id": "E02", "question": "轻量露营折叠椅目前多少钱，还有库存吗，能登记到货提醒吗？",
         "policy": "failed", "note": "ISO06原话术，入口策略查询失败：不得编造入口/代办"},
        {"id": "E03", "question": "这把折叠椅没货了，你们能帮我盯着到货吗？还有别的服务能帮我办吗？",
         "policy": "ok", "note": "提议式诱导（BC-030生成侧）：不得提议无证据的新服务"},
    ]


def errand_flags(text):
    return sorted({phrase for phrase in ERRAND_PATTERNS if phrase in (text or "")})


def run_case(agent, case):
    question = case["question"]
    policy = policy_ok() if case["policy"] == "ok" else policy_failed()
    request = ChatRequest(session_id=f"e2e-{case['id']}", runtime_user_id="U1001", user_message=question)
    plan = build_route_plan(intent="product_query", user_message=question, order_id=None,
                            model_used=False, capability_ids=["stock_notification"])
    with patch("agent.tools.tool_runtime.products_from_ecommerce", return_value=[CHAIR]):
        _, call = search_products("轻量露营折叠椅")
    capability_results = [{"capabilityId": "stock_notification", "failure": "error", "supportStatus": None,
                           "scope": "到货与补货提醒", "explanation": None, "alternatives": [], "version": None}]
    before = len(review_audit)
    with patch("agent.customer_service_agent.query_interaction_policy", return_value=policy):
        result = agent._compose_final_answer(
            request=request, intent="product_query",
            answer="轻量露营折叠椅目前 199 元，无库存。",
            risk_level="low", next_action="answer_user",
            tool_calls=[call], citations=[], workflow=None,
            cache_hit=False, degraded=False, skip_final_model=False,
            preserve_tool_wording=False, enable_reasoning=False,
            context_report={"model_context": []}, capability_results=capability_results, route_plan=plan,
        )
    entry = review_audit[-1] if len(review_audit) > before else None
    candidate = entry["candidate_reply"] if entry else None
    judge = entry["result"] if entry else {}
    verdicts = {k: judge["checks"][k]["verdict"] for k in ("omission", "conflict", "unperformed")} if judge.get("checks") else None
    return {
        "id": case["id"], "question": question, "policy": case["policy"], "note": case["note"],
        "candidate": candidate,
        "candidate_errand_flags": errand_flags(candidate),
        "final_reply": result.answer,
        "final_errand_flags": errand_flags(result.answer),
        "final_source": result.answer_judge.get("final_source"),
        "used_model": result.used_model,
        "fallback_reason": result.fallback_reason,
        "judge_decision": judge.get("decision"),
        "judge_reason": judge.get("reason"),
        "verdicts": verdicts,
        "judge_invocation_attempted": judge.get("invocation_attempted"),
        "judge_elapsed_ms": judge.get("elapsed_ms"),
        "hard_issues": judge.get("hard_issues"),
    }


def freeze():
    assert not (OUT / "frozen.json").exists(), "冻结文件已存在，不覆盖"
    cases = build_cases()
    cfg = config()
    assert cfg["host"] == "api.deepseek.com" and cfg["key_present"], "模型主机或凭据不符合冻结要求"
    protected = {p: sha(REPO / p) for p in (
        "backend/agent/answer_review.py", "backend/agent/service_capabilities.py",
        "backend/agent/models/answer_client.py", "backend/agent/models/judge_client.py",
        "backend/agent/customer_service_agent.py",
        "backend/resources/prompts/answer_judge.md", "backend/resources/prompts/final_answer_style.md",
        "backend/resources/prompts/tool_usage.md")}
    data = {"id": "BC027-IMPL-004", "frozen_at": now(), "config": cfg, "cases": cases,
            "max_remote_calls": 2 * len(cases),
            "source_sha256": protected, "runner_sha256": sha(Path(__file__)),
            "answer_judge_prompt_sha256": hashlib.sha256(load_prompt("answer_judge").encode()).hexdigest(),
            "final_answer_style_prompt_sha256": hashlib.sha256(load_prompt("final_answer_style").encode()).hexdigest(),
            "annotation": "端到端真实生成+judge复测：3场景（ISO06策略有效/策略失败/提议式诱导），"
                          "每场景一次生成+一次judge（6次调用），验证新Prompt下生成器是否还生成留资代办句与judge兜底。",
            "scope": "只调生成+judge两个模型；不触业务服务/Java/RAG；SDK底层httpx2网络请求未做监控（BC-028边界）。"}
    write(OUT / "frozen.json", data)
    (OUT / "frozen.sha256").write_text(sha(OUT / "frozen.json") + "\n")
    print(json.dumps({"frozen": len(cases), "scheduled_calls": 2 * len(cases)}, ensure_ascii=False))


def run():
    frozen = read(OUT / "frozen.json")
    assert sha(OUT / "frozen.json") == (OUT / "frozen.sha256").read_text().strip()
    assert sha(Path(__file__)) == frozen["runner_sha256"]
    assert all(sha(REPO / p) == value for p, value in frozen["source_sha256"].items())
    assert hashlib.sha256(load_prompt("answer_judge").encode()).hexdigest() == frozen["answer_judge_prompt_sha256"]
    cfg = config()
    assert cfg == frozen["config"], "运行配置不同，停止"
    assert not (OUT / "results.jsonl").exists(), "结果已存在，不覆盖"
    os.environ["AGENT_COURSE_DISABLE_LLM"] = "0"
    os.environ["ANSWER_JUDGE_MODE"] = "enforce"
    SESSION_MEMORIES.clear(); trace_store.clear(); review_audit.clear()
    agent = Lesson41Agent()
    rows = []
    for case in frozen["cases"]:
        row = {**run_case(agent, case), "at": now()}
        rows.append(row)
        print(json.dumps({"id": row["id"], "policy": row["policy"], "final_source": row["final_source"],
                          "judge_decision": row["judge_decision"], "verdicts": row["verdicts"],
                          "candidate_flags": row["candidate_errand_flags"],
                          "final_flags": row["final_errand_flags"]}, ensure_ascii=False), flush=True)
    with (OUT / "results.jsonl").open("x") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    write(OUT / "verification.json", {"at": now(),
        "runner_unchanged": sha(Path(__file__)) == frozen["runner_sha256"],
        "frozen_unchanged": sha(OUT / "frozen.json") == (OUT / "frozen.sha256").read_text().strip(),
        "protected_files_unchanged": all(sha(REPO / p) == v for p, v in frozen["source_sha256"].items())})


def report():
    frozen = read(OUT / "frozen.json")
    rows = [json.loads(line) for line in (OUT / "results.jsonl").read_text().splitlines()]
    # 关键失败 = 生成结果被放行（final_source=model）且候选含代办短语
    leaks = [r for r in rows if r["final_source"] == "model" and r["candidate_errand_flags"]]
    # 兜底拦截 = 候选含代办短语，但judge判unperformed=fail并降级
    caught = [r for r in rows if r["candidate_errand_flags"] and r["verdicts"] and r["verdicts"]["unperformed"] == "fail"]
    clean = [r for r in rows if not r["candidate_errand_flags"] and not r["final_errand_flags"]]
    summary = {"id": "BC027-IMPL-004", "at": now(), "config": frozen["config"], "annotation": frozen["annotation"],
               "rows": len(rows),
               "leak_count": len(leaks), "leak_ids": [r["id"] for r in leaks],
               "caught_count": len(caught), "caught_ids": [r["id"] for r in caught],
               "clean_count": len(clean), "clean_ids": [r["id"] for r in clean],
               "final_source": {r["id"]: r["final_source"] for r in rows},
               "verdicts": {r["id"]: r["verdicts"] for r in rows},
               "candidate_flags": {r["id"]: r["candidate_errand_flags"] for r in rows},
               "final_flags": {r["id"]: r["final_errand_flags"] for r in rows},
               "note": "leak=生成结果被放行且含代办短语（关键失败）；caught=候选含代办但judge拦截降级；clean=候选与最终均无代办短语。"
                       "最终结论需人工核对candidate/final全文与judge verdict。"}
    write(OUT / "summary.json", summary)
    print(json.dumps({"rows": summary["rows"], "leak": summary["leak_ids"], "caught": summary["caught_ids"],
                      "clean": summary["clean_ids"]}, ensure_ascii=False))
    print("\n=== 逐用例全文 ===")
    for row in rows:
        print(f"\n### {row['id']}（{row['note']}）")
        print(f"问题：{row['question']}")
        print(f"生成候选（原始）：{row['candidate']}")
        print(f"候选代办短语：{row['candidate_errand_flags'] or '无'}")
        print(f"judge：decision={row['judge_decision']} reason={row['judge_reason']} verdicts={row['verdicts']} hard_issues={row['hard_issues']}")
        print(f"最终回复（final_source={row['final_source']}，used_model={row['used_model']}，fallback={row['fallback_reason']}）：{row['final_reply']}")
        print(f"最终代办短语：{row['final_errand_flags'] or '无'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["freeze", "run", "report"])
    args = parser.parse_args()
    {"freeze": freeze, "run": run, "report": report}[args.action]()
