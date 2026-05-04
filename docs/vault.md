# Vault

The vault is what makes round-trip deanonymization work. When a
`request` call comes in with a `litellm_call_id`, the
surrogate→original mapping for that request is stored under that ID.
When the matching `response` call arrives, the mapping is *peeked*
(read without removal) so the upstream model's reply can be restored
verbatim. Eviction happens via [`VAULT_TTL_S`](#configuration) rather
than on the read — required for streaming responses, where LiteLLM
forwards multiple sampled `response` calls under the same
`litellm_call_id` (every Nth chunk + a final assembled-response
call), each needing to see the same mapping.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `VAULT_BACKEND` | `memory` | `memory` (default) or `redis`. The memory backend is process-local — fine for single-replica deployments. The Redis backend shares state across replicas. See [Backends](#backends) below. |
| `VAULT_REDIS_URL` | *(empty)* | Required when `VAULT_BACKEND=redis`. Format: `redis://[user:pass@]host:port/db`. Use a dedicated logical DB index per deployment so `DBSIZE` and key scans don't collide with other consumers. |
| `VAULT_TTL_S` | `120` | The vault's only eviction signal. `pipeline.deanonymize` is read-only (peek), so entries persist until TTL — both for entries whose `post_call` never came AND for entries whose `post_call` ran successfully. The default comfortably covers typical chat completions (including streaming sampled-chunk peeks, which all happen within the response lifetime). Raise for workloads where individual requests can legitimately exceed 2 minutes (long generation, approval queues, batch). Applies to both backends: MemoryVault checks lazily on read; RedisVault sets `EXPIRE` so Redis evicts server-side. |
| `VAULT_MAX_ENTRIES` | `10000` | Memory backend only. Hard cap on vault entries; LRU-evicted on overflow as a backstop against a flood of unique `call_id`s before TTL clears them. Raise for sustained high in-flight traffic; floor of 1 protects against typos. The Redis backend has no equivalent — operators bound Redis memory via `maxmemory` policy on the Redis side. |

## Backends

Two implementations ship behind a `VaultBackend` Protocol:

### `MemoryVault` (default)

Process-local OrderedDict + per-key timestamp + LRU cap. Suitable
for single-replica deployments. The pre_call and post_call MUST
land on the same replica or the mapping is lost. Zero dependencies.

### `RedisVault` (multi-replica)

Shared store backed by Redis. Suitable for horizontal scaling
behind a load balancer where pre_call and post_call may land on
different replicas. Selecting this backend requires the optional
extra:

```bash
pip install "anonymizer-guardrail[vault-redis]"
```

Wire format on Redis:

  * **Key:** `vault:{call_id}`. The `vault:` prefix is reserved so
    the backend can share a Redis instance with other consumers
    without colliding. A dedicated logical DB index is still
    recommended. The `call_id` is used **directly, not hashed**:
    LiteLLM mints it as an opaque random UUID with no correlation
    to user content, so it's not a confirmation-attack vector. (The
    detector result cache, by contrast, hashes its keys with a
    secret salt because they're derived from raw input text — see
    [operations → Redis backend: why the cache key is salted](operations.md#redis-backend-why-the-cache-key-is-salted).)
    Direct keys also keep `redis-cli GET vault:<call_id>` available
    for oncall debugging.
  * **Value:** JSON-encoded `VaultEntry` — a 3-key top-level object
    with `surrogates`, `detector_mode`, and `kwargs`. The structured
    shape is required by the [pipeline-level cache prewarm](operations.md#pipeline-level-result-cache-with-source-tracked-prewarm)
    so synthesised matches carry their `entity_type` and the source
    detectors that found each entity. JSON over msgpack so operators
    can debug entries directly: `redis-cli GET vault:abc-123`. Example:

    ```json
    {
      "surrogates": {
        "[PERSON_ABCD1234]": ["Alice Smith", "PERSON", ["regex", "llm"]],
        "[EMAIL_ADDRESS_DEADBEEF]": ["alice@example.com", "EMAIL_ADDRESS", ["regex"]]
      },
      "detector_mode": ["regex", "llm"],
      "kwargs": [
        ["llm",   [["model", "anonymize"], ["prompt_name", "default"]]],
        ["regex", [["overlap_strategy", null], ["patterns_name", null]]]
      ]
    }
    ```

    Each surrogate value is a 3-tuple `[original, entity_type,
    source_detectors]`. The Python types live in
    [`vault.py`](../src/anonymizer_guardrail/vault.py) as
    `VaultEntry` / `VaultSurrogate`; see
    [design-decisions → Bidirectional pipeline-level result cache](design-decisions.md#bidirectional-pipeline-level-result-cache-with-source-tracked-prewarm)
    for the rationale on each field.
  * **TTL:** per-key `EXPIRE` set on `put`. Redis evicts expired
    entries server-side — no client-side scheduling.
  * **Read paths:** `peek` is `GET` (read-only, preserves TTL —
    used by `pipeline.deanonymize` so streaming repeated invocations
    all observe the same entry). `pop` is `GETDEL` (Redis ≥ 6.2;
    single round-trip read-and-delete, no race) and is reserved for
    utility callers — test cleanup, manual force-eviction.

Failure modes:

  * **Redis unreachable on `put`:** the put raises
    `RedisVaultError`, the pipeline propagates, `main.py` returns
    BLOCKED. The pre_call DOESN'T return anonymized text whose
    mapping we couldn't store — that would ship surrogates we
    can't restore.
  * **Redis unreachable on `peek` (production deanonymize path):**
    raises `RedisVaultError` and the response goes back with
    surrogates still in it. Logged at ERROR (same severity as a TTL
    miss in the memory backend). Same shape applies to the rare
    `pop` caller path (test cleanup, manual force-eviction).

LiteLLM's `unreachable_fallback` setting does NOT apply to vault
errors — those are internal-state errors, distinct from "guardrail
endpoint is unreachable."

## Lifecycle

- **Written** on `input_type=request`, **peeked** (read-only) on
  `input_type=response`. Entries are NOT evicted on the response
  call — required for streaming responses, where LiteLLM's
  `UnifiedLLMGuardrails` invokes our post_call repeatedly under the
  same `litellm_call_id` (every Nth chunk + a final assembled-
  response call), each needing the same mapping.
- **Expiry:** entries older than `VAULT_TTL_S` (default 120s) are
  evicted. MemoryVault checks lazily on read (next `peek` for an
  expired call_id returns empty); RedisVault relies on Redis-side
  TTL eviction. With the peek-based deanonymize path, TTL is the
  *primary* eviction signal — both for the request-errored-out
  failure case (no response ever arrives) and for the success case
  (response arrived but the entry stays around until TTL). Set
  `VAULT_TTL_S` aggressively if your workload would benefit from
  faster cleanup.
- **Size cap (memory backend):** `VAULT_MAX_ENTRIES` (default
  10000) bounds the store with LRU eviction as a second backstop.
  Every entry — successful round-trips and orphaned ones alike —
  sits in memory until TTL fires under the peek-based deanonymize
  path, so a burst of unique `call_id`s (high traffic, client
  crashes, malicious flood) can grow the store faster than TTL
  reclaims it. Eviction emits a warning so a sustained overrun is
  visible in logs.
- **Skipped when `call_id` is missing.** A request without
  `litellm_call_id` is still anonymized, but no mapping is stored —
  the response side has nothing to restore against. Surfaces in the
  log as `"Anonymized N entities but no call_id was provided —
  deanonymization will not work for this request"`.

## Observability

`/health` exposes `vault_size`: the count of vault entries currently
in the store. It's a **capacity gauge** — compare against
`VAULT_MAX_ENTRIES` and raise the cap if the value sits close to the
limit. Steady-state is bounded by
`(request rate) × VAULT_TTL_S` (entries persist until TTL evicts
them, regardless of whether the matching response was seen), so a
healthy deployment lands somewhere proportional to traffic; the
exact value is a sizing input, not a per-request alert signal.

**Backend-specific behaviour:**

  * **MemoryVault:** `vault_size` returns the exact in-process count.
  * **RedisVault:** `vault_size` returns `0` as a documented
    placeholder — `/health` is a sync endpoint and the Redis SCAN
    needed for an accurate count is async. Operators query Redis
    directly when they need the actual number: `DBSIZE` against
    the dedicated DB index, or `redis-cli --scan --pattern
    "vault:*" | wc -l`.

See [operations → Observability](operations.md#observability) for the
full `/health` shape.
