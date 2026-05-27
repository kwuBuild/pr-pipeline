# AI PR Pipeline — Design

**Status:** Draft
**Date:** 2026-05-04
**Working name:** `pr-pipeline`

## Overview

A **skill-driven** automation that drives GitHub issues through implement → review → fix → re-review using AI agents. The user installs a skill bundle into Claude Code (or follows AGENTS.md guidance in Codex). When invoked via `/pr-pipeline-tick`, an **orchestrator agent** reads issue and PR labels, claims work atomically, dispatches **engine subprocesses** (claude or codex) in **per-task git worktrees**, parses verdicts, and applies label transitions. Engines can be mixed per stage.

The orchestrator agent has reasoning. When something goes wrong — a permission error, an ambiguous engine output, a transient API failure — it diagnoses, propagates the error up to the user, proposes a fix, and retries. Most situations the original daemon design treated as terminal failures are now recoverable in-session. There is no local state storage — GitHub labels and PR metadata are the sole source of truth.

**The Python toolkit** (`pr-pipeline` CLI) provides deterministic primitives: list issues/PRs by label, atomically claim, render a prompt, run an engine subprocess, transition labels, post comments. It does **not** orchestrate. Orchestration is the skill markdown the agent interprets.

**Distribution:** Claude Code skill bundle (primary) + PyPI package (toolkit dependency) + AGENTS.md snippet for Codex users. Daemon mode (launchd) is deferred to v0.5.

## Goals

- **Eliminate manual agent dispatch.** No more spawning N Claude sessions per stage by hand.
- **Per-task worktree isolation.** One worktree per issue or PR for its lifetime; parallel tasks never collide.
- **Fresh context per engine call.** Each engine dispatch is a new subprocess, no token bloat from prior stages.
- **Engine pluggability.** Pick `claude` or `codex` (or future engines) per stage independently.
- **Label-driven state machine.** GitHub labels are the source of truth.
- **Self-healing in-session.** The orchestrator agent recovers from common failures (permissions, transient errors) without human intervention beyond confirmation.
- **Trust-friendly onboarding.** Run manually first, escalate to `/loop` or daemon as comfort grows.
- **Distributable.** Others install a skill, follow a one-page setup, and are running within minutes.

## Non-goals

- **Not a CI replacement.** Tests still run in CI; the pipeline observes results, doesn't replace them.
- **Not a webhook server.** No public endpoint, no GitHub App.
- **Not a multi-tenant service.** Runs on the user's machine for the user's repos.
- **Not autonomous merging.** Final merge is always human.
- **Not a long-lived daemon (in v0).** Ticks happen when the user — or `/loop` — triggers them. Autonomous polling is v0.5.
- **Not deterministic at the orchestration layer.** The agent makes judgment calls; runs may differ in identical situations. Determinism is preserved only in the Python primitives.

## High-level architecture

```
                 ┌─────────────────────────────┐
                 │  User                       │
                 │  /pr-pipeline-tick          │ ← manual or via /loop
                 └──────────────┬──────────────┘
                                │ invokes
                                ▼
                 ┌─────────────────────────────┐
                 │  Orchestrator Agent         │ ← Claude Code session
                 │  (interpreting skill)       │   running the skill markdown
                 │                             │
                 │  - reads issue/PR labels    │
                 │  - claims work              │
                 │  - dispatches engines       │
                 │  - parses verdicts          │
                 │  - creates PRs from impl.  │
                 │  - reasons about failures   │
                 │  - proposes fixes to user   │
                 │  - applies transitions      │
                 └──────────────┬──────────────┘
                                │ shells out to
                                ▼
                 ┌─────────────────────────────┐
                 │  pr-pipeline CLI primitives │ ← Python toolkit
                 │  list / claim / dispatch /  │   (no orchestration)
                 │  transition / comment / ... │
                 └──────────────┬──────────────┘
                                │
                  ┌─────────────┼─────────────┐
                  ▼             ▼             ▼
              ┌────────┐   ┌────────┐   ┌────────┐
              │ claude │   │ codex  │   │   gh   │ ← external CLIs
              │ --print│   │  exec  │   │        │
              └────┬───┘   └────┬───┘   └────────┘
                   │            │
                   └────────────┘
                          ▼
                 ┌─────────────────────────────┐
                 │  per-task worktree           │ ← .worktrees/{issue,pr}-<N>/
                 └─────────────────────────────┘
```

Two layers of agency, separated cleanly:

- **Orchestrator agent** (the skill executor) — reasons about workflow, handles exceptions. One context per tick run, persists across all PRs in that tick.
- **Engine subprocesses** (claude `--print`, codex `exec`) — fresh context per dispatch, isolated from the orchestrator. They do the actual review/fix work in a worktree.

State lives in GitHub (labels + comments). The local filesystem holds worktrees and logs. The Python toolkit is stateless; the skill markdown encodes the procedure.

## State machine

Labels are the wire format. Names below are defaults; per-repo overrides via config. These are the labels pr-pipeline manages in **target repos**. They are distinct from the labels used to develop pr-pipeline itself (see `docs/working/pr-guidelines.md`).

### Issue states (implementation stage)

| Label | Meaning | Next action |
|---|---|---|
| `ai:available` | Issue ready for AI implementation | orchestrator claims |
| `ai:implementing` | Implementer claimed | (transient until PR opens) |

When an issue is labeled `ai:available`, the orchestrator claims it (`ai:available` → `ai:implementing`), creates a branch and worktree from the repo's default branch (typically `origin/main`; determined by `gh repo view --json defaultBranchRef`), and dispatches an implementation engine. On success, the orchestrator creates a PR whose body includes `Closes #<ISSUE>` (so the issue auto-closes on merge) and applies label `ai:implemented` to the PR. The `ai:implementing` label is removed from the issue on successful PR creation. The issue's claim marker is also cleared.

