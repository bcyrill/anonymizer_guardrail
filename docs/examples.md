# Example requests

Curl recipes for exercising the guardrail end-to-end. They assume the
container is reachable on `http://localhost:8000` (the default; change
`PORT` if you started it elsewhere) and that you've started it with
whichever `DETECTOR_MODE` matches the example you want to try:

```bash
# Regex-only (covers most examples below).
scripts/launcher.sh -t slim -d regex --no-faker

# Add the privacy-filter NER for the address / name examples.
scripts/launcher.sh -t pf -d regex,privacy_filter

# Full stack, requires LLM_API_BASE / LLM_API_KEY for the LLM examples.
scripts/launcher.sh -t pf -d regex,privacy_filter,llm \
  --llm-backend external \
  --llm-api-base http://litellm:4000/v1 \
  --llm-api-key sk-litellm-master \
  --llm-model anonymize
```

The `--no-faker` flag in the first line makes surrogates opaque
(`[PERSON_AB12CD34]`) so it's obvious which substrings the guardrail
caught — handy for inspection. Drop it for realistic Faker output.

The endpoint is a single POST. `input_type` selects the direction:

- `"request"` — anonymize, store mapping under `litellm_call_id`
- `"response"` — deanonymize using the stored mapping, then evict it

Pretty-print responses by piping into `python -m json.tool`.

## Health

```bash
curl -s http://localhost:8000/health | python -m json.tool
```

```json
{
  "status": "ok",
  "detector_mode": "regex",
  "vault_size": 0,
  "surrogate_cache_size": 0,
  "surrogate_cache_max": 100000,
  "llm_in_flight": 0,
  "llm_max_concurrency": 10
}
```

The cache + concurrency caps come from `SURROGATE_CACHE_MAX_SIZE`
and `LLM_MAX_CONCURRENCY` — useful for an ops dashboard that wants
to alert on saturation (`llm_in_flight` approaching
`llm_max_concurrency`) without having to know the configured cap
separately.

## Anonymize / deanonymize round-trip

The same `litellm_call_id` ties the two calls together. Step 1 stores the
mapping, step 2 reverses the substitution and evicts it.

```bash
# 1. Anonymize the user's prompt before LiteLLM forwards it upstream.
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["Email me at alice.smith@example.com about ticket #42."],
    "input_type": "request",
    "litellm_call_id": "demo-roundtrip-1"
  }' | python -m json.tool
```

```json
{
  "action": "GUARDRAIL_INTERVENED",
  "texts": ["Email me at [EMAIL_ADDRESS_…] about ticket #42."]
}
```

```bash
# 2. Deanonymize the upstream model's reply, using the same call_id.
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["I will email [EMAIL_ADDRESS_…] right away."],
    "input_type": "response",
    "litellm_call_id": "demo-roundtrip-1"
  }' | python -m json.tool
```

The response replaces the surrogate with `alice.smith@example.com`. Use
the actual surrogate that came back from step 1 when you replay this —
it's salted and changes per process restart.

## No matches → `action: NONE`

If nothing in the texts looks sensitive, the guardrail returns `NONE`
and LiteLLM passes the request through unchanged.

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["What is the capital of France?"],
    "input_type": "request",
    "litellm_call_id": "demo-empty-1"
  }' | python -m json.tool
```

## Regex layer — entity-type showcase

Each example is the smallest text that triggers exactly one regex
pattern. Run them with `DETECTOR_MODE=regex` (or any mode that includes
`regex`).

### Emails and phones

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["Reach me at jane.doe+work@corp.example or +1-415-555-0142."],
    "input_type": "request",
    "litellm_call_id": "demo-contact"
  }' | python -m json.tool
```

### IPs, CIDR ranges, internal hostnames

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": [
      "The cluster runs on 10.0.42.7 inside 10.0.0.0/16, fronted by api.acme.internal."
    ],
    "input_type": "request",
    "litellm_call_id": "demo-network"
  }' | python -m json.tool
```

CIDR matches first, then bare IPs — overlapping spans are resolved by
the order patterns appear in `regex_default.yaml`.

### Cloud / SaaS credentials

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": [
      "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz OPENAI_KEY=sk-proj-AbCdEfGhIjKlMnOpQrStUvWx SLACK_TOKEN=xoxb-1234567890-0987654321-aBcDeFgHiJkLmNoPqRsTuVwX"
    ],
    "input_type": "request",
    "litellm_call_id": "demo-creds"
  }' | python -m json.tool
```

These all map to opaque tokens (`[TOKEN_…]`, `[AWS_ACCESS_KEY_…]`) even
when Faker is enabled — Faker producing a "realistic" credential
surrogate would be misleading.

### JWT

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUgRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"],
    "input_type": "request",
    "litellm_call_id": "demo-jwt"
  }' | python -m json.tool
```

### Hashes (MD5 / SHA-1 / SHA-256)

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": [
      "MD5 5d41402abc4b2a76b9719d911017c592, SHA-1 aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d, SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855."
    ],
    "input_type": "request",
    "litellm_call_id": "demo-hashes"
  }' | python -m json.tool
```

Longest first, so the SHA-256 isn't truncated to a SHA-1 prefix.

### UUID

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["Order id 550e8400-e29b-41d4-a716-446655440000 failed."],
    "input_type": "request",
    "litellm_call_id": "demo-uuid"
  }' | python -m json.tool
```

### Credit card and IBAN

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": [
      "Card 4111-1111-1111-1111 charged to IBAN DE89370400440532013000."
    ],
    "input_type": "request",
    "litellm_call_id": "demo-finance"
  }' | python -m json.tool
```

