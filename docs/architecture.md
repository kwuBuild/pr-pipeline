# PR Pipeline Architecture

This is the architecture reference for the pipeline: what the control plane is,
which component owns each decision, and *why* each boundary sits where it does.
It is the doc to read before changing dispatch, harvest, custody, or cadence.

It is not the operator manual. For invocation, flags, and troubleshooting see
[`skills/tick/README.md`](../skills/tick/README.md) and
[`DEBUG.md`](../skills/tick/DEBUG.md); for the internal call-site
map see [`docs/working/pr-pipeline-internals.md`](working/pr-pipeline-internals.md).

## Problem Statement

At 5+ concurrent PRs on a repository, the implement → review → fix → re-review
loop requires constant manual relay between agents. The developer acts as a
human message bus: finding which worktree an implementer worked in, copying
review output to a fixer session, tracking which PRs need re-review, and
deciding what to dispatch next. This babysitting overhead scales linearly with
concurrent PR count and displaces higher-value work (plan review, architecture
decisions, product direction).

The core bottleneck is not pipeline architecture — it is the handoff between
implementer and reviewer agents. Each handoff requires the developer to:

1. Locate the worktree and determine the current state
2. Gather context (diff, review comments, issue requirements)
3. Assemble a prompt and dispatch the right engine
4. Wait for completion, parse the result, and transition to the next stage

## Goals

- Eliminate manual relay between implement, review, and fix stages
- Support 5+ concurrent PRs without proportional developer attention
- Developer only reviews PRs that reach `ai:ready-for-human`
- Drive ticks on a state-aware cadence: poll fast while engines are
  in-flight, back off when idle, and stop at quiescence
- Use cross-model review (Claude implements, Codex reviews) for independence

## Non-Goals

