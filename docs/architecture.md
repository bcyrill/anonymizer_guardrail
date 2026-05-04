# Architecture

A bird's-eye view of how the anonymizer-guardrail is structured and
how a request flows through it. Each component covered here has a
dedicated deep-dive doc — this one stitches them together.

## TL;DR

The guardrail sits as a **LiteLLM Generic Guardrail API** between a
client and an upstream LLM. On the request side it detects PII in
the prompt and substitutes opaque or Faker-style surrogates; on the
response side it restores the originals using a per-call_id vault
mapping. Detection is pluggable (regex / denylist / privacy-filter /
gliner-pii / LLM); surrogate generation is deterministic per a salt
so multiple replicas agree without sharing state. Two cache layers
(per-detector and pipeline-level) make multi-turn chat affordable
to redact in real time.

## Position in the request flow

The guardrail is HTTP-only and sees only what LiteLLM forwards through
the Generic Guardrail hooks. It does not sit inline on the upstream
LLM call — LiteLLM does. Two HTTP round-trips per chat turn:

```
                                                        ┌────────────────┐
                                                        │   Upstream LLM │
                                                        │ (OpenAI, Claude│
                                                        │  self-hosted…) │
                                                        └────────┬───────┘
                                                                 ▲ │
                                                                 │ │
                                                                 │ ▼
┌──────────┐         ┌─────────────────┐          ┌───────────────────┐
│  Client  │ ──(1)──▶│                 │ ──(2)───▶│                   │
│ (browser,│         │  LiteLLM        │          │  anonymizer-      │
│  app,    │ ◀─(7)── │  Proxy          │          │  guardrail        │
│  SDK)    │         │                 │ ◀─(3)─── │  (pre_call)       │
│          │         │                 │          │                   │
│          │         │  ──(4)─────────▶│          │  ┌─────────────┐  │
│          │         │  ◀─(5)──────────│          │  │   Pipeline  │  │
│          │         │                 │          │  │  + Vault    │  │
│          │         │                 │ ──(6a)──▶│  │  + Caches   │  │
│          │         │                 │          │  │             │  │
│          │         │                 │ ◀─(6b)── │  │ (post_call) │  │
│          │         │                 │          │  └─────────────┘  │
└──────────┘         └─────────────────┘          └───────────────────┘
```

Numbered events:

1. Client sends a chat-completion request with PII-laden text.
2. LiteLLM extracts `texts` from the messages and calls the
   guardrail's pre-call hook.
3. Guardrail runs detection + substitution; returns
   `GUARDRAIL_INTERVENED` with surrogate-laden `texts`. Stores the
   reverse mapping under `litellm_call_id` in the vault.
4. LiteLLM substitutes the surrogate-laden texts into the messages
   array and forwards the request upstream.
5. Upstream LLM responds with surrogate-laden output (it sees and
   echoes surrogates).
6. LiteLLM calls the guardrail's post-call hook with the response
   `texts`; (6a) the guardrail looks up the vault entry, (6b)
   substitutes surrogates back to originals, returns
   `GUARDRAIL_INTERVENED`.
7. LiteLLM forwards the deanonymized response to the client.

