"""Smoke tests for the anonymization pipeline (regex-only mode)."""

from __future__ import annotations

import os

# Force regex-only so tests don't need an LLM. Must be set before importing
# anything that reads config.
os.environ["DETECTOR_MODE"] = "regex"

import asyncio

import pytest

from anonymizer_guardrail.pipeline import Pipeline


@pytest.fixture
def pipeline() -> Pipeline:
    return Pipeline()


async def test_round_trip_restores_original(pipeline: Pipeline) -> None:
    original = (
        "Connect to 10.20.30.40 using token sk-abc123XYZ456defGHI789jklMNO012p "
        "and email alice@acmecorp.com."
    )

    modified, mapping = await pipeline.anonymize([original], call_id="test-1")
    assert modified[0] != original
    assert "10.20.30.40" not in modified[0]
    assert "alice@acmecorp.com" not in modified[0]
    assert "sk-abc123XYZ456defGHI789jklMNO012p" not in modified[0]
    assert len(mapping) >= 3

    restored = await pipeline.deanonymize(modified, call_id="test-1")
    assert restored[0] == original


async def test_same_entity_gets_consistent_surrogate(pipeline: Pipeline) -> None:
    text_a = "host A: 10.0.0.1"
    text_b = "host A again: 10.0.0.1"

    modified, mapping = await pipeline.anonymize([text_a, text_b], call_id="test-2")
    # Both texts mention 10.0.0.1 — the surrogate should match.
    surrogate = next(s for s, o in mapping.items() if o == "10.0.0.1")
    assert surrogate in modified[0]
    assert surrogate in modified[1]


async def test_unknown_call_id_passes_through(pipeline: Pipeline) -> None:
    text = "no mapping was ever stored for this call"
    out = await pipeline.deanonymize([text], call_id="never-stored")
    assert out == [text]


async def test_empty_input(pipeline: Pipeline) -> None:
    modified, mapping = await pipeline.anonymize([], call_id="empty")
    assert modified == []
    assert mapping == {}


async def test_no_entities_produces_empty_mapping(pipeline: Pipeline) -> None:
    text = "the quick brown fox jumps over the lazy dog"
    modified, mapping = await pipeline.anonymize([text], call_id="boring")
    assert modified == [text]
    assert mapping == {}


async def test_vault_evicts_after_pop(pipeline: Pipeline) -> None:
    await pipeline.anonymize(["email me at bob@example.com"], call_id="evict-test")
    assert pipeline.vault.size() == 1
    await pipeline.deanonymize(["…"], call_id="evict-test")
    assert pipeline.vault.size() == 0


def _patch_fail_closed(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Flip the LLM detector's fail_closed flag for one test by patching
    `llm_mod.CONFIG`. The pipeline reads `spec.config.fail_closed` live,
    so the patch takes effect immediately."""
    from dataclasses import replace
    from anonymizer_guardrail.detector import llm as llm_mod

    monkeypatch.setattr(
        llm_mod, "CONFIG", replace(llm_mod.CONFIG, fail_closed=value),
    )


def _patch_pf_fail_closed(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Same shim for the privacy_filter detector's fail_closed flag."""
    from dataclasses import replace
    from anonymizer_guardrail.detector import privacy_filter as pf_mod

    monkeypatch.setattr(
        pf_mod, "CONFIG", replace(pf_mod.CONFIG, fail_closed=value),
    )


def _patch_gliner_pii_fail_closed(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Same shim for the gliner_pii detector's fail_closed flag."""
    from dataclasses import replace
    from anonymizer_guardrail.detector import remote_gliner_pii as gp_mod

    monkeypatch.setattr(
        gp_mod, "CONFIG", replace(gp_mod.CONFIG, fail_closed=value),
    )


def _gliner_detector_that_raises(exc_factory):
    """A bare RemoteGlinerPIIDetector instance whose detect() raises
    whatever exc_factory returns. Bypasses the real httpx wiring."""
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        RemoteGlinerPIIDetector,
    )

    bad = RemoteGlinerPIIDetector.__new__(RemoteGlinerPIIDetector)
    bad.name = "gliner_pii"

    async def boom(*_args, **_kwargs):
        raise exc_factory()

    bad.detect = boom  # type: ignore[method-assign]
    return bad


def _pf_detector_that_raises(exc_factory):
    """A bare RemotePrivacyFilterDetector instance whose detect() raises
    whatever exc_factory returns. Bypasses the real httpx wiring."""
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        RemotePrivacyFilterDetector,
    )

    bad = RemotePrivacyFilterDetector.__new__(RemotePrivacyFilterDetector)
    bad.name = "privacy_filter"

    async def boom(*_args, **_kwargs):
        raise exc_factory()

    bad.detect = boom  # type: ignore[method-assign]
    return bad


