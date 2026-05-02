"""Tests for the central pydantic-settings Config.

Replaces the hand-rolled `_env_bool` / `_env_int` parsers that lived
here pre-migration. The win documented in this file is the one the
migration was for: malformed env values crash at import time with a
clear ValidationError, instead of silently falling back to defaults.
"""

from __future__ import annotations

import os

# Match the other test files: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest
from pydantic import ValidationError

from anonymizer_guardrail.config import Config


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
    ],
)
def test_use_faker_parses_truthy_strings(
    raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pydantic-settings handles the bool parsing now. Lock the truthy/
    falsy set down so a future Pydantic version that narrows it (say,
    drops "yes"/"no") is caught by tests rather than silently flipping
    safety defaults in production."""
    monkeypatch.setenv("USE_FAKER", raw)
    cfg = Config()
    assert cfg.use_faker is expected


def test_use_faker_default_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("USE_FAKER", raising=False)
    assert Config().use_faker is True


def test_invalid_bool_crashes_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """The win the pydantic-settings migration is for: a bogus bool
    used to silently fall back to the default. Now it crashes at
    instantiation so the operator sees the typo immediately."""
    monkeypatch.setenv("USE_FAKER", "garbage")
    with pytest.raises(ValidationError):
        Config()


def test_invalid_port_crashes_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same win for ints. Pre-migration `_env_int("PORT", 8000)` would
    silently swallow `PORT=abc` and serve on 8000 — exactly the kind
    of misconfiguration that should crash boot, not 30 minutes later
    when the operator notices the wrong port."""
    monkeypatch.setenv("PORT", "not-a-number")
    with pytest.raises(ValidationError):
        Config()


def test_valid_port_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9090")
    assert Config().port == 9090


def test_detector_mode_lowercased(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator-typed env var like `DETECTOR_MODE=Regex,LLM` should be
    folded for /health readability and downstream parsing."""
    monkeypatch.setenv("DETECTOR_MODE", "Regex,LLM")
    assert Config().detector_mode == "regex,llm"


def test_redis_backend_without_url_fails_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`VAULT_BACKEND=redis` requires `VAULT_REDIS_URL`. The model
    validator catches the misconfiguration at construction so the
    process refuses to start rather than failing at first request.
    Pins the cross-field invariant in `config.py:_vault_redis_url_required_for_redis_backend`."""
    from pydantic import ValidationError

    monkeypatch.setenv("VAULT_BACKEND", "redis")
    monkeypatch.setenv("VAULT_REDIS_URL", "")  # explicit empty
    with pytest.raises(ValidationError, match="VAULT_REDIS_URL"):
        Config()


def test_redis_backend_with_url_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_BACKEND", "redis")
    monkeypatch.setenv("VAULT_REDIS_URL", "redis://localhost:6379/0")
    cfg = Config()
    assert cfg.vault_backend == "redis"
    assert cfg.vault_redis_url == "redis://localhost:6379/0"


def test_memory_backend_does_not_require_url() -> None:
    """Defaults / explicit memory backend should construct fine with
    an empty URL — only the `redis` branch demands it."""
    cfg = Config(vault_backend="memory", vault_redis_url="")
    assert cfg.vault_backend == "memory"
