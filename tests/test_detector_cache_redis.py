"""RedisDetectionCache-specific behaviour: BLAKE2b-keyed namespaced
keys, JSON wire format, TTL via `EXPIRE`, Redis-down passthrough with
rate-limited warnings, error fallthrough on bad payloads, factory
plumbing.

Cross-backend contract tests (Protocol satisfaction, basic hit/miss,
defensive copy) live in `test_detector_cache.py` for the in-memory
backend; the redis backend's contract tests are inline below because
RedisDetectionCache has its own setup ceremony (fakeredis fixture,
salt resolution, namespace handling) that doesn't fit the
single-class shape used there.

Uses `fakeredis.aioredis` so CI doesn't need a real Redis server.
The fakeredis instance speaks the redis-py asyncio protocol, so the
tests exercise the same code path as production.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from unittest.mock import patch

# Match other test modules: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.detector.base import Match
from anonymizer_guardrail.detector.cache import (
    DetectorResultCache,
    build_detector_cache,
    _reset_resolved_salt_for_tests,
)
from anonymizer_guardrail.detector.cache_redis import RedisDetectionCache


def _m(text: str, etype: str = "PERSON") -> Match:
    return Match(text=text, entity_type=etype)


@pytest.fixture(autouse=True)
def _reset_salt() -> None:
    """Each test starts with a fresh salt resolution so monkeypatched
    `cache_salt` values don't bleed across tests via the
    module-level cache. Auto-applied to every test in this file."""
    _reset_resolved_salt_for_tests()
    yield
    _reset_resolved_salt_for_tests()


@pytest.fixture
async def cache() -> RedisDetectionCache:
    """Build a RedisDetectionCache wired to fakeredis. Same trick as
    `tests/test_vault_redis.py` — patch `redis.asyncio.from_url` to
    return the fake."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        c = RedisDetectionCache(
            namespace="llm",
            url="redis://fake/0",
            ttl_s=60,
            salt=b"test-salt-16-bytes",
        )
    yield c
    await c.aclose()


# ── Protocol seam ───────────────────────────────────────────────────────


async def test_satisfies_protocol(cache: RedisDetectionCache) -> None:
    """Mirror of test_detector_cache.py's protocol check. The
    detector layer codes against `DetectorResultCache`; the redis
    backend must satisfy it."""
    assert isinstance(cache, DetectorResultCache)


# ── Round-trip ──────────────────────────────────────────────────────────


async def test_first_call_misses_subsequent_calls_hit(
    cache: RedisDetectionCache,
) -> None:
    calls = 0

    async def compute() -> list[Match]:
        nonlocal calls
        calls += 1
        return [_m("alice")]

    r1 = await cache.get_or_compute("k", compute)
    r2 = await cache.get_or_compute("k", compute)
    r3 = await cache.get_or_compute("k", compute)

    assert r1 == r2 == r3 == [_m("alice")]
    # Compute called exactly once — Redis served the second + third.
    assert calls == 1
    s = cache.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1


async def test_distinct_keys_get_distinct_slots(
    cache: RedisDetectionCache,
) -> None:
    async def compute_a() -> list[Match]:
        return [_m("alice")]

    async def compute_b() -> list[Match]:
        return [_m("bob")]

    a = await cache.get_or_compute(("a",), compute_a)
    b = await cache.get_or_compute(("b",), compute_b)
    a2 = await cache.get_or_compute(("a",), compute_a)

    assert a == [_m("alice")]
    assert b == [_m("bob")]
    assert a2 == [_m("alice")]


async def test_returned_list_is_independent_of_cache_state(
    cache: RedisDetectionCache,
) -> None:
    """Caller mutating the returned list must not poison subsequent
    reads. Redis backend deserializes from a fresh string each time
    so this is automatic, but pin the contract."""
    async def compute() -> list[Match]:
        return [_m("alice"), _m("bob")]

    first = await cache.get_or_compute("k", compute)
    first.append(_m("mallory"))
    first.clear()

    second = await cache.get_or_compute("k", compute)
    assert [m.text for m in second] == ["alice", "bob"]


# ── Always-enabled ──────────────────────────────────────────────────────


