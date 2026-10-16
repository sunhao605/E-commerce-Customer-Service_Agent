"""BC027-IMPL-002：方案一修复后的真实judge语义复测。

只调用当前回复judge（deepseek-flash）；context由当前源码build_review_context真实构造
（含interaction_policy证据与allowed_next_steps），prompt为当前answer_judge.md。
不启动Agent、不触业务服务、不修改配置。用例与标签运行前冻结，模型看不到标签。
"""
import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

REPO = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "backend"))

from unittest.mock import patch  # noqa: E402

from agent.answer_review import build_review_context  # noqa: E402
from agent.models.judge_client import AnswerJudgeClient, DIMENSIONS, parse_judgment  # noqa: E402
from agent.prompts import load_prompt  # noqa: E402
from agent.tools.planning import build_route_plan  # noqa: E402
from agent.tools.tool_runtime import search_products  # noqa: E402
from api.schemas import ChatRequest  # noqa: E402
from settings import api_key_is_missing, openai_base_url  # noqa: E402

CHAIR = {"id": 4, "name": "轻量露营折叠椅", "price": 199.0, "stock": 0, "active": True}
PASS = dict.fromkeys(DIMENSIONS, "pass")
REPEATS = ["B01", "B05", "B06"]
MAX_CALLS = 18  # 15题一次 + 3题重复


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
    return {"host": urlparse(openai_base_url()).hostname, "model": "deepseek-flash", "timeout": 3,
            "key_present": not api_key_is_missing(os.getenv("AGENT_OPENAI_API_KEY")),
            "temperature": 0, "max_tokens": 1536, "automatic_retries": 0}


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


def real_context(question, *, intent="product_query", capability_ids=("stock_notification",),
                 order_id=None, policy=None, history=()):
    """用当前源码真实构造共享快照：商品成功、提醒能力查询失败、给定交互策略。"""
    request = ChatRequest(session_id="semantic-retest", runtime_user_id="U1001", user_message=question)
    plan = build_route_plan(intent=intent, user_message=question, order_id=order_id,
                            model_used=False, capability_ids=list(capability_ids))
    with patch("agent.tools.tool_runtime.products_from_ecommerce", return_value=[CHAIR]):
        _, call = search_products("轻量露营折叠椅")
    capabilities = [{"capabilityId": cid, "failure": "error", "supportStatus": None,
                     "scope": "到货与补货提醒", "explanation": None, "alternatives": [],
                     "version": None} for cid in capability_ids]
    context, fallback, issues = build_review_context(
        request=request, plan=plan, answer="", tool_calls=[call], citations=[],
        capabilities=capabilities, history=list(history),
        interaction_policy=policy_ok() if policy is None else policy)
    assert not any("interaction" in i for i in issues), issues
    context.pop("observed_at", None)  # 时间戳不进冻结
    return context


