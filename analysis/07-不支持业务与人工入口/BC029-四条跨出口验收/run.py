"""四条固定出口验收：当前离线知识＋真实生成/judge；业务策略为fixture。"""
import asyncio
import hashlib
import importlib
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
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
                  AGENT_COURSE_OFFLINE_FACTS='1', ANSWER_JUDGE_MODE='enforce',
                  ANSWER_JUDGE_TIMEOUT_SECONDS='5')
from agent.answer_review import build_review_context, review_audit
from agent.customer_service_agent import Lesson41Agent
from agent.models.answer_client import ModelInvocationResult
from agent.service_capabilities import catalog_payload
from agent.tools.planning import build_route_plan
from api.schemas import ChatRequest
from retrieval.knowledge import policy_query_result, promotion_policy_result
from settings import api_key_is_missing, openai_base_url, openai_model_name
from tests.test_answer_judge import interaction_policy_fixture

QUESTION = '露营季满300减40能和平台会员券叠加吗？另外支持到货提醒吗？'
BAD = '露营季满300减40可以和平台会员券叠加，同类型满减券不能重复叠加，具体适用需核验。我已为您登记到货提醒，到货后会主动通知您。'
CASES = [
    {'id': 'C01', 'question': QUESTION, 'intent': 'promotion_query', 'injection': None,
     'expected': '答清可叠加、同类券限制、个人适用待核验及提醒不支持；正常放行或真实修复后放行。', 'budget': 4},
    {'id': 'C02', 'question': QUESTION, 'intent': 'promotion_query', 'injection': 'first_generation',
     'expected': '真实judge拒绝注入的登记承诺；基于同一证据真实修复并复查，至多修复一次。', 'budget': 3},
    {'id': 'C03', 'question': QUESTION, 'intent': 'promotion_query', 'injection': 'judge_timeout',
     'expected': '注入judge调用不可用后不修复，发送无模型备用稿；保留有效政策与提醒范围。', 'budget': 1},
    {'id': 'C04', 'question': '未发货退款什么时候到账？', 'intent': 'refund_policy_query', 'injection': None,
     'expected': '先说明当前演示余额／真实渠道无到账时限保证及具体结果需核验；只是咨询、未申请，不展开无关办理细节。', 'budget': 4},
]
FORBIDDEN = ['无依据代办或主动通知承诺', '教学说明／模拟问法进入正文',
             '同一结论反复堆叠', '未问政策整段带入', '丢失影响结论的条件或未知边界']
network = []
active_case = None
MAX_CALLS = 12

def permitted(request):
    url = urlparse(str(request.url))
    return (request.method == 'POST' and url.scheme == 'https' and url.hostname == 'api.deepseek.com'
            and url.port in (None, 443) and url.path == '/chat/completions')

def before_send(request):
    case_budget = next((c['budget'] for c in CASES if c['id'] == active_case), MAX_CALLS)
    if (not permitted(request) or len(network) >= MAX_CALLS
            or sum(item['case'] == active_case for item in network) >= case_budget):
        raise RuntimeError('network_destination_or_budget_blocked')
    body = json.loads(request.content)
    item = {'case': active_case, 'status': 'started', 'model': body.get('model'),
            'stage': 'judge' if body.get('response_format') else 'generation',
            'input_sha256': hashlib.sha256(request.content).hexdigest()}
    network.append(item)
    return item

def capture(item, response, started):
    item.update(status=response.status_code, elapsed_ms=round((time.monotonic()-started)*1000))
    try:
        item['usage'] = response.json().get('usage', {})
    except (ValueError, UnicodeDecodeError):
        item['usage'] = {}

def wrap_sync(original):
    def send(client, request, *args, **kwargs):
        item = before_send(request)
        started = time.monotonic()
        try:
            response = original(client, request, *args, **kwargs)
            response.read()
            capture(item, response, started)
            return response
        except Exception as exc:
            item.update(status=type(exc).__name__, elapsed_ms=round((time.monotonic()-started)*1000))
            raise
    return send

def wrap_async(original):
    async def send(client, request, *args, **kwargs):
        item = before_send(request)
        started = time.monotonic()
        try:
            response = await original(client, request, *args, **kwargs)
            await response.aread()
            capture(item, response, started)
            return response
        except Exception as exc:
            item.update(status=type(exc).__name__, elapsed_ms=round((time.monotonic()-started)*1000))
            raise
    return send

def install_guards(stack):
    for name in ('httpx', 'httpx2'):
        module = importlib.import_module(name)
        stack.enter_context(patch.object(module.Client, '_send_single_request', wrap_sync(module.Client._send_single_request)))
        stack.enter_context(patch.object(module.AsyncClient, '_send_single_request', wrap_async(module.AsyncClient._send_single_request)))

def self_check():
    global active_case
    active_case = 'self_check'
    with ExitStack() as stack:
        install_guards(stack)
        for name in ('httpx', 'httpx2'):
            module = importlib.import_module(name)
            transport = module.MockTransport(lambda request: module.Response(200, json={'usage': {'total_tokens': 1}}))
            with module.Client(transport=transport) as client:
                client.post('https://api.deepseek.com/chat/completions', json={})
                for method, url in [('GET', 'https://api.deepseek.com/chat/completions'),
                                    ('POST', 'https://other.invalid/chat/completions'),
                                    ('POST', 'http://127.0.0.1:8081/api/orders')]:
                    try:
                        client.request(method, url, json={})
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError('forbidden_request_not_blocked')
            async def check_async():
                async with module.AsyncClient(transport=transport) as client:
                    await client.post('https://api.deepseek.com/chat/completions', json={})
            asyncio.run(check_async())
        assert len(network) == 4 and all(item['status'] == 200 for item in network)
        import httpx
        while len(network) < MAX_CALLS:
            before_send(httpx.Request('POST', 'https://api.deepseek.com/chat/completions', json={}))
        try:
            before_send(httpx.Request('POST', 'https://api.deepseek.com/chat/completions', json={}))
        except RuntimeError:
            pass
        else:
            raise AssertionError('budget_not_enforced')
        network.clear()
        active_case = 'C03'
        before_send(httpx.Request('POST', 'https://api.deepseek.com/chat/completions', json={}))
        try:
            before_send(httpx.Request('POST', 'https://api.deepseek.com/chat/completions', json={}))
        except RuntimeError:
            pass
        else:
            raise AssertionError('case_budget_not_enforced')
    network.clear()
    active_case = None

