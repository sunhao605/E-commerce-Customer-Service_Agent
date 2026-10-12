"""实际浏览器验证两个缺信息场景的低分取消出口，并重跑原24项回归。"""
import importlib.util
import json
from pathlib import Path
import urllib.request

from playwright.sync_api import sync_playwright, expect

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("closure_check", OUT.parent / "取消闭环验收/check.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
base.OUT = OUT
original_browser_tests = base.browser_tests


def low_score_scenarios():
    errors = []
    expect.set_options(timeout=20000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        contexts = []
        try:
            def start(message):
                context = browser.new_context(viewport={"width":1440, "height":1000})
                contexts.append(context)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base.SITE, wait_until="networkidle")
                page.get_by_role("button", name="登录商城", exact=True).click()
                page.get_by_role("heading", name="账号登录").wait_for(state="hidden")
                page.get_by_role("button", name="客服", exact=True).click()
                response = send(page, message)
                assert not response.get("confirmRequired"), response
                return page, response["sessionId"]

            def send(page, message):
                page.get_by_placeholder("请输入您的问题").fill(message)
                with page.expect_response(lambda r:"/api/customer-service/chat" in r.url, timeout=130000) as pending:
                    page.get_by_role("button", name="发送", exact=True).click()
                response = pending.value
                assert response.status == 200
                data = response.json()["data"]
                base.TURNS.append({"browser":True, "message":message, "response":data})
                base.save("dialogue-scripted.json", base.TURNS)
                expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
                return data

            def click(page, label):
                with page.expect_response(lambda r:"/api/customer-service/resume" in r.url) as pending:
                    page.get_by_role("button", name=label, exact=True).last.click()
                response = pending.value
                assert response.status == 200
                data = response.json()["data"]
                base.TURNS.append({"browser":True, "click":label, "response":data})
                base.save("dialogue-scripted.json", base.TURNS)
                expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
                return data

            def low(page, sid, scene, stage, label):
                before = base.state(sid)["session"]
                card = send(page, "我想了想，还是不退了")
                judgment = base.trace(sid)[-1]
                state = base.state(sid)["session"]
                assert judgment["action"] == "score_low" and judgment["scores"]["decision"] == 0.41, judgment
                base.check(not judgment["executed"] and state["pending"] == stage and state["revision"] == before["revision"],
                           scene + "：低分不自动取消，办理内容和版本保留")
                actions = [a["code"] for a in card.get("actions", [])]
                exists = card.get("confirmRequired") and actions == ["KEEP", "CANCEL_DRAFT"]
                base.check(exists, scene + "：页面提供保留和取消按钮，不提供提交")
                page.locator(".service-drawer").screenshot(path=str(OUT / (scene + "-低分回复.png")))
                if not exists:
                    return None
                expect(page.get_by_role("button", name=label, exact=True).last).to_be_enabled()
                base.check("取消本次申请" not in card["answer"], scene + "：提示与实际取消按钮对应")
                base.check(base.resume(card, "CONFIRM_SUBMIT")[0] == 400 and base.state(sid)["session"]["pending"] == stage,
                           scene + "：这张选择卡不能提交申请")
                return card

            scenes = [
                ("还没确定退款订单", "帮我退款。", "REFUND_ORDER_REQUIRED", "结束这次未提交的退款办理", "第一笔。", "REFUND_REASON_REQUIRED"),
                ("还没说明退款原因", "ORD9801 我要退款。", "REFUND_REASON_REQUIRED", "取消这次尚未提交的售后申请", "原因：买错了。", "CONFIRM_SUBMIT"),
            ]
            for scene, first, stage, label, followup, next_stage in scenes:
                page, sid = start(first)
                card = low(page, sid, scene, stage, label)
                if card is None:
                    continue
                ended = click(page, label)
                base.check(ended.get("pendingAction") == "CANCELLED" and base.state(sid)["session"].get("token") is None,
                           scene + "：客户点击取消后结束办理并清除凭证")
                base.check(base.resume(card, "CANCEL_DRAFT")[0] == 409, scene + "：取消后旧按钮重复请求被拒绝")
                for button in page.locator(".confirm-card button").all():
                    expect(button).to_be_disabled()
                send(page, "第一笔。")
                with urllib.request.urlopen(base.PY + "/check/memory/" + sid) as response:
                    memory = json.load(response)
                base.check(memory["open_task"] is None and base.state(sid)["session"]["pending"] == "CANCELLED",
                           scene + "：取消后旧候选或待办不恢复")

                page, sid = start(first)
                card = low(page, sid, scene, stage, label)
                kept = click(page, "保留当前办理")
                base.check(base.state(sid)["session"]["pending"] == stage and not kept.get("confirmRequired"),
                           scene + "：客户点保留后仍等待原来的信息")
                base.check(base.resume(card, "KEEP")[0] == 403, scene + "：保留后旧选择卡不能再次操作")
                next_reply = send(page, followup)
                base.check(base.state(sid)["session"]["pending"] == next_stage
                           and bool(next_reply.get("confirmRequired")) == (next_stage == "CONFIRM_SUBMIT"),
                           scene + "：补充原来缺少的信息后可继续正常办理")

                page, sid = start(first)
                card = low(page, sid, scene, stage, label)
                old_revision = base.state(sid)["session"]["revision"]
                send(page, "ORD9801" if stage == "REFUND_ORDER_REQUIRED" else "原因：重复购买")
                base.check(base.state(sid)["session"]["revision"] > old_revision
                           and base.resume(card, "CANCEL_DRAFT")[0] in (403,409)
                           and base.state(sid)["session"]["pending"] != "CANCELLED",
                           scene + "：办理内容变化后旧选择卡不能取消新的办理")
                expect(page.get_by_role("button", name=label, exact=True)).to_be_disabled()
            base.check(not errors, "新增场景：无JavaScript异常", errors=errors)
        finally:
            for context in contexts:
                context.close()
            browser.close()


def browser_tests():
    low_score_scenarios()
    original_browser_tests()


if __name__ == "__main__":
    base.browser_tests = browser_tests
    base.main()
