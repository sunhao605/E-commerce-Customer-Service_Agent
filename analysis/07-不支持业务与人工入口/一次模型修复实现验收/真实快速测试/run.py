"""三条定向冒烟：仅模型外呼，业务证据使用构造fixture，含明确标识的故障注入。"""
import hashlib
import importlib
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'backend'))
for name in ('.env.model.local', '.env.local'):
    path = ROOT / name
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))
os.environ.update(AGENT_COURSE_DISABLE_LLM='0', AGENT_COURSE_OFFLINE_RAG='1',
                  AGENT_COURSE_OFFLINE_FACTS='1', ANSWER_JUDGE_MODE='enforce', ANSWER_JUDGE_TIMEOUT_SECONDS='5')
from agent.answer_review import review_audit
from agent.customer_service_agent import Lesson41Agent
from agent.models.answer_client import ModelInvocationResult
from agent.service_capabilities import catalog_payload
from agent.tools.planning import build_route_plan
from agent.tools.tool_runtime import search_products
from api.schemas import ChatRequest
from settings import openai_base_url, openai_model_name, api_key_is_missing
from tests.test_answer_judge import interaction_policy_fixture

QUESTION = '轻量露营折叠椅多少钱，还有库存吗，能登记到货提醒吗？'
BAD = '轻量露营折叠椅199元，当前无库存。我已经为您登记到货提醒，到货后我会主动通知您。'
CASES = [('Q01', '自然生成与检查', 0), ('Q02', '首稿注入错误，真实judge＋真实模型修复＋真实复查', 1),
         ('Q03', '两稿均注入错误，真实judge检查并验证最终兜底', 2)]
SOURCES = ['backend/agent/customer_service_agent.py', 'backend/agent/models/answer_client.py',
           'backend/agent/models/judge_client.py', 'backend/agent/answer_review.py']
SOURCES += [str(p.relative_to(ROOT)) for p in (ROOT / 'backend/resources/prompts').glob('*.md')]
def fingerprints():
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES}

def permitted(request):
    url = urlparse(str(request.url))
    return request.method == 'POST' and url.scheme == 'https' and url.hostname == 'api.deepseek.com' and url.path == '/chat/completions'

network = []
active_case = None
MAX_CALLS = 9

def before_send(request):
    if not permitted(request) or len(network) >= MAX_CALLS:
        raise RuntimeError('network_destination_or_budget_blocked')
    item = {'case': active_case, 'method': request.method, 'url': str(request.url), 'status': 'started'}
    network.append(item)
    return item

def wrap_sync(original):
    def send(client, request, *args, **kwargs):
        item = before_send(request); started = time.monotonic()
        try:
            response = original(client, request, *args, **kwargs)
            item.update(status=response.status_code)
            response.read()
            try: item['usage'] = response.json().get('usage', {})
            except Exception: pass
            return response
        except Exception as exc:
            item['status'] = type(exc).__name__
            raise
        finally: item['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return send

def wrap_async(original):
    async def send(client, request, *args, **kwargs):
        item = before_send(request); started = time.monotonic()
        try:
            response = await original(client, request, *args, **kwargs)
            item.update(status=response.status_code)
            # 单次传输层响应尚未读取；仅对非流式judge读取JSON以记录真实usage。
            await response.aread()
            try: item['usage'] = response.json().get('usage', {})
            except Exception: pass
            return response
        except Exception as exc:
            item['status'] = type(exc).__name__
            raise
        finally: item['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return send

assert permitted(type('Req', (), {'method': 'POST', 'url': 'https://api.deepseek.com/chat/completions'})())
assert not permitted(type('Req', (), {'method': 'POST', 'url': 'http://127.0.0.1:8081/api/orders'})())
assert not permitted(type('Req', (), {'method': 'GET', 'url': 'https://api.deepseek.com/chat/completions'})())
assert urlparse(openai_base_url()).hostname == 'api.deepseek.com'
assert not api_key_is_missing(os.getenv('AGENT_OPENAI_API_KEY'))
assert not (OUT / 'results.json').exists(), '保留原运行，不覆盖'
protected = fingerprints()
frozen = {'cases': CASES, 'question': QUESTION, 'injected_reply': BAD, 'max_calls': MAX_CALLS,
          'generation_model': openai_model_name(), 'judge_model': os.getenv('ANSWER_JUDGE_MODEL', 'deepseek-flash'),
          'judge_timeout_seconds': 5, 'source_sha256': protected,
          'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'scope': '仅脱敏构造商品/能力证据发往DeepSeek；不访问商城/Java/MySQL/RAG；不重启服务'}
(OUT / 'frozen.json').write_text(json.dumps(frozen, ensure_ascii=False, indent=2))
rows = []
try:
    with ExitStack() as stack:
        for name in ('httpx', 'httpx2'):
            module = importlib.import_module(name)
            stack.enter_context(patch.object(module.Client, '_send_single_request', wrap_sync(module.Client._send_single_request)))
            stack.enter_context(patch.object(module.AsyncClient, '_send_single_request', wrap_async(module.AsyncClient._send_single_request)))
        agent = Lesson41Agent()
        for cid, description, injected in CASES:
            active_case = cid
            started = time.monotonic()
            plan = build_route_plan(intent='product_query', user_message=QUESTION, order_id=None,
                                    model_used=False, capability_ids=['stock_notification'])
            with patch('agent.tools.tool_runtime.products_from_ecommerce', return_value=[
                {'id': 4, 'name': '轻量露营折叠椅', 'price': 199.0, 'stock': 0, 'active': True}]):
                _, call = search_products('轻量露营折叠椅')
            real_invoke = agent.answer_model_client._invoke_chain
            generated = []
            def invoke(*args, **kwargs):
                if len(generated) < injected:
                    response = ModelInvocationResult(content=BAD)
                    source = 'injected'
                else:
                    response = real_invoke(*args, **kwargs)
                    source = 'real_model'
                generated.append({'source': source, 'answer': response.content})
                return response
            review_audit.clear()
            with patch('agent.customer_service_agent.query_interaction_policy', return_value=interaction_policy_fixture()) as policy, \
                 patch.object(agent.answer_model_client, '_invoke_chain', side_effect=invoke):
                result = agent._compose_final_answer(request=ChatRequest(session_id=cid, runtime_user_id='U1001', user_message=QUESTION),
                    intent='product_query', answer='轻量露营折叠椅 商品标价199元，当前无库存。',
                    risk_level='low', next_action='answer_user', tool_calls=[call], citations=[], workflow=None,
                    cache_hit=False, degraded=False, context_report={'model_context': []},
                    capability_results=[catalog_payload('stock_notification')], route_plan=plan)
            policy.assert_called_once()
            row = {'id': cid, 'description': description, 'generated': generated, 'final': result.answer,
                   'judge': result.answer_judge, 'audit': list(review_audit),
                   'elapsed_ms': round((time.monotonic() - started)*1000)}
            rows.append(row)
            print(json.dumps({k: row[k] for k in ('id','final','judge','elapsed_ms')}, ensure_ascii=False), flush=True)
finally:
    report = {'frozen': frozen, 'rows': rows, 'network': network, 'actual_requests': len(network),
              'source_unchanged': fingerprints() == protected}
    (OUT / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'actual_requests': len(network), 'source_unchanged': report['source_unchanged']}), flush=True)
