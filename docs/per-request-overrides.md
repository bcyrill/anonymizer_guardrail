# Per-request overrides

Several settings can be flipped on a per-call basis via LiteLLM's
`additional_provider_specific_params` field — useful when one model
needs a different detection mode, locale, or anonymization model than
the deployment-wide default.

This page documents the **cross-cutting** overrides that apply to
the pipeline as a whole. Detector-specific overrides
(`regex_overlap_strategy`, `regex_patterns`, `denylist`, `gliner_labels`,
`gliner_threshold`, `llm_model`, `llm_prompt`) live with each detector's
docs:

- [Regex detector → Per-request overrides](detectors/regex.md#per-request-overrides)
- [Denylist detector → Per-request overrides](detectors/denylist.md#per-request-overrides)
- [GLiNER-PII detector → Per-request overrides](detectors/gliner-pii.md#per-request-overrides)
- [LLM detector → Per-request overrides](detectors/llm.md#per-request-overrides)

## Cross-cutting overrides

| Override key | Type | Effect |
|---|---|---|
| `use_faker` | `bool` | Switch Faker on/off for this call. False forces opaque `[TYPE_…]` tokens. |
| `faker_locale` | `string` or `string[]` | Override `FAKER_LOCALE`. Accepts `"pt_BR,en_US"` or `["pt_BR", "en_US"]` — first locale is primary, rest are fallbacks. |
| `detector_mode` | `string` or `string[]` | **Subset filter** over the detectors built at startup — can narrow the active set for one call but cannot introduce a detector that wasn't built at boot (every detector needs constructor work that isn't safe to run mid-request). Override naming a detector that wasn't configured logs a warning and is dropped. |

## Validation policy

Unknown keys are silently ignored; known-key bad values log a warning
and are dropped. The other overrides in the same dict still take
effect, and the request is **not** blocked over a bad override —
anonymization proceeds with config defaults for whatever was rejected.

## Trust boundary — overrides are a privileged capability

Several overrides can **weaken** anonymization for the request that
carries them:

- `detector_mode` is a *subset filter* — a caller can disable
  detectors that the operator configured at boot. A request sending
  `detector_mode: ["regex"]` against a `regex,llm`-configured
  guardrail bypasses LLM detection for that call. Useful for narrow
  test traffic; unsafe for general user input.
- `regex_patterns`, `llm_prompt`, `denylist` switch which named
  alternative the matching detector uses. A caller picking a
  registry entry with a less aggressive pattern set still gets
  *some* coverage, but a different shape than the deployment
  default.
- `use_faker` and `faker_locale` change surrogate *form* but not
  whether anonymization happens — a caller flipping these can't
  make originals leak, just changes how surrogates look.

Treat the overrides field as a **trusted-caller capability**, the
same way you'd treat a header that disables WAF rules. Recommended
deployment posture:

- **LiteLLM front-end** — keep
  `additional_provider_specific_params` reachable only from the
  LiteLLM config (set there as deployment-wide static defaults), and
  do **not** put `extra_body` into client-allowed parameter lists.
  LiteLLM's per-key permission model is the place to decide which
  client virtual-keys may carry overrides.
- **Direct HTTP exposure** — if the guardrail accepts requests
  outside LiteLLM, terminate untrusted callers at a proxy that
  strips `additional_provider_specific_params` from the body, or
  pin the field to an empty dict before forwarding.
- **Audit** — bad overrides log a warning naming the offending
  field (`ignoring invalid override <key>=<value>: <reason>`).
  Successful overrides aren't logged today; if you need that for an
  audit trail, that's a logging-layer extension.

Server-side static defaults (the env vars themselves —
`USE_FAKER`, `DETECTOR_MODE`, etc.) are **not** overrideable by
clients, only narrowed via the per-request keys above. The lowest
posture a client can negotiate is "everything the operator
configured, minus whatever this request opts out of."

## Defensive limits (anti-OOM)

| Limit | Default | What it bounds |
|---|---|---|
| `faker_locale` chain length | 3 | Maximum locales in a single `faker_locale` value. A request asking for 50 locales is malformed and inflates Faker construction time. Hardcoded — primary + 1–2 fallbacks is the realistic shape. |
| `detector_mode` list length | `len(REGISTERED_SPECS)` (currently 5) | Caps the override at the number of registered detectors — anything longer cannot be valid since a `detector_mode` override is a *subset filter* over what's already configured. Auto-bumps when a new detector is registered. |
| `SURROGATE_FAKER_LRU_MAX` | `32` | LRU cap on the per-locale Faker instance cache. Each Faker is a few MB resident (provider dictionaries); without a bound, a caller cycling distinct locale tuples could grow memory unboundedly. On overflow, the least-recently-used Faker is dropped and reconstructed on next use (~1–2 ms). Override at startup if your deployment legitimately serves many distinct locale combos. |

When a length cap is exceeded, the override is dropped (warn-and-drop)
and the request falls back to the configured default for that key —
the request itself isn't blocked. The Faker LRU cap is invisible to
callers; eviction never produces a wrong surrogate, just adds a one-
time reconstruction cost on the next request that misses.

## Cache impact

`use_faker` and `faker_locale` extend the surrogate cache key from
`(entity_type, text)` to `(entity_type, text, use_faker, locale)`.
Different combos coexist in the cache, each consistent within its
bucket. Default-config traffic still buckets to the original key shape
— no migration cost. The surrogate cache itself remains bounded by
`SURROGATE_CACHE_MAX_SIZE`.

## Named alternatives

`regex_patterns`, `llm_prompt`, and `denylist` deliberately accept
*names*, not paths. Letting callers pass arbitrary paths over the wire
would be a path-traversal + bypass vector (e.g. an empty YAML disables
redaction). Operators pre-declare the allowed alternatives via three
env vars:

```bash
REGEX_PATTERNS_REGISTRY="pentest=bundled:regex_pentest.yaml,internal=/etc/anon/internal.yaml"
LLM_SYSTEM_PROMPT_REGISTRY="pentest=bundled:llm_pentest.md,legal=/etc/anon/legal.md"
DENYLIST_REGISTRY="legal=/etc/anon/legal-deny.yaml,marketing=/etc/anon/marketing-deny.yaml"
```

Format: comma-separated `name=path` pairs (whitespace around `=` and
`,` is stripped). Each path uses the same `bundled:NAME` /
filesystem-path syntax as `REGEX_PATTERNS_PATH` /
`LLM_SYSTEM_PROMPT_PATH` / `DENYLIST_PATH`. Validation:

- All entries are loaded + compiled at startup. Typos / unreadable
  files / empty prompts crash boot loudly with the offending entry
  named (`REGEX_PATTERNS_REGISTRY[pentest]=…`).
- The reserved name `default` is rejected — the default lives in the
  matching `*_PATH` env var, never in the registry, so adding a
  registry can never silently change no-override behaviour.

A request referencing an unknown name (e.g. `regex_patterns: "wrong"`)
logs a warning and falls back to the default pattern set or prompt —
the request itself isn't blocked.

## Where to set them

```yaml
# Deployment-wide static defaults (litellm config.yaml):
litellm_settings:
  guardrails:
    - guardrail_name: anonymizer
      litellm_params:
        guardrail: generic_guardrail_api
        api_base: http://anonymizer:8000
        additional_provider_specific_params:
          use_faker: false
          regex_overlap_strategy: longest
```

```python
# Per-request override (client side):
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[...],
    guardrails=[
        {"anonymizer": {"extra_body": {
            "use_faker": True,
            "faker_locale": "ja_JP",
            "detector_mode": ["regex", "llm"],
            "llm_model": "gpt-4o",
        }}}
    ],
)
```

LiteLLM merges client-side `extra_body` into the
`additional_provider_specific_params` it sends to this guardrail, so
both static config defaults and dynamic per-request overrides land on
the same field.
