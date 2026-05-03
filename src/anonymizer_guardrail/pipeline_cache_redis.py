"""
Redis-backed `PipelineResultCache` implementation.

Lives in its own module so the `redis` dependency only loads when
operators select `PIPELINE_CACHE_BACKEND=redis`. Operators install
the package via `pip install "anonymizer-guardrail[cache-redis]"`;
selecting the backend without installing the extra fails loud at
boot with a clear message.

# Wire format

  * **Key:** `pipeline:<digest>` where `<digest>` is a 32-byte
    BLAKE2b hex of `repr(cache_key)` keyed with the operator-resolved
    cache salt. Same scheme as the per-detector Redis cache (in
    `detector/cache_redis.py`) — a keyed digest closes the
    confirmation-attack oracle for read-only Redis access shapes.
    Salt comes from `CACHE_SALT` (or the random fallback resolved
    by `detector.cache.resolve_cache_salt`); the salt itself never
    reaches Redis.

    Distinct namespace from the per-detector caches
    (`cache:<detector>:<digest>`) so a `FLUSHDB` or `SCAN MATCH
    cache:*` operation on one doesn't wipe the other. They share
    `CACHE_REDIS_URL` and `CACHE_SALT` but live in separate keyspaces.
  * **Value:** JSON-encoded `list[{"text": str, "type": str}]`. Same
    shape as the per-detector cache value — operator-readable from
    `redis-cli GET pipeline:<digest>` and easy to deserialise.
  * **TTL:** per-key `EX` set on `SET`. Redis evicts expired entries
    server-side; no client-side eviction loop. Reset on every cache
    write — a frequently-hit key stays warm.

# Failure handling

If Redis is unreachable on either side of the round-trip, the cache
becomes a passthrough: `get_or_compute` calls through to compute,
nothing is cached, and a warning is logged at most once per minute.
`enabled` stays `True` — a degraded cache is not the same as a
degraded detector. Same posture as `RedisDetectionCache`.

This is deliberately *different* from `RedisVault`'s policy, which
fails the request when Redis is down. The vault is a correctness
mechanism (lost mappings ⇒ unredacted responses); the pipeline
cache is an efficiency mechanism (lost cache hits ⇒ slower
detection). Tying them together would force operators who only
need the cache feature to inherit the vault's stricter availability
requirements.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Awaitable, Callable

from .detector.base import Match
from .pipeline_cache import MatchWithSources, PipelineCacheKey


log = logging.getLogger("anonymizer.pipeline.cache.redis")


# Rate-limit interval for Redis-down warnings (seconds). One warning
# per minute — enough to surface the issue, not enough to drown other
# logs during an outage.
_WARN_INTERVAL_S = 60.0


class RedisPipelineCache:
    """Redis-backed `PipelineResultCache`. Constructed via
    `build_pipeline_cache(backend='redis', …)`; not intended for
    direct instantiation outside the factory and tests.

    One instance per pipeline. Connection pooling, retries, and
    reconnect-on-disconnect are delegated to `redis.asyncio.Redis`
    — same defaults as `RedisDetectionCache` and `RedisVault`.

    Thread-safety: redis-py's asyncio client is single-event-loop
    safe. The pipeline layer is fully async, so no extra
    synchronization is needed beyond what the client already
    provides.
    """

    _KEY_PREFIX = "pipeline:"

    def __init__(
        self,
        *,
        url: str,
        ttl_s: int,
        salt: bytes,
    ) -> None:
        # Local import (NOT module-level) so importing this module
        # doesn't require redis-py — operators on the memory backend
        # never construct a RedisPipelineCache and shouldn't pay for
        # the dep. Same pattern as `vault_redis.py`.
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:
            raise RuntimeError(
                "PIPELINE_CACHE_BACKEND=redis requires the 'redis' "
                "package. Install via "
                "`pip install \"anonymizer-guardrail[cache-redis]\"` "
                "or `pip install redis>=5.0` directly."
            ) from exc

        if not url:
            raise RuntimeError(
                "RedisPipelineCache requires a non-empty url. "
                "Set CACHE_REDIS_URL."
            )
        if not salt:
            # Defensive — the factory always resolves a non-empty salt
            # (random fallback) before calling us. Reject so the misuse
            # is loud.
            raise RuntimeError(
                "RedisPipelineCache requires a non-empty salt — the "
                "factory should resolve CACHE_SALT or fall back to a "
                "random one before constructing this."
            )

        self._url = url
        self._ttl_s = ttl_s
        self._salt = salt
        # `decode_responses=True` so we get `str` back from Redis.
        # Safe because we serialise via `json.dumps()` whose default
        # `ensure_ascii=True` emits pure ASCII.
        self._client = redis_asyncio.from_url(url, decode_responses=True)
        # Per-instance counters. Atomic to read/increment under the
        # GIL — same shape as `RedisDetectionCache`.
        self._hits = 0
        self._misses = 0
        # Last-warned timestamp for rate-limited Redis-down logging.
        # Monotonic so wall-clock changes don't reset the gate.
        self._last_warn_at = 0.0
        log.info(
            "RedisPipelineCache wired to %s (ttl=%ds). stats.size will "
            "report 0; query Redis directly via "
            "SCAN MATCH %s* COUNT N for the actual count.",
            url, ttl_s, self._KEY_PREFIX,
        )

    @property
    def enabled(self) -> bool:
        """Always True — operators picked the redis backend
        explicitly. The Redis-down passthrough is internal to
        `get_or_compute`, not gated on `enabled`."""
        return True

    def _redis_key(self, key: PipelineCacheKey) -> str:
        """Hash the pipeline cache key into a fixed-length Redis key.
        `repr()` is used because the in-memory backend already relies
        on `__hash__` over the same tuple shape; `repr()` is a
        deterministic stringification of the same content. The digest
        is BLAKE2b keyed with the operator-resolved cache salt — no
        plaintext input ever reaches Redis.

        Invariant on `key`: pipeline hands a tuple of stable-repr
        values (str, frozen tuple of strings, frozen tuple of
        `(str, frozen-pairs)`). Don't add dicts or sets to the
        cache-key tuple — their `repr()` ordering is implementation-
        dependent and would silently scatter cache hits."""
        blob = repr(key).encode("utf-8")
        digest = hashlib.blake2b(
            blob, key=self._salt, digest_size=32,
        ).hexdigest()
        return f"{self._KEY_PREFIX}{digest}"

    @staticmethod
    def _serialize(entries: list[MatchWithSources]) -> str:
        """Match-list-with-sources → JSON. Each entry is
        `{"text": str, "type": str, "sources": list[str]}`. The
        per-detector Redis cache uses `text/type` only; we add
        `sources` so a cache HIT at anonymize time can still populate
        the vault's `source_detectors` field. Operator-readable from
        `redis-cli GET pipeline:<…>`."""
        return json.dumps([
            {
                "text": m.text,
                "type": m.entity_type,
                "sources": list(sources),
            }
            for m, sources in entries
        ])

    @staticmethod
    def _deserialize(payload: str) -> list[MatchWithSources] | None:
        """JSON → list of (Match, source_detectors) pairs. Returns
        None on any parse failure or unexpected shape; callers treat
        None the same as a cache miss (and the bad entry is
        overwritten on the next compute)."""
        try:
            arr = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(arr, list):
            return None
        out: list[MatchWithSources] = []
        for entry in arr:
            if not isinstance(entry, dict):
                return None
            text = entry.get("text")
            etype = entry.get("type")
            sources = entry.get("sources", [])
            if not isinstance(text, str) or not isinstance(etype, str):
                return None
            if not isinstance(sources, list) or not all(
                isinstance(s, str) for s in sources
            ):
                return None
            out.append((Match(text=text, entity_type=etype), tuple(sources)))
        return out

    def _maybe_warn(self, op: str, exc: Exception) -> None:
        """Rate-limited Redis-down warning. One per
        `_WARN_INTERVAL_S` per cache instance — enough to surface the
        outage, not enough to flood logs during a flap. Stays at
        WARNING (not ERROR) because the pipeline is still serving
        requests; only cache hits are degraded.

        Same shape as `RedisDetectionCache._maybe_warn`. Non-locking
        read-modify-write on `_last_warn_at` is safe under
        single-event-loop asyncio (no awaits in this method)."""
        now = time.monotonic()
        if now - self._last_warn_at >= _WARN_INTERVAL_S:
            log.warning(
                "Redis pipeline cache %s failed: %s — cache temporarily "
                "passthrough; further warnings rate-limited to one per "
                "%ds.",
                op, exc, int(_WARN_INTERVAL_S),
            )
            self._last_warn_at = now

    async def get_or_compute(
        self,
        key: PipelineCacheKey,
        compute: Callable[[], Awaitable[list[MatchWithSources]]],
    ) -> list[MatchWithSources]:
        """Try Redis GET; on hit deserialize and return. On miss,
        compute via the pipeline closure and best-effort SET with TTL
        (NX so a thundering herd doesn't overwrite each other).
        On any Redis failure, fall through to compute() and skip the
        write — cache miss penalty + one logged warning, no request
        impact.

        Same shape as `RedisDetectionCache.get_or_compute`. The
        compute is run OUTSIDE the (implicit) Redis round-trip;
        thundering herd → all do the work, only the first SET wins.
        """
        redis_key = self._redis_key(key)

        try:
            payload = await self._client.get(redis_key)
        except Exception as exc:  # noqa: BLE001 — transport-agnostic catch
            self._maybe_warn("GET", exc)
            return await compute()

        if payload is not None:
            decoded = self._deserialize(payload)
            if decoded is not None:
                self._hits += 1
                return decoded
            log.error(
                "Redis pipeline cache entry at %s decoded to "
                "unexpected shape — treating as miss and overwriting.",
                redis_key,
            )

        self._misses += 1
        matches = await compute()

        try:
            await self._client.set(
                redis_key, self._serialize(matches),
                ex=self._ttl_s, nx=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._maybe_warn("SET", exc)

        return matches

    async def put(
        self, key: PipelineCacheKey, matches: list[MatchWithSources],
    ) -> None:
        """Best-effort prewarm write. Uses `SET nx=True` so an
        existing slot (real detection landed first) isn't overwritten
        — same posture as `get_or_compute`'s write path. Failures are
        rate-limit-warned and swallowed; prewarm is purely a latency
        optimisation for the next request."""
        redis_key = self._redis_key(key)
        try:
            await self._client.set(
                redis_key, self._serialize(matches),
                ex=self._ttl_s, nx=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._maybe_warn("PUT", exc)

    def stats(self) -> dict[str, int]:
        """Counter snapshot for `/health`. `size` and `max` are 0 by
        design (the SCAN to compute the actual count is async and
        `stats()` is sync); `hits` / `misses` are process-local."""
        return {
            "size": 0,
            "max": 0,
            "hits": self._hits,
            "misses": self._misses,
        }

    async def aclose(self) -> None:
        """Drain the Redis connection pool. Called by `Pipeline.aclose`
        on FastAPI shutdown. Wrapping in try/except — never fail
        shutdown over a cleanup error."""
        try:
            await self._client.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning("RedisPipelineCache aclose failed: %s", exc)


__all__ = ["RedisPipelineCache"]
