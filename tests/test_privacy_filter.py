"""Tests for the privacy-filter span post-processing.

Targets `remote_privacy_filter._to_matches` directly — the function
runs client-side inside `RemotePrivacyFilterDetector._parse_matches`,
turning opf's raw DetectedSpan-shaped wire payload into canonical
Match objects. HTTP-client tests for the detector itself live in
`test_remote_privacy_filter.py`. This file covers the pure
post-processing: extract / merge / split / emit, label mapping,
hallucination guard, per-label gap caps, paragraph-break rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Match the other test files: harmless transitive config.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.detector.remote_privacy_filter import _to_matches


@dataclass(frozen=True)
class _Span:
    """opf.DetectedSpan-shaped fixture object — same `.label / .start /
    .end / .text` attribute surface the production `_to_matches` reads
    via `getattr`. Lightweight stand-in so tests don't pull opf in."""
    label: str
    start: int
    end: int
    text: str = ""


def _matches_by_type(matches: list) -> dict[str, str]:
    """Convenience: {entity_type: text} for assertion brevity. Drops
    any entries that share an entity_type — tests where that matters
    inspect the raw list themselves."""
    return {m.entity_type: m.text for m in matches}


# ── Label mapping + offset extraction ──────────────────────────────────────

def test_label_mapping_and_offsets() -> None:
    """Each known label maps to its target ENTITY_TYPES entry, and the
    matched text is sliced from the input via the start/end offsets."""
    text = "My name is Alice and email alice@example.com plus key abcdef123"
    spans = [
        _Span(label="private_person", start=11, end=16, text="Alice"),
        _Span(label="private_email", start=27, end=44, text="alice@example.com"),
        _Span(label="secret", start=54, end=63, text="abcdef123"),
    ]
    matches = _to_matches(spans, text)
    by_type = _matches_by_type(matches)
    assert by_type["PERSON"] == "Alice"
    assert by_type["EMAIL_ADDRESS"] == "alice@example.com"
    assert by_type["CREDENTIAL"] == "abcdef123"


def test_unknown_label_normalizes_to_other() -> None:
    """A label not in our LABEL_MAP falls through to OTHER."""
    text = "stuff here"
    matches = _to_matches([_Span(label="made_up_label", start=0, end=5, text="stuff")], text)
    assert matches[0].entity_type == "OTHER"


def test_offsets_preferred_over_text_field() -> None:
    """When start/end are valid, the substring is sliced from the
    source text — not taken from .text (which can carry tokenizer
    artifacts under some decoders)."""
    text = "hello Alice-Marie Smith there"
    matches = _to_matches(
        [_Span(label="private_person", start=6, end=23, text="Alice ##- ##Marie Smith")],
        text,
    )
    assert matches[0].text == "Alice-Marie Smith"


def test_hallucination_guard_drops_entities_not_in_input() -> None:
    """If a span's offsets are missing AND the .text field doesn't
    appear in the source, drop it. Mirrors the LLMDetector guard."""
    text = "hello world"
    spans = [_Span(label="private_person", start=-1, end=-1, text="ghost-name")]
    assert _to_matches(spans, text) == []


# ── Merge pass: same-type spans across small gaps ─────────────────────────

def test_split_person_name_is_merged() -> None:
    """`["Alice", "Smith"]` → one PERSON `"Alice Smith"` after merge."""
    text = "Hi, my name is Alice Smith there"
    matches = _to_matches([
        _Span(label="private_person", start=15, end=20, text="Alice"),
        _Span(label="private_person", start=21, end=26, text="Smith"),
    ], text)
    assert len(matches) == 1
    assert matches[0].text == "Alice Smith"
    assert matches[0].entity_type == "PERSON"


def test_slash_separated_persons_stay_distinct() -> None:
    """Slash means list, not multi-word entity."""
    text = "owners: Alice / Bob agree"
    matches = _to_matches([
        _Span(label="private_person", start=8, end=13, text="Alice"),
        _Span(label="private_person", start=16, end=19, text="Bob"),
    ], text)
    assert len(matches) == 2
    assert {m.text for m in matches} == {"Alice", "Bob"}


def test_and_separated_persons_stay_distinct() -> None:
    """A 5-char gap with non-connector content (`" and "`) blocks the
    merge: the alphabetic chars aren't in the connector set."""
    text = "Alice and Bob arrived"
    matches = _to_matches([
        _Span(label="private_person", start=0, end=5, text="Alice"),
        _Span(label="private_person", start=10, end=13, text="Bob"),
    ], text)
    assert len(matches) == 2


def test_address_merges_across_comma() -> None:
    """ADDRESS uniquely allows commas in the merge gap because they're
    structurally part of the address ("123 Main St, Springfield")."""
    text = "I live at 123 Main St, Springfield."
    matches = _to_matches([
        _Span(label="private_address", start=10, end=21, text="123 Main St"),
        _Span(label="private_address", start=23, end=34, text="Springfield"),
    ], text)
    assert len(matches) == 1
    assert matches[0].text == "123 Main St, Springfield"
    assert matches[0].entity_type == "ADDRESS"


