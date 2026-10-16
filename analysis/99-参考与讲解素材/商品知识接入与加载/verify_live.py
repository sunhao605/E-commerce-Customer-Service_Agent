from pathlib import Path
import json,urllib.request,urllib.parse
BASE=Path('/private/tmp/product-rag-load-20261012');cases=json.loads((BASE/'browser-acceptance.json').read_text());session=cases[-1]['response']['sessionId']
with urllib.request.urlopen('http://127.0.0.1:8000/sessions/'+urllib.parse.quote(session,safe='')+'/trace',timeout=5) as response:events=json.load(response)
(BASE/'live-trace.json').write_text(json.dumps(events,ensure_ascii=False,indent=2)+'\n')
rag=[e for e in events if e['event_type']=='rag_pre_retrieved' and e['payload'].get('policy_id')=='outdoor_product_guide']
hybrid=[e for e in events if e['event_type']=='rag_hybrid_retrieved']
assert len(rag)==3 and len(hybrid)==3
assert all(e['payload']['index_chunk_count']==85 for e in hybrid)
assert all(x['passed'] and not x['response']['confirmRequired'] and not x['response']['fallback'] for x in cases)
assert '2499' in cases[-1]['visible_answer'] and '1999' in cases[-1]['visible_answer'] and '180' in cases[-1]['visible_answer']
with urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3) as response:health=json.load(response)
assert health['rag_index']['loaded'] and health['rag_index']['chunk_count']==85
summary={'session':session,'page_cases':3,'rag_events':len(rag),'hybrid_events':len(hybrid),'section_ids':[e['payload'].get('section_ids') for e in rag],'model_route_used_count':sum(bool(e['payload'].get('used_model')) for e in events if e['event_type']=='model_route_planned'),'final_answer_model_skipped_count':sum(e['event_type']=='model_answer_skipped' for e in events),'tool_event_count':sum(e['event_type']=='tool_finished' for e in events),'no_confirmation':True,'health':health}
(BASE/'live-verified.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False,indent=2))
