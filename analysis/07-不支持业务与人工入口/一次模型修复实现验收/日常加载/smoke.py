"""重启后的单条只读咨询，不经过商城历史写入入口。"""
import json
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
import httpx
OUT=Path(__file__).resolve().parent
sid='repair-daily-'+uuid4().hex
request={'session_id':sid,'runtime_user_id':'U1001','user_message':'轻量露营折叠椅多少钱，还有库存吗，能登记到货提醒吗？'}
started=time.monotonic()
with httpx.Client(timeout=90,trust_env=False) as client:
    r=client.post('http://127.0.0.1:8000/chat',json=request);r.raise_for_status();response=r.json()
    trace=client.get('http://127.0.0.1:8000/sessions/'+sid+'/trace');trace.raise_for_status()
    health=client.get('http://127.0.0.1:8000/health');health.raise_for_status()
row={'at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'request':request,'response':response,'trace':trace.json(),
     'elapsed_ms':round((time.monotonic()-started)*1000),'health':health.json()}
(OUT/'smoke.json').write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n')
judge=response['session_state']['model']['answer_judge']
assert response['session_state']['workflow'] is None
assert judge['mode']=='enforce' and 'repair_attempted' in judge
assert '199' in response['answer'] and '库存' in response['answer']
assert health.json()['rag_index']['snapshot_version']=='knowledge-d8e495a59835'
print(json.dumps({'answer':response['answer'],'judge':judge,'elapsed_ms':row['elapsed_ms']},ensure_ascii=False))
