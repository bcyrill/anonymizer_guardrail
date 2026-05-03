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
    # The 13 bundled presets shipped with the launcher today.
    expected = {
        "regex-default",
        "regex-pentest",
        "regex-debug",
        "regex-llm-debug",
        "llm-pentest",
        "privacy-filter-service",
        "gliner-pii-service",
        "gliner-pii-minimal",
        "gliner-pii-financial",
        "gliner-pii-healthcare",
        "vault-redis",
        "pf-cache-redis",
        "regex-pentest-gliner-pii-service",
    }
    assert expected.issubset(presets.keys())
    for lp in presets.values():
        assert lp.source == "bundled"


def test_bundled_regex_pentest_pins_pattern_path() -> None:
    """The regex-pentest preset switches REGEX_PATTERNS_PATH to the
    pentest set via env_overrides. Pin so a refactor that drops the
    pattern-path override silently regresses to the default set."""
    from tools.launcher.preset_loader import load_presets

    pentest = load_presets()["regex-pentest"].spec
    assert pentest.env_overrides["REGEX_PATTERNS_PATH"] == "bundled:regex_pentest.yaml"
    assert pentest.env_overrides["REGEX_OVERLAP_STRATEGY"] == "longest"
    assert pentest.detector_mode == "regex"
    assert pentest.use_faker is False


def test_bundled_privacy_filter_service_pins_hf_variant() -> None:
    """The privacy-filter-service preset must select the `hf` service
    variant — that's the operator-facing differentiator. A refactor
    that drops `service_variants` would silently start the opf-only
    privacy-filter-service:8001 instead of privacy-filter-hf-service:8003,
    which is exactly the bug this preset exists to prevent."""
    from tools.launcher.preset_loader import load_presets

    pf = load_presets()["privacy-filter-service"].spec
    assert pf.detector_mode == "privacy_filter"
    assert pf.pf_backend == "service"
    assert pf.service_variants == {"privacy_filter": "hf"}


def test_bundled_regex_default_does_not_auto_start_anything() -> None:
    """Pure-regex presets must not pre-pick any backend — there's
    nothing to auto-start. Pin so a future contributor copy-pasting
    the privacy-filter-service preset doesn't accidentally inherit
    `pf_backend: service`."""
    from tools.launcher.preset_loader import load_presets

    rd = load_presets()["regex-default"].spec
    assert rd.llm_backend == ""
    assert rd.pf_backend == ""
    assert rd.gliner_backend == ""
    assert rd.service_variants == {}


def test_bundled_gliner_label_overrides_carry_through() -> None:
    """The narrowed-label gliner presets (`gliner-pii-minimal`,
    `gliner-pii-financial`, `gliner-pii-healthcare`) restrict the
    model's vocabulary via `GLINER_PII_LABELS` in env_overrides.
    Pin so a refactor that drops the env_overrides silently regresses
    these presets to the broad default label set — operator who
    picked `gliner-pii-financial` would silently start getting
    person/email/phone hits they didn't ask for."""
    from tools.launcher.preset_loader import load_presets

    presets = load_presets()
    minimal = presets["gliner-pii-minimal"].spec
    financial = presets["gliner-pii-financial"].spec
    healthcare = presets["gliner-pii-healthcare"].spec

    # Each must auto-start the gliner service AND set a non-empty
    # GLINER_PII_LABELS that's distinct from the others — otherwise
    # they collapse into duplicates of `gliner-pii-service`.
    for spec in (minimal, financial, healthcare):
        assert spec.detector_mode == "gliner_pii"
        assert spec.gliner_backend == "service"
        assert "GLINER_PII_LABELS" in spec.env_overrides
        assert spec.env_overrides["GLINER_PII_LABELS"]

    label_sets = {
        minimal.env_overrides["GLINER_PII_LABELS"],
        financial.env_overrides["GLINER_PII_LABELS"],
        healthcare.env_overrides["GLINER_PII_LABELS"],
    }
    assert len(label_sets) == 3, "label sets must be distinct across the three presets"


