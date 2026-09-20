---
name: automation-control-plane-review
description: Use when reviewing or changing pr-pipeline dispatch, harvest, engine ownership, launch boundaries, or GitHub mutation authority
---

# Automation Control-Plane Review

Use this when the change affects ownership, liveness, completion, retries, or
cleanup for unattended automation. The goal is to catch control-plane bugs that
hide behind green CI and happy-path tests.

This skill is for executable review, not broad architecture commentary. The
review should prove the risky paths with concrete probes: exact argv, actual
worktree/cwd, branch continuity, custody after failed relabels, and whether the
docs/tests claim behavior the current head does not actually ship.

Pull in [durability-boundary-review](../durability-boundary-review/SKILL.md) when the main risk is archive-before-delete,
retry-reset ordering, corrupt/unknown custody, or relabel durability. Pull in
[loop-cadence-contract-review](../loop-cadence-contract-review/SKILL.md) when the main risk is self-paced scheduling,
quiescence stop behavior, CI-rollup promotion, or drift between helper cadence
logic and the operator-facing loop contract.

## Runtime owners and applicability

Start with the changed behavior in [tick.py](../tick/lib/tick.py),
[pipeline.py](../tick/lib/pipeline.py), and
[process_control.py](../tick/lib/process_control.py). Compare the
[operator contract](../tick/SKILL.md) with the executed path.

Apply the checks for the boundaries the change affects. These are review
questions, including checks for proposed features; they do not assert that every
mode or mechanism already ships. Verify each claimed behavior at the reviewed
head. Record why a dimension does not apply instead of inventing a runtime path.
These skills guide developers implementing and reviewing pr-pipeline itself.
Use the applicable checks when choosing failure tests and during self-review.
Dispatched engines retain their prompt's tool and authority limits.

## Review Focus

Check these invariants explicitly:

1. **Owned identity**
   - What exact process or process tree is owned?
   - Is the durable identity published before spend starts?
   - Does verification use the same OS token it records?

2. **Completion authority**
   - What single artifact proves completion?
   - Can a partial result, stale file, or missing envelope bypass that gate?
   - Does completion require both successful exit and valid output?

3. **Unknown identity custody**
   - What happens when liveness cannot be proven?
   - `unknown` must retain custody; it must not trigger kill, cleanup, or requeue.

4. **Tree-death proof**
   - What proves the whole owned tree is gone, not just the wrapper PID?
   - Cleanup and slot release must wait for that proof.

5. **Executable shell contract**
   - Is each shell block valid in the scope where it runs?
   - Top-level shell must not rely on `local`, function-only variables, or comments
     that promise behavior the code does not actually execute.

6. **Sibling-actor symmetry**
   - Name every actor type the control plane drives symmetrically — each engine,
     worker lane, step type, or queue consumer that shares one dispatch path.
   - Any change to dispatch, completion handling, state-flip failure handling,
     escalation, or cleanup must be applied to all of them — or the skipped ones
     explicitly deferred with a reason.
   - Fixes repeatedly land on one actor type and silently miss the siblings.

7. **Claimed-vs-shipped contract**
   - README, `SKILL.md`, test names, and PR copy must not claim a side effect the
     code still stubs, skips, or narrows.
   - A green test that exercises an intended command shape instead of the emitted
     command is not proof.

8. **Authoritative external reads fail closed**
   - GitHub/API/state reads are authoritative inputs, not hints.
   - Failed, malformed, empty, or unrecognized payloads must retain custody or
     hold progress; they must not be silently converted into "no work",
     "passing", or "closed".

9. **Terminal transition custody**
   - Archive, marker cleanup, and attempt clearing must wait for the terminal
     remote transition or explicit dead-engine proof the code claims to require.
   - If relabel/copy/move fails, the current marker, attempts, and evidence must
     remain durable for the next tick.

10. **Mutation-time authority**
   - Name each read that authorizes an irreversible remote mutation: current
     label/hold state, PR head and body, linked issue contract, policy mode, or
     reviewer decision.
   - If an engine run, retry, approval window, or other actor can intervene
     after that read, re-read and validate the authority immediately before the
     mutation. A snapshot taken at dispatch is not permission to edit at harvest.
   - If the authority changed, preserve evidence and stop or escalate; never
     overwrite the newer state with a stale planned mutation.

## Required Probe Matrix

For control-plane changes, check these concrete surfaces explicitly:

1. **Dispatch custody**
   - What happens if marker publish succeeds but the relabel fails?
   - Can the same item be selected again under the pre-dispatch label while the
     original engine is still live or launching?
   - Does the code retain custody on `unknown`, or does it silently clean up and
     re-dispatch?

