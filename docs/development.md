# Development

Guide for contributors. For end-user / operator docs, see the
[README](../README.md) and the rest of the [docs/](./) folder.

## Setup

```bash
pip install -e ".[dev]"
pytest                                 # full unit-test suite, no container needed
uvicorn anonymizer_guardrail.main:app --reload   # hot-reload dev server
```

`[dev]` pulls in `pytest`, `pytest-asyncio`, `pytest-mock`, plus the
launcher's deps (`click`, `textual`, `rich`). Production installs (the
container image and `pip install anonymizer-guardrail`) skip `[dev]` so
none of those land in production.

## Running tests

The full unit-test suite is fast and self-contained:

```bash
pytest
pytest tests/test_pipeline.py -v       # one file
pytest -k surrogate                    # name match
```

End-to-end tests exercise the full HTTP path against an actual
container (builds + runs + asserts via `launcher.sh --preset`):

```bash
# Run `scripts/launcher.sh --show-presets` for the full preset list.
scripts/test-examples.sh --preset regex-default                       # regex only
scripts/test-examples.sh --preset regex-pentest-gliner-pii-service    # regex + gliner combo
scripts/test-examples.sh                                              # connect to BASE_URL (already-running guardrail)
```

## Repo layout

```
src/anonymizer_guardrail/        # the importable package — what ships
  detector/
    __init__.py                  # REGISTERED_SPECS + LAUNCHER_METADATA
                                 # + lookup tables
    spec.py                      # DetectorSpec dataclass
    launcher.py                  # LauncherSpec / ServiceSpec dataclasses
    base.py                      # Detector Protocol, Match, ENTITY_TYPES
    cache.py                     # DetectorResultCache Protocol +
                                 # cache-salt resolver + factory
    cache_memory.py              # InMemoryDetectionCache (default)
    cache_redis.py               # RedisDetectionCache (opt-in)
    regex.py                     # Each detector module owns its
    denylist.py                  # CONFIG + SPEC + LAUNCHER_SPEC +
    llm.py                       # (optionally) Unavailable error class.
    remote_privacy_filter.py
    remote_gliner_pii.py
  pipeline.py                    # Iterates REGISTERED_SPECS to build
  main.py                        # everything dynamic.
  config.py                      # Cross-cutting config only (vault,
                                 # surrogate, http server, cache redis).
                                 # Per-detector config lives in the
                                 # detector module.
  vault.py                       # VaultBackend Protocol + factory
                                 # + VaultEntry/VaultSurrogate types
                                 # + freeze_kwargs helper
  vault_memory.py                # MemoryVault (default)
  vault_redis.py                 # RedisVault (opt-in)
  pipeline_cache.py              # PipelineResultCache Protocol +
                                 # factory + Disabled-sentinel
  pipeline_cache_memory.py       # InMemoryPipelineCache (default
                                 # when backend != "none")
  pipeline_cache_redis.py        # RedisPipelineCache (opt-in)

tools/launcher/                  # dev-only, NOT in the wheel
  __main__.py                    # `python -m tools.launcher` entry
  spec_extras.py                 # cross-cutting constants
                                 # (SHARED_NETWORK, …) + re-exports
                                 # LAUNCHER_METADATA from the prod
                                 # package (per-detector metadata is
                                 # colocated with each detector module)
  engine.py                      # podman/docker dispatch
  services.py                    # auto-start lifecycle
  runner.py                      # build run argv + exec engine
  main.py                        # Click CLI
  menu.py                        # Textual TUI

tools/image_builder/             # dev-only, NOT in the wheel
  __main__.py                    # `python -m tools.image_builder`
  specs.py                       # FLAVOURS catalog + PRESETS
  runner.py                      # `<engine> build` argv + exec
  main.py                        # Click CLI
  menu.py                        # Textual TUI (preset radio + grid)

tools/detector_bench/            # dev-only, NOT in the wheel
  __main__.py                    # `python -m tools.detector_bench`
  corpus.py                      # YAML loader + schema validation
  runner.py                      # per-case execution + scoring
  cli.py                         # Click CLI

scripts/                         # bash wrappers, ~5 lines each
  launcher.sh                    # exec python -m tools.launcher
                                 #   (CLI by default, --ui opens TUI)
  image_builder.sh               # exec python -m tools.image_builder
                                 #   (CLI by default, --ui opens TUI)
  detector_bench.sh              # exec python -m tools.detector_bench
  test-examples.sh               # end-to-end curl recipes
  release.sh                     # git tag + push for CI

services/                        # auxiliary inference containers
  fake_llm/
  privacy_filter/
  gliner_pii/

tests/                           # pytest suite

docs/                            # operator-facing documentation
  configuration.md               # cross-cutting env vars (per-
                                 # detector vars live in detectors/<name>.md)
  litellm-integration.md         # LiteLLM wiring
  surrogates.md                  # surrogates / salt / Faker / locales
  vault.md                       # round-trip mapping, TTL, size cap
  per-request-overrides.md       # additional_provider_specific_params
  operations.md                  # /health, detector result caching,
                                 # merged-input mode
  limitations.md                 # known constraints (single replica,
                                 # streaming, body-size cap, etc.)
  design-decisions.md            # paths considered and declined
  examples.md                    # curl recipes
  deployment.md                  # container images, build, run
  development.md                 # this file
  tools.md                       # implementation map for tools/launcher,
                                 # tools/image_builder, tools/detector_bench
  detector-bench.md              # operator guide for detector_bench.sh
  service-bench.md               # CPU/GPU latency comparison across detector services
  detectors/                     # one file per detector
    index.md                     # overview + comparison
    regex.md
    denylist.md
    privacy-filter.md
    gliner-pii.md
    llm.md
```

