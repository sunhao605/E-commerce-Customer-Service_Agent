"""用 BC032-LIVE-001 的 29 轮原始记录离线复核验收口径修订。

不启动服务、不调用模型：把每轮已记录的 Python/Java/页面状态重新送入当前 cases.json
与 run.py 的 evaluate，验证口径修订只影响误判，不改变真实产品失败的判定。
"""
import importlib.util
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(OUT))

spec = importlib.util.spec_from_file_location("bc032_runner", OUT / "run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

CASES = json.loads((OUT / "cases.json").read_text())
TURN_BY_ID = {turn["id"]: turn for case in CASES["cases"] for turn in case["turns"]}
ROWS = [json.loads(line) for line in (OUT / "turns-BC032-LIVE-001.jsonl").read_text().splitlines()]

# 口径修订后首次记录的预期重分类：四轮口径误判转PASS；三轮真产品缺口（页面无候选）保持FAIL；
# G05-T4 的证据在首次运行中被重复 seed 覆盖，离线无法还原，只能等新执行，保持FAIL。
EXPECTED = {
    "pass": {"G02-T3", "G02B-T2", "G03B-T3", "G03C-T2"},
    "still_fail_product": {"G02-T1", "G03B-T1", "G03B-T2"},
    "still_fail_poisoned": {"G05-T4"},
}


def replay_row(row):
    turn = TURN_BY_ID[row["turn"]]
    py = row.get("python") or {}
    python_turn = {"response": {
        "answer": py.get("answer"),
        "session_state": {"intent": py.get("intent"), "task_relation": py.get("task_relation"),
                          "context_report": {"chosen_order_id": py.get("chosen_order_id")}},
    }}
    checks, failed, blocking, _ = runner.evaluate(
        turn, python_turn, row.get("memory") or {}, row.get("before_memory") or {},
        {"session": (row.get("java") or {}).get("before")}, {"session": (row.get("java") or {}).get("after")},
        row.get("response") or {}, row.get("page") or {})
    verdict = "BLOCKED" if blocking else ("FAIL" if failed else "PASS")
    return {"turn": row["turn"], "original_verdict": row["verdict"], "original_failed": row["failed"],
            "verdict": verdict, "failed": failed, "blocking": blocking}


def main():
    results = [replay_row(row) for row in ROWS]
    by_turn = {item["turn"]: item for item in results}
    summary = {
        "total": len(results),
        "pass": sum(1 for item in results if item["verdict"] == "PASS"),
        "fail": [item["turn"] for item in results if item["verdict"] == "FAIL"],
        "reclassified": [item["turn"] for item in results
                         if item["verdict"] != item["original_verdict"]],
    }
    for turn in EXPECTED["pass"]:
        assert by_turn[turn]["verdict"] == "PASS", (turn, by_turn[turn])
    for turn in EXPECTED["still_fail_product"]:
        assert by_turn[turn]["verdict"] == "FAIL" and by_turn[turn]["failed"] == ["页面候选顺序"], by_turn[turn]
    for turn in EXPECTED["still_fail_poisoned"]:
        assert by_turn[turn]["verdict"] == "FAIL", by_turn[turn]
    (OUT / "replay-口径修订.json").write_text(
        json.dumps({"summary": summary, "rows": results}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
