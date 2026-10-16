"""商品知识接入：片段证据、实时事实、澄清和售后状态的边界。"""
import unittest
from unittest.mock import patch
from api.schemas import ChatRequest
from agent.context import SESSION_MEMORIES,current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.models.router_client import ModelRouteResult
from agent.tools.planning import build_route_plan,classify_intent
from retrieval import knowledge,hybrid_retrieval as hybrid

CHAIR={'id':4,'name':'轻量露营折叠椅','price':899.0,'stock':3,'active':True}

class ProductKnowledgeTest(unittest.TestCase):
    def setUp(self):
        SESSION_MEMORIES.clear();hybrid.reset_hybrid_index_and_cache()
        self.agent=Lesson41Agent()
        for target,method in [(self.agent.route_model_client,'plan_intent'),(self.agent.answer_model_client,'can_call_model'),(self.agent.langchain_tool_runner,'can_call_model')]:
            mock=patch.object(target,method,side_effect=(lambda message,**kw:ModelRouteResult(intent=kw['fallback_intent'],used_model=False)) if method=='plan_intent' else None,return_value=False)
            mock.start();self.addCleanup(mock.stop)
        self.search=patch('agent.tools.tool_runtime.products_from_ecommerce',return_value=[CHAIR]);self.search_mock=self.search.start();self.addCleanup(self.search.stop)
        self.detail=patch('agent.tools.tool_runtime.product_detail_from_ecommerce',return_value=({**CHAIR,'_fact_source':'business_api'},None));self.detail_mock=self.detail.start();self.addCleanup(self.detail.stop)

    def chat(self,message):return self.agent.chat(ChatRequest(session_id='product-knowledge-test',runtime_user_id='U1001',user_message=message))
    def guide_citations(self,r):return [c for c in r.citations if c.metadata.get('policy_id')=='outdoor_product_guide']

    def test_general_tent_condensation_needs_no_product_lookup(self):
        r=self.chat('帐篷里面有水是怎么回事？')
        self.assertTrue(self.guide_citations(r));self.assertIn('冷凝',r.answer)
        self.assertEqual(r.tool_calls,[]);self.search_mock.assert_not_called();self.detail_mock.assert_not_called()
        self.assertNotIn('899',r.answer)

    def test_plan_distinguishes_knowledge_from_price(self):
        plan=build_route_plan(intent='product_query',user_message='帐篷怎么保养？',order_id=None,model_used=False)
        self.assertTrue(plan.needs_rag);self.assertFalse(plan.needs_business_tools)
        mixed=build_route_plan(intent='product_query',user_message='轻量露营折叠椅多少钱，支架怎么展开？',order_id=None,model_used=False)
        self.assertTrue(mixed.needs_rag);self.assertTrue(mixed.needs_business_tools)

    def test_sleeping_bag_missing_temperature_is_not_invented(self):
        r=self.chat('睡袋零下五度能睡吗，温标怎么看？')
        cites=self.guide_citations(r);self.assertTrue(cites)
        self.assertIn('温标',r.answer);self.assertIn('没有',r.answer)
        self.assertTrue(any('睡袋' in c.metadata['section'] for c in cites))
        self.assertTrue(all(len(c.snippet)<1500 for c in cites))
        self.assertTrue(all(c.metadata.get('version')=='2026-10-16.1' for c in cites))

    def test_price_with_knowledge_keeps_live_facts(self):
        r=self.chat('轻量露营折叠椅多少钱，支架怎么展开？')
        self.assertIn('899',r.answer);self.assertIn('库存 3 件',r.answer)
        self.assertTrue(self.guide_citations(r));self.assertIn('支架',r.answer)
        self.assertEqual(r.session_state['product_query']['product_id'],'4')

    def test_price_only_does_not_append_guide(self):
        r=self.chat('轻量露营折叠椅多少钱？')
        self.assertIn('899',r.answer);self.assertFalse(self.guide_citations(r))

    def test_general_knowledge_does_not_change_refund_draft(self):
        memory=current_memory('product-knowledge-test','U1001');draft={'intent':'refund_request','pending_slot':'order_id','candidates':[]};memory['open_task']=draft.copy()
        r=self.chat('帐篷怎么保养？')
        self.assertTrue(self.guide_citations(r));self.assertEqual(memory['open_task'],draft)
        self.assertIsNone(r.session_state['workflow'])

    def test_failed_price_lookup_keeps_failure_and_independent_knowledge(self):
        self.search_mock.return_value=None
        r=self.chat('轻量露营折叠椅多少钱，支架怎么展开？')
        self.assertTrue(r.session_state['degraded']);self.assertTrue(self.guide_citations(r));self.assertNotIn('899',r.answer)
        self.assertIn('没有成功', r.answer)

    def test_followup_knowledge_has_product_intent(self):
        self.assertEqual(classify_intent('怎么保养？'),'product_query')

    def test_cache_key_includes_top_k(self):
        a=hybrid.retrieve_knowledge('帐篷漏水及收纳','product_query',top_k=1)
        b=hybrid.retrieve_knowledge('帐篷漏水及收纳','product_query',top_k=3)
        self.assertFalse(b.debug['retrieval_cache_hit']);self.assertNotEqual(a.debug['cache_key'],b.debug['cache_key'])
        self.assertTrue(all(c.metadata['policy_id']=='outdoor_product_guide' for c in b.citations))

    def test_vector_domain_filter_runs_before_top_k(self):
        index,_=hybrid.get_knowledge_index()
        with patch.object(index.vector_store,'similarity_search_with_score',wraps=index.vector_store.similarity_search_with_score) as search:
            hybrid.retrieve_knowledge('睡袋温标','product_query')
        filter_fn=search.call_args.kwargs['filter']
        accepted=[doc for doc in index.documents if filter_fn(doc)]
        self.assertTrue(accepted)
        self.assertTrue(all(doc.metadata['scene_key']=='product_knowledge' for doc in accepted))
        self.assertTrue(all('睡袋' in str(doc.metadata) for doc in accepted))

    def test_tent_care_retrieves_operational_section_first(self):
        r=self.chat('帐篷怎么保养？')
        self.assertTrue(self.guide_citations(r))
        self.assertIn('3.3',r.citations[0].metadata['section'])
        self.assertIn('晾干',r.answer)

    def test_sleeping_bag_does_not_retrieve_other_topics(self):
        r=self.chat('睡袋温标怎么看？')
        self.assertTrue(r.citations)
        self.assertTrue(all('5.2' in c.metadata['section'] for c in r.citations))
        self.assertNotIn('会员券',r.answer)

    def test_policy_multi_chunk_score_keeps_best_match(self):
        index,_=hybrid.get_knowledge_index()
        docs=[d for d in index.documents if d.metadata['scene_key']=='promotion']
        with patch.object(index.vector_store,'similarity_search_with_score',return_value=[(docs[0],.95),(docs[1],.12)]),patch.object(hybrid,'_bm25_scores',return_value={}):
            r=hybrid.retrieve_knowledge('zzzz9876','promotion_query')
        self.assertEqual(r.debug['source_scores']['promotion_camping_season_stack_rule']['vector'],.95)

    def test_startup_preloads_index_for_health(self):
        from fastapi.testclient import TestClient
        from api.routes import app
        hybrid.reset_hybrid_index_and_cache()
        with TestClient(app) as client:
            health=client.get('/health').json()
        self.assertTrue(health['rag_index']['loaded'])
        self.assertGreater(health['rag_index']['chunk_count'],0)
        self.assertEqual(health['rag_index']['chunk_count'],len(hybrid.get_knowledge_index()[0].documents))

    def test_mixed_query_answers_storage_section_not_product_overview(self):
        tent={**CHAIR,'name':'山野四人帐篷'}
        self.search_mock.return_value=[tent]
        self.detail_mock.return_value=({**tent,'_fact_source':'business_api'},None)
        r=self.chat('山野四人帐篷多少钱，怎么收纳？')
        self.assertTrue(self.guide_citations(r))
        self.assertTrue(any('3.3' in c.metadata['section'] for c in self.guide_citations(r)))
        self.assertIn('晾干',r.answer)

    def test_customer_answer_does_not_copy_markdown_headers(self):
        r=self.chat('帐篷怎么保养？')
        self.assertGreaterEqual(len(r.citations),1)
        self.assertTrue(all(len(c.snippet) < 400 for c in r.citations))
        self.assertNotIn('###',r.answer)
        self.assertNotIn('](http',r.answer)
        self.assertIn('###',r.citations[0].snippet)

    def test_product_name_does_not_trigger_unasked_knowledge(self):
        from agent.product_query import needs_product_knowledge
        self.assertFalse(needs_product_knowledge('户外防水收纳袋套装多少钱？'))
        self.assertFalse(needs_product_knowledge('全地形营地收纳车还有货吗？'))
        self.assertTrue(needs_product_knowledge('防水袋防水等级怎么看？'))

if __name__=='__main__':unittest.main()
