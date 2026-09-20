# Plugin Distribution Design

Design for turning `pr-pipeline` from a repository that operates on itself into a
Claude Code plugin that one operator installs once and points at many
repositories they do not own.

This is a design document, not operational documentation. Implementation is
sequenced by the plan that follows it; nothing here is built yet.

Revised across four reviews. Findings that survived verification against the
source are folded in; section 13 records what each review changed and
what it corrected. The reviews ran against pre-squash commits that no published
ref reaches, so they are identified by round rather than by SHA — a hash nobody
else can resolve is not a citation.

---

## 1. Goal and target experience

Today the pipeline is a git clone whose install path the operator exports as
`$PR_PIPELINE_HOME`, whose configuration lives inside that clone, and which only
completes a fix cycle because the repository it operates on happens to be itself.

The target is an ordinary plugin install:

```
/plugin marketplace add kwuBuild/pr-pipeline
/plugin install pr-pipeline
/pr-pipeline-setup          # once: confirm login, name repos, confirm verify commands
/pr-pipeline:tick           # run from repo B, C, or D
```

One operator (call them A), one install, many target repositories (B, C, D). A
owns every policy decision; no target repository gets a vote.

## 2. Configuration ownership

**Configuration is authored by the operator and read from outside both the
install and the target repository.**

### Why it cannot stay in the install

`pipeline.py:197` resolves `_POLICY_CONFIG_PATH` install-relative. A plugin
install lands in `~/.claude/plugins/cache/<marketplace>/<plugin>/<hash>/`, and
`/plugin update` installs the next version at a *different* hash. An
install-relative config is therefore silently discarded on every upgrade.

That settles the location question on its own, independent of any threat model,
and it is the reason this section exists.

### Why it cannot move into the target repository either

The obvious alternative — per-repo config committed to the target — puts policy
inside the tree the reviewer and fixer check out. A pull request would then
supply the settings it is judged under. That is a bad shape regardless of who is
contributing, so config lives with the operator.

### What this does *not* claim

This is a placement decision, not a security boundary, and an earlier draft of
this document oversold it as one.

Moving configuration outside the target tree stops a PR from **supplying**
policy. It does not stop a PR from **overwriting** it. A granted verification
command such as `npm test` or `./gradlew test` executes scripts the pull request
controls, in a process holding A's filesystem access and ambient credentials.
That script can write `~/.claude/pr-pipeline/config.toml` directly. A Bash
permission rule governs which command string the model may invoke; it inspects
nothing about what the resulting process then does.

Containment of that belongs to the engine and model layer — process isolation, a
sandboxed filesystem and network, of the kind the reviewer already gets via its
scrub-dir and `HOME`/`CODEX_HOME` redirect. It is not something a config-location
rule can deliver, and no amount of orchestrator-side gating substitutes for it. It
is out of scope here and recorded as open question 12.1.

The property that *is* enforced gets a test: no configuration layer is ever read
from inside a target repository or an engine worktree. It should fail loudly if a
later convenience override reintroduces the path.

## 3. Configuration: location, layers, and schema

### Location

`~/.claude/pr-pipeline/config.toml`, honouring `$CLAUDE_CONFIG_DIR` when set,
with `$PR_PIPELINE_CONFIG` as an explicit override for testing.

This replaces the install-relative `_POLICY_CONFIG_PATH` (`pipeline.py:197`)
for the upgrade-safety reason given in section 2.

### Layers

Resolution order, later layers overriding earlier ones:

1. **Packaged defaults** — the install's existing
   `skills/tick/config.toml`, now read-only and treated as the
   shipped floor.
2. **`[defaults]`** — operator-wide settings in A's config.
3. **`[repos."owner/name"]`** — per-repository overrides.
4. **Environment variables** — the existing `PIPELINE_REPO` /
   `PIPELINE_TRUSTED_AUTHOR` one-off overrides.
5. **Explicit CLI flags** — `--permission-mode` and the other per-invocation
   arguments the tick already accepts. Highest, because a flag typed on one run
   is the most specific statement of intent there is.

The two environment overrides need restating, because the keys they override
today do not survive this change:

- `PIPELINE_REPO` no longer overrides a config key — `[target] repo` is gone. It
  overrides **git-remote resolution**: the canonical target becomes its value
  instead of whatever the working directory's remote reports, and the
  `[repos."owner/name"]` section selected is the one matching that value. It is
  checked against a non-empty `[scan] repos` like any other target, so a one-off
  override cannot widen scope the operator restricted.
- `PIPELINE_TRUSTED_AUTHOR` overrides `[operator] login`, which sits outside the
  `[defaults]` / `[repos.*]` layering — see the note under the schema. The layer
  order still applies; there is simply one layer beneath it rather than three.

### The schema

Every per-target setting the runtime reads appears in `[defaults]` and may be
overridden in `[repos."owner/name"]`. Nothing is per-target *only* in one of the
two, because a key that exists in one table and not the other is a key an
operator cannot express at the layer they need it.

`[operator] login` is the one deliberate exception, and it is stated here so the
rule above is not quietly broken later: it is an identity, not a per-target
setting. One operator drives every target, so a per-repository override would
mean the pipeline reviewing PRs as somebody else on one repo — a trust boundary
change wearing a configuration key's clothes. Widening *issue* pickup to other
authors is a separate, per-repository concern, deferred out of phase 1 (section
4).

**The default is that the operator is trusted and nobody else is.** Issues
authored by the operator are picked up; pull requests authored by the operator are
reviewed. That needs no configuration. Every other author is out of scope for
phase 1 — see section 4.

```toml
# Confirmed at setup (section 8). The one identity the trust gates are written
# against: issues it authors are picked up, PRs it authors are reviewed.
[operator]
login = "userA"

# Operator-wide defaults. Every key here is overridable per repository.
[defaults]
# Auto-review PRs that edit reviewer-governing files (see below). True by
# default: under operator-only authorship there is no untrusted author to
# defend against, and on a repository the pipeline develops itself this gate
# would escalate nearly every PR.
allow_touching  = true
# Repo-relative paths, in the target, naming that repo's own review checklists.
# CLAUDE.md and AGENTS.md are matched by basename already, so this is for extras
# only and empty is correct for most repositories.
checklist_paths = []
verify_tools    = []      # engine Bash grants (was [engine]); ships empty
base_ref        = ""      # "" = resolve the target's default branch
permission_mode = "auto"  # engine --permission-mode (section 5)

[scan]
repos = []                # empty = unrestricted

[repos."kwuBuild/pr-pipeline"]
verify_tools = ["uv run pytest *", "uv run ruff *"]

[repos."kwuBuild/notes-app"]
base_ref        = "master"
verify_tools    = ["npm test *"]
checklist_paths = ["docs/review-checklist.md"]

[repos."kwuBuild/reporting-service"]
base_ref        = "trunk"
verify_tools    = ["./gradlew test"]
permission_mode = "dontAsk"   # this route failed the section 8 auto probe
```

The table key is `owner/repo` exactly as `gh` reports it.

**Issue authorship and PR authorship are separate gates, and stay separate.**
Issue pickup decides whose issue becomes an implementer prompt; reviewer
admission (`trusted_review_candidate`, matched against the PR author) decides
whose PR may be auto-reviewed. Both default to `[operator] login`. When the
author set is widened later (section 4), widening one must not widen the other —
permitting someone to file issues would otherwise silently permit auto-review of
their PRs as well.

### What `allow_touching` governs

The reviewer runs with its working directory inside a checkout of the PR's own
head, and Codex loads `AGENTS.md` from there automatically — which in this
repository routes to `CLAUDE.md`. So a PR that edits those files edits the
instructions its own reviewer runs under. `touches_review_policy`
(`pipeline.py:1676`) covers `CLAUDE.md`, `AGENTS.md`, the operator's
`checklist_paths`, and the pipeline's own `config.toml`/`pipeline.py`/`tick.py`
by path suffix; `tick.py:9098` routes a matching PR to a human instead of
auto-reviewing it.

`allow_touching = true` turns that routing off, and **that is the shipped
default**. The gate is only load-bearing once someone other than the operator can
get code in front of the reviewer, which phase 1 does not permit. Against that,
it escalates constantly on any repository the pipeline develops itself — where
nearly every PR touches `pipeline.py` or `tick.py` — which would stop a cycle
from ever completing unattended.

The default flips back to `false` as part of the deferred author-widening work,
not separately; section 4 records the two as one change.

### Mapping from the current format

The runtime reads five keys today across three tables, each through its own
accessor: `[target] repo`, `[target] trusted_author`, `[policy] allow_touching`,
`[policy] checklist_paths`, `[engine] verify_tools`. A sixth setting,
`permission_mode`, has no config key at all — it is CLI-only (`tick.py:12633`,
defaulted to `dontAsk` at `:12699`), and section 8 requires an operator's
per-route choice to be recorded rather than re-typed on every invocation, so it
acquires one here. Layering new tables beside the existing ones produces two
disagreeing configurations, so the mapping is part of the change, not a
consequence of it:

