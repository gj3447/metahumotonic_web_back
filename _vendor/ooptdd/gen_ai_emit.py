"""GenAI 이벤트 emit 헬퍼 — cid ≡ W3C trace_id 통일.

legion / APT / jaebaeman 등 에이전트 루프가 OTel GenAI semconv(``gen_ai.*``) 어휘로 구조화
이벤트를 내보낼 때 쓰는 빌더. 핵심: **cid 를 W3C trace_id 와 동일하게** 묶어
(``correlation_keys(trace_id)``) 전 생태계(legion 한 트레이스, ooptdd verify, harness_console
Verify 큐)가 *한 트레이스 공간*을 공유하게 한다 → cross-tool positive-arrival 검증이 가능.

gen_ai.* 는 실험적이라 :data:`~ooptdd.domain.semconv.SEMCONV_VERSION` 으로 version-pin;
안정 축은 OTLP(write) + W3C trace context(``trace_id``/``span_id``)다.

빌더가 낸 이벤트는 :func:`~ooptdd.domain.semconv.gen_ai_ontology` 의 필수 attr 계약을 충족한다
(invoke_agent→``gen_ai.agent.id``, execute_tool→``gen_ai.tool.name``).

# KG: gen-ai-emit-cid-trace-unify-2026-06-27 (OTel GenAI semconv Tier-2 #8 위에 cid 통일)
"""
from __future__ import annotations

from .domain.model import correlation_keys, with_trace_context
from .domain.semconv import GEN_AI_OPERATIONS


def _gen_ai_event(operation: str, trace_id: str, span_id: str | None, attrs: dict) -> dict:
    """``gen_ai.<op>`` 이벤트 + W3C trace context + cid≡trace_id 통일."""
    if operation not in GEN_AI_OPERATIONS:
        raise ValueError(
            f"unknown gen_ai operation {operation!r}; one of {GEN_AI_OPERATIONS}"
        )
    rec = {"event": f"gen_ai.{operation}", **attrs}
    rec = with_trace_context(rec, trace_id, span_id)  # trace_id (+span_id)
    rec.update(correlation_keys(trace_id))  # cid ≡ correlation_id ≡ cycle_id ≡ trace_id
    return rec


def invoke_agent_event(
    *,
    trace_id: str,
    agent_id: str,
    agent_name: str | None = None,
    provider: str | None = None,
    request_model: str | None = None,
    span_id: str | None = None,
    **extra,
) -> dict:
    """``gen_ai.invoke_agent`` 이벤트 (legion stage / jaebaeman subagent 출격 시)."""
    attrs: dict = {"gen_ai.agent.id": agent_id}
    if agent_name is not None:
        attrs["gen_ai.agent.name"] = agent_name
    if provider is not None:
        attrs["gen_ai.provider.name"] = provider
    if request_model is not None:
        attrs["gen_ai.request.model"] = request_model
    attrs.update(extra)
    return _gen_ai_event("invoke_agent", trace_id, span_id, attrs)


def execute_tool_event(
    *,
    trace_id: str,
    tool_name: str,
    tool_call_id: str | None = None,
    span_id: str | None = None,
    **extra,
) -> dict:
    """``gen_ai.execute_tool`` 이벤트 (legion/APT 의 도구 호출 시)."""
    attrs: dict = {"gen_ai.tool.name": tool_name}
    if tool_call_id is not None:
        attrs["gen_ai.tool.call.id"] = tool_call_id
    attrs.update(extra)
    return _gen_ai_event("execute_tool", trace_id, span_id, attrs)


__all__ = ["execute_tool_event", "invoke_agent_event"]
