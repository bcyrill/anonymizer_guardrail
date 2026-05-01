"""Benchmark execution + scoring.

For each case:

  1. Send the text to the guardrail with `use_faker: false` so the
     response uses opaque `[TYPE_HEX]` tokens — that lets us recover
     types from the response string without a second request.
  2. Compute per-case scores by checking which expected substrings
     are absent from the response (recall) and which `must_keep`
     substrings survived (precision).
  3. For type accuracy, count how many of each type were expected
     and how many `[TYPE_*]` tokens of the same type appear in the
     response. The aggregate is a useful proxy for "did the
     classifier get the type right" without needing per-entity
     attribution from the guardrail.

Scoring choices kept simple on purpose: this is a benchmark for
operators, not a research artefact. If you need finer attribution,
extend the per-entity check to do a per-substring isolated request.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from .corpus import Case, Corpus, ExpectedEntity


# Opaque-token format from src/anonymizer_guardrail/surrogate.py:134
# (`f"[{prefix}_{digest}]"`). The digest is BLAKE2b truncated to
# 8 hex chars uppercase. The prefix is the entity-type name and
# can contain digits (`IPV4_ADDRESS`, `IPV6_CIDR`, …) — early drafts
# of this regex used `[A-Z_]+` and silently lost every IP-shaped
# match. We use this regex to read types back out of the response
# after `use_faker: false` flips surrogates to the prefixed form.
_TOKEN_RE = re.compile(r"\[([A-Z][A-Z0-9_]*)_([0-9A-F]{8,})\]")


@dataclass(frozen=True)
class CaseResult:
    """Per-case scoring output."""
    case_id: str
    skipped: bool
    skip_reason: str = ""
    blocked: bool = False
    blocked_reason: str = ""
    expected_total: int = 0
    expected_tolerated: int = 0
    redacted_count: int = 0
    redacted_excluding_tolerated: int = 0
    type_correct: int = 0
    type_expected: int = 0
    must_keep_total: int = 0
    must_keep_kept: int = 0
    latency_ms: float = 0.0
    missed: tuple[ExpectedEntity, ...] = ()
    leaked: tuple[str, ...] = ()  # must_keep entries that were redacted


@dataclass
class Summary:
    """Aggregate across all cases.

    Three states a case can be in:

      * **skipped** — the case never dispatched a request (its
        `requires:` declared a detector the running guardrail doesn't
        have, or the HTTP transport failed before we could measure
        anything).
      * **blocked** — the case dispatched, but the guardrail returned
        `BLOCKED` (typically `LLM_FAIL_CLOSED` tripped by an upstream
        error). Surfaced separately because BLOCKED measures error
        policy / availability, not detection quality — folding it
        into the metrics would make a flaky LLM look like a
        recall problem.
      * **scored** — everything else: the case ran end-to-end and
        produced a response we could compute recall / type accuracy
        / precision against. The aggregate metrics divide only over
        these.

    `executed` is kept as a back-compat alias for `scored` (the
    non-skipped cases, including blocked) — the comparison renderer
    needs to know "did the variant actually do anything" without
    caring whether responses came back BLOCKED.
    """
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def executed(self) -> list[CaseResult]:
        """Non-skipped cases — includes blocked ones. Used by callers
        that need to know "did the variant run at all"."""
        return [c for c in self.cases if not c.skipped]

    @property
    def skipped(self) -> list[CaseResult]:
        return [c for c in self.cases if c.skipped]

    @property
    def blocked(self) -> list[CaseResult]:
        return [c for c in self.cases if c.blocked]

    @property
    def scored(self) -> list[CaseResult]:
        """Cases used as the denominator for recall / precision /
        type accuracy. Excludes both skipped and blocked."""
        return [c for c in self.cases if not c.skipped and not c.blocked]

    @property
    def recall(self) -> float | None:
        sc = self.scored
        total = sum(c.expected_total for c in sc)
        if total == 0:
            return None
        return sum(c.redacted_count for c in sc) / total

    @property
    def recall_excluding_tolerated(self) -> float | None:
        sc = self.scored
        total = sum(c.expected_total - c.expected_tolerated for c in sc)
        if total == 0:
            return None
        return sum(c.redacted_excluding_tolerated for c in sc) / total

    @property
    def type_accuracy(self) -> float | None:
        sc = self.scored
        total = sum(c.type_expected for c in sc)
        if total == 0:
            return None
        return sum(c.type_correct for c in sc) / total

    @property
    def precision(self) -> float | None:
        sc = self.scored
        total = sum(c.must_keep_total for c in sc)
        if total == 0:
            return None
        return sum(c.must_keep_kept for c in sc) / total

    @property
    def avg_latency_ms(self) -> float:
        sc = self.scored
        if not sc:
            return 0.0
        return sum(c.latency_ms for c in sc) / len(sc)


# Detector mode missing from /health → these cases sit out.
def _required_detectors_present(case: Case, active: set[str]) -> tuple[bool, str]:
    """When the case declares `requires: [...]` and any of those
    detectors aren't in the running guardrail's `DETECTOR_MODE`,
    the case is skipped (not failed) — same policy as test-examples.sh.
    """
    if not case.requires:
        return True, ""
    missing = [d for d in case.requires if d not in active]
    if missing:
        return False, f"requires {','.join(missing)} not in DETECTOR_MODE"
    return True, ""


def _build_request_body(case: Case, overrides: dict[str, Any]) -> bytes:
    """Single-text request body. Always forces `use_faker: false` so
    the response carries `[TYPE_HEX]` tokens we can inspect for type
    accuracy. Operator overrides from the corpus are layered ON TOP of
    that — they cannot un-set use_faker (would defeat type scoring)."""
    final_overrides = dict(overrides)
    final_overrides["use_faker"] = False
    body = {
        "texts": [case.text],
        "input_type": "request",
        "litellm_call_id": f"bench-{case.id}",
        "additional_provider_specific_params": final_overrides,
    }
    return json.dumps(body).encode("utf-8")


def _merge_overrides(
    corpus_overrides: dict[str, Any],
    extra_overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge corpus-level overrides with run-level extras. Extras win
    on key collision — that's how `--compare` filters down to a single
    detector despite the corpus author setting their own overrides.
    """
    if not extra_overrides:
        return corpus_overrides
    merged = dict(corpus_overrides)
    merged.update(extra_overrides)
    return merged


