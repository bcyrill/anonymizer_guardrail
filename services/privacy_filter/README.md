# privacy-filter-service

HTTP wrapper around the
[openai/privacy-filter](https://huggingface.co/openai/privacy-filter)
token-classification model. Pair with the guardrail's
`RemotePrivacyFilterDetector` (planned) so the heavy ML deps live in
their own container instead of inflating the guardrail image.

## API

### `POST /detect`

```json
// request
{ "text": "Email alice@example.com about the meeting." }

// response
{
  "matches": [
    {
      "text": "alice@example.com",
      "entity_type": "EMAIL_ADDRESS",
      "start": 6,
      "end": 23,
      "score": 0.998
    }
  ]
}
```

`entity_type` uses the same canonical names as the guardrail
(`PERSON`, `EMAIL_ADDRESS`, `PHONE`, `URL`, `ADDRESS`, `DATE_OF_BIRTH`,
`IDENTIFIER`, `CREDENTIAL`). Unknown labels fall back to `OTHER`.

The post-processing — span extraction and same-type merge across
whitespace gaps — is identical to the in-process detector, so output
is byte-equivalent for the same input.

### `GET /health`

```json
{ "status": "ok", "model": "openai/privacy-filter" }
```

Status is `loading` until the model finishes loading at startup. The
container `HEALTHCHECK` treats anything other than `ok` as unhealthy.

## Build

Two flavours, both built from this directory:

```bash
# Runtime download — small image (~1 GB), model downloads on first start.
podman build -t privacy-filter-service:latest \
    -f Containerfile .

# Baked — model in the image (~6.9 GB), works air-gapped.
podman build -t privacy-filter-service:baked \
    --build-arg BAKE_MODEL=true \
    -f Containerfile .
```

Or use `scripts/build-image.sh` from the repo root: types `pf-service`
and `pf-service-baked`.

## Run

```bash
# With a persistent cache so the model survives container recreates:
podman volume create privacy-filter-hf-cache
podman run --rm -p 8001:8001 \
    -v privacy-filter-hf-cache:/app/.cache/huggingface \
    privacy-filter-service:latest
```

Configuration:

| Variable     | Default                  | Notes                                  |
|--------------|--------------------------|----------------------------------------|
| `HOST`       | `0.0.0.0`                |                                        |
| `PORT`       | `8001`                   |                                        |
| `MODEL_NAME` | `openai/privacy-filter`  | HuggingFace model id loaded at startup |
| `LOG_LEVEL`  | `info`                   |                                        |

GPU build: pass `--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121`
to the `podman build` (default is the CPU wheel index, since most
deployments don't have a GPU and the CUDA `torch` wheel pulls in ~2 GB
of CUDA runtime libraries).
