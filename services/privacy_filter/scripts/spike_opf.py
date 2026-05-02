#!/usr/bin/env python3
"""Phase-1 spike for the opf-based Viterbi decoding migration.

Runs the OpenAI Privacy Filter model via the `opf` package's API
against the existing fixtures, with three different calibration
profiles. Goal: decide whether tuned Viterbi terminates spans at
paragraph boundaries cleanly enough to replace our post-hoc
`_PARAGRAPH_BREAK` split pass in
`services/privacy_filter/main.py`.

See TASKS.md → "opf-based Viterbi decoding" → Phase 1 for the
full plan and the decision criteria this script informs.

Prerequisites:

    # Install CPU-only torch FIRST so the opf install doesn't pull
    # the ~3 GB CUDA build from PyPI (which is its default on Linux):
    pip install torch --index-url https://download.pytorch.org/whl/cpu

    # Then install opf — pip sees torch satisfied and leaves it alone:
    pip install git+https://github.com/openai/privacy-filter@main

If you accidentally installed CUDA torch first, the cleanup needs
to nuke ~15 nvidia-* packages plus triton — package names vary by
PyTorch CUDA version (cu12 vs cu13 vs unsuffixed), so the safest
recovery is a glob match:

    pip freeze | grep -iE '^(nvidia-|triton)' | sed 's/[=@].*//' \\
        | xargs -r pip uninstall -y
    pip uninstall -y torch
    pip install torch --index-url https://download.pytorch.org/whl/cpu
    pip install --force-reinstall --no-deps \\
        git+https://github.com/openai/privacy-filter@main

Cold first run also downloads ~3 GB of model weights to
`~/.opf/privacy_filter` (override via `$OPF_CHECKPOINT`).
Subsequent runs are fast — model + torch are cached.

Run from the repo root:

    python services/privacy_filter/scripts/spike_opf.py

Output: for each fixture, three runs side by side:

  1. `default` — opf with no bias tuning. Establishes the baseline
     and confirms the decoder works at all without calibration.
  2. `anti-merge` — biases tuned for OUR use case: encourage span
     termination at neutral context, discourage running across
     paragraph breaks. If this profile produces clean spans on
     `sample.txt` (no `Customer:` / `Resolution:` glued onto dates,
     no `Issue` glued onto the address) and on `engagement_notes.txt`
     (no `Spotted` / `SHA-256` glued onto the AWS-key block / JWT),
     the migration is worth doing.
  3. `privacy-parser` — biases the chiefautism/privacy-parser repo
     uses (pro-merge, designed to glue fragmented person names back
     together). Included as a contrast: confirms the bias dial
     actually moves behaviour in the expected direction. If this
     profile makes things *worse* than `default` on our fixtures
     while `anti-merge` makes them better, the calibration surface
     is doing what we expect.

The compared "current behaviour" baseline lives in
services/privacy_filter/scripts/PROBE.md → Empirical findings —
the post-fix span tables there are what each profile here should
match or beat.

Spans containing `\\n\\n` are flagged in the output with a warning
marker — those are the over-merges the migration needs to
eliminate.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


try:
    from opf._api import OPF
except ImportError:
    sys.stderr.write(
        "spike_opf.py requires the `opf` package, which isn't on PyPI.\n"
        "Install with:\n"
        "    pip install git+https://github.com/openai/privacy-filter@main\n"
        "(~3 GB model weights download on first run.)\n"
    )
    sys.exit(1)


# Repo root: this script lives at
# `services/privacy_filter/scripts/spike_opf.py` so .parents[3] is
# the repo top.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_FIXTURES = (
    _REPO_ROOT / "services" / "privacy_filter" / "scripts" / "sample.txt",
    _REPO_ROOT / "services" / "gliner_pii" / "scripts" / "engagement_notes.txt",
)


# Calibration profiles to compare. Each dict is the value subset of
# what gets written to the operating-points JSON; missing biases are
# filled with 0.0 by `_write_calibration` below.
_PROFILES: dict[str, dict[str, float]] = {
    "default": {
        # All zeros — opf's built-in defaults, no calibration applied.
        # The baseline answer for whether the decoder needs *any*
        # tuning to be better than HF's aggregation_strategy="first".
    },
    "anti-merge": {
        # OUR target setting: encourage span termination at neutral
        # context, discourage running across uncertainty. If this
        # profile produces clean spans on the over-merge cases
        # documented in PROBE.md, the migration replaces the split
        # pass with a principled decode-time fix.
        "transition_bias_inside_to_continue": -0.3,
        "transition_bias_inside_to_end":       0.2,
        "transition_bias_end_to_background":   0.5,
    },
    "privacy-parser": {
        # The biases chiefautism/privacy-parser uses (pro-merge —
        # designed to glue fragmented person names back together).
        # Included for *contrast*: confirms the bias dial works in
        # the expected direction. On our fixtures we expect this
        # profile to either match `default` or make things worse,
        # while `anti-merge` should improve on it.
        "transition_bias_end_to_start":        -0.5,
        "transition_bias_inside_to_continue":   0.2,
    },
}


# The opf calibration loader requires all six bias keys present —
# any missing key in the JSON is a load error, not a default-zero.
_REQUIRED_BIAS_KEYS = (
    "transition_bias_background_stay",
    "transition_bias_background_to_start",
    "transition_bias_inside_to_continue",
    "transition_bias_inside_to_end",
    "transition_bias_end_to_background",
    "transition_bias_end_to_start",
)


def _write_calibration(biases: dict[str, float]) -> Path:
    """Materialize a calibration JSON in the format opf's
    `set_viterbi_decoder(calibration_path=...)` expects.

    Mirrors the format from chiefautism/privacy-parser's
    `ModelPIIParser._write_calibration`. The wrapping
    `{"operating_points": {"default": {"biases": {...}}}}` is opf's
    shape, not ours — see `opf/_api.py` in the privacy-filter repo.
    """
    full = {k: float(biases.get(k, 0.0)) for k in _REQUIRED_BIAS_KEYS}
    payload = {"operating_points": {"default": {"biases": full}}}
    fd, path = tempfile.mkstemp(prefix="opf_spike_calib_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return Path(path)


def _run_one(opf_instance: OPF, text: str) -> list[dict[str, Any]]:
    """Run `opf.redact(text)` and normalize the spans into plain
    dicts — easier to print and to diff between profiles than the
    frozen DetectedSpan objects."""
    result = opf_instance.redact(text)
    # `opf.redact` returns a string when output_text_only=True (we
    # don't set that), otherwise a RedactionResult with .detected_spans.
    spans: list[dict[str, Any]] = []
    for span in result.detected_spans:
        spans.append({
            "label": span.label,
            "start": span.start,
            "end":   span.end,
            "text":  span.text,
        })
    return spans


def _format_table(spans: list[dict[str, Any]], source_text: str) -> str:
    """Render spans as a fixed-width table. Spans whose source-text
    slice contains `\\n\\n` get a warning marker — those are the
    over-merges Phase 1 is here to detect."""
    if not spans:
        return "  (no matches)"
    lines = []
    for s in spans:
        slice_text = source_text[s["start"]:s["end"]]
        warn = "  [!! \\n\\n in span !!]" if "\n\n" in slice_text else ""
        # repr() prevents a multi-line span text from breaking the
        # table layout — shows escapes for any embedded newlines.
        lines.append(
            f"  [{s['start']:>5}:{s['end']:>5}]  "
            f"{s['label']:<18}  {s['text']!r}{warn}"
        )
    return "\n".join(lines)


def _run_fixture(fixture: Path) -> dict[str, list[dict[str, Any]]]:
    """Run every profile against one fixture; return a profile→spans
    mapping for later summary comparison."""
    print()
    print("=" * 78)
    print(f"Fixture: {fixture}")
    print("=" * 78)
    if not fixture.exists():
        print("  (fixture missing — skipping)")
        return {}
    text = fixture.read_text(encoding="utf-8")

    results: dict[str, list[dict[str, Any]]] = {}
    for profile_name, biases in _PROFILES.items():
        print()
        print(f"--- profile: {profile_name} ---")
        if biases:
            print(f"    biases: {biases}")
        # Construct a fresh OPF per profile so the calibration is
        # cleanly applied (or absent for `default`). Same model
        # weights so the cost is just the per-instance setup, not
        # a re-download.
        opf_instance = OPF(device="cpu", decode_mode="viterbi")
        if biases:
            calib_path = _write_calibration(biases)
            opf_instance.set_viterbi_decoder(calibration_path=str(calib_path))
        spans = _run_one(opf_instance, text)
        print(_format_table(spans, text))
        results[profile_name] = spans
    return results


def _summary(per_fixture: dict[Path, dict[str, list[dict[str, Any]]]]) -> None:
    """Print a per-profile, per-fixture count of (a) total spans and
    (b) spans containing `\\n\\n`. The latter is the Phase-1 success
    metric: zero on the `anti-merge` profile means the migration is
    worth doing."""
    print()
    print("=" * 78)
    print("Summary — over-merges per profile")
    print("=" * 78)
    print(f"{'fixture':<48}  {'profile':<16}  {'spans':>5}  {'with \\n\\n':>10}")
    print("-" * 86)
    for fixture, profile_results in per_fixture.items():
        text = fixture.read_text(encoding="utf-8") if fixture.exists() else ""
        for profile_name, spans in profile_results.items():
            with_paragraph_break = sum(
                1 for s in spans if "\n\n" in text[s["start"]:s["end"]]
            )
            print(
                f"{str(fixture.name):<48}  {profile_name:<16}  "
                f"{len(spans):>5}  {with_paragraph_break:>10}"
            )
    print()
    print("Decision criteria for proceeding to Phase 2 (TASKS.md):")
    print("  * `anti-merge` profile: 0 spans with \\n\\n on both fixtures")
    print("  * span counts roughly comparable to the post-fix output in")
    print("    services/privacy_filter/scripts/PROBE.md → Empirical findings")
    print("  * confidence scores look reasonable (no degraded recall)")
    print()
    print("If `anti-merge` still shows over-merges that bias tuning can't")
    print("eliminate, abandon the migration: the existing split pass + per-")
    print("label gap caps are good enough and the remaining 5-6 days of")
    print("Phase 2-6 work isn't justified.")


def main() -> int:
    print("opf Viterbi spike — comparing calibration profiles against the")
    print("PROBE.md fixtures. See TASKS.md → 'opf-based Viterbi decoding'.")
    per_fixture: dict[Path, dict[str, list[dict[str, Any]]]] = {}
    for fixture in _FIXTURES:
        per_fixture[fixture] = _run_fixture(fixture)
    _summary(per_fixture)
    return 0


if __name__ == "__main__":
    sys.exit(main())
