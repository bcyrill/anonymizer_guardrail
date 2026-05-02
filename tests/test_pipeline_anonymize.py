"""Smoke tests for Pipeline.anonymize / deanonymize round-trip behaviour
in regex-only mode.

The `pipeline` fixture is locally overridden to parametrize over both
vault backends (`memory`, `redis`). Tests that work for both run twice;
tests that depend on MemoryVault-specific behaviour (e.g. asserting
`vault.size()` directly) skip the Redis parametrization explicitly.

This catches any accidental coupling between Pipeline.anonymize /
deanonymize and MemoryVault semantics — anonymize wires the vault via
the `VaultBackend` Protocol, so it should be backend-agnostic.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from anonymizer_guardrail.pipeline import Pipeline
from anonymizer_guardrail.vault_memory import MemoryVault


@pytest.fixture(params=["memory", "redis"])
def pipeline(request, monkeypatch) -> Pipeline:
    """Override conftest's `pipeline` fixture: parametrize on backend.

    For the redis branch, we patch the central config to select the
    Redis backend AND intercept `redis.asyncio.from_url` to return a
    fakeredis client — same trick used in `tests/test_vault.py`'s
    parametrized contract fixture. The Pipeline's `build_vault()`
    factory reads the patched config and constructs a RedisVault
    against the fake.

    The `vault.config` import is what `build_vault()` reads (vault.py
    does `from .config import config`), so we patch the module-level
    binding there. Patching only `config_mod.config` would miss this
    because `from … import` creates a separate name binding.
    """
    if request.param == "memory":
        return Pipeline()

    from anonymizer_guardrail import vault as vault_mod
    monkeypatch.setattr(
        vault_mod, "config",
        vault_mod.config.model_copy(update={
            "vault_backend": "redis",
            "vault_redis_url": "redis://fake/0",
        }),
    )

    import fakeredis.aioredis as fakeredis_aioredis
    fake_client = fakeredis_aioredis.FakeRedis(decode_responses=True)
    with patch("redis.asyncio.from_url", return_value=fake_client):
        return Pipeline()


async def test_round_trip_restores_original(pipeline: Pipeline) -> None:
    original = (
        "Connect to 10.20.30.40 using token sk-abc123XYZ456defGHI789jklMNO012p "
        "and email alice@acmecorp.com."
    )

    modified, mapping = await pipeline.anonymize([original], call_id="test-1")
    assert modified[0] != original
    assert "10.20.30.40" not in modified[0]
    assert "alice@acmecorp.com" not in modified[0]
    assert "sk-abc123XYZ456defGHI789jklMNO012p" not in modified[0]
    assert len(mapping) >= 3

    restored = await pipeline.deanonymize(modified, call_id="test-1")
    assert restored[0] == original


async def test_same_entity_gets_consistent_surrogate(pipeline: Pipeline) -> None:
    text_a = "host A: 10.0.0.1"
    text_b = "host A again: 10.0.0.1"

    modified, mapping = await pipeline.anonymize([text_a, text_b], call_id="test-2")
    # Both texts mention 10.0.0.1 — the surrogate should match.
    surrogate = next(s for s, o in mapping.items() if o == "10.0.0.1")
    assert surrogate in modified[0]
    assert surrogate in modified[1]


async def test_unknown_call_id_passes_through(pipeline: Pipeline) -> None:
    text = "no mapping was ever stored for this call"
    out = await pipeline.deanonymize([text], call_id="never-stored")
    assert out == [text]


async def test_empty_input(pipeline: Pipeline) -> None:
    modified, mapping = await pipeline.anonymize([], call_id="empty")
    assert modified == []
    assert mapping == {}


async def test_no_entities_produces_empty_mapping(pipeline: Pipeline) -> None:
    text = "the quick brown fox jumps over the lazy dog"
    modified, mapping = await pipeline.anonymize([text], call_id="boring")
    assert modified == [text]
    assert mapping == {}


async def test_vault_evicts_after_pop(pipeline: Pipeline) -> None:
    """Pipeline.deanonymize must call vault.pop, leaving no residue
    behind. Probed via a manual pop after deanonymize: a clean eviction
    means the second pop returns `{}`. Backend-agnostic — RedisVault's
    `size()` returns 0 unconditionally so we can't use that probe."""
    await pipeline.anonymize(["email me at bob@example.com"], call_id="evict-test")
    await pipeline.deanonymize(["…"], call_id="evict-test")
    # Manual pop — anything still in the vault under this call_id
    # would surface here. `{}` confirms deanonymize evicted it.
    assert await pipeline.vault.pop("evict-test") == {}


async def test_memory_vault_size_reflects_in_flight_entries() -> None:
    """MemoryVault-specific: `vault.size()` increments on `put` and
    decrements on `pop`. This is the operator-facing `vault_size`
    `/health` gauge for memory deployments. RedisVault's `size()`
    returns 0 by design (sync /health accessor can't run an async
    SCAN), so the size-tracking contract is memory-only."""
    p = Pipeline()
    assert isinstance(p.vault, MemoryVault), (
        "this test pins MemoryVault behaviour; conftest.py forces "
        "VAULT_BACKEND=memory by leaving env unset"
    )
    pre = p.vault.size()
    await p.anonymize(["email me at bob@example.com"], call_id="mem-size-test")
    assert p.vault.size() == pre + 1
    await p.deanonymize(["…"], call_id="mem-size-test")
    assert p.vault.size() == pre
