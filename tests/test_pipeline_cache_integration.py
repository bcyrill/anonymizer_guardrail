"""End-to-end tests for the bidirectional pipeline-level cache.

Pins:
  * Detection-side write: a second anonymize for the same text + same
    kwargs hits the pipeline cache (no detector dispatch, observable
    via mocked HTTP-call counters).
  * Override sensitivity: changing an LLM-affecting kwarg (model)
    misses the pipeline cache → detection runs again. Identical
    request afterwards hits the new slot.
  * Faker support: USE_FAKER=true round-trips through the pipeline
    cache via the typed vault entry — no surrogate-prefix recovery
    needed.
  * Source-detector-driven per-detector prewarm: deanonymize-side
    prewarm fills each per-detector cache with ONLY that detector's
    matches (filtered via vault's source_detectors).
  * Vault entry shape: anonymize stores `VaultEntry` with all three
    structured fields (typed surrogates, detector_mode, kwargs).
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Match other test modules: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")


def _setup(
    monkeypatch,
    *,
    pipeline_cache_backend: str = "memory",
    pipeline_cache_max_size: int = 100,
    use_faker: bool = False,
    detector_mode: str = "regex,llm",
    llm_cache_max_size: int = 100,
):
    """Shared setup for the integration tests. Returns
    (Pipeline, mock_post). Caller drives anonymize / deanonymize and
    inspects mock_post.call_count to detect cache hits vs real
    detection."""
    from anonymizer_guardrail import config as config_mod
    from anonymizer_guardrail import pipeline as pipeline_mod
    from anonymizer_guardrail import surrogate as surrogate_mod
    from anonymizer_guardrail.detector import llm as llm_mod
    from anonymizer_guardrail.pipeline import Pipeline

    patched_config = config_mod.config.model_copy(update={
        "detector_mode": detector_mode,
        "use_faker": use_faker,
        "pipeline_cache_backend": pipeline_cache_backend,
        "pipeline_cache_max_size": pipeline_cache_max_size,
    })
    # Each module that did `from .config import config` keeps its own
    # binding; patch each module that reads it. Same shape as the prior
    # prewarm tests.
    monkeypatch.setattr(config_mod, "config", patched_config)
    monkeypatch.setattr(pipeline_mod, "config", patched_config)
    monkeypatch.setattr(surrogate_mod, "config", patched_config)
    monkeypatch.setattr(
        llm_mod, "CONFIG",
        llm_mod.CONFIG.model_copy(update={
            "cache_max_size": llm_cache_max_size,
            "max_chars": 10_000,
        }),
    )

    mock_post = AsyncMock()

    def _ok_response(entities):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"entities": entities})}}]
        }
        resp.text = ""
        return resp

    mock_post.return_value = _ok_response([
        {"text": "alice@corp.example", "type": "EMAIL_ADDRESS"},
    ])
    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    return Pipeline(), mock_post


# ── Detection-side write ───────────────────────────────────────────────


async def test_pipeline_cache_hit_skips_detector_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First anonymize on a text runs detection (LLM HTTP call fires).
    Second anonymize on the SAME text + same kwargs hits the pipeline
    cache — no LLM HTTP call. This is the load-bearing happy path."""
    p, mock_post = _setup(monkeypatch)

    user_text = "Email me at alice@corp.example."
    await p.anonymize([user_text], call_id="t1")
    pre_count = mock_post.call_count
    assert pre_count >= 1  # LLM ran on the first call

    await p.anonymize([user_text], call_id="t2")
    assert mock_post.call_count == pre_count, (
        f"expected pipeline-cache hit on second identical anonymize — "
        f"got {mock_post.call_count - pre_count} extra POST(s)"
    )

    # /health stats reflect a hit + miss.
    stats = p.stats()
    assert stats["pipeline_cache_hits"] >= 1
    assert stats["pipeline_cache_misses"] >= 1
    assert stats["pipeline_cache_backend"] == "memory"
    await p.aclose()


# ── Override sensitivity ───────────────────────────────────────────────


async def test_pipeline_cache_misses_when_kwargs_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same text but different LLM model override → cache key
    differs → real detection runs again. Identical follow-up hits the
    new slot."""
    from anonymizer_guardrail.api import Overrides

    p, mock_post = _setup(monkeypatch)

    user_text = "Email me at alice@corp.example."
    await p.anonymize([user_text], call_id="t1")
    after_first = mock_post.call_count

    # Change the model — different cache key.
    await p.anonymize(
        [user_text], call_id="t2",
        overrides=Overrides(llm_model="other-model"),
    )
    assert mock_post.call_count > after_first, (
        "expected real detection to run when model override changes"
    )
    after_second = mock_post.call_count

    # Repeat identical override → hit.
    await p.anonymize(
        [user_text], call_id="t3",
        overrides=Overrides(llm_model="other-model"),
    )
    assert mock_post.call_count == after_second, (
        "expected hit on second identical-override anonymize"
    )
    await p.aclose()


# ── Pipeline cache off (sentinel sanity) ───────────────────────────────


async def test_no_pipeline_cache_when_backend_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PIPELINE_CACHE_BACKEND=none` (default) means every anonymize
    runs detection. The per-detector cache may still hit, but the
    pipeline cache is a no-op passthrough."""
    p, mock_post = _setup(
        monkeypatch,
        pipeline_cache_backend="none",
        pipeline_cache_max_size=0,
        llm_cache_max_size=0,  # also disable per-detector cache so no hits anywhere
    )

    user_text = "Email me at alice@corp.example."
    await p.anonymize([user_text], call_id="t1")
    after_first = mock_post.call_count
    await p.anonymize([user_text], call_id="t2")
    assert mock_post.call_count > after_first, (
        "expected real detection on every call when both caches are off"
    )

    stats = p.stats()
    assert stats["pipeline_cache_backend"] == "none"
    assert stats["pipeline_cache_hits"] == 0
    assert stats["pipeline_cache_misses"] == 0
    await p.aclose()


