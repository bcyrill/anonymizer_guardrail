# Limitations

Known constraints of the current implementation. None of these are
hard blockers for the typical single-replica deployment, but each is
worth knowing about before you commit to a topology.

## Single replica

The [vault](vault.md) is in-memory; mappings written on one replica
aren't visible from another. Two consequences:

- **No horizontal scale-out.** A second replica can serve traffic, but
  every `request` and the matching `response` MUST land on the same
  replica — otherwise the response side has no mapping to restore from
  and ships unredacted text upstream.
- **Restarts lose in-flight round-trips.** Pre-restart `request` →
  post-restart `response` won't deanonymize. The
  [VAULT_TTL_S](vault.md#configuration) backstop bounds the leak from
  the other direction (vault grows when responses don't arrive), but
  there's nothing to do about a restart mid-round-trip beyond
  accepting it as a small loss.

For multi-replica deployments, swap the in-memory `Vault` for a
Redis-backed implementation — the interface is two methods (`put` and
`pop`). Tracked in
[`TASKS.md` → Multi-replica support](../TASKS.md#multi-replica-support-redis-backed-vault).

## No streaming responses

LiteLLM's guardrail calls are pre/post; streaming responses are
deanonymized after assembly, not chunk-by-chunk. If you need to
anonymize partial chunks as they arrive, this isn't the right tool —
the round-trip mapping shape doesn't fit a streaming protocol.
Streaming-aware deanonymization is tracked in
[`TASKS.md` → Streaming response support](../TASKS.md#streaming-response-support);
gated on a streaming use case actually showing up, since LiteLLM's
streaming-guardrail contract may still evolve.

## Surrogate cache is process-local

Cross-call consistency (same input → same surrogate across many
requests) only holds within one process. A second replica generates
its own surrogates from the same originals — same shape, but
different values unless you set a stable
[SURROGATE_SALT](surrogates.md#surrogate-salt-privacy-hardening).
Whether to move the surrogate cache to a shared backend or accept
per-replica inconsistency is an open question on
[`TASKS.md` → Multi-replica support](../TASKS.md#multi-replica-support-redis-backed-vault).

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

## In-memory only

Both stores ([vault](vault.md) and
[surrogate cache](surrogates.md#surrogate-cache)) live in process
memory. No persistence, no shared state. This keeps the guardrail
zero-dependency and easy to deploy, at the cost of the limitations
above. The detector result cache (`detector/cache.py`) follows the
same in-memory-by-default pattern; cross-replica / persistent
variants are tracked in
[`TASKS.md` → Redis-backed detector result cache](../TASKS.md#redis-backed-detector-result-cache).
