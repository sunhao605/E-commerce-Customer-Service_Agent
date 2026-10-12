"""两条商品页面链路及一个页面编号接口检查；停在售后提交前。"""
import json
from pathlib import Path
import time

import httpx
from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parent
SITE = 'http://127.0.0.1:18103'
PYTHON = 'http://127.0.0.1:18101'
CHECKS, TURNS, API_TURNS, TRACES = [], [], [], {}


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def check(ok, label):
    CHECKS.append({'check': label, 'passed': bool(ok)})
    save('checks.json', CHECKS)
    print(('PASS ' if ok else 'FAIL ') + label, flush=True)
    assert ok, label


def state(sid=''):
    response = httpx.get(SITE + '/api/check/state', params={'session': sid})
    response.raise_for_status()
    return response.json()


def selected(sid):
    trace = httpx.get(PYTHON + '/sessions/' + sid + '/trace').json()
    TRACES[sid] = trace
    save('traces.json', TRACES)
    return next(event['payload'] for event in reversed(trace) if event['event_type'] == 'product_target_resolved')


def turn(page, text):
    with page.expect_response(lambda r: '/api/customer-service/chat' in r.url) as response:
        page.get_by_placeholder('请输入您的问题').fill(text)
        page.get_by_role('button', name='发送', exact=True).click()
    assert response.value.status == 200
    data = response.value.json()['data']
    expect(page.get_by_role('button', name='发送', exact=True)).to_be_enabled(timeout=15000)
    shot = OUT / '对话截图' / f'{len(TURNS)+1:02d}.png'
    shot.parent.mkdir(exist_ok=True)
    page.locator('.service-drawer').screenshot(path=str(shot), style='''
        .service-drawer { position: absolute !important; height: auto !important; max-height: none !important;
            overflow: visible !important; grid-template-rows: auto auto auto !important; }
        .messages { overflow: visible !important; height: auto !important; max-height: none !important; }
    ''')
    TURNS.append({'input': text, 'response': data, 'screenshot': str(shot.relative_to(OUT))})
    save('dialogue.json', TURNS)
    return data


def login_page(browser):
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    page.goto(SITE + '/login', wait_until='networkidle')
    page.get_by_role('button', name='登录商城', exact=True).wait_for(state='visible')
    page.get_by_role('button', name='登录商城', exact=True).click()
    page.get_by_role('heading', name='账号登录').wait_for(state='hidden')
    page.get_by_role('button', name='客服', exact=True).click()
    return context, page


deadline = time.monotonic() + 30
while 'CANCEL_CHECK_READY' not in (OUT / '.runtime/java.log').read_text():
    assert time.monotonic() < deadline, '隔离 fixture 未就绪'
    time.sleep(0.2)
products = httpx.get(SITE + '/api/products', params={'keyword': '折叠椅'}).json()['data']
chair_a = next(item for item in products if item['name'] == '露营折叠椅')
chair_b = next(item for item in products if item['name'] == '轻量露营折叠椅')
save('fixture-products.json', products)
before = state()['tables']

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel='chrome')
    try:
        context, page = login_page(browser)
        try:
            candidates = turn(page, '折叠椅多少钱，还有库存吗？')
            sid = candidates['sessionId']
            check(candidates['pendingAction'] == 'PRODUCT_QUERY' and not candidates['confirmRequired'], 'P03 商品候选属于只读查询')
            check('1. 露营折叠椅' in candidates['answer'] and '2. 轻量露营折叠椅' in candidates['answer'] and '标价' not in candidates['answer'], 'P03 页面多款澄清，不默选第一款')
            result = turn(page, '第二款')
            check(selected(sid)['product_id'] == str(chair_b['id']), 'P03 中文序号绑定已展示的第二款编号')
            check(chair_b['name'] in result['answer'] and str(chair_b['stock']) in result['answer'] and str(int(chair_b['price'])) in result['answer'], 'P03 第二款价格和库存来自同一商品')
            check(state(sid)['session'].get('pending') is None, 'P03 商品查询没有创建退款草稿')
        finally:
            context.close()

        context, page = login_page(browser)
        try:
            card = turn(page, 'ORD9801 我要申请退款，原因：买错了')
            sid = card['sessionId']
            check(card['confirmRequired'] and card['relatedOrderNo'] == 'ORD9801', 'P08 前置退款确认卡存在，停在提交前')
            draft = state(sid)['session']
            candidates = turn(page, '折叠椅多少钱？')
            check(not candidates['confirmRequired'] and not candidates.get('resumeToken') and not candidates['actions'], 'P08 商品澄清没有重发退款卡')
            check(state(sid)['session'] == draft, 'P08 商品澄清不改变原草稿、版本和凭证')
            expect(page.get_by_role('button', name='确认提交申请', exact=True)).to_be_enabled()
            result = turn(page, '第2款')
            check(selected(sid)['product_id'] == str(chair_b['id']) and chair_b['name'] in result['answer'], 'P08 数字序号仍选择第二款，不被旧退款任务吞掉')
            check(state(sid)['session'] == draft and not result['confirmRequired'], 'P08 选择后原退款草稿仍不变')
            expect(page.get_by_role('button', name='确认提交申请', exact=True)).to_be_enabled()
            check(True, 'P08 历史退款确认卡仍可点击')
            returned = turn(page, '继续办理退款')
            check(returned['confirmRequired'] and returned['relatedOrderNo'] == 'ORD9801', 'P08 明确回到退款后续接原流程')
        finally:
            context.close()
    finally:
        browser.close()

with httpx.Client(timeout=30) as client:
    response = client.post(SITE + '/api/auth/login', json={'username': 'zhangsan', 'password': '123456'})
    response.raise_for_status()
    body = {'sessionId': 'page-bound-api', 'message': '这件多少钱，还有货吗？', 'pageContext': {'type': 'PRODUCT_DETAIL', 'productId': chair_b['id']}}
    response = client.post(SITE + '/api/customer-service/chat', json=body)
    response.raise_for_status()
    result = response.json()['data']
    API_TURNS.append({'input': body, 'response': result})
    save('api-dialogue.json', API_TURNS)
    target = selected('page-bound-api')
    check(target['source'] == 'page' and target['product_id'] == str(chair_b['id']), 'P01 页面编号精准读取该商品')
    check(chair_b['name'] in result['answer'] and not result['confirmRequired'], 'P01 页面咨询点名该商品，不产生办理卡')

after = state()['tables']
save('business.json', {'before': before, 'after': after})
check(before == after, '商品查询及申请准备阶段8张H2业务表未变')
save('result.json', {'passed': True, 'checks': len(CHECKS), 'screenshots': len(TURNS), 'online_models': False, 'submitted': False, 'database': 'isolated H2', 'failed': []})
print(f'完成 {len(CHECKS)} 项检查、{len(TURNS)} 轮截图；未提交、未调用外部模型。', flush=True)
