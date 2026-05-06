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

## Streaming responses (pending upstream LiteLLM merge)

Service-side, the response handler implements the
[LiteLLM streaming action protocol](https://github.com/BerriAI/litellm/pull/27228):
the request carries `is_final`, the response returns one of
`NONE` / `GUARDRAIL_INTERVENED` / `WAIT` / `BLOCKED`, and a `WAIT`
defers substitution when the accumulated text trails off
mid-surrogate (`[PERSON_…` unclosed at a chunk boundary). With
opaque-token surrogates (`USE_FAKER=false`) this delivers per-chunk
deanonymization end-to-end including correct boundary handling —
see `scripts/test-streaming-action-protocol.sh` for the e2e
harness exercising both the plain-chunk and chunk-straddle arms.

**Upstream status: PR open, not yet merged.**
[BerriAI/litellm#27228](https://github.com/BerriAI/litellm/pull/27228)
adds the iterator-side state machine that consumes the action
protocol and rewrites chunks on the wire. Stock LiteLLM still
samples guardrail output for moderation only and yields the
*original* (surrogate-laden) chunks regardless of what we return —
so until that PR lands, operators who want streaming end-to-end
must build a derived LiteLLM image off the PR branch (the harness
script tags it `litellm:streaming-action-protocol`).

### Faker mode trades streaming UX for correctness (opt-in flag)

The `WAIT` heuristic relies on the opaque-token shape
(`[PREFIX_DIGEST]`): a literal `[` followed by uppercase/digit/`_`
content closing on `]`. Faker surrogates are natural-language
strings (`John Smith`, `quasarware.local`) with no detectable
boundary, so we can't distinguish a partial surrogate from real
text mid-stream. Two operator-selectable behaviours via
`STREAMING_FAKER_MODE`:

- **`disabled`** (default): no protection. Faker surrogates that
  straddle chunk boundaries leak to the client. Streaming UX is
  preserved.
- **`buffer`**: every non-final response chunk returns `WAIT`,
  deferring substitution to the `is_final=True` call which sees
  the full assembled text. Correct end-to-end (same code path as
  non-streaming), but the client receives nothing until
  end-of-stream — time-to-first-token becomes time-to-last-token.

The flag is opt-in because turning it on silently would regress
streaming UX in deployments that already accepted the leak risk.
The boundary-aware streaming path (suffix-prefix matching against
the vault's surrogate set) is tracked as the follow-up upgrade in
[`TASKS.md` → Faker streaming with boundary-aware
substitution](../TASKS.md#faker-streaming-with-boundary-aware-substitution).
See [design-decisions → Faker streaming buffers to
end-of-stream](design-decisions.md#faker-streaming-buffers-to-end-of-stream-behind-an-opt-in-flag)
for the full reasoning.

### Secondary effect: cache mismatch across turns (resolved by PR 27228)

On stock LiteLLM, the deanonymize-side prewarm seeds the pipeline +
per-detector caches keyed by the text we *return* to LiteLLM
(originals substituted in, when `DEANONYMIZE_SUBSTITUTE=true`).
That text never reaches the client — Open WebUI and similar chat
UIs render the surrogate-laden chunks they actually received. The
next turn's chat history therefore carries the surrogate-laden
assistant reply, not the original-laden one our prewarm assumed,
and the cache misses.

What that means in practice on stock LiteLLM:

- The prewarmed slot (keyed by original-laden text) sits unused
  until LRU/TTL evicts it. Wasted cache capacity proportional to
  streaming traffic.
- Detection re-runs from scratch on the surrogate-laden history,
  with no cache shortcut. Latency cost per turn.
- An LLM detector seeing `[PERSON_xyz]` in the prior assistant
  reply could plausibly flag it as suspicious and re-anonymize
  it into a fresh inner surrogate (vault entry chains
  surrogate→surrogate over turns; the original PII becomes
  inaccessible after the first vault entry expires).

Once PR 27228 merges, chunks are rewritten on the wire — the
client's history carries originals, the prewarm key matches, and
this consequence dissolves. We've also wired the response handler
to gate `_prewarm_caches` on `is_final` so a PR-27228 stream writes
exactly one cache slot at end-of-stream (instead of one per
sampled chunk); see
[design-decisions → Prewarm gates on `is_final`](design-decisions.md#prewarm-gates-on-is_final-for-streaming-fires-unconditionally-otherwise).
Until the upstream PR ships, the workaround for stock LiteLLM is
to **disable streaming** on anonymizer-protected models: the
client gets the same final text in one response, just without the
typing animation.

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
