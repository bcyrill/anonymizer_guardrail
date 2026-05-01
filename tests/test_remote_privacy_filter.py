"""Tests for the RemotePrivacyFilterDetector.

These tests don't require torch — the whole point of the remote
detector is that the heavy ML deps live in a separate process. We
mock httpx to drive the detector through happy paths and every
HTTP-layer failure mode it has to degrade gracefully on.
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

from anonymizer_guardrail.detector import privacy_filter as pf_mod
from anonymizer_guardrail.detector import remote_privacy_filter as rpf_mod
from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterConfig
from anonymizer_guardrail.detector.remote_privacy_filter import (
    PrivacyFilterUnavailableError,
    RemotePrivacyFilterDetector,
)


def _fake_config(**overrides: Any) -> PrivacyFilterConfig:
    """Build a PrivacyFilterConfig with a small test-friendly baseline
    plus per-test overrides. Field names lost the `privacy_filter_`
    prefix when PrivacyFilterConfig moved into the detector module —
    overrides use `url`, `timeout_s`, etc."""
    base = PrivacyFilterConfig(
        url="http://privacy-filter-service:8001",
        timeout_s=5,
    )
    return replace(base, **overrides) if overrides else base


def _ok_response(matches: list[dict[str, Any]]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"matches": matches}
    resp.text = ""
    return resp


@pytest.fixture
def detector(monkeypatch: pytest.MonkeyPatch) -> RemotePrivacyFilterDetector:
    monkeypatch.setattr(pf_mod, "CONFIG", _fake_config())
    return RemotePrivacyFilterDetector(
        url="http://privacy-filter-service:8001", timeout_s=5,
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
    monkeypatch.setattr(pf_mod, "CONFIG", _fake_config(url=""))
    with pytest.raises(RuntimeError, match="PRIVACY_FILTER_URL"):
        RemotePrivacyFilterDetector(url="")


def test_url_trailing_slash_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing slash on PRIVACY_FILTER_URL would otherwise produce
    `host:port//detect` — strip it so callers can't trip on that."""
    monkeypatch.setattr(pf_mod, "CONFIG", _fake_config())
    det = RemotePrivacyFilterDetector(url="http://service:8001/")
    assert det.url == "http://service:8001"


# ── Happy path ──────────────────────────────────────────────────────────────


