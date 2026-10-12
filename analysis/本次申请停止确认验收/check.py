"""实际页面逐轮截图；scripted隔离H2，live复用已有MySQL订单，不提交申请。"""
import json
from pathlib import Path
import sys
import httpx
from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parent
MODE = sys.argv[1]
assert MODE in ("scripted", "live")
SITE, PY = ("http://127.0.0.1:18103", "http://127.0.0.1:18101") if MODE == "scripted" else ("http://127.0.0.1:5174", "http://127.0.0.1:8000")
ORDER = "ORD9801" if MODE == "scripted" else "SO20261010124526218-b60d3f08"
RESULTS, TURNS, TRACES, ERRORS = [], [], {}, []
SCENE = ""


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def check(ok, label, **details):
    RESULTS.append({"check": label, "passed": bool(ok), **details})
    save(f"checks-{MODE}.json", RESULTS)
    print(("PASS " if ok else "FAIL ") + label, flush=True)
    return ok


def capture(page, record):
    images = OUT / f"对话截图-{MODE}"
    images.mkdir(exist_ok=True)
    filename = f"{len(TURNS)+1:02d}-{SCENE}.png"
    page.locator(".service-drawer").screenshot(path=str(images / filename), style="""
        .service-drawer { position: absolute !important; height: auto !important; max-height: none !important;
            overflow: visible !important; grid-template-rows: auto auto auto !important; }
        .messages { overflow: visible !important; height: auto !important; max-height: none !important; }
    """)
    TURNS.append({"scene": SCENE, **record, "screenshot": f"对话截图-{MODE}/" + filename})
    save(f"dialogue-{MODE}.json", TURNS)


def send(page, message):
    page.get_by_placeholder("请输入您的问题").fill(message)
    with page.expect_response(lambda r: "/api/customer-service/chat" in r.url, timeout=130000) as response:
        page.get_by_role("button", name="发送", exact=True).click()
    assert response.value.status == 200
    data = response.value.json()["data"]
    expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
    capture(page, {"message": message, "response": data})
    return data


def click(page, label):
    with page.expect_response(lambda r: "/api/customer-service/resume" in r.url) as response:
        page.get_by_role("button", name=label, exact=True).last.click()
    assert response.value.status == 200
    data = response.value.json()["data"]
    expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
    capture(page, {"click": label, "response": data})
    return data


scenes = [
    ("还没确定退款订单", "帮我退款。", "REFUND_ORDER_REQUIRED", "结束这次未提交的退款办理", ORDER, "REFUND_REASON_REQUIRED", "退款"),
    ("还没说明退款原因", ORDER + " 我要退款。", "REFUND_REASON_REQUIRED", "取消这次尚未提交的售后申请", "原因：买错了。", "CONFIRM_SUBMIT", "退款"),
]
if MODE == "scripted":
    scenes += [
        ("退款等待提交确认", "ORD9801 我要退款，原因：买错了。", "CONFIRM_SUBMIT", "取消这次尚未提交的售后申请", None, None, "退款"),
        ("退货退款等待提交确认", "ORD9803 我要申请七天无理由退货。", "CONFIRM_SUBMIT", "取消这次尚未提交的售后申请", None, None, "退货退款"),
    ]
