"""Tests for the PrivacyFilterDetector.

These tests must run on machines without `transformers` installed — the
heavy dependency is optional. We mock `_load_pipeline` to return a
controlled callable, exercising:

  - label-to-ENTITY_TYPES mapping
  - offset-based text extraction (vs the pipeline's `word` field, which
    can include subword artifacts)
  - hallucination guard (entity must appear in the input)
  - the import-error path that fires when the optional extra isn't
    installed
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock

# Match the other test files: harmless transitive config.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest


@pytest.fixture
def mock_load(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the lazy pipeline loader with a callable that returns a
    MagicMock standing in for the HF token-classification pipeline. Tests
    set `mock_load.return_value` to whatever span list they want the
    "model" to produce."""
    from anonymizer_guardrail.detector import privacy_filter as pf

    fake_pipeline = MagicMock()
    monkeypatch.setattr(pf, "_load_pipeline", lambda _name: fake_pipeline)
    return fake_pipeline


async def test_label_mapping_and_offsets(mock_load: MagicMock) -> None:
    """Each known label maps to its target ENTITY_TYPES entry, and the
    matched text is sliced from the input via the start/end offsets."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "My name is Alice and email alice@example.com plus key abcdef123"
    mock_load.return_value = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice",
         "start": 11, "end": 16},
        {"entity_group": "private_email", "score": 0.98, "word": "alice@example.com",
         "start": 27, "end": 44},
        {"entity_group": "secret", "score": 0.92, "word": "abcdef123",
         "start": 54, "end": 63},
    ]

    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)

    by_type = {m.entity_type: m.text for m in matches}
    assert by_type["PERSON"] == "Alice"
    assert by_type["EMAIL_ADDRESS"] == "alice@example.com"
    assert by_type["CREDENTIAL"] == "abcdef123"


async def test_unknown_label_normalizes_to_other(mock_load: MagicMock) -> None:
    """A label not in our LABEL_MAP falls through to OTHER via the
    Match.__post_init__ normalization."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    mock_load.return_value = [
        {"entity_group": "made_up_label", "score": 0.99, "word": "stuff",
         "start": 0, "end": 5},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect("stuff here")
    assert matches[0].entity_type == "OTHER"


async def test_offsets_preferred_over_word_field(mock_load: MagicMock) -> None:
    """The pipeline's `word` field can contain ## subword markers and
    stripped whitespace; offsets give us the exact source span. Verify
    we don't blindly trust `word`."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "hello Alice-Marie Smith there"
    mock_load.return_value = [
        # `word` has tokenizer noise; `start`/`end` are correct.
        {"entity_group": "private_person", "score": 0.99,
         "word": "Alice ##- ##Marie Smith",
         "start": 6, "end": 23},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert matches[0].text == "Alice-Marie Smith"


async def test_empty_text_short_circuits(mock_load: MagicMock) -> None:
    """Empty / whitespace-only text returns no matches without invoking
    the pipeline (which would also handle it but at unnecessary cost)."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    detector = PrivacyFilterDetector()
    assert await detector.detect("") == []
    assert await detector.detect("   \n\t ") == []
    mock_load.assert_not_called()


async def test_hallucination_guard_drops_entities_not_in_input(
    mock_load: MagicMock,
) -> None:
    """If a span's offsets point outside the text or the resolved value
    isn't actually in the input, drop it. Mirrors the LLMDetector guard."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "hello world"
    mock_load.return_value = [
        # No offsets, word doesn't appear in text.
        {"entity_group": "private_person", "score": 0.99, "word": "ghost-name"},
    ]
    detector = PrivacyFilterDetector()
    assert await detector.detect(text) == []


def test_load_pipeline_wraps_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the privacy-filter extra installed, _load_pipeline must
    raise an ImportError that points at the install command — not the
    bare `No module named 'transformers'` mystery."""
    from anonymizer_guardrail.detector import privacy_filter as pf

    # Make `from transformers import pipeline` raise even if transformers
    # is actually installed in the dev env. Setting sys.modules entry to
    # None makes the import machinery treat it as missing.
    monkeypatch.setitem(sys.modules, "transformers", None)

    with pytest.raises(ImportError, match=r"privacy-filter"):
        pf._load_pipeline("any-model")