def _post(base_url: str, body: bytes, timeout_s: float) -> dict[str, Any]:
    """Plain stdlib HTTP — no extra deps for the bench script.
    Raises RuntimeError on transport / non-200; caller handles."""
    url = base_url.rstrip("/") + "/beta/litellm_basic_guardrail_api"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} from guardrail: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Cannot reach guardrail at {url}: {exc}") from exc


def _types_in(text: str) -> Counter[str]:
    """Count each `[TYPE_HEX]` occurrence in the text. Used both to
    score type accuracy and to identify the redaction footprint."""
    return Counter(m.group(1) for m in _TOKEN_RE.finditer(text))


def score_case(
    case: Case,
    response_text: str,
) -> tuple[int, int, int, int, int, int, tuple[ExpectedEntity, ...], tuple[str, ...]]:
    """Pure scoring: given the response text, return the per-case
    counts. Split out so the unit tests can exercise scoring without
    spinning up an HTTP server.

    Returns:
      (redacted_count, redacted_excluding_tolerated,
       type_correct, type_expected,
       must_keep_kept,  must_keep_total,
       missed_entities, leaked_must_keep)
    """
    redacted = 0
    redacted_excl = 0
    missed: list[ExpectedEntity] = []
    type_expected_counter: Counter[str] = Counter()

    for e in case.expect:
        # Recall: was the substring removed? Substring presence is
        # the operator-meaningful signal; partial-redaction edge
        # cases don't count as "redacted".
        was_redacted = e.text not in response_text
        if was_redacted:
            redacted += 1
            if not e.tolerated_miss:
                redacted_excl += 1
        else:
            missed.append(e)
        type_expected_counter[e.type] += 1

    # Type accuracy — aggregate by type. min(expected[T], present[T])
    # is the count we got right; we don't try to attribute per-entity
    # because the guardrail doesn't tell us which surrogate replaced
    # which expected substring.
    types_present = _types_in(response_text)
    type_correct = sum(
        min(count, types_present.get(t, 0))
        for t, count in type_expected_counter.items()
    )
    type_expected = sum(type_expected_counter.values())

    # Precision: must_keep substrings should still appear verbatim
    # in the response. A leaked one means the detector mix flagged
    # a false positive.
    leaked: list[str] = []
    must_keep_kept = 0
    for m in case.must_keep:
        if m in response_text:
            must_keep_kept += 1
        else:
            leaked.append(m)

    return (
        redacted,
        redacted_excl,
        type_correct,
        type_expected,
        must_keep_kept,
        len(case.must_keep),
        tuple(missed),
        tuple(leaked),
    )


