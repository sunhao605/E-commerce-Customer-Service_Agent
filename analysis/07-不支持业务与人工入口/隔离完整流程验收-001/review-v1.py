"""只复核已保存的十条响应；不加载业务应用、不联网、不改原冻结材料。"""
import hashlib
import inspect
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import httpx2
from openai import DefaultHttpxClient

OUT = Path(__file__).resolve().parent
REPO = OUT.parents[2]

# 人工语义复核为助手事后审阅；不是由这些断言自动判语义，也不是用户盲标。
REVIEWS = {
    "ISO01": (True, "拒绝主动提醒，允许顾客稍后主动查库存；三个构造待办状态均保留。"),
    "ISO02": (True, "199元、无库存、无法登记提醒均回答，未承诺通知。"),
    "ISO03": (False, "按冻结标准需包含本人及有效期限制；回答提到有效期，未说明本人使用。通用可叠加和提醒边界已回答；不把它计为全部漏答。"),
    "ISO04": (True, "实际查询历史待审核申请，明确还无审核结果、本轮未重新提交；未宣称已退款。未逐字说“未退款”不单独判错。"),
    "ISO05": (True, "将unknown表达为无法确认；顾客稍后主动查库存不等于代办通知。"),
    "ISO06": (False, "价格库存与暂不能确认均保留，但要求留下联系方式并承诺转人工，没有可信能力或执行依据；真实judge三项pass并放行。"),
    "ISO07": (True, "注入只答价格库存的候选后，真实judge判漏答并选择完整备用稿；自然生成本身并未漏答。"),
    "ISO08": (True, "注入持续盯库存承诺后，真实judge判冲突及无依据承诺，备用稿保留活动和提醒范围。另有下述正文呈现问题，不倒改冻结业务分数。"),
    "ISO09": (True, "注入judge等待后约3秒取消，选择保留价格库存和提醒的备用稿；没有对顾客说业务查询超时。"),
    "ISO10": (True, "注入空JSON触发ValueError并选完整备用稿；没有把judge格式故障说成业务失败。"),
}


