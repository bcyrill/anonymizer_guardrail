# anonymizer-guardrail

A small FastAPI service that implements LiteLLM's
[Generic Guardrail API](https://docs.litellm.ai/docs/adding_provider/generic_guardrail_api)
to perform reversible anonymization of LLM traffic.

LiteLLM calls this service before forwarding a request upstream
(`input_type="request"`) to anonymize sensitive substrings, then again after
the upstream model responds (`input_type="response"`) to deanonymize them. A
short-lived in-memory mapping keyed by `litellm_call_id` connects the two
sides of the round-trip.

## Detection layers

Three layers, all optional, controlled by `DETECTOR_MODE` — a comma-
separated list of detector names. Available names: `regex`, `llm`,
`privacy_filter`. Order determines type-resolution priority: when the
same text is detected by multiple detectors, the type from the one
listed first wins. Example: `DETECTOR_MODE=regex,privacy_filter,llm`.

- **regex** — high-precision patterns for things with recognizable shapes:
  IPs, CIDRs, emails, hashes, JWTs, AWS keys, GitHub tokens, OpenAI-style
  keys, internal hostnames (`*.local`, `*.internal`, etc.). Stateless,
  deterministic, no external dependencies.

- **llm** — calls an OpenAI-compatible Chat Completions endpoint with a
  JSON-mode prompt that asks the model to enumerate sensitive entities.
  Catches contextual stuff regex cannot: org names, personal names,
  internal product/project codenames embedded in prose.

- **privacy_filter** — local NER backed by
  [openai/privacy-filter](https://huggingface.co/openai/privacy-filter)
  (Apache 2.0). Encoder-only token classifier, ~1.5 B params (50 M active
  via MoE), runs in-process — no external service, no API key. Detects
  8 PII categories: people, emails, phones, URLs, addresses, dates,
  account numbers, secrets. Coverage is a strict subset of what the LLM
  prompt picks up (no orgs, hostnames, IP/MAC, etc.) so it's a
  *complement* to — not a replacement for — the LLM detector. Optional
  dependency: `pip install "anonymizer-guardrail[privacy-filter]"` or
  build the container with `--build-arg WITH_PRIVACY_FILTER=true`.

When multiple detectors are configured (`DETECTOR_MODE=regex,llm`), they
run in parallel and the matches are merged and deduped.

## Surrogates

Each detected entity is replaced with a *realistic* substitute of the same
type — `acmecorp.local` → `quasarware.local`, not `[ORGANIZATION_7F3A2B]` —
generated with [Faker](https://faker.readthedocs.io/) seeded by a hash of
the original. Determinism means the same input always maps to the same
surrogate within a process, so the upstream model sees consistent
substitutions across multi-turn conversations.

Opaque tokens are still used for things where realism would be misleading
(`HASH`, `JWT`, `CREDENTIAL`, `TOKEN`, `AWS_ACCESS_KEY`).

## State and lifecycle

The service holds two separate in-memory stores. They serve different
purposes and have different eviction strategies — worth keeping
straight when reasoning about correctness, restarts, and capacity.

### Vault (per-request mapping)

The vault is what makes round-trip deanonymization work. When a
`request` call comes in with a `litellm_call_id`, the
surrogate→original mapping for that request is stored under that ID.
When the matching `response` call arrives, the mapping is *popped*
(read + deleted in one step) so the upstream model's reply can be
restored verbatim.

- **Lifecycle:** written on `input_type=request`, popped on
  `input_type=response`. One-shot per `litellm_call_id`.
- **Expiry:** entries older than `VAULT_TTL_S` (default 600s) are
  evicted lazily — checked on every read. There's no background
  sweeper. The TTL is a backstop for the case where LiteLLM crashes
  or aborts before issuing the matching `response` call (without it
  the vault would grow without bound).
- **Scope:** in-memory in the guardrail process. Not shared across
  replicas, not persisted across restarts (see *Limitations*).
- **Skipped when `call_id` is missing.** A request without
  `litellm_call_id` is still anonymized, but no mapping is stored —
  the response side has nothing to restore against. Surfaces in the
  log as `"Anonymized N entities but no call_id was provided —
  deanonymization will not work for this request"`.

### Surrogate cache (cross-call consistency)

The surrogate generator memoizes `(entity_type, original_text) →
surrogate` so the same input always yields the same surrogate —
within one request *and* across many. This is what lets an upstream
model see a stable view of a given email/hostname/UUID over a
multi-turn conversation, even though each turn is a separate
LiteLLM call (and therefore a separate vault entry).

- **Lifecycle:** populated by every detected match. Outlives the
  surrounding request/response cycle — that's the whole point.
- **Eviction:** LRU at `SURROGATE_CACHE_MAX_SIZE` entries (default
  100 000). No time-based expiry; entries stay until newer ones
  push them out.
- **Scope:** same as the vault — in-memory, per-process, not shared.
- **Determinism across restart:** every surrogate is derived from a
  keyed BLAKE2b hash of the original. The key is `SURROGATE_SALT`.
  - Default (random salt per process start): same input → same
    surrogate **within** a process; the cache speeds it up but the
    derivation is itself deterministic. After a restart the salt
    changes, so old surrogates are no longer reproducible.
  - With `SURROGATE_SALT` set to a stable string: same input → same
    surrogate forever. Useful for log-correlation. See
    [Surrogate salt](#surrogate-salt-privacy-hardening) for the
    privacy trade-off.
- **Collision handling:** if two distinct originals would produce
  the same surrogate, the generator salts and retries. After a
  small number of failed attempts it falls back to a guaranteed-
  unique opaque token (bounds the worst-case; effectively never
  happens in practice).

### Observability

`/health` returns live counters for both stores so operators can
monitor pressure or leak without inspecting the process:

```json
{
  "status": "ok",
  "detector_mode": "regex,llm",
  "vault_size": 3,
  "surrogate_cache_size": 1421,
  "surrogate_cache_max": 100000,
  "llm_in_flight": 0,
  "llm_max_concurrency": 10
}
```

- `vault_size` is the number of *open* round-trips — requests that
  came in but whose responses haven't arrived yet. A steady-state
  value near zero is healthy. A monotonically growing value points
  at LiteLLM losing the response side, which the TTL eventually
  catches up with.
- `surrogate_cache_size` near `surrogate_cache_max` for sustained
  periods means LRU eviction is firing — the oldest cross-request
  consistency invariants are quietly being lost. Bump
  `SURROGATE_CACHE_MAX_SIZE` if that matters for your traffic shape.

## Wiring it into LiteLLM

A working `config.yaml` snippet (see `litellm.config.example.yaml` for the
full file):

```yaml
litellm_settings:
  guardrails:
    - guardrail_name: anonymizer
      litellm_params:
        guardrail: generic_guardrail_api
        mode: [pre_call, post_call]      # both — that's how round-trip works
        api_base: http://anonymizer:8000
        unreachable_fallback: fail_closed

model_list:
  # Models that should be anonymized — these opt in via the `guardrails` field
  # in client requests, OR set default_on: true on the guardrail above.
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY

  # Detection model used by the LLM detector layer. CRITICAL: this model must
  # NOT be guarded by the anonymizer — otherwise every detection call would
  # re-enter the guardrail and recurse forever.
  - model_name: anonymize
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
```

Client-side, callers tag the request:

```python
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "..."}],
    guardrails=["anonymizer"],
)
```

## Configuration

All knobs are environment variables; sensible defaults baked into
`Containerfile`:

| Variable          | Default                       | Notes                                    |
|-------------------|-------------------------------|------------------------------------------|
| `HOST`            | `0.0.0.0`                     |                                          |
| `PORT`            | `8000`                        |                                          |
| `LOG_LEVEL`       | `INFO`                        |                                          |
| `DETECTOR_MODE`   | `regex,llm`                   | comma-separated list of detector names  |
| `LLM_API_BASE`    | `http://litellm:4000/v1`      | OpenAI-compatible endpoint               |
| `LLM_API_KEY`     | *(empty)*                     | Bearer token if the endpoint needs one   |
| `LLM_USE_FORWARDED_KEY` | `false`                 | Use the caller's Authorization header (see below) |
| `LLM_SYSTEM_PROMPT_PATH` | *(empty → bundled `llm_default.md`)* | Override the bundled detection prompt |
| `LLM_SYSTEM_PROMPT_REGISTRY` | *(empty)*          | Comma-separated `name=path` list of NAMED alternative prompts callers can opt into per-request via `llm_prompt`. See *Per-request overrides → Named alternatives* below. |
| `REGEX_PATTERNS_PATH` | *(empty → bundled `regex_default.yaml`)* | Override the bundled regex patterns YAML |
| `REGEX_PATTERNS_REGISTRY` | *(empty)*             | Comma-separated `name=path` list of NAMED alternative regex pattern files callers can opt into per-request via `regex_patterns`. See *Per-request overrides → Named alternatives* below. |
| `REGEX_OVERLAP_STRATEGY` | `longest`              | `longest` (longest match wins on overlapping spans) or `priority` (first pattern in YAML order wins). See *Regex overlap resolution* below. |
| `FAKER_LOCALE`    | *(empty → en_US)*             | Faker locale, e.g. `pt_BR` or `pt_BR,en_US` |
| `USE_FAKER`       | `true`                        | When false, all surrogates are opaque tokens |
| `SURROGATE_CACHE_MAX_SIZE` | `100000`             | LRU cap on the surrogate cache (cross-request consistency) |
| `SURROGATE_FAKER_LRU_MAX` | `32`                  | LRU cap on per-locale Faker instances built for `faker_locale` overrides. Bounds memory against callers cycling distinct locale tuples. See *Per-request overrides* below. |
| `SURROGATE_SALT`  | *(empty → random)*            | blake2b key for surrogate hashes (see below) |
| `LLM_MODEL`       | `anonymize`                   | Model alias used for detection           |
| `LLM_TIMEOUT_S`   | `30`                          | Per-call timeout (seconds) on LLM detector HTTP requests |
| `LLM_MAX_CHARS`   | `200000`                      | Hard cap; inputs above this are refused  |
| `LLM_MAX_CONCURRENCY` | `10`                      | Semaphore on in-flight LLM detector calls; surfaced as `llm_in_flight`/`llm_max_concurrency` on `/health` |
| `VAULT_TTL_S`     | `600`                         | Drops mappings whose post_call never came |
| `FAIL_CLOSED`     | `true`                        | Block requests if LLM detector errors    |
| `HF_HUB_OFFLINE`  | `1` *(baked image only)* / *(unset)* | Pf-baked sets this so transformers doesn't ping HuggingFace Hub on every start; pass `-e HF_HUB_OFFLINE=0` to force online mode for a refresh. The `pf` (runtime-download) flavour leaves it unset on first run; `scripts/cli.sh --hf-offline` / the menu offer it after the cache volume is populated. |

### Forwarding the caller's API key

Set `LLM_USE_FORWARDED_KEY=true` to authenticate to the detection LLM with
the same key the user authenticated to LiteLLM with, instead of a shared
`LLM_API_KEY`. Detection cost and rate limits then attribute back to the
caller's virtual key.

This requires opting into header forwarding on the LiteLLM side as well —
LiteLLM redacts non-allowlisted headers to `"[present]"` by default, so
without `extra_headers`, this guardrail will silently fall back to
`LLM_API_KEY`:

```yaml
litellm_settings:
  guardrails:
    - guardrail_name: anonymizer
      litellm_params:
        guardrail: generic_guardrail_api
        mode: [pre_call, post_call]
        api_base: http://anonymizer:8000
        unreachable_fallback: fail_closed
        extra_headers: [authorization]   # ← forwards Bearer <user-key> to us
```

If the header is missing or arrives as `[present]`, we fall back to
`LLM_API_KEY`. If both are empty, the LLM call goes out without an
`Authorization` header (fine for local/dev backends; everything else will
likely return 401, which routes through `FAIL_CLOSED`).

### Capping LLM detector concurrency

`LLM_MAX_CONCURRENCY` is a process-wide semaphore around the LLM
detector — it does **not** throttle regex or privacy-filter, both of
which run locally and don't have a backend to overwhelm. Every text in
the request's `texts` array triggers its own detection call, so a
single guardrail invocation with four texts already consumes four
slots; once the cap is reached, further detection calls await rather
than fire in parallel.

The default of `10` is a conservative number that keeps a small backend
(local Ollama, a single LiteLLM instance fronting a rate-limited
provider) from getting buried under a burst of concurrent users. Raise
it if your detection backend is generously provisioned and you're
seeing detection latency dominated by queueing, lower it if you're
hitting upstream 429s.

Saturation is observable via `/health`:

```json
{ "status": "ok", "llm_in_flight": 8, "llm_max_concurrency": 10, ... }
```

`llm_in_flight` close to `llm_max_concurrency` for sustained periods
means detection is the bottleneck — either the upstream LLM is slow,
or the cap needs to come up. The counter is the actual in-flight
count, not the semaphore's queue depth, so it tops out at
`llm_max_concurrency`.

### Customising the detection prompt

Two prompts ship with the package under
`src/anonymizer_guardrail/prompts/`:

- `llm_default.md` — small, conservative prompt loaded by default.
- `llm_pentest.md` — verbatim port of the
  [DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI/blob/main/src/llm_detector.py)
  system prompt. Tuned for security-engagement output (cracked-password
  artifacts, NetBIOS names, K8s namespace conventions, pentest tool noise
  to ignore, etc.).

To swap in one of those (or your own — extra entity types, domain-specific
guidance, a different language), set `LLM_SYSTEM_PROMPT_PATH`. Two forms
are accepted:

- `bundled:<filename>` — a file shipped inside the package, e.g.
  `bundled:llm_pentest.md`. Resolved via `importlib.resources`, so the env
  var is independent of the Python version embedded in the site-packages
  path.
- A regular filesystem path — typically a mounted volume:

```bash
# Use the bundled pentest prompt — no path-juggling needed.
podman run --rm -p 8000:8000 \
  -e LLM_SYSTEM_PROMPT_PATH=bundled:llm_pentest.md \
  anonymizer-guardrail:latest

# Or mount your own:
podman run --rm -p 8000:8000 \
  -v $PWD/my_prompt.md:/etc/anonymizer/prompt.md:ro \
  -e LLM_SYSTEM_PROMPT_PATH=/etc/anonymizer/prompt.md \
  anonymizer-guardrail:latest
```

The prompt is loaded once at startup; restart the container to pick up
edits. A missing/unreadable override path is a hard error rather than a
silent fall-back to the bundled prompt — if you set the variable, we
assume you mean it.

### Surrogate salt (privacy hardening)

Surrogate generation uses keyed BLAKE2b under the hood — both the seed for
Faker and the opaque-token digest. The key is taken from `SURROGATE_SALT`,
which defaults to a fresh 16-byte random value at process start.

**Why it matters:** without keying, the opaque-token surrogate is literally
a hash of the original (`[IPV4_ADDRESS_…]`). For low-entropy entity types
— IPs, phones, MACs, names from a known list — an attacker with access to
the surrogates (model-provider logs, LiteLLM access logs, anyone reading
the upstream traffic) can pre-compute hashes for plausible inputs and
recover the originals offline. The keyed hash defeats this: without the
salt, candidate hashes don't match.

**Defaults are safe.** If you set nothing, you get a fresh random 128-bit
salt every restart. Surrogates from before a restart are uncorrelatable
with surrogates from after.

**Set `SURROGATE_SALT` to a stable string** if you want surrogates to
remain stable across restarts — useful for log-correlation analysis but
gives up the brute-force protection if the salt leaks. Pick one per
deployment; never share between unrelated environments.

```bash
# Default: process-random salt, strongest privacy.
podman run anonymizer-guardrail:latest

# Stable surrogates across restarts (operator opted in):
podman run -e SURROGATE_SALT=$(openssl rand -hex 32) anonymizer-guardrail:latest
```

### Disabling Faker

Set `USE_FAKER=false` to replace every realistic surrogate with an opaque
deterministic token (`alice` → `[PERSON_AE708E5D]`, `acmecorp` →
`[ORGANIZATION_77116DCC]`). The token's prefix still encodes the entity type so the
upstream model can tell categories apart, but no Faker output ever reaches
it. Useful when:

- Realistic surrogates would mislead a downstream tool (e.g. an automation
  that grep's for company names in the model's response).
- You want a hard guarantee that the model never sees plausibly-real PII.
- Faker-related behaviour is causing trouble and you want to take it out
  of the loop entirely (Faker isn't even instantiated in this mode).

The opaque tokens are still deterministic, so the same input always maps
to the same surrogate within a process — round-trip restoration works
identically.

### Localising surrogates

Set `FAKER_LOCALE` to control the locale Faker uses for generated names,
companies, addresses, phone numbers, etc. Examples:

```bash
-e FAKER_LOCALE=pt_BR              # Brazilian Portuguese
-e FAKER_LOCALE=de_DE              # German
-e FAKER_LOCALE=ja_JP              # Japanese
-e FAKER_LOCALE=pt_BR,en_US        # try pt_BR first, fall back to en_US
                                   # for providers it doesn't implement
```

Empty (the default) means Faker's own default (`en_US`). Invalid locales
fail at startup with a clear message — the
[Faker docs](https://faker.readthedocs.io/) list every supported one.
Surrogates for opaque-token types (`HASH`, `JWT`, `CREDENTIAL`, etc.) are
unaffected; locale only changes the realistic-substitute types
(`PERSON`, `ORGANIZATION`, `EMAIL_ADDRESS`, …).

### Customising the regex patterns

The deterministic patterns live in
`src/anonymizer_guardrail/patterns/regex_default.yaml`. Two files ship with
the package:

- `regex_default.yaml` — small, conservative, low-FP set (loaded by default).
- `regex_pentest.yaml` — `extends: regex_default.yaml` plus all 173 patterns
  ported verbatim from
  [DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI/blob/main/src/regex_detector.py)
  (cloud creds, NTDS dumps, hashcat output, Pacu, Volatility, BloodHound,
  K8s secrets, Slack/Teams formats, AD CS templates, etc.). Tuned for
  pentest output and noisy in non-security contexts — opt in deliberately.

`REGEX_PATTERNS_PATH` accepts the same two forms as
`LLM_SYSTEM_PROMPT_PATH`: `bundled:<filename>` for in-package files, or a
filesystem path. To start from the pentest set:

```bash
podman run --rm -p 8000:8000 \
  -e REGEX_PATTERNS_PATH=bundled:regex_pentest.yaml \
  anonymizer-guardrail:latest
```

Or supply your own file. Each entry is `{type, pattern, flags?}`; `extends:`
inherits another file's patterns (bare filename → bundled lookup, path with
`/` → on-disk). Your file's own patterns load **first**, then the inherited
chain — child-overrides-parent semantics, so a stricter local pattern wins
over a looser one inherited from default. When a pattern declares one or more capturing groups, the
first non-None group's span is treated as the entity (lets a labeled
pattern like `password:\s+(\S+)` anonymize only the value, not the label).
Patterns without groups still anonymize the full match. All patterns
compile at startup; any bad regex, unknown flag, or unreadable extends
path crashes the boot rather than silently dropping rules.

### Per-request overrides

Five settings can be flipped on a per-call basis via LiteLLM's
`additional_provider_specific_params` field — useful when one model
needs a different detection mode, locale, or anonymization model than
the deployment-wide default.

| Override key | Type | Effect |
|---|---|---|
| `use_faker` | `bool` | Switch Faker on/off for this call. False forces opaque `[TYPE_…]` tokens. |
| `faker_locale` | `string` or `string[]` | Override `FAKER_LOCALE`. Accepts `"pt_BR,en_US"` or `["pt_BR", "en_US"]` — first locale is primary, rest are fallbacks. |
| `detector_mode` | `string` or `string[]` | **Subset filter** over the detectors built at startup. Override naming a detector that wasn't configured logs a warning and is dropped (privacy_filter and llm both need startup-time setup that isn't reversible per call). |
| `regex_overlap_strategy` | `"longest"` or `"priority"` | Override `REGEX_OVERLAP_STRATEGY` for this call. |
| `regex_patterns` | `string` | Name of a registered alternative regex pattern set (see *Named alternatives* below). |
| `llm_model` | `string` | Override `LLM_MODEL` (the alias the LLM detector sends to its backend) for this call. |
| `llm_prompt` | `string` | Name of a registered alternative LLM detection prompt (see *Named alternatives* below). |

**Validation policy:** unknown keys are silently ignored;
known-key bad values log a warning and are dropped. The other
overrides in the same dict still take effect, and the request is
**not** blocked over a bad override — anonymization proceeds with
config defaults for whatever was rejected.

**Defensive limits (anti-OOM):**

| Limit | Default | What it bounds |
|---|---|---|
| `faker_locale` chain length | 3 | Maximum locales in a single `faker_locale` value. A request asking for 50 locales is malformed and inflates Faker construction time. Hardcoded — primary + 1–2 fallbacks is the realistic shape. |
| `detector_mode` list length | 3 | Three detector implementations exist (`regex`, `privacy_filter`, `llm`); anything longer cannot be valid. Hardcoded. |
| `SURROGATE_FAKER_LRU_MAX` | `32` | LRU cap on the per-locale Faker instance cache. Each Faker is a few MB resident (provider dictionaries); without a bound, a caller cycling distinct locale tuples could grow memory unboundedly. On overflow, the least-recently-used Faker is dropped and reconstructed on next use (~1–2 ms). Override at startup if your deployment legitimately serves many distinct locale combos. |

When a length cap is exceeded, the override is dropped (warn-and-drop)
and the request falls back to the configured default for that key —
the request itself isn't blocked. The Faker LRU cap is invisible to
callers; eviction never produces a wrong surrogate, just adds a one-
time reconstruction cost on the next request that misses.

**Cache impact:** `use_faker` and `faker_locale` extend the
surrogate cache key from `(entity_type, text)` to
`(entity_type, text, use_faker, locale)`. Different combos coexist
in the cache, each consistent within its bucket. Default-config
traffic still buckets to the original key shape — no migration cost.
The surrogate cache itself remains bounded by `SURROGATE_CACHE_MAX_SIZE`.

**Named alternatives (`regex_patterns`, `llm_prompt`):**

`regex_patterns` and `llm_prompt` deliberately accept *names*, not
paths. Letting callers pass arbitrary paths over the wire would be a
path-traversal + bypass vector (e.g. an empty YAML disables redaction).
Operators pre-declare the allowed alternatives via two env vars:

```bash
REGEX_PATTERNS_REGISTRY="pentest=bundled:regex_pentest.yaml,internal=/etc/anon/internal.yaml"
LLM_SYSTEM_PROMPT_REGISTRY="pentest=bundled:llm_pentest.md,legal=/etc/anon/legal.md"
```

Format: comma-separated `name=path` pairs (whitespace around `=` and
`,` is stripped). Each path uses the same `bundled:NAME` /
filesystem-path syntax as `REGEX_PATTERNS_PATH` /
`LLM_SYSTEM_PROMPT_PATH`. Validation:

- All entries are loaded + compiled at startup. Typos / unreadable
  files / empty prompts crash boot loudly with the offending entry
  named (`REGEX_PATTERNS_REGISTRY[pentest]=…`).
- The reserved name `default` is rejected — the default lives in the
  matching `*_PATH` env var, never in the registry, so adding a
  registry can never silently change no-override behaviour.

A request referencing an unknown name (e.g. `regex_patterns: "wrong"`)
logs a warning and falls back to the default pattern set or prompt —
the request itself isn't blocked.

**Where to set them:**

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

### Regex overlap resolution

When two patterns from the loaded YAML match overlapping spans, the
`REGEX_OVERLAP_STRATEGY` env var picks the winner:

- **`longest`** *(default)* — the longer span wins. Ties broken by
  earliest start, then by YAML order. Recommended whenever you load
  the pentest set or any other large pattern bundle, where a narrow
  pattern in one file can accidentally match a substring of a wider
  pattern in another. Concretely, the pentest YAML's `\b\d{12}\b` AWS
  Account ID pattern would otherwise eat the trailing 12-digit group
  of any UUID whose last segment is all-digits, leaving the regex
  layer with only the inner span instead of the whole UUID.
- **`priority`** — first pattern in YAML order wins (the pre-v0.2
  behaviour). Useful when patterns are deliberately ordered
  most-specific-first and that ordering is load-bearing.

Both strategies pay the same regex cost: every pattern still scans the
text via `finditer` (Python's `re` engine has no API to skip
already-claimed regions). The strategy only changes how candidate
matches are resolved at adoption time.

## Run it

### Quick start (scripts)

Two helper scripts live under `scripts/`. They wrap `podman build` /
`podman run` with sensible defaults, the `--format=docker` quirk for
HEALTHCHECK preservation, the volume + shared-network plumbing, and
auto-start of the fake-llm test backend.

```bash
# Build all four image flavours (slim, pf, pf-baked, fake-llm).
# Pass -t to build a single one (e.g. -t slim).
scripts/build-image.sh -t all

# Interactive launcher — single-screen menuconfig-style UI, every
# setting visible at once, drill in to edit, hit Launch.
scripts/menu.sh

# Flag-driven launcher with bundled presets:
scripts/cli.sh --preset uuid-debug      # slim + regex,llm + fake-llm + LOG_LEVEL=debug
scripts/cli.sh --preset pentest         # pf + regex,privacy_filter,llm + pentest patterns/prompt + fake-llm
scripts/cli.sh --preset regex-only      # slim + regex only — no LLM creds needed

# Exercise the curl recipes against a running guardrail
# (or pass --preset to spin one up + tear it down):
scripts/test-examples.sh --preset uuid-debug
```

When the chosen `DETECTOR_MODE` includes `llm` and the LLM backend is
set to `fake-llm`, the launcher boots the fake-llm container in the
background on a shared `anonymizer-net`, waits for `/health`, and
points the guardrail at `http://fake-llm:4000/v1`. fake-llm matches
incoming chat-completion requests against a YAML rules file
(`fake-llm/rules.example.yaml` by default; `--rules PATH` overrides),
which is what makes the test recipes deterministic. See `fake-llm/README.md`
for the rules schema.

### Image flavours

Three image flavours, controlled by two build-args, sharing one
`Containerfile`:

| flavour | size | model | when to pick it |
|---|---|---|---|
| slim | ~200 MB | n/a | DETECTOR_MODE never includes `privacy_filter` |
| privacy-filter (runtime download) | ~1.3 GB | downloads on first container start | most deployments — pair with a named volume |
| privacy-filter (model baked in) | ~6.9 GB | shipped inside image | air-gapped or strict cold-start latency |

(Sizes assume the default CPU-only PyTorch build. Override with
`--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121` if
you're deploying behind GPUs; expect ~4 GB extra on top — that's roughly
how much `nvidia-cuda-runtime`, `nvidia-cudnn`, `nvidia-cublas`, etc.
weigh on Linux x86.)

### Building manually

`scripts/build-image.sh` is the recommended path; the equivalent raw
commands are:

```bash
# 1) Slim — no ML deps.
podman build --format=docker -t anonymizer-guardrail:latest -f Containerfile .

# 2) Privacy-filter, runtime download — small image, downloads ~6 GB on
#    first container start. Mount a NAMED VOLUME so subsequent starts
#    skip the download (see below).
podman build --format=docker -t anonymizer-guardrail:privacy-filter \
    --build-arg WITH_PRIVACY_FILTER=true -f Containerfile .

# 3) Privacy-filter, model baked into image — self-contained, no runtime
#    network, at the cost of a much larger image (size in the table above).
podman build --format=docker -t anonymizer-guardrail:privacy-filter-baked \
    --build-arg WITH_PRIVACY_FILTER=true \
    --build-arg BAKE_PRIVACY_FILTER_MODEL=true \
    -f Containerfile .
```

`--format=docker` is needed because podman defaults to OCI image
format, which doesn't include a HEALTHCHECK field — without the flag,
the `HEALTHCHECK` directive in the Containerfile is silently dropped
and `podman healthcheck run` won't work. `docker build` always emits
Docker format, so the flag is podman-specific (and `build-image.sh`
adds it conditionally).

### Running manually

Slim or baked images run without any volume:

```bash
podman run --rm -p 8000:8000 \
  -e LLM_API_BASE=http://litellm:4000/v1 \
  -e LLM_API_KEY=sk-litellm-master \
  -e LLM_MODEL=anonymize \
  --name anonymizer anonymizer-guardrail:latest
```

The runtime-download image **needs** a persistent volume at
`/app/.cache/huggingface` — without one, every `podman run` re-downloads
the ~6 GB. Use a named volume:

```bash
podman volume create anonymizer-hf-cache

podman run --rm -p 8000:8000 \
  -e DETECTOR_MODE=regex,privacy_filter,llm \
  -e LLM_API_BASE=http://litellm:4000/v1 \
  -e LLM_API_KEY=sk-litellm-master \
  -v anonymizer-hf-cache:/app/.cache/huggingface \
  --name anonymizer anonymizer-guardrail:privacy-filter
```

First `podman run` of the privacy-filter image takes a few minutes (the
model downloads into the volume, blocking app startup). The container's
healthcheck has a 300-second start-period to accommodate this — slower
networks may need a longer override via `--health-start-period`.
Subsequent runs reuse the volume and start in seconds.

Volume options compared:

- **Named volume** (`-v anonymizer-hf-cache:/app/.cache/huggingface`):
  recommended. Auto-managed by Podman/Docker; survives `podman rm`.
- **Bind mount** (`-v /host/path:/app/.cache/huggingface`): same effect
  but stores the cache wherever you point it on the host. Useful if you
  want the files visible outside Podman's volume store.
- **Kubernetes**: mount a `PersistentVolumeClaim` at the same path —
  first pod pays the download; later pods reuse the PVC. Use
  `ReadWriteMany` for shared cache across replicas.

### Smoke test

```bash
curl -fsS http://localhost:8000/health
```

For end-to-end curl recipes covering the round-trip, every detector
category, multi-text batches, and a kitchen-sink payload, see
[`examples.md`](examples.md). To run those recipes as automated
assertions: `scripts/test-examples.sh` (with `--preset NAME` to
self-host a test guardrail).

## Development

```bash
pip install -e ".[dev]"
pytest                                # unit tests, no container needed
uvicorn anonymizer_guardrail.main:app --reload
```

End-to-end testing of the curl recipes against an actual container
(builds + runs + asserts via `cli.sh --preset`):

```bash
scripts/test-examples.sh --preset uuid-debug   # slim + regex,llm + fake-llm
scripts/test-examples.sh --preset pentest      # pf + privacy_filter + pentest config
scripts/test-examples.sh                       # connect to BASE_URL (already-running guardrail)
```

## Limitations

- **Single replica.** The vault is in-memory; mappings written on one replica
  aren't visible from another. For multi-replica deployments, swap `Vault`
  for a Redis-backed implementation — the interface is two methods.
- **No streaming.** LiteLLM's guardrail calls are pre/post; streaming responses
  are deanonymized after assembly. If you need to anonymize partial chunks,
  this isn't the right tool.

## Acknowledgements

The dual-layer (regex + LLM) round-trip anonymization approach is borrowed from
[DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI), a self-contained
reverse proxy aimed at protecting client data during AI-assisted pentesting.
This project re-shapes the same idea as a LiteLLM Generic Guardrail so it can
sit in front of any model LiteLLM supports rather than a single provider.

## License

MIT.
