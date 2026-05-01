"""Container-engine helpers (podman / docker dispatch).

Mirrors the bash `_lib.sh` predicates / side-effect helpers, but in
Python so the typer / questionary CLI can call them directly. Every
helper shells out to `podman` or `docker` — we don't bind to libpod /
docker-py because (a) those are heavy deps and (b) the surface we use
is tiny (image inspect, volume create, run -d, etc.).

`detect_engine()` is called once at launcher startup; the resulting
engine name is passed around via the `Engine` dataclass that wraps
the subprocess invocations in clear methods.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Engine:
    """Resolved container engine binary, plus subprocess helpers.

    Construction calls `detect_engine()` which prefers podman (rootless
    by default, the project's deployment baseline) and falls back to
    docker. Operators can force one via `ENGINE=docker python -m
    tools.launcher …` — `detect_engine` honours the env override.
    """

    name: str  # "podman" or "docker"

    # ── Predicates ─────────────────────────────────────────────────────────
    def image_exists(self, tag: str) -> bool:
        return self._inspect("image", tag)

    def container_exists(self, name: str) -> bool:
        return self._inspect("container", name)

    def container_running(self, name: str) -> bool:
        """True iff the container exists AND is in `running` state."""
        try:
            out = subprocess.run(
                [self.name, "container", "inspect", "-f", "{{.State.Status}}", name],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return False
        if out.returncode != 0:
            return False
        return out.stdout.strip() == "running"

    def volume_exists(self, name: str) -> bool:
        return self._inspect("volume", name)

    def network_exists(self, name: str) -> bool:
        return self._inspect("network", name)

    def _inspect(self, kind: str, name: str) -> bool:
        try:
            r = subprocess.run(
                [self.name, kind, "inspect", name],
                capture_output=True, check=False,
            )
        except FileNotFoundError:
            return False
        return r.returncode == 0

    # ── Side-effect helpers ────────────────────────────────────────────────
    def ensure_network(self, name: str) -> None:
        """Create the shared network if missing. Idempotent."""
        if not self.network_exists(name):
            subprocess.run(
                [self.name, "network", "create", name],
                check=True, capture_output=True,
            )

    def ensure_volume(self, name: str) -> bool:
        """Create the named volume if missing. Returns True iff the
        volume was just created (vs already existed) — callers use
        the flag to decide whether to print "first run, will fetch
        weights" warnings."""
        if self.volume_exists(name):
            return False
        subprocess.run(
            [self.name, "volume", "create", name],
            check=True, capture_output=True,
        )
        return True

    def remove_container(self, name: str) -> None:
        """Force-remove a container. Used for replace-on-conflict and
        when an existing-but-stopped container would block start."""
        subprocess.run(
            [self.name, "rm", "-f", name],
            check=False, capture_output=True,
        )

    def stop_container(self, name: str) -> None:
        """Best-effort stop. Cleanup paths swallow errors — by the time
        we're stopping, we don't want a missing container to crash the
        teardown."""
        subprocess.run(
            [self.name, "stop", name],
            check=False, capture_output=True,
        )

    def exec_health_probe(self, container: str, port: int, endpoint: str, timeout: int = 2) -> tuple[bool, str]:
        """Probe `http://127.0.0.1:<port><endpoint>` from INSIDE the
        container. Avoids host-vs-network routing edge cases that bit
        the bash version (running on host where the container's
        published port might not be accessible).

        Returns (success, body). Body is empty on connection failure;
        non-empty string when we got a response (regardless of status).
        Caller checks `health_ok_substring in body` to decide ready vs
        loading.
        """
        # Use python3 inside the container so we don't have to rely on
        # `curl` being installed (privacy_filter / gliner images do
        # ship curl for HEALTHCHECK, but fake-llm doesn't necessarily).
        probe = (
            "import urllib.request, sys; "
            f"r = urllib.request.urlopen('http://127.0.0.1:{port}{endpoint}', timeout={timeout}); "
            "sys.stdout.write(r.read().decode('utf-8', 'replace'))"
        )
        try:
            r = subprocess.run(
                [self.name, "exec", container, "python3", "-c", probe],
                capture_output=True, text=True, check=False, timeout=timeout + 2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False, ""
        if r.returncode != 0:
            return False, ""
        return True, r.stdout


def detect_engine() -> Engine:
    """Return the operator-preferred container engine.

    Priority:
      1. ENGINE env var (operator override; values: podman | docker).
      2. podman if installed (project's rootless-friendly baseline).
      3. docker as fallback.

    Raises RuntimeError when neither is in PATH — same fail-loud
    behaviour as the bash version.
    """
    import os

    forced = os.environ.get("ENGINE", "").strip().lower()
    if forced:
        if forced not in ("podman", "docker"):
            raise RuntimeError(
                f"ENGINE={forced!r} is not recognized. Use 'podman' or 'docker'."
            )
        if not shutil.which(forced):
            raise RuntimeError(
                f"ENGINE={forced!r} forced via env, but the binary isn't in PATH."
            )
        return Engine(name=forced)

    if shutil.which("podman"):
        return Engine(name="podman")
    if shutil.which("docker"):
        return Engine(name="docker")
    raise RuntimeError(
        "Neither podman nor docker found in PATH. Install one or set ENGINE=…"
    )


__all__ = ["Engine", "detect_engine"]
