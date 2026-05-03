# Cache effectiveness benchmark

Runs the [pipeline-level cache](operations.md#pipeline-level-result-cache-with-source-tracked-prewarm)
and the [per-detector caches](operations.md#detector-result-caching)
through a realistic multi-turn chat matrix and produces a report
showing how much each cache layer actually buys you.

The bench is in-process — it constructs `Pipeline` objects directly
and exercises them, no FastAPI / HTTP layer in between. That isolates
cache behaviour from serialisation noise. The detection cost it's
measuring against (gliner-pii / privacy-filter HTTP round-trips) is
real because those detectors are real HTTP services.

## What it measures

For each cell in the matrix:

- **detector mix** — `regex`, `gliner_pii`, `privacy_filter`,
  `regex+gliner_pii`, `regex+gliner_pii+privacy_filter`.
- **cache mode** — `none`, `detector` (per-detector caches only),
  `pipeline` (pipeline cache only), `both`.
- **backend** — `memory` or `redis`.
- **conversation length** — 5, 10, 20, 30 turns.
- **overrides mode** — `none` (every call uses defaults) or `varied`
  (every other call rotates `gliner_threshold=0.7`, simulating
  per-tenant config rotation).

Per cell the bench:

1. Builds a fresh `Pipeline` with that cell's cache config.
2. Walks the conversation turn-by-turn, simulating LiteLLM's
   pre/post-call cycle (anonymize on the growing chat history,
   deanonymize on the assistant response).
3. Records per-turn timings and `pipeline.stats()` deltas.
4. Tears down (`pipeline.aclose()`) and restores the original config.

Each cell runs `--repeats` times (default 3); medians are reported.

## Setup

The bench needs services running for cells that include
`gliner_pii` or `privacy_filter`, plus a Redis instance for
`backend=redis` cells.

### Option A — auto-start via the launcher

The launcher already knows how to start the sidecars:

```bash
# Start gliner-pii + privacy-filter-hf + Redis sidecars, with the
# guardrail bound to those services.
scripts/launcher.sh \
    --gliner-pii-backend service \
    --privacy-filter-backend service \
    --privacy-filter-variant hf \
    --vault-backend redis
```

The launcher foregrounds the guardrail; sidecars stay up after
`^C`. The bench connects to the same URLs the launcher sets:

| Service | Default URL |
|---|---|
| gliner-pii-service | `http://localhost:8002` |
| privacy-filter-hf-service | `http://localhost:8003` |
| redis (vault + cache share) | the URL the launcher prints; usually `redis://localhost:6379/0` |

### Option B — bring your own services

Set the URLs via env vars or CLI flags:

```bash
GLINER_PII_URL=http://localhost:8002 \
PRIVACY_FILTER_URL=http://localhost:8003 \
CACHE_REDIS_URL=redis://localhost:6379/2 \
scripts/cache_bench.sh
```

Cells using a service that isn't reachable are **skipped** with a
clear message in the report — the bench still runs everything that
*can* run. So a regex-only run works without any sidecars at all.

## Running

```bash
# Full matrix (~25 minutes typical with 3 repeats):
scripts/cache_bench.sh

# Fast iteration during development:
scripts/cache_bench.sh --quick

# Subset of the matrix:
scripts/cache_bench.sh \
    --detector-mix=regex \
    --detector-mix=regex+gliner_pii \
    --length=10 \
    --length=30

# Single cache config:
scripts/cache_bench.sh --cache-mode=both --backend=memory

# Skip override-fragmentation runs:
scripts/cache_bench.sh --overrides-mode=none

scripts/cache_bench.sh --help    # full flag list
```

Output lands in `cache-bench-results/` by default:

- `report.md` — human-readable summary with per-detector-mix tables.
- `results.json` — every per-turn observation, for re-aggregation.

Override the path with `-o` / `--output-dir`.

The default output dir is gitignored so ad-hoc bench runs don't
clutter the working tree. The canonical, committed results live at
[`docs/benchmark/cache-bench/`](benchmark/cache-bench/report.md) —
operators reproducing the report can re-run the bench and overwrite
that path explicitly via `-o docs/benchmark/cache-bench/` if they
want the committed numbers refreshed.

## Reading the report

The headline view is one markdown table per detector mix. Rows are
cache config × override mode; columns are conversation lengths.
Each cell shows:

```
{anon_ms}  ({speedup}×)  ·  pipe {hits}/{total}  ·  det {hits}/{total}
```

- **`anon_ms`** — median total `anonymize` wall time across all
  turns of one conversation. Lower is better.
- **`speedup`** — relative to the no-cache baseline at the same
  length + override mode.
- **`pipe hits/total`** — pipeline-cache hits over total
  pipeline-cache lookups. A hit means no detectors ran.
- **`det hits/total`** — per-detector cache hits summed across
  active cache-using detectors and all turns. A per-detector hit
  short-circuits one detector's dispatch but the others still run.

A "Summary observations" section at the top of the report calls
out the largest speed-up, the override-fragmentation cost, and the
memory-vs-redis overhead, so operators don't have to scan the
tables to see the headline numbers.

## What the matrix doesn't cover

- **LLM detection.** Skipped because every run would hit a real
  LLM endpoint, costing money and adding noise from upstream
  latency variance. Behaviour mirrors privacy-filter (HTTP detector
  with cache key `(text, model, prompt_name)`); operators can
  extend the harness if they want to bench LLM specifically.
- **Concurrent requests.** The bench drives one conversation at a
  time. The cache's behaviour under concurrent traffic
  (race-on-cold-key, contention on the surrogate-cache lock) is
  documented in the relevant module docstrings but not measured
  here.
- **Failure modes.** Service-down + cache-degraded paths aren't
  measured — service health is checked at startup and unreachable
  cells are skipped.

## Extending

- **More scenarios**: `tools/cache_bench/payloads.py` —
  add a persona + 30 templated user/assistant message pairs.
- **More cache modes**: `harness.BenchCell` — add the new cache
  shape to the dataclass and `_build_pipeline_config` /
  `_build_detector_configs`.
- **More detector mixes**: `harness._DETECTOR_COMBOS` — add the
  tuple. The skip-check handles missing services automatically.
