"""Shared pytest fixtures.

The `pipeline` fixture is consumed by the four `test_pipeline_*.py`
files (split out of the original monolithic test_pipeline.py — see
that file's git history for the change). Putting it here means the
split files don't have to redefine it.

DETECTOR_MODE is forced to `regex` at module import so the Pipeline
constructed by the fixture doesn't try to spin up an LLM detector
that requires a reachable backend in tests.
"""

from __future__ import annotations

import os

# Force regex-only so tests don't need an LLM. Must be set before
# importing anything that reads config — pytest imports conftest.py
# first, so this runs before any test module's own imports.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from anonymizer_guardrail.pipeline import Pipeline


@pytest.fixture
def pipeline() -> Pipeline:
    return Pipeline()
