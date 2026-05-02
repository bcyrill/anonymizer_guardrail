"""
Privacy-filter inference service.

FastAPI wrapper around the openai/privacy-filter token-classification
model via the [`opf`](https://github.com/openai/privacy-filter)
package's constrained-Viterbi decoder. Pairs with the guardrail's
RemotePrivacyFilterDetector so the heavy ML deps (torch + opf + ~3 GB
of weights) live in their own container instead of inflating the
guardrail image.

API:

  POST /detect      body: {"text": "..."}
                    response: {"spans": [
                        {"label": str, "start": int, "end": int,
                         "text": str},
                        ...
                    ]}

  GET  /health      readiness probe; returns ok once the model is loaded.

The wire format is opf's raw DetectedSpan shape — `label` is the
model's BIO-decoded class (`private_person`, `private_email`, …),
NOT a canonical guardrail entity type. Label translation, merge,
split, and per-label gap-cap post-processing all run client-side
in the guardrail's `RemotePrivacyFilterDetector`. This service is
deliberately a thin opf wrapper, mirroring gliner-pii-service's
shape: model wrapper here, canonicalisation in the detector.

Configuration:
  MODEL_NAME                    Default "openai/privacy-filter" → opf
                                auto-downloads from HuggingFace into
                                ~/.opf/privacy_filter on first request.
                                Anything else → filesystem path to a
                                checkpoint directory.
  PORT                          listen port (default 8001)
  LOG_LEVEL                     Python logging level (default INFO)
  PRIVACY_FILTER_DEVICE         "cpu" (default) or "cuda".
  PRIVACY_FILTER_CALIBRATION    path to a Viterbi calibration JSON.
                                Optional; ignored if missing. See
                                services/privacy_filter/scripts/spike_opf.py
                                for the JSON shape.

Persistent cache: mount a named volume at `/app/.opf` to keep the
model across container recreations. opf writes its checkpoint into
`~/.opf/privacy_filter` (= `/app/.opf/privacy_filter` for the app
user) and ignores HF_HOME. `OPF_CHECKPOINT` is intentionally NOT
set — that env var disables opf's auto-download (it's interpreted
as "operator-supplied checkpoint, trust them"), which we need
active for the runtime-download flavour.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("privacy-filter-service")


# ── Configuration ──────────────────────────────────────────────────────────
DEFAULT_MODEL = os.environ.get("MODEL_NAME", "openai/privacy-filter")

# Per-request perf breakdown. Off by default (no overhead). Set
# PRIVACY_FILTER_PROFILE=1 in the container env to log forward /
# Viterbi / overhead times after each /detect call. Diagnostic
# tool — not for production logging.
_PROFILE_ENABLED = os.environ.get("PRIVACY_FILTER_PROFILE") == "1"

# Worker-thread accumulators. The hooks run in the same thread as
# `model.redact()` (asyncio.to_thread worker), so we use a
# threading.local so concurrent requests on different worker threads
# don't cross-pollute. The collect-and-return happens inside the
# same thread before we hand the result back to the asyncio caller.
_PROFILE_STATE = threading.local()


def _profile_reset() -> None:
    _PROFILE_STATE.forward_ms = []
    _PROFILE_STATE.forward_seq_lens = []
    _PROFILE_STATE.decode_ms = []
    _PROFILE_STATE.decode_seq_lens = []


def _profile_snapshot() -> dict[str, Any]:
    return {
        "forward_ms": list(getattr(_PROFILE_STATE, "forward_ms", [])),
        "forward_seq_lens": list(getattr(_PROFILE_STATE, "forward_seq_lens", [])),
        "decode_ms": list(getattr(_PROFILE_STATE, "decode_ms", [])),
        "decode_seq_lens": list(getattr(_PROFILE_STATE, "decode_seq_lens", [])),
    }


def _install_profiling_hooks(model: Any) -> None:
    """Hook the runtime's forward + the decoder's decode method so we
    can attribute per-request time to model forward (per-window) vs
    Viterbi decode vs everything else.

    Uses a forward pre-hook + post-hook on the nn.Module (no source
    edits to opf), and monkeypatches the decoder's `decode` method
    (the decoder is a plain Python object, no hooks). Both stash
    timings on _PROFILE_STATE.
    """
    runtime, decoder = model.get_prediction_components()

    def _pre(_module: Any, args: Any) -> None:
        _PROFILE_STATE._fwd_t0 = time.perf_counter()
        # args[0] is window_tokens of shape [1, seq_len]
        try:
            _PROFILE_STATE._fwd_seq_len = int(args[0].shape[1])
        except Exception:
            _PROFILE_STATE._fwd_seq_len = -1

    def _post(_module: Any, _args: Any, _output: Any) -> None:
        elapsed_ms = (time.perf_counter() - _PROFILE_STATE._fwd_t0) * 1000
        if not hasattr(_PROFILE_STATE, "forward_ms"):
            _profile_reset()
        _PROFILE_STATE.forward_ms.append(elapsed_ms)
        _PROFILE_STATE.forward_seq_lens.append(_PROFILE_STATE._fwd_seq_len)

    runtime.model.register_forward_pre_hook(_pre)
    runtime.model.register_forward_hook(_post)

    if decoder is not None:
        orig_decode = decoder.decode

        def _timed_decode(token_logprobs: Any) -> Any:
            t0 = time.perf_counter()
            out = orig_decode(token_logprobs)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if not hasattr(_PROFILE_STATE, "decode_ms"):
                _profile_reset()
            _PROFILE_STATE.decode_ms.append(elapsed_ms)
            try:
                _PROFILE_STATE.decode_seq_lens.append(int(token_logprobs.shape[0]))
            except Exception:
                _PROFILE_STATE.decode_seq_lens.append(-1)
            return out

        decoder.decode = _timed_decode  # type: ignore[method-assign]


def _redact_with_profile(model: Any, text: str) -> tuple[Any, dict[str, Any]]:
    """Run redact in the worker thread; reset + snapshot the per-thread
    profile state in the same thread so the hooks (which fire in this
    thread) see a fresh accumulator and we capture their writes
    before returning to the asyncio caller."""
    _profile_reset()
    redact_t0 = time.perf_counter()
    result = model.redact(text)
    redact_ms = (time.perf_counter() - redact_t0) * 1000
    snap = _profile_snapshot()
    snap["redact_ms"] = redact_ms
    return result, snap


# ── Model loading ──────────────────────────────────────────────────────────
def _load_model(model_name: str) -> Any:
    """Construct the opf-backed PII model.

    `device` defaults to "cpu" — opf's own constructor default is
    "cuda", which would silently select GPU on a CPU-only host and
    fail at first request. cu130 image builds set
    `PRIVACY_FILTER_DEVICE=cuda` to flip back.

    `model_name="openai/privacy-filter"` (the default) maps to
    `model=None` for opf's auto-download path; anything else is a
    filesystem path to a checkpoint directory.
    """
    from opf._api import OPF  # heavy import; defer to startup

    opf_model = None if model_name == "openai/privacy-filter" else model_name
    device = os.environ.get("PRIVACY_FILTER_DEVICE", "cpu")
    model = OPF(model=opf_model, device=device, decode_mode="viterbi")
    calibration = os.environ.get("PRIVACY_FILTER_CALIBRATION", "")
    if calibration and os.path.isfile(calibration):
        model.set_viterbi_decoder(calibration_path=calibration)
        log.info("Loaded Viterbi calibration from %s", calibration)
    return model


# ── App lifecycle ──────────────────────────────────────────────────────────
# Load the model at startup, not at first request. First-request load
# can take 30+ seconds for a cold model, which would (a) silently time
# out callers and (b) make the /health probe lie about readiness. The
# lifespan hook runs before uvicorn marks the worker ready.
_state: dict[str, Any] = {"model": None, "model_name": DEFAULT_MODEL}


@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    log.info("Loading privacy-filter model (%s) — first start may be slow…",
             _state["model_name"])
    model = await asyncio.to_thread(_load_model, _state["model_name"])
    # Warm up the lazy runtime loader. opf's `OPF(...)` constructor
    # only stores config; the actual checkpoint resolution and
    # safetensors → torch-tensor load happen on the FIRST `redact()`
    # call. Without this warmup, /health=ok would claim ready while
    # the next /detect request still blocks 30-60s loading 1.5B
    # parameters off disk on a CPU host. Push that cost into startup
    # so /health=ok genuinely means "ready to serve fast", and so
    # operator timeouts on the first probe don't fire spuriously.
    log.info("Warming up runtime (loading model into memory)…")
    await asyncio.to_thread(model.redact, "warmup")
    if _PROFILE_ENABLED:
        # Install hooks AFTER warmup — `runtime`/`decoder` are
        # lazy-built on first redact, so they don't exist before then.
        _install_profiling_hooks(model)
        log.info("PRIVACY_FILTER_PROFILE=1 — per-request phase timings will be logged")
    _state["model"] = model
    log.info("privacy-filter ready")
    yield
    # No teardown needed — the model holds GPU/CPU memory that the
    # interpreter will reclaim on exit.


app = FastAPI(title="privacy-filter-service", version="0.1.0", lifespan=lifespan)


# ── Request / response schemas ─────────────────────────────────────────────
# The wire format is opf's raw DetectedSpan shape — `label` is the
# model's BIO-decoded class (e.g. `private_person`), NOT a canonical
# guardrail entity type. Label translation and merge/split/gap-cap
# post-processing run client-side in
# anonymizer_guardrail.detector._pf_postprocess._to_matches. This
# matches gliner-pii-service's wire contract: the service is a thin
# model wrapper, the client owns canonicalisation.
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
    """Readiness probe. Returns `loading` until the model is in memory.
    Container HEALTHCHECK should treat anything other than "ok" as
    unhealthy so traffic doesn't land before inference is ready."""
    return {
        "status": "ok" if _state["model"] is not None else "loading",
        "model": _state["model_name"],
    }


