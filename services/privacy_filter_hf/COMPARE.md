# `privacy-filter-hf-service` vs `privacy-filter-service`

Side-by-side measurements of the two privacy-filter service variants
on the same checkpoint (`openai/privacy-filter`), same host, same
fixtures.

## Methodology

* Both services running in podman containers on the same CPU host
  (no GPU). `privacy-filter-service:cpu` on port 8001 (opf-only path),
  `privacy-filter-hf-service:cpu` on port 8003 (HF forward + opf
  Viterbi decode).
* Probe via `services/privacy_filter/scripts/probe.py --url …` so
  both variants exercise the same client code path.
* Three fixtures: a tiny inline string (~13 tokens), `sample.txt`
  (1.3 KB / ~383 tokens), `engagement_notes.txt` (2.5 KB / ~918
  tokens).
* Each fixture probed once per variant. Times include HTTP round-trip
  but the network is loopback so it's negligible.
* Both services warmed up at startup (one redact / forward call before
  the first probe). All measurements are warm.

## Results

### Latency

| Fixture | Tokens | opf-only | hf+opf-decoder | Speedup |
|---|---|---|---|---|
| tiny inline | ~13 | 1.99 s | 0.61 s | **3.3x** |
| sample.txt | ~383 | 32.3 s | 4.66 s | **6.9x** |
| engagement_notes.txt | ~918 | 74.2 s | 10.8 s | **6.9x** |

The hf+opf variant scales at roughly the same per-token cost as the
opf-only variant scales *itself*, just with a much smaller constant.
At 918 tokens, opf-only is at 81 ms/token; hf+opf is at 12 ms/token.
Both variants spend ≥99% of request time in the forward pass (verified
via the `PRIVACY_FILTER_PROFILE=1` perf-counter mode in the opf-only
service); the speedup is entirely attributable to HF Transformers'
better-tuned CPU forward kernels.

### Span output parity

| Fixture | opf-only spans | hf+opf spans | Boundary diffs |
|---|---|---|---|
| tiny inline | 3 | 3 | 0 / 3 |
| sample.txt | 19 | 19 | 0 / 19 |
| engagement_notes.txt | 16 | 16 | 0 / 16 |

After applying whitespace trimming on the HF-side spans (mirroring
opf's `trim_whitespace=True` default — see *Implementation note*
below), every emitted span — label, character offsets, and text —
matches byte-for-byte across both variants on all three fixtures.

### Implementation note: whitespace trimming

The HF tokenizer is BPE-based and fuses a leading space into the
first token of a word (`Ġfoo`-style tokens, where `Ġ` represents a
preceding space). When BIOES tags get translated back to character
spans via `tokenizer.offset_mapping`, the leading space is included
in the span's start offset.

Initial pass without trimming showed 9 of 16 spans on
`engagement_notes.txt` differing by exactly one character on the
`start` side, with the leading character being whitespace:

```
opf:  private_person [ 232:242] 'Sarah Chen'
hf:   private_person [ 231:242] ' Sarah Chen'   ← leading space
```

opf's `OPF` constructor defaults to `trim_whitespace=True`, which
strips this in `_run_inference`'s post-decode step. Our HF variant
applies the same trim in `_bioes_to_spans._span()`. Verified
post-fix: 0 / 16 differences on `engagement_notes.txt`, 0 / 19 on
`sample.txt`.

## Span examples (engagement_notes.txt)

Both variants produced identical output on all 16 spans. A few
representative examples:

| Offsets | Label | Text |
|---|---|---|
| [232:242] | `private_person` | `Sarah Chen` |
| [992:1079] | `secret` | `AKIA4QWERTYUIOPASDFG\n  AWS_SECRET_ACCESS_KEY = wJalr…` |
| [1113:1153] | `secret` | `ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8` |
| [1187:1206] | `secret` | `dc01.acmecorp.local` (model misclassification — see PROBE.md) |
| [1606:1689] | `secret` | NTDS hash dump line |
| [2080:2145] | `secret` | JWT bearer token |

## Why the speed gap exists

opf reimplements the model from scratch in `opf/_model/model.py`:
custom MoE routing, custom sliding-window attention, eager-mode
forward without `torch.compile`. The implementation is correct but
not CPU-perf-tuned.

HF Transformers loads the same checkpoint (`openai/privacy-filter`
ships with `architectures: ["OpenAIPrivacyFilterForTokenClassification"]`
in its config; transformers ≥5.7.0 has the architecture registered
in the model zoo) through the standard
`AutoModelForTokenClassification` loader. That gets:

- Maintainers' years of CPU performance tuning for the surrounding
  inference path
- SDPA-style attention where the architecture supports it
- Eager-mode optimisations baked into Transformers' `_call_impl`
- Fused GLU / RMSNorm where applicable

opf's `ViterbiCRFDecoder` runs on the post-forward logits in 16-35
ms regardless of which forward produced them, so reusing it adds
negligible cost.

## Recommendation

For CPU production deployment, the hf+opf variant is the better
default: 7x faster with full quality parity on the test fixtures.

The opf-only variant remains useful for:

- **GPU deployments** where opf's MoE Triton kernel
  (`OPF_MOE_TRITON=1`) and the `OPF_VITERBI_CUDA_BATCH_SIZE` paths
  may close or reverse the gap (untested here).
- **Air-gapped runs.** Only the opf-only variant ships a baked
  image (`pf-service-baked` / `pf-service-baked-cu130`) — the hf+opf
  build hits disk-space pressure during the bake's layer commit
  (transformers + opf + ~3 GB of weights, with overlayfs duplicating
  cached files via snapshot symlinks, balloons the working set well
  past the image's nominal size), so it ships runtime-download only.
  For an air-gapped hf+opf deployment, populate the HF cache via a
  bind mount or a sidecar that pre-fetches.
- **Reference behaviour for span quality** — opf is the upstream
  source of truth for the Viterbi decoder; the hf+opf variant
  consumes the same `ViterbiCRFDecoder` class but should be
  re-validated whenever opf's pinned commit (`OPF_GIT_REF`) bumps.

## Caveats

- Only tested on CPU. GPU comparison TBD; the trade may invert.
- Only tested on the two bundled fixtures plus a tiny inline string.
  Production corpora may surface tokenizer-edge-case differences
  (rare unicode, code blocks, very long lines) where the two
  variants disagree.
- The hf+opf variant's `_bioes_to_spans` is a custom implementation;
  the opf-only path uses opf's own span builder (`opf/_core/spans.py`).
  These are tested-equivalent on the fixtures here but may diverge
  on edge cases (overlapping predicted spans, malformed BIOES
  sequences the constrained Viterbi shouldn't produce but
  theoretically could).
- The hf+opf variant's wire format is identical to opf-only's only
  after the post-decode whitespace trim. Without that trim, span
  starts can drift by one character on word-initial tokens.
