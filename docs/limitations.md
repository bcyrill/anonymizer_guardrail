# Limitations

Known constraints of the current implementation. None of these are
hard blockers for the typical single-replica deployment, but each is
worth knowing about before you commit to a topology.

## Single replica

Applies to the **default** vault backend; opt out with
`VAULT_BACKEND=redis` (see below). The default
[vault](vault.md) backend (`MemoryVault`) is process-local;
mappings written on one replica aren't visible from another. Two
consequences when running this default:

- **No horizontal scale-out.** A second replica can serve traffic,
  but every `request` and the matching `response` MUST land on the
  same replica — otherwise the response side has no mapping to
  restore from and ships unredacted text upstream.
- **Restarts lose in-flight round-trips.** Pre-restart `request` →
  post-restart `response` won't deanonymize. The
  [VAULT_TTL_S](vault.md#configuration) backstop bounds the leak
  from the other direction (vault grows when responses don't
  arrive), but there's nothing to do about a restart
  mid-round-trip beyond accepting it as a small loss.

**Solved for multi-replica deployments by `VAULT_BACKEND=redis`.**
Selecting the Redis backend gives a shared store: pre_call and
post_call can land on different replicas, and restart-mid-round-trip
survives provided Redis stayed up. Install with
`pip install "anonymizer-guardrail[vault-redis]"` and set
`VAULT_REDIS_URL`; see [vault → Backends](vault.md#backends) for
the wire format and failure modes.

## Streaming responses are not deanonymized

When a client requests `stream: true`, the assistant's response
chunks reach the client with surrogates intact (`[PERSON_…]`,
`[EMAIL_ADDRESS_…]`, etc.). The pre-call (anonymize) side still
works — the upstream LLM only ever sees anonymized text — but the
client sees surrogates in the streamed reply.

**Root cause is in LiteLLM, not this service.** LiteLLM's
`UnifiedLLMGuardrails` integration calls our
`apply_guardrail` endpoint repeatedly during streaming (every Nth
chunk plus a final assembled-response call), but its iterator-hook
implementation is **moderation-only**: it computes the guardrail
output for compliance scanning and `BLOCKED` decisions, then yields
the *original* upstream chunk to the client regardless of any
modified text the guardrail returned. See
[`unified_guardrail.py` line 417 `original_item = copy.deepcopy(item)`
and line 465 `yield original_item`](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/guardrails/guardrail_hooks/unified_guardrail/unified_guardrail.py).

Service-side, the deanonymize path is already correct under this
load — `pipeline.deanonymize` uses `vault.peek` so every sampled
streaming call sees the same surrogate map and produces the right
deanonymized output. That output just doesn't make it to the
client because LiteLLM yields the original chunk.

**Workarounds:**

- **Disable streaming** on anonymizer-protected models. The
  anonymizer protects the upstream LLM unconditionally; the only
  thing streaming breaks is the client-side restoration. For chat
  UIs that rely on token-by-token rendering this is a UX downgrade
  but not a correctness regression — the client gets the same
  final text in one response, just without the typing animation.
- **Track upstream.** Comment / +1 on a feature request to make
  `UnifiedLLMGuardrails` apply guardrail-modified texts to yielded
  chunks (gated by a config flag so existing moderation-only
  consumers aren't surprised).

**Long-term fix tracked here.** A LiteLLM-side `CustomGuardrail`
Python plugin that implements `async_post_call_streaming_iterator_hook`
as a true generator transformer (yielding modified chunks rather
than originals) would close the loop end-to-end. Tracked in
[`TASKS.md` → Streaming response support](../TASKS.md#streaming-response-support);
deferred because (a) the plugin requires a thin derived LiteLLM
image rather than the official one, which is a meaningful
deployment-shape change, and (b) the upstream fix may land first
and obviate the plugin entirely.

## Hard cap on POST body size

Request bodies above
[`MAX_BODY_BYTES`](configuration.md#cross-cutting) (10 MiB by
default) are rejected with HTTP 503 before Pydantic deserializes
them — see `main.py:_BodySizeLimitMiddleware`. The cap exists as
DoS protection (distinct from `LLM_MAX_CHARS`, which is an LLM-
context-window concern that fires *after* parsing).

503 — rather than the semantically-precise 413 — was chosen so
LiteLLM's
[`unreachable_fallback`](litellm-integration.md) setting governs
the upstream call: under `fail_open` an oversized body lets the
LLM call proceed, under `fail_closed` it blocks. LiteLLM hardcodes
`is_unreachable in (502, 503, 504)`, so 413 would block
unconditionally. Operators relying on `fail_open` for availability
should know that an oversized body still respects that choice.

## Surrogate cache is process-local

Cross-call consistency (same input → same surrogate across many
requests) only holds within one process. A second replica generates
its own surrogates from the same originals — same shape, but
different values unless you set a stable
[SURROGATE_SALT](surrogates.md#surrogate-salt-privacy-hardening).
With a stable salt every replica derives the same surrogate
deterministically (the cache is just a memoization layer over
seeded BLAKE2b), so a shared Redis-backed surrogate cache is *not*
required for multi-replica deployments — `SURROGATE_SALT` is the
canonical answer. The cache stays per-replica.

## No `/metrics` endpoint

Today's [`/health`](operations.md#observability) exposes point-in-time
gauges (vault size, in-flight detector calls, cache stats) — fine for
"is the process alive and does it look saturated right now?" Counters,
histograms, and any time-series question (request rate, p95 detection
latency, hourly entity counts by type, saturation events over time)
require a Prometheus-style `/metrics` endpoint, which the guardrail
doesn't expose yet. Tracked in
[`TASKS.md` → Prometheus-style /metrics endpoint](../TASKS.md#prometheus-style-metrics-endpoint);
deferred until a metrics scraper is actually deployed in front of it.

## In-memory by default

Four internal stores back the guardrail's stateful behaviour. Default
backends are all process-local; opt-in Redis backends ship for the
three that benefit from cross-replica or restart-persistent state:

- [Vault](vault.md) — `MemoryVault` (default) or `RedisVault` (opt-in
  via `VAULT_BACKEND=redis` + `VAULT_REDIS_URL`). Required for
  multi-replica deployments where pre_call and post_call may land on
  different replicas behind a load balancer.
- [Pipeline-level result cache](operations.md#pipeline-level-result-cache-with-source-tracked-prewarm)
  — disabled by default; opt in via `PIPELINE_CACHE_BACKEND=memory`
  (in-process LRU) or `PIPELINE_CACHE_BACKEND=redis` (shared store
  reusing `CACHE_REDIS_URL` + `CACHE_SALT`). Wraps detector dispatch
  — on a hit, the pipeline skips ALL detectors. Useful for chat
  workloads where prior turn texts repeat across requests.
- [Per-detector result cache](operations.md#detector-result-caching) —
  `InMemoryDetectionCache` (default) or `RedisDetectionCache` (opt-in
  per detector via `<DETECTOR>_CACHE_BACKEND=redis` +
  `CACHE_REDIS_URL`). Useful for multi-replica cache hits and
  cache survival across guardrail restarts. Acts as the structural
  backup tier when the pipeline cache misses on partial-kwargs
  changes.
- [Surrogate cache](surrogates.md#surrogate-cache) — process-local
  only. Cross-replica surrogate consistency is achieved via a shared
  `SURROGATE_SALT` (deterministic seeded BLAKE2b — every replica
  derives the same surrogate without sharing state), not via a
  shared cache. A Redis-backed surrogate cache would buy memoization
  speedup, not correctness, so it isn't on the roadmap.

Default backends keep single-replica deployments zero-dependency. The
Redis-backed alternatives are bundled in the default container image
(`pip install ".[vault-redis,cache-redis]"`) so flipping the env var
doesn't require a rebuild.

## Round-trip restoration is the default

By default the guardrail completes a full round-trip: anonymize on
the request side, deanonymize on the response side, and the
calling client receives the original PII restored. For
redaction-only deployments where the surrogate form should
persist in responses (telemetry / log scrubbing, training-data
sanitization, compliance pipelines), set
`DEANONYMIZE_SUBSTITUTE=false` — see [operations →
Surrogates persist in responses](operations.md#surrogates-persist-in-responses-deanonymize_substitutefalse).

Two implications operators should know about regardless of the
flag:

- **PII the model itself emits (vs the surrogate it received) is
  not redacted.** The deanonymize step only does substring
  replacement of known surrogates → originals. A model that
  retrieves customer records from a downstream tool and surfaces
  them in its response surfaces real PII to the client. See
  [design-decisions → Why we don't redact model output](design-decisions.md)
  for the rationale.
- **Clients receiving surrogate-laden responses
  (`DEANONYMIZE_SUBSTITUTE=false`)** see literal surrogate strings
  in the model's output. For human-facing chat UIs this is
  confusing; for machine consumers it's typically what's wanted.
  This is a deployment-shape choice, not a backwards-compatible
  feature flag — switch only when the consumer is built around
  the surrogate form.
