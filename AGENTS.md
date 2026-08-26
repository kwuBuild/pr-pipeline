# pr-pipeline Agent Guide

## Startup Convention

Before doing substantive work in this repository:

1. Read `AGENTS.md`
2. Read `CLAUDE.md`
3. Read the relevant section of the design and internals docs `CLAUDE.md` routes to, by section or line range rather than wholesale
4. Follow repo-specific guidance from those files unless it conflicts with higher-priority system, developer, or user instructions

If both files apply:

- `AGENTS.md` is the short operational guide
- `CLAUDE.md` is the deeper project and architecture guide
- For Codex and other AGENTS.md-native agents, `AGENTS.md` is authoritative for workflow, tooling, and runtime behavior
- `CLAUDE.md` may contain Claude Code-specific overrides; those overrides do not apply to Codex unless `AGENTS.md` says they do
- If they conflict, prefer `AGENTS.md` for workflow/tooling/runtime behavior and `CLAUDE.md` for project context unless the user says otherwise

## Working Rules

**If you were dispatched by `pr-pipeline-tick`, every label instruction in this
file and in `pr-guidelines.md` is not for you** — not the workflow bullets
below, not the blanket pre-authorization for updating workflow labels, and not
the review-posting flow that runs `gh pr edit`. The scope is unbounded on
purpose: it covers every label instruction in this file, present and future, not
a counted subset you can read past. The tick owns both ends of your stage's
label transitions. Do not add, remove, or change any `ai:` label on any issue or
PR; report your outcome through the completion sentinel named in your prompt and
let the harvest do the relabel. A label you set mid-run cannot be retracted and
will be read as a human hold that overrides the sentinel you finish with. See
[Pipeline label authority](docs/working/pr-guidelines.md#pipeline-label-authority).

- Start with the smallest relevant context instead of loading large project documents wholesale
- Use `CLAUDE.md` as the routing map for project architecture and planning documents
- Prefer focused reads by section or line range when a referenced document is large
- Before using project-status guidance, verify your local base branch is current with the remote
- When creating GitHub issues, read `docs/working/pr-guidelines.md` and follow its issue body template; every issue should explain why the work matters and what fails if it is skipped
- When raising a PR manually, read `docs/working/pr-guidelines.md` and follow its required PR body structure
- When picking up an issue, reviewing a PR, or revising after review, follow the workflow-label and AI-review-posting rules in `docs/working/pr-guidelines.md`
- When reviewing a PR, issue, implementation proposal, design proposal, or plan, review the full change scope against the current live repo and adjacent contracts. Do not limit the review to newly edited lines, previously requested fixes, or the author's stated intent.
- Treat proposal and documentation reviews with the same completeness standard as code reviews: verify that the content is executable, ordered correctly, and consistent with current code, tests, runtime contracts, and workflow docs.
- Report the complete set of blocker and major findings you can identify in the current pass. Do not hold back issues to keep the comment short, and do not stop after finding the first valid blocker. The goal is to avoid repeated cycles where one issue is fixed only for the reviewer to surface another issue that was already discoverable.
- GitHub workflow operations requested by the user or required by repo workflow are pre-authorized when they are non-destructive: creating issues, creating PRs, posting issue/PR/review comments, and updating workflow labels. Do not ask for per-item confirmation before running the matching `gh` command. This does not authorize destructive remote actions such as closing issues/PRs, deleting branches, merging, or force-pushing someone else's branch.
- When preparing GitHub body text (issue bodies, PR bodies, review bodies, comments, or review replies), write body files to `tmp/` at the project root (gitignored, inside the sandbox write allowlist). Run `mkdir -p tmp` first.

## Delivery Expectations

- Use test-first development for implementation work
- Do not write implementation code before a failing test exists
- Keep changes minimal, verify locally, and review the diff thoroughly before presenting work — apply principal-engineer standards whether the change is code or documentation
- Preserve existing user changes and do not revert unrelated modifications

## Commit Rules

- Treat commit formatting as a hard requirement, not a preference
- Before creating a commit, read `docs/working/commit-guidelines.md` and follow it exactly
- Before opening a PR, read `docs/working/pr-guidelines.md` for branch surgery (squash, rebase, force-push)
- One commit per task by default; squash before pushing a PR
- Every task commit needs a body with `What:` and `Why:` sections — no subject-only commits
- No internal task codes or AI tooling names in commit messages

## Running Tests

- Stdlib + pytest only; there is no packaging manifest and nothing to install
- Run against an interpreter that is actually >= 3.11: `python3.13 -m pytest skills/ -q`, or `uv run --python 3.13 --with pytest pytest skills/ -q`
- Never use bare `python3` — it resolves to 3.9 on stock macOS and under Anaconda, and collection fails on `tomllib`
- `gh`, `git`, and `codex` must resolve on PATH or the tick's startup probe exits 3

## Project Overview

This repository *is* the pipeline. Everything shipped lives under `skills/`:

- **`skills/pr-pipeline-tick/`** — the control plane. `skills/pr-pipeline-tick/lib/tick.py` is one deterministic Python process that runs HARVEST then DISPATCH per invocation, `skills/pr-pipeline-tick/lib/pipeline.py` holds the primitives it composes (label vocabulary, marker validation, verdict parsing, policy gates), and `skills/pr-pipeline-tick/lib/prompts/` holds the engine prompts. `SKILL.md` is a lean operator SOP, not the orchestrator.
- **`skills/pr-pipeline-status/`** — a read-only dashboard over the same state. It never writes.

Dispatched engines (`claude --print` / `codex exec`) run in per-task `git worktree` checkouts with fresh context and report through a completion sentinel; the harvest owns every label transition.

Read `skills/pr-pipeline-tick/SKILL.md` to run it, `docs/working/pr-pipeline-internals.md` for runtime behaviour, and `docs/architecture.md` for the design rationale.

## User Prompt Convention

To activate this startup flow in a new session, the user should begin with:

`Read AGENTS.md and CLAUDE.md first, then proceed.`