@app.post("/detect", response_model=DetectResponse)
async def detect(req: DetectRequest) -> DetectResponse:
    text = req.text
    if not text or not text.strip():
        return DetectResponse(spans=[])

    model = _state["model"]
    if model is None:
        # Lifespan hook completed but state is unset — shouldn't happen,
        # but if it does the right answer is to fail loud rather than
        # silently return empty (which would let the guardrail proceed
        # with regex-only redaction without knowing).
        raise RuntimeError("privacy-filter model is not loaded")

    # Inference is CPU/GPU-bound and synchronous; offload so the loop
    # stays responsive.
    if _PROFILE_ENABLED:
        result, snap = await asyncio.to_thread(_redact_with_profile, model, text)
        forward_total = sum(snap["forward_ms"])
        decode_total = sum(snap["decode_ms"])
        overhead = snap["redact_ms"] - forward_total - decode_total
        log.info(
            "profile text=%dB redact=%.0fms = forward=%.0fms (%d windows, lens=%s) "
            "+ viterbi=%.0fms (%d sequences, lens=%s) + overhead=%.0fms",
            len(text),
            snap["redact_ms"],
            forward_total,
            len(snap["forward_ms"]),
            snap["forward_seq_lens"],
            decode_total,
            len(snap["decode_ms"]),
            snap["decode_seq_lens"],
            overhead,
        )
    else:
        result = await asyncio.to_thread(model.redact, text)
    # Emit opf's raw DetectedSpans verbatim — no label translation or
    # span-merging on this side. The guardrail's
    # `RemotePrivacyFilterDetector` runs `_to_matches` to produce
    # canonical Match objects from this list.
    spans = [
        DetectedSpan(
            label=s.label,
            start=s.start,
            end=s.end,
            text=s.text,
        )
        for s in result.detected_spans
    ]
    return DetectResponse(spans=spans)
