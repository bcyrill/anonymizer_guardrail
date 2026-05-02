"""Launcher preset loader tests.

Covers the bundled-default + operator-extension contract:

  * Bundled YAML round-trips through the Pydantic schema with no
    surprises (catches silent schema drift if the bundled file
    grows a new field that the model rejects).
  * Operator file extends the set; new names appear, collisions
    replace the bundled entry.
  * Path resolution: --presets-file (via `set_operator_presets_file`)
    wins over LAUNCHER_PRESETS_FILE; both fail loud on missing files.
  * Schema validation rejects bad values with a useful error.

The TUI launcher and the main CLI both consume `load_presets()`
output, so pinning the loader contract here covers both surfaces
without needing UI-level tests.
"""

from __future__ import annotations

import os
from pathlib import Path

# Match other test modules: keep transitive imports harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest


@pytest.fixture(autouse=True)
def _clear_operator_override():
    """Each test starts with no `--presets-file` override and no
    `LAUNCHER_PRESETS_FILE` env var leaking from an earlier case.
    Auto-applied to every test in this file."""
    from tools.launcher.preset_loader import set_operator_presets_file
    set_operator_presets_file(None)
    yield
    set_operator_presets_file(None)


# ── Bundled defaults round-trip ─────────────────────────────────────────


def test_bundled_presets_load() -> None:
    """The bundled YAML must parse cleanly through the Pydantic schema.
    Catches a future field-name typo in `default.yaml` that would
    otherwise only surface when an operator runs `--show-presets`."""
    from tools.launcher.preset_loader import load_presets

    presets = load_presets()
    # The 3 bundled presets shipped with the launcher must be present.
    assert {"uuid-debug", "pentest", "regex-only"}.issubset(presets.keys())
    for lp in presets.values():
        assert lp.source == "bundled"


def test_bundled_pentest_preserves_env_overrides() -> None:
    """Pentest carries non-trivial env_overrides (regex pattern path,
    LLM prompt path). Pin those — the pentest presets is the most
    operator-visible "complicated" preset, so a refactor that drops
    its env_overrides silently would be a real regression."""
    from tools.launcher.preset_loader import load_presets

    pentest = load_presets()["pentest"].spec
    assert pentest.env_overrides["REGEX_PATTERNS_PATH"] == "bundled:regex_pentest.yaml"
    assert pentest.env_overrides["LLM_SYSTEM_PROMPT_PATH"] == "bundled:llm_pentest.md"
    assert pentest.llm_backend == "service"
    assert pentest.pf_backend == "service"


def test_bundled_uuid_debug_does_not_set_pf_backend() -> None:
    """Empty backend fields stay empty after schema parse — they
    don't auto-fill to "service" or anything else. uuid-debug
    auto-starts LLM only; PF stays unset."""
    from tools.launcher.preset_loader import load_presets

    uuid_debug = load_presets()["uuid-debug"].spec
    assert uuid_debug.llm_backend == "service"
    assert uuid_debug.pf_backend == ""
    assert uuid_debug.gliner_backend == ""


# ── Operator extension ─────────────────────────────────────────────────


def test_operator_file_appends_new_preset(tmp_path: Path) -> None:
    """A name not in the bundled set is appended with `source=operator`.
    Iteration order is bundled-first, then operator entries, so
    `--show-presets` table columns stay predictable."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    op_file = tmp_path / "ops.yaml"
    op_file.write_text(
        "presets:\n"
        "  custom-compliance:\n"
        "    detector_mode: regex,denylist\n"
        "    log_level: warning\n"
    )
    set_operator_presets_file(str(op_file))

    presets = load_presets()
    assert "custom-compliance" in presets
    assert presets["custom-compliance"].source == "operator"
    assert presets["custom-compliance"].spec.detector_mode == "regex,denylist"
    # Bundled set is still intact.
    assert "uuid-debug" in presets
    assert presets["uuid-debug"].source == "bundled"


def test_operator_file_replaces_bundled_by_name(tmp_path: Path) -> None:
    """When an operator's preset shares a name with a bundled preset,
    the operator entry replaces the bundled one verbatim — no
    per-field merging. Operator visibility into "did my override
    take effect?" is the load-bearing property."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    op_file = tmp_path / "ops.yaml"
    op_file.write_text(
        "presets:\n"
        "  pentest:\n"
        "    detector_mode: regex\n"           # very different from bundled
        "    log_level: error\n"
        "    use_faker: true\n"
    )
    set_operator_presets_file(str(op_file))

    presets = load_presets()
    pentest = presets["pentest"]
    assert pentest.source == "operator"
    assert pentest.spec.detector_mode == "regex"
    assert pentest.spec.log_level == "error"
    assert pentest.spec.use_faker is True
    # Bundled env_overrides MUST NOT bleed through — full replace.
    assert pentest.spec.env_overrides == {}
    assert pentest.spec.pf_backend == ""


