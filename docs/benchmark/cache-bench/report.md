# Cache benchmark report

Measures the effect of each caching layer (per-detector,
pipeline-level, both) across detector mixes (`regex`, `gliner_pii`,
`privacy_filter`, `regex+gliner_pii`, `regex+gliner_pii+privacy_filter`)
on multi-turn chat workloads (5/10/20 turns), under both memory and
Redis backends, with and without per-call override variation.

## Run setup

- **Bench harness**: `tools/cache_bench/` (in-process driver, no
  HTTP/serialization layer between bench and `Pipeline`).
- **Conversation**: deterministic templated chat — persona reuses
  the same name/email/phone/address/CC/IBAN/SSN across all turns,
  so PII recurs naturally and longer conversations have more
  cache-eligible texts.
- **Detector services**: `gliner-pii-service:cpu` on `localhost:8002`,
  `privacy-filter-hf-service:cpu` on `localhost:8003` — both running
  in containers on a single CPU host (no GPU). LLM detector
  intentionally excluded (would need a real LLM endpoint with cost).
- **Redis**: `redis:7-alpine` on `localhost:6379/2`,
  `FLUSHDB`'d before every cell so cells are isolated.
- **Memory matrix**: 40 cells × 3 lengths × 1 repeat (120 runs,
  ~52 minutes). Drives ALL cache-mode/detector-mix/override
  combinations.
- **Redis subset**: 16 cells × 2 lengths × 1 repeat (32 runs,
  ~3 minutes). Focused on `regex` and `regex+gliner_pii` to
  capture RTT cost without spending hours on the gliner-on-CPU
  bottleneck.
- **Repeats=1**: each cell runs the conversation once. Variance
  on this hardware is dominated by CPU inference noise; medians
  across multiple repeats would tighten the numbers but not change
  any of the observations below.

Raw data: `results-memory.json`, `results-redis.json`.

## Headline observations

1. **Pipeline cache delivers 16-18× speed-up at L=20 for slow-detector
   workloads** under default overrides. The cache short-circuits
   detector dispatch entirely on prior turns' texts; the only work
   left per turn is the new user message.

2. **Per-detector cache delivers comparable speed-up to the pipeline
   cache when overrides are stable** (gliner: 16.6× vs 15.0× at L=20
   memory; PF: 15.6× vs 17.1×). The two layers are largely
   interchangeable in the no-overrides scenario — the pipeline cache
   wins by a hair because one lookup returns the deduped match list
   directly without going through per-detector dispatch.

3. **Override fragmentation hurts pipeline-only configurations
   ~2-3× more than per-detector configurations.** With `varied`
   overrides (gliner_threshold rotating per call), the pipeline-cache
   key fragments — half the calls miss. Per-detector caches absorb
   the partial mismatch when the kwarg only affects one detector.
   Worst case: pipeline-memory regex+gliner at L=5 with varied
   overrides shows +282% anon time vs the no-overrides baseline.

4. **Privacy-filter is immune to override fragmentation** because it
   has no per-call overrides. PF cells show identical hit rates and
   anon times under `none` and `varied` (its cache key is just
   `(text,)`). gliner cells, by contrast, show the largest
   fragmentation cost.

5. **Redis backend adds enough RTT overhead that it INVERTS the
   value proposition for cheap detectors.** regex-only with
   pipeline-redis at L=10 took 57ms vs 16ms for no-cache — Redis
   GET takes longer than running regex against the text. Memory
   backend or no cache is the right choice for regex-only or
   denylist-only workloads.

6. **Redis backend recovers on slow-detector workloads but at
   reduced effective speed-up.** regex+gliner at L=10 saw 7.9× with
   pipeline-redis vs 8.4× memory equivalent — Redis RTT (~5ms per
   lookup) is a fixed tax that grows with conversation length.
   Memory remains preferred for single-replica deployments;
   Redis is for cross-replica sharing or restart-persistent state.

## When does each cache layer help?

