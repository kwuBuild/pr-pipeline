# Event feed contract

`pr-pipeline events` gives the operator a readable history of local pipeline
progress. The tick writes observations and confirmed handoffs. The reader
formats those records without starting an engine or calling GitHub. A terminal
reader consumes no model tokens. The launcher may run `claude plugin list` to
find installed code; that local discovery does not invoke a model.

## Run

Run the installed launcher from the target repository or a linked worktree.
If an older copied launcher rejects `events`, refresh it using the
[launcher installation instructions](../tick/README.md) first:

```bash
~/.claude/pr-pipeline/bin/pr-pipeline events
~/.claude/pr-pipeline/bin/pr-pipeline events --follow '#27'
~/.claude/pr-pipeline/bin/pr-pipeline events --json pr:81
```

| Option | Behavior |
| --- | --- |
| No target | Show all retained local events. |
| `'#N'` | Show issue N and PR events whose link to N exists in a retained `pr_handoff` record. Quote the `#`. |
| `pr:N` | Show events owned by PR N. |
| `--follow` | Replay retained history, then inspect logs every second until interrupted. |
| `--json` | Emit one version 1 JSON object per event. |

Repository-wide startup failures, crashes, and loop stops remain visible under
both filters. A filter never performs a remote lookup. Missing issue-to-PR
linkage can therefore omit PR history from an issue-filtered view.

The reader resolves state through the target's Git common directory, like
`status`. Installed code and consumer state can live in different directories.
The reader takes no tick lock and writes no labels, markers, caches, or logs.
It can run while a tick writes events. It does not change notifications,
scheduling, engine prompts, or spending limits.

## History and delivery

1. On startup, the reader prints a retained-history notice to stderr. Logs
   normally retain 50 existing ticks plus the newest tick. Records from another
   host, pruned files, failed logging, or older code without confirmation records
   cannot be recovered here. Missing logs produce an explicit notice.
2. The reader presents retained records oldest first, using timestamp, file
   name, and record position for ordering. Each follow scan orders newly visible
   records the same way; late records can have timestamps older than output
   already delivered. Late retained linkage can expose previously buffered PR
   events to an issue filter.
3. Follow mode reads newly appended complete lines, including lines appended
   after `tick_complete`. It normally displays them within about 10 seconds of
   publication. It does not wait for the next tick. New logs are discovered and
   pruned files are dropped from its in-memory window.
4. Each record is consumed once per reader process. Identical observations for
   the same dispatch are suppressed across harvest retries. Repeated cooldown
   observations are suppressed until the relevant item/stage state changes;
   a changed cooldown deadline or a new attempt is visible. Other items do not
   reset this suppression. Distinct crashes, startup failures, loop stops, and
   terminal-label records remain visible even when their projected details
   match. Other observations without a dispatch identity are separate occurrences.
5. Restarting the reader intentionally replays retained history. There is no
   durable delivery cursor or exactly-once guarantee across restarts. Renaming a log preserves its consumed records. Log
   replacement or truncation starts a new local file generation and warns that
   history may be incomplete.

Incomplete final lines remain pending during follow mode. A one-shot read warns
and omits them. Malformed or oversized records (over 1 MiB) are skipped with a
warning. An unreadable log or directory exits 1 and reports history unavailable;
it is not an empty successful result. Invalid arguments or target resolution
exit 2; an unavailable supported Python interpreter exits 3. Ctrl-C exits
normally. Diagnostics use stderr; JSON stdout contains only event objects.
Terminal-label records require an entity of `issue` or `pr` and a positive item
number. An invalid or missing identity follows the malformed-record warning path.

## Events and confirmation

| Event | Meaning | `details` fields |
| --- | --- | --- |
| `launch` | Controller launched a stage engine. | `model`, `effort` |
| `result` | Engine result/usage observation; not confirmation of an external transition. | `reported_cost`, `duration_ms`, `usage_status`, `proposed_action`, `input_tokens`, `cached_input_tokens`, `output_tokens` |
| `pr_handoff` | PR creation or rediscovery, PR label handoff, and issue custody convergence succeeded. | `issue_number`, `pr_number`, `created` |
| `review_handoff` | Review comment publication, if required, and label convergence succeeded. An outcome can also discard a stale review. | `label`, `outcome`, `head_sha` |
| `revision_handoff` | Revision publication and re-review label convergence succeeded. This includes a validated no-change result. | `label`, `outcome`, `head_sha` |
| `retry` | Transient failure was requeued and its cooldown recorded. | `domain`, `failure_class`, `cooldown_until` |
| `cooldown` | Dispatch was skipped for an active provider cooldown. | `domain`, `failure_class`, `cooldown_until` |
| `stalled` | The existing stall reporter detected a live engine with no recent output. | `progress_age_seconds` |
| `terminal` | A terminal workflow label landed. | `label` |
| `stopped` | Loop stopped after quiescence. | `reason`, `source_action` |
| `startup_failure` | Startup could not proceed with the target, configuration, labels, or required tools. | `reason`, `source_action` |
| `crash` | Tick recorded an unexpected failure. | `reason`, `source_action` |

Reviewer launch model/effort are joined from a matching retained
`model_resolved` record when absent on the launch record. Model resolution alone
does not produce a launch event.

An `engine_result_summary` with proposed action `create_pr` can precede a failed
GitHub request. Only the later `pr_handoff` record reports confirmation and
issue/PR linkage. `created: false` means the controller rediscovered an existing
PR; it does not claim a new PR was opened. Confirmation logging is best-effort
and never changes transition authority or recovery ordering. Its absence does
not prove that an external operation failed.

Usage and duration come from existing engine reports. Token counters are
nonnegative integers or null; cached input is part of input, not an extra total. Missing or invalid values
are null, never inferred zero. `reported_cost` is either null or
`{"amount": 1.25, "unit": "USD"}`. It is a provider-reported estimate, not an
invoice or subscription-quota measurement. Raw result excerpts, tool payloads,
and startup environment dumps are excluded. Display strings are bounded and
terminal control characters are removed.

## JSON-lines version 1

Every object has these fields. Scripts must use the structured fields rather
than parse `message`, which is display text and can change.

| Field | Type and meaning |
| --- | --- |
| `schema_version` | Integer `1`. |
| `id` | Opaque local record identity. Stable while that log incarnation remains in place; not a cross-host or archival identifier. |
| `repo` | Resolved `owner/repository`. |
| `ts` | Nonnegative Unix timestamp in seconds (number). |
| `event` | Event kind from the table above. |
| `entity` | `issue`, `pr`, or `pipeline`. |
| `number` | Positive issue/PR number, or null for pipeline events. |
| `stage` | `implement`, `review`, `fix`, or empty when not stage-specific. |
| `engine` | Reported engine name, or null. |
| `dispatch_id` | Controller dispatch identity when present, or null. |
| `details` | Object with the event-specific fields above. Identity fields are required; optional usage and observations can be null. |
| `message` | Readable description. Not a script parsing contract. |

Readers should ignore additional fields and unknown event kinds within a known
schema version. An incompatible change requires a new version. Internal
tick-log records remain an implementation detail; this projection is the
script interface.
