"""
Mapping store keyed by `litellm_call_id`.

The pre_call writes the surrogate→original mapping; the matching
post_call reads and evicts it. Entries are TTL'd in case the
post_call never arrives (request errored out, client disconnected,
etc.) so memory doesn't grow unbounded.

# Backends

`VaultBackend` is the Protocol the pipeline codes against; concrete
implementations plug in below it. Two backends ship today:

  * `MemoryVault` (default) — process-local, zero dependencies.
    Suitable for single-replica deployments. The pre_call and
    post_call MUST land on the same replica or the mapping is lost.
  * `RedisVault` — shared store; suitable for multi-replica
    deployments behind a load balancer. Lives in
    `vault_redis.py` so the redis-py dependency only loads when
    actually selected (operators install via `pip install
    "anonymizer-guardrail[vault-redis]"`).

Selection happens in `build_vault()` based on `VAULT_BACKEND`. The
factory is the only place that imports a concrete backend; the
pipeline references the Protocol type so swapping doesn't ripple
across the codebase.

`Vault` is kept as a name alias for `MemoryVault` so existing
imports of `from anonymizer_guardrail.vault import Vault` keep
working.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Protocol, runtime_checkable

from .config import config

log = logging.getLogger("anonymizer.vault")


@runtime_checkable
class VaultBackend(Protocol):
    """Round-trip mapping store contract.

    Implementations: `MemoryVault` (in-process, default) and
    `RedisVault` (in `vault_redis.py`, opt-in via
    `VAULT_BACKEND=redis`).

    `put` and `pop` are async because Redis I/O is fundamentally
    async; bouncing through a thread pool would defeat the
    event-loop architecture for what's a hot-path operation
    (called twice per request: pre_call + post_call). MemoryVault
    implements them as thin async wrappers around its sync body
    so the contract is uniform.

    `size` stays sync — it's a `/health` accessor, not on the
    request hot path; for MemoryVault it's a `len()` call, for
    RedisVault it's a documented placeholder (operators query
    Redis directly for the actual count, e.g. `DBSIZE` on a
    dedicated DB index).

    `runtime_checkable` so a future test can `isinstance`-narrow a
    backend if needed; costs nothing at runtime.
    """

    async def put(self, call_id: str, mapping: dict[str, str]) -> None:
        """Store the surrogate→original mapping for one call. Empty
        mapping or empty call_id is a no-op."""
        ...

    async def pop(self, call_id: str) -> dict[str, str]:
        """Retrieve and remove the mapping. Returns `{}` if absent or
        expired. Idempotent — second pop with the same call_id
        returns `{}`."""
        ...

    def size(self) -> int:
        """In-flight entry count, surfaced as `vault_size` on
        `/health`. MemoryVault returns the exact length;
        RedisVault returns 0 — operators read the actual count via
        `DBSIZE` or `SCAN MATCH vault:*` against the configured
        Redis DB."""
        ...

    async def aclose(self) -> None:
        """Drain any open connections. Wired to `Pipeline.aclose`
        so FastAPI shutdown closes Redis pools cleanly. No-op for
        MemoryVault."""
        ...


class MemoryVault:
    """Process-local mapping store backed by an OrderedDict + TTL +
    LRU cap. Default backend; suitable for single-replica
    deployments.

    Multi-replica deployments need `RedisVault` (see
    `vault_redis.py`) — the pre_call and post_call must land on the
    same replica or the mapping is lost. See
    `docs/limitations.md` for the broader story.
    """

    def __init__(
        self, ttl_s: int | None = None, max_entries: int | None = None
    ) -> None:
        self._ttl_s = ttl_s if ttl_s is not None else config.vault_ttl_s
        # Hard cap as a backstop against unbounded growth when callers
        # never issue the matching post_call (TTL eventually clears them,
        # but a burst of unique call_ids can still blow the heap before
        # then). max(1, …) so a typoed 0/negative value can't disable
        # writes entirely.
        self._max_entries = max(
            1,
            max_entries if max_entries is not None else config.vault_max_entries,
        )
        # OrderedDict so LRU eviction is O(1) via popitem(last=False).
        # Insertion order is preserved (matching the existing
        # _evict_expired_locked assumption that older entries sit at
        # the front).
        self._store: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
        self._lock = threading.Lock()

    async def put(self, call_id: str, mapping: dict[str, str]) -> None:
        """Store the surrogate→original mapping for one call.

        Async-by-protocol; the body is fully synchronous (in-memory
        dict + threading.Lock). The lock is sub-microsecond so the
        coroutine doesn't yield — it returns to the caller immediately
        after the dict update."""
        if not call_id or not mapping:
            return
        with self._lock:
            self._evict_expired_locked()
            # Remove and re-insert to ensure this call_id is at the end of
            # the dict (insertion order), keeping the chronological assumption
            # valid for _evict_expired_locked.
            self._store.pop(call_id, None)
            self._store[call_id] = (time.monotonic(), dict(mapping))
            # LRU backstop in case TTL eviction hasn't reclaimed enough
            # space (e.g. flood of unique call_ids well within TTL).
            # Evicted entries lose their mapping — the matching post_call
            # will deanonymize nothing, surfacing as the same warning the
            # TTL path emits. Preferable to OOM.
            evicted = 0
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
                evicted += 1
            if evicted:
                log.warning(
                    "Vault over capacity (%d entries); LRU-evicted %d. "
                    "Raise VAULT_MAX_ENTRIES if this is sustained traffic, "
                    "not a flood.",
                    self._max_entries, evicted,
                )

    async def pop(self, call_id: str) -> dict[str, str]:
        """Retrieve and remove the mapping for a call. Returns {} if absent/expired.

        Expiry is logged at ERROR (not WARNING) because it means the
        post_call arrived AFTER the vault TTL fired, so the
        deanonymisation that the operator presumably wanted has
        silently failed: the response shipped with `[PERSON_…]`
        surrogates still in it. That's data-loss-ish — the original
        request's mapping is gone for good. WARNING reads as benign;
        ERROR puts it on the same severity as other detector
        unavailability so an operator monitoring `error` lines sees
        it.

        Absent / no call_id returns silently — that's the no-op
        path (`input_type=request` without a matching `response`,
        or a request that intentionally bypasses the vault).
        """
        if not call_id:
            return {}
        with self._lock:
            entry = self._store.pop(call_id, None)
        if entry is None:
            return {}
        ts, mapping = entry
        if time.monotonic() - ts > self._ttl_s:
            log.error(
                "Vault entry for call_id=%s expired before post_call — "
                "deanonymise dropped, surrogates still in response. "
                "Raise VAULT_TTL_S if requests legitimately take this long.",
                call_id,
            )
            return {}
        return mapping

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    async def aclose(self) -> None:
        """No-op for the in-memory backend — no connections to drain.
        Kept for Protocol compatibility so `Pipeline.aclose()` can call
        it uniformly across backends."""
        return None

    def _evict_expired_locked(self) -> None:
        """Remove expired entries from the start of the store.

        Assumes entries are inserted chronologically (enforced in put()).
        The chronological invariant is *non-decreasing*, not strictly
        increasing — POSIX `monotonic()` is only required to be
        non-decreasing, so two `put()` calls in the same monotonic
        tick get equal timestamps. The break-on-first-non-expired
        below is still correct under that invariant: if entry N+1
        isn't expired and shares timestamp ts with entry N, neither
        is expired and stopping is right.

        Caller must hold self._lock.
        """
        now = time.monotonic()
        to_del = []
        for cid, (ts, _) in self._store.items():
            if now - ts > self._ttl_s:
                to_del.append(cid)
            else:
                break

        for cid in to_del:
            del self._store[cid]

        if to_del:
            log.debug("Evicted %d expired vault entries", len(to_del))


# Backward-compatible name alias. Existing code that did
# `from .vault import Vault` continues to work without an edit;
# new code should reference `MemoryVault` (or, better, the
# `VaultBackend` Protocol type for fields/properties).
Vault = MemoryVault


def build_vault() -> VaultBackend:
    """Construct the configured vault backend.

    Reads `VAULT_BACKEND` from the central config:
      * `memory` (default) — `MemoryVault`. Zero deps.
      * `redis` — `RedisVault`. Requires the `redis` package
        (install via `pip install "anonymizer-guardrail[vault-redis]"`)
        and a reachable Redis at `VAULT_REDIS_URL`.

    The redis import is deferred to *inside* the redis branch so
    operators on the memory backend never pay for the import — the
    slim production image doesn't need redis-py loaded.
    """
    backend = config.vault_backend
    if backend == "memory":
        return MemoryVault()
    if backend == "redis":
        from .vault_redis import RedisVault  # local import — see docstring
        return RedisVault(
            url=config.vault_redis_url,
            ttl_s=config.vault_ttl_s,
        )
    raise RuntimeError(
        f"Unknown VAULT_BACKEND={backend!r}. Allowed: memory, redis."
    )


__all__ = ["MemoryVault", "RedisVault", "Vault", "VaultBackend", "build_vault"]


# RedisVault is re-exported lazily so `from anonymizer_guardrail.vault
# import RedisVault` works without the redis dep when nobody actually
# touches RedisVault. The `__all__` above lists it for documentation;
# attempting the import resolves it lazily here.
def __getattr__(name: str):  # PEP 562 — module-level __getattr__
    if name == "RedisVault":
        from .vault_redis import RedisVault
        return RedisVault
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
