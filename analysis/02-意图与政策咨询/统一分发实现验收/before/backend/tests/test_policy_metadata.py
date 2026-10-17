"""PM01–PM18：固定时钟与完整测试快照；无在线模型或业务写入。"""
import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

from api.schemas import ChatRequest
from agent.context import SESSION_MEMORIES, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.models.router_client import ModelRouteResult
from agent.state import COMMON_HIT_CACHE, WORKFLOW_CHECKPOINTS
from agent.trace import trace_store
from retrieval import documents as docs, hybrid_retrieval as hybrid, knowledge


@contextmanager
def at_time(value):
    token = docs._REQUEST_TIME.set(datetime.fromisoformat(value))
    try:
        yield
    finally:
        docs._REQUEST_TIME.reset(token)


class PolicyMetadataTest(unittest.TestCase):
    def setUp(self):
        self.original_dir = docs.KNOWLEDGE_DIR
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        for name in (*docs.KNOWLEDGE_FILES, "release-manifest.json"):
            shutil.copy2(self.original_dir / name, self.directory / name)
        self.directory_patch = patch.object(docs, "KNOWLEDGE_DIR", self.directory)
        self.directory_patch.start()
        self.reload_snapshot()
        SESSION_MEMORIES.clear()
        self.saved_cache = dict(COMMON_HIT_CACHE)
        COMMON_HIT_CACHE.clear()
        self.agent = Lesson41Agent()
        for target, method, options in (
            (self.agent.route_model_client, "plan_intent", {"side_effect": lambda message, **kw:
                ModelRouteResult(intent=kw["fallback_intent"], used_model=True,
                    inquiry_topics={"invoice_query": ["invoice_issue"], "product_query": ["promotion_stacking"]}.get(kw["fallback_intent"], []), topic_status="valid")}),
            (self.agent.answer_model_client, "can_call_model", {"return_value": False}),
            (self.agent.langchain_tool_runner, "can_call_model", {"return_value": False}),
        ):
            mocked = patch.object(target, method, **options)
            mocked.start()
            self.addCleanup(mocked.stop)

    def tearDown(self):
        self.directory_patch.stop()
        self.reload_snapshot()
        self.temp.cleanup()
        SESSION_MEMORIES.clear()
        COMMON_HIT_CACHE.clear()
        COMMON_HIT_CACHE.update(self.saved_cache)

    def reload_snapshot(self):
        docs.get_knowledge_snapshot.cache_clear()
        hybrid.reset_hybrid_index_and_cache()

    def material(self, name, *, remove=None, body=None, same_version=False, **updates):
        path = self.directory / name
        raw = path.read_text()
        front, original_body = raw[4:].split("\n---\n", 1)
        metadata = yaml.safe_load(front)
        if not same_version:
            metadata["version"] += ".test"
        metadata.update(updates)
        if remove:
            metadata.pop(remove)
        path.write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
                        + "---\n" + (original_body if body is None else body))

    def chat(self, message, session="metadata-test"):
        return self.agent.chat(ChatRequest(session_id=session, runtime_user_id="U1001", user_message=message))

    def test_pm01_complete_snapshot_and_citation_metadata(self):
        snapshot = docs.get_knowledge_snapshot()
        self.assertEqual(set(snapshot.citations), set(docs.KNOWLEDGE_FILES))
        index, _ = hybrid.get_knowledge_index()
        self.assertIn("received_return_policy.md", hybrid.KNOWLEDGE_FILES)
        self.assertTrue(all(set(docs.POLICY_FIELDS).issubset(d.metadata) for d in index.documents))
        self.assertTrue(all(d.metadata["snapshot_version"] == snapshot.version for d in index.documents))
        returned = knowledge.policy_query_result("pm01", "return_policy_query")
        self.assertEqual(returned.citations[0].metadata["snapshot_version"], snapshot.version)
        self.assertEqual(returned.citations[0].metadata["policy_id"], "return_after_delivery")

    def test_pm02_invalid_metadata_blocks_whole_batch(self):
        invalid = [{"remove": "publication_status"}, {"version": ""}, {"version": 1},
                   {"publication_status": "approved"}, {"effective_from": None},
                   {"effective_from": "2025-01-01"}, {"effective_from": 2025},
                   {"effective_to": "2024-01-01T00:00:00+08:00"},
                   {"effective_to": "2025-01-01T00:00:00+08:00"},
                   {"policy_id": "invoice_issue"}]
        for updates in invalid:
            with self.subTest(updates=updates):
                shutil.copy2(self.original_dir / "after_sale_policy.md", self.directory / "after_sale_policy.md")
                self.material("after_sale_policy.md", **updates)
                self.reload_snapshot()
                with self.assertRaisesRegex(ValueError, "policy.md"):
                    docs.get_knowledge_snapshot()
        from fastapi.testclient import TestClient
        from api.routes import app
        with self.assertRaises(ValueError), TestClient(app):
            pass
        shutil.copy2(self.original_dir / "after_sale_policy.md", self.directory / "after_sale_policy.md")
        for malformed_manifest in ([], {"after_sale_policy.md": {"version": "old"}}):
            with self.subTest(manifest=malformed_manifest):
                (self.directory / "release-manifest.json").write_text(json.dumps(malformed_manifest))
                self.reload_snapshot()
                with self.assertRaisesRegex(ValueError, "release-manifest.json"):
                    docs.get_knowledge_snapshot()

    def test_pm03_draft_and_withdrawn_cannot_return_through_either_route(self):
        for status in ("draft", "withdrawn"):
            with self.subTest(status=status):
                for name in ("promotion_policy.md", "member_coupon_policy.md", "received_return_policy.md"):
                    self.material(name, publication_status=status)
                self.reload_snapshot()
                result = hybrid.retrieve_knowledge("露营季会员券叠加", "promotion_query")
                self.assertEqual(result.citations, [])
                self.assertEqual(result.debug["vector_policy_ids"], [])
                self.assertEqual(result.debug["keyword_policy_ids"], [])
                direct = knowledge.policy_query_result("pm03", "return_policy_query")
                self.assertEqual(direct.citations, [])
                self.assertEqual(direct.retrieval_debug["reason"], status)

    def test_pm04_start_inclusive_end_exclusive(self):
        self.material("payment_invoice_policy.md", effective_from="2026-10-14T10:00:00+08:00",
                      effective_to="2026-10-14T11:00:00+08:00")
        self.reload_snapshot()
        for value, valid in (("2026-10-14T09:59:59+08:00", False),
                             ("2026-10-14T10:00:00+08:00", True),
                             ("2026-10-14T11:00:00+08:00", False)):
            with self.subTest(value=value), at_time(value):
                direct = knowledge.invoice_faq_result("pm04")
                hybrid_result = hybrid.retrieve_knowledge("电子发票", "invoice_query")
                self.assertEqual(bool(direct.citations), valid)
                self.assertEqual(bool(hybrid_result.citations), valid)

    def test_pm05_explicit_no_end_and_withdrawal(self):
        self.material("payment_invoice_policy.md", effective_to=None)
        self.reload_snapshot()
        with at_time("2030-01-01T00:00:00+08:00"):
            self.assertTrue(knowledge.invoice_faq_result("pm05").citations)
        self.material("payment_invoice_policy.md", publication_status="withdrawn")
        self.reload_snapshot()
        self.assertFalse(knowledge.invoice_faq_result("pm05").citations)
        self.material("payment_invoice_policy.md", remove="effective_to")
        self.reload_snapshot()
        with self.assertRaisesRegex(ValueError, "effective_to"):
            docs.get_knowledge_snapshot()

    def test_pm06_cached_answer_expires_without_rebuilding_index(self):
        self.material("promotion_policy.md", effective_to="2026-10-14T10:00:00+08:00")
        self.reload_snapshot()
        with at_time("2026-10-14T09:59:59+08:00"):
            first = knowledge.promotion_policy_result("pm06", "露营季满减规则")
            warm = knowledge.promotion_policy_result("pm06", "露营季满减规则")
        self.assertTrue(first.citations)
        self.assertTrue(warm.retrieval_debug["retrieval_cache_hit"])
        with at_time("2026-10-14T10:00:00+08:00"):
            expired = knowledge.promotion_policy_result("pm06", "露营季满减规则")
        self.assertEqual(expired.citations, [])
        self.assertNotIn("满 300 减 40", expired.answer)
        self.assertNotIn("券过期", expired.answer)
        self.assertEqual(expired.retrieval_debug["index_version"], first.retrieval_debug["index_version"])
        self.assertEqual(expired.retrieval_debug["blocked_policies"]["promotion_camping_season_stack_rule"], "expired")

    def test_pm07_empty_cache_does_not_hide_newly_effective_policy(self):
        self.material("promotion_policy.md", effective_from="2026-10-14T10:00:00+08:00")
        self.material("member_coupon_policy.md", publication_status="draft")
        self.reload_snapshot()
        with at_time("2026-10-14T09:59:59+08:00"):
            empty = hybrid.retrieve_knowledge("露营季满减", "promotion_query")
            self.assertTrue(hybrid.retrieve_knowledge("露营季满减", "promotion_query").debug["retrieval_cache_hit"])
        with at_time("2026-10-14T10:00:00+08:00"):
            active = hybrid.retrieve_knowledge("露营季满减", "promotion_query")
        self.assertFalse(empty.citations)
        self.assertTrue(active.citations)
        self.assertNotEqual(empty.debug["cache_key"], active.debug["cache_key"])
        self.assertFalse(active.debug["retrieval_cache_hit"])

    def test_pm08_cache_citations_revalidated_on_hit(self):
        for field, value in (("version", "stale"), ("snapshot_version", "wrong"),
                             ("publication_status", "withdrawn")):
            with self.subTest(field=field):
                hybrid.reset_hybrid_index_and_cache()
                result = hybrid.retrieve_knowledge("帐篷怎么收纳", "product_query")
                self.assertTrue(result.citations)
                cached = hybrid.RAG_RETRIEVAL_CACHE[result.debug["cache_key"]]
                cached.citations[0].metadata[field] = value
                refreshed = hybrid.retrieve_knowledge("帐篷怎么收纳", "product_query")
                self.assertFalse(refreshed.debug["retrieval_cache_hit"])
                self.assertNotEqual(refreshed.citations[0].metadata[field], value)

    def test_pm09_after_sale_attachment_blocked_without_relaxing_eligibility(self):
        self.material("after_sale_policy.md", effective_to="2026-01-01T00:00:00+08:00")
        self.reload_snapshot()
        direct = self.chat("退款政策是什么？只咨询")
        self.assertFalse(direct.citations)
        self.assertIsNone(direct.session_state["workflow"])
        order = {"orderNo": "ORD1234", "userId": "U1001", "paymentStatus": "PAID",
                 "status": "PAID_PENDING_SHIPMENT", "logisticsStatus": "NOT_SHIPPED"}
        with patch("agent.tools.tool_runtime.order_fact_from_ecommerce", return_value=order):
            eligible = self.chat("帮我申请 ORD1234 退款", session="pm09-eligible")
        self.assertFalse(eligible.citations)
        self.assertIsNotNone(eligible.session_state["workflow"])
        self.assertEqual(eligible.session_state["workflow"]["policy_ids"], [])
        order["status"] = "SHIPPED"
        order["logisticsStatus"] = "SHIPPED"
        with patch("agent.tools.tool_runtime.order_fact_from_ecommerce", return_value=order):
            blocked = self.chat("帮我申请 ORD1234 退款", session="pm09-shipped")
        self.assertIsNone(blocked.session_state["workflow"])
        self.assertFalse(blocked.citations)
        self.material("received_return_policy.md", effective_to="2026-01-01T00:00:00+08:00")
        self.reload_snapshot()
        order.update(status="SIGNED", logisticsStatus="SIGNED", signedAt="2026-07-14", returnable=True)
        with patch("agent.tools.tool_runtime.order_fact_from_ecommerce", return_value=order):
            returned = self.chat("我要申请 ORD1234 七天无理由退货", session="pm09-return")
        self.assertFalse(returned.citations)
        self.assertEqual(returned.session_state["workflow"]["policy_ids"], [])

    def test_pm10_expired_invoice_ignores_legacy_final_answer_cache(self):
        self.material("payment_invoice_policy.md", effective_to="2026-01-01T00:00:00+08:00")
        self.reload_snapshot()
        COMMON_HIT_CACHE["faq:invoice_issue"] = {"answer": "旧发票承诺"}
        response = self.chat("电子发票在哪里下载？")
        self.assertFalse(response.citations)
        self.assertNotIn("旧发票承诺", response.answer)
        self.assertNotIn("24 小时", response.answer)
        self.assertEqual(COMMON_HIT_CACHE["faq:invoice_issue"], {"answer": "旧发票承诺"})

    def test_pm11_valid_invoice_queries_check_admission_without_final_cache(self):
        for message in ("电子发票多久开具？", "电子发票在哪里下载？"):
            response = self.chat(message)
            self.assertEqual(response.citations[0].metadata["policy_id"], "invoice_issue")
            self.assertEqual(response.session_state["rag"]["admission"]["snapshot_version"],
                             response.citations[0].metadata["snapshot_version"])
            self.assertNotIn("faq:invoice_issue", COMMON_HIT_CACHE)
            self.assertIsNone(response.session_state["cost_summary"]["cache"]["cache_key"])
            self.assertEqual(response.session_state["route_plan"]["retrieval_strategies"][0]["retrieval_mode"], "hybrid")

    def test_pm12_live_price_preserved_when_promotion_unavailable(self):
        for name in ("promotion_policy.md", "member_coupon_policy.md"):
            self.material(name, publication_status="withdrawn")
        self.reload_snapshot()
        chair = {"id": 4, "name": "轻量露营折叠椅", "price": 899.0, "stock": 3, "active": True}
        with patch("agent.tools.tool_runtime.products_from_ecommerce", return_value=[chair]), \
             patch("agent.tools.tool_runtime.product_detail_from_ecommerce", return_value=({**chair, "_fact_source": "business_api"}, None)):
            response = self.chat("轻量露营折叠椅多少钱，露营季满减和会员券能叠加吗？")
        self.assertIn("899", response.answer)
        self.assertIn("暂不能确认", response.answer)
        self.assertNotIn("满 300 减 40", response.answer)
        self.assertFalse(response.citations)
        self.assertTrue(response.tool_calls)

    def test_pm13_full_reload_replaces_snapshot_and_direct_citations(self):
        first = hybrid.retrieve_knowledge("电子发票", "invoice_query")
        old_snapshot = docs.get_knowledge_snapshot().version
        self.material("payment_invoice_policy.md", body="新版本发票说明。\n")
        self.reload_snapshot()
        second = hybrid.retrieve_knowledge("电子发票", "invoice_query")
        direct = knowledge.designated_policy_result("pm13", "payment_invoice_policy.md", "invoice_query")
        self.assertNotEqual(first.debug["index_version"], second.debug["index_version"])
        self.assertNotEqual(old_snapshot, direct.citations[0].metadata["snapshot_version"])
        self.assertEqual(direct.citations[0].snippet, "新版本发票说明。")
        self.assertEqual(knowledge.invoice_faq_result("pm13").answer, "新版本发票说明。")
        self.assertFalse(second.debug["retrieval_cache_hit"])

    def test_pm14_same_version_edit_rejected_and_index_reset_keeps_loaded_snapshot(self):
        initial = hybrid.retrieve_knowledge("电子发票", "invoice_query")
        self.material("payment_invoice_policy.md", body="未经发布的新正文\n", same_version=True)
        hybrid.reset_hybrid_index_and_cache()
        rebuilt = hybrid.retrieve_knowledge("电子发票", "invoice_query")
        self.assertEqual(initial.debug["index_version"], rebuilt.debug["index_version"])
        self.assertNotIn("未经发布", rebuilt.citations[0].snippet)
        self.reload_snapshot()
        with self.assertRaisesRegex(ValueError, "new version"):
            docs.get_knowledge_snapshot()

    def test_pm15_direct_only_return_policy_participates_in_fingerprint(self):
        initial_index, _ = hybrid.get_knowledge_index()
        old_snapshot = docs.get_knowledge_snapshot().version
        self.material("received_return_policy.md", body="退货政策新版本\n")
        self.reload_snapshot()
        next_index, _ = hybrid.get_knowledge_index()
        returned = knowledge.policy_query_result("pm15", "return_policy_query")
        self.assertNotEqual(initial_index.version, next_index.version)
        self.assertNotEqual(old_snapshot, docs.get_knowledge_snapshot().version)
        self.assertEqual(returned.citations[0].snippet, "退货政策新版本")

    def test_pm16_rollback_expired_or_withdrawn_policy_is_not_usable(self):
        old = (self.directory / "promotion_policy.md").read_text()
        self.material("promotion_policy.md", publication_status="withdrawn")
        self.reload_snapshot()
        self.assertFalse(knowledge.promotion_policy_result("pm16", "露营季活动规则").citations)
        self.assertEqual(docs.knowledge_admission()["blocked_policies"]["promotion_camping_season_stack_rule"], "withdrawn")
        # 人工恢复旧文件也不能跳过当前时间；没有自动回滚入口。
        (self.directory / "promotion_policy.md").write_text(old)
        self.reload_snapshot()
        with at_time("2028-01-01T00:00:00+08:00"):
            self.assertFalse(knowledge.promotion_policy_result("pm16", "露营季活动规则").citations)

    def test_pm17_no_published_or_no_current_policies_are_normal_empty_results(self):
        for name in docs.KNOWLEDGE_FILES:
            self.material(name, publication_status="draft", effective_from=None, effective_to=None)
        self.reload_snapshot()
        with patch.object(hybrid, "build_course_embeddings", side_effect=AssertionError("empty knowledge needs no embedding")):
            index, _ = hybrid.get_knowledge_index()
        self.assertEqual(index.documents, [])
        self.assertFalse(hybrid.retrieve_knowledge("帐篷收纳", "product_query").citations)
        self.assertFalse(knowledge.invoice_faq_result("pm17").citations)
        for name in docs.KNOWLEDGE_FILES:
            shutil.copy2(self.original_dir / name, self.directory / name)
        self.reload_snapshot()
        with at_time("2028-01-01T00:00:00+08:00"):
            self.assertFalse(hybrid.retrieve_knowledge("帐篷收纳", "product_query").citations)
            self.assertFalse(knowledge.invoice_faq_result("pm17").citations)

    def test_pm18_knowledge_change_preserves_product_candidates_and_refund_card(self):
        candidates = [{"id": "1", "name": "露营折叠椅"}, {"id": "4", "name": "轻量露营折叠椅"}]
        memory = current_memory("pm18", "U1001")
        memory["product_query"] = {"pending_candidates": candidates, "original_question": "椅子多少钱？"}
        draft = {"intent": "refund_request", "pending_slot": "reason", "order_id": "ORD1234"}
        memory["open_task"] = dict(draft)
        key = ("pm18", "pm18-workflow")
        checkpoint = {"approval_token": "test-only-token", "status": "pending_approval"}
        WORKFLOW_CHECKPOINTS[key] = checkpoint
        try:
            hybrid.retrieve_knowledge("电子发票", "invoice_query")
            self.material("payment_invoice_policy.md", publication_status="withdrawn")
            self.reload_snapshot()
            result = knowledge.invoice_faq_result("pm18")
            self.assertFalse(result.citations)
            self.assertEqual(memory["product_query"]["pending_candidates"], candidates)
            self.assertEqual(memory["open_task"], draft)
            self.assertEqual(WORKFLOW_CHECKPOINTS[key], checkpoint)
            chair = {"id": 4, "name": "轻量露营折叠椅", "price": 899.0, "stock": 3, "active": True}
            with patch("agent.tools.tool_runtime.product_detail_from_ecommerce", return_value=({**chair, "_fact_source": "business_api"}, None)):
                response = self.chat("第二款", session="pm18")
            self.assertEqual(response.session_state["product_query"]["product_id"], "4")
            self.assertEqual(memory["open_task"], draft)
            self.assertEqual(WORKFLOW_CHECKPOINTS[key], checkpoint)
        finally:
            WORKFLOW_CHECKPOINTS.pop(key, None)

    def test_one_request_uses_one_time_across_knowledge_domains(self):
        self.material("promotion_policy.md", effective_to="2026-10-14T10:00:00+08:00")
        self.reload_snapshot()
        with patch.object(docs, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat("2026-10-14T09:59:59+08:00")
            response = self.chat("轻量露营折叠椅怎么保养，露营季活动规则呢？")
            clock.now.assert_called_once()
        self.assertEqual(response.session_state["rag"]["admission"]["judged_at"], "2026-10-14T09:59:59+08:00")
        self.assertTrue(any(c.metadata["policy_id"] == "promotion_camping_season_stack_rule" for c in response.citations))

    def test_blocked_invoice_never_reaches_final_model(self):
        self.material("payment_invoice_policy.md", publication_status="withdrawn")
        self.reload_snapshot()
        with patch.object(self.agent.answer_model_client, "compose_answer", return_value=None) as compose:
            response = self.chat("电子发票多久能准备好？")
        compose.assert_not_called()
        self.assertFalse(response.citations)
        self.assertIn("发票", response.answer)
        self.assertTrue(any(e.event_type == "rag_policy_blocked" for e in trace_store.list("metadata-test")))

    def test_long_policy_citation_is_located_section(self):
        result = knowledge.promotion_policy_result("bc021", "露营季满300减40一般规则，能否和会员券叠加？")
        promotion = next(c for c in result.citations if c.metadata["policy_id"] == "promotion_camping_season_stack_rule")
        member = next(c for c in result.citations if c.metadata["policy_id"] == "member_coupon_gold_rule")
        self.assertIn("4.", promotion.metadata["section"])
        self.assertIn("每满", promotion.snippet)
        self.assertLess(len(promotion.snippet), 1200)
        self.assertNotIn("## 8.", promotion.snippet)
        self.assertIn("会员", member.snippet)  # 叠加正文只有活动文件维护；会员文件仅引用身份。
        self.assertLess(len(member.snippet), 1200)
        self.assertNotEqual(promotion.snippet, member.snippet)
        invoice = hybrid.retrieve_knowledge("电子发票通常多久", "invoice_query")
        self.assertEqual(len(invoice.citations), 1)
        self.assertIn("24 小时", invoice.citations[0].snippet)
        self.assertEqual(invoice.citations[0].metadata.get("rule_id"), "invoice_issue")

    def test_refund_and_return_answers_use_indexed_sections(self):
        refund = knowledge.policy_query_result("bc021-refund", "refund_policy_query", "未发货退款什么时候到账？")
        returned = knowledge.policy_query_result("bc021-return", "return_policy_query", "签收后退货的运费谁承担？")
        full_refund = docs.load_knowledge_citation("after_sale_policy.md").snippet
        full_return = docs.load_knowledge_citation("received_return_policy.md").snippet
        self.assertIn("received_return_policy.md", hybrid.KNOWLEDGE_FILES)
        self.assertTrue(all(len(item.snippet) < len(full_refund) / 2 for item in refund.citations))
        self.assertTrue(all(len(item.snippet) < len(full_return) / 2 for item in returned.citations))
        from retrieval.rules import select_rules, rules_answer
        selected, selection = select_rules(refund.citations, ["refund_arrival"], ["after_sale_policy"])
        self.assertIn("到账", rules_answer(selected, selection))
        selected, selection = select_rules(returned.citations, ["return_shipping_cost"], ["received_return_policy"])
        self.assertIn("7.", selected[0].metadata["section"])
        self.assertIn("运费", rules_answer(selected, selection))
        self.assertTrue(all("常见客服问法" not in item.snippet for item in refund.citations))
        self.assertTrue(all("常见客服对话" not in item.snippet for item in returned.citations))
        self.assertEqual(refund.retrieval_debug["mode"], "hybrid_chunk_fusion")

    def test_customer_knowledge_keeps_rules_limits_and_safe_use_without_internal_guidance(self):
        required = {
            "after_sale_policy.md": ("已支付", "未出库", "整单", "确认", "待审核", "重复", "到账时限"),
            "received_return_policy.md": ("上海日期", "168小时", "购买时", "无需另外填写", "验收", "运费", "到付"),
            "promotion_policy.md": ("300", "40", "平台会员券叠加", "同类型满减券", "每满", "结算"),
            "member_coupon_policy.md": ("有效期", "不能转让", "所属账号", "状态", "品类券", "返券"),
            "product_knowledge.md": ("晾干", "冷凝", "停止承重", "不要自行钻孔", "没有提供温标", "质量问题核验"),
            "payment_invoice_policy.md": ("24 小时", "订单详情页"),
        }
        for name, terms in required.items():
            with self.subTest(name=name):
                body = docs.load_knowledge_citation(name).snippet
                for term in terms:
                    self.assertIn(term, body)
                for internal in ("顾客问", "客服应", "模拟问法", "模拟样例", "以下为编写", "本文是项目演示", "种子数据"):
                    self.assertNotIn(internal, body)


if __name__ == "__main__":
    unittest.main()
