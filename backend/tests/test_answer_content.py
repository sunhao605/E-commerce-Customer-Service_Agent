"""ANSWER-CONTENT-001：固定路由输出经过真实解析/编排；不计真实模型语义成绩。"""
import copy
import json
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from api.schemas import ChatRequest, HistoryMessage
from agent.context import SESSION_MEMORIES, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.models.router_client import RouteModelClient
from agent.models.answer_client import ModelInvocationResult
from agent.models.judge_client import AnswerJudgeResult
from agent.tools.planning import build_route_plan
from agent.answer_review import review_audit
from agent.service_capabilities import catalog_payload
from retrieval import documents as docs
from retrieval import rules
from retrieval.hybrid_retrieval import retrieve_knowledge, reset_hybrid_index_and_cache


def decision(topics, intent='promotion_query', **extra):
    value = {'intent': intent, 'capability_ids': [], 'inquiry_topics': topics}
    if intent == 'promotion_query':
        value['clarification_fields'] = []
    return {**value, **extra}


def judgment(verdict='pass'):
    return AnswerJudgeResult(status='success', decision='allow' if verdict == 'pass' else 'fallback',
        invocation_attempted=True, checks={name: {'verdict': verdict if name == 'unsupported_action' else 'pass',
        'evidence_ids': ['capability:stock_notification'], 'reason': '固定测试判定', 'requirement': ''}
        for name in ('omission', 'conflict', 'unsupported_action')})


class TopicContractTest(unittest.TestCase):
    def test_local_topic_errors_preserve_intent_and_capability(self):
        for value, status in ((None, 'invalid_type'), ([], 'empty'), ('refund_arrival', 'invalid_type'),
                              (['bogus'], 'unknown_id'), (['refund_arrival'] * 2, 'duplicate')):
            payload = decision(value, 'refund_policy_query', capability_ids=['human_takeover'])
            parsed = RouteModelClient._extract_decision(json.dumps(payload))
            self.assertEqual(parsed['intent'], 'refund_policy_query')
            self.assertEqual(parsed['capability_ids'], ['human_takeover'])
            self.assertEqual(parsed['topic_status'], status)
        payload.pop('inquiry_topics')
        self.assertEqual(RouteModelClient._extract_decision(json.dumps(payload))['topic_status'], 'missing')

    def test_cross_domain_topics_are_kept_and_no_model_is_not_no_demand(self):
        # MULTI-READ-001：跨域只读主题不再被主意图判 incompatible 丢弃，全部保留并并入知识域。
        plan = build_route_plan(intent='invoice_query', user_message='发票', order_id=None,
            model_used=True, inquiry_topics=['refund_arrival'], topic_status='valid')
        self.assertEqual(plan.topic_status, 'valid')
        self.assertTrue(plan.needs_rag)
        self.assertEqual(plan.inquiry_topics, ['refund_arrival'])
        self.assertIn('after_sale_policy', plan.knowledge_domains)
        self.assertIn('invoice', plan.knowledge_domains)
        # 无模型时仍标记 no_model，主题清空。
        plan = build_route_plan(intent='invoice_query', user_message='发票', order_id=None,
            model_used=False, inquiry_topics=['refund_arrival'], topic_status='valid')
        self.assertEqual(plan.topic_status, 'no_model')
        self.assertEqual(plan.inquiry_topics, [])

    def test_route_output_budget_for_compact_multi_topic_contract(self):
        payload = decision(['tent_care', 'promotion_stacking', 'coupon_recovery'], 'product_query',
            needs_product_facts=True, knowledge_domains=['product_knowledge', 'promotion_and_member_policy'],
            clarification_fields=[], capability_ids=['stock_notification', 'human_takeover'],
            answer_order=['facts', 'topic:tent_care', 'topic:promotion_stacking', 'topic:coupon_recovery',
                          'capability:stock_notification', 'capability:human_takeover'])
        # ASCII bytes are a conservative bound for this fixture, not provider usage.
        self.assertLess(len(json.dumps(payload, separators=(',', ':')).encode()), 512)
        self.assertIsNone(RouteModelClient._extract_decision(json.dumps(payload)[:-8]))

    def test_scope_consultation_keeps_controlled_knowledge_without_authorizing_tools(self):
        for topic, domain in rules.topic_catalog().items():
            with self.subTest(topic=topic):
                plan = build_route_plan(intent='service_scope_query', user_message='混合咨询', order_id=None,
                    model_used=True, inquiry_topics=[topic], topic_status='valid',
                    capability_ids=['stock_notification'])
                self.assertEqual(plan.inquiry_topics, [topic])
                self.assertEqual(plan.knowledge_domains, [domain])
                self.assertTrue(plan.needs_rag)
                self.assertEqual(plan.required_tools, [])
                self.assertFalse(plan.requires_workflow)


