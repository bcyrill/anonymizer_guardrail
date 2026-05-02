"""
Redis-backed VaultBackend implementation.

Lives in its own module so the `redis` dependency only loads when
operators select `VAULT_BACKEND=redis`. Operators install the
package via `pip install "anonymizer-guardrail[vault-redis]"`;
selecting the backend without installing the extra fails loud at
boot with a clear message.

# Wire format

  * **Key:** `vault:{call_id}`. The `vault:` prefix is reserved so
    operators can safely share the Redis instance with other
    consumers (operator's choice — we recommend a dedicated logical
    DB regardless).
  * **Value:** JSON-encoded `dict[str, str]` (the surrogate→original
    mapping). JSON over msgpack because the entries are short, the
    debuggability via `redis-cli GET vault:<id>` matters more than
    bytes-on-the-wire, and pulling in another serialization
    dependency isn't justified for a string→string dict.
  * **TTL:** per-key `EXPIRE` set on `put`. Redis evicts expired
    entries server-side, so the lazy-eviction loop the in-memory
    backend has is unnecessary here.
  * **Atomic pop:** `GETDEL` (Redis ≥ 6.2). Single round-trip
    read-and-delete; no race between get/del where a concurrent
    request could see and re-pop the same entry.

# `size()`

Returns 0. Computing the actual count requires `SCAN MATCH vault:*`
which is async — but `size()` is the sync `/health` accessor.
Operators who need the in-flight count read it from Redis directly:
`DBSIZE` against a dedicated DB index, or `SCAN MATCH vault:* COUNT
1000` from `redis-cli`. Documented in `docs/operations.md`.

# Failure modes

  * **Redis unreachable on `put`:** the put raises `RedisVaultError`,
    `Pipeline.anonymize` propagates, `main.py` returns BLOCKED with
    the standard guardrail-failure path. The pre_call DOESN'T return
    a "GUARDRAIL_INTERVENED" with anonymized text whose mapping
    we couldn't store — that would ship surrogates that we can't
    later restore.
  * **Redis unreachable on `pop`:** also raises. The post_call sees
    a guardrail error and the response goes back with surrogates
    still in it. ERROR-logged; same severity as a TTL miss for the
    in-memory backend.

Both cases are bounded availability failures — the request is
bounded by the operator's LiteLLM-side retry policy, and the
guardrail's own `unreachable_fallback` doesn't apply to vault
errors (those are internal-state errors, distinct from "guardrail
endpoint is unreachable").
"""

from __future__ import annotations

import json
import logging

from .vault import VaultBackend  # type-only reference is fine, no cycle

log = logging.getLogger("anonymizer.vault.redis")


class RedisVaultError(RuntimeError):
    """Raised when Redis I/O fails on `put`/`pop`. Distinct from
    detector-side `*UnavailableError` — vault errors are internal
    state errors, not guardrail-endpoint reachability errors. The
    pipeline propagates them as request failures rather than
    routing through `unreachable_fallback`."""


class RedisVault:
    """Shared mapping store backed by Redis. Suitable for
    multi-replica deployments where the pre_call and post_call may
    land on different replicas behind a load balancer.

    Connection pooling, retries, and reconnect-on-disconnect are
    all delegated to `redis.asyncio.Redis` — the client's own
    defaults are sensible (auto-reconnect via the connection
    pool). Operators tune Redis-side concerns (max connections,
    timeouts, TLS) via the connection URL.
    """

    def __init__(self, *, url: str, ttl_s: int) -> None:
        # Local import — see module docstring. Operators on the
        # memory backend never reach here, so they don't need the
        # redis package installed even if they construct a Pipeline.
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:
            raise RuntimeError(
                "VAULT_BACKEND=redis requires the 'redis' package. "
                "Install via `pip install \"anonymizer-guardrail[vault-redis]\"` "
                "or `pip install redis>=5.0` directly."
            ) from exc

        if not url:
            # Defensive — should never happen because the central
            # Config validator catches this at boot. Keep the guard
            # so a programmatic constructor can't bypass validation.
            raise RuntimeError(
                "RedisVault requires a non-empty url. Set VAULT_REDIS_URL."
            )

        self._url = url
        self._ttl_s = ttl_s
        # `decode_responses=True` so we get str back from Redis
        # rather than bytes. The connection pool is created lazily
        # on first command; `aclose()` drains it.
        self._client = redis_asyncio.from_url(
            url,
            decode_responses=True,
        )
        log.info(
            "RedisVault wired to %s (ttl=%ds). Vault size on /health "
            "will report 0; query Redis directly for the actual count.",
            url, ttl_s,
        )

    @staticmethod
    def _key(call_id: str) -> str:
        """Namespaced key — the `vault:` prefix is reserved so this
        backend can share a Redis instance with other consumers
        without colliding. Operators are still encouraged to use a
        dedicated logical DB index (`/<n>` in the URL)."""
        return f"vault:{call_id}"

    async def put(self, call_id: str, mapping: dict[str, str]) -> None:
        """SET <key> <json> EX <ttl>. Single round-trip; Redis evicts
        on expiry server-side, no client-side scheduling needed."""
        if not call_id or not mapping:
            return
        try:
            payload = json.dumps(mapping)
            await self._client.set(self._key(call_id), payload, ex=self._ttl_s)
        except Exception as exc:
            # Wrap in a typed error so the pipeline's BLOCKED path
            # has a clean discriminator; the underlying redis-py
            # exceptions vary by error class (ConnectionError,
            # TimeoutError, ResponseError, ...) and we don't want
            # them to leak into the BLOCKED handler.
            raise RedisVaultError(
                f"Failed to store vault entry for call_id={call_id!r}: {exc}"
            ) from exc

    async def pop(self, call_id: str) -> dict[str, str]:
        """Atomic GETDEL. Returns `{}` if absent or already-popped /
        TTL-expired (Redis returns nil and the key is gone server-side).

        GETDEL is a single round-trip and atomic; using GET + DEL would
        race with concurrent pops from another replica, where two
        post_calls for the same call_id could both see the entry. With
        GETDEL only one wins; the other gets {}.
        """
        if not call_id:
            return {}
        try:
            payload = await self._client.getdel(self._key(call_id))
        except Exception as exc:
            raise RedisVaultError(
                f"Failed to retrieve vault entry for call_id={call_id!r}: {exc}"
            ) from exc

        if payload is None:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            # Corrupted entry — log ERROR, return empty so the
            # post_call doesn't crash but the operator sees the
            # data-loss-ish severity. Same logging contract as
            # MemoryVault's TTL-expiry case.
            log.error(
                "Vault entry for call_id=%s decoded to non-JSON — "
                "deanonymise dropped, surrogates still in response. "
                "Inspect Redis manually or rotate the call_id namespace.",
                call_id,
            )
            return {}

    def size(self) -> int:
        """Returns 0. See module docstring — `/health` exposes this
        for the in-memory backend's exact length; the Redis backend's
        true count requires an async SCAN that doesn't fit a sync
        accessor. Operators query Redis directly when they need it."""
        return 0

    async def aclose(self) -> None:
        """Drain the connection pool. Wired to `Pipeline.aclose` so
        FastAPI shutdown closes Redis cleanly."""
        try:
            await self._client.aclose()
        except Exception as exc:  # noqa: BLE001 — never fail shutdown
            log.warning("RedisVault aclose failed: %s", exc)


__all__ = ["RedisVault", "RedisVaultError"]