def run(
    base_url: str,
    corpus: Corpus,
    *,
    active_detectors: Iterable[str],
    timeout_s: float = 30.0,
    extra_overrides: dict[str, Any] | None = None,
) -> Summary:
    """Execute the corpus against `base_url` and return a Summary.

    `extra_overrides` is merged on top of `corpus.overrides` per
    request (extras win). Used by `--compare` to filter the active
    detector set per run via `{"detector_mode": [name]}` without
    mutating the corpus.

    Pure: caller drives presentation.
    """
    active = set(active_detectors)
    merged_overrides = _merge_overrides(corpus.overrides, extra_overrides)
    summary = Summary()

    for case in corpus.cases:
        ok, reason = _required_detectors_present(case, active)
        if not ok:
            summary.cases.append(
                CaseResult(case_id=case.id, skipped=True, skip_reason=reason)
            )
            continue

        body = _build_request_body(case, merged_overrides)
        t0 = time.monotonic()
        try:
            resp = _post(base_url, body, timeout_s)
        except RuntimeError as exc:
            summary.cases.append(
                CaseResult(
                    case_id=case.id,
                    skipped=True,
                    skip_reason=f"transport error: {exc}",
                )
            )
            continue
        latency_ms = (time.monotonic() - t0) * 1000

        action = resp.get("action")
        if action == "BLOCKED":
            summary.cases.append(
                CaseResult(
                    case_id=case.id,
                    skipped=False,
                    blocked=True,
                    blocked_reason=str(resp.get("blocked_reason") or ""),
                    expected_total=len(case.expect),
                    expected_tolerated=sum(1 for e in case.expect if e.tolerated_miss),
                    must_keep_total=len(case.must_keep),
                    latency_ms=latency_ms,
                )
            )
            continue

        # `texts` is None when action=NONE (no redaction); fall back
        # to the original — that's effectively zero recall, which is
        # exactly what the score should reflect.
        texts = resp.get("texts") or [case.text]
        response_text = texts[0] if texts else case.text

        (
            redacted,
            redacted_excl,
            type_correct,
            type_expected,
            kept,
            must_total,
            missed,
            leaked,
        ) = score_case(case, response_text)

        summary.cases.append(
            CaseResult(
                case_id=case.id,
                skipped=False,
                expected_total=len(case.expect),
                expected_tolerated=sum(1 for e in case.expect if e.tolerated_miss),
                redacted_count=redacted,
                redacted_excluding_tolerated=redacted_excl,
                type_correct=type_correct,
                type_expected=type_expected,
                must_keep_total=must_total,
                must_keep_kept=kept,
                latency_ms=latency_ms,
                missed=missed,
                leaked=leaked,
            )
        )

    return summary


__all__ = [
    "CaseResult",
    "Summary",
    "run",
    "score_case",
]