class CompleteRulesTest(unittest.TestCase):
    def select(self, topics, domains=None, candidates=None):
        return rules.select_rules(list(docs.get_knowledge_snapshot().citations.values()) if candidates is None else candidates,
            topics, domains or list(set(rules.topic_catalog().values())))

    def test_stack_pair_preserves_conditions_and_only_requested_recovery(self):
        selected, info = self.select(['promotion_stacking'])
        answer = rules.rules_answer(selected, info)
        self.assertEqual(answer.count('可以与平台会员券叠加'), 1)
        for term in ('同类型满减券', '开始和截止时间', '本人', '每满', '运费', '结算'):
            self.assertIn(term, answer)
        self.assertNotIn('退款', answer)
        selected, info = self.select(['promotion_stacking', 'coupon_recovery'])
        self.assertIn('退款不自动代表优惠券已退回', rules.rules_answer(selected, info))
        stack = next(c for c in selected if c.metadata['rule_id'] == 'promotion_stacking')
        self.assertEqual(len(stack.metadata['rule_sources']), 2)

    def test_three_refund_questions_have_distinct_complete_answers(self):
        answers = []
        for topic, contains, absent in (
            ('refund_arrival', '到账时限保证', '确认信息包括'),
            ('refund_conditions', '未出库', '待审核：'),
            ('refund_process', '待审核', '今日标价')):
            selected, info = self.select([topic])
            text = rules.rules_answer(selected, info)
            self.assertIn(contains, text); self.assertNotIn(absent, text)
            answers.append(text)
        self.assertEqual(len(set(answers)), 3)

    def test_complete_rule_survives_chunking_and_titles(self):
        reset_hybrid_index_and_cache()
        result = retrieve_knowledge('帐篷清洁晾干', 'product_query', top_k=1)
        selected, info = self.select(['tent_care'], candidates=result.citations)
        care = next(c for c in selected if c.metadata['rule_id'] == 'tent_care')
        self.assertIn('单张照片不能独立确定责任', care.snippet)
        self.assertIn('不使用洗衣机', care.snippet)
        changed = [c.model_copy(update={'title': '任意标题', 'snippet': '切断的结论'}) for c in result.citations]
        other, _ = self.select(['tent_care'], candidates=changed)
        self.assertEqual([c.snippet for c in selected], [c.snippet for c in other])

    def test_missing_identity_evidence_does_not_restore_old_guide(self):
        selected, info = self.select(['tent_care'], candidates=[])
        self.assertEqual(selected, [])
        self.assertIn('topic_unconfirmed:tent_care', info['gaps'])

    def altered(self, mutate):
        snapshot = copy.deepcopy(docs.get_knowledge_snapshot())
        mutate(snapshot)
        stack = ExitStack()
        stack.enter_context(patch.object(docs, 'get_knowledge_snapshot', return_value=snapshot))
        stack.enter_context(patch.object(rules, 'get_knowledge_snapshot', return_value=snapshot))
        return stack

    def test_conflicting_rule_is_not_first_wins(self):
        def mutate(snapshot):
            c = snapshot.citations['promotion_policy.md']
            r = copy.deepcopy(next(r for r in c.metadata['rules'] if r['id'] == 'promotion_stacking'))
            r['text'] = '同条件下不能叠加。'
            c.metadata['rules'].append(r)
        with self.altered(mutate):
            selected, info = self.select(['promotion_stacking'])
        self.assertNotIn('promotion_stacking', [c.metadata['rule_id'] for c in selected])
        self.assertIn('rule_conflict:promotion_stacking', info['gaps'])

    def test_different_scope_is_kept_separate(self):
        def mutate(snapshot):
            c = snapshot.citations['promotion_policy.md']
            r = copy.deepcopy(next(r for r in c.metadata['rules'] if r['id'] == 'promotion_stacking'))
            r.update(scope='other_channel', text='另一个渠道的独立规则。', requires=[])
            c.metadata['rules'].append(r)
        with self.altered(mutate):
            selected, _ = self.select(['promotion_stacking'])
        self.assertEqual(len([c for c in selected if c.metadata['rule_id'] == 'promotion_stacking']), 2)

    def test_missing_condition_blocks_conclusion_but_keeps_other_topic(self):
        def mutate(snapshot):
            c = snapshot.citations['member_coupon_policy.md']
            c.metadata['rules'] = [r for r in c.metadata['rules'] if r['id'] != 'coupon_conditions']
        with self.altered(mutate):
            selected, info = self.select(['promotion_stacking', 'coupon_recovery'])
        self.assertNotIn('promotion_stacking', [c.metadata['rule_id'] for c in selected])
        self.assertIn('coupon_recovery', [c.metadata['rule_id'] for c in selected])
        self.assertIn('rule_conditions_missing:promotion_stacking', info['gaps'])

    def test_time_boundary_and_withdrawal_remove_only_affected_rules(self):
        from datetime import datetime
        token = docs._REQUEST_TIME.set(datetime.fromisoformat('2028-01-01T00:00:00+08:00'))
        try:
            selected, info = self.select(['invoice_issue'])
            self.assertEqual(selected, []); self.assertTrue(info['gaps'])
        finally:
            docs._REQUEST_TIME.reset(token)
        with self.altered(lambda s: s.citations['promotion_policy.md'].metadata.update(publication_status='withdrawn')):
            selected, info = self.select(['promotion_stacking', 'invoice_issue'])
            self.assertEqual([c.metadata['rule_id'] for c in selected], ['invoice_issue'])
            self.assertTrue(info['gaps'])

    def test_topic_changes_do_not_reuse_selected_content(self):
        first, _ = self.select(['promotion_stacking'])
        second, _ = self.select(['coupon_recovery'])
        self.assertNotEqual([c.metadata['rule_id'] for c in first], [c.metadata['rule_id'] for c in second])
        # Selection is intentionally uncached; retrieval cache cannot cache the final topic choice.
        self.assertNotIn('promotion_stacking', [c.metadata['rule_id'] for c in second])


    def test_invalid_annotation_does_not_take_down_other_rules(self):
        valid = '<!-- rule {"id":"x","domain":"invoice","scope":"demo","label":"开票","topics":["x"],"requires":[]} -->24小时<!-- /rule -->'
        parsed, issues = rules.parse_rules('<!-- rule {"id":12} -->坏规则<!-- /rule -->' + valid)
        self.assertEqual([r['id'] for r in parsed], ['x'])
        self.assertEqual(issues, ['invalid_rule_annotation'])

    def test_unannotated_source_is_a_gap_not_raw_fallback(self):
        with self.altered(lambda s: s.citations['payment_invoice_policy.md'].metadata.update(rules=[])):
            selected, info = self.select(['invoice_issue'])
        self.assertEqual(selected, [])
        self.assertIn('topic_unconfirmed:invoice_issue', info['gaps'])

    def test_same_identity_different_version_is_a_conflict(self):
        def mutate(snapshot):
            original = snapshot.citations['promotion_policy.md']
            other = snapshot.citations['member_coupon_policy.md']
            other.metadata['version'] += '.different'
            rule = copy.deepcopy(next(r for r in original.metadata['rules'] if r['id'] == 'promotion_stacking'))
            other.metadata['rules'].append(rule)
        with self.altered(mutate):
            selected, info = self.select(['promotion_stacking'])
        self.assertNotIn('promotion_stacking', [c.metadata['rule_id'] for c in selected])
        self.assertIn('rule_conflict:promotion_stacking', info['gaps'])

    def test_short_invoice_and_product_safety_units(self):
        selected, info = self.select(['invoice_issue', 'trekking_poles_safety'])
        answer = rules.rules_answer(selected, info)
        self.assertIn('24 小时', answer)
        self.assertIn('停止不可靠的支撑', answer)
        self.assertNotIn('炊具', answer)
        self.assertFalse(info['gaps'])


