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
[`privacy-filter-service`](../../services/privacy_filter/) sidecar
over HTTP. The slim guardrail image carries no ML deps — torch,
[`opf`](https://github.com/openai/privacy-filter) (OpenAI's own
inference wrapper for this model), and the model weights all live
in the sidecar.

Mirrors gliner-pii's architectural split: thin model wrapper in the
service container, label canonicalisation + merge / split / per-label
gap caps in the guardrail-side
[`RemotePrivacyFilterDetector`](../../src/anonymizer_guardrail/detector/remote_privacy_filter.py).

## Configuration (guardrail-side)

| Variable | Default | Notes |
|---|---|---|
| `PRIVACY_FILTER_URL` | *(empty)* | HTTP URL of the privacy-filter-service. The launcher's `--privacy-filter-backend service` auto-start sets this to `http://privacy-filter-service:8001`; otherwise the operator sets it to wherever they're running the sidecar. Required when `DETECTOR_MODE` includes `privacy_filter` — the detector errors at construction if unset. |
| `PRIVACY_FILTER_TIMEOUT_S` | `30` | Per-call HTTP timeout (seconds). |
| `PRIVACY_FILTER_FAIL_CLOSED` | `true` | Block requests when the privacy_filter detector errors. Independent flag — operators can fail closed on one detector and open on another. |
| `PRIVACY_FILTER_MAX_CONCURRENCY` | `10` | Semaphore on in-flight `privacy_filter` calls. Independent of `LLM_MAX_CONCURRENCY`. Surfaced as `pf_in_flight` / `pf_max_concurrency` on `/health`. |

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
image is installed locally, preferring the baked variant when
present (avoids the cold-start model fetch).

| Flavour | Notes |
|---|---|
| `pf-service` | CPU torch, runtime model download on first request (~3 GB pulled into `/app/.opf` — mount a named volume there for persistence). |
| `pf-service-baked` | CPU torch, model weights baked in. Self-contained for air-gapped runs. |
| `pf-service-cu130` | CUDA-13.0 torch wheels, runtime download. `PRIVACY_FILTER_DEVICE` defaults to `cuda` in this image. Host needs nvidia-container-toolkit. |
| `pf-service-baked-cu130` | CUDA-13.0 + baked weights. |

When the auto-started service is a `cu*` flavour, the launcher
emits the engine-specific GPU flag (`--device nvidia.com/gpu=all`
on podman, `--gpus all` on docker) automatically. Operators
running CUDA images outside the launcher need the same flag on
their `podman run` invocation. The host requires
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
installed.

## Selecting the backend

The launcher exposes the choice as `--privacy-filter-backend`:

| Value | What happens |
|---|---|
| `service` *(default)* | Auto-start a `privacy-filter-service` container on the shared network and point the guardrail at it. Same UX as `--llm-backend service`. |
| `external` | Use the URL given by `--privacy-filter-url` / `PRIVACY_FILTER_URL`. Nothing is auto-started. Use this for production deployments where the service is managed separately (Kubernetes, docker-compose, etc.). |

The auto-start path mounts an `anonymizer-hf-cache` volume at
`/app/.opf` inside the container, so the model download survives
container recreation. opf writes its checkpoint there directly
(it ignores `HF_HOME` for its own checkpoint location).

## Decoder

The service loads the model via OpenAI's
[`opf`](https://github.com/openai/privacy-filter) package — specifically
`OPF.redact()` with `decode_mode="viterbi"`. The Viterbi path is the
decoder OpenAI actually trained the model with; tighter span
boundaries (no trailing punctuation absorbed), zero `\n\n`
over-merges, and high recall on emails / API keys / plaintext
passwords compared to the older transformers integration. See
[`services/privacy_filter/scripts/PROBE.md`](../../services/privacy_filter/scripts/PROBE.md)
for the empirical span tables.

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
