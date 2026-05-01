#!/usr/bin/env bash
# Thin wrapper that execs into the Python launcher's Textual menu.
# The launcher itself lives at tools/launcher/ — outside the production
# wheel — so adding it didn't grow the slim image.
#
# Operators run this exactly as before:
#   scripts/menu.sh
# A Textual single-screen UI replaces the bash `dialog` UI; the concept
# stays the same (general / detectors / faker / launch).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

# `python -m tools.launcher.menu` runs tools/launcher/menu.py as the
# main module. Without the trailing `.menu` it would invoke the
# package's __main__.py (the CLI) with `menu` as a stray argv.
exec env LAUNCHER_PROG_NAME="scripts/menu.sh" \
    python3 -m tools.launcher.menu