def test_operator_file_via_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """LAUNCHER_PRESETS_FILE is the lower-priority resolution channel
    (CLI flag wins). When only the env var is set, it works alone."""
    from tools.launcher.preset_loader import load_presets

    op_file = tmp_path / "via-env.yaml"
    op_file.write_text(
        "presets:\n"
        "  env-only:\n"
        "    detector_mode: regex\n"
    )
    monkeypatch.setenv("LAUNCHER_PRESETS_FILE", str(op_file))

    presets = load_presets()
    assert "env-only" in presets
    assert presets["env-only"].source == "operator"


def test_cli_flag_overrides_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When both --presets-file and LAUNCHER_PRESETS_FILE are set, the
    CLI flag wins. Same precedence pattern other operator-side knobs
    in the project use (CLI override > env > default)."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    env_file = tmp_path / "env.yaml"
    env_file.write_text(
        "presets:\n  env-only: { detector_mode: regex }\n"
    )
    cli_file = tmp_path / "cli.yaml"
    cli_file.write_text(
        "presets:\n  cli-only: { detector_mode: denylist }\n"
    )
    monkeypatch.setenv("LAUNCHER_PRESETS_FILE", str(env_file))
    set_operator_presets_file(str(cli_file))

    presets = load_presets()
    # CLI wins — env file is ignored entirely.
    assert "cli-only" in presets
    assert "env-only" not in presets


# ── Failure modes ──────────────────────────────────────────────────────


def test_missing_operator_file_raises(tmp_path: Path) -> None:
    """A configured-but-missing operator file is fail-loud — operator
    intent ("use this file") shouldn't silently fall through to
    bundled-only when their file path was wrong."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    set_operator_presets_file(str(tmp_path / "does-not-exist.yaml"))
    with pytest.raises(RuntimeError, match="not found"):
        load_presets()


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    """Malformed YAML produces a clear parse-error. Surfaces with the
    file path so the operator knows where to look."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    op_file = tmp_path / "broken.yaml"
    op_file.write_text("presets: { this is not: valid yaml: at: all }")
    set_operator_presets_file(str(op_file))

    with pytest.raises(RuntimeError, match="YAML parse error"):
        load_presets()


def test_unknown_field_rejected(tmp_path: Path) -> None:
    """`extra="forbid"` on the schema means a typoed field name fails
    loud at YAML load instead of silently doing nothing. Pin that —
    the alternative (silent ignore) is exactly the kind of "I changed
    the YAML and nothing happened" debugging trap operators hate."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    op_file = tmp_path / "typo.yaml"
    op_file.write_text(
        "presets:\n"
        "  bad:\n"
        "    detector_mode: regex\n"
        "    use_facker: true\n"               # typo: facker / faker
    )
    set_operator_presets_file(str(op_file))

    with pytest.raises(RuntimeError, match="schema validation failed"):
        load_presets()


def test_invalid_log_level_rejected(tmp_path: Path) -> None:
    """log_level is a Literal["debug","info","warning","error"]; an
    out-of-set value rejects with a clear Pydantic error."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    op_file = tmp_path / "bad-log.yaml"
    op_file.write_text(
        "presets:\n"
        "  bad:\n"
        "    detector_mode: regex\n"
        "    log_level: SHOUTING\n"
    )
    set_operator_presets_file(str(op_file))

    with pytest.raises(RuntimeError, match="schema validation failed"):
        load_presets()


def test_invalid_backend_value_rejected(tmp_path: Path) -> None:
    """Backend fields accept "service" / "external" / "" only. Other
    values rejected loud."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    op_file = tmp_path / "bad-backend.yaml"
    op_file.write_text(
        "presets:\n"
        "  bad:\n"
        "    detector_mode: regex,llm\n"
        "    llm_backend: turbo\n"             # not a valid backend
    )
    set_operator_presets_file(str(op_file))

    with pytest.raises(RuntimeError, match="schema validation failed"):
        load_presets()


def test_empty_detector_mode_rejected(tmp_path: Path) -> None:
    """detector_mode is required (min_length=1) — a preset with no
    detectors would launch a guardrail that does nothing, which the
    operator probably didn't mean."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    op_file = tmp_path / "empty-mode.yaml"
    op_file.write_text(
        "presets:\n"
        "  bad:\n"
        "    detector_mode: \"\"\n"
    )
    set_operator_presets_file(str(op_file))

    with pytest.raises(RuntimeError, match="schema validation failed"):
        load_presets()


# ── Apply preset (integration with main.py) ────────────────────────────


def test_apply_preset_normalizes_backend_choices() -> None:
    """`_apply_preset` returns the per-detector backend dict the auto-
    start path consumes. Empty-string backend values from the schema
    surface as None in the returned dict so the caller's `if backend`
    check stays clean."""
    from tools.launcher.main import _apply_preset
    from tools.launcher.runner import LaunchConfig

    cfg = LaunchConfig()
    backends = _apply_preset(cfg, "regex-only")
    # regex-only has no backends — all None.
    assert backends == {"llm": None, "privacy_filter": None, "gliner_pii": None}
    assert cfg.detector_mode == "regex"
    assert cfg.use_faker is True
