"""Tests for tools/detector_bench.

Covers corpus loading + the scoring math. The HTTP layer is tested
manually via `scripts/detector_bench.sh --preset …` — too
heavy for the unit suite.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

# Match other test files: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from tools.detector_bench.corpus import (
    Case,
    Corpus,
    CorpusError,
    ExpectedEntity,
    load,
    resolve_path,
)
from tools.detector_bench.runner import (
    CaseResult,
    Summary,
    _merge_overrides,
    _types_in,
    score_case,
)


# ── corpus.resolve_path ────────────────────────────────────────────────────


def test_resolve_path_bundled_appends_yaml_when_missing() -> None:
    p = resolve_path("bundled:pentest")
    assert p.name == "pentest.yaml"


def test_resolve_path_bundled_keeps_explicit_extension() -> None:
    p = resolve_path("bundled:pentest.yaml")
    assert p.name == "pentest.yaml"


def test_resolve_path_filesystem_path_passes_through(tmp_path: Path) -> None:
    f = tmp_path / "x.yaml"
    f.write_text("cases: []")
    assert resolve_path(str(f)) == f


def test_resolve_path_bundled_empty_name_rejected() -> None:
    with pytest.raises(CorpusError, match="requires a name"):
        resolve_path("bundled:")


# ── corpus.load ────────────────────────────────────────────────────────────


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "corpus.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_load_minimal_corpus(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        cases:
          - id: c1
            text: "Email alice@example.com"
            expect:
              - text: "alice@example.com"
                type: EMAIL_ADDRESS
    """)
    c = load(str(p))
    assert c.name == "corpus"
    assert len(c.cases) == 1
    assert c.cases[0].id == "c1"
    assert c.cases[0].expect[0].type == "EMAIL_ADDRESS"
    assert c.cases[0].expect[0].tolerated_miss is False


def test_load_uppercases_type(tmp_path: Path) -> None:
    """Authors typing `email_address` shouldn't break scoring against
    the canonical UPPERCASE prefix the surrogate emits."""
    p = _write(tmp_path, """
        cases:
          - id: c1
            text: "alice@example.com"
            expect:
              - text: "alice@example.com"
                type: email_address
    """)
    c = load(str(p))
    assert c.cases[0].expect[0].type == "EMAIL_ADDRESS"


def test_load_must_keep_only_case_is_valid(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        cases:
          - id: fp
            text: "What is the capital of France?"
            must_keep: ["capital of France"]
    """)
    c = load(str(p))
    assert c.cases[0].must_keep == ("capital of France",)
    assert c.cases[0].expect == ()


def test_load_rejects_case_with_no_expectations(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        cases:
          - id: empty
            text: "anything"
    """)
    with pytest.raises(CorpusError, match="nothing to score"):
        load(str(p))


def test_load_rejects_expect_text_not_in_case_text(tmp_path: Path) -> None:
    """Authoring trap: edit the text, forget to update `expect:`. We
    catch this at load instead of running and reporting a mysterious
    100% recall against entities that aren't there."""
    p = _write(tmp_path, """
        cases:
          - id: typo
            text: "Email is bob@example.com"
            expect:
              - text: "alice@example.com"
                type: EMAIL_ADDRESS
    """)
    with pytest.raises(CorpusError, match="not a substring"):
        load(str(p))


def test_load_rejects_must_keep_not_in_text(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        cases:
          - id: typo
            text: "abc"
            must_keep: ["xyz"]
    """)
    with pytest.raises(CorpusError, match="not a substring"):
        load(str(p))


def test_load_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        cases:
          - id: dup
            text: "abc"
            must_keep: ["abc"]
          - id: dup
            text: "xyz"
            must_keep: ["xyz"]
    """)
    with pytest.raises(CorpusError, match="duplicate case id"):
        load(str(p))


