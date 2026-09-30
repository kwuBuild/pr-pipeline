#!/usr/bin/env bash
#
# L4 - upgrade survival. Publish a version bump of the installed candidate,
# update the install the way an adopter does, and prove two things: the newly
# installed code is what a fresh session runs, and the operator config is
# byte-for-byte unchanged.
#
#   LADDER_WORK=/abs/scratch [PR_PIPELINE_CONFIG=/abs/config.toml] \
#     validation/l4-upgrade.sh <owner/name>
#
# Run after L1, with the same LADDER_WORK and <owner/name>: L1's local-scope
# install in the unrelated project is the one updated. Point PR_PIPELINE_CONFIG
# at the config L3 wrote to check that its targets' configuration survives;
# without it, the operator's default config path is checked.
#
# Stop every tick loop running from the install first: the update replaces
# the versioned cache entry those loops run from.
#
# The procedure is explicit because a marketplace refresh completing is not
# evidence of anything:
#   1. bump `version` in .claude-plugin/plugin.json, the update key, and push;
#   2. `claude plugin marketplace update` refreshes the catalog;
#   3. `claude plugin update` updates the installed plugin;
#   4. a fresh session (a running one keeps its old CLAUDE_PLUGIN_ROOT) runs
#      the dashboard, and the root it ran from must hold the bumped version.
#
# Step 4 makes one short model call (LADDER_SESSION_MODEL, default sonnet).

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

[ $# -eq 1 ] || ladder_die "usage: l4-upgrade.sh <owner/name of the candidate marketplace repo>"
source_repo="$1"
ladder_require claude gh git jq shasum
work="$(ladder_work)"
project="$(ladder_project "$work")"
old_version="$(ladder_installed "$project" version)"
old_path="$(ladder_installed "$project" installPath)"
[ -n "$old_version" ] || ladder_die "no local install in $project: run L1 first"

config="${PR_PIPELINE_CONFIG:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/pr-pipeline/config.toml}"
digest() { if [ -f "$1" ]; then shasum -a 256 "$1" | cut -d' ' -f1; else echo absent; fi; }
config_before="$(digest "$config")"
ladder_note "installed $old_version at $old_path; config $config ($config_before)"

# 1. Publish the bump.
source="$work/l4-source"
if [ -d "$source/.git" ]; then
  if ! { git -C "$source" fetch -q origin && git -C "$source" reset -q --hard "@{upstream}"; }; then
    ladder_die "cannot refresh $source"
  fi
else
  gh repo clone "$source_repo" "$source" -- -q 2>/dev/null || ladder_die "cannot clone $source_repo"
fi
manifest="$source/.claude-plugin/plugin.json"
published="$(jq -r .version "$manifest")"
[ "$published" = "$old_version" ] \
  || ladder_die "$source_repo publishes $published but $old_version is installed: rerun L1"
new_version="$(printf '%s' "$published" | awk -F. -v OFS=. '{ $NF = $NF + 1; print }')"
sed -i.bak "s/\"version\": \"$published\"/\"version\": \"$new_version\"/" "$manifest" && rm "$manifest.bak"
[ "$(jq -r .version "$manifest")" = "$new_version" ] || ladder_die "version bump did not apply"
git -C "$source" commit -q -am "chore: bump the ladder candidate to $new_version"
git -C "$source" push -q origin HEAD || ladder_die "cannot push the bump"
ladder_note "published $new_version to $source_repo"

# 2 and 3. Refresh the catalog, then update the installed plugin.
(cd "$project" && claude plugin marketplace update pr-pipeline) >&2 \
  || ladder_die "marketplace update failed"
(cd "$project" && claude plugin update "$LADDER_PLUGIN_ID" --scope local) >&2 \
  || ladder_die "plugin update failed"
new_path="$(ladder_installed "$project" installPath)"
installed="$(ladder_installed "$project" version)"
ladder_assert "the install record moved to $new_version" "installed=$installed at $new_path" \
  test "$installed" = "$new_version"
old_kept="no"
[ -d "$old_path" ] && old_kept="yes"
ladder_note "previous cache entry $old_path still present after update: $old_kept"

# 4. A fresh session must run the new code.
report="$(ladder_status_session "$project" "$new_path" "$work/l4-session.jsonl")"
session_status=$?
printf '%s\n' "$report" >"$work/l4-session-report.json"
command="$(printf '%s' "$report" | jq -r '.bash[0].command // ""')"
running_root="${command#bash }"
running_root="${running_root%/skills/status/status.sh}"
running_version="$(jq -r .version "$running_root/.claude-plugin/plugin.json" 2>/dev/null)"
ladder_assert "a fresh session runs the dashboard from the new install" \
  "session exit=$session_status; ran: ${command:-nothing}" \
  test "$session_status" = 0 -a "$command" = "bash $new_path/skills/status/status.sh"
ladder_assert "the code that ran reports version $new_version" \
  "running root $running_root reports ${running_version:-nothing}" \
  test "$running_version" = "$new_version"
ladder_note "session cost: $(printf '%s' "$report" | jq -r '.cost_usd') USD"

config_after="$(digest "$config")"
ladder_assert "operator config unchanged by the update ($config)" \
  "before $config_before, after $config_after" test "$config_before" = "$config_after"

ladder_finish "L4"
