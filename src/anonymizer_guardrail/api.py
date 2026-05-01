"""
Pydantic models for the LiteLLM Generic Guardrail API contract.

Spec: https://docs.litellm.ai/docs/adding_provider/generic_guardrail_api

We accept extra fields liberally and only declare the ones we actively use,
so future LiteLLM additions don't break us.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from .detector import REGISTERED_SPECS

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

_VALID_OVERLAP_STRATEGIES = frozenset({"longest", "priority"})

# Defensive caps on parsed override values. The detector_mode cap is
# derived from REGISTERED_SPECS so adding a new detector automatically
# bumps it — pre-refactor this was hardcoded and went silently stale
# every time a detector landed. Three locales covers primary + a couple
# of fallbacks, which is what realistic Faker chains use; anything
# beyond is almost certainly abuse and inflates per-request Faker
# construction time.
_MAX_LOCALE_CHAIN = 3
_MAX_DETECTOR_LIST = len(REGISTERED_SPECS)
# Cap on per-request gliner labels. The model can technically take
# more, but a runaway list balloons the request body, the model's
# attention cost, and per-call latency. 50 covers any realistic
# vocabulary; anything larger smells like an authoring mistake or
# abuse. Same defensive shape as `_MAX_LOCALE_CHAIN`.
_MAX_GLINER_LABELS = 50


def _csv_or_list(max_len: int):
    """Build a BeforeValidator that accepts ``"a,b,c"`` or ``["a","b","c"]``
    and normalizes to a tuple of stripped non-empty strings, capped at
    ``max_len``. Empty input → ``None`` (the field's default).

    Each comma-or-list field has its own cap, so we close over it here
    rather than push the cap into a single generic validator.
    """
    def _validate(v: Any) -> tuple[str, ...] | None:
        if isinstance(v, str):
            parts = [s.strip() for s in v.split(",") if s.strip()]
        elif isinstance(v, (list, tuple)):
            parts = []
            for item in v:
                if not isinstance(item, str):
                    raise ValueError(
                        f"list item must be string, got {type(item).__name__}"
                    )
                s = item.strip()
                if s:
                    parts.append(s)
        else:
            raise ValueError(
                f"expected string or list, got {type(v).__name__}"
            )
        if len(parts) > max_len:
            raise ValueError(
                f"length {len(parts)} exceeds cap of {max_len}"
            )
        return tuple(parts) if parts else None
    return _validate


def _strict_bool(v: Any) -> bool:
    """Reject non-bool inputs. Pydantic's default `bool` field would
    coerce ``"yes"`` / ``1`` to True; we want strictly the JSON
    literals ``true`` / ``false``."""
    if not isinstance(v, bool):
        raise ValueError(f"expected bool, got {type(v).__name__}")
    return v


def _strict_threshold(v: Any) -> float:
    """Accept int or float in [0, 1], rejecting bool. Python `bool` is
    a subclass of `int` so ``True`` would otherwise slip through as
    ``1.0``. The env-var form (`GLINER_PII_THRESHOLD="0.5"`) parses
    strings inside the detector — the JSON request body should always
    carry a number, so accepting strings here would just hide
    misconfigured clients."""
    if isinstance(v, bool):
        raise ValueError("expected number, got bool")
    if not isinstance(v, (int, float)):
        raise ValueError(f"expected number, got {type(v).__name__}")
    f = float(v)
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"value {f} not in [0, 1]")
    return f


def _strict_overlap_strategy(v: Any) -> str:
    if not isinstance(v, str):
        raise ValueError(f"expected string, got {type(v).__name__}")
    norm = v.strip().lower()
    if norm not in _VALID_OVERLAP_STRATEGIES:
        raise ValueError(
            f"unknown strategy {v!r}; allowed: "
            f"{', '.join(sorted(_VALID_OVERLAP_STRATEGIES))}"
        )
    return norm


def _strict_name(v: Any) -> str | None:
    """For naming fields (`regex_patterns`, `llm_prompt`, `denylist`,
    `llm_model`): accept a string, strip surrounding whitespace, and
    treat empty/whitespace-only as 'no override' (None)."""
    if not isinstance(v, str):
        raise ValueError(f"expected string, got {type(v).__name__}")
    stripped = v.strip()
    return stripped if stripped else None


class Overrides(BaseModel):
    model_config = ConfigDict(frozen=True)

    use_faker: Annotated[bool | None, BeforeValidator(_strict_bool)] = None
    faker_locale: Annotated[
        tuple[str, ...] | None, BeforeValidator(_csv_or_list(_MAX_LOCALE_CHAIN))
    ] = None
    detector_mode: Annotated[
        tuple[str, ...] | None, BeforeValidator(_csv_or_list(_MAX_DETECTOR_LIST))
    ] = None
    regex_overlap_strategy: Annotated[
        str | None, BeforeValidator(_strict_overlap_strategy)
    ] = None
    regex_patterns: Annotated[str | None, BeforeValidator(_strict_name)] = None
    llm_model: Annotated[str | None, BeforeValidator(_strict_name)] = None
    llm_prompt: Annotated[str | None, BeforeValidator(_strict_name)] = None
    denylist: Annotated[str | None, BeforeValidator(_strict_name)] = None
    # Tuple keeps the field immutable in line with the rest of the
    # model; the detector converts back to a list when building the
    # request body.
    gliner_labels: Annotated[
        tuple[str, ...] | None, BeforeValidator(_csv_or_list(_MAX_GLINER_LABELS))
    ] = None
    gliner_threshold: Annotated[
        float | None, BeforeValidator(_strict_threshold)
    ] = None

    @classmethod
    def empty(cls) -> "Overrides":
        return cls()


# Per-field TypeAdapters power the lenient parse loop below. A
# TypeAdapter built from the field's annotation (`bool | None` plus
# its BeforeValidator) lets us validate one value at a time and catch
# `ValidationError` per field — which is how we honour the
# warn-and-drop contract without giving up declarative validation.
#
# `field.annotation` strips the `Annotated[...]` wrapper to just the
# bare type; the BeforeValidator and friends live in `field.metadata`.
# We have to reattach them, otherwise the per-field adapter would
# accept inputs (e.g. `"0.5"` for a float, `True` for `gliner_threshold`)
# that the model's own validator rejects, and the bad value would only
# fail at the final `Overrides(**fields)` construction — outside the
# per-field try/except, defeating the warn-and-drop contract.
#
# Shape recovered for the `gliner_threshold` field:
#     field.annotation = float | None
#     field.metadata   = [BeforeValidator(_strict_threshold)]
#     reconstructed    = Annotated[float | None,
#                                  BeforeValidator(_strict_threshold)]
def _field_adapter(field: Any) -> TypeAdapter:
    if field.metadata:
        return TypeAdapter(Annotated[(field.annotation, *field.metadata)])
    return TypeAdapter(field.annotation)


_FIELD_ADAPTERS: dict[str, TypeAdapter] = {
    name: _field_adapter(field)
    for name, field in Overrides.model_fields.items()
}


def _format_validation_error(exc: ValidationError) -> str:
    """Pydantic's str(ValidationError) is multi-line and noisy for log
    output. Pull the first error's `msg` so the log line stays tight
    while still pointing at what was wrong."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    return errors[0].get("msg", str(exc))


def parse_overrides(raw: dict[str, Any] | None) -> Overrides:
    """Build an Overrides from ``additional_provider_specific_params``.

    Validation policy: each unknown / malformed field logs a warning and
    is dropped. Other fields in the same dict still take effect. The
    request itself is never blocked — operators get a logged hint and
    config defaults take over.
    """
    if not raw:
        return Overrides.empty()

    fields: dict[str, Any] = {}
    for key, value in raw.items():
        adapter = _FIELD_ADAPTERS.get(key)
        if adapter is None:
            # Unknown key — silently ignore. Other guardrails sharing
            # the same dict don't apply here (LiteLLM scopes
            # additional_provider_specific_params per guardrail), but
            # we still tolerate forward-compat / typo-tolerance.
            log.debug("ignoring unknown override key %r", key)
            continue
        try:
            validated = adapter.validate_python(value)
        except ValidationError as exc:
            log.warning(
                "ignoring invalid override %s=%r: %s",
                key, value, _format_validation_error(exc),
            )
            continue
        # `None` means the validator legitimately treated the input
        # as "no override" (e.g. empty `faker_locale=""`). Skip rather
        # than pass it back to the model — the BeforeValidator would
        # re-run on the second pass and reject None as not-a-string.
        # Field defaults are None anyway, so this is identical.
        if validated is None:
            continue
        fields[key] = validated
    return Overrides(**fields)
