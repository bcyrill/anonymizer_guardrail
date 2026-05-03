"""
Mapping store keyed by `litellm_call_id` — abstract surface.

The pre_call writes the surrogate→original mapping; the matching
post_call reads and evicts it. Entries are TTL'd in case the
post_call never arrives (request errored out, client disconnected,
etc.) so memory doesn't grow unbounded.

# Vault entry shape

The vault stores a structured `VaultEntry` per call_id (not just a
flat surrogate→original dict). The extras are needed by the
deanonymize-side pipeline-cache pre-warm:

  * Per-surrogate `entity_type` — synthesised matches need a typed
    entity_type to land in the right surrogate-cache slot. Recovering
    type from the surrogate prefix only works for opaque tokens; with
    Faker output (`Robert Jones`, `192.0.2.5`) we'd lose the signal.
  * Per-surrogate `source_detectors` — which detector(s) actually
    produced this match before `_dedup` collapsed the per-detector
    lists. Lets the prewarm path filter the synthesised match list
    per detector so per-detector cache slots get *that* detector's
    matches (not the deduped union — see the bidirectional pipeline
    cache rationale in docs/design-decisions.md).
  * Call-level `detector_mode` + per-detector `kwargs` — required to
    reconstruct the pipeline-cache key (`(text, detector_mode,
    frozen_kwargs)`) and per-detector cache keys at deanonymize time.
    Without these, prewarm would have to fall back to default-overrides
    slots, which silently re-introduces the override-staleness the
    prior Option-2 prewarm had.

This module owns the *interface* (`VaultBackend` Protocol), the
*shape* (`VaultEntry`, `VaultSurrogate`, the kwargs-freezing helper),
and the *selection* (`build_vault()` factory). Concrete implementations
each live in their own module:

  * `vault_memory.py` → `MemoryVault` (default; process-local,
    zero deps).
  * `vault_redis.py` → `RedisVault` (opt-in via `VAULT_BACKEND=redis`;
    shared store for multi-replica deployments). Imports `redis-py`
    only when the backend is actually selected.

Test layout mirrors this split: `tests/test_vault.py` holds the
parametrized contract tests that run against every backend; each
`test_vault_<backend>.py` file owns the backend-specific tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from .config import config


# ── Per-entry shape ────────────────────────────────────────────────────


# Hashable kwargs shape stored per-detector. Each entry is
# `(detector_name, frozen_kwargs)` where `frozen_kwargs` is a
# tuple of `(key, value)` pairs sorted by key. Lists/tuples in the
# value position become tuples; scalars pass through. Constrained
# enough to round-trip through JSON for the Redis backend, hashable
# enough to be a piece of the pipeline-cache key.
FrozenKwargs = tuple[tuple[str, Any], ...]
FrozenCallKwargs = tuple[tuple[str, FrozenKwargs], ...]


def freeze_kwargs(kwargs: Mapping[str, Any]) -> FrozenKwargs:
    """Sort + tupleify a per-detector kwargs dict so it can serve as
    a hashable cache-key component AND round-trip through JSON.

    Values are normalised:
      * `list` / `tuple` → `tuple` (recursive on contents).
      * Scalars (`str`, `int`, `float`, `bool`, `None`) pass through.
      * `dict` is rejected — there's no detector today that returns
        nested dict kwargs and freezing them would require a stable
        recursion contract we don't need.

    Sorting by key gives a canonical order: kwargs={"a": 1, "b": 2}
    and {"b": 2, "a": 1} freeze to the same tuple. Without that, the
    pipeline cache would scatter hits across distinct frozen tuples
    for the same logical input.
    """
    out: list[tuple[str, Any]] = []
    for key in sorted(kwargs):
        value = kwargs[key]
        out.append((key, _freeze_value(value)))
    return tuple(out)


def _freeze_value(value: Any) -> Any:
    """Recursive value normalisation for `freeze_kwargs`. Lists and
    tuples become tuples; scalars pass through; dicts are rejected.
    """
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(v) for v in value)
    if isinstance(value, dict):
        raise TypeError(
            "freeze_kwargs: nested dict values aren't supported. "
            f"Got {value!r}."
        )
    return value


@dataclass(frozen=True)
class VaultSurrogate:
    """One surrogate's metadata stored in the vault.

    `original` is the substring the surrogate replaces (the value the
    deanonymize-side substring-replacer puts back). `entity_type` is
    the canonical type from the producing Match — needed at
    deanonymize-side prewarm time so synthesised matches carry the
    same type the original detector did. `source_detectors` is the
    list of detectors that independently found this entity *before*
    `_dedup` collapsed per-detector lists; used to filter the
    synthesised match list per detector when the prewarm hook seeds
    each per-detector cache.
    """

    original: str
    entity_type: str
    source_detectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class VaultEntry:
    """One full vault entry per call_id.

    Equivalent to the legacy `dict[str, str]` shape but with three
    structured additions (per-surrogate type, per-surrogate source
    detectors, call-level detector_mode + frozen kwargs) — see the
    module docstring for why each is needed by the pipeline-cache
    prewarm.
    """

    surrogates: dict[str, VaultSurrogate] = field(default_factory=dict)
    detector_mode: tuple[str, ...] = ()
    kwargs: FrozenCallKwargs = ()

    @property
    def is_empty(self) -> bool:
        """True iff this entry carries no surrogates — used by both
        backends to short-circuit `put` (an empty mapping shouldn't
        store a placeholder)."""
        return not self.surrogates


@runtime_checkable
class VaultBackend(Protocol):
    """Round-trip vault contract.

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

    async def put(self, call_id: str, entry: VaultEntry) -> None:
        """Store the structured entry for one call. Empty entry or
        empty call_id is a no-op."""
        ...

    async def pop(self, call_id: str) -> VaultEntry:
        """Retrieve and remove the entry. Returns an empty
        `VaultEntry()` if absent or expired. Idempotent — second
        pop with the same call_id returns empty."""
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
    operators never pay for the `redis-py` import — the production
    image stays unaffected when only the memory backend is in use.
    MemoryVault's import is also lazy for symmetry, even though it
    has no extra cost.
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


__all__ = [
    "FrozenCallKwargs",
    "FrozenKwargs",
    "VaultBackend",
    "VaultEntry",
    "VaultSurrogate",
    "build_vault",
    "freeze_kwargs",
]
