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

from anonymizer_guardrail.vault import VaultBackend, VaultEntry, VaultSurrogate
from anonymizer_guardrail.vault_memory import MemoryVault
from anonymizer_guardrail.vault_redis import RedisVault


def _entry(**surrogates: tuple[str, str, tuple[str, ...]] | tuple[str, str]) -> VaultEntry:
    """Helper: build a VaultEntry from a kwargs-style spec.

    Each kwarg is `surrogate=(original, entity_type)` or
    `surrogate=(original, entity_type, source_detectors)`. Used by
    the contract tests below to keep test data terse. Real callers
    construct VaultEntry / VaultSurrogate directly.
    """
    out: dict[str, VaultSurrogate] = {}
    for sur, value in surrogates.items():
        if len(value) == 2:
            original, etype = value
            sources: tuple[str, ...] = ()
        else:
            original, etype, sources = value  # type: ignore[misc]
        out[sur] = VaultSurrogate(
            original=original, entity_type=etype, source_detectors=sources,
        )
    return VaultEntry(surrogates=out)


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
    """Basic happy path: put an entry, get it back via pop. Every
    backend must satisfy this."""
    entry = _entry(surrogate=("original", "PERSON"))
    await vault.put("call-1", entry)
    got = await vault.pop("call-1")
    assert got == entry


async def test_pop_after_pop_is_idempotent(vault: VaultBackend) -> None:
    """A second `pop` for the same call_id returns an empty entry. Pins the
    "pop is destructive" semantics that both backends honour
    (MemoryVault deletes the dict entry; RedisVault uses GETDEL)."""
    entry = _entry(s=("v", "OTHER"))
    await vault.put("call-1", entry)
    assert await vault.pop("call-1") == entry
    assert await vault.pop("call-1") == VaultEntry()


async def test_pop_missing_returns_empty(vault: VaultBackend) -> None:
    """A call_id that was never `put` returns an empty entry — the
    "no-op for requests without a matching pre-call" contract."""
    assert await vault.pop("never-stored") == VaultEntry()


# ── Contract: peek (read-only) ─────────────────────────────────────────


async def test_peek_returns_entry_without_removing(vault: VaultBackend) -> None:
    """`peek` is the read-only sibling of `pop`. Pins the "peek does
    not remove" contract — required for streaming deanonymize, where
    LiteLLM invokes our post_call repeatedly under the same call_id
    (every Nth chunk + final assembled response). Each invocation
    must see the same surrogate map."""
    entry = _entry(s=("v", "PERSON"))
    await vault.put("call-peek", entry)
    assert await vault.peek("call-peek") == entry
    # Second peek returns the same entry — eviction is TTL-driven, not
    # call-driven.
    assert await vault.peek("call-peek") == entry
    # Third peek too — `pop` is the only way to remove it explicitly.
    assert await vault.peek("call-peek") == entry


async def test_peek_then_pop_drains(vault: VaultBackend) -> None:
    """After arbitrary peeks, an explicit `pop` still drains the
    entry. Pins the utility-callers contract — tests cleaning up
    between cases, manual force-eviction — for both backends."""
    entry = _entry(s=("v", "PERSON"))
    await vault.put("call-peek-pop", entry)
    await vault.peek("call-peek-pop")
    await vault.peek("call-peek-pop")
    assert await vault.pop("call-peek-pop") == entry
    # And after pop, peek sees nothing.
    assert await vault.peek("call-peek-pop") == VaultEntry()


async def test_peek_missing_returns_empty(vault: VaultBackend) -> None:
    """A call_id that was never `put` peeks to empty — same shape as
    pop's missing case. Important for the deanonymize-without-anonymize
    edge case (e.g. response-side call landing on a replica that
    didn't see the request side; or a call_id that bypassed the
    vault entirely)."""
    assert await vault.peek("never-stored") == VaultEntry()


async def test_peek_does_not_revive_after_pop(vault: VaultBackend) -> None:
    """A `pop` followed by `peek` for the same call_id sees nothing.
    Sanity check: the eviction wrought by pop is observable through
    peek too, not just through subsequent pops."""
    entry = _entry(s=("v", "PERSON"))
    await vault.put("call-pop-then-peek", entry)
    assert await vault.pop("call-pop-then-peek") == entry
    assert await vault.peek("call-pop-then-peek") == VaultEntry()


async def test_peek_full_entry_roundtrip(vault: VaultBackend) -> None:
    """Same shape-fidelity check as `test_full_entry_roundtrip` but
    via peek. All structured additions (entity_type, source_detectors,
    detector_mode + kwargs) survive a peek round-trip on every
    backend — important because the deanonymize-side prewarm hook
    reads these fields from the peeked entry."""
    entry = VaultEntry(
        surrogates={
            "[PERSON_ABCD1234]": VaultSurrogate(
                original="Alice Smith",
                entity_type="PERSON",
                source_detectors=("regex", "llm"),
            ),
        },
        detector_mode=("regex", "llm"),
        kwargs=(
            ("llm", (("model", "anonymize"),)),
            ("regex", (("overlap_strategy", None),)),
        ),
    )
    await vault.put("call-peek-full", entry)
    assert await vault.peek("call-peek-full") == entry
    # Still there for the next streaming-chunk call.
    assert await vault.peek("call-peek-full") == entry


# ── Contract: defensive no-ops on degenerate input ──────────────────────


async def test_empty_call_id_is_noop(vault: VaultBackend) -> None:
    """Empty call_id on `put` doesn't store anything; on `pop`
    returns empty. Both backends must honour this — without it a
    misconfigured caller could overwrite a "" entry across requests."""
    await vault.put("", _entry(s=("v", "OTHER")))
    assert await vault.pop("") == VaultEntry()


async def test_empty_entry_is_noop(vault: VaultBackend) -> None:
    """Empty entry on `put` doesn't roundtrip a placeholder. Important
    so a request with no detected entities doesn't pollute the vault
    with empty entries that the matching post_call would then need to
    interpret."""
    await vault.put("call-empty", VaultEntry())
    assert await vault.pop("call-empty") == VaultEntry()


# ── Contract: structured fields ─────────────────────────────────────────


async def test_full_entry_roundtrip(vault: VaultBackend) -> None:
    """All three structured additions (entity_type, source_detectors,
    detector_mode + kwargs) survive the round-trip on every backend."""
    entry = VaultEntry(
        surrogates={
            "[PERSON_ABCD1234]": VaultSurrogate(
                original="Alice Smith",
                entity_type="PERSON",
                source_detectors=("regex", "llm"),
            ),
            "[EMAIL_ADDRESS_DEADBEEF]": VaultSurrogate(
                original="alice@example.com",
                entity_type="EMAIL_ADDRESS",
                source_detectors=("regex",),
            ),
        },
        detector_mode=("regex", "llm"),
        kwargs=(
            ("llm", (("model", "anonymize"), ("prompt_name", "default"))),
            ("regex", (("overlap_strategy", None), ("patterns_name", None))),
        ),
    )
    await vault.put("call-full", entry)
    got = await vault.pop("call-full")
    assert got == entry


# ── Contract: aclose ────────────────────────────────────────────────────


async def test_aclose_does_not_raise(vault: VaultBackend) -> None:
    """Every backend's `aclose` must complete without raising. For
    MemoryVault it's a no-op; for RedisVault it drains the connection
    pool. Pipeline.aclose() relies on this contract — a misbehaving
    aclose could hang FastAPI shutdown."""
    # vault fixture already calls aclose on teardown — calling it
    # explicitly here pins the behaviour during normal test flow.
    await vault.aclose()
