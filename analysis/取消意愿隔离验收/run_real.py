"""真实 Jev 与 DeepSeek 的隔离重跑。CANCEL_SCORE_MIN 固定为 0.9，跑完不改。"""
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv/bin/python"
RUNTIME = OUT / ".runtime"
JDK = next(Path("/tmp/jdk17").glob("*/Contents/Home/bin/java"))
LIB = ROOT / "analysis/候选订单验收/.runtime/lib"
MODES = {
    "five": ("RealModelCancelCheck", "18016", "18082", "real-result.json"),
    "script": ("RealScriptCheck", "18018", "18083", "script-real-result.json"),
    "reply": ("RealReplyCheck", "18020", "18085", "reply-result.json"),
}
MODE = sys.argv[1] if len(sys.argv) > 1 else "five"


def load_model_env(env):
    for line in (ROOT / ".env.model.local").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("\"'")


def wait_http(url, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.4)
    raise TimeoutError(url)


def main():
    if MODE not in MODES:
        sys.exit("用法: run_real.py [five|script|reply]")
    java_class, py_port, java_port, result_name = MODES[MODE]
    classes = RUNTIME / f"classes-{MODE}"
    if classes.exists():
        shutil.rmtree(classes)
    classes.mkdir(parents=True)
    shutil.copytree(ROOT / "ecommerce-backend/src/main/resources", classes, dirs_exist_ok=True)
    sources = sorted((ROOT / "ecommerce-backend/src/main/java").rglob("*.java"))
    sources.append(OUT / f"{java_class}.java")
    source_list = RUNTIME / f"sources-{MODE}.txt"
    source_list.write_text("\n".join(str(path) for path in sources) + "\n")
    compiled = subprocess.run([str(JDK.parent / "javac"), "--release", "17", "-encoding", "UTF-8",
                               "-cp", str(LIB / "*"), "-d", str(classes), f"@{source_list}"],
                              stdout=(RUNTIME / f"compile-{MODE}.log").open("w"), stderr=subprocess.STDOUT)
    if compiled.returncode != 0:
        print((RUNTIME / f"compile-{MODE}.log").read_text()[-3000:])
        sys.exit(compiled.returncode)
    env = os.environ.copy()
    load_model_env(env)
    env.update({
        "PYTHONPATH": str(ROOT / "backend"),
        "CANCEL_INTENT_EXECUTE": "1",
        "CANCEL_SCORE_MIN": "0.9",
        "CANCEL_JEV_MODEL": "jev-1.13.0",
        "AGENT_COURSE_DISABLE_LLM": "1",
        "AGENT_COURSE_OFFLINE_RAG": "1",
        "AGENT_COURSE_OFFLINE_FACTS": "1",
        "AGENT_COURSE_ENV": "/tmp/huangye-no-model.env",
        "ECOMMERCE_BASE_URL": f"http://127.0.0.1:{java_port}",
        "AGENT_SERVICE_AUTH_TOKEN": "course-debug-agent-service",
        "CANCEL_PY_PORT": py_port,
        "CANCEL_MEMORY_LOG": str(OUT / f"python-memory-{MODE}.jsonl"),
    })
    env.pop("CANCEL_JEV_URL", None)
    Path("/tmp/huangye-no-model.env").write_text("")
    memory_log = Path(env["CANCEL_MEMORY_LOG"])
    memory_log.unlink(missing_ok=True)
    python = subprocess.Popen([str(PYTHON), str(OUT / "serve_agent.py")], cwd=ROOT, env=env,
                              stdout=(RUNTIME / f"python-{MODE}.log").open("w"), stderr=subprocess.STDOUT)
    try:
        wait_http("http://127.0.0.1:" + env["CANCEL_PY_PORT"] + "/health")
        java = subprocess.run([str(JDK), "-cp", f"{classes}:{LIB}/*",
                               "com.teachdemo.ecommerce.customer_service." + java_class, str(OUT)],
                              env=env, stdout=(RUNTIME / f"java-{MODE}.log").open("w"), stderr=subprocess.STDOUT)
        if java.returncode != 0:
            print((RUNTIME / f"java-{MODE}.log").read_text()[-5000:])
        if (OUT / result_name).exists():
            print((OUT / result_name).read_text())
        sys.exit(java.returncode)
    finally:
        python.send_signal(signal.SIGTERM)
        try:
            python.wait(timeout=5)
        except subprocess.TimeoutExpired:
            python.kill()


if __name__ == "__main__":
    main()
