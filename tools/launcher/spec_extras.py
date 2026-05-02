"""Per-detector launcher metadata.

Lives outside the main package (`src/anonymizer_guardrail/`) so the
production wheel and the slim guardrail container don't pick up the
launcher's deps (typer, questionary, etc.). Operators install this via
`pip install -e ".[dev]"` from a checkout when they want the launcher.

The launcher consumes `anonymizer_guardrail.detector.REGISTERED_SPECS`
to know which detectors exist and how to dispatch them. This module
adds the launcher-only knowledge: which detectors have an auto-startable
service container, what env vars get forwarded to the guardrail, what
URLs the auto-started services live at.

**Adding a new detector**: add a `LauncherSpec(...)` entry to
`LAUNCHER_METADATA` keyed by `SPEC.name`. If the detector has no
service to auto-start (regex / denylist), only the
`guardrail_env_passthroughs` field matters.

The pattern stays out of `src/anonymizer_guardrail/detector/spec.py`
because that file ships in the production wheel — putting launcher
concerns there would either pollute the prod image or force an
unwanted import-graph split.
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

    Composed with `DetectorSpec` (in the main package) by name lookup.
    Detectors that don't appear in LAUNCHER_METADATA still work — the
    launcher just won't auto-start anything for them and won't pass
    detector-specific env vars (the central guardrail Config defaults
    apply)."""

    guardrail_env_passthroughs: list[str] = field(default_factory=list)
    """Env vars from the operator's environment to forward to the
    GUARDRAIL container as `-e <var>=<value>`. Only emitted when the
    var is non-empty — keeps the env clean at default settings.

    Conventionally lists every `<DETECTOR>_*` knob from the matching
    per-detector CONFIG dataclass, so an operator who exports
    GLINER_PII_LABELS in their shell automatically gets it on the
    container without a flag."""

    service: ServiceSpec | None = None
    """Optional auto-startable service. None for in-process detectors
    (regex, denylist) and for the in-process privacy_filter on
    pf/pf-baked images."""


