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

## Multi-replica support (Redis-backed Vault)

**What:** make the call-id → mapping store pluggable, and add a Redis-backed
implementation alongside the existing in-memory `Vault`. Selected via
env var (e.g. `VAULT_BACKEND=memory|redis`).

**Why:** today the vault is a process-local dict. The pre-call (anonymize)
and post-call (deanonymize) hooks must land on the same replica or the
mapping is lost — the response gets shipped back to the user with surrogates
still in it. This works for a single-replica deployment; it breaks the
moment anyone runs >1 replica behind a load balancer (the typical HA
posture). README already calls this out as a known limitation.

**Why deferred:** single-replica is fine for many deployments. Adding Redis
pulls in operational complexity (deploy + monitor + secure another piece of
infra) that's unwarranted until horizontal scaling is actually needed.

**Sketch:**

1. Extract a `VaultBackend` Protocol from the current `Vault` class — two
   methods, `put(call_id, mapping)` and `pop(call_id) -> dict`. Move the
   in-memory implementation to `MemoryVault` (or keep `Vault` as the
   in-memory default).
2. Add `RedisVault` using `redis-py` with `asyncio` support:
   - Key: `f"vault:{call_id}"`, value: msgpack/JSON-encoded mapping.
   - TTL via Redis `EXPIRE` (replaces the in-memory `_evict_expired` loop).
   - `pop` = `GETDEL` (atomic).
3. Add `VAULT_BACKEND` and `REDIS_URL` env vars; wire selection in
   `Pipeline.__init__`.
4. Tests via `fakeredis` so CI doesn't need a real Redis.
5. Open question for the **surrogate cache**: stay per-replica (acceptable
   inconsistency — same entity on different replicas may get different
   surrogates) or also move to Redis (stronger consistency, extra round
   trip per surrogate)? For the typical pre/post-call pair landing on
   different replicas, only the *vault* needs to be shared — the
   surrogate is built into the request, not re-derived on the response
   side. So per-replica surrogate cache is fine; document it.
6. The **detector result cache** is a separable concern with its own
   triggers (multi-replica, restart persistence, cross-replica hit
   rate) — see *Redis-backed detector result cache* below.
7. Document the limitation that's now solved + the new env vars.

**Concrete trigger:** the day someone adds a second replica.

**Non-goals:**

- Don't generalize to "any KV store" — one production-quality backend
  (Redis) covers the use case. Cassandra/DynamoDB/etc. can be added
  per-need.

---

## Redis-backed detector result cache

**What:** add a `RedisDetectionCache` implementation alongside the
existing `InMemoryDetectionCache`, selectable per detector via
`<DETECTOR>_CACHE_BACKEND=memory|redis`. Backed by one shared Redis
instance with per-detector key namespaces.

**Why:** the in-memory cache (`detector/cache.py`) is process-local
and lost on restart. Three independent triggers want a shared /
durable cache:

- **Multi-replica.** A request routed to replica B can't reuse work
  done on replica A, so cache hit rate degrades inversely with the
  number of replicas. Distinct from the *Multi-replica support
  (Redis-backed Vault)* task above — that one is about correctness
  (lost mappings ⇒ unredacted responses), this one is about
  efficiency (cold caches per replica).
- **Restart persistence.** Multi-turn conversations live for
  minutes-to-hours; a deploy mid-conversation drops every cached
  detection result and the next replayed history pays full LLM cost
  again.
- **Memory pressure.** Operators caching aggressively (large
  conversation counts × long histories) can move the cache off the
  Python heap onto a separate process / host.

**Why deferred:** none of those triggers fires for a single-replica
deployment with caching disabled (the current default). Redis
introduces real complexity — a network dependency, serialization,
connection pooling, and a "Redis is down" failure mode that the
detector layer has to decide how to handle. Premature until the
cache is actually enabled in production *and* one of the triggers
above is real.

**The seam is already there.** `cache.py` defines a
`runtime_checkable` Protocol `DetectorResultCache` that
`InMemoryDetectionCache` satisfies; `LLMDetector._cache` is
annotated as the Protocol. Adding a new backend is a new file plus
a factory selector — no edits to detector code.

**Concrete trigger:** any of:

- Horizontal scaling (>1 replica).
- A deployment where conversations routinely outlive process
  restarts and the lost cache is operator pain.
