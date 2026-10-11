#!/usr/bin/env bash
# Read retained events from the consumer's common checkout, using installed code.
set -uo pipefail
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$(bash "$SKILL_DIR/../tick/resolve-target.sh")" || exit 2
REPO="${TARGET%%$'\n'*}"
COMMON_DIR="$(git rev-parse --git-common-dir)" || exit 2
ROOT="$(cd "$(dirname "$COMMON_DIR")" && pwd)" || exit 2
# shellcheck source=../tick/resolve-python.sh
. "$SKILL_DIR/../tick/resolve-python.sh"
PY="$(pr_pipeline_resolve_python)" || { pr_pipeline_python_requirement_error FAIL; exit 3; }
exec "$PY" -B "$SKILL_DIR/events.py" --repo "$REPO" \
  --log-dir "$ROOT/.claude/worktrees/.tick-log" "$@"
