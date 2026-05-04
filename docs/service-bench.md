# Detector-service latency benchmark

`scripts/service_bench.sh` measures per-request `/detect` latency
for the three detector services
([privacy-filter-service](../services/privacy_filter/README.md),
[gliner-pii-service](../services/gliner_pii/README.md),
[privacy-filter-hf-service](../services/privacy_filter_hf/README.md))
in either CPU or GPU mode (or both, side-by-side, in one run). Where
[detector-bench.md](detector-bench.md) asks *"what fraction of
expected entities does the detector mix catch?"*, this asks *"how
fast does each backing service respond on this hardware, on the same
inputs?"*.

The bench is **out-of-process** — the host script starts each service
container in turn, polls `/health`, sends 30 different payloads to
`POST /detect`, then stops the container before moving to the next
(service, mode) pair. Each request's wall-clock time is captured via
`curl`'s `%{time_total}` so the numbers include the full HTTP
round-trip through podman's port forwarding, not just model forward
time.

## What it measures

For each `(mode, service)` pair:

- **One container start** with the matching image
  (`ghcr.io/bcyrill/<service>:0.5-cpu` or `:0.5-cu130` by default).
  GPU mode passes the device through via
  `--device=nvidia.com/gpu=all`; CPU mode runs plain podman with no
  device flag. Model cache mounts on a persistent named volume
  shared across modes so subsequent runs don't re-download.
- **Health-gated start** — `/health` is polled until `"status":"ok"`.
  The status flips to `ok` only after the model is fully loaded
  (privacy-filter also runs a one-shot warmup `redact()` call before
  flipping). All download / load time stays *outside* the timed
  region.
- **Two timed phases**, each preceded by its own warmup `/detect`
  call (not counted) to absorb first-request kernel-tune cost at
  that input shape:
  - **`short`** — 30 distinct payloads of varied PII shape (emails,
    phones, IBANs, SSNs, MRNs, IPs, URLs, names, addresses, …),
    each ~80–200 chars / ~20–40 tokens. Mirrors typical chat-message
    traffic.
  - **`long`** — the same 30 payloads, each repeated
    `LONG_REPEAT_FACTOR` times (default 50), separated by spaces.
    The result is on the order of a few thousand chars / ~1k–2k
    tokens per request, pushing the forward pass into a regime where
    compute dominates over HTTP / Python / tokenisation overhead
    while staying inside the models' max-position embedding so we
    measure actual length scaling rather than silent truncation.
- Per `(mode, service, size)` row: `total`, `avg`, `min`, `max`,
  plus `ok` / `err` counts.

All three services accept the same `{"text": "..."}` request body —
gliner-pii falls back to its `DEFAULT_LABELS` env var when the
request omits `labels`, so the comparison is apples-to-apples on
input shape.

## Setup

The host needs:

