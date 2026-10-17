"""BC-032：固定路由、隔离业务事实，验证任务生命周期与澄清焦点。"""
import copy
import json
import unittest
from unittest.mock import patch

from agent.context import current_memory
from agent.models.router_client import ModelRouteResult, RouteModelClient
from api.schemas import ChatRequest, ToolCallTrace
from tests import test_answer_content as fixtures
from tests.test_answer_content import decision


class TaskLifecycleTest(unittest.TestCase):
    setUp = fixtures.ContentPipelineTest.setUp
    chat = fixtures.ContentPipelineTest.chat

    def seed_draft(self):
        draft = {'intent': 'refund_request', 'pending_slot': 'order_id',
                 'candidates': [{'value': 'ORD9801', 'label': '第一笔'},
                                {'value': 'ORD9802', 'label': '第二笔'}], 'version': 3}
        current_memory('content-pipeline', 'U1001')['open_task'] = copy.deepcopy(draft)
        return draft

    def route_as(self, intent, **extra):
        self.route.return_value = json.dumps(decision([], intent, capability_ids=[], **extra))

    def test_unrelated_turns_preserve_draft_and_read_only_semantics(self):
        for intent, text in [('order_query', '先查一下物流到哪了，不办理退款。'),
                             ('refund_status_query', '我先查售后进度。'),
                             ('general_chat', '谢谢，我再想想。'),
                             ('unknown', '我还没想清楚。')]:
            with self.subTest(intent=intent):
                draft = self.seed_draft()
                self.route_as(intent)
                response = self.chat(text)
                self.assertEqual(response.session_state['memory']['open_task'], draft)
                self.assertTrue(response.session_state['read_only_turn'])
                self.assertIsNone(response.session_state['workflow'])

    def test_query_candidates_and_refund_candidates_are_independent(self):
        draft = self.seed_draft()
        orders = [{'orderNo': f'ORD981{i}', 'userId': 'U1001', 'items': []} for i in (1, 2)]
        self.route_as('order_query')
        response = self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
            user_message='先查物流', runtime_context={'currentUserOrders': orders}))
        memory = current_memory('content-pipeline', 'U1001')
        self.assertEqual(memory['open_task'], draft)
        self.assertEqual([c['value'] for c in memory['order_query']['candidates']], ['ORD9811', 'ORD9812'])
        self.assertEqual(memory['clarification_focus'], 'order_query')
        self.route_as('general_chat')
        self.chat('谢谢，我再想想。')
        self.assertEqual(memory['open_task'], draft)
        # 模型故障的纯序号也只补当前查询，不能因存在退款待办而误续办。
        with patch.object(self.agent.route_model_client, 'plan_intent',
                          return_value=ModelRouteResult(intent='unknown', used_model=False)), \
             patch('agent.customer_service_agent.get_order_detail', return_value=(None,
                 ToolCallTrace(
                     tool_name='get_order_detail', arguments={}, output_summary='隔离查询失败', status='error',
                     error_type='business_api_unavailable'))):
            selected = self.chat('第二笔')
        self.assertEqual(selected.session_state['intent'], 'order_query')
        self.assertEqual(selected.session_state['context_report']['chosen_order_id'], 'ORD9812')
        self.assertEqual(memory['open_task'], draft)

    def test_explicit_resume_uses_original_snapshot_over_query_and_page(self):
        draft = self.seed_draft()
        memory = current_memory('content-pipeline', 'U1001')
        memory['order_query'] = {'intent': 'order_query', 'pending_slot': 'order_id',
                                 'candidates': [{'value': 'ORD9811', 'label': '查询第一笔'}]}
        memory['clarification_focus'] = 'order_query'
        memory['eligibility_query'] = {'intent': 'after_sale_eligibility_query', 'order_id': 'ORD9899', 'pending_slot': None}
        self.route_as('refund_request', task_relation='resume')
        response = self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
            user_message='继续刚才退款，选第二笔', runtime_context={'mallAfterSaleRouting': True,
                'afterSaleOpenTask': draft, 'afterSaleQuery': memory['eligibility_query'], 'relatedOrderNo': 'ORD9999'}))
        self.assertEqual(response.session_state['context_report']['chosen_order_id'], 'ORD9802')
        self.assertFalse(response.session_state['read_only_turn'])
        self.assertIsNone(response.session_state['workflow'])

    def test_control_proposal_is_not_a_cancellation_receipt(self):
        for relation in ('stop', 'switch'):
            with self.subTest(relation=relation):
                draft = self.seed_draft()
                self.route_as('order_query', task_relation=relation)
                response = self.chat('先不要退款，查物流。')
                self.assertEqual(response.session_state['memory']['open_task'], draft)
                self.assertTrue(response.session_state['task_control_requested'])
                self.assertFalse(response.session_state['read_only_turn'])

    def test_order_number_reply_uses_query_focus_before_background_refund(self):
        draft = self.seed_draft()
        memory = current_memory('content-pipeline', 'U1001')
        memory['order_query'] = {'intent': 'order_query', 'pending_slot': 'order_id', 'candidates': []}
        memory['clarification_focus'] = 'order_query'
        self.route_as('refund_request', task_relation='resume')
        response = self.chat('ORD9812')
        self.assertEqual(response.session_state['intent'], 'order_query')
        self.assertEqual(response.session_state['context_report']['chosen_order_id'], 'ORD9812')
        self.assertEqual(memory['open_task'], draft)
        self.assertTrue(response.session_state['read_only_turn'])
        self.assertIsNone(response.session_state['workflow'])

    def test_resume_without_selection_does_not_choose_page_or_recent_order(self):
        draft = self.seed_draft()
        memory = current_memory('content-pipeline', 'U1001')
        memory['last_order_id'] = 'ORD9899'
        self.route_as('refund_request', task_relation='resume')
        for page in ({'relatedOrderNo': 'ORD9999'}, {}):
            with self.subTest(page=page):
                response = self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
                    user_message='继续刚才退款', runtime_context={'mallAfterSaleRouting': True,
                        'afterSaleOpenTask': draft, **page}))
                report = response.session_state['context_report']
                self.assertIsNone(report['chosen_order_id'])
                self.assertFalse(any('[order_reference/session]' in item for item in report['model_context']))
                self.assertFalse(any('采用页面订单' in item for item in report['conflict_resolutions']))
                self.assertEqual([c.value for c in response.clarification.candidates], ['ORD9801', 'ORD9802'])
                self.assertIn('ORD9801', response.answer)
                self.assertIn('ORD9802', response.answer)
                self.assertEqual(memory['open_task'], draft)

    def test_visible_candidates_then_second_choice_binds_original_refund(self):
        draft = self.seed_draft()
        self.route_as('refund_request', task_relation='resume')
        first = self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
            user_message='继续刚才退款', runtime_context={'mallAfterSaleRouting': True,
                'afterSaleOpenTask': draft}))
        self.assertIn('ORD9801', first.answer)
        self.assertIn('ORD9802', first.answer)
        second = self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
            user_message='第二笔', runtime_context={'mallAfterSaleRouting': True,
                'afterSaleOpenTask': {'intent': 'refund_request', 'pending_slot': 'order_id'}}))
        self.assertEqual(second.session_state['context_report']['chosen_order_id'], 'ORD9802')

    def test_invalid_selection_keeps_original_task_version_and_candidates(self):
        draft = self.seed_draft()
        self.route_as('refund_request', task_relation='resume')
        response = self.agent.chat(ChatRequest(session_id='content-pipeline', runtime_user_id='U1001',
            user_message='继续刚才退款，第三笔', runtime_context={'mallAfterSaleRouting': True,
                'afterSaleOpenTask': draft, 'relatedOrderNo': 'ORD9999',
                'currentUserOrders': [{'orderNo': 'ORD9899', 'userId': 'U1001', 'items': []}]}))
        self.assertIsNone(response.session_state['context_report']['chosen_order_id'])
        self.assertEqual(response.session_state['memory']['open_task'], draft)
        self.assertEqual([c.value for c in response.clarification.candidates], ['ORD9801', 'ORD9802'])

    def test_query_failure_does_not_finish_after_sale_task(self):
        draft = self.seed_draft()
        self.route_as('order_query')
        self.generate.side_effect = TimeoutError('injected')
        response = self.chat('查一下物流')
        self.assertEqual(response.session_state['memory']['open_task'], draft)

    def test_unconfirmed_or_conflicting_route_cannot_replace_after_sale_task(self):
        for route in (ModelRouteResult(intent='refund_request', used_model=False),
                      ModelRouteResult(intent='refund_request', used_model=True, task_relation='unrelated'),
                      ModelRouteResult(intent='return_request', used_model=True, task_relation='uncertain')):
            with self.subTest(route=route):
                draft = self.seed_draft()
                with patch.object(self.agent.route_model_client, 'plan_intent', return_value=route):
                    response = self.chat('这轮先聊别的，我还没决定。')
                self.assertEqual(response.session_state['memory']['open_task'], draft)
                self.assertIsNone(response.session_state['workflow'])
                self.assertTrue(response.session_state['read_only_turn'])

    def test_legacy_order_query_moves_to_its_own_context(self):
        memory = current_memory('content-pipeline', 'U1001')
        legacy = {'intent': 'order_query', 'pending_slot': 'order_id',
                  'candidates': [{'value': 'ORD9811', 'label': '查询订单'}]}
        memory['open_task'] = copy.deepcopy(legacy)
        current_memory('content-pipeline', 'U1001')
        self.assertIsNone(memory['open_task'])
        self.assertEqual(memory['order_query'], legacy)
        self.assertEqual(memory['clarification_focus'], 'order_query')

    def test_unrelated_turn_preserves_other_consultation_states(self):
        memory = current_memory('content-pipeline', 'U1001')
        product = {'pending_candidates': [{'id': 4, 'name': '帐篷'}], 'original_question': '帐篷多少钱'}
        policy = {'question': '会员券能叠加吗？'}
        memory['product_query'] = copy.deepcopy(product)
        memory['promotion_query'] = copy.deepcopy(policy)
        self.route_as('invoice_query')
        self.chat('发票在哪里下载？')
        self.assertEqual(memory['product_query'], product)
        self.assertEqual(memory['promotion_query'], policy)

    def test_task_relation_contract_is_optional_and_invalid_relation_does_not_grant_control(self):
        for relation in ('resume', 'stop', 'switch', 'unrelated', 'uncertain'):
            with self.subTest(relation=relation):
                parsed = RouteModelClient._extract_decision(json.dumps(decision([], 'general_chat', task_relation=relation)))
                self.assertEqual(parsed['task_relation'], relation)
        for invalid in ('delete_all', {}, [], False, 42):
            parsed = RouteModelClient._extract_decision(json.dumps(decision([], 'general_chat', task_relation=invalid)))
            self.assertEqual(parsed['intent'], 'general_chat')
            self.assertIsNone(parsed.get('task_relation'))

