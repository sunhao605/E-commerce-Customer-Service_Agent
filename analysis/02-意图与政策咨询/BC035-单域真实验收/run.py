"""BC-035 单域政策无命中真实模型小批验收（SD-LIVE-001）：自然问句→真实路由→离线知识取证→验证不生成/不judge。

复用 DISPATCH-LIVE-001 的 guard（预算、来源校验、计量）与冻结方式；
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
from unittest.mock import patch

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
# 4 用例：BC-035 核心场景（单域无命中）+ 单域有命中对照 + 售后待办下插问。
CASES = [
 {'id': 'SD01', 'question': '只咨询退款多久到账，不办理。',
  'expected': '单域退款政策无命中时，不调用生成/judge，答复为"未找到规则"说明，风险medium',
  'budget': 3},
 {'id': 'SD02', 'question': '电子发票多久开好？',
  'expected': '单域发票有命中时，正常生成+judge，答案含24小时',
  'budget': 4},
 {'id': 'SD03', 'seed_draft': True, 'question': '只咨询退款多久到账，不办理。',
  'expected': '售后待办下插问单域政策无命中；原退款待办完整保留；不调用生成/judge',
  'budget': 3},
 {'id': 'SD04', 'question': '签收后的七天无理由退货有哪些条件？',
  'expected': '单域退货政策有命中时，正常生成+judge，答案含退货条件',
  'budget': 4},
]
STATE = {'phase': None, 'case': None, 'stop': None}
MAX_CALLS = 20
TOKEN_STOP = 60000


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
              'scope': '4条自然请求；真实路由；单域无命中时不应调用生成/judge；离线知识检索＋构造业务事实；非在线Embedding或Java/MySQL/页面验收',
              'judging': '运行前预期由助手冻结；模型不看expected；运行后按①单域无命中时generation=0且judge=0②风险medium③业务状态保留逐题判定；首次失败不重跑挑结果。'}
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
        with phase('generation'):
            result = real_invoke(*args, **kwargs)
        stages.append({'kind': 'generation', 'result': result.model_dump(mode='json')})
        return result

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
                    sid = 'sd-live-' + case['id']
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
                    gen_calls = len([s for s in stages if s['kind'] == 'generation'])
                    judge_calls = len([s for s in stages if s['kind'] == 'judge'])
                    print(json.dumps({'id': case['id'], 'error': row.get('error'),
                        'http': len(row['network']),
                        'domains': [s.get('knowledge_domain') for s in plan.get('retrieval_strategies', [])],
                        'task_kept': row['task_after'] == row['task_before'],
                        'gen_calls': gen_calls, 'judge_calls': judge_calls,
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
