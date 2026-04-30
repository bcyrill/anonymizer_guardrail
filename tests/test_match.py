"""Tests for the Match dataclass invariants in detector/base.py.

Match.__post_init__ does two normalisations that EVERY detector path
benefits from regardless of whether the detector author thought about it:

  1. Unknown entity_type → OTHER (so downstream code never has to guard).
  2. Trailing sentence punctuation is trimmed (so an entity span that
     accidentally swallowed the period at end-of-sentence doesn't make
     the surrogate substitution eat that period too) — but only for
     types where the punctuation can't be structurally part of the value.
"""

from __future__ import annotations

import os

# Match the other test files: harmless transitive config.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.detector.base import ENTITY_TYPES, Match


# ── Trailing-punctuation trim ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,trimmed,etype",
    [
        # The cases that motivated the change — NER models include
        # sentence-ending punctuation in entity spans.
        ("Springfield.",             "Springfield",             "ADDRESS"),
        ("+1-415-555-0100.",         "+1-415-555-0100",         "PHONE"),
        ("alice.smith@example.com.", "alice.smith@example.com", "EMAIL_ADDRESS"),
        # Other sentence-punctuation variants on natural-text types.
        ("Alice Smith,",             "Alice Smith",             "PERSON"),
        ("Acme Corp!",               "Acme Corp",               "ORGANIZATION"),
        ("Why?",                     "Why",                     "PERSON"),
        ("Item one;",                "Item one",                "PERSON"),
        # Multiple trailing punctuation characters.
        ("Wait...",                  "Wait",                    "PERSON"),
        ("Really?!",                 "Really",                  "PERSON"),
    ],
)
def test_trailing_punctuation_is_trimmed(
    raw: str, trimmed: str, etype: str
) -> None:
    m = Match(text=raw, entity_type=etype)
    assert m.text == trimmed


@pytest.mark.parametrize(
    "raw,etype",
    [
        # Passwords and secrets routinely end with sentence-punctuation
        # that's STRUCTURAL — trimming would corrupt the value.
        ("hunter2!",        "CREDENTIAL"),
        ("P@ssw0rd?",       "CREDENTIAL"),
        ("admin;",          "CREDENTIAL"),
        ("opaque-token...", "TOKEN"),
        # PATH / IDENTIFIER / OTHER are opaque enough to leave alone.
        ("/home/user/.",    "PATH"),
        ("acct-12345.",     "IDENTIFIER"),
        ("anything.",       "OTHER"),
    ],
)
def test_credential_like_types_are_never_trimmed(
    raw: str, etype: str
) -> None:
    """Types in _DO_NOT_TRIM_TYPES preserve trailing punctuation
    verbatim — the punctuation can be part of the value (passwords,
    opaque tokens, paths)."""
    m = Match(text=raw, entity_type=etype)
    assert m.text == raw


@pytest.mark.parametrize(
    "intact,etype",
    [
        # Clean spans — the regex layer's normal output. Trim is a no-op.
        ("Alice Smith",                "PERSON"),
        ("alice@example.com",          "EMAIL_ADDRESS"),
        ("10.20.30.40",                "IPV4_ADDRESS"),
        ("192.168.1.0/24",             "IPV4_CIDR"),
        # Internal punctuation — must NOT be touched.
        ("123 Main St, Springfield",   "ADDRESS"),       # comma is part of address
        ("https://example.com/path",   "URL"),           # dots in URL
        ("1985-04-15",                 "DATE_OF_BIRTH"), # hyphens in date
        ("user@host.example.com",      "EMAIL_ADDRESS"), # dots inside email
    ],
)
def test_clean_or_internal_punctuation_preserved(
    intact: str, etype: str
) -> None:
    assert Match(text=intact, entity_type=etype).text == intact


def test_non_trim_punctuation_stays_put() -> None:
    """The trim set is intentionally narrow (.,;!?). Other punctuation
    that COULD legitimately end an entity span (closing brackets,
    quotes, slashes, hash) stays put even on a trim-eligible type."""
    assert Match(text="(group)", entity_type="PERSON").text == "(group)"
    assert Match(text="path/",   entity_type="URL").text    == "path/"
    assert Match(text='"quote"', entity_type="PERSON").text == '"quote"'
    assert Match(text="conf#1",  entity_type="HOSTNAME").text == "conf#1"


def test_pathological_all_punctuation_becomes_empty() -> None:
    """A span that's entirely sentence-punctuation collapses to empty
    after trim. The pipeline's _dedup drops empty-text Matches, so this
    is harmless in practice — but pin the behaviour so it doesn't
    regress."""
    m = Match(text="...", entity_type="PERSON")
    assert m.text == ""


# ── Entity-type normalisation (existing behaviour) ──────────────────────────


def test_unknown_entity_type_becomes_other() -> None:
    """Detectors emitting a type we haven't declared in ENTITY_TYPES
    (e.g. an LLM hallucinating a category) get mapped to OTHER instead
    of leaking the unknown string into the surrogate selection logic."""
    m = Match(text="something", entity_type="MADE_UP_LABEL")
    assert m.entity_type == "OTHER"


def test_known_entity_type_passes_through() -> None:
    for et in ENTITY_TYPES:
        m = Match(text="something", entity_type=et)
        assert m.entity_type == et


def test_unknown_type_normalisation_runs_before_trim() -> None:
    """Unknown types get normalised to OTHER, which is in
    _DO_NOT_TRIM_TYPES — so an unknown-type Match keeps its trailing
    punctuation. Verifies the order of operations in __post_init__."""
    m = Match(text="anything.", entity_type="MADE_UP")
    assert m.entity_type == "OTHER"
    assert m.text == "anything."  # not trimmed because OTHER doesn't trim