"""
Privacy-filter inference service.

A tiny FastAPI app that loads the openai/privacy-filter token-classification
model via the [`opf`](https://github.com/openai/privacy-filter) package
(OpenAI's own constrained-Viterbi decoder, replacing the old
`transformers.pipeline` integration) and exposes it over HTTP. Designed
to be paired with the guardrail's RemotePrivacyFilterDetector so the
heavy ML deps (torch + opf + ~3 GB of weights) live in a separate,
independently-scaled container instead of inflating the guardrail image.

API:

  POST /detect      body: {"text": "..."}
                    response: {"matches": [
                        {"text": str, "entity_type": str,
                         "start": int, "end": int},
                        ...
                    ]}

  GET  /health      readiness probe; returns ok once the model is loaded.

No `score` field on the wire — opf's DetectedSpan doesn't expose a
per-span confidence and we removed the hardcoded `1.0` placeholder
that briefly lived here, since a synthetic constant is worse than
nothing (operators reading it would assume real signal). For
confidence-based routing decisions, layer the regex detector on top
— `AKIA…` / `ghp_…` / etc. matches are deterministic.

The post-processing here (span extract/merge/split) intentionally mirrors
`anonymizer_guardrail.detector.privacy_filter.PrivacyFilterDetector._to_matches`
in-process. Parity is a hard contract — operators must be able to swap
between the in-process and remote variants with zero observable output
drift.

Configuration:
  MODEL_NAME                    Sentinel "openai/privacy-filter" (default) →
                                opf auto-downloads from HuggingFace into
                                ~/.opf/privacy_filter on first request.
                                Anything else → treated as a filesystem path
                                to a checkpoint directory.
  PORT                          listen port (default 8001)
  LOG_LEVEL                     Python logging level (default INFO)
  PRIVACY_FILTER_DEVICE         "cpu" (default) or "cuda" — overrides opf's CUDA default.
  PRIVACY_FILTER_CALIBRATION    path to a Viterbi calibration JSON. Optional;
                                ignored if missing. See
                                services/privacy_filter/scripts/spike_opf.py
                                for the JSON shape.

Cache: opf writes its checkpoint into `~/.opf/privacy_filter` (=
`/app/.opf/privacy_filter` for the app user). That's the path
operators mount the persistent named volume at. We deliberately do
NOT set `OPF_CHECKPOINT` — the env var disables opf's auto-download
path (it's interpreted as "operator says the model is here, trust
them"), which we want active for the non-baked flavour. Earlier
designs tried mounting at `/app/.cache/huggingface` and symlinking
`/app/.opf/` into it; the volume overlay broke that approach
because pathlib's `mkdir(parents=True, exist_ok=True)` chokes on a
dangling symlink in the parent chain when the volume is empty.
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
# detector for the long rationale. tl;dr: defense in depth against
# any decoder that fuses a span across a `\n\n`. opf's Viterbi
# doesn't produce these on our fixtures; kept anyway for safety.
_PARAGRAPH_BREAK = re.compile(r"\n{2,}")


# ── Model loading ──────────────────────────────────────────────────────────
def _load_model(model_name: str) -> Any:
    """Construct the opf-backed PII model.

    `device` defaults to "cpu" — opf's own constructor default is
    "cuda", and silently selecting GPU on a CPU-only host would
    explode at first request. cu130 image builds set
    `PRIVACY_FILTER_DEVICE=cuda` to flip back. Mirrors the in-process
    detector's loader; see anonymizer_guardrail.detector.privacy_filter
    for the longer rationale.

    `model_name` is interpreted in two ways. The default sentinel
    "openai/privacy-filter" (carried over from the transformers
    integration where it was a HuggingFace repo id) is translated to
    `None`, which triggers opf's auto-download path
    (`OPF_CHECKPOINT` env var → bundled default `~/.opf/privacy_filter`
    → fetch from HuggingFace if not on disk). Anything else is
    passed through as a filesystem path to a checkpoint directory.
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


# ── Span post-processing ───────────────────────────────────────────────────
def _to_matches(spans: Any, source_text: str) -> list[dict[str, Any]]:
    """Four passes — extract, merge, split, emit — matching the in-process
    PrivacyFilterDetector exactly. Keeping behaviour identical means a
    deployment can swap between in-process and remote without observable
    output drift.

    `spans` is the iterable of opf DetectedSpan-shaped objects from
    `RedactionResult.detected_spans` — each carrying `.label / .start /
    .end / .text`. opf doesn't expose a per-span score, so the wire
    format omits it.
    """
    # 1) extract: turn opf DetectedSpans into (start, end, type) tuples.
    extracted: list[tuple[int, int, str]] = []
    for span in spans:
        label = getattr(span, "label", "") or ""
        if not label:
            continue
        entity_type = _LABEL_MAP.get(str(label), "OTHER")
        start = getattr(span, "start", None)
        end = getattr(span, "end", None)
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end > len(source_text)
            or start >= end
        ):
            # Decoder didn't give us usable offsets — fall back to the
            # span's own .text with no merging. We can't compute gaps
            # without positions, so this candidate stands alone.
            fallback = str(getattr(span, "text", "") or "").strip()
            if fallback and fallback in source_text:
                pos = source_text.find(fallback)
                extracted.append((pos, pos + len(fallback), entity_type))
            continue
        extracted.append((start, end, entity_type))

    # opf normally returns spans in left-to-right order, but be
    # defensive against re-orders — the merge pass below assumes
    # increasing start.
    extracted.sort(key=lambda s: (s[0], s[1]))

    # 2) merge: same type, gap mergeable by the label's rule.
    merged: list[tuple[int, int, str]] = []
    for start, end, etype in extracted:
        if merged:
            prev_start, prev_end, prev_etype = merged[-1]
            if prev_etype == etype and _gap_is_mergeable(
                source_text, prev_end, start, etype,
            ):
                merged[-1] = (prev_start, end, etype)
                continue
        merged.append((start, end, etype))

    # 3) split — break any span that crosses a paragraph break. Mirror
    # of the merge pass's `\n\n` carve-out: the merge pass refuses to
    # combine two spans across a paragraph break (from below); this
    # pass splits a single span the decoder already combined across
    # one (from above). Defense in depth — opf's Viterbi doesn't
    # produce these on our fixtures, but production traffic is
    # broader than fixtures.
    split: list[tuple[int, int, str]] = []
    for start, end, etype in merged:
        sub = source_text[start:end]
        if "\n\n" not in sub:
            split.append((start, end, etype))
            continue
        cursor = 0
        for m in _PARAGRAPH_BREAK.finditer(sub):
            if m.start() > cursor:
                split.append((start + cursor, start + m.start(), etype))
            cursor = m.end()
        if cursor < len(sub):
            split.append((start + cursor, end, etype))

    # 4) emit
    out: list[dict[str, Any]] = []
    for start, end, etype in split:
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
            }
        )
    return out


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
class DetectRequest(BaseModel):
    text: str = Field(..., description="Free-text input to scan for PII.")


class DetectMatch(BaseModel):
    text: str
    entity_type: str
    start: int
    end: int


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
        # silently return empty (which would let the guardrail proceed
        # with regex-only redaction without knowing).
        raise RuntimeError("privacy-filter model is not loaded")

    # Inference is CPU/GPU-bound and synchronous; offload so the loop
    # stays responsive. Same pattern the in-process detector uses.
    result = await asyncio.to_thread(model.redact, text)
    matches_dicts = _to_matches(result.detected_spans, text)
    return DetectResponse(matches=[DetectMatch(**m) for m in matches_dicts])
