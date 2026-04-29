"""
FastAPI app for the LiteLLM Generic Guardrail API.

Single endpoint: POST /beta/litellm_basic_guardrail_api
  - input_type="request"  → anonymize, store mapping under call_id
  - input_type="response" → deanonymize, evict mapping
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

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


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "vault_size": _pipeline.vault.size(),
        "detector_mode": config.detector_mode,
    }


@app.post("/beta/litellm_basic_guardrail_api", response_model=GuardrailResponse)
async def guardrail(req: GuardrailRequest) -> GuardrailResponse:
    if not req.texts:
        # Nothing for us to do — let the request through unchanged.
        return GuardrailResponse(action="NONE")

    try:
        if req.input_type == "request":
            modified, mapping = await _pipeline.anonymize(req.texts, req.litellm_call_id)
            if not mapping:
                return GuardrailResponse(action="NONE")
            return GuardrailResponse(action="GUARDRAIL_INTERVENED", texts=modified)

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