def _llm_detector_that_raises(exc_factory):
    """A bare LLMDetector instance whose detect() raises whatever
    exc_factory returns. Bypasses the real httpx wiring."""
    from anonymizer_guardrail.detector.llm import LLMDetector

    bad = LLMDetector.__new__(LLMDetector)
    bad.name = "llm"

    async def boom(*_args, **_kwargs):
        raise exc_factory()

    bad.detect = boom  # type: ignore[method-assign]
    return bad


async def test_unexpected_llm_failure_routes_through_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under LLM_FAIL_CLOSED, any failure inside the LLM detector — even an
    unexpected one that isn't LLMUnavailableError — must propagate so the
    guardrail BLOCKs the request instead of silently returning empty
    matches. Without this, a programmer error in the LLM path would let
    unredacted text reach the upstream model in fail-closed mode."""
    from anonymizer_guardrail.detector.llm import LLMUnavailableError

    _patch_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_llm_detector_that_raises(
        lambda: RuntimeError("unexpected non-LLMUnavailable explosion")
    )]

    with pytest.raises(LLMUnavailableError, match="Unexpected failure"):
        await p.anonymize(["alice"], call_id="unexpected-fail")


async def test_privacy_filter_unavailable_propagates_under_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRIVACY_FILTER_FAIL_CLOSED=true → a PrivacyFilterUnavailableError
    inside the PF detector propagates so main.py returns BLOCKED. Without
    this, an outage of the privacy-filter service would silently drop PF
    coverage even though the operator explicitly chose fail-closed."""
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        PrivacyFilterUnavailableError,
    )

    _patch_pf_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_pf_detector_that_raises(
        lambda: PrivacyFilterUnavailableError("service unreachable")
    )]

    with pytest.raises(PrivacyFilterUnavailableError, match="service unreachable"):
        await p.anonymize(["alice"], call_id="pf-fail-closed")


async def test_privacy_filter_unavailable_swallowed_under_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRIVACY_FILTER_FAIL_CLOSED=false → log + degrade to no PF matches.
    The request still goes through (covered by other detectors)."""
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        PrivacyFilterUnavailableError,
    )

    _patch_pf_fail_closed(monkeypatch, False)

    p = Pipeline()
    p._detectors = [_pf_detector_that_raises(
        lambda: PrivacyFilterUnavailableError("service unreachable")
    )]

    # No exception, no matches — the only detector returned [] for this call.
    modified, mapping = await p.anonymize(["alice"], call_id="pf-fail-open")
    assert mapping == {}
    assert modified == ["alice"]


async def test_unexpected_pf_failure_routes_through_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-PrivacyFilterUnavailable crash inside a PF detector must
    still route through PRIVACY_FILTER_FAIL_CLOSED — same asymmetry the
    LLM detector has. Otherwise a programmer error in the PF path
    (torch OOM, tokenizer drift, etc.) would silently drop coverage in
    fail-closed mode."""
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        PrivacyFilterUnavailableError,
    )

    _patch_pf_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_pf_detector_that_raises(
        lambda: RuntimeError("unexpected non-PrivacyFilterUnavailable explosion")
    )]

    with pytest.raises(PrivacyFilterUnavailableError, match="Unexpected failure"):
        await p.anonymize(["alice"], call_id="pf-unexpected-fail")


