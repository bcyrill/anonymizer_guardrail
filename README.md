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
type — `acmecorp.local` → `quasarware.local`, not `[ORG_7F3A2B]` —
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
| `LLM_MODEL`       | `anonymize`                   | Model alias used for detection           |
| `LLM_TIMEOUT_S`   | `30`                          |                                          |
| `LLM_MAX_CHARS`   | `200000`                      | Hard cap; inputs above this are refused  |
| `VAULT_TTL_S`     | `600`                         | Drops mappings whose post_call never came |
| `FAIL_CLOSED`     | `true`                        | Block requests if LLM detector errors    |

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
- **String-replace anonymization.** Substitution is `str.replace`-based with
  longest-first ordering. Edge cases with deeply nested overlapping spans can
  produce odd results; for those, consider a span-based rewriter.
- **No streaming.** LiteLLM's guardrail calls are pre/post; streaming responses
  are deanonymized after assembly. If you need to anonymize partial chunks,
  this isn't the right tool.

## License

MIT.
