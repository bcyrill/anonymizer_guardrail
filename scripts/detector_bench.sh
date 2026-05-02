#!/usr/bin/env bash
# Score a guardrail's detector mix against a labelled corpus.
#
# Sister script to test-examples.sh. Where test-examples.sh asks
# "do the curl recipes still work end-to-end?", this asks
# "for THIS corpus on THIS detector mix, what fraction of expected
# entities does the guardrail catch, and how often does it falsely
# flag stuff that should be left alone?"
#
# Usage:
#   scripts/detector_bench.sh --config bundled:pentest
#   scripts/detector_bench.sh --config bundled:pentest --preset pentest
#   scripts/detector_bench.sh --config tests/corpus/legal.yaml
#
# See `scripts/detector_bench.sh --help` for the full flag list.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

# BENCH_PROG_NAME makes --help's "Usage:" line read `scripts/...sh`
# rather than `python -m tools.detector_bench`, matching how the
# operator invokes us.
exec env BENCH_PROG_NAME="scripts/detector_bench.sh" \
    python3 -m tools.detector_bench "$@"
