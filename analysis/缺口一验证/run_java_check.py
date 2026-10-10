"""使用已有镜像依赖编译全部 Java 源码，运行隔离 H2/HTTP 退款检查。无模型、无外部业务网络。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

out = Path(__file__).resolve().parent
root = out.parents[1]
check_source = out / 'CustomerRefundCheck.java'
returns = '--return' in sys.argv
approval = '--approval' in sys.argv or returns
if approval:
    out = root / ('analysis/七天退货验证' if returns else 'analysis/退款状态验证')
    out.mkdir(exist_ok=True)
runtime = out / '.runtime'
runtime.mkdir(exist_ok=True)
jar = runtime / 'runtime.jar'
if not jar.exists():
    container = subprocess.check_output(['docker', 'create', '--network', 'none', 'huangye-outdoor-ecommerce:latest'], text=True).strip()
    try:
        subprocess.run(['docker', 'cp', f'{container}:/app/app.jar', str(jar)], check=True)
    finally:
        subprocess.run(['docker', 'rm', container], check=True, stdout=subprocess.DEVNULL)
lib = runtime / 'lib'
lib.mkdir(exist_ok=True)
with zipfile.ZipFile(jar) as archive:
    for name in archive.namelist():
        if name.startswith('BOOT-INF/lib/') and name.endswith('.jar'):
            (lib / Path(name).name).write_bytes(archive.read(name))
with tarfile.open(root / 'ecommerce-backend/docker/m2-cache.tar.gz') as archive:
    name = next(member.name for member in archive.getmembers() if member.name.endswith('/h2-2.2.224.jar'))
    (lib / 'h2-2.2.224.jar').write_bytes(archive.extractfile(name).read())
classes = runtime / 'classes'
if classes.exists():
    shutil.rmtree(classes)
shutil.copytree(root / 'ecommerce-backend/src/main/resources', classes)
sources = sorted((root / 'ecommerce-backend/src/main/java').rglob('*.java'))
sources.append(check_source)
(runtime / 'sources.txt').write_text('\n'.join('/workspace/' + str(path.relative_to(root)) for path in sources) + '\n')
command = ['docker', 'run', '--rm', '--network', 'none', '-v', f'{root}:/workspace:ro', '-v', f'{runtime}:/check',
           '-w', '/check', 'maven:3.9.6-eclipse-temurin-17', 'bash', '-c',
           'javac --release 17 -parameters -encoding UTF-8 -cp "/check/lib/*" -d /check/classes @/check/sources.txt && '
           'java -cp "/check/classes:/check/lib/*" com.teachdemo.ecommerce.customer_service.CustomerRefundCheck' + (' --return' if returns else ' --approval' if approval else '')]
with (out / 'java-check.log').open('w') as log:
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
text = (out / 'java-check.log').read_text()
if result.returncode:
    print(text[-14000:])
    raise SystemExit(result.returncode)
prefix = 'SEVEN_DAY_RETURN_RESULT ' if returns else 'REFUND_COMPLETION_RESULT ' if approval else 'GAP_ONE_RESULT '
payload = next(line.removeprefix(prefix) for line in text.splitlines() if line.startswith(prefix))
result = json.loads(payload)
result.update(java_sources_compiled=len(sources)-1, compiler='javac --release 17; dependencies from existing application image; not Maven package')
(out / 'java-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
