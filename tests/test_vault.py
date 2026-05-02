"""Vault Protocol contract tests — parametrized across all backends.

Tests in this file exercise the `VaultBackend` Protocol contract: the
behaviour every implementation MUST satisfy regardless of whether the
underlying store is in-process memory, Redis, or anything future. New
backends are added by extending the `vault` fixture below; the contract
tests then run against them automatically.

Backend-specific behaviour lives in:

  * `test_vault_memory.py` — LRU cap, TTL via lazy check, max_entries floor.
  * `test_vault_redis.py` — atomic GETDEL, JSON wire format, key prefix,
    error wrapping, RedisVault-specific construction failures.

The TTL contract isn't covered here because the mechanisms differ
substantially across backends (lazy check vs. server-side EXPIRE) and
the cross-backend assertion would require slow `asyncio.sleep`s on the
Redis side. Each backend's TTL test stays in its own file.
"""

from __future__ import annotations

import os
from unittest.mock import patch

# Match the other test modules: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.vault import VaultBackend
from anonymizer_guardrail.vault_memory import MemoryVault
from anonymizer_guardrail.vault_redis import RedisVault


@pytest.fixture(params=["memory", "redis"])
async def vault(request) -> VaultBackend:
    """Yield a fresh VaultBackend for each parametrized run.

    The two backends construct very differently:

      * `MemoryVault(ttl_s=…, max_entries=…)` — direct construction,
        zero infrastructure.
      * `RedisVault(url=…, ttl_s=…)` — needs a Redis client. We patch
        `redis.asyncio.from_url` to return a `fakeredis` async client
        so CI doesn't need a real Redis. Uses the same redis-py
        asyncio surface as production.

    Each test gets its own backend instance, so cross-test state
    leakage is impossible.
    """
    if request.param == "memory":
        v: VaultBackend = MemoryVault(ttl_s=600, max_entries=100)
        yield v
        await v.aclose()
    else:
        import fakeredis.aioredis as fakeredis_aioredis

        fake_client = fakeredis_aioredis.FakeRedis(decode_responses=True)
        with patch("redis.asyncio.from_url", return_value=fake_client):
            v = RedisVault(url="redis://fake/0", ttl_s=600)
        yield v
        await v.aclose()


# ── Contract: roundtrip ─────────────────────────────────────────────────


async def test_put_and_pop_roundtrip(vault: VaultBackend) -> None:
    """Basic happy path: put a mapping, get it back via pop. Every
    backend must satisfy this."""
    await vault.put("call-1", {"surrogate": "original"})
    assert await vault.pop("call-1") == {"surrogate": "original"}


async def test_pop_after_pop_is_idempotent(vault: VaultBackend) -> None:
    """A second `pop` for the same call_id returns `{}`. Pins the
    "pop is destructive" semantics that both backends honour
    (MemoryVault deletes the dict entry; RedisVault uses GETDEL)."""
    await vault.put("call-1", {"k": "v"})
    assert await vault.pop("call-1") == {"k": "v"}
    assert await vault.pop("call-1") == {}


async def test_pop_missing_returns_empty(vault: VaultBackend) -> None:
    """A call_id that was never `put` returns `{}` — the "no-op for
    requests without a matching pre-call" contract."""
    assert await vault.pop("never-stored") == {}


# ── Contract: defensive no-ops on degenerate input ──────────────────────


async def test_empty_call_id_is_noop(vault: VaultBackend) -> None:
    """Empty call_id on `put` doesn't store anything; on `pop`
    returns `{}`. Both backends must honour this — without it a
    misconfigured caller could overwrite a "" entry across requests."""
    await vault.put("", {"k": "v"})
    assert await vault.pop("") == {}


async def test_empty_mapping_is_noop(vault: VaultBackend) -> None:
    """Empty mapping on `put` doesn't roundtrip a placeholder entry.
    Important so a request with no detected entities (mapping={}) doesn't
    pollute the vault with empty entries that the matching post_call
    would then need to interpret."""
    await vault.put("call-empty-map", {})
    assert await vault.pop("call-empty-map") == {}


# ── Contract: aclose ────────────────────────────────────────────────────


async def test_aclose_does_not_raise(vault: VaultBackend) -> None:
    """Every backend's `aclose` must complete without raising. For
    MemoryVault it's a no-op; for RedisVault it drains the connection
    pool. Pipeline.aclose() relies on this contract — a misbehaving
    aclose could hang FastAPI shutdown."""
    # vault fixture already calls aclose on teardown — calling it
    # explicitly here pins the behaviour during normal test flow.
    await vault.aclose()
