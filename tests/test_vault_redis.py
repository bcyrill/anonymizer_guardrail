"""RedisVault-specific behaviour: atomic GETDEL semantics, TTL via
`EXPIRE`, JSON wire format, key prefix, error wrapping, construction
failures, `size()` placeholder.

Cross-backend contract tests (put/pop roundtrip, pop-missing, empty
inputs, aclose) live in `test_vault.py` and run against RedisVault
automatically via the parametrized `vault` fixture there.

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

from anonymizer_guardrail.vault import VaultEntry, VaultSurrogate
from anonymizer_guardrail.vault_redis import RedisVault, RedisVaultError


def _entry(surrogate: str = "sur", original: str = "orig") -> VaultEntry:
    """Test helper — single-surrogate entry. Backend-specific tests
    don't care about the structured fields."""
    return VaultEntry(
        surrogates={
            surrogate: VaultSurrogate(original=original, entity_type="OTHER"),
        },
    )


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


# Roundtrip / pop-missing / empty-input contract tests live in
# `test_vault.py` and run against this backend automatically via the
# parametrized `vault` fixture there. This file only owns
# RedisVault-specific behaviour.


# ── Atomic GETDEL semantics ─────────────────────────────────────────────


async def test_getdel_only_one_pop_wins(vault: RedisVault) -> None:
    """Two concurrent pops on the same call_id: one wins (gets the
    entry), the other gets empty. GETDEL is atomic, so the race is
    won by the first server-side request — not partially observed."""
    entry = _entry("k", "v")
    await vault.put("call-race", entry)

    # Run two pops in parallel against the same key.
    results = await asyncio.gather(
        vault.pop("call-race"),
        vault.pop("call-race"),
    )
    # Exactly one should have the entry; the other should be empty.
    non_empty = [r for r in results if not r.is_empty]
    empty = [r for r in results if r.is_empty]
    assert len(non_empty) == 1
    assert non_empty[0] == entry
    assert len(empty) == 1


# ── TTL ─────────────────────────────────────────────────────────────────


