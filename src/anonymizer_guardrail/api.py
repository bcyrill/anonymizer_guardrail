"""
Pydantic models for the LiteLLM Generic Guardrail API contract.

Spec: https://docs.litellm.ai/docs/adding_provider/generic_guardrail_api

We accept extra fields liberally and only declare the ones we actively use,
so future LiteLLM additions don't break us.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("anonymizer.api")


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


# ── Per-request overrides ──────────────────────────────────────────────────
# Operators can set these on a per-call basis via LiteLLM's
# `additional_provider_specific_params` field (or per-request via
# `extra_body` on the client call). Each field is None by default,
# meaning "use the configured server-side value". Unknown keys / bad
# types log a warning and are ignored — we don't BLOCK over a single
# bad override since the rest of the request is still anonymizable.

@dataclass(frozen=True)
class Overrides:
    use_faker: bool | None = None
    faker_locale: tuple[str, ...] | None = None
    detector_mode: tuple[str, ...] | None = None
    regex_overlap_strategy: str | None = None
    regex_patterns: str | None = None
    llm_model: str | None = None
    llm_prompt: str | None = None

    @classmethod
    def empty(cls) -> Overrides:
        return cls()


_VALID_OVERLAP_STRATEGIES = frozenset({"longest", "priority"})

# Defensive caps on parsed override values. Three detectors exist
# (regex, privacy_filter, llm) so a list longer than that is malformed.
# Three locales covers primary + a couple of fallbacks, which is what
# realistic Faker chains use; anything beyond is almost certainly
# abuse and inflates per-request Faker construction time.
_MAX_LOCALE_CHAIN = 3
_MAX_DETECTOR_LIST = 3


def _parse_locale(value: Any) -> tuple[str, ...] | None:
    """Accept either ``"pt_BR,en_US"`` (string) or ``["pt_BR", "en_US"]``
    (array) and normalize to a tuple. Empty input → None."""
    if isinstance(value, str):
        parts = [s.strip() for s in value.split(",") if s.strip()]
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if not isinstance(item, str):
                raise TypeError(f"locale list item must be string, got {type(item).__name__}")
            s = item.strip()
            if s:
                parts.append(s)
    else:
        raise TypeError(f"expected string or list, got {type(value).__name__}")
    if len(parts) > _MAX_LOCALE_CHAIN:
        raise ValueError(
            f"locale chain length {len(parts)} exceeds cap of {_MAX_LOCALE_CHAIN}"
        )
    return tuple(parts) if parts else None


def _parse_detector_mode(value: Any) -> tuple[str, ...] | None:
    """Accept comma-separated string or list of strings, normalize to tuple."""
    if isinstance(value, str):
        parts = [s.strip() for s in value.split(",") if s.strip()]
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if not isinstance(item, str):
                raise TypeError(f"detector_mode list item must be string, got {type(item).__name__}")
            s = item.strip()
            if s:
                parts.append(s)
    else:
        raise TypeError(f"expected string or list, got {type(value).__name__}")
    if len(parts) > _MAX_DETECTOR_LIST:
        raise ValueError(
            f"detector_mode list length {len(parts)} exceeds cap of {_MAX_DETECTOR_LIST}"
        )
    return tuple(parts) if parts else None


def parse_overrides(raw: dict[str, Any] | None) -> Overrides:
    """Build an Overrides from ``additional_provider_specific_params``.

    Validation policy: each unknown / malformed field logs a warning and
    is dropped. Other fields in the same dict still take effect. The
    request itself is never blocked — operators get a logged hint and
    config defaults take over.
    """
    if not raw:
        return Overrides.empty()

    use_faker: bool | None = None
    faker_locale: tuple[str, ...] | None = None
    detector_mode: tuple[str, ...] | None = None
    regex_overlap_strategy: str | None = None
    regex_patterns: str | None = None
    llm_model: str | None = None
    llm_prompt: str | None = None

    for key, value in raw.items():
        try:
            if key == "use_faker":
                if not isinstance(value, bool):
                    raise TypeError(f"expected bool, got {type(value).__name__}")
                use_faker = value
            elif key == "faker_locale":
                faker_locale = _parse_locale(value)
            elif key == "detector_mode":
                detector_mode = _parse_detector_mode(value)
            elif key == "regex_overlap_strategy":
                if not isinstance(value, str):
                    raise TypeError(f"expected string, got {type(value).__name__}")
                norm = value.strip().lower()
                if norm not in _VALID_OVERLAP_STRATEGIES:
                    raise ValueError(
                        f"unknown strategy {value!r}; allowed: "
                        f"{', '.join(sorted(_VALID_OVERLAP_STRATEGIES))}"
                    )
                regex_overlap_strategy = norm
            elif key == "llm_model":
                if not isinstance(value, str):
                    raise TypeError(f"expected string, got {type(value).__name__}")
                stripped = value.strip()
                if stripped:
                    llm_model = stripped
            elif key in ("regex_patterns", "llm_prompt"):
                # Both name a registered alternative (REGEX_PATTERNS_REGISTRY
                # / LLM_SYSTEM_PROMPT_REGISTRY). The detector resolves the
                # name against its registry at detect() time; an unknown
                # name there logs a warning and falls back to the default.
                # Path traversal is impossible because we never accept a
                # path here — only a name.
                if not isinstance(value, str):
                    raise TypeError(f"expected string, got {type(value).__name__}")
                stripped = value.strip()
                if stripped:
                    if key == "regex_patterns":
                        regex_patterns = stripped
                    else:
                        llm_prompt = stripped
            else:
                # Unknown key — silently ignore. Other guardrails sharing
                # the same dict don't apply here (LiteLLM scopes
                # additional_provider_specific_params per guardrail), but
                # we still tolerate forward-compat / typo-tolerance.
                log.debug("ignoring unknown override key %r", key)
        except (TypeError, ValueError) as exc:
            log.warning(
                "ignoring invalid override %s=%r: %s",
                key, value, exc,
            )

    return Overrides(
        use_faker=use_faker,
        faker_locale=faker_locale,
        detector_mode=detector_mode,
        regex_overlap_strategy=regex_overlap_strategy,
        regex_patterns=regex_patterns,
        llm_model=llm_model,
        llm_prompt=llm_prompt,
    )
