"""Tests for the LLM detector's input handling.

The guardrail invariant under test: nothing in the input may silently bypass
detection because of length. We do that by *refusing* oversized inputs (so
LLM_FAIL_CLOSED in the pipeline decides), never by truncating.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

# Match test_pipeline.py: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import httpx
import pytest

from anonymizer_guardrail.detector import llm as llm_mod
from anonymizer_guardrail.detector.llm import LLMDetector, LLMUnavailableError


def _fake_config(**overrides: object) -> "llm_mod.LLMConfig":
    """Build an LLMConfig from a small test-friendly baseline plus any
    overrides. Tests apply via
    `monkeypatch.setattr(llm_mod, "CONFIG", _fake_config(...))`.
    Field names lost the `llm_` prefix when LLMConfig moved into the
    detector module — overrides use the new names (e.g. `api_base`,
    `system_prompt_path`, `max_chars`)."""
    base = llm_mod.LLMConfig(
        api_base="http://test",
        api_key="",
        model="test",
        timeout_s=5,
        max_chars=100,
        system_prompt_path="",
    )
    return base.model_copy(update=overrides) if overrides else base


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
    monkeypatch.setattr(llm_mod, "CONFIG", _fake_config())
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
        [{"text": "alice", "type": "PERSON"}, {"text": "10.0.0.1", "type": "IPV4_ADDRESS"}]
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
    They must raise so LLM_FAIL_CLOSED decides whether to block or fall back."""
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


async def test_read_error_routes_through_llm_unavailable(
    detector: LLMDetector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guardrail-bypass we're guarding against: any httpx error must
    become LLMUnavailableError so FAIL_CLOSED fires. Bare exceptions used
    to leak past the pipeline's defensive handler and silently ship
    unredacted text upstream."""
    async def boom(*args, **kwargs):
        raise httpx.ReadError("connection reset by peer")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)

    with pytest.raises(LLMUnavailableError, match="HTTP error"):
        await detector.detect("hello")


async def test_remote_protocol_error_routes_through_llm_unavailable(
    detector: LLMDetector, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args, **kwargs):
        raise httpx.RemoteProtocolError("invalid HTTP framing")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)

    with pytest.raises(LLMUnavailableError, match="HTTP error"):
        await detector.detect("hello")


# ── Whole-response content errors on a 200 → raise LLMUnavailableError ────
# A 200 OK whose body or content payload we can't parse means the
# backend replied but said nothing actionable. Soft-failing to [] under
# LLM_FAIL_CLOSED=True would silently let unredacted text through —
# routing through the typed error means the policy decides.


async def test_non_json_envelope_raises_unavailable(
    detector: LLMDetector, mock_post: AsyncMock,
) -> None:
    """200 OK but the outer body isn't JSON at all (proxy returning
    HTML, etc.). Must raise so LLM_FAIL_CLOSED applies."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.side_effect = ValueError("Expecting value")
    resp.text = "<html>oops</html>"
    mock_post.return_value = resp
    with pytest.raises(LLMUnavailableError, match="non-JSON"):
        await detector.detect("hello")


async def test_unexpected_envelope_shape_raises_unavailable(
    detector: LLMDetector, mock_post: AsyncMock,
) -> None:
    """JSON body but no `choices[0].message.content` path — backend
    misbehaving (or LiteLLM's response-shape contract drifted)."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"unexpected": "shape"}
    resp.text = ""
    mock_post.return_value = resp
    with pytest.raises(LLMUnavailableError, match="unexpected response shape"):
        await detector.detect("hello")


async def test_non_json_content_raises_unavailable(
    detector: LLMDetector, mock_post: AsyncMock,
) -> None:
    """Envelope is fine but the model's `content` payload isn't JSON
    we can extract. The LLM is misbehaving — block under fail-closed
    rather than silently treat as 'no entities'."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": "this is just plain prose, no JSON anywhere"}}]
    }
    resp.text = ""
    mock_post.return_value = resp
    with pytest.raises(LLMUnavailableError, match="non-JSON content"):
        await detector.detect("hello")


