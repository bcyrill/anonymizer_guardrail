"""Tests for the RemotePrivacyFilterDetector.

These tests don't require torch — the whole point of the remote
detector is that the heavy ML deps live in a separate process. We
mock httpx to drive the detector through happy paths and every
HTTP-layer failure mode it has to degrade gracefully on.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Match the other test files: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import httpx
import pytest

from anonymizer_guardrail.detector import remote_privacy_filter as pf_mod
from anonymizer_guardrail.detector.remote_privacy_filter import (
    PrivacyFilterConfig,
    PrivacyFilterUnavailableError,
    RemotePrivacyFilterDetector,
)


def _fake_config(**overrides: Any) -> PrivacyFilterConfig:
    """Build a PrivacyFilterConfig with a small test-friendly baseline
    plus per-test overrides."""
    base = PrivacyFilterConfig(
        url="http://privacy-filter-service:8001",
        timeout_s=5,
    )
    return base.model_copy(update=overrides) if overrides else base


def _ok_response(spans: list[dict[str, Any]]) -> MagicMock:
    """Build a fake 200 OK with the wire format the service emits —
    `{"spans": [{label, start, end, text}, ...]}`. Label translation
    and merge/split run client-side in `_to_matches`."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"spans": spans}
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
        {"label": "private_email", "start": 6, "end": 23,
         "text": "alice@example.com"},
        {"label": "private_person", "start": 47, "end": 52,
         "text": "Alice"},
    ])

    matches = await detector.detect(text)

    # POST was issued to /detect with the full text body.
    assert mock_post.call_count == 1
    posted_url = mock_post.call_args.args[0]
    assert posted_url.endswith("/detect")
    assert mock_post.call_args.kwargs["json"] == {"text": text}

    # Raw `private_*` labels translate to canonical entity types
    # client-side via `_to_matches`.
    by_type = {m.entity_type: m.text for m in matches}
    assert by_type["EMAIL_ADDRESS"] == "alice@example.com"
    assert by_type["PERSON"] == "Alice"


