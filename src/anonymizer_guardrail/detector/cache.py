"""
Reusable per-detector result cache.

Slow detectors (LLM, remote gliner, remote privacy filter) repeat work
in multi-turn conversations because each LiteLLM call replays the full
conversation history in `texts`. Caching a detector's result by input
text + the overrides that affect output skips the re-detection on
already-seen turns.

Default off — operators opt in per detector via that detector's
*_CACHE_MAX_SIZE env var. Caveat: caching a nondeterministic detector
(LLM) freezes the first-seen result for a given input until eviction;
turn-to-turn stability beats per-turn re-classification for chat
workloads, but it is a behaviour change worth surfacing.

Sizing note: pair with SURROGATE_CACHE_MAX_SIZE. A detector cache
hit that finds a stale surrogate-cache miss can re-derive a different
surrogate via the collision-retry path (surrogate.py), losing
cross-turn surrogate consistency. Operators enabling detector
caching should keep the surrogate cache comfortably above the
expected unique-entity count.

# Backends

`DetectorResultCache` is a Protocol the detector layer codes
against; concrete backends plug in below it. One backend ships
today: `InMemoryDetectionCache`, an in-process LRU. A
Redis-backed implementation is a candidate for when horizontal
scaling lands (see TASKS.md → Multi-replica support); the
detector code does not change when that arrives — only the
factory that constructs the cache instance.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Awaitable, Callable, Hashable, Protocol, runtime_checkable

from .base import Match


# Cached values are tuples so the cache can hand them back without
# defensive copying on every hit (callers receive a list — see get_or_compute).
_CacheValue = tuple[Match, ...]


@runtime_checkable
class DetectorResultCache(Protocol):
    """The contract a detector depends on for result caching.

    The Protocol is deliberately minimal: detectors don't need to know
    whether the storage is in-process, Redis, or anything else — they
    hand a hashable key + a coroutine that produces matches, and get
    back a fresh list of matches. Stats are surfaced on /health.

    `runtime_checkable` so a future test that wants to assert "this
    object satisfies the protocol" can use isinstance — costs nothing
    at runtime since the Protocol has no abstract methods.
    """

    @property
    def enabled(self) -> bool:
        """Whether the cache will actually store/return entries.
        A disabled cache (max_size <= 0 in the in-memory backend, or
        a Redis backend whose connection is degraded) is a passthrough
        for `get_or_compute`. Detectors do NOT need to gate on this
        themselves — `get_or_compute` short-circuits internally. Exposed
        for visibility (tests, /health, log lines)."""
        ...

    async def get_or_compute(
        self,
        key: Hashable,
        compute: Callable[[], Awaitable[list[Match]]],
    ) -> list[Match]:
        """Return cached matches for `key`, or run `compute()` and
        cache its result. Returned list is independent of cache
        state — callers (e.g. the pipeline's `_dedup`) can mutate
        it without poisoning subsequent reads."""
        ...

    def stats(self) -> dict[str, int]:
        """Snapshot of cache counters for the /health probe.
        Required keys: `size`, `max`, `hits`, `misses`. Backends MAY
        add their own keys; the pipeline only reads the four required
        ones today."""
        ...


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

    Process-local: not shared across replicas. See
    `DetectorResultCache` docstring + TASKS.md for the multi-replica
    plan if/when that lands.
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


__all__ = ["DetectorResultCache", "InMemoryDetectionCache"]
