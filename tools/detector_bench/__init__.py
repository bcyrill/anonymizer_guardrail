"""Detector quality benchmark.

Sends a labelled corpus of texts through the guardrail and scores how
well the configured detectors redact what's labelled as sensitive.
Operator-facing question this answers: "Is detector X / detector mix Y
worth turning on for my workload?"

Lives outside `src/anonymizer_guardrail/` so it stays out of the
production wheel — same scoping as `tools/launcher/`. Operators install
via `pip install -e ".[dev]"`.

Entry point: `python -m tools.detector_bench --config <path>`. Bash
wrapper at `scripts/benchmark.sh` mirrors the launcher
wrappers' shape.
"""
