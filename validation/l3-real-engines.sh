#!/usr/bin/env bash
#
# L3 - real engines. Run the installed pipeline's real `tick.sh --loop` against
# a throwaway target with real `claude` and `codex`, until the issue's PR
# reaches a terminal label. Paid: expect several engine runs per target.
#
#   LADDER_WORK=/abs/scratch validation/l3-real-engines.sh <a|b> <owner/name> <pipeline-root>
#
# <a|b> selects fixtures/l3-<a|b>/: the target's seed files (repo/), its
# default branch and operator configuration (target.env), and the issue
# (issue.md: title line, then body). Run it once per fixture, against two
# different throwaway repositories, so one operator config holds two
# differently configured targets. The config is isolated in the workspace
# through PR_PIPELINE_CONFIG and shared by both runs.
#
# Setup runs through the installed entry point, including its real route
# probe (two short model calls). Real engines are wrapped by stubs/observer.sh,
# which tees each engine's stream to the run directory: a successful harvest
# keeps no stream, and the stream is the only record of which files an engine
# read.
#
# Checks: the PR targets the fixture's default branch; its label history holds
# a rejection followed by a fix and a re-review that passes (a run approved
# first time proves nothing about the fixer: rerun with a new issue); the
# fixer read the hygiene skill doc from the pipeline root; every engine launch
# carried this target's permission mode and grant; and the tick opened no file
# in the target outside .claude/worktrees.
#
# LADDER_L3_ISSUE names another issue file in the fixture, for a rerun when
# the first review approved. LADDER_ENGINE_MODEL selects the implementer and
# fixer model (`--model`) for both the probe and the tick; a lighter model
# makes a rejecting first review more likely. LADDER_ENGINE_SETTINGS is a settings JSON the
# observer passes to every `claude` call with --settings, for a host whose
# user settings must not reach unattended engines, for example
# '{"sandbox":{"enabled":false}}' where the operator's interactive sessions
# run sandboxed with autoAllowBashIfSandboxed: that setting auto-allows Bash,
# so the engine allowlist is not enforced and setup's probe is unmeasurable.
#
# Not covered here: an issue filed from a second account (section 4 of the
# distribution design). File one by hand and confirm the tick skips it.

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

