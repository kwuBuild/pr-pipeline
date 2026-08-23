# Design: Extract PR Pipeline Tick Bash → Python

Status: IMPLEMENTED (all phases: HARVEST with side effects + DISPATCH with fallback; parity tests + snapshot tests)
Created: 2026-06-28
Mode: Developer tooling (workflow automation — not product code)

## Context

The `pr-pipeline-tick` skill's SKILL.md is 1,919 lines with 25 inlined bash code
blocks. The AI reads and executes each block sequentially as separate `Bash` tool
calls — the AI is the control flow. This execution model has proven broken in
practice. Running the skill surfaced five categories of runtime failure:

1. **No continuous shell.** Each `Bash` call is a fresh process. Every variable
   (`TARGET_TYPE`, `TARGET_NUM`, `recon_prs`, `pid`, `identity`, `reconcile_failed`)
   must be re-declared at the top of every block. The SKILL.md is written as if it
   runs in one long-lived shell, but the runtime gives it 25 independent shells.
2. **Shell dialect mismatch.** The SKILL.md uses `shopt -s nullglob` (bash), but
   the environment is zsh. Bare globs like `"$WT_ROOT"/pr-*/` error out when no
   matches exist, requiring `find` workarounds.
3. **UV cache corruption.** Sandbox and non-sandbox environments share
   `/tmp/claude/uv-cache/`. Sandboxed cache entries are unreadable outside the
   sandbox, so `uv run python` fails with opaque `No such file or directory` errors
   on `.whl` metadata. Each tick makes ~47 `uv run python` calls (37 to
   `pipeline.py` + ~10 to `process_control.py`), each a cache-corruption dice roll.
4. **Environment ambiguity.** `DEEPSEEK_API_KEY` vs `DEEPSEEK_API_KEYPATH` — the
   skill's `elif` chain handles both but must be re-executed correctly in every
   block. A missed block inherits the wrong var and engine launch fails silently.
5. **Word-splitting bugs.** `for N in $multiline_var` doesn't reliably split on
   newlines across shells. `while IFS= read -r` works but is inconsistently applied.

The root cause is the execution model: **the AI is treated as a persistent shell
session, but the runtime provides 25 independent shells with no shared state,
different dialects, and infrastructure quirks accumulating across calls.**

The bash also calls `pipeline.py` via `uv run python` subprocess 37 times per tick,
parsing JSON output with `jq`. This is slow (25 AI round-trips), token-heavy (full
SKILL.md loaded each invocation), untestable (the orchestration glue has zero test
coverage), and fragile (bash error handling requires an explicit capture-check-use
contract the skill has to teach the AI to follow).

The decision logic is already in tested Python (`lib/pipeline.py`, 1,756 lines),
backed by `lib/test_pipeline.py` (2,450 lines of tests). The bash is glue — `gh`/`git`
calls, file I/O, loops, and label transitions. This extraction collapses it into a
single Python process that imports `pipeline.py` directly:
- **1 process, not 25.** One `uv run python lib/tick.py` call. Variables are normal
  Python locals that live for the duration of `main()`. No state re-establishment.
- **47 `uv run python` calls → 1.** Direct imports eliminate all subprocess spawns
  to `pipeline.py` and `process_control.py`. The UV cache corruption surface shrinks
  from 47 dice rolls per tick to 1.
- **No shell dialect, no word-splitting.** `pathlib` globs, `json.loads()` parsing,
  Python iteration — no `shopt` vs zsh, no `for` vs `while read`.
- **Testable.** The orchestration glue gets the same unit-test treatment the decision
  logic already has.

**User direction:** simplify while extracting (deduplicate the 3 near-identical
reconcile loops into a shared function), and use direct Python imports instead of
keeping pipeline.py as a CLI boundary.

