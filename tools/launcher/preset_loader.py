"""Launcher preset loader: bundled defaults + optional operator file.

Replaces the hardcoded `_PRESETS` dict that used to live in `main.py`.
The user-facing surface is a single function — `load_presets()` — that
returns a name → `LauncherPreset` mapping the CLI / TUI / `--show-presets`
all read from. Same shape the operator-facing knobs (env-prefix /
registry pattern) use elsewhere in the project, so adding a custom
preset is an out-of-tree edit.

# Resolution order

  1. Bundled defaults from `tools/launcher/presets/default.yaml`. Always
     loaded — they're shipped with the launcher source. A malformed
     bundled file is a programmer-error crash, not an operator concern.
  2. Operator file (optional). Path resolved from, in priority order:
     * `--presets-file PATH` CLI flag (a Click eager callback in
       `main.py` reads the value into `_OPERATOR_PRESETS_FILE_OVERRIDE`
       below before `load_presets()` is called)
     * `LAUNCHER_PRESETS_FILE` env var
     * (none) → only bundled presets are exposed.

# Merge semantics

Operator entries with names that collide with a bundled preset
*replace* the bundled entry verbatim — no per-field merging. Partial
overrides would invite confusion ("did my operator file's missing
log_level inherit from bundled or fall back to the global default?")
that's not worth the convenience. New names are appended.

# Schema

`LauncherPreset` is a `pydantic.BaseModel` with `extra="forbid"` so a
typo'd field surfaces at YAML load with a clear error, not as silent
behaviour drift. Field names match LaunchConfig's where they're a
direct copy (`flavour`, `detector_mode`, …); the per-detector backend
choices use clean names (`llm_backend`, `pf_backend`, `gliner_backend`)
without the underscore prefix the old hardcoded dict used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


# Path to the bundled default presets. Relative to this module so the
# launcher works from any CWD.
_BUNDLED_PRESETS_PATH = Path(__file__).parent / "presets" / "default.yaml"


# ── Schema ──────────────────────────────────────────────────────────────


class LauncherPreset(BaseModel):
    """Validated shape of one preset entry. Mirrors the operator-facing
    fields a preset can populate on a `LaunchConfig`. `extra="forbid"`
    so a typoed key fails loud at YAML load."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flavour: str = "default"
    detector_mode: str = Field(min_length=1)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    use_faker: bool = False

    # Per-detector backend selection. Empty / unset = the preset
    # doesn't pre-pick this detector's backend (operator must supply
    # it via flag or it's left at default). "service" = auto-start
    # the bundled service container. "external" = operator supplies
    # the URL. Same vocabulary the CLI's `--<det>-backend` flag accepts.
    llm_backend: Literal["service", "external", ""] = ""
    pf_backend: Literal["service", "external", ""] = ""
    gliner_backend: Literal["service", "external", ""] = ""

    # Per-side Redis backend selection. The launcher runs at most one
    # shared `anonymizer-redis` container even when both sides pick
    # service (each side gets its own logical DB index). External =
    # operator supplies the matching URL via env or CLI.
    #
    #   vault_redis_backend  → drives VAULT_BACKEND=redis routing
    #   cache_redis_backend  → drives `<DETECTOR>_CACHE_BACKEND=redis` routing
    #
    # Empty on either field means "this side stays on its memory
    # backend"; explicit env_overrides like `VAULT_BACKEND: redis` are
    # what *select* redis for that side, the *_redis_backend field
    # then picks where the redis comes from.
    vault_redis_backend: Literal["service", "external", ""] = ""
    cache_redis_backend: Literal["service", "external", ""] = ""

    # Per-detector service-variant selection. Mirrors LaunchConfig's
    # `service_variants` dict — keys are detector names, values are
    # variant names declared in the matching `LauncherSpec.service_variants`.
    # Today only `privacy_filter` has variants (default opf-only or
    # `hf` for the HF-pipeline build). Unknown variants fall back to
    # the default at `resolve_service()` time, but operators expect
    # the named variant to actually fire — typos surface as silent
    # default-fallback otherwise.
    service_variants: dict[str, str] = Field(default_factory=dict)

    # Env vars to set on the guardrail container. Merged into
    # `cfg.env_overrides` AFTER the per-field copy above, BEFORE
    # explicit CLI flags. Use this for prompt/pattern path overrides
    # the preset wants to pre-set.
    env_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("detector_mode", mode="after")
    @classmethod
    def _validate_detector_mode(cls, v: str) -> str:
        # The pipeline lower-cases internally, but normalising at load
        # time keeps `--show-presets` output predictable regardless of
        # how the YAML author capitalised the value.
        return v.strip().lower()


