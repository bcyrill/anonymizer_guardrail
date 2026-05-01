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

## Migrate `Config` to pydantic-settings

**What:** replace the hand-rolled `Config` dataclass + `_env_int` /
`_env_bool` helpers in `src/anonymizer_guardrail/config.py` with a
`pydantic_settings.BaseSettings` model. Same env-var names, same
fields — the change is in the parsing layer.

**Why:** the current `_env_int` (`config.py:32-39`) silently swallows
`ValueError` and falls back to the default. `PORT=abc` becomes `8000`
with zero signal — exactly the class of misconfiguration that should
crash at boot, not 30 minutes later when the operator notices the
service is on the wrong port. pydantic-settings raises a clear
validation error at import time. Other wins: built-in `.env` support
(useful for local dev), automatic type coercion, free
`PORT: int = Field(ge=1, le=65535)`-style range validation, JSON
schema export for ops dashboards.

**Why deferred:** pure modernization, not a bug fix. The existing
parsers work; the silent-fallback issue has not been a real-world
problem. Timed for the next dependency-bump window.

**Sketch:**

1. Add `pydantic-settings` to `pyproject.toml`.
2. Convert `Config` to inherit from `BaseSettings`. The existing field
   names map 1:1; replace `os.getenv` defaults with normal defaults
   plus `model_config = SettingsConfigDict(env_file=".env",
   case_sensitive=False)`.
3. Per-detector configs (`detector/llm.py:LLMConfig`,
   `privacy_filter.py:CONFIG`, `remote_gliner_pii.py:GlinerPIIConfig`,
   `regex.py`, `denylist.py`) follow the same pattern. Keep them
   per-module — the grouping is what makes the env table operator-
   readable.
4. Drop `_env_int` / `_env_bool` once nothing imports them. They're
   re-exported from `config.py`; check call sites in detector modules
   and remove imports.
5. Confirm test monkey-patches still work — many tests rebuild
   `CONFIG` after env changes, and pydantic-settings may need
   `Config.model_construct()` or a refresh helper.

**Non-goals:**

- Don't go further and pull every operator-tunable string into Pydantic
  validators (e.g. validating `DETECTOR_MODE` token names there). The
  detector loader's warn-and-skip behavior is intentional for forward-
  compat.

---

## Convert `Overrides` to a Pydantic model

**What:** rewrite `api.Overrides` as a `pydantic.BaseModel` with
`field_validator`s, replacing the ~150 lines of manual `_parse_*`
helpers in `api.py` (`_parse_locale`, `_parse_threshold`,
`_parse_label_list`, `_parse_detector_mode`) plus the per-key
`if/elif` chain in `parse_overrides`.

**Why:** the current parser is mostly boilerplate — type checks,
strip-and-split, length caps, list-vs-string handling. Pydantic does
all of this declaratively with validators and `Field(max_length=…)`.
The api.py file is ~280 lines today; a Pydantic conversion plausibly
halves it, with stronger validation as a side effect.

**Why deferred:** the existing parser has a non-default behavior
contract — *"each unknown / malformed field logs a warning and is
dropped; the request itself is never blocked"* (see docstring,
`api.py:185-191`). Pydantic's default is to raise on the first
validation failure. The migration needs either:

- per-field `try/except ValidationError` wrapping in `parse_overrides`
  (gets back the lenient behavior at the cost of some of the
  declarative-ness Pydantic gives us), or
- `model_validator(mode="before")` that drops bad fields up-front
  (cleaner but harder to log per-field which key failed and why).

Neither is hard; neither is free. Worth doing alongside the
`pydantic-settings` migration so both land together.

**Sketch:**

1. `class Overrides(BaseModel)` with frozen config (it's hashed in the
   detector cache key).
2. `Annotated[tuple[str, ...] | None, BeforeValidator(_split_csv)]` for
   `faker_locale`, `detector_mode`, `gliner_labels` — accepts string
   or list, normalizes to tuple. Length caps via `Field(max_length=…)`.