[ $# -eq 3 ] || ladder_die "usage: l3-real-engines.sh <a|b> <owner/name> <pipeline-root>"
fixture="$here/fixtures/l3-$1"
target="$2"
root="$(cd "$3" && pwd)" || ladder_die "no such pipeline root: $3"
[ -f "$fixture/target.env" ] || ladder_die "no fixture: $fixture"
# shellcheck source=fixtures/l3-a/target.env
. "$fixture/target.env"
ladder_require claude codex gh git jq
work="$(ladder_work)"
run="$work/l3-${target//\//-}/run-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$run/setup-bin" "$run/bin" "$run/hook" "$run/observed-setup" "$run/observed"
py="$(ladder_python "$root")"
export PR_PIPELINE_CONFIG="$work/l3-operator/config.toml"

# Observers go first on PATH; each wraps the binary PATH resolved before it.
install_observers() {
  local dir="$1" logs="$2" name real
  for name in claude codex; do
    real="$(command -v "$name")"
    sed -e "s|@REAL@|$real|" -e "s|@NAME@|$name|" -e "s|@LOG_DIR@|$logs|" \
      -e "s|@PYTHON@|$py|" \
      -e "s|@SELF_DIR@|$dir|" -e "s|@CLAUDE_SETTINGS@|${LADDER_ENGINE_SETTINGS:-}|" \
      "$here/stubs/observer.sh" >"$dir/$name"
    chmod +x "$dir/$name"
  done
}
install_observers "$run/setup-bin" "$run/observed-setup"
install_observers "$run/bin" "$run/observed"
cp "$here/stubs/sitecustomize.py" "$run/hook/"

# --- The target -----------------------------------------------------------

if ! gh repo view "$target" >/dev/null 2>&1; then
  ladder_note "creating private throwaway $target (default branch $DEFAULT_BRANCH)"
  gh repo create "$target" --private \
    --description "Throwaway target for the pr-pipeline independence ladder (L3). Safe to delete." >&2 \
    || ladder_die "cannot create $target"
  seed="$run/seed"
  gh repo clone "$target" "$seed" -- -q 2>/dev/null || ladder_die "cannot clone $target"
  git -C "$seed" checkout -q -b "$DEFAULT_BRANCH"
  cp -R "$fixture/repo/." "$seed/"
  git -C "$seed" add -A
  git -C "$seed" commit -q -m "chore: seed the L3 ladder target"
  git -C "$seed" push -q -u origin "$DEFAULT_BRANCH" || ladder_die "cannot push the seed commit"
  gh repo edit "$target" --default-branch "$DEFAULT_BRANCH" >&2 || ladder_die "cannot set the default branch"
fi
default_branch="$(gh repo view "$target" --json defaultBranchRef --jq .defaultBranchRef.name)"
ladder_assert "target default branch is $DEFAULT_BRANCH" "got $default_branch" \
  test "$default_branch" = "$DEFAULT_BRANCH"
checkout="$work/l3-${target//\//-}/checkout"
if [ ! -d "$checkout/.git" ]; then
  gh repo clone "$target" "$checkout" -- -q 2>/dev/null || ladder_die "cannot clone $target"
fi

# --- Setup, serialised: both targets share one operator config ------------

# A run killed inside this block leaves the lock directory behind, and every
# later run waits on it: remove it by hand once no setup is running.
lock="$PR_PIPELINE_CONFIG.ladder-lock"
mkdir -p "$(dirname "$PR_PIPELINE_CONFIG")"
ladder_note "waiting for the setup lock $lock"
until mkdir "$lock" 2>/dev/null; do sleep 2; done
(
  # shellcheck disable=SC2030 # the observers apply to setup's subshell only
  export PATH="$run/setup-bin:$PATH"
  setup() { (cd "$checkout" && bash "$root/skills/setup/setup.sh" "$@"); }
  login="$(setup login | jq -r .login)"
  setup labels "$target" >"$run/setup-labels.json" || exit 1
  setup probe "$target" ${LADDER_ENGINE_MODEL:+--model "$LADDER_ENGINE_MODEL"} \
    --verify-tool "$VERIFY_TOOL" >"$run/setup-probe.json"
  setup write-config "$target" --login "$login" --permission-mode "$PERMISSION_MODE" \
    --verify-tool "$VERIFY_TOOL" >"$run/setup-write-config.json"
)
setup_status=$?
rmdir "$lock"
outcome="$(jq -r .outcome "$run/setup-probe.json" 2>/dev/null)"
ladder_assert "setup probe passes on the real route" "outcome=$outcome; see $run/setup-probe.json" \
  test "$outcome" = pass
[ "$setup_status" = 0 ] || ladder_die "setup failed; see $run/setup-*.json"

# --- The run --------------------------------------------------------------

issue_file="$fixture/${LADDER_L3_ISSUE:-issue.md}"
[ -f "$issue_file" ] || ladder_die "no such issue fixture: $issue_file"
title="$(head -n 1 "$issue_file")"
issue_url="$(tail -n +2 "$issue_file" \
  | gh issue create --repo "$target" --label ai:available --title "$title" --body-file -)" \
  || ladder_die "cannot seed the issue"
issue="${issue_url##*/}"
ladder_note "seeded issue #$issue; starting the loop"

ai_labels() {
  gh "$1" view "$2" --repo "$target" --json labels \
    --jq '[.labels[].name | select(startswith("ai:"))] | join(",")' 2>/dev/null
}
pr_closing() {
  gh pr list --repo "$target" --state all --json number,body \
    --jq ".[] | select(.body | test(\"(?m)^Closes #$1\$\")) | .number" 2>/dev/null | head -n 1
}

(
  # shellcheck disable=SC2031 # the loop's own subshell, not setup's
  export PATH="$run/bin:$PATH" PYTHONPATH="$run/hook" OPEN_AUDIT_LOG="$run/opened.jsonl"
  cd "$checkout" && exec bash "$root/skills/tick/tick.sh" --loop \
    ${LADDER_ENGINE_MODEL:+--model "$LADDER_ENGINE_MODEL"} "#$issue"
) >"$run/loop.log" 2>&1 &
loop_pid=$!
started=$(date +%s)
terminal=""
pr=""
while kill -0 "$loop_pid" 2>/dev/null; do
  sleep 60
  pr="$(pr_closing "$issue")"
  issue_labels="$(ai_labels issue "$issue")"
  pr_labels=""
  [ -n "$pr" ] && pr_labels="$(ai_labels pr "$pr")"
  ladder_note "$(( ($(date +%s) - started) / 60 ))m: issue #$issue ${issue_labels:-none}; PR ${pr:-none} ${pr_labels}"
  case ",$pr_labels,$issue_labels," in
    *,ai:ready-for-human,* | *,ai:needs-human-decision,* | *,ai:max-iterations,* | *,ai:blocked,*)
      terminal="${pr_labels:-$issue_labels}"
      break ;;
  esac
  if [ $(( $(date +%s) - started )) -gt $(( ${LADDER_L3_MAX_MINUTES:-180} * 60 )) ]; then
    terminal="timeout"
    break
  fi
