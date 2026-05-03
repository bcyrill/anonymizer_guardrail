"""Tests for the pipeline-level result cache primitive.

Covers the contract both backends share: disabled-state passthrough,
hit/miss accounting, LRU eviction, defensive copy on hit, the put()
prewarm path (idempotent when slot is populated), and the disabled
sentinel returned by `build_pipeline_cache(backend="none")`.

Concrete backends tested here:
  * `InMemoryPipelineCache` — full coverage.
  * `_DisabledPipelineCache` — sentinel-shape coverage (the no-op
    passthrough returned for `backend="none"`).

Redis-specific behaviour (key digest, JSON wire format, error
handling) lives in `test_pipeline_cache_redis.py`.
"""

from __future__ import annotations

import os

# Match other test modules: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.detector.base import Match
from anonymizer_guardrail.pipeline_cache import (
    PipelineCacheKey,
    PipelineResultCache,
    build_pipeline_cache,
)
from anonymizer_guardrail.pipeline_cache_memory import InMemoryPipelineCache


def _m(text: str, etype: str = "PERSON") -> Match:
    return Match(text=text, entity_type=etype)


def _key(text: str = "hello", *, mode=("regex",), kwargs=()) -> PipelineCacheKey:
    return (text, mode, kwargs)


# ── Protocol seam ───────────────────────────────────────────────────────


def test_in_memory_cache_satisfies_protocol() -> None:
    """The seam Pipeline codes against. Future backends must satisfy
    the same Protocol — runtime_checkable lets a test assert without
    importing the concrete type."""
    assert isinstance(InMemoryPipelineCache(0), PipelineResultCache)
    assert isinstance(InMemoryPipelineCache(10), PipelineResultCache)


def test_disabled_sentinel_satisfies_protocol() -> None:
    """`backend="none"` returns a sentinel that mimics the Protocol.
    Pipeline can call `get_or_compute`/`put`/`stats`/`aclose`
    uniformly without a None check on the cache reference."""
    cache = build_pipeline_cache(
        backend="none", cache_max_size=0, cache_ttl_s=0,
    )
    assert isinstance(cache, PipelineResultCache)
    assert cache.enabled is False


# ── Disabled state ──────────────────────────────────────────────────────


async def test_disabled_cache_always_calls_compute() -> None:
    """max_size=0 → cache is a passthrough; every call hits compute."""
    cache = InMemoryPipelineCache(0)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return [(_m("alice"), ("regex",))]

    for _ in range(3):
        await cache.get_or_compute(_key(), compute)
    assert calls == 3


async def test_disabled_sentinel_passthrough() -> None:
    """The `backend="none"` sentinel calls compute on every call and
    `put` is a no-op. Stats report all zeros."""
    cache = build_pipeline_cache(
        backend="none", cache_max_size=0, cache_ttl_s=0,
    )
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return [(_m("alice"), ("regex",))]

    for _ in range(3):
        await cache.get_or_compute(_key(), compute)
    assert calls == 3

    # `put` does nothing — a follow-up get_or_compute still runs compute.
    await cache.put(_key("other"), [(_m("alice"), ("regex",))])
    await cache.get_or_compute(_key("other"), compute)
    assert calls == 4

    assert cache.stats() == {"size": 0, "max": 0, "hits": 0, "misses": 0}


# ── Hit/miss accounting ────────────────────────────────────────────────


async def test_hit_miss_counters() -> None:
    """First call to a key is a miss; second is a hit. Counters
    surface on /health via stats()."""
    cache = InMemoryPipelineCache(10)

    async def compute():
        return [(_m("alice"), ("regex",))]

    await cache.get_or_compute(_key(), compute)  # miss
    await cache.get_or_compute(_key(), compute)  # hit
    await cache.get_or_compute(_key(), compute)  # hit

    s = cache.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert s["size"] == 1
    assert s["max"] == 10


# ── Defensive copy on hit ──────────────────────────────────────────────


async def test_get_returns_independent_list() -> None:
    """Mutating a returned list must NOT poison the cache. The
    backend stores tuples internally; every get-style hit returns
    a fresh list. Pins the contract pipeline relies on (it dedups
    + mutates the returned list in some paths)."""
    cache = InMemoryPipelineCache(10)

    async def compute():
        return [(_m("alice"), ("regex",))]

    first = await cache.get_or_compute(_key(), compute)
    first.append((_m("evil"), ("attacker",)))

    second = await cache.get_or_compute(_key(), compute)
    assert len(second) == 1
    assert second[0][0].text == "alice"