def test_load_rejects_missing_id(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        cases:
          - text: "abc"
            must_keep: ["abc"]
    """)
    with pytest.raises(CorpusError, match="non-empty `id:`"):
        load(str(p))


def test_load_missing_file_message_includes_path() -> None:
    with pytest.raises(CorpusError, match="not found"):
        load("/tmp/does-not-exist-corpus-12345.yaml")


def test_load_empty_cases_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "cases: []\n")
    with pytest.raises(CorpusError, match="non-empty"):
        load(str(p))


def test_load_bundled_pentest_corpus_is_valid() -> None:
    """The shipped starter corpus must always parse — guards against
    drift if we tweak the schema."""
    c = load("bundled:pentest")
    assert c.name
    assert len(c.cases) >= 5


# ── runner._types_in ───────────────────────────────────────────────────────


def test_types_in_counts_each_token() -> None:
    text = "Hello [PERSON_AB12CD34], your hash is [HASH_DEADBEEF]."
    counts = _types_in(text)
    assert counts == {"PERSON": 1, "HASH": 1}


def test_types_in_handles_compound_type_names() -> None:
    text = "[EMAIL_ADDRESS_AAAAAAAA] and [IPV4_ADDRESS_BBBBBBBB]"
    counts = _types_in(text)
    assert counts == {"EMAIL_ADDRESS": 1, "IPV4_ADDRESS": 1}


def test_types_in_ignores_lowercase_brackets() -> None:
    """Faker output isn't a [TYPE_HEX] token — must not be counted."""
    text = "Hello world, this [is just] markdown."
    assert _types_in(text) == {}


# ── runner._merge_overrides ────────────────────────────────────────────────


def test_merge_overrides_extras_win_on_collision() -> None:
    """`--compare` relies on this: corpus may pin its own detector_mode
    override but the per-run filter must take precedence."""
    corpus = {"detector_mode": ["regex", "llm"], "use_faker": True}
    extras = {"detector_mode": ["regex"]}
    merged = _merge_overrides(corpus, extras)
    assert merged["detector_mode"] == ["regex"]
    assert merged["use_faker"] is True


def test_merge_overrides_no_extras_returns_corpus_dict() -> None:
    corpus = {"regex_patterns": "pentest"}
    assert _merge_overrides(corpus, None) == corpus
    assert _merge_overrides(corpus, {}) == corpus


def test_merge_overrides_does_not_mutate_corpus() -> None:
    """Merge must produce a new dict — corpus.overrides is read across
    every case in the loop and must stay stable."""
    corpus = {"a": 1}
    extras = {"b": 2}
    merged = _merge_overrides(corpus, extras)
    assert merged == {"a": 1, "b": 2}
    assert corpus == {"a": 1}  # original untouched


# ── runner.score_case ──────────────────────────────────────────────────────


def _case(
    *,
    expect: tuple[ExpectedEntity, ...] = (),
    must_keep: tuple[str, ...] = (),
    text: str = "x",
) -> Case:
    return Case(id="t", text=text, expect=expect, must_keep=must_keep)


def test_score_case_full_recall_correct_types() -> None:
    case = _case(
        expect=(
            ExpectedEntity("alice@example.com", "EMAIL_ADDRESS"),
            ExpectedEntity("10.0.0.1", "IPV4_ADDRESS"),
        ),
    )
    response = "Email [EMAIL_ADDRESS_AAAAAAAA] from [IPV4_ADDRESS_BBBBBBBB]"
    redacted, redacted_excl, type_correct, type_expected, kept, must_total, missed, leaked = (
        score_case(case, response)
    )
    assert redacted == 2
    assert redacted_excl == 2
    assert type_correct == 2
    assert type_expected == 2
    assert missed == ()
    assert leaked == ()


def test_score_case_records_missed_entities() -> None:
    case = _case(
        expect=(
            ExpectedEntity("alice@example.com", "EMAIL_ADDRESS"),
            ExpectedEntity("10.0.0.1", "IPV4_ADDRESS"),
        ),
    )
    response = "Email [EMAIL_ADDRESS_AAAAAAAA] from 10.0.0.1"  # IP not redacted
    redacted, *_, missed, _ = score_case(case, response)
    assert redacted == 1
    assert [e.text for e in missed] == ["10.0.0.1"]


def test_score_case_tolerated_miss_doesnt_count_against_strict_recall() -> None:
    case = _case(
        expect=(
            ExpectedEntity("Project Zephyr", "ORGANIZATION", tolerated_miss=True),
            ExpectedEntity("alice@example.com", "EMAIL_ADDRESS"),
        ),
    )
    # Tolerated entity NOT redacted, strict one IS.
    response = "Project Zephyr launches; mail [EMAIL_ADDRESS_AAAAAAAA]."
    redacted, redacted_excl, *_ = score_case(case, response)
    assert redacted == 1            # raw recall: 1 of 2 expected
    assert redacted_excl == 1       # strict recall: 1 of 1 non-tolerated


def test_score_case_type_accuracy_penalises_misclassification() -> None:
    """Two PERSONs expected, response has one PERSON + one ORG (mismatched
    classification). type_correct should be 1, not 2."""
    case = _case(
        expect=(
            ExpectedEntity("Alice", "PERSON"),
            ExpectedEntity("Bob", "PERSON"),
        ),
    )
    response = "[PERSON_AAAAAAAA] meets [ORGANIZATION_BBBBBBBB]"
    _, _, type_correct, type_expected, *_ = score_case(case, response)
    assert type_correct == 1
    assert type_expected == 2


def test_score_case_must_keep_kept() -> None:
    case = _case(
        text="The capital of France is Paris.",
        must_keep=("capital of France",),
    )
    response = "The capital of France is [PERSON_AAAAAAAA]."
    *_, kept, total, _, leaked = score_case(case, response)
    assert kept == 1
    assert total == 1
    assert leaked == ()


def test_score_case_must_keep_leaked() -> None:
    case = _case(
        text="documentation example uuid",
        must_keep=("documentation example",),
    )
    # The literal phrase got mangled (treated as a redacted span).
    response = "[OTHER_AAAAAAAA] uuid"
    *_, kept, total, _, leaked = score_case(case, response)
    assert kept == 0
    assert total == 1
    assert leaked == ("documentation example",)


def test_score_case_with_no_expectations_returns_zeros() -> None:
    """Pure must_keep cases shouldn't crash on empty expect lists."""
    case = _case(must_keep=("x",), text="x y")
    redacted, redacted_excl, type_correct, type_expected, *_ = score_case(case, "x y")
    assert redacted == 0
    assert redacted_excl == 0
    assert type_correct == 0
    assert type_expected == 0


# ── Summary blocked / scored partitioning ──────────────────────────────────


def _scored_result(
    case_id: str,
    *,
    expected: int = 1,
    redacted: int = 1,
    must_keep: int = 0,
    must_keep_kept: int = 0,
    latency_ms: float = 100.0,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        skipped=False,
        expected_total=expected,
        expected_tolerated=0,
        redacted_count=redacted,
        redacted_excluding_tolerated=redacted,
        type_correct=redacted,
        type_expected=expected,
        must_keep_total=must_keep,
        must_keep_kept=must_keep_kept,
        latency_ms=latency_ms,
    )


def _blocked_result(case_id: str, *, expected: int = 1, latency_ms: float = 50.0) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        skipped=False,
        blocked=True,
        blocked_reason="LLM unreachable",
        expected_total=expected,
        latency_ms=latency_ms,
    )


