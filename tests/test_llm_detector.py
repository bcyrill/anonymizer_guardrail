"""Tests for the LLM detector's input handling.

The guardrail invariant under test: nothing in the input may silently bypass
detection because of length. We do that by *refusing* oversized inputs (so
FAIL_CLOSED in the pipeline decides), never by truncating.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Match test_pipeline.py: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import httpx
import pytest

from anonymizer_guardrail.detector import llm as llm_mod
from anonymizer_guardrail.detector.llm import LLMDetector, LLMUnavailableError


def _fake_config(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        llm_api_base="http://test",
        llm_api_key="",
        llm_model="test",
        llm_timeout_s=5,
        llm_max_chars=100,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _ok_response(entities: list[dict[str, str]]) -> MagicMock:
    """Build a mock httpx.Response shaped like an OpenAI chat completion."""
    import json

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [
            {"message": {"content": json.dumps({"entities": entities})}}
        ]
    }
    resp.text = ""
    return resp


@pytest.fixture
def detector(monkeypatch: pytest.MonkeyPatch) -> LLMDetector:
    monkeypatch.setattr(llm_mod, "config", _fake_config())
    return LLMDetector(
        api_base="http://test", api_key="", model="test", timeout_s=5
    )


@pytest.fixture
def mock_post(monkeypatch: pytest.MonkeyPatch):
    """Patch httpx.AsyncClient.post so no network call happens."""
    post = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return post


async def test_empty_or_whitespace_short_circuits(
    detector: LLMDetector, mock_post: AsyncMock
) -> None:
    assert await detector.detect("") == []
    assert await detector.detect("   \n\t ") == []
    mock_post.assert_not_called()


async def test_single_call_for_input_within_cap(
    detector: LLMDetector, mock_post: AsyncMock
) -> None:
    """Normal input → one HTTP call carrying the full text, full results returned."""
    mock_post.return_value = _ok_response(
        [{"text": "alice", "type": "PERSON"}, {"text": "10.0.0.1", "type": "IP_ADDRESS"}]
    )

    result = await detector.detect("hello alice at 10.0.0.1")

    assert mock_post.call_count == 1
    # The full text is sent verbatim as the user message.
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["messages"][-1]["content"] == "hello alice at 10.0.0.1"

    texts = sorted(m.text for m in result)
    assert texts == ["10.0.0.1", "alice"]


async def test_oversized_input_refused_without_http_call(
    detector: LLMDetector, mock_post: AsyncMock
) -> None:
    """The bug we're fixing: oversized inputs MUST NOT be silently truncated.
    They must raise so FAIL_CLOSED decides whether to block or fall back."""
    text = "a" * 101  # cap is 100

    with pytest.raises(LLMUnavailableError, match="too large"):
        await detector.detect(text)

    mock_post.assert_not_called()


async def test_input_at_exactly_cap_is_accepted(
    detector: LLMDetector, mock_post: AsyncMock
) -> None:
    """Boundary: len == cap is allowed, len > cap is refused."""
    mock_post.return_value = _ok_response([])

    text = "a" * 100  # exactly at cap
    await detector.detect(text)

    assert mock_post.call_count == 1


async def test_hallucinated_entity_not_in_input_is_dropped(
    detector: LLMDetector, mock_post: AsyncMock
) -> None:
    """If the model invents a substring that isn't actually in the input, drop
    it — we can't anonymize what isn't there. Real ones still come through."""
    mock_post.return_value = _ok_response(
        [
            {"text": "alice", "type": "PERSON"},
            {"text": "ghost-entity-not-in-text", "type": "PERSON"},
        ]
    )

    result = await detector.detect("hello alice")
    assert [m.text for m in result] == ["alice"]


async def test_connect_error_becomes_llm_unavailable(
    detector: LLMDetector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Network failures route through LLMUnavailableError so FAIL_CLOSED applies."""
    async def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)

    with pytest.raises(LLMUnavailableError, match="Cannot reach LLM"):
        await detector.detect("hello")


async def test_non_200_response_becomes_llm_unavailable(
    detector: LLMDetector, mock_post: AsyncMock
) -> None:
    bad = MagicMock(spec=httpx.Response)
    bad.status_code = 503
    bad.text = "upstream overloaded"
    mock_post.return_value = bad

    with pytest.raises(LLMUnavailableError, match="503"):
        await detector.detect("hello")