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


def _detector_with(
    entries_yaml: str, tmp_path, *, backend: str | None = None,
) -> deny_mod.DenylistDetector:
    """Build a detector wired to a tmp YAML file. Avoids touching the
    module-level _DEFAULT_INDEX so tests don't leak into each other.
    `backend` defaults to whatever the import-time config picked
    (regex); the cross-backend equivalence tests below override it."""
    p = tmp_path / "deny.yaml"
    p.write_text(entries_yaml, encoding="utf-8")
    entries = deny_mod._load_entries(str(p), "DENYLIST_PATH")
    detector = deny_mod.DenylistDetector()
    if backend is None:
        detector._default_index = deny_mod._build_index(entries)  # type: ignore[attr-defined]
    elif backend == "regex":
        detector._default_index = deny_mod._build_index_regex(entries)  # type: ignore[attr-defined]
    elif backend == "aho":
        detector._default_index = deny_mod._build_index_aho(entries)  # type: ignore[attr-defined]
    else:
        raise ValueError(f"unknown backend in test helper: {backend!r}")
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

    # Wire DENYLIST_PATH into the denylist detector's CONFIG so a
    # freshly-built Pipeline sees it. Detector_mode lives on the central
    # Config; denylist's own fields live on deny_mod.CONFIG.
    from dataclasses import replace
    import anonymizer_guardrail.config as cfg_mod
    import anonymizer_guardrail.pipeline as pipeline_mod

    new_central_cfg = SimpleNamespace(
        **{**cfg_mod.config.__dict__, "detector_mode": "denylist,regex"},
    )
    monkeypatch.setattr(cfg_mod, "config", new_central_cfg)
    monkeypatch.setattr(pipeline_mod, "config", new_central_cfg)
    # Patch the per-detector CONFIG and reload its module-level cache so
    # the new path is picked up at the next Pipeline construction.
    monkeypatch.setattr(deny_mod, "CONFIG", replace(deny_mod.CONFIG, path=str(p)))
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


# ── Cross-backend equivalence ───────────────────────────────────────────────
# The Aho-Corasick backend is opt-in (DENYLIST_BACKEND=aho). Wherever its
# behaviour MUST match the regex backend, parametrize over both so the
# two paths can't drift silently. The matrix below is the canonical set
# of behaviours each backend has to honour: basic match, case sensitivity
# (default + opt-out), word boundaries (default + opt-out), multi-word
# entries, longest-wins overlap, and clean handling of an empty input.

_BACKENDS = ["regex", "aho"]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_xb_basic_match(backend: str, tmp_path) -> None:
    detector = _detector_with(
        "entries:\n  - type: ORGANIZATION\n    value: Acme\n",
        tmp_path, backend=backend,
    )
    matches = await detector.detect("I work at Acme today")
    assert [(m.text, m.entity_type) for m in matches] == [("Acme", "ORGANIZATION")]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_xb_case_sensitive_default(backend: str, tmp_path) -> None:
    detector = _detector_with(
        "entries:\n  - type: ORGANIZATION\n    value: Acme\n",
        tmp_path, backend=backend,
    )
    assert await detector.detect("acme corp") == []
    assert len(await detector.detect("Acme")) == 1


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_xb_case_insensitive(backend: str, tmp_path) -> None:
    yaml_text = (
        "entries:\n"
        "  - type: ORGANIZATION\n"
        "    value: Acme\n"
        "    case_sensitive: false\n"
    )
    detector = _detector_with(yaml_text, tmp_path, backend=backend)
    matches = await detector.detect("acme + ACME + Acme")
    # Source casing is preserved on each match — matters for the surrogate
    # replacer, which has to find the exact substring back in the text.
    assert sorted(m.text for m in matches) == ["ACME", "Acme", "acme"]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_xb_word_boundary_default(backend: str, tmp_path) -> None:
    detector = _detector_with(
        "entries:\n  - type: PERSON\n    value: Bob\n",
        tmp_path, backend=backend,
    )
    # Default is word_boundary: true → "Bob" doesn't match inside "Bobby".
    assert await detector.detect("Bobby works for Robert") == []
    # But matches at word edges fine.
    matches = await detector.detect("Bob and Bob's office")
    assert all(m.text == "Bob" for m in matches)
    assert len(matches) == 2


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_xb_word_boundary_disabled(backend: str, tmp_path) -> None:
    yaml_text = (
        "entries:\n"
        "  - type: PERSON\n"
        "    value: Bob\n"
        "    word_boundary: false\n"
    )
    detector = _detector_with(yaml_text, tmp_path, backend=backend)
    # With word boundaries off, "Bobby" yields one substring match plus
    # the standalone "Bob".
    matches = await detector.detect("Bobby and Bob")
    assert len(matches) == 2


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_xb_longest_wins_overlap(backend: str, tmp_path) -> None:
    yaml_text = (
        "entries:\n"
        "  - type: ORGANIZATION\n"
        "    value: Acme\n"
        "  - type: ORGANIZATION\n"
        "    value: Acme Corp\n"
    )
    detector = _detector_with(yaml_text, tmp_path, backend=backend)
    matches = await detector.detect("I work at Acme Corp downtown")
    assert [m.text for m in matches] == ["Acme Corp"]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_xb_multi_word_term(backend: str, tmp_path) -> None:
    detector = _detector_with(
        "entries:\n  - type: ORGANIZATION\n    value: Project Aurora\n",
        tmp_path, backend=backend,
    )
    matches = await detector.detect("update on Project Aurora today")
    assert [m.text for m in matches] == ["Project Aurora"]


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_xb_empty_input(backend: str, tmp_path) -> None:
    detector = _detector_with(
        "entries:\n  - type: PERSON\n    value: Bob\n",
        tmp_path, backend=backend,
    )
    assert await detector.detect("") == []