| Today | Becomes |
|---|---|
| `[target] repo` | removed — resolved from the working directory's git remote |
| `[target] trusted_author` | `[operator] login` |
| `[policy] allow_touching` | `[defaults]` / `[repos.*]` `allow_touching` |
| `[policy] checklist_paths` | `[defaults]` / `[repos.*]` `checklist_paths` |
| `[engine] verify_tools` | `[defaults]` / `[repos.*]` `verify_tools` |
| `--permission-mode` (CLI only) | `[defaults]` / `[repos.*]` `permission_mode`; the flag stays as the top override layer |

The packaged `config.toml` is restructured into the same shape rather than kept
in the old one — it is layer 1 of the resolution order, so a packaged file still
written as `[target]`/`[policy]`/`[engine]` would be a fourth disagreeing
configuration rather than the floor.

### Resolution rules

- **One resolver, one result.** A single function returns the effective
  configuration for a resolved target, and `tick.py`, the setup skill, and every
  Python caller read it. No caller reads a raw table. The existing per-key
  accessors (`pipeline.config_string`, `pipeline.config_string_list`) become its
  internals, not its API.
- **Lists replace, they do not merge.** A repository's `verify_tools` overrides
  the default list wholesale. A merge would mean an operator could never
  *narrow* a grant for one target, which is the wrong direction for a
  permission list.
- **Fail-closed is scoped to the settings that have a safe direction.** A
  non-list where a list is expected, or a non-boolean gate, resolves to the
  restrictive value and emits a tick-log entry, matching the current
  conservative read of a malformed `config.toml`. It is never silently coerced.
  Settings with no conservative value — `base_ref`, `permission_mode`,
  `[operator] login` — have no fail-closed reading to fall back to, so a
  malformed one is a hard startup error naming the key. A blank `base_ref` is
  not malformed; it is the documented "resolve the default branch" value.
- **A malformed `verify_tools` is not an empty `verify_tools`.** These are
  different states and section 7 treats them differently: an empty list is a
  deliberate operator choice that dispatches with a warning, while a list that
  failed to parse is a hard startup error. Without the distinction, a typo in
  one config line silently becomes an unverified PR promoted to
  `ai:ready-for-human` — fail-closed and section 7's dispatch-anyway decision
  compose into exactly the hole neither rule has on its own.
- **A repository section naming a repo absent from a non-empty `[scan] repos`**
  is a configuration error reported at setup, not a silent override.

### `status.sh` reads no config at all

`status.sh` cannot call the resolver. Its own comment (`status.sh:32-38`) records
why it `sed`-parses rather than importing a TOML library: it *"must run before any
interpreter is resolved."* That is load-bearing — the dashboard's job includes
telling an operator that their interpreter situation is broken, so it cannot
require one.

It does not need one either. The only thing the dashboard reads from config today
is the target, and the target now comes from the working directory's git remote
with `PIPELINE_REPO` as the override — the same rule the resolver uses, reached
without an interpreter and without TOML. So the `[target] repo` parse
(`status.sh:44-45`) is deleted rather than ported.

That leaves exactly one reader of the config, in Python, and no bash/Python
agreement test to maintain. If the dashboard later needs to *display* a resolved
config value, that is the point to reopen this — not before.

### One canonical target, used everywhere

The target repository is resolved from the working directory's git remote.
`[target] repo` and the startup check that refuses to run when it is unset are
removed — that check is what confines an install to a single repository.

Removing it is not sufficient on its own, because repository identity is
currently split across three mechanisms:

- `gh()` (`tick.py:1004`) forwards its arguments verbatim and most call sites,
  including relabels, pass no `--repo` — so they resolve against whatever the
  working directory happens to be.
- Other call sites use an explicit trusted-head-repo value.
- Git fetch and push assume the remote is named `origin`.
- `status.sh:44-45` independently `sed`-parses the install-relative `config.toml`
  for `[target] repo`, deliberately avoiding a TOML library because it must run
  before any interpreter is resolved (`status.sh:32-38`). Deleting that key
  breaks the dashboard.

So this change resolves **one** canonical target plus its matching git remote,
and threads it through dispatch, harvest, cadence, mutation, and the dashboard.
`status.sh` drops the config parse and resolves the target from the git remote
with `PIPELINE_REPO` as the override — the same rule as the resolver, reached
without an interpreter, per the carve-out above.

`[scan] repos = []` means unrestricted, which is the shipped default. A non-empty
list restricts which repositories are eligible.

`trusted_review_candidate` (`pipeline.py:2508`) separately matches the PR's
*head* repository to reject cross-repository fork PRs. That mechanism stays; it
derives from the resolved target rather than from config.

## 4. Issue-author trust gate

### Behaviour

The pipeline picks up **only issues authored by the operator running it.** There
is no configuration key and no way to widen it in phase 1.

The rationale mirrors `trusted_review_candidate`: an issue body becomes a
dispatched engine prompt, executed with A's credentials. A stranger's issue
becoming an instruction to a credentialed agent is a prompt-injection surface.

Shipping only the fixed rule — rather than the rule plus the knobs to relax it —
is deliberate. A single-operator install never needs the knobs, and widening the
author set is the part that is genuinely fiddly: it turns one bounded query into
a multi-author merge, and it reopens queue-scan behaviour that the bounded query
makes moot. None of that is needed for an adopter to run the pipeline on their
own repositories, which is what phase 1 is for.

### What changes

The available-issue query requests `--json number,title` (`tick.py:9433`) — no
author is fetched at all, so this is new behaviour rather than a toggle over
existing logic. It needs:

- `author` added to the query's JSON field list.
- A new `pipeline.trusted_issue_candidate()` beside `trusted_review_candidate`,
  failing closed on missing or mistyped author metadata exactly as that function
  does.

### Filter at the query, not after it

The available-issue query becomes `gh issue list --author <operator login>`.
`--author` is a documented `gh issue list` filter, so the narrowing happens
server-side and the tick never reads an issue it cannot dispatch.

That single decision is what keeps this section small. A client-side predicate
over one page would have to answer for the scan limit
(`_ACTIONABLE_QUEUE_SCAN_LIMIT`, 200): other authors' issues would consume the
budget and hide eligible work behind it, and `_queue_page_has_dispatchable`
(`tick.py:12010`) treats a full page as possibly-actionable, so a queue of
skipped issues would hold `--loop` awake indefinitely. With a bounded single
author, the page contains only the operator's own issues and neither failure has
a way to occur.

The same predicate still applies at targeted runs, so `--issue N` on someone
else's issue is refused rather than dispatched.

### Disposition of an issue by another author

Skipped. Specifically **not** relabelled and **not** escalated.

The `ai:` vocabulary is the pipeline's own, and moving a third party's issue to
`ai:needs-human-decision` would be the pipeline claiming custody of an item it
was never granted. A skip is also not a pipeline failure, so it must not look
like one in the logs.

Because the filter is server-side, the tick never holds such an issue and cannot
log one. That is invisibility, and section 6 rejects exactly this shape for
unknown labels — an item on `ai:available` that is queued nowhere and mentioned
nowhere. It is paid for once, cheaply: after the filtered read the tick issues one
unfiltered `gh issue list --label ai:available --json number` capped at the scan
limit and logs the difference — *"N available issues not authored by the
operator."* One extra bounded read, no per-issue detail, degrading to silence
rather than a wrong number if it fails, and reporting "N or more" at the cap.


### Deferred: widening the author set

Permitting non-operator authors is a separate feature request, not part of the
plugin work. It carries **two** changes that must land together, because they are
answers to the same condition:

1. The author-set configuration itself — `issue_authors` and
   `allow_any_issue_author` as per-repository keys, a multi-author merged read,
   and pagination for the unbounded case. Pagination must make progress (a page
   returning no new issue numbers ends the read) and an incomplete read must
   report unknown rather than quiescent, so cadence stays warm instead of
   declaring an empty queue it never proved.
2. **Restoring `allow_touching = false`** as the default. Section 3's argument
   for `true` holds only while the operator is the sole author: once a stranger's
   issue can become an implementer prompt, the PR it produces can carry a
   `CLAUDE.md` edit that steers its own reviewer, and the gate becomes
   load-bearing again.

Shipping (1) without (2) would quietly remove a protection at precisely the
moment it starts mattering, which is why they are recorded as one item rather
than two.

### Learning the operator's login

`pr-pipeline-setup` (section 8) resolves it from `gh api user --jq .login`,
displays it, and asks A to confirm. Auto-discovered for convenience, never
silently adopted — the value is one side of a trust boundary, so a human
confirms it once.

## 5. Permission mode: `auto` by default