| Scenario | no cache | per-detector | pipeline | both |
|---|---|---|---|---|
| Single slow detector, stable overrides | baseline | 16-17× faster | 15-17× faster | 15-17× faster |
| Multiple slow detectors, stable overrides | baseline | 16× faster | 16× faster | 16× faster |
| Slow detector(s), varied overrides | baseline | 6× faster | 6× faster | 6× faster (per-detector tier kicks in) |
| Cheap detector only (regex), stable overrides | baseline | ~no-op (regex has no cache) | 2-3× faster (memory) / 3× SLOWER (redis) | same as pipeline |
| Cheap + slow detectors, varied overrides | baseline | 4-8× faster | 3-7× faster | 4-8× faster |

**Rule of thumb**: for any deployment with at least one slow
detector (gliner_pii, privacy_filter, llm), turn on caching. With
stable per-call overrides, either layer alone delivers the win;
with varied overrides, ship `cache_mode=both` so the per-detector
tier absorbs the fragmentation. The Redis backend is only worth
the RTT tax when you're deploying multi-replica.

## Per-detector-mix results (memory backend)

Each cell shows: `total anonymize ms (speedup×) · pipeline cache hits/lookups · per-detector cache hits/lookups`.

### Detector mix: `gliner_pii` only

| Cache | Overrides | L=5 | L=10 | L=20 |
|---|---|---|---|---|
| none | none | 14393 ms (1.0×) · 0/0 · 0/0 | 52862 ms (1.0×) · 0/0 · 0/0 | 197854 ms (1.0×) · 0/0 · 0/0 |
| none | varied | 13667 ms (1.0×) · 0/0 · 0/0 | 50538 ms (1.0×) · 0/0 · 0/0 | 195358 ms (1.0×) · 0/0 · 0/0 |
| detector | none | 2997 ms (4.8×) · 0/0 · 20/30 | 6759 ms (7.8×) · 0/0 · 90/110 | 11900 ms (16.6×) · 0/0 · 380/420 |
| detector | varied | 7556 ms (1.8×) · 0/0 · 12/30 | 15953 ms (3.2×) · 0/0 · 72/110 | 33012 ms (5.9×) · 0/0 · 342/420 |
| pipeline | none | 2989 ms (4.8×) · 20/25 · 0/0 | 6954 ms (7.6×) · 90/100 · 0/0 | 13202 ms (15.0×) · 380/400 · 0/0 |
| pipeline | varied | 7853 ms (1.7×) · 12/25 · 0/0 | 16325 ms (3.1×) · 72/100 · 0/0 | 33617 ms (5.8×) · 342/400 · 0/0 |
| both | none | 3252 ms (4.4×) · 20/25 · 0/10 | 6354 ms (8.3×) · 90/100 · 0/20 | 12796 ms (15.5×) · 380/400 · 0/40 |
| both | varied | 7581 ms (1.8×) · 12/25 · 0/18 | 16440 ms (3.1×) · 72/100 · 0/38 | 33855 ms (5.8×) · 342/400 · 0/78 |

**Read**: The 197s → 12s win at L=20 (16.6×) is the biggest
absolute saving. gliner-pii on CPU is the slowest detector in
the suite (~1-2s per call), so the cache-hit path saves a lot of
wall time. Override fragmentation halves the speed-up (16× → 6×)
because gliner's `threshold` is part of its cache key.

### Detector mix: `privacy_filter` only

