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
| [`privacy_filter`](privacy-filter.md) | Remote NER (`openai/privacy-filter`) | 8 PII categories: people, emails, phones, URLs, addresses, dates, account numbers, secrets | Standalone HTTP service only — no in-process variant | ~ms (CPU) / sub-ms (GPU) | When you want NER coverage without an LLM round-trip. Strict subset of LLM coverage — complement, not replacement. |
| [`gliner_pii`](gliner-pii.md) | Remote zero-shot NER (`nvidia/gliner-pii`) | Caller-supplied label set on each request (e.g. `["ssn", "iban", "medical_record_number"]`) | Standalone HTTP service only — no in-process variant | ~ms (CPU) / sub-ms (GPU) | When you need flexible PII categories per request without retraining. |
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

## When detectors disagree — worked example

`DETECTOR_MODE` order resolves type conflicts when two detectors
flag the same span. Concrete example: input is
`"contact alice@acme.com"`, both `regex` and `llm` are active.

| Detector | What it returns |
|---|---|
| `regex` | `Match(text="alice@acme.com", entity_type="EMAIL_ADDRESS")` |
| `llm`   | `Match(text="alice@acme.com", entity_type="PERSON")` (the model interprets the email as a personal handle) |

Dedup is keyed on the matched **text**, not on `(text, type)`. Whichever
detector appears first in `DETECTOR_MODE` keeps its type:

- `DETECTOR_MODE=regex,llm` → `EMAIL_ADDRESS` wins, the LLM's PERSON
  classification is dropped, and the surrogate is an email-shaped
  string (`fake.email()`).
- `DETECTOR_MODE=llm,regex` → `PERSON` wins, surrogate is a personal
  name (`fake.name()`) — semantically wrong for an email and almost
  certainly not what you want.

**Practical consequence:** put the deterministic detectors first
(`regex,denylist,…`) so shape-based classifications win conflicts
against the more interpretive LLM/NER layers. This is the rationale
behind the recommended ordering above.

Partial overlaps (regex catches `alice@acme.com`, LLM catches just
`alice`) survive dedup as two distinct entries — `_dedup` keys on
the matched text, so the differing strings don't collide. Both get
surrogates generated and both land in the vault.

At **replacement** time, the substitution regex sorts keys
longest-first and joins them as an alternation
(`alice@acme\.com|alice`). Python's `re` engine commits to the first
matching alternative at each position, so the email gets replaced
whole and the inner `alice` substring is never matched at that
position. The `alice` surrogate is therefore dead weight when the
inner text only ever appears *inside* the longer span — generated,
stored in the vault, but never visible in the anonymised output.

When `alice` appears elsewhere on its own (e.g. `"contact alice@acme.com
or alice directly"`), both replacements fire — at different positions —
and you'd see something like `"contact fake_email_1 or fake_name_1
directly"`. The longest-first sort is what keeps the email-only case
correct: without it, replacing `alice` first would produce
`fake_name_1@acme.com`, which then no longer matches the email
pattern.

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
