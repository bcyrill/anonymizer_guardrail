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
    base: dict[str, Any] = dict(
        regex_patterns_path="",
        # Default detect()'s overlap strategy. Tests that exercise the
        # opposite strategy override per-test.
        regex_overlap_strategy="longest",
    )
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


@pytest.mark.parametrize(
    "sample,expected_match",
    [
        # Leading + must be captured — the original `\b\+?…` pattern dropped
        # it because `\b` doesn't anchor on `+` (both space and `+` are
        # non-word). The (?<![\w+]) lookbehind fixes that.
        ("Reach me at +1-415-555-0100 today",   "+1-415-555-0100"),
        ("Number +44 20 7946 0958 here",         "+44 20 7946 0958"),
        # No-+ form still works.
        ("Plain phone 555-1234-5678 here",       "555-1234-5678"),
        # International with separators.
        ("Call +49 30 12345678 anytime",          "+49 30 12345678"),
    ],
)
async def test_phone_regex_captures_leading_plus(
    sample: str, expected_match: str
) -> None:
    """Regression test for the leading-+ bug — the previous \\b-anchored
    pattern matched the digits but dropped the leading +. The fix uses a
    negative lookbehind so the match can start AT a + when one is present."""
    detector = regex_mod.RegexDetector()
    matches = await detector.detect(sample)
    phone = next((m for m in matches if m.entity_type == "PHONE"), None)
    assert phone is not None, f"no PHONE match in {sample!r}"
    assert phone.text == expected_match


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

# ── Overlap-strategy tests ─────────────────────────────────────────────────
# The motivating case: regex_pentest.yaml's `\b\d{12}\b` (AWS Account ID)
# matches the trailing 12-digit group of a UUID. With "priority" the
# pentest pattern claims first and shadows the UUID. With "longest" the
# UUID's 36-char span wins.

@pytest.mark.asyncio
async def test_overlap_strategy_longest_picks_uuid_over_aws_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        regex_mod,
        "config",
        _fake_config(
            regex_patterns_path="bundled:regex_pentest.yaml",
            regex_overlap_strategy="longest",
        ),
    )
    monkeypatch.setattr(regex_mod, "_COMPILED_PATTERNS", regex_mod._load_patterns())

    detector = regex_mod.RegexDetector()
    matches = await detector.detect("test 550e8400-e29b-41d4-a716-446655440000.")
    by_text = {m.text: m.entity_type for m in matches}
    # The full UUID's 36-char span wins under longest, and inherits
    # the UUID label from the default YAML (the pentest YAML no longer
    # redeclares the UUID pattern — see the comment in regex_pentest.yaml).
    assert by_text.get("550e8400-e29b-41d4-a716-446655440000") == "UUID"
    # The trailing-12-digit \d{12} pattern's match must NOT have been
    # adopted: its span is contained in the UUID's, and longest-wins
    # consumed it. This is the bug fix.
    assert "446655440000" not in by_text


@pytest.mark.asyncio
async def test_overlap_strategy_priority_keeps_legacy_first_match_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        regex_mod,
        "config",
        _fake_config(
            regex_patterns_path="bundled:regex_pentest.yaml",
            regex_overlap_strategy="priority",
        ),
    )
    monkeypatch.setattr(regex_mod, "_COMPILED_PATTERNS", regex_mod._load_patterns())

    detector = regex_mod.RegexDetector()
    matches = await detector.detect("test 550e8400-e29b-41d4-a716-446655440000.")
    by_text = {m.text: m.entity_type for m in matches}
    # Pre-v0.2 behaviour: the pentest YAML's own \d{12} pattern claims
    # the inner span first, the inherited UUID pattern is then skipped
    # because its match overlaps the claim.
    assert "446655440000" in by_text
    assert by_text["446655440000"] == "IDENTIFIER"
    assert "550e8400-e29b-41d4-a716-446655440000" not in by_text


