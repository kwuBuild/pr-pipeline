# PR Pipeline Internals

Canonical reference for current runtime mechanisms and their source owners.
For commands and configuration examples, use the [operator README](../../skills/tick/README.md).
For design decisions, use the [architecture rationale](../architecture.md).
The [distribution plan](plugin-distribution-design.md) retains unfinished migration requirements.

---

## 1. Engine launch summary

| Property | Implementer | Fixer | Reviewer |
|---|---|---|---|
| Binary | `claude -p` | `claude -p` | `codex exec` (via `engine_wrapper.py`) |
| Permission mode | `--permission-mode auto` (config/CLI can select `dontAsk`) | `--permission-mode auto` (config/CLI can select `dontAsk`) | `-s read-only` (sandbox) |
| Pre-approved tools | `--allowedTools` ([`tick._engine_allowed_tools`][tick]); auto may approve more | `--allowedTools` ([`tick._engine_allowed_tools`][tick]); auto may approve more | None (read-only sandbox enforces) |
| Model (default) | whatever `claude` resolves (no `--model` flag) | whatever `claude` resolves / `deepseek-v4-pro[1m]` (claude-ds) | operator's `$CODEX_HOME/config.toml` (else `~/.codex/config.toml`), else `gpt-5.5` |
| Model override | `--model` flag from tick CLI | `--model` flag from tick CLI | `--review-model` flag from tick CLI |
| Reasoning effort | Not set (claude default) | Not set (claude default) | operator's codex config, else `high`; `--review-effort` overrides |
| Env isolation | Full parent env via `_engine_env()` ([`tick._engine_env`][tick]) | Full parent env via `_engine_env()` ([`tick._engine_env`][tick]) | scrub-dir with `HOME`/`CODEX_HOME` redirect ([`tick._dispatch_one_reviewer`][tick]) |
| Prompt source | `lib/prompts/implementer.txt` | `lib/prompts/fixer.txt` | `lib/prompts/reviewer.txt` |
| Output capture | stdout → `result.json` | stdout → `result.json` | final output → atomic `review.json`; JSONL stdout → bounded sanitizer |
| Progress heartbeat | `heartbeat` mtime, bumped per output event (≤1 write/5s) | `heartbeat` mtime, bumped per output event (≤1 write/5s) | none — JSONL is sanitized for observations, not a heartbeat; the review timeout bounds the run |
| Timeout | 60 min (`IMPLEMENTER_TIMEOUT`, [`tick.IMPLEMENTER_TIMEOUT`][tick]) | 60 min (`FIXER_TIMEOUT`, [`tick.FIXER_TIMEOUT`][tick]) | 20 min (`REVIEW_TIMEOUT`, [`tick.REVIEW_TIMEOUT`][tick]) |
| Process group | `start_new_session=True` | `start_new_session=True` | `os.setpgrp()` in wrapper ([`engine_wrapper.run_engine`][engine_wrapper]) |

## 2. Model and reasoning effort

### Implementer

Default model: whatever the `claude` CLI resolves. With no override,
`_launch_claude_engine` omits `--model` entirely ([`tick._launch_claude_engine`][tick]).
Pinning a model here would silently override the operator's configured default,
which is the one thing the default is meant to honor.

The contract is "the CLI's own resolution", not "the user settings file"
specifically — Claude Code's settings precedence puts a checked-in
`.claude/settings.json` above `~/.claude/settings.json`, and the CLI flag above
both. Neither project settings file in this repo sets `model`, so today the
resolved model is the user default; if one ever does, it wins, and for the fixer
(whose worktree is the PR head) that file is branch content. Pass `--model` when
a run must be pinned independent of the branch.