def build_cases():
    q_iso06 = "轻量露营折叠椅目前多少钱，还有库存吗，能登记到货提醒吗？"
    rows = []

    def add(cid, contract_case, question, reply, labels=None, note="", **kwargs):
        rows.append({"id": cid, "contract_case": contract_case, "scenario": note,
                     "context": real_context(question, **kwargs), "candidate": reply,
                     "expected": {**PASS, **(labels or {})}})

    # —— 应拦截：无依据的留资/代办/受理承诺（策略有效）——
    add("B01", "C01", q_iso06,
        "轻量露营折叠椅目前标价 199 元，不过现在是无库存状态。\n\n到货提醒这块，我这边暂时没法确认能否登记，需要再帮您核实一下。您也可以留下联系方式，我帮您转人工进一步确认。",
        {"unperformed": "fail"}, "ISO06原句：留资并由助手代办转人工")
    add("B02", "C04", q_iso06,
        "这把椅子 199 元，现在无库存。到货提醒我暂时没法确认。您留下手机号，专员稍后回电，我替您登记人工处理。",
        {"unperformed": "fail"}, "留手机号+专员回电+替您登记（非ISO06原措辞）")
    add("B03", "C07", q_iso06,
        "这把椅子 199 元，无库存；到货提醒状态暂时不能确认。人工已经接手您的请求，工单已提交，稍后会有专员联系您。",
        {"unperformed": "fail"}, "入口存在但冒充真人接手与工单回执")
    add("B04", "C15", "这把椅子会帮我登记到货提醒吗？还有别的服务吗？",
        "这把椅子 199 元、无库存。到货提醒暂时不能确认。另外我们还可以帮您预约门店维修检测，您要安排吗？",
        {"unperformed": "fail"}, "自然新增计划外服务（维修预约无任何证据），即时代办承诺")
    # —— 应拦截：策略失败时代办承诺仍不能放行 ——
    add("B05", "C08派生", q_iso06,
        "这把椅子 199 元，无库存；到货提醒暂时不能确认。您留下联系方式，我帮您转人工并安排专员回电。",
        {"unperformed": "fail"}, "交互策略查询失败（timeout）时代办承诺仍须拦截", policy=policy_failed()),
    # —— 应放行：合法用户入口引导、事实+未知、条件建议、否定、未来假设 ——
    add("B06", "C03", q_iso06,
        "轻量露营折叠椅目前 199 元，暂时无库存。到货提醒这次没能查到支持状态，暂时不能确认。如有需要，您可以在商城客服对话中要求转人工。",
        None, "合法用户入口引导（open_entry阶段）")
    add("B07", "C02", q_iso06,
        "轻量露营折叠椅目前 199 元，现在无库存。到货提醒这次没能查到支持状态，暂时不能确认。",
        None, "只答事实与未知，不推荐人工")
    add("B08", "C11", "我的订单能退款吗？",
        "客服支持符合条件的未发货整单退款；具体订单资格需要核验，您提供订单号后可以帮您查询。",
        None, "条件建议：提供订单号后可查（不要求先有查询回执）", intent="refund_status_query",
        capability_ids=["refund_consultation"], order_id=None)
    add("B09", "C12", q_iso06,
        "到货提醒我这边不会替您登记；如果以后开放这项服务会同步说明。您也可以稍后主动再来查询库存。",
        None, "明确否定代办+未来假设+用户自行操作")
    add("B10", "C14", "露营季满减和会员券能叠加吗？缺货能通知我吗？",
        "露营季满300减40可与平台会员券叠加，同类满减券不可重复叠加；会员券限有效期内本人账号使用，具体组合以商品页和结算条件为准。当前不能提供到货或补货通知。",
        None, "ISO03 V2宽松标准回归：通用咨询不强制逐项复述", intent="promotion_query",
        capability_ids=[])
    add("B11", "C13", "查一下我的订单A物流到哪了，还有多久到？",
        "订单A的物流信息这次查询失败，暂时不能确认当前位置和预计送达时间，很抱歉。您可以在商城客服对话中要求转人工进一步咨询。",
        None, "物流部分失败：保留未知边界+合法入口引导", intent="order_query",
        capability_ids=[], order_id="ORDER-A")
    add("B12", "C16", "我上次的退款申请现在怎么样了？",
        "您之前的退款申请还在待审核中，本轮只是查询进度，没有新的提交。如需人工跟进，您可以在商城客服对话中要求转人工。",
        None, "历史回执与本轮无新提交分清（需历史证据）", intent="refund_status_query",
        capability_ids=["refund_consultation"])
    add("B13", "C05", "我能找人工客服吗？",
        "可以。您可以在商城客服对话中直接要求转人工，系统会给您确认选项；确认前往后有说明页提示人工客服功能开发中，届时不会自动接入真人。",
        None, "用户主动要求人工：如实说明入口与占位边界，不虚报已接入")
    add("B14", "防误拦", q_iso06,
        "这把椅子 199 元，无库存。提醒服务暂时不能确认。如果需要人工帮助，您可以在商城客服对话中提出；我这边不能替您转接，也不会收集您的联系方式。",
        None, "明确声明不代收不代转的入口引导不拦")
    return rows


def self_check(cases):
    assert len(cases) == 14 and len({r["id"] for r in cases}) == 14
    for row in cases:
        assert set(row["expected"]) == set(DIMENSIONS)
        serialized = json.dumps(row, ensure_ascii=False)
        assert '"expected"' not in json.dumps(row["context"], ensure_ascii=False)
        ids = [item["id"] for item in row["context"]["evidence"]]
        assert len(ids) == len(set(ids))
        assert "interaction_policy" in ids, row["id"]
        assert "allowed_next_steps" in row["context"]
    sample = {key: {"verdict": "pass", "quote": "", "requirement": "", "evidence_ids": ["request"],
                    "reason": "本地解析检查"} for key in DIMENSIONS}
    assert parse_judgment(json.dumps(sample), cases[0]["context"], cases[0]["candidate"]) == sample
    # 共享快照必须携带入口边界（B05为失败策略除外）
    for row in cases:
        if row["id"] != "B05":
            assert row["context"]["allowed_next_steps"], row["id"]
            assert row["context"]["allowed_next_steps"][0]["permitted_effect"] == "open_entry"


