"""End-to-end HTTP tests for the FastAPI endpoint.

The other test files exercise the Pipeline / detectors / surrogate generator
directly; this one drives the actual `/beta/litellm_basic_guardrail_api`
and `/health` HTTP routes via FastAPI's TestClient. It catches regressions
in main.py's request-handling glue: action mapping, forwarded-key
extraction, exception → BLOCKED translation, and the lifespan shutdown
hook firing without errors.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

# Force regex-only mode BEFORE importing main — otherwise the module-level
# Pipeline() in main.py would try to initialise the LLM detector and reach
# for the LLM endpoint at import time.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest
from fastapi.testclient import TestClient

from anonymizer_guardrail import main as main_mod


@pytest.fixture
def client() -> TestClient:
    # Wrapping in `with` triggers FastAPI lifespan startup/shutdown — we
    # exercise that path too, so a broken aclose() would surface here.
    with TestClient(main_mod.app) as c:
        yield c


# ── Happy paths ──────────────────────────────────────────────────────────────


def test_health_returns_expected_shape(client: TestClient) -> None:
    """The /health probe must return the documented keys with the right
    types, regardless of how /beta is doing."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # The 5 stats fields from Pipeline.stats() must all be present and ints.
    for key in (
        "vault_size",
        "surrogate_cache_size",
        "surrogate_cache_max",
        "llm_in_flight",
        "llm_max_concurrency",
    ):
        assert key in body, f"missing stat: {key}"
        assert isinstance(body[key], int)
    assert isinstance(body["detector_mode"], str)


def test_request_with_no_texts_returns_none(client: TestClient) -> None:
    """Empty texts → nothing to do, action=NONE, no vault entry created."""
    pre_size = main_mod._pipeline.vault.size()
    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": [],
            "input_type": "request",
            "litellm_call_id": "no-texts-test",
        },
    )
    assert r.status_code == 200
    assert r.json()["action"] == "NONE"
    assert main_mod._pipeline.vault.size() == pre_size


def test_request_with_no_detectable_entities_returns_none(client: TestClient) -> None:
    """The regex layer finds nothing in plain prose → action=NONE, no
    GUARDRAIL_INTERVENED, no vault entry."""
    pre_size = main_mod._pipeline.vault.size()
    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["the quick brown fox jumps over the lazy dog"],
            "input_type": "request",
            "litellm_call_id": "boring-test",
        },
    )
    assert r.json()["action"] == "NONE"
    # Vault unchanged because no mapping was created.
    assert main_mod._pipeline.vault.size() == pre_size


def test_request_with_entities_returns_intervened_and_redacts(
    client: TestClient,
) -> None:
    """A detectable entity → action=GUARDRAIL_INTERVENED, the original is
    not visible in the modified text, and a vault entry exists for later
    deanonymization."""
    call_id = "intervene-test"
    original = "Mail alice@example.com from 10.20.30.40"
    pre_size = main_mod._pipeline.vault.size()
    try:
        r = client.post(
            "/beta/litellm_basic_guardrail_api",
            json={
                "texts": [original],
                "input_type": "request",
                "litellm_call_id": call_id,
            },
        )
        body = r.json()
        assert body["action"] == "GUARDRAIL_INTERVENED"
        assert body["texts"] is not None and len(body["texts"]) == 1
        modified = body["texts"][0]
        assert "alice@example.com" not in modified
        assert "10.20.30.40" not in modified
        # Vault must hold the reverse mapping for the post-call —
        # exactly one new entry, not "at least one" (the pre-snapshot
        # rules out cross-test pollution from a previous test that
        # forgot to clean up).
        assert main_mod._pipeline.vault.size() == pre_size + 1
    finally:
        # Pop our entry so the vault is clean for the next test, even
        # under failure. `main._pipeline._vault` is a process-wide
        # singleton; without this, every endpoint test that uses
        # `client` inherits this entry.
        import asyncio
        asyncio.run(main_mod._pipeline.vault.pop(call_id))


