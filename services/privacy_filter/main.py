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

# NER models often split a single entity into adjacent same-type spans
# (e.g. "Alice Smith" → two PERSON tokens). Merge them when the gap is
# pure whitespace; keep them separate when a non-whitespace separator
# (slash, pipe, "and", etc.) signals a list. Addresses get the comma
# exception because it's structurally part of the value.
#
# Both rules also forbid `\n\n` (paragraph break) — a blank line is a
# stronger separator than a same-type label call, so adjacent spans
# bracketing a paragraph break stay distinct even when the model labels
# them the same way. Mirrors the in-process detector's gap rules; see
# anonymizer_guardrail.detector.privacy_filter for the longer rationale.
_DEFAULT_GAP_PATTERN = re.compile(r"[^\S\n]*\n?[^\S\n]*")
_GAP_PATTERNS: dict[str, re.Pattern[str]] = {
    "ADDRESS": re.compile(r"(?:[^\S\n]|,)*\n?(?:[^\S\n]|,)*"),
}

# Paragraph break used by the split pass — see the in-process
# detector for the long rationale. tl;dr: HF's aggregation step can
# fuse a span across a `\n\n` when it tags both halves with the
# same entity; we split it back apart here.
_PARAGRAPH_BREAK = re.compile(r"\n{2,}")


def _gap_pattern_for(entity_type: str) -> re.Pattern[str]:
    return _GAP_PATTERNS.get(entity_type, _DEFAULT_GAP_PATTERN)


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

    # 2) merge: same type, non-overlapping, gap matches the type's rule.
    # When merging, take the max of the two scores so the merged span's
    # confidence reflects the strongest evidence the model gave us.
    merged: list[tuple[int, int, str, float]] = []
    for start, end, etype, score in extracted:
        if merged:
            prev_start, prev_end, prev_etype, prev_score = merged[-1]
            if prev_etype == etype and prev_end <= start:
                gap = source_text[prev_end:start]
                if not gap or _gap_pattern_for(etype).fullmatch(gap):
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