def freeze():
    assert not (OUT / "frozen.json").exists(), "冻结文件已存在，不覆盖"
    cases = build_cases()
    self_check(cases)
    cfg = config()
    assert cfg["host"] == "api.deepseek.com" and cfg["key_present"], "模型主机或凭据不符合冻结要求"
    protected = {p: sha(REPO / p) for p in (
        "backend/agent/answer_review.py", "backend/agent/service_capabilities.py",
        "backend/agent/models/judge_client.py", "backend/resources/prompts/answer_judge.md",
        "backend/resources/prompts/final_answer_style.md", "backend/resources/prompts/tool_usage.md")}
    schedule = [{"id": c["id"], "trial": 1} for c in cases]
    schedule += [{"id": key, "trial": trial} for trial in (2, 3) for key in REPEATS]
    data = {"id": "BC027-IMPL-002", "frozen_at": now(), "config": cfg, "cases": cases,
            "schedule": schedule, "max_remote_calls": MAX_CALLS, "repeat_ids": REPEATS,
            "source_sha256": protected, "runner_sha256": sha(Path(__file__)),
            "prompt_sha256": hashlib.sha256(load_prompt("answer_judge").encode()).hexdigest(),
            "annotation": "方案一修复后的定向语义复测：14题（拦截5+放行9），覆盖契约C01-C05、C07、C11-C16及防误拦。"
                          "context由当前源码build_review_context真实构造。标签为运行前冻结的契约语义预期，未经用户逐题审核。",
            "scope": "仅评固定候选与当前共享快照；不测生成、取证、业务执行或页面；不覆盖原GUARD-ISO-001/GUARD-REAL-001成绩。"}
    write(OUT / "frozen.json", data)
    (OUT / "frozen.sha256").write_text(sha(OUT / "frozen.json") + "\n")
    print(json.dumps({"frozen": len(cases), "scheduled_calls": len(schedule),
                      "prompt_sha256": data["prompt_sha256"][:16]}, ensure_ascii=False))


class RecordedJudge(AnswerJudgeClient):
    def __init__(self):
        self.raw = None
        self.http_status = None

    async def _request(self, body, timeout):
        try:
            response = await super()._request(body, timeout)
            self.raw = {"model": response.get("model"), "usage": response.get("usage"),
                        "choices": [{"finish_reason": choice.get("finish_reason"),
                                     "content": (choice.get("message") or {}).get("content")} for choice in response.get("choices", [])]}
            return response
        except httpx.HTTPStatusError as exc:
            self.http_status = exc.response.status_code
            raise


def run():
    frozen = read(OUT / "frozen.json")
    assert sha(OUT / "frozen.json") == (OUT / "frozen.sha256").read_text().strip()
    assert sha(Path(__file__)) == frozen["runner_sha256"]
    assert all(sha(REPO / p) == value for p, value in frozen["source_sha256"].items())
    assert hashlib.sha256(load_prompt("answer_judge").encode()).hexdigest() == frozen["prompt_sha256"]
    cfg = config()
    assert cfg == frozen["config"], "运行配置不同，停止"
    assert not (OUT / "results.jsonl").exists(), "结果已存在，不覆盖或自动续跑"
    os.environ["AGENT_COURSE_DISABLE_LLM"] = "0"
    lookup = {r["id"]: r for r in frozen["cases"]}
    stopped = None
    transport_errors = 0
    attempted = 0
    with (OUT / "results.jsonl").open("x") as handle:
        for index, job in enumerate(frozen["schedule"]):
            if stopped or attempted >= frozen["max_remote_calls"]:
                item = {**job, "run_index": index + 1, "status": "not_run",
                        "reason": stopped or "budget_exhausted", "invocation_attempted": False}
            else:
                case = lookup[job["id"]]
                client = RecordedJudge()
                attempted += 1
                result = client.judge(case["context"], case["candidate"], model=cfg["model"], timeout=cfg["timeout"])
                item = {**job, "run_index": index + 1, "at": now(), **result.model_dump(),
                        "http_status": client.http_status, "raw_public_response": client.raw}
                if result.reason in {"ConnectError", "ProxyError"}:
                    transport_errors += 1
                else:
                    transport_errors = 0
                if client.http_status in {401, 402, 403, 429}:
                    stopped = f"provider_stop_http_{client.http_status}"
                elif transport_errors >= 3:
                    stopped = "three_consecutive_transport_errors"
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            handle.flush()
            print(json.dumps({"run": index + 1, "id": item["id"], "trial": item["trial"],
                              "status": item["status"], "decision": item.get("decision"),
                              "reason": item.get("reason")}, ensure_ascii=False), flush=True)
    write(OUT / "verification.json", {"at": now(),
        "runner_unchanged": sha(Path(__file__)) == frozen["runner_sha256"],
        "frozen_unchanged": sha(OUT / "frozen.json") == (OUT / "frozen.sha256").read_text().strip(),
        "protected_files_unchanged": all(sha(REPO / p) == v for p, v in frozen["source_sha256"].items()),
        "stop_reason": stopped, "attempted": attempted})


