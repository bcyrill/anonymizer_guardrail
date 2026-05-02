"""
Redis-backed `DetectorResultCache` implementation.

Lives in its own module so the `redis` dependency only loads when at
least one detector selects `<DETECTOR>_CACHE_BACKEND=redis`. Operators
install the package via `pip install "anonymizer-guardrail[cache-redis]"`;
selecting the backend without installing the extra fails loud at boot
with a clear message.

# Wire format

  * **Key:** `cache:<namespace>:<digest>` where `<namespace>` is the
    detector's `spec.name` (e.g. `llm`, `privacy_filter`, `gliner_pii`)
    and `<digest>` is a 32-byte BLAKE2b hex of `repr(cache_key)` keyed
    with the operator-resolved cache salt. The digest is one-way — no
    plaintext input survives in Redis keys, even with read access.

    Why a *keyed* hash and not a plain hash: the cache key tuple
    contains raw user input (text, model, prompt). Without a secret
    salt, anyone with `SCAN`/`KEYS` access to Redis could mount a
    confirmation attack — pick a candidate input X, compute
    `BLAKE2b(repr((X, …)))` locally with the same public code path,
    and check Redis to confirm whether X was processed within the
    TTL window. Low-entropy inputs (9-digit SSNs, email lists) make
    this cheap. Salting the digest with `CACHE_SALT` (server-side
    state never written to Redis) closes the oracle under any access
    shape that surfaces keys but not values — sysadmin SCANs,
    metrics agents, ACL-restricted clients (`+@keyspace -@string`),
    backup snapshots. Cache *values* leak content too, so this is
    primarily defence-in-depth against partial-access shapes.

    The vault module does NOT use this scheme — its `vault:<call_id>`
    keys are derived from a LiteLLM-minted random UUID with no content
    correlation, so direct passthrough is safe and keeps
    `redis-cli GET vault:<call_id>` available for oncall debugging.
    Rule of thumb: hash + salt any Redis key derived from sensitive
    content; pass through any Redis key that's already a random
    opaque token from elsewhere in the system.
  * **Value:** JSON-encoded `list[{"text": str, "type": str}]`. JSON
    over msgpack for the same reasons as `RedisVault`: the entries are
    short, debuggability via `redis-cli GET cache:<…>` matters more
    than bytes, and we already depend on the stdlib `json` module.
  * **TTL:** per-key `EX` set on `SET`. Redis evicts expired entries
    server-side; no client-side eviction loop. Reset on every cache
    write — a frequently-hit key stays warm.

# `enabled`

Returns `True` unconditionally — operators selected the redis backend,
they want caching. The Redis-down passthrough is internal to
`get_or_compute`; consumers don't need to gate on it.

# Stats

  * `size`: 0 (placeholder, same rationale as `RedisVault.size()` —
    `SCAN MATCH cache:<ns>:* COUNT N` is async; `stats()` is sync).
    Operators query Redis directly when they need the actual count.
  * `max`: 0 — there is no per-detector cap; Redis-side `maxmemory`
    bounds the global footprint. Documented asymmetry vs the in-memory
    backend, where `max` reports the configured LRU cap.
  * `hits` / `misses`: process-local counters. They reflect this
    instance's traffic, not cluster-wide totals — different replicas
    report different numbers, which is what an operator actually wants
    (per-replica cache effectiveness).

# Failure handling

If Redis is unreachable on either side of the round-trip, the cache
becomes a passthrough: `get_or_compute` calls through to the detector,
nothing is cached, and a warning is logged at most once per minute per
namespace so a flap doesn't spam logs. `enabled` stays `True` — a
degraded cache is not the same as a degraded detector, and the
detector itself is still serving requests.

This is deliberately *different* from the vault Redis policy, which
fails the request when Redis is down. The vault is a correctness
mechanism (lost mappings ⇒ unredacted responses); the cache is an
efficiency mechanism (lost cache hits ⇒ slower detection). Tying them
together would force operators who only need the cache feature to
inherit the vault's stricter availability requirements.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Awaitable, Callable, Hashable

from .base import Match


log = logging.getLogger("anonymizer.detector.cache.redis")


# Rate-limit interval for Redis-down warnings (seconds). One warning
# per namespace per minute — enough to surface the issue, not enough
# to drown other logs during an outage.
_WARN_INTERVAL_S = 60.0


class RedisDetectionCache:
    """Redis-backed `DetectorResultCache`. Constructed via
    `build_detector_cache(backend='redis', …)`; not intended for
    direct instantiation outside the factory and tests.

    One instance per detector — they share the underlying Redis
    instance but each carries its own namespace prefix and per-process
    hit/miss counters.

    Thread-safety: redis-py's asyncio client is single-event-loop
    safe. The detector layer is fully async, so no extra synchronization
    is needed beyond what the client already provides.
    """

    _KEY_PREFIX = "cache:"

    def __init__(
        self,
        *,
        namespace: str,
        url: str,
        ttl_s: int,
        salt: bytes,
    ) -> None:
        # Local import (NOT module-level) so importing this module
        # doesn't require redis-py — operators on the memory backend
        # never construct a RedisDetectionCache and shouldn't pay for
        # the dep. Same pattern as `vault_redis.py`.
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:
            raise RuntimeError(
                f"<DETECTOR>_CACHE_BACKEND=redis (namespace={namespace!r}) "
                "requires the 'redis' package. Install via "
                "`pip install \"anonymizer-guardrail[cache-redis]\"` "
                "or `pip install redis>=5.0` directly."
            ) from exc

        if not namespace:
            raise RuntimeError(
                "RedisDetectionCache requires a non-empty namespace "
                "(typically the detector's spec.name)."
            )
        if not url:
            raise RuntimeError(
                "RedisDetectionCache requires a non-empty url. "
                "Set CACHE_REDIS_URL."
            )
        if not salt:
            # Defensive — the factory always resolves a non-empty salt
            # (random fallback) before calling us. An empty salt would
            # leave BLAKE2b unkeyed which is fine cryptographically but
            # surprising operationally; reject so the misuse is loud.
            raise RuntimeError(
                "RedisDetectionCache requires a non-empty salt — the "
                "factory should resolve CACHE_SALT or fall back to a "
                "random one before constructing this."
            )

        self._namespace = namespace
        self._url = url
        self._ttl_s = ttl_s
        self._salt = salt
        # See vault_redis.py — `decode_responses=True` so we get `str`
        # back from Redis. Safe because we serialise via json.dumps()
        # whose default `ensure_ascii=True` emits pure ASCII.
        self._client = redis_asyncio.from_url(url, decode_responses=True)
        # Per-instance counters. Atomic to read/increment under the
        # GIL — same shape as InMemoryDetectionCache.
        self._hits = 0
        self._misses = 0
        # Last-warned timestamp for rate-limited Redis-down logging.
        # Monotonic so wall-clock changes don't reset the gate.
        self._last_warn_at = 0.0
        log.info(
            "RedisDetectionCache wired for namespace=%r url=%s ttl=%ds "
            "(stats.size will report 0; query Redis directly via "
            "SCAN MATCH %s%s:* COUNT N).",
            namespace, url, ttl_s, self._KEY_PREFIX, namespace,
        )

    @property
    def enabled(self) -> bool:
        """Always True — operators picked the redis backend explicitly.
        The Redis-down passthrough is handled inside `get_or_compute`,
        not by gating on `enabled`."""
        return True

    def _redis_key(self, key: Hashable) -> str:
        """Hash the per-detector cache key into a fixed-length Redis
        key. `repr()` is used because the in-memory backend already
        relies on `__hash__` over the same tuple shape; `repr()` is a
        deterministic stringification of the same content. The digest
        is BLAKE2b keyed with the operator-resolved cache salt — no
        plaintext input ever reaches Redis.

        Invariant on `key`: detectors hand us a tuple of stable-repr
        values (str, int, float, bool, None, frozen tuple). Don't add
        dicts, sets, or unsorted iterables to the cache-key tuple — their
        `repr()` ordering is implementation-dependent and would silently
        scatter cache hits across distinct digests for the same logical
        input. Today's three detectors (LLM/PF/gliner) all use plain
        string tuples; this comment exists for future detector authors."""
        blob = repr(key).encode("utf-8")
        digest = hashlib.blake2b(
            blob, key=self._salt, digest_size=32,
        ).hexdigest()
        return f"{self._KEY_PREFIX}{self._namespace}:{digest}"

    @staticmethod
    def _serialize(matches: list[Match]) -> str:
        """Match list → JSON. Match is a frozen dataclass with two
        strings; round-trip is trivial. The wire field is `type`
        (matching the LLM detector's outbound JSON contract) so an
        operator inspecting `redis-cli GET cache:llm:…` sees a
        familiar shape."""
        return json.dumps(
            [{"text": m.text, "type": m.entity_type} for m in matches]
        )

    @staticmethod
    def _deserialize(payload: str) -> list[Match] | None:
        """JSON → Match list. Returns None on any parse failure or
        unexpected shape; callers treat None the same as a cache miss
        (and the bad entry is overwritten on the next compute)."""
        try:
            arr = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(arr, list):
            return None
        out: list[Match] = []
        for entry in arr:
            if not isinstance(entry, dict):
                return None
            text = entry.get("text")
            etype = entry.get("type")
            if not isinstance(text, str) or not isinstance(etype, str):
                return None
            out.append(Match(text=text, entity_type=etype))
        return out

    def _maybe_warn(self, op: str, exc: Exception) -> None:
        """Rate-limited Redis-down warning. One per `_WARN_INTERVAL_S`
        per cache instance — enough to surface the outage, not enough
        to flood logs during a flap. Stays at WARNING (not ERROR)
        because the detector itself is still serving requests; only
        cache hits are degraded.

        Thread-safety: read-then-assign on `self._last_warn_at` is not
        synchronised. The detector layer is async-only on a single
        event loop, so concurrent calls are interleaved at await
        boundaries — `_maybe_warn` itself does no awaits, so a single
        invocation runs atomically from the loop's perspective. If the
        guardrail ever moves to running detection on a thread pool
        (today only sync FastAPI handlers do), this would need a lock.
        """
        now = time.monotonic()
        if now - self._last_warn_at >= _WARN_INTERVAL_S:
            log.warning(
                "Redis cache %s failed for namespace=%r: %s — "
                "cache temporarily passthrough; further warnings "
                "rate-limited to one per %ds.",
                op, self._namespace, exc, int(_WARN_INTERVAL_S),
            )
            self._last_warn_at = now

    async def get_or_compute(
        self,
        key: Hashable,
        compute: Callable[[], Awaitable[list[Match]]],
    ) -> list[Match]:
        """Try Redis GET; on hit deserialize and return. On miss,
        compute via the detector closure and best-effort SET with TTL.
        On any Redis failure, fall through to compute() and skip the
        write — cache miss penalty + one logged warning, no request
        impact.

        The compute is run OUTSIDE the (implicit) Redis round-trip.
        Holding back compute on the Redis call would serialise every
        cache miss behind Redis I/O latency — defeats the purpose.
        Trade-off: a thundering herd on a cold key (N concurrent
        identical inputs all do the work, last writer wins). Same
        trade-off as the in-memory backend; same justification.
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
            # Bad payload — log once, treat as miss, overwrite below.
            log.error(
                "Redis cache entry at %s decoded to unexpected shape — "
                "treating as miss and overwriting.",
                redis_key,
            )

        self._misses += 1
        matches = await compute()

        try:
            # `nx=True` makes the SET conditional on the key being
            # absent. Under a thundering herd on a cold key, N
            # concurrent identical computes all reach this point with
            # the same matches — only the first SET wins; the rest no-op
            # without overwriting. Saves N-1 wasted Redis writes (and
            # the matching N-1 TTL resets) for free, since all the
            # computes converge on the same value anyway.
            await self._client.set(
                redis_key, self._serialize(matches),
                ex=self._ttl_s, nx=True,
            )
        except Exception as exc:  # noqa: BLE001
            # Set failed — return the freshly-computed matches anyway,
            # log rate-limited warning. Next request for this key pays
            # the miss again, no correctness impact.
            self._maybe_warn("SET", exc)

        return matches

    def stats(self) -> dict[str, int]:
        """Counter snapshot for `/health`. `size` and `max` are 0 by
        design (see module docstring); `hits` / `misses` are
        process-local."""
        return {
            "size": 0,
            "max": 0,
            "hits": self._hits,
            "misses": self._misses,
        }

    async def aclose(self) -> None:
        """Drain the Redis connection pool. Called by `Pipeline.aclose`
        on FastAPI shutdown via the detector's `aclose()`. Wrapping in
        try/except — never fail shutdown over a cleanup error."""
        try:
            await self._client.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "RedisDetectionCache aclose failed for namespace=%r: %s",
                self._namespace, exc,
            )


__all__ = ["RedisDetectionCache"]
