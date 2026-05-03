# Follow-up tasks

Backlog of work that's been considered and deliberately deferred. Each entry
captures what to build, why it matters, and the rough shape of the work so a
future contributor (or future-you) can pick it up cold.

---

## Prometheus-style `/metrics` endpoint

**What:** a new HTTP endpoint that returns OpenMetrics text for scraping by
Prometheus, Grafana Agent, Datadog Agent, etc. Counters + histograms — not
just point-in-time gauges.

**Why:** today's `/health` snapshot answers *"what is the state right now?"*
(vault size, cache size, in-flight LLM calls). It does **not** answer the
operational questions that show up first in production:

- *How many PERSON entities have we anonymized over the last hour?* (counter)
- *What's the p95 LLM-detection latency over the last 24h?* (histogram)
- *What's the regex-vs-LLM detection-rate split, broken down by entity type?*
- *Are we approaching the LLM concurrency cap?* (rate of saturation events)

These are time-series questions; gauges in `/health` can't answer them.
The standard answer in any modern infra is a `/metrics` endpoint.

**Why deferred:** no metrics scraper deployed yet. Premature without
Prometheus/Grafana/Datadog already in place — would just be unused
instrumentation.

**Sketch:**

1. Add `prometheus-client` (or `opentelemetry-sdk`) to `pyproject.toml`.
2. Define metrics:
   - `anonymizer_entities_total{type, detector}` — counter, incremented per
     `Match` produced.
   - `anonymizer_detector_seconds{detector}` — histogram, observed around
     each `det.detect()` call inside `Pipeline._detect_one`.
   - `anonymizer_request_seconds{op}` — histogram for `op="anonymize"` and
     `op="deanonymize"`, observed in `Pipeline`.
   - `anonymizer_llm_unavailable_total{reason}` — counter, incremented when
     `LLMUnavailableError` is raised, with `reason` derived from the error
     class (timeout / connect / non-200 / oversize / unexpected).
   - `anonymizer_vault_size` / `anonymizer_surrogate_cache_size` — gauges
     mirroring what `/health` already exposes.
3. Wire instrumentation into the existing call sites — most of the data is
   already flowing through the pipeline; this is mostly hooks, not logic.
4. Expose `GET /metrics` returning `prometheus_client.generate_latest()`.
5. Decide on auth (probably none — Prometheus targets are typically inside
   the cluster — but document it as a deployment concern).
6. Document `METRICS_ENABLED` and the endpoint URL in the README.

**Non-goals:**

- No `/audit` HTML dashboard. The DontFeedTheAI dashboard logs
  `original → surrogate` mappings in plaintext, which is fine for a
  single-tenant local tool but a cross-tenant data-leak risk for an infra
  service. Counters and histograms hold no PII; transformation logs do.
- No engagement-tab UI. We're a LiteLLM guardrail plugin, not a standalone
  proxy with a notion of pentest engagements.

---

## Streaming response support

**What:** make `deanonymize` correct for token-by-token streaming responses
from the upstream model, where surrogates may straddle chunk boundaries.

**Why:** LiteLLM's pre/post-call hook contract today gives us the full
response body, and our regex-based substitution assumes the surrogate
appears whole somewhere in the text. With streaming (`stream: true` on
chat completions), the upstream model emits one or many tokens per chunk;
a surrogate like `Robert Jones` might arrive as `"Robert"` in chunk N and
`" Jones"` in chunk N+1. Our current code, applied chunk-by-chunk, would
miss the deanonymization (no whole match in either chunk); applied to the
assembled response it works but defeats the streaming use case (client
sees surrogate text until the stream ends and post-call fires).

README calls this out as a limitation: "LiteLLM's guardrail calls are
pre/post; streaming responses are deanonymized after assembly."

**Why deferred:** non-streaming completions are still the default for many
clients; the people who care about streaming are a known subset. The fix
is non-trivial (state across chunks, edge cases with partial matches), and
LiteLLM's guardrail-API contract for streaming may evolve.

