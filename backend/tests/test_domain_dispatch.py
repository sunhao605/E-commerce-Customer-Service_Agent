"""DOMAIN-DISPATCH-001：固定路由和离线知识，核对组件、来源及三个出口。"""
import copy
import itertools
import json
import unittest
from unittest.mock import patch

from agent.context import current_memory
from api.schemas import ChatRequest
from tests import test_answer_content as fixtures
from tests.test_answer_content import decision, judgment
from retrieval import knowledge
from agent.models.answer_client import ModelInvocationResult
from retrieval.documents import get_knowledge_snapshot
from retrieval.documents import load_knowledge_citation

DOMAINS = {
    'after_sale_policy': ('refund_policy_query', 'refund_arrival', '退款多久到账？', 'policy_query_result', {'after_sale_refund'}),
    'received_return_policy': ('return_policy_query', 'return_conditions', '签收后的七天无理由退货有哪些条件？', 'policy_query_result', {'after_sale_return'}),
    'invoice': ('invoice_query', 'invoice_issue', '电子发票多久开好？', 'invoice_faq_result', {'payment_invoice'}),
    'promotion_and_member_policy': ('promotion_query', 'promotion_stacking', '露营季满减和平台会员券能叠加吗？', 'promotion_policy_result', {'promotion', 'member_coupon'}),
    'product_knowledge': ('product_query', 'tent_care', '帐篷怎么清洁收纳？', 'product_knowledge_result', {'product_knowledge'}),
}


