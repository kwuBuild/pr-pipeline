#!/usr/bin/env bash
#
# Transparent observer for a real engine binary (ladder stage L3). Installed
# first on PATH as `claude` or `codex` with @REAL@, @NAME@, @SELF_DIR@,
# @LOG_DIR@, @PYTHON@ and @CLAUDE_SETTINGS@ rewritten; the reviewer's scrubbed
# environment carries no ladder variables, so nothing is read from the
# environment.
#
# It runs the real binary with the same argv, stdin and exit status, and tees
# stdout to a log. The pipeline keeps a successful engine's stream nowhere, and
# the stream is the only record of which files a real engine read.
#
# The observer's own directory is removed from PATH first. @REAL@ may itself be
# a wrapper that looks up the next `claude` on PATH (a terminal's shim does);
# with the observer still on PATH that lookup finds the observer again, and the
# two call each other without end. The depth guard is a second stop for a
# wrapper that restores PATH.

real="@REAL@"
name="@NAME@"
self_dir="@SELF_DIR@"
# Extra settings for `claude` calls only (LADDER_ENGINE_SETTINGS at install
# time), recorded in the argv log like every other argument.
settings='@CLAUDE_SETTINGS@'
if [ "$name" = claude ] && [ -n "$settings" ]; then
  set -- --settings "$settings" "$@"
fi
if [ "${LADDER_OBSERVER_DEPTH:-0}" -gt 0 ]; then
  echo "ladder observer: re-entered through $real; refusing to recurse" >&2
  exit 97
fi
export LADDER_OBSERVER_DEPTH=1
path=":$PATH:"
path="${path//:$self_dir:/:}"
path="${path#:}"
export PATH="${path%:}"

log="@LOG_DIR@/$name-$(date -u +%Y%m%dT%H%M%SZ)-$$"
{ pwd; printf '%s\n' "$@"; } >"$log.argv"
# Preserve argument boundaries and the controller's lease before harvest can
# delete it. Version/setup probes legitimately have no dispatch marker.
if ! "@PYTHON@" - "$log.launch.json" "$@" <<'PY'
import json, os, pathlib, sys
marker = pathlib.Path("dispatch.json")
entry = {"cwd": os.getcwd(), "argv": sys.argv[2:],
         "dispatch": json.loads(marker.read_text()) if marker.exists() else None}
pathlib.Path(sys.argv[1]).write_text(json.dumps(entry) + "\n")
PY
then
  exit 96
fi
"$real" "$@" | tee "$log.jsonl"
status=${PIPESTATUS[0]}
printf '%s\n' "$status" >"$log.exit"
exit "$status"
