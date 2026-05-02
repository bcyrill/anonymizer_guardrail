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

## LLM batching for small fragments

**What:** pack multiple short input texts from a single request into
one LLM detection call instead of N parallel calls. The model is
asked to return a list-of-lists (one entity list per input), and the
pipeline maps the result back to per-text matches.

**Why:** today each text in `req.texts` produces its own
`_detect_one` task (see `Pipeline.anonymize` in `pipeline.py`), each
of which fans out to its own LLM call (`LLMDetector.detect` in
`detector/llm.py`). For a request with 10
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
  in `_parse_entities` (`detector/llm.py`) checks each matched
  substring against the source text. With multiple sources
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

---

## Shape-anchored regex override tier in privacy-filter

**What:** add an explicit override layer to the privacy-filter
post-processing (or to the regex detector's priority logic) so
that strong-shape regex matches override conflicting model labels
for a known set of prefixes:
`AKIA…`, `ghp_…`, `gho_…`, `sk-…`, `pk-…`, `xoxb-…`, `xoxp-…`,
`Bearer …`, plus URLs starting with `http://` / `https://`. Direct
adaptation of
[chiefautism/privacy-parser's](https://github.com/chiefautism/privacy-parser/blob/main/pii_parser/hybrid.py)
`HybridPIIParser._apply_regex_backstop`.

**Why:** the empirical findings on `engagement_notes.txt` showed:

- GitHub PAT (`ghp_a1b2…`) — caught by gliner-pii at 1.000 as
  `api_key`, missed by privacy-filter.
- AWS access key (`AKIA…`) — caught by privacy-filter as part of a
  multi-line CREDENTIAL block at moderate confidence; the bare
  prefix would be a deterministic regex hit.
- Phone-vs-account mislabels: privacy-parser specifically calls out
  the case where a long digit string the model labels as PHONE is
  actually an account number; their account-regex overrides the
  phone label when the regex span fully contains the model span.

These are exactly the failure modes a shape-anchored override
list would close. Privacy-parser's strategy is: keep model output
where it exists, but if a strong-shape regex match overlaps a
model span with the *wrong* label, the regex wins.

**Why deferred:** our regex detector already runs in parallel to
privacy_filter and the pipeline's deduplication gives regex
priority for matching strings, so most of this works in practice.
The override tier is a refinement, not a fix for an active bug —
worth landing once we've confirmed the post-merge probe output
shows the override would catch real misses on real fixtures.

**Sketch:**

1. Decide where the override lives. Two options:
   - **Inside the privacy-filter detector's post-processing.** A
     small post-filter that runs after `_to_matches`: drop model
     spans whose text starts with these prefixes, ceding to the
     regex layer. No new regex pass; just span suppression.
   - **In the regex detector's priority layer.** Higher-priority
     "shape-anchored" rules that override existing regex matches
     and signal to the dedup phase that they should win against
     overlapping model spans.

   Option A is more targeted (only touches privacy-filter). Option
   B is more general (covers gliner-pii misses too) but bigger.
2. Build the prefix list:
   - Schemed URLs: `http://`, `https://`
   - API key prefixes: `AKIA`, `ghp_`, `gho_`, `sk-`, `pk-`,
     `xoxb-`, `xoxp-`
   - Token-like: `Bearer ` (with trailing space — distinguishes
     from prose mention of the word)
3. Tests:
   - Feed mock model output that mislabels `ghp_a1b2…` as `OTHER`
     (or any wrong label), assert the override drops it.
   - Same for `AKIA…`, `Bearer abc…`, `https://example.com/x`.
   - Sanity: a `Bob Roberts` PERSON span doesn't get accidentally
     overridden (no prefix matches).
4. The phone-vs-account containment swap from privacy-parser is a
   separate sub-task — defer until we observe the failure mode in
   our fixtures (we haven't yet).

**Non-goals:**

- Don't replace the regex detector's existing rules — this is an
  override *tier*, not a rewrite.
- Don't add prefixes for `cryptocurrency_address` / `private_key`
  shapes yet. The seven listed above are the empirically-needed
  set; expand only with evidence.

---

## opf-based Viterbi decoding (decoder swap with broad recall + boundary wins)

**What:** replace
`transformers.pipeline("token-classification", aggregation_strategy="first")`
in both `services/privacy_filter/main.py` and
`src/anonymizer_guardrail/detector/privacy_filter.py` with the
[`opf`](https://github.com/openai/privacy-filter) package's
constrained-Viterbi decoder.

The migration was originally framed as a root-cause fix for the
`\n\n` over-merge bug. **The Phase-1 spike (now done — see
`services/privacy_filter/scripts/spike_opf.py` and "Spike results"
below) showed the win is significantly broader than that** —
opf's *default* Viterbi already produces zero `\n\n` over-merges
on both fixtures, plus tighter span boundaries, materially higher
recall, and better label assignments than HF's `aggregation_strategy`.
Calibration tuning (the original headline lever) turned out to be
a marginal refinement, not the load-bearing piece.

**Why:** four independent wins from the decoder swap, validated
empirically on `sample.txt` and `engagement_notes.txt`:

1. **Zero `\n\n` over-merges**, with or without calibration. The
   bug we patched with the split pass in `_to_matches` step 3
   doesn't occur in the first place.
2. **Tighter span boundaries** — opf doesn't absorb trailing
   `,`/`.`/`)` into spans, eliminating a class of cosmetic
   surrogate-output noise (`1987-03-22),` → `1987-03-22`,
   `+1 415-555-0181.` → `+1 415-555-0181`, `Marcus Chen,` →
   `Marcus Chen`).
3. **Materially higher recall**: opf catches entities HF misses,
   including `jane.doe@example.com` (HF returned 0
   `EMAIL_ADDRESS` matches on `sample.txt`), the GitHub PAT
   `ghp_a1b2…` (previously needed gliner-pii to find), the
   plaintext passwords `Sp4rkl3-Pony!@` and `jenkinsCI123`, the
   SHA-256 binary hash, and the `SVC-2026-04-117` ticket number.
4. **Better label assignments**: `siem.acmecorp.local` is
   `private_url` instead of `CREDENTIAL`; the NTDS hash is one
   tight `secret` span instead of three weakly-confident
   `IDENTIFIER` fragments.

The case for migrating is no longer "fix one bug" — it's "swap to
the decoder OpenAI actually trained the model with, and pick up
half a dozen recall improvements as a side effect."

**Why deferred:** Phase 1 gate has passed; remaining 4–5
person-days of migration work (Phases 3–6 — Phase 2 calibration
is now optional) is just a question of scheduling. The current
post-fix output (per-label gap caps + paragraph-split pass) is
correct on the fixtures we have; this migration is a recall +
correctness upgrade, not a hot fix. Reasonable to wait for a
window where the dependency churn (transformers → opf,
Containerfile changes, mock-surface change in tests) doesn't
collide with other in-flight work.

### Spike results (Phase 1 outcome)

Run via `python services/privacy_filter/scripts/spike_opf.py` —
output committed mentally to
`services/privacy_filter/scripts/PROBE.md` (will be folded into
the doc as part of Phase 6).

**All three calibration profiles produced 0 over-merges across
both fixtures**:

| fixture | profile | spans | with `\n\n` |
|---|---|---|---|
| `sample.txt` | default | 19 | 0 |
| `sample.txt` | anti-merge | 19 | 0 |
| `sample.txt` | privacy-parser | 19 | 0 |
| `engagement_notes.txt` | default | 16 | 0 |
| `engagement_notes.txt` | anti-merge | 16 | 0 |
| `engagement_notes.txt` | privacy-parser | 16 | 0 |

The three profiles produced **identical span tables** in 5 of 6
runs. The single observed difference: on `engagement_notes.txt`,
the `anti-merge` profile terminated the NTDS-dump `secret` span
at `[1616:1689]` (excluding the `bob.smith:` username prefix)
while `default` and `privacy-parser` both produced `[1606:1689]`
(including it). That's a marginal correctness win for our biases
but not a crisis — the default already gets it 90% right.

**Implication: ship with the default decoder.** Calibration JSON
becomes a follow-on optimization, gated on a future corpus where
the marginal recall/precision delta actually matters. Don't pin
calibration values into the migration's MVP.

### Established facts

`opf` package, from `openai/privacy-filter` `pyproject.toml`:

- Pkg name `opf`, Python ≥3.10, Apache-2.0.
- Deps: `huggingface_hub, numpy, packaging, torch, safetensors,
  tiktoken`. **No `transformers`** — opf has its own runtime.
- Not on PyPI. Install via
  `pip install git+https://github.com/openai/privacy-filter@<sha>`.
- Model auto-downloads to `~/.opf/privacy_filter` (override via
  `$OPF_CHECKPOINT`).
- Public API: `from opf._api import OPF, RedactionResult, DecodeOptions`.

Constructor signature:

```python
OPF(
    model: str | PathLike | None = None,
    device: Literal["cpu", "cuda"] = "cuda",
    output_mode: Literal["typed", "redacted"] = "typed",
    decode_mode: Literal["viterbi", "argmax"] = "viterbi",
    ...
)
```

Plus `opf.set_viterbi_decoder(calibration_path=...)` for loading the
operating-points JSON. JSON shape (per privacy-parser's
`ModelPIIParser._write_calibration`):

```json
{"operating_points": {"default": {"biases": {
    "transition_bias_background_stay": 0.0,
    "transition_bias_background_to_start": 0.0,
    "transition_bias_inside_to_continue": -0.3,
    "transition_bias_inside_to_end": 0.2,
    "transition_bias_end_to_background": 0.5,
    "transition_bias_end_to_start": 0.0
}}}}
```

`opf.redact(text)` returns `RedactionResult` with
`.detected_spans: tuple[DetectedSpan, ...]`, each having
`label / start / end / text` — the same shape our `_to_matches`
already consumes, so no per-span format adapter needed.

### Phased plan

**Phase 1 — Spike** ✅ **DONE**

Spike script at `services/privacy_filter/scripts/spike_opf.py`
runs opf with three calibration profiles (no-tuning baseline,
anti-merge tuning for our use case, privacy-parser's pro-merge
tuning for contrast) against `sample.txt` and
`engagement_notes.txt`. **Result: gate passed** — see "Spike
results" above. Default Viterbi alone clears the criteria; the
anti-merge profile is a marginal additional refinement.

Install reference (kept here in case the spike needs to be re-run
after model or opf updates):

```bash
# Two-step CPU-only install — pre-install torch from PyTorch's
# CPU wheel index BEFORE opf, otherwise opf pulls torch's CUDA
# build from PyPI (~3 GB extra) even on CPU-only hosts:
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install git+https://github.com/openai/privacy-filter@main
# ~3 GB of model weights download on first run to ~/.opf/privacy_filter.
```

Recovery if the install order was wrong (the CUDA torch wheel
pulls 15+ `nvidia-*` packages plus `triton`, package names vary
by CUDA version cu12 / cu13 / unsuffixed):

```bash
pip freeze | grep -iE '^(nvidia-|triton)' | sed 's/[=@].*//' \
    | xargs -r pip uninstall -y
pip uninstall -y torch
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install --force-reinstall --no-deps \
    git+https://github.com/openai/privacy-filter@main
```

**Phase 2 — Calibrate biases** *(optional; defer until needed)*

The spike showed default Viterbi already wins on our fixtures.
Calibration tuning is a marginal refinement worth skipping in the
initial migration — ship with the default decoder, add calibration
only when a future corpus surfaces a recall/precision gap that the
default doesn't close.

If a corpus does drive a need:

- Iterate on a calibration JSON against the affected fixtures.
- Pin the resulting JSON at
  `services/privacy_filter/calibration.json` (committed). Both
  the service and the in-process detector load from this same
  path via `PRIVACY_FILTER_CALIBRATION` env (defaults to
  `/app/calibration.json` in the container, project-relative path
  in dev).
- The spike's `anti-merge` profile (positive `end_to_background`,
  negative `inside_to_continue`, mild positive `inside_to_end`) is
  a reasonable starting point — it terminated one
  semicolon-prefixed hash span more tightly than default, with no
  observed regressions.

**Phase 3 — Migrate the service (1.5 days)**

`services/privacy_filter/main.py`:

- Drop `from transformers import pipeline` import.
- Replace `_load_pipeline()`:
  ```python
  def _load_model(model_name: str | None) -> OPF:
      from opf._api import OPF
      opf = OPF(model=model_name or None, device="cpu",
                decode_mode="viterbi")
      # Optional calibration — load only if the JSON exists.
      # Default Viterbi already produces clean spans on every
      # fixture we've measured; this is a hook for future corpora
      # that need precision/recall tuning, not a required step.
      calib = os.environ.get("PRIVACY_FILTER_CALIBRATION",
                              "/app/calibration.json")
      if os.path.exists(calib):
          opf.set_viterbi_decoder(calibration_path=calib)
      return opf
  ```
- Replace `await asyncio.to_thread(pipeline_obj, text)` with
  `await asyncio.to_thread(opf.redact, text)`; iterate
  `result.detected_spans` instead of the HF dict list.
- Field-name remap: opf emits `label` directly; we currently look
  for `entity_group or entity`. The `_LABEL_MAP` keys
  (`private_person`, `private_email`, …) match what opf emits, so
  the mapping itself stays the same.
- **Keep** the per-label `_MAX_GAP_BY_LABEL` merge pass and the
  `_PARAGRAPH_BREAK` split pass — both as defense in depth. The
  spike showed neither fires on our fixtures with opf, but
  production traffic is broader than the fixtures and the cost of
  these passes is negligible. Re-evaluate after collecting some
  production data: if neither pass ever fires across N requests,
  remove them then.

`services/privacy_filter/Containerfile`:

- Replace the transformers install with:
  ```dockerfile
  RUN pip install --no-cache-dir \
      "git+https://github.com/openai/privacy-filter@<pinned-sha>"
  ```
  Pin to a specific SHA — opf isn't versioned and master can move.
- The existing `TORCH_INDEX_URL` build-arg pattern (CPU vs cu130
  wheel index) still applies; opf brings `torch` as a dep but
  doesn't pin a CUDA build. No additional plumbing needed.
- Set `OPF_CHECKPOINT=/app/.cache/huggingface/opf` so we keep the
  existing volume name (and the ops-side migration is just a tag
  bump). The model auto-downloads from HuggingFace
  (`openai/privacy-filter`) on first run; ~2.8 GB.
- *(Optional)* Copy `calibration.json` into `/app/calibration.json`
  if Phase 2 ever lands.

`tools/launcher/spec_extras.py` privacy-filter `LauncherSpec`:

- No volume-name change needed if we use `OPF_CHECKPOINT`.

**Phase 4 — Migrate the in-process detector (1 day, lockstep)**

Same code shape in
`src/anonymizer_guardrail/detector/privacy_filter.py`. Critical
that the parity contract holds: same `_LABEL_MAP`, same merge /
split, same calibration JSON.

`pyproject.toml`:

- The optional `[privacy-filter]` extra changes from `transformers`
  to `opf @ git+https://github.com/openai/privacy-filter@<sha>`.
  PEP 508 supports VCS URLs in extras.
- Note in `docs/development.md` that this extra needs git available
  to install.

**Phase 5 — Adapt test mocks (1 day)**

- Existing 19 `test_privacy_filter.py` tests mock `_load_pipeline()`
  to return a callable consumed as `pipeline_obj(text) -> list[dict]`.
  New mock target: `OPF`-shaped object with
  `.redact(text) -> RedactionResult`.
- Build a `_FakeOPF` test helper that returns a frozen
  `RedactionResult`-shaped object.
- Most tests still apply unchanged — they exercise the merge/split
  passes and label mapping, both of which run *after* the model.
  Only the fixture's input shape changes.
- New tests:
  - Calibration JSON path env-var-overridable; missing file → no
    error, falls back to opf's default decoder.
  - Mock model returns clean spans (no `\n\n`); merge/split passes
    are no-ops in the happy path. Confirms the post-processing
    layer doesn't accidentally split spans the model got right.

**Phase 6 — Empirical validation + docs (½ day)**

- Re-run `sample.txt` and `engagement_notes.txt` probes through the
  rebuilt service. Compare outputs against the spike's findings —
  the running service should match what `spike_opf.py default`
  produced (since that's the migration's MVP configuration).
- Update `services/privacy_filter/scripts/PROBE.md` "Empirical
  findings" section with post-Viterbi span numbers. Specifically
  call out the new wins relative to the post-fix HF baseline:
  tighter span boundaries (no trailing `,`/`.`/`)`), recall on
  `jane.doe@example.com` / `ghp_…` / `Sp4rkl3-Pony!@` /
  `jenkinsCI123` / SHA-256 / `SVC-2026-04-117`, and the
  `siem.acmecorp.local` URL relabel.
- Add a "Decoder" section to `docs/detectors/privacy-filter.md`
  noting the switch from `transformers.pipeline` to opf and what
  the calibration-JSON env var (`PRIVACY_FILTER_CALIBRATION`)
  does. Don't write a tuning guide yet — that's load-bearing only
  if Phase 2 lands.

### Interaction with other follow-up tasks

- **The regex-backstop-tier task above** becomes *more* valuable
  post-migration, not less. opf labels more strings as `secret`
  than HF does (it caught `dc01.acmecorp.local` and the NTDS hash
  block under `secret`); regex's stable shape-anchors
  (`AKIA…` → CREDENTIAL, `ghp_…` → CREDENTIAL, `^[a-f0-9]{32}$` →
  HASH) become the right way to disambiguate what kind of secret
  each match actually is. Worth landing the regex backstop after
  this migration, not before.
- **Per-label gap thresholds** (already shipped) become partially
  redundant once opf is decoding cleanly. Keep them as defense in
  depth for the same reason as the merge/split passes; revisit
  removal after production data shows they never fire.

### Risks and open questions

| Risk | Mitigation |
|---|---|
| **opf API instability** (no PyPI = no version contract) | Pin Containerfile install to a specific commit SHA. Vendor a copy if upstream ever breaks. |
| **Image size delta** | opf brings `torch + safetensors + huggingface_hub + tiktoken`; transformers extras bundle similar. Spike confirms model weights are ~2.8 GB (download timing observed at 192 MB/s in the spike run, consistent with HuggingFace's openai/privacy-filter repo). Final image-size delta to be measured in Phase 3 against a transformers-built image; expected net-neutral or slightly leaner because opf doesn't pull `transformers` itself. |
| **Device default differs from current behaviour** | We currently use `transformers.pipeline(...)` without an explicit `device` argument, which defaults to CPU. The `OPF` constructor's `device` parameter defaults to `"cuda"` (see `opf/_api.py`'s signature: `device: Literal["cpu", "cuda"] = "cuda"`). The fix is trivial — pass `device="cpu"` explicitly in `_load_model()` — but it must be done deliberately or the cpu image will silently try to load CUDA on hosts without a GPU. The CUDA path itself works fine (see `examples/scripts/finetuning/finetune_secret_demo.sh` in the privacy-filter repo for a CPU-mode reference); we just need a `PRIVACY_FILTER_DEVICE` env or build-arg to flip to `"cuda"` for cu130 image builds. |
| **Mock-surface change in tests** | The 19 existing `test_privacy_filter.py` tests mock `_load_pipeline()` and consume the result as `pipeline_obj(text) -> list[dict]`. Phase 5 swaps this to a `_FakeOPF` returning a `RedactionResult`. Most assertions stay (they target the merge/split/label-mapping passes), but every test's fixture-construction line changes. Schedule a focused half-day for this rather than threading it through Phase 3/4 ad-hoc. |
| **Label set differs subtly from HF output** | Spike showed opf labels `dc01.acmecorp.local` and `siem.acmecorp.local` as `private_url` / `secret` rather than `OTHER` — the recall increase pulls in entities that previously fell through. Run a one-time audit of `_LABEL_MAP` after Phase 4: any label opf emits that we don't map is currently routed to `OTHER`. Probably fine; worth a sanity check against a sample of production traffic. |
| **Calibration tuning is corpus-specific** | Ship a reasonable default; expose `PRIVACY_FILTER_CALIBRATION` env so deployments with different traffic can override. Don't bake corpus-specific values. |
| **Mock surface change breaks tests** | Wrap mock construction in a helper that produces opf-shaped or pipeline-shaped output via a flag during a transition window. |

### Effort summary

- Total: **5–7 person-days** for full migration.
- Stop-and-revert checkpoint: end of Phase 1.
- Smallest viable shipping unit: Phases 1–3 (service-side only).
  In-process can land separately as long as the parity contract is
  preserved.

**Non-goals:**

- Don't switch other detectors (gliner-pii uses a different model
  with no equivalent tuning surface).
- Don't expose the bias values as runtime env vars yet — calibrate
  to good defaults first. Exposing too early invites operators to
  twiddle without understanding the BIOES transition implications.
- Don't try to get raw logits out of the HF pipeline and re-decode
  them ourselves. The opf wrapper owns the decoder; reimplementing
  it outside opf means reproducing the calibration loader and is
  not worth the maintenance burden.
