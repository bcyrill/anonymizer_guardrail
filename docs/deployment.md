# Deployment

## Quick start (scripts)

Two helper scripts live under `scripts/`. They wrap `podman build` /
`podman run` with sensible defaults, the `--format=docker` quirk for
HEALTHCHECK preservation, the volume + shared-network plumbing, and
auto-start of both the fake-llm test backend and the privacy-filter
inference service when the operator opts into them.

```bash
# Build every CPU flavour: the slim guardrail image plus the
# auxiliary services (privacy-filter-service, gliner-pii-service,
# fake-llm). Pass -t to build a single one (e.g. -t slim, or
# -t pf-service-cu130 for the CUDA build of the privacy-filter
# service).
scripts/image_builder.sh --preset all

# Interactive launcher — single-screen menuconfig-style UI, every
# setting visible at once, drill in to edit, hit Launch.
scripts/launcher.sh --ui

# Flag-driven launcher with bundled presets:
scripts/launcher.sh --preset uuid-debug      # slim + regex,llm + fake-llm + LOG_LEVEL=debug
scripts/launcher.sh --preset pentest         # slim + regex,privacy_filter,llm + pf-service + fake-llm + pentest patterns/prompt
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
service: when `DETECTOR_MODE` includes `privacy_filter`, the launcher
starts a `privacy-filter-service` container on the same shared
network (the privacy-filter detector is HTTP-only, so this happens
by default — `--privacy-filter-backend external` opts out and uses
an operator-managed URL instead), mounts the shared
`anonymizer-hf-cache` volume at `/app/.opf` for the model checkpoint,
waits for `/health` to flip to `ok` (which can take minutes on a cold
runtime-download image), and points the guardrail at
`http://privacy-filter-service:8001`. See
[`services/privacy_filter/README.md`](../services/privacy_filter/README.md)
for the API contract.

