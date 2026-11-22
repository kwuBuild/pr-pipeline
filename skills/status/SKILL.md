---
name: status
description: Show a read-only PR-pipeline dashboard — in-flight review/fix engines (PID, stage, time-to-deadline) plus every open PR/issue grouped by ai: pipeline label. Use to see pipeline state at a glance without touching it. Safe to run while a tick/loop is running.
---

# PR Pipeline Status

Read-only dashboard for the PR pipeline driven by the `pr-pipeline:tick` skill. It
makes **no writes** — it never edits labels, dispatches engines, or touches markers —
so it is safe to run at any time, including in a `watch` pane while a tick or
`/loop /pr-pipeline:tick` is running.

It reads only the state sources the pipeline externalizes:

1. **GitHub labels** — the pipeline stage (queue / reviewing / fixing / done).
2. **Worktree markers** (`.claude/worktrees/pr-*/dispatch.json`) — live engine state
   (PID liveness, deadline, whether a result file has landed) and the sibling
   `heartbeat` file — when the engine last produced output. Markers are
   machine-local: this is every engine *this* machine launched, and no engine any
   other machine launched. An in-flight label with no engine row is therefore not
   custody proof — it reads identically for another machine's healthy dispatch,
   for a manual `ai:implementing` pickup, and for a dead engine. Never relabel
   from that reading; see the `pr-pipeline:tick` README, "Markerless in-flight
   items".
3. **Tick logs** (`.claude/worktrees/.tick-log/tick-*.jsonl`) — the most recent
   tick's durable decision trail. The **DISPATCH SCOPE** banner at the top reads
   them to name the newest tick's scope (full queue, target, or `--only` list
   with its expansion; a list or target that failed before resolving shows as
   requested but not resolved, and a log with no scope record shows
   `scope unknown`) and whether that evidence is `active`, `ended`, or
   `STALE`. This is historical evidence; `STALE` means no tick ran after
   the advertised next wake. See the `pr-pipeline:tick` README,
   "DISPATCH SCOPE".
4. **Loop record** (`.claude/worktrees/.tick-state/loop.json`) — the native loop's
   local identity. **LOOP PROCESS** uses the shared read-only classifier to show
   `running`, `not_running`, `unknown`, or `unavailable`, with explicit record
   presence and source health. A missing record does not prove no loop exists;
   `not_running` only refutes the recorded identity and says nothing about work
   completion. See the [record and classifier contract](LOOP-STATUS.md).

## Run

Run with the **target repository** as the working directory: the dashboard reads
pipeline *state* from there, while `${CLAUDE_PLUGIN_ROOT}` — already resolved to
the absolute path of this pipeline's install — is where its *code* lives.

```bash
bash ${CLAUDE_PLUGIN_ROOT}/skills/status/status.sh
```

The engine row uses `kill -0 "$pid"`, so — like the tick — run it in the **same trust
domain that launched the engines (sandbox off)**; a sandbox that blocks process
introspection can make an engine row read as dead. The separate loop classifier
keeps unreadable identity as `unknown` unless shared existence evidence proves
absence.

Continuous view in a separate terminal pane. These run in a shell that this
skill's text never reaches, so paste the install path the command above resolved
to rather than either variable — nothing outside skill content substitutes
`${CLAUDE_PLUGIN_ROOT}`, and an unset variable silently makes the path
root-relative:

```bash
# Primary (Linux / watch installed):
watch -n 5 'bash /absolute/path/to/install/skills/status/status.sh'

# Fallback (macOS / no watch):
while true; do clear; bash /absolute/path/to/install/skills/status/status.sh; sleep 5; done
```

The repo comes from the main checkout's Git remote through the same
`skills/tick/resolve-target.sh` helper the tick uses. The dashboard reads no
configuration and resolves the target before looking for Python.
Linked-worktree invocations share the main checkout's target and marker space.
`PIPELINE_REPO=<owner/repo>` selects exactly one matching local remote; multiple
remotes require this selection. A missing, ambiguous, or unsafe remote, or a
different push destination, exits 2 before any GitHub request. The dashboard
does not enforce `[scan] repos` because it reads no TOML; the tick enforces that
operator scope before doing any work.

The GitHub sections read every page of open issues and PRs, grouped by label.
**PARKED ITEMS** separately lists items with one or more unknown `ai:` labels
and no known workflow label. No dispatch query selects these items; deciding
whether to change their labels belongs to the repository owner. Items also
carrying a known label stay in the ordinary label sections.

The known vocabulary comes from the installed `pipeline.py` constants through
`labels.py vocabulary`. When Python cannot load it, raw label rows remain
visible and parked-item classification says unavailable. A failed GitHub read
also says unavailable, rather than reporting an empty pipeline. The dashboard
never provisions, renames, or deletes labels.

## Local event feed

For a retained history or a continuously updating terminal feed, run the
standalone launcher from the target repository:

```bash
~/.claude/pr-pipeline/bin/pr-pipeline events --follow
```

This reader makes no GitHub or model calls and changes no pipeline state. Do
not keep an AI session polling or narrating the full feed automatically. The
one exception is the tick skill's
[milestone watch](../tick/README.md#milestone-watch): it runs
`events --follow --milestones --cursor <path>`, so the session wakes only for
a milestone. `--mark` writes that cursor file in the system temporary
directory, outside pipeline state.
Use `--json` for scripts and `'#N'` or `pr:N` to filter. The [event feed contract](EVENTS.md)
owns the schema, retention limits, and follow behavior.

## Progress column

Implement/fix rows carry a progress column next to the result column:

| Value | Meaning |
| --- | --- |
| `quiet-Nm` | N minutes since the engine's last output. Before the first output event N counts from dispatch instead. Not by itself a health verdict: this is also what a stall *exclusion* renders (a reused PID, an already-overdue engine), so a long `quiet-Nm` means "no stall claim", not "working normally". |
| `STALLED-Nm` | Live engine, no output for N minutes, past the stall threshold recorded in its marker at dispatch (the operator's `engine_stall_seconds`, 30 minutes shipped). |
| `-` | No progress contract: the reviewer stage (its wrapper discards engine output), or a marker with no usable clock. |
| `?` | No verdict for this row. Either the report could not be produced at all (the `pr-pipeline:tick` lib is unreachable), or it was produced without a line for this marker — a marker created between the report call and the render, or one Python skipped as malformed while bash's own parse succeeded. Not a claim either way. |

The verdict is not derived here. This script shells out to
`tick.py engine-progress`, the same code the tick and its stall watcher run, so the
column and the tick log cannot disagree: `STALLED` requires both silence past
the threshold **and** the identity-verified liveness check harvest trusts. A
reused PID or an already-overdue engine passes `kill -0` but is not a stall, and
`quiet-Nm` is what those render. That identity check *refutes* liveness rather
than confirming it — where the process table cannot be read the marker still
counts as live, so a `STALLED` row can rest on liveness that was never
disproved. The call takes no pipeline lock and makes no pipeline state
transition, so it stays safe in a `watch` pane against a live tick.

`STALLED` is **advisory**: nothing is killed, relabelled, or requeued because of
it, and the engine keeps its full deadline. It is a prompt to look at the engine's
`stderr` in its worktree, not a state transition. The same condition appears as an
`engine_stalled` entry in the tick log — see the `pr-pipeline:tick` DEBUG.md.
