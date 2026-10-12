"""复用M06已有依赖编译全部Java源码，运行独立H2；保存每次基线/复测。"""
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

out = Path(__file__).resolve().parent
root = out.parents[2]
phase = sys.argv[1]
assert phase in {'before', 'after', 'regression'}
offline = '--offline' in sys.argv or phase == 'regression'
runtime = out / '.runtime'
runtime.mkdir(exist_ok=True)
classes = runtime / 'classes'
if classes.exists():
    shutil.rmtree(classes)
shutil.copytree(root / 'ecommerce-backend/src/main/resources', classes)
sources = sorted((root / 'ecommerce-backend/src/main/java').rglob('*.java'))
if phase == 'regression':
    sources.append(root / 'analysis/01-售后申请与审核/提交与审核分离验收/CustomerRefundCheck.java')
else:
    sources += [root / 'analysis/08-候选订单绑定/候选订单验收/CandidateDialogueCheck.java', out / 'CancellationDialogueCheck.java']
(runtime / 'sources.txt').write_text('\n'.join('/workspace/' + str(p.relative_to(root)) for p in sources) + '\n')
saved = out / (phase + ('-offline' if offline else '-live'))
saved.mkdir(exist_ok=True)
for name in ['dialogue.json', 'result.json']:
    (saved / name).unlink(missing_ok=True)
command = ['docker', 'run', '--rm', '--name', 'huangye-m07-check', '-p', '127.0.0.1:18193:8081',
           '-v', f'{root}:/workspace:ro', '-v', f'{runtime}:/check', '-w', '/check',
           'maven:3.9.6-eclipse-temurin-17', 'bash', '-c',
           'javac --release 17 -parameters -encoding UTF-8 -cp "/workspace/analysis/08-候选订单绑定/候选订单验收/.runtime/lib/*" '
           '-d /check/classes @/check/sources.txt && java -cp "/check/classes:/workspace/analysis/08-候选订单绑定/候选订单验收/.runtime/lib/*" '
           'com.teachdemo.ecommerce.customer_service.' + ('CustomerRefundCheck --return' if phase == 'regression'
               else 'CancellationDialogueCheck' + (' --offline' if offline else ''))]
for name in ['dialogue.json', 'result.json']:
    (runtime / name).unlink(missing_ok=True)
with (saved / 'check.log').open('w') as log:
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
for name in ['dialogue.json', 'result.json']:
    if (runtime / name).exists():
        shutil.copyfile(runtime / name, saved / name)
if phase == 'regression' and result.returncode == 0:
    log = (saved / 'check.log').read_text()
    payload = next(line.removeprefix('SEVEN_DAY_RETURN_RESULT ') for line in log.splitlines()
                   if line.startswith('SEVEN_DAY_RETURN_RESULT '))
    (saved / 'result.json').write_text(json.dumps(json.loads(payload), ensure_ascii=False, indent=2) + '\n')
if not offline and (saved / 'dialogue.json').exists():
    sessions = {turn['request']['sessionId'] for turn in json.loads((saved / 'dialogue.json').read_text())}
    for session in sorted(sessions):
        with urllib.request.urlopen('http://127.0.0.1:8013/sessions/' + session + '/trace', timeout=10) as response:
            trace = json.load(response)
        (saved / (session + '-trace.json')).write_text(json.dumps(trace, ensure_ascii=False, indent=2) + '\n')
if (saved / 'result.json').exists():
    print((saved / 'result.json').read_text())
else:
    print((saved / 'check.log').read_text()[-4000:])
sys.exit(result.returncode)
