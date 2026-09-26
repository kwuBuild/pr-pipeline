# shellcheck shell=bash
# Shared helpers for the independence-ladder stages. Sourced, never run.
#
# Every stage prints one `PASS  <check>` or `FAIL  <check> - <why>` line per
# check and exits 1 when any check failed, 2 when it could not run at all.

LADDER_FAILED=0

# The plugin as a marketplace names it; both halves come from the manifests.
LADDER_PLUGIN_ID="pr-pipeline@pr-pipeline"

ladder_die() {
  printf 'ladder: %s\n' "$*" >&2
  exit 2
}

ladder_note() {
  printf '== %s\n' "$*" >&2
}

# ladder_check <name> <status> [detail]
ladder_check() {
  if [ "$2" = 0 ]; then
    printf 'PASS  %s\n' "$1"
  else
    printf 'FAIL  %s - %s\n' "$1" "${3:-}"
    LADDER_FAILED=1
  fi
}

# ladder_assert <name> <detail> <command...> - PASS when the command succeeds.
ladder_assert() {
  local name="$1" detail="$2"
  shift 2
  if "$@"; then
    ladder_check "$name" 0
  else
    ladder_check "$name" 1 "$detail"
  fi
}

ladder_finish() {
  if [ "$LADDER_FAILED" = 0 ]; then
    printf 'RESULT  %s passed\n' "$1"
    exit 0
  fi
  printf 'RESULT  %s FAILED\n' "$1"
  exit 1
}

ladder_require() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || ladder_die "missing tool on PATH: $tool"
  done
}

# The workspace every stage writes into. Absolute, so a stage that changes
# directory never writes somewhere else.
ladder_work() {
  [ -n "${LADDER_WORK:-}" ] || ladder_die "set LADDER_WORK to an absolute scratch directory"
  case "$LADDER_WORK" in
    /*) ;;
    *) ladder_die "LADDER_WORK must be absolute: $LADDER_WORK" ;;
  esac
  mkdir -p "$LADDER_WORK" || ladder_die "cannot create $LADDER_WORK"
  printf '%s\n' "$LADDER_WORK"
}

# Resolve Python >= 3.11 the way the pipeline under test does, as an absolute
# path: the stubs use it as their shebang.
ladder_python() {
  local root="$1" name
  # shellcheck source=../skills/tick/resolve-python.sh
  . "$root/skills/tick/resolve-python.sh"
  name="$(pr_pipeline_resolve_python)" \
    || ladder_die "no Python >= 3.11 found by $root/skills/tick/resolve-python.sh"
  command -v "$name" || ladder_die "cannot locate $name"
}

# The unrelated project the plugin is installed into at local scope. Its
# remote is a public repository that has never used pr-pipeline.
ladder_project() {
  local project="$1/unrelated"
  if [ ! -d "$project/.git" ]; then
    mkdir -p "$project"
    git -C "$project" init -q || ladder_die "git init failed in $project"
    git -C "$project" remote add origin \
      "https://github.com/${LADDER_UNRELATED_REPO:-octocat/Hello-World}.git"
  fi
  printf '%s\n' "$project"
}

# ladder_installed <project> <field> — one field of this plugin's local-scope
# install record in <project>, or nothing when it is not installed there.
ladder_installed() {
  (cd "$1" && claude plugin list --json 2>/dev/null) \
    | jq -r --arg id "$LADDER_PLUGIN_ID" --arg project "$1" --arg field "$2" '
        .[] | select(.id == $id and .scope == "local" and .projectPath == $project)
        | .[$field] // empty
      '
}

# ladder_status_session <project> <install_path> <out.jsonl>
#
# A fresh headless session in <project> that invokes /pr-pipeline:status, so
# the skill body is loaded and substituted by Claude Code itself. Prints the
# session report (see session_report.py) as JSON.
ladder_status_session() {
  local project="$1" install_path="$2" out="$3" py status
  py="$(ladder_python "$install_path")"
  (
    cd "$project" &&
      claude -p "/pr-pipeline:status" \
        --model "${LADDER_SESSION_MODEL:-sonnet}" \
        --append-system-prompt "Run the skill's Run-section bash command exactly once, exactly as the skill text writes it, then report its output verbatim." \
        --allowedTools "Bash(bash $install_path/skills/status/status.sh)" \
        --output-format stream-json --verbose \
        </dev/null >"$out" 2>"$out.stderr"
  )
  status=$?
  if [ "$status" != 0 ]; then
    ladder_note "session exited $status; see $out.stderr"
    return 1
  fi
  "$py" "$(dirname "${BASH_SOURCE[0]}")/session_report.py" "$out" \
    --dashboard-command "bash $install_path/skills/status/status.sh"
}
