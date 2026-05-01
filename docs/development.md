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
container (builds + runs + asserts via `cli.sh --preset`):

```bash
scripts/test-examples.sh --preset uuid-debug   # slim + regex,llm + fake-llm
scripts/test-examples.sh --preset pentest      # pf + privacy_filter + pentest config
scripts/test-examples.sh                       # connect to BASE_URL (already-running guardrail)
```

## Repo layout

```
src/anonymizer_guardrail/        # the importable package — what ships
  detector/
    __init__.py                  # REGISTERED_SPECS + lookup tables
    spec.py                      # DetectorSpec dataclass
    base.py                      # Detector Protocol, Match, ENTITY_TYPES
    regex.py                     # Each detector module owns its
    denylist.py                  # CONFIG + SPEC + (optionally)
    llm.py                       # Unavailable error class.
    privacy_filter.py
    remote_privacy_filter.py
    remote_gliner_pii.py
  pipeline.py                    # Iterates REGISTERED_SPECS to build
  main.py                        # everything dynamic.
  config.py                      # Cross-cutting config only (vault,
                                 # surrogate, http server). Per-detector
                                 # config lives in the detector module.

tools/launcher/                  # dev-only, NOT in the wheel
  __main__.py                    # `python -m tools.launcher` entry
  spec_extras.py                 # LAUNCHER_METADATA (per-detector
                                 # service / env-passthrough metadata)
  engine.py                      # podman/docker dispatch
  services.py                    # auto-start lifecycle
  runner.py                      # build run argv + exec engine
  main.py                        # Click CLI
  menu.py                        # Textual TUI

scripts/                         # bash wrappers, ~5 lines each
  cli.sh                         # exec python -m tools.launcher
  menu.sh                        # exec python -m tools.launcher.menu
  build-image.sh                 # podman/docker build wrapper
  release.sh                     # git tag + push for CI

services/                        # auxiliary inference containers
  fake_llm/
  privacy_filter/
  gliner_pii/

tests/                           # pytest suite

docs/                            # operator-facing documentation
  configuration.md               # main env-var table
  litellm-integration.md         # LiteLLM wiring
  surrogates.md                  # surrogates / salt / Faker / locales
  per-request-overrides.md       # additional_provider_specific_params
  operations.md                  # vault, surrogate cache, /health
  deployment.md                  # container images, build, run
  development.md                 # this file
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
- `has_semaphore`, `stats_prefix`, `max_concurrency_field` — concurrency cap.
- `unavailable_error`, `fail_closed_field`, `blocked_reason` — failure mode.

**Per-detector `CONFIG`** — each detector module
(`detector/<name>.py`) defines its own frozen `<Name>Config` dataclass
and instantiates `CONFIG`. Cross-cutting fields (vault, surrogate,
http server, faker locale) stay on the central `Config` in `config.py`.

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

**`tools/launcher/spec_extras.LAUNCHER_METADATA`** — separately, the
launcher carries per-detector launcher metadata (auto-startable
service, env passthroughs, container/port). Lives outside the main
package because the launcher is dev-only.

## The launcher

Two thin bash wrappers (`scripts/cli.sh`, `scripts/menu.sh`)
exec into Python entry points under `tools/launcher/`. The launcher
is **dev-only** — its Python source lives outside `src/` and isn't
shipped in the production wheel.

- **`scripts/cli.sh`** → `python -m tools.launcher` → Click CLI.
  Single-command app with options grouped per detector via a
  custom `GroupedCommand` formatter (option-name columns aligned
  globally across sections).
- **`scripts/menu.sh`** → `python -m tools.launcher.menu` →
  Textual TUI. Single-screen menuconfig-style — General / one
  section per enabled detector / Faker. Arrow keys navigate;
  Space toggles checkboxes; Enter confirms; Ctrl+↑/↓ reorders
  detectors in the enable+order modal.

Both produce a `LaunchConfig` and hand it to
`tools/launcher/runner.run_guardrail()`, which composes the engine
argv from `REGISTERED_SPECS` + `LAUNCHER_METADATA` and exec's the
container in the foreground. Auto-startable services (fake-llm,
privacy-filter-service, gliner-pii-service) are started before the
guardrail and torn down via `atexit`.

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
    max_concurrency_field="max_concurrency",
    stats_prefix="my_detector",            # /health key prefix
    unavailable_error=MyDetectorUnavailableError,
    fail_closed_field="fail_closed",
    blocked_reason=(
        "My detector is unreachable; request blocked to prevent "
        "unredacted data from reaching the upstream model."
    ),
)
```

Optional `prepare_call_kwargs=fn` if `detect()` takes per-request
overrides from the `Overrides` dataclass — see
`detector/llm.py` for the shape (LLM passes `api_key`, `model`,
`prompt_name`).