async def test_pf_and_llm_fail_modes_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators must be able to fail closed on one detector and open
    on the other. Pin that down: PF closed + LLM open → PF errors
    propagate, LLM errors get swallowed."""
    from anonymizer_guardrail.detector.llm import LLMUnavailableError
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        PrivacyFilterUnavailableError,
    )

    # Each per-detector CONFIG is independent now, so the two patches
    # are simply applied in sequence to their respective modules.
    _patch_fail_closed(monkeypatch, False)        # LLM open
    _patch_pf_fail_closed(monkeypatch, True)      # PF closed

    # PF detector raising → propagates (PF is fail-closed).
    p = Pipeline()
    p._detectors = [_pf_detector_that_raises(
        lambda: PrivacyFilterUnavailableError("pf service unreachable")
    )]
    with pytest.raises(PrivacyFilterUnavailableError):
        await p.anonymize(["alice"], call_id="independent-pf")

    # LLM detector raising → swallowed (LLM is fail-open).
    p = Pipeline()
    p._detectors = [_llm_detector_that_raises(
        lambda: LLMUnavailableError("llm api unreachable")
    )]
    modified, mapping = await p.anonymize(["alice"], call_id="independent-llm")
    assert mapping == {}
    assert modified == ["alice"]


async def test_stats_reports_cache_and_concurrency(pipeline: Pipeline) -> None:
    """Pipeline.stats() snapshots vault size, surrogate cache size + cap,
    and per-detector in-flight + cap. Used by /health for ops monitoring."""
    s = pipeline.stats()
    expected_keys = {
        "vault_size",
        "surrogate_cache_size",
        "surrogate_cache_max",
        "llm_in_flight",
        "llm_max_concurrency",
        "pf_in_flight",
        "pf_max_concurrency",
        "gliner_pii_in_flight",
        "gliner_pii_max_concurrency",
    }
    assert expected_keys.issubset(s.keys())
    assert s["vault_size"] == 0
    assert s["llm_in_flight"] == 0
    assert s["pf_in_flight"] == 0
    assert s["gliner_pii_in_flight"] == 0
    assert s["surrogate_cache_max"] >= 1

    # Anonymize a request — surrogate cache should grow, vault should fill.
    await pipeline.anonymize(
        ["mail alice@acmecorp.com from 10.0.0.1"], call_id="stats-test"
    )
    s = pipeline.stats()
    assert s["vault_size"] == 1
    assert s["surrogate_cache_size"] >= 2  # at least email + ip


async def test_llm_in_flight_increments_around_detect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counter must reflect concurrent LLM activity. We block the
    fake detector mid-call, snapshot stats, then unblock it."""
    _patch_fail_closed(monkeypatch, False)
    p = Pipeline()
    started = asyncio.Event()
    finish = asyncio.Event()

    from anonymizer_guardrail.detector.llm import LLMDetector
    bad = LLMDetector.__new__(LLMDetector)
    bad.name = "llm"

    async def slow_detect(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return []

    bad.detect = slow_detect  # type: ignore[method-assign]
    p._detectors = [bad]

    task = asyncio.create_task(p.anonymize(["x"], call_id="inflight"))
    await started.wait()
    assert p.stats()["llm_in_flight"] == 1
    finish.set()
    await task
    assert p.stats()["llm_in_flight"] == 0


async def test_pf_in_flight_increments_around_detect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Privacy-filter counter mirrors the LLM one. Same blocked-detector
    technique; uses RemotePrivacyFilterDetector since instantiation
    doesn't touch torch / the model."""
    _patch_pf_fail_closed(monkeypatch, False)
    p = Pipeline()
    started = asyncio.Event()
    finish = asyncio.Event()

    from anonymizer_guardrail.detector.remote_privacy_filter import (
        RemotePrivacyFilterDetector,
    )
    bad = RemotePrivacyFilterDetector.__new__(RemotePrivacyFilterDetector)
    bad.name = "privacy_filter"

    async def slow_detect(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return []

    bad.detect = slow_detect  # type: ignore[method-assign]
    p._detectors = [bad]

    task = asyncio.create_task(p.anonymize(["x"], call_id="pf-inflight"))
    await started.wait()
    assert p.stats()["pf_in_flight"] == 1
    finish.set()
    await task
    assert p.stats()["pf_in_flight"] == 0


async def test_gliner_pii_in_flight_increments_around_detect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GLiNER-PII counter mirrors the LLM and PF ones. Confirms the
    semaphore wrap is around the detect() call (not just declared
    next to the field)."""
    _patch_gliner_pii_fail_closed(monkeypatch, False)
    p = Pipeline()
    started = asyncio.Event()
    finish = asyncio.Event()

    from anonymizer_guardrail.detector.remote_gliner_pii import (
        RemoteGlinerPIIDetector,
    )
    bad = RemoteGlinerPIIDetector.__new__(RemoteGlinerPIIDetector)
    bad.name = "gliner_pii"

    async def slow_detect(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return []

    bad.detect = slow_detect  # type: ignore[method-assign]
    p._detectors = [bad]

    task = asyncio.create_task(p.anonymize(["x"], call_id="gliner-inflight"))
    await started.wait()
    assert p.stats()["gliner_pii_in_flight"] == 1
    finish.set()
    await task
    assert p.stats()["gliner_pii_in_flight"] == 0


async def test_gliner_pii_unavailable_propagates_under_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GLINER_PII_FAIL_CLOSED=true → a GlinerPIIUnavailableError inside
    the detector propagates so main.py returns BLOCKED. Same risk shape
    as the PF and LLM detectors — silent coverage downgrade is exactly
    what fail-closed exists to prevent."""
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        GlinerPIIUnavailableError,
    )

    _patch_gliner_pii_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_gliner_detector_that_raises(
        lambda: GlinerPIIUnavailableError("service unreachable")
    )]

    with pytest.raises(GlinerPIIUnavailableError, match="service unreachable"):
        await p.anonymize(["alice"], call_id="gliner-fail-closed")


