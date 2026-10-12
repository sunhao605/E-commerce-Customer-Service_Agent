"""新样本两轮与页面闭环。失败保留原始证据，误取消立即停止该批。"""
import hashlib
import http.cookiejar
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright, expect

OUT = Path(__file__).resolve().parent
MODE = sys.argv[1]
BASE = "http://127.0.0.1:18102"
PY = "http://127.0.0.1:18101"
SITE = "http://127.0.0.1:18103"
CLIENT = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
RESULTS, TURNS = [], []


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def call(path, body=None):
    req = urllib.request.Request(BASE + path, None if body is None else json.dumps(body).encode(),
                                 {"Content-Type":"application/json"})
    try:
        response = CLIENT.open(req, timeout=130)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = json.load(response)
    if body is not None:
        TURNS.append({"path":path, "request":body, "status":response.status, "response":raw})
        save(f"dialogue-{MODE}.json", TURNS)
    return response.status, raw.get("data", raw)


def check(ok, label, **detail):
    RESULTS.append({"check":label, "passed":bool(ok), **detail})
    save(f"checks-{MODE}.json", RESULTS)
    print(("PASS " if ok else "FAIL ") + label, flush=True)
    return ok


def chat(session, message):
    code, data = call("/api/customer-service/chat", {"sessionId":session, "message":message, "pageContext":{"type":"HOME"}})
    assert code == 200, (code, data)
    return data


def resume(card, decision):
    return call("/api/customer-service/resume", {"sessionId":card["sessionId"], "resumeToken":card["resumeToken"],
        "relatedOrderNo":card.get("relatedOrderNo"), "decision":decision})


def state(session=""):
    code, data = call("/api/check/state?session=" + session)
    assert code == 200
    return data


def trace(session):
    with urllib.request.urlopen(PY + "/sessions/" + session + "/trace") as response:
        events = json.load(response)
    return [e["payload"] for e in events if e["event_type"] == "cancellation_intent_observed"]


def cases():
    frozen = (OUT / "cases.json").read_bytes()
    save("frozen.json", {"sha256":hashlib.sha256(frozen).hexdigest(), "rounds":2, "score_min":0.9,
                         "stop_criterion":"自动取消或保留且返回可点击的取消入口", "cases":json.loads(frozen)})
    for round_no in (1, 2):
        for case in json.loads(frozen):
            sid = f"closure-{round_no}-{case['id']}"
            card = chat(sid, "ORD9801 我要退款，原因：买错了。")
            assert card.get("confirmRequired"), card
            original_state = state(sid)["session"]
            reply = chat(sid, case["message"])
            snapshot = state(sid)["session"]
            judgments = trace(sid)
            judgment = judgments[-1] if judgments else {}
            cancelled = snapshot["pending"] == "CANCELLED"
            safe = case["group"] == "stop" or not cancelled
            buttons = [a["code"] for a in reply.get("actions", [])]
            actionable = cancelled or reply.get("confirmRequired") and any(b in buttons for b in ("CANCEL_SUBMIT", "CANCEL_DRAFT"))
            action_ok = judgment.get("action") in case["allowed"]
            draft_kept = snapshot["pending"] == "CONFIRM_SUBMIT" and snapshot.get("order") == original_state.get("order") \
                and snapshot["revision"] == original_state["revision"]
            check(safe and action_ok and (actionable if case["group"] == "stop" else draft_kept),
                  f"新样本 R{round_no}-{case['id']} {case['group']}",
                  safe=safe, action_ok=action_ok, cancellation_exit=bool(actionable), draft_kept=draft_kept, message=case["message"],
                  judgment=judgment, response=reply, state=snapshot)
            if not safe:
                raise AssertionError("发现未授权取消，停止本批")
    assert hashlib.sha256((OUT / "cases.json").read_bytes()).hexdigest() == hashlib.sha256(frozen).hexdigest()


