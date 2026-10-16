"""续接时恢复本项目已停止的Python与两套前端，沿用已核验开关。"""
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
NODE = Path("/Users/sunhao/.nvm/versions/node/v24.15.0/bin/node")
settings = json.loads((OUT / "runtime-after.json").read_text())["settings"]
expected = json.loads((OUT / "preflight.json").read_text())
assert NODE.is_file()
assert settings["AGENT_COURSE_DISABLE_LLM"] == "0" and settings["AGENT_COURSE_OFFLINE_FACTS"] == "0"
assert settings["CANCEL_INTENT_EXECUTE"] == "0"


def listeners(port):
    process = subprocess.run(["/usr/sbin/lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             text=True, capture_output=True)
    return list(dict.fromkeys(int(value) for value in process.stdout.split()))


env = dict(os.environ)
for key in ("AGENT_OPENAI_API_KEY", "AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_MODEL"):
    env.pop(key, None)
env.update(settings, PYTHONPATH=str(ROOT / "backend"))
frontenv = dict(os.environ, VITE_AGENT_BASE_URL="http://127.0.0.1:8000", VITE_ECOMMERCE_BASE_URL="http://127.0.0.1:8081")
services = (
    ("agent", 8000, ROOT, [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "api.routes:app", "--host", "127.0.0.1", "--port", "8000"], env, "/health"),
    ("workbench", 5173, ROOT / "frontend", [str(NODE), "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "5173", "--strictPort"], frontenv, "/"),
    ("mall", 5174, ROOT / "ecommerce-backend/frontend", [str(NODE), "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "5174", "--strictPort"], frontenv, "/"),
)
result = {"checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "services": {}, "settings": settings}
with httpx.Client(timeout=5, trust_env=False) as client:
    for name, port, cwd, command, selected_env, path in services:
        old = listeners(port)
        child = None
        if old:
            for pid in old:
                actual = subprocess.check_output(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], text=True)
                assert f"n{cwd}\n" in actual, f"Different checkout occupies {port}"
        else:
            with (OUT / f"restored-{name}.log").open("ab") as log:
                child = subprocess.Popen(command, cwd=cwd, env=selected_env, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(60):
            try:
                response = client.get(f"http://127.0.0.1:{port}{path}")
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if child is not None and child.poll() is not None:
                    raise RuntimeError(f"{name} exited; inspect restored-{name}.log")
                time.sleep(0.25)
        else:
            raise RuntimeError(f"{name} did not become healthy")
        result["services"][name] = {"pids": listeners(port), "status_code": response.status_code,
                                     "body": response.json() if name == "agent" else {"html_bytes": len(response.content)}}
    response = client.get("http://127.0.0.1:8081/actuator/health")
    response.raise_for_status()
    result["services"]["java"] = response.json()
rag = result["services"]["agent"]["body"]["rag_index"]
assert rag["version"] == expected["index_version"] and rag["snapshot_version"] == expected["snapshot_version"]
assert rag["chunk_count"] == 85 and len(rag["governed_files"]) == 6
(OUT / "runtime-restored.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps({"services": {name: state.get("pids", state) for name, state in result["services"].items()},
                  "snapshot": rag["snapshot_version"], "index": rag["version"]}, ensure_ascii=False))
