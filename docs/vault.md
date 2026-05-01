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
| `VAULT_TTL_S` | `600` | Drops mappings whose `post_call` never came. |
| `VAULT_MAX_ENTRIES` | `10000` | Hard cap on vault entries; LRU-evicted on overflow as a backstop against a flood of unique `call_id`s before TTL clears them. Raise for sustained high in-flight traffic; floor of 1 protects against typos. |

## Lifecycle

- **Written** on `input_type=request`, **popped** on
  `input_type=response`. One-shot per `litellm_call_id`.
- **Expiry:** entries older than `VAULT_TTL_S` (default 600s) are
  evicted lazily — checked on every read. There's no background
  sweeper. The TTL is a backstop for the case where LiteLLM crashes
  or aborts before issuing the matching `response` call (without it
  the vault would grow without bound).
- **Size cap:** `VAULT_MAX_ENTRIES` (default 10000) bounds the store
  with LRU eviction as a second backstop. A burst of unique
  `call_id`s without matching `response` calls (client crashes,
  malicious flood) would otherwise sit in memory until TTL fires.
  Eviction emits a warning so a sustained overrun is visible in logs.
- **Scope:** in-memory in the guardrail process. Not shared across
  replicas, not persisted across restarts (see [limitations](limitations.md)).
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

See [operations → Observability](operations.md#observability) for the
full `/health` shape.
