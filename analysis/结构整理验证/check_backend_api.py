"""目录迁移后的离线入口检查；禁用模型、Embedding 和业务网络。"""
import importlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

root = Path(__file__).resolve().parents[2]
os.environ.update(AGENT_COURSE_DISABLE_LLM='1', AGENT_COURSE_OFFLINE_RAG='1',
                  AGENT_COURSE_OFFLINE_FACTS='1', AGENT_COURSE_ENV='/tmp/huangye-no-model.env',
                  LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false')
sys.path.insert(0, str(root / 'backend'))
from fastapi.testclient import TestClient

layout = json.loads(Path(__file__).with_name('backend-layout.json').read_text())
for name in layout['modules'].values():
    importlib.import_module(name)
from api.routes import app
from agent.prompts import PROMPTS_DIR, load_prompt
from retrieval.documents import KNOWLEDGE_DIR, load_knowledge_citation
from settings import CAPABILITIES_PATH, CASES_PATH, DEFAULT_COURSE_ENV_PATH

assert DEFAULT_COURSE_ENV_PATH == root.parent / 'course.env'
assert all(path.is_file() for path in (CAPABILITIES_PATH, CASES_PATH))
for path in PROMPTS_DIR.glob('*.md'):
    assert load_prompt(path.stem)
for path in KNOWLEDGE_DIR.glob('*.md'):
    assert load_knowledge_citation(path.name).title
with patch('agent.ecommerce_client.ecommerce_get', side_effect=ConnectionError('offline smoke')):
    with TestClient(app) as client:
        assert client.get('/health').json()['status'] == 'ok'
        assert client.get('/capabilities').json()['agent']['name'] == '荒野户外售后服务助手'
        reply = client.post('/chat', json={'session_id': 'backend-layout-smoke',
                            'runtime_user_id': 'U1001', 'user_message': '露营折叠椅多少钱、有多少库存？'})
        assert reply.status_code == 200, reply.text
        data = reply.json()
        assert any(call['tool_name'] == 'search_products' for call in data['tool_calls'])
        assert '599' in data['answer'] and '520' in data['answer'], data['answer']
        assert client.get('/sessions/backend-layout-smoke/trace').json()
result = {'modules_imported': len(layout['modules']), 'prompts_loaded': len(list(PROMPTS_DIR.glob('*.md'))),
          'knowledge_loaded': len(list(KNOWLEDGE_DIR.glob('*.md'))),
          'endpoints': ['health', 'capabilities', 'chat', 'trace'], 'model_calls': 0}
Path(__file__).with_name('backend-api-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
