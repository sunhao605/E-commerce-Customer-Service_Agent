"""只读保存日常服务、配置指纹与源码基线；不保存凭据。"""
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


assert not (OUT / "runtime-before.json").exists(), "已有基线，禁止覆盖"
pids = command("/usr/sbin/lsof", "-t", "-nP", "-iTCP:8000", "-sTCP:LISTEN").split()
assert len(pids) == 1
pid = int(pids[0])
cwd = command("/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn")
assert f"n{ROOT}" in cwd.splitlines(), "不是本项目进程"
raw = command("/bin/ps", "eww", "-p", str(pid), "-o", "command=")
env = dict(re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([^\s]*)", raw))
keys = ("AGENT_COURSE_ENV", "AGENT_COURSE_DISABLE_LLM", "AGENT_COURSE_OFFLINE_RAG", "AGENT_COURSE_OFFLINE_FACTS",
        "CANCEL_INTENT_EXECUTE", "CANCEL_INTENT_OBSERVE", "CANCEL_SCORE_MIN", "AGENT_OPENAI_MODEL", "AGENT_EMBEDDING_MODEL")
settings = {key: env[key] for key in keys if key in env}
assert settings["AGENT_COURSE_DISABLE_LLM"] == "0"
assert settings["AGENT_COURSE_OFFLINE_FACTS"] == "0"
assert settings["AGENT_COURSE_OFFLINE_RAG"] == "0", "保留现场真实语义向量配置"
assert settings["CANCEL_INTENT_EXECUTE"] == "0"
with httpx.Client(timeout=15, trust_env=False) as client:
    health = client.get("http://127.0.0.1:8000/health"); health.raise_for_status()
    endpoints = {}
    for port, path in ((8081, "/actuator/health"), (5173, "/"), (5174, "/")):
        response = client.get(f"http://127.0.0.1:{port}{path}")
        response.raise_for_status()
        endpoints[str(port)] = response.status_code
assert health.json()["rag_index"]["chunk_count"] == 115
assert health.json()["rag_index"]["embedding"] == "openai_compatible_embedding:Qwen/Qwen3-Embedding-8B"
paths = [p for folder in ("backend", "ecommerce-backend/src", "ecommerce-backend/frontend/src")
         for p in (ROOT / folder).rglob("*") if p.suffix in {".py", ".java", ".md", ".json", ".yml", ".tsx", ".ts", ".css"}]
paths += [ROOT / name for name in ("compose.local.yml", "ecommerce-backend/Dockerfile", "ecommerce-backend/pom.xml", ".env.local", ".env.model.local") if (ROOT / name).exists()]
save("load-source-manifest-before.json", {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)})
runtime = {"at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "pid": pid, "settings": settings,
           "health": health.json(), "http": endpoints,
           "java_image": command("docker", "inspect", "--format", "{{.Image}}", "huangye-outdoor-ecommerce-1"),
           "mysql_started_at": command("docker", "inspect", "--format", "{{.State.StartedAt}}", "huangye-outdoor-mysql-1"),
           "mysql_mounts": json.loads(command("docker", "inspect", "--format", "{{json .Mounts}}", "huangye-outdoor-mysql-1")),
           "credentials_saved": False}
save("runtime-before.json", runtime)
print(json.dumps({"pid": pid, "settings": settings, "healthy": endpoints, "java_image": runtime["java_image"]}, ensure_ascii=False))
