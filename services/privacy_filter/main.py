"""
Privacy-filter inference service.

A tiny FastAPI app that loads the openai/privacy-filter token-classification
model and exposes it over HTTP. Designed to be paired with the guardrail's
RemotePrivacyFilterDetector so the heavy ML deps (torch + transformers +
~6 GB of weights) live in a separate, independently-scaled container
instead of inflating the guardrail image.

API:

  POST /detect      body: {"text": "..."}
                    response: {"matches": [
                        {"text": str, "entity_type": str,
                         "start": int, "end": int, "score": float},
                        ...
                    ]}

  GET  /health      readiness probe; returns ok once the model is loaded.

The post-processing here (span extract/merge) intentionally mirrors what
`anonymizer_guardrail.detector.privacy_filter.PrivacyFilterDetector._to_matches`
does in-process. Keeping the logic in the service means the guardrail
client only has to deserialize JSON — model evolution (new label set,
different tokenizer behaviour) ships entirely on the service side.

Configuration:
  MODEL_NAME    HuggingFace model id (default openai/privacy-filter)
  PORT          listen port (default 8001)
  LOG_LEVEL     Python logging level (default INFO)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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


# ── Label map and merging rules ────────────────────────────────────────────
# Mirrors anonymizer_guardrail.detector.privacy_filter so output is
# semantically identical to the in-process detector. Any label not in the
# map falls through to OTHER on the client side via Match.__post_init__.
_LABEL_MAP: dict[str, str] = {
    "private_person":  "PERSON",
    "private_email":   "EMAIL_ADDRESS",
    "private_phone":   "PHONE",
    "private_url":     "URL",
    "private_address": "ADDRESS",
    # The model treats any date as PII; we map to DATE_OF_BIRTH because
    # that's the only date-shaped entity type the guardrail has. Operators
    # wanting to distinguish arbitrary dates should post-filter.
    "private_date":    "DATE_OF_BIRTH",
    # `account_number` is a catch-all for IBAN / CC / SSN-shaped strings.
    # The model doesn't tell us which, so we use IDENTIFIER and let the
    # client's regex layer claim the more specific shapes first.
    "account_number":  "IDENTIFIER",
    "secret":          "CREDENTIAL",
}

# Per-label merge rules — mirrors the in-process detector. See
# anonymizer_guardrail.detector.privacy_filter for the longer
# rationale. tl;dr: each label gets its own max-gap length and
# allowed connector set; `\n\n` blocks merging unconditionally.
_MAX_GAP_BY_LABEL: dict[str, int] = {
    "PERSON": 3,
    "ADDRESS": 3,
    "DATE_OF_BIRTH": 3,
    "URL": 2,
    "PHONE": 2,
    "CREDENTIAL": 2,
    "IDENTIFIER": 1,
    "EMAIL_ADDRESS": 0,   # never merge — emails don't fragment with whitespace
}
_DEFAULT_MAX_GAP = 1

_DEFAULT_CONNECTORS: frozenset[str] = frozenset(" \t\n\r")
_CONNECTORS_BY_LABEL: dict[str, frozenset[str]] = {
    "ADDRESS": _DEFAULT_CONNECTORS | frozenset(","),
}


def _gap_is_mergeable(
    source_text: str,
    left_end: int,
    right_start: int,
    label: str,
) -> bool:
    """Mirrors the in-process detector's predicate; see
    anonymizer_guardrail.detector.privacy_filter for the rule order."""
    if right_start < left_end:
        return False
    gap = source_text[left_end:right_start]
    if not gap:
        return True
    if "\n\n" in gap:
        return False
    max_gap = _MAX_GAP_BY_LABEL.get(label, _DEFAULT_MAX_GAP)
    if len(gap) > max_gap:
        return False
    connectors = _CONNECTORS_BY_LABEL.get(label, _DEFAULT_CONNECTORS)
    return all(ch in connectors for ch in gap)


# Paragraph break used by the split pass — see the in-process
# detector for the long rationale. tl;dr: HF's aggregation step can
# fuse a span across a `\n\n` when it tags both halves with the
# same entity; we split it back apart here.
_PARAGRAPH_BREAK = re.compile(r"\n{2,}")


# ── Pipeline loading ───────────────────────────────────────────────────────
def _load_pipeline(model_name: str) -> Any:
    """Construct the HuggingFace token-classification pipeline.

    aggregation_strategy="first" collapses BIOES per-token output into
    contiguous spans, with the entity_group derived from the first
    token's label — same setting the in-process detector uses, so the
    raw spans look identical."""
    from transformers import pipeline  # heavy import; defer to startup

    return pipeline(
        "token-classification",
        model=model_name,
        aggregation_strategy="first",
    )


# ── Span post-processing ───────────────────────────────────────────────────
def _to_matches(spans: list[dict[str, Any]], source_text: str) -> list[dict[str, Any]]:
    """Three passes — extract, merge, emit — matching the in-process
    PrivacyFilterDetector exactly. Keeping behaviour identical means a
    deployment can swap between in-process and remote without observable
    output drift.
    """
    # 1) extract: turn raw pipeline output into typed (start, end, type, score)
    extracted: list[tuple[int, int, str, float]] = []
    for span in spans:
        label = span.get("entity_group") or span.get("entity")
        if not label:
            continue
        entity_type = _LABEL_MAP.get(str(label), "OTHER")
        score_raw = span.get("score", 0.0)
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = 0.0
        start, end = span.get("start"), span.get("end")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end > len(source_text)
            or start >= end
        ):
            # Tokenizer didn't give us usable offsets — fall back to the
            # `word` field with no merging. We can't compute gaps without
            # positions, so this candidate stands alone.
            fallback = str(span.get("word", "")).strip()
            if fallback and fallback in source_text:
                pos = source_text.find(fallback)
                extracted.append((pos, pos + len(fallback), entity_type, score))
            continue
        extracted.append((start, end, entity_type, score))

    # The pipeline normally returns spans in left-to-right order, but
    # be defensive against re-orders introduced by future aggregation
    # strategies — the merge pass below assumes increasing start.
    extracted.sort(key=lambda s: (s[0], s[1]))

    # 2) merge: same type, gap mergeable by the label's rule. Score is
    # the max of the two so the merged span's confidence reflects the
    # strongest evidence the model gave us.
    merged: list[tuple[int, int, str, float]] = []
    for start, end, etype, score in extracted:
        if merged:
            prev_start, prev_end, prev_etype, prev_score = merged[-1]
            if prev_etype == etype and _gap_is_mergeable(
                source_text, prev_end, start, etype,
            ):
                merged[-1] = (prev_start, end, etype, max(prev_score, score))
                continue
        merged.append((start, end, etype, score))

    # 3) split — break any span that crosses a paragraph break. The HF
    # pipeline's aggregation_strategy="first" can fuse a span across a
    # `\n\n` when the model labels tokens on both sides identically;
    # we apply the same paragraph-boundary rule the merge pass uses,
    # just from the opposite direction. Score is preserved unchanged
    # on each piece — splitting doesn't reduce confidence in any one
    # half, just acknowledges they're unrelated entities.
    split: list[tuple[int, int, str, float]] = []
    for start, end, etype, score in merged:
        sub = source_text[start:end]
        if "\n\n" not in sub:
            split.append((start, end, etype, score))
            continue
        cursor = 0
        for m in _PARAGRAPH_BREAK.finditer(sub):
            if m.start() > cursor:
                split.append((start + cursor, start + m.start(), etype, score))
            cursor = m.end()
        if cursor < len(sub):
            split.append((start + cursor, end, etype, score))

    # 4) emit
    out: list[dict[str, Any]] = []
    for start, end, etype, score in split:
        value = source_text[start:end].strip()
        # Hallucination / drift guard: the substring must actually be
        # in the source text. Slicing can fall outside it if the
        # tokenizer's offsets ever drift.
        if not value or value not in source_text:
            continue
        out.append(
            {
                "text": value,
                "entity_type": etype,
                "start": start,
                "end": end,
                "score": score,
            }
        )
    return out


# ── App lifecycle ──────────────────────────────────────────────────────────
# Load the pipeline at startup, not at first request. First-request load
# can take 30+ seconds for a cold model, which would (a) silently time
# out callers and (b) make the /health probe lie about readiness. The
# lifespan hook runs before uvicorn marks the worker ready.
_state: dict[str, Any] = {"pipeline": None, "model_name": DEFAULT_MODEL}


@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    log.info("Loading privacy-filter model (%s) — first start may be slow…",
             _state["model_name"])
    _state["pipeline"] = await asyncio.to_thread(_load_pipeline, _state["model_name"])
    log.info("privacy-filter ready")
    yield
    # No teardown needed — the pipeline holds GPU/CPU memory that the
    # interpreter will reclaim on exit.


app = FastAPI(title="privacy-filter-service", version="0.1.0", lifespan=lifespan)


# ── Request / response schemas ─────────────────────────────────────────────
class DetectRequest(BaseModel):
    text: str = Field(..., description="Free-text input to scan for PII.")


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
        "status": "ok" if _state["pipeline"] is not None else "loading",
        "model": _state["model_name"],
    }


@app.post("/detect", response_model=DetectResponse)
async def detect(req: DetectRequest) -> DetectResponse:
    text = req.text
    if not text or not text.strip():
        return DetectResponse(matches=[])

    pipeline_obj = _state["pipeline"]
    if pipeline_obj is None:
        # Lifespan hook completed but state is unset — shouldn't happen,
        # but if it does the right answer is to fail loud rather than
        # silently return empty (which would let the guardrail proceed
        # with regex-only redaction without knowing).
        raise RuntimeError("privacy-filter model is not loaded")

    # Inference is CPU/GPU-bound and synchronous; offload so the loop
    # stays responsive. Same pattern the in-process detector uses.
    spans: list[dict[str, Any]] = await asyncio.to_thread(pipeline_obj, text)
    matches_dicts = _to_matches(spans, text)
    return DetectResponse(matches=[DetectMatch(**m) for m in matches_dicts])
