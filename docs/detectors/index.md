# Detectors

Five detection layers, all optional, controlled by `DETECTOR_MODE` —
a comma-separated list of detector names. Available names: `regex`,
`denylist`, `privacy_filter`, `gliner_pii`, `llm`.

Order in `DETECTOR_MODE` determines type-resolution priority: when the
same text is detected by multiple detectors, the type from the one
listed first wins. Example:

```
DETECTOR_MODE=denylist,regex,privacy_filter,llm
```

When multiple detectors are configured, they run in **parallel** and
the matches are merged and deduped.

## Comparison

| Detector | Kind | What it catches | External dep | Latency | When to pick it |
|---|---|---|---|---|---|
| [`regex`](regex.md) | Stateless pattern match | Things with recognizable shapes: IPs, CIDRs, emails, hashes, JWTs, AWS keys, GitHub tokens, OpenAI keys, internal hostnames | None | Microseconds | Always — high-precision baseline. Adds the pentest set for security engagements. |
| [`denylist`](denylist.md) | Literal-string match | Org-specific terms regex can't shape-match and the LLM may miss: employee names, project codenames, customer IDs, internal product names | None (operator YAML) | Microseconds | When you have a known list of sensitive terms. Deterministic, no false positives. |
| [`privacy_filter`](privacy-filter.md) | NER (`openai/privacy-filter`), in-process **or** remote | 8 PII categories: people, emails, phones, URLs, addresses, dates, account numbers, secrets | `transformers` + ~6 GB model in-process (`pf` / `pf-baked` images) **or** standalone HTTP service container | ~ms (CPU) / sub-ms (GPU) | When you want NER coverage without an LLM round-trip. Strict subset of LLM coverage — complement, not replacement. Pick the remote backend when you want the slim guardrail (no torch in the API container) or want to share one inference service across replicas / put it on a GPU node. |
| [`gliner_pii`](gliner-pii.md) | Remote zero-shot NER (`nvidia/gliner-pii`) | Caller-supplied label set on each request (e.g. `["ssn", "iban", "medical_record_number"]`) | Standalone HTTP service only — no in-process variant | ~ms (CPU) / sub-ms (GPU) | When you need flexible PII categories per request without retraining. **Experimental.** |
| [`llm`](llm.md) | OpenAI-compatible Chat Completions in JSON mode | Contextual entities regex can't catch: org names, personal names, internal product/project codenames embedded in prose | An OpenAI-compatible LLM endpoint (LiteLLM, Ollama, etc.) | LLM round-trip | When precision on contextual entities matters more than latency. Fail-closed by default. |

## How to combine

The detectors are designed to layer:

- **`regex`** is the always-on baseline — cheap, deterministic, no
  false positives on shape-matched entities.
- **`denylist`** adds your org's known-sensitive list. Goes early in
  `DETECTOR_MODE` so its types win conflicts (an employee name
  registered as `PERSON` beats a generic LLM `PERSON` classification).
- **`privacy_filter`** vs **`gliner_pii`** vs **`llm`** are not
  mutually exclusive but overlap substantially. Pick based on
  topology and cost: privacy_filter for fixed PII coverage, gliner_pii
  for caller-controlled labels, llm for contextual entities.

A typical production setup:

```
DETECTOR_MODE=regex,denylist,privacy_filter,llm
```

A pentest / debugging setup (deterministic — no LLM):

```
DETECTOR_MODE=regex,denylist
```

A "just see what NER catches" setup:

```
DETECTOR_MODE=regex,privacy_filter
```

## Common conventions

- Each detector emits `Match(text, entity_type)` records; the
  pipeline merges + dedupes by matched text (first detector to claim a
  substring keeps its type).
- Each remote detector (LLM, remote privacy_filter, gliner_pii) has
  its own typed unavailable error and an independent
  `*_FAIL_CLOSED` env var. Failure of one detector doesn't force
  failure on the others — see each detector's *Failure handling*
  section.
- Each detector with a finite-capacity backend has its own
  semaphore via `*_MAX_CONCURRENCY`. See
  [configuration → Capping detector concurrency](../configuration.md#capping-detector-concurrency).
- Each detector that loads a config file (regex patterns, denylist,
  LLM prompt) supports per-request *named* alternatives — operators
  pre-declare allowed names; callers reference them by name. See
  [per-request overrides → Named alternatives](../per-request-overrides.md#named-alternatives).
