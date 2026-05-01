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

## Detector quality benchmark script (`scripts/test-detector-quality.sh`)

**What:** a new test driver, sibling to `scripts/test-examples.sh`,
that scores how well a chosen detector configuration redacts a
labelled corpus. The corpus and the expected entities live in a
**config file** (one per scenario — pentest, legal, healthcare,
HR, etc.), so the same script can answer "is the gliner_pii detector
worth turning on for our pentest workflow?" or "do we need both
privacy_filter AND llm for legal?"

The CLI shape mirrors `test-examples.sh`:

```bash
scripts/test-detector-quality.sh --config corpus/pentest.yaml         # against $BASE_URL
scripts/test-detector-quality.sh --config corpus/pentest.yaml --preset uuid-debug
scripts/test-detector-quality.sh --config corpus/legal.yaml --detector-mode regex,llm
```

**Why deferred:** the existing `scripts/test-examples.sh` only
checks "did this exact substring get redacted yes/no" against a few
hand-picked recipes — it's a smoke test, not a benchmark. We can
ship detector improvements without it, but operators currently can't
answer "which detector mix is best for my workload" without rolling
their own measurement.

**Why it matters:**

- Detector selection is currently guesswork. The
  [detectors index](docs/detectors/index.md) gives qualitative
  guidance ("privacy_filter is a strict subset of LLM coverage") but
  no quantitative way to verify on the operator's own data.
- Adding a new detector (gliner_pii is the live example) needs a
  way to argue "this is worth wiring in" beyond "it exists." A
  benchmark script makes that argument concrete.
- Pentest / legal / healthcare configs are different enough that
  one corpus can't speak for all of them — the config-driven shape
  scales without code changes.

**Sketch:**

### Corpus config format

Each config is a YAML file under (suggested) `tests/corpus/<name>.yaml`,
versioned with the repo so different scenarios are reproducible:

```yaml
# tests/corpus/pentest.yaml
name: "Pentest engagement transcript"
description: |
  Synthetic snippets typical of a security engagement: cracked password
  artifacts, AWS keys, internal hostnames, employee names embedded in
  prose, NTLM hashes, etc.

# Optional: pin the detector mix to test. If unset, --detector-mode on
# the CLI wins; if neither is set, use the running guardrail's default.
detector_mode: regex,denylist,privacy_filter,llm

# Optional: pin per-request overrides (regex_patterns, llm_prompt, ...)
overrides:
  regex_patterns: pentest
  llm_prompt: pentest

cases:
  - id: cracked-creds-table
    text: |
      Internal hostname dc01.acmecorp.local is reachable from
      10.0.7.42. Cracked password from NTDS dump:
      bob.smith:1107:aad3b435b51404eeaad3b435b51404ee:8846f7eaee8fb117ad06bdd830b7586c:::
    expect:
      # Each entry: literal substring that MUST be redacted, with the
      # entity type the operator considers correct.
      - text: "dc01.acmecorp.local"
        type: HOSTNAME
      - text: "10.0.7.42"
        type: IPV4_ADDRESS
      - text: "8846f7eaee8fb117ad06bdd830b7586c"
        type: HASH
      - text: "bob.smith"
        type: PERSON
        # Optional: mark items the operator EXPECTS the chosen detector
        # mix to miss, so a missed match is reported as "expected miss"
        # not a regression.
        # tolerated_miss: true

  - id: aws-creds-in-prose
    text: "Use AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY for the demo."
    expect:
      - text: "AKIAIOSFODNN7EXAMPLE"
        type: AWS_ACCESS_KEY
      - text: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        type: AWS_SECRET_KEY

# Optional: anti-cases the detectors MUST leave alone (false-positive
# sentinel). Anything redacted from these texts counts against the
# precision score.
do_not_redact:
  - id: documentation-uuid
    text: "Request id 11111111-2222-3333-4444-555555555555 is a documentation example, not real."
    must_keep: ["11111111-2222-3333-4444-555555555555"]
```

### Score shape

The script reports per-case and overall:

| Metric | What it counts |
|---|---|
| `recall` | expected substrings that got redacted / total expected |
| `recall_excluding_tolerated` | as above but ignores `tolerated_miss` items |
| `type_accuracy` | of the substrings redacted, fraction whose entity_type matches the corpus's expected type (operator can disable strict typing per-case) |
| `precision` | 1 − (false-positive redactions / total `must_keep` items in `do_not_redact`) |
| `latency_ms` | per-case wall-clock (informational, not a pass/fail) |

A summary table at the end lets the operator compare runs:

```
== Pentest corpus, DETECTOR_MODE=regex,denylist,privacy_filter,llm ==
case                          recall    type_acc  precision  latency
cracked-creds-table           4/4       4/4       —          312ms
aws-creds-in-prose            2/2       2/2       —          105ms
                              ---------------------------------------
                              12/12     11/12     1.0        avg 208ms
```

### Pluggable backends

Same `--preset` machinery as `test-examples.sh`: with no preset,
talk to `$BASE_URL`; with `--preset NAME`, spawn a guardrail via
`scripts/cli.sh --preset NAME` on a non-default port, run the
benchmark, tear it down. This lets the operator run the same corpus
against several configurations in a loop.

### Config search path

Bundle a few starter configs under `tests/corpus/` (pentest, legal,
healthcare are the obvious starters); operators can also pass their
own paths via `--config`. The script accepts either a bundled name
(`--config bundled:pentest`) or a filesystem path. Same shape as the
`bundled:NAME` / path convention in `REGEX_PATTERNS_PATH` /
`LLM_SYSTEM_PROMPT_PATH`.

### Implementation language

Python rather than bash — the score arithmetic, JSON parsing of the
guardrail's response, and YAML config loading are all painful in
shell. Lives under `tools/` (sibling to `tools/launcher/`) so it
stays out of the production wheel; `scripts/test-detector-quality.sh`
is a thin bash wrapper that execs into `python -m
tools.detector_bench` (mirroring the cli.sh / menu.sh shape).

**Concrete trigger:** the next decision about which detector mix to
ship by default, or the first time an operator asks "is gliner_pii
better than privacy_filter for my data?"

**Non-goals:**

- **Not a regression test for detector code itself** —
  `tests/test_*_detector.py` already covers that with mocked
  backends. This script measures *configurations*, not code.
- **Not a fuzzing harness.** The corpus is hand-curated to mirror the
  operator's real workload; surprise-finding is what production logs
  are for.
- **Not a load tester.** Latency is reported as informational only;
  for throughput / saturation testing, point a load generator at a
  running guardrail.
- Don't bundle a giant default corpus into the repo; the starter
  corpora should be small (~10-30 cases each) and easy for operators
  to fork. Large datasets belong in operator-side fixtures, not in
  the wheel.
