#!/usr/bin/env bash
#
# Preflight gate for the PR-pipeline reviewer (design: "Preflight requirement").
# Must pass before the first review dispatch. Verifies, against the installed
# Codex CLI, that the structured-verdict mechanism the harvest depends on works:
#
#   (a) `codex exec` runs to completion here (native dependency installed).
#   (b) `--output-schema` + `-o` produce a file that validates against
#       review-schema.json (via the tested `validate-review` helper).
#   (c) Collects a model-reported read-only denial signal, explicitly UNVERIFIED
#       because this CLI emits no tool/OS-sourced execution-denial event.
#
# Codex needs network access, so run this with the agent sandbox disabled.
#
# Usage: bash $PR_PIPELINE_HOME/skills/pr-pipeline-tick/preflight.sh

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA="$SKILL_DIR/review-schema.json"
PROBE_SCHEMA="$SKILL_DIR/preflight-probe-schema.json"
LIB="$SKILL_DIR/lib/pipeline.py"
MIN_VERSION="codex-cli 0.142.5"

# Resolve an interpreter that can actually run the lib. `uv run python` is not
# available outside a uv project, and bare `python3` is NOT a safe substitute:
# tick.py/pipeline.py need 3.11+ for `tomllib`, while the ambient `python3` is
# whatever PATH resolves (3.9 on a stock macOS box, and on Anaconda installs).
# Note this is about PRECEDENCE, not availability: a box can have 3.13 installed
# and still resolve `python3` to a 3.9 (Anaconda, pyenv shim, system Python).
# Set PR_PIPELINE_PYTHON to pin an interpreter explicitly; otherwise probe
# explicit minors first, then verify whatever `python3` resolves to. Fail loudly
# rather than letting a 3.9 produce a confusing ImportError inside a probe.
resolve_python() {
  local candidate
  for candidate in ${PR_PIPELINE_PYTHON:+"$PR_PIPELINE_PYTHON"} \
                   python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PY="$(resolve_python)"; then
  echo "FAIL: no Python >= 3.11 found on PATH (needed for tomllib)." >&2
  echo "      Tried: python3.14 python3.13 python3.12 python3.11 python3" >&2
  exit 1
fi

# Resolve the binary by the SAME path dispatch uses (`command -v codex`), then bind
# every probe below to that absolute path. Preflight and dispatch must verify and launch
# the identical build, or the sandbox/schema guarantees this gate proves do not transfer
# to the reviewer the tick actually runs.
if ! CODEX_BIN="$(command -v codex)" || [[ -z "$CODEX_BIN" ]]; then
  echo "FAIL: no 'codex' on PATH. Install the Codex CLI before dispatching reviewers."
  exit 1
fi

# Hard-fail if codex is not runnable at all (e.g. the wrapper package is present
# but its native binary — `@openai/codex-*` — was never installed). A broken
# install must abort here with an actionable message, not surface later as a
# confusing FAIL(a).
if ! version="$("$CODEX_BIN" --version 2>/dev/null | tr -d '\r')" || [[ -z "$version" ]]; then
  echo "FAIL: codex is not runnable ('$CODEX_BIN --version' failed or printed nothing)."
  echo "      Install/repair the Codex CLI (its native '@openai/codex-*' dependency)"
  echo "      before dispatching reviewers."
  exit 1
fi
# Hard-fail if installed version is below the minimum. Extract semver from
# "codex-cli X.Y.Z" and compare numerically so upgrades don't break dispatch.
# Strip pre-release suffixes (e.g. 0.141.0-beta.1 → 0.141.0) before parsing.
min_semver="${MIN_VERSION#codex-cli }"
installed_semver="${version#codex-cli }"
# Strip pre-release suffix from patch (e.g. "0-beta.1" → "0")
installed_semver="${installed_semver%%-*}"
# Validate format: exactly 3 dot-separated decimal components.
if ! [[ "$installed_semver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "FAIL: codex version '$version' has unexpected format (expected X.Y.Z)."
  exit 1
fi
IFS='.' read -r min_major min_minor min_patch <<< "$min_semver"
IFS='.' read -r inst_major inst_minor inst_patch <<< "$installed_semver"
version_ok=0
if (( 10#$inst_major > 10#$min_major )); then version_ok=1
elif (( 10#$inst_major == 10#$min_major )); then
  if (( 10#$inst_minor > 10#$min_minor )); then version_ok=1
  elif (( 10#$inst_minor == 10#$min_minor && 10#$inst_patch >= 10#$min_patch )); then version_ok=1
  fi
fi
if (( ! version_ok )); then
  echo "FAIL: codex version '$version' is below minimum '$MIN_VERSION'."
  echo "      Update codex or lower MIN_VERSION if the older build is verified."
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Isolate codex from the operator's ~/.codex config (MCP servers, plugins, hooks,
# rules), exactly as the production dispatch does — those load as out-of-sandbox
# processes and can hang the run. But auth ALSO lives in CODEX_HOME, so an empty home
# would leave codex unauthenticated; instead build a minimal home that symlinks only
# auth.json (no config.toml/plugins/rules/hooks/skills), preserving login while
# dropping every steerable surface.
export CODEX_HOME="$work/codex-home"
mkdir -p "$CODEX_HOME"
if [[ -f "$HOME/.codex/auth.json" ]]; then
  ln -s "$HOME/.codex/auth.json" "$CODEX_HOME/auth.json"
else
  echo "FAIL: no '$HOME/.codex/auth.json' — codex is not logged in. Run 'codex login' first."
  exit 1
fi

# Bound each probe so a hung codex (e.g. a stuck tool handshake) fails the gate instead
# of blocking forever. Prefer GNU `timeout`/`gtimeout`; if neither is present, run
# unbounded and warn rather than silently dropping the guard.
PROBE_TIMEOUT=120
if command -v timeout >/dev/null 2>&1; then bound() { timeout "$PROBE_TIMEOUT" "$@"; }
elif command -v gtimeout >/dev/null 2>&1; then bound() { gtimeout "$PROBE_TIMEOUT" "$@"; }
else
  echo "WARN: no 'timeout'/'gtimeout' found — probes run unbounded (install coreutils)."
  bound() { "$@"; }
fi

# (a) + (b): a schema-conforming verdict is produced and validates.
out="$work/review.json"
if ! bound "$CODEX_BIN" exec -C "$work" --skip-git-repo-check -s read-only \
      --output-schema "$SCHEMA" -o "$out" \
      "Output a code-review verdict only. There is no diff to review for this preflight: return verdict ready_for_human with an empty findings array, conforming exactly to the output schema." \
      2> "$work/stderr-ab.log"; then
  echo "FAIL(a): codex exec did not complete (or timed out)"
  sed 's/^/  codex: /' "$work/stderr-ab.log"
  exit 1
fi

if ! verdict="$("$PY" "$LIB" validate-review --review "$out")"; then
  echo "FAIL(b): codex output did not validate against review-schema.json"
  echo "  produced:"; sed 's/^/  /' "$out" 2>/dev/null || echo "  (no output file written)"
  exit 1
fi
echo "PASS(a)(b): codex exec produced a schema-valid verdict ($verdict)"

# (c): collect a read-only sandbox signal without presenting it as proof.
#
# The earlier two-run probe (write under workspace-write, then read-only) had a
# logical gap: proving the FIRST run chose to write never proved the SECOND run
# attempted it, so an absent file under read-only did not establish sandbox
# denial — the model may simply not have tried. (`codex sandbox`, which would run
# the write deterministically with no model, aborts intermittently with SIGABRT
# on the pinned CLI, so it is not a reliable probe here.)
#
# Instead, do ONE read-only run that reports via a typed output schema whether it
# attempted the write and observed a denial, then corroborate that no file exists.
# These fields remain the model's final answer, not execution telemetry, so this is
# useful compatibility evidence but not deterministic proof of sandbox enforcement.
#
# Limitation (originally verified on codex-cli 0.141.0): `codex exec --json` did
# NOT emit a structured command-execution/denial event — the stream was only
# thread.started / turn.started / item.completed(agent_message) / turn.completed,
# and the shell denial ("operation not permitted") appeared solely inside the
# model-authored agent_message. write_attempted/write_blocked self-report
# corroborated by the filesystem is the strongest signal that probe supports.
# Note: 0.142.5 has been observed emitting `command_execution` items in the JSONL
# stream, so a typed exec-event probe may now be possible — a future improvement,
# not yet implemented here.
probe_out="$work/probe.json"
bound "$CODEX_BIN" exec -C "$work" --skip-git-repo-check -s read-only \
  --output-schema "$PROBE_SCHEMA" -o "$probe_out" \
  "Attempt to create a file named WRITE_TEST.txt containing the word blocked in the current working directory by running a shell command. Then report, conforming exactly to the output schema: set write_attempted=true ONLY if you actually executed a command that tried to create the file, and write_blocked=true if that command was denied by the sandbox (e.g. 'operation not permitted'). Never report write_attempted=true unless you really ran the command." \
  > /dev/null 2> "$work/stderr-c.log" || true

attempted="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("write_attempted"))' "$probe_out" 2>/dev/null || echo None)"
blocked="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("write_blocked"))' "$probe_out" 2>/dev/null || echo None)"

if [[ -f "$work/WRITE_TEST.txt" ]]; then
  echo "FAIL(c): -s read-only did NOT block the write (WRITE_TEST.txt was created)."
  exit 1
fi
if [[ "$attempted" != "True" ]]; then
  echo "FAIL(c): inconclusive — the read-only run did not confirm it attempted the write"
  echo "         (write_attempted=$attempted), so an absent file does not prove denial."
  echo "         Strengthen the probe prompt and re-run."
  sed 's/^/  codex: /' "$work/stderr-c.log" 2>/dev/null || true
  exit 1
fi
if [[ "$blocked" != "True" ]]; then
  echo "FAIL(c): the write was attempted but the run did not report it as sandbox-denied"
  echo "         (write_blocked=$blocked)."
  exit 1
fi
echo "UNVERIFIED(c): model-reported sandbox denial signal; no tool/OS proof available"

echo "PREFLIGHT OK — binary/schema mechanism verified against $version ($CODEX_BIN)"
echo "  Minimum version floor: $MIN_VERSION"
echo "  Sandbox denial remains UNVERIFIED; the probe above is model-reported only."
echo "  Dispatch must launch this same build: verify_codex_bin resolves 'shutil.which'"
echo "  and gates on MIN_CODEX_VERSION, so a missing or too-old codex stops dispatch."
