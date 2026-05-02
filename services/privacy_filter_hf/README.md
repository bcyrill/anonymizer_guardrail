# privacy-filter-hf-service (experimental)

Sibling of [`privacy_filter`](../privacy_filter/) that pairs HF
Transformers' forward pass with opf's Viterbi decoder. Goal: keep
opf's span quality while regaining HF's CPU performance.

## Why this exists

Empirical perf measurement on the opf-only service (see
`docs/detectors/privacy-filter.md` and the perf-counter
`PRIVACY_FILTER_PROFILE=1` mode in `services/privacy_filter/main.py`)
showed forward-pass time scales at ~70 ms/token on CPU, dominating
99% of total request latency. opf reimplements the model from
scratch in `opf/_model/model.py` (custom MoE, custom sliding-window
attention) and isn't CPU-tuned. HF Transformers loads the same
checkpoint with substantially better-tuned kernels.

This variant tests the hypothesis that swapping just the forward
pass closes most of the gap without sacrificing decode quality —
opf's Viterbi runs in 16-35 ms total per request, so reusing it has
a negligible cost.

## Differences from `privacy_filter`

| Aspect | `privacy_filter` (opf-only) | `privacy_filter_hf` (this) |
|---|---|---|
| Model loader | opf's custom `Transformer.from_checkpoint` | `transformers.AutoModelForTokenClassification.from_pretrained` |
| Tokeniser | opf's tiktoken-based encoder | HF Hub `tokenizer.json` via `AutoTokenizer` |
| Decode | opf `ViterbiCRFDecoder` | opf `ViterbiCRFDecoder` (same code, reused) |
| Wire format | `{spans: [{label, start, end, text}]}` | identical |
| Default port | 8001 | 8003 |
| Cache mount path | `/app/.opf` | `/app/.cache/huggingface` |
| Image base | `python:3.12-slim` | `python:3.12-slim` |
| Heavy deps | torch, opf | torch, opf, **transformers** |

The label vocabulary (8 raw opf labels: `private_email`,
`private_person`, …) is identical because both variants run the
same checkpoint. The guardrail-side
`RemotePrivacyFilterDetector` consumes either with no changes.

## Build

```bash
podman build -t privacy-filter-hf-service:cpu \
    -f services/privacy_filter_hf/Containerfile services/privacy_filter_hf/

# Or via the image_builder:
scripts/image_builder.sh -f pf-hf-service
```

## Run

```bash
podman volume create privacy-filter-hf-cache

# First start downloads ~3 GB (model + tokenizer) into the volume.
podman run --rm -p 8003:8003 \
    -v privacy-filter-hf-cache:/app/.cache/huggingface \
    --name pf-hf privacy-filter-hf-service:cpu
```

## Probe

The existing privacy-filter probe works against this variant — point
it at the alternate port:

```bash
python services/privacy_filter/scripts/probe.py \
    --url http://localhost:8003 \
    --text-file services/privacy_filter/scripts/sample.txt
```

Expected output: same `private_*` labels and span boundaries as the
opf-only service. Different timing.

## Side-by-side benchmarks

Both services can run on the same host on different ports. Probe
each in turn and compare:

```bash
# opf-only
podman run --rm -d -p 8001:8001 \
    -v anonymizer-hf-cache:/app/.opf \
    --name pf-opf privacy-filter-service:cpu

# HF + opf-decoder
podman run --rm -d -p 8003:8003 \
    -v privacy-filter-hf-cache:/app/.cache/huggingface \
    --name pf-hf privacy-filter-hf-service:cpu

time python services/privacy_filter/scripts/probe.py \
    --url http://localhost:8001 --text-file services/privacy_filter/scripts/sample.txt
time python services/privacy_filter/scripts/probe.py \
    --url http://localhost:8003 --text-file services/privacy_filter/scripts/sample.txt
```

## Status

Experimental. Not wired into the launcher / image-builder presets —
add it manually with `--flavour pf-hf-service` or with the
`podman run` invocations above. Promote to a launcher backend once
the speed/quality measurements justify replacing the opf-only
variant in production.