When the locally-built privacy-filter-service image is a CUDA flavour
(`pf-service-cu130` or `pf-service-baked-cu130`), the launcher also
emits the engine-specific GPU flag (`--device nvidia.com/gpu=all` on
podman, `--gpus all` on docker) automatically. The host needs
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
installed.

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
`scripts/image_builder.sh` knows about every flavour listed below. The
*published as* column shows the GHCR tag CI ships; flavours marked
**local only** are buildable but not on the registry — see
[Why some flavours aren't published](#why-some-flavours-arent-published)
for the rationale.

### Guardrail (`anonymizer-guardrail`)

One slim image — no ML deps. Privacy-filter and gliner-pii ship as
standalone services; the guardrail talks to them over HTTP via
`PRIVACY_FILTER_URL` / `GLINER_PII_URL`. See
[Privacy-filter detector](detectors/privacy-filter.md) for the
service-side image flavours and their device knobs.

| flavour | size | published as | what's in it |
|---|---|---|---|
| `slim` | ~200 MB | `:vX.Y.Z` | regex / denylist / LLM detectors in-process; remote clients for privacy_filter / gliner_pii. |

### Privacy-filter-service (`privacy-filter-service`) — opf-only variant

Default privacy-filter sidecar. Standalone HTTP wrapper around
opf's full inference stack (forward + constrained-Viterbi decode)
for the openai/privacy-filter model. Pair with the slim guardrail
and auto-started by `scripts/launcher.sh` when `DETECTOR_MODE`
includes `privacy_filter`. Each variant gets an explicit tag suffix
(no implicit default) so a typoed pull fails loud rather than
handing back the wrong torch build. CUDA variants set
`PRIVACY_FILTER_DEVICE=cuda` automatically (via `TARGET_DEVICE`
build-arg) so the launcher's auto-start does the right thing without
operator-side env juggling.

| flavour | torch wheels | model | published as | runtime needs |
|---|---|---|---|---|
| `pf-service` | CPU-only | downloads on first start | `:vX.Y.Z-cpu` | none beyond the container |
| `pf-service-cu130` | CUDA 13.0 | downloads on first start | `:vX.Y.Z-cu130` | nvidia GPU + nvidia-container-toolkit |
| `pf-service-baked` | CPU-only | shipped inside image | local only | none beyond the container |
| `pf-service-baked-cu130` | CUDA 13.0 | shipped inside image | local only | nvidia GPU + nvidia-container-toolkit |

### Privacy-filter-hf-service (`privacy-filter-hf-service`) — HF + opf-decoder variant *(experimental)*

Sibling sidecar that pairs HuggingFace Transformers' forward pass
with opf's Viterbi decoder. Same wire format as the opf-only
service (the guardrail's `RemotePrivacyFilterDetector` consumes
either with no client changes), but ~7x faster on CPU on bundled
fixtures because HF's loader gets the official library's CPU
optimisations while opf's from-scratch reimplementation isn't
CPU-tuned. See
[`services/privacy_filter_hf/COMPARE.md`](../services/privacy_filter_hf/COMPARE.md)
for the speed/quality measurements.

Auto-start via `scripts/launcher.sh --privacy-filter-variant hf`
(default is the opf-only variant above). Different port (8003 vs
8001), different cache volume (`privacy-filter-hf-cache` vs
`anonymizer-hf-cache`); the two variants can run side-by-side for
A/B benchmarks without colliding.

| flavour | torch wheels | model | published as | runtime needs |
|---|---|---|---|---|
| `pf-hf-service` | CPU-only | downloads on first start | `:vX.Y.Z-cpu` | none beyond the container |
| `pf-hf-service-cu130` | CUDA 13.0 | downloads on first start | `:vX.Y.Z-cu130` | nvidia GPU + nvidia-container-toolkit |

No baked-model variants for the HF flavour: the build hits
disk-space pressure during commit (transformers + opf + ~3 GB of
weights, with overlayfs duplicating cached files via snapshot
symlinks). For air-gapped HF deployment, populate the HF cache via
a bind mount or a sidecar that pre-fetches the model.

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
actual LLM. **Local-build only** — `scripts/image_builder.sh -f fake-llm`.
Not for production.

### Why some flavours aren't published

CI publishes the variants most operators pull. The local-only set is:

- **Baked-model variants** (`pf-service-baked`,
  `pf-service-baked-cu130`, `gliner-service-baked`,
  `gliner-service-baked-cu130`) — multi-GB images that store
  indefinitely on GHCR for what's usually a build-once artifact. A
  runtime-download image with a persistent HF cache volume gets you to
  the same place after one cold start, so the registry mass isn't
  worth it. Build with `scripts/image_builder.sh -f pf-service-baked`
  (etc.) when you actually need them.
- **`fake-llm`** — a test/dev tool with no place in a production
  registry. Operators who need it for their own CI typically build
  once into their own infra registry.

(CPU sizes assume the default CPU-only PyTorch build. CUDA wheels add
~2 GB on top — that's roughly how much `nvidia-cuda-runtime`,
`nvidia-cudnn`, `nvidia-cublas`, etc. weigh on Linux x86.)

## Building manually

`scripts/image_builder.sh` is the recommended path; the equivalent raw
command for the slim guardrail is:

```bash
podman build --format=docker -t anonymizer-guardrail:latest -f Containerfile .
```

For the privacy-filter / gliner-pii / fake-llm services, see each
service's README for the build recipe and supported build-args
(`TORCH_INDEX_URL`, `BAKE_MODEL`, `TARGET_DEVICE` for the
privacy-filter cu130 variants, etc.).

`--format=docker` is needed because podman defaults to OCI image
format, which doesn't include a HEALTHCHECK field — without the flag,
the `HEALTHCHECK` directive in the Containerfile is silently dropped
and `podman healthcheck run` won't work. `docker build` always emits
Docker format, so the flag is podman-specific (and `image_builder.sh`
adds it conditionally).

## Running manually

The slim guardrail runs without any volume:

```bash
podman run --rm -p 8000:8000 \
  -e LLM_API_BASE=http://litellm:4000/v1 \
  -e LLM_API_KEY=sk-litellm-master \
  -e LLM_MODEL=anonymize \
  --name anonymizer anonymizer-guardrail:latest
```

When `DETECTOR_MODE` includes `privacy_filter` or `gliner_pii`,
the guardrail talks HTTP to the matching service container. Set
`PRIVACY_FILTER_URL` / `GLINER_PII_URL` to point at it, e.g.:

```bash
podman run --rm -p 8000:8000 \
  -e DETECTOR_MODE=regex,privacy_filter,llm \
  -e PRIVACY_FILTER_URL=http://privacy-filter-service:8001 \
  -e LLM_API_BASE=http://litellm:4000/v1 \
  -e LLM_API_KEY=sk-litellm-master \
  --network anonymizer-net \
  --name anonymizer anonymizer-guardrail:latest
```

The privacy-filter-service itself **needs** a persistent volume at
`/app/.opf` — without one, every `podman run` re-downloads the
~3 GB checkpoint:

```bash
podman volume create anonymizer-hf-cache

podman run --rm -d -p 8001:8001 \
  -v anonymizer-hf-cache:/app/.opf \
  --network anonymizer-net \
  --name privacy-filter-service \
  privacy-filter-service:cpu
```

For the cu130 image, also pass `--device nvidia.com/gpu=all`
(podman) or `--gpus all` (docker) — `scripts/launcher.sh` adds
those automatically when it auto-starts a CUDA flavour, but a
manual run needs to. Host needs nvidia-container-toolkit.

First `podman run` of the privacy-filter-service image takes a few
minutes (the model downloads into the volume, blocking the
lifespan hook). The container's healthcheck has a 300-second
start-period to accommodate this — slower networks may need a
longer override via `--health-start-period`. Subsequent runs reuse
the volume and start in seconds.

Volume options compared:

- **Named volume** (`-v anonymizer-hf-cache:/app/.opf`):
  recommended. Auto-managed by Podman/Docker; survives `podman rm`.
- **Bind mount** (`-v /host/path:/app/.opf`): same effect but stores
  the cache wherever you point it on the host. Useful if you want
  the files visible outside Podman's volume store.
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
