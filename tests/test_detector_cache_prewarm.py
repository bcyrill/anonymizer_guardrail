"""Tests for `DETECTOR_CACHE_PREWARM` — the deanonymize-side
detector-cache pre-warm.

What's being pinned:

  * The cross-field validator at boot (DETECTOR_CACHE_PREWARM=true
    requires USE_FAKER=false). Operators get a clean Pydantic error
    on misconfig rather than runtime confusion.
  * Per-call `use_faker=true` overrides are rejected with a warning
    when prewarm is on (the override silently ignored, surrogate
    generator falls back to config.use_faker=false).
  * The opaque-token entity-type extractor handles every canonical
    `ENTITY_TYPES` value + falls through to None for non-opaque
    surrogates.
  * Round-trip: anonymize → deanonymize on a text seen at turn N,
    next anonymize on the same text → cache hit (no real detection).
  * Pre-warm doesn't fire when the flag is off (default).

This file deliberately doesn't assert per-detector cache
hit/miss counter values — Option 2's known caveat is that those
stats become misleading post-prewarm. The doc carries the
caveat; the tests pin the operationally-load-bearing properties
(cache hits skip detection; flag gating; mutual-exclusion).
"""

from __future__ import annotations

import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Match other test modules: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

from anonymizer_guardrail.config import Config


# ── Boot validator ─────────────────────────────────────────────────────


def test_prewarm_with_faker_rejected_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DETECTOR_CACHE_PREWARM=true requires USE_FAKER=false. The
    validator catches the misconfig at config construction so the
    process refuses to start rather than failing at first
    deanonymize call."""
    from pydantic import ValidationError

    monkeypatch.setenv("DETECTOR_CACHE_PREWARM", "true")
    monkeypatch.setenv("USE_FAKER", "true")
    with pytest.raises(ValidationError, match="USE_FAKER=false"):
        Config()


def test_prewarm_with_opaque_tokens_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supported combination — prewarm on, faker off."""
    monkeypatch.setenv("DETECTOR_CACHE_PREWARM", "true")
    monkeypatch.setenv("USE_FAKER", "false")
    cfg = Config()
    assert cfg.detector_cache_prewarm is True
    assert cfg.use_faker is False


def test_prewarm_off_with_faker_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When prewarm is off, USE_FAKER can be anything. Default config
    (prewarm=false, faker=true) must construct cleanly — the
    validator only fires for the conflicting pair."""
    monkeypatch.setenv("DETECTOR_CACHE_PREWARM", "false")
    monkeypatch.setenv("USE_FAKER", "true")
    cfg = Config()  # no raise


# ── Surrogate prefix extractor ─────────────────────────────────────────


def test_opaque_token_extractor_recovers_canonical_types() -> None:
    from anonymizer_guardrail.detector.base import ENTITY_TYPES
    from anonymizer_guardrail.surrogate import opaque_token_entity_type

    # Every canonical entity_type round-trips through the opaque
    # format. This is the load-bearing invariant — if someone adds
    # a new ENTITY_TYPES entry that breaks the regex pattern (e.g.
    # contains lowercase), prewarm silently stops working for that
    # type.
    for etype in ENTITY_TYPES:
        surrogate = f"[{etype}_DEADBEEF]"
        assert opaque_token_entity_type(surrogate) == etype, (
            f"failed to recover {etype!r} from {surrogate!r}"
        )


def test_opaque_token_extractor_returns_none_for_faker_output() -> None:
    """Faker output (realistic names, IPs, emails) doesn't match the
    opaque pattern. Extractor returns None — caller treats as
    "skip this entry"."""
    from anonymizer_guardrail.surrogate import opaque_token_entity_type

    for non_opaque in (
        "Robert Jones",
        "192.0.2.5",
        "alice@corp.example",
        "+1-555-0142",
        "1234-5678-9012-3456",
    ):
        assert opaque_token_entity_type(non_opaque) is None


def test_opaque_token_extractor_rejects_unknown_types() -> None:
    """A `[BOGUS_DEADBEEF]` surrogate has the right shape but BOGUS
    isn't in `ENTITY_TYPES`. Extractor returns None — same posture
    as `Match.__post_init__`'s "unknown types fall to OTHER", but
    here we'd rather skip than silently misclassify."""
    from anonymizer_guardrail.surrogate import opaque_token_entity_type

    assert opaque_token_entity_type("[BOGUS_TYPE_DEADBEEF]") is None
    assert opaque_token_entity_type("[NOT_REAL_AABBCCDD]") is None


