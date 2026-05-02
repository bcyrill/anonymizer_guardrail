"""Tests for `Pipeline.stats()` — the snapshot consumed by `/health`.

Covers the key set, the per-detector in-flight counters incrementing
around live `detect()` calls, and the per-detector cache stats keys
that appear once a detector opts into `has_cache=True`.

The `pipeline` fixture comes from conftest.py; helpers come from
`_pipeline_helpers.py`.
"""

from __future__ import annotations

import asyncio

import pytest

from anonymizer_guardrail.pipeline import Pipeline
from tests._pipeline_helpers import (
    _patch_fail_closed,
    _patch_gliner_pii_fail_closed,
    _patch_pf_fail_closed,
)


async def test_stats_reports_cache_and_concurrency(pipeline: Pipeline) -> None:
    """Pipeline.stats() snapshots vault size, surrogate cache size + cap,
    per-detector in-flight + cap, and per-detector result-cache stats.
    Used by /health for ops monitoring."""
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
        # Per-detector result cache. All four keys must be present
        # for every detector that opts into has_cache=True even when
        # the cache is disabled (cache_max_size=0) so dashboards can
        # pin to stable names.
        "llm_cache_size",
        "llm_cache_max",
        "llm_cache_hits",
        "llm_cache_misses",
        "pf_cache_size",
        "pf_cache_max",
        "pf_cache_hits",
        "pf_cache_misses",
        "gliner_pii_cache_size",
        "gliner_pii_cache_max",
        "gliner_pii_cache_hits",
        "gliner_pii_cache_misses",
    }
    assert expected_keys.issubset(s.keys())
    assert s["vault_size"] == 0
    assert s["llm_in_flight"] == 0
    assert s["pf_in_flight"] == 0
    assert s["gliner_pii_in_flight"] == 0
    assert s["surrogate_cache_max"] >= 1
    # Default config disables LLM result caching (cache_max_size=0).
    assert s["llm_cache_max"] == 0
    assert s["llm_cache_size"] == 0

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

    from anonymizer_guardrail.detector.cache_memory import InMemoryDetectionCache
    from anonymizer_guardrail.detector.llm import LLMDetector
    bad = LLMDetector.__new__(LLMDetector)
    bad.name = "llm"
    # Pipeline.stats() reads cache_stats() on detectors with has_cache=True;
    # a disabled cache satisfies that without enabling caching for the test.
    bad._cache = InMemoryDetectionCache(0)

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

    from anonymizer_guardrail.detector.cache_memory import InMemoryDetectionCache
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        RemotePrivacyFilterDetector,
    )
    bad = RemotePrivacyFilterDetector.__new__(RemotePrivacyFilterDetector)
    bad.name = "privacy_filter"
    bad._cache = InMemoryDetectionCache(0)

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

    from anonymizer_guardrail.detector.cache_memory import InMemoryDetectionCache
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        RemoteGlinerPIIDetector,
    )
    bad = RemoteGlinerPIIDetector.__new__(RemoteGlinerPIIDetector)
    bad.name = "gliner_pii"
    bad._cache = InMemoryDetectionCache(0)

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
