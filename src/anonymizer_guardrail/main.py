"""
FastAPI app for the LiteLLM Generic Guardrail API.

Single endpoint: POST /beta/litellm_basic_guardrail_api
  - input_type="request"  → anonymize, store mapping under call_id
  - input_type="response" → deanonymize, evict mapping
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from typing import Mapping

from .api import GuardrailRequest, GuardrailResponse
from .config import config
from .detector.llm import LLMUnavailableError
from .pipeline import Pipeline

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("anonymizer")

app = FastAPI(
    title="anonymizer-guardrail",
    description="LiteLLM Generic Guardrail API for round-trip anonymization.",
    version="0.1.0",
)

# Single Pipeline instance — its surrogate cache and vault are intentionally
# process-wide so we get cross-call surrogate consistency for free.
_pipeline = Pipeline()


def _forwarded_bearer(headers: Mapping[str, object]) -> str | None:
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
        "vault_size": _pipeline.vault.size(),
        "detector_mode": config.detector_mode,
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
        if forwarded_key is None:
            log.debug(
                "LLM_USE_FORWARDED_KEY is set but no Authorization bearer was "
                "forwarded — falling back to LLM_API_KEY. Ensure LiteLLM's "
                "guardrail config includes `extra_headers: [authorization]`."
            )

    try:
        if req.input_type == "request":
            modified, mapping = await _pipeline.anonymize(
                req.texts, req.litellm_call_id, api_key=forwarded_key
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
        # Reached only when fail_closed=True; otherwise the pipeline swallows it.
        log.error("Blocking request — LLM detector unavailable: %s", exc)
        return GuardrailResponse(
            action="BLOCKED",
            blocked_reason=(
                "Anonymization LLM is unreachable; request blocked to prevent "
                "unredacted data from reaching the upstream model."
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
