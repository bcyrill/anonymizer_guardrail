# Follow-up tasks

Backlog of work that's been considered and deliberately deferred. Each entry
captures what to build, why it matters, and the rough shape of the work so a
future contributor (or future-you) can pick it up cold.

---

## Remove deprecated `FAIL_CLOSED` env var + `--fail-open` / `--fail-closed` cli flags

**What:** drop the backward-compat shims that keep the old `FAIL_CLOSED`
env var and the old cli aliases working. The new names are
`LLM_FAIL_CLOSED` (env var) and `--llm-fail-open` / `--llm-fail-closed`
(cli flags), introduced when the privacy-filter detector got its own
fail-mode flag and the old name became misleading.

**Why deferred:** doing it now would break operators upgrading from the
previous release in a single step. The shim emits a loud deprecation
warning at boot (env var) or per-invocation (cli flag), so anyone still
on the old names will see the message before this task fires.

**Why it matters:** the shim is small (~15 lines in
`config.py:_llm_fail_closed_default`, two lines in `cli.sh`), but it
muddles the surface area — every reader has to learn that
`config.llm_fail_closed` MIGHT come from `FAIL_CLOSED` instead of
`LLM_FAIL_CLOSED`. Drop it once the rename has had a release or two
to bake in.

**Sketch:**

1. Remove `_llm_fail_closed_default` from `src/anonymizer_guardrail/config.py`;
   inline `_env_bool("LLM_FAIL_CLOSED", True)` on the field.
2. Remove the two `--fail-open` / `--fail-closed` deprecated-alias
   cases from `scripts/cli.sh`.
3. Drop the `(Renamed from FAIL_CLOSED — …)` parenthetical from the
   README env-var table and the `_lib.sh` globals doc.
4. Grep for any lingering `FAIL_CLOSED` references introduced after
   the rename and update them.

**Concrete trigger:** the next minor release after the rename
ships, or whenever someone notices the shim is no longer earning
its keep (no recent reports of operators hitting the deprecation
warning).

**Non-goals:**

- Don't remove the shim before at least one release with the
  deprecation warning. The whole point of the soft rename was to
  give operators a heads-up.

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
6. Document the limitation that's now solved + the new env vars.

**Concrete trigger:** the day someone adds a second replica.

**Non-goals:**

- Don't generalize to "any KV store" — one production-quality backend
  (Redis) covers the use case. Cassandra/DynamoDB/etc. can be added
  per-need.

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

## HTTP request body-size cap

**What:** middleware that rejects POST bodies above a configurable byte
limit with HTTP 413, before Pydantic deserializes them.

**Why:** `LLM_MAX_CHARS` caps the text we send to the detection LLM, but
that check fires *after* Pydantic has already parsed the request body.
A caller — malicious or just confused — can POST a 500 MB body and the
process happily allocates memory to parse it. Soft DoS vector. Distinct
from `LLM_MAX_CHARS` because that's an LLM-context-window concern, not a
DoS-protection concern.

**Why deferred:** when the guardrail sits inside a private network behind
LiteLLM, the surface area is small and there's typically an upstream proxy
(nginx, Envoy, ALB) already enforcing body-size limits. Becomes urgent the
moment the endpoint sits at a trust boundary.

**Sketch:**

1. Add `MAX_BODY_BYTES` env var (default ~10 MiB — enough for very
   chatty requests, small enough that a runaway client can't OOM us).
2. Add a Starlette/FastAPI middleware that reads `Content-Length` and
   short-circuits with a 413 response when over the limit. Fall back to
   incremental read with a counter for clients that don't send
   `Content-Length`.
3. Document the difference from `LLM_MAX_CHARS` in the README env table.

**Concrete trigger:** the guardrail starts accepting traffic from
untrusted callers, or an upstream proxy isn't reliably enforcing the
limit.

**Non-goals:**

- Not a per-tenant rate limit. That's a separate concern (and probably
  belongs in LiteLLM, not the guardrail).

---

## Per-request `gliner_labels` / `gliner_threshold` overrides

**What:** plumb `gliner_labels` (list[str]) and `gliner_threshold`
(float) through `additional_provider_specific_params` so the
RemoteGlinerPIIDetector can override its configured defaults on a
per-call basis. Same shape as the existing per-request overrides
(`use_faker`, `detector_mode`, `regex_overlap_strategy`, `llm_model`,
`llm_prompt`, `regex_patterns`, `denylist`).

**Why:** the differentiator of GLiNER over a fixed token-classification
model is *zero-shot labels* — the entity-type vocabulary is an input
to the model. Letting callers pick labels per request is what unlocks
the "different vocab per use case" story (one route asks for medical
labels, another for finance labels, no redeploy). Today the labels
are pinned to whatever the operator configured server-side.

