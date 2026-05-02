"""Failure-mode tests for the anonymization pipeline.

Covers the fail-closed / fail-open contract for each of the three
detectors that have an `unavailable_error` (LLM, privacy_filter,
gliner_pii), plus the TaskGroup cancellation property that locks
down the asyncio.gather → TaskGroup migration.

Helpers come from `_pipeline_helpers.py`.
"""

from __future__ import annotations

import asyncio

import pytest

from anonymizer_guardrail.pipeline import Pipeline
from tests._pipeline_helpers import (
    _gliner_detector_that_raises,
    _llm_detector_that_raises,
    _patch_fail_closed,
    _patch_gliner_pii_fail_closed,
    _patch_pf_fail_closed,
    _pf_detector_that_raises,
)


# ── LLM detector ────────────────────────────────────────────────────────


async def test_unexpected_llm_failure_routes_through_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under LLM_FAIL_CLOSED, any failure inside the LLM detector — even an
    unexpected one that isn't LLMUnavailableError — must propagate so the
    guardrail BLOCKs the request instead of silently returning empty
    matches. Without this, a programmer error in the LLM path would let
    unredacted text reach the upstream model in fail-closed mode."""
    from anonymizer_guardrail.detector.llm import LLMUnavailableError

    _patch_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_llm_detector_that_raises(
        lambda: RuntimeError("unexpected non-LLMUnavailable explosion")
    )]

    with pytest.raises(LLMUnavailableError, match="Unexpected failure"):
        await p.anonymize(["alice"], call_id="unexpected-fail")


async def test_unexpected_llm_failure_swallowed_under_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fail_open=true → LLM exceptions log and fall through to whatever
    the regex layer produced. The text proceeds, no BLOCK."""
    _patch_fail_closed(monkeypatch, False)

    p = Pipeline()
    p._detectors = [_llm_detector_that_raises(lambda: RuntimeError("kaboom"))]

    # With no other detectors and the LLM swallowed, nothing gets
    # anonymized — the text passes through unchanged.
    modified, mapping = await p.anonymize(
        ["alice met bob"], call_id="failopen-test"
    )
    assert modified == ["alice met bob"]
    assert mapping == {}


async def test_batch_cancels_sibling_texts_on_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one text in a multi-text batch trips fail-closed, the
    sibling texts' in-flight detector calls must be cancelled — not
    left to burn compute and pin concurrency slots until they
    complete naturally. Locks down the TaskGroup-vs-gather choice in
    Pipeline.anonymize.

    The check that catches a regression is `completed == 0` after the
    sibling tasks would have had time to finish:

      * TaskGroup → siblings cancelled at their `await`, never reach
        the `completed += 1` line; counter stays 0 forever.
      * asyncio.gather → exception propagates immediately, but
        siblings keep running detached; after their sleep elapses
        they reach the increment and the counter goes nonzero.

    The 0.5 s sleep + 1.0 s settle window is the discriminator.
    """
    from anonymizer_guardrail.detector.llm import LLMDetector, LLMUnavailableError

    _patch_fail_closed(monkeypatch, True)

    started = 0
    completed = 0

    bad = LLMDetector.__new__(LLMDetector)
    bad.name = "llm"

    async def detect(text, *_args, **_kwargs):
        nonlocal started, completed
        started += 1
        if text == "explode":
            # Yield once so the sibling tasks get a chance to start
            # their sleep — otherwise the cancellation case has nothing
            # to cancel and the test doesn't exercise the property.
            await asyncio.sleep(0)
            raise LLMUnavailableError("forced")
        await asyncio.sleep(0.5)
        completed += 1
        return []

    bad.detect = detect  # type: ignore[method-assign]
    p = Pipeline()
    p._detectors = [bad]

    with pytest.raises(LLMUnavailableError, match="forced"):
        await p.anonymize(
            ["slow1", "explode", "slow2"], call_id="cancel-test",
        )

    # Settle: long enough that detached gather-style siblings would
    # have finished their 0.5 s sleep and bumped `completed`.
    await asyncio.sleep(1.0)
    assert completed == 0, (
        f"slow detector tasks completed despite sibling failure "
        f"(started={started}, completed={completed}); "
        f"sibling cancellation is broken — Pipeline.anonymize is "
        f"probably back on asyncio.gather instead of TaskGroup."
    )


# ── Privacy-filter detector ─────────────────────────────────────────────


async def test_privacy_filter_unavailable_propagates_under_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRIVACY_FILTER_FAIL_CLOSED=true → a PrivacyFilterUnavailableError
    inside the PF detector propagates so main.py returns BLOCKED. Without
    this, an outage of the privacy-filter service would silently drop PF
    coverage even though the operator explicitly chose fail-closed."""
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        PrivacyFilterUnavailableError,
    )

    _patch_pf_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_pf_detector_that_raises(
        lambda: PrivacyFilterUnavailableError("service unreachable")
    )]

    with pytest.raises(PrivacyFilterUnavailableError, match="service unreachable"):
        await p.anonymize(["alice"], call_id="pf-fail-closed")


