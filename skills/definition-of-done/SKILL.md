---
name: definition-of-done
description: Use when you implement a feature or a fix that changes runtime or workflow behavior. Read it before the first edit, and again before the first of these events — you commit, you push, you write the PR body, or you report the work complete. Use it whenever a green test suite is about to become the proof that the change works where users and operators reach it.
---

# Definition of Done

Green is not done. The verification commands of a repository show that the
change did not break the covered behavior. They do not show that the change does
anything.

Two phases supply the missing bar. Phase A shapes the work before the first
edit. Phase B tries to disprove the work before you hand it on. Reviewers use
the same bar, but they get the answers from the diff instead of from the author,
and they report a finding when an answer is wrong instead of when it is missing.

`references/why.md` holds the defects that each rule below comes from. Read it
when a Phase B answer comes out as "nothing here can fail in that way".

## Scope

Run both phases for each change that makes a claim about **runtime or workflow
behavior**: code that a user or an operator reaches, or a contract that controls
how work is done.

Some changes make no such claim: a typo, a comment, a format pass, a dependency
update with no behavior change, or a repair to test infrastructure. These have
no production entry point, and an entry point invented for one makes the false
receipt that this skill rejects. Decide this once, before Phase A, and write the
decision where the Phase B block goes:

```
Definition of done: n/a — <why this change makes no runtime or workflow claim>
```

That line is the full alternative contract: one decision, made in the open, that
a reviewer can check against the diff. If the answer is unclear, the change
makes a claim. Run both phases.

This skill controls **whether you proved the claim**. It does not control **how
you write the proof** — which composition root a test calls, what to assert. The
repository owns that, in its `CLAUDE.md` or `AGENTS.md` or in a skill that it
names. Where the repository states a testing contract, that contract controls
the construction and this skill still controls the bar.

## Phase A — Declare the Contract Before You Edit

Do Phase A after you understand the task and before the first edit. It is the
only cheap moment to find a scope error.

1. **Production entry point and mode.** Name the entry point that a user or an
   operator reaches: the HTTP route, the worker loop, the command, or the
   invocation that an operator document prescribes. Do not name the function
   that you are about to edit. Then name the mode: which flag, which working
   directory, which configuration. Where the repository documents more than one
   mode, say which modes are in scope and which one is **authoritative**. If the
   entry point resolves paths, inherits a working directory, or reads
   environment variables, write down the values that production supplies.

2. **Invariant and owner.** Write the invariant in one sentence, and name the
   function or module that enforces it at runtime. Then draw the boundary of the
   change from that invariant, not from the type of artifact. A mechanism and
   the operator surface that describes it belong in one change when they own one
   invariant together. Two unrelated invariants go in two changes, even when
   they are in one file.

3. **Shared boundary.** Name the gate, guard, classifier, finalizer, cleanup
   step, persistence step, or emit point that you expect to move. If you are
   about to attach behavior to a *wrapper* around that boundary, write that down
   now. It is the sweep target in Phase B.

4. **Controller-to-engine contract.** When an engine, a prompt, or a classifier
   is part of the claim, name the controller-owned artifact that supplies each
   required input, the tools and policy that the engine receives, and the
   validator that rejects an incomplete result. A prompt can request behavior. A
   prompt cannot give authority, supply missing context, or enforce completion.

Write the result to `tmp/done-declaration-<branch>.md`, with each `/` in the
branch name replaced by `-`, so that the name stays one file instead of a nested
path. `tmp/` is not in version control, but the file is on disk, so Phase B
reads the declaration again instead of recalling it. Head the file with the
baseline, recorded as you write it:

```
Baseline HEAD:   <git rev-parse HEAD>
Baseline status: <git status --porcelain, or "clean">

Claim:      <one sentence: what will be true in production that is not true now>
Entry:      <entry point> in <mode>; authoritative mode: <mode>
Invariant:  <invariant> — owned by <runtime owner>
Boundary:   <shared gate/finalizer/classifier expected to move>
Controller: <required engine inputs/tools/policy and result validator, or "n/a — no engine">
Excluded:   <invariants and paths deliberately not in this change>
```

If a line still reads `<unknown>`, resolve it before you edit. An unnamed entry
point or owner means that the task is still research.

Do not write the declaration later. One written after the diff exists comes from
the diff, can only agree with it, and cannot catch the scope error that it
exists to catch. The baseline is what keeps that order checkable rather than
asserted, so record it truthfully. A dirty baseline is not a failure and needs
no cleanup: record what the working tree already held and continue.

A missing Phase A fails the definition of done, and disclosure is not an exit
from it. Two exits keep the contract, and both are visible in the Phase B
output:

- **Restart.** Set the diff aside — save the patch, or park it on a work commit
  on a scratch branch. Write the declaration from the task description instead
  of from the code, then make the change again against it. Report
  `Phase A: declared before first edit (restarted)`.
- **Escalate.** Stop, and ask the owner to waive Phase A for this task. Report
  `Phase A: waived by <owner> — <where the waiver is recorded>`.

`Phase A: skipped` is not a third exit.