### The change

Engines default to `--permission-mode auto` instead of `dontAsk`. `auto` is
already implemented and accepted at `tick.py:952`; it is currently opt-in and
documented as experimental.

### Why it fits the rest of this design

Under `dontAsk` an undeclared command is not prompted for. The comment at
`tick.py:760` records the consequence: "a denial mid-run reads to the engine as a
broken repo, not a missing grant." That is why the packaged floor's
`[defaults] verify_tools` ships a Python stack today — an empty default would
have made a fresh install fail its first dispatch on an illegible denial. (That
default lived in `tick.py` as `_DEFAULT_VERIFY_TOOLS` when this was written; it
now lives only in `skills/tick/config.toml`, because a fallback in the code is
one an operator cannot narrow away.)

Section 7 empties that default. Under `auto` an undeclared command reaches the
classifier instead, so an unconfigured Gradle repository degrades to "the
classifier evaluates `./gradlew test`" rather than "the engine concludes the
repository is broken."

### Reaching the classifier is not the same as being permitted

That is as far as the argument goes, and the earlier draft took it further than
it goes. This project's own documentation says so: `SKILL.md:24` records that
under `auto` *"a classifier denial fails the run,"* and `README.md:644` describes
the same path. A classifier that declines `./gradlew test` produces a failed run,
not a graceful degradation — the same illegible outcome `dontAsk` produces, via a
different route.

Two facts about the launcher make the headless case genuinely unestablished
rather than merely unproven:

- `build_engine_cmd` (`tick.py:959-964`) passes `-p`, `--permission-mode`, and
  `--allowedTools`, and nothing else bearing on prompting.
- `claude --help` on `2.1.270` documents `--permission-prompts`, defaulting to
  `host`: with `--print` and no SDK host or `--permission-prompt-tool`, *"anything
  that would prompt is denied automatically; the permission mode still decides
  everything else."*

So what happens to an undeclared command that `auto`'s classifier does not
auto-approve — denial, prompt-then-denial, or hang — is a property of the
installed CLI that has not been measured. The honest form of the argument is
therefore conditional:

**`auto` improves on `dontAsk` for commands the classifier approves, and is no
worse for the rest.** That is still the right default, because the set of
approved-but-undeclared commands is exactly the set `dontAsk` strands. It is not
a guarantee that an unconfigured repository completes a dispatch, and section 7's
empty `verify_tools` must not be justified as if it were.

What follows is a real gate rather than a reassurance. Section 8's probe is
strengthened to measure this directly instead of confirming that the flag is
accepted, and open question 12.3 owns the prompting semantics, alongside
12.2's `--allowedTools` question.

### The operator's deny list is the floor

`_engine_env()` (`tick.py:465`) returns `dict(os.environ)` for implementer and
fixer engines, and the engine command (`tick.py:959`) passes no `--settings` and
no `CLAUDE_CONFIG_DIR` redirect. Those engines therefore resolve A's normal
settings hierarchy, and A's `~/.claude/settings.json` deny rules apply — deny
outranks allow, in `auto` as in `dontAsk`. No new mechanism is required.

(The reviewer is different and should not be described with the same sentence: it
runs under a scrub-dir with `HOME`/`CODEX_HOME` redirected, so it does not
inherit this environment.)

### The exposure is not created by `auto`, only widened

An earlier draft claimed the project-settings exposure was "largely inert" under
`dontAsk` because the CLI allowlist governed. That is too strong.
`--allowedTools` grants approvals; it is not an exclusive definition of the tools
available, and `dontAsk` still honours applicable settings allow rules. The
launcher passes neither a tool-exclusive flag nor any settings-source isolation.
So a PR's own `.claude/` configuration already has reach under `dontAsk`;
flipping to `auto` widens the surface rather than opening it.

The practical consequence is that this section's fix is required *now*, not as a
precondition of the default flip — and that the precise reach of
`--allowedTools` under each mode must be verified against the installed CLI
before the flip ships (open question 12.2), not inferred from the flag's name.

### The fix is a trusted-input allowlist, not two more filenames

Because engine `cwd` is a checkout of the PR's own head, every file in that tree
that Claude Code or Codex reads as configuration is PR-authored. The policy gate
covers very little of that surface: `_POLICY_BASENAMES` (`pipeline.py:168`) is
`{CLAUDE.md, AGENTS.md}`, and `_POLICY_PATH_SUFFIXES` covers only the pipeline's
own `config.toml`, `pipeline.py`, and `tick.py`.

Naming `.claude/settings.json` and `.claude/settings.local.json` as two more
entries was the earlier plan, and it is insufficient. The surface includes at
least:

| Surface | Why it matters |
|---|---|
| `.claude/settings.json`, `.claude/settings.local.json` | Permission allow rules |
| `.mcp.json` | Project MCP servers, connected without prompting in headless runs |
| `.claude/skills/**`, `.claude/commands/**` | Can carry `allowed-tools` grants |
| `.claude/hooks/**` and any script a hook references | Executes code; the *referenced script* can change while the hook config does not |
| `CLAUDE.local.md`, `.claude/rules/**`, imported instruction files | Steer the engine |
| `AGENTS.override.md` | Codex prefers it over `AGENTS.md` |

Two things follow. First, these are not one mechanism: some **grant
permissions or execute code**, others only **steer**, and they deserve different
dispositions rather than one shared escalation. Second, a denylist of filenames
cannot be complete, because a hook whose config is untouched can point at a
script the PR rewrote.

So the gate is defined as an **allowlist of trusted engine inputs**, resolved
transitively to the files actually referenced, with anything outside it either
escalated or neutralised before launch. Neutralising is the stronger option for
the permission-granting and code-executing rows: the engine could be launched
with project settings sources disabled outright, making the question moot rather
than policed. Whether the CLI supports that is part of open question 12.2.

### Not in scope: fixer-dispatch revalidation

An earlier draft of this document specified a three-part revalidation at fixer
dispatch — re-run `touches_review_policy` against the current diff, pin the head
SHA, re-run `trusted_review_candidate`. It is removed, for two reasons that
arrived from different directions.

**The premise was wrong.** The concern was that the reviewer pins `headRefOid`
(`tick.py:8991`, `:9120`) while the fixer checks out `origin/<head_ref>`
(`tick.py:10286-10288`), so a commit landing between review harvest and fixer
dispatch leaves the fixer working from findings that describe an earlier commit.
Most of that window does not exist: review harvest already pins the reviewed SHA
and discards the review outright when the head moved under it (`head_advanced`,
`pipeline.py:1110`; the discard branch reverts to `pre_label` and posts nothing).
What remains is the gap between harvest labelling the PR and dispatch admitting
it — usually the same tick, seconds apart.

**And the residue is not the orchestrator's problem.** The design was treating
review findings as instructions the fixer executes. They are not. The fixer
already makes a per-finding judgement — `Decision: repair` versus
`Decision: replacement`, with a replacement owing an argument against what the
reviewer proposed — and the correct disposition of a finding that no longer
applies is for the fixer to say so, not for the tick to detect drift on its
behalf. An agent that checks each finding against the code actually in front of
it makes the drift question moot.

That implies two small runtime changes, both in the prompt and harvest layer and
neither in dispatch. They are **not** plugin-distribution work and belong in a
standalone issue:

- A third per-finding disposition — *no change needed*, with a stated reason — so
  a finding already addressed can be recorded rather than force a fabricated
  change or an escalation.
- A legal "nothing to push" completion. `decide_fix_harvest` treats a success
  claim with an unchanged head as contradictory output and escalates
  (`pipeline.py:2412-2413`), which is right for a fixer that failed to push and
  wrong for one that correctly found nothing to do. The two are distinguishable
  by whether every finding carries an explicit no-change decision.

The permission half of the original section — whether the fixer's engine inputs
are trusted — is covered by the allowlist above and does not need a separate
per-stage gate given operator-only authorship.

### Tradeoff, recorded

`_ENGINE_ALLOWED_TOOLS_BASE` (`tick.py:745`) describes itself as "a named
security boundary for unattended operation: no force-push, remote-branch
deletion, or arbitrary refspec push." Under `auto`, those reach the classifier
rather than being refused. A deny rule is the mitigation and the setup flow
offers to install one — with the caveat from section 2 that a Bash rule matches
command strings, not process behaviour, so it bounds what the *model* invokes and
not what an invoked test script does.

## 6. Adoption prerequisites

Four things a fresh target repository needs that nothing currently provides. The
first three fail on the first tick against any repository that is not this one;
the fourth fails silently, which is worse.

### Default branch

`base_ref` is hardcoded to `"main"` at `tick.py:9608` for issue dispatch, with a
matching default at `tick.py:6801`. A repository on `master`, `trunk`, or
anything else either fails to fetch or branches from the wrong ref. The resolved
value comes from the repository's actual default branch, with a
`[repos."owner/name"] base_ref` override.

