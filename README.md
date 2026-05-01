# anonymizer-guardrail

A small FastAPI service that implements LiteLLM's
[Generic Guardrail API](https://docs.litellm.ai/docs/adding_provider/generic_guardrail_api)
to perform reversible anonymization of LLM traffic.

LiteLLM calls this service before forwarding a request upstream
(`input_type="request"`) to anonymize sensitive substrings, then again after
the upstream model responds (`input_type="response"`) to deanonymize them. A
short-lived in-memory mapping keyed by `litellm_call_id` connects the two
sides of the round-trip.

## Quick start

```bash
# Build all images (slim guardrail + privacy-filter-service + fake-llm).
scripts/build-image.sh -t all

# Interactive launcher.
scripts/menu.sh

# Or flag-driven with a preset:
scripts/cli.sh --preset uuid-debug      # slim + regex,llm + fake-llm
scripts/cli.sh --preset pentest         # pf + privacy_filter,llm + pentest config
scripts/cli.sh --preset regex-only      # slim + regex only — no LLM creds needed
```

For LiteLLM wiring, see [docs/litellm-integration.md](docs/litellm-integration.md).

## What it does

Five detection layers, all optional, controlled by `DETECTOR_MODE`:

- **[regex](docs/detectors/regex.md)** — high-precision patterns for
  IPs, emails, hashes, JWTs, cloud creds. Stateless, microseconds.
- **[denylist](docs/detectors/denylist.md)** — literal-string match
  against an operator-supplied YAML list (employee names, project
  codenames, customer IDs).
- **[privacy_filter](docs/detectors/privacy-filter.md)** — local NER
  (`openai/privacy-filter`), in-process or via a remote HTTP service.
  8 PII categories.
- **[gliner_pii](docs/detectors/gliner-pii.md)** — remote zero-shot
  NER (`nvidia/gliner-pii`). Caller-supplied label set per request.
  **Experimental.**
- **[llm](docs/detectors/llm.md)** — OpenAI-compatible Chat Completions
  in JSON mode. Catches contextual entities regex can't.

When multiple detectors are configured, they run in parallel and the
matches are merged and deduped. Order in `DETECTOR_MODE` determines
type-resolution priority — see
[detectors](docs/detectors/index.md) for the comparison table and how
to combine them.

Detected entities are replaced with deterministic surrogates — by
default realistic substitutes from
[Faker](https://faker.readthedocs.io/), opaque tokens for things
where realism would mislead. See [surrogates](docs/surrogates.md).

## Documentation

| Topic | Where |
|---|---|
| **Detector overview + comparison** | [docs/detectors/](docs/detectors/index.md) |
| Per-detector deep dives | [regex](docs/detectors/regex.md), [denylist](docs/detectors/denylist.md), [privacy-filter](docs/detectors/privacy-filter.md), [gliner-pii](docs/detectors/gliner-pii.md), [llm](docs/detectors/llm.md) |
| Cross-cutting env vars (HTTP, surrogate, vault) | [docs/configuration.md](docs/configuration.md) |
| Per-request overrides | [docs/per-request-overrides.md](docs/per-request-overrides.md) |
| LiteLLM wiring | [docs/litellm-integration.md](docs/litellm-integration.md) |
| Surrogate generation, Faker, salt, locales | [docs/surrogates.md](docs/surrogates.md) |
| Vault — round-trip mapping, TTL, size cap | [docs/vault.md](docs/vault.md) |
| Observability — `/health`, in-flight gauges | [docs/operations.md](docs/operations.md) |
| Container images, build, run | [docs/deployment.md](docs/deployment.md) |
| Curl recipes | [docs/examples.md](docs/examples.md) |
| Detector quality benchmark | [docs/benchmark.md](docs/benchmark.md) |
| Limitations — single replica, no streaming | [docs/limitations.md](docs/limitations.md) |
| Contributor guide | [docs/development.md](docs/development.md) |

## Acknowledgements

The dual-layer (regex + LLM) round-trip anonymization approach is borrowed from
[DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI), a self-contained
reverse proxy aimed at protecting client data during AI-assisted pentesting.
This project re-shapes the same idea as a LiteLLM Generic Guardrail so it can
sit in front of any model LiteLLM supports rather than a single provider.

## License

MIT.