class ContentPipelineTest(unittest.TestCase):
    def setUp(self):
        SESSION_MEMORIES.clear(); review_audit.clear()
        self.agent = Lesson41Agent()
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'ANSWER_JUDGE_MODE': 'enforce',
            'AGENT_COURSE_DISABLE_LLM': '1', 'AGENT_COURSE_OFFLINE_RAG': '1', 'AGENT_COURSE_OFFLINE_FACTS': '1'}))
        self.stack.enter_context(patch.object(self.agent.route_model_client, 'can_call_model', return_value=True))
        self.stack.enter_context(patch.object(self.agent.route_model_client, '_create_chat_model', return_value=object()))
        self.route = self.stack.enter_context(patch.object(self.agent.route_model_client, '_invoke_chain', return_value=json.dumps(
            decision(['promotion_stacking'], capability_ids=['stock_notification']))))
        self.stack.enter_context(patch.object(self.agent.langchain_tool_runner, 'can_call_model', return_value=False))
        self.capability = self.stack.enter_context(patch('agent.customer_service_agent.query_service_capability', side_effect=catalog_payload))
        self.policy = self.stack.enter_context(patch('agent.customer_service_agent.query_interaction_policy', return_value={
            'source': 'controlled_http', 'schemaVersion': 'interaction_policy_v1', 'version': 'fixture', 'entries': []}))
        self.stack.enter_context(patch.object(self.agent.answer_model_client, 'can_call_model', return_value=True))
        self.stack.enter_context(patch.object(self.agent.answer_model_client, '_create_chat_model', return_value=object()))
        self.generate = self.stack.enter_context(patch.object(self.agent.answer_model_client, '_invoke_chain',
            return_value=ModelInvocationResult(content='固定模型答复')))
        self.judge = self.stack.enter_context(patch.object(self.agent.answer_judge_client, 'judge', return_value=judgment()))

    def chat(self, text='露营季满减和会员券能否一起使用，支持到货提醒吗？', history=None):
        return self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
            user_message=text, history_messages=history or []))

    def test_route_to_generation_and_review_share_selection(self):
        response = self.chat()
        self.assertEqual(response.answer, '固定模型答复')
        payload = json.loads(self.generate.call_args.args[1])
        self.assertEqual(payload, self.judge.call_args.args[0])
        self.assertNotIn('退款不自动代表', json.dumps(payload, ensure_ascii=False))
        self.assertIn('原请求', payload['required_points_source'])
        self.assertIn('同类型满减券', payload['verified_summary'])
        self.assertEqual(self.route.call_count, 1)
        self.assertIn('inquiry_topics', self.route.call_args.args[2])
        self.assertTrue(response.citations)

    def test_judge_fault_and_generation_fault_use_same_fallback(self):
        self.judge.return_value = AnswerJudgeResult(status='error', decision='fallback', reason='timeout')
        a = self.chat().answer
        self.generate.side_effect = TimeoutError('injected')
        b = self.chat().answer
        self.assertEqual(a, b)
        self.assertEqual(a.count('可以与平台会员券叠加'), 1)
        self.assertIn('不支持到货', a); self.assertNotIn('退款不自动代表', a)
        self.assertEqual(self.judge.call_count, 1)

    def test_one_repair_keeps_payload_and_queries_once(self):
        self.judge.side_effect = [judgment('fail'), judgment()]
        self.generate.side_effect = [ModelInvocationResult(content='已登记'), ModelInvocationResult(content='修复答复')]
        response = self.chat()
        self.assertEqual(response.answer, '修复答复')
        a, b = [json.loads(call.args[1]) for call in self.generate.call_args_list]
        b.pop('repair_feedback')
        self.assertEqual(a, b)
        self.assertEqual(self.judge.call_count, 2)
        self.capability.assert_called_once(); self.policy.assert_called_once(); self.route.assert_called_once()

    def test_missing_topics_preserve_capability_and_pending_draft(self):
        payload = decision([], capability_ids=['stock_notification']); payload.pop('inquiry_topics')
        self.route.return_value = json.dumps(payload)
        self.generate.side_effect = TimeoutError('injected')
        draft = {'intent': 'refund_request', 'pending_slot': 'order_id'}
        current_memory('content-pipeline', 'U1001')['open_task'] = copy.deepcopy(draft)
        response = self.chat()
        self.assertIn('不支持到货', response.answer); self.assertIn('尚未明确', response.answer)
        self.assertNotIn('300', response.answer)
        self.assertEqual(current_memory('content-pipeline', 'U1001')['open_task'], draft)

    def test_short_followup_gets_history_and_replaces_old_topic(self):
        self.route.return_value = json.dumps(decision(['refund_arrival'], 'refund_policy_query'))
        self.generate.side_effect = TimeoutError('injected')
        response = self.chat('那多久到账？', [HistoryMessage(role='user', content='我想了解退款规则')])
        self.assertIn('我想了解退款规则', self.route.call_args.args[2])
        self.assertIn('到账时限', response.answer)
        self.route.return_value = json.dumps(decision(['invoice_issue'], 'invoice_query'))
        response = self.chat('发票在哪里下载？')
        self.assertIn('24 小时', response.answer); self.assertNotIn('退款', response.answer)

    def test_off_and_shadow_keep_judge_semantics_with_selected_content(self):
        self.generate.return_value = ModelInvocationResult(content='当前客服不支持到货或补货提醒。固定模型答复')
        for mode in ('off', 'shadow'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'ANSWER_JUDGE_MODE': mode}):
                self.judge.reset_mock()
                self.judge.return_value = AnswerJudgeResult(status='error', decision='fallback', reason='timeout')
                response = self.chat()
                self.assertEqual(response.answer, self.generate.return_value.content)
                payload = json.loads(self.generate.call_args.args[1])
                self.assertTrue(payload['content_selection']['selected_rules'])
                self.assertEqual(self.judge.call_count, int(mode == 'shadow'))

    def test_cross_category_order_and_conditions_follow_route_order(self):
        self.route.return_value = json.dumps(decision(['promotion_stacking'], capability_ids=['stock_notification'],
            answer_order=['capability:stock_notification', 'topic:promotion_stacking', 'topic:unqueried']))
        self.generate.side_effect = TimeoutError('injected')
        response = self.chat('支持到货提醒吗，露营季满减和会员券能否一起用？')
        self.assertLess(response.answer.index('不支持到货'), response.answer.index('满 300 减 40'))
        self.assertIn('开始和截止时间', response.answer)
        self.assertNotIn('topic:unqueried', response.session_state['route_plan']['answer_order'])
        self.assertEqual(self.route.call_count, 1)

    def test_scope_and_promotion_keep_both_demands_in_either_order(self):
        # BC-031/N02 的原路由已识别主题；这里只回放该结构，不重新评分模型识别。
        for text, order in (
            ('先问一下，售罄以后能给我个信儿吗？再问露营季满减和平台会员券是不是能一起用？',
             ['capability:stock_notification', 'topic:promotion_stacking']),
            ('露营季满减和平台会员券是不是能一起用？售罄以后能给我个信儿吗？',
             ['topic:promotion_stacking', 'capability:stock_notification'])):
            with self.subTest(text=text):
                self.route.return_value = json.dumps(decision(['promotion_stacking'], 'service_scope_query',
                    capability_ids=['stock_notification'], answer_order=order))
                self.generate.side_effect = TimeoutError('injected')
                response = self.chat(text)
                plan = response.session_state['route_plan']
                self.assertEqual(plan['topic_status'], 'valid')
                self.assertEqual(plan['answer_order'], order)
                self.assertEqual(plan['knowledge_domains'], ['promotion_and_member_policy'])
                self.assertIn('同类型满减券', response.answer)
                self.assertIn('开始和截止时间', response.answer)
                self.assertIn('不支持到货', response.answer)
                capability_first = response.answer.index('不支持到货') < response.answer.index('满 300 减 40')
                self.assertEqual(capability_first, order[0].startswith('capability:'))
                self.assertEqual(response.tool_calls, [])
                self.assertIsNone(response.session_state['workflow'])
        self.assertEqual(self.route.call_count, 2)
        self.assertEqual(self.capability.call_count, 2)

    def test_scope_consultation_retrieves_each_planned_domain(self):
        for topic, text, expected in (
            ('tent_care', '支持到货提醒吗？帐篷怎么清洁收纳？', '不使用洗衣机'),
            ('refund_arrival', '支持到货提醒吗？未发货退款多久到账？', '到账时限保证'),
            ('return_shipping_cost', '支持到货提醒吗？签收退货寄回运费谁出？', '到付'),
            ('invoice_issue', '支持到货提醒吗？发票多久开好？', '24 小时')):
            with self.subTest(topic=topic):
                self.route.return_value = json.dumps(decision([topic], 'service_scope_query',
                    capability_ids=['stock_notification']))
                self.generate.side_effect = TimeoutError('injected')
                response = self.chat(text)
                self.assertIn(expected, response.answer)
                self.assertIn('不支持到货', response.answer)
                self.assertEqual(response.session_state['route_plan']['inquiry_topics'], [topic])
                self.assertEqual(list(response.session_state['evidence_retrievals']),
                    [rules.topic_catalog()[topic]])
                self.assertTrue(response.citations)
                self.assertEqual(response.tool_calls, [])
                self.assertIsNone(response.session_state['workflow'])
                self.assertIsNone(response.session_state['memory']['product_query'])

    def test_cross_domain_consultation_answers_all_topics(self):
        # MULTI-READ-001：跨域组合（发票＋退款到账）不再丢主题，最终答复逐项覆盖。
        for intent, topics, text, expected in (
            ('invoice_query', ['invoice_issue', 'refund_arrival'],
             '发票一般多久开好？未发货退款多久到账？', ('24 小时', '到账时限保证')),
            ('refund_policy_query', ['refund_arrival', 'invoice_issue'],
             '未发货退款多久到账？发票一般多久开好？', ('到账时限保证', '24 小时')),
            ('promotion_query', ['promotion_stacking', 'refund_arrival'],
             '露营季满减和会员券能否叠加？退款多久到账？', ('满 300 减 40', '到账时限保证'))):
            with self.subTest(intent=intent):
                extra = {'answer_order': ['topic:' + t for t in topics]}
                self.route.return_value = json.dumps(decision(topics, intent, **extra))
                self.generate.side_effect = TimeoutError('injected')
                response = self.chat(text)
                plan = response.session_state['route_plan']
                self.assertEqual(plan['topic_status'], 'valid')
                self.assertEqual(plan['inquiry_topics'], topics)
                for item in expected:
                    self.assertIn(item, response.answer)
                self.assertEqual(response.tool_calls, [])
                self.assertIsNone(response.session_state['workflow'])

    def test_policy_consultation_preserves_full_draft_across_answer_exits(self):
        draft = {'intent': 'refund_request', 'pending_slot': 'order_id',
                 'candidates': [{'value': 'ORD9801', 'label': '第一笔'}, {'value': 'ORD9802', 'label': '第二笔'}]}
        for intent, topics, text in (
            ('promotion_query', ['coupon_conditions', 'coupon_status'],
             '朋友把山友会员券截图发给我，我能拿来用吗？这张券还没到使用时间。'),
            ('refund_policy_query', ['refund_arrival'], '只咨询退款到账时间，不申请。'),
            ('return_policy_query', ['return_shipping_cost'], '只问寄回运费谁出，不办理。'),
            ('invoice_query', ['invoice_issue'], '发票多久开好，在哪里下载？')):
            for exit_path in ('normal', 'repair', 'fallback'):
                with self.subTest(intent=intent, exit_path=exit_path):
                    memory = current_memory('content-pipeline', 'U1001')
                    memory['open_task'] = copy.deepcopy(draft)
                    self.route.return_value = json.dumps(decision(topics, intent))
                    self.generate.side_effect = (TimeoutError('injected') if exit_path == 'fallback' else
                        [ModelInvocationResult(content='固定模型答复')] * 2)
                    self.judge.side_effect = [judgment('fail'), judgment()] if exit_path == 'repair' else None
                    response = self.chat(text)
                    self.assertEqual(memory['open_task'], draft)
                    self.assertEqual(response.session_state['memory']['open_task'], draft)
                    self.assertEqual(response.tool_calls, [])
                    self.assertIsNone(response.session_state['workflow'])
                    self.assertIsNone(response.clarification)

    def test_gateway_cancel_is_not_undone_by_policy_consultation(self):
        memory = current_memory('content-pipeline', 'U1001')
        memory['open_task'] = {'intent': 'refund_request', 'pending_slot': 'order_id', 'candidates': []}
        self.route.return_value = json.dumps(decision(['coupon_conditions']))
        response = self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
            user_message='优惠券要满足什么条件？', runtime_context={'cancelledAfterSaleDraft': True}))
        self.assertIsNone(response.session_state['memory']['open_task'])

    def test_no_evidence_consultation_keeps_draft_and_resume_still_binds_candidate(self):
        draft = {'intent': 'refund_request', 'pending_slot': 'order_id',
                 'candidates': [{'value': 'ORD9801', 'label': '第一笔'}, {'value': 'ORD9802', 'label': '第二笔'}]}
        memory = current_memory('content-pipeline', 'U1001')
        memory['open_task'] = copy.deepcopy(draft)
        self.route.return_value = json.dumps(decision(['promotion_stacking']))
        self.chat('未知活动能和会员券叠加吗？')
        self.assertEqual(memory['open_task'], draft)
        self.route.return_value = json.dumps(decision([], 'refund_request'))
        response = self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
            user_message='第二笔', runtime_context={'mallAfterSaleRouting': True, 'afterSaleOpenTask': draft}))
        self.assertEqual(response.session_state['intent'], 'refund_request')
        self.assertEqual(response.session_state['context_report']['chosen_order_id'], 'ORD9802')
        self.assertIsNone(response.session_state['workflow'])

    def test_direct_skip_uses_rules_without_judge(self):
        plan = build_route_plan(intent='promotion_query', user_message='优惠叠加', order_id=None,
            model_used=True, inquiry_topics=['promotion_stacking'], topic_status='valid', capability_ids=[])
        result = self.agent._compose_final_answer(request=ChatRequest(session_id='direct',runtime_user_id='U1001',user_message='优惠叠加'),
            intent='promotion_query', answer='未经选择的整段退券正文', risk_level='low', next_action='answer_user',
            tool_calls=[], citations=[docs.load_knowledge_citation('promotion_policy.md')], workflow=None,
            cache_hit=False,degraded=False,skip_final_model=True,context_report={'model_context':[]},capability_results=[],route_plan=plan)
        self.assertNotIn('退券正文', result.answer)
        self.assertIn('300', result.answer)
        self.generate.assert_not_called(); self.judge.assert_not_called()
        self.assertEqual(result.answer_judge['decision'], 'not_reviewed')
