from pathlib import Path
import os,sys,json
ROOT=Path('/Users/sunhao/Downloads/03_AIPMProject/0305_E-commerce-Customer-Service_Agent');OUT=Path('/private/tmp/rag-policy-id-rename-20261013')
sys.path.insert(0,str(ROOT/'backend'))
os.environ.update(AGENT_COURSE_OFFLINE_RAG='1',AGENT_COURSE_DISABLE_LLM='1',AGENT_COURSE_OFFLINE_FACTS='1',AGENT_COURSE_ENV=str(OUT/'no-model.env'))
from retrieval.hybrid_retrieval import get_knowledge_index
from retrieval.knowledge import promotion_policy_result
from evals.runner import EvalRunner
from agent.customer_service_agent import Lesson41Agent
from settings import CASES_PATH
NEW='promotion_camping_season_stack_rule';OLD='promotion_618_stack_rule'
idx,_=get_knowledge_index()
r=promotion_policy_result('policy-id-rename','露营季活动满减和山友会员券能不能叠加？')
ids=[c.metadata['policy_id'] for c in r.citations]
assert set(ids)=={NEW,'member_coupon_gold_rule'}
assert all(d.metadata['policy_id']!=OLD for d in idx.documents)
cases=EvalRunner(Lesson41Agent(),CASES_PATH).load_cases()
assert OLD not in json.dumps(cases,ensure_ascii=False,default=str)
assert sum(NEW in c.get('expected_citations',[]) for c in cases)==2
summary={'index':{'version':idx.version,'chunks':len(idx.documents),'embedding':idx.embedding_mode},'citation_ids':ids,'active_cases':len(cases),'new_id_expected_in_cases':2,'old_id_in_active_sources':False,'offline_validation':True}
(OUT/'inspection.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False,indent=2))
s=(Path('/private/tmp/rag-recheck-20261013/restart_services.py')).read_text().replace("OUT=Path('/private/tmp/rag-recheck-20261013')","OUT=Path('/private/tmp/rag-policy-id-rename-20261013')")
(OUT/'restart_services.py').write_text(s)
