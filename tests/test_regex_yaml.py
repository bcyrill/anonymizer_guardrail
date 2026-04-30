"""Tests for the YAML-driven regex pattern loader."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

# Match the other tests: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.detector import regex as regex_mod


def _fake_config(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(regex_patterns_path="")
    base.update(overrides)
    return SimpleNamespace(**base)


def test_default_yaml_loads_at_import_time() -> None:
    """The bundled default must produce a non-empty compiled list — proves
    hatchling is shipping the patterns/ directory in the wheel."""
    assert len(regex_mod._COMPILED_PATTERNS) > 0
    types = {etype for etype, _ in regex_mod._COMPILED_PATTERNS}
    # A few canonical types we know are in the bundled defaults.
    assert "EMAIL_ADDRESS" in types
    assert "AWS_ACCESS_KEY" in types
    assert "JWT" in types
    # Newly-added high-PII types must also be present.
    assert "CREDIT_CARD" in types
    assert "IBAN" in types
    assert "DATE_OF_BIRTH" in types


@pytest.mark.parametrize(
    "sample,expected_type",
    [
        ("Visa: 4111-1111-1111-1111",        "CREDIT_CARD"),
        ("Mastercard 5500 0000 0000 0004",   "CREDIT_CARD"),
        ("Amex 3782 822463 10005",           "CREDIT_CARD"),
        ("DE89370400440532013000",           "IBAN"),
        ("GB82WEST12345698765432",           "IBAN"),
        ("DOB: 1985-04-15",                  "DATE_OF_BIRTH"),
        ("Date of Birth: 12/25/1990",        "DATE_OF_BIRTH"),
    ],
)
async def test_new_default_patterns_match(sample: str, expected_type: str) -> None:
    """Each new bundled-default pattern matches a representative input
    with the right entity type. Locks in the regex shape against future
    drift."""
    detector = regex_mod.RegexDetector()
    matches = await detector.detect(sample)
    assert matches, f"no match in: {sample!r}"
    assert any(m.entity_type == expected_type for m in matches), (
        f"expected {expected_type} in {[(m.entity_type, m.text) for m in matches]}"
    )


async def test_pentest_yaml_relabels_brazilian_docs_as_national_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pentest YAML had Brazilian CPF/CNPJ/RG labelled CREDENTIAL
    (verbatim from DontFeedTheAI). They were relabelled NATIONAL_ID so
    the surrogate generator picks Faker's locale-aware ssn() for them.
    Pin that down."""
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path="bundled:regex_pentest.yaml")
    )
    regex_mod._COMPILED_PATTERNS = regex_mod._load_patterns()
    detector = regex_mod.RegexDetector()
    try:
        matches = await detector.detect("CPF 123.456.789-01 here")
        assert any(m.entity_type == "NATIONAL_ID" for m in matches), (
            f"expected NATIONAL_ID, got {[(m.entity_type, m.text) for m in matches]}"
        )
    finally:
        # Restore the default-bundled patterns so other tests stay isolated.
        monkeypatch.setattr(
            regex_mod, "config", _fake_config(regex_patterns_path="")
        )
        regex_mod._COMPILED_PATTERNS = regex_mod._load_patterns()


def test_override_path_replaces_patterns(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        "patterns:\n"
        "  - type: TOKEN\n"
        "    pattern: 'XYZ\\d+'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(custom))
    )

    compiled = regex_mod._load_patterns()
    assert len(compiled) == 1
    assert compiled[0][0] == "TOKEN"
    assert compiled[0][1].pattern == r"XYZ\d+"


def test_override_with_bundled_extends_merges(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extends: regex_default.yaml pulls in bundled defaults before this
    file's own patterns. The combined order is defaults-first."""
    custom = tmp_path / "extended.yaml"
    custom.write_text(
        "extends: regex_default.yaml\n"
        "patterns:\n"
        "  - type: TOKEN\n"
        "    pattern: 'CUSTOM_TOKEN_\\w+'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(custom))
    )

    compiled = regex_mod._load_patterns()
    # > defaults plus our one extra.
    assert len(compiled) > 1
    # Local patterns load FIRST so they take precedence over inherited
    # patterns on overlapping spans (child-overrides-parent semantics).
    assert compiled[0][1].pattern == r"CUSTOM_TOKEN_\w+"
    # And the inherited defaults are still present somewhere down the list.
    assert any(et == "EMAIL_ADDRESS" for et, _ in compiled)


def test_pentest_yaml_extends_default_yaml() -> None:
    """The shipped pentest YAML should declare extends and load cleanly —
    if someone breaks that file it shouldn't pass review."""
    from importlib import resources

    text = (
        resources.files("anonymizer_guardrail")
        .joinpath("patterns/regex_pentest.yaml")
        .read_text(encoding="utf-8")
    )
    assert "extends: regex_default.yaml" in text


def test_missing_override_path_raises() -> None:
    """Typo in REGEX_PATTERNS_PATH crashes loud, not silent fall-back."""
    cfg = _fake_config(regex_patterns_path="/no/such/file.yaml")
    # Patch via the module attribute the loader reads.
    orig = regex_mod.config
    regex_mod.config = cfg  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="could not be read"):
            regex_mod._load_patterns()
    finally:
        regex_mod.config = orig  # type: ignore[assignment]


