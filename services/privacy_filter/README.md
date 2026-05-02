# privacy-filter-service

HTTP wrapper around the
[openai/privacy-filter](https://huggingface.co/openai/privacy-filter)
token-classification model. Pair with the guardrail's
`RemotePrivacyFilterDetector` (set `PRIVACY_FILTER_URL` on the
guardrail container, or `--privacy-filter-backend service` /
`external` from the launcher) so the heavy ML deps live in their own
container instead of inflating the guardrail image. See
[docs/detectors/privacy-filter.md](../../docs/detectors/privacy-filter.md)
for when to pick the in-process vs the remote variant.

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
# Runtime download — model downloads on first container start.
podman build -t privacy-filter-service:latest \
    -f Containerfile .

# Baked — model shipped in the image, works air-gapped.
podman build -t privacy-filter-service:baked \
    --build-arg BAKE_MODEL=true \
    -f Containerfile .
```

Or use `scripts/image_builder.sh` from the repo root: types `pf-service`
and `pf-service-baked`. See
[docs/deployment.md → Container images](../../docs/deployment.md#container-images)
for the size/runtime trade-off across all flavours.

## Run

```bash
# With a persistent cache so the model survives container recreates.
# opf writes its checkpoint into `/app/.opf/privacy_filter` (its
# default model path), so that's where the volume mounts. HF_HOME
# isn't involved — opf uses huggingface_hub for the download stage
# only and writes the final checkpoint to its own location.
podman volume create privacy-filter-opf-cache
podman run --rm -p 8001:8001 \
    -v privacy-filter-opf-cache:/app/.opf \
    privacy-filter-service:latest
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

| Variable     | Default                  | Notes                                  |
|--------------|--------------------------|----------------------------------------|
| `HOST`       | `0.0.0.0`                |                                        |
| `PORT`       | `8001`                   |                                        |
| `MODEL_NAME` | `openai/privacy-filter`  | HuggingFace model id loaded at startup |
| `LOG_LEVEL`  | `info`                   |                                        |

GPU build: pass `--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121`
to the `podman build` (default is the CPU wheel index, since most
deployments don't have a GPU and the CUDA `torch` wheel adds the
nvidia runtime libraries on top).
