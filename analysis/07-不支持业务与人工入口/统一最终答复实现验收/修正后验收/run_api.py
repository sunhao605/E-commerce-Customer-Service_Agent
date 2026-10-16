"""四个API新会话的真实只读咨询；保留每次响应，不自动重试失败。"""
import hashlib
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


frozen = json.loads((OUT / "frozen-live-cases.json").read_text())
assert len(frozen["cases"]) == 6
frozen["cases"] = [c for c in frozen["cases"] if c["surface"] == "api"]
assert len(frozen["cases"]) == 4
assert len({c["id"] for c in frozen["cases"]}) == 4
assert not list(OUT.glob("ANSWER-RECHECK-*.json")), "已有运行结果，不覆盖或自动重跑"
paths = [p for folder in ("backend/agent", "backend/retrieval", "backend/resources/prompts", "backend/resources/knowledge")
         for p in (ROOT / folder).rglob("*") if p.suffix in {".py", ".md", ".json", ".yml"}]
paths += [ROOT / "backend/settings.py", OUT / "frozen-live-cases.json"]
save("source-manifest.json", {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)})
with httpx.Client(base_url="http://127.0.0.1:8081", timeout=55, trust_env=False) as mall, \
     httpx.Client(base_url="http://127.0.0.1:8000", timeout=10, trust_env=False) as agent:
    health = agent.get("/health"); health.raise_for_status()
    before = health.json()
    assert before["rag_index"]["embedding"] == "openai_compatible_embedding:Qwen/Qwen3-Embedding-8B"
    assert before["rag_index"]["chunk_count"] == 115
    save("health-before.json", before)
    save("version.json", {"started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                          "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                          "version_scope": "当前工作区磁盘指纹；运行索引以health和逐条trace核对，不读取或保存凭据"})
    # 复用已有本地演示账户；不保存 Cookie、令牌或登录响应。
    login = mall.post("/api/auth/login", json={"username": "zhangsan", "password": "123456"})
    login.raise_for_status()
    assert login.json()["success"] is True
    products = mall.get("/api/products"); products.raise_for_status()
    chair = next(p for p in products.json()["data"] if p["name"] == "轻量露营折叠椅")
    save("business-fact.json", {k: chair[k] for k in ("id", "name", "price", "stock")})
    contracts = {}
    for capability_id in sorted({cid for case in frozen["cases"] for cid in case["capability_ids"]}):
        response = mall.get(f"/api/customer-service/capabilities/{capability_id}")
        response.raise_for_status()
        contracts[capability_id] = response.json()["data"]
        assert contracts[capability_id]["version"] == "service-capabilities-v1"
    save("capability-contracts.json", contracts)
    sessions = set()
    for case in frozen["cases"]:
        session = "answer-live-" + uuid4().hex
        assert session not in sessions
        sessions.add(session)
        prior = agent.get(f"/sessions/{session}/trace"); prior.raise_for_status()
        assert prior.json() == [], "新会话不应已有Trace"
        request = {"sessionId": session, "message": case["message"]}
        start = time.perf_counter()
        row = {"case": case, "request": request, "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
        try:
            response = mall.post("/api/customer-service/chat", json=request)
            row.update({"status_code": response.status_code, "response": response.json()})
        except Exception as exc:
            row["error"] = {"type": type(exc).__name__, "message": str(exc)}
        row["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
        save(case["id"] + ".json", row)
        trace = agent.get(f"/sessions/{session}/trace"); trace.raise_for_status()
        row["trace"] = trace.json()
        save(case["id"] + ".json", row)
        print(json.dumps({"id": case["id"], "elapsed_ms": row["elapsed_ms"],
                          "answer": row.get("response", {}).get("data", {}).get("answer"),
                          "error": row.get("error")}, ensure_ascii=False), flush=True)
    assert len(sessions) == 4
    health = agent.get("/health"); health.raise_for_status()
    save("health-after.json", health.json())
