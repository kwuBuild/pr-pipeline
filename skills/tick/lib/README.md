# pr-pipeline helper library

`pipeline.py` holds the bug-prone decisions a pipeline tick makes about a finished
review — schema validation, verdict→label mapping, iteration counting, base-SHA
drift, trusted-candidate gating, marker-based admission, and dispatch-liveness
classification — as small, deterministic, unit-tested functions plus a thin CLI.
`process_control.py` owns the one effectful helper: bounded TERM→KILL rollback. The
`pr-pipeline:tick` skill shells out to these helpers rather than re-deriving control
logic in inline bash each tick.

## Constraints

- **Stdlib only.** This directory installs against a target repo it does not
  own, so it runs under a bare `python3` (3.11+) and takes no dependency on any
  target's toolchain.
  Do not add third-party imports.
- All logic lives in tested functions; the CLI is I/O glue. New behaviour starts
  with a failing test in `test_pipeline.py`.

## Minimum reviewer version

Binary execution and structured-output compatibility are verified by
`../preflight.sh` against a minimum version floor:

```
codex-cli 0.142.5
```

Any version at or above the floor is accepted. The floor tracks the build the
reviewer argv contract was verified on — ``0.142.5`` for bare ``codex exec``
with ``--ignore-rules``, ``-s read-only``, and ``--output-schema`` honored for
the final message (the ``exec review`` subcommand ignores it and emits prose,
which is why dispatch does not use it). Bump the floor only alongside
re-verifying the emitted reviewer argv against the newer CLI. The read-only
denial probe remains model-reported and explicitly unverified.

## CLI