### Label provisioning — part of installation

There is no `gh label create` anywhere in the tree. The `ai:` vocabulary is
assumed to exist, so on a fresh repository every transition fails.

Label creation is part of the installation flow. The mechanism is
`pr-pipeline-setup` rather than `/plugin install` itself: a plugin install is a
file copy that knows nothing about a target repository, so there is no point in
it at which a repository's labels could be created. Setup is where a repository
is named, so that is where its labels are created — idempotently, from the
`LABEL_*` constants in `pipeline.py`, at first-time install and again each time A
adds a repository.

The tick also verifies the set is present before its first transition, so an
operator who adds a repository without running setup gets a legible error instead
of a mid-dispatch failure.

### Unknown `ai:` labels — reported, never deleted

Provisioning has to handle the reverse drift too: an `ai:`-prefixed label that
exists on the target but is not in `LABEL_*`. This repository carried three
(`ai:in-queue`, `ai:recheck`, `ai:permission-pending`), which is how the case was
found; they have since been reconciled by the owner, so a reader checking today
will find the `ai:` set here equal to the eleven `LABEL_*` constants. An adopter
repository can acquire the same drift from an earlier tool, a fork, or a human's
own convention.

The hazard is silence, not failure. A label outside the vocabulary is selected by
no dispatch query, so an item carrying only one of them is not queued, not
in-flight, and not escalated — it is invisible. Nothing errors, which is why the
drift survives: an operator who moves an item there reasonably believes they have
parked it somewhere the pipeline will come back to.

So the fix is legibility at both ends, and deliberately not mutation:

- **Setup reports them.** After creating the vocabulary, setup lists every `ai:*`
  label on the target that is not in `LABEL_*`, names the go-dark consequence, and
  stops there.
- **Setup never deletes them.** Deleting a label strips it from every issue and PR
  that carries it, across all of history, and cannot be undone — a destructive
  remote action on a repository the pipeline does not own. It is also frequently
  wrong: an unknown `ai:*` label is often a human's deliberate hold. Reconciling
  is the repository owner's decision, and setup's job ends at telling them it
  needs making.
- **The dashboard surfaces the dark items.** `status.sh` grows a row for open
  items whose only `ai:` label is outside the vocabulary, so a parked item is
  visible as parked rather than absent. Without this, the reported-at-setup
  warning is a one-time message about a permanent condition.

### Repository routing

Covered in section 3 — `gh()` call sites without `--repo`, `origin` assumed as
the remote name, and `status.sh`'s independent config parse.

## 7. `verify_tools` ships empty

The packaged floor's `[defaults] verify_tools` currently ships `uv run pytest`,
`uv run ruff`, `uv run pyright`, and an `npm --prefix frontend` entry. For a
plugin pointed at a repository we have never seen, those are a guess about
someone else's toolchain — and a wrong guess is worse than none, because the
allowlist is an exact grant, so the engine silently cannot run the target's real
tests. A Gradle or Maven repository gets nothing usable.

The packaged layer therefore documents the *shape* with empty lists and no
values. Per-repository values come from `[repos."owner/name"] verify_tools`,
populated by `pr-pipeline-setup`.

### Empty dispatches with a warning

**Decided: a deliberately empty `verify_tools` dispatches rather than blocking.**
This is the operator's call, made with the objection below already on the table.

"Deliberately" carries weight. Per section 3, a `verify_tools` that failed to
parse is a hard startup error, not an empty list — otherwise a typo inherits this
decision and quietly produces an unverified PR. Empty means an operator wrote
`[]`, or accepted it at setup on a route that passed the section 8 probe.

The objection, which is strong: the pipeline can spend money, push code, and
award `ai:ready-for-human` with no operator-defined acceptance check having run.
`auto` decides whether a command *may* execute, not whether verification
*happened* — and per section 5 it does not reliably decide even that, since a
classifier denial fails the run. And `verify_tools` is only a permission grant — the prompts tell the
engine to run the verification commands the target's own `CLAUDE.md`/`AGENTS.md`
prescribe (`tick.py:751`), so on a target with no such documentation, configuring
`verify_tools` does not by itself cause anything to be run.

Given the decision to dispatch anyway, the warning has to be durable evidence
rather than a log line, because it is the only signal the human merge gate gets:

- A tick-log entry at dispatch.
- A line in the PR body stating verification was unconfigured, which **survives
  harvest and the fixer's PR-body replacement**. A warning that a later revision
  silently drops is worse than none, because the PR then looks verified.
- The completion sentinel already requires green verification commands. Where
  none were configured, it records that explicitly as a waiver rather than
  reporting a vacuous pass.

## 8. The `pr-pipeline-setup` skill

A third skill, run once after install and again whenever A points the pipeline at
a new repository.

1. Resolve A's login via `gh api user --jq .login`, display it, ask A to confirm.
2. Ask which repositories A wants to drive.
3. Per repository: resolve the default branch; create the `ai:` label set
   (section 6); inspect for `pom.xml`, `build.gradle`, `package.json`,
   `pyproject.toml`, `Cargo.toml`; **propose** verification commands; A confirms
   or edits.
4. Offer to install deny rules for the destructive git surface (section 5).
5. Check the host prerequisites: `gh`, `git`, `codex`, `jq` (the dashboard needs
   it), and whether the installed `claude` supports the `auto` permission mode at
   all — including on the DeepSeek implementer route. Report what is missing
   rather than failing on first dispatch.

   `build_engine_cmd()` accepting the argument is not the check, and neither is a
   one-shot run that merely starts. `auto` is gated on the provider/model/account
   actually supporting it, and — per section 5 — reaching the classifier is not
   the same as being permitted. So the probe is a real
   `claude --print --permission-mode auto` against the configured route, launched
   the way dispatch launches an engine (same `--allowedTools`, no
   `--permission-prompt-tool`, no TTY), asked to run one benign command that is
   deliberately **not** in the allowlist — `git status` will do. It passes only if
   that command's output comes back. A probe that starts, classifies, and denies
   is a failure, and it is the failure that matters: it is the one that would
   otherwise be discovered as an unexplained dispatch failure on an adopter's
   first real run.

   The probe result is recorded per route, because the answer can differ by
   provider and model and a single global yes/no would be wrong for a
   mixed-engine install.

   Its failure has one of three explicit outcomes, chosen by A and recorded in
   the config as `permission_mode`, never defaulted silently:
   1. the route is unsupported and the engine selection changes;
   2. the run falls back to `dontAsk` with the verification grants configured, because
      `dontAsk` without grants is the mode that strands an engine mid-run; or
   3. A accepts `auto`-unsupported operation for that route and the pipeline
      refuses to dispatch to it.

   This probe runs **before** step 3 accepts an empty `verify_tools`: the warning
   in section 7 is only survivable because an undeclared command can still be
   approved at run time, which is exactly what a route failing this probe takes
   away. On a route that fails it, setup declines to accept an empty
   `verify_tools` and requires A to name the target's verification commands —
   the `dontAsk`-with-grants combination, which is the one configuration that
   does not strand an engine mid-run.
6. Write `~/.claude/pr-pipeline/config.toml`. Never write into a target
   repository.

Step 3 reads the target repository to *propose*, but a human confirms before
anything is written, so the target still cannot author its own configuration —
the rule stated in section 2.

### Re-entry on an unconfigured repository

When a tick resolves a target with no `[repos."owner/D"]` section, it prompts to
run setup. Under `--loop` there is nobody to answer, so unattended operation
skips that repository with a tick-log entry rather than blocking on input.

## 9. Plugin packaging and path resolution

### Manifests

- `.claude-plugin/plugin.json` at the repository root, with `"skills": "./skills"`.
- `.claude-plugin/marketplace.json` at the repository root, plugin
  `source: "./"` — the repository is its own marketplace, so
  `/plugin marketplace add kwuBuild/pr-pipeline` works with no separate registry.

The existing layout is already plugin-shaped: the skill directories with
name/description frontmatter are what a plugin's `skills` entry points at.

**Invocation names change, and the directory is what changes them.** Plugin
skills are namespaced by plugin, so the original directory names would have
yielded `/pr-pipeline:pr-pipeline-tick`. They were renamed to `tick` and
`status` — 12.4, decided below — for `/pr-pipeline:tick` and
`/pr-pipeline:status`, with `setup` reserved for section 8's skill.

The claim that the frontmatter `name` rather than the directory determines the
command was **measured and is false**. A user-level skill whose `SKILL.md`
declares `name: gstack` from a directory named `.gstack-bak` is offered as
`.gstack-bak`; a second copy of the same file under `_gstack-command` is offered
as `_gstack-command`. The directory wins, so the rename was load-bearing rather
than cosmetic. `test_each_published_skill_is_invoked_by_its_directory_name`
keeps the two equal so the ordering stops mattering.

