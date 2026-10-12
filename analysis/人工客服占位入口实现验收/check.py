"""真实隔离页面／HTTP、固定Agent、内存H2；不确认任何售后提交。"""
import json
import os
from pathlib import Path
import time

import httpx
from playwright.sync_api import expect, sync_playwright

BASE = Path(__file__).resolve().parent
BEFORE = os.environ.get("HUMAN_CHECK_BEFORE") == "1"
OUT = BASE / ("before" if BEFORE else "after")
SITE = "http://127.0.0.1:18303"
CHECKS, TURNS, RESUMES = [], [], []


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def check(ok, label):
    CHECKS.append({"check": label, "passed": bool(ok)})
    save("checks.json", CHECKS)
    print(("PASS " if ok else "FAIL ") + label, flush=True)
    if not BEFORE:
        assert ok, label


def state(sid=""):
    response = httpx.get(SITE + "/api/check/state", params={"session": sid})
    response.raise_for_status()
    return response.json()


def shot(page, label, response=None, whole=False):
    path = OUT / "截图" / f"{len(TURNS)+1:02d}.png"
    path.parent.mkdir(exist_ok=True)
    if whole:
        page.screenshot(path=str(path), full_page=True)
    else:
        page.locator(".service-drawer").screenshot(path=str(path), style="""
          .service-drawer { position: absolute !important; height: auto !important; max-height: none !important;
            overflow: visible !important; grid-template-rows: auto auto auto !important; }
          .messages { overflow: visible !important; height: auto !important; max-height: none !important; }
        """)
    TURNS.append({"input_or_action": label, "response": response, "url": page.url, "screenshot": str(path.relative_to(OUT))})
    save("dialogue.json", TURNS)


def turn(page, text):
    with page.expect_response(lambda r: "/api/customer-service/chat" in r.url) as response:
        page.get_by_placeholder("请输入您的问题").fill(text)
        page.get_by_role("button", name="发送", exact=True).click()
    assert response.value.status == 200
    data = response.value.json()["data"]
    expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=15000)
    shot(page, text, data)
    return data


def login(browser, mobile=False):
    context = browser.new_context(viewport={"width": 390 if mobile else 1440, "height": 844 if mobile else 1000})
    page = context.new_page()
    page.on("request", lambda r: RESUMES.append(r.url) if "/api/customer-service/resume" in r.url else None)
    page.goto(SITE + "/login", wait_until="networkidle")
    page.get_by_role("button", name="登录商城", exact=True).click()
    page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
    page.get_by_role("button", name="客服", exact=True).click()
    return context, page


deadline = time.monotonic() + 30
while "HUMAN_SUPPORT_CHECK_READY" not in (OUT / "java.log").read_text():
    assert time.monotonic() < deadline, "隔离样本未就绪"
    time.sleep(0.2)
