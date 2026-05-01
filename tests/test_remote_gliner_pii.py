"""Tests for the RemoteGlinerPIIDetector.

These tests don't require torch or the gliner library — the whole
point of the remote detector is that the heavy ML deps live in a
separate process. We mock httpx to drive the detector through happy
paths and every HTTP-layer failure mode it has to degrade gracefully on.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Match the other test files: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import httpx
import pytest

from dataclasses import replace

from anonymizer_guardrail.detector import remote_gliner_pii as gp_mod
from anonymizer_guardrail.detector.remote_gliner_pii import (
    GlinerPIIConfig,
    GlinerPIIUnavailableError,
    RemoteGlinerPIIDetector,
)


def _fake_config(**overrides: Any) -> GlinerPIIConfig:
    """Build a GlinerPIIConfig with a small test-friendly baseline plus
    per-test overrides. Field names lost the `gliner_pii_` prefix when
    GlinerPIIConfig moved into the detector module — overrides use
    `url`, `labels`, `threshold`, etc."""
    base = GlinerPIIConfig(
        url="http://gliner-pii-service:8002",
        timeout_s=5,
        labels="",
        threshold="",
    )
    return replace(base, **overrides) if overrides else base


def _ok_response(matches: list[dict[str, Any]]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"matches": matches}
    resp.text = ""
    return resp


@pytest.fixture
def detector(monkeypatch: pytest.MonkeyPatch) -> RemoteGlinerPIIDetector:
    monkeypatch.setattr(gp_mod, "CONFIG", _fake_config())
    return RemoteGlinerPIIDetector(
        url="http://gliner-pii-service:8002", timeout_s=5,
    )


@pytest.fixture
def mock_post(monkeypatch: pytest.MonkeyPatch):
    """Patch httpx.AsyncClient.post so no network call happens."""
    post = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return post


# ── Construction ────────────────────────────────────────────────────────────


def test_empty_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory in pipeline.py only constructs this class when the
    URL is set. If a caller bypasses the factory, fail loud rather than
    POST to '/detect' with no host."""
    monkeypatch.setattr(gp_mod, "CONFIG", _fake_config(url=""))
    with pytest.raises(RuntimeError, match="GLINER_PII_URL"):
        RemoteGlinerPIIDetector(url="")


def test_url_trailing_slash_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing slash on GLINER_PII_URL would otherwise produce
    `host:port//detect` — strip it so callers can't trip on that."""
    monkeypatch.setattr(gp_mod, "CONFIG", _fake_config())
    det = RemoteGlinerPIIDetector(url="http://service:8002/")
    assert det.url == "http://service:8002"


def test_labels_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """GLINER_PII_LABELS is parsed comma-separated and stripped."""
    monkeypatch.setattr(
        gp_mod, "CONFIG",
        _fake_config(labels=" person , email, ssn  "),
    )
    det = RemoteGlinerPIIDetector(url="http://service:8002")
    assert det.labels == ["person", "email", "ssn"]


def test_empty_labels_means_server_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty GLINER_PII_LABELS → None, which the request body omits the
    field entirely so the service's DEFAULT_LABELS apply."""
    monkeypatch.setattr(gp_mod, "CONFIG", _fake_config())
    det = RemoteGlinerPIIDetector(url="http://service:8002")
    assert det.labels is None


def test_threshold_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gp_mod, "CONFIG", _fake_config(threshold="0.7"),
    )
    det = RemoteGlinerPIIDetector(url="http://service:8002")
    assert det.threshold == 0.7


