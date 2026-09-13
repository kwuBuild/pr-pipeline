#!/usr/bin/env bash
#
# Read-only status dashboard for the PR pipeline. Safe to run continuously in a
# watch loop in a separate terminal pane:
#
#   watch -n 5 'bash $PR_PIPELINE_HOME/skills/status/status.sh'
#
# It makes no writes to pipeline state: it never edits labels, dispatches
# engines, or touches markers. (The two Python calls below invoke a resolved
# interpreter directly and take no tick lock.) It only *reads* the two state
# sources the pipeline
# externalizes, so it cannot interfere with a running tick/loop:
#
#   1. GitHub labels      = pipeline stage   (queue / reviewing / done)
#   2. worktree markers   = live engine state (pid liveness, deadline, result)
#
# Sections:
#   IN-FLIGHT ENGINES  — one row per live dispatch.json: which PR, stage, engine,
#                        whether the PID is still alive, whether its result file
#                        has landed, how long since the engine last produced
#                        output (`quiet-Nm`, or `STALLED-Nm` past the threshold),
#                        and minutes left before the hung-deadline.
#   PIPELINE STATE     — every open PR/issue carrying an ai: pipeline label,
#                        grouped by label so the queue is visible at a glance.

set -uo pipefail
# nullglob: suppress errors when globs match nothing. shopt is bash; setopt is zsh.
# Both are tried; the one for the wrong shell is a silent no-op.
shopt -s nullglob 2>/dev/null || true
setopt NULL_GLOB 2>/dev/null || true

# The target repo. Read from the tick's config.toml so the dashboard and the
# tick cannot disagree about which repository they are talking about, with
# PIPELINE_REPO as the one-off override. There is no fallback default on
# purpose: a pinned one would point a fresh install at somebody else's repo and
# render *their* queue, which reads as a plausible dashboard rather than as an
# error. Parsed with sed rather than a TOML library because this script must run
# before any interpreter is resolved.
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="${PIPELINE_REPO:-}"
if [ -z "$REPO" ]; then
  # Scoped to the [target] table: an unscoped match would read a `repo` key
  # from whichever table happened to declare one first.
  REPO="$(sed -n '/^\[target\]/,/^\[/ s/^[[:space:]]*repo[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$SKILL_DIR/../tick/config.toml" 2>/dev/null | head -1)"
fi
if [ -z "$REPO" ]; then
  echo "status: no target repo configured. Set [target] repo in" >&2
  echo "  skills/tick/config.toml, or export PIPELINE_REPO." >&2
  exit 2
fi
# Anchor on the git common dir, not --show-toplevel: run from inside a linked
# worktree the latter names that worktree, whose .claude/worktrees is empty,
# and the dashboard would report zero in-flight engines for a busy pipeline.
COMMON_DIR="$(git rev-parse --git-common-dir)"
ROOT="$(cd "$(dirname "$COMMON_DIR")" && pwd)"
WT_ROOT="$ROOT/.claude/worktrees"
# Pipeline CODE is resolved relative to this script, not to the target repo:
# under a plugin install the skills live outside the repo entirely. Pipeline
# STATE (WT_ROOT, TICK_LOG_DIR) stays repo-relative — it belongs to the repo
# being operated on. Conflating the two is what makes a moved dashboard report
# a busy pipeline as idle.
TICK_LIB="$SKILL_DIR/../tick/lib"
NOW=$(date +%s)

# Resolve an interpreter that can actually run the lib. Bare `python3` is NOT a
# safe substitute for `uv run python`: tick.py/pipeline.py need 3.11+ for
# `tomllib`, and the ambient `python3` is whatever PATH resolves (3.9 on a stock
# macOS box, and on Anaconda installs). This is about PRECEDENCE, not
# availability: a box can have 3.13 installed and still resolve `python3` to a
# 3.9. Set PR_PIPELINE_PYTHON to pin one explicitly. A too-old interpreter would
# fail every render into `?` — a dashboard reporting false idle, which is exactly
# the signal an operator uses to decide the pipeline is safe to touch.
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
PY="$(resolve_python || true)"
if [[ -z "$PY" ]]; then
  echo "WARN: no Python >= 3.11 on PATH (needed for tomllib);" >&2
  echo "      engine-progress and tick-log columns will render as '?'." >&2
fi

# Progress verdicts, one `name<TAB>label` line per marker, from the same code
# the loop's stall watcher runs. A stall is "quiet past the threshold" *and*
# identity-verified live, and this script can prove neither: `kill -0` cannot
# tell a dispatched engine from whatever process inherited its PID, and the
# thresholds would have to be duplicated in a second language. A dashboard
# raising STALLED where the tick log stays silent teaches the operator to
# distrust both surfaces, so the verdict is computed once, in Python.
#
# Takes no pipeline lock and makes no pipeline state transition, so it is safe
# against a live tick. The interpreter is the one resolved above, not bare
# `python3`: tick.py is stdlib-only but needs 3.11+ for `tomllib`, and the
# ambient `python3` is whatever the operator's PATH resolves (3.9 on a stock
# macOS box), which would fail every render into `?`. If the call cannot be
# made at all the column reads `?` (see below) rather than guessing.
PROGRESS_VERDICTS=$("$PY" "$TICK_LIB/tick.py" engine-progress \
                      --wt-root "$WT_ROOT" 2>/dev/null || true)