3. `Annotated[float | None, …]` for `gliner_threshold` with
   `Field(ge=0, le=1)`. Reject `bool` explicitly in a validator
   (Python's `True == 1` would otherwise slip through).
4. Per-field error handling: keep `parse_overrides` as a thin wrapper
   that iterates `raw.items()`, validates one field at a time via
   `Overrides.model_validate({key: value})` (or per-field constructor
   calls), catches `ValidationError`, logs, and merges into a final
   `Overrides`.
5. Tests stay the same — the contract is unchanged from the caller's
   perspective. The internal parsing is just shorter.

**Non-goals:**

- Don't expose `Overrides` directly on `GuardrailRequest` as a typed
  field. The wire contract is `additional_provider_specific_params:
  dict[str, Any]` per LiteLLM's spec; we own the parsing on top of
  that.

---

## Optional structured (JSON) logging

**What:** add a `LOG_FORMAT=text|json` env var (default `text`). When
`json`, install a `python-json-logger` `JsonFormatter` on the root
logger so every log line is one JSON object — `level`, `name`,
`message`, `timestamp`, plus structured `extra={…}` fields like
`call_id`, `entity_count`, `detector`.

**Why:** today the format string in `main.py:23-26` is human-readable
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
  already gates body-content logs behind DEBUG (`main.py:93-94`);
  the structured fields we'd log (`call_id`, counts) are
  PII-free.

---

## LLM batching for small fragments

**What:** pack multiple short input texts from a single request into
one LLM detection call instead of N parallel calls. The model is
asked to return a list-of-lists (one entity list per input), and the
pipeline maps the result back to per-text matches.

**Why:** today each text in `req.texts` produces its own
`_detect_one` task (`pipeline.py:391-398`), each of which fans out to
its own LLM call (`detector/llm.py:detect`). For a request with 10
short texts, that's 10 LLM round-trips, each carrying the full system
prompt. The system prompt currently ~600 tokens (see
`prompts/llm_default.md`); 10 short inputs of ~50 tokens each means
we send ~6500 system-prompt tokens for ~500 user tokens — a 13×
overhead. Batching one round-trip with the system prompt sent once
plus a `[input1, input2, …]` user message would cut both token cost
and total wall-clock latency (one TTFT + a longer generation, vs.
ten TTFTs serialized through a concurrency cap of 10).

**Why deferred:** non-trivial. Tradeoffs:

- **Failure mode worsens.** Today one LLM call failing only loses
  detection for one text; under `fail_closed=False` the others still
  get coverage. Batched, a single bad response loses coverage for
  the whole batch — and a *partially* malformed response (model
  returns 9 of 10 expected lists) is messy to handle.
- **Hallucination accounting gets harder.** The hallucination guard
  in `_parse_entities` (`detector/llm.py:170-172`) checks each
  matched substring against the source text. With multiple sources
  in flight, the model could return a real entity from text 3 but
  attribute it to text 1's list. The guard would still drop it (it
  isn't in text 1), so we lose a real entity — net worse than
  before.
- **Heterogeneous batch sizes.** A request with one 50k-char text
  and nine 50-char texts shouldn't batch — the long one alone is
  near `LLM_MAX_CHARS`. Need a sizing heuristic.
- The other detectors (regex, denylist, privacy_filter, gliner_pii)
  don't benefit from this — only the LLM path. So the optimization
  is detector-specific and adds only-the-LLM-batches branching to
  the pipeline.

**Why deferred (continued):** the wins are real but proportional to
"many small texts per request," and our current traffic shape (mostly
single-message LiteLLM hooks with a few texts each) doesn't yet
spend much on system-prompt overhead. Worth measuring before
implementing.

**Concrete trigger:** a deployment shows a meaningful share of LLM
detection cost / latency tied to small-fragment fan-out, OR a request
shape with lots of small texts shows up (e.g. tool-call arguments,
chat history with many short turns).

**Sketch:**

1. Move LLM detection out of the per-text fan-out at the *LLM
   detector layer*. The pipeline still calls one detector per text;
   the detector itself buffers and packs.
2. Or simpler: introduce a `Pipeline._detect_batch` path that runs
   the non-LLM detectors per-text (as today) and the LLM detector
   once for the whole batch.
3. Update the system prompt to describe the input format change
   (`{"texts": ["...", "..."]}`) and the response format
   (`{"entities_per_text": [[…], […]]}` or similar).
4. Sizing heuristic: only batch texts whose combined length is below
   some fraction of `LLM_MAX_CHARS` (e.g. 50%). Texts above the cut
   go alone.
5. Hallucination guard: per-text-list, check each entity against its
   own source-text — preserves the existing protection.
6. Failure handling under `fail_closed=False`: a malformed batched
   response degrades the *whole batch* to `[]` for the LLM detector
   (other detectors still cover). Document this as a tradeoff.
7. Add a benchmark in `docs/benchmark.md` showing the
   tokens-per-request and p95 latency improvement on a realistic
   small-fragment workload.

**Non-goals:**

- Don't batch across requests. Cross-request batching needs a
  request-coalescing window (extra latency for the first request in
  the window) and undermines per-request `Overrides` (different
  requests may pick different prompts / models). Out of scope.
- Don't batch the regex / denylist / privacy_filter detectors. They
  don't benefit from amortizing a system prompt because they don't
  have one.