### `$PR_PIPELINE_HOME` → `${CLAUDE_PLUGIN_ROOT}`

An operator cannot know the cache path to export, so the commands a skill tells
an agent to run move to `${CLAUDE_PLUGIN_ROOT}`. `PR_PIPELINE_HOME` is retained
as an explicit override so development from a git working tree keeps working.

**The substitution does not reach a shell.** `${CLAUDE_PLUGIN_ROOT}` is replaced
in plugin skill, agent, and command *content* as Claude loads it, and exported
into plugin hook, MCP, and LSP processes. It is not in the environment of a Bash
tool call — verified on CLI `2.1.270`, where `echo "${CLAUDE_PLUGIN_ROOT}"` from a
session Bash call prints empty. So there are two kinds of documented command, and
a blanket substitution breaks the second:

1. **Commands inside `SKILL.md`** — the skill body is substituted before the
   agent reads it, so the agent runs an absolute path. `${CLAUDE_PLUGIN_ROOT}` is
   correct here.
2. **Commands an operator or agent reads out of `README.md` / `DEBUG.md` and runs
   in a shell** — including the documented dashboard loop,
   `watch -n 5 'bash $PR_PIPELINE_HOME/skills/status/status.sh'`. These files are
   read as plain text, by a human in a separate terminal or by an agent via
   `Read`; nothing substitutes them and nothing exports the variable. Written
   against `${CLAUDE_PLUGIN_ROOT}` the command resolves to
   `/skills/status/status.sh` and fails.

So the standalone surface gets a real entrypoint rather than a placeholder:

- A **launcher installed outside the cache** — `~/.claude/pr-pipeline/bin/` —
  written by setup, which resolves the current install path and executes the
  target script. It is written once and survives plugin upgrade because the
  resolution happens at run time, which a hardcoded export would not.
- `README.md`/`DEBUG.md` commands are rewritten against the launcher, and the
  `PR_PIPELINE_HOME` export is documented as the development-checkout path, still
  supported and no longer the only way to run anything.
- **Install smoke test:** run every documented standalone command with both
  `CLAUDE_PLUGIN_ROOT` and `PR_PIPELINE_HOME` unset. Each must either work or fail
  with a message naming the setup step — never silently resolve a root-relative
  path.

The `test_skill_contract.py` assertions that pinned the literal
`uv run python $PR_PIPELINE_HOME/skills/pr-pipeline-tick/lib/tick.py`, and the
path-shape check that encoded the same prefix, moved with the contract: the
skill-body assertions now pin `${CLAUDE_PLUGIN_ROOT}`, the path-shape regex
accepts either root, and `test_skill_bodies_resolve_the_install_through_the_plugin_root`
makes the split itself the invariant rather than a convention.

Caveat, documented in `DEBUG.md` under "The install path resolves in two
different places": a session already running holds a stale
`CLAUDE_PLUGIN_ROOT` after `/plugin marketplace update` installs a new cache
version, so it keeps executing the previous version's code with no error and no
version mismatch to notice.

### `review-response-hygiene`

The fixer prompt binds `.claude/skills/review-response-hygiene/SKILL.md`, and
`_bound_skills_resolve` (`tick.py:8475`) probes for it **inside the fixer's
worktree** via `skill_doc_path` (`tick.py:423`) against `SKILL_DOC_ROOT =
".claude/skills"`.

This is the coupling that makes the current pipeline non-independent, and it is
invisible while dogfooding: this repository ships that file at its own
`.claude/skills/`, so the probe passes here and fails on every adopter
repository, escalating every fix dispatch with `skill_unresolved`.

Fix: ship the skill with the plugin rather than expecting it in the target, and
resolve the bound doc against the plugin root instead of the worktree.

**This retires a documented property, and the trade is deliberate.**
`README.md:622-628` states the worktree probe is intentional: it runs against the
fixer's own checkout *"so a PR branch cut before the skill existed is caught
rather than waved through by the operator's newer checkout,"* and the whole
`skill_unresolved` escalation path — no lease, no relabel, no engine launched —
hangs off it failing. Plugin-root resolution makes that probe unconditionally
pass, so the property goes away.

It should. The property is only meaningful while the skill is *the target
repository's* file, where a PR branch can predate it or delete it. Once the
document ships with the pipeline, the operator's installed copy is the correct
one for every target by construction, and a per-branch check is asking a question
with one possible answer. What replaces it is a narrower failure: the bound doc
missing from the *install* is a packaging defect, and dispatch fails the same way
rather than silently launching an unguided fixer. The `skill_unresolved`
escalation stays; only what it probes moves.

`README.md` and `DEBUG.md` both describe the worktree probe and its recovery
procedure ("restore the skill doc on the PR branch, usually by rebasing"), which
stops being the recovery. Updating them is part of phase 1, not a follow-up —
see section 14.

**Where it lands: under `skills/`, published as a command.** With
`"skills": "./skills"` in the manifest, anything under that directory becomes a
namespaced, user-invocable plugin skill whose description loads into every
session's context — so this is a decision about what the plugin publishes, not
about where a file is filed. The alternative considered and rejected was
`resources/review-response-hygiene/`, kept out of the manifest and bound by path
only.

The owner's decision is `skills/`, on the grounds that the guidance these
documents carry is worth invoking directly and not only at the moment a fixer is
dispatched. The cost is real and accepted: one more description in every
session's context, and a command an operator can invoke in a state where it does
not apply.

`definition-of-done` ships on the same grounds and by the same route.

**One premise in that decision does not hold for dispatched engines, and the
binding is what makes it not matter.** `_ENGINE_ALLOWED_TOOLS_BASE` grants
`Read Edit Write` plus an exact `Bash` set — there is no `Skill` tool on it — so
a dispatched implementer or fixer cannot invoke a published skill by name no
matter where the file sits. Publishing reaches *interactive* sessions; the
engines reach it the way they already do, as a doc path the controller binds
into the prompt. Making an engine invoke one of these by name would mean
widening the engine allowlist, which is a separate decision from this one and is
not taken here.

L1 (section 10) asserts the resulting skill set as an equality, so an unintended
command is observable rather than assumed.

Open question for implementation, to be answered by a real engine run rather than
by a stub: the engine allowlist carries `Read` and no `Skill` tool, which is why
the binding is a doc path. Whether `Read` resolves an absolute path outside the
engine's worktree must be verified in L3, not L2 — a PATH-stubbed engine proves
nothing about the real engine's permissions.

### Interpreter resolution

