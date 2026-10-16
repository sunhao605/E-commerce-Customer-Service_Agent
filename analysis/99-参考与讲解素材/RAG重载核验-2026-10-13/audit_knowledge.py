import os,sys,json,hashlib,socket
from pathlib import Path
from collections import Counter
ROOT=Path('/Users/sunhao/Downloads/03_AIPMProject/0305_E-commerce-Customer-Service_Agent')
sys.path.insert(0,str(ROOT/'backend'))
os.environ.update(AGENT_COURSE_ENV='/private/tmp/rag-recheck-20261013/no-model.env',AGENT_COURSE_OFFLINE_RAG='1',AGENT_COURSE_DISABLE_LLM='1',AGENT_COURSE_OFFLINE_FACTS='1')
from retrieval.hybrid_retrieval import get_knowledge_index,retrieve_knowledge,KNOWLEDGE_FILES
from retrieval.documents import KNOWLEDGE_DIR
from retrieval.knowledge import product_knowledge_result,promotion_policy_result
from settings import CASES_PATH,DEFAULT_COURSE_ENV_PATH
import yaml
idx,_=get_knowledge_index()
files=[]
for p in sorted(KNOWLEDGE_DIR.glob('*.md')):
    raw=p.read_text()
    if not raw.startswith('---\n'):continue
    front,_,body=raw[4:].partition('\n---\n');meta=yaml.safe_load(front)
    files.append({'file':p.name,'chars':len(body.strip()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'policy_id':meta['policy_id'],'version':meta.get('version'),'indexed':p.name in KNOWLEDGE_FILES})
cases=yaml.safe_load(CASES_PATH.read_text())['cases']
legacy_terms=['双十一','双11','11.11','618','金卡','蓝牙耳机','手机']
case_scan=[{'case_id':c['case_id'],'question':c.get('user_message',c.get('start_user_message')),'legacy_question_terms':[term for term in legacy_terms if term in c.get('user_message',c.get('start_user_message',''))]} for c in cases]
queries=[('tent-condensation','帐篷里面有水，怎么判断是冷凝还是漏水？','product_query','3.2'),('tent-care','帐篷怎么保养和收纳？','product_query','3.3'),('sleeping-bag','睡袋的极限温标能作为日常使用温度吗？','product_query','5.2'),('unknown-campaign','双十一活动也能满300减40吗？','promotion_query',None)]
probes=[]
for cid,q,intent,section in queries:
    r=product_knowledge_result('isolated-audit',q) if intent=='product_query' else promotion_policy_result('isolated-audit',q)
    probes.append({'id':cid,'query':q,'intent':intent,'answer':r.answer,'citations':[c.model_dump() for c in r.citations],'retrieval':r.retrieval_debug,'expected_section':section,'section_match':any(section in c.metadata.get('section','') for c in r.citations) if section else None})
r={'root':str(ROOT),'knowledge_dir':str(KNOWLEDGE_DIR),'cases_path':str(CASES_PATH),'default_shared_env_path':str(DEFAULT_COURSE_ENV_PATH),'shared_env_exists':DEFAULT_COURSE_ENV_PATH.exists(),'corpus':files,'index':{'version':idx.version,'chunks':len(idx.documents),'embedding':idx.embedding_mode,'chunks_per_file':dict(Counter(d.metadata['filename'] for d in idx.documents))},'active_case_count':len(cases),'active_cases':case_scan,'probes':probes,'boundary':'本地字符向量与确定性辅助函数；没有外部模型或业务接口调用'}
Path('/private/tmp/rag-recheck-20261013/inspection.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in r.items() if k not in ('probes',)},ensure_ascii=False,indent=2))
print(json.dumps([{'id':p['id'],'section_match':p['section_match'],'answer_preview':p['answer'][:180]} for p in probes],ensure_ascii=False))
