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


# ── Per-text filtering at prewarm ──────────────────────────────────────


async def test_prewarm_filters_synth_per_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical: prewarm writes per-text-scoped match lists, NOT the
    full vault-entry union to every text's slot. Without this, a
    response text containing only one entity would get a cache value
    listing every entity from anywhere in the original anonymize call,
    and the next anonymize on that text would write spurious vault
    entries for entities that aren't actually in the text.

    Test shape:
      1. Anonymize two texts, each containing a different email.
      2. Deanonymize a response containing only ONE of the two
         surrogates → restored response text contains only one email.
      3. Anonymize the restored response text.
      4. Vault entry for that anonymize call should contain ONLY the
         one email — NOT a leaked entry for the other email from the
         original turn.
    """
    p, mock_post = _setup(monkeypatch, llm_cache_max_size=0)

    # Disable the LLM finding things (so regex is the sole detector
    # that finds emails — pin source_detectors to ('regex',)).
    def _no_entities():
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"entities": []})}}]
        }
        resp.text = ""
        return resp
    mock_post.return_value = _no_entities()

    # Anonymize two texts with distinct emails.
    text_a = "Contact alice@example.org for billing"
    text_b = "Contact bob@example.com for support"
    _, mapping = await p.anonymize([text_a, text_b], call_id="leak-1")
    sur_a = next(s for s, o in mapping.items() if o == "alice@example.org")
    sur_b = next(s for s, o in mapping.items() if o == "bob@example.com")

    # Deanonymize a response that mentions ONLY alice's surrogate.
    response = f"Acknowledged — billing routed to {sur_a}."
    restored = await p.deanonymize([response], call_id="leak-1")
    assert "alice@example.org" in restored[0]
    assert "bob@example.com" not in restored[0]

    # Anonymize the restored response text — pipeline cache hit
    # should give us ONLY alice's match, not bob's. Otherwise the
    # vault entry for this call would carry a stale bob-surrogate
    # the substring replacer never applies.
    _, mapping2 = await p.anonymize([restored[0]], call_id="leak-2")
    originals = set(mapping2.values())
    assert "alice@example.org" in originals
    assert "bob@example.com" not in originals, (
        "prewarm leaked an entity from a different text — synth "
        "should be filtered per-text by substring presence"
    )
    await p.aclose()


# ── Empty-text in batch ────────────────────────────────────────────────


async def test_empty_text_in_batch_does_not_pollute_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty string in the texts list short-circuits at multiple
    levels — `_detect_via_cache` returns [] for empty input, the
    pipeline-cache prewarm guard skips empty restored texts, and
    `_safe_pipeline_put`'s defensive guard catches any escapees.
    Pin that no slot is written for empty input so a future
    `anonymize([""])` doesn't get a cached match list."""
    p, _ = _setup(monkeypatch, llm_cache_max_size=0)

    # Anonymize a real text; no empty-text mix yet — pure baseline.
    await p.anonymize(["Email alice@example.org"], call_id="empty-1")
    pre_size = p.stats()["pipeline_cache_size"]

    # Deanonymize with an empty response in the batch.
    restored = await p.deanonymize(["", "no entities here"], call_id="empty-1")
    assert restored == ["", "no entities here"]

    # The pipeline cache should NOT have grown by an entry keyed on
    # empty text. Two new slots possible at most: one for the
    # non-empty restored text. The empty text must not contribute.
    post_size = p.stats()["pipeline_cache_size"]
    # The non-empty text is short enough that its filtered synth
    # list is empty (no surrogate's original is in "no entities here").
    # Even so, an empty-list slot may be written for it. The empty
    # text must NOT contribute a slot.
    assert post_size - pre_size <= 1, (
        f"expected at most 1 new pipeline-cache slot from prewarm "
        f"(only the non-empty text), got {post_size - pre_size}"
    )
    await p.aclose()


# ── Detector dropped between anonymize and deanonymize ─────────────────