## Architecture: the detector registry

The pipeline is registry-driven. Adding a detector touches a small
fixed set of files because the central code (`Pipeline`, `main.py`,
the launcher) iterates `REGISTERED_SPECS` to build everything dynamic.

**`detector.REGISTERED_SPECS`** — a tuple of `DetectorSpec` constants,
one per `DETECTOR_MODE` token. Each spec carries:

- `name` — the `DETECTOR_MODE` token + the `Detector.name` attribute.
- `factory` — a callable returning a `Detector` instance.
- `module` — the detector's own module (captured via
  `sys.modules[__name__]`); the `config` property reads `module.CONFIG`
  live so test monkeypatches are visible.
- `prepare_call_kwargs(overrides, api_key)` — builds per-call kwargs.
- `has_semaphore`, `stats_prefix` — concurrency cap. The cap value
  itself comes from `spec.config.max_concurrency` (live lookup).
- `unavailable_error`, `blocked_reason` — failure mode. The
  fail-closed flag comes from `spec.config.fail_closed` (live lookup).

**Per-detector `CONFIG`** — each detector module
(`detector/<name>.py`) defines its own `<Name>Config` BaseSettings model
(env-prefixed, `frozen=True`) and instantiates `CONFIG`. Cross-cutting
fields (vault, surrogate, http server, faker locale) stay on the
central `Config` in `config.py`.

**`Pipeline.__init__`** iterates `REGISTERED_SPECS` to allocate
`_semaphores` / `_inflight_counters` dicts, build the
`_DETECTOR_FACTORIES` mapping, and emit the startup log line.
**`Pipeline._run`** dispatches via `SPECS_BY_NAME[det.name]`, no
`isinstance` chains. **`Pipeline.stats()`** iterates
`SPECS_WITH_SEMAPHORE` for the per-detector keys.

**`main.py`'s exception handler** uses the
`TYPED_UNAVAILABLE_ERRORS` tuple to catch every detector's typed
error in one `except` clause; the matching `spec.blocked_reason`
becomes the operator-facing message.

