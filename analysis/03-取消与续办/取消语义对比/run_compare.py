"""独立分类比较；仅调用模型API，不访问商城。密钥只从环境/忽略的本地配置读取。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]


def load_config():
    for path in [REPO / '.env.model.local', REPO / '.env.local']:
        if path.exists():
            for line in path.read_text().splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def auto_cancel(choice, state):
    return choice == 'cancel' and state['owner_verified'] and state['stage'] in {
        'draft', 'choosing_order', 'awaiting_reason'
    }


def rule_pattern():
    path = REPO / 'ecommerce-backend/src/main/java/com/teachdemo/ecommerce/customer_service/CustomerServiceGatewayService.java'
    source = path.read_text()
    literal = next(line for line in source.splitlines() if 'private static final Pattern CANCEL_DRAFT' in line)
    pattern = literal.split('Pattern.compile("', 1)[1].rsplit('");', 1)[0].replace('\\\\', '\\')
    return re.compile(pattern), hashlib.sha256(source.encode()).hexdigest()


def query(provider, fixture, case):
    if provider == 'rules':
        pattern, _ = rule_pattern()
        choice = 'cancel' if pattern.fullmatch(case['state']['latest_user_message'].strip()) else 'keep'
        return {'choice': choice, 'model': 'current_java_regex_python_equivalent', 'usage': {}, 'raw': None}
    criteria = fixture['criteria']
    if provider == 'deepseek':
        url = os.environ['AGENT_OPENAI_BASE_URL'].rstrip('/') + '/chat/completions'
        key = os.environ['AGENT_OPENAI_API_KEY']
        body = {'model': os.environ['AGENT_OPENAI_MODEL'], 'temperature': 0, 'max_tokens': 128,
                'response_format': {'type': 'json_object'}, 'thinking': {'type': 'disabled'},
                'messages': [{'role': 'system', 'content': fixture['instructions'] + '\n' + json.dumps(criteria, ensure_ascii=False) + '\n只输出JSON：{"choice":"cancel或keep或clarify"}。'},
                             {'role': 'user', 'content': json.dumps(case['state'], ensure_ascii=False)}]}
    else:
        url = 'https://api.typesafe.ai/v1/systemone'
        key = os.environ['TYPESAFE_API_KEY']
        body = {'model': fixture['jev_model'], 'state': case['state'],
                'questions': {'cancel_intent': {'type': 'choice', 'instructions': fixture['instructions'], 'criteria': criteria}}}
    request = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode(),
                                     {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = json.load(response)
    if provider == 'deepseek':
        content = raw['choices'][0]['message']['content']
        try:
            choice = json.loads(content).get('choice')
        except (ValueError, AttributeError):
            choice = None
        result = {'choice': choice, 'content': content}
    else:
        answer = raw['answers']['cancel_intent']
        result = {'choice': answer['choice'], 'probabilities': answer['probabilities'], 'confidence': answer['confidence']}
    result.update(model=raw.get('model'), usage=raw.get('usage', {}), raw=raw)
    if result['choice'] not in criteria:
        result['choice'] = 'invalid'
    return result


def quantile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(fixture):
    cases = {c['id']: c for c in fixture['cases']}
    report = {'fixture_sha256': hashlib.sha256((ROOT / 'frozen.json').read_bytes()).hexdigest(), 'providers': {}}
    prices = fixture['prices_usd_per_million']
    for provider in ['rules', 'deepseek', 'jev']:
        path = ROOT / (provider + '.jsonl')
        if not path.exists():
            report['providers'][provider] = {'status': 'not_run', 'reason': 'credential_missing' if provider == 'jev' else 'not_run'}
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        valid = [r for r in rows if r['choice'] in fixture['criteria']]
        latencies = [r['elapsed_ms'] for r in valid]
        cancel_cases = [r for r in rows if cases[r['id']]['expected_choice'] == 'cancel']
        others = [r for r in rows if cases[r['id']]['expected_choice'] != 'cancel']
        by_id = {}
        for row in rows:
            by_id.setdefault(row['id'], []).append(row['choice'])
        disagree = [ident for ident, choices in by_id.items() if len(set(choices)) > 1]
        estimate_low = estimate_high = 0.0
        tokens_known = all('prompt_tokens' in r.get('usage', {}) if provider == 'deepseek' else 'input_tokens' in r.get('usage', {}) for r in valid) if provider != 'rules' else True
        for row in valid:
            usage = row.get('usage', {})
            if provider == 'deepseek':
                hit = usage.get('prompt_cache_hit_tokens', 0)
                miss = usage.get('prompt_cache_miss_tokens', usage.get('prompt_tokens', 0) - hit)
                output = usage.get('completion_tokens', 0)
                estimate_low += (hit * prices['deepseek_offpeak_input_hit'] + miss * prices['deepseek_offpeak_input_miss'] + output * prices['deepseek_offpeak_output']) / 1e6
                estimate_high += (hit * prices['deepseek_peak_input_hit'] + miss * prices['deepseek_peak_input_miss'] + output * prices['deepseek_peak_output']) / 1e6
            elif provider == 'jev':
                amount = usage.get('input_tokens', 0) * prices['jev_input'] / 1e6
                estimate_low += amount
                estimate_high += amount
        report['providers'][provider] = {
            'status': 'complete' if len(rows) == len(cases) * fixture['repeats'] else 'partial',
            'runs': len(rows), 'valid_responses': len(valid), 'correct': sum(r['choice'] == cases[r['id']]['expected_choice'] for r in rows),
            'false_cancel': sum(r['choice'] == 'cancel' for r in others), 'non_cancel_runs': len(others),
            'missed_cancel': sum(r['choice'] != 'cancel' for r in cancel_cases), 'cancel_runs': len(cancel_cases),
            'clarify_correct': sum(r['choice'] == 'clarify' for r in rows if cases[r['id']]['expected_choice'] == 'clarify'),
            'state_gate_correct': sum(r['auto_cancel'] == cases[r['id']]['expected_auto_cancel'] for r in rows),
            'p50_ms': quantile(latencies, .5), 'p95_ms': quantile(latencies, .95),
            'repeat_disagreements': disagree, 'cost_estimate_usd_range': [estimate_low, estimate_high] if tokens_known else None,
            'errors': [{'id': r['id'], 'repeat': r['repeat'], 'error': r.get('error')} for r in rows if r['choice'] not in fixture['criteria']],
            'mismatches': [{'id': r['id'], 'repeat': r['repeat'], 'expected': cases[r['id']]['expected_choice'], 'actual': r['choice']} for r in rows if r['choice'] != cases[r['id']]['expected_choice']]
        }
    (ROOT / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('provider', choices=['rules', 'deepseek', 'jev', 'summary', 'self-check'])
    args = parser.parse_args()
    data = (ROOT / 'frozen.json').read_bytes()
    assert hashlib.sha256(data).hexdigest() == (ROOT / 'frozen.sha256').read_text().strip(), 'frozen fixture changed'
    fixture = json.loads(data)
    if args.provider == 'self-check':
        pattern, _ = rule_pattern()
        assert pattern.fullmatch('算了，不退了。')
        assert not pattern.fullmatch('我想了想 还是不退了')
        assert not pattern.fullmatch('如果不退了，优惠券会恢复吗？')
        assert not auto_cancel('cancel', {'stage': 'submitted', 'owner_verified': True})
        assert not auto_cancel('cancel', {'stage': 'draft', 'owner_verified': False})
        assert auto_cancel('cancel', {'stage': 'draft', 'owner_verified': True})
        assert len({c['id'] for c in fixture['cases']}) == 30
        print('self-check passed')
        return
    if args.provider == 'summary':
        print(json.dumps(summarize(fixture), ensure_ascii=False))
        return
    load_config()
    required = ['TYPESAFE_API_KEY'] if args.provider == 'jev' else ['AGENT_OPENAI_API_KEY', 'AGENT_OPENAI_BASE_URL', 'AGENT_OPENAI_MODEL'] if args.provider == 'deepseek' else []
    if any(not os.getenv(name) for name in required):
        print('missing configuration: ' + ', '.join(name for name in required if not os.getenv(name)))
        return
    path = ROOT / (args.provider + '.jsonl')
    assert not path.exists(), 'preserve existing run; use a new experiment directory for reruns'
    metadata = {'provider': args.provider, 'started_utc': datetime.now(timezone.utc).isoformat(), 'fixture_sha256': hashlib.sha256(data).hexdigest(), 'rule_source_sha256': rule_pattern()[1], 'retries': 0, 'timeout_seconds': 30, 'repeats': fixture['repeats']}
    (ROOT / (args.provider + '-metadata.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    with path.open('w') as stream:
        for repeat in range(fixture['repeats']):
            # 第二轮倒序，减小单一顺序与短期缓存对结果的影响；所有模型使用同一顺序。
            cases = fixture['cases'] if repeat == 0 else list(reversed(fixture['cases']))
            for case in cases:
                started = time.perf_counter()
                try:
                    result = query(args.provider, fixture, case)
                except Exception as exc:
                    result = {'choice': 'error', 'error': type(exc).__name__ + (':' + str(exc.code) if isinstance(exc, urllib.error.HTTPError) else ''), 'usage': {}}
                row = {'id': case['id'], 'repeat': repeat + 1, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 3), **result}
                row['auto_cancel'] = auto_cancel(row['choice'], case['state']['current_application'])
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                stream.flush()
                print(args.provider, case['id'], repeat + 1, row['choice'], row['elapsed_ms'], flush=True)
                if row.get('error') in ['HTTPError:401', 'HTTPError:402', 'HTTPError:403']:
                    print('stopped: authentication/access/balance error', flush=True)
                    break
            else:
                continue
            break
    summarize(fixture)


if __name__ == '__main__':
    main()