- `podman`. For GPU mode, also the
  [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
  (or pass `GPU_FLAG=--gpus=all` for docker-style runtimes).
- `curl`.
- The relevant images pulled (or network access to ghcr.io). With
  default tags:

```text
ghcr.io/bcyrill/privacy-filter-service:{0.5-cpu,0.5-cu130}
ghcr.io/bcyrill/gliner-pii-service:{0.5-cpu,0.5-cu130}
ghcr.io/bcyrill/privacy-filter-hf-service:{0.5-cpu,0.5-cu130}
```

First run downloads ~3-5 GB of weights per service into named
volumes (`privacy-filter-opf-cache`, `gliner-hf-cache`,
`privacy-filter-hf-cache`). Subsequent runs reuse them, so re-runs
only pay the model-load-into-memory cost (seconds), not the download
cost. CPU and GPU modes share the same volumes — running `both`
doesn't double the disk footprint.

## Running

```bash
scripts/service_bench.sh             # both modes (cpu then gpu)
scripts/service_bench.sh cpu         # cpu only
scripts/service_bench.sh gpu         # gpu only
scripts/service_bench.sh --help
```

Tunables (env vars):

| Variable | Default | Notes |
|---|---|---|
| `IMAGE_TAG_CPU` | `0.5-cpu` | Override to bench a different release for CPU mode. |
| `IMAGE_TAG_GPU` | `0.5-cu130` | Override to bench a different release / CUDA flavour for GPU mode. |
| `GPU_FLAG` | `--device=nvidia.com/gpu=all` | Switch to `--gpus=all` for docker-style runtimes. Only applied in gpu mode. |
| `HEALTH_TIMEOUT_SEC` | `600` | Bump on the very first run if model download is slow. CPU loads can also be slower than GPU on cold start. |
| `REQUEST_TIMEOUT_SEC` | `120` | Per-request curl timeout. The long phase on CPU may need more than the short phase. |
| `LONG_REPEAT_FACTOR` | `50` | How many times each short payload is repeated to build the long set. The default sits comfortably inside the models' ~2048-token max-position embedding, so the bench measures actual length scaling rather than silent truncation. Push higher to stress the truncation regime. |
| `SERVICE_FILTER` | *(empty)* | Comma-separated subset of service names to run. Empty means all three. Names: `privacy-filter`, `gliner-pii`, `privacy-filter-hf`. Useful when you've established that one service's pattern won't change and want to focus a follow-up run on the others. |

The script registers an EXIT trap that stops/removes any
`bench-<service>-<mode>` container it started, so a `^C` mid-run
doesn't leak GPU-attached processes.

## Reading the output

The summary table prints one row per `(mode, service, size)` triple
— up to 12 rows for a `both` run:

```text
mode  service                size   status         total(s)     avg(s)     min(s)     max(s)   ok  err
----- ---------------------- ------ ------------ ---------- ---------- ---------- ---------- ---- ----
cpu   privacy-filter         short  OK             ...        ...        ...        ...        30    0
cpu   privacy-filter         long   OK             ...        ...        ...        ...        30    0
cpu   gliner-pii             short  OK             ...        ...        ...        ...        30    0
cpu   gliner-pii             long   OK             ...        ...        ...        ...        30    0
…
```

- **`mode`** — `cpu` or `gpu`.
- **`size`** — `short` (single payload, ~30 tokens) or `long` (same
  payload repeated `LONG_REPEAT_FACTOR` times).
- **`total`** — sum of the 30 per-request wall-clock seconds for
  that phase.
- **`avg`** — `total / ok`.
- **`min` / `max`** — fastest / slowest single request, useful for
  spotting per-payload variance.
- **`status`** — `OK`, `FAILED_START` (podman couldn't start the
  container), or `UNHEALTHY` (the service didn't reach `status=ok`
  within `HEALTH_TIMEOUT_SEC`). Failures emit one row per phase so
  the table stays rectangular.

The interesting comparison is **per-row CPU vs GPU within the same
size** (does the GPU win on this workload?), and **per-mode short vs
long for the same service** (does GPU help more once the forward
pass has real work to do?).

## Sample results

Single-host run of `LONG_REPEAT_FACTOR=10 scripts/service_bench.sh both`
against image tag `0.5` (`0.5-cpu` and `0.5-cu130` flavours).
30 short payloads + 30 long (each long = its short counterpart
repeated 10×). Absolute numbers depend on the host's CPU and GPU —
treat the *ratios* as the transferable signal:

| service | mode | short avg | long avg | long / short |
|---|---|---|---|---|
| `privacy-filter` (opf custom forward) | cpu | 314 ms | **3168 ms** | 10.1× |
| `privacy-filter` (opf custom forward) | gpu | 346 ms | **3261 ms** | 9.4× |
| `gliner-pii` (GLiNER large-v2.1) | cpu | 148 ms | 260 ms | 1.76× |
| `gliner-pii` (GLiNER large-v2.1) | gpu | 116 ms | 210 ms | 1.81× |
| `privacy-filter-hf` (HF forward + opf decoder) | cpu | 145 ms | 267 ms | 1.84× |
| `privacy-filter-hf` (HF forward + opf decoder) | gpu | 61 ms | 94 ms | 1.54× |

GPU speedup at each length (CPU avg ÷ GPU avg):

| service | short | long |
|---|---|---|
| `privacy-filter` (opf) | 0.91× — **GPU slower** | 0.97× — **still no win** |
| `gliner-pii` | 1.27× | 1.24× |
| `privacy-filter-hf` | 2.37× | **2.86×** — grows with length |

Four findings:

- **opf gets no GPU benefit even on long inputs.** This refutes the
  initial "the short payloads are hiding the GPU advantage"
  hypothesis. With ~10× the work per request, the GPU is still
  within noise of the CPU (and fractionally slower). opf's
  hand-rolled MoE / sliding-window-attention kernels in
  `opf/_model/model.py` simply aren't GPU-accelerated in any
  meaningful way for this workload, regardless of input size.
- **opf is compute-bound, just on the wrong compute.** Its CPU
  long-to-short ratio (10.1×) tracks the 10× repetition almost
  perfectly, and the GPU ratio (9.4×) does too. Wall time on opf
  scales linearly with token count — the issue isn't overhead, it's
  that the heavy work doesn't accelerate.
- **pf-hf's GPU advantage grows with length.** 2.4× on short →
  2.9× on long. Canonical compute-scaled GPU win — exactly the
  shape we expected for kernels that *are* GPU-tuned (HF
  Transformers + cuDNN / SDPA).
- **gliner-pii's GPU advantage stays flat at ~1.25×** across both
  lengths. Predictable: similar fraction of wall time is GPU-eligible
  at every input size on this hardware.

### Follow-up at `LONG_REPEAT_FACTOR=50` (gliner-pii and pf-hf only)

Same host, re-running just `gliner-pii` and `privacy-filter-hf` at
5× the previous long-phase repetition (skipping `privacy-filter`
because the 10× run had already established its no-GPU-win pattern):

| service | mode | short avg | long avg | long / short | GPU speedup at this size |
|---|---|---|---|---|---|
| `gliner-pii` | cpu | 112 ms | 540 ms | 4.82× | — |
| `gliner-pii` | gpu | 116 ms | 529 ms | 4.56× | **1.02× — flat** |
| `privacy-filter-hf` | cpu | 144 ms | 608 ms | 4.22× | — |
| `privacy-filter-hf` | gpu | 61 ms | 114 ms | 1.87× | **5.33× — grew** |

Two new findings on top of the 10× run:

- **pf-hf's GPU advantage scales with workload.** 2.4× at 10×
  repetition → 5.3× at 50×. The previous run hinted at this trend;
  the 50× run confirms it cleanly. On a GPU host, pf-hf at the
  longer workload is **~5× faster** than the same service on CPU,
  while staying inside model context.
- **gliner-pii's GPU advantage flatlines at this length.** At 50×
  the CPU and GPU long-phase averages are within 2% (540 ms vs
  529 ms), even though at 10× there was a ~1.25× GPU win. The
  most likely explanation is that GLiNER large-v2.1's backbone
  (DeBERTa-v3 large, 512-token max-position) is truncating both
  modes internally at ~1500 tokens, and at that clipped length the
  CPU and GPU compute on this host happen to land on the same
  wall-clock. Either way, **GPU is not a meaningful lever for
  gliner-pii on workloads at or near its context limit.**

### What this means for picking a service

Pick the *implementation* before you pick the hardware:

- **`privacy-filter` (opf-only) is the wrong choice on any hardware
  shape we've measured.** Even on a GPU host with long inputs,
  it's ~12× slower than `privacy-filter-hf` running on CPU
  (3261 ms vs 267 ms). They use the same checkpoint and the same
  Viterbi decoder; pf-hf swaps in HF Transformers' forward pass and
  that's where the entire gap comes from.
- **`privacy-filter-hf` is the speed pick** for opf-style detection,
  and **the only service where buying a GPU clearly pays off**.
  It beats opf on CPU at every length we measured, scales gracefully
  with input size, and its GPU advantage *grows* with workload —
  ~2.4× on short, ~2.9× at 10× long, ~5.3× at 50× long.
- **`gliner-pii` is the right pick when you need its zero-shot
  label vocabulary**, not when raw speed or GPU acceleration is the
  deciding factor. Its GPU advantage is small at short inputs
  (~25%) and disappears entirely once inputs hit its 512-token
  context limit (50× repetition).

The numbers are not directly comparable to `gliner-pii` on detection
quality — it's a different model architecture, so use
[detector-bench.md](detector-bench.md) when weighing speed against
recall / precision on a labelled corpus.

### Why GPU helps for some services and not others

Short payloads are 80–200 chars / ~20–40 tokens. For a sub-100 ms
request, a big chunk of wall time is host-side overhead — Python
dispatch + FastAPI + JSON serialisation + podman port-forward +
tokenisation — none of which the GPU accelerates. The hypothesis
going into this bench was that the GPU would pull ahead once the
forward pass had enough work to amortise that fixed cost. **That
turned out to be true for `privacy-filter-hf` but not for `opf`** —
which tells us opf's lack of GPU win isn't a measurement artefact;
it's a property of the implementation.

The long phase repeats each short payload `LONG_REPEAT_FACTOR`
times. Pushed too high (≳50× with these payloads) the inputs may
exceed the model's max-position embedding (~2048 tokens for these
checkpoints) and get truncated silently — still a fair shared upper
bound, but at that point the long-phase numbers reflect "max-context
per request" rather than literal scaling. The default of 50 sits
inside that limit; the run shown here used 10× and stayed well
inside.

## What this benchmark doesn't cover

- **Concurrent requests.** The bench drives one request at a time,
  sequentially. Throughput under concurrent load (batching gains,
  GPU contention) isn't measured.
- **Cold-start cost.** The warmup call is discarded by design.
  Operators who care about first-request latency (autoscale,
  serverless) should measure that separately.
- **Detection quality.** Same caveat as for raw latency in
  `cache-bench.md` — speed only. Use `detector-bench.md` for
  precision / recall.
