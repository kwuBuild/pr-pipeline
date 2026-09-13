# PR Guidelines

Use this document when preparing a manual pull request in this repository.

## Before opening the PR

- `git fetch origin main` to get the latest base before any branch surgery
- Confirm the branch diff against the base branch contains only the intended task changes
- Confirm the branch history against the base branch contains only the intended task commit(s)
- Squash all commits into one via `git rebase -i origin/main` (do not use
  `git reset --soft origin/main` — soft-reset moves HEAD without applying
  the new base's changes to the working tree, so files that changed on
  main since the branch diverged end up as unintended reverts in the
  squashed commit)
- Force-push with `--force-with-lease` after squashing or rebasing
- Run the relevant tests before opening the PR
- After creating the PR, update workflow labels per the [Workflow labels](#workflow-labels) section below: remove `ai:implementing` from the issue and add `ai:implemented` to the PR
- When posting a PR review or revision comment via `gh pr comment --body-file`:
  write the body file to `tmp/` at the project root. Run `mkdir -p tmp` first — the
  directory may not exist in a fresh clone. The project-level `tmp/` should be
  gitignored and inside the sandbox write allowlist. Clean up the temp file after
  the comment is posted.

The PR should reflect the repo's one-commit-per-task rule unless the user explicitly asked for stacked or bisectable history.

## PR body template

PR descriptions must include these sections:

```md
## Summary
- What this PR does (1-3 bullets)
- Reference the issue: `Closes #N`

## Why this approach
- Why the implementation is structured this way
- Key design decisions and tradeoffs
- Why this approach is the best fit for this codebase right now

## What changed
- What each file/module does
- Any reviewer context needed to navigate the diff

## Test coverage
- What's tested and how
- Notable test design choices (e.g. why a mock vs real subprocess, why fixture vs direct invocation)

## Test plan
- [x] Tests that passed locally
- [ ] Remaining manual verification items, if any
```

## Section intent

### Summary

Keep this short and outcome-focused. A reviewer should understand the user-visible or architectural change in a few seconds.

### Why this approach

This section is required. Do not hide the reasoning inside the file-by-file change list.

This section is not just a high-level slogan. It should explain the key decisions behind the implementation.

Cover the important decisions, such as:

- Why the code is organized this way
- What alternative was intentionally avoided
- Why the chosen approach fits the current architecture and conventions
- Any tradeoffs or known limits the reviewer should understand

Good prompts:

- Why does this primitive belong in the CLI rather than the skill?
- Why is explicit failure better than a fallback here?
- Why are these tests mocked at this layer instead of shelling out?
- Why is this the simplest approach that still preserves correctness?

### What changed

Walk the reviewer through the diff. Keep it concrete and file-oriented, but avoid repeating the design rationale from `## Why this approach` or implementation details already obvious from the patch.

### Test coverage

State what is exercised and why the chosen test shape is appropriate.

### Test plan

List the exact local verification that passed, then call out any remaining manual checks.

## Issue creation

Use this section when creating GitHub issues manually or drafting issue breakdowns. Issues must explain why the work exists before listing what to build. The goal is for an agent to understand the architectural role of the task, what capability it unlocks, and what goes wrong if the work is skipped.

### Issue body template

Issue descriptions must include these sections:

```md
## Summary
- What needs to be built.

## Why this matters
- Why the project needs this capability.
- What architectural role it plays.
- What problem remains unsolved without it.

## Failure mode if skipped
- If we do not build this, <specific consequence> happens.

## Scope
- Concrete implementation scope.

## File ownership
- Expected files/modules.

## Implementation references
- Planning, architecture, prior issues, or review notes.

## Done when
- Acceptance criteria.

## Out of scope
- Explicit boundaries.
```

### Issue section intent

#### Summary

Keep this brief and outcome-focused. It should tell a reader what capability the issue adds without turning into a file-by-file plan.

#### Why this matters

This section is required. It is not the same as implementation rationale in a PR. The issue should answer the earlier question: why do we need to build this at all?

Cover the important reasons, such as:

- Why this belongs in the current architecture
- What later work depends on it
- What role it plays in the system reliability or user experience
- Why deferring it would block, distort, or weaken the surrounding plan

#### Failure mode if skipped

Name the concrete consequence. Prefer "without this, X cannot happen" or "if we skip this, Y will drift/break/leak/fail" over vague importance claims.

Good prompts:

- What user-visible flow remains impossible?
- What architectural assumption would become false?
- What later task would be forced to duplicate or work around this?
- What bug, ambiguity, or operational risk will keep recurring?

#### Scope

List the buildable work. Keep this separate from the rationale so implementers can scan both the reason and the execution boundary.

#### File ownership

Name expected files or modules when known, especially for agent-parallel work. Use this to reduce merge friction, not to prevent better local judgment.

#### Implementation references

Point to the smallest relevant planning or architecture context. Do not require agents to load large documents when a focused section or line range is enough.

#### Done when

Define acceptance criteria that can be verified locally or in review.

#### Out of scope

State what the issue should not grow to include. This is especially important when the rationale is broad but the implementation slice is deliberately small.

## Workflow labels

Tasks are tracked as GitHub Issues and PRs with workflow labels. Keep exactly one current workflow label on each work item whenever possible. Label names use the `ai:` namespace. `pipeline.py`'s `LABEL_*` constants are the vocabulary: a label not defined there is not part of the workflow, and an item moved to one goes dark, because no dispatch query selects it.

Issue states:
- `ai:available` — ready for an agent to pick up
- `ai:implementing` — an agent is actively implementing

PR states:
- `ai:implemented` — PR raised, awaiting review
- `ai:reviewing` — reviewer claimed (transient until verdict posts)
- `ai:changes-requested` — review found issues requiring fixes
- `ai:fixing` — fixer claimed (transient until fixes push)
- `ai:revised` — fixes pushed, re-review needed
- `ai:ready-for-human` — passed AI review; ready for owner merge

Escalation and hold states:
- `ai:needs-human-decision` — scope/design decision beyond agent remit
- `ai:max-iterations` — review/fix cycles exceeded threshold
- `ai:blocked` — human-set: waiting on a dependency or decision

Required transitions:
- **Before starting work:** Run `gh issue list --label "ai:available"` to see open tasks.
- **When picking up a task:** Replace `ai:available` with `ai:implementing` on the issue:
  ```bash
  gh issue edit <number> --remove-label "ai:available" --add-label "ai:implementing"
  ```
- **If blocked before a PR exists:** Replace the issue's current label with `ai:needs-human-decision` and comment with what is blocked:
  ```bash
  gh issue edit <number> --remove-label "ai:available" --remove-label "ai:implementing" --add-label "ai:needs-human-decision"
  gh issue comment <number> --body "<what is blocked and what would unblock it>"
  ```
- **When raising the PR:** Push the branch, create a PR that references the issue (`Closes #N`), then remove the issue's workflow labels and label the PR `ai:implemented`:
  ```bash
  gh issue edit <number> --remove-label "ai:available" --remove-label "ai:implementing"
  gh pr edit <pr-number> --add-label "ai:implemented"
  ```
- **When an AI reviews the PR:** The reviewer picks the terminal label based on verdict (see [AI review posting](#ai-review-posting)):
  - If the review has blocker/major findings or open questions requiring code changes → `ai:changes-requested`
  - Otherwise (no blockers/majors) → `ai:ready-for-human` directly; do not stop at an intermediate label
  - Remove any prior AI workflow label before adding the terminal label so the PR ends with exactly one current AI workflow label
- **When an AI responds to review comments and pushes a new revision:** Comment with what changed, push the revision, and replace the PR's AI workflow label with `ai:revised`:
  ```bash
  gh pr edit <pr-number> --remove-label "ai:implemented" --remove-label "ai:reviewing" --remove-label "ai:changes-requested" --remove-label "ai:ready-for-human" --add-label "ai:revised"
  ```
- **When escalating to human:** Replace the current label with the appropriate escalation label (`ai:needs-human-decision` or `ai:max-iterations`) and comment with context.

Do not use `ai:ready-for-human` if there are unresolved `blocker` or `major` review findings, failing required checks, known test failures, or open questions that require code changes.

## AI review posting

When an agent reviews a PR, this section covers finding the diff, formatting the output, picking the right terminal label, and posting. It does NOT prescribe review methodology or depth — that is the reviewing agent's judgment.

The review output MUST be posted to the PR as a comment, and the PR MUST move to the correct terminal label. Do not just print the review into the chat.

Workflow:
1. Identify the target PR. If not given, use `gh pr view --json number` on the current branch.
2. Find the diff. Typical starting point: `gh pr diff <number>`, or `git diff $(gh pr view <number> --json baseRefName -q .baseRefName)...HEAD` if checked out. The reviewing agent may read files, check git history, or investigate however it sees fit.
3. Conduct the review. Approach, depth, and focus are up to the reviewing agent.
4. Pick the terminal label based on verdict:
   - Blocker/major findings or open questions requiring code changes → `ai:changes-requested`
   - No blockers/majors → `ai:ready-for-human`
   Nits alone do not warrant `ai:changes-requested`.
5. Write the review body to a local file, post it as a PR comment, and set the terminal label:
   ```bash
   mkdir -p tmp
   REVIEW_FILE=tmp/ai-review-pr<number>.md
   # write review content to $REVIEW_FILE
   gh pr comment <number> --body-file "$REVIEW_FILE"
   # Remove prior AI workflow labels, then add the terminal label from step 4
   gh pr edit <number> \
     --remove-label "ai:implemented" \
     --remove-label "ai:reviewing" \
     --remove-label "ai:revised" \
     --remove-label "ai:changes-requested" \
     --remove-label "ai:ready-for-human" \
     --add-label "<terminal-label>"
   ```
   Clean up the temp file after posting.
6. Report the comment URL back to the user.

Suggested comment format (reviewing agent may adapt as long as the structure is recognizable):
```
## AI Code Review

**Verdict:** <approve / request changes / comment>

### Findings
- **[severity]** <file:line> — <issue>. <suggested fix>.

### Nits
- <file:line> — <minor point>

### What's good
- <positive observations>
```

Severity tags: `blocker`, `major`, `minor`. Omit empty sections. The format is a guideline — if the reviewing agent has a better way to communicate a specific finding, use it.

This posting + labeling action is pre-authorized — it is not a destructive remote op and does not need per-PR confirmation. It does NOT override the owner-only rule for deletes, closes, merges, or force-pushes to others' branches.

## Pipeline label authority

A pipeline label is a *final-state* signal: it says who the item is waiting for
right now. Who may move it depends on the actor and on whether the item is under
pipeline custody — that is, whether it holds a dispatch marker.

| Actor | May move labels on | Notes |
|---|---|---|
| `pr-pipeline:tick` | Any item holding a dispatch marker | Owns every stage transition on marker-owned work: both the dispatch flip and the harvest flip |
| A human | Any item, at any time | Normal workflow transitions on items with no dispatch marker, **plus** an emergency hold on marker-owned work — see below |
| An interactive agent working by hand | Any item with no dispatch marker | This is how work enters the queue; the commands under [Transitions](#transitions) are written for this actor |
| A dispatched engine (implementer, fixer, reviewer) | Nothing | Reports its outcome through the completion sentinel in its prompt |

**Checking for a dispatch marker.** A marker is a `dispatch.json` in the item's
worktree — `.claude/worktrees/issue-<N>/dispatch.json` for an issue,
`.claude/worktrees/pr-<N>/dispatch.json` for a PR. If one exists, the item is
under pipeline custody and only the tick moves its label (a human emergency hold
excepted, below). This is a local check: markers live on the machine running the
tick, so an agent working somewhere else cannot see them and should treat any
item it did not queue itself as potentially owned.

**A human may stop a dispatched run at any time.** Adding
`ai:needs-human-decision` or `ai:blocked` to an item that is currently under
pipeline custody is the emergency stop, and the tick honours it deliberately:
dispatch skips a held candidate before it spends anything, and reconcile lets the
hold outrank even a *completed* engine result — the hold wins, the completion is
not harvested, and the tick publishes the validated engine outcome before it
releases its in-flight label and archives the output locally. A failed post leaves
the marker, result, scratch, attempts, and in-flight label intact for retry. This
is the one label move that is legitimate on marker-owned work, and it is why the
engine prohibition below has to be a contract: the tick cannot detect and ignore
an engine-set label without also ignoring the human's stop.

**A dispatched engine must never add, remove, or change an `ai:` label** — not on
its own work item, and not on any other. Its prompt names a completion sentinel
(`IMPLEMENTATION_COMPLETE`, `FIXES_PUSHED_RECHECK_NEEDED`, or
`NEEDS_HUMAN_DECISION`); that envelope is the engine's only outcome channel, and
the tick performs the matching transition when it harvests.

The reason is that the two channels have different lifetimes. The sentinel is
written once, at exit, and reflects the run's final state. A label is written
immediately and cannot be retracted — so an engine that hits a wall mid-run,
labels itself `ai:needs-human-decision`, then recovers and finishes successfully
leaves behind a hold that contradicts its own envelope. Reconcile cannot tell
that hold apart from the human emergency stop above — engines run with the tick's
own GitHub token, so even the timeline actor matches — so it fails closed toward
the human, and the successful run is archived locally rather than published on
the item. Removing the label channel from engines is what makes the remaining
holds genuinely human.

This is a contract, not a sandbox. The implementer and fixer run under the
operator's full environment by design (they need it to push), so nothing stops
them from reaching `gh pr edit` — the prompt instruction is the control. The
reviewer is the exception: it launches under a scrubbed environment with no
GitHub token and a read-only sandbox, so it is structurally incapable of
labelling and its prompt simply tells it not to attempt `gh` at all.

Because dispatched engines read this repository's workflow docs alongside their
prompt, the label commands throughout this document are for interactive agents
and humans only.

## Workflow labels

Issues and PRs each carry exactly one pipeline label at a time. Every label is an `ai:` namespace label. Each represents a "waiting for someone" state — transient work happens between labels, not as a label.

An issue being actively implemented MUST carry `ai:implementing`. The label transition is the first implementation step — run the `gh issue edit` command before writing code.

### Issue states (implementation stage)

| Label | Meaning | What happens next |
|---|---|---|
| `ai:available` | Ready for implementation | Agent picks it up |
| `ai:implementing` | Agent working on it | PR opens → label removed |
| `ai:blocked` | Waiting on dependency / decision | Owner unblocks |

### PR states (review pipeline)

| Label | Meaning | What happens next |
|---|---|---|
| `ai:implemented` | PR opened, awaiting review | Reviewer looks at it |
| `ai:changes-requested` | Review found issues | Author pushes fixes |
| `ai:revised` | Fixes pushed, awaiting re-review | Reviewer looks again |
| `ai:ready-for-human` | Passed AI review | Human merges |

### Pipeline automation states (pr-pipeline:tick)

The `pr-pipeline:tick` skill (`docs/architecture.md`) introduces
four labels the human workflow does not need. They mark "an engine is dispatched"
(transient work the human convention treats as happening *between* labels) and two
escalation terminals with no human-workflow equivalent. A human reviewing manually
never sets these.

| Label | Meaning | What happens next |
|---|---|---|
| `ai:reviewing` | Reviewer engine dispatched | Tick harvests the verdict |
| `ai:fixing` | Fixer engine dispatched | Tick harvests the result |
| `ai:needs-human-decision` | Scope/design decision beyond agent remit | Owner decides |
| `ai:max-iterations` | Review/fix cycles exceeded threshold (default 5) | Owner intervenes |

The in-flight labels (`ai:reviewing`, `ai:fixing`) are **not** in the dispatch
pickup set; if an engine dies after the label flips, the tick reverts the label to
its recorded pre-dispatch state (`ai:reviewing`→`ai:implemented` *or* `ai:revised`
depending on which the PR carried before review, `ai:fixing`→`ai:changes-requested`,
issue `ai:implementing`→`ai:available`) to re-queue the work.

#### Manual pickup is valid custody

An `ai:` label is a **workflow state, not a pipeline lease**. A human — or a
human-directed agent — who follows the pickup step below and moves an issue to
`ai:implementing` holds valid manual custody of that work, and the tick must leave
it alone.

Dispatch markers live on the dispatching machine's disk, so a missing marker
proves nothing — every tick on every other machine sees exactly that, and so does
every tick looking at your manual pickup. Reconciliation therefore **never** acts
on a markerless in-flight item. The item stays where it is and appears in the
`pr-pipeline:status` dashboard, grouped by `ai:` label.

The consequence is deliberate: an engine that dies without writing a result
leaves its item at an in-flight label until the owner clears it by hand. The
dashboard makes that item visible, but it does not identify it — it reads shared
GitHub labels plus only the local machine's markers, so an in-flight label with
no engine row is equally consistent with another machine's live dispatch and
with your own manual pickup. Confirm with the dispatch host before relabelling
anything; the runbook is in the `pr-pipeline:tick` skill README under
"Markerless in-flight items". Stranding is still cheaper than the alternative,
which relabelled healthy work and burned down the
credibility of `ai:needs-human-decision` — the label every custody escalation in
the tick routes to, `ai:max-iterations` being reachable only from the reviewer's
iteration cap below. See
`docs/architecture.md` → "Why custody is not inferred, and
not yet proven".

Automation transitions (driven by the tick, not by a human):

```
pr:ai:implemented / ai:revised ──[dispatch reviewer]──▶ pr:ai:reviewing
pr:ai:reviewing ──[verdict ready_for_human]──▶ pr:ai:ready-for-human
pr:ai:reviewing ──[verdict changes_requested]──▶ pr:ai:changes-requested
pr:ai:reviewing ──[changes_requested at iteration cap]──▶ pr:ai:max-iterations
pr:ai:changes-requested ──[dispatch fixer]──▶ pr:ai:fixing
pr:ai:fixing ──[fixes pushed]──▶ pr:ai:revised
any ──[scope/design question]──▶ ai:needs-human-decision
```

### Transitions

These commands are for humans and interactive agents. If you are a dispatched
pipeline engine, run none of them — see
[Pipeline label authority](#pipeline-label-authority).

Each transition is an explicit add + remove. Never rely on "swapping" — always issue both the `--add-label` and `--remove-label` as separate, deliberate steps.

- **When picking up a task — label it first:**
  ```bash
  gh issue edit <number> --add-label ai:implementing --remove-label ai:available
  ```
  This is the first implementation step. Do it before writing code.
  Discover available tasks with `gh issue list --label ai:available`.
  If the issue has no `ai:` label, add `ai:available` first to make it visible
  to discovery, then immediately transition to `ai:implementing` — this preserves
  the audit trail so the label history shows the issue was never unlabeled.
  If the issue is already `ai:blocked`, unblock it first
  (the label transition signals that the block is resolved).
- **If blocked before a PR exists:** Add `ai:blocked`, remove the current issue label, and comment with what is waiting:
  ```bash
  gh issue edit <number> --add-label ai:blocked --remove-label ai:available --remove-label ai:implementing
  gh issue comment <number> --body "<what is blocked and what would unblock it>"
  ```
- **When raising the PR:** Push the branch, create a PR that references the issue (`Closes #N`), then add `ai:implemented` to the PR and remove `ai:implementing` from the issue. This keeps exactly one pipeline label on the work and leaves the issue in a state a dispatcher will pick up; it frees no concurrency budget, because the pipeline's spend cap counts the dispatch markers on the machine running the tick, never labels:
  ```bash
  gh pr edit <pr-number> --add-label ai:implemented
  gh issue edit <issue-number> --remove-label ai:implementing
  ```
- **When an AI reviews the PR:** The reviewer picks the terminal label based on verdict (see [AI review posting](#ai-review-posting)):
  - Blocker/major findings or open questions requiring code changes → add `ai:changes-requested`, remove `ai:implemented`
  - No blockers/majors → add `ai:ready-for-human`, remove `ai:implemented`
  - Nits alone do not warrant `ai:changes-requested`
  ```bash
  gh pr edit <number> --add-label <terminal-label> --remove-label ai:implemented
  ```
- **When an AI pushes fixes in response to review:** Comment with what changed, push the revision, then add `ai:revised` and remove the current PR label:
  ```bash
  gh pr edit <pr-number> --add-label ai:revised --remove-label ai:changes-requested
  ```
- **When re-reviewing after revision:** Same as the initial review — pick terminal label, remove `ai:revised`:
  ```bash
  gh pr edit <number> --add-label <terminal-label> --remove-label ai:revised
  ```

Do not use `ai:ready-for-human` if there are unresolved `blocker` or `major` review findings, failing required checks, known test failures, or open questions that require code changes.

## Related guidance

- Commit rules: `docs/working/commit-guidelines.md`
- Repo context and Claude Code-specific overrides: `CLAUDE.md`
