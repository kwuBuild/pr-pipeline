# PR Pipeline — End-to-End Usage & Monitoring Guide

Status: COMPLETE (HARVEST + DISPATCH fully implemented — 3 stages each)
Created: 2026-06-28
Updated: 2026-07-01 (dispatch phases implemented, operator guide updated)

## Quick-start checklist

**Trust domain:** The tick invocation must use `dangerouslyDisableSandbox: true`.
The Bash tool's command-pattern allowlist and the OS-level sandbox (macOS
`sandbox-exec`) are separate concerns — subprocesses bypass the allowlist
(spawned from Python, not the Bash tool) but inherit the OS-level sandbox.
Under the default sandbox: `ps` is denied so process identity is permanently
unverifiable (Invariant 4 blocks all reaping); cross-boundary kill signals
are denied so timeout enforcement cannot terminate anything; and dispatched
engines inherit the sandbox, losing network access to non-allowlisted LLM
endpoints and `~/.codex` writes, so they stall silently after the label flip.
Scope: the tick process and its children, not the entire session.

The orchestrator is a single Python process (`lib/tick.py`); the skill's
`SKILL.md` is a lean operator SOP. See `docs/architecture.md` for why the
control plane is shaped the way it is.

**Installing.** The repository is its own plugin marketplace, so there is no
separate registry to add:

```
/plugin marketplace add kwuBuild/pr-pipeline
/plugin install pr-pipeline
```

The manifest publishes the skills under `skills/<name>/SKILL.md` as
`/pr-pipeline:<name>` commands. `claude plugin details pr-pipeline` lists the
installed set. Run `/pr-pipeline:setup` once after installing, and again for
each new target repository; then use `/pr-pipeline:tick` to operate the
pipeline and `/pr-pipeline:status` to inspect it.