async def test_empty_or_whitespace_short_circuits(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    assert await detector.detect("") == []
    assert await detector.detect("   \n\t ") == []
    mock_post.assert_not_called()


async def test_parses_matches_from_service_response(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    text = "Email alice@example.com about the meeting with Alice"
    mock_post.return_value = _ok_response([
        {"text": "alice@example.com", "entity_type": "EMAIL_ADDRESS",
         "start": 6, "end": 23, "score": 0.99},
        {"text": "Alice", "entity_type": "PERSON",
         "start": 47, "end": 52, "score": 0.97},
    ])

    matches = await detector.detect(text)

    # POST was issued to /detect with the full text body.
    assert mock_post.call_count == 1
    posted_url = mock_post.call_args.args[0]
    assert posted_url.endswith("/detect")
    assert mock_post.call_args.kwargs["json"] == {"text": text}

    # Both matches preserved with the canonical entity types.
    by_type = {m.entity_type: m.text for m in matches}
    assert by_type["EMAIL_ADDRESS"] == "alice@example.com"
    assert by_type["PERSON"] == "Alice"


async def test_unknown_entity_type_normalized_to_other(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """A future service version emitting a label the guardrail doesn't
    know must fall through to OTHER (via Match.__post_init__) — not
    crash, not propagate the unknown type into the surrogate generator."""
    text = "Surprising token: abc123"
    mock_post.return_value = _ok_response([
        {"text": "abc123", "entity_type": "WIDGET", "start": 18, "end": 24,
         "score": 0.91},
    ])

    matches = await detector.detect(text)
    assert len(matches) == 1
    assert matches[0].entity_type == "OTHER"
    assert matches[0].text == "abc123"


async def test_hallucination_guard_drops_text_not_in_input(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """A buggy / hostile service must not be able to inject arbitrary
    surrogates into the pipeline by claiming a match for a string that
    isn't in the input."""
    mock_post.return_value = _ok_response([
        {"text": "alice@example.com", "entity_type": "EMAIL_ADDRESS",
         "start": 0, "end": 17, "score": 0.99},
        # This one isn't in the source — must be dropped.
        {"text": "evil@attacker.com", "entity_type": "EMAIL_ADDRESS",
         "start": 0, "end": 0, "score": 0.99},
    ])

    matches = await detector.detect("Email alice@example.com please.")
    assert [m.text for m in matches] == ["alice@example.com"]


async def test_empty_matches_field_yields_no_matches(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    mock_post.return_value = _ok_response([])
    assert await detector.detect("nothing sensitive here") == []


# ── Availability failure modes — raise PrivacyFilterUnavailableError ───────
# Transport-layer / non-200 responses raise so the pipeline's
# PRIVACY_FILTER_FAIL_CLOSED policy decides BLOCKED vs fall-back.
# Mirrors the LLMUnavailableError contract — see also the pipeline-level
# tests below that confirm the dispatch flips correctly.


async def test_connect_error_raises_unavailable(
    detector: RemotePrivacyFilterDetector, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(PrivacyFilterUnavailableError, match="Cannot reach"):
        await detector.detect("hello")


async def test_timeout_raises_unavailable(
    detector: RemotePrivacyFilterDetector, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow(*args, **kwargs):
        raise httpx.TimeoutException("read timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", slow)
    with pytest.raises(PrivacyFilterUnavailableError, match="timed out"):
        await detector.detect("hello")


async def test_generic_http_error_raises_unavailable(
    detector: RemotePrivacyFilterDetector, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ReadError / RemoteProtocolError / NetworkError etc. all extend
    httpx.HTTPError. The guard must catch them as a class so a
    novel transport error doesn't escape into the pipeline."""
    async def boom(*args, **kwargs):
        raise httpx.RemoteProtocolError("invalid HTTP framing")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(PrivacyFilterUnavailableError, match="HTTP error"):
        await detector.detect("hello")


async def test_non_200_raises_unavailable(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    bad = MagicMock(spec=httpx.Response)
    bad.status_code = 503
    bad.text = "Service Unavailable"
    mock_post.return_value = bad
    with pytest.raises(PrivacyFilterUnavailableError, match="503"):
        await detector.detect("hello")


# ── Content failure modes — non-fatal, log + return [] ─────────────────────
# A 200 with a malformed body is the service responding badly, not an
# outage. Same split LLMDetector uses.


async def test_non_json_body_degrades_to_empty(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.side_effect = ValueError("Expecting value")
    resp.text = "not json"
    mock_post.return_value = resp
    assert await detector.detect("hello") == []


async def test_malformed_response_shape_degrades_to_empty(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """`matches` field present but the wrong type — drop quietly rather
    than crash. Same warn-and-empty policy as every other HTTP failure."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"matches": "not a list"}
    resp.text = ""
    mock_post.return_value = resp
    assert await detector.detect("hello") == []


async def test_response_with_top_level_list_degrades_to_empty(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """A service that sent a bare list at the top instead of a JSON
    object → defensive: drop quietly. Documents the expected shape."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = ["unexpected", "shape"]
    resp.text = ""
    mock_post.return_value = resp
    assert await detector.detect("hello") == []


# ── Pipeline factory dispatch ───────────────────────────────────────────────


def test_factory_picks_remote_when_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The privacy_filter factory reads CONFIG.url at every
    instantiation, so flipping it flips which detector backs
    DETECTOR_MODE=privacy_filter on the next pipeline build."""
    from anonymizer_guardrail.detector.privacy_filter import (
        PrivacyFilterDetector,
        _privacy_filter_factory,
    )

    monkeypatch.setattr(
        pf_mod, "CONFIG",
        replace(pf_mod.CONFIG, url="http://privacy-filter-service:8001"),
    )

    det = _privacy_filter_factory()
    assert isinstance(det, RemotePrivacyFilterDetector)

    # Now flip the URL off and confirm the factory dispatches to the
    # in-process class. PrivacyFilterDetector's __init__ does heavy
    # work (loads torch + the model), so we mock it out — the test is
    # about *which class the factory picks*, not about constructing it.
    monkeypatch.setattr(pf_mod, "CONFIG", replace(pf_mod.CONFIG, url=""))
    constructed: list[str] = []

    def fake_init(self, *_args, **_kwargs):
        constructed.append("PrivacyFilterDetector")
        self.name = "privacy_filter"

    monkeypatch.setattr(PrivacyFilterDetector, "__init__", fake_init)
    det = _privacy_filter_factory()
    assert isinstance(det, PrivacyFilterDetector)
    assert constructed == ["PrivacyFilterDetector"]
