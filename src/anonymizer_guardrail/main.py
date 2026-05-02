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
from .detector import REGISTERED_SPECS, TYPED_UNAVAILABLE_ERRORS
from .detector import llm as llm_mod
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


class _BodySizeLimitMiddleware:
    """Pure-ASGI middleware that rejects POST bodies above
    ``config.max_body_bytes`` with HTTP 503 BEFORE Pydantic
    deserializes them.

    Distinct from `LLM_MAX_CHARS`: that's an LLM-context-window concern
    that fires after the body is already in memory; this is a
    DoS-protection concern that prevents the process from allocating
    memory for a runaway payload in the first place. See
    `docs/configuration.md`.

    Two paths:

    - **Honest client (`Content-Length` header set):** the size is
      known up front; reject in O(1) without reading the body.
    - **Chunked / no Content-Length:** buffer chunks while counting
      cumulative bytes; reject as soon as the cap is crossed.
      Otherwise replay the buffered body to the app verbatim. Worst-
      case memory per request is `max_bytes` (the buffer is dropped
      either way).

    Implemented as a pure ASGI middleware rather than
    `BaseHTTPMiddleware` because the latter consumes the request body
    through its own internal pipeline; constructing a `Request` with
    a custom `receive` callable doesn't intercept the body-read path
    `BaseHTTPMiddleware` uses, so the downstream handler's
    `await request.json()` blocks forever. The ASGI-level wrap of
    `receive` is the canonical pattern for body-size enforcement.

    The cap applies to *every* request — including `/health`. That's
    intentional: a no-op endpoint shouldn't accept a 500 MB body
    either, and a generous default cap (10 MiB) is well above any
    realistic guardrail payload.

    Why 503 instead of the semantically-precise 413: LiteLLM's
    Generic Guardrail API client (see
    `litellm/proxy/guardrails/guardrail_hooks/generic_guardrail_api/`)
    has a hardcoded `is_unreachable = status_code in (502, 503, 504)`
    check. Returning 413 makes LiteLLM treat the response as a hard
    failure and block the upstream LLM call regardless of the
    operator's `unreachable_fallback` setting. Returning 503 routes
    through `unreachable_fallback` instead, so operators who chose
    `fail_open` get traffic through and those who chose `fail_closed`
    get the request blocked — *both* honoured. The HTTP-semantic
    mismatch (503 means "service unavailable," not "body too large")
    is the cost; honouring operator intent is the win.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        # Lifespan / websocket / etc. — pass through untouched.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Fast path: declared Content-Length over cap. Reject in O(1)
        # without reading the body. Headers are list[(bytes, bytes)] in
        # raw ASGI scope.
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    # Malformed header — leave it for downstream to
                    # reject; we don't enforce on garbage input.
                    break
                if declared > self.max_bytes:
                    await self._send_too_large(send, declared)
                    return
                break

        # Streaming path: drain receive() into a buffer, counting bytes
        # as they arrive. Reject the moment cumulative size crosses the
        # cap. Otherwise replay the buffered messages to the downstream
        # app so the handler reads the same body it would have seen.
        buffered: list[dict] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # Disconnect or other event — buffer it and stop.
                buffered.append(message)
                break
            chunk = message.get("body", b"") or b""
            total += len(chunk)
            if total > self.max_bytes:
                await self._send_too_large(send, total)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        # Replay buffered messages once; after they're exhausted, behave
        # like a closed stream (disconnect). The downstream handler
        # reads via this wrapped receive and sees exactly the body we
        # already measured.
        replay_idx = 0

        async def _replay():
            nonlocal replay_idx
            if replay_idx < len(buffered):
                msg = buffered[replay_idx]
                replay_idx += 1
                return msg
            return {"type": "http.disconnect"}

        await self.app(scope, _replay, send)

    async def _send_too_large(self, send, observed_bytes: int) -> None:
        log.warning(
            "Rejecting request body of %d bytes (cap %d). Adjust "
            "MAX_BODY_BYTES if legitimate traffic exceeds the cap.",
            observed_bytes, self.max_bytes,
        )
        body = (
            f"Request body exceeds the {self.max_bytes}-byte cap "
            f"(MAX_BODY_BYTES). Reject at the proxy or raise the cap."
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


app.add_middleware(_BodySizeLimitMiddleware, max_bytes=config.max_body_bytes)


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
    if llm_mod.CONFIG.use_forwarded_key and req.input_type == "request":
        forwarded_key = _forwarded_bearer(req.request_headers)
        if forwarded_key is None and not llm_mod.CONFIG.api_key:
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

    except TYPED_UNAVAILABLE_ERRORS as exc:
        # Reached only when the matching detector's fail_closed flag is
        # true; otherwise the pipeline swallows the error and degrades.
        # The spec lookup picks the right BLOCKED message — adding a new
        # detector with its own typed error doesn't require an edit
        # here, just a new entry in REGISTERED_SPECS.
        spec = next(
            s for s in REGISTERED_SPECS
            if s.unavailable_error is not None and isinstance(exc, s.unavailable_error)
        )
        log.error(
            "Blocking request — %s detector unavailable: %s",
            spec.name, exc,
        )
        return GuardrailResponse(
            action="BLOCKED",
            blocked_reason=spec.blocked_reason,
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