### 4. Register the spec

`src/anonymizer_guardrail/detector/__init__.py` — append to
`REGISTERED_SPECS` in canonical priority order (regex → denylist →
privacy_filter → … → llm). Position matters: `_dedup` keeps the
first-seen entity_type for duplicate text matches, so the detector
listed first wins type-resolution conflicts.

```python
from .my_detector import SPEC as _MY_DETECTOR_SPEC

REGISTERED_SPECS = (
    _REGEX_SPEC,
    _DENYLIST_SPEC,
    _PRIVACY_FILTER_SPEC,
    _GLINER_PII_SPEC,
    _MY_DETECTOR_SPEC,            # ← here
    _LLM_SPEC,
)
```

**That's it for the runtime.** `Pipeline.__init__` picks up the new
spec automatically — its semaphore + counter are allocated, the log
line includes its fail-mode and cap, `_run` dispatches via
`SPECS_BY_NAME[det.name]`, and `main.py`'s BLOCKED handler maps the
typed error via `TYPED_UNAVAILABLE_ERRORS`. `/health` gains
`<stats_prefix>_in_flight` and `<stats_prefix>_max_concurrency` keys.
The per-request `detector_mode` length cap in `api.py` is also
derived from `len(REGISTERED_SPECS)`, so callers can pass the new
detector in their override list without bumping a constant.

### 5. Tests

Two files:

**`tests/test_my_detector.py`** — unit tests for the detector class.
Mock external deps (httpx, model, files). Copy the shape of
`tests/test_remote_gliner_pii.py`. Patch config via:

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

### 6. (If your detector has an auto-startable service container)

Five additional touches:

**a. `services/my_service/`** — new directory:

- `main.py` — FastAPI app exposing `POST /detect` and `GET /health`.
- `Containerfile` — build instructions. Mirror
  `services/privacy_filter/Containerfile` for shape.
- `README.md` — service-specific docs.

**b. `scripts/build-image.sh`** — add the new flavour:

- `TAG_MY_SERVICE` env-var default near the top.
- New entry in the interactive menu.
- New case in `resolve_flavour()`.
- Add to `BUILD_LIST` for `--type all` if appropriate.
- Post-build run hint.

**c. `tools/launcher/spec_extras.py`** — add a `LauncherSpec(...)`
entry to `LAUNCHER_METADATA`:

```python
"my_detector": LauncherSpec(
    guardrail_env_passthroughs=[
        "MY_DETECTOR_URL", "MY_DETECTOR_TIMEOUT_S",
        "MY_DETECTOR_FAIL_CLOSED", "MY_DETECTOR_MAX_CONCURRENCY",
    ],
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
),
```

This single entry powers: launcher service auto-start, env-var
forwarding to the guardrail container, network setup, /health probe,
atexit cleanup. No further bash/Python wiring for the lifecycle.

### 7. (If you want CLI / menu support)

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

### 8. (If you want documentation)

**`docs/detectors/my-detector.md`** — when to use it, env-var
reference, failure-mode notes. Mirror the shape of the existing
files in `docs/detectors/`.

**`docs/configuration.md`** — env-var table rows for every new
`MY_DETECTOR_*` knob.

**`services/my_service/README.md`** — service-specific docs (API,
build, run).

### TL;DR — files to touch

| Scope | Files |
|---|---|
| **In-process detector only** | 2 — new detector module + `detector/__init__.py` |
| **+ tests** | +1 to +2 — `tests/test_my_detector.py`, optional `tests/test_pipeline.py` |
| **+ service container** | +5 — `services/my_service/{main.py,Containerfile,README.md}`, `scripts/build-image.sh`, `tools/launcher/spec_extras.py` |
| **+ launcher CLI** | +1 — `tools/launcher/main.py` |
| **+ launcher menu** | +1 — `tools/launcher/menu.py` |
| **+ docs** | +1 to +2 — `docs/detectors/<name>.md`, optional row in `docs/configuration.md` |

What you don't have to touch: `pipeline.py`, `main.py`, `config.py`,
the bash wrapper scripts (`cli.sh` / `menu.sh`), or any "list of
detectors" enumeration. They all consume `REGISTERED_SPECS` /
`LAUNCHER_METADATA` and adapt automatically.

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

- [TASKS.md](../TASKS.md) — backlog of deliberately deferred work, with
  rationale for each deferral. Useful before starting on something
  that might already have a documented design sketch.
- [README.md](../README.md) — project landing page; links into the
  rest of `docs/`.
- [services/privacy_filter/README.md](../services/privacy_filter/README.md)
  and [services/gliner_pii/README.md](../services/gliner_pii/README.md) —
  reference shape for new auxiliary inference services.