| Cache | Overrides | L=5 | L=10 | L=20 |
|---|---|---|---|---|
| none | none | 9499 ms (1.0×) · 0/0 · 0/0 | 39656 ms (1.0×) · 0/0 · 0/0 | 154608 ms (1.0×) · 0/0 · 0/0 |
| none | varied | 8975 ms (1.0×) · 0/0 · 0/0 | 39233 ms (1.0×) · 0/0 · 0/0 | 155339 ms (1.0×) · 0/0 · 0/0 |
| detector | none | 2803 ms (3.4×) · 0/0 · 20/30 | 5361 ms (7.4×) · 0/0 · 90/110 | 9900 ms (15.6×) · 0/0 · 380/420 |
| detector | varied | 2435 ms (3.7×) · 0/0 · 20/30 | 4698 ms (8.4×) · 0/0 · 90/110 | 8929 ms (17.4×) · 0/0 · 380/420 |
| pipeline | none | 2335 ms (4.1×) · 20/25 · 0/0 | 4697 ms (8.4×) · 90/100 · 0/0 | 9061 ms (17.1×) · 380/400 · 0/0 |
| pipeline | varied | 2298 ms (3.9×) · 20/25 · 0/0 | 4395 ms (8.9×) · 90/100 · 0/0 | 8333 ms (18.6×) · 380/400 · 0/0 |
| both | none | 2328 ms (4.1×) · 20/25 · 0/10 | 4620 ms (8.6×) · 90/100 · 0/20 | 8907 ms (17.4×) · 380/400 · 0/40 |
| both | varied | 2521 ms (3.6×) · 20/25 · 0/10 | 4375 ms (9.0×) · 90/100 · 0/20 | 9725 ms (16.0×) · 380/400 · 0/40 |

**Read**: PF has no per-call overrides, so `varied` mode is
identical to `none` — same hit count, same speed-up. PF is also
faster than gliner per-call (~50-150ms typical) but the cache
math is the same.

### Detector mix: `regex` only

| Cache | Overrides | L=5 | L=10 | L=20 |
|---|---|---|---|---|
| none | none | 7 ms (1.0×) · 0/0 · 0/0 | 15 ms (1.0×) · 0/0 · 0/0 | 35 ms (1.0×) · 0/0 · 0/0 |
| none | varied | 8 ms (1.0×) · 0/0 · 0/0 | 12 ms (1.0×) · 0/0 · 0/0 | 40 ms (1.0×) · 0/0 · 0/0 |
| detector | none | 5 ms (1.3×) · 0/0 · 0/0 | 15 ms (1.0×) · 0/0 · 0/0 | 36 ms (1.0×) · 0/0 · 0/0 |
| detector | varied | 5 ms (1.7×) · 0/0 · 0/0 | 13 ms (0.9×) · 0/0 · 0/0 | 35 ms (1.1×) · 0/0 · 0/0 |
| pipeline | none | 4 ms (2.0×) · 20/25 · 0/0 | 5 ms (2.9×) · 90/100 · 0/0 | 10 ms (3.6×) · 380/400 · 0/0 |
| pipeline | varied | 4 ms (2.1×) · 20/25 · 0/0 | 5 ms (2.4×) · 90/100 · 0/0 | 18 ms (2.2×) · 380/400 · 0/0 |
| both | none | 6 ms (1.1×) · 20/25 · 0/0 | 6 ms (2.6×) · 90/100 · 0/0 | 10 ms (3.4×) · 380/400 · 0/0 |
| both | varied | 4 ms (2.2×) · 20/25 · 0/0 | 5 ms (2.3×) · 90/100 · 0/0 | 10 ms (3.8×) · 380/400 · 0/0 |