def test_opaque_token_extractor_rejects_malformed_hex() -> None:
    """Hex tail must be exactly 8 uppercase chars — anything else
    isn't an opaque token we wrote."""
    from anonymizer_guardrail.surrogate import opaque_token_entity_type

    assert opaque_token_entity_type("[EMAIL_ADDRESS_short]") is None     # too few chars
    assert opaque_token_entity_type("[EMAIL_ADDRESS_DEADBEEFEX]") is None  # too many
    assert opaque_token_entity_type("[EMAIL_ADDRESS_deadbeef]") is None    # lowercase


# ── Per-call use_faker override rejection ──────────────────────────────


async def test_per_call_use_faker_true_override_ignored_when_prewarm_on(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When `DETECTOR_CACHE_PREWARM=true`, a request that arrives with
    `use_faker=true` in additional_provider_specific_params gets the
    override silently dropped (with a WARNING). The surrogate
    generator falls back to config.use_faker=false (which the
    boot validator pinned), so the produced surrogates remain
    opaque-format and the deanonymize-side prewarm stays correct."""
    from anonymizer_guardrail import pipeline as pipeline_mod
    from anonymizer_guardrail import surrogate as surrogate_mod
    from anonymizer_guardrail.api import Overrides
    from anonymizer_guardrail.pipeline import Pipeline

    # Pin config: prewarm on, opaque tokens. Patch both bindings —
    # see `_setup_prewarm_pipeline` for the rationale.
    patched = pipeline_mod.config.model_copy(update={
        "detector_cache_prewarm": True,
        "use_faker": False,
    })
    monkeypatch.setattr(pipeline_mod, "config", patched)
    monkeypatch.setattr(surrogate_mod, "config", patched)

    p = Pipeline()
    overrides = Overrides(use_faker=True)

    with caplog.at_level(logging.WARNING, logger="anonymizer.pipeline"):
        modified, mapping = await p.anonymize(
            ["Email me at alice@corp.example."],
            call_id="prewarm-override-test",
            overrides=overrides,
        )

    # The warning fired.
    assert any(
        "use_faker=true override ignored" in r.message
        for r in caplog.records
    ), f"warning missing; got: {[r.message for r in caplog.records]}"
    # The produced surrogate is opaque-format (config.use_faker=false
    # took effect), not Faker-format.
    surrogate = next(s for s, o in mapping.items() if o == "alice@corp.example")
    assert surrogate.startswith("[") and surrogate.endswith("]"), (
        f"expected opaque-format surrogate, got {surrogate!r}"
    )
    await p.aclose()


# ── Prewarm round-trip ─────────────────────────────────────────────────


def _setup_prewarm_pipeline(monkeypatch, *, prewarm: bool):
    """Shared setup for the round-trip tests. Returns (Pipeline,
    mock_post). Caller drives anonymize / deanonymize calls and
    inspects mock_post.call_count to detect cache hits vs real
    detection."""
    from anonymizer_guardrail import pipeline as pipeline_mod
    from anonymizer_guardrail import surrogate as surrogate_mod
    from anonymizer_guardrail.detector import llm as llm_mod
    from anonymizer_guardrail.pipeline import Pipeline

    patched_config = pipeline_mod.config.model_copy(update={
        "detector_mode": "regex,llm",
        "detector_cache_prewarm": prewarm,
        "use_faker": False,
    })
    # Both modules independently bound `config` at their own import
    # time (`from .config import config`). Patching one wouldn't reach
    # the other — the SurrogateGenerator would still read the
    # original `use_faker=True` and emit Faker surrogates, which the
    # opaque-token extractor can't recover types from. Patch both.
    monkeypatch.setattr(pipeline_mod, "config", patched_config)
    monkeypatch.setattr(surrogate_mod, "config", patched_config)
    monkeypatch.setattr(
        llm_mod, "CONFIG",
        llm_mod.CONFIG.model_copy(update={
            "cache_max_size": 100,
            "max_chars": 10_000,
        }),
    )

    mock_post = AsyncMock()

    def _ok_response(entities):
        import json
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"entities": entities})}}]
        }
        resp.text = ""
        return resp

    mock_post.return_value = _ok_response([
        {"text": "alice@corp.example", "type": "EMAIL_ADDRESS"},
    ])
    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    return Pipeline(), mock_post


async def test_prewarm_populates_cache_on_deanonymize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: deanonymize seeds the per-detector caches for the
    *restored* text. Subsequent anonymize on a DIFFERENT text that
    contains the same entity (mimicking a model response that
    paraphrases instead of echoing) → if the response text appears
    in turn N+1, prewarm makes that a cache hit.

    Test shape: simulate a model that produces a response with new
    framing around the same entity. Deanonymize that response,
    which contains "alice@corp.example" embedded in different
    surrounding text than the user's original message. A future
    anonymize on the response text hits the prewarmed cache.

    Without prewarm, the response text was never seen by the
    detectors → real detection runs → mock_post.call_count
    increases."""
    p, mock_post = _setup_prewarm_pipeline(monkeypatch, prewarm=True)

    # Turn N user → guardrail → anonymize the user message.
    user_text = "Please email alice@corp.example."
    modified, mapping = await p.anonymize([user_text], call_id="t1")
    surrogate = next(s for s, o in mapping.items() if o == "alice@corp.example")

    # Turn N model → guardrail → deanonymize a *different* response
    # text that contains the surrogate (simulating the model
    # paraphrasing the user's input around the entity).
    response_with_surrogate = f"Confirmed contact for {surrogate} on file."
    restored = await p.deanonymize([response_with_surrogate], call_id="t1")
    assert "alice@corp.example" in restored[0]
    # Critical: the restored response text is structurally different
    # from user_text — the LLM cache was NOT populated for it
    # during turn N's anonymize call. Only prewarm could have
    # populated it.
    assert restored[0] != user_text

    pre_count = mock_post.call_count

    # Turn N+1 user → echoes the response text in their next message.
    # Anonymize it. With prewarm, the LLM cache hits; without
    # prewarm, it'd miss and call HTTP.
    await p.anonymize([restored[0]], call_id="t2")

    assert mock_post.call_count == pre_count, (
        f"expected prewarm-induced cache hit on response text — got "
        f"{mock_post.call_count - pre_count} new POST(s)"
    )
    await p.aclose()


async def test_prewarm_does_not_fire_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverse of the previous test: with `DETECTOR_CACHE_PREWARM=false`,
    deanonymize is pure substring-replace, no cache writes. Anonymizing
    the response text (which differs from any text the detectors saw
    on turn N) runs real detection."""
    p, mock_post = _setup_prewarm_pipeline(monkeypatch, prewarm=False)

    user_text = "Please email alice@corp.example."
    modified, mapping = await p.anonymize([user_text], call_id="off-1")
    surrogate = next(s for s, o in mapping.items() if o == "alice@corp.example")

    # Same shape as the prewarm test — response text is structurally
    # different from user_text.
    response_with_surrogate = f"Confirmed contact for {surrogate} on file."
    restored = await p.deanonymize([response_with_surrogate], call_id="off-1")
    assert restored[0] != user_text

    pre_count = mock_post.call_count
    await p.anonymize([restored[0]], call_id="off-2")

    assert mock_post.call_count > pre_count, (
        "expected real detection on response text when prewarm is off"
    )
    await p.aclose()


# ── LLM cache-key parity ───────────────────────────────────────────────


async def test_llm_default_cache_key_strips_model_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`detect()` resolves `effective_model = (model or self.model).strip()
    or self.model`. The prewarm-side `_default_cache_key` MUST mirror
    the same strip — otherwise an operator-misconfigured
    `LLM_MODEL=" anonymize "` writes prewarm to the unstripped slot
    while real `detect()` reads the stripped slot, and the prewarm
    silently never hits.

    This test pins the slot equality by constructing both keys and
    asserting they're identical for a model carrying surrounding
    whitespace."""
    from anonymizer_guardrail.detector.llm import LLMDetector

    det = LLMDetector.__new__(LLMDetector)  # bypass httpx wiring
    det.model = "  llm-anonymize  "

    prewarm_key = det._default_cache_key("hello")

    # Reconstruct what `detect(model=None, prompt_name=None)` would
    # produce — same logic, inlined here so the test pins the parity
    # rather than just calling the same code on both sides.
    effective_model = (None or det.model).strip() or det.model
    detect_key = ("hello", effective_model, "default")

    assert prewarm_key == detect_key, (
        f"slot mismatch: prewarm={prewarm_key!r} detect={detect_key!r}"
    )


# ── Resilience: prewarm failure must not break deanonymize ─────────────


async def test_prewarm_failure_does_not_break_deanonymize(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Prewarm is best-effort: if `_default_cache_key` (or any other
    step in the warm path) raises, deanonymize still returns the
    restored text. The caller already received the substitution
    work — a prewarm failure is purely a missed optimisation, not a
    contract violation."""
    p, mock_post = _setup_prewarm_pipeline(monkeypatch, prewarm=True)

    user_text = "Please email alice@corp.example."
    modified, mapping = await p.anonymize([user_text], call_id="boom-1")
    surrogate = next(s for s, o in mapping.items() if o == "alice@corp.example")

    # Sabotage the LLM detector's cache-key construction. Pipeline
    # iterates self._detectors at deanonymize time and builds tasks
    # for each cache-using detector → at least one task will raise.
    llm_det = next(d for d in p._detectors if d.name == "llm")
    monkeypatch.setattr(
        llm_det, "_default_cache_key",
        lambda text: (_ for _ in ()).throw(RuntimeError("synthetic prewarm failure")),
    )

    response_with_surrogate = f"Confirmed contact for {surrogate} on file."
    with caplog.at_level(logging.ERROR, logger="anonymizer.pipeline"):
        restored = await p.deanonymize([response_with_surrogate], call_id="boom-1")

    # Round-trip survives: the substitution still happened.
    assert "alice@corp.example" in restored[0]
    # And the failure was logged — operator visibility into the missed
    # prewarm without a request-time crash.
    assert any(
        "prewarm failed" in r.message and "detector=llm" in r.message
        for r in caplog.records
    ), f"expected prewarm-failure log; got: {[r.message for r in caplog.records]}"
    await p.aclose()


# ── warm_cache no-op when cache disabled ───────────────────────────────


async def test_warm_cache_no_op_when_cache_disabled() -> None:
    """`LLM_CACHE_MAX_SIZE=0` puts the detector cache in `enabled=False`
    state — get_or_compute becomes a passthrough. `warm_cache` must
    short-circuit before constructing the cache key (which would
    otherwise be wasted work writing to a no-op cache).

    Pinned via a synthetic detector with an in-memory cache built at
    `max_size=0` and a `_default_cache_key` that raises if called —
    asserts warm_cache returns without touching the key path."""
    from anonymizer_guardrail.detector.base import Match
    from anonymizer_guardrail.detector.cache_memory import InMemoryDetectionCache
    from anonymizer_guardrail.detector.remote_base import BaseRemoteDetector

    class _Sentinel(BaseRemoteDetector):
        name = "sentinel"

        def _default_cache_key(self, text: str):
            raise AssertionError(
                "warm_cache must short-circuit on disabled cache "
                "before constructing the key"
            )

    det = _Sentinel.__new__(_Sentinel)  # skip httpx wiring
    det._cache = InMemoryDetectionCache(max_size=0)
    assert det._cache.enabled is False

    # Should not raise.
    await det.warm_cache(
        "some text", [Match(text="x", entity_type="EMAIL_ADDRESS")],
    )
