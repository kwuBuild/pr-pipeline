#!/usr/bin/env bash
# Copied outside the plugin cache by install-launcher.sh. Resolve on every call;
# neither this file's location nor a session's CLAUDE_PLUGIN_ROOT pins a version.
set -uo pipefail

usage() {
  echo 'Usage: pr-pipeline {tick|status|events|preflight|labels} [arguments...]'
}

setup_error() {
  echo "FAIL: $*" >&2
  echo 'Setup: install and enable pr-pipeline@pr-pipeline, then rerun the launcher installation step (skills/tick/install-launcher.sh).' >&2
  echo 'For development, export PR_PIPELINE_HOME with an absolute checkout path.' >&2
  exit 3
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  tick) target=skills/tick/tick.sh ;;
  status) target=skills/status/status.sh ;;
  events) target=skills/status/events.sh ;;
  preflight) target=skills/tick/preflight.sh ;;
  labels) target=skills/tick/lib/labels.py ;;
  *) usage >&2; exit 2 ;;
esac
command_name="$1"
shift

if [[ "${PR_PIPELINE_HOME+x}" == x ]]; then
  install_root="$PR_PIPELINE_HOME"
  [[ "$install_root" == /* ]] || setup_error 'PR_PIPELINE_HOME must be a nonempty absolute checkout path; fix it or unset it.'
else
  command -v claude >/dev/null 2>&1 || setup_error 'claude is missing from PATH.'
  command -v jq >/dev/null 2>&1 || setup_error 'jq is missing from PATH.'
  plugins="$(claude plugin list --json)" || setup_error 'Could not list installed Claude plugins.'
  # Use Claude's inventory, including its enabled state and configured home,
  # instead of depending on its private registry layout or guessing by mtime.
  # Multiple enabled records are ambiguous; never launch an arbitrary version.
  install_root="$(printf '%s' "$plugins" | jq -er '
    if type != "array" then error("expected plugin array") else . end
    | if all(.[]; type == "object") then . else error("invalid plugin record") end
    | map(select(.id == "pr-pipeline@pr-pipeline" and .enabled == true))
    | if length == 1 then .[0].installPath else error("expected one enabled pr-pipeline install") end
    | if type == "string" then . else error("missing installPath") end
    | if startswith("/") and (test("[\\r\\n]") | not) then . else error("invalid installPath") end
  ')" || setup_error 'No unique enabled pr-pipeline install with an absolute path was found.'
fi

[[ -d "$install_root" && -f "$install_root/$target" ]] \
  || setup_error "The resolved install is missing $target: $install_root"

# Keep the target checkout as cwd and replace this process so signals and exit
# status reach the existing entry points without another supervising wrapper.
if [[ "$command_name" == labels ]]; then
  [[ -f "$install_root/skills/tick/resolve-python.sh" ]] \
    || setup_error 'The resolved install is missing resolve-python.sh.'
  # shellcheck source=resolve-python.sh
  . "$install_root/skills/tick/resolve-python.sh"
  if ! interpreter="$(pr_pipeline_resolve_python)"; then
    pr_pipeline_python_requirement_error
    exit 3
  fi
  exec "$interpreter" "$install_root/$target" "$@"
fi
exec bash "$install_root/$target" "$@"
