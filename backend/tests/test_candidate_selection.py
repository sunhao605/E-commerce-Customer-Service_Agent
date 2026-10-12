"""候选序号必须绑定已展示的本人会话快照，不能回落到页面或旧订单。"""
import unittest
from unittest.mock import patch

from api.schemas import ChatRequest
from agent.context import SESSION_MEMORIES, build_context, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.models.router_client import ModelRouteResult


class CandidateSelectionTest(unittest.TestCase):
    def setUp(self):
        SESSION_MEMORIES.clear()
        self.orders = [dict(orderNo=f'ORD980{i}', userId='u1', paymentStatus='PAID',
                            fulfillmentStatus='UNSHIPPED', items=[dict(productName='验收露营椅')]) for i in (1, 2)]

    def request(self, message, **kwargs):
        return ChatRequest(session_id=kwargs.pop('session_id', 'candidate-test'), runtime_user_id=kwargs.pop('runtime_user_id', 'u1'),
                           user_message=message, **kwargs)

    def show_candidates(self):
        current_memory('candidate-test', 'u1')['open_task'] = dict(intent='refund_request', pending_slot='order_id',
            candidates=[dict(value=order['orderNo'], label=order['orderNo']) for order in self.orders])

    def test_selection_uses_shown_order_even_when_current_list_is_reversed(self):
        self.show_candidates()
        current_memory('candidate-test', 'u1')['last_order_id'] = 'ORD9999'
        for message, expected in [('第一笔。', 'ORD9801'), ('第二笔', 'ORD9802'), ('第2笔', 'ORD9802')]:
            with self.subTest(message=message):
                request = self.request(message, runtime_context={'relatedOrderNo': 'ORD9998', 'currentUserOrders': self.orders[::-1]})
                self.assertEqual(build_context(request, None)[0], expected)

    def test_missing_invalid_and_other_session_never_fall_back_to_old_order(self):
        self.show_candidates()
        for message, extra in [('第三笔', {}), ('第0笔', {}), ('第一笔', {'session_id': 'other'}),
                               ('第一笔', {'runtime_user_id': 'u2'})]:
            with self.subTest(message=message, extra=extra):
                request = self.request(message, runtime_context={'relatedOrderNo': 'ORD9998'}, **extra)
                self.assertIsNone(build_context(request, None)[0])

    def test_dialogue_binds_tool_workflow_and_memory_to_first_order(self):
        agent = Lesson41Agent()
        with patch.object(agent.route_model_client, 'plan_intent', return_value=ModelRouteResult(intent='refund_request', used_model=True, model_name='test')):
            first = agent.chat(self.request('帮我退款。', runtime_context={'currentUserOrders': self.orders}))
            self.assertEqual([c.value for c in first.clarification.candidates], ['ORD9801', 'ORD9802'])
            second = agent.chat(self.request('第一笔。', runtime_context={'currentUserOrders': self.orders[::-1]}))
        self.assertIsNone(second.clarification)
        self.assertEqual(second.tool_calls[0].arguments['order_id'], 'ORD9801')
        self.assertEqual(second.session_state['workflow']['order_id'], 'ORD9801')
        self.assertEqual(second.session_state['memory']['last_order_id'], 'ORD9801')

    def test_gateway_page_text_cannot_override_the_original_selection(self):
        self.show_candidates()
        agent = Lesson41Agent()
        with patch.object(agent.route_model_client, 'plan_intent', return_value=ModelRouteResult(intent='refund_request', used_model=False)):
            response = agent.chat(self.request('第一笔。\n\n[荒野户外商城页面上下文] 关联订单号=ORD9802;',
                runtime_context={'userMessage': '第一笔。', 'relatedOrderNo': 'ORD9802', 'currentUserOrders': self.orders}))
        self.assertEqual(response.tool_calls[0].arguments['order_id'], 'ORD9801')
        self.assertEqual(response.session_state['model']['route_planner']['fallback_reason'], 'open_task_slot')

    def test_explicit_switch_keeps_model_intent_and_explicit_order(self):
        self.show_candidates()
        agent = Lesson41Agent()
        with patch.object(agent.route_model_client, 'plan_intent', return_value=ModelRouteResult(intent='order_query', used_model=True, model_name='test')):
            response = agent.chat(self.request('先不要退款，查 ORD9802 的物流。', runtime_context={'currentUserOrders': self.orders}))
        self.assertEqual(response.session_state['intent'], 'order_query')
        self.assertEqual(response.tool_calls[0].arguments['order_id'], 'ORD9802')
        self.assertIsNone(response.session_state.get('workflow'))

    def test_selected_order_still_requires_current_ownership(self):
        self.show_candidates()
        self.orders[0]['userId'] = 'u2'
        agent = Lesson41Agent()
        with patch.object(agent.route_model_client, 'plan_intent', return_value=ModelRouteResult(intent='refund_request', used_model=True, model_name='test')):
            response = agent.chat(self.request('第一笔。', runtime_context={'currentUserOrders': self.orders}))
        self.assertEqual(response.tool_calls[0].error_type, 'owner_mismatch')
        self.assertIsNone(response.session_state.get('workflow'))
        self.assertIsNone(response.session_state['memory']['last_order_id'])

    def test_selected_order_uses_current_fulfillment_state(self):
        self.show_candidates()
        self.orders[0]['fulfillmentStatus'] = 'SHIPPED'
        agent = Lesson41Agent()
        with patch.object(agent.route_model_client, 'plan_intent', return_value=ModelRouteResult(intent='refund_request', used_model=True, model_name='test')):
            response = agent.chat(self.request('第一笔。', runtime_context={'currentUserOrders': self.orders}))
        self.assertEqual(response.tool_calls[0].arguments['order_id'], 'ORD9801')
        self.assertNotEqual((response.session_state.get('workflow') or {}).get('pending_action'), 'require_approval')