def test_detector_registered_in_pipeline() -> None:
    """`privacy_filter` is a known detector name — DETECTOR_MODE can list
    it without falling through to the unknown-name warning path."""
    from anonymizer_guardrail.detector import SPECS_BY_NAME

    assert "privacy_filter" in SPECS_BY_NAME


async def test_split_person_name_is_merged(mock_load: MagicMock) -> None:
    """The headline bug: NER models routinely emit `["Alice", "Smith"]`
    instead of one `"Alice Smith"` span. Without merging each half gets
    its own surrogate and the output reads as two concatenated fake
    names. With merging, the two spans collapse and produce ONE PERSON
    Match for the whole name."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Hi, my name is Alice Smith there"
    mock_load.return_value = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice",
         "start": 15, "end": 20},
        {"entity_group": "private_person", "score": 0.97, "word": "Smith",
         "start": 21, "end": 26},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 1
    assert matches[0].text == "Alice Smith"
    assert matches[0].entity_type == "PERSON"


async def test_slash_separated_persons_stay_distinct(
    mock_load: MagicMock,
) -> None:
    """Slash means list, not multi-word entity — `"Alice / Bob"` is two
    real people and must produce two separate surrogates so the upstream
    model can tell them apart."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "owners: Alice / Bob agree"
    mock_load.return_value = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice",
         "start": 8, "end": 13},
        {"entity_group": "private_person", "score": 0.99, "word": "Bob",
         "start": 16, "end": 19},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 2
    assert {m.text for m in matches} == {"Alice", "Bob"}


