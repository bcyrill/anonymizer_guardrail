"""
Reusable per-detector result cache — abstract surface.

Slow detectors (LLM, remote gliner, remote privacy filter) repeat work
in multi-turn conversations because each LiteLLM call replays the full
conversation history in `texts`. Caching a detector's result by input
text + the overrides that affect output skips the re-detection on
already-seen turns.

Default off — operators opt in per detector via that detector's
*_CACHE_MAX_SIZE env var (memory backend) or by setting
`<DETECTOR>_CACHE_BACKEND=redis` (redis backend). Caveat: caching a
nondeterministic detector (LLM) freezes the first-seen result for a
given input until eviction; turn-to-turn stability beats per-turn
re-classification for chat workloads, but it is a behaviour change
worth surfacing.

Sizing note: pair with SURROGATE_CACHE_MAX_SIZE. A detector cache
hit that finds a stale surrogate-cache miss can re-derive a different
surrogate via the collision-retry path (surrogate.py), losing
cross-turn surrogate consistency. Operators enabling detector
caching should keep the surrogate cache comfortably above the
expected unique-entity count.

This module owns the *interface* (`DetectorResultCache` Protocol),
the *cross-cutting cache-salt resolution* used by the redis backend,
and the *selection* (`build_detector_cache()` factory). Concrete
implementations each live in their own module:

  * `cache_memory.py` → `InMemoryDetectionCache` (default;
    process-local, zero deps).
  * `cache_redis.py` → `RedisDetectionCache` (opt-in via
    `<DETECTOR>_CACHE_BACKEND=redis`; shared store for multi-replica
    deployments and restart-persistence). Imports `redis-py` only
    when the backend is actually selected.

Selection happens in `build_detector_cache()` based on each detector's
own `cache_backend` config knob. The factory is the only place that
imports a concrete backend; detectors reference the Protocol type so
swapping doesn't ripple across the codebase.

Test layout mirrors this split: `tests/test_detector_cache.py` holds
the in-memory backend tests, `tests/test_detector_cache_redis.py`
holds the redis-specific tests + cross-backend factory tests.
"""

from __future__ import annotations

import logging
import secrets
from typing import Awaitable, Callable, Hashable, Literal, Protocol, runtime_checkable

from .base import Match


log = logging.getLogger("anonymizer.detector.cache")


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

    async def aclose(self) -> None:
        """Drain backend-side resources (e.g. Redis connection pool).
        Wired to `Pipeline.aclose` via the detector's `aclose()` so
        FastAPI shutdown closes Redis cleanly. No-op for in-memory
        backends; required by the Protocol so consumers don't need
        a `hasattr` guard."""
        ...


# ── Salt resolution ──────────────────────────────────────────────────────
# Mirrors `surrogate._resolve_salt`: empty operator value → fresh random
# bytes, non-empty → literal UTF-8. Truncated to BLAKE2b's max key length.
# Resolved once per process and cached so multi-detector deployments
# share the same salt without each reading `secrets.token_bytes`
# independently.
#
# Lives here (not in cache_redis.py) because it's cross-cutting — used
# only by the redis backend today, but conceptually a property of the
# cache subsystem rather than any one backend, and the factory below
# wires it in. Keeping it here also lets test code reach
# `_reset_resolved_salt_for_tests` without importing the redis module.
_MAX_BLAKE2B_KEY_LEN = 64
_resolved_salt: bytes | None = None


