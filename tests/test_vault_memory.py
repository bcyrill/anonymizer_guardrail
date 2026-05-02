"""MemoryVault-specific behaviour: LRU cap, TTL via lazy check,
max_entries floor.

Cross-backend contract tests live in `test_vault.py` (parametrized
across MemoryVault + RedisVault). This file tests behaviours that are
unique to the in-process backend:

  * **LRU cap.** RedisVault has no client-side LRU — operators bound
    Redis memory via `maxmemory` policy on the Redis side. Only
    MemoryVault enforces a per-instance entry count.
  * **TTL via lazy check.** RedisVault sets server-side `EXPIRE`;
    MemoryVault checks `time.monotonic() - ts > ttl_s` on every read.
    The lazy-check mechanism (and its `ttl_s=0` boundary) is
    MemoryVault-specific; the corresponding RedisVault TTL test
    uses `EXPIRE` semantics in `test_vault_redis.py`.
  * **`max_entries` floor.** Defensive guard against a typoed
    `VAULT_MAX_ENTRIES=0`. RedisVault has no equivalent field.
"""

from __future__ import annotations

import asyncio
import os

# Match other test modules: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

from anonymizer_guardrail.vault_memory import MemoryVault


# ── LRU cap ─────────────────────────────────────────────────────────────


async def test_lru_evicts_oldest_when_over_cap() -> None:
    """Filling past max_entries drops the least-recently-inserted entry."""
    v = MemoryVault(ttl_s=600, max_entries=2)
    await v.put("a", {"sa": "oa"})
    await v.put("b", {"sb": "ob"})
    await v.put("c", {"sc": "oc"})  # evicts "a"

    assert v.size() == 2
    assert await v.pop("a") == {}            # evicted, gone
    assert await v.pop("b") == {"sb": "ob"}  # still there
    assert await v.pop("c") == {"sc": "oc"}  # still there


async def test_lru_eviction_handles_burst() -> None:
    """Inserting many more than the cap leaves only the most recent N."""
    v = MemoryVault(ttl_s=600, max_entries=3)
    for i in range(10):
        await v.put(f"call-{i}", {"s": f"o-{i}"})

    assert v.size() == 3
    # Only the last three inserts survive.
    for i in range(7):
        assert await v.pop(f"call-{i}") == {}
    for i in range(7, 10):
        assert await v.pop(f"call-{i}") == {"s": f"o-{i}"}


async def test_re_putting_same_id_refreshes_recency() -> None:
    """A repeat put() on an existing call_id moves it to the end, so it
    isn't the next victim of LRU eviction."""
    v = MemoryVault(ttl_s=600, max_entries=2)
    await v.put("a", {"sa": "oa"})
    await v.put("b", {"sb": "ob"})
    await v.put("a", {"sa": "oa2"})  # refresh "a" to most-recent
    await v.put("c", {"sc": "oc"})   # should evict "b", not "a"

    assert await v.pop("a") == {"sa": "oa2"}
    assert await v.pop("b") == {}
    assert await v.pop("c") == {"sc": "oc"}


async def test_max_entries_floor_at_one() -> None:
    """A typoed 0/negative max_entries can't disable writes — floor is 1."""
    v = MemoryVault(ttl_s=600, max_entries=0)
    await v.put("a", {"sa": "oa"})
    assert v.size() == 1


# ── TTL via lazy check ──────────────────────────────────────────────────


async def test_ttl_expiry() -> None:
    """An entry whose monotonic timestamp is past the TTL returns {}.
    MemoryVault-specific: the TTL is checked lazily on every `pop`,
    not via a background sweeper. RedisVault's TTL is server-side
    via `EXPIRE` — covered separately in `test_vault_redis.py`."""
    v = MemoryVault(ttl_s=0, max_entries=10)  # TTL 0 → everything is "expired"
    await v.put("a", {"sa": "oa"})
    # A 0-second TTL means the `now - ts > ttl` check trips on any
    # positive elapsed time. The tiny sleep is enough.
    await asyncio.sleep(0.01)
    assert await v.pop("a") == {}
