"""回放 LIVE-001 已保存的原路由，注入生成失败；不调用真实模型或业务系统。"""
import copy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

os.environ.update(AGENT_COURSE_ENV='/dev/null', AGENT_COURSE_DISABLE_LLM='1',
                  AGENT_COURSE_OFFLINE_RAG='1', AGENT_COURSE_OFFLINE_FACTS='1', CANCEL_INTENT_OBSERVE='0')

from agent.context import current_memory
from agent.models.router_client import ModelRouteResult
from tests.test_answer_content import ContentPipelineTest

OUT = Path(__file__).resolve().parent
SOURCE = OUT.parent / 'ANSWER-CONTENT-LIVE-001' / 'results.json'
original_bytes = SOURCE.read_bytes()
source = json.loads(original_bytes)
rows = []
for original in source['rows']:
    if original['id'] not in {'N02', 'N12'}:
        continue
    case = ContentPipelineTest()
    case.setUp()
    try:
        archived_route = next(s['result'] for s in original['stages'] if s['kind'] == 'route')
        draft = {'intent': 'refund_request', 'pending_slot': 'order_id', 'candidates': []}
        if original['id'] == 'N12':
            current_memory('content-pipeline', 'U1001')['open_task'] = copy.deepcopy(draft)
        case.generate.side_effect = TimeoutError('offline_replay_injection')
        with patch.object(case.agent.route_model_client, 'plan_intent',
                          return_value=ModelRouteResult.model_validate(archived_route)):
            response = case.chat(original['question'])
        state = response.session_state
        assert not response.tool_calls and state['workflow'] is None
        assert case.route.call_count == 0 and case.judge.call_count == 0
        if original['id'] == 'N02':
            assert state['route_plan']['topic_status'] == 'valid'
            assert state['route_plan']['inquiry_topics'] == ['promotion_stacking']
            assert '同类型满减券' in response.answer and '不支持到货' in response.answer
            assert response.answer.index('不支持到货') < response.answer.index('满 300 减 40')
        else:
            assert state['memory']['open_task'] == draft
            assert '本人' in response.answer and '未到使用时间' in response.answer
        rows.append({'id': original['id'], 'question': original['question'],
                     'archived_route': archived_route, 'response': response.model_dump(mode='json'),
                     'check': 'pass', 'kind': 'offline_saved_route_replay_with_generation_failure'})
    finally:
        case.doCleanups()
assert SOURCE.read_bytes() == original_bytes
(OUT / 'original-route-replay.json').write_text(json.dumps({
    'source': str(SOURCE.relative_to(OUT.parents[2])),
    'source_sha256': hashlib.sha256(original_bytes).hexdigest(),
    'real_model_calls': 0, 'source_unchanged': True, 'rows': rows,
}, ensure_ascii=False, indent=2) + '\n')
print('保存的原路由离线回放：2/2；真实模型调用0；业务调用0；原结果未改动。')
