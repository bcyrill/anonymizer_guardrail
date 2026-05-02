#!/usr/bin/env bash
# Thin wrapper that execs into the Python launcher.
#
# CLI mode (default):
#   scripts/launcher.sh -t slim -d regex,llm --llm-backend service
#
# Interactive Textual menu:
#   scripts/launcher.sh --ui
#   scripts/launcher.sh --interactive
#
# The mode switch lives in tools.launcher (Click eager callback on
# --ui / --interactive); this script just forwards argv. The launcher
# itself lives at tools/launcher/ — outside the production wheel —
# so adding it didn't grow the slim image.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

# LAUNCHER_PROG_NAME makes the --help "Usage:" line read
# `scripts/launcher.sh` instead of `python -m tools.launcher`, matching
# how the operator actually invokes us. The launcher app is
# single-command (no subcommand layer) so flags follow directly.
exec env LAUNCHER_PROG_NAME="scripts/launcher.sh" \
    python3 -m tools.launcher "$@"
