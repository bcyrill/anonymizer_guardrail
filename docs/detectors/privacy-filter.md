# Privacy-filter detector

PII NER backed by
[openai/privacy-filter](https://huggingface.co/openai/privacy-filter)
(Apache 2.0). Encoder-only token classifier, ~1.5 B params (50 M
active via MoE). Detects 8 PII categories: people, emails, phones,
URLs, addresses, dates, account numbers, secrets. Coverage is a
strict subset of what the LLM prompt picks up (no orgs, hostnames,
IP/MAC, etc.) — a *complement* to, not a replacement for, the LLM
detector.

The detector is HTTP-only: the guardrail talks to a standalone
sidecar over HTTP. The guardrail image carries no ML deps —
torch, [`opf`](https://github.com/openai/privacy-filter) (OpenAI's
own inference wrapper for this model), and the model weights all
live in the sidecar.

Mirrors gliner-pii's architectural split: thin model wrapper in the
service container, label canonicalisation + merge / split / per-label
gap caps in the guardrail-side
[`RemotePrivacyFilterDetector`](../../src/anonymizer_guardrail/detector/remote_privacy_filter.py).

## Variants

Two ghcr-published service images implement the same wire format —
the guardrail-side detector talks to either with no client changes.
Pick by deployment topology and CPU/GPU availability.

| Variant | Image | Wire format | When to pick |
|---|---|---|---|
| **opf-only** *(default)* | [`privacy-filter-service`](../../services/privacy_filter/) | `{spans: [{label, start, end, text}]}` | Default and reference implementation. Conservative pick. Ships a baked-model image flavour for air-gapped deployments. |
| **HF + opf-decoder** *(experimental)* | [`privacy-filter-hf-service`](../../services/privacy_filter_hf/) | identical | Pairs HF Transformers' forward pass with opf's Viterbi decoder. **~7x faster on CPU** than the opf-only variant on bundled fixtures (see [COMPARE.md](../../services/privacy_filter_hf/COMPARE.md)) at full span-quality parity, because opf reimplements the Transformer from scratch and isn't CPU-tuned while HF's loader gets the official library's CPU optimisations. Recommended for new CPU deployments once the speed-quality trade has been validated against a representative corpus. No baked-model variant (build hits disk-space pressure during commit). |

The launcher's `--privacy-filter-variant opf|hf` selects which
sidecar to auto-start (default `opf`); the menu has the same prompt.
For external (operator-managed) services, point
`PRIVACY_FILTER_URL` at whichever sidecar you're running and the
choice is implicit. See [Selecting the backend](#selecting-the-backend)
below.

The rest of this document focuses on the opf-only variant since it's
the current default; everything that's not flavour-specific applies
equally to both variants. Variant-specific notes are flagged inline.

## Configuration (guardrail-side)

| Variable | Default | Notes |
|---|---|---|
| `PRIVACY_FILTER_URL` | *(empty)* | HTTP URL of the privacy-filter-service. The launcher's `--privacy-filter-backend service` auto-start sets this to `http://privacy-filter-service:8001`; otherwise the operator sets it to wherever they're running the sidecar. Required when `DETECTOR_MODE` includes `privacy_filter` — the detector errors at construction if unset. |
| `PRIVACY_FILTER_TIMEOUT_S` | `30` | Per-call HTTP timeout (seconds). |
| `PRIVACY_FILTER_FAIL_CLOSED` | `true` | Block requests when the privacy_filter detector errors. Independent flag — operators can fail closed on one detector and open on another. |
| `PRIVACY_FILTER_MAX_CONCURRENCY` | `10` | Semaphore on in-flight `privacy_filter` calls. Independent of `LLM_MAX_CONCURRENCY`. Surfaced as `pf_in_flight` / `pf_max_concurrency` on `/health`. |
| `PRIVACY_FILTER_CACHE_MAX_SIZE` | `0` | LRU cap on the privacy-filter detector's result cache (memory backend only). `0` disables caching (default). When enabled, repeat calls with the same input text skip the remote PF round-trip. See [operations → Detector result caching](../operations.md#detector-result-caching) for the trade-offs. |
| `PRIVACY_FILTER_CACHE_BACKEND` | `memory` | `memory` (process-local LRU) or `redis` (shared store). The redis backend requires the central `CACHE_REDIS_URL` and the `cache-redis` install extra. |
| `PRIVACY_FILTER_CACHE_TTL_S` | `600` | Per-key TTL for redis-backed entries, reset on every cache write. Memory backend ignores this knob. |
| `PRIVACY_FILTER_INPUT_MODE` | `per_text` | How the pipeline dispatches `req.texts` to this detector. `per_text` (default) calls the detector once per text; `merged` concatenates all texts with a sentinel separator and makes one call per request. See [operations → Merged-input mode](../operations.md#merged-input-mode) for the trade-offs. Mutually exclusive with `PRIVACY_FILTER_CACHE_MAX_SIZE`: setting both logs a warning at boot and the cache is bypassed. |

## Configuration (service-side)

These knobs apply to the standalone privacy-filter-service container.
The launcher forwards them from the operator's shell environment to
the sidecar (see `service_env_passthroughs` in
[`tools/launcher/spec_extras.py`](../../tools/launcher/spec_extras.py)),
so a single shell-side `export` configures both ends.

| Variable | Default | Notes |
|---|---|---|
| `PRIVACY_FILTER_DEVICE` | `cpu` (cpu image) / `cuda` (cu130 image) | Inference device. opf's own constructor default is `"cuda"`; the Containerfile bakes a different default per image flavour via the `TARGET_DEVICE` build-arg, so the cpu image doesn't silently try to load CUDA on a GPU-less host. Operators can override at run time (e.g. for GPU experiments on the cpu image with a manual `-e PRIVACY_FILTER_DEVICE=cuda` plus the GPU device flag). |
| `PRIVACY_FILTER_CALIBRATION` | *(unset)* | Optional path **inside the container** to a Viterbi operating-points JSON. Empty / missing → opf's stock decoder. Tune-and-mount is the workflow: produce a JSON locally (see `services/privacy_filter/scripts/spike_opf.py` for the shape), bind-mount it into the service, and set this var. |
| `MODEL_NAME` | `openai/privacy-filter` | The default sentinel maps to opf's auto-download path (`~/.opf/privacy_filter`); anything else is treated as a filesystem path to a local checkpoint directory. |
| `HF_HUB_OFFLINE` | *(unset)* / `1` *(baked images)* | Set to `1` to stop `huggingface_hub` (used by opf during the model download) from pinging the Hub on every container start. The baked image flavours set this automatically as deployment-intent documentation; runtime-download flavours leave it unset on first run so the download succeeds. |

## Image flavours

Build via `scripts/image_builder.sh -f <flavour>`. The launcher's
`--privacy-filter-backend service` path uses whichever `pf-service-*`
or `pf-hf-service-*` image is installed locally (per `--privacy-filter-variant`),
preferring a baked variant when present (avoids the cold-start
model fetch).

### opf-only variant

| Flavour | Notes |
|---|---|
| `pf-service` | CPU torch, runtime model download on first request (~3 GB pulled into `/app/.opf` — mount a named volume there for persistence). |
| `pf-service-baked` | CPU torch, model weights baked in. Self-contained for air-gapped runs. |
| `pf-service-cu130` | CUDA-13.0 torch wheels, runtime download. `PRIVACY_FILTER_DEVICE` defaults to `cuda` in this image. Host needs nvidia-container-toolkit. |
| `pf-service-baked-cu130` | CUDA-13.0 + baked weights. |

### HF + opf-decoder variant

| Flavour | Notes |
|---|---|
| `pf-hf-service` | CPU torch + transformers, runtime model download from HuggingFace Hub on first request (~3 GB pulled into `/app/.cache/huggingface` — mount a named volume there for persistence). |
| `pf-hf-service-cu130` | CUDA-13.0 torch wheels + transformers, runtime download. `PRIVACY_FILTER_DEVICE` defaults to `cuda` in this image. Host needs nvidia-container-toolkit. |

No `*-baked` variants for the HF flavour: the build hits disk-space
pressure during commit (transformers + opf + ~3 GB of weights, with
overlayfs duplicating cached files via snapshot symlinks). For
air-gapped HF deployment, populate the cache via a bind mount or a
sidecar that pre-fetches the model.

### GPU access

When the auto-started service is a `cu*` flavour (either variant),
the launcher emits the engine-specific GPU flag (`--device nvidia.com/gpu=all`
on podman, `--gpus all` on docker) automatically. Operators running
CUDA images outside the launcher need the same flag on their
`podman run` invocation. The host requires
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
installed.

## Selecting the backend

The launcher exposes two related choices:

* `--privacy-filter-backend service|external` — whether to
  auto-start the sidecar or point at one the operator manages.
* `--privacy-filter-variant opf|hf` — which sidecar implementation
  to start. Default `opf`. Only meaningful when
  `--privacy-filter-backend=service`.

### `--privacy-filter-backend`

| Value | What happens |
|---|---|
| `service` *(default)* | Auto-start a sidecar container on the shared network and point the guardrail at it. The variant chosen via `--privacy-filter-variant` determines which image starts. Same UX as `--llm-backend service`. |
| `external` | Use the URL given by `--privacy-filter-url` / `PRIVACY_FILTER_URL`. Nothing is auto-started. Use this for production deployments where the service is managed separately (Kubernetes, docker-compose, etc.). The variant is implicit in whichever sidecar the URL points at. |

### `--privacy-filter-variant`

| Value | Image | Cache mount path | Volume name |
|---|---|---|---|
| `opf` *(default)* | `privacy-filter-service` (port 8001) | `/app/.opf` | `anonymizer-hf-cache` |
| `hf` *(experimental)* | `privacy-filter-hf-service` (port 8003) | `/app/.cache/huggingface` | `privacy-filter-hf-cache` |

Distinct volume names because the cache layouts are not
interchangeable: opf writes to a flat `~/.opf/privacy_filter`
snapshot dir; HF Transformers uses the standard Hub
`hub/blobs/...` + `hub/snapshots/...` tree. Mounting one variant's
volume at the other's path produces "model not found" errors at
startup.

## Decoder

Both variants use opf's
[`ViterbiCRFDecoder`](https://github.com/openai/privacy-filter/blob/main/opf/_core/decoding.py),
the constrained-BIOES Viterbi OpenAI actually trained the model with.
This is what gives the privacy-filter its tight span boundaries (no
trailing punctuation absorbed), zero `\n\n` over-merges, and high
recall on emails / API keys / plaintext passwords compared to the
greedy `transformers.pipeline(aggregation_strategy="first")`
integration we ran pre-migration. See
[`services/privacy_filter/scripts/PROBE.md`](../../services/privacy_filter/scripts/PROBE.md)
for the empirical span tables.

The variants differ only in *forward-pass implementation*:

* The opf-only variant uses opf's from-scratch Transformer
  (`opf/_model/model.py`) for both forward and decode.
* The HF variant uses
  `transformers.AutoModelForTokenClassification.from_pretrained("openai/privacy-filter")`
  for the forward pass and feeds the resulting logits into the same
  `ViterbiCRFDecoder`. transformers ≥5.7.0 has the
  `OpenAIPrivacyFilterForTokenClassification` architecture
  registered, so HF's loader handles the model natively.

Decode quality is therefore identical across variants by
construction. The HF variant adds a small post-decode whitespace
trim to match opf's `trim_whitespace=True` default — without it,
HF's BPE tokenizer's leading-space handling produces spans that
are off by one character at word-initial positions. See
[COMPARE.md → Implementation note](../../services/privacy_filter_hf/COMPARE.md#implementation-note-whitespace-trimming)
for the full story.

The post-processing pipeline on the guardrail side (per-label merge
gap caps, paragraph-break split pass) is kept as defense in depth —
it doesn't fire on the fixtures under opf's stock decoder, but
production traffic is broader than fixtures and the runtime cost is
negligible.

Tuning: `PRIVACY_FILTER_CALIBRATION` points the service at an opf
operating-points JSON to bias the BIOES Viterbi transitions. Six
biases (`transition_bias_*`); see
[`services/privacy_filter/scripts/spike_opf.py`](../../services/privacy_filter/scripts/spike_opf.py)
for the JSON shape and three pre-built profiles (`default`,
`anti-merge`, `privacy-parser`) you can compare empirically against
your own corpus.

## Failure handling

`PRIVACY_FILTER_FAIL_CLOSED` (default `true`) is independent from
`LLM_FAIL_CLOSED` and `GLINER_PII_FAIL_CLOSED` — operators can fail
closed on one detector and open on another. When the privacy-filter
detector raises `PrivacyFilterUnavailableError` (service unreachable,
timeout, non-200, unparseable 200 OK body or wrong-shape payload),
the guardrail returns `BLOCKED`. With fail-open, the error is logged
and the request proceeds with coverage from the remaining detectors.
A 200 OK with garbage in it counts as unavailable — soft-failing
those would let unredacted text through under fail-closed.
Per-entry malformed entries inside an otherwise-valid
`{"spans": [...]}` payload drop silently inside the post-processing
layer's hallucination guard (the substring must actually be in the
input).