# ── Per-detector metadata ─────────────────────────────────────────────────
# Order doesn't matter (lookup is by name); kept in canonical priority
# order to match REGISTERED_SPECS for visual consistency.
LAUNCHER_METADATA: dict[str, LauncherSpec] = {
    "regex": LauncherSpec(
        guardrail_env_passthroughs=[
            "REGEX_PATTERNS_PATH",
            "REGEX_PATTERNS_REGISTRY",
            "REGEX_OVERLAP_STRATEGY",
        ],
    ),
    "denylist": LauncherSpec(
        guardrail_env_passthroughs=[
            "DENYLIST_PATH",
            "DENYLIST_REGISTRY",
            "DENYLIST_BACKEND",
        ],
    ),
    "privacy_filter": LauncherSpec(
        guardrail_env_passthroughs=[
            "PRIVACY_FILTER_URL",
            "PRIVACY_FILTER_TIMEOUT_S",
            "PRIVACY_FILTER_FAIL_CLOSED",
            "PRIVACY_FILTER_MAX_CONCURRENCY",
        ],
        service=ServiceSpec(
            container_name="privacy-filter-service",
            # Prefer baked (model in image) over runtime-download —
            # avoids the multi-minute first-start fetch.
            image_tag_envs=("TAG_PF_SERVICE_BAKED", "TAG_PF_SERVICE"),
            image_tag_defaults=(
                "privacy-filter-service:baked-cpu",
                "privacy-filter-service:cpu",
            ),
            port=8001,
            # Cold runtime-download fetches a ~6 GB model; 5-minute
            # ceiling is generous but still fails loud if the network
            # is genuinely broken.
            readiness_timeout_s=300,
            hf_cache_volume="anonymizer-hf-cache",
            # opf writes its checkpoint into `~/.opf/privacy_filter`
            # (= `/app/.opf/privacy_filter` for the app user) and
            # ignores HF_HOME — so we mount the cache volume there
            # rather than at the conventional HF cache path. Mounting
            # at `/app/.cache/huggingface` and using a symlink
            # doesn't work: pathlib's mkdir(parents=True, exist_ok=True)
            # chokes on a dangling symlink in the parent chain when
            # the volume is empty.
            cache_mount_path="/app/.opf",
            guardrail_env_when_started={
                "PRIVACY_FILTER_URL": "http://privacy-filter-service:8001",
            },
        ),
    ),
    "gliner_pii": LauncherSpec(
        guardrail_env_passthroughs=[
            "GLINER_PII_URL",
            "GLINER_PII_TIMEOUT_S",
            "GLINER_PII_LABELS",
            "GLINER_PII_THRESHOLD",
            "GLINER_PII_FAIL_CLOSED",
            "GLINER_PII_MAX_CONCURRENCY",
        ],
        service=ServiceSpec(
            container_name="gliner-pii-service",
            image_tag_envs=("TAG_GLINER_SERVICE_BAKED", "TAG_GLINER_SERVICE"),
            image_tag_defaults=(
                "gliner-pii-service:baked-cpu",
                "gliner-pii-service:cpu",
            ),
            port=8002,
            readiness_timeout_s=300,
            # Separate cache from PF so wiping one doesn't break the
            # other (different model, different download).
            hf_cache_volume="gliner-hf-cache",
            guardrail_env_when_started={
                "GLINER_PII_URL": "http://gliner-pii-service:8002",
            },
            # Forward the operator's GLINER_PII_LABELS / _THRESHOLD into
            # the SERVICE's DEFAULT_LABELS / DEFAULT_THRESHOLD so a
            # single env var configures both ends.
            service_env_passthroughs={
                "DEFAULT_LABELS": "GLINER_PII_LABELS",
                "DEFAULT_THRESHOLD": "GLINER_PII_THRESHOLD",
            },
        ),
    ),
    "llm": LauncherSpec(
        guardrail_env_passthroughs=[
            "LLM_API_BASE",
            "LLM_API_KEY",
            "LLM_MODEL",
            "LLM_TIMEOUT_S",
            "LLM_USE_FORWARDED_KEY",
            "LLM_SYSTEM_PROMPT_PATH",
            "LLM_SYSTEM_PROMPT_REGISTRY",
            "LLM_MAX_CHARS",
            "LLM_MAX_CONCURRENCY",
            "LLM_FAIL_CLOSED",
        ],
        service=ServiceSpec(
            container_name="fake-llm",
            image_tag_envs=("TAG_FAKE_LLM",),
            image_tag_defaults=("fake-llm:latest",),
            port=4000,
            readiness_timeout_s=30,
            # No HF cache — fake-llm is rule-driven, no model.
            hf_cache_volume=None,
            guardrail_env_when_started={
                "LLM_API_BASE": "http://fake-llm:4000/v1",
                # fake-llm doesn't actually validate the key but the
                # detector refuses to send the Authorization header
                # when the key is empty — set a non-empty placeholder.
                "LLM_API_KEY": "any-non-empty",
                "LLM_MODEL": "fake",
            },
        ),
    ),
}


# ── Cross-cutting constants ───────────────────────────────────────────────
# Shared with the bash wrapper-style invocation contract — change here,
# the launcher follows.

SHARED_NETWORK = "anonymizer-net"
"""Docker/Podman network all auto-started services join so the guardrail
can dial them by container name."""

CONTAINER_NAME_DEFAULT = "anonymizer-guardrail"
"""Default name for the guardrail container itself. Operator can override
via `--name`."""


def get_image_tag(envs: tuple[str, ...], defaults: tuple[str, ...]) -> tuple[str, str | None]:
    """Pick the operator-resolved image tag for a service.

    Returns (image_tag, kind) where `kind` is the env-var name that
    was used (for "we're using the baked variant" log lines).
    Falls back to the first default when no env vars are set —
    callers then check whether the image actually exists locally
    via engine.image_exists.
    """
    import os

    for env_name, default in zip(envs, defaults):
        val = os.environ.get(env_name)
        if val:
            return val, env_name
    # All env vars unset → return the first default.
    return defaults[0], envs[0] if envs else None


__all__ = [
    "LAUNCHER_METADATA",
    "LauncherSpec",
    "ServiceSpec",
    "SHARED_NETWORK",
    "CONTAINER_NAME_DEFAULT",
    "get_image_tag",
]