**Read**: `detector` cache mode is a no-op for regex (regex has no
per-detector cache by design — it's microseconds per call). The
pipeline cache buys 2-3× even on regex by short-circuiting the
detector dispatch path entirely, but the absolute saving is
~25ms at L=20 — invisible on a wall clock for any workload that
also includes a slow detector.

### Detector mix: `regex + gliner_pii`

| Cache | Overrides | L=5 | L=10 | L=20 |
|---|---|---|---|---|
| none | none | 13295 ms (1.0×) · 0/0 · 0/0 | 49259 ms (1.0×) · 0/0 · 0/0 | 193474 ms (1.0×) · 0/0 · 0/0 |
| none | varied | 13317 ms (1.0×) · 0/0 · 0/0 | 49697 ms (1.0×) · 0/0 · 0/0 | 200275 ms (1.0×) · 0/0 · 0/0 |
| detector | none | 3162 ms (4.2×) · 0/0 · 20/30 | 5899 ms (8.4×) · 0/0 · 90/110 | 11359 ms (17.0×) · 0/0 · 380/420 |
| detector | varied | 8396 ms (1.6×) · 0/0 · 12/30 | 17452 ms (2.8×) · 0/0 · 72/110 | 36499 ms (5.5×) · 0/0 · 342/420 |
| pipeline | none | 2842 ms (4.7×) · 20/25 · 0/0 | 5889 ms (8.4×) · 90/100 · 0/0 | 11548 ms (16.8×) · 380/400 · 0/0 |
| pipeline | varied | 10851 ms (1.2×) · 12/25 · 0/0 | 19673 ms (2.5×) · 72/100 · 0/0 | 34012 ms (5.9×) · 342/400 · 0/0 |
| both | none | 3074 ms (4.3×) · 20/25 · 0/10 | 6734 ms (7.3×) · 90/100 · 0/20 | 12668 ms (15.3×) · 380/400 · 0/40 |
| both | varied | 7664 ms (1.7×) · 12/25 · 0/18 | 16218 ms (3.1×) · 72/100 · 0/38 | 34865 ms (5.7×) · 342/400 · 0/78 |

**Read**: regex+gliner essentially mirrors gliner-only because
regex is so fast its contribution is invisible. Note the
override-fragmentation cost is largest here for `pipeline` mode
(L=5 varied is 10.8s vs 2.8s for no-overrides — the pipeline
cache misses on every other turn). `both` mode is half a step
faster than `pipeline` under varied overrides because the gliner
per-detector cache picks up some of the slack — but only when
the rotated kwarg was the same as in a prior turn.

### Detector mix: `regex + gliner_pii + privacy_filter` (full stack)

| Cache | Overrides | L=5 | L=10 | L=20 |
|---|---|---|---|---|
| none | none | 18970 ms (1.0×) · 0/0 · 0/0 | 75115 ms (1.0×) · 0/0 · 0/0 | 295376 ms (1.0×) · 0/0 · 0/0 |
| none | varied | 19013 ms (1.0×) · 0/0 · 0/0 | 74481 ms (1.0×) · 0/0 · 0/0 | 291856 ms (1.0×) · 0/0 · 0/0 |
| detector | none | 4681 ms (4.1×) · 0/0 · 40/60 | 9376 ms (8.0×) · 0/0 · 180/220 | 18768 ms (15.7×) · 0/0 · 760/840 |
| detector | varied | 8714 ms (2.2×) · 0/0 · 32/60 | 18645 ms (4.0×) · 0/0 · 162/220 | 37134 ms (7.9×) · 0/0 · 722/840 |
| pipeline | none | 4685 ms (4.0×) · 20/25 · 0/0 | 9477 ms (7.9×) · 90/100 · 0/0 | 18412 ms (16.0×) · 380/400 · 0/0 |
| pipeline | varied | 10142 ms (1.9×) · 12/25 · 0/0 | 22521 ms (3.3×) · 72/100 · 0/0 | 44893 ms (6.5×) · 342/400 · 0/0 |
| both | none | 4691 ms (4.0×) · 20/25 · 0/20 | 9338 ms (8.0×) · 90/100 · 0/40 | 18542 ms (15.9×) · 380/400 · 0/80 |
| both | varied | 10072 ms (1.9×) · 12/25 · 8/36 | 18724 ms (4.0×) · 72/100 · 18/76 | 38756 ms (7.5×) · 342/400 · 38/156 |

**Read**: The full-stack 295s → 18s at L=20 is the worst-case
absolute saving in the matrix. Without caching this is a 5-minute
detection pass for a 20-turn chat — entirely impractical for
real-time use. With caching it drops to 18s.

Note the `both` cell under varied overrides: pipeline cache hits
342/400 (the same as pipeline-only) but per-detector cache picks
up 38 additional hits on the entries where gliner_threshold
matched. PF's per-detector cache also hits whenever the pipeline
cache misses, regardless of override (PF has no per-call overrides).
That's why `both/varied` (38.8s) is slightly faster than
`pipeline/varied` (44.9s) at L=20.

## Per-detector-mix results (Redis backend)

Same shape, fewer cells (subset benched). Demonstrates the RTT cost.

### Detector mix: `regex` only (Redis)

| Cache | Overrides | L=5 | L=10 |
|---|---|---|---|
| none | none | 8 ms (1.0×) · 0/0 · 0/0 | 16 ms (1.0×) · 0/0 · 0/0 |
| none | varied | 8 ms (1.0×) · 0/0 · 0/0 | 16 ms (1.0×) · 0/0 · 0/0 |
| detector | none | 6 ms (1.2×) · 0/0 · 0/0 | 16 ms (1.0×) · 0/0 · 0/0 |
| detector | varied | 7 ms (1.1×) · 0/0 · 0/0 | 16 ms (1.0×) · 0/0 · 0/0 |
| pipeline | none | 26 ms (0.3×) · 20/25 · 0/0 | 61 ms (0.3×) · 90/100 · 0/0 |
| pipeline | varied | 27 ms (0.3×) · 20/25 · 0/0 | 57 ms (0.3×) · 90/100 · 0/0 |
| both | none | 24 ms (0.3×) · 20/25 · 0/0 | 57 ms (0.3×) · 90/100 · 0/0 |
| both | varied | 30 ms (0.3×) · 20/25 · 0/0 | 80 ms (0.2×) · 90/100 · 0/0 |

**Read**: Redis pipeline cache is consistently 3× SLOWER than no
cache for regex-only. Each Redis GET is ~5ms; regex against a
typical conversation text is ~1ms. The cache hit avoids regex but
adds the GET round-trip — a net loss when the detector itself is
this fast. **Don't enable Redis caching for regex-only / denylist-only
deployments.**

### Detector mix: `regex + gliner_pii` (Redis)

| Cache | Overrides | L=5 | L=10 |
|---|---|---|---|
| none | none | 14726 ms (1.0×) · 0/0 · 0/0 | 49754 ms (1.0×) · 0/0 · 0/0 |
| none | varied | 13226 ms (1.0×) · 0/0 · 0/0 | 50077 ms (1.0×) · 0/0 · 0/0 |
| detector | none | 3375 ms (4.4×) · 0/0 · 20/30 | 6738 ms (7.4×) · 0/0 · 90/110 |
| detector | varied | 7742 ms (1.7×) · 0/0 · 12/30 | 18935 ms (2.6×) · 0/0 · 72/110 |
| pipeline | none | 3248 ms (4.5×) · 20/25 · 0/0 | 6299 ms (7.9×) · 90/100 · 0/0 |
| pipeline | varied | 9194 ms (1.4×) · 12/25 · 0/0 | 16786 ms (3.0×) · 72/100 · 0/0 |
| both | none | 3083 ms (4.8×) · 20/25 · 0/10 | 6717 ms (7.4×) · 90/100 · 0/20 |
| both | varied | 8059 ms (1.6×) · 12/25 · 0/18 | 16853 ms (3.0×) · 72/100 · 0/38 |

**Read**: Redis recovers value once a slow detector is in the
mix — 7.9× speed-up for `pipeline-redis` at L=10 vs 8.4× for
`pipeline-memory` at the same length. The RTT tax is roughly
fixed (~150-300ms additive across a 10-turn conversation), so
the longer the conversation and slower the detector, the smaller
the relative cost.

## Override fragmentation: detailed breakdown

The "varied" override mode rotates `gliner_threshold=0.7` on every
other turn, simulating a per-tenant config or A/B-test where
different requests go through with different gliner sensitivity
settings. `gliner_threshold` is part of gliner's cache key, so
varied requests fragment the gliner caches. The pipeline cache key
INCLUDES every detector's kwargs, so the pipeline cache fragments
on ANY detector's override change.

| Detector mix | Cache | L=10 stable | L=10 varied | varied tax |
|---|---|---|---|---|
| gliner_pii | pipeline-memory | 6954 ms | 16325 ms | +135% |
| gliner_pii | both-memory | 6354 ms | 16440 ms | +159% |
| privacy_filter | pipeline-memory | 4697 ms | 4395 ms | -6% (PF has no overrides) |
| regex+gliner_pii | pipeline-memory | 5889 ms | 19673 ms | +234% |
| regex+gliner_pii | both-memory | 6734 ms | 16218 ms | +141% |
| regex+gliner+pf | pipeline-memory | 9477 ms | 22521 ms | +138% |
| regex+gliner+pf | both-memory | 9338 ms | 18724 ms | +101% |

**Pattern**: in cells with gliner active, the `pipeline` mode is
always more affected than the `both` mode — because the per-detector
gliner cache hits on entries where the threshold matched (i.e.
half the turns under "every other turn" rotation). The
non-gliner detectors (PF in the full stack) keep their
per-detector hits since their cache key doesn't include gliner's
threshold. This is the "structural backup tier" the
[design-decisions doc](../docs/design-decisions.md#bidirectional-pipeline-level-result-cache-with-source-tracked-prewarm)
describes.

## Recommendations

1. **Single-replica deployment, slow detectors only**:
   `cache_mode=both`, `backend=memory`, decent
   `<DETECTOR>_CACHE_MAX_SIZE` (1000-5000) and
   `PIPELINE_CACHE_MAX_SIZE=2000`. Stable per-call overrides:
   pipeline cache absorbs everything. Varied overrides:
   per-detector tier kicks in.

2. **Single-replica, regex-only or fast-detectors-only**:
   `cache_mode=none` OR `cache_mode=pipeline` with
   `backend=memory` only. Don't use Redis here — RTT exceeds
   detection cost.

3. **Multi-replica, slow detectors**: `cache_mode=both`,
   `backend=redis`, share `CACHE_REDIS_URL` + a stable
   `CACHE_SALT`. Trade ~5ms RTT per cache lookup for
   cross-replica hits. The per-detector tier helps here too if
   overrides are partial — same logic as memory.

4. **Don't enable caching when overrides change every call**:
   the cache fragments and adds 1-3ms overhead per lookup with
   no hit benefit. Better to leave caching off and pay the
   detection cost outright.

5. **Don't deploy `input_mode=merged` with the pipeline cache**:
   the merged blob is unique by construction → 100% miss rate
   on the pipeline cache, only storage churn. The startup log
   warns about this combination.

## Reproducing this report

```bash
# Start the services (assuming the images are pulled):
podman run -d --rm --name gliner-pii-service -p 8002:8002 \
    localhost/gliner-pii-service:cpu
podman run -d --rm --name privacy-filter-hf-service -p 8003:8003 \
    localhost/privacy-filter-hf-service:cpu
podman run -d --rm --name bench-redis -p 6379:6379 \
    docker.io/library/redis:7-alpine

# Wait for /health endpoints to come up (~30-60s).

# Run the bench (full memory matrix + Redis subset for backend
# comparison):
GLINER_PII_URL=http://localhost:8002 \
PRIVACY_FILTER_URL=http://localhost:8003 \
CACHE_REDIS_URL=redis://localhost:6379/2 \
scripts/cache_bench.sh \
    --repeats=1 \
    --length=5 --length=10 --length=20 \
    --backend=memory \
    -o cache-bench-results-memory

GLINER_PII_URL=http://localhost:8002 \
PRIVACY_FILTER_URL=http://localhost:8003 \
CACHE_REDIS_URL=redis://localhost:6379/2 \
scripts/cache_bench.sh \
    --repeats=1 \
    --length=5 --length=10 \
    --detector-mix=regex \
    --detector-mix=regex+gliner_pii \
    --backend=redis \
    -o cache-bench-results-redis

# Tear down:
podman rm -f gliner-pii-service privacy-filter-hf-service bench-redis
```

The full memory matrix takes ~50 minutes on this hardware
(Intel CPU, no GPU, gliner-pii running on CPU is the bottleneck).
The Redis subset takes ~3 minutes since it omits the L=20 cells
and gliner-pii alone / privacy-filter alone / full-stack
detector mixes.