def test_round_trip_request_then_response(client: TestClient) -> None:
    """The full LiteLLM hook cycle: pre-call (request) anonymizes,
    post-call (response) restores. The response we ship back to the user
    must contain the original strings, not the surrogates."""
    call_id = "roundtrip-test"
    original = "Reach me at bob@acmecorp.com"

    pre = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": [original],
            "input_type": "request",
            "litellm_call_id": call_id,
        },
    ).json()
    assert pre["action"] == "GUARDRAIL_INTERVENED"
    surrogate_text = pre["texts"][0]
    assert "bob@acmecorp.com" not in surrogate_text

    # The "model response" is what we'd get back from the upstream LLM —
    # for a faithful test, mention the same surrogate so deanonymize has
    # something to substitute back.
    post = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": [f"Sure, I'll contact {surrogate_text} shortly."],
            "input_type": "response",
            "litellm_call_id": call_id,
        },
    ).json()
    assert post["action"] == "GUARDRAIL_INTERVENED"
    assert "bob@acmecorp.com" in post["texts"][0]

    # Vault entry persists across deanonymize — streaming responses
    # call us repeatedly with the same call_id, all needing to see
    # the same surrogate map. Confirm it survived AND that a second
    # response-side call still restores correctly. Cleanup happens
    # via the explicit pop below (utility path); production
    # deployments rely on VAULT_TTL_S.
    import asyncio
    from anonymizer_guardrail.vault import VaultEntry
    entry_after = asyncio.run(main_mod._pipeline.vault.peek(call_id))
    assert entry_after != VaultEntry(), (
        "deanonymize is read-only; entry should still be in the vault "
        "for the next streaming-chunk invocation"
    )
    second_post = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": [f"Sure, I'll contact {surrogate_text} shortly."],
            "input_type": "response",
            "litellm_call_id": call_id,
        },
    ).json()
    assert second_post["action"] == "GUARDRAIL_INTERVENED"
    assert "bob@acmecorp.com" in second_post["texts"][0]
    # Drain so the vault is clean for the next test.
    asyncio.run(main_mod._pipeline.vault.pop(call_id))


# ── Streaming action protocol (LiteLLM B3 / iterator_hook_mode=action) ───────


def test_streaming_response_partial_surrogate_returns_wait(
    client: TestClient,
) -> None:
    """When the streamed response trails off mid-surrogate (e.g. `[EMAI`),
    the next upstream chunk is likely the rest of the token. Substituting
    now would emit a partial surrogate to the client and then prevent the
    post-substitution text from prefixing what was already emitted (the
    LiteLLM action protocol's cursor-monotonic invariant). Return WAIT so
    LiteLLM holds the trailing bytes until the next chunk lands."""
    call_id = "streaming-wait-test"
    # Pre-call to seed the vault — though we never get to substitute here
    # because the suffix is a partial surrogate.
    client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["Reach me at alice@example.com"],
            "input_type": "request",
            "litellm_call_id": call_id,
        },
    )

    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["Sure, I'll contact [EMAIL_ADDR"],
            "input_type": "response",
            "litellm_call_id": call_id,
            "is_final": False,
        },
    ).json()
    assert r["action"] == "WAIT", r
    assert r.get("texts") is None  # WAIT must not include modified texts

    # Cleanup
    import asyncio
    asyncio.run(main_mod._pipeline.vault.pop(call_id))


def test_streaming_response_complete_surrogate_returns_intervened(
    client: TestClient,
) -> None:
    """A streamed chunk that ends after a closed surrogate `]` is safe
    to substitute. action=GUARDRAIL_INTERVENED with the deanonymized text."""
    call_id = "streaming-complete-test"
    pre = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["Reach me at alice@example.com"],
            "input_type": "request",
            "litellm_call_id": call_id,
        },
    ).json()
    surrogate = pre["texts"][0].split("Reach me at ")[1]

    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": [f"Sure, I'll contact {surrogate} shortly."],
            "input_type": "response",
            "litellm_call_id": call_id,
            "is_final": False,
        },
    ).json()
    assert r["action"] == "GUARDRAIL_INTERVENED", r
    assert "alice@example.com" in r["texts"][0]

    import asyncio
    asyncio.run(main_mod._pipeline.vault.pop(call_id))