```bash
PIPELINE=$PR_PIPELINE_HOME/skills/tick/lib/pipeline.py
# The same resolver tick.sh, preflight.sh, and the dashboard use: pipeline.py
# needs 3.11+ for `tomllib`, and a bare `python3` is a 3.9 on a stock macOS box.
PY=$(bash $PR_PIPELINE_HOME/skills/tick/resolve-python.sh)

# Decide a finished review's outcome (prints a decision JSON object).
# --comments accepts the bare array (gh ... -q .comments) or the {"comments": [...]} object.
# Two-SHA model: --pinned-base is the base tip snapshot from dispatch.json
# (base_tip_sha) compared against --current-base for drift; --diff-base-sha is the
# merge-base (dispatch.json base_sha) the reviewer's diff was actually anchored to,
# embedded in the audit comment and idempotency token. Omit --diff-base-sha only for
# markers that predate the split (both roles then fall back to --pinned-base).
# --pinned-base-ref/--current-base-ref carry the base branch *names* (dispatch
# marker vs live point-read): a mismatch means the PR was retargeted under the
# review and the decision is a discard, even when every SHA is unchanged. The
# pinned name is also part of the comment's idempotency token, so a post-retarget
# round with an identical SHA triple still posts its own comment.
"$PY" "$PIPELINE" harvest-review \
  --review review.json --comments comments.json \
  --pinned-base <sha> --current-base <sha> \
  --diff-base-sha <sha> \
  --pinned-head <sha> --current-head <sha> \
  [--pinned-base-ref <ref> --current-base-ref <ref>] \
  [--pre-label ai:implemented|ai:revised] [--threshold 5]

# Decide a finished fixer's outcome (prints a decision JSON object). The
# FIXES_PUSHED sentinel is the engine's claim, not proof: when --pinned-head
# (dispatch.json head_sha) and --current-head (the PR's live head, point-read at
# harvest) are both given and EQUAL, the claim is contradictory — a fast-forward
# push always advances the head and force-push is not permitted — so the decision
# escalates to ai:needs-human-decision with head_advanced=false instead of
# relabeling to ai:revised. Trusting the claim would re-enter a review round with
# an identity identical to the last one; its replay-suppressed comment freezes
# the comment-derived iteration count below the cap, looping reviewer+fixer spend
# without a terminal state. Empty head values disable the check (markers that
# predate push verification).
#
# --body-revision reproduces the comment's PR-body line for a run in which the
# fixer offered a replacement body (see the tick's `_fixer_body_revision`).
# It only renders the line; applying the body is the tick's job. The accepted
# values are `pipeline._BODY_REVISION_INPUTS` — every state the classifier
# returns — and `harvest-fix --help` prints the current set rather than this
# comment restating it.
"$PY" "$PIPELINE" harvest-fix \
  --result result.json [--pre-label ai:changes-requested] \
  [--pinned-head <sha> --current-head <sha>] \
  [--body-revision <state>]

# Validate a review.json against the schema (prints verdict; exit 1 + "INVALID" if not).
"$PY" "$PIPELINE" validate-review --review review.json

# Classify a dispatch's liveness (prints {"state": ...}). State ∈
# complete|live|stale|hung|launching. `complete` requires the PID to have exited (a
# result alongside a live PID is `live`, to avoid harvesting a mid-write file). A
# `launching` lease marker (no published PID) reports `launching`, not `stale`, so
# reconcile fails closed instead of re-dispatching. The marker's own `state` field is
# read from --dispatch.
"$PY" "$PIPELINE" classify-dispatch \
  --dispatch dispatch.json --now <epoch> --pid-alive 0|1 --result-exists 0|1

# Atomically write stdin to a file (temp-then-rename).
printf '%s' "$json" | "$PY" "$PIPELINE" atomic-write --path dispatch.json

# Report whether a diff touches a reviewer-governing file (CLAUDE.md/AGENTS.md at
# any level, or a path listed under the resolved checklist_paths). Reads paths
# from stdin; prints
# {"touches_policy": true|false}. Dispatch escalates a true result to a human.
# --repo selects the target's [repos."owner/name"] config layer; without it only
# the operator-wide defaults apply.
git diff --name-only <base>...<head> \
  | "$PY" "$PIPELINE" touches-review-policy --repo owner/name

# Enforce the shipped credential boundary before worktree/auth exposure. Reads the
# gh pr view JSON object from stdin; only the exact owner-authored same-repo shape
# prints {"trusted": true}. Missing or mismatched metadata fails closed.
gh pr view 100 --json author,headRepository,isCrossRepository \
  | "$PY" "$PIPELINE" validate-review-candidate \
      --trusted-author "$PIPELINE_TRUSTED_AUTHOR" --trusted-repo "$PIPELINE_REPO"

# Report whether a dispatch marker is structurally valid for a given PR/stage
# (prints {"valid": true|false}). Validation is by type + enumeration + identity and
# rejects unknown fields: the marker must carry the FULL provenance set (engine, stage,
# pr_number, deadline, base_ref, base_sha, head_sha, pre_label) and nothing extra, with
# a non-empty string engine, a known stage matching --stage, a real (non-bool) int
# pr_number matching --pr, non-empty string base_ref/base_sha/head_sha, a pre_label that
# is one of the real pre-dispatch labels (ai:implemented/ai:revised — never an arbitrary
# or terminal label a revert would promote to), a finite numeric deadline (NaN/Infinity
# rejected), and EXACTLY ONE ownership signal — a usable pid XOR a `launching` lease
# state (a marker carrying both, or a pid masking an invalid state, is rejected).
# The optional base_tip_sha (the freshness anchor of the two-SHA model) is validated
# as provenance when present: review markers only, canonical 40-hex commit OID —
# a malformed value or the field on an implement/fix marker is rejected. The
# tick runs this BEFORE classify so a corrupt or mis-targeted marker fails closed
# (treated as absent) instead of driving an incorrect liveness/revert decision.
"$PY" "$PIPELINE" validate-marker \
  --dispatch dispatch.json --pr 100 [--stage review]

# Decide HARVEST's action for one in-flight review by folding the PR's current
# label into the marker/liveness state (prints {"action", "kill", "label"}).
# action ∈ harvest|leave|revert|escalate|cleanup|kill_escalate|kill_cleanup|noop.
# A `launching` state escalates (never re-dispatches: the paid child's liveness is
# unprovable). --dispatch-attempts feeds the durable resultless-failure counter:
# once a revert would reach --attempt-threshold, the action becomes `escalate`
# instead of `revert` (bounded spend). The marker must
# pass valid_marker (type + enum + --pr/--stage identity) or it is treated as absent —
# an empty/partial/mis-targeted marker cannot drive a stale revert.
#
# A markerless in-flight item is ALWAYS a no-op. Markers are machine-local, so
# their absence proves nothing: it is equally consistent with another machine's
# live dispatch and with the documented manual `ai:implementing` pickup. The
# pr-pipeline:status dashboard makes the item *visible* rather than having a tick
# relabel work it cannot prove it owns — but the dashboard reads only this
# machine's markers, so an absent engine row is not custody proof for an operator
# either. Recovery needs out-of-band confirmation. See
# docs/architecture.md -> "Why custody is not inferred, and not yet proven".
"$PY" "$PIPELINE" reconcile-dispatch \
  --dispatch dispatch.json --pr 100 [--stage review] --current-label ai:reviewing \
  --classify-state complete|live|stale|hung|launching \
  [--dispatch-attempts 0] [--attempt-threshold 3]

# Stop a just-spawned reviewer within bounded TERM/KILL grace periods. Exit zero and
# {"stopped": true} only after death is confirmed; otherwise preserve PID ownership.
"$PY" $PR_PIPELINE_HOME/skills/tick/lib/process_control.py terminate \
  --pid <pid> --grace 5 --kill-grace 2 --poll 0.1

# --- Tick log helpers (structured per-tick audit log) ---

# Open a new tick log file (prunes old ones, prints path). Call once per tick, before Phase 1.
"$PY" "$PIPELINE" tick-log-open \
  --log-dir .claude/worktrees/.tick-log [--keep 50]

# Append one decision entry to the tick log. Non-fatal wrapper: || true.
"$PY" "$PIPELINE" tick-log-write \
  --log-path <path> \
  --entry '{"ts":1234567890,"pr":123,"phase":"harvest","stage":"review","action":"revert","reason":"stale"}'

# Print all entries from the latest tick log as a JSON array. Used by pr-pipeline:status.
"$PY" "$PIPELINE" tick-log-tail \
  --log-dir .claude/worktrees/.tick-log
```

