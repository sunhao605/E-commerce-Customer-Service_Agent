"""实际 Java/MySQL 商城联调；不拦截 API，不执行购买或售后提交。"""
import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

out=Path(__file__).resolve().parent
with httpx.Client(base_url='http://127.0.0.1:8081',timeout=30) as client:
    health=client.get('/actuator/health').json()
    assert health['status']=='UP',health
    login=client.post('/api/auth/login',json={'username':'zhangsan','password':'123456'})
    assert login.status_code==200 and login.json()['success']
    data=client.get('/api/shop/products').json()['data']
    assert len(data)==17,len(data)
    chair=next(x for x in data if x['name']=='露营折叠椅')
    assert chair['price']==599.0 and chair['stockQuantity']==520,chair
    admin_client=httpx.Client(base_url='http://127.0.0.1:8081',timeout=30)
    assert admin_client.post('/api/auth/login',json={'username':'admin','password':'admin123456'}).json()['success']
    admin_products=admin_client.get('/api/admin/products').json()['data']
    assert len(admin_products)==18,len(admin_products)
    admin_client.close()

errors=[];chat=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
    page.on('pageerror',lambda e:errors.append(str(e)))
    def capture(response):
        if response.url.endswith('/api/customer-service/chat'):
            chat.append({'status':response.status,'data':response.json()['data']})
    page.on('response',capture)
    page.goto('http://127.0.0.1:5174/login')
    page.get_by_label('账号',exact=True).fill('zhangsan')
    page.get_by_label('密码',exact=True).fill('123456')
    page.get_by_role('button',name='登录商城',exact=True).click()
    page.get_by_role('heading',name='把周末，交给旷野。',exact=True).wait_for()
    page.get_by_text('露营折叠椅',exact=True).first.wait_for()
    page.wait_for_load_state('networkidle')
    assert page.locator('img').evaluate_all('(imgs)=>imgs.every(i=>i.complete && i.naturalWidth>0)')
    page.screenshot(path=str(out/'商城实际数据.png'))
    page.get_by_role('button',name='咨询客服',exact=True).click()
    page.get_by_placeholder('请输入您的问题',exact=True).fill('请帮我查一下 SO20260602103000009-a1000009 的物流')
    page.get_by_role('button',name='发送',exact=True).click()
    page.locator('.message').filter(has_text='运输中').wait_for(timeout=45000)
    assert len(chat)==1 and chat[0]['status']==200 and chat[0]['data']['fallback'] is False,chat
    page.screenshot(path=str(out/'商城客服实际链路.png'))
    browser.close()
assert not errors,errors
trace=httpx.get(f"http://127.0.0.1:8000/sessions/{chat[0]['data']['sessionId']}/trace").json()
assert {'get_order_detail','get_order_logistics'} <= {e.get('target',{}).get('name') for e in trace if e['event_type']=='tool_finished'}
direct=httpx.post('http://127.0.0.1:8000/chat',json={'session_id':'java-source-check-20261007','runtime_user_id':'U1001','user_message':'请帮我查一下 SO20260420103000001-a1000001 的物流','reasoning_view':'summary'},timeout=30).json()
assert any(x['tool_name']=='get_order_detail' and x['arguments'].get('fact_source')=='business_api' for x in direct['tool_calls']),direct
result={'date':'2026-10-07','mode':'actual Java/MySQL and browser; no API interception; model disabled','health':health,'shop_products':len(data),'admin_products':len(admin_products),'chair':chair,'chat':chat,'page_errors':errors,'trace':trace,'direct_business_api_check':direct}
(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print('真实商城联调通过：Java 健康、用户/管理员登录、17/18 件商品、图片、商城客服查询；直接工具调用确认来源 business_api（订单镜像回退已关闭）。')
