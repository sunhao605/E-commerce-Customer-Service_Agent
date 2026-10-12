"""仅记录一条当前资格咨询；不进入申请、提交或审核。"""
import hashlib
import json
from pathlib import Path
import httpx
from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
ORDER = "SO20261010124526218-b60d3f08"
MESSAGE = ORDER + " 还能退吗？先别申请。"
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto("http://127.0.0.1:5174", wait_until="networkidle")
        page.get_by_role("button", name="登录商城", exact=True).click()
        page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
        request = page.context.request
        before = {name: request.get("http://127.0.0.1:5174/api/shop/" + name).json()["data"]
                  for name in ["after-sales", "balance", "orders/" + ORDER]}
        page.get_by_role("button", name="客服", exact=True).click()
        page.get_by_placeholder("请输入您的问题").fill(MESSAGE)
        with page.expect_response(lambda r: "/api/customer-service/chat" in r.url, timeout=130000) as response:
            page.get_by_role("button", name="发送", exact=True).click()
        assert response.value.status == 200
        data = response.value.json()["data"]
        expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
        page.locator(".service-drawer").screenshot(path=str(OUT / "当前资格咨询.png"), style="""
            .service-drawer { position: absolute !important; height: auto !important; max-height: none !important;
                overflow: visible !important; grid-template-rows: auto auto auto !important; }
            .messages { overflow: visible !important; height: auto !important; max-height: none !important; }
        """)
        after = {name: request.get("http://127.0.0.1:5174/api/shop/" + name).json()["data"] for name in before}
        trace = httpx.get("http://127.0.0.1:8000/sessions/" + data["sessionId"] + "/trace").json()
        (OUT / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n")
        record = {"input": MESSAGE, "response": data, "same_business_reads": before == after,
                  "before": before, "after": after, "screenshot": "当前资格咨询.png",
                  "scope": "一条新会话的当前表现核验，不是MVP验收或完整模型评测"}
        (OUT / "current.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"input": MESSAGE, "response": data, "same_business_reads": before == after}, ensure_ascii=False))
        assert not data.get("confirmRequired") and before == after
    finally:
        browser.close()
sources = ["backend/agent/tools/planning.py", "backend/resources/prompts/route_contract.md",
           "backend/agent/customer_service_agent.py",
           "ecommerce-backend/src/main/java/com/teachdemo/ecommerce/order/ShopService.java",
           "ecommerce-backend/src/main/java/com/teachdemo/ecommerce/customer_service/CustomerServiceGatewayService.java"]
(OUT / "source.json").write_text(json.dumps({p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                                            for p in sources}, ensure_ascii=False, indent=2) + "\n")
