# PR Pipeline Internals

Reference document for the `pr-pipeline-tick` skill's internal mechanisms.
This is **not** operational documentation — see `SKILL.md` and `README.md` in
`skills/pr-pipeline-tick/` for how to run the tick.

---

## 1. Engine launch summary

| Property | Implementer | Fixer | Reviewer |
|---|---|---|---|
| Binary | `claude -p` | `claude -p` | `codex exec` (via `engine_wrapper.py`) |
| Permission mode | `--permission-mode dontAsk` | `--permission-mode dontAsk` | `-s read-only` (sandbox) |
| Tool restriction | `--allowedTools` ([tick.py:591–663][impl-tools]) | `--allowedTools` ([tick.py:591–663][impl-tools]) | None (read-only sandbox enforces) |
| Model (default) | whatever `claude` resolves (no `--model` flag) | whatever `claude` resolves / `deepseek-v4-pro[1m]` (claude-ds) | operator's `$CODEX_HOME/config.toml` (else `~/.codex/config.toml`), else `gpt-5.5` |
| Model override | `--model` flag from tick CLI | `--model` flag from tick CLI | `--review-model` flag from tick CLI |
| Reasoning effort | Not set (claude default) | Not set (claude default) | operator's codex config, else `high`; `--review-effort` overrides |
| Env isolation | Full parent env via `_engine_env()` ([tick.py:311–322][engine-env]) | Full parent env via `_engine_env()` ([tick.py:311–322][engine-env]) | scrub-dir with `HOME`/`CODEX_HOME` redirect ([tick.py:6047–6065][scrub]) |
| Prompt source | `lib/prompts/implementer.txt` | `lib/prompts/fixer.txt` | `lib/prompts/reviewer.txt` |
| Output capture | stdout → `result.json` | stdout → `result.json` | final output → atomic `review.json`; JSONL stdout → bounded sanitizer |
| Progress heartbeat | `heartbeat` mtime, bumped per output event (≤1 write/5s) | `heartbeat` mtime, bumped per output event (≤1 write/5s) | none — the wrapper discards engine output; stall cost bounded by the 20 min timeout |
| Timeout | 60 min (`IMPLEMENTER_TIMEOUT`, [tick.py:186][impl-timeout]) | 60 min (`FIXER_TIMEOUT`, [tick.py:187][fix-timeout]) | 20 min (`REVIEW_TIMEOUT`, [tick.py:129][rev-timeout]) |
| Process group | `start_new_session=True` | `start_new_session=True` | `os.setpgrp()` in wrapper ([engine_wrapper.py:69][wrapper-setpgrp]) |

## 2. Model and reasoning effort

### Implementer

