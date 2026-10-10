"""真实浏览器 → 隔离 Java/H2；仅替换 Agent 模型入口，/api 拦截仅转发到隔离端口。"""
import json
import time
import httpx
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parent
backend = 'http://127.0.0.1:18191'
for attempt in range(60):
    try:
        if httpx.get(backend + '/actuator/health', timeout=2).json().get('status') == 'UP':
            break
    except (httpx.HTTPError, ValueError):
        pass
    time.sleep(0.5)
else:
    raise RuntimeError('隔离后端未就绪')
requests, chat_responses, errors, checks = [], [], [], []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel='chrome')
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()

    def forward(route):
        url = urlparse(route.request.url)
        target = backend + url.path + ('?' + url.query if url.query else '')
        response = route.fetch(url=target)
        if url.path.endswith('/chat'):
            chat_responses.append(response.json()['data'])
        if url.path.endswith('/resume'):
            requests.append(route.request.post_data_json)
        route.fulfill(response=response)

    page.route('**/api/**', forward)
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto('http://127.0.0.1:6184/')
        page.wait_for_load_state('networkidle')
        assert any('密码' in label for label in page.locator('label').all_inner_texts())
        page.get_by_label('账号', exact=True).fill('zhangsan')
        page.get_by_label('密码', exact=True).fill('123456')
        page.get_by_role('button', name='登录商城', exact=True).click()
        page.get_by_role('heading', name='把周末，交给旷野。', exact=True).wait_for()
        page.wait_for_load_state('networkidle')
        balance_before = context.request.get(backend + '/api/shop/balance').json()['data']['availableBalance']
        page.get_by_role('button', name='咨询客服', exact=True).click()
        drawer = page.locator('.service-drawer')

        def send(message):
            count = len(chat_responses)
            page.get_by_placeholder('请输入您的问题', exact=True).fill(message)
            page.get_by_role('button', name='发送', exact=True).click()
            page.get_by_placeholder('请输入您的问题', exact=True).wait_for()
            assert len(chat_responses) == count + 1
            return chat_responses[-1]

        missing = send('ORD9200 我要退款')
        assert not missing['confirmRequired'] and '原因' in missing['answer']
        assert drawer.locator('.confirm-card').count() == 0
        amount = send('能退多少钱？')
        assert '200.00' in amount['answer'] and not amount['confirmRequired']
        card = send('原因：买错了')
        cards = drawer.locator('.confirm-card')
        assert all(term in cards.last.inner_text() for term in ['ORD9200', '整单退款', '200.00', '买错了', '尚未提交'])
        assert cards.last.get_by_role('button', name='确认提交申请', exact=True).is_enabled()
        checks.append('缺少原因只追问；补齐后显示订单、范围、实付金额和未提交状态')
        same = send('审核是什么流程？')
        assert same['resumeToken'] == card['resumeToken']
        assert cards.first.get_by_role('button', name='确认提交申请', exact=True).is_disabled()
        assert cards.last.get_by_role('button', name='确认提交申请', exact=True).is_enabled()
        checks.append('继续提问保留待确认；仅最新卡可操作')
        changed = send('原因：行程取消')
        assert changed['resumeToken'] != card['resumeToken']
        assert '行程取消' in cards.last.inner_text()
        cards.last.scroll_into_view_if_needed()
        page.screenshot(path=str(out / '确认卡.png'))
        checks.append('修改原因后旧卡失效，新卡内容正确')
        cards.last.get_by_role('button', name='确认提交申请', exact=True).click()
        drawer.get_by_text('退款申请已登记', exact=False).last.wait_for()
        assert all(button.is_disabled() for button in cards.get_by_role('button', name='确认提交申请', exact=True).all())
        records = context.request.get(backend + '/api/shop/after-sales').json()['data']
        own = [item for item in records if item['orderNo'] == 'ORD9200']
        assert len(own) == 1 and own[0]['status'] == 'PENDING_REVIEW' and own[0]['reason'] == '行程取消'
        assert requests[-1]['decision'] == 'CONFIRM_SUBMIT'
        assert '已提交，待审核' in cards.last.inner_text() and own[0]['requestNo'] in cards.last.inner_text()
        cards.last.scroll_into_view_if_needed()
        page.screenshot(path=str(out / '提交待审核.png'))
        checks.append('页面提交结果与实际待审核记录一致，按钮失效')
        cancelled = send('ORD9201 我要退款，原因：买错了')
        assert cancelled['confirmRequired']
        cards.last.get_by_role('button', name='取消本次申请', exact=True).click()
        drawer.get_by_text('本次退款申请已取消，未提交。', exact=True).last.wait_for()
        assert requests[-1]['decision'] == 'CANCEL_SUBMIT'
        assert cards.last.get_by_role('button', name='确认提交申请', exact=True).is_disabled()
        retry = context.request.post(backend + '/api/customer-service/resume', data={
            'sessionId': cancelled['sessionId'], 'resumeToken': cancelled['resumeToken'],
            'relatedOrderNo': cancelled['relatedOrderNo'], 'decision': 'CONFIRM_SUBMIT'})
        assert retry.status == 409
        records = context.request.get(backend + '/api/shop/after-sales').json()['data']
        assert not any(item['orderNo'] == 'ORD9201' for item in records)
        assert context.request.get(backend + '/api/shop/balance').json()['data']['availableBalance'] == balance_before
        checks.append('取消实际写回后端、无新申请，旧卡重放被拒绝；余额不变')
        page.set_viewport_size({'width': 390, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        assert not errors, errors
        checks.append('桌面和手机宽度无横向溢出、无页面异常')
    finally:
        browser.close()
result = {'mode': 'real browser and Java HTTP/auth + isolated H2; API routing proxy; synthetic Agent',
          'checks': checks, 'decision_requests': requests, 'page_errors': errors, 'model_calls': 0}
(out / 'ui-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