async def test_enabled_is_always_true(cache: RedisDetectionCache) -> None:
    """Operators who selected the redis backend explicitly want
    caching. `enabled` is unconditionally True; the Redis-down
    passthrough is internal and doesn't flip the flag."""
    assert cache.enabled is True


# ── Key namespacing + digest ────────────────────────────────────────────


async def test_keys_use_namespace_prefix_and_blake2b_digest(
    cache: RedisDetectionCache,
) -> None:
    """Keys go under `cache:<namespace>:<hex>` so the backend can
    share a Redis instance with vault and other consumers without
    colliding. The hex part is BLAKE2b — 64 chars (32 bytes hex)."""
    async def compute() -> list[Match]:
        return [_m("alice")]

    await cache.get_or_compute(("hello", "world"), compute)

    keys = [k async for k in cache._client.scan_iter()]
    cache_keys = [k for k in keys if k.startswith("cache:llm:")]
    assert len(cache_keys) == 1
    digest = cache_keys[0].removeprefix("cache:llm:")
    assert len(digest) == 64
    # Hex digest — only 0-9a-f.
    assert all(c in "0123456789abcdef" for c in digest)


async def test_namespace_isolation_across_detectors() -> None:
    """Two RedisDetectionCache instances sharing a Redis client but
    with different namespaces must NOT see each other's entries —
    even when the underlying cache key is identical."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        cache_llm = RedisDetectionCache(
            namespace="llm", url="redis://fake/0",
            ttl_s=60, salt=b"shared-salt",
        )
        cache_pf = RedisDetectionCache(
            namespace="privacy_filter", url="redis://fake/0",
            ttl_s=60, salt=b"shared-salt",
        )

    async def compute_llm() -> list[Match]:
        return [_m("from-llm")]

    async def compute_pf() -> list[Match]:
        return [_m("from-pf")]

    same_key = ("the-input-text",)
    await cache_llm.get_or_compute(same_key, compute_llm)
    pf_result = await cache_pf.get_or_compute(same_key, compute_pf)
    # PF should see its own value, not the LLM's, despite identical keys.
    assert pf_result == [_m("from-pf")]
    # And LLM should still see its own.
    llm_result = await cache_llm.get_or_compute(same_key, compute_llm)
    assert llm_result == [_m("from-llm")]
    await cache_llm.aclose()
    await cache_pf.aclose()


async def test_salt_changes_invalidate_cache_keys() -> None:
    """The cache key digest is keyed with the operator-resolved cache
    salt. Rotating the salt produces different digests, so a fresh
    instance with a new salt won't see the old entries (Redis-side
    TTL still cleans them up)."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        cache_a = RedisDetectionCache(
            namespace="llm", url="redis://fake/0",
            ttl_s=60, salt=b"salt-A",
        )
        cache_b = RedisDetectionCache(
            namespace="llm", url="redis://fake/0",
            ttl_s=60, salt=b"salt-B",
        )

    counter = {"a": 0, "b": 0}

    async def compute_a() -> list[Match]:
        counter["a"] += 1
        return [_m("alice")]

    async def compute_b() -> list[Match]:
        counter["b"] += 1
        return [_m("alice")]

    await cache_a.get_or_compute("the-key", compute_a)
    # Same input text on a different-salted instance → different digest
    # → cache miss → compute_b runs.
    await cache_b.get_or_compute("the-key", compute_b)
    assert counter == {"a": 1, "b": 1}
    await cache_a.aclose()
    await cache_b.aclose()


# ── JSON wire format ────────────────────────────────────────────────────


async def test_value_is_json_encoded(cache: RedisDetectionCache) -> None:
    """The wire format is JSON — operators inspecting `redis-cli GET
    cache:<…>` should see readable JSON, not pickle. Pin the contract."""
    async def compute() -> list[Match]:
        return [_m("alice"), _m("bob", "ORGANIZATION")]

    await cache.get_or_compute("k", compute)

    # Find the one key we just wrote.
    keys = [k async for k in cache._client.scan_iter() if k.startswith("cache:")]
    assert len(keys) == 1
    raw = await cache._client.get(keys[0])
    assert isinstance(raw, str)
    arr = json.loads(raw)
    assert arr == [
        {"text": "alice", "type": "PERSON"},
        {"text": "bob", "type": "ORGANIZATION"},
    ]


