"""Auto-startable service lifecycle.

Generic dispatcher driven by `LAUNCHER_METADATA[name].service` —
no per-detector special cases. Adding a new detector with a service:
populate its `LAUNCHER_SPEC = LauncherSpec(service=ServiceSpec(...))`
in the detector module (alongside `CONFIG` and `SPEC`) and the
lifecycle helpers below pick it up automatically. `LAUNCHER_METADATA`
is aggregated in `anonymizer_guardrail.detector.__init__` and
re-exported via `tools/launcher/spec_extras.py` for in-package use.

Three responsibilities:

  * `start_service(engine, name)` — create the shared network if
    missing, create the HF cache volume if missing, pick the best
    available image tag, run the container in detached mode, poll
    `/health` until ready (or fail loudly with a deadline message).

  * `cleanup_service(engine, name)` — stop the container if WE
    started it (tracked via `_STARTED_SERVICES`). Idempotent —
    safe to call when the operator started the container themselves
    (we don't tear down what we don't own).

  * `register_atexit_cleanup(engine)` — installs an `atexit` handler
    that fires `cleanup_service` for every service we started. The
    bash version used a single EXIT trap target; Python's atexit
    accepts multiple callbacks but we still funnel through one
    handler so test harnesses can disable cleanup atomically.
"""

from __future__ import annotations

import atexit
import os
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from rich.console import Console

from .engine import Engine

if TYPE_CHECKING:
    # Type-only import — runtime would create a cycle (runner.py
    # imports services.py). The helper below takes a LaunchConfig
    # by reference, so the forward reference is enough.
    from .runner import LaunchConfig
from .spec_extras import (
    LAUNCHER_METADATA,
    SHARED_NETWORK,
    LauncherSpec,
    ServiceSpec,
    get_image_tag,
)


# ── Redis infrastructure service ──────────────────────────────────────
# Single shared Redis container the operator opts into via
# `redis_backend=service`. Used as the backing store for VAULT (when
# `VAULT_BACKEND=redis`) and for the per-detector result caches (when
# any `<DETECTOR>_CACHE_BACKEND=redis`). Distinct logical DB indices
# (vault → /0, cache → /1) keep the two namespaces from colliding so
# operators can `FLUSHDB` one without wiping the other.
#
# Defined here (not in `LAUNCHER_METADATA` which is keyed by detector
# name) because Redis isn't a detector — it's a peer infrastructure
# dependency. The lifecycle is identical to a detector service though,
# so it reuses `ServiceSpec` and feeds through the same start/stop
# machinery.
_REDIS_SERVICE = ServiceSpec(
    container_name="anonymizer-redis",
    image_tag_envs=("TAG_REDIS",),
    image_tag_defaults=("redis:7-alpine",),
    port=6379,
    # `redis-cli ping` returns "PONG" + exit 0 once the server is up.
    # No HTTP, so we use the command-based probe path.
    health_command=("redis-cli", "ping"),
    readiness_timeout_s=15,
    # No HF cache; redis state lives in memory by default. Operators
    # wanting persistence mount their own volume (`-v <name>:/data`)
    # via `--` extras to launcher.sh.
    hf_cache_volume=None,
)


# Sentinel name for the redis container in `_STARTED_SERVICES` and
# the auto-start loop. Distinct from any detector name so the runner
# loop can iterate `cfg.backends` for detectors and check redis
# separately without false collisions.
_REDIS_NAME = "_redis_infra"


# stderr console — same rationale as runner._console: keep our status
# output off the guardrail container's stdout stream.
_console = Console(stderr=True)

# Set of detector names whose service WE started (vs reused operator's).
# Cleanup teardown reads this so we don't stop containers the operator
# was already running.
_STARTED_SERVICES: set[str] = set()


def _resolve_image(engine: Engine, service: ServiceSpec) -> tuple[str, str]:
    """Pick the first image tag in `image_tag_envs` that's actually
    built locally. Falls back to the LAST default (typically the
    runtime-download flavour) when nothing matches, so the run will
    fail loudly with a clear "image not built" error from the engine
    rather than silently picking something inappropriate.

    Returns (tag, kind) where kind is 'baked' or 'runtime-download' or
    'fallback'. Used for the operator-facing log line.
    """
    for env_name, default in zip(service.image_tag_envs, service.image_tag_defaults):
        candidate = os.environ.get(env_name) or default
        if engine.image_exists(candidate):
            kind = "baked" if "baked" in candidate else "runtime-download"
            return candidate, kind
    # Nothing built. Return the LAST default so the eventual `engine run`
    # fails with a recognisable tag in the error message.
    return service.image_tag_defaults[-1], "fallback"


