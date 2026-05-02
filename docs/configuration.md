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
| `VAULT_TTL_S` | `600` | Drops mappings whose `post_call` never came. See [vault](vault.md). |
| `VAULT_MAX_ENTRIES` | `10000` | Hard cap on vault entries; LRU-evicted on overflow as a backstop against a flood of unique `call_id`s before TTL clears them. Raise for sustained high in-flight traffic; floor of 1 protects against typos. |

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
