# pr-pipeline Agent Guide

## Startup Convention

Before doing substantive work in this repository:

1. Read `AGENTS.md`
2. Read `CLAUDE.md`
3. Read the relevant spec/plan section in `docs/specs/` and `docs/plans/`
4. Follow repo-specific guidance from those files unless it conflicts with higher-priority system, developer, or user instructions

If both files apply:

- `AGENTS.md` is the short operational guide
- `CLAUDE.md` is the deeper project and architecture guide
- For Codex and other AGENTS.md-native agents, `AGENTS.md` is authoritative for workflow, tooling, and runtime behavior
- `CLAUDE.md` may contain Claude Code-specific overrides; those overrides do not apply to Codex unless `AGENTS.md` says they do
- If they conflict, prefer `AGENTS.md` for workflow/tooling/runtime behavior and `CLAUDE.md` for project context unless the user says otherwise

## Working Rules

- Start with the smallest relevant context instead of loading large project documents wholesale
- Use `CLAUDE.md` as the routing map for project architecture and planning documents
- Prefer focused reads by section or line range when a referenced document is large
- Before using project-status guidance, verify your local base branch is current with the remote
- When creating GitHub issues, read `docs/working/pr-guidelines.md` and follow its issue body template; every issue should explain why the work matters and what fails if it is skipped
- When raising a PR manually, read `docs/working/pr-guidelines.md` and follow its required PR body structure
- When picking up an issue, reviewing a PR, or revising after review, follow the workflow-label and AI-review-posting rules in `docs/working/pr-guidelines.md`
- When reviewing a PR, issue, implementation proposal, design proposal, or plan, review the full change scope against the current live repo and adjacent contracts. Do not limit the review to newly edited lines, previously requested fixes, or the author's stated intent.
- Report the complete set of blocker and major findings you can identify in the current pass. Do not hold back issues to keep the comment short, and do not stop after finding the first valid blocker. The goal is to avoid repeated cycles where one issue is fixed only for the reviewer to surface another issue that was already discoverable.
- GitHub workflow operations requested by the user or required by repo workflow are pre-authorized when they are non-destructive: creating issues, creating PRs, posting issue/PR/review comments, and updating workflow labels. Do not ask for per-item confirmation before running the matching `gh` command. This does not authorize destructive remote actions such as closing issues/PRs, deleting branches, merging, or force-pushing someone else's branch.
- When preparing GitHub body text (issue bodies, PR bodies, review bodies, comments, or review replies), write body files to `tmp/` at the project root (gitignored, inside the sandbox write allowlist). Run `mkdir -p tmp` first.

## Delivery Expectations

- Use test-first development for implementation work
- Do not write implementation code before a failing test exists
- Keep changes minimal, verify locally, and review the diff before presenting work
- Preserve existing user changes and do not revert unrelated modifications

## Commit Rules

- Treat commit formatting as a hard requirement, not a preference
- Before creating a commit, read `docs/working/commit-guidelines.md` and follow it exactly
- Before opening a PR, read `docs/working/pr-guidelines.md` for branch surgery (squash, rebase, force-push)
- One commit per task by default; squash before pushing a PR
- Every task commit needs a body with `What:` and `Why:` sections — no subject-only commits
- No internal task codes or AI tooling names in commit messages

## Running Tests

- Use `uv run pytest ...` as the default test runner
- Do not use `python -m pytest` directly

## Project Overview

This is the toolkit + skill bundle that makes `pr-pipeline` work. Two halves:

- **Python toolkit** (`src/pr_pipeline/`) — deterministic CLI primitives. No orchestration logic.
- **Skill bundle** (`plugin/`) — markdown that an orchestrator agent interprets. The brain.

Read `docs/specs/design.md` for the architecture, then `docs/plans/implementation.md` for the build sequence.

## User Prompt Convention

To activate this startup flow in a new session, the user should begin with:

`Read AGENTS.md and CLAUDE.md first, then proceed.`