# Tag substrings that mark an image as CUDA-built. The image_builder's
# cu130 flavours emit tags like `:cu130` and `:baked-cu130`; future
# CUDA versions (cu140, etc.) get caught by the `cu1` prefix.
# Operators with custom tag schemes who want GPU exposure on a tag
# we don't recognise can override via `<NAME>_USE_GPU=1` (handled below).
_GPU_TAG_MARKERS: tuple[str, ...] = (":cu1", "-cu1")


def _image_uses_gpu(image: str) -> bool:
    """True iff the resolved image tag looks like a CUDA build. The
    launcher emits `--device nvidia.com/gpu=all` (podman) or
    `--gpus all` (docker) when this returns True so the container can
    see the GPU. Tag-substring detection over a separate spec field
    because the LauncherSpec doesn't know which flavour the operator
    picked at runtime — image_tag_envs returns whichever variant is
    actually built on this host."""
    return any(marker in image for marker in _GPU_TAG_MARKERS)


def start_service(
    engine: Engine,
    name: str,
    log_level: str = "info",
    extra_volumes: Iterable[tuple[str, str]] | None = None,
    variant: str | None = None,
) -> None:
    """Auto-start the service container for detector `name`.

    Idempotent: if the container is already running, reuses it (and
    does NOT register cleanup — we don't tear down what we don't own).
    If a stopped container with the same name exists, removes it
    first so the run can claim the name.

    Reads operator-side env vars per `service.service_env_passthroughs`
    (e.g. GLINER_PII_LABELS → DEFAULT_LABELS) and forwards them to the
    service container. Polls /health until ready or the
    `readiness_timeout_s` deadline fires.

    `extra_volumes` is an optional list of (host_path, container_path)
    pairs. The container_path may include a mode suffix (e.g.
    ``/app/rules.yaml:ro``). Used today to mount a custom fake-llm
    rules file when the operator passes ``--rules``; kept generic so
    other services can opt in without adding a second special case.

    `variant` selects an alternative service implementation when the
    detector ships more than one (e.g. privacy_filter has an opf-only
    default and a `hf` variant). None / empty → the default. Unknown
    variants fall back to the default (the CLI / menu validate the
    choice list, so this is just programmer-error tolerance).
    """
    spec = LAUNCHER_METADATA.get(name)
    if spec is None or spec.service is None:
        raise RuntimeError(
            f"Detector {name!r} has no service to auto-start. "
            f"Check the detector module's LAUNCHER_SPEC "
            f"(in src/anonymizer_guardrail/detector/<name>.py)."
        )
    service = spec.resolve_service(variant)
    assert service is not None  # spec.service was non-None above

    engine.ensure_network(SHARED_NETWORK)

    if engine.container_exists(service.container_name):
        if engine.container_running(service.container_name):
            _console.print(
                f"[green]{service.container_name} already running — "
                f"reusing it (will NOT stop on exit).[/green]",
            )
            return
        _console.print(
            f"[yellow]{service.container_name} exists but isn't running — removing.[/yellow]",
        )
        engine.remove_container(service.container_name)

    image, kind = _resolve_image(engine, service)
    if kind == "fallback":
        # Don't raise — let the engine's `run` produce the canonical
        # "image not found" error. Print a hint so the operator knows
        # which build command to invoke.
        _console.print(
            f"[red]No {service.container_name} image found locally. "
            f"Build it with `scripts/image_builder.sh -f <flavour>` "
            f"(see {' / '.join(service.image_tag_envs)} for tag candidates).[/red]",
        )

    cmd: list[str] = [
        engine.name, "run", "-d",
        "--name", service.container_name,
        "--network", SHARED_NETWORK,
        "-p", f"{service.port}:{service.port}",
        "-e", f"LOG_LEVEL={log_level}",
    ]
    # Deliberately NO `--rm`: when a service crashes during startup
    # we want the corpse to stick around so `_wait_for_health` can
    # capture its exit logs (and the operator can `<engine> inspect`
    # it for further forensics). Cleanup happens via `cleanup_service`
    # below — atexit force-removes the container on graceful shutdown,
    # and the existing "container exists but isn't running → remove"
    # branch at the top of this function handles a leftover corpse on
    # the next launcher run.

    # GPU exposure — when the resolved image tag suggests a CUDA build
    # (`:cu130`, `:baked-cu130`, etc.), emit the engine-specific flag
    # so the container can see the GPU. The host needs nvidia-container-
    # toolkit installed; without it the run fails loud at start. CPU
    # images skip this branch entirely.
    if _image_uses_gpu(image):
        if engine.name == "podman":
            cmd.extend(["--device", "nvidia.com/gpu=all"])
        else:  # docker
            cmd.extend(["--gpus", "all"])
        _console.print(
            f"[yellow]Image {image!r} requests GPU access. Host needs "
            f"nvidia-container-toolkit; failing here would have been "
            f"a confusing 'cuda not available' at first request.[/yellow]",
        )

    # HF cache volume — only for services that need persistent model
    # weights. Create on first run and warn that the first start will
    # be slow (model download).
    if service.hf_cache_volume:
        just_created = engine.ensure_volume(service.hf_cache_volume)
        cmd.extend(["-v", f"{service.hf_cache_volume}:{service.cache_mount_path}"])
        if just_created and kind != "baked":
            _console.print(
                f"[yellow]First run: model will be fetched from HuggingFace "
                f"into volume \"{service.hf_cache_volume}\". This can take "
                f"several minutes on a slow connection.[/yellow]",
            )

    # Forward operator-side env vars into the service. The mapping
    # lives on the spec so we don't have per-detector special cases
    # here. E.g. GLINER_PII_LABELS → DEFAULT_LABELS in the gliner
    # service, so the operator sets one var and both ends agree.
    for service_var, launcher_var in service.service_env_passthroughs.items():
        val = os.environ.get(launcher_var, "")
        if val:
            cmd.extend(["-e", f"{service_var}={val}"])

    # Caller-supplied bind mounts (e.g. fake-llm's rules file from
    # `--rules`). Path validation is the caller's job — we just emit
    # the `-v host:container[:mode]` flags.
    if extra_volumes:
        for host, container in extra_volumes:
            cmd.extend(["-v", f"{host}:{container}"])

    cmd.append(image)

    _console.print(
        f"[green]Starting {service.container_name} ({kind}) on network "
        f"\"{SHARED_NETWORK}\" (port {service.port})…[/green]",
    )

    import subprocess
    subprocess.run(cmd, check=True, capture_output=True)
    _STARTED_SERVICES.add(name)

    _wait_for_health(engine, service)