**Why deferred:** the gliner_pii detector is brand-new and being
evaluated locally first. Adding per-request overrides before the
basic path is validated is premature — the override surface is the
kind of thing that's easier to design right once we know how
operators actually use the detector.

**Sketch:**

1. Add `gliner_labels` (tuple[str, ...] | None) and `gliner_threshold`
   (float | None) fields to `Overrides` in `src/anonymizer_guardrail/api.py`.
2. Extend `parse_overrides` to accept both keys with the same warn-and-
   ignore policy on bad types. Reuse `_parse_locale`-style normalization
   for the labels list (string OR list, comma-split when string).
3. Cap `gliner_labels` at a defensive max length (e.g. 50) — same
   shape as `_MAX_LOCALE_CHAIN`, prevents an oversized list from
   blowing up the model's input.
4. Pass `overrides.gliner_labels` and `overrides.gliner_threshold`
   into `RemoteGlinerPIIDetector.detect()` as kwargs (mirror how
   `LLMDetector.detect` takes `model` and `prompt_name`).
5. In `Pipeline._run`, plumb the overrides into the gliner branch
   (mirror the LLM branch which passes `model` / `prompt_name`).
6. Update the README's *Per-request overrides* section.

**Concrete trigger:** the day a deployment surfaces with two routes
that legitimately want different label vocabularies, or once the
detector earns its keep on real traffic and operators ask for the
flexibility.

**Non-goals:**

- Don't add a *per-request URL* override — labels and threshold are
  cheap to vary; pointing at a different inference service per
  request is a deployment-shape question that doesn't belong in the
  request body.

---

## Detector registration API

**What:** replace the per-detector hand-wiring in `Pipeline`,
`config.py`, `_lib.sh`, `menu.sh`, and `cli.sh` with a single
`DetectorSpec` descriptor each detector registers, plus an iterator
that the centralized files consume. Adding a detector should be "drop
a file under `detector/`, append to a registry list" — not "touch
seven places".

**Why:** every new detector currently requires edits in a fan-out of
files:

  * `config.py`         — env-var fields
  * `pipeline.py`       — factory entry, semaphore, in-flight counter,
                          `_run` branch, exception handler, TaskGroup
                          `except*` clause, `stats()` keys, log line
  * `main.py`           — typed exception → BLOCKED handler
  * `_lib.sh`           — `DETECTOR_HAS_*` predicate, env-var globals
                          docs, optional auto-start lifecycle, env-var
                          passthroughs in `build_run_args`, plan rows,
                          run command wiring
  * `menu.sh`           — checklist entry, submenu, labels/dialogs,
                          backend-selection, validation
  * `cli.sh`            — flags, validation, auto-start hook
  * Tests               — fixtures for the new detector + pipeline tests

The fan-out is the actual cost of "adding a detector"; the file
location of the detector itself is incidental. Centralizing the wiring
into a registry collapses most of these edits into one descriptor.

**Why deferred:** the abstraction needs to be wide enough to fit every
detector's quirks without becoming a leaky superset:

  * regex carries overlap strategy + per-call `patterns_name` override
  * llm carries per-call `api_key` + `model` + `prompt_name`
  * denylist carries per-call `denylist_name`
  * privacy_filter has both in-process and remote variants under one
    DETECTOR_MODE token, picked by URL presence
  * gliner_pii is remote-only, with per-call `labels` + `threshold`
    override (planned, see *Per-request gliner_labels* above)
  * privacy_filter and gliner_pii have an auto-start lifecycle in the
    launcher; regex / denylist don't

Designing the descriptor before we have enough datapoints risks
either (a) a too-narrow abstraction that breaks the next detector
or (b) an over-general one that's worse than the hand-wiring it
replaced. Wait until a 4th or 5th external detector lands so the
shape is forced by real variety, not anticipated.

**Sketch:**

1. Define `DetectorSpec` (dataclass) carrying:
   * `name` — DETECTOR_MODE token
   * `factory` — `() -> Detector`, may raise at boot
   * `config_class` — owns its `*_url`, `*_fail_closed`,
     `*_max_concurrency`, etc. fields; merged into `Config` via
     composition
   * `pipeline_hooks` — exposes the semaphore name, in-flight counter
     name, typed unavailable-error class, fail-closed field name,
     and a `prepare_kwargs(overrides) -> dict` for per-call args
   * `launcher` — optional auto-start metadata
     (`container_name`, `image_tag`, `port`, `health_endpoint`,
     `start_timeout_s`, env-var passthrough list)
   * `endpoint_block_reason` — string template for `main.py`'s
     BLOCKED response when the typed error fires
