"""
FastAPI app for the LiteLLM Generic Guardrail API.

Single endpoint: POST /beta/litellm_basic_guardrail_api
  - input_type="request"  → anonymize, store mapping under call_id
  - input_type="response" → deanonymize, evict mapping
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Mapping

from fastapi import FastAPI

from .api import GuardrailRequest, GuardrailResponse, parse_overrides
from .config import config
from .detector.llm import LLMUnavailableError
from .detector.remote_privacy_filter import PrivacyFilterUnavailableError
from .pipeline import Pipeline

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("anonymizer")

# Single Pipeline instance — its surrogate cache and vault are intentionally
# process-wide so we get cross-call surrogate consistency for free.
_pipeline = Pipeline()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Drain the LLM detector's connection pool on shutdown so SIGTERM
    doesn't leave half-open sockets. Pre-yield setup is empty — the
    Pipeline is constructed at import-time."""
    yield
    await _pipeline.aclose()


app = FastAPI(
    title="anonymizer-guardrail",
    description="LiteLLM Generic Guardrail API for round-trip anonymization.",
    version="0.1.0",
    lifespan=lifespan,
)


def _forwarded_bearer(headers: Mapping[str, object] | None) -> str | None:
    """Extract the user's bearer token from a forwarded Authorization header.

    LiteLLM only forwards the actual header value when the guardrail is
    configured with `extra_headers: [authorization]`; otherwise the value
    arrives as the literal string "[present]" (a redaction marker), which we
    must NOT use as a bearer token. Returns None for missing/redacted/non-
    Bearer values so the caller can fall back to LLM_API_KEY cleanly.

    Header lookup is case-insensitive — HTTP doesn't constrain casing and
    LiteLLM's forwarded dict preserves whatever the client sent.
    """
    if not headers:
        return None
    value: str | None = None
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() == "authorization":
            value = str(v) if v is not None else None
            break
    if not value or value == "[present]":
        return None
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1].strip()
        return token or None
    return None


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "detector_mode": config.detector_mode,
        **_pipeline.stats(),
    }


@app.post("/beta/litellm_basic_guardrail_api", response_model=GuardrailResponse)
async def guardrail(req: GuardrailRequest) -> GuardrailResponse:
    # Full request body contains the very text we're meant to anonymize, so
    # only emit it at DEBUG. Guard with isEnabledFor so we don't pay the
    # JSON-serialization cost on every call in production.
    if log.isEnabledFor(logging.DEBUG):
        log.debug("Guardrail request: %s", req.model_dump_json())

    if not req.texts:
        # Nothing for us to do — let the request through unchanged.
        return GuardrailResponse(action="NONE")

    forwarded_key: str | None = None
    if config.llm_use_forwarded_key and req.input_type == "request":
        forwarded_key = _forwarded_bearer(req.request_headers)
        if forwarded_key is None and not config.llm_api_key:
            # Bump to WARNING (not DEBUG) when there's no fallback — this path
            # produces a 401 from the detection LLM with a confusing "no api
            # key" message, so the operator deserves a pointer to the actual
            # misconfiguration rather than having to dig through the LLM logs.
            auth_value = next(
                (str(v) for k, v in (req.request_headers or {}).items()
                 if isinstance(k, str) and k.lower() == "authorization"),
                None,
            )
            if auth_value is None:
                hint = (
                    "no Authorization header was forwarded. Add "
                    "`extra_headers: [authorization]` to the guardrail's "
                    "litellm_params, or set LLM_API_KEY as a fallback."
                )
            elif auth_value == "[present]":
                hint = (
                    "Authorization header arrived as the redaction marker "
                    "'[present]'. Add `authorization` to the guardrail's "
                    "`extra_headers` allowlist in your LiteLLM config."
                )
            else:
                hint = (
                    "Authorization header is present but not in `Bearer <token>` "
                    "form, which is the only scheme we know how to forward."
                )
            log.warning(
                "LLM_USE_FORWARDED_KEY is set with no LLM_API_KEY fallback, "
                "but %s",
                hint,
            )

    # Per-request overrides come in via additional_provider_specific_params
    # (see api.parse_overrides for the accepted keys + validation policy).
    # Only meaningful on the request side — deanonymize is a vault lookup
    # that's already pinned by call_id.
    overrides = parse_overrides(req.additional_provider_specific_params)

    try:
        if req.input_type == "request":
            modified, mapping = await _pipeline.anonymize(
                req.texts,
                req.litellm_call_id,
                api_key=forwarded_key,
                overrides=overrides,
            )
            if not mapping:
                return GuardrailResponse(action="NONE")
            return GuardrailResponse(action="GUARDRAIL_INTERVENED", texts=modified)
        else:
            # input_type == "response"
            restored = await _pipeline.deanonymize(req.texts, req.litellm_call_id)
            if restored == req.texts:
                return GuardrailResponse(action="NONE")
            return GuardrailResponse(action="GUARDRAIL_INTERVENED", texts=restored)

    except LLMUnavailableError as exc:
        # Reached only when llm_fail_closed=True; otherwise the pipeline swallows it.
        log.error("Blocking request — LLM detector unavailable: %s", exc)
        return GuardrailResponse(
            action="BLOCKED",
            blocked_reason=(
                "Anonymization LLM is unreachable; request blocked to prevent "
                "unredacted data from reaching the upstream model."
            ),
        )
    except PrivacyFilterUnavailableError as exc:
        # Reached only when privacy_filter_fail_closed=True; otherwise the
        # pipeline swallows it. Same risk shape as the LLM case — silent
        # downgrade of redaction coverage is the failure mode we're guarding
        # against.
        log.error("Blocking request — privacy-filter detector unavailable: %s", exc)
        return GuardrailResponse(
            action="BLOCKED",
            blocked_reason=(
                "Anonymization privacy-filter is unreachable; request "
                "blocked to prevent unredacted data from reaching the "
                "upstream model."
            ),
        )
    except Exception as exc:
        # Unexpected failure → block. LiteLLM's `unreachable_fallback` setting
        # only triggers on network/HTTP errors talking to us; logical errors
        # surface as a BLOCKED action.
        log.exception("Guardrail call failed unexpectedly: %s", exc)
        return GuardrailResponse(
            action="BLOCKED",
            blocked_reason=f"Guardrail internal error: {type(exc).__name__}",
        )
