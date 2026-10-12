"""Jev置信分流→DeepSeek复核的隔离实验；业务动作和政策查询均为Mock。"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('targeted', ROOT.parent / '取消语义定向修复/run_targeted.py')
targeted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(targeted)


def valid_parts(parts):
    allowed = {'decision': {'stop', 'continue', 'uncertain'},
               'target': {'current', 'other', 'ambiguous'},
               'extra_task': {'yes', 'no', 'uncertain'}}
    return isinstance(parts, dict) and all(isinstance(parts.get(k), str) and parts[k] in v for k, v in allowed.items())


def review_needed(jev, threshold):
    parts = jev.get('components')
    if not valid_parts(parts):
        return True
    answers = jev.get('raw', {}).get('answers', {})
    keys = ['decision', 'extra_task'] if parts['decision'] == 'continue' else list(parts)
    scores = [answers.get(k, {}).get('confidence') for k in keys]
    if any(not isinstance(s, (int, float)) or isinstance(s, bool) or not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
        return True
    if min(scores) < threshold or parts['extra_task'] != 'no':
        return True
    return parts['decision'] != 'continue' and (parts['decision'] != 'stop' or parts['target'] != 'current')


def review_query(fixture, state):
    # 不把Jev选项告诉复核模型，减少顺从首次错误的机会。
    body = {'model': os.environ['AGENT_OPENAI_MODEL'], 'temperature': 0, 'max_tokens': 512,
            'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'},
            'messages': [{'role': 'system', 'content': fixture['review_prompt']},
                         {'role': 'user', 'content': json.dumps(state, ensure_ascii=False)}]}
    request = urllib.request.Request(os.environ['AGENT_OPENAI_BASE_URL'].rstrip('/') + '/chat/completions',
                                    json.dumps(body, ensure_ascii=False).encode(),
                                    {'Authorization': 'Bearer ' + os.environ['AGENT_OPENAI_API_KEY'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = json.load(response)
    try:
        result = json.loads(raw['choices'][0]['message']['content'])
    except (ValueError, KeyError, TypeError):
        result = None
    return {'result': result, 'raw': raw, 'usage': raw.get('usage', {}), 'model': raw.get('model')}


def valid_tasks(tasks, state):
    if not isinstance(tasks, list) or len(tasks) > 4:
        return False
    allowed = {'cancel_refund', 'lookup_logistics', 'lookup_coupons', 'prepare_refund', 'cancel_order', 'withdraw_other_refund'}
    for task in tasks:
        if not isinstance(task, dict) or task.get('kind') not in allowed:
            return False
        order = task.get('order')
        known = state.get('known_orders', [state['current_application']['order']])
        if order is not None and (not isinstance(order, str) or order not in known):
            return False
        if task['kind'] != 'lookup_coupons' and order is None:
            return False
        if task['kind'] == 'cancel_refund' and order != state['current_application']['order']:
            return False
    return sum(t['kind'] == 'cancel_refund' for t in tasks) <= 1


def valid_review(result, state):
    if not valid_parts(result) or result.get('reason') not in {'none', 'object', 'condition', 'hesitation'} or not valid_tasks(result.get('tasks'), state):
        return False
    tasks = result['tasks']
    cancels = [t for t in tasks if t['kind'] == 'cancel_refund']
    if len(cancels) > 1 or (cancels and (result['decision'] != 'stop' or result['target'] != 'current' or result['reason'] != 'none')):
        return False
    if result['decision'] == 'stop' and result['target'] == 'current' and result['reason'] == 'none' and not cancels:
        return False
    primary = {'cancel_refund'} if result['target'] == 'current' else {'cancel_order', 'withdraw_other_refund'}
    extra = [t for t in tasks if t['kind'] not in primary]
    return not (result['extra_task'] == 'no' and extra) and not (result['extra_task'] == 'yes' and not extra)


def confirmation(state, reason, tasks=None):
    draft = state['current_application']
    tasks = tasks if valid_tasks(tasks, state) and any(t['kind'] == 'cancel_refund' for t in tasks) and all(t['kind'] in {'cancel_refund', 'lookup_logistics', 'lookup_coupons', 'prepare_refund'} for t in tasks) else []
    return {'action': 'confirm_cancel', 'reason': reason, 'tasks': tasks,
            'confirmation': {'order': draft['order'], 'draft_token': draft['token'],
                             'question': f"是否取消{draft['order']}这次尚未提交的售后申请？",
                             'pending_tasks': tasks,
                             'buttons': [{'decision': 'KEEP', 'label': '继续办理'},
                                         {'decision': 'CANCEL_SUBMIT', 'label': '按所列顺序处理这些任务' if tasks else '取消本次申请'}]}}


def route(state, jev, review, environment, threshold):
    draft = state['current_application']
    if not draft['owner_verified']:
        return {'action': 'denied', 'tasks': []}
    if draft['stage'] not in {'draft', 'choosing_order', 'awaiting_reason'}:
        return {'action': 'not_cancellable', 'tasks': []}
    parts = jev.get('components')
    if not review_needed(jev, threshold):
        if parts['decision'] == 'continue':
            return {'action': 'keep', 'tasks': []}
        return {'action': 'cancel', 'tasks': [{'kind': 'cancel_refund', 'order': draft['order']}]}
    result = review.get('result') if isinstance(review, dict) else None
    if valid_parts(result) and result.get('reason') == 'condition':
        # shortcut: 只注入测试政策结果；接商城前须绑定订单资格及真实政策来源。
        fact = environment.get('return_shipping', {'status': 'unavailable'})
        return {'action': 'lookup_fact', 'tasks': [{'kind': 'lookup_return_shipping', 'order': draft['order']}],
                'fact': fact, 'next': confirmation(state, 'fact_requires_user_decision') if fact.get('status') == 'ok' else None}
    if valid_parts(result) and result.get('reason') == 'hesitation':
        return {'action': 'ask_decision', 'tasks': [], 'question': '先保留当前办理。你决定继续还是取消后再操作。'}
    target_score = jev.get('raw', {}).get('answers', {}).get('target', {}).get('confidence', 0)
    uncertain_object = valid_parts(parts) and parts['decision'] != 'continue' and (parts['target'] != 'current' or not isinstance(target_score, (int, float)) or not math.isfinite(target_score) or target_score < threshold)
    if uncertain_object or valid_parts(result) and (result.get('reason') == 'object' or result['target'] in {'other', 'ambiguous'} and result['decision'] != 'continue'):
        # 两个模型同意也不能替代缺失的对象确认。
        return {'action': 'ask_object', 'tasks': [], 'question': f"你要取消订单本身，还是售后申请？请说明订单号。当前售后是{draft['order']}。"}
    if not valid_review(result, state):
        return confirmation(state, 'review_unavailable_or_invalid', result.get('tasks') if isinstance(result, dict) else None)
    if result['decision'] == 'uncertain':
        return {'action': 'ask_decision', 'tasks': [], 'question': '先保留当前办理。你决定继续还是取消后再操作。'}
    keys = ['decision', 'extra_task'] if result['decision'] == 'continue' else ['decision', 'target', 'extra_task']
    if not valid_parts(parts) or any(parts[k] != result[k] for k in keys):
        return confirmation(state, 'model_disagreement', result['tasks'])
    if result['extra_task'] == 'yes':
        return {'action': 'plan_tasks', 'tasks': result['tasks']}
    if result['decision'] == 'continue':
        return {'action': 'keep', 'tasks': []}
    return {'action': 'cancel', 'tasks': result['tasks']}


def mock_execute(plan, state, fail_kind=None):
    draft = dict(state['current_application'])
    trace = []
    for task in plan['tasks'] if plan['action'] in {'cancel', 'plan_tasks'} else []:
        if task['kind'] == fail_kind:
            trace.append({**task, 'status': 'failed'})
            break
        if task['kind'] == 'cancel_refund':
            if not targeted.baseline.auto_cancel('cancel', draft) or task['order'] != draft['order']:
                trace.append({**task, 'status': 'blocked'})
                break
            draft['stage'] = 'cancelled'
            trace.append({**task, 'status': 'cancelled'})
        elif task['kind'] in {'lookup_logistics', 'lookup_coupons'}:
            trace.append({**task, 'status': 'mock_read_only'})
        elif task['kind'] == 'prepare_refund':
            trace.append({**task, 'status': 'mock_prepared_not_submitted'})
        else:
            trace.append({**task, 'status': 'unsupported'})
            break
    return {'draft': draft, 'trace': trace}


def click(plan, state, decision, token, order):
    card = plan.get('confirmation')
    draft = state['current_application']
    if not card or token != card['draft_token'] or token != draft['token'] or order != card['order'] or order != draft['order']:
        return {'status': 'blocked'}
    if not targeted.baseline.auto_cancel('cancel', draft):
        return {'status': 'blocked'}
    if decision == 'KEEP':
        return {'status': 'kept'}
    if decision == 'CANCEL_SUBMIT':
        if card.get('pending_tasks'):
            execution = mock_execute({'action': 'plan_tasks', 'tasks': card['pending_tasks']}, state)
            draft.update(execution['draft'])
            return {'status': 'cancelled' if draft['stage'] == 'cancelled' else 'blocked', 'trace': execution['trace']}
        draft['stage'] = 'cancelled'
        return {'status': 'cancelled'}
    return {'status': 'blocked'}


def safe_call(fn, *args):
    started = time.perf_counter()
    try:
        result = fn(*args)
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 402, 403}:
            raise RuntimeError(f'Access/balance error {exc.code}; stopped') from None
        result = {'error': type(exc).__name__, 'usage': {}}
    return {**result, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 3)}


def run_case(fixture, case, repeat, cached=None):
    started = time.perf_counter()
    state = case['state']
    jev = cached if cached is not None else safe_call(targeted.split_query, 'jev', fixture, case)
    needs_review = targeted.baseline.auto_cancel('cancel', state['current_application']) and review_needed(jev, fixture['threshold'])
    review = safe_call(review_query, fixture, state) if needs_review else None
    plan = route(state, jev, review, case.get('environment', {}), fixture['threshold'])
    execution = mock_execute(plan, state)
    expected = case['expected']
    actual_tasks = [t['kind'] + ':' + str(t['order']) for t in plan['tasks']]
    correct = plan['action'] in expected['actions'] and ('tasks' not in expected or actual_tasks == expected['tasks'])
    false_cancel = execution['draft']['stage'] == 'cancelled' and not expected['allow_cancel']
    return {'id': case['id'], 'repeat': repeat, 'jev_cached': cached is not None, 'jev': jev, 'review': review,
            'baseline_choice': targeted.combine(jev.get('components')), 'plan': plan, 'execution': execution,
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 3), 'correct': correct, 'false_cancel': false_cancel}


def self_check(fixture):
    state = fixture['cases'][0]['state']
    def jev(parts, scores=(1, 1, 1)):
        return {'components': parts, 'raw': {'answers': {k: {'confidence': s} for k, s in zip(parts, scores)}}}
    stop = {'decision': 'stop', 'target': 'current', 'extra_task': 'no'}
    keep = {'decision': 'continue', 'target': 'ambiguous', 'extra_task': 'no'}
    assert not review_needed(jev(keep, (1, 0, 1)), .85)
    assert review_needed(jev(stop, (.82, 1, 1)), .85)
    assert review_needed(jev(stop, (float('nan'), 1, 1)), .85)
    for reason, action in [('object', 'ask_object'), ('condition', 'lookup_fact'), ('hesitation', 'ask_decision')]:
        uncertain = {'result': {'decision': 'uncertain', 'target': 'ambiguous' if reason == 'object' else 'current',
                                'extra_task': 'no', 'reason': reason, 'tasks': []}}
        assert route(state, jev(stop, (.5, 1, 1)), uncertain, {}, .85)['action'] == action
    review = {'result': {**keep, 'reason': 'none', 'tasks': []}}
    plan = route(state, jev(stop, (.82, 1, 1)), review, {}, .85)
    assert plan['action'] == 'confirm_cancel'
    agreeing = {'result': {**stop, 'reason': 'none', 'tasks': [{'kind': 'cancel_refund', 'order': state['current_application']['order']}]}}
    assert route(state, jev(stop, (1, .49, 1)), agreeing, {}, .85)['action'] == 'ask_object'
    token, order = state['current_application']['token'], state['current_application']['order']
    assert click(plan, state, 'CANCEL_SUBMIT', 'wrong', order)['status'] == 'blocked'
    assert click(plan, state, 'CANCEL_SUBMIT', token, 'ORDER_B')['status'] == 'blocked'
    assert click(plan, state, 'KEEP', token, order)['status'] == 'kept'
    clone = json.loads(json.dumps(state))
    assert click(plan, clone, 'CANCEL_SUBMIT', token, order)['status'] == 'cancelled'
    assert click(plan, clone, 'CANCEL_SUBMIT', token, order)['status'] == 'blocked'
    clone = json.loads(json.dumps(state)); clone['current_application']['token'] = 'new-token'
    assert click(plan, clone, 'CANCEL_SUBMIT', token, order)['status'] == 'blocked'
    clone['current_application']['owner_verified'] = False
    assert route(clone, jev(stop), None, {}, .85)['action'] == 'denied'
    clone = json.loads(json.dumps(state)); clone['current_application']['stage'] = 'submitted'
    assert route(clone, jev(stop), None, {}, .85)['action'] == 'not_cancellable'
    tasks = [{'kind': 'cancel_refund', 'order': order}, {'kind': 'prepare_refund', 'order': 'ORDER_B'}]
    compound = {'action': 'plan_tasks', 'tasks': tasks}
    assert len(mock_execute(compound, state, 'cancel_refund')['trace']) == 1
    assert mock_execute(compound, state, 'prepare_refund')['draft']['stage'] == 'cancelled'
    result = {**stop, 'reason': 'none', 'extra_task': 'yes', 'tasks': tasks}
    assert valid_review(result, state)
    result['tasks'][0]['order'] = 'ORDER_B'
    assert not valid_review(result, state)
    tasks = [{'kind': 'lookup_coupons', 'order': None}, {'kind': 'cancel_refund', 'order': order}]
    inconsistent = {'result': {**keep, 'target': 'current', 'extra_task': 'yes', 'reason': 'none', 'tasks': tasks}}
    queued = route(state, jev({**stop, 'extra_task': 'yes'}), inconsistent, {}, .85)
    assert queued['action'] == 'confirm_cancel' and queued['tasks'] == tasks
    clone = json.loads(json.dumps(state))
    assert [t['kind'] for t in click(queued, clone, 'CANCEL_SUBMIT', token, order)['trace']] == ['lookup_coupons', 'cancel_refund']
    assert len({c['id'] for c in fixture['cases']}) == len(fixture['cases'])
    print('self-check passed: routes, invalid scores/schema, permissions, button identity/token/repeat, ordered partial failure')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['self-check', 'run', 'regression'])
    args = parser.parse_args()
    data = (ROOT / 'frozen.json').read_bytes()
    assert hashlib.sha256(data).hexdigest() == (ROOT / 'frozen.sha256').read_text().strip()
    fixture = json.loads(data)
    if args.mode == 'self-check':
        self_check(fixture)
        return
    targeted.baseline.load_config()
    assert all(os.environ.get(k) for k in ['TYPESAFE_API_KEY', 'AGENT_OPENAI_API_KEY', 'AGENT_OPENAI_MODEL', 'AGENT_OPENAI_BASE_URL'])
    path = ROOT / (args.mode + '.jsonl')
    assert not path.exists(), 'preserve original run'
    metadata = {'started_utc': datetime.now(timezone.utc).isoformat(), 'fixture_sha256': hashlib.sha256(data).hexdigest(),
                'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'retries': 0, 'mock_actions': True}
    (ROOT / (args.mode + '-metadata.json')).write_text(json.dumps(metadata, indent=2) + '\n')
    if args.mode == 'regression':
        runs = []
        for case in fixture['regression']:
            rows = [json.loads(line) for line in (ROOT.parent / case['raw_file']).read_text().splitlines()]
            runs.extend((case, row['repeat'], row) for row in rows if row['id'] == case['id'])
    else:
        runs = [(case, repeat, None) for repeat in [1, 2] for case in (fixture['cases'] if repeat == 1 else list(reversed(fixture['cases'])))]
    with path.open('w') as stream:
        for case, repeat, cached in runs:
            row = run_case(fixture, case, repeat, cached)
            stream.write(json.dumps(row, ensure_ascii=False) + '\n'); stream.flush()
            print(case['id'], repeat, row['plan']['action'], row['correct'], 'review=' + str(row['review'] is not None), flush=True)


if __name__ == '__main__':
    main()
