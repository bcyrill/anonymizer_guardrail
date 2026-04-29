"""Tests for the Authorization-header forwarding path."""

from __future__ import annotations

import os

# Keep config harmless for transitive imports — same pattern as the other tests.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.main import _forwarded_bearer


def test_extracts_bearer_token() -> None:
    assert _forwarded_bearer({"authorization": "Bearer sk-abc123"}) == "sk-abc123"


def test_header_lookup_is_case_insensitive() -> None:
    assert _forwarded_bearer({"Authorization": "Bearer sk-x"}) == "sk-x"
    assert _forwarded_bearer({"AUTHORIZATION": "Bearer sk-y"}) == "sk-y"


def test_bearer_keyword_is_case_insensitive() -> None:
    assert _forwarded_bearer({"authorization": "bearer sk-x"}) == "sk-x"
    assert _forwarded_bearer({"authorization": "BEARER sk-y"}) == "sk-y"


def test_redaction_marker_is_treated_as_missing() -> None:
    """LiteLLM sends '[present]' when the header is NOT on the allowlist —
    using that as a bearer would be a real bug."""
    assert _forwarded_bearer({"authorization": "[present]"}) is None


def test_missing_header_returns_none() -> None:
    assert _forwarded_bearer({}) is None
    assert _forwarded_bearer({"x-other": "value"}) is None


def test_non_bearer_scheme_returns_none() -> None:
    """We only know how to extract Bearer; Basic/etc. fall back to LLM_API_KEY."""
    assert _forwarded_bearer({"authorization": "Basic dXNlcjpwYXNz"}) is None


def test_empty_bearer_returns_none() -> None:
    assert _forwarded_bearer({"authorization": "Bearer "}) is None
    assert _forwarded_bearer({"authorization": "Bearer    "}) is None