# Design decisions

This file captures choices we considered, evaluated, and **deliberately
declined to make**. Different from `TASKS.md`:

| File | Purpose |
|---|---|
| [`TASKS.md`](../TASKS.md) | Things we intend to do later. Each entry has a "concrete trigger" — something that, when it happens, flips the entry from deferred to active. |
| `design-decisions.md` (this file) | Things we evaluated and chose **not** to do. Each entry exists so a future contributor (or future-us) doesn't re-litigate the same trade-off cold, and so the decision can be re-evaluated when the load-bearing assumptions change. |

If a decision in here genuinely changes (constraints shift, the
tradeoff inverts), update the entry in place — don't add a "we
reversed this" footnote. The entry should reflect the *current* answer
with the *current* reasoning; git history covers the rest.

---

## Detector-specific override fields stay in `api.Overrides`

**Considered:** moving each detector's override fields out of the flat
`api.Overrides` model and into the detector module that consumes them.
Each `DetectorSpec` would declare an `override_fields` mapping;
`api.Overrides` would be assembled dynamically at import time via
`pydantic.create_model` aggregating across `REGISTERED_SPECS`.
Cross-cutting fields (`use_faker`, `faker_locale`, `detector_mode`)
would stay in api.py.

**The pull:**

- Locality. `gliner_labels` cap (`_MAX_GLINER_LABELS`) and the
  `_strict_threshold` validator currently live in `api.py` despite
  belonging conceptually to `detector/remote_gliner_pii.py`.
- `_VALID_OVERLAP_STRATEGIES` is duplicated between `api.py` and
  `detector/regex.py` — moving the validator would collapse that
  duplication.
- Symmetric with the existing registry-driven design:
  `DetectorSpec.prepare_call_kwargs` already lives per-module, and
  per-detector override declarations would round out the pattern.
- Adding a new detector would become fully self-contained — no
  api.py edit.

**Why we declined:**

- **Static analyzability lost.** With `pydantic.create_model(...)`
  building `Overrides` dynamically, IDE autocomplete on
  `overrides.llm_model` and mypy/pyright field-existence checks both
  stop working. The codebase relies on this attribute access in
  `pipeline.py` and the per-detector `prepare_call_kwargs` functions.
  That's a real ergonomic regression on every read of those fields.
- **Cognitive overhead.** "What fields does `Overrides` have?" stops
  being "look at the class" and becomes "look at the registry plus
  every imported detector module."
- **Marginal locality win.** Adding a detector already requires editing
  `pipeline.py`, `detector/__init__.py`, and a launcher spec. The
  api.py edit is ~5 lines (add 2-3 fields, maybe a validator) — not
  a meaningful additional barrier in a change that already touches
  several files.
- **Detectors aren't a plugin surface for external contributors.** All
  detectors live in this repo. The "self-contained module" property
  would only matter if third parties added detectors, which isn't
  the deployment shape.

**When to re-evaluate:**

- If detectors become an external plugin surface (third-party
  packages contributing `DetectorSpec`s). Then the api.py edit
  *would* be a barrier and the dynamic-model cost would be paid
  once for a proper extension point.
- If the field count grows enough that a flat `Overrides` becomes
  unreadable (current: 10 fields; rough threshold: 25+).
- If a detector's overrides become structurally complex enough
  (nested config, conditional fields) that they can't be expressed
  as flat fields on a single model.

**Cheaper middle ground (also evaluated, also not done):** move only
the validators and constants (`_VALID_OVERLAP_STRATEGIES`,
`_MAX_GLINER_LABELS`, `_strict_threshold`) to their detector modules
and import them into api.py. Keeps the field declarations in
`Overrides` (static, IDE-friendly) but puts the validation logic next
to the detector. We declined this too — the duplication is small and
the indirection (validator imports criss-crossing the detector/api
boundary) wasn't a clear win over the status quo. Worth reconsidering
if the validator set grows substantially.

---

## Surrogate generator dict is rebuilt per-match under `use_faker` overrides

**Considered:** hoisting the `_build_generators(use_faker, salt)` call
in `surrogate.SurrogateGenerator.for_match` out of the per-match path
when a `use_faker` override is active. Today, every call to
`for_match` with a `use_faker` argument invokes
`_build_generators(...)`, which iterates `_GENERATOR_SPEC`
(~25 entries) and constructs a fresh dict — sometimes including a
fresh `_opaque(...)` closure per entry. The default-no-override path
already reuses `self._generators`, so this only affects the override
path.

The natural optimisation: at the start of `Pipeline.anonymize`,
materialise the override-shape generator dict once and pass it down,
or memoise it on `SurrogateGenerator` keyed by `(use_faker, locale)`.
Either avoids the per-match rebuild.

**Why we declined:**