def browser_tests():
    errors = []
    expect.set_options(timeout=20000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        contexts = []
        try:
            def open_chat():
                context = browser.new_context(viewport={"width":1440, "height":1000})
                contexts.append(context)
                page = context.new_page()
                page.on("pageerror", lambda err: errors.append(str(err)))
                page.goto(SITE, wait_until="networkidle")
                page.get_by_role("button", name="登录商城", exact=True).click()
                page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
                page.get_by_role("button", name="客服", exact=True).click()
                expect(page.get_by_placeholder("请输入您的问题")).to_be_visible()
                return page

            def send(page, message):
                page.get_by_placeholder("请输入您的问题").fill(message)
                with page.expect_response(lambda r:"/api/customer-service/chat" in r.url, timeout=130000) as pending:
                    page.get_by_role("button", name="发送", exact=True).click()
                response = pending.value
                assert response.status == 200
                data = response.json()["data"]
                TURNS.append({"browser":True, "message":message, "response":data})
                save(f"dialogue-{MODE}.json", TURNS)
                expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
                return data

            def click(page, label):
                with page.expect_response(lambda r:"/api/customer-service/resume" in r.url) as pending:
                    page.get_by_role("button", name=label, exact=True).last.click()
                response = pending.value
                data = response.json()["data"]
                TURNS.append({"browser":True, "click":label, "status":response.status, "response":data})
                save(f"dialogue-{MODE}.json", TURNS)
                expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
                assert response.status == 200
                return data

            page = open_chat()
            original = send(page, "ORD9801 我要退款，原因：买错了。")
            low = send(page, "我想了想，还是不退了")
            check(low.get("confirmRequired") and low["resumeToken"] == original["resumeToken"]
                  and "取消本次申请" in low["answer"], "页面：低分重发原卡和原凭证")
            expect(page.get_by_role("button", name="取消本次申请", exact=True).last).to_be_enabled()
            page.screenshot(path=str(OUT / f"{MODE}-低分取消入口.png"), full_page=True)
            cancelled = click(page, "取消本次申请")
            check(cancelled.get("pendingAction") == "CANCELLED", "页面：点击原卡取消成功")
            check(resume(original, "CONFIRM_SUBMIT")[0] == 409, "页面：取消后旧卡提交返回409")
            for button in page.locator(".confirm-card button").all():
                expect(button).to_be_disabled()
            check(True, "页面：取消后历史卡所有按钮禁用")
            sid = cancelled["sessionId"]
            following = send(page, "第一笔。")
            with urllib.request.urlopen(PY + "/check/memory/" + sid) as response:
                memory = json.load(response)
            check(not following.get("confirmRequired") and memory["open_task"] is None,
                  "页面：取消后序号不恢复办理，Python待办为空")
            page.screenshot(path=str(OUT / f"{MODE}-取消后旧卡.png"), full_page=True)

            if MODE == "scripted":
                page = open_chat()
                send(page, "ORD9801 我要退款，原因：买错了。")
                card = send(page, "我有点想取消这单退款")
                check([a["code"] for a in card.get("actions", [])] == ["KEEP", "CANCEL_DRAFT"],
                      "页面：冲突卡只有保留和取消，无提交动作")
                assert [a["code"] for a in card.get("actions", [])] == ["KEEP", "CANCEL_DRAFT"], "前置冲突卡不存在，禁止发送提交测试"
                check(resume(card, "CONFIRM_SUBMIT")[0] == 400, "页面：冲突凭证不能伪装成提交")
                page.screenshot(path=str(OUT / "scripted-冲突卡.png"), full_page=True)
                kept = click(page, "保留当前办理")
                snapshot = state(card["sessionId"])["session"]
                check(snapshot["pending"] == "CONFIRM_SUBMIT" and not snapshot["handled"], "页面：保留不取消草稿")
                check(resume(card, "KEEP")[0] == 403, "页面：保留后重复点击旧冲突卡被拒绝")
                available = page.get_by_role("button", name="确认提交申请", exact=True).all()
                check(kept.get("confirmRequired") and any(b.is_enabled() for b in available),
                      "页面：保留后可继续使用当前申请确认卡")
                check(kept.get("resumeToken") != card["resumeToken"] and kept.get("resumeToken") == snapshot.get("token"),
                      "页面：保留后新凭证与草稿一致，旧冲突凭证不复用")
                page.screenshot(path=str(OUT / "scripted-保留后.png"), full_page=True)

                page = open_chat()
                old = send(page, "ORD9801 我要退款，原因：买错了。")
                card = send(page, "我有点想取消这单退款")
                cancelled = click(page, "取消这次尚未提交的售后申请")
                check(cancelled.get("pendingAction") == "CANCELLED" and state(card["sessionId"])["session"].get("token") is None,
                      "页面：冲突卡取消后草稿终止、凭证清空")
                check(resume(card, "CANCEL_DRAFT")[0] == 409, "页面：冲突卡取消后重复请求返回409")
                check(resume(old, "CONFIRM_SUBMIT")[0] == 409, "页面：冲突取消后原提交卡不可用")
                page.screenshot(path=str(OUT / "scripted-冲突取消后.png"), full_page=True)

                page = open_chat()
                send(page, "ORD9801 我要退款，原因：买错了。")
                card = send(page, "我有点想取消这单退款")
                before_revision = state(card["sessionId"])["session"]["revision"]
                changed = send(page, "原因：重复购买")
                snapshot = state(card["sessionId"])["session"]
                check(changed.get("confirmRequired") and snapshot["revision"] > before_revision,
                      "页面：修改原因递增草稿版本并重发提交卡")
                expect(page.get_by_role("button", name="取消这次尚未提交的售后申请", exact=True)).to_be_disabled()
                check(resume(card, "CANCEL_DRAFT")[0] in (403,409) and state(card["sessionId"])["session"]["pending"] != "CANCELLED",
                      "页面：草稿变化后旧冲突卡禁用且后端拒绝")

                page = open_chat()
                selecting = send(page, "帮我退款。")
                check(not selecting.get("confirmRequired"), "页面：选单阶段展示候选但不显示提交卡")
                send(page, "取消订单吧")
                ended = send(page, "结束这次")
                follow = send(page, "第一笔。")
                check(ended.get("pendingAction") == "CANCELLED" and not follow.get("confirmRequired")
                      and follow.get("pendingAction") not in ("REFUND_ORDER_REQUIRED","REFUND_REASON_REQUIRED"),
                      "页面：取消选单后旧候选不复活")
                page.screenshot(path=str(OUT / "scripted-选单取消后.png"), full_page=True)
                for start, stage in [("帮我退款。", "REFUND_ORDER_REQUIRED"), ("ORD9801 我要退款。", "REFUND_REASON_REQUIRED")]:
                    page = open_chat()
                    send(page, start)
                    conflict = send(page, "我有点想取消这单退款")
                    kept = click(page, "保留当前办理")
                    check(state(conflict["sessionId"])["session"]["pending"] == stage and not kept.get("confirmRequired")
                          and page.get_by_role("button", name="确认提交申请", exact=True).count() == 0,
                          "页面：保留" + stage + "仍需补信息，不能提交")
                page = open_chat()
                returning = send(page, "ORD9803 我要申请七天无理由退货。")
                assert returning.get("confirmRequired"), returning
                conflict = send(page, "我有点想取消这单退款")
                kept = click(page, "保留当前办理")
                check(kept.get("confirmRequired") and kept.get("confirmationTitle") == "确认提交退货退款申请"
                      and "商家实际收到且验收通过后才退款" in kept.get("confirmationSummary", ""),
                      "页面：退货退款保留后重发正确类型确认卡")
                ended = click(page, "取消本次申请")
                check(ended.get("pendingAction") == "CANCELLED", "页面：退货退款保留后的新卡也可取消")
            check(not errors, "页面：无JavaScript异常", errors=errors)
        finally:
            for context in contexts:
                context.close()
            browser.close()


def main():
    assert call("/api/auth/login", {"username":"zhangsan", "password":"123456"})[0] == 200
    before = state()["tables"]
    save(f"business-before-{MODE}.json", before)
    failure = None
    try:
        if MODE == "real":
            cases()
        browser_tests()
    except Exception as exc:
        failure = type(exc).__name__ + ": " + str(exc)
        check(False, "验收执行中断", error=failure)
    finally:
        after = state()["tables"]
        save(f"business-after-{MODE}.json", after)
        check(before == after, "八张业务表行数与内容哈希完全不变")
        summary = {"passed":all(r["passed"] for r in RESULTS), "mode":MODE, "score_min":0.9,
                   "checks":len(RESULTS), "failed":[r["check"] for r in RESULTS if not r["passed"]],
                   "model_calls":len((OUT / f"models-{MODE}.jsonl").read_text().splitlines()) if (OUT / f"models-{MODE}.jsonl").exists() else 0,
                   "error":failure}
        save(f"result-{MODE}.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    sys.exit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
