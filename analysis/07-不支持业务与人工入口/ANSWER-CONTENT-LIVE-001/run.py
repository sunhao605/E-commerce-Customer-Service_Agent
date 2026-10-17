"""真实路由/工具选择/生成/judge；知识与业务数据隔离，冻结后单次运行。"""
import copy
import hashlib
import importlib.util
import json
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'backend'))
GUARD_PATH = OUT.parent / 'BC029-四条跨出口验收/run.py'
spec = importlib.util.spec_from_file_location('existing_acceptance_guard', GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
from agent.answer_review import review_audit, redact_review
from agent.context import SESSION_MEMORIES, current_memory
from agent.customer_service_agent import Lesson41Agent
from agent.models.answer_client import ModelInvocationResult
from agent.service_capabilities import catalog_payload
from agent.workflows.after_sale_graph import REFUND_APPROVAL_GRAPH, RECEIVED_RETURN_GRAPH
from api.schemas import ChatRequest, HistoryMessage
from retrieval.documents import get_knowledge_snapshot
from settings import answer_judge_settings, openai_model_name, openai_base_url, api_key_is_missing
from tests.test_answer_judge import interaction_policy_fixture

PROMO = '露营季满300减40能和平台会员券叠加吗？另外支持到货提醒吗？'
BAD = '露营季满300减40可以和平台会员券叠加，同类型满减券不能重复叠加，具体适用需核验。我已为您登记到货提醒，到货后会主动通知您。'
PRODUCTS = [{'id': 4, 'name': '山野四人帐篷', 'price': 899.0, 'stock': 3, 'active': True, 'returnable': True}]
DRAFT = {'intent': 'refund_request', 'pending_slot': 'order_id', 'candidates': []}
CASES = [
 {'id':'N01','question':PROMO,'expected':'可叠加及同类券/本人/有效时间/适用边界；提醒不支持；无退券扩写','budget':5},
 {'id':'N02','question':'先问一下，售罄以后能给我个信儿吗？再问露营季满减和平台会员券是不是能一起用？','expected':'先回应提醒不支持，再回应叠加及条件；不承诺盯库存','budget':5},
 {'id':'N03','question':'露营季满减和平台会员券可以叠加吗？如果订单退款，券会自动退回并延长有效期吗？','expected':'同时回答叠加条件和退券/延期没有统一自动恢复承诺','budget':5},
 {'id':'N04','question':'我只了解未发货退款的到账时间，银行卡或微信一般几天能到账？不申请退款。','expected':'演示余额与真实渠道分开；没有真实渠道时限保证；不扩写整套申请流程','budget':5},
 {'id':'N05','session':'refund-followup','question':'未发货订单申请退款，需要满足哪些条件？我先了解，不办理。','expected':'本人已支付、未出库未发货、整单及资格待核验，不创建申请','budget':5},
 {'id':'N06','session':'refund-followup','history_from':'N05','question':'那多久到账？','expected':'结合前轮按退款到账回答；不问在说哪个主题，不重复退款条件','budget':5},
 {'id':'N07','question':'未发货退款从确认到商家审核，大概经过哪些步骤？这里只咨询流程。','expected':'确认提交、待审核、审批与实际资金结果区分，不宣称已申请或到账','budget':5},
 {'id':'N08','question':'签收后退货，寄回运费谁承担？商家能收到付件吗？','expected':'无统一运费补贴/到付接收保证，依对应订单与商家要求；不扩写全部七天资格','budget':5},
 {'id':'N09','question':'订单完成以后，电子发票多久开好，去哪里下载？','expected':'通常24小时内；订单详情查看下载，无退款政策','budget':5},
 {'id':'N10','question':'贴身露营睡袋能在零下五度睡吗？舒适温度和极限温度有什么区别？','expected':'现有资料无温标，不能确认适用；极限不等于舒适，保留环境/个人差异','budget':5},
 {'id':'N11','question':'山野四人帐篷现在多少钱、还有货吗？用完怎么清洗收纳？露营季满减能和平台会员券一起用吗？另外支持到货提醒吗？','expected':'fixture899元库存3；清洁晾干及安全限制；叠加条件；提醒不支持，不保证具体结算已适用','budget':6},
 {'id':'N12','question':'朋友把山友会员券截图发给我，我能拿来用吗？这张券还没到使用时间。','seed_draft':True,'expected':'券归属本人账号，不转让，未到使用时间不算本人可用；不清已有退款待办','budget':5},
 {'id':'F01','question':PROMO,'injection':'first_generation','expected':'真实路由；首稿为注入登记承诺；真实judge拒绝，至多一次真实修复和复查；不重跑查询','budget':4},
 {'id':'F02','question':PROMO,'injection':'judge_timeout','expected':'真实路由和生成；本地注入judge超时，发送规则要点，无修复；不计自然超时','budget':2},
 {'id':'F03','question':PROMO,'injection':'generation_failure','expected':'真实路由；本地注入生成故障，发送完整要点，不发送错误候选','budget':1},
]
STATE = {'phase':None,'case':None,'stop':None}
MAX_CALLS = 60
TOKEN_STOP = 180000

def dump(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

@contextmanager
def phase(name):
    old = STATE['phase']; STATE['phase'] = name
    try: yield
    finally: STATE['phase'] = old


def setup_guard():
    guard.CASES = CASES; guard.MAX_CALLS = MAX_CALLS
    original = guard.before_send
    def before(request):
        used = sum(item.get('usage',{}).get('total_tokens',0) for item in guard.network)
        if STATE['stop'] or used >= TOKEN_STOP:
            raise RuntimeError('run_stopped_or_token_budget_reached')
        item = original(request)
        body = json.loads(request.content)
        if body.get('model') != openai_model_name() or body.get('stream'):
            raise RuntimeError('unexpected_model_or_stream')
        item['stage'] = STATE['phase']
        return item
    guard.before_send = before
    capture = guard.capture
    def record(item,response,started):
        capture(item,response,started)
        if response.status_code in {401,402,403,429}:
            STATE['stop'] = 'provider_http_' + str(response.status_code)
    guard.capture = record


def config():
    assert guard.urlparse(openai_base_url()).hostname == 'api.deepseek.com'
    assert not api_key_is_missing(os.getenv('AGENT_OPENAI_API_KEY'))
    assert openai_model_name() == 'deepseek-flash'
    mode, judge_model, timeout = answer_judge_settings()
    assert mode == 'enforce' and judge_model == openai_model_name()
    return {'host':'api.deepseek.com','model':openai_model_name(),'judge_model':judge_model,
            'judge_timeout_seconds':timeout,'mode':mode,'max_model_http':MAX_CALLS,
            'stop_before_next_request_at_tokens':TOKEN_STOP,'automatic_retries':0,
            'offline_rag':True,'business_fixtures':True,'generation_temperature':0.2,'judge_temperature':0}


def prepare():
    assert not (OUT/'frozen.json').exists(), '不覆盖冻结'
    guard.self_check()
    frozen={'cases':CASES,'fixtures':{'products':PRODUCTS,'pending_draft':DRAFT,'bad_candidate':BAD,
        'capabilities':{cid:catalog_payload(cid) for cid in ('stock_notification','refund_consultation','return_consultation')},
        'interaction_policy':interaction_policy_fixture()},'config':config(),
        'source_sha256':guard.fingerprints(),'runner_sha256':sha(Path(__file__)),
        'guard_sha256':sha(GUARD_PATH),'knowledge_snapshot':get_knowledge_snapshot().version,
        'scope':'12条自然请求＋3条故障注入；真实路由/必要的工具选择/最终生成/judge；离线知识检索＋构造业务事实，非在线Embedding或Java/MySQL/页面验收',
        'judging':'运行前预期由助手冻结；模型不看expected或标签；运行后逐题人工式复核需求、事实动作、条件、相关重复、表达。judge三维pass不替代体验验收；首次失败不重跑挑结果。'}
    dump('frozen.json',frozen)
    (OUT/'frozen.sha256').write_text(sha(OUT/'frozen.json')+'\n')
    print(json.dumps({'prepared':True,'cases':len(CASES),'real_http':0,'config':frozen['config']},ensure_ascii=False))


def run():
    frozen=json.loads((OUT/'frozen.json').read_text())
    assert sha(OUT/'frozen.json') == (OUT/'frozen.sha256').read_text().strip()
    assert frozen['runner_sha256']==sha(Path(__file__)) and frozen['guard_sha256']==sha(GUARD_PATH)
    assert frozen['source_sha256']==guard.fingerprints() and config()==frozen['config']
    assert not (OUT/'responses.jsonl').exists(), '不覆盖首次运行'
    setup_guard()
    SESSION_MEMORIES.clear(); review_audit.clear()
    agent=Lesson41Agent(); rows=[]; calls=[]; stages=[]; histories={}
    real_route=agent.route_model_client.plan_intent
    real_tools=agent.langchain_tool_runner.run
    real_compose=agent.answer_model_client.compose_answer
    real_invoke=agent.answer_model_client._invoke_chain
    real_judge=agent.answer_judge_client.judge
    def route(*args,**kwargs):
        with phase('route'):
            result=real_route(*args,**kwargs)
        stages.append({'kind':'route','result':result.model_dump(mode='json')})
        return result
    def tools(*args,**kwargs):
        with phase('tool_selection'):return real_tools(*args,**kwargs)
    def compose(*args,**kwargs):
        with phase('generation'): result=real_compose(*args,**kwargs)
        stages.append({'kind':'generation','result':result.model_dump(mode='json'),
                       'shared_content':kwargs.get('review_context')})
        return result
    def invoke(*args,**kwargs):
        case=STATE['case']; n=sum(x['kind']=='generation' for x in stages)
        if case.get('injection')=='first_generation' and n==0:
            stages.append({'kind':'injected_candidate','text':BAD})
            return ModelInvocationResult(content=BAD)
        if case.get('injection')=='generation_failure':
            stages.append({'kind':'injected_generation_failure'})
            raise TimeoutError('local_injection')
        return real_invoke(*args,**kwargs)
    def judge(*args,**kwargs):
        with phase('judge'):result=real_judge(*args,**kwargs)
        stages.append({'kind':'judge','result':result.model_dump(mode='json')})
        return result
    def capability(cid):
        calls.append({'kind':'capability','id':cid}); return catalog_payload(cid)
    def policy():
        calls.append({'kind':'interaction_policy'}); return interaction_policy_fixture()
    def products(*args,**kwargs):
        calls.append({'kind':'products'}); return copy.deepcopy(PRODUCTS)
    def detail(pid,*args,**kwargs):
        calls.append({'kind':'product_detail','id':str(pid)})
        p=next((p for p in PRODUCTS if str(p['id'])==str(pid)),None)
        return ({**p,'_fact_source':'isolated_fixture'},None) if p else (None,'product_not_found')
    def forbidden(*args,**kwargs):raise RuntimeError('business_write_or_unfrozen_read_blocked')
    with ExitStack() as stack:
        guard.install_guards(stack)
        for target,name,replacement in ((agent.route_model_client,'plan_intent',route),
            (agent.langchain_tool_runner,'run',tools),(agent.answer_model_client,'compose_answer',compose),
            (agent.answer_model_client,'_invoke_chain',invoke),(agent.answer_judge_client,'judge',judge),
            (REFUND_APPROVAL_GRAPH,'run',forbidden),(RECEIVED_RETURN_GRAPH,'run',forbidden)):
            stack.enter_context(patch.object(target,name,side_effect=replacement))
        for name,fn in (('agent.customer_service_agent.query_service_capability',capability),
            ('agent.customer_service_agent.query_interaction_policy',policy),
            ('agent.tools.tool_runtime.products_from_ecommerce',products),
            ('agent.tools.tool_runtime.product_detail_from_ecommerce',detail),
            ('agent.tools.tool_runtime.order_fact_from_ecommerce',forbidden),
            ('agent.tools.tool_runtime.after_sale_requests_from_ecommerce',forbidden)):
            stack.enter_context(patch(name,side_effect=fn))
        with (OUT/'responses.jsonl').open('x') as output:
            try:
                for case in frozen['cases']:
                    if STATE['stop']:break
                    STATE['case']=case; guard.active_case=case['id']; calls.clear(); stages.clear(); review_audit.clear()
                    sid='content-live-'+case.get('session',case['id'])
                    if case.get('seed_draft'):current_memory(sid,'SYNTHETIC_USER')['open_task']=copy.deepcopy(DRAFT)
                    history=histories.get(case.get('history_from'),[])
                    started=time.monotonic(); row={'id':case['id'],'question':case['question']}
                    try:
                        with ExitStack() as cs:
                            if case.get('injection')=='judge_timeout':
                                cs.enter_context(patch.object(agent.answer_judge_client,'_request',new=AsyncMock(side_effect=TimeoutError('local_injection'))))
                            response=agent.chat(ChatRequest(session_id=sid,runtime_user_id='SYNTHETIC_USER',
                                user_message=case['question'],history_messages=[HistoryMessage(**h) for h in history]))
                        row['response']=response.model_dump(mode='json')
                        histories[case['id']]=history+[{'role':'user','content':case['question']},{'role':'assistant','content':response.answer}]
                    except Exception as exc:row['error']=type(exc).__name__
                    row.update(elapsed_ms=round((time.monotonic()-started)*1000,3),business_calls=copy.deepcopy(calls),
                               stages=copy.deepcopy(stages),audit=copy.deepcopy(list(review_audit)),
                               network=[x for x in guard.network if x['case']==case['id']])
                    row=redact_review(row); rows.append(row)
                    output.write(json.dumps(row,ensure_ascii=False)+'\n'); output.flush()
                    print(json.dumps({'id':case['id'],'error':row.get('error'),'elapsed_ms':row['elapsed_ms'],
                        'http':len(row['network']),'route':(row.get('response') or {}).get('route_plan'),
                        'final':(row.get('response') or {}).get('answer')},ensure_ascii=False),flush=True)
            finally:
                dump('results.json',{'rows':rows,'network':guard.network,'actual_requests':len(guard.network),
                    'stop_reason':STATE['stop'],'source_unchanged':frozen['source_sha256']==guard.fingerprints(),
                    'runner_unchanged':frozen['runner_sha256']==sha(Path(__file__)),
                    'frozen_unchanged':sha(OUT/'frozen.json')==(OUT/'frozen.sha256').read_text().strip()})
                print(json.dumps({'completed_cases':len(rows),'actual_requests':len(guard.network),'stop':STATE['stop']}),flush=True)

if __name__=='__main__':
    os.environ.update(CANCEL_INTENT_EXECUTE='0',CANCEL_INTENT_OBSERVE='0')
    if '--prepare' in sys.argv:prepare()
    elif '--run' in sys.argv:run()
    else:raise SystemExit('use --prepare or --run')