done
# A terminal label leaves no engine in flight for this target; stop the loop's
# quiescent heartbeats rather than wait them out.
kill -TERM "$loop_pid" 2>/dev/null
wait "$loop_pid"
loop_status=$?
ladder_note "loop stopped (exit $loop_status) at: ${terminal:-loop exited by itself}"

# --- Assertions -----------------------------------------------------------

ladder_assert "the harvest opened a PR closing #$issue" "no PR" test -n "$pr"
[ -n "$pr" ] || ladder_finish "L3 ($target)"
base="$(gh pr view "$pr" --repo "$target" --json baseRefName --jq .baseRefName)"
ladder_assert "PR #$pr targets the default branch ($DEFAULT_BRANCH)" "base=$base" \
  test "$base" = "$DEFAULT_BRANCH"
ladder_assert "PR #$pr ends at ai:ready-for-human" "ended at: ${terminal:-unknown}" \
  test "$(ai_labels pr "$pr")" = "ai:ready-for-human"

history="$(gh api "repos/$target/issues/$pr/timeline" --paginate \
  --jq '.[] | select(.event == "labeled") | .label.name | select(startswith("ai:"))' | paste -sd, -)"
printf '%s\n' "$history" >"$run/pr-label-history.txt"
# shellcheck disable=SC2329 # invoked through ladder_assert
fixed_after_rejection() {
  [[ ",$history," =~ ,ai:changes-requested,ai:fixing,ai:revised,ai:reviewing,.*ai:ready-for-human, ]]
}
ladder_assert "a review rejected, the fixer ran, and a re-review passed" \
  "history=$history" fixed_after_rejection

ladder_assert "a real fixer Read the hygiene skill doc from the pipeline root" \
  "see $run/observed and the tick launch log" \
  "$py" "$here/evidence.py" fixer-read "$run/observed" "$checkout" "$root" "$pr"

head_ref="$(gh pr view "$pr" --repo "$target" --json headRefName --jq .headRefName)"
ladder_assert "all claude launches carried exactly $PERMISSION_MODE and this target's grants" \
  "see $run/observed/*.launch.json" \
  "$py" "$here/evidence.py" grants "$run/observed" "$checkout" "$issue" "$pr" \
    "$head_ref" "$PERMISSION_MODE" "$VERIFY_TOOL"

ladder_assert "the tick opened no file in the target outside .claude/worktrees" \
  "see $run/opened.jsonl" \
  "$py" "$here/evidence.py" audit "$run/opened.jsonl" "$checkout" "$root"

cost="$(cat "$run"/observed/claude-*.jsonl 2>/dev/null | jq -Rn '
  [inputs | fromjson? | select(.type == "result") | .total_cost_usd // 0] | add // 0')"
tokens="$(cat "$run"/observed/codex-*.jsonl 2>/dev/null | jq -Rn '
  [inputs | fromjson? | select(.type == "turn.completed")
   | (.usage.input_tokens // 0) + (.usage.output_tokens // 0)] | add // 0')"
ladder_note "cost: claude engines ${cost} USD; codex reviewer ${tokens} tokens; wall $(( ($(date +%s) - started) / 60 ))m"
ladder_note "evidence: $run"
ladder_finish "L3 ($target)"
