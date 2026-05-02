"""Tests for the PrivacyFilterDetector.

These tests must run on machines without `opf` (OpenAI's privacy-filter
inference wrapper) installed — it's an optional extra. We mock
`_load_model` to return a `_FakeOPF` standing in for the real model,
exercising:

  - label-to-ENTITY_TYPES mapping
  - offset-based text extraction (vs the span's `text` field, which
    used to be `word` under the transformers integration and could
    carry subword artifacts)
  - hallucination guard (entity must appear in the input)
  - the import-error path that fires when the optional extra isn't
    installed
  - merge / split / per-label gap rules (run after the model)
  - calibration JSON loading (env-var-pointed, no-ops if file missing)

The fixture's `_FakeOPF.return_value` mirrors the legacy
transformers-pipeline shape (list of dicts with `entity_group`,
`word`, `start`, `end`) so most of the suite reads identically to
its pre-opf form. The fake's `redact()` translates those dicts into
opf-shape DetectedSpan-likes on demand.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

# Match the other test files: harmless transitive config.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest


@dataclass(frozen=True)
class _FakeSpan:
    """Stand-in for opf's DetectedSpan — same `.label / .start / .end /
    .text` attribute surface the production `_to_matches` reads via
    `getattr`."""
    label: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _FakeRedactionResult:
    """Stand-in for opf's RedactionResult. Production code only reads
    `.detected_spans`; other fields are present so a future opf API
    reach (warning surface, schema_version) doesn't break us."""
    detected_spans: tuple[_FakeSpan, ...]
    text: str = ""
    warning: str | None = None


class _FakeOPF:
    """Test stand-in for `opf.OPF`. Exposes `.redact(text)` returning a
    `_FakeRedactionResult`, plus a no-op `set_viterbi_decoder` so the
    calibration-loading branch in `_load_model` can run without an
    actual opf install.

    Tests configure spans via `return_value`, the same attribute the
    pre-opf MagicMock fixture used. Each entry can be either a
    DetectedSpan-shape dict (`label / start / end / text`) or the
    legacy transformers-pipeline shape (`entity_group / start / end /
    word`); `redact()` accepts both and converts on read.
    """

    def __init__(self) -> None:
        # List of legacy or DetectedSpan-shape span dicts. Tests set
        # this directly via `mock_load.return_value = [...]` for
        # parity with the old MagicMock-based fixture API.
        self.return_value: list[dict[str, Any]] = []
        # Bookkeeping for the assertion patterns that used to rely on
        # MagicMock.assert_not_called() / .call_args_list.
        self.redact_calls: list[str] = []
        self.calibration_path: str | None = None

    def redact(self, text: str) -> _FakeRedactionResult:
        self.redact_calls.append(text)
        spans: list[_FakeSpan] = []
        for raw in self.return_value:
            label = raw.get("label") or raw.get("entity_group") or raw.get("entity") or ""
            text_field = raw.get("text") or raw.get("word") or ""
            start = raw.get("start")
            end = raw.get("end")
            spans.append(_FakeSpan(
                label=str(label),
                start=start if isinstance(start, int) else -1,
                end=end if isinstance(end, int) else -1,
                text=str(text_field),
            ))
        return _FakeRedactionResult(detected_spans=tuple(spans), text=text)

    def set_viterbi_decoder(self, *, calibration_path: str) -> None:
        # Track the load so the calibration-loaded test can assert it.
        self.calibration_path = calibration_path


@pytest.fixture
def mock_load(monkeypatch: pytest.MonkeyPatch) -> _FakeOPF:
    """Replace the lazy model loader with a `_FakeOPF` instance. Tests
    set `mock_load.return_value` to whatever span list they want the
    "model" to produce when `.redact()` is called.

    Calibration is left unset (the `PRIVACY_FILTER_CALIBRATION` env
    var is unset and the `_load_model` body's `os.path.isfile` check
    short-circuits before `set_viterbi_decoder` runs); a dedicated
    test below exercises the calibration-loading branch.
    """
    from anonymizer_guardrail.detector import privacy_filter as pf

    fake = _FakeOPF()
    monkeypatch.setattr(pf, "_load_model", lambda _name: fake)
    return fake


async def test_label_mapping_and_offsets(mock_load: _FakeOPF) -> None:
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


async def test_unknown_label_normalizes_to_other(mock_load: _FakeOPF) -> None:
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


async def test_offsets_preferred_over_word_field(mock_load: _FakeOPF) -> None:
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


async def test_empty_text_short_circuits(mock_load: _FakeOPF) -> None:
    """Empty / whitespace-only text returns no matches without invoking
    the model (which would also handle it but at unnecessary cost)."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    detector = PrivacyFilterDetector()
    # Constructor warms the model with a "warmup" redact — that's one
    # expected call. Snapshot it so we can assert the empty-text
    # detects don't add MORE calls.
    after_warmup = list(mock_load.redact_calls)
    assert await detector.detect("") == []
    assert await detector.detect("   \n\t ") == []
    assert mock_load.redact_calls == after_warmup


async def test_hallucination_guard_drops_entities_not_in_input(
    mock_load: _FakeOPF,
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


def test_load_model_wraps_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the privacy-filter extra installed, _load_model must
    raise an ImportError that points at the install command — not the
    bare `No module named 'opf'` mystery."""
    from anonymizer_guardrail.detector import privacy_filter as pf

    # Make `from opf._api import OPF` raise even if opf is installed in
    # the dev env. Setting sys.modules entry to None makes the import
    # machinery treat it as missing.
    monkeypatch.setitem(sys.modules, "opf", None)
    monkeypatch.setitem(sys.modules, "opf._api", None)

    with pytest.raises(ImportError, match=r"privacy-filter"):
        pf._load_model("any-model")


def test_detector_registered_in_pipeline() -> None:
    """`privacy_filter` is a known detector name — DETECTOR_MODE can list
    it without falling through to the unknown-name warning path."""
    from anonymizer_guardrail.detector import SPECS_BY_NAME

    assert "privacy_filter" in SPECS_BY_NAME


async def test_split_person_name_is_merged(mock_load: _FakeOPF) -> None:
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
    mock_load: _FakeOPF,
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
    mock_load: _FakeOPF,
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


async def test_address_merges_across_comma(mock_load: _FakeOPF) -> None:
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


async def test_persons_with_comma_stay_distinct(mock_load: _FakeOPF) -> None:
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


async def test_emails_never_merge_across_whitespace(mock_load: _FakeOPF) -> None:
    """EMAIL_ADDRESS has max_gap=0 — emails don't legitimately fragment
    with whitespace between halves, so two adjacent EMAIL_ADDRESS spans
    are far more likely two distinct emails (CSV-style listing) than
    one entity the tokenizer split. Per-label cap from the privacy-
    parser-inspired _MAX_GAP_BY_LABEL table."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Contacts: alice@example.com bob@example.com cc'd."
    mock_load.return_value = [
        {"entity_group": "private_email", "score": 0.99,
         "word": "alice@example.com",
         "start": 10, "end": 27},
        {"entity_group": "private_email", "score": 0.99,
         "word": "bob@example.com",
         "start": 28, "end": 43},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 2
    assert {m.text for m in matches} == {"alice@example.com", "bob@example.com"}


async def test_long_whitespace_gap_blocks_person_merge(mock_load: _FakeOPF) -> None:
    """PERSON's max_gap is 3 chars. A 4-space gap between two PERSON
    spans exceeds the cap and the merge is refused — even though every
    character in the gap is a valid connector. Caps complement the
    connector-set check: a long whitespace run is more likely a
    formatting boundary (column alignment, sentence break) than
    intra-name spacing."""
    from anonymizer_guardrail.detector.privacy_filter import PrivacyFilterDetector

    text = "Hi Alice    Smith there"
    mock_load.return_value = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice",
         "start": 3, "end": 8},
        {"entity_group": "private_person", "score": 0.97, "word": "Smith",
         "start": 12, "end": 17},
    ]
    detector = PrivacyFilterDetector()
    matches = await detector.detect(text)
    assert len(matches) == 2
    assert {m.text for m in matches} == {"Alice", "Smith"}


async def test_paragraph_break_blocks_merge(mock_load: _FakeOPF) -> None:
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


async def test_paragraph_break_splits_one_span(mock_load: _FakeOPF) -> None:
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


async def test_single_newline_still_merges(mock_load: _FakeOPF) -> None:
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


async def test_different_types_never_merge(mock_load: _FakeOPF) -> None:
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


def test_module_import_does_not_pull_opf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the detector module on a slim install must NOT touch
    opf — that's the whole point of the lazy-import design. Catches a
    regression where someone hoists the import to the top."""
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

    monkeypatch.setitem(sys.modules, "opf", None)
    monkeypatch.setitem(sys.modules, "opf._api", None)
    monkeypatch.delitem(sys.modules, "anonymizer_guardrail.detector.privacy_filter", raising=False)
    try:
        # Should not raise.
        import anonymizer_guardrail.detector.privacy_filter  # noqa: F401
    finally:
        # Restore both the sys.modules entry and the parent-package
        # attribute so other tests can reuse the original module.
        sys.modules["anonymizer_guardrail.detector.privacy_filter"] = original_pf_module
        detector_pkg.privacy_filter = original_pf_attr


# ── Calibration JSON loading ──────────────────────────────────────────────
# Two tests covering the optional Viterbi-calibration env var:
#  - missing file → graceful no-op (the production default; spike showed
#    opf's stock decoder already produces clean spans on our fixtures)
#  - present file → opf.set_viterbi_decoder is called with the path

def test_calibration_missing_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """If `PRIVACY_FILTER_CALIBRATION` points at a non-existent file,
    `_load_model` skips the `set_viterbi_decoder` call rather than
    raising — opf's stock decoder is the documented default and a
    typoed path shouldn't crash boot."""
    from anonymizer_guardrail.detector import privacy_filter as pf

    fake = _FakeOPF()

    # Stub the OPF constructor so we don't pull the real opf package.
    class _StubOPFModule:
        @staticmethod
        def OPF(**_kwargs: Any) -> _FakeOPF:
            return fake

    monkeypatch.setitem(sys.modules, "opf", _StubOPFModule())
    monkeypatch.setitem(sys.modules, "opf._api", _StubOPFModule())
    # Patch via CONFIG.model_copy because CONFIG is a frozen
    # BaseSettings instance instantiated at import time — env-var
    # changes after that point don't reach it. Standard project
    # pattern (see other detector test files for prior art).
    monkeypatch.setattr(pf, "CONFIG", pf.CONFIG.model_copy(update={
        "calibration": str(tmp_path / "does-not-exist.json"),
    }))

    pf._load_model("any-model")
    assert fake.calibration_path is None, (
        "Expected the calibration loader to skip when the path doesn't exist."
    )


def test_calibration_present_is_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """If `PRIVACY_FILTER_CALIBRATION` points at a real file,
    `_load_model` calls `opf.set_viterbi_decoder(calibration_path=...)`
    so the operating-points JSON gets applied to the decoder."""
    from anonymizer_guardrail.detector import privacy_filter as pf

    calibration_path = tmp_path / "calibration.json"
    # opf's actual JSON shape is {"operating_points": {"default": {"biases": {...}}}}
    # but the fake doesn't validate; we just need a path that exists.
    calibration_path.write_text(json.dumps({
        "operating_points": {"default": {"biases": {
            "transition_bias_background_stay": 0.0,
            "transition_bias_background_to_start": 0.0,
            "transition_bias_inside_to_continue": 0.0,
            "transition_bias_inside_to_end": 0.0,
            "transition_bias_end_to_background": 0.5,
            "transition_bias_end_to_start": 0.0,
        }}}
    }))

    fake = _FakeOPF()

    class _StubOPFModule:
        @staticmethod
        def OPF(**_kwargs: Any) -> _FakeOPF:
            return fake

    monkeypatch.setitem(sys.modules, "opf", _StubOPFModule())
    monkeypatch.setitem(sys.modules, "opf._api", _StubOPFModule())
    monkeypatch.setattr(pf, "CONFIG", pf.CONFIG.model_copy(update={
        "calibration": str(calibration_path),
    }))

    pf._load_model("any-model")
    assert fake.calibration_path == str(calibration_path)