`uv run python` is removed from the documented contract. The stated runtime
contract is stdlib-only on Python ≥ 3.11, so requiring `uv` to run the tick
contradicts it and adds a dependency nothing declares. (Not every documented
command needs `uv` today: `preflight.sh` and `status.sh` are bash and already
resolve an interpreter directly — the tick invocations are the ones that don't.)
During exploration, `uv run --python 3.13` also resolved an interpreter without
`tomllib` under a sandbox, so the indirection is not reliably correct either.

The tick commands resolve a ≥ 3.11 interpreter the way `status.sh` already does.
`uv` remains a fine way for a contributor to run the suite; it stops being a
requirement for an operator to run the pipeline.

**Shipped.** `skills/tick/resolve-python.sh` is the single definition of a
usable interpreter, sourced by `tick.sh`, `preflight.sh`, and `status.sh` so the
three cannot disagree about which Python the pipeline runs on. `tick.sh` is the
documented operator entry point — `bash ${CLAUDE_PLUGIN_ROOT}/skills/tick/tick.sh
[args]`, the same shape `preflight.sh` and `status.sh` already had — and it exits
`3` (the existing "required tool missing, nothing ran" code) with a message
naming the ≥ 3.11 requirement and `PR_PIPELINE_PYTHON`. `tick.py` and
`pipeline.py` carry the same requirement as a guard ahead of `import tomllib`,
for the invocation that did not come through a launcher.

## 10. Validating the independence claim

Staged, cheapest first, ordered so a failure at L(n) makes L(n+1) meaningless.

The ladder's weakness is that cheap stages cannot prove the expensive properties.
A PATH-stubbed engine exercises the controller, not the engine's permissions; and
a happy-path end-to-end run never reaches the fixer, which is where the worst
coupling lives. The stages below are scoped accordingly.

### L0 — static

Clean clone into a temporary directory; run the suite there. Grep that no runtime
path resolves to a developer home directory or to this repository by name. Assert
the section 2 rule: no config read from inside a target tree.

Current baseline: 1328 passed, 10 skipped. One failure and seven errors appear
under a restrictive sandbox, all from `ps` start-time reads being blocked
(`test_status.py:63`, "cannot read the child's start time"); they are
environmental, not code defects.

### L1 — plugin install smoke

`/plugin marketplace add <clone>`, `/plugin install`, then confirm:
`${CLAUDE_PLUGIN_ROOT}` resolves inside a skill body; the dashboard runs **in an
unrelated repository** and reports an empty queue rather than crashing; and the
invocable skills are **exactly** the intended set under their real namespaced
names — `/pr-pipeline:tick`, `/pr-pipeline:status`,
`/pr-pipeline:definition-of-done`, plus `/pr-pipeline:setup` and
`/pr-pipeline:review-response-hygiene` once those land.

Exactly, not at-least: an unintended command is the observable symptom of a
misplaced file, and an assertion that only checks presence cannot see it.
`test_published_skill_set_is_exactly_the_intended_commands` is the static half
of this, so between the two a drift is caught before the install.

### L2 — stubbed tick, controller only

A scratch GitHub repository with a seeded issue at `ai:available`; `claude` and
`codex` intercepted by PATH stubs emitting canned completion sentinels — the same
seam CI already uses for `codex`, noting that the CI stub only prints a version
and is not itself a lifecycle test. Verify harvest → dispatch → label
transitions, worktree creation, and evidence archival at zero token cost.

What this proves: the controller's target resolution, label vocabulary, base-ref
handling, and config layering work against a repository that is not this one.
What it cannot prove: anything about real engine permissions.

### L3 — real engines, two targets, full fix cycle

The stage that actually proves independence. Requirements, each closing a gap a
thinner version would leave open:

- **A real fixer cycle**: implement → PR → review that *rejects* → fixer →
  re-review → `ai:ready-for-human`. A run that passes review first time never
  exercises `_bound_skills_resolve` and would miss the hygiene binding entirely —
  the single most important coupling in this document.
- **Real `Read` resolution** of the plugin-root skill doc by a real engine.
- **Two target repositories with different configuration**, to prove
  per-repository isolation. One target cannot.
- **A target whose default branch is not `main`**, for section 6.
- **An issue filed from a different account**, for the section 4 gate.
Throwaway targets are required rather than convenient: run against this
repository, none of it can detect self-coupling.

### L3-adversarial — phase 2, and not a phase 1 gate

These fixtures test mechanisms phase 1 deliberately does not build (section 14),
so they are listed separately and the phase 1 release claim does not include
them. Running them against phase 1 would produce expected failures, and a
ladder whose stated acceptance criteria cannot pass is a ladder an implementer
will quietly waive:

- **Hostile steering fixtures**: PRs that add `.claude/settings.json`,
  `.mcp.json`, a project skill carrying `allowed-tools`, a hook script change,
  and `AGENTS.override.md` — each must be escalated or neutralised per section 5.
  Requires the phase 2 trusted-input allowlist.

Phase 1 records the expected outcome of each as a known gap with the accepted
exposure from section 14, rather than asserting a pass. Phase 2 is not done until
all of them pass.

### L4 — upgrade survival

Confirm both that A's configuration survived **and that the newly installed
plugin code is the code that runs**. A marketplace refresh completing is not
evidence of either, and refreshing the marketplace is a different operation from
updating the installed plugin, so the procedure is explicit:

1. Bump the version in `.claude-plugin/plugin.json`. That field, with the
   marketplace metadata, is the update key — a standalone `VERSION` file is a
   human-readable mirror and must be generated from it, never the source.
2. `/plugin marketplace update <marketplace>` to refresh the catalog.
3. `/plugin update pr-pipeline` — the qualified update of the installed plugin.
4. Start a **fresh session**. A session already running holds a stale
   `CLAUDE_PLUGIN_ROOT` and would report the old version correctly.
5. Assert the version the running skill reports equals the bumped one, and that
   `~/.claude/pr-pipeline/config.toml` is unchanged.

The version assertion is what makes step 3 falsifiable: if the running code is
still the old cache entry, the marker says so.

## 11. Distribution

- Root `README.md` leading with the install commands and the setup step.
- `VERSION` and `CHANGELOG.md`.
- A CI job validating the plugin manifests, so a malformed `plugin.json` fails
  here rather than on an adopter's machine.

This section lists the artifacts. Section 15 covers what goes in them, what the
project claims about itself, and the gate that has to pass before any of it is
published.

## 12. Open questions

1. **Engine process isolation** (section 2). Implementer and fixer engines run
   with A's full environment and filesystem access, so a granted verification
   command can execute PR-controlled code that tampers with operator config or
   credentials. The reviewer already has a scrub-dir pattern to borrow. Out of
   scope here. This is the layer where containment belongs — section 2 decides
   where config *lives*, and no orchestrator-side gate substitutes for isolation.
2. **What `--allowedTools` actually constrains under each permission mode**, and
   whether project settings sources can be disabled outright at launch
   (section 5). Partly resolved: CLI `2.1.270` exposes `--setting-sources`,
   `--strict-mcp-config`, and `--tools`, so the mechanism for neutralising
   PR-supplied settings, MCP servers, and tool grants at launch exists. What
   remains is their combined runtime behaviour under each permission mode, to be
   measured against the installed CLI rather than inferred from the flag list.
   Determines whether the trusted-input surface is policed or neutralised.
3. **What happens to an unapproved command in a headless `auto` run**
   (section 5). `--permission-prompts` defaults to `host`, and dispatch supplies
   no host and no `--permission-prompt-tool`, so a classifier that does not
   auto-approve may deny, may prompt into nothing, or may hang. Section 8's probe
   measures the pass case; this question is what the failure case *is*, per route.
   It bounds how much of section 7's empty-`verify_tools` decision is load-bearing.
4. **Skill naming and placement under the plugin namespace** (section 9).
   **Answered.** The skills are renamed to `tick`, `status`, and `setup`, for
   `/pr-pipeline:tick` and siblings; the *directory* name is what the harness
   namespaces, which was measured rather than assumed (section 9). And
   `review-response-hygiene` lives **under** the manifest's `skills` entry as a
   published command, as does `definition-of-done` — the owner's call, on the
   grounds that the guidance is worth invoking directly. The cost accepted is one
   description per skill in every session's context.

   What the decision does *not* buy: a dispatched engine still cannot invoke
   them. The engine allowlist carries no `Skill` tool, so the controller binds
   these documents by path either way, and publishing them reaches interactive
   sessions only.
5. Whether `[scan] repos` belongs in this change or is deferred until an operator
   wants the restriction. The unrestricted default ships either way.

## 13. Review history

### First round

An independent review by Codex (`gpt-6-astra`) against the first draft was
verified claim by claim against the source. Confirmed by direct inspection and
incorporated above:

- `base_ref` hardcoded to `"main"` (`tick.py:9608`, default at `:6801`).
- No `ai:` label provisioning anywhere in the tree.
- `touches_review_policy` and `trusted_review_candidate` have one caller each,
  both reviewer-only; fixer dispatch rechecks neither. (The fourth round removed
  the section built on this — see below — but the observation itself held.)
- `status.sh:44-45` `sed`-parses the install-relative `config.toml` for
  `[target] repo`, so deleting that key breaks the dashboard.
- `gh()` (`tick.py:1004`) forwards arguments with no `--repo`.
- Verification commands execute PR-controlled code, which is why section 2 makes
  no containment claim.
- The policy gate's missing surface is far wider than two settings files.
- The earlier L0–L4 ladder could pass with the hygiene binding untested.

Corrections accepted against the earlier draft:

- `_DEFAULT_VERIFY_TOOLS` starts at `tick.py:766`, not 741; the issue query's
  JSON fields are at `:9433`.
- "An adopter without `uv` cannot run a single command in the README" was false:
  `preflight.sh` and `status.sh` already resolve an interpreter directly.
- Reviewers do not inherit `_engine_env()`; their credentials are scrubbed, so
  the environment claim applies to implementer and fixer only.
- "Largely inert under `dontAsk`" overstated what `--allowedTools` excludes.

One finding was argued and not adopted: that empty `verify_tools` should block
promotion rather than dispatch with a warning. The operator's decision stands;
section 7 records the objection and hardens the warning into durable evidence
instead.

### Second round

A second Codex review of the written design. Four findings, each verified against
the source before being accepted:

- **Phase 1 could not pass its own acceptance criteria.** L3 required hostile
  steering fixtures and a post-review force-push while section 14 deferred both
  mechanisms to phase 2. Confirmed by reading the two sections together. Fixed by
  splitting L3: the functional cycle gates phase 1, the adversarial fixtures gate
  phase 2, and phase 1 records their expected failure as a known gap rather than
  claiming a pass.
- **`${CLAUDE_PLUGIN_ROOT}` does not reach a shell.** Verified directly:
  `echo "${CLAUDE_PLUGIN_ROOT}"` from a Bash tool call on CLI `2.1.270` prints
  empty, so the substitution is a content-load operation, not an export. The
  documented dashboard loop (`README.md:44`) is run by a human in a separate
  terminal and would have resolved to a root-relative path. Fixed with a
  run-time-resolving launcher outside the cache and a smoke test with both path
  variables unset.
- **The configuration contract was incomplete.** Confirmed against `config.toml`
  and the accessors: the runtime reads `[target] repo`/`trusted_author`,
  `[policy] allow_touching`/`checklist_paths`, and `[engine] verify_tools`
  independently, while the design introduced `[defaults]`, `[scan]`, and
  `[repos.*]` with no mapping, no field for the confirmed operator login, no
  field for the permitted issue authors section 4 promises, and no per-repository
  policy nesting. Fixed with a complete schema, an explicit mapping table, a
  single shared resolver, and list-replacement and fail-closed rules.
- **A shared predicate does not fix the pagination starvation.** Confirmed: the
  issue query and the cadence scan both read one 200-item page
  (`_ACTIONABLE_QUEUE_SCAN_LIMIT`), `gh issue list` returns newest first, and
  `_queue_page_has_dispatchable` treats a full page as possibly-actionable. Fixed
  by filtering server-side with `gh issue list --author` in the bounded case,
  paginating with a progress contract in the unbounded one, and requiring an
  incomplete read to report unknown rather than quiescent.

Two smaller corrections were accepted and applied: the L4 upgrade procedure now
distinguishes a marketplace refresh from a qualified plugin update and names
`plugin.json` as the authoritative version field, and the `auto` capability check
is a real probe of the configured route with three explicit failure outcomes,
ordered before an empty `verify_tools` is accepted.

The review's own validation run (`1345 passed, 1 skipped`) and the baseline
recorded for this branch (`1328 passed, 10 skipped`, 1 failure and 7 errors) sum
to the same 1346 collected tests, which confirms those failures as the
sandbox-blocked process reads they were diagnosed as, not defects.

### Third round

A principal-engineer review of this document on the open pull request. Four
blockers, each an internal contradiction rather than a disputed judgement, and
each verified against the source before being accepted:

- **Section 4's server-side filter erased section 4's own disposition contract.**
  "Skipped, with a tick-log entry" is unachievable once the query is narrowed by
  `--author`, because the tick never reads the issue it would log. The design was
  fixing section 6's silent-dark-item hazard while re-creating it one section
  earlier. Fixed with a bounded count read and a dashboard row, and by saying
  plainly that invisibility is what the filter costs.
- **"One resolver, one result" could not include `status.sh`.** Confirmed against
  `status.sh:32-38`: it must run before an interpreter is resolved, so it cannot
  call a Python resolver, and a bash reimplementation is the second implementation
  the rule exists to forbid. Fixed by scoping the rule to Python callers, moving
  the dashboard's target resolution to the git remote (which needs no TOML at
  all), and pinning the residual duplication with an agreement test.
