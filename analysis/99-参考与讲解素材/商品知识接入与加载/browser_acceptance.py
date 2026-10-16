from pathlib import Path
import json,time
from playwright.sync_api import sync_playwright
BASE=Path('/private/tmp/product-rag-load-20261012');results=[]
cases=[('tent-care','帐篷怎么保养？',('晾干','收纳')),('sleeping-bag','睡袋温标怎么看？',('温标','舒适')),('tool-and-knowledge','山野四人帐篷现在多少钱，怎么收纳？',('山野四人帐篷','收纳'))]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=browser.new_page(viewport={'width':1440,'height':1050})
 page.goto('http://127.0.0.1:5174/',wait_until='networkidle')
 page.get_by_label('账号',exact=True).fill('zhangsan');page.get_by_label('密码',exact=True).fill('123456')
 page.get_by_role('button',name='登录商城',exact=True).click()
 page.get_by_role('button',name='客服',exact=True).wait_for(timeout=15000)
 page.screenshot(path=str(BASE/'01-login.png'))
 page.get_by_role('button',name='客服',exact=True).click()
 drawer=page.locator('.service-drawer');drawer.wait_for();page.screenshot(path=str(BASE/'02-chat-open.png'))
 # Inspect the rendered control before sending.
 assert drawer.get_by_placeholder('请输入您的问题').count()==1
 assert drawer.get_by_role('button',name='发送',exact=True).count()==1
 for number,(case_id,query,terms) in enumerate(cases,1):
  started=time.monotonic()
  drawer.get_by_placeholder('请输入您的问题').fill(query)
  with page.expect_response(lambda r:'/api/customer-service/chat' in r.url and r.request.method=='POST',timeout=90000) as expected:
   drawer.get_by_role('button',name='发送',exact=True).click()
   page.screenshot(path=str(BASE/f'{number:02d}-{case_id}-sent.png'))
  response=expected.value;body=response.json();data=body.get('data',body)
  drawer.get_by_role('button',name='发送',exact=True).wait_for(timeout=15000)
  page.wait_for_function("document.querySelector('.service-drawer .drawer-input input')?.disabled===false",timeout=15000)
  text=drawer.locator('.message.agent').last.inner_text()
  page.screenshot(path=str(BASE/f'{number:02d}-{case_id}-answer.png'))
  record={'case':case_id,'query':query,'http_status':response.status,'elapsed_seconds':round(time.monotonic()-started,2),'response':data,'visible_answer':text,'expected_terms':list(terms),'passed':response.status==200 and all(term in text for term in terms) and not data.get('confirmRequired',False) and '###' not in text and '](http' not in text and len(text)<900}
  results.append(record);(BASE/'browser-acceptance.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
  print(json.dumps({'case':case_id,'passed':record['passed'],'response_keys':list(data),'pending_action':data.get('pendingAction'),'elapsed':record['elapsed_seconds'],'answer_preview':text[:220]},ensure_ascii=False),flush=True)
  if not record['passed']:break
 browser.close()
assert len(results)==3 and all(r['passed'] for r in results),'Read saved receipts and screenshots; no business action was submitted'
