---
name: review-response-hygiene
description: Use when revising a PR after review feedback or re-reviewing a revised PR so the fix set closes the actual blocker surfaces instead of only the first edited lines
---

# Review Response Hygiene

Use this skill after review feedback lands and before declaring a revision done.

The purpose is narrow: prevent the common Sworn pattern where the main blocker is
fixed, but one adjacent source-of-truth, sibling path, or PR-body claim remains
stale and causes another avoidable review round.

## Revision Rules

1. **Re-check the blocker surface, not just the edited hunk**
   - If the finding was about custody, re-check all custody entry points.
   - If it was about docs ownership, re-check every file that now teaches that
     contract.

2. **Update the story surface**
   - PR body, revision comment, README/DEBUG/SKILL ownership notes, and test
     descriptions must match the final live split.
   - A correct diff with a stale reviewer handoff still causes review bounce.

3. **Look for sibling omissions**
   - Review fixes often land on one engine type, one doc file, or one loop path.
   - Explicitly inspect the siblings the original finding implies.

4. **Preserve verification truth**
   - The revision note must say what was actually rerun and what stayed review-only.
   - Do not inherit the earlier PR-body test story if the final revision changed
     the scope or ownership.

5. **Re-read the issue/contract named by the review**
   - If the review cited an issue or contract, make sure the revised head now
     satisfies that exact contract, not just the illustrative example in the
     comment.

## Revision Pass Checklist

1. Restate each blocker in one sentence.
2. Name the sibling surfaces implied by that blocker.
3. Verify the diff fixes each sibling or explicitly defer it.
4. Verify the PR body/revision comment still describes the final live change.
5. Re-run only the checks that actually prove the repaired surface, then report
   them exactly.

## Review Questions

- What adjacent file or sibling path would still teach the old contract?
- Which engine/doc/loop sibling did this fix not touch?
- Does the latest revision comment describe the final head or the previous one?
- If I review only the blocker area named last time, what second-order drift is
  still visible right now?

## Required Output

Structure the pass in these buckets:

1. Blocker-surface repairs
2. Sibling-surface gaps
3. PR-body / revision-note drift
4. Verification truth
5. Residual risks

## Common Misses

- Review fix lands in code but not in README/DEBUG/PR body
- First review said "named cross-links"; revision updated links but not the exact
  section names or tests
- One stage/engine path updated, sibling stage still on the old contract
- Revision note claims older verification after a later force-push/rebase

*Source patterns: PR #541 two-round docs split correction; PR #540 repeated
cadence/custody re-reviews; PR #542 blocker-area re-check plus post-rebase truth
maintenance.*
