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


async def test_vault_entry_persists_after_deanonymize(pipeline: Pipeline) -> None:
    """Pipeline.deanonymize must NOT evict the vault entry — streaming
    post_call invocations call deanonymize repeatedly with the same
    call_id and growing accumulated text, all needing to see the same
    surrogate map. Eviction is TTL-driven (or via an explicit
    `vault.pop` for utility callers).

    Probed by a manual peek after deanonymize: a non-empty entry
    confirms the surrogate map is still there for the next
    streaming-chunk invocation. Backend-agnostic — works for both
    MemoryVault and RedisVault.
    """
    from anonymizer_guardrail.vault import VaultEntry
    await pipeline.anonymize(["email me at bob@example.com"], call_id="persist-test")
    pre_entry = await pipeline.vault.peek("persist-test")
    assert pre_entry != VaultEntry(), "anonymize should have populated vault"

    await pipeline.deanonymize(["…"], call_id="persist-test")
    post_entry = await pipeline.vault.peek("persist-test")
    assert post_entry == pre_entry, (
        "deanonymize must leave the entry in place so streaming "
        "repeated invocations under the same call_id see the same "
        "surrogate map"
    )

    # Belt-and-braces: a second deanonymize on the same call_id still
    # restores correctly. The pop-based world would have returned
    # surrogate-laden text on the second call.
    out2 = await pipeline.deanonymize(["my email is bob@example.com"], call_id="persist-test")
    assert "bob@example.com" in out2[0]


async def test_streaming_pattern_repeated_deanonymize(pipeline: Pipeline) -> None:
    """Simulates LiteLLM's `UnifiedLLMGuardrails` streaming sampled
    post_call pattern: one anonymize call, then N deanonymize calls
    sharing one call_id with growing accumulated text. Every call
    must restore correctly.

    This is the regression test for the streaming-deanonymize bug
    that motivated the pop → peek switch. Before the fix, the second
    and later calls would find an empty vault and return their input
    unchanged (surrogate still in the response).
    """
    text = "Please contact alice@corp.example for follow-up."
    modified, mapping = await pipeline.anonymize([text], call_id="stream-test")
    # Find the email's surrogate so we can simulate the model echoing
    # it back in chunks.
    surrogate = next(s for s, original in mapping.items() if original == "alice@corp.example")

    # LiteLLM forwards accumulated assistant text every N chunks. Each
    # call gets longer text containing the surrogate.
    samples = [
        f"I will reach out to {surrogate}",
        f"I will reach out to {surrogate} this afternoon.",
        f"I will reach out to {surrogate} this afternoon. Let me know "
        f"if {surrogate} is unavailable.",
    ]
    for sample in samples:
        out = await pipeline.deanonymize([sample], call_id="stream-test")
        assert out[0] == sample.replace(surrogate, "alice@corp.example"), (
            f"streaming sample {sample!r} should have surrogate {surrogate} "
            f"replaced with the original; got {out[0]!r}"
        )


async def test_memory_vault_size_holds_until_pop_or_ttl() -> None:
    """MemoryVault-specific: `vault.size()` increments on `put` and
    stays incremented through deanonymize (peek is read-only).
    Decrement happens on the next explicit `pop` or via the
    TTL-driven sweep on the next put.

    This is the operator-facing `vault_size` `/health` gauge for
    memory deployments. RedisVault's `size()` returns 0 by design
    (sync /health accessor can't run an async SCAN), so the
    size-tracking contract is memory-only.
    """
    p = Pipeline()
    assert isinstance(p.vault, MemoryVault), (
        "this test pins MemoryVault behaviour; conftest.py forces "
        "VAULT_BACKEND=memory by leaving env unset"
    )
    pre = p.vault.size()
    await p.anonymize(["email me at bob@example.com"], call_id="mem-size-test")
    assert p.vault.size() == pre + 1
    await p.deanonymize(["…"], call_id="mem-size-test")
    # Critical: deanonymize is read-only on the vault now. Size should
    # still reflect the entry as in-flight. Operators relying on this
    # gauge see "entry stays until TTL or explicit pop", same as the
    # request-errored-out failure case has always behaved.
    assert p.vault.size() == pre + 1
    # An explicit pop drains it (utility path: tests, manual eviction).
    await p.vault.pop("mem-size-test")
    assert p.vault.size() == pre