# ── Vault entry shape ──────────────────────────────────────────────────


async def test_vault_entry_carries_typed_structured_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """anonymize must persist the structured `VaultEntry` shape:
    typed surrogates, detector_mode, frozen kwargs. This is what the
    deanonymize-side prewarm relies on for Faker support and
    source-detector filtering."""
    from anonymizer_guardrail.vault import VaultEntry

    p, _ = _setup(monkeypatch)

    await p.anonymize(
        ["Email alice@corp.example for help"], call_id="probe-1",
    )

    entry = await p.vault.pop("probe-1")
    assert isinstance(entry, VaultEntry)
    assert entry.detector_mode == ("regex", "llm")
    # kwargs is a frozen tuple of `(detector_name, frozen_kwargs)` per
    # active detector. LLM's frozen kwargs include model + prompt_name
    # but NOT api_key (intentionally excluded from the cache view).
    kwargs_dict = dict(entry.kwargs)
    assert "regex" in kwargs_dict
    assert "llm" in kwargs_dict
    llm_keys = {k for k, _ in kwargs_dict["llm"]}
    assert "model" in llm_keys
    assert "prompt_name" in llm_keys
    assert "api_key" not in llm_keys, (
        "api_key must not flow through the cache view — would fragment "
        "the cache per user for no correctness benefit"
    )

    # Surrogates are typed + carry sources.
    assert entry.surrogates, "expected at least one surrogate"
    for sur, vs in entry.surrogates.items():
        assert vs.entity_type, f"surrogate {sur!r} missing entity_type"
        assert vs.source_detectors, (
            f"surrogate {sur!r} missing source_detectors — "
            "the Option-3-style prewarm relies on this attribution"
        )
    await p.aclose()


# ── Faker support ──────────────────────────────────────────────────────


async def test_faker_round_trip_through_pipeline_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USE_FAKER=true now round-trips through the pipeline cache via
    the typed vault entry. The previous Option-2 prewarm forbade
    this combination because surrogate-prefix recovery couldn't
    extract types from Faker output; the structured vault makes it
    a non-issue."""
    p, mock_post = _setup(monkeypatch, use_faker=True)

    user_text = "Reach alice@corp.example for support."
    modified, mapping = await p.anonymize([user_text], call_id="faker-1")
    # Faker output: looks like a realistic email, NOT `[EMAIL_ADDRESS_…]`.
    surrogate = next(s for s, o in mapping.items() if o == "alice@corp.example")
    assert not surrogate.startswith("["), (
        f"expected Faker-format surrogate, got opaque {surrogate!r}"
    )

    pre_count = mock_post.call_count

    # Second anonymize on the same text → pipeline cache hit.
    await p.anonymize([user_text], call_id="faker-2")
    assert mock_post.call_count == pre_count, (
        "expected pipeline-cache hit under USE_FAKER=true"
    )

    # And a deanonymize round-trip that prewarms the cache for the
    # response text. The vault entry carries entity_type + sources, so
    # we can synthesise typed Match objects without surrogate-prefix
    # recovery.
    response_with_surrogate = f"Confirmed contact for {surrogate} on file."
    restored = await p.deanonymize(
        [response_with_surrogate], call_id="faker-1",
    )
    assert "alice@corp.example" in restored[0]

    # Subsequent anonymize on the deanonymized response text → hit
    # (prewarm landed there).
    pre_response_count = mock_post.call_count
    await p.anonymize([restored[0]], call_id="faker-3")
    assert mock_post.call_count == pre_response_count, (
        "expected pipeline-cache prewarm hit on response text — "
        "the previous Option-2 implementation couldn't do this under Faker"
    )
    await p.aclose()


# ── Source-detector-driven per-detector prewarm ────────────────────────


async def test_source_detectors_filter_per_detector_prewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deanonymize hook filters per-detector cache writes by
    `source_detectors` from the vault entry. An entity found by regex
    only (LLM returned nothing for that text) lands in the regex slot
    only — NOT the LLM slot.

    Probed via vault inspection: the source_detectors field reflects
    which detectors actually produced each match. The filtering itself
    is internal to `_prewarm_caches`, so the load-bearing assertion is
    that source_detectors == ('regex',) when LLM didn't find the entity.
    """
    p, mock_post = _setup(monkeypatch, llm_cache_max_size=0)

    # Override the LLM mock to return NO entities — only regex finds
    # the email (via the bundled patterns).
    def _no_entities():
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"entities": []})}}]
        }
        resp.text = ""
        return resp
    mock_post.return_value = _no_entities()

    await p.anonymize(
        ["Email alice@corp.example for help"], call_id="src-1",
    )

    # Inspect the vault entry — the email's source_detectors should be
    # only ('regex',) since the LLM didn't return it.
    entry = await p.vault.pop("src-1")
    email_surrogates = [
        (sur, vs) for sur, vs in entry.surrogates.items()
        if vs.entity_type == "EMAIL_ADDRESS"
    ]
    assert email_surrogates, "expected regex to find the email"
    sur, vs = email_surrogates[0]
    assert vs.source_detectors == ("regex",), (
        f"expected source_detectors=('regex',), got {vs.source_detectors!r}"
    )
    await p.aclose()
