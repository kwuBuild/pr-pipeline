#!/usr/bin/env bash
#
# The entry point for pr-pipeline setup's subcommands. Resolves a Python >= 3.11
# interpreter the same way the tick does and hands every argument to
# lib/setup_flow.py:
#
#   bash ${CLAUDE_PLUGIN_ROOT}/skills/setup/setup.sh <subcommand> [arguments...]
#
# It adds no behaviour of its own. A missing interpreter exits 3, the code the
# subcommands already use for "the environment cannot run this".

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../tick/resolve-python.sh
. "$SKILL_DIR/../tick/resolve-python.sh"

if ! PY="$(pr_pipeline_resolve_python)"; then
  pr_pipeline_python_requirement_error
  exit 3
fi

exec "$PY" "$SKILL_DIR/lib/setup_flow.py" "$@"