- Measured memory pressure (cache size pinned to the cap with high
  hit rate, but operators can't grow the cap further on the host).

**Sketch:**

1. Add a cross-cutting `CACHE_REDIS_URL` env var on the central
   `Config`. *One* Redis URL — operators don't want N Redis
   instances to deploy and monitor just because we have N
   detectors.
2. Add `<DETECTOR>_CACHE_BACKEND=memory|redis` to each cache-using
   detector's `*Config`. Default `memory` — Redis is opt-in even
   when `CACHE_REDIS_URL` is set.
3. New file `detector/redis_cache.py` with `RedisDetectionCache`
   implementing `DetectorResultCache`. Use `redis-py`'s asyncio
   client (`redis.asyncio.Redis`) — the detector layer is already
   async.
4. Per-detector key namespace: `cache:<spec.name>:<blake2b(key)>`
   where `key` is the same hashable tuple the in-memory backend
   uses today. Hash the tuple before storing — Redis keys are
   strings, and `repr(tuple)` is fragile. Reuse `SURROGATE_SALT`
   or introduce a separate `CACHE_SALT` so cache keys don't leak
   originals to anyone with Redis read access.
5. Value serialization: msgpack or JSON over the `tuple[Match, ...]`.
   Match is a frozen dataclass with two strings — round-trip is
   trivial regardless of choice. JSON is debuggable from
   `redis-cli`; msgpack is smaller. Pick one; document.
6. Eviction: per-detector TTL via `<DETECTOR>_CACHE_TTL_S` (Redis
   `EXPIRE`). Redis doesn't do LRU per-namespace natively, so TTL
   is the per-detector knob; the `maxmemory` policy on the Redis
   instance itself bounds the global footprint. *No* `*_CACHE_MAX_SIZE`
   for the Redis backend — that's a memory-backend concept; document
   the asymmetry.
7. Failure handling: if Redis is unreachable, the cache becomes a
   passthrough (`get_or_compute` calls through to the detector,
   nothing is cached). Same shape as `InMemoryDetectionCache`'s
   disabled state. Log a warning at most every N seconds so a flap
   doesn't spam logs. Do *not* fail-closed — a degraded cache is
   not the same as a degraded detector.
8. Stats: `<prefix>_cache_size` reports `DBSIZE` filtered to the
   namespace (or `SCAN MATCH cache:llm:* COUNT N` periodically;
   exact size is OK to be approximate). Hit/miss counters stay
   process-local — they're per-instance traffic, not cluster-wide.
9. Tests via `fakeredis[asyncio]` so CI doesn't need a real Redis,
   plus a small integration test gated by an env var that runs
   against a real Redis when one's available.
10. Document the operator surface: `CACHE_REDIS_URL`, the per-detector
    `*_CACHE_BACKEND` and `*_CACHE_TTL_S`, the namespace scheme, and
    the "Redis down → passthrough" failure mode.

**Non-goals:**

- **Don't replace the in-memory backend.** Single-replica
  deployments shouldn't grow a Redis dependency for a feature that
  works without one. Both backends ship; the env var selects.
- **Don't share key construction with the in-memory backend.** The
  in-memory cache uses Python tuples directly as dict keys; the
  Redis backend hashes them to strings. The detectors hand a
  hashable to `get_or_compute` either way — the backend decides
  what to do with it.
- **Don't move the surrogate cache to Redis as part of this task.**
  That's covered in *Multi-replica support* with a different set of
  trade-offs (correctness vs. efficiency). Keep the scopes
  separate.
- **Don't add per-detector Redis URLs.** If operators want to
  shard, they can put a different DB number per detector
  (`redis://host/0` for LLM, `redis://host/1` for gliner) via
  Redis's existing logical-DB feature without us inventing config
  shape for it.

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

## Pre-warm detector cache from deanonymize-derived matches

**Priority:** low.

**What:** during the existing `pipeline.deanonymize` step, build
synthetic `Match` objects from the vault entries that contributed
substring replacements and use them to pre-warm the detector
cache, keyed by the *deanonymized* output text. Turn N's
assistant message — which today always misses cache when it
appears inside turn N+1's request — would be a hit instead.

**Why:** the detector cache today gives hits on every prior user
message and the system prompt, but the most recent assistant
message always misses on first sight (it was never put through
detection — only deanonymize). Steady-state cost is roughly two
detection calls per round-trip: one for the new user message,
one for the new assistant message. Pre-warming closes the
assistant half of that, dropping steady-state to one new call
per round-trip.

**Why deferred (low priority):** the assistant-side miss is
*one constant call per turn*, not a multiplier on conversation
length. The cache already wins on the dominant cost — replayed
history. Operators with a measurable hit-rate gap (visible via
`<prefix>_cache_misses` on `/health`) get a clear signal to
prioritise this; absent that signal, the win is small.

**The key insight that makes this cheap:** we don't need to run
detection on the response side. The vault already holds every
`(surrogate, original)` pair the request side detected. After
deanonymize substitutes them back into the response text, we
already know which substrings of that text were entities. That
substantially changes the cost / failure-mode picture relative
to "run detection on responses" (see
`docs/design-decisions.md` → *Response side runs deanonymize
only*).

**Vault change required.** Today's vault stores
`surrogate → original` (strings only). Pre-warming with
`tuple[Match, ...]` needs the entity type, which `Match`
carries. Extend the vault entry shape to
`surrogate → (original, entity_type)`; recover the type at
deanonymize time. Small change, isolated to `vault.py` and the
two pipeline call sites.

**Architectural shape — two alternatives, decide at
implementation time:**

1. **New pipeline-level cache.** A separate cache, distinct
   from the per-detector caches, keyed `text → tuple[Match, ...]`
   that holds the deduped match set. `_detect_one(text)` checks
   this cache first; on hit, skips ALL detectors and returns the
   cached set directly. The deanonymize step is a natural
   producer for this cache; per-detector caches stay focused on
   their own detector's output. **Cleanest, but adds a new
   cache type.**
2. **Pre-warm each per-detector cache** with the deduped match
   set. Doesn't add a cache type; reuses the existing
   `DetectorResultCache`. Downside: per-detector hit/miss stats
   become misleading (every detector "found" everything). The
   cached match set may also be missing entities the *real*
   detector would have found if run on the deanonymized text
   directly — the deduped set reflects what *all detectors
   combined* found on the original surrogate-anonymized text, not
   what each detector independently would find on the
   deanonymized form.

Option 1 is more honest about what the data actually is. Option
2 is smaller. Pick at implementation time.

**Concrete trigger:**

- A workload's `<prefix>_cache_misses` counter on `/health`
  shows the assistant-message miss is a measurable share of
  detection cost — typically chatty workloads with many turns
  per conversation.
- An operator instruments hit-rate over a representative window
  and measures that the constant-per-turn miss is meaningful
  next to their detection-call budget.

**Sketch:**

1. Extend `Vault` entry shape to store entity_type alongside
   each original. `pipeline.anonymize` already has both; pass
   them through.
2. Add a hook on `pipeline.deanonymize` that, after the
   substring-replace pass, builds `tuple[Match, ...]` from the
   vault entries and records `(deanonymized_text, matches)`
   pairs.
3. Implement Option 1 OR Option 2 above. For Option 1: define
   a new `PipelineResultCache` (Protocol mirroring
   `DetectorResultCache`), instantiate one on `Pipeline`,
   consult it at the top of `_detect_one`, store entries from
   the deanonymize hook. For Option 2: write into each active
   detector's `_cache` directly, building the per-detector key
   from the configured-default override values (since the
   response side doesn't carry per-call overrides).
4. Stats: surface hit/miss/size on `/health` under a new
   `pipeline_cache_*` prefix (Option 1) or fold into existing
   `<prefix>_cache_*` keys (Option 2).
5. Tests: turn-N response → turn-N+1 request hits cache;
   pre-warming respects per-call override boundaries (Option 2);
   evicted entries fall back to detection cleanly.

**Non-goals:**

- **Don't add response-side detection.** This entry is the
  cheap path that avoids it. Running detection on responses
  remains a separate, explicit design call (see design-decisions.md).
- **Don't try to re-derive entity types from surrogate
  structure.** Vault carries the type; the heuristic
  ("`[EMAIL_ADDRESS_…]` looks like an email" /
  "Faker output shaped like an IP") is fragile and unnecessary
  given the vault change is small.
- **Don't pre-warm with stale per-call overrides.** If turn N's
  request was made with `llm_prompt="strict"`, the cached
  matches are "what strict-prompt LLM + regex + others found."
  Turn N+1 might use the default prompt — pre-warming the
  default-prompt cache slot with strict-prompt-derived matches
  would be wrong. Either include the request-side overrides in
  the cache key (Option 1 makes this natural) or only pre-warm
  the slot matching turn N's overrides.

---

## BaseRemoteDetector consolidation

**What:** extract the shared shape across LLM, privacy_filter, and
gliner_pii detectors into a small base class. Each currently
duplicates ~80% of `__init__`, `cache_stats()`, `aclose()`, and the
cache-wrapper template inside `detect()`.

**Why:** the three detectors all do the same five things — construct
an httpx.AsyncClient, instantiate a `DetectorResultCache`, expose
`cache_stats()`, drain the client on `aclose()`, and wrap `detect()`
with the cache template (`get_or_compute(key, lambda: self._do_detect(...))`).
Adding a fourth remote detector would copy the same boilerplate a
fourth time. Refactoring one of the existing detectors (e.g. for
the BaseRemoteDetector design itself, or for streaming, or for any
other cross-cutting work) currently requires three parallel edits.

**Why deferred:** the current shape works; the consolidation would
touch three production detectors plus their tests; and there's a
real divergence point (each detector's `detect()` signature
differs — LLM has `api_key`/`model`/`prompt_name`, gliner has
`labels`/`threshold`, PF has nothing) that needs careful handling.
The benefit is structural cleanliness, not a fix for a current bug,
so it's worth deferring until the next detector lands or a related
refactor coincides.

**Concrete trigger:**

- A fourth remote detector is on the way (the boilerplate cost
  becomes 4x).
- A cross-cutting refactor (e.g. retry policy, request logging,
  per-call telemetry) wants a single integration point across all
  remote detectors.
- The three detectors drift apart — e.g. one starts logging
  request bodies and the others don't, or one's `aclose()` grows
  cleanup logic the others lack.

**Sketch:**

1. Create `detector/remote_base.py` with `BaseRemoteDetector`
   (or a mixin). It owns:

   - `self.url`, `self.timeout_s`, `self._client`
   - `self._cache: DetectorResultCache`
   - `cache_stats()` → `self._cache.stats()`
   - `aclose()` → `await self._client.aclose()`
   - A template `detect()` that calls `self._cache_key(text, **kwargs)`
     and `self._do_detect(text, **kwargs)`.

2. Subclasses provide:

   - `_cache_key(self, text, **kwargs) -> Hashable` — builds the
     detector-specific cache key (LLM: `(text, model, prompt_name)`;
     gliner: `(text, labels_tuple, threshold)`; PF: `(text,)`).
   - `_do_detect(self, text, **kwargs) -> list[Match]` — the actual
     HTTP call. Same shape as today's `_do_detect` methods, just
     virtual.

3. The diverging `detect()` signatures stay subclass-specific.
   The base's `detect()` is `async def detect(self, text, **kwargs)`
   and dispatches via `_cache_key` / `_do_detect`. Subclasses
   declare their concrete signature for type-checker friendliness:

   ```python
   class LLMDetector(BaseRemoteDetector):
       async def detect(
           self, text, *, api_key=None, model=None, prompt_name=None,
       ) -> list[Match]:
           return await super().detect(
               text, api_key=api_key, model=model, prompt_name=prompt_name,
           )
   ```

4. `Pipeline._run_detector` is unaffected — it already calls
   `det.detect(text, **kwargs)` with the spec-prepared kwargs.
5. Tests: each detector's existing test file stays valid (the
   public `detect()` signature is unchanged); add a small
   `tests/test_base_remote_detector.py` for the shared template
   itself if useful.

**Non-goals:**

- **Don't force a single `detect()` signature.** The whole point of
  the divergence is per-detector overrides; flattening them into a
  generic `**kwargs` would lose IDE autocomplete and type-check
  coverage at every call site.
- **Don't pull regex/denylist into the base.** They're in-process,
  not remote, and have no httpx client or cache. The base is for
  *remote* detectors specifically.
- **Don't extract the cache instantiation behind a factory.** The
  per-detector `cache_max_size` config is the right granularity;
  hiding it behind a factory adds indirection without saving lines.

**Effort:** medium. Touches 3 detectors + 3 test files; the
production code change is mechanical, the test side might surface
unexpected coupling.

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

---

## Co-locate launcher metadata with each detector module

**What:** today the launcher-side metadata for a detector
(`LauncherSpec` with `ServiceSpec`, env passthroughs, container
name, ports, cache mount paths, GPU flags) lives in
[`tools/launcher/spec_extras.py`](tools/launcher/spec_extras.py),
separate from the detector module itself. A contributor adding a
new detector has to remember to update *two* files: the detector
module (with `CONFIG`, `SPEC`, the `Detector` class) and
`spec_extras.py` (with the launcher-side metadata).

The proposal: move each detector's launcher metadata into its own
sibling file (`detector/llm_launcher.py`,
`detector/remote_privacy_filter_launcher.py`, etc.) and have
`tools/launcher/spec_extras.py` build `LAUNCHER_METADATA` by
importing those siblings. The physical separation that keeps
`tools/` out of the wheel is preserved (the `_launcher.py` files
sit next to the detector modules but are imported lazily by the
launcher, never by the package's `__init__`).

**Why deferred:** structural refactor with no current operator
pain. Adding a new detector is the natural moment — at that point
the cohesion improvement has a concrete use-site, and you're
already in the right files.

**Trigger:** the next detector lands.
