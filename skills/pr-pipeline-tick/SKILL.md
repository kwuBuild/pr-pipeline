---
name: pr-pipeline-tick
description: Run one PR-pipeline tick: harvest finished implementer, reviewer, and fixer engines, then dispatch eligible work.
---

# PR Pipeline Tick

Run the pipeline from the repo root. GitHub labels are the source of truth; each
tick runs HARVEST before DISPATCH. Usage examples and observability are in
`README.md`; debugging, invariants, and recovery are in `DEBUG.md`.

## Run Procedure

1. Before the first reviewer dispatch after install or Codex version drift:
   ```bash
   bash .claude/skills/pr-pipeline-tick/preflight.sh
   ```
2. Run the tick with Bash `dangerouslyDisableSandbox: true` (see DEBUG.md
   "Why dangerouslyDisableSandbox" for the rationale):
   ```bash
   uv run python .claude/skills/pr-pipeline-tick/lib/tick.py [--loop] [pr:N | '#N'] [--impl-engine claude|claude-ds] [--model <model>] [--review-model <model>] [--review-effort <effort>] [--permission-mode dontAsk|auto]
   ```
   `--permission-mode auto` is experimental, not promoted: classifier-gated engines; default stays `dontAsk`; trial via targeted `pr:N`/`'#N'` runs; a classifier denial fails the run.
3. With `--loop`, cadence is an upper bound: engine completion or `touch .claude/worktrees/.tick-state/wake-request` starts the next tick early. Nudges change only timing, never what a tick concludes. See README "Edge-triggered wake-up".
4. Use `pr:N` or `'#N'` only for a targeted harvest/dispatch. For `#N`, implementation stays scoped to issue `N`; zero or one open closing PR scopes the reviewer and fixer. `pr:N` stays direct PR scope. An ambiguous or malformed closing-PR response, or GitHub read failure, stops before scratch/worktree or GitHub workflow mutation; the audit log and tick lock are excepted. Targeting does not bypass reconciliation, admission, trust, or label-safety gates.

## Outcome Check

- Tick logs: `.claude/worktrees/.tick-log/tick-*.jsonl`
- Fast check:
  ```bash
  grep -E '"action": "(startup_probe_failed|skill_unresolved|lock_held|tick_crash|retained_scratch|corrupt_marker_escalate|archive_failed|kill_failed|worktree_prune_failed|stale_label_retained|terminal_intent_failed|terminal_intent_unresolved|notify_terminal_label|engine_stalled|loop_quiescent_stop|tick_complete)"' .claude/worktrees/.tick-log/tick-*.jsonl
  ```
- For live queue state, run `/pr-pipeline-status` or
  `bash .claude/skills/pr-pipeline-status/status.sh`.

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Full tick completed: HARVEST then DISPATCH |
| 1 | Reconcile failed or target parse failed; dispatch gated |
| 2 | Reconcile OK, authoritative in-flight label read failed; dispatch gated |
| 3 | Startup probe failed; required `gh`, `git`, or `codex` missing |
| 4 | Unhandled exception; traceback written to tick log when possible |
| 5 | Tick mutex already held by another invocation |

## Stop or Escalate

- Stop and read DEBUG.md "Startup failure diagnostics" and "Debugging pointers"
  on code `3`, code `4`, an empty/missing tick log, `startup_probe_failed`, or
  `tick_crash`.
- Wait and retry on code `5`; another tick owns `.claude/worktrees/.tick.lock`.
- Escalate terminal labels: `ai:needs-human-decision` or `ai:max-iterations`.
- A markerless in-flight item is never the tick's to touch — it is a manual pickup, another machine's live dispatch, or an engine that died silently. The tick leaves all three alone, and none of them consumes an admission slot: the cap counts this machine's dispatch markers, never a shared `ai:` label. The dashboard cannot tell them apart either: it reads shared GitHub labels plus only *this* machine's markers, so an absent engine row is not custody proof. Clearing one is a manual action that needs out-of-band confirmation from the dispatch host or the person who picked it up — see README.md "Markerless in-flight items".
- A dispatch-bound failed reviewer completion is archived and routed to `ai:needs-human-decision`; it must not be redispatched. On archive failure, keep the in-flight label and scratch. On relabel failure, keep the archive and scratch under pipeline custody for retry.
- On `archive_failed`, leave the scratch directory in place and make no further
  label transition. If a terminal relabel already succeeded, keep that terminal
  state. Fix the storage/permission failure, then rerun the tick and confirm the
  corresponding `.harvest-evidence/` bundle before any manual cleanup.