async def test_privacy_filter_unavailable_swallowed_under_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRIVACY_FILTER_FAIL_CLOSED=false → log + degrade to no PF matches.
    The request still goes through (covered by other detectors)."""
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        PrivacyFilterUnavailableError,
    )

    _patch_pf_fail_closed(monkeypatch, False)

    p = Pipeline()
    p._detectors = [_pf_detector_that_raises(
        lambda: PrivacyFilterUnavailableError("service unreachable")
    )]

    # No exception, no matches — the only detector returned [] for this call.
    modified, mapping = await p.anonymize(["alice"], call_id="pf-fail-open")
    assert mapping == {}
    assert modified == ["alice"]


async def test_unexpected_pf_failure_routes_through_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-PrivacyFilterUnavailable crash inside a PF detector must
    still route through PRIVACY_FILTER_FAIL_CLOSED — same asymmetry the
    LLM detector has. Otherwise a programmer error in the PF path
    (torch OOM, tokenizer drift, etc.) would silently drop coverage in
    fail-closed mode."""
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        PrivacyFilterUnavailableError,
    )

    _patch_pf_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_pf_detector_that_raises(
        lambda: RuntimeError("unexpected non-PrivacyFilterUnavailable explosion")
    )]

    with pytest.raises(PrivacyFilterUnavailableError, match="Unexpected failure"):
        await p.anonymize(["alice"], call_id="pf-unexpected-fail")


async def test_pf_and_llm_fail_modes_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators must be able to fail closed on one detector and open
    on the other. Pin that down: PF closed + LLM open → PF errors
    propagate, LLM errors get swallowed."""
    from anonymizer_guardrail.detector.llm import LLMUnavailableError
    from anonymizer_guardrail.detector.remote_privacy_filter import (
        PrivacyFilterUnavailableError,
    )

    # Each per-detector CONFIG is independent now, so the two patches
    # are simply applied in sequence to their respective modules.
    _patch_fail_closed(monkeypatch, False)        # LLM open
    _patch_pf_fail_closed(monkeypatch, True)      # PF closed

    # PF detector raising → propagates (PF is fail-closed).
    p = Pipeline()
    p._detectors = [_pf_detector_that_raises(
        lambda: PrivacyFilterUnavailableError("pf service unreachable")
    )]
    with pytest.raises(PrivacyFilterUnavailableError):
        await p.anonymize(["alice"], call_id="independent-pf")

    # LLM detector raising → swallowed (LLM is fail-open).
    p = Pipeline()
    p._detectors = [_llm_detector_that_raises(
        lambda: LLMUnavailableError("llm api unreachable")
    )]
    modified, mapping = await p.anonymize(["alice"], call_id="independent-llm")
    assert mapping == {}
    assert modified == ["alice"]


# ── GLiNER-PII detector ─────────────────────────────────────────────────


async def test_gliner_pii_unavailable_propagates_under_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GLINER_PII_FAIL_CLOSED=true → a GlinerPIIUnavailableError inside
    the detector propagates so main.py returns BLOCKED. Same risk shape
    as the PF and LLM detectors — silent coverage downgrade is exactly
    what fail-closed exists to prevent."""
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        GlinerPIIUnavailableError,
    )

    _patch_gliner_pii_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_gliner_detector_that_raises(
        lambda: GlinerPIIUnavailableError("service unreachable")
    )]

    with pytest.raises(GlinerPIIUnavailableError, match="service unreachable"):
        await p.anonymize(["alice"], call_id="gliner-fail-closed")