def test_streaming_response_partial_surrogate_at_eos_does_not_wait(
    client: TestClient,
) -> None:
    """At end-of-stream (is_final=True) WAIT is forbidden by the action
    protocol. A trailing partial surrogate at EOS is a stray `[ABC`-shaped
    fragment from the model and is left as-is by deanonymize."""
    call_id = "streaming-eos-test"
    client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["Reach me at alice@example.com"],
            "input_type": "request",
            "litellm_call_id": call_id,
        },
    )

    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["Sure thing — talk soon. [INCOMPLETE"],
            "input_type": "response",
            "litellm_call_id": call_id,
            "is_final": True,
        },
    ).json()
    # The trailing fragment isn't in the vault and won't deanonymize, so
    # the text comes back unchanged → action is NONE. Either NONE or
    # GUARDRAIL_INTERVENED is acceptable; WAIT must not appear.
    assert r["action"] in ("NONE", "GUARDRAIL_INTERVENED"), r
    assert r["action"] != "WAIT"

    import asyncio
    asyncio.run(main_mod._pipeline.vault.pop(call_id))


def test_streaming_response_no_is_final_field_skips_wait(
    client: TestClient,
) -> None:
    """Non-streaming requests don't include is_final. The WAIT path only
    triggers on is_final=False; without it we substitute as today."""
    call_id = "no-isfinal-test"
    pre = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["Reach me at alice@example.com"],
            "input_type": "request",
            "litellm_call_id": call_id,
        },
    ).json()
    surrogate = pre["texts"][0].split("Reach me at ")[1]

    # Mid-surrogate trailer but no is_final field — this is the legacy
    # non-streaming shape, where we substitute whatever's present.
    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": [f"Reply mentions {surrogate} and trails [EMAI"],
            "input_type": "response",
            "litellm_call_id": call_id,
        },
    ).json()
    assert r["action"] != "WAIT"

    import asyncio
    asyncio.run(main_mod._pipeline.vault.pop(call_id))


def test_response_with_unknown_call_id_returns_none(client: TestClient) -> None:
    """A post-call without a matching pre-call (e.g. the request was
    served by a different replica, or pre-call never happened) → action
    is NONE; we don't have a mapping to apply, so we forward the model's
    output unchanged."""
    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["nothing here corresponds to anything we anonymized"],
            "input_type": "response",
            "litellm_call_id": "never-saw-this-call-id",
        },
    )
    assert r.json()["action"] == "NONE"


# ── Error paths ──────────────────────────────────────────────────────────────


class _BasePipelineStub:
    """Shared scaffolding for pipeline test stubs. Provides the vault,
    `stats()` payload, and `aclose` no-op that every Pipeline-shaped
    stub needs; subclasses implement the `anonymize` / `deanonymize`
    behaviour they're trying to exercise.

    Putting this here (instead of in `_pipeline_helpers.py`) because
    these stubs are endpoint-test-specific — they fake the *Pipeline
    Protocol* main.py expects, not the per-detector Protocol other
    tests fake. If a third stub class shows up that's also Pipeline-
    shaped, this is the natural extension point."""

    def __init__(self) -> None:
        from anonymizer_guardrail.vault_memory import MemoryVault

        # /health reads vault.size(); a real MemoryVault gives a working
        # size() without forcing every subclass to fake one.
        self.vault = MemoryVault()

    def stats(self) -> dict[str, int]:
        return {
            "vault_size": 0,
            "surrogate_cache_size": 0,
            "surrogate_cache_max": 1,
            "llm_in_flight": 0,
            "llm_max_concurrency": 1,
        }

    async def aclose(self) -> None:
        return None


class _ExplodingPipeline(_BasePipelineStub):
    """Stand-in pipeline whose anonymize/deanonymize raise on demand.
    Lets us exercise main.guardrail's exception handling without any
    LLM machinery."""

    def __init__(self, exc_factory) -> None:
        super().__init__()
        self._exc = exc_factory

    async def anonymize(self, *args: Any, **kwargs: Any):
        raise self._exc()

    async def deanonymize(self, *args: Any, **kwargs: Any):
        raise self._exc()