### PR states (active pipeline)

| Label | Meaning | Next action |
|---|---|---|
| `ai:implemented` | PR opened (by human or implementer), awaiting review | reviewer claims |
| `ai:reviewing` | Reviewer claimed | (transient until verdict posts) |
| `ai:changes-requested` | Review found issues | fixer claims |
| `ai:fixing` | Fixer claimed | (transient until fixes push) |
| `ai:recheck` | Fixes pushed, re-review needed | reviewer claims |
| `ai:ready-for-human` | Passed AI review | human merges |

### Escalation states (orchestrator handed off to human)

These represent **decisions the orchestrator agent escalated**, not crashes it couldn't handle. Most "failures" the orchestrator now resolves inline — these labels apply only when human input is genuinely required.

| Label | Meaning |
|---|---|
| `ai:needs-human-decision` | Fixer flagged scope/design call beyond its remit |
| `ai:max-iterations` | Review/fix cycles exceeded threshold; escalate before more spend |
| `ai:permission-pending` | Orchestrator proposed a settings change to the user; awaiting approval |

The original "permission-blocked" / "engine-error" / "tests-failed" terminal labels from v1 are gone. Permission errors are handled by the orchestrator (proposing settings updates inline). Engine errors are retried or routed via `ai:permission-pending` / `ai:needs-human-decision` based on cause. Test failures cause the fixer to emit `NEEDS_HUMAN_DECISION` if it can't resolve them.

### Transitions

```
issue:ai:available
    ──[orchestrator claims]──▶ issue:ai:implementing
                                ├─[implementation succeeds]──▶ creates PR with pr:ai:implemented
                                └─[blocked/failed]───────────▶ issue:ai:needs-human-decision

human-opened PR ──[labeled]──▶ pr:ai:implemented

pr:ai:implemented
    ──[reviewer claims]──▶ pr:ai:reviewing
                            ├─[no blockers]──▶ pr:ai:ready-for-human  ◀── done
                            └─[blockers]─────▶ pr:ai:changes-requested

pr:ai:changes-requested
    ──[fixer claims]──▶ pr:ai:fixing
                        ├─[fixes pushed]──▶ pr:ai:recheck ──┐
                        └─[needs human]───▶ pr:ai:needs-human-decision
                                                            │
pr:ai:recheck ──────────────────────────────────────────────┘
    ──[reviewer claims]──▶ pr:ai:reviewing  (loops, capped by max_iterations)

Any transient failure
    ──[orchestrator diagnoses]──▶ either retries inline OR
                                   transitions to ai:permission-pending
                                   with a settings diff for user approval
```

Iteration counter is tracked via PR-body marker (`<!-- ai-iter: N -->`) updated on each fix. Crossing the threshold (default 5) routes to `ai:max-iterations`.

## Components

### 1. CLI primitives (Python toolkit)

Single command `pr-pipeline` exposing granular subcommands. Each is small, deterministic, and testable. The orchestrator agent composes them.

#### Query

| Subcommand | Arguments | Description |
|---|---|---|
| `list-issues` | `--label <LABEL>` | JSON list of matching issues |
| `list-prs` | `--label <LABEL>` | JSON list of matching PRs |
| `context` | `<PR#>` | Full PR context as JSON: diff, issue, comments, iter, checks status, head metadata |

#### Claim

| Subcommand | Arguments | Description |
|---|---|---|
| `claim` | `<PR#> --from <L> --to <L> --owner <ID>` | CAS-like claim: verifies source label present, swaps labels, writes claim marker, re-reads to prove ownership. Exit 0 if claimed, exit 2 if lost race. `--owner` is a unique orchestrator identifier (format below). |
| `claim-issue` | `<ISSUE#> --from <L> --to <L> --owner <ID>` | Same semantics as `claim`, for issues. |

#### Render & dispatch

| Subcommand | Arguments | Description |
|---|---|---|
| `render-prompt` | `<stage> --pr <PR#> \| --issue <ISSUE#>` | Renders the prompt template for the given stage to stdout. The orchestrator is responsible for writing this to a temp file and passing the path to `dispatch --prompt-file`. |
| `dispatch` | `--engine <e> --pr <PR#> \| --issue <ISSUE#> --prompt-file <path> [--cwd <path>]` | Reads the prompt from `--prompt-file` and passes it to the engine subprocess via stdin. Returns JSON `{exit_code, stdout, stderr, duration_s, timed_out, sentinels, permission_failures}` and appends a JSONL dispatch log entry. `--cwd` overrides the worktree path; if omitted, derived from config. |

#### Transition

| Subcommand | Arguments | Description |
|---|---|---|
| `transition` | `<PR#> --from <LABEL> --to <LABEL>` | Swaps labels: removes `--from`, adds `--to`, clears claim marker. `--from` is required so the caller explicitly controls which label is removed. |
| `transition-issue` | `<ISSUE#> --from <LABEL> --to <LABEL>` | Same semantics as `transition`, for issues. |

#### Comment

| Subcommand | Arguments | Description |
|---|---|---|
| `post-comment` | `<PR#> --body-file <p>` | Posts a comment on the PR. |
| `post-comment-issue` | `<ISSUE#> --body-file <p>` | Posts a comment on the issue. |

#### Worktree & PR creation

