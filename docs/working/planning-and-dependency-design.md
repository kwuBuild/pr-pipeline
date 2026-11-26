# Planning and Dependency Design

**Status: proposal.** Nothing here is shipped. For current runtime behavior, see
[architecture](../architecture.md), [internals](pr-pipeline-internals.md), and the
[tick README](../../skills/tick/README.md).

This proposal extends `pr-pipeline` upstream of `ai:available`. An operator
describes a goal, and the pipeline does three things:

1. It drafts the goal as GitHub issues, each with its definition of done.
2. A reviewer checks the issues as one plan, and the operator approves each
   issue.
3. `tick` implements the approved issues in dependency order. GitHub's native
   `blockedBy` edges set the order. When an approved issue's blockers are
   complete, the pipeline tells the operator, and the issue starts only after
   the operator confirms it.

Nothing starts without the operator's confirmation, so an unattended loop never
starts work that the operator did not expect.

**A single issue needs none of this.** An operator who creates one issue and adds
`ai:available` gets today's behavior. An issue with no `blockedBy` edges passes
every dependency check. The plan skill is optional.

## 1. Problem and scope

Today `tick` treats every `ai:available` issue as independent. A goal that
spans several issues has these problems:

- The operator splits the goal into issues and relays them for review by hand.
- `tick` dispatches dependent issues before their prerequisites, or runs them in
  parallel and causes edit collisions.
- No label says "approved, but not started".

**Goals**

- An interactive `/pr-pipeline:plan` skill takes a goal through drafting,
  holistic review, per-issue approval, and publication.
- GitHub-native primitives hold the structure and the order: a Goal issue with
  sub-issues, and `blockedBy` edges. `tick` reads GitHub's own blocked status.
  No pipeline label copies it.
- `tick` finds the approved issues that can start, and the operator confirms
  each start. The pipeline separates two situations: every blocker is complete,
  or a blocker was canceled.
- `tick` starts higher-priority ready issues first, within the existing
  `max_in_flight` cap.
- `tick` closes a Goal when all of its sub-issues are complete.
- `ai:blocked` becomes `ai:hold`, so that "blocked" refers only to GitHub
  dependencies.

**Non-goals**

- Projects v2 writes, or a backlog ordering engine. Priority labels order
  dispatch only. A Projects v2 board stays an optional view that the pipeline
  never reads.
- A second controller that writes workflow labels. The only label writers are
  `tick`, the plan skill acting for the operator, and the operator.
- Automatic promotion. `tick` never moves an issue toward implementation on its
  own.