async def test_prewarm_skips_detectors_missing_from_vault_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anonymize with detector_mode override that excludes the LLM,
    then deanonymize. The vault entry's `kwargs` only carries regex,
    so prewarm must skip the LLM per-detector cache (no kwargs to
    construct a key with). Pins that DETECTOR_MODE-drift between
    anonymize and deanonymize doesn't crash prewarm."""
    import logging

    from anonymizer_guardrail.api import Overrides

    p, mock_post = _setup(monkeypatch)

    # Anonymize with regex-only override → vault entry kwargs only
    # has 'regex'.
    user_text = "Email alice@example.org"
    _, mapping = await p.anonymize(
        [user_text], call_id="drift-1",
        overrides=Overrides(detector_mode=("regex",)),
    )
    surrogate = next(s for s, o in mapping.items() if o == "alice@example.org")

    # Deanonymize a response — `self._detectors` still has llm
    # configured, but the vault entry's kwargs only has regex.
    response = f"Confirmed contact {surrogate} on file."
    with caplog.at_level(logging.DEBUG, logger="anonymizer.pipeline"):
        restored = await p.deanonymize([response], call_id="drift-1")

    # Round-trip survives despite the kwargs-mismatch.
    assert "alice@example.org" in restored[0]

    # The skip is logged at DEBUG so an operator investigating "why
    # didn't my prewarm fire?" has a breadcrumb.
    assert any(
        "DETECTOR_MODE drift" in r.message and "llm" in r.message
        for r in caplog.records
    ), f"expected drift-skip log; got: {[r.message for r in caplog.records]}"
    await p.aclose()


# ── Streaming-action-protocol prewarm gate ─────────────────────────────


async def test_deanonymize_skips_prewarm_on_mid_stream_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`is_final=False` (mid-stream chunk under PR #27228) must NOT
    write a pipeline-cache slot — the accumulated-so-far text is a
    partial assistant reply that no future request will key on, so
    caching it just evicts useful entries.

    `is_final=None` (non-streaming, also stock-LiteLLM streaming
    samples) and `is_final=True` (end-of-stream) must keep prewarming
    — they carry the text the client actually receives, which is
    what next-turn anonymize will look up."""
    p, _ = _setup(monkeypatch, llm_cache_max_size=0)

    # Seed a vault entry once; reuse for all three deanonymize calls.
    text = "Reach alice@corp.example for support."
    _, mapping = await p.anonymize([text], call_id="stream-1")
    surrogate = next(iter(mapping))

    response = f"OK, contacting {surrogate} now."
    baseline_size = p.stats()["pipeline_cache_size"]

    # Mid-stream (is_final=False): prewarm must be skipped.
    await p.deanonymize([response], call_id="stream-1", is_final=False)
    assert p.stats()["pipeline_cache_size"] == baseline_size, (
        "mid-stream deanonymize wrote a pipeline-cache slot — prewarm "
        "should have been skipped under is_final=False"
    )

    # End-of-stream (is_final=True): prewarm runs, slot gets written.
    await p.deanonymize([response], call_id="stream-1", is_final=True)
    after_final_size = p.stats()["pipeline_cache_size"]
    assert after_final_size > baseline_size, (
        "end-of-stream deanonymize did NOT write a pipeline-cache slot "
        "— prewarm should have run under is_final=True"
    )

    # Non-streaming (is_final=None, the legacy default): prewarm runs.
    # Same response text → no new slot (already cached on the True
    # call), but probe via a distinct text so the size delta is
    # observable.
    response_2 = f"Sent — also CC'd {surrogate}."
    pre_legacy = p.stats()["pipeline_cache_size"]
    await p.deanonymize([response_2], call_id="stream-1")
    assert p.stats()["pipeline_cache_size"] > pre_legacy, (
        "non-streaming deanonymize (is_final=None) did NOT prewarm — "
        "the gate must only skip on is_final=False, not on None"
    )
    await p.aclose()