save(f"frozen-{MODE}.json", {"scenes": scenes, "score_min": 0.9, "low_sentence": "我想了想，还是不退了"})
before = httpx.get(SITE + "/api/check/state").json()["tables"] if MODE == "scripted" else None
expect.set_options(timeout=20000)
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        for scene, first, stage, label, followup, next_stage, kind in scenes:
            for choice in ("取消", "保留"):
                SCENE = scene + "-" + choice
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                page.on("pageerror", lambda error: ERRORS.append(str(error)))
                sid = None
                try:
                    page.goto(SITE, wait_until="networkidle")
                    page.get_by_role("button", name="登录商城", exact=True).click()
                    page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
                    page.get_by_role("button", name="客服", exact=True).click()
                    initial = send(page, first)
                    sid = initial["sessionId"]
                    assert check(initial.get("pendingAction") == stage or (stage == "REFUND_ORDER_REQUIRED" and "可选订单" in initial["answer"]), SCENE + "：进入预定场景")
                    card = send(page, "我想了想，还是不退了")
                    events = httpx.get(PY + "/sessions/" + sid + "/trace").json()
                    judgment = [e["payload"] for e in events if e["event_type"] == "cancellation_intent_observed"][-1]
                    assert check(judgment.get("components", {}).get("target") == "current" and any(score < 0.9 for score in judgment.get("scores", {}).values())
                                 and judgment.get("action") == "score_low" and not judgment.get("executed"), SCENE + "：当前申请评分不足，不自动取消", judgment=judgment)
                    assert check(card["answer"] == f"是否停止本次尚未提交的{kind}申请？"
                                 and card.get("confirmRequired") and [a["code"] for a in card.get("actions", [])] == ["KEEP", "CANCEL_DRAFT"],
                                 SCENE + "：直接确认本次申请，只提供保留和取消")
                    assert check("购买订单" not in card["answer"] and "取消订单" not in card["answer"] and "继续保留" not in card["answer"], SCENE + "：没有订单二选一或额外说明")
                    if initial.get("resumeToken"):
                        stale = context.request.post(SITE + "/api/customer-service/resume", data={"sessionId": sid,
                            "resumeToken": initial["resumeToken"], "relatedOrderNo": initial.get("relatedOrderNo"), "decision": "CONFIRM_SUBMIT"})
                        check(stale.status in (403, 409), SCENE + "：原提交卡失效", status=stale.status)
                    decision = "CANCEL_DRAFT" if choice == "取消" else "KEEP"
                    result = click(page, label if choice == "取消" else "保留当前办理")
                    if choice == "取消":
                        check(result.get("pendingAction") == "CANCELLED", SCENE + "：点击后结束办理")
                        for button in page.locator(".confirm-card button").all():
                            expect(button).to_be_disabled()
                    else:
                        ready = stage == "CONFIRM_SUBMIT"
                        check(result.get("pendingAction") == ("CONFIRM_SUBMIT" if ready else "KEEP")
                              and bool(result.get("confirmRequired")) == ready, SCENE + "：保留后恢复原办理状态")
                        if followup:
                            following = send(page, followup)
                            check(following.get("pendingAction") == next_stage, SCENE + "：补信息后正常继续")
                            ready = next_stage == "CONFIRM_SUBMIT"
                        if ready:
                            click(page, "取消本次申请")
                    replay = context.request.post(SITE + "/api/customer-service/resume", data={"sessionId": sid,
                        "resumeToken": card["resumeToken"], "relatedOrderNo": card.get("relatedOrderNo"), "decision": decision})
                    check(replay.status in (403, 409), SCENE + "：旧停止确认卡不能重复操作", status=replay.status)
                except Exception as error:
                    check(False, SCENE + "：路径未完成", error=str(error))
                finally:
                    if sid:
                        TRACES[SCENE] = httpx.get(PY + "/sessions/" + sid + "/trace").json()
                        save(f"traces-{MODE}.json", TRACES)
                    context.close()
        check(not ERRORS, "页面无JavaScript异常", errors=ERRORS)
    finally:
        browser.close()
if MODE == "scripted":
    after = httpx.get(SITE + "/api/check/state").json()["tables"]
    save("business-scripted.json", {"before": before, "after": after})
    check(before == after, "8张隔离业务表内容完全一致")
result = {"passed": all(r["passed"] for r in RESULTS), "checks": len(RESULTS), "screenshots": len(TURNS),
          "failed": [r["check"] for r in RESULTS if not r["passed"]], "online_models": MODE == "live"}
save(f"result-{MODE}.json", result)
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["passed"] else 1)
