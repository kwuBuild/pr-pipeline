# PR Pipeline — End-to-End Usage & Monitoring Guide

Status: COMPLETE (HARVEST + DISPATCH fully implemented — 3 stages each)
Created: 2026-06-28
Updated: 2026-07-01 (dispatch phases implemented, operator guide updated)

## Quick-start checklist

**Trust domain:** The tick invocation must use `dangerouslyDisableSandbox: true`.
The Bash tool's command-pattern allowlist and the OS-level sandbox (macOS
`sandbox-exec`) are separate concerns — subprocesses bypass the allowlist
(spawned from Python, not the Bash tool) but inherit the OS-level sandbox.
Under the default sandbox: `ps` is denied so process identity is permanently
unverifiable (Invariant 4 blocks all reaping); cross-boundary kill signals
are denied so timeout enforcement cannot terminate anything; and dispatched
engines inherit the sandbox, losing network access to non-allowlisted LLM
endpoints and `~/.codex` writes, so they stall silently after the label flip.
Scope: the tick process and its children, not the entire session.

The orchestrator was extracted from 25 bash code blocks into a single Python process
(`lib/tick.py`). The skill's `SKILL.md` is now a lean operator SOP. See
`docs/planning/designs/pr-pipeline-tick-python-extraction.md` for the extraction
design and post-implementation review findings.

**Where the pipeline lives.** Pipeline *code* is resolved from the install, not
from the repository being operated on: `$PR_PIPELINE_HOME` is the absolute path
of your `pr-pipeline` checkout (or plugin install). Pipeline *state*
(`.claude/worktrees/`) stays relative to the target repo, which is why every
command below runs with the target repo as the working directory.

```bash
export PR_PIPELINE_HOME=/absolute/path/to/pr-pipeline
```

The examples are unquoted for readability; quote `"$PR_PIPELINE_HOME/..."`
yourself if your install path contains spaces. Under `watch '...'` the variable
must be exported, not just set.

```bash
# 1. One-time: verify the Codex binary and schema (required before first reviewer dispatch)
bash $PR_PIPELINE_HOME/skills/pr-pipeline-tick/preflight.sh

# 2. Open the dashboard in a separate pane
# Primary (Linux / watch installed):
watch -n 5 'bash $PR_PIPELINE_HOME/skills/pr-pipeline-status/status.sh'
# Fallback (macOS / no watch):
while true; do clear; bash $PR_PIPELINE_HOME/skills/pr-pipeline-status/status.sh; sleep 5; done

# 3. Run one tick (full HARVEST + DISPATCH loop)
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py

# 4. Run a targeted tick (single PR or issue)
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py pr:468
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py '#439'

# 5. Verify the dispatch landed (use find to avoid bare-glob errors in zsh)
find .claude/worktrees -maxdepth 2 -name dispatch.json -path '*/issue-*' \
  -exec jq '{engine, stage, pid}' {} \;

# 6. Loop in-process (autonomous polling with mutex + quiescence stop)
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py --loop
```

## Document split

Three files, each with a clear job:

- **`SKILL.md`** — Injected operator SOP (≲80 lines). Run procedure, outcome
  check, exit codes, stop conditions, `/loop` prompt.
- **`README.md`** (this file) — Usage guide: invocation modes, observability
  surfaces, worked examples, design principles.
- **`DEBUG.md`** — Debugging and recovery reference: operational invariants,
  sandbox rationale, startup diagnostics, reviewer posture, constants, and
  debugging pointers. Read it when a tick fails or the SOP says "read DEBUG.md."

Update all three together when an operator-facing command or stop condition
changes.

### Stage and engine state

The current implementation runs the full HARVEST + DISPATCH loop. Reviews run via
Codex (`engine_wrapper.py`); implementers and fixers run via Claude (`claude -p`).
All three stages support targeted mode (`pr:N` / `'#N'`). The orchestrator also
supports in-process `--loop` mode with a tick mutex, self-paced cadence,
edge-triggered wake nudges, quiescence stop, and terminal-label notifications.

## Invocation

The orchestrator is a single Python process: `uv run python lib/tick.py`. The
`/pr-pipeline-tick` skill now loads a lean SOP that tells the operator when to
run `preflight.sh`, how to invoke the tick, and where to inspect outcomes.

### Basic

```bash
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py
```

Runs one full tick: HARVEST all three stages (review → implement → fix) with full
side-effect processing (PR creation, comment posting, label transitions), then
DISPATCH all three stages (reviewer → implementer → fixer). Exit code is 0 on
success, 1 when reconciliation failed and dispatch was skipped.

### Targeted (single issue/PR)

```bash
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py pr:468   # PR #468 only
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py '#439'   # issue #439 only
```

`pr:N` stays direct PR scope for reviewer and fixer work; it does not switch to
same-number issue scope. For `#N`, implementation stays scoped to issue `N`, while
zero or one open closing PR scopes the reviewer and fixer. With no open closing PR,
only the issue implementation scope is active. An ambiguous or malformed
closing-PR response, or GitHub read failure, stops before scratch/worktree or GitHub workflow mutation; the audit log and tick lock are excepted. Invalid target syntax has the same workflow-mutation boundary and exits `1`.

HARVEST, stale-label reconciliation, and DISPATCH use those resolved stage scopes.
Safety gates (`reconcile_failed`, `MAX_IN_FLIGHT` admission cap) still apply
globally, and targeting never changes labels or scratch for unrelated work.

Cadence follows the same scope. Under a target, the actionable-queue probe reads
only the resolved stage scopes instead of the global queues, so `quiescent`
describes the targeted work and `--loop pr:N` stops once `N` is terminal or
non-dispatchable — even while unrelated PRs still carry `ai:changes-requested`.
An item counts as actionable on exactly dispatch's own terms, through the same
`pipeline.resolve_pickup_eligibility` the dispatch gate calls (asked with no
stage, so cadence stays awake for work *any* stage can take): open, free of
both a human hold and an in-flight label, and carrying exactly one pickup label
across all stages for its entity. An item mid-transition (pickup label plus
in-flight label), and an ambiguously labelled one (`ai:implemented` +
`ai:revised`, or `ai:implemented` + `ai:changes-requested`), are both work no
tick can take, so counting either would hold the loop short of its quiescent
stop forever; an engine this machine owns keeps cadence warm through its own
marker instead. Unknown
state still stays warm — a failed scope resolution or item read reads as
unknown, never as quiescence. In-flight count and the `MAX_IN_FLIGHT` cap stay
global, so a targeted loop never mistakes a saturated pipeline for a quiet one.

### Looped (autonomous polling)

```bash
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py --loop
```

Runs ticks inside one Python process. `--loop` keeps the same HARVEST →
DISPATCH ordering as one-shot mode, but self-paces between ticks and exits when
the pipeline has stayed quiescent for the configured heartbeat count.

Every tick invocation, looped or one-shot, takes a non-blocking mutex at
`.claude/worktrees/.tick.lock`. If another tick is running, the new invocation
exits immediately with code `5`, writes `lock_held` to the tick log when
possible, and does no harvest or dispatch work.

| State at end of tick | Next wake |
|---|---|
| ≥1 engine in-flight, deadline > 0 | `min(nearest deadline, 270s)` cache-warm poll |
| ≥1 engine in-flight, deadline lapsed mid-scan | 1s — immediate retry to harvest/escalate |
| No engine in-flight, queue non-empty | ~270s cache-warm poll |
| At `MAX_IN_FLIGHT`, saturated | ~270s cache-warm poll |
| Quiescent (all in-scope items terminal) | ~1800s heartbeat |
| Quiescent for N consecutive heartbeats | stop + notify |

Only a deadline that still requires cadence action paces the loop. Both cadence
surfaces share one scan, and it counts a marker only when the identity-verified
classification harvest and admission trust says `live` or `launching`. A reused
PID, a dead engine, or any `hung` engine contributes no deadline — whether a human
now owns recovery or a failed termination left the marker under pipeline custody
for another attempt. Such a marker still occupies a concurrency slot and keeps
the loop warm, but its deadline is a fixed past timestamp that never becomes less
overdue. In the failed-termination case a 1s retry cannot accelerate an `EPERM` or
an already-delivered `SIGKILL`; the wake watcher nudges within 5s after PID and
group death. The overdue row above is therefore a narrow mid-scan race, not a
steady-state retry contract.

