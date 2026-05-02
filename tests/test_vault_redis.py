"""RedisVault behaviour: put/pop round-trip, TTL eviction, atomic
GETDEL semantics, JSON serialization, error wrapping.

Uses `fakeredis.aioredis` so CI doesn't need a real Redis server.
The fakeredis instance speaks the redis-py asyncio protocol, so
the tests exercise the same code path as production.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

# Match other test modules: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.vault_redis import RedisVault, RedisVaultError


@pytest.fixture
async def vault() -> RedisVault:
    """Build a RedisVault wired to fakeredis.aioredis. Construction
    intercepts the `redis.asyncio.from_url()` call to substitute the
    fake — same surface as production code, but no network."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch(
        "redis.asyncio.from_url",
        return_value=fake_client,
    ):
        v = RedisVault(url="redis://fake/0", ttl_s=60)
    yield v
    await v.aclose()


# ── Round-trip ──────────────────────────────────────────────────────────


async def test_put_and_pop_roundtrip(vault: RedisVault) -> None:
    """Basic happy path: put a mapping, get it back via pop, pop again
    returns empty (GETDEL removed the entry)."""
    await vault.put("call-1", {"surrogate": "original"})
    assert await vault.pop("call-1") == {"surrogate": "original"}
    assert await vault.pop("call-1") == {}  # GETDEL already removed it


async def test_pop_missing_returns_empty(vault: RedisVault) -> None:
    assert await vault.pop("never-stored") == {}


async def test_empty_call_id_or_mapping_is_noop(vault: RedisVault) -> None:
    """Defensive: empty call_id / empty mapping should not roundtrip
    a placeholder entry into Redis."""
    await vault.put("", {"a": "b"})
    await vault.put("call-1", {})
    assert await vault.pop("") == {}
    assert await vault.pop("call-1") == {}


# ── Atomic GETDEL semantics ─────────────────────────────────────────────


async def test_getdel_only_one_pop_wins(vault: RedisVault) -> None:
    """Two concurrent pops on the same call_id: one wins (gets the
    mapping), the other gets `{}`. GETDEL is atomic, so the race is
    won by the first server-side request — not partially observed."""
    await vault.put("call-race", {"k": "v"})

    # Run two pops in parallel against the same key.
    results = await asyncio.gather(
        vault.pop("call-race"),
        vault.pop("call-race"),
    )
    # Exactly one should have the mapping; the other should be empty.
    non_empty = [r for r in results if r]
    empty = [r for r in results if not r]
    assert len(non_empty) == 1
    assert non_empty[0] == {"k": "v"}
    assert len(empty) == 1


# ── TTL ─────────────────────────────────────────────────────────────────


async def test_ttl_set_on_put() -> None:
    """`put` sets EXPIRE so Redis evicts the entry server-side."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        v = RedisVault(url="redis://fake/0", ttl_s=10)

    await v.put("call-ttl", {"k": "v"})
    # Inspect TTL via the underlying fakeredis client. -1 = no expire,
    # -2 = key doesn't exist, positive = seconds remaining.
    ttl = await fake_client.ttl("vault:call-ttl")
    assert ttl > 0 and ttl <= 10
    await v.aclose()


# ── JSON serialization ──────────────────────────────────────────────────


async def test_value_is_json_encoded() -> None:
    """The wire format is JSON, not pickle / msgpack. Operators
    debugging from `redis-cli GET vault:<id>` should see readable
    JSON — pin that contract."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        v = RedisVault(url="redis://fake/0", ttl_s=60)

    mapping = {"sur1": "orig1", "sur2": "orig2"}
    await v.put("call-json", mapping)

    raw = await fake_client.get("vault:call-json")
    assert isinstance(raw, str)
    assert json.loads(raw) == mapping
    await v.aclose()


async def test_corrupted_json_returns_empty_logs_error(
    vault: RedisVault, caplog: pytest.LogCaptureFixture,
) -> None:
    """If a vault entry is corrupted (e.g. another process wrote the
    key with a non-JSON value), pop should return {} and log ERROR
    — same severity contract as MemoryVault's TTL-expiry case."""
    import logging

    # Inject a non-JSON value directly via the underlying client.
    await vault._client.set("vault:call-corrupt", "this-is-not-json", ex=60)

    with caplog.at_level(logging.ERROR, logger="anonymizer.vault.redis"):
        result = await vault.pop("call-corrupt")
    assert result == {}
    assert any("non-JSON" in r.message for r in caplog.records)


# ── Key namespacing ─────────────────────────────────────────────────────


async def test_key_is_prefixed_with_vault(vault: RedisVault) -> None:
    """Keys go under the `vault:` prefix so the backend can share a
    Redis instance with other consumers without colliding."""
    await vault.put("my-call", {"k": "v"})

    keys = [k async for k in vault._client.scan_iter()]
    assert any(k == "vault:my-call" for k in keys)
    # No keys without the prefix from our put().
    assert all(k.startswith("vault:") for k in keys if k.endswith("my-call"))


# ── Error wrapping ──────────────────────────────────────────────────────


async def test_redis_failure_on_put_wraps_as_redis_vault_error() -> None:
    """A redis-py exception during put() should surface as
    RedisVaultError, not the underlying redis-py error class. This
    gives the pipeline a single typed discriminator for vault
    failures regardless of which redis-py error fired."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)

    async def boom(*_args, **_kwargs):
        raise ConnectionError("simulated connection drop")

    with patch("redis.asyncio.from_url", return_value=fake_client):
        v = RedisVault(url="redis://fake/0", ttl_s=60)

    # Replace the client's set method with our failing version.
    v._client.set = boom  # type: ignore[method-assign]

    with pytest.raises(RedisVaultError, match="Failed to store"):
        await v.put("call-1", {"k": "v"})


async def test_redis_failure_on_pop_wraps_as_redis_vault_error() -> None:
    """Mirror of the put error case for the pop path."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)

    async def boom(*_args, **_kwargs):
        raise ConnectionError("simulated connection drop")

    with patch("redis.asyncio.from_url", return_value=fake_client):
        v = RedisVault(url="redis://fake/0", ttl_s=60)

    v._client.getdel = boom  # type: ignore[method-assign]

    with pytest.raises(RedisVaultError, match="Failed to retrieve"):
        await v.pop("call-1")


# ── Construction failure modes ──────────────────────────────────────────


def test_empty_url_raises() -> None:
    """Defensive guard against bypassing the central Config validator
    via direct construction (e.g. test code passing url="" by mistake)."""
    with pytest.raises(RuntimeError, match="non-empty url"):
        RedisVault(url="", ttl_s=60)


# ── size() placeholder ──────────────────────────────────────────────────


async def test_size_returns_zero(vault: RedisVault) -> None:
    """`size()` is sync (it's a /health accessor) and the SCAN it
    would need is async; we return 0 as a documented placeholder.
    Operators query Redis directly for the actual count."""
    await vault.put("call-1", {"k": "v"})
    await vault.put("call-2", {"k": "v"})
    assert vault.size() == 0
