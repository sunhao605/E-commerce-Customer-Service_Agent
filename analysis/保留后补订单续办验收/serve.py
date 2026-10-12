"""隔离替身故意把补订单号判成停止，验证续办不依赖模型碰巧判对。"""
import importlib.util
from pathlib import Path
import re
import uvicorn

spec = importlib.util.spec_from_file_location("shared", Path(__file__).resolve().parent.parent / "取消闭环验收/serve.py")
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
original = shared.cancel.call_jev


def jev(state):
    result = original(state)
    if shared.MODE == "scripted" and (state["latest_user_message"] == "我想了想，还是不退了"
            or re.fullmatch(r"ORD\d{4,}", state["latest_user_message"])):
        result["answers"]["decision"] = {"choice": "stop", "confidence": 0.3}
        result["answers"]["target"] = {"choice": "current", "confidence": 0.51}
        shared.record({"kind": "forced_jev", "message": state["latest_user_message"], "response": result})
    return result


shared.cancel.call_jev = jev
uvicorn.run(shared.app, host="127.0.0.1", port=18101, log_level="warning")
