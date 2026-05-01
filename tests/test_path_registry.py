"""Tests for the named-path registry mechanism.

REGEX_PATTERNS_REGISTRY and LLM_SYSTEM_PROMPT_REGISTRY let operators
declare named alternatives that callers reference by name only — no
arbitrary paths over the wire.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

# Match the other tests: keep config harmless for transitive imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import logging

import pytest

from anonymizer_guardrail.registry import parse_named_path_registry
from anonymizer_guardrail.detector import regex as regex_mod
from anonymizer_guardrail.detector import llm as llm_mod


# ── Registry parser ────────────────────────────────────────────────────────
def test_registry_empty_returns_empty_dict() -> None:
    assert parse_named_path_registry("", "X") == {}
    assert parse_named_path_registry("   ", "X") == {}


def test_registry_basic_pairs() -> None:
    out = parse_named_path_registry(
        "pentest=bundled:regex_pentest.yaml,internal=/etc/foo.yaml", "X"
    )
    assert out == {
        "pentest": "bundled:regex_pentest.yaml",
        "internal": "/etc/foo.yaml",
    }


def test_registry_strips_surrounding_whitespace() -> None:
    out = parse_named_path_registry(
        " a = b , c=d  ,  e =f ", "X"
    )
    assert out == {"a": "b", "c": "d", "e": "f"}


def test_registry_rejects_default_name() -> None:
    with pytest.raises(RuntimeError, match="reserved"):
        parse_named_path_registry("default=bundled:regex_default.yaml", "X")


def test_registry_rejects_malformed_entry_no_equals() -> None:
    with pytest.raises(RuntimeError, match="malformed"):
        parse_named_path_registry("only-a-name", "X")


def test_registry_rejects_empty_name() -> None:
    with pytest.raises(RuntimeError, match="malformed"):
        parse_named_path_registry("=foo", "X")


def test_registry_rejects_empty_path() -> None:
    with pytest.raises(RuntimeError, match="malformed"):
        parse_named_path_registry("name=", "X")


def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(RuntimeError, match="duplicate"):
        parse_named_path_registry("a=1,a=2", "X")


def test_registry_error_message_names_the_var() -> None:
    """A typo in 'pentest=' should surface naming the env var so an
    operator can find it without grep — matters when several
    registries coexist."""
    with pytest.raises(RuntimeError, match="MY_VAR"):
        parse_named_path_registry("bad", "MY_VAR")


# ── End-to-end: regex registry ─────────────────────────────────────────────
def test_regex_registry_loaded_at_module_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When REGEX_PATTERNS_REGISTRY is set, _load_patterns_registry()
    compiles each entry and the result is keyed by name."""
    monkeypatch.setattr(
        regex_mod, "CONFIG",
        regex_mod.CONFIG.model_copy(update={
            "patterns_path": "",
            "patterns_registry": "pentest=bundled:regex_pentest.yaml",
        }),
    )
    out = regex_mod._load_patterns_registry()
    assert "pentest" in out
    # Compiled list is non-empty (regex_pentest.yaml has many patterns).
    assert len(out["pentest"]) > 1


@pytest.mark.asyncio
async def test_regex_detect_named_patterns_used_when_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `patterns_name` known to the registry routes detection through
    that compiled set, not the default."""
    detector = regex_mod.RegexDetector()
    # Inject a small "alt" registry entry distinguishable from default.
    import re
    alt_compiled = [("CREDENTIAL", re.compile(r"\bSEKRET-\w+\b"))]
    monkeypatch.setattr(detector, "_registry", {"alt": alt_compiled})

    # Default detector doesn't know SEKRET-XYZ.
    matches = await detector.detect("contact SEKRET-XYZ tomorrow")
    assert all(m.text != "SEKRET-XYZ" for m in matches)

    # patterns_name="alt" routes to alt_compiled which catches it.
    matches = await detector.detect(
        "contact SEKRET-XYZ tomorrow", patterns_name="alt"
    )
    assert any(
        m.text == "SEKRET-XYZ" and m.entity_type == "CREDENTIAL"
        for m in matches
    )


@pytest.mark.asyncio
async def test_regex_detect_unknown_name_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown patterns_name → warn-and-use-default, never block."""
    detector = regex_mod.RegexDetector()
    monkeypatch.setattr(detector, "_registry", {})  # empty registry
    caplog.set_level(logging.WARNING)
    matches = await detector.detect(
        "alice@example.com", patterns_name="nope"
    )
    # Default still catches the email.
    assert any(m.entity_type == "EMAIL_ADDRESS" for m in matches)
    assert any("nope" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_regex_detect_default_name_is_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """patterns_name='default' resolves to the configured default,
    not a registry entry — even if a registry entry called 'default'
    somehow existed (parse_named_path_registry blocks that, but the
    detector also short-circuits)."""
    detector = regex_mod.RegexDetector()
    matches = await detector.detect(
        "alice@example.com", patterns_name="default"
    )
    assert any(m.entity_type == "EMAIL_ADDRESS" for m in matches)


# ── End-to-end: llm prompt registry ────────────────────────────────────────
def test_llm_prompt_registry_loaded_at_module_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_mod, "CONFIG",
        llm_mod.CONFIG.model_copy(update={
            "system_prompt_path": "",
            "system_prompt_registry": "pentest=bundled:llm_pentest.md",
        }),
    )
    out = llm_mod._load_system_prompt_registry()
    assert "pentest" in out
    assert out["pentest"]  # non-empty content
