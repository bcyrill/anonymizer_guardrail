"""MemoryVault behaviour: TTL eviction, LRU cap, basic put/get/pop semantics.

These tests cover the in-memory backend specifically. The Redis-backed
sibling lives in `test_vault_redis.py`. The shared Protocol contract is
exercised by both files; backend-specific behaviour (LRU cap, the
`_store` internal layout) is tested only here.
"""

from __future__ import annotations

import asyncio

from anonymizer_guardrail.vault import Vault


async def test_lru_evicts_oldest_when_over_cap() -> None:
    """Filling past max_entries drops the least-recently-inserted entry."""
    v = Vault(ttl_s=600, max_entries=2)
    await v.put("a", {"sa": "oa"})
    await v.put("b", {"sb": "ob"})
    await v.put("c", {"sc": "oc"})  # evicts "a"

    assert v.size() == 2
    assert await v.pop("a") == {}            # evicted, gone
    assert await v.pop("b") == {"sb": "ob"}  # still there
    assert await v.pop("c") == {"sc": "oc"}  # still there


async def test_lru_eviction_handles_burst() -> None:
    """Inserting many more than the cap leaves only the most recent N."""
    v = Vault(ttl_s=600, max_entries=3)
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
    v = Vault(ttl_s=600, max_entries=2)
    await v.put("a", {"sa": "oa"})
    await v.put("b", {"sb": "ob"})
    await v.put("a", {"sa": "oa2"})  # refresh "a" to most-recent
    await v.put("c", {"sc": "oc"})   # should evict "b", not "a"

    assert await v.pop("a") == {"sa": "oa2"}
    assert await v.pop("b") == {}
    assert await v.pop("c") == {"sc": "oc"}


async def test_max_entries_floor_at_one() -> None:
    """A typoed 0/negative max_entries can't disable writes — floor is 1."""
    v = Vault(ttl_s=600, max_entries=0)
    await v.put("a", {"sa": "oa"})
    assert v.size() == 1


async def test_put_and_pop_roundtrip() -> None:
    v = Vault(ttl_s=600, max_entries=10)
    await v.put("call-1", {"surrogate": "original"})
    assert v.size() == 1
    assert await v.pop("call-1") == {"surrogate": "original"}
    assert v.size() == 0


async def test_pop_missing_returns_empty() -> None:
    v = Vault(ttl_s=600, max_entries=10)
    assert await v.pop("never-stored") == {}


async def test_ttl_expiry() -> None:
    """An entry whose monotonic timestamp is past the TTL returns {}."""
    v = Vault(ttl_s=0, max_entries=10)  # TTL 0 → everything is "expired"
    await v.put("a", {"sa": "oa"})
    # Call get/pop after the immediate write — a 0-second TTL means the
    # `now - ts > ttl` check trips on any positive elapsed time. Use pop
    # so we also cover the eviction path.
    await asyncio.sleep(0.01)
    assert await v.pop("a") == {}


async def test_aclose_is_a_noop() -> None:
    """MemoryVault has no connections to drain; aclose returns cleanly
    so Pipeline.aclose() can call it uniformly across backends."""
    v = Vault()
    await v.aclose()  # should not raise
