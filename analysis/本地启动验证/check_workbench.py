"""实际工作台冒烟检查：模型关闭，Java/MySQL 提供业务事实，不拦截 API。"""
import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parent
health = httpx.get('http://127.0.0.1:8000/health').json()
assert health['status'] == 'ok'
responses = []
errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    page = browser.new_page(viewport={'width':1440,'height':1000}, reduced_motion='reduce')
    page.on('pageerror', lambda e: errors.append(str(e)))
    def capture(response):
        if response.url in ('http://127.0.0.1:8000/chat', 'http://127.0.0.1:8000/chat/resume'):
            responses.append({'url':response.url, 'status':response.status, 'data':response.json()})
    page.on('response', capture)
    page.goto('http://127.0.0.1:5173')
    page.get_by_role('heading', name='荒野户外 · 评测工作台').wait_for()
    page.get_by_text('64/64 项能力开放', exact=True).wait_for()
    for text,expected in [
        ('请帮我查一下 SO20260602103000009-a1000009 的物流', '运输中'),
        ('SO20260601090000008-a1000008 还没发货，我想申请退款', '未发货退款申请判断'),
    ]:
        page.locator('#question').fill(text)
        page.get_by_role('button', name='发送给 Agent', exact=True).click()
        page.locator('.message-card.assistant').filter(has_text=expected).wait_for()
    page.get_by_text('待审核 · 模拟售后申请', exact=True).wait_for()
    page.screenshot(path=str(out/'待审核预览.png'))
    page.get_by_role('button', name='批准', exact=True).click()
    page.locator('.message-card.assistant').filter(has_text='不执行真实资金退款').wait_for()
    assert len(responses) == 3 and all(r['status']==200 for r in responses), responses
    assert responses[-1]['data']['status']=='completed'
    assert not errors, errors
    result = {'date':'2026-10-07','mode':'real HTTP and browser; models disabled; Java/MySQL business facts; no API interception','health':health,'steps':['物流运输中','未发货退款暂停待审核','批准后模拟完成，明确无真实资金退款'],'page_errors':errors,'responses':responses}
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    browser.close()
print('实际浏览器与 HTTP 三步通过：物流查询 → 退款待审核 → 模拟批准；未替换响应。')