class DomainDispatchTest(unittest.TestCase):
    setUp = fixtures.ContentPipelineTest.setUp
    chat = fixtures.ContentPipelineTest.chat

    def prepare(self, domains, intent=None, **extra):
        intent = intent or DOMAINS[domains[0]][0]
        if intent == 'product_query':
            extra.update(needs_product_facts=False,
                         knowledge_domains=[d for d in domains if d in {'product_knowledge', 'promotion_and_member_policy'}],
                         clarification_fields=[])
        if intent == 'promotion_query':
            extra.setdefault('clarification_fields', [])
        self.route.return_value = json.dumps(decision([DOMAINS[d][1] for d in domains], intent, **extra))
        self.generate.side_effect = TimeoutError('fixed_generation_fault')
        return ''.join(DOMAINS[d][2] for d in domains)

    def assert_domains(self, response, domains):
        self.assertEqual(set(response.session_state['route_plan']['knowledge_domains']), set(domains))
        retrievals = response.session_state['evidence_retrievals']
        for domain in domains:
            result = retrievals[domain]
            self.assertEqual(result['provider'], DOMAINS[domain][3])
            self.assertEqual(result['status'], 'answered')
            self.assertTrue(set(result['source_domains']) <= DOMAINS[domain][4])
            self.assertTrue(result['source_domains'])
            self.assertIn(DOMAINS[domain][1], [t for c in response.citations for t in c.metadata.get('rule_topics', [])])
        self.assertIsNone(response.session_state['workflow'])
        self.assertTrue(response.session_state['read_only_turn'])

    def test_five_single_domains_and_all_ten_pairs_in_both_orders(self):
        groups = [(d,) for d in DOMAINS] + [order for pair in itertools.combinations(DOMAINS, 2)
                                                  for order in (pair, pair[::-1])]
        for domains in groups:
            with self.subTest(domains=domains):
                response = self.chat(self.prepare(domains))
                self.assert_domains(response, domains)

    def test_three_domains_and_all_consultation_entries_use_same_providers(self):
        groups = [
            ['invoice', 'promotion_and_member_policy', 'product_knowledge'],
            ['after_sale_policy', 'invoice', 'promotion_and_member_policy'],
            ['product_knowledge', 'invoice', 'received_return_policy'],
        ]
        for domains in groups:
            for intent in [*(DOMAINS[d][0] for d in domains), 'service_scope_query']:
                with self.subTest(domains=domains, intent=intent):
                    self.assert_domains(self.chat(self.prepare(domains, intent)), domains)

    def test_partial_clarification_keeps_invoice_and_pending_question(self):
        text = self.prepare(['promotion_and_member_policy', 'invoice'],
                            clarification_fields=['promotion_combination'])
        response = self.chat('会员券能和优惠叠加吗？另外发票多久开好？')
        self.assertIn('24 小时', response.answer)
        self.assertIn('哪项优惠', response.answer)
        self.assertLess(response.answer.index('24 小时'), response.answer.index('哪项优惠'))
        self.assertEqual(response.session_state['next_action'], 'answer_user')
        self.assertFalse(any(c.metadata.get('rule_id') == 'promotion_stacking' for c in response.citations))

    def test_partial_failure_keeps_invoice_and_does_not_claim_unsupported(self):
        self.prepare(['promotion_and_member_policy', 'invoice'])
        response = self.chat('火星会员有什么优惠？发票多久开好？')
        self.assertIn('24 小时', response.answer)
        self.assertNotIn('不支持发票', response.answer)
        self.assertNotIn('满 300 减 40', response.answer)

    def test_wrong_source_is_rejected_before_selection_and_generation(self):
        text = self.prepare(['product_knowledge', 'invoice'])
        wrong = knowledge.KnowledgePathResult(answer='错误活动资料不能解释养护',
            citations=[load_knowledge_citation('promotion_policy.md')], risk_level='low',
            next_action='answer_user', needs_human_approval=False)
        with patch.object(knowledge, 'product_knowledge_result', return_value=wrong):
            response = self.chat(text)
        self.assertIn('24 小时', response.answer)
        self.assertNotIn('错误活动资料', response.answer)
        self.assertNotIn('promotion_policy.md', [c.source.removeprefix('knowledge/') for c in response.citations])
        self.assertEqual(response.session_state['evidence_retrievals']['product_knowledge']['status'], 'error')

    def test_exception_is_local_and_not_retried(self):
        text = self.prepare(['invoice', 'product_knowledge'])
        with patch.object(knowledge, 'product_knowledge_result', side_effect=TimeoutError('private error')) as provider:
            response = self.chat(text)
        provider.assert_called_once()
        self.assertIn('24 小时', response.answer)
        self.assertNotIn('private error', response.answer)
        self.assertEqual(response.session_state['evidence_retrievals']['product_knowledge']['status'], 'error')

    def test_cross_domain_query_keeps_background_after_sale_state(self):
        memory = current_memory('content-pipeline', 'U1001')
        draft = {'intent': 'refund_request', 'pending_slot': 'order_id', 'version': 6,
                 'candidates': [{'value': 'ORD9801', 'label': '第一笔'}]}
        memory['open_task'] = copy.deepcopy(draft)
        domains = ['product_knowledge', 'invoice']
        response = self.chat(self.prepare(domains))
        self.assert_domains(response, domains)
        self.assertEqual(memory['open_task'], draft)

    def test_valid_topics_extend_order_query_plan_without_authorizing_other_tools(self):
        self.prepare(['invoice', 'product_knowledge'], 'order_query')
        response = self.chat('先查一下物流。另外帐篷怎么清洗、发票多久开好？')
        plan = response.session_state['route_plan']
        self.assertEqual(set(plan['knowledge_domains']), {'invoice', 'product_knowledge'})
        self.assertEqual([t['name'] for t in plan['tool_candidates']], ['get_order_logistics'])
        self.assertEqual(response.tool_calls, [])
        self.assertIn('24 小时', response.answer)
        self.assertIn('晾干', response.answer)
        self.assertIsNotNone(response.clarification)

    def test_unknown_domain_has_no_default_provider(self):
        result = knowledge.query_knowledge_domain('unknown-domain', 'not_configured', '查规则')
        self.assertEqual(result.citations, [])
        self.assertEqual(result.retrieval_debug['failure'], 'unknown_knowledge_domain')
        self.assertIsNone(result.retrieval_debug['provider'])
        self.assertNotIn('不支持', result.answer)

    def test_forged_version_and_forged_publication_do_not_override_canonical_source(self):
        original = load_knowledge_citation('product_knowledge.md')
        for field, value in [('scene_key', 'invoice'), ('version', 'made-up'),
                             ('snapshot_version', 'old-snapshot')]:
            with self.subTest(field=field):
                forged = original.model_copy(deep=True)
                forged.metadata[field] = value
                wrong = knowledge.KnowledgePathResult('不要泄漏的伪造正文', [forged], 'low', 'answer_user', False)
                with patch.object(knowledge, 'product_knowledge_result', return_value=wrong):
                    result = knowledge.query_knowledge_domain('forged', 'product_knowledge', '帐篷怎么清洗')
                self.assertEqual(result.citations, [])
                self.assertEqual(result.retrieval_debug['failure'], 'knowledge_source_mismatch')
                self.assertNotIn('伪造正文', result.answer)
        snapshot = copy.deepcopy(get_knowledge_snapshot())
        snapshot.citations['product_knowledge.md'].metadata['publication_status'] = 'withdrawn'
        forged = knowledge.KnowledgePathResult('不要泄漏的旧正文', [original], 'low', 'answer_user', False)
        with patch.object(knowledge, 'get_knowledge_snapshot', return_value=snapshot), \
             patch.object(knowledge, 'product_knowledge_result', return_value=forged):
            result = knowledge.query_knowledge_domain('withdrawn', 'product_knowledge', '帐篷怎么清洗')
        self.assertFalse(result.citations)
        self.assertEqual(result.retrieval_debug['status'], 'error')

    def test_order_eligibility_and_invoice_keep_structured_evidence_and_original_permission(self):
        self.prepare(['invoice'], 'after_sale_eligibility_query')
        with patch('agent.tools.tool_runtime.ecommerce_get', return_value={
            'orderNo': 'ORD9801', 'status': 'ELIGIBLE', 'applicationType': 'REFUND',
            'amount': 398, 'reason': '已支付未发货'}):
            response = self.chat('查 ORD9801 的退款资格，另外发票多久开好？')
        self.assertIn('398', response.answer)
        self.assertIn('24 小时', response.answer)
        self.assertIn('没有准备申请', response.answer)
        self.assertIsNone(response.session_state['workflow'])
        self.assertEqual([c.tool_name for c in response.tool_calls], ['get_after_sale_eligibility'])

    def test_normal_repair_and_fallback_share_valid_sources_without_reretrieval(self):
        for mode in ('normal', 'repair', 'fallback'):
            with self.subTest(mode=mode):
                text = self.prepare(['invoice', 'product_knowledge'])
                self.generate.side_effect = None
                self.generate.return_value = ModelInvocationResult(content='已核实的正常答复')
                self.judge.side_effect = None
                self.judge.return_value = judgment()
                if mode == 'repair':
                    self.generate.side_effect = [ModelInvocationResult(content='错误候选'), ModelInvocationResult(content='修复后答复')]
                    self.judge.side_effect = [judgment('fail'), judgment()]
                elif mode == 'fallback':
                    self.generate.side_effect = TimeoutError('fixed')
                with patch.object(knowledge, 'invoice_faq_result', wraps=knowledge.invoice_faq_result) as invoice, \
                     patch.object(knowledge, 'product_knowledge_result', wraps=knowledge.product_knowledge_result) as product:
                    response = self.chat(text)
                invoice.assert_called_once()
                product.assert_called_once()
                self.assert_domains(response, ['invoice', 'product_knowledge'])
                self.assertEqual(response.session_state['model']['answer_judge']['repair_attempted'], mode == 'repair')
                if mode == 'fallback':
                    self.assertIn('24 小时', response.answer)
                    self.assertIn('晾干', response.answer)
                else:
                    self.assertEqual(response.answer, '修复后答复' if mode == 'repair' else '已核实的正常答复')

    def test_ending_product_selection_does_not_execute_its_old_knowledge_plan(self):
        memory = current_memory('content-pipeline', 'U1001')
        memory['product_query'] = {'pending_candidates': [{'id': 4, 'name': '帐篷'}]}
        memory['clarification_focus'] = 'product_query'
        self.prepare(['product_knowledge', 'invoice'])
        with patch.object(knowledge, 'product_knowledge_result') as product, \
             patch.object(knowledge, 'invoice_faq_result') as invoice:
            response = self.chat('先不看了')
        product.assert_not_called()
        invoice.assert_not_called()
        self.assertEqual(response.citations, [])
        self.assertIn('结束这次商品选择', response.answer)

    def test_repeated_topics_share_one_domain_read(self):
        self.prepare(['invoice'])
        self.route.return_value = json.dumps(decision(['invoice_issue', 'invoice_issue'], 'invoice_query'))
        with patch.object(knowledge, 'invoice_faq_result', wraps=knowledge.invoice_faq_result) as provider:
            response = self.chat('发票多久开好，在哪里下载？')
        provider.assert_called_once()
        self.assertEqual(response.session_state['route_plan']['knowledge_domains'], ['invoice'])
        self.assertIn('24 小时', response.answer)

    def test_ambiguous_product_price_does_not_block_independent_invoice(self):
        self.route.return_value = json.dumps(decision(['invoice_issue'], 'product_query',
            needs_product_facts=True, knowledge_domains=[], clarification_fields=[]))
        self.generate.side_effect = TimeoutError('fixed')
        products = [{'id': i, 'name': f'露营折叠椅{label}', 'price': 100 * i, 'stock': 1, 'active': True}
                    for i, label in [(1, 'A'), (2, 'B')]]
        with patch('agent.tools.tool_runtime.products_from_ecommerce', return_value=products):
            response = self.chat('折叠椅多少钱？发票多久开好？')
        self.assertIn('24 小时', response.answer)
        self.assertIn('第二款', response.answer)
        self.assertEqual(response.session_state['next_action'], 'answer_user')
        self.assertIsNone(response.session_state['workflow'])

    def test_single_refund_or_return_without_evidence_preserves_direct_fallback(self):
        unavailable = knowledge.KnowledgePathResult('当前没有核实到有效售后规则，暂不能确认。', [],
            'medium', 'answer_user', False)
        for domain in ('after_sale_policy', 'received_return_policy'):
            with self.subTest(domain=domain):
                text = self.prepare([domain])
                with patch.object(knowledge, 'policy_query_result', return_value=unavailable), \
                     patch.object(self.agent.answer_model_client, 'compose_answer') as generate:
                    response = self.chat(text)
                generate.assert_not_called()
                self.assertEqual(response.citations, [])
                self.assertIn('没有核实到有效售后规则', response.answer)
                self.assertEqual(response.session_state['evidence_retrievals'][domain]['status'], 'no_evidence')
