"""Tests for the YAML-driven denylist detector."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

# Match the other tests: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.detector import denylist as deny_mod


def _fake_config(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(denylist_path="")
    base.update(overrides)
    return SimpleNamespace(**base)


def _detector_with(entries_yaml: str, tmp_path) -> deny_mod.DenylistDetector:
    """Build a detector wired to a tmp YAML file. Avoids touching the
    module-level _DEFAULT_INDEX so tests don't leak into each other."""
    p = tmp_path / "deny.yaml"
    p.write_text(entries_yaml, encoding="utf-8")
    entries = deny_mod._load_entries(str(p), "DENYLIST_PATH")
    detector = deny_mod.DenylistDetector()
    detector._default_index = deny_mod._build_index(entries)  # type: ignore[attr-defined]
    return detector


# ── Empty / missing path ────────────────────────────────────────────────────


async def test_unset_path_yields_no_matches() -> None:
    """No DENYLIST_PATH set → detector loads, registers, matches nothing.
    This is the default boot path so it has to be a no-op, not a crash."""
    detector = deny_mod.DenylistDetector()
    matches = await detector.detect("any text containing anything at all")
    assert matches == []


async def test_empty_yaml_yields_no_matches(tmp_path) -> None:
    detector = _detector_with("", tmp_path)
    assert await detector.detect("hello world") == []


async def test_yaml_with_no_entries_key_yields_no_matches(tmp_path) -> None:
    detector = _detector_with("# only a comment\n", tmp_path)
    assert await detector.detect("anything") == []


# ── Basic matching ──────────────────────────────────────────────────────────


async def test_basic_literal_match(tmp_path) -> None:
    yaml_text = """
entries:
  - type: ORGANIZATION
    value: Acme
"""
    detector = _detector_with(yaml_text, tmp_path)
    matches = await detector.detect("I work at Acme today")
    assert len(matches) == 1
    assert matches[0].text == "Acme"
    assert matches[0].entity_type == "ORGANIZATION"


async def test_no_match_when_term_absent(tmp_path) -> None:
    detector = _detector_with(
        "entries:\n  - type: PERSON\n    value: Alice\n", tmp_path,
    )
    assert await detector.detect("nothing relevant here") == []


# ── Case sensitivity ────────────────────────────────────────────────────────


async def test_case_sensitive_default_does_not_match_different_case(tmp_path) -> None:
    """Default is case-sensitive — `Acme` does not match `acme` or `ACME`."""
    detector = _detector_with(
        "entries:\n  - type: ORGANIZATION\n    value: Acme\n", tmp_path,
    )
    matches = await detector.detect("acme corp and ACME inc")
    assert matches == []


async def test_case_insensitive_matches_any_casing(tmp_path) -> None:
    yaml_text = """
entries:
  - type: ORGANIZATION
    value: Acme
    case_sensitive: false
"""
    detector = _detector_with(yaml_text, tmp_path)
    matches = await detector.detect("acme corp and ACME inc and Acme too")
    assert len(matches) == 3
    # Each preserves the source-text casing in Match.text — important for
    # downstream replacement so the surrogate fits the text it's replacing.
    assert {m.text for m in matches} == {"acme", "ACME", "Acme"}
    assert {m.entity_type for m in matches} == {"ORGANIZATION"}


# ── Word boundaries ─────────────────────────────────────────────────────────


async def test_word_boundary_default_does_not_match_inside_word(tmp_path) -> None:
    """`Bob` should not match inside `Bobby` — that's the whole reason
    word boundaries are the default."""
    detector = _detector_with(
        "entries:\n  - type: PERSON\n    value: Bob\n", tmp_path,
    )
    matches = await detector.detect("Bobby works for Robert")
    assert matches == []


async def test_word_boundary_matches_at_word_edges(tmp_path) -> None:
    detector = _detector_with(
        "entries:\n  - type: PERSON\n    value: Bob\n", tmp_path,
    )
    matches = await detector.detect("Bob, Bob's office, and Bob's friend")
    assert len(matches) == 3
    assert all(m.text == "Bob" for m in matches)