**`detector.LAUNCHER_METADATA`** — parallel mapping
(`{spec.name: LauncherSpec}`) the dev-only launcher reads for
auto-startable services, env passthroughs, container/port. Aggregated
in `detector/__init__.py` from each module's `LAUNCHER_SPEC`
constant — colocated with `CONFIG` and `SPEC` so adding a detector is
a single-file edit. The dataclasses (`LauncherSpec`, `ServiceSpec`)
are pure stdlib so shipping them in the wheel costs nothing
operationally; `tools/launcher/spec_extras.py` is just a re-export shim
plus a few cross-cutting constants (`SHARED_NETWORK`, …).

## Dev tools

The launcher, image builder, and detector benchmark all live under
`tools/` (dev-only, not in the wheel) with thin bash wrappers under
`scripts/`. Their implementation map — module layout, CLI/TUI
shape, runner internals — lives in [tools.md](tools.md). Read that
before extending any of them.

## Adding a new detector

This is the canonical walkthrough. Skim the **TL;DR** at the bottom
first; the sections below cover each step in detail.

### 1. Write the detector module

New file: `src/anonymizer_guardrail/detector/my_detector.py`. Implement
the `Detector` protocol from `base.py`:

```python
from .base import Detector, Match


class MyDetector:
    name = "my_detector"

    def __init__(self) -> None:
        # construct deps; raise loudly if config is incomplete
        ...

    async def detect(self, text: str, **kwargs: Any) -> list[Match]:
        # produce Match(text, entity_type) for every detected entity
        ...
```

If the detector talks to an external service (HTTP, model, etc.),
define a typed exception so failures route through the fail-closed
policy:

```python
class MyDetectorUnavailableError(RuntimeError):
    """Raised on availability failure (connect / timeout / non-200 /
    unparseable 200 OK body)."""
```

A 200 OK that the detector can't parse counts as an availability
failure: the backend replied but said nothing actionable, and
soft-failing to `[]` would silently violate `*_FAIL_CLOSED=true`
(the explicit "block rather than risk leakage" posture). Per-entry
malformed entries within an otherwise-parseable payload still drop
silently — those only invalidate one match, not the whole response.

### 2. Define a `CONFIG` BaseSettings model

Same module. `pydantic_settings.BaseSettings` subclass with
`env_prefix` set to your detector's namespace; the upper-snake-case
form of each field name maps to the env var (e.g. `url` → `MY_DETECTOR_URL`).
Conventional field names for backend-style detectors: `url`,
`timeout_s`, `max_concurrency`, `fail_closed`.

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class MyDetectorConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MY_DETECTOR_",
        extra="ignore",
        frozen=True,
    )

    url: str = ""
    timeout_s: int = 30
    max_concurrency: int = 10
    fail_closed: bool = True


CONFIG = MyDetectorConfig()
```

The detector reads its own config via the module-level `CONFIG`
attribute (e.g. `CONFIG.url`). The pipeline reads
`spec.config.fail_closed` and `spec.config.max_concurrency` live, so
test monkeypatches via `MOD.CONFIG.model_copy(update={...})` take
effect immediately for all consumers. Pydantic crashes loud at
instantiation on a malformed env value (`MY_DETECTOR_TIMEOUT_S=abc`),
which is exactly the misconfiguration that should fail boot rather
than first request.

### 3. Define a `SPEC` constant

Same module. Captures the module reference for live `CONFIG` lookup:

```python
import sys

from .spec import DetectorSpec


