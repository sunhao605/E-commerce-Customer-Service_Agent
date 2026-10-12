"""只在隔离H2验证保留后补订单；每条消息及按钮点击留截图。"""
import json
import os
from pathlib import Path
import sys
import httpx
from playwright.sync_api import expect, sync_playwright

OUT = Path(os.environ["CANCEL_CHECK_OUT"])
LIVE = sys.argv[1] == "live"
SITE, PY = ("http://127.0.0.1:5174", "http://127.0.0.1:8000") if LIVE else ("http://127.0.0.1:18103", "http://127.0.0.1:18101")
CHECKS, TURNS = [], []


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def check(ok, name, **details):
    CHECKS.append({"check": name, "passed": bool(ok), **details})
    print(("PASS " if ok else "FAIL ") + name, flush=True)


def turn(page, scene, text, button=False):
    path = "/api/customer-service/resume" if button else "/api/customer-service/chat"
    with page.expect_response(lambda r: path in r.url, timeout=130000) as response:
        if button:
            page.get_by_role("button", name=text, exact=True).last.click()
        else:
            page.get_by_placeholder("请输入您的问题").fill(text)
            page.get_by_role("button", name="发送", exact=True).click()
    assert response.value.status == 200
    data = response.value.json()["data"]
    expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
    images = OUT / "对话截图"
    images.mkdir(exist_ok=True)
    image = f"{len(TURNS)+1:02d}-{scene}.png"
    page.locator(".service-drawer").screenshot(path=str(images / image), style="""
        .service-drawer { position: absolute !important; height: auto !important; max-height: none !important;
            overflow: visible !important; grid-template-rows: auto auto auto !important; }
        .messages { overflow: visible !important; height: auto !important; max-height: none !important; }
    """)
    TURNS.append({"scene": scene, "input": text, "button": button, "response": data,
                  "screenshot": "对话截图/" + image})
    save("dialogue.json", TURNS)
    return data


def trace_count(sid):
    events = httpx.get(PY + "/sessions/" + sid + "/trace").json()
    return sum(e["event_type"] == "cancellation_intent_observed" for e in events)


scenes = [("正常续办", "SO20261010124526218-b60d3f08")] if LIVE else [
    ("正常续办", "ORD9801"), ("未决确认与不存在订单", "ORD9999")]
save("frozen.json", {"scenarios": scenes, "online_models": LIVE,
    "model_stub": None if LIVE else "故意把纯订单号判成stop/current，决定分0.3，对象分0.51",
    "submission": "不提交、不审批、不退款"})
before = None if LIVE else httpx.get(SITE + "/api/check/state").json()["tables"]
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        for scene, order in scenes:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            try:
                page.goto(SITE, wait_until="networkidle")
                page.get_by_role("button", name="登录商城", exact=True).click()
                page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
                page.get_by_role("button", name="客服", exact=True).click()
                initial = turn(page, scene, "帮我退款。")
                sid = initial["sessionId"]
                card = turn(page, scene, "我想了想，还是不退了")
                if not card.get("confirmRequired"):
                    card = turn(page, scene, "刚才这次退款申请")
                check(card.get("confirmRequired") and [a["code"] for a in card["actions"]] == ["KEEP", "CANCEL_DRAFT"], scene + "：出现停止选择卡")
                if order == "ORD9999":
                    waiting = turn(page, scene, "ORD9801")
                    check(waiting.get("confirmRequired") and waiting.get("pendingAction") == "REFUND_ORDER_REQUIRED", "未处理停止确认时补订单不能绕过选择")
                    card = waiting
                kept = turn(page, scene, "保留当前办理", button=True)
                check(kept.get("pendingAction") == "KEEP", scene + "：明确保留成功")
                count = trace_count(sid)
                following = turn(page, scene, order)
                check(trace_count(sid) == count, scene + "：纯订单号不重复调用取消判断")
                if order != "ORD9999":
                    check(following.get("pendingAction") == "REFUND_REASON_REQUIRED" and not following.get("confirmRequired")
                          and "退款原因" in following["answer"] and following.get("relatedOrderNo") == order,
                          "保留后补本人订单正常询问退款原因，不再显示停止卡")
                    cancelled = turn(page, scene, "请结束当前这份尚未提交的退款申请。")
                    if cancelled.get("confirmRequired"):
                        cancel = next(a["label"] for a in cancelled["actions"] if a["code"] == "CANCEL_DRAFT")
                        cancelled = turn(page, scene, cancel, button=True)
                    check(cancelled.get("pendingAction") == "CANCELLED", "补订单后重新表达取消仍可取消")
                else:
                    check(following.get("pendingAction") == "BLOCKED" and not following.get("confirmRequired"), "不存在订单仍被业务校验拒绝")
                replay = context.request.post(SITE + "/api/customer-service/resume", data={"sessionId": sid,
                    "resumeToken": card["resumeToken"], "decision": "KEEP"})
                check(replay.status in (403, 409), scene + "：旧停止卡不可复用")
            finally:
                context.close()
    finally:
        browser.close()
if not LIVE:
    after = httpx.get(SITE + "/api/check/state").json()["tables"]
    check(before == after, "8张隔离业务表未变")
    save("business.json", {"before": before, "after": after})
save("checks.json", CHECKS)
result = {"passed": all(c["passed"] for c in CHECKS), "checks": len(CHECKS), "screenshots": len(TURNS),
          "failed": [c["check"] for c in CHECKS if not c["passed"]], "online_models": LIVE}
save("result.json", result)
raise SystemExit(0 if result["passed"] else 1)
