"""
In-memory mapping store keyed by litellm_call_id.

A pre_call writes the surrogate→original mapping; the matching post_call reads
and evicts it. Entries are TTL'd in case the post_call never arrives (request
errored out, client disconnected, etc.) so memory doesn't grow unbounded.

For multi-replica deployments, swap this for Redis. The interface is small
enough that the swap is a one-class change.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

from .config import config

log = logging.getLogger("anonymizer.vault")


class Vault:
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

    def put(self, call_id: str, mapping: dict[str, str]) -> None:
        """Store the surrogate→original mapping for one call."""
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

    def pop(self, call_id: str) -> dict[str, str]:
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


__all__ = ["Vault"]