- **Wins are tiny.** Closure construction in CPython is on the
  order of a few hundred nanoseconds; the dict comprehension over
  25 entries is similar. A request producing 100 matches with a
  `use_faker` override pays well under 1 ms total — invisible
  next to the LLM detection round-trip (tens to hundreds of ms).
- **Per-match call frequency is bounded.** The pipeline already
  de-duplicates per-request (`if m.text not in
  original_to_surrogate` in `Pipeline.anonymize`), so distinct
  inputs determine the call count, not the number of detected
  spans. Pathological inputs that reach hundreds of unique spans
  *and* set `use_faker` per request *and* don't fit the
  same-pattern-many-times shape are rare.
- **The optimisation adds a layer.** Either a kwarg threading
  the prebuilt dict down through `for_match`, or a memoisation
  cache keyed by `(use_faker, locale)` that needs invalidation
  when `salt` rotates. Both add surface area; the maintenance
  cost outweighs the runtime cost at our scale.

**When to re-evaluate:**

- Profiling on a real workload shows surrogate generation as a
  measurable fraction of request time (>5% of p50 or p95).
- A regex-only deployment (no LLM detection latency to amortize
  the work against) starts hitting hundreds of distinct spans per
  request with `use_faker` overrides — the per-match cost is then
  no longer in the noise.
- A future detector expands `_GENERATOR_SPEC` substantially — the
  per-match rebuild scales linearly with that table. At ~100
  entries the calculus shifts.

---

## Response side runs deanonymize only — no detection on model output

**Considered:** running detector layers on the response side
(`input_type="response"`) in addition to the existing vault
lookup, to anonymize *new* PII the model emits in its reply.
Today's response path is a pure substring-replace from the vault
keyed by `call_id`; if the model invents an email address or
person name that wasn't in the request, that goes back to the
user untouched.

The natural shape: in `main.py`, `input_type="response"` would
call `pipeline.anonymize` *before* (or instead of) `deanonymize`,
treating the assistant message like any other text and emitting
modified output if new PII is detected.

**Why we declined:**

- **Response-path latency.** Today the response side is a vault
  lookup — microseconds. Adding detection makes every response,
  including short conversational replies ("ok!" "thanks"), pay
  detector latency (hundreds of ms for LLM/PF/gliner). That cost
  scales with conversation count, not request count, so chatty
  workloads pay it on every turn.
- **Failure mode worsens.** Under fail-closed (the default for
  LLM/PF/gliner), a detector error on the response side would
  block the *response*, meaning the user gets a guardrail error
  instead of the model's reply *on a request that already
  succeeded*. That's a strictly worse UX than failing on the
  request, where the user can retry without losing context.
- **The product question isn't decided.** Whether to redact PII
  the model generates is a real product call (does it block the
  response, modify it, only log it? what counts as "the model's
  PII" vs. legitimate output?). Wiring detection in
  speculatively would commit to one answer implicitly. Keep this
  as a deliberate design concern with its own decision when a
  use case actually needs it.

**Consequence (current behaviour, document explicitly):**

- Detection runs on `input_type="request"` only.
- The response side performs `pipeline.deanonymize` (vault lookup
  + substring-replace) and nothing else.
- New PII generated by the model in its response is **not**
  redacted. Operators relying on the guardrail to redact model
  output (rather than just model input) need to know this.

**Note on cache pre-warming:** an earlier draft of this entry
also covered using response-side detection to pre-warm the
detector cache so turn N's assistant message wouldn't miss on
turn N+1. That's a separate path — it can be done *without*
running detection by deriving synthetic matches from the vault
during the existing deanonymize step. Implemented as a
documented design decision below — see "Detector cache pre-warm
from deanonymize-derived matches" — so the design decision
here stays focused on whether to *redact model output*, which
the synthetic-match approach does not address.

**When to re-evaluate:**

- A use case shows up where the model is expected to emit PII
  the guardrail must redact (e.g. agentic flows where the model
  retrieves and surfaces customer records). At that point the
  response-side anonymization question becomes load-bearing in
  its own right.
- LiteLLM's guardrail contract changes such that response-side
  hooks become async / non-blocking from the caller's view (the
  latency objection above weakens substantially in that world).

---

## Oversized per-text input under fail_closed blocks the whole batch

**Considered:** when a single text in `req.texts` exceeds
`LLM_MAX_CHARS` (and `LLM_FAIL_CLOSED=true`, the default),
`LLMDetector._do_detect` raises `LLMUnavailableError`, the
TaskGroup propagates it, and `Pipeline.anonymize` aborts the
*entire request* — including the other texts that fit fine and
might have been anonymized successfully. Could we drop the
oversized text and proceed with the rest, since "all the others
are anonymized" sounds like more coverage, not less?

**Why we declined (the silent-bypass argument):**