2. Detectors live where they live (flat files for now; can be
   packagized later if any one outgrows ~500 lines). Each module
   exposes a `SPEC = DetectorSpec(...)` constant.
3. `src/anonymizer_guardrail/detector/__init__.py` enumerates the
   modules and re-exports `REGISTERED_SPECS`.
4. `Pipeline.__init__` iterates `REGISTERED_SPECS` to set up
   semaphores, in-flight counters, the `_DETECTOR_FACTORIES` dict,
   the `stats()` payload, and the log line. `_run` dispatches via
   `spec.pipeline_hooks` instead of `isinstance` chains.
5. `main.py`'s exception handlers iterate `REGISTERED_SPECS` to
   register `(error_class → blocked_reason)` mappings.
6. `_lib.sh` reads a generated env-var passthrough table (or has
   each spec's launcher block emit its own `RUN_*_ARGS` array).
   Same for the auto-start dispatch.
7. `menu.sh` / `cli.sh` enumerate `REGISTERED_SPECS` to build the
   checklist + submenus + flags. Detector-specific dialogs stay
   per-spec but get wired up generically.
8. Tests gain a `register_test_detector` helper for pipeline-level
   tests that don't need a real detector.

**Concrete trigger:** the moment we add a detector that the existing
hand-wiring doesn't fit cleanly (e.g. one that needs a different
auth model, a streaming response, or a different fail-mode shape).
Or when the per-detector edit list crosses ~10 files.

**Non-goals:**

- Not a *plugin* system. We're not loading detectors from third-party
  packages via entry points — that's premature for a project that
  ships its own detector set. The registry stays an in-tree
  enumeration; entry-point support can be layered on later if the
  use case appears.
- Not a refactor of `Match` / `Detector` Protocol / `_dedup`. The
  shared types stay. This is wiring-level cleanup only.

---

## Document the GLiNER-PII detector + service in README

**What:** the GLiNER-PII detector (`RemoteGlinerPIIDetector`) and the
companion `gliner-pii-service` container exist with full code, tests,
build-image.sh flavours, cli.sh / menu.sh wiring, and detailed
service-level docs at `services/gliner_pii/README.md` — but the
top-level README only documents the GLINER_PII_* env vars in the
config table. The rest of the user-facing surface isn't there yet:

  * no overview mention alongside the other detectors in the intro
  * no `### GLiNER-PII detector` section parallel to the existing
    `### Privacy-filter detector` (in-process / remote split, when
    to pick it, label / threshold semantics, license note about
    NVIDIA Open Model License)
  * no entry in the *Container images* section for `gliner-pii-service`
    (CPU + CUDA matrix, baked variants, "local-only — not yet
    CI-published" note)
  * no mention in the *Capping detector concurrency* section that
    `GLINER_PII_MAX_CONCURRENCY` exists alongside the LLM and PF caps
  * `DETECTOR_MODE` examples don't include `gliner_pii`

**Why deferred:** the README was already getting unwieldy at 800+
lines, and the user has flagged a docs split (multi-file `docs/`
folder) as the next docs project. Adding more to the existing README
before that split makes the eventual reorganization more painful.
Better to add the GLiNER content as part of the docs split, in the
right new home.

**Why it matters:** the detector is fully functional — operators
discovering it via `cli.sh --help` or the menu can run it, but they
won't find the design docs (when to pick it vs privacy_filter, the
license caveat, the experimental status) without reading the source.

**Sketch:**

1. Wait for the README → `docs/` split (separate task — see the
   notes in the user conversation; not yet logged here as it's a
   broader docs reorg, not a defined deliverable).
2. In the new structure, add a `docs/detectors.md` (or similar)
   GLiNER-PII section mirroring the privacy_filter section's shape:
   what the model is, where it differs, how to run the service,
   how to wire the detector, when to pick it, the license note.
3. Add a `gliner-pii-service` row to the *Container images* section
   noting it's local-build-only.
4. Extend the *Capping detector concurrency* section with
   `GLINER_PII_MAX_CONCURRENCY`.
5. Update DETECTOR_MODE examples that currently list
   `regex,denylist,privacy_filter,llm` to mention the gliner_pii
   token as an option.

**Concrete trigger:** the README docs split lands.

**Non-goals:**

- Don't move the existing `services/gliner_pii/README.md` into the
  top-level docs — keep the service-specific README colocated with
  its code. The top-level README links there for the deep-dive.