| Subcommand | Arguments | Description |
|---|---|---|
| `worktree` | `<PR#> [--ensure\|--destroy]` | Manages the per-PR worktree. |
| `worktree-issue` | `<ISSUE#> [--ensure\|--destroy]` | Creates or destroys a worktree for an issue, branched from `origin/main`. |
| `create-pr` | `--issue <ISSUE#> --branch <B> --title <T> --body-file <F>` | Creates a PR from an implementation branch, links to the issue, applies `ai:implemented`, removes `ai:implementing` from the issue, and clears the issue's claim marker. Pre-flight: verifies branch exists on remote (exit 3 if not); checks for existing open PR linked to issue by head ref or `Closes #<N>` in body (exit 2 if found — outputs `{"existing_pr_number": N}` as JSON so the orchestrator can proceed with the existing PR). |

#### Housekeeping

| Subcommand | Arguments | Description |
|---|---|---|
| `iter` | `<PR#> [--bump]` | Reads or bumps the iteration marker in the PR body. |
| `labels-needed` | | Lists the labels the config requires. |
| `labels-create` | | Creates any missing labels in the repo. |
| `cleanup` | | Destroys worktrees for closed/merged PRs, and issue worktrees whose linked PR is closed/merged. Idempotent. |
| `pause` / `resume` / `paused` | | Global flag-file gate: pause, resume, or check whether the pipeline is paused. Only relevant for headless/scheduled deployments (`claude --print`, cron, `/schedule` agents) where there is no live interactive session to interrupt. In an interactive Claude Code session the user controls the loop directly. |
| `status` | `[--pretty]` | JSON list of all in-flight issues and PRs with their current label, owner, claimed_at, and iter. `--pretty` renders a human-readable table. |

No `tick` subcommand in v1. The orchestrator skill runs the loop. A `tick` subcommand for autonomous daemon mode is deferred to v0.5.

**`context` JSON schema:**

```json
{
  "pr_number": 12,
  "title": "Add OAuth login",
  "branch": "feat/auth",
  "base_branch": "main",
  "head_sha": "abc123",
  "head_repo_url": "https://github.com/owner/repo",
  "is_fork": false,
  "diff": "diff --git ...",
  "checks": "PASSING",
  "iter": 2,
  "review_comments": ["Missing error handling"],
  "issue_url": "https://github.com/owner/repo/issues/42",
  "issue_body": "We need OAuth2 support",
  "project_conventions": {"claude_md": "contents or null", "agents_md": "contents or null"},
  "body": "PR body text"
}
```

`project_conventions` is an object containing the contents of both convention files when present: `{"claude_md": "contents or null", "agents_md": "contents or null"}`. The orchestrator decides which to include in the rendered prompt based on the engine being dispatched (Claude engines use `CLAUDE.md`, Codex uses `AGENTS.md`). The linked issue is resolved by parsing the PR body for `Closes #<N>` / `Fixes #<N>` patterns.

**`status` JSON schema (default) / `--pretty` table:**

```json
[
  {"number": 12, "title": "Add OAuth", "label": "ai:reviewing", "owner": "host-1234", "claimed_at": "...", "iter": 2},
  {"number": 42, "title": "Fix bug", "label": "ai:implemented", "owner": null, "claimed_at": null, "iter": 0}
]
```

`--pretty` renders a human-readable table with columns: `#`, `Title`, `Label`, `Owner`, `Claimed`, `Iter`. Includes both issues in `ai:implementing` and PRs in active pipeline labels.

**`labels-needed` and `labels-create`:** These commands flatten all label config values into a unique set. Since `pr_ready_for_review` is a list (`[ai:implemented, ai:recheck]`), both entries are included. The total unique label count is 11.

Implementation language: **Python 3.11+**. JSON output by default for machine consumption. Human-readable output behind `--pretty` for direct user invocation.

### 2. Engine adapters

Same shape as before. Each adapter knows how to invoke its CLI, capture output, and detect permission/error patterns specific to that engine.

```python
class Engine(Protocol):
    name: str
    def run(self, prompt: str, cwd: Path, timeout_s: int) -> EngineResult: ...

@dataclass
class EngineResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    sentinels: set[Sentinel]
    permission_failures: list[str]
```

**Timeout handling:** Engine adapters catch `subprocess.TimeoutExpired`, return an `EngineResult` with `timed_out=True`, `exit_code=-1`, and empty sentinels. The `dispatch` CLI always returns valid JSON, even on timeout. The orchestrator treats timeout as a retriable failure (one retry, then escalate).

**Prompt delivery:** Engine adapters pass the prompt via `subprocess.run(input=prompt)` (stdin). This is the preferred method for long prompts to avoid shell argument length limits.

**v1 engine contract:** Claude Code is the implementer and fixer (via `claude --print`); Codex is the reviewer only. This split reflects each engine's strengths and avoids granting Codex unrestricted sandbox access.

Initial adapters: `ClaudeEngine` (invokes `claude --print`; permissions scoped via `--permission-mode` and `permissions.allow` in settings), `CodexEngine` (invokes `codex exec -s read-only -c approval_policy=never`; reviewer never needs to write files or commit, so `read-only` sandbox suffices). The per-stage `engine` config key exists for future flexibility, but v1 defaults and documentation encode this split: `implement`=claude, `fix`=claude, `review`=codex.

### 3. Worktree manager

Two modes depending on whether work starts from an issue or a PR:

**Implementation worktrees (from issues):**
- Path: `<repo_root>/.worktrees/issue-<NUMBER>/` — **temporary**; renamed to `pr-<NUMBER>/` after PR creation
- Created from `origin/main` on a new branch `agent/issue-<NUMBER>`
- The implementation engine works here, commits, and pushes
- After `create-pr` returns the PR number, the orchestrator immediately renames the
  worktree: `git worktree move .worktrees/issue-<N> .worktrees/pr-<PR>`. From this
  point forward, `pr-<PR>` is the canonical path and `issue-<N>` no longer exists.