- **The `auto` graceful-degradation claim was contradicted by this project's own
  docs.** `SKILL.md:24` says "a classifier denial fails the run"; `README.md:644`
  agrees. Reaching the classifier is not being permitted. Confirmed further
  against `claude --help` on `2.1.270`: `--permission-prompts` defaults to `host`
  and `build_engine_cmd` (`tick.py:959-964`) supplies no host, so the headless
  failure path is unmeasured. Fixed by reducing the claim to "better for approved
  commands, no worse for the rest," strengthening section 8's probe into one that
  runs an unapproved command, and adding open question 12.3.
- **Moving the hygiene-skill resolution to the plugin root silently retired a
  documented property.** `README.md:622-628` states the worktree probe
  deliberately catches a PR branch cut before the skill existed. Fixed by arguing
  the trade explicitly, and by putting the `README.md`/`DEBUG.md` reconciliation
  in phase 1 rather than leaving it to be discovered.

Five majors were accepted and applied: `permission_mode` had no config key
though section 8 requires recording one (confirmed CLI-only at `tick.py:12633`,
`:12699`); section 3's fail-closed rule and section 7's dispatch-anyway decision
composed into a config typo silently producing an unverified
`ai:ready-for-human`; the environment layer still named `PIPELINE_REPO` as
overriding a key the design deletes; section 6 asserted in the present tense that
this repository carries three unknown `ai:` labels, which the owner has since
reconciled; and moving `review-response-hygiene` under `skills/` would publish it
as a fourth invocable command, which is not what a doc-path binding wants.

Six nits were applied: the header pointed at section 12 for the review history,
`tick.py:12009` is a blank line, `status.sh:38` named a comment rather than the
`sed` parse, two review anchors were commits no published ref reaches, "sections
9", and "four keys" introducing a list of five.

### Fourth round

Owner review on the open pull request. This round cut material rather than adding
it; every change below is a reduction in scope, and two of them reverse decisions
the earlier rounds had argued for.

- **Section 2 was overselling a placement decision as a trust boundary.** The
  config move is forced by the plugin cache hash alone. Preventing a model from
  scripting around a permission rule is the engine and model layer's problem, not
  something this design can claim. Rewritten as "Configuration ownership" with
  the residual exposure stated as a limit rather than an invariant.
- **`allow_touching` now defaults to `true`.** The gate only does work when
  someone other than the operator can put code in front of the reviewer, which
  phase 1 does not permit; against that, it escalates nearly every PR on a
  repository the pipeline develops itself. The default is coupled to the deferred
  author-widening item so the two move together.
- **The issue-author knobs come out of phase 1 entirely.** Operator-only pickup
  is the whole gate. With a single bounded `--author` query, the starvation and
  false-wakefulness analysis the third round added has no way to occur, so it
  moves to the deferred item where it applies.
- **`status.sh` reads no config at all**, so the third round's bash/Python
  agreement test — a duplication pinned rather than removed — is deleted along
  with the duplication.
- **Fixer-dispatch revalidation is removed, not deferred.** Verified against the
  source: review harvest already discards a review whose head moved under it
  (`head_advanced`, `pipeline.py:1110`), so the window the section described is
  seconds wide rather than open-ended. The owner's framing settles the rest — the
  fixer should judge each finding against the code in front of it rather than
  execute the reviewer's list, which makes drift detection unnecessary. What
  survives is two small runtime changes recorded as a standalone issue.

The through-line is that three of these were the design solving problems it had
created one section earlier, and the fourth was the orchestrator compensating for
judgement that belongs to the agent.

## 14. Sequencing: a functional pipeline first

The review turned up two different kinds of defect, and they do not belong in the
same change. **Phase 1 makes the pipeline work on a repository that is not
itself. Phase 2 hardens what an engine launched inside one can do.** Phase 1 is
shippable without phase 2; phase 2 is not worth designing against a pipeline that
cannot yet complete a cycle on an adopter's repo.

### Phase 1 — functional independence

Everything required for A to install the plugin and drive B, C, and D:

- Plugin manifests; `${CLAUDE_PLUGIN_ROOT}` resolution; skill naming and the
  placement of `review-response-hygiene` (section 9).
- `review-response-hygiene` shipped with the plugin and resolved from the plugin
  root — the coupling that currently makes every fixer dispatch fail on an adopter
  repo — **with `README.md` and `DEBUG.md` updated**, since both document the
  worktree probe and a recovery procedure that this retires (section 9).
- Operator-owned layered config, restructured packaged `config.toml`, and one
  canonical target threaded through dispatch, harvest, cadence, mutation, and
  `status.sh` — which drops its config parse entirely and resolves the target
  from the git remote (section 3).
- Adoption prerequisites: default-branch resolution, label provisioning at
  install, repository routing (section 6).
- `pr-pipeline-setup`, including host-prerequisite checks and the `auto` route
  probe that runs an unapproved command rather than just starting (section 8).
- `verify_tools` ships empty; a *deliberately* empty list dispatches with a
  durable warning, a malformed one is a startup error (sections 3 and 7).
- Issue-author trust gate: operator-only pickup, filtered at the query, applied
  at targeted runs too, with the bounded skipped-issue count that keeps the
  server-side filter from going silent (section 4). Widening the author set is
  explicitly *not* in phase 1.
- `auto` as the default permission mode, `permission_mode` as a config key, and
  the per-route fallback the probe may force (sections 3, 5, 8).
- ~~Interpreter resolution without `uv` (section 9).~~ **Done** — `tick.sh` and the shared `resolve-python.sh`.
- A standalone entrypoint for the shell-run commands (section 9) — the plugin-root
  substitution covers skill bodies only.
- Validation L0 through L4, **L3-adversarial excepted** (section 10). Phase 1's
  release claim is the functional ladder; the adversarial fixtures are listed with
  their expected outcome as known gaps, and passing them is phase 2's gate.

### Phase 2 — permission and isolation hardening

Deferred deliberately, recorded so it is not lost:

- The trusted-input allowlist for PR-authored engine configuration, or
  neutralising project settings sources at launch (section 5).
- Engine process isolation for implementer and fixer (open question 12.1). This
  is where containment actually lives; section 2 is a placement decision, not a
  boundary.

