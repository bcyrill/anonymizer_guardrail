"""Privacy-filter-HF inference service.

Experimental sibling of services/privacy_filter/ that pairs:

  * HuggingFace `AutoModelForTokenClassification` for the **forward
    pass** — gets HF Transformers' optimised CPU paths, attention
    kernels, and the years of perf tuning baked into the official
    library.
  * opf's `ViterbiCRFDecoder` for the **decode** step — same
    constrained BIOES Viterbi the opf-only service uses, so span
    quality (tight boundaries, no `\\n\\n` over-merges) is preserved.

The hypothesis being tested: opf's from-scratch Transformer
reimplementation (`opf/_model/model.py`) is 30-70x slower per token on
CPU than HF's loader of the same checkpoint. If that's the dominant
cost — which the perf-counter measurements showed it is — replacing
just the forward pass should give us most of HF's speed back without
sacrificing decode quality.

Wire-format-compatible with services/privacy_filter/: same DetectedSpan
shape (raw opf-vocabulary labels: `private_email`, `private_person`,
…), so the guardrail's RemotePrivacyFilterDetector talks to either
service with no client-side changes. A/B is just `PRIVACY_FILTER_URL`
pointing at port 8001 (opf-only) vs 8003 (HF + opf-decoder).

Configuration:
  MODEL_NAME           default `openai/privacy-filter`. HF Hub model
                       id; tokenizer + weights download to
                       `~/.cache/huggingface/hub/` on first call.
  HOST / PORT          default 0.0.0.0:8003
  LOG_LEVEL            python logging level, default INFO
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import torch
import torch.nn.functional as F
from fastapi import FastAPI
from pydantic import BaseModel, Field
from transformers import AutoModelForTokenClassification, AutoTokenizer

# opf provides the decoder + label-space scaffolding. We don't use any
# of opf's model loading or runtime — only the Viterbi DP and the
# BIOES tag bookkeeping. Pinned to the same commit as the opf-only
# service so both variants decode against identical transition
# constraints.
from opf._core.decoding import ViterbiCRFDecoder
from opf._core.sequence_labeling import build_label_info

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("privacy-filter-hf-service")


# ── Configuration ──────────────────────────────────────────────────────────
DEFAULT_MODEL = os.environ.get("MODEL_NAME", "openai/privacy-filter")


# ── Model loading ──────────────────────────────────────────────────────────
def _load_model(model_name: str) -> dict[str, Any]:
    """Pull the HF model + tokenizer, build the Viterbi decoder.

    Both the model and the decoder are constructed once at startup and
    reused across requests. The decoder is keyed off the model's
    `id2label` so the BIOES tag list matches exactly — feeding the
    decoder a different label space than the model produced would
    silently misroute spans.
    """
    log.info("Loading tokenizer %s", model_name)
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    log.info("Loading model %s", model_name)
    model = AutoModelForTokenClassification.from_pretrained(model_name)
    model.eval()

    # The model's id2label is keyed by str on JSON-loaded configs and by
    # int after Transformers-side normalisation. Sort by integer key
    # either way to get a stable ordering.
    id2label = {int(k): v for k, v in model.config.id2label.items()}
    class_names = [id2label[i] for i in range(len(id2label))]
    label_info = build_label_info(class_names)
    decoder = ViterbiCRFDecoder(label_info=label_info)

    return {
        "tokenizer": tokenizer,
        "model": model,
        "decoder": decoder,
        "id2label": id2label,
    }


# ── Span reconstruction ────────────────────────────────────────────────────
# BIOES → character-span conversion. The decoder returns one tag id per
# token; we walk the sequence, group consecutive tags belonging to one
# entity, and translate token offsets to character offsets via the fast
# tokenizer's offset_mapping.
def _bioes_to_spans(
    label_ids: list[int],
    id2label: dict[int, str],
    offsets: list[tuple[int, int]],
    text: str,
) -> list[dict[str, Any]]:
    """Walk a BIOES label sequence and emit one entry per entity span.

    The opf-only service emits `DetectedSpan(label=<base>, start, end,
    text)` where `<base>` is the underscore-form label without the
    BIOES prefix (`private_email`, not `B-private_email`). We emit the
    same shape so the wire format is identical.

    Tokens with offset (0, 0) — special tokens like CLS/SEP — are
    skipped. They have no character span and BIOES tags on them would
    be meaningless anyway.
    """
    spans: list[dict[str, Any]] = []
    cur_label: str | None = None
    cur_start: int | None = None
    cur_end: int | None = None

    for tok_idx, label_id in enumerate(label_ids):
        tag_str = id2label.get(label_id, "O")
        ch_start, ch_end = offsets[tok_idx]
        # Skip special tokens (no character span) and any label/offset
        # mismatch we can't make sense of.
        if ch_start == ch_end == 0 and tok_idx > 0:
            continue
        if tag_str == "O" or "-" not in tag_str:
            if cur_label is not None and cur_start is not None and cur_end is not None:
                spans.append(_span(cur_label, cur_start, cur_end, text))
            cur_label = cur_start = cur_end = None
            continue

        boundary, base = tag_str.split("-", 1)
        if boundary == "B":
            # Close any open span and start a new one.
            if cur_label is not None and cur_start is not None and cur_end is not None:
                spans.append(_span(cur_label, cur_start, cur_end, text))
            cur_label = base
            cur_start = ch_start
            cur_end = ch_end
        elif boundary == "I":
            # Continue an existing span only if the type matches; if
            # the constrained Viterbi did its job an I always follows a
            # B/I of the same type, but be defensive.
            if cur_label == base and cur_start is not None:
                cur_end = ch_end
            else:
                # Stray I — close any open span, start fresh as if B.
                if cur_label is not None and cur_start is not None and cur_end is not None:
                    spans.append(_span(cur_label, cur_start, cur_end, text))
                cur_label = base
                cur_start = ch_start
                cur_end = ch_end
        elif boundary == "E":
            # Close out a span. Same defensive handling as I — if the
            # current type doesn't match, treat E as a single-token span.
            if cur_label == base and cur_start is not None:
                cur_end = ch_end
                spans.append(_span(cur_label, cur_start, cur_end, text))
            else:
                if cur_label is not None and cur_start is not None and cur_end is not None:
                    spans.append(_span(cur_label, cur_start, cur_end, text))
                spans.append(_span(base, ch_start, ch_end, text))
            cur_label = cur_start = cur_end = None
        elif boundary == "S":
            # Single-token span; emits standalone, doesn't touch the open span.
            if cur_label is not None and cur_start is not None and cur_end is not None:
                spans.append(_span(cur_label, cur_start, cur_end, text))
                cur_label = cur_start = cur_end = None
            spans.append(_span(base, ch_start, ch_end, text))

    # Trailing open span (sequence ended on B/I without an E — possible
    # if the constrained decoder's end transitions allowed it).
    if cur_label is not None and cur_start is not None and cur_end is not None:
        spans.append(_span(cur_label, cur_start, cur_end, text))

    return spans


def _span(label: str, start: int, end: int, text: str) -> dict[str, Any]:
    """Build a wire-format span entry, trimming whitespace at the
    boundaries.

    HF's BPE tokenizer fuses a leading space into the first token of
    a word (`Ġfoo` → token starts at the space, not at `f`), so
    BIOES-derived character offsets typically include a leading space
    when the entity isn't at position 0. opf's runtime strips this
    via `trim_whitespace=True` (its constructor default); we match that
    so the wire output is interchangeable between variants.
    """
    body = text[start:end]
    leading = len(body) - len(body.lstrip())
    trailing = len(body) - len(body.rstrip())
    if leading or trailing:
        start += leading
        end -= trailing
        body = text[start:end]
    return {
        "label": label,
        "start": start,
        "end": end,
        "text": body,
    }


# ── App lifecycle ──────────────────────────────────────────────────────────
_state: dict[str, Any] = {
    "tokenizer": None,
    "model": None,
    "decoder": None,
    "id2label": None,
    "model_name": DEFAULT_MODEL,
}


@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    log.info("Loading privacy-filter (HF variant) — first start downloads ~3 GB…")
    loaded = await asyncio.to_thread(_load_model, _state["model_name"])
    # Warm the model so first /detect doesn't pay torch's lazy-init
    # cost (cuDNN handles, eager-mode op caches, etc.). Mirrors the
    # opf-only service.
    log.info("Warming up runtime…")
    await asyncio.to_thread(_run_inference, loaded, "warmup")
    _state.update(loaded)
    log.info("privacy-filter-hf ready")
    yield


app = FastAPI(
    title="privacy-filter-hf-service", version="0.1.0", lifespan=lifespan
)


# ── Inference path ─────────────────────────────────────────────────────────
@torch.inference_mode()
def _run_inference(state: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """One synchronous forward + decode + span reconstruction.

    Inputs short enough to fit in the model's context don't need
    chunking. The model's max_position_embeddings is 131072, so any
    realistic guardrail input fits in one window.
    """
    tokenizer = state["tokenizer"]
    model = state["model"]
    decoder = state["decoder"]
    id2label = state["id2label"]

    enc = tokenizer(
        text,
        return_tensors="pt",
        return_offsets_mapping=True,
        truncation=False,
    )
    offsets = enc.pop("offset_mapping")[0].tolist()
    enc = {k: v.to(model.device) for k, v in enc.items()}
    outputs = model(**enc)
    # log_softmax once, on CPU to match opf's decoder expectations
    # (decoder caches its score tensors per (device, dtype)).
    log_probs = F.log_softmax(outputs.logits.float(), dim=-1)[0].cpu()
    label_ids = decoder.decode(log_probs)
    return _bioes_to_spans(label_ids, id2label, offsets, text)


# ── Request / response schemas ─────────────────────────────────────────────
# Identical to services/privacy_filter/main.py — the wire format is
# the cross-variant contract.
class DetectRequest(BaseModel):
    text: str = Field(..., description="Free-text input to scan for PII.")


class DetectedSpan(BaseModel):
    label: str
    start: int
    end: int
    text: str


class DetectResponse(BaseModel):
    spans: list[DetectedSpan]


# ── Endpoints ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok" if _state["model"] is not None else "loading",
        "model": _state["model_name"],
        "variant": "hf+opf-viterbi",
    }


@app.post("/detect", response_model=DetectResponse)
async def detect(req: DetectRequest) -> DetectResponse:
    text = req.text
    if not text or not text.strip():
        return DetectResponse(spans=[])

    if _state["model"] is None:
        raise RuntimeError("privacy-filter-hf model is not loaded")

    spans = await asyncio.to_thread(_run_inference, _state, text)
    return DetectResponse(spans=[DetectedSpan(**s) for s in spans])
