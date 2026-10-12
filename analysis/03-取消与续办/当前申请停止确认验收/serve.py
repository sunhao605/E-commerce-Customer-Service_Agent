"""只在隔离进程固定取消对象分0.85，复用正式API及原验收模型替身。"""
import importlib.util
from pathlib import Path
import uvicorn

spec = importlib.util.spec_from_file_location("shared", Path(__file__).resolve().parent.parent / "取消闭环验收/serve.py")
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
original = shared.cancel.call_jev


def jev(state):
    result = original(state)
    if state["latest_user_message"] == "我想了想，还是不退了":
        result["answers"]["target"]["confidence"] = 0.85
    return result


shared.cancel.call_jev = jev
uvicorn.run(shared.app, host="127.0.0.1", port=18101, log_level="warning")