When `--impl-engine claude-ds` is active, the model resolves to
`deepseek-v4-pro[1m]` by default. The `_resolve_engine` function
([`tick._resolve_engine`][tick]) sets `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, and `CLAUDE_CODE_SUBAGENT_MODEL` to the resolved
model, routing Claude Code's internal model selection through the DeepSeek API
gateway.

The tick CLI accepts `--model <model>` ([`tick.main`][tick]) which overrides
the per-engine default for both the `--model` flag and `ANTHROPIC_MODEL`.

Reasoning effort is not explicitly set. Claude Code uses its own default.
For claude-ds, `CLAUDE_CODE_EFFORT_LEVEL=max` is set ([`tick._resolve_engine`][tick]).

### Fixer

Same as the implementer — the operator's own model (claude) or
`deepseek-v4-pro[1m]` (claude-ds). Model resolution at [`tick._dispatch_one_fixer`][tick].
Uses `_launch_claude_engine` with the same `--model` handling and env construction.

### Reviewer

Model and effort resolve in this order, independently per value:

1. `--review-model` / `--review-effort` from the tick CLI
2. `model` / `model_reasoning_effort` in the operator's codex config —
   `$CODEX_HOME/config.toml` when set, else `~/.codex/config.toml`
   (`_operator_codex_home`, which the scrub-dir `auth.json` symlink shares)
3. the pinned fallbacks `gpt-5.5` and `high`

Profiles are deliberately not consulted. Since codex 0.146 the legacy
`profile = "…"` key is rejected by codex itself (`legacy profile config is no
longer supported; use --profile <name> with <name>.config.toml`), and profiles v2
layer only when `--profile` is passed on the command line — which the pipeline
never does. Top-level config is therefore the operator's effective default.

`resolve_reviewer_model_settings` performs the resolution and the dispatch
forwards the result as `--model <m>` and `-c 'model_reasoning_effort="<e>"'`.

The reviewer cannot inherit the config the way the claude engines inherit their
environment. It runs under `env -i` isolation with `HOME`/`CODEX_HOME` pointed
at a per-PR scrub dir, *and* `--ignore-user-config` ([`tick._dispatch_one_reviewer`][tick]).
Dropping that flag would not help — `$CODEX_HOME/config.toml` does not exist in
the scrub dir. So the tick, as the trusted parent, reads the file and passes the
values explicitly.

**Only `model` and `model_reasoning_effort` are read.** This is a security
boundary, not a convenience shortcut: the reviewer is the one engine whose
entire input is attacker-controlled PR-head content, and a real operator config
also carries `[mcp_servers]` (a node REPL is arbitrary code execution),
per-project `sandbox_mode`/`network_access`, hooks, and `notify`.

Every value is shape-checked **for the sink it reaches**, flags included — the
resolver is the single point all entry paths funnel through:

- effort → interpolated into `-c model_reasoning_effort="…"`, so it must be a
  bare token; otherwise a hand-edited entry could close the quote and set a
  second, unreviewed key
- model → its own argv element, so `/` and `:` in provider-qualified ids are
  fine, but a leading dash is not: codex would read it as an option

A rejected value is reported on stderr and falls through to the next source. A
missing, malformed, or wrong-typed config falls back to the pins rather than
aborting the dispatch or letting Codex use its own built-in defaults.

Each reviewer dispatch logs the resolved pair (`action=model_resolved`), so a
review's provenance survives a later edit to the operator config.

## 3. Hook applicability

The pipeline does not install or enforce an operator's pre-PR hooks. Hook
behavior belongs to the engine host and the settings it loads:

- **Implementer and fixer:** [`tick._launch_claude_engine`][tick] launches
  `claude -p` without `--bare`. Non-interactive mode alone does not disable
  Claude hooks; configured hooks can run. See the host's
  [programmatic-mode documentation](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode).
- **Reviewer:** [`tick._dispatch_one_reviewer`][tick] launches Codex, which does
  not use Claude Code's hook configuration. Its own isolation is described in
  [Reviewer](#reviewer-1).
- **Controller:** [`tick.gh`][tick] invokes `gh` directly from Python. A Claude
  `PreToolUse` hook on a `Bash(gh pr create...)` call cannot intercept it.

An interactive pre-PR hook is therefore not proof that the controller's PR
creation passed the same gate. Pipeline completion and review requirements
must hold independently of optional host hooks.

## 4. CLAUDE.md / AGENTS.md inheritance

### Governing-file contracts per engine

Each engine runs with `cwd` set to an isolated git worktree:
- Implementer: worktree at the configured implementation base on the selected remote
  ([`tick._dispatch_one_implementer`][tick])
- Fixer: worktree at the PR's head branch
  ([`tick._dispatch_one_fixer`][tick])
- Reviewer: worktree at the PR's head SHA (detached)
  ([`tick._dispatch_one_reviewer`][tick])

**Implementer and fixer** (Claude Code, `claude -p`): Claude Code loads
`CLAUDE.md` files in the directory hierarchy above `cwd`. This repository's
`CLAUDE.md` contains only `@AGENTS.md`, so its shared rules are included through
the import rather than maintained twice.

Claude Code 2.1.277+ also supports native `AGENTS.md` discovery by default when
no project/local `CLAUDE.md` exists in `cwd` or its ancestors. That fallback is
unavailable on third-party provider routes such as `claude-ds`, with telemetry
disabled, and in some hook/configuration modes. The one-line import preserves
compatibility with those routes and older clients; a qualifying ancestor
`CLAUDE.md` can otherwise suppress the fallback in nested worktrees. See
[Claude's instruction-loading documentation](https://code.claude.com/docs/en/memory#agentsmd).
These are host discovery rules, not extra reads performed by the pipeline.

The fixer and the implementer each additionally get a **prompt-bound skill
doc**: the prompt names an install's `skills/<name>/SKILL.md` as an absolute path
(from `tick.FIXER_SKILLS` or `tick.IMPLEMENTER_SKILLS` via
`pipeline.skill_doc_path`, substituted at render) and requires it to be read
before editing. The fixer binds `review-response-hygiene`. The implementer binds
`definition-of-done`: Phase A before its first edit, Phase B before its commit,
and the Phase B evidence block (or the skill's `n/a` line) under
`## Test coverage` in `.pr-body.md` before `IMPLEMENTATION_COMPLETE`. The harvest
checks that body's section structure only, not the block's content, so the
evidence gate is a prompt obligation whose result is published in the PR body,
not a harvest validation. The implementer must escalate if required guidance
or proof is unavailable; see [Prompt-bound skills](../../skills/tick/README.md#prompt-bound-skills-skill_unresolved)
for the permission, exemption, and evidence-refresh contract. This is a doc
path rather than a `Skill` invocation because the allowlist in §5 carries `Read` and no `Skill` tool. The
doc ships with the pipeline, so the operator's installed copy governs every
target repository — a target that has never heard of the skill still gets an
engine whose prompt names it, and a branch cannot hand its fixer a rewritten copy
of the rules. Dispatch resolves the path against the install before launching;
a doc missing from the install is a packaging defect, and it logs
`skill_unresolved` and escalates instead of running an engine that would
silently read nothing.

The fixer also gets two documents it has no pre-approved way to fetch — `gh`
and web access are not granted — read by dispatch and substituted in: the **PR body** and the
body of **every issue GitHub reports under `closingIssuesReferences`**. These are
what `review-response-hygiene` rules 2 and 5 operate on, so without them the
prompt demands a check the engine cannot perform. Both are authoritative reads
that retain custody on failure, and both are capped at
`tick.FIXER_CONTEXT_BUDGET` with the cut announced in the text — the body against
one budget, the linked issues sharing a second one shortest-first via
`tick._split_context_budget`, so a long first contract cannot consume the whole
budget and leave a later issue with no heading at all.

The same dispatch read also pins the marker's `pr_body` provenance block — the
body's digest, the closure contract it declares, and whether the engine was shown
all of it — which is what lets the fixer hand a **replacement PR body** back. It
is instructed to leave `gh` writes to the controller: after its final push it writes `.pr-body-revision.md` in its
worktree root under a two-line envelope (`pr-body-revision: <dispatch_id>`, then
`pr-body-revision-head: <pushed OID>`), and the harvest checks the envelope, the
closure contract, the sections, and the live body against the pinned digest, then
re-reads labels/head/body one last time and applies it with
`gh pr edit --body-file` before the `ai:revised` relabel. Only a `recheck`
outcome publishes one; every other verdict — `pipeline._BODY_REVISION_NOTES`
carries one per cause, from an unreadable or oversized file through stale-head,
closure-drift, truncated, invalid, and conflicting to an escalated run — leaves
the live body untouched and names its own reason in the fix comment. A hold or drift caught at that
final read defers the entire harvest instead. See the README's "The PR body the
fixer hands back".

**Reviewer** (Codex CLI, `codex exec`): Codex loads applicable `AGENTS.md`
instructions from the directory hierarchy as project guidance. This repository's
shared guide routes directly to task-specific references; Codex does not need to
read the Claude import adapter. Other target repositories can route differently.
Further review checklists and architecture sections are consulted for the task,
not injected as a mandatory reading stack. Guaranteed repository guidance is
`AGENTS.md` ([reviewer design rationale][design-reviewer-ctx]). The reviewer
prompt itself is minimal (`Review PR #...`); it does not pin a reading list
or explicitly apply any checklist ([reviewer prompt][reviewer-prompt]).

### Policy gate: `touches_review_policy`

With `allow_touching = false`, reviewer dispatch checks whether the PR's diff
modifies any file that governs the reviewer's own behaviour
([`tick._dispatch_one_reviewer`][tick]). If so, the PR is escalated to human review.

The gate is operator-configurable: `allow_touching = true` under `[defaults]` or
`[repos."owner/name"]` in `~/.claude/pr-pipeline/config.toml` lets policy-editing
PRs flow to auto-review anyway (the operator accepts that the review runs under
instructions the PR itself supplies). The packaged floor ships `true` together
with operator-only issue pickup; reviewer admission also requires the operator.
An absent operator setting inherits the floor. A missing floor or mistyped
`allow_touching` falls back to `false`; malformed operator TOML refuses startup.
Set `false` to require human review for policy edits. Widening issue authors
must restore the shipped `false` default in the same change; see the skill
README's "Admission gate: reviewer policy files" section.

The gate function `touches_review_policy` ([`pipeline.touches_review_policy`][pipeline])
matches changed paths against:

**Basenames** (matched at any directory level, because both Claude Code and Codex
load governing files from the nearest match up the tree) — `_POLICY_BASENAMES`
([`pipeline._POLICY_BASENAMES`][pipeline]):
- `CLAUDE.md`
- `AGENTS.md`

**Operator-declared paths** — the resolved `checklist_paths`, read from
`resolve_config` ([`pipeline.resolve_config`][pipeline]). Repo-relative, matched exactly, and
empty by default: a target repo's own review checklists are the target's to name,
so the pipeline ships none. Plus the suffix set below:

**Path suffixes** (matched per path segment, so the rule is one rule at every
install depth — `.claude/skills/` in a host repo, `skills/` in the pipeline's
own repository, or a plugin root) — `_POLICY_PATH_SUFFIXES`
([`pipeline._POLICY_PATH_SUFFIXES`][pipeline]):
- `skills/tick/config.toml` — the packaged floor, which is where the
  gate's shipped default lives; while the gate is enabled, a PR flipping its
  `allow_touching` is escalated to a human (a merged flip would otherwise disable
  the gate for every future policy-editing PR). The operator's own config is
  outside every repository, so no PR can edit it
- `skills/tick/lib/pipeline.py` and `lib/tick.py` — the
  gate's own implementation; a PR that rewrites `touches_review_policy` to
  return `False` unconditionally, or removes the gate call from the reviewer
  dispatch, is the same silent-disable vector, so pipeline-code PRs escalate to
  a human while the gate is enabled
- `skills/tick/resolve-target.sh` — selects the repository whose review policy
  startup loads and validates the effective worktree destinations. Changing
  this authority requires the same policy review as changing the controller
- `skills/<name>/SKILL.md` for every `pipeline.BOUND_SKILLS` entry, and every
  file under `skills/tick/lib/prompts/` — the instructions the install *supplies*
  to a dispatched engine. Sourcing them from the install already stops a PR from
  governing its own fixer; the gate covers the other half, that a PR rewriting
  them for every *future* run is not auto-reviewed under the rules it changes.
  Both sets are **derived** from the constants the runtime reads — the skill
  names from `BOUND_SKILLS`, the directory from `pipeline.PROMPTS_DIR` — so a
  skill bound by a future stage, or a prompt template added for one, is gated by
  landing in the runtime rather than by being restated in the path list. The
  prompts entry is matched against the path's *parent* segments
  (`_POLICY_DIR_SUFFIXES`), because a template that does not exist yet cannot be
  enumerated

Rationale: a PR that edits these files supplies instructions the reviewer itself
would follow — "untrusted input controls the validator." The gate prevents the
reviewer from auto-approving a PR that modifies the very rules used to evaluate it
([`pipeline.touches_review_policy`][pipeline]).

## 5. Security posture per engine

### Implementer and fixer

Permission model: `--permission-mode auto` + `--allowedTools` by default;
operator configuration or the CLI can select `dontAsk`
([`tick._engine_allowed_tools`][tick], [`tick._launch_claude_engine`][tick]).

The `--allowedTools` list supplies narrow pre-approved grants
([`tick._engine_allowed_tools`][tick]):
```
Read Edit Write
Bash(git add *) Bash(git commit *) Bash(git status) Bash(git diff *)
Bash(git status *) Bash(git rev-parse *) Bash(git merge-base *)
Bash(git log *) Bash(git push <remote> HEAD)
<one Bash(...) entry per resolved verify_tools value>
```

`<remote>` is the validated remote selected at startup; the fixer uses the
exact `HEAD:refs/heads/<head-ref>` refspec on that same remote.

The verification entries are the operator's, not the pipeline's. The packaged
floor is empty. Dispatch validates and resolves the grants, wraps each in
`Bash(...)`, and passes that same tuple to the launcher while pinning
`verification_unconfigured` in the implement/fix marker. A configuration edit
between lease publication and launch cannot change those grants. The broader
per-tick configuration snapshot remains issue #26. Implementer dispatch also
pins `permission_mode` and the unwrapped `verify_tools` list in that lease.
Harvest uses these optional fields for the [escalation report](../../skills/tick/README.md#escalation-publication-coverage),
so the report describes the launch policy even if the operator later edits config.
Old markers remain valid and report the missing policy as unknown.

A malformed list (including parentheses/newlines in a permission body) is an
error, never an empty grant. A valid empty list dispatches with a
`verification_unconfigured` log entry and a prompt requiring the exact line
`VERIFICATION_UNCONFIGURED_WAIVER` immediately before the success sentinel.
Harvest validates that waiver against the marker and the same final sentinel
occurrence that supplies the outcome, never against current config. No-change
fixer completion requires the same waiver outside its finding-summary blocks.
Legacy markers without the field retain their old completion contract.

The controller prefixes `pipeline.VERIFICATION_WARNING` as a standalone opening
paragraph at PR creation, outside engine-authored Markdown/HTML context, and
preserves it on fixer body replacements even after grants change. Implementation
handoff checks the live body/head/holds on both created and rediscovered PRs;
missing warnings are added through the guarded body-write path before labels
or cleanup, preserving progressed PR workflow state. An empty-grant fixer with
no body replacement uses the same guarded warning path. Missing evidence or
failed writes cannot advance that run without the warning. This is a waiver of
missing grants, not proof of test execution or permission to skip the target's
required checks.

These grants cover commit + the stage's exact push. Under `dontAsk`, calls that
would prompt are denied; under `auto`, calls beyond these grants can reach the
classifier. The list therefore does not forbid force-push, remote-branch
deletion, or arbitrary refspec push under auto. Operator-owned deny rules are
the mitigation; the launcher never passes `--dangerously-skip-permissions`.
Classifier denial can fail the run, and flag acceptance is not proof of a
route's capabilities. Setup's route probe measures the route and records the
operator's chosen mode per repository; tick uses the selected value without an
automatic fallback.
See [engine permission modes](../../skills/tick/README.md#engine-permission-modes).

Environment contract: `_engine_env()` ([`tick._engine_env`][tick]) preserves
the operator's complete parent environment. Implementers and fixers are trusted
local engines running with the same ambient credentials and capabilities as the
terminal that launched the tick. This keeps dispatch terminal-agnostic: cmux
variables are present when launched from cmux and absent elsewhere. The reviewer
retains a separate scrubbed environment because it has a different trust posture.

Publication surface: implementer result escalations publish
`pipeline.render_implementation_escalation_comment`; its fields and limits are
owned by the [operator report contract](../../skills/tick/README.md#escalation-publication-coverage).
A fixer **escalation** echoes a bounded excerpt of the
engine's `result` text into a PR comment (`pipeline.render_fix_comment`), so
branch-controlled text reaches a public comment. This does not widen the trust
boundary — a fixer already commits and pushes to the PR, an unbounded channel —
but the excerpt is truncated and stripped of `<!--`/`-->`, fence, and
`</details>` sequences so engine text cannot forge a pipeline marker (which would
inflate the `count_review_rounds` iteration cap) or break out of its rendered
block. A `recheck` publishes the same bounded excerpt: only the `escalate` branch
archives harvest evidence, so on a successful fixer run the PR comment is the sole
durable record of what the engine changed. The attempt-cap and transient
escalations publish no engine text at all — they report a failed side effect and a
spent retry budget respectively, both controller-owned facts.

Publication ordering: a transition that reports an engine result, or fires before
one was launched, should explain itself before the label moves. The
[coverage map](../../skills/tick/README.md#escalation-publication-coverage) names
which paths meet that requirement and which remain silent. The comment gate
holds on the implemented paths wherever the retry can be bounded. It is
deliberately released in the two places where it cannot: the attempt cap (whose
remote writes are what keep failing) and the transient path's attempt-counter
write failure (whose retry budget is what failed to persist). Both log
`escalation_comment_failed` when the best-effort post misses. Transient
exhaustion is gated *and* bounded — a failed post burns an attempt, so repeated
failure terminates at the cap rather than looping. The held completion — whose
gated mutation is the release of the pipeline's own in-flight label rather than a
move to a terminal one — burns no attempt; its bound is the hold itself, since
the item is already awaiting the operator whose hold stopped the harvest, and
resolving it is what frees the retained slot.

The held completion is also the one gated post that carries no branch-controlled
text: `pipeline.render_held_completion_comment` accepts only a verdict or sentinel
that stage's own validator already returned, and rejects any other value, so the
published outcome is drawn from a closed set rather than excerpted from the
engine.

The pre-launch dispatch escalations are bounded against that same counter
(`_publish_dispatch_escalation` → `_dispatch_attempts`, resolved from the
dispatching *stage*'s config so it is the file that stage's harvest cap reads —
keying on the entity would have worked only while `review` and `fix` share a
prefix). A failed post there costs nothing but a held label — no engine, no
admission slot, no marker of its own — and the item stays at its ordinary pickup
label. That is exactly why it
needs a bound rather than pure gating: an unpostable comment would otherwise
leave the item looking like available work on every future tick. At the cap the
label lands unexplained, and `_end_dispatch_escalation` retires the receipt once
it does. It clears the counter only when the path did *not* retain the marker:
the ambiguous-marker path leaves one on disk by definition, where a mid-retry
harvest may be counting into the same file, and clearing would reset that budget
and delay its own cap-based cleanup of the scratch just retained.

What publishes is bounded; what stays silent is its complement. Exactly two
functions run `gh <entity> comment`: `_harvest_one_reviewer` (the reviewer
verdict, posted directly and gating its relabel — the only post with no receipt,
so a relabel failure after a landed post reposts it next tick) and
`_post_harvest_comment` (everything else, under `fix-comment`, `cap-escalation`,
`transient-escalation`, or `dispatch-escalation`). **Every other terminal
transition publishes nothing.** The silent set is stated this way round because
enumerating it failed three times (counted as four, then six, then hand-named as
four, each an undercount): it is large, spans reconcile, harvest, and dispatch,
and was maintained by hand in three documents. The publishing side is two call
sites, pinned by `test_tick_publishes_from_only_two_places`; the escalating side
is a census pinned by
`test_every_needs_human_transition_is_a_recorded_decision`, so a new route to the
hold label fails until someone records whether it explains itself. That census
fails closed — a site passing a computed label counts even when it usually
resolves elsewhere — because recognising only `pipeline.LABEL_NEEDS_HUMAN` would
have missed both reconcile escalations, which pass a bare string and a
guard-proven variable respectively, and those are the shape a new silent path
would copy.

The remaining silent escalation paths and their evidence locations are listed
in the [coverage map](../../skills/tick/README.md#silent-transitions). Tick
maintainers own those follow-on gaps. They include unreadable reviewer verdicts,
wrapper-confirmed reviewer failures, reconciliation without a usable result,
and post-launch label failures where the engine could not be terminated.

Receipt custody: the "comment landed" receipts live under `WT_ROOT/.receipts/`,
deliberately outside the item's scratch. A fixer scratch is a checked-out worktree
of the PR branch, so a receipt stored there would be author-controlled and a
tracked file at that path could forge the proof — suppressing the comment while
the label still moved. Receipts are keyed to the run's `dispatch_id` *and* to the
decision they published (target label plus rendered body), and record the posted
comment's URL. The decision half matters because a harvest recomputes its
decision from a fresh head read on every retry: keyed on the dispatch alone, a
first attempt's escalation comment would suppress the repost while a later
attempt lands `ai:revised` under it.

The `dispatch-escalation` kind is the one exception, and inverts that reasoning.
It has no `dispatch_id` to key to — it fires before a marker is published, or
over one whose identity is exactly what could not be read — and a per-run id
would be actively wrong there, because every tick re-attempts the *same* failed
setup and would repost under a fresh id each time. It therefore uses the constant
`_DISPATCH_RECEIPT_ID`, leaving the decision key to do the whole job: the same
failure is suppressed, a different one publishes. Being constant, it is also the
only kind a later dispatch cannot fail to match, so it is retired explicitly —
by `_end_dispatch_escalation` when the label lands, and by
`_discard_comment_receipts` if the item reaches a terminal harvest state with one
still on disk.


Terminal-intent custody: a terminal transition — the attempt cap, transient
exhaustion, or a result-driven stage escalation — records its decision to
`WT_ROOT/.terminal/<entity>-<N>.json` before attempting the relabel and archive
move it already decided — and, on the fixer, before the run comment that names
that terminal label. The record is typed by kind (`cap`, `transient_exhausted`,
`escalate`) and carries the payload needed to reproduce the original comment
exactly; the resume validates it as a whole against that closed schema, so an
unreadable, malformed, or unknown-kind record retains custody for inspection
(`terminal_intent_unresolved`) rather than defaulting into a transition nobody
decided. The one accepted exception is the legacy key set (`dispatch_id`,
`label`, `stage`, plus the `phase` a resume may since have added), which is
read as the cap it could only have been — an upgrade that landed
mid-transition must not pin the item its intent exists to release. First sight
rewrites it in the current schema and pins its failure count from the attempt
counter, which the transition has not yet cleared; without that pin a re-entry
after a failed archive move would derive a different count and publish a
second cap comment reporting a failure that never happened. The write is a
required gate: a branch that cannot durably record its decision does not start
the transition at all.
Reconciliation resumes a pending intent for its marker's `dispatch_id` before
computing any fresh decision — including the generic hold branch — so a failed
relabel *and* a failed final archive move are retried as the original
transition rather than replaced by the run's own (now superseded) label or
republished as a held completion under the pipeline's own terminal label. The
intent and its comment receipts are retired only after the final archive move
lands. A resumed transition re-attempts the relabel only from a state that
proves it never ran: the phase the intent records once the relabel lands, or a
live label set still carrying the stage's in-flight label with no hold beside
it. Sitting exactly at the decided target means the relabel already landed;
anything else — a third label, no label — belongs to a later actor, and the
resume logs `terminal_relabel_ambiguous` and finishes local custody without
touching the labels, because converging there would strip a human's transition
and re-apply a superseded terminal decision. The in-flight label present
*beside a hold* is its own state: convergence refuses a hold by design, so the
resume logs `terminal_relabel_held` and makes the one mutation the hold permits
— releasing the pipeline's own in-flight label — rather than re-attempting a
converge that can never land and leaving the item looking pipeline-held until
the operator acts. What that release buys is an honest label set, not a freed
slot: admission counts local markers, never labels, so the spend slot is
released by the custody move below that destroys the marker. That release is
gated like any other remote mutation: a failed one
retains the marker, attempts, in-flight label, and intent for the next tick.
Every release in the module names the hold that justifies it and revalidates it
on both sides of the removal; `_release_in_flight_label` takes
`required_hold_label` without a default, so a decision arriving without that
provenance is refused rather than silently released on an inapplicable guard.
Each of the three then reads the hold once more before the local custody move
that destroys the marker, and restores the released in-flight label when it has
gone — the read-back inside the release proves the hold survived the removal,
not the archive move that follows it.

Skill-usage telemetry: Claude runs with `--output-format stream-json --verbose`
behind the controller installation's `lib/claude_stream_wrapper.py`. The
wrapper owns Claude's stdout pipe, extracts `Skill` tool-use blocks from
assistant protocol events, and publishes only the terminal result object plus
`skill_usage_status` and `skills_used` to `result.json`. Branch-controlled tool
output cannot become a protocol tool-use event, and no host-privileged hook,
engine-visible telemetry path, marker file, or branch-writable dispatch lookup
participates in the signal. Harvest reports the wrapper fields in the existing
`engine_result_summary` entry for implementers and fixers; `skills_used: []` is
meaningful when status is `active`. Missing/malformed stream completion leaves
the normal invalid-result path authoritative. Telemetry remains fail-open with
respect to custody and relabel decisions.

Progress heartbeats reuse that same owned stdout pipe. As each stream event is
consumed the wrapper bumps the mtime of `heartbeat` beside `result.json` — the
mtime is the whole payload, and writes are throttled to at most one per 5s. The
bump happens before the line is parsed, since an unparseable line is still proof
the engine is emitting. Every write failure is swallowed: the heartbeat is
advisory diagnostics and must never influence whether a result reaches harvest.
The path is resolved absolute in `_heartbeat_path`, not at the launch sites:
the wrapper runs with `cwd` set to the worktree, so a relative path would
resolve a second time underneath it — and because heartbeat failures are
swallowed by design, that misresolution would be silent and would leave every
healthy engine reading as stalled once the fallback clock expired. `WT_ROOT` is
absolute on both branches of `_resolve_wt_root`, so the resolution is
what keeps absoluteness a property of the path rather than of that anchoring.

That mtime is read to distinguish a wedged engine from a working one, and the
result is `engine_stalled`; detection only, with no custody, label, kill, or
deadline effect. Two callers reach the same scan: every tick runs it at the
start of its body, and `--loop`'s wake watcher additionally polls it every 5s to
shorten the sleep. The tick-side call is what makes the signal exist at all in
the operating mode `SKILL.md` prescribes, where each iteration is its own
process and no watcher runs. Because those callers do not share memory, the
already-evaluated progress timestamp per dispatch identity is persisted to
`.tick-state/stall-episodes.json` — the same reason `quiescent-counter.txt`
exists. Each scan drops entries whose dispatch is gone, so the file is bounded
by in-flight work and carries no pruning rules of its own; a lost or malformed
file costs one repeated advisory report. A refuted liveness verdict is
checkpointed immediately; a live-stall timestamp is checkpointed only after
its operator report returns and before the scan advances. Report attempts use
at-least-once retry semantics across process death: an exit before the
post-report write commits can retry the attempt, but it cannot durably suppress
the stall. The macOS notification itself remains best effort. Under `--loop`
those two callers run
concurrently, so the read-modify-write is serialized by `stall-episodes.lock`;
a caller that cannot take it skips the pass rather than waiting, because the
holder is running the identical scan. The scan is wrapped at both call sites
(`_scan_stalled_engines_safely`, logging `stall_scan_failed` once per caller
per exception class), so a marker that makes `_classify_marker` raise cannot
end the tick or take the two older wake sources down with it. A stall is
silence past the threshold **and**
identity-verified liveness, so a reused PID or an already-overdue engine is not
one. That identity check *refutes* liveness rather than confirming it:
`_classify_marker` records `identity: "unknown"` when the process table cannot
be read and still counts the marker live, so a reported stall can rest on
liveness that was merely never disproved — which is why the log entry carries
the `identity` field. `pr-pipeline:status` renders the same verdict by
shelling out to `tick.py engine-progress` rather than re-deriving it in bash —
the dashboard cannot verify process identity and would otherwise raise alarms
the tick log never confirms.

The reviewer has a deliberately weaker observation contract because Codex exposes no
equivalent explicit `Skill` tool event. Reviewer dispatch adds `--json`; the engine
wrapper drains that stream and recognizes successful command-execution events that
reference normalized repository or installed-plugin `SKILL.md` paths. It retains no
raw event, prompt, model message, command, output, or absolute path. The strict bounded
summary (including a sanitized terminal token-usage snapshot in schema version 2)
is logged at HARVEST as `reviewer_skill_observation`. Its
`explicit_invocation_events_supported` field is `false`: `observed_skills` is
successful command-reference evidence, not proof of a read or semantic invocation,
and an empty list does not prove skills were unavailable. Missing or malformed
telemetry becomes `status=unavailable` and never changes result validation, custody,
relabeling, or cleanup.

The wrapper authenticates collection against branch-controlled stdout content;
after publication, telemetry has the same custody as the engine's established
`result.json` completion contract. It is observability, not an adversarial
attestation against an engine that deliberately mutates its own result after
exit. Fixer admission is limited to the trusted repository/author boundary,
and no workflow or security decision consumes these telemetry fields.

### Reviewer

Permission model: `-s read-only` (read-only sandbox) ([`tick._dispatch_one_reviewer`][tick]).

Claude's `permission_mode` setting does not apply here. Codex 0.155.1 exposes
`codex exec --approve-for-me` for automatic approval review, but that shortcut
selects `workspace-write` and permits reviewed escalation. It would change the
reviewer's read-only boundary, so dispatch does not use it. The deprecated
`--full-auto` is a different workspace-write compatibility flag. See the
[CLI investigation and limits](../../skills/tick/README.md#engine-permission-modes).

The reviewer runs via `codex exec`, which is non-interactive by design. A review
only reads the worktree and emits structured JSON (the tick posts the review comment
at harvest, not the reviewer itself). The read-only sandbox blocks filesystem
writes ([reviewer sandbox rationale][design-sandbox-scope]), so the full bypass
(`--dangerously-bypass-approvals-and-sandbox`) is unnecessary and is not used.

**Residual risk, accepted deliberately:** `-s read-only` sandboxes the filesystem
only — not the network and not out-of-process tools — and it still permits reads.
A prompt-injected PR could induce the reviewer to read a secret and place it in a
finding the tick then posts as a public PR comment. Full isolation would require
running the reviewer in an externally sandboxed environment. Dispatch provides
an author/repository trust gate and an optional policy-file gate:

1. **Trusted-candidate gate** (`pipeline.trusted_review_candidate`,
   [`pipeline.trusted_review_candidate`][pipeline]): the tick only dispatches a review for
   PRs from a trusted author against a trusted repo. This is the primary trust
   boundary — untrusted PRs never reach the reviewer engine.
2. **Policy-file gate** (`pipeline.touches_review_policy`,
   [`tick._dispatch_one_reviewer`][tick]): if the PR's diff modifies files that govern the
   reviewer's own behaviour (`CLAUDE.md`, `AGENTS.md`, review checklists — see
   section 4), the PR is escalated to human review when the operator sets
   `allow_touching = false` in their own config (the packaged floor sets
   `true` with operator-only issue pickup, per section 4). When active, it prevents a trusted author from
   (accidentally or otherwise) auto-approving changes to the review policy
   itself.

Additional flags:
- `--ephemeral` — no persistent state across runs
- `--ignore-user-config` — blocks `~/.codex/config.toml` (see section 6)
- `--ignore-rules` — blocks project-level `.rules` from the PR head worktree
  ([`tick._dispatch_one_reviewer`][tick])
- `--output-schema` — constrains output to the review JSON schema
  ([`tick._dispatch_one_reviewer`][tick])

Env isolation: scrub-dir pattern ([`tick._dispatch_one_reviewer`][tick]). `HOME` and
`CODEX_HOME` are redirected to a tick-owned `pr-{N}-scrub/` directory alongside
the worktree under `WT_ROOT`, not inside it. Only
`auth.json` is symlinked from the real `~/.codex/` directory. The inherited env
vars are `HOME` (scrub dir), `CODEX_HOME`, `PATH`, `GIT_TERMINAL_PROMPT`, and
`TMPDIR`. Provider API keys (`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`) are deliberately excluded — the reviewer authenticates via the
`CODEX_HOME` `auth.json` symlink, not provider keys. Forwarding them to this
prompt-injection surface would enable exfiltration through posted review comments.

The `--ignore-rules` flag closes the project `.rules` vector: without it,
`.rules` files from the PR head would be loaded by Codex, which is the same
vulnerability class as the CLAUDE.md/AGENTS.md policy gate (untrusted PR content
governing the reviewer's behaviour).

### Parent tick vs. engine sandbox

The tick invocation itself (the Claude Code session running `tick.py`) **must use
`dangerouslyDisableSandbox: true`** because its subprocesses are spawned via
`subprocess.Popen`, not the Bash tool — they bypass the Bash tool's command-pattern
allowlist but inherit the OS-level sandbox profile. Under the default macOS sandbox,
`ps` and `os.killpg` are denied, which breaks process identity verification and
timeout enforcement (see [SKILL.md § Trust domain][skill-trust] for the full
breakdown). This is a separate concern from the reviewer subprocess's `-s read-only`
sandbox, which is a Codex-level restriction on what the reviewer model can do inside
its own session.

### Compensating controls summary

| Control | Implementer/Fixer | Reviewer |
|---|---|---|
| Pre-approved tools | Narrow `--allowedTools`; auto may approve more, deny rules still apply | N/A (read-only sandbox) |
| Sandbox mode | None imposed by tick (operator settings apply) | Read-only (`-s read-only`) |
| Env scrub | None (trusted local engine inherits the parent env) | scrub-dir (`HOME`/`CODEX_HOME` redirect) |
| User config blocked | No | Yes (`--ignore-user-config`) |
| Project rules blocked | No | Yes (`--ignore-rules`) |
| Policy gate | N/A (implementers don't review) | Enabled by `allow_touching = false` (`touches_review_policy`) |
| Worktree isolation | Yes | Yes |
| Process group isolation | Yes (`start_new_session`) | Yes (`os.setpgrp()`) |
| Trusted-candidate gate | Implementer: operator issue author (`trusted_issue_candidate`) | Yes (`trusted_review_candidate`) |
| Residual read+exfil risk | Accepted within the operator-equivalent local trust boundary | Accepted; trusted-candidate gate is the primary control, with an optional policy gate (see section 4) |

## 6. Reviewer `--ignore-user-config` rationale

The `--ignore-user-config` flag ([`tick._dispatch_one_reviewer`][tick]) prevents Codex from
loading `~/.codex/config.toml`. Without it, user configuration could:

- **Spawn MCP servers or plugins** that open network connections, listen on ports,
  or launch out-of-sandbox processes. In an unattended run, these would hang
  indefinitely (no operator to dismiss prompts or kill stalled servers).
- **Load hooks** that expect interactive input or access resources unavailable in
  the scrubbed environment.
Because `--ignore-user-config` discards the entire `config.toml`, the reviewer
**must** pass model and effort explicitly on the command line; without them
Codex falls back to its built-in defaults (historically `codex-auto-review` /
`medium`), which may be weaker than intended.

Model and effort are the deliberate exception to the blanket discard, and they
are re-admitted by a different route: the tick reads those two keys itself and
forwards them as flags ([`tick.resolve_reviewer_model_settings`][tick]). That keeps the review
tracking the operator's chosen model — the usual reason to touch that file —
without letting anything else in the file cross into the engine. Nothing above
this line is relaxed: MCP servers, plugins, hooks, and sandbox/approval keys
stay discarded, and the two values that do cross are shape-checked first.

---

## 7. Operator configuration resolution

Before configuration is selected, `pipeline.resolve_target` invokes the shared
`skills/tick/resolve-target.sh` helper. It resolves one GitHub repository and
matching local remote without Python inside the helper or any GitHub request.
The common Git directory anchors resolution to the main checkout, so linked
worktree settings cannot make the dashboard and controller disagree.
`PIPELINE_REPO` selects a matching remote and cannot override `[scan] repos`.
Missing or ambiguous matches, unsafe names, and conflicting push destinations
fail startup. `status.sh` invokes this same helper before resolving Python and
reads no TOML. `[target] repo` is no longer read.

The controller binds this identity for the tick: `gh` adds
`--repo github.com/owner/name` (including the host), all fetches
use the resolved remote, and implementer/fixer prompts and push grants carry the
same remote. Qualified issue reads may use `gh_json(..., repo="owner/other")`;
the explicit read repository is separate from the target used for mutations.
Repository comparisons in configuration, scan scope, and reviewer admission
ignore case. `--loop` exits on startup rejection before observing remote cadence.

`pipeline.validate_target_context` invokes the helper's `--check-remote` mode
without changing the supplied working directory. `_launch_claude_engine` runs
it with the engine's cwd and environment before opening output files or starting
either an implementer or fixer. A conflicting effective fetch or push URL
returns a launch failure, leaves the item on its pickup label, and reports the
target error on stderr. `_harvest_one_reviewer` performs the same local check
before its worktree fetch; a rejection logs `target_context_invalid` and retains
the marker and result for retry. This closes routing differences caused by
conditional includes and worktree configuration while preserving startup's
shared main-checkout identity. Configuration must stay stable during an engine
run; the checks do not pin future Git transport.

[`pipeline.resolve_config`][pipeline] owns parsing and layering; Python callers
consume its `ResolvedConfig` rather than raw tables. This is one resolver, not
necessarily one resolution per tick: keeping one validated snapshot through all
consumers remains [issue #26](https://github.com/kwuBuild/pr-pipeline/issues/26).

Exactly two files are read, both through `_read_toml`:

1. the install's `skills/tick/config.toml` — the read-only shipped floor,
   which rejects operator `[operator]`, `[repos.*]`, and `[scan]` tables
2. `~/.claude/pr-pipeline/config.toml` — `$CLAUDE_CONFIG_DIR` relocates it,
   `$PR_PIPELINE_CONFIG` names it outright

Neither path is cwd-dependent, which is the property that matters at runtime: an
engine's working directory is a checkout of the pull request's own head, so a
config read relative to the cwd would be a PR supplying the policy it is judged
under. A test asserts the read set and fails loudly if a third path appears.

That property is enforced, not merely intended: `_operator_config_location`
refuses a relative `$PR_PIPELINE_CONFIG` or `$CLAUDE_CONFIG_DIR` with a startup
error and reads no operator file at all. `main` chdirs to the repository root
*after* this module is imported, so a relative override would otherwise have one
process resolve its identity from one file and its policy from another.

Layers, later overriding earlier: floor → `[defaults]` → `[repos."owner/name"]`
→ environment (`PIPELINE_TRUSTED_AUTHOR`) → CLI overrides. Presence decides, not
truthiness, so `verify_tools = []` for one repository beats a non-empty default —
lists replace wholesale rather than merging, because an operator who cannot
*narrow* a grant per target cannot use a permission list properly.

The login for issue pickup, reviewer admission, and issue cadence is the one
each tick resolves and validates. Successful startup binds
`TRUSTED_REVIEW_AUTHOR` for dispatch and the following loop observation, and
also passes the login explicitly to `dispatch_reviewers`. The next tick adopts
a later config edit. No issue author gate rereads config during a tick.

`trusted_issue_candidate` requires an exact operator login and refuses missing
or mistyped authors. Global dispatch and cadence read only that author's
issues using bounded `--author` queries. Targeted issue reads include `author`
and apply the same predicate. Known other authors are non-actionable; unreadable
authors keep cadence warm while remaining refused at dispatch. Neither case
grants custody or changes an issue label. A separate bounded number-only query
reports excluded issues in the dispatch log; it does not feed cadence.

`[operator] login` sits outside that layering deliberately: it is an identity,
not a per-target setting, and a per-repository override would mean reviewing PRs
as somebody else on one repo. Beneath it is the packaged file's deprecated
`[target] trusted_author`, read only so an install configured before the move to
the operator's home survives the change.

Malformation splits two ways:

- **Has a safe direction** (`allow_touching`) — resolves to the conservative
  reading and appends to `ResolvedConfig.warnings`. The tick logs
  `startup_config_warning` and runs.
- **Has none** (`base_ref`, `permission_mode`, `engine_settings`,
  `[operator] login`, `verify_tools`, `checklist_paths`, `[scan] repos`) —
  appends to
  `ResolvedConfig.errors`. The tick logs `startup_config_invalid` and exits 3,
  naming the key. So does a container declared as something other than a table:
  `[[defaults]]` read as absent would replace every setting under it with the
  shipped floor, and `[[scan]]` read as absent would turn a declared scope into
  an unrestricted one.

`checklist_paths` sits in the second group for the opposite reason to the rest.
These are paths to *block*, so `()` is the permissive reading, not the
conservative one; on malformation the resolver keeps every usable string any
layer declared (`_every_usable_entry`) so that a bad per-repository override
cannot erase a valid `[defaults]` list, and still errors so the tick stops.

`ResolvedConfig.repo_configured` reports whether the target matched a
`[repos."owner/name"]` table, empty or not, case-insensitively. After the
configuration and login checks, a tick whose target has no such table logs
`startup_repo_unconfigured` and exits `UNCONFIGURED_TARGET_EXIT_CODE` (6)
before the label check. Under `--loop` the reason says the repository was
skipped, and the loop returns without observing cadence. Neither path reads
input.

The setup skill (`skills/setup/lib/setup_flow.py`) is the only writer of the
operator file. It merges one repository's section into the parsed document and
serialises the result. It validates the result by resolving a staged copy
through `resolve_config` with `$PR_PIPELINE_CONFIG` pointed at it, so it
cannot leave a file the tick refuses. Only then does it replace the file.

A malformed `verify_tools` is therefore not an empty one. Read as empty it would
compose with the dispatch-anyway rule into an engine that verifies nothing and a
PR promoted to `ai:ready-for-human` regardless — a hole neither rule has alone.
"Malformed" covers the entries as well as the container: a value that cannot be
a `Bash(...)` permission body is reported here rather than filtered at dispatch,
because filtering turns one typo into a silently narrower grant and an
all-invalid list into exactly the empty state this paragraph exists to prevent.
`[scan] repos` splits the same way: absent or `[]` is unrestricted because both
are things the operator wrote, while a malformed value resolves to the empty
*restrictive* set, so even a caller that ignores errors cannot read a typo as
"every repository is eligible".

The config resolver never raises. `tick.py` resolves at import to seed
`TRUSTED_REVIEW_AUTHOR`, and an exception there would turn a
config typo into a traceback from whichever CLI helper imported the module first.
The startup gate is the single enforcement point.

For issue dispatch, a non-blank resolved `base_ref` wins. A blank value queries
`gh repo view github.com/<canonical-target> --json defaultBranchRef`; neither
the checkout's current branch nor a cached remote HEAD supplies the default.
Failed or unusable discovery takes the existing setup-escalation path with an
error naming the target, before any fetch, worktree creation, or engine launch.
Dispatch fetches the selected branch from the resolved remote and pins its name
and SHA in `dispatch.json`. PR creation uses the pinned name even if the
operator's config or the repository's default changes while the engine runs.
Reconciliation still rejects invalid markers before harvest. As a further guard,
if the base becomes missing or blank between reconciliation and PR creation, the
harvest helper enters its existing bounded failure path instead of guessing.
This guard does not impede handoff to an already-created PR. Review and fixer
dispatch continue to use the actual PR base, including stacked PRs.

---

## 8. Label vocabulary and setup

`pipeline.WORKFLOW_LABELS` derives from the `LABEL_*` constants. The installed
`skills/tick/lib/labels.py provision` command resolves the canonical local Git
target, reads all repository label pages with an explicit GitHub host, and
creates only missing vocabulary labels. Existing metadata is preserved; no
delete, rename, or force-update operation exists. A failed create is accepted
only when a fresh read proves that another setup has already created the label.
The final read reports all unknown `ai:` labels and their no-dispatch consequence.
The interactive setup flow is outside this primitive; the operator runs it in
each repository being added (see the tick README for interpreter resolution).

`_run_single_tick_locked` calls `labels.verify` after target, configuration,
identity, and binary validation and before `_run_tick_body`. This precedes
HARVEST as well as DISPATCH, since both can transition labels. Missing labels
or unreadable vocabulary emit `startup_labels_invalid`, failed tick state, and
exit 3; no workflow transition occurs. The error names missing labels and the
installed provisioning command. Each loop tick repeats the check; unknown
labels are tolerated. This is a startup check, not a lock against an operator
deleting a label during a tick.

`status.sh` reads all open items from the paginated REST issues endpoint, which
includes PRs, and uses `labels.py vocabulary` to obtain the installed known set.
Its parked rows contain items with at least one unknown `ai:` label and no
known workflow label. The raw label sections remain usable without Python;
classification or GitHub read failures render as unavailable, never as no
parked items. Status remains read-only.

## Source references

Cite the owning file and searchable symbol, not a source line number. These
links deliberately survive unrelated insertions; verify the symbol when its
behavior changes. The documentation contract test checks Python symbol targets.

[tick]: ../../skills/tick/lib/tick.py
[pipeline]: ../../skills/tick/lib/pipeline.py
[engine_wrapper]: ../../skills/tick/engine_wrapper.py
[skill-trust]: ../../skills/tick/SKILL.md#trust-domain-and-sandbox-posture
[design-reviewer-ctx]: ../architecture.md#engine-dispatch-details
[design-sandbox-scope]: ../architecture.md#engine-dispatch-details
[reviewer-prompt]: ../../skills/tick/lib/prompts/reviewer.txt