def test_invalid_regex_fails_at_load(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-supplied bad regex must surface immediately, not at first
    request — otherwise the whole detector silently degrades."""
    custom = tmp_path / "bad.yaml"
    custom.write_text(
        "patterns:\n"
        "  - type: TOKEN\n"
        "    pattern: '['\n",   # unclosed character class
        encoding="utf-8",
    )
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(custom))
    )
    with pytest.raises(RuntimeError, match="did not compile"):
        regex_mod._load_patterns()


def test_unknown_flag_name_fails_at_load(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "badflag.yaml"
    custom.write_text(
        "patterns:\n"
        "  - type: TOKEN\n"
        "    pattern: 'foo'\n"
        "    flags: [WHATEVER]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(custom))
    )
    with pytest.raises(RuntimeError, match="unknown regex flag"):
        regex_mod._load_patterns()


def test_known_flags_apply(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "flags.yaml"
    custom.write_text(
        "patterns:\n"
        "  - type: TOKEN\n"
        "    pattern: 'hello'\n"
        "    flags: [IGNORECASE]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(custom))
    )
    compiled = regex_mod._load_patterns()
    import re as _re
    assert compiled[0][1].flags & _re.IGNORECASE


def test_extends_cycle_detected(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(f"extends: {b}\npatterns: []\n", encoding="utf-8")
    b.write_text(f"extends: {a}\npatterns: []\n", encoding="utf-8")
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(a))
    )
    with pytest.raises(RuntimeError, match="cycle|exceeded depth"):
        regex_mod._load_patterns()


async def test_capture_group_narrows_match_to_value(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A labeled-value pattern with a capturing group should anonymize only
    the value, not the whole label-and-value span. This is the contract
    that DontFeedTheAI's patterns rely on."""
    custom = tmp_path / "label.yaml"
    custom.write_text(
        "patterns:\n"
        "  - type: CREDENTIAL\n"
        "    pattern: 'password:\\s+(\\S+)'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(custom))
    )
    # Force the module-level _COMPILED_PATTERNS to refresh against the new config.
    regex_mod._COMPILED_PATTERNS = regex_mod._load_patterns()
    detector = regex_mod.RegexDetector()

    matches = await detector.detect("login: alice password: hunter2!")
    assert len(matches) == 1
    # Only the password value, not "password: hunter2!".
    assert matches[0].text == "hunter2!"
    assert matches[0].entity_type == "CREDENTIAL"


async def test_no_capture_group_preserves_full_match(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patterns without capturing groups still get the whole match, same as
    before — capture-group support is opt-in per-pattern."""
    custom = tmp_path / "nogroup.yaml"
    custom.write_text(
        "patterns:\n"
        "  - type: TOKEN\n"
        "    pattern: 'XYZ\\d+'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(custom))
    )
    regex_mod._COMPILED_PATTERNS = regex_mod._load_patterns()
    detector = regex_mod.RegexDetector()

    matches = await detector.detect("token is XYZ12345 today")
    assert len(matches) == 1
    assert matches[0].text == "XYZ12345"


def test_pentest_yaml_loads_with_full_pattern_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full pentest YAML must load without compile errors. Catches drift
    if someone edits a pattern and accidentally introduces invalid regex,
    a typo'd flag name, or an entity type we don't know about."""
    from importlib import resources

    path = (
        resources.files("anonymizer_guardrail")
        .joinpath("patterns/regex_pentest.yaml")
    )
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path=str(path))
    )
    compiled = regex_mod._load_patterns()
    # 173 patterns from DontFeedTheAI + 14 from the bundled defaults via extends.
    # Loose lower bound so a small future cleanup doesn't fail the test.
    assert len(compiled) >= 180


def test_bundled_prefix_resolves_to_packaged_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bundled:regex_pentest.yaml` should resolve via importlib.resources,
    so the env var works the same regardless of Python install layout."""
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path="bundled:regex_pentest.yaml")
    )
    compiled = regex_mod._load_patterns()
    assert len(compiled) >= 180


def test_bundled_prefix_unknown_name_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path="bundled:no_such.yaml")
    )
    with pytest.raises(RuntimeError, match="not found in bundled patterns"):
        regex_mod._load_patterns()


def test_bundled_prefix_rejects_path_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        regex_mod, "config", _fake_config(regex_patterns_path="bundled:../etc/passwd")
    )
    with pytest.raises(RuntimeError, match="bare filename"):
        regex_mod._load_patterns()