Default model: whatever the `claude` CLI resolves. With no override,
`_launch_claude_engine` omits `--model` entirely ([tick.py:749][launch-model]).
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
([tick.py:325–397][resolve]) sets `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, and `CLAUDE_CODE_SUBAGENT_MODEL` to the resolved
model, routing Claude Code's internal model selection through the DeepSeek API
gateway.

The tick CLI accepts `--model <model>` ([tick.py:9185][cli-model]) which overrides
the per-engine default for both the `--model` flag and `ANTHROPIC_MODEL`.

Reasoning effort is not explicitly set. Claude Code uses its own default.
For claude-ds, `CLAUDE_CODE_EFFORT_LEVEL=max` is set ([tick.py:395][ds-effort]).

### Fixer

Same as the implementer — the operator's own model (claude) or
`deepseek-v4-pro[1m]` (claude-ds). Model resolution at [tick.py:7063–7068][fix-model].
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
at a per-PR scrub dir, *and* `--ignore-user-config` ([tick.py:6130][ignore-config]).
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

The hooks in this repo (e.g. `pre-pr-review-implementation.sh`,
`pre-pr-review-documentation.sh` in `.claude/hooks/`) are Claude Code `PreToolUse`
hooks, configured in the operator's local `.claude/settings.local.json` (not
tracked — it is a per-machine file). They fire only when the interactive Claude Code
harness is about to execute a `Bash(...)` tool call, matching patterns like
`Bash(gh pr create*)`, `Bash(gh pr edit*)`, and `Bash(git push --force-with-lease*)`.

**None of these hooks fire for any engine.** The mechanism:

- **Implementer and fixer** run as `claude -p` subprocesses spawned via
  `subprocess.Popen` ([tick.py:757–769][popen-impl]). They are headless processes
  with no interactive harness. PreToolUse hooks are a feature of the Claude Code
  interactive harness that interposes on tool calls *within the harness* — a
  subprocess does not load or evaluate them.

- **Reviewer** runs as `codex exec` ([tick.py:6118][codex-exec]), also spawned via
  `subprocess.Popen` ([tick.py:6148–6156][popen-rev]). Codex CLI is a separate
  binary with its own execution model; it does not load Claude Code's
  `settings.local.json` hooks.

- **`gh pr create` in the tick** runs via `subprocess.run(["gh", ...])` in the
  Python tick process ([tick.py:786–809][gh-helper]), not through Claude Code's
  `Bash(...)` tool. PreToolUse hooks only intercept tool calls within the harness,
  not arbitrary subprocesses.

Consequence: engine-generated PRs bypass the pre-PR review gates
(`pre-pr-review-implementation.sh`, `pre-pr-review-documentation.sh`) entirely.
The pre-PR hooks are a quality gate for the interactive workflow only.

## 4. CLAUDE.md / AGENTS.md inheritance

### Governing-file contracts per engine

Each engine runs with `cwd` set to an isolated git worktree:
- Implementer: worktree at `origin/main` tip
  ([tick.py:6449][impl-wt])
- Fixer: worktree at the PR's head branch
  ([tick.py:6855–6856][fix-wt])
- Reviewer: worktree at the PR's head SHA (detached)
  ([tick.py:5784–5785][rev-wt])

**Implementer and fixer** (Claude Code, `claude -p`): Claude Code auto-loads the
nearest `CLAUDE.md` up the directory tree from `cwd`. This is guaranteed context.
`AGENTS.md` is not auto-loaded by Claude Code.

The fixer additionally gets a **prompt-bound skill doc**: its prompt names
`.claude/skills/review-response-hygiene/SKILL.md` (from `tick.FIXER_SKILLS`,
substituted at render) and requires it to be read before editing. This is a doc
path rather than a `Skill` invocation because the allowlist in §5 carries `Read`
and no `Skill` tool. Dispatch resolves the path against the fixer's *own*
worktree — the PR head, not the operator checkout — before launching, so a
branch that predates the skill logs `skill_unresolved` and escalates instead of
running an engine that would silently read nothing.

The fixer also gets two documents it has no way to fetch — it carries no `gh`
and no web access — read by dispatch and substituted in: the **PR body** and the
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
still runs no `gh`: after its final push it writes `.pr-body-revision.md` in its
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

**Reviewer** (Codex CLI, `codex exec`): Codex auto-loads the nearest `AGENTS.md`
up the directory tree as its project doc. `AGENTS.md` routes to `CLAUDE.md`
(line 8: "Read `CLAUDE.md`"), but `CLAUDE.md` is not auto-loaded — it is
consulted at the reviewer's discretion, along with other standards docs
(`.claude/hooks/sworn-review-checklist.md`, design docs). Guaranteed context
is `AGENTS.md` only ([design:388–399][design-reviewer-ctx]). The reviewer
prompt itself is minimal (`Review PR #...`); it does not pin a reading list
or explicitly apply any checklist ([reviewer.txt:1][reviewer-prompt]).

### Policy gate: `touches_review_policy`

The reviewer dispatch checks whether the PR's diff modifies any file that governs
the reviewer's own behaviour ([tick.py:9007][policy-call]). If so, the PR is
escalated to human review instead of being auto-reviewed.

The gate is operator-configurable: `[policy] allow_touching = true` in
`skills/pr-pipeline-tick/config.toml` lets policy-editing PRs flow to
auto-review anyway (the operator accepts that the review runs under instructions
the PR itself supplies). Missing config, malformed TOML, or `false` keep the
escalation — the gate fails closed. The committed config ships `false`, because
an install inherits it and the opt-out is a trust decision only the operator can
make; see the skill README's "Admission gate: reviewer policy files" section.

The gate function `touches_review_policy` ([pipeline.py:1572–1609][policy-fn])
matches changed paths against:

**Basenames** (matched at any directory level, because both Claude Code and Codex
load governing files from the nearest match up the tree) — `_POLICY_BASENAMES`
([pipeline.py:137][basenames]):
- `CLAUDE.md`
- `AGENTS.md`

**Exact paths** — `_POLICY_PATHS` ([pipeline.py:138–141][paths]), plus the suffix set below:
- `.claude/hooks/sworn-review-checklist.md`
- `.claude/hooks/sworn-review-checklist-frontend.md`

**Path suffixes** (matched per path segment, so the rule is one rule at every
install depth — `.claude/skills/` in a host repo, `skills/` in the pipeline's
own repository, or a plugin root) — `_POLICY_PATH_SUFFIXES`
([pipeline.py:157–164][path-suffixes]):
- `skills/pr-pipeline-tick/config.toml` — the file that toggles the
  gate itself; a PR flipping `allow_touching` is escalated to a human rather
  than auto-reviewed (a merged flip would otherwise disable the gate for every
  future policy-editing PR)
- `skills/pr-pipeline-tick/lib/pipeline.py` and `lib/tick.py` — the
  gate's own implementation; a PR that rewrites `_policy_touching_allowed` to
  return `True` unconditionally, or removes the gate call from the reviewer
  dispatch, is the same silent-disable vector, so pipeline-code PRs escalate to
  a human while the gate is enabled

Rationale: a PR that edits these files supplies instructions the reviewer itself
would follow — "untrusted input controls the validator." The gate prevents the
reviewer from auto-approving a PR that modifies the very rules used to evaluate it
([pipeline.py:131–136][policy-comment]).

## 5. Security posture per engine

### Implementer and fixer

Permission model: `--permission-mode dontAsk` + `--allowedTools`
([tick.py:591–663][impl-tools], [tick.py:741–750][launch-flags]).

The `--allowedTools` allowlist is a named security boundary
([tick.py:591–663][impl-tools]):
```
Read Edit Write
Bash(git add *) Bash(git commit *) Bash(git status) Bash(git diff *)
Bash(git log *) Bash(git push origin HEAD)
Bash(uv run pytest *) Bash(uv run ruff *) Bash(uv run pyright)
Bash(npm --prefix frontend *)
```

This restricts the git surface to commit + fast-forward push of the engine's own
branch. No force-push, remote-branch deletion, or arbitrary refspec push is
possible. The design contract is `--permission-mode dontAsk` + this explicit list —
never `--dangerously-skip-permissions`.

Environment contract: `_engine_env()` ([tick.py:311–322][engine-env]) preserves
the operator's complete parent environment. Implementers and fixers are trusted
local engines running with the same ambient credentials and capabilities as the
terminal that launched the tick. This keeps dispatch terminal-agnostic: cmux
variables are present when launched from cmux and absent elsewhere. The reviewer
retains a separate scrubbed environment because it has a different trust posture.

Publication surface: a fixer **escalation** echoes a bounded excerpt of the
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

Publication ordering: the gate applies to a terminal transition that *reports an
engine result, or fires before one was launched* — every such transition posts
before the label moves. That is stated as a bound rather than a list because the
gated side grows with each new terminal outcome, while the released side is
closed by an argument: the gate holds wherever the retry can be bounded. It is
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

