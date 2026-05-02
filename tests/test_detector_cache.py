"""Tests for the reusable detection-cache primitive.

Covers the contract every detector that opts into caching depends on:
disabled-state passthrough, hit/miss accounting, LRU eviction,
defensive copy on hit, and concurrent-miss safety.

Concrete backend tested here is `InMemoryDetectionCache`. The Protocol
shape (`DetectorResultCache`) is what detectors code against — see the
isinstance check below for the satisfies-the-Protocol assertion.
"""

from __future__ import annotations

import asyncio
import os

# Match other test modules: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.detector.base import Match
from anonymizer_guardrail.detector.cache import DetectorResultCache
from anonymizer_guardrail.detector.cache_memory import InMemoryDetectionCache


def _m(text: str, etype: str = "PERSON") -> Match:
    return Match(text=text, entity_type=etype)


# ── Protocol seam ───────────────────────────────────────────────────────


def test_in_memory_cache_satisfies_protocol() -> None:
    """The seam detectors code against. A future RedisDetectionCache
    must satisfy the same Protocol — runtime_checkable lets a test
    assert that without importing the concrete type."""
    assert isinstance(InMemoryDetectionCache(0), DetectorResultCache)
    assert isinstance(InMemoryDetectionCache(10), DetectorResultCache)


# ── Disabled state ──────────────────────────────────────────────────────


async def test_disabled_cache_always_calls_compute() -> None:
    """max_size=0 → cache is a passthrough; every call hits compute."""
    cache = InMemoryDetectionCache(0)
    calls = 0

    async def compute() -> list[Match]:
        nonlocal calls
        calls += 1
        return [_m("alice")]

    for _ in range(3):
        result = await cache.get_or_compute("k", compute)
        assert result == [_m("alice")]
    assert calls == 3
    assert cache.enabled is False
    s = cache.stats()
    assert s == {"size": 0, "max": 0, "hits": 0, "misses": 0}


async def test_negative_max_size_clamped_to_zero() -> None:
    """Defensive: a negative cap behaves like disabled, not like a crash."""
    cache = InMemoryDetectionCache(-5)
    assert cache.enabled is False
    assert cache.stats()["max"] == 0


# ── Hit / miss accounting ───────────────────────────────────────────────


async def test_first_call_misses_subsequent_calls_hit() -> None:
    cache = InMemoryDetectionCache(10)
    calls = 0

    async def compute() -> list[Match]:
        nonlocal calls
        calls += 1
        return [_m("alice")]

    await cache.get_or_compute("k", compute)
    await cache.get_or_compute("k", compute)
    await cache.get_or_compute("k", compute)

    assert calls == 1
    s = cache.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert s["size"] == 1


async def test_distinct_keys_get_distinct_slots() -> None:
    cache = InMemoryDetectionCache(10)

    async def compute_a() -> list[Match]:
        return [_m("alice")]

    async def compute_b() -> list[Match]:
        return [_m("bob")]

    a = await cache.get_or_compute("a", compute_a)
    b = await cache.get_or_compute("b", compute_b)
    a2 = await cache.get_or_compute("a", compute_a)

    assert a == [_m("alice")]
    assert b == [_m("bob")]
    assert a2 == [_m("alice")]
    assert cache.stats()["size"] == 2


# ── LRU eviction ────────────────────────────────────────────────────────


async def test_lru_evicts_least_recently_used() -> None:
    """Cap=2: insert k1, k2; touch k1; insert k3 → k2 (oldest) evicted."""
    cache = InMemoryDetectionCache(2)

    async def make_compute(label: str):
        async def compute() -> list[Match]:
            return [_m(label)]
        return compute

    await cache.get_or_compute("k1", await make_compute("v1"))
    await cache.get_or_compute("k2", await make_compute("v2"))
    # Touch k1 to make it most-recently-used.
    await cache.get_or_compute("k1", await make_compute("v1"))
    await cache.get_or_compute("k3", await make_compute("v3"))

    assert cache.stats()["size"] == 2

    # k2 should have been evicted (its compute will run again).
    ran_again = False

    async def compute_k2_again() -> list[Match]:
        nonlocal ran_again
        ran_again = True
        return [_m("v2")]

    await cache.get_or_compute("k2", compute_k2_again)
    assert ran_again is True


async def test_eviction_keeps_size_at_or_below_cap() -> None:
    cache = InMemoryDetectionCache(3)

    async def compute() -> list[Match]:
        return []

    for i in range(20):
        await cache.get_or_compute(f"k{i}", compute)

    assert cache.stats()["size"] == 3


# ── Defensive copy on hit ───────────────────────────────────────────────


async def test_returned_list_is_a_copy_not_shared_reference() -> None:
    """Caller mutating the returned list must NOT poison the cache.
    The pipeline's `_dedup` iterates and could in principle mutate."""
    cache = InMemoryDetectionCache(10)

    async def compute() -> list[Match]:
        return [_m("alice"), _m("bob")]

    first = await cache.get_or_compute("k", compute)
    first.append(_m("mallory"))
    first.clear()

    second = await cache.get_or_compute("k", compute)
    assert [m.text for m in second] == ["alice", "bob"]


# ── Concurrent miss ─────────────────────────────────────────────────────


async def test_concurrent_misses_on_same_key_dont_deadlock() -> None:
    """Compute runs outside the lock — two concurrent misses on the same
    key both succeed (last writer wins on the cache slot). Documents
    the thundering-herd trade-off: identical concurrent inputs do
    duplicate work, but they don't block each other."""
    cache = InMemoryDetectionCache(10)
    started = asyncio.Event()
    finish = asyncio.Event()
    call_count = 0

    async def compute() -> list[Match]:
        nonlocal call_count
        call_count += 1
        started.set()
        await finish.wait()
        return [_m("alice")]

    t1 = asyncio.create_task(cache.get_or_compute("k", compute))
    await started.wait()
    # By the time we kick off t2, t1's compute is already running and
    # has counted itself a miss. t2 should also miss (compute runs
    # again) — proving the lock isn't held across the await.
    t2 = asyncio.create_task(cache.get_or_compute("k", compute))
    await asyncio.sleep(0.01)  # let t2 reach its lock acquire + miss path
    finish.set()

    r1 = await t1
    r2 = await t2

    assert r1 == [_m("alice")]
    assert r2 == [_m("alice")]
    assert call_count == 2  # both saw the cold key, both did the work


# ── Stored values are immutable tuples ──────────────────────────────────


async def test_compute_returning_list_is_stored_as_tuple_internally() -> None:
    """Internal _CacheValue is tuple, not list. Verifies via the
    invariant that mutating the compute's return value before the cache
    serialises it can't reach back into the cache's storage."""
    cache = InMemoryDetectionCache(10)

    source: list[Match] = [_m("alice")]

    async def compute() -> list[Match]:
        return source

    await cache.get_or_compute("k", compute)
    # Mutate the source list AFTER the cache has stored it.
    source.append(_m("mallory"))

    # Cache should still return a single 'alice' (it stored a tuple
    # snapshot, not a reference to the mutable source list).
    second = await cache.get_or_compute("k", compute)
    assert [m.text for m in second] == ["alice"]