def test_persons_with_comma_stay_distinct() -> None:
    """Comma in a PERSON gap is a list separator, not intra-name glue."""
    text = "Authors: Alice, Bob"
    matches = _to_matches([
        _Span(label="private_person", start=9, end=14, text="Alice"),
        _Span(label="private_person", start=16, end=19, text="Bob"),
    ], text)
    assert len(matches) == 2


def test_emails_never_merge_across_whitespace() -> None:
    """EMAIL_ADDRESS has max_gap=0 — emails don't legitimately fragment
    with whitespace between halves."""
    text = "Contacts: alice@example.com bob@example.com cc'd."
    matches = _to_matches([
        _Span(label="private_email", start=10, end=27, text="alice@example.com"),
        _Span(label="private_email", start=28, end=43, text="bob@example.com"),
    ], text)
    assert len(matches) == 2
    assert {m.text for m in matches} == {"alice@example.com", "bob@example.com"}


def test_long_whitespace_gap_blocks_person_merge() -> None:
    """PERSON's max_gap is 3 chars. A 4-space gap exceeds the cap and
    blocks the merge even though every char is a valid connector."""
    text = "Hi Alice    Smith there"
    matches = _to_matches([
        _Span(label="private_person", start=3, end=8, text="Alice"),
        _Span(label="private_person", start=12, end=17, text="Smith"),
    ], text)
    assert len(matches) == 2


# ── Paragraph-break rules (merge-side + split-side) ────────────────────────

def test_paragraph_break_blocks_merge() -> None:
    """A pure `\\n\\n` between two same-type spans is a paragraph
    break, which blocks merging even when the model labels them
    identically. Gap text MUST be exactly the paragraph break with
    no other characters — otherwise the all-whitespace requirement
    would block the merge for the wrong reason and this test would
    pass trivially."""
    text = "Date: 2026-04-17\n\nResolution: contact support."
    # text[16:18] is exactly "\n\n".
    matches = _to_matches([
        _Span(label="private_date", start=6, end=16, text="2026-04-17"),
        _Span(label="private_date", start=18, end=29, text="Resolution:"),
    ], text)
    assert len(matches) == 2
    assert {m.text for m in matches} == {"2026-04-17", "Resolution:"}


def test_paragraph_break_splits_one_span() -> None:
    """When a single span already crosses a paragraph break (decoder
    aggregation collapsed across `\\n\\n`), the split pass breaks it
    apart. Mirror of the merge-side carve-out."""
    text = "Date: 2026-04-17\n\nResolution: contact support."
    matches = _to_matches(
        [_Span(label="private_date", start=6, end=29, text="2026-04-17 Resolution:")],
        text,
    )
    assert len(matches) == 2
    assert {m.text for m in matches} == {"2026-04-17", "Resolution:"}


def test_single_newline_still_merges() -> None:
    """A single newline is in-paragraph wrapping, not a paragraph
    boundary. The merge proceeds."""
    text = "Hi, my name is Alice\nSmith there"
    matches = _to_matches([
        _Span(label="private_person", start=15, end=20, text="Alice"),
        _Span(label="private_person", start=21, end=26, text="Smith"),
    ], text)
    assert len(matches) == 1
    assert matches[0].text == "Alice\nSmith"
    assert matches[0].entity_type == "PERSON"


def test_different_types_never_merge() -> None:
    """Adjacent same-whitespace spans of different types must NOT merge."""
    text = "Alice alice@example.com"
    matches = _to_matches([
        _Span(label="private_person", start=0, end=5, text="Alice"),
        _Span(label="private_email", start=6, end=23, text="alice@example.com"),
    ], text)
    assert len(matches) == 2


# ── SPEC registration ──────────────────────────────────────────────────────

def test_detector_registered_in_pipeline() -> None:
    """`privacy_filter` is a known detector name — DETECTOR_MODE can
    list it without falling through to the unknown-name warning path."""
    from anonymizer_guardrail.detector import SPECS_BY_NAME

    assert "privacy_filter" in SPECS_BY_NAME


def test_factory_errors_when_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The privacy-filter detector is HTTP-only. Instantiating without
    PRIVACY_FILTER_URL set must raise a clear remediation message —
    not a confusing transport error on first request."""
    from anonymizer_guardrail.detector import remote_privacy_filter as pf

    # Force CONFIG.url to empty regardless of env state in this process.
    monkeypatch.setattr(pf, "CONFIG", pf.CONFIG.model_copy(update={"url": ""}))
    with pytest.raises(RuntimeError, match=r"PRIVACY_FILTER_URL"):
        pf._privacy_filter_factory()
