"""隔离验收里的真实判断服务。额外记下取消后 Python 待办是否还在。"""
import json
import os
from pathlib import Path

import uvicorn
from starlette.requests import Request
from starlette.responses import Response

from agent.context import SESSION_MEMORIES
from api.routes import app

OUT = Path(os.getenv("CANCEL_MEMORY_LOG", str(Path(__file__).resolve().parent / "python-memory.jsonl")))


@app.middleware("http")
async def record_memory(request: Request, call_next):
    body = await request.body()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(request.scope, receive)
    response = await call_next(request)
    raw = b""
    async for chunk in response.body_iterator:
        raw += chunk
    response = Response(content=raw, status_code=response.status_code, media_type=response.media_type or "application/json")
    if request.url.path in {"/chat", "/cancellation/judge"} and body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        session_id = payload.get("session_id")
        runtime = payload.get("runtime_context") or {}
        memory = SESSION_MEMORIES.get(session_id) or {}
        task = memory.get("open_task")
        judge = {}
        if request.url.path == "/cancellation/judge" and raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            judge = {key: parsed.get(key) for key in ("action", "executed", "reason", "components", "scores", "reply")}
        error = raw[:500].decode("utf-8", "replace") if response.status_code >= 400 or request.url.path == "/chat" else ""
        with OUT.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "path": request.url.path,
                "session_id": session_id,
                "message": runtime.get("userMessage") or payload.get("user_message"),
                "cancelledAfterSaleDraft": runtime.get("cancelledAfterSaleDraft"),
                "open_task": None if not task else {"intent": task.get("intent"), "candidates": task.get("candidates")},
                "status": response.status_code,
                "judge": judge,
                "error": error,
            }, ensure_ascii=False) + "\n")
    return response


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("CANCEL_PY_PORT", "18014")), log_level="warning")
