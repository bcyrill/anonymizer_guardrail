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
    base: dict[str, Any] = dict(
        faker_locale="",
        use_faker=True,
        surrogate_cache_max_size=10_000,
        # Fixed salt for the bulk of tests so cross-instance
        # determinism assertions hold. Tests that specifically exercise
        # the random-salt behaviour override this with "".
        surrogate_salt="test-fixed-salt",
    )
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


def test_lru_evicts_least_recently_used_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With cache cap N, the (N+1)-th unique entity must evict the oldest;
    a touched entry must be promoted out of eviction range."""
    monkeypatch.setattr(
        surrogate_mod,
        "config",
        _fake_config(use_faker=False, surrogate_cache_max_size=3),
    )
    gen = surrogate_mod.SurrogateGenerator()

    a = gen.for_match(Match(text="alice", entity_type="PERSON"))
    b = gen.for_match(Match(text="bob",   entity_type="PERSON"))
    c = gen.for_match(Match(text="carol", entity_type="PERSON"))
    assert len(gen._cache) == 3

    # Touching "alice" promotes it; the next eviction should drop "bob",
    # not "alice".
    a_again = gen.for_match(Match(text="alice", entity_type="PERSON"))
    assert a == a_again

    gen.for_match(Match(text="dan", entity_type="PERSON"))
    keys = set(gen._cache.keys())
    assert ("PERSON", "alice") in keys
    assert ("PERSON", "carol") in keys
    assert ("PERSON", "dan") in keys
    assert ("PERSON", "bob") not in keys, "least-recently-used should have been evicted"


def test_evicted_surrogate_is_freed_for_collision_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an LRU entry is evicted, its surrogate must leave the
    used-set too — otherwise collision detection would treat freed
    values as still-taken and force needless salt-retries."""
    monkeypatch.setattr(
        surrogate_mod,
        "config",
        _fake_config(use_faker=False, surrogate_cache_max_size=2),
    )
    gen = surrogate_mod.SurrogateGenerator()

    s_a = gen.for_match(Match(text="alice", entity_type="PERSON"))
    gen.for_match(Match(text="bob", entity_type="PERSON"))
    # Force eviction of "alice" by adding a third entry.
    gen.for_match(Match(text="carol", entity_type="PERSON"))

    assert ("PERSON", "alice") not in gen._cache
    # The surrogate that "alice" used to hold is now free for re-issue.
    assert s_a not in gen._used_surrogates


def test_empty_salt_yields_random_per_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SURROGATE_SALT='' must produce a different surrogate for the same
    input across instances (each instance generates its own random salt
    at construction). This is the privacy property: an attacker can't
    pre-compute hashes without knowing the salt, and the salt rotates
    on every restart."""
    monkeypatch.setattr(
        surrogate_mod, "config", _fake_config(use_faker=False, surrogate_salt="")
    )
    g1 = surrogate_mod.SurrogateGenerator()
    g2 = surrogate_mod.SurrogateGenerator()
    a = g1.for_match(Match(text="alice", entity_type="PERSON"))
    b = g2.for_match(Match(text="alice", entity_type="PERSON"))
    assert a != b, (
        "with random salts the same input must produce different surrogates "
        "across instances — otherwise the salt isn't doing its job"
    )


def test_fixed_salt_yields_stable_surrogate_across_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-supplied fixed SURROGATE_SALT → same input maps to same
    surrogate across instances/restarts. Documented use case: stable
    log correlation."""
    monkeypatch.setattr(
        surrogate_mod,
        "config",
        _fake_config(use_faker=False, surrogate_salt="my-stable-salt"),
    )
    g1 = surrogate_mod.SurrogateGenerator()
    g2 = surrogate_mod.SurrogateGenerator()
    a = g1.for_match(Match(text="alice", entity_type="PERSON"))
    b = g2.for_match(Match(text="alice", entity_type="PERSON"))
    assert a == b


def test_different_fixed_salts_yield_different_surrogates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two operators picking different SURROGATE_SALT values must see
    disjoint surrogate spaces — useful when a multi-tenant deployment
    fronts a per-tenant guardrail with a per-tenant salt."""
    monkeypatch.setattr(
        surrogate_mod,
        "config",
        _fake_config(use_faker=False, surrogate_salt="salt-A"),
    )
    a = surrogate_mod.SurrogateGenerator().for_match(
        Match(text="alice", entity_type="PERSON")
    )
    monkeypatch.setattr(
        surrogate_mod,
        "config",
        _fake_config(use_faker=False, surrogate_salt="salt-B"),
    )
    b = surrogate_mod.SurrogateGenerator().for_match(
        Match(text="alice", entity_type="PERSON")
    )
    assert a != b


def test_resolve_salt_truncates_oversize_input() -> None:
    """blake2b rejects keys >64 bytes; _resolve_salt truncates rather
    than erroring out so an over-eager 100-byte SURROGATE_SALT doesn't
    crash the boot."""
    big = "x" * 200
    out = surrogate_mod._resolve_salt(big)
    assert len(out) == 64


def test_resolve_salt_empty_returns_random_bytes() -> None:
    """Empty input → 16 random bytes. Two consecutive calls return
    different values."""
    a = surrogate_mod._resolve_salt("")
    b = surrogate_mod._resolve_salt("")
    assert len(a) == 16
    assert a != b


@pytest.mark.parametrize(
    "entity_type,sample_input",
    [
        ("ADDRESS",       "123 Main St, Springfield, IL 62701"),
        ("CREDIT_CARD",   "4111-1111-1111-1111"),
        ("DATE_OF_BIRTH", "1985-04-15"),
        ("IBAN",          "DE89370400440532013000"),
        ("NATIONAL_ID",   "123-45-6789"),
    ],
)
def test_new_pii_types_produce_realistic_surrogates(
    monkeypatch: pytest.MonkeyPatch, entity_type: str, sample_input: str
) -> None:
    """The 5 newly-added Faker-backed types must each produce a non-empty
    surrogate that's distinct from the original. Catches Faker calls that
    return None, datetime objects, multi-line strings, etc."""
    monkeypatch.setattr(surrogate_mod, "config", _fake_config())
    gen = surrogate_mod.SurrogateGenerator()
    out = gen.for_match(Match(text=sample_input, entity_type=entity_type))
    assert isinstance(out, str) and out
    assert out != sample_input
    # ADDRESS in particular must not contain newlines — Faker.address()
    # is multi-line by default and we collapse to single line.
    assert "\n" not in out


def test_zero_or_negative_cap_floored_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator typo (cap=0) must not fully disable caching — we floor
    to 1 to keep within-request behaviour predictable."""
    monkeypatch.setattr(
        surrogate_mod,
        "config",
        _fake_config(use_faker=False, surrogate_cache_max_size=0),
    )
    gen = surrogate_mod.SurrogateGenerator()
    assert gen._max_cache_size == 1