- **The full `req.texts` array is forwarded to the upstream LLM.**
  LiteLLM's guardrail contract is "guardrail returns modified
  texts; LiteLLM substitutes them and forwards the request." If
  the guardrail drops one entry from anonymization, the
  *original* (un-anonymized) text still reaches the upstream
  model — there's no "skip this text" option in the request
  forwarded.
- **Dropping under `fail_closed=true` would be the silent bypass
  fail-closed exists to prevent.** Operators who set fail-closed
  did so because they want the guardrail to *refuse* uncertain
  situations, not loosen them. "We couldn't scan one text, but
  we anonymized the others, so call it good" is exactly the
  posture fail-closed rejects.
- **There's no safe middle ground.** Truncation was already
  rejected for the per-text `LLM_MAX_CHARS` check (silently
  scanning 90% and shipping the last 10% unredacted is the worst
  possible outcome). A separate "oversize policy" knob would
  just rename the same trade-off without resolving it.

**Consequence (current behaviour, document explicitly):**

- Under `LLM_FAIL_CLOSED=true`, a single oversized text in
  `req.texts` blocks the entire request. Operators see a
  `BLOCKED` action with the LLM's blocked_reason; their client
  retries with smaller payloads or a higher cap.
- Under `LLM_FAIL_CLOSED=false`, the LLM detector swallows the
  `LLMUnavailableError` and contributes no matches for the
  oversized text. The other detectors (regex, denylist, PF,
  gliner) still run on it, providing whatever coverage they
  can. The upstream LLM receives the request — possibly with
  un-anonymized PII the LLM detector would have caught. This is
  the trade-off `fail_open` operators have explicitly chosen.
- This is not asymmetric with merged-mode size fallback (which
  *does* per-detector demote on oversized blob). There, no
  detector has yet committed to "I can scan this text"; demoting
  to per_text means we still scan every text, just one detector
  at a time. Here, the per-text dispatch has already
  committed — exceeding the cap on one text means *that text*
  cannot be scanned, period.

**When to re-evaluate:**

- LiteLLM's guardrail contract grows a "skip this text" or
  "block-by-index" mechanism that lets the guardrail surface
  partial unscanability without reaching the upstream model.
- A use case shows up where heterogeneous traffic (one large
  document + several short messages) is genuinely common AND
  raising `LLM_MAX_CHARS` is not a viable mitigation. The
  remediation today is "use a larger detection model and bump
  the cap"; a real production case where that's insufficient
  would be load-bearing evidence.

---

## Detector cache pre-warm from deanonymize-derived matches

**Implemented behind the `DETECTOR_CACHE_PREWARM` env var
(default off).** When enabled, `pipeline.deanonymize` synthesises
`Match` objects from the vault entries that contributed substring
replacements and seeds each active cache-using detector
(`SPECS_WITH_CACHE`) with the deduped match list keyed at the
detector's default-overrides cache slot. The next request that
contains the same restored text — typical for multi-turn
conversations where turn N's assistant reply lands inside turn
N+1's prompt — gets a per-detector cache hit instead of running
detection again.

The implementation matches "Option 2" of the three architectural
shapes considered (pre-warm each per-detector cache with the
deduped match set). Variations 1 (new bidirectional pipeline
cache) and 3 (per-detector pre-warm with detector-source
tracking) were considered and dismissed for now — see below.

### Why this is gated behind a flag

`DETECTOR_CACHE_PREWARM=false` (the default) keeps today's
deanonymize behaviour exactly as before — pure substring-replace,
no cache writes, no surprise cross-call interactions. Operators
opting in are accepting two documented caveats:

1. **Per-detector hit/miss stats become misleading.** The same
   deduped match list lands in every active cache-using
   detector's slot. When LLM/PF/gliner are all active, every
   detector's cache reports "found" entities the others actually
   found.
2. **Override-staleness for non-default per-call overrides.**
   The pre-warm writes to each detector's *default-overrides*
   cache slot. Non-default-overrides requests land in different
   slots and run detection normally; a textually-identical
   default-overrides request that follows a non-default-overrides
   anonymize call gets the non-default-derived matches in its
   default slot. Stable-overrides workloads aren't affected.

### Why USE_FAKER must be false when pre-warm is on

The boot validator
(`Config._detector_cache_prewarm_requires_opaque_tokens`) rejects
`DETECTOR_CACHE_PREWARM=true` paired with `USE_FAKER=true`.

The reason: pre-warm needs to know each entity's `entity_type`
when synthesising `Match` objects. Without storing the type on
the vault (a deliberate non-goal — see "Variations considered"),
we recover it at deanonymize time by parsing the surrogate's
prefix:

```
[<TYPE>_<8 uppercase hex>]   →   TYPE
```

