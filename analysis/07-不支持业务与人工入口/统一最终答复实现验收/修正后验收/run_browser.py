"""两条冻结页面咨询；独立上下文，不点击业务确认，不保存登录凭据。"""
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent
cases = [c for c in json.loads((OUT / "frozen-live-cases.json").read_text())["cases"] if c["surface"] == "browser"]
assert len(cases) == 2
assert all(not (OUT / (c["id"] + ".json")).exists() for c in cases), "不覆盖或重跑已有结果"

with sync_playwright() as pw, httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False) as agent:
    browser = pw.chromium.launch(headless=True, executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    sessions = set()
    try:
        for case in cases:
            context = browser.new_context(viewport={"width": 1440, "height": 1050})
            try:
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://127.0.0.1:5174/")
                page.wait_for_load_state("networkidle")
                page.get_by_label("账号", exact=True).fill("zhangsan")
                page.get_by_label("密码", exact=True).fill("123456")
                page.get_by_role("button", name="登录商城", exact=True).click()
                page.wait_for_url("http://127.0.0.1:5174/")
                page.wait_for_load_state("networkidle")
                page.get_by_role("button", name="客服", exact=True).click()
                field = page.get_by_placeholder("请输入您的问题", exact=True)
                field.fill(case["message"])
                start = time.perf_counter()
                row = {"case": case, "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                       "initial_state": "fresh isolated browser context; no prior chat; Java creates session on first message"}
                with page.expect_response(lambda r: "/api/customer-service/chat" in r.url and r.request.method == "POST", timeout=60000) as response_info:
                    page.get_by_role("button", name="发送", exact=True).click()
                response = response_info.value
                row.update(status_code=response.status, request=response.request.post_data_json, response=response.json(),
                           elapsed_ms=round((time.perf_counter() - start) * 1000, 1))
                path = OUT / (case["id"] + ".json")
                path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
                assert response.status == 200 and row["response"]["success"]
                data = row["response"]["data"]
                assert not row["request"].get("sessionId")
                session = data["sessionId"]
                assert session not in sessions
                sessions.add(session)
                trace = agent.get(f"/sessions/{session}/trace")
                trace.raise_for_status()
                row["trace"] = trace.json()
                built = next(e["payload"] for e in row["trace"] if e["event_type"] == "context_built")
                assert built["history_kept_count"] == 0
                text = page.get_by_text(data["answer"], exact=True)
                text.wait_for(state="visible", timeout=15000)
                text.scroll_into_view_if_needed()
                screenshot = "截图/" + case["id"] + ".png"
                page.screenshot(path=str(OUT / screenshot), full_page=True)
                row.update(screenshot=screenshot, rendered_answer=text.inner_text(), page_errors=errors,
                           rendered_answer_visible=True)
                path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps({"id": case["id"], "session_id": session, "answer": data["answer"], "page_errors": errors}, ensure_ascii=False), flush=True)
            finally:
                context.close()
        assert len(sessions) == 2
    finally:
        browser.close()
