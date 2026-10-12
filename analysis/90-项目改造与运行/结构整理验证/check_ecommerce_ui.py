"""商城/后台目录迁移检查：只用浏览器拦截的本地 fixture，不调用真实业务服务。"""
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parent
results = []
product = dict(productId=1, code='SKU-AUD-101', name='露营折叠椅', category='营地家具',
               description='本地页面检查示例', price=599, stockQuantity=520,
               imageUrl='/products/camp-chair.svg', supportsSevenDayReturn=True,
               afterSaleNote='需审核', purchaseAvailable=True, promotion=None, highlights='折叠便携',
               scenarioTags='露营', status='ON_SALE', createdAt='2026-07-15T00:00:00', updatedAt='2026-07-15T00:00:00')

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        for role, entry in [('USER', '/'), ('ADMIN', '/admin/')]:
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            state = {'logged_in': False}
            calls, errors, missing_assets = [], [], []
            account = dict(accountId=1, username='fixture-user', role=role, userId='U1001',
                           nickname='山友', mobile=None, memberLevel='gold', redirectPath=entry)

            def fixture(route):
                path = urlparse(route.request.url).path
                calls.append(route.request.method + ' ' + path)
                if path == '/api/auth/me':
                    if not state['logged_in']:
                        route.fulfill(status=401, json=dict(success=False, code='UNAUTHORIZED', message='请先登录', data=None))
                        return
                    data = account
                elif path == '/api/auth/login':
                    assert route.request.post_data_json == dict(username='fixture-user', password='fixture-pass')
                    state['logged_in'] = True
                    data = account
                elif path == '/api/auth/logout':
                    state['logged_in'] = False
                    data = None
                elif path in ('/api/shop/products', '/api/admin/products'):
                    data = [product]
                elif path == '/api/shop/products/1':
                    data = product
                elif path == '/api/shop/balance':
                    data = dict(userId='U1001', availableBalance=1000, updatedAt='2026-07-15T00:00:00')
                elif path == '/api/shop/cart':
                    data = dict(items=[], selectedTotalAmount=0, selectedItemCount=0)
                else:
                    data = []
                route.fulfill(json=dict(success=True, code='OK', message='local fixture', data=data))

            page.route('**/api/**', fixture)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('response', lambda response: missing_assets.append(response.url)
                    if response.status >= 400 and '/api/' not in response.url else None)
            page.goto('http://127.0.0.1:5184' + entry)
            page.wait_for_load_state('networkidle')
            # Discover the rendered form before operating it.
            labels = page.locator('label').all_inner_texts()
            assert any('密码' in label for label in labels), labels
            page.locator('input[autocomplete="username"]').fill('fixture-user')
            page.get_by_label('密码', exact=True).fill('fixture-pass')
            page.get_by_role('button', name='登录商城' if role == 'USER' else '登录后台', exact=True).click()
            page.get_by_text('露营折叠椅', exact=True).first.wait_for()
            page.wait_for_load_state('networkidle')
            assert page.locator('img[src="/products/camp-chair.svg"]').count() or role == 'ADMIN'
            page.screenshot(path=str(out / ('shop-layout.png' if role == 'USER' else 'admin-layout.png')))
            if role == 'ADMIN':
                page.get_by_role('button', name='订单管理', exact=True).click()
                page.wait_for_url('**/admin/orders')
                page.wait_for_load_state('networkidle')
            if role == 'USER':
                page.get_by_role('button', name='山友', exact=True).click()
            page.get_by_role('button', name='退出登录', exact=True).click()
            page.get_by_role('button', name='登录商城' if role == 'USER' else '登录后台', exact=True).wait_for()
            assert 'POST /api/auth/login' in calls and 'POST /api/auth/logout' in calls, calls
            assert not errors, errors
            assert not missing_assets, missing_assets
            results.append(dict(role=role, entry=entry, checks=['entry', 'login', 'product', 'logout'],
                                requests=calls, page_errors=errors, missing_assets=missing_assets))
            context.close()
    finally:
        browser.close()
result = dict(mode='browser_local_api_fixture', database_calls=0, model_calls=0, pages=results)
(out / 'ecommerce-ui-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
