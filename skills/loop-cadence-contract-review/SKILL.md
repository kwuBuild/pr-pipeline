---
name: loop-cadence-contract-review
description: Use when a pr-pipeline change affects self-paced polling, tick cadence emission, quiescence stop behavior, CI/status polling, or any loop contract shared across helper logic, runtime stderr lines, and operator docs
---

# Loop Cadence Contract Review

Use this when the risk is that cadence logic looks correct in one helper, but the
live executable loop, emitted status line, and documented operator contract do
not actually match.

This skill is about one specific failure class: drift between the decision helper,
the runtime path that consumes it, and the operator docs that teach it.

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

## Contract Checks

1. **One executable cadence contract**
   - The helper, in-process loop, one-shot stderr/status output, and `SKILL.md`
     loop instructions must describe the same scheduling behavior.
   - If one path still uses older constants or fallback logic, treat that as a
     blocker.

2. **Unknown state stays warm**
   - Unknown post-dispatch or external-read state must not degrade into a cold
     heartbeat or quiescent stop.
   - If liveness/queue/CI state is unknown, the next wake should stay short or
     the loop should surface the uncertainty explicitly.

3. **Only actionable deadlines pace the loop**
   - Use `_nearest_actionable_engine_deadline` in `tick.py` to distinguish
     identity-verified `live` or `launching` markers from `hung` markers. A
     still-actionable deadline that lapses mid-scan requires an immediate retry.
   - A marker already classified as `hung` contributes no deadline, including
     when termination failed. In the failed-termination case, retain custody and
     admission occupancy and use the warm timer; the in-process wake watcher can
     wake sooner after proven process-group death. Repeatedly pacing to the same
     expired timestamp creates a busy retry loop.
   - Check both consumers against the [operator cadence contract](../tick/README.md).

4. **Identity-authoritative liveness**
   - Cadence decisions must use the same identity proof as harvest/admission when
     deciding whether an engine is still live.
   - Bare `os.kill(pid, 0)`-style checks are not enough if PID reuse is possible.

5. **Stop semantics**
   - Quiescence stop, irrecoverable corruption, and operator-needed states must
     stop and notify the operator exactly where the docs claim they do.
   - A helper that computes `stop=true` is not enough if the loop consumer ignores
     it.

6. **Empty/unknown CI surfaces**
   - Empty rollups, unknown conclusions, and malformed check entries must not be
     interpreted as passing or stable-idle.

## Concrete Probes

- Feed the post-dispatch cadence helper an all-unknown state. What exact wake
  line is emitted?
- Let an actionable deadline lapse mid-scan. Does the consumer retry immediately?
- Fail termination of an identity-verified `hung` engine. Does its marker retain
  custody and admission occupancy while contributing no deadline, leaving the
  timer at the warm interval and the in-process watcher able to wake on death?
- Simulate PID reuse. Does cadence still believe a stale marker is live?
- Run the documented loop path and the one-shot path. Do both honor the same
  cadence numbers and stop conditions?
- Force an empty CI rollup right after a push. Can the PR still promote or sleep?

## Review Questions

- Which path actually controls the next wake in production?
- Where does the operator learn the cadence contract, and is that text still true?
- Does any "fallback heartbeat" path activate while state is actually unknown?
- Which liveness classifier does cadence use, and is it the same one harvest trusts?
- Does `stop=true` have a real consumer or only a documented one?

## Required Output

Structure the review in these buckets:

1. Helper-vs-runtime drift
2. Unknown/empty-state cadence findings
3. Identity/liveness findings
4. Stop/notify contract findings
5. Verification gaps

## Common Misses

- One-shot cadence line updated, in-process loop still on older constants
- Unknown state coerced to idle or empty queue
- Empty CI rollup treated as green or stable
- Actionable deadline lapsing mid-scan but sleeping the normal interval
- Hung or failed-termination marker forcing repeated immediate retries
- PID reuse creating a permanent hot loop or fake live engine