before = state()["tables"]

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        for text, reason in [
            ("ORD9801 收到时就破了，我只要换一件，不要退款。", "换货"),
            ("ORD9801 两件里只退一件，给我部分退款。", "部分退款"),
            ("ORD9801 只退这件商品，其他商品不退。", "部分退款"),
            ("ORD9801 的椅子坏了，帮我维修。", "维修"),
            ("ORD9801 少件了，帮我补发。", "补发"),
            ("ORD9801 商品破损，请处理质量问题。", "质量问题"),
        ]:
            context, page = login(browser)
            try:
                calls = httpx.get(SITE + "/api/check/agent-calls").json()["count"]
                result = turn(page, text)
                check(result["pendingAction"] == "HUMAN_SUPPORT" and reason in result["answer"], reason + "进入人工占位确认卡")
                check(state(result["sessionId"])["session"].get("pending") is None and not result.get("resumeToken"), reason + "没有准备售后申请或提交凭证")
                if not BEFORE:
                    check(httpx.get(SITE + "/api/check/agent-calls").json()["count"] == calls, reason + "执行前阻断，不依赖固定Agent错误退款标签")
                    check([a["code"] for a in result["actions"]] == ["OPEN_HUMAN_SUPPORT", "STAY_WITH_AGENT"], reason + "动作仅前往或暂不前往")
            finally:
                context.close()

        if not BEFORE:
            context, page = login(browser)
            try:
                card = turn(page, "不用换货，ORD9801 还没发货，我要整单退款，原因：买错了")
                sid = card["sessionId"]
                check(card["confirmRequired"] and card["pendingAction"] == "CONFIRM_SUBMIT", "否定换货后正常整单退款仍可准备")
                draft = state(sid)["session"]
                consult = turn(page, "换货政策是什么？")
                check(consult["pendingAction"] == "HUMAN_SUPPORT" and state(sid)["session"] == draft, "政策咨询展示入口但保留原草稿版本和凭证")
                expect(page.get_by_role("button", name="确认提交申请", exact=True)).to_be_enabled()
                page.get_by_role("button", name="暂不前往", exact=True).click()
                shot(page, "点击暂不前往")
                check("已留在客服助手" in page.locator(".messages").inner_text(), "暂不前往显示选择结果且保持当前对话")
                handoff = turn(page, "请转人工客服")
                check(handoff["pendingAction"] == "HUMAN_SUPPORT" and state(sid)["session"] == draft, "单独要求人工入口不取消原申请")
                page.get_by_role("button", name="确认前往", exact=True).last.click()
                expect(page.get_by_role("heading", name="人工客服功能开发中", exact=True)).to_be_visible()
                shot(page, "确认前往说明页面", whole=True)
                check(page.url.endswith("/human-support") and len(RESUMES) == 0, "确认前往只跳转独立页面，没有调用售后resume")
                page.reload(wait_until="networkidle")
                expect(page.get_by_role("heading", name="人工客服功能开发中", exact=True)).to_be_visible()
                shot(page, "刷新说明页面", whole=True)
                check(True, "人工入口支持直接路由和刷新")
            finally:
                context.close()

            # 另开对话核验返回时保留会话；刷新整页会按现有商城规则重建前端对话。
            context, page = login(browser, mobile=True)
            try:
                card = turn(page, "ORD9801 我要申请退款，原因：买错了")
                sid = card["sessionId"]
                turn(page, "找人工客服")
                page.get_by_role("button", name="确认前往", exact=True).click()
                expect(page.get_by_role("heading", name="人工客服功能开发中", exact=True)).to_be_visible()
                shot(page, "手机前往说明页", whole=True)
                check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "390像素说明页面无横向溢出")
                page.get_by_role("button", name="返回客服助手", exact=True).click()
                expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()
                shot(page, "返回原客服对话")
                check("找人工客服" in page.locator(".messages").inner_text(), "返回客服助手保留当前对话")
                replaced = turn(page, "不要退款，改换货")
                check(replaced["pendingAction"] == "HUMAN_SUPPORT" and "确认卡已失效" in replaced["answer"], "明确改换货说明旧未提交卡失效")
                check(state(sid)["session"].get("token") is None and state(sid)["session"].get("pending") is None, "旧退款凭证及待办已清理")
                expect(page.get_by_role("button", name="确认提交申请", exact=True)).to_be_disabled()
                old = page.request.post(SITE + "/api/customer-service/resume", data={"sessionId": sid, "resumeToken": card["resumeToken"], "relatedOrderNo": card["relatedOrderNo"], "decision": "CONFIRM_SUBMIT"})
                save("old-token-rejection.json", {"status": old.status, "body": old.json()})
                check(old.status in {400, 403, 409}, "旧凭证无法直接提交，服务端拒绝")
            finally:
                context.close()

            for text, human in [
                ("核验失败样本", True),
                ("ORD9801 退款审批样本，原因：买错了", False),
                ("ORD9803 申请七天无理由整单退货", False),
                ("不要转人工，ORD9801 未发货，我要整单退款，原因：买错了", False),
                ("不换货，不需要帮我转人工，ORD9801 未发货，我要整单退款，原因：买错了", False),
                ("我只退款，不换货，ORD9801 这件商品未发货，我要申请整单退款，原因：买错了", False),
            ]:
                context, page = login(browser)
                try:
                    result = turn(page, text)
                    check((result["pendingAction"] == "HUMAN_SUPPORT") == human, text + "路由正确")
                    if not human:
                        check(result["confirmRequired"] and result["pendingAction"] == "CONFIRM_SUBMIT", "受支持办理仍使用原本人确认卡")
                finally:
                    context.close()
            context, page = login(browser)
            try:
                response = page.goto("http://127.0.0.1:18302/human-support", wait_until="networkidle")
                expect(page.get_by_role("heading", name="人工客服功能开发中", exact=True)).to_be_visible()
                shot(page, "实际Java静态服务直达人工页", whole=True)
                check(response.status == 200, "Java静态部署的新页面直达返回200")
                response = page.reload(wait_until="networkidle")
                expect(page.get_by_role("heading", name="人工客服功能开发中", exact=True)).to_be_visible()
                shot(page, "实际Java静态服务刷新人工页", whole=True)
                check(response.status == 200, "Java静态部署的新页面刷新返回200")
            finally:
                context.close()
    finally:
        browser.close()

if not BEFORE:
    with httpx.Client(base_url=SITE) as client:
        client.post("/api/auth/login", json={"username": "zhangsan", "password": "123456"}).raise_for_status()
        submitted = state("submitted-synthetic")["session"]
        r = client.post("/api/customer-service/chat", json={"sessionId": "submitted-synthetic", "message": "改换货", "pageContext": {"type": "HOME"}})
        r.raise_for_status()
        save("submitted-session-contract.json", {"before": submitted, "response": r.json(), "after": state("submitted-synthetic")["session"]})
        check(state("submitted-synthetic")["session"] == submitted, "已提交会话不因换货出口撤回或改变原状态（合成会话）")

after = state()["tables"]
save("business.json", {"before": before, "after": after})
check(before == after, "申请准备、人工入口与旧卡拒绝后8张H2业务表未变")
save("result.json", {"phase": "before" if BEFORE else "after", "passed": all(c["passed"] for c in CHECKS),
                     "checks": len(CHECKS), "failed": [c["check"] for c in CHECKS if not c["passed"]],
                     "screenshots": len(TURNS), "online_models": 0, "database": "isolated H2",
                     "submitted": False, "browser_resume_requests": RESUMES})