Phase 1 ships `auto` without the phase 2 allowlist, which is a deliberate
acceptance rather than an oversight: the exposure pre-dates `auto` and is bounded
by A's own deny list, and A is the only author of issues the pipeline picks up
(section 4), so the hostile-PR path requires a PR from someone A already trusts
enough to review. Phase 2 is what makes that boundary hold for a repository with
outside contributors — which is the point at which it stops being optional, and
the same point at which section 4's deferred author-widening item comes due.

### Outside both phases

Two items surfaced by this design are runtime work with no dependency on plugin
distribution, and belong in their own issues rather than in either phase:

- **Fixer judgement of stale findings** (section 5): a third *no change needed*
  disposition and a legal "nothing to push" completion.
- **Widening the issue-author set** (section 4), carrying the
  `allow_touching = false` restore with it.

### Publication

Section 15. Gated on phase 1 and on L3 specifically, and independent of phase 2:
what a stranger needs in order to find the project, understand what it claims,
and trust it enough to point at a repository they care about. It is sequenced
after phase 1 rather than alongside it because every artifact it produces —
the install commands, the run capture, the cost figure — is a description of
behaviour phase 1 creates. Writing it earlier would document an intention.

## 15. Publication

Phase 1 makes the pipeline work for someone else. This section is what makes
someone else willing to try it. It is separated from section 11 because those are
artifacts and these are claims, and a claim this project cannot support is more
expensive than a missing file.

### The gate

**Nothing here ships until L3 passes.** The ordering is not stylistic. A
one-command install that then escalates on the first dispatch is worse than no
install at all: it converts "not ready yet", which a reader forgives, into
"broken", which a reader does not retry. The 2026 expectation of an installable
plugin is an argument for finishing phase 1 *before* publishing, not for
publishing something thinner sooner.

L3 is also the only source of three artifacts this section needs and cannot
fabricate: a terminal capture of a real run, an order-of-magnitude token and
dollar figure for one implement → review → fix → re-review cycle, and the list of
escalations the run actually produced.

### What the project claims

The orchestration primitives here are commodity. A community index surveyed in
September 2026 listed 197 agent orchestrators; 85+ use git worktrees, 63+ claim
unattended operation, and 18+ drive GitHub issues to pull requests. Worktree
isolation plus a poll loop differentiates nothing and should not be led with.

Three things do differentiate, in the order they should be claimed:

1. **Custody.** Engines cannot move an `ai:` label. They report through a typed
   completion sentinel and the orchestrator applies the transition, so an agent
   cannot mark its own work done. *Evidence bound: five neighbouring projects
   were read closely and the rest of the index was not audited. The supportable
   claim is "I have not found another project that enforces this rather than
   treating it as convention," not "no other project does." The stronger form is
   disproved by one counterexample and takes the rest of the positioning with
   it.*
2. **GitHub holds the whole state machine, not a task tag.** Labels carry stage;
   issue and PR threads carry context. No local database, no board, no UI. The
   orchestrator holds no memory and neither do the engines, so a tick killed
   mid-flight leaves state recoverable from `gh issue list`. Among the projects
   read closely: one keeps state in in-memory JSON, one in a local web board, one
   uses labels as a task tag rather than as the state machine.
3. **The design/execution split.** Spec design is a human-AI conversation that
   does not automate. Execution is verifiable *against* that conversation, and
   does. Externalising the design to GitHub is what makes the second half
   possible: a reviewer session that never saw the implementer's transcript can
   still check the work against the requirement, because the requirement is in
   the issue rather than in the implementer's context. Fresh context per dispatch
   is therefore a property being bought, not a limitation being tolerated.

The adjacent tooling does not close this. Spec-authoring tools stop at the spec.
Ticket-executing orchestrators start at the ticket and are indifferent to where
it came from, so none of them can report that an implementation drifted from its
plan — they never had the plan.

**Do not use the phrase "externalised spec-driven development" in any
user-facing copy.** It compresses the idea above into a term that only parses for
someone who already understands it. Lead with the mechanism:

> pr-pipeline drives GitHub issues to reviewed PRs unattended. Every stage is a
> label, every agent starts with fresh context and reads the issue and PR thread
> to know what to do, and no agent is allowed to move its own label.
>
> Claude implements, Codex reviews, you merge.

### Escalation legibility is a publication requirement

A label with no stated reason is indistinguishable from a crash. An operator who
installs this, sees several items sitting at `ai:needs-human-decision` or
`ai:max-iterations` with nothing explaining why, and no further movement, has no
way to tell a working escalation from a failure — and in a category with 197
alternatives the rational response is to uninstall rather than investigate. This
is the single most likely way to lose an adopter who got past installation, and
it is cheaper to fix than to recover from.

Required before publication:

- **Audit first.** There are ~30 `LABEL_NEEDS_HUMAN` / `LABEL_MAX_ITERATIONS`
  sites and 162 `reason=` call sites in `tick.py`. `pipeline.py:2035` carries an
  `escalation_reason` on the fixer-run comment renderer, which establishes that
  at least one path does this and nothing about the other paths. The deliverable
  is the coverage map; closing the gaps is the follow-on.
- **Every escalation posts a reason comment**, not only a label.
- **Posting is idempotent.** The tick is restartable by design, so a naive post
  duplicates on re-harvest.

The escalations L3 produces are the test data for this. If the operator running
L3 cannot tell from the repository alone why each one stopped, neither can an
adopter.

### Reviewer tamper check

One paragraph in `lib/prompts/reviewer.txt`: diff tests against the pinned
merge-base, and treat a deleted test, a skipped test, or a weakened assertion as
a blocker finding.

This is cheap because the plumbing exists. `dispatch.json` already pins
`base_sha`, the merge-base the reviewer's diff is anchored to, and harvest
already validates that anchor rather than silently falling back
(`pipeline.py:2757`). Without it, the honest description of an unattended
pipeline is that its acceptance gate can be satisfied by deleting the test that
fails — which, given that this project's own headline is custody, is the
inconsistency a hostile reader will find first.

Explicitly **not** in scope: a reproduction gate requiring tests to fail at the
merge-base and pass at the tip. It is repo-specific, expensive, and produces
false blocks on any target with non-hermetic tests. Revisit only if adopters
report distrusting the reviewer.

### Repository presentation

- **Root `README.md`** (artifact listed in section 11). Contents, in order: the
  positioning lines above; the L3 terminal capture showing labels actually
  moving; install and prerequisites, stating plainly that `python3` resolves to
  3.9 on a stock macOS box and under Anaconda and will fail on `tomllib`; the
  label table; a comparison table whose columns are *where state lives*, *who is
  allowed to move it*, and *what stage it stops at*, since those are the axes
  where the answers differ; the non-goals verbatim from `docs/architecture.md`;
  the sentence that the operator applying `ai:available` is itself the trust gate
  and nothing self-dispatches; how to stop a run and clean up
  `.claude/worktrees/`; and the cost and blast-radius summary below.
- **Cost and blast radius.** Nobody runs an unattended agent loop without knowing
  what it spends and what it can reach. State the human merge gate, the
  `MAX_IN_FLIGHT` cap, and the L3 cost figure. On pushes, state only what is
  enforced: `implementer.txt` declares `git push origin HEAD` the only allowed
  push form, and the worktree is on a feature branch. That is a prompt convention
  plus branch isolation, not a guard. Either add a guard or describe it as what
  it is — a safety claim falsified once would take the custody claim down with
  it.
- **`docs/architecture.md` split.** 1811 lines with no root README inverts the
  reading order a visitor needs. Front section of roughly 200 lines answering
  what this is, what owns each decision, and where the boundaries sit; everything
  requiring a line-anchored source reference moves to the deep-dive.
- **`CONTRIBUTING.md` and `.github/ISSUE_TEMPLATE/`.** The success condition for
  publishing is useful issues from strangers, and nothing currently shapes them.
  The templates should ask for the target repository's language and default
  branch, since section 6 and section 7 make both load-bearing.

### What is deliberately not done before publishing

Phase 2 in full: the trusted-input allowlist and engine process isolation. The exposure they close is bounded by the section 4
trust gate, and section 14 already records why shipping without them is a
considered acceptance rather than an oversight. The README states the boundary
in the blast-radius section rather than implying it does not exist.

### Success criteria

- An operator who is not the author installs the plugin, runs setup, points it at
  a repository they own, and reaches either a pull request or an escalation that
  states its reason — without reading `docs/`.
- No escalation reaches an operator without a posted explanation.
- The README's first line survives being read on a GitHub search results page,
  with no surrounding context.
- *Diagnostic rather than a target:* if two or more of the first twenty filed
  issues concern distrusting the reviewer's verdict rather than installation or
  operation, the reproduction gate deferred above returns to scope.