async def test_unknown_label_normalized_to_other(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """A future service version emitting a label the guardrail doesn't
    know must fall through to OTHER — not crash, not propagate the
    unknown type into the surrogate generator."""
    text = "Surprising token: abc123"
    mock_post.return_value = _ok_response([
        {"label": "made_up_widget", "start": 18, "end": 24, "text": "abc123"},
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
    isn't in the input. The guard runs inside `_to_matches`."""
    mock_post.return_value = _ok_response([
        {"label": "private_email", "start": 6, "end": 23,
         "text": "alice@example.com"},
        # Bad offsets + the .text fallback string isn't in source —
        # `_to_matches` drops it silently.
        {"label": "private_email", "start": -1, "end": -1,
         "text": "evil@attacker.com"},
    ])

    matches = await detector.detect("Email alice@example.com please.")
    assert [m.text for m in matches] == ["alice@example.com"]


async def test_empty_spans_field_yields_no_matches(
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


# ── Whole-response content errors → raise PrivacyFilterUnavailableError ───
# A 200 with an unparseable / wrong-shape body means the service replied
# but didn't say anything we can act on. fail_closed=True is explicitly
# the "block rather than risk leakage" posture, so soft-failing these
# would silently violate it. Per-entry malformed entries (covered above
# by the hallucination-guard test) still drop quietly — those are local
# to one entity, not the whole response.


async def test_non_json_body_raises_unavailable(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.side_effect = ValueError("Expecting value")
    resp.text = "not json"
    mock_post.return_value = resp
    with pytest.raises(PrivacyFilterUnavailableError, match="non-JSON"):
        await detector.detect("hello")


async def test_malformed_response_shape_raises_unavailable(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """`spans` field present but the wrong type — schema violation,
    raise so PRIVACY_FILTER_FAIL_CLOSED applies."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"spans": "not a list"}
    resp.text = ""
    mock_post.return_value = resp
    with pytest.raises(PrivacyFilterUnavailableError, match="spans"):
        await detector.detect("hello")


async def test_response_with_top_level_list_raises_unavailable(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """A service that sent a bare list at the top instead of a JSON
    object — schema violation, raise."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = ["unexpected", "shape"]
    resp.text = ""
    mock_post.return_value = resp
    with pytest.raises(PrivacyFilterUnavailableError, match="isn't an object"):
        await detector.detect("hello")


async def test_per_entry_malformed_entries_drop_silently(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """Per-entry failures invalidate ONE entity; the rest of the response
    is still good. Different from whole-response failures, which raise."""
    mock_post.return_value = _ok_response([
        "not a dict",                                  # bad entry → drop
        {"label": "private_email", "start": 6, "end": 23,
         "text": "alice@example.com"},
        {"label": "private_person", "start": -1, "end": -1,
         "text": "ghost"},                             # text not in source → drop
    ])
    matches = await detector.detect("Email alice@example.com please")
    assert [m.text for m in matches] == ["alice@example.com"]


# ── Pipeline factory dispatch ───────────────────────────────────────────────


def test_factory_returns_remote_when_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The privacy_filter factory always returns the remote detector
    (the in-process variant has been removed). It reads CONFIG.url at
    instantiation; the URL must point at a running privacy-filter-service."""
    from anonymizer_guardrail.detector.remote_privacy_filter import _privacy_filter_factory

    monkeypatch.setattr(
        pf_mod, "CONFIG",
        pf_mod.CONFIG.model_copy(update={"url": "http://privacy-filter-service:8001"}),
    )

    det = _privacy_filter_factory()
    assert isinstance(det, RemotePrivacyFilterDetector)


# ── Result caching ──────────────────────────────────────────────────────────
# PRIVACY_FILTER_CACHE_MAX_SIZE > 0 enables a per-detector LRU keyed
# on `(text,)` — PF has no per-call overrides today, so input text
# is the only thing that varies.


def _cached_pf_detector(monkeypatch: pytest.MonkeyPatch, max_size: int) -> RemotePrivacyFilterDetector:
    monkeypatch.setattr(pf_mod, "CONFIG", _fake_config(cache_max_size=max_size))
    return RemotePrivacyFilterDetector(
        url="http://privacy-filter-service:8001", timeout_s=5,
    )


async def test_pf_cache_disabled_by_default_every_call_hits_http(
    detector: RemotePrivacyFilterDetector, mock_post: AsyncMock,
) -> None:
    """Default cache_max_size=0 must keep the pre-caching behaviour:
    repeat detect() calls always reach the mocked HTTP layer."""
    mock_post.return_value = _ok_response([
        {"label": "private_email", "start": 6, "end": 23,
         "text": "alice@example.com"},
    ])

    await detector.detect("Email alice@example.com please")
    await detector.detect("Email alice@example.com please")

    assert mock_post.call_count == 2
    assert detector._cache.enabled is False


async def test_pf_cache_hit_skips_http_call(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock,
) -> None:
    """With caching on, identical inputs reach HTTP once; subsequent
    calls return the cached matches without hitting the wire."""
    detector = _cached_pf_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([
        {"label": "private_email", "start": 6, "end": 23,
         "text": "alice@example.com"},
    ])

    r1 = await detector.detect("Email alice@example.com please")
    r2 = await detector.detect("Email alice@example.com please")
    r3 = await detector.detect("Email alice@example.com please")

    assert mock_post.call_count == 1
    assert [m.text for m in r1] == ["alice@example.com"]
    assert [m.text for m in r2] == ["alice@example.com"]
    assert [m.text for m in r3] == ["alice@example.com"]
    s = detector.cache_stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert s["size"] == 1


async def test_pf_cache_keys_separate_per_text(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock,
) -> None:
    """Different inputs must take different cache slots; otherwise
    two unrelated requests would collapse into one fake result."""
    detector = _cached_pf_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([])

    await detector.detect("first input")
    await detector.detect("second input")
    await detector.detect("first input")  # hit
    await detector.detect("second input")  # hit

    assert mock_post.call_count == 2
    assert detector.cache_stats()["size"] == 2


async def test_pf_cache_eviction_recomputes(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock,
) -> None:
    """Cap=1 → inserting a second key evicts the first; coming back
    to the original input pays for the round-trip again."""
    detector = _cached_pf_detector(monkeypatch, max_size=1)
    mock_post.return_value = _ok_response([])

    await detector.detect("text A")  # miss
    await detector.detect("text B")  # miss, evicts A
    await detector.detect("text A")  # miss again

    assert mock_post.call_count == 3
    s = detector.cache_stats()
    assert s["misses"] == 3
    assert s["hits"] == 0


async def test_pf_cache_hit_returns_independent_list(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock,
) -> None:
    """Mutating a list returned on a cache hit must not poison
    subsequent hits — the cache stores an immutable tuple internally."""
    detector = _cached_pf_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([
        {"label": "private_email", "start": 6, "end": 23,
         "text": "alice@example.com"},
    ])

    r1 = await detector.detect("Email alice@example.com please")
    r1.clear()  # poison attempt

    r2 = await detector.detect("Email alice@example.com please")
    assert [m.text for m in r2] == ["alice@example.com"]