# ── LRU eviction ────────────────────────────────────────────────────────


async def test_lru_evicts_oldest() -> None:
    """Insert max_size + 1 distinct keys. The first one is evicted;
    a re-fetch runs compute again."""
    cache = InMemoryPipelineCache(2)
    calls = 0

    async def compute(text):
        nonlocal calls
        calls += 1
        return [(_m(text), ("regex",))]

    await cache.get_or_compute(_key("a"), lambda: compute("a"))  # miss → cache size 1
    await cache.get_or_compute(_key("b"), lambda: compute("b"))  # miss → cache size 2
    await cache.get_or_compute(_key("c"), lambda: compute("c"))  # miss + evicts "a"

    pre_calls = calls
    await cache.get_or_compute(_key("a"), lambda: compute("a"))  # miss again
    assert calls == pre_calls + 1


async def test_lru_recency_on_hit() -> None:
    """A hit on an existing key bumps its recency, so the next
    eviction takes a different victim."""
    cache = InMemoryPipelineCache(2)

    async def compute(text):
        return [(_m(text), ("regex",))]

    await cache.get_or_compute(_key("a"), lambda: compute("a"))
    await cache.get_or_compute(_key("b"), lambda: compute("b"))
    # Hit on "a" — moves it to most-recent.
    await cache.get_or_compute(_key("a"), lambda: compute("a"))
    # Insert "c" — should evict "b", not "a".
    await cache.get_or_compute(_key("c"), lambda: compute("c"))

    s = cache.stats()
    assert s["size"] == 2
    # "b" is gone (re-fetch is a miss)
    pre_misses = s["misses"]
    await cache.get_or_compute(_key("b"), lambda: compute("b"))
    assert cache.stats()["misses"] == pre_misses + 1


# ── put() prewarm path ─────────────────────────────────────────────────


async def test_put_populates_cold_slot() -> None:
    """`put` writes when the slot is missing. Subsequent
    `get_or_compute` for the same key hits without running compute."""
    cache = InMemoryPipelineCache(10)
    synth = [(_m("alice"), ("regex", "llm"))]

    await cache.put(_key(), synth)

    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return []

    got = await cache.get_or_compute(_key(), compute)
    assert calls == 0
    assert len(got) == 1
    assert got[0][0].text == "alice"
    assert got[0][1] == ("regex", "llm")


async def test_put_does_not_overwrite_populated_slot() -> None:
    """`put` is a setdefault: an already-populated slot stays. Pins
    the prewarm-doesn't-clobber-real-detection invariant the bidirectional
    cache rationale relies on."""
    cache = InMemoryPipelineCache(10)
    real = [(_m("from-real"), ("regex",))]

    async def compute():
        return real

    # Populate via get_or_compute first (real detection landed).
    await cache.get_or_compute(_key(), compute)

    # Prewarm tries to write a different value — should be ignored.
    synth = [(_m("from-synth"), ("llm",))]
    await cache.put(_key(), synth)

    got = await cache.get_or_compute(_key(), compute)
    assert got == real


async def test_put_disabled_cache_is_noop() -> None:
    """A disabled cache (max_size=0) silently ignores put. Otherwise
    the prewarm hook would burn cycles writing into a backing store
    that's about to drop the value."""
    cache = InMemoryPipelineCache(0)
    await cache.put(_key(), [(_m("alice"), ("regex",))])
    assert cache.stats() == {"size": 0, "max": 0, "hits": 0, "misses": 0}


# ── aclose ──────────────────────────────────────────────────────────────


async def test_in_memory_aclose_is_noop() -> None:
    """The in-memory backend has nothing to drain; aclose must
    complete without raising for the Protocol contract."""
    cache = InMemoryPipelineCache(10)
    await cache.aclose()


# ── Factory rejection ───────────────────────────────────────────────────


def test_factory_rejects_unknown_backend() -> None:
    """An unknown backend value crashes loudly at construction —
    boot-time misconfig, not a request-time surprise."""
    with pytest.raises(RuntimeError, match="Unknown PIPELINE_CACHE_BACKEND"):
        build_pipeline_cache(
            backend="bogus",  # type: ignore[arg-type]
            cache_max_size=10, cache_ttl_s=60,
        )