async def test_corrupted_json_treated_as_miss_logs_error(
    cache: RedisDetectionCache,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If a cache entry is corrupted (non-JSON, wrong shape), the
    cache treats it as a miss, runs compute, and overwrites. Same
    contract as the vault's corrupted-entry path: log ERROR but
    don't crash the request."""
    # Pre-poison the digest a real call would compute. Compute the
    # digest the way RedisDetectionCache does for ("k",).
    poisoned_key = cache._redis_key("k")
    await cache._client.set(poisoned_key, "this-is-not-json", ex=60)

    async def compute() -> list[Match]:
        return [_m("alice")]

    with caplog.at_level(logging.ERROR, logger="anonymizer.detector.cache.redis"):
        result = await cache.get_or_compute("k", compute)

    assert result == [_m("alice")]
    assert any("unexpected shape" in r.message for r in caplog.records)


async def test_wrong_shape_payload_treated_as_miss(
    cache: RedisDetectionCache,
) -> None:
    """A JSON payload that's a dict instead of a list, or has missing
    `text`/`type` keys — treat as miss, compute fresh."""
    poisoned_key = cache._redis_key("k")
    # Top-level dict instead of list.
    await cache._client.set(poisoned_key, json.dumps({"oops": "nope"}), ex=60)

    async def compute() -> list[Match]:
        return [_m("alice")]

    result = await cache.get_or_compute("k", compute)
    assert result == [_m("alice")]


# ── TTL ─────────────────────────────────────────────────────────────────


async def test_ttl_set_on_first_write(cache: RedisDetectionCache) -> None:
    async def compute() -> list[Match]:
        return [_m("alice")]

    await cache.get_or_compute("k", compute)
    redis_key = cache._redis_key("k")
    ttl = await cache._client.ttl(redis_key)
    assert ttl > 0 and ttl <= 60


async def test_ttl_refreshed_on_rewrite() -> None:
    """A second SET (e.g. after eviction-and-recompute) resets TTL to
    the full window — pin the contract so a future refactor that
    uses `SET ... KEEPTTL` doesn't silently break it."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        c = RedisDetectionCache(
            namespace="llm", url="redis://fake/0",
            ttl_s=60, salt=b"some-salt",
        )

    async def compute() -> list[Match]:
        return [_m("alice")]

    # First write.
    await c.get_or_compute("k", compute)
    redis_key = c._redis_key("k")
    # Wind the TTL down so we can tell whether re-write resets it.
    await fake_client.expire(redis_key, 5)
    ttl_before = await fake_client.ttl(redis_key)
    assert ttl_before <= 5

    # Force a miss-and-rewrite by deleting the cached value, then
    # re-running through get_or_compute. (A miss + SET refreshes TTL.)
    await fake_client.delete(redis_key)
    await c.get_or_compute("k", compute)
    ttl_after = await fake_client.ttl(redis_key)
    assert ttl_after > 5
    assert ttl_after <= 60
    await c.aclose()


# ── Redis-down passthrough ──────────────────────────────────────────────


async def test_get_failure_falls_through_to_compute() -> None:
    """When Redis fails on GET, get_or_compute falls through to
    compute() and returns the fresh result. The cache is degraded
    but the request succeeds."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)

    async def boom(*_args, **_kwargs):
        raise ConnectionError("simulated Redis down")

    with patch("redis.asyncio.from_url", return_value=fake_client):
        c = RedisDetectionCache(
            namespace="llm", url="redis://fake/0",
            ttl_s=60, salt=b"some-salt",
        )

    c._client.get = boom  # type: ignore[method-assign]

    async def compute() -> list[Match]:
        return [_m("alice")]

    result = await c.get_or_compute("k", compute)
    assert result == [_m("alice")]
    await c.aclose()


async def test_set_failure_falls_through_returns_compute_result() -> None:
    """When Redis fails on SET (after a miss), get_or_compute returns
    the freshly-computed value anyway — no correctness impact, just
    a future cache miss for the same key."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)

    async def boom(*_args, **_kwargs):
        raise ConnectionError("simulated Redis down on SET")

    with patch("redis.asyncio.from_url", return_value=fake_client):
        c = RedisDetectionCache(
            namespace="llm", url="redis://fake/0",
            ttl_s=60, salt=b"some-salt",
        )

    # Let GET succeed (returns None → miss) but make SET fail.
    c._client.set = boom  # type: ignore[method-assign]

    async def compute() -> list[Match]:
        return [_m("alice")]

    result = await c.get_or_compute("k", compute)
    assert result == [_m("alice")]
    await c.aclose()


async def test_redis_down_warning_is_rate_limited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated Redis failures must not flood logs — at most one
    warning per `_WARN_INTERVAL_S` seconds per namespace. Drives the
    rate limiter directly by manipulating the internal timestamp."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)

    async def boom(*_args, **_kwargs):
        raise ConnectionError("simulated Redis down")

    with patch("redis.asyncio.from_url", return_value=fake_client):
        c = RedisDetectionCache(
            namespace="llm", url="redis://fake/0",
            ttl_s=60, salt=b"some-salt",
        )

    c._client.get = boom  # type: ignore[method-assign]

    async def compute() -> list[Match]:
        return [_m("alice")]

    with caplog.at_level(logging.WARNING, logger="anonymizer.detector.cache.redis"):
        # Three back-to-back failures — only the first should warn.
        await c.get_or_compute("k1", compute)
        await c.get_or_compute("k2", compute)
        await c.get_or_compute("k3", compute)

    warnings = [
        r for r in caplog.records
        if "cache temporarily passthrough" in r.message
    ]
    assert len(warnings) == 1, f"expected 1 warning, got {len(warnings)}"

    # Force the rate-limit window past — next failure should warn again.
    c._last_warn_at -= 120.0
    with caplog.at_level(logging.WARNING, logger="anonymizer.detector.cache.redis"):
        await c.get_or_compute("k4", compute)

    warnings = [
        r for r in caplog.records
        if "cache temporarily passthrough" in r.message
    ]
    assert len(warnings) == 2

    await c.aclose()


# ── Construction failure modes ──────────────────────────────────────────


def test_empty_namespace_raises() -> None:
    with pytest.raises(RuntimeError, match="non-empty namespace"):
        RedisDetectionCache(
            namespace="", url="redis://fake/0", ttl_s=60, salt=b"s",
        )


def test_empty_url_raises() -> None:
    with pytest.raises(RuntimeError, match="non-empty url"):
        RedisDetectionCache(
            namespace="llm", url="", ttl_s=60, salt=b"s",
        )


def test_empty_salt_raises() -> None:
    """The factory always resolves a non-empty salt; constructing
    directly with empty salt is misuse."""
    with pytest.raises(RuntimeError, match="non-empty salt"):
        RedisDetectionCache(
            namespace="llm", url="redis://fake/0", ttl_s=60, salt=b"",
        )


# ── Stats placeholder ───────────────────────────────────────────────────


async def test_stats_size_and_max_are_zero(
    cache: RedisDetectionCache,
) -> None:
    """`size` and `max` are 0 by design — operators query Redis
    directly via `SCAN MATCH cache:llm:*` for the actual count, and
    the per-detector cap is managed by Redis-side `maxmemory`. Pin
    so a future refactor that surfaces a real count doesn't silently
    change the /health contract."""
    async def compute() -> list[Match]:
        return [_m("alice")]

    await cache.get_or_compute("k", compute)
    s = cache.stats()
    assert s["size"] == 0
    assert s["max"] == 0
    assert s["hits"] == 0
    assert s["misses"] == 1


async def test_stats_hits_and_misses_are_per_instance(
    cache: RedisDetectionCache,
) -> None:
    async def compute() -> list[Match]:
        return [_m("alice")]

    await cache.get_or_compute("k1", compute)  # miss
    await cache.get_or_compute("k1", compute)  # hit
    await cache.get_or_compute("k2", compute)  # miss

    s = cache.stats()
    assert s["hits"] == 1
    assert s["misses"] == 2


# ── Factory plumbing ────────────────────────────────────────────────────


def test_factory_memory_returns_in_memory_cache() -> None:
    from anonymizer_guardrail.detector.cache_memory import InMemoryDetectionCache

    c = build_detector_cache(
        namespace="llm",
        backend="memory",
        cache_max_size=10,
        cache_ttl_s=60,
    )
    assert isinstance(c, InMemoryDetectionCache)


def test_factory_redis_requires_cache_redis_url(monkeypatch) -> None:
    """Factory rejects redis backend when CACHE_REDIS_URL is unset."""
    from anonymizer_guardrail.detector import cache as cache_mod

    # Ensure the central config sees an empty cache_redis_url.
    from anonymizer_guardrail import config as config_mod
    monkeypatch.setattr(
        config_mod, "config",
        config_mod.config.model_copy(update={"cache_redis_url": ""}),
    )
    # cache.py imports config lazily inside build_detector_cache, so
    # patch the module the factory looks at.
    import importlib
    importlib.reload(cache_mod)

    with pytest.raises(RuntimeError, match="CACHE_REDIS_URL"):
        cache_mod.build_detector_cache(
            namespace="llm", backend="redis",
            cache_max_size=0, cache_ttl_s=60,
        )


def test_factory_unknown_backend_raises() -> None:
    with pytest.raises(RuntimeError, match="Unknown cache backend"):
        build_detector_cache(
            namespace="llm",
            backend="bogus",  # type: ignore[arg-type]
            cache_max_size=10,
            cache_ttl_s=60,
        )


def test_factory_redis_constructs_via_central_config(monkeypatch) -> None:
    """Factory reads CACHE_REDIS_URL and CACHE_SALT from central config
    when constructing the redis backend. Mirrors the vault factory's
    pattern of central-config field lookup at call time so test
    monkeypatches are visible."""
    from anonymizer_guardrail import config as config_mod
    from anonymizer_guardrail.detector import cache as cache_mod

    monkeypatch.setattr(
        config_mod, "config",
        config_mod.config.model_copy(update={
            "cache_redis_url": "redis://fake/0",
            "cache_salt": "explicit-test-salt",
        }),
    )
    # Ensure the lazy import inside build_detector_cache picks up
    # the patched config (it does `from ..config import config` each
    # call, which resolves the module attribute live).
    import fakeredis.aioredis as fakeredis
    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        c = cache_mod.build_detector_cache(
            namespace="llm", backend="redis",
            cache_max_size=0, cache_ttl_s=60,
        )

    assert isinstance(c, RedisDetectionCache)
    assert c._namespace == "llm"
    assert c._url == "redis://fake/0"
    assert c._ttl_s == 60
    # Salt was resolved from the explicit operator value.
    assert c._salt == b"explicit-test-salt"


def test_factory_redis_random_salt_when_unset(monkeypatch) -> None:
    """Empty CACHE_SALT → factory generates a random salt at first
    resolution, caches it for the process. Subsequent factory calls
    see the same salt — otherwise multi-detector deployments would
    each generate their own salt and cache hits across detectors
    would never be reproducible."""
    from anonymizer_guardrail import config as config_mod
    from anonymizer_guardrail.detector import cache as cache_mod

    monkeypatch.setattr(
        config_mod, "config",
        config_mod.config.model_copy(update={
            "cache_redis_url": "redis://fake/0",
            "cache_salt": "",  # empty → random
        }),
    )

    import fakeredis.aioredis as fakeredis
    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        c1 = cache_mod.build_detector_cache(
            namespace="llm", backend="redis",
            cache_max_size=0, cache_ttl_s=60,
        )
        c2 = cache_mod.build_detector_cache(
            namespace="privacy_filter", backend="redis",
            cache_max_size=0, cache_ttl_s=60,
        )

    # Salts are non-empty and equal — process-wide caching kicked in.
    assert c1._salt
    assert c1._salt == c2._salt
    # And the resolved salt is 16 bytes (the documented random size).
    assert len(c1._salt) == 16
