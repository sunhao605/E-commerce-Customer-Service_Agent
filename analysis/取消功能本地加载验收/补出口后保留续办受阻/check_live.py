"""真实本地商城/模型，复用已有演示订单；不提交申请或支付。"""
import json
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parent
ORDER = "SO20261010124526218-b60d3f08"
RESULTS, TURNS, ERRORS, TRACES = [], [], [], {}
SCENE = ""


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def check(ok, label, **details):
    RESULTS.append({"check": label, "passed": bool(ok), **details})
    save("checks-live.json", RESULTS)
    print(("PASS " if ok else "FAIL ") + label, flush=True)
    return ok


def capture_turn(page, record):
    images = OUT / "对话截图"
    images.mkdir(exist_ok=True)
    filename = f"{len(TURNS) + 1:02d}-{SCENE}.png"
    page.locator(".service-drawer").screenshot(path=str(images / filename), style="""
        .service-drawer { position: absolute !important; height: auto !important;
            max-height: none !important; overflow: visible !important; grid-template-rows: auto auto auto !important; }
        .messages { overflow: visible !important; height: auto !important; max-height: none !important; }
    """)
    TURNS.append({"scene": SCENE, **record, "screenshot": "对话截图/" + filename})
    save("dialogue-live.json", TURNS)


def send(page, message):
    page.get_by_placeholder("请输入您的问题").fill(message)
    with page.expect_response(lambda r: "/api/customer-service/chat" in r.url, timeout=130000) as response:
        page.get_by_role("button", name="发送", exact=True).click()
    assert response.value.status == 200
    data = response.value.json()["data"]
    expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
    capture_turn(page, {"message": message, "response": data})
    return data


def click(page, label):
    with page.expect_response(lambda r: "/api/customer-service/resume" in r.url) as response:
        page.get_by_role("button", name=label, exact=True).last.click()
    assert response.value.status == 200
    data = response.value.json()["data"]
    expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
    capture_turn(page, {"click": label, "response": data})
    return data


scenes = [
    ("还没确定退款订单", "帮我退款。", "REFUND_ORDER_REQUIRED", "结束这次未提交的退款办理", ORDER, "REFUND_REASON_REQUIRED"),
    ("还没说明退款原因", ORDER + " 我要退款。", "REFUND_REASON_REQUIRED", "取消这次尚未提交的售后申请", "原因：买错了。", "CONFIRM_SUBMIT"),
]
save("frozen-live.json", {"score_min": 0.9, "low_sentence": "我想了想，还是不退了", "scenes": scenes,
                          "choices_per_scene": ["取消", "保留"], "expected_action": "score_low，或先明确对象后保留低分决定"})
expect.set_options(timeout=20000)
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, channel="chrome")
    try:
        for scene, first, stage, label, followup, next_stage in scenes:
            for choice in ("取消", "保留"):
                name = scene + "-" + choice
                SCENE = name
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                page.on("pageerror", lambda error: ERRORS.append(str(error)))
                sid = None
                try:
                    page.goto("http://127.0.0.1:5174/", wait_until="networkidle")
                    page.get_by_role("button", name="登录商城", exact=True).click()
                    page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
                    page.get_by_role("button", name="客服", exact=True).click()
                    initial = send(page, first)
                    sid = initial["sessionId"]
                    assert check(not initial.get("confirmRequired") and (initial.get("pendingAction") == stage
                                 or (stage == "REFUND_ORDER_REQUIRED" and "可选订单" in initial["answer"])), name + "：进入原来缺信息的场景")
                    card = send(page, "我想了想，还是不退了")
                    trace = httpx.get("http://127.0.0.1:8000/sessions/" + sid + "/trace").json()
                    judgments = [e["payload"] for e in trace if e["event_type"] == "cancellation_intent_observed"]
                    judgment = judgments[-1] if judgments else {}
                    if judgment.get("action") == "ask_object":
                        assert check(not judgment.get("executed"), name + "：对象不明确先追问，不自动取消", judgment=judgment)
                        card = send(page, "刚才这次退款申请")
                        trace = httpx.get("http://127.0.0.1:8000/sessions/" + sid + "/trace").json()
                        judgment = [e["payload"] for e in trace if e["event_type"] == "cancellation_intent_observed"][-1]
                    assert check(judgment.get("action") in ("score_low", "object_noted") and not judgment.get("executed"),
                                 name + "：真实模型进入低分保留分支", judgment=judgment)
                    assert check(card.get("pendingAction") == stage and card.get("confirmRequired")
                                 and [a["code"] for a in card.get("actions", [])] == ["KEEP", "CANCEL_DRAFT"],
                                 name + "：页面提供保留和取消，不提供提交")
                    expect(page.get_by_role("button", name=label, exact=True).last).to_be_enabled()
                    decision = "CANCEL_DRAFT" if choice == "取消" else "KEEP"
                    result = click(page, label if choice == "取消" else "保留当前办理")
                    if choice == "取消":
                        check(result.get("pendingAction") == "CANCELLED", name + "：点击取消后结束办理")
                        for button in page.locator(".confirm-card button").all():
                            expect(button).to_be_disabled()
                        following = send(page, "第一笔。")
                        check(not following.get("confirmRequired") and not following.get("actions"), name + "：取消后回复序号不重发申请确认卡")
                    else:
                        check(result.get("pendingAction") == stage and not result.get("confirmRequired"), name + "：保留后仍等待原来的信息")
                        following = send(page, followup)
                        check(following.get("pendingAction") == next_stage and bool(following.get("confirmRequired")) == (next_stage == "CONFIRM_SUBMIT"),
                              name + "：补信息后正常继续")
                        if following.get("confirmRequired"):
                            click(page, "取消本次申请")
                    replay = context.request.post("http://127.0.0.1:5174/api/customer-service/resume", data={
                        "sessionId": sid, "resumeToken": card["resumeToken"], "relatedOrderNo": card.get("relatedOrderNo"), "decision": decision})
                    check(replay.status in (403, 409), name + "：旧选择卡不能重复操作", status=replay.status)
                except Exception as error:
                    check(False, name + "：路径未完成", error=str(error))
                finally:
                    if sid:
                        TRACES[name] = httpx.get("http://127.0.0.1:8000/sessions/" + sid + "/trace").json()
                        save("traces-live.json", TRACES)
                    context.close()
        check(not ERRORS, "页面无JavaScript异常", errors=ERRORS)
    finally:
        browser.close()
result = {"passed": all(r["passed"] for r in RESULTS), "checks": len(RESULTS),
          "failed": [r["check"] for r in RESULTS if not r["passed"]], "online_models": True,
          "refund_submissions": 0, "admin_approvals": 0, "new_orders": 0, "payments": 0}
save("result-live.json", result)
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result["passed"] else 1)
