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


# ── Generic config-patching ─────────────────────────────────────────────


def _patch_detector_config(
    monkeypatch: pytest.MonkeyPatch,
    module,
    **updates,
) -> None:
    """Patch `module.CONFIG` with arbitrary field overrides. The pipeline
    reads `spec.config.<field>` live, so the patch takes effect on the
    next `Pipeline()` instance.

    The named shims below (`_patch_fail_closed`,
    `_patch_pf_fail_closed`, `_patch_gliner_pii_fail_closed`) are
    one-liners over this generic helper — kept because they convey
    intent at call sites better than a raw module reference does. New
    patches that aren't `fail_closed` should call this helper directly:

        _patch_detector_config(monkeypatch, llm_mod, input_mode="merged")
    """
    monkeypatch.setattr(
        module, "CONFIG",
        module.CONFIG.model_copy(update=updates),
    )


# ── fail_closed flag shims (intent-revealing wrappers) ──────────────────


def _patch_fail_closed(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Flip the LLM detector's fail_closed flag for one test."""
    from anonymizer_guardrail.detector import llm as llm_mod
    _patch_detector_config(monkeypatch, llm_mod, fail_closed=value)


def _patch_pf_fail_closed(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Flip the privacy_filter detector's fail_closed flag for one test."""
    from anonymizer_guardrail.detector import remote_privacy_filter as pf_mod
    _patch_detector_config(monkeypatch, pf_mod, fail_closed=value)


def _patch_gliner_pii_fail_closed(
    monkeypatch: pytest.MonkeyPatch, value: bool,
) -> None:
    """Flip the gliner_pii detector's fail_closed flag for one test."""
    from anonymizer_guardrail.detector import remote_gliner_pii as gp_mod
    _patch_detector_config(monkeypatch, gp_mod, fail_closed=value)


def _patch_detector_mode(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Swap pipeline_mod.config for one with `detector_mode` overridden.
    Distinct from the per-detector helpers above because DETECTOR_MODE
    lives on the central Config, not on a per-detector CONFIG."""
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


def _detector_stub_that_raises(detector_cls, name: str, exc_factory):
    """Build a bare detector instance whose `detect()` raises whatever
    `exc_factory` returns. Generic factory; the per-detector shims
    below pin the class + name for readable call sites."""
    from anonymizer_guardrail.detector.cache_memory import InMemoryDetectionCache

    bad = detector_cls.__new__(detector_cls)
    bad.name = name
    bad._cache = InMemoryDetectionCache(0)

    async def boom(*_args, **_kwargs):
        raise exc_factory()

    bad.detect = boom  # type: ignore[method-assign]
    return bad


def _llm_detector_that_raises(exc_factory):
    """Bare LLMDetector whose detect() raises `exc_factory()`."""
    from anonymizer_guardrail.detector.llm import LLMDetector
    return _detector_stub_that_raises(LLMDetector, "llm", exc_factory)


def _pf_detector_that_raises(exc_factory):
    """Bare RemotePrivacyFilterDetector whose detect() raises
    `exc_factory()`."""
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        RemotePrivacyFilterDetector,
    )
    return _detector_stub_that_raises(
        RemotePrivacyFilterDetector, "privacy_filter", exc_factory,
    )


def _gliner_detector_that_raises(exc_factory):
    """Bare RemoteGlinerPIIDetector whose detect() raises
    `exc_factory()`."""
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        RemoteGlinerPIIDetector,
    )
    return _detector_stub_that_raises(
        RemoteGlinerPIIDetector, "gliner_pii", exc_factory,
    )