async def test_ttl_set_on_put() -> None:
    """`put` sets EXPIRE so Redis evicts the entry server-side."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        v = RedisVault(url="redis://fake/0", ttl_s=10)

    await v.put("call-ttl", _entry("k", "v"))
    # Inspect TTL via the underlying fakeredis client. -1 = no expire,
    # -2 = key doesn't exist, positive = seconds remaining.
    ttl = await fake_client.ttl("vault:call-ttl")
    assert ttl > 0 and ttl <= 10
    await v.aclose()


async def test_re_put_refreshes_ttl() -> None:
    """A second `put` for the same call_id must reset the TTL to the
    full window (Redis's `SET ... EX` semantics — pin so a future
    refactor that uses `SETEX NX` doesn't silently break the contract).
    A re-put on an existing entry is rare in practice but the
    `MemoryVault` path explicitly re-stores the timestamp on re-put,
    so the Redis path should mirror that."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        v = RedisVault(url="redis://fake/0", ttl_s=60)

    await v.put("call-refresh", _entry("k", "v1"))
    # Manually wind the TTL down (fakeredis supports EXPIRE) so we can
    # tell whether the second put resets it.
    await fake_client.expire("vault:call-refresh", 5)
    ttl_before = await fake_client.ttl("vault:call-refresh")
    assert ttl_before <= 5

    # Second put — should reset TTL to the full window.
    await v.put("call-refresh", _entry("k", "v2"))
    ttl_after = await fake_client.ttl("vault:call-refresh")
    assert ttl_after > 5  # refreshed
    assert ttl_after <= 60

    # And the value was overwritten.
    assert await v.pop("call-refresh") == _entry("k", "v2")
    await v.aclose()


# ── JSON serialization ──────────────────────────────────────────────────


async def test_value_is_json_encoded_with_full_shape() -> None:
    """The wire format is JSON with the three-key top-level object
    (surrogates / detector_mode / kwargs). Operators debugging from
    `redis-cli GET vault:<id>` should see readable structured JSON —
    pin that contract."""
    import fakeredis.aioredis as fakeredis

    fake_client = fakeredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        v = RedisVault(url="redis://fake/0", ttl_s=60)

    entry = VaultEntry(
        surrogates={
            "[PERSON_ABCD1234]": VaultSurrogate(
                original="Alice", entity_type="PERSON",
                source_detectors=("regex", "llm"),
            ),
        },
        detector_mode=("regex", "llm"),
        kwargs=(("llm", (("model", "anon"),)),),
    )
    await v.put("call-json", entry)

    raw = await fake_client.get("vault:call-json")
    assert isinstance(raw, str)
    decoded = json.loads(raw)
    # Structured shape — pin each top-level key so a future refactor
    # that flattens or renames the wire format breaks loud.
    assert decoded == {
        "surrogates": {
            "[PERSON_ABCD1234]": ["Alice", "PERSON", ["regex", "llm"]],
        },
        "detector_mode": ["regex", "llm"],
        "kwargs": [["llm", [["model", "anon"]]]],
    }
    await v.aclose()


async def test_corrupted_json_returns_empty_logs_error(
    vault: RedisVault, caplog: pytest.LogCaptureFixture,
) -> None:
    """If a vault entry is corrupted (e.g. another process wrote the
    key with a non-JSON value), pop should return an empty entry and
    log ERROR — same severity contract as MemoryVault's TTL-expiry case."""
    import logging

    # Inject a non-JSON value directly via the underlying client.
    await vault._client.set("vault:call-corrupt", "this-is-not-json", ex=60)

    with caplog.at_level(logging.ERROR, logger="anonymizer.vault.redis"):
        result = await vault.pop("call-corrupt")
    assert result == VaultEntry()
    assert any("non-JSON" in r.message for r in caplog.records)


async def test_unexpected_json_shape_returns_empty_logs_error(
    vault: RedisVault, caplog: pytest.LogCaptureFixture,
) -> None:
    """A JSON value with the wrong shape (not the three-key object)
    surfaces as ERROR + empty entry, same contract as raw-JSON
    corruption. Pins the decoder's defensive guarding so a malformed
    write from a different process version doesn't crash the
    deanonymize path."""
    import logging

    # Legacy flat shape (just a string→string dict — what the previous
    # vault wrote) — must not silently round-trip; the new decoder
    # treats it as corrupted and returns empty.
    await vault._client.set(
        "vault:call-legacy",
        json.dumps({"sur1": "orig1"}),
        ex=60,
    )

    with caplog.at_level(logging.ERROR, logger="anonymizer.vault.redis"):
        result = await vault.pop("call-legacy")
    assert result == VaultEntry()
    assert any("unexpected JSON shape" in r.message for r in caplog.records)


# ── Key namespacing ─────────────────────────────────────────────────────


async def test_key_is_prefixed_with_vault(vault: RedisVault) -> None:
    """Keys go under the `vault:` prefix so the backend can share a
    Redis instance with other consumers without colliding."""
    await vault.put("my-call", _entry())

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
        await v.put("call-1", _entry())


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
    await vault.put("call-1", _entry())
    await vault.put("call-2", _entry())
    assert vault.size() == 0


# ── Frozen-kwargs round-trip with list-valued kwargs ──────────────────


async def test_frozen_kwargs_with_tuple_value_round_trips(
    vault: RedisVault,
) -> None:
    """gliner's `labels` kwarg is a tuple in `FrozenKwargs`. JSON
    encodes tuples as arrays; on decode we MUST coerce back to tuple
    so the post-pop kwargs dict + cache_key_for round-trip produces
    the same hashable shape a freshly-frozen kwargs would.

    Regression: without `_coerce_value`'s list→tuple step in the
    decoder, `entry.kwargs[i][1]` would carry a list at deanonymize
    time and a tuple at anonymize time → mismatched hashes →
    pipeline cache misses on every prewarm."""
    from anonymizer_guardrail.vault import VaultEntry, VaultSurrogate

    entry = VaultEntry(
        surrogates={
            "[PERSON_A1B2C3D4]": VaultSurrogate(
                original="alice", entity_type="PERSON",
                source_detectors=("regex", "gliner_pii"),
            ),
        },
        detector_mode=("regex", "gliner_pii"),
        # Tuple-valued kwarg simulating gliner's frozen labels.
        kwargs=(
            ("regex", (("overlap_strategy", None),)),
            ("gliner_pii", (
                ("labels", ("person", "organization")),
                ("threshold", 0.5),
            )),
        ),
    )
    await vault.put("call-frozen", entry)
    got = await vault.pop("call-frozen")

    # Equality: tuple-typed labels must come back as tuples, not lists.
    assert got == entry
    gliner_kwargs = dict(got.kwargs)["gliner_pii"]
    labels_pair = next((k, v) for k, v in gliner_kwargs if k == "labels")
    assert isinstance(labels_pair[1], tuple), (
        f"labels must round-trip as tuple, got {type(labels_pair[1]).__name__}"
    )
    assert labels_pair[1] == ("person", "organization")
