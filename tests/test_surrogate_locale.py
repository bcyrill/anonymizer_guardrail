"""Tests for FAKER_LOCALE plumbing in the surrogate generator."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

# Match the other test files: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail import surrogate as surrogate_mod
from anonymizer_guardrail.detector.base import Match


def _fake_config(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(faker_locale="", use_faker=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_default_locale_uses_faker_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty FAKER_LOCALE → SurrogateGenerator boots and produces a name."""
    monkeypatch.setattr(surrogate_mod, "config", _fake_config())
    gen = surrogate_mod.SurrogateGenerator()
    out = gen.for_match(Match(text="Original Person", entity_type="PERSON"))
    assert isinstance(out, str) and out
    assert out != "Original Person"


def test_locale_changes_surrogate_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A different locale produces a different surrogate for the same input
    — the seed is identical, so any divergence is locale-driven."""
    monkeypatch.setattr(surrogate_mod, "config", _fake_config(faker_locale="en_US"))
    en_us = surrogate_mod.SurrogateGenerator().for_match(
        Match(text="Anchor Name", entity_type="PERSON")
    )
    monkeypatch.setattr(surrogate_mod, "config", _fake_config(faker_locale="ja_JP"))
    ja_jp = surrogate_mod.SurrogateGenerator().for_match(
        Match(text="Anchor Name", entity_type="PERSON")
    )
    assert en_us != ja_jp, (
        "Locale should affect surrogate output for the same seed/match"
    )


def test_multi_locale_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma-separated list is accepted (Faker uses subsequent locales as
    fallback when the primary doesn't implement a provider)."""
    monkeypatch.setattr(
        surrogate_mod, "config", _fake_config(faker_locale="pt_BR, en_US")
    )
    gen = surrogate_mod.SurrogateGenerator()
    out = gen.for_match(Match(text="Some Name", entity_type="PERSON"))
    assert isinstance(out, str) and out


def test_invalid_locale_fails_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typos must surface at boot, not at first request."""
    monkeypatch.setattr(
        surrogate_mod, "config", _fake_config(faker_locale="not_A_locale")
    )
    with pytest.raises(RuntimeError, match="not a valid Faker locale"):
        surrogate_mod.SurrogateGenerator()


def test_use_faker_false_emits_opaque_for_realistic_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USE_FAKER=false → realistic-type surrogates become opaque tokens
    (e.g. `[PERSON_…]` instead of a Faker-generated name)."""
    monkeypatch.setattr(surrogate_mod, "config", _fake_config(use_faker=False))
    gen = surrogate_mod.SurrogateGenerator()

    out = gen.for_match(Match(text="alice", entity_type="PERSON"))
    assert out.startswith("[PERSON_") and out.endswith("]")

    out = gen.for_match(Match(text="acmecorp", entity_type="ORGANIZATION"))
    assert out.startswith("[ORGANIZATION_") and out.endswith("]")

    # Already-opaque types still work the same.
    out = gen.for_match(Match(text="some-hash", entity_type="HASH"))
    assert out.startswith("[HASH_") and out.endswith("]")


def test_use_faker_false_skips_locale_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Faker is off, an invalid locale shouldn't crash boot —
    Faker is never instantiated, so the locale value is moot."""
    monkeypatch.setattr(
        surrogate_mod,
        "config",
        _fake_config(use_faker=False, faker_locale="garbage"),
    )
    # Should construct without raising; the locale is unused.
    surrogate_mod.SurrogateGenerator()


def test_use_faker_false_outputs_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same (text, type) → same opaque token across instances."""
    monkeypatch.setattr(surrogate_mod, "config", _fake_config(use_faker=False))
    g1 = surrogate_mod.SurrogateGenerator()
    g2 = surrogate_mod.SurrogateGenerator()
    a = g1.for_match(Match(text="alice", entity_type="PERSON"))
    b = g2.for_match(Match(text="alice", entity_type="PERSON"))
    assert a == b


def test_surrogate_collision_resolved_with_unique_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distinct originals must always produce distinct surrogates.
    Otherwise the vault (keyed by surrogate→original) would lose one of
    them and deanonymize would corrupt the response."""
    monkeypatch.setattr(surrogate_mod, "config", _fake_config(use_faker=False))
    gen = surrogate_mod.SurrogateGenerator()

    # Pre-populate the used set with the surrogate the first call would
    # naturally produce, simulating the rare case where two seeds collide.
    pre = gen.for_match(Match(text="alice", entity_type="PERSON"))
    assert pre  # baseline

    # Forcibly insert that value as if another entity had already taken it,
    # so the next NEW (text,type) combo has to salt-retry.
    gen._used_surrogates.add(pre)
    gen._cache[("PERSON", "carol")] = pre  # sanity: this combo not actually issued
    del gen._cache[("PERSON", "carol")]    # remove again, keep used set polluted

    out = gen.for_match(Match(text="bob", entity_type="PERSON"))
    assert out != pre, "salt-retry must produce a value not in _used_surrogates"


def test_collision_with_original_still_avoided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long-standing guarantee: surrogate must never equal the input."""
    monkeypatch.setattr(surrogate_mod, "config", _fake_config(use_faker=False))
    gen = surrogate_mod.SurrogateGenerator()
    out = gen.for_match(Match(text="anything", entity_type="OTHER"))
    assert out != "anything"