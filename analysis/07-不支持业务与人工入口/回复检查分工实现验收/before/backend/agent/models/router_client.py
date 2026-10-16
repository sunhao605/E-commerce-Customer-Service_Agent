"""第 41 课：真实模型路由客户端。"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, get_args

from pydantic import BaseModel, Field

from api.schemas import Intent
from settings import api_key_is_missing, deepseek_extra_body, load_course_env, openai_base_url, openai_model_name
from agent.prompts import PromptManager, prompt_manager


class ModelRouteResult(BaseModel):
    """模型路由结果。不可用或输出不可信时回退到课程规则路由。"""

    intent: Intent
    used_model: bool = False
    model_name: str | None = None
    fallback_reason: str | None = None
    framework: str | None = None
    prompt_fragments: list[dict[str, Any]] = Field(default_factory=list)
    needs_product_facts: bool | None = None
    knowledge_domains: list[str] | None = None
    clarification_fields: list[str] = Field(default_factory=list)
    capability_ids: list[str] | None = None
    invocation_attempted: bool = False
    elapsed_ms: float | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class RouteModelClient:
    """用真实大模型判断本轮应该进入哪条受控路径。"""

    def __init__(self, manager: PromptManager = prompt_manager) -> None:
        self.prompt_manager = manager

    def can_call_model(self) -> bool:
        """检查当前课程环境是否能真实调用聊天模型。"""
        load_course_env()
        if os.getenv("AGENT_COURSE_DISABLE_LLM") == "1":
            return False
        if api_key_is_missing(os.getenv("AGENT_OPENAI_API_KEY")):
            return False
        try:
            self._chat_model_class()
        except ImportError:
            return False
        return True

    def plan_intent(self, user_message: str, *, fallback_intent: Intent, task_note: str | None = None) -> ModelRouteResult:
        """优先用真实模型判断本轮意图，失败时回退到规则分类。有待办时把短状态附在系统提示后。"""
        if not self.can_call_model():
            return ModelRouteResult(intent=fallback_intent, fallback_reason="model_config_missing")
        from langchain_core.callbacks import get_usage_metadata_callback

        started = time.perf_counter()
        attempted = False
        usage: dict[str, int] = {}
        try:
            model = self._create_chat_model(temperature=0)
            fragments = self.prompt_manager.select_fragments({"phase": "route"})
            system_prompt = self.prompt_manager.render_system_prompt(fragments)
            if task_note:
                system_prompt = f"{system_prompt}\n\n{task_note}"
            attempted = True
            with get_usage_metadata_callback() as callback:
                content = self._invoke_chain(model, user_message, system_prompt)
            if callback.usage_metadata:
                usage = {key: sum(value.get(key, 0) for value in callback.usage_metadata.values()) for key in ("input_tokens", "output_tokens", "total_tokens")}
            prompt_fragments = self.prompt_manager.selection_summary(fragments, phase="route")
            decision = self._extract_decision(content)
            if decision is None:
                return ModelRouteResult(
                    intent=fallback_intent,
                    model_name=openai_model_name(),
                    fallback_reason="invalid_model_route",
                    prompt_fragments=prompt_fragments,
                    invocation_attempted=attempted,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 3), usage=usage,
                )
            return ModelRouteResult(
                **decision,
                used_model=True,
                model_name=openai_model_name(),
                framework="langchain_runnable_sequence",
                prompt_fragments=prompt_fragments,
                invocation_attempted=attempted,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3), usage=usage,
            )
        except Exception as exc:
            return ModelRouteResult(
                intent=fallback_intent,
                model_name=openai_model_name(),
                fallback_reason=exc.__class__.__name__,
                invocation_attempted=attempted,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3), usage=usage,
            )

    @staticmethod
    def _extract_decision(content: str) -> dict[str, Any] | None:
        allowed = set(get_args(Intent))
        text = content.strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if match is None:
                return None
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict) or not isinstance(payload.get("intent"), str) or payload["intent"] not in allowed:
            return None
        intent = payload["intent"]
        fields = {"intent"}
        if "capability_ids" in payload:
            ids = payload["capability_ids"]
            if not isinstance(ids, list) or len(ids) > 8 or any(
                not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item) for item in ids
            ) or len(ids) != len(set(ids)):
                return None
            fields.add("capability_ids")
        elif intent == "service_scope_query":
            return None
        if intent in {"product_query", "promotion_query"}:
            fields.add("clarification_fields")
            clarification = payload.get("clarification_fields")
            if not isinstance(clarification, list) or any(item != "promotion_combination" for item in clarification):
                return None
        if intent == "product_query":
            fields.update({"needs_product_facts", "knowledge_domains"})
            if "needs_product_facts" not in payload or (payload["needs_product_facts"] is not None and type(payload["needs_product_facts"]) is not bool):
                return None
            domains = payload.get("knowledge_domains")
            if not isinstance(domains, list) or any(item not in ("product_knowledge", "promotion_and_member_policy") for item in domains):
                return None
            if "promotion_combination" in payload["clarification_fields"] and "promotion_and_member_policy" not in domains:
                return None
            if payload["needs_product_facts"] is False and not domains:
                return None
        return payload if set(payload) == fields else None

    @staticmethod
    def _extract_intent(content: str) -> Intent | None:
        decision = RouteModelClient._extract_decision(content)
        return decision["intent"] if decision else None

    @staticmethod
    def _chat_model_class() -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI

    def _create_chat_model(self, *, temperature: float) -> Any:
        load_course_env()
        chat_model_class = self._chat_model_class()
        return chat_model_class(
            model=openai_model_name(),
            api_key=os.getenv("AGENT_OPENAI_API_KEY"),
            base_url=openai_base_url(),
            temperature=temperature,
            extra_body=deepseek_extra_body(),
            max_tokens=256,
            timeout=30,
            max_retries=0,
        )

    @staticmethod
    def _invoke_chain(model: Any, user_message: str, system_prompt: str) -> str:
        """用 LangChain RunnableSequence 执行可替换 Prompt、模型和输出解析。"""
        from langchain_core.messages import SystemMessage
        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=system_prompt),
                ("human", "{user_message}"),
            ]
        )
        chain = prompt | model | StrOutputParser()
        return str(chain.invoke({"user_message": user_message}))