async def test_word_boundary_disabled_matches_inside_word(tmp_path) -> None:
    """Operators who genuinely want substring matching opt into it
    explicitly with word_boundary: false."""
    yaml_text = """
entries:
  - type: PERSON
    value: Bob
    word_boundary: false
"""
    detector = _detector_with(yaml_text, tmp_path)
    matches = await detector.detect("Bobby and Bob")
    # "Bobby" contains "Bob" → 2 matches total.
    assert len(matches) == 2
    assert all(m.text == "Bob" for m in matches)


# ── Overlapping entries ─────────────────────────────────────────────────────


async def test_longer_entry_wins_over_prefix(tmp_path) -> None:
    """When `Acme` and `Acme Corp` both match the same span, the longer
    span wins (and `Acme` doesn't appear as a duplicate). This mirrors
    the regex detector's `longest` overlap strategy."""
    yaml_text = """
entries:
  - type: ORGANIZATION
    value: Acme
  - type: ORGANIZATION
    value: Acme Corp
"""
    detector = _detector_with(yaml_text, tmp_path)
    matches = await detector.detect("I work at Acme Corp downtown")
    assert len(matches) == 1
    assert matches[0].text == "Acme Corp"


async def test_disjoint_entries_both_match(tmp_path) -> None:
    yaml_text = """
entries:
  - type: PERSON
    value: Alice
  - type: PERSON
    value: Bob
"""
    detector = _detector_with(yaml_text, tmp_path)
    matches = await detector.detect("Alice and Bob met")
    texts = sorted(m.text for m in matches)
    assert texts == ["Alice", "Bob"]


# ── Multi-word terms ────────────────────────────────────────────────────────


async def test_multi_word_term_matches_with_internal_space(tmp_path) -> None:
    """Spaces inside a value are part of the literal — `\\b` still works
    around the outer edges because the value's first/last chars are
    word chars."""
    detector = _detector_with(
        "entries:\n  - type: ORGANIZATION\n    value: Project Aurora\n",
        tmp_path,
    )
    matches = await detector.detect("status update on Project Aurora today")
    assert len(matches) == 1
    assert matches[0].text == "Project Aurora"


# ── Type normalization (via Match.__post_init__) ────────────────────────────


async def test_unknown_type_normalizes_to_other(tmp_path) -> None:
    """Operator-declared types outside ENTITY_TYPES fall back to OTHER —
    keeps surrogate generation predictable for typos."""
    detector = _detector_with(
        "entries:\n  - type: WIDGET\n    value: foo\n", tmp_path,
    )
    matches = await detector.detect("the foo here")
    assert len(matches) == 1
    assert matches[0].entity_type == "OTHER"


# ── Loader validation ───────────────────────────────────────────────────────


def test_missing_path_raises(tmp_path) -> None:
    """A non-empty path that doesn't exist is almost always a typo —
    crash loud rather than silently match nothing."""
    with pytest.raises(RuntimeError, match="could not be read"):
        deny_mod._load_entries(str(tmp_path / "nonexistent.yaml"))


