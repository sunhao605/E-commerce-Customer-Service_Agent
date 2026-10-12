"""商城已取消的草稿不能通过旧候选续办；不清除已核验订单事实。"""
import unittest
from unittest.mock import patch

from api.schemas import ChatRequest
from agent.context import SESSION_MEMORIES, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.models.router_client import ModelRouteResult


class CancelledDraftTest(unittest.TestCase):
    def setUp(self):
        SESSION_MEMORIES.clear()
        self.memory = current_memory('cancel-test', 'u1')
        self.memory['last_order_id'] = 'ORD9802'
        self.memory['open_task'] = dict(intent='refund_request', pending_slot='order_id',
            candidates=[dict(value='ORD9801', label='ORD9801')])
        self.agent = Lesson41Agent()

    def test_gateway_cancellation_removes_old_candidates_before_routing(self):
        request = ChatRequest(session_id='cancel-test', runtime_user_id='u1', user_message='第一笔。',
            runtime_context={'cancelledAfterSaleDraft': True})
        with patch.object(self.agent.route_model_client, 'plan_intent',
            return_value=ModelRouteResult(intent='unknown', used_model=True, model_name='test')) as route:
            response = self.agent.chat(request)
        self.assertIsNone(route.call_args.kwargs['task_note'])
        self.assertIsNone(response.session_state['context_report']['chosen_order_id'])
        self.assertEqual(response.tool_calls, [])
        self.assertIsNone(self.memory['open_task'])
        self.assertEqual(self.memory['last_order_id'], 'ORD9802')

    def test_without_cancellation_keeps_the_pending_selection(self):
        request = ChatRequest(session_id='cancel-test', runtime_user_id='u1', user_message='第一笔。')
        with patch.object(self.agent.route_model_client, 'plan_intent',
            return_value=ModelRouteResult(intent='unknown', used_model=True, model_name='test')) as route:
            response = self.agent.chat(request)
        self.assertIn('ORD9801', route.call_args.kwargs['task_note'])
        self.assertEqual(response.session_state['context_report']['chosen_order_id'], 'ORD9801')

    def test_cancellation_does_not_prevent_a_new_refund_request(self):
        request = ChatRequest(session_id='cancel-test', runtime_user_id='u1', user_message='帮我退款。',
            runtime_context={'cancelledAfterSaleDraft': True})
        with patch.object(self.agent.route_model_client, 'plan_intent',
            return_value=ModelRouteResult(intent='refund_request', used_model=True, model_name='test')) as route:
            self.agent.chat(request)
        self.assertIsNone(route.call_args.kwargs['task_note'])
        self.assertEqual(current_memory('cancel-test', 'u1')['recent_intent'], 'refund_request')