def _wait_for_health(engine: Engine, service: ServiceSpec) -> None:
    """Poll the service's /health endpoint until it returns the
    ready substring or the deadline fires. Prints a periodic
    progress hint so a multi-minute model download doesn't look
    like a hang."""
    timeout_override = os.environ.get(
        f"{service.container_name.upper().replace('-', '_')}_READY_TIMEOUT_S"
    )
    timeout_s = int(timeout_override) if timeout_override else service.readiness_timeout_s

    _console.print(
        f"Waiting for {service.container_name} to be ready "
        f"(timeout: {timeout_s}s)…",
    )

    deadline = time.time() + timeout_s
    last_progress = time.time()
    started_at = time.time()
    while True:
        now = time.time()
        if now >= deadline:
            _console.print(
                f"[red]{service.container_name} did not become ready "
                f"within {timeout_s}s. Check: {engine.name} logs "
                f"{service.container_name}[/red]",
            )
            raise SystemExit(1)

        # Detect a dead container BEFORE polling /health. If the
        # container exited (crashed during startup, OOM-killed,
        # uvicorn import error, etc.) the /health probe will just
        # quietly fail forever and the operator sees nothing useful
        # — until pre-fix the loop would print "still loading" until
        # the timeout fires, leaving the operator to chase a
        # non-existent network issue. Catching the exit state up
        # front lets us surface the actual error from the container's
        # own log tail.
        if not engine.container_running(service.container_name):
            _print_dead_container_diagnostics(engine, service)
            raise SystemExit(1)

        if service.health_command is not None:
            # Command-based probe (e.g. Redis: `redis-cli ping` →
            # exit 0 + "PONG"). Used for non-HTTP services where
            # the standard /health probe can't reach.
            ok, body = engine.exec_command(
                service.container_name, service.health_command,
            )
        else:
            ok, body = engine.exec_health_probe(
                service.container_name,
                service.port,
                service.health_endpoint,
            )
        if ok and (
            service.health_command is not None
            or service.health_ok_substring in body
        ):
            _console.print(f"[green]{service.container_name} is ready.[/green]")
            return
        # Periodic progress: reassures the operator the launcher is
        # still alive during a multi-minute model fetch.
        if now - last_progress >= 20:
            elapsed = int(now - started_at)
            _console.print(f"  [dim]still loading (elapsed: {elapsed}s)[/dim]")
            last_progress = now
        time.sleep(1)


