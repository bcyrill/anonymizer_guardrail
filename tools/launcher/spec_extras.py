"""Launcher-side metadata: re-exports + cross-cutting constants.

Per-detector launcher metadata (`LAUNCHER_METADATA`) and the
`LauncherSpec` / `ServiceSpec` dataclasses now live alongside each
detector inside the production package
(`anonymizer_guardrail.detector.*`), so adding a new detector is a
single-file edit instead of a paired edit between the detector module
and a separate launcher table. The dataclasses are pure stdlib (no
`click` / `textual` / `rich`), so shipping them in the wheel costs
~hundred lines of pure Python — cheap relative to the maintenance win.

This module re-exports them so existing launcher code keeps importing
from `tools.launcher.spec_extras`. New code in the launcher can import
straight from `anonymizer_guardrail.detector` if preferred.

What stays here:

  * `SHARED_NETWORK`, `CONTAINER_NAME_DEFAULT` — cross-cutting launcher
    constants that aren't per-detector.
  * `get_image_tag` — `os.environ`-touching helper for image-tag
    resolution. Launcher-runtime concern, no place in the prod wheel.
"""

from __future__ import annotations

from anonymizer_guardrail.detector import (
    LAUNCHER_METADATA,
    LauncherSpec,
    ServiceSpec,
)


# ── Cross-cutting constants ───────────────────────────────────────────────
# Shared with the bash wrapper-style invocation contract — change here,
# the launcher follows.

SHARED_NETWORK = "anonymizer-net"
"""Docker/Podman network all auto-started services join so the guardrail
can dial them by container name."""

CONTAINER_NAME_DEFAULT = "anonymizer-guardrail"
"""Default name for the guardrail container itself. Operator can override
via `--name`."""


def get_image_tag(envs: tuple[str, ...], defaults: tuple[str, ...]) -> tuple[str, str | None]:
    """Pick the operator-resolved image tag for a service.

    Returns (image_tag, kind) where `kind` is the env-var name that
    was used (for "we're using the baked variant" log lines).
    Falls back to the first default when no env vars are set —
    callers then check whether the image actually exists locally
    via engine.image_exists.
    """
    import os

    for env_name, default in zip(envs, defaults):
        val = os.environ.get(env_name)
        if val:
            return val, env_name
    # All env vars unset → return the first default.
    return defaults[0], envs[0] if envs else None


__all__ = [
    "LAUNCHER_METADATA",
    "LauncherSpec",
    "ServiceSpec",
    "SHARED_NETWORK",
    "CONTAINER_NAME_DEFAULT",
    "get_image_tag",
]