**Sketch:**

1. Confirm what LiteLLM actually sends for streaming responses through the
   guardrail hook — is each chunk a separate POST, or does LiteLLM
   re-assemble? The fix shape depends entirely on this.
2. If per-chunk: maintain a per-call_id streaming-state buffer. For each
   chunk:
   - Emit everything UP TO the last `len(longest_surrogate)` chars
     immediately (those can't possibly be a partial-match prefix of any
     surrogate longer than that).
   - Hold the trailing tail and prepend it to the next chunk before
     scanning. On stream-end, flush the tail.
   - Apply the existing alternation regex to the buffered region.
3. If LiteLLM re-assembles before calling us: nothing to do — the README
   limitation is wrong, and we should fix the docs instead.
4. Tests with simulated chunk boundaries falling INSIDE a surrogate, AT a
   surrogate boundary, and across multiple surrogates.

**Concrete trigger:** a streaming use case shows up.

**Non-goals:**

- Don't try to anonymize partial chunks on the request side — the
  request body is delivered whole to the pre-call hook.

---

## Optional structured (JSON) logging

**What:** add a `LOG_FORMAT=text|json` env var (default `text`). When
`json`, install a `python-json-logger` `JsonFormatter` on the root
logger so every log line is one JSON object — `level`, `name`,
`message`, `timestamp`, plus structured `extra={…}` fields like
`call_id`, `entity_count`, `detector`.

**Why:** today the `logging.basicConfig` call in `main.py` uses a
human-readable format string
(`%(asctime)s %(levelname)s %(name)s — %(message)s`). Fine for `docker
logs` and local dev, but in any production-grade log pipeline (ELK,
Splunk, Datadog, Loki + Grafana) JSON lines are dramatically easier
to query — *"show me all `anonymize` calls in the last hour where
`entity_count > 50`"* is one filter expression on JSON, vs. a regex
on text. Pairs well with the planned Prometheus `/metrics` endpoint:
metrics for trends, structured logs for per-call drill-down.

**Why deferred:** no production deployment yet that's plumbed into a
log aggregator. Premature otherwise.

**Sketch:**

1. Add `python-json-logger` to `pyproject.toml`.
2. Extend `Config` with `log_format: str = "text"`.
3. In `main.py`, branch on `config.log_format`: keep the existing
   `basicConfig` for `text`; for `json`, replace the root handler's
   formatter with `pythonjsonlogger.jsonlogger.JsonFormatter`.
4. Replace existing `log.info("anonymize call_id=%s entities=%d
   texts=%d", …)` calls with `log.info("anonymize", extra={"call_id":
   …, "entities": …, "texts": …})` so the structured fields land as
   top-level JSON keys. Keep the message unchanged for text mode
   (the JSON formatter respects `extra`; the text formatter ignores
   it).
5. Document `LOG_FORMAT` in the README env table next to `LOG_LEVEL`.

**Non-goals:**

- Don't add OpenTelemetry tracing here. Logging and tracing are
  different shapes (point events vs. spans), and OTel pulls in a
  much bigger dependency surface — separate task if/when needed.
- Don't try to redact PII from logs structurally. The pipeline
  already gates body-content logs behind DEBUG in `main.py`'s
  `guardrail` handler; the structured fields we'd log (`call_id`,
  counts) are PII-free.

---

## Prefix-based cache for merge mode

**What:** a sibling cache type, `PrefixDetectionCache` (or similar),
indexed by message-boundary prefixes of the merged blob rather than
exact text. On a request, find the longest cached prefix of the
current blob, reuse those matches, run detection only on the
remaining suffix (with a leading-context window for context-aware
detectors), and merge results.

