# Why — Failure Shapes and Excuses

Read this when a Phase B answer comes out as "nothing here can fail in that
way", or when the verification commands of the repository are about to become
the story. This file is the evidence behind `SKILL.md`, not more rules.

## Common Misses

- The proof passes an absolute temporary path where production passes a relative
  root, resolved against a working directory that the launcher changes. The
  feature never runs, and the swallowed error keeps the exit code clean.
- The feature runs only in the long-lived loop mode, but the operator document
  prescribes the one-shot invocation.
- The fix attaches to a wrapper. Sibling call sites reach the wrapped boundary
  directly and keep the old behavior.
- Two writes that must agree are ordered so that a crash between them leaves a
  state that no later run repairs — and no test was aimed at that window.
- Stored state is accepted because it is non-empty instead of because it matches
  a schema, so a corrupt record authorizes the action that the schema was to
  gate.
- One document is corrected and the other documents still teach the old
  contract. The document left stale is often the one with higher priority.
- A rule is made narrower in an executable surface ("on this PR") while the
  published contract still says "on any item", and the test asserts only the
  shared prefix.
- The entry-point proof confirms that the new line appeared and never asks what
  else moved. A filter that the change made less strict now admits a record that
  the run used to skip, and the scope re-check reads clean because nobody
  compared the two runs.
- The verification commands are reported as the proof, and nobody named the
  condition that broke the change.

## When You Are About to Release on Green

| Excuse | Reality |
|---|---|
| "Every verification command passes" | They show that nothing broke, not that anything new ran. Name the entry point that ran it. |
| "The unit test covers it" | A unit test proves the function. The claim is about the path that calls the function. |
| "It is the same code path" | The proof supplied its own paths, working directory, and mode. Production supplies different ones, and that gap holds the defect. |
| "I will sweep the siblings if review asks" | The question from review is the failure that this prevents. The sweep is one search. |
| "I will write the Phase A declaration now, from the diff" | It will agree with the diff, because you copied it from the diff. Restart from a real declaration, or record a waiver. There is no `Phase A: skipped` exit. |
| "Nothing is committed, so the gate does not apply" | The gate is the first of four events, and the report of completion is one of them. A handoff is where somebody starts to trust the change. |
| "The remaining call sites are follow-up work" | Then the claim covers the sites that you fixed, not the boundary. Make the claim smaller, or the PR asserts something that a path you named contradicts. |
| "Crash windows are theoretical" | Two of them shipped in one change in the source reviews below, and exact-head probes reproduced both. |
| "The documents are a separate PR" | A document that teaches the contract that this change made false is the same defect in another file. |
| "Nothing here can fail in that way" | Then write the `n/a` line with the reason. That costs one line and stays checkable. Silence is not. |

## Why the Rules Have This Shape

- **The gate is the first of four events, because each one leaks alone.** Gate
  only on the report of completion, and the commit is already made and the
  branch is already pushed, so the block has nowhere to go. Gate only on the
  commit, the push, and the PR body, and a task that ends with an uncommitted
  working tree reaches no gate at all.
- **The declaration must come first, because a later one comes from the diff.**
  It can then only agree with the diff, so it cannot catch the scope error that
  it exists to catch.
- **The baseline is what makes that order checkable.** A `Baseline status` of
  `clean` is the claim *no edit of mine existed yet*, and Phase B can contradict
  it: if the declaration came after the first edit, that edit is either in the
  recorded status or the baseline was rewritten to hide it. The modification
  time of the file cannot do this job, because it records only the *last* write.
  What fails is a baseline that first appears after the work.
- **A missing Phase A is not a disclosure.** Work that reaches completion with
  no declaration reaches it with the invariant boundary never established, and a
  receipt that says so describes an unreviewable change instead of a finished
  one.
- **Phase A reads the declaration back from disk, not from memory.** A long task
  reaches Phase B far enough from Phase A that the declaration can leave the
  context, and a scope re-check against a half-remembered `Excluded` line is the
  circularity that the skill prevents.

## Source Patterns

These are the four reviews that this skill was written from:

- A heartbeat that never ran in production, because a relative path was resolved
  again against a changed working directory; and a stall scan reachable only in
  the loop mode, while the prescribed harness runs one-shot ticks.
- A prune attached to a wrapper, while the terminal, escalation, and recovery
  finalizers bypassed it.
- A receipt written after the remote change that it authorizes, and durable
  state accepted because it was non-empty instead of because it matched a
  schema.
- One document corrected, while the other documents and a narrowed engine prompt
  kept the old contract.

All four were green on the full verification command set of their repository
when they reached review. Each one cost three or four review cycles to find.
