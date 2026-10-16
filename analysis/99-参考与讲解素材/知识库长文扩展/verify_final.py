import os,sys,socket,hashlib,json,re,unittest,io
from pathlib import Path
ROOT=Path('/Users/sunhao/Downloads/03_AIPMProject/0305_E-commerce-Customer-Service_Agent');BASE=Path('/private/tmp/outdoor-kb-expansion-20261012');KB=ROOT/'backend/resources/knowledge';EVIDENCE=ROOT/'analysis/99-参考与讲解素材/知识库长文扩展'
os.environ['AGENT_COURSE_OFFLINE_RAG']='1';os.environ['AGENT_COURSE_DISABLE_LLM']='1';os.environ['AGENT_COURSE_ENV']=str(BASE/'nonexistent.env')
sys.path.insert(0,str(ROOT/'backend'));sys.path.insert(1,str(ROOT))
def block(*a,**k):raise AssertionError('External network is disabled')
socket.socket.connect=block
import yaml
from retrieval.documents import load_knowledge_citation
from retrieval.hybrid_retrieval import get_knowledge_index
from retrieval.knowledge import promotion_policy_result,policy_query_result
from agent.source_guard import inspect_source
files={};old=json.loads((BASE/'before-manifest.json').read_text())
for name in ['product_knowledge.md','member_coupon_policy.md','promotion_policy.md','after_sale_policy.md','received_return_policy.md']:
 p=KB/name;raw=p.read_text();front,body=raw[4:].split('\n---\n',1);meta=yaml.safe_load(front)
 assert raw==(BASE/'staged'/name).read_text(),name
 citation=load_knowledge_citation(name)
 assert citation.snippet==body.strip()
 assert meta['content_status']=='demo_knowledge'
 assert not inspect_source('rag_document',body)['tainted'],name
 files[name]={'before_body_chars':old.get(name,{}).get('body_chars',0),'after_body_chars':len(body.strip()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'metadata_preserved':name in old,'source_guard_tainted':False}
assert hashlib.sha256((KB/'payment_invoice_policy.md').read_bytes()).hexdigest()==old['payment_invoice_policy.md']['sha256']
idx,_=get_knowledge_index()
assert len(idx.documents)==56
from collections import Counter
for c in idx.documents:assert len(c.page_content)<=220
r=promotion_policy_result('kb-final-promotion','露营季满减和山友会员券能否叠加？')
assert {c.metadata['policy_id'] for c in r.citations}=={'promotion_618_stack_rule','member_coupon_gold_rule'}
for intent in ['refund_policy_query','return_policy_query']:
 r=policy_query_result('kb-final-policy',intent)
 assert len(r.citations)==1 and r.next_action=='answer_user'
linked=[KB/name for name in [*files,'README.md']]+[ROOT/'docs/04-学习与复盘/知识库扩展说明.md',ROOT/'docs/04-学习与复盘/README.md',EVIDENCE/'README.md']
links=0
for p in linked:
 for url in re.findall(r'\]\(([^)]+)\)',p.read_text()):
  if url.startswith(('http://','https://')):continue
  target=url.split('#')[0]
  if not target:continue
  if (p.parent/target).resolve()==(EVIDENCE/'result.json').resolve():continue
  assert (p.parent/target).resolve().exists(),(p.name,url)
  links+=1
frozen=json.loads((BASE/'business-source-before.json').read_text())
assert all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h for name,h in frozen.items())
modules=['backend.tests.test_outdoor_scenario','backend.tests.test_route_priority','backend.tests.test_eligibility_query']
log=io.StringIO();suite=unittest.defaultTestLoader.loadTestsFromNames(modules);result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
(EVIDENCE/'final-unittest.log').write_text(log.getvalue())
assert result.wasSuccessful(),log.getvalue()
record={'date':'2026-10-12','files':files,'total_body_chars':sum(v['after_body_chars'] for v in files.values()),'index_chunk_count':len(idx.documents),'index_chunks_by_file':dict(Counter(c.metadata['filename'] for c in idx.documents)),'checks':{'existing_test_count':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'relative_links_verified':links,'business_source_hashes_match':len(frozen),'invoice_unchanged':True,'citations_load':True,'source_guard_untainted':True,'max_chunk_chars':220},'boundary':{'offline_hash_embedding':True,'external_model_calls':0,'business_service_calls':0,'source_code_changes':0,'service_reload':False,'product_guide_indexed':False,'return_policy_indexed':False,'real_dialogue_acceptance':False}}
(EVIDENCE/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
assert (EVIDENCE/'result.json').exists()
(EVIDENCE/'verify_final.py').write_text(Path(__file__).read_text())
print(json.dumps({'files':{k:[v['before_body_chars'],v['after_body_chars']] for k,v in files.items()},'total_body_chars':record['total_body_chars'],'chunks':len(idx.documents),'checks':record['checks'],'boundary':record['boundary']},ensure_ascii=False,indent=2))
