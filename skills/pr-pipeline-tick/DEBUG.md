# PR Pipeline Tick — Debugging & Recovery Reference

Read this when a tick fails, an engine stalls, or the operator SOP says "inspect
DEBUG.md." For normal operation, use `SKILL.md` (the injected SOP). For usage
examples and observability, use `README.md`.

## Operational invariants

1. **HARVEST then DISPATCH** — every tick runs both phases in order.
2. **Fail closed on every external read** — a failed `gh`/`git` call never becomes
   valid empty state.
3. **Lease-before-label** — the dispatch marker lease is written and confirmed
   before the GitHub label is flipped.
4. **Kill only on proven identity** — `os.kill()` is only called when process
   identity verification returns `"matched"`.
5. **can_dispatch_now fails CLOSED** — one corrupt marker blocks the entire
   dispatch phase.
6. **Scratch deletion gated on label edit** — the dispatch marker survives a
   failed remote label transition.
7. **Admission counts this machine's engines** — the spend ceiling counts local
   dispatch markers only. A shared `ai:` in-flight label never occupies a slot,
   so a manual pickup or another host's dispatch cannot starve this one.
8. **`gh` exits 0 on empty results** — non-zero exit means error, not "nothing
   found."
9. **Archive before scratch deletion** — retained or escalated scratch is deleted
   only after every evidence copy and diagnostic snapshot succeeds.
10. **Targeted ticks do not mutate unrelated work** — admission still counts
    every engine this machine launched, but HARVEST, stale-label reconciliation, and
    DISPATCH side effects remain scoped to the requested issue or PR.
11. **Successful reads still validate shape** — authoritative admission queries
    must return lists of positive integer item numbers. A malformed exit-zero
    response skips dispatch exactly like a transport failure.
12. **Wake nudges are advisory only** — the `--loop` wake watcher may change
    *when* a tick runs, never what it concludes. It holds no custody, makes no
    state transition, and fails open to the cadence timer.
13. **Stall detection is detection only** — an `engine_stalled` entry never
    kills, relabels, requeues, or shortens a deadline. A stalled engine keeps
    its full timeout and every custody rule above still applies unchanged.

## Wake watcher (`--loop` only)

In `--loop` mode a daemon thread polls every 5s and ends the inter-tick sleep
early on engine-completion evidence or a
`.claude/worktrees/.tick-state/wake-request` file (consumed by unlink). The tick
that follows is the normal full HARVEST → DISPATCH re-read.

Debugging notes:

- `wake_watcher_failed` is appended to the newest tick log rather than opening
  a competing log: during a tick that is the active log; between ticks the
  entry follows the prior tick's `tick_complete`. A new log is created only
  when none exists yet (the thread-start-failure path before the first tick).
  It means the watcher is gone for the rest of that loop process. This is
  **not** an incident: the loop keeps running on its cadence schedule, only
  without early wakes. Restart the loop to restore them.
