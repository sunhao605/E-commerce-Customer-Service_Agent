"""两条页面路径及少量网关边界；所有提交按钮均不点击。"""
import json
from pathlib import Path
import time

import httpx
from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parent
SITE = "http://127.0.0.1:18103"
PY = "http://127.0.0.1:18101"
CHECKS, TURNS, API_TURNS, TRACES = [], [], [], {}


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def check(ok, label):
    CHECKS.append({"check": label, "passed": bool(ok)})
    print(("PASS " if ok else "FAIL ") + label, flush=True)


def state(sid=""):
    return httpx.get(SITE + "/api/check/state", params={"session": sid}).json()


def turn(page, text):
    with page.expect_response(lambda r: "/api/customer-service/chat" in r.url) as response:
        page.get_by_placeholder("请输入您的问题").fill(text)
        page.get_by_role("button", name="发送", exact=True).click()
    assert response.value.status == 200
    data = response.value.json()["data"]
    expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=15000)
    shots = OUT / "对话截图"
    shots.mkdir(exist_ok=True)
    path = shots / f"{len(TURNS)+1:02d}.png"
    page.locator(".service-drawer").screenshot(path=str(path), style="""
        .service-drawer { position: absolute !important; height: auto !important; max-height: none !important;
            overflow: visible !important; grid-template-rows: auto auto auto !important; }
        .messages { overflow: visible !important; height: auto !important; max-height: none !important; }
    """)
    TURNS.append({"input": text, "response": data, "screenshot": str(path.relative_to(OUT))})
    save("dialogue.json", TURNS)
    return data


def api_chat(client, sid, text, page=None):
    request = {"sessionId": sid, "message": text, "pageContext": page or {"type": "HOME"}}
    response = client.post(SITE + "/api/customer-service/chat", json=request)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    API_TURNS.append({"input": request, "response": data})
    return data


deadline = time.monotonic() + 30
while "ELIGIBILITY_CHECK_READY" not in (OUT / ".runtime/java.log").read_text():
    assert time.monotonic() < deadline, "隔离fixture未就绪"
    time.sleep(0.2)
line = next(line for line in (OUT / ".runtime/java.log").read_text().splitlines() if line.startswith("ELIGIBILITY_DIRECT_CHECKS "))
CHECKS.extend(json.loads(line.removeprefix("ELIGIBILITY_DIRECT_CHECKS ")))
before = state()["tables"]

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        for eligible in (True, False):
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            try:
                page.goto(SITE + "/login", wait_until="networkidle")
                save("initial-ui.json", {"url": page.url, "body": page.locator("body").inner_text()})
                page.get_by_role("button", name="登录商城", exact=True).wait_for(state="visible")
                page.get_by_role("button", name="登录商城", exact=True).click()
                page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
                page.get_by_role("button", name="客服", exact=True).click()
                query = turn(page, ("ORD9801" if eligible else "ORD9812") + " 还能退吗？先别申请")
                sid = query["sessionId"]
                check(not query["confirmRequired"] and not query.get("resumeToken") and not query["actions"]
                    and state(sid)["session"].get("pending") is None, "正常咨询无办理草稿" if eligible else "不符合条件的咨询无草稿")
                if eligible:
                    check("可以申请" in query["answer"] and "200" in query["answer"], "页面返回严格核验后的类型和金额")
                    ack = turn(page, "好的")
                    check(not ack["confirmRequired"] and state(sid)["session"].get("pending") is None, "好的不代表申请")
                    apply = turn(page, "那帮我申请退款")
                    check(apply["relatedOrderNo"] == "ORD9801" and apply["pendingAction"] == "REFUND_REASON_REQUIRED", "明确申请沿用查询订单并询问原因")
                    card = turn(page, "原因：买错了")
                    check(card["confirmRequired"] and card["relatedOrderNo"] == "ORD9801" and "买错了" in card["confirmationSummary"], "复用原确认卡，停在提交前")
                    draft = state(sid)["session"]
                    consulted = turn(page, "ORD9812 还能退吗？只查资格")
                    check(not consulted["confirmRequired"] and draft == state(sid)["session"], "查询另一笔订单不修改旧草稿、版本或凭证")
                    expect(page.get_by_role("button", name="确认提交申请", exact=True).last).to_be_enabled()
                    check(True, "历史确认卡仍可选择，查询不使原卡失效")
                else:
                    check("七天" in query["answer"] and "不满足" in query["answer"] and "可以申请" not in query["answer"], "页面说明超出七天窗口")
                TRACES[sid] = httpx.get(PY + "/sessions/" + sid + "/trace").json()
            finally:
                context.close()
    finally:
        browser.close()

with httpx.Client(timeout=30) as client:
    login = client.post(SITE + "/api/auth/login", json={"username": "zhangsan", "password": "123456"})
    assert login.status_code == 200
    foreign = api_chat(client, "query-foreign", "ORD9899 能退吗？先别申请")
    check("999" not in foreign["answer"] and "不能泄露" not in foreign["answer"] and not foreign["confirmRequired"], "他人订单不泄露金额或客户信息")
    unknown = api_chat(client, "query-unknown", "ORD9813 能退吗？先别申请")
    check("暂时无法核实" in unknown["answer"] and "不满足" not in unknown["answer"], "支付事实缺失不会说成不符合条件")
    waiting = api_chat(client, "query-select", "这笔订单能不能退？先别申请")
    chosen = api_chat(client, "query-select", "第一笔")
    check(waiting["pendingAction"] == "ELIGIBILITY_ORDER_REQUIRED" and "1." in waiting["answer"]
        and chosen["pendingAction"] == "ELIGIBILITY_QUERY" and state("query-select")["session"].get("pending") is None, "选单只续接查询，不创建办理任务")
    direct = api_chat(client, "query-direct", "ORD9801 我要申请退款，原因：买错了")
    check(direct["confirmRequired"] and "买错了" in direct["confirmationSummary"], "直接申请不额外问是否办理")
    api_chat(client, "query-return", "ORD9803 能退吗？先别申请")
    returned = api_chat(client, "query-return", "那帮我申请七天无理由退货")
    check(returned["confirmRequired"] and returned["relatedOrderNo"] == "ORD9803" and "退款原因" not in returned["answer"], "七天无理由退货不额外问原因")
    after = state()["tables"]
    check(before == after, "查询与申请准备阶段8张业务表均未变")
    save("business.json", {"before": before, "after": after})
    api_chat(client, "query-changed", "ORD9802 能退吗？先别申请")
    assert client.post(SITE + "/api/check/ship").status_code == 200
    changed_before = state()["tables"]
    blocked = api_chat(client, "query-changed", "那帮我申请退款，原因：买错了")
    check(not blocked["confirmRequired"] and blocked["pendingAction"] == "BLOCKED", "查询后发货，申请重新核验并阻断")
    check(changed_before == state()["tables"], "测试发货后，阻断申请没有额外业务写入")
    for sid in ("query-select", "query-return", "query-changed", "query-direct"):
        TRACES[sid] = httpx.get(PY + "/sessions/" + sid + "/trace").json()
save("checks.json", CHECKS)
save("api-dialogue.json", API_TURNS)
save("traces.json", TRACES)
result = {"passed": all(c["passed"] for c in CHECKS), "checks": len(CHECKS), "screenshots": len(TURNS),
    "online_models": False, "submitted": False, "failed": [c["check"] for c in CHECKS if not c["passed"]]}
save("result.json", result)
print(json.dumps(result, ensure_ascii=False), flush=True)
raise SystemExit(0 if result["passed"] else 1)