- **Failure recovery — rename crash:** if the orchestrator crashes between PR creation
  and rename, the orphaned `issue-<N>` worktree is cleaned up by `pr-pipeline cleanup`.
  On the next tick, `worktree --ensure pr-<PR>` re-creates the worktree from the PR
  head SHA; no data is lost since the branch is already on the remote.
- **Re-implementation:** If branch `agent/issue-<N>` already exists, `ensure_issue`
  checks whether it has commits ahead of `origin/main`. If it does (implementation
  was pushed but PR creation failed), the branch is preserved so `create-pr` can
  retry without re-dispatching the engine. If it does not (e.g., a prior failed
  attempt that produced no commits, or a human resolved `ai:needs-human-decision`
  and re-labeled the issue as `ai:available`), the branch is reset to `origin/main`.
- Implementation always targets the same repo; fork PRs are not relevant here
  (fork handling applies only to the PR review/fix stages).

**PR worktrees (review/fix stages):**
- Path: `<repo_root>/.worktrees/pr-<NUMBER>/`
- For implementation-originated PRs: this worktree is the renamed `issue-<N>` worktree (see above); it already exists at the correct head SHA when review starts.
- For human-opened PRs: created on first claim, from the PR head SHA.
- Persists across stages, destroyed on PR close/merge.
- Created from the PR head SHA, not only `headRefName`. The GitHub
  context primitive must expose enough PR head metadata to fetch and checkout the
  correct commit deterministically: head ref, head SHA, head repository owner/name
  (and URL), base ref, and whether the PR comes from a fork.
- Same-repo PRs may use the local branch when it exists at the expected SHA.
  Fork PRs or missing/stale local branches must be fetched into an internal ref
  such as `refs/pr-pipeline/pr-<NUMBER>` before creating the worktree.
- Branch names are not globally unique. The worktree path is keyed by PR number,
  and checkout correctness is verified by matching `HEAD` to the PR head SHA.