What stays silent is now one shape: an engine was launched and the tick could not
read a result from it. **It finished and nothing is published** — now only
`_harvest_failed_reviewer`'s wrapper-confirmed reviewer failure — is a hole in
the gate; it archives under `.harvest-evidence/`, so the explanation is
machine-local rather than absent. Reconciliation's hold escalation over a
completed marker was the other member until #618 moved it into the gated set; the
reviewer path has the same shape, is tracked by neither issue, and belongs with
it. **It may still be running** — the reconcile escalations (hung engine,
unverified live custody) and each stage's post-launch guard, which fires when the
in-flight relabel failed *and* the engine could not be killed. Those relabel
through `_relabel_with_context`, whose only terminal side effect is
`_notify_terminal_label`: stderr, a tick-log entry, and a local desktop
notification. They are not a gap of the same kind: the label is a
duplicate-dispatch guard over custody the tick could not prove it holds, the
marker is retained, and the engine's own harvest still owns the explanation.

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
decided. The one accepted exception is the pre-#618 key set (`dispatch_id`,
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
absolute on both branches of `_resolve_wt_root` (#604), so the resolution is
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
the `identity` field. `pr-pipeline-status` renders the same verdict by
shelling out to `tick.py engine-progress` rather than re-deriving it in bash —
the dashboard cannot verify process identity and would otherwise raise alarms
the tick log never confirms.

The reviewer has a deliberately weaker observation contract because Codex exposes no
equivalent explicit `Skill` tool event. Reviewer dispatch adds `--json`; the engine
wrapper drains that stream and recognizes successful command-execution events that
reference normalized repository or installed-plugin `SKILL.md` paths. It retains no
raw event, prompt, model message, command, output, or absolute path. The strict bounded
summary is logged at HARVEST as `reviewer_skill_observation`. Its
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

Permission model: `-s read-only` (read-only sandbox) ([tick.py:6128][sandbox]).

The reviewer runs via `codex exec`, which is non-interactive by design. A review
only reads the worktree and emits structured JSON (the tick posts the review comment
at harvest, not the reviewer itself). The read-only sandbox blocks filesystem
writes ([design:447–448][design-sandbox-scope]), so the full bypass
(`--dangerously-bypass-approvals-and-sandbox`) is unnecessary and is not used.

**Residual risk, accepted deliberately:** `-s read-only` sandboxes the filesystem
only — not the network and not out-of-process tools — and it still permits reads.
A prompt-injected PR could induce the reviewer to read a secret and place it in a
finding the tick then posts as a public PR comment. Full isolation would require
running the reviewer in an externally sandboxed environment. Until then, two
dispatch-time gates are the primary controls:

1. **Trusted-candidate gate** (`pipeline.trusted_review_candidate`,
   [tick.py:5719–5730][trusted-candidate]): the tick only dispatches a review for
   PRs from a trusted author against a trusted repo. This is the primary trust
   boundary — untrusted PRs never reach the reviewer engine.
2. **Policy-file gate** (`pipeline.touches_review_policy`,
   [tick.py:9007][policy-call]): if the PR's diff modifies files that govern the
   reviewer's own behaviour (`CLAUDE.md`, `AGENTS.md`, review checklists — see
   section 4), the PR is escalated to human review unless the operator opts out
   via `config.toml` `[policy] allow_touching = true` (the shipped config sets
   `false`, per section 4). When active, it prevents a trusted author from
   (accidentally or otherwise) auto-approving changes to the review policy
   itself.

Additional flags:
- `--ephemeral` — no persistent state across runs
- `--ignore-user-config` — blocks `~/.codex/config.toml` (see section 6)
- `--ignore-rules` — blocks project-level `.rules` from the PR head worktree
  ([tick.py:6133][ignore-rules])
- `--output-schema` — constrains output to the review JSON schema
  ([tick.py:6120–6122][schema])

