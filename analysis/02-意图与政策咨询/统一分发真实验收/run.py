"""统一分发真实模型小批验收（DISPATCH-LIVE-001）：自然问句→真实路由→离线知识取证→真实生成/judge。

复用 ANSWER-CONTENT-LIVE-001 的 guard（预算、来源校验、计量）与冻结方式；
业务事实为构造夹具，知识为当前离线快照；不触 Java/MySQL/页面/日常服务。
"""
import copy
import hashlib
import importlib.util
import json
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'backend'))
GUARD_PATH = OUT.parent.parent / '07-不支持业务与人工入口' / 'BC029-四条跨出口验收' / 'run.py'
spec = importlib.util.spec_from_file_location('existing_acceptance_guard', GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
from agent.answer_review import review_audit, redact_review
from agent.context import SESSION_MEMORIES, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.service_capabilities import catalog_payload
from agent.workflows.after_sale_graph import REFUND_APPROVAL_GRAPH, RECEIVED_RETURN_GRAPH
from api.schemas import ChatRequest, HistoryMessage
from retrieval.documents import get_knowledge_snapshot
from settings import answer_judge_settings, openai_model_name, openai_base_url, api_key_is_missing
from tests.test_answer_judge import interaction_policy_fixture

PRODUCTS = [{'id': 4, 'name': '山野四人帐篷', 'price': 899.0, 'stock': 3, 'active': True, 'returnable': True}]
DRAFT = {'intent': 'refund_request', 'pending_slot': 'order_id',
         'candidates': [{'value': 'ORD9801', 'label': 'ORD9801', 'hint': '帐篷 899元'},
                        {'value': 'ORD9802', 'label': 'ORD9802', 'hint': '野餐垫 199元'}]}
# 6 用例：核心组合双向、澄清与保留、售后插问、单域对照、生成故障注入。
CASES = [
 {'id': 'D01', 'question': '发票多久开好？露营季满减和平台会员券能叠加吗？',
  'expected': '发票规则（24小时/订单详情下载）＋叠加规则（可叠加/同类券不重复/本人有效）均来自正确领域；无退货政策混入',
  'budget': 5},
 {'id': 'D02', 'question': '露营季满减和平台会员券能叠加吗？发票多久开好？',
  'expected': '顺序互换后两项仍各自取证正确；答复顺序可变，分发不变',
  'budget': 5},
 {'id': 'D03', 'question': '会员券能和优惠叠加吗？另外发票多久开好？',
  'expected': '优惠组合未明确时可澄清该项，但发票答案必须保留并给出；不能只发澄清丢发票',
  'budget': 6},
 {'id': 'D04', 'seed_draft': True, 'question': '先问一下，帐篷怎么清洗收纳？',
  'expected': '商品知识作答；原退款待办（ORD9801/ORD9802 候选）完整保留，不当作退款原因，不创建申请',
  'budget': 5},
 {'id': 'D05', 'question': '电子发票多久开好？',
  'expected': '单域发票对照：发票组件＋payment_invoice 来源，无其他领域资料混入',
  'budget': 4},
 {'id': 'D06', 'question': '发票多久开好？露营季满减和平台会员券能叠加吗？', 'injection': 'generation_failure',
  'expected': '注入生成故障后走无模型备用；已核验的发票与优惠依据仍保留在答复中，不发送错误候选',
  'budget': 2},
]
STATE = {'phase': None, 'case': None, 'stop': None}
MAX_CALLS = 40
TOKEN_STOP = 120000


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def phase(name):
    old = STATE['phase']; STATE['phase'] = name
    try:
        yield
    finally:
        STATE['phase'] = old


def setup_guard():
    guard.CASES = CASES
    guard.MAX_CALLS = MAX_CALLS
    original = guard.before_send
    def before(request):
        used = sum(item.get('usage', {}).get('total_tokens', 0) for item in guard.network)
        if STATE['stop'] or used >= TOKEN_STOP:
            raise RuntimeError('run_stopped_or_token_budget_reached')
        item = original(request)
        body = json.loads(request.content)
        if body.get('model') != openai_model_name() or body.get('stream'):
            raise RuntimeError('unexpected_model_or_stream')
        item['stage'] = STATE['phase']
        return item
    guard.before_send = before
    capture = guard.capture
    def record(item, response, started):
        capture(item, response, started)
        if response.status_code in {401, 402, 403, 429}:
            STATE['stop'] = 'provider_http_' + str(response.status_code)
    guard.capture = record


def config():
    assert guard.urlparse(openai_base_url()).hostname == 'api.deepseek.com'
    assert not api_key_is_missing(os.getenv('AGENT_OPENAI_API_KEY'))
    assert openai_model_name() == 'deepseek-flash'
    mode, judge_model, timeout = answer_judge_settings()
    assert mode == 'enforce' and judge_model == openai_model_name()
    return {'host': 'api.deepseek.com', 'model': openai_model_name(), 'judge_model': judge_model,
            'judge_timeout_seconds': timeout, 'mode': mode, 'max_model_http': MAX_CALLS,
            'stop_before_next_request_at_tokens': TOKEN_STOP, 'automatic_retries': 0,
            'offline_rag': True, 'business_fixtures': True,
            'generation_temperature': 0.2, 'judge_temperature': 0}


def prepare():
    assert not (OUT / 'frozen.json').exists(), '不覆盖冻结'
    guard.self_check()
    frozen = {'cases': CASES,
              'fixtures': {'products': PRODUCTS, 'pending_draft': DRAFT,
                           'interaction_policy': interaction_policy_fixture()},
              'config': config(),
              'source_sha256': guard.fingerprints(), 'runner_sha256': sha(Path(__file__)),
              'guard_sha256': sha(GUARD_PATH),
              'knowledge_snapshot': get_knowledge_snapshot().version,
              'scope': '6条自然请求（含1条生成故障注入）；真实路由/最终生成/judge；离线知识检索＋构造业务事实；非在线Embedding或Java/MySQL/页面验收',
              'judging': '运行前预期由助手冻结；模型不看expected；运行后按①取证正确②结果完整③业务状态保留逐题判定，judge pass不替代验收；首次失败不重跑挑结果。'}
    dump('frozen.json', frozen)
    (OUT / 'frozen.sha256').write_text(sha(OUT / 'frozen.json') + '\n')
    print(json.dumps({'prepared': True, 'cases': len(CASES), 'real_http': 0,
                      'config': frozen['config']}, ensure_ascii=False))


def run():
    frozen = json.loads((OUT / 'frozen.json').read_text())
    assert sha(OUT / 'frozen.json') == (OUT / 'frozen.sha256').read_text().strip()
    assert frozen['runner_sha256'] == sha(Path(__file__)) and frozen['guard_sha256'] == sha(GUARD_PATH)
    assert frozen['source_sha256'] == guard.fingerprints() and config() == frozen['config']
    assert not (OUT / 'responses.jsonl').exists(), '不覆盖首次运行'
    setup_guard()
    SESSION_MEMORIES.clear(); review_audit.clear()
    agent = Lesson41Agent(); rows = []; calls = []; stages = []
    real_route = agent.route_model_client.plan_intent
    real_invoke = agent.answer_model_client._invoke_chain
    real_judge = agent.answer_judge_client.judge

    def route(*args, **kwargs):
        with phase('route'):
            result = real_route(*args, **kwargs)
        stages.append({'kind': 'route', 'result': result.model_dump(mode='json')})
        return result

    def invoke(*args, **kwargs):
        case = STATE['case']
        if case.get('injection') == 'generation_failure':
            stages.append({'kind': 'injected_generation_failure'})
            raise TimeoutError('local_injection')
        return real_invoke(*args, **kwargs)

    def judge(*args, **kwargs):
        with phase('judge'):
            result = real_judge(*args, **kwargs)
        stages.append({'kind': 'judge', 'result': result.model_dump(mode='json')})
        return result

    def policy():
        calls.append({'kind': 'interaction_policy'}); return interaction_policy_fixture()

    def products(*args, **kwargs):
        calls.append({'kind': 'products'}); return copy.deepcopy(PRODUCTS)

    def detail(pid, *args, **kwargs):
        calls.append({'kind': 'product_detail', 'id': str(pid)})
        p = next((p for p in PRODUCTS if str(p['id']) == str(pid)), None)
        return ({**p, '_fact_source': 'isolated_fixture'}, None) if p else (None, 'product_not_found')

    def forbidden(*args, **kwargs):
        raise RuntimeError('business_write_or_unfrozen_read_blocked')

    with ExitStack() as stack:
        guard.install_guards(stack)
        for target, name, replacement in (
            (agent.route_model_client, 'plan_intent', route),
            (agent.answer_model_client, '_invoke_chain', invoke),
            (agent.answer_judge_client, 'judge', judge),
            (REFUND_APPROVAL_GRAPH, 'run', forbidden),
            (RECEIVED_RETURN_GRAPH, 'run', forbidden)):
            stack.enter_context(patch.object(target, name, side_effect=replacement))
        for name, fn in (
            ('agent.customer_service_agent.query_interaction_policy', policy),
            ('agent.tools.tool_runtime.products_from_ecommerce', products),
            ('agent.tools.tool_runtime.product_detail_from_ecommerce', detail),
            ('agent.tools.tool_runtime.order_fact_from_ecommerce', forbidden),
            ('agent.tools.tool_runtime.after_sale_requests_from_ecommerce', forbidden)):
            stack.enter_context(patch(name, side_effect=fn))
        with (OUT / 'responses.jsonl').open('x') as output:
            try:
                for case in frozen['cases']:
                    if STATE['stop']:
                        break
                    STATE['case'] = case; guard.active_case = case['id']
                    calls.clear(); stages.clear(); review_audit.clear()
                    sid = 'dispatch-live-' + case['id']
                    memory = current_memory(sid, 'SYNTHETIC_USER')
                    if case.get('seed_draft'):
                        memory['open_task'] = copy.deepcopy(DRAFT)
                    task_before = copy.deepcopy(memory.get('open_task'))
                    started = time.monotonic()
                    row = {'id': case['id'], 'question': case['question'],
                           'task_before': task_before}
                    try:
                        response = agent.chat(ChatRequest(
                            session_id=sid, runtime_user_id='SYNTHETIC_USER',
                            user_message=case['question']))
                        row['response'] = response.model_dump(mode='json')
                    except Exception as exc:
                        row['error'] = type(exc).__name__
                    row['task_after'] = copy.deepcopy(memory.get('open_task'))
                    row.update(elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                               business_calls=copy.deepcopy(calls),
                               stages=copy.deepcopy(stages),
                               audit=copy.deepcopy(list(review_audit)),
                               network=[x for x in guard.network if x['case'] == case['id']])
                    row = redact_review(row); rows.append(row)
                    output.write(json.dumps(row, ensure_ascii=False) + '\n'); output.flush()
                    plan = (row.get('response') or {}).get('route_plan') or {}
                    print(json.dumps({'id': case['id'], 'error': row.get('error'),
                        'http': len(row['network']),
                        'domains': [s.get('knowledge_domain') for s in plan.get('retrieval_strategies', [])],
                        'task_kept': row['task_after'] == row['task_before'],
                        'final': (row.get('response') or {}).get('answer')}, ensure_ascii=False), flush=True)
            finally:
                dump('results.json', {'rows': rows, 'network': guard.network,
                    'actual_requests': len(guard.network), 'stop_reason': STATE['stop'],
                    'source_unchanged': frozen['source_sha256'] == guard.fingerprints(),
                    'runner_unchanged': frozen['runner_sha256'] == sha(Path(__file__)),
                    'frozen_unchanged': sha(OUT / 'frozen.json') == (OUT / 'frozen.sha256').read_text().strip()})
                print(json.dumps({'completed_cases': len(rows),
                    'actual_requests': len(guard.network), 'stop': STATE['stop']}), flush=True)


if __name__ == '__main__':
    os.environ.update(CANCEL_INTENT_EXECUTE='0', CANCEL_INTENT_OBSERVE='0')
    if '--prepare' in sys.argv:
        prepare()
    elif '--run' in sys.argv:
        run()
    else:
        raise SystemExit('use --prepare or --run')
