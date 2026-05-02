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