**Adversarial review:** plan reviewed by 3 subagents (correctness, risk, architecture
lenses). Two blockers and 13 major findings addressed — see
[Review Findings](#review-findings) below.

## Invariants (preserved from bash)

These invariants are load-bearing for a money-spending unattended control plane.
Every one is enforced in Python code, not in prose:

1. **HARVEST then DISPATCH, unconditionally** — every tick runs both phases in order.
2. **Fail closed on every external read** — a failed `gh`/`git` call never yields a
   value mistaken for valid empty state. `CalledProcessError` propagates; only
   explicitly caught at the per-PR loop boundary (`continue` = retry next tick).
3. **Lease-before-label** — the dispatch marker lease is written and confirmed BEFORE
   the GitHub label is flipped. Lease write failure → no label flip, no spawn.
   Label flip failure → lease deleted, no spawn. Applies to all 3 dispatch paths.
4. **Kill only on proven identity** — `os.kill()` is only called when
   `verify_identity()` returns `"matched"`. "unknown" retains custody; "mismatched"
   confirms reuse and never signals.
5. **can_dispatch_now fails CLOSED** — a single corrupt marker, classify-dispatch
   crash, or admission read error blocks the ENTIRE dispatch phase, not just that
   one PR. One bad marker must never cause undercounting of a live paid engine.
6. **Scratch deletion gated on label edit** — the dispatch marker (duplicate-dispatch
   guard) survives a failed remote label transition. Only deleted after `gh pr edit`
   / `gh issue edit` confirms success.
7. **Admission counts this machine's engines** — the spend ceiling is not
   reviewer-only, but it is local-only. Admission counts local `live` /
   `launching` / `hung` markers, plus any marker whose process group is still
   alive, across all three stages. A markerless in-flight label consumes no
   slot: `ai:implementing` is also the documented manual pickup label, so
   counting it let four hand-set labels starve dispatch permanently (#534,
   #663). A lost marker still cannot prove the engine is gone — that is why
   the tick never *clears* such a label (#592/#604); it simply does not bill
   this machine for it.
8. **`gh` exits 0 on empty results** — `gh pr list --label ai:reviewing` with no
   matches exits 0 with empty stdout. Non-zero exit = API/network/auth error, not
   "nothing found." This is load-bearing for distinguishing valid-empty from crash.

## Design

### File changes

| File | Change |
|---|---|
| `lib/pipeline.py` | Export 16 public functions + TypedDicts alongside CLI wrappers |
| `lib/tick.py` (new) | Orchestrator: HARVEST → DISPATCH loop, ~2,750 lines |
| `SKILL.md` | Thin to ~60 line invocation stub with debugging section |
| `lib/test_tick.py` (new) | Tests for the orchestrator (mock subprocess) |
| `lib/test_skill_contract.py` | Replace grep-based tests with behavioral tests, categorize by type |
| `lib/test_pipeline.py` | Add parity tests: direct-call output ≡ CLI round-trip |
| `docs/planning/designs/pr-pipeline-tick.md` | Update to reflect Python orchestration (remove bash-centric sections) |

### Module boundary

`tick.py` lives in `lib/tick.py` alongside `pipeline.py` and `process_control.py`.
This eliminates `sys.path` hacks: `tick.py` does `import pipeline` and
`import process_control` directly. `test_tick.py` (also in `lib/`) does
`import tick`. The SKILL.md invocation is:

```
uv run python .claude/skills/pr-pipeline-tick/lib/tick.py [--target pr:N | #N] [--impl-engine claude|claude-ds]
```

### 1. Export public functions from `pipeline.py`

For each of the 16 subcommands, add a public function with keyword-only arguments
and typed return dicts. The CLI wrappers read from `args` + `sys.stdin`, call the
function, and print JSON. Existing tests pass unchanged.

**TypedDicts** (defined in `pipeline.py`, exported alongside functions):

```python
class HarvestResult(TypedDict):
    action: Literal["post_review", "drift_revert", "label_only", "error"]
    label: str
    # ... stage-specific fields

class ClassifyResult(TypedDict):
    state: Literal["live", "launching", "complete", "stale", "hung"]
    identity: Literal["matched", "mismatched", "unknown"]

class ReconcileResult(TypedDict):
    action: Literal["noop", "leave", "harvest", "escalate", "revert",
                     "kill_escalate", "cleanup", "kill_cleanup"]
    label: str
    kill: bool

@dataclass(frozen=True)
class AdmissionResult:
    can_dispatch: bool
    reason: str           # "" when True, else "max_in_flight" | "admission_error"
    in_flight_count: int
    max_in_flight: int

@dataclass(frozen=True)
class InFlightRef:
    kind: Literal["issue", "pr"]
    number: int
    label: Literal["ai:implementing", "ai:reviewing", "ai:fixing"]
```

**Public function signatures** (delegation pattern):

```python
def harvest_review(
    *, review: str, comments: str, pinned_base: str, current_base: str,
    pinned_head: str = "", current_head: str = "", pre_label: str = "",
    threshold: int = MAX_ITERATIONS,
) -> HarvestResult:
    ...

def _cmd_harvest_review(args: argparse.Namespace) -> int:
    result = harvest_review(
        review=args.review, comments=args.comments, pinned_base=args.pinned_base,
        current_base=args.current_base, pinned_head=args.pinned_head,
        current_head=args.current_head, pre_label=args.pre_label,
        threshold=args.threshold,
    )
    json.dump(result, sys.stdout)
    return 0
```

**stdin-based functions** — four functions currently read `sys.stdin`. Their public
signatures accept the data as explicit parameters:

| Function | stdin data | Public parameter |
|---|---|---|
| `touches_review_policy` | Newline-separated paths | `changed_paths: list[str]` |
| `validate_review_candidate` | JSON object with author, headRepository, isCrossRepository | `candidate: CandidateMetadata` (TypedDict) |
| `atomic_write` | Raw bytes to write | `data: str` |

The CLI wrappers read `sys.stdin`, pass to the function, and print the result.

The 15 functions: `harvest_review`, `validate_review`, `classify_dispatch`,
`atomic_write`, `touches_review_policy`, `validate_review_candidate`,
`validate_marker`, `harvest_implementation`, `harvest_fix`, `reconcile_dispatch`,
`validate_envelope`, `verify_identity`, `tick_log_open`, `tick_log_write`, `tick_log_tail`.
(`dispatch_admission` was the 16th until #663 retired it — see below.)

Module-level safety verified: `pipeline.py` (1,756 lines) has zero module-level side
effects — only stdlib imports, constant definitions, and function definitions. The
`main()` entry point is guarded by `if __name__ == "__main__"`. Safe to import from.

### 2. Use process_control.py functions directly

`process_control.py` already has public functions — no extraction needed. They are
called directly by `tick.py` with their native return types:

- `terminate_process(pid, *, grace_seconds, kill_grace_seconds, poll_interval) -> bool`
- `terminate_process_group(pgid, *, grace_seconds, kill_grace_seconds, poll_interval) -> bool`
- `get_process_start_time(pid: int) -> float | None`

Each has injectable dependencies (`is_alive`, `send_signal`, `monotonic`, `sleep`)
for testing. The existing CLI wrappers (`terminate`, `terminate-group`,
`get-start-time`) print JSON and remain available for bash consumers (status.sh,
preflight.sh), but `tick.py` calls the functions directly and uses the native return
types — no subprocess, no JSON round-trip.

### 3. `lib/tick.py` — the orchestrator

#### Repo-root resolution

First thing `main()` does: `git rev-parse --show-toplevel` → `os.chdir()`.
All paths (`WT_ROOT`, `TICK_LOG_DIR`, etc.) resolve against this root.
This prevents broken paths when invoked from a subdirectory.

#### gh/git helpers

Three helpers (not one) to prevent bugs at the type level:

```python
def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Raw gh invocation. check=False for expected-failure calls."""
    return subprocess.run(
        ["gh", *args], capture_output=True,
        encoding="utf-8", errors="replace",  # never crash on non-UTF-8
        check=check,
    )

def gh_json(*args: str) -> Any:
    """gh --json ... → parsed JSON. Raises on non-zero exit or parse failure."""
    result = gh(*args, check=True)
    return json.loads(result.stdout)

def gh_text(*args: str) -> str:
    """gh ... → stripped text. Raises on non-zero exit."""
    result = gh(*args, check=True)
    return result.stdout.strip()

def git(*args: str) -> str:
    """git ... → stripped stdout. Raises on non-zero exit."""
    result = subprocess.run(
        ["git", *args], capture_output=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return result.stdout.strip()
```

`encoding="utf-8", errors="replace"` prevents `UnicodeDecodeError` on non-UTF-8
bytes (CJK, emoji, smart quotes in issue bodies). Matches bash behavior (never
crashes on encoding). The `gh` CLI always emits UTF-8; `errors="replace"` is a
safety net.

#### `relabel()`

```python
def relabel(entity: str, number: int, from_label: str, to_label: str) -> None:
    """Transition a PR/issue label. Handles empty from_label (no --remove-label)."""
    if from_label == to_label:
        return
    if from_label:
        gh(entity, "edit", str(number), "--add-label", to_label,
           "--remove-label", from_label)
    else:
        gh(entity, "edit", str(number), "--add-label", to_label)
```

Entity routing: callers derive `"pr"` vs `"issue"` from `StageConfig.worktree_prefix`
via the `_gh_entity()` helper. This replaces the design's original `gh_edit_fn`
callable field on `StageConfig` — a simpler, deterministic mapping that avoids
embedding function references in a frozen dataclass.

#### `can_dispatch_now()` → `AdmissionResult`

```python
def can_dispatch_now(*, max_in_flight: int) -> AdmissionResult:
```

Iterates `WT_ROOT/pr-*/` and `WT_ROOT/issue-*/`, loads each `dispatch.json`,
performs identity-verified liveness check, calls `classify_dispatch()` directly,
collects states, and passes them to `in_flight_refs()`, keyed by
`(kind, number, stage label)`. **Fail-closed:** any single
`classify_dispatch()` crash or corrupt marker returns
`AdmissionResult(can_dispatch=False, reason="admission_error")` — one bad marker
blocks ALL dispatch for this tick.

`dispatch_admission()` / `in_flight_prs()` were the older PR-number-only
admission primitives, kept behind a `dispatch-admission` CLI subcommand after they stopped
driving the tick. #663 removed all three: once `in_flight_refs()` stopped
counting labels, they answered a question the system no longer asks and
returned the opposite verdict for the same repo state, which is exactly the
second contract an operator reaches for by mistake.

Admission takes no authoritative label parameter. The three label reads

- `gh issue list --label ai:implementing --state open`
- `gh pr list --label ai:reviewing --state open`
- `gh pr list --label ai:fixing --state open`

still run, but for two other jobs. In DISPATCH they are the per-item
*eligibility* input: each dispatcher skips an item already at an in-flight
label and appends the engines it launches, so all three share one view of what
this tick has taken. In the cadence observation they run as a fail-closed probe
on a surface the queue check cannot see — they go through GitHub's search
index, the queue probes go through the list API — and their value is discarded
there. Neither job is occupancy. A markerless `ai:implementing` issue or
`ai:fixing` PR is a manual pickup, another machine's dispatch, or a silently
dead engine; the tick leaves all three alone (#604) and none of them spends
this machine's budget.

Result file selection: for each worktree, check `result.json` first (implementer/fixer),
fall back to `review.json` (reviewer). This matches the bash priority logic.

#### `reconcile_engine()` — shared reconcile loop

Parameterized by a `StageConfig`:

```python
@dataclass(frozen=True)
class StageConfig:
    stage: Literal["review", "implement", "fix"]
    worktree_prefix: str                     # "pr" or "issue"
    attempts_key_prefix: str                 # "" or "issue-"
    result_file: str                         # "review.json" or "result.json"
    in_flight_label: str
    pre_label: str
    strict_dual_label: bool  # True for all stages (convergence re-read)
    tick_abort_on_enum_failure: bool  # True for reviewer (raise), False for others
```

Entity routing (`"pr"` vs `"issue"`) is derived from `worktree_prefix` by the
`_gh_entity()` helper rather than embedding callables in the dataclass — simpler,
deterministic, and avoids function references in a frozen dataclass. The original
design's `gh_list_fn`/`gh_view_fn`/`gh_edit_fn`/`harvest_fn` callable fields were
removed in favor of this approach. Per-stage harvest logic lives in standalone
functions (`harvest_reviewers()` / `harvest_implementers()` / `harvest_fixers()`).

`reconcile_engine()` handles:
1. Build reconcile set (union of authoritative label query + local markers)
2. Per-item loop with `pid = None` reset (Python scoping provides this naturally)
3. Lifecycle check (OPEN guard) — closed/merged → identity-verified terminate, cleanup, continue
4. Marker validation + identity-verified liveness + classify-dispatch — this runs
   BEFORE the dual-label decision so durable engine state, not GitHub label
   ordering, decides custody.
5. Dual-label handling — a live, hung, or completed marker whose in-flight or
   recorded pre-label is present recovers through that marker-owned label
   (live → leave, hung → identity-verified kill path, complete → harvest, which
   later converges the full remote set in one `gh edit` mutation plus a
   verification read). A hold label (`ai:needs-human-decision`, `ai:blocked` —
   never produced by the tick next to pipeline custody) is treated as human-set
   and outranks a completed marker: the completion is *published, not
   harvested* — evidence prepared, validated outcome posted, only the
   pipeline's own in-flight label released, the exact hold never mutated (#614,
   #640). It cannot be *proven* human-set — engines
   share the tick's GitHub token, so the timeline actor matches either way; the
   engine contract closes that route (see `docs/working/pr-guidelines.md`,
   "Pipeline label authority") and the runtime rule stays
   fail closed toward the human, because a human racing to stop a bad run must
   win. Everything else falls to strict escalation — `strict_dual_label=True`:
   converge to `ai:needs-human-decision` through the same single-mutation
   `gh edit` + verification-read helper harvest uses, and only drop scratch
   after the verified convergence. `strict_dual_label=False`: leave the labels
   untouched (noop; retry next tick).
6. `reconcile_dispatch()` call
7. Kill on matched identity when `kill=True`
8. Returns `ReconcileDecision` dict — **does not perform stage-specific side effects**
   (posting comments, bumping iteration, creating PRs). Those are the caller's
   responsibility.

**Dual-label convergence asymmetry:** the reviewer's stricter convergence re-read
(bash lines 316-348) is applied to ALL three stages in the extraction —
it's safer. The `strict_dual_label` parameter preserves the distinction for
documentation but defaults to True for all stages.

**Enumeration failure asymmetry:** the reviewer `exit 1` on `gh pr list --label
ai:reviewing` failure aborts the entire tick (including implementer/fixer harvest).
The implementer/fixer just set `reconcile_failed=1`. Unified to: all three set
`reconcile_failed=1` and continue. The admission cap re-checks all authoritative
in-flight labels freshly in the Phase 2 preamble, so any failed in-flight label
enumeration gates DISPATCH.

#### HARVEST phases (thin wrappers)

- `harvest_reviewers()` — calls `reconcile_engine()` with reviewer `StageConfig`,
  then for `action == "harvest"`: resolves base/head SHAs, fetches comments, calls
  `harvest_review()`, posts review comment, handles drift/replay/idempotency.
- `harvest_implementers()` — calls `reconcile_engine()` with implementer `StageConfig`,
  then for `action == "harvest"`: reads `result.json`, calls `harvest_implementation()`,
  creates PR, renames worktree `issue-N` → `pr-N`.
- `harvest_fixers()` — calls `reconcile_engine()` with fixer `StageConfig`,
  then for `action == "harvest"`: extracts pipeline review comment, counts rounds,
  calls `harvest_fix()`, bumps iteration counter, checks `MAX_ITERATIONS`.

#### DISPATCH phases

- `dispatch_reviewers()` — enumerate `ai:implemented` + `ai:revised` PRs, for each:
  `can_dispatch_now()` → trusted-candidate gate → resolve refs/SHAs → policy gate →
  create worktree (detached) → verify pin → write lease → flip label → spawn
  `engine_wrapper.py` via subprocess. Full atomic ownership via wrapper.
- `dispatch_implementers()` — enumerate `ai:available` issues, for each:
  `can_dispatch_now()` → resolve issue title/body → delete stale branch →
  create worktree (`-b agent/issue-<N>`) → assemble prompt → write lease →
  flip label → spawn `claude -p` (or `claude` with DeepSeek env for `claude-ds`) →
  **publish PID marker post-spawn** → wait → write completion envelope.
  Known gap: PID published after spawn (no wrapper). **Fallback on PID marker
  publish failure:** terminate engine → if successful, wait + relabel back +
  delete marker → continue. If termination fails, write fallback marker via
  temp-then-rename so next tick can reconcile. This is production safety code,
  not a deferred enhancement.
- `dispatch_fixers()` — enumerate `ai:changes-requested` PRs, for each:
  `can_dispatch_now()` → trusted-candidate gate → extract pipeline review
  comment (escalate if missing) → assemble fix prompt with review findings →
  reset worktree to PR branch → write lease → flip label → spawn `claude -p` →
  same PID-post-spawn + fallback pattern as implementer.

#### Engine launch isolation

Reviewer dispatch keeps the existing scrubbed environment contract: use `env -i`,
set `CODEX_HOME` to a tick-owned scrub dir with only a symlink to
`~/.codex/auth.json`, set `HOME` to the scrub dir, and pass only the variables the
reviewer needs. The `engine_wrapper.py` is invoked as a subprocess for reviewer
dispatch.

Implementer/fixer dispatch **does not** switch to `env -i` in this extraction.
Those engines must authenticate both to the model provider and to `git push origin
HEAD`; the current bash path relies on inherited local auth (`HOME` / git config,
`SSH_AUTH_SOCK`, provider env vars, and optional `DEEPSEEK_API_KEY` /
`DEEPSEEK_API_KEYPATH`). Stripping the environment without a tested replacement
would make the plan non-executable. This revision therefore preserves the inherited
implementer/fixer environment while keeping the existing bounded Claude tool
allowlist. A future isolation hardening may replace it only after it names the
minimal authenticated environment and passes the push-capable probe in the
verification plan.

#### Error handling

`subprocess.CalledProcessError` from `gh`/`git` is caught at the per-PR loop
boundary with `continue` (retry next tick). The `gh` CLI exits 0 on empty results
("no matching PRs") and non-zero only on API/network/auth errors — this
distinction is load-bearing and documented in a module-level comment.

#### Main entry point

```python
def main(target: str | None = None) -> int:
    os.chdir(git("rev-parse", "--show-toplevel"))
    tick_log = TickLog.open(TICK_LOG_DIR)

    # Phase 1: HARVEST
    reconcile_ok = True
    reconcile_ok &= harvest_reviewers(tick_log, target)
    reconcile_ok &= harvest_implementers(tick_log, target)
    reconcile_ok &= harvest_fixers(tick_log, target)

    # Phase 2: DISPATCH (gated on reconcile_ok)
    if reconcile_ok:
        authoritative_in_flight = read_authoritative_in_flight_labels()
        dispatch_reviewers(tick_log, target, authoritative_in_flight)
        dispatch_implementers(tick_log, target, authoritative_in_flight)
        dispatch_fixers(tick_log, target, authoritative_in_flight)
    else:
        tick_log.log("", "dispatch", "all", "skip_reconcile_failed",
                      "reconciliation or authoritative in-flight state untrustworthy")

    # Phase 3: Summary
    print_summary()

if __name__ == "__main__":
    raise SystemExit(main())
```

### 4. SKILL.md

Trimmed to ~60 lines:

```markdown
---
name: pr-pipeline-tick
description: Run one PR-pipeline tick ...
---

# PR Pipeline Tick

Drives the implement → review → fix → re-review loop.
Design: `docs/planning/designs/pr-pipeline-tick.md`.

## Invariants

(8 invariants listed above, kept in SKILL.md so the AI can reason about failures)

## Run

bash .claude/skills/pr-pipeline-tick/preflight.sh
uv run python .claude/skills/pr-pipeline-tick/lib/tick.py [--target pr:N | #N] [--impl-engine claude|claude-ds]

The preflight is required before the first reviewer dispatch after install or Codex
version drift. `tick.py` also refuses reviewer dispatch if its pinned Codex version
check fails, but the version check is not a replacement for the binary/schema
preflight.

## Debugging

- Decision logic: `lib/pipeline.py` docstrings
- Tick audit trail: `.claude/worktrees/.tick-log/tick-*.jsonl`
- Process control: `lib/process_control.py`
- Dashboard: `/pr-pipeline-status`

## Constants

MAX_IN_FLIGHT=3, REVIEW_TIMEOUT=1200s, trust boundaries live in lib/tick.py.
```

### 5. Tests

#### `lib/test_tick.py` (new)

Tests using `unittest.mock.patch` on `subprocess.run` and `subprocess.Popen`:

- **Admission:** `can_dispatch_now` returns `AdmissionResult(can_dispatch=False)`
  at MAX_IN_FLIGHT, returns True when slots free, returns reason="admission_error"
  on corrupt marker (fail-closed), includes correct `in_flight_count`, and counts
  this machine's dispatch markers alone — a markerless `issue:ai:implementing`,
  `pr:ai:reviewing`, or `pr:ai:fixing` label is shared workflow state and counts
  against nothing (#663)
- **Reconcile decisions:** each action maps to correct label transition and scratch
  behavior; `kill=True` only when identity="matched"
- **Lifecycle:** closed/merged PRs trigger cleanup not relabel; dual-label items
  recover through the marker-owned label when the marker is live/hung/complete
  (stage- and order-invariant), escalate in favor of human holds, and otherwise
  escalate with convergence re-read
- **Identity:** matched→kill allowed, mismatched→no kill, unknown→retain custody
- **Harvest:** review drift detection, iteration cap, replay idempotency
- **Dispatch:** trusted-candidate gate, policy-change escalation, dedup guard,
  lease-before-label ordering, implementer/fixer PID fallback sequence, reviewer
  `env -i` isolation, and implementer/fixer inherited-auth environment preservation
- **Targeted mode:** `--target pr:N` / `--target '#N'` narrows to single item
- **Tick log:** non-fatal logging, correct field types (ts=number, pr=number|null)
- **`verify_codex_bin`:** returns path on match, raises on version mismatch
- **`relabel`:** empty from_label omits `--remove-label`, from_label==to_label is no-op

#### `lib/test_skill_contract.py` (update)

Replace grep-based SKILL.md tests with behavioral tests, categorized by verification strategy:

- **Category A — ordering invariants:** mock `subprocess.run`/`subprocess.Popen` via
  `unittest.mock.patch`, assert call sequence with `assert_has_calls`. E.g.,
  `test_candidate_gate_precedes_worktree_creation` asserts `validate_review_candidate()`
  is called before `git worktree add`. Fragile but necessary.
- **Category B — behavioral invariants:** standard unit tests with mocked
  subprocess returns, assert on return values and label transitions. E.g.,
  `test_fixer_dispatch_escalates_without_pipeline_review_comment`.
- **Category C — structural invariants:** combined with Category A — the subprocess
  call args ARE the invariant. E.g., `engine_wrapper.py` flags include
  `--envelope-path`.
- **Category D — documentation invariants:** unchanged. E.g.,
  `test_design_doc_marks_fixer_as_shipped_not_manual` (design doc check).

#### `lib/test_pipeline.py` (add)

Parity tests: call the public function directly → get `dict`. Call the CLI wrapper
with a matching `Namespace` → capture stdout → `json.loads()`. Assert
`json.loads(captured_stdout) == direct_result` (logical equality, not string match).
Also add a tick-log round-trip test: bash `jq -n` output ≡ Python `json.dumps` output
for the same logical entry.

#### Snapshot tests (new)

Capture the shape of real `gh` output for each `--json` query used by tick.py
(`gh pr view --json state,labels,headRefOid,baseRefName,comments,...`). Store as
test fixtures. Before any production run, compare snapshot against live output to
detect `gh` CLI format drift. This addresses the risk that mocked tests pass but
real `gh` output has changed (field renames, type changes).

### 6. Out of scope (follow-up)

- `lib/process_control.py` — no changes needed; already has public functions.
  `tick.py` calls `terminate_process()` / `terminate_process_group()` /
  `get_process_start_time()` directly with native return types (`bool`, `float | None`).
- `pr-pipeline-status/status.sh` — same bash-in-skill pattern, separate extraction
- Implementer/fixer atomic ownership (wrapper equivalent to `engine_wrapper.py`) —
  deferred per existing design note; the PID-post-spawn fallback is preserved
- Reviewer preflight implementation — `preflight.sh` stays as bash, but the thinned
  `SKILL.md` still requires it before the first reviewer dispatch. Moving the
  preflight into Python is a separate concern.
- Implementer/fixer env hardening — the extraction preserves inherited auth. A future
  isolation pass must name the minimal authenticated environment and prove a
  push-capable launch before replacing it.

## Implementation order

1. ✅ **Define TypedDicts in pipeline.py** — `HarvestResult`, `ClassifyResult`,
   `ReconcileResult`, `AdmissionResult`, `CandidateMetadata`
2. ✅ **Export pipeline.py public functions** — 16 functions, stdin-based ones accept
   explicit parameters
3. ✅ **Verify process_control.py public API** — confirm `terminate_process()`,
   `terminate_process_group()`, `get_process_start_time()` are importable and
   correctly typed. No code changes needed — already public with injectable
   dependencies. `tick.py` calls them directly with native return types.
4. ✅ **Add pipeline.py direct-call-vs-CLI parity tests** — verify delegation +
   stdout round-trip. Use `json.loads(captured) == direct_result`. 12 parity
   tests covering all 16 public functions with CLI wrappers (some functions
   share a test).
5. ✅ **Write `lib/tick.py` shared helpers** — `gh()`/`gh_json()`/`gh_text()`/`git()`,
   `TickLog`, `Attempts`, `cleanup_scratch()`, `relabel()`, `StageConfig` dataclass,
   `verify_codex_bin()`, repo-root resolution
6. ✅ **Write authoritative in-flight read** — query `issue:ai:implementing`,
   `pr:ai:reviewing`, and `pr:ai:fixing`; any read failure gates all DISPATCH.
7. ✅ **Write `can_dispatch_now()`** — `AdmissionResult` return,
   `authoritative_in_flight` param, fail-closed on corrupt marker, count markerless
   implement/review/fix labels against `MAX_IN_FLIGHT`
8. ✅ **Write `reconcile_engine()`** — parameterized by `StageConfig`, returns
   `ReconcileDecision`. Kill on proven identity is performed inside
   `_reconcile_one` (identity is derived there; re-deriving at the caller
   would be fragile). Other stage-specific side effects (comment posting,
   PR creation, iteration bump) remain the caller's responsibility.
9. ✅ **Write HARVEST phases** — `harvest_reviewers()`, `harvest_implementers()`,
   `harvest_fixers()` with full side-effect processing: PR creation, review comment
   posting, drift detection, label transitions, iteration cache bump. Each returns
   `bool` (reconcile ok).
10. ✅ **Write DISPATCH phases** — `dispatch_reviewers()`, `dispatch_implementers()`,
   `dispatch_fixers()`. Fallback sequence for implementer/fixer/reviewer
   (`_publish_marker_or_revert`) handles PID marker publish failure:
   terminate engine → relabel back + cleanup, or write fallback marker if
   termination fails. 46 dispatch tests across all three stages.
11. ✅ **Write `main()` and CLI** — `sys.argv` for `--target`, `os.chdir()` to repo
    root, HARVEST → gate → DISPATCH → Summary. `if __name__ == "__main__"` guard.
12. ✅ **Write `test_tick.py`** — mock subprocess, test each function (88 tests,
    416 across full suite)
13. ✅ **Update `test_skill_contract.py`** — categorize by A/B/C/D, replace grep tests
14. ✅ **Add snapshot tests** — capture real `gh --json` output shapes (8 tests,
   `test_gh_snapshots.py`). Validates field presence and types for all `gh --json`
   queries used by `tick.py`. Skipped when `gh` is not authenticated.
15. ✅ **Thin SKILL.md** — invocation stub with invariants, preflight command, and
    debugging section
16. ✅ **Update design doc** — `docs/planning/designs/pr-pipeline-tick.md`: remove
    bash-centric sections (the "Shell out to it" instruction, bash dispatch commands),
    update architecture diagram to show Python orchestrator, point to this extraction
    doc for the new contract. This doc updated to reflect as-built state.

## Review Findings

Plan adversarially reviewed by 3 subagents:
- **Correctness lens:** 8 findings (4 major, 4 minor) — parameterization gaps,
  `can_dispatch_now` under-specification, fallback logic, enumeration asymmetry
- **Risk lens:** 10 findings (2 blocker, 5 major, 3 minor) — stdin function
  signatures, `can_dispatch_now` fail-closed, relative paths, unicode encoding,
  `relabel` edge case, dry run insufficiency
- **Architecture lens:** 8 findings (6 major, 2 minor) — module boundary,
  TypedDicts, `reconcile_engine` contract, `gh` helper variants,
  `AdmissionResult`, test categorization

Codex review of PR #477 added four further findings, all folded into this
revision:
- **Global admission:** `can_dispatch_now()` now receives all authoritative
  in-flight labels and tests markerless `ai:implementing`, `ai:reviewing`, and
  `ai:fixing` as slot-consuming.
- **Executable auth boundary:** reviewer dispatch keeps `env -i`; implementer/fixer
  dispatch preserves inherited auth until a separately tested minimal environment
  exists.
- **Preflight retention:** the thinned `SKILL.md` still tells operators to run
  `preflight.sh`, and the implementation keeps the reviewer version gate.
- **True dry-run proof:** the verification plan uses a no-spawn fake-engine harness
  plus a local push-capable inherited-auth probe instead of dispatching a real
  temporary issue.

## Post-Implementation Adversarial Review

The implementation underwent **two rounds** of adversarial review (3 subagents per round
across correctness, risk, and architecture lenses) totaling 6 independent reviews.

### Round 1 (implementation review): 18 findings

**Blockers (4):** `pr_number` missing from decision dicts → dead HARVEST actions (B1);
`relabel()` hardcoded `gh pr edit` for all stages (B2); no `__main__` guard (B3);
jq filter `".[]"` returned JSON objects, not digits (B4).

**Majors (6):** `can_dispatch_now` undercounted authoritative labels (M1); hung OPEN
engines never killed (M2); dispatch stubs uncommunicated in SKILL.md (M3); silent gh
failures (M4); `cleanup_scratch` on mismatched identity violated Invariant 4 (M5);
`StageConfig` missing callable fields (M6).

All blockers and majors fixed. See the git history for the full diff.

### Round 2 (fix verification + residual): 9 findings

**Majors (3):** Missing `deadline` key coerced to `0.0` instead of `float("inf")`
(gated fix; Invariant 5); kill failure produced zombie engines — label flipped or
scratch deleted while engine still running (gated fix); dual-label escalation returns
lacked `pr_number` (fixed).

**Minors/robustness (4):** `_execute_reconcile_actions` cleanup lacked identity gate
(fixed); early-return dicts inconsistently lacked `pr_number` (fixed); `_scan_markers`
skipped `valid_marker()` validation (fixed); `_print_summary` always reported success
(fixed).

**Test coverage gaps (noted for follow-up):** `_classify_marker()` and dual-label
escalation logic lack direct tests; TypedDicts defined but not enforced as return types;
`Attempts` counter not concurrency-safe (by design — ticks must run sequentially).

## Verification

1. ✅ `uv run pytest .claude/skills/pr-pipeline-tick/lib/test_pipeline.py` — 257 tests
2. ✅ `uv run pytest .claude/skills/pr-pipeline-tick/lib/test_tick.py` — 88 orchestrator
   tests
3. ✅ `uv run pytest .claude/skills/pr-pipeline-tick/lib/test_dispatch.py` — 46 dispatch
   tests
4. ✅ `uv run pytest .claude/skills/pr-pipeline-tick/lib/test_skill_contract.py` — 17
   contract tests
5. ✅ `uv run pytest .claude/skills/pr-pipeline-tick/lib/test_gh_snapshots.py` — 8
   snapshot tests (gh CLI shape validation)
6. ⬜ No-spawn scenario harness — deferred (post-extraction enhancement)
7. ⬜ Push-capable inherited-auth probe — deferred (post-extraction enhancement)
8. ✅ `bash .claude/skills/pr-pipeline-tick/preflight.sh` — unchanged, still required
9. ✅ `uv run pyright .claude/skills/pr-pipeline-tick/lib/` — zero errors
10. ✅ `uv run ruff check .claude/skills/pr-pipeline-tick/lib/tick.py` — clean
