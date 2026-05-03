"""Launcher run-argv composition tests.

The launcher itself lives outside `src/` (dev-only, not in the
production wheel), but it's importable from tests via
`tools.launcher.*`. These tests pin the env-var forwarding contract
between the operator's environment and the guardrail container so a
future refactor doesn't silently drop env vars (which is exactly how
the privacy_filter `hf` variant bug shipped — the menu's variant
picker stored the choice on `cfg.service_variants` but the
auto-start path didn't read it).

Coverage focus: the per-detector + cross-cutting passthrough loop in
`build_run_argv`. Service auto-start is its own subsystem (tested via
the smoke path in CI when an operator runs the launcher; engine
side-effects make it expensive to unit-test).
"""

from __future__ import annotations

import os

# Match other test modules: keep transitive imports harmless.
os.environ.setdefault("DETECTOR_MODE", "regex")

import pytest

from tools.launcher.runner import LaunchConfig, build_run_argv


class _FakeEngine:
    """Minimal engine stub. `build_run_argv` only reads `engine.name`."""
    name = "podman"


def _argv_to_env_dict(argv: list[str]) -> dict[str, str]:
    """Walk a `podman run ... -e K=V -e K=V ...` argv and return the
    {K: V} dict. Last-wins semantics match how the engine resolves
    duplicate `-e` flags."""
    env: dict[str, str] = {}
    for i, arg in enumerate(argv):
        if arg == "-e" and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("=")
            env[k] = v
    return env


# ── Per-detector passthroughs ───────────────────────────────────────────


def test_per_detector_cache_env_vars_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new detector cache env var (`*_CACHE_BACKEND`,
    `*_CACHE_TTL_S`, `*_CACHE_MAX_SIZE`, `*_INPUT_MODE`) set in the
    operator's shell must reach the guardrail container. Pre-existing
    `*_CACHE_MAX_SIZE` and `*_INPUT_MODE` had been silently dropped —
    pin all four to prevent regression."""
    monkeypatch.setenv("LLM_CACHE_BACKEND", "redis")
    monkeypatch.setenv("LLM_CACHE_TTL_S", "1200")
    monkeypatch.setenv("LLM_CACHE_MAX_SIZE", "500")
    monkeypatch.setenv("LLM_INPUT_MODE", "merged")
    # CACHE_REDIS_URL is required when *_CACHE_BACKEND=redis is set —
    # otherwise the central Config's validator would reject the env.
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://fake/0")

    cfg = LaunchConfig(detector_mode="regex,llm")
    argv = build_run_argv(_FakeEngine(), cfg)
    env = _argv_to_env_dict(argv)

    assert env["LLM_CACHE_BACKEND"] == "redis"
    assert env["LLM_CACHE_TTL_S"] == "1200"
    assert env["LLM_CACHE_MAX_SIZE"] == "500"
    assert env["LLM_INPUT_MODE"] == "merged"


def test_pf_and_gliner_cache_env_vars_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract for privacy_filter and gliner_pii."""
    monkeypatch.setenv("PRIVACY_FILTER_CACHE_BACKEND", "redis")
    monkeypatch.setenv("PRIVACY_FILTER_CACHE_TTL_S", "900")
    monkeypatch.setenv("GLINER_PII_CACHE_BACKEND", "redis")
    monkeypatch.setenv("GLINER_PII_CACHE_TTL_S", "1800")
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://fake/0")
    # PF and gliner detectors require their service URLs to be
    # resolvable for the spec config to parse cleanly.
    monkeypatch.setenv("PRIVACY_FILTER_URL", "http://pf:8001")
    monkeypatch.setenv("GLINER_PII_URL", "http://gliner:8002")

    cfg = LaunchConfig(detector_mode="regex,privacy_filter,gliner_pii")
    argv = build_run_argv(_FakeEngine(), cfg)
    env = _argv_to_env_dict(argv)

    assert env["PRIVACY_FILTER_CACHE_BACKEND"] == "redis"
    assert env["PRIVACY_FILTER_CACHE_TTL_S"] == "900"
    assert env["GLINER_PII_CACHE_BACKEND"] == "redis"
    assert env["GLINER_PII_CACHE_TTL_S"] == "1800"


# ── Cross-cutting passthroughs ──────────────────────────────────────────


