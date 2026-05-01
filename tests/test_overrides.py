"""Per-request override tests.

Verifies that values landing in `additional_provider_specific_params`
take effect on the corresponding detector / surrogate generator path
without affecting the no-override case.
"""

from __future__ import annotations

import os

# Match the other tests: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import logging

import pytest

from anonymizer_guardrail.api import Overrides, parse_overrides
from anonymizer_guardrail.detector.base import Match
from anonymizer_guardrail.detector import regex as regex_mod
from anonymizer_guardrail.surrogate import SurrogateGenerator


# ── parse_overrides ─────────────────────────────────────────────────────────
def test_parse_overrides_empty_returns_default() -> None:
    o = parse_overrides({})
    assert o == Overrides.empty()
    assert parse_overrides(None) == Overrides.empty()


def test_parse_overrides_accepts_known_keys() -> None:
    o = parse_overrides({
        "use_faker": False,
        "faker_locale": "pt_BR,en_US",
        "detector_mode": ["regex", "llm"],
        "regex_overlap_strategy": "PRIORITY",
        "regex_patterns": "pentest",
        "llm_model": "gpt-4o-mini",
        "llm_prompt": "legal",
        "denylist": "marketing",
    })
    assert o.use_faker is False
    assert o.faker_locale == ("pt_BR", "en_US")
    assert o.detector_mode == ("regex", "llm")
    assert o.regex_overlap_strategy == "priority"  # normalized lowercase
    assert o.regex_patterns == "pentest"
    assert o.llm_model == "gpt-4o-mini"
    assert o.llm_prompt == "legal"
    assert o.denylist == "marketing"


def test_parse_overrides_named_alternatives_strip_whitespace() -> None:
    """`regex_patterns`, `llm_prompt`, and `denylist` accept names — the
    parser strips surrounding whitespace, mirroring how detector_mode
    tokens are normalised."""
    o = parse_overrides({
        "regex_patterns": "  pentest  ",
        "llm_prompt": "\tlegal\n",
        "denylist": " legal ",
    })
    assert o.regex_patterns == "pentest"
    assert o.llm_prompt == "legal"
    assert o.denylist == "legal"


def test_parse_overrides_named_alternatives_empty_string_means_default() -> None:
    """An empty/whitespace name doesn't pin an override — caller will
    fall through to the configured default."""
    o = parse_overrides(
        {"regex_patterns": "", "llm_prompt": "   ", "denylist": ""}
    )
    assert o.regex_patterns is None
    assert o.llm_prompt is None
    assert o.denylist is None