def _print_dead_container_diagnostics(engine: Engine, service: ServiceSpec) -> None:
    """Capture and emit the dead container's last log lines so the
    operator sees WHY it exited rather than chasing a generic
    'didn't become ready' timeout. The container is left in place
    (we run without `--rm`) so further forensics like
    `<engine> inspect <name>` are still possible; the next launcher
    run's name-collision handling reaps it on cleanup."""
    _console.print(
        f"[red]{service.container_name} exited before becoming ready. "
        f"Last log lines:[/red]",
    )
    import subprocess
    result = subprocess.run(
        [engine.name, "logs", "--tail", "30", service.container_name],
        capture_output=True, text=True, check=False,
    )
    # Some engines print logs to stderr, others to stdout — capture both.
    output = (result.stdout or "") + (result.stderr or "")
    if output.strip():
        for line in output.splitlines():
            _console.print(f"  [dim]{line}[/dim]")
    else:
        _console.print(
            f"  [dim](no logs captured — {engine.name} logs returned empty)[/dim]",
        )
    _console.print(
        f"[yellow]Container left in place for inspection. "
        f"Run `{engine.name} rm -f {service.container_name}` when done, "
        f"or re-run the launcher to retry (it will reap the corpse "
        f"automatically).[/yellow]",
    )


def start_redis(engine: Engine) -> None:
    """Auto-start the shared Redis infrastructure container. Behaves
    like `start_service` but for a non-detector peer service. Tracked
    in `_STARTED_SERVICES` under the `_REDIS_NAME` sentinel so the
    atexit cleanup path tears it down.

    The container joins `SHARED_NETWORK` so the guardrail can dial it
    at `redis://anonymizer-redis:6379/<db>`. Idempotent: a running
    redis with the same name is reused (and NOT torn down on exit —
    we don't stop containers we didn't start)."""
    service = _REDIS_SERVICE

    engine.ensure_network(SHARED_NETWORK)

    if engine.container_exists(service.container_name):
        if engine.container_running(service.container_name):
            _console.print(
                f"[green]{service.container_name} already running — "
                f"reusing it (will NOT stop on exit).[/green]",
            )
            return
        _console.print(
            f"[yellow]{service.container_name} exists but isn't running — "
            f"removing.[/yellow]",
        )
        engine.remove_container(service.container_name)

    image, _kind = _resolve_image(engine, service)
    _console.print(
        f"Starting {service.container_name} on network "
        f"\"{SHARED_NETWORK}\" (port {service.port})…",
    )

    cmd = [
        engine.name, "run", "-d",
        "--name", service.container_name,
        "--network", SHARED_NETWORK,
        "-p", f"{service.port}:{service.port}",
        image,
    ]
    import subprocess
    subprocess.run(cmd, check=True, capture_output=True)
    _STARTED_SERVICES.add(_REDIS_NAME)

    _wait_for_health(engine, service)


def cleanup_redis(engine: Engine) -> None:
    """Stop and remove the redis container if WE started it. Mirrors
    `cleanup_service` for the detector side — distinct because the
    redis container isn't keyed by a detector name in
    `LAUNCHER_METADATA`."""
    if _REDIS_NAME not in _STARTED_SERVICES:
        return
    _console.print(
        f"\nStopping auto-started {_REDIS_SERVICE.container_name}…",
    )
    engine.stop_container(_REDIS_SERVICE.container_name)
    engine.remove_container(_REDIS_SERVICE.container_name)


