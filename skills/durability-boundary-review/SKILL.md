---
name: durability-boundary-review
description: Use when a pr-pipeline change affects marker ownership, retries, evidence archival, checkpoint/recovery boundaries, terminal relabels, or any path where local custody and remote state can drift
---

# Durability Boundary Review

Use this when the risky part of a change is not the happy-path decision, but the
moment ownership moves from one durable artifact to another.

This skill is narrower than [automation-control-plane-review](../automation-control-plane-review/SKILL.md). Load it when the
main question is: "exactly what durable proof allows cleanup, relabel, retry
reset, or archive deletion here?"

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

## Core Rule

Do not accept "we logged it" as durability. A durable boundary means the next
tick, next process, or next operator can reconstruct custody from the surviving
artifact alone.

## Boundary Checks

1. **Single custody owner**
   - Name the current durable owner at each stage: marker, retry counter,
     archive directory, checkpoint row, terminal label, result envelope.
   - If two artifacts claim ownership at once, define which one wins.

2. **Release condition**
   - State the exact proof required before cleanup or retry reset.
   - Examples: successful remote relabel, verified archive move, valid result
     envelope, whole-process-tree death.

3. **Archive-before-destroy**
   - If the design says evidence is retained, verify the archive copy/move is
     checked for success before the source is deleted or relabeled terminally.
   - "Best effort archive" plus unconditional cleanup is a blocker.

4. **Relabel failure retention**
   - If remote relabel/edit/post fails, custody must stay recoverable.
   - Markers, attempts, and evidence cannot be cleared just because the desired
     terminal state was computed.

5. **Unknown and corrupt states**
   - `unknown`, malformed, or partially readable identity must retain custody by
     default.
   - Corrupt markers need an explicit escalation/inspection path, not silent
     deletion because cleanup seems convenient.

6. **Sibling-path symmetry**
   - Name every stage or actor type sharing the boundary pattern under review,
     and audit all of them.
   - A repair that lands on only one stage is incomplete unless the others are
     explicitly deferred.

7. **Retry semantics**
   - Attempts must reset only when the durable owner truly changes.
   - If the same custody artifact still governs the item, retry state should
     survive.

## Concrete Probes

- Force archive creation or copy failure. Does the source still remain?
- Force remote relabel failure. Which marker, attempts, and label survive?
- Present a schema-invalid marker that still contains a live PID. Is custody
  retained or destroyed?
- Present a sibling-stage marker in a shared worktree namespace. Does the wrong
  stage delete it?
- Re-run after interruption. Can the next tick/operator infer ownership from the
  surviving artifacts alone?

## Review Questions

- What is the last durable thing that proves this work still belongs to someone?
- What exact event makes it safe to clear retries?
- What artifact would a later operator inspect after the failure path?
- Which path still destroys evidence because a helper returned falsy/empty?
- Does "corrupt" mean "dead", or merely "needs human inspection"?

## Required Output

Structure the review in these buckets:

1. Durable-owner transitions
2. Cleanup/relabel ordering findings
3. Unknown/corrupt custody findings
4. Symmetry gaps
5. Verification gaps

## Common Misses

- Archive helper swallows errors and caller still deletes the source
- Retry counters cleared before remote relabel succeeds
- Terminal cleanup deletes shared-stage custody
- Corrupt marker treated as dead without identity proof
- Review comment says "retained for audit" but no durable location survives