class _PresetsDocument(BaseModel):
    """Top-level YAML shape: `presets: {name: LauncherPreset}`. Wrapper
    exists so a future schema addition (versioning, defaults block,
    etc.) doesn't break operator files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    presets: dict[str, LauncherPreset] = Field(default_factory=dict)


# ── Resolution ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LoadedPreset:
    """One preset plus the source it came from. The source is
    operator-facing — `--show-presets` surfaces it so an operator can
    tell at a glance whether they're looking at a bundled default or
    one their own file replaced."""

    spec: LauncherPreset
    source: Literal["bundled", "operator"]


# Set by main.py's `--presets-file` eager callback before load_presets()
# is invoked. None = use the env var; empty string = same as None
# (treat unset and empty consistently).
_OPERATOR_PRESETS_FILE_OVERRIDE: str | None = None


def set_operator_presets_file(path: str | None) -> None:
    """Store the `--presets-file` CLI value so `load_presets()` picks
    it up. Called from a Click eager callback in main.py before any
    other preset code runs. Passing None clears the override (so the
    env var takes over)."""
    global _OPERATOR_PRESETS_FILE_OVERRIDE
    _OPERATOR_PRESETS_FILE_OVERRIDE = path or None


def _resolve_operator_presets_file() -> str | None:
    """Return the operator file path or None if no operator file is
    configured. CLI flag wins over env var."""
    if _OPERATOR_PRESETS_FILE_OVERRIDE:
        return _OPERATOR_PRESETS_FILE_OVERRIDE
    val = os.environ.get("LAUNCHER_PRESETS_FILE", "").strip()
    return val or None


def _load_yaml(path: Path, label: str) -> _PresetsDocument:
    """Read + parse one presets YAML. `label` names the source for
    error messages (e.g. `LAUNCHER_PRESETS_FILE=…` or
    `bundled tools/launcher/presets/default.yaml`) so the operator
    knows which file to fix."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{label}: file not found at {path}. "
            f"Either remove the override or point at an existing file."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"{label}: cannot read {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"{label}: YAML parse error: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(
            f"{label}: top-level must be a mapping (got "
            f"{type(raw).__name__}). Expected `presets: {{...}}`."
        )

    try:
        return _PresetsDocument.model_validate(raw)
    except Exception as exc:
        # Pydantic's ValidationError already names the offending
        # field path; wrapping with the label keeps "which file" in
        # the operator's view.
        raise RuntimeError(f"{label}: schema validation failed: {exc}") from exc


def load_presets() -> dict[str, LoadedPreset]:
    """Return the merged preset registry: bundled defaults plus any
    operator entries from `--presets-file` / `LAUNCHER_PRESETS_FILE`.

    Operator entries with names that collide with a bundled preset
    *replace* the bundled entry — no per-field merging. New names are
    appended. Iteration order is bundled-first, then operator-only;
    the CLI's `--show-presets` table renders columns in this order.
    """
    bundled_doc = _load_yaml(
        _BUNDLED_PRESETS_PATH,
        f"bundled {_BUNDLED_PRESETS_PATH.name}",
    )
    out: dict[str, LoadedPreset] = {
        name: LoadedPreset(spec=spec, source="bundled")
        for name, spec in bundled_doc.presets.items()
    }

    operator_path = _resolve_operator_presets_file()
    if operator_path:
        op_path = Path(operator_path).expanduser()
        # Resolve relative paths against CWD (same convention as every
        # other operator-supplied path knob in the project).
        if not op_path.is_absolute():
            op_path = Path.cwd() / op_path
        op_doc = _load_yaml(op_path, f"LAUNCHER_PRESETS_FILE={operator_path}")
        for name, spec in op_doc.presets.items():
            out[name] = LoadedPreset(spec=spec, source="operator")

    return out


__all__ = [
    "LauncherPreset",
    "LoadedPreset",
    "load_presets",
    "set_operator_presets_file",
]
