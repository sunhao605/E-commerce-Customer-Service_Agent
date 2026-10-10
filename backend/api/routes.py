"""FastAPI 路由层，只负责接入 Agent、Resume、Trace、Eval 和 Feedback。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from agent.customer_service_agent import Lesson41Agent
from agent.workflows.resume import resolve_trusted_reviewer
from api.schemas import *
from settings import CASES_PATH, load_agent_capabilities
from evals.runner import EvalRunner
from evals.feedback import FailureAttributor, build_backfilled_case
from agent.trace import trace_store
from agent.state import BACKFILLED_CASES, FEEDBACK_RECORDS

agent = Lesson41Agent()
eval_runner = EvalRunner(agent, CASES_PATH)
eval_runner.backfilled_cases = BACKFILLED_CASES
failure_attributor = FailureAttributor()
app = FastAPI(title="荒野户外售后服务助手")
_local_origin = r"https?://(localhost|127\.0\.0\.1|\[::1\]|host\.docker\.internal|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?$"
_extra_origins = [item.strip() for item in os.getenv("AGENT_WORKBENCH_ORIGINS", "").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_extra_origins,
    allow_origin_regex=_local_origin,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """提供课程快照健康检查。"""
    return {"status": "ok", "lesson": "41"}


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    """返回当前课程能力清单。"""
    return load_agent_capabilities()


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """处理一次总演习聊天请求。"""
    return agent.chat(request)


@app.post("/chat/resume", response_model=ChatResumeResponse)
def chat_resume(request: ChatResumeRequest, http_request: Request) -> ChatResumeResponse:
    """恢复暂停的售后 workflow。审核身份来自商城管理员会话，不采用请求体里的角色。"""
    reviewer = resolve_trusted_reviewer(http_request.headers.get("cookie"))
    return agent.resume(request, reviewer)


@app.get("/sessions/{session_id}/trace", response_model=list[TraceEvent])
def session_trace(session_id: str) -> list[TraceEvent]:
    """返回指定会话的公开 Trace。"""
    return trace_store.list(session_id)


@app.post("/eval/run", response_model=EvalRunResponse)
def run_eval(request: EvalRunRequest) -> EvalRunResponse:
    """运行第 41 课大促总演习回归评测。"""
    return eval_runner.run(case_id=request.case_id)


@app.post("/feedback/submit", response_model=FeedbackSubmitResponse)
def submit_feedback(request: FeedbackRequest) -> FeedbackSubmitResponse:
    """提交反馈、生成归因并回填成临时回归 case。"""
    eval_report = eval_runner.run(case_id=request.case_id) if request.case_id else None
    eval_result = eval_report.results[0] if eval_report and eval_report.results else None
    events = trace_store.list(request.session_id)
    attributions = failure_attributor.attribute(feedback=request, trace_events=events, eval_result=eval_result)
    base_case = next((case for case in eval_runner.load_cases() if case["case_id"] == request.case_id), None)
    backfilled_case = build_backfilled_case(request, attributions, base_case)
    BACKFILLED_CASES.append(backfilled_case)
    record = FeedbackRecord(
        feedback_id=f"fb-{len(FEEDBACK_RECORDS) + 1:03d}",
        session_id=request.session_id,
        case_id=request.case_id,
        rating=request.rating,
        user_comment=request.user_comment,
        trace_event_names=[event.event_type for event in events],
        eval_failure_categories=eval_result.failure_categories if eval_result else [],
        attributions=attributions,
        backfilled_case=backfilled_case,
    )
    FEEDBACK_RECORDS.append(record)
    return FeedbackSubmitResponse(record=record, eval_report=eval_report)