- **Fork PR policy (v1):** Fork PRs are **review-only**. The reviewer runs
  normally in a fetched worktree, but if the verdict is `BLOCKERS_FOUND`, the
  orchestrator transitions to `ai:changes-requested` and posts a comment
  summarizing the findings for the fork author — it does NOT dispatch a fixer
  (the pipeline cannot push to an arbitrary contributor's fork). The fork author
  fixes manually and the PR re-enters review when updated. Auto-fix for fork PRs
  (via a maintainer branch or permission request) is deferred to a future version.

**Worktree visibility rules:**
- **Pre-commit review** (reviewing uncommitted implementation): run a read-only
  reviewer in the *same* worktree. A separate worktree would not see uncommitted edits.
- **Post-push review** (re-reviewing after fixes are pushed): use a detached
  worktree at the PR head SHA for a stable snapshot.

**Shared rules:**
- Engine subprocesses receive a **filtered environment** via an explicit allowlist (see Security considerations). The orchestrator process has access to the user's full env, but each engine adapter passes only the keys the engine needs (`HOME`, `PATH`, `SHELL`, plus engine-specific API keys and `GITHUB_TOKEN`). No `.env` file management is needed. Engine sandbox settings further constrain what engines can *do*: Claude Code permissions scope tool access; Codex's `read-only` sandbox plus `shell_environment_policy.inherit=none` prevents both network exfiltration and env inspection. In v1, Codex only reviews (read-only sandbox), and Claude runs with scoped permissions — no engine gets unrestricted access.

### 4. Prompt templates

- Plain Markdown with YAML frontmatter, one per stage (`implement`, `review`, `fix`) — same format as Claude Code skills
- Variable substitution via `string.Template` (stdlib, `$variable` syntax); no templating engine dependency
- Complex fields (e.g. `review_comments` list) are pre-serialized to formatted Markdown strings by `render-prompt` before substitution — no loops needed in the template
- User overrides at `<repo>/.pr-pipeline/prompts/<stage>.md`
- Strong defaults shipped in package
- Templates emit sentinel tokens the parser consumes (`IMPLEMENTATION_COMPLETE`, `NO_BLOCKERS_READY_FOR_HUMAN`, `BLOCKERS_FOUND`, `FIXES_PUSHED_RECHECK_NEEDED`, `NEEDS_HUMAN_DECISION`)

### 5. State machine module

Deterministic logic, called by primitives. Pure functions, no I/O.

Three stages: `IMPLEMENT`, `REVIEW`, `FIX`. Each maps sentinel sets to outcomes:
- `IMPLEMENT` + `IMPLEMENTATION_COMPLETE` → orchestrator creates PR (not a label transition; the function returns a `CREATE_PR` action)
- `IMPLEMENT` + no sentinel / error → `ai:needs-human-decision` on issue
- `REVIEW` + `NO_BLOCKERS_READY_FOR_HUMAN` → `ai:ready-for-human`
- `REVIEW` + `BLOCKERS_FOUND` → `ai:changes-requested`
- `REVIEW` + no sentinel (exit 0) → treat conservatively as `ai:changes-requested` (do not raise; ambiguous output from a reviewer defaults to "blockers found")
- `FIX` + `FIXES_PUSHED_RECHECK_NEEDED` → `ai:recheck`
- `FIX` + `NEEDS_HUMAN_DECISION` → `ai:needs-human-decision`
- `FIX` + no sentinel → `ai:needs-human-decision` (ambiguous fix output escalates)
- Any stage at `max_iter` → `ai:max-iterations`

Functions:
- `next_action(stage, sentinels, iteration, max_iter)` — given a stage and engine result, return the next action (label transition or `CREATE_PR`). CI check status is *not* gated here — the orchestrator skill reads `checks` from `context` and decides whether to proceed or wait before calling `next_action`. This keeps the state machine pure.
- `parse_iter_marker(body) -> int`, `bump_iter_marker(body) -> str`
- `parse_sentinels(output) -> set[Sentinel]`

`parse_permission_failures` lives in each engine adapter (not the state machine), since each engine has its own error message format.

**Iteration counter timing:** `iter --bump` is called by the orchestrator each time a FIX dispatch completes (regardless of outcome). The max_iterations check fires in `next_action` for FIX stage outcomes. REVIEW does not bump the counter — only fix cycles count toward the cap.

### 6. Skill bundle (the orchestrator brain)

Markdown files distributed as a Claude Code skill. The agent interprets them.

```text
plugin/
├── plugin.json                      # plugin manifest
├── commands/
│   ├── pr-pipeline-tick.md          # main loop procedure
│   ├── pr-pipeline-init.md          # interactive setup
│   ├── pr-pipeline-status.md        # render dashboard
│   ├── pr-pipeline-pause.md         # flip pause flag
│   ├── pr-pipeline-resume.md        # flip pause flag
│   └── pr-pipeline-reset.md         # unstick a PR
└── skills/
    └── pr-pipeline-troubleshoot.md  # exception-handling playbook
```

`pr-pipeline-tick.md` encodes the loop:

> 1. Run `pr-pipeline paused`. If paused, exit.
> 2. **Implementation queue:** Run `pr-pipeline list-issues --label ai:available`. For each (capped):
>    - CAS-like claim via `pr-pipeline claim-issue --from ai:available --to ai:implementing`. If lost, skip.
>    - Run `pr-pipeline worktree-issue --ensure` (creates branch from `origin/main`).
>    - Run `pr-pipeline render-prompt implement`.
>    - Run `pr-pipeline dispatch` in the issue worktree.
>    - Parse the JSON result.
>    - **If `IMPLEMENTATION_COMPLETE`** → engine has committed and pushed. Run `pr-pipeline create-pr` to open PR with `ai:implemented`. Clear issue label.
>    - **If blocked/failed** → transition issue to `ai:needs-human-decision`, post comment with error context.
> 3. **Review/fix queue:** Run `pr-pipeline list-prs` for each pickable label separately. Map labels to stages:
>    - `ai:implemented`, `ai:recheck` → **REVIEW** stage
>    - `ai:changes-requested` → **FIX** stage
> 4. For each PR (capped by `max_concurrent_per_tick`):
>    - CAS-like claim via `pr-pipeline claim`. If lost, skip.
>    - Run `pr-pipeline worktree --ensure`.
>    - Determine stage from the PR's current label (see mapping above).
>    - Run `pr-pipeline render-prompt <stage>`.
>    - Run `pr-pipeline dispatch`.
>    - Parse the JSON result.
>    - **If timed_out** → retry once; if still timed out, transition to `ai:needs-human-decision`.
>    - **If sentinels indicate `NO_BLOCKERS_READY_FOR_HUMAN`** → check `context.checks`. If checks are failing, treat as `ai:changes-requested` (fixer should address test failures). If checks are pending, skip this PR for now (re-pick on a future tick when checks resolve). Only transition to `ai:ready-for-human` when checks are passing or intentionally absent (no CI configured).
>    - **If sentinels are otherwise clean** → run `pr-pipeline transition --from <current> --to <next>` per state machine, post comment, log.
>    - **For FIX stage:** always run `pr-pipeline iter --bump` after dispatch, before checking sentinels.
>    - **If permission_failures non-empty** → invoke the `pr-pipeline-troubleshoot` skill.
>    - **If exit_code != 0 with no permission_failures** → analyze stderr; retry once if transient (rate limit, network), else escalate.
>    - **If `NEEDS_HUMAN_DECISION`** → transition to `ai:needs-human-decision`.

`pr-pipeline-troubleshoot.md` encodes the exception playbook:

> Given a `permission_failures` list:
> 1. Group by tool/command pattern.
> 2. For each, propose the minimal `.claude/settings.json` (or codex equivalent) edit.
> 3. Show the user the diff, ask for approval.
> 4. On approval: apply the edit, run `pr-pipeline transition` back to the input label, retry on the next tick.
> 5. On rejection or no response: transition to `ai:permission-pending` with a comment summarizing the proposed fix.

### 7. AGENTS.md snippet (Codex users)

Shipped in the repo as `examples/AGENTS.md.snippet`. Codex users append it to their AGENTS.md to get equivalent guidance: "When the user invokes `pr-pipeline tick`, follow this procedure...". No slash-command system needed; Codex follows the markdown directly.

### 8. Config

Per-repo `.pr-pipeline.yaml`:

```yaml
labels:
  issue_available: ai:available
  issue_implementing: ai:implementing
  pr_ready_for_review: [ai:implemented, ai:recheck]
  pr_changes_requested: ai:changes-requested
  pr_ready_for_human: ai:ready-for-human
  pr_reviewing: ai:reviewing
  pr_fixing: ai:fixing
  needs_human: ai:needs-human-decision
  max_iterations: ai:max-iterations
  permission_pending: ai:permission-pending

stages:
  implement:
    engine: claude       # v1: Claude implements (headless, scoped permissions)
    timeout_s: 3600
  review:
    engine: codex        # v1: Codex reviews (read-only sandbox)
    timeout_s: 3600
  fix:
    engine: claude       # v1: Claude fixes (headless, scoped permissions)
    timeout_s: 3600

worktree_root: .worktrees
max_concurrent_per_tick: 10
max_iterations: 5
```

Prompt template paths default to package-shipped defaults; users override by placing files at `.pr-pipeline/prompts/<stage>.md`.

## Skill orchestration flow (happy path)

When the user runs `/pr-pipeline-tick`:

```text
1. Read pause flag. If paused, exit.
2. Query GitHub for issues in ai:available and PRs in pickable states.
3. Build action queue: implementation issues first, then PRs (cap at max_concurrent_per_tick total).
4. For each implementation issue:
     a. CAS-like claim via `pr-pipeline claim-issue`.
     b. Create worktree from origin/main with branch agent/issue-<N>.
     c. Render implementation prompt, dispatch engine.
     d. On success: engine commits and pushes; orchestrator creates PR
        with ai:implemented, clears issue label.
     e. On failure: escalate issue to ai:needs-human-decision.
6. For each PR:
     a. CAS-like claim via `pr-pipeline claim`.
     b. Ensure worktree.
     c. Gather context, render prompt.
     d. Dispatch engine subprocess.
     e. Parse JSON result.
     f. Decide next label per state machine.
     g. Apply transition, post comment.
     h. On exception: invoke troubleshoot procedure (see below).
6. Print summary to user.
```

## Failure handling (rewritten)

Three tiers, in order of severity:

### Tier 1: Auto-recovered (orchestrator handles silently)

- Transient API errors (rate limit, network blip) — retry up to N times with backoff.
- Engine output missing sentinels but exit code is clean — re-prompt the engine or treat conservatively (default to "blockers found" for review, escalate for fix).
- PR creation failure after successful implementation push — retry `create-pr` directly; do NOT re-dispatch the engine (the branch already exists on the remote with the implementation commits).
- `create-pr` exits 2 (PR already exists from a prior crashed tick) — extract the PR number from the existing PR (by matching head ref or `Closes #<N>` in body), rename `issue-<N>` to `pr-<PR>` if `issue-<N>` still exists, then proceed as if `create-pr` had just succeeded. No re-dispatch needed.

### Tier 2: Pause-and-confirm (orchestrator proposes a fix to the user inline)

- Permission failure with an obvious settings remediation — orchestrator shows the diff, asks for approval, applies, retries.
- Engine binary missing or wrong version — orchestrator surfaces the install command, asks user to fix, then resumes.
- Config drift (e.g., new label not yet created in repo) — orchestrator proposes `pr-pipeline labels-create`, runs it on approval.

These are visible decisions in the user's session. The PR doesn't enter a terminal state; it stays claimed while the user resolves.

### Tier 3: Escalate to human (label transitions to escalation state)

- `NEEDS_HUMAN_DECISION` sentinel from fixer — design/scope call, by definition.
- Iteration counter reached `max_iterations` — too many cycles, stop spending.
- User declined a Tier 2 proposed fix — `ai:permission-pending` with proposal in PR comment.

These are the only labels that exit the active queue and require explicit human action to resume.

## Concurrency & locking

The `pr-pipeline claim` primitive is the lock; orchestrator agents on different
tickets/sessions must not double-claim.

- **Within a tick:** `max_concurrent_per_tick` caps how many engine subprocesses run in parallel.
- **Across ticks (or sessions):** claim is a compare-and-swap style operation,
  not a blind `gh pr edit`. The primitive must:
  1. Re-read the PR and verify the `--from` label is present and no incompatible
     in-progress claim label is present.
  2. Swap `--from` to `--to`.
  3. Write a durable claim marker containing PR number, target label,
     orchestrator id, and claimed-at timestamp.
  4. Re-read the PR and marker, then return exit 0 only if the target label and
     this orchestrator's claim marker are both present. If the source label was
     absent, another claim marker won, or the postcondition is ambiguous, return
     exit 2.
- **Atomic label swap:** All label transitions use a single `gh pr edit
  --remove-label X --add-label Y` invocation (or the equivalent `gh issue edit`),
  which maps to one API call. This prevents label-less limbo from a crash between
  two separate calls — it is not a full compare-and-swap (CAS-like safety comes
  from the 4-step claim procedure above, not from this single invocation).
- **Owner ID format:** `--owner` is a string identifying the orchestrator session.
  Format: `{hostname}-{pid}` (e.g., `macbook-12345`). Generated once at the start
  of the orchestrator's tick run and reused for all claims in that tick. This is
  sufficient for v0 (single-machine, manual ticks). If two `/loop` sessions run
  concurrently, their PIDs differ, enabling double-claim detection.
- **Claim marker:** v0 stores claim metadata in a body HTML comment (on the PR
  body for PR claims, on the issue body for issue claims) such as
  `<!-- pr-pipeline-claim: {"from":"ai:implemented","label":"ai:reviewing","owner":"macbook-12345","claimed_at":"2026-05-04T10:00:00Z"} -->`.
  The `from` field records the source label so that `/pr-pipeline-reset` can restore
  the correct pickable state (e.g., `ai:reviewing` back to `ai:implemented` vs
  `ai:recheck`).
  The marker is updated on each successful claim and cleared or overwritten on
  final transition. Both `claim` and `claim-issue` use the same marker format and semantics.
- **Race window (best-effort):** The claim procedure is not fully atomic — there
  is a window between reading the body, swapping labels, and writing the marker
  where two orchestrators could race. The re-read verification (step 4 of claim)
  detects most double-claims. If two markers appear, the later `claimed_at` wins
  and the other orchestrator backs off. This is acceptable for v0 where concurrent
  orchestrators are uncommon. A future version could use a single GraphQL mutation
  (`updatePullRequest` with `body` + `labelIds`) to reduce the window to near-zero.
- **Body clobber risk:** Claim marker and iter counter edits read, modify, and
  rewrite the full PR/issue body. Concurrent human edits to the body may be lost.
  Document this in the README and warn users not to edit PR bodies while the
  pipeline is active. A future version could move markers to PR comments
  (append-only, no clobber risk).
- **Stale claims (v0.5):** if a claim goes stale (crash mid-tick), the user sees
  the state via `pr-pipeline status` and can use `/pr-pipeline-reset` to unstick it.
  Auto-reset on every tick is deferred to v0.5.
- **No file-level locking needed** — worktree-per-PR isolation handles that.

## Observability

- **Per-tick narration:** the orchestrator agent narrates as it goes. The user sees every decision live.
- **Per-PR comments:** the pipeline posts short decision comments when it changes state. Comments are for human orientation, not a complete audit log.
- **Lightweight structured log:** Python toolkit appends one JSONL record per claim, transition, and engine dispatch (PR#, head SHA, stage, engine when applicable, duration, exit, sentinels, next label, compact error reason). Path: `<repo_root>/.pr-pipeline/logs/<YYYY-MM-DD>.jsonl`. This is enough for future agents to understand what happened without turning v0 into an observability product.
- **`pr-pipeline status --pretty`:** human-readable table of all in-flight PRs.

## Session continuity policy

Engine sessions can be resumed but should not be treated as required for correctness.

| Worker role | Session policy |
|---|---|
| Initial implementer | New session |
| Independent reviewer | Always fresh session |
| Fixer addressing review changes | Resume if available, new session if stale |
| Recovery after crash or stale context | New session in restored worktree |

The branch and filesystem state are authoritative. Session context is a productivity optimisation, not a correctness requirement.

## Guardrails

- **Scope engine sandbox permissions per stage and role.** Claude implementer/fixer permissions are scoped via `--permission-mode` and `permissions.allow`; Codex reviewer runs in `read-only` sandbox. No engine gets unrestricted access. The orchestrator proposes permission expansions to the user explicitly.
- **Do not create a new branch for every PR revision.** Revisions continue on the existing PR head branch (`agent/issue-<N>`), not `agent/issue-<N>-revision-2`.
- **Do not create a separate reviewer worktree before changes are committed.** The reviewer will not see uncommitted edits. Use same-worktree read-only review pre-commit.
- **Do not rely only on the worker's final prose.** Require sentinel tokens and independently validate tests, diff, branch identity, and PR status.
- **Do not nest engines inside engines.** Run implementation and review workers as sibling subprocesses from the orchestrator, not as Claude → Bash → Codex chains. Sandbox restrictions propagate to child processes and create unpredictable failures.

## Security considerations

- **Trusted-repo scope (v1).** The pipeline assumes the target repo is owned or controlled by the user running it. Untrusted third-party repos (where PR authors may inject malicious content into diffs, issue bodies, or prompt overrides) are out of scope for v1. Multi-tenant or public-repo support would require additional sandboxing and is deferred.
- **No `--dangerously-skip-permissions` ever.** When the orchestrator hits a permission, it proposes a scoped settings edit; the user approves explicitly.
- **Engine API keys** read from environment. Pipeline never stores or logs them.
- **GitHub auth** uses `gh` CLI's existing auth.
- **Prompt templates are user-controlled** but ship with safe defaults.
- **Engine environment allowlist.** Engine subprocesses do NOT inherit the full orchestrator environment. Each engine adapter passes an explicit allowlisted `env` dict to `subprocess.run`, containing only the keys the engine needs to function (e.g., `HOME`, `PATH`, `SHELL`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN` for Claude; `HOME`, `PATH`, `OPENAI_API_KEY`, `GITHUB_TOKEN` for Codex). This prevents accidental exposure of unrelated secrets (`AWS_SECRET_ACCESS_KEY`, database URLs, etc.) even if prompt injection convinces the engine to run `printenv`. For Codex, the adapter additionally sets `-c shell_environment_policy.inherit=none` so the sandbox itself enforces minimal env.
- **Output redaction before posting.** Before the orchestrator posts any engine output as a PR comment, it scrubs known secret patterns (prefixes: `sk-`, `ghp_`, `gho_`, `ghs_`, `AKIA`, `xox`; and any value matching env vars in the allowlist). This is defense-in-depth — the allowlist prevents the engine from knowing secrets, and redaction prevents accidental leakage of the few secrets the engine *does* need.
- **Engines push their own commits** within their subprocess (implementer and fixer). The orchestrator creates PRs and manages labels but does not commit or push on behalf of engines.
- **Pause flag** lives in `.pr-pipeline/paused` (untracked). Orchestrator checks it first thing every tick.

## Distribution

Three artifacts shipped together:

### A. PyPI package (`pr-pipeline`)

The Python toolkit. `pip install pr-pipeline`. Provides the `pr-pipeline` CLI with all primitives. Required by both the Claude Code skill and Codex users.

### B. Claude Code skill bundle (primary entry point)

Distributed as a plugin in the same repo (`/plugin` directory). Users install via Claude Code's plugin mechanism. Provides `/pr-pipeline-*` slash commands and the orchestration markdown.

### C. AGENTS.md snippet for Codex

A markdown file (`examples/AGENTS.md.snippet`) Codex users append to their repo's `AGENTS.md`. Encodes the same orchestration procedure. Codex agents follow it when the user types `pr-pipeline tick` (or similar trigger).

**Trust ramp:**

| Trust level | How to run | Setup |
|---|---|---|
| Day 1 | `/pr-pipeline-tick` manually | `pip install` + skill install + `init` |
| Week 1 | `/loop 10m /pr-pipeline-tick` | + active Claude Code session |
| v0.5+ | launchd plist running `pr-pipeline tick` subcommand (added in v0.5) | + plist install (deferred feature) |

## MVP scope

**MVP includes:**
- Python toolkit CLI (all primitives: list-issues, list-prs, context, claim, claim-issue, render-prompt, dispatch, transition, transition-issue, post-comment, post-comment-issue, iter, worktree, worktree-issue, create-pr, labels-create, cleanup, pause/resume/paused)
- Two engine adapters (Claude, Codex)
- Three stage templates (implement, review, fix) with strong defaults
- Deterministic state-machine module (sentinel parser, label transitions, iter counter)
- Per-issue and per-PR worktree manager
- Claude Code skill bundle (5 slash commands + troubleshoot skill)
- AGENTS.md snippet for Codex
- README covering install + per-repo setup

**MVP defers:**
- **Daemon mode (launchd).** Manual + `/loop` are the v0 trigger options.
- **Distinct test-failure label.** Rolls into `ai:needs-human-decision` for v0.
- **Multi-repo orchestration from one config.** One config per repo.
- **Web dashboard / GUI.** JSON logs + `status --pretty` are enough.
- **Linux/cron support.** macOS-first.

## Resolved decisions

- **Architecture:** skill-driven orchestration. Agent reasons; toolkit provides primitives. No local state files — GitHub labels and PR metadata are the sole source of truth.
- **Implementer scope:** in v0. Issues labeled `ai:available` are claimed and implemented by engine subprocesses.
- **Git mutations:** implementation engines commit and push their own work. Fixers push their own commits. The orchestrator creates PRs and manages labels but does not commit on behalf of engines.
- **Repo conventions:** generic `ai:*` scheme. Initialization adds labels; existing repo labels are not modified.
- **Iteration counter:** PR body HTML comment marker.
- **Test-pass signal:** `gh pr checks` via the `context` primitive.
- **Prompt template ownership:** strong defaults + user override path.
- **Trigger mechanism:** user-invoked slash command + optional `/loop`. No daemon in v0.
- **Failure model:** three tiers (auto-recover, pause-and-confirm, escalate). Most v1 "terminal" labels gone.

## Open questions

1. **Per-engine concurrency caps.** Global cap fine for v0; revisit if one engine throttles harder than the other.
2. **Orchestrator skill model.** Which Claude model handles orchestration? Default to whatever the user's session is running (Sonnet/Opus). Document that Opus pays for itself in fewer wrong decisions; Sonnet is fine for routine ticks.
3. **Skill bundle format.** Claude Code plugin structure exists; need to confirm exact `plugin.json` shape and slash-command directory layout against current docs.
4. **Codex parity.** Codex doesn't have slash-command primitives in the same shape. The AGENTS.md snippet works, but there's no "/pr-pipeline-tick" trigger — Codex users invoke by saying "run pr-pipeline tick" in their session. Acceptable for v0.
5. **Implementation engine permissions.** The implementer engine needs to commit and push. What's the minimal permission set? `Bash(git add *),Bash(git commit *),Bash(git push *)` plus sandbox network access to `github.com`. Need to verify this works end-to-end before shipping.

## Risks

- **Token spend at orchestration layer.** Adding an orchestrator agent on top of engine subprocesses means extra tokens per tick. Mitigated by: small primitive output (JSON, not prose), `max_concurrent_per_tick`, manual-trigger default. For 5 PRs in a tick, the orchestrator's overhead should be <10% of total spend if primitives return tight JSON.
- **Non-determinism.** The agent may handle similar situations differently. Mitigated by: tight skill markdown, explicit decision rules, sentinel parsing in code (deterministic). Worst case: a few unexpected transitions per week, all visible to the user.
- **Runaway loops.** Reviewer/fixer ping-pong. Mitigated by `max_iterations`. Threshold needs tuning during sworn dogfooding.
- **Engine version drift.** `claude --print` and `codex exec` interfaces may change. Engine adapters need version pinning or feature detection. Permission failure detection relies on string matching against engine-specific error messages (e.g., `"requires permission"` for Claude, `"sandbox: operation not permitted"` for Codex) — these patterns are fragile and should be updated when engine versions change.
- **Prompt template quality.** Headless agents behave differently than interactive sessions. Defaults need real-PR tuning before they're shipped publicly.
- **Codex skill parity gap.** Codex users get a less polished UX than Claude Code users in v0. Acceptable trade-off; can build a Codex-specific skill plugin in v0.5 if there's demand.
- **Implementation stage scope risk.** Issues vary wildly in complexity. The implementer engine may produce incomplete, incorrect, or architecturally wrong implementations. Mitigated by: the review stage immediately follows; `max_iterations` caps fix cycles; `ai:needs-human-decision` is the escape hatch. Start with well-scoped issues during dogfooding.

## Next steps

1. Rewrite implementation plan to match this architecture (smaller Python surface, larger skill-bundle surface).
2. Execute via subagent-driven development.
3. **First milestone:** primitives CLI + engine adapters + implement stage + review stage + skill `pr-pipeline-tick.md`. One issue flows `ai:available → ai:implementing → [PR created] → ai:implemented → ai:reviewing → ai:ready-for-human` end-to-end via the skill.
