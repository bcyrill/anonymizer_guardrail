"""Tests for `DETECTOR_MODE` parsing — comma-separated list,
order preservation, whitespace tolerance, dedup-with-warning, and
unknown-name fallback to regex.

Helpers come from `_pipeline_helpers.py`.
"""

from __future__ import annotations

import pytest

from tests._pipeline_helpers import _patch_detector_mode


def test_detector_mode_parses_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`regex,llm` produces a regex detector followed by an llm detector
    — order preserved (it determines _dedup type priority)."""
    from anonymizer_guardrail import pipeline as pipeline_mod

    _patch_detector_mode(monkeypatch, "regex,llm")
    detectors = pipeline_mod._build_detectors()
    assert [type(d).__name__ for d in detectors] == ["RegexDetector", "LLMDetector"]


def test_detector_mode_order_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators picking `llm,regex` get LLM type-priority on duplicates."""
    from anonymizer_guardrail import pipeline as pipeline_mod

    _patch_detector_mode(monkeypatch, "llm,regex")
    detectors = pipeline_mod._build_detectors()
    assert [type(d).__name__ for d in detectors] == ["LLMDetector", "RegexDetector"]


def test_detector_mode_tolerates_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from anonymizer_guardrail import pipeline as pipeline_mod

    _patch_detector_mode(monkeypatch, "  regex ,  llm  ")
    detectors = pipeline_mod._build_detectors()
    assert len(detectors) == 2


def test_detector_mode_dedupes_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`regex,regex` collapses to a single detector and logs a warning."""
    import logging

    from anonymizer_guardrail import pipeline as pipeline_mod

    _patch_detector_mode(monkeypatch, "regex,regex")
    with caplog.at_level(logging.WARNING, logger="anonymizer.pipeline"):
        detectors = pipeline_mod._build_detectors()
    assert len(detectors) == 1
    assert any("duplicate" in r.message.lower() for r in caplog.records)


def test_detector_mode_unknown_falls_back_to_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage in DETECTOR_MODE: warn, fall back to regex so the service boots."""
    from anonymizer_guardrail import pipeline as pipeline_mod

    _patch_detector_mode(monkeypatch, "magic-detector")
    detectors = pipeline_mod._build_detectors()
    assert [type(d).__name__ for d in detectors] == ["RegexDetector"]
