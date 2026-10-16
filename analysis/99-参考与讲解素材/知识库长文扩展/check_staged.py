import os,sys,socket,json,hashlib,shutil,unittest
from pathlib import Path
ROOT=Path('/Users/sunhao/Downloads/03_AIPMProject/0305_E-commerce-Customer-Service_Agent')
BASE=Path('/private/tmp/outdoor-kb-expansion-20261012'); STAGED=BASE/'staged'
old_dir=ROOT/'backend/resources/knowledge'
os.environ['AGENT_COURSE_OFFLINE_RAG']='1';os.environ['AGENT_COURSE_DISABLE_LLM']='1';os.environ['AGENT_COURSE_ENV']=str(BASE/'nonexistent.env')
sys.path.insert(0,str(ROOT/'backend'))
sys.path.insert(1,str(ROOT))
def block(*a,**k):raise AssertionError('External network is disabled for this check')
socket.socket.connect=block
import yaml
manifest={}
for name in ['after_sale_policy.md','member_coupon_policy.md','promotion_policy.md','received_return_policy.md','payment_invoice_policy.md']:
 p=old_dir/name; data=p.read_bytes();manifest[name]={'sha256':hashlib.sha256(data).hexdigest(),'chars':len(data.decode()),'body_chars':len(data.decode().split('---',2)[-1].strip())}
(BASE/'before-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
shutil.copy2(old_dir/'payment_invoice_policy.md',STAGED/'payment_invoice_policy.md')
check={}
for p in STAGED.glob('*.md'):
 raw=p.read_text();front,body=raw[4:].split('\n---\n',1);meta=yaml.safe_load(front)
 assert set(['policy_id','scene_key','title','score','retrieval_stage']).issubset(meta)
 if p.name!='payment_invoice_policy.md':
  assert meta['content_status']=='demo_knowledge' and len(body)>2500
  assert '蓝牙' not in raw and '耳机' not in raw
 if p.name in manifest:
  previous=yaml.safe_load((old_dir/p.name).read_text()[4:].split('\n---\n',1)[0])
  for key in ['policy_id','scene_key','title','score','retrieval_stage']:assert meta[key]==previous[key],(p.name,key)
 check[p.name]={'chars':len(raw),'body_chars':len(body.strip()),'policy_id':meta['policy_id']}
from retrieval import documents as d
from retrieval import hybrid_retrieval as h
d.KNOWLEDGE_DIR=STAGED;h.KNOWLEDGE_DIR=STAGED
d.load_knowledge_citation.cache_clear();h.reset_hybrid_index_and_cache()
for name in check:d.load_knowledge_citation(name)
index,_=h.get_knowledge_index()
from collections import Counter
chunk_counts=Counter(x.metadata['filename'] for x in index.documents)
check['index']={'chunks':len(index.documents),'per_file':dict(chunk_counts),'embedding':index.embedding_mode,'product_guide_indexed':False,'return_policy_indexed':False}
from retrieval.knowledge import promotion_policy_result,policy_query_result
r=promotion_policy_result('kb-staged-check','露营季满减和山友会员券能否叠加？')
assert {x.metadata['policy_id'] for x in r.citations}=={'promotion_618_stack_rule','member_coupon_gold_rule'}
assert '七天无理由' in policy_query_result('kb-staged-return','return_policy_query').answer
modules=['backend.tests.test_outdoor_scenario','backend.tests.test_route_priority','backend.tests.test_eligibility_query']
suite=unittest.defaultTestLoader.loadTestsFromNames(modules)
result=unittest.TextTestRunner(verbosity=1).run(suite)
check['existing_checks']={'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}
assert result.wasSuccessful()
(BASE/'staged-check.json').write_text(json.dumps(check,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(check,ensure_ascii=False,indent=2))
