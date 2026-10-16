"""仅重启当前工作区的8000 Agent；运行环境只在内存中继承。"""
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx

ROOT=Path(__file__).resolve().parents[4]
OUT=Path(__file__).resolve().parent
KEYS=('AGENT_COURSE_DISABLE_LLM','AGENT_COURSE_OFFLINE_RAG','AGENT_COURSE_OFFLINE_FACTS',
      'ANSWER_JUDGE_MODE','ANSWER_JUDGE_TIMEOUT_SECONDS','CANCEL_INTENT_EXECUTE','CANCEL_SCORE_MIN')
def listeners():
    r=subprocess.run(['/usr/sbin/lsof','-t','-nP','-iTCP:8000','-sTCP:LISTEN'],capture_output=True,text=True)
    return sorted(set(map(int,r.stdout.split())))
def save(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
def sources():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ('backend/agent','backend/retrieval','backend/resources')
            for p in (ROOT/folder).rglob('*') if p.suffix in ('.py','.json','.md','.yml')}

pids=listeners();assert len(pids)==1,pids
pid=pids[0]
cwd=subprocess.check_output(['/usr/sbin/lsof','-a','-p',str(pid),'-d','cwd','-Fn'],text=True)
assert 'n'+str(ROOT) in cwd.splitlines()
raw=subprocess.check_output(['/bin/ps','eww','-p',str(pid),'-o','command='],text=True)
assert 'uvicorn api.routes:app' in raw
old_env=dict(re.findall(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([^\s]*)',raw))
settings={k:old_env.get(k) for k in KEYS}
assert settings==dict(zip(KEYS,('0','0','0','enforce','5','0','0.9'))),settings
env=dict(os.environ)
env.update({k:v for k,v in old_env.items() if k.startswith(('AGENT_','CANCEL_','ANSWER_','LANGCHAIN_','LANGSMITH_','ECOMMERCE_'))})
env['PYTHONPATH']=str(ROOT/'backend')
manifest=sources()
with httpx.Client(timeout=10,trust_env=False) as client:
    before={}
    for key,url in [('agent','http://127.0.0.1:8000/health'),('java','http://127.0.0.1:8081/actuator/health')]:
        r=client.get(url);r.raise_for_status();before[key]=r.json()
save('before.json',{'at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'pid':pid,'settings':settings,'health':before,'source_sha256':manifest})
assert listeners()==[pid]
os.kill(pid,signal.SIGTERM)
for _ in range(50):
    if not listeners():break
    time.sleep(.1)
assert not listeners(),'旧进程未退出'
with (OUT/'python-daily.log').open('ab') as log:
    child=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','api.routes:app','--host','127.0.0.1','--port','8000'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
save('launched.json',{'old_pid':pid,'new_pid':child.pid,'settings':settings})
print(json.dumps({'old_pid':pid,'new_pid':child.pid,'state':'starting'}),flush=True)
with httpx.Client(timeout=2,trust_env=False) as client:
    for _ in range(180):
        if child.poll() is not None:raise RuntimeError('新进程退出，请查启动日志')
        try:
            r=client.get('http://127.0.0.1:8000/health');r.raise_for_status();health=r.json();break
        except (httpx.HTTPError,ValueError):time.sleep(.25)
    else:raise RuntimeError('新进程未就绪')
assert listeners()==[child.pid]
raw=subprocess.check_output(['/bin/ps','eww','-p',str(child.pid),'-o','command='],text=True)
newenv=dict(re.findall(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([^\s]*)',raw))
assert {k:newenv.get(k) for k in KEYS}==settings
assert sources()==manifest
save('after.json',{'at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'old_pid':pid,'new_pid':child.pid,'settings':settings,'health':health,'source_unchanged':True})
print(json.dumps({'state':'ready','pid':child.pid,'health':health},ensure_ascii=False),flush=True)
