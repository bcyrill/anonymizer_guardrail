# Vault

The vault is what makes round-trip deanonymization work. When a
`request` call comes in with a `litellm_call_id`, the
surrogate→original mapping for that request is stored under that ID.
When the matching `response` call arrives, the mapping is *popped*
(read + deleted in one step) so the upstream model's reply can be
restored verbatim.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `VAULT_BACKEND` | `memory` | `memory` (default) or `redis`. The memory backend is process-local — fine for single-replica deployments. The Redis backend shares state across replicas. See [Backends](#backends) below. |
| `VAULT_REDIS_URL` | *(empty)* | Required when `VAULT_BACKEND=redis`. Format: `redis://[user:pass@]host:port/db`. Use a dedicated logical DB index per deployment so `DBSIZE` and key scans don't collide with other consumers. |
| `VAULT_TTL_S` | `600` | Drops mappings whose `post_call` never came. Applies to both backends — MemoryVault checks lazily on read; RedisVault sets `EXPIRE` so Redis evicts server-side. |
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
  * **Value:** JSON-encoded `dict[str, str]` (the surrogate→original
    mapping). JSON over msgpack so operators can debug entries
    directly: `redis-cli GET vault:abc-123`.
  * **TTL:** per-key `EXPIRE` set on `put`. Redis evicts expired
    entries server-side — no client-side scheduling.
  * **Atomic pop:** `GETDEL` (Redis ≥ 6.2). Single round-trip
    read-and-delete with no race; two concurrent `pop`s for the
    same call_id can't both observe the entry.

Failure modes:

  * **Redis unreachable on `put`:** the put raises
    `RedisVaultError`, the pipeline propagates, `main.py` returns
    BLOCKED. The pre_call DOESN'T return anonymized text whose
    mapping we couldn't store — that would ship surrogates we
    can't restore.
  * **Redis unreachable on `pop`:** raises `RedisVaultError` and
    the response goes back with surrogates still in it. Logged at
    ERROR (same severity as a TTL miss in the memory backend).

LiteLLM's `unreachable_fallback` setting does NOT apply to vault
errors — those are internal-state errors, distinct from "guardrail
endpoint is unreachable."

## Lifecycle

- **Written** on `input_type=request`, **popped** on
  `input_type=response`. One-shot per `litellm_call_id`.
- **Expiry:** entries older than `VAULT_TTL_S` (default 600s) are
  evicted. MemoryVault checks lazily on read; RedisVault relies on
  Redis-side TTL eviction. The TTL is a backstop for the case
  where LiteLLM crashes or aborts before issuing the matching
  `response` call (without it the store would grow without bound).
- **Size cap (memory backend):** `VAULT_MAX_ENTRIES` (default
  10000) bounds the store with LRU eviction as a second backstop.
  A burst of unique `call_id`s without matching `response` calls
  (client crashes, malicious flood) would otherwise sit in memory
  until TTL fires. Eviction emits a warning so a sustained overrun
  is visible in logs.
- **Skipped when `call_id` is missing.** A request without
  `litellm_call_id` is still anonymized, but no mapping is stored —
  the response side has nothing to restore against. Surfaces in the
  log as `"Anonymized N entities but no call_id was provided —
  deanonymization will not work for this request"`.

## Observability

`/health` exposes `vault_size`: the number of *open* round-trips —
requests that came in but whose responses haven't arrived yet. A
steady-state value near zero is healthy. A monotonically growing
value points at LiteLLM losing the response side, which the TTL
eventually catches up with.

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