# ── Aho-specific edge cases ─────────────────────────────────────────────────
# Word boundaries are baked into the regex via `\b` but post-filtered for
# Aho-Corasick. These tests cover the post-filter directly to make sure
# the behaviour matches `\b` on values whose edges are non-word chars
# (where `_wrap_with_boundaries` skips the boundary on the regex side too).


def test_aho_word_boundary_helper_skips_non_word_edges() -> None:
    """A value like ":foo:" has non-word edges, so neither side should
    enforce a word boundary. Mirrors the regex side's edge handling."""
    text = "abc :foo: xyz"
    start = text.index(":foo:")
    end = start + len(":foo:")
    assert deny_mod._has_word_boundaries(text, start, end) is True


def test_aho_word_boundary_helper_blocks_inside_word() -> None:
    """`Bob` inside `Bobby` — left edge fine (start of word), right edge
    fails because `B` is followed by `b` (both word chars)."""
    text = "Bobby"
    assert deny_mod._has_word_boundaries(text, 0, 3) is False


def test_aho_word_boundary_helper_allows_at_string_boundaries() -> None:
    text = "Bob"
    assert deny_mod._has_word_boundaries(text, 0, 3) is True


# ── Backend dispatch / failure modes ────────────────────────────────────────


def test_invalid_backend_value_crashes_at_import(monkeypatch) -> None:
    """An unknown DENYLIST_BACKEND value must surface immediately at boot
    so a typo doesn't ship with a silent regex fallback. After the
    config-by-detector refactor, DenylistConfig reads DENYLIST_BACKEND
    directly from os.environ at instantiation, so we set the env var
    and re-import the module (forcing a fresh DenylistConfig + the
    module-top backend validation)."""
    import importlib
    import sys
    import anonymizer_guardrail.detector

    monkeypatch.setenv("DENYLIST_BACKEND", "bogus")
    sys.modules.pop("anonymizer_guardrail.detector.denylist", None)
    # detector/__init__.py imports the SPEC from denylist; it's also
    # cached in sys.modules and would shadow our re-import. Clear it
    # (and restore the parent attribute after).
    original_pkg_attr = anonymizer_guardrail.detector.denylist
    try:
        with pytest.raises(RuntimeError, match="Invalid DENYLIST_BACKEND"):
            importlib.import_module("anonymizer_guardrail.detector.denylist")
    finally:
        # Re-import the real module so subsequent tests see the
        # production-config singleton (and the module-level _DEFAULT_INDEX
        # is rebuilt against the real config).
        sys.modules.pop("anonymizer_guardrail.detector.denylist", None)
        monkeypatch.undo()
        importlib.import_module("anonymizer_guardrail.detector.denylist")
        # Restore the parent-package attribute (see test_module_import in
        # test_privacy_filter.py for the same restoration rationale).
        anonymizer_guardrail.detector.denylist = original_pkg_attr


def test_aho_backend_without_pyahocorasick_fails_with_clear_message(
    monkeypatch,
) -> None:
    """Operator opted into DENYLIST_BACKEND=aho but didn't install the
    extra → the build call must raise a RuntimeError that names the
    pip extra. Simulated by hiding `ahocorasick` at import time."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ahocorasick":
            raise ImportError("simulated: ahocorasick is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="pyahocorasick"):
        deny_mod._build_index_aho(
            [{"type": "PERSON", "value": "Alice",
              "case_sensitive": True, "word_boundary": True}]
        )


def test_aho_empty_entries_returns_no_op_index() -> None:
    """Building an aho index from zero entries must not construct a
    half-initialised automaton — it returns the no-op constant just like
    the regex backend."""
    fn = deny_mod._build_index_aho([])
    assert fn is deny_mod._EMPTY_CANDIDATES
    assert fn("any text") == []
