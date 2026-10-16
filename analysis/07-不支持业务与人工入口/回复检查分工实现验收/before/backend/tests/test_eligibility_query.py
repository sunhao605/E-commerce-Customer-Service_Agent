"""资格查询的职责与副作用边界；固定路由和业务结果，无外部模型调用。"""
import unittest
from unittest.mock import patch

import httpx

from api.schemas import ChatRequest
from agent.context import SESSION_MEMORIES, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.models.router_client import ModelRouteResult, RouteModelClient
from agent.tools.planning import build_route_plan, classify_intent
from agent.tools.tool_runtime import get_after_sale_eligibility


class EligibilityQueryTest(unittest.TestCase):
    def setUp(self):
        SESSION_MEMORIES.clear()
        self.agent = Lesson41Agent()
        self.route = patch.object(self.agent.route_model_client, "plan_intent", side_effect=lambda message, **kw:
            ModelRouteResult(intent=kw["fallback_intent"], used_model=False))
        self.route.start()
        self.addCleanup(self.route.stop)
        self.answer = patch.object(self.agent.answer_model_client, "can_call_model", return_value=False)
        self.answer.start()
        self.addCleanup(self.answer.stop)

    def chat(self, text, context=None):
        return self.agent.chat(ChatRequest(session_id="query-test", runtime_user_id="U1001", user_message=text,
            runtime_context=context))

    def test_one_query_label_and_read_only_tool_plan(self):
        for text in ("ORD9801 能退吗？先别申请", "帮我看看这单能不能退", "ORD9801 如果能退就帮我退"):
            self.assertEqual(classify_intent(text), "after_sale_eligibility_query")
        self.assertEqual(classify_intent("我要申请退款，原因买错了"), "refund_request")
        self.assertEqual(classify_intent("我要申请七天无理由退货"), "return_request")
        self.assertEqual(RouteModelClient._extract_intent('{"intent":"after_sale_eligibility_query"}'), "after_sale_eligibility_query")
        plan = build_route_plan(intent="after_sale_eligibility_query", user_message="", order_id="ORD9801", model_used=True)
        self.assertEqual(plan.required_tools, ["get_after_sale_eligibility"])
        self.assertFalse(plan.requires_workflow)
        self.assertFalse(plan.needs_rag)
        self.assertEqual(plan.risk_level, "low")

    def test_wrong_apply_route_cannot_prepare_a_specific_query(self):
        old = {"intent": "refund_request", "pending_slot": "order_id", "candidates": []}
        current_memory("query-test", "U1001")["open_task"] = old.copy()
        result = {"orderNo": "ORD9801", "status": "ELIGIBLE", "applicationType": "REFUND", "amount": 200, "reason": "已核实。"}
        with patch.object(self.agent.route_model_client, "plan_intent", return_value=ModelRouteResult(intent="refund_request", used_model=True)), \
                patch("agent.tools.tool_runtime.ecommerce_get", return_value=result) as read, \
                patch("agent.customer_service_agent.REFUND_APPROVAL_GRAPH.run") as workflow:
            response = self.chat("ORD9801 还能退吗？先别申请")
        self.assertEqual(response.session_state["intent"], "after_sale_eligibility_query")
        self.assertIsNone(response.session_state["workflow"])
        workflow.assert_not_called()
        read.assert_called_once_with("/api/orders/ORD9801/after-sale-eligibility", delegated_user_id="U1001")
        self.assertEqual(current_memory("query-test", "U1001")["open_task"], old)
        self.assertEqual(current_memory("query-test", "U1001")["eligibility_query"]["order_id"], "ORD9801")
        self.assertEqual(current_memory("query-test", "U1001")["eligibility_query"]["application_type"], "REFUND")
        self.assertIsNone(current_memory("query-test", "U1001")["last_order_id"])
        self.assertFalse(response.session_state["needs_human_approval"])
        self.assertFalse(response.session_state["model"]["final_answer"]["used_model"])

    def test_query_candidates_are_independent_of_application_candidates(self):
        old = {"intent": "refund_request", "pending_slot": "order_id", "candidates": [{"value": "ORD9802"}]}
        current_memory("query-test", "U1001")["open_task"] = old.copy()
        current_memory("query-test", "U1001")["last_order_id"] = "ORD9802"
        context = {"currentUserOrders": [{"orderNo": "ORD9801", "userId": "U1001", "items": []}]}
        asked = self.chat("这笔订单能不能退", context)
        self.assertEqual(asked.clarification.candidates[0].value, "ORD9801")
        self.assertIsNone(asked.session_state["eligibility_query"]["order_id"])
        result = {"orderNo": "ORD9801", "status": "UNKNOWN", "applicationType": "REFUND", "reason": "支付事实缺失。"}
        with patch("agent.tools.tool_runtime.ecommerce_get", return_value=result) as read:
            chosen = self.chat("第一笔")
        self.assertEqual(chosen.session_state["intent"], "after_sale_eligibility_query")
        read.assert_called_once_with("/api/orders/ORD9801/after-sale-eligibility", delegated_user_id="U1001")
        self.assertEqual(current_memory("query-test", "U1001")["open_task"], old)
        self.assertIn("暂时无法核实", chosen.answer)

    def test_mall_explicit_apply_reuses_query_object_but_defers_preparation(self):
        context = {"mallAfterSaleRouting": True, "afterSaleQuery": {"order_id": "ORD9801", "pending_slot": None}}
        with patch("agent.customer_service_agent.REFUND_APPROVAL_GRAPH.run") as workflow, \
                patch("agent.customer_service_agent.get_order_detail") as detail:
            response = self.chat("那帮我申请退款，原因：买错了", context)
        self.assertEqual(response.session_state["intent"], "refund_request")
        self.assertEqual(response.session_state["context_report"]["chosen_order_id"], "ORD9801")
        self.assertIsNone(response.session_state["workflow"])
        workflow.assert_not_called()
        detail.assert_not_called()
        self.assertEqual(self.agent.route_model_client.plan_intent.call_count, 1)

    def test_unknown_and_failure_are_not_ineligible_and_have_no_mirror_fallback(self):
        with patch("agent.tools.tool_runtime.ecommerce_get", return_value={"orderNo": "ORD9801", "status": "UNKNOWN",
                "applicationType": "REFUND", "amount": None, "reason": "实付记录不完整。"}):
            result, call = get_after_sale_eligibility("ORD9801", "U1001")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("暂时无法核实", call.output_summary)
        for status in (404, 503):
            response = httpx.Response(status, request=httpx.Request("GET", "http://isolated/"))
            with patch("agent.tools.tool_runtime.ecommerce_get", side_effect=httpx.HTTPStatusError("failure", request=response.request, response=response)):
                result, call = get_after_sale_eligibility("ORD9801", "U1001")
            self.assertIsNone(result)
            self.assertEqual(call.error_type, "order_unavailable" if status == 404 else "business_api_unavailable")
            self.assertNotIn("可以申请", call.output_summary)


if __name__ == "__main__":
    unittest.main()
