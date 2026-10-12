"""隔离Java/H2和Vite；--before编译本轮改动前的两个源码快照。"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BEFORE = "--before" in sys.argv
RUN = OUT / ("before" if BEFORE else "after")
RUNTIME = RUN / ".runtime"
RUNTIME.mkdir(parents=True, exist_ok=True)
JDK = next(Path("/tmp/jdk17").glob("*/Contents/Home/bin/java"))
LIB = ROOT / "analysis/08-候选订单绑定/候选订单验收/.runtime/lib"
classes = RUNTIME / "classes"
if classes.exists():
    shutil.rmtree(classes)
shutil.copytree(ROOT / "ecommerce-backend/src/main/resources", classes)
shutil.copytree(OUT / "frontend-build", classes / "static", dirs_exist_ok=True)
sources = sorted((ROOT / "ecommerce-backend/src/main/java").rglob("*.java"))
frontend = ROOT / "ecommerce-backend/frontend"
if BEFORE:
    for relative in ["customer_service/CustomerServiceGatewayService.java", "config/SpaForwardController.java"]:
        current = ROOT / "ecommerce-backend/src/main/java/com/teachdemo/ecommerce" / relative
        old = RUNTIME / current.name
        old.write_bytes((OUT / (current.name + ".before")).read_bytes())
        sources = [p for p in sources if p != current] + [old]
    # Vite只替换本轮修改前的App文件，其余代码与依赖沿用当前版本。
check_server = RUNTIME / "CheckServer.java"
check_server.write_text((ROOT / "analysis/03-取消与续办/取消闭环验收/CheckServer.java").read_text().replace("18102", "18302"))
sources += [ROOT / "analysis/03-取消与续办/取消意愿隔离验收/IsolatedCancelCheck.java", check_server, OUT / "HumanSupportCheckServer.java"]
(RUNTIME / "sources.txt").write_text("\n".join(map(str, sources)))
with (RUN / "compile.log").open("w") as log:
    subprocess.run([str(JDK.parent / "javac"), "--release", "17", "-parameters", "-encoding", "UTF-8",
                    "-cp", str(LIB / "*"), "-d", str(classes), "@" + str(RUNTIME / "sources.txt")],
                   stdout=log, stderr=subprocess.STDOUT, check=True)
config = RUNTIME / "vite.config.mjs"
plugin = ""
if BEFORE:
    plugin = "{name:'before-app',enforce:'pre',transform(code,id){if(id.endsWith('/src/shop/App.tsx'))return " + json.dumps((OUT / "App.tsx.before").read_text()) + ";}},"
config.write_text(f"import react from '{(frontend / 'node_modules/@vitejs/plugin-react/dist/index.js').as_uri()}';\n"
                  f"export default {{root:{json.dumps(str(frontend))},plugins:[{plugin}react()],server:{{host:'127.0.0.1',port:18303,strictPort:true,proxy:{{'/api':'http://127.0.0.1:18302'}}}}}};\n")
env = os.environ.copy()
env.update({"CANCEL_CHECK_MODE": "scripted", "AGENT_COURSE_DISABLE_LLM": "1", "HUMAN_CHECK_BEFORE": str(int(BEFORE))})
commands = [[str(JDK), "-cp", f"{classes}:{LIB}/*", "com.teachdemo.ecommerce.customer_service.HumanSupportCheckServer"],
            ["node", str(frontend / "node_modules/vite/bin/vite.js"), "--config", str(config)]]
wrapper = [str(ROOT / ".venv/bin/python"), "/Users/sunhao/.codex/skills/webapp-testing/scripts/with_server.py"]
for name, port, command in zip(("java", "vite"), (18302, 18303), commands):
    wrapper += ["--server", shlex.join(command) + " > " + shlex.quote(str(RUN / f"{name}.log")) + " 2>&1", "--port", str(port)]
wrapper += ["--timeout", "60", "--", str(ROOT / ".venv/bin/python"), str(OUT / "check.py")]
subprocess.run(wrapper, cwd=ROOT, env=env, check=True)
