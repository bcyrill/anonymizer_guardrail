# GLiNER-PII detector

Remote zero-shot NER backed by NVIDIA's
[`nvidia/gliner-pii`](https://huggingface.co/nvidia/gliner-PII) model
— a fine-tune of GLiNER large-v2.1 for PII / PHI detection.

The differentiator vs the [privacy_filter detector](privacy-filter.md)
is **zero-shot labels**: the entity-type list is an input to the model
rather than baked into the architecture. A caller can configure
`["ssn", "medical_record_number", "iban"]` for one deployment and
`["person", "organization"]` for another, no retraining.

**Status: experimental.** The detector is fully functional and tested,
but the gliner-pii-service container images are not yet published to
GHCR — operators evaluate the model locally first via
`scripts/build-image.sh -t gliner-service`. CI publishing follows once
the model graduates out of experimental status.

**Remote only.** No in-process variant ships. The model is heavy
(~570 MB weights + the `gliner` library + torch) and the production
deployment shape we want — slim guardrail + a sharable inference
service on a GPU node — doesn't benefit from an in-process option.
Setting `gliner_pii` in `DETECTOR_MODE` without `GLINER_PII_URL`
crashes loud at boot.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GLINER_PII_URL` | *(empty)* | HTTP base URL of a [gliner-pii-service](../../services/gliner_pii/README.md) container. Required when `gliner_pii` is in `DETECTOR_MODE`. |
| `GLINER_PII_TIMEOUT_S` | `30` | Per-call timeout (seconds) on the gliner-pii HTTP requests. |
| `GLINER_PII_FAIL_CLOSED` | `true` | Block requests when the gliner-pii detector errors. Independent flag — operators can fail closed on one detector and open on another. |
| `GLINER_PII_LABELS` | *(empty → server default)* | Comma-separated default zero-shot labels sent with every detection request (e.g. `"person,email,ssn,credit_card"`). Empty = use the gliner-pii-service's `DEFAULT_LABELS`. |
| `GLINER_PII_THRESHOLD` | *(empty → server default)* | Confidence cutoff (0..1) sent with every request. Empty = use the gliner-pii-service's `DEFAULT_THRESHOLD`. |
| `GLINER_PII_MAX_CONCURRENCY` | `10` | Semaphore on in-flight gliner-pii calls. Independent of `LLM_MAX_CONCURRENCY` and `PRIVACY_FILTER_MAX_CONCURRENCY`. Surfaced as `gliner_pii_in_flight`/`gliner_pii_max_concurrency` on `/health`. |

## Per-request overrides

Two gliner-specific keys can be passed in
`additional_provider_specific_params` (see
[per-request overrides](../per-request-overrides.md) for the general
shape):

| Override key | Type | Effect |
|---|---|---|
| `gliner_labels` | `string` (comma-separated) or `string[]` | Override `GLINER_PII_LABELS` for this call. Empty / missing → fall back to `GLINER_PII_LABELS`, then to the service's `DEFAULT_LABELS`. Capped at 50 entries (anything larger logs a warning and falls back). |
| `gliner_threshold` | `number` in `[0, 1]` | Override `GLINER_PII_THRESHOLD` for this call. Out-of-range / non-number values warn and fall back. JSON booleans are explicitly rejected (Python's `bool` would otherwise slip through as `0`/`1`). |

Why per-request rather than a deployment-wide setting: the
differentiator of GLiNER over a fixed token-classification model is
*zero-shot labels* — the entity-type vocabulary is an input to the
model. Per-request overrides let one deployment serve multiple routes
with different vocabularies (one route asks for medical labels,
another for finance labels) without redeploying or running multiple
gliner-pii-service containers.

```python
# Ask the gliner detector to look for HIPAA-flavoured PII on this one call:
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[...],
    guardrails=[
        {"anonymizer": {"extra_body": {
            "gliner_labels": ["person", "date_of_birth", "medical_record_number", "address"],
            "gliner_threshold": 0.6,
        }}}
    ],
)
```

## Picking labels and threshold

The label list is the model's only steering knob. A few patterns:

- **Broad PII coverage** — start with a comprehensive set
  (`person,email,phone,address,ssn,credit_card,iban,date_of_birth,medical_record_number`)
  and trim based on false-positive review.
- **Compliance-driven** — pick the exact labels a regulation cares
  about (HIPAA → medical record numbers, dates of birth, names; PCI →
  card numbers; GDPR → broad PII).
- **Domain-specific** — for legal, finance, healthcare etc., add
  domain labels alongside the standard ones.

Threshold trades recall for precision. The default 0.5 is a reasonable
middle ground; lower for a long-tail of edge cases, higher when noise
is more costly than misses.

## Selecting the backend

Same launcher pattern as the other remote detectors:

| Value      | What happens                                                   |
|------------|----------------------------------------------------------------|
| `service`  | Auto-start a `gliner-pii-service` container on the shared network and point the guardrail at it. Builds a separate `gliner-hf-cache` volume so its model download is independent from the privacy-filter cache. |
| `external` | Use the URL given by `--gliner-pii-url` / `GLINER_PII_URL`. Nothing is auto-started. |

There is no in-process backend for this detector — `--gliner-pii-backend`
must be set when `gliner_pii` is in `DETECTOR_MODE`.

## Failure handling

`GLINER_PII_FAIL_CLOSED` (default `true`) is independent from the
`LLM_FAIL_CLOSED` / `PRIVACY_FILTER_FAIL_CLOSED` flags — operators
can fail closed on one detector and open on another. When the
detector raises `GlinerPIIUnavailableError` (service unreachable,
timeout, non-200, or any unexpected exception under fail-closed), the
guardrail returns `BLOCKED`. With fail-open, the error is logged and
the request proceeds with coverage from the remaining detectors.

## License note

The model is released under the
[NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/),
which permits commercial use but adds clauses (responsible-AI use,
attribution, distribution rules) that the openai/privacy-filter
Apache-2.0 license doesn't. Verify it matches your deployment's
license posture before relying on this service in production.

## See also

- [`services/gliner_pii/README.md`](../../services/gliner_pii/README.md) —
  service-level docs: API contract, build commands, runtime config,
  CUDA variants.
