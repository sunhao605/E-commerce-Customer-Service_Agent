"""冻结的探索路由探针，仅调用路由模型；不实例化 Agent、不访问业务工具。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agent.models.router_client import ModelRouteResult, RouteModelClient
from agent.tools.planning import classify_intent

ROOT = Path(__file__).resolve().parent
cases = json.loads((ROOT / 'frozen-route-probes.json').read_text())


def matches_expected(case, result):
    passed = result.used_model and result.intent == case['intent']
    if 'facts' in case:
        passed = passed and result.needs_product_facts is case['facts'] and set(result.knowledge_domains or []) == set(case['domains'])
    if 'clarification' in case:
        passed = passed and result.clarification_fields == case['clarification']
    return bool(passed)


def check(case):
    result = RouteModelClient().plan_intent(case['message'], fallback_intent=classify_intent(case['message']), task_note=case.get('note'))
    passed = matches_expected(case, result)
    return {'id':case['id'], 'input':case['message'], 'passed':bool(passed), 'result':result.model_dump(exclude={'prompt_fragments'})}


if __name__ == "__main__":
    assert len({case['id'] for case in cases}) == len(cases)
    assert not matches_expected({'intent': 'product_query', 'facts': True, 'domains': []},
        ModelRouteResult(intent='product_query', used_model=True, needs_product_facts=False, knowledge_domains=['product_knowledge']))
    rows = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(check, case) for case in cases]):
            row = future.result()
            rows.append(row)
            (ROOT / 'real-route-probes.json').write_text(json.dumps({'kind':'exploratory_real_routes', 'rows':sorted(rows,key=lambda r:r['id'])}, ensure_ascii=False, indent=2))
            print(json.dumps({'id':row['id'],'passed':row['passed'],'fallback':row['result']['fallback_reason'],'elapsed_ms':row['result']['elapsed_ms']},ensure_ascii=False),flush=True)
    print(json.dumps({'passed':sum(r['passed'] for r in rows),'total':len(rows),'wall_seconds':round(time.perf_counter()-started,3)},ensure_ascii=False),flush=True)