**Why:** under merge mode (now wired for LLM, privacy_filter, and
gliner_pii via `<DETECTOR>_INPUT_MODE=merged`; see
`pipeline.py:_partition_by_input_mode` and
`tests/test_pipeline_merge_mode.py`), each turn's merged blob is
unique by construction — so the existing per-text
`InMemoryDetectionCache` 100% misses. But turn N+1's blob is
byte-identical to turn N's blob up to the message boundary, plus
new content appended:

```
turn N:   [sys] SEP [u1] SEP [a1] ... SEP [u_N]
turn N+1: [sys] SEP [u1] SEP [a1] ... SEP [u_N] SEP [a_N] SEP [u_{N+1}]
```

A prefix-keyed cache lets turn N+1 reuse turn N's detection work
on the shared prefix and run the LLM/gliner/PF call only on the
new suffix (plus a small leading-context window so the
context-aware detector still sees what the new content is
*about*).

**Why deferred:** complementary to merge mode but substantially
more complex than either the existing per-text cache or vanilla
merge mode. Worth building only if (a) merge mode ships and (b)
operators report that the per-turn fan-out cost in long
conversations is still the bottleneck. The simpler "merge mode
without cache" path may already be fast enough — measure first.

**The seam doesn't fit the existing Protocol.** `DetectorResultCache`
today is `key → matches`. Prefix caching is
`find_longest_prefix(blob) → (matched_prefix, matches)` — a
different operation backed by a different storage shape (trie or
boundary-checkpoint list). Adding it as a third backend behind the
same Protocol would distort the Protocol; better to introduce it
as a sibling cache type that detectors hold *instead of* the
per-text cache when `input_mode="merged"`.

**Concrete trigger:** any of:

- Merge mode is shipping in production and operators report
  per-turn LLM/gliner/PF latency that scales linearly with
  conversation length.
- Token-cost monitoring shows the merged-mode amortization win
  is partially eaten by repeated detection over the same prior
  turns.
- A long-conversation use case (>20 turns) becomes a primary
  workload.

**Sketch:**

1. Define a sibling Protocol or extended interface alongside
   `DetectorResultCache`. Most natural shape:
   `lookup_prefix(blob, boundaries) -> (matched_prefix_len,
   matches)` and `store(prefix, matches)`. The `boundaries`
   argument is the list of message-boundary character offsets so
   the cache only checkpoints at semantically meaningful cuts —
   never mid-message.
2. Storage: an LRU-bounded list of `(prefix_hash, prefix_length,
   matches)` keyed by hash of the prefix string. Lookup walks
   candidate prefixes from longest to shortest, hashing each, and
   short-circuits on the first hit. For typical conversations
   (<50 turns) the list is small enough that linear-scan is fine.
   A trie is overkill at this scale.
3. Detector integration: when `input_mode="merged"` AND a prefix
   cache is configured, the detector consults the prefix cache
   before its merged-blob call. On hit, the detect call covers
   only `blob[prefix_len - K : ]` where K is a leading-context
   window (configurable, ~1-2 messages worth). On miss, detect
   the full blob.
4. Result merge: the cache returns matches from the prefix scan
   (matches A), the live detect call returns matches from the
   suffix scan (matches B). Pipeline's existing dedup-by-value
   handles the union; entities found in both regions collapse
   naturally. Drop matches whose text appears *only* inside the
   leading-context portion of B — they'd double-count against A.
5. Eviction: per-conversation hint not available (the guardrail
   is stateless across calls beyond `call_id`, which is per-call
   not per-conversation). Default to LRU on prefix-hash with a
   cap; document the limitation.
6. Sentinel safety: the message-boundary token used for merging
   is the cut point. Detectors must NOT classify the sentinel
   itself as anything; pin it as a reserved string in the
   detection prompt and add a regex post-filter that drops any
   match overlapping the sentinel literal.
7. Stats: `<prefix>_prefix_cache_size`, `<prefix>_prefix_cache_hits`,
   `<prefix>_prefix_cache_misses`, plus a histogram-shaped
   counter for "fraction of blob covered by cache hit on a hit"
   so operators can see whether the cache is meaningfully
   shrinking detection work or just providing nominal hits.