In `--loop` mode these intervals are an **upper bound**: a wake nudge can start
the next tick sooner (see [Edge-triggered wake-up](#edge-triggered-wake-up-in-loop-mode)).
The one-shot `tick-cadence:` line and the harness `/loop` prompt that consumes it
are unchanged — that path has no external interrupt and stays purely timer-paced.
The two paths do share the persisted quiescent streak, so `--loop` marks a
nudge-initiated tick as not-a-heartbeat and the counter keeps measuring quiet
time for whichever path reads it next.

Both paths derive the quiescent reason from the same helper, so a stop caused by
a human-held engine names it identically: `--loop` in its notification, the
one-shot path in the `tick-cadence:` reason the harness reports (see
[Held engines](#held-engines)).

SIGINT/SIGTERM request a clean stop: the current tick finishes, sleep is
interrupted, and the lock is released.

### Edge-triggered wake-up (in `--loop` mode)

Ticks are level-triggered: every tick re-reads labels and markers in full. That
makes an early wake safe by construction — a nudge carries no information, and a
spurious or duplicate one costs at most one extra tick. So `--loop` waits on a
wake channel with the cadence interval as its timeout, and the watcher thread
(`_wake_watcher_loop`) is the only thing that ends that wait early *into another
tick* — SIGINT/SIGTERM also end it, but to stop the loop rather than to nudge it.
Every nudge source below is one of that thread's polls; nothing outside it turns
a sleep into an early tick:

1. **Engine completion.** A watcher thread polls every 5s for evidence that a
   dispatched engine finished. The artifact check is reviewer-only: a
   `completion.json` or `review.json` has landed, which the reviewer wrapper
   publishes by atomic rename after the engine exits. Implementers and fixers
   run wrapperless and publish neither, and their engine holds a `Write` tool
   with the worktree as its cwd — so for them one of those files is untrusted
   input, not evidence, and is not read. The liveness check runs for every
   stage: both the marker's recorded PID and process group are no longer
   alive. Wrapper death alone is not
   completion: a paid child can continue in the inherited process group, so
   watcher, harvest, reconciliation, and redispatch retain custody while that
   group lives. If its deadline expires after wrapper identity is lost, the
   item escalates to `ai:needs-human-decision`, but its marker and scratch stay
   in custody and the item continues to occupy a `MAX_IN_FLIGHT` slot until the
   group dies. That escalation is also the point the engine becomes *held*
   (see [Held engines](#held-engines)), so it stops holding loop cadence
   awake. Only validated positive
   identifiers are probed — `os.kill` on a zero or negative PID targets a
   process *group*, which is why `_classify_marker` refuses those too. A
   non-empty `result.json` is
   deliberately *not* evidence: it is a stdout redirect created empty at spawn
   and written in place, so a read can catch a partial write and it never proves
   the engine exited. Each dispatch identity nudges at most once — spending
   that one nudge before harvest could act on it would leave real completion
   waiting for the next scheduled poll.
2. **Wake-request file.** Anything that creates
   `.claude/worktrees/.tick-state/wake-request` nudges the loop; the watcher
   consumes it by unlink, so repeated touches coalesce into one early tick:

   ```bash
   touch .claude/worktrees/.tick-state/wake-request
   ```

   This is the generic nudge interface for external transports (an operator, a
   webhook forwarder) — none of them need to know anything about tick internals.
3. **Engine stall.** The same watcher poll checks whether a live implementer or
   fixer has stopped *producing*. Its stream wrapper bumps the mtime of a
   `heartbeat` file beside `result.json` as engine output events arrive; when
   that mtime has not moved for 30 minutes the loop wakes and the operator gets
   an `engine_stalled` tick-log entry and a notification. See
   [Engine progress heartbeats](DEBUG.md#engine-progress-heartbeats-engine_stalled).

   Unlike the two sources above, this one is **not** `--loop`-only. Every tick
   runs the same scan at the start of its body, so one-shot ticks report the
   stall too — they simply have no sleep for the nudge half to shorten. Both
   callers share the episode memory in
   `.claude/worktrees/.tick-state/stall-episodes.json`, which is what keeps a
   single wedge from being reported on every poll. A report attempt is
   checkpointed before the scan advances; if the process exits before that
   post-report checkpoint commits, the next scan retries the attempt rather
   than risking a silent stall.

   This is **detection only** — no kill, no relabel, no requeue, no deadline
   change. A stalled engine keeps its full 60-minute timeout; the signal exists
   so the operator learns about a wedge tens of minutes before that timeout
   fires, instead of watching a healthy-looking row burn its whole deadline.

Two properties keep this from becoming a control-plane risk:

- **The watcher holds no custody.** It reads no results, makes no state
  transition, and never kills or cleans anything. Harvest remains the sole
  completion authority. If the watcher fails it logs `wake_watcher_failed` once
  and exits, and the loop continues on its unmodified timer schedule.
- **Nudges cannot accelerate the quiescent stop.** Only a tick that followed a
  wait running its full scheduled interval counts toward the
  consecutive-quiescent heartbeat limit, so "stop after N quiescent heartbeats"
  keeps meaning roughly N × 1800s of quiet that cannot be accelerated by nudge traffic. This
  gate covers *both* counters: the loop's in-memory streak and the
  `.tick-state/quiescent-counter.txt` the one-shot `tick-cadence:` line reads.
  A nudge-initiated tick is also held to a 10s floor measured from the end of
  the previous tick, bounding the rate a nudge storm can force. The previously
  scheduled cadence deadline remains the upper bound: if it arrives before the
  floor, the tick starts at that deadline.

Terminal transitions into `ai:ready-for-human`, `ai:needs-human-decision`, or
`ai:max-iterations` emit a best-effort macOS notification, a stderr line, and a
`notify_terminal_label` tick-log entry. Notification failure is non-fatal. The
loop's quiescent stop emits the same operator-facing signals with
`loop_quiescent_stop`, and a detected engine stall with `engine_stalled`. The
quiescent stop also names any held item occupying a slot when it stops.

### Held engines

A dead wrapper can leave a paid child alive in its recorded process group. The
tick can neither prove that process's identity nor safely signal the group, so
it escalates the item to `ai:needs-human-decision` and leaves the marker and
scratch alone while the group may still be alive. Occupancy has to answer two
different questions about that marker, and one count cannot answer both:

- **Admission** asks "could a paid engine still be running here?" — yes, so the
  slot stays taken and no second engine is ever dispatched onto the same work.
- **Quiescence** asks "is there a decision left for *the loop* to make?" — for
  the escalated-and-left shape, no: the hold has handed that to a human.
  Counting it would make the loop's terminal stop unreachable, so the pipeline
  would poll forever with nothing to do and the operator would never be told.

A hold on its own is **not** enough to discount an item from quiescence. The
question is whether a process is still alive under the marker, not what the
marker's classify state says — the loop only stops supervising work it has
genuinely given up on. `_pipeline_actionable` is where that bound lives, and the
table reads off it: an item stays awake while its process or group is alive and,
for a `hung` marker, while identity proved `matched`; a held item outside that
bound is discounted.

| Held item | Pipeline still owes it | Discounted? |
|---|---|---|
| `live`, group alive | enforce the deadline; archive the evidence and reclaim the slot on proven death | **No** |
| `complete`, group alive | archive the result once the group dies | **No** |
| `hung`, identity `matched` | terminate the runaway | **No** |
| `hung`, identity unprovable | nothing — escalate-and-leave (#578) | Yes |
| `launching` (pre-spawn lease) | nothing — the marker records no PID | Yes |
| markerless | nothing — no process to harvest or signal | Yes |

Discounting a live engine would let the loop stop with a paid process still
running and nobody left to collect its result. `complete` is on that list
because an envelope can be published while a child still runs, and the hold
defers the archive until the group is proven dead — so the loop has to be there
to do it.

The `yes` rows that still have a marker are the shapes the pipeline can never
act on again. A lease is the sharper one: `valid_marker` makes the ownership
groups mutually exclusive, so a lease carries no `pid`/`start_time`/`pgid` and
`classify_dispatch` pins it at `launching` without consulting the deadline.
Death can never be proven for it, so the hold gate reclaims it on the absence of
a recorded process instead — otherwise the pipeline strands a slot on its own
escalation, since a crashed spawn escalates to `ai:needs-human-decision` and
then reads that label back as a hold forever.

"Publish" is not "harvest". A hold forbids advancing the stage, so a result that
finishes under one does not create a PR, promote a review, or move a fixed PR to
recheck. Once process-group death is proven, the tick prepares local evidence,
publishes the validated engine outcome on the item, releases only its in-flight
label, and then finalizes the archive. `held_result_published` names that completed
sequence; `held_result_comment_failed` means the post failed and the marker,
result, scratch, and attempts all remain in source custody for the next tick.

The exact hold is revalidated at mutation time — before the comment is published
and again before the terminal local custody move, whether or not an in-flight
label needs removal — so a hold removed after the decision cannot receive a
comment claiming it "remains authoritative", nor be finalized from a stale
observation. Once the comment has landed, the prepared archive is retained even
if the release then fails: the comment has promised the operator the output is
in the harvest-evidence archive, and a live promise must not point at nothing.

The durable workflow authority is the GitHub human-hold label itself:
`ai:needs-human-decision` or `ai:blocked`. There is deliberately no local
custody receipt. The completed-result branch does keep a controller-owned comment
receipt long enough to suppress reposts after a crash; it records publication,
not ownership, and is retired after the archive lands. The hold label remains
remote, durable across process and host restarts, visible to the operator, and
re-read on every tick.

#### Two custody tracks

A hold does not transfer one thing; it transfers one of two, and conflating them
is how a held item ends up either over-protected or leaking a slot:

| | **Workflow custody** | **Process custody** |
|---|---|---|
| Governs | labels, escalation, advancing the stage | the paid process, the dispatch deadline |
| Does a hold transfer it? | **Yes**, fully | **No — it never transfers** |
| Released by | the operator resolving the hold | proven process-group death |

Process custody never moves because the operator cannot hold it: they have no
PID, cannot signal it, and may not know the process exists. Setting a hold is a
statement about the *work*, not an offer to manage a Unix process. The dispatch
deadline therefore remains this pipeline's promise, and it still applies.

That yields one rule per question, with no special cases:

- **Escalate, replace the hold, or advance the stage?** No — workflow custody
  moved.
- **Kill an overdue engine whose identity is proven `matched`?** **Yes** — a
  runaway is a spend problem, not a decision. The kill advances nothing: the
  labels and the scratch are untouched, so the operator keeps every piece of
  evidence and the pending choice stays exactly where they left it. An engine
  whose identity is *unprovable* is still left alone, because its PID may have
  been reused and signalling it could hit an unrelated process.
- **Delete scratch while the group may live?** No — the marker is the only link
  to a process that would otherwise become untrackable and unkillable.
- **Release the pipeline's own in-flight label after proven death?** Yes — see
  below. That is the pipeline retracting its own receipt, not overruling a human.

**Proven group death is the one release condition,** and it releases two things:
the local scratch, into the ordinary archive/cleanup path, and the pipeline's
*own* in-flight label (`ai:reviewing` / `ai:implementing` / `ai:fixing`), via a
pure `--remove-label` that never names the hold. That label is an automation
receipt, not human custody; the admission slot is freed by destroying the
marker, not by removing the label, and leaving the label behind would strand
the item at a pipeline label no tick clears (#604) and no dispatcher picks
up — costing an operator's attention rather than a slot. The release is ordered
*before* the archive and confirmed by a read-back: if the removal or its
confirmation fails, the marker survives as the only thing that can re-prove
death on the next tick. The hold itself always remains for the operator to
resolve.

Cadence resolves holds with one `gh <kind> view --json labels` per occupied
identity, rather than scanning the repository's whole history of held items,
and a closed or merged item reads the same as an open one. A failed or
malformed hold read is unknown control-plane state: the loop stays warm and
does not claim quiescence.

Occupancy is the set of **local dispatch markers** — the engines this machine
launched and can still see — and nothing else. A marker holds its slot while
its classify state is engine-occupied *or* its process group is still alive,
so an engine that published its envelope with a child still running keeps the
slot it is still spending and still owes an archive.

An `ai:` in-flight label does **not** hold a slot. The label is a shared
workflow state, not a pipeline lease: `ai:implementing` is also the documented
manual pickup label, and a markerless in-flight label is a deliberate no-op
that no tick clears. Counting those as paid engines meant a hand-set label, or
one whose harvest relabel was missed, silently consumed a slot forever with no
dispatch marker behind it — four of them starved every dispatch on 2026-07-07
and again on 2026-08-20 (#534) until an operator diagnosed the count and
stripped labels by hand. The cap now
bounds this machine's concurrent spend, which is the thing it exists to bound.

This changes the ceiling, not per-item acquisition. Eligibility remains a
separate gate that reads the authoritative label set at the point of decision,
so an item another machine or a person already holds *at that moment* is still
skipped. It is not an exclusive lock and never was: dispatch runs lease →
launch → relabel, so two ticks that both select an item before either relabel
lands will both spawn a paid engine, and the loser terminates its own when its
relabel fails. That bounded acquisition race is unchanged by this section — see
the lease → launch → relabel step under "File-based handoff" in
`docs/planning/designs/pr-pipeline-tick.md` for why closing it needs
compare-and-swap acquisition.

What is deliberately given up here is the sum across machines: another host's
engine no longer consumes this host's budget.

The in-flight read still runs, for two other jobs. In DISPATCH it is the
per-item *eligibility* input: each dispatcher skips an item already carrying an
in-flight label, and appends the engines it launches so all three share one
view of what this tick has taken. In the cadence observation it runs as a
health probe on a surface the queue check cannot see — it goes through GitHub's
search index while both queue probes go through the list API — and its value is
discarded. It is not what keeps a failed queue read out of a quiescence claim;
each queue probe already fails warm on its own. Stranded in-flight labels
surface on the status dashboard; they no longer cost dispatch.

Consequences for the loop:

- With nothing actionable and every occupied slot held, the loop reaches
  quiescence and stops after the usual heartbeat count — on both the in-process
  `--loop` path and the one-shot `tick-cadence:` path.
- One held slot out of `MAX_IN_FLIGHT` does **not** stop a loop that still has
  actionable work; the other slots keep dispatching. If *every* slot is held
  while a queue is still actionable, the loop legitimately stays warm and cannot
  dispatch until the operator acts — so the cadence reason names the holders in
  that case too, not only on a stop.
- A human hold coexisting with an in-flight label keeps workflow custody with
  the human while the engine may live. The controller leaves the labels and the
  scratch untouched; admission counts the occupied dispatch identity while
  cadence discounts the held item.
- Once the process group dies, the marker classifies `stale` like any other,
  ordinary cleanup reclaims scratch and worktree metadata, the in-flight label
  is released, and the slot returns to the cap. The human hold is untouched.
- An item that reaches *markerless* scratch under a hold keeps its in-flight
  label: with no marker there is no PID or process group to probe, so death
  cannot be proven and the label is retained deliberately rather than cleared.
  It holds no admission slot — the marker that would have spent one is already
  gone — so the cost is a stranded item an operator must resolve, not a
  concurrency slot. Resolving the hold hands it back to ordinary stale-label
  reconciliation.
- Cleanup under a hold also clears the durable attempt counter, because custody
  genuinely moved to the human. An operator who holds and then releases an item
  therefore returns it to a full retry budget.

`tick-state:` reports the count as `held=N`, so an `in_flight=0` tick that
still cannot dispatch at the cap is explainable rather than mysterious. `held`
counts *slots*, matching `in_flight`, so the two always add up to occupancy —
a PR carrying both `ai:reviewing` and `ai:fixing` is two held slots, because
admission already treats it as two paid ones. The `tick-cadence:` reason
additionally *names* each held slot
(`; awaiting human decision: pr #42 ai:reviewing (ai:needs-human-decision)`) —
the one-shot `/loop` path has only that line to notify from, so neither a stop
nor a wedged cap caused by a human holding an engine can go unexplained.

### Engine selection (`--impl-engine`)

```bash
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py --impl-engine claude-ds
```

Controls whether the implementer and fixer engines use `claude` (default) or
`claude-ds` (DeepSeek via Claude wrapper). The reviewer always uses `codex`.

### Model override (`--model`)

```bash
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py --model claude-sonnet-4-6
```

Overrides the model used by implementer and fixer engines. When not given, the
`claude` engine gets **no `--model` flag at all**, so the CLI resolves the model
through its own settings precedence — upgrade your default and the pipeline
follows. Precedence is worth knowing, because it is not simply "your user
settings": a checked-in `.claude/settings.json` outranks `~/.claude/settings.json`,
and the fixer runs in a worktree of the PR head, so branch content can select its
model. Neither is set in this repo today, and the flag above outranks both — pass
it when a run must be pinned regardless of branch content. `claude-ds` still
defaults to `deepseek-v4-pro[1m]`, because the DeepSeek endpoint cannot resolve
Anthropic model aliases and so has no operator default to inherit.

When given, `--model` overrides the `--model` CLI flag for the `claude` engine
and additionally sets `ANTHROPIC_MODEL` for `claude-ds`.

### Reviewer model and effort (`--review-model`, `--review-effort`)

```bash
uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py --review-model gpt-6 --review-effort xhigh
```

The reviewer is a separate provider from the implementer/fixer engines, so it
has its own knobs. Resolution order for each value independently:

1. the explicit flag, when given
2. `model` / `model_reasoning_effort` from the operator's codex config —
   `$CODEX_HOME/config.toml` when that variable is set, else `~/.codex/config.toml`,
   the same root `codex` itself resolves
3. the pinned fallbacks (`gpt-5.5`, `high`)

Profiles are not consulted: since codex 0.146 the legacy `profile = "…"` key is
rejected by codex outright, and profiles v2 apply only when `--profile` is passed
per invocation, which the pipeline never does. Top-level config *is* the
operator's effective default.

Every value — flag or config — is shape-checked for the sink it reaches. The
effort is interpolated into a `-c model_reasoning_effort="…"` TOML expression, so
it must be a bare token; the model is its own argv element, so provider-qualified
ids like `openai/gpt-5.1-codex` are fine but a leading dash (which codex would
read as an option) is not. A rejected value is reported on stderr and falls
through to the next source rather than being swapped in silence.

The reviewer cannot inherit this config the way the claude engines inherit
theirs. It runs under `env -i` isolation with `HOME`/`CODEX_HOME` pointed at a
per-PR scrub dir, plus `--ignore-user-config` — so the tick (the trusted
parent) reads the file and forwards the two resolved values as explicit flags.

**Exactly two keys ever cross that boundary**, and widening the set is a
security decision, not a convenience one. The reviewer is the one engine whose
entire input is attacker-controlled PR-head content; a real operator config
also carries `[mcp_servers]` (a node REPL is arbitrary code execution),
per-project `sandbox_mode`/`network_access`, hooks, and `notify`. None of that
may reach it. A missing, malformed, or wrong-typed config falls back to the
pins rather than aborting the dispatch or silently handing the review to
codex's own built-in defaults.

Each dispatch logs the values it resolved (`action=model_resolved`) so a
review's provenance survives a later config edit.

### Admission gate: reviewer policy files (`config.toml`)

A PR whose diff edits files that govern the reviewer itself — `CLAUDE.md`,
`AGENTS.md` (at any directory depth), or the review checklists under
`.claude/hooks/` — is normally escalated to `ai:needs-human-decision` instead
of auto-reviewed: the reviewer runs inside the PR's own worktree, so it would
review under instructions the PR itself supplied. The operator can accept that
trust boundary for the whole gate in
`$PR_PIPELINE_HOME/skills/pr-pipeline-tick/config.toml`:

```toml
[policy]
allow_touching = true
```

`true` lets policy-editing PRs flow to auto-review anyway. Missing config,
malformed TOML, or `false` (or an absent key) all keep the conservative
escalation — the gate fails closed. **The shipped `config.toml` sets `false`.**
That is deliberate: an install inherits whatever is committed here, so shipping
the opt-out would make a trust decision on the adopter's behalf. Setting `true`
is an act the operator performs on their own copy. The human merge gate after
`ai:ready-for-human` is unaffected either way.

`config.toml` itself is part of the policy path set, as are the gate's own
implementation files (`lib/pipeline.py`, `lib/tick.py`): a PR that flips
`allow_touching`, rewrites `_policy_touching_allowed` to return `True`
unconditionally, or removes the gate call from the reviewer dispatch is
escalated to a human (while the gate is enabled) instead of auto-reviewed —
otherwise a merged change would silently disable the gate for every future
policy-editing PR. Those three are matched by path *suffix*
(`skills/pr-pipeline-tick/config.toml` and the two `lib/` files), so the
self-protection holds wherever the skill is installed — under `.claude/skills/`
in a host repo, at `skills/` in the pipeline's own repository, or under a
plugin root.

### Prerequisites for `claude-ds`

Implementer and fixer engines are trusted local subprocesses and inherit the
operator's complete environment. This is the current MIP trust boundary: the
engines receive the same ambient credentials and capabilities as the terminal
that launches the tick. In cmux that includes `CMUX_SOCKET_PATH`; in another
terminal the engines inherit that terminal's authentication instead. The
reviewer remains separately isolated with its scrubbed environment.

When dispatching with `--impl-engine claude-ds`, set
`DEEPSEEK_API_KEY` in the shell that runs the tick, or set
`DEEPSEEK_API_KEYPATH` to a readable token file. Legacy environments may place an
`sk-*` token directly in `DEEPSEEK_API_KEYPATH`; an unmistakable token shape is
accepted as a value only when nothing exists at that path, while missing,
directory, dangling-symlink, permission, and other path-read failures fail
closed. `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are optional.

### Prompt-bound skills (`skill_unresolved`)

The fixer prompt binds `review-response-hygiene` by naming its doc path and
requiring the engine to read it before editing, then to declare **per finding**
whether the fix **repairs** the accepted design or **replaces** it. One run may
mix the two, and each replacement states its own reasoning rather than sharing
one run-wide set. A replacement is never blocked and never escalates — it
publishes the shape the reviewer proposed, why that shape was rejected, the
contract that replaced it, and the surfaces it audited, so the next reviewer
evaluates the decision instead of reconstructing it from the diff. The decisions
are re-checked against the pushed diff before the completion sentinel, which now
needs them alongside the three green commands.

The binding is a doc path rather than a skill invocation on purpose: engines run
under `--permission-mode dontAsk` (the default; the experimental `auto` mode is
opt-in) with a bounded allowlist that carries `Read` and no `Skill` tool.

Because it is a path, dispatch resolves it before spending anything. The probe
runs against the **fixer's own worktree** — a detached checkout of the PR head,
which is the tree the engine's cwd resolves the path in, so a PR branch cut
before the skill existed is caught rather than waved through by the operator's
newer checkout. An unreadable doc logs `skill_unresolved` (with the skill names
and the probed paths), escalates the PR to `ai:needs-human-decision`, and removes
the worktree — no lease, no relabel to `ai:fixing`, no engine launched. Recover by
restoring the skill doc on the PR branch (usually by rebasing it onto a base that
has it), then relabel the PR back to `ai:changes-requested`.

### Governing context the fixer is handed

The rules above ask the fixer to check the PR's story surface (rule 2) and to
re-read the contract the review cited (rule 5). Neither document is in the
worktree, and the engine has no `gh` and no web access, so dispatch reads them
and substitutes them into the prompt: the PR body, and the body of every issue
GitHub reports under `closingIssuesReferences`. Without them the prompt would be
demanding work the engine cannot perform.

Reading them here rather than granting the engine `gh` is the point, and it does
not depend on which permission mode the engine launched under. Under `dontAsk` a
`gh` call is denied outright; under the experimental `auto` mode it would reach
the classifier instead, which is exactly the case the dispatch-side read is
insurance against — an engine-side read that fails is invisible to the
controller and silently becomes "no contract", while a controller-side one
retains the PR. The prompt therefore tells the engine the quoted copies are all
it has, in either mode.

Both are capped at `FIXER_CONTEXT_BUDGET` (20000 characters) with the cut stated
in the text, so a truncated contract cannot read as a shorter one. The PR body
gets one budget of its own; the linked issues share a second one. That share is
not a flat `budget // n` — the issues are allowanced shortest-first with each
one's unused remainder rolled forward, so a one-paragraph contract cannot
reserve half the budget away from a long one, and a long one cannot spend the
whole budget before a later issue is reached. Each issue keeps its
`### Issue #N — title` heading outside its allowance, because an issue cut short
is something the fixer can still act on and an issue that renders as nothing is
not.

These are authoritative reads: a failed `gh` call, or a payload whose `body` /
`closingIssuesReferences` fields are not the expected types, retains the PR at
`ai:changes-requested` and logs a `skip` — it never renders as an empty body or
an absent contract. A PR that genuinely closes no issue is not an error; the
prompt says so in words.

### The PR body the fixer hands back

The fixer still has no `gh`, so it cannot edit the PR body itself. What it can do
is write a full replacement to `.pr-body-revision.md` in its worktree root and
let the harvest publish it — the same engine-writes-file, controller-applies-it
shape as the implementer's `.pr-body.md`. Leaving the file absent is the normal
case and the prompt says so; it is written only when the body no longer describes
the head the fixer pushed.

The file carries a two-line envelope before the body. The first line binds it to
the **dispatch** (`pr-body-revision: <dispatch id>`), the second to the **commit**
(`pr-body-revision-head: <40-char OID>`). Two different lies need catching: a file
that rode in on the branch from an earlier run, and a file this engine really did
write but wrote before its last commit. The prompt therefore has the fixer write
the file *after* its final push, since only the push settles which OID the body
describes.

Every gate below stands between that file and the live body, because `gh pr edit`
replaces the whole body and GitHub gives the pipeline no undo. They run in the
order listed, so the reason published is the true one rather than the first one
that happens to match:

| Gate | What it rejects |
|---|---|
| Size | A file past `_PR_BODY_MAX_CHARS` (65,536), measured in characters *and* in UTF-8 bytes — GitHub does not document which it counts, so the stricter reading wins. The bound is applied to the `read` call, not to the string it returns, so a runaway engine file cannot exhaust the tick before being classified. A file present but unreadable is its own verdict again: neither is reported as a malformed body. |
| Stamp | A file whose first two lines are not this run's dispatch stamp and a head stamp. The fixer worktree is a detached checkout of the PR head, so an unstamped file rode in on the branch — a stray `git add -A` from an earlier run — and is not this run's work. |
| Provenance | A marker with no `pr_body` block (written before this path existed, or a PR whose body pinned no closure), or a live body the harvest could not read. Judging such a candidate on its content would report a perfectly good body as malformed. |
| Truncation | Any revision at all, when the body handed to the engine was cut short by the context bound. The engine would be replacing a document whose tail it never saw, and no structural check can tell a deliberate rewrite from the silent loss of a section past the cut. |
| Head | A head stamp that is not the PR's live head at harvest. A body written for an intermediate commit describes work that is no longer the tip. |
| Closures | A body whose `## Summary` no longer opens with the pinned `Closes #N`, or whose full set of closing directives (`closes`/`fixes`/`resolves` anywhere in the body, cross-repo and URL forms included) differs from the set pinned at dispatch. Merging acts on every one of them, so a swap, a drop, or an addition changes what the PR does. Read from the body rather than from `closingIssuesReferences`, which GitHub leaves empty for a PR based on a non-default branch. |
| Sections | A body that fails the repo's PR-body contract. |
| Conflict | A live body that no longer matches the digest dispatch recorded — somebody edited it while the fixer ran. A live body that already *equals* the candidate is this run's own landed write, not a conflict, so a retry re-applies it harmlessly. |
| Outcome | Any result other than `recheck`. An escalated or requeued run may not have pushed the head the replacement describes. |

Those gates all read state the harvest captured earlier in the tick. Immediately
before `gh pr edit`, one more read re-establishes authority against GitHub as it
is *now*: the PR's labels, head, and body. A hold (`ai:blocked`,
`ai:needs-human-decision`) added since reconcile wins over a completed engine
result, and a head or body that moved since classification means the candidate was
judged against something else. Any of the three abandons the whole harvest for
this tick — no body, no comment, no relabel — and the scratch, marker, candidate,
and label are all retained so the next tick re-derives the outcome from what it
finds then. A hold does not count as a failed attempt: the attempt counter
escalates to `ai:needs-human-decision` at its cap, and escalating onto a human's
own hold is exactly what the hold revokes.

That last read narrows the unguarded window to a single API call; it does not
close it. GitHub's REST API offers no conditional (compare-and-swap) write for a
PR body — conditional requests do not apply to unsafe methods — so a human edit
landing inside that one call is still overwritten. The pipeline's claim is
"narrowed and fail-closed on anything observable", not "atomic".

The edit lands before the fix comment, and therefore before `ai:revised` — the
label that invites the next reviewer must never arrive over a handoff the
pipeline knew was stale. A failed `gh pr edit` takes the same bound as a failed
comment post: no comment, no relabel, scratch retained for the next tick.

Every refusal is named in the published comment as `- PR body: left unchanged —
<reason>`; only an absent file is silent. A body quietly left alone reads exactly
like a pipeline that never looked at it, and the next reviewer cannot tell the
difference. The tick log records the same verdict as `pr_body_revision`, and an
abandoned write as `pr_body_revision_withheld`.

The implementer needs no envelope of its own. It also writes `.pr-body.md` after
its final push, so the body describes the commits it published — but the harvest
uses that file to *open* a PR that does not exist yet, so there is no live body to
clobber and no undo to be missing. The reviewer runs read-only and pushes nothing
at all.

### Transient quota and limit results

Implementer and fixer results without a valid completion sentinel are classified
as transient when the provider reports status 429, a known rate-limit subtype, or
a known session/usage/quota-limit message. A valid success or
`NEEDS_HUMAN_DECISION` sentinel remains authoritative even if the surrounding text
mentions a limit.

Transient results share an account-domain cooldown across implementer and fixer
dispatch. `claude` and `claude-ds` use separate domains so a Claude subscription
limit does not block DeepSeek-routed work. When the provider message includes a
reset wall-clock time, the cooldown ends five minutes after the next occurrence;
otherwise it lasts one hour. Durable state lives in
`.claude/worktrees/.engine-cooldowns/`.

The retry boundary is `MAX_DISPATCH_ATTEMPTS = 3`:

1. Results one and two restore the dispatch marker's pre-label
   (`ai:available` or `ai:changes-requested`), increment the item attempt counter,
   and clean the completed scratch worktree.
2. Result three moves the item to `ai:needs-human-decision`, archives the engine
   evidence, clears the attempt counter, and cleans scratch.

The structured tick log records every decision:

| Action | Meaning |
|---|---|
| `transient_revert` | Cooldown persisted and the item requeued for a bounded retry |
| `cooldown_skip` | Implementer/fixer dispatch skipped while that provider domain is cooling down |
| `transient_exhausted` | Third transient result escalated instead of spending a fourth engine run |
| `cooldown_state_error` | Cooldown state could not be read or written; paid dispatch fails closed |

Genuine escalations and exhausted transient retries retain their source artifacts
under `.claude/worktrees/.harvest-evidence/` before the terminal label transition and
scratch cleanup. If the result cannot be archived, the in-flight label and scratch
evidence remain for a later tick. The
`engine_result_summary` tick entry carries the parsed sentinel, structured error
fields, and a bounded result excerpt.

Raw model output stays out of GitHub comments with one deliberate exception: both
**terminal fixer outcomes** embed a bounded excerpt of the engine's `result` text
in the PR comment. An escalation needs it because the comment asks a human to make
a decision and they need the engine's own words; a `recheck` needs it because the
recheck path archives no evidence and deletes the scratch that held `result.json`,
so the comment is the run's only durable record of what was fixed. The fixer
prompt states a summary budget (`pipeline.FIX_SUMMARY_BUDGET`, 3000 characters)
and the excerpt limit is that budget plus headroom for the completion sentinel,
so a summary written to the stated size — including the repair-or-replacement
decision each finding's block carries — reaches the PR whole. Anything past the
limit is truncated, and the excerpt is stripped of `<!--`, `-->`, fence, and
`</details>` sequences so engine text cannot forge a pipeline marker or escape
its rendered block. The attempt-cap and transient escalations publish no model
output — they report a failed side effect and a spent retry budget, not an
engine result.

A terminal transition that **reports an engine result, or fires before one was
launched** posts a comment before the label moves. Every one of them is gated on
that post landing, except the two below that cannot be bounded — those are
best-effort. Today's outcomes:

| Terminal transition | Post gates the relabel | On post failure |
|---|---|---|
| fixer `recheck` → `ai:revised` | yes | label held, scratch retained, retried next tick |
| fixer `escalate` → `ai:needs-human-decision` | yes | same |
| transient retries exhausted → `ai:needs-human-decision` | yes, and bounded | label held; the failure burns an attempt, so repeated failure walks into the cap |
| hold beside a completed result → in-flight label released, hold retained | yes, bounded by the hold itself | nothing moves; marker, result, scratch, and attempts retained, retried next tick |
| dispatch setup failure → `ai:needs-human-decision` | yes, and bounded | label held at the pickup label and the scratch removed — no engine ran, and a retained lease would be reconciled to the terminal label next tick; the failure burns an attempt |
| PR touches reviewer policy → `ai:needs-human-decision` (configurable via `config.toml` `[policy] allow_touching`, see "Admission gate" above) | yes, and bounded | same |
| ambiguous marker → `ai:needs-human-decision` | yes, and bounded | label held, but the scratch is **retained** — this path cannot prove the marker safe to clear; dispatch stays blocked either way |
| attempt cap → `ai:needs-human-decision` | **no — best effort** | label lands unexplained; logged `escalation_comment_failed` |
| transient attempt-counter write failure → `ai:needs-human-decision` | **no — best effort** | same |

**Two paths are exempt from the gate by design**, both for the same reason: they
exist to stop an item pinning an admission slot forever, so they cannot be blocked
on one more remote write.

- The **attempt-cap** escalation fires because this item's remote writes keep
  failing; gating it on another would reinstate the stall the cap prevents.
- The **attempt-counter write failure** inside the transient path escalates
  because the retry budget itself could not be persisted. With no budget to
  bound it, a gated transition could never terminate, so its post is best-effort
  too and a failure is logged as `escalation_comment_failed`.

Transient *exhaustion* is gated, because that one can be bounded: a failed post
increments the attempt counter, and once the counter reaches the cap the item
lands `ai:needs-human-decision` under the cap's own superseding comment.

The **held completion** is gated on a different bound. It burns no attempt, so a
persistently failing post retains full custody — marker, scratch, and in-flight
label — until it lands, and it is the retained *marker* that keeps one admission
slot occupied; the label alone never does. That is acceptable only because the
item is already awaiting a human by definition: the operator who set the hold
resolves it, the next tick then harvests the completion through the ordinary
path, and clearing the marker there is what frees the slot. The trade is deliberate: releasing custody on a failed post is what left
PR #578 merged under an unexplained `ai:needs-human-decision`. If a post can never
succeed — a locked or archived item — the operator's own hold removal is the
release, not another tick.

The **pre-launch dispatch escalations** in the table above are gated and bounded
the same way, against the same counter. Nothing is spent when they fire — no
engine, no admission slot — so a failed post costs only a held label, and the
item sits at its ordinary pickup label until the next tick retries. That is also
why they need the bound: a permanently unpostable comment would leave the item
looking like available work forever, so once the counter reaches the cap the
label lands unexplained and the miss is logged as `escalation_comment_failed`.

A held escalation also takes its scratch with it. Setup failed before launch, so
any lease it wrote is a phantom no engine owns — and since a tick harvests before
it dispatches, leaving one behind would let the next tick reconcile it straight
to `ai:needs-human-decision` with no comment instead of retrying the post.

When the label does land, the counter is cleared so a later cycle for the same
item starts fresh — except on the ambiguous-marker path, which retains the
marker and therefore leaves the counter to whoever still owns it.

### Silent transitions

The table above plus the reviewer's own verdict comment is the tick's *entire*
publishing surface — exactly two functions in `tick.py` run `gh … comment`.
**Every other terminal transition publishes nothing.**

That is deliberately stated as a complement rather than a list. The silent
transitions were enumerated three times and undercounted three times — they fire
from reconcile, from harvest, and from dispatch, and the list was maintained by
hand in three documents. The publishing side is two call sites, so this direction
cannot go stale.

Every silent transition left is one where an engine *was* launched and the tick
could not read a result from it. For triage, what matters is whether that engine
finished — that is what decides where the explanation lives:

| | Silent transitions | Where the explanation is | Tracked by |
|---|---|---|---|
| **An engine finished** | wrapper-confirmed reviewer failure | `.harvest-evidence/` bundle — machine-local, not on the item | untracked. #618 closed the sibling case (a hold beside a completed marker, now the gated row above); this path has the same shape and needs the same treatment |
| **An engine may still be running** | reconcile escalations (hung engine, unverified live custody); each stage's post-launch guard, when the in-flight relabel failed *and* the engine could not be killed | tick log only — no evidence bundle is written, and the scratch is retained because a live engine may still own it | untracked; the marker is retained, so harvest still owns the item |

The top row is unfinished work, not an exemption: nothing bounds its post today
and nothing prevents bounding it. It is also the one that surprises operators —
an engine finished its work and the run still reports `harvested: 0`. A hold
found beside a *completed* marker has the same shape and still resolves in the
hold's favour rather than being harvested, but it is no longer silent: it
publishes the validated outcome before releasing the in-flight label. The bottom
row is different: the label there is a duplicate-dispatch guard over a possibly
live engine, not a verdict on the item, and the run that engine is still in owns
the explanation.

**Recovering the reason for an unexplained hold.** An item at
`ai:needs-human-decision` with no comment usually means an engine was launched
for it — but not always, so check the cheapest disqualifier first:

1. The tick log at `.claude/worktrees/.tick-log`, filtered to that item's number.
   An `escalation_comment_failed` entry means the comment is missing because it
   could not be posted, not because the path is silent — the reason is in that
   entry, and no engine need ever have run. This is also the only record anywhere
   for the bottom row of the table above.
2. Its scratch under `.claude/worktrees/`. A retained scratch with a marker means
   the bottom row — read `dispatch.json` for the engine's identity and check
   whether that process is still alive before touching anything.
3. `.harvest-evidence/` for that item's bundle. A bundle exists only when an
   engine actually finished (the top row). If it is there, it holds what the
   engine did.

An empty `.harvest-evidence/` does **not** mean nothing ran — it means no engine
result was retained. And with no `escalation_comment_failed` entry for the item,
it does rule out a dispatch that never launched: every pre-launch escalation
whose comment landed says so on the item itself.

## Three observability surfaces

You have three independent surfaces, each serving a different purpose.

### 1. Dashboard — `status.sh` (real-time, human-readable)

Run in a watch loop in a separate terminal pane:

```bash
# Primary (Linux / watch installed):
watch -n 5 'bash $PR_PIPELINE_HOME/skills/pr-pipeline-status/status.sh'

# Fallback (macOS / no watch):
while true; do clear; bash $PR_PIPELINE_HOME/skills/pr-pipeline-status/status.sh; sleep 5; done
```

Read-only — safe to run alongside a live tick. It reads GitHub labels and worktree
markers; it never writes, edits labels, or touches engines.

#### IN-FLIGHT ENGINES

One row per live `dispatch.json` in the worktrees:

```
IN-FLIGHT ENGINES
  PR#468   review    codex     pid=12345  alive  result-ready   12m-to-deadline
  PR#469   implement claude-ds pid=12346  alive  working        47m-to-deadline
```

| Column | Meaning |
|---|---|
| `PR#468` | PR number (or `?` if marker is malformed) |
| `review` / `implement` / `fix` | Pipeline stage |
| `codex` / `claude-ds` / `claude` | Engine+model dispatched |
| `pid=12345` | Process ID (so you can `htop` or `kill` it) |
| `alive` / `DEAD` | `kill -0` check — DEAD means the process exited or crashed |
| `working` / `result-ready` | Whether a non-empty result file has landed — implement/fix engines redirect stdout into `result.json`, so an empty one means nothing has been written yet |
| `12m-to-deadline` | Minutes before the hung-engine timeout fires |

This is your **triage pane**. If you see `DEAD` + `working` + negative deadline, an
engine crashed silently — check the tick log for that PR's last action.

The rows come from *this machine's* marker root only. An engine dispatched from
another machine has no row here and never will, so the absence of a row is not
evidence that nothing is running — see
[Markerless in-flight items](#markerless-in-flight-items--why-the-tick-leaves-them-alone).

#### PIPELINE STATE

All open PRs and all open issues with `ai:` labels, grouped by label, in two
sections:

```
PIPELINE STATE (open PRs by ai: label)
  ai:reviewing          #468   Implement memory retrieval
  ai:ready-for-human    #467   Update auth middleware

PIPELINE STATE (open issues by ai: label)
  ai:available          #439   Add streaming support to chat endpoint
  ai:implementing       #438   Fix pagination offset bug
```

This is your **queue view** — what's waiting, what's in progress, what's ready for
you. Both listings cover every `ai:` label, not just the dispatchable ones: since
reconciliation never escalates an in-flight item on a missing marker, this is the
only place a stalled `ai:implementing` becomes visible. Visible is not the same
as diagnosed — these labels are shared across every machine, while the engine
rows above are not, so pairing them tells you an item *may* need attention, never
that it is unowned.

#### LAST TICK AUDIT LOG

The structured decisions from the most recent tick:

```
LAST TICK AUDIT LOG
  14:32:15  PR#468   harvest    review     reconciling        marker+label matched,live
  14:32:15  PR#468   harvest    review     skip_live          still live
  14:32:15  PR#-     dispatch   implement  launch             impl-1748321535-12345-439      claude-ds
  14:32:15  PR#468   dispatch   review     launch             d-468-1748321535-12345         codex
```

| Column | Meaning |
|---|---|
| `14:32:15` | Timestamp |
| `PR#468` | PR number (`-` = not PR-specific) |
| `harvest` / `dispatch` | Tick phase |
| `review` / `implement` / `fix` | Pipeline stage |
| `launch` / `skip_live` / `skip_no_token` | Action taken |
| `impl-1748...` / `still live` | Reason (dispatch ID or explanation) |
| `claude-ds` / `codex` / `claude` / *(blank)* | Engine used (blank for harvest entries) |

This is the **audit trail** — replay the tick's decisions without re-reading SKILL.md.

### 2. Tick log — `.claude/worktrees/.tick-log/` (structured, queryable)

Each tick writes one JSON-lines file (`tick-<timestamp>.jsonl`). One line per decision:

```json
{
  "ts": 1748321535,
  "pr": 468,
  "phase": "dispatch",
  "stage": "implement",
  "action": "launch",
  "reason": "pid=12345",
  "engine": "claude-ds",
  "dispatch_id": "impl-468-1748321535",
  "details": {"head_sha": "abc12345"}
}
```

This is your **query surface** for answering historical questions:

```bash
LOG_DIR=".claude/worktrees/.tick-log"

# All claude-ds implementer dispatches
jq 'select(.engine=="claude-ds")' "$LOG_DIR"/tick-*.jsonl

# Engine usage breakdown across all ticks
jq -s '[.[] | select(.action=="launch")] | group_by(.engine) | map({engine: .[0].engine, count: length})' "$LOG_DIR"/tick-*.jsonl

# Correlate dispatch → harvest for a single engine (cross-tick trace)
jq -s '[.[] | select(.dispatch_id=="impl-468-1748321535")] | sort_by(.ts)' "$LOG_DIR"/tick-*.jsonl

# Skill files observed during completed reviewer runs
jq -s '.[] | select(.action=="reviewer_skill_observation") | {pr, dispatch_id, details}' "$LOG_DIR"/tick-*.jsonl

# Admission gating: why were engines skipped?
jq -s '.[] | select(.action=="skip") | {pr, reason, details}' "$LOG_DIR"/tick-*.jsonl

# All dispatch skips and why (debug stalls)
jq -s '.[] | select(.action|startswith("skip_")) | {pr, action, reason}' "$LOG_DIR"/tick-*.jsonl

# Retained scratch, process/metadata cleanup failures, and stale-label reconciliation
jq -s '.[] | select(.action=="retained_scratch" or .action=="archive_failed" or .action=="kill_failed" or .action=="worktree_prune_failed" or (.action|startswith("stale_label_"))) | {pr, stage, action, reason, details}' "$LOG_DIR"/tick-*.jsonl

# Ticks that ran today
ls "$LOG_DIR"/tick-$(date +%Y%m%d)*.jsonl 2>/dev/null | wc -l
```

### 3. Worktree markers — `dispatch.json` (per-engine, authoritative)

Every dispatched engine gets a `dispatch.json` in its worktree:

```
.claude/worktrees/
  issue-439/
    dispatch.json      ← implementer marker (engine, PID, deadline, stage)
    .impl-prompt.txt   ← the prompt the implementer is working from
    result.json        ← implementer output (lands when engine exits)
  pr-468/
    dispatch.json      ← reviewer marker
    review.json        ← reviewer typed verdict (lands when engine exits)
    completion.json    ← engine wrapper exit proof
    review-observation.json ← bounded sanitized skill-file evidence
    stderr.log         ← bounded sanitized wrapper diagnostics only
```

The reviewer runs with `codex exec --json`. Its stdout is streamed through a bounded
sanitizer; raw JSONL, prompts, model messages, commands, command output, and absolute
paths are never retained. `review-observation.json` contains only normalized identities
for `SKILL.md` paths referenced by successful command executions plus parser counters.
HARVEST validates that exact shape and writes it to the tick log as
`reviewer_skill_observation` before cleanup. This is evidence of successful command
references, not a skill-file read or semantic invocation event: the current Codex CLI
exposes no explicit invocation event. An empty `observed_skills` list means no
successful command reference
was observed; it does not prove that no skill context was available or used. Observation
status never changes review validity, custody, labels, or cleanup decisions.

Reviewer stderr is discarded by the child wrapper. Reviewer `stderr.log` contains only
bounded sanitized wrapper diagnostics: the startup error class or fixed
`EngineExitError`. Inspect it with `completion.json` after a wrapper failure.
Implementer/fixer engine stderr remains available in the same per-worktree filename.

Implementers and fixers are wrapperless. Once their process is proven dead, a
non-empty `result.json` is treated as ready for harvest in memory; the tick does
not fabricate a `completion.json` or claim a verified zero exit status for those
engines — and it does not *read* one either. Dispatch passes `--envelope-path`
only for review, so an envelope in a wrapperless worktree had no legitimate
writer, while the engine that could have written it holds an unrestricted
`Write` tool with that worktree as its cwd. Believing it would let a running
engine declare its own completion and publish the result it wrote for itself.

A marker at launch (before the engine PID is published):

```json
{
  "engine": "claude-ds",
  "stage": "implement",
  "state": "launching",
  "dispatch_id": "impl-1748321535-12345-439",
  "deadline": 1748325135,
  "pr_number": 439,
  "base_ref": "main",
  "base_sha": "abc123...",
  "head_sha": "def456..."
}
```

After engine spawn, the marker is updated with `pid`, `start_time`, `pgid`, and
`state` moves from `"launching"` to the live identity.

Check what's running right now:

```bash
for f in .claude/worktrees/{issue,pr}-*/dispatch.json; do
  echo "$f: engine=$(jq -r '.engine' "$f") stage=$(jq -r '.stage' "$f") pid=$(jq -r '.pid' "$f")"
done
```

### Markerless in-flight items — why the tick leaves them alone

Markers are **machine-local**. Ticks run from more than one machine (and from
linked worktrees on one machine), so no tick can see another's `dispatch.json`.
A missing marker therefore proves nothing about custody: it is the steady-state
reading of another machine's live dispatch, and of the manual `ai:implementing`
pickup that `CLAUDE.md` tells contributors to perform before writing code.

So the tick **never acts on a markerless in-flight item**. No relabel, no
cleanup, no escalation. An earlier rule did escalate on marker absence and
mass-relabelled healthy work with it (issue #592); the credibility of
`ai:needs-human-decision` is worth more than the automation it bought. Every
custody escalation the tick can raise routes to that one label — the reviewer's
iteration cap is the only path to `ai:max-iterations` — so a signal that fires on
healthy work degrades the pipeline's whole stop channel, not one corner of it.

What you give up: an engine that dies without writing a result leaves its item at
an in-flight label indefinitely, and nothing reaps it. Clearing it is a manual,
operator-only action — and the automation's own reason for not doing it applies
to you.

**Dashboard absence is never custody proof.** `status.sh` reads shared GitHub
labels plus *this machine's* marker root. An in-flight label with no matching
row in `IN-FLIGHT ENGINES` is exactly as consistent with another machine's
healthy dispatch, or with a manual `ai:implementing` pickup, as it is with a
dead engine — the dashboard cannot tell them apart, and relabelling on that
reading steals live work the same way the old automation did.

So establish custody out of band before editing any label:

1. Ask on every machine that runs ticks. `pr-pipeline-status` on *that* machine
   shows its markers; a live engine appears under `IN-FLIGHT ENGINES` there.
2. Ask whoever may have taken the item by hand — `CLAUDE.md` tells contributors
   to add `ai:implementing` before writing code, so an issue at that label with
   no engine anywhere is usually a person, not a corpse.
3. Only when the dispatch host's owner (or the session owner for a manual
   pickup) confirms nothing is running is the item yours to relabel.

Then restore the pickup label the stage started from, so the next tick can
redispatch it:

```bash
# implement — the issue goes back to the implementer queue
gh issue edit 439 --add-label ai:available --remove-label ai:implementing
# review — the PR returns to whichever label it was picked up from
gh pr edit 604 --add-label ai:implemented --remove-label ai:reviewing
# fix — the PR returns to the review verdict that called for the fix
gh pr edit 604 --add-label ai:changes-requested --remove-label ai:fixing
```

Check the PR's own timeline for which review label preceded `ai:reviewing`
(`ai:implemented` on a first review, `ai:revised` after a fix round); guessing
sends the item through the wrong stage.

**A late result can still arrive.** The engine you concluded was dead may
finish after you relabel — its scratch directory is untouched, and the next
tick on *its* machine will harvest it and drive its own transition on top of
yours. If you relabel, also remove that machine's scratch directory (with its
owner, after archiving it under `.harvest-evidence/`), or expect one surprise
label move later. Leaving the item stranded and visible is the safer default;
relabel only to put the item back in a queue someone will pick up. It frees no
slot here: no dispatch marker is holding one.

A *valid* marker is still proof and is still acted on: a `launching` lease seen at
reconcile means the spawn crashed after starting the engine, and that escalates to
`ai:needs-human-decision` because a paid engine may be alive and unkillable.

The full rationale, the incident data, and the conditions for revisiting this are
in `docs/planning/designs/pr-pipeline-tick.md` → "Why custody is not inferred, and
not yet proven".

### Retained scratch and harvest evidence

Every tick emits `retained_scratch` for an `issue-*` or `pr-*` directory that no
longer has a live engine. Redispatch never deletes that state first: it copies the
known engine artifacts and git diagnostic snapshots to:

```text
.claude/worktrees/.harvest-evidence/<kind>-<number>-<dispatch-id>/
```

Markerless interrupted-harvest directories use the suffix `retained-markerless`;
later incidents for the same item receive a numeric suffix instead of overwriting
the first bundle. Every completed archive moves the full original directory under
`scratch/`, preserving untracked or unknown recovery files as well as the copied
top-level evidence. Archives include implementer/fixer `result.json` and reviewer
`review.json` plus `completion.json` when those artifacts exist.
The shared archive finalizer immediately runs `git worktree prune` after that move,
so terminal harvest and stale/markerless redispatch recovery all reclaim the linked
worktree metadata that would otherwise pin a SHA. `worktree_prune_failed` reports
deferred metadata garbage collection without undoing the already-safe archive move.
An evidence directory without `scratch/` is an incomplete pre-transition copy;
the original worktree remains authoritative until a later retry completes the move.
If archive creation, artifact copy, snapshot write, or the final scratch move fails,
the tick emits `archive_failed` and retains the original scratch. It makes no further
label transition; a terminal relabel that already succeeded remains authoritative.
Do not manually delete the source directory until the archive contains the evidence
needed for the incident.

A dispatch-bound failed reviewer completion is not a retry signal. HARVEST archives
its completion evidence, relabels the PR from `ai:reviewing` to
`ai:needs-human-decision`, then finalizes scratch cleanup; it must not be redispatched.
On archive failure, the in-flight label and scratch remain authoritative. On relabel
failure, the evidence archive and scratch remain in pipeline custody so a later tick
can retry the terminal transition without launching another reviewer.

An unreadable or unclassifiable marker is never deleted because the engine's
identity is uncertain. If the item is selected from a pickup label, dispatch moves
it to `ai:needs-human-decision` and retains the marker for inspection instead of
skipping it forever. That judgement is stage-scoped: one item has one scratch
directory, so a fix-stage dispatch can find a reviewer's marker there. A marker
that is structurally valid for the item but records another stage is that
stage's custody — dispatch skips and logs it, and only a marker this stage could
have written is treated as corrupt. Stale in-flight labels without a local marker are handled
separately and never relabelled — see
[Markerless in-flight items](#markerless-in-flight-items--why-the-tick-leaves-them-alone).
The sweep point-reads each item only to log an accurate `stale_label_retained`;
a failed lookup or malformed response leaves the label unchanged either way.

## How the three surfaces fit together

```
                    ┌─────────────────────────┐
                    │     status.sh (watch)    │  ← "what's happening right now?"
                    │  IN-FLIGHT | QUEUE | LOG │
                    └──────────┬──────────────┘
                               │ reads (read-only)
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
    ┌───────────────┐  ┌──────────────┐  ┌──────────────────┐
    │ dispatch.json │  │ GitHub labels │  │ .tick-log/*.jsonl│
    │ (per-engine)  │  │ (per-PR)      │  │ (per-tick audit) │
    └───────────────┘  └──────────────┘  └──────────────────┘
            │                  │                  │
            │  "is PID 12345   │  "PR #468 is     │  "at 14:32, the tick
            │   still alive?   │   ai:reviewing"  │   dispatched claude-ds
            │   what engine?"  │                  │   for issue #439"
            │                  │                  │
            └──────────────────┴──────────────────┘
                     authority: HARVEST + DISPATCH
```

## Quick reference: which surface for which question

| Question | Look at |
|---|---|
| "What's running right now?" | Dashboard → IN-FLIGHT ENGINES |
| "What's in the queue?" | Dashboard → PIPELINE STATE |
| "Why was issue #439 skipped?" | Tick log → `jq 'select(.pr==439)'` |
| "Is the claude-ds engine alive?" | Dashboard → find row with `claude-ds` engine, check `alive` |
| "How many times have I used claude-ds vs claude?" | Tick log → group by engine |
| "What did the implementer produce?" | Worktree → `result.json` |
| "What errors did the engine hit?" | Tick log → `jq 'select(.pr==N)'` |
| "What did the reviewer find?" | Worktree → `review.json` |
| "Why did the reviewer reject?" | Worktree → `review.json` → `.verdict` field |
| "Which reviewer skill files were observed?" | Tick log → `reviewer_skill_observation` (`observed_skills`) |
| "How many fix iterations has this PR gone through?" | Dashboard (iteration in label) or `gh pr view` |
| "Did a dispatch fail to launch?" | Tick log → `select(.action|startswith("skip_"))` |
| "Is the pipeline currently quiescent?" | Dashboard → IN-FLIGHT ENGINES shows `(none)`, state shows all terminal labels |

## Worked example

You run `uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py '#439'` with issue #439 at `ai:available`.

**Dashboard** (after dispatch):
```
IN-FLIGHT ENGINES
  PR#439   implement claude-ds pid=18234  alive  working     55m-to-deadline
```
→ Engine `claude-ds` launched, PID 18234, still running, 55 minutes before timeout.

**Tick log** entry:
```json
{"ts":1748395200,"pr":439,"phase":"dispatch","stage":"implement","action":"launch","reason":"impl-1748395200-18234-439","engine":"claude-ds"}
```

**Marker** (`issue-439/dispatch.json`):
```json
{"engine":"claude-ds","stage":"implement","dispatch_id":"impl-1748395200-18234-439","pid":18234,...}
```

**Later, when the engine finishes and the next tick harvests:**

Dashboard IN-FLIGHT ENGINES goes to `(none)`.

Tick log gets:
```json
{"ts":1748398800,"pr":439,"phase":"harvest","stage":"implement","action":"harvest_complete","reason":"IMPLEMENTATION_COMPLETE sentinel found","engine":""}
```

GitHub label moves: `ai:implementing` → PR created with `ai:implemented`.
The completed `issue-439` worktree is cleared. Reviewer dispatch creates a fresh
`pr-<new-PR-number>` worktree so implement-stage marker custody cannot leak into the
review stage.

The next tick's DISPATCH phase will see the new PR at `ai:implemented` and dispatch
a reviewer (Codex).

## Design principles

| Design decision | Established principle |
|---|---|
| Labels are the source of truth; every tick re-reads them | Level-triggered reconciliation (a missed tick loses nothing) |
| Advisory nudges end the loop's sleep early; the tick still re-reads everything | Edge-triggered wake-up, level-triggered processing (the informer pattern: events affect *when* you reconcile, never *what* you conclude) |
| HARVEST then DISPATCH, unconditionally | Observe-then-act reconcile ordering |
| Lease-before-label; marker survives a failed label edit | Write-ahead intent: record intent before acting so a crash mid-operation is recoverable |
| Fail closed on every external read and malformed response; one corrupt marker blocks all dispatch | Crash-only design: prefer doing nothing over acting on garbage state |
| `MAX_IN_FLIGHT` counts this machine's dispatch markers | Admission control bounding local spend, not shared workflow state |
| Structured per-decision tick log | Reconcile audit trail |
| Detached engines + identity-verified PID supervision | Orphan supervision with PID-reuse protection (the cost of a crash-safe ephemeral ticker) |

## Known limitations

### Resolved (Python extraction)

1. ~~**No single continuous shell.**~~ **RESOLVED.** The orchestrator is now a single
   Python process (`lib/tick.py`). Variables are normal Python locals that live for
   the duration of `main()`. No state re-establishment between blocks.

2. ~~**UV cache cross-contamination.**~~ **RESOLVED.** 47 `uv run python` calls per
   tick reduced to 1. `pipeline.py` and `process_control.py` are imported directly
   — no subprocess round-trips for decision logic.

3. ~~**Shell dialect mismatch.**~~ **RESOLVED.** No bash at all. `pathlib` for globs,
   `json.loads()` for parsing, Python iteration — no `shopt` vs zsh, no `for` vs
   `while read`.

4. ~~**Harvest side effects deferred.**~~ **RESOLVED.** All three `_harvest_one_*`
   functions are fully implemented:
   - Implementer: reads `result.json`, creates PR via `gh pr create`, clears its
     completed `issue-N` scratch, and transitions labels. Reviewer dispatch owns a
     fresh `pr-PR` worktree.
   - Reviewer: reads `review.json`, detects base/head drift, posts review comment,
     enforces idempotent replay, transitions labels.
   - Fixer: reads `result.json`, posts the fixer-run comment (head before → after,
     outcome, bounded engine excerpt, and — on an escalation — the escalation
     reason), transitions to `ai:revised`, bumps the iteration cache. The post
     gates the relabel, and a **controller-owned** receipt under
     `WT_ROOT/.receipts/`, keyed to the run's `dispatch_id` *and* the decision it
     published, keeps a retried harvest from double-posting without letting a
     retry that decides differently hide behind the old comment. The receipt is
     deliberately *not* stored in the scratch: that is a checked-out worktree of
     the PR branch, so a tracked file at the receipt path would forge the
     "comment landed" proof.
   Each function fails closed — a `gh`/`git` error retains the marker for the
   next tick. The exceptions are the two best-effort posts listed above (the
   attempt-cap escalation and the attempt-counter write failure), which do not
   block their own relabel. Every terminal transition — the attempt cap,
   transient exhaustion, and a result-driven stage escalation — records a typed
   terminal intent under `WT_ROOT/.terminal/` before any remote mutation,
   because each publishes a comment claiming to supersede the run's own
   outcome. The next tick resumes that intent, so a failed relabel or a failed
   final archive move cannot be quietly replaced by the run's own label. If a
   workflow hold has taken the label in the meantime, the resume releases only
   the pipeline's own in-flight label (`terminal_relabel_held`) rather than
   re-attempting a converge the hold makes impossible. The reviewer's own
   escalation is included even though it posts nothing: it converges to a hold
   label, so an interrupted transition would leave a completed marker under a
   hold the pipeline set itself, and the held-completion notice the next tick
   would publish there says a hold was observed while the engine ran.
