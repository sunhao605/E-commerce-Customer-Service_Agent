from pathlib import Path
import json,time,urllib.request,urllib.parse
from playwright.sync_api import sync_playwright
OUT=Path('/private/tmp/rag-recheck-20261013');results=[]
cases=[('cold-tent-care','帐篷怎么保养？'),('warm-tent-care','帐篷怎么保养？'),('unknown-campaign','双十一活动也能满300减40吗？'),('coupon-expiry','山友会员券过期了，系统能自动延期吗？')]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=browser.new_page(viewport={'width':1440,'height':1050})
 page.goto('http://127.0.0.1:5174/',wait_until='networkidle')
 page.get_by_label('账号',exact=True).fill('zhangsan');page.get_by_label('密码',exact=True).fill('123456')
 page.get_by_role('button',name='登录商城',exact=True).click()
 page.get_by_role('button',name='客服',exact=True).wait_for(timeout=15000)
 page.get_by_role('button',name='客服',exact=True).click()
 drawer=page.locator('.service-drawer');drawer.wait_for()
 page.screenshot(path=str(OUT/'browser-new-session.png'))
 for number,(cid,query) in enumerate(cases,1):
  started=time.monotonic()
  drawer.get_by_placeholder('请输入您的问题').fill(query)
  with page.expect_response(lambda r:'/api/customer-service/chat' in r.url and r.request.method=='POST',timeout=90000) as expected:
   drawer.get_by_role('button',name='发送',exact=True).click()
   page.screenshot(path=str(OUT/f'{number:02d}-{cid}-sent.png'))
  response=expected.value;body=response.json();data=body.get('data',body)
  page.wait_for_function("document.querySelector('.service-drawer .drawer-input input')?.disabled===false",timeout=15000)
  text=drawer.locator('.message.agent').last.inner_text()
  page.screenshot(path=str(OUT/f'{number:02d}-{cid}-answer.png'))
  session=data.get('sessionId')
  events=[]
  if session:
   with urllib.request.urlopen('http://127.0.0.1:8000/sessions/'+urllib.parse.quote(session,safe='')+'/trace',timeout=5) as r:events=json.load(r)
  (OUT/f'{number:02d}-{cid}-trace.json').write_text(json.dumps(events,ensure_ascii=False,indent=2)+'\n')
  record={'id':cid,'query':query,'response':data,'http_status':response.status,'visible_answer':text,'seconds':round(time.monotonic()-started,2),'no_submission_card':not data.get('confirmRequired',False),'trace_file':f'{number:02d}-{cid}-trace.json'}
  results.append(record);(OUT/'browser-smoke.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
  print(json.dumps({'id':cid,'http_status':response.status,'no_submission_card':record['no_submission_card'],'seconds':record['seconds'],'answer':text},ensure_ascii=False),flush=True)
  if response.status!=200 or data.get('confirmRequired',False):break
 wb=browser.new_page(viewport={'width':1440,'height':1000})
 wb.goto('http://127.0.0.1:5173/',wait_until='networkidle')
 wb.get_by_role('button',name='运行评测',exact=True).wait_for(timeout=15000)
 wb.screenshot(path=str(OUT/'workbench-ready.png'))
 print('Workbench evaluation control ready; full evaluation not triggered.',flush=True)
 browser.close()
assert len(results)==4 and all(x['http_status']==200 and x['no_submission_card'] for x in results)
