"""Launcher metadata dataclasses (`LauncherSpec`, `ServiceSpec`).

Shipped in the production wheel even though only the dev-only launcher
(`tools/launcher/`) reads them. The dataclasses themselves are pure
stdlib (no `click` / `textual` / `rich` deps), so the wheel cost is a
hundred lines of pure Python — and in exchange, each detector module
can declare its launcher metadata next to its `CONFIG` and `SPEC`,
removing the parallel `LAUNCHER_METADATA` table that used to live in
`tools/launcher/spec_extras.py`.

The detector module owns:

  * `CONFIG` — env-var-driven settings model (read at runtime by
    pipeline / main.py)
  * `SPEC` — `DetectorSpec` describing pipeline-side wiring (semaphore,
    typed error, blocked reason, …)
  * `LAUNCHER_SPEC` — `LauncherSpec` describing launcher-side wiring
    (env passthroughs, optional auto-startable service)

`detector/__init__.py` aggregates each module's `LAUNCHER_SPEC` into a
`LAUNCHER_METADATA` dict keyed by the SPEC's `name`. The launcher imports
that dict (along with `LauncherSpec` / `ServiceSpec` for type hints).

Cross-cutting launcher constants (`SHARED_NETWORK`,
`CONTAINER_NAME_DEFAULT`) and `os.environ`-touching helpers
(`get_image_tag`) stay in `tools/launcher/spec_extras.py` — those are
launcher-runtime concerns, not detector definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServiceSpec:
    """Metadata for an auto-startable service container.

    Used when the operator picks `backend=service` for a detector that
    has one (LLM → fake-llm, privacy_filter → privacy-filter-service,
    gliner_pii → gliner-pii-service). The launcher uses this to:

      * pick which image tag to look for,
      * publish the right port,
      * mount the right HF cache volume,
      * forward operator-side env vars into the service,
      * set the matching env vars on the guardrail so it points at
        the just-started service.
    """

    container_name: str
    """Docker/Podman container name. Also acts as the hostname on the
    shared `anonymizer-net` network so the guardrail can dial it."""

    image_tag_envs: tuple[str, ...]
    """Env var names to consult for the image tag, in preference order.
    The launcher tries each in turn and uses the first whose env value
    points at an image that actually exists locally. Falls back to
    `image_tag_defaults` (same index) when the env var is unset.

    For services with a runtime-download/baked split (PF, gliner_pii)
    the baked variant is listed first — when both are built the launcher
    prefers baked so the model isn't re-downloaded on first run."""

    image_tag_defaults: tuple[str, ...]
    """Default tag values when the matching `image_tag_envs` var is
    unset. Length must match `image_tag_envs`."""

    port: int
    """Port the service listens on. Published on the host AND used as
    `<container_name>:<port>` for the URL the guardrail dials."""

    health_endpoint: str = "/health"
    """HTTP path the readiness probe hits. The probe expects a 200
    response whose body contains `health_ok_substring`."""

    health_ok_substring: str = '"status":"ok"'
    """Substring the readiness probe looks for in the /health response.
    The default matches what the privacy_filter / gliner_pii services
    return when the model has finished loading."""

    readiness_timeout_s: int = 30
    """Upper bound (seconds) the launcher waits for the service to
    become healthy. Generous default for ML-loading services like
    privacy_filter / gliner_pii (which can take minutes on a cold
    runtime-download). Overrides via `<NAME>_READY_TIMEOUT_S` env
    are honoured by the readiness probe in services.py."""

    hf_cache_volume: str | None = None
    """Named volume to mount inside the container for persistent model
    weights. Mount path is `cache_mount_path` (default
    `/app/.cache/huggingface`). None means the service doesn't need a
    persistent cache (e.g. fake-llm). Different services use different
    volumes so a `volume rm` of one doesn't wipe the other."""

    cache_mount_path: str = "/app/.cache/huggingface"
    """Where inside the container to mount `hf_cache_volume`. Default
    is the standard HuggingFace cache path used by transformers-based
    services (gliner-pii, fake-llm). Privacy-filter overrides to
    `/app/.opf` because opf writes its checkpoint into `~/.opf/` and
    bypasses HF_HOME — see services/privacy_filter/Containerfile for
    the rationale. Ignored when hf_cache_volume is None."""

    guardrail_env_when_started: dict[str, str] = field(default_factory=dict)
    """Env vars the launcher sets on the GUARDRAIL container when this
    service is auto-started, so the guardrail talks to the just-spawned
    service. E.g. PF sets `PRIVACY_FILTER_URL=http://privacy-filter-service:8001`,
    LLM sets `LLM_API_BASE=http://fake-llm:4000/v1`, `LLM_API_KEY`,
    `LLM_MODEL`."""

    service_env_passthroughs: dict[str, str] = field(default_factory=dict)
    """Env vars the launcher forwards to the SERVICE container itself.
    Format: `{service_env_var: launcher_global_env_var}`. E.g.
    `{"DEFAULT_LABELS": "GLINER_PII_LABELS"}` — the launcher reads
    GLINER_PII_LABELS from its own environment (set by the operator
    via flag or env) and emits `-e DEFAULT_LABELS=<value>` on the
    service container. Lets the operator tune the service via the
    same vars they'd set client-side, no separate flags."""


@dataclass(frozen=True)
class LauncherSpec:
    """All launcher-side metadata for one detector.

    Composed with `DetectorSpec` (same module) by `detector/__init__.py`,
    which builds `LAUNCHER_METADATA: dict[str, LauncherSpec]` keyed by
    `SPEC.name`. Detectors without a `LAUNCHER_SPEC` constant still work
    — the launcher just won't auto-start anything for them and won't
    pass detector-specific env vars (the central guardrail Config
    defaults apply)."""

    guardrail_env_passthroughs: list[str] = field(default_factory=list)
    """Env vars from the operator's environment to forward to the
    GUARDRAIL container as `-e <var>=<value>`. Only emitted when the
    var is non-empty — keeps the env clean at default settings.

    Conventionally lists every `<DETECTOR>_*` knob from the matching
    per-detector CONFIG dataclass, so an operator who exports
    GLINER_PII_LABELS in their shell automatically gets it on the
    container without a flag."""

    service: ServiceSpec | None = None
    """Optional auto-startable service — the *default* variant. None
    for in-process detectors (regex, denylist)."""

    service_variants: dict[str, ServiceSpec] = field(default_factory=dict)
    """Named alternative ServiceSpecs for detectors that ship more
    than one backing service (e.g. privacy_filter has an opf-only and
    an HF-pipeline variant). The launcher's `--<name>-variant` flag
    selects from this dict; absent / empty means there's only the
    default `service` and the variant flag is a no-op."""

    def resolve_service(self, variant: str | None = None) -> ServiceSpec | None:
        """Pick the ServiceSpec to use for an auto-start.

        - `variant` is None or empty → return `self.service` (the
          default).
        - `variant` matches a key in `service_variants` → return that
          alternative.
        - Unknown variant → fall back to `self.service` (the launcher
          CLI / menu validates the choice list, so this branch only
          fires under programmer error).
        """
        if variant and variant in self.service_variants:
            return self.service_variants[variant]
        return self.service


__all__ = ["LauncherSpec", "ServiceSpec"]