def test_bundled_combined_preset_includes_both_detectors() -> None:
    """regex-pentest-gliner-pii-service must list BOTH detectors in
    detector_mode AND auto-start gliner. The `regex` half adds the
    deterministic shape-classifier layer; the `gliner_pii` half adds
    semantic NER. Pin both so a refactor doesn't drop one half
    silently."""
    from tools.launcher.preset_loader import load_presets

    combined = load_presets()["regex-pentest-gliner-pii-service"].spec
    assert combined.detector_mode == "regex,gliner_pii"
    assert combined.gliner_backend == "service"
    assert combined.env_overrides["REGEX_PATTERNS_PATH"] == "bundled:regex_pentest.yaml"


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
    assert "regex-default" in presets
    assert presets["regex-default"].source == "bundled"


def test_operator_file_replaces_bundled_by_name(tmp_path: Path) -> None:
    """When an operator's preset shares a name with a bundled preset,
    the operator entry replaces the bundled one verbatim — no
    per-field merging. Operator visibility into "did my override
    take effect?" is the load-bearing property."""
    from tools.launcher.preset_loader import load_presets, set_operator_presets_file

    op_file = tmp_path / "ops.yaml"
    # Replace `regex-pentest` (bundled) — drop its env_overrides
    # entirely and flip use_faker. The full-replace contract means
    # NONE of the bundled fields bleed through.
    op_file.write_text(
        "presets:\n"
        "  regex-pentest:\n"
        "    detector_mode: regex\n"           # narrower than bundled
        "    log_level: error\n"
        "    use_faker: true\n"
    )
    set_operator_presets_file(str(op_file))

    presets = load_presets()
    pentest = presets["regex-pentest"]
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
    # regex-default has no auto-start backends — all None.
    backends = _apply_preset(cfg, "regex-default")
    assert backends == {"llm": None, "privacy_filter": None, "gliner_pii": None}
    assert cfg.detector_mode == "regex"
    assert cfg.use_faker is False
    # And no service variants on a pure-regex preset.
    assert cfg.service_variants == {}


def test_bundled_vault_redis_preset_pins_backend_and_env() -> None:
    """vault-redis must (a) auto-start the shared Redis container and
    (b) set VAULT_BACKEND=redis. Pin both — operator picking the
    preset expects the vault to actually use redis, not the memory
    backend with redis side-running."""
    from tools.launcher.preset_loader import load_presets

    vr = load_presets()["vault-redis"].spec
    assert vr.redis_backend == "service"
    assert vr.env_overrides["VAULT_BACKEND"] == "redis"


def test_bundled_pf_cache_redis_pins_backend_and_cache_env() -> None:
    """pf-cache-redis must auto-start (PF service, redis container)
    AND select PRIVACY_FILTER_CACHE_BACKEND=redis so the PF detector
    actually routes its result cache through redis. Plus the HF
    variant — matches `privacy-filter-service`'s choice for the same
    rationale."""
    from tools.launcher.preset_loader import load_presets

    pf = load_presets()["pf-cache-redis"].spec
    assert pf.pf_backend == "service"
    assert pf.redis_backend == "service"
    assert pf.service_variants == {"privacy_filter": "hf"}
    assert pf.env_overrides["PRIVACY_FILTER_CACHE_BACKEND"] == "redis"


def test_apply_preset_writes_redis_backend() -> None:
    """`_apply_preset` must propagate `redis_backend` from the spec
    into `cfg.redis_backend` so `auto_start_services` knows to fire
    `start_redis`. Pin so a refactor that drops the apply step
    silently regresses every redis-using preset to memory backends."""
    from tools.launcher.main import _apply_preset
    from tools.launcher.runner import LaunchConfig

    cfg = LaunchConfig()
    _apply_preset(cfg, "vault-redis")
    assert cfg.redis_backend == "service"
    assert cfg.env_overrides["VAULT_BACKEND"] == "redis"


def test_apply_preset_writes_service_variants() -> None:
    """privacy-filter-service must propagate `privacy_filter=hf` into
    `cfg.service_variants` so the runner's auto-start path picks the
    HF-pipeline service container (port 8003) rather than the opf-only
    default (port 8001). Pin so a refactor that drops the service-
    variants apply step silently regresses to the wrong service."""
    from tools.launcher.main import _apply_preset
    from tools.launcher.runner import LaunchConfig

    cfg = LaunchConfig()
    backends = _apply_preset(cfg, "privacy-filter-service")
    assert backends["privacy_filter"] == "service"
    assert cfg.service_variants == {"privacy_filter": "hf"}