def _resolve_cache_salt(raw: str) -> bytes:
    """Operator-provided cache salt → bytes for BLAKE2b's `key`.

    Empty `raw` → 16 random bytes from `secrets.token_bytes` (logged
    once at INFO so operators see the rotation). Non-empty → the
    literal string, UTF-8-encoded and truncated to 64 bytes (BLAKE2b's
    max key length). Cached at module level so subsequent factory
    calls within the process see the same value — without that, every
    detector would generate a different random salt and Redis cache
    keys wouldn't collide across detectors deliberately, but they
    also wouldn't be reproducible after a process restart for
    diagnostic purposes."""
    global _resolved_salt
    if _resolved_salt is not None:
        return _resolved_salt
    if raw:
        _resolved_salt = raw.encode("utf-8")[:_MAX_BLAKE2B_KEY_LEN]
    else:
        _resolved_salt = secrets.token_bytes(16)
        log.info(
            "CACHE_SALT unset — generated a random 16-byte salt for "
            "this process. Set CACHE_SALT to a fixed value for "
            "multi-replica cache hits or restart-persistent caching."
        )
    return _resolved_salt


def _reset_resolved_salt_for_tests() -> None:
    """Test-only hook: clears the cached salt so tests that
    monkeypatch `CACHE_SALT` see fresh resolution. Not part of the
    public API."""
    global _resolved_salt
    _resolved_salt = None


# ── Factory ──────────────────────────────────────────────────────────────


def build_detector_cache(
    *,
    namespace: str,
    backend: Literal["memory", "redis"],
    cache_max_size: int,
    cache_ttl_s: int,
) -> DetectorResultCache:
    """Construct the configured cache backend for one detector.

    Reads cross-cutting fields (`cache_redis_url`, `cache_salt`) from
    the central config when `backend="redis"`; per-detector knobs
    (`cache_max_size`, `cache_ttl_s`) come in as kwargs. Detectors
    don't need to know which backend is in play — they hand a
    hashable to `get_or_compute` and the backend takes it from there.

    `namespace` is the detector's `spec.name` — used as the key prefix
    for the redis backend (`cache:<namespace>:<digest>`) so multiple
    detectors can share a Redis instance without colliding.

    Imports are deferred inside each branch — same pattern as
    `vault.build_vault()`. The redis import is the load-bearing one
    (defers `redis-py` for memory-only operators); the memory import
    is lazy too for symmetry, even though it has no extra cost.
    """
    if backend == "memory":
        from .cache_memory import InMemoryDetectionCache
        return InMemoryDetectionCache(cache_max_size)
    if backend == "redis":
        # Local imports defer the redis-py + central-config touch until
        # we know we need them. Tests that monkeypatch `cache_mod.config`
        # see the patched value because we re-resolve here on every
        # detector __init__.
        from ..config import config
        from .cache_redis import RedisDetectionCache

        if not config.cache_redis_url:
            raise RuntimeError(
                f"<DETECTOR>_CACHE_BACKEND=redis (namespace={namespace!r}) "
                "requires CACHE_REDIS_URL to be set. Either set the URL "
                "or switch to <DETECTOR>_CACHE_BACKEND=memory."
            )
        return RedisDetectionCache(
            namespace=namespace,
            url=config.cache_redis_url,
            ttl_s=cache_ttl_s,
            salt=_resolve_cache_salt(config.cache_salt),
        )
    raise RuntimeError(
        f"Unknown cache backend={backend!r} for namespace={namespace!r}. "
        "Allowed: memory, redis."
    )


__all__ = [
    "DetectorResultCache",
    "build_detector_cache",
    "resolve_cache_salt",
]


# Public alias for cross-module reuse — the pipeline-level cache
# (in `anonymizer_guardrail/pipeline_cache_redis.py`) needs the same
# operator-resolved salt so an operator's CACHE_SALT applies to both
# detector and pipeline caches uniformly. Kept as a thin wrapper rather
# than renaming `_resolve_cache_salt` so the existing `_reset_…_for_tests`
# hook stays internal.
def resolve_cache_salt(raw: str) -> bytes:
    """Public wrapper around `_resolve_cache_salt`. Same semantics
    (cached, random fallback, BLAKE2b-truncation) — exposed so the
    pipeline-cache redis backend can reuse the operator's CACHE_SALT
    without re-implementing the resolution logic."""
    return _resolve_cache_salt(raw)