SPEC = DetectorSpec(
    name="my_detector",                    # DETECTOR_MODE token
    factory=MyDetector,                    # () -> Detector
    module=sys.modules[__name__],          # for live CONFIG lookup
    has_semaphore=True,                    # gate detect() behind a semaphore?
    stats_prefix="my_detector",            # /health key prefix
    unavailable_error=MyDetectorUnavailableError,
    blocked_reason=(
        "My detector is unreachable; request blocked to prevent "
        "unredacted data from reaching the upstream model."
    ),
    has_cache=True,                        # surface result-cache stats on /health?
)
```

`has_semaphore=True` requires `CONFIG.max_concurrency` (an int)
on your detector module. `unavailable_error=...` requires
`CONFIG.fail_closed` (a bool). `has_cache=True` requires
`CONFIG.cache_max_size` (an int) and a `cache_stats()` method on
the detector instance returning `{size, max, hits, misses}` —
see `detector/cache.py`'s `DetectorResultCache` Protocol. The
`__post_init__` on `DetectorSpec` validates these at module
import; misconfiguration crashes the import that defined the
bad spec, not the request that first hit it.

If you want operators to be able to pick the redis cache backend
for your detector, also add `cache_backend: Literal["memory",
"redis"] = "memory"` and `cache_ttl_s: int = 600` to your
`*Config`. These are conventional (LLM/PF/gliner all carry them)
but not validated by `DetectorSpec` — `BaseRemoteDetector.__init__`
forwards them to `build_detector_cache()` if present. The central
`Config` already owns the cross-cutting `CACHE_REDIS_URL` and
`CACHE_SALT`, so you don't need to repeat them per-detector.

Optional `prepare_call_kwargs=fn` if `detect()` takes per-request
overrides from the `Overrides` dataclass — see
`detector/llm.py` for the shape (LLM passes `api_key`, `model`,
`prompt_name`).

### 4. Define a `LAUNCHER_SPEC` constant

Same module — colocated with `SPEC` so adding a detector is a
single-file edit. The launcher reads this to know which env vars to
forward onto the guardrail container and (optionally) which sidecar
service to auto-start. The dataclasses are pure stdlib, so they ship
in the wheel without dragging launcher deps in.

```python
from .launcher import LauncherSpec, ServiceSpec


LAUNCHER_SPEC = LauncherSpec(
    guardrail_env_passthroughs=[
        # Every <DETECTOR>_* env var the operator might set in their
        # shell. The launcher emits `-e <var>=<value>` only when the
        # var is non-empty, so this list can be exhaustive without
        # noise at default settings.
        "MY_DETECTOR_URL",
        "MY_DETECTOR_TIMEOUT_S",
        "MY_DETECTOR_FAIL_CLOSED",
        "MY_DETECTOR_MAX_CONCURRENCY",
    ],
    # Omit `service=` for in-process detectors (regex, denylist).
    service=ServiceSpec(
        container_name="my-service",
        image_tag_envs=("TAG_MY_SERVICE_BAKED", "TAG_MY_SERVICE"),
        image_tag_defaults=("my-service:baked", "my-service:cpu"),
        port=8003,
        readiness_timeout_s=300,            # generous for ML services
        hf_cache_volume="my-hf-cache",      # if it pulls weights
        guardrail_env_when_started={
            "MY_DETECTOR_URL": "http://my-service:8003",
        },
    ),
)
```

### 5. Register the spec

`src/anonymizer_guardrail/detector/__init__.py` — append to
`REGISTERED_SPECS` in canonical priority order (regex → denylist →
privacy_filter → … → llm). Position matters: `_dedup` keeps the
first-seen entity_type for duplicate text matches, so the detector
listed first wins type-resolution conflicts. Add the matching
`LAUNCHER_SPEC` import + `LAUNCHER_METADATA` entry in the same edit.

```python
from .my_detector import (
    LAUNCHER_SPEC as _MY_DETECTOR_LAUNCHER_SPEC,
    SPEC as _MY_DETECTOR_SPEC,
)

REGISTERED_SPECS = (
    _REGEX_SPEC,
    _DENYLIST_SPEC,
    _PRIVACY_FILTER_SPEC,
    _GLINER_PII_SPEC,
    _MY_DETECTOR_SPEC,            # ← here
    _LLM_SPEC,
)

