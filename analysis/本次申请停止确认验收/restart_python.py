"""重启本项目8000服务；check临时开启取消判断，default恢复默认关闭。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
mode = sys.argv[1]
assert mode in ("check", "default")
listeners = subprocess.run(["lsof", "-t", "-iTCP:8000", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
assert len(set(listeners)) <= 1, listeners
old_pid = int(listeners[0]) if listeners else None
if old_pid:
    command = subprocess.check_output(["ps", "-p", str(old_pid), "-o", "command="], text=True)
    assert str(ROOT / ".venv") in command and "api.routes:app" in command and "--port 8000" in command
    os.kill(old_pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while subprocess.run(["lsof", "-t", "-iTCP:8000", "-sTCP:LISTEN"], capture_output=True).stdout:
        assert time.monotonic() < deadline, "旧8000服务尚未退出"
        time.sleep(0.2)

env = os.environ.copy()
env.update({"PYTHONPATH": str(ROOT / "backend"), "AGENT_COURSE_DISABLE_LLM": "0",
            "AGENT_COURSE_OFFLINE_RAG": "1", "AGENT_COURSE_OFFLINE_FACTS": "0",
            "AGENT_COURSE_ENV": str(ROOT / ".env.model.local"),
            "AGENT_ECOMMERCE_SERVICE_TOKEN": "course-debug-agent-service",
            "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false",
            "CANCEL_INTENT_EXECUTE": "1" if mode == "check" else "0",
            "CANCEL_INTENT_OBSERVE": "0", "CANCEL_SCORE_MIN": "0.9"})
with (OUT / f"python-{mode}.log").open("a") as log:
    process = subprocess.Popen([str(ROOT / ".venv/bin/uvicorn"), "api.routes:app", "--host", "127.0.0.1", "--port", "8000"],
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
deadline = time.monotonic() + 40
while True:
    assert process.poll() is None, "新8000服务提前退出，见本轮日志"
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
            health = json.load(response)
        assert health["status"] == "ok"
        break
    except (OSError, AssertionError):
        assert time.monotonic() < deadline, "新8000服务未就绪"
        time.sleep(0.2)
record = {"mode": mode, "old_pid": old_pid, "new_pid": process.pid, "health": health,
          "cancel_execute": env["CANCEL_INTENT_EXECUTE"], "score_min": "0.9"}
(OUT / f"python-{mode}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(record, ensure_ascii=False))
