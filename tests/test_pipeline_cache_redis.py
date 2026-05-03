"""RedisPipelineCache-specific behaviour: digest key construction,
JSON wire format, source_detectors round-trip, NX-on-write semantics,
factory cross-config validation, redis-down passthrough.

The cross-backend Protocol contract (hit/miss accounting, defensive
copy on hit, put-doesn't-overwrite-populated-slot) is exercised by
`test_pipeline_cache.py` against `InMemoryPipelineCache`. This file
only owns Redis-specific behaviour.

Uses `fakeredis.aioredis` so CI doesn't need a real Redis server.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

# Match other test modules: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.detector.base import Match
from anonymizer_guardrail.pipeline_cache import build_pipeline_cache
from anonymizer_guardrail.pipeline_cache_redis import RedisPipelineCache


def _m(text: str, etype: str = "PERSON") -> Match:
    return Match(text=text, entity_type=etype)


@pytest.fixture
async def cache() -> RedisPipelineCache:
    """Build a RedisPipelineCache wired to fakeredis.aioredis."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        c = RedisPipelineCache(
            url="redis://fake/0", ttl_s=60, salt=b"test-salt",
        )
    yield c
    await c.aclose()


# ── Round-trip with sources ────────────────────────────────────────────


async def test_round_trip_preserves_sources(cache: RedisPipelineCache) -> None:
    """A get_or_compute round-trip preserves the per-match
    source_detectors tuple. Pins the load-bearing addition vs the
    per-detector cache (which only stores Match without sources)."""
    payload = [
        (_m("alice"), ("regex", "llm")),
        (_m("bob@example.com", "EMAIL_ADDRESS"), ("regex",)),
    ]

    async def compute():
        return payload

    # Miss → compute runs, payload is stored.
    miss = await cache.get_or_compute(("text", ("regex", "llm"), ()), compute)
    assert miss == payload

    # Hit → payload comes back from Redis with sources intact.
    hit = await cache.get_or_compute(("text", ("regex", "llm"), ()), compute)
    assert hit == payload
    assert hit[0][1] == ("regex", "llm")
    assert hit[1][1] == ("regex",)


# ── Wire format ────────────────────────────────────────────────────────


async def test_json_wire_format_matches_spec(cache: RedisPipelineCache) -> None:
    """Each entry is `{"text", "type", "sources": [...]}`. Operators
    debugging from `redis-cli GET pipeline:<digest>` should see this
    shape — pin it so a future refactor that drops `sources` for
    space breaks loudly."""
    await cache.get_or_compute(
        ("text", ("regex",), ()),
        lambda: _matches([(_m("alice"), ("regex",))]),
    )

    # There's exactly one key under the prefix.
    keys = [k async for k in cache._client.scan_iter(match="pipeline:*")]
    assert len(keys) == 1
    raw = await cache._client.get(keys[0])
    decoded = json.loads(raw)
    assert decoded == [{"text": "alice", "type": "PERSON", "sources": ["regex"]}]


async def _matches(items):
    """Async helper for compute callbacks — matches the cache's
    expected `Awaitable[list[MatchWithSources]]` callable shape."""
    return items


# ── Key digest ─────────────────────────────────────────────────────────


async def test_key_uses_pipeline_prefix(cache: RedisPipelineCache) -> None:
    """All keys live under `pipeline:` so a `FLUSHDB` or scan on
    `cache:` (per-detector) doesn't wipe pipeline cache entries.
    Pin the namespace separation."""
    await cache.get_or_compute(
        ("text", ("regex",), ()),
        lambda: _matches([(_m("alice"), ("regex",))]),
    )
    keys = [k async for k in cache._client.scan_iter()]
    assert all(k.startswith("pipeline:") for k in keys)


