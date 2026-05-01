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
