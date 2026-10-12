"""模型给出合法标签后不被关键词规则改掉。不调用模型。"""
import inspect
import os
import unittest
from unittest.mock import patch

from api.schemas import ChatRequest
from agent.customer_service_agent import Lesson41Agent
from agent.models.router_client import ModelRouteResult, RouteModelClient
from agent.context import current_memory
from agent.state import COMMON_HIT_CACHE
from agent.tools.planning import build_route_plan, classify_intent, format_open_task, is_slot_reply


class RoutePriorityTest(unittest.TestCase):
    def test_chat_keeps_a_valid_model_label_when_the_rule_disagrees(self):
        message = "我不要退款，只查 ORD1234 的物流。"
        self.assertEqual(classify_intent(message), "refund_request")
        model_result = ModelRouteResult(intent="order_query", used_model=True, model_name="test")
        source = inspect.getsource(Lesson41Agent.chat)
        self.assertNotIn("_apply_route_guard", source)
        self.assertNotIn("rule_guard_", source)
        plan = build_route_plan(
            intent=model_result.intent,
            user_message=message,
            order_id="ORD1234",
            model_used=model_result.used_model,
        )
        self.assertEqual(plan.intent, "order_query")
        self.assertEqual(plan.source, "llm_with_policy_constraints")

    def test_invalid_or_disabled_model_still_uses_the_rule(self):
        self.assertIsNone(RouteModelClient._extract_intent("不是 JSON"))
        with patch.dict(os.environ, {"AGENT_COURSE_DISABLE_LLM": "1"}):
            result = RouteModelClient().plan_intent(
                "我不要退款，只查 ORD1234 的物流。",
                fallback_intent="refund_request",
            )
        self.assertEqual(result.intent, "refund_request")
        self.assertFalse(result.used_model)
        self.assertEqual(result.fallback_reason, "model_config_missing")

    def test_reported_fault_is_not_treated_as_a_verified_outage(self):
        agent = Lesson41Agent()
        message = "我查 ORD1234 物流时页面提示接口不可用，现在还是查不了。"
        with patch.object(
            agent.route_model_client,
            "plan_intent",
            return_value=ModelRouteResult(intent="degradation_request", used_model=True, model_name="test"),
        ):
            response = agent.chat(ChatRequest(session_id="t-n12", runtime_user_id="u1", user_message=message))
        self.assertEqual(response.session_state["intent"], "degradation_request")
        self.assertEqual(response.tool_calls, [])
        self.assertIn("还没有核实", response.answer)
        self.assertNotIn("暂时不可用", response.answer)
        self.assertNotIn("不存在", response.answer)
        self.assertEqual(response.session_state["cost_summary"]["degradation"]["reason"], "user_reported_unverified")

    def test_course_fault_injection_still_states_the_service_is_unavailable(self):
        agent = Lesson41Agent()
        message = "【故障注入演示】模拟物流服务返回 SERVICE_TIMEOUT：请帮我查一下 SO20260602103000009-a1000009 的物流"
        with patch.object(
            agent.route_model_client,
            "plan_intent",
            return_value=ModelRouteResult(intent="degradation_request", used_model=True, model_name="test"),
        ):
            response = agent.chat(ChatRequest(session_id="t-inject", runtime_user_id="u1", user_message=message))
        self.assertIn("暂时不可用", response.answer)
        self.assertEqual(response.tool_calls, [])

    def test_unclear_order_task_asks_instead_of_looking_up(self):
        agent = Lesson41Agent()
        message = "帮我处理一下 ORD1234。"
        with patch.object(
            agent.route_model_client,
            "plan_intent",
            return_value=ModelRouteResult(intent="unknown", used_model=True, model_name="test"),
        ):
            response = agent.chat(ChatRequest(session_id="t-b14", runtime_user_id="u1", user_message=message))
        self.assertEqual(response.session_state["intent"], "unknown")
        self.assertEqual(response.tool_calls, [])
        self.assertIsNone(response.clarification)
        self.assertIn("查物流", response.answer)
        self.assertIn("申请售后", response.answer)
        self.assertIn("不会替你选定", response.answer)
        self.assertFalse(response.session_state["model"]["final_answer"]["used_model"])

    def test_collecting_turn_passes_the_open_task_and_keeps_a_model_switch(self):
        agent = Lesson41Agent()
        seen = []

        def fake_plan(message, *, fallback_intent, task_note=None):
            seen.append(task_note)
            if "不要退款" in message:
                return ModelRouteResult(intent="order_query", used_model=True, model_name="test")
            return ModelRouteResult(intent="refund_request", used_model=True, model_name="test")

        with patch.object(agent.route_model_client, "plan_intent", side_effect=fake_plan):
            agent.chat(ChatRequest(session_id="t-open", runtime_user_id="u1", user_message="帮我申请退款。"))
            switched = agent.chat(
                ChatRequest(session_id="t-open", runtime_user_id="u1", user_message="先不要退款，查物流。")
            )
        self.assertIsNone(seen[0])
        self.assertIn("任务：refund_request", seen[1])
        self.assertIn("缺少：order_id", seen[1])
        self.assertEqual(switched.session_state["intent"], "order_query")
        self.assertEqual(current_memory("t-open", "u1")["open_task"]["intent"], "order_query")

    def test_model_failure_keeps_the_open_task_for_a_slot_reply(self):
        agent = Lesson41Agent()
        current_memory("t-slot", "u1")["open_task"] = {
            "intent": "refund_request",
            "pending_slot": "candidate_index",
            "candidates": [{"value": "ORD1234", "label": "ORD1234"}, {"value": "ORD5678", "label": "ORD5678"}],
        }
        seen = []

        def fake_plan(message, *, fallback_intent, task_note=None):
            seen.append(task_note)
            return ModelRouteResult(intent="general_chat", used_model=False, fallback_reason="invalid_model_route")

        with patch.object(agent.route_model_client, "plan_intent", side_effect=fake_plan):
            response = agent.chat(ChatRequest(session_id="t-slot", runtime_user_id="u1", user_message="第一笔。"))
        self.assertEqual(response.session_state["intent"], "refund_request")
        self.assertEqual(response.session_state["model"]["route_planner"]["fallback_reason"], "open_task_slot")
        self.assertIn("1. ORD1234", seen[0])
        self.assertTrue(is_slot_reply("ORD1234"))
        self.assertTrue(is_slot_reply("我选择 ORD1234。"))
        self.assertFalse(is_slot_reply("先不要退款，帮我查 ORD1234 的物流。"))
        self.assertIsNone(format_open_task(None))

    def test_policy_queries_stay_read_only_and_invoice_stays_invoice(self):
        self.assertEqual(classify_intent("退款政策是什么？我先了解一下，不申请退款。"), "refund_policy_query")
        self.assertEqual(classify_intent("七天无理由退货需要满足什么条件？只是咨询，暂时不退。"), "return_policy_query")
        self.assertEqual(classify_intent("发票开具后在哪里下载？"), "invoice_query")
        self.assertEqual(classify_intent("ORD1234 还没发货，我现在要申请退款。"), "refund_request")
        self.assertEqual(classify_intent("ORD1234 已签收，我想申请七天无理由退货。"), "return_request")
        self.assertEqual(classify_intent("ORD1234 还能退吗？先别申请。"), "unknown")
        self.assertEqual(classify_intent("我不要退款，只查 ORD1234 的物流。"), "refund_request")
        self.assertEqual(RouteModelClient._extract_intent('{"intent":"refund_policy_query"}'), "refund_policy_query")
        self.assertEqual(RouteModelClient._extract_intent('{"intent":"return_policy_query"}'), "return_policy_query")
        self.assertEqual(RouteModelClient._extract_intent('{"intent":"invoice_query"}'), "invoice_query")
        self.assertIsNone(RouteModelClient._extract_intent('{"intent":"faq_query"}'))

        refund_plan = build_route_plan(
            intent="refund_policy_query",
            user_message="退款政策是什么？",
            order_id=None,
            model_used=True,
        )
        self.assertTrue(refund_plan.needs_rag)
        self.assertFalse(refund_plan.needs_business_tools)
        self.assertEqual(refund_plan.required_tools, [])
        self.assertFalse(refund_plan.requires_workflow)
        self.assertEqual(refund_plan.knowledge_domains, ["after_sale_policy"])
        apply_plan = build_route_plan(
            intent="return_request",
            user_message="我想申请七天无理由退货。",
            order_id="ORD1234",
            model_used=True,
        )
        self.assertTrue(apply_plan.requires_workflow)
        self.assertEqual(apply_plan.required_tools, ["get_order_detail"])

        agent = Lesson41Agent()
        saved_cache = dict(COMMON_HIT_CACHE)
        COMMON_HIT_CACHE.clear()
        COMMON_HIT_CACHE["faq:invoice_issue"] = {
            "answer": "发票缓存标记",
            "citation": "invoice_issue",
            "source": "test",
        }

        def use_fallback(message, *, fallback_intent, task_note=None):
            return ModelRouteResult(intent=fallback_intent, used_model=False, fallback_reason="invalid_model_route")

        try:
            with patch.object(agent.route_model_client, "plan_intent", side_effect=use_fallback), patch.object(
                agent.answer_model_client, "can_call_model", return_value=False
            ):
                refund = agent.chat(
                    ChatRequest(
                        session_id="t-refund-policy",
                        runtime_user_id="u1",
                        user_message="退款政策是什么？我先了解一下，不申请退款。",
                    )
                )
                returned = agent.chat(
                    ChatRequest(
                        session_id="t-return-policy",
                        runtime_user_id="u1",
                        user_message="七天无理由退货需要满足什么条件？只是咨询，暂时不退。",
                    )
                )
                invoice = agent.chat(
                    ChatRequest(session_id="t-invoice", runtime_user_id="u1", user_message="发票开具后在哪里下载？")
                )
                eligibility = agent.chat(
                    ChatRequest(
                        session_id="t-eligibility",
                        runtime_user_id="u1",
                        user_message="ORD1234 还能退吗？先别申请。",
                    )
                )
                apply_refund = agent.chat(
                    ChatRequest(session_id="t-apply-refund", runtime_user_id="u1", user_message="帮我申请退款。")
                )
                with patch.object(
                    agent.route_model_client,
                    "plan_intent",
                    return_value=ModelRouteResult(intent="unknown", used_model=True, model_name="test"),
                ):
                    asked = agent.chat(
                        ChatRequest(
                            session_id="t-eligibility-unknown",
                            runtime_user_id="u1",
                            user_message="ORD1234 还能退吗？先别申请。",
                        )
                    )
                with patch.object(
                    agent.route_model_client,
                    "plan_intent",
                    return_value=ModelRouteResult(intent="refund_policy_query", used_model=True, model_name="test"),
                ):
                    kept = agent.chat(
                        ChatRequest(session_id="t-model-policy", runtime_user_id="u1", user_message="帮我申请退款。")
                    )
                cache_after = dict(COMMON_HIT_CACHE)
        finally:
            COMMON_HIT_CACHE.clear()
            COMMON_HIT_CACHE.update(saved_cache)

        for response, policy_id in ((refund, "refund_before_shipping"), (returned, "return_after_delivery")):
            self.assertEqual(response.tool_calls, [])
            self.assertIsNone(response.clarification)
            self.assertIsNone(response.session_state["workflow"])
            self.assertIsNone(response.session_state["memory"]["open_task"])
            self.assertEqual(response.session_state["next_action"], "answer_user")
            self.assertFalse(response.session_state["needs_human_approval"])
            self.assertEqual(
                [item.metadata.get("policy_id") for item in response.citations],
                [policy_id],
            )
            self.assertNotIn("发票", response.answer)
        self.assertEqual(refund.session_state["intent"], "refund_policy_query")
        self.assertEqual(returned.session_state["intent"], "return_policy_query")
        self.assertIn("未发货", refund.answer)
        self.assertIn("7 天", returned.answer)
        self.assertEqual(invoice.session_state["intent"], "invoice_query")
        self.assertEqual(invoice.answer, "发票缓存标记")
        self.assertEqual(invoice.session_state["memory"]["open_task"], None)
        self.assertEqual(eligibility.session_state["intent"], "unknown")
        self.assertIsNone(eligibility.session_state["workflow"])
        self.assertIsNone(eligibility.session_state["memory"]["open_task"])
        self.assertIn("不会准备申请", eligibility.answer)
        self.assertEqual(apply_refund.session_state["intent"], "refund_request")
        self.assertEqual(apply_refund.session_state["memory"]["open_task"]["intent"], "refund_request")
        self.assertEqual(asked.session_state["intent"], "unknown")
        self.assertEqual(asked.tool_calls, [])
        self.assertIsNone(asked.session_state["workflow"])
        self.assertIsNone(asked.session_state["memory"]["open_task"])
        self.assertIn("不会准备申请", asked.answer)
        self.assertEqual(kept.session_state["intent"], "refund_policy_query")
        self.assertEqual(kept.tool_calls, [])
        self.assertIsNone(kept.session_state["workflow"])
        self.assertIsNone(kept.session_state["memory"]["open_task"])
        self.assertEqual(cache_after, {"faq:invoice_issue": {
            "answer": "发票缓存标记",
            "citation": "invoice_issue",
            "source": "test",
        }})
