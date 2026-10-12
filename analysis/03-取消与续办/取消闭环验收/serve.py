"""复用正式入口；只在隔离验收进程记录模型原始结果或使用固定模型回复。"""
import json
import os
from pathlib import Path

import uvicorn
from agent import cancellation as cancel
from agent.context import SESSION_MEMORIES
from api.routes import app

OUT = Path(os.environ["CANCEL_CHECK_OUT"])
MODE = os.environ["CANCEL_CHECK_MODE"]
real_jev, real_review = cancel.call_jev, cancel.call_review


def record(row):
    with (OUT / f"models-{MODE}.jsonl").open("a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def jev(state):
    message = state["latest_user_message"]
    if MODE == "real":
        raw = real_jev(state)
    else:
        decision, target, extra, score = "continue", "current", "no", 0.99
        if message == "我想了想，还是不退了":
            decision, score = "stop", 0.41
        elif message == "我有点想取消这单退款":
            decision, score = "stop", 0.5
        elif message == "取消订单吧":
            decision, target = "stop", "ambiguous"
        elif message == "请结束当前这份尚未提交的退款申请。":
            decision = "stop"
        raw = {"model":"scripted-jev", "answers":{
            key: {"choice": choice, "confidence": confidence} for key, choice, confidence in (
                ("decision", decision, score), ("target", target, 0.99), ("extra_task", extra, 0.99))}}
    record({"kind":"jev", "message":message, "state":state, "response":raw})
    return raw


def review(state):
    if MODE == "real":
        raw = real_review(state)
    else:
        raw = {"decision":"continue" if state["latest_user_message"] == "我有点想取消这单退款" else "stop",
               "target":"current", "extra_task":"no", "reason":"none"}
    record({"kind":"review", "message":state["latest_user_message"], "state":state, "response":raw})
    return raw


cancel.call_jev, cancel.call_review = jev, review


@app.get("/check/memory/{session_id}")
def memory(session_id: str):
    return {"open_task": (SESSION_MEMORIES.get(session_id) or {}).get("open_task")}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18101, log_level="warning")
