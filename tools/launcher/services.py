"""Auto-startable service lifecycle.

Generic dispatcher driven by `LAUNCHER_METADATA[name].service` —
no per-detector special cases. Adding a new detector with a service:
populate its `ServiceSpec` in `spec_extras.py` and the lifecycle
helpers below pick it up automatically.

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

from rich.console import Console

from .engine import Engine
from .spec_extras import (
    LAUNCHER_METADATA,
    SHARED_NETWORK,
    LauncherSpec,
    ServiceSpec,
    get_image_tag,
)


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
            f"Check LAUNCHER_METADATA in tools/launcher/spec_extras.py."
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

        ok, body = engine.exec_health_probe(
            service.container_name,
            service.port,
            service.health_endpoint,
        )
        if ok and service.health_ok_substring in body:
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
    services so the teardown ordering is deterministic."""
    names = list(names)

    def _cleanup() -> None:
        for n in names:
            cleanup_service(engine, n)

    atexit.register(_cleanup)


def started_services() -> set[str]:
    """Read-only view of which services this process auto-started.
    Used by the run-command composer to decide which service URLs to
    inject into the guardrail's env."""
    return frozenset(_STARTED_SERVICES)


__all__ = [
    "start_service",
    "cleanup_service",
    "register_atexit_cleanup",
    "started_services",
]
