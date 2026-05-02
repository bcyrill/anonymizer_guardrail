#!/usr/bin/env bash
# Thin wrapper that execs into the Python image builder.
#
# CLI mode (default):
#   scripts/image_builder.sh --preset minimal
#   scripts/image_builder.sh -f slim -f pf-service
#   scripts/image_builder.sh --preset all -- --no-cache
#
# Interactive Textual menu (preset radio + checkbox grid):
#   scripts/image_builder.sh --ui
#   scripts/image_builder.sh --interactive
#
# List all available flavours and presets:
#   scripts/image_builder.sh --list
#
# The mode switch lives in tools.image_builder (Click eager callback
# on --ui / --interactive); this script just forwards argv. The
# builder itself lives at tools/image_builder/ — outside the production
# wheel — so adding it didn't grow the slim image.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

# IMAGE_BUILDER_PROG_NAME makes the --help "Usage:" line read
# `scripts/image_builder.sh` instead of `python -m tools.image_builder`,
# matching how the operator actually invokes us.
exec env IMAGE_BUILDER_PROG_NAME="scripts/image_builder.sh" \
    python3 -m tools.image_builder "$@"
