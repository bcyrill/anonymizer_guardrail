# gliner-pii-service

HTTP wrapper around NVIDIA's
[nvidia/gliner-pii](https://huggingface.co/nvidia/gliner-PII) model — a
fine-tune of GLiNER large-v2.1 for PII / PHI detection. Paired with
the guardrail's `RemoteGlinerPIIDetector` (set `GLINER_PII_URL` on
the guardrail container, or `--gliner-pii-backend service` /
`external` from the launcher) — there is no in-process variant. See
[docs/detectors/gliner-pii.md](../../docs/detectors/gliner-pii.md)
for when to pick this detector vs `privacy_filter`, label / threshold
guidance, and the NVIDIA Open Model License caveat.

The interesting differentiator vs the existing
`privacy-filter-service` is **zero-shot labels**: the entity-type list
is an input to the model rather than baked into the architecture. A
caller can ask for `["ssn", "medical_record_number", "iban"]` on one
request and `["person", "organization"]` on the next, no retraining.

## API

### `POST /detect`

```json
// request — labels and threshold are optional
{
  "text": "Email alice@example.com about her SSN 123-45-6789.",
  "labels": ["email", "ssn"],
  "threshold": 0.5
}

// response
{
  "matches": [
    {
      "text": "alice@example.com",
      "entity_type": "email",
      "start": 6,
      "end": 23,
      "score": 0.99
    },
    {
      "text": "123-45-6789",
      "entity_type": "ssn",
      "start": 39,
      "end": 50,
      "score": 0.98
    }
  ]
}
```

Omit `labels` to fall back to the server's `DEFAULT_LABELS` (env-var
configured). Omit `threshold` to use `DEFAULT_THRESHOLD`.

`entity_type` is whatever label the request asked the model to find —
the service deliberately does **not** normalize them to the guardrail's
canonical `ENTITY_TYPES`. That mapping (e.g. `ssn` → `NATIONAL_ID`,
`email` → `EMAIL_ADDRESS`) belongs to the detector layer when wired,
so this service stays a thin model wrapper that's reusable outside the
guardrail context.

### `GET /health`

```json
{ "status": "ok", "model": "nvidia/gliner-pii" }
```

Status is `loading` until the model finishes loading at startup. The
container `HEALTHCHECK` treats anything other than `ok` as unhealthy.

## Build

Two flavours, both built from this directory. Both have CPU and CUDA
variants (CUDA tag matches the pytorch wheel-index convention — e.g.
`cu130` for CUDA 13.0):

```bash
# CPU, runtime download — small image, model downloads on first start.
podman build -t gliner-pii-service:cpu \
    -f Containerfile .

# CPU, baked — model shipped in the image, works air-gapped.
podman build -t gliner-pii-service:baked-cpu \
    --build-arg BAKE_MODEL=true \
    -f Containerfile .

# CUDA 13.0, runtime download — needs an nvidia GPU at runtime.
podman build -t gliner-pii-service:cu130 \
    --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 \
    -f Containerfile .

# CUDA 13.0, baked.
podman build -t gliner-pii-service:baked-cu130 \
    --build-arg BAKE_MODEL=true \
    --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 \
    -f Containerfile .
```

Or use `scripts/build-image.sh` from the repo root: types
`gliner-service`, `gliner-service-baked`, `gliner-service-cu130`,
`gliner-service-baked-cu130`.

CI publishes the runtime-download variants (`gliner-service`,
`gliner-service-cu130`) to GHCR as
`ghcr.io/<owner>/gliner-pii-service:vX.Y.Z-cpu` / `:vX.Y.Z-cu130` on
each `vX.Y.Z+gliner-service` tag — same `+gliner-service` tag
convention as the privacy-filter-service. The baked flavours stay
local-build only (multi-GB; not worth the registry mass).

## Run

```bash
# With a persistent cache so the model survives container recreates:
podman volume create gliner-hf-cache
podman run --rm -p 8002:8002 \
    -v gliner-hf-cache:/app/.cache/huggingface \
    gliner-pii-service:cpu

# Probe it:
curl -s http://localhost:8002/detect \
    -H 'Content-Type: application/json' \
    -d '{"text": "Email me at alice@example.com.", "labels": ["email"]}' \
    | python -m json.tool
```

For the CUDA variants, add `--device nvidia.com/gpu=all` (podman) or
`--gpus all` (docker) and make sure
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
is set up on the host.

## Configuration

| Variable             | Default                                 | Notes                                                                                                |
|----------------------|-----------------------------------------|------------------------------------------------------------------------------------------------------|
| `HOST`               | `0.0.0.0`                               |                                                                                                      |
| `PORT`               | `8002`                                  | Distinct from privacy-filter-service's `8001` so both can run on the same docker network             |
| `MODEL_NAME`         | `nvidia/gliner-pii`                     | HuggingFace model id loaded at startup                                                               |
| `LOG_LEVEL`          | `info`                                  |                                                                                                      |
| `DEFAULT_LABELS`     | *(broad PII set; see `main.py`)*        | Comma-separated default labels for `POST /detect` requests that don't supply their own               |
| `DEFAULT_THRESHOLD`  | `0.5`                                   | Confidence cutoff (0..1) for requests that don't override                                            |

## License note

The model is released under the
[NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/),
which permits commercial use but adds clauses (responsible-AI use,
attribution, distribution rules) that the openai/privacy-filter
Apache-2.0 license doesn't. Verify it matches your deployment's license
posture before relying on this service in production.

## Why a separate service (vs in-process)

Same rationale as the privacy-filter-service:

- Heavy ML deps (`gliner` library + torch + the model weights) stay
  out of the guardrail image — slim guardrail can still benefit by
  pointing at this URL.
- Independent scaling — typically deploy on a GPU node and share with
  multiple guardrail replicas.
- License containment — the NVIDIA Open Model License terms apply only
  to operators who deploy this image, not to everyone who pulls the
  guardrail.
