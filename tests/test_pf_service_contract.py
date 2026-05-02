"""Wire-format contract tests between the privacy-filter services and
the guardrail-side detector.

The pf detector talks HTTP to a sidecar that emits raw opf
DetectedSpans:

    {"spans": [{"label": str, "start": int, "end": int, "text": str},
               ...]}

This contract is duplicated in three places:

  * `services/privacy_filter/main.py`              (opf-only service)
  * `services/privacy_filter_hf/main.py`           (HF + opf-decoder)
  * `src/anonymizer_guardrail/detector/remote_privacy_filter.py`
                                                    (parser side)

The other test files (test_remote_privacy_filter.py) cover the
parser side. This file pins the *contract itself*: we define the
canonical wire schema as a tiny pydantic model in the test, then

  1. Verify the detector's parser accepts JSON produced by the
     canonical schema (catches detector drift).
  2. When the service deps are installed, import each service's own
     pydantic models and assert they produce byte-identical JSON to
     the canonical schema (catches service drift).

Tests in the second category are gated with `pytest.importorskip` so
the suite still passes in the lightweight test environment that
intentionally lacks torch / transformers / opf.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Keep transitive config harmless — same idiom as the other test files.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest
from pydantic import BaseModel

from anonymizer_guardrail.detector.base import Match
from anonymizer_guardrail.detector.remote_privacy_filter import (
    _LABEL_MAP,
    _parse_matches,
)


# ── Canonical wire schema (what BOTH services must emit) ────────────────────
# Single source of truth for this test file. Each service module
# defines its own copy; the contract test below verifies they match
# THIS one byte-for-byte.
class _CanonicalDetectedSpan(BaseModel):
    label: str
    start: int
    end: int
    text: str


class _CanonicalDetectResponse(BaseModel):
    spans: list[_CanonicalDetectedSpan]


# ── Label vocabulary contract ──────────────────────────────────────────────
# The complete set of raw opf labels the model emits (per its model
# card on HuggingFace). If opf adds a new label, this set lifts to
# include it AND `_LABEL_MAP` should grow a corresponding canonical
# mapping; the test below catches the drift either way.
_OPF_RAW_LABELS = frozenset({
    "private_person",
    "private_email",
    "private_phone",
    "private_url",
    "private_address",
    "private_date",
    "account_number",
    "secret",
})


def test_label_map_covers_every_opf_label() -> None:
    """`_LABEL_MAP` must have a canonical entity-type mapping for
    every raw opf label. Missing labels would silently fall through
    to OTHER, dropping the surrogate generator's ability to produce
    a type-matched substitute. Bumping opf's pinned commit and
    forgetting to update `_LABEL_MAP` is exactly the drift this
    catches at unit-test speed (vs surfacing as wrong-typed
    surrogates in production)."""
    missing = _OPF_RAW_LABELS - set(_LABEL_MAP)
    assert not missing, (
        f"`_LABEL_MAP` in remote_privacy_filter.py is missing canonical "
        f"mappings for {sorted(missing)}. Add entries for each, or "
        f"explicitly drop them from `_OPF_RAW_LABELS` in this test if "
        f"the model card stops emitting them."
    )
    # The reverse direction — extra entries in `_LABEL_MAP` for
    # labels the model never emits — is harmless (dead code) but
    # worth flagging so a future cleanup can spot them.
    extra = set(_LABEL_MAP) - _OPF_RAW_LABELS
    assert not extra, (
        f"`_LABEL_MAP` has entries for labels not in the documented "
        f"opf vocabulary: {sorted(extra)}. Either add them to "
        f"`_OPF_RAW_LABELS` (if the model card now emits them) or "
        f"drop the dead entries from `_LABEL_MAP`."
    )


# ── Detector-side: parser accepts the canonical shape ──────────────────────
class TestParserAgainstCanonicalShape:
    """Verify the guardrail-side parser handles JSON produced by the
    canonical wire schema. Drift on the detector side breaks this."""

    def test_parses_minimum_well_formed_response(self) -> None:
        """A response with one well-formed span produces one Match,
        canonicalised to the project's entity vocabulary."""
        source = "Email alice@example.com about the meeting."
        wire = _CanonicalDetectResponse(spans=[
            _CanonicalDetectedSpan(
                label="private_email",
                start=6, end=23,
                text="alice@example.com",
            ),
        ])
        body = json.loads(wire.model_dump_json())
        matches = _parse_matches(body, source, endpoint="http://test/detect")
        assert len(matches) == 1
        assert matches[0].text == "alice@example.com"
        assert matches[0].entity_type == "EMAIL_ADDRESS"

    def test_parses_empty_spans_field(self) -> None:
        """An empty `spans` list is the canonical "no PII detected"
        response. Must produce zero matches without raising."""
        wire = _CanonicalDetectResponse(spans=[])
        body = json.loads(wire.model_dump_json())
        matches = _parse_matches(body, "any text", endpoint="http://test/detect")
        assert matches == []

    def test_parses_all_canonical_labels(self) -> None:
        """Every raw opf label the model emits should canonicalise
        to a project entity type the surrogate generator can handle.
        Drift in the model's label set (or in our `_LABEL_MAP`)
        surfaces here as missing/extra labels.

        Span texts are picked to NOT end in trailing sentence
        punctuation, because `Match.__post_init__` strips trailing
        `.,;!?` for natural-text entity types — that trim is desired
        canonical behaviour but would make this test compare two
        different strings."""
        source = (
            "Alice Smith called +1 415 555 0123 from 123 Main Street "
            "Email: alice@example.com URL: https://example.com "
            "DOB: 1985-04-15 Account: GB29NWBK60161331926819 "
            "Token: ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"
        )
        # Each tuple is (raw_opf_label, expected_canonical_type, span_text).
        cases = [
            ("private_person",  "PERSON",        "Alice Smith"),
            ("private_phone",   "PHONE",         "+1 415 555 0123"),
            ("private_address", "ADDRESS",       "123 Main Street"),
            ("private_email",   "EMAIL_ADDRESS", "alice@example.com"),
            ("private_url",     "URL",           "https://example.com"),
            ("private_date",    "DATE_OF_BIRTH", "1985-04-15"),
            ("account_number",  "IDENTIFIER",    "GB29NWBK60161331926819"),
            ("secret",          "CREDENTIAL",    "ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"),
        ]
        spans = []
        for raw_label, _, span_text in cases:
            start = source.index(span_text)
            spans.append(_CanonicalDetectedSpan(
                label=raw_label,
                start=start,
                end=start + len(span_text),
                text=span_text,
            ))
        wire = _CanonicalDetectResponse(spans=spans)
        body = json.loads(wire.model_dump_json())
        matches = _parse_matches(body, source, endpoint="http://test/detect")
        produced = {(m.text, m.entity_type) for m in matches}
        expected = {(span_text, canon_type) for _, canon_type, span_text in cases}
        missing = expected - produced
        assert not missing, (
            f"Canonical labels not surfaced as expected entity types. "
            f"Missing: {missing}. Produced: {produced}. "
            f"Check `_LABEL_MAP` in remote_privacy_filter.py."
        )


