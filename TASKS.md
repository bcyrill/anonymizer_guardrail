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

## Per-detector contribution metrics

**Priority:** low.

**What:** track and surface on `/health` per-detector "pull its
weight" stats — how many matches each active detector contributed
to a request, and (more importantly) how many of those were
*unique* finds vs ones already caught by an earlier-priority
detector. Today the per-detector cache hit/miss counters tell
operators whether each detector's *cache* is firing; they don't
tell them whether each detector is *finding things the others
miss*.

**Why:** detectors are layered on the assumption that each adds
something the others don't. Regex catches shape-anchored types,
LLM catches semantic / contextual entities, gliner catches
zero-shot NER. In practice the layers overlap heavily — an
EMAIL_ADDRESS could come from regex, LLM, gliner, or PF. Without
per-detector contribution stats, an operator can't easily answer
"is gliner actually catching things regex doesn't already, or am
I just paying for redundant detection?". The dedup pass collapses
the per-detector lists into one before the pipeline returns —
the contribution info is computed implicitly but never surfaced.

Source-tracked prewarm is already shipped as part of the
bidirectional pipeline cache (see
[design-decisions → Bidirectional pipeline-level result cache](docs/design-decisions.md#bidirectional-pipeline-level-result-cache-with-source-tracked-prewarm));
this task gives operators the *aggregate* per-process question
("is gliner pulling its weight across all calls?") that the
per-call vault `source_detectors` field doesn't surface.

**Why deferred (low priority):** today's deployments are mostly
"trust the canonical priority order, run the full stack". The
contribution question becomes load-bearing when:

  * An operator wants to drop a detector from the mode for cost
    reasons (LLM detection is the expensive one) and needs data
    to decide if the loss is acceptable.
  * Per-detector capacity tuning matters (e.g. "should I bump
    LLM_MAX_CONCURRENCY?") and the operator wants to know what
    fraction of LLM's load was actually contributing unique
    entities.

**Concrete trigger:** any of the above shows up.

**Sketch:**

1. **Pipeline-local counters.** Three new counter dicts on
   `Pipeline.__init__`, all keyed by detector name:
     * `_matches_total: dict[str, int]` — every match each
       detector returned, counted before dedup.
     * `_matches_unique: dict[str, int]` — matches this detector
       found that no earlier-priority detector also found.
     * `_matches_redundant: dict[str, int]` — matches this
       detector found that an earlier detector already had.
   Initialised to 0 for every name in `REGISTERED_SPECS`. Same
   shape and lifecycle as the existing `_inflight_counters`.

2. **Instrumentation point.** In `pipeline._run_detection` (or
   wherever the per-detector match lists are collected before
   `_dedup` runs), walk each detector's match list:
     * For each match, increment `_matches_total[detector_name]`.
     * If the match's `(text, entity_type)` pair is already
       claimed by an earlier-priority detector in the same
       request, increment `_matches_redundant[detector_name]`.
     * Otherwise increment `_matches_unique[detector_name]` and
       record the pair as claimed.
   Track claims in a per-request set so the unique/redundant
   distinction is request-local — the pipeline-level counters
   accumulate across requests.

3. **/health stats.** Extend `Pipeline.stats()`:
     * `<prefix>_matches_total: int`
     * `<prefix>_matches_unique: int`
     * `<prefix>_matches_redundant: int`
   Same key-prefix convention as the existing per-detector
   stats. Operators can compute `unique / total` to get the
   "pull its weight" ratio per detector.

4. **Tests.**
   * Layered detection where two detectors find the same entity:
     the higher-priority detector's `_matches_unique` increments;
     the lower-priority's `_matches_redundant` does. Pin the
     priority-order semantics — first-listed in `DETECTOR_MODE`
     wins the unique credit.
   * Single-detector mode: every match is unique by definition.
   * Counters survive across multiple requests in one process.

**Non-goals:**

- **Don't store per-match source info anywhere persistent.** The
  contribution metrics live in process-local counters; nothing
  new goes to the vault. The vault's existing `source_detectors`
  field already answers the per-call question ("which detectors
  found this specific entity"); contribution metrics answer the
  process-lifetime aggregate ("did each detector pull its weight
  across all calls"). Two separate questions, two separate stores.
- **Don't break the dedup pass.** The instrumentation walks the
  per-detector lists in the same order `_dedup` does and uses
  the same `(text, entity_type)` claim semantics. A future
  change to dedup priority should keep the contribution
  counters consistent — the test should pin that.

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
