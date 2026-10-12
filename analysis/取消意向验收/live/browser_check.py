"""日常MySQL商城浏览器取消验收；复用已支付样本，只取消，不点击确认提交。"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

out = Path(__file__).resolve().parent
order = 'SO20261010124526218-b60d3f08'
turns, checks = [], []

def check(ok, label):
    checks.append({'check': label, 'passed': ok})
    assert ok, label

def login(browser):
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    page.goto('http://127.0.0.1:5174', wait_until='networkidle')
    with page.expect_response(lambda response: '/api/auth/login' in response.url) as response:
        page.get_by_role('button', name='登录商城', exact=True).click()
    assert response.value.status == 200
    page.get_by_role('heading', name='账号登录').wait_for(state='hidden')
    page.get_by_role('button', name='客服', exact=True).click()
    expect(page.get_by_placeholder('请输入您的问题')).to_be_visible()
    return context, page

def send(page, message):
    page.get_by_placeholder('请输入您的问题').fill(message)
    with page.expect_response(lambda response: '/api/customer-service/chat' in response.url, timeout=90000) as pending:
        page.get_by_role('button', name='发送', exact=True).click()
    response = pending.value
    assert response.status == 200
    result = response.json()['data']
    expect(page.get_by_role('button', name='发送', exact=True)).to_be_enabled(timeout=90000)
    turns.append({'input': message, 'request': response.request.post_data_json, 'response': result})
    (out / 'browser-dialogue.json').write_text(json.dumps(turns, ensure_ascii=False, indent=2) + '\n')
    return result

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    try:
        context, page = login(browser)
        card = send(page, order + ' 我要退款，原因：买错了。')
        check(card['confirmRequired'] and card['relatedOrderNo'] == order, '本人199元订单生成未提交确认卡')
        expect(page.locator('.confirm-card')).to_have_count(1)
        page.screenshot(path=str(out / 'before-cancel.png'), full_page=True)
        cancelled = send(page, '算了，不退了。')
        check(cancelled['pendingAction'] == 'CANCELLED' and not cancelled['confirmRequired'], '自然表达取消成功')
        expect(page.get_by_role('button', name='确认提交申请', exact=True)).to_be_disabled()
        expect(page.get_by_role('button', name='取消本次申请', exact=True)).to_be_disabled()
        expect(page.get_by_text('此确认卡已失效，请以最新回复为准。', exact=True)).to_be_visible()
        check(True, '旧卡显示失效且提交/取消按钮均禁用')
        page.screenshot(path=str(out / 'after-cancel.png'), full_page=True)
        logistics = send(page, '查 ' + order + ' 的物流。')
        check(not logistics['confirmRequired'] and order in logistics['answer'], '取消后查物流正常且不恢复确认卡')
        ordinal = send(page, '第一笔。')
        check(not ordinal['confirmRequired'] and ordinal.get('pendingAction') not in ('REFUND_REASON_REQUIRED', 'REFUND_ORDER_REQUIRED'),
              '取消后序号不会续办旧申请')
        new_card = send(page, order + ' 我要退款，原因：买错了。')
        check(new_card['confirmRequired'] and new_card['resumeToken'] != card['resumeToken'], '重新申请生成新卡和新凭证')
        buttons = page.get_by_role('button', name='取消本次申请', exact=True)
        expect(buttons.last).to_be_enabled()
        with page.expect_response(lambda response: '/api/customer-service/resume' in response.url) as pending:
            buttons.last.click()
        response = pending.value
        result = response.json()['data']
        turns.append({'input': '点击取消本次申请', 'request': response.request.post_data_json, 'response': result})
        check(response.status == 200 and result['pendingAction'] == 'CANCELLED', '按钮取消新草稿成功')
        expect(page.get_by_role('button', name='确认提交申请', exact=True).last).to_be_disabled()
        page.screenshot(path=str(out / 'new-card-cancelled.png'), full_page=True)
        (out / 'card-dom.txt').write_text(page.locator('.service-drawer').inner_text())
        context.close()

        context, page = login(browser)
        shown = send(page, '帮我退款。')
        check(not shown['confirmRequired'], '新会话先展示候选订单')
        stopped = send(page, '取消本次申请。')
        check(stopped['pendingAction'] == 'CANCELLED', '等待选单时可取消')
        selected = send(page, '第一笔。')
        check(not selected['confirmRequired'] and selected.get('pendingAction') not in ('REFUND_REASON_REQUIRED', 'REFUND_ORDER_REQUIRED'),
              '旧候选不会复活退款待办')
        page.screenshot(path=str(out / 'candidates-cancelled.png'), full_page=True)
        (out / 'candidate-dom.txt').write_text(page.locator('.service-drawer').inner_text())
        for session in sorted({turn['response']['sessionId'] for turn in turns}):
            trace = context.request.get('http://127.0.0.1:8000/sessions/' + session + '/trace').json()
            (out / (session + '-trace.json')).write_text(json.dumps(trace, ensure_ascii=False, indent=2) + '\n')
        context.close()
        result = {'passed': True, 'groups': 2, 'turns': len(turns), 'checks': checks, 'dialogue': turns,
                  'confirmation_submitted': False, 'new_orders_or_payments': False}
        (out / 'browser-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'passed': True, 'checks': len(checks), 'turns': len(turns)}, ensure_ascii=False))
    finally:
        browser.close()
