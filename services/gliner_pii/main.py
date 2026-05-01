"""
GLiNER-PII inference service.

A tiny FastAPI app that loads NVIDIA's nvidia/gliner-pii model (a fine-tune
of GLiNER large-v2.1) and exposes it over HTTP. Companion to the
privacy-filter inference service — same shape, different model.

Why a separate service: GLiNER's strength over a fixed token-classification
model is *zero-shot labels* — the entity-type list is an input to the
model, not baked into the architecture. Running it as a sidecar lets
multiple guardrail replicas share one inference container, and lets the
heavy ML deps (torch + the gliner library + ~570M model weights) live
outside the guardrail image.

API:

  POST /detect      body: {"text": str,
                           "labels": list[str] | null,
                           "threshold": float | null}
                    response: {"matches": [
                        {"text": str, "entity_type": str,
                         "start": int, "end": int, "score": float},
                        ...
                    ]}

  GET  /health      readiness probe; returns "loading" until the model
                    is in memory, then "ok".

`labels` is the zero-shot entity-type vocabulary the request wants the
model to look for. Omit it to fall back to DEFAULT_LABELS (env-var
configured). `threshold` defaults to DEFAULT_THRESHOLD.

The shape of `entity_type` in the response is whatever label the
*request* asked the model to find (e.g. "ssn", "credit_card",
"person") — this service deliberately doesn't normalize them to the
guardrail's canonical ENTITY_TYPES set. That mapping belongs to the
detector layer (when wired) so this service stays a thin model
wrapper, reusable for non-guardrail uses.

Configuration:
  MODEL_NAME         HuggingFace model id (default nvidia/gliner-pii)
  PORT               listen port (default 8002 — distinct from
                     privacy-filter-service's 8001 so both can run on
                     the same docker network)
  LOG_LEVEL          Python logging level (default INFO)
  DEFAULT_LABELS     Comma-separated default labels for requests that
                     don't supply their own. The default list mirrors
                     the guardrail's canonical entity vocabulary so a
                     bare POST /detect with just {text} returns
                     useful results out of the box.
  DEFAULT_THRESHOLD  Confidence cutoff (0..1). Defaults to 0.5.
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
log = logging.getLogger("gliner-pii-service")


# ── Configuration ──────────────────────────────────────────────────────────
DEFAULT_MODEL = os.environ.get("MODEL_NAME", "nvidia/gliner-pii")

# A reasonable starting label set covering most PII a typical
# anonymization pipeline cares about. Operators can override per-request
# (the API takes a `labels` field) or globally via DEFAULT_LABELS. The
# names match the gliner-PII model card examples (lowercase, snake_case)
# rather than the guardrail's canonical UPPER_SNAKE — keeping the
# service close to the model's native vocabulary makes it more
# reusable outside the guardrail context.
_DEFAULT_LABELS_RAW = os.environ.get(
    "DEFAULT_LABELS",
    "person,organization,email,phone_number,address,date_of_birth,"
    "credit_card,iban,ssn,account_number,ip_address,url,username,"
    "national_id,passport_number,medical_record_number",
)


def _parse_labels(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


DEFAULT_LABELS = _parse_labels(_DEFAULT_LABELS_RAW)

try:
    DEFAULT_THRESHOLD = float(os.environ.get("DEFAULT_THRESHOLD", "0.5"))
except ValueError:
    log.warning(
        "DEFAULT_THRESHOLD=%r is not a float; falling back to 0.5",
        os.environ.get("DEFAULT_THRESHOLD"),
    )
    DEFAULT_THRESHOLD = 0.5


# ── Model loading ──────────────────────────────────────────────────────────
def _load_model(model_name: str) -> Any:
    """Construct the GLiNER model from a HuggingFace model id.

    `gliner` is a thin wrapper over transformers; it pulls torch in as
    a transitive dep and exposes a `from_pretrained` factory that
    matches HuggingFace conventions. Loading is synchronous and
    several seconds on a cold cache — the lifespan hook offloads it
    so it doesn't block the event loop during startup."""
    # Lazy import: keeps this module loadable for tooling/linters that
    # don't have the gliner extra installed. The error path below
    # gives a clear remediation message instead of a generic
    # ModuleNotFoundError.
    try:
        from gliner import GLiNER  # noqa: WPS433
    except ImportError as exc:
        raise ImportError(
            "gliner-pii-service requires the `gliner` package. "
            "Install with `pip install gliner` or rebuild the container "
            "(the Containerfile installs it as part of the image)."
        ) from exc
    return GLiNER.from_pretrained(model_name)


