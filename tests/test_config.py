"""Tests for the configuration module — backward-compat fallbacks and
env-var resolution rules that have to keep working under deprecation.
"""

from __future__ import annotations

import logging
import os

# Match the other test files: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.config import _llm_fail_closed_default


# ── LLM_FAIL_CLOSED / FAIL_CLOSED deprecation shim ─────────────────────────
# `FAIL_CLOSED` was renamed to `LLM_FAIL_CLOSED` once the privacy-filter
# detector got its own fail-mode flag — the old name was misleading. The
# soft-rename keeps `FAIL_CLOSED` working for one release cycle with a
# deprecation warning. These tests pin down both the precedence rules
# and the warning so a future refactor doesn't quietly drop either.


def test_llm_fail_closed_uses_new_name_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_FAIL_CLOSED", "false")
    monkeypatch.delenv("FAIL_CLOSED", raising=False)
    assert _llm_fail_closed_default() is False


def test_llm_fail_closed_falls_back_to_legacy_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operators on the old name keep working but get a loud rename
    hint — the whole point of the soft rename is that the migration is
    impossible to miss."""
    monkeypatch.delenv("LLM_FAIL_CLOSED", raising=False)
    monkeypatch.setenv("FAIL_CLOSED", "false")

    with caplog.at_level(logging.WARNING, logger="anonymizer.config"):
        assert _llm_fail_closed_default() is False

    msgs = " | ".join(r.message for r in caplog.records)
    assert "FAIL_CLOSED is deprecated" in msgs
    assert "LLM_FAIL_CLOSED" in msgs


def test_llm_fail_closed_new_name_wins_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When both names are set, the new one wins and no deprecation
    warning fires (the operator already migrated; the legacy var being
    set is presumably leftover from their previous deployment)."""
    monkeypatch.setenv("LLM_FAIL_CLOSED", "true")
    monkeypatch.setenv("FAIL_CLOSED", "false")  # would say "fail-open" if used

    with caplog.at_level(logging.WARNING, logger="anonymizer.config"):
        assert _llm_fail_closed_default() is True

    assert not any(
        "FAIL_CLOSED is deprecated" in r.message for r in caplog.records
    ), "deprecation warning should not fire when the new name is set"


def test_llm_fail_closed_defaults_to_true_when_neither_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default is fail-closed — the safer choice, matching the LLM
    detector's historical behaviour and what the README documents."""
    monkeypatch.delenv("LLM_FAIL_CLOSED", raising=False)
    monkeypatch.delenv("FAIL_CLOSED", raising=False)
    assert _llm_fail_closed_default() is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true",  True),
        ("True",  True),
        ("1",     True),
        ("yes",   True),
        ("on",    True),
        ("false", False),
        ("False", False),
        ("0",     False),
        ("no",    False),
        ("off",   False),
        ("",      False),  # empty string is falsy in our parser
        ("garbage", False),  # anything not in the truthy set → False
    ],
)
def test_llm_fail_closed_parses_truthy_strings(
    raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same truthy parser as `_env_bool`. Locks down the contract so a
    future refactor doesn't silently start treating `"FALSE"` as `True`
    (Python's `bool("FALSE")` is True — exactly the kind of bug the
    explicit truthy set guards against)."""
    monkeypatch.setenv("LLM_FAIL_CLOSED", raw)
    monkeypatch.delenv("FAIL_CLOSED", raising=False)
    assert _llm_fail_closed_default() is expected