def test_invalid_yaml_raises(tmp_path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("entries: [unclosed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid YAML"):
        deny_mod._load_entries(str(p))


def test_top_level_must_be_mapping(tmp_path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="top-level YAML must be a mapping"):
        deny_mod._load_entries(str(p))


def test_entries_must_be_list(tmp_path) -> None:
    p = tmp_path / "wrong.yaml"
    p.write_text("entries: not_a_list\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="`entries` must be a list"):
        deny_mod._load_entries(str(p))


def test_missing_value_raises(tmp_path) -> None:
    p = tmp_path / "noval.yaml"
    p.write_text("entries:\n  - type: PERSON\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="`value` is required"):
        deny_mod._load_entries(str(p))


def test_missing_type_raises(tmp_path) -> None:
    p = tmp_path / "notype.yaml"
    p.write_text("entries:\n  - value: foo\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="`type` is required"):
        deny_mod._load_entries(str(p))


def test_non_bool_case_sensitive_raises(tmp_path) -> None:
    """YAML's `yes` / `no` would silently coerce — reject anything that's
    not an actual bool so the operator's intent is unambiguous."""
    p = tmp_path / "badbool.yaml"
    p.write_text(
        "entries:\n"
        "  - type: PERSON\n"
        "    value: Alice\n"
        "    case_sensitive: \"no\"\n",   # quoted string, not a bool
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="must be a boolean"):
        deny_mod._load_entries(str(p))


def test_duplicate_entries_dropped_with_warning(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    p = tmp_path / "dup.yaml"
    p.write_text(
        "entries:\n"
        "  - type: PERSON\n"
        "    value: Alice\n"
        "  - type: PERSON\n"
        "    value: Alice\n",
        encoding="utf-8",
    )
    import logging
    with caplog.at_level(logging.WARNING, logger="anonymizer.denylist"):
        entries = deny_mod._load_entries(str(p))
    assert len(entries) == 1
    assert "duplicate entry" in caplog.text.lower()


# ── bundled: prefix ─────────────────────────────────────────────────────────


def test_bundled_prefix_unknown_name_raises() -> None:
    """No denylist files ship with the package, but the bundled-loader
    machinery should still surface a clean error for typos."""
    with pytest.raises(RuntimeError, match="not found in bundled"):
        deny_mod._load_entries("bundled:nonexistent.yaml")


def test_bundled_prefix_rejects_path_separators() -> None:
    with pytest.raises(RuntimeError, match="must be a bare filename"):
        deny_mod._load_entries("bundled:sub/dir/file.yaml")


# ── Pipeline integration ────────────────────────────────────────────────────


async def test_pipeline_uses_denylist_alongside_regex(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a denylist-only term gets anonymized, AND a regex-shaped
    entity in the same text gets anonymized too. Confirms the detector
    composes correctly under DETECTOR_MODE=denylist,regex."""
    p = tmp_path / "deny.yaml"
    p.write_text(
        "entries:\n"
        "  - type: ORGANIZATION\n"
        "    value: AcmeCorpInternal\n",
        encoding="utf-8",
    )

    # Wire DENYLIST_PATH into the module-level config so a freshly-built
    # Pipeline sees it. The Config dataclass is frozen at import; we
    # rebuild it via a SimpleNamespace shim and patch the references.
    import anonymizer_guardrail.config as cfg_mod
    import anonymizer_guardrail.pipeline as pipeline_mod

    new_cfg = SimpleNamespace(
        **{**cfg_mod.config.__dict__, "denylist_path": str(p),
           "detector_mode": "denylist,regex"},
    )
    monkeypatch.setattr(cfg_mod, "config", new_cfg)
    monkeypatch.setattr(pipeline_mod, "config", new_cfg)
    # The denylist module reads from its own `from ..config import config`
    # binding, so we have to patch that too AND reload its module-level
    # cache so the new path is picked up.
    monkeypatch.setattr(deny_mod, "config", new_cfg)
    monkeypatch.setattr(deny_mod, "_LOADED_ENTRIES", deny_mod._load_entries())
    monkeypatch.setattr(
        deny_mod, "_DEFAULT_INDEX",
        deny_mod._build_index(deny_mod._LOADED_ENTRIES),
    )

    pipeline = pipeline_mod.Pipeline()
    text = "Email alice@example.com about AcmeCorpInternal review."
    modified, mapping = await pipeline.anonymize([text], call_id="dl-test")

    # Both kinds of entity must be present in the reverse mapping.
    originals = set(mapping.values())
    assert "AcmeCorpInternal" in originals
    assert "alice@example.com" in originals
    # And the original strings must be gone from the output.
    assert "AcmeCorpInternal" not in modified[0]
    assert "alice@example.com" not in modified[0]
