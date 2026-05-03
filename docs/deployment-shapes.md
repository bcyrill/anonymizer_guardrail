# Deployment shapes

The guardrail flexes along several orthogonal dimensions. This doc
walks through the deployment-shape decisions an operator faces — what
each one is, what the trade-offs are, and what env vars to set —
then puts them together into common recipes at the end.

If you just want a cheat-sheet, skip to [Recipes](#recipes). If you
need to make an informed choice, work through the dimensions in order.

## Quick reference

| Decision | Knobs | When you flip it |
|---|---|---|
| Workload shape | `<DETECTOR>_CACHE_*`, `PIPELINE_CACHE_*` | Multi-turn chat → cache; single-turn → don't bother. |
| Detector mix | `DETECTOR_MODE` | Cheap (regex/denylist), or add LLM/gliner/PF as your PII surface area demands. |
| Surrogate format | `USE_FAKER` | Faker for human-facing chat; opaque for machine-readable / log-friendly. |
| Override frequency | `<DETECTOR>_CACHE_*`, `PIPELINE_CACHE_BACKEND` | Heavy overrides → `cache_mode=both`; rare/none → `pipeline` is enough. |
| Round-trip vs redaction-only | `DEANONYMIZE_SUBSTITUTE` | `false` for log scrubbing / training data prep; `true` (default) for chat. |
| Failure posture | `<DETECTOR>_FAIL_CLOSED` | `true` (default) blocks on unavailable; `false` proceeds with what's available. |
| Replica count | `VAULT_BACKEND`, cache backends | Multi-replica → vault MUST be Redis; caches optionally too. |

---

## Workload shape: single-turn vs multi-turn

The cache layers (per-detector + pipeline) only earn their keep when
the same text appears across multiple requests. Long-running chats
naturally have this property — turn N+1's prompt includes turns
1..N's history, so the early texts repeat dozens of times over the
conversation. One-shot scans don't.

**Multi-turn chat** (assistant, copilot, customer support):
- Caches are the difference between full-cost detection on every
  prior turn and free hits. See
  [benchmark](benchmark/cache-bench/report.md) — 16-18× speedup at
  L=20 conversations.
- Recommended: `cache_mode=both` (both layers active). Pipeline
  cache absorbs the common case; per-detector cache is the backup
  tier when overrides fragment.

**Single-turn / batch** (one-shot redaction, log scrubbing, ad-hoc
API calls without history):
- Cache hit rate stays near zero — every request has fresh content.
- Caches add storage churn and lookup overhead with no win.
- Recommended: `<DETECTOR>_CACHE_MAX_SIZE=0` (default) and
  `PIPELINE_CACHE_BACKEND=none` (default). The default config is
  already the right one for this shape.

**Mixed**: lean toward enabling caches. The single-turn requests
pay a tiny lookup cost (~µs memory, ~5ms Redis) per text but the
multi-turn ones save full detector dispatches.

See [operations → Detector result caching](operations.md#detector-result-caching)
and [operations → Pipeline-level result cache](operations.md#pipeline-level-result-cache-with-source-tracked-prewarm)
for the full mechanics.

---

## Detector mix selection

The single highest-impact deployment decision. Detectors compose
(see [docs/detectors/index.md](detectors/index.md) for the per-
detector breakdown):

| Detector | What it catches | Cost per call | Failure mode |
|---|---|---|---|
| `regex` | Shape-anchored: emails, IPs, IBANs, credit cards, SSN-shaped, hashes | µs | Always succeeds (a bad regex is a programmer error, not an outage) |
| `denylist` | Operator-supplied literal strings (project codenames, internal hostnames) | µs | Always succeeds |
| `privacy_filter` | NER over a fine-tuned `openai/privacy-filter` model | tens-to-hundreds of ms (HTTP) | Service-availability concern; opt fail-closed/open per deployment |
| `gliner_pii` | Zero-shot NER (NVIDIA `gliner-pii`); operator picks the label vocabulary | hundreds of ms (HTTP, CPU); seconds without GPU | Same — service-availability concern |
| `llm` | Generic LLM with a JSON-mode prompt; catches contextual / inferred entities | seconds (HTTP); paid per token | Most flexible, most expensive, only paid layer |

Decisions to make:

**Do you need NER at all?** Regex+denylist alone catches everything
shape-anchored — emails, IPs, credit cards, IBANs. If your PII
surface is mostly structured (logs, machine-emitted records, API
payloads) regex+denylist is sufficient and 1000× faster than any
NER layer. **Recommended for redaction pipelines that don't deal
with prose.**

**Do you need contextual / unstructured PII (names, addresses,
arbitrary persona references)?** That's where NER earns its keep.
Pick one of:
- `gliner_pii` — zero-shot, you tune the label vocabulary per
  deployment. Adds a service to deploy. Free to run (CPU or GPU).
- `privacy_filter` — fine-tuned for PII out of the box, narrower
  label space. Adds a service to deploy. Free to run.
- `llm` — most flexible (can handle "the customer's address" via
  context) but most expensive (token cost per request) and slowest
  (seconds per call). Pair with caching aggressively.

**Cost sensitivity.** LLM detection is the only paid layer. If
you're cost-constrained:
1. Stack regex+denylist+(gliner OR privacy-filter) and skip LLM.
2. If you keep LLM, set `LLM_CACHE_MAX_SIZE` high (10k+) and
   `PIPELINE_CACHE_BACKEND=memory` so prior turns don't keep
   re-paying token cost.
3. The cache benchmark shows aggressive caching turns a 200s LLM
   detection bill at L=20 chat into a 12s bill plus the LLM cost
   for one new turn — see [benchmark](benchmark/cache-bench/report.md).

**Recommended starting points:**
- Conservative + free: `DETECTOR_MODE=regex,denylist,privacy_filter`
- Conservative + free + flexible labels:
  `DETECTOR_MODE=regex,denylist,gliner_pii`
- High-recall (paid): `DETECTOR_MODE=regex,denylist,gliner_pii,llm`
- Minimum: `DETECTOR_MODE=regex` (the default — useful only when
  PII is purely shape-anchored)

Order matters — first-listed detector wins type-resolution conflicts
in the dedup pass. Put the deterministic detectors first (regex,
denylist) so their shape-based classifications beat the
NER/LLM layers' interpretive ones.

---

## Surrogate format: Faker vs opaque

The surrogate generator replaces detected PII with one of two forms:

**Faker (`USE_FAKER=true`, default).** Realistic substitutes —
`Robert Jones`, `192.0.2.5`, `bob@corp.example`. The upstream LLM
treats them as plausible content; the model can refer back to
"Robert" coherently in its response. Good for human-facing chat.

Trade-offs:
- LLM might hallucinate plausible "facts" about Robert as if he
  were a real customer. Mostly harmless — your downstream consumer
  sees the originals on deanonymize, so model hallucinations stay
  inside the surrogate space.
- Surrogates look identical to real PII. A downstream tool seeing
  the surrogate-laden form (e.g. via `DEANONYMIZE_SUBSTITUTE=false`,
  or via post-call logs) can't tell what's real vs synthetic
  without checking the vault.
- Locale-sensitive (`FAKER_LOCALE=pt_BR` for Portuguese-Brazil
  names, etc.). Per-request override via `faker_locale`.

**Opaque (`USE_FAKER=false`).** Mechanical placeholders —
`[PERSON_DEADBEEF]`, `[EMAIL_ADDRESS_AABBCCDD]`. The pattern is
type-tagged and unmistakably non-content.

Trade-offs:
- Some LLMs handle opaque tokens worse than realistic ones —
  responses can be stilted ("I can email [EMAIL_ADDRESS_AABBCCDD]")
  or the model may reference the surrogate format itself
  ("the email at the placeholder"). Test with your actual model.
- Trivially distinguishable from real PII anywhere downstream —
  log indexers, audit trails, training-data scrubbers can match
  on the `[<TYPE>_<HEX>]` pattern.
- No locale concerns; no Faker dependency at runtime.

**Recommended:**
- **Human-facing chat / assistant** → `USE_FAKER=true` (default).
  Realistic surrogates keep the model's responses coherent.
- **Redaction-only pipelines** (logs, training data, telemetry)
  → `USE_FAKER=false`. Mechanical pattern is easier to grep, and
  the LLM-coherence concern doesn't apply because there's no
  conversational continuity.
- **Compliance audits** (where you need to prove PII was
  anonymized) → `USE_FAKER=false`. Auditors can verify by pattern
  match.

See [surrogates → Disabling Faker](surrogates.md#disabling-faker)
for the full discussion.

---

## Per-request override frequency

The biggest non-obvious deployment dimension. Per-request overrides
let callers narrow detector behaviour for one request — different
LLM model, different gliner labels, different denylist, etc. Each
override changes the cache key, so frequent overrides fragment the
caches.

Decide which of these your traffic looks like:

**No overrides** (single-tenant, stable config). Every request runs
with the same kwargs. Caches are pure win.
- `cache_mode=pipeline` (memory or redis) is sufficient — one
  layer, one lookup per text.
- Per-detector cache off — adds no value when nothing changes.

**Rare overrides** (>90% of requests use defaults). Caches mostly
hit; the occasional override request runs cold. Same recommendation
as no-overrides.

**Heavy overrides** (multi-tenant SaaS, A/B testing, per-customer
prompt registry). Frequent override changes mean:
- Pipeline cache fragments — each kwargs combination gets its own
  slot. Hit rate drops.
- Per-detector caches absorb partial overlap: if only LLM kwargs
  change between calls, gliner / privacy-filter kwargs still match,
  and those per-detector slots still hit.
- **Recommended: `cache_mode=both`.** The structural backup tier
  is what makes this case workable. The pipeline cache catches the
  fully-stable requests; the per-detector caches catch the
  partial-mismatch requests.

**Always-different overrides** (per-call thresholds, every request
is bespoke). Caches mostly miss either way. Disable both layers
and pay the detection cost outright.

What "an override" actually changes (cache-key dimensions):

| Override | Affects | Detectors that re-key |
|---|---|---|
| `llm_model` | LLM cache key | LLM, pipeline |
| `llm_prompt` | LLM cache key | LLM, pipeline |
| `regex_patterns` | Regex's per-call kwargs | regex (no cache), pipeline |
| `regex_overlap_strategy` | Regex's per-call kwargs | regex (no cache), pipeline |
| `denylist` | Denylist's per-call kwargs | denylist (no cache), pipeline |
| `gliner_labels` | Gliner cache key | gliner_pii, pipeline |
| `gliner_threshold` | Gliner cache key | gliner_pii, pipeline |
| `use_faker` | Surrogate cache key (NOT detection) | nothing detection-side; surrogate cache only |
| `faker_locale` | Surrogate cache key (NOT detection) | nothing detection-side; surrogate cache only |
| `detector_mode` | Active detector list | All — different `detector_mode` is a different pipeline-cache slot |

**The two surrogate-affecting overrides** (`use_faker`, `faker_locale`)
don't fragment the detection caches — they only change which
surrogate gets generated for a given match. The surrogate cache
already buckets on `(text, type, use_faker, locale)` so each combo
has its own consistency.

See [docs/per-request-overrides.md](per-request-overrides.md) for
the full override surface.

---

## Round-trip vs redaction-only

The default flow is round-trip: anonymize the request, deanonymize
the response, the calling client sees originals restored. That's
what chat workloads want.

**`DEANONYMIZE_SUBSTITUTE=false`** flips it: the deanonymize step
runs (vault.pop happens, prewarm fires, caches stay warm) but the
substring substitution is skipped — the calling client receives
surrogate-laden output.

Use it for:
- **Log / telemetry scrubbing.** PII goes in via your guardrail,
  redacted output goes to the indexer. Restoring originals would
  defeat the redaction.
- **Training-data sanitization.** Feed the model surrogate-laden
  text; downstream training shouldn't memorise originals. Output
  should also stay surrogate-laden so synthetic-data pipelines
  don't reverse the redaction.
- **Audit / compliance pipelines.** Surrogates persist as the
  canonical record; the vault is the only place originals exist
  during the request lifetime.

What it does NOT change:
- Vault still works — needed to drive the prewarm.
- Caches still help — the surrogate-laden response IS what
  subsequent requests see, and the cache key matches.
- Surrogate consistency across turns is preserved.
- **Detection still does NOT redact what the model emits in its
  response.** This limitation predates the flag — see
  [limitations](limitations.md) → Round-trip restoration.

See [operations → Surrogates persist in responses](operations.md#surrogates-persist-in-responses-deanonymize_substitutefalse)
for the full discussion.

---

## Failure posture: fail-closed vs fail-open

Each remote detector (LLM, privacy_filter, gliner_pii) has its own
`*_FAIL_CLOSED` flag controlling what happens when the detector
service is unreachable, times out, or returns malformed data:

- **`FAIL_CLOSED=true` (default)** — typed availability error
  propagates → `Pipeline.anonymize` raises → `main.py` returns
  `BLOCKED` with the detector's `blocked_reason`. Upstream LLM
  call does NOT proceed.
- **`FAIL_CLOSED=false`** — error is caught + logged at WARNING,
  detector returns `[]` (no matches), other detectors still run.
  Upstream LLM call proceeds with whatever coverage remains.

The decision is per-detector and depends on your tolerance for two
different failure modes:

**fail_closed=true is right when:**
- You're in a regulated industry (HIPAA, GDPR, financial services)
  where unredacted PII reaching the upstream model is a compliance
  incident.
- The detector you're flagging is your *primary* PII catcher (e.g.
  if regex is your only line of defence and regex is 100%
  reliable in-process anyway, you don't need fail_closed on it).
- The upstream LLM is third-party (OpenAI, Anthropic) — you can't
  trust them with un-redacted data on detector outage.

**fail_closed=false is right when:**
- You have layered defence (regex+denylist+NER) and any one layer
  going down still gives you partial coverage.
- The upstream LLM is in your own trust boundary (self-hosted,
  same VPC) — you tolerate occasional unredacted PII reaching it
  in exchange for availability.
- You're running a redaction pipeline where dropping requests
  costs more than letting some PII leak (e.g. high-volume log
  ingestion).

Common mixes:
- **Strict regulated**: every detector `*_FAIL_CLOSED=true`
- **Layered defence with high availability**: regex (no flag —
  always succeeds), `LLM_FAIL_CLOSED=false` (LLM is paid and
  remote, accept best-effort), `PRIVACY_FILTER_FAIL_CLOSED=true`
  (PF is the primary PII catcher).
- **Best-effort scrubbing**: every flag `false`.

Each detector's flag is independent — see
[detectors/llm.md](detectors/llm.md), [detectors/privacy-filter.md](detectors/privacy-filter.md),
[detectors/gliner-pii.md](detectors/gliner-pii.md) for per-detector
config.

---

## Replica count: single-replica vs multi-replica

Single-replica deployments are the default. Memory-backed everywhere,
zero external dependencies (other than the optional NER services).

Multi-replica deployments (load-balanced behind nginx/traefik/k8s
service) introduce a hard requirement and a couple of soft
recommendations:

**HARD: vault MUST be Redis** (`VAULT_BACKEND=redis`) **if you ever
deanonymize.** The Generic Guardrail flow puts the vault entry on
the pre-call (one replica) and reads it on the post-call (possibly
a different replica). Without shared state the post-call sees
nothing → the response goes back to the client with surrogates
intact. Same end-user effect as `DEANONYMIZE_SUBSTITUTE=false`,
except silently and inconsistently (only on the unlucky requests).

If you're running `DEANONYMIZE_SUBSTITUTE=false` AND multi-replica
AND the vault is purely for prewarm, you have two options:
1. Memory vault per replica. Each replica's prewarm only seeds its
   own caches. Multi-replica cache hits fragment per-replica.
2. Redis vault. Each replica's prewarm sees all replicas' vault
   entries. Pairs naturally with Redis-backed caches.

**SOFT: caches benefit from Redis** when you want cross-replica
hits or restart-persistent state. The Redis backends for
per-detector and pipeline caches share `CACHE_REDIS_URL` and
`CACHE_SALT` — set those once for both layers. The
[benchmark](benchmark/cache-bench/report.md) shows Redis adds
~5ms RTT per lookup, which:
- Recovers value with slow detectors (gliner/PF/LLM): 7-8× speedup
  vs ~16× for memory-equivalent.
- Inverts value with fast detectors (regex-only): the lookup
  exceeds the detection cost, so Redis is *slower* than no cache.

Recommended config patterns by replica count:

| Replicas | Vault | Detector caches | Pipeline cache |
|---|---|---|---|
| 1 | memory (default) | memory | memory |
| 2-N replicas, sticky load balancing | memory (works because pre/post stay on same replica) | memory | memory |
| 2-N replicas, no stickiness | **redis** (required) | redis (recommended) or memory (per-replica only) | redis (recommended) or memory |
| Multi-cluster / restart-persistent | redis | redis | redis |

Set `CACHE_SALT` to a fixed value on multi-replica deployments —
otherwise each replica generates its own random salt at boot and
the Redis cache keys don't collide across replicas (entries are
written and read but never shared). Same applies across restarts.

See [vault → Backends](vault.md#backends) and
[operations → Detector result caching](operations.md#detector-result-caching)
for the full mechanics.

---

## Recipes

Concrete starting points for common deployment archetypes. Each row
gives the env vars to set; everything not listed stays at default.

### A. Single-replica chat assistant (the default)

The shape this project optimises for. Multi-turn conversations, PII
restored on response, single backing host.

```bash
# Detector mix — pick what your PII surface needs
DETECTOR_MODE=regex,llm

# Caching — multi-turn benefits a lot
LLM_CACHE_MAX_SIZE=2000
PIPELINE_CACHE_BACKEND=memory
PIPELINE_CACHE_MAX_SIZE=2000

# Failure posture — block on LLM outage (regulated)
LLM_FAIL_CLOSED=true

# Everything else: defaults (USE_FAKER=true,
# DEANONYMIZE_SUBSTITUTE=true, VAULT_BACKEND=memory)
```

### B. Multi-replica chat assistant

Same as A, but Redis-backed for cross-replica state. Fixed salt
ensures cache hits across replicas.

```bash
DETECTOR_MODE=regex,llm
LLM_FAIL_CLOSED=true

# Vault must be Redis
VAULT_BACKEND=redis
VAULT_REDIS_URL=redis://redis-cache:6379/0

# Caches share the same Redis instance (different DB optional)
LLM_CACHE_BACKEND=redis
LLM_CACHE_TTL_S=600
PIPELINE_CACHE_BACKEND=redis
PIPELINE_CACHE_TTL_S=600
CACHE_REDIS_URL=redis://redis-cache:6379/1
CACHE_SALT=<a-stable-random-value-rotated-on-key-rotation-only>
```

### C. Redaction-only telemetry pipeline

One-shot scans of log entries / events. No conversation context,
no restoration. Surrogates persist in output.

```bash
# Cheap, deterministic detectors — telemetry is mostly
# shape-anchored PII
DETECTOR_MODE=regex,denylist,privacy_filter

# Mechanical surrogates are easier to grep / audit
USE_FAKER=false

# Surrogates persist in output — the redaction is the deliverable
DEANONYMIZE_SUBSTITUTE=false

# Single-turn workload — caches don't help
# (defaults: caches off)

# Best-effort scrubbing — never block a log ingest
PRIVACY_FILTER_FAIL_CLOSED=false
```

### D. Multi-tenant SaaS with per-tenant prompt registry

Heavy override usage — each tenant has their own LLM prompt /
denylist / regex pattern set. Cache fragmentation under varied
overrides; per-detector tier kicks in.

```bash
DETECTOR_MODE=regex,llm,gliner_pii

# Both cache layers — pipeline absorbs same-tenant traffic;
# per-detector absorbs cross-tenant requests where some kwargs
# match
LLM_CACHE_MAX_SIZE=5000
GLINER_PII_CACHE_MAX_SIZE=5000
PIPELINE_CACHE_BACKEND=memory
PIPELINE_CACHE_MAX_SIZE=5000

# Per-tenant registries — operators ship the registry, callers
# pick named entries via per-request overrides
LLM_SYSTEM_PROMPT_REGISTRY="tenant_a=bundled:llm_default.md,tenant_b=/etc/anon/tenant_b.md"
REGEX_PATTERNS_REGISTRY="tenant_a=bundled:regex_pentest.yaml,..."
DENYLIST_REGISTRY="..."
```

### E. Training-data sanitization

Batch processing — files of conversation transcripts → PII-stripped
copies. No restoration, opaque tokens for grep-ability.

```bash
DETECTOR_MODE=regex,gliner_pii

# Mechanical surrogates so training data is recognisably
# anonymized after the fact (audit trail)
USE_FAKER=false

# Surrogates persist — that's the deliverable
DEANONYMIZE_SUBSTITUTE=false

# Each conversation is processed once — caching across
# conversations is rare, but within one transcript it helps
GLINER_PII_CACHE_MAX_SIZE=2000
PIPELINE_CACHE_BACKEND=memory
PIPELINE_CACHE_MAX_SIZE=2000

# Fail-open — dropping a record is worse than letting some PII
# slip through (which can be caught downstream)
GLINER_PII_FAIL_CLOSED=false
```

### F. Cost-optimised chat with cheap detection

LLM detection is too expensive for the workload — rely on
regex+gliner+PF and skip the LLM layer entirely.

```bash
DETECTOR_MODE=regex,denylist,privacy_filter,gliner_pii

# All NER layers cached — repeated turns hit cache instead of
# round-tripping
PRIVACY_FILTER_CACHE_MAX_SIZE=2000
GLINER_PII_CACHE_MAX_SIZE=2000
PIPELINE_CACHE_BACKEND=memory
PIPELINE_CACHE_MAX_SIZE=2000

# Layered defence — each detector independent
PRIVACY_FILTER_FAIL_CLOSED=true
GLINER_PII_FAIL_CLOSED=false
```

### G. Strict-compliance regulated chat

Maximum redaction surface, fail-closed everywhere, audit-friendly
opaque tokens, multi-replica Redis.

```bash
DETECTOR_MODE=regex,denylist,privacy_filter,gliner_pii,llm

# Fixed salt for audit reproducibility
SURROGATE_SALT=<a-stable-deployment-secret>
USE_FAKER=false

# Block-on-outage everywhere
LLM_FAIL_CLOSED=true
PRIVACY_FILTER_FAIL_CLOSED=true
GLINER_PII_FAIL_CLOSED=true

# Multi-replica Redis (fixed salt across replicas)
VAULT_BACKEND=redis
VAULT_REDIS_URL=redis://redis-cluster:6379/0
LLM_CACHE_BACKEND=redis
PRIVACY_FILTER_CACHE_BACKEND=redis
GLINER_PII_CACHE_BACKEND=redis
PIPELINE_CACHE_BACKEND=redis
CACHE_REDIS_URL=redis://redis-cluster:6379/1
CACHE_SALT=<a-stable-deployment-secret-distinct-from-surrogate-salt>

# Round-trip preserved (default) — chat needs originals back

# Long TTL for vault since regulated workflows can have slow
# round-trips through approval queues
VAULT_TTL_S=3600
```

---

## Putting it together

If your shape isn't on the recipe list, walk the dimensions in
[Quick reference](#quick-reference) and pick a value for each. The
dimensions are independent — there's no incompatible combination
the validators will reject — so any column-product gives a working
deployment. Some combinations buy you nothing (e.g. caches with
single-turn workloads), some are over-built for their use case
(e.g. Redis everywhere on a single-replica deployment).

When the trade-offs aren't obvious, run the
[cache benchmark](cache-bench.md) against your actual detector mix
and conversation shape — the numbers will pick the right setting
faster than any hand-wave.
