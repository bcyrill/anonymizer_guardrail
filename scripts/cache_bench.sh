#!/usr/bin/env bash
# Run the cache effectiveness benchmark.
#
# Sister script to detector_bench.sh. Where detector_bench.sh asks
# "for THIS detector mix, what fraction of expected entities does
# the guardrail catch?", this asks "for THIS detector mix, how
# much does each cache layer (per-detector / pipeline / both) save
# on a multi-turn chat workload?".
#
# Usage:
#   scripts/cache_bench.sh                                                                # full matrix
#   scripts/cache_bench.sh --quick                                                        # fast iteration
#   scripts/cache_bench.sh --gliner-pii-url=http://localhost:8002 \
#                          --privacy-filter-url=http://localhost:8003 \
#                          --redis-url=redis://localhost:6379/2
#   scripts/cache_bench.sh --detector-mix=regex --length=30 --repeats=5
#
# The bench needs the gliner-pii-service and privacy-filter-hf-service
# containers running for cells that include those detectors. Start
# them via the launcher:
#   scripts/launcher.sh --gliner-pii-backend service \
#                       --privacy-filter-backend service \
#                       --privacy-filter-variant hf
#
# (After the launcher comes up, kill the foreground guardrail with
# ^C; the sidecars stay running.) Or `podman start <container>` if
# they're already pulled.
#
# See scripts/cache_bench.sh --help for the full flag list.
# See docs/cache-bench.md for setup details and how to read the
# generated report.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

# BENCH_PROG_NAME makes --help's "Usage:" line read `scripts/...sh`
# rather than `python -m tools.cache_bench`, matching how the operator
# invokes us.
exec env BENCH_PROG_NAME="scripts/cache_bench.sh" \
    python3 -m tools.cache_bench "$@"
