# anonymizer-guardrail

A small FastAPI service that implements LiteLLM's
[Generic Guardrail API](https://docs.litellm.ai/docs/adding_provider/generic_guardrail_api)
to perform reversible anonymization of LLM traffic.

LiteLLM calls this service before forwarding a request upstream
(`input_type="request"`) to anonymize sensitive substrings, then again after
the upstream model responds (`input_type="response"`) to deanonymize them. A
short-lived mapping keyed by `litellm_call_id` connects the two sides of the
round-trip — in-memory by default; Redis-backed when running multi-replica
(see [docs/vault.md](docs/vault.md#backends)).

## Quick start

```bash
# Build all images (guardrail + privacy-filter-service + fake-llm).
scripts/image_builder.sh --preset all

# Interactive launcher.
scripts/launcher.sh --ui

# Or flag-driven with a preset:
scripts/launcher.sh --preset uuid-debug      # guardrail + regex,llm + fake-llm
scripts/launcher.sh --preset pentest         # guardrail + regex,privacy_filter,llm + pf-service + fake-llm + pentest config
scripts/launcher.sh --preset regex-only      # guardrail + regex only — no LLM creds needed
```

For LiteLLM wiring, see [docs/litellm-integration.md](docs/litellm-integration.md).

## Deployment topology

```
            ┌─────────────────────────────────────────────────────────┐
            │  Detection layer (parallel, request-side only)          │
            │    regex / denylist          in-process, microseconds   │
            │    privacy-filter-service    HTTP sidecar               │
            │    gliner-pii-service        HTTP sidecar               │
            │    LLM detector ──────→ LiteLLM (anonymize alias) *     │
            └─────────────────▲───────────────────────────────────────┘
                              │
                              │ pre_call:  detect → replace with surrogates
                              │ post_call: vault lookup → restore originals
                              │
   client ──→ LiteLLM ────────┴────→ anonymizer-guardrail
                 │
                 └──→ upstream LLM (OpenAI / Anthropic / local / fake-llm)

   *  the `anonymize` alias must NOT have the guardrail attached,
      otherwise every detection call would re-enter and recurse forever.
```

LiteLLM is the entry point and calls the guardrail twice per request:
once on the way in (`pre_call` → anonymize), once on the way back
(`post_call` → deanonymize via the in-memory vault). Detection runs
only on the request side; the response side is a pure substring-replace
from the vault keyed by `litellm_call_id`. See
[docs/deployment.md](docs/deployment.md) for image / sidecar details
and [docs/limitations.md](docs/limitations.md) for the single-replica
implication of the in-memory vault.

## What it does

Five detection layers, all optional, controlled by `DETECTOR_MODE`:

- **[regex](docs/detectors/regex.md)** — high-precision patterns for
  IPs, emails, hashes, JWTs, cloud creds. Stateless, microseconds.
- **[denylist](docs/detectors/denylist.md)** — literal-string match
  against an operator-supplied YAML list (employee names, project
  codenames, customer IDs).
- **[privacy_filter](docs/detectors/privacy-filter.md)** — NER
  (`openai/privacy-filter`) via a standalone HTTP service container.
  8 PII categories.
- **[gliner_pii](docs/detectors/gliner-pii.md)** — remote zero-shot
  NER (`nvidia/gliner-pii`). Caller-supplied label set per request.
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
| **Architecture overview** — components, request lifecycle, layout | [docs/architecture.md](docs/architecture.md) |
| **Detector overview + comparison** | [docs/detectors/](docs/detectors/index.md) |
| Per-detector deep dives | [regex](docs/detectors/regex.md), [denylist](docs/detectors/denylist.md), [privacy-filter](docs/detectors/privacy-filter.md), [gliner-pii](docs/detectors/gliner-pii.md), [llm](docs/detectors/llm.md) |
| Cross-cutting env vars (HTTP, surrogate, vault) | [docs/configuration.md](docs/configuration.md) |
| Deployment shapes — picking config for your use case | [docs/deployment-shapes.md](docs/deployment-shapes.md) |
| Per-request overrides | [docs/per-request-overrides.md](docs/per-request-overrides.md) |
| LiteLLM wiring | [docs/litellm-integration.md](docs/litellm-integration.md) |
| Surrogate generation, Faker, salt, locales | [docs/surrogates.md](docs/surrogates.md) |
| Vault — round-trip mapping, TTL, size cap | [docs/vault.md](docs/vault.md) |
| Caching — pipeline / per-detector / surrogate / vault layers | [docs/operations.md](docs/operations.md#detector-result-caching) |
| Observability — `/health`, in-flight gauges | [docs/operations.md](docs/operations.md) |
| Container images, build, run | [docs/deployment.md](docs/deployment.md) |
| Curl recipes | [docs/examples.md](docs/examples.md) |
| Detector quality benchmark | [docs/detector-bench.md](docs/detector-bench.md) |
| Cache effectiveness benchmark — script | [docs/cache-bench.md](docs/cache-bench.md) |
| Cache effectiveness benchmark — committed results | [docs/benchmark/cache-bench/](docs/benchmark/cache-bench/report.md) |
| Detector-service latency benchmark (CPU + GPU) | [docs/service-bench.md](docs/service-bench.md) |
| Limitations — single replica, no streaming deanonymization | [docs/limitations.md](docs/limitations.md) |
| Contributor guide | [docs/development.md](docs/development.md) |
| Design decisions — paths considered and declined | [docs/design-decisions.md](docs/design-decisions.md) |

## Acknowledgements

The dual-layer (regex + LLM) round-trip anonymization approach is borrowed from
[DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI), a self-contained
reverse proxy aimed at protecting client data during AI-assisted pentesting.
This project re-shapes the same idea as a LiteLLM Generic Guardrail so it can
sit in front of any model LiteLLM supports rather than a single provider.

## License

MIT.
