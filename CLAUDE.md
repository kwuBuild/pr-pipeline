# pr-pipeline

A skill-driven automation that drives GitHub issues through implement → review → fix → re-review using AI agents (Claude Code, Codex). The Claude Code skill bundle is the orchestrator; the Python `pr-pipeline` CLI is a deterministic toolkit of primitives.

## Document Map

- `docs/specs/design.md` — Architecture spec. Read this first.
- `docs/plans/implementation.md` — Implementation plan, 28 TDD tasks. The build sequence.
- `docs/working/commit-guidelines.md` — Commit format rules. Read before committing.
- `docs/working/pr-guidelines.md` — PR body template, issue template, workflow labels, AI review posting. Read before raising a PR, creating an issue, or reviewing.

## Architecture in one paragraph

Two layers of agency. The **orchestrator agent** (a Claude Code session running the skill) reads issue and PR labels, claims work atomically, dispatches engine subprocesses in per-task git worktrees, parses verdicts, applies transitions, and handles exceptions inline (proposes settings fixes, retries transient errors). The **engine subprocesses** (`claude --print` / `codex exec`) run with fresh context per dispatch. The Python toolkit composes deterministic primitives (`list-issues`, `list-prs`, `claim`, `dispatch`, `transition`, `post-comment`, etc.) — no orchestration in code.

## Tech Stack

- Python 3.11+
- Click (CLI), PyYAML (config), `string.Template` stdlib (prompt templates)
- `gh` CLI (GitHub I/O)
- `git worktree` (per-task isolation)
- pytest + pytest-mock (testing)
- Markdown skill bundle for Claude Code

## Task Coordination

