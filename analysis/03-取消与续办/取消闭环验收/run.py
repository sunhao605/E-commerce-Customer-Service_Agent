"""运行隔离闭环验收：python run.py real|scripted。进程结束时清理全部临时服务。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
RUNTIME = OUT / ".runtime"
PYTHON = ROOT / ".venv/bin/python"
LIB = ROOT / "analysis/08-候选订单绑定/候选订单验收/.runtime/lib"
JDK = next(Path("/tmp/jdk17").glob("*/Contents/Home/bin/java"))


def wait_http(url, processes):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if any(p.poll() is not None for p in processes):
            raise RuntimeError("隔离服务提前退出，见 .runtime 日志")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(url)


def main():
    mode = sys.argv[1]
    assert mode in {"real", "scripted"}
    RUNTIME.mkdir(exist_ok=True)
    sources = sorted((ROOT / "ecommerce-backend/src/main/java").rglob("*.java"))
    sources += [ROOT / "analysis/03-取消与续办/取消意愿隔离验收/IsolatedCancelCheck.java", OUT / "CheckServer.java"]
    fingerprint = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    for p in [ROOT / "backend/agent/cancellation.py", ROOT / "backend/agent/customer_service_agent.py",
              ROOT / "ecommerce-backend/frontend/src/shop/App.tsx", OUT / "cases.json"]:
        fingerprint[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
    (OUT / f"source-{mode}.json").write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2))
    classes = RUNTIME / "classes"
    if classes.exists():
        shutil.rmtree(classes)
    shutil.copytree(ROOT / "ecommerce-backend/src/main/resources", classes)
    source_list = RUNTIME / "sources.txt"
    source_list.write_text("\n".join(map(str, sources)))
    with (RUNTIME / "compile.log").open("w") as log:
        subprocess.run([str(JDK.parent / "javac"), "--release", "17", "-parameters", "-encoding", "UTF-8",
                        "-cp", str(LIB / "*"), "-d", str(classes), "@" + str(source_list)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    env = os.environ.copy()
    if mode == "real":
        for line in (ROOT / ".env.model.local").read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("\"'")
    empty_env = RUNTIME / "empty.env"
    empty_env.write_text("")
    env.update({"PYTHONPATH":str(ROOT / "backend"), "CANCEL_INTENT_EXECUTE":"1", "CANCEL_SCORE_MIN":"0.9",
                "CANCEL_JEV_MODEL":"jev-1.13.0", "CANCEL_CHECK_MODE":mode, "CANCEL_CHECK_OUT":str(OUT),
                "AGENT_COURSE_DISABLE_LLM":"1", "AGENT_COURSE_OFFLINE_RAG":"1", "AGENT_COURSE_OFFLINE_FACTS":"1",
                "AGENT_COURSE_ENV":str(empty_env), "ECOMMERCE_BASE_URL":"http://127.0.0.1:18102",
                "AGENT_SERVICE_AUTH_TOKEN":"course-debug-agent-service"})
    env.pop("CANCEL_JEV_URL", None)
    if mode == "scripted":
        env["TYPESAFE_API_KEY"] = "isolated-check"
    (OUT / f"models-{mode}.jsonl").unlink(missing_ok=True)
    frontend = ROOT / "ecommerce-backend/frontend"
    vite_config = RUNTIME / "vite.config.mjs"
    vite_config.write_text(
        f"import react from '{(frontend / 'node_modules/@vitejs/plugin-react/dist/index.js').as_uri()}';\n"
        f"export default {{root:{json.dumps(str(frontend))}, plugins:[react()], server:{{host:'127.0.0.1', port:18103, strictPort:true, proxy:{{'/api':'http://127.0.0.1:18102'}}}}}};\n")
    commands = [
        [str(PYTHON), str(OUT / "serve.py")],
        [str(JDK), "-cp", f"{classes}:{LIB}/*", "com.teachdemo.ecommerce.customer_service.CheckServer"],
        ["node", str(frontend / "node_modules/vite/bin/vite.js"), "--config", str(vite_config)],
    ]
    processes = []
    logs = []
    try:
        for name, command in zip(("python", "java", "vite"), commands):
            log = (RUNTIME / f"{name}-{mode}.log").open("w")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
        wait_http("http://127.0.0.1:18101/health", processes)
        wait_http("http://127.0.0.1:18102/actuator/health", processes)
        wait_http("http://127.0.0.1:18103", processes)
        # 等合成订单准备完毕，健康检查本身不代表 fixture 已完成。
        deadline = time.monotonic() + 30
        while "CANCEL_CHECK_READY" not in (RUNTIME / f"java-{mode}.log").read_text():
            assert time.monotonic() < deadline
            time.sleep(0.2)
        completed = subprocess.run([str(PYTHON), str(OUT / "check.py"), mode], cwd=ROOT, env=env)
        sys.exit(completed.returncode)
    finally:
        for p in reversed(processes):
            p.terminate()
        for p in reversed(processes):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
