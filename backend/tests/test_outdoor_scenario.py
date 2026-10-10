"""户外业务词迁移检查；无模型调用、无真实业务接口。"""
import os
import unittest
from unittest.mock import patch

os.environ.update(AGENT_COURSE_DISABLE_LLM='1', AGENT_COURSE_OFFLINE_RAG='1', AGENT_COURSE_OFFLINE_FACTS='1', AGENT_COURSE_ENV='/tmp/huangye-no-model.env')
from agent.ecommerce_client import product_query_keyword, products_from_ecommerce
from retrieval.knowledge import promotion_policy_result
from agent.tools.planning import classify_intent, extract_order_id

class OutdoorScenarioTest(unittest.TestCase):
    def test_outdoor_paths_and_evidence(self):
        for noun in ('露营椅', '折叠椅', '野餐垫', '帐篷', '天幕', '营地灯', '背包', '登山杖', '睡袋', '收纳柜', '炊具', '营地旗'):
            self.assertEqual(classify_intent(f'看看{noun}'), 'product_query', noun)
        self.assertEqual(classify_intent('露营季满减和山友会员券能否叠加？'), 'promotion_query')
        self.assertEqual(classify_intent('查订单物流并申请退款'), 'refund_request')
        self.assertEqual(extract_order_id('查 ORD1001 物流'), 'ORD1001')
        self.assertEqual(product_query_keyword('露营椅价格是多少'), '折叠椅')
        with patch('agent.ecommerce_client.ecommerce_get', side_effect=ConnectionError('offline')):
            products = products_from_ecommerce('露营椅')
            self.assertEqual([(p['name'],p['price'],p['stock']) for p in products], [('露营折叠椅',599.0,520)])
            self.assertEqual(products_from_ecommerce('月球充气艇'), [])
        result = promotion_policy_result('outdoor-policy-test', '露营季满减和山友会员券能否叠加？')
        self.assertEqual({c.metadata['policy_id'] for c in result.citations}, {'promotion_618_stack_rule','member_coupon_gold_rule'})
        self.assertIn('山友会员', result.answer)
        unknown = promotion_policy_result('outdoor-unknown-test', '露营季有隐藏券吗？')
        self.assertEqual(unknown.citations, [])
        self.assertEqual(unknown.next_action, 'transfer_to_human')

if __name__ == '__main__':
    unittest.main()
