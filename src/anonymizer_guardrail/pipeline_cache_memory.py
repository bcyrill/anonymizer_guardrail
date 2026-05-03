"""
In-memory `PipelineResultCache` implementation.

Selected when `PIPELINE_CACHE_BACKEND=memory`. Process-local
OrderedDict + LRU cap, zero deps. Suitable for single-replica
deployments; for multi-replica or restart-persistent caching, switch
to `RedisPipelineCache` (in `pipeline_cache_redis.py`).

Lives in its own module for symmetry with `detector/cache_memory.py`
/ `cache_redis.py` and `vault_memory.py` / `vault_redis.py`.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from .detector.base import Match
from .pipeline_cache import MatchWithSources, PipelineCacheKey


log = logging.getLogger("anonymizer.pipeline.cache.memory")


# Cached values are tuples-of-tuples so the cache can hand them back
# without defensive copying on every hit (callers receive a list — see
# `get_or_compute`). Each entry is `((Match, sources_tuple), ...)`.
_CacheValue = tuple[MatchWithSources, ...]


class InMemoryPipelineCache:
    """Process-wide LRU cache of deduped match-list tuples, keyed by
    the full `PipelineCacheKey` shape.

    Thread-safe via a single lock — same shape as
    `detector.cache_memory.InMemoryDetectionCache`. Sub-millisecond
    hold time, so contention under realistic concurrency is invisible.

    Disabled state: when `max_size <= 0` the instance is a no-op
    (`get_or_compute` always calls through, `stats` reports zeros).
    Pipeline constructs the backend unconditionally and the `enabled`
    short-circuit avoids paying for storage churn when the cap is zero.

    Process-local: not shared across replicas and lost on restart.
    For multi-replica or restart-persistent caching, switch to
    `PIPELINE_CACHE_BACKEND=redis` (see `pipeline_cache_redis.py`).
    """

    def __init__(self, max_size: int) -> None:
        self._max_size = max(0, int(max_size))
        self._cache: OrderedDict[PipelineCacheKey, _CacheValue] = OrderedDict()
        self._lock = threading.Lock()
        # Counters are plain ints — atomic to read/increment under the
        # GIL, no extra lock needed beyond the cache lock that already
        # serialises hit/miss bookkeeping.
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return self._max_size > 0

    async def get_or_compute(self, key, compute):
        """Return cached matches for `key`, or run `compute()` and
        cache its result. The returned list is a fresh copy on every
        hit — the cache keeps an immutable tuple internally so callers
        can mutate the returned list without poisoning the cache.

        When the cache is disabled (`max_size <= 0`) this is a thin
        passthrough — same async signature, no bookkeeping.

        The compute is run OUTSIDE the lock. Holding the lock across
        an LLM call (hundreds of ms) would serialise every cache miss
        and defeat the per-detector concurrency cap. Trade-off: a
        thundering herd on a cold key (N concurrent identical inputs
        all do the work, last writer wins). Acceptable — the result
        is deterministic for a given key in this setup, so the
        last-writer-wins value is the same as any other.
        """
        if not self.enabled:
            return await compute()

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self._hits += 1
                return list(cached)
            self._misses += 1

        matches = await compute()

        with self._lock:
            self._cache[key] = tuple(matches)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
        return matches

    async def put(self, key, matches: list[MatchWithSources]) -> None:
        """Best-effort write used by the prewarm path. `setdefault`
        semantics: if a slot is already populated (e.g. real
        detection raced and won), the existing entry stays; the
        prewarm value is dropped. Real-detection results are
        deterministically equal to synthesised ones for the same key,
        so this is correctness-safe either way — but preserving the
        existing entry avoids spurious LRU-recency shuffles."""
        if not self.enabled:
            return
        with self._lock:
            if key in self._cache:
                # Don't overwrite — and don't bump LRU recency. A real
                # `get_or_compute` hit would touch recency, but a
                # silent prewarm shouldn't disturb the LRU order on
                # already-populated slots.
                return
            self._cache[key] = tuple(matches)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def stats(self) -> dict[str, int]:
        """Snapshot of cache counters for the /health probe.

        `len()` on a CPython dict is atomic and the int counters are
        read under the GIL, so the snapshot is lock-free. Values may
        be momentarily inconsistent across a concurrent put — that's
        acceptable for a stats endpoint.
        """
        return {
            "size": len(self._cache),
            "max": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
        }

    async def aclose(self) -> None:
        """No-op — there are no external resources to drain. Defined
        so the Protocol contract is satisfied uniformly across
        backends; consumers can call `await cache.aclose()` without
        a `hasattr` guard."""
        return None


__all__ = ["InMemoryPipelineCache"]
