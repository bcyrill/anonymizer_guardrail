# privacy-filter-service

HTTP wrapper around the
[openai/privacy-filter](https://huggingface.co/openai/privacy-filter)
token-classification model. The guardrail's
`RemotePrivacyFilterDetector` is the only consumer — there is no
in-process variant. Set `PRIVACY_FILTER_URL` on the guardrail
container, or use `--privacy-filter-backend service` / `external`
from the launcher. See
[docs/detectors/privacy-filter.md](../../docs/detectors/privacy-filter.md)
for the configuration knobs and image flavours.

## API

### `POST /detect`

```json
// request
{ "text": "Email alice@example.com about the meeting." }

// response
{
  "spans": [
    {
      "label": "private_email",
      "start": 6,
      "end": 23,
      "text": "alice@example.com"
    }
  ]
}
```

The wire format is opf's raw `DetectedSpan` shape — `label` is the
opf label verbatim (`private_person`, `private_email`,
`private_phone_number`, `private_url`, `private_address`,
`private_date_of_birth`, `private_identifier`, `private_credential`).
Label canonicalisation, paragraph-break split, and per-label
gap-cap post-processing all run client-side in the guardrail's
[`RemotePrivacyFilterDetector`](../../src/anonymizer_guardrail/detector/remote_privacy_filter.py).
This mirrors gliner-pii-service: the service is a thin model
wrapper, the guardrail-side detector handles vocabulary mapping and
heuristics. Per-span `score` isn't included — opf's `DetectedSpan`
doesn't expose confidence, and a synthetic constant would be worse
than its absence.

### `GET /health`

```json
{ "status": "ok", "model": "openai/privacy-filter" }
```

Status is `loading` until the model finishes loading at startup
(including a one-shot warmup `redact()` call to amortise lazy-init
ahead of the first real request). The container `HEALTHCHECK`
treats anything other than `ok` as unhealthy.

## Build

Four flavours, all built from this directory:

```bash
# CPU, runtime download
podman build -t privacy-filter-service:cpu \
    -f Containerfile .

# CPU, model baked into the image (air-gapped runs)
podman build -t privacy-filter-service:baked-cpu \
    --build-arg BAKE_MODEL=true \
    -f Containerfile .

# CUDA 13.0, runtime download — TARGET_DEVICE=cuda makes the
# service default to GPU inference inside the container.
podman build -t privacy-filter-service:cu130 \
    --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 \
    --build-arg TARGET_DEVICE=cuda \
    -f Containerfile .

# CUDA 13.0 + baked model
podman build -t privacy-filter-service:baked-cu130 \
    --build-arg BAKE_MODEL=true \
    --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 \
    --build-arg TARGET_DEVICE=cuda \
    -f Containerfile .
```

Or use `scripts/image_builder.sh` from the repo root: flavour names
`pf-service`, `pf-service-baked`, `pf-service-cu130`,
`pf-service-baked-cu130`. See
[docs/deployment.md → Container images](../../docs/deployment.md#container-images)
for the size/runtime trade-off across all flavours.

## Run

```bash
# Persistent cache so the model survives container recreates.
# opf writes its checkpoint into `/app/.opf/privacy_filter` (its
# default model path), so that's where the volume mounts. HF_HOME
# isn't involved — opf uses huggingface_hub for the download stage
# only and writes the final checkpoint to its own location.
podman volume create privacy-filter-opf-cache
podman run --rm -p 8001:8001 \
    -v privacy-filter-opf-cache:/app/.opf \
    privacy-filter-service:cpu

# CUDA flavour — host needs nvidia-container-toolkit installed.
podman run --rm -p 8001:8001 \
    --device nvidia.com/gpu=all \
    -v privacy-filter-opf-cache:/app/.opf \
    privacy-filter-service:cu130
```

## Probing

`scripts/probe.py` is a stdlib-only Python script that hits a
running service with a text input and prints the matches plus a
per-entity-type count. Use it to see how the model classifies your
data (and to compare against gliner-pii on the same fixture).

```bash
python services/privacy_filter/scripts/probe.py \
    --text "Alice Smith (alice@acme.com) lives at 123 Main St."
```

See [scripts/PROBE.md](scripts/PROBE.md) for runnable examples
(file / stdin input, side-by-side comparison with gliner-pii) and
`--help` for the full flag list.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `HOST` | `0.0.0.0` | |
| `PORT` | `8001` | |
| `MODEL_NAME` | `openai/privacy-filter` | The default sentinel maps to opf's auto-download path; anything else is treated as a filesystem path to a local checkpoint directory. |
| `PRIVACY_FILTER_DEVICE` | `cpu` (cpu image) / `cuda` (cu130 image) | Inference device. The Containerfile bakes the per-image default via `TARGET_DEVICE` build-arg. Override at run time only when you know the host has the right hardware/runtime for it. |
| `PRIVACY_FILTER_CALIBRATION` | *(unset)* | Optional path *inside the container* to a Viterbi operating-points JSON. Empty / missing → opf's stock decoder. See [`scripts/spike_opf.py`](scripts/spike_opf.py) for the JSON shape and three pre-built profiles. |
| `HF_HUB_OFFLINE` | *(unset)* / `1` *(baked images)* | Set to `1` to stop `huggingface_hub` (used by opf during the model download) from pinging the Hub on every container start. The baked images set this automatically. |
| `LOG_LEVEL` | `info` | |
