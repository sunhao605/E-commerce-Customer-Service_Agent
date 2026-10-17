"""隔离验收用 Python 入口：复用正式 app，只在验收进程内采集状态、计量和注入故障。"""
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path

import uvicorn
from fastapi import Request

OUT = Path(os.environ["BC032_OUT"])
MODE = os.environ.get("BC032_MODE", "real")
STATE = {"phase": None, "case": None, "inject": None, "injections": [], "inject_pending": None}

# 先装载环境，再导入 app，确保模型/业务地址按本轮配置生效。
for line in (OUT.parents[2] / ".env.model.local").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
os.environ.update(AGENT_COURSE_OFFLINE_RAG="1", AGENT_COURSE_OFFLINE_FACTS="1",
                  ANSWER_JUDGE_MODE="enforce", ANSWER_JUDGE_TIMEOUT_SECONDS="5",
                  ECOMMERCE_BASE_URL="http://127.0.0.1:18112",
                  AGENT_SERVICE_AUTH_TOKEN="course-debug-agent-service",
                  CANCEL_INTENT_EXECUTE="0",
                  AGENT_COURSE_DISABLE_LLM="1" if MODE == "offline" else "0")

import api.routes as routes
from agent.context import SESSION_MEMORIES, current_memory
from settings import answer_judge_settings, openai_model_name
from guard import Guard

frozen = json.loads((OUT / "frozen.json").read_text())
guard = Guard(model=frozen["config"]["model"], max_calls=frozen["config"]["max_model_http"],
              token_stop=frozen["config"]["stop_before_next_request_at_tokens"],
              case_budgets=frozen["config"]["case_budgets"], log_path=str(OUT / f"models-{MODE}.jsonl"),
              case_getter=lambda: STATE["case"], phase_getter=lambda: STATE["phase"],
              local_prefixes=("http://127.0.0.1:18112",))
STACK = ExitStack()
guard.install(STACK)
agent = routes.agent


def phase(name, call):
    STATE["phase"] = name
    try:
        return call()
    finally:
        STATE["phase"] = None


def consume(stage):
    if STATE["inject"] == stage:
        STATE["inject"] = None
        STATE["injections"].append({"stage": stage, "at": round(time.time(), 3)})
        return True
    return False


real_route = agent.route_model_client.plan_intent
real_tools = agent.langchain_tool_runner.run
real_generate = agent.answer_model_client._invoke_chain
real_judge = agent.answer_judge_client.judge
real_chat = agent.chat


def route(*args, **kwargs):
    if consume("route"):
        raise TimeoutError("bc032_local_injection_route")
    return phase("route", lambda: real_route(*args, **kwargs))


def tools(*args, **kwargs):
    if consume("tool"):
        raise TimeoutError("bc032_local_injection_tool")
    return phase("tool_selection", lambda: real_tools(*args, **kwargs))


def generate(*args, **kwargs):
    if consume("generation"):
        raise TimeoutError("bc032_local_injection_generation")
    return phase("generation", lambda: real_generate(*args, **kwargs))


def judge(*args, **kwargs):
    return phase("judge", lambda: real_judge(*args, **kwargs))


def chat(request):
    row = {"case": STATE["case"], "request": request.model_dump(mode="json")}
    started = time.monotonic()
    try:
        response = real_chat(request)
    except Exception as exc:
        row.update(error=type(exc).__name__, elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        with (OUT / f"python-turns-{MODE}.jsonl").open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        raise
    row.update(response=response.model_dump(mode="json"), elapsed_ms=round((time.monotonic() - started) * 1000, 3))
    with (OUT / f"python-turns-{MODE}.jsonl").open("a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return response


agent.route_model_client.plan_intent = route
agent.langchain_tool_runner.run = tools
agent.answer_model_client._invoke_chain = generate
agent.answer_judge_client.judge = judge
agent.chat = chat

app = routes.app
MEMORY_KEYS = ("open_task", "order_query", "eligibility_query", "product_query", "promotion_query",
               "clarification_focus", "last_order_id")


@app.get("/check/status")
def status():
    return {"case": STATE["case"], "phase": STATE["phase"], "stop": guard.stop,
            "calls": len(guard.items), "tokens": guard.used_tokens(),
            "injections": STATE["injections"], "inject_pending": STATE["inject"],
            "judge": list(answer_judge_settings())[:2], "model": openai_model_name()}


@app.post("/check/case/{case}")
def set_case(case: str):
    STATE["case"] = case
    return {"case": case}


@app.post("/check/inject/{stage}")
def set_inject(stage: str):
    STATE["inject"] = None if stage == "none" else stage
    return {"inject": STATE["inject"]}


@app.get("/check/memory/{session_id}")
def memory(session_id: str):
    return {key: SESSION_MEMORIES.get(session_id, {}).get(key) for key in MEMORY_KEYS}


@app.post("/check/seed/{session_id}")
async def seed(session_id: str, request: Request):
    body = await request.json()
    current = current_memory(session_id, "U1001")
    for key in MEMORY_KEYS:
        current.pop(key, None)
    for key in MEMORY_KEYS:
        if key in body:
            current[key] = body[key]
    return {"session": session_id, **{key: current.get(key) for key in MEMORY_KEYS}}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18101, log_level="warning")