def test_llm_unavailable_returns_blocked_with_specific_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMUnavailableError must surface as action=BLOCKED with the
    LiteLLM-facing reason string mentioning unreachable anonymization
    LLM. Without that, an operator chasing a 'BLOCKED' has no clue
    whether the cause was network, programmer error, or policy."""
    from anonymizer_guardrail.detector.llm import LLMUnavailableError

    monkeypatch.setattr(
        main_mod,
        "_pipeline",
        _ExplodingPipeline(lambda: LLMUnavailableError("boom")),
    )
    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["alice"],
            "input_type": "request",
            "litellm_call_id": "fail-llm",
        },
    )
    body = r.json()
    assert body["action"] == "BLOCKED"
    assert "unreachable" in (body["blocked_reason"] or "").lower()


def test_vault_unavailable_returns_blocked_with_vault_specific_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RedisVaultError` from the vault layer (Redis unreachable, auth
    error, etc.) must surface as action=BLOCKED with a *vault-specific*
    operator-facing reason — not the generic "Guardrail internal error"
    fallthrough. The pipeline raises `RedisVaultError` from
    `_vault.put()` / `_vault.pop()`; main.py has a dedicated
    `except RedisVaultError` clause that emits a "Vault backend
    unavailable" reason. Pin the end-to-end shape so a future refactor
    that swallows the typed error into the generic `except Exception`
    path doesn't silently regress operator UX."""
    from anonymizer_guardrail.vault_redis import RedisVaultError

    monkeypatch.setattr(
        main_mod,
        "_pipeline",
        _ExplodingPipeline(lambda: RedisVaultError("simulated Redis outage")),
    )
    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["alice"],
            "input_type": "request",
            "litellm_call_id": "fail-vault",
        },
    )
    body = r.json()
    assert body["action"] == "BLOCKED"
    reason = (body["blocked_reason"] or "").lower()
    # Operator-facing reason should name the vault, not generic
    # "internal error". Don't pin the exact wording — that would be
    # over-strict — but require the vault-specific signal.
    assert "vault" in reason
    # And must NOT route through the generic fallthrough whose reason
    # includes the Python type name.
    assert "RedisVaultError" not in (body["blocked_reason"] or "")


def test_unexpected_exception_returns_blocked_with_internal_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-LLMUnavailableError exception bubbling out of the pipeline
    must NOT crash the endpoint or leak a stack trace; it must turn into
    a clean BLOCKED with an internal-error reason. Confirms the broad
    `except Exception` in main.guardrail is doing its job."""
    monkeypatch.setattr(
        main_mod,
        "_pipeline",
        _ExplodingPipeline(lambda: RuntimeError("some bug")),
    )
    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["alice"],
            "input_type": "request",
            "litellm_call_id": "fail-other",
        },
    )
    body = r.json()
    assert body["action"] == "BLOCKED"
    # The reason should name the exception type but not the message —
    # we don't want raw exception details leaking to the client.
    assert "RuntimeError" in (body["blocked_reason"] or "")
    assert "some bug" not in (body["blocked_reason"] or "")


# ── Forwarded-key path ───────────────────────────────────────────────────────


def _patch_llm_config(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Thin shim over the generic `_patch_detector_config` helper.

    Tests in this file historically used `llm_use_forwarded_key=True`
    style kwargs from when this was a central-Config knob; the
    `llm_` prefix gets stripped here so existing call sites keep
    working without churn. New tests should pass field names directly
    (e.g. `use_forwarded_key=True`)."""
    from anonymizer_guardrail.detector import llm as llm_mod
    from tests._pipeline_helpers import _patch_detector_config

    cleaned = {k.removeprefix("llm_"): v for k, v in overrides.items()}
    _patch_detector_config(monkeypatch, llm_mod, **cleaned)