@pytest.mark.asyncio
async def test_overlap_strategy_longest_does_not_drop_unrelated_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 12-digit AWS account ID NOT inside a UUID must still be caught."""
    monkeypatch.setattr(
        regex_mod,
        "config",
        _fake_config(
            regex_patterns_path="bundled:regex_pentest.yaml",
            regex_overlap_strategy="longest",
        ),
    )
    monkeypatch.setattr(regex_mod, "_COMPILED_PATTERNS", regex_mod._load_patterns())

    detector = regex_mod.RegexDetector()
    matches = await detector.detect("Account 123456789012 was suspended.")
    by_text = {m.text: m.entity_type for m in matches}
    assert by_text.get("123456789012") == "IDENTIFIER"


# ── Per-type structural validators ──────────────────────────────────────────
# Cover the post-filter that rejects regex matches that fail a type-specific
# structural check (Luhn for cards, MOD-97 for IBANs, octet/group rules for
# IPs). The regex layer is intentionally permissive — these tests pin down
# the validator behaviour so over-redaction on the new high-PII types
# doesn't regress.


def _detector_with_default_patterns() -> regex_mod.RegexDetector:
    """Build a RegexDetector wired to the BUNDLED default pattern set,
    independent of whatever some earlier test leaked into the module-level
    `_COMPILED_PATTERNS`. Several earlier tests directly assign that
    attribute without restoring it, so any test that wants the canonical
    bundled patterns has to load them itself."""
    detector = regex_mod.RegexDetector()
    # _load_patterns(None) → reads config.regex_patterns_path. To avoid
    # whatever the current `config` attribute is, pass an explicit
    # bundled: name so we always get the shipped default file.
    detector._compiled = regex_mod._load_patterns("bundled:regex_default.yaml")  # type: ignore[attr-defined]
    return detector


@pytest.mark.parametrize(
    "value,expected",
    [
        ("4111-1111-1111-1111", True),   # Visa test number
        ("5500 0000 0000 0004", True),   # Mastercard test number
        ("3782 822463 10005",   True),   # Amex test number
        # Same shape, single-digit perturbation — Luhn must reject.
        ("4111-1111-1111-1112", False),
        ("5500 0000 0000 0005", False),
    ],
)
def test_luhn_validator(value: str, expected: bool) -> None:
    assert regex_mod._luhn(value) is expected


async def test_credit_card_pattern_drops_luhn_invalid() -> None:
    """A 16-digit string with a Visa prefix but a bad checksum used to
    sail through the regex layer; the validator now drops it. The valid
    test number (4111-…) must still match."""
    detector = _detector_with_default_patterns()
    valid = await detector.detect("Paid with 4111-1111-1111-1111 today.")
    assert any(m.entity_type == "CREDIT_CARD" for m in valid)

    invalid = await detector.detect("Paid with 4111-1111-1111-1112 today.")
    assert not any(m.entity_type == "CREDIT_CARD" for m in invalid)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("DE89370400440532013000", True),    # German test IBAN
        ("GB82WEST12345698765432", True),    # UK test IBAN
        # Wrong check digits — same shape, MOD-97 rejects.
        ("DE00370400440532013000", False),
        ("GB00WEST12345698765432", False),
        # Non-alphanumeric in BBAN — must reject without crashing.
        ("DE89370400440532013!!!", False),
    ],
)
def test_iban_mod97_validator(value: str, expected: bool) -> None:
    assert regex_mod._iban_mod97(value) is expected


async def test_iban_pattern_drops_mod97_invalid() -> None:
    detector = _detector_with_default_patterns()
    valid = await detector.detect("Wire to DE89370400440532013000 by Friday.")
    assert any(m.entity_type == "IBAN" for m in valid)

    invalid = await detector.detect("Wire to DE00370400440532013000 by Friday.")
    assert not any(m.entity_type == "IBAN" for m in invalid)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("192.168.1.1",     True),
        ("0.0.0.0",         True),
        ("255.255.255.255", True),
        ("192.168.001.001", True),    # leading zeros in prose — accept
        ("999.999.999.999", False),   # textbook over-the-top case
        ("256.0.0.1",       False),   # one octet over by one
        ("192.168.1",       False),   # too few groups
        ("1.2.3.4.5",       False),   # too many groups
    ],
)
def test_ipv4_address_validator(value: str, expected: bool) -> None:
    assert regex_mod._ipv4_address(value) is expected


async def test_ipv4_pattern_drops_out_of_range_octets() -> None:
    detector = _detector_with_default_patterns()
    matches = await detector.detect("server 999.999.999.999 is unreachable")
    assert not any(m.entity_type == "IPV4_ADDRESS" for m in matches)

    matches = await detector.detect("server 10.0.0.5 is unreachable")
    assert any(m.entity_type == "IPV4_ADDRESS" for m in matches)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("10.0.0.0/24",     True),
        ("192.168.1.5/24",  True),    # host bits set — accepted (strict=False)
        ("0.0.0.0/0",       True),
        ("10.0.0.0/33",     False),   # prefix > 32
        ("999.0.0.0/24",    False),   # bad octet
        ("10.0.0.0",        False),   # no slash at all
    ],
)
def test_ipv4_network_validator(value: str, expected: bool) -> None:
    assert regex_mod._ipv4_network(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2001:db8::1",                            True),
        ("::1",                                    True),
        ("fe80::1ff:fe23:4567:890a",               True),
        ("2001:0db8:0000:0000:0000:0000:0000:0001", True),
        ("fe80::1ff:fe23:4567:890a%eth0",          True),     # zone ID accepted
        ("::1::2",                                 False),    # two `::` is illegal
        ("gggg::1",                                False),    # non-hex group
        ("12345::",                                False),    # group > 4 hex digits
    ],
)
def test_ipv6_address_validator(value: str, expected: bool) -> None:
    assert regex_mod._ipv6_address(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2001:db8::/32", True),
        ("fe80::/10",     True),
        ("::1/129",       False),   # prefix > 128
        ("::1::2/64",     False),   # two `::`
    ],
)
def test_ipv6_network_validator(value: str, expected: bool) -> None:
    assert regex_mod._ipv6_network(value) is expected


async def test_ipv6_pattern_passes_valid_address() -> None:
    """The pentest YAML's IPV6_ADDRESS regex won't typically generate
    `::1::2`-shaped matches on its own — the unit-level validator test
    (above) covers the rejection path. This higher-level test just
    confirms a structurally valid v6 still flows through the validator
    once it's wired up."""
    detector = regex_mod.RegexDetector()
    detector._compiled = regex_mod._load_patterns(  # type: ignore[attr-defined]
        "bundled:regex_pentest.yaml"
    )
    valid = await detector.detect("connect to 2001:db8::1 please")
    assert any(m.entity_type == "IPV6_ADDRESS" for m in valid)
