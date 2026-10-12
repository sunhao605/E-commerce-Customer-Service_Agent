"""取消判断定向实验：复用001调用与业务门控；三项语义判断组合，不接商城。"""
import argparse
import hashlib
import importlib.util
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent / '取消语义对比'
spec = importlib.util.spec_from_file_location('baseline_compare', BASE / 'run_compare.py')
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def combine(parts):
    allowed = {'decision': {'stop', 'continue', 'uncertain'},
               'target': {'current', 'other', 'ambiguous'},
               'extra_task': {'yes', 'no', 'uncertain'}}
    if not isinstance(parts, dict):
        return 'invalid'
    # DeepSeek会把选项放在choice/type中；只接受已知位置及有限枚举，不猜测自由文本。
    parts = {key: value.get('choice', value.get('type')) if isinstance(value, dict) else value for key, value in parts.items()}
    if any(not isinstance(parts.get(key), str) or parts[key] not in values for key, values in allowed.items()):
        return 'invalid'
    if parts['decision'] == 'continue':
        return 'keep'
    if parts['decision'] == 'uncertain':
        return 'clarify'
    return 'cancel' if parts['target'] == 'current' and parts['extra_task'] == 'no' else 'clarify'


def split_query(provider, fixture, case):
    questions = fixture['questions']
    if provider == 'jev':
        url, key = 'https://api.typesafe.ai/v1/systemone', os.environ['TYPESAFE_API_KEY']
        body = {'model': fixture['jev_model'], 'state': case['state'], 'questions': questions}
    else:
        url = os.environ['AGENT_OPENAI_BASE_URL'].rstrip('/') + '/chat/completions'
        key = os.environ['AGENT_OPENAI_API_KEY']
        prompt = '独立回答以下三个问题，每个字段只能使用对应criteria中的选项。不要合成最终取消动作，只返回JSON对象，字段为decision、target、extra_task。\n' + json.dumps(questions, ensure_ascii=False)
        body = {'model': os.environ['AGENT_OPENAI_MODEL'], 'temperature': 0, 'max_tokens': 128,
                'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': prompt},
                             {'role': 'user', 'content': json.dumps(case['state'], ensure_ascii=False)}]}
    request = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode(),
                                     {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = json.load(response)
    if provider == 'jev':
        parts = {key: raw['answers'][key]['choice'] for key in questions}
    else:
        try:
            parts = json.loads(raw['choices'][0]['message']['content'])
        except (ValueError, TypeError):
            parts = None
    return {'choice': combine(parts), 'components': parts, 'raw': raw,
            'usage': raw.get('usage', {}), 'model': raw.get('model')}


def run_one(arm, fixture, original, case, repeat, stream):
    started = time.perf_counter()
    try:
        if arm.startswith('split_'):
            result = split_query(arm.removeprefix('split_'), fixture, case)
        else:
            result = baseline.query(arm.removeprefix('single_'), original, case)
    except Exception as exc:
        result = {'choice': 'error', 'usage': {},
                  'error': type(exc).__name__ + (':' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else '')}
    row = {'id': case['id'], 'repeat': repeat, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 3), **result}
    row['auto_cancel'] = baseline.auto_cancel(row['choice'], case['state']['current_application'])
    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    stream.flush()
    print(arm, case['id'], repeat, row['choice'], row.get('components'), row['elapsed_ms'], flush=True)
    if row.get('error') in {'HTTPError:401', 'HTTPError:402', 'HTTPError:403'}:
        raise RuntimeError('authentication/access/balance error; stopping calls')


def metrics(arm, rows, cases, prices):
    expected = {c['id']: c for c in cases}
    valid = [r for r in rows if r['choice'] in {'cancel', 'keep', 'clarify'}]
    lo = hi = 0.0
    usage_known = True
    for row in rows:
        usage = row.get('usage', {})
        if 'jev' in arm:
            usage_known &= 'input_tokens' in usage
            amount = usage.get('input_tokens', 0) * prices['jev_input'] / 1e6
            lo += amount
            hi += amount
        elif 'deepseek' in arm:
            usage_known &= 'prompt_tokens' in usage
            hit = usage.get('prompt_cache_hit_tokens', 0)
            miss = usage.get('prompt_cache_miss_tokens', usage.get('prompt_tokens', 0) - hit)
            output = usage.get('completion_tokens', 0)
            for period in ['offpeak', 'peak']:
                amount = (hit * prices['deepseek_' + period + '_input_hit'] + miss * prices['deepseek_' + period + '_input_miss'] + output * prices['deepseek_' + period + '_output']) / 1e6
                if period == 'offpeak':
                    lo += amount
                else:
                    hi += amount
    cancels = [r for r in rows if expected[r['id']]['expected_choice'] == 'cancel']
    others = [r for r in rows if expected[r['id']]['expected_choice'] != 'cancel']
    by_id = {}
    for row in rows:
        by_id.setdefault(row['id'], []).append(row['choice'])
    return {'runs': len(rows), 'valid': len(valid),
            'correct': sum(r['choice'] == expected[r['id']]['expected_choice'] for r in rows),
            'false_cancel': sum(r['choice'] == 'cancel' for r in others), 'non_cancel_runs': len(others),
            'missed_cancel': sum(r['choice'] != 'cancel' for r in cancels), 'cancel_runs': len(cancels),
            'p50_ms': baseline.quantile([r['elapsed_ms'] for r in valid], .5),
            'p95_ms': baseline.quantile([r['elapsed_ms'] for r in valid], .95),
            'cost_estimate_usd_range': [lo, hi] if usage_known else None,
            'repeat_disagreements': [ident for ident, choices in by_id.items() if len(set(choices)) > 1],
            'mismatches': [{'id': r['id'], 'repeat': r['repeat'], 'expected': expected[r['id']]['expected_choice'],
                            'actual': r['choice'], 'components': r.get('components')} for r in rows if r['choice'] != expected[r['id']]['expected_choice']]}