async def test_gliner_pii_unavailable_swallowed_under_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GLINER_PII_FAIL_CLOSED=false → log + degrade to no gliner matches.
    The request still goes through (covered by other detectors)."""
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        GlinerPIIUnavailableError,
    )

    _patch_gliner_pii_fail_closed(monkeypatch, False)

    p = Pipeline()
    p._detectors = [_gliner_detector_that_raises(
        lambda: GlinerPIIUnavailableError("service unreachable")
    )]

    modified, mapping = await p.anonymize(["alice"], call_id="gliner-fail-open")
    assert mapping == {}
    assert modified == ["alice"]


async def test_unexpected_gliner_pii_failure_routes_through_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-GlinerPIIUnavailable crash inside the detector must still
    route through GLINER_PII_FAIL_CLOSED — same asymmetry the LLM and
    PF detectors have. Otherwise a programmer error would silently
    drop coverage in fail-closed mode."""
    from anonymizer_guardrail.detector.remote_gliner_pii import (
        GlinerPIIUnavailableError,
    )

    _patch_gliner_pii_fail_closed(monkeypatch, True)

    p = Pipeline()
    p._detectors = [_gliner_detector_that_raises(
        lambda: RuntimeError("unexpected explosion")
    )]

    with pytest.raises(GlinerPIIUnavailableError, match="Unexpected failure"):
        await p.anonymize(["alice"], call_id="gliner-unexpected-fail")


# ── Vault-backend failure ───────────────────────────────────────────────


async def test_vault_put_failure_propagates_blocks_request() -> None:
    """Critical correctness invariant: if `vault.put` raises (e.g.
    Redis unreachable), `Pipeline.anonymize` MUST propagate the error
    so the caller returns BLOCKED. Returning the modified texts
    without storing the mapping would ship surrogates that nobody
    can deanonymize on the response side — silent surrogate leakage
    to the user.

    Pinned here as a Pipeline-level invariant rather than a
    detector-failure test because it's about state-storage
    correctness, not detection availability."""
    from anonymizer_guardrail.vault_redis import RedisVaultError

    p = Pipeline()

    # Replace the vault with a stub whose put raises the typed error.
    class _FailingVault:
        async def put(self, call_id, mapping):
            raise RedisVaultError("simulated Redis outage")

        async def pop(self, call_id):
            return {}

        def size(self) -> int:
            return 0

        async def aclose(self) -> None:
            return None

    p._vault = _FailingVault()  # type: ignore[assignment]

    # Use a text that triggers regex matches so anonymize actually
    # has a non-empty mapping to store. Without matches, the vault
    # path is skipped entirely (mapping is empty).
    with pytest.raises(RedisVaultError, match="simulated Redis outage"):
        await p.anonymize(
            ["email me at alice@example.com"],
            call_id="vault-put-fails",
        )


async def test_pipeline_aclose_invokes_vault_aclose() -> None:
    """`Pipeline.aclose()` must call `vault.aclose()` so RedisVault's
    connection pool drains on FastAPI shutdown. Pin the wiring so a
    future refactor can't silently drop the call."""
    p = Pipeline()

    aclose_called = False

    class _TrackingVault:
        async def put(self, call_id, mapping): pass

        async def pop(self, call_id): return {}

        def size(self) -> int: return 0

        async def aclose(self) -> None:
            nonlocal aclose_called
            aclose_called = True

    p._vault = _TrackingVault()  # type: ignore[assignment]

    await p.aclose()
    assert aclose_called, "Pipeline.aclose() did not invoke vault.aclose()"


async def test_pipeline_aclose_times_out_on_hung_vault(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Pipeline.aclose()` wraps each backend's `aclose` in
    `asyncio.wait_for(timeout=_ACLOSE_TIMEOUT_S)` so a wedged Redis
    can't hang FastAPI shutdown indefinitely. Stub a vault whose
    `aclose` blocks forever, lower the timeout to keep the test fast,
    and verify Pipeline.aclose returns within the budget AND logs
    a WARNING naming the vault.

    Pins the contract that a misbehaving backend produces a clean
    timeout-and-warn at shutdown rather than a hang."""
    import logging
    from anonymizer_guardrail import pipeline as pipeline_mod

    # Lower the budget so a real test doesn't sit for 5 seconds.
    monkeypatch.setattr(pipeline_mod, "_ACLOSE_TIMEOUT_S", 0.1)

    p = Pipeline()

    class _HungVault:
        async def put(self, call_id, mapping): pass

        async def pop(self, call_id): return {}

        def size(self) -> int: return 0

        async def aclose(self) -> None:
            # Block effectively forever — wait_for must cut us off.
            await asyncio.sleep(60)

    p._vault = _HungVault()  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="anonymizer.pipeline"):
        await p.aclose()  # must return cleanly, NOT hang

    assert any(
        "vault aclose timed out" in r.message.lower()
        for r in caplog.records
    ), (
        "expected a WARNING naming the timed-out vault aclose; got: "
        f"{[r.message for r in caplog.records]}"
    )