- Completion evidence is a wrapper-published `completion.json`/`review.json`
  (reviewers **only** — a wrapperless engine could write either one itself, so
  neither is read for implement/fix), or a marker whose recorded PID **and**
  process group are dead; the liveness check runs for every stage, and only
  validated positive identifiers are
  probed. A dead wrapper PID does not release custody while its paid child group
  lives. An overdue group with unprovable wrapper identity escalates to
  `ai:needs-human-decision` without an unsafe kill; later ticks retain its
  marker and scratch and count it against `MAX_IN_FLIGHT` until group death is
  proved. That escalation also records the engine as **held**, which is what
  lets the loop stop instead of supervising a slot forever — see
  [Held engines](#held-engines). A non-empty `result.json` is
  deliberately *not* evidence: it is a stdout redirect written in place with
  no atomic rename, so a read can catch a partial write and it never proves
  exit. It also matches what harvest requires — a wrapperless result counts
  only alongside proven process-tree death.
- Each dispatch identity nudges at most once per loop process, so a marker that
  cannot be harvested (corrupt, custody-retained) cannot hold the loop in a hot
  cycle. It will still be revisited on the normal cadence.
- A nudge-initiated tick waits out a 10s floor from the end of the previous
  tick unless the scheduled cadence deadline arrives first. That deadline
  remains the upper bound. The nudged tick advances neither quiescent counter
  — not the loop's in-memory streak, and not
  `.tick-state/quiescent-counter.txt`. If a loop stops on quiescence, or a
  later one-shot tick reports `stop=true`, the documented N × 1800s of quiet
  really did elapse.
- Suspect the watcher is over-nudging? `jq -s '[.[] | select(.action=="tick_complete")]' .claude/worktrees/.tick-log/tick-*.jsonl`
  and check tick spacing; nudged ticks appear as normal ticks closer together
  than the preceding `cadence.next_wake_seconds`.

## Engine progress heartbeats (`engine_stalled`)

`kill -0` proves an engine process exists; it does not prove the engine is doing
anything. An engine wedged on a network call that never returns, a permission
wait, or a silent retry loop passes every liveness probe and burns its whole
60-minute deadline before the pipeline reacts.

The implementer/fixer stream wrapper already owns the engine's stdout, so it
bumps the mtime of a `heartbeat` file next to that engine's `result.json` as
output events arrive (throttled to at most one write per 5s; the mtime *is* the
whole payload). A heartbeat write failure is swallowed — it must never affect
whether the engine's result reaches harvest.

A marker whose engine is still `live` but whose heartbeat has not moved for
**30 minutes** (`ENGINE_STALL_SECONDS`) is reported as a stall.

**Both loop paths produce this signal.** Every tick runs the scan at the start
of its body, so one-shot ticks scheduled from the `tick-cadence:` line — the
mode `SKILL.md` prescribes — report a stall exactly like `--loop` does. Under
`--loop` the wake watcher additionally polls the same scan every 5s, which only
gets the tick there sooner. That is the split throughout: the nudge half is
`--loop` only, the reporting half is not.

That threshold is 3x the longest silence a *healthy* engine can produce
(`MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS`). Engines emit stream events only at
message boundaries, so heartbeat silence is *at least* tool-call duration —
the model turn that consumes the tool result and any internal 429/529 backoff
add to it — and Claude Code caps a single `Bash` call, `Bash(uv run pytest *)`
being allowlisted, at 10 minutes. A threshold set *at* that bound would report
a healthy engine on its longest legal call; the multiple is what absorbs the
turn latency stacked on top of it.

Debugging notes:

- `engine_stalled` carries `progress_age_seconds`, `progress_source`,
  `seconds_to_deadline`, and the marker's `identity`, so the entry alone says
  which engine, how long it has been quiet, and how much deadline is left:

  ```bash
  jq -s '[.[] | select(.action=="engine_stalled")]' .claude/worktrees/.tick-log/tick-*.jsonl
  ```

- It is **advisory**. Nothing is killed, relabelled, requeued, or deadline-
  shortened; the tick that the stall wakes reaches exactly the conclusions it
  would have reached on its normal schedule. Acting on a stall changes spend and
  custody semantics and is deliberately not part of this signal. The operator's
  move is to read that worktree's `stderr` file and decide.
- **Steady-state de-duplication per stall episode.** The progress timestamp
  already evaluated is remembered per dispatch identity in
  `.claude/worktrees/.tick-state/stall-episodes.json`, so a wedged engine does
  not re-notify every 5s poll — or, under one-shot ticks, once every ~270s
  cadence interval for the ~30 minutes left on its deadline. Refused verdicts
  are remembered too, so a quiet marker whose engine is *not* live costs one
  `ps` probe rather than one per scan for as long as the directory survives. If
  the heartbeat starts advancing again, detection re-arms — a later silence at
  a newer timestamp reports as a fresh episode.

  Report attempts use at-least-once retry semantics across process death. A
  live-stall timestamp is checkpointed only after its operator report returns,
  then before the scan advances to another marker. That closes the later-marker
  replay window without turning an exit before the post-report checkpoint
  commits into a permanently silent stall; the attempt can therefore retry on
  the next scan. The macOS notification itself remains best effort.

  The file is on disk for the same reason `quiescent-counter.txt` is: the two
  executable loop paths are different process models, and in-process memory
  cannot dedupe across one-shot ticks. It needs no pruning rules of its own —
  each scan drops entries for dispatches that no longer exist, so it stays
  bounded by in-flight work. Delete it freely: the cost is at most one repeated
  advisory report, which is also what an unreadable or malformed file gets.

  Under `--loop` the watcher and the tick both reach the scan, so the
  read-modify-write is serialized by `stall-episodes.lock` beside it. A caller
  that cannot take that lock skips the pass — the caller holding it is running
  the identical scan — so a busy lock is not an error and never appears in the
  log.
- **`stall_scan_failed` means the scan died, not the watcher.** The scan reads
  arbitrary marker input, so it is wrapped at both call sites: a raise leaves
  the tick, the completion nudges, and the wake-request channel all running.
  The watcher logs one entry per exception class rather than one per 5s poll,
  so a marker that raises the same way forever costs one line while a new
  failure still surfaces; the tick logs at most one per tick. Contrast
  `wake_watcher_failed`, which means the watcher is gone and `--loop` is back
  on its bare cadence timer.
- **No heartbeat file is not a stall.** It means the engine has not reached its
  first output event, or the marker predates heartbeats. Those fall back to the
  marker's `start_time` plus a 5-minute startup grace
  (`ENGINE_STALL_STARTUP_GRACE_SECONDS`) on top of the threshold.
- **Only implement/fix stages.** The reviewer wrapper deliberately discards
  engine output, so there is no progress signal to read; its stall cost is
  already bounded by the 20-minute review timeout, and inventing a weaker proxy
  for it is deferred rather than guessed at.
- The stall check runs only after the cheap mtime test says something is wrong,
  and then uses the same identity-verified classification harvest trusts. A
  reused PID or an already-`hung` marker is somebody else's business and is not
  reported as a stall. The identity proof is *refutation*, not confirmation: if
  `ps` cannot be read at all, `_classify_marker` records `identity: "unknown"`
  and still counts the marker live, so a stall can be reported on a liveness
  that was merely never disproved. The entry carries that `identity` field for
  exactly this reason — check it before trusting the row.
- **The dashboard shows the same verdict, not its own.**
  `pr-pipeline-status` shells out to `tick.py engine-progress --wt-root PATH`
  rather than re-deriving one in bash, which could prove neither process
  identity nor the thresholds. That call takes no pipeline lock and makes no
  pipeline state transition, so it is safe against a live tick — though `uv run`
  may sync the project venv, so it is not literally write-free. A `?` in its
  progress column means the call failed — the dashboard is declining to guess,
  not reporting a healthy engine. Reproduce it by hand with:

  ```bash
  uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py engine-progress \
    --wt-root .claude/worktrees
  ```

  Run it through `uv`, not a bare `python3`: tick.py needs 3.11+ for `tomllib`
  and the ambient `python3` may be older (macOS ships 3.9), which fails the
  call and renders `?` on every row.

- Seeing `engine_stalled` for engines that finish fine? The threshold is below
  the longest legitimate silence for your workload. Raise
  `ENGINE_STALL_SECONDS`; nothing else depends on it — the dashboard reads the
  same constant through the command above. Raise
  `MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS` with it if the new floor comes from a
  slower tool call rather than from a longer chain of them, and keep the sum of
  threshold plus startup grace under the 60-minute engine timeout or the stall
  can never be reported before `hung` escalates. Both bounds are asserted in
  `test_tick.py`.

## Retained scratch recovery

`retained_scratch` identifies a worktree directory with no live engine.
Redispatch archives it under `.claude/worktrees/.harvest-evidence/` before cleanup;
markerless directories use the `retained-markerless` suffix (plus a numeric suffix
for later incidents). Every completed bundle contains the original tree under
`scratch/`, so unknown and untracked recovery files survive alongside the copied
top-level evidence. `archive_failed` means the source remains authoritative and
must not be deleted. Reviewer archives must contain `review.json` and
`completion.json` when present. Repair disk space or permissions, rerun the tick,
and confirm the archive before clearing anything. A bundle without `scratch/` is
an incomplete pre-transition copy, not proof that the source can be removed.

Corrupt or unclassifiable markers retain custody because process identity is
unknown. When dispatch encounters one on a pickup item, it moves the item to
`ai:needs-human-decision` without deleting scratch. Inspect the marker and process
state manually; never relabel for redispatch until custody is resolved.

`terminal_intent_unresolved` is the same posture for controller state. A
terminal intent under `.claude/worktrees/.terminal/` is the recovery owner for a
transition whose comment is already live on the item, so a record that cannot be
resolved — unreadable, malformed, an unknown `kind`, or a dispatch marker that
will not read — executes nothing and retires nothing. Ordinary harvest is held
off for that item while it stands, so it repeats every tick until an operator
acts; it does not clear on its own:

```bash
grep -hE '"action": "(terminal_intent_unresolved|terminal_intent_failed|terminal_relabel_ambiguous|terminal_relabel_held)"' \
  .claude/worktrees/.tick-log/tick-*.jsonl
```

Read the intent file and the item's comments together. If the transition it
records is already visible on GitHub, delete the intent — the item is where the
intent wanted it. `terminal_intent_failed` is the opposite and needs no cleanup:
the decision could not be written, so nothing was published and the next tick
re-decides. `terminal_relabel_ambiguous` is informational — a later actor owned
the label, automation finished its local custody and left that label alone.

`terminal_relabel_held` is also informational, and is the one that will recur in
normal operation: a hold arrived while a terminal transition was still pending,
so the transition can never converge (convergence refuses a hold by design). The
tick drops only its own in-flight label and finishes local custody, leaving the
hold to own the item. The published explanation still names the terminal label
the transition decided — the item is at the operator's hold instead, which means
the same thing. The alternative is worse: re-attempting the converge every tick
never finishes local custody, so the dispatch marker stays in the admission
root and costs one of the `MAX_IN_FLIGHT` slots with no engine behind it.

Stale in-flight labels are reconciled only when the local marker scan and required
GitHub reads succeed. An `ai:implementing` issue loses that label only after an
open closing PR is confirmed. A failed lookup is logged as `side_effect_failed`;
an invalid response shape is logged as `stale_reconcile_skipped`. Both leave the
label unchanged for the next tick.

A dispatch-bound failed reviewer completion is terminal evidence, not a redispatch
candidate. HARVEST first archives `completion.json`, then routes the PR from
`ai:reviewing` to `ai:needs-human-decision`; it must not be redispatched. On archive failure,
keep the in-flight label and scratch authoritative. On relabel failure,
keep both the evidence archive and scratch under pipeline custody; rerun the tick
after the remote failure clears. If final archive completion fails after relabel,
the terminal label remains authoritative and scratch is retained for recovery.

## Held engines

GitHub's `ai:needs-human-decision` and `ai:blocked` labels are the durable record
that a dispatch is awaiting a human decision. There is no local receipt: on every
cadence observation the tick re-reads the labels of each occupied identity with a
bounded `gh <kind> view --json labels`, which resolves open, closed, and merged
items alike.

A held slot keeps counting against `MAX_IN_FLIGHT` (a paid process may still be
running under it) but is excluded from loop quiescence, so a queue of only held
items can still reach the terminal stop. The stop notification, the
`loop_quiescent_stop` entry, and the `tick-cadence:` reason all name the held
items — the last of those is what the one-shot `/loop` path notifies from, and it
names them on any wedged tick, not just a stop.

The exclusion is narrower than "carries a hold". Only a held item the pipeline
has *no pending action on* is discounted, and that is decided by liveness
rather than by the classify state. While the recorded process or its group is
alive the loop stays awake — its result must be harvested or its process
terminated, and stopping would strand it with nobody to collect it. That
includes a `complete` marker whose group is still running: the envelope is
published but the hold defers the archive until death is proven.

The bound is stated on the awake side, in one predicate: `_pipeline_actionable`
keeps a held item counting only while its recorded process or group is alive
and — for a `hung` marker — only when identity proved `matched`. Everything else
a hold covers is discounted, so a new unactionable shape needs no new rule. The
ones that exist today: the escalate-and-leave `hung` (alive, but never
signallable), a pre-spawn `launching` lease (it records no PID, so death can
never be proven and the state never changes), and markerless scratch.

A hold transfers **workflow** custody, not **process** custody. While it is
active the tick will not escalate the item, mutate its labels, advance its
stage, or archive/remove scratch, even if a later probe re-proves the PID —
`workflow_hold_retained` records that gate firing.

Two actions record a refusal, at two different layers, and they are easy to
confuse. `workflow_hold_retained` is the reconcile gate above: the tick saw a
hold and left the item alone. `human_hold_retained` is one layer down in label
convergence — a mutation was actually attempted and refused because a hold was
already on the remote. The second also fires when the target *is* the hold that
is already present: automation cannot prove it created that hold, so it reports
failure rather than continuing into scratch cleanup on a human's label. A held
item whose bookkeeping stalls that way is completed by the group-death cleanup
path, not by a later escalation.

It will still terminate an *overdue* engine whose identity is proven `matched`
(`hung_engine_killed_under_hold`). The operator never held process custody: they
have no PID and cannot signal it, so a hold cannot silently extend the dispatch
deadline. The kill advances no decision — labels and scratch are untouched, so
all evidence and the pending choice survive. An overdue engine whose identity is
*unprovable* is still left alone under the #578 escalate-and-leave contract,
because its PID may have been reused.

Proven group death is the only release. For resultless scratch, the tick emits a
pure `--remove-label` for the pipeline's own in-flight label
(`in_flight_label_released`) before it archives and removes the scratch. For a
completed result, it first prepares evidence and publishes the validated engine
outcome, then performs the same pure removal, then finalizes the archive. The hold
is never named in that mutation. The removal is confirmed by a read-back; if
either the edit or its confirmation fails, the tick logs `label_release_failed`,
keeps the source marker, and retries next tick, because the marker is the only
thing that can re-prove death.

Every release names the hold that justifies it, on all three paths — resultless
cleanup, held-result publication, and a terminal transition a hold has taken the
label of. That hold is point-read immediately before the removal and again in
the read-back after it, so a release decided a tick ago never lands on an item
whose hold has since been lifted. A release with no hold to name is refused
outright rather than falling back to an unguarded removal: `label_release_failed`
with `details.required_hold_label` empty means a decision reached the release
without the provenance that makes it legitimate, and that is a bug to report
rather than an operator action.

Past the release a refusal is no longer free — the in-flight label is already
gone. So when an operator lifts the hold inside that window, the tick puts the
released label back and confirms it, logging `label_release_failed` with
`details.in_flight_restored`. It skips the restore only when the read names some
other workflow label already owning the item; re-adding `ai:fixing` on top of a
human's transition would manufacture a dual-label state. `in_flight_restored:
false` is the one to chase: the removal landed, the restore did not, and the
item may be sitting with no workflow label at all.

All three release paths read the hold once more before the local custody move
that destroys the marker, because the release's own read-back proves only that
the hold outlived the removal — not that it outlives the archive and delete that
follow. The completed-result path needs the read for a second reason: with an
empty `release_label` there is no release, and so no read-back to stand in for
it at all. On the terminal-transition path the restore does more than prevent an
unlabelled item: with the hold gone and the in-flight label back, the next tick
reads the transition as pending again and converges it as originally decided.

The hold forbids advancing the stage, so an engine that *finished* under one is
not harvested. Its validated outcome is nevertheless published before the
in-flight label is released. `held_result_published` records the outcome after
the archive lands; `held_result_comment_failed` means the post failed and all
source custody was retained for retry.

Admission identities are `(kind, number, stage label)`, so same-number issues and
PRs—and accidentally overlapping reviewer/fixer dispatches—remain separate paid
slots. Cadence discounts only occupied identities whose `(kind, number)` has a
durable human hold.

```bash
# Which reconciliations left a held item alone, and which killed a runaway?
# human_hold_retained is the convergence-layer refusal — include it, or an
# item left alone by a refused mutation looks like nothing happened at all.
grep -hE '"action": "(workflow_hold_retained|human_hold_retained|hung_engine_killed_under_hold)"' \
  .claude/worktrees/.tick-log/tick-*.jsonl

# Which held slots were returned to the cap, and which releases failed?
grep -hE '"action": "(in_flight_label_released|label_release_failed)"' \
  .claude/worktrees/.tick-log/tick-*.jsonl

# Which held items published an unharvested result, or retained it after a post failure?
grep -hE '"action": "(held_result_published|held_result_comment_failed)"' \
  .claude/worktrees/.tick-log/tick-*.jsonl
```

Reading `tick-state: ... in_flight=0 ... held=1` means the loop has nothing
to supervise while one slot is still held by a process a human owns. Resolve it
by dealing with the escalated item; once the recorded process group dies, the
marker classifies `stale` and ordinary cleanup archives the scratch, which is
what returns the slot to the cap — the in-flight label is released alongside
it and holds nothing of its own. Remove or replace
the GitHub hold only when the operator has made the corresponding custody
decision.

If `held` equals `MAX_IN_FLIGHT` the pipeline cannot dispatch at all, even
though `in_flight` reads 0 — the `tick-cadence:` reason names the holders in
that state, so match them against the log and resolve one to unblock dispatch.
A held item whose scratch is already gone (`identity: markerless`) keeps its
in-flight label on purpose: with no marker there is nothing to probe, so death
cannot be proven. Clearing the hold hands it back to ordinary stale-label
reconciliation.

## Why `dangerouslyDisableSandbox: true`

The tick invocation must use `dangerouslyDisableSandbox: true`. The Bash tool's
command-pattern allowlist and the OS-level sandbox (macOS `sandbox-exec`) are
separate concerns — subprocesses bypass the allowlist (spawned from Python, not
the Bash tool) but inherit the OS-level sandbox. Under the default sandbox:

1. **Process identity is unverifiable.** `ps -o lstart=` is denied, so
   `verify_identity` returns `"unknown"` for every marker, and Invariant 4
   blocks all reaping.
2. **Kill signals are undeliverable.** Cross-boundary `SIGTERM`/`SIGKILL` via
   `os.killpg` is denied, so timeout enforcement cannot terminate anything.
   `pid_is_alive` treats `PermissionError` as alive, so engines appear live
   forever.
3. **Engines inherit the sandbox.** Dispatched engines lose network access to
   non-allowlisted LLM endpoints and `~/.codex` writes, so they stall silently
   after the label flip.

Scope: the tick process and its children, not the entire session.

## Permission requirements for unattended runs

Add these to `.claude/settings.local.json` (`permissions.allow`):

```jsonc
// The tick itself — one uv run for one-shot or --loop mode
"Bash(uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py *)",
// Preflight check after install or Codex version change
"Bash(bash $PR_PIPELINE_HOME/skills/pr-pipeline-tick/preflight.sh)",
```

Without these, a one-shot tick triggers a permission prompt and unattended loops
stall.

## Reviewer sandbox posture

The reviewer engine runs bare `codex exec` unattended with a read-only sandbox
(`-s read-only`, the same mode preflight probes), not
`--dangerously-bypass-approvals-and-sandbox`. The reviewer reads the PR worktree
and emits a schema-validated verdict; harvest posts the review comment and flips
labels, so the engine needs no write, push, or `gh` access.

The reviewer also passes `--ignore-rules`, `--ignore-user-config`, a scrubbed
`HOME`/`CODEX_HOME` containing only `auth.json`, and explicit model/reasoning
settings. Bare `codex exec` is deliberate: on the pinned CLI, the review
subcommand writes prose into every output channel and ignores `--output-schema`.
The base SHA travels in the prompt rather than `--base` because `--base` is
review-subcommand-only.

Residual risk: the read-only sandbox blocks writes and command-level network, but
model-run commands can still read the filesystem, and posted review text is
public. Until externally sandboxed review exists, the trusted-author/trusted-repo
dispatch gate is the primary control.

## Startup failure diagnostics

Every tick log starts with:

- `startup_context` — Python version, cwd, PATH directories, tick PID.
- `startup_probe` — whether `gh`, `git`, and `codex` resolved on PATH.

A `startup_probe_failed` entry exits with code 3 before HARVEST. If the tick log
is empty or missing, the process never reached `main()`; check OS-level spawn
failures such as E2BIG, a missing Python executable, or broken `uv`.

## Debugging pointers

| What | Where |
|---|---|
| Decision logic | `lib/pipeline.py` docstrings |
| Orchestrator | `lib/tick.py` |
| Process control | `lib/process_control.py` |
| Tick audit trail | `.claude/worktrees/.tick-log/tick-*.jsonl` |
| Engine or wrapper stderr | Per-worktree `stderr.log`; reviewer has bounded sanitized wrapper diagnostics only, while implementer/fixer engine stderr remains available |
| Dashboard | `/pr-pipeline-status` or `bash $PR_PIPELINE_HOME/skills/pr-pipeline-status/status.sh` |
| Startup failures | First two entries of the latest tick log (see above) |
| OS-level spawn failures | `dmesg` for E2BIG, `ulimit -a` for arg limits |

Reviewer JSONL stdout is drained through a bounded sanitizer. Raw events and
candidate-controlled output are discarded; only normalized skill-file identities and
parse counters can reach `review-observation.json` and the
`reviewer_skill_observation` tick entry. This is successful command-reference evidence,
not an explicit semantic invocation signal. Reviewer child stderr still goes to
`DEVNULL`, so `stderr.log` contains only a bounded startup error class or fixed
`EngineExitError`.
Use `completion.json` for structured failure evidence. Implementer/fixer engine stderr
is still captured in `stderr.log`.

## Constants and boundaries

- Worktree root: `.claude/worktrees`
- Tick log directory: `.claude/worktrees/.tick-log`
- Tick mutex: `.claude/worktrees/.tick.lock`
- Wake-request file: `.claude/worktrees/.tick-state/wake-request`
- Quiescent streak: `.claude/worktrees/.tick-state/quiescent-counter.txt`
  (counts heartbeat intervals, not ticks — see the wake watcher section)
- Wake watcher poll: 5s; nudge debounce floor: 10s, capped by the scheduled
  cadence deadline (`--loop` only)
- Engine progress heartbeat: `<worktree>/heartbeat` (mtime only), written at
  most once per 5s by the implement/fix stream wrapper
- Stall threshold: 1800s of no engine output (3x the 600s longest legitimate
  silence, `MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS`); 300s extra startup grace
  when no heartbeat file exists yet
- Admission cap: `MAX_IN_FLIGHT=3`
- Timeouts: reviewer 1200s, implementer 3600s, fixer 3600s
- Fixer skill bindings: `tick.FIXER_SKILLS`, resolved as
  `<fixer worktree>/.claude/skills/<name>/SKILL.md` before launch; unreadable
  logs `skill_unresolved` and escalates (see README "Prompt-bound skills")
- Fixer injected context: PR body + every `closingIssuesReferences` issue body,
  with the cut stated inline; the body gets `tick.FIXER_CONTEXT_BUDGET=20000`
  and the linked issues share a second one, shortest-first so no issue is
  dropped; a failed or mistyped read retains the PR and logs a `skip`
- Fixer change-summary budget: `pipeline.FIX_SUMMARY_BUDGET=3000`, substituted
  into the fixer prompt; the published excerpt is capped at that plus
  `_FIX_REPORT_HEADROOM`
- Fixer PR-body revision: engine writes `<fixer worktree>/.pr-body-revision.md`
  after its final push, first line `pr-body-revision: <dispatch_id>`, second line
  `pr-body-revision-head: <pushed OID>`; the harvest validates it against the
  marker's `pr_body` provenance block (`digest`, `summary_closes`, `closes`,
  `truncated` — all four or the block is corrupt), re-reads labels/head/body
  immediately before the write, and applies it with `gh pr edit --body-file`
  before the `ai:revised` relabel. A hold, head, or body change seen at that
  re-read logs `pr_body_revision_withheld` and defers the whole harvest. Body
  staged under `.claude/worktrees/.receipts/`, never the worktree. Max body
  length `tick._PR_BODY_MAX_CHARS=65536`, enforced against both the character
  count and the UTF-8 byte length; the classifier reads at most
  `tick._PR_BODY_REVISION_READ_LIMIT`
- Trusted reviewer gate: author `kwuBuild`, head repo `kwuBuild/sworn`
- Reviewer Codex floor: `codex-cli 0.142.5`
