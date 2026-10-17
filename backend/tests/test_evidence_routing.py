"""证据需求契约与隔离执行：没有在线模型、业务 API 或资金写入。"""
import json
import unittest
from unittest.mock import patch

from api.schemas import ChatRequest, HistoryMessage
from agent.context import SESSION_MEMORIES, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.models.router_client import ModelRouteResult, RouteModelClient
from retrieval.hybrid_retrieval import reset_hybrid_index_and_cache

TENT = {"id": 4, "name": "山野四人帐篷", "price": 899.0, "stock": 3, "active": True}
OTHER = {**TENT, "id": 1, "name": "轻量双人帐篷", "price": 599.0}


def product_decision(facts=False, domains=None, clarification=None):
    return {"intent": "product_query", "needs_product_facts": facts,
            "knowledge_domains": ["product_knowledge"] if domains is None else domains,
            "clarification_fields": clarification or [],
            "inquiry_topics": (["tent_care"] if domains is None or "product_knowledge" in domains else [])
                + (["promotion_stacking"] if domains and "promotion_and_member_policy" in domains else [])}


class EvidenceContractTest(unittest.TestCase):
    def test_conditional_fields_are_strict(self):
        valid = product_decision()
        invalid = [[], {"intent": []}, {"intent": "product_query"},
                   {**valid, "needs_product_facts": "false"},
                   {**valid, "needs_product_facts": 0},
                   {**valid, "knowledge_domains": ["invoice"]},
                   {**valid, "knowledge_domains": "product_knowledge"},
                   {**valid, "clarification_fields": ["order_id"]},
                   {**valid, "clarification_fields": ["promotion_combination"]},
                   {**valid, "knowledge_domains": []},
                   {**valid, "tool": "refund"},
                   {"intent": "promotion_query"},
                   {"intent": "promotion_query", "clarification_fields": [], "needs_product_facts": True},
                   {"intent": "order_query", "knowledge_domains": []}]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertIsNone(RouteModelClient._extract_decision(json.dumps(payload)))
        self.assertEqual(RouteModelClient._extract_decision(json.dumps(valid)), {**valid, "topic_status": "valid"})
        unknown = product_decision(facts=None)
        self.assertEqual(RouteModelClient._extract_decision(json.dumps(unknown)), {**unknown, "topic_status": "valid"})

    def test_one_call_preserves_valid_evidence(self):
        client = RouteModelClient()
        payload = product_decision(domains=["product_knowledge", "promotion_and_member_policy"])
        with patch.object(client, "can_call_model", return_value=True), \
             patch.object(client, "_create_chat_model"), \
             patch.object(client, "_invoke_chain", return_value=json.dumps(payload)) as invoke:
            result = client.plan_intent("帐篷不要推荐，保养和会员规则呢？", fallback_intent="product_query")
        self.assertTrue(result.used_model)
        self.assertFalse(result.needs_product_facts)
        self.assertEqual(result.knowledge_domains, payload["knowledge_domains"])
        invoke.assert_called_once()

    def test_missing_evidence_falls_back_without_inventing_fields(self):
        client = RouteModelClient()
        with patch.object(client, "can_call_model", return_value=True), \
             patch.object(client, "_create_chat_model"), \
             patch.object(client, "_invoke_chain", return_value='{"intent":"product_query"}'):
            result = client.plan_intent("帐篷多少钱", fallback_intent="product_query")
        self.assertFalse(result.used_model)
        self.assertIsNone(result.knowledge_domains)
        self.assertEqual(result.fallback_reason, "invalid_model_route")