def redis_was_started() -> bool:
    """Read-only check used by the run-argv composer to decide whether
    to inject `VAULT_REDIS_URL` / `CACHE_REDIS_URL` into the guardrail
    env. Mirrors `started_services()` for the detector side."""
    return _REDIS_NAME in _STARTED_SERVICES


def auto_start_services(
    engine: Engine,
    cfg: "LaunchConfig",
    *,
    extra_volumes: dict[str, list[tuple[str, str]]] | None = None,
) -> list[str]:
    """Iterate `cfg.backends` and start every detector with
    `backend="service"`. Returns the list of started detector names so
    the caller can pass it to `register_atexit_cleanup`.

    Also auto-starts the shared Redis container when
    `cfg.redis_backend == "service"` — done in this same helper so
    the CLI / TUI don't have to remember a separate Redis-start step.

    Honours `cfg.service_variants` for variant resolution (e.g.
    `privacy_filter` → `hf` picks `privacy-filter-hf-service`). Forwards
    `cfg.log_level`. Optional `extra_volumes` lets the caller mount
    additional bind volumes per detector — used by the CLI for
    `--rules` on the auto-started fake-llm; the TUI doesn't expose
    that knob today and passes None.

    `SystemExit` from a failed health-check inside `start_service`
    propagates. The CLI lets Click handle it; the TUI catches it
    around the call so it can return a clean exit code from
    `run_interactive`.
    """
    extra_volumes = extra_volumes or {}
    started: list[str] = []
    # Start Redis FIRST so by the time detector services / the
    # guardrail come up they can dial it. (Order matters less than
    # network reachability — Redis is fast-ready (~1s) so any
    # detector race is benign — but starting it first keeps the
    # operator-facing log lines in a sensible order.)
    if getattr(cfg, "redis_backend", "") == "service":
        start_redis(engine)
        started.append(_REDIS_NAME)
    for det_name, backend in cfg.backends.items():
        if backend != "service":
            continue
        start_service(
            engine, det_name,
            log_level=cfg.log_level,
            extra_volumes=extra_volumes.get(det_name) or None,
            variant=cfg.service_variants.get(det_name) or None,
        )
        started.append(det_name)
    return started


def cleanup_service(engine: Engine, name: str) -> None:
    """Stop and remove the service container if WE started it. No-op
    otherwise.

    Removal is necessary because we run without `--rm` (so dead
    containers stay around for diagnostics). On graceful shutdown
    we want to leave a clean slate; on hard kill, the next launcher
    run's name-collision handling reaps the corpse.
    """
    if name not in _STARTED_SERVICES:
        return
    spec = LAUNCHER_METADATA.get(name)
    if spec is None or spec.service is None:
        return
    _console.print(
        f"\nStopping auto-started {spec.service.container_name}…",
    )
    engine.stop_container(spec.service.container_name)
    engine.remove_container(spec.service.container_name)


def register_atexit_cleanup(engine: Engine, names: Iterable[str]) -> None:
    """Install an atexit handler that tears down every service we
    auto-started. Single registration call covers all relevant
    services so the teardown ordering is deterministic.

    `names` may include the `_REDIS_NAME` sentinel (when the launcher
    auto-started Redis); the handler routes that one through
    `cleanup_redis` instead of `cleanup_service` because the redis
    container isn't keyed by a detector name in `LAUNCHER_METADATA`.
    Detectors stop first so any in-flight cache writes complete
    before Redis goes away."""
    names = list(names)

    def _cleanup() -> None:
        for n in names:
            if n == _REDIS_NAME:
                cleanup_redis(engine)
            else:
                cleanup_service(engine, n)

    atexit.register(_cleanup)


def started_services() -> set[str]:
    """Read-only view of which services this process auto-started.
    Used by the run-command composer to decide which service URLs to
    inject into the guardrail's env."""
    return frozenset(_STARTED_SERVICES)


__all__ = [
    "auto_start_services",
    "start_service",
    "start_redis",
    "cleanup_service",
    "cleanup_redis",
    "register_atexit_cleanup",
    "started_services",
    "redis_was_started",
]
