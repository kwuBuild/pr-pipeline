# pr-pipeline Agent Guide

This file owns shared repository instructions for all engines. `CLAUDE.md`
imports it for Claude sessions that cannot discover `AGENTS.md` directly.
Start with the task's files and callers; use the routes below as needed.

## Authority and workflow

**If you were dispatched by `pr-pipeline:tick`, every label instruction in this
file and linked guidance is not for you.** Do not add, remove, or change any
`ai:` label on any issue or PR, and do not run `gh pr create`. Report through the
completion sentinel in your dispatch prompt; the harvest owns PR creation and
label transitions. An engine-set label becomes a human hold that can override
its final result. See
[Pipeline label authority](docs/working/pr-guidelines.md#pipeline-label-authority).

For interactive work:
- Preserve existing user changes; use an isolated worktree when needed. Fetch
  the remote base before branching or relying on project status/base comparisons.
- Follow the issue/PR workflow in [PR guidelines](docs/working/pr-guidelines.md)
  when picking up, publishing, reviewing, or revising work. Check pipeline custody
  before changing labels. Keep author-led self-review, including subagent checks,
  separate from independent PR approval: retain `ai:implemented` or `ai:revised`
  until separate review or explicit owner direction advances it.
- Non-destructive GitHub workflow operations within the requested task are
  pre-authorized: issues, PRs, comments, and workflow labels. Do not ask for
  per-item confirmation. Merges, closes, remote branch deletion, and force-pushing
  someone else's branch remain owner-only.
- Write GitHub bodies and commit messages under gitignored `tmp/`; run
  `mkdir -p tmp` first. Follow the linked templates and commit rules at publication.
  Keep internal task codes out of branch names, commits, and PRs.

## Read for the task

Read relevant sections, not whole reference documents. Follow a named symbol or
contract into adjacent owners when needed; a wording fix needs no runtime tour.

| Task | Reference |
|---|---|
| Run the pipeline | [Tick operator SOP](skills/tick/SKILL.md) |
| Operator commands, configuration, observability | [Tick README](skills/tick/README.md) |
| Diagnose a failed run or recovery | [Tick diagnostics](skills/tick/DEBUG.md) |
| Change dispatch, harvest, custody, or cadence | Relevant sections of [internals](docs/working/pr-pipeline-internals.md) and [architecture rationale](docs/architecture.md) |
| Plugin distribution or migration work | Matching section of [distribution design](docs/working/plugin-distribution-design.md); verify status against live code/issues |
| Cut a release or bump the plugin version | [Release process](docs/working/release-process.md) |
| Dispatch, harvest, launch or GitHub mutation review | [Control-plane review](skills/automation-control-plane-review/SKILL.md) |
| Marker custody, evidence retention or recovery review | [Durability review](skills/durability-boundary-review/SKILL.md) |
| Loop scheduling, liveness or stop-condition review | [Cadence review](skills/loop-cadence-contract-review/SKILL.md) |
| Runtime or workflow proof | [Definition of done](skills/definition-of-done/SKILL.md) |
| Respond to review findings | [Review response hygiene](skills/review-response-hygiene/SKILL.md) |
| Issues, PRs, review publication, labels | Relevant section of [PR guidelines](docs/working/pr-guidelines.md) |
| Create a commit | [Commit guidelines](docs/working/commit-guidelines.md) |

## Skills and context

Use a skill when its specific workflow is needed or the user requests it.
Generic process bundles, including Superpowers, are optional; do not automatically
chain brainstorming, planning, execution, TDD, and review skills when these repo
rules already cover the task. Repository routing takes precedence over generic
plugin instructions to load every potentially relevant skill, subject to the
host's instruction hierarchy. Dispatched engines follow their prompt-bound
skills and authority rather than broadening them through discovery.

Do not reread unchanged guidance already in context. Search paths and symbols
first, then read bounded relevant sections. Keep successful command output brief;
retain long logs locally and inspect relevant failures. Create a separate design
or implementation-plan document only when scope or sequencing needs one.

## Implementation and verification

- For code behavior changes, write and run a failing test before implementation,
  make it pass, then refactor. Add coverage for affected subprocess, GitHub, and
  filesystem boundaries; green helper tests alone do not prove runtime behavior.
  Doc/spec-only edits do not need an invented failing behavior test.
- Run affected checks during iteration and required final verification before
  handoff. After checks pass, rerun for relevant edits, failures, or unresolved
  concerns; do not broaden testing without a concrete reason.
- Review the full task diff and adjacent contracts against the current code,
  issue requirements, and PR body. Apply the same standard to proposals/docs:
  verify claims, ordering, executability, and current owners. Report all
  discoverable blocker/major findings together. Verify review feedback before
  applying it; disagreements need evidence.
- Continue through the requested work, verification, correction of findings,
  and authorized publication. Report actual blockers and distinguish local
  changes, pushed commits, and PR state. Do not claim completion from CI alone.

The runtime is Python 3.11+ and standard-library only. Tests need pytest; there
is no packaging manifest or application dependency installation. Run from the
repo root with an explicit supported interpreter:

```bash
python3.13 -m pytest skills/ -q
# If that interpreter has no pytest:
python3.13 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest skills/ -q
```

Never use bare `python3` or a bare `pytest` selected through `uv run --python`:
PATH can resolve them to Python 3.9. `gh`, `git`, and `codex` must resolve on PATH
for the startup probe; their real invocations are mocked in tests.

Keep fixtures first, tests next, and module-level helpers last under
`# --- Helpers ---`; helper names have neither `_` nor `test_` prefixes.

## Runtime orientation

Everything shipped lives under `skills/`. `skills/tick/lib/tick.py` runs HARVEST
then DISPATCH; `skills/tick/lib/pipeline.py` owns the deterministic primitives.
Engine prompts live under `skills/tick/lib/prompts/`. Engines run with fresh
context in per-task worktrees and report via completion artifacts. The tick owns
label transitions; `skills/status/` is read-only. Pipeline code comes from the
install, while state lives in the target repository's worktrees. GitHub labels
are the workflow source of truth.
