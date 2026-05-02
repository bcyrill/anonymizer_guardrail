# Configuration

All knobs are environment variables; sensible defaults baked into
`Containerfile`.

This page documents the **cross-cutting** settings — HTTP server,
detector activation, surrogate generation, vault, observability.
Detector-specific env vars (every `LLM_*`, `REGEX_*`, `DENYLIST_*`,
`PRIVACY_FILTER_*`, `GLINER_PII_*` knob) live with each detector's
docs:

- [Regex detector](detectors/regex.md#configuration)
- [Denylist detector](detectors/denylist.md#configuration)
- [Privacy-filter detector](detectors/privacy-filter.md#configuration)
- [GLiNER-PII detector](detectors/gliner-pii.md#configuration)
- [LLM detector](detectors/llm.md#configuration)

## Cross-cutting

| Variable | Default | Notes |
|---|---|---|
| `HOST` | `0.0.0.0` | |
| `PORT` | `8000` | |
| `LOG_LEVEL` | `INFO` | |
| `DETECTOR_MODE` | `regex,llm` | Comma-separated list of detector names. Order matters — see [detectors](detectors/index.md). |
| `MAX_BODY_BYTES` | `10485760` (10 MiB) | Hard cap on POST request body size, enforced by middleware BEFORE Pydantic parses. Distinct from `LLM_MAX_CHARS` — that's an LLM-context-window concern; this is DoS protection that prevents the process from allocating memory for a runaway payload. Oversized requests get HTTP 503; LiteLLM's `unreachable_fallback` setting decides whether the upstream LLM call proceeds (`fail_open`) or is blocked (`fail_closed`). 503 — rather than the semantically-precise 413 — was chosen because LiteLLM's Generic Guardrail client hardcodes `is_unreachable in (502, 503, 504)`, so 413 would block the LLM call regardless of operator intent. Floor of 1 byte — a 0/negative cap fails at boot. |

## Surrogates and Faker

| Variable | Default | Notes |
|---|---|---|
| `USE_FAKER` | `true` | When false, all surrogates are opaque tokens. See [surrogates → Disabling Faker](surrogates.md#disabling-faker). |
| `FAKER_LOCALE` | *(empty → en_US)* | Faker locale, e.g. `pt_BR` or `pt_BR,en_US`. See [surrogates → Localising](surrogates.md#localising-surrogates). |
| `SURROGATE_SALT` | *(empty → random)* | blake2b key for surrogate hashes. See [surrogates → Salt](surrogates.md#surrogate-salt-privacy-hardening). |
| `SURROGATE_CACHE_MAX_SIZE` | `100000` | LRU cap on the surrogate cache (cross-request consistency). See [surrogates → Surrogate cache](surrogates.md#surrogate-cache). |
| `SURROGATE_FAKER_LRU_MAX` | `32` | LRU cap on per-locale Faker instances built for `faker_locale` overrides. Bounds memory against callers cycling distinct locale tuples. See [per-request overrides](per-request-overrides.md). |

## Vault

| Variable | Default | Notes |
|---|---|---|
| `VAULT_BACKEND` | `memory` | `memory` (default) or `redis`. Memory is process-local; Redis shares state across replicas. See [vault → Backends](vault.md#backends). |
| `VAULT_REDIS_URL` | *(empty)* | Required when `VAULT_BACKEND=redis`. Format: `redis://[user:pass@]host:port/db`. Setting `redis` without this fails at boot. Install `pip install "anonymizer-guardrail[vault-redis]"` to get the redis dep. |
| `VAULT_TTL_S` | `600` | Drops mappings whose `post_call` never came. Applies to both backends. See [vault](vault.md). |
| `VAULT_MAX_ENTRIES` | `10000` | Memory backend only. Hard cap; LRU-evicted on overflow. Redis bounds via its own `maxmemory` policy. Floor of 1 protects against typos. |

## Detector cache (cross-cutting)

Per-detector cache backends and capacity knobs live in each detector's
own page (see `<DETECTOR>_CACHE_*` rows in
[LLM detector](detectors/llm.md#configuration),
[privacy filter](detectors/privacy-filter.md#configuration),
[GLiNER PII](detectors/gliner-pii.md#configuration)). The two
cross-cutting fields below are shared across every detector that
selects `<DETECTOR>_CACHE_BACKEND=redis`:

| Variable | Default | Notes |
|---|---|---|
| `CACHE_REDIS_URL` | *(empty)* | Required when any detector picks `cache_backend=redis`. Format: `redis://[user:pass@]host:port/db`. Distinct from `VAULT_REDIS_URL` so vault and cache can target different Redis instances. Install `pip install "anonymizer-guardrail[cache-redis]"` to get the redis dep. |
| `CACHE_SALT` | *(empty → random per process)* | Secret keying material for the BLAKE2b digest used in Redis cache keys. Closes a confirmation-attack oracle on Redis read access — see [operations → Redis backend: why the cache key is salted](operations.md#redis-backend-why-the-cache-key-is-salted). Empty → fresh 16 random bytes generated at process start (logged once). Set to a fixed value for multi-replica cache hits or cache survival across process restarts. Distinct from `SURROGATE_SALT` so they can be rotated independently. |

## Capping detector concurrency

Each remote / model-backed detector has its own semaphore set via
`*_MAX_CONCURRENCY`. The values are independent so a saturated detector
doesn't reduce headroom for the others:

- `LLM_MAX_CONCURRENCY` (default 10) — see
  [LLM detector → Configuration](detectors/llm.md#configuration).
- `PRIVACY_FILTER_MAX_CONCURRENCY` (default 10) — see
  [Privacy-filter detector → Configuration](detectors/privacy-filter.md#configuration).
- `GLINER_PII_MAX_CONCURRENCY` (default 10) — see
  [GLiNER-PII detector → Configuration](detectors/gliner-pii.md#configuration).

Regex and denylist are not throttled — they're stdlib-`re` against the
input string, microseconds per call.

Every text in the request's `texts` array triggers its own detection
call per active detector, so a single guardrail invocation with four
texts already consumes four LLM slots, four PF slots, and four
gliner-pii slots in parallel. Once a cap is reached, further calls
into that detector await rather than fire in parallel.

Saturation is observable via `/health` — see
[operations → Observability](operations.md#observability) for the
counter shape and how to read it.
