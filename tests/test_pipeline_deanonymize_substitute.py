"""Tests for `DEANONYMIZE_SUBSTITUTE=false` mode.

Pins:
  * Default true preserves today's full round-trip behaviour
    (covered indirectly by every other pipeline test; one explicit
    pin here for the stable invariant).
  * Substitute-off: deanonymize returns texts UNCHANGED (still
    surrogate-laden) and the vault entry is consumed.
  * Prewarm still fires under substitute-off — but seeds the cache
    for the SURROGATE-LADEN text rather than the original-laden one.
    Subsequent anonymize calls on the same surrogate-laden text get a
    cache hit returning empty matches (no re-anonymization of
    already-anonymized text).
  * Surrogate consistency holds across turns regardless of the flag
    (surrogate cache is independent of deanonymize substitute).
"""

from __future__ import annotations

import os

# Match other test modules: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest


def _setup(monkeypatch, *, deanonymize_substitute: bool, pipeline_cache: bool = True):
    """Configure a fresh pipeline with the test-relevant knobs.
    Patches the central config + each module that did
    `from .config import config`."""
    from anonymizer_guardrail import config as config_mod
    from anonymizer_guardrail import pipeline as pipeline_mod
    from anonymizer_guardrail import surrogate as surrogate_mod
    from anonymizer_guardrail.pipeline import Pipeline

    patched = config_mod.config.model_copy(update={
        "deanonymize_substitute": deanonymize_substitute,
        "pipeline_cache_backend": "memory" if pipeline_cache else "none",
        "pipeline_cache_max_size": 1000 if pipeline_cache else 0,
    })
    monkeypatch.setattr(config_mod, "config", patched)
    monkeypatch.setattr(pipeline_mod, "config", patched)
    monkeypatch.setattr(surrogate_mod, "config", patched)
    return Pipeline()


# ── Default true — full round-trip behaviour preserved ────────────────


async def test_default_substitute_true_restores_originals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity pin: default (`deanonymize_substitute=true`) still
    restores originals on the deanonymize side. Everything in the
    test suite assumes this; explicit pin here makes the regression
    target obvious if a future refactor flips the default."""
    p = _setup(monkeypatch, deanonymize_substitute=True, pipeline_cache=False)

    modified, mapping = await p.anonymize(
        ["Email me at bob@example.com"], call_id="default-1",
    )
    surrogate = next(s for s, o in mapping.items() if o == "bob@example.com")
    response_with_surrogate = f"Reply sent to {surrogate}"

    restored = await p.deanonymize([response_with_surrogate], call_id="default-1")
    assert restored == [f"Reply sent to bob@example.com"]
    await p.aclose()


# ── Substitute=false — texts pass through ─────────────────────────────


async def test_substitute_false_returns_texts_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core contract: deanonymize is a passthrough when the flag is
    off. The response keeps the surrogates the upstream LLM emitted.
    Vault entry is *not* consumed by deanonymize (peek-based) — TTL
    handles cleanup. Pin via a second deanonymize on the same
    call_id which must still observe the entry and produce the same
    surrogate-laden output."""
    p = _setup(monkeypatch, deanonymize_substitute=False, pipeline_cache=False)

    modified, mapping = await p.anonymize(
        ["Email me at carol@example.org"], call_id="skip-1",
    )
    surrogate = next(s for s, o in mapping.items() if o == "carol@example.org")
    response_with_surrogate = f"Reply sent to {surrogate}"

    restored = await p.deanonymize([response_with_surrogate], call_id="skip-1")
    # Surrogate persists in the response.
    assert restored == [response_with_surrogate]
    assert "carol@example.org" not in restored[0]
    assert surrogate in restored[0]

    # Vault entry persists across deanonymize — second call sees the
    # same entry and produces the same surrogate-laden output, matching
    # the streaming-repeated-invocation pattern.
    restored2 = await p.deanonymize([response_with_surrogate], call_id="skip-1")
    assert restored2 == [response_with_surrogate]
    await p.aclose()


async def test_substitute_false_log_marker(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operators tailing the log need a way to see substitute-off
    requests at a glance. Pin the `substitute=skip` marker so a
    log-shipping pipeline can grep for it."""
    import logging

    p = _setup(monkeypatch, deanonymize_substitute=False, pipeline_cache=False)
    modified, _mapping = await p.anonymize(
        ["Contact dave@example.com"], call_id="skip-log-1",
    )

    with caplog.at_level(logging.INFO, logger="anonymizer.pipeline"):
        await p.deanonymize(["echo from model"], call_id="skip-log-1")

    assert any(
        "substitute=skip" in r.message and "skip-log-1" in r.message
        for r in caplog.records
    ), f"expected substitute=skip marker; got: {[r.message for r in caplog.records]}"
    await p.aclose()


# ── Prewarm still fires (cache key matches surrogate-laden text) ──────


async def test_substitute_false_prewarm_seeds_surrogate_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deanonymize-side prewarm must still write to the pipeline
    cache when substitute is off — but keyed on the SURROGATE-LADEN
    text (since that's what a future anonymize call will look up,
    given the client receives surrogates and replays them in chat
    history).

    Probed via `pipeline_cache_size` growth: anonymize doesn't write
    to the cache slot for the response text (the response was never
    anonymized); only prewarm could."""
    p = _setup(monkeypatch, deanonymize_substitute=False, pipeline_cache=True)

    modified, mapping = await p.anonymize(
        ["Reach me at eve@example.com please"], call_id="prewarm-1",
    )
    pre_size = p.stats()["pipeline_cache_size"]

    surrogate = next(s for s, o in mapping.items() if o == "eve@example.com")
    response = f"Acknowledged — I'll reach {surrogate} shortly"
    restored = await p.deanonymize([response], call_id="prewarm-1")
    # Substitute off — response unchanged.
    assert restored == [response]

    post_size = p.stats()["pipeline_cache_size"]
    # Prewarm should have written one slot for the response text.
    assert post_size > pre_size, (
        f"prewarm did not write a slot under substitute=false "
        f"(pre={pre_size}, post={post_size})"
    )
    await p.aclose()


async def test_substitute_false_subsequent_anonymize_hits_prewarmed_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a future anonymize call on the same surrogate-laden
    response text gets a pipeline-cache hit. The cached match list is
    empty (the surrogate-laden text contains no originals to detect),
    so the surrogate-laden text passes through unchanged on the
    second anonymize — exactly the behaviour we want.

    Probes via the pipeline_cache_hits counter."""
    p = _setup(monkeypatch, deanonymize_substitute=False, pipeline_cache=True)

    # Turn 1: anonymize a user message + deanonymize the response
    # (which seeds the cache for the surrogate-laden response text).
    modified, mapping = await p.anonymize(
        ["My phone is +1-555-987-6543"], call_id="hit-1",
    )
    surrogate = next(s for s, o in mapping.items() if o == "+1-555-987-6543")
    response = f"Confirmed phone {surrogate} on file"
    await p.deanonymize([response], call_id="hit-1")

    pre_hits = p.stats()["pipeline_cache_hits"]

    # Turn 2: anonymize a request that includes the surrogate-laden
    # response in its history. The pipeline cache should hit on the
    # response text.
    await p.anonymize(
        [response, "What was my number again?"], call_id="hit-2",
    )

    post_hits = p.stats()["pipeline_cache_hits"]
    assert post_hits > pre_hits, (
        f"expected pipeline-cache hit on surrogate-laden response "
        f"text under substitute=false (pre={pre_hits}, post={post_hits})"
    )
    await p.aclose()


# ── Surrogate consistency across turns ────────────────────────────────


async def test_substitute_false_surrogate_consistency_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same original PII produces the same surrogate across turns
    regardless of the substitute flag — the surrogate cache is
    process-wide and independent of deanonymize. Pin the invariant
    so a future refactor that conflates the two doesn't silently
    break cross-turn references.

    Uses an email (regex-detectable) so the test runs without
    needing the gliner/LLM service stack."""
    p = _setup(monkeypatch, deanonymize_substitute=False, pipeline_cache=False)

    # Turn 1
    _, mapping_1 = await p.anonymize(
        ["Email me at frank@example.org"], call_id="cons-1",
    )
    surrogate_1 = next(s for s, o in mapping_1.items() if o == "frank@example.org")

    # Turn 2 — different text, same email
    _, mapping_2 = await p.anonymize(
        ["frank@example.org needs an update"], call_id="cons-2",
    )
    surrogate_2 = next(s for s, o in mapping_2.items() if o == "frank@example.org")

    assert surrogate_1 == surrogate_2, (
        f"surrogate consistency broken: turn1={surrogate_1!r}, "
        f"turn2={surrogate_2!r}"
    )
    await p.aclose()
