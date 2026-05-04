"""
In-process MemoryVault implementation.

Default `VaultBackend` (selected by `VAULT_BACKEND=memory`, the
default). Suitable for single-replica deployments — zero
dependencies, sub-microsecond `put`/`pop`. Multi-replica deployments
need `RedisVault` (see `vault_redis.py`); the pre_call and post_call
must land on the same replica with this backend or the entry is
lost.

# Internal layout

Storage is an `OrderedDict` so LRU eviction is O(1) via
`popitem(last=False)`. Each entry is `(monotonic_timestamp,
VaultEntry)`. The chronological insertion order matches what
`_evict_expired_locked` relies on for early termination of the TTL
sweep — `put()` re-inserts an existing call_id at the end to keep
that invariant valid even on re-puts.

# Thread safety

`threading.Lock` protects the store. The lock is held only across
sync operations (no `await` inside the with-block); the coroutine
returns to the caller immediately after the dict update. Holding
the lock across an `await` would risk lock-ordering issues with
other coroutines on the same event loop, so don't add I/O inside
the locked section.

The lock is over-paranoia from a strict asyncio perspective (one
event loop, no preemption between syncs), but FastAPI runs sync
handlers on a thread pool and the lock keeps us safe across both
dispatch modes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

from .config import config
from .vault import VaultEntry

log = logging.getLogger("anonymizer.vault.memory")


class MemoryVault:
    """Process-local vault backed by an OrderedDict + TTL + LRU
    cap. Default backend; suitable for single-replica deployments.

    Multi-replica deployments need `RedisVault` (see
    `vault_redis.py`) — the pre_call and post_call must land on the
    same replica or the entry is lost. See `docs/limitations.md`
    for the broader story.
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
        self._store: OrderedDict[str, tuple[float, VaultEntry]] = OrderedDict()
        self._lock = threading.Lock()

    async def put(self, call_id: str, entry: VaultEntry) -> None:
        """Store the structured entry for one call.

        Async-by-protocol; the body is fully synchronous (in-memory
        dict + threading.Lock). The lock is sub-microsecond so the
        coroutine doesn't yield — it returns to the caller immediately
        after the dict update."""
        if not call_id or entry.is_empty:
            return
        with self._lock:
            self._evict_expired_locked()
            # Remove and re-insert to ensure this call_id is at the end of
            # the dict (insertion order), keeping the chronological assumption
            # valid for _evict_expired_locked.
            self._store.pop(call_id, None)
            self._store[call_id] = (time.monotonic(), entry)
            # LRU backstop in case TTL eviction hasn't reclaimed enough
            # space (e.g. flood of unique call_ids well within TTL).
            # Evicted entries lose their data — the matching post_call
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

    async def peek(self, call_id: str) -> VaultEntry:
        """Retrieve the entry without removing it. Returns an empty
        `VaultEntry()` if absent/expired.

        Used by `pipeline.deanonymize` on the hot path. Streaming
        post_call invocations call this multiple times for the same
        `call_id` with growing accumulated text; each call sees the
        same surrogate map. Eviction is TTL-driven, not call-driven.

        Expiry path emits the same ERROR log as `pop` for the same
        reason: the post_call arrived after the entry expired, so
        the deanonymisation silently failed and the response
        shipped with surrogates still in it.
        """
        if not call_id:
            return VaultEntry()
        with self._lock:
            entry = self._store.get(call_id)
        if entry is None:
            return VaultEntry()
        ts, vault_entry = entry
        if time.monotonic() - ts > self._ttl_s:
            # Don't bother removing here; the next `_evict_expired_locked`
            # call (on the next put) will sweep this entry away. Returning
            # empty preserves the "expired = invisible" contract.
            log.error(
                "Vault entry for call_id=%s expired before post_call — "
                "deanonymise dropped, surrogates still in response. "
                "Raise VAULT_TTL_S if requests legitimately take this long.",
                call_id,
            )
            return VaultEntry()
        return vault_entry

    async def pop(self, call_id: str) -> VaultEntry:
        """Retrieve and remove the entry for a call. Returns an empty
        `VaultEntry()` if absent/expired.

        Not used by the production deanonymize path (which uses
        `peek` so streaming repeated invocations all see the same
        entry). Kept on the surface for utility callers — test
        cleanup between cases, manual force-eviction.

        Expiry path emits ERROR for the same reason as `peek`: see
        that method's docstring for the rationale.

        Absent / no call_id returns silently — that's the no-op
        path (`input_type=request` without a matching `response`,
        or a request that intentionally bypasses the vault).
        """
        if not call_id:
            return VaultEntry()
        with self._lock:
            entry = self._store.pop(call_id, None)
        if entry is None:
            return VaultEntry()
        ts, vault_entry = entry
        if time.monotonic() - ts > self._ttl_s:
            log.error(
                "Vault entry for call_id=%s expired before post_call — "
                "deanonymise dropped, surrogates still in response. "
                "Raise VAULT_TTL_S if requests legitimately take this long.",
                call_id,
            )
            return VaultEntry()
        return vault_entry

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


__all__ = ["MemoryVault"]