LAUNCHER_METADATA = {
    _REGEX_SPEC.name: _REGEX_LAUNCHER_SPEC,
    _DENYLIST_SPEC.name: _DENYLIST_LAUNCHER_SPEC,
    _PRIVACY_FILTER_SPEC.name: _PRIVACY_FILTER_LAUNCHER_SPEC,
    _GLINER_PII_SPEC.name: _GLINER_PII_LAUNCHER_SPEC,
    _MY_DETECTOR_SPEC.name: _MY_DETECTOR_LAUNCHER_SPEC,    # ← here
    _LLM_SPEC.name: _LLM_LAUNCHER_SPEC,
}
```

**That's it for the runtime.** `Pipeline.__init__` picks up the new
spec automatically — its semaphore + counter are allocated, the log
line includes its fail-mode and cap, `_run_detector` dispatches via
`SPECS_BY_NAME[det.name]`, and `main.py`'s BLOCKED handler maps the
typed error via `TYPED_UNAVAILABLE_ERRORS`. `/health` gains
`<stats_prefix>_in_flight` / `<stats_prefix>_max_concurrency` keys
(when `has_semaphore=True`) and
`<stats_prefix>_cache_size` / `_cache_max` / `_cache_hits` /
`_cache_misses` (when `has_cache=True`). The launcher picks up the new
`LAUNCHER_METADATA` entry automatically too.
The per-request `detector_mode` length cap in `api.py` is also
derived from `len(REGISTERED_SPECS)`, so callers can pass the new
detector in their override list without bumping a constant.

### 6. Tests

Two files:

**`tests/test_detector_my_detector.py`** — unit tests for the detector
class. Mock external deps (httpx, model, files). Copy the shape of
`tests/test_detector_gliner_pii.py`. Patch config via:

```python
from dataclasses import replace
from anonymizer_guardrail.detector import my_detector as md_mod


def _fake_config(**overrides):
    base = md_mod.MyDetectorConfig(url="http://test", timeout_s=5)
    return replace(base, **overrides) if overrides else base


def test_something(monkeypatch):
    monkeypatch.setattr(md_mod, "CONFIG", _fake_config(url="http://x"))
    # ... test detector behavior with the patched config
```

**`tests/test_pipeline.py`** — add per-detector blocks for fail-closed
propagation, fail-open swallowing, and in-flight counter increments.
Mirror the existing `_pf_detector_that_raises` /
`_gliner_detector_that_raises` helpers.

### 7. (If your detector has an auto-startable service container)

The `LAUNCHER_SPEC` you added in step 4 already wires the launcher's
side of the lifecycle (auto-start, env-var forwarding, /health probe,
atexit cleanup). What's left is the service itself, plus an optional
image-builder catalog entry.

**a. `services/my_service/`** — new directory:

- `main.py` — FastAPI app exposing `POST /detect` and `GET /health`.
- `Containerfile` — build instructions. Mirror
  `services/privacy_filter/Containerfile` for shape.
- `README.md` — service-specific docs.

**b. `tools/image_builder/specs.py`** *(optional — convenience for
local-dev builds via `scripts/image_builder.sh`)*. The image builder
is a dev-loop wrapper around `<engine> build`; its catalog has no
runtime effect on the launcher or the service. Operators can always
build the new Containerfile directly (`podman build -f
services/my_service/Containerfile services/my_service`), and CI
publishes via its own workflow under `.github/workflows/`. Add a
catalog entry only if you want the new image to show up in the
builder's TUI / `--preset` / `--list`:

- New `Flavour(...)` entry in `_build_catalog()`. Set `containerfile`,
  `context`, any `build_args` (e.g. CUDA index, baked-model toggle),
  the default tag (with a matching `TAG_MY_SERVICE` env override via
  `_resolve_default_tag`), and `bakes_model=True` if the build pulls
  weights from HuggingFace at build time.
- If you want it grouped under a new menu section, add a
  `GROUP_MY_GROUP = "my-group"` constant and an entry in
  `_GROUP_ORDER` in `tools/image_builder/menu.py`. Otherwise reuse
  one of the existing groups.
- Add it to relevant entries in the `PRESETS` dict (`all` is computed
  automatically; per-service presets are explicit).

The CLI's `--flavour` validation, the menu's checkbox grid, and
`--list` all read `FLAVOURS` directly, so nothing else needs touching.

### 8. (If you want CLI / menu support)

**`tools/launcher/main.py`** — for each operator-facing knob, one
`@grouped_option(...)` decorator on `cli()` plus one line in the
function body to map it to `cfg.env_overrides`. Add a section
constant if your detector needs its own help group:

```python
_S_MY = "My detector"