class EvidenceExecutionTest(unittest.TestCase):
    def setUp(self):
        SESSION_MEMORIES.clear()
        reset_hybrid_index_and_cache()
        self.agent = Lesson41Agent()
        self.decision = product_decision()
        self.route = patch.object(self.agent.route_model_client, "plan_intent", side_effect=self.route_result)
        self.route_mock = self.route.start()
        self.addCleanup(self.route.stop)
        for client in (self.agent.answer_model_client, self.agent.langchain_tool_runner):
            mock = patch.object(client, "can_call_model", return_value=False)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch("agent.tools.tool_runtime.products_from_ecommerce", return_value=[TENT])
        self.search = mock.start()
        self.addCleanup(mock.stop)
        mock = patch("agent.tools.tool_runtime.product_detail_from_ecommerce", side_effect=lambda pid: (
            {**(TENT if str(pid) == "4" else OTHER), "_fact_source": "business_api"}, None))
        self.detail = mock.start()
        self.addCleanup(mock.stop)

    def route_result(self, message, **kwargs):
        payload = RouteModelClient._extract_decision(json.dumps(self.decision))
        self.assertIsNotNone(payload)
        return ModelRouteResult(**payload, used_model=True, model_name="fixed-route")

    def chat(self, message, context=None, session="evidence-test", user="U1001"):
        return self.agent.chat(ChatRequest(session_id=session, runtime_user_id=user,
            user_message=message, runtime_context=context))

    def test_negated_fact_keyword_does_not_override_model(self):
        response = self.chat("帐篷不要推荐，只说怎么保养？")
        self.assertEqual(response.tool_calls, [])
        self.search.assert_not_called()
        self.assertFalse(response.session_state["route_plan"]["needs_business_tools"])
        self.assertIn("晾干", response.answer)

    def test_synonyms_and_simple_followup_reach_care_section(self):
        for term in ("保养", "清洗", "打理"):
            with self.subTest(term=term):
                response = self.chat(f"帐篷怎么{term}？")
                self.assertIn("3.3", response.citations[0].metadata["section"])
        followup = self.chat("怎么清洗？")
        self.assertEqual(followup.tool_calls, [])
        self.assertIn("3.3", followup.citations[0].metadata["section"])

    def test_product_name_does_not_force_a_guide(self):
        self.decision = product_decision(facts=True, domains=[])
        response = self.chat("防水收纳袋还有货吗？")
        self.assertEqual(response.citations, [])
        self.assertEqual(response.session_state["route_plan"]["retrieval_strategies"], [])

    def test_requested_spec_is_not_invented_from_price_data(self):
        self.decision = product_decision(True, [])
        response = self.chat("山野四人帐篷承重多少？")
        self.assertTrue(response.tool_calls)
        self.assertIn("尚未核实所问的承重", response.answer)
        self.assertNotIn("公斤", response.answer)

    def test_leak_return_question_does_not_prepare_application(self):
        self.decision = {"intent": "after_sale_eligibility_query"}
        response = self.chat("帐篷漏水了，能退吗？")
        self.assertEqual(response.session_state["intent"], "after_sale_eligibility_query")
        self.assertEqual(response.tool_calls, [])
        self.assertEqual(response.citations, [])
        self.assertIsNone(response.session_state["workflow"])
        self.assertEqual(response.session_state["next_action"], "ask_clarification")

    def test_order_eligibility_guard_discards_product_policy_fields(self):
        self.decision = product_decision(True, ["promotion_and_member_policy"], ["promotion_combination"])
        self.decision["inquiry_topics"] = []  # 本句只问资格，没有独立政策咨询。
        response = self.chat("这单能不能退？先别申请")
        self.assertEqual(response.session_state["intent"], "after_sale_eligibility_query")
        self.assertEqual(response.session_state["route_plan"]["clarification_fields"], [])
        self.assertEqual(response.session_state["route_plan"]["knowledge_domains"], [])
        self.assertEqual(response.tool_calls, [])
        self.assertIsNone(response.session_state["workflow"])

    def test_mixed_request_keeps_both_knowledge_domains(self):
        self.decision = product_decision(True, ["product_knowledge", "promotion_and_member_policy"])
        response = self.chat("山野四人帐篷多少钱，怎么收纳，露营季满减和会员券能叠加吗？")
        self.assertIn("899", response.answer)
        self.assertIn("晾干", response.answer)
        self.assertIn("满 300 减 40", response.answer)
        self.assertEqual(len(response.session_state["evidence_retrievals"]), 2)
        policies = {item.metadata.get("policy_id") for item in response.citations}
        self.assertIn("outdoor_product_guide", policies)
        self.assertTrue({"promotion_camping_season_stack_rule", "member_coupon_gold_rule"} <= policies)
        self.assertTrue(all(len(item.snippet) < 400 for item in response.citations))
        strategies = response.session_state["route_plan"]["retrieval_strategies"]
        self.assertEqual({x["citation_scope"] for x in strategies}, {"section"})
        self.assertEqual(self.route_mock.call_count, 1)

    def test_read_failure_does_not_block_independent_knowledge(self):
        self.decision = product_decision(True, ["product_knowledge", "promotion_and_member_policy"])
        self.search.return_value = None
        response = self.chat("山野四人帐篷多少钱，怎么收纳，露营季满减和会员券能叠加吗？")
        self.assertTrue(response.session_state["degraded"])
        self.assertIn("没有成功", response.answer)
        self.assertNotIn("899", response.answer)
        self.assertIn("晾干", response.answer)
        self.assertIn("满 300 减 40", response.answer)
        self.assertEqual(len(response.session_state["evidence_retrievals"]), 2)

    def test_missing_policy_keeps_verified_price_without_inventing_discount(self):
        self.decision = product_decision(True, ["promotion_and_member_policy"])
        self.decision["inquiry_topics"] = []  # 未发布活动没有受控主题。
        response = self.chat("山野四人帐篷多少钱，未发布的活动怎么优惠？")
        self.assertIn("899", response.answer)
        self.assertIn("尚未明确", response.answer)
        self.assertEqual(response.citations, [])
        self.assertIsNone(response.session_state["workflow"])

    def test_policy_only_has_no_product_evidence_or_tools(self):
        self.decision = {"intent": "promotion_query", "clarification_fields": [], "inquiry_topics": ["promotion_stacking"]}
        response = self.chat("露营季满减和会员券能叠加吗？")
        self.assertEqual(response.tool_calls, [])
        self.assertIsNone(response.session_state["route_plan"]["needs_product_facts"])
        self.assertIn("promotion_and_member_policy", response.session_state["evidence_retrievals"])

    def test_missing_combination_asks_and_resumes_without_fake_order_task(self):
        self.decision = {"intent": "promotion_query", "clarification_fields": ["promotion_combination"], "inquiry_topics": ["promotion_stacking"]}
        first = self.chat("会员券能否叠加？")
        self.assertEqual(first.citations, [])
        self.assertIn("哪项优惠", first.answer)
        self.assertIsNone(current_memory("evidence-test")["open_task"])
        self.decision = {"intent": "promotion_query", "clarification_fields": [], "inquiry_topics": ["promotion_stacking"]}
        second = self.chat("和露营季满减")
        self.assertIn("满 300 减 40", second.answer)
        self.assertEqual(second.tool_calls, [])
        self.assertNotIn("promotion_query", current_memory("evidence-test"))

    def test_price_completed_before_policy_reply_is_not_queried_again(self):
        self.decision = product_decision(True, ["promotion_and_member_policy"], ["promotion_combination"])
        first = self.chat("山野四人帐篷多少钱，会员券能叠加吗？")
        self.assertIn("899", first.answer)
        self.assertIn("哪项优惠", first.answer)
        calls = (self.search.call_count, self.detail.call_count)
        self.decision = {"intent": "promotion_query", "clarification_fields": [], "inquiry_topics": ["promotion_stacking"]}
        second = self.chat("和露营季满减")
        self.assertEqual((self.search.call_count, self.detail.call_count), calls)
        self.assertIn("满 300 减 40", second.answer)
        self.assertNotIn("899", second.answer)

    def test_two_missing_objects_can_be_filled_in_either_order(self):
        for policy_first in (True, False):
            with self.subTest(policy_first=policy_first):
                SESSION_MEMORIES.clear()
                self.search.return_value = [OTHER, TENT]
                self.decision = product_decision(True, ["promotion_and_member_policy"], ["promotion_combination"])
                first = self.chat("帐篷多少钱，会员券能叠加吗？")
                self.assertIn("第二款", first.answer)
                self.assertIn("哪项优惠", first.answer)
                for message in (("和露营季满减", "第二款") if policy_first else ("第二款", "和露营季满减")):
                    self.decision = ({"intent": "general_chat"} if message == "第二款" else {"intent": "promotion_query", "clarification_fields": [], "inquiry_topics": ["promotion_stacking"]})
                    response = self.chat(message)
                    if message == "第二款":
                        self.assertIn("899", response.answer)
                        self.assertEqual(response.session_state["product_query"]["product_id"], "4")
                    else:
                        self.assertIn("满 300 减 40", response.answer)
                self.assertNotIn("pending_evidence", current_memory("evidence-test")["product_query"])

    def test_withdrawn_price_need_does_not_execute_old_plan(self):
        self.search.return_value = [OTHER, TENT]
        self.decision = product_decision(True, [])
        self.chat("帐篷多少钱？")
        self.decision = product_decision(False)
        second = self.chat("先不问价格，只说帐篷怎么收纳")
        self.assertEqual(second.tool_calls, [])
        self.assertEqual(second.session_state["product_query"]["pending_candidates"], None)
        self.assertIn("晾干", second.answer)

    def test_uncertain_facts_are_not_converted_to_false(self):
        self.decision = product_decision(None)
        response = self.chat("帐篷帮我看看")
        self.assertEqual(response.tool_calls, [])
        self.assertIsNone(response.session_state["route_plan"]["needs_product_facts"])
        self.assertEqual(response.session_state["next_action"], "answer_user")
        self.assertIn("晾干", response.answer)

    def test_end_selection_clears_evidence_even_if_model_is_uncertain(self):
        self.search.return_value = [OTHER, TENT]
        self.decision = product_decision(True, [])
        self.chat("帐篷多少钱？")
        self.decision = product_decision(None, [])
        response = self.chat("先不看了")
        self.assertEqual(response.session_state["product_query"]["pending_candidates"], None)
        self.assertNotIn("pending_evidence", current_memory("evidence-test")["product_query"])

    def test_route_history_is_redacted_before_model_input(self):
        self.agent.chat(ChatRequest(session_id="evidence-test", runtime_user_id="U1001", user_message="帐篷怎么保养？",
            history_messages=[HistoryMessage(role="user", content="邮箱 a@example.com，电话 13800138000")]))
        note = self.route_mock.call_args.kwargs["task_note"]
        self.assertNotIn("13800138000", note)
        self.assertNotIn("a@example.com", note)

    def test_failed_model_attempt_is_still_counted_in_usage(self):
        self.route_mock.side_effect = None
        self.route_mock.return_value = ModelRouteResult(intent="product_query", used_model=False,
            invocation_attempted=True, fallback_reason="invalid_model_route", usage={"input_tokens": 42})
        response = self.chat("山野四人帐篷多少钱？")
        self.assertEqual(response.session_state["cost_summary"]["model_calls"]["route_planner"], 1)
        self.assertEqual(response.session_state["cost_summary"]["route_usage_actual"], {"input_tokens": 42})

    def test_runtime_original_sentence_is_the_model_input(self):
        self.chat("页面商品含价格库存等说明", {"userMessage": "帐篷怎么清洗？"})
        self.assertEqual(self.route_mock.call_args.args, ("帐篷怎么清洗？",))

    def test_page_guide_lookup_only_resolves_identity_not_price(self):
        response = self.chat("这款怎么收纳？", {"relatedProductId": 4})
        self.detail.assert_called_once_with(4)
        self.search.assert_not_called()
        self.assertTrue(response.session_state["route_plan"]["needs_business_tools"])
        self.assertFalse(response.session_state["route_plan"]["needs_product_facts"])
        self.assertTrue(any("3.3" in item.metadata["section"] for item in response.citations))
        self.assertNotIn("899", response.answer)

    def test_new_page_guide_does_not_use_previous_product(self):
        memory = current_memory("evidence-test", "U1001")
        memory["product_query"] = {"product_id": "1", "product_name": "露营折叠椅", "knowledge_topic": "折叠椅"}
        response = self.chat("这款怎么收纳？", {"relatedProductId": 4})
        self.detail.assert_called_once_with(4)
        self.assertTrue(any("3.3" in item.metadata["section"] for item in response.citations))
        self.assertEqual(response.session_state["product_query"]["product_id"], "4")

    def test_page_identity_failure_does_not_reuse_old_guide(self):
        memory = current_memory("evidence-test", "U1001")
        memory["product_query"] = {"product_id": "1", "product_name": "露营折叠椅", "knowledge_topic": "折叠椅"}
        self.detail.side_effect = None
        self.detail.return_value = (None, "timeout")
        response = self.chat("这款怎么收纳？", {"relatedProductId": 4})
        self.assertEqual(response.citations, [])
        self.assertTrue(response.session_state["degraded"])
        self.assertNotIn("899", response.answer)
        self.assertEqual(response.session_state["next_action"], "answer_user")

    def test_guide_can_use_page_context_without_this_item_phrase(self):
        response = self.chat("怎么收纳？", {"relatedProductId": 4})
        self.detail.assert_called_once_with(4)
        self.assertTrue(any("3.3" in item.metadata["section"] for item in response.citations))
        self.assertNotIn("899", response.answer)

    def test_existing_refund_draft_is_preserved_by_read_only_knowledge(self):
        memory = current_memory("evidence-test", "U1001")
        memory["open_task"] = {"intent": "refund_request", "pending_slot": "order_id", "candidates": []}
        before = dict(memory["open_task"])
        response = self.chat("帐篷怎么清洗？")
        self.assertEqual(memory["open_task"], before)
        self.assertIsNone(response.session_state["workflow"])

    def test_policy_pending_does_not_cross_user_identity(self):
        self.decision = {"intent": "promotion_query", "clarification_fields": ["promotion_combination"], "inquiry_topics": ["promotion_stacking"]}
        self.chat("会员券能否叠加？")
        self.decision = {"intent": "promotion_query", "clarification_fields": [], "inquiry_topics": ["promotion_stacking"]}
        self.chat("和露营季满减", user="U1002")
        self.assertNotIn("promotion_query", current_memory("evidence-test", "U1002"))

    def test_policy_pending_does_not_override_valid_chat_intent(self):
        self.decision = {"intent": "promotion_query", "clarification_fields": ["promotion_combination"], "inquiry_topics": ["promotion_stacking"]}
        self.chat("会员券能否叠加？")
        self.decision = {"intent": "general_chat"}
        response = self.chat("和你说再见，先不问了")
        self.assertEqual(response.session_state["intent"], "general_chat")
        self.assertEqual(response.citations, [])
        self.assertEqual(response.tool_calls, [])


if __name__ == "__main__":
    unittest.main()