2. **Workspace isolation**
   - Does the launched engine run inside a real `git worktree`, detached checkout,
     or other isolated tree the code claims to use?
   - If the cwd is only a subdirectory inside the operator checkout, treat that as
     a blocker unless the contract explicitly says shared-tree execution is safe.

3. **Branch/ref continuity**
   - Does dispatch create the same branch/ref that the prompt names and harvest
     later consumes?
   - If harvest hard-codes `agent/issue-<N>` or a PR head ref, prove dispatch and
     the engine's push target use that exact branch.

4. **Auth and launch boundary**
   - Is the proposed env merely "minimal", or is it actually sufficient for the
     promised push/auth/model path?
   - If the design says inherited auth is required until a narrower contract is
     proven, a smaller env is a regression unless the proof landed in this diff.

5. **CLI/tool contract**
   - Validate the exact argv the production code emits, not a hand-written
     approximation in docs or tests.
   - For `gh`, `codex`, `claude`, or shell helpers, reject tests that assert a
     "correct" command while production builds a different one.

6. **Fail-closed setup**
   - Fetch, diff, worktree creation, review posting, and relabel failures must end
     in a visible terminal state or retained custody, not a silent skip.
   - If the code catches and logs but leaves the item at the pre-dispatch label,
     check whether the next tick can duplicate spend.

7. **Targeted-vs-global scope**
   - Targeted `pr:N` / `#N` runs must not mutate unrelated issues or PRs while
     diagnosing one item.
   - Global admission accounting may stay global, but relabels, cleanup, stale
     reconciliation, and archive decisions must stay scoped to the requested item.

8. **Empty or unknown CI/state surfaces**
   - Empty status rollups, unknown conclusions, malformed entries, and missing
     required fields are not green.
   - Treat "nothing classified" as pending/unknown unless the current code proves
     that empty is impossible at the decision point.

9. **Irreversible remote writes**
   - For every PR-body edit, review post, label transition, branch update, or
     similar mutation, identify the authoritative reads that justify it and the
     last point at which they are revalidated.
   - Probe a late hold, a competing body/head edit, and a retry after partial
     success. The mutation must either target the still-current artifact or
     retain custody without mutating it.

## Adversarial Prompts

Ask these during review:
- If the wrapper dies after spawn, what still runs and who owns it?
- If identity is unreadable, what state transition still happens by mistake?
- If the result lands but the completion marker does not, what gets harvested?
- If SIGTERM kills only the wrapper, what prevents double-dispatch?
- If a shell snippet is copied into the real tick, does it still execute?
- If the relabel fails after spawn, what exact label and marker state remain?
- If a GitHub/API read fails or returns junk, what terminal or promotion decision
  still happens by mistake?
- If the CI rollup is empty for a moment after push, can the item still promote?
- If archive creation fails, what evidence still survives and what cleanup is
  blocked?
- If the prompt says one branch but harvest consumes another, what actually gets
  pushed or reviewed?
- If a hold or competing PR edit lands after dispatch, which fresh read prevents
  harvest from applying its stale decision?
- If the tests are green, which exact production helper or emitted artifact did
  they exercise?

## Required Proof

- Run or reason through one crash-window per invariant, not just the happy path.
- Use direct classifier/helper probes to isolate failures, then verify the
  production path that consumes those decisions. For implementation completion,
  follow [definition-of-done](../definition-of-done/SKILL.md); helper probes and
  reasoning through crash windows supplement its execution requirements.
- Treat green CI as insufficient unless it proves the durability boundary itself.
- Compare docs, tests, and production code at the same head before accepting any
  claim that "dispatch is implemented", "harvest posts the review", or similar.

## Required Output

Structure the review in these buckets:

1. Ownership/custody findings
2. Isolation/auth findings
3. CLI/tool-contract findings
4. Claimed-vs-shipped contract findings
5. Verification gaps

## Common Misses

- PID-only liveness or kill paths
- Bare result files treated as complete
- Unknown identity collapsed to dead
- Cleanup before whole-tree death is proven
- Runbook text that is syntactically invalid at runtime
- A fix applied to one actor type but not its siblings
- A real command built incorrectly while tests assert a hand-written "correct" one
- Failed/empty authoritative reads converted into "no PR", "no checks", or green
- Attempt counters or scratch cleared before the terminal relabel actually lands
- A mutation authorized from dispatch-time state rather than authority re-read at
  the moment of the remote write
- Targeted mode mutating unrelated labels or stale state
- Empty CI rollup treated as passing
- Docs, `SKILL.md`, or test names claiming side effects the orchestrator does not ship
