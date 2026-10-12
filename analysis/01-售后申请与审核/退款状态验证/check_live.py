"""只读核对用户已退款订单在当前 MySQL 和商城页面中的展示。"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parent
order_no = "SO20261009061332122-26971b7b"
errors = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:5174/")
        page.wait_for_load_state("networkidle")
        page.get_by_label("账号", exact=True).fill("zhangsan")
        page.get_by_label("密码", exact=True).fill("123456")
        page.get_by_role("button", name="登录商城", exact=True).click()
        page.get_by_role("button", name="我的订单", exact=True).click()
        card = page.locator(".order-card").filter(has_text=order_no)
        card.get_by_text("已退款", exact=True).wait_for()
        page.locator(".section-head select").select_option("REFUNDED")
        page.wait_for_load_state("networkidle")
        assert order_no in page.locator(".order-list").inner_text()
        page.screenshot(path=str(out / "实际订单列表.png"))
        card.get_by_role("button", name="详情", exact=True).click()
        grid = page.locator(".info-grid")
        grid.get_by_text("已取消", exact=True).wait_for()
        assert grid.get_by_text("已退款", exact=True).count() == 2
        assert "当前订单暂无可发起售后类型" in page.inner_text("body")
        page.screenshot(path=str(out / "实际订单详情.png"))
        assert not errors, errors
    finally:
        browser.close()
(out / "live-ui-result.json").write_text(json.dumps({"orderNo": order_no,
    "checks": ["实际订单列表显示已退款", "已退款筛选命中", "详情订单/支付已退款、履约已取消"],
    "mode": "真实商城/Java/MySQL，没有替换接口响应", "pageErrors": errors,
    "modelCalls": 0, "refundOrApprovalWrites": 0}, ensure_ascii=False, indent=2) + "\n")
print("当前 MySQL 订单在商城列表、筛选和详情展示正确；无资金或审批写入。")
