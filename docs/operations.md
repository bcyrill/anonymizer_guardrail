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
  up with. Full lifecycle in [vault](vault.md).
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

When extra detectors with semaphores are configured (e.g. `gliner_pii`),
their `<stats_prefix>_in_flight` and `<stats_prefix>_max_concurrency`
keys appear in `/health` automatically — see
[Adding a new detector](development.md#adding-a-new-detector).