def test_invalid_threshold_warn_and_fall_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A garbage threshold value warns and behaves as 'use server default'.
    Crashing on a bad threshold would block the detector from loading."""
    monkeypatch.setattr(
        gp_mod, "CONFIG", _fake_config(threshold="not-a-float"),
    )
    import logging
    caplog.set_level(logging.WARNING)
    det = RemoteGlinerPIIDetector(url="http://service:8002")
    assert det.threshold is None
    assert any("not a float" in r.message for r in caplog.records)


# ── Happy path ──────────────────────────────────────────────────────────────


async def test_empty_or_whitespace_short_circuits(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    assert await detector.detect("") == []
    assert await detector.detect("   \n\t ") == []
    mock_post.assert_not_called()


async def test_parses_matches_and_maps_labels(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    """Snake_case gliner labels (`email`, `ssn`, `person`) get normalized
    to canonical ENTITY_TYPES (`EMAIL_ADDRESS`, `NATIONAL_ID`, `PERSON`)."""
    text = "Email alice@example.com about Alice's SSN 123-45-6789"
    mock_post.return_value = _ok_response([
        {"text": "alice@example.com", "entity_type": "email",
         "start": 6, "end": 23, "score": 0.99},
        {"text": "Alice", "entity_type": "person",
         "start": 30, "end": 35, "score": 0.92},
        {"text": "123-45-6789", "entity_type": "ssn",
         "start": 42, "end": 53, "score": 0.97},
    ])

    matches = await detector.detect(text)
    assert {(m.text, m.entity_type) for m in matches} == {
        ("alice@example.com", "EMAIL_ADDRESS"),
        ("Alice", "PERSON"),
        ("123-45-6789", "NATIONAL_ID"),
    }


async def test_unmapped_label_falls_through_to_other(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    """A label the map doesn't know is normalized to OTHER via
    Match.__post_init__. Beats inventing a guardrail entity-type
    speculatively."""
    text = "license plate ABC-123"
    mock_post.return_value = _ok_response([
        {"text": "ABC-123", "entity_type": "vehicle_plate",
         "start": 14, "end": 21, "score": 0.88},
    ])
    matches = await detector.detect(text)
    assert matches[0].entity_type == "OTHER"


async def test_request_includes_labels_and_threshold_when_set(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock,
) -> None:
    """When the detector has labels/threshold configured, they go into
    the request body so the server uses them. Lets operators tune
    per-deployment without redeploying the service."""
    monkeypatch.setattr(
        gp_mod, "CONFIG",
        _fake_config(labels="email,ssn", threshold="0.6"),
    )
    det = RemoteGlinerPIIDetector(url="http://service:8002")
    mock_post.return_value = _ok_response([])

    await det.detect("anything")

    _, call_kwargs = mock_post.call_args
    body = call_kwargs["json"]
    assert body["text"] == "anything"
    assert body["labels"] == ["email", "ssn"]
    assert body["threshold"] == 0.6


async def test_request_omits_labels_when_unset(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    """No client-side labels → no `labels` field in the body, so the
    service's DEFAULT_LABELS apply. Same for threshold. Decouples
    client and server tuning."""
    mock_post.return_value = _ok_response([])
    await detector.detect("hello")

    _, call_kwargs = mock_post.call_args
    body = call_kwargs["json"]
    assert "labels" not in body
    assert "threshold" not in body


# ── Hallucination guard ────────────────────────────────────────────────────


async def test_drops_text_not_in_source(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    """A buggy or malicious service shouldn't be able to inject a
    surrogate for text that isn't actually in the input — the
    replacement step would no-op and the surrogate would leak as
    fake noise downstream."""
    mock_post.return_value = _ok_response([
        {"text": "Bob", "entity_type": "person",
         "start": 0, "end": 3, "score": 0.99},
    ])
    matches = await detector.detect("Alice was here")
    assert matches == []


# ── Availability failures → GlinerPIIUnavailableError ──────────────────────


async def test_connect_error_raises(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    mock_post.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(GlinerPIIUnavailableError, match="Cannot reach"):
        await detector.detect("hello")


async def test_timeout_raises(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    mock_post.side_effect = httpx.TimeoutException("read timeout")
    with pytest.raises(GlinerPIIUnavailableError, match="timed out"):
        await detector.detect("hello")


async def test_transport_error_raises(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    """ReadError / WriteError / RemoteProtocolError all surface as
    HTTPError subclasses; route through GlinerPIIUnavailableError so
    GLINER_PII_FAIL_CLOSED applies."""
    mock_post.side_effect = httpx.ReadError("peer reset")
    with pytest.raises(GlinerPIIUnavailableError, match="HTTP error"):
        await detector.detect("hello")


async def test_non_200_raises(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 503
    resp.text = "service unavailable"
    mock_post.return_value = resp
    with pytest.raises(GlinerPIIUnavailableError, match="503"):
        await detector.detect("hello")


# ── Content errors → log and return [] ─────────────────────────────────────


async def test_non_json_returns_empty(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    """Service is reachable (200) but body isn't JSON. Don't BLOCK over
    a bad payload — log it and proceed with no PII matches from this
    detector. Other detectors still run."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.side_effect = ValueError("not json")
    resp.text = "garbage"
    mock_post.return_value = resp
    assert await detector.detect("hello") == []


async def test_malformed_payload_returns_empty(
    detector: RemoteGlinerPIIDetector, mock_post: AsyncMock,
) -> None:
    """Body is JSON but doesn't have the expected `matches` list shape."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"matches": "not a list"}
    resp.text = ""
    mock_post.return_value = resp
    assert await detector.detect("hello") == []
