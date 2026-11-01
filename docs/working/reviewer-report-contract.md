# Reviewer report contract

Review reports let the owner assess the reviewer's conclusion and evidence.
The reviewer supplies the report. The controller validates its structure, pins
its identity, publishes it, and passes it to the next fixer and reviewer.
Structural validity does not prove review quality or sufficient business judgment.

## Report content

New dispatches use `report_protocol_version: 2` and the installed
[review schema](../../skills/tick/review-schema.json). These fields accept
Markdown paragraphs, lists, and code blocks. There is no minimum length or
universal checklist. A short change can have a short report.

The strict engine-facing schema requires every declared property. Use an empty
string for `observations` or a finding's `resolution` when that optional content
is absent. The renderer omits empty sections. The runtime validator also accepts
omitted optional properties in retained protocol-2 reports for compatibility.

| Field | Required content |
|---|---|
| `verdict` | `ready_for_human`, `changes_requested`, or `needs_human_decision`. |
| `reasoning` | A concise explanation of the conclusion. A clean review needs an explanation, not an invented finding. |
| `scope` | The task contract and relevant boundaries examined. Distinguish inspection from execution. The controller supplies the reviewed base ref, diff base, base tip and head. |
| `findings` | An array, empty for a clean review. Each entry has a unique `id`, `severity` (`blocker`, `major`, `minor`), `location` (empty when not applicable), `explanation` of the trigger and consequence, and supporting `evidence`. Optional `resolution` suggests a fix only when justified. |
| `verification` | Checks and probes actually performed, relevant commands and outcomes, attributed CI or author evidence, and relevant checks not run with their reasons. Reading a test is not running it. |
| `risks` | Remaining uncertainty, nonblocking concerns, and deferred work with owner/reference when known. State that none were identified when applicable. |
| `observations` | Optional discussion, tradeoffs, questions, diagrams or titled subsections. Identify observations outside agreed scope. These are not required findings. |

Use `changes_requested` only with a blocker or major finding. A contradictory
new report is a protocol failure. Use `needs_human_decision` when required
evidence, authority, or access prevents a defensible conclusion. Nonessential
checks not run do not automatically require that outcome. Explain their
relevance. Unknown or unavailable evidence is expressible without fabricating
a code finding.

## Normal sequence and compatibility

1. Reviewer dispatch pins protocol 2 in `dispatch.json` and wrapper arguments.
   The wrapper carries it into `completion.json`. Missing version means an
   already-running legacy dispatch; an unknown version retains custody.
2. The wrapper retains the original output before publishing completion.
   `result_valid` describes validation; `result_captured` describes retention.
   Captured invalid protocol 2 output can reach harvest, which validates again.
   It can never fall back to the legacy schema.
3. Harvest keeps the existing pinned-commit freshness and authenticated-comment
   gates. It archives `review.json`, `review-rendered.md`, and
   `review-manifest.json` before publication. The manifest records dispatch,
   source and rendered digests, validation reason, and outcome.
4. A complete successful review uses the existing review/round header and
   single-round retry behavior. A valid human-decision report uses the escalation
   header. Invalid or oversized output produces a concise escalation notice
   with the local archive location. These escalations do not count as completed
   code-review rounds and do not dispatch a code fixer to repair prose.
5. Archive, notice, or relabel failure retains source and custody for retry.
   Existing terminal-intent and receipt recovery owns escalation retries.
   Evidence is retained after the remote transition and scratch cleanup.

Legacy reports keep their existing validation and freshness gates. Published
legacy reviews visibly state that detailed reasoning, verification, and risks
were not recorded. Schema-rejected legacy output is retained as
`review-rejected.json`; its existing completion gates remain unchanged. Older
controllers reject the new marker fields and retain custody instead of cleaning
up a run they cannot understand.

## Publication and finding identity

Publish one complete comment. The final rendered comment, including controller
metadata, must fit both 60,000 characters and 60,000 UTF-8 bytes. The schema and
prompt derive a 52,000 character/byte authored-text allowance from that shared
bound. The final render remains authoritative because headings and escaping add
size. This is a platform safety bound, not a target report length. Oversized
reports remain intact locally; the controller does not truncate or split them.

The publication boundary escapes reserved `<!-- pr-pipeline:` openers in
authored text while preserving Markdown and code fences. Only controller header
records supply outcome identity and round counting; quoted sentinels and markers
do not.

A finding reference combines an immutable originating review identity with its
report-local ID. `F1` from two reviews is not one finding. Protocol 2 metadata
records a dispatch-derived identity, a canonical authored-report digest, and
the finding IDs. Fixer dispatch pins these references, that digest, and the
exact published-comment digest in `review_handoff`. The complete report enters
the fixer prompt unchanged. Re-review receives the exact originating report and
the same snapshot, with explicit carry-forward rather than prose matching.

Historical comments use the shared authenticated-author predicate from #32.
The adapter preserves their original text and assigns ordinal `F1`, `F2`, ...
IDs only when the controller's historical one-line format is unambiguous.
Ambiguous formats stop with an explicit human-decision explanation. Full new
fixer responses and automatic disposition coverage remain in #88.

## Examples

A clean report can be concise:

```json
{
  "report_protocol_version": 2,
  "verdict": "ready_for_human",
  "reasoning": "The change retains failed-post evidence without changing the successful path.",
  "scope": "Inspected cleanup callers and executed the failed-post regression.",
  "findings": [],
  "verification": "Ran pytest -k failed_post: 2 passed. Author-supplied CI is pending; I did not run the full suite.",
  "risks": "The regression uses a GitHub double. Real service availability is not tested.",
  "observations": ""
}
```

A finding can retain the reproduction and omit a speculative fix:

```json
{
  "id": "F1",
  "severity": "major",
  "location": "worker.py:42",
  "explanation": "When comment publication fails, cleanup deletes the only result.\n\nThe next tick cannot retry publication.",
  "evidence": "Executed:\n\n```sh\npytest -k failed_post\n```\n\nThe retained-result assertion failed: the result file was absent.",
  "resolution": ""
}
```

A blocked reviewer can use `needs_human_decision`, an empty finding array, and
explain that required production access is unavailable in `reasoning` and
`verification`. Human review must assess whether the evidence and limitations
justify the conclusion in each example; length and section presence cannot do so.