def fingerprints():
    paths = [p for p in (ROOT/'backend').rglob('*') if p.is_file() and p.suffix in {'.py', '.md', '.json', '.yml'}]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}

if __name__ == '__main__':
    self_check()
    if '--self-check' in sys.argv:
        print('httpx/httpx2同步异步、禁止主机与方法、总预算和单例预算自检通过；联网0。')
        raise SystemExit(0)
    assert not (OUT/'results.json').exists(), '保留原运行，不覆盖'
    assert urlparse(openai_base_url()).hostname == 'api.deepseek.com'
    assert not api_key_is_missing(os.getenv('AGENT_OPENAI_API_KEY'))
    protected = fingerprints()
    rows = []
    frozen = {'cases': CASES, 'forbidden': FORBIDDEN, 'bad_candidate': BAD, 'max_calls': MAX_CALLS,
              'model': openai_model_name(), 'judge_model': os.getenv('ANSWER_JUDGE_MODEL', 'deepseek-flash'),
              'generation_temperature': 0.2, 'judge_temperature': 0, 'judge_timeout_seconds': 5,
              'retries': 0, 'source_sha256': protected,
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'scope': '当前离线检索＋固定RoutePlan＋构造能力和入口策略＋真实最终生成/judge，非在线RAG/商城全链路'}
    with ExitStack() as stack:
        install_guards(stack)
        agent = Lesson41Agent()
        prepared = []
        for case in CASES:
            knowledge = (promotion_policy_result(case['id'], case['question'], clarification_required=False)
                         if case['intent'] == 'promotion_query'
                         else policy_query_result(case['id'], case['intent'], case['question']))
            assert knowledge.citations and knowledge.next_action == 'answer_user'
            capabilities = [catalog_payload('stock_notification')] if case['intent'] == 'promotion_query' else []
            plan = build_route_plan(intent=case['intent'], user_message=case['question'], order_id=None,
                                    model_used=False, capability_ids=['stock_notification'] if capabilities else [])
            prepared.append((case, knowledge, capabilities, plan))
        frozen['inputs'] = [{'id': c['id'], 'knowledge': k.answer, 'citations': [x.model_dump(mode='json') for x in k.citations],
                             'capabilities': caps, 'plan': plan.model_dump(mode='json')}
                            for c, k, caps, plan in prepared]
        frozen['generation_contexts'] = [build_review_context(
            request=ChatRequest(session_id=c['id'], runtime_user_id='SYNTHETIC_USER', user_message=c['question']),
            plan=plan, answer=k.answer, tool_calls=[], citations=k.citations, capabilities=caps, history=[],
            interaction_policy=interaction_policy_fixture())[0] for c, k, caps, plan in prepared]
        (OUT/'frozen.json').write_text(json.dumps(frozen, ensure_ascii=False, indent=2))
        if '--prepare' in sys.argv:
            assert not network
            print('四条输入、证据和生成上下文已冻结；真实请求0。')
            raise SystemExit(0)
        try:
            for case, knowledge, capabilities, plan in prepared:
                active_case = case['id']
                started = time.monotonic()
                generated = []
                original_invoke = agent.answer_model_client._invoke_chain
                def invoke(*args, **kwargs):
                    injected = case['injection'] == 'first_generation' and not generated
                    result = ModelInvocationResult(content=BAD) if injected else original_invoke(*args, **kwargs)
                    generated.append({'source': 'injected' if injected else 'real_model', 'answer': result.content})
                    return result
                review_audit.clear()
                with ExitStack() as case_stack:
                    policy = case_stack.enter_context(patch('agent.customer_service_agent.query_interaction_policy', return_value=interaction_policy_fixture()))
                    case_stack.enter_context(patch.object(agent.answer_model_client, '_invoke_chain', side_effect=invoke))
                    if case['injection'] == 'judge_timeout':
                        case_stack.enter_context(patch.object(agent.answer_judge_client, '_request', new=AsyncMock(side_effect=TimeoutError('injected'))))
                    result = agent._compose_final_answer(
                        request=ChatRequest(session_id=case['id'], runtime_user_id='SYNTHETIC_USER', user_message=case['question']),
                        intent=case['intent'], answer=knowledge.answer, risk_level='low', next_action='answer_user',
                        tool_calls=[], citations=knowledge.citations, workflow=None, cache_hit=False, degraded=False,
                        context_report={'model_context': []}, capability_results=capabilities, route_plan=plan)
                    policy.assert_called_once()
                row = {'id': case['id'], 'generated': generated, 'final': result.answer, 'judge': result.answer_judge,
                       'audit': list(review_audit), 'elapsed_ms': round((time.monotonic()-started)*1000)}
                rows.append(row)
                print(json.dumps({key: row[key] for key in ('id', 'final', 'judge', 'elapsed_ms')}, ensure_ascii=False), flush=True)
        finally:
            report = {'rows': rows, 'network': network, 'actual_requests': len(network),
                      'source_unchanged': fingerprints() == protected}
            (OUT/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({'actual_requests': len(network), 'source_unchanged': report['source_unchanged']}), flush=True)
