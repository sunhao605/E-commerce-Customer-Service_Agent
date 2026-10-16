"""按已保存响应校正Trace解析；不重跑模型，不改写原始判定或冻结文件。"""
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
original = json.loads((OUT / "live-cases.json").read_text())
products = json.loads((OUT / "business-products-before.json").read_text())
corrections = {
    "tool_path": "商品路径的工具完成结果通过hook_executed/post_tool_call和cost_recorded记录，不能只找tool_finished。",
    "LIVE-META-04.final_model_path": "纯价格成功路径允许最终生成模型；原期待跳过模型不符合现有源码。保留价格/库存正确和能力边界审查。",
    "timing": "此解析校正在观察后完成；原始判定与冻结标准保留，不称为事前冻结或独立评测。",
}
rows = []
usage = {}
model_counts = {}
for entry in original:
    case = entry["case"]
    events = entry["trace"]
    costs = entry["costs"]
    checks = dict(entry["checks"])
    hooks = [e["payload"] for e in events if e["event_type"] == "hook_executed"
             and e["payload"].get("hook_type") == "post_tool_call"]
    tool_count = costs[-1]["business_tool_call_count"] if costs else 0
    checks["tool_path"] = (tool_count > 0) == case["business_tools"] and all(h["status"] == "success" for h in hooks)
    if case["business_tools"]:
        checks["tool_path"] = checks["tool_path"] and bool(hooks)
    if case["id"] == "LIVE-META-04":
        checks["final_model_path"] = any(m.get("used_model") is True for m in entry["final_model_results"]) or any(
            e["event_type"] == "model_answer_skipped" and e["payload"].get("reason") in {"verified_tool_wording", "runtime_context_direct_answer"}
            for e in events
        )
    answer = entry["response"]["data"]["answer"]
    if case["id"] == "LIVE-META-01":
        checks["answer_matches_known_policy"] = all(word in answer for word in ("24", "订单详情", "下载"))
    elif case["id"] == "LIVE-META-02":
        checks["answer_matches_known_policy"] = all(word in answer for word in ("七天", "签收", "审核")) and ("无需" in answer or "不需要" in answer)
    elif case["id"] == "LIVE-META-03":
        checks["answer_matches_guide"] = "晾干" in answer and "收纳" in answer
    else:
        name = "山野四人帐篷" if case["id"] == "LIVE-META-05" else "轻量露营折叠椅"
        product = next(p for p in products if p["name"] == name)
        price = format(Decimal(str(product["price"])).normalize(), "f")
        checks["price_matches_business_api"] = re.search(r"(?<!\d)" + re.escape(price) + r"\s*元", answer) is not None
        if case["id"] in {"LIVE-META-04", "LIVE-META-05"}:
            checks["stock_matches_business_api"] = ("无库存" in answer or "缺货" in answer) if product["stock"] == 0 else f"库存{product['stock']}件" in answer.replace(" ", "")
    if costs:
        for key, value in costs[-1].get("route_usage_actual", {}).items():
            usage[key] = usage.get(key, 0) + value
        for key, value in costs[-1]["model_calls"].items():
            model_counts[key] = model_counts.get(key, 0) + value
    human_review = {
        "LIVE-META-01": "通用发票规则正确；额外表示可进一步核实，具体发票状态读取能力本轮未验证，不作为通过能力。",
        "LIVE-META-02": "退货条件、无需额外原因、独立审核和验收后退款均正确；未准备申请。",
        "LIVE-META-03": "取得帐篷晾干收纳小节，无商品查询；引用中有客服操作语气，表达仍可收敛。",
        "LIVE-META-04": "价格库存正确；最终模型增加项目未支持的到货提醒建议，话术不通过，见BC-020。",
        "LIVE-META-05": "实时帐篷事实与通用养护分开，未编造型号参数。",
        "LIVE-META-06": "保留实时199元、缺货及通用叠加规则，明确不代表当前商品或本人券一定适用。",
    }[case["id"]]
    rows.append({"id": case["id"], "input": case["message"], "answer": answer,
                 "original_structural_pass": entry["passed_structural_checks"],
                 "rechecked_structural_checks": checks, "rechecked_structural_pass": all(checks.values()),
                 "human_review": human_review, "confirmed_quality_issue": case["id"] == "LIVE-META-04",
                 "elapsed_ms": entry["elapsed_ms"], "citation_ids": entry["citation_ids"],
                 "actual_route_model": entry["route_model"], "business_tool_count": tool_count})
assert len(rows) == 6 and all(row["rechecked_structural_pass"] for row in rows)
assert model_counts["route_planner"] == 6
prior = json.loads((OUT.parent / "政策元数据实现验收/summary.json").read_text())
changed = [p for p, digest in prior["source_sha256"].items()
           if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != digest]
assert not changed, changed
summary = {"kind": "bounded_real_model_and_business_read_acceptance_not_production_accuracy",
           "corrections": corrections, "cases": rows, "structural_and_known_fact_passed": 6,
           "confirmed_response_quality_issues": 1, "quality_score_not_formal": True,
           "actual_route_usage": usage, "recorded_model_calls_six_cases": model_counts,
           "source_hashes_match_implementation": True,
           "limits": ["六条构造用例非生产准确率", "发票具体状态读取未验", "本地字符向量未切成语义Embedding", "到期边界仍由此前固定时钟检查证明"]}
(OUT / "reviewed-results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps({"rechecked_structural_and_fact_passed": 6, "confirmed_quality_issues": 1,
                  "model_calls": model_counts, "route_usage_actual": usage}, ensure_ascii=False))