@grouped_option(
    "--my-detector-url",
    type=str, default=None, group=_S_MY,
    help="MY_DETECTOR_URL — implies --my-detector-backend external.",
)
# ... and one mapping line in the cli body:
if my_detector_url is not None:
    cfg.env_overrides["MY_DETECTOR_URL"] = my_detector_url
```

**`tools/launcher/menu.py`** — three additions:

- A new section + per-detector rows in `_build_options()`, gated by
  `if "my_detector" in active`.
- An `elif key == "my_detector_url":` branch in `_edit_setting()` per
  field that needs an editor modal.
- Add `"my_detector"` to the `canonical` list in `_detector_order()`
  so it shows up in the enable+order picker.

### 9. (If you want documentation)

**`docs/detectors/my-detector.md`** — when to use it, the full
`MY_DETECTOR_*` env-var reference (under a `## Configuration`
section), failure-mode notes. Mirror the shape of the existing
files in `docs/detectors/`. `docs/configuration.md` deliberately
keeps only the cross-cutting knobs and links out to each detector's
page for its own env vars, so per-detector tables stay in one place.

**`services/my_service/README.md`** — service-specific docs (API,
build, run).

### TL;DR — files to touch

| Scope | Files |
|---|---|
| **In-process detector only** | 2 — new detector module (CONFIG + SPEC + LAUNCHER_SPEC, all colocated) + `detector/__init__.py` (REGISTERED_SPECS + LAUNCHER_METADATA entries) |
| **+ tests** | +1 to +2 — `tests/test_my_detector.py`, optional `tests/test_pipeline.py` |
| **+ service container** | +3 — `services/my_service/{main.py,Containerfile,README.md}`. The launcher already picks up the auto-start metadata from your detector module's `LAUNCHER_SPEC`. Optionally +1 (`tools/image_builder/specs.py`) so the new image shows up in the local-dev image-builder catalog. |
| **+ launcher CLI** | +1 — `tools/launcher/main.py` |
| **+ launcher menu** | +1 — `tools/launcher/menu.py` |
| **+ docs** | +1 — `docs/detectors/<name>.md` (per-detector env vars live there, not in `docs/configuration.md`) |

What you don't have to touch: `pipeline.py`, `main.py`, `config.py`,
the bash wrapper script (`launcher.sh`), or any "list of detectors"
enumeration. They all consume `REGISTERED_SPECS` / `LAUNCHER_METADATA`
and adapt automatically.

## Conventions

- **No comments unless WHY is non-obvious.** Code should explain
  itself. Save comments for hidden constraints, subtle invariants,
  workarounds for specific bugs, or behaviour that would surprise a
  reader. Don't restate what the code does.
- **No backwards-compat shims** unless explicitly needed for a
  deprecation cycle. Track deprecation work in `TASKS.md`.
- **Per-detector config** lives in the detector module. The central
  `Config` only carries cross-cutting fields. Tests patch via
  `MOD.CONFIG.model_copy(update={...})`, never reach into central
  `Config`.
- **Fail loud at boot** for misconfiguration. A typo in
  `REGEX_PATTERNS_PATH` should crash on import, not at first request.
  Add validation in `__post_init__` or module-top check blocks.
- **Detector exceptions** route through the fail-closed/fail-open
  policy via the typed error → `spec.unavailable_error`. Don't catch
  and swallow inside the detector — let the pipeline decide.

## See also

- [tools.md](tools.md) — implementation map for the launcher, image
  builder, and detector benchmark.
- [TASKS.md](../TASKS.md) — backlog of deliberately deferred work, with
  rationale for each deferral. Useful before starting on something
  that might already have a documented design sketch.
- [design-decisions.md](design-decisions.md) — paths considered and
  declined, with the load-bearing reasoning so future work can
  re-evaluate when the assumptions change.
- [README.md](../README.md) — project landing page; links into the
  rest of `docs/`.
- [services/privacy_filter/README.md](../services/privacy_filter/README.md)
  and [services/gliner_pii/README.md](../services/gliner_pii/README.md) —
  reference shape for new auxiliary inference services.
