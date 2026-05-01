"""Corpus loading + validation.

A corpus is a YAML file describing a benchmark scenario: which texts
to send, which substrings should be redacted, what type each should
be classified as. One corpus per scenario (pentest, legal,
healthcare, …) so the same harness scores different deployments.

The schema is deliberately small — the format is meant to be easy to
hand-author and easy to fork. Everything except `cases` is optional;
within a case, `text` and `expect` are required.

Validation is fail-loud at load time: a typo in the YAML crashes the
benchmark before any HTTP traffic, so the operator sees a clean
error pointing at the corpus file rather than a confusing low score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Bundled corpora live next to the test suite — they're test fixtures,
# not part of the shipped package. `bundled:NAME` resolves to
# `tests/corpus/NAME.yaml` so callers don't need full paths for
# the starter corpora.
_BUNDLED_DIR = Path(__file__).resolve().parents[2] / "tests" / "corpus"


@dataclass(frozen=True)
class ExpectedEntity:
    """One substring the operator expects to be redacted, with the
    type they consider correct. `tolerated_miss=True` excludes this
    entity from the strict recall score (counted in
    `recall_excluding_tolerated` only) — useful for marking edge
    cases the chosen detector mix isn't expected to catch."""
    text: str
    type: str
    tolerated_miss: bool = False


@dataclass(frozen=True)
class Case:
    """One test case: a text to anonymize, the entities expected to
    be redacted, and (optionally) substrings that MUST stay
    untouched (false-positive sentinels). `requires` lists detector
    names the case depends on — when any are missing from the
    running guardrail's DETECTOR_MODE, the case is skipped, not
    failed."""
    id: str
    text: str
    expect: tuple[ExpectedEntity, ...] = ()
    must_keep: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class Corpus:
    """Whole loaded scenario."""
    name: str
    description: str
    overrides: dict[str, Any] = field(default_factory=dict)
    cases: tuple[Case, ...] = ()
    path: str = ""  # source path, for error messages


class CorpusError(ValueError):
    """Raised on any corpus validation failure. Always carries a
    file-path-aware message so the operator knows which YAML to
    fix."""


def resolve_path(spec: str) -> Path:
    """Resolve `bundled:NAME` or a filesystem path to a Path. Same
    syntax as `REGEX_PATTERNS_PATH` etc. so corpus references look
    familiar to operators who've configured the guardrail."""
    if spec.startswith("bundled:"):
        name = spec[len("bundled:"):]
        if not name:
            raise CorpusError("'bundled:' requires a name (e.g. 'bundled:pentest').")
        # Tolerate `bundled:pentest` and `bundled:pentest.yaml` — the
        # operator's mental model is the corpus name, not the suffix.
        if not name.endswith(".yaml") and not name.endswith(".yml"):
            name = name + ".yaml"
        return _BUNDLED_DIR / name
    return Path(spec)


def load(path_or_spec: str) -> Corpus:
    """Load a corpus from a `bundled:NAME` spec or a filesystem
    path. Raises CorpusError on missing file, bad YAML, or schema
    violations."""
    try:
        import yaml
    except ImportError as exc:
        raise CorpusError(
            "PyYAML is required to load corpora. Install with "
            "`pip install pyyaml` or via the dev extras."
        ) from exc

    path = resolve_path(path_or_spec)
    if not path.is_file():
        raise CorpusError(f"Corpus file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CorpusError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise CorpusError(f"{path}: top-level must be a mapping, got {type(raw).__name__}")

    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise CorpusError(f"{path}: must define a non-empty `cases:` list.")

    cases: list[Case] = []
    seen_ids: set[str] = set()
    for i, case_raw in enumerate(cases_raw):
        if not isinstance(case_raw, dict):
            raise CorpusError(f"{path}: cases[{i}] must be a mapping.")
        case = _parse_case(case_raw, i, path)
        if case.id in seen_ids:
            raise CorpusError(f"{path}: duplicate case id {case.id!r}.")
        seen_ids.add(case.id)
        cases.append(case)

    overrides_raw = raw.get("overrides", {})
    if not isinstance(overrides_raw, dict):
        raise CorpusError(f"{path}: `overrides:` must be a mapping if present.")

    return Corpus(
        name=str(raw.get("name") or path.stem),
        description=str(raw.get("description") or ""),
        overrides=dict(overrides_raw),
        cases=tuple(cases),
        path=str(path),
    )


def _parse_case(case_raw: dict, index: int, path: Path) -> Case:
    cid = case_raw.get("id")
    if not isinstance(cid, str) or not cid:
        raise CorpusError(f"{path}: cases[{index}] requires a non-empty `id:`.")

    text = case_raw.get("text")
    if not isinstance(text, str) or not text:
        raise CorpusError(f"{path}: case {cid!r} requires a non-empty `text:`.")

    expect = _parse_expect(case_raw.get("expect"), cid, path)
    must_keep = _parse_str_list(case_raw.get("must_keep"), cid, path, "must_keep")
    requires = _parse_str_list(case_raw.get("requires"), cid, path, "requires")

    if not expect and not must_keep:
        # A case with neither tells us nothing about quality; almost
        # certainly an authoring error. Refusing here is more useful
        # than silently scoring 100% on an empty rubric.
        raise CorpusError(
            f"{path}: case {cid!r} has neither `expect:` nor `must_keep:` — "
            f"there's nothing to score."
        )

    # Sanity-check that every expected substring actually appears in
    # the text. Catches the very common "I edited the text but forgot
    # to update expect" authoring bug.
    for e in expect:
        if e.text not in text:
            raise CorpusError(
                f"{path}: case {cid!r}: expect entry {e.text!r} is not "
                f"a substring of the case text."
            )
    for m in must_keep:
        if m not in text:
            raise CorpusError(
                f"{path}: case {cid!r}: must_keep entry {m!r} is not "
                f"a substring of the case text."
            )

    return Case(
        id=cid,
        text=text,
        expect=expect,
        must_keep=must_keep,
        requires=requires,
    )


def _parse_expect(raw: Any, cid: str, path: Path) -> tuple[ExpectedEntity, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CorpusError(f"{path}: case {cid!r}: `expect:` must be a list if present.")
    out: list[ExpectedEntity] = []
    for j, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CorpusError(f"{path}: case {cid!r}: expect[{j}] must be a mapping.")
        text = entry.get("text")
        etype = entry.get("type")
        if not isinstance(text, str) or not text:
            raise CorpusError(f"{path}: case {cid!r}: expect[{j}] needs `text:`.")
        if not isinstance(etype, str) or not etype:
            raise CorpusError(f"{path}: case {cid!r}: expect[{j}] needs `type:`.")
        tolerated = entry.get("tolerated_miss", False)
        if not isinstance(tolerated, bool):
            raise CorpusError(
                f"{path}: case {cid!r}: expect[{j}].tolerated_miss must be a bool."
            )
        out.append(ExpectedEntity(text=text, type=etype.upper(), tolerated_miss=tolerated))
    return tuple(out)


def _parse_str_list(raw: Any, cid: str, path: Path, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CorpusError(
            f"{path}: case {cid!r}: `{field_name}:` must be a list if present."
        )
    for j, item in enumerate(raw):
        if not isinstance(item, str) or not item:
            raise CorpusError(
                f"{path}: case {cid!r}: {field_name}[{j}] must be a non-empty string."
            )
    return tuple(raw)


__all__ = [
    "Case",
    "Corpus",
    "CorpusError",
    "ExpectedEntity",
    "load",
    "resolve_path",
]
