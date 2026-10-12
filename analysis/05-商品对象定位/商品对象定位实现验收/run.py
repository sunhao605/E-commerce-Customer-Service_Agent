"""编译当前Java，运行本机隔离H2、商品对象定位和页面；不访问外部模型。"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
RUNTIME = OUT / ".runtime"
RUNTIME.mkdir(exist_ok=True)
JDK = next(Path("/tmp/jdk17").glob("*/Contents/Home/bin/java"))
LIB = ROOT / "analysis/08-候选订单绑定/候选订单验收/.runtime/lib"
classes = RUNTIME / "classes"
if classes.exists():
    shutil.rmtree(classes)
shutil.copytree(ROOT / "ecommerce-backend/src/main/resources", classes)
sources = sorted((ROOT / "ecommerce-backend/src/main/java").rglob("*.java")) + [
    ROOT / "analysis/03-取消与续办/取消意愿隔离验收/IsolatedCancelCheck.java",
    ROOT / "analysis/03-取消与续办/取消闭环验收/CheckServer.java"]
(RUNTIME / "sources.txt").write_text("\n".join(map(str, sources)))
with (RUNTIME / "compile.log").open("w") as log:
    subprocess.run([str(JDK.parent / "javac"), "--release", "17", "-parameters", "-encoding", "UTF-8",
        "-cp", str(LIB / "*"), "-d", str(classes), "@" + str(RUNTIME / "sources.txt")],
        stdout=log, stderr=subprocess.STDOUT, check=True)
env = os.environ.copy()
empty = RUNTIME / "empty.env"
empty.write_text("")
env.update({"PYTHONPATH": str(ROOT / "backend"), "AGENT_COURSE_DISABLE_LLM": "1", "AGENT_COURSE_OFFLINE_RAG": "1",
    "AGENT_COURSE_OFFLINE_FACTS": "0", "AGENT_COURSE_ENV": str(empty), "CANCEL_CHECK_MODE": "scripted",
    "CANCEL_INTENT_EXECUTE": "0", "ECOMMERCE_BASE_URL": "http://127.0.0.1:18102",
    "AGENT_SERVICE_AUTH_TOKEN": "course-debug-agent-service"})
frontend = ROOT / "ecommerce-backend/frontend"
config = RUNTIME / "vite.config.mjs"
config.write_text(f"import react from '{(frontend / 'node_modules/@vitejs/plugin-react/dist/index.js').as_uri()}';\n"
    f"export default {{root:{json.dumps(str(frontend))},plugins:[react()],server:{{host:'127.0.0.1',port:18103,strictPort:true,proxy:{{'/api':'http://127.0.0.1:18102'}}}}}};\n")
commands = [
    [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "api.routes:app", "--host", "127.0.0.1", "--port", "18101", "--log-level", "warning"],
    [str(JDK), "-cp", f"{classes}:{LIB}/*", "com.teachdemo.ecommerce.customer_service.CheckServer"],
    ["/Users/sunhao/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node", str(frontend / "node_modules/vite/bin/vite.js"), "--config", str(config)],
]
wrapper = [str(ROOT / ".venv/bin/python"), "/Users/sunhao/.agents/skills/webapp-testing/scripts/with_server.py"]
for name, port, command in zip(("python", "java", "vite"), (18101, 18102, 18103), commands):
    wrapper += ["--server", shlex.join(command) + " > " + shlex.quote(str(RUNTIME / f"{name}.log")) + " 2>&1", "--port", str(port)]
wrapper += ["--timeout", "60", "--", str(ROOT / ".venv/bin/python"), str(OUT / "check.py")]
subprocess.run(wrapper, cwd=ROOT, env=env, check=True)