Setup confirms the operator login and, for each repository, resolves the
default branch, provisions the label vocabulary, proposes verification grants
for the operator to confirm, and runs the [route probe](#setup-route-probe). It
then writes the repository's section of the operator config, offers
[deny rules](#engine-permission-modes), and installs the launcher below. It is
the only writer of the operator config and never writes into a target
repository.

For implementation and revision work, use `definition-of-done` and
`review-response-hygiene`. Developers implementing or reviewing changes to
pr-pipeline itself should use
[automation-control-plane-review](../automation-control-plane-review/SKILL.md),
[durability-boundary-review](../durability-boundary-review/SKILL.md), and
[loop-cadence-contract-review](../loop-cadence-contract-review/SKILL.md) when their
boundaries apply. The isolated reviewer also receives these installed skills as
optional native guidance. Its prompt requires selection by scope; the catalog
does not make these reviews mandatory for other repositories or change engine
permissions. See [native stage bindings](#prompt-bound-skills-skill_unresolved).

**Where the pipeline lives.** Pipeline *code* is resolved from the install, not
from the repository being operated on. Pipeline *state* (`.claude/worktrees/`)
stays relative to the target repo, which is why every command below runs with
the target repo as the working directory.

**Standalone shell commands.** Install the launcher once at
`~/.claude/pr-pipeline/bin/pr-pipeline`. `/pr-pipeline:setup` runs
`skills/tick/install-launcher.sh` for you. From a development checkout, run:

```bash
bash skills/tick/install-launcher.sh
```

For a plugin install, have the plugin session run `install-launcher.sh` from
its resolved `skills/tick/` directory. This copies the launcher outside the
version cache; it does not write pipeline configuration or target repositories.
Run the commands below from the target checkout after that installation step.
Adding `~/.claude/pr-pipeline/bin` to PATH is optional.

The launcher queries `claude plugin list --json` on every invocation and requires
one enabled `pr-pipeline@pr-pipeline` record with an existing absolute install
path. Missing, disabled, malformed, or ambiguous installs fail with setup
guidance. Install `claude` and `jq` on PATH. After a plugin upgrade the next
invocation resolves the new path; an already running tick keeps its loaded code.

When an upgrade adds a subcommand, such as `events`, rerun
`skills/tick/install-launcher.sh` from the current install to refresh the copied
launcher's command list. This is the Launcher step of setup; it does not require
rerunning engine probes or rewriting configuration.

`CLAUDE_PLUGIN_ROOT` is substituted only in plugin skill content, not in this
file or a separate terminal. No install-root export is needed for these shell
commands. Development from a checkout remains supported through an explicit
absolute override, which bypasses plugin discovery:

```bash
export PR_PIPELINE_HOME=/absolute/path/to/pr-pipeline
```

An empty or relative override is an error. Unset it to return to the plugin
install. Paths with spaces are supported; quote the value when exporting it.
The launcher forwards arguments, cwd, signals, and exit status to the existing
entry points. `tick` accepts all tick subcommands and flags; `status`, `events`,
`preflight`, and `labels` select their respective scripts. Use
`~/.claude/pr-pipeline/bin/pr-pipeline --help` for the command list.

**Where your settings live.** `~/.claude/pr-pipeline/config.toml`, outside both
the install and every target repository — see [Configuration](#configuration).
Do not edit the install's `skills/tick/config.toml`: it is the
shipped floor, and a plugin upgrade installs the next version at a different
path, silently discarding anything written there.

**Provision each target repository before its first tick.** Run this from the
target checkout, both at first-time installation and when adding a repository:

```bash
~/.claude/pr-pipeline/bin/pr-pipeline labels provision
```

This setup primitive resolves the same canonical Git target as the tick and
creates only missing `LABEL_*` labels from `pipeline.py`. Re-running it preserves
existing labels, colors, and descriptions. It reports every unknown `ai:` label
and explains that items carrying only unknown workflow labels are not selected
by any dispatch query. It never deletes, renames, or updates a label.
`/pr-pipeline:setup` performs the same provisioning, by repository name, for
each repository it adds.

Every tick checks the complete vocabulary before HARVEST or DISPATCH. Missing
labels, a failed read, or a malformed response exits 3 with
`startup_labels_invalid`; missing-label errors name each label and print the
provisioning command. A loop rechecks on every tick and stops on this startup
failure. Unknown labels do not block a tick. The status dashboard's **PARKED
ITEMS** section keeps items with only unknown `ai:` labels visible across all
pages of open issues and PRs.

```bash
# 1. One-time: verify the Codex binary and schema (required before first reviewer dispatch)
~/.claude/pr-pipeline/bin/pr-pipeline preflight

# 2. Open the dashboard in a separate pane
# Primary (Linux / watch installed):
watch -n 5 '~/.claude/pr-pipeline/bin/pr-pipeline status'
# Fallback (macOS / no watch):
while true; do clear; ~/.claude/pr-pipeline/bin/pr-pipeline status; sleep 5; done

# 3. Run one tick (full HARVEST + DISPATCH loop)
~/.claude/pr-pipeline/bin/pr-pipeline tick

# 4. Run a targeted tick (single PR or issue)
~/.claude/pr-pipeline/bin/pr-pipeline tick pr:468
~/.claude/pr-pipeline/bin/pr-pipeline tick '#439'

# 4b. Launch only listed work, in list order (harvest stays global)
~/.claude/pr-pipeline/bin/pr-pipeline tick --only '#439,pr:468'

# 5. Verify the dispatch landed (use find to avoid bare-glob errors in zsh)
find .claude/worktrees -maxdepth 2 -name dispatch.json -path '*/issue-*' \
  -exec jq '{engine, stage, pid}' {} \;

# 6. Loop in-process (autonomous polling with mutex + quiescence stop)
~/.claude/pr-pipeline/bin/pr-pipeline tick --loop
```

## Document split

Three files, each with a clear job:

- **`SKILL.md`** — Injected operator SOP (≲80 lines). Run procedure, outcome
  check, exit codes, stop conditions, `/loop` prompt.
- **`README.md`** (this file) — Usage guide: invocation modes, observability
  surfaces, worked examples, design principles.
- **`DEBUG.md`** — Debugging and recovery reference: operational invariants,
  sandbox rationale, startup diagnostics, reviewer posture, constants, and
  debugging pointers. Read it when a tick fails or the SOP says "read DEBUG.md."

Update each affected surface when its commands or stop conditions change;
link to the detailed owner instead of copying its mechanics. The
[internals reference](../../docs/working/pr-pipeline-internals.md) owns current
runtime behavior, [architecture](../../docs/architecture.md) owns rationale, and
the [distribution plan](../../docs/working/plugin-distribution-design.md) owns
unfinished migration requirements.

### Stage and engine state

The current implementation runs the full HARVEST + DISPATCH loop. Reviews run via
Codex (`engine_wrapper.py`); implementers and fixers run via Claude (`claude -p`).
All three stages support targeted mode (`pr:N` / `'#N'`). The orchestrator also
supports in-process `--loop` mode with a tick mutex, self-paced cadence,
edge-triggered wake nudges, quiescence stop, and terminal-label notifications.

## Configuration

Operator configuration lives in **`~/.claude/pr-pipeline/config.toml`** —
`$CLAUDE_CONFIG_DIR/pr-pipeline/config.toml` when that variable is set, and
`$PR_PIPELINE_CONFIG` names the file outright (that override exists for tests
and one-off runs).

Keep operator values in this file, not in the packaged defaults or target repo.
See [configuration ownership](../../docs/architecture.md#configuration-ownership)
for the upgrade and trust rationale.

```toml
[operator]
login = "your-github-login"   # whose issues are picked up and PRs reviewed

[defaults]                    # operator-wide; every key is overridable per repo
allow_touching  = true        # auto-review PRs that edit reviewer-governing files
approved_reviewers = []       # logins whose PR comments direct the fixer; login is implicit
checklist_paths = []          # the target's own review checklists, repo-relative
verify_tools    = ["<your test command> *"]  # engine Bash grants, replaced wholesale
base_ref        = ""          # "" = discover the target's GitHub default branch
permission_mode = "auto"      # Claude implementer/fixer --permission-mode
engine_settings = ""          # Claude implementer/fixer --settings; "" = omit
max_in_flight   = 3           # admission cap: engines running at once per target
implementer_timeout_seconds = 3600  # see "Deadlines, stall limits, routing and cadence"

[scan]
repos = []                    # empty or absent = unrestricted

[repos."owner/notes-app"]     # overrides any [defaults] key, for one target
max_in_flight = 5
base_ref     = "master"
verify_tools = ["<that repo's test command> *"]
impl_engine  = "claude"       # editable routing defaults, initially set by setup
model        = ""
effort       = "high"         # implementer CLAUDE_CODE_EFFORT_LEVEL; "" = no override
fix_effort   = "medium"       # fixer effort; "" inherits effort
probed_impl_engine = "claude" # setup evidence: refresh through setup
probed_model       = ""
```

The verification commands are whatever the target repository's own
`CLAUDE.md`/`AGENTS.md` prescribe — the pipeline never names a toolchain, it only
grants permission to run the one the operator declares. A trailing ` *` allows
arguments; without it only the bare command is permitted.

Per-repository keys override `[defaults]`; lists replace wholesale, so
`verify_tools = []` grants no verification commands. Explicit CLI overrides win.
The [resolver contract](../../docs/working/pr-pipeline-internals.md#7-operator-configuration-resolution)
owns the complete precedence and validation rules.

`approved_reviewers` lists GitHub logins, beside `[operator] login`, whose PR
conversation comments and submitted reviews the fixer acts on. See
[Comment trust and incomplete history](#comment-trust-and-incomplete-history).

Bad configuration exits 3 and names the offending key. The exception is
`allow_touching`: an unusable value keeps the conservative gate, warns, and lets
the tick run. An explicitly empty grant is valid; a malformed grant is an error.
Fix the named value rather than treating the error as an empty/default setting.

Fresh installs ship `verify_tools = []`. Implementer and fixer dispatch still
proceed, log `verification_unconfigured`, and pin that state in `dispatch.json`.
Their successful completion must include `VERIFICATION_UNCONFIGURED_WAIVER`
on the line immediately before the final success sentinel, including no-change
fixer completion. Missing waivers escalate. This waives only the missing operator
grants: failed or denied required checks still block completion, and an absent
check is never evidence of a pass.

Harvest puts a visible "Verification unconfigured" warning first in the PR body,
including generated fallback bodies and rediscovered PRs. The handoff rechecks
live head/body/holds and retains progressed PR labels. Fixer replacements preserve
that history, even if grants are configured later; an empty-grant fixer with no
replacement still adds the warning through the guarded body-write path. Failed
reads or writes retain custody for retry. Configured grants do not prove that
any check ran.

A blank `base_ref` is not malformed; it is the documented "resolve the target's
default branch". Issue dispatch reads it from the canonical target on GitHub and
pins the selected branch for PR creation; failed discovery stops dispatch with an
error naming the repository, and an explicit override skips discovery. A blank
`permission_mode` is invalid. A blank `engine_settings` omits `--settings`; a
value that is not a JSON object, or an absolute path to a readable file holding
one, is invalid.
`$PR_PIPELINE_CONFIG` and `$CLAUDE_CONFIG_DIR` must
name absolute locations.

`max_in_flight` is the admission cap: the most engines (implementer, reviewer,
and fixer combined) that a tick keeps running at once for one target checkout.
The shipped default is 3. Set it in `[defaults]`, or in `[repos."owner/name"]`
for one target, to match your plan limits, machine, and queue size. The cap
counts this checkout's own dispatch markers, so it is not shared across
repositories or machines. The value must be an integer of at least 1. Zero, a
negative number, a float, a string, or a boolean stops the tick at startup with
an error that names the key; the tick never substitutes a different cap. Each
tick reads the cap once, at startup. Every dispatch stage admits against that
value, and the tick's cadence (and, under `--loop`, the observation after the
tick) judges capacity against the same value. A config edit takes effect at the
next tick.

The operator login is reloaded for each tick; a blank login stops the loop.
Issue pickup and reviewer admission both compare against this identity. It
stays fixed through dispatch and the following cadence observation, even if
the config changes during a tick.

The target repository and remote come from the main checkout's Git configuration,
anchored by the common Git directory even when invoked from a linked worktree.
With one GitHub remote, no repository setting is needed. With multiple remotes, set
`PIPELINE_REPO=owner/name`; exactly one remote must match. Missing or ambiguous
matches, unsafe remote names, and differing fetch/push destinations stop startup
before GitHub access. `[target] repo` is ignored with a migration warning.

`[scan] repos = []` permits any resolved target; a non-empty list restricts the
tick, including `PIPELINE_REPO` overrides. Repository identity comparisons are
case-insensitive, including per-repository config and reviewer admission.
Duplicate `[repos.*]` sections differing only in case are configuration errors.

The same selected target drives controller and dashboard operations. An engine
worktree whose effective fetch/push destination differs is refused before
launch, leaving the item queued. A reviewer-harvest mismatch retains the marker
and result: correct the Git configuration and retry. The
[internals target-resolution contract](../../docs/working/pr-pipeline-internals.md#7-operator-configuration-resolution)
describes the shared resolver, explicit GitHub host, and worktree-context checks.
Git configuration must remain stable while a dispatched engine is running.
Supported URLs are GitHub.com HTTPS, SSH
(`git@github.com:owner/name` or `ssh://git@github.com[:22]/owner/name`), and Git
protocol URLs, with an optional `.git` suffix. Remote names use letters, digits,
dot, underscore, or hyphen, start with a letter or digit, and cannot contain
`..` or end in `.` or `.lock`.

A startup failure exits 3 in both single-tick and `--loop` mode; loop cadence
does not issue GitHub requests after a rejected target or configuration. A tick
that crashes (`tick_crash`, exit 4) also ends `--loop` before the next wait, so
a persistent exception reaches the operator instead of retrying in the
background.

### Deadlines, stall limits, routing and cadence

These keys tune paid engine lifetimes, review effort, engine routes, and loop
activity. Each one can be set in `[defaults]` and overridden in
`[repos."owner/name"]`, like every other per-target key. The packaged
[`config.toml`](config.toml) ships today's values and documents each key.

| Key | Unit | Default | Accepted values | Read by |
|---|---|---|---|---|
| `implementer_timeout_seconds` | seconds | 3600 | integer ≥ 60, greater than `engine_stall_seconds` | implementer dispatch, recorded as the marker `deadline` |
| `fixer_timeout_seconds` | seconds | 3600 | integer ≥ 60, greater than `engine_stall_seconds` | fixer dispatch, recorded as the marker `deadline` |
| `review_timeout_seconds` | seconds | 1200 | integer ≥ 60 | reviewer dispatch, recorded as the marker `deadline` |
| `engine_stall_seconds` | seconds | 1800 | integer > 600 and less than both engine deadlines | implementer/fixer dispatch, recorded in the marker for stall detection |
| `engine_stall_startup_grace_seconds` | seconds | 300 | integer ≥ 0 | implementer/fixer dispatch, recorded in the marker |
| `max_iterations` | review/fix rounds | 5 | integer ≥ 1 | reviewer harvest: the round that reaches it gets `ai:max-iterations` |
| `impl_engine` | — | `"claude"` | `"claude"` or `"claude-ds"` | implementer launch |
| `model` | — | `""` | blank, or a model id not starting with `-` | implementer `--model` (`ANTHROPIC_MODEL` for claude-ds) |
| `fix_engine` | — | `""` | blank (the implementer's engine), `"claude"`, or `"claude-ds"` | fixer launch |
| `fix_model` | — | `""` | blank, or a model id | fixer `--model`; blank inherits `model` only while the fixer runs on the implementer's engine |
| `effort` | — | `""` | blank, `"low"`, `"medium"`, `"high"`, `"xhigh"`, or `"max"` | implementer `CLAUDE_CODE_EFFORT_LEVEL`; blank sets nothing on claude (the inherited environment and settings apply) and means `max` on claude-ds |
| `fix_effort` | — | `""` | blank, or a level allowed for `effort` | fixer `CLAUDE_CODE_EFFORT_LEVEL`; blank inherits `effort` on any fixer engine |
| `review_model` | — | `""` | blank (codex config, then `gpt-5.5`), or a model id | reviewer `--model` |
| `review_effort` | — | `""` | blank (codex config, then `high`), or a bare token | reviewer `model_reasoning_effort` |
| `loop_short_poll_seconds` | seconds | 270 | integer ≥ 60, not 300 | wake while work is queued, at capacity, or state is unknown |
| `loop_max_poll_seconds` | seconds | 270 | integer ≥ 60, not 300 | longest wait while an engine is in flight |
| `loop_heartbeat_seconds` | seconds | 1800 | integer ≥ 60, not 300 | wake while quiescent |
| `loop_quiescent_stop_after` | heartbeats | 3 | integer ≥ 1 | quiescent stop in both loop paths |
| `transient_cooldown_seconds` | seconds | 3600 | integer ≥ 60 | provider cooldown after a transient result that names no reset time |
| `transient_cooldown_reset_buffer_seconds` | seconds | 300 | integer ≥ 0 | margin added to a reset time the provider names |

Precedence is the same as for every key: explicit CLI flags, then
`[repos."owner/name"]`, then `[defaults]`, then the packaged floor. Only the
route keys have flags: `--impl-engine`, `--model`, `--fix-engine`,
`--fix-model`, `--effort`, `--fix-effort`, `--review-model`, and
`--review-effort`. A typed flag is
validated like a config value.

Integer keys accept integers only. A boolean, float, or string, a value below
the minimum, a wake of exactly 300 seconds (the prompt-cache TTL cliff, which
the cadence never waits for), or a stall threshold outside its bounds stops the
tick at startup with exit 3, naming the key. So does a route value that is not
a supported engine or a usable model id. Nothing falls back silently.

The optional harness `/loop` prompt schedules each one-shot tick through
`ScheduleWakeup`, which clamps a wake to 60–3600 seconds. Under that prompt a
`loop_heartbeat_seconds` above 3600 wakes after 3600 seconds, so the quiescent
stop comes sooner than the configured interval implies. `--loop` has no clamp.

The tick resolves the configuration once, at the startup gate, before HARVEST.
Every stage and both loop paths read that one validated configuration, so a
config edit made during a tick takes effect at the next tick. An edit that
makes one of these keys invalid mid-tick changes nothing in that tick; the next
tick refuses it at startup. Each engine's
deadline and stall limits are recorded in its `dispatch.json` at dispatch:
reconciliation, the stall watcher, and the status dashboard read them from
there, so a later edit does not move them for an engine already running.

`/pr-pipeline:setup` sets `impl_engine` and `model` in the repository's section,
so the tick runs the measured route without flags. In the same atomic config
write, it records separate `probed_impl_engine` and `probed_model` evidence.
These two metadata keys apply only to that repository; they do not inherit
from `[defaults]` or CLI flags. Edit routing defaults as needed, and refresh
probe metadata only by rerunning setup. Both metadata keys must be present
and valid together; a partial or malformed record stops startup with exit 3.

When a config edit or flag selects a different implementer route, the tick
prints `tick: implementer route … differs from the route /pr-pipeline:setup
probed …` at startup, logs `startup_route_unprobed`, and runs. Setup measured
`auto` permission mode on one route only; rerun setup to measure another. A
repository set up before separate probe metadata was recorded has no recorded
route, so the tick has nothing to compare against; rerun setup to record it.
Mutable routing defaults alone never count as probe evidence.

### Unconfigured targets

A target is configured when the operator config has a `[repos."owner/name"]`
section for it, even an empty one. `[defaults]` alone does not count: it says
what the operator wants everywhere, not that anyone proposed this repository's
verification grants or probed its engine route. Only `/pr-pipeline:setup`
writes that section.

After the configuration and login checks, and before the label check, a tick
whose target has no section stops. It writes `startup_repo_unconfigured` to
the tick log and exits 6 before HARVEST or DISPATCH, naming the missing
section and `/pr-pipeline:setup`. The tick never reads input:

- **A single tick** hands the prompt to the operator's session. The
  `/pr-pipeline:tick` skill asks the operator whether to run setup, then reruns
  the tick.
- **`--loop`** has nobody to answer, so it logs the repository as skipped and
  returns 6 without observing cadence. Run setup, then restart the loop.

A missing `[operator] login` still exits 3; its message also names setup.

Use separate clones for different target repositories. Existing markers and
receipts share a common-directory state space and do not record repository
identity; switching `PIPELINE_REPO` to another repository while reusing that
state is not supported. Target resolution does not migrate existing state;
[repository binding is tracked separately](https://github.com/kwuBuild/pr-pipeline/issues/41).

## Invocation

The orchestrator is a single Python process: `tick.sh` resolves a Python >= 3.11
interpreter and runs `lib/tick.py` under it. The `/pr-pipeline:tick` skill loads
a lean SOP that tells the operator when to run `preflight.sh`, how to invoke the
tick, and where to inspect outcomes.

### Basic

```bash
~/.claude/pr-pipeline/bin/pr-pipeline tick
```

Runs one full tick: HARVEST all three stages (review → implement → fix) with full
side-effect processing (PR creation, comment posting, label transitions), then
DISPATCH all three stages (reviewer → implementer → fixer). Exit code is 0 on
success, 1 when reconciliation failed and dispatch was skipped, and 2 when a
DISPATCH gate held some or all dispatch: the authoritative in-flight label read
failed, or an installed doc a stage requires is unreadable (see
[prompt-bound skills](#prompt-bound-skills-skill_unresolved)).

### Targeted (single issue/PR)

```bash
~/.claude/pr-pipeline/bin/pr-pipeline tick pr:468   # PR #468 only
~/.claude/pr-pipeline/bin/pr-pipeline tick '#439'   # issue #439 only
```

`pr:N` stays direct PR scope for reviewer and fixer work; it does not switch to
same-number issue scope. For `#N`, implementation stays scoped to issue `N`, while
zero or one open closing PR scopes the reviewer and fixer. With no open closing PR,
only the issue implementation scope is active. An ambiguous or malformed
closing-PR response, or GitHub read failure, stops before scratch/worktree or GitHub workflow mutation; the audit log, tick lock, and target quiet counter are excepted. Invalid target syntax has the same workflow-mutation boundary and exits `1`.
Only one positional target is accepted: a second one (for example
`tick --loop '#101' pr:66`) exits `1` before any work instead of silently
replacing the first. Use [`--only`](#dispatch-list---only) for several items.

HARVEST, stale-label reconciliation, and DISPATCH use those resolved stage scopes.
Safety gates (`reconcile_failed`, `max_in_flight` admission cap) still apply
globally, and targeting never changes labels or scratch for unrelated work.

### Target completion and cadence

Both a one-shot targeted tick and `--loop` check the target after harvest and
dispatch. When every active stage scope is closed, merged, or carries exactly
one terminal label (`ai:ready-for-human`, `ai:needs-human-decision`, or
`ai:max-iterations`), the cadence line reports `next_wake=0 stop=true`. Its reason
names the requested target, each resolved item, and its label or closed state.
The tick returns its exit status and the same cadence decision it emits.
`--loop` consumes that decision without another target or queue read, so a later
read failure cannot override an advertised stop. It reports the terminal target
reason through stderr, the `loop_target_stop` log event, and a best-effort
operator notification. This stop does not wait for quiet heartbeats or
unrelated engines. It releases no engine
custody; unrelated work still needs its own ticks.

An issue target follows the same closing PR that dispatch resolves. When an open
issue has no `ai:` label and has one open closing PR, implementation has handed
off to that PR. The PR determines completion. If the issue still has a workflow
label, both the issue and the PR must be terminal or closed. A blocked or
in-flight issue therefore cannot be hidden by a terminal PR. Handoff requires
the selected PR to remain open in the completion observation. If it closed or
merged after scope resolution while the issue remains open and unlabelled,
completion is unknown; the next tick resolves the scope again.

The existing human-hold priority applies: `ai:needs-human-decision` is terminal
even beside another workflow label, including a live engine's label. The reason
names all observed labels, and stopping does not release the marker.

Non-dispatchable does not mean finished. A target with `ai:blocked`, no `ai:`
label and no closing PR, or conflicting workflow labels reports `not terminal`
and continues polling. Quiet heartbeats do not stop an unfinished target.
Unreadable scope, item, or control-plane state stays on the short poll and
cannot prove completion. No target check bypasses startup or reconciliation
failures.

The actionable-queue probe reads only resolved stage scopes, using
`pipeline.resolve_pickup_eligibility` just as dispatch does. In-flight count and
admission capacity remain global. Thus `tick-state` can report an unrelated
engine in flight while `tick-cadence` reports that the requested target stopped.

Quiet streaks live under `.claude/worktrees/.tick-state/quiescent-counters/`:
`queue.txt` for the full queue, `issue-N.txt` for `#N`, `pr-N.txt` for `pr:N`,
and `only-<key>.txt` for an [`--only` list](#dispatch-list---only).
Each tick reads, advances, or resets only its own scope, including on failures.
The target key remains the requested issue even after handoff to its closing PR.
The former shared `quiescent-counter.txt` is ignored because its prior scope
cannot be recovered; the new counters start at zero. Missing or malformed
counters, including negative values, also start at zero. Writes are atomic and
ticks use the existing mutex. Native loops use this same persisted streak;
there is no separate in-memory stop counter. The invocation's first tick
counts toward the streak; later native-loop ticks count only after a full
scheduled wait. With a fresh queue counter and the shipped threshold of three,
three quiet ticks stop the loop after two 1800-second waits. A retained streak
can stop it sooner. Nudge-triggered ticks do not advance that streak.
If a nudge first observes a quiet queue after activity, the count stays zero
and the loop waits, even when the configured stop threshold is one.

### Dispatch list (`--only`)

```bash
~/.claude/pr-pipeline/bin/pr-pipeline tick --only '#439,pr:468'
~/.claude/pr-pipeline/bin/pr-pipeline tick --loop --only='#439,pr:468,#440'
```

`--only` lets an operator drain a chosen set of items concurrently without
changing GitHub labels. It bounds which items a tick may **launch**. It does
not narrow custody work: HARVEST, stale-label reconciliation, and admission
occupancy stay global. This deliberately differs from a positional target,
whose HARVEST is scoped to the target.

Normal flow of one tick with a list:

1. HARVEST and stale-label reconciliation run for every engine this machine
   owns, listed or not. A finished unlisted engine is harvested and relabeled
   as usual; harvest removes its local dispatch marker, which frees its
   admission slot.
2. The tick expands the list once. A listed issue keeps implementation scope
   and gains its one open closing PR immediately after it, so review and fix
   can finish work the list started. The expansion uses the same strict
   zero-or-one closing-PR resolver as a positional `#N`. The tick logs the
   result as `only_scope_resolved` (`details.only` and `details.scope`).
3. DISPATCH runs in the usual stage order: reviewers, implementers, fixers.
   Each stage considers only listed items of its own kind, in list order.
   No stage lists its label queue. The implement stage reads each listed
   issue directly. The review and fix stages read each listed or expanded PR
   directly and keep it only when it is open and carries one of the stage's
   pickup labels, the same selection the queue query makes. So an item
   outside the bounded 200-item queue page is still found, and an unlisted
   item receives no read and no write. An unreadable, closed, or mismatched
   listed item logs a `skip` and is read again next tick. All stages
   share the one `max_in_flight` cap, so under limited capacity the earlier
   list position wins within a stage. Strict ordering across stages is not
   provided.
4. Cadence observes the same expanded scope; it does not expand the list a
   second time. The next tick expands it again, so a closing PR opened in
   between joins the scope.

`--only` does not bypass trust, eligibility, reconciliation, or admission
gates. A listed issue by another author, a held item, or an item at an
in-flight label is skipped exactly as in a full-queue tick.

Input rules. Tokens are `#N` (issue) or `pr:N` (PR) with a positive number,
separated by commas; spaces around tokens are ignored. A duplicate keeps its
first position. The CLI exits `1` before the tick lock or any GitHub call for
an empty list or element, a malformed token, a repeated `--only`, or `--only`
combined with a positional target.

**Expansion read failure.** When a GitHub read for the expansion fails, or a
closing-PR response is malformed or ambiguous, the tick has already completed
HARVEST and stale reconciliation (step 1). It then starts no new engine, logs
`only_scope_unreadable` with the reason and the requested list, prints
`PARTIAL (dispatch skipped: --only scope unreadable)`, exits `2`, and reports
`state unknown` on the short poll. The next tick reads the list again. This
differs from the positional target, which fails closed before HARVEST; that
path is unchanged.

**Completion and quiet counter.** A list has no immediate terminal stop like
the [target completion rule](#target-completion-and-cadence). It uses the
quiescent table below with a quiet streak of its own:

- The list's queue value is tri-state: `true` when any listed or expanded item
  is dispatchable, `false` when every item is proven non-dispatchable or
  closed, and `unknown` when any read or payload cannot be trusted. Unknown
  stays on the short poll and resets the list's streak.
- Occupancy stays global. While this machine owns any engine, listed or not,
  the tick is not quiet, so a loop never stops while it still owes a harvest.
- The streak lives in `quiescent-counters/only-<key>.txt`. The key is a hash
  of the listed *set*, so reordering the same items keeps one streak and a
  different set starts its own. It never shares a file with `queue.txt` or a
  target's `issue-N.txt`/`pr-N.txt`, and a list tick never reads or resets
  those.
- After `loop_quiescent_stop_after` qualifying quiet observations, the cadence
  reports `stop=true` with a reason that starts with `--only <list>:`, names
  each item's labels, and says `--only list exhausted`. `--loop` reports it
  through stderr, the `loop_only_stop` log event, and an operator
  notification. Work outside the list may still be queued.

Every cadence reason in a list run starts with `--only <list>:` and the
observed item states, so the one-shot `/loop` path can report progress.

### Looped (autonomous polling)

```bash
~/.claude/pr-pipeline/bin/pr-pipeline tick --loop
```

Runs ticks inside one Python process. `--loop` keeps the same HARVEST →
DISPATCH ordering as one-shot mode and self-paces between ticks. A targeted loop
uses the [target completion rule](#target-completion-and-cadence). A list loop
uses the [dispatch list rule](#dispatch-list---only). The full queue
exits after the configured number of quiescent heartbeats.

Every tick invocation, looped or one-shot, takes a non-blocking mutex at
`.claude/worktrees/.tick.lock`. If another tick is running, the new invocation
exits immediately with code `5`, writes `lock_held` to the tick log when
possible, and does no harvest or dispatch work.

| State at end of tick | Next wake (shipped default) |
|---|---|
| Target scopes terminal or closed | stop immediately; see [target completion](#target-completion-and-cadence) |
| ≥1 engine in-flight, deadline > 0 | `min(nearest deadline, loop_max_poll_seconds)` (270s) cache-warm poll |
| ≥1 engine in-flight, deadline lapsed mid-scan | 1s — immediate retry to harvest/escalate |
| No engine in-flight, queue non-empty | `loop_short_poll_seconds` (270s) cache-warm poll |
| At `max_in_flight`, saturated | `loop_short_poll_seconds` (270s) |
| Quiescent (no supervisable engines or actionable work) | `loop_heartbeat_seconds` (1800s) heartbeat |
| Full queue quiescent for `loop_quiescent_stop_after` (3) consecutive heartbeats | stop + notify |
| `--only` list drained, no local engine, for `loop_quiescent_stop_after` (3) heartbeats | stop + notify (`loop_only_stop`); see [dispatch list](#dispatch-list---only) |

The intervals are [operator configuration](#deadlines-stall-limits-routing-and-cadence).
Both loop paths — the one-shot `tick-cadence:` line and `--loop` — pace from the
configuration the tick validated at startup. An invocation turned away by the
mutex paces its retry from the configured short poll when the configuration
resolves cleanly.

Only a deadline that still requires cadence action paces the loop. Both cadence
surfaces share one scan, and it counts a marker only when the identity-verified
classification harvest and admission trust says `live` or `launching`. A reused
PID, a dead engine, or any `hung` engine contributes no deadline — whether a human
now owns recovery or a failed termination left the marker under pipeline custody
for another attempt. Such a marker still occupies a concurrency slot and keeps
the loop warm, but its deadline is a fixed past timestamp that never becomes less
overdue. In the failed-termination case a 1s retry cannot accelerate an `EPERM` or
an already-delivered `SIGKILL`; the wake watcher nudges within 5s after PID and
group death. The overdue row above is therefore a narrow mid-scan race, not a
steady-state retry contract.

In `--loop` mode these intervals are an **upper bound**: a wake nudge can start
the next tick sooner (see [Edge-triggered wake-up](#edge-triggered-wake-up-in-loop-mode)).
The one-shot `tick-cadence:` line and the harness `/loop` prompt that consumes it
are unchanged — that path has no external interrupt and stays purely timer-paced.
The two paths share persisted quiescent streaks within each scope, so `--loop`
marks a nudge-initiated tick as not-a-heartbeat. Each counter retains qualifying
quiet observations for whichever path reads that scope next.

Both paths derive the quiescent reason from the same helper, so a stop caused by
a human-held engine names it identically: `--loop` in its notification, the
one-shot path in the `tick-cadence:` reason the harness reports (see
[Held engines](#held-engines)).

SIGINT/SIGTERM request a clean stop: the current tick finishes, sleep is
interrupted, and the lock is released.

### Edge-triggered wake-up (in `--loop` mode)

Ticks are level-triggered: every tick re-reads labels and markers in full. That
makes an early wake safe by construction — a nudge carries no information, and a
spurious or duplicate one costs at most one extra tick. So `--loop` waits on a
wake channel with the cadence interval as its timeout, and the watcher thread
(`_wake_watcher_loop`) is the only thing that ends that wait early *into another
tick* — SIGINT/SIGTERM also end it, but to stop the loop rather than to nudge it.
Every nudge source below is one of that thread's polls; nothing outside it turns
a sleep into an early tick:

1. **Engine completion.** A watcher thread polls every 5s for evidence that a
   dispatched engine finished. Before probing liveness, it polls the `Popen`
   handles retained for children launched by this tick process. This nonblocking
   wait reaps exited children so zombie PIDs cannot delay a completion wake until
   the cadence timer. It never waits on a PID taken from a marker. Each tick
   also polls those handles, so reaping continues if the watcher fails.
   Reaping supplies no completion or custody authority; the checks below still
   apply. The artifact check is reviewer-only: a
   `completion.json` or `review.json` has landed, which the reviewer wrapper
   publishes by atomic rename after the engine exits. Implementers and fixers
   run wrapperless and publish neither, and their engine holds a `Write` tool
   with the worktree as its cwd — so for them one of those files is untrusted
   input, not evidence, and is not read. The liveness check runs for every
   stage: both the marker's recorded PID and process group are no longer
   alive. Wrapper death alone is not
   completion: a paid child can continue in the inherited process group, so
   watcher, harvest, reconciliation, and redispatch retain custody while that
   group lives. If its deadline expires after wrapper identity is lost, the
   item escalates to `ai:needs-human-decision`, but its marker and scratch stay
   in custody and the item continues to occupy a `max_in_flight` slot until the
   group dies. That escalation is also the point the engine becomes *held*
   (see [Held engines](#held-engines)), so it stops holding loop cadence
   awake. Only validated positive
   identifiers are probed — `os.kill` on a zero or negative PID targets a
   process *group*, which is why `_classify_marker` refuses those too. A
   non-empty `result.json` is
   deliberately *not* evidence: it is a stdout redirect created empty at spawn
   and written in place, so a read can catch a partial write and it never proves
   the engine exited. Each dispatch identity nudges at most once — spending
   that one nudge before harvest could act on it would leave real completion
   waiting for the next scheduled poll.
2. **Wake-request file.** Anything that creates
   `.claude/worktrees/.tick-state/wake-request` nudges the loop; the watcher
   consumes it by unlink, so repeated touches coalesce into one early tick:

   ```bash
   touch .claude/worktrees/.tick-state/wake-request
   ```

   This is the generic nudge interface for external transports (an operator, a
   webhook forwarder) — none of them need to know anything about tick internals.
3. **Engine stall.** The same watcher poll checks whether a live implementer or
   fixer has stopped *producing*. Its stream wrapper bumps the mtime of a
   `heartbeat` file beside `result.json` as engine output events arrive; when
   that mtime has not moved for the engine's stall threshold (`engine_stall_seconds`,
   30 minutes shipped, recorded in its marker) the loop wakes and the operator gets
   an `engine_stalled` tick-log entry and a notification. See
   [Engine progress heartbeats](DEBUG.md#engine-progress-heartbeats-engine_stalled).

   Unlike the two sources above, this one is **not** `--loop`-only. Every tick
   runs the same scan at the start of its body, so one-shot ticks report the
   stall too — they simply have no sleep for the nudge half to shorten. Both
   callers share the episode memory in
   `.claude/worktrees/.tick-state/stall-episodes.json`, which is what keeps a
   single wedge from being reported on every poll. A report attempt is
   checkpointed before the scan advances; if the process exits before that
   post-report checkpoint commits, the next scan retries the attempt rather
   than risking a silent stall.

   This is **detection only** — no kill, no relabel, no requeue, no deadline
   change. A stalled engine keeps its full deadline (60 minutes shipped); the signal exists
   so the operator learns about a wedge tens of minutes before that timeout
   fires, instead of watching a healthy-looking row burn its whole deadline.

Two properties keep this from becoming a control-plane risk:

- **The watcher holds no custody.** It reads no results, makes no state
  transition, and never kills engines or cleans scratch. Harvest remains the sole
  completion authority. If the watcher fails it logs `wake_watcher_failed` once
  and exits, and the loop continues on its unmodified timer schedule.
- **Nudges cannot accelerate the quiescent stop.** Only a tick that followed a
  wait running its full scheduled interval counts toward the
  consecutive-quiescent heartbeat limit. The invocation's first tick also
  counts; see [counter and target completion rules](#target-completion-and-cadence).
  This gate covers the per-scope files shared by native and one-shot ticks.
  A terminal target stops immediately.
  A nudge-initiated tick is also held to a 10s floor measured from the end of
  the previous tick, bounding the rate a nudge storm can force. The previously
  scheduled cadence deadline remains the upper bound: if it arrives before the
  floor, the tick starts at that deadline.

Terminal transitions into `ai:ready-for-human`, `ai:needs-human-decision`, or
`ai:max-iterations` emit a best-effort macOS notification, a stderr line, and a
`notify_terminal_label` tick-log entry. Notification failure is non-fatal. The
loop's full-queue quiescent stop emits the same operator-facing signals with
`loop_quiescent_stop`, a terminal target stop with `loop_target_stop`, and a
detected engine stall with `engine_stalled`. The
quiescent stop also names any held item occupying a slot when it stops.

### Held engines

A dead wrapper can leave a paid child alive in its recorded process group. The
tick can neither prove that process's identity nor safely signal the group, so
it escalates the item to `ai:needs-human-decision` and leaves the marker and
scratch alone while the group may still be alive. Occupancy has to answer two
different questions about that marker, and one count cannot answer both:

- **Admission** asks "could a paid engine still be running here?" — yes, so the
  slot stays taken and no second engine is ever dispatched onto the same work.
- **Quiescence** asks "is there a decision left for *the loop* to make?" — for
  the escalated-and-left shape, no: the hold has handed that to a human.
  Counting it would make the loop's terminal stop unreachable, so the pipeline
  would poll forever with nothing to do and the operator would never be told.

A hold on its own is **not** enough to discount an item from quiescence. The
question is whether a process is still alive under the marker, not what the
marker's classify state says — the loop only stops supervising work it has
genuinely given up on. `_pipeline_actionable` is where that bound lives, and the
table reads off it: an item stays awake while its process or group is alive and,
for a `hung` marker, while identity proved `matched`; a held item outside that
bound is discounted.

| Held item | Pipeline still owes it | Discounted? |
|---|---|---|
| `live`, group alive | enforce the deadline; archive the evidence and reclaim the slot on proven death | **No** |
| `complete`, group alive | archive the result once the group dies | **No** |
| `hung`, identity `matched` | terminate the runaway | **No** |
| `hung`, identity unprovable | nothing — escalate-and-leave | Yes |
| `launching` (pre-spawn lease) | nothing — the marker records no PID | Yes |
| markerless | nothing — no process to harvest or signal | Yes |

Discounting a live engine would let the loop stop with a paid process still
running and nobody left to collect its result. `complete` is on that list
because an envelope can be published while a child still runs, and the hold
defers the archive until the group is proven dead — so the loop has to be there
to do it.

The `yes` rows that still have a marker are the shapes the pipeline can never
act on again. A lease is the sharper one: `valid_marker` makes the ownership
groups mutually exclusive, so a lease carries no `pid`/`start_time`/`pgid` and
`classify_dispatch` pins it at `launching` without consulting the deadline.
Death can never be proven for it, so the hold gate reclaims it on the absence of
a recorded process instead — otherwise the pipeline strands a slot on its own
escalation, since a crashed spawn escalates to `ai:needs-human-decision` and
then reads that label back as a hold forever.

"Publish" is not "harvest". A hold forbids advancing the stage, so a result that
finishes under one does not create a PR, promote a review, or move a fixed PR to
recheck. Once process-group death is proven, the tick prepares local evidence,
publishes the validated engine outcome on the item, releases only its in-flight
label, and then finalizes the archive. `held_result_published` names that completed
sequence; `held_result_comment_failed` means the post failed and the marker,
result, scratch, and attempts all remain in source custody for the next tick.

The exact hold is revalidated at mutation time — before the comment is published
and again before the terminal local custody move, whether or not an in-flight
label needs removal — so a hold removed after the decision cannot receive a
comment claiming it "remains authoritative", nor be finalized from a stale
observation. Once the comment has landed, the prepared archive is retained even
if the release then fails: the comment has promised the operator the output is
in the harvest-evidence archive, and a live promise must not point at nothing.

The durable workflow authority is the GitHub human-hold label itself:
`ai:needs-human-decision` or `ai:blocked`. There is deliberately no local
custody receipt. The completed-result branch does keep a controller-owned comment
receipt long enough to suppress reposts after a crash; it records publication,
not ownership, and is retired after the archive lands. The hold label remains
remote, durable across process and host restarts, visible to the operator, and
re-read on every tick.

#### Two custody tracks

A hold does not transfer one thing; it transfers one of two, and conflating them
is how a held item ends up either over-protected or leaking a slot:

| | **Workflow custody** | **Process custody** |
|---|---|---|
| Governs | labels, escalation, advancing the stage | the paid process, the dispatch deadline |
| Does a hold transfer it? | **Yes**, fully | **No — it never transfers** |
| Released by | the operator resolving the hold | proven process-group death |

Process custody never moves because the operator cannot hold it: they have no
PID, cannot signal it, and may not know the process exists. Setting a hold is a
statement about the *work*, not an offer to manage a Unix process. The dispatch
deadline therefore remains this pipeline's promise, and it still applies.

That yields one rule per question, with no special cases:

- **Escalate, replace the hold, or advance the stage?** No — workflow custody
  moved.
- **Kill an overdue engine whose identity is proven `matched`?** **Yes** — a
  runaway is a spend problem, not a decision. The kill advances nothing: the
  labels and the scratch are untouched, so the operator keeps every piece of
  evidence and the pending choice stays exactly where they left it. An engine
  whose identity is *unprovable* is still left alone, because its PID may have
  been reused and signalling it could hit an unrelated process.
- **Delete scratch while the group may live?** No — the marker is the only link
  to a process that would otherwise become untrackable and unkillable.
- **Release the pipeline's own in-flight label after proven death?** Yes — see
  below. That is the pipeline retracting its own receipt, not overruling a human.

**Proven group death is the one release condition,** and it releases two things:
the local scratch, into the ordinary archive/cleanup path, and the pipeline's
*own* in-flight label (`ai:reviewing` / `ai:implementing` / `ai:fixing`), via a
pure `--remove-label` that never names the hold. That label is an automation
receipt, not human custody; the admission slot is freed by destroying the
marker, not by removing the label, and leaving the label behind would strand
the item at a pipeline label no tick clears and no dispatcher picks
up — costing an operator's attention rather than a slot. The release is ordered
*before* the archive and confirmed by a read-back: if the removal or its
confirmation fails, the marker survives as the only thing that can re-prove
death on the next tick. The hold itself always remains for the operator to
resolve.

Cadence resolves holds with one `gh <kind> view --json labels` per occupied
identity, rather than scanning the repository's whole history of held items,
and a closed or merged item reads the same as an open one. A failed or
malformed hold read is unknown control-plane state: the loop stays warm and
does not claim quiescence.

Occupancy is the set of **local dispatch markers** — the engines this machine
launched and can still see — and nothing else. A marker holds its slot while
its classify state is engine-occupied *or* its process group is still alive,
so an engine that published its envelope with a child still running keeps the
slot it is still spending and still owes an archive.

An `ai:` in-flight label does **not** hold a slot. The label is a shared
workflow state, not a pipeline lease: `ai:implementing` is also the documented
manual pickup label, and a markerless in-flight label is a deliberate no-op
that no tick clears. Counting those as paid engines meant a hand-set label, or
one whose harvest relabel was missed, silently consumed a slot forever with no
dispatch marker behind it — four of them starved every dispatch on 2026-07-07
and again on 2026-08-20 until an operator diagnosed the count and
stripped labels by hand. The cap now
bounds this machine's concurrent spend, which is the thing it exists to bound.

This changes the ceiling, not per-item acquisition. Eligibility remains a
separate gate that reads the authoritative label set at the point of decision,
so an item another machine or a person already holds *at that moment* is still
skipped. It is not an exclusive lock and never was: dispatch runs lease →
launch → relabel, so two ticks that both select an item before either relabel
lands will both spawn a paid engine, and the loser terminates its own when its
relabel fails. That bounded acquisition race is unchanged by this section — see
the lease → launch → relabel step under "File-based handoff" in
`docs/architecture.md` for why closing it needs
compare-and-swap acquisition.

What is deliberately given up here is the sum across machines: another host's
engine no longer consumes this host's budget.

The in-flight read still runs, for two other jobs. In DISPATCH it is the
per-item *eligibility* input: each dispatcher skips an item already carrying an
in-flight label, and appends the engines it launches so all three share one
view of what this tick has taken. In the cadence observation it runs as a
health probe on a surface the queue check cannot see — it goes through GitHub's
search index while both queue probes go through the list API — and its value is
discarded. It is not what keeps a failed queue read out of a quiescence claim;
each queue probe already fails warm on its own. Stranded in-flight labels
surface on the status dashboard; they no longer cost dispatch.

Consequences for the loop:

- With nothing actionable and every occupied slot held, an untargeted loop
  reaches quiescence and stops after the usual heartbeat count on both paths.
  Targeted runs use the [target completion rule](#target-completion-and-cadence).
- One held slot out of `max_in_flight` does **not** stop a loop that still has
  actionable work; the other slots keep dispatching. If *every* slot is held
  while a queue is still actionable, the loop legitimately stays warm and cannot
  dispatch until the operator acts — so the cadence reason names the holders in
  that case too, not only on a stop.
- A human hold coexisting with an in-flight label keeps workflow custody with
  the human while the engine may live. The controller leaves the labels and the
  scratch untouched; admission counts the occupied dispatch identity while
  cadence discounts the held item.
- Once the process group dies, the marker classifies `stale` like any other,
  ordinary cleanup reclaims scratch and worktree metadata, the in-flight label
  is released, and the slot returns to the cap. The human hold is untouched.
- An item that reaches *markerless* scratch under a hold keeps its in-flight
  label: with no marker there is no PID or process group to probe, so death
  cannot be proven and the label is retained deliberately rather than cleared.
  It holds no admission slot — the marker that would have spent one is already
  gone — so the cost is a stranded item an operator must resolve, not a
  concurrency slot. Resolving the hold hands it back to ordinary stale-label
  reconciliation.
- Cleanup under a hold also clears the durable attempt counter, because custody
  genuinely moved to the human. An operator who holds and then releases an item
  therefore returns it to a full retry budget.

`tick-state:` reports the count as `held=N`, so an `in_flight=0` tick that
still cannot dispatch at the cap is explainable rather than mysterious. `held`
counts *slots*, matching `in_flight`, so the two always add up to occupancy —
a PR carrying both `ai:reviewing` and `ai:fixing` is two held slots, because
admission already treats it as two paid ones. The `tick-cadence:` reason
additionally *names* each held slot
(`; awaiting human decision: pr #42 ai:reviewing (ai:needs-human-decision)`) —
the one-shot `/loop` path has only that line to notify from, so neither a stop
nor a wedged cap caused by a human holding an engine can go unexplained.

### Engine selection (`--impl-engine`, `--fix-engine`)

```bash
~/.claude/pr-pipeline/bin/pr-pipeline tick --impl-engine claude-ds
~/.claude/pr-pipeline/bin/pr-pipeline tick --fix-engine claude-ds --fix-model deepseek-v4-flash
```

Controls whether the implementer uses `claude` (default) or `claude-ds`
(DeepSeek via Claude wrapper). The fixer runs on the implementer's route unless
`fix_engine`/`--fix-engine` names its own engine; `fix_model`/`--fix-model`
names its model. A blank fixer model inherits the implementer's model only while
the fixer runs on the implementer's engine, because a model id names one
provider's model. The reviewer always uses `codex`.

Each flag has a config key of the same name (`impl_engine`, `fix_engine`,
`fix_model`), so a repository's route needs no flags on every tick; setup
writes the probed implementer route there. A flag overrides the configuration
for one run, and the tick reports at startup when that run's implementer route
differs from the probed one (see [Configuration](#deadlines-stall-limits-routing-and-cadence)).

### Model override (`--model`)

```bash
~/.claude/pr-pipeline/bin/pr-pipeline tick --model claude-sonnet-4-6
```

Sets the implementer model (config key `model`), which the fixer inherits as
described above. When the resolved value is blank, the
`claude` engine gets **no `--model` flag at all**, so the CLI resolves the model
through its own settings precedence — upgrade your default and the pipeline
follows. Precedence is worth knowing, because it is not simply "your user
settings": a checked-in `.claude/settings.json` outranks `~/.claude/settings.json`,
and the fixer runs in a worktree of the PR head, so branch content can select its
model. Neither is set in this repo today, and the flag above outranks both — pass
it when a run must be pinned regardless of branch content. `claude-ds` still
defaults to `deepseek-v4-pro[1m]`, because the DeepSeek endpoint cannot resolve
Anthropic model aliases and so has no operator default to inherit.

When given, `--model` overrides the `--model` CLI flag for the `claude` engine
and additionally sets `ANTHROPIC_MODEL` for `claude-ds`.

### Implementer and fixer effort (`--effort`, `--fix-effort`)

```bash
~/.claude/pr-pipeline/bin/pr-pipeline tick --effort high --fix-effort medium
```

Sets the effort level that the implementer and the fixer receive as
`CLAUDE_CODE_EFFORT_LEVEL` (config keys `effort` and `fix_effort`). The allowed
levels are `low`, `medium`, `high`, `xhigh` and `max`; any other value stops the
tick at startup with an error that names the key. A blank `fix_effort` inherits
`effort` on any fixer engine, because effort levels are not specific to one
provider (a model id is, which is why `fix_model` inherits only on the same
engine).

When the resolved effort is blank, `claude` gets no `CLAUDE_CODE_EFFORT_LEVEL`
from the tick, so an exported value or the Claude settings apply as before.
`claude-ds` defaults to `max`; a set effort replaces that default. The
implementer and fixer `launch` log events record the effort the tick set in
`details.effort` (`null` when the tick set none).

### Reviewer model and effort (`--review-model`, `--review-effort`)

```bash
~/.claude/pr-pipeline/bin/pr-pipeline tick --review-model gpt-6 --review-effort xhigh
```

The reviewer is a separate provider from the implementer/fixer engines, so it
has its own knobs. Resolution order for each value independently:

1. the explicit flag, when given
2. `review_model` / `review_effort` in the pipeline's operator config
   (`[repos."owner/name"]`, then `[defaults]`)
3. `model` / `model_reasoning_effort` from the operator's codex config —
   `$CODEX_HOME/config.toml` when that variable is set, else `~/.codex/config.toml`,
   the same root `codex` itself resolves
4. the pinned fallbacks (`gpt-5.5`, `high`)

Profiles are not consulted: since codex 0.146 the legacy `profile = "…"` key is
rejected by codex outright, and profiles v2 apply only when `--profile` is passed
per invocation, which the pipeline never does. Top-level config *is* the
operator's effective default.

Every value — flag or config — is shape-checked for the sink it reaches. The
effort is interpolated into a `-c model_reasoning_effort="…"` TOML expression, so
it must be a bare token; the model is its own argv element, so provider-qualified
ids like `openai/gpt-5.1-codex` are fine but a leading dash (which codex would
read as an option) is not. A rejected flag or pipeline config value stops the
tick at startup, naming the key. A rejected codex config value is reported on
stderr and falls through to the pinned fallback rather than being swapped in
silence.

The reviewer cannot inherit this config the way the claude engines inherit
theirs. It runs under `env -i` isolation with `HOME`/`CODEX_HOME` pointed at a
per-PR scrub dir, plus `--ignore-user-config` — so the tick (the trusted
parent) reads the file and forwards the two resolved values as explicit flags.

**Exactly two keys ever cross that boundary**, and widening the set is a
security decision, not a convenience one. The reviewer is the one engine whose
entire input is attacker-controlled PR-head content; a real operator config
also carries `[mcp_servers]` (a node REPL is arbitrary code execution),
per-project `sandbox_mode`/`network_access`, hooks, and `notify`. None of that
may reach it. A missing, malformed, or wrong-typed config falls back to the
pins rather than aborting the dispatch or silently handing the review to
codex's own built-in defaults.

Each dispatch logs the values it resolved (`action=model_resolved`) so a
review's provenance survives a later config edit.

### Expressive reviewer reports

New reviewer dispatches pin report protocol 2 through the wrapper and harvest.
The [reviewer report contract](../../docs/working/reviewer-report-contract.md)
owns report fields, examples, compatibility, size limits and finding identity.
Full reports include reasoning, scope, detailed findings, verification, risks,
and optional observations. A valid human-decision report needs no invented code
finding. Invalid or oversized output is retained intact and produces an
escalation notice; it never starts a code fixer or counts as a completed round.

The archive contains `review.json`, `review-rendered.md`, and
`review-manifest.json`. Archive, post and relabel failures retain custody for
retry. Fixer dispatch authenticates the originating comment and pins stable
finding references and report/comment digests while passing its full text.
Ambiguous historical report formats require a human decision.

### Admission gate: issue authors

Only issues authored by the configured operator can enter implementation.
Both dispatch and cadence filter issue queues server-side with
`gh issue list --author <login> --limit 200`, then check returned author
metadata. Other authors cannot consume the scan budget or keep an otherwise
empty loop awake. A targeted run reads its issue directly and applies the same
predicate; targeting does not grant trust. Unreadable authors are refused at
dispatch but keep cadence warm because the queue state is unknown.

Other authors' issues are skipped without relabeling or escalation. After an
untargeted dispatch query, one additional number-only query (also capped at
200) reports `N available issues not authored by the operator.` in the tick
log under `issue_authors_filtered`. A saturated count read reports `N or more`.
If the operator page also saturates, only `0 or more` is provable without more
reads. Failed, malformed, or detectably inconsistent reads produce no count.
There is no author-widening setting in this release.

### Admission gate: reviewer policy files (`allow_touching`)

A PR whose diff edits files that govern the reviewer itself — `CLAUDE.md`,
`AGENTS.md` (at any directory depth), or operator-declared `checklist_paths` —
can steer the reviewer because it runs inside the PR's own worktree.
With operator-only issue pickup and reviewer admission, the packaged default
is `allow_touching = true`. To require human review for these edits, enable
the policy gate in the operator's own config
(`~/.claude/pr-pipeline/config.toml`, see [Configuration](#configuration)):

```toml
[defaults]
allow_touching = false

# or for one target only
[repos."owner/name"]
allow_touching = false
```

`false` escalates policy-editing PRs to `ai:needs-human-decision`. An absent
operator setting inherits the packaged `true`; a missing packaged floor or
mistyped `allow_touching` value falls back to `false`. Malformed operator TOML
refuses startup. Widening issue authors must restore the packaged default to
`false` in the same change. The human merge gate after `ai:ready-for-human`
is unaffected either way.

The packaged `config.toml` itself is part of the policy path set, as are the
gate's own implementation files (`lib/pipeline.py`, `lib/tick.py`) and
`resolve-target.sh`, which selects the repository whose policy is loaded: a PR that
flips the floor's `allow_touching`, rewrites `touches_review_policy` to return
`False` unconditionally, or removes the gate call from the reviewer dispatch is
escalated to a human (while the gate is enabled) instead of auto-reviewed —
otherwise a merged change would silently disable the gate for every future
policy-editing PR. These files are matched by path *suffix*
(`skills/tick/config.toml`, `skills/tick/resolve-target.sh` and the two `lib/` files), so the
self-protection holds wherever the skill is installed — under `.claude/skills/`
in a host repo, at `skills/` in the pipeline's own repository, or under a
plugin root.

The instructions the install supplies to a dispatched engine are in the same set
and matched the same way: every bound skill doc (`skills/<name>/SKILL.md` for
each `pipeline.BOUND_SKILLS` entry) and every prompt template under
`skills/tick/lib/prompts/`. Those are read from the install, so a PR can no
longer steer its own fixer with them — but a merged rewrite governs every
*future* run, which is the same silent-disable shape as a gate flip. Both sets
are derived from the constants the runtime actually reads rather than restated,
so a skill bound by a future stage, or a prompt added for one, is inside the gate
from the moment it lands.

### Prerequisites for `claude-ds`

Implementer and fixer engines are trusted local subprocesses and inherit the
operator's complete environment. This is the current MIP trust boundary: the
engines receive the same ambient credentials and capabilities as the terminal
that launches the tick. In cmux that includes `CMUX_SOCKET_PATH`; in another
terminal the engines inherit that terminal's authentication instead. The
reviewer remains separately isolated with its scrubbed environment.

When dispatching with `--impl-engine claude-ds`, set
`DEEPSEEK_API_KEY` in the shell that runs the tick, or set
`DEEPSEEK_API_KEYPATH` to a readable token file. Legacy environments may place an
`sk-*` token directly in `DEEPSEEK_API_KEYPATH`; an unmistakable token shape is
accepted as a value only when nothing exists at that path, while missing,
directory, dangling-symlink, permission, and other path-read failures fail
closed. `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are optional.

### Engine permission modes

Claude implementers and fixers default to `--permission-mode auto`. The existing
`permission_mode` key accepts only `auto` and `dontAsk`, with precedence:
packaged floor → operator `[defaults]` → `[repos."owner/name"]` → CLI flag.
Malformed values stop startup and name the key. For a repository whose route
fails the [setup route probe](#setup-route-probe), setup records the operator's
chosen fallback in the operator's config:

```toml
[repos."owner/project"]
permission_mode = "dontAsk"
verify_tools = ["./gradlew test *"]
```

`auto` lets calls needing approval beyond the explicit grants reach Claude's
classifier; it does not guarantee approval. A classifier denial can prevent
verification and fail the run. CLI flag acceptance does not prove that a
particular provider/model supports auto or can finish unattended. Tick passes
the selected mode without automatically changing it; setup's route probe
measures it. Changing the provider or model requires running setup again.

The narrow `--allowedTools` grants, including the stage's exact push command,
remain in both modes. Under `dontAsk`, calls that would prompt are denied.
Under `auto`, commands outside those grants, including force-push, remote-branch
deletion, and arbitrary refspec pushes, can reach the classifier. Use
operator-owned Claude deny rules for forbidden operations; omission from the
allowlist is not a denial rule. `/pr-pipeline:setup` offers deny rules for
force-push, `--delete`/`-d` and `:refs/...` deletion, `+` forced refspecs,
`--mirror`, and `--prune`. It installs them in the operator's Claude user
settings only on an explicit yes, where they also bind the operator's own
sessions. Short-form deletion (`git push <remote> :branch`) has no rule: a rule
ending in `:*` is Claude Code's legacy prefix form and would deny every stage's
own push. Implementers/fixers still inherit the operator's environment and
Claude settings; see [Engine settings](#engine-settings).

#### Engine settings

Headless `claude -p` engines load the operator's Claude settings hierarchy,
which is usually written for interactive sessions. The `engine_settings` key
gives engines a different layer without editing those files. Its value is
passed to `claude --settings`: a JSON object, or an absolute path to a settings
file. A relative path is refused because engines run inside a checkout of the
work under review. The file must be readable and hold a JSON object. Claude does
not reject a malformed settings file, so the resolver reads it. Tick startup
resolves config, so a file that is broken when a tick starts fails that tick
with `startup_config_invalid` before harvest or dispatch. Each dispatch
resolves config again before launch, so a file that breaks during a tick
escalates the item before any engine or lease. Blank, the default, omits the
flag.

`--settings` outranks the user, project, and local settings files, so it
overrides a scalar setting such as `sandbox.enabled`. Permission rule lists
merge across sources instead: engine settings can add deny rules but cannot
remove an allow rule from the user settings. Settings sources are not isolated;
user deny rules still apply to engines.

The common case is an operator whose interactive sessions enable the Claude
sandbox with `autoAllowBashIfSandboxed: true`. Engines inherit it, Bash runs
without the allowlist, and the setup probe reports `unmeasurable`. Record this
per repository to run engines unsandboxed:

```toml
[repos."owner/project"]
engine_settings = '{"sandbox":{"enabled":false}}'
```

The implementer, the fixer, and setup's probe build their argv with one
function, `claude_engine_argv`, so they launch with the same settings. Setup
records the probed value and `write-config` refuses a different one. It writes
the value to the repository section even when blank, so a nonblank
`[defaults]` value cannot replace the "no flag" the probe measured.

#### Setup route probe

Accepting `--permission-mode auto` proves nothing, and neither does a run that
merely starts: `auto` depends on the provider, model, and account, and a
classifier denial is not an approval. Setup therefore measures the route the
tick will use (`impl_engine`, `model`) for each repository, and `write-config`
records routing defaults and separate probe evidence in the repository's
section (see [the route metadata contract](#deadlines-stall-limits-routing-and-cadence)):

1. Compose the allowlist an implementer launches with: the engine base grants,
   the repository's confirmed `verify_tools`, and the exact push grant. Refuse
   to measure (`inconclusive`) if any entry grants the probe command.
2. **Negative control.** Launch `claude -p --permission-mode dontAsk` with
   dispatch's own argv: the same `--allowedTools`, the proposed
   `engine_settings` as `--settings`, no `--permission-prompt-tool`, and the
   prompt on a stdin pipe (no TTY). Ask it
   to run `git init` in a fresh throwaway directory. The command must be
   attempted and denied.
3. **Measurement.** Repeat under `--permission-mode auto`. The route passes
   only if the directory now holds `.git` and the harness's tool result
   contains the directory path, which the prompt never states.

`git init` is used because it is not granted, not read-only, and observable.
`pwd` and `git status` are not valid probes: on CLI 2.1.281 `pwd` runs under
`dontAsk` as a read-only command, and `git status` is a base grant. A test
fails if the base grants ever cover the probe command.

| Outcome | Meaning | What setup accepts |
|---|---|---|
| `pass` | Denied under `dontAsk`, run under `auto` | `auto`, including empty `verify_tools` |
| `auto_failed` | Control held; `auto` denied, rejected, failed, or timed out | Another route, or `dontAsk` with named grants |
| `unmeasurable` | Not denied under `dontAsk`: the host does not enforce the allowlist (for example sandbox `autoAllowBashIfSandboxed`) | Another route, `dontAsk` with named grants, or a new probe with [engine settings](#engine-settings) that disable the sandbox |
| `inconclusive` | Nothing measured: route cannot launch, control never attempted the command, or the probe command is granted | Another route, or `dontAsk` with named grants |

The operator chooses the fallback; setup never picks one. The result is
recorded per repository as `permission_mode` in `[repos."owner/name"]`, and the
probe evidence is kept under `setup-probes/` beside the operator config.
`write-config` refuses `auto`, or an empty `verify_tools`, without a passing
probe for exactly the grants and engine settings being written. It also refuses `dontAsk` with no
grants, because that combination strands an engine mid-run. The probe does not
settle what a denied command does to an otherwise healthy headless run (design
open question 12.3); it records only whether the command ran.

**Codex uses different controls.** On the locally inspected `codex-cli 0.155.1`,
`codex exec --help` exposes `--approve-for-me`: automatic review of approval
requests with a `workspace-write` sandbox. Its configuration uses
`approval_policy = "on-request"` and `approvals_reviewer = "auto_review"`.
Actions already permitted by the sandbox proceed normally; eligible requests
to cross its boundary go to a separate reviewer, which can approve or deny.
Denial can still stop an unattended task. See OpenAI's
[auto-review documentation](https://learn.chatgpt.com/docs/sandboxing/auto-review).

The older `--full-auto` is a deprecated compatibility flag for workspace-write,
not classifier review; see the [CLI reference](https://developers.openai.com/codex/cli/reference).
Neither flag is the full sandbox/approval bypass. The pipeline's Codex engine is
a reviewer, so it keeps `-s read-only`, `--ignore-user-config`, and
`--ignore-rules`; the Claude `permission_mode` setting does not apply to it.
Applying `--approve-for-me` would permit workspace writes and approval-based
escalation, changing that review boundary. No live Codex approval probe or
provider-capability claim is implied by the CLI inspection.

### Prompt-bound skills (`skill_unresolved`)

The fixer binds `review-response-hygiene` and `definition-of-done` through the
native Claude `Skill` tool before editing. Each finding receives its own
assessment, disposition, evidence and change or no-change reason. Re-check repair
versus replacement against the final diff. Replacements explain the new contract,
tradeoff and audited surfaces; replacement alone is not an escalation reason.
The [complete report contract](#complete-fixer-reports) owns the outcome fields,
verification requirements, publication and recovery rules.

The implementer prompt binds `definition-of-done` the same way. The engine runs
Phase A (claim, production entry point and authoritative mode, invariant owner,
shared boundary, exclusions) before its first edit and Phase B before its commit.
If the skill exempts the change, the engine records that decision before editing
instead of running either phase. After a relevant edit, rebase, or revision, it
re-runs the affected Phase B steps and updates the evidence before commit or push.
It puts the Phase B evidence block, or the skill's `Definition of done: n/a` line
for a change with no runtime claim, under `## Test coverage` in `.pr-body.md`.
`IMPLEMENTATION_COMPLETE` requires that block in addition to green verification
and a successful push. The target's own guides still name the verification
commands. If the engine cannot read required guidance or complete a required
Phase A or Phase B step, the prompt requires `NEEDS_HUMAN_DECISION`. Denied
commands must be recorded as `not run — <command> denied`; missing evidence
cannot be replaced by an n/a claim. The harvest checks only the body's section
structure. It neither validates the evidence nor adds a warning when the block
is missing, including when it publishes a generated fallback body.

Claude already discovers namespaced skills from plugins. Dispatch loads the
pipeline's installed plugin directly with `claude -p --plugin-dir <install>` and
preapproves the selected `Skill(pr-pipeline:<name>)` call in both `auto` and
`dontAsk`. The implementer invokes `pr-pipeline:definition-of-done`; the fixer
invokes both `pr-pipeline:review-response-hygiene` and `pr-pipeline:definition-of-done`. The prompt names the installed
source so a same-named task-branch skill does not satisfy the binding. No copied
plugin or second namespace is created. Supporting files remain in the install.

Required invocation or supporting-file access can still be denied by host policy;
the engine must report `NEEDS_HUMAN_DECISION`. There is no silent document-read
fallback. Verification commands retain their existing permission rules, and
skills do not grant publication or label authority.

The Codex reviewer has an isolated home. Dispatch links `native_skills.REVIEW_SKILLS`
into its `HOME/.agents/skills` and lists their names and paths in the prompt.
These installed skills are optional: the reviewer follows target `AGENTS.md`
routing and selects only applicable guidance. Control-plane, durability, and
cadence reviews apply to pr-pipeline changes. Plain catalog names avoid activating
every skill through a dollar-prefixed mention. No operator configuration, hooks,
plugins, or additional credentials are imported; the read-only sandbox remains.
The links share the existing reviewer scratch lifecycle.

Repeat commands for the opt-in real-engine checks are at the top of
`lib/test_native_engine_integration.py`. Run results belong in the PR. Full
classification of transitive guidance references remains
[issue #77](https://github.com/kwuBuild/pr-pipeline/issues/77).

Dispatch resolves mandatory installed documents before spending anything. A
doc that is missing, unreadable, blank or not UTF-8 text means the *install* is
missing a file its own prompt names — a packaging defect, not anything about
the item. The tick checks this once, after HARVEST and stale-label recovery and
before DISPATCH creates a worktree or changes a label:

| Stage | Installed docs it requires |
|---|---|
| Review | Every `native_skills.REVIEW_SKILLS` entry |
| Implement | `tick.IMPLEMENTER_SKILLS` (`definition-of-done`) |
| Fix | `tick.FIXER_SKILLS` (`review-response-hygiene`, `definition-of-done`) |

Only stages in the tick's target scope are checked. An `--only` tick has no
positional target, so it checks every stage after its list resolves. A stage with an unreadable
doc does not dispatch in that tick; the other stages dispatch normally. The tick
prints one `tick: dispatch blocked for <stages>: ...` line naming the failed
paths and logs one `skill_unresolved` entry with `pr: null` and
`details.blocked_stages`, `details.skills` and `details.probe_paths`. The status
line reads `PARTIAL (dispatch blocked: installed guidance unreadable)`, the
`tick-state:` status is `PARTIAL`, and the tick exits `2`. Eligible issues and
PRs keep their labels, so repeated ticks do not move the backlog to
`ai:needs-human-decision`. Recover by repairing the install — reinstall or
update the plugin, or check out the missing doc in a development checkout. The
next tick dispatches the eligible items with no relabeling.

The per-item launch guard remains as a backstop for an install that changes
between that check and a launch. If an item's own probe then finds an unreadable
doc, it logs `skill_unresolved` with the item number, escalates that item to
`ai:needs-human-decision`, and removes the worktree — no lease, no relabel to
`ai:fixing` or `ai:implementing`, no engine launched. After repairing the
install, relabel that PR back to `ai:changes-requested`, or that issue back to
`ai:available`.

### Governing context the fixer is handed

Every reviewer and fixer launch receives fresh context from the authenticated
controller. The controller reads the current PR title/body, each governing
issue's title/body and comments, PR conversation comments, submitted review
bodies, and inline review threads and replies. Each source retains its ID/URL,
author and creation/edit times. Inline threads retain path, line and commit
associations, resolution and outdated status. Comments and reviews are ordered
by creation or submission time within their source; replies stay in their thread.
Deleted authors remain explicit nulls.

Governing issues come from GitHub's fully paginated `closingIssuesReferences`.
When GitHub reports no links, as it does for a PR with a non-default base,
dispatch uses the existing primary-issue contract: one `Closes #N` declaration
under `## Summary`, in the target repository. If there is no unambiguous primary
issue, the issue set is empty. Dispatch does not scan other prose for closures
or require rendered HTML. The complete raw body remains available to the engine;
its separate conservative replacement-provenance check is unchanged.

Each reported reference must have a valid repository and positive issue number.
Dispatch validates all identities before reading issues, preserves discovery
order, and deduplicates by case-insensitive repository plus number. Same-numbered
issues in different repositories remain distinct. Missing repository identity
never falls back to the target repository. Other links and examples remain PR
context and do not become mandatory issue reads.

The controller follows every page of each connection, including replies within
each inline thread. Explicit null bodies are empty. Missing or malformed data,
GraphQL errors and failed pages are unknown. A failed acquisition or file write
logs its source and cause, leaves the pickup label unchanged, and launches no
engine. A later tick retries through the existing dispatch path; there is no
new retry counter or repair job. A successfully read empty issue set or empty
discussion is valid.

The prompt points to `discussion.json` in a new `.pr-pipeline-context-*`
directory inside the stage worktree. The file contains the complete fetched
text, including text beyond any inline excerpt. `bodyParts` and `diffHunkParts`
store ordered text chunks that concatenate without a separator. This keeps
individual JSON lines readable without losing whitespace or Unicode. Its internal `.gitignore`
excludes the directory's files from ordinary staging; existing scratch cleanup
and archival own its lifetime. Large selected reports and excerpts also move
to files to keep the launch command bounded. Engines must read the full files,
using multiple reads when necessary. A missing file blocks completion.

The fixer's PR-body and issue-contract excerpts retain the existing
`FIXER_CONTEXT_BUDGET` (20000 characters) and shortest-first budget sharing.
Every issue keeps its qualified heading. The full source remains available in
the discussion file. The existing PR-body replacement guard remains
conservative: when its inline body excerpt was truncated, dispatch still marks
that provenance as truncated and harvest declines a replacement.

Discussion is attributed task evidence, not instructions or permission to
change tools, credentials, policy, findings authority or completion rules.
The existing selected originating review and fixer report remain separate
inputs, selected under [comment trust and incomplete history](#comment-trust-and-incomplete-history).
Quoted markers do not become new
control records or reset review rounds. The engine assesses conflicting
comments against the code and governing requirements.

Freshness applies to each launch. A later launch reads edits and follow-up
comments even when the code head is unchanged. Comments posted during a run
enter the next launch. This adds no automatic re-review, context identity,
harvest freshness check, in-run access broker, credential grant or sandbox change.

### Authored implementation reports

The [shared PR report contract](lib/prompts/pr-body-contract.md) owns the template,
closure parser, presentation variants, and normalization rules. Both author
prompts receive its exact text. Harvest preserves the title independently of
body layout and publishes authored prose, evidence, and scope without semantic
rewriting or invented verification results.

| Initial report | Harvest outcome |
|---|---|
| Readable body with valid closure authority | Publish the complete content; missing non-control sections add one metadata/layout notice. |
| Missing or fenced primary reference, or an unrecognized Summary heading | Preserve the full report and prepend a marked controller issue reference using the dispatched target. Formatting alone does not escalate the harvest. |
| Absent, empty, or undecodable body | Publish a metadata-unavailable placeholder identifying the issue; preserve a separately readable title. Claim no verification results. The PR still enters `ai:implemented` for review. |
| Conflicting, ambiguous, or wrong primary closure; oversized content | Preserve the source and escalate to `ai:needs-human-decision` with the reason. Never truncate or infer a different issue. |
| Read, staging, or retention I/O failure | Keep source, marker, and attempt state; retry under the existing bounded harvest policy. |

Before publication, harvest retains original `.pr-body.md` bytes (when present)
and `decision.json` under
`.claude/worktrees/.pr-publications/issue-<N>-<dispatch-id>/`. The decision records
the title, body, outcome, validation reason, notice, normalization actions, and
whether harvest supplied a controller issue reference. The retained source is
the original report, including malformed formatting.
The worktree also holds `.pr-metadata-decision.json`; terminal escalation copies
it and the source into `.harvest-evidence/` before cleanup. These records survive
handoff; general retention policy remains separate work in #69.

If creation succeeds but a later label step fails, harvest rediscovers the PR.
It never replays the archived title or body over that PR. It adds a required
notice to the freshly read live body through the guarded update path, which
rechecks head, body, and labels before writing and preserves later human edits.
A required layout notice reflects the current live report's missing sections,
including partial or complete repairs made after creation with LF or CRLF line
endings. Leading controller notices follow the
[shared report contract](lib/prompts/pr-body-contract.md); embedded examples stay authored.
Implementation publication also adds a visible **Implementation evidence
missing** notice when Test coverage has neither the required definition-of-done
fields nor the explicit Scope exemption. This applies to authored reports,
generated placeholders, and rediscovered PRs, including legacy handoffs without
a retained metadata decision. The
[shared report contract](lib/prompts/pr-body-contract.md) defines the accepted
evidence forms and notice refresh rules. Retry uses the current live body and
preserves its title, authored content, and workflow labels. A failed or stale
body update retains custody for retry. Metadata/layout notices, missing-evidence
notices, and verification-grant warnings remain separate. None certifies the
meaning or quality of the evidence or grants approval.

### The PR body the fixer hands back

The fixer is instructed to leave GitHub metadata writes to the controller. It
writes a full replacement to `.pr-body-revision.md` in its worktree root and
lets the harvest publish it — the same engine-writes-file, controller-applies-it
shape as the implementer's `.pr-body.md`. Leaving the file absent is the normal
case and the prompt says so; it is written only when the body no longer describes
the head the fixer pushed.

For a present candidate, harvest retains the original `.pr-body-revision.md`
and its classification in `.pr-publications/pr-<N>-<dispatch-id>/` before any
publication or scratch cleanup. A retention failure keeps custody for bounded
retry. An absent candidate remains normal and never selects an implementation
placeholder.

The file carries a two-line envelope before the body. The first line binds it to
the **dispatch** (`pr-body-revision: <dispatch id>`), the second to the **commit**
(`pr-body-revision-head: <40-char OID>`). Two different lies need catching: a file
that rode in on the branch from an earlier run, and a file this engine really did
write but wrote before its last commit. The prompt therefore has the fixer write
the file *after* its final push, since only the push settles which OID the body
describes.

Every gate below stands between that file and the live body, because `gh pr edit`
replaces the whole body and GitHub gives the pipeline no undo. They run in the
order listed, so the reason published is the true one rather than the first one
that happens to match:

| Gate | What it rejects |
|---|---|
| Size | A file past `_PR_BODY_MAX_CHARS` (65,536), measured in characters *and* in UTF-8 bytes — GitHub does not document which it counts, so the stricter reading wins. The bound is applied to the `read` call, not to the string it returns, so a runaway engine file cannot exhaust the tick before being classified. A file present but unreadable is its own verdict again: neither is reported as a malformed body. |
| Stamp | A file whose first two lines are not this run's dispatch stamp and a head stamp. The fixer worktree is a detached checkout of the PR head, so an unstamped file rode in on the branch — a stray `git add -A` from an earlier run — and is not this run's work. |
| Provenance | A marker with no `pr_body` block (written before this path existed, or a PR whose body pinned no closure), or a live body the harvest could not read. Judging such a candidate on its content would report a perfectly good body as malformed. |
| Truncation | Any revision when the PR-body excerpt was cut short by the context bound. This conservative guard remains in place even though the full body is available in the discussion file. |
| Head | A head stamp that is not the PR's live head at harvest. A body written for an intermediate commit describes work that is no longer the tip. |
| Closures | A body with conflicting primary authority, or whose full set of closing directives (`closes`/`fixes`/`resolves` anywhere in the body, cross-repo and URL forms included) differs from the set pinned at dispatch after supplying a missing primary reference. The controller can prepend the pinned issue with a marked explanation when formatting omits or hides the primary line. Merging acts on every one of them, so a swap, a drop, or an addition changes what the PR does. Read from the body rather than from `closingIssuesReferences`, which GitHub leaves empty for a PR based on a non-default branch. |
| Presentation | Apply the shared PR report contract. Missing non-control sections add a notice; optional context and custom subsections survive. An empty candidate remains invalid. |
| Conflict | A live body that no longer matches the digest dispatch recorded — somebody edited it while the fixer ran. A live body that already *equals* the candidate is this run's own landed write, not a conflict, so a retry re-applies it harmlessly. |
| Outcome | Any result other than `recheck`. An escalated or requeued run may not have pushed the head the replacement describes. |

Those gates all read state the harvest captured earlier in the tick. Immediately
before `gh pr edit`, one more read re-establishes authority against GitHub as it
is *now*: the PR's labels, head, and body. A hold (`ai:blocked`,
`ai:needs-human-decision`) added since reconcile wins over a completed engine
result, and a head or body that moved since classification means the candidate was
judged against something else. Any of the three abandons the whole harvest for
this tick — no body, no comment, no relabel — and the scratch, marker, candidate,
and label are all retained so the next tick re-derives the outcome from what it
finds then. A hold does not count as a failed attempt: the attempt counter
escalates to `ai:needs-human-decision` at its cap, and escalating onto a human's
own hold is exactly what the hold revokes.

That last read narrows the unguarded window to a single API call; it does not
close it. GitHub's REST API offers no conditional (compare-and-swap) write for a
PR body — conditional requests do not apply to unsafe methods — so a human edit
landing inside that one call is still overwritten. The pipeline's claim is
"narrowed and fail-closed on anything observable", not "atomic".

The edit lands before the fix comment, and therefore before `ai:revised` — the
label that invites the next reviewer must never arrive over a handoff the
pipeline knew was stale. A failed `gh pr edit` takes the same bound as a failed
comment post: no comment, no relabel, scratch retained for the next tick.

Every refusal is named in the published comment as `- PR body: left unchanged —
<reason>`; only an absent file is silent. A body quietly left alone reads exactly
like a pipeline that never looked at it, and the next reviewer cannot tell the
difference. The tick log records the same verdict as `pr_body_revision`, and an
abandoned write as `pr_body_revision_withheld`.

The implementer needs no envelope of its own. It also writes `.pr-body.md` after
its final push, so the body describes the commits it published — but the harvest
uses that file to *open* a PR that does not exist yet, so there is no live body to
clobber and no undo to be missing. The reviewer runs read-only and pushes nothing
at all.

### Transient quota and limit results

Implementer and legacy fixer results without a valid completion sentinel are classified
as transient when the provider reports status 429, a known rate-limit subtype, or
a known session/usage/quota-limit message. A valid success or
`NEEDS_HUMAN_DECISION` sentinel remains authoritative even if the surrounding text
mentions a limit.

Transient results share an account-domain cooldown across implementer and fixer
dispatch. `claude` and `claude-ds` use separate domains so a Claude subscription
limit does not block DeepSeek-routed work. When the provider message includes a
reset wall-clock time, the cooldown ends `transient_cooldown_reset_buffer_seconds`
(shipped: five minutes) after the next occurrence; otherwise it lasts
`transient_cooldown_seconds` (shipped: one hour). Durable state lives in
`.claude/worktrees/.engine-cooldowns/`.

The retry boundary is `MAX_DISPATCH_ATTEMPTS = 3`:

1. Results one and two restore the dispatch marker's pre-label
   (`ai:available` or `ai:changes-requested`), increment the item attempt counter,
   and clean the completed scratch worktree.
2. Result three moves the item to `ai:needs-human-decision`, archives the engine
   evidence, clears the attempt counter, and cleans scratch.

The structured tick log records every decision:

| Action | Meaning |
|---|---|
| `transient_revert` | Cooldown persisted and the item requeued for a bounded retry |
| `cooldown_skip` | Implementer/fixer dispatch skipped while that provider domain is cooling down |
| `transient_exhausted` | Third transient result escalated instead of spending a fourth engine run |
| `cooldown_state_error` | Cooldown state could not be read or written; paid dispatch fails closed |

Genuine escalations and exhausted transient retries retain their source artifacts
under `.claude/worktrees/.harvest-evidence/` before the terminal label transition and
scratch cleanup. If the result cannot be archived, the in-flight label and scratch
evidence remain for a later tick. The
`engine_result_summary` tick entry carries the parsed sentinel, structured error
fields, and a bounded result excerpt.

Result-driven **implementer escalations** publish the stop reason, parsed sentinel
(or its absence), and a bounded excerpt of the engine's last message on the issue.
The comment also shows denied commands from `permission_denials`, the dispatch-pinned
permission mode and verification grants, and the local `.harvest-evidence/` path
on the machine that ran the tick. Old markers without these policy fields report
them as unknown; harvest does not substitute current configuration. Denials and
grants each show at most ten entries and 3200 source characters, with truncation stated.
Only tool names and command strings are included from denial records.

### Complete fixer reports

New fixer dispatches pin `report_protocol_version=2`, the exact originating
review digest, and the immutable qualified finding references from the reviewer
report. The shared authenticated-comment reader and legacy-review adapter select
that review. Ambiguous historical finding boundaries take a human hold before
launch. The existing discussion snapshot supplies current PR/issue requirements
and owner clarifications; there is no second context acquisition lifecycle.
Both Claude and Claude-DS receive installed revision-hygiene and definition-of-done
guidance through native skill binding.

The fixer returns one JSON object as its final response. The stream adapter
preserves that response in `result.json` and stamps the dispatch protocol version
beside it. The report repeats the dispatch ID, pinned head, published head and
original review fingerprint. Its dedicated `outcome` field selects the result;
completion tokens inside prose or code examples have no control authority.
The response fields are defined in `lib/fixer_reports.py` and explained in the
fixer prompt: revision summary, one assessment/disposition/evidence/change-or-reason
per finding, verification and status, PR/documentation updates, remaining work,
and optional Additional context. Prose can contain paragraphs, lists, code fences
and subsections. Mechanical validation checks identity, coverage and completeness;
it does not judge the correctness of a rationale or grant independent approval.

| Complete report state | Harvest disposition |
|---|---|
| All expected findings resolved, at least one repair/replacement, verification passed, and claimed pushed head matches an advanced live head | Publish the complete report and request independent re-review at `ai:revised`. |
| All expected findings have evidence-backed no-change dispositions, verification passed, and pinned head is unchanged | Publish the complete report and request independent re-review without a commit. |
| Human-decision outcome, including unresolved findings, unavailable verification or partial pushes | Publish the complete explanation and take `ai:needs-human-decision`. |
| Missing, duplicate or unknown references; malformed/incomplete report; contradictory outcome/head; oversized comment | Archive complete source and validation reason, publish a concise human-decision notice and hold. Never rerun implementation to repair prose. |
| Archive, comment, body-update or label failure | Retain source, marker and attempts; retry publication under the existing bounded custody rules. |

Successful outcomes retain the existing required-check and verification-waiver
rules. The author allowance comes from the reviewer's shared publication bound:
52,000 characters and UTF-8 bytes of authored content. The complete rendered
comment, including controller metadata, must fit both 60,000 characters and
60,000 UTF-8 bytes. Harvest preserves Markdown/code fences and neutralizes only
reserved controller-marker openers. It never truncates or splits a new report.
Oversized source and the full attempted rendering remain in the evidence archive.

Every new run archives `fix-rendered.md`, `fix-manifest.json`, dispatch/prompt
evidence and complete scratch before cleanup. It preserves `result.json` when
present and records its absence otherwise. The manifest records
source/rendered fingerprints, originating review references, published head and
validation reason. A report-less dead fixer can already have pushed; it takes a
report-failure hold instead of replaying code work. Process-tree death and the
existing result authority still gate harvest; fixers do not use reviewer envelopes.

Comment receipts retain the existing at-least-once publication contract: retries
with a receipt suppress duplicates, while a crash after GitHub accepts a comment
but before its receipt is saved can publish one duplicate. Head/hold checks run
again before new-report publication and re-review transition. PR body replacement
keeps its existing provenance, closure, concurrent-edit and pushed-head guards.

An accepted no-change comment carries a stable dispatch-derived
`pr-pipeline:no-change` token. Each new no-change round counts toward
`max_iterations`, even on the same commit. Retries preserve that token. Both
pushed and no-change reports carry the pinned `pr-pipeline:fix-review` fingerprint.
Re-review receives the complete published report and exact original review. A
missing or edited original for any new report retains the PR for retry. Restore
the exact comment or explicitly requeue a fresh review at `ai:implemented`.
After a report-failure hold, use the existing operator recovery workflow; no
automatic report-repair engine is launched.

Only already-running dispatches without a protocol version use legacy sentinel
and no-change validation. Their comments include a visible legacy-evidence note;
the old 3,000-character summary and bounded sanitized excerpt remain compatibility
behavior. A malformed new result can never select that fallback.

### Comment trust and incomplete history

The tick uses the operator login validated at startup to authenticate PR
comments. Before reviewer/fixer dispatch and reviewer harvest, it reads the
GraphQL comment connection twice. Each traversal must have unique comment IDs,
valid page boundaries, and a stable total equal to the number of returned records.
The two complete traversals must match. A detected deletion, edit, or replacement
holds the PR for a later tick. These checks detect changing history; they do not
provide an atomic GitHub snapshot or cover a change after the final read.

Only unedited comments by that operator, with an exact marker on the first line,
can supply controller metadata or review replay proof. The same published review
token counts once; older trusted
comments without a round token still count individually. LF and CRLF separators
are accepted; header whitespace is not. Raw bodies stay unchanged for fingerprints.

Fixer input is one approved source, selected as follows:

1. An *approved input* is a PR conversation comment or a submitted review body
   from the operator login or a login in `approved_reviewers` (compared without
   case). Submitted reviews must be `CHANGES_REQUESTED` or `COMMENTED`;
   pending, dismissed, and approval reviews do not start fixes. Inline threads
   remain discussion context, not separate fixer inputs.
2. An approved input can have no marker or one exact leading
   `<!-- pr-pipeline:review -->` marker. Any other pipeline marker, including a
   quoted marker or controller metadata supplied by another approved author,
   disqualifies it. Existing operator-authored pipeline review reports retain
   their controller finding set.
3. Operator conversation comments with control markers are *pipeline actions*.
   The newest approved input after the last action wins. Without one, the
   latest operator pipeline review is the fixer input. Submitted reviews use
   `submittedAt`, not draft creation time; comments use `createdAt`. A pipeline
   action wins a same-timestamp tie with a submitted review.
4. Each approved input is one finding, `C1`. Dispatch pins both its body SHA-256
   and the SHA-256 of `id:<GraphQL source ID>`. Harvest publishes the latter as
   `pr-pipeline:approved-input` in its controller header. Each distinct source
   counts as a round even if its text repeats an earlier input. Publication
   retries count once, and source deletion cannot erase the published round.
   Existing body-only pins retain their historical counting rule. Operator
   review-marker comments already counted as reviews are not counted twice.

Fixer selection and resume read both comment and review connections twice,
with the same complete-pagination and stable-snapshot checks described above.
An approved input needs explicit unedited provenance, or a valid edit timestamp
and a known editor matching its author. Unknown or malformed edit provenance
blocks progress; an edit by another user is not eligible. Operator conversation
comments retain the stricter unedited-only rule below. Submitted reviews never
supply controller metadata, even when authored by the operator.

A newer approved input also releases a hold. Each tick lists open
`ai:needs-human-decision` PRs (or the `--only` PRs). With no other workflow label
except `ai:changes-requested`, no dispatch marker, and an unused approved input,
the tick checks `max_iterations` before replacing the human hold with
`ai:changes-requested`. Every direct fixer dispatch, including one selected from
a pipeline review, applies the same budget check. Reviewer harvest uses the same
combined round count before choosing the next label.
At the cap, the tick sets `ai:max-iterations` and launches no fixer. This covers
repeated fix → human decision → reply cycles and subsequent re-review. An approved
candidate uses an available round; its optional review marker does not make it a
previously spent round. Historical controller reviews retain their existing count.
`ai:blocked`, `ai:max-iterations`, and in-flight labels keep the hold. The loop
does not wake for a comment; resume occurs on the next tick that runs.

Before launch, dispatch records the source ID in
`WT_ROOT/.receipts/pr-<N>-approved-inputs.json`. Resume refuses a consumed input.
The budget counts each source once across receipts, published fix headers and
operator review comments. Receipts without published fix headers remain counted,
so silent or resultless outcomes cannot reset spending on this checkout. A receipt
for the current input without a published result can retry dispatch after a pre-launch
crash; the existing dispatch-attempt cap still applies. A failed receipt write
skips launch, and an unreadable receipt blocks resume, all fixer admission and
reviewer harvest while preserving custody. Receipts are local: another checkout
cannot reconstruct silent attempts, while published source pins remain available
through GitHub.

GitHub's `lastEditedAt`, `editor`, and `includesCreatedEdit` must explicitly report
no edit on every operator-authored comment. Original authorship and matching REST
timestamps do not authenticate the current body. An edited operator comment holds
processing even when its marker is gone, since ignoring it could erase a review
round. This also holds ordinary operator notes and operator-made edits. Authors outside the approved set cannot direct fixes. Only the operator
can supply controller metadata.

Controller-history accounting logs non-operator entries as
`comment_authors_ignored`; `fixer_input` identifies the input actually selected.
If a fixer has neither a trusted review nor a newer
approved comment, setup escalates to `ai:needs-human-decision` without launching
an engine. An approved comment with missing edit provenance logs
`comment_history_refused` and holds the PR. Missing/malformed authors,
missing edit provenance, edited operator comments, malformed bodies or pages,
changing histories, and failed page reads are different: the tick records
`comment_history_refused`, retains the current label and existing evidence, and
launches no reviewer or fixer. Reviewer harvest cannot promote or reset rounds
from that incomplete history. Restore access and retry a transient read or race.
An edited operator record needs manual review and disposition; do not delete or
repost it as a way to reset the recorded rounds. The tick has no automatic edit
waiver. A missing author or edit record must not be treated as zero genuine reviews.

Review publication uses authenticated comment history for replay. Fixer and
escalation publication use the existing controller-owned receipts under
`WT_ROOT/.receipts/`; remote `FIX_MARKER` or `ESCALATION_MARKER` text cannot forge
those receipts. Fixer comments are read only for authenticated re-review context.
This boundary does not distinguish two actors using the same operator credential.

### Escalation publication coverage

The following map records the harvest and dispatch publication paths. The silent
paths below remain separate work under distribution design section 15; adding
implementer reporting does not change their escalation conditions.

The publication requirement covers a transition that reports an engine result,
or fires before one was launched. The map distinguishes implemented posts from
the remaining gaps. These comment-bearing transitions post before the label moves. Posts gate the
transition except where the table states a bounded or best-effort exception:

| Terminal transition | Post gates the relabel | On post failure |
|---|---|---|
| implementer result `escalate` → `ai:needs-human-decision` (`_harvest_one_implementer` → `_complete_escalate_transition`) | yes, and bounded | label held, scratch retained; repeated post failure reaches the attempt cap |
| protocol 2 reviewer human-decision, invalid, or oversized result → `ai:needs-human-decision` | yes, and bounded | source and manifest retained; notice and terminal transition retried |
| reviewer verdict → `ai:max-iterations` (`_harvest_one_reviewer`) | yes, and bounded | verdict post retried; repeated failure reaches the attempt cap |
| fixer `recheck` → `ai:revised` | yes | label held, scratch retained, retried next tick |
| fixer `escalate` → `ai:needs-human-decision` | yes | same |
| transient retries exhausted → `ai:needs-human-decision` | yes, and bounded | label held; the failure burns an attempt, so repeated failure walks into the cap |
| hold beside a completed result → in-flight label released, hold retained | yes, bounded by the hold itself | nothing moves; marker, result, scratch, and attempts retained, retried next tick |
| dispatch setup failure → `ai:needs-human-decision` | yes, and bounded | label held at the pickup label and the scratch removed — no engine ran, and a retained lease would be reconciled to the terminal label next tick; the failure burns an attempt |
| PR touches reviewer policy → `ai:needs-human-decision` (configurable via `allow_touching`, see "Admission gate" above) | yes, and bounded | same |
| ambiguous marker → `ai:needs-human-decision` | yes, and bounded | label held, but the scratch is **retained** — this path cannot prove the marker safe to clear; dispatch stays blocked either way |
| attempt cap → `ai:needs-human-decision` | **no — best effort** | label lands unexplained; logged `escalation_comment_failed` |
| transient attempt-counter write failure → `ai:needs-human-decision` | **no — best effort** | same |

**Two paths are exempt from the gate by design**, both for the same reason: they
exist to stop an item pinning an admission slot forever, so they cannot be blocked
on one more remote write.

- The **attempt-cap** escalation fires because this item's remote writes keep
  failing; gating it on another would reinstate the stall the cap prevents.
- The **attempt-counter write failure** inside the transient path escalates
  because the retry budget itself could not be persisted. With no budget to
  bound it, a gated transition could never terminate, so its post is best-effort
  too and a failure is logged as `escalation_comment_failed`.

Transient *exhaustion* is gated, because that one can be bounded: a failed post
increments the attempt counter, and once the counter reaches the cap the item
lands `ai:needs-human-decision` under the cap's own superseding comment.

The **held completion** is gated on a different bound. It burns no attempt, so a
persistently failing post retains full custody — marker, scratch, and in-flight
label — until it lands, and it is the retained *marker* that keeps one admission
slot occupied; the label alone never does. That is acceptable only because the
item is already awaiting a human by definition: the operator who set the hold
resolves it, the next tick then harvests the completion through the ordinary
path, and clearing the marker there is what frees the slot. The trade is deliberate: releasing custody on a failed post is what once left
a PR merged under an unexplained `ai:needs-human-decision`. If a post can never
succeed — a locked or archived item — the operator's own hold removal is the
release, not another tick.

The **pre-launch dispatch escalations** in the table above are gated and bounded
the same way, against the same counter. Nothing is spent when they fire — no
engine, no admission slot — so a failed post costs only a held label, and the
item sits at its ordinary pickup label until the next tick retries. That is also
why they need the bound: a permanently unpostable comment would leave the item
looking like available work forever, so once the counter reaches the cap the
label lands unexplained and the miss is logged as `escalation_comment_failed`.

A held escalation also takes its scratch with it. Setup failed before launch, so
any lease it wrote is a phantom no engine owns — and since a tick harvests before
it dispatches, leaving one behind would let the next tick reconcile it straight
to `ai:needs-human-decision` with no comment instead of retrying the post.

When the label does land, the counter is cleared so a later cycle for the same
item starts fresh — except on the ambiguous-marker path, which retains the
marker and therefore leaves the counter to whoever still owns it.

Exactly two functions run `gh … comment`. The publication owners are
`_post_harvest_comment` (implementer/fixer result,
cap, transient, held-result, and dispatch comments) and `_harvest_one_reviewer`
(review verdicts). `_publish_dispatch_escalation` serves setup failures,
review-policy checks, and ambiguous markers. `_complete_terminal_escalation`
owns the attempt-cap comment; `_complete_transient_exhaustion` owns transient
exhaustion, and `_handle_transient_engine_result` publishes counter-write failures;
`_publish_held_result_and_archive` owns held results. These paths already explain their outcomes and need no additional
report in this change.

Implementer reports reuse the terminal intent and controller-owned receipt. A
failed label update or archive move resumes the same report without reposting a
receipted comment. This follows the existing at-least-once contract: a process
crash after GitHub accepts the post but before the local receipt is written can
still produce a duplicate. A missing receipt does not prove that publication
failed. Once a report post is attempted, failed reposts, label updates, and hold
releases retain its evidence directory, including after the attempt-cap handoff.
Retries reuse that same directory until archive finalization succeeds. A report
names the directory only after its `.prepared` marker exists, so a crash during
preparation cannot publish an empty or partial evidence path. See
[Retained scratch and harvest evidence](#retained-scratch-and-harvest-evidence)
for cleanup ownership.

Every other terminal transition publishes nothing.

### Silent transitions

The following gaps remain owned by the tick maintainers as follow-on publication
work under [distribution design section 15](../../docs/working/plugin-distribution-design.md#escalation-legibility-is-a-publication-requirement).
They require their own reasons and recovery contracts; the implementer result
renderer does not cover them.

| Runtime owner | Silent escalation | Evidence and consequence |
|---|---|---|
| `_harvest_one_reviewer` → `_complete_escalate_transition` | Legacy dispatch with invalid verdict, or legacy changes requested with no blocking finding | Archived review result; the PR has no explanation for the validation failure. Protocol 2 reports use the published escalation path above. |
| `_harvest_failed_reviewer` | Wrapper-confirmed reviewer failure | `.harvest-evidence/` bundle; the PR has no explanation for the engine failure. |
| `_reconcile_one` → `_execute_reconcile_actions` | Launch timeout, hung engine, unknown live process identity, conflicting pipeline labels, or resultless dispatch attempt cap | Tick log and retained scratch when custody requires it; no published reason. Reconcile does not require a readable result and cannot use the implementer report. |
| `_dispatch_one_implementer`, `_dispatch_one_fixer`, `_dispatch_one_reviewer` | In-flight label write fails and engine termination also fails | Tick log and retained marker/scratch; the terminal label prevents duplicate dispatch while an engine may still run. |

A hold found beside a completed result is covered by the gated held-completion
row above. It publishes the validated outcome before releasing the in-flight
label. The remaining silent paths are gaps, not exemptions from the publication
requirement.

**Recovering the reason for an unexplained hold.** An item at
`ai:needs-human-decision` with no comment usually means an engine was launched
for it — but not always, so check the cheapest disqualifier first:

1. The tick log at `.claude/worktrees/.tick-log`, filtered to that item's number.
   An `escalation_comment_failed` entry means the comment is missing because it
   could not be posted, not because the path is silent — the reason is in that
   entry, and no engine need ever have run. The tick log also records the
   silent reconcile and post-launch failures in the table above.
2. Its scratch under `.claude/worktrees/`. A retained scratch with a marker means
   possible live custody — read `dispatch.json` for the engine's identity and check
   whether that process is still alive before touching anything.
3. `.harvest-evidence/` for that item's bundle. A bundle exists only when a
   result or other scratch evidence was retained. Inspect its `dispatch.json`,
   result, and stderr files to determine how far the run progressed.

An empty `.harvest-evidence/` does **not** mean nothing ran — it means no engine
result was retained. And with no `escalation_comment_failed` entry for the item,
it does rule out a dispatch that never launched: every pre-launch escalation
whose comment landed says so on the item itself.

## Three observability surfaces

You have three independent surfaces, each serving a different purpose.

### 1. Dashboard — `status.sh` (real-time, human-readable)

Run in a watch loop in a separate terminal pane:

```bash
# Primary (Linux / watch installed):
watch -n 5 '~/.claude/pr-pipeline/bin/pr-pipeline status'

# Fallback (macOS / no watch):
while true; do clear; ~/.claude/pr-pipeline/bin/pr-pipeline status; sleep 5; done
```

Read-only — safe to run alongside a live tick. It reads GitHub labels, worktree
markers, and tick logs; it never writes, edits labels, or touches engines.

#### DISPATCH SCOPE

The first line under the header names the newest tick's dispatch scope and
how current that evidence is:

```
DISPATCH SCOPE: --only #439,pr:468 (dispatch scope #439,pr:470,pr:468) · log evidence active: last tick 14:32:15, next due by 14:36:45
```

The dashboard cannot see the loop process, so the banner reads only retained
tick logs. The scope is `full queue`, `target #N (scope …)`,
`--only <list> (dispatch scope <expanded items>)`, or
`--only <list> (scope UNREADABLE at last tick; no engine started)`.

Every tick that takes the lock logs `tick_scope_requested` right after its
startup context, before any startup check. When a list or target tick stops before it resolves its scope
(a startup refusal, a target resolution failure, or a failed harvest), the
banner shows `--only <list> (requested; scope not resolved; no engine
started)` or `target #N (requested; …)`. Only a `tick_scope_requested` record
with mode `full` shows `full queue`. A tick log with no scope record shows
`scope unknown (no scope record)`; the banner never reads missing evidence as
the full queue. Logs that hold only a `lock_held` refusal or a loop-stop
notice are skipped as ticks.

| Evidence | Meaning |
|---|---|
| `active` | The newest tick completed and its advertised next wake, plus 120 s, has not passed; or a tick started within 30 minutes and is still running |
| `ended` | A `loop_only_stop`, `loop_target_stop`, or `loop_quiescent_stop` followed the tick, or its cadence reported `stop=true` |
| `STALE` | The next wake is overdue with no newer tick, the tick never completed, or it reported no cadence. The scope shown is history, not a running loop |

#### IN-FLIGHT ENGINES

One row per live `dispatch.json` in the worktrees:

```
IN-FLIGHT ENGINES
  PR#468   review    codex     pid=12345  alive  result-ready   12m-to-deadline
  PR#469   implement claude-ds pid=12346  alive  working        47m-to-deadline
```

| Column | Meaning |
|---|---|
| `PR#468` | PR number (or `?` if marker is malformed) |
| `review` / `implement` / `fix` | Pipeline stage |
| `codex` / `claude-ds` / `claude` | Engine+model dispatched |
| `pid=12345` | Process ID (so you can `htop` or `kill` it) |
| `alive` / `DEAD` | `kill -0` check — DEAD means the process exited or crashed |
| `working` / `result-ready` | Whether a non-empty result file has landed — implement/fix engines redirect stdout into `result.json`, so an empty one means nothing has been written yet |
| `12m-to-deadline` | Minutes before the hung-engine timeout fires |

This is your **triage pane**. If you see `DEAD` + `working` + negative deadline, an
engine crashed silently — check the tick log for that PR's last action.

The rows come from *this machine's* marker root only. An engine dispatched from
another machine has no row here and never will, so the absence of a row is not
evidence that nothing is running — see
[Markerless in-flight items](#markerless-in-flight-items--why-the-tick-leaves-them-alone).

#### PIPELINE STATE

All open PRs and all open issues with `ai:` labels, grouped by label, in two
sections:

```
PIPELINE STATE (open PRs by ai: label)
  ai:reviewing          #468   Implement memory retrieval
  ai:ready-for-human    #467   Update auth middleware

PIPELINE STATE (open issues by ai: label)
  ai:available          #439   Add streaming support to chat endpoint
  ai:implementing       #438   Fix pagination offset bug
```

This is your **queue view** — what's waiting, what's in progress, what's ready for
you. Both listings cover every `ai:` label, not just the dispatchable ones: since
reconciliation never escalates an in-flight item on a missing marker, this is the
only place a stalled `ai:implementing` becomes visible. Visible is not the same
as diagnosed — these labels are shared across every machine, while the engine
rows above are not, so pairing them tells you an item *may* need attention, never
that it is unowned.

#### LAST TICK AUDIT LOG

The structured decisions from the most recent tick:

```
LAST TICK AUDIT LOG
  14:32:15  PR#468   harvest    review     reconciling        marker+label matched,live
  14:32:15  PR#468   harvest    review     skip_live          still live
  14:32:15  PR#-     dispatch   implement  launch             impl-1748321535-12345-439      claude-ds
  14:32:15  PR#468   dispatch   review     launch             d-468-1748321535-12345         codex
```

| Column | Meaning |
|---|---|
| `14:32:15` | Timestamp |
| `PR#468` | PR number (`-` = not PR-specific) |
| `harvest` / `dispatch` | Tick phase |
| `review` / `implement` / `fix` | Pipeline stage |
| `launch` / `skip_live` / `skip_no_token` | Action taken |
| `impl-1748...` / `still live` | Reason (dispatch ID or explanation) |
| `claude-ds` / `codex` / `claude` / *(blank)* | Engine used (blank for harvest entries) |

This is the **audit trail** — replay the tick's decisions without re-reading SKILL.md.

### Local event feed — `events`

Run this in the target repository, or one of its linked worktrees:

```bash
~/.claude/pr-pipeline/bin/pr-pipeline events --follow
~/.claude/pr-pipeline/bin/pr-pipeline events '#27'
~/.claude/pr-pipeline/bin/pr-pipeline events --json pr:81
```

The feed shows retained local progress and confirmed handoffs. It reads logs
without changing pipeline state, calling GitHub, or invoking a model. Following
it in a terminal adds no model-token usage. It does not supervise an AI session
or change desktop notifications. Existing alerts remain in place.

History is bounded by tick-log retention and may lack old issue-to-PR links.
An engine result is an observation; a proposed PR creation is not confirmation
that the PR opened. Scripts should use the versioned JSON-lines format, not
parse human text. See [Event feed contract](../status/EVENTS.md) for event kinds,
filters, follow/restart behavior, failure handling, and the output schema.

### 2. Tick log — `.claude/worktrees/.tick-log/` (structured, queryable)

Each tick writes one JSON-lines file (`tick-<timestamp>.jsonl`). One line per decision:

```json
{
  "ts": 1748321535,
  "pr": 468,
  "phase": "dispatch",
  "stage": "implement",
  "action": "launch",
  "reason": "pid=12345",
  "engine": "claude-ds",
  "dispatch_id": "impl-468-1748321535",
  "details": {"head_sha": "abc12345"}
}
```

This is your **query surface** for answering historical questions:

```bash
LOG_DIR=".claude/worktrees/.tick-log"

# All claude-ds implementer dispatches
jq 'select(.engine=="claude-ds")' "$LOG_DIR"/tick-*.jsonl

# Engine dispatch counts across all ticks (not token usage)
jq -s '[.[] | select(.action=="launch")] | group_by(.engine) | map({engine: .[0].engine, count: length})' "$LOG_DIR"/tick-*.jsonl

# Correlate dispatch → harvest for a single engine (cross-tick trace)
jq -s '[.[] | select(.dispatch_id=="impl-468-1748321535")] | sort_by(.ts)' "$LOG_DIR"/tick-*.jsonl

# Skill files observed during completed reviewer runs
jq -s '.[] | select(.action=="reviewer_skill_observation") | {pr, dispatch_id, details}' "$LOG_DIR"/tick-*.jsonl

# Admission gating: why were engines skipped?
jq -s '.[] | select(.action=="skip") | {pr, reason, details}' "$LOG_DIR"/tick-*.jsonl

# All dispatch skips and why (debug stalls)
jq -s '.[] | select(.action|startswith("skip_")) | {pr, action, reason}' "$LOG_DIR"/tick-*.jsonl

# Retained scratch, process/metadata cleanup failures, and stale-label reconciliation
jq -s '.[] | select(.action=="retained_scratch" or .action=="archive_failed" or .action=="kill_failed" or .action=="worktree_prune_failed" or (.action|startswith("stale_label_"))) | {pr, stage, action, reason, details}' "$LOG_DIR"/tick-*.jsonl

# Ticks that ran today
ls "$LOG_DIR"/tick-$(date +%Y%m%d)*.jsonl 2>/dev/null | wc -l
```

#### Skill observations

Status reads the existing audit log. Dispatch `skill_context` rows record
`requested_skills` and `available_skills`; availability is not proof of use.
Claude `engine_result_summary` rows retain `skills_used` and `skill_usage_status`.
The collector records at most 64 names from native calls with matching successful
tool results. Older wrappers recorded attempted calls, so historical rows must
not be reclassified as successful loading. Neither form proves compliance.

Codex keeps its existing schema version 2 `review-observation.json`; version 1
remains readable. Its normalized names describe successful commands referencing
skill files, including the installed discovery links classified as `plugin`.
Even an `echo` of a path qualifies; these are not native invocation events.
Raw commands, output, and paths are discarded.

Empty observations mean use is unknown. Missing observations are unavailable.
Claude prompt expansion may emit no `Skill` call. Observations never determine
completion, verdicts, labels, or cleanup. Detailed cross-engine read/attempt
classification is outside this feature.

#### Per-attempt token usage

`engine_result_summary.details.usage` (Claude implement/fix) and
`reviewer_skill_observation.details.usage` (Codex review) contain the same small
record: `status`, `source`, `token_unit: "tokens"`, `input_tokens`,
`cached_input_tokens`, `output_tokens`, allowlisted `native` counters, and optional
`reported_cost: {amount, unit: "USD", source}`. Missing or invalid numeric values are
`null` in normalized fields and absent from `native`, never invented zeroes.
`complete` means the three comparable counters are present and valid;
`partial` preserves usable values with missing/invalid counters or degraded
collection; `unavailable` means no usable token/cost report. Availability is
independent of engine success. Explicit provider zeroes remain zeroes.
Counters must be nonnegative integers within signed 64-bit range; cost must be
finite, nonnegative and at most USD 1 trillion. Boolean and numeric-string
values are rejected. Contradictory Codex subcounts also make usage partial.

Provider semantics are deliberately retained:

| Source | Input total | Subcounts and limits |
| --- | --- | --- |
| Claude terminal `result.usage` | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` | Cache reads are cached input. Nested cache-creation TTL counts are excluded because their parent already counts them. |
| Codex terminal `turn.completed.usage` | `input_tokens` | `cached_input_tokens` and optional `cache_write_input_tokens` are input subcounts; optional `reasoning_output_tokens` is an output subcount. None is added again. |

These are cumulative token-processing reports for the engine invocation/turn,
not the size of its current context window. Claude's result `usage` describes
the main agent loop; its `total_cost_usd` can include subagents and is a
provider-reported estimate, not proof of invoiced charges or subscription quota
consumption. Error results can underreport or report zero after a crash. This
feature does not reconstruct missing usage or aggregate `modelUsage` on top of
`usage`. See [Claude result accounting](https://code.claude.com/docs/en/agent-sdk/cost-tracking),
[Claude cache semantics](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
and the [Codex terminal schema](https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs).

The Codex collector keeps the last terminal snapshot without summing events.
Multiple terminals make usage `partial` because no stable event identity is
available to aggregate them safely. Oversized/malformed events retain the
existing stream bounds. Version 1 observations still supply skill evidence but
have unavailable usage. Unknown counters, raw events and tool payloads are not
copied. None of this evidence participates in completion validation or labels.

Run this read-only query from the target repository for one row per retained
stage/dispatch attempt. It selects one usage snapshot even if harvest logs it
again on retry, and keeps separate dispatch IDs separate. Launches without
usable terminal evidence appear as unavailable. Records lacking dispatch IDs
cannot be assigned safely and are excluded. Log retention limits historical
coverage; a missing log does not prove zero usage.

<!-- engine-usage-report -->
```bash
LOG_DIR=".claude/worktrees/.tick-log"
jq -s '
  [.[] | select(.action | IN("launch", "model_resolved", "engine_result_summary", "reviewer_skill_observation"))
       | select((.dispatch_id | type) == "string" and .dispatch_id != "" and .dispatch_id != "unknown")]
  | group_by([.pr, .stage, .dispatch_id])[]
  | select(any(.action != "model_resolved")) | sort_by(.ts) as $rows
  | ($rows | map(select(.details.usage?))
     | max_by([({unavailable: 0, partial: 1, complete: 2}[.details.usage.status] // 0), .ts])) as $evidence
  | ($rows | map(select(.details.model? != null)) | last | .details) as $route
  | {pr: $rows[0].pr, stage: $rows[0].stage, dispatch_id: $rows[0].dispatch_id,
     engine: ($rows | map(select(.engine != "")) | last | .engine),
     model: $route.model, effort: $route.effort,
     reported_models: $evidence.details.reported_models,
     engine_reported_turns: $evidence.details.num_turns,
     usage: ($evidence.details.usage // {status: "unavailable", input_tokens: null, cached_input_tokens: null, output_tokens: null})}
' "$LOG_DIR"/tick-*.jsonl
```

`model`/`effort` are dispatch-supplied settings when recorded; an inherited
Claude default is `null`. `reported_models` lists bounded model identifiers
actually present in Claude's terminal `modelUsage`, which may include more than
the primary model. Missing provenance stays `null`, including when its dispatch
log has expired. `engine_reported_turns` is Claude's `num_turns`, not a model/API
call count; the latter is not reported here. Engine names identify the route
(`claude`, `claude-ds`, `codex`); usage source names identify the CLI schema.

A background-task notification re-enters the session, and Claude prints one
`result` event per segment, each covering only that segment. The stream wrapper
sums `usage`, `num_turns` and `duration_ms` across all of a session's result
events before it publishes `result.json`, so the recorded values cover the whole
session. The `engine_result_summary` detail `result_events` reports how many
events were summed (`null` for a legacy result without the field); a counter
that a segment cannot contribute is omitted and the usage status becomes
`partial`. `total_cost_usd` and `modelUsage` are already cumulative across the
session and come from the last event.

### 3. Worktree markers — `dispatch.json` (per-engine, authoritative)

Every dispatched engine gets a `dispatch.json` in its worktree:

```
.claude/worktrees/
  issue-439/
    dispatch.json      ← implementer marker (engine, PID, deadline, stage)
    .impl-prompt.txt   ← the prompt the implementer is working from
    result.json        ← implementer output (lands when engine exits)
  pr-468/
    dispatch.json      ← reviewer marker
    review.json        ← reviewer typed verdict (lands when engine exits)
    completion.json    ← engine wrapper exit proof
    review-observation.json ← bounded sanitized skill-file and usage evidence
    stderr.log         ← bounded sanitized wrapper diagnostics only
```

The reviewer runs with `codex exec --json`. Its stdout is streamed through a bounded
sanitizer; raw JSONL, prompts, model messages, commands, command output, and absolute
paths are never retained. `review-observation.json` contains only normalized identities
for `SKILL.md` paths referenced by successful command executions, parser counters,
and the optional terminal usage snapshot described above.
HARVEST validates that exact shape and writes it to the tick log as
`reviewer_skill_observation` before cleanup. This is evidence of successful command
references, not a skill-file read or semantic invocation event: the current Codex CLI
exposes no explicit invocation event. An empty `observed_skills` list means no
successful command reference
was observed; it does not prove that no skill context was available or used. Observation
status never changes review validity, custody, labels, or cleanup decisions.

Reviewer stderr is discarded by the child wrapper. Reviewer `stderr.log` contains only
bounded sanitized wrapper diagnostics: the startup error class or fixed
`EngineExitError`. Inspect it with `completion.json` after a wrapper failure.
Implementer/fixer engine stderr remains available in the same per-worktree filename.

Implementers and fixers are wrapperless. Once their process is proven dead, a
non-empty `result.json` is treated as ready for harvest in memory; the tick does
not fabricate a `completion.json` or claim a verified zero exit status for those
engines — and it does not *read* one either. Dispatch passes `--envelope-path`
only for review, so an envelope in a wrapperless worktree had no legitimate
writer, while the engine that could have written it holds an unrestricted
`Write` tool with that worktree as its cwd. Believing it would let a running
engine declare its own completion and publish the result it wrote for itself.

A marker at launch (before the engine PID is published):

```json
{
  "engine": "claude-ds",
  "stage": "implement",
  "state": "launching",
  "dispatch_id": "impl-1748321535-12345-439",
  "deadline": 1748325135,
  "pr_number": 439,
  "base_ref": "main",
  "base_sha": "abc123...",
  "head_sha": "def456...",
  "permission_mode": "dontAsk",
  "verify_tools": ["sh ./check.sh"]
}
```

After engine spawn, the marker is updated with `pid`, `start_time`, `pgid`, and
`state` moves from `"launching"` to the live identity.

Check what's running right now:

```bash
for f in .claude/worktrees/{issue,pr}-*/dispatch.json; do
  echo "$f: engine=$(jq -r '.engine' "$f") stage=$(jq -r '.stage' "$f") pid=$(jq -r '.pid' "$f")"
done
```

### Markerless in-flight items — why the tick leaves them alone

Markers are **machine-local**. Ticks run from more than one machine (and from
linked worktrees on one machine), so no tick can see another's `dispatch.json`.
A missing marker therefore proves nothing about custody: it is the steady-state
reading of another machine's live dispatch, and of the manual `ai:implementing`
pickup that `CLAUDE.md` tells contributors to perform before writing code.

So the tick **never acts on a markerless in-flight item**. No relabel, no
cleanup, no escalation. An earlier rule did escalate on marker absence and
mass-relabelled healthy work with it; the credibility of
`ai:needs-human-decision` is worth more than the automation it bought. Every
custody escalation the tick can raise routes to that one label — the reviewer's
iteration cap is the only path to `ai:max-iterations` — so a signal that fires on
healthy work degrades the pipeline's whole stop channel, not one corner of it.

What you give up: an engine that dies without writing a result leaves its item at
an in-flight label indefinitely, and nothing reaps it. Clearing it is a manual,
operator-only action — and the automation's own reason for not doing it applies
to you.

**Dashboard absence is never custody proof.** `status.sh` reads shared GitHub
labels plus *this machine's* marker root. An in-flight label with no matching
row in `IN-FLIGHT ENGINES` is exactly as consistent with another machine's
healthy dispatch, or with a manual `ai:implementing` pickup, as it is with a
dead engine — the dashboard cannot tell them apart, and relabelling on that
reading steals live work the same way the old automation did.

So establish custody out of band before editing any label:

1. Ask on every machine that runs ticks. `pr-pipeline:status` on *that* machine
   shows its markers; a live engine appears under `IN-FLIGHT ENGINES` there.
2. Ask whoever may have taken the item by hand — `CLAUDE.md` tells contributors
   to add `ai:implementing` before writing code, so an issue at that label with
   no engine anywhere is usually a person, not a corpse.
3. Only when the dispatch host's owner (or the session owner for a manual
   pickup) confirms nothing is running is the item yours to relabel.

Then restore the pickup label the stage started from, so the next tick can
redispatch it:

```bash
# implement — the issue goes back to the implementer queue
gh issue edit 439 --add-label ai:available --remove-label ai:implementing
# review — the PR returns to whichever label it was picked up from
gh pr edit 604 --add-label ai:implemented --remove-label ai:reviewing
# fix — the PR returns to the review verdict that called for the fix
gh pr edit 604 --add-label ai:changes-requested --remove-label ai:fixing
```

Check the PR's own timeline for which review label preceded `ai:reviewing`
(`ai:implemented` on a first review, `ai:revised` after a fix round); guessing
sends the item through the wrong stage.

**A late result can still arrive.** The engine you concluded was dead may
finish after you relabel — its scratch directory is untouched, and the next
tick on *its* machine will harvest it and drive its own transition on top of
yours. If you relabel, also remove that machine's scratch directory (with its
owner, after archiving it under `.harvest-evidence/`), or expect one surprise
label move later. Leaving the item stranded and visible is the safer default;
relabel only to put the item back in a queue someone will pick up. It frees no
slot here: no dispatch marker is holding one.

A *valid* marker is still proof and is still acted on: a `launching` lease seen at
reconcile means the spawn crashed after starting the engine, and that escalates to
`ai:needs-human-decision` because a paid engine may be alive and unkillable.

The full rationale, the incident data, and the conditions for revisiting this are
in `docs/architecture.md` → "Why custody is not inferred, and
not yet proven".

### Retained scratch and harvest evidence

Every tick emits `retained_scratch` for an `issue-*` or `pr-*` directory that no
longer has a live engine. Redispatch never deletes that state first: it copies the
known engine artifacts and git diagnostic snapshots to:

```text
.claude/worktrees/.harvest-evidence/<kind>-<number>-<dispatch-id>/
```

Markerless interrupted-harvest directories use the suffix `retained-markerless`;
later incidents for the same item receive a numeric suffix instead of overwriting
the first bundle. Every completed archive moves the full original directory under
`scratch/`, preserving untracked or unknown recovery files as well as the copied
top-level evidence. Archives include implementer/fixer `result.json` and reviewer
`review.json` plus `completion.json` when those artifacts exist.
The shared archive finalizer immediately runs `git worktree prune` after that move,
so terminal harvest and stale/markerless redispatch recovery all reclaim the linked
worktree metadata that would otherwise pin a SHA. `worktree_prune_failed` reports
deferred metadata garbage collection without undoing the already-safe archive move.
An evidence directory without `scratch/` is an incomplete pre-transition copy;
the original worktree remains authoritative until a later retry completes the move.
The tick writes a `.prepared` marker after every evidence copy and snapshot
succeeds. A recovery retry returns an existing directory for this dispatch as is
only when that marker exists. Otherwise, preparation was interrupted, or an
older release left the archive unmarked, and the tick copies the evidence again
into the same directory before any report names it. Each evidence file and
snapshot is written to a temporary `.<name>.partial` sibling and replaces its
destination only when complete, so a crash or failed write never shortens a file
that a posted report already names. A failed repair keeps that directory and
retains the source scratch.
If archive creation, artifact copy, snapshot write, or the final scratch move fails,
the tick emits `archive_failed` and retains the original scratch. It makes no further
label transition; a terminal relabel that already succeeded remains authoritative.
Do not manually delete the source directory until the archive contains the evidence
needed for the incident.

The tick owns archive preparation and finalization during recovery. Operators
must preserve both the source scratch and its prepared evidence while a terminal
transition is pending. After finalization, old completed archives remain on disk
until the repository operator deliberately removes them after investigation.
`git worktree prune` removes stale Git registrations, not archived files. There
is no automatic archive expiry today; operator-controlled retention is tracked
in [#69](https://github.com/kwuBuild/pr-pipeline/issues/69). A published local
evidence path is subject to that operator cleanup, not a permanent hosted link.

A dispatch-bound failed reviewer completion is not a retry signal. HARVEST archives
its completion evidence, relabels the PR from `ai:reviewing` to
`ai:needs-human-decision`, then finalizes scratch cleanup; it must not be redispatched.
On archive failure, the in-flight label and scratch remain authoritative. On relabel
failure, the evidence archive and scratch remain in pipeline custody so a later tick
can retry the terminal transition without launching another reviewer.

An unreadable or unclassifiable marker is never deleted because the engine's
identity is uncertain. If the item is selected from a pickup label, dispatch moves
it to `ai:needs-human-decision` and retains the marker for inspection instead of
skipping it forever. That judgement is stage-scoped: one item has one scratch
directory, so a fix-stage dispatch can find a reviewer's marker there. A marker
that is structurally valid for the item but records another stage is that
stage's custody — dispatch skips and logs it, and only a marker this stage could
have written is treated as corrupt. Stale in-flight labels without a local marker are handled
separately and never relabelled — see
[Markerless in-flight items](#markerless-in-flight-items--why-the-tick-leaves-them-alone).
The sweep point-reads each item only to log an accurate `stale_label_retained`;
a failed lookup or malformed response leaves the label unchanged either way.

## How the three surfaces fit together

```
                    ┌─────────────────────────┐
                    │     status.sh (watch)    │  ← "what's happening right now?"
                    │  IN-FLIGHT | QUEUE | LOG │
                    └──────────┬──────────────┘
                               │ reads (read-only)
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
    ┌───────────────┐  ┌──────────────┐  ┌──────────────────┐
    │ dispatch.json │  │ GitHub labels │  │ .tick-log/*.jsonl│
    │ (per-engine)  │  │ (per-PR)      │  │ (per-tick audit) │
    └───────────────┘  └──────────────┘  └──────────────────┘
            │                  │                  │
            │  "is PID 12345   │  "PR #468 is     │  "at 14:32, the tick
            │   still alive?   │   ai:reviewing"  │   dispatched claude-ds
            │   what engine?"  │                  │   for issue #439"
            │                  │                  │
            └──────────────────┴──────────────────┘
                     authority: HARVEST + DISPATCH
```

## Quick reference: which surface for which question

| Question | Look at |
|---|---|
| "What's running right now?" | Dashboard → IN-FLIGHT ENGINES |
| "What's in the queue?" | Dashboard → PIPELINE STATE |
| "Why was issue #439 skipped?" | Tick log → `jq 'select(.pr==439)'` |
| "Is the claude-ds engine alive?" | Dashboard → find row with `claude-ds` engine, check `alive` |
| "How many times have I used claude-ds vs claude?" | Tick log → group by engine |
| "What did the implementer produce?" | Worktree → `result.json` |
| "What errors did the engine hit?" | Tick log → `jq 'select(.pr==N)'` |
| "What did the reviewer find?" | Worktree → `review.json` |
| "Why did the reviewer reject?" | Worktree → `review.json` → `.verdict` field |
| "Which reviewer skill files were observed?" | Tick log → `reviewer_skill_observation` (`observed_skills`) |
| "How many fix iterations has this PR gone through?" | Dashboard (iteration in label) or `gh pr view` |
| "Did a dispatch fail to launch?" | Tick log → `select(.action|startswith("skip_"))` |
| "Is the pipeline currently quiescent?" | Dashboard → IN-FLIGHT ENGINES shows `(none)`, state shows all terminal labels |

## Worked example

You run `~/.claude/pr-pipeline/bin/pr-pipeline tick '#439'` with issue #439 at `ai:available`.

**Dashboard** (after dispatch):
```
IN-FLIGHT ENGINES
  PR#439   implement claude-ds pid=18234  alive  working     55m-to-deadline
```
→ Engine `claude-ds` launched, PID 18234, still running, 55 minutes before timeout.

**Tick log** entry:
```json
{"ts":1748395200,"pr":439,"phase":"dispatch","stage":"implement","action":"launch","reason":"impl-1748395200-18234-439","engine":"claude-ds"}
```

**Marker** (`issue-439/dispatch.json`):
```json
{"engine":"claude-ds","stage":"implement","dispatch_id":"impl-1748395200-18234-439","pid":18234,...}
```

**Later, when the engine finishes and the next tick harvests:**

Dashboard IN-FLIGHT ENGINES goes to `(none)`.

Tick log gets:
```json
{"ts":1748398800,"pr":439,"phase":"harvest","stage":"implement","action":"harvest_complete","reason":"IMPLEMENTATION_COMPLETE sentinel found","engine":""}
```

GitHub label moves: `ai:implementing` → PR created with `ai:implemented`.
The completed `issue-439` worktree is cleared. Reviewer dispatch creates a fresh
`pr-<new-PR-number>` worktree so implement-stage marker custody cannot leak into the
review stage.

The next tick's DISPATCH phase will see the new PR at `ai:implemented` and dispatch
a reviewer (Codex).

## Design principles

| Design decision | Established principle |
|---|---|
| Labels are the source of truth; every tick re-reads them | Level-triggered reconciliation (a missed tick loses nothing) |
| Advisory nudges end the loop's sleep early; the tick still re-reads everything | Edge-triggered wake-up, level-triggered processing (the informer pattern: events affect *when* you reconcile, never *what* you conclude) |
| HARVEST then DISPATCH, unconditionally | Observe-then-act reconcile ordering |
| Lease-before-label; marker survives a failed label edit | Write-ahead intent: record intent before acting so a crash mid-operation is recoverable |
| Fail closed on every external read and malformed response; one corrupt marker blocks all dispatch | Crash-only design: prefer doing nothing over acting on garbage state |
| `max_in_flight` counts this machine's dispatch markers | Admission control bounding local spend, not shared workflow state |
| Structured per-decision tick log | Reconcile audit trail |
| Detached engines + identity-verified PID supervision | Orphan supervision with PID-reuse protection (the cost of a crash-safe ephemeral ticker) |

## Known limitations

### Resolved (Python extraction)

1. ~~**No single continuous shell.**~~ **RESOLVED.** The orchestrator is now a single
   Python process (`lib/tick.py`). Variables are normal Python locals that live for
   the duration of `main()`. No state re-establishment between blocks.

2. ~~**UV cache cross-contamination.**~~ **RESOLVED.** 47 per-tick `uv` subprocess
   calls reduced to none: `pipeline.py` and `process_control.py` are imported
   directly — no subprocess round-trips for decision logic — and the one
   remaining process is launched by `tick.sh`, which resolves an interpreter
   itself rather than going through `uv` at all.

3. ~~**Shell dialect mismatch.**~~ **RESOLVED.** No bash at all. `pathlib` for globs,
   `json.loads()` for parsing, Python iteration — no `shopt` vs zsh, no `for` vs
   `while read`.

4. ~~**Harvest side effects deferred.**~~ **RESOLVED.** All three `_harvest_one_*`
   functions are fully implemented:
   - Implementer: reads `result.json`, creates PR via `gh pr create`, clears its
     completed `issue-N` scratch, and transitions labels. Reviewer dispatch owns a
     fresh `pr-PR` worktree. Result-driven escalations publish the report
     described in [Escalation publication coverage](#escalation-publication-coverage).
   - Reviewer: reads `review.json`, detects base/head drift, posts review comment,
     enforces idempotent replay, transitions labels.
   - Fixer: reads `result.json`, posts the fixer-run comment (head before → after,
     outcome, bounded engine excerpt, and — on an escalation — the escalation
     reason), transitions to `ai:revised`, bumps the iteration cache. The post
     gates the relabel, and a **controller-owned** receipt under
     `WT_ROOT/.receipts/`, keyed to the run's `dispatch_id` *and* the decision it
     published, keeps a retried harvest from double-posting without letting a
     retry that decides differently hide behind the old comment. The receipt is
     deliberately *not* stored in the scratch: that is a checked-out worktree of
     the PR branch, so a tracked file at the receipt path would forge the
     "comment landed" proof.
   Each function fails closed — a `gh`/`git` error retains the marker for the
   next tick. The exceptions are the two best-effort posts listed above (the
   attempt-cap escalation and the attempt-counter write failure), which do not
   block their own relabel. Every terminal transition — the attempt cap,
   transient exhaustion, and a result-driven stage escalation — records a typed
   terminal intent under `WT_ROOT/.terminal/` before any remote mutation,
   because each publishes a comment claiming to supersede the run's own
   outcome. The next tick resumes that intent, so a failed relabel or a failed
   final archive move cannot be quietly replaced by the run's own label. If a
   workflow hold has taken the label in the meantime, the resume releases only
   the pipeline's own in-flight label (`terminal_relabel_held`) rather than
   re-attempting a converge the hold makes impossible. The reviewer's own
   escalation is included even though it posts nothing: it converges to a hold
   label, so an interrupted transition would leave a completed marker under a
   hold the pipeline set itself, and the held-completion notice the next tick
   would publish there says a hold was observed while the engine ran.
