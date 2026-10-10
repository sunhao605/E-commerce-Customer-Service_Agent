"""显式离线回归：关闭模型/网络，使用原项目种子镜像，不代表在线业务验证。"""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

os.environ.update(AGENT_COURSE_DISABLE_LLM='1', AGENT_COURSE_OFFLINE_RAG='1', AGENT_COURSE_OFFLINE_FACTS='1', AGENT_COURSE_ENV='/tmp/huangye-no-model.env', LANGCHAIN_TRACING_V2='false')
backend = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(backend))
from agent.customer_service_agent import Lesson41Agent
from evals.runner import EvalRunner
from agent.trace import trace_store

with patch('agent.ecommerce_client.ecommerce_get', side_effect=ConnectionError('explicit_offline_test')):
    report = EvalRunner(Lesson41Agent(), backend / 'resources/cases.yml').run()
output = Path(sys.argv[2])
output.write_text(json.dumps({'mode': 'offline_seed_mirror_network_blocked', 'model_calls': 0, 'report': report.model_dump(mode='json'), 'traces': {session: [e.model_dump(mode='json') for e in events] for session, events in trace_store._events.items()}}, ensure_ascii=False, indent=2))
print(json.dumps({'total':report.total,'passed':report.passed,'failed':report.failed,'failures':[{ 'case_id':r.case_id,'categories':r.failure_categories,'missing':r.missing_session_state+r.missing_answer_claims+r.missing_citations} for r in report.results if not r.passed]},ensure_ascii=False))
