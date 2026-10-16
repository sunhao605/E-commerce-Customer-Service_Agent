"""按实时PID完整重载8000，相关环境只在内存中复制，拒绝停止其他工作区。"""
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
before = json.loads((OUT / "runtime-before.json").read_text())


def listeners():
    result = subprocess.run(["/usr/sbin/lsof", "-t", "-nP", "-iTCP:8000", "-sTCP:LISTEN"], capture_output=True, text=True)
    return sorted(set(map(int, result.stdout.split())))


assert listeners() == [before["pid"]], "进程已变，不停止"
cwd = subprocess.check_output(["/usr/sbin/lsof", "-a", "-p", str(before["pid"]), "-d", "cwd", "-Fn"], text=True)
assert f"n{ROOT}" in cwd.splitlines()
raw = subprocess.check_output(["/bin/ps", "eww", "-p", str(before["pid"]), "-o", "command="], text=True)
old_env = dict(re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([^\s]*)", raw))
assert all(old_env.get(k) == v for k, v in before["settings"].items())
env = dict(os.environ)
env.update({k: v for k, v in old_env.items() if k.startswith(("AGENT_", "CANCEL_", "LANGCHAIN_", "LANGSMITH_", "ECOMMERCE_"))})
env["PYTHONPATH"] = str(ROOT / "backend")
with httpx.Client(timeout=15, trust_env=False) as client:
    response = client.get("http://127.0.0.1:8081/actuator/health"); response.raise_for_status()
os.kill(before["pid"], signal.SIGTERM)
for _ in range(50):
    if not listeners():
        break
    time.sleep(0.1)
assert not listeners(), "旧进程未退出"
with (OUT / "python-daily.log").open("ab") as log:
    child = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "api.routes:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
with httpx.Client(timeout=8, trust_env=False) as client:
    for _ in range(120):
        try:
            response = client.get("http://127.0.0.1:8000/health"); response.raise_for_status()
            health = response.json()
            break
        except (httpx.HTTPError, ValueError):
            if child.poll() is not None:
                raise RuntimeError("新进程退出，请查python-daily.log")
            time.sleep(0.25)
    else:
        raise RuntimeError("新进程未就绪")
assert listeners() == [child.pid]
for key in ("version", "snapshot_version", "chunk_count", "embedding", "governed_files"):
    assert health["rag_index"][key] == before["health"]["rag_index"][key], key
result = {"old_pid": before["pid"], "new_pid": child.pid, "settings": before["settings"], "health": health,
          "credentials_saved": False, "copied_environment_keys": sorted(k for k in old_env if k.startswith(("AGENT_", "CANCEL_", "LANGCHAIN_", "LANGSMITH_", "ECOMMERCE_")))}
(OUT / "runtime-after-python.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"old_pid": before["pid"], "new_pid": child.pid, "index": health["rag_index"]["version"], "chunks": health["rag_index"]["chunk_count"]}, ensure_ascii=False))