async def test_and_separated_persons_stay_distinct(
    mock_load: MagicMock,
) -> None:
    """Pure-whitespace gap is the only PERSON-merge case. The word "and"
    inside the gap (i.e. any alphanumeric) prevents the merge — these
    are clearly two different people."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Alice and Bob arrived"
    mock_load.return_value = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice",
         "start": 0, "end": 5},
        {"entity_group": "private_person", "score": 0.99, "word": "Bob",
         "start": 10, "end": 13},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 2


async def test_address_merges_across_comma(mock_load: MagicMock) -> None:
    """ADDRESS is the one type whose merge rule allows commas in the
    gap, because comma is part of the address itself ("123 Main St,
    Springfield" is one place). PERSON wouldn't merge across a comma."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "I live at 123 Main St, Springfield."
    mock_load.return_value = [
        {"entity_group": "private_address", "score": 0.97, "word": "123 Main St",
         "start": 10, "end": 21},
        {"entity_group": "private_address", "score": 0.93, "word": "Springfield",
         "start": 23, "end": 34},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 1
    assert matches[0].text == "123 Main St, Springfield"
    assert matches[0].entity_type == "ADDRESS"


async def test_persons_with_comma_stay_distinct(mock_load: MagicMock) -> None:
    """Comma between two PERSON spans is a list separator (CSV-style),
    not part of a single name. PERSON's gap rule is whitespace-only, so
    these stay as two separate entities."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Authors: Alice, Bob"
    mock_load.return_value = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice",
         "start": 9, "end": 14},
        {"entity_group": "private_person", "score": 0.99, "word": "Bob",
         "start": 16, "end": 19},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 2


async def test_paragraph_break_blocks_merge(mock_load: MagicMock) -> None:
    """A pure `\\n\\n` between two same-type spans is a paragraph break,
    which is a stronger separator than the model's same-label call.
    Don't merge — the spans are likely unrelated entities the model
    just happened to tag identically (e.g. a real date at the end of
    one paragraph and a "Resolution:" heading at the start of the next
    that got misclassified as a date).

    The gap text MUST be exactly the paragraph break with no other
    characters — otherwise the all-whitespace requirement on the gap
    pattern blocks the merge for the wrong reason and this test
    would pass under the pre-fix `\\s+` pattern too."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Date: 2026-04-17\n\nResolution: contact support."
    # text[16:18] is exactly "\n\n" — gap is the paragraph break and
    # nothing else, so the pattern is the only thing that decides
    # whether to merge.
    mock_load.return_value = [
        {"entity_group": "private_date", "score": 0.99, "word": "2026-04-17",
         "start": 6, "end": 16},
        {"entity_group": "private_date", "score": 0.99, "word": "Resolution:",
         "start": 18, "end": 29},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 2
    assert {m.text for m in matches} == {"2026-04-17", "Resolution:"}


async def test_paragraph_break_splits_one_span(mock_load: MagicMock) -> None:
    """When the HF pipeline's aggregation produces ONE span that
    already crosses a paragraph break (it groups adjacent same-entity
    tokens regardless of whitespace in the source), the split pass
    breaks it into the parts on either side. Same logical rule as the
    merge pass's `\\n\\n` carve-out, applied from the opposite
    direction — a paragraph boundary is a stronger separator than the
    model's same-label call.

    This is the realistic case in production: HF's
    aggregation_strategy="first" emits one span like
    "2026-04-17\\n\\nResolution:" because both halves got tagged
    `private_date`, and the merge pass never sees two spans to keep
    apart. Without this split pass the operator gets one DATE_OF_BIRTH
    Match covering a date AND a misclassified header, which makes the
    redaction nonsensical."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Date: 2026-04-17\n\nResolution: contact support."
    # The pipeline returned ONE span [6:29] covering
    # "2026-04-17\n\nResolution:" — a real-world artifact of HF's
    # aggregation collapsing adjacent same-entity tokens.
    mock_load.return_value = [
        {"entity_group": "private_date", "score": 0.99,
         "word": "2026-04-17 Resolution:",
         "start": 6, "end": 29},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 2
    assert {m.text for m in matches} == {"2026-04-17", "Resolution:"}


async def test_single_newline_still_merges(mock_load: MagicMock) -> None:
    """A *single* newline between same-type spans is in-paragraph line
    wrapping (a long name that broke across lines, etc.), not a
    paragraph boundary. Still merge — the paragraph hasn't ended, so
    the model is most likely splitting one entity rather than tagging
    two."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Hi, my name is Alice\nSmith there"
    mock_load.return_value = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice",
         "start": 15, "end": 20},
        {"entity_group": "private_person", "score": 0.97, "word": "Smith",
         "start": 21, "end": 26},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 1
    assert matches[0].text == "Alice\nSmith"
    assert matches[0].entity_type == "PERSON"


async def test_different_types_never_merge(mock_load: MagicMock) -> None:
    """A PERSON adjacent to an EMAIL_ADDRESS — even with a pure
    whitespace gap — must NOT merge. The merge rule is same-type only."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Alice alice@example.com"
    mock_load.return_value = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice",
         "start": 0, "end": 5},
        {"entity_group": "private_email", "score": 0.99, "word": "alice@example.com",
         "start": 6, "end": 23},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 2
    types = {m.entity_type for m in matches}
    assert types == {"PERSON", "EMAIL_ADDRESS"}


def test_module_import_does_not_pull_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the detector module on a slim install must NOT touch
    transformers — that's the whole point of the lazy-import design.
    Catches a regression where someone hoists the import to the top."""
    import anonymizer_guardrail.detector as detector_pkg

    # Capture the originals so we can restore them after the re-import.
    # monkeypatch reverts `sys.modules` entries but does NOT restore the
    # parent package's `.privacy_filter` attribute, which `import x.y`
    # silently rebinds to the freshly-loaded module. Without restoring
    # that attribute, later `from anonymizer_guardrail.detector import
    # privacy_filter as pf` calls in OTHER test files would receive a
    # detached module whose `config` attribute the test patched but
    # whose factory still references the original module's globals —
    # a silent split that surfaces as confusing failures downstream.
    original_pf_module = sys.modules["anonymizer_guardrail.detector.privacy_filter"]
    original_pf_attr = detector_pkg.privacy_filter

    monkeypatch.setitem(sys.modules, "transformers", None)
    monkeypatch.delitem(sys.modules, "anonymizer_guardrail.detector.privacy_filter", raising=False)
    try:
        # Should not raise.
        import anonymizer_guardrail.detector.privacy_filter  # noqa: F401
    finally:
        # Restore both the sys.modules entry and the parent-package
        # attribute so other tests can reuse the original module.
        sys.modules["anonymizer_guardrail.detector.privacy_filter"] = original_pf_module
        detector_pkg.privacy_filter = original_pf_attr