async def test_different_keys_produce_different_digests(
    cache: RedisPipelineCache,
) -> None:
    """The digest is derived from `repr(key)`, so changing any tuple
    component changes the Redis key. Without this, the kwargs in the
    cache key would be cosmetic — pipeline cache hits would scatter
    across single-digest slots regardless of overrides."""
    await cache.get_or_compute(
        ("text", ("regex",), (("llm", (("model", "a"),)),)),
        lambda: _matches([(_m("a"), ("regex",))]),
    )
    await cache.get_or_compute(
        ("text", ("regex",), (("llm", (("model", "b"),)),)),
        lambda: _matches([(_m("b"), ("regex",))]),
    )
    keys = sorted([k async for k in cache._client.scan_iter()])
    assert len(keys) == 2  # different model → different slot


# ── put() with NX semantics ────────────────────────────────────────────


async def test_put_does_not_overwrite_populated_slot(
    cache: RedisPipelineCache,
) -> None:
    """`put` uses `SET nx=True` so a real-detection result that landed
    first stays; a prewarm write afterwards is a no-op."""
    real = [(_m("from-real"), ("regex",))]
    synth = [(_m("from-synth"), ("llm",))]

    async def compute():
        return real

    await cache.get_or_compute(("text", ("regex",), ()), compute)
    await cache.put(("text", ("regex",), ()), synth)

    got = await cache.get_or_compute(("text", ("regex",), ()), compute)
    assert got == real


async def test_put_populates_cold_slot(cache: RedisPipelineCache) -> None:
    """When the slot is empty, prewarm `put` is the only writer —
    later get_or_compute hits without running compute."""
    synth = [(_m("alice"), ("regex", "llm"))]
    await cache.put(("text", ("regex", "llm"), ()), synth)

    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return []

    got = await cache.get_or_compute(("text", ("regex", "llm"), ()), compute)
    assert calls == 0
    assert got == synth


# ── Stats ──────────────────────────────────────────────────────────────


async def test_stats_size_and_max_are_zero(cache: RedisPipelineCache) -> None:
    """`size` and `max` are 0 by design (the SCAN to compute the
    actual count is async; stats() is sync). Operators query Redis
    directly."""
    await cache.get_or_compute(
        ("text", ("regex",), ()),
        lambda: _matches([(_m("a"), ("regex",))]),
    )
    s = cache.stats()
    assert s["size"] == 0
    assert s["max"] == 0


async def test_stats_hits_and_misses_are_per_process(
    cache: RedisPipelineCache,
) -> None:
    """Hit/miss counters reflect THIS instance's traffic — what an
    operator wants to see (per-replica effectiveness)."""
    async def compute():
        return [(_m("a"), ("regex",))]

    await cache.get_or_compute(("k", ("regex",), ()), compute)  # miss
    await cache.get_or_compute(("k", ("regex",), ()), compute)  # hit
    await cache.get_or_compute(("k", ("regex",), ()), compute)  # hit

    s = cache.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1


# ── Factory cross-config ───────────────────────────────────────────────


def test_redis_factory_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PIPELINE_CACHE_BACKEND=redis` without `CACHE_REDIS_URL` fails
    at construction. The Pydantic-level validator catches the env-var
    misconfig at boot; this is the defence-in-depth check for
    programmatic callers."""
    from anonymizer_guardrail import config as config_mod

    patched = config_mod.config.model_copy(update={"cache_redis_url": ""})
    monkeypatch.setattr(config_mod, "config", patched)

    with pytest.raises(RuntimeError, match="CACHE_REDIS_URL"):
        build_pipeline_cache(
            backend="redis", cache_max_size=10, cache_ttl_s=60,
        )


# ── Construction failure modes ─────────────────────────────────────────


def test_empty_url_rejected() -> None:
    """Direct construction with empty URL fails loud — matches the
    other Redis backends' construction guards."""
    with pytest.raises(RuntimeError, match="non-empty url"):
        RedisPipelineCache(url="", ttl_s=60, salt=b"x")


def test_empty_salt_rejected() -> None:
    """The factory always resolves a non-empty salt (random fallback);
    direct construction with empty salt is a misuse."""
    with pytest.raises(RuntimeError, match="non-empty salt"):
        RedisPipelineCache(url="redis://x", ttl_s=60, salt=b"")
