"""启动固定模型、真实 Python 判断和隔离 H2 商城，跑取消验收。"""
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv/bin/python"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
RUNTIME = OUT / ".runtime"
JDK = next(Path("/tmp/jdk17").glob("*/Contents/Home/bin/java"), None)
if JDK is None:
    JDK = next(Path("/tmp/jdk17").glob("*/bin/java"), None)
LIB = ROOT / "analysis/08-候选订单绑定/候选订单验收/.runtime/lib"
PORT_PY = "18014"
PORT_MODEL = "18015"


def wait_http(url, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(url)


def main():
    if JDK is None or not JDK.exists():
        sys.exit("没有可用的 JDK：/tmp/jdk17")
    if not LIB.exists():
        sys.exit("没有编译用的依赖：" + str(LIB))
    RUNTIME.mkdir(exist_ok=True)
    classes = RUNTIME / "classes"
    if classes.exists():
        shutil.rmtree(classes)
    classes.mkdir()
    shutil.copytree(ROOT / "ecommerce-backend/src/main/resources", classes, dirs_exist_ok=True)
    sources = sorted((ROOT / "ecommerce-backend/src/main/java").rglob("*.java"))
    sources.append(OUT / "IsolatedCancelCheck.java")
    (RUNTIME / "sources.txt").write_text("\n".join(str(path) for path in sources) + "\n")
    javac = JDK.parent / "javac"
    compile_log = RUNTIME / "compile.log"
    compiled = subprocess.run([str(javac), "--release", "17", "-encoding", "UTF-8", "-cp", str(LIB / "*"),
                               "-d", str(classes), f"@{RUNTIME / 'sources.txt'}"],
                              stdout=compile_log.open("w"), stderr=subprocess.STDOUT)
    if compiled.returncode != 0:
        print(compile_log.read_text()[-4000:])
        sys.exit(compiled.returncode)
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "backend"),
        "CANCEL_INTENT_EXECUTE": "1",
        "CANCEL_SCORE_MIN": "0.9",
        "TYPESAFE_API_KEY": "isolated-check",
        "CANCEL_JEV_URL": f"http://127.0.0.1:{PORT_MODEL}/v1/systemone",
        "CANCEL_JEV_MODEL": "jev-1.13.0",
        "AGENT_OPENAI_BASE_URL": f"http://127.0.0.1:{PORT_MODEL}/v1",
        "AGENT_OPENAI_API_KEY": "isolated-check",
        "AGENT_OPENAI_MODEL": "isolated-review",
        "AGENT_COURSE_DISABLE_LLM": "1",
        "AGENT_COURSE_OFFLINE_RAG": "1",
        "AGENT_COURSE_OFFLINE_FACTS": "1",
        "AGENT_COURSE_ENV": "/tmp/huangye-no-model.env",
        "ECOMMERCE_BASE_URL": "http://127.0.0.1:18081",
        "AGENT_SERVICE_AUTH_TOKEN": "course-debug-agent-service",
    })
    Path("/tmp/huangye-no-model.env").write_text("")
    (OUT / "python-memory.jsonl").unlink(missing_ok=True)
    model = subprocess.Popen([str(PYTHON), str(OUT / "fake_model.py")], env=env)
    python = subprocess.Popen([str(PYTHON), str(OUT / "serve_agent.py")],
                              cwd=ROOT, env=env, stdout=(RUNTIME / "python.log").open("w"), stderr=subprocess.STDOUT)
    try:
        wait_http(f"http://127.0.0.1:{PORT_PY}/health", timeout=90)
        java_env = env.copy()
        java = subprocess.run([str(JDK), "-cp", f"{classes}:{LIB}/*",
                               "com.teachdemo.ecommerce.customer_service.IsolatedCancelCheck", str(OUT)],
                              env=java_env, stdout=(RUNTIME / "java.log").open("w"), stderr=subprocess.STDOUT)
        if java.returncode != 0:
            print((RUNTIME / "java.log").read_text()[-4000:])
        if (OUT / "result.json").exists():
            print((OUT / "result.json").read_text())
        sys.exit(java.returncode)
    finally:
        for process in (python, model):
            process.send_signal(signal.SIGTERM)
        for process in (python, model):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
