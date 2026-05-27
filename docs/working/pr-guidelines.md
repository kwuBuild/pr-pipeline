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

Tasks are tracked as GitHub Issues and PRs with workflow labels. Keep exactly one current workflow label on each work item whenever possible. Label names use the `ai:` namespace defined in `docs/specs/design.md`.

Issue states:
- `ai:available` — ready for an agent to pick up
- `ai:implementing` — an agent is actively implementing

PR states:
- `ai:implemented` — PR raised, awaiting review
- `ai:reviewing` — reviewer claimed (transient until verdict posts)
- `ai:changes-requested` — review found issues requiring fixes
- `ai:fixing` — fixer claimed (transient until fixes push)
- `ai:recheck` — fixes pushed, re-review needed
- `ai:ready-for-human` — passed AI review; ready for owner merge

Escalation states:
- `ai:needs-human-decision` — scope/design decision beyond agent remit
- `ai:max-iterations` — review/fix cycles exceeded threshold
- `ai:permission-pending` — orchestrator proposed a settings change; awaiting approval

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
- **When an AI responds to review comments and pushes a new revision:** Comment with what changed, push the revision, and replace the PR's AI workflow label with `ai:recheck`:
  ```bash
  gh pr edit <pr-number> --remove-label "ai:implemented" --remove-label "ai:reviewing" --remove-label "ai:changes-requested" --remove-label "ai:ready-for-human" --add-label "ai:recheck"
  ```
- **When escalating to human:** Replace the current label with the appropriate escalation label (`ai:needs-human-decision`, `ai:max-iterations`, or `ai:permission-pending`) and comment with context.

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
     --remove-label "ai:recheck" \
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

## Related guidance

- Commit rules: `docs/working/commit-guidelines.md`
- Repo context and Claude Code-specific overrides: `CLAUDE.md`
