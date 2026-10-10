"""双角色真实浏览器 → 隔离 Java/H2；API 只转发，模型为替身。"""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

out = Path(__file__).resolve().parent
backend = "http://127.0.0.1:18191"
site = "http://127.0.0.1:5174"
checks, errors, writes = [], [], []
expect.set_options(timeout=20000)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        def proxy(route):
            url = urlparse(route.request.url)
            response = route.fetch(url=backend + url.path + ("?" + url.query if url.query else ""))
            if route.request.method == "POST" and not url.path.endswith("/login"):
                writes.append({"path": url.path, "status": response.status})
            route.fulfill(response=response)

        def client_page(path):
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.route("**/api/**", proxy)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(site + path)
            page.wait_for_load_state("networkidle")
            return context, page

        buyer, page = client_page("/")
        page.get_by_label("账号", exact=True).fill("zhangsan")
        page.get_by_label("密码", exact=True).fill("123456")
        page.get_by_role("button", name="登录商城", exact=True).click()
        page.get_by_role("button", name="我的订单", exact=True).click()
        card = page.locator(".order-card").filter(has_text="ORD9400")
        card.wait_for()
        before = buyer.request.get(backend + "/api/shop/balance").json()["data"]["availableBalance"]
        card.get_by_role("button", name="咨询", exact=True).click()
        page.get_by_placeholder("请输入您的问题", exact=True).fill("ORD9400 我要七天无理由退货")
        page.get_by_role("button", name="发送", exact=True).click()
        page.get_by_role("button", name="确认提交申请", exact=True).wait_for()
        expect(page.locator(".confirm-card")).to_contain_text("商家实际收到且验收通过后才退款")
        page.get_by_role("button", name="确认提交申请", exact=True).click()
        page.get_by_text("已提交，待审核", exact=True).wait_for()
        page.get_by_role("button", name="关闭", exact=True).click()
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(card).to_contain_text("退货进度：待审核")
        checks.append("客户确认仅提交RETURN待审核，列表读取同一申请")

        detail = buyer.new_page()
        detail.on("pageerror", lambda error: errors.append(str(error)))
        detail.goto(site + "/orders/ORD9400")
        detail.wait_for_load_state("networkidle")
        expect(detail.locator(".return-panel")).to_contain_text("待审核")
        assert buyer.request.get(backend + "/api/shop/balance").json()["data"]["availableBalance"] == before

        reviewer, admin = client_page("/admin/")
        admin.on("dialog", lambda dialog: dialog.accept())
        admin.get_by_label("管理员账号", exact=True).fill("admin")
        admin.get_by_label("密码", exact=True).fill("admin123456")
        admin.get_by_role("button", name="登录后台", exact=True).click()
        admin.get_by_role("button", name="售后审批", exact=True).click()

        def select_application(order):
            admin.get_by_placeholder("订单号", exact=True).fill(order)
            admin.get_by_role("button", name="查询", exact=True).click()
            admin.wait_for_load_state("networkidle")
            admin.locator("tbody tr").filter(has_text=order).click()
            expect(admin.locator(".detail-panel")).to_contain_text(order)

        select_application("ORD9400")
        expect(admin.get_by_role("button", name="同意退货", exact=True)).to_be_disabled()
        assert admin.get_by_label("审批金额", exact=True).count() == 0
        admin.get_by_label("退货地址", exact=True).fill("隔离浏览器测试地址，请勿寄送")
        admin.get_by_label("退货联系人", exact=True).fill("合成收货员")
        admin.get_by_label("退货联系电话", exact=True).fill("测试电话，不可使用")
        admin.get_by_role("button", name="同意退货", exact=True).click()
        admin.get_by_text("售后申请已同意退货。", exact=True).wait_for()
        assert buyer.request.get(backend + "/api/shop/balance").json()["data"]["availableBalance"] == before
        assert admin.get_by_role("button", name="同意退货", exact=True).count() == 0
        checks.append("后台同意需地址/联系人/电话、不可改金额，首次同意零入账")
        detail.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(detail.locator(".return-panel")).to_contain_text("隔离浏览器测试地址，请勿寄送")
        detail.set_viewport_size({"width": 390, "height": 900})
        assert detail.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        detail.screenshot(path=str(out / "待买家寄回-手机.png"), full_page=True)
        detail.set_viewport_size({"width": 1440, "height": 1000})
        detail.get_by_label("快递公司", exact=True).fill("隔离快递")
        detail.get_by_label("退回物流单号", exact=True).fill("UI-TEST-PARCEL")
        detail.get_by_role("button", name="提交退回物流", exact=True).click()
        expect(detail.locator(".return-panel")).to_contain_text("待商家收货验收")
        assert buyer.request.get(backend + "/api/shop/balance").json()["data"]["availableBalance"] == before
        checks.append("客户focus读退回地址，上传物流推进待验收仍零入账，手机无横向溢出")
        select_application("ORD9400")
        expect(admin.locator(".detail-panel")).to_contain_text("UI-TEST-PARCEL")
        expect(admin.get_by_role("button", name="确认验收并退款", exact=True)).to_be_disabled()
        admin.get_by_label("处理意见", exact=True).fill("合成验收异常：配件缺失，请人工核验")
        admin.get_by_role("button", name="验收未通过", exact=True).click()
        admin.get_by_text("售后申请已记录验收未通过。", exact=True).wait_for()
        detail.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(detail.locator(".return-panel")).to_contain_text("配件缺失")
        assert buyer.request.get(backend + "/api/shop/balance").json()["data"]["availableBalance"] == before
        detail.screenshot(path=str(out / "验收未通过.png"), full_page=True)
        checks.append("验收失败必须填原因，客户端可见且零入账")
        admin.get_by_label("处理意见", exact=True).fill("合成人工核验完成，商品配件赠品完好")
        admin.get_by_label("已实际收到商品且验收通过", exact=True).check()
        admin.get_by_role("button", name="确认验收并退款", exact=True).click()
        admin.get_by_text("售后申请已确认验收并退款。", exact=True).wait_for()
        assert admin.get_by_role("button", name="确认验收并退款", exact=True).count() == 0
        assert buyer.request.get(backend + "/api/shop/balance").json()["data"]["availableBalance"] == before + 200
        admin.screenshot(path=str(out / "后台退款与三阶段记录.png"), full_page=True)
        checks.append("人工核验后勾选收货验收才能退200元，终态无重复资金按钮")
        detail.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(detail.locator(".info-grid").get_by_text("已退回", exact=True)).to_be_visible()
        assert detail.locator(".info-grid").get_by_text("已退款", exact=True).count() == 2
        expect(detail.locator(".return-panel")).to_contain_text("已退款")
        assert detail.get_by_role("button", name="提交退回物流", exact=True).count() == 0
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(card).to_contain_text("退货进度：已退款")
        detail.screenshot(path=str(out / "商城退款完成.png"), full_page=True)
        checks.append("列表、详情、订单支付履约终态一致并保留历史物流")
        detail.goto(site + "/after-sales")
        detail.wait_for_load_state("networkidle")
        expect(detail.locator(".return-panel")).to_contain_text("已退款")
        expect(detail.get_by_role("button", name="查看订单 ORD9400", exact=True)).to_be_visible()
        checks.append("售后记录展示同一真实终态和订单入口")
        assert not errors, errors
        assert all(item["status"] == 200 for item in writes), writes
    finally:
        browser.close()

result = {"checks": checks, "writes": writes, "pageErrors": errors, "database": "isolated H2",
          "api": "proxy to real Java; no synthetic business responses", "model": "synthetic stub",
          "existingMySQLWrites": 0, "modelCalls": 0}
(out / "ui-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(result, ensure_ascii=False, indent=2))