- Unattended multi-issue publication. This is deferred to
  [Phase 3](#7-phase-3-deferred).
- A guarantee against merge conflicts from `## File ownership`. Overlap checks
  are advisory only.

## 2. Labels and state

### 2.1 Label rules

The `ai:` prefix is reserved for the pipeline. Every other label belongs to the
user. `tick` never adds, removes, or reacts to a user label, with one exception:
it reads `priority:*` labels to order dispatch. A user can put any number of
their own labels on any issue or PR.

| Label | Meaning | Added by | Removed by | `tick` use |
|---|---|---|---|---|
| `backlog` | Not approved yet: an idea, or an issue that still needs review | Operator, plan skill | Operator, plan skill | None. The plan skill can take a backlog issue as its goal. |
| `priority:P0` to `priority:P3` | Dispatch order, P0 first | Operator, plan skill | Operator | Orders ready issues (§5.4) |
| `ai:waiting` | Reviewed and approved, but not started. It starts only after the operator confirms. This covers both a blocked issue and an issue that the operator wants to start later. | Operator, plan skill | Operator, plan skill | Read only. `tick` reports when the issue can start (§5.3). |
| `ai:available` | Approved, and confirmed to start | Operator, plan skill, `tick` (existing requeue only) | `tick` at dispatch; operator; plan skill | Dispatch candidate |
| Existing stage labels, from `ai:implementing` to `ai:max-iterations` | Existing pipeline stages | `tick` | `tick` | Unchanged |
| `ai:needs-human-decision` | Hold: the pipeline needs the operator's decision | `tick`, operator | Operator | Hold, unchanged |
| `ai:hold` (renamed from `ai:blocked`) | Hold: the operator froze this item | Operator only | Operator only | Hold, unchanged |
| `ai:goal` | Goal issue: a container that is never implemented | Operator, plan skill | Nobody. It stays after the Goal closes. | Never dispatched. Found for auto-close (§5.5). |

An issue with no `ai:` label is outside the pipeline. It is either not
scheduled, or its labels already moved to its closing PR. `backlog` marks the
first case explicitly.

An `ai:available` issue with open blockers starts automatically when its
blockers complete. Only an operator who adds `ai:available` by hand creates
this case, and that label is the operator's confirmation. To be asked first, use
`ai:waiting`.

**Precedence.** `tick` applies the first rule that matches an item's `ai:`
labels:

1. A hold (`ai:hold` or `ai:needs-human-decision`) wins over everything. `tick`
   leaves the item alone.
2. An in-flight label means that an engine holds the item. This is unchanged.
3. `ai:goal` makes the issue a Goal. A Goal is never dispatched, whatever other
   labels it carries.
4. `ai:waiting` makes the issue wait for the operator. It wins over
   `ai:available`: an issue that carries both labels is an interrupted change.
   `tick` does not dispatch it, and the decision review asks about it (§6.3).
5. `ai:available` makes the issue a dispatch candidate.
6. Any other `ai:` label is unknown. Status lists an item that carries only
   unknown `ai:` labels as parked. The existing label convergence removes every
   `ai:` label except its target, so an unknown `ai:` label does not survive a
   converged stage change. For their own labels, users choose a prefix other
   than `ai:`.

### 2.2 Issue lifecycle

| From | To | Actor | When |
|---|---|---|---|
| No `ai:` label, or `backlog` | `ai:waiting` | Plan skill or operator | The operator approves the issue, but does not start it now |
| No `ai:` label, or `backlog` | `ai:available` | Plan skill or operator | The operator approves an issue with no pending blocker and starts it now, or creates a standalone issue |
| `ai:waiting` | `ai:available` | Plan skill, through the decision review | The operator confirms the start (§6.3) |
| `ai:available` | `ai:implementing`, then the existing PR pipeline | `tick` | Dispatch (§5.4) |
| Open Goal | Closed as completed | `tick` | Every sub-issue closed as `COMPLETED` (§5.5) |

`tick` never moves an issue toward implementation on its own.

### 2.3 What GitHub provides, and what `tick` adds

GitHub provides these primitives natively:

- `blockedBy` and `blocking` relationships between issues. The plan skill writes
  them with `gh issue edit --add-blocked-by` (`gh >= 2.94`).
- A Blocked badge in the GitHub UI on every issue that has an open blocker.
- The search qualifiers `is:blocked` and `is:blocking`.
- GraphQL `Issue.blockedBy`, which lists each blocker with its state and
  `stateReason`, and `issueDependenciesSummary`. In that summary, `blockedBy`
  counts open blockers, and `totalBlockedBy` counts open and closed blockers.
- `stateReason`, with four values: `COMPLETED`, `NOT_PLANNED`, `DUPLICATE`, and
  `REOPENED`. `REOPENED` applies to an open issue. A merged PR that closes an
  issue closes it as `COMPLETED`.

`tick` adds three things that GitHub does not provide:

- GitHub counts every closed blocker as done, also one closed as `NOT_PLANNED`
  or `DUPLICATE`. `tick` checks `stateReason` (§5.2).
- `is:blocked` comes from the search index, which can lag behind a change.
  `tick` reads `blockedBy` live before it reports or launches an issue (§5.2).
- GitHub does not close a parent issue when its sub-issues close. `tick` closes
  a complete Goal (§5.5).

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | Rename `ai:blocked` to `ai:hold` as a clean cut | One installation exists, so no compatibility code is needed |
| D2 | GitHub's native `blockedBy` edges and blocked status decide readiness. No pipeline label records whether an issue is blocked. | A copy of GitHub's state can drift from GitHub |
| D3 | `ai:waiting` records an approved issue that has not started. Only the operator's confirmation moves it to `ai:available`. | An unattended loop must never start work that the operator did not expect |
| D4 | The pipeline reports two separate situations: **ready** (every blocker closed as `COMPLETED`) and **not sure** (a blocker was canceled or cannot be read) | A ready issue needs only a yes. A not-sure issue needs the evidence and the choice to re-plan. |
| D5 | Each label change adds the new label first, confirms it, and then removes the old label in a second write. `ai:waiting` and the holds win over `ai:available`. | `gh` sends the add and the remove as separate mutations. With add-first and this precedence, a partial outcome leaves an extra label that never starts work. |
| D6 | `ai:goal` marks a Goal issue | `tick` never dispatches it, and uses it to find the Goals to close |
| D7 | `tick` closes a Goal when every sub-issue closed as `COMPLETED` | GitHub has no built-in auto-close for a parent issue, and a close starts no work |
| D8 | Dispatch orders ready issues by `priority:*`, then by age, within `max_in_flight` | The operator controls the order and the concurrency cap |
| D9 | The plan skill keeps no state of its own. GitHub holds the state: body markers, relationships, and labels. | The pipeline already uses labels as its source of truth. A second state store would need its own recovery rules. |
| D10 | The plan writes each issue's definition of done: Phase A of the [definition-of-done skill](../../skills/definition-of-done/SKILL.md) | The proof bar is cheapest to set before work starts, and the reviewer checks it together with the plan |
| D11 | The spec reviewer uses the pipeline's review route, not a fixed model | Review is model-agnostic, as implementation is |

## 4. Phase 0: rename `ai:blocked` to `ai:hold`

**Changes**

- `pipeline.py`: rename `LABEL_BLOCKED` to `LABEL_HOLD = "ai:hold"`.
  `HOLD_LABELS` follows.
- Update the hold call sites, the log events, `status.sh`, the prompts, and the
  operator docs that name `ai:blocked`. This includes the target-completion
  rule in the tick README.

**Rollout on this repository**

1. Stop the loop.
2. Upgrade the plugin.
3. Rename the label: `gh label edit ai:blocked --name ai:hold`. GitHub keeps a
   renamed label on every item that carries it.
4. Start the loop.

`labels.verify` refuses every tick while a vocabulary label is missing. A runner
therefore never runs against the other vocabulary.

**Verification.** The existing hold tests pass under the new name.

## 5. Phase 1: dependency-aware `tick`

This phase also applies to issues that an operator links by hand. It does not
need the plan skill. The only new label or state write that `tick` makes is the
Goal close (§5.5).

### 5.1 Order within one tick

DISPATCH gains two steps before implementer dispatch. HARVEST is unchanged.

1. Close complete Goals (§5.5).
2. Find the `ai:waiting` issues that can start, and report them (§5.3).
3. Dispatch ready `ai:available` issues in priority order (§5.4).

### 5.2 Dependency check

One shared function decides readiness for dispatch, for the readiness report,
and for cadence. If cadence counted work that dispatch refuses, the loop would
short-poll forever.

**Queries.** The queue queries add `-is:blocked` to their search. A blocked
issue therefore never takes a place in the bounded read of at most 200 matching
issues.

- Dispatch: open `ai:available` issues by the trusted author. This is the
  existing query with the filter added.
- Readiness report: open `ai:waiting` issues by the trusted author.

**Live read.** For each candidate, `tick` reads the issue's `blockedBy` list
through GraphQL, not through search. It reads every page, with each blocker's
state and `stateReason`. The first matching row decides the verdict:

| Live read | Verdict | `ai:available` issue | `ai:waiting` issue |
|---|---|---|---|
| The request failed: network error, timeout, 5xx, rate limit, or malformed response | `unknown` | Skip. The loop stays warm and never proves quiescence. | Skip. Report nothing. |
| The read succeeded, but it cannot account for a blocker: a null node, `FORBIDDEN` or `NOT_FOUND` on a blocker, or fewer nodes than `totalCount` after the last page | `unresolvable` | Skip. Report as **not sure**. | Report as **not sure** |
| A blocker is open, because the search result lagged | `waiting` | Skip | Nothing to report |
| A blocker closed as `NOT_PLANNED` or `DUPLICATE`, or with no reason | `canceled` | Skip. Report as **not sure**. | Report as **not sure** |
| No blocker, or every blocker closed as `COMPLETED` | `ready` | Dispatch | Report as **ready** |

`tick` writes nothing for any verdict. A skipped issue keeps its labels, and the
operator answers through the decision review (§6.3).

Before the implementation codes this classifier, it records GitHub's real
response for a blocker that the token cannot read.

The `-is:blocked` filter only keeps blocked issues out of the bounded read. The
live read is the authority. A search result that lags in the other direction
delays a report or a dispatch by one tick.

### 5.3 Readiness report

The report tells the operator which approved issues wait for a confirmation. It
covers the `ai:waiting` issues and the skipped `ai:available` issues from §5.2,
in two groups:

- **Ready:** every blocker closed as `COMPLETED`.
- **Not sure:** a blocker closed as `NOT_PLANNED` or `DUPLICATE`, closed with no
  reason, or cannot be read. The report names each such blocker.

The report reaches the operator in three places:

1. **A notification** when an issue first enters a group. `tick` remembers which
   issues it already reported, and in which group, in
   `.claude/worktrees/.tick-state/readiness-notified.json`. This file is
   bookkeeping only. If it is lost, a notification repeats, and no work starts.
2. **The stop notice.** When the loop stops quietly, the notice lists both
   groups.
3. **`/pr-pipeline:status`**, which shows both groups, read-only.

Neither group is cadence work, because both wait for the operator.

### 5.4 Dispatch order and launch check

Dispatch sorts the ready `ai:available` issues:

1. By priority: `priority:P0` first, then P1, P2, and P3. An issue with no
   priority label counts as P2. An issue with more than one priority label uses
   the highest.
2. Then by age: the oldest issue number first.

Dispatch starts issues in that order until it reaches `max_in_flight`, the
operator's existing concurrency cap. Only implementation dispatch uses priority.
Review and fix keep their current order. A high-priority issue does not raise
the priority of its blocker. The plan skill gives all children of one Goal the
same priority.

Dispatch skips an issue that also carries `ai:waiting` (§2.1, rule 4).

**Launch check.** After the worktree is ready and before the engine starts,
`tick` repeats the live read for that issue. If the verdict is no longer
`ready`, or the read fails, `tick` cancels the launch through the existing
launch-failure path and leaves the labels unchanged. The next tick decides
again. A blocker added after the engine starts does not stop the engine. To
stop it, the operator applies `ai:hold`.

### 5.5 Goal auto-close

Each full-queue tick reads the open `ai:goal` issues by the trusted author, with
their sub-issues, in a bounded read:

| Sub-issues | Action |
|---|---|
| None | Leave the Goal open |
| One or more are open | Leave the Goal open |
| All closed as `COMPLETED` | Close the Goal as completed, with a comment that lists the sub-issues |
| All closed, and one or more not as `COMPLETED` | Leave the Goal open. The decision review asks the operator (§6.3). |

`tick` leaves a Goal with a hold alone. A failed read changes nothing. Goal
close is not cadence work: a failed close is tried again on the next tick.

### 5.6 Scope, cadence, and restart

**Scope**

- `tick` may read any blocker, parent, or sibling.
- Dispatch and the readiness report cover only items that the run may launch:
  every candidate in a full queue, the listed issues for `--only`, and the
  target issue for a positional target. Goal close runs only in a full-queue
  tick.
- With `--only '#A'`, `tick` refuses `#A` while its blocker `#B` is open. It does
  not launch `#B` instead.
- HARVEST, reconciliation, and admission keep their current scope.

**Cadence.** An issue that waits for a blocker or for the operator is not work.
A `ready` `ai:available` issue is work. An `unknown` verdict keeps the loop
warm.

| The blocker is... | Loop behavior |
|---|---|
| Running an engine | The in-flight poll keeps the loop warm |
| Waiting for a human merge at `ai:ready-for-human`, or for manual work | The quiescent heartbeats apply: one every 1800 seconds, three times by default. If the blocker closes in that window, the next heartbeat reports the dependent as ready. |
| Closed while the loop is stopped | The next tick reports the dependent. Run `tick --loop` again after the merge. |

After the operator confirms a start, the next tick dispatches the issue. A
positional target that waits keeps polling, as every non-terminal target does
today. An `--only` list exits through its quiet streak, and the stop reason
names the waiting issues.

### 5.7 Phase 1 changes and verification

**Changes**

- `pipeline.py`: add `LABEL_WAITING` and `LABEL_GOAL` to the vocabulary, the
  dependency verdict and the `ai:waiting` precedence to the eligibility
  decision, and the priority sort key.
- `tick.py`: the `-is:blocked` filter, the shared live reader, the readiness
  report and its notification record, Goal close, the launch check, and stop
  notices that list both report groups.
- `status.sh`: show a held item only under its hold, and show both report
  groups, issues waiting for blockers, and Goals that cannot close.
- Docs: the tick README (cadence, target, and `--only`), the PR guidelines (the
  label rules in §2.1), internals, and architecture.

**Verification.** The implementation issue lists the individual cases. These
are the acceptance obligations:

- On real GitHub issues: `-is:blocked` excludes a blocked issue and includes it
  after its blocker closes. A blocker closed as `NOT_PLANNED` unblocks the
  issue in search, but the live read returns `canceled`.
- Unit tests: every verdict row, the precedence rules, the priority order, and
  agreement between cadence and dispatch on the same input.
- Subprocess tests that run the real `tick.sh` with `gh` shims: each report
  group and its single notification, no label write for any verdict, each
  Goal-close row, the launch check against a reopened blocker, each run mode,
  and a restart after a quiescent stop.
- End to end on real GitHub: `A` is blocked by `B`, `B` has `ai:available`, and
  `A` has `ai:waiting`. `tick` starts `B`. After `B`'s PR merges, `tick` reports
  `A` as ready and does not start it. After the operator confirms `A`, the next
  tick starts `A`. After `A`'s PR merges, `tick` closes the Goal.

## 6. Phase 2: interactive `/pr-pipeline:plan`

### 6.1 Workflow

1. **Intake.** The skill clarifies the scope and the acceptance criteria.
2. **Reconnaissance.** The skill inspects the configured base ref and records
   its SHA. It never assumes `main`. It also reads the open issues and Goals with
   their `blockedBy` edges, the `## File ownership` sections, and the files that
   open PRs change.
3. **Draft.**
   - The skill prefers independent vertical slices. It adds `blockedBy` only
     for a real interface, schema, or sequencing dependency.
   - A one-issue goal becomes one issue that follows the
     [issue-creation guideline](pr-guidelines.md#issue-creation), with no Goal.
   - A larger goal becomes a Goal issue (`ai:goal`, the rationale, the base SHA,
     and a Mermaid graph) and bounded sub-issues with their `blockedBy` edges.
   - Each issue's `## Done when` section carries its definition of done: the
     Phase A declaration (entry point and mode, invariant and owner, shared
     boundary, and engine contract), or the line
     `Definition of done: n/a — <reason>`.
   - Each issue gets the priority label that the operator chooses. All children
     of one Goal share it.
   - File-ownership overlaps between issues that can run at the same time are
     shown as advisory warnings.
4. **Holistic review.** One read-only review covers the whole plan: the Goal,
   every issue, the edges, the definitions of done, and the base SHA. The skill
   revises the plan and runs the review again, with a round limit, until no
   blockers or majors remain. An edit after the review that changes an
   interface, an edge, or a definition of done needs another review round.
5. **Approve one by one.** The skill shows each issue with its review result.
   The operator chooses one answer per issue:
   - **Start now:** the issue gets `ai:available`. The skill offers this answer
     only when the issue has no blocker that is still open or not complete.
   - **Approve, start later:** the issue gets `ai:waiting`.
   - **Backlog:** the issue gets `backlog`. It is not approved.
   - **Edit:** the plan returns to step 4.
   - **Drop:** the issue is not published.

   The skill refuses to approve an issue whose planned blocker is in the backlog
   or dropped, unless the operator removes or replaces that edge.
6. **Publish** under the operator's login, through §6.2. The existing author
   trust check depends on that login.

**Reviewer route.** The spec reviewer uses the PR reviewer's route and
isolation: a scrubbed environment, a read-only sandbox, output accepted only
against a schema, and no GitHub write authority. It receives only issue content
that the operator wrote, as the `trusted_review_candidate` gate requires. Other
open work appears only as its number, title, and file ownership. Today the
review stage accepts only `codex` (`_VALID_ENGINES_BY_STAGE`), so the spec
reviewer runs Codex until a second review engine exists. Adding one is a
separate change, shared with the PR reviewer, because each engine needs its own
read-only isolation.

### 6.2 Publication

GitHub has no multi-issue transaction. The skill relies on two facts instead of
its own state:

- `tick` acts only on issues with `ai:` labels. An issue without them is
  outside the pipeline.
- While the skill holds the tick lock (`.claude/worktrees/.tick.lock`), no tick
  runs on this machine. A tick that starts meanwhile exits `5`, and the loop
  retries on its short poll.

The skill saves the approved plan text to
`.claude/worktrees/.plan-drafts/<plan-id>.md`, so that a rerun publishes the
same text. The file holds content only. GitHub holds the state.

**Steps.** The skill holds the tick lock for all of them.

1. Create each issue with `--title` and `--body` only. Every body carries
   `<!-- pr-pipeline:plan=<plan-id> member=<key> -->`. Labels stay off, so the
   issue stays outside the pipeline. The type, parent, and dependency flags stay
   off too: in gh 2.94.0 they go in a second request after the create, and gh
   prints the URL only after that request succeeds
   ([source](https://github.com/cli/cli/blob/v2.94.0/pkg/cmd/issue/create/create.go#L409-L424)).
2. If a create fails without a URL, find the issue by its marker through the
   REST issue list
   (`GET /repos/{o}/{r}/issues?creator=<login>&state=all&sort=created`), not
   through search, which lags. Retry the create only when no issue has the
   marker.
3. Add each parent and `blockedBy` relationship that does not exist yet. Then add
   the Mermaid graph to the Goal body.
4. Read the whole plan back from GitHub. Each marker matches one issue, each
   body is as drafted, the Goal's sub-issues are exactly the planned issues,
   each issue's `blockedBy` set is exactly the planned set, and no member has an
   `ai:` label. If anything differs, stop and show the difference.
5. Apply the labels: `ai:goal` on the Goal, then each issue's approved label
   (`ai:available`, `ai:waiting`, or `backlog`) and its priority label. Read each
   label back.
6. Release the tick lock.

A rerun repeats the same steps. Each step reads GitHub first and skips work that
is already done, so a crash at any point needs no other recovery. An issue that
got its label before a crash already had all its edges (step 4), so `tick`
cannot start it early. The skill asks the operator not to edit plan issues
during publication.

**Residual risk.** A create whose response was lost could land after the lookup
in step 2. Step 4 then finds two issues with one marker, and it stops.

### 6.3 Decision review

Running `/pr-pipeline:plan` without a goal starts a decision review. The skill
reads GitHub live, finds every question that waits for the operator, and asks
them in this order:

| Situation | Question | Answers |
|---|---|---|
| **Ready:** an `ai:waiting` issue whose blockers all closed as `COMPLETED` | Start it now? The skill lists all ready issues together, so the operator can start them all at once or one by one. | **Start:** add `ai:available`, confirm, remove `ai:waiting`. **Not yet:** no change. **Backlog:** add `backlog`, confirm, remove `ai:waiting`. **Drop:** close the issue as not planned. |
| **Not sure:** an `ai:waiting` or skipped `ai:available` issue with a blocker that was canceled or cannot be read | The skill shows each such blocker, its `stateReason` or error, and its title. Start it anyway, or re-plan? | **Start anyway:** remove each canceled or unreadable blocker edge, because that dependency no longer applies. Then the issue gets `ai:available`, or keeps it. The dependency check then finds no unfinished blocker. **Re-plan:** replace the blocker edge with an edge to the issue that now does that work; the issue keeps or gets `ai:waiting`. **Backlog** or **Drop**, as above. |
| **Interrupted change:** an issue with both `ai:waiting` and `ai:available` | Which state is correct? | Keep one label and remove the other |
| **Goal not sure:** a Goal whose sub-issues are all closed, but not all as `COMPLETED` | Close the Goal? | **Close as completed**, **close as not planned**, or **add work**, which returns to planning |

Every label change follows D5. The skill holds the tick lock while it writes,
and it reads each label back. Before it adds `ai:available`, it refuses an issue
that already has an open or merged closing PR, so an answer never starts a
second implementation.

### 6.4 Phase 2 changes and verification

**Changes**

- `skills/plan/SKILL.md`: the procedures in §6.1 to §6.3.
- `skills/plan/lib/`: a standard-library helper for the tick lock, the marker
  lookup, the relationship writes, the read-back, and the label changes. The
  helper makes the publication and answer rules testable against `gh` shims.
- `skills/plan/prompts/spec-reviewer.txt` and its output schema.
- `skills/definition-of-done/SKILL.md`: when the issue already carries a Phase A
  declaration, the implementer checks it against the code before the first edit
  and records any correction, instead of writing a new declaration. Phase B is
  unchanged.
- `docs/working/pr-guidelines.md`: the `## Done when` section carries the
  declaration.

**Verification**

- Publication against `gh` shims: a lost create response, a relationship
  failure, a crash between any two steps followed by a rerun, a rerun after full
  success that makes no writes, and a stop on a duplicate marker.
- Decision review: each situation and each answer, a partial label change, and
  the refusal when a closing PR exists.
- End to end on real GitHub: plan a two-issue goal where `A` is blocked by `B`,
  approve both, and confirm the Phase 1 end-to-end result.

### 6.5 Lifecycle example

Goal `G` has the sub-issues `A`, `B`, and `C`. `A` is blocked by `B`.

| Event | Result |
|---|---|
| The operator starts `B` now, approves `A` to start later, and puts `C` in the backlog | `G` has `ai:goal`, `B` has `ai:available`, `A` has `ai:waiting`, and `C` has `backlog`. `tick` starts `B`. |
| `B`'s PR reaches `ai:ready-for-human` | `A` waits, and the loop sends heartbeats (§5.6) |
| The operator merges `B`'s PR, and `B` closes as `COMPLETED` | The next tick reports `A` as **ready** and sends a notification. Nothing starts. |
| The operator runs the decision review and starts `A` | `A` gets `ai:available`. The next tick starts it. |
| `B` closes as `NOT_PLANNED` instead | The next tick reports `A` as **not sure** and names `B`. The decision review offers start anyway, re-plan, backlog, or drop. |
| `A` closes as `COMPLETED`, and the operator later plans and completes `C` | `tick` closes `G` |

## 7. Phase 3 (deferred)

Phase 3 starts only after Phases 0 to 2 are proven in daily use.

- **3A. Read-only visualizer.** A separate tool shows sub-issues, `blockedBy`,
  `stateReason`, labels, and overlap badges. It is needed only if the GitHub UI
  and the Mermaid graphs are not enough. A future UI that changes state calls a
  `pr-pipeline` command. It never writes workflow labels itself.
- **3B. Unattended planning.** This includes `ai:plan-requested` intake. These
  contracts come first:
  1. Dedicated spec stages in the stage tables, not reused PR review markers.
  2. One review owner, the Goal, with a manifest of the members, their digests,
     the edges, and the base SHA. A change to any member makes the approvals of
     its siblings stale.
  3. The §6.2 procedure without the human. Each write needs an operation
     identity, and the two cases that Phase 2 gives to the operator need
     automated rules: a lost create with no marker match, and a duplicate
     marker.
  4. The shared `max_in_flight` cap, provider cooldowns, round limits, and
     plan-size limits.
  5. Process-level probes: late holds, drift, a crash after the first write, a
     lost create, mixed labels, and an edge inserted during a running
     implementation.
