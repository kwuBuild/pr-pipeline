#!/usr/bin/env bash
#
# The operator entry point for one PR-pipeline tick. Resolves a Python >= 3.11
# interpreter and hands every argument straight to lib/tick.py:
#
#   ~/.claude/pr-pipeline/bin/pr-pipeline tick [--loop] [pr:N | '#N'] ...
#
# Run it from the target repo root: pipeline CODE resolves relative to this
# script (under a plugin install the skills live outside the repo entirely),
# while pipeline STATE — .claude/worktrees/ — belongs to the repo being
# operated on and is resolved from the working directory by tick.py.
#
# This script exists so the documented command does not have to name an
# interpreter. `python3` is wrong (it is a 3.9 on a stock macOS box) and
# `uv run python` was wrong in a different way — it required a package manager
# the stdlib-only runtime contract never declared. See resolve-python.sh.
#
# It adds no behaviour of its own: it takes no lock, reads no config, and exits
# with tick.py's own status. The one exception is a missing interpreter, which
# exits 3 — the code already meaning "a required tool is missing and nothing
# ran", and the one the documented /loop prompt stops on.

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=resolve-python.sh
. "$SKILL_DIR/resolve-python.sh"

if ! PY="$(pr_pipeline_resolve_python)"; then
  pr_pipeline_python_requirement_error
  exit 3
fi

# exec, not a call: the tick installs its own signal handling and writes its own
# exit code, and an intervening shell would take the operator's Ctrl-C and the
# loop's termination signals instead of the process that knows what to do with
# them.
exec "$PY" "$SKILL_DIR/lib/tick.py" "$@"
