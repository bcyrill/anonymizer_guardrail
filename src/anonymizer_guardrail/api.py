"""
Pydantic models for the LiteLLM Generic Guardrail API contract.

Spec: https://docs.litellm.ai/docs/adding_provider/generic_guardrail_api

We accept extra fields liberally and only declare the ones we actively use,
so future LiteLLM additions don't break us.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GuardrailRequest(BaseModel):
    """POST body sent to /beta/litellm_basic_guardrail_api."""

    model_config = ConfigDict(extra="allow")

    texts: list[str] = Field(default_factory=list)
    images: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    structured_messages: list[dict[str, Any]] | None = None

    request_data: dict[str, Any] = Field(default_factory=dict)
    request_headers: dict[str, Any] | None = None
    input_type: Literal["request", "response"] = "request"
    litellm_call_id: str | None = None
    litellm_trace_id: str | None = None
    additional_provider_specific_params: dict[str, Any] = Field(default_factory=dict)


class GuardrailResponse(BaseModel):
    """Response body returned to LiteLLM."""

    action: Literal["BLOCKED", "NONE", "GUARDRAIL_INTERVENED"]
    blocked_reason: str | None = None
    texts: list[str] | None = None
    images: list[str] | None = None