- Do not manually delete worktrees, markers, or labels during routine ticks;
  treat those as separate incident-recovery actions.

## Running Under /loop

Invoke this skill once when setting up the loop. The recurring loop prompt must
run the compact command contract below; it must not reinvoke the slash command or
reload this skill text each iteration.

Each iteration is a one-shot tick scheduled from the emitted `tick-cadence:` line, with no in-process sleep to interrupt: wake nudges do not apply here. Follow `next_wake` exactly. Detection that only *shortens* a sleep (engine-completion and wake-request nudges) is inert in this mode; detection that reports to the operator is not — the tick runs its own stall scan, so `engine_stalled` reaches the log here exactly as it does under `--loop`, and reporting it is this prompt's job.

Paste this prompt into /loop:

```text
Run one Sworn PR-pipeline iteration from /Users/keithwu/Documents/sworn. Do not invoke /pr-pipeline-tick. Run Bash with dangerouslyDisableSandbox: true:
uv run python .claude/skills/pr-pipeline-tick/lib/tick.py

Then inspect the newest .claude/worktrees/.tick-log/tick-*.jsonl with grep for startup_probe_failed, skill_unresolved, lock_held, tick_crash, corrupt_marker_escalate, workflow_hold_retained, human_hold_retained, hung_engine_killed_under_hold, label_release_failed, held_result_comment_failed, held_result_published, kill_failed, worktree_prune_failed, notify_terminal_label, engine_stalled, loop_quiescent_stop, and tick_summary. Report the exit code, harvested or dispatched issue/PR numbers, label changes, terminal labels, workflow_hold_retained/label_release_failed/held_result_comment_failed/held_result_published/kill_failed/worktree_prune_failed events, engine_stalled events, and any stop/escalation condition. A GitHub `ai:needs-human-decision` or `ai:blocked` label holds the *workflow*: it consumes admission only while this machine still holds the item's dispatch marker (a shared label alone never occupies a slot), and the tick must not relabel the item, advance its stage, or archive live scratch. It does not hold the *process* — an overdue engine with proven identity is still terminated so it stops spending, which mutates no label and destroys no evidence. Only proven process-group death releases the scratch and the pipeline's own in-flight label; the hold label is never mutated. A result that finished under a hold is not harvested: the tick prepares evidence, publishes its validated outcome, releases only the pipeline label, then finalizes the archive. `held_result_published` marks that completed sequence; `held_result_comment_failed` means publication failed and all source custody was retained for retry. Neither kill_failed nor worktree_prune_failed alone stops the loop: kill_failed retains engine custody while the watcher/timer observes it, and worktree_prune_failed retains archive custody but may require a later dispatch or operator retry for metadata garbage collection. engine_stalled does not stop the loop either — it is advisory detection, de-duplicated across uninterrupted polls but retried if the process exits before its post-report checkpoint commits, and the engine keeps its full deadline; report it with its progress_age_seconds and seconds_to_deadline so the developer can decide. Stop and notify the developer if `corrupt_marker_escalate` appears. Stop the loop if the command exits 3 or 4, the log is empty or missing, a terminal human-decision/max-iterations label appears, or the same lock_held state repeats.
Parse the final `tick-cadence:` line. If `stop=true`, do not schedule another iteration and notify the developer with its full reason, including any `awaiting human decision:` suffix. Otherwise call `ScheduleWakeup` once using `next_wake` seconds; never substitute a fixed interval.
```