## Phase B — Prove the Contract Before You Hand It On

Do Phase B before the first of these four events: you commit, you push, you
write the PR body, or you report the work complete. Each of the four is named
because each one leaks alone.

1. **Production-path proof.** Run the declared entry point in the declared mode.
   A helper, a fixture, or an in-process copy of the entry point is not the
   entry point. Supply the production values for everything that the entry point
   resolves — path, working directory, environment, configuration. A proof that
   passes an absolute temporary path, where production passes a relative root,
   cannot see the defect in the resolution. If the change alters the documented
   mode, change the documents in the same diff.

   Run the same entry point on the code before the change first, and keep its
   output. If you look only for the new behavior, you confirm the addition and
   stay blind to the subtraction beside it: a line that stopped, a record now
   processed that the run used to skip, a count that moved. Each difference
   between the two runs is either one that the claim predicts or a second change
   that you did not declare.

2. **Shared-boundary sweep.** Search for the boundary symbol that Phase A named,
   and read each result. Fix each one, or defer it and name its owner and its
   consequence. Silence is not a deferral, and a named deferral is not a
   permission: when the deferred site is inside the claim, make the claim
   smaller. A claim that still spans a path that you recorded as broken is the
   same defect with a receipt. If the change attached behavior to a wrapper,
   sweep the callers of the *wrapped* boundary. The callers of the wrapper are
   the set that already works.

3. **Disproof attempt.** Write the claim again in one sentence, then try to make
   it false. Both halves are necessary:
   - **Static sweep** — the sibling call sites; each other documented mode; each
     adjacent contract (README, SKILL.md, design documents, the PR body) that
     this change makes wrong — a document it makes wrong ships in this diff; and
     reachability: is there a production path on which the new code never runs?
     When an engine takes part, trace each claimed obligation to a
     controller-supplied input, a permitted tool, a trusted policy, or a result
     validator. Prompt text alone is not proof.
   - **Execution attempt** — run the change with a production-shaped topology
     and production-shaped inputs. Then apply the failure conditions that are
     applicable: an interruption or a crash between two writes that must agree,
     malformed or unknown stored state, a second concurrent actor, a repeated
     run, or an external call that fails.

   If you cannot write down a condition that *would* make the claim false, you
   have not tried to disprove it yet.

4. **Applicability record.** Each failure condition is either exercised or
   recorded as `n/a — <reason>` on one line. A change with no multi-write
   boundary owes no crash test, and an invented one proves nothing. An
   applicable condition must not disappear because nobody mentions it.

5. **Scope re-check.** Read `tmp/done-declaration-<branch>.md` again — the file,
   not your memory of it — and compare the final diff with its `Invariant` and
   `Excluded` lines. Then check the order: run `git status --porcelain` and
   `git rev-parse HEAD` again, confirm that each path you touched is absent from
   the recorded `Baseline status`, and confirm that the recorded `Baseline HEAD`
   is an ancestor of the current one. A path in both was already modified when
   you wrote the declaration, so the declaration came from it. If the work found
   a second invariant, move it to its own change or defer it with an owner. To
   absorb it silently turns a reviewable change into an unreviewable one. Delete
   the file after you write the block.

## Required Output

Phase B ends in one block. When you publish the change as a PR, the block goes
in the `## Test coverage` section. When you do not, it goes in the completion
report, word for word — a task handed back with an uncommitted working tree
still owes it. If a PR opens later, copy that block in without changes.

The block describes one diff, and `Proved at:` says which one. A diff moves
after Phase B more often than not — a rebase, an amend, a conflict fix, a
revision after review — and the block does not move with it. When the head
moves, do the Phase B steps that the new change touches again, then update those
lines and `Proved at:`. A format pass touches none of them. A change to the code
that the claim is about touches step 1 as a minimum.

Name what you reached and what it showed. Each line must name something that a
reviewer can run again or read again: a command, a path, a symbol, or a call
site. "Verified end to end" names nothing and is not evidence. Name the call
sites instead of counting them: a count is stale after the next commit.

```
Phase A: <declared before first edit | waived by <owner> — <where recorded>>
Proved at: <head sha, or "uncommitted, baseline <sha>">
Entry point: <entry point> run in <mode> — <how you drove it>
Boundary swept: <symbol> — <call sites reached>; deferred: <site> (<owner>, <consequence>)
Controller proof: <engine obligation> — <controller input/tool/policy/validator that enforces it>
Disproof (static): <what you swept> — <what it showed>
Disproof (execution): <production-shaped run> — <what it showed>
Scope rechecked: <Phase A invariant> — <what the final diff holds against it>
n/a: <condition> — <why this change has no such boundary>
Deferred scope: <second invariant found> — <owner or issue>
```

Remove the `deferred` part, `Controller proof`, `n/a`, and `Deferred scope` only
when there is nothing to report. Always keep the other seven lines.
`Scope rechecked` is one of the seven: a silently absorbed second invariant
leaves no trace, so "the diff still holds the declared invariant and nothing
else" is a result, not an empty line.
