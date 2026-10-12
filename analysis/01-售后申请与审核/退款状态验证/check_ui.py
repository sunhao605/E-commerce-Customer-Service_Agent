"""当前商城/后台页面 + 隔离 Java/H2；业务 API 只转发，模型为替身。"""
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parent
backend = "http://127.0.0.1:18191"
for _ in range(60):
    try:
        if httpx.get(backend + "/actuator/health", timeout=1).json()["status"] == "UP":
            break
    except (httpx.HTTPError, ValueError):
        pass
    time.sleep(0.5)
else:
    raise RuntimeError("隔离 H2 后端未就绪")

errors, decisions = [], []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        def proxy(route):
            url = urlparse(route.request.url)
            response = route.fetch(url=backend + url.path + ("?" + url.query if url.query else ""))
            if url.path.endswith("/approve"):
                decisions.append({"status": response.status, "body": response.json()})
            route.fulfill(response=response)

        buyer = browser.new_context(viewport={"width": 1440, "height": 1000})
        buyer.route("**/api/**", proxy)
        page = buyer.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:5174/")
        page.wait_for_load_state("networkidle")
        page.get_by_label("账号", exact=True).fill("zhangsan")
        page.get_by_label("密码", exact=True).fill("123456")
        page.get_by_role("button", name="登录商城", exact=True).click()
        page.get_by_role("button", name="我的订单", exact=True).click()
        card = page.locator(".order-card").filter(has_text="ORD9200")
        card.get_by_role("button", name="咨询", exact=True).click()
        page.get_by_placeholder("请输入您的问题", exact=True).fill("ORD9200 我要退款，原因：买错了")
        page.get_by_role("button", name="发送", exact=True).click()
        page.get_by_role("button", name="确认提交申请", exact=True).wait_for()
        page.get_by_role("button", name="确认提交申请", exact=True).click()
        page.get_by_text("已提交，待审核", exact=True).wait_for()
        page.get_by_role("button", name="关闭", exact=True).click()
        assert "待发货" in card.inner_text()
        before = buyer.request.get(backend + "/api/shop/balance").json()["data"]["availableBalance"]

        detail = buyer.new_page()
        detail.on("pageerror", lambda error: errors.append(str(error)))
        detail.goto("http://127.0.0.1:5174/orders/ORD9200")
        detail.wait_for_load_state("networkidle")
        assert "待发货" in detail.locator(".info-grid").inner_text()

        reviewer = browser.new_context(viewport={"width": 1440, "height": 1000})
        reviewer.route("**/api/**", proxy)
        admin = reviewer.new_page()
        admin.on("pageerror", lambda error: errors.append(str(error)))
        admin.on("dialog", lambda dialog: dialog.accept())
        admin.goto("http://127.0.0.1:5174/admin/")
        admin.wait_for_load_state("networkidle")
        admin.get_by_label("管理员账号", exact=True).fill("admin")
        admin.get_by_label("密码", exact=True).fill("admin123456")
        admin.get_by_role("button", name="登录后台", exact=True).click()
        admin.get_by_role("button", name="售后审批", exact=True).click()
        admin.get_by_placeholder("订单号", exact=True).fill("ORD9200")
        admin.get_by_role("button", name="查询", exact=True).click()
        admin.locator("tbody tr").filter(has_text="ORD9200").click()
        admin.get_by_label("审批意见", exact=True).fill("隔离浏览器手测通过")
        admin.get_by_role("button", name="通过", exact=True).click()
        admin.get_by_text("售后申请已通过。", exact=True).wait_for()
        assert len(decisions) == 1 and decisions[0]["status"] == 200
        assert buyer.request.get(backend + "/api/shop/balance").json()["data"]["availableBalance"] == before + 200

        # headless 浏览器用 focus 事件确定性复核切回窗口后的读取，不伪造业务结果。
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        card.get_by_text("已退款", exact=True).wait_for()
        page.locator(".section-head select").select_option("REFUNDED")
        page.wait_for_load_state("networkidle")
        assert "ORD9200" in page.locator(".order-list").inner_text()
        page.screenshot(path=str(out / "隔离审批后列表.png"))
        detail.evaluate("window.dispatchEvent(new Event('focus'))")
        detail.locator(".info-grid").get_by_text("已取消", exact=True).wait_for()
        assert detail.locator(".info-grid").get_by_text("已退款", exact=True).count() == 2
        assert "当前订单暂无可发起售后类型" in detail.inner_text("body")
        detail.screenshot(path=str(out / "隔离审批后详情.png"))
        detail.set_viewport_size({"width": 390, "height": 900})
        assert detail.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
    finally:
        browser.close()

result = {"checks": ["商城提交待审核后，后台实际批准", "列表 focus 刷新和已退款筛选", "详情 focus 刷新、订单/支付/履约与售后条件", "手机宽度和无页面异常"],
    "database": "isolated H2", "model": "synthetic stub", "api": "routing proxy to real Java, no synthetic business responses",
    "approvals": decisions, "pageErrors": errors, "existingMySQLWrites": 0, "modelCalls": 0}
(out / "ui-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(result, ensure_ascii=False, indent=2))