Faker output (`Robert Jones`, `192.0.2.5`, `bob@corp.example`)
carries no such signal. Allowing both knobs simultaneously would
either silently fall back to `OTHER` for every Faker entity —
breaking the surrogate generator's `(text, entity_type)` cache
key invariant and producing different surrogates on cache hits
than on cold detection — or require the vault schema change we
explicitly opted out of.

The same constraint extends to per-call `use_faker=true`
overrides: `pipeline.anonymize` rejects them with a WARNING
when `DETECTOR_CACHE_PREWARM=true`, falling back to
`config.use_faker=false` for that request. Without this, an
operator with prewarm enabled who lets a single per-call Faker
override slip through would punch a hole in the
opaque-tokens-only invariant for that call_id, and the matching
deanonymize-side prewarm would either silently drop those
entities (treating them as malformed) or — worse, with a future
permissive parser — attribute them to `OTHER` and break
surrogate consistency on the next turn.

### Variations considered

**1. New bidirectional pipeline-level cache.** A separate cache
keyed `(text, detector_mode, frozen_per_call_kwargs) → tuple[Match, ...]`
that holds the deduped match set, populated by both real
detection runs and the deanonymize hook. `_detect_one` consults
it first; on hit, skips ALL detectors. Theoretically the
cleanest abstraction (symmetric coverage of user-typed text
*and* deanonymize-derived text). Dismissed for now because:

- Adds a new cache type with its own backend / failure-mode
  surface (memory + redis), doubling the cache complexity for
  a feature whose primary win (catching the assistant-message
  miss) the chosen Option 2 already delivers.
- The pipeline cache key is union-sensitive: any change to any
  detector's per-call kwargs invalidates the slot for all of
  them. Operators with override-fragmented workloads (multi-tenant
  prompt rotation, per-call gliner labels) get fewer hits than
  they would on per-detector caches alone. The combined Option-1-
  plus-per-detector design that addresses this is more code than
  Option 2 by a margin that wasn't justified by the available
  trigger.

**2. (the chosen option — implemented above)**.

**3. Per-detector pre-warm with detector-source tracking.** Like
Option 2, but each vault entry stores `source_detectors` (which
detectors found each entity) plus the resolved per-call overrides
each active detector used at anonymize time. On deanonymize,
rebuild *each detector's* match list filtered by source and
keyed by that detector's actual overrides — per-detector cache
hit/miss stats stay honest, and pre-warmed entries land in the
exact slot future identical-overrides requests would query.

Dismissed because:

- The vault schema grows substantially (3-10× bytes per entry on
  a typical 5-detector deployment with 5-20 entities) and
  requires JSON-wire-format changes on both `MemoryVault` and
  `RedisVault`.
- Per-detector hit/miss-fidelity is a real operational property
  but isn't load-bearing for any current deployment shape.
  Operators reading per-detector cache stats today aren't doing
  capacity planning that pivots on the source attribution.

If a future operator's workload makes the per-detector stats
honesty load-bearing (e.g. detector-mix optimisation based on
per-detector hit rates), Option 3 is the upgrade path. The
vault entity-type addition that's a non-goal today becomes
load-bearing then.

### What enabling it actually does

```bash
export DETECTOR_CACHE_PREWARM=true
export USE_FAKER=false   # required by the boot validator

# Detector caches must be enabled for prewarm to populate them.
# Memory backend (default for single-replica) or redis backend
# (for cross-replica share):
export LLM_CACHE_MAX_SIZE=200
# OR: LLM_CACHE_BACKEND=redis  + CACHE_REDIS_URL=...
```

Wire-up summary:

- `Pipeline.deanonymize` → after substring replacement →
  `_prewarm_detector_caches(restored, mapping)`.
- `_prewarm_detector_caches` walks `mapping` (surrogate →
  original), recovers `entity_type` via
  `surrogate.opaque_token_entity_type`, builds one
  `tuple[Match, ...]` and writes it into each active
  cache-using detector's `_cache` via `det.warm_cache(text, matches)`.
- `BaseRemoteDetector.warm_cache` calls
  `self._cache.get_or_compute(self._default_cache_key(text), …)` —
  which writes on miss but preserves any pre-existing entry
  (so real-detection results aren't overwritten by synthesised
  ones if both paths fire on the same text).

### When to re-evaluate

- A workload's `<prefix>_cache_misses` counter on `/health`
  shows the assistant-message miss is a measurable share of
  detection cost AND the override-staleness caveat is biting
  (heavy override fragmentation in mixed traffic). Time to
  consider Option 3.
- A deployment wants a single cache layer for both user and
  response text (text-level repeats independent of provenance).
  Time to consider Option 1's bidirectional pipeline cache,
  with the pipeline cache as primary tier and per-detector
  caches as the override-fragmentation backup tier. The task
  expansion is tracked in
  [TASKS.md → Bidirectional pipeline-level result cache](../TASKS.md#bidirectional-pipeline-level-result-cache).