### Machine-readable tick-state line

Every `main()` exit path emits a single parseable line to stderr:

```
tick-state: status=ok in_flight=2 queue=true quiescent=false held=0
```

| field | values | meaning |
|---|---|---|
| `status` | `ok` / `FAILED` / `PARTIAL` | Tick exit status. `FAILED` = reconcile failed (dispatch skipped). `PARTIAL` = reconcile OK but authoritative label read failed (dispatch skipped). |
| `in_flight` | integer / `unknown` | Count of *supervisable* engines in flight — this machine's dispatch markers, minus held slots. A shared `ai:` label with no local marker is not counted. `unknown` on failed/partial paths where the count could not be determined. |
| `queue` | `true` / `false` / `unknown` | Whether any item carries a dispatch-eligible label (`ai:available`, `ai:implemented`, `ai:revised`, `ai:changes-requested`). `unknown` on failed/partial paths. |
| `quiescent` | `true` / `false` / `unknown` | `true` when `in_flight=0` AND `queue=false` (every tracked item is terminal, no live dispatch). `unknown` whenever either input is unknown. |
| `held` | integer / `unknown` | Count of occupied items excluded from `in_flight` because GitHub carries `ai:needs-human-decision` or `ai:blocked` **and** the pipeline has no pending action on them (see the skill README's "Held engines"). A held engine that is still live, or one the pipeline can still terminate, is *not* excluded. They all still hold admission slots. The labels—not a local receipt—are the durable authority for *workflow* custody and include closed/merged items. |

On FAILED/PARTIAL exit paths (or crash/startup-probe-failed), fields that could not
be computed are emitted as `unknown` — the line never claims quiescence on a failed tick.

The same fields are appended as a `tick_state` object inside the `tick_complete`
summary entry in the tick log (`.tick-log/tick-*.jsonl`), so the dashboard and
historical queries can read them.

### `harvest-review` decision object

| key | meaning |
|---|---|
| `action` | `promote` \| `request_changes` \| `max_iterations` \| `discard` \| `escalate` (malformed verdict, or `changes_requested` with no blocker/major finding) |
| `label` | GitHub label to set for `action` |
| `sentinel` | logical verdict token (`NO_BLOCKERS_READY_FOR_HUMAN` / `BLOCKERS_FOUND`) |
| `verdict` | validated verdict string, or `null` if the reviewer output was malformed |
| `base_retarget` | `true` if the PR's base *branch* changed under the review (→ `discard`, checked before SHA drift — a retarget is invisible to the SHA checks when the old ref's tip and the head are unchanged) |
| `base_drift` | `true` if the base moved under the review (→ `discard`) |
| `head_drift` | `true` if the PR head moved under the review (→ `discard`) |
| `rounds` | prior recorded review rounds (iteration authority is the PR comments) |
| `post_comment` | whether to post `comment_body` (`false` on an idempotent replay) |
| `comment_body` | rendered review comment (marker + per-round diff-base:base-tip:head:base-ref token; empty when not posting) |

