"""退款资格、业务进度和独立模拟审核的回归；不调用模型或业务 HTTP。"""
import os
import unittest
from unittest.mock import patch

from api.schemas import ChatRequest, ChatResumeRequest
from agent.customer_service_agent import Lesson41Agent
from agent.state import WORKFLOW_CHECKPOINTS, SUBMITTED_ACTIONS
from agent.workflows.resume import SIMULATED_REVIEWER


class RefundSubmissionBoundaryTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, AGENT_COURSE_DISABLE_LLM='1', AGENT_COURSE_OFFLINE_RAG='1',
                                 AGENT_COURSE_OFFLINE_FACTS='0', AGENT_COURSE_ENV='/tmp/huangye-refund-test.env')
        environment.start()
        self.addCleanup(environment.stop)
        self.agent = Lesson41Agent()
        self.order = {'orderNo': 'ORD9401', 'userId': 'U1001', 'status': 'PAID_PENDING_SHIPMENT',
                      'paymentStatus': 'PAID', 'fulfillmentStatus': 'UNSHIPPED', 'totalAmount': 200,
                      'items': [{'productName': '合成露营椅', 'quantity': 1, 'returnable': True}]}

    def chat(self, session, message):
        return self.agent.chat(ChatRequest(session_id=session, runtime_user_id='U1001', user_message=message,
            runtime_context={'userId': 'U1001', 'currentUserOrders': [self.order]}))

    def test_missing_payment_stops_refund_workflow(self):
        self.order.pop('paymentStatus')
        with patch('httpx.Client.request', side_effect=AssertionError('business network forbidden')) as network:
            response = self.chat('refund-no-payment', 'ORD9401 我要退款')
        self.assertIsNone(response.session_state['workflow'])
        self.assertFalse(network.called)
        self.assertFalse(any(key[0] == 'refund-no-payment' for key in WORKFLOW_CHECKPOINTS))

    def test_progress_reads_existing_business_status(self):
        for status, label in [('PENDING_REVIEW', '待审核'), ('NEED_MORE_INFO', '待补充材料')]:
            with patch('httpx.Client.request', side_effect=AssertionError('business network forbidden')), \
                 patch('agent.tools.tool_runtime.after_sale_requests_from_ecommerce', return_value=[
                     {'requestId': 'AS-existing', 'requestType': 'REFUND', 'status': status}]):
                response = self.chat('refund-progress-' + status, 'ORD9401 退款进度怎么样？')
            self.assertIn('AS-existing', response.answer)
            self.assertIn(label, response.answer)
            self.assertIsNone(response.session_state['workflow'])

    def test_workbench_approval_is_independent_simulation(self):
        with patch('httpx.Client.request', side_effect=AssertionError('business network forbidden')) as network, \
             patch('agent.workflows.resume.order_fact_from_ecommerce', return_value=self.order):
            response = self.chat('refund-simulation', 'ORD9401 我要退款')
            workflow = response.session_state['workflow']
            resumed = self.agent.resume(ChatResumeRequest(session_id='refund-simulation',
                workflow_id=workflow['workflow_id'], resume_token=workflow['resume_token'],
                reviewer_id='course-reviewer-001', reviewer_role='after_sale_manager', decision='approved'),
                SIMULATED_REVIEWER)
        self.assertEqual(resumed.status, 'completed')
        self.assertIn('模拟', resumed.answer)
        self.assertIn('不执行真实资金退款', resumed.answer)
        self.assertIn(workflow['idempotency_key'], SUBMITTED_ACTIONS)
        self.assertFalse(network.called)

    def test_eligible_seven_day_return_stops_before_submission(self):
        self.order.update(status='DELIVERED', fulfillmentStatus='DELIVERED', deliveredAt='2026-07-14T10:00:00', returnable=True)
        with patch('httpx.Client.request', side_effect=AssertionError('business network forbidden')):
            response = self.chat('return-still-consultation', 'ORD9401 已签收，七天无理由退货')
        self.assertNotEqual(response.session_state['workflow']['pending_action'], 'require_approval')
        self.assertFalse(any(key[0] == 'return-still-consultation' for key in WORKFLOW_CHECKPOINTS))

    def test_no_reason_return_prepares_without_inventing_quality_facts(self):
        self.order.update(status='DELIVERED', fulfillmentStatus='DELIVERED', deliveredAt='2026-07-14T10:00:00', returnable=True)
        with patch('httpx.Client.request', side_effect=AssertionError('business network forbidden')):
            for phrase in ('我要退货', '七天无理由退款'):
                response = self.chat('return-no-reason-' + phrase, 'ORD9401 ' + phrase)
                workflow = response.session_state['workflow']
                self.assertEqual(workflow['pending_action'], 'prepare_return_application')
                self.assertEqual(workflow['return_reason'], '七天无理由')
                self.assertIn('验收通过才退款', response.answer)
            other = self.chat('return-quality', 'ORD9401 商品破损，我要退货')
        self.assertEqual(other.session_state['workflow']['eligibility_reason'], 'other_after_sale_type')
        self.assertEqual(other.session_state['workflow']['return_reason'], '商品破损')
        self.assertFalse(other.session_state['workflow']['needs_human_approval'])

    def test_return_progress_reads_each_real_stage_without_new_workflow(self):
        labels = [('WAITING_RETURN', '待买家寄回'), ('RETURN_IN_TRANSIT', '待商家收货验收'),
                  ('RETURN_INSPECTION_FAILED', '验收未通过'), ('REFUNDED', '已退款')]
        for status, label in labels:
            with patch('httpx.Client.request', side_effect=AssertionError('business network forbidden')), \
                 patch('agent.tools.tool_runtime.after_sale_requests_from_ecommerce', return_value=[
                     {'requestId': 'AS-return', 'requestType': 'RETURN', 'status': status}]):
                response = self.chat('return-progress-' + status, 'ORD9401 退货进度怎么样？')
            self.assertIn('AS-return', response.answer)
            self.assertIn(label, response.answer)
            self.assertIsNone(response.session_state['workflow'])


if __name__ == '__main__':
    unittest.main()
