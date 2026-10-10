"""页面核验：Java API 使用合成 fixture；工作台聊天/审批/评测调用真实离线 Python Agent。"""
from pathlib import Path
import json,re,sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'backend'))
from agent.ecommerce_client import COURSE_SEED_ORDER_MIRRORS
catalog=json.loads((root/'analysis/改造验证/product-mapping.json').read_text())
java=(root/'ecommerce-backend/src/main/java/com/teachdemo/ecommerce/config/DataInitializer.java').read_text()
pattern=re.compile(r'(?:new Product|saveProductWithImage)\("(SKU-[A-Z0-9-]+)".*?new BigDecimal\("([0-9.]+)"\), (\d+), "[^"]*",\s*(true|false), (true|false)',re.S)
facts={r[0]:r[1:] for r in pattern.findall(java)}
products=[]
for i,(code,d) in enumerate(catalog.items(),1):
    price,stock,active,returnable=facts[code]
    products.append(dict(productId=i,code=code,name=d['name'],category=d['category'],description=d['description'],price=float(price),stockQuantity=int(stock),imageUrl=f"/products/{d['slug']}.svg",supportsSevenDayReturn=returnable=='true',afterSaleNote=d['afterSaleLimit'],purchaseAvailable=active=='true' and int(stock)>0,promotion=dict(id=i,promotionName='露营季营地装备会场',promotionType='member_discount',discountSummary='露营季活动，满 300 减 40 可与会员券叠加；具体价格以结算页为准。',promotionPrice=529 if i==1 else float(price),requiredMemberLevel='gold' if i==1 else None,conditionSummary='山友会员专享' if i==1 else '适用条件以结算页为准',startAt='2026-06-01T00:00:00',endAt='2027-12-31T23:59:00'),highlights=d['highlights'],status='ACTIVE' if active=='true' else 'INACTIVE',scenarioTags=d['tags'],createdAt='2026-07-01T00:00:00',updatedAt='2026-07-01T00:00:00'))

def fixture(role):
    def handle(route):
        path=urlparse(route.request.url).path
        if route.request.method=='OPTIONS':
            route.fulfill(status=204,headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'*'});return
        if path.endswith('/me'):
            data=dict(accountId=1,username='admin' if role=='ADMIN' else 'camper',role=role,userId='U1001',nickname='山友',mobile=None,memberLevel='gold',redirectPath='/admin/' if role=='ADMIN' else '/')
        elif 'order-context' in path:
            data=dict(orders=list(COURSE_SEED_ORDER_MIRRORS.values()),truncated=False)
        elif path.endswith('/products'):
            data=products if role=='ADMIN' else [p for p in products if p['status']=='ACTIVE']
        elif re.search(r'/products/\d+$',path):data=products[int(path.split('/')[-1])-1]
        elif path.endswith('/cart'):data=dict(items=[],selectedTotalAmount=0,selectedItemCount=0)
        else:data=[]
        route.fulfill(json=dict(success=True,code='OK',message='Java API fixture',data=data),headers={'Access-Control-Allow-Origin':'*'})
    return handle

out=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else root/'analysis/改造验证'
out.mkdir(parents=True,exist_ok=True)
errors=[];checks=[]
viewports=[(1440,1000),(1024,768),(768,1024),(375,812),(812,375)]

def check_layout(page,label):
    for width,height in viewports:
        page.set_viewport_size({'width':width,'height':height})
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'{label} overflow {width}x{height}'
    page.set_viewport_size({'width':1440,'height':1000})
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    page.evaluate('window.scrollTo(0,0)')
    page.keyboard.press('Tab')
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle !== 'none'"), label+' missing keyboard focus'
    checks.append(label+'：五种尺寸无横向溢出，可见键盘焦点')
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    work=browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='reduce')
    work.route('**/api/**',fixture('USER'))
    page=work.new_page();page.on('pageerror',lambda e:errors.append('workbench: '+str(e)))
    page.goto('http://127.0.0.1:15173');page.wait_for_load_state('networkidle')
    assert page.get_by_role('heading',name='荒野户外 · 评测工作台').is_visible()
    check_layout(page,'工作台')
    assert page.locator('.camp-illustration').evaluate('(i)=>i.complete && i.naturalWidth>0')
    page.locator('#question').fill('请帮我查一下 SO20260602103000009-a1000009 的物流')
    page.get_by_role('button',name='发送给 Agent',exact=True).click()
    page.locator('.message-card.assistant').filter(has_text='运输中').wait_for()
    checks.append('工作台实际离线物流响应与执行记录')
    page.locator('#question').fill('SO20260601090000008-a1000008 还没发货，我想申请退款')
    page.get_by_role('button',name='发送给 Agent',exact=True).click()
    page.get_by_text('待审核 · 模拟售后申请',exact=True).wait_for()
    page.screenshot(path=str(out/'workbench-preview.png'))
    page.screenshot(path=str(out/'workbench-desktop.png'),full_page=True)
    checks.append('模拟审批停点及中文状态')
    page.get_by_role('button',name='批准',exact=True).click()
    page.locator('.message-card.assistant').filter(has_text='不执行真实资金退款').wait_for()
    checks.append('工作台实际离线审批恢复')
    page.locator('.evaluation-drawer summary').click()
    page.get_by_role('button',name='运行评测',exact=True).click()
    page.get_by_text('规则化离线评测用于本演示项目回归',exact=False).wait_for()
    checks.append('工作台实际离线评测响应')
    page.set_viewport_size({'width':375,'height':812});page.wait_for_load_state('networkidle')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'workbench overflow'
    page.screenshot(path=str(out/'workbench-mobile.png'),full_page=True)
    work.close()
    for role,url,label in [('USER','http://127.0.0.1:15174/','shop'),('ADMIN','http://127.0.0.1:15174/admin/','admin')]:
        context=browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='reduce')
        context.route('**/api/**',fixture(role))
        page=context.new_page();page.on('pageerror',lambda e:errors.append(label+': '+str(e)))
        page.goto(url);page.wait_for_load_state('networkidle')
        assert '荒野户外' in page.locator('body').inner_text()
        assert '荒野户外' in page.locator('body').inner_text()
        page.get_by_text('露营折叠椅',exact=True).first.wait_for()
        check_layout(page,label)
        assert page.locator('img').evaluate_all('(imgs)=>imgs.every(i=>i.complete && i.naturalWidth>0)'), 'broken product image'
        page.screenshot(path=str(out/f'{label}-preview.png'))
        page.screenshot(path=str(out/f'{label}-desktop.png'),full_page=True)
        if role=='USER':
            page.get_by_role('button',name='查看详情',exact=True).first.click();page.wait_for_load_state('networkidle')
            page.get_by_role('heading',name='露营折叠椅',exact=True).wait_for()
            checks.append('商城 fixture：商品列表、图片及详情导航')
        else: checks.append('管理后台 fixture：商品列表及户外图片')
        page.set_viewport_size({'width':375,'height':812});page.wait_for_load_state('networkidle')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), label+' overflow'
        page.screenshot(path=str(out/f'{label}-mobile.png'),full_page=True)
        context.close()
    browser.close()
assert not errors,errors
(out/'ui-result.json').write_text(json.dumps(dict(mode='Java API fixture / real offline Python Agent',viewports=[f'{w}x{h}' for w,h in viewports],checks=checks,page_errors=errors),ensure_ascii=False,indent=2))
print(json.dumps({'checks':checks,'page_errors':errors},ensure_ascii=False))