8. Tests: prefix hit / no hit, prefix hit with leading-context
   window, boundary-spanning entity not double-counted, sentinel
   not classified.

**Non-goals:**

- **Don't generalise to arbitrary substring caching.** Only
  *prefixes* exploit the multi-turn append-only invariant. Caching
  arbitrary substrings would chase diminishing returns with
  exploding storage and lookup cost.
- **Don't track per-conversation identity.** The guardrail
  doesn't know which `call_id`s belong to the same conversation
  (LiteLLM doesn't pass that through). Prefix matching is
  global-keyed-by-content; that's enough.
- **Don't share the storage with `InMemoryDetectionCache`.** The
  two have different access patterns (point lookup vs. longest-
  prefix walk), different eviction semantics, and different
  staleness trade-offs. One detector picks one cache type per
  `input_mode`, not both.
- **Don't reimplement merge mode itself.** This optimization sits
  on top of the existing merge dispatch (already wired in
  `pipeline.py`); it doesn't replace or duplicate that mechanism.

---

## Bidirectional pipeline-level result cache

**Related to:** the implemented design-decision
[Detector cache pre-warm from deanonymize-derived matches](docs/design-decisions.md#detector-cache-pre-warm-from-deanonymize-derived-matches),
which lists this work as the "Option 1" variation that was
considered and dismissed. This entry is the formal task expansion
of that variation for if/when the trigger fires.

**Priority:** low.

**What:** add a new `PipelineResultCache` layer that sits ABOVE the
per-detector caches and caches the *deduped* match set keyed by
`(text, detector_mode, frozen_per_call_kwargs)`. Two write paths
populate it, both writing to the same key/value shape:

  * **Detection-side write**: after `_detect_one(text)` runs the
    active detectors and dedups, the result lands in the pipeline
    cache so the next request for the same text hits in one shot.
  * **Deanonymize-side pre-warm**: `pipeline.deanonymize` synthesises
    matches from vault entries (same path as today's
    `_prewarm_detector_caches`) and writes them into the pipeline
    cache for the deanonymized output text.

`_detect_one(text)` checks the pipeline cache first; on hit, skips
ALL detectors and returns the cached set directly. The "input vs
output" asymmetry the existing per-detector pre-warm has — fresh
user text populates per-detector caches while deanonymize-derived
text populates per-detector caches via a separate hook, both
keyed at the default-overrides slot — collapses into a single
unified caching layer that covers text-level repeats regardless
of provenance.

**Why:** completes the deanonymize-prewarm story by making
pre-warm a special case of a general "we've seen this text
before" cache. Today's Option 2 implementation pre-warms
per-detector caches with deduped matches keyed at default
overrides; that's correct for stable-overrides workloads but
mis-attributes hit/miss across detectors. The pipeline cache
attributes hits to "the deduped result for this exact request
shape" — semantically clean and correct.

**Why deferred (low priority):** Option 2 is already shipped and
covers the common case (multi-turn conversations where turn N's
assistant message lands in turn N+1's prompt). The pipeline cache
is a layered upgrade: bigger surface area (new cache Protocol,
factory, two backends, /health stats, merged-mode warning
update), with the primary win the same one Option 2 already
delivers. Premature until override fragmentation OR per-detector
stats fidelity become load-bearing for an actual deployment.

**Concrete trigger:** any of:

- An operator's per-detector hit/miss stats become misleading
  enough that capacity planning is hard. The pipeline cache's
  per-shape stats (`pipeline_cache_hits/misses`) replace the
  multi-detector confusion with one number.
- A workload with stable per-call overrides where Option 2's
  default-slot pre-warm misses the textually-identical-but-
  override-different requests. The pipeline cache's
  `(text, detector_mode, kwargs)` key handles this naturally.
- Operators measuring text-level repeat rate find that a
  unified cache layer would absorb traffic that today
  fragments across N per-detector slots.

**Architectural shape — single bidirectional cache, mirrors the
existing detector cache structure:**

The new module layout matches `detector/cache.py` / `cache_memory.py`
/ `cache_redis.py`, just at the pipeline scope:

```
src/anonymizer_guardrail/
  pipeline_cache.py          # PipelineResultCache Protocol + factory
  pipeline_cache_memory.py   # InMemoryPipelineCache (default, zero deps)
  pipeline_cache_redis.py    # RedisPipelineCache (opt-in via cache-redis)
```

`PipelineResultCache` Protocol mirrors `DetectorResultCache`:
`enabled`, `get_or_compute(key, compute) → list[Match]`, `stats() →
dict[str, int]`, `aclose()`. Same shape detectors already code
against, just at a different layer.

**Cache-key shape** (the load-bearing detail):

```python
PipelineCacheKey = tuple[str, tuple[str, ...], tuple[tuple[str, tuple], ...]]
#                   text   detector_mode    frozen_per_call_kwargs
```

Where `frozen_per_call_kwargs` is a hashable tuple-of-tuples
version of the per-detector kwargs each active detector's
`prepare_call_kwargs` produced for the request — LLM's `(model,
prompt_name)`, gliner's `(labels_tuple, threshold)`, regex's
`(overlap_strategy, patterns_name)`, etc. Verbose, but correct:
any change to any detector's overrides invalidates the slot
(documented union sensitivity — see design-decisions entry).

**Backends — same factory pattern as detector caches:**

  * `InMemoryPipelineCache`: process-local LRU keyed by the
    PipelineCacheKey tuple (Python's `dict` handles the tuple
    natively). Sized via `PIPELINE_CACHE_MAX_SIZE` (memory backend
    only). Same eviction + thread-safety pattern as
    `InMemoryDetectionCache`.
  * `RedisPipelineCache`: shared store. Key:
    `pipeline:<blake2b-keyed-digest of repr(key))>` — same
    salt + digest scheme `RedisDetectionCache` uses today,
    reusing `CACHE_SALT` for the digest key. Value: JSON
    `[{"text": str, "type": str}]`. TTL via `EXPIRE`
    (`PIPELINE_CACHE_TTL_S`). Reuses the existing
    `CACHE_REDIS_URL` from central config.

**Sketch:**

1. **Schema + Protocol.** New `pipeline_cache.py` defines
   `PipelineResultCache` Protocol + `_resolve_cache_salt`
   (importable from `detector/cache.py` to share the resolution
   logic) + `build_pipeline_cache(backend, max_size, ttl_s)`
   factory. Two backend modules. Same shape as detector cache.

2. **Pipeline integration.**
   * `Pipeline.__init__` constructs the pipeline cache via
     factory, stores on `self._pipeline_cache`.
   * `_detect_one(text, ...)` (the per-text dispatch) checks
     `self._pipeline_cache.get_or_compute(key, compute)` first
     where `compute` is the existing per-detector dispatch chain.
     On hit, the pipeline cache absorbs it; on miss, detectors
     run as today and the result lands in the pipeline cache.
   * `_prewarm_detector_caches` (the existing deanonymize hook)
     gets a sibling `_prewarm_pipeline_cache` that writes to the
     pipeline cache instead of (or alongside, depending on flags)
     the per-detector ones.

3. **Config knobs (central Config):**
   * `PIPELINE_CACHE_BACKEND: Literal["memory", "redis", "none"] = "none"`
     — three-way: memory, redis, or off entirely. Off is the
     default to keep behaviour unchanged for existing deployments.
   * `PIPELINE_CACHE_MAX_SIZE: int = 0` — LRU cap, memory backend
     only.
   * `PIPELINE_CACHE_TTL_S: int = 600` — TTL, redis backend only.
   * Reuse `CACHE_REDIS_URL` + `CACHE_SALT` (no new cross-cutting
     vars).
   * Cross-field validator: `PIPELINE_CACHE_BACKEND=redis`
     requires `CACHE_REDIS_URL`. Same shape as the existing
     `_cache_redis_url_required_when_any_detector_picks_redis`
     validator — extend it or add a sibling.

4. **/health stats.** Add `pipeline_cache_size`,
   `pipeline_cache_max`, `pipeline_cache_hits`,
   `pipeline_cache_misses`, `pipeline_cache_backend` to
   `Pipeline.stats()`. Same pattern the per-detector caches
   already use.

5. **Merged-mode warning extension.** The existing pipeline-init
   warning (`pipeline.py:_detector_cache_prewarm_requires_opaque_tokens`-
   adjacent block) flags `input_mode=merged` + per-detector
   cache-on as wasted work. With a pipeline cache, merged mode
   ALSO produces unique-by-construction blobs → the pipeline
   cache writes one-shot entries. Warn on
   `input_mode=merged` + `PIPELINE_CACHE_BACKEND != "none"`,
   same wording shape.

6. **Interaction with existing `DETECTOR_CACHE_PREWARM`.**
   When BOTH the new pipeline cache and the existing prewarm
   are active, decide policy:
     * Option A: prewarm writes to pipeline cache only when
       enabled; per-detector prewarm becomes the fallback.
     * Option B: prewarm writes to both (defensive — costs
       extra writes but covers operators who toggle one off).
     * Option C: deprecate the per-detector prewarm path when
       the pipeline cache is on, with a one-release migration
       window (warning at boot when both are set).
   Pick at implementation time. Option B is safest; Option C
   is cleanest long-term.

7. **Tests:**
   * Round-trip: detection populates pipeline cache; second
     identical request hits the pipeline cache (no detector
     dispatch, observable via mocked HTTP-call counters).
   * Override sensitivity: change an LLM prompt override →
     pipeline cache misses; identical request afterwards hits
     the new slot.
   * Backend parity: redis backend round-trips through fakeredis
     with the same `(text, detector_mode, kwargs)` key shape.
   * Stats: hits/misses/size/max/backend on `/health` for both
     backends, including the redis `size=0, max=0` placeholder
     pattern.
   * Pre-warm interaction: the deanonymize hook populates the
     pipeline cache with the synthesised match list under the
     correct key (default detector_mode + default kwargs).

**Non-goals:**

- **Don't auto-disable per-detector caches** when the pipeline
  cache is on. Operators with override-fragmented workloads
  benefit from the per-detector caches absorbing the unchanged-
  detector hits the pipeline cache structurally can't reach
  (its key is union-sensitive — see design-decisions discussion).
  Per-detector caches remain operator-controlled; the pipeline
  cache is a separate opt-in layer.
- **Don't conflate this with the per-detector cache redis backend.**
  They're two independent caches that happen to share `CACHE_REDIS_URL`
  + `CACHE_SALT`. Redis namespaces (`cache:<detector>:<digest>`
  vs `pipeline:<digest>`) keep them separate so a `FLUSHDB` on
  one doesn't wipe the other.
- **Don't ship a "pipeline-only mode"** that disables per-detector
  caches automatically when the pipeline cache is on. The
  trade-off (override fragmentation) is workload-dependent;
  operators pick.

---

## Pin Viterbi calibration JSON for privacy-filter

**What:** ship a default `services/privacy_filter/calibration.json`
(loaded automatically when present at `/app/calibration.json` in
the container, or via `PRIVACY_FILTER_CALIBRATION` env), with the
six transition-bias values tuned for whatever corpus is driving
the precision/recall gap.

**Why deferred:** opf's stock decoder already produces clean spans
on the two fixtures the spike measured — see
`services/privacy_filter/scripts/PROBE.md` → "After the opf
migration", and the `default` profile column in
`services/privacy_filter/scripts/spike_opf.py`. The
`anti-merge` profile we built differed from `default` on exactly
one span (the NTDS-hash boundary on `engagement_notes.txt`), which
isn't enough to justify pinning corpus-specific values.
Calibration is ammunition for when a real deployment surfaces a
recall/precision gap default doesn't close.

**The gate is signal, not effort.** Implementation is small (see
estimate below); what's missing is a fixture that justifies
tuning. Pinning biases against the single NTDS-hash delta would be
cargo-culting, not tuning. Stay parked until one of the triggers
below fires.

**Trigger:** any of these is sufficient evidence to start tuning.

  * A production corpus shows recall regressions vs the bundled
    fixtures (e.g. opf misses entities the regex detector catches
    despite labelling concepts in its trained vocabulary).
  * A new fixture in `services/privacy_filter/scripts/` exposes
    spans the merge or split passes have to repair frequently —
    the post-processing layer firing routinely is a sign the
    decoder isn't terminating where it should.
  * An opf upstream update changes default decoder semantics in
    a way that drifts our pinned post-migration tables (re-run
    `spike_opf.py` after every `OPF_GIT_REF` bump in
    `services/privacy_filter/Containerfile`).

**Sketch + effort** (when triggered, ~1 person-day total):

| Phase | Effort | What |
|---|---|---|
| 1. Reproduce the gap | ~1 hr | Run `python services/privacy_filter/scripts/spike_opf.py` against the motivating fixture. Confirm `default` underperforms and identify which biases need to move. The 3 baseline profiles (`default` / `anti-merge` / `privacy-parser`) bracket most of the practical tuning space. |
| 2. Tune | ~2-4 hrs | Iterate on the 6 transition biases. Edit `_PROFILES` in spike_opf.py to inject a fourth candidate; each spike run is ~1 min on CPU so the loop is fast. Start from `privacy-parser` if the gap is over-fragmentation, `anti-merge` if it's over-merging. |
| 3. Plumb | ~1 hr | Pin chosen biases at `services/privacy_filter/calibration.json`. In `services/privacy_filter/Containerfile`: `COPY calibration.json /app/calibration.json` and add `PRIVACY_FILTER_CALIBRATION=/app/calibration.json` to the `ENV` block. Operators override with a bind-mounted JSON at run time. |
| 4. Lock the regression test | ~30 min | Add the new fixture to `_FIXTURES` in spike_opf.py so future spike runs catch drift on it automatically. |
| 5. Document | ~30 min | Add a row to PROBE.md *After the opf migration* showing default-vs-pinned spans on the new fixture. |
| 6. Rebuild + verify | ~30 min | Rebuild the image, run probe against the new fixture, confirm the calibration is loaded (spike_opf.py output now matches the pinned profile). |

**Non-goals:**

- Don't pin calibration values speculatively. The biases are
  corpus-dependent — values that improve recall on one shape of
  text often hurt precision on another. Default decoder is the
  honest baseline until a corpus says otherwise.
- Don't expose individual biases as runtime env vars. Operators
  who need different tuning ship a different JSON; the env var
  selects the file, not the values.

**Companion task — retire the merge / split / gap-cap layer if
production traffic confirms it never fires:**
The guardrail-side `_to_matches` runs paragraph-break split,
per-label gap caps, and same-type merge as defense in depth. On
the bundled fixtures under opf's stock decoder these passes don't
fire (the constrained Viterbi already produces correct boundaries).
If production telemetry confirms zero firings across N requests
(say, 10k+ across diverse shapes), the layer can be retired —
saves a small per-request regex pass and removes one source of
"why did the span change?" debugging questions. Until that signal
exists, keep the layer (cost is microseconds).

---

## Promote `privacy-filter-hf-service` to default

**What:** the experimental hf+opf-decoder variant
([`services/privacy_filter_hf/`](services/privacy_filter_hf/)) is
~7x faster than the opf-only service on CPU at full quality parity
on the bundled fixtures (see
`services/privacy_filter_hf/COMPARE.md`). Once it's been validated
on more diverse corpora, replace the opf-only service as the
production default — keep both packages published while the
transition lands, then deprecate the opf-only path.

**Status:** infrastructure for the promotion is in place; the
actual default-switch and deprecation are gated on Phase 2
(wider-corpus validation). Both variants are published, buildable,
and operator-selectable; the only thing standing between
"experimental" and "default" is signal.

| Step | Status | Where |
|---|---|---|
| CPU image flavour | ✅ done | `pf-hf-service` |
| CUDA image flavour | ✅ done | `pf-hf-service-cu130` |
| CI workflow | ✅ done | `.github/workflows/publish-pf-hf-service-image.yml` (CPU + CUDA matrix) |
| release.sh axis | ✅ done | `+pf-hf-service` tag prompt |
| Launcher CLI flag | ✅ done | `--privacy-filter-variant opf\|hf` |
| Launcher menu prompt | ✅ done | Variant row under Privacy-filter section |
| LauncherSpec wiring | ✅ done | `LauncherSpec.service_variants["hf"]` resolves to the hf ServiceSpec |
| Documentation (variants section) | ✅ done | `docs/detectors/privacy-filter.md` "Variants" + `docs/deployment.md` HF service section |
| **Wider corpus validation (Phase 2)** | ⏳ blocking | not started |
| Default-switch + migration note | ⏳ pending Phase 2 | will switch CLI default from `opf` to `hf` and add migration paragraph |
| opf-only deprecation timer | ⏳ pending Phase 2 | CHANGELOG entry committing to N-release deprecation window |

**Why deferred:** the speed/quality numbers come from two bundled
fixtures plus a one-line probe. Production corpora will surface
edge cases (long inputs, rare unicode, code blocks, tokeniser
quirks) where the two variants might diverge. Promoting to default
without that signal is premature.

**Trigger:** any of these is sufficient to flip the default and
start the deprecation timer.

  * A real corpus shows the hf+opf variant matches the opf-only
    variant span-for-span on >99% of inputs.
  * Operators report the opf-only variant's CPU latency as a
    real production blocker.

**Remaining phases** (now ~1 person-day total since the
infrastructure landed):

| Phase | Effort | What |
|---|---|---|
| 2. Run a wider corpus comparison | ~2-3 hrs | Probe both variants on a real customer-data corpus (or the `engagement_notes.txt` fixture extended). Diff span-for-span. Fix any divergence at the BIOES→span layer in `services/privacy_filter_hf/main.py`. |
| 5. Switch the default + add migration note | ~1 hr | Flip `--privacy-filter-variant`'s default from `opf` to `hf`. Add a one-paragraph migration guide at the top of `docs/detectors/privacy-filter.md` so existing `PRIVACY_FILTER_URL` consumers know the wire format hasn't changed and the swap is just an image-tag edit. Update README.md / examples.md to recommend the HF variant. |
| 6. Deprecate opf-only on a timer | ~30 min | Add a CHANGELOG entry committing to keep the opf-only image published for N releases (3?) before retiring it. Don't actually delete the workflow until that period elapses. |

**Non-goals:**

- Don't add a baked variant of `pf-hf-service`. The build hits
  disk-space pressure during the layer commit (transformers + opf
  + ~3 GB of weights, with overlayfs duplicating cached files via
  snapshot symlinks). If a corpus needs air-gapped HF deployment,
  ship a sidecar that pre-fetches into a bind-mounted cache rather
  than baking the model into the image.
- Don't merge the two services into one image with a switch. The
  whole point of having both is to keep them comparable and roll
  back cleanly if hf+opf turns out to misbehave on production
  inputs. Two packages, two workflows, one wire format.
- Don't change the wire format during promotion. The guardrail-side
  detector is shape-stable across both variants today; promoting
  without preserving that guarantee defeats the rollback story.