### Date of birth (label-anchored)

The DOB pattern requires a label like `DOB:` or `Date of Birth:` —
unlabelled dates aren't reliably PII (they could be timestamps,
versions, etc.). The capture group narrows the surrogate to the value,
so the label is preserved.

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["DOB: 1985-04-15. Patient lookup pending."],
    "input_type": "request",
    "litellm_call_id": "demo-dob"
  }' | python -m json.tool
```

## Denylist — known sensitive terms

Requires `DETECTOR_MODE` to include `denylist` and a YAML file at
`DENYLIST_PATH`. Use this when you have a stable, well-known set of
sensitive strings (employee names, project codenames, customer
identifiers) — pure dictionary lookup is more reliable than regex
shapes or LLM inference for those.

A small example list:

```yaml
# /etc/anonymizer/deny.yaml
entries:
  - type: ORGANIZATION
    value: AcmeCorp
  - type: PERSON
    value: alice smith
    case_sensitive: false
  - type: IDENTIFIER
    value: project zephyr
    case_sensitive: false
```

Pass it via `--denylist-path`, or set `DENYLIST_PATH` directly:

```bash
scripts/launcher.sh -t slim -d denylist,regex --denylist-path /etc/anonymizer/deny.yaml
```

Then a request mentioning any of the entries gets them flagged:

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["Alice Smith from AcmeCorp shipped Project Zephyr today."],
    "input_type": "request",
    "litellm_call_id": "demo-denylist"
  }' | python -m json.tool
```

Two backends are available — `regex` (default, stdlib alternation,
fast up to low thousands of entries) and `aho` (Aho-Corasick via
`pyahocorasick`, sub-linear in pattern count) — controlled by
`DENYLIST_BACKEND` / `--denylist-backend`. See the README's
*Customising the denylist* section for the full schema.

## Privacy-filter NER — addresses and names

Requires `DETECTOR_MODE` to include `privacy_filter`. The detector
talks HTTP to a standalone `privacy-filter-service` sidecar — set
`PRIVACY_FILTER_URL` on the slim guardrail (or pass
`--privacy-filter-backend service` to auto-start one). See
[docs/detectors/privacy-filter.md](detectors/privacy-filter.md) for
image flavours (cpu / cu130, with optional baked-in weights) and
the device / calibration knobs.

Catches contextual entities the regex layer misses — full names,
street addresses, free-text dates.

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": [
      "Alice Smith lives at 123 Main Street, Springfield, and her DOB is 15 April 1985."
    ],
    "input_type": "request",
    "litellm_call_id": "demo-ner"
  }' | python -m json.tool
```

`Alice Smith` (PERSON), `123 Main Street, Springfield` (ADDRESS), and
the free-text date all get surrogates. The regex layer alone wouldn't
flag any of them.

## LLM detector — organizations and codenames

Requires `DETECTOR_MODE` to include `llm` and a working
`LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL`. The LLM catches
contextual things the other layers can't reason about — internal
project codenames, team names, customer names embedded in prose.

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": [
      "AcmeCorp engineering is rolling out Project Zephyr to the Northstar customer fleet next quarter."
    ],
    "input_type": "request",
    "litellm_call_id": "demo-llm"
  }' | python -m json.tool
```

`AcmeCorp` (ORGANIZATION), `Project Zephyr` (IDENTIFIER), and
`Northstar` (ORGANIZATION or IDENTIFIER, depending on the prompt) get
surrogates.

## Multi-text batch

`texts` is a list — LiteLLM may pack the system prompt, user message,
and tool descriptions into separate entries. The surrogate cache
ensures the same input yields the same surrogate across entries, so
referential consistency is preserved.

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": [
      "You are a helpful assistant. The current user is bob@corp.example.",
      "Hi, can you summarize my last email exchange with bob@corp.example?"
    ],
    "input_type": "request",
    "litellm_call_id": "demo-multi"
  }' | python -m json.tool
```

Both occurrences of `bob@corp.example` map to the same surrogate.

## Combined kitchen-sink request

A bigger payload that exercises every layer at once. Useful for
spot-checking that the merge / dedupe logic is doing the right thing.

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": [
      "Incident report: AcmeCorp engineer Alice Smith (alice.smith@acme.example, +1-415-555-0142, DOB: 1985-04-15) noticed unusual traffic from 10.0.42.7 to api.acme.internal at 03:14 UTC. The attacker appears to have lifted GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz from a public Gist. Affected resource id 550e8400-e29b-41d4-a716-446655440000. Card 4111-1111-1111-1111 was used on the linked account. Suspected internal codename: Project Zephyr."
    ],
    "input_type": "request",
    "litellm_call_id": "demo-kitchen-sink"
  }' | python -m json.tool
```

## Forwarding the caller's API key

When `LLM_USE_FORWARDED_KEY=true`, the guardrail authenticates to the
detection LLM with the bearer token forwarded by LiteLLM. LiteLLM only
forwards the actual value when the guardrail is configured with
`extra_headers: [authorization]`; otherwise it arrives as the literal
string `[present]`.

```bash
curl -s -X POST http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["Customer Bob Jones complained about login latency."],
    "input_type": "request",
    "litellm_call_id": "demo-forwarded-key",
    "request_headers": {
      "authorization": "Bearer sk-user-abc123"
    }
  }' | python -m json.tool
```

Detection cost and rate limits then attribute back to the caller's
virtual key rather than a shared `LLM_API_KEY`.