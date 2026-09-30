#!/usr/bin/env bash
#
# L2 - stubbed tick, controller only. Drive two issues on a real GitHub
# repository that is not this one, with canned engines:
#
#   1. the whole cycle: issue ai:available -> implement -> PR -> review rejects
#      -> fix -> re-review -> ai:ready-for-human;
#   2. an escalation: the implementer reports NEEDS_HUMAN_DECISION, the issue
#      goes to ai:needs-human-decision, and the harvest archives the engine
#      result under .claude/worktrees/.harvest-evidence/. A successful harvest
#      archives nothing, so this is the path that proves archival.
#
#   LADDER_WORK=/abs/scratch validation/l2-stubbed-tick.sh <owner/name> <pipeline-root>
#
# <owner/name> is a throwaway repository. When it does not exist it is created
# private, with `trunk` as its default branch, so base-ref resolution is
# exercised off `main`. <pipeline-root> is the pipeline under test: pass the
# L1 install path to run the installed code rather than a checkout.
#
# `claude` and `codex` are the canned stubs in stubs/, first on PATH; no model
# is called. Setup, labels, issues, PRs, pushes and comments are real. The
# operator config is isolated in the workspace through PR_PIPELINE_CONFIG.
# Every file the pipeline's Python opens is recorded, and none may be in the
# target checkout outside `.claude/worktrees`.
#
# What this cannot prove: anything about real engine permissions (L3).

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

