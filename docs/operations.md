# Operations: observability

The guardrail is operated as a stateless HTTP service with four
opt-in stores it manages internally:

| Store | Where | When does it fire | Default |
|---|---|---|---|
| **Vault** | per-call mapping (surrogate → original + type + sources + kwargs) | Written on `request`; popped on the matching `response` | always on (memory backend; flip to redis for multi-replica) |
| **Surrogate cache** | process-wide `original → surrogate` map | Hit on every entity the surrogate generator touches; backs cross-request consistency | always on (LRU cap of 100k) |
| **Pipeline-level result cache** | deduped match list keyed by `(text, detector_mode, kwargs)` | Wraps detector dispatch; hit skips ALL detectors. Also dual-written by deanonymize-side prewarm | OFF (`PIPELINE_CACHE_BACKEND=none`) |
| **Per-detector result cache** | one cache per cache-using detector keyed by `(text, that-detector's kwargs)` | Hit inside `det.detect(text)`; backup tier when the pipeline cache misses on partial-kwargs change | OFF (`<DETECTOR>_CACHE_MAX_SIZE=0`) |

This page covers the cross-cutting `/health` endpoint and how to
read it; each store's own lifecycle and sizing knobs live with its
dedicated docs.

For deeper background:

- **[Vault](vault.md)** — round-trip mapping, TTL, size cap.
- **[Surrogate cache](surrogates.md#surrogate-cache)** — cross-call
  consistency, LRU eviction.
- **[Pipeline-level result cache](#pipeline-level-result-cache-with-source-tracked-prewarm)**
  (this page, below) — the new layer that absorbs full-text
  repeats; deanonymize-side prewarm closes the assistant-message
  miss.
- **[Per-detector result caching](#detector-result-caching)** (this
  page, below) — one cache per slow detector; structural backup
  tier under override fragmentation.
- **[Limitations](limitations.md)** — single-replica constraint,
  streaming, restart behaviour.

## Observability

`/health` returns live counters for both stores plus per-detector
concurrency and cache gauges so operators can monitor pressure or
leak without inspecting the process. Keys are static across
detector modes — `pf_*`, `gliner_pii_*`, and `llm_*` are always
present, regardless of which detectors are active under
`DETECTOR_MODE`. That lets dashboards pin to stable names without
needing a per-deployment template:

```json
{
  "status": "ok",
  "detector_mode": "regex,llm",
  "vault_size": 3,
  "surrogate_cache_size": 1421,
  "surrogate_cache_max": 100000,
  "pipeline_cache_backend": "none",
  "pipeline_cache_size": 0,
  "pipeline_cache_max": 0,
  "pipeline_cache_hits": 0,
  "pipeline_cache_misses": 0,
  "pf_in_flight": 0,
  "pf_max_concurrency": 10,
  "gliner_pii_in_flight": 0,
  "gliner_pii_max_concurrency": 10,
  "llm_in_flight": 0,
  "llm_max_concurrency": 10,
  "pf_cache_size": 0,
  "pf_cache_max": 0,
  "pf_cache_hits": 0,
  "pf_cache_misses": 0,
  "gliner_pii_cache_size": 0,
  "gliner_pii_cache_max": 0,
  "gliner_pii_cache_hits": 0,
  "gliner_pii_cache_misses": 0,
  "llm_cache_size": 0,
  "llm_cache_max": 0,
  "llm_cache_hits": 0,
  "llm_cache_misses": 0
}
```

How to read each counter:

- **`vault_size`** — number of *open* round-trips. A steady-state value
  near zero is healthy; a monotonically growing value points at
  LiteLLM losing the response side, which the TTL eventually catches
  up with. Full lifecycle in [vault](vault.md). Backend-specific:
  `MemoryVault` returns the exact in-process count; the Redis backend
  returns `0` as a placeholder — operators query Redis directly for
  the actual depth (`DBSIZE` on a dedicated DB index, or `redis-cli
  --scan --pattern "vault:*" | wc -l`). See
  [vault → Backends](vault.md#backends) for the multi-replica
  deployment path.
- **`surrogate_cache_size`** vs **`surrogate_cache_max`** — when these
  converge, LRU eviction is firing and the oldest cross-request
  consistency invariants are quietly being lost. Bump
  `SURROGATE_CACHE_MAX_SIZE` if that matters for your traffic shape.
  Full lifecycle in [surrogate cache](surrogates.md#surrogate-cache).
- **`llm_in_flight`** / **`pf_in_flight`** (and matching
  `*_max_concurrency`) — see
  [configuration → Capping detector concurrency](configuration.md#capping-detector-concurrency).
  Counters close to their cap for sustained periods point at the
  bottleneck detector — either the upstream service is slow, or the
  cap needs to come up. The counters are actual in-flight counts, not
  semaphore queue depth, so they top out at the matching cap.
- **`<prefix>_cache_size`** / **`<prefix>_cache_max`** /
  **`<prefix>_cache_hits`** / **`<prefix>_cache_misses`** — per-detector
  result-cache counters, emitted for every detector that opted into
  caching (today: `llm_*`, `pf_*`, `gliner_pii_*`). All four keys are
  present even when caching is disabled (`*_cache_max=0`), so dashboards
  can pin to stable names. See
  [Detector result caching](#detector-result-caching) below.
- **`pipeline_cache_backend`** / **`pipeline_cache_size`** /
  **`pipeline_cache_max`** / **`pipeline_cache_hits`** /
  **`pipeline_cache_misses`** — the pipeline-level cache that wraps
  detector dispatch. `backend` reflects the configured value
  (`none` / `memory` / `redis`); the four counters are 0 when
  `backend=none`. `size`/`max` follow the same backend asymmetry as
  the per-detector caches (memory: actual count vs LRU cap; redis: 0
  placeholder, query Redis directly). See
  [Pipeline-level result cache](#pipeline-level-result-cache-with-source-tracked-prewarm)
  below.

When extra detectors with semaphores are configured (e.g. `gliner_pii`),
their `<stats_prefix>_in_flight` and `<stats_prefix>_max_concurrency`
keys appear in `/health` automatically — see
[Adding a new detector](development.md#adding-a-new-detector).

`/health` exposes point-in-time gauges only. Time-series questions
— request rate, p95 detector latency, hourly entity counts by type,
saturation events over time — need a Prometheus-style `/metrics`
endpoint, which is tracked in
[`TASKS.md` → Prometheus-style /metrics endpoint](../TASKS.md#prometheus-style-metrics-endpoint).

## Detector result caching

LiteLLM forwards the *full* conversation history in `texts` on every
turn — see [examples → Multi-text batch](examples.md#multi-text-batch).
For long chats this means every slow detector re-classifies every
prior turn from scratch on each call. The detector result cache is an
opt-in store keyed by input text plus the overrides that affect that
detector's output, letting repeat calls skip the slow detection work
entirely. Two backends ship: a process-local LRU (default,
zero-dependency) and a Redis-backed shared store (opt-in, suitable
for multi-replica deployments and cache survival across guardrail
restarts).

### Configuration

Three detectors opt into caching. Each carries its own backend
selector and per-backend knobs; defaults are off and process-local.
The fast detectors (`regex`, `denylist`) are intentionally not on
this list — they're microseconds per call against pre-built data
structures, so caching would add overhead for negligible savings.

#### Backend selection

| Variable | Default | Notes |
|---|---|---|
| `LLM_CACHE_BACKEND` | `memory` | `memory` (process-local LRU) or `redis` (shared store). |
| `PRIVACY_FILTER_CACHE_BACKEND` | `memory` | Same shape as `LLM_CACHE_BACKEND`. |
| `GLINER_PII_CACHE_BACKEND` | `memory` | Same shape as `LLM_CACHE_BACKEND`. |

**Bounds asymmetry:** the memory backend caps by *entry count* via
`*_MAX_SIZE` (LRU eviction); the redis backend caps by *wall-clock
TTL* via `*_CACHE_TTL_S` plus Redis-side `maxmemory` policy. Each
backend's knob is silently ignored on the other — operators should
tune whichever pair their backend uses and leave the other at default.

#### Memory-backend knobs

LRU cap for the in-process cache. `0` disables the cache entirely
(detector calls go straight to the wire — same behaviour as before
caching shipped). Eviction fires once size grows past the cap.

| Variable | Default | Notes |
|---|---|---|
| `LLM_CACHE_MAX_SIZE` | `0` | LRU cap. Memory backend only. |
| `PRIVACY_FILTER_CACHE_MAX_SIZE` | `0` | LRU cap. Memory backend only. |
| `GLINER_PII_CACHE_MAX_SIZE` | `0` | LRU cap. Memory backend only. |

#### Redis-backend knobs

Per-detector TTL plus the shared connection URL and salt. The
`*_MAX_SIZE` knobs above are ignored when the backend is `redis` —
Redis-side `maxmemory` policy bounds the global footprint.

| Variable | Default | Notes |
|---|---|---|
| `CACHE_REDIS_URL` | *(empty)* | Connection URL shared across every detector that selects `cache_backend=redis`. Required when any detector picks redis. To shard, use the existing `/<n>` logical-DB suffix per detector. |
| `CACHE_SALT` | *(empty → random per process)* | Keying material for the BLAKE2b digest used in Redis cache keys. Empty → fresh 16 random bytes at process start (logged once). Set to a fixed value for multi-replica cache hits or restart-persistent caching. Distinct from `SURROGATE_SALT`. |
| `LLM_CACHE_TTL_S` | `600` | Per-key TTL via `EXPIRE`, reset on every cache write. Redis backend only. |
| `PRIVACY_FILTER_CACHE_TTL_S` | `600` | Same shape as `LLM_CACHE_TTL_S`. |
| `GLINER_PII_CACHE_TTL_S` | `600` | Same shape as `LLM_CACHE_TTL_S`. |

Each detector occupies its own Redis namespace (`cache:llm:<digest>`,
`cache:privacy_filter:<digest>`, `cache:gliner_pii:<digest>`) so they
can share a single Redis instance without colliding.

The `cache-redis` extra (`pip install ".[cache-redis]"`) ships the
`redis-py` client. The default container image bundles it
unconditionally — the client is ~3 MB of pure Python and lazy-imported,
so memory-backend deployments pay nothing at runtime. Direct-pip-install
operators selecting `*_CACHE_BACKEND=redis` without the extra fail
loud at boot with a remediation message.

The cache key per detector is the input text plus the overrides
that affect that detector's output:

| Detector | Cache key |
|---|---|
| LLM | `(text, llm_model, llm_prompt)` |
| Privacy filter | `(text,)` |
| GLiNER-PII | `(text, labels_tuple, threshold)` |

Per-call overrides resolved to the same effective value share a
slot (`prompt_name=None` and `prompt_name="default"` collapse;
`labels=None` and `labels=<configured default>` collapse). The
forwarded `api_key` is deliberately *not* in the LLM cache key —
same input + prompt + model produces the same detection regardless
of which user authenticated. For gliner, label *order* IS part of
the key — different orderings are treated as different calls
because the wire request preserves order.

### Pipeline-level result cache (with source-tracked prewarm)

| Variable | Default | Notes |
|---|---|---|
| `PIPELINE_CACHE_BACKEND` | `none` | One of `none`, `memory`, `redis`. `none` (the default) is off — every anonymize runs detection. `memory` caches deduped match lists in-process; `redis` routes to a shared `RedisPipelineCache` for multi-replica deployments and restart-persistence (uses `CACHE_REDIS_URL` + `CACHE_SALT`, same as the per-detector caches). |
| `PIPELINE_CACHE_MAX_SIZE` | `0` | LRU cap, memory backend only. 0 with `backend=memory` disables caching at runtime. |
| `PIPELINE_CACHE_TTL_S` | `600` | Per-key TTL, redis backend only. Reset on every cache write — frequently-hit keys stay warm. |

When enabled, the pipeline cache sits ABOVE the per-detector caches
and stores the *deduped* match list keyed by `(text, detector_mode,
frozen_per_call_kwargs)`. Two write paths populate it:

- **Detection-side write**: after detection runs, the deduped result
  lands in the pipeline cache. The next request for the same text +
  same kwargs hits in one shot — no detector dispatch.
- **Deanonymize-side prewarm**: `pipeline.deanonymize` synthesises
  matches from the typed vault entry and writes them into both the
  pipeline cache (for the deanonymized text) AND each per-detector
  cache (filtered by the entry's `source_detectors` per surrogate).
  See [design-decisions → Bidirectional pipeline-level result cache](design-decisions.md#bidirectional-pipeline-level-result-cache-with-source-tracked-prewarm)
  for why filtering by `source_detectors` matters (without it, every
  per-detector cache stat would inflate with entities other detectors
  found).

Closes the assistant-message-on-turn-N+1 case: today the cache
hits on every replayed prior user message but always misses on
the most recent assistant reply (which was never put through
detection — only deanonymize). Prewarm closes that half, dropping
steady-state cost from "two detection calls per round-trip" to
"one new call per round-trip".

Compatible with `USE_FAKER=true` — the structured vault entry
carries `entity_type` per surrogate, so prewarm doesn't need to
recover types from surrogate prefixes. Per-detector cache stats
stay meaningful: `source_detectors` filters per-detector writes so
each slot only holds what THAT detector actually produced (not the
deduped union).

### When to enable

Worth turning on when you serve multi-turn conversations and the LLM
detector's per-call latency is the dominant cost. With caching off,
turn N pays the LLM round-trip for every replayed prior message; with
caching on, only newly-introduced text incurs the round-trip.

Skip it for:

- **Single-turn workloads** — there's nothing to reuse.
- **Deployments that want fresh detection on every call** — caching
  freezes a detector's verdict on a given input until LRU eviction
  (the "Trade-off" subsection below explains why this is usually a
  feature for chat). For one-shot scans where every call should run
  full detection independently, leave the cache disabled. Note this
  is distinct from *surrogate* stability across calls, which is
  governed by [`SURROGATE_SALT`](surrogates.md#surrogate-salt-privacy-hardening),
  not by the detector cache.

### Trade-off: stability vs. fresh detection

Caching the LLM detector freezes whatever it returned on first sight.
If the model missed a borderline entity on turn 1, every subsequent
turn that replays that text gets the same miss, until LRU eviction.
That's usually an upgrade for chat workloads (turn-to-turn
disagreement is worse than a stable miss), but it is a behaviour
change worth flagging — operators who want fresh detection on every
call should leave the cache disabled.

### Sizing alongside the surrogate cache

Pair `LLM_CACHE_MAX_SIZE` with `SURROGATE_CACHE_MAX_SIZE`. A detector
cache hit that finds a surrogate-cache *miss* re-derives the surrogate
deterministically — *usually* the same value, but the
[collision-retry path](surrogates.md#surrogate-cache) can flip it. To
keep cross-turn surrogate consistency intact, keep the surrogate cache
comfortably above the conversation's expected unique-entity count.
`/health` exposes both so saturation is visible.

### Restart behaviour

The memory backend is process-local — after a restart the cache is
cold and the first occurrence of each input pays the full detection
cost again. There's no on-disk persistence; for multi-replica or
restart-persistent caching, switch to the redis backend.

The redis backend keeps entries server-side under per-key `EXPIRE`,
so they survive a guardrail restart up to the configured TTL. To
share cache hits across replicas, set `CACHE_SALT` to a fixed value
on every replica — otherwise each replica generates a different
random salt at boot and computes a different digest for the same
input.

### Redis backend: why the cache key is salted

The cache key in Redis is

```
cache:<detector>:<BLAKE2b-keyed digest of the cache key tuple>
```

where the cache-key tuple includes the raw input text — `(text,)` for
PF, `(text, llm_model, llm_prompt)` for LLM, `(text, labels, threshold)`
for gliner. The BLAKE2b digest is keyed with `CACHE_SALT`.

Without the salt, anyone with `SCAN`/`KEYS` access to Redis could mount
a **confirmation attack**:

1. Pick a candidate input X (e.g. a particular SSN, email, customer name).
2. Compute `BLAKE2b(repr((X, …)))` locally — same code path, public hash.
3. Check Redis: if the key exists, X was processed by the guardrail
   within the TTL window.

Low-entropy inputs make this cheap. A nine-digit SSN is 10⁹ candidates;
an email check against a 10M-name list is 10⁷. With the salt, computing
candidate digests requires knowing the salt — the salt is server-side
state never sent to Redis, so the attacker hits a brick wall even with
full read-access on the keyspace.

Cache *values* (`[{"text": "alice@example.com", "type": "EMAIL_ADDRESS"}, …]`)
also leak content, of course — this is mostly defence-in-depth against
asymmetric Redis access shapes that surface keys but not values:

- Sysadmins running `redis-cli --scan` to count keys for diagnostics.
- Metrics agents aggregating by key prefix.
- Compromised replicas with Redis ACL `+@keyspace -@string` (read keys,
  not values).
- Backup snapshots that retain key metadata after value retention has
  expired.

Salting the key adds one env var of operational complexity and closes
the confirmation oracle under all of those shapes for free.

**The vault is structurally different and does not need a salt.** Its
Redis key is `vault:<call_id>` where `call_id` is the `litellm_call_id`
LiteLLM mints for the request — a random opaque token with no
correlation to user content. Knowing `vault:9f2c-…` exists tells an
attacker nothing they didn't already know (LiteLLM's own logs already
carry the call_id). Hashing or salting it would only obscure
`redis-cli GET vault:<call_id>` — exactly the lookup an oncall wants
during a vault incident — without buying any privacy.

The rule the codebase follows: **hash + salt any Redis key derived
from sensitive content; pass through any Redis key that's already a
random opaque token from elsewhere in the system.**

### Redis backend: failure handling

If Redis becomes unreachable, the cache becomes a passthrough:
`get_or_compute` calls through to the detector, nothing is cached,
and a warning is logged at most once per minute per detector
namespace. The detector itself keeps serving requests — this is a
deliberate departure from the vault's stricter policy
(see [vault → Backends](vault.md#backends)) because the cache is an
*efficiency* mechanism (lost cache hits ⇒ slower detection), not a
*correctness* mechanism (lost mappings ⇒ unredacted responses). The
two share `redis-py` but ship as independent extras (`vault-redis`
and `cache-redis`) and take separate URLs (`VAULT_REDIS_URL`,
`CACHE_REDIS_URL`) so operators can mix-and-match — e.g. memory
vault with redis cache, or redis vault with memory cache.

The `enabled` field on `<prefix>_cache_backend` stays `redis` even
when Redis is unreachable — the passthrough is internal to
`get_or_compute`, not a backend swap. Operators reading `/health` see
the *configured* backend, not the *current* state. Watch the WARNING
log line `Redis cache GET failed for namespace=…` (or `SET failed`)
as the canonical signal that the cache is degraded.

**What an operator does when Redis is down:**

1. Grep guardrail logs for `"Redis cache "` to confirm the cache layer
   has been seeing failures (rate-limited to one per minute per
   namespace, so absence of recent lines doesn't prove health —
   tail the active log too).
2. Verify the URL works from the guardrail container:
   `redis-cli -u $CACHE_REDIS_URL ping` should return `PONG`.
3. No guardrail restart needed. The cache reconnects per-call via
   redis-py's connection pool; once Redis is reachable again the
   warning stops firing and hits resume on the next request.

### Redis backend: diagnosing low hit rate

`<prefix>_cache_hits` / `<prefix>_cache_misses` on `/health` give
per-replica counters. Hit-rate well below expectations on a
multi-turn workload usually means one of:

- **Different replicas are computing different cache-key digests.**
  Each replica logs `CACHE_SALT unset — generated a random 16-byte
  salt for this process` once at INFO when the operator-facing
  `CACHE_SALT` is empty. If you see that line in any replica's logs,
  cross-replica cache hits are impossible: each process has a
  different salt → different digest → no shared keys. Set
  `CACHE_SALT` to a fixed value on every replica.
- **Override fragmentation.** The cache key includes the per-call
  overrides that affect output (`(text, llm_model, llm_prompt)` for
  LLM, `(text, labels, threshold)` for gliner, `(text,)` for PF). If
  callers pass different `additional_provider_specific_params` per
  request, each variant occupies its own slot. Look at LiteLLM
  request-side instrumentation to see whether `llm_model` /
  `llm_prompt` / `labels` are stable across the calls you'd expect
  to be cached.
- **Caching a high-cardinality detector.** Per-call salt-rotation,
  unique IDs in the input text, etc. all defeat caching. If the
  workload is structurally low-repetition, the cache won't help —
  switch back to `<DETECTOR>_CACHE_BACKEND=memory` +
  `<DETECTOR>_CACHE_MAX_SIZE=0` to skip the Redis writes.

### Redis backend: flushing one detector's cache

After a label change (gliner), prompt rotation (LLM), or any other
operator action that should invalidate cached verdicts:

```bash
# Approximate; non-blocking. Use this in production.
redis-cli -u $CACHE_REDIS_URL --scan --pattern 'cache:gliner_pii:*' \
    | xargs redis-cli -u $CACHE_REDIS_URL del

# When the namespace owns its own logical DB index — instant.
redis-cli -u $CACHE_REDIS_URL -n 2 FLUSHDB
```

Replace `gliner_pii` with `llm` or `privacy_filter` for the matching
detector. Flushing is safe at runtime — affected detectors will see
fresh misses and recompute from the current model / prompt.

### Redis backend: stats asymmetry

For the memory backend `/health` reports `<prefix>_cache_size` (live
length) and `<prefix>_cache_max` (the LRU cap). For the redis
backend both fields report `0` by design — `SCAN MATCH cache:<ns>:*`
is async; `stats()` is sync. Operators query Redis directly when
they need the actual count:

```bash
# Approximate count — non-blocking.
redis-cli SCAN 0 MATCH 'cache:llm:*' COUNT 1000

# Exact count when the namespace owns its own logical DB.
redis-cli -n 2 DBSIZE
```

`<prefix>_cache_hits` and `<prefix>_cache_misses` remain
process-local on both backends — they reflect this replica's
traffic, not cluster-wide totals.

## Surrogates persist in responses (`DEANONYMIZE_SUBSTITUTE=false`)

By default the guardrail's `deanonymize` step substitutes
surrogates back to originals so the calling client receives the
real values in the model's response. Set
`DEANONYMIZE_SUBSTITUTE=false` to skip the substitution — the
client receives surrogate-laden output instead.

### What this is for

Redaction-only pipelines where round-trip restoration is the wrong
thing to do:

- Telemetry / log scrubbing — anonymize before writing, never restore.
- Training-data sanitization — feed the model surrogate-laden text;
  the output should also be surrogate-laden so downstream training
  doesn't memorise originals.
- Compliance pipelines that strip PII for auditing — restoration
  would defeat the audit trail.

### What it changes

- `pipeline.deanonymize` calls `vault.pop` and runs `_prewarm_caches`
  exactly as before, then returns `texts` unchanged (skipping the
  substring substitution).
- The cache prewarm seeds the pipeline cache + per-detector caches
  for the **surrogate-laden** response text. That's the right key
  shape: the client receives the surrogate-laden form, includes it
  in the next turn's chat history, and the future anonymize call
  on that same text gets a cache hit (with empty matches — no
  re-anonymization of already-anonymized text).
- Log lines on deanonymize calls carry a `substitute=skip` marker
  so log shippers can grep for substitute-off requests.

### What it does NOT change

- **Vault** still works as before. Operators who want to save
  vault overhead choose `VAULT_BACKEND=memory` (default;
  sub-microsecond) or accept the Redis cost. The vault is needed
  to drive the prewarm; no separate flag disables it.
- **Per-detector caches** still work as before — the prewarm
  writes them, future anonymize lookups hit them.
- **Pipeline cache** still works as before.
- **Surrogate consistency across turns** is preserved — same
  original PII produces the same surrogate regardless of the
  flag. The surrogate cache (process-wide
  `original → surrogate` map) is independent of the deanonymize
  path.
- **Detection on response-side text**: the deanonymize step does
  NOT run detection (and never has, regardless of this flag). If
  the upstream LLM emits NEW PII not present in the original
  prompt — e.g. via retrieval-augmented generation surfacing
  customer records — that PII passes through unchanged. The
  guardrail does NOT redact model output. See
  [design-decisions](design-decisions.md) for the rationale.

### What clients receiving surrogate-laden responses must handle

A chat UI rendering the model's response will show literal
surrogate strings (e.g. `[PERSON_DEADBEEF]` or `Robert Jones` if
`USE_FAKER=true`). For human-facing chat that's confusing; for
machine consumers (downstream tools, training-data pipelines,
log indexes) it's typically what's wanted. Operators choosing
this knob are deciding the surrogate form is the canonical
output — make sure your downstream consumer is built around that.

### Performance

No measurable change vs the default. The substring substitution
that's skipped is a single regex pass per text and was already
~microseconds. The win is operational (semantic match for the
deployment shape), not latency.

## Merged-input mode

Today, every text in `req.texts` triggers its own detector call —
the pipeline fans out N texts × M detectors = N×M calls. For LLM
detection, this means ten short texts cost ten separate LLM
round-trips, each carrying the ~600-token system prompt
(`prompts/llm_default.md`) for ~50 user tokens of payload —
roughly 13× system-prompt overhead.

Merged mode collapses the per-detector fan-out: the pipeline
joins all texts into a single sentinel-separated blob and the
detector makes ONE call per request. Configurable per detector
because regex and denylist are shape-based and don't benefit;
LLM/gliner/PF can.

### Configuration

Wired for all three slow detectors. Each has its own knob — the
choice is per-detector because the trade-offs differ in practice
(LLM benefits most from system-prompt amortization; PF / gliner
benefit more from cross-segment context).

| Variable | Default | Notes |
|---|---|---|
| `LLM_INPUT_MODE` | `per_text` | `per_text` keeps the existing fan-out; `merged` collapses to one call per request. |
| `PRIVACY_FILTER_INPUT_MODE` | `per_text` | Same shape as `LLM_INPUT_MODE` for the privacy-filter detector. |
| `GLINER_PII_INPUT_MODE` | `per_text` | Same shape as `LLM_INPUT_MODE` for the gliner-pii detector. |

The fast detectors (`regex`, `denylist`) intentionally do not have
this knob — they're shape-based and microseconds per call; merging
adds boundary-spanning false positives without amortization upside.

### Two wins

1. **Token / latency amortization.** One round-trip carries the
   system prompt once instead of once per text. For a request
   with 10 short texts this is roughly 13× fewer system-prompt
   tokens and fewer time-to-first-token waits.
2. **Cross-segment context.** A detector seeing all texts at
   once can correlate signals across them — a bare username in
   text 1 next to "password reset request for ..." in text 2
   reads as `CREDENTIAL` more reliably than each in isolation.

### Trade-offs (read before enabling)

- **Mutually exclusive with the result cache.** A merged blob is
  unique per request by construction (the new user message is
  always different), so the cache is 100% miss in merged mode and
  entries only consume storage. Both backends are affected:
  - Memory backend: setting `LLM_INPUT_MODE=merged` AND
    `LLM_CACHE_MAX_SIZE>0` fills the LRU with one-shot entries that
    immediately compete for the cap.
  - Redis backend: setting `LLM_INPUT_MODE=merged` AND
    `LLM_CACHE_BACKEND=redis` populates Redis with one-shot entries
    until TTL evicts them; Redis-side `maxmemory` is the only bound.

  Either combination logs a warning at boot. Pick one optimization
  per detector — set `*_INPUT_MODE=per_text` to use the cache, or
  set `*_CACHE_BACKEND=memory` + `*_CACHE_MAX_SIZE=0` to disable it.
- **Failure mode worsens.** Today one LLM call failing only
  loses detection for one text; under `fail_closed=False` the
  others still get coverage. Merged, a single bad response loses
  coverage for the *whole request*.
- **Sentinel filtering.** Pipeline drops any match whose text
  contains the segment marker (`ANONYMIZER-SEGMENT-BREAK`),
  defending against the model classifying the separator itself
  or returning a span that crosses a boundary
  ("Alice<SEP>Smith" as PERSON). Operators don't see this; it's
  silent defense in depth.
- **Size fallback.** If the merged blob exceeds a detector's
  `max_chars` cap (currently only the LLM detector declares one,
  via `LLM_MAX_CHARS`), that detector falls back to per_text
  dispatch *for that one request* (debug log emitted). The
  optimization is opportunistic — a single oversized request
  doesn't break detection coverage. Privacy-filter and gliner have
  no `max_chars` and rely on the remote service's own size policy.

### When to enable

Worth turning on when:

- Conversations carry many short texts per request (chat history,
  tool-call arguments) and LLM token cost dominates.
- The detector's classification quality regresses when texts are
  isolated — operators report entities the LLM catches in a
  contiguous prompt but misses when split.

Skip it when:

- The detector cache's hit rate is high (visible on `/health`).
  Caching saves the call entirely; merged mode just makes a
  single call cheaper.
- Failure isolation per text matters more than amortization.