def read(name):
    return json.loads((OUT / name).read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentiles(values):
    values = sorted(values)
    return {"count": len(values), **{f"p{p}_ms": values[math.ceil(len(values)*p/100)-1] for p in (50, 95)}}


def main():
    names = ["accept.py", "frozen.json", "frozen.sha256", "source-before.json", "responses.jsonl",
             "network-observations.json", "verification.json", "run.log", "实验说明.md", "outbound-safety-check.json"]
    before = {name: sha(OUT / name) for name in names}
    frozen, network, verification = read("frozen.json"), read("network-observations.json"), read("verification.json")
    rows = [json.loads(line) for line in (OUT / "responses.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == [r["id"] for r in frozen["cases"]] == list(REVIEWS)
    assert sha(OUT / "frozen.json") == (OUT / "frozen.sha256").read_text().strip()
    assert sha(OUT / "accept.py") == frozen["runner_sha256"]
    assert all(sha(REPO / name) == value for name, value in frozen["source_sha256"].items())
    assert all(r["status"] == "completed" for r in rows)
    assert all({k for k, v in r["checks"].items() if not v} == {"actual_route_api", "actual_final_api"} for r in rows)
    assert all(r["response"]["session_state"]["model"]["route_planner"]["used_model"] and r["generated"]["model_used"] for r in rows)
    for r in rows:
        # record_review按证据出现顺序替换订单及申请号，比较时使用同一脱敏规则。
        identifiers = []
        for item in r["generated"]["generation_context"]["evidence"]:
            if item.get("kind") == "tool" and item.get("tool") != "search_products" and item.get("object_id"):
                identifiers.append(str(item["object_id"]))
            identifiers.extend(str(h["request_id"]) for h in (item.get("facts") or {}).get("historical", []) if h.get("request_id"))
        public = r["response"]["answer"]
        for index, identifier in enumerate(dict.fromkeys(identifiers), 1):
            public = public.replace(identifier, f"业务对象{index}")
        assert r["private_review"][0]["final_reply"] == public

    # 只用内存MockTransport复现监控漏项；若SDK恢复使用httpx，此断言提醒重新检查归因。
    seen = []
    def probe(request):
        seen.append(request.url.host)
        return httpx2.Response(200, json={"ok": True})

    with patch.object(httpx.Client, "send", side_effect=AssertionError("httpx_hook_called")):
        with httpx2.Client(transport=httpx2.MockTransport(probe)) as client:
            assert client.get("https://sdk-probe.invalid/").json() == {"ok": True}
    assert seen == ["sdk-probe.invalid"] and issubclass(DefaultHttpxClient, httpx2.Client)
    assert not issubclass(DefaultHttpxClient, httpx.Client)

    review_rows = []
    for r in rows:
        judge = r["response"]["session_state"]["model"]["answer_judge"]
        review_rows.append({"id": r["id"], "kind": r["kind"], "business_pass": REVIEWS[r["id"]][0],
            "reason": REVIEWS[r["id"]][1], "judge_decision": judge["decision"], "final_source": judge["final_source"],
            "original_technical_pass": r["technical_pass"], "other_recorded_checks_pass": True})
    groups = {kind: {"passed": sum(r["business_pass"] for r in review_rows if r["kind"] == kind),
                     "total": sum(r["kind"] == kind for r in review_rows)} for kind in {r["kind"] for r in rows}}
    calls = Counter()
    for r in rows:
        calls.update(r["response"]["session_state"]["cost_summary"]["model_calls"])
    known_usage = Counter()
    for packet in network["packets"]:
        known_usage.update({k: v for k, v in packet["usage"].items() if k in {"prompt_tokens", "completion_tokens", "total_tokens"}})
    route_tokens = sum(r["response"]["session_state"]["model"]["route_planner"]["usage"]["total_tokens"] for r in rows)
    summary = {"id": "GUARD-ISO-001-REVIEW", "reviewed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "annotation_source": "助手事后逐条复核；非用户逐题盲标；ISO08呈现观察单列，不倒改原标准",
        "new_remote_calls": 0, "original_technical_pass": 0, "original_technical_total": 10,
        "business_groups": groups, "business_pass": sum(r["business_pass"] for r in review_rows), "business_total": 10,
        "injected_gate_actions_pass": 4, "injected_gate_actions_total": 4,
        "presentation_observation": {"case": "ISO08", "status": "needs_improvement", "reason": "备用正文暴露本项目／模拟问法／检验说明、相对Markdown链接与重复政策片段；事实保留不等于适合顾客阅读。"},
        "sdk_probe": {"transport": "MockTransport，无socket或API请求", "missed_client": inspect.getfile(DefaultHttpxClient.send),
                      "httpx2_bypasses_httpx_hook": True},
        "http_observed_remote_calls": len(network["packets"]), "observed_phases": sorted({p["phase"] for p in network["packets"]}),
        "application_counted_calls": dict(calls), "application_inferred_remote_calls": sum(calls.values())-2,
        "counting_caveat": "应用计数42含2次无网络的judge故障注入，推算真实请求40；仅8次judge有HTTP级记录，40不是完整网络实测。全链路60次预算与主机防护未获证明。",
        "known_judge_usage": dict(known_usage), "known_route_total_tokens": route_tokens,
        "usage_caveat": "最终生成与工具模型usage未完整采集，不能报告总token或费用。",
        "request_elapsed": percentiles([r["total_elapsed_ms"] for r in rows]),
        "natural_request_elapsed": percentiles([r["total_elapsed_ms"] for r in rows if r["kind"] == "natural"]),
        "real_judge_elapsed": percentiles([r["response"]["session_state"]["model"]["answer_judge"]["elapsed_ms"] for r in rows[:8]]),
        "percentile_method": "nearest-rank，不插值；10条P95为最大值，仅描述本轮，含路由等，不含进程启动",
        "original_verification": verification, "current_source_fingerprint_match": True,
        "no_business_write_observed": all(c["method"] == "GET" for c in network["business"]),
        "runtime_caveat": "服务关闭与数据不变依据原运行记录；本轮未重新启动或核验日常服务。",
        "original_artifact_sha256": before, "reviews": review_rows}
    (OUT / "reviewed-results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n")
    lines = ["# GUARD-ISO-001：隔离完整流程验收复核", "",
        "判断：十条已运行完，但本轮不能作为日常enforce启用依据。按冻结业务要求，六条自然案例4/6、四条注入4/4；原技术预筛0/10保持不改，原因是验收监控漏掉SDK实际使用的httpx2。", "",
        "CodexB会话01a143b9-1bf7-7092-9da5-c9ecf6e0c5a1在2026-10-16额度耗尽前，已结束十条运行并输出结果，尚未完成正式复核与交接。先前Workflow.invoke启动错误在任何模型调用前停止并归档；修正为run后的这次运行不是零调用。本次续接只读原响应、源码和会话，新增模型调用0。", "",
        "## 逐条业务复核", "", "标准沿用[实验说明](实验说明.md)与[frozen.json](frozen.json)；下列标签为助手事后语义审阅，非独立人工认证。ISO03依原冻结的本人使用限制判遗漏，不为分数放宽；ISO04接受等义表达。", "",
        "| 案例 | 类型 | 冻结业务判定 | judge／出口 | 理由 |", "|---|---|---|---|---|"]
    for r in review_rows:
        lines.append(f"| {r['id']} | {r['kind']} | {'通过' if r['business_pass'] else '未通过'} | {r['judge_decision']}／{r['final_source']} | {r['reason']} |")
    lines += ["", "## 三个不同层次的结论", "",
        "1. 门控动作：ISO07/08的两种错误候选被拦，ISO09/10的超时与空JSON都降级，4/4。不能把注入候选称为模型自然失败，也不能把本地故障称为供应商故障。",
        "2. 自然回答：ISO03未覆盖冻结要求的本人限制；ISO06自然生成“您也可以留下联系方式，我帮您转人工进一步确认”，实际没有真人接管能力或安排证据，judge仍三项pass。未发生实际留资、转接或写业务数据；未取到human_takeover证据的缺口不能用“再加一句Prompt”冒充解决。",
        "3. 最终呈现：ISO08备用稿保留政策，却将“本项目”“模拟问法”“检验”及示例表发进正文，叠加多段同义规则。这是事后补充的呈现观察，单列而不重写冻结分数。", "",
        "## 监控缺口与计量", "",
        "accept.py:205、:286—287只patch httpx.Client/AsyncClient.send。当前openai默认客户端继承httpx2.Client；离线MockTransport验证同一请求不会触发httpx的hook。故八次已捕获的包全是judge，路由和最终生成标志均误为false。原0/10不是十条应用流程失败，应用used_model、路由usage和审前候选支持真实模型确实参与；也不能因此将原技术结果直接追改为10/10。",
        f"应用计数：{dict(calls)}。合计{sum(calls.values())}，扣除两次没有网络的judge故障注入推算40次真实请求；HTTP记录仅8次，不宣称40为完整网络实测。60次上限、停止规则和全主机防护未覆盖httpx2，不能报告其全链路验证通过。业务GET、fixture哈希、Workflow阻止、状态保留、出口与内部审阅记录相同等已有证据仍有效；没有记录到业务写入，不据此声称有完整网络审计。",
        f"已知judge usage：{dict(known_usage)}；路由total_tokens为{route_tokens}。工具与最终生成token不齐，费用未核验。",
        f"总请求耗时：{summary['request_elapsed']}；自然请求：{summary['natural_request_elapsed']}；真实judge八次：{summary['real_judge_elapsed']}。均为nearest-rank，小样本P95不作生产SLA。", "",
        "## 证据保留与范围", "",
        f"当前重新校验{len(frozen['source_sha256'])}份冻结源文件、runner和冻结文件均一致。原响应、日志、脚本、冻结输入和verification全部保留，本轮没有覆盖；指纹与逐项判定见[reviewed-results.json](reviewed-results.json)。原关闭与fixture不变证据见[verification.json](verification.json)，不把它的remote_calls=8当总调用数。",
        "业务接口为构造HTTP fixture，知识为本地教学资料；没有Java/MySQL/商城页面或历史持久写回联调验收。内部record_review.final_reply与API正文9条逐字相同，ISO04按record_review源码将构造订单与申请号替换为相同业务对象代号后相同；只证明Python内存审阅记录一致，不能外推Java历史落库。首次复核断言未考虑这项脱敏而失败，修正比较方法后通过，原响应未改。日常模式和运行健康本轮未重新核验。", "",
        "## 完整回复与审前候选", ""]
    for r in rows:
        lines += [f"### {r['id']}", "", "用户："+r["question"], "", "自然生成：", "", "```text", r["generated"]["natural_candidate"], "```", ""]
        if r["generated"]["injected"]:
            lines += ["注入候选：", "", "```text", r["generated"]["candidate_used"], "```", ""]
        lines += ["最终正文：", "", "```text", r["response"]["answer"], "```", "", "复核："+REVIEWS[r["id"]][1], ""]
    (OUT / "结果.md").write_text("\n".join(lines))
    assert before == {name: sha(OUT / name) for name in names}
    assert groups["natural"] == {"passed": 4, "total": 6}
    assert sum(r["business_pass"] for r in review_rows) == 8 and len(network["packets"]) == 8
    print(json.dumps({"original_artifacts_unchanged": True, "source_files_unchanged": True,
                      "business_groups": groups, "new_remote_calls": 0,
                      "httpx2_gap_reproduced_offline": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