async def test_gliner_pii_unavailable_swallowed_under_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GLINER_PII_FAIL_CLOSED=false → log + degrade to no gliner matches.
    The request still goes through (covered by other detectors)."""
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        GlinerPIIUnavailableError,
    )

    _patch_gliner_pii_fail_closed(monkeypatch, False)

    p = Pipeline()
    p._detectors = [_gliner_detector_that_raises(
        lambda: GlinerPIIUnavailableError("service unreachable")
    )]

    modified, mapping = await p.anonymize(["alice"], call_id="gliner-fail-open")
    assert mapping == {}
    assert modified == ["alice"]


async def test_unexpected_gliner_pii_failure_routes_through_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-GlinerPIIUnavailable crash inside the detector must still
    route through GLINER_PII_FAIL_CLOSED — same asymmetry the LLM and
    PF detectors have. Otherwise a programmer error would silently
    drop coverage in fail-closed mode."""
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        GlinerPIIUnavailableError,
    )

    _patch_gliner_pii_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_gliner_detector_that_raises(
        lambda: RuntimeError("unexpected explosion")
    )]

    with pytest.raises(GlinerPIIUnavailableError, match="Unexpected failure"):
        await p.anonymize(["alice"], call_id="gliner-unexpected-fail")


def test_detector_mode_parses_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`regex,llm` produces a regex detector followed by an llm detector
    — order preserved (it determines _dedup type priority)."""
    from anonymizer_guardrail import pipeline as pipeline_mod
    from anonymizer_guardrail.detector.llm import LLMDetector
    from anonymizer_guardrail.detector.regex import RegexDetector

    fields = {f: getattr(pipeline_mod.config, f) for f in pipeline_mod.config.__dataclass_fields__}
    fields["detector_mode"] = "regex,llm"
    from types import SimpleNamespace
    monkeypatch.setattr(pipeline_mod, "config", SimpleNamespace(**fields))

    detectors = pipeline_mod._build_detectors()
    assert [type(d).__name__ for d in detectors] == ["RegexDetector", "LLMDetector"]


def test_detector_mode_order_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators picking `llm,regex` get LLM type-priority on duplicates."""
    from anonymizer_guardrail import pipeline as pipeline_mod
    from types import SimpleNamespace

    fields = {f: getattr(pipeline_mod.config, f) for f in pipeline_mod.config.__dataclass_fields__}
    fields["detector_mode"] = "llm,regex"
    monkeypatch.setattr(pipeline_mod, "config", SimpleNamespace(**fields))

    detectors = pipeline_mod._build_detectors()
    assert [type(d).__name__ for d in detectors] == ["LLMDetector", "RegexDetector"]


