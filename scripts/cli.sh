#!/usr/bin/env bash
# Thin wrapper that execs into the Python launcher's `cli` subcommand.
# The launcher itself lives at tools/launcher/ — outside the production
# wheel — so adding it didn't grow the slim image.
#
# Operators run this exactly as before:
#   scripts/cli.sh -t slim -d regex,llm --llm-backend fake-llm
# After the rewrite, "fake-llm" maps to "service" (auto-start the
# fake-llm container); "custom" maps to "external" (operator URL).
# All other flags are unchanged. See `python -m tools.launcher cli --help`.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

# LAUNCHER_PROG_NAME makes the --help "Usage:" line read
# `scripts/cli.sh` instead of `python -m tools.launcher`, matching
# how the operator actually invokes us. The launcher app is
# single-command (no subcommand layer) so flags follow directly.
exec env LAUNCHER_PROG_NAME="scripts/cli.sh" \
    python3 -m tools.launcher "$@"
