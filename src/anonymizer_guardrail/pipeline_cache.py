"""
Pipeline-level result cache — abstract surface.

This cache sits ABOVE the per-detector caches and stores the *deduped*
match list keyed by the full request shape: `(text, detector_mode,
frozen_per_call_kwargs)`. Two write paths populate it:

  * **Detection-side write**: after `_run_detection` runs the active
    detectors and `_dedup` collapses the per-detector match lists, the
    result lands in the pipeline cache so the next request for the
    same text + same kwargs hits in one shot — no detector dispatch.
  * **Deanonymize-side pre-warm**: `pipeline.deanonymize` synthesises
    matches from the typed vault entries (`VaultEntry.surrogates`)
    and writes them into the pipeline cache slot keyed by the same
    `(text, detector_mode, kwargs)` shape reconstructed from the same
    vault entry. The next request that contains the same restored
    text gets a one-shot cache hit, regardless of whether the original
    anonymize call used Faker or opaque tokens.

Default off (`PIPELINE_CACHE_BACKEND=none`) — operators opt in
explicitly. When the per-detector caches are also live, both layers
participate: pipeline cache is the fast path (one lookup, no detector
dispatch), per-detector caches are the structural backup tier when
override fragmentation lands keys in different pipeline-cache slots
(the per-detector cache key only fragments by *that* detector's
kwargs, not the union).

This module owns the *interface* (`PipelineResultCache` Protocol),
the *cache-key shape* (`PipelineCacheKey`), and the *selection*
(`build_pipeline_cache()` factory). Concrete implementations live in:

  * `pipeline_cache_memory.py` → `InMemoryPipelineCache` (default
    when backend != "none"; process-local, zero deps).
  * `pipeline_cache_redis.py` → `RedisPipelineCache` (opt-in via
    `PIPELINE_CACHE_BACKEND=redis`; shared store for multi-replica
    deployments and restart-persistence). Imports `redis-py` only
    when the backend is actually selected.

The shape mirrors the per-detector cache surface (`detector/cache.py`
+ `cache_memory.py` + `cache_redis.py`) — same Protocol contract,
same factory pattern, same operator-config salt resolution.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Hashable, Literal, Protocol, runtime_checkable

from .detector.base import Match
from .vault import FrozenCallKwargs


log = logging.getLogger("anonymizer.pipeline.cache")


# Cache-key shape. The frozen kwargs slot is the SAME `FrozenCallKwargs`
# tuple that lives on `VaultEntry.kwargs` — so a key built at detection
# time and a key reconstructed at deanonymize-prewarm time hash to the
# same value when the call's kwargs match. Don't add unsorted iterables
# (dict, set) to this tuple; their `repr()` ordering is implementation-
# dependent and would silently scatter cache hits across distinct keys
# for the same logical input.
PipelineCacheKey = tuple[str, tuple[str, ...], FrozenCallKwargs]


# Cache value: each cached match carries the tuple of detectors that
# independently produced it (pre-`_dedup`). Needed so a cache HIT at
# anonymize time can still populate the vault entry's per-surrogate
# `source_detectors` field — without that, deanonymize-side per-detector
# cache prewarm couldn't filter the synthesised match list per detector.
#
# Tuple over a richer dataclass to keep the JSON wire format trivial:
# `[text, type, [sources]]` round-trips through Redis without a schema
# layer.
MatchWithSources = tuple[Match, tuple[str, ...]]


@runtime_checkable
class PipelineResultCache(Protocol):
    """The contract pipeline-cache backends satisfy.

    Mirrors `DetectorResultCache` exactly — same `enabled`,
    `get_or_compute`, `stats`, `aclose` shape — so the pipeline can
    treat the two cache layers uniformly except for their position
    in the dispatch chain.

    `runtime_checkable` so a future test that wants to assert "this
    object satisfies the protocol" can use `isinstance` — costs
    nothing at runtime since the Protocol has no abstract methods.
    """

    @property
    def enabled(self) -> bool:
        """Whether the cache will store/return entries. A disabled
        cache is a passthrough for `get_or_compute`. Pipeline does
        NOT need to gate on this — the `get_or_compute` body
        short-circuits internally. Exposed for visibility (tests,
        /health, log lines)."""
        ...

    async def get_or_compute(
        self,
        key: PipelineCacheKey,
        compute: Callable[[], Awaitable[list[MatchWithSources]]],
    ) -> list[MatchWithSources]:
        """Return cached matches for `key`, or run `compute()` and
        cache its result. Returned list is independent of cache
        state — callers (e.g. the pipeline's downstream stages) can
        mutate it without poisoning subsequent reads."""
        ...

    async def put(
        self, key: PipelineCacheKey, matches: list[MatchWithSources],
    ) -> None:
        """Best-effort write — populates the slot if missing. Used by
        the deanonymize-side prewarm path, which doesn't need the
        get-or-compute round-trip semantics. On a populated slot, the
        existing entry stays (Redis: SET nx=True; in-memory: setdefault
        equivalent). Empty match lists are still written — they're a
        valid "we ran detection, found nothing" answer."""
        ...

    def stats(self) -> dict[str, int]:
        """Snapshot of cache counters for the /health probe.
        Required keys: `size`, `max`, `hits`, `misses`. Backends MAY
        add their own keys; the pipeline only reads the four required
        ones today."""
        ...

    async def aclose(self) -> None:
        """Drain backend-side resources (e.g. Redis connection pool).
        Wired to `Pipeline.aclose` so FastAPI shutdown closes Redis
        cleanly. No-op for in-memory backends; required by the
        Protocol so consumers don't need a `hasattr` guard."""
        ...


# ── Sentinel: "cache disabled" passthrough ────────────────────────────


class _DisabledPipelineCache:
    """No-op implementation used when `PIPELINE_CACHE_BACKEND=none`
    (the default). `get_or_compute` calls through to the compute
    callable without storing anything; `put` is a no-op; stats
    report all zeros.

    Lets `Pipeline._detect_one` always wrap with the cache without
    a `if cache is None` branch — fewer lines of pipeline code,
    fewer chances of forgetting to apply the wrap somewhere."""

    @property
    def enabled(self) -> bool:
        return False

    async def get_or_compute(
        self,
        key: PipelineCacheKey,
        compute: Callable[[], Awaitable[list[MatchWithSources]]],
    ) -> list[MatchWithSources]:
        return await compute()

    async def put(
        self, key: PipelineCacheKey, matches: list[MatchWithSources],
    ) -> None:
        return None

    def stats(self) -> dict[str, int]:
        return {"size": 0, "max": 0, "hits": 0, "misses": 0}

    async def aclose(self) -> None:
        return None


# ── Factory ───────────────────────────────────────────────────────────


def build_pipeline_cache(
    *,
    backend: Literal["none", "memory", "redis"],
    cache_max_size: int,
    cache_ttl_s: int,
) -> PipelineResultCache:
    """Construct the configured pipeline cache backend.

    Reads cross-cutting fields (`cache_redis_url`, `cache_salt`) from
    the central config when `backend="redis"`. `cache_max_size` is the
    LRU cap for the in-memory backend (ignored by Redis — operators
    bound Redis memory via server-side `maxmemory`). `cache_ttl_s`
    is the per-key EXPIRE for Redis (ignored by the in-memory backend,
    which uses the LRU cap as its sole bound).

    `backend="none"` returns a no-op cache so the pipeline can wrap
    every dispatch unconditionally — keeps the hot-path code free of
    `if cache is None` branches.

    Imports are deferred inside each branch — same pattern as
    `vault.build_vault()` and `detector.cache.build_detector_cache()`.
    The redis import is the load-bearing one (defers `redis-py` for
    memory-only operators); the memory import is lazy too for
    symmetry, even though it has no extra cost.
    """
    if backend == "none":
        return _DisabledPipelineCache()
    if backend == "memory":
        from .pipeline_cache_memory import InMemoryPipelineCache
        return InMemoryPipelineCache(cache_max_size)
    if backend == "redis":
        # Local imports defer the redis-py + central-config touch until
        # we know we need them. Same shape as `build_detector_cache`.
        from .config import config
        from .detector.cache import resolve_cache_salt
        from .pipeline_cache_redis import RedisPipelineCache

        if not config.cache_redis_url:
            raise RuntimeError(
                "PIPELINE_CACHE_BACKEND=redis requires CACHE_REDIS_URL "
                "to be set. Either set the URL or switch to "
                "PIPELINE_CACHE_BACKEND=memory."
            )
        return RedisPipelineCache(
            url=config.cache_redis_url,
            ttl_s=cache_ttl_s,
            salt=resolve_cache_salt(config.cache_salt),
        )
    raise RuntimeError(
        f"Unknown PIPELINE_CACHE_BACKEND={backend!r}. "
        "Allowed: none, memory, redis."
    )


__all__ = [
    "MatchWithSources",
    "PipelineCacheKey",
    "PipelineResultCache",
    "build_pipeline_cache",
]