def test_detector_mode_tolerates_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from anonymizer_guardrail import pipeline as pipeline_mod
    from types import SimpleNamespace

    fields = {f: getattr(pipeline_mod.config, f) for f in pipeline_mod.config.__dataclass_fields__}
    fields["detector_mode"] = "  regex ,  llm  "
    monkeypatch.setattr(pipeline_mod, "config", SimpleNamespace(**fields))

    detectors = pipeline_mod._build_detectors()
    assert len(detectors) == 2


def test_detector_mode_dedupes_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`regex,regex` collapses to a single detector and logs a warning."""
    import logging

    from anonymizer_guardrail import pipeline as pipeline_mod
    from types import SimpleNamespace

    fields = {f: getattr(pipeline_mod.config, f) for f in pipeline_mod.config.__dataclass_fields__}
    fields["detector_mode"] = "regex,regex"
    monkeypatch.setattr(pipeline_mod, "config", SimpleNamespace(**fields))

    with caplog.at_level(logging.WARNING, logger="anonymizer.pipeline"):
        detectors = pipeline_mod._build_detectors()
    assert len(detectors) == 1
    assert any("duplicate" in r.message.lower() for r in caplog.records)


def test_detector_mode_both_emits_migration_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Legacy `both` is no longer accepted; the warning must point operators
    at the new syntax so a migration is obvious from the logs."""
    import logging

    from anonymizer_guardrail import pipeline as pipeline_mod
    from types import SimpleNamespace

    fields = {f: getattr(pipeline_mod.config, f) for f in pipeline_mod.config.__dataclass_fields__}
    fields["detector_mode"] = "both"
    monkeypatch.setattr(pipeline_mod, "config", SimpleNamespace(**fields))

    with caplog.at_level(logging.ERROR, logger="anonymizer.pipeline"):
        detectors = pipeline_mod._build_detectors()
    # No detectors built from `both` → falls back to regex.
    assert len(detectors) == 1
    assert type(detectors[0]).__name__ == "RegexDetector"
    assert any(
        "regex,llm" in r.message and "both" in r.message for r in caplog.records
    )


def test_detector_mode_unknown_falls_back_to_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage in DETECTOR_MODE: warn, fall back to regex so the service boots."""
    from anonymizer_guardrail import pipeline as pipeline_mod
    from types import SimpleNamespace

    fields = {f: getattr(pipeline_mod.config, f) for f in pipeline_mod.config.__dataclass_fields__}
    fields["detector_mode"] = "magic-detector"
    monkeypatch.setattr(pipeline_mod, "config", SimpleNamespace(**fields))

    detectors = pipeline_mod._build_detectors()
    assert [type(d).__name__ for d in detectors] == ["RegexDetector"]


async def test_unexpected_llm_failure_swallowed_under_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fail_open=true → LLM exceptions log and fall through to whatever
    the regex layer produced. The text proceeds, no BLOCK."""
    _patch_fail_closed(monkeypatch, False)

    p = Pipeline()
    p._detectors = [_llm_detector_that_raises(lambda: RuntimeError("kaboom"))]

    # With no other detectors and the LLM swallowed, nothing gets
    # anonymized — the text passes through unchanged.
    modified, mapping = await p.anonymize(
        ["alice met bob"], call_id="failopen-test"
    )
    assert modified == ["alice met bob"]
    assert mapping == {}
