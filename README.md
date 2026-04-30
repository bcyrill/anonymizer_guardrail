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

Two layers, both optional, controlled by `DETECTOR_MODE` (`regex` | `llm` | `both`):

- **regex** — high-precision patterns for things with recognizable shapes:
  IPs, CIDRs, emails, hashes, JWTs, AWS keys, GitHub tokens, OpenAI-style
  keys, internal hostnames (`*.local`, `*.internal`, etc.). Stateless,
  deterministic, no external dependencies.

- **llm** — calls an OpenAI-compatible Chat Completions endpoint with a
  JSON-mode prompt that asks the model to enumerate sensitive entities.
  Catches contextual stuff regex cannot: org names, personal names,
  internal product/project codenames embedded in prose.

In `both` mode the regex matches and LLM matches are merged and deduped.

## Surrogates

Each detected entity is replaced with a *realistic* substitute of the same
type — `acmecorp.local` → `quasarware.local`, not `[ORGANIZATION_7F3A2B]` —
generated with [Faker](https://faker.readthedocs.io/) seeded by a hash of
the original. Determinism means the same input always maps to the same
surrogate within a process, so the upstream model sees consistent
substitutions across multi-turn conversations.

Opaque tokens are still used for things where realism would be misleading
(`HASH`, `JWT`, `CREDENTIAL`, `TOKEN`, `AWS_ACCESS_KEY`).

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
| `DETECTOR_MODE`   | `both`                        | `regex` / `llm` / `both`                 |
| `LLM_API_BASE`    | `http://litellm:4000/v1`      | OpenAI-compatible endpoint               |
| `LLM_API_KEY`     | *(empty)*                     | Bearer token if the endpoint needs one   |
| `LLM_USE_FORWARDED_KEY` | `false`                 | Use the caller's Authorization header (see below) |
| `LLM_SYSTEM_PROMPT_PATH` | *(empty)*              | Override the bundled detection prompt    |
| `REGEX_PATTERNS_PATH` | *(empty)*                 | Override the bundled regex patterns YAML |
| `FAKER_LOCALE`    | *(empty → en_US)*             | Faker locale, e.g. `pt_BR` or `pt_BR,en_US` |
| `USE_FAKER`       | `true`                        | When false, all surrogates are opaque tokens |
| `SURROGATE_CACHE_MAX_SIZE` | `100000`             | LRU cap on the surrogate cache (cross-request consistency) |
| `LLM_MODEL`       | `anonymize`                   | Model alias used for detection           |
| `LLM_TIMEOUT_S`   | `30`                          |                                          |
| `LLM_MAX_CHARS`   | `200000`                      | Hard cap; inputs above this are refused  |
| `VAULT_TTL_S`     | `600`                         | Drops mappings whose post_call never came |
| `FAIL_CLOSED`     | `true`                        | Block requests if LLM detector errors    |

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
`/` → on-disk). When a pattern declares one or more capturing groups, the
first non-None group's span is treated as the entity (lets a labeled
pattern like `password:\s+(\S+)` anonymize only the value, not the label).
Patterns without groups still anonymize the full match. All patterns
compile at startup; any bad regex, unknown flag, or unreadable extends
path crashes the boot rather than silently dropping rules. Order matters
— first match wins on overlapping spans.

## Run it

```bash
podman build -t anonymizer-guardrail:latest -f Containerfile .
podman run --rm -p 8000:8000 \
  -e LLM_API_BASE=http://litellm:4000/v1 \
  -e LLM_API_KEY=sk-litellm-master \
  -e LLM_MODEL=anonymize \
  --name anonymizer anonymizer-guardrail:latest
```

Smoke test:

```bash
curl -fsS http://localhost:8000/health
```

## Development

```bash
pip install -e ".[dev]"
pytest                                # regex-only tests, no LLM needed
uvicorn anonymizer_guardrail.main:app --reload
```

## Limitations

- **Single replica.** The vault is in-memory; mappings written on one replica
  aren't visible from another. For multi-replica deployments, swap `Vault`
  for a Redis-backed implementation — the interface is two methods.
- **Single pass anonymization.** Substitution uses a single-pass regex approach
  to avoid transitive replacements. Longest keys match first to ensure
  correctness when one sensitive entity is a substring of another.
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