In `DEANONYMIZE_SUBSTITUTE=false` mode (redaction-only deployments),
step 6b skips the substitution — the client at step 7 receives
surrogate-laden output. See
[deployment-shapes](deployment-shapes.md#round-trip-vs-redaction-only).

## Internal layout

What's inside the guardrail box from the diagram above:

```
┌───────────────────────────────────────────────────────────────────────┐
│                   anonymizer-guardrail process                         │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │                     HTTP layer (FastAPI)                          │ │
│  │   POST /beta/litellm_basic_guardrail_api    GET /health           │ │
│  │   • Body-size middleware (DoS protection)                         │ │
│  │   • Header-forwarding for use_forwarded_key (LLM API key          │ │
│  │     attribution)                                                  │ │
│  │   • Override parsing (additional_provider_specific_params →       │ │
│  │     Overrides)                                                    │ │
│  └────────────────────────────┬─────────────────────────────────────┘ │
│                               │                                        │
│                       (anonymize / deanonymize)                        │
│                               ▼                                        │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │                          Pipeline                                  │ │
│  │  • Detector orchestration (per-text fan-out, merged dispatch)    │ │
│  │  • Dedup + source-detector accumulator                           │ │
│  │  • Pipeline-cache wrap (optional)                                │ │
│  │  • Surrogate generation                                          │ │
│  │  • Vault put / pop                                               │ │
│  │  • Deanonymize-side prewarm hook                                 │ │
│  └─────┬────────────┬───────────────┬──────────────┬─────────────┬─┘ │
│        │            │               │              │             │   │
│        ▼            ▼               ▼              ▼             ▼   │
│  ┌──────────┐ ┌───────────┐ ┌──────────────┐ ┌────────┐ ┌────────────┐│
│  │Detectors │ │ Surrogate │ │   Vault      │ │ Per-   │ │  Pipeline  ││
│  │          │ │ generator │ │              │ │detector│ │   cache    ││
│  │ regex    │ │ + cache   │ │ memory or    │ │ caches │ │            ││
│  │ denylist │ │           │ │ redis        │ │        │ │ memory /   ││
│  │ llm      │ │ Faker or  │ │              │ │ memory │ │ redis /    ││
│  │ pf       │ │ opaque    │ │ structured   │ │ /redis │ │ none       ││
│  │ gliner   │ │ tokens    │ │ VaultEntry   │ │ /none  │ │            ││
│  └──────────┘ └───────────┘ └──────────────┘ └────────┘ └────────────┘│
│        │                                                               │
│   (HTTP for pf, gliner, llm — regex/denylist run in-process)          │
│        │                                                               │
└────────┼───────────────────────────────────────────────────────────────┘
         ▼
   ┌─────────────────┐ ┌─────────────────┐ ┌───────────────────┐
   │ privacy-filter  │ │ gliner-pii      │ │ Upstream LLM      │
   │ -hf-service     │ │ -service        │ │ (LiteLLM, OpenAI) │
   │ (sidecar)       │ │ (sidecar)       │ │                   │
   └─────────────────┘ └─────────────────┘ └───────────────────┘
```

Each component is opt-in. A regex-only deployment runs the FastAPI
layer + Pipeline + Surrogate generator + Vault — nothing else. Each
NER detector adds its own service to deploy.

## Anonymize flow (pre-call hook)

What happens when LiteLLM calls
`POST /beta/litellm_basic_guardrail_api` with `input_type=request`:

```
 (1) Body parsed to GuardrailRequest, overrides parsed.
      │
      ▼
 (2) Pipeline.anonymize(texts, call_id, overrides):
      │
      ├─► (3) _build_call_kwargs(active_detectors, overrides, api_key)
      │        Returns frozen tuple used as part of the
      │        pipeline-cache key + stored on the vault entry.
      │
      ├─► (4) _run_detection(texts, …):
      │        ┌───────────────────────────────────────────────┐
      │        │ TaskGroup:                                     │
      │        │  • per-text fan-out: _detect_one(text_i)       │
      │        │     ┌──────────────────────────────────┐       │
      │        │     │ pipeline_cache.get_or_compute    │       │
      │        │     │  key=(text_i, mode, kwargs)      │       │
      │        │     │  ┌────────────────────────────┐  │       │
      │        │     │  │ HIT:  return cached matches │  │       │
      │        │     │  └────────────────────────────┘  │       │
      │        │     │  ┌────────────────────────────┐  │       │
      │        │     │  │ MISS: _compute()           │  │       │
      │        │     │  │  • TaskGroup over           │  │       │
      │        │     │  │    detectors                │  │       │
      │        │     │  │    (each gated by its       │  │       │
      │        │     │  │    semaphore + its own     │  │       │
      │        │     │  │    cache wrap)              │  │       │
      │        │     │  │  • _dedup_with_sources     │  │       │
      │        │     │  └────────────────────────────┘  │       │
      │        │     └──────────────────────────────────┘       │
      │        │  • merged-mode detectors run once on the       │
      │        │    sentinel-joined blob                        │
      │        └───────────────────────────────────────────────┘
      │
      ├─► (5) Build per-call original→surrogate map by walking
      │        per-text + merged matches in DETECTOR_MODE order.
      │        SurrogateGenerator.for_match() handles Faker /
      │        opaque generation, with collision-retry and the
      │        process-wide LRU cache for cross-request
      │        consistency.
      │
      ├─► (6) Build VaultEntry:
      │        {surrogates: dict[str, VaultSurrogate(original, type,
      │                                              source_detectors)],
      │         detector_mode: tuple[str, ...],
      │         kwargs: FrozenCallKwargs}
      │        and vault.put(call_id, entry).
      │
      ├─► (7) Apply substring replacer to texts → modified texts.
      │
      └─► (8) Return (modified_texts, mapping).
            FastAPI response: GUARDRAIL_INTERVENED + the modified
            texts. LiteLLM forwards them upstream.
```

Step 4's pipeline-cache wrap is the load-bearing optimisation for
multi-turn chat. On a hit, no detectors run at all; the cached
deduped match list comes back directly. See
[design-decisions → Bidirectional pipeline-level result cache](design-decisions.md#bidirectional-pipeline-level-result-cache-with-source-tracked-prewarm).

## Deanonymize flow (post-call hook)

Mirror of the anonymize side:

```
 (1) Body parsed (input_type=response). litellm_call_id picked
     up from the request body.
      │
      ▼
 (2) Pipeline.deanonymize(texts, call_id):
      │
      ├─► (3) entry = vault.pop(call_id)
      │        • MemoryVault: dict pop + TTL check
      │        • RedisVault: GETDEL (atomic read+delete)
      │        Returns VaultEntry() if missing/expired/no call_id.
      │        Empty entry → return texts unchanged (NONE).
      │
      ├─► (4) If config.deanonymize_substitute:
      │          replacer = _build_replacer(surrogate→original map)
      │          restored = [replacer(t) for t in texts]
      │       else:
      │          restored = list(texts)         # surrogates persist
      │
      ├─► (5) _prewarm_caches(restored, entry) — for each restored
      │        text in parallel:
      │        ┌───────────────────────────────────────────────┐
      │        │ Filter synth list to entries whose original    │
      │        │ appears in this text (substring presence).     │
      │        │                                                │
      │        │ Pipeline-cache PUT:                            │
      │        │   key=(text, detector_mode, kwargs from entry) │
      │        │   value=filtered (Match, source_detectors) list│
      │        │                                                │
      │        │ For each cache-using detector:                 │
      │        │   filtered_for_det = [m for m, s in synth_text │
      │        │                       if det in s]             │
      │        │   per-detector cache PUT under key built from  │
      │        │   detector's own kwargs from entry             │
      │        └───────────────────────────────────────────────┘
      │        First-writer-wins on populated slots; failures are
      │        logged + swallowed (prewarm is best-effort).
      │
      └─► (6) Return restored texts.
            FastAPI: GUARDRAIL_INTERVENED + the restored texts.
            LiteLLM forwards to the client.
```

Step 5's prewarm is what closes the assistant-message-on-turn-N+1
cache miss — without it, the response text would be re-detected
fresh on the next anonymize. See
[operations → Pipeline-level result cache](operations.md#pipeline-level-result-cache-with-source-tracked-prewarm).

## State stores

Four internal stores plus per-detector HTTP sidecars. Each has a
distinct contract:

```
            ┌────────────────────────────────────────┐
            │  Surrogate cache (in-process LRU)       │
            │  process-wide, never shared              │
            │  key:   (text, type, use_faker, locale)  │
            │  value: surrogate string                 │
            │  consistency: deterministic given salt   │
            │  → no Redis variant, salt does the job   │
            └────────────────────────────────────────┘

            ┌────────────────────────────────────────┐
            │  Vault                                   │
            │  per-call_id, TTL'd                      │
            │  key:   call_id                          │
            │  value: VaultEntry                       │
            │           • surrogates dict              │
            │           • detector_mode                │
            │           • frozen kwargs                │
            │  backends: memory / redis                │
            │  → REQUIRED Redis for multi-replica when │
            │    deanonymizing                         │
            └────────────────────────────────────────┘

            ┌────────────────────────────────────────┐
            │  Per-detector result cache               │
            │  one cache per cache-using detector      │
            │  key:   (text, that detector's kwargs)   │
            │  value: list[Match]                      │
            │  backends: memory / redis                │
            │  → optional, structural backup tier      │
            └────────────────────────────────────────┘

            ┌────────────────────────────────────────┐
            │  Pipeline-level result cache             │
            │  one cache, wraps all detector dispatch  │
            │  key:   (text, detector_mode, frozen     │
            │          per-call kwargs)                │
            │  value: list[(Match, source_detectors)]  │
            │  backends: none / memory / redis         │
            │  → optional, fastest path on a hit       │
            └────────────────────────────────────────┘
```

Cache layering:

```
       _detect_one(text)                  ┌── cache HIT ──▶ matches
            │                              │  (no detectors run)
            ▼                              │
   ┌────────────────────────────┐          │
   │  pipeline_cache.            │──────────┘
   │  get_or_compute(key, …)     │
   └────────┬───────────────────┘
            │  cache MISS
            ▼
   ┌────────────────────────────┐
   │  TaskGroup over detectors   │
   │   ├─ regex (no cache)       │
   │   ├─ denylist (no cache)    │
   │   ├─ llm.detect ────────────┼─ uses LLM per-detector cache
   │   ├─ privacy_filter.detect ─┼─ uses PF per-detector cache
   │   └─ gliner.detect ─────────┼─ uses gliner per-detector cache
   └────────┬───────────────────┘
            ▼
   ┌────────────────────────────┐
   │  _dedup_with_sources         │
   └────────┬───────────────────┘
            ▼
       cached + returned
```

The pipeline-cache key contains every detector's kwargs (frozen
into a tuple) — so any kwarg change anywhere fragments the key.
The per-detector caches only fragment by *that* detector's kwargs.
That's why the per-detector tier acts as a structural backup when
overrides change for some detectors but not others. Walked through
in detail in
[design-decisions](design-decisions.md#bidirectional-pipeline-level-result-cache-with-source-tracked-prewarm).

## Detector pluggability

Each detector is a self-contained module under
`src/anonymizer_guardrail/detector/` with five things:

1. A `Detector` (or `BaseRemoteDetector` subclass) implementing
   the detection contract — `detect(text, **kwargs) → list[Match]`.
2. A `CONFIG` (Pydantic BaseSettings) with the env vars the
   detector reads.
3. A `DetectorSpec` constant describing how the pipeline should
   wire it: factory, optional concurrency cap (`has_semaphore`),
   optional cache (`has_cache`), optional availability error
   class (`unavailable_error`), and the `prepare_call_kwargs` /
   `cache_kwargs` callables.
4. A `LAUNCHER_SPEC` (only relevant for the dev-only launcher tool)
   describing whether the detector has a backing service to
   auto-start.
5. An entry in `src/anonymizer_guardrail/detector/__init__.py`'s
   `REGISTERED_SPECS` tuple.

The `Pipeline` iterates `REGISTERED_SPECS` to build everything
dynamic — concurrency caps, in-flight counters, exception handlers,
`/health` stats, BLOCKED handlers in `main.py`, the bash launcher's
CLI flags. Adding a new detector means writing one module + adding
its spec to the registry — no edits scattered across the codebase.

See [development.md](development.md#adding-a-new-detector) for the
walkthrough.

## Source code map

```
src/anonymizer_guardrail/
├── api.py                  GuardrailRequest / GuardrailResponse / Overrides
├── main.py                 FastAPI app, lifespan hooks, BLOCKED responses
├── config.py               Cross-cutting Config (server, surrogate, vault,
│                           pipeline cache, deanonymize behaviour)
├── pipeline.py             Pipeline class — orchestration, detector
│                           dispatch, anonymize / deanonymize flows,
│                           prewarm hook
├── surrogate.py            SurrogateGenerator (Faker + opaque generators,
│                           process-wide LRU, salt resolution, locale chain)
│
├── vault.py                VaultBackend Protocol + factory + VaultEntry /
│                           VaultSurrogate types + freeze_kwargs helper
├── vault_memory.py         MemoryVault (default; OrderedDict + TTL + LRU)
├── vault_redis.py          RedisVault (opt-in; GETDEL atomic pop, JSON
│                           wire format)
│
├── pipeline_cache.py       PipelineResultCache Protocol + factory +
│                           DisabledPipelineCache sentinel
├── pipeline_cache_memory.py InMemoryPipelineCache (LRU)
├── pipeline_cache_redis.py RedisPipelineCache (NX-on-write,
│                           keyed BLAKE2b digests)
│
└── detector/
    ├── base.py             Match dataclass, Detector / CachingDetector
    │                       Protocols, ENTITY_TYPES
    ├── spec.py             DetectorSpec dataclass — descriptor used to
    │                       build pipeline wiring dynamically
    ├── launcher.py         LauncherSpec / ServiceSpec (dev tool only)
    │
    ├── cache.py            DetectorResultCache Protocol + factory +
    │                       resolve_cache_salt
    ├── cache_memory.py     InMemoryDetectionCache
    ├── cache_redis.py      RedisDetectionCache (NX-on-write,
    │                       keyed BLAKE2b digests)
    │
    ├── regex.py            RegexDetector (in-process, YAML-driven
    │                       patterns, no cache, no semaphore)
    ├── denylist.py         DenylistDetector (in-process, Aho-Corasick
    │                       over operator-supplied literals)
    ├── remote_base.py      BaseRemoteDetector — shared httpx +
    │                       cache-wrap template
    ├── llm.py              LLMDetector (OpenAI-compatible JSON-mode)
    ├── remote_privacy_filter.py   RemotePrivacyFilterDetector + post-
    │                              processing for opf DetectedSpans
    └── remote_gliner_pii.py       RemoteGlinerPIIDetector + label
                                   canonicalisation
```

Sidecar service implementations live under `services/`:

```
services/
├── fake_llm/               LiteLLM-compatible stub for tests / dev
├── privacy_filter/         opf-based privacy-filter HTTP service
└── gliner_pii/             nvidia/gliner-pii HTTP service
```

Dev-only tooling (not in the published wheel):

```
tools/
├── launcher/               podman/docker orchestration for sidecars
├── image_builder/          builds + tags container images
├── detector_bench/         scores a detector mix against a corpus
└── cache_bench/            benchmarks cache effectiveness across the matrix
```

## Where to read next

| Want to understand… | Read |
|---|---|
| The HTTP request/response contract | [api.py docstring](../src/anonymizer_guardrail/api.py) + [examples.md](examples.md) |
| Detector deep-dives (each layer) | [detectors/index.md](detectors/index.md) and the per-detector pages |
| Surrogate generation (Faker, opaque, locale) | [surrogates.md](surrogates.md) |
| Vault — round-trip mapping, TTL, backends | [vault.md](vault.md) |
| Caching mechanics (per-detector + pipeline) | [operations.md → Detector result caching](operations.md#detector-result-caching) and [→ Pipeline-level result cache](operations.md#pipeline-level-result-cache-with-source-tracked-prewarm) |
| Why the architecture looks like this (paths considered + declined) | [design-decisions.md](design-decisions.md) |
| What it doesn't do | [limitations.md](limitations.md) |
| Picking config for a deployment | [deployment-shapes.md](deployment-shapes.md) |
| Per-call override surface | [per-request-overrides.md](per-request-overrides.md) |
| Adding a new detector | [development.md → Adding a new detector](development.md#adding-a-new-detector) |
| LiteLLM wiring (the "how do I plug this in" question) | [litellm-integration.md](litellm-integration.md) |
| Detection-quality benchmark | [detector-bench.md](detector-bench.md) |
| Cache effectiveness benchmark | [benchmark/cache-bench/report.md](benchmark/cache-bench/report.md) |
| Detector-service latency benchmark (CPU + GPU) | [service-bench.md](service-bench.md) |