# ── App lifecycle ──────────────────────────────────────────────────────────
# Load the model at startup, not at first request. First-request load
# can take 30+ seconds for a cold model, which would (a) silently time
# out callers and (b) make the /health probe lie about readiness. The
# lifespan hook runs before uvicorn marks the worker ready.
_state: dict[str, Any] = {"model": None, "model_name": DEFAULT_MODEL}


@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    log.info(
        "Loading GLiNER-PII model (%s) — first start may be slow…",
        _state["model_name"],
    )
    _state["model"] = await asyncio.to_thread(_load_model, _state["model_name"])
    log.info(
        "GLiNER-PII ready — default labels=%s threshold=%.2f",
        DEFAULT_LABELS, DEFAULT_THRESHOLD,
    )
    yield
    # No teardown needed — the model holds GPU/CPU memory that the
    # interpreter will reclaim on exit.


app = FastAPI(title="gliner-pii-service", version="0.1.0", lifespan=lifespan)


# ── Request / response schemas ─────────────────────────────────────────────
class DetectRequest(BaseModel):
    text: str = Field(..., description="Free-text input to scan for PII.")
    # `labels=None` means "use the server-configured default". An empty
    # list would be ambiguous (does the operator want zero detections,
    # or the default?), so we treat it the same as None to keep the
    # request shape forgiving.
    labels: list[str] | None = Field(
        default=None,
        description="Zero-shot entity labels to detect. Defaults to "
                    "the server's DEFAULT_LABELS env var.",
    )
    threshold: float | None = Field(
        default=None,
        description="Confidence cutoff (0..1). Defaults to the server's "
                    "DEFAULT_THRESHOLD env var.",
    )


class DetectMatch(BaseModel):
    text: str
    entity_type: str
    start: int
    end: int
    score: float


class DetectResponse(BaseModel):
    matches: list[DetectMatch]


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
        return DetectResponse(matches=[])

    model = _state["model"]
    if model is None:
        # Lifespan hook completed but state is unset — shouldn't happen,
        # but if it does the right answer is to fail loud rather than
        # silently return empty (which would let a caller proceed
        # without knowing the model wasn't actually queried).
        raise RuntimeError("gliner-pii model is not loaded")

    labels = req.labels if req.labels else DEFAULT_LABELS
    threshold = req.threshold if req.threshold is not None else DEFAULT_THRESHOLD

    # Inference is CPU/GPU-bound and synchronous; offload so the loop
    # stays responsive. Same pattern privacy-filter-service uses.
    raw: list[dict[str, Any]] = await asyncio.to_thread(
        model.predict_entities, text, labels, threshold,
    )
    return DetectResponse(matches=_to_matches(raw, text))


def _to_matches(raw: list[dict[str, Any]], source_text: str) -> list[DetectMatch]:
    """Translate gliner's `predict_entities` output into our wire format.

    GLiNER returns dicts of shape:
        {"text": str, "label": str, "score": float, "start": int, "end": int}

    The wire format renames `label` → `entity_type` for parity with the
    privacy-filter-service response. The hallucination guard rejects
    entries whose `text` doesn't actually appear at the claimed offsets
    in the source — defensive against future gliner versions returning
    drifted spans.
    """
    out: list[DetectMatch] = []
    for entry in raw:
        try:
            text = str(entry.get("text", "")).strip()
            label = str(entry.get("label", ""))
            start = entry.get("start")
            end = entry.get("end")
            score = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if not text or not label:
            continue
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end > len(source_text)
            or start >= end
        ):
            continue
        # Defensive: confirm the substring at [start:end] matches the
        # claimed text (modulo whitespace), so a future gliner version
        # with drifted offsets doesn't silently leak unredactable spans.
        if source_text[start:end].strip() != text:
            continue
        out.append(
            DetectMatch(
                text=text, entity_type=label, start=start, end=end, score=score,
            )
        )
    return out