async def test_content_top_level_not_object_raises_unavailable(
    detector: LLMDetector, mock_post: AsyncMock,
) -> None:
    """Content parses as JSON but the top-level value is a list, not
    an object — schema violation."""
    import json as _json

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": _json.dumps(["a", "b"])}}]
    }
    resp.text = ""
    mock_post.return_value = resp
    with pytest.raises(LLMUnavailableError, match="not an object"):
        await detector.detect("hello")


async def test_content_entities_field_not_a_list_raises_unavailable(
    detector: LLMDetector, mock_post: AsyncMock,
) -> None:
    """`entities` field exists but is the wrong type."""
    import json as _json

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": _json.dumps({"entities": "not a list"})}}]
    }
    resp.text = ""
    mock_post.return_value = resp
    with pytest.raises(LLMUnavailableError, match="not a list"):
        await detector.detect("hello")


async def test_content_missing_entities_treated_as_empty(
    detector: LLMDetector, mock_post: AsyncMock,
) -> None:
    """`entities` missing entirely → the model said 'no PII' in the
    object form. That's a valid, parseable response — return []."""
    import json as _json

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": _json.dumps({"other_field": 42})}}]
    }
    resp.text = ""
    mock_post.return_value = resp
    assert await detector.detect("hello") == []


async def test_per_entry_malformed_entries_drop_silently(
    detector: LLMDetector, mock_post: AsyncMock,
) -> None:
    """Per-entry failures invalidate ONE entity; the rest of the
    `entities` list is still good. Distinct from whole-response
    failures, which raise."""
    mock_post.return_value = _ok_response(
        [
            "not a dict",                              # bad entry → drop
            {"text": "alice", "type": "PERSON"},
            {"type": "PERSON"},                        # missing text → drop
        ]
    )
    result = await detector.detect("hello alice")
    assert [m.text for m in result] == ["alice"]


def test_default_system_prompt_loads_from_bundled_file() -> None:
    """The bundled prompt must be readable at import-time — if hatchling
    stops packaging the prompts/ directory, this fails loudly."""
    from anonymizer_guardrail.detector.llm import _SYSTEM_PROMPT

    # A few stable phrases from the prompt; if anyone rewrites it heavily,
    # update this assertion rather than removing it — the point is to prove
    # we loaded a real prompt, not an empty file.
    assert "privacy guardian" in _SYSTEM_PROMPT
    assert "entities" in _SYSTEM_PROMPT


def test_override_path_loads_custom_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM PROMPT CONTENTS", encoding="utf-8")
    monkeypatch.setattr(
        llm_mod, "CONFIG", _fake_config(system_prompt_path=str(custom))
    )

    assert llm_mod._load_system_prompt() == "CUSTOM PROMPT CONTENTS"


def test_override_path_missing_file_raises_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typos in LLM_SYSTEM_PROMPT_PATH should crash loudly, not fall back
    silently to the bundled prompt — operator intent is unambiguous."""
    monkeypatch.setattr(
        llm_mod, "CONFIG", _fake_config(system_prompt_path="/no/such/file.md")
    )
    with pytest.raises(RuntimeError, match="could not be read"):
        llm_mod._load_system_prompt()


def test_empty_prompt_file_rejected(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0-byte/whitespace-only prompt would make the LLM return [] every
    time — that's silent regex-only mode. Reject at load."""
    empty = tmp_path / "empty.md"
    empty.write_text("   \n\n  \t\n", encoding="utf-8")
    monkeypatch.setattr(
        llm_mod, "CONFIG", _fake_config(system_prompt_path=str(empty))
    )
    with pytest.raises(RuntimeError, match="empty"):
        llm_mod._load_system_prompt()


