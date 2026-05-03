# Dev tools

Implementation map for the three dev-only Python tools under
`tools/` — the launcher, the image builder, and the detector
benchmark. Each ships with a thin bash wrapper under `scripts/`,
each lives **outside** `src/anonymizer_guardrail/` so adding them
hasn't grown the production wheel, and each follows the same
shape: Click CLI by default, optional Textual TUI on `--ui`.

For operator-facing docs (how to *use* these tools), see:

- [deployment.md](deployment.md) — running the launcher and the
  images it builds.
- [benchmark.md](benchmark.md) — running the detector benchmark.

This page is the engineering view: how each tool is wired, where
to look when extending it.

## The launcher

A single thin bash wrapper (`scripts/launcher.sh`) execs into
`python -m tools.launcher`. The launcher is **dev-only** — its Python
source lives outside `src/` and isn't shipped in the production wheel.

Two modes share the entry point:

- **CLI (default)** — Click single-command app with options grouped
  per detector via a custom `GroupedCommand` formatter (option-name
  columns aligned globally across sections).
- **TUI (`--ui` / `--interactive`)** — eager Click callback hands off
  to the Textual app in `tools/launcher/menu.py`. Single-screen
  menuconfig-style — General / one section per enabled detector /
  Faker. Arrow keys navigate; Space toggles checkboxes; Enter
  confirms; Ctrl+↑/↓ reorders detectors in the enable+order modal.

Both produce a `LaunchConfig` and hand it to
`tools/launcher/runner.run_guardrail()`, which composes the engine
argv from `REGISTERED_SPECS` + `LAUNCHER_METADATA` and exec's the
container in the foreground. Auto-startable services (fake-llm,
privacy-filter-service, gliner-pii-service) are started before the
guardrail and torn down via `atexit`.

### Presets (`--preset NAME`)

Presets are pre-canned `LaunchConfig` partials that set image
flavour, detector mode, log level, surrogate mode, per-detector
backend choices, and any env-var overrides. Bundled defaults ship
in `tools/launcher/presets/default.yaml`; print the merged set with
`./scripts/launcher.sh --show-presets` to see exactly what each
preset applies.

Operators can extend or replace the bundled set via either:

- `--presets-file PATH` — CLI flag, takes precedence.
- `LAUNCHER_PRESETS_FILE=PATH` — env var, lower priority.

The operator file uses the same schema as the bundled YAML (see
`tools/launcher/preset_loader.py:LauncherPreset`). Operator entries
with names matching a bundled preset *replace* the bundled entry
verbatim (no per-field merging — the alternative invites
"why didn't my override take effect?" debugging traps). New names
are appended.

```yaml
# ~/anonymizer-presets.yaml
presets:
  # Replace the bundled "regex-default" with the pentest pattern set
  # plus debug logging — useful as an org-internal default.
  regex-default:
    detector_mode: regex
    log_level: debug
    use_faker: false
    env_overrides:
      REGEX_OVERLAP_STRATEGY: longest
      REGEX_PATTERNS_PATH: bundled:regex_pentest.yaml

  # Add a new "compliance" preset for org-internal scans.
  compliance:
    detector_mode: regex,denylist,privacy_filter
    log_level: warning
    use_faker: true
    pf_backend: service
    service_variants:
      privacy_filter: hf
    env_overrides:
      DENYLIST_PATH: /etc/anon/compliance-deny.yaml
```

```bash
# Inspect the merged set.
./scripts/launcher.sh --presets-file ~/anonymizer-presets.yaml --show-presets

# Or via env var, then run normally.
export LAUNCHER_PRESETS_FILE=~/anonymizer-presets.yaml
./scripts/launcher.sh --preset compliance
```

The schema rejects unknown fields (`extra="forbid"`) so a typo'd
key fails loud at YAML load with a Pydantic-formatted error rather
than silently doing nothing. Missing file paths are also fail-loud —
operators get a Click error pointing at the offending flag.

## The image builder

`scripts/image_builder.sh` execs into `python -m tools.image_builder`.
Same shape as the launcher: dev-only Python under `tools/`, never
shipped in the wheel; one bash wrapper, two interlocking modes
(Click CLI by default, Textual TUI on `--ui`).

The catalog of build targets lives in
`tools/image_builder/specs.py` as `FLAVOURS` (one `Flavour` per
buildable image — guardrail, the privacy-filter and gliner-pii
sidecars across CPU/CUDA/baked variants, the fake-llm companion) and
`PRESETS` (named subsets — `all`, `guardrail`, `privacy-filter`,
`gliner-pii`, `minimal`, `minimal-fakellm`). Adding an image flavour
is one entry in `_build_catalog()`; the CLI's `--flavour` validation,
the menu's checkbox grid, and `--list` all read `FLAVOURS` directly.

- **CLI** — `--flavour/-f NAME` (repeatable), `--preset NAME`,
  `--list`, `--tag/-T`, `--engine podman|docker`, `--yes/-y`, plus
  passthrough args after `--` (e.g. `-- --no-cache --pull`). Engine
  detection is the same `tools.launcher.engine.detect_engine` —
  prefers podman, falls back to docker, honours `ENGINE=…` and the
  per-flavour `TAG_*` env overrides the old bash script accepted.
- **TUI (`--ui`)** — preset radio at the top, grouped checkbox grid
  (two columns per group) below, Build/Cancel buttons. Left/right
  cycles the preset radio (auto-commits, skips the `custom`
  sentinel); up/down moves focus between widgets and walks the
  checkbox grid; space toggles a checkbox; ctrl+b builds; q/Esc
  cancels.

The runner shells out to `<engine> build` (subprocess, no
podman-py / docker-py dep — the surface we use is one call). Podman
gets `--format=docker` so the Containerfile's HEALTHCHECK isn't
silently dropped (OCI is podman's default and has no HEALTHCHECK
field). Multiple flavours run in sequence; the runner bails on the
first failure and returns the engine's exit code.

## The detector benchmark

`scripts/detector_bench.sh` execs into `python -m tools.detector_bench`.
Sister tool to `scripts/test-examples.sh` — where test-examples
answers *"do the curl recipes still work?"*, the benchmark answers
*"for THIS corpus on THIS detector mix, what fraction of expected
entities does the guardrail catch, and how often does it falsely
flag stuff that should be left alone?"* (recall, type accuracy,
precision, latency).

Three modules:

- `corpus.py` — YAML loader + schema validation. Fail-loud at load
  time so a typo surfaces before any HTTP traffic (rather than as a
  mysterious zero-recall score). `bundled:NAME` resolves to
  `tests/corpus/NAME.yaml`.
- `runner.py` — per-case execution + scoring. Forces
  `use_faker: false` per request so the response carries opaque
  `[TYPE_HEX]` tokens — that's how the script recovers types
  without per-entity attribution from the guardrail.
- `cli.py` — Click CLI, two operating modes. Default connects to
  `$BASE_URL` (caller manages the guardrail). `--preset NAME`
  spawns a fresh test guardrail via `scripts/launcher.sh --preset`,
  waits for `/health`, runs the corpus, and tears it down on exit
  (same `--port`/`--name`/`--keep` semantics as `test-examples.sh`).

`--compare` runs the corpus once per active detector (using the
per-request `detector_mode` override to filter the active set
down) plus once with the full mix, then prints a side-by-side
metric table. Always exits 0 — exploratory, not a CI gate.

Operator-facing docs live in [benchmark.md](benchmark.md); this
page is the implementation map.

## See also

- [development.md](development.md) — broader contributor guide;
  in particular the "Adding a new detector" walkthrough that
  references several of these tools.
