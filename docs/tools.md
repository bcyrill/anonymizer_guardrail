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

## The image builder

`scripts/image_builder.sh` execs into `python -m tools.image_builder`.
Same shape as the launcher: dev-only Python under `tools/`, never
shipped in the wheel; one bash wrapper, two interlocking modes
(Click CLI by default, Textual TUI on `--ui`).

The catalog of build targets lives in
`tools/image_builder/specs.py` as `FLAVOURS` (one `Flavour` per
buildable image — guardrail slim/pf/pf-baked, the privacy-filter and
gliner-pii sidecars across CPU/CUDA/baked variants, the fake-llm
companion) and `PRESETS` (named subsets — `all`, `guardrail`,
`privacy-filter`, `gliner-pii`, `minimal`, `minimal-fakellm`). Adding
an image flavour is one entry in `_build_catalog()`; the CLI's
`--flavour` validation, the menu's checkbox grid, and `--list` all
read `FLAVOURS` directly.

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
