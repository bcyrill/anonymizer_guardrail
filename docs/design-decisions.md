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
documented design decision below — see "Bidirectional
pipeline-level result cache (with source-tracked prewarm)" — so
the design decision here stays focused on whether to *redact
model output*, which the synthetic-match approach does not
address.

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

## Bidirectional pipeline-level result cache (with source-tracked prewarm)

**Implemented behind the `PIPELINE_CACHE_BACKEND` env var
(default `none`, off).** When enabled, the pipeline cache stores
deduped match lists keyed by `(text, detector_mode,
frozen_per_call_kwargs)`. Two write paths populate it:

- **Detection-side write**: after `_run_detection` runs the active
  detectors, the deduped result lands in the pipeline cache. The
  next request for the same text + same kwargs hits in one shot —
  no detector dispatch.
- **Deanonymize-side prewarm**: `pipeline.deanonymize` synthesises
  matches from the typed `VaultEntry` and writes them into BOTH the
  pipeline cache (deduped synth list keyed by full request shape)
  AND each per-detector cache (filtered by `source_detectors` per
  surrogate, keyed with that detector's own kwargs).

The implementation bundles Option 1 (bidirectional pipeline cache)
and Option 3 (per-detector source tracking) — both pieces sharing
the same vault schema migration so the migration cost is paid once.

### Vault schema

The vault stores a structured `VaultEntry` per call_id (not a flat
surrogate→original dict):

```python
@dataclass(frozen=True)
class VaultEntry:
    surrogates: dict[str, VaultSurrogate]    # surrogate → typed entry
    detector_mode: tuple[str, ...]           # active detectors at anonymize time
    kwargs: FrozenCallKwargs                 # frozen per-detector cache-affecting kwargs

@dataclass(frozen=True)
class VaultSurrogate:
    original: str
    entity_type: str                         # canonical type from the producing Match
    source_detectors: tuple[str, ...]        # detectors that independently found this entity
```

All three additions are load-bearing:

- `entity_type` per surrogate — synthesised matches need a typed
  Match to land in the right surrogate-cache slot. Faker output
  (`Robert Jones`, `192.0.2.5`) carries no type signal, so we can't
  recover it from the surrogate prefix.
- `source_detectors` per surrogate — captured BEFORE `_dedup`
  collapses per-detector lists. Lets the deanonymize hook filter
  the synthesised match list per detector so per-detector cache
  slots only get *that* detector's matches (not the deduped union).
  Per-detector hit/miss stats stay meaningful.
- Call-level `detector_mode` + `kwargs` — required to reconstruct
  the pipeline-cache key AND each per-detector cache key at
  deanonymize time. Without these, prewarm would have to fall back
  to default-overrides slots, which silently re-introduces
  override-staleness.

### Cache architecture

Two cache layers participate when both are configured live:

1. **Pipeline cache** (this layer, opt-in via `PIPELINE_CACHE_BACKEND`):
   one lookup, returns the deduped match list directly. Fast path
   when no overrides changed since the last call for the same text.
2. **Per-detector caches** (existing per-detector `<DETECTOR>_CACHE_*`
   knobs): structural backup tier. The pipeline cache key is
   union-sensitive (any kwarg change anywhere → miss); the
   per-detector key only fragments by *that* detector's kwargs.
   When the pipeline cache misses on partial-override-mismatch,
   detectors whose kwargs are unchanged still hit their own slots.

The deanonymize-side prewarm writes to BOTH layers in one pass.
Per-detector writes are filtered by `source_detectors` so
detectors that didn't independently find an entity don't get it
written to their slot — preserves honest per-detector stats and
keeps the per-detector tier from carrying the deduped-union lie.

### Why prewarm matters under multi-turn workloads

The cache without prewarm hits on every replayed prior user
message but always misses on the most recent assistant reply
(which was never put through detection — only deanonymize).
Prewarm closes that half:

- Steady-state cost without prewarm: 2 detection passes per
  round-trip (the new user message + the new assistant reply that
  echoes back at turn N+1 without ever being seen by detection).
- With prewarm: 1 detection pass per round-trip (the new user
  message). The assistant reply was prewarmed into both caches at
  the previous deanonymize, so turn N+1's anonymize hits.

### Faker support

Compatible with `USE_FAKER=true`. The structured vault entry
carries `entity_type` per surrogate, so prewarm doesn't need to
recover types from surrogate prefixes — that surrogate-prefix
extractor (the previous Option-2 implementation's
load-bearing helper) is no longer needed.

### What enabling it looks like

```bash
# Memory backend (single-replica or restart-tolerant):
export PIPELINE_CACHE_BACKEND=memory
export PIPELINE_CACHE_MAX_SIZE=2000

# OR redis backend (multi-replica + restart-persistent):
export PIPELINE_CACHE_BACKEND=redis
export PIPELINE_CACHE_TTL_S=600
export CACHE_REDIS_URL=redis://localhost:6379/2
export CACHE_SALT=<deployment-secret>   # shared with per-detector cache

# Per-detector caches stay opt-in via the existing
# <DETECTOR>_CACHE_MAX_SIZE / <DETECTOR>_CACHE_BACKEND knobs —
# enabling them gives the structural backup tier on override
# fragmentation. Disable them and only the pipeline cache is live.
```

Wire-up summary:

- `Pipeline.__init__` → constructs `self._pipeline_cache` via
  `build_pipeline_cache(backend=...)`. Backend `none` returns a
  no-op sentinel so the wrap-around-detect path is branch-free.
- `Pipeline._detect_one` → wraps the per-text fan-out in
  `self._pipeline_cache.get_or_compute(key, _compute)`. Cache
  key = `(text, detector_mode, frozen_call_kwargs)`. Compute
  callable runs the detectors, dedupes via `_dedup_with_sources`,
  returns `list[(Match, source_detectors)]`.
- `Pipeline.anonymize` → after detection, builds the structured
  `VaultEntry` (typed surrogates + source attribution + frozen
  kwargs) and `vault.put`s it.
- `Pipeline.deanonymize` → reads the entry, substring-replaces,
  calls `_prewarm_caches(restored, entry)` — which in parallel
  writes to the pipeline cache (per restored text) AND each active
  cache-using detector's per-detector cache (filtered by
  `source_detectors`, keyed with the detector's kwargs from the
  vault entry).

### Trade-offs accepted

- **No backwards compatibility on the vault wire format.** This
  was a clean break from the prior `dict[str, str]` shape. In-flight
  vault entries from a prior version are decoded as malformed and
  treated as missing — the deanonymize path returns the response
  with surrogates still in it (logged ERROR, same severity as a
  TTL miss). Operators on Redis backends should drain in-flight
  entries before deploying, or accept the one-window grace miss.
- **Pipeline cache key is union-sensitive.** Any change to any
  detector's kwargs invalidates the slot for all of them. The
  per-detector tier is the structural backup; both layers
  together give graceful degradation.
- **`source_detectors` adds vault size**, ~3-10x bytes per
  surrogate on a typical 5-detector deployment. Acceptable: the
  vault entries are short-lived (`VAULT_TTL_S` seconds, default
  600) and the size is dominated by the per-text mapping anyway.
