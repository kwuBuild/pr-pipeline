# Operator Issue Pickup Implementation Plan

> **For agentic workers:** Execute these coupled changes in order with test-driven development, then request an independent review.

**Goal:** Implement #14: only the configured operator's issues can dispatch or keep a loop active, with a bounded diagnostic count for excluded issues.

**Architecture:** Add one fail-closed author predicate beside the PR predicate. Filter issue queue reads with `--author`, validate returned metadata before dispatch, and use the tick's validated login for both dispatch and cadence. Unknown author metadata refuses dispatch but keeps cadence warm. Preserve existing PR eligibility and label authority.

**Tech Stack:** Python 3.13, standard library, pytest, GitHub CLI.

## Steps

- [x] Add `trusted_issue_candidate(metadata, *, trusted_author)` in `skills/tick/lib/pipeline.py`, covered in `test_pipeline.py`. Test exact matches and absent, blank, non-string, or differently authored metadata first; run `.venv/bin/python -m pytest skills/tick/lib/test_pipeline.py -k trusted_issue -q`, observe failure, then implement dictionary/string validation and exact login comparison.
- [x] In `skills/tick/lib/test_issue_trust.py`, prove issue dispatch queries include `--author` and `author` JSON metadata; targeted runs read the requested issue directly so eligible targets outside the first page remain reachable. Test refusal before label, body, or worktree actions. Run the focused tests red, then update `dispatch_implementers` in `tick.py`.
- [x] Test one extra bounded unfiltered `number` read and log output for exact counts, capped lower bounds, malformed/failed reads, and two saturated pages. Implement the diagnostic in `tick.py`; when both pages saturate, report the provable `0 or more` lower bound rather than treating unseen operator issues as foreign.
- [x] Test full-page foreign-only queues through `_run_loop_locked`, targeted foreign issues, unknown author metadata, unchanged PR queues, and login reload between ticks. Run red, then apply the issue filter to `_queue_page_has_dispatchable`, the predicate to targeted cadence, and bind the resolved login at successful startup for the entire tick and its following cadence observation.
- [x] Test the real packaged `allow_touching = true` floor while retaining malformed-value fallback to false; change `skills/tick/config.toml` only after that test fails. Update adjacent operator/security documentation to describe the new default and issue admission boundary.
- [x] Run `.venv/bin/python -m pytest skills/ -q`, inspect `git diff --check` and the complete diff against fresh `origin/main`, and independently review the security and cadence paths. Read commit guidelines before publication.

## Acceptance checks

No third-party issue launches, body reads, relabels, or escalations. A full page of third-party issues cannot starve the operator or prevent loop quiescence. Issue reads are bounded or point reads. Diagnostics never claim an unproven exact count. Malformed authors refuse dispatch and stay warm in cadence. Both dispatch and cadence adopt a corrected operator login at the next tick.

Delivery follows the repository workflow: one commit, a feature-branch push, a PR with `Closes #14`, and workflow ownership moved from the issue to the PR.
