"""
Mapping store keyed by `litellm_call_id` — abstract surface.

The pre_call writes the surrogate→original mapping; the matching
post_call reads and evicts it. Entries are TTL'd in case the
post_call never arrives (request errored out, client disconnected,
etc.) so memory doesn't grow unbounded.

This module owns the *interface* (`VaultBackend` Protocol) and the
*selection* (`build_vault()` factory). Concrete implementations
each live in their own module:

  * `vault_memory.py` → `MemoryVault` (default; process-local,
    zero deps).
  * `vault_redis.py` → `RedisVault` (opt-in via `VAULT_BACKEND=redis`;
    shared store for multi-replica deployments). Imports `redis-py`
    only when the backend is actually selected.

Selection happens in `build_vault()` based on `VAULT_BACKEND`. The
factory is the only place that imports a concrete backend; the
pipeline references the Protocol type so swapping doesn't ripple
across the codebase.

Test layout mirrors this split: `tests/test_vault.py` holds the
parametrized contract tests that run against every backend; each
`test_vault_<backend>.py` file owns the backend-specific tests.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .config import config


@runtime_checkable
class VaultBackend(Protocol):
    """Round-trip mapping store contract.

    Implementations: `MemoryVault` (in `vault_memory.py`, default)
    and `RedisVault` (in `vault_redis.py`, opt-in via
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


def build_vault() -> VaultBackend:
    """Construct the configured vault backend.

    Reads `VAULT_BACKEND` from the central config:
      * `memory` (default) — `MemoryVault` from `vault_memory.py`.
        Zero deps.
      * `redis` — `RedisVault` from `vault_redis.py`. Requires the
        `redis` package (install via `pip install
        "anonymizer-guardrail[vault-redis]"`) and a reachable Redis
        at `VAULT_REDIS_URL`.

    Imports are deferred to *inside* each branch so memory-only
    operators never pay for the `redis-py` import — the slim
    production image stays unaffected when only the memory backend
    is in use. MemoryVault's import is also lazy for symmetry, even
    though it has no extra cost.
    """
    backend = config.vault_backend
    if backend == "memory":
        from .vault_memory import MemoryVault
        return MemoryVault()
    if backend == "redis":
        from .vault_redis import RedisVault
        return RedisVault(
            url=config.vault_redis_url,
            ttl_s=config.vault_ttl_s,
        )
    raise RuntimeError(
        f"Unknown VAULT_BACKEND={backend!r}. Allowed: memory, redis."
    )


__all__ = ["VaultBackend", "build_vault"]
