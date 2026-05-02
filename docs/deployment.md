# Deployment

## Quick start (scripts)

Two helper scripts live under `scripts/`. They wrap `podman build` /
`podman run` with sensible defaults, the `--format=docker` quirk for
HEALTHCHECK preservation, the volume + shared-network plumbing, and
auto-start of both the fake-llm test backend and the privacy-filter
inference service when the operator opts into them.

```bash
# Build every CPU flavour: three guardrail images (slim, pf, pf-baked)
# plus the auxiliary services (privacy-filter-service in two
# variants, fake-llm). Pass -t to build a single one (e.g. -t slim,
# or -t pf-service-cu130 for the CUDA build of the pf service).
scripts/build-image.sh -t all

# Interactive launcher — single-screen menuconfig-style UI, every
# setting visible at once, drill in to edit, hit Launch.
scripts/launcher.sh --ui

# Flag-driven launcher with bundled presets:
scripts/launcher.sh --preset uuid-debug      # slim + regex,llm + fake-llm + LOG_LEVEL=debug
scripts/launcher.sh --preset pentest         # pf + regex,privacy_filter,llm + pentest patterns/prompt + fake-llm
scripts/launcher.sh --preset regex-only      # slim + regex only — no LLM creds needed

# Exercise the curl recipes against a running guardrail
# (or pass --preset to spin one up + tear it down):
scripts/test-examples.sh --preset uuid-debug
```

When the chosen `DETECTOR_MODE` includes `llm` and the LLM backend is
set to `fake-llm`, the launcher boots the fake-llm container in the
background on a shared `anonymizer-net`, waits for `/health`, and
points the guardrail at `http://fake-llm:4000/v1`. fake-llm matches
incoming chat-completion requests against a YAML rules file
(`services/fake_llm/rules.example.yaml` by default; `--rules PATH`
overrides), which is what makes the test recipes deterministic. See
[`services/fake_llm/README.md`](../services/fake_llm/README.md) for
the rules schema.

The same auto-start pattern applies to the privacy-filter inference
service: when `DETECTOR_MODE` includes `privacy_filter` and the
backend is set to `service` (`--privacy-filter-backend service`, or
the matching menu choice), the launcher starts a
`privacy-filter-service` container on the same shared network, mounts
the shared `anonymizer-hf-cache` volume so the model isn't
re-downloaded if you've used the `pf` guardrail flavour before, waits
for `/health` to flip to `ok` (which can take minutes on a cold
runtime-download image), and points the guardrail at
`http://privacy-filter-service:8001`. Use this on top of the slim
guardrail image to avoid baking torch + the model into the guardrail.
See [`services/privacy_filter/README.md`](../services/privacy_filter/README.md)
for the API contract.

The same pattern works for `gliner_pii` —
`--gliner-pii-backend service` auto-starts `gliner-pii-service` on
the shared network. See
[`services/gliner_pii/README.md`](../services/gliner_pii/README.md)
and [GLiNER-PII detector](detectors/gliner-pii.md).

## Container images