def summarize(fixture, original):
    arms = fixture['arms'] + ['regression_split_jev']
    report = {'fixture_sha256': hashlib.sha256((ROOT / 'frozen.json').read_bytes()).hexdigest(), 'arms': {}}
    for arm in arms:
        path = ROOT / (arm + '.jsonl')
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            cases = original['cases'] if arm.startswith('regression_') else fixture['cases']
            report['arms'][arm] = metrics(arm, rows, cases, fixture['prices_usd_per_million'])
    (ROOT / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['self-check', 'regression', 'holdout', 'repair-deepseek', 'summary'])
    args = parser.parse_args()
    data = (ROOT / 'frozen.json').read_bytes()
    assert hashlib.sha256(data).hexdigest() == (ROOT / 'frozen.sha256').read_text().strip()
    fixture = json.loads(data)
    assert hashlib.sha256((BASE / 'frozen.json').read_bytes()).hexdigest() == fixture['baseline_fixture_sha256']
    original = json.loads((BASE / 'frozen.json').read_text())
    if args.mode == 'self-check':
        assert combine({'decision': 'stop', 'target': 'current', 'extra_task': 'no'}) == 'cancel'
        assert combine({'decision': 'stop', 'target': 'other', 'extra_task': 'no'}) == 'clarify'
        assert combine({'decision': 'stop', 'target': 'current', 'extra_task': 'yes'}) == 'clarify'
        assert combine({'decision': 'uncertain', 'target': 'current', 'extra_task': 'no'}) == 'clarify'
        assert combine({'decision': 'continue', 'target': 'ambiguous', 'extra_task': 'yes'}) == 'keep'
        assert combine({'decision': 'stop', 'target': 'current'}) == 'invalid'
        assert combine(None) == 'invalid'
        assert combine({'decision': {'choice': 'stop'}, 'target': {'choice': 'current'}, 'extra_task': {'choice': 'no'}}) == 'cancel'
        assert combine({'decision': {'type': 'stop'}, 'target': {'type': 'other'}, 'extra_task': {'type': 'no'}}) == 'clarify'
        assert combine({'decision': {'type': 'choice'}, 'target': 'current', 'extra_task': 'no'}) == 'invalid'
        assert combine({'decision': {}, 'target': 'current', 'extra_task': 'no'}) == 'invalid'
        assert not baseline.auto_cancel('cancel', {'stage': 'submitted', 'owner_verified': True})
        assert not baseline.auto_cancel('cancel', {'stage': 'draft', 'owner_verified': False})
        assert len({c['id'] for c in fixture['cases']}) == 20
        assert not {c['state']['latest_user_message'] for c in fixture['cases']} & {c['state']['latest_user_message'] for c in original['cases']}
        print('self-check passed: combination branches, invalid output, state boundary, 20 new utterances')
        return
    if args.mode == 'summary':
        print(json.dumps(summarize(fixture, original), ensure_ascii=False))
        return
    baseline.load_config()
    required = ['TYPESAFE_API_KEY'] if args.mode == 'regression' else ['TYPESAFE_API_KEY', 'AGENT_OPENAI_API_KEY', 'AGENT_OPENAI_BASE_URL', 'AGENT_OPENAI_MODEL']
    assert all(os.getenv(key) for key in required), 'missing model configuration'
    arms = ['split_jev'] if args.mode == 'regression' else ['split_deepseek'] if args.mode == 'repair-deepseek' else fixture['arms']
    filenames = {arm: ('regression_split_jev' if args.mode == 'regression' else arm) for arm in arms}
    assert all(not (ROOT / (name + '.jsonl')).exists() for name in filenames.values()), 'preserve existing runs'
    metadata = {'started_utc': datetime.now(timezone.utc).isoformat(), 'mode': args.mode,
                'fixture_sha256': hashlib.sha256(data).hexdigest(),
                'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'baseline_script_sha256': hashlib.sha256((BASE / 'run_compare.py').read_bytes()).hexdigest(),
                'rule_source_sha256': baseline.rule_pattern()[1], 'retries': 0, 'timeout_seconds': 30}
    (ROOT / (args.mode + '-metadata.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    streams = {arm: (ROOT / (name + '.jsonl')).open('w') for arm, name in filenames.items()}
    try:
        for repeat in range(1, fixture['repeats'] + 1):
            cases = original['cases'] if args.mode == 'regression' else fixture['cases']
            cases = cases if repeat == 1 else list(reversed(cases))
            for case in cases:
                for arm in arms if repeat == 1 else list(reversed(arms)):
                    run_one(arm, fixture, original, case, repeat, streams[arm])
    finally:
        for stream in streams.values():
            stream.close()
        summarize(fixture, original)


if __name__ == '__main__':
    main()
