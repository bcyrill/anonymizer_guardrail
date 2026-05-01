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
    # Vault must hold the reverse mapping for the post-call.
    assert main_mod._pipeline.vault.size() >= 1


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
    # Vault entry must have been popped — otherwise mappings linger past
    # their useful lifetime.
    assert call_id not in main_mod._pipeline.vault._store  # type: ignore[attr-defined]


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


class _ExplodingPipeline:
    """Stand-in pipeline whose anonymize/deanonymize raise on demand.
    Lets us exercise main.guardrail's exception handling without any
    LLM machinery."""

    def __init__(self, exc_factory) -> None:
        from anonymizer_guardrail.vault import Vault

        self._exc = exc_factory
        self.vault = Vault()  # /health needs vault.size()

    def stats(self) -> dict[str, int]:
        return {
            "vault_size": 0,
            "surrogate_cache_size": 0,
            "surrogate_cache_max": 1,
            "llm_in_flight": 0,
            "llm_max_concurrency": 1,
        }

    async def anonymize(self, *args: Any, **kwargs: Any):
        raise self._exc()

    async def deanonymize(self, *args: Any, **kwargs: Any):
        raise self._exc()

    async def aclose(self) -> None:
        return None


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


def _patch_config(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Replace main_mod.config with a SimpleNamespace mirroring the real
    Config plus overrides. Config is a frozen dataclass, so we can't
    mutate it directly."""
    fields = {
        f: getattr(main_mod.config, f)
        for f in main_mod.config.__dataclass_fields__
    }
    fields.update(overrides)
    monkeypatch.setattr(main_mod, "config", SimpleNamespace(**fields))


class _CapturingPipeline:
    """Pipeline stand-in that records the api_key it was called with so
    the test can assert on the forwarded-key extraction path. Returns no
    matches so the response is action=NONE — we're testing wiring, not
    detection."""

    def __init__(self) -> None:
        from anonymizer_guardrail.vault import Vault

        self.vault = Vault()
        self.received_api_key: str | None = "<not called>"

    def stats(self) -> dict[str, int]:
        return {
            "vault_size": 0,
            "surrogate_cache_size": 0,
            "surrogate_cache_max": 1,
            "llm_in_flight": 0,
            "llm_max_concurrency": 1,
        }

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

    async def aclose(self) -> None:
        return None


def test_forwarded_bearer_extracted_when_feature_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM_USE_FORWARDED_KEY=true + a `Bearer …` Authorization header in
    request_headers → the token reaches Pipeline.anonymize as api_key."""
    cap = _CapturingPipeline()
    monkeypatch.setattr(main_mod, "_pipeline", cap)
    _patch_config(monkeypatch, llm_use_forwarded_key=True)

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
    _patch_config(monkeypatch, llm_use_forwarded_key=False)

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
    _patch_config(monkeypatch, llm_use_forwarded_key=True)

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