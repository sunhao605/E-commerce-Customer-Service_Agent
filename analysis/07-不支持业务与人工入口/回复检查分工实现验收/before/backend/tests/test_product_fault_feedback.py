"""商品查询故障反馈：空结果、读取失败、部分事实和恢复。不调用在线模型，不访问真实业务接口。"""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from api.schemas import ChatRequest
from agent.context import SESSION_MEMORIES, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.ecommerce_client import product_detail_from_ecommerce, product_query_keyword, products_from_ecommerce
from agent.models.answer_client import FinalAnswerModelResult
from agent.models.router_client import ModelRouteResult
from agent.tools.tool_runtime import search_products

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "analysis" / "工具故障反馈实现验收"
CHAIR = {"id": 4, "name": "轻量露营折叠椅", "price": 899.0, "stock": 36, "active": True}
QUESTION = "折叠椅多少钱？"
NAMED = "轻量露营折叠椅多少钱？"


def _response(status, payload=None, *, broken=False):
    response = MagicMock(status_code=status)
    if broken:
        response.json.side_effect = ValueError("bad json")
    else:
        response.json.return_value = payload
    if status >= 400:
        request = httpx.Request("GET", "http://goods.test")
        response.raise_for_status.side_effect = httpx.HTTPStatusError("failed", request=request, response=response)
    return response


class _HTTP:
    """隔离 HTTP 替身。reply 放在列表里，避免类上的函数被当成实例方法。"""

    reply = [lambda url: _response(500, {"success": False})]

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        return self.reply[0](url)


class ProductReadContractTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"AGENT_COURSE_OFFLINE_FACTS": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = patch("agent.ecommerce_client.httpx.Client", _HTTP)
        self.client.start()
        self.addCleanup(self.client.stop)

    def test_f05_business_json_and_shape_failures_are_not_empty_success(self):
        cases = {
            "business_false": _response(200, {"success": False, "data": []}),
            "json": _response(200, broken=True),
            "not_a_list": _response(200, {"success": True, "data": {"id": 1}}),
            "rows_not_objects": _response(200, {"success": True, "data": ["bad"]}),
            "search_404": _response(404, {"success": False, "code": "NOT_FOUND", "message": "missing"}),
            "http_500": _response(500, {"success": False, "code": "ERROR"}),
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                _HTTP.reply[0] = lambda url, response=response: response
                found, note = products_from_ecommerce(QUESTION)
                self.assertIsNone(found)
                self.assertEqual(note, "unavailable")
                _products, call = search_products("折叠椅", match_text=QUESTION)
                # 读取失败不能走“成功但没找到”。search_products 看到的是客户端返回值；这里直接用同一分类。
                self.assertNotEqual(call.arguments.get("result"), "no_match")

        def time_out(url):
            raise httpx.ReadTimeout("slow")

        _HTTP.reply[0] = time_out
        found, note = products_from_ecommerce(QUESTION)
        self.assertIsNone(found)
        self.assertEqual(note, "timeout")

        _HTTP.reply[0] = lambda url: _response(200, {"success": True, "data": []})
        found, note = products_from_ecommerce(QUESTION)
        self.assertEqual(found, [])
        self.assertEqual(note, "business_api")
        _products, call = search_products("折叠椅", match_text=QUESTION)
        self.assertEqual(call.status, "success")
        self.assertEqual(call.arguments["result"], "no_match")
        self.assertIsNone(call.error_type)

    def test_detail_not_found_requires_contract_other_bad_payloads_fail(self):
        product = {"id": 4, "name": "轻量露营折叠椅", "price": 1, "stock": 1}
        checks = [
            (_response(404, {"success": False, "code": "NOT_FOUND"}), "not_found"),
            (_response(404, {"success": False, "code": "NO_SUCH_ROUTE"}), "unavailable"),
            (_response(404, broken=True), "unavailable"),
            (_response(400, {"success": False, "code": "NOT_FOUND"}), "unavailable"),
            (_response(500, {"success": False}), "unavailable"),
            (_response(200, {"success": False, "code": "NOT_FOUND", "data": None}), "unavailable"),
            (_response(200, {"success": True, "data": None}), "unavailable"),
            (_response(200, {"success": True, "data": {"id": 9, "name": "别的商品"}}), "unavailable"),
            (_response(200, {"success": True, "data": product}), None),
        ]
        for response, expected in checks:
            with self.subTest(expected=expected, status=response.status_code):
                _HTTP.reply[0] = lambda url, response=response: response
                found, error = product_detail_from_ecommerce(4)
                self.assertEqual(error, expected)
                if expected is None:
                    self.assertEqual(found["name"], "轻量露营折叠椅")
                else:
                    self.assertIsNone(found)

        def time_out(url):
            raise httpx.ReadTimeout("slow")

        _HTTP.reply[0] = time_out
        found, error = product_detail_from_ecommerce(4)
        self.assertIsNone(found)
        self.assertEqual(error, "timeout")


class ProductFaultFeedbackTest(unittest.TestCase):
    records: list[dict] = []

    def setUp(self):
        SESSION_MEMORIES.clear()
        self.agent = Lesson41Agent()
        route = patch.object(
            self.agent.route_model_client, "plan_intent",
            side_effect=lambda message, **kw: ModelRouteResult(intent=kw["fallback_intent"], used_model=False),
        )
        route.start()
        self.addCleanup(route.stop)
        for target in (self.agent.answer_model_client, self.agent.langchain_tool_runner):
            model_switch = patch.object(target, "can_call_model", return_value=False)
            model_switch.start()
            self.addCleanup(model_switch.stop)
        self.search = patch("agent.tools.tool_runtime.products_from_ecommerce")
        self.search_mock = self.search.start()
        self.addCleanup(self.search.stop)
        self.detail = patch("agent.tools.tool_runtime.product_detail_from_ecommerce")
        self.detail_mock = self.detail.start()
        self.addCleanup(self.detail.stop)

    def tearDown(self):
        SESSION_MEMORIES.clear()

    def chat(self, text, context=None, session_id="fault"):
        return self.agent.chat(ChatRequest(
            session_id=session_id, runtime_user_id="U1001", user_message=text, runtime_context=context,
        ))

    def liar(self, text, session_id):
        """固定模型把任何商品结果说成缺货。最终回复不能采用这句。"""

        class ToolModel(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        model = ToolModel(responses=[
            AIMessage(content="", tool_calls=[{
                "id": "product-call", "name": "search_products",
                "args": {"keyword": product_query_keyword(text)},
            }]),
            AIMessage(content="这款已经缺货了"),
        ])
        lied = FinalAnswerModelResult(answer="这款已经缺货了", used_model=True)
        self.agent.langchain_tool_runner.can_call_model.return_value = True
        self.agent.answer_model_client.can_call_model.return_value = True
        try:
            with patch.object(self.agent.langchain_tool_runner, "_create_chat_model", return_value=model), \
                    patch.object(self.agent.answer_model_client, "compose_answer", return_value=lied) as spy:
                response = self.chat(text, session_id=session_id)
        finally:
            self.agent.langchain_tool_runner.can_call_model.return_value = False
            self.agent.answer_model_client.can_call_model.return_value = False
        return response, spy

    def note(self, case_id, response, **extra):
        call = response.tool_calls[-1]
        self.records.append({
            "case": case_id,
            "answer": response.answer,
            "tool_status": call.status,
            "error_type": call.error_type,
            "arguments": call.arguments,
            "degraded": response.session_state["degraded"],
            "degradation_reason": response.session_state["cost_summary"]["degradation"]["reason"],
            "final_answer_skip": response.session_state["model"]["final_answer"]["fallback_reason"],
            **extra,
        })

    def test_f01_empty_success_is_not_a_service_failure(self):
        self.search_mock.return_value = []
        response = self.chat(QUESTION, session_id="f01")
        call = response.tool_calls[-1]
        self.assertEqual(call.status, "success")
        self.assertIsNone(call.error_type)
        self.assertEqual(call.arguments["result"], "no_match")
        self.assertEqual(call.arguments["fact_source"], "business_api")
        self.assertIn("没有找到", response.answer)
        self.assertIn("补充", response.answer)
        self.assertNotIn("没有成功", response.answer)
        self.assertNotIn("缺货", response.answer)
        self.assertFalse(response.session_state["degraded"])
        self.assertIsNone(response.session_state["cost_summary"]["degradation"]["reason"])
        self.assertEqual(response.session_state["model"]["final_answer"]["fallback_reason"], "clarification_required")
        self.note("F01", response, path="code_fallback", input=QUESTION)

    def test_f02_timeout_and_http_error_stay_failures_on_both_paths(self):
        for path in ("code_fallback", "model_tool"):
            for failure, error_type in (("timeout", "timeout"), ("unavailable", "business_api_unavailable")):
                with self.subTest(path=path, failure=failure):
                    self.search_mock.return_value = (None, failure)
                    session_id = f"f02-{path}-{failure}"
                    spy = None
                    if path == "model_tool":
                        response, spy = self.liar(QUESTION, session_id)
                        self.assertTrue(response.session_state["tool_calling"]["create_agent"])
                        self.assertIn("search_products", response.session_state["tool_calling"]["selected_tools"])
                        spy.assert_not_called()
                    else:
                        response = self.chat(QUESTION, session_id=session_id)
                    call = response.tool_calls[-1]
                    self.assertEqual(call.status, "error")
                    self.assertEqual(call.error_type, error_type)
                    self.assertIn("没有成功", response.answer)
                    self.assertIn("稍后重试", response.answer)
                    for banned in ("不存在", "缺货", "无库存", "补充更完整", "停机"):
                        self.assertNotIn(banned, response.answer)
                    self.assertTrue(response.session_state["degraded"])
                    self.assertEqual(response.session_state["cost_summary"]["degradation"]["reason"], error_type)
                    self.assertEqual(response.session_state["model"]["final_answer"]["fallback_reason"], "degraded_path")
                    self.note("F02", response, path=path, injected=failure, input=QUESTION)

    def test_f03_zero_stock_is_a_fact(self):
        self.search_mock.return_value = [{**CHAIR, "price": 199.0, "stock": 0}]
        response = self.chat(NAMED, session_id="f03")
        call = response.tool_calls[-1]
        self.assertEqual(call.status, "success")
        self.assertIsNone(call.error_type)
        self.assertIn("轻量露营折叠椅", response.answer)
        self.assertIn("标价 199", response.answer)
        self.assertIn("当前无库存", response.answer)
        for banned in ("没有成功", "暂未核实", "None", "缺货", "不存在"):
            self.assertNotIn(banned, response.answer)
        self.assertFalse(response.session_state["degraded"])
        self.note("F03", response, path="code_fallback", input=NAMED)

    def test_f04_partial_fields_and_zero_price_keep_only_verified_values(self):
        variants = [
            ("missing_stock", {"price": 199.0, "stock": None}, ["标价 199", "库存暂未核实"], ["None", "缺货", "无库存"]),
            ("missing_price", {"price": None, "stock": 8}, ["库存 8 件", "价格暂未核实"], ["None", "缺货"]),
            ("zero_price", {"price": 0, "stock": 4}, ["标价 0", "库存 4 件"], ["暂未核实", "None"]),
            ("negative_stock", {"price": 50, "stock": -1}, ["标价 50", "库存暂未核实"], ["-1", "None"]),
            ("string_price", {"price": "199", "stock": 3}, ["库存 3 件", "价格暂未核实"], ["199", "None"]),
        ]
        for path in ("code_fallback", "model_tool"):
            for name, fields, required, banned in variants:
                if path == "model_tool" and name != "missing_stock":
                    continue
                with self.subTest(path=path, name=name):
                    self.search_mock.return_value = [{**CHAIR, **fields}]
                    session_id = f"f04-{path}-{name}"
                    if path == "model_tool":
                        response, spy = self.liar(NAMED, session_id)
                        spy.assert_not_called()
                        self.assertTrue(response.session_state["tool_calling"]["create_agent"])
                    else:
                        response = self.chat(NAMED, session_id=session_id)
                    call = response.tool_calls[-1]
                    self.assertEqual(call.status, "success")
                    self.assertIsNone(call.error_type)
                    self.assertIn("轻量露营折叠椅", response.answer)
                    for text in required:
                        self.assertIn(text, response.answer)
                    for text in banned:
                        self.assertNotIn(text, response.answer)
                    self.assertFalse(response.session_state["degraded"])
                    if name == "zero_price":
                        self.assertNotIn("missing_fields", call.arguments)
                    else:
                        self.assertTrue(call.arguments["missing_fields"])
                        self.assertEqual(
                            response.session_state["model"]["final_answer"]["fallback_reason"],
                            "verified_tool_wording",
                        )
                    self.note("F04", response, path=path, variant=name, input=NAMED)

    def test_f05_unusable_rows_are_not_reported_as_no_match(self):
        self.search_mock.return_value = [{"price": 199, "stock": 1}]
        _products, call = search_products("折叠椅", match_text=QUESTION)
        self.assertEqual(call.status, "error")
        self.assertEqual(call.error_type, "business_api_unavailable")
        self.assertNotEqual(call.arguments.get("result"), "no_match")
        self.assertIn("没有成功", call.output_summary)

    def test_f06_failure_keeps_refund_draft_and_reread_uses_new_facts(self):
        session_id = "f06"
        open_task = {"intent": "refund_request", "pending_slot": "reason", "candidates": [{"value": "ORD9801", "label": "ORD9801"}]}
        current_memory(session_id, "U1001")["open_task"] = dict(open_task)
        context = {"mallAfterSaleRouting": True, "afterSaleDraft": {
            "order": "ORD9801", "valid": True, "ownerVerified": True, "version": 3, "token": "card-1",
        }}
        self.search_mock.return_value = [CHAIR]
        self.detail_mock.side_effect = [
            (None, "timeout"),
            ({**CHAIR, "price": 910.0, "stock": 2, "_fact_source": "business_api"}, None),
        ]
        first = self.chat(NAMED, context, session_id)
        self.assertIn("899", first.answer)
        self.assertIn("库存 36 件", first.answer)
        self.assertEqual(current_memory(session_id, "U1001")["open_task"], open_task)
        failed = self.chat("刚才那款还有货吗？", context, session_id)
        self.assertIn("没有成功", failed.answer)
        self.assertNotIn("899", failed.answer)
        self.assertNotIn("36", failed.answer)
        self.assertEqual(failed.tool_calls[-1].error_type, "timeout")
        self.assertTrue(failed.session_state["degraded"])
        self.assertEqual(failed.session_state["intent"], "product_query")
        self.assertFalse(failed.session_state["needs_human_approval"])
        self.assertIsNone(failed.session_state["workflow"])
        memory = current_memory(session_id, "U1001")
        self.assertEqual(memory["open_task"], open_task)
        self.assertEqual(memory["product_query"]["product_id"], "4")
        recovered = self.chat("刚才那款多少钱？", context, session_id)
        self.assertIn("910", recovered.answer)
        self.assertIn("库存 2 件", recovered.answer)
        self.assertNotIn("899", recovered.answer)
        self.assertNotIn("36", recovered.answer)
        self.assertNotIn("没有成功", recovered.answer)
        self.assertFalse(recovered.session_state["degraded"])
        self.assertEqual(recovered.session_state["intent"], "product_query")
        self.assertIsNone(recovered.session_state["workflow"])
        memory = current_memory(session_id, "U1001")
        self.assertEqual(memory["open_task"], open_task)
        self.assertEqual(memory["product_query"]["product_id"], "4")
        self.assertEqual(self.detail_mock.call_args_list[-1].args[0], "4")
        self.note("F06", failed, path="code_fallback", input="刚才那款还有货吗？", stage="failed")
        self.note("F06", recovered, path="code_fallback", input="刚才那款多少钱？", stage="recovered")

    @classmethod
    def tearDownClass(cls):
        if len(cls.records) < 12:
            return
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "result.json").write_text(json.dumps(cls.records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
