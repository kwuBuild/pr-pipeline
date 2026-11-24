# Local loop process evidence

The native `tick --loop` process publishes its identity so status tools can
verify the recorded process independently of historical tick activity. The
classifier reports one local observation. It does not establish why a process
ended, whether work completed, or whether another machine runs a loop.

## Record ownership and lifecycle

`tick.main` acquires `TickLock`. `_run_loop_locked` installs SIGINT and SIGTERM
handlers, then atomically replaces `.claude/worktrees/.tick-state/loop.json`.
The record belongs to that invocation. Publication uses a temporary file in the
same directory and an atomic rename; readers see an entire old or new record.
The path is relative to the target repository's main checkout, including when
invoked from a linked worktree. Pipeline installation paths do not own state.

A lock-refused invocation does not touch the record. A one-shot tick, including
one scheduled repeatedly by a harness, neither creates nor removes it. A new
native loop replaces stale evidence only after it owns the lock and handlers.

The loop removes its unchanged record before restoring handlers and releasing
the lock. It preserves a replacement or unreadable record. Failure to read its
own process identity or publish the record refuses loop startup with exit 4;
previous evidence remains. Later startup refusal, normal stop, and SIGINT or
SIGTERM run cleanup. SIGKILL or a machine crash can leave the record behind.
Cleanup failures are reported to stderr and leave evidence for the next reader.
No new command sends a stop signal.

Version 1 records contain:

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1`. |
| `invocation_id` | Unique invocation token; cleanup also compares the full record. |
| `pid` | Positive OS process ID. |
| `process_start_time` | Epoch seconds from `process_control.get_process_start_time`, the engine identity source. |
| `recorded_at` | Publication timestamp in epoch seconds. |
| `host` | Local `platform.node()` value; records from a different host cannot verify local identity. |
| `scope` | Requested scope: `{"mode":"full","targets":[]}`, `{"mode":"target","targets":["#117"]}`, or `{"mode":"only","targets":["#117","pr:118"]}`. |

Scope records normalized operator tokens in order. It is the requested scope,
not a new GitHub query or the latest target expansion. Use tick logs for the
last resolved dispatch scope. Host names are a locality guard, not a globally
unique machine identity. Identity verification inherits the platform precision
of the shared process-start source (seconds on macOS/BSD).

## Shared read-only classifier

Python consumers call `tick.classify_loop_process(wt_root=None)`. The CLI emits
the same JSON object and exits 0 for any classified observation:

```bash
# Use a supported Python interpreter, from the target repository:
python3.13 -B /absolute/path/to/install/skills/tick/lib/tick.py loop-status
# Consumers can provide the target's canonical marker root explicitly:
python3.13 -B /absolute/path/to/install/skills/tick/lib/tick.py loop-status \
  --wt-root /absolute/path/to/target/.claude/worktrees
```

The optional `wt_root` resolves at call time to the target's main-checkout marker
root. Bad CLI arguments exit 2. The classifier acquires no lock, writes no
pipeline state, performs no GitHub calls, and never sends a signal. When start
time is unavailable, the shared `pid_is_alive` signal-zero probe checks
existence without delivering a signal. Permission failure cannot prove death.

A matching start time alone does not prove that the process runs. A killed
process whose parent has not yet reaped it is a zombie: it keeps its PID and
start time, and it answers the existence probe, but it cannot run ticks. The
classifier therefore also reads the OS process state through
`process_control.get_process_state` (`ps -o stat=` on macOS/BSD, the state
field of `/proc/<pid>/stat` on Linux). It reads the state before the start
time. A PID cannot be reused while its process or zombie exists, so a later
matching start time shows that the state belongs to the recorded process.
Status and future Mission Control consumers must use this classifier rather
than infer liveness from logs, the lock file, or a PID alone.

Every result contains `schema_version: 1`, `observed_at` (epoch seconds),
`state`, `record_present`, `source_health`, `reason`, and `record`. `record` is
the validated source object, or null when unavailable. Extra fields may be
added without changing version 1; consumers must check the version and handle
unrecognized versions as unavailable evidence.

| Evidence | `state` | `record_present` | `reason` |
|---|---|---|---|
| No record at the observed path | `unavailable` | false | `record_missing` |
| Record cannot be read | `unavailable` | true if presence was observed, otherwise null | `record_unreadable` |
| Invalid JSON, invalid fields, or unsupported record version | `unavailable` | true | `record_malformed` |
| Readable local record, matching process start identity, live process state | `running` | true | `identity_verified` |
| Readable local record, matching process start identity, exited but unreaped (state `Z`, or `X` on Linux) | `not_running` | true | `process_zombie` |
| Readable local record, different process start identity (PID reuse) | `not_running` | true | `identity_mismatch` |
| Start identity or process state unavailable and existence probe proves absence | `not_running` | true | `process_dead` |
| Process identity unreadable and absence not proved | `unknown` | true | `identity_unreadable` |
| Matching start identity, process state unreadable, and absence not proved | `unknown` | true | `process_state_unreadable` |
| Record names a different host | `unknown` | true | `host_mismatch` |

`source_health.record` is `missing`, `unreadable`, `malformed`, or `readable`.
`source_health.process` is `not_checked`, `verified`, `mismatch`, `dead`,
`zombie`, `unreadable`, or `foreign_host`. Source health describes evidence quality;
`state` describes what that evidence supports. Presence and process reads can
race with publication, cleanup, or exit, so the result is an observation at
`observed_at`, not a promise of continuing liveness.

`not_running` concerns only the recorded identity. Missing evidence does not
prove that no loop exists. No result establishes completion or an exit cause,
and there is no `exited` state. A running process can still have stale tick
logs; recent tick logs can still belong to a dead process.

## Dashboard display

`status.sh` prints the classifier state, record presence, reason, source health,
observation time, and available PID/scope in `LOOP PROCESS`. If the classifier
cannot run, it prints `unavailable`, `record_present=null`, and
`classifier_unavailable`. The separate `DISPATCH SCOPE` and `LAST TICK AUDIT LOG`
sections retain their tick-log meaning. Process evidence does not overwrite or
reinterpret those historical sections.