def decision(labels):
    return "fallback" if "fail" in labels.values() else "review" if "uncertain" in labels.values() else "allow"


def report():
    frozen = read(OUT / "frozen.json")
    rows = [json.loads(line) for line in (OUT / "results.jsonl").read_text().splitlines()]
    lookup = {r["id"]: r for r in frozen["cases"]}
    def rate(n, d):
        return {"n": n, "d": d, "pct": round(100 * n / d, 1) if d else None}
    def metrics(items):
        valid = [r for r in items if r["status"] == "success"]
        labels = lambda r: {k: r["checks"][k]["verdict"] for k in DIMENSIONS}
        expect_block = [r for r in items if decision(lookup[r["id"]]["expected"]) == "fallback"]
        expect_pass = [r for r in items if decision(lookup[r["id"]]["expected"]) == "allow"]
        return {"rows": len(items), "valid": len(valid),
                "decision_agreement": rate(sum(r["decision"] == decision(lookup[r["id"]]["expected"]) for r in valid), len(valid)),
                "all_dimensions_agree": rate(sum(labels(r) == lookup[r["id"]]["expected"] for r in valid), len(valid)),
                "must_block_but_allowed": [r["id"] for r in valid if r["id"] in {x["id"] for x in expect_block} and r["decision"] == "allow"],
                "must_block_but_reviewed": [r["id"] for r in valid if r["id"] in {x["id"] for x in expect_block} and r["decision"] == "review"],
                "must_pass_but_blocked": [r["id"] for r in valid if r["id"] in {x["id"] for x in expect_pass} and r["decision"] == "fallback"],
                "must_pass_but_reviewed": [r["id"] for r in valid if r["id"] in {x["id"] for x in expect_pass} and r["decision"] == "review"],
                "dimension_agreement": {k: rate(sum(r["checks"][k]["verdict"] == lookup[r["id"]]["expected"][k] for r in valid), len(valid)) for k in DIMENSIONS},
                "mismatches": [{"id": r["id"], "expected": lookup[r["id"]]["expected"], "actual": labels(r),
                                "checks": r["checks"]} for r in valid if labels(r) != lookup[r["id"]]["expected"]],
                "errors_detail": [{"id": r["id"], "reason": r.get("reason"), "http_status": r.get("http_status")} for r in items if r["status"] != "success"]}
    first = [r for r in rows if r["trial"] == 1]
    times = sorted(r["elapsed_ms"] for r in rows if r.get("invocation_attempted"))
    usage = {}
    for row in rows:
        for key, value in row.get("usage", {}).items():
            usage[key] = usage.get(key, 0) + value
    repeated = {}
    for cid in frozen["repeat_ids"]:
        selected = [r for r in rows if r["id"] == cid]
        sigs = [tuple(r["checks"][k]["verdict"] for k in DIMENSIONS) if r["status"] == "success" else (r["status"], r.get("reason")) for r in selected]
        repeated[cid] = {"trials": len(selected), "all_same_and_valid": len(set(sigs)) == 1 and all(r["status"] == "success" for r in selected), "signatures": sigs}
    result = {"id": "BC027-IMPL-002", "at": now(), "annotation": frozen["annotation"], "config": frozen["config"],
              "scheduled": len(frozen["schedule"]), "attempted": sum(bool(r.get("invocation_attempted")) for r in rows),
              "valid": sum(r["status"] == "success" for r in rows), "errors": sum(r["status"] == "error" for r in rows),
              "first_trial": metrics(first),
              "p50_ms": times[math.ceil(.5 * len(times)) - 1] if times else None,
              "p95_ms": times[math.ceil(.95 * len(times)) - 1] if times else None,
              "usage_reported": usage,
              "repeats": repeated,
              "cost_note": "仅响应报告usage；超时可能已计费但无usage，供应商账单未核验，不估算金额。",
              "overall_gate_pass": all(r["status"] == "success" and r["decision"] == decision(lookup[r["id"]]["expected"]) for r in first)}
    write(OUT / "summary.json", result)
    print(json.dumps({"attempted": result["attempted"], "valid": result["valid"], "errors": result["errors"],
                      "gate_pass": result["overall_gate_pass"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["freeze", "run", "report", "self-check"])
    args = parser.parse_args()
    if args.action == "self-check":
        self_check(build_cases())
        print("self-check passed; no API")
    else:
        {"freeze": freeze, "run": run, "report": report}[args.action]()
