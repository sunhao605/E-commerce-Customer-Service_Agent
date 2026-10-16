from pathlib import Path
import json,urllib.request,uuid
out=Path('/private/tmp/rag-policy-id-rename-20261013')
session='rename-policy-'+uuid.uuid4().hex
payload={'session_id':session,'runtime_user_id':'U1001','user_message':'露营季活动满减和山友会员券能不能叠加？'}
req=urllib.request.Request('http://127.0.0.1:8000/chat',data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
with urllib.request.urlopen(req,timeout=90) as r:body=json.load(r)
(out/'live-response.json').write_text(json.dumps(body,ensure_ascii=False,indent=2)+'\n')
ids=[c.get('metadata',{}).get('policy_id') for c in body['citations']]
assert 'promotion_camping_season_stack_rule' in ids
assert 'promotion_618_stack_rule' not in ids
assert body['session_state']['rag']['index_version']=='lesson41-idx-5268ea0829cc'
with urllib.request.urlopen('http://127.0.0.1:8000/sessions/'+session+'/trace',timeout=5) as r:events=json.load(r)
(out/'live-trace.json').write_text(json.dumps(events,ensure_ascii=False,indent=2)+'\n')
summary={'citation_ids':ids,'index_version':body['session_state']['rag']['index_version'],'chunk_count':body['session_state']['rag']['index_chunk_count'],'retrieval_cache_hit':body['session_state']['rag']['retrieval_cache_hit'],'new_id_verified':True,'tool_call_count':len(body['tool_calls']),'answer_preview':body['answer'][:160]}
(out/'live-verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(summary,ensure_ascii=False,indent=2))