def test_bundled_prefix_resolves_to_packaged_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bundled:llm_pentest.md` should load the file shipped under
    prompts/, regardless of the Python install location."""
    monkeypatch.setattr(
        llm_mod, "CONFIG", _fake_config(system_prompt_path="bundled:llm_pentest.md")
    )
    text = llm_mod._load_system_prompt()
    # Stable phrase from the DontFeedTheAI prompt.
    assert "data privacy guardian" in text


def test_bundled_prefix_unknown_name_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_mod, "CONFIG", _fake_config(system_prompt_path="bundled:does_not_exist.md")
    )
    with pytest.raises(RuntimeError, match="not found in bundled prompts"):
        llm_mod._load_system_prompt()


def test_bundled_prefix_rejects_path_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bundled:` is for bare filenames only — disallow path traversal."""
    monkeypatch.setattr(
        llm_mod, "CONFIG", _fake_config(system_prompt_path="bundled:../etc/passwd")
    )
    with pytest.raises(RuntimeError, match="bare filename"):
        llm_mod._load_system_prompt()


async def test_per_call_api_key_overrides_configured_key(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """A per-call api_key (forwarded user key) wins over the configured one."""
    monkeypatch.setattr(llm_mod, "CONFIG", _fake_config())
    detector = LLMDetector(
        api_base="http://test", api_key="configured-key", model="test", timeout_s=5
    )
    mock_post.return_value = _ok_response([])

    await detector.detect("hello", api_key="forwarded-key")

    sent_headers = mock_post.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer forwarded-key"


async def test_per_call_api_key_none_falls_back_to_configured(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """No forwarded key → configured key is still used."""
    monkeypatch.setattr(llm_mod, "CONFIG", _fake_config())
    detector = LLMDetector(
        api_base="http://test", api_key="configured-key", model="test", timeout_s=5
    )
    mock_post.return_value = _ok_response([])

    await detector.detect("hello")  # api_key omitted

    sent_headers = mock_post.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer configured-key"


async def test_no_key_anywhere_omits_authorization_header(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """Both keys empty → no Authorization header sent (some local backends need this)."""
    monkeypatch.setattr(llm_mod, "CONFIG", _fake_config())
    detector = LLMDetector(
        api_base="http://test", api_key="", model="test", timeout_s=5
    )
    mock_post.return_value = _ok_response([])

    await detector.detect("hello")

    assert "Authorization" not in mock_post.call_args.kwargs["headers"]


# ── Result caching ──────────────────────────────────────────────────────
# LLM_CACHE_MAX_SIZE > 0 enables a per-detector LRU keyed on
# (text, effective_model, effective_prompt_name). api_key is
# deliberately NOT in the key — same input + prompt + model → same
# detection regardless of which user authenticated the call.


def _cached_detector(monkeypatch: pytest.MonkeyPatch, max_size: int) -> LLMDetector:
    monkeypatch.setattr(
        llm_mod, "CONFIG", _fake_config(cache_max_size=max_size)
    )
    return LLMDetector(
        api_base="http://test", api_key="", model="test", timeout_s=5
    )


async def test_cache_disabled_by_default_every_call_hits_http(
    detector: LLMDetector, mock_post: AsyncMock
) -> None:
    """Default cache_max_size=0 must keep behaviour identical to pre-caching:
    repeat detect() calls always reach the mocked HTTP layer."""
    mock_post.return_value = _ok_response([{"text": "alice", "type": "PERSON"}])

    await detector.detect("hello alice")
    await detector.detect("hello alice")
    await detector.detect("hello alice")

    assert mock_post.call_count == 3
    assert detector._cache.enabled is False
    assert detector.cache_stats()["hits"] == 0


async def test_cache_hit_skips_http_call(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """With caching on, identical inputs reach HTTP once; subsequent
    calls return the cached matches without hitting the wire."""
    detector = _cached_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([{"text": "alice", "type": "PERSON"}])

    r1 = await detector.detect("hello alice")
    r2 = await detector.detect("hello alice")
    r3 = await detector.detect("hello alice")

    assert mock_post.call_count == 1
    assert [m.text for m in r1] == ["alice"]
    assert [m.text for m in r2] == ["alice"]
    assert [m.text for m in r3] == ["alice"]
    s = detector.cache_stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert s["size"] == 1


async def test_cache_hit_returns_independent_list(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """Mutating the list returned on a cache hit must not poison subsequent
    hits — the cache stores an immutable tuple and copies on return."""
    detector = _cached_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([{"text": "alice", "type": "PERSON"}])

    r1 = await detector.detect("hello alice")
    r1.clear()  # poison attempt

    r2 = await detector.detect("hello alice")
    assert [m.text for m in r2] == ["alice"]


async def test_cache_keys_separate_per_model_override(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """Different `model` overrides must take different cache slots —
    a request that asked for a stricter detection model should not
    receive matches computed under the default."""
    detector = _cached_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([{"text": "alice", "type": "PERSON"}])

    await detector.detect("hello alice")                         # default model
    await detector.detect("hello alice", model="other-model")    # different slot
    await detector.detect("hello alice", model="other-model")    # hits the second slot

    assert mock_post.call_count == 2
    assert detector.cache_stats()["size"] == 2


async def test_cache_keys_separate_per_prompt_override(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock, tmp_path
) -> None:
    """Different `prompt_name` overrides → different cache slots,
    same as model overrides."""
    # Stub the registry so a known prompt_name resolves successfully.
    monkeypatch.setattr(
        llm_mod, "_SYSTEM_PROMPT_REGISTRY", {"strict": "STRICT PROMPT BODY"}
    )
    detector = _cached_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([{"text": "alice", "type": "PERSON"}])

    await detector.detect("hello alice")                              # default prompt
    await detector.detect("hello alice", prompt_name="strict")        # different slot
    await detector.detect("hello alice", prompt_name="strict")        # hits second slot

    assert mock_post.call_count == 2
    assert detector.cache_stats()["size"] == 2


async def test_cache_default_and_explicit_default_share_slot(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """`prompt_name=None` and `prompt_name="default"` must hit the same
    cache slot — both resolve to the configured default prompt, so a
    caller that explicitly opts into the default shouldn't pay an
    extra LLM call."""
    detector = _cached_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([])

    await detector.detect("hello", prompt_name=None)
    await detector.detect("hello", prompt_name="default")

    assert mock_post.call_count == 1
    assert detector.cache_stats()["size"] == 1


async def test_cache_ignores_api_key_in_key(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """Same input + same prompt + same model → same detection regardless
    of which API key authenticated the call. Forwarded user keys must
    share cache entries; otherwise the cache fragments uselessly per
    user without any correctness benefit."""
    detector = _cached_detector(monkeypatch, max_size=10)
    mock_post.return_value = _ok_response([])

    await detector.detect("hello", api_key="user-1-key")
    await detector.detect("hello", api_key="user-2-key")

    assert mock_post.call_count == 1
    assert detector.cache_stats()["hits"] == 1


async def test_cache_eviction_recomputes(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock
) -> None:
    """Cap=1 → inserting a second key evicts the first; coming back to
    the original input pays for the LLM call again."""
    detector = _cached_detector(monkeypatch, max_size=1)
    mock_post.return_value = _ok_response([])

    await detector.detect("text A")  # miss
    await detector.detect("text B")  # miss, evicts A
    await detector.detect("text A")  # miss again (evicted)

    assert mock_post.call_count == 3
    s = detector.cache_stats()
    assert s["misses"] == 3
    assert s["hits"] == 0
    assert s["size"] == 1


# ── Redis-backed cache integration ──────────────────────────────────────
# End-to-end: LLM detector with `cache_backend=redis` + a fakeredis
# instance. Verifies the BaseRemoteDetector → factory → RedisDetectionCache
# wiring is intact: a hit on the second call skips the HTTP layer, the
# /health-shaped stats reflect the redis-backend asymmetry (size=0,
# max=0), and the `aclose()` chain drains the redis connection pool.


def _redis_cached_detector(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis_client,
) -> LLMDetector:
    """Configure the LLM detector to use the redis cache backend
    against a fakeredis instance. The factory reads CACHE_REDIS_URL +
    CACHE_SALT from the central config, so we patch both."""
    from anonymizer_guardrail import config as config_mod
    from anonymizer_guardrail.detector import cache as cache_mod

    monkeypatch.setattr(
        llm_mod, "CONFIG",
        _fake_config(cache_backend="redis", cache_ttl_s=60, cache_max_size=0),
    )
    monkeypatch.setattr(
        config_mod, "config",
        config_mod.config.model_copy(update={
            "cache_redis_url": "redis://fake/0",
            "cache_salt": "test-salt",
        }),
    )
    cache_mod._reset_resolved_salt_for_tests()

    from unittest.mock import patch
    with patch("redis.asyncio.from_url", return_value=fake_redis_client):
        return LLMDetector(
            api_base="http://test", api_key="", model="test", timeout_s=5
        )


async def test_redis_cache_hit_skips_http_call(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock,
) -> None:
    """LLM detector with redis backend: identical inputs go to HTTP
    once, subsequent calls hit redis."""
    import fakeredis.aioredis as fakeredis
    fake_client = fakeredis.FakeRedis(decode_responses=True)

    detector = _redis_cached_detector(monkeypatch, fake_client)
    mock_post.return_value = _ok_response([{"text": "alice", "type": "PERSON"}])

    r1 = await detector.detect("hello alice")
    r2 = await detector.detect("hello alice")
    r3 = await detector.detect("hello alice")

    assert mock_post.call_count == 1
    assert [m.text for m in r1] == ["alice"]
    assert [m.text for m in r2] == ["alice"]
    assert [m.text for m in r3] == ["alice"]
    s = detector.cache_stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    # Redis backend asymmetry — size and max are placeholders.
    assert s["size"] == 0
    assert s["max"] == 0
    await detector.aclose()


async def test_redis_cache_keys_separate_per_model_override(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock,
) -> None:
    """Cache key includes (text, model, prompt_name) — different
    `model` overrides occupy different redis keys. Same contract as
    the in-memory backend."""
    import fakeredis.aioredis as fakeredis
    fake_client = fakeredis.FakeRedis(decode_responses=True)

    detector = _redis_cached_detector(monkeypatch, fake_client)
    mock_post.return_value = _ok_response([{"text": "alice", "type": "PERSON"}])

    await detector.detect("hello alice")                         # default model
    await detector.detect("hello alice", model="other-model")    # different slot
    await detector.detect("hello alice", model="other-model")    # hits second

    assert mock_post.call_count == 2
    # Two distinct redis keys.
    keys = [k async for k in fake_client.scan_iter() if k.startswith("cache:llm:")]
    assert len(keys) == 2
    await detector.aclose()


async def test_redis_cache_aclose_drains_redis_pool(
    monkeypatch: pytest.MonkeyPatch, mock_post: AsyncMock,
) -> None:
    """`detector.aclose()` chains through to the cache's aclose()
    so the redis connection pool drains on FastAPI shutdown."""
    import fakeredis.aioredis as fakeredis
    fake_client = fakeredis.FakeRedis(decode_responses=True)

    detector = _redis_cached_detector(monkeypatch, fake_client)
    mock_post.return_value = _ok_response([])
    await detector.detect("warmup")  # creates the connection

    # Track aclose invocation on the underlying redis client.
    aclose_called = {"flag": False}
    original_aclose = detector._cache._client.aclose

    async def tracking_aclose():
        aclose_called["flag"] = True
        await original_aclose()

    detector._cache._client.aclose = tracking_aclose  # type: ignore[method-assign]
    await detector.aclose()
    assert aclose_called["flag"] is True