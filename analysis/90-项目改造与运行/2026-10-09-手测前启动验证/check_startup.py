"""实际本地服务检查；准备一笔演示订单，验证确认卡及取消，不提交退款或批准。"""
import json
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parent
fixture_file = out / "手测订单.json"
with httpx.Client(base_url="http://127.0.0.1:8081", timeout=15) as client:
    assert client.get("/actuator/health").json()["status"] == "UP"
    client.post("/api/auth/login", json={"username": "zhangsan", "password": "123456"}).raise_for_status()
    if fixture_file.exists():
        fixture = json.loads(fixture_file.read_text())
    else:
        products = client.get("/api/shop/products").json()["data"]
        product = min((p for p in products if p["purchaseAvailable"]), key=lambda p: p["price"])
        balance = client.get("/api/shop/balance").json()["data"]["availableBalance"]
        response = client.post("/api/shop/orders", json={"source": "DIRECT_BUY", "productId": product["productId"],
            "quantity": 1, "remark": "2026-10-09 缺口一手测专用；未提交退款"})
        response.raise_for_status()
        order = response.json()["data"]
        fixture = {"orderNo": order["orderNo"], "product": product["name"], "amount": order["totalAmount"],
            "balanceBeforePayment": balance, "source": "本地演示库，真实下单及模拟余额支付接口"}
        fixture_file.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n")
    order_no = fixture["orderNo"]
    order = client.get(f"/api/shop/orders/{order_no}").json()["data"]
    if order["paymentStatus"] != "PAID":
        client.post(f"/api/shop/orders/{order_no}/pay").raise_for_status()
    order = client.get(f"/api/shop/orders/{order_no}").json()["data"]
    assert order["paymentStatus"] == "PAID" and order["fulfillmentStatus"] == "UNSHIPPED", order
    records_before = client.get("/api/shop/after-sales").json()["data"]
    balance_before = client.get("/api/shop/balance").json()["data"]["availableBalance"]

errors, responses = [], []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        def capture(response):
            if response.url.endswith(("/api/customer-service/chat", "/api/customer-service/resume")):
                responses.append({"status": response.status, "data": response.json()["data"]})
        page.on("response", capture)
        page.goto("http://127.0.0.1:5174/")
        page.wait_for_load_state("networkidle")
        page.get_by_label("账号", exact=True).fill("zhangsan")
        page.get_by_label("密码", exact=True).fill("123456")
        page.get_by_role("button", name="登录商城", exact=True).click()
        page.get_by_role("button", name="咨询客服", exact=True).click()
        page.get_by_placeholder("请输入您的问题", exact=True).fill(f"{order_no} 我要申请退款，原因：买错了")
        page.get_by_role("button", name="发送", exact=True).click()
        page.get_by_role("button", name="确认提交申请", exact=True).wait_for(timeout=60000)
        assert responses[-1]["status"] == 200 and responses[-1]["data"]["confirmRequired"]
        assert not responses[-1]["data"]["fallback"], responses[-1]
        card = page.locator(".confirm-card").last
        assert all(term in card.inner_text() for term in [order_no, "买错了", "整单退款", "尚未提交"])
        page.screenshot(path=str(out / "实际确认卡.png"))
        page.get_by_role("button", name="取消本次申请", exact=True).click()
        page.get_by_text("本次退款申请已取消，未提交。", exact=True).last.wait_for()
        assert responses[-1]["status"] == 200
        assert page.get_by_role("button", name="确认提交申请", exact=True).is_disabled()
        page.screenshot(path=str(out / "实际取消.png"))
        admin = browser.new_page()
        admin.goto("http://127.0.0.1:5174/admin/")
        admin.wait_for_load_state("networkidle")
        admin.get_by_label("管理员账号", exact=True).fill("admin")
        admin.get_by_label("密码", exact=True).fill("admin123456")
        admin.get_by_role("button", name="登录后台", exact=True).click()
        admin.get_by_role("button", name="售后审批", exact=True).click()
        admin.get_by_role("heading", name="售后申请", exact=True).wait_for()
        assert not errors, errors
    finally:
        browser.close()
with httpx.Client(base_url="http://127.0.0.1:8081", timeout=15) as client:
    client.post("/api/auth/login", json={"username": "zhangsan", "password": "123456"}).raise_for_status()
    assert client.get("/api/shop/after-sales").json()["data"] == records_before
    assert client.get("/api/shop/balance").json()["data"]["availableBalance"] == balance_before
    transactions = client.get("/api/shop/balance/transactions").json()["data"]
    assert sum(t["type"] == "PAYMENT" and t["orderNo"] == order_no for t in transactions) == 1
result = {"date": "2026-10-09", "mode": "最新 Java/MySQL、真实商城浏览器、已启用在线模型的 Python；没有替换接口响应",
    "fixture": fixture, "balanceAfterPayment": balance_before, "chatResponses": responses, "pageErrors": errors,
    "checks": ["健康及登录", "下单及模拟支付流水", "实际客服确认卡", "取消无售后写入或退款余额变化", "后台售后页面"],
    "refundSubmissions": 0, "adminApprovals": 0}
(out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({k: v for k, v in result.items() if k != "chatResponses"}, ensure_ascii=False, indent=2))