Promotion is gated on the verdict **and** the findings: a `ready_for_human` verdict
that still carries a blocker/major finding is routed to `request_changes`, never
promoted. `post_comment` is `false` when a comment for this exact
(diff base, base tip, head, base ref) round was already posted (replay safety), so a
comment lands at most once per reviewed round — while a re-review of the same head
against a *new* base still posts. The base branch *name* is part of the identity for
the same reason it is part of the retarget check: a PR retargeted to a branch whose
tip is the same commit changes none of the SHAs, and the pre-retarget comment must
not suppress the fresh round's post (the posted comment is the next fixer's
handoff). Comments whose marker predates the `base_ref` key carry the legacy
three-part token; the formats never match each other, so a crash-window replay
across the upgrade re-posts at most one comment.

### `harvest-fix` decision keys

| key | meaning |
|---|---|
| `action` | `recheck` \| `escalate` \| `retry` |
| `label` | GitHub label to set for `action` |
| `sentinel` | extracted completion sentinel, or `null` if the engine emitted none |
| `head_advanced` | present (and `false`) only for the contradictory `FIXES_PUSHED`-without-a-push escalate |
| `failure_class` | present only on `retry` (`quota_or_limit`) |
| `post_comment` | `true` on both terminal outcomes, absent on `retry` |
| `comment_body` | rendered fixer comment (marker, head before → after, outcome, sentinel, a bounded engine excerpt; plus the escalation reason on `escalate`, and a `- PR body:` line whenever `--body-revision` is given) |

The tick posts `comment_body` **before** flipping the label and treats a failed
post as a failed harvest: the label stays put and the marker + scratch survive for
the next tick, matching reviewer semantics. Without it the PR would show new
commits and a changed label — including the terminal `ai:needs-human-decision` —
with no stated reason, leaving the run auditable only from a machine-local
evidence archive.

Replay safety is receipt-based rather than comment-derived: a successful post
writes `<WT_ROOT>/.receipts/pr-<N>-fix-comment.json`, recording the run's
`dispatch_id`, a `decision_key` (the target label plus the rendered body,
hashed), and the URL GitHub returned for the comment. A receipt suppresses a
repost only when it parses as controller-written JSON, names the *same*
`dispatch_id` and `decision_key`, and carries that URL. The decision half of the
key is load-bearing: the harvest point-reads a fresh head and recomputes
`harvest_fix` on every retry, so a dispatch-only key would let a first attempt's
`escalate` comment suppress the repost while a later attempt — seeing the push
land — flips the item to `ai:revised` underneath it. Nothing counts fixer comments (unlike review
comments, which are the iteration-cap authority), so this covers the replay
surface without a `gh` read whose failure would stall the harvest. `retry` posts
nothing — the label reverts to `pre_label` for re-dispatch, so there is no outcome
to record yet.

