---
name: tick
description: Run one PR-pipeline tick: harvest finished implementer, reviewer, and fixer engines, then dispatch eligible work.
---

# PR Pipeline Tick

Run from the **target repo** root; `${CLAUDE_PLUGIN_ROOT}` is this pipeline's install path, already resolved below (code from the install, state from the target). Running from a development checkout instead, substitute the checkout path — see README "Where the pipeline lives".

Labels are the source of truth; each tick runs HARVEST before DISPATCH. Usage and observability: `README.md`; debugging, invariants, recovery: `DEBUG.md`. Before the first tick on each target, run `/pr-pipeline:setup` for it: setup confirms the operator login, provisions the label vocabulary, probes the engine route, and writes the repository's `[repos."owner/name"]` section.

## Run Procedure

1. Before the first reviewer dispatch after install or Codex version drift:
   ```bash
   bash ${CLAUDE_PLUGIN_ROOT}/skills/tick/preflight.sh
   ```
2. Run the tick with Bash `dangerouslyDisableSandbox: true` (see DEBUG.md
   "Why dangerouslyDisableSandbox" for the rationale):
   ```bash
   bash ${CLAUDE_PLUGIN_ROOT}/skills/tick/tick.sh [--loop] [pr:N | '#N' | --only '#N,pr:M'] [--impl-engine claude|claude-ds] [--impl-model <model>] [--fix-engine claude|claude-ds] [--fix-model <model>] [--impl-effort <level>] [--fix-effort <level>] [--review-model <model>] [--review-effort <effort>] [--permission-mode dontAsk|auto]
   ```
   Pass route flags only for a one-off run. Each repository's route, stage deadlines, stall limits, round limit and cadence come from the operator config (README "Deadlines, stall limits, routing and cadence"); setup records the probed implementer route there. A flag outranks the config for that run, and a run whose implementer route differs from the probed one logs `startup_route_unprobed` and still runs.
   Claude implementers/fixers default to `auto`: calls needing approval beyond explicit grants reach the classifier; denial can fail the run. `permission_mode` resolves packaged floor → operator `[defaults]` → `[repos."owner/name"]` → CLI. `/pr-pipeline:setup` records `dontAsk` for a route that fails its probe; tick performs no automatic route fallback. Under `auto`, the allowlist is not a ceiling: use operator-owned deny rules for forbidden operations. The Codex reviewer retains its separate read-only policy; see [permission modes](README.md#engine-permission-modes).
3. For unattended runs, the default is the native loop: start `bash ${CLAUDE_PLUGIN_ROOT}/skills/tick/tick.sh --loop` with the same target and flags the operator chose (a targeted first tick stays targeted; `--permission-mode`, `--impl-model`, and engine flags are not sticky) as one Bash call with `run_in_background: true` and `dangerouslyDisableSandbox: true`. It makes no model calls between ticks; do not poll it. Its cadence is an upper bound: engine completion or `touch .claude/worktrees/.tick-state/wake-request` starts the next tick early, and nudges change only timing, never what a tick concludes (README "Edge-triggered wake-up"). When the task notification reports its exit, run the Outcome Check and report as "Stop or Escalate" directs. Pair the loop with the milestone watch. Before starting the loop, run `bash ${CLAUDE_PLUGIN_ROOT}/skills/status/events.sh --mark` from the target repo root; it prints the path of a new milestone cursor. Right after starting the loop, call the Monitor tool (`timeout_ms` 1800000) from the target repo root on `bash ${CLAUDE_PLUGIN_ROOT}/skills/status/events.sh --follow --milestones --cursor <path>`. That reader, not the model, filters the tick logs: it prints one line per `launch`, `terminal`, `attention`, `stalled`, `stopped`, `startup_failure`, or `crash` event written after the cursor, advances the cursor, and exits after a loop stop, startup failure, or crash, so milestones written before the watch starts or during a re-arm are still reported. Heartbeats and tick summaries never wake the session. For each line, send one short PushNotification: the item, plus stage and model, label (with the escalation reason for `ai:needs-human-decision`), attention reason, stall age and time to deadline, or stop reason (a `--only list settled` stop means the run is done). For `terminal`, first run `events.sh --json` for that item and add the summed reported cost and duration of its `result` events, or say they are unknown. Re-arm the same command when the watch expires while the loop runs. When the loop's exit notification arrives, stop a still-running watch with TaskStop, run the same command once without `--follow` to report what it had not printed, and never re-arm it. Cost: no model tokens while quiet, one turn per milestone (about 4 to 6 for a single issue), and one short re-arm turn per 30-minute expiry. If Monitor or PushNotification is unavailable, tell the operator that the run falls back to the exit notification alone (README "Milestone watch").
4. Use `pr:N` or `'#N'` only for a targeted harvest/dispatch. For `#N`, implementation stays scoped to issue `N`; zero or one open closing PR scopes the reviewer and fixer. `pr:N` stays direct PR scope. An ambiguous or malformed closing-PR response, or GitHub read failure, stops before scratch/worktree or GitHub workflow mutation; the audit log, tick lock, and target quiet counter are excepted. Targeting does not bypass reconciliation, admission, trust, or label-safety gates. Targeted one-shot and native loops report `stop=true` immediately for terminal or closed scopes; unfinished targets keep waiting. Each requested target has its own quiet counter; the full queue retains its heartbeat stop. Report the full stop reason. See [target completion and cadence](README.md#target-completion-and-cadence) for handoff, holds, and unknown-state rules. Only one positional target is accepted; a second one exits `1`. Use `--only '#N,pr:M'` to launch only listed items, in list order within each stage, without relabeling anything. It is mutually exclusive with a positional target. HARVEST, stale reconciliation, and occupancy stay global, so unlisted engines are still harvested and free their slots. A listed issue also admits its one open closing PR. If the list cannot be expanded, the tick still harvests, starts no engine, logs `only_scope_unreadable`, exits `2`, and retries next tick. A list loop stops only on a tick with no local engine in flight. It stops on the first such tick where every listed item is settled (`ai:ready-for-human`, `ai:needs-human-decision`, `ai:max-iterations`, `ai:blocked`, closed, or an issue handed off to its open closing PR); the reason says `--only list settled`. Otherwise it stops after its own quiet streak shows no dispatchable listed item; the reason says `--only list exhausted`. `--loop` logs both as `loop_only_stop`. See [dispatch list](README.md#dispatch-list---only).

## Outcome Check

- Tick logs: `.claude/worktrees/.tick-log/tick-*.jsonl`
- Fast check:
  ```bash
  grep -E '"action": "(startup_probe_failed|startup_repo_unconfigured|startup_labels_invalid|startup_route_unprobed|skill_unresolved|lock_held|tick_crash|retained_scratch|corrupt_marker_escalate|archive_failed|kill_failed|terminal_label_failed|label_convergence_unverified|worktree_prune_failed|stale_label_retained|terminal_intent_failed|terminal_intent_unresolved|notify_terminal_label|engine_stalled|loop_quiescent_stop|loop_target_stop|loop_only_stop|only_scope_unreadable|tick_complete)"' .claude/worktrees/.tick-log/tick-*.jsonl
  ```
- For live queue state, run `/pr-pipeline:status` or `bash ${CLAUDE_PLUGIN_ROOT}/skills/status/status.sh`.

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Full tick completed: HARVEST then DISPATCH |
| 1 | Reconcile failed, target parse failed, or the CLI rejected the target/`--only` input; dispatch gated |
| 2 | Reconcile OK; dispatch gated because the authoritative in-flight label read failed, the `--only` expansion could not be read, or an installed doc a stage requires is unreadable (`skill_unresolved` with `pr: null`; repair the install, no relabel needed) |
| 3 | Startup rejected: missing tools/Python >= 3.11, invalid target/config, or missing/unreadable label vocabulary |
| 4 | Unhandled exception or loop record startup failure; diagnostics in stderr or the tick log; `--loop` returns without another tick |
| 5 | Tick mutex already held by another invocation |
| 6 | Target has no `[repos."owner/name"]` section: setup never ran for it; `--loop` logs `startup_repo_unconfigured` and returns |

## Stop or Escalate

- Stop and read DEBUG.md "Startup failure diagnostics" and "Debugging pointers"
  on code `3`, code `4`, an empty/missing tick log, `startup_probe_failed`, or
  `tick_crash`.
- Wait and retry on code `5`; another tick owns `.claude/worktrees/.tick.lock`. On code `6`, ask the operator whether to run `/pr-pipeline:setup` for the named repository now; never write its section yourself. Rerun the tick after setup.
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

Optional: use `/loop` only when the operator wants a report after every tick; each iteration costs a model turn, wake nudges cannot start a tick early, and the harness clamps each wake to 60–3600 s, so shorter `next_wake` values run late. The prompt runs the whole queue with configured defaults: append the same target and flags to its `tick.sh` line before pasting.
Invoke this skill once when setting up the loop. The recurring loop prompt must run the compact command contract below; it must not reinvoke the slash command or reload this skill text each iteration.

Each iteration is a one-shot tick scheduled from the emitted `tick-cadence:` line, with no in-process sleep to interrupt: wake nudges do not apply here. Follow `next_wake` exactly. Detection that only *shortens* a sleep (engine-completion and wake-request nudges) is inert in this mode; detection that reports to the operator is not — the tick runs its own stall scan, so `engine_stalled` reaches the log here exactly as it does under `--loop`, and reporting it is this prompt's job.

Paste this prompt into /loop:

```text
Run one PR-pipeline iteration with <TARGET_REPO_ROOT> as the working directory (replace it with the absolute path of the repository this pipeline operates on; the pipeline's own install path is already resolved in the command below). Do not invoke /pr-pipeline:tick. Run Bash with dangerouslyDisableSandbox: true:
bash ${CLAUDE_PLUGIN_ROOT}/skills/tick/tick.sh

Then inspect the newest .claude/worktrees/.tick-log/tick-*.jsonl with grep for startup_probe_failed, skill_unresolved, lock_held, tick_crash, corrupt_marker_escalate, workflow_hold_retained, human_hold_retained, hung_engine_killed_under_hold, label_release_failed, held_result_comment_failed, held_result_published, kill_failed, terminal_label_failed, label_convergence_unverified, worktree_prune_failed, notify_terminal_label, engine_stalled, loop_quiescent_stop, loop_target_stop, loop_only_stop, only_scope_unreadable, and tick_summary. Report the exit code, harvested or dispatched issue/PR numbers, label changes, terminal labels, workflow_hold_retained/label_release_failed/held_result_comment_failed/held_result_published/kill_failed/worktree_prune_failed events, engine_stalled events, and any stop/escalation condition. A GitHub `ai:needs-human-decision` or `ai:blocked` label holds the *workflow*: it consumes admission only while this machine still holds the item's dispatch marker (a shared label alone never occupies a slot), and the tick must not relabel the item, advance its stage, or archive live scratch. It does not hold the *process* — an overdue engine with proven identity is still terminated so it stops spending, which mutates no label and destroys no evidence. Only proven process-group death releases the scratch and the pipeline's own in-flight label; the hold label is never mutated. A result that finished under a hold is not harvested: the tick prepares evidence, publishes its validated outcome, releases only the pipeline label, then finalizes the archive. `held_result_published` marks that completed sequence; `held_result_comment_failed` means publication failed and all source custody was retained for retry. Neither kill_failed nor worktree_prune_failed alone stops the loop: kill_failed retains engine custody while the watcher/timer observes it, and worktree_prune_failed retains archive custody but may require a later dispatch or operator retry for metadata garbage collection. engine_stalled does not stop the loop either — it is advisory detection, de-duplicated across uninterrupted polls but retried if the process exits before its post-report checkpoint commits, and the engine keeps its full deadline; report it with its progress_age_seconds and seconds_to_deadline so the developer can decide. Stop and notify the developer if `corrupt_marker_escalate` appears. Stop the loop if the command exits 3, 4, or 6 (6: the target was never set up; report it and do not run setup unattended), the log is empty or missing, a terminal human-decision/max-iterations label appears, or the same lock_held state repeats.
Parse the final `tick-cadence:` line. If `stop=true`, do not schedule another iteration and notify the developer with its full reason, including any `awaiting human decision:` suffix. Otherwise call `ScheduleWakeup` once using `next_wake` seconds; never substitute a fixed interval.
```
