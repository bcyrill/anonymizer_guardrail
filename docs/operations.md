# Operations: observability

The guardrail is operated as a stateless HTTP service with two
in-memory stores ([vault](vault.md) and
[surrogate cache](surrogates.md#surrogate-cache)). This page covers
the cross-cutting `/health` endpoint and how to read it; each store's
own lifecycle and sizing knobs live with its dedicated docs.

For deeper background:

- **[Vault](vault.md)** — round-trip mapping, TTL, size cap.
- **[Surrogate cache](surrogates.md#surrogate-cache)** — cross-call
  consistency, LRU eviction.
- **[Limitations](limitations.md)** — single-replica constraint,
  streaming, restart behaviour.

## Observability

`/health` returns live counters for both stores plus per-detector
concurrency gauges so operators can monitor pressure or leak without
inspecting the process:

```json
{
  "status": "ok",
  "detector_mode": "regex,llm",
  "vault_size": 3,
  "surrogate_cache_size": 1421,
  "surrogate_cache_max": 100000,
  "llm_in_flight": 0,
  "llm_max_concurrency": 10,
  "pf_in_flight": 0,
  "pf_max_concurrency": 10
}
```

How to read each counter:

- **`vault_size`** — number of *open* round-trips. A steady-state value
  near zero is healthy; a monotonically growing value points at
  LiteLLM losing the response side, which the TTL eventually catches
  up with. Full lifecycle in [vault](vault.md). Multi-replica
  deployments need a shared store for the vault — tracked in
  [`TASKS.md` → Multi-replica support](../TASKS.md#multi-replica-support-redis-backed-vault).
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
  caching (currently only `llm_*`). All four keys are present even when
  caching is disabled (`*_cache_max=0`), so dashboards can pin to stable
  names. See [Detector result caching](#detector-result-caching) below.

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
opt-in process-wide LRU keyed by input text plus the overrides that
affect that detector's output, letting repeat calls skip the slow
detection work entirely.

### Configuration

Three detectors opt into caching. Each has its own cap, default
off (`0` disables). The cap controls the in-memory LRU; eviction
fires once the size grows past the cap.

| Variable | Default | Notes |
|---|---|---|
| `LLM_CACHE_MAX_SIZE` | `0` | LRU cap on the LLM detector's result cache. |
| `PRIVACY_FILTER_CACHE_MAX_SIZE` | `0` | LRU cap on the privacy-filter detector's result cache. |
| `GLINER_PII_CACHE_MAX_SIZE` | `0` | LRU cap on the gliner-pii detector's result cache. |

The fast detectors (`regex`, `denylist`) are intentionally not on
this list — they're microseconds per call against pre-built data
structures, so caching would add overhead for negligible savings.

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

### When to enable

Worth turning on when you serve multi-turn conversations and the LLM
detector's per-call latency is the dominant cost. With caching off,
turn N pays the LLM round-trip for every replayed prior message; with
caching on, only newly-introduced text incurs the round-trip.

Skip it for:

- **Single-turn workloads** — there's nothing to reuse.
- **Privacy-sensitive deployments where stable surrogates would be a
  leak channel** — caching makes the detector's output stable across
  turns, which by design makes the surrogate output stable too.

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

Process-local, like every other in-memory store. After a restart the
cache is cold and the first occurrence of each input pays the full
detection cost again. There's no on-disk persistence and no
replication across replicas — see [limitations](limitations.md) for
the broader single-replica story.
