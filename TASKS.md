# Follow-up tasks

Backlog of work that's been considered and deliberately deferred. Each entry
captures what to build, why it matters, and the rough shape of the work so a
future contributor (or future-you) can pick it up cold.

---

## Remove deprecated `FAIL_CLOSED` env var

**What:** drop the backward-compat shim in
`detector/llm.py:_llm_fail_closed_default` that still honours the old
`FAIL_CLOSED` env var. The new name is `LLM_FAIL_CLOSED`, introduced
when the privacy-filter detector got its own fail-mode flag and the
old name became misleading.

The matching `--fail-open` / `--fail-closed` cli aliases were
removed implicitly when the bash `cli.sh` was replaced with the
Click-based launcher (the new launcher only knows `--llm-fail-open`).

**Why deferred:** the env var shim still fires for operators upgrading
from the FAIL_CLOSED era; emits a loud deprecation warning at boot.
Drop only after a release or two has baked in the rename.

**Why it matters:** the shim is small (~20 lines), but it muddles the
surface area — every reader has to learn that the LLM detector's
fail-closed flag MIGHT come from the legacy `FAIL_CLOSED` env var.

**Sketch:**

1. Remove `_llm_fail_closed_default` from
   `src/anonymizer_guardrail/detector/llm.py`; inline
   `_env_bool("LLM_FAIL_CLOSED", True)` on the LLMConfig field.
2. Update or remove the matching test in `tests/test_config.py`
   (the deprecation-shim tests).
3. Grep for any lingering `FAIL_CLOSED` references and update.

**Concrete trigger:** the next minor release after the rename ships,
or whenever an operator-facing deprecation cycle has clearly passed
(no recent reports of operators hitting the warning).

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

## ~~Detector registration API — bash launcher consolidation~~ — done

**Closed.** Both halves of this task shipped. Brief record below; see
[DEVELOPMENT.md](DEVELOPMENT.md) for the canonical "adding a new
detector" walkthrough.

**Python side (registry):**
- `detector/spec.py` defines `DetectorSpec` (name, factory, module ref
  for live CONFIG lookup, per-call kwargs builder, semaphore + stats
  prefix, typed-error + blocked-reason).
- Each detector module owns its own `<Name>Config` dataclass + a
  `CONFIG` instance + a `SPEC` constant.
- Central `Config` shrank to cross-cutting fields only (vault, surrogate,
  http server, faker locale).
- `detector/__init__.py` exposes `REGISTERED_SPECS`, `SPECS_BY_NAME`,
  `SPECS_WITH_SEMAPHORE`, `TYPED_UNAVAILABLE_ERRORS`.
- `Pipeline.__init__` / `_run` / `stats()` and `main.py`'s exception
  handler all iterate the registry — no per-detector branches.

**Bash launcher side:** the original plan was to keep bash and feed it
a generated manifest. Instead the bash launchers (`cli.sh`, `menu.sh`,
`_lib.sh`) were **rewritten in Python** under `tools/launcher/`:

- `cli.sh` → 5-line wrapper that execs `python -m tools.launcher`
  (Click CLI, options grouped per detector via a custom `GroupedCommand`).
- `menu.sh` → 5-line wrapper that execs `python -m tools.launcher.menu`
  (Textual single-screen TUI).
- `_lib.sh` deleted entirely. Per-detector launcher metadata lives in
  `tools/launcher/spec_extras.LAUNCHER_METADATA` — the launcher
  consumes the Python registry directly.

The launcher lives **outside the production wheel** (`tools/` is not
under `src/`, hatch's wheel target excludes it implicitly). Production
operators install `anonymizer-guardrail` without `[dev]` and never see
typer/click/textual/rich. Devs install `pip install -e ".[dev]"` for
the launcher.

Adding a Python-only in-process detector now touches **2 files** (the
new detector module + `detector/__init__.py`). With a service container
+ launcher CLI/menu support: **~10 files**. Down from the pre-refactor
~10 Python files plus 3 bash files for every new detector.

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

**Why deferred:** the README is already 1000+ lines and a full docs
split (multi-file `docs/` folder) is on the table as a separate
follow-up. DEVELOPMENT.md was factored out as a first step — the
contributor-facing material now lives there — but the operator-facing
README is still the canonical home for detector overviews, env-var
tables, and deployment guidance, and it's getting unwieldy. Either
fold the GLiNER content into the existing README or wait until the
broader split decides where detector docs should live.

**Why it matters:** the detector is fully functional — operators
discovering it via `cli.sh --help` or the menu can run it, but they
won't find the design docs (when to pick it vs privacy_filter, the
license caveat, the experimental status) without reading the source.

**Sketch:**

1. Add a `### GLiNER-PII detector` section under the existing
   *Privacy-filter detector* section in `README.md`, mirroring its
   shape: what the model is, where it differs, label / threshold
   semantics, how to run the service, when to pick it, the NVIDIA
   Open Model License caveat.
2. Add a `gliner-pii-service` row to the *Container images* section
   noting it's local-build-only (CPU + CUDA matrix).
3. Extend the *Capping detector concurrency* section with
   `GLINER_PII_MAX_CONCURRENCY`.
4. Update DETECTOR_MODE examples that currently list
   `regex,denylist,privacy_filter,llm` to mention the `gliner_pii`
   token as an option.
5. (Optional) revisit as part of the broader README → `docs/` split
   if that lands first — the detector content moves to
   `docs/detectors.md` (or similar) instead of growing the README.

**Concrete trigger:** next time someone touches the detector docs
in the README, OR when the broader docs split is decided.

**Non-goals:**

- Don't move the existing `services/gliner_pii/README.md` into the
  top-level docs — keep the service-specific README colocated with
  its code. The top-level README links there for the deep-dive.
