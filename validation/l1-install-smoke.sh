#!/usr/bin/env bash
#
# L1 - plugin install smoke. Install the candidate the way an adopter does,
# from a GitHub marketplace, into a project that has never used pr-pipeline.
#
#   LADDER_WORK=/abs/scratch validation/l1-install-smoke.sh <owner/name>
#
# <owner/name> is a GitHub repository whose default branch holds the candidate
# plugin. Use a throwaway repository: L4 pushes a version bump to it.
#
# A directory marketplace (`/plugin marketplace add ./path`) is not an adopter
# install: Claude Code runs such a plugin in place, so `${CLAUDE_PLUGIN_ROOT}`
# names the source directory rather than the versioned cache entry, and L4
# could not observe an update. The install is local-scope, so only sessions in
# the scratch project see it.
#
# Checks:
#   1. the installed skills are exactly `_PUBLISHED_SKILLS`, not a superset;
#   2. the installed dashboard, run in the unrelated project, reports an empty
#      queue and no error;
#   3. a fresh session invoking /pr-pipeline:status runs the dashboard from the
#      absolute install path: `${CLAUDE_PLUGIN_ROOT}` was substituted.
#
# Check 3 makes one short model call (LADDER_SESSION_MODEL, default sonnet).

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

[ $# -eq 1 ] || ladder_die "usage: l1-install-smoke.sh <owner/name of the candidate marketplace repo>"
source_repo="$1"
ladder_require claude gh git jq
work="$(ladder_work)"
project="$(ladder_project "$work")"

ladder_note "installing $LADDER_PLUGIN_ID from $source_repo into $project"
(cd "$project" && claude plugin marketplace add "$source_repo" --scope local) >&2 \
  || ladder_die "marketplace add failed"
(cd "$project" && claude plugin install "$LADDER_PLUGIN_ID" --scope local) >&2 \
  || ladder_die "plugin install failed"
install_path="$(ladder_installed "$project" installPath)"
version="$(ladder_installed "$project" version)"
[ -n "$install_path" ] || ladder_die "no local install record for $LADDER_PLUGIN_ID in $project"
ladder_note "installed $version at $install_path"
py="$(ladder_python "$install_path")"

case "$install_path" in
  "$HOME"/.claude/plugins/cache/*/"$version" | "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/*/"$version")
    ladder_check "install is a versioned cache entry" 0 ;;
  *) ladder_check "install is a versioned cache entry" 1 "$install_path" ;;
esac

# 1. Exact skill set.
installed_skills="$(
  cd "$project" && claude plugin details "$LADDER_PLUGIN_ID" 2>/dev/null \
    | sed -n 's/^ *Skills ([0-9]*) *//p' | tr -d ' ' | tr ',' '\n' | sort
)"
published_skills="$(
  "$py" - "$install_path/skills/tick/lib/test_skill_contract.py" <<'EOF'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        getattr(t, "id", "") == "_PUBLISHED_SKILLS" for t in node.targets
    ):
        print("\n".join(sorted(ast.literal_eval(node.value.args[0]))))
EOF
)"
ladder_assert "installed skills equal _PUBLISHED_SKILLS exactly" \
  "installed: $(echo "$installed_skills" | paste -sd, -) published: $(echo "$published_skills" | paste -sd, -)" \
  test -n "$installed_skills" -a "$installed_skills" = "$published_skills"

# 2. The dashboard in an unrelated repository.
dashboard="$(cd "$project" && bash "$install_path/skills/status/status.sh" 2>&1)"
dashboard_status=$?
printf '%s\n' "$dashboard" >"$work/l1-dashboard.txt"
queue_rows="$(printf '%s\n' "$dashboard" | grep -E '^  ai:' || true)"
errors="$(printf '%s\n' "$dashboard" | grep -iE 'error|unavailable|traceback' || true)"
ladder_check "dashboard exits 0 in an unrelated repository" "$dashboard_status" "see $work/l1-dashboard.txt"
ladder_assert "dashboard reports no error" "$errors" test -z "$errors"
ladder_assert "dashboard reports an empty queue" "$queue_rows" test -z "$queue_rows"

# 3. ${CLAUDE_PLUGIN_ROOT} resolves inside a skill body.
report="$(ladder_status_session "$project" "$install_path" "$work/l1-session.jsonl")"
session_status=$?
printf '%s\n' "$report" >"$work/l1-session-report.json"
first_command="$(printf '%s' "$report" | jq -r '.bash[0].command // ""')"
first_error="$(printf '%s' "$report" | jq -r '.bash[0].is_error')"
first_output="$(printf '%s' "$report" | jq -r '.bash[0].output // ""')"
ladder_assert "session ran the dashboard from the absolute install path" \
  "first Bash call: ${first_command:-none}" \
  test "$first_command" = "bash $install_path/skills/status/status.sh"
# shellcheck disable=SC2329 # invoked through ladder_assert
dashboard_ran() {
  [ "$session_status" = 0 ] && [ "$first_error" = false ] \
    && printf '%s' "$first_output" | grep -q 'PR PIPELINE'
}
ladder_assert "session's dashboard call succeeded" "is_error=$first_error" dashboard_ran
ladder_note "session cost: $(printf '%s' "$report" | jq -r '.cost_usd') USD"

ladder_finish "L1"