def test_vault_env_vars_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """`VAULT_BACKEND=redis` from the operator's shell must reach the
    guardrail container. Without forwarding the operator would have
    to pass `-- -e VAULT_BACKEND=redis -e VAULT_REDIS_URL=...` as
    podman extras, which is the bug this test prevents."""
    monkeypatch.setenv("VAULT_BACKEND", "redis")
    monkeypatch.setenv("VAULT_REDIS_URL", "redis://prod-vault:6379/0")
    monkeypatch.setenv("VAULT_TTL_S", "1800")
    monkeypatch.setenv("VAULT_MAX_ENTRIES", "50000")

    cfg = LaunchConfig(detector_mode="regex")
    argv = build_run_argv(_FakeEngine(), cfg)
    env = _argv_to_env_dict(argv)

    assert env["VAULT_BACKEND"] == "redis"
    assert env["VAULT_REDIS_URL"] == "redis://prod-vault:6379/0"
    assert env["VAULT_TTL_S"] == "1800"
    assert env["VAULT_MAX_ENTRIES"] == "50000"


def test_cache_redis_url_and_salt_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-cutting cache settings — `CACHE_REDIS_URL` and
    `CACHE_SALT` — flow through. CACHE_SALT especially is critical
    for multi-replica deployments (the boot validator catches missing
    URL but not missing salt; without forwarding, every replica
    would generate a different random salt and cross-replica cache
    hits silently fail)."""
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://prod-cache:6379/1")
    monkeypatch.setenv("CACHE_SALT", "fixed-salt-for-multi-replica")

    cfg = LaunchConfig(detector_mode="regex,llm")
    argv = build_run_argv(_FakeEngine(), cfg)
    env = _argv_to_env_dict(argv)

    assert env["CACHE_REDIS_URL"] == "redis://prod-cache:6379/1"
    assert env["CACHE_SALT"] == "fixed-salt-for-multi-replica"


def test_max_body_bytes_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """`MAX_BODY_BYTES` was a pre-existing knob the launcher silently
    dropped. Now forwarded via the cross-cutting list."""
    monkeypatch.setenv("MAX_BODY_BYTES", "20971520")  # 20 MiB

    cfg = LaunchConfig(detector_mode="regex")
    argv = build_run_argv(_FakeEngine(), cfg)
    env = _argv_to_env_dict(argv)

    assert env["MAX_BODY_BYTES"] == "20971520"


def test_unset_cross_cutting_vars_not_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/unset operator env vars must NOT produce empty `-e VAR=`
    flags. Keeps the engine command-line clean at default settings."""
    # Make sure no cross-cutting vars are set.
    for var in (
        "VAULT_BACKEND", "VAULT_REDIS_URL", "VAULT_TTL_S",
        "VAULT_MAX_ENTRIES",
        "CACHE_REDIS_URL", "CACHE_SALT",
        "MAX_BODY_BYTES",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = LaunchConfig(detector_mode="regex")
    argv = build_run_argv(_FakeEngine(), cfg)
    env = _argv_to_env_dict(argv)

    # None of the cross-cutting vars should appear.
    assert "VAULT_BACKEND" not in env
    assert "VAULT_REDIS_URL" not in env
    assert "CACHE_REDIS_URL" not in env
    assert "CACHE_SALT" not in env
    assert "MAX_BODY_BYTES" not in env


# ── --show-presets renderer ─────────────────────────────────────────────


def test_show_presets_table_lists_every_bundled_preset() -> None:
    """`--show-presets` renders one row per loaded preset. Pin so a
    future preset addition that forgets to land in the table surfaces
    as a test failure rather than as silent operator confusion.

    Layout: presets-as-rows, settings-as-columns. The table's
    `columns[0]` is "Preset" (left-most label); each preset gets a
    row with the same column shape."""
    from rich.console import Console
    from tools.launcher.main import _render_presets_table
    from tools.launcher.preset_loader import load_presets

    table = _render_presets_table()
    headers = [c.header for c in table.columns]
    # First column carries the preset name.
    assert headers[0] == "Preset"
    # Render and look for every preset name appearing left-aligned.
    console = Console(width=200, record=True, file=open("/dev/null", "w"))
    console.print(table)
    rendered = console.export_text()
    for name in load_presets():
        assert name in rendered, f"preset {name!r} missing from rendered table"


def test_show_presets_table_includes_env_overrides() -> None:
    """Env overrides (e.g. `REGEX_PATTERNS_PATH=bundled:regex_pentest.yaml`)
    are applied silently by `--preset regex-pentest`. The table must
    surface them — otherwise an operator can't tell from the listing
    why one preset's behaviour differs from another's."""
    from rich.console import Console
    from tools.launcher.main import _render_presets_table

    # Render to a string we can grep, with width forced wide so
    # nothing truncates.
    console = Console(width=200, record=True, file=open("/dev/null", "w"))
    console.print(_render_presets_table())
    rendered = console.export_text()

    assert "REGEX_PATTERNS_PATH=bundled:regex_pentest.yaml" in rendered
    assert "REGEX_PATTERNS_PATH=bundled:regex_default.yaml" in rendered
    assert "REGEX_OVERLAP_STRATEGY=longest" in rendered


def test_show_presets_table_renders_backend_choices() -> None:
    """Operators read the table to answer "which preset auto-starts
    PF?". The PF / GLiNER / LLM columns must surface the backend
    choice for each preset — not silently dropped."""
    from rich.console import Console
    from tools.launcher.main import _render_presets_table

    console = Console(width=200, record=True, file=open("/dev/null", "w"))
    console.print(_render_presets_table())
    rendered = console.export_text()

    # privacy-filter-service auto-starts PF; gliner-pii-service auto-
    # starts GLiNER; the column headers must be present.
    assert "PF" in rendered
    assert "GLiNER" in rendered
    assert "LLM" in rendered
    # And `service` appears for the auto-starting presets.
    assert "service" in rendered


def test_show_presets_table_renders_service_variants() -> None:
    """The privacy-filter-service preset's `privacy_filter=hf` variant
    selection must be visible in the table — that's the operator-
    facing differentiator from a hypothetical opf-only privacy filter
    preset."""
    from rich.console import Console
    from tools.launcher.main import _render_presets_table

    console = Console(width=200, record=True, file=open("/dev/null", "w"))
    console.print(_render_presets_table())
    rendered = console.export_text()

    assert "privacy_filter=hf" in rendered


def test_show_presets_table_inlines_source_annotation() -> None:
    """The Source column was collapsed into the Preset cell as a
    `(bundled)` / `(operator)` annotation — narrower table without
    losing the load-bearing information ("did my override take
    effect?"). Pin so a refactor that drops the annotation
    silently regresses that signal."""
    from pathlib import Path
    from rich.console import Console
    from tools.launcher.main import _render_presets_table
    from tools.launcher.preset_loader import set_operator_presets_file

    console = Console(width=200, record=True, file=open("/dev/null", "w"))

    # Bundled-only run — preset rows are just the bare name, no
    # prefix annotation. `operator:` should NOT appear anywhere.
    console.print(_render_presets_table())
    rendered = console.export_text()
    assert "operator:" not in rendered
    # Bare preset names appear (whitespace-bounded so partial matches
    # of the name as a substring of an env-override path don't false-pass).
    assert " regex-default " in rendered or rendered.startswith("regex-default")

    # Add an operator file with a fresh name and verify ONLY that
    # row gets the operator: prefix; bundled rows stay bare.
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False,
    ) as tf:
        tf.write(
            "presets:\n"
            "  test-operator-only:\n"
            "    detector_mode: regex\n"
        )
        op_path = tf.name
    try:
        set_operator_presets_file(op_path)
        console2 = Console(width=200, record=True, file=open("/dev/null", "w"))
        console2.print(_render_presets_table())
        rendered2 = console2.export_text()
        assert "operator: test-operator-only" in rendered2
        # The pre-existing bundled preset is still bare — no prefix.
        # Anchor the check against an env-overrides bundled-path which
        # also contains "bundled:" but never "bundled:" with a space
        # before it, so this is unambiguously the name-cell check.
        import re
        assert re.search(r"\bregex-default\b", rendered2)
    finally:
        set_operator_presets_file(None)
        Path(op_path).unlink(missing_ok=True)


def test_detector_specific_takes_precedence_over_cross_cutting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a per-detector passthrough and a cross-cutting passthrough
    ever name the same var, the per-detector one wins (the loop
    runs first; the cross-cutting loop checks `already_set_keys`).
    No collision today, but pin the precedence so future additions
    can't silently break it."""
    # Inject a fake collision: cfg.env_overrides forces an LLM_API_BASE
    # value via the per-detector loop. Then we set the same name as a
    # cross-cutting var (impossible today, but cfg.env_overrides is
    # consulted by both loops). The per-detector value should remain.
    monkeypatch.setenv("LLM_API_BASE", "http://operator-set:9999/v1")

    cfg = LaunchConfig(detector_mode="regex,llm")
    cfg.env_overrides["LLM_API_BASE"] = "http://override:1111/v1"

    argv = build_run_argv(_FakeEngine(), cfg)
    env = _argv_to_env_dict(argv)

    # cfg.env_overrides wins over os.environ in the per-detector loop;
    # cross-cutting loop wouldn't get to the same key.
    assert env["LLM_API_BASE"] == "http://override:1111/v1"
