"""
In-memory `DetectorResultCache` implementation.

Default backend — selected when `<DETECTOR>_CACHE_BACKEND=memory`
(or left unset). Process-local OrderedDict + LRU cap, zero deps.
Suitable for single-replica deployments; for multi-replica or
restart-persistent caching, switch to `RedisDetectionCache` (in
`cache_redis.py`).

Lives in its own module for symmetry with `vault_memory.py` /
`vault_redis.py` — `cache.py` owns the Protocol + factory, each
backend ships in its own file. Unlike `cache_redis.py` there's no
import-cost win (the in-memory backend is pure stdlib), but the
parallel layout keeps the architecture consistent across the two
features.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Awaitable, Callable, Hashable

from .base import Match


# Cached values are tuples so the cache can hand them back without
# defensive copying on every hit (callers receive a list — see
# get_or_compute).
_CacheValue = tuple[Match, ...]


class InMemoryDetectionCache:
    """Process-wide LRU cache of detector-result tuples, keyed by an
    arbitrary hashable derived per-detector from (text, relevant overrides).

    Thread-safe via a single lock — same shape as `SurrogateGenerator`'s
    cache. Sub-millisecond hold time, so contention under realistic
    concurrency is invisible.

    Disabled state: when `max_size <= 0` the instance is a no-op
    (`get_or_compute` always calls through, `stats` reports zeros).
    Detectors construct one unconditionally in their __init__ and the
    `enabled` short-circuit avoids paying for storage churn when the
    cap is zero.

    Process-local: not shared across replicas and lost on restart.
    For multi-replica or restart-persistent caching, switch the
    detector's `cache_backend` to `redis` (see `cache_redis.py`).
    """

    def __init__(self, max_size: int) -> None:
        self._max_size = max(0, int(max_size))
        self._cache: OrderedDict[Hashable, _CacheValue] = OrderedDict()
        self._lock = threading.Lock()
        # Counters are plain ints — atomic to read/increment under the
        # GIL, no extra lock needed beyond the cache lock that already
        # serialises hit/miss bookkeeping.
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return self._max_size > 0

    async def get_or_compute(
        self,
        key: Hashable,
        compute: Callable[[], Awaitable[list[Match]]],
    ) -> list[Match]:
        """Return cached matches for `key`, or run `compute()` and cache
        its result. The returned list is a fresh copy on every hit —
        the cache keeps an immutable tuple internally so callers can
        mutate the returned list without poisoning the cache.

        When the cache is disabled (`max_size <= 0`) this is a thin
        passthrough — same async signature, no bookkeeping.

        The compute is run OUTSIDE the lock. Holding the lock across an
        LLM call (hundreds of ms) would serialise every cache miss and
        defeat the per-detector concurrency cap. Trade-off: a thundering
        herd on a cold key (N concurrent identical inputs all do the
        work, last writer wins). Acceptable — duplicate concurrent
        inputs are rare in detection workloads, and the result is the
        same regardless of who wins.
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


__all__ = ["InMemoryDetectionCache"]