def test_summary_excludes_blocked_from_metrics() -> None:
    """A blocked case should NOT pull recall/precision/etc. down — it
    measures error policy, not detection quality. Two scored cases
    with full recall + one blocked case should report recall=100%,
    not 67%."""
    s = Summary(cases=[
        _scored_result("a", expected=2, redacted=2),
        _scored_result("b", expected=2, redacted=2),
        _blocked_result("c", expected=4),
    ])
    assert s.recall == 1.0
    assert s.recall_excluding_tolerated == 1.0
    assert s.type_accuracy == 1.0
    assert len(s.scored) == 2
    assert len(s.blocked) == 1
    assert len(s.skipped) == 0


def test_summary_executed_includes_blocked_for_back_compat() -> None:
    """`executed` is non-skipped (= blocked + scored). Used by callers
    asking "did the variant run at all"."""
    s = Summary(cases=[
        _scored_result("a"),
        _blocked_result("b"),
        CaseResult(case_id="c", skipped=True, skip_reason="x"),
    ])
    assert {c.case_id for c in s.executed} == {"a", "b"}
    assert {c.case_id for c in s.scored} == {"a"}
    assert {c.case_id for c in s.blocked} == {"b"}
    assert {c.case_id for c in s.skipped} == {"c"}


def test_summary_avg_latency_ignores_blocked() -> None:
    """avg_latency is over scored cases only. A blocked case at
    50ms shouldn't drag down the average for scored cases at 100ms."""
    s = Summary(cases=[
        _scored_result("a", latency_ms=100.0),
        _scored_result("b", latency_ms=200.0),
        _blocked_result("c", latency_ms=50.0),
    ])
    assert s.avg_latency_ms == 150.0


def test_summary_all_blocked_returns_none_metrics() -> None:
    """If every case got blocked, the metric denominators are zero —
    the properties should return None rather than 0%, so the renderer
    can show 'n/a' instead of misleading '0%'."""
    s = Summary(cases=[
        _blocked_result("a"),
        _blocked_result("b"),
    ])
    assert s.recall is None
    assert s.recall_excluding_tolerated is None
    assert s.type_accuracy is None
    assert s.precision is None
    assert s.avg_latency_ms == 0.0  # latency falls back to 0 (renderer hides it)
    assert len(s.blocked) == 2
    assert len(s.scored) == 0
