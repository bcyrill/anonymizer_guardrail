"""Flavour catalog for the image builder.

Single source of truth for what `tools.image_builder` knows how to
build. Adding a new image flavour means appending one `Flavour`
entry below — the CLI's `--flavour` validation, the menu's checkbox
list, and the build runner all consume `FLAVOURS` directly, so
nothing else needs touching.

Per-flavour fields:

  * `name` — the canonical flavour name; what `--flavour <NAME>`
    accepts. Must be unique.
  * `group` / `label` — display metadata for the menu (group header
    + the line shown under the [x] box).
  * `containerfile` / `context` — what `<engine> build -f … <ctx>`
    receives; paths are relative to the repo root.
  * `build_args` — passed verbatim as `--build-arg KEY=VALUE`.
  * `default_tag` — the local tag the build produces unless the
    operator passes `--tag`. Mirrors the env-var-overridable defaults
    in the old bash script (TAG_SLIM, TAG_PF, …) — operators who set
    those env vars before invoking the wrapper still see them honored
    via `_resolve_default_tag`.
  * `bakes_model` — true for any flavour that downloads model
    weights at build time; the runner shows a heads-up before
    starting so the operator isn't surprised by the network traffic.

Presets are named selections of flavour names. The TUI radio-list
applies a preset by checking the matching boxes; the CLI
`--preset NAME` is shorthand for the same set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# ── Display groups (menu section headers) ─────────────────────────────────
GROUP_GUARDRAIL = "anonymizer_guardrail"
GROUP_PF = "privacy-filter"
GROUP_PF_HF = "privacy-filter (HF variant)"
GROUP_GLINER = "gliner-pii"
GROUP_COMPANION = "companion"


@dataclass(frozen=True)
class Flavour:
    name: str
    group: str
    label: str
    containerfile: str
    context: str
    build_args: dict[str, str] = field(default_factory=dict)
    default_tag_env: str = ""
    default_tag: str = ""
    bakes_model: bool = False


def _resolve_default_tag(env_name: str, fallback: str) -> str:
    """Honor the same TAG_SLIM / TAG_PF / … env overrides the old bash
    script accepted, so operators with existing tagging workflows
    don't have to retrain. Empty env value → fallback (matches bash's
    `${TAG_SLIM:-anonymizer-guardrail:latest}` semantics)."""
    val = os.environ.get(env_name, "").strip()
    return val or fallback


# Order here is the order they appear in the menu and in `--list` output.
# The FLAVOURS tuple is built lazily so env-var overrides resolve at
# import time but stay readable.
def _build_catalog() -> tuple[Flavour, ...]:
    return (
        # ── Guardrail (one Containerfile, no build-args) ───────────────
        # `default` is the only guardrail flavour. Privacy-filter and
        # gliner-pii ship as standalone services. The flavour
        # mechanism is kept (rather than reduced to a single hardcoded
        # name) for future expansion — a future GPU-bundled or
        # alternate-runtime variant slots in here.
        Flavour(
            name="default",
            group=GROUP_GUARDRAIL,
            label="default",
            containerfile="Containerfile",
            context=".",
            default_tag=_resolve_default_tag("TAG_DEFAULT", "anonymizer-guardrail:latest"),
        ),
        # ── Privacy-filter standalone service ───────────────────────────
        Flavour(
            name="pf-service",
            group=GROUP_PF,
            label="cpu",
            containerfile="services/privacy_filter/Containerfile",
            context="services/privacy_filter",
            default_tag=_resolve_default_tag("TAG_PF_SERVICE", "privacy-filter-service:cpu"),
        ),
        Flavour(
            name="pf-service-baked",
            group=GROUP_PF,
            label="cpu + model (baked)",
            containerfile="services/privacy_filter/Containerfile",
            context="services/privacy_filter",
            build_args={"BAKE_MODEL": "true"},
            default_tag=_resolve_default_tag("TAG_PF_SERVICE_BAKED", "privacy-filter-service:baked-cpu"),
            bakes_model=True,
        ),
        Flavour(
            name="pf-service-cu130",
            group=GROUP_PF,
            label="cuda-130",
            containerfile="services/privacy_filter/Containerfile",
            context="services/privacy_filter",
            build_args={
                "TORCH_INDEX_URL": "https://download.pytorch.org/whl/cu130",
                "TARGET_DEVICE": "cuda",
            },
            default_tag=_resolve_default_tag("TAG_PF_SERVICE_CU130", "privacy-filter-service:cu130"),
        ),
        Flavour(
            name="pf-service-baked-cu130",
            group=GROUP_PF,
            label="cuda-130 + model (baked)",
            containerfile="services/privacy_filter/Containerfile",
            context="services/privacy_filter",
            build_args={
                "BAKE_MODEL": "true",
                "TORCH_INDEX_URL": "https://download.pytorch.org/whl/cu130",
                "TARGET_DEVICE": "cuda",
            },
            default_tag=_resolve_default_tag("TAG_PF_SERVICE_BAKED_CU130", "privacy-filter-service:baked-cu130"),
            bakes_model=True,
        ),
        # ── Privacy-filter (HF variant — experimental) ──────────────────
        # Pairs HF Transformers' forward pass with opf's Viterbi
        # decoder. Same wire format as the opf-only pf-service, so
        # the guardrail talks to either with no client changes. See
        # services/privacy_filter_hf/README.md and COMPARE.md for the
        # rationale and measurements.
        Flavour(
            name="pf-hf-service",
            group=GROUP_PF_HF,
            label="cpu (HF forward + opf decode)",
            containerfile="services/privacy_filter_hf/Containerfile",
            context="services/privacy_filter_hf",
            default_tag=_resolve_default_tag(
                "TAG_PF_HF_SERVICE", "privacy-filter-hf-service:cpu"
            ),
        ),
        Flavour(
            name="pf-hf-service-cu130",
            group=GROUP_PF_HF,
            label="cuda-130 (HF forward + opf decode)",
            containerfile="services/privacy_filter_hf/Containerfile",
            context="services/privacy_filter_hf",
            build_args={
                "TORCH_INDEX_URL": "https://download.pytorch.org/whl/cu130",
                "TARGET_DEVICE": "cuda",
            },
            default_tag=_resolve_default_tag(
                "TAG_PF_HF_SERVICE_CU130", "privacy-filter-hf-service:cu130"
            ),
        ),
        # No baked variant — the build hits disk-space pressure during
        # the layer commit (transformers + opf + ~3 GB of weights, with
        # overlayfs duplicating cached files via snapshot symlinks,
        # balloons the working set well past the image's nominal size).
        # See services/privacy_filter_hf/Containerfile for the full
        # rationale. Same exclusion applies to the cu130 build.
        # ── GLiNER-PII standalone service ───────────────────────────────
        Flavour(
            name="gliner-service",
            group=GROUP_GLINER,
            label="cpu",
            containerfile="services/gliner_pii/Containerfile",
            context="services/gliner_pii",
            default_tag=_resolve_default_tag("TAG_GLINER_SERVICE", "gliner-pii-service:cpu"),
        ),
        Flavour(
            name="gliner-service-baked",
            group=GROUP_GLINER,
            label="cpu + model (baked)",
            containerfile="services/gliner_pii/Containerfile",
            context="services/gliner_pii",
            build_args={"BAKE_MODEL": "true"},
            default_tag=_resolve_default_tag("TAG_GLINER_SERVICE_BAKED", "gliner-pii-service:baked-cpu"),
            bakes_model=True,
        ),
        Flavour(
            name="gliner-service-cu130",
            group=GROUP_GLINER,
            label="cuda-130",
            containerfile="services/gliner_pii/Containerfile",
            context="services/gliner_pii",
            build_args={"TORCH_INDEX_URL": "https://download.pytorch.org/whl/cu130"},
            default_tag=_resolve_default_tag("TAG_GLINER_SERVICE_CU130", "gliner-pii-service:cu130"),
        ),
        Flavour(
            name="gliner-service-baked-cu130",
            group=GROUP_GLINER,
            label="cuda-130 + model (baked)",
            containerfile="services/gliner_pii/Containerfile",
            context="services/gliner_pii",
            build_args={
                "BAKE_MODEL": "true",
                "TORCH_INDEX_URL": "https://download.pytorch.org/whl/cu130",
            },
            default_tag=_resolve_default_tag("TAG_GLINER_SERVICE_BAKED_CU130", "gliner-pii-service:baked-cu130"),
            bakes_model=True,
        ),
        # ── Companion test backend ──────────────────────────────────────
        Flavour(
            name="fake-llm",
            group=GROUP_COMPANION,
            label="fake-llm (deterministic test backend)",
            containerfile="services/fake_llm/Containerfile",
            context="services/fake_llm",
            default_tag=_resolve_default_tag("TAG_FAKE_LLM", "fake-llm:latest"),
        ),
    )


FLAVOURS: tuple[Flavour, ...] = _build_catalog()
FLAVOURS_BY_NAME: dict[str, Flavour] = {f.name: f for f in FLAVOURS}


def flavours_in_group(group: str) -> tuple[Flavour, ...]:
    """All flavours in `group`, in catalog order."""
    return tuple(f for f in FLAVOURS if f.group == group)


# ── Presets ────────────────────────────────────────────────────────────────
# Named subsets of flavour names. Used by both `--preset NAME` (CLI)
# and the menu's preset radio. Keys must be lower-case, no spaces.
#
# `all` covers every flavour including baked + CUDA. The "minimal"
# pair is the recommended development setup (the guardrail image
# plus the CPU runtime-download services). Per-service presets
# bundle every variant of one image for operators rebuilding a
# single component across CPU/CUDA/baked combos.
PRESETS: dict[str, tuple[str, ...]] = {
    "all": tuple(f.name for f in FLAVOURS),
    "guardrail": tuple(f.name for f in flavours_in_group(GROUP_GUARDRAIL)),
    "privacy-filter": tuple(f.name for f in flavours_in_group(GROUP_PF)),
    "privacy-filter-hf": tuple(f.name for f in flavours_in_group(GROUP_PF_HF)),
    "gliner-pii": tuple(f.name for f in flavours_in_group(GROUP_GLINER)),
    "minimal": ("default", "pf-service", "gliner-service"),
    "minimal-fakellm": ("default", "pf-service", "gliner-service", "fake-llm"),
}


def preset_names() -> tuple[str, ...]:
    """Stable order for menu/help display."""
    return tuple(PRESETS.keys())


# Order tuple for resolving "what preset matches this checkbox state".
# When the operator hand-picks a set that exactly matches a preset,
# the menu's preset indicator snaps back to it.
def match_preset(selected: frozenset[str]) -> str | None:
    """Return the preset name whose flavour set equals `selected`,
    or None if none matches. Used by the menu to keep the preset
    indicator in sync with manual checkbox toggles.
    """
    for name, members in PRESETS.items():
        if frozenset(members) == selected:
            return name
    return None


__all__ = [
    "Flavour",
    "FLAVOURS",
    "FLAVOURS_BY_NAME",
    "PRESETS",
    "GROUP_GUARDRAIL",
    "GROUP_PF",
    "GROUP_GLINER",
    "GROUP_COMPANION",
    "flavours_in_group",
    "preset_names",
    "match_preset",
]
