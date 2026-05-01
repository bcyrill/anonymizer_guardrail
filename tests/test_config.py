"""Tests for the env-var parsing helpers in `config.py`.

The truthy parser is shared by every detector's `*_FAIL_CLOSED` flag and
by `USE_FAKER`. A regression here would silently flip safety defaults —
e.g. Python's `bool("FALSE")` is True — so the contract is locked down
explicitly.
"""

from __future__ import annotations

import os

# Match the other test files: keep transitive config harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.config import _env_bool


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
def test_env_bool_parses_truthy_strings(
    raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANONYMIZER_TEST_FLAG", raw)
    assert _env_bool("ANONYMIZER_TEST_FLAG", default=True) is expected


def test_env_bool_returns_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANONYMIZER_TEST_FLAG", raising=False)
    assert _env_bool("ANONYMIZER_TEST_FLAG", default=True) is True
    assert _env_bool("ANONYMIZER_TEST_FLAG", default=False) is False