def test_parse_overrides_named_alternatives_reject_non_string(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wrong type → warn + drop. Matches the warn-and-drop policy of
    the other knobs; the request still proceeds with the default."""
    caplog.set_level(logging.WARNING)
    o = parse_overrides(
        {"regex_patterns": 42, "llm_prompt": ["a", "b"], "denylist": 7}
    )
    assert o.regex_patterns is None
    assert o.llm_prompt is None
    assert o.denylist is None
    msgs = " | ".join(r.message for r in caplog.records)
    assert "regex_patterns" in msgs
    assert "llm_prompt" in msgs
    assert "denylist" in msgs


def test_parse_overrides_locale_array_form_normalizes_to_tuple() -> None:
    o = parse_overrides({"faker_locale": ["pt_BR", "en_US"]})
    assert o.faker_locale == ("pt_BR", "en_US")


def test_parse_overrides_invalid_value_warns_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bad values for known keys log a warning and are silently dropped;
    other keys in the same dict still take effect."""
    caplog.set_level(logging.WARNING)
    o = parse_overrides({
        "use_faker": "yes",                      # bad type
        "regex_overlap_strategy": "best",        # unknown value
        "llm_model": "gpt-4o-mini",              # valid
    })
    assert o.use_faker is None
    assert o.regex_overlap_strategy is None
    assert o.llm_model == "gpt-4o-mini"
    msgs = " | ".join(r.message for r in caplog.records)
    assert "use_faker" in msgs
    assert "regex_overlap_strategy" in msgs


def test_parse_overrides_unknown_key_silently_ignored() -> None:
    """LiteLLM forward-compat: a stray param shouldn't BLOCK or warn loud."""
    o = parse_overrides({"future_thing": True})
    assert o == Overrides.empty()


def test_parse_overrides_empty_locale_string_means_default() -> None:
    o = parse_overrides({"faker_locale": ""})
    assert o.faker_locale is None


def test_parse_overrides_oversize_locale_chain_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive cap: a chain of 50 locales is malformed input or abuse.
    Caller gets a warning + fallback to default — request proceeds, no
    Faker is constructed."""
    caplog.set_level(logging.WARNING)
    huge = ",".join(f"en_{i:02d}" for i in range(50))
    o = parse_overrides({"faker_locale": huge, "use_faker": True})
    assert o.faker_locale is None
    assert o.use_faker is True
    assert any("exceeds cap" in r.message for r in caplog.records)


def test_parse_overrides_oversize_detector_list_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    o = parse_overrides({"detector_mode": [f"x{i}" for i in range(20)]})
    assert o.detector_mode is None
    assert any("exceeds cap" in r.message for r in caplog.records)


# ── Surrogate cache key with overrides ──────────────────────────────────────
def test_surrogate_override_use_faker_separates_cache_buckets() -> None:
    """use_faker=False forces opaque tokens regardless of the configured
    default; entries with that override land in their own cache slot
    so a default-config request can still produce realistic surrogates
    for the same input."""
    gen = SurrogateGenerator()
    m = Match(text="Alice Smith", entity_type="PERSON")
    default = gen.for_match(m)
    overridden = gen.for_match(m, use_faker=False)
    # Override forced opaque-token shape.
    assert overridden.startswith("[PERSON_")
    # Both cached, separately.
    assert (m.entity_type, m.text, gen._use_faker, None) in gen._cache
    assert (m.entity_type, m.text, False, None) in gen._cache
    # And consistent across calls for the override bucket too.
    assert gen.for_match(m, use_faker=False) == overridden
    # Default behaviour unaffected.
    assert gen.for_match(m) == default


def test_surrogate_override_locale_caches_per_locale_tuple() -> None:
    """Different locale tuples → different cache slots, each consistent
    on its own."""
    gen = SurrogateGenerator()
    m = Match(text="Alice Smith", entity_type="PERSON")
    pt = gen.for_match(m, use_faker=True, locale=("pt_BR",))
    de = gen.for_match(m, use_faker=True, locale=("de_DE",))
    # Likely different (not strictly guaranteed if Faker hashed identically,
    # but extremely unlikely with seeded Faker output across locales).
    # At minimum the cache key separates them and per-locale calls are stable.
    assert gen.for_match(m, use_faker=True, locale=("pt_BR",)) == pt
    assert gen.for_match(m, use_faker=True, locale=("de_DE",)) == de
    assert ("PERSON", "Alice Smith", True, ("pt_BR",)) in gen._cache
    assert ("PERSON", "Alice Smith", True, ("de_DE",)) in gen._cache


def test_surrogate_override_invalid_locale_warns_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bogus locale logs a warning and falls back to the default Faker
    instance — the request still succeeds."""
    gen = SurrogateGenerator()
    m = Match(text="Bob", entity_type="PERSON")
    caplog.set_level(logging.WARNING)
    out = gen.for_match(m, use_faker=True, locale=("zz_ZZ",))
    assert out
    assert any("zz_ZZ" in r.message for r in caplog.records)


# ── Regex detector override ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_regex_overlap_strategy_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`overlap_strategy` kwarg overrides config.regex_overlap_strategy
    for one call."""
    from types import SimpleNamespace
    monkeypatch.setattr(
        regex_mod,
        "config",
        SimpleNamespace(
            regex_patterns_path="bundled:regex_pentest.yaml",
            regex_overlap_strategy="priority",
        ),
    )
    monkeypatch.setattr(regex_mod, "_COMPILED_PATTERNS", regex_mod._load_patterns())
    detector = regex_mod.RegexDetector()
    text = "test 550e8400-e29b-41d4-a716-446655440000."

    # Default path uses config.regex_overlap_strategy = "priority" →
    # the inner \d{12} match shadows the UUID.
    matches = await detector.detect(text)
    by_text = {m.text: m.entity_type for m in matches}
    assert "446655440000" in by_text
    assert "550e8400-e29b-41d4-a716-446655440000" not in by_text

    # Per-call override flips to "longest" — UUID wins.
    matches = await detector.detect(text, overlap_strategy="longest")
    by_text = {m.text: m.entity_type for m in matches}
    assert by_text.get("550e8400-e29b-41d4-a716-446655440000") == "UUID"
    assert "446655440000" not in by_text


@pytest.mark.asyncio
async def test_regex_overlap_strategy_invalid_raises() -> None:
    detector = regex_mod.RegexDetector()
    with pytest.raises(ValueError, match="Invalid overlap_strategy"):
        await detector.detect("foo", overlap_strategy="bogus")


# ── Denylist detector override ─────────────────────────────────────────────
from anonymizer_guardrail.detector import denylist as deny_mod


def _index_from_yaml(text: str, tmp_path, name: str) -> deny_mod._Index:
    """Compile a denylist YAML body into an index suitable for assigning
    onto DenylistDetector._registry. The detector itself doesn't read
    the file — registries are pre-built at import time — so per-test we
    bypass that and inject indices directly."""
    p = tmp_path / f"{name}.yaml"
    p.write_text(text, encoding="utf-8")
    return deny_mod._build_index(deny_mod._load_entries(str(p)))


@pytest.mark.asyncio
async def test_denylist_override_selects_named_registry_entry(tmp_path) -> None:
    """Per-call `denylist_name` override picks an entry from the registry
    instead of the default. Confirms detection switches behaviour
    cleanly for one call without persisting state."""
    legal_yaml = (
        "entries:\n"
        "  - type: ORGANIZATION\n"
        "    value: ACME-LEGAL\n"
    )
    marketing_yaml = (
        "entries:\n"
        "  - type: ORGANIZATION\n"
        "    value: ACME-MKT\n"
    )

    detector = deny_mod.DenylistDetector()
    detector._registry = {
        "legal": _index_from_yaml(legal_yaml, tmp_path, "legal"),
        "marketing": _index_from_yaml(marketing_yaml, tmp_path, "marketing"),
    }

    text = "Notes: ACME-LEGAL and ACME-MKT both appear here."

    legal_matches = await detector.detect(text, denylist_name="legal")
    assert {m.text for m in legal_matches} == {"ACME-LEGAL"}

    mkt_matches = await detector.detect(text, denylist_name="marketing")
    assert {m.text for m in mkt_matches} == {"ACME-MKT"}


@pytest.mark.asyncio
async def test_denylist_override_unknown_name_falls_back_to_default(
    tmp_path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Typo in the per-call `denylist` name doesn't BLOCK the request —
    log a warning, fall through to the default. Mirrors the
    regex_patterns / llm_prompt fallback contract."""
    default_yaml = (
        "entries:\n"
        "  - type: ORGANIZATION\n"
        "    value: DefaultListTerm\n"
    )

    detector = deny_mod.DenylistDetector()
    detector._default_index = _index_from_yaml(
        default_yaml, tmp_path, "default",
    )
    detector._registry = {
        "legal": _index_from_yaml(
            "entries:\n  - type: ORGANIZATION\n    value: LEGAL_TERM\n",
            tmp_path, "legal",
        ),
    }

    text = "DefaultListTerm and LEGAL_TERM appear in the same line."
    caplog.set_level(logging.WARNING, logger="anonymizer.denylist")
    matches = await detector.detect(text, denylist_name="nonexistent")
    assert {m.text for m in matches} == {"DefaultListTerm"}
    assert any(
        "nonexistent" in r.message and "fallback" in r.message.lower() or
        "falling back" in r.message.lower()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_denylist_override_default_keyword_uses_default_list(
    tmp_path,
) -> None:
    """The literal name `default` is reserved (the registry loader
    rejects it). Passing it as an override is a no-op — same as
    passing None — and routes through the default list."""
    default_yaml = (
        "entries:\n"
        "  - type: ORGANIZATION\n"
        "    value: DefaultListTerm\n"
    )

    detector = deny_mod.DenylistDetector()
    detector._default_index = _index_from_yaml(
        default_yaml, tmp_path, "default",
    )
    detector._registry = {
        "legal": _index_from_yaml(
            "entries:\n  - type: ORGANIZATION\n    value: LEGAL_TERM\n",
            tmp_path, "legal",
        ),
    }

    text = "Both DefaultListTerm and LEGAL_TERM appear here."
    no_override = await detector.detect(text)
    explicit_default = await detector.detect(text, denylist_name="default")
    assert {m.text for m in no_override} == {"DefaultListTerm"}
    assert {m.text for m in explicit_default} == {"DefaultListTerm"}


def test_denylist_registry_loader_rejects_default_name() -> None:
    """The shared registry parser bans the reserved `default` name —
    putting one into DENYLIST_REGISTRY must crash boot, not silently
    shadow DENYLIST_PATH."""
    from anonymizer_guardrail.registry import parse_named_path_registry
    with pytest.raises(RuntimeError, match="reserved"):
        parse_named_path_registry(
            "default=/etc/anon/default.yaml", "DENYLIST_REGISTRY"
        )


def test_denylist_registry_loader_compiles_each_entry(tmp_path) -> None:
    """`_load_registry` walks DENYLIST_REGISTRY and produces one compiled
    index per name. A typo in any path crashes boot — same loud-fail
    contract as the default path."""
    p_legal = tmp_path / "legal.yaml"
    p_legal.write_text(
        "entries:\n  - type: ORGANIZATION\n    value: ACME-LEGAL\n",
        encoding="utf-8",
    )
    p_mkt = tmp_path / "marketing.yaml"
    p_mkt.write_text(
        "entries:\n  - type: ORGANIZATION\n    value: ACME-MKT\n",
        encoding="utf-8",
    )

    from types import SimpleNamespace
    fake_cfg = SimpleNamespace(
        denylist_path="",
        denylist_registry=f"legal={p_legal},marketing={p_mkt}",
        denylist_backend="regex",
    )
    import unittest.mock
    with unittest.mock.patch.object(deny_mod, "config", fake_cfg):
        registry = deny_mod._load_registry()
    assert set(registry) == {"legal", "marketing"}
    # Each compiled index is a candidate-generator function — calling it
    # against a string containing the value should yield one match.
    for name, candidates_fn in registry.items():
        sample = f"prefix {('ACME-LEGAL' if name == 'legal' else 'ACME-MKT')} suffix"
        out = candidates_fn(sample)
        assert len(out) == 1, f"{name} compiled to an empty/wrong index: {out}"


# ── Faker LRU cache bound (OOM safety) ──────────────────────────────────────
def test_surrogate_faker_lru_evicts_oldest_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cycling through more distinct locale tuples than _FAKER_LRU_MAX
    must NOT grow the cache without bound. Cap is enforced via LRU
    eviction; the dropped Faker is reconstructible on demand."""
    from anonymizer_guardrail import surrogate as surrogate_mod
    monkeypatch.setattr(surrogate_mod, "_FAKER_LRU_MAX", 3)
    gen = SurrogateGenerator()
    m = Match(text="Alice", entity_type="PERSON")

    # Use four real Faker locales so each one constructs successfully.
    for loc in [("pt_BR",), ("de_DE",), ("fr_FR",), ("es_ES",)]:
        gen.for_match(m, use_faker=True, locale=loc)
    assert len(gen._faker_by_locale) == 3, "LRU cap must hold"
    # The oldest entry (pt_BR) should be the one evicted.
    assert ("pt_BR",) not in gen._faker_by_locale
    assert ("es_ES",) in gen._faker_by_locale