class _CapturingPipeline(_BasePipelineStub):
    """Pipeline stand-in that records the api_key it was called with so
    the test can assert on the forwarded-key extraction path. Returns no
    matches so the response is action=NONE — we're testing wiring, not
    detection."""

    def __init__(self) -> None:
        super().__init__()
        self.received_api_key: str | None = "<not called>"

    async def anonymize(
        self,
        texts: list[str],
        call_id: str | None,
        *,
        api_key: str | None = None,
        overrides: Any = None,
    ):
        # `overrides` was added when the guardrail picked up
        # additional_provider_specific_params support — accept it but
        # don't assert anything here; the forwarded-key tests below
        # only care about api_key plumbing.
        self.received_api_key = api_key
        return list(texts), {}

    async def deanonymize(self, texts: list[str], call_id: str | None):
        return list(texts)


def test_forwarded_bearer_extracted_when_feature_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM_USE_FORWARDED_KEY=true + a `Bearer …` Authorization header in
    request_headers → the token reaches Pipeline.anonymize as api_key."""
    cap = _CapturingPipeline()
    monkeypatch.setattr(main_mod, "_pipeline", cap)
    _patch_llm_config(monkeypatch, llm_use_forwarded_key=True)

    client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["irrelevant"],
            "input_type": "request",
            "litellm_call_id": "fwd-test",
            "request_headers": {"Authorization": "Bearer sk-virtual-12345"},
        },
    )
    assert cap.received_api_key == "sk-virtual-12345"


def test_forwarded_bearer_ignored_when_feature_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with a Bearer header on the request, the key is NOT forwarded
    when LLM_USE_FORWARDED_KEY is false. Default behaviour."""
    cap = _CapturingPipeline()
    monkeypatch.setattr(main_mod, "_pipeline", cap)
    _patch_llm_config(monkeypatch, llm_use_forwarded_key=False)

    client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["irrelevant"],
            "input_type": "request",
            "litellm_call_id": "fwd-disabled",
            "request_headers": {"Authorization": "Bearer sk-should-be-ignored"},
        },
    )
    assert cap.received_api_key is None


def test_redacted_authorization_does_not_become_bearer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LiteLLM's redaction marker '[present]' must NEVER be treated as a
    bearer token — that would send a literal `Bearer [present]` to the
    detection LLM and produce a confusing 401."""
    cap = _CapturingPipeline()
    monkeypatch.setattr(main_mod, "_pipeline", cap)
    _patch_llm_config(monkeypatch, llm_use_forwarded_key=True)

    client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": ["irrelevant"],
            "input_type": "request",
            "litellm_call_id": "redacted-test",
            "request_headers": {"authorization": "[present]"},
        },
    )
    assert cap.received_api_key is None


# ── MAX_BODY_BYTES middleware ──────────────────────────────────────────────
# Caps POST body size BEFORE Pydantic deserializes (DoS protection
# distinct from LLM_MAX_CHARS). 503 chosen over 413 so LiteLLM's
# hardcoded `is_unreachable in (502, 503, 504)` check routes the
# rejection through the operator's `unreachable_fallback` setting —
# see main.py's `_BodySizeLimitMiddleware` docstring for the rationale.


def _client_with_cap(monkeypatch: pytest.MonkeyPatch, max_bytes: int) -> TestClient:
    """Rebuild the FastAPI app with a smaller body cap so tests can
    exercise the limit without sending megabytes through TestClient.
    The default cap (10 MiB) makes legitimate test payloads unrejectable,
    which is exactly the production behaviour we want — but useless for
    proving the middleware fires."""
    from fastapi import FastAPI
    from anonymizer_guardrail.main import _BodySizeLimitMiddleware

    test_app = FastAPI()

    @test_app.post("/echo")
    async def echo(payload: dict) -> dict:
        return {"received": len(payload.get("texts", []))}

    @test_app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    test_app.add_middleware(_BodySizeLimitMiddleware, max_bytes=max_bytes)
    return TestClient(test_app)


def test_body_under_cap_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body comfortably under the cap goes through and the handler
    sees the payload it would have seen without the middleware."""
    c = _client_with_cap(monkeypatch, max_bytes=1024)
    r = c.post("/echo", json={"texts": ["a", "b", "c"]})
    assert r.status_code == 200
    assert r.json() == {"received": 3}


def test_body_at_exactly_cap_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: len == cap is allowed, len > cap is rejected. Pads the
    payload to a deterministic byte length so the boundary is testable."""
    import json
    payload = {"pad": "a" * 100}
    body = json.dumps(payload).encode("utf-8")
    cap = len(body)  # exactly at cap

    c = _client_with_cap(monkeypatch, max_bytes=cap)
    r = c.post("/echo", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 200


def test_body_over_cap_rejected_with_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Honest client: declared Content-Length over the cap. Reject in
    O(1) with 503 — must NOT touch the handler at all."""
    c = _client_with_cap(monkeypatch, max_bytes=100)
    big_payload = {"pad": "x" * 1000}  # ~1 KB, well over 100 B
    r = c.post("/echo", json=big_payload)
    assert r.status_code == 503
    assert b"MAX_BODY_BYTES" in r.content


def test_body_over_cap_via_chunked_rejected_with_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunked transfer (no Content-Length): the streaming path counts
    cumulative bytes and rejects once the cap is crossed. Simulated by
    sending raw bytes via httpx.Stream so TestClient doesn't add a
    Content-Length header automatically."""
    c = _client_with_cap(monkeypatch, max_bytes=100)

    def chunked_body():
        yield b'{"pad": "'
        yield b"x" * 200    # cumulative ~210 B, over 100 B cap
        yield b'"}'

    # httpx infers chunked transfer when given a generator and no
    # explicit Content-Length. The middleware's streaming path then
    # fires and rejects mid-stream.
    r = c.post(
        "/echo",
        content=chunked_body(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 503
    assert b"MAX_BODY_BYTES" in r.content


def test_health_request_passes_through_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET requests with no body must not be confused with oversized
    POSTs. Middleware should pass them through cleanly."""
    c = _client_with_cap(monkeypatch, max_bytes=10)  # tiny cap
    r = c.get("/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_content_length_lie_caught_by_streaming_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attacker (or buggy client) might declare Content-Length within
    the cap and then send a chunked body that exceeds it. The middleware's
    fast-path Content-Length check passes, but the streaming counter must
    still catch the actual oversize and reject with 503. Pins that the
    declared CL doesn't get treated as authoritative."""
    c = _client_with_cap(monkeypatch, max_bytes=100)

    def chunked_body():
        # Far over 100 bytes when actually summed.
        yield b"x" * 500

    # Force a tiny declared Content-Length while sending a much larger
    # chunked body. httpx normally chooses one transfer mode or the
    # other; we force the Content-Length header explicitly. Starlette
    # / uvicorn TestClient honor this.
    r = c.post(
        "/echo",
        content=chunked_body(),
        headers={
            "content-type": "application/json",
            "content-length": "10",  # the lie
        },
    )
    # Either path should reject. With the explicit CL=10 < cap=100, the
    # fast-path doesn't trip — the streaming path catches the actual
    # 500-byte body.
    assert r.status_code == 503
    assert b"MAX_BODY_BYTES" in r.content


def test_concurrent_over_cap_requests_each_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The middleware must not share state across concurrent requests.
    Two concurrent oversized POSTs both reject; neither leaks bytes /
    counter state into the other's request."""
    import threading

    c = _client_with_cap(monkeypatch, max_bytes=100)
    big = {"pad": "x" * 1000}

    results: dict[int, int] = {}

    def post(idx: int) -> None:
        r = c.post("/echo", json=big)
        results[idx] = r.status_code

    threads = [threading.Thread(target=post, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {0: 503, 1: 503, 2: 503, 3: 503}


def test_real_endpoint_default_cap_doesnt_reject_legitimate_traffic(
    client: TestClient,
) -> None:
    """The default 10 MiB cap on the real app must comfortably accept
    realistic guardrail payloads — pin a small-but-real one as a
    regression check that we didn't accidentally default the cap too low."""
    # ~5 KB of texts: well within 10 MiB, well above any realistic
    # production payload's smallest text.
    big_text = "alice@example.com " * 250  # ~4.5 KB
    r = client.post(
        "/beta/litellm_basic_guardrail_api",
        json={
            "texts": [big_text],
            "input_type": "request",
            "litellm_call_id": "default-cap-regression",
        },
    )
    # Don't assert the exact action — depends on whether regex caught
    # the email. We just need the request to be ACCEPTED (not 503).
    assert r.status_code == 200