"""Tests for the per-detector merged-input mode.

Covers the dispatch contract of `LLM_INPUT_MODE=merged`:

  * Per-text mode (default) is unchanged — regression coverage.
  * Merged mode → one detector call against a sentinel-joined blob.
  * Sentinel-spanning matches are dropped before mapping construction.
  * Oversized blob falls back to per_text dispatch for that request.
  * Mixed-mode pipeline (regex per_text + LLM merged) works.
  * `cache_max_size > 0` paired with `input_mode=merged` logs a warning
    at Pipeline init.

Helpers come from `_pipeline_helpers.py` and conftest's `pipeline`
fixture is not used here because every test rebuilds the Pipeline
after monkeypatching `llm.CONFIG.input_mode` — fixture caching would
defeat that.
"""

from __future__ import annotations

import logging

import pytest

from anonymizer_guardrail.detector import llm as llm_mod
from anonymizer_guardrail.detector.base import Match
from anonymizer_guardrail.detector.cache import InMemoryDetectionCache
from anonymizer_guardrail.detector.llm import LLMDetector
from anonymizer_guardrail.pipeline import Pipeline, _MERGE_SENTINEL


def _stub_llm_detector(detect_fn) -> LLMDetector:
    """Build a bare LLMDetector with a custom detect() — bypasses
    httpx wiring. Same shape as the helpers in _pipeline_helpers.py
    but accepts an arbitrary detect implementation, since merge-mode
    tests need to capture the text the detector was called with."""
    bad = LLMDetector.__new__(LLMDetector)
    bad.name = "llm"
    bad._cache = InMemoryDetectionCache(0)
    bad.detect = detect_fn  # type: ignore[method-assign]
    return bad


def _patch_llm_config(monkeypatch: pytest.MonkeyPatch, **updates) -> None:
    """Patch `llm.CONFIG` with the given field overrides. Pipeline
    reads `spec.config.<field>` live, so the patch takes effect on
    next Pipeline()."""
    monkeypatch.setattr(
        llm_mod, "CONFIG",
        llm_mod.CONFIG.model_copy(update=updates),
    )


# ── Per-text mode is unchanged (regression) ─────────────────────────────


async def test_per_text_default_calls_detector_once_per_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default mode calls the LLM detector once per text in
    `req.texts`. Locks down the existing fan-out so the merge
    dispatch can't accidentally regress it."""
    _patch_llm_config(monkeypatch, fail_closed=False, input_mode="per_text")

    seen_texts: list[str] = []

    async def detect(text, *_args, **_kwargs):
        seen_texts.append(text)
        return []

    p = Pipeline()
    p._detectors = [_stub_llm_detector(detect)]

    await p.anonymize(["alpha", "beta", "gamma"], call_id="per-text-test")

    assert seen_texts == ["alpha", "beta", "gamma"]


# ── Merged mode dispatch ────────────────────────────────────────────────


async def test_merged_mode_calls_detector_once_with_joined_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merged mode collapses N texts into ONE call carrying the
    sentinel-joined blob. The whole point of the optimisation."""
    _patch_llm_config(monkeypatch, fail_closed=False, input_mode="merged")

    seen_texts: list[str] = []

    async def detect(text, *_args, **_kwargs):
        seen_texts.append(text)
        return []

    p = Pipeline()
    p._detectors = [_stub_llm_detector(detect)]

    await p.anonymize(["alpha", "beta", "gamma"], call_id="merged-test")

    assert len(seen_texts) == 1
    blob = seen_texts[0]
    assert blob == _MERGE_SENTINEL.join(["alpha", "beta", "gamma"])


async def test_merged_mode_matches_propagate_to_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matches from the merged-mode call must apply to the original
    texts, not the merged blob — substitution is value-based via
    `_build_replacer`."""
    _patch_llm_config(monkeypatch, fail_closed=False, input_mode="merged")

    async def detect(_text, *_args, **_kwargs):
        # Merged-mode detector returns a real entity that appears in
        # one of the original texts. The pipeline's value-based
        # substitution must surrogate it in that text.
        return [Match(text="alice@example.com", entity_type="EMAIL_ADDRESS")]

    p = Pipeline()
    p._detectors = [_stub_llm_detector(detect)]

    modified, mapping = await p.anonymize(
        ["hello", "alice@example.com is here", "goodbye"],
        call_id="merged-substitute-test",
    )

    assert modified[0] == "hello"
    assert "alice@example.com" not in modified[1]
    assert modified[2] == "goodbye"
    assert "alice@example.com" in mapping.values()


# ── Sentinel filter ─────────────────────────────────────────────────────


async def test_merged_mode_filters_sentinel_spanning_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A match whose text contains the sentinel literal — either
    because the model classified the separator as an entity, or
    returned a span crossing a segment boundary — must be dropped
    before mapping construction. The surrogate generator should
    never see a sentinel-bearing match."""
    _patch_llm_config(monkeypatch, fail_closed=False, input_mode="merged")

    async def detect(_text, *_args, **_kwargs):
        return [
            # Real match — must be kept
            Match(text="alice", entity_type="PERSON"),
            # Cross-boundary hallucination — must be dropped
            Match(text=f"alice{_MERGE_SENTINEL}bob", entity_type="PERSON"),
            # Sentinel itself classified — must be dropped
            Match(text=_MERGE_SENTINEL.strip(), entity_type="OTHER"),
        ]

    p = Pipeline()
    p._detectors = [_stub_llm_detector(detect)]

    _, mapping = await p.anonymize(["alice met bob"], call_id="sentinel-filter-test")

    assert "alice" in mapping.values()
    # Neither the cross-boundary hallucination nor the sentinel itself
    # should produce a vault entry.
    for original in mapping.values():
        assert _MERGE_SENTINEL not in original
        assert _MERGE_SENTINEL.strip() not in original


# ── Size fallback ───────────────────────────────────────────────────────


async def test_merged_mode_falls_back_to_per_text_when_blob_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the merged blob exceeds `LLM_MAX_CHARS`, the LLM detector
    falls back to per_text dispatch for THAT request only — the mode
    on the detector instance is unchanged."""
    # Tight cap so a small payload exercises the fallback path.
    _patch_llm_config(
        monkeypatch, fail_closed=False, input_mode="merged", max_chars=20,
    )

    seen_texts: list[str] = []

    async def detect(text, *_args, **_kwargs):
        seen_texts.append(text)
        return []

    p = Pipeline()
    p._detectors = [_stub_llm_detector(detect)]

    # Two texts that together (with sentinel) blow past the 20-char cap.
    await p.anonymize(
        ["aaaaaaaaaa", "bbbbbbbbbb"], call_id="size-fallback-test",
    )

    # Per-text dispatch: one call per text.
    assert seen_texts == ["aaaaaaaaaa", "bbbbbbbbbb"]


async def test_merged_mode_succeeds_when_blob_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary check on size fallback: when the blob fits exactly
    (or just below the cap), merged dispatch fires normally."""
    blob = _MERGE_SENTINEL.join(["a", "b"])
    _patch_llm_config(
        monkeypatch, fail_closed=False, input_mode="merged",
        max_chars=len(blob),
    )

    seen_texts: list[str] = []

    async def detect(text, *_args, **_kwargs):
        seen_texts.append(text)
        return []

    p = Pipeline()
    p._detectors = [_stub_llm_detector(detect)]

    await p.anonymize(["a", "b"], call_id="size-boundary-test")

    # Single merged call — at-cap is allowed.
    assert seen_texts == [blob]


# ── Mixed-mode pipeline ─────────────────────────────────────────────────


async def test_mixed_mode_regex_per_text_plus_llm_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realistic configuration: regex (always per_text — it's
    shape-based and microseconds) plus LLM in merged mode for context.
    Both contribute matches; pipeline dedupes the union."""
    _patch_llm_config(monkeypatch, fail_closed=False, input_mode="merged")

    llm_seen: list[str] = []

    async def llm_detect(text, *_args, **_kwargs):
        llm_seen.append(text)
        # LLM finds a person name only it can detect.
        return [Match(text="Alice", entity_type="PERSON")]

    p = Pipeline()
    # Keep the real regex detector + replace LLM with our stub.
    regex_det = next(d for d in p._detectors if d.name == "regex")
    p._detectors = [regex_det, _stub_llm_detector(llm_detect)]

    _, mapping = await p.anonymize(
        ["Alice at 10.20.30.40", "another 10.20.30.40 mention"],
        call_id="mixed-mode-test",
    )

    # LLM saw the merged blob exactly once.
    assert len(llm_seen) == 1
    assert "Alice" in llm_seen[0]
    assert "10.20.30.40" in llm_seen[0]

    # Both the regex IP match and the LLM PERSON match appear in the
    # vault — the union is what gets surrogate-mapped.
    originals = set(mapping.values())
    assert "Alice" in originals
    assert "10.20.30.40" in originals


# ── Cache + merge warning ───────────────────────────────────────────────


def test_cache_plus_merge_emits_warning_at_pipeline_init(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """An operator who configures both `cache_max_size > 0` AND
    `input_mode=merged` for the same detector gets a startup warning.
    The cache is silently inactive in merged mode — without this
    warning, a confused operator sees no cache hits and wonders why."""
    _patch_llm_config(monkeypatch, input_mode="merged", cache_max_size=100)

    with caplog.at_level(logging.WARNING, logger="anonymizer.pipeline"):
        Pipeline()

    matched = [
        r for r in caplog.records
        if "input_mode=merged" in r.message
        and "cache_max_size" in r.message
        and "llm" in r.message.lower()
    ]
    assert matched, (
        "expected a WARNING about the cache + merge mutual exclusivity, "
        f"got: {[r.message for r in caplog.records]}"
    )


def test_no_warning_when_cache_or_merge_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Cache-only or merge-only configurations are valid and shouldn't
    fire the warning. Locks down "warns only on the mutually exclusive
    combination."""
    # Merge-only: cache_max_size=0
    _patch_llm_config(monkeypatch, input_mode="merged", cache_max_size=0)
    with caplog.at_level(logging.WARNING, logger="anonymizer.pipeline"):
        Pipeline()
    assert not [
        r for r in caplog.records
        if "input_mode=merged" in r.message and "cache_max_size" in r.message
    ]
    caplog.clear()

    # Cache-only: input_mode=per_text
    _patch_llm_config(monkeypatch, input_mode="per_text", cache_max_size=100)
    with caplog.at_level(logging.WARNING, logger="anonymizer.pipeline"):
        Pipeline()
    assert not [
        r for r in caplog.records
        if "input_mode=merged" in r.message and "cache_max_size" in r.message
    ]
