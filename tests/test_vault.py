"""Vault behaviour: TTL eviction, LRU cap, basic put/get/pop semantics."""

from __future__ import annotations

from anonymizer_guardrail.vault import Vault


def test_lru_evicts_oldest_when_over_cap() -> None:
    """Filling past max_entries drops the least-recently-inserted entry."""
    v = Vault(ttl_s=600, max_entries=2)
    v.put("a", {"sa": "oa"})
    v.put("b", {"sb": "ob"})
    v.put("c", {"sc": "oc"})  # evicts "a"

    assert v.size() == 2
    assert v.pop("a") == {}            # evicted, gone
    assert v.pop("b") == {"sb": "ob"}  # still there
    assert v.pop("c") == {"sc": "oc"}  # still there


def test_lru_eviction_handles_burst() -> None:
    """Inserting many more than the cap leaves only the most recent N."""
    v = Vault(ttl_s=600, max_entries=3)
    for i in range(10):
        v.put(f"call-{i}", {"s": f"o-{i}"})

    assert v.size() == 3
    # Only the last three inserts survive.
    for i in range(7):
        assert v.pop(f"call-{i}") == {}
    for i in range(7, 10):
        assert v.pop(f"call-{i}") == {"s": f"o-{i}"}


def test_re_putting_same_id_refreshes_recency() -> None:
    """A repeat put() on an existing call_id moves it to the end, so it
    isn't the next victim of LRU eviction."""
    v = Vault(ttl_s=600, max_entries=2)
    v.put("a", {"sa": "oa"})
    v.put("b", {"sb": "ob"})
    v.put("a", {"sa": "oa2"})  # refresh "a" to most-recent
    v.put("c", {"sc": "oc"})   # should evict "b", not "a"

    assert v.pop("a") == {"sa": "oa2"}
    assert v.pop("b") == {}
    assert v.pop("c") == {"sc": "oc"}


def test_max_entries_floor_at_one() -> None:
    """A typoed 0/negative max_entries can't disable writes — floor is 1."""
    v = Vault(ttl_s=600, max_entries=0)
    v.put("a", {"sa": "oa"})
    assert v.size() == 1


def test_put_and_pop_roundtrip() -> None:
    v = Vault(ttl_s=600, max_entries=10)
    v.put("call-1", {"surrogate": "original"})
    assert v.size() == 1
    assert v.pop("call-1") == {"surrogate": "original"}
    assert v.size() == 0


def test_pop_missing_returns_empty() -> None:
    v = Vault(ttl_s=600, max_entries=10)
    assert v.pop("never-stored") == {}


def test_ttl_expiry() -> None:
    """An entry whose monotonic timestamp is past the TTL returns {}."""
    v = Vault(ttl_s=0, max_entries=10)  # TTL 0 → everything is "expired"
    v.put("a", {"sa": "oa"})
    # Call get/pop after the immediate write — a 0-second TTL means the
    # `now - ts > ttl` check trips on any positive elapsed time. Use pop
    # so we also cover the eviction path.
    import time
    time.sleep(0.01)
    assert v.pop("a") == {}