Receipts live under `WT_ROOT` and **not** in the item's scratch. A fixer scratch is
a checked-out worktree of the PR branch, so a receipt kept there would be
author-controlled: a tracked file at the receipt path would forge the "comment
landed" proof and let the label move with nothing published. `WT_ROOT` is
controller-owned (alongside `.iter` and `.harvest-evidence`) and is scanned only
for `pr-`/`issue-` prefixed entries, so a dotted subdirectory is inert there.
Because a receipt now outlives the scratch, the `dispatch_id` key is what keeps an
earlier run's receipt from suppressing a later run's comment; the terminal harvest
retires them.

The guarantee is **at-least-once, not exactly-once**. If the post lands and the
process dies before the receipt is written, the next tick posts one duplicate.
Failing a landed post would be worse, so that crash window is accepted rather than
closed.

Engine-authored text is embedded on **both** terminal outcomes. An escalation asks
a human to decide and they need the engine's words. A `recheck` needs it for a
different reason: only the `escalate` branch calls `_archive_harvest_evidence`, so
a successful fixer run archives nothing and `cleanup_scratch` deletes the worktree
holding `result.json`. Omitting the excerpt there would leave the run recorded
nowhere but a truncated tick-log line — and the recheck comment has to name
what was fixed, not just that a head moved. The excerpt is neutralised for
`<!--`, `-->`, ` ``` `, and `</details>` before rendering. The first two matter for
correctness, not tidiness: `count_review_rounds` matches the raw `REVIEW_MARKER`
substring in a comment body, so a fixer quoting a reviewer comment back at us would
otherwise add a phantom round to the iteration cap.

### Attempt-cap escalation comments

`_bound_failed_harvest` is shared by all three stages. When a harvest's GitHub side
effect keeps failing, it forces `ai:needs-human-decision` — a remote-visible
terminal transition — and now publishes
`pipeline.render_cap_escalation_comment` (`ESCALATION_MARKER`, stage, failure
count, resulting label) before the relabel. The counter is compared against
`MAX_DISPATCH_ATTEMPTS` *before* it is incremented, so the escalating harvest is
the `MAX_DISPATCH_ATTEMPTS + 1`-th consecutive failure; the comment reports that
actual count rather than the cap constant. The body states that it **supersedes**
any earlier comment from the run: a fixer that posted `recheck → ai:revised` and
then failed its relabel would otherwise leave the PR carrying a comment that
contradicts its own label.

Unlike every other harvest post, a failed cap escalation post does **not** block
the relabel — it is logged as `escalation_comment_failed` and the label lands
anyway. The cap exists because this item's remote writes keep failing; gating the
terminal label on one more of them would pin the admission slot forever, which is
the outcome the cap prevents. The label is the load-bearing signal and the comment
is the explanation.

Because that comment is published before the relabel and claims to supersede the
run's own outcome, the decision has to outlive a failed relabel. Before any
remote mutation, the deciding branch writes a typed terminal intent to
`<WT_ROOT>/.terminal/<entity>-<N>.json` (`dispatch_id`, target label, stage,
kind). Every harvester calls `_resume_terminal_escalation` before computing a
fresh decision: a pending intent for the marker's `dispatch_id` means the
outcome is already settled and announced, so the tick re-runs the original
completion (re-prepare the archive, repost — suppressed by its receipt —
converge, or skip convergence when the relabel already landed) instead of
recomputing one. Without it, a recovered relabel on the next tick lands the
run's own `ai:revised` under a comment saying `ai:needs-human-decision`
supersedes it. The intent is dispatch-bound, so a re-dispatched item harvests on
its own merits; a stale record is retired on sight. The intent and its comment
receipts are retired only after the final archive move lands — not when the
relabel does. Once the relabel lands, the intent records the phase, so the
resume distinguishes "our relabel landed; finish local custody only" from
"relabel still pending" and never re-applies the terminal label over a later
human transition. The intent write is a required gate: a transition whose
decision cannot be durably recorded is not started at all.

`_harvest_one_reviewer`'s escalate branch records one too, and it is the only
terminal transition with no comment in front of it. `decide_review_harvest`
escalates an unvalidatable verdict, and changes-requested with no blocking
finding, with `post_comment` false, so `review_already_posted` has no comment to
recognise on a retry. Its target is a hold, so an interrupted transition leaves
a completed marker under a hold this pipeline set itself, and the generic hold
branch would publish `render_held_completion_comment` over it — a notice
asserting that a workflow hold was observed while the engine was running. The
branch therefore records the intent and hands the transition to
`_complete_escalate_transition` with an empty `comment_body`, exactly as the
implementer's silent escalation does.

`_terminal_relabel_state` classifies the live label set into the four states
that decide which mutation the resume may make, and `_land_terminal_transition`
makes it — one place, so all three completions cannot drift apart. `pending`
converges; `landed` and `ambiguous` mutate nothing; `held` (the in-flight label
still present beside a workflow hold) releases the pipeline's own in-flight
label instead, because convergence refuses a hold by design and retrying it
every tick could only keep failing. The release reclaims no admission slot —
the marker the caller destroys next is what frees that — it stops the label set
from asserting a pipeline custody the hold already owns. Every release in
the module names the hold that justifies it — `required_hold_label` has no
default, so a decision that reaches `_release_in_flight_label` without that
provenance fails closed rather than removing a label on an unchecked guard — and
every release is followed by `_hold_survived_custody_move` before the caller
destroys the marker, because the release's own read-back proves only that the
hold outlived the removal. All three sites (the resultless cleanup and the
held-result publication in `_execute_reconcile_actions`, and the held terminal
transition here) therefore carry the same two gates and the same restore.

### Transient-path escalation comments

`_handle_transient_engine_result` owns the other two terminal transitions to
`ai:needs-human-decision`, and both now publish
`pipeline.render_transient_escalation_comment` (`ESCALATION_MARKER`, stage,
outcome slug, engine failure class) before their relabel, under the
`transient-escalation` receipt kind:

- **`transient-exhausted`** — the engine returned a transient failure on every
  attempt of its retry budget. The post **gates** the relabel, and a failure is
  also *bounded*: it discards the prepared archive and routes to
  `_bound_failed_harvest`, which burns an attempt now and, once the counter
  reaches the cap, lands the terminal label under the cap's own superseding
  comment. Gating without bounding would retry the post every tick forever while
  the item pinned an admission slot.
- **`attempt-state-error`** — the durable attempt counter could not be written,
  so the retry budget cannot be tracked and further retries would be unbounded.
  Its post is **best-effort**, for the same reason the cap's is: the thing that
  would bound a gated retry is exactly what failed. A miss is logged as
  `escalation_comment_failed`.

Only the exhaustion path archives evidence locally, so only its comment points at
the harvest-evidence archive.

Both paths record a typed terminal intent (`transient_exhausted`, or `escalate`
for the result-driven transition) before the relabel, for the same reason the
cap does. The decision is a pure function of an immutable `result.json` and a
counter the exhausted path never advances, so a failed *relabel* recomputes the
identical outcome — but a failed *final archive move* must still resume the
original transition rather than fall back to the generic hold branch, which
would republish the completed outcome as a held completion under the pipeline's
own terminal label. The decision-keyed receipt suppresses the duplicate comment;
the intent carries custody across the archive move.

## Tests

```bash
uv run pytest $PR_PIPELINE_HOME/skills/tick/lib --basetemp="$TMPDIR/ppltest"
uv run ruff check $PR_PIPELINE_HOME/skills/tick/lib/
uv run pyright $PR_PIPELINE_HOME/skills/tick/lib/
```

(`--basetemp` avoids a stale-temp-dir cleanup quirk in pytest's default basetemp;
the lint/type paths are explicit because this directory is intentionally outside the
repo's default lint/type surface to keep it extraction-isolated.)
