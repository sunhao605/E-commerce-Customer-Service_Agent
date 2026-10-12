"""复用现有镜像依赖；只编译当前源码并在独立 H2 运行对话验收。"""
import subprocess
import tarfile
import zipfile
from pathlib import Path

out=Path(__file__).resolve().parent
root=out.parents[2]
runtime=out/'.runtime'
runtime.mkdir(exist_ok=True)
jar=runtime/'runtime.jar'
if not jar.exists():
    container=subprocess.check_output(['docker','create','--network','none','huangye-outdoor-ecommerce:latest'],text=True).strip()
    try:
        subprocess.run(['docker','cp',f'{container}:/app/app.jar',str(jar)],check=True)
    finally:
        subprocess.run(['docker','rm',container],check=True,stdout=subprocess.DEVNULL)
lib=runtime/'lib'
lib.mkdir(exist_ok=True)
with zipfile.ZipFile(jar) as z:
    for name in z.namelist():
        if name.startswith('BOOT-INF/lib/') and name.endswith('.jar'):
            (lib/Path(name).name).write_bytes(z.read(name))
with tarfile.open(root/'ecommerce-backend/docker/m2-cache.tar.gz') as archive:
    name=next(m.name for m in archive.getmembers() if m.name.endswith('/h2-2.2.224.jar'))
    (lib/'h2-2.2.224.jar').write_bytes(archive.extractfile(name).read())
classes=runtime/'classes'
classes.mkdir(exist_ok=True)
import shutil
shutil.copytree(root/'ecommerce-backend/src/main/resources',classes,dirs_exist_ok=True)
sources=sorted((root/'ecommerce-backend/src/main/java').rglob('*.java'))+[out/'CandidateDialogueCheck.java']
(runtime/'sources.txt').write_text('\n'.join('/workspace/'+str(p.relative_to(root)) for p in sources)+'\n')
command=['docker','run','--rm','--name','huangye-m06-check','-p','127.0.0.1:18192:8081',
         '-v',f'{root}:/workspace:ro','-v',f'{runtime}:/check','-w','/check','maven:3.9.6-eclipse-temurin-17','bash','-c',
         'javac --release 17 -parameters -encoding UTF-8 -cp "/check/lib/*" -d /check/classes @/check/sources.txt && java -cp "/check/classes:/check/lib/*" com.teachdemo.ecommerce.customer_service.CandidateDialogueCheck']
with (out/'mall-check.log').open('w') as log:
    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
for name in ['dialogue.json','result.json']:
    if (runtime/name).exists():
        shutil.copyfile(runtime/name,out/('mall-'+name))
print((out/'mall-check.log').read_text()[-1800:])
assert result.returncode == 0, 'see mall-check.log'
assert (out/'mall-result.json').exists()