[ $# -eq 2 ] || ladder_die "usage: l2-stubbed-tick.sh <owner/name> <pipeline-root>"
target="$1"
root="$(cd "$2" && pwd)" || ladder_die "no such pipeline root: $2"
ladder_require gh git jq
work="$(ladder_work)/l2-${target//\//-}"
run="$work/run-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$run/bin" "$run/hook" "$run/state"
py="$(ladder_python "$root")"

for stub in claude codex; do
  { printf '#!%s\n' "$py"; sed "s|@STATE_DIR@|$run/state|" "$here/stubs/$stub.py"; } >"$run/bin/$stub"
  chmod +x "$run/bin/$stub"
done
cp "$here/stubs/sitecustomize.py" "$run/hook/"
export PATH="$run/bin:$PATH"
export PR_PIPELINE_CONFIG="$work/operator/config.toml"

# --- The target -----------------------------------------------------------

if ! gh repo view "$target" >/dev/null 2>&1; then
  ladder_note "creating private throwaway $target (default branch trunk)"
  gh repo create "$target" --private \
    --description "Throwaway target for the pr-pipeline independence ladder (L2). Safe to delete." >&2 \
    || ladder_die "cannot create $target"
  seed="$work/seed"
  gh repo clone "$target" "$seed" -- -q 2>/dev/null || ladder_die "cannot clone $target"
  git -C "$seed" checkout -q -b trunk
  printf '# L2 ladder target\n\nThrowaway repository for pr-pipeline validation.\n' >"$seed/README.md"
  # shellcheck disable=SC2016 # the backticks are Markdown, not a substitution
  printf '# Agent guide\n\nVerification: run `test -f README.md` before committing.\n' >"$seed/AGENTS.md"
  git -C "$seed" add README.md AGENTS.md
  git -C "$seed" commit -q -m "chore: seed the L2 ladder target"
  git -C "$seed" push -q -u origin trunk || ladder_die "cannot push the seed commit"
  gh repo edit "$target" --default-branch trunk >&2 || ladder_die "cannot set the default branch"
fi
default_branch="$(gh repo view "$target" --json defaultBranchRef --jq .defaultBranchRef.name)"
checkout="$work/checkout"
if [ ! -d "$checkout/.git" ]; then
  gh repo clone "$target" "$checkout" -- -q 2>/dev/null || ladder_die "cannot clone $target"
fi
ladder_note "target $target, default branch $default_branch, checkout $checkout"

# --- Setup, through the installed entry point -----------------------------

setup() { (cd "$checkout" && bash "$root/skills/setup/setup.sh" "$@"); }
login="$(setup login | jq -r .login)"
[ -n "$login" ] || ladder_die "setup login returned nothing"
setup labels "$target" >"$run/setup-labels.json" || ladder_die "setup labels failed"
setup probe "$target" --verify-tool 'test -f README.md' >"$run/setup-probe.json"
outcome="$(jq -r .outcome "$run/setup-probe.json")"
ladder_assert "setup probe passes on the stub route" "outcome=$outcome" test "$outcome" = pass
setup write-config "$target" --login "$login" --permission-mode auto \
  --verify-tool 'test -f README.md' >"$run/setup-write-config.json" \
  || ladder_die "setup write-config failed"

# --- The cycles -----------------------------------------------------------

seed_issue() {
  local url
  url="$(gh issue create --repo "$target" --label ai:available \
    --title "L2 ladder run ${run##*/run-}: $1" --body "$2")" || ladder_die "cannot seed an issue"
  printf '%s\n' "${url##*/}"
}

ai_labels() {
  gh "$1" view "$2" --repo "$target" --json labels \
    --jq '[.labels[].name | select(startswith("ai:"))] | join(",")'
}

pr_closing() {
  gh pr list --repo "$target" --state all --json number,body \
    --jq ".[] | select(.body | test(\"(?m)^Closes #$1\$\")) | .number" | head -n 1
}

tick_failures=""
# drive <issue> <kind> <label> - targeted ticks until the issue (kind=issue) or
# its closing PR (kind=pr) carries <label>, or the tick budget runs out.
drive() {
  local issue="$1" kind="$2" want="$3" n status number labels
  for n in $(seq 1 "${LADDER_L2_MAX_TICKS:-10}"); do
    (cd "$checkout" && bash "$root/skills/tick/tick.sh" "#$issue") >>"$run/ticks.log" 2>&1
    status=$?
    [ "$status" = 0 ] || tick_failures="$tick_failures #$issue/tick$n=$status"
    number="$issue"
    [ "$kind" = pr ] && number="$(pr_closing "$issue")"
    labels=""
    [ -n "$number" ] && labels="$(ai_labels "$kind" "$number")"
    ladder_note "#$issue tick $n exit $status; $kind ${number:-none} ${labels}"
    [ "$labels" = "$want" ] && return 0
    # The canned engines exit within seconds; the next tick harvests them.
    sleep 5
  done
  return 1
}

export PYTHONPATH="$run/hook" OPEN_AUDIT_LOG="$run/opened.jsonl"
: >"$OPEN_AUDIT_LOG"
issue="$(seed_issue "full cycle" "Seeded by validation/l2-stubbed-tick.sh; the canned engines implement it.")"
ladder_note "seeded issue #$issue (full cycle)"
drive "$issue" pr ai:ready-for-human
escalated="$(seed_issue "escalation" "Seeded by validation/l2-stubbed-tick.sh. L2-ESCALATE: the canned implementer escalates.")"
ladder_note "seeded issue #$escalated (escalation)"
drive "$escalated" issue ai:needs-human-decision
unset PYTHONPATH OPEN_AUDIT_LOG

# --- Assertions -----------------------------------------------------------

ladder_assert "every tick exits 0" "$tick_failures" test -z "$tick_failures"
pr="$(pr_closing "$issue")"
ladder_assert "the harvest opened a PR closing #$issue" "no PR" test -n "$pr"
[ -n "$pr" ] || ladder_finish "L2"

base="$(gh pr view "$pr" --repo "$target" --json baseRefName --jq .baseRefName)"
ladder_assert "PR #$pr targets the default branch ($default_branch)" "base=$base" \
  test "$base" = "$default_branch"
final_label="$(ai_labels pr "$pr")"
ladder_assert "PR #$pr ends at ai:ready-for-human" "labels=$final_label" \
  test "$final_label" = "ai:ready-for-human"

label_history() {
  gh api "repos/$target/issues/$1/timeline" --paginate \
    --jq '.[] | select(.event == "labeled") | .label.name | select(startswith("ai:"))' \
    | paste -sd, -
}
pr_history="$(label_history "$pr")"
expected="ai:implemented,ai:reviewing,ai:changes-requested,ai:fixing,ai:revised,ai:reviewing,ai:ready-for-human"
ladder_assert "PR label history is the full reject-then-fix cycle" \
  "got $pr_history" test "$pr_history" = "$expected"
issue_history="$(label_history "$issue")"
ladder_assert "issue #$issue went ai:available -> ai:implementing" "got $issue_history" \
  test "$issue_history" = "ai:available,ai:implementing"
escalated_history="$(label_history "$escalated")"
ladder_assert "issue #$escalated went ai:available -> ai:implementing -> ai:needs-human-decision" \
  "got $escalated_history" \
  test "$escalated_history" = "ai:available,ai:implementing,ai:needs-human-decision"

calls="$run/state/claude-calls.jsonl"
roles="$(jq -r .role "$calls" | sort | paste -sd, -)"
ladder_assert "claude ran the probe twice, the implementer twice and the fixer once" \
  "roles=$roles" test "$roles" = "fixer,implementer,implementer,probe,probe"
reviews="$(jq -r .verdict "$run/state/codex-calls.jsonl" | paste -sd, -)"
ladder_assert "codex reviewed twice: reject, then approve" "verdicts=$reviews" \
  test "$reviews" = "changes_requested,ready_for_human"
skill_doc="$(jq -r 'select(.role == "fixer") | .skill_doc' "$calls")"
ladder_assert "the fixer's bound skill doc resolved inside the pipeline root" \
  "skill_doc=$skill_doc" test "$skill_doc" = "$root/skills/review-response-hygiene/SKILL.md"

worktrees="$(cd "$checkout" && pwd -P)/.claude/worktrees"
engine_dirs="$(jq -r 'select(.role != "probe") | .cwd' "$calls")"
outside="$(printf '%s\n' "$engine_dirs" | grep -v "^$worktrees/" || true)"
ladder_assert "engines ran in pipeline worktrees under the target" \
  "outside $worktrees: $outside" test -n "$engine_dirs" -a -z "$outside"
leftover="$(find "$worktrees" -mindepth 1 -maxdepth 1 \( -name 'issue-*' -o -name 'pr-*' \) | paste -sd, -)"
ladder_assert "harvest removed every engine worktree" "$leftover" test -z "$leftover"

stage_counts="$(cat "$worktrees"/.tick-log/tick-*.jsonl \
  | jq -r --arg pr "$pr" --arg issue "$issue" '
      select((.action == "launch" or .action == "harvest")
             and ((.pr | tostring) == $pr or (.pr | tostring) == $issue))
      | "\(.action):\(.stage)"' | sort | uniq -c | awk '{print $2 "=" $1}' | paste -sd, -)"
ladder_assert "tick log: one implement, two reviews and one fix launched and harvested" \
  "got $stage_counts" test "$stage_counts" \
  = "harvest:fix=1,harvest:implement=1,harvest:review=2,launch:fix=1,launch:implement=1,launch:review=2"

bundle="$(find "$worktrees/.harvest-evidence" -mindepth 1 -maxdepth 1 -type d \
  -name "issue-$escalated-*" 2>/dev/null | head -n 1)"
ladder_assert "the escalation archived its engine result under .harvest-evidence" \
  "no issue-$escalated-* bundle" test -n "$bundle" -a -f "$bundle/result.json"

ladder_assert "no file opened in the target outside .claude/worktrees" \
  "see $run/opened.jsonl" \
  "$py" "$here/evidence.py" audit "$run/opened.jsonl" "$checkout" "$root"

ladder_note "evidence: $run"
ladder_finish "L2"
