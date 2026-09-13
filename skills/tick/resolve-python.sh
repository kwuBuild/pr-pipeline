#!/usr/bin/env bash
#
# One definition of "an interpreter that can run this pipeline", shared by every
# entry point: tick.sh, preflight.sh, and the status dashboard. Three copies of
# this logic could disagree about which Python the pipeline runs on, and the
# dashboard would then report on a tick it is not actually able to run.
#
# The requirement is Python >= 3.11: tick.py and pipeline.py parse config.toml
# with `tomllib`, which is 3.11+. Below the floor they do not import at all.
#
# Bare `python3` is not a safe substitute. This is about PRECEDENCE, not
# availability: a box can have 3.13 installed and still resolve `python3` to a
# 3.9 (stock macOS, Anaconda, a pyenv shim). So probe the explicit minors first
# and fall back to `python3` only once it has been version-checked.
#
# `uv run python` is not a substitute either, and used to be the documented one.
# It adds a dependency the stdlib-only runtime contract does not declare, it is
# not present on an operator box that never opted into it, and it is not
# reliably correct: `uv run --python 3.13` has been observed resolving an
# interpreter without `tomllib`, and an Anaconda `PATH` shadows the resolved
# environment through the `pytest` console script.
#
# Two ways to use it:
#
#   source "$SKILL_DIR/resolve-python.sh"          # defines the functions below
#   PY="$(bash "$SKILL_DIR/resolve-python.sh")"    # prints the interpreter
#
# Callers decide the failure policy, because it differs: preflight.sh and
# tick.sh must abort, while the dashboard degrades to a `?` column and keeps
# rendering the state it can still read.

# Interpreter names probed in order, newest first. Declared once because three
# things read it — the resolution loop, the failure message, and the test that
# holds CI's matrix to it — and a second copy is one nobody notices going stale.
# Anything added here is a version the pipeline may silently start running on,
# so CI's top leg has to move with it; `test_launcher.py` enforces that.
PR_PIPELINE_PYTHON_CANDIDATES=(python3.14 python3.13 python3.12 python3.11 python3)

pr_pipeline_python_usable() {
  command -v "$1" >/dev/null 2>&1 \
    && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
      >/dev/null 2>&1
}

# `PR_PIPELINE_PYTHON` is the operator's escape hatch: on a shared box where
# `PATH` order is not theirs to change, it is the only way to name the right
# interpreter. It is a hint, not an override — a pin below the floor is still
# rejected, since `tomllib` will not appear because someone asked for it.
#
# Falling back past a pin is announced rather than silent. A typo'd path and a
# deliberate-but-too-old pin look identical to `command -v`, and both end with
# the pipeline running on an interpreter the operator did not choose; without
# the warning, a tick that then succeeds reads as confirmation of a setting that
# was in fact discarded.
pr_pipeline_resolve_python() {
  local candidate
  if [[ -n "${PR_PIPELINE_PYTHON:-}" ]]; then
    if pr_pipeline_python_usable "$PR_PIPELINE_PYTHON"; then
      printf '%s\n' "$PR_PIPELINE_PYTHON"
      return 0
    fi
    echo "WARN: PR_PIPELINE_PYTHON ($PR_PIPELINE_PYTHON) is not a usable Python >= 3.11." >&2
    echo "      Ignoring it and probing PATH. Fix the pin or unset it." >&2
  fi
  for candidate in "${PR_PIPELINE_PYTHON_CANDIDATES[@]}"; do
    if pr_pipeline_python_usable "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

# The failure message an operator has to act on. It names the requirement rather
# than letting the failure surface as `ModuleNotFoundError: tomllib`, which
# reads as a broken install and sends them to reinstall the pipeline — the one
# action that cannot fix a `PATH`-precedence problem.
# Takes the severity as $1 because the callers differ: the tick and preflight
# abort, the dashboard degrades. A dashboard that says FAIL and then renders
# the queue anyway teaches an operator to ignore the word.
pr_pipeline_python_requirement_error() {
  echo "${1:-FAIL}: no Python >= 3.11 found on PATH (tick.py and pipeline.py need tomllib)." >&2
  echo "      Tried:${PR_PIPELINE_PYTHON:+ \$PR_PIPELINE_PYTHON ($PR_PIPELINE_PYTHON)} ${PR_PIPELINE_PYTHON_CANDIDATES[*]}" >&2
  echo "      This is PATH precedence, not availability: a box can have 3.13" >&2
  echo "      installed and still resolve python3 to a 3.9 (macOS, Anaconda, pyenv)." >&2
  echo "      Install Python >= 3.11, or export PR_PIPELINE_PYTHON with the path to one." >&2
}

# Executed rather than sourced: behave as the resolver command, so a caller that
# only wants the interpreter name does not have to source a file to get it.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if ! pr_pipeline_resolve_python; then
    pr_pipeline_python_requirement_error
    exit 1
  fi
fi
