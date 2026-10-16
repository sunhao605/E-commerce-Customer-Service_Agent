"""完整重载本项目8000；保留当前开关，凭据只在进程内使用。"""
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]


def listeners():
    result = subprocess.run(["/usr/sbin/lsof", "-t", "-nP", "-iTCP:8000", "-sTCP:LISTEN"],
                            text=True, capture_output=True)
    return list(dict.fromkeys(int(value) for value in result.stdout.split()))


old = listeners()
assert old == [json.loads((OUT / "runtime-before.json").read_text())["pid"]], "Current service changed; inspect before stopping"
cwd = subprocess.check_output(["/usr/sbin/lsof", "-a", "-p", str(old[0]), "-d", "cwd", "-Fn"], text=True)
assert f"n{ROOT}\n" in cwd, "Refuse to stop another checkout"
raw = subprocess.check_output(["/bin/ps", "eww", "-p", str(old[0]), "-o", "command="], text=True)
settings = json.loads((OUT / "runtime-before.json").read_text())["settings"]
assert settings["AGENT_COURSE_DISABLE_LLM"] == "0"
assert settings["AGENT_COURSE_OFFLINE_FACTS"] == "0"
assert settings["AGENT_COURSE_OFFLINE_RAG"] == "1"
assert settings["CANCEL_INTENT_EXECUTE"] == "0"
env = dict(os.environ)
env.update(settings)
env["PYTHONPATH"] = str(ROOT / "backend")
for key in ("AGENT_ECOMMERCE_SERVICE_TOKEN", "AGENT_OPENAI_API_KEY", "AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_MODEL"):
    match = re.search(r"(?:^|\s)" + key + r"=([^\s]*)", raw)
    if match:
        env[key] = match.group(1)
expected = json.loads((OUT / "preflight.json").read_text())
os.kill(old[0], signal.SIGTERM)
for _ in range(50):
    if not listeners():
        break
    time.sleep(0.1)
assert not listeners(), "Old service has not stopped"
with (OUT / "python-daily.log").open("ab") as log:
    child = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "api.routes:app",
                              "--host", "127.0.0.1", "--port", "8000"], cwd=ROOT, env=env,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
with httpx.Client(timeout=5, trust_env=False) as client:
    for _ in range(60):
        try:
            response = client.get("http://127.0.0.1:8000/health")
            response.raise_for_status()
            health = response.json()
            break
        except (httpx.HTTPError, ValueError):
            if child.poll() is not None:
                raise RuntimeError("New process exited; inspect python-daily.log")
            time.sleep(0.25)
    else:
        raise RuntimeError("New process did not become healthy")
assert listeners() == [child.pid]
assert health["rag_index"]["snapshot_version"] == expected["snapshot_version"]
assert health["rag_index"]["version"] == expected["index_version"]
assert set(health["rag_index"]["governed_files"]) == set(expected["governed_files"])
assert health["rag_index"]["chunk_count"] == expected["chunks"]
result = {"old_pid": old[0], "new_pid": child.pid, "settings": settings, "health": health,
          "fresh_process": True, "disk_index_matches_live": True, "credentials_logged": False}
(OUT / "runtime-after.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps({"old_pid": old[0], "new_pid": child.pid, "snapshot": expected["snapshot_version"],
                  "index": expected["index_version"], "chunks": expected["chunks"], "governed_files": 6}, ensure_ascii=False))