echo "PR PIPELINE  ·  $(date '+%Y-%m-%d %H:%M:%S')  ·  repo $REPO"
echo "================================================================"

# --- IN-FLIGHT ENGINES (worktree markers) ---------------------------------
echo
echo "IN-FLIGHT ENGINES"
found=0
# Scan both pr-* (review/fix engines) and issue-* (implement engines).
for marker in "$WT_ROOT"/pr-*/dispatch.json "$WT_ROOT"/issue-*/dispatch.json; do
  found=1
  dir="$(dirname "$marker")"
  pr=$(jq -r '.pr_number // "?"' "$marker")
  stage=$(jq -r '.stage // "?"' "$marker")
  engine=$(jq -r '.engine // "?"' "$marker")
  pid=$(jq -r '.pid // 0' "$marker")
  # tick.py writes deadline (and start_time) as floats, and bash arithmetic
  # rejects a decimal point — so floor at the read boundary. A non-number
  # falls back to 0 rather than letting jq write an error to stderr. pid
  # needs no floor: it only reaches `kill -0` and printf, never $(( )).
  deadline=$(jq -r '
      if (.deadline | type) == "number" then (.deadline | floor) else 0 end
    ' "$marker")

  if kill -0 "$pid" 2>/dev/null; then alive="alive"; else alive="DEAD"; fi

  # Progress column. Liveness above proves the process exists; this says
  # whether it is still producing — `quiet-Nm`, `STALLED-Nm`, or `-` where no
  # progress contract exists (the reviewer stage discards engine output). The
  # verdict is looked up, never derived: `?` means the report above could not
  # be produced, which is different from "no progress contract" and must not
  # be rendered as either a clean row or a stall.
  progress=$(printf '%s\n' "$PROGRESS_VERDICTS" \
    | awk -F'\t' -v key="$(basename "$dir")" '$1 == key { print $2; exit }')
  [ -n "$progress" ] || progress="?"

  # -s, not -f: implement/fix engines redirect stdout into result.json, so it
  # exists at zero bytes from the moment of spawn. tick.py's classifier applies
  # the same size>0 rule (_nonempty_file), and the two must agree on what
  # "has a result" means or this column contradicts the harvest that follows.
  if [ -s "$dir/review.json" ] || [ -s "$dir/result.json" ]; then result="result-ready"; else result="working"; fi
  ttl=$(( (deadline - NOW) / 60 ))

  printf "  PR#%-5s %-7s %-6s pid=%-7s %-5s %-13s %-12s %4sm-to-deadline\n" \
    "$pr" "$stage" "$engine" "$pid" "$alive" "$result" "$progress" "$ttl"
done
[ "$found" = 0 ] && echo "  (none — no engines dispatched)"

# --- PIPELINE STATE (GitHub labels) ---------------------------------------
echo
echo "PIPELINE STATE (open PRs by ai: label)"
gh pr list --repo "$REPO" --state open --json number,title,labels --limit 100 \
  | jq -r '
      .[]
      | . as $p
      | ($p.labels[].name | select(startswith("ai:"))) as $l
      | "\($l)\t#\($p.number)\t\($p.title[0:60])"
    ' \
  | sort \
  | awk -F'\t' '{ printf "  %-22s %-6s %s\n", $1, $2, $3 }'

echo
echo "PIPELINE STATE (open issues by ai: label)"
# Every ai: label, not just ai:available: reconciliation never escalates an
# in-flight item on a missing marker, so this listing is the only place a
# markerless ai:implementing becomes visible to the operator.
gh issue list --repo "$REPO" --state open --json number,title,labels --limit 100 \
  | jq -r '
      .[]
      | . as $i
      | ($i.labels[].name | select(startswith("ai:"))) as $l
      | "\($l)\t#\($i.number)\t\($i.title[0:60])"
    ' \
  | sort \
  | awk -F'\t' '{ printf "  %-22s %-6s %s\n", $1, $2, $3 }'

# --- LAST TICK LOG SUMMARY ------------------------------------------------------
echo
echo "LAST TICK AUDIT LOG"
PIPELINE="$SKILL_DIR/../tick/lib/pipeline.py"
TICK_LOG_DIR="$ROOT/.claude/worktrees/.tick-log"
if entries=$("$PY" "$PIPELINE" tick-log-tail --log-dir "$TICK_LOG_DIR" 2>/dev/null) \
   && [ "$(printf '%s' "$entries" | jq 'length' 2>/dev/null)" -gt 0 ]; then
  printf '%s' "$entries" \
    | jq -r '.[] | "\(.ts|tonumber|strftime("%H:%M:%S"))  PR#\(.pr // "-")\t\(.phase)\t\(.stage)\t\(.action)\t\(.dispatch_id // "")\t\((.reason // "")[0:30])\t\(.engine // "")"' \
    | awk -F'\t' '{ printf "  %s  %-8s %-10s %-16s %-24s %-30s %s\n", $1, $2, $3, $4, $5, $6, $7 }' 2>/dev/null \
    || echo "  (could not render entries)"
else
  echo "  (no tick log entries found)"
fi
echo