- Multi-repo orchestration (one tick drives one target repository)
- Autonomous merging (human merge gate always)
- Daemon mode (manual tick + `/loop` only)
- CAS-style claim logic (single orchestrator)
- CI integration (read `gh pr checks` but don't replace CI)

## Architecture

### Two-phase tick model

Each tick runs two fast phases. Engines run *between* ticks, not during them.

```
┌──────────────────────────────────────────────────┐
│  /pr-pipeline:tick  (dynamic self-paced loop)    │
│                                                  │
│  HARVEST phase:                                  │
│    For each .claude/worktrees/{issue,pr}-*/             │
│      If a result file exists (result.json /      │
│      review.json, per the dispatched engine):    │
│        → read verdict (typed JSON field)         │
│        → post the outcome comment (gh)           │
│        → transition GitHub label (gh)            │
│        → bump iteration counter if fix stage     │
│        → clean up dispatch marker                │
│                                                  │
│  DISPATCH phase:                                 │
│    gh issue list --label ai:available            │
│    gh pr list --label ai:implemented,...          │
│    For each item, up to MAX_IN_FLIGHT:           │
│      → ensure worktree exists                    │
│      → write prompt to file                      │
│      → launch engine as background process       │
│      → write dispatch.json marker                │
│                                                  │
│  Print summary table                             │
└──────────────────────────────────────────────────┘
            │                          │
  ┌─────────▼──────────┐    ┌─────────▼──────────┐
  │ claude -p           │    │ codex exec         │
  │ (implement/fix)     │    │ (review)           │
  │ cwd: worktree       │    │ -C worktree        │
  │ → result.json       │    │ → review.json      │
  └─────────────────────┘    └─────────────────────┘
```

This design keeps each tick under 30 seconds regardless of concurrent PR
count. Engines run as detached background processes writing results to files.
The next tick harvests those results.

### Loop and cadence

Because engines are detached background processes, the harness never notifies
the orchestrator when one finishes — the loop must poll. The cadence is
therefore load-bearing and is engineered, not fixed. At the end of every tick
the orchestrator computes its next wake from in-flight state and schedules it
(dynamic-mode `/loop` / `ScheduleWakeup`). Two cache-aware bands only, never
300s (the prompt-cache TTL breakpoint that pays a cache miss without
amortizing it):

| State at end of tick | Next wake |
|---|---|
| ≥1 engine in-flight (live dispatch, no result) | `min(nearest actionable deadline, 270s)` — cache-warm |
| No engine in-flight, actionable queue non-empty | ~270s |
| At `MAX_IN_FLIGHT` with work in-flight | ~270s (harvest to free a slot) |
| Quiescent: no live dispatch, all items terminal | ~1800s heartbeat |
| Quiescent for N consecutive heartbeats | stop + notify developer |

In in-process `--loop` mode these intervals are an upper bound rather than a
fixed schedule. Because every tick re-reads full state, an early wake cannot
change what a tick concludes — only when it runs — so the loop waits on a wake
channel that an advisory watcher can signal on engine-completion evidence, an
operator/external `wake-request` file, or a detected engine stall — a `live`
implementer/fixer whose progress heartbeat has not advanced past its threshold.
The watcher holds no custody and fails open to the timer.

The first two are nudges and nothing more, so they exist only in `--loop`,
where there is a sleep to shorten. The stall source also *reports* to the
operator, so it is not scoped that way: every tick runs the same scan before
HARVEST, which is what makes the signal exist in the one-shot mode the skill
prescribes. The two callers share on-disk episode memory, serialized by its own
lock, so normal polls do not repeat one stall episode. A report attempt is
checkpointed before the scan advances; an exit before that post-report write
commits retries the attempt on the next scan instead of silently losing a real
stall. The macOS notification remains best effort. Process completion remains
group-aware: wrapper PID death
alone cannot nudge or release custody while a child in the recorded process
group is alive. If that group outlives its deadline after wrapper identity is
lost, the item escalates to a human while the marker and scratch retain custody
and continues to occupy a concurrency slot until group death can be proved. Such
a marker occupies a slot but no longer paces cadence: both cadence surfaces read
one actionable-deadline scan that counts only `live`/`launching` markers under
the same identity proof harvest and admission use. The same exclusion covers an
identity-matched `hung` engine whose termination failed: retrying `EPERM` or an
already-delivered `SIGKILL` at 1s cannot accelerate death, and the wake watcher
nudges within 5s once both PID and group are gone. A fixed past deadline therefore
cannot pin the loop to the overdue floor. A
nudge observes a 10s floor unless the already-scheduled cadence deadline
arrives first, so the cadence decision remains an upper bound. Only ticks that
followed a full-interval wait count toward the quiescent stop, so the heartbeat
contract above is unchanged. That gate applies to the persisted quiescent
streak as well as the loop's in-memory one — the streak is shared with the
one-shot path, so it has to keep measuring quiet time rather than tick count.
The harness `/loop` + `ScheduleWakeup` path has no in-process sleep to interrupt
and stays purely timer-paced.

Concurrency is bounded by `MAX_IN_FLIGHT` (default 3): the dispatch phase never
launches beyond it, and the cadence tightens to ~270s while saturated so slots
free promptly. Quiescence is the loop's fixed point — every tracked item in a
terminal label (`ai:ready-for-human`, `ai:needs-human-decision`,
`ai:max-iterations`, merged, or closed) with no live dispatch — at which point
the loop heartbeats slowly and then stops.

Admission counts **local dispatch markers only** — those classified `live`,
`launching`, or `hung`, or whose process group is still alive — keyed by
`(kind, number, stage label)`. An authoritative `ai:` in-flight label is shared
workflow state, not a pipeline lease: `ai:implementing` is also the documented
manual pickup label, so counting it let stale hand-set labels starve dispatch
permanently. The cap bounds the engines *this* machine launched
and can still see; a corrupt or unreadable marker still aborts dispatch for the
tick. Kind and stage are load-bearing: issue `#42` and PR `#42` are different
processes, as are accidentally overlapping review and fix dispatches for one PR.

The cost of dropping the union is the cross-machine sum: another host's engine
no longer consumes this host's budget. Per-item exclusion is unaffected, because
the label was never an acquisition lock — see the lease → launch → relabel step
under "File-based handoff".

Loop supervision is intentionally narrower than admission. An occupied item
carrying `ai:needs-human-decision` or `ai:blocked` still consumes its admission
slot, but is excluded from cadence when — and only when — the pipeline has no
pending action on it. That question is answered by *liveness*, not by the
classify state: while the recorded process or its group is alive, the pipeline
still owes the item a harvest, an archive, or a kill, and it is the only party
that can perform any of them. `_pipeline_actionable` carries that bound in one
predicate — alive, and for a `hung` marker only when identity proved `matched` —
and every held shape outside it is discounted, so an unactionable shape added
later needs no new rule. The ones that exist today: an overdue engine whose
identity cannot be proven is alive but unactionable, because the
escalate-and-leave contract forbids signalling a PID that may have been reused;
a pre-spawn lease is never alive and never becomes so, since it records no PID
and `classify_dispatch` pins it at `launching` without consulting the deadline,
so counting it would keep the loop awake permanently; and a markerless hold has
no process to reach at all.

Published, not harvested: the hold forbids advancing the stage, so a result that
finishes under one cannot advance workflow. After process-group death, the tick
prepares evidence, publishes the validated engine outcome, releases only its
in-flight label, and finalizes the archive. `held_result_published` distinguishes
that sequence from an empty-scratch reclaim; a failed post retains all source
custody and logs `held_result_comment_failed` for retry.
The tick resolves holds with one label read per occupied identity, so open and
terminal items share one contract without a `--state all` scan. The cost tracks
occupancy, and occupancy is this machine's dispatch markers alone: manual
`ai:implementing` pickups and the markerless labels reconciliation
deliberately leaves in place hold no slot and are never read here, so the bound is the live marker set
that admission already gates at `MAX_IN_FLIGHT`. No local *custody* receipt is
written. The completed-result publication
uses a short-lived comment receipt for replay suppression, but ownership remains
the remote hold plus the source marker until release succeeds. A failed hold read
cannot prove quiescence and keeps the loop warm.

The hold suspends automation custody; it does not transfer the pipeline's own
bookkeeping to the human. When the recorded process group is proven dead, the
tick releases both the scratch and its in-flight label — the label is an
automation receipt, and retaining it past proven death would strand the item at
a pipeline label no tick clears. It would cost no spend cap: destroying
the marker is what returns the slot. The release is ordered before the
archive so a failed remote mutation leaves the marker behind to re-prove death.
The hold label itself is never a target of any mutation.

Tick *duration* (kept under 30s above) is not what bounds per-PR latency; the
inter-tick *interval* is. Engineering the interval, not just the tick speed, is
what makes the pipeline converge quickly without over-polling.

### State machine

GitHub labels are the source of truth for *pipeline stage* and workflow custody.
Local dispatch markers and result files are evidence used to reconcile a paid
process; they never override a human hold. Controller-owned publication receipts
and terminal intents under the worktree root support replay of already-decided
side effects, but they do not authorize engine custody. A lost or recreated item
worktree therefore cannot forge either a stage transition or a human handoff.

The label vocabulary lives in `docs/working/pr-guidelines.md`. It reuses the
labels a human workflow already carries wherever one means the right thing, and
introduces new labels only where automation needs a distinction the human
workflow does not.

- **Reused as-is:** `ai:available`, `ai:implementing`, `ai:implemented`,
  `ai:changes-requested`, `ai:revised` (fixes pushed, awaiting re-review),
  `ai:ready-for-human`.
- **New, introduced by this design:** `ai:reviewing` and `ai:fixing` (explicit
  "engine dispatched" states — the human convention treats this transient work
  as happening *between* labels, but the orchestrator must see that an engine is
  in-flight); `ai:needs-human-decision` and `ai:max-iterations` (escalation
  terminals with no human-workflow equivalent).

Adopting this design means creating the four new labels and adding them (plus
their transitions) to `pr-guidelines.md` so the two documents stay in sync.

**Issue states (implementation stage):**

| Label | Meaning | Tick action |
|---|---|---|
| `ai:available` | Issue ready for implementation | Dispatch implementer |
| `ai:implementing` | Implementer dispatched | Wait for result |

**PR states (review/fix loop):**

| Label | Meaning | Tick action |
|---|---|---|
| `ai:implemented` | PR raised, awaiting review | Dispatch reviewer |
| `ai:reviewing` | Reviewer dispatched | Wait for result |
| `ai:changes-requested` | Review found blockers | Dispatch fixer |
| `ai:fixing` | Fixer dispatched | Wait for result |
| `ai:revised` | Fixes pushed, awaiting re-review | Dispatch reviewer |
| `ai:ready-for-human` | Passed AI review | Skip (developer reviews) |

**Escalation states:**

| Label | Meaning |
|---|---|
| `ai:needs-human-decision` | Scope/design decision beyond agent remit |
| `ai:max-iterations` | Review/fix cycles exceeded threshold (default 5) |

**Transitions:**

```
issue:ai:available
    ──[tick dispatches implementer]──▶ issue:ai:implementing
    ──[harvest: IMPLEMENTATION_COMPLETE]──▶ creates PR with ai:implemented

pr:ai:implemented
    ──[tick dispatches reviewer]──▶ pr:ai:reviewing

pr:ai:reviewing
    ──[harvest: NO_BLOCKERS]──▶ pr:ai:ready-for-human
    ──[harvest: BLOCKERS_FOUND]──▶ pr:ai:changes-requested
    ──[harvest: base or head drifted]──▶ pr:ai:implemented|ai:revised   (discard + re-dispatch)
    ──[dead reviewer + dispatch-bound failure envelope]──▶ pr:ai:needs-human-decision   (archive + terminal route; no redispatch)
    ──[dead PID, no result]──▶ pr:ai:implemented|ai:revised   (revert to pre_label + re-dispatch)
    ──[ai:reviewing with missing/corrupt marker]──▶ pr:ai:reviewing   (retained; no proof of custody)

pr:ai:changes-requested
    ──[tick dispatches fixer]──▶ pr:ai:fixing

pr:ai:fixing
    ──[harvest: FIXES_PUSHED, head advanced]──▶ pr:ai:revised
    ──[harvest: FIXES_PUSHED, head unchanged]──▶ pr:ai:needs-human-decision  (contradictory claim)
    ──[harvest: NEEDS_HUMAN]──▶ pr:ai:needs-human-decision
    ──[dead PID, no result]──▶ pr:ai:changes-requested   (revert + re-dispatch)

pr:ai:revised
    ──[tick dispatches reviewer]──▶ pr:ai:reviewing (loops back)
```

Harvest is **label-driven**, reconciling each PR from both its label and its
marker rather than iterating worktree markers alone. The in-flight labels
(`ai:reviewing`, `ai:fixing`) are deliberately *not* in the dispatch queue's
pickup set, so a process that dies after the label flips would otherwise strand
the PR there permanently; the stale-dispatch recovery below reverts each
in-flight label to its pre-dispatch label so the dispatch phase re-queues it on
the next tick. Because the marker is disposable and the label authoritative,
harvest reconciles every PR in `{labelled ai:reviewing} ∪ {worktree with a
dispatch marker}` and folds the current label into a single tested decision
(`reconcile_dispatch`):

- An `ai:reviewing` PR whose marker is missing or corrupt is invisible to a
  marker-only sweep and is never re-selected by dispatch. It is **not** escalated
  on that basis: markers are machine-local, so a missing one is equally
  consistent with another machine's live dispatch and with a human's manual
  pickup, and escalating on it mass-relabelled healthy work. Reconciliation
  leaves every markerless item alone; the `pr-pipeline:status` dashboard lists
  every open PR and issue by `ai:` label, so a genuinely stranded item is
  visible there for the operator to clear by hand. See
  [Why custody is not inferred, and not yet proven](#why-custody-is-not-inferred-and-not-yet-proven).
- A completed result is harvested whenever the PR is still in pipeline custody —
  `ai:reviewing`, **or** the recorded pre-dispatch label if the post-dispatch
  label edit failed. Without this, a result-gated dedup would let dispatch launch
  a *second* reviewer over the finished result. If a partial GitHub transition
  leaves multiple `ai:` labels, reconciliation derives custody from the marker's
  in-flight/pre-label pair rather than GitHub's label ordering — for all three
  stages (implement, review, fix), since every stage's harvest converges labels
  the same way. Harvest converges in one `gh edit` mutation (add the target,
  remove every other pipeline label) and verifies the exact remote set before
  deleting scratch; a failed mutation, a verified mismatch, or an unreadable
  confirmation read (logged unverified) all retain the marker for an idempotent
  retry — only a verified read can rule out a hold added concurrently with the
  mutation, and a transition that actually landed completes its bookkeeping
  through the already-converged early return on the next tick. Human holds (`ai:needs-human-decision`, `ai:blocked`)
  outrank the marker: the pipeline never produces `ai:blocked` and only pairs
  `ai:needs-human-decision` with an in-flight label when its own harvest target
  is that label, so seeing either next to pipeline custody means the hold is
  *treated as human-set*. It cannot be proven human-set: a dispatched implementer
  or fixer runs under the operator's full environment and shares the tick's
  GitHub token, so an engine-set label is indistinguishable from a human's down
  to the timeline actor. The engine prompts and `docs/working/pr-guidelines.md`
  ("Pipeline label authority") close the engine-set route by contract; the
  runtime rule stays **fail closed toward the human**, because a human racing to
  stop a bad run must win. Convergence toward any automation target therefore
  refuses to remove the hold, and a completed marker under a hold is *published,
  not harvested*: after process-group death the tick prepares evidence,
  publishes the validated engine outcome, releases only its in-flight label,
  and finalizes the archive — the exact hold is never mutated. Reconciliation
  does not converge a held item onto `ai:needs-human-decision` (or fold
  `ai:blocked` into it): the hold is already the human's own writing, and
  converging it would replace that writing with the pipeline's terminal label —
  the authority behavior that was removed. A live or hung engine with dual
  labels keeps custody of its scratch: live defers escalation until the engine
  exits and hung routes to the identity-verified kill path, so evidence is
  archived rather than deleted under a running process. Dispatch applies the
  same rule at pickup: a candidate carrying a hold label next to its
  pre-dispatch label is skipped (never launched), so a hold stops spend before
  it starts, not just at harvest. The pickup queries are label *filters*, not
  eligibility proofs, so before anything mutates — scratch, setup, or a paid
  launch — dispatch point-reads the candidate's complete label set and judges
  it with one pure predicate, `pipeline.resolve_pickup_eligibility`: no hold,
  no in-flight label, and **exactly one pickup label counted across every
  stage** for that entity, which must be the calling stage's. Counting within
  one stage is not enough: a PR at `ai:implemented` + `ai:changes-requested`
  shows review exactly one of its labels and fix exactly one of its own, so
  both would claim it. An in-flight label is a claim by *some* machine whose
  marker lives on that machine's disk, so a missing local marker cannot
  disprove it; since markerless in-flight items are no longer converged, a
  partial transition (in-flight label added, pickup label not yet removed)
  persists for the life of that engine and keeps matching the pickup query.
  Identities already in the tick's authoritative in-flight set are rejected
  before the read as a cheaper first layer — one list is shared by all three
  dispatchers and appended to on every launch, so it also carries what *this*
  tick already dispatched at an earlier stage — but the point-read is the one
  that holds when GitHub's search index lags the label write. Both cadence
  probes call the same predicate with no stage ("can *any* stage take this?"),
  so a queue of held, in-flight, or ambiguously labelled items reads as
  quiescent instead of keeping the loop awake over items dispatch will
  never take. The implementer's PR-creation success path
  converges its issue to no pipeline label through the same helper, so a
  partially applied dispatch transition (`ai:implementing` + `ai:available`)
  cannot leave the issue re-dispatchable after its PR exists. Any other sole label means the item has left pipeline custody — a human took over: the
  scratch is cleaned up and the label is never clobbered (a hung process is
  still killed so it cannot pin the cadence). Correspondingly, the dispatch
  dedup blocks on **any** existing marker, not only a result-less one, so
  HARVEST is the sole owner of a marker's lifecycle.

**Publication ordering.** Every terminal label transition that *reports an engine
result, or fires before one was launched* publishes its explanation before the
label moves — with two documented exceptions and the open gaps enumerated below —
and the post gates the transition: a failed post leaves the label untouched, so
the next tick retries both. What it retains differs by path: the harvest-side
transitions keep their marker and scratch, the ambiguous-marker path keeps the
scratch it could not prove safe to clear, and the setup-failure path removes it —
no engine ran, and a retained lease would be reconciled to the terminal label
before the retry could run. Posting after the relabel would leave a window where
the item shows a new — possibly terminal — label with no stated reason, which is
the audit gap the comment exists to close. This holds for the reviewer verdict
comment, for both terminal fixer outcomes (`recheck` → `ai:revised`, `escalate` →
`ai:needs-human-decision`), for the transient path's exhaustion escalation, for a
hold found beside a completed result — whose gated mutation is the release of the
pipeline's own in-flight label rather than a move to a terminal one — and for
every pre-launch dispatch escalation. A transient `retry` that reverts to the
pre-dispatch label posts nothing — no outcome has been decided yet.

The scope is load-bearing, not hedging. `ai:needs-human-decision` is still
reachable from transitions the gate does not cover — enumerated under **Silent
escalations** below — and stating the rule unscoped is what kept those paths
undocumented: a reader checking the invariant against the reviewer and fixer
paths finds it holds and stops looking. What the two clauses leave out is the
middle: an engine was launched, and the tick cannot read a result from it.

The gate holds wherever a failed post can be *bounded*. Transient exhaustion is
the case that makes this explicit: a failed post there discards the prepared
archive and burns a durable attempt, so repeated failure terminates at the
attempt cap instead of retrying the post forever behind a pinned admission slot.

The held completion is bounded by the hold instead of by a counter. It burns no
attempt, so a post that never lands keeps its retained marker — and therefore
its admission slot — indefinitely, with the in-flight label alongside it costing
nothing. That is sound only because the item is already awaiting a human
by construction — the hold is the reason the result is not being harvested — so
the operator resolving it is the release, and the next tick harvests the
completion through the ordinary path. The alternative, releasing custody on a
failed post, is precisely the silent archival this gate was added to end.

The pre-launch dispatch escalations are bounded against the same durable counter,
and they show why gating alone is not enough. Nothing is spent when one fires —
no engine, no admission slot, no marker — so a failed post costs only a held
label and the item waits at its ordinary pickup label. That is precisely the
hazard: an unpostable comment would leave a broken item looking like available
work on every future tick, forever. Burning an attempt per failed post makes the
loop terminate, at which point the label lands unexplained under the same
exemption the attempt cap takes. The counter is the dispatching *stage*'s, not
the entity's, so it is the file that stage's harvest cap reads.

What the landed escalation then retires depends on whether it kept the marker.
The counter is per item, not per purpose, so on the ambiguous-marker path — which
leaves the marker on disk by definition — a mid-retry harvest may be counting
into the same file, and clearing would reset its budget and delay its own
cap-based cleanup of the scratch just retained. That path therefore leaves both
the counter and the harvest's receipt alone, and only the setup-failure path,
which retains nothing, clears.

**Two documented exceptions**, both where nothing is left to bound the retry:

- The **attempt cap** exists because this item's remote writes keep failing, so
  gating its terminal label on one more of them would pin the admission slot
  forever — the outcome the cap was built to prevent.
- The transient path's **attempt-counter write failure** escalates precisely
  because the retry budget could not be persisted; a gated transition would have
  no counter to terminate it.

Both posts are therefore best-effort: the label lands either way and a failure is
logged as `escalation_comment_failed`.

**Silent escalations, stated by complement.** The publishing surface is small and
closed. Exactly two functions in `tick.py` run `gh <entity> comment`:

- `_harvest_one_reviewer` posts the reviewer's verdict directly and gates the
  relabel on it. This one carries no receipt, unlike every other post: if the
  post lands and the relabel then fails, the harvest retains the scratch and the
  next tick reposts the same verdict. The cost is a duplicate comment rather than
  a wrong label, which is why it has not been receipted — but it is the one
  publishing path where at-least-once is unbounded rather than bounded to one
  extra.
- `_post_harvest_comment` posts everything else, under one of four receipted
  kinds: `fix-comment` (fixer `recheck` and `escalate`), `cap-escalation` (the
  attempt cap), `transient-escalation` (exhaustion and the counter-write
  failure), and `dispatch-escalation` (every pre-launch dispatch escalation).

**Every other terminal transition publishes nothing.**

That is the durable form of the rule. The silent set was enumerated three times
and undercounted three times — first totalled as four transitions, then as six,
then hand-named as four — because it is large, spread across reconcile, harvest,
and dispatch, and was maintained by hand in three documents at once. Bounding the
publishing side instead is checkable against the source in one pass, so the
complement cannot drift: a new silent path leaves the statement true, and a new
publishing site fails `test_tick_publishes_from_only_two_places`.

The escalating side is pinned the other way round, by census:
`test_every_needs_human_transition_is_a_recorded_decision` enumerates every
function that can relabel to `ai:needs-human-decision` and how many such sites it
owns. A new route to the hold label fails until someone records whether it
explains itself, which is the question the previously-silent paths were never
asked.

That census fails *closed*, and has to. Recognising only
`pipeline.LABEL_NEEDS_HUMAN` would have missed both reconcile escalations —
`_reconcile_one` passes the bare string, `_execute_reconcile_actions` a variable
proven equal by an enclosing guard — which is precisely the shape a new silent
path gets written in, since those two are the examples anyone would copy. So a
site passing a *computed* label counts even when it usually resolves to a normal
transition. A false positive costs one recorded line; a false negative is the
accumulating silence the census exists to stop.

Only the *target* label position is read, though, or the classifier would fold
in `from_label` and count every ordinary in-flight relabel. The entries divide
accordingly: sites that provably name the hold label, and sites that pass a
computed one and are counted because the walk cannot rule it out. The second
group includes transitions that are the *opposite* of an escalation —
`_publish_marker_or_revert` reverts to the pre-dispatch label after a confirmed
engine death — so a count there is not evidence of a new silent path.

It also pins the shape of the dispatch phase, where every pre-launch escalation
is funnelled through the two publishing helpers. The one provable site left in
each `_dispatch_one_*` is the post-launch guard, so a *second* provable site
there means a new silent pre-launch path.

**On the third named silent path.** Three silent escalations were originally
named: the dispatch-setup failure, the ambiguous marker, and the stale in-flight
label (`stale_label_escalated`). The first two are closed above. The third no
longer exists to close — `_reconcile_stale_in_flight_labels` now retains every
stale ref and never relabel, because a missing local marker is the steady-state
reading of another machine's live dispatch and cannot prove custody either way.
Its closure condition is therefore met by the two surviving paths plus the
four pre-launch escalations the census found while closing them.

What is left is one shape: an engine was launched, and no result reached the
tick. It divides by whether that engine is finished, which decides how to fix it:

- **It finished and still says nothing** — a genuine hole in the gate above,
  because the archive under `.harvest-evidence/` makes the item's only
  explanation machine-local. One member remains: `_harvest_failed_reviewer`,
  which escalates a wrapper-confirmed reviewer failure. Reconciliation's hold
  escalation over a completed marker was the other, and it has since moved into
  the gated set above — it now publishes its validated outcome before releasing
  the in-flight label. The reviewer path has the same shape, is tracked by
  nothing, and belongs with it.
- **It may still be running**: the reconcile escalations (hung engine,
  unverified live custody) and each stage's post-launch guard, which fires when
  the in-flight relabel failed *and* the engine could not be killed. These are
  not the same kind of gap. The label there is a duplicate-dispatch guard over
  custody the tick could not prove it holds, the marker is deliberately
  retained, and the run that engine is still in owns the explanation — so the
  fix, if there is one, belongs to whatever proves that custody, not to the
  publication gate.

Nothing prevents bounding the first group's posts: no admission slot is pinned
and no retry budget has been spent.

**Publication durability.** Replay safety is receipt-based, not comment-derived:
nothing counts fixer comments, so a `gh` read to detect a prior post would only
add a failure mode that stalls the harvest. Receipts live under `WT_ROOT`
alongside `.iter` and `.harvest-evidence`, never inside the item's scratch — a
fixer scratch is a checked-out worktree of the PR branch, so a tracked file at
the receipt path would forge the "comment landed" proof and let the label move
with nothing published. A receipt is keyed to the run's `dispatch_id` **and** to
the decision it published (target label plus rendered body), because the harvest
point-reads a fresh head and recomputes its decision on every retry: a
dispatch-only key would let a first attempt's escalation comment suppress the
repost while a later attempt lands `ai:revised` under it. The guarantee is
at-least-once — a crash between a landed post and the receipt write costs one
duplicate — because failing a landed post would be worse.

The pre-launch `dispatch-escalation` receipt is the one exception to the
`dispatch_id` half, and it inverts the argument above. It has no dispatch to key
to — it fires before a marker exists, or over one whose identity is what could
not be read — and a per-run id would be actively wrong, because every tick
re-attempts the *same* failed setup and would repost under a fresh id. It uses a
constant instead, leaving the decision key to carry the whole contract: the same
failure suppressed, a different one published. That makes it the only kind a
later dispatch cannot fail to match, so unlike the others it must be retired
explicitly rather than aged out by a changing id.

Every terminal transition — the attempt cap, transient exhaustion, or a
result-driven stage escalation — records its intent under `WT_ROOT` before any
remote mutation. Its comment states that `ai:needs-human-decision` supersedes
whatever the run itself concluded, so if the relabel or the final archive move
fails the decision has to outlive the tick: the next tick resumes that
transition instead of re-entering ordinary harvest logic, which would otherwise
republish the completed outcome as a held completion under the pipeline's own
terminal label. "Before any remote mutation" includes the stage comment, not
just the relabel: the fixer's run comment names the terminal label, so
publishing it ahead of the record would leave a live explanation with no
recovery owner. The intent is dispatch-bound and typed (`cap`,
`transient_exhausted`, `escalate`), and is retired only after the final archive
move lands — not when the relabel does.

The reviewer's own escalation records one even though it publishes nothing.
`decide_review_harvest` escalates an unvalidatable verdict, and a
changes-requested verdict carrying no blocking finding, with `post_comment`
false — so the comment history that makes the reviewer's other outcomes
idempotent is empty here, and there is nothing for a retry to recognise. Its
target is also a hold, so a converge that lands without its scratch cleanup
following it leaves a completed marker sitting under a hold *the pipeline
applied itself*. Ordinary harvest would then route that marker through the
held-completion publication, whose text says a workflow hold was observed while
the engine was running — false on both halves. Silence is why this transition
needs the record, not a reason it can go without one.

The record is controller state that drives a remote mutation, so it is read
back through a closed, kind-specific validator rather than field by field: one
snapshot, every field required and bounded to its own set (a second read of the
same path can disagree with the first, and a defaulted field would execute a
transition nobody decided). Anything the validator rejects — unreadable,
malformed, an unknown kind, a persisted label outside the terminal set — retains
custody for operator inspection instead of falling back to a default path, and
an intent is retired only against a *provable* mismatch: a marker whose dispatch
id cannot be read never retires one.

The relabel is re-attempted only from a state that proves it never ran. The
cheap proof is the phase the intent records once the relabel lands; without one
the live label set decides. Still carrying the stage's in-flight label means
convergence never happened, so it runs. Sitting exactly at the decided target
means it already did. Anything else — a third label, no label — belongs to a
later actor, and the resume finishes local custody without touching the labels;
converging there would strip a human's transition and re-apply a superseded
terminal decision. An unreadable label state proves neither and holds
everything.

One source state proves the relabel never ran *and* that it never can: the
in-flight label still present beside a workflow hold. Convergence refuses a hold
by design, so re-attempting it every tick would leave the in-flight label on the
item until the operator lifts the hold — stranding it at a label no tick clears
, though the destroyed marker means it consumes no slot, with no
engine behind it, which is the leak the release exists to prevent. The
transition instead makes the one mutation the hold permits: it releases the
pipeline's own in-flight label and finishes local custody, leaving the exact
hold untouched. This is the same custody rule the held-completion path applies,
reached from the other direction — there the hold is found beside a completed
result, here beside a transition already decided and published. The release is a
gate like any other remote mutation: a failed one retains the marker, the
attempts, the in-flight label, and the intent for the next tick. It carries the
same post-release hold read as the two cleanup releases, so a hold lifted
between the confirmed removal and the archive move puts the in-flight label
back rather than stranding the item with no workflow label — and the restored
label is what lets the following tick read the transition as pending and
converge it as originally decided. The published
comment still names the terminal label the transition decided while the item
carries the operator's hold; both are holds, so the explanation stays true to
what the item now is, and the alternative — a silently pinned slot — was the
worse trade.

The intent write is a required gate — a transition whose decision cannot be
durably recorded is not started at all, because one that starts without a record
and then fails its archive move leaves the next tick with nothing to resume.

**Iteration counter.** The fix-iteration count is the runaway-spend safety
valve, so it must survive worktree loss. Its durable source is GitHub-visible:
each completed review round posts exactly one pipeline-authored review comment,
so the count is `gh pr view <N> --json comments` filtered to the pipeline's
review-comment marker (a fixed header string the reviewer harvest always emits).
`iter.txt` in the worktree is a *cache* of that count for fast reads; if it is
missing or the worktree was recreated, the harvest reconstructs it from the PR
comment history before bumping. Crossing the threshold (default 5) routes to
`ai:max-iterations` instead of `ai:revised`. Because the authority is the PR
itself, a cleaned-up or moved worktree cannot silently reset the counter and let
the loop keep spending.

The comment-derived count has one blind spot the fixer harvest must close: a
round whose comment is replay-suppressed does not increment it. If a fixer
claimed `FIXES_PUSHED` without actually advancing the head, the next review
round would carry an identity identical to the last one — the idempotency token
suppresses its comment, the count freezes below the threshold, and the
review/fix loop would spend forever without reaching `ai:max-iterations`. The
fixer harvest therefore verifies the claim before relabeling: it point-reads
the PR's live head and compares it against the marker's dispatch-pinned
`head_sha`. A fast-forward push always advances the head and force-push is not
permitted, so `FIXES_PUSHED` with an unchanged head is contradictory output and
escalates to `ai:needs-human-decision` (evidence archived); a failed or
malformed point-read retains the marker for the next tick. Markers without a
pinned head (pre-upgrade) skip the check — no contradiction can be proven
without dispatch-time provenance.

### Why custody is not inferred, and not yet proven

**Decision (2026-08-02): reconciliation never acts on a
markerless in-flight item, and the pipeline does not attempt to prove custody
at all. Revisit only when a genuine orphan is observed.**

The original rule inferred custody from the *absence* of a machine-local
dispatch marker: an item at an in-flight label with no marker was declared
stranded and escalated to `ai:needs-human-decision`. That inference is invalid
on this control plane. Markers live on local disk, so no tick can ever see
another machine's, and `CLAUDE.md` instructs contributors to move an issue to
`ai:implementing` by hand before writing code. "No marker" is therefore the
steady-state reading of healthy work, not evidence of an orphan.

It fired accordingly. On 2026-08-01 it escalated two issues that were both
being implemented normally — one of them opened its PR two minutes later. Across the
entire tick log to that date the path had recovered **zero** genuine orphans
and produced **two** false positives. Every `escalate` and `cleanup` event
actually recorded was semantic — an engine emitting `NEEDS_HUMAN_DECISION`, or
dying on an expired credential — not a custody event.

The cost is not the two relabels. Every custody escalation in the tick routes to
`ai:needs-human-decision` — `ai:max-iterations` is reachable only from the
reviewer's iteration cap — so that one label carries the whole stop channel; a
signal that fires on healthy work stops being believed, and then it cannot stop
anything.

**Why the fix is a no-op rather than a better proof.** A GitHub-side dispatch
receipt was designed and built once: dispatch posts a
comment carrying the dispatch id, stage, host, and deadline, and reconciliation
binds it to the in-flight label applied within the next 120 seconds. It was cut
before merge. An ordinary issue comment is not an authenticated custody record
— any actor can write the marker, comment bodies are editable so `created_at`
does not prove when the payload appeared, and timestamp proximity across two
independently-consistent endpoints is not a safe mutation precondition. That
design replaces "no proof of custody" with "forgeable proof of custody" and
rebuilds the destructive path on top of it. Making it sound needs a distinct
signed writer (a dedicated GitHub App), a strict receipt schema, an immutable
event identity, and a label epoch — real work, justified by no observed
failure.

**The open-closing-PR exception went with it.** The stale-label sweep used to
keep one mutating branch: a markerless `ai:implementing` issue had its label
removed once `gh search prs --state open "Closes #N"` returned a hit. That is
the same invalid inference in a thinner disguise. The search is full-text, so it
matches drafts, PRs under active revision, and PRs that merely quote the string
— none of which mean implementation stopped. And the branch removed a label
without adding one, leaving the issue with no pipeline label at all, in breach
of the one-pipeline-label-per-item rule the rest of the control plane depends
on. The sweep now has no mutating branch; its only GitHub call is a point-read
used to log an accurate retention.

**What is given up.** An engine that dies without writing a result leaves its
item at an in-flight label indefinitely. Nothing reaps it automatically. It is
visible in `pr-pipeline:status` (in-flight label, no live engine) and the
operator clears it with one `gh issue edit`. At the observed rate of zero, that
is the cheaper side of the trade.

**Reopen the question when** a genuine orphan appears in the tick log — an item
whose dispatch this pipeline made, whose engine is provably gone, and which no
one noticed on the dashboard. Three of those in a month is a real signal, and
by then the log says what the receipt would actually have to prove. Until then,
the correct posture is: act only on proof you hold, and surface the rest.

Note that a *valid* marker is proof, and is still acted on. A `launching` lease
seen at reconcile (the spawn crashed between starting the engine and publishing
its PID) escalates to `ai:needs-human-decision`, because a paid engine may be
alive and unkillable — money burning is a genuine call for the operator, not
lost bookkeeping.

### Engine dispatch details

**Implementer (Claude):**

Claude supplies a read/write implementation engine while the controller retains
GitHub metadata and workflow ownership. Its target repository supplies coding
and verification instructions; the operator supplies permission grants.

The [operator engine options](../skills/tick/README.md#engine-selection---impl-engine)
own route selection, credentials, and overrides. The
[internals launch summary](working/pr-pipeline-internals.md#1-engine-launch-summary)
and [model resolution](working/pr-pipeline-internals.md#2-model-and-reasoning-effort)
own the current launch behavior and source references. Follow those owners
rather than copying a subprocess command here: wrapper capture, exact push
grants, and environment construction are part of the runtime contract.

**Git surface and remote blast radius.** The explicit grants include commit
and one exact stage-specific push: `git push <remote> HEAD` for the implementer,
`git push <remote> HEAD:refs/heads/<head-ref>` for the detached fixer. The remote
and head ref are validated before launch. No wildcard push grant is added.

Claude engines default to `auto`, resolved through the packaged floor, operator
`[defaults]`, per-repository settings, and finally the CLI. `dontAsk` remains an
explicit fallback for routes that fail setup's probe. Under `dontAsk`, calls
that would prompt are denied. Under `auto`, calls outside the pre-approved
list can reach the classifier, including `gh`, force-push, remote-branch deletion,
and arbitrary refspec pushes. The allowlist is not a ceiling in that mode;
operator-owned deny rules must forbid operations that must never run. Prompts
assign GitHub metadata writes to the controller, but are not enforcement.

A classifier denial can still fail verification and the run. Tick does not probe
or silently change the selected mode; setup (#18) owns measuring route behavior
and recording any fallback/refusal. CLI acceptance alone is not evidence of
provider capability. See [engine permission modes](../skills/tick/README.md#engine-permission-modes),
including why Codex's `--approve-for-me` is not applied to the read-only reviewer.

**Reviewer (Codex):**

The reviewer engine is **bare `codex exec` — not `codex review` and not the
`codex exec review` subcommand.** The structured review subcommands are the
wrong tool for an unattended loop, verified live against the pinned CLI
(`codex-cli 0.142.5`): `--base <BRANCH>` is mutually exclusive with a custom
`[PROMPT]` (`the argument '--base <BRANCH>' cannot be used with '[PROMPT]'`),
and the review subcommand renders prose into every output channel and ignores
`--output-schema`, so its result can never validate against
`review-schema.json` — the harvest gate would reject every review. The
implementer/fixer **commit and push** before review, so the diff is committed —
scoping it forces `--base`, which then forbids the prompt, which leaves the
verdict recoverable only from prose or an undocumented exit code. A promote-to-
human decision cannot rest on prose-scraping.

Bare `codex exec` removes the conflict entirely. It takes a full prompt **and**
honors `--output-schema` for the final message, so the reviewer scopes the diff
itself (`git diff <base-sha>...HEAD`, with the base SHA carried in the prompt)
and returns a typed verdict — no `--base` flag, no sentinel-in-prose recovery.
Verified flags on `codex-cli 0.142.5` (the version floor preflight and dispatch
enforce):

- `--output-schema <FILE>` — a JSON Schema for the model's final response shape.
  This pins the verdict to a typed object the harvest reads directly.
- `-o/--output-last-message <FILE>` — writes the final message to a file, so the
  verdict is captured cleanly without scraping interleaved logs. Dispatch does
  not pass this flag itself; `engine_wrapper.py` injects `-o <temp>` and
  atomically publishes the validated result as `review.json`.
- `-s read-only` — non-mutating sandbox, the same safety posture
  `codex review` defaulted to and the same mode the preflight probes.
- `--ephemeral` — no session files persisted to disk.
- `--ignore-user-config` — the operator's steerable `~/.codex/config.toml` is
  not loaded; auth still resolves via `CODEX_HOME`. Because this discards the
  operator's model selection, dispatch always passes `--model` and
  `-c model_reasoning_effort=...` explicitly — otherwise codex silently falls
  back to its built-in defaults. The tick resolves those two values on the
  trusted side (`--review-model`/`--review-effort`, else the operator's
  config.toml, else `gpt-5.5`/`high`) and forwards only them; the rest of that
  file — MCP servers, hooks, sandbox and approval keys — stays discarded.
- `--ignore-rules` — project execpolicy `.rules` files from the PR head
  worktree are not loaded (same vulnerability class as the
  CLAUDE.md/AGENTS.md policy gate below).

The engine runs with its working directory set to the PR worktree (no `-C`
flag; the wrapper is launched with `cwd` at the worktree root):

```bash
# `python` is the tick's own `sys.executable`, inherited rather than
# re-resolved: the wrapper must run on the same >= 3.11 interpreter the
# tick does, and a second resolution could disagree with the first.
python .claude/skills/tick/engine_wrapper.py run \
  --marker-path .claude/worktrees/pr-<N>/dispatch.json \
  --result-path .claude/worktrees/pr-<N>/review.json \
  --envelope-path .claude/worktrees/pr-<N>/completion.json \
  ... \
  -- codex exec \
    --output-schema .claude/skills/tick/review-schema.json \
    -s read-only \
    --ephemeral \
    --ignore-user-config \
    --ignore-rules \
    --model <resolved-model> \
    -c 'model_reasoning_effort="<resolved-effort>"' \
    "Review the code changes between commit <BASE_SHA> and HEAD in the \
     current repository; use git diff and git log to inspect them, and \
     judge only the changed code. Your final message must be ONLY a JSON \
     object conforming to the provided output schema: [verdict + findings \
     semantics spelled out]"
```

The `<BASE_SHA>` is the pinned base recorded at dispatch (see *Base refresh*
below), not a live `<remote>/<base>` ref — so the review can never silently run
against a base that moved underneath it.

**The prompt deliberately does not pin a reading list.** Codex auto-loads the
worktree's `AGENTS.md` as its project doc — that file carries the binding
review obligations (review the full change scope against adjacent contracts;
report the complete set of blocker/major findings) — and the read-only sandbox
leaves every repo file readable, so the reviewer decides for itself which
additional standards docs (the target's review checklists and relevant
architecture sections) to consult, with the same independence as a manually invoked
codex review. Verified empirically on
the pinned CLI with the exact dispatch flags: the project `AGENTS.md` is loaded
automatically and doc pointers inside it are followed. The accepted trade:
guaranteed context is `AGENTS.md` only; the hook checklist's obligations apply
on runs where the reviewer chooses to read it.

**Verdict schema.** `review-schema.json` defines the response shape the harvest
depends on:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["verdict", "findings"],
  "properties": {
    "verdict": { "enum": ["ready_for_human", "changes_requested"] },
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["severity", "file", "summary"],
        "properties": {
          "severity": { "enum": ["blocker", "major", "minor"] },
          "file": { "type": "string" },
          "summary": { "type": "string" }
        }
      }
    }
  }
}
```

The harvest reads `review.json.verdict` directly — no prose parsing, no exit-code
guessing. `findings` is required, so output missing it never promotes a PR. Promotion
is gated on the verdict **and** the findings, not the verdict string alone: a
`ready_for_human` verdict that still carries a blocker/major finding contradicts the
schema's own definition and is routed to `changes_requested` rather than shipped to a
human as clean. The review comment posted to the PR is rendered from the same typed
`findings` array and embeds the reviewed (diff base, base tip, head, base ref)
identity as an idempotency key, so a tick that crashes between posting the comment
and flipping the label never posts a duplicate (which would inflate the
comment-derived iteration count) on replay — while a re-dispatch that changes any
component (a legitimately different diff, base state, or base branch) is recognised
as a fresh round and still posts. The base branch *name* is in the key because a
retarget can land on a branch whose tip is the same commit: every SHA matches, yet
the pre-retarget comment must not suppress the fresh round's post — the posted
comment is what the next fixer dispatch reads as its findings handoff.

The reviewer gets:
- Read-only sandbox (`-s read-only`) — blocks filesystem writes
- The committed diff, self-scoped against the pinned base SHA
- The PR title and body, fetched by the trusted tick with its own `gh` access and
  embedded in the prompt as delimited context-only text — the reviewer environment
  has no GitHub credentials (by design, see below), so it cannot run `gh pr view`
  itself, and a failed metadata read retains the PR for the next tick instead of
  launching a context-less review
- The worktree's `AGENTS.md`, auto-loaded by codex as the project doc, plus
  unrestricted read access to the rest of the tree (`CLAUDE.md`, review
  checklists, docs) to consult at its own discretion

**Reviewer isolation (trust boundary).** `-s read-only` sandboxes the *filesystem*
only — not the network and not out-of-process tools — and it still permits *reads*, so
the reviewer (running over candidate-controlled code, a prompt-injection surface that
could be induced to read a secret and place it in a finding the tick then posts) is
locked down with an **allowlist, not a blocklist**:

- `env -i` drops the entire inherited environment, then re-adds only `PATH`, a scrub
  `HOME`, `TMPDIR`, `GIT_TERMINAL_PROMPT=0`, and `CODEX_HOME`. A blocklist
  (`env -u GH_TOKEN …`) leaves every unlisted secret (e.g. `DEEPSEEK_API_KEY*`)
  reachable; the allowlist does not. `GH_CONFIG_DIR` is omitted: the reviewer runs
  `codex exec` which authenticates via `CODEX_HOME/auth.json`, not `gh`.
- `CODEX_HOME` points at a fresh tick-owned dir containing **only a symlink to
  `~/.codex/auth.json`** — preserving login while loading none of the operator's
  steerable config (`config.toml`, plugins, rules, hooks, skills — MCP/plugins run as
  out-of-sandbox processes). An *empty* `CODEX_HOME` would leave codex unauthenticated,
  so auth must be carried in explicitly. Verified end-to-end against the pinned CLI.
- `HOME` is a scrub dir (not the real home) so the reviewer cannot read `~/.ssh`,
  `~/.config`, or other credential files; auth still resolves via the explicit
  `CODEX_HOME` symlink, which is not under `HOME`.

Scrub state lives at a tick-owned path **outside** the candidate worktree
(`WT_ROOT/pr-{N}-scrub`, not `wt_dir/.scrub`). This removes the vector where a committed
symlink at `.scrub` could theoretically redirect `HOME`/`CODEX_HOME`/`TMPDIR`. The
trusted-author/trusted-repo eligibility gate below remains as the primary control that
prevents untrusted candidates from reaching dispatch. The shipped dispatch
prompt does **not** carry untrusted-data framing or a no-secrets-in-findings instruction;
the residual risk that a prompt-injected PR could exfiltrate readable file contents
through the posted review comment is documented and accepted in `SKILL.md` (*Reviewer
sandbox posture*), with the trusted-author/trusted-repo eligibility gate below as the
primary control.

**Reviewer eligibility boundary (shipped slice).** The credential remains readable to
the Codex process and its tools, so the review slice is not safe for arbitrary candidate
content. Before worktree creation or credential exposure, dispatch reads the PR author,
head repository, and cross-repository flag from GitHub and checks them against the
operator-owned repository contract: the login the operator declared as
`[operator] login` (in their own config), the repository resolved from local Git
remotes, and
`isCrossRepository=false`. Only a proven match may proceed. Anything missing,
malformed, or outside that allowlist routes to `ai:needs-human-decision`; it
never launches Codex. This is an enforced trust boundary, not a prose
assumption — and it is configuration rather than a constant because the
pipeline drives a repository it does not own, so a shipped default would be a
trust decision made on the adopter's behalf. Full untrusted-review isolation
remains open (see *Future evolution*).

**Atomic launch/completion ownership.** The dispatch flow runs
the reviewer engine through `engine_wrapper.py`, which provides four guarantees:
(1) ownership before spawn — the wrapper creates a dedicated process group
(`os.setpgrp`), then writes its PID + `start_time` + PGID + `dispatch_id` to the
marker before the engine starts, where `start_time` is the OS-reported process
start time (same source the verifier reads) and PGID equals the wrapper's PID
(group leader); (2) signal forwarding — a SIGTERM handler terminates the engine
child before the wrapper exits; (3) atomic result placement — the engine writes to
a temp file and the wrapper validates exit status + schema before atomically
renaming it into the published path; (4) completion envelope — `completion.json`
is a dispatch-id-bound proof of either confirmed success or strict wrapper failure.
A success envelope records exit 0 and `result_valid=True`. A failure envelope records
a non-zero exit, `result_valid=False`, and non-empty `error_class`; its exact
`dispatch_id` must match the marker. HARVEST requires a valid success envelope for
completion (`classify_dispatch(envelope_valid=True)`), while a valid failure envelope
on a dead reviewer classifies as `failed`. A bare result file or malformed/unbound
envelope is treated as stale, not complete or terminally failed.
Liveness uses three-valued process identity verification (`verify-identity`
returns `matched`/`mismatched`/`unknown`): `matched` proves the same process and is
safe to signal; `mismatched` confirms PID reuse (never signal); `unknown` (OS start
time unreadable) retains custody and counts the slot — it never drives kill,
cleanup, or requeue. Termination signals the entire process group (`terminate-group`
via `os.killpg`) so the wrapper and its engine child are stopped together.
Post-spawn relabel failure uses the same group-aware death proof before archiving
scratch or releasing marker custody; wrapper exit alone is insufficient.

**Issue-author admission.** Only issues authored by the configured operator can
dispatch. `trusted_issue_candidate` fails closed on missing or mistyped authors
before an issue body is read. Issue queue reads use a bounded server-side
`--author` filter, including cadence reads; a page of other authors' issues
cannot starve eligible work or prevent quiescence. Targeted issue reads apply
the same predicate. Unknown author metadata keeps cadence warm while dispatch
refuses it. Skipped issues receive no relabel or escalation. One additional
bounded number-only read logs excluded issues, as a lower bound when truncated;
when both pages truncate only zero is provable, and unreadable or detectably
inconsistent reads produce no count. The validated operator identity stays
fixed through the tick and its following cadence observation.

**Candidate-controlled review policy (trust boundary).** Because the reviewer runs
inside the PR's checked-out tree, auto-loads that tree's `AGENTS.md`, and reads its
`CLAUDE.md` and review checklists at its own discretion, a PR that *edits* any of those
files would steer the very reviewer that decides whether it is promoted — untrusted
input controlling its own validator. An operator who requires unchanged review
policy enables the gate with `allow_touching = false`. Dispatch then checks the diff
(`git diff --name-only <base_sha>...<head_sha>`) for any reviewer-governing file
(`CLAUDE.md` or `AGENTS.md` at any level, plus any path the operator listed under
`checklist_paths`) and routes such a PR to `ai:needs-human-decision`
instead of launching a reviewer. `CLAUDE.md`/`AGENTS.md` are universal and matched
by basename; a target's own checklists are its to name, which is why they are
configuration rather than a shipped path list. The match set is intentionally
narrow — an unrelated file elsewhere under `.claude/` does not steer the reviewer
and is still auto-reviewed.

The pipeline's own gate files (`config.toml`, `lib/pipeline.py`, `lib/tick.py`)
are review policy on the same reasoning and are matched by path *suffix*, per
segment, so the rule holds at every install depth rather than only the one the
skills were incubated at.

So are the instructions the install supplies to a dispatched engine — every
bound skill doc and every prompt template. Those are read from the install, so
a PR cannot govern its own fixer with them; what the gate adds is that a PR
rewriting them for every future run is not auto-reviewed under the rules it is
changing. Their entries are *derived* from the constants the runtime reads
(`pipeline.BOUND_SKILLS`, `pipeline.PROMPTS_DIR`) rather than restated as a
path list, so a binding added by a future stage cannot land outside the gate by
omission — which is the failure the hand-maintained alternative invites.

This gate is operator-configurable: `allow_touching = true` under `[defaults]` or
`[repos."owner/name"]` in the operator's own config lets policy-editing PRs flow to
auto-review anyway (the operator accepts that the review runs under instructions the
PR itself supplies). The packaged floor ships `true` together with operator-only
issue pickup and reviewer admission. Absent operator settings inherit that
floor; a missing floor or mistyped value falls back to `false`, and malformed
operator TOML refuses startup. Widening issue authors must restore the shipped
`false` default in the same change. See the skill README's "Admission gate:
reviewer policy files" section.

Note that the operator's config is not in any repository, so no PR can edit it.
The packaged `config.toml` is, and it is still part of the policy path set for
exactly that reason: it carries the gate's shipped default.

**Base and head refresh (closes the stale-base AND stale-head risks).** Reviewing
against a stale base is worse in an unattended loop than manually: a PR can be
reviewed against yesterday's base, declared clean, and promoted to
`ai:ready-for-human` while the real base has moved. So dispatch must, for each PR:
read the PR's actual `baseRefName` (`gh pr view <N> --json baseRefName`), `git fetch
<remote> <baseRefName>`, and pin **two** base SHAs in `dispatch.json` — they serve
different invariants. `base_tip_sha` is the fetched base tip (the freshness
snapshot): harvest re-reads the current base tip, and if it advanced past the
pinned tip the review is discarded and the PR re-dispatched rather than promoted.
`base_sha` is the merge-base of that tip and the pinned head (the diff anchor):
it is what the reviewer's diff actually runs from — diffing from the tip itself
would flag commits that landed on the base after the branch was cut. The posted
review comment and its hidden idempotency token record the full
(diff base, base tip, head, base ref) identity so the audit trail states honestly
which range was reviewed and against which base. If the `baseRefName` read fails, the
PR is retained for the next tick — the base is never guessed as `<remote>/main`,
because a stacked PR's base is another branch.

The base branch *name* is re-checked at harvest, not only at dispatch: harvest
point-reads the live `baseRefName` together with `headRefOid` in one snapshot, and
if the PR was retargeted to a different base while the review ran the review is
discarded and re-dispatched. The SHA drift checks alone cannot catch a retarget —
the old ref's tip and the head can both be unchanged while the PR now merges into
a different branch — and the old ref may no longer exist, so the retarget check
runs before (and without) resolving the old base tip.

The head is pinned the same way and for the same reason: a PR can be revised or
force-pushed *while the review runs*, so dispatch resolves and records the exact
`head_sha` (`gh pr view <N> --json headRefOid`), checks the worktree out to it
(detached), and harvest re-reads the **live PR head** (not the worktree's checked-out
HEAD, which is the pinned commit). If the live head differs from the pinned `head_sha`,
the review is discarded and re-dispatched rather than promoting a head the reviewer
never saw.

**Preflight requirement (blocks implementation).** Before the first dispatch,
the implementation must confirm against the exact installed CLI version: (a)
`codex exec` runs to completion here — the optional native dependency
(`@openai/codex-*`) must actually be installed, not just the wrapper package (the
preflight hard-fails with an actionable message if `codex --version` is not even
runnable); (b) `--output-schema` + `-o` produce a file whose contents validate
against `review-schema.json`. Pin the verified version in the skill.

A fully tool-execution-sourced sandbox proof was *not* available on
`codex-cli 0.141.0`, where this was originally verified: its `codex exec --json`
stream emitted no structured command-execution or denial event (only
`thread.started` / `turn.started` / `item.completed`(agent_message) /
`turn.completed`), and the sandbox denial surfaced only inside the model-authored
agent message. The read-only probe may still collect the typed
`write_attempted`/`write_blocked` self-report and corroborate that no file exists,
but it must print that result as an **unverified model-reported signal**, never as a
proved sandbox property. `PREFLIGHT OK` is scoped to binary execution and structured
output only. The pinned floor is now `codex-cli 0.142.5` — the build the shipped
reviewer argv (bare `codex exec` with `--ignore-rules`, `-s read-only`, and
`--output-schema` honored for the final message) was verified on. `0.142.5` has been
observed emitting `command_execution` items in the JSONL stream, so a typed
exec-event probe may now be possible — a future improvement, not yet implemented.

**Reviewer binary identity (shipped).** Dispatch binds to the exact build the
preflight verified: both resolve the binary via `command -v codex` and gate on the
pinned `PINNED_CODEX_VERSION`/`PINNED_VERSION`, failing closed. A PATH that drifts to
an unverified or incompatible build (wrong version, missing native dependency) stops
dispatch (`verify_codex_bin` returns non-zero → `break`) rather than launching a
reviewer whose sandbox/schema behaviour was never proven. The launch then invokes the
resolved `"$CODEX_BIN"`, not a bare PATH lookup.

> **Deferred hardening.** The shipped review slice enforces its self-authored,
> same-repository boundary before dispatch. Two reviewer-launch hardening items are
> still required before widening that allowlist: (1) keeping the Codex credential
> outside the reviewer's reachable filesystem,
> blocking its network egress, and secret-scanning the result before posting (the
> reviewer runs over a prompt-injection surface); and (2) exercising the *full*
> production launch boundary (`env -i` / scrubbed `HOME` / rule isolation) from the
> preflight itself, rather than only the schema/sandbox probe above. (Binding dispatch
> to the verified binary/version is now shipped — see "Reviewer binary identity".)

**Fixer (Claude):**

Same as implementer, but the prompt includes the review comments fetched
from the PR via `gh pr view <N> --json comments`, the worktree is a detached
checkout of the PR branch's remote tip (no local branch custody, so a stale
implementer worktree holding the same branch cannot block dispatch), and the
push permission is the exact refspec `git push <remote> HEAD:refs/heads/<head-ref>`
instead of the implementer's bare `git push <remote> HEAD`.

### File-based handoff

Each worktree contains pipeline state files:

```
.claude/worktrees/pr-100/
├── dispatch.json      # Published around engine start; carries process identity
│                      # {engine, pid, start_time, pgid, stage, deadline, dispatch_id,
│                      #  pr_number, base_ref, base_sha, head_sha, pre_label,
│                      #  base_tip_sha (review only: base tip freshness anchor;
│                      #  base_sha is then the merge-base diff anchor)}
├── completion.json    # Reviewer-only wrapper exit proof and HARVEST gate
│                      # {dispatch_id, exit_status, wrapper_pid, start_time,
│                      #  result_valid, error_class (required on failure only)}
├── result.json        # Claude engines (implement/fix): JSON envelope
│                      # written via --output-format json
├── review.json        # Codex reviewer: typed verdict + findings, atomically placed
│                      # by engine_wrapper.py after schema validation
├── review-observation.json
│                      # bounded sanitized reviewer skill-file evidence
├── stderr.log         # Reviewer: bounded sanitized wrapper diagnostics only;
│                      # implementer/fixer engine stderr remains captured
├── prompt.txt         # The prompt that was dispatched
├── iter.txt           # Fix-iteration cache (plain integer); authority is the
│                      # PR comment history — see Iteration counter
└── (repo files...)    # The actual code
```

Marker and reviewer-completion files are written **temp-then-rename** so every read
sees a complete object — under the tight adaptive cadence a tick can fire while a
marker is mid-write.

**Reviewer completion authority.** The Codex reviewer runs through
`engine_wrapper.py`. HARVEST requires both a non-empty `review.json` and a valid,
dispatch-bound `completion.json` proving successful exit and schema validation. A
bare reviewer result is stale, never complete. Separately, the wrapper always
publishes a strict dispatch-bound failure envelope when startup/spawn fails or the
reviewer exits non-zero: it has `result_valid=False`, a non-empty `error_class`, and
no authoritative review result. Once the recorded reviewer is proven dead, that
envelope classifies as `failed` and terminally routes to human decision rather than
spending another reviewer run.

**Failed reviewer evidence custody.** The terminal branch is archive before relabel:
copy and validate the dispatch evidence first, then relabel `ai:reviewing` to
`ai:needs-human-decision`, then clear the attempt counter, then finalize the archive
by moving scratch. The counter clears before the move so a crash between the two
cannot strand a stale durable count after the marker is gone. Archive failure before
relabel retains scratch and the in-flight label. Relabel failure retains both the
pre-transition archive and scratch so a later tick retries the terminal route without
redispatch. If final archive completion fails after relabel, the terminal label
remains authoritative and the pipeline retains scratch for operator recovery.

**Reviewer output suppression and observability.** Reviewer JSONL stdout is drained
through a bounded sanitizer; raw events, prompts, messages, commands, output, and
absolute paths are discarded. `review-observation.json` retains only normalized
`SKILL.md` identities referenced by successful command events and fixed counters.
The current CLI exposes no explicit invocation event, so this is command-reference
evidence, not proof that the file was opened or the skill was semantically invoked.
Missing or malformed observation is non-authoritative
and never changes completion, custody, relabeling, or cleanup. Reviewer child stderr
is redirected to `DEVNULL`. The reviewer's per-worktree `stderr.log` contains only a
bounded sanitized startup error class or fixed `EngineExitError` marker. Structured
failure details remain in `completion.json`. This boundary does not apply to Claude
stages: implementer/fixer engine stderr remains captured in their per-worktree
`stderr.log`.

**Wrapperless implementer/fixer completion authority.** Claude implementers and
fixers write `result.json` directly and do not produce `completion.json`. Once the
recorded process is proven dead, a non-empty `result.json` is ready for harvest in
memory. The tick does not persist a synthetic envelope or claim a verified zero exit
status. A zero-byte result remains stale. Missing sentinels are then classified as a
known transient quota/limit result or a genuine escalation; transient results share a
provider-domain cooldown and consume at most `MAX_DISPATCH_ATTEMPTS`.

**Strict marker validation (fail closed).** Atomic rename prevents torn writes but does
not prove a marker carries the fields recovery acts on. Before any liveness check, kill,
cleanup, revert, or label transition, the marker is validated by *type, enumeration, and
identity* (`valid_marker`): it must carry the full provenance set (`engine`, `stage`,
`pr_number`, `deadline`, `base_ref`, `base_sha`, `head_sha`, `pre_label`) with no unknown
fields, a known stage-appropriate engine (`codex` for review, `claude` or `claude-ds`
for implement/fix), a known `stage` matching the directory's PR/stage, a finite
`deadline`, a `pre_label` that is one of the real pre-dispatch labels
(`ai:implemented`/`ai:revised`, never an arbitrary or terminal label a revert would
promote to), and **exactly one**
ownership signal (a usable `pid` XOR a `launching` state). The optional
`base_tip_sha` (review markers only; the two-SHA freshness anchor) is likewise
validated as provenance when present — it must be a canonical 40-hex commit OID,
since a malformed value would silently degrade the drift guard to the merge-base
fallback. An invalid marker is treated as marker-less, and marker absence is
never evidence: reconciliation **retains** the `ai:reviewing` label and takes no
action rather than driving an incorrect revert from a corrupt record. The one
place a corrupt marker still escalates is *dispatch* — an item selected from a
pickup label whose worktree holds an unreadable marker moves to
`ai:needs-human-decision` and keeps the marker for inspection, because there the
tick is about to spend money and cannot prove no engine is already running. That
exception is about imminent spend, not about custody inference. It is reached
only for a candidate that cleared the label eligibility gate: an item also
carrying an in-flight label is skipped before its marker is read, so a partial
transition on another machine cannot be escalated on this one. It is also
narrowed by stage: one item has one scratch directory, so a fix-stage dispatch
can find a *reviewer's* marker there. A marker that is structurally valid for
the item but records another stage is that stage's custody, not corruption —
dispatch skips and leaves it to its own harvest. Only a marker this stage could
plausibly have written may be judged corrupt. Neither branch strands the loop: a
live sibling engine is engine-occupied, so the admission marker scan counts it
and cadence stays warm on its deadline; a dead one is in its own stage's
reconcile set (HARVEST unions local markers with the label query) and is cleaned
there.

**Reviewer dispatch marker lifecycle:**
0. Label eligibility, before the marker is even read: dispatch point-reads the
   candidate's complete label set and requires exactly one pickup label across
   all stages — this stage's — with no hold and no in-flight label, rejecting
   identities already in the tick's shared authoritative in-flight set first.
   The marker scan below answers "did *this* machine already launch?"; only the
   label set can answer "did *any* machine?", and a partial transition elsewhere
   leaves both labels in place for the life of that engine. The shared list is
   the third answer — "did this *tick* already launch, at any stage?" — and it
   is the only one that survives a GitHub read that lags within one DISPATCH
   phase.
0b. Replay safety: before launching, dispatch skips any PR whose worktree already has a
   live `dispatch.json` with no result file — an engine is already in-flight. A prior
   tick may have written the marker and launched the engine but failed to flip the
   label before returning, leaving the PR in the pickup set; without this guard the
   next tick would launch a duplicate engine into the same worktree and double-spend.
   An unreadable or unclassifiable marker retains custody and moves a pickup item to
   `ai:needs-human-decision`; it is never deleted or skipped indefinitely.
1. Tick writes a `launching` lease (`dispatch.json` with `state: "launching"` and
   `dispatch_id` but no PID), spawns the engine, and *then* flips the label to
   `ai:reviewing`. The order is **lease → launch → relabel** in all three stages
   (reviewer `tick.py:5942`→`5994`, implementer, fixer), not lease → relabel →
   spawn. This is deliberate — a relabel that lands before a failed spawn strands
   the item at an in-flight label with nothing running — but it means **the label
   is not a pre-spend acquisition lock**. Two ticks that select the same item
   before either relabel lands will both spawn a paid engine; the loser's relabel
   then fails and it terminates its engine (see the failure branch below), so the
   spend is real but bounded. Closing that window needs compare-and-swap
   acquisition, which is out of scope here and deserves its own measurement.
2. The engine wrapper (`engine_wrapper.py`) creates a dedicated process group
   (`os.setpgrp`), then replaces the lease with a full ownership marker carrying
   `pid` + `start_time` (OS-reported) + `pgid` (= wrapper PID, since it is the group
   leader) + `dispatch_id` before spawning codex. The child inherits the process group.
3. Engine runs; wrapper validates exit status + schema, then atomically renames the
   result to `review.json`. The wrapper always writes `completion.json`: either a
   success envelope or the strict failure envelope described above.
4. Next tick sees `completion.json` → validates envelope (dispatch-id match, exit 0,
   `result_valid=True`) → classifies as `complete` → harvests → deletes marker +
   envelope + result files. A bare `review.json` without a valid envelope is NOT
   treated as complete — it is classified as stale.
5. A dispatch-bound failure envelope (`exit_status != 0`, `result_valid=False`,
   non-empty `error_class`) plus a dead reviewer classifies as `failed`. HARVEST
   archives evidence, terminally relabels to `ai:needs-human-decision`, and finalizes
   cleanup under the custody rules above. It never reverts for reviewer redispatch.
5a. If `dispatch.json` exists but no valid success or failure envelope and the PID
   is dead (identity `mismatched` or process gone) → stale dispatch: tick reverts the in-flight label to
   its pre-dispatch label so the next dispatch phase re-queues it. Liveness uses
   three-valued identity verification (matched/mismatched/unknown); `unknown` retains
   custody (never treated as dead). **Bounded:** each revert increments a durable
   per-PR attempt counter; once it would reach `MAX_DISPATCH_ATTEMPTS`, reconcile
   escalates to `ai:needs-human-decision` instead of reverting.
5b. If the marker is a `launching` lease (written pre-spawn, real PID never published
   because the tick crashed between spawning codex and rewriting the marker) → the
   paid child's liveness is unprovable, so reconcile **escalates** rather than treating
   it as stale and re-dispatching (which would double-spend).
6. If the PID is still alive but `deadline` has passed → hung engine: tick kills the
   entire process group (`terminate-group` via `os.killpg`), escalates to
   `ai:needs-human-decision`, frees the slot, and excludes the dispatch from the
   in-flight cadence input so it cannot pin the loop at "busy". Kill only fires when
   identity is `matched` — never on `unknown` or `mismatched`.
7. Terminal-PR cleanup (merged/closed while reviewer runs) also uses identity-verified,
   group-aware termination before dropping the scratch.
8. Retained or completed scratch is archived under
   `.claude/worktrees/.harvest-evidence/` before redispatch cleanup. Markerless
   interrupted-harvest state uses a `retained-markerless` archive suffix; repeated
   incidents get separate numeric suffixes. The entire original tree moves under
   `scratch/` so untracked and unknown recovery files cannot be lost.
   Archive creation, artifact copy, diagnostic snapshot writes, and the final scratch
   move are mandatory: any failure retains the original scratch for operator recovery.

**Stale-label reconciliation.** After HARVEST, the tick compares authoritative
in-flight labels with local markers. Every markerless in-flight item is
**retained** — the sweep has no mutating branch at all, including for an
`ai:implementing` issue that has an open closing PR, because
`gh search prs "Closes #N"` is a full-text match that hits drafts and PRs under
active revision, and removing the label would leave the issue carrying no
pipeline label. A per-item point-read runs only so `stale_label_retained` is
logged against the item's authoritative current label rather than a stale search
index row; a failed or malformed read leaves the label unchanged either way. In
targeted mode the sweep is restricted to the requested item. Global in-flight
labels participate in no admission accounting at all: the cap counts this
machine's dispatch markers. See
[Why custody is not inferred, and not yet proven](#why-custody-is-not-inferred-and-not-yet-proven).

### Prompt assembly

Prompts are assembled by the tick from live data:

Every Claude engine prompt (implementer and fixer) must end with an explicit
**sentinel instruction**, because the emitted sentinel is the only signal the
harvest uses to decide an engine's outcome. The reviewer does not use a sentinel
— its verdict is a typed field in `review.json`, pinned by `--output-schema`
(see *Reviewer (Codex)*).

**Implementation prompt:**
- Issue body and title (via `gh issue view`)
- Relevant architecture doc sections (referenced in issue)
- The target repo's governing instructions (inherited by the worktree), which
  also supply its verification commands. This repo's `CLAUDE.md` imports the
  shared `AGENTS.md`; see [instruction inheritance](working/pr-pipeline-internals.md#4-claudemd--agentsmd-inheritance)
- **Sentinel instruction:** end with `IMPLEMENTATION_COMPLETE` once code is
  committed and pushed, or `NEEDS_HUMAN_DECISION` if blocked on scope/design

**Review prompt:**
- Instruction to self-scope the committed diff via `git diff`/`git log` against
  the pinned base SHA carried in the prompt (from `dispatch.json`, not a live ref)
- Instruction that the final message must be ONLY a JSON object matching the
  output schema, with the verdict and findings semantics spelled out
- No pinned reading list — deliberate. The worktree's `AGENTS.md` auto-loads as
  the codex project doc, and the reviewer consults further standards docs
  (the target's review checklists and architecture sections) as needed (see
  *Reviewer (Codex)*)
- `--output-schema` pointing at `review-schema.json`; verdict read from the typed
  `review.json` the wrapper validates and publishes

**Fix prompt:**
- Review comments from the PR (via `gh pr view --json comments`)
- The current PR body, and the requirements of every issue GitHub reports under
  `closingIssuesReferences` (via `gh pr view --json body,closingIssuesReferences`
  plus `gh issue view --json title,body`). The engine has no pre-approved `gh` access, so
  these are read at dispatch and substituted; each is capped at
  `tick.FIXER_CONTEXT_BUDGET` with the cut stated in the text — the linked issues
  share one budget, allowanced shortest-first so no issue is dropped outright —
  and a failed or mistyped read retains the PR rather than rendering an absent
  contract. The same read pins the marker's `pr_body` provenance block — the
  body's digest, the closure contract it declares, and whether the copy handed to
  the engine was cut short — which gates the PR-body revision path below
- The filename, the dispatch-bound stamp line, and the head-stamp prefix for an
  optional replacement PR body (see the PR-body revision contract below)
- The current diff (the worktree is the PR head; `git diff`/`git log` are allowed)
- Instructions to address each finding, commit, and push
- The doc path of each bound discipline skill (`tick.FIXER_SKILLS`), resolved
  against the *install* at dispatch — the guidance ships with the pipeline, so
  no target repository needs a copy and no PR branch supplies the rules its own
  fixer runs under; a doc missing from the install is a packaging defect and
  escalates instead of launching
- A repair-or-replacement declaration **per finding**, made before editing and
  re-checked against the pushed diff, published in that finding's own block of
  the change summary. One run may mix repairs and replacements; each replacement
  states its own rejected shape, reasoning, replacing contract, and audited
  surfaces
- **Sentinel instruction:** end with `FIXES_PUSHED_RECHECK_NEEDED` once
  revisions are committed and pushed *and* every finding addressed carries a
  decision matching the pushed diff — the three green commands are necessary but
  not sufficient — or `NEEDS_HUMAN_DECISION` if a finding is out of scope

The fixer never runs `gh pr edit` itself — that would place an unretractable
remote mutation mid-run, before the push it describes is verified. It writes a
candidate body instead, and the controller decides, the same engine-writes-file
shape as the implementer's `.pr-body.md`:

- The engine writes `tick._PR_BODY_REVISION_FILE` (`.pr-body-revision.md`) in
  its worktree root **after its final push**, and only when the body it was
  handed no longer describes the head it pushed. Absent is the normal case. The
  file is not committed.
- Its first two lines are an envelope. `pr-body-revision: <dispatch_id>` is the
  literal dispatch substitutes into the prompt: the fixer worktree is a detached
  checkout of the PR head, so an unstamped file is one the branch carried in — a
  stray `git add -A` on an earlier run — and publishing it could overwrite a body
  a human has since edited. `pr-body-revision-head: <OID>` is the commit the
  engine pushed, and the harvest requires it to equal the PR's live head; a
  dispatch id alone cannot distinguish a file written for the final commit from
  one written for an intermediate one, which is why the prompt orders the write
  after the push.
- The read is bounded at the `read` call (`_PR_BODY_REVISION_READ_LIMIT`), not
  after it. A size check that consumes the file first can be exhausted by the
  runaway file it exists to reject. `_PR_BODY_MAX_CHARS` (65,536) is then
  enforced against both the character count and the UTF-8 byte length, because
  GitHub does not document which one its own limit counts and the two differ by
  up to 4x on non-ASCII prose.
- Provenance is checked before content. A marker with no `pr_body` block (one
  written before this path existed, or a PR whose body pinned no closure) or an
  unreadable live body withholds the replacement outright, because judging the
  candidate on its sections would publish "malformed body" about a body that is
  fine.
- A revision is withheld outright when the body handed to the engine was
  truncated for the prompt. The engine cannot preserve what it never read, and no
  structural check can distinguish an intentional rewrite from a section lost
  past the cut.
- The closure contract comes from the body, not from `closingIssuesReferences`:
  the replacement must open `## Summary` with the same `Closes #N` and declare
  exactly the same set of closing directives anywhere in the body. Merging acts
  on all of them, so a swap, a drop, or an addition changes what the PR does.
  Reading the body is also what keeps the path alive for a PR based on a
  non-default branch, where GitHub reports no linked issues at all.
- The remainder is validated against `_pr_body_matches_guidelines` for the
  section contract.
- Conflict detection compares the live body against the `pr_body` block's
  `digest` — a SHA-256 of the body dispatch handed the engine, pinned because by
  harvest time the live body may already have changed. A digest rather than the
  body keeps an unbounded document out of a strictly validated marker; GitHub
  keeps the body's own edit history. A live body that already equals the
  candidate is treated as this run's own landed write, not a conflict, so a retry
  after a failed comment post is idempotent.
- The whole `pr_body` block is written and validated as a unit. Its fields are
  produced by one dispatch and mean nothing apart, so a marker carrying a subset
  is corruption rather than a legacy marker; `pipeline.valid_pr_body_provenance`
  is the single definition, called by both `valid_marker` and the harvest's
  `PRBodyProvenance.from_marker`.
- The edit is applied only on the `recheck` outcome, before the fix comment and
  therefore before the `ai:revised` relabel. Escalated and requeued runs may not
  have pushed the head the replacement describes, so their body is left alone.
- Immediately before `gh pr edit`, authority is re-established against GitHub:
  labels, head, and body. A hold added since reconcile outranks the completed
  result, and a moved head or body means the candidate was judged against
  something else — any of the three abandons the whole harvest for this tick
  (no body, no comment, no relabel) with every input retained, and a hold does
  not consume a retry attempt. This narrows the unguarded window to one API call
  rather than closing it: GitHub offers no conditional write for unsafe methods,
  so an edit landing inside that call is still overwritten.
- Every refusal — absent aside — is named in the published fix comment
  (`- PR body: …`), and each cause carries its own reason rather than sharing
  one: an unreadable file, an oversized one, and a body that fails the section
  contract are three different things for the next reviewer to look at. A body
  silently left alone is indistinguishable from a pipeline that never looked.
- A failed `gh pr edit` takes the same bound as a failed comment post: no
  comment, no relabel, scratch and in-flight label retained for the next tick
  through `_bound_failed_harvest`.

The implementer needs no envelope, and the reviewer needs no path at all. The
implementer also writes `.pr-body.md` after its final push, so its body describes
the commits it published — but the harvest uses that file to *open* a PR that does
not exist yet, so there is no live body to clobber and no undo to be missing. The
reviewer runs read-only and pushes nothing.

### Worktree management

The pipeline uses a single worktree root (created on first dispatch; it does not
exist in the repo yet):
- `.claude/worktrees/` — used by every engine: the implementer, fixer, and
  orchestrator (Claude) and the reviewer (`codex exec` runs with its cwd set to
  the same worktree, so Codex never manages a separate worktree of its own)

**Implementation worktrees:** The tick creates a worktree via
`git worktree add .claude/worktrees/issue-<N> -b agent/issue-<N> <remote>/main`.
After PR creation, the directory is renamed to `.claude/worktrees/pr-<PR>/`.

**PR worktrees (fix stage):** `.claude/worktrees/pr-<N>/` persists across
fix cycles. The fixer's worktree is (re)created as a detached checkout of
`<remote>/<head-ref>` — never a named-branch checkout, so a stale implementer
worktree still holding the branch cannot collide with fixer dispatch.

**PR worktrees (review stage):** `codex exec` runs with its cwd set to
`.claude/worktrees/pr-<N>`, checked out detached at the pinned PR head SHA.
The reviewer runs under `-s read-only`, so no writes to the worktree occur.

Worktree creation uses `git worktree add`. Cleanup happens when a tick sees
a worktree for a merged/closed PR (`git worktree remove`). When the scratch
tree is archived instead of removed in place, the shared archive finalizer
*moves* the directory into the evidence tree and immediately runs
`git worktree prune`; terminal harvest and stale/markerless redispatch recovery
all cross that boundary. Failure is logged as deferred metadata garbage
collection because the evidence has already moved into archive custody.

### Sentinel tokens

These are the logical verdicts the harvest acts on. The Claude engines
(implementer, fixer) emit their token literally in `result.json` because they
take a prompt that instructs them to. The reviewer is different: its verdict is
a **typed field in `review.json`**, pinned by `codex exec --output-schema` (see
*Reviewer (Codex)*) — the harvest reads `verdict` directly, not a string in
prose. The `ready_for_human`/`changes_requested` schema values map to the
logical verdicts below.

| Verdict | Source | Meaning |
|---|---|---|
| `IMPLEMENTATION_COMPLETE` | Implementer (literal in `result.json`) | Code committed and pushed |
| `NO_BLOCKERS_READY_FOR_HUMAN` | Reviewer (`review.json` verdict `ready_for_human`) | Clean review, no findings |
| `BLOCKERS_FOUND` | Reviewer (`review.json` verdict `changes_requested`) | Review found issues |
| `FIXES_PUSHED_RECHECK_NEEDED` | Fixer (literal in `result.json`) | Revisions committed and pushed (claim — cross-checked against the live head at harvest) |
| `NEEDS_HUMAN_DECISION` | Any | Scope/design question for the developer |

When the expected signal is missing (no sentinel for a Claude engine; missing,
unparseable, or schema-invalid `review.json` for the reviewer):
- Review stage: treat as `BLOCKERS_FOUND` (conservative default)
- Fix stage: treat as `NEEDS_HUMAN_DECISION` (ambiguous output escalates)
- Implement stage: treat as `NEEDS_HUMAN_DECISION`

### What belongs to the target repo, and what belongs to the pipeline

The pipeline drives a repository it does not own. Anything that varies per
target is the target's to state; the tick's job is to carry it, not to encode
it.

**The target repo owns its own standards.** Test, lint and typecheck commands,
commit conventions, PR body format, and review checklists all live in the
target's `CLAUDE.md`/`AGENTS.md` and the docs those route to. Engine prompts say
*run the repository's verification commands and follow its workflow* — they do
not name `pytest`, `ruff`, `npm`, or any other stack. A pipeline that hard-coded
one repo's toolchain would be installable in exactly one repo.

The operator declares verification commands as `verify_tools`, per target where
they differ. Those entries are pre-approved tool grants, not instructions: the
prompt still defers to the repo's guides for what to run and in what order.
Under `dontAsk`, calls needing approval beyond the grants are denied; under the
default `auto`, they reach the classifier. Approval is not guaranteed. The
shipped floor still grants the Python triple; emptying it is a separate change.

**The pipeline owns the control plane.** The label vocabulary
(`docs/working/pr-guidelines.md`), the state machine, custody, and the cadence
are pipeline concerns and are identical for every target.

**Models are operator configuration.** The implementer runs Claude via
`claude -p` and can be routed to another provider by setting
`ANTHROPIC_BASE_URL` in the dispatch env. The reviewer's model is pinned
explicitly in the dispatch argv, because `--ignore-user-config` discards the
operator's `config.toml` defaults.

### Configuration ownership

**The operator owns configuration outside both the install and the target
repository.** An install-relative file disappears when a plugin upgrade moves
to a new cache directory. A target-owned file lets the PR supply the policy it
is judged under. These are separate reasons for the same placement decision.

Placement is **not process isolation**: an allowed verification command can run
PR-authored scripts with the operator's filesystem access and overwrite external
configuration. Containing that belongs to engine isolation. Configuration layers
must never be read from a target repository or engine worktree.

**One resolver owns interpretation.** Callers use `ResolvedConfig` rather than
inventing their own parsing rules. A list override must be able to narrow a
grant, and an explicitly empty grant must remain distinct from malformed input.
A setting with no safe fallback must refuse startup rather than silently change
the operator's intent. The remaining per-tick snapshot work is tracked with the
[current resolver contract](working/pr-pipeline-internals.md#7-operator-configuration-resolution).

**Target identity is shared by controller and dashboard.** Resolving one
repository and matching remote prevents reads, mutations, and engine push grants
from disagreeing about their target. Worktree-context validation checks the
actual Git destination before launch/fetch; it does not freeze Git settings for
an already running engine.

The [internals configuration section](working/pr-pipeline-internals.md#7-operator-configuration-resolution)
owns file selection, precedence, validation, and target-resolution mechanics.
The [operator configuration guide](../skills/tick/README.md#configuration) owns
copyable examples and corrective actions. The
[distribution plan](working/plugin-distribution-design.md#3-configuration-location-layers-and-schema)
keeps migration requirements, including changes not yet shipped.

## Skill structure

The implementation is delivered as Claude Code skills. Each command is its own
discoverable skill directory with a `SKILL.md` entrypoint (the directory name is the
`/command`); the decision logic the tick depends on lives in a small, stdlib-only,
unit-tested Python CLI co-located with the tick skill. The tick orchestrator is
Python (`lib/tick.py`), not shell: `SKILL.md` is an invocation stub, so the
decisions a tick makes are unit-testable rather than re-derived in bash on every
run.

The layout below is written at the pipeline repo's own depth (`skills/`). Under
a host repo it sits at `.claude/skills/`, and under a plugin install outside the
repo entirely — which is why nothing here resolves a pipeline path by counting
directories.

`skills/` is the plugin's published command surface, not a convenient place to
keep files: `"skills": "./skills"` in the manifest makes every directory under
it a namespaced command whose description loads into every session's context.
Anything the pipeline needs but does not mean to publish belongs outside it.

```
.claude-plugin/
  plugin.json                   # the plugin: name, metadata, "skills": "./skills"
  marketplace.json              # the repository as its own marketplace, source "./"
skills/
  tick/
    SKILL.md                    # /pr-pipeline:tick — invocation stub (thin)
    README.md                   # operator guide: usage, observability, knobs
    DEBUG.md                    # invariants, diagnostics, recovery
    config.toml                 # the packaged config floor, read-only (layer 1)
    review-schema.json          # Codex reviewer output schema (verdict + findings)
    preflight.sh                # one-time live verification gate
    preflight-probe-schema.json # typed schema for the read-only sandbox probe
    engine_wrapper.py           # process-group ownership around the reviewer
    lib/
      tick.py                   # the orchestrator: HARVEST then DISPATCH, one process
      pipeline.py               # pure decision logic + argparse CLI (imported by tick.py)
      process_control.py        # bounded TERM/KILL rollback helper
      claude_stream_wrapper.py  # stream-json reader + progress heartbeat
      reviewer_observability.py # bounded, sanitized reviewer stderr capture
      prompts/                  # implementer / reviewer / fixer templates
      README.md                 # CLI reference + pinned Codex version
      test_*.py                 # unit, dispatch, and executable-contract suites
  status/
    SKILL.md                    # /pr-pipeline:status — read-only dashboard
    status.sh                   # the dashboard implementation
    test_status.py              # dashboard rendering tests
  definition-of-done/
    SKILL.md                    # the bar a change clears before green counts as proof
    references/why.md           # the failure shapes each step was built from
  review-response-hygiene/
    SKILL.md                    # the revision rules the fixer prompt binds by path
```

The skills compose `gh`, `git`, `claude -p`, and `codex exec` directly. The decisions
a tick must make about a finished review (schema validation, verdict→label mapping,
iteration counting, base/head-SHA drift, dispatch-liveness, idempotent replay) are
**not** re-derived in inline code each tick — they live in `lib/pipeline.py` as pure,
tested functions behind a thin CLI, so an unattended loop behaves identically every
run. `lib/` is stdlib-only — it takes no dependency on any target repo's
toolchain, which is what makes it installable against an arbitrary repository.
The config artifacts are the packaged `config.toml` floor and the two JSON Schemas
(`review-schema.json`, `preflight-probe-schema.json`). The operator's own settings
are not in this tree at all — see *Configuration ownership* below.

## Usage

> **Implementation status.** The sections below describe the *full* end-to-end
> design (implement → review → fix → re-review, self-paced loop). The **currently
> shipped slices are the implement, review, and fix stages**, run as a single
> supervised tick: it dispatches a Claude implementer for issues at `ai:available`,
> harvests the `IMPLEMENTATION_COMPLETE` / `NEEDS_HUMAN_DECISION` sentinel from
> `result.json`, creates the PR labelled `ai:implemented`, dispatches the Codex
> reviewer, harvests the typed verdict and transitions the label, dispatches a
> Claude fixer for PRs at `ai:changes-requested`, harvests the
> `FIXES_PUSHED_RECHECK_NEEDED` / `NEEDS_HUMAN_DECISION` sentinel, verifies a
> push claim against the PR's live head (an unchanged head escalates as
> contradictory), flips to `ai:revised` for re-review, and bumps the iteration
> cache. Self-paced cadence
> is shipped through the compact `/loop` prompt in `SKILL.md`: each iteration
> consumes `tick-cadence:`, schedules the emitted `next_wake`, or stops and
> notifies the developer when `stop=true`.

**Manual tick (the runnable behaviour today):**
```
/pr-pipeline:tick
```

**Looped (self-paced):**
```
/pr-pipeline:tick, then paste the SKILL.md recurring prompt into /loop
```
No fixed interval. Each tick schedules its own next wake from pipeline state
(see Loop and cadence): ~270s while engines are in-flight, ~1800s when idle,
and it stops once the pipeline reaches quiescence.

**Status check (optional):**
```
/pr-pipeline:status
```
Most of what you'd want is already on GitHub — `gh pr list --label ai:...` (or
the web UI) shows every pipeline stage, since labels are the source of truth.
The dashboard's only added value is the **in-flight, non-GitHub state**: which
engines are live right now (PID, stage, time-to-deadline from the local
`dispatch.json` markers), per-PR iteration counts, and whether the loop has
reached quiescence. If that local view isn't worth a second skill, this can be
dropped and `gh pr list` used directly — it is not load-bearing for the
pipeline.

**Workflow from the developer's perspective:**

1. Write issues describing work, label them `ai:available`
2. Run `/pr-pipeline:tick`; for self-paced operation, paste its compact
   recurring prompt into `/loop`
3. Tick implements, reviews, fixes, re-reviews — all via label transitions
4. Check `/pr-pipeline:status` to see which PRs are `ai:ready-for-human`
5. Review those final diffs, merge manually

The developer only touches PRs at `ai:ready-for-human`.

In the **shipped implement/review/fix slice**, issues at `ai:available` can be
implemented into PRs, PRs at `ai:implemented` or `ai:revised` can be reviewed,
and PRs at `ai:changes-requested` can be fixed and sent back to `ai:revised`.
Manual invocation advances one tick; the compact `/loop` prompt self-schedules
subsequent ticks from the emitted cadence decision.

## End-to-end example

A tick is not two modes it switches between — **every tick runs HARVEST then
DISPATCH, in that order, unconditionally.** HARVEST reaps whatever engines
finished since the last tick (advancing labels); DISPATCH then launches new
engines for whatever is now actionable, up to `MAX_IN_FLIGHT`. Engines run
*between* ticks as detached processes; the tick itself never waits on one.

The walkthrough below follows two issues (#50, #51) through the pipeline with
`MAX_IN_FLIGHT=3`. "in-flight" is this machine's own dispatch markers — those
classified `live`, `launching`, or `hung`, or whose process group is still
alive — keyed by `(kind, number, stage label)`. A shared `ai:` in-flight label
holds no slot; see "Loop and cadence" above for why.

| Tick | HARVEST (reap finished) | DISPATCH (launch new) | State after tick |
|---|---|---|---|
| 1 | nothing in-flight | #50, #51 are `ai:available` → launch 2 implementers, each in its own worktree | #50, #51 `ai:implementing`, 2 in-flight |
| 2 | both still running | at cap-aware idle; nothing new actionable | unchanged, 2 in-flight; wake ~270s |
| 3 | **#50's** `result.json` has `IMPLEMENTATION_COMPLETE` → create PR #100, label `ai:implemented`; #51 still running | PR #100 `ai:implemented` → launch reviewer (Codex) | #100 `ai:reviewing`, #51 `ai:implementing`, 2 in-flight |
| 4 | **both** finished this interval: #51 → PR #101 `ai:implemented`; #100's `review.json` verdict `changes_requested` → post review comment, then label `ai:changes-requested` | #101 → reviewer; #100 → fixer | #100 `ai:fixing`, #101 `ai:reviewing`, 2 in-flight |
| 5 | #100 fixer → `FIXES_PUSHED_RECHECK_NEEDED`, post the fixer-run comment, label `ai:revised`, bump iter→1 | #100 `ai:revised` → reviewer again | #100 `ai:reviewing`, #101 `ai:reviewing` |
| 6 | #100 review → verdict `ready_for_human` → `ai:ready-for-human`; #101 review → verdict `ready_for_human` → `ai:ready-for-human` | nothing actionable | both terminal, **quiescent** → heartbeat ~1800s |

**Two results in the same tick (tick 4 above).** Results never collide because
each dispatch runs in its own worktree and writes its own result file
(`.claude/worktrees/pr-100/review.json`, `.claude/worktrees/pr-101/result.json`).
HARVEST iterates over *all* worktree directories and processes each result
independently in one pass, so N engines finishing in the same interval are all
reaped in the next tick — there is no single shared `result.json` to contend
over. The only ordering that matters is HARVEST-before-DISPATCH within the tick,
so freed slots and advanced labels are visible to DISPATCH immediately.

## Risks and mitigations

**Engine output unparseable:** Headless agents sometimes produce malformed
output. Mitigation: fall back to conservative sentinel interpretation
(escalate rather than guess). The existing hook system proves that
`claude -p --output-format json` reliably produces parseable output.

**Runaway review/fix loops:** Reviewer and fixer disagree endlessly.
Mitigation: iteration cap (default 5). At threshold, label transitions to
`ai:max-iterations` and the developer intervenes.

**Stale, failed, and hung dispatches:** An engine may crash mid-run without a valid
dispatch-bound failure envelope or wedge while still alive. Mitigation: the tick
checks PID liveness, completion-envelope shape/binding, and the `deadline` in
`dispatch.json`. A dead reviewer with a strict non-zero failure envelope is archived
and routed to `ai:needs-human-decision`; it is not stale or re-queued. Dead PID with
no valid success or failure envelope = stale, cleaned up and re-queued by reverting
the in-flight label to its pre-dispatch label (see *Dispatch marker lifecycle* step 5a).
Live PID past its deadline = hung, escalated
to `ai:needs-human-decision` and the slot freed so the loop's cadence is not
pinned at "busy" by a stuck engine.

**Context window in tick session:** in `/loop` (dynamic self-paced) mode the
ticks run in **one continuing context window**, not a fresh one per tick — the
harness summarizes the conversation as it grows rather than resetting it. So
context accumulates across ticks, and processing 5+ PRs per tick could bloat it.
Mitigation: the tick reads only small structured files (dispatch markers, label
JSON from `gh`), never full diffs or engine output — full output stays on disk
and is referenced by path. If per-tick isolation ever becomes necessary, the
alternative is to run each tick as a separate `claude -p` invocation (fresh
window every tick) instead of `/loop`, at the cost of re-establishing context
each time; the small-reads discipline makes that unnecessary for now.

**Implementer produces bad code:** Inevitable for complex issues. Mitigation:
the review stage catches it; the fix stage addresses it; `max_iterations`
caps spend; `ai:needs-human-decision` is the escape hatch. Start with
well-scoped issues.

**Permission mode for headless Claude:** implementers and fixers request `auto`
by default and inherit the operator's complete environment and settings. Tick
does not provision a scoped GitHub token or an OS sandbox for them. The explicit
allowlist pre-approves narrow commands but does not cap classifier approvals.
Use operator-owned deny rules for forbidden actions and scoped credentials to
bound remote access. Worktree isolation keeps edits separate; it does not prevent
an engine with ambient credentials from acting elsewhere. `dontAsk` remains
available when an exact pre-approval workflow is required. Neither mode proves
that a provider/model route supports unattended completion; setup's probe owns
that evidence.

## Repo strategy

The pipeline was incubated inside its first target repository and now lives in
its own repository as the source of truth. That order was deliberate: the
unknowns were behavioral, not structural — whether the `--output-schema` verdict
capture worked end-to-end against the installed Codex, whether the dynamic
cadence converged instead of thrashing, whether the state machine ever stranded
work, whether spend stayed bounded before the iteration cap. Those are only
answerable by running the loop against real PRs, and that learning cycle was far
cheaper with everything in one repo. Extracting earlier would have forced a
multi-repo abstraction to be designed before anyone knew where the real
target-vs-generic seams were.

Two properties made the extraction cheap when it came, and both are still
load-bearing constraints on new work:

1. **Every pipeline artifact lives under the `pr-pipeline-*` skill
   directories.** The tick skill carries the `lib/` CLI; the status skill is a
   thin read-only sibling. No pipeline logic is scattered elsewhere.
2. **Nothing target-specific is hard-coded inline.** Verification commands,
   review checklists, and the target's own conventions are configuration or
   prompt-level deferrals — see *What belongs to the target repo* above. The
   moment a stack name appears in engine or tick logic, the pipeline is
   installable in one repo again.

The dedicated-repo boundary matters most for the **scoped bot token**. It is the
piece of the safety model that becomes load-bearing when the loop runs truly
unattended (overnight, no human watching), which is the opposite of the
supervised first milestone: a low `MAX_IN_FLIGHT`, the developer present, one PR
taken through implement → review → fix without stranding or overspending. At
that stage the human in the loop is the blast-radius boundary; after it, the
token is.

## Future evolution

Known gaps, in the order they block adoption:

1. **Standalone install acceptance.** Operator settings live outside the install
   and the target resolves from local Git remotes. Full foreign-repository
   adoption still needs the remaining Phase 1 acceptance work in the plugin
   distribution design.
2. **Process-level install proof.** No test yet proves the *installed* plugin
   behaves identically to an in-repo copy; that needs a shim harness driving the
   shipped entry points from outside the checkout.
3. **`gh` primitives into the Python CLI.** The remaining shell-level `gh` calls
   belong in `lib/` for testability and distribution.
4. **CAS claim logic** for multi-orchestrator safety. Today a single
   orchestrator is assumed.
5. **Claude Code plugin packaging** so the pipeline installs without a manual
   `PR_PIPELINE_HOME`.