Tasks are tracked as GitHub Issues and PRs with workflow labels. The full label set and transition rules live in `docs/working/pr-guidelines.md` under [Workflow labels](docs/working/pr-guidelines.md#workflow-labels) — read that doc before picking up, raising, reviewing, or revising any work.

**If you were dispatched by `pr-pipeline-tick`, every label instruction in this
file and in `pr-guidelines.md` is not for you.** The tick owns both ends of your
stage's label transitions. Do not add, remove, or change any `ai:` label on any
issue or PR; report your outcome through the completion sentinel named in your
prompt and let the harvest do the relabel. A label you set mid-run cannot be
retracted and will be read as a human hold that overrides the sentinel you
finish with. For the same reason, do not run `gh pr create` — the instructions
elsewhere in this file that tell you to raise the PR yourself are for
interactive agents. The harvest opens your PR from the `pr-body` file your
prompt names, and labels it. See
[Pipeline label authority](docs/working/pr-guidelines.md#pipeline-label-authority).

### Workflow labels

Issue states:
- `ai:available` — ready for an agent to pick up
- `ai:implementing` — an agent is actively implementing

PR states:
- `ai:implemented` — PR raised, awaiting review
- `ai:reviewing` — reviewer claimed (transient)
- `ai:changes-requested` — review found issues requiring fixes
- `ai:fixing` — fixer claimed (transient)
- `ai:recheck` — fixes pushed, re-review needed
- `ai:ready-for-human` — passed AI review; ready for owner merge

Escalation states:
- `ai:needs-human-decision` — scope/design decision beyond agent remit
- `ai:max-iterations` — review/fix cycles exceeded threshold
- `ai:permission-pending` — orchestrator proposed a settings change; awaiting approval

Quick reminders:
- Before starting: `gh issue list --label "ai:available"`
- When creating issues: include the required rationale sections from `docs/working/pr-guidelines.md`, especially why the work matters and what fails if skipped
- Keep exactly one current workflow label per work item
- An AI reviewer goes straight from `ai:implemented` (or `ai:recheck`) to `ai:ready-for-human` when there are no blocker/major findings — do not stop at an intermediate label just because a comment was posted

## TDD Enforcement

When implementing any feature or fix:
1. Write ONE failing test first. Run it. Confirm it fails.
2. Write minimum code to pass that test. Run tests. Confirm green.
3. **Refactor phase:** Ask: "Would a principal engineer approve this implementation,
   or leave a review comment?" If I'd expect a comment — refactor the implementation
   and re-run tests to confirm still green. Never add new behavior during refactor;
   that requires a new failing test first.
4. **Test type check:** Consider whether this feature also needs integration or E2E tests
   beyond unit tests. If it touches subprocess, GitHub API, or filesystem — write
   those tests too.
5. Repeat steps 1-4 for each requirement within the task autonomously.
   For doc/spec/plan-only changes, skip steps 1-5 and go directly to step 6.
6. Self-review the diff (`git diff main...HEAD`) as if presenting to a principal engineer.
   For code changes: check for missing edge cases, naming clarity, unnecessary complexity,
   security issues, and test coverage gaps.
   For doc/spec/plan changes: verify the content is complete, ordered correctly, executable
   by an implementer without follow-up questions, and consistent with existing working docs
   and current code. Fix any findings before presenting.
7. Only present to the user when the full task is complete, reviewed, and ready to commit.

Never write implementation code without first having a failing test.

## PR Workflow

When raising a PR manually, read `docs/working/pr-guidelines.md` and follow it exactly.

The short version:
- Include `## Summary`, `## Why this approach`, `## What changed`, `## Test coverage`, and `## Test plan`
- Keep `## Summary` brief
- Use `## Why this approach` to explain design decisions and tradeoffs explicitly, not buried in the file-by-file diff walkthrough
- Squash all commits into one and rebase onto the base branch before pushing
- Run the relevant tests and confirm they pass before raising the PR

**Force pushing** to your own PR branch before merge is fine (`--force-with-lease`) to keep commit history clean.

**Remote operations are owner-only.** Never delete remote branches, close PRs/issues, or perform any destructive remote action. Only the owner decides what happens on the remote. Local cleanup (worktrees, local branches) is fine.

## Commit Workflow

- Commit hygiene in this repo is a hard requirement
- Before creating a commit, read `docs/working/commit-guidelines.md` and follow it exactly
- One commit per task by default, and no internal task codes in commit messages

## AI Review Posting

The review-posting workflow (how to find the diff, pick the terminal label, format the comment, and post it in one invocation) lives in `docs/working/pr-guidelines.md` under [AI review posting](docs/working/pr-guidelines.md#ai-review-posting). Read it before reviewing a PR.

Posting + labeling per that doc is pre-authorized — it is not a destructive remote op and does not need per-PR confirmation. It does NOT override the owner-only rule for deletes, closes, merges, or force-pushes to others' branches.

## Tool Use — Always Prefer Dedicated Tools Over Bash

Never use Bash to do what a dedicated tool can do:
- Read files → `Read` tool, not `cat`/`head`/`tail`/`sed`
- Write files → `Write` tool, not `echo >` or heredoc
- Search files → `Grep`/`Glob`, not `grep`/`find`/`rg`
- Edit files → `Edit` tool, not `sed`/`awk`

Only use Bash for commands that genuinely require shell execution (running tests, git operations, process management).

## Test File Organization

- **Fixtures first** — `autouse` fixtures and shared fixtures at the top
- **Tests next** — all `test_*` functions come before any helpers
- **Helpers last** — under a `# --- Helpers ---` separator at the bottom
- **No `_` prefix on helpers** — they're module-level functions
- **No `test_` prefix on helpers** — pytest would collect and run them as tests

## Running Tests

Use `uv run pytest ...` as the default test runner. Do not use `python -m pytest` directly.

## Review Feedback Hygiene

Never blindly apply review feedback — whether from a subagent, another AI, or a
human reviewer. Even a principal engineer's suggestion may be based on incomplete
context, outdated knowledge, or a misunderstanding of the trade-offs. Before acting
on any review finding:
1. Think independently about whether the suggestion actually makes sense given the
   full context you have. Does it improve correctness, or just sound plausible?
2. Separate factual claims (flag X exists, config key Y does Z) from judgment calls
   (this naming is unclear, this section is missing context). Verify factual claims
   independently — run `--help`, search official docs, or check the codebase.
3. If a suggestion conflicts with your understanding, investigate before accepting.
   The reviewer may be right, or they may be confidently wrong.
4. Only apply changes you have reasoned through and confirmed are correct.

## Conventions

- Internal task codes (Task 1, Task 2, etc. in the implementation plan) are private planning references. Never use them in commits, PRs, or branch names. Use descriptive names instead.