The repo builds three artifacts: the **guardrail** itself, the
standalone **privacy-filter-service** and **gliner-pii-service**
(paired with the slim guardrail when you don't want torch in the API
container), and the **fake-llm** test backend.
`scripts/build-image.sh` knows about every flavour listed below. The
*published as* column shows the GHCR tag CI ships; flavours marked
**local only** are buildable but not on the registry — see
[Why some flavours aren't published](#why-some-flavours-arent-published)
for the rationale.

### Guardrail (`anonymizer-guardrail`)

Three flavours, controlled by two build-args, sharing one
`Containerfile`. The privacy-filter flavours are CPU-only by design —
when you need GPU acceleration, deploy the standalone
`privacy-filter-service` on a GPU node and point the slim guardrail at
it via `PRIVACY_FILTER_URL` (see
[Privacy-filter detector](detectors/privacy-filter.md)).

| flavour | size | model | published as | when to pick it |
|---|---|---|---|---|
| `slim` | ~200 MB | n/a (in-process) | `:vX.Y.Z` | covers regex/llm/denylist; pair with `PRIVACY_FILTER_URL` for remote `privacy_filter` coverage |
| `pf` | ~1.3 GB | downloads on first container start | `:vX.Y.Z-pf` | in-process `privacy_filter` for most deployments — pair with a named volume |
| `pf-baked` | ~6.9 GB | shipped inside image | local only | in-process `privacy_filter`, air-gapped or strict cold-start latency |

### Privacy-filter-service (`privacy-filter-service`)

Standalone HTTP wrapper around the same `transformers`
token-classification pipeline the in-process detector uses. Pair with
the slim guardrail to keep the heavy ML deps in their own container —
typically deployed on a GPU node and shared by multiple guardrail
replicas. Each variant gets an explicit tag suffix (no implicit
default) so a typoed pull fails loud rather than handing back the
wrong torch build.

| flavour | torch wheels | model | published as | runtime needs |
|---|---|---|---|---|
| `pf-service` | CPU-only | downloads on first start | `:vX.Y.Z-cpu` | none beyond the container |
| `pf-service-cu130` | CUDA 13.0 | downloads on first start | `:vX.Y.Z-cu130` | nvidia GPU + nvidia-container-toolkit |
| `pf-service-baked` | CPU-only | shipped inside image | local only | none beyond the container |
| `pf-service-baked-cu130` | CUDA 13.0 | shipped inside image | local only | nvidia GPU + nvidia-container-toolkit |

### GLiNER-PII service (`gliner-pii-service`)

Standalone HTTP wrapper around the `nvidia/gliner-pii` zero-shot model.
Same shape as the privacy-filter service (CPU + CUDA matrix, baked /
runtime-download split). See [GLiNER-PII detector](detectors/gliner-pii.md)
for the model license note.

| flavour | torch wheels | model | published as | runtime needs |
|---|---|---|---|---|
| `gliner-service` | CPU-only | downloads on first start | `:vX.Y.Z-cpu` | none beyond the container |
| `gliner-service-cu130` | CUDA 13.0 | downloads on first start | `:vX.Y.Z-cu130` | nvidia GPU + nvidia-container-toolkit |
| `gliner-service-baked` | CPU-only | shipped inside image | local only | none beyond the container |
| `gliner-service-baked-cu130` | CUDA 13.0 | shipped inside image | local only | nvidia GPU + nvidia-container-toolkit |

### Fake-llm (`fake-llm`)

OpenAI-compatible Chat Completions server backed by a YAML rules file
(see [`services/fake_llm/README.md`](../services/fake_llm/README.md)).
Used by the test recipes and `scripts/launcher.sh --llm-backend service`
to exercise the LLM-detector path deterministically without an
actual LLM. **Local-build only** — `scripts/build-image.sh -t fake-llm`.
Not for production.

### Why some flavours aren't published

CI publishes the variants most operators pull. The local-only set is:

- **Baked-model variants** (`pf-baked`, `pf-service-baked`,
  `pf-service-baked-cu130`, `gliner-service-baked`,
  `gliner-service-baked-cu130`) — multi-GB images that store
  indefinitely on GHCR for what's usually a build-once artifact. A
  runtime-download image with a persistent HF cache volume gets you to
  the same place after one cold start, so the registry mass isn't
  worth it. Build with `scripts/build-image.sh -t pf-baked` (etc.)
  when you actually need them.
- **`fake-llm`** — a test/dev tool with no place in a production
  registry. Operators who need it for their own CI typically build
  once into their own infra registry.

(CPU sizes assume the default CPU-only PyTorch build. CUDA wheels add
~2 GB on top — that's roughly how much `nvidia-cuda-runtime`,
`nvidia-cudnn`, `nvidia-cublas`, etc. weigh on Linux x86.)

## Building manually

`scripts/build-image.sh` is the recommended path; the equivalent raw
commands are:

```bash
# 1) Slim — no ML deps.
podman build --format=docker -t anonymizer-guardrail:latest -f Containerfile .

# 2) Privacy-filter, runtime download — small image, downloads ~6 GB on
#    first container start. Mount a NAMED VOLUME so subsequent starts
#    skip the download (see below).
podman build --format=docker -t anonymizer-guardrail:privacy-filter \
    --build-arg WITH_PRIVACY_FILTER=true -f Containerfile .

# 3) Privacy-filter, model baked into image — self-contained, no runtime
#    network, at the cost of a much larger image (size in the table above).
podman build --format=docker -t anonymizer-guardrail:privacy-filter-baked \
    --build-arg WITH_PRIVACY_FILTER=true \
    --build-arg BAKE_PRIVACY_FILTER_MODEL=true \
    -f Containerfile .
```

`--format=docker` is needed because podman defaults to OCI image
format, which doesn't include a HEALTHCHECK field — without the flag,
the `HEALTHCHECK` directive in the Containerfile is silently dropped
and `podman healthcheck run` won't work. `docker build` always emits
Docker format, so the flag is podman-specific (and `build-image.sh`
adds it conditionally).

## Running manually

Slim or baked images run without any volume:

```bash
podman run --rm -p 8000:8000 \
  -e LLM_API_BASE=http://litellm:4000/v1 \
  -e LLM_API_KEY=sk-litellm-master \
  -e LLM_MODEL=anonymize \
  --name anonymizer anonymizer-guardrail:latest
```

The runtime-download image **needs** a persistent volume at
`/app/.cache/huggingface` — without one, every `podman run` re-downloads
the ~6 GB. Use a named volume:

```bash
podman volume create anonymizer-hf-cache

podman run --rm -p 8000:8000 \
  -e DETECTOR_MODE=regex,privacy_filter,llm \
  -e LLM_API_BASE=http://litellm:4000/v1 \
  -e LLM_API_KEY=sk-litellm-master \
  -v anonymizer-hf-cache:/app/.cache/huggingface \
  --name anonymizer anonymizer-guardrail:privacy-filter
```

First `podman run` of the privacy-filter image takes a few minutes (the
model downloads into the volume, blocking app startup). The container's
healthcheck has a 300-second start-period to accommodate this — slower
networks may need a longer override via `--health-start-period`.
Subsequent runs reuse the volume and start in seconds.

Volume options compared:

- **Named volume** (`-v anonymizer-hf-cache:/app/.cache/huggingface`):
  recommended. Auto-managed by Podman/Docker; survives `podman rm`.
- **Bind mount** (`-v /host/path:/app/.cache/huggingface`): same effect
  but stores the cache wherever you point it on the host. Useful if you
  want the files visible outside Podman's volume store.
- **Kubernetes**: mount a `PersistentVolumeClaim` at the same path —
  first pod pays the download; later pods reuse the PVC. Use
  `ReadWriteMany` for shared cache across replicas.

## State and replicas — read this before scaling out

The guardrail keeps two stores in process memory:

- **The vault** — `litellm_call_id → surrogate→original mapping`,
  written by the pre-call hook and consumed by the matching post-call
  hook. Without the matching mapping, the post-call hook ships
  surrogates back to the user instead of restoring originals.
- **The surrogate cache** — cross-request consistency for
  multi-turn conversations (same input → same surrogate).

Three production implications fall out of "in-memory":

1. **Pre/post-call hooks must land on the same replica.** Round-trip
   anonymization breaks the moment a load balancer routes the
   pre-call to replica A and the post-call to replica B — B has no
   mapping to restore. Required posture for >1 replica: sticky
   routing keyed on `litellm_call_id`, OR run a single replica.
   See [limitations → Single replica](limitations.md#single-replica).
2. **Restarts lose in-flight round-trips.** A pre-call written
   before a restart can't be deanonymized by a post-call after the
   restart. The
   [`VAULT_TTL_S`](configuration.md#vault) backstop bounds the
   *other* direction (vault grows when responses don't arrive); the
   restart-mid-roundtrip case has no fix beyond accepting it.
3. **Surrogate consistency is per-process.** Replica A and replica
   B will issue different surrogates for the same original unless
   you pin a stable [`SURROGATE_SALT`](surrogates.md#surrogate-salt-privacy-hardening).
   Even then, salt only stabilises the surrogate value, not the LRU
   cache contents — the *consistency window* (how far back two
   identical inputs still produce the same surrogate) is bounded by
   `SURROGATE_CACHE_MAX_SIZE` per replica.

For multi-replica deployments where sticky routing isn't an option,
swap the `Vault` for a shared backend (Redis is the natural fit).
The interface is two methods (`put` / `pop`); see
[`TASKS.md` → Multi-replica support](../TASKS.md) for the design
sketch when this becomes urgent.

`/health` exposes `vault_size` and `surrogate_cache_size` — see
[operations](operations.md) for how to monitor them.

## Smoke test

```bash
curl -fsS http://localhost:8000/health
```

For end-to-end curl recipes covering the round-trip, every detector
category, multi-text batches, and a kitchen-sink payload, see
[`examples.md`](examples.md). To run those recipes as automated
assertions: `scripts/test-examples.sh` (with `--preset NAME` to
self-host a test guardrail).
