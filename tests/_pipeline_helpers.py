"""Shared helpers for the test_pipeline_*.py split files.

Underscore prefix keeps pytest from collecting this as a test module.
Imports are intentionally lazy (inside each helper) so a module that
only needs the fail_closed shim doesn't pay for importing every
detector module.

If you're adding a new helper that several of the split files need,
put it here. Per-file helpers should stay in their own file.
"""

from __future__ import annotations

import pytest


# ── fail_closed flag patches ────────────────────────────────────────────


def _patch_fail_closed(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Flip the LLM detector's fail_closed flag for one test by patching
    `llm_mod.CONFIG`. The pipeline reads `spec.config.fail_closed` live,
    so the patch takes effect immediately."""
    from anonymizer_guardrail.detector import llm as llm_mod

    monkeypatch.setattr(
        llm_mod, "CONFIG",
        llm_mod.CONFIG.model_copy(update={"fail_closed": value}),
    )


def _patch_pf_fail_closed(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Same shim for the privacy_filter detector's fail_closed flag."""
    from anonymizer_guardrail.detector import remote_privacy_filter as pf_mod

    monkeypatch.setattr(
        pf_mod, "CONFIG",
        pf_mod.CONFIG.model_copy(update={"fail_closed": value}),
    )


def _patch_gliner_pii_fail_closed(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Same shim for the gliner_pii detector's fail_closed flag."""
    from anonymizer_guardrail.detector import remote_gliner_pii as gp_mod

    monkeypatch.setattr(
        gp_mod, "CONFIG",
        gp_mod.CONFIG.model_copy(update={"fail_closed": value}),
    )


def _patch_detector_mode(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Swap pipeline_mod.config for one with `detector_mode` overridden.
    Uses Pydantic's `model_copy(update=...)` — the central Config moved
    from a dataclass to BaseSettings during the pydantic-settings
    migration, so this is now the canonical way to override one field
    while keeping every other field at its environment-derived value."""
    from anonymizer_guardrail import pipeline as pipeline_mod

    monkeypatch.setattr(
        pipeline_mod, "config",
        pipeline_mod.config.model_copy(update={"detector_mode": value}),
    )


# ── Detector stubs that raise ───────────────────────────────────────────
# Each stub bypasses the real httpx wiring by constructing the detector
# class via `__new__` and replacing its `detect` method. Pipeline.stats()
# iterates SPECS_WITH_CACHE and calls `cache_stats()` on every active
# detector with has_cache=True, so each stub also attaches a disabled
# cache so the stats path doesn't trip on a missing attribute. The fake
# `detect()` bypasses the cache anyway by overriding the public method
# outright — the cache only matters for the stats lookup.


def _llm_detector_that_raises(exc_factory):
    """A bare LLMDetector instance whose detect() raises whatever
    exc_factory returns."""
    from anonymizer_guardrail.detector.cache import InMemoryDetectionCache
    from anonymizer_guardrail.detector.llm import LLMDetector

    bad = LLMDetector.__new__(LLMDetector)
    bad.name = "llm"
    bad._cache = InMemoryDetectionCache(0)

    async def boom(*_args, **_kwargs):
        raise exc_factory()

    bad.detect = boom  # type: ignore[method-assign]
    return bad


def _pf_detector_that_raises(exc_factory):
    """A bare RemotePrivacyFilterDetector instance whose detect() raises
    whatever exc_factory returns."""
    from anonymizer_guardrail.detector.cache import InMemoryDetectionCache
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        RemotePrivacyFilterDetector,
    )

    bad = RemotePrivacyFilterDetector.__new__(RemotePrivacyFilterDetector)
    bad.name = "privacy_filter"
    bad._cache = InMemoryDetectionCache(0)

    async def boom(*_args, **_kwargs):
        raise exc_factory()

    bad.detect = boom  # type: ignore[method-assign]
    return bad


def _gliner_detector_that_raises(exc_factory):
    """A bare RemoteGlinerPIIDetector instance whose detect() raises
    whatever exc_factory returns."""
    from anonymizer_guardrail.detector.cache import InMemoryDetectionCache
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        RemoteGlinerPIIDetector,
    )

    bad = RemoteGlinerPIIDetector.__new__(RemoteGlinerPIIDetector)
    bad.name = "gliner_pii"
    bad._cache = InMemoryDetectionCache(0)

    async def boom(*_args, **_kwargs):
        raise exc_factory()

    bad.detect = boom  # type: ignore[method-assign]
    return bad