# ── Service-side: import the actual service modules + diff the schema ─────
# These tests run only when torch / transformers are installed, since
# importing the service modules pulls them in. The test environment
# (tests/conftest.py) intentionally doesn't include them; CI / dev
# machines with the service deps installed catch service-side drift.
class TestServicesEmitCanonicalShape:
    """For each service, import its pydantic models and verify they
    produce JSON byte-identical to what the canonical schema would.
    Catches: a service author renames `start` to `start_offset`, or
    adds a `score` field, or changes the top-level key from `spans`.

    JSON round-trip (rather than annotation introspection) because
    pydantic returns ForwardRef-wrapped annotations for nested model
    types, which doesn't compare cleanly across modules even when the
    on-the-wire shape is identical. The wire format is what we
    actually care about; serialise → compare bytes."""

    _SAMPLE_SPAN_FIELDS = {
        "label": "private_email",
        "start": 6,
        "end": 23,
        "text": "alice@example.com",
    }

    def _canonical_span_json(self) -> str:
        return _CanonicalDetectedSpan(**self._SAMPLE_SPAN_FIELDS).model_dump_json()

    def _canonical_response_json(self) -> str:
        return _CanonicalDetectResponse(
            spans=[_CanonicalDetectedSpan(**self._SAMPLE_SPAN_FIELDS)],
        ).model_dump_json()

    def _import_service_main(self, path: str, mod_name: str) -> Any:
        import importlib.util
        spec = importlib.util.spec_from_file_location(mod_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # `spec_from_file_location` doesn't put the module in
        # sys.modules; pydantic's forward-ref resolution needs the
        # module name to resolve `list[DetectedSpan]`. Rebuild the
        # response model with the spans type explicitly bound so the
        # forward ref is never visited.
        module.DetectResponse.model_rebuild(_types_namespace={
            "DetectedSpan": module.DetectedSpan,
        })
        return module

    def _check_service(self, module: Any, source_label: str) -> None:
        # JSON round-trip on both DetectedSpan and DetectResponse. If
        # the service drops/renames a field (or adds one), the JSON
        # diverges from the canonical schema's serialisation.
        actual_span = module.DetectedSpan(**self._SAMPLE_SPAN_FIELDS).model_dump_json()
        assert actual_span == self._canonical_span_json(), (
            f"{source_label}:DetectedSpan emits JSON that differs from "
            f"the canonical wire schema."
        )
        # Pass the inner span as a plain dict — pydantic validates it
        # into DetectedSpan and the JSON output is independent of which
        # constructor we used.
        actual_response = module.DetectResponse(
            spans=[self._SAMPLE_SPAN_FIELDS],
        ).model_dump_json()
        assert actual_response == self._canonical_response_json(), (
            f"{source_label}:DetectResponse emits JSON that differs from "
            f"the canonical wire schema."
        )

    def test_privacy_filter_service_models_match_canonical(self) -> None:
        # opf and torch are the heavy deps here; skip if not installed.
        pytest.importorskip("opf", reason="services/privacy_filter requires opf")
        pytest.importorskip("torch", reason="services/privacy_filter requires torch")
        module = self._import_service_main(
            "services/privacy_filter/main.py", "_pf_service_main",
        )
        self._check_service(module, "services/privacy_filter/main.py")

    def test_privacy_filter_hf_service_models_match_canonical(self) -> None:
        # Heavy deps for the HF variant.
        pytest.importorskip("transformers", reason="services/privacy_filter_hf requires transformers")
        pytest.importorskip("torch", reason="services/privacy_filter_hf requires torch")
        pytest.importorskip("opf", reason="services/privacy_filter_hf requires opf")
        module = self._import_service_main(
            "services/privacy_filter_hf/main.py", "_pf_hf_service_main",
        )
        self._check_service(module, "services/privacy_filter_hf/main.py")