Env isolation: scrub-dir pattern ([tick.py:6047–6065][scrub]). `HOME` and
`CODEX_HOME` are redirected to a tick-owned `pr-{N}-scrub/` directory alongside
the worktree under `WT_ROOT`, not inside it (#528). Only
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
| Tool allowlist | Yes (`--allowedTools`) | N/A (read-only sandbox) |
| Sandbox mode | None (relies on allowlist) | Read-only (`-s read-only`) |
| Env scrub | None (trusted local engine inherits the parent env) | scrub-dir (`HOME`/`CODEX_HOME` redirect) |
| User config blocked | No | Yes (`--ignore-user-config`) |
| Project rules blocked | No | Yes (`--ignore-rules`) |
| Policy gate | N/A (implementers don't review) | Yes (`touches_review_policy`) |
| Worktree isolation | Yes | Yes |
| Process group isolation | Yes (`start_new_session`) | Yes (`os.setpgrp()`) |
| Trusted-candidate gate | N/A | Yes (`trusted_review_candidate`) |
| Residual read+exfil risk | Accepted within the operator-equivalent local trust boundary | Accepted; trusted-candidate + policy gates are primary controls (policy gate currently opted out via `config.toml`, see section 4) |

## 6. Reviewer `--ignore-user-config` rationale

The `--ignore-user-config` flag ([tick.py:6130][ignore-config]) prevents Codex from
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
forwards them as flags ([tick.py:6140][rev-model]). That keeps the review
tracking the operator's chosen model — the usual reason to touch that file —
without letting anything else in the file cross into the engine. Nothing above
this line is relaxed: MCP servers, plugins, hooks, and sandbox/approval keys
stay discarded, and the two values that do cross are shape-checked first.

---

## Source references

<!-- Anchors for inline references. Line numbers are approximate and should be
     verified against the current source when the document is updated. -->

[impl-tools]: ../../skills/pr-pipeline-tick/lib/tick.py#L591-L663
[launch-model]: ../../skills/pr-pipeline-tick/lib/tick.py#L749
[launch-flags]: ../../skills/pr-pipeline-tick/lib/tick.py#L741-L750
[engine-env]: ../../skills/pr-pipeline-tick/lib/tick.py#L311-L322
[resolve]: ../../skills/pr-pipeline-tick/lib/tick.py#L325-L397
[ds-effort]: ../../skills/pr-pipeline-tick/lib/tick.py#L395
[popen-impl]: ../../skills/pr-pipeline-tick/lib/tick.py#L757-L769
[rev-model]: ../../skills/pr-pipeline-tick/lib/tick.py#L6140
[rev-effort]: ../../skills/pr-pipeline-tick/lib/tick.py#L6141
[ignore-config]: ../../skills/pr-pipeline-tick/lib/tick.py#L6130
[ignore-rules]: ../../skills/pr-pipeline-tick/lib/tick.py#L6133
[schema]: ../../skills/pr-pipeline-tick/lib/tick.py#L6120-L6122
[sandbox]: ../../skills/pr-pipeline-tick/lib/tick.py#L6128
[codex-exec]: ../../skills/pr-pipeline-tick/lib/tick.py#L6118
[popen-rev]: ../../skills/pr-pipeline-tick/lib/tick.py#L6148-L6156
[scrub]: ../../skills/pr-pipeline-tick/lib/tick.py#L6047-L6065
[policy-call]: ../../skills/pr-pipeline-tick/lib/tick.py#L9007
[impl-wt]: ../../skills/pr-pipeline-tick/lib/tick.py#L6449
[fix-wt]: ../../skills/pr-pipeline-tick/lib/tick.py#L6855-L6856
[rev-wt]: ../../skills/pr-pipeline-tick/lib/tick.py#L5784-L5785
[impl-timeout]: ../../skills/pr-pipeline-tick/lib/tick.py#L186
[fix-timeout]: ../../skills/pr-pipeline-tick/lib/tick.py#L187
[fix-model]: ../../skills/pr-pipeline-tick/lib/tick.py#L7063-L7068
[rev-timeout]: ../../skills/pr-pipeline-tick/lib/tick.py#L129
[cli-model]: ../../skills/pr-pipeline-tick/lib/tick.py#L9185
[gh-helper]: ../../skills/pr-pipeline-tick/lib/tick.py#L786-L809
[hook-impl]: ../../.claude/hooks/pre-pr-review-implementation.sh
[hook-docs]: ../../.claude/hooks/pre-pr-review-documentation.sh
[skill-trust]: ../../skills/pr-pipeline-tick/SKILL.md#trust-domain-and-sandbox-posture
[policy-fn]: ../../skills/pr-pipeline-tick/lib/pipeline.py#L1572-L1609
[basenames]: ../../skills/pr-pipeline-tick/lib/pipeline.py#L137
[paths]: ../../skills/pr-pipeline-tick/lib/pipeline.py#L138-L141
[path-suffixes]: ../../skills/pr-pipeline-tick/lib/pipeline.py#L157-L164
[policy-comment]: ../../skills/pr-pipeline-tick/lib/pipeline.py#L131-L136
[trusted-candidate]: ../../skills/pr-pipeline-tick/lib/tick.py#L5719-L5730
[design-reviewer-ctx]: ../../docs/planning/designs/pr-pipeline-tick.md#L388-L399
[design-sandbox-scope]: ../../docs/planning/designs/pr-pipeline-tick.md#L447-L448
[reviewer-prompt]: ../../skills/pr-pipeline-tick/lib/prompts/reviewer.txt#L1
[wrapper-setpgrp]: ../../skills/pr-pipeline-tick/engine_wrapper.py#L69
