"""一次只读回复评审；不授权、不改写、不执行工具。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from agent.prompts import load_prompt
from settings import api_key_is_missing, deepseek_extra_body, openai_base_url

DIMENSIONS = ("omission", "conflict", "unperformed")


class AnswerJudgeResult(BaseModel):
    status: str = "skipped"
    decision: str = "not_reviewed"
    checks: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    invocation_attempted: bool = False
    model_name: str | None = None
    response_model: str | None = None
    prompt_version: str | None = None
    input_sha256: str | None = None
    elapsed_ms: float = 0
    usage: dict[str, int] = Field(default_factory=dict)


def parse_judgment(content: str, context: dict[str, Any], candidate: str) -> dict[str, Any]:
    data = json.loads(content)
    if not isinstance(data, dict) or set(data) != set(DIMENSIONS):
        raise ValueError("invalid_judge_dimensions")
    valid_ids = {item["id"] for item in context["evidence"]}
    for dimension, check in data.items():
        if not isinstance(check, dict) or set(check) != {"verdict", "quote", "requirement", "evidence_ids", "reason"}:
            raise ValueError("invalid_judge_fields")
        if check["verdict"] not in {"pass", "fail", "uncertain"}:
            raise ValueError("invalid_judge_verdict")
        for key in ("quote", "requirement", "reason"):
            if not isinstance(check[key], str) or len(check[key]) > 1000:
                raise ValueError("invalid_judge_text")
        refs = check["evidence_ids"]
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in valid_ids for ref in refs):
            raise ValueError("invalid_judge_evidence")
        if not check["reason"].strip() or (check["quote"] and check["quote"] not in candidate):
            raise ValueError("invalid_judge_explanation")
        if check["verdict"] == "fail":
            if dimension == "omission":
                if not check["requirement"].strip() or "request" not in refs:
                    raise ValueError("missing_omission_requirement")
            elif not check["quote"] or not any(ref not in {"request", "history"} for ref in refs):
                raise ValueError("missing_conflict_evidence")
        if check["verdict"] == "uncertain" and not check["requirement"].strip():
            raise ValueError("missing_uncertainty_gap")
    return data


class AnswerJudgeClient:
    def judge(self, context: dict[str, Any], candidate: str, *, model: str, timeout: float) -> AnswerJudgeResult:
        started = time.monotonic()
        result = AnswerJudgeResult(status="error", decision="fallback", model_name=model)
        try:
            prompt = load_prompt("answer_judge")
            result.prompt_version = hashlib.sha256(prompt.encode()).hexdigest()
            payload = json.dumps({"context": context, "candidate_reply": candidate}, ensure_ascii=False, sort_keys=True)
            result.input_sha256 = hashlib.sha256(payload.encode()).hexdigest()
            if os.getenv("AGENT_COURSE_DISABLE_LLM") == "1" or api_key_is_missing(os.getenv("AGENT_OPENAI_API_KEY")):
                result.reason = "model_config_missing"
                return result
            body = {"model": model, "temperature": 0, "max_tokens": 1536, "stream": False,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": payload}]}
            body.update(deepseek_extra_body() or {})
            result.invocation_attempted = True
            response = asyncio.run(self._bounded_request(body, timeout))
            result.response_model = response.get("model")
            result.usage = {key: value for key, value in (response.get("usage") or {}).items()
                            if type(value) is int and value >= 0 and key in {
                                "prompt_tokens", "completion_tokens", "total_tokens",
                                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}}
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("incomplete_judge_output")
            result.checks = parse_judgment(choice["message"]["content"], context, candidate)
            verdicts = [check["verdict"] for check in result.checks.values()]
            result.decision = "fallback" if "fail" in verdicts else "review" if "uncertain" in verdicts else "allow"
            result.status = "success"
        except Exception as exc:
            # 不记录异常正文，HTTP错误中可能含供应商响应或敏感输入。
            result.reason = "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else type(exc).__name__
        finally:
            result.elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        return result

    async def _bounded_request(self, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        # wait_for 限制整次调用，而非仅限制每个网络读操作；取消后关闭连接，不后台重试。
        return await asyncio.wait_for(self._request(body, timeout), timeout=timeout)

    async def _request(self, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(f"{openai_base_url()}/chat/completions", json=body,
                headers={"Authorization": f"Bearer {os.getenv('AGENT_OPENAI_API_KEY')}"})
            response.raise_for_status()
            return response.json()
