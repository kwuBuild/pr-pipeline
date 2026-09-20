# pr-pipeline

An unattended control plane that drives GitHub issues through implement → review → fix → re-review using AI engines (Claude Code, Codex). It ships as a Claude Code plugin, published from this repository as its own marketplace (`.claude-plugin/`); every directory under `skills/` becomes one namespaced command — `/pr-pipeline:tick`, `/pr-pipeline:status`, `/pr-pipeline:definition-of-done`. GitHub labels are the only source of truth for what stage a work item is in.

## Document Map

- `skills/tick/SKILL.md` — the operator SOP. Lean by contract; read it first to run anything.
- `skills/tick/README.md` — usage, observability, and the tuning knobs.
- `skills/tick/DEBUG.md` — invariants, failure diagnostics, and recovery.
- `docs/working/pr-pipeline-internals.md` — how the runtime actually behaves, with line-anchored links into the source.
- `docs/architecture.md` — the architecture reference: what owns each decision and why each boundary sits where it does. Read before changing dispatch, harvest, custody, or cadence.
- `skills/definition-of-done/SKILL.md` — the bar a change has to clear before green stands in as proof it works. Shipped with the plugin so it travels to every target repository.
- `skills/review-response-hygiene/SKILL.md` — the revision rules a fixer follows after review feedback lands. Shipped with the plugin and bound into the fixer prompt by absolute path, so the installed copy governs every target repository and no PR branch supplies the rules its own fixer runs under.
- `docs/working/plugin-distribution-design.md` — design for shipping the pipeline as an installable Claude Code plugin: operator-owned config, issue-author trust, and the staged independence validation. Partially implemented — the manifests, the skill naming, and section 3's layered configuration (`~/.claude/pr-pipeline/config.toml`, one resolver) have landed; section 14 tracks the rest.
- `docs/working/commit-guidelines.md` — Commit format rules. Read before committing.
- `docs/working/pr-guidelines.md` — PR body template, issue template, workflow labels, AI review posting. Read before raising a PR, creating an issue, or reviewing.

## Architecture in one paragraph

One deterministic orchestrator, many disposable engines. `skills/tick/lib/tick.py` is a single Python process: each invocation runs HARVEST (collect finished engines, archive evidence, apply the transition their sentinel earned) and then DISPATCH (admit eligible work under a concurrency cap and launch an engine for it). `skills/tick/lib/pipeline.py` holds the deterministic primitives it composes — label vocabulary, marker validation, verdict parsing, policy gates. The **engines** (`claude --print` / `codex exec`) are subprocesses with fresh context per dispatch, each in its own `git worktree`, and they never move an `ai:` label — they report through a completion sentinel and the harvest relabels. Pipeline *code* resolves from the install; pipeline *state* (`.claude/worktrees/`) lives in the repository being operated on.

## Tech Stack

- Python 3.11+, **standard library only** (`tomllib` is the 3.11 floor). CI proves this on `{ubuntu, macos} × {3.11, 3.14}` with nothing but pytest installed: the floor, and the newest interpreter `resolve-python.sh` will pick. An accidental third-party import must fail there, not on an adopter's machine.
- pytest (no plugins; `unittest.mock` for doubles)
- `gh` CLI (GitHub I/O), `git worktree` (per-task isolation)
- Claude Code and Codex CLIs as dispatched engines
- Markdown skill bundle for Claude Code (`skills/`)

## Task Coordination

Tasks are tracked as GitHub Issues and PRs with workflow labels. The full label set and transition rules live in `docs/working/pr-guidelines.md` under [Workflow labels](docs/working/pr-guidelines.md#workflow-labels) — read that doc before picking up, raising, reviewing, or revising any work.

**If you were dispatched by `pr-pipeline:tick`, every label instruction in this
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
- `ai:revised` — fixes pushed, re-review needed
- `ai:ready-for-human` — passed AI review; ready for owner merge

Escalation and hold states:
- `ai:needs-human-decision` — scope/design decision beyond agent remit
- `ai:max-iterations` — review/fix cycles exceeded threshold
- `ai:blocked` — human-set: waiting on a dependency or decision

`pipeline.py`'s `LABEL_*` constants are the vocabulary. A label not defined there is not part of the workflow, and an item moved to one goes dark: no dispatch query selects it.

Quick reminders:
- Before starting: `gh issue list --label "ai:available"`
- When creating issues: include the required rationale sections from `docs/working/pr-guidelines.md`, especially why the work matters and what fails if skipped
- Keep exactly one current workflow label per work item
- An AI reviewer goes straight from `ai:implemented` (or `ai:revised`) to `ai:ready-for-human` when there are no blocker/major findings — do not stop at an intermediate label just because a comment was posted

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
6. Self-review the diff (`git diff origin/main...HEAD`) as if presenting to a principal engineer.
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

There is no packaging manifest and no dependency install — the suite is stdlib + pytest, run against an interpreter that is actually 3.11 or newer:

```bash
python3.13 -m pytest skills/ -q        # or any python >= 3.11 that has pytest

# If pytest is not installed for that interpreter, give it one (.venv is ignored):
python3.13 -m venv .venv && .venv/bin/pip install -q pytest
.venv/bin/python -m pytest skills/ -q
```

Do **not** use bare `python3`: it resolves to 3.9 on a stock macOS box and under Anaconda, and collection fails on `tomllib`. This is a PATH-precedence problem, so "install a newer Python" does not fix it. CI runs `python -m pytest skills/ -q` on a pinned matrix for the same reason.

`uv run --python 3.13 --with pytest pytest skills/ -q` used to be documented here and is **not** reliable: `--python` selects the interpreter, but the bare `pytest` console script is still resolved from `PATH`, so an Anaconda install wins and the suite runs under its 3.9 — collection dies inside `pipeline.py`'s floor guard with no test having run (`--isolated` does not change this). Invoke an interpreter you named, as above.

The suite also needs `gh`, `git`, and `codex` on PATH — the tick's startup probe exits 3 without them. Every real invocation is mocked; only resolution is required.

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

- Internal task codes (Task 1, Task 2, etc. used while planning) are private references. Never use them in commits, PRs, or branch names. Use descriptive names instead.
