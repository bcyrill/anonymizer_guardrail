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

from .config import config

log = logging.getLogger("anonymizer.vault")


class Vault:
    def __init__(self, ttl_s: int | None = None) -> None:
        self._ttl_s = ttl_s if ttl_s is not None else config.vault_ttl_s
        self._store: dict[str, tuple[float, dict[str, str]]] = {}
        self._lock = threading.Lock()

    def put(self, call_id: str, mapping: dict[str, str]) -> None:
        """Store the surrogate→original mapping for one call."""
        if not call_id or not mapping:
            return
        with self._lock:
            self._evict_expired_locked()
            self._store[call_id] = (time.monotonic(), dict(mapping))

    def pop(self, call_id: str) -> dict[str, str]:
        """Retrieve and remove the mapping for a call. Returns {} if absent/expired."""
        if not call_id:
            return {}
        with self._lock:
            entry = self._store.pop(call_id, None)
        if entry is None:
            return {}
        ts, mapping = entry
        if time.monotonic() - ts > self._ttl_s:
            log.warning("Vault entry for call_id=%s expired before post_call", call_id)
            return {}
        return mapping

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def _evict_expired_locked(self) -> None:
        """Caller must hold self._lock."""
        now = time.monotonic()
        expired = [cid for cid, (ts, _) in self._store.items() if now - ts > self._ttl_s]
        for cid in expired:
            self._store.pop(cid, None)
        if expired:
            log.debug("Evicted %d expired vault entries", len(expired))


__all__ = ["Vault"]
