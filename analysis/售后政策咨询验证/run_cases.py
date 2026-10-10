"""重跑现有 B01/B02；可传用例编号限定范围，先保存标准与完整证据。"""

import hashlib
import importlib.util
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("intent_eval", ROOT / "analysis/意图识别评测/run_intent_eval.py")
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)
sys.path.insert(0, str(ROOT / "backend"))
from agent.tools.planning import classify_intent

cases = [case for case in suite.parse_cases(suite.CASE_PATH.read_text()) if case["id"] in {"B01", "B02"}]
assert [case["id"] for case in cases] == ["B01", "B02"]
assert all(not case["gap"] and not case["prior_user_messages"] for case in cases)
assert [case["expected"] for case in cases] == [["refund_policy_query"], ["return_policy_query"]]
requested_ids = set(sys.argv[1:] or ["B01", "B02"])
assert requested_ids <= {"B01", "B02"}
cases = [case for case in cases if case["id"] in requested_ids]
started = datetime.now(ZoneInfo("Asia/Shanghai"))
run_id = started.strftime("%Y%m%d%H%M%S%f")
out = Path(__file__).resolve().parent / run_id
out.mkdir()


def save(name, value):
    (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def request(url, body=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.status, json.load(response)


sources = sorted((ROOT / "backend").rglob("*.py")) + sorted((ROOT / "backend/resources").rglob("*.md"))
save("manifest.json", {
    "run_id": run_id,
    "started_at": started.isoformat(),
    "entry": "running_python_agent_chat",
    "agent_url": suite.AGENT_URL,
    "version_boundary": "本目录无独立 Git 提交；保存磁盘源码和政策/Prompt 指纹，运行进程身份另行核验。",
    "sources": {str(path.relative_to(ROOT)): {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mtime": path.stat().st_mtime} for path in sources},
})
save("frozen_cases.json", {
    "frozen_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    "initial_state": "每条全新会话；runtime_user_id=intent-eval；无订单、历史、页面上下文；不清理共享 FAQ 缓存。",
    "expected": "回答对应售后通用政策并给出匹配依据；不要求订单号，不创建办理待办/确认卡/申请；不承诺具体订单有资格。",
    "return_policy_expected": "保留商品支持七天无理由的限制；无理由无需用户另填原因；说明确认提交待审核、同意后寄回、收到且验收通过才退款。",
    "allowed": "售后政策只读检索、模型解释，或基于核验政策的确定性回复；标签分别为 refund_policy_query、return_policy_query，另独立判断业务行为。",
    "forbidden": ["回答发票或其他无关政策", "把咨询作为退款/退货办理", "强制补订单号", "新增待办理任务或确认卡", "提交申请或批准退款"],
    "cases": cases,
})
_, health = request("http://127.0.0.1:8000/health")
assert health.get("status") == "ok"
results = []
for case in cases:
    case_id = case["id"]
    body = {"session_id": f"policy-eval-{run_id}-{case_id}", "runtime_user_id": "intent-eval", "user_message": case["message"], "debug": False}
    save(f"{case_id}-request.json", body)
    tick = time.perf_counter()
    status, payload = request(suite.AGENT_URL, body)
    elapsed_ms = round((time.perf_counter() - tick) * 1000)
    save(f"{case_id}-response.json", payload)
    _, trace = request(f"http://127.0.0.1:8000/sessions/{body['session_id']}/trace")
    save(f"{case_id}-trace.json", trace)
    state = payload.get("session_state") or {}
    cost = state.get("cost_summary") or {}
    result = {"id": case_id, "input": case["message"], "session_id": body["session_id"], "http_status": status, "elapsed_ms": elapsed_ms, "rule_fallback": classify_intent(case["message"]), "intent": state.get("intent"), "answer": payload.get("answer"), "next_action": state.get("next_action"), "cache_hit": (cost.get("rag") or {}).get("cache_hit"), "model": state.get("model"), "route_plan": state.get("route_plan"), "memory": state.get("memory"), "cost_summary": cost, "citations": payload.get("citations"), "tool_calls": payload.get("tool_calls"), "workflow": state.get("workflow"), "clarification": payload.get("clarification"), "trace_events": [event["event_type"] for event in trace]}
    results.append(result)
    save("result.json", {"run_id": run_id, "health": health, "results": results})
    print(json.dumps(result, ensure_ascii=False), flush=True)
assert len(results) == len(cases) and all(item["http_status"] == 200 for item in results)
print(f"Saved: {out}", flush=True)
