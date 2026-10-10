"""两条真实聊天请求：Agent 模型标记 + 商城浏览器链路。不执行购买或售后。"""
import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parent
question = '请帮我查一下 SO20260602103000009-a1000009 的物流'
response = httpx.post('http://127.0.0.1:8000/chat', json={
    'session_id': 'deepseek-check-20261007', 'runtime_user_id': 'U1001',
    'user_message': question, 'reasoning_view': 'summary',
}, timeout=90)
response.raise_for_status()
direct = response.json()
(out/'real-model-direct.json').write_text(json.dumps(direct, ensure_ascii=False, indent=2))
models = direct['session_state']['model']
assert models['route_planner']['used_model'], models
assert models['final_answer']['used_model'], models
assert models['final_answer']['model_name'] == 'deepseek-flash', models
assert '运输中' in direct['answer'], direct['answer']
print('Agent：真实 DeepSeek 路由和最终回答均通过。', flush=True)

chat = []
errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    page.on('pageerror', lambda e: errors.append(str(e)))
    def capture(response):
        if response.url.endswith('/api/customer-service/chat'):
            chat.append({'status': response.status, 'data': response.json()['data']})
    page.on('response', capture)
    page.goto('http://127.0.0.1:5174/login')
    page.get_by_label('账号', exact=True).fill('zhangsan')
    page.get_by_label('密码', exact=True).fill('123456')
    page.get_by_role('button', name='登录商城', exact=True).click()
    page.get_by_role('button', name='咨询客服', exact=True).click()
    page.get_by_placeholder('请输入您的问题', exact=True).fill(question)
    page.get_by_role('button', name='发送', exact=True).click()
    page.locator('.message').filter(has_text='运输中').wait_for(timeout=60000)
    assert len(chat) == 1 and chat[0]['status'] == 200 and chat[0]['data']['fallback'] is False, chat
    assert not errors, errors
    page.screenshot(path=str(out/'商城DeepSeek回复.png'))
    browser.close()
(out/'real-model-result.json').write_text(json.dumps({
    'model': 'deepseek-flash', 'direct_model_flags': models,
    'mall_chat': chat, 'page_errors': errors,
    'scope': 'two real chat requests; live Java/MySQL facts; local policy embeddings',
}, ensure_ascii=False, indent=2))
print('商城：实际登录、Java 网关和模型已启用的 Agent 返回运输中，无网关回退。')
