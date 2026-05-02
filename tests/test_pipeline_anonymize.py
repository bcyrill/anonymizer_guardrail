"""Smoke tests for Pipeline.anonymize / deanonymize round-trip behaviour
in regex-only mode.

The `pipeline` fixture comes from conftest.py.
"""

from __future__ import annotations

from anonymizer_guardrail.pipeline import Pipeline


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
    await pipeline.anonymize(["email me at bob@example.com"], call_id="evict-test")
    assert pipeline.vault.size() == 1
    await pipeline.deanonymize(["…"], call_id="evict-test")
    assert pipeline.vault.size() == 0
