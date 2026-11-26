# Mission Control: component inventory

Status: draft 5 for review in #121. Implementation is tracked in #120. This
document owns the component list, the status vocabulary and the Phase 2 action
rules. [The design document](mission-control-design.md) owns the visual design
language and links here for every status rule.

The data gaps in section 6 map to these issues:

| Gap | Issue |
|---|---|
| 1 (no machine-readable status) and 8 (tick summary is internal) | #116 |
| 2 (reviewer has no progress heartbeat) | #115 |
| 5 (loop process is invisible) | #117 |
| 6 (round count not exported) and 14 (finding counts not in events) | #118 |
| 7 (outcomes missing from events) | #119 |
| 13 (no resolved configuration) | #123 |

The other gaps have no issue. Gaps 3, 4, 9 and 11 are by design: the heartbeat
stores only an mtime, no engine narrative is kept, history is bounded, and engines
on other machines are not visible. Gaps 10 and 12 are read from GitHub by the
monitor itself. #116 and the corrected design are prerequisites for #120.
The other signal issues are optional; their observations remain unavailable
until implemented. #123 has shipped; its export describes a standalone
invocation and does not prove an existing loop's applied settings.

The cut list in section 7 names the early design mockups (`Main`,
`InstrumentPanel`) where each removed element appeared. Those mockups are not in
the repository. [The static mockup](mission-control-mockup.html) shows the
current direction.

## Purpose and scope

This document lists what a Mission Control user needs to see and do, the
components that serve those needs, the states of each component, and the
source of each piece of data. The design document owns the visual design language.
This document does not describe colors, fonts, or materials.

- **Phase 1** is a read-only monitor. It makes no writes and uses no model
  tokens. It reads GitHub, local files, and existing read-only commands.
- **Phase 2** is deferred action design. Each proposal names an existing
  mechanism or a missing command contract. Loop stopping is deferred; neither
  #117 nor #120 implements a stop control.

Current runtime facts were checked against commit `8ab8b28`. Interfaces
assigned to #115–#119 are proposed capabilities, not claims that those
features ship today. #123 has shipped as the read-only `tick config --json`
export ([configuration export schema](../../skills/status/CONFIG.md)). "GAP" marks missing data; section 6 lists each gap.

### Terms

| Term | Meaning |
|---|---|
| Tick | One run of `skills/tick/lib/tick.py`: HARVEST (collect finished engines), then DISPATCH (start new engines). |
| Loop | `tick --loop`: one process that runs ticks and waits between them. |
| Engine | A headless worker: `claude -p` (implementer, fixer) or `codex exec` (reviewer). |
| Task | One unit of work as the user sees it: an issue, its closing PR, or a PR with no issue. |
| Marker | `.claude/worktrees/{issue,pr}-N/dispatch.json`. It records an engine that **this machine** launched. Markers are machine-local. |
| Heartbeat | `<worktree>/heartbeat`. The implement/fix stream wrapper updates its mtime, at most once per 5 s, when the engine emits output. The mtime is the only content. |
| Pickup label | A stage candidate label; other admission checks still apply: `ai:available` (implement), `ai:implemented` or `ai:revised` (review), `ai:changes-requested` (fix). Source: `pipeline.STAGE_PICKUP_LABELS`. |
| In-flight label | `ai:implementing`, `ai:reviewing`, `ai:fixing`. It means "an engine or a person holds this item". Source: `pipeline.STAGE_IN_FLIGHT_LABELS`. |
| Hold | `ai:needs-human-decision` or `ai:blocked` (`pipeline.HOLD_LABELS`). A human hold outranks every other label. |
| Workflow custody | Who may move labels and advance the stage. A hold moves it to the human. |
| Process custody | Who owns the running process and its deadline. It never leaves the tick. |
| Tick evidence | What retained logs say about ticks. Process identity is a separate observation supplied only after #117. |
| Event feed | `pr-pipeline events --json`, schema version 1 (`skills/status/EVENTS.md`). |

The complete label vocabulary is the 11 `LABEL_*` constants in
`skills/tick/lib/pipeline.py` (`pipeline.WORKFLOW_LABELS`): `ai:available`,
`ai:implementing`, `ai:implemented`, `ai:revised`, `ai:reviewing`, `ai:fixing`,
`ai:changes-requested`, `ai:ready-for-human`, `ai:max-iterations`,
`ai:needs-human-decision`, `ai:blocked`. `labels.py vocabulary` prints this set
as JSON.

## 1. User questions, in priority order

| # | Question | Why this rank | Component that answers it |
|---|---|---|---|
| 1 | Is anything stuck or broken? | A stalled or overdue engine spends money. A dead loop leaves finished work unharvested. | Alarm strip (C3) |
| 2 | Can I trust what I see? | Every other answer is wrong if a source failed or the data is old. | Freshness stamp (S9), Source health (C15) |
| 3 | What needs me? | The pipeline stops and waits at decision, round-limit, ready, and blocked labels. | Your turn list (C4) |
| 4 | Is the pipeline running, and when does the next tick come? | Nothing advances between ticks. | Loop status (C2) |
| 5 | What is running on this machine, and is it producing output? | Shows spend in progress and early stall signs. | Running here (C6) |
| 6 | Where is each task, and what happened last? | The main tracking view. | Pipeline map (C7), Task list (C8), Task row (C9) |
| 7 | What is the full history of one task? | Diagnosis and trust in an outcome. | Task detail (C10), Task timeline (C10a) |
| 8 | Is any work at an in-flight label with no engine here? | These items can strand. No tick ever clears them. | Unattributed in-flight (C11) |
| 9 | What just happened across the pipeline? | Situational awareness without a terminal. | Event feed (C13) |
| 10 | Which provider cooldowns were recorded? | Shows observed provider constraints without predicting a queued item's eligibility. | Cooldown banner (C14) |
| 11 | What did a task cost and how long did it take? | Spend awareness. | Cost and duration readout (S10) |
| 12 | (Phase 2) What should the pipeline do next? | Hides the CLI for normal use. | Work picker (C16), Run list (C17), Run controls (C18) |

## 2. Information architecture

The design document owns the page order
([Page layout](mission-control-design.md#page-layout)). This section maps each
component to a region of that layout. The page has one monitor view, and Phase 2
adds one separate "Plan work" view.

| Design region | Components |
|---|---|
| Toolbar | Header and scope bar (C1), Loop status (C2), Freshness stamp (S9) |
| Large title and summary | Summary counters (C5), written as one sentence |
| Alarm strip | Alarm strip (C3). It sits above the map, so triage comes first whenever an alarm exists. |
| Pipeline map | Pipeline map (C7) with Stage stations (S6) and Task tokens (S2). Selecting a station opens the Task list (C8) for that station. |
| Running on this machine, and Your turn | Running here (C6), including Unattributed in-flight items (C11) as "Custody unknown" rows; Your turn list (C4) |
| Recent events | Event feed (C13), Cooldown banner (C14) |
| Source health | Source health (C15), Parked items (C12) |
| On selection | Task detail (C10), a side panel, so the surrounding context stays visible |
| Plan work view (Phase 2) | C16 to C22. A separate view, so the monitor stays read-only and a user cannot start spend from the monitor by accident. |

"The machinery is broken" (an alarm) and "it is your turn" need different
responses, so the Alarm strip and the Your turn list stay separate regions.

### Server boundary (applies to every region)

- The server is Python 3.11+ standard library only (`http.server`, `json`,
  `subprocess`, `tomllib`).
- It listens on `127.0.0.1` only. It rejects a request whose `Host` header is
  not the loopback address and port (DNS rebinding defense). It rejects a
  request with a cross-site `Origin` or `Sec-Fetch-Site: cross-site`. It sends
  no CORS headers.
- Phase 1 serves `GET` only. Phase 2 `POST` requests also need a per-session
  token that the page receives at load time.
- The server runs from the target repository, in the same trust domain that
  launched the engines (sandbox off). Otherwise `kill -0` reads every live
  engine as dead (`skills/status/SKILL.md`, "Run").
- One shared collector polls local snapshots through #116 every 5 s and
  reads cooldown/counter observations with explicit source health. Run one
  `events --json --follow` child and consume its structured health separately
  from task history. Poll GitHub every 60 s or slower with rate-limit backoff.
- Bound one-shot commands, event-reader shutdown and restart backoff. An idle
  follower is not a failure. A failed GitHub request must not block local reads.
  Suppress identical replay, merge documented launch enrichment, and preserve
  incompatible event-ID reuse as a new collector generation with incomplete
  history, as specified in #120.
- Phase 1 never runs `events --mark` or `--cursor`, because those write a
  cursor file.

## 3. Component inventory

Source paths are relative to the target repository root. "Install" means
`${CLAUDE_PLUGIN_ROOT}`. Commands are the ones `skills/status/status.sh`
already runs.

### 3a. Shared parts

| ID | Name | Question it answers | Data source | States | Interactions | Phase | Accessibility |
|---|---|---|---|---|---|---|---|
| S1 | Status indicator | What condition is this item or source in? | Derived by the rules in section 4 | normal; waiting; your turn; warning; alarm; unknown; stale | Focus or hover shows the rule and source that produced the state | 1 | Always shows a text word. Shape and text carry meaning, not color alone. Unknown has its own word and shape. |
| S2 | Task token | Which item is this? | GitHub REST item `number` and `pull_request` key; marker directory prefix `issue-` or `pr-` | issue; PR; issue with PR; malformed (`?`, marker `pr_number` unreadable) | Activate opens Task detail; secondary link opens GitHub in a new tab | 1 | Accessible name "Issue 439: <title>" or "Pull request 468: <title>". A map token shows no title, to keep stations compact. Every list, alarm and detail heading shows the title (GitHub REST `title`) beside the ID. Use the directory prefix, not marker `pr_number`: `status.sh` prints implementer issue numbers as `PR#`. |
| S3 | Time-ago label | How long ago did this happen? | Event `ts`; heartbeat mtime; marker `start_time` (epoch, set at spawn); server read time | under 1 min; minutes; hours; days; unknown (no timestamp); clock skew (future time, show absolute) | Focus or hover shows absolute local time | 1 | Use `<time datetime>`. Update text at most every 30 s. Never announce updates. |
| S4 | Deadline meter | How long until the tick treats this engine as hung? | Marker `deadline` (epoch) and `start_time`. Defaults: implement and fix 3600 s, review 1200 s (`*_timeout_seconds`) | within deadline; under 20 % left; overdue (shared classification `hung`); lease (`state: launching`, no PID); no marker (hidden) | None | 1 | Text such as "18 min left of 60". Meter has `role="meter"` and `aria-valuetext`. |
| S5 | Activity bar | Has the engine produced output recently? | #116 heartbeat-backed `progress_at`, sampled every 5 s and held in memory only. Implement and fix today; review after #115. | output seen in interval; no output in interval; no data (before server start or sample gap); not applicable (reviewer: no progress signal); unreadable | None | 1 | Text equivalent such as "Output in 9 of the last 15 min; last 2 min ago". "No data" must differ from "no output". Binary per interval only; no counts exist (GAP 3). |
| S6 | Stage station | How many tasks are at this stage? | Label counts from the GitHub read, mapped by section 4.2 | empty; has tasks; has an alarm task (alarm border; every alarm token, including Attention, uses the alarm marker); crowded (more tasks than the token preview shows: "+N more"); selected; unavailable (GitHub read failed) | Activate, or activate "+N more", to open the Task list for this station. The token preview follows the design document ("Crowded stations"). | 1 | A button with a full name such as "Queued: 7 tasks, 2 shown" or "Reviewing: 2 tasks, 1 alarm". The selected station has `aria-pressed="true"`. |
| S7 | Label chip | Which exact label does GitHub show? | GitHub `labels[].name` starting with `ai:`; vocabulary from `labels.py vocabulary` | known workflow label; hold label; unknown `ai:` label; vocabulary unavailable | None | 1 | The visible text is the exact label name. |
| S8 | Engine badge | Which engine and model run this stage? | Marker `engine` (`claude`, `claude-ds`, `codex`) and `stage`; `launch` event `details.model` and `details.effort` | known; model unknown (`null`); no engine | None | 1 | Text only, for example "codex reviewer, gpt-5.5". |
| S9 | Freshness stamp | Is this data current? | Server-side time of the last successful read, per source | current; stale (older than 3 poll intervals); failed (with error class) | Activate opens Source health (C15) | 1 | Text such as "GitHub as of 14:31". Announce a change to stale or failed once, not on every poll. |
| S10 | Cost and duration readout | What did this task cost, and how long did engines run? | `result` events: `details.reported_cost` (`{amount, unit: "USD"}` or `null`), `duration_ms`, `input_tokens`, `cached_input_tokens`, `output_tokens` | subtotal of retained local observations; partial (missing values or unproved history coverage); unknown (no usable values) | None | 1 | Label the value "provider-reported estimate from retained local results". Never imply a complete task total or show `null` as zero. |

### 3b. Monitor components

| ID | Name | Question it answers | Data source | States | Interactions | Phase | Accessibility |
|---|---|---|---|---|---|---|---|
| C1 | Header and scope bar | Which repository and which machine am I looking at? | `skills/tick/resolve-target.sh` (the same resolver as `status.sh`); host name; S9 per source | normal; target resolution failed (resolver exits 2: whole-page error that names the cause); Python 3.11+ unavailable (warning: progress, tick logs, and vocabulary unavailable) | None | 1 | The page heading contains the repository. Permanent text: "Engines shown: this machine only". |
| C2 | Tick outcome and loop evidence | Did the last tick succeed, and what scheduling/process evidence exists? | #116 `dispatch-scope --json`: source health, scope and expansion, distinct tick timestamps, completed `tick_state`, `last_successful_tick_completion_at` with retained-evidence coverage, cadence and separate action counts; current GitHub items for waiting-work counts; #117 shared process classifier when available; failure/stop events | see section 4.1 | Expand shows scope, source times and recorded action counts. `escalated` and `reverted` count decisions, not confirmed remote changes. | 1 (display); 2 adds C18 | Announce outcome and activity changes separately in a polite live region. |
| C3 | Alarm strip | Is anything stuck or broken? | The alarm rules in section 4.6 | hidden (no alarm and every source current; the strip never shows "No alarms" text); "Cannot check: <source> unavailable"; list of alarms | Each alarm names its task ID and title, and opens its Task detail or Source health. The strip lists every alarm; it is never truncated. | 1 | `role="alert"` only for a newly added alarm. The heading carries the count. |
| C4 | Your turn list | What needs me? | GitHub labels `ai:needs-human-decision`, `ai:max-iterations`, `ai:ready-for-human`, `ai:blocked`; conflicting-label items (section 4.2); escalation reason from `terminal` event `details.reason` (can be `null`) | empty ("Nothing waiting for you", only when GitHub is current); unavailable; grouped list | Each item shows its ID and title. Open Task detail; open on GitHub. Phase 2 adds Guidance (C20) and Release block (section 5) | 1 | Group headings name each reason in text. Ready-for-you and needs-decision are separate groups. |
| C5 | Summary counters | How much is in each bucket? | Counts from C3, C4, C6, and `ai:available` issues | number; unknown ("—" with the word "unknown"); stale | Activate moves focus to the section | 1 | Never show 0 for an unknown count. Accessible name such as "Alarms: unknown, GitHub unavailable". |
| C6 | Running here | What is running on this machine, and is it producing output? | The #116 JSON from `tick.py engine-progress --wt-root .claude/worktrees`: per marker, `engine`, `stage`, `pid`, `dispatch_id`, `marker_mtime`, `start_time`, `deadline`, `stall_seconds`, the shared classification, the identity result, and the progress source and age; plus source health. Never `kill -0` or result-file size. | see section 4.3; empty ("No engines on this machine"; the text must not say "nothing is running" because other machines are invisible) | Each row shows the task ID and title. Open Task detail; copy the worktree path (to read `stderr.log`) | 1 | A table with row headers. The progress state is text. |
| C7 | Pipeline map | Where is all the work? | Stage counts by section 4.2 | per S6; shows the review and fix loop (`ai:changes-requested` → `ai:fixing` → `ai:revised` → `ai:reviewing`) and four ends (Ready for you, Needs decision, Round limit, Blocked) | A station opens its Task list below the map. Stations keep a fixed size, so the map and its arrows never move when a count changes. | 1 | The map is a list in DOM order. Loop arrows have a text description. |
| C8 | Task list | Where is each task, and what happened last? | Open GitHub items with an `ai:` label, with their `title`; issue-to-PR grouping from `pr_handoff` events (`issue_number`, `pr_number`) or the PR's `closingIssuesReferences` (GitHub GraphQL read); newest event per task from `events --json` | normal; empty (no labeled open items); filtered empty; unavailable; stale | Default sort by S1 class: alarm, your turn, warning, unknown or stale, normal, waiting; within one class, by number. This order is not a queue position. Filter by station; a station's token preview uses the same order. Select opens Task detail. | 1 | Keyboard list or grid. The selected row has `aria-selected`. |
| C9 | Task row | One task at a glance | S2 with the item title, S1 (stage and condition), S8, S3 (last output), S4, and the newest event `message` (display text only; never parsed) | union of sections 4.2 to 4.4 | Select; open on GitHub | 1 | The row reads as one sentence to a screen reader. |
| C10 | Task detail | What is the full state of this task? | The item title (GitHub REST `title`); labels (S7); stage and condition (section 4); marker fields; the recorded failure counter `.claude/worktrees/<N>-attempts` (PR, shared by review and fix) or `.claude/worktrees/issue-<N>-attempts` (section 4.4, "Recorded failure counter"); review round count (GAP 6); cost (S10); worktree path | loading; normal; not in the current open-item snapshot (section 4.2); no retained events | Open issue or PR on GitHub; copy worktree path. Phase 2 adds actions from section 5. | 1 | The panel heading shows the ID and the title. Focus moves to the heading on open. Escape closes the panel and returns focus to the row. |
| C10a | Task timeline | What happened, in order? | `events --json '#N'` and `events --json pr:M`. Kinds: `launch`, `result`, `pr_handoff`, `review_handoff`, `revision_handoff`, `retry`, `cooldown`, `stalled`, `terminal`, `attention`; after #119, `requeued` and `held_result_published` | normal; partial ("History starts at <oldest retained tick>; other machines' events are not included"); none | Expand an entry to show its structured `details` | 1 | Ordered list. Each entry has a time and a text. |
| C11 | Unattributed in-flight items | Is any work at an in-flight label with no engine on this machine? | GitHub items at `ai:implementing`, `ai:reviewing`, or `ai:fixing` with no matching local marker directory | none; list; unavailable | Show the custody runbook text (tick README, "Markerless in-flight items"); open on GitHub | 1 | Text says "may be another machine, a manual pickup, or a dead engine". Never use the words "stuck" or "dead". |
| C12 | Parked items | Does any item carry only unknown `ai:` labels? | GitHub labels compared with `labels.py vocabulary` (the `PARKED ITEMS` rule in `status.sh`) | none; list; vocabulary unavailable (raw labels still shown) | Open on GitHub | 1 | Text says that no dispatch query selects these items. |
| C13 | Event feed | What just happened? | `~/.claude/pr-pipeline/bin/pr-pipeline events --json --follow` (version 1 objects; new lines normally visible within about 10 s) | streaming; replaying history (each reader start replays retained history); reader failed (exit 1: history unavailable; exit 2: bad arguments or target; exit 3: no supported Python); empty | Filter by task; pause auto-scroll; activate an entry to open its task | 1 | `role="log"`, polite, batched. A "pause updates" control. |
| C14 | Cooldown banner | Which provider cooldown was recorded? | `.claude/worktrees/.engine-cooldowns/<domain>.json` (`domain`, `until`, `reason`); domains are `claude` and `claude-ds` only; `retry` and `cooldown` events (`domain`, `failure_class`, `cooldown_until`) | hidden; recorded cooldown until a time; unreadable (warning) | None | 1 | Name the provider, record time and absolute end time. This does not establish that any particular queued item is blocked. |
| C15 | Source health | Can I trust what I see? | Per-source result of the last read: GitHub, marker scan, `engine-progress`, `dispatch-scope`, events reader, vocabulary, Python resolver; oldest retained tick log time | ok; degraded; failed | Expand shows the error class | 1 | Text per source. |

### 3c. Phase 2 components

| ID | Name | Question it answers | Data source | States | Interactions | Phase | Accessibility |
|---|---|---|---|---|---|---|---|
| C16 | Work picker | Which open issues could a new configured invocation pick up? | GitHub open issues and labels; #123 configuration resolved for the proposed invocation; custody confirmation under section 5 | candidates; none; ineligible under that invocation; unavailable | Queue (section 5, action 1) | 2 | Name the invocation context and the reason for a disabled row. Do not infer an existing loop's identity from a standalone export. |
| C17 | Run list builder | Which items should run, and which comes first within each stage? | User's selection, turned into `--only` tokens (`#N`, `pr:N`); each item's current labels | empty; ready; item not eligible (no pickup label, hold, in-flight label, other author); another tick holds the lock | Add, remove, reorder; Start list run. List order sets priority within each stage only. Dispatch runs reviewers, then implementers, then fixers, so a later review item can start before an earlier fix item (`skills/tick/README.md`, "Dispatch list (`--only`)"). The builder and the Start confirmation say so. | 2 | Reorder works by keyboard (move up and move down buttons). |
| C18 | Run controls | Start pipeline work | Child processes the server started, exit codes and C2; settings resolved for the proposed invocation | idle; starting; observed child running; exited with code and meaning (0 ok; 1 reconcile failed or bad target; 2 dispatch gate held or scope unreadable; 3 startup/configuration failure; 4 crash; 5 lock held; 6 unconfigured target) | Proposed Start loop, Start list run and Run one tick. Stop controls are deferred. | 2 | Each button names its effect, for example "Start loop (may launch engines)". |
| C19 | Hold control | Stop the pipeline from advancing this task | Item labels; local marker | available; already held; not allowed | Add hold (section 5, action 7) | 2 | The confirmation text says that the engine keeps running. |
| C20 | Guidance composer | Tell the fixer what to change on a held PR | PR labels; local marker; operator login | allowed; not allowed (with reason); posted | Write and post a PR comment (section 5, action 9) | 2 | Labelled text area; the character count is text. |
| C21 | Confirmation dialog | Do I really want this effect? | The action's preconditions, re-read at open time | open; preconditions changed (block and explain) | Confirm; cancel | 2 | Modal dialog with focus trap. The default focus is Cancel. |
| C22 | Action result | Did my action land, and what happens next? | Re-read of the item's labels after the write; the next `launch` or handoff event | pending; confirmed; refused (precondition changed); failed (GitHub error); unverified (write sent, re-read failed) | Retry a failed read; open Task detail | 2 | Polite live region. "Unverified" is a separate state, never "done". |

## 4. Status vocabulary

A task has one **stage** (from GitHub labels) and, on this machine, at most one
**engine condition** (from local files). Flags (section 4.4) add context. The
UI evaluates each rule list top to bottom; the first match wins.

### 4.1 Tick outcome, scheduling evidence and loop process

These are separate facts. Read them from the #116 JSON and source health;
never parse display text or internal tick logs. The current GitHub snapshot
supplies waiting-work counts. #117 adds a separate process observation.

#### Last tick outcome

| Order | Shown as | Rule | Class |
|---|---|---|---|
| 1 | Tick outcome unavailable | Relevant snapshot evidence failed or is incomplete | unknown; source failure is reported in C3/C15 |
| 2 | Last tick failed | Newest completed `tick_state.status` is `FAILED`, or retained evidence proves a startup/crash failure has no later successful completion | alarm |
| 3 | Last tick incomplete | Newest completed status is `PARTIAL`, with no unresolved later startup/crash failure | warning; show time and recorded reason when available |
| 4 | Last tick completed | Newest completed status is `ok`, with no unresolved later failure | normal; describes that tick only |
| 5 | Tick outcome unknown | No completed status, or an unrecognized/missing status | unknown |

A failed tick's own `tick_complete` does not clear its fault. A startup/crash
fault resolves only when #116 reports `last_successful_tick_completion_at`
later than that fault. A null timestamp does not prove that no success occurred.
If retained-evidence coverage or source health cannot establish recovery, show
"Earlier fault recovery unknown" beside the newest completed outcome; do not
invent an unresolved alarm or claim recovery. This uncertainty takes precedence
over the table's normal outcome. Preserve the last outcome while a new tick is
in progress.

#### Scheduling evidence

| Order | Shown as | Rule | Class |
|---|---|---|---|
| 1 | Tick evidence unavailable | Snapshot source health reports a read failure or required evidence is unavailable | unknown |
| 2 | Loop running; tick evidence stale | Evidence is `STALE` and #117 verifies the recorded loop as `running` | alarm |
| 3 | Loop ended without cleanup | Evidence is not `ended`, and #117 reports `not_running` with `record_present: true` | alarm when a local dispatch marker exists or the current pickup count (see [Current work with stale evidence](#current-work-with-stale-evidence)) is above zero; unknown ("Cannot check: <source> unavailable") when that count is unknown; otherwise warning |
| 4 | Tick started; completion not observed | Evidence is `active` with a start but no completion | scheduling evidence; does not clear a failed or partial outcome |
| 5 | Next tick due by HH:MM | Evidence is `active` with a completion and advertised next wake | scheduling evidence; does not prove a loop runs or that the tick succeeded. When #117 reports `unavailable` with `record_present: false`, show "Next wake advertised HH:MM; no loop record on this machine" instead |
| 6 | Stopped: reason | Evidence is `ended` from a recorded stop or cadence `stop=true` | scheduling evidence; retain the last tick outcome |
| 7 | Tick evidence stale | Evidence is `STALE`, and #117 is absent or reports `unknown` or `unavailable` | alarm while a local dispatch marker exists; otherwise warning with current waiting-work count |
| 8 | No tick evidence | No evidence and the source was read successfully | warning |

"Next tick due by" is an upper bound. A loop wake nudge can start a tick sooner;
do not display a second-by-second countdown. Recent scheduling evidence never
makes a failed, partial or unknown outcome look normal.

Row 3 depends on the #117 record lifecycle. Only `--loop` publishes the record.
A clean exit removes its own record, and a lock-refused invocation leaves the
record unchanged. Therefore a retained record with a dead process means that
the loop ended without its own cleanup: for example, SIGHUP from a closed
terminal, SIGKILL, the OOM killer or a host reboot. The tick installs stop
handlers only for SIGINT and SIGTERM (`tick.py`, `_run_loop_locked`). One-shot
ticks and normal stops do not produce this state. Crashes and startup failures
end through the normal exit path and raise "Last tick failed". Row 3 does not
claim why the process ended. If #117 changes this lifecycle, for example by
cleaning up on SIGHUP, review row 3 again.

#### Process observation (after #117)

Show the classifier's `running`, `not_running`, `unknown` or `unavailable`
result beside tick evidence. `not_running` with `record_present=true` reads
"Loop record remains; recorded process is not running". This does not prove
why it ended or whether it completed its work; scheduling evidence row 3 owns
its alarm class. A missing record is `unavailable` with `record_present=false`;
it is not proof that no loop exists, so it stays a warning. An unreadable
identity is `unknown`. There is no `exited` state. Before #117, show "Loop
process observation unavailable".

#### Current work with stale evidence

A one-shot tick records cadence too; its evidence becomes stale without any
loop having failed. Without #117 process evidence, current waiting work alone
is a warning, not proof of a dead loop. A local marker, a verified
running-but-stale loop, or a retained loop record whose process is not running
(row 3) raises the alarm described above.

1. Count items currently at Queued, Awaiting review or Waiting for fixer
   (section 4.2), within the last observed dispatch scope. Full queue includes
   all those items; a target or `--only` scope uses its recorded expansion.
2. Say "N items at a pickup label; no tick since HH:MM". This is not proven
   dispatch eligibility: trust, invocation configuration and admission still
   apply.
3. Show N as unknown when GitHub is not current or the scope/expansion is
   unavailable. Never substitute the last tick's `tick_state.queue` value.

### 4.2 Stage (GitHub labels)

Source: the paginated open-item read that `status.sh` uses
(`gh api repos/<repo>/issues?state=open&per_page=100 --paginate --slurp`).
Only workflow labels count; holds are `pipeline.HOLD_LABELS`.

| Order | Shown as | Rule | Class |
|---|---|---|---|
| 1 | Unknown | No successful GitHub read yet, or the item is missing from a read that failed. After a failure, show the last good stage with a stale flag. | unknown |
| 2 | Needs decision | The labels include `ai:needs-human-decision`. It outranks every other label. | your turn |
| 3 | Blocked | The labels include `ai:blocked`. | your turn |
| 4 | Unsupported workflow label for this item type | A known stage label is on an entity that its stage does not accept: implement labels require issues; review/fix labels require PRs. Show exact labels and explain that this combination is not dispatched. | your turn |
| 5 | Conflicting labels | Two or more non-hold workflow labels, except one in-flight label with its own stage's pickup label (a known partial relabel; show the in-flight stage with an "extra label" flag). | your turn |
| 6 | Round limit | `ai:max-iterations`. Reviewer harvest or fixer admission sets it when the round cap (`max_iterations`) is used; approval at the cap still promotes. | your turn |
| 7 | Ready for you | `ai:ready-for-human` | your turn |
| 8 | Fixing | `ai:fixing` | see 4.3 |
| 9 | Reviewing | `ai:reviewing` | see 4.3 |
| 10 | Implementing | `ai:implementing` | see 4.3 |
| 11 | Waiting for fixer | `ai:changes-requested` | waiting |
| 12 | Awaiting review / re-review | `ai:implemented` / `ai:revised` | waiting |
| 13 | Queued | Issue at `ai:available` | waiting; author eligibility is not asserted in initial Phase 1 (gap 13) |
| 14 | Parked / unsupported combination | Only unknown `ai:` labels, or a labeled item matched no defined stage rule. Keep its raw labels visible. | your turn (owner decides) |
| — | (merged into its PR) | Issue with no `ai:` label and exactly one open closing PR: the PR's stage is the task's stage | — |

An item that is absent from the current open-item read is shown as "not in the
current open-item snapshot". Its labels may have changed, access may have
changed, or it may be closed. Read the item (its `state`, and for a PR its merge
status) before showing "closed" or "merged". If that read fails, keep the last
known state and mark it uncertain.

Unsupported combinations, conflicting labels and parked items are not map
stations. Keep all of them in Your turn and Source health; never silently omit
a labeled item. Holds retain their higher priority.

### 4.3 Engine condition (this machine only)

Source: the #116 JSON from `tick.py engine-progress`: the shared harvest
classification (`complete`, `failed`, `live`, `stale`, `hung`, `launching`), the
identity result (`matched`, `mismatched`, `unknown`, `dead`), the progress source
(`heartbeat`, `start_time`, `none`) and its age, and source health. This is the
same classification that harvest and the stall watcher use. The UI never
computes a stall, a liveness result or a completion itself. It never infers them
from marker fields, `kill -0` or result-file size: a non-empty result file does
not prove reviewer completion, a wrapperless result also needs process death,
and a failed PID probe does not prove that the process group is dead. Until #116
lands, these rows cannot be shown faithfully.

| Order | Shown as | Rule | Class |
|---|---|---|---|
| 1 | Engine evidence unavailable | Source health reports an unreadable/malformed marker or a failed read of heartbeat metadata, completion envelope, result metadata or another required observation | alarm; name the failed source and do not present affected fields as observed facts |
| 2 | Launching | Classification `launching` (a pre-spawn lease, no `pid`) | normal; alarm if #116 reports a tick completion newer than observed `marker_mtime` and classification is still `launching`; unknown if either observation is unavailable |
| 3 | Overdue | Classification `hung` | alarm: "The next tick attempts termination if identity is proven; current labels and custody determine subsequent workflow changes" |
| 4 | Completed result awaiting reconciliation | Classification `complete` | normal while fresh tick evidence is active and its known outcome is not failed/partial; otherwise alarm. A hold prevents stage advancement; the tick publishes the held outcome before releasing custody. |
| 5 | Engine failed, awaiting reconciliation | Classification `failed` | waiting; alarm when fresh active tick evidence is absent or the last outcome is failed/partial |
| 6 | Exited without accepted completion | Classification `stale` | waiting ("the next tick reconciles current labels; requeue or escalation requires continuing workflow custody"); alarm when fresh active tick evidence is absent or the last outcome is failed/partial |
| 7 | Stalled N min | Classification `live` and progress verdict `STALLED-Nm` | alarm (advisory: nothing is killed or relabelled) |
| 8 | Progress unknown | Classification `live` and progress verdict `?` | unknown |
| 9 | Last output N min ago | Classification `live`, progress source `heartbeat` | normal. This is the only state that shows recent output (the breathing light). The text never says "healthy" or "working"; `quiet-Nm` is not a health verdict (`skills/status/SKILL.md`, "Progress column"). |
| 10 | No output observed since launch | Classification `live`, progress source `start_time`: no heartbeat yet, so the clock runs from dispatch plus the startup grace | normal |
| 11 | Running, no progress signal | Classification `live`, progress source `none` (the reviewer stage, until #115) | normal but visibly unmonitored: show only elapsed time and deadline |
| 12 | Custody unknown | In-flight stage (Implementing, Reviewing or Fixing) and no local marker directory | unknown. It is never an alarm and never healthy. |

Identity `unknown` adds the flag "identity not verified" to rows 2 to 11. The
identity check refutes liveness; it does not confirm it.

### 4.4 Flags

| Flag | Rule | Source |
|---|---|---|
| Attention: reason | An open `attention` episode for the item: `kill_failed`, `escalation_comment_retry`, `corrupt_marker_escalate`, `terminal_label_failed`, or `label_convergence_unverified`. A later `launch`, confirmed handoff or `terminal` ends the episode. After #119, `requeued` and `held_result_published` also end the episodes they resolve; a later failure is a new episode. | events |
| Held, engine still running | A hold label and a live local marker. Process custody stays with the tick. | labels, marker |
| Recorded provider cooldown | Show a recorded `retry`/`cooldown` observation with its provider and time, or the provider-wide banner C14. Initial Phase 1 does not predict which queued item is blocked by a current cooldown. | recorded events and cooldown source health; gap 13 |
| Recorded failure counter: N | Show the stored positive counter without a denominator or remaining-attempt estimate. It can increase after dispatch, engine or harvest failures and is shared by review/fix for a PR. Success, held-result publication and terminal cleanup can clear it. It is not a launch ordinal. Unreadable/malformed values are unknown. | `.claude/worktrees/[issue-]<N>-attempts` |
| Re-review | Stage came from `ai:revised` | labels |
| Escalation reason | A `terminal` event for `ai:needs-human-decision` with a non-null `details.reason` | events |

### 4.5 Stall threshold

- The threshold is the marker's `stall_seconds`. Dispatch records the
  operator's `engine_stall_seconds` there. The shipped value is 1800 s (30 min).
  Startup refuses a value of 600 s or less, or a value at or above the
  implementer or fixer deadline. #115 extends this validation to the review
  deadline and rejects incompatible explicit settings; it does not silently disable review stall reporting.
- Before the first heartbeat exists, the clock starts at `start_time`, and the
  marker's `stall_startup_grace_seconds` (shipped 300 s) is added.
- A stall is reported only for a live engine with identity not refuted. A reused
  PID or an overdue engine renders `quiet-Nm`, not `STALLED`.
- The reviewer stage has no progress heartbeat. It cannot stall in the UI. Its
  only bound is `review_timeout_seconds` (shipped 1200 s). #115 adds a
  reviewer heartbeat and a 3600 s shipped deadline together; until it lands,
  preserve the current limit.

### 4.6 What counts as an alarm

An alarm is any of: Last tick failed; a verified running loop with stale tick
evidence; stale tick evidence while a local dispatch marker remains; a loop
that ended without cleanup while a local dispatch marker remains or current
pickup work exists in its recorded scope (section 4.1, scheduling evidence
row 3); Engine
evidence unavailable; stuck Launching; Overdue; Stalled; a completed, failed
or stale engine awaiting reconciliation without fresh active tick evidence or
after a failed/partial tick; an unresolved Attention flag; a failed required
source. Last tick incomplete is a warning. Other your-turn items are not alarms.

### 4.7 Unknown and stale data

1. Each source has a freshness stamp (S9). Local sources are stale after 15 s
   without a successful read. GitHub is stale after 3 missed polls.
2. A failed source makes its components say "unavailable: <source>". They never
   show an empty list or a zero.
3. A count that depends on a failed source shows "—" and the word "unknown".
4. A zero alarm count, a hidden alarm strip and "Nothing waiting for you"
   appear only when every source they depend on is current.
5. The normal (healthy-looking) indicator never appears for: Unknown, Progress
   unknown, No engine on this machine, Running with no progress signal, or any
   item whose data is stale.
6. Event-reader health and event-ID replay behavior follow #116 and #120.
   No new domain event is not evidence that polling failed.
7. Event history is bounded. Tick logs keep 50 existing ticks plus the newest
   one (`tick_log_open(keep=50)`). At the shipped 270 s cadence that is about
   3.8 hours. The timeline says where retained history starts.

## 5. Phase 2 actions

### Rules for every action

1. The server acts with the operator's `gh` token. Before any write it checks
   that `gh api user` returns the `[operator] login`. The tick trusts issues
   and PR comments only from that login, so a different login would queue work
   that never runs.
2. Immediately before a write, the server re-reads the item's labels and the
   local marker directory. If anything changed since the user saw it, the server
   refuses and shows the new state.
3. Label writes use explicit `--add-label` and `--remove-label` arguments
   (`docs/working/pr-guidelines.md`, "Transitions").
4. The UI never changes a label on an item with a local marker, except to add a
   hold. It never removes a hold on its own. A missing local marker proves
   nothing about custody, because an engine on another machine leaves no marker
   here (`docs/working/pr-guidelines.md`, "Manual pickup is valid custody").
   Every non-hold label mutation requires confirmation that no other machine
   owns the item; if custody cannot be established, refuse and show the runbook.
   A label re-read alone does not establish remote process custody.
5. After a write, the server re-reads the labels and reports confirmed,
   refused, failed, or unverified (C22).
6. The tick owns every label transition after dispatch. The UI starts the tick;
   it does not imitate it.

### Actions

| # | Button | Mechanism today | Authority and safety | Confirmation |
|---|---|---|---|---|
| 1 | Queue this issue | Add `ai:available` to an open issue with no `ai:` label (human transition in pr-guidelines) | Human may label an item with no marker. Only issues authored by the operator are dispatched (`gh issue list --author <login> --limit 200`); disable the button for other authors. The tick sets no sort order and reads the CLI default order, so the UI must not promise a queue position. | Yes; confirm cross-machine custody under rule 4. Show C22. |
| 2 | Remove from queue | Remove `ai:available` when the labels are exactly `ai:available` and no local marker exists | A tick on any machine may lease the item between read and write. If the re-read shows `ai:implementing`, refuse. | Yes; confirm cross-machine custody under rule 4. |
| 3 | Run these items | Start `~/.claude/pr-pipeline/bin/pr-pipeline tick --loop --only '<tokens>'` from the target root | Existing tick flag. `--only` limits launches only; harvest stays global. Listed items still need a pickup label. A loop holds `.claude/worktrees/.tick.lock` for its whole life, so a second tick or loop exits 5. The list loop ends itself when the list is settled or exhausted (`loop_only_stop`). Spends model tokens. | Yes. List the items, the engines, and `max_in_flight`. State that list order applies within each stage only (C17). |
| 4 | Start loop | Start `pr-pipeline tick --loop` | Same lock rule. Spends model tokens. Stops after 3 quiescent heartbeats (shipped). | Yes |
| 5 | Run one tick | Start `pr-pipeline tick` | Exits 5 if a loop is running. May launch engines. | Yes |
| 6 | Stop run | Deferred: needs a later command contract | #117 adds observation only. No stop button or new signal command is included in #117 or #120. Existing CLI termination behavior is not a UI authorization contract. | Unavailable until separately designed |
| 7 | Hold (stop advancing) | Add `ai:blocked` (or `ai:needs-human-decision`) | The one label move a human may make on marker-owned work (pr-guidelines, "Pipeline label authority"). Dispatch skips the item. A finished result is posted as a comment and not harvested. Process custody stays with the tick: the engine runs until it exits or reaches its deadline. This is not a pause; there is no resume to the same point. | Yes. Use the button name "Hold", not "Pause". |
| 8 | Release block | Show the exact proposed change: remove `ai:blocked` and all other non-hold workflow labels; add the one pickup label selected by the operator; preserve unrelated labels | Refuse a local marker, any in-flight label or another hold. Require explicit confirmation that other machines have released custody. Re-read labels immediately before writing and refuse a changed precondition. Verify afterward that the selected pickup is the sole workflow label; failed/inconclusive verification is not success. Issue pickup: `ai:available`; PR pickup: `ai:implemented`, `ai:revised` or `ai:changes-requested`. | Yes; explain machine-local visibility and the selected stage. Never guess it. |
| 9 | Send guidance to the fixer | Post a PR conversation comment as the operator on a PR at `ai:needs-human-decision` with no marker and no other workflow label except `ai:changes-requested` | Existing approved-input path (tick README, "Comment trust and incomplete history"). The next tick replaces the hold with `ai:changes-requested` and dispatches a fixer, or sets `ai:max-iterations` at the round cap. The comment must stay unedited and must contain no `<!-- pr-pipeline:` marker. It uses one review round. The loop does not wake for a comment. | Yes. State that it uses a round. State that a local check cannot see another machine: if another machine still holds the PR and publishes its held result, that comment is a newer pipeline action, so this guidance does not release the hold. |
| 10 | Stop engine | Needs a new tick-owned command | Today the tick terminates only an overdue engine whose identity is proven. The UI must never signal a PID itself (PID reuse, process groups). Local machine only. | Yes, when built |
| 11 | Requeue a stranded item | Manual runbook only (tick README, "Markerless in-flight items") | Needs out-of-band custody confirmation on every machine. The UI cannot prove custody. Show the runbook and copyable commands, not a one-click button. A button needs a new tick-owned command with cross-machine custody proof. | Not a button |
| 12 | Merge, close, delete branch | Owner-only | No button. Show "Open on GitHub". | — |
| 13 | Configure this repository | `/pr-pipeline:setup` is a skill that needs a model session; the tick exits 6 for an unconfigured target | Show the instruction. No button. | — |

## 6. Data gaps

| # | Gap | Consequence for the UI | Smallest change that closes it |
|---|---|---|---|
| 1 | No complete machine-readable status snapshot | Text cannot preserve classification, evidence provenance and nested read failures for dashboards. | #116 exports shared classifications, identity, marker mtime, progress and complete source health. Required before #120. |
| 2 | The reviewer has no progress heartbeat. The wrapper reads the reviewer's JSONL stream for sanitized observations but exports no output recency. | Reviewer rows cannot show output age or stall. They show elapsed time and deadline only. | #115: heartbeat updates in the existing stream reader, with no content kept. |
| 3 | The heartbeat is an mtime only. No output rate or history exists. | Per-minute output counts are impossible. The activity bar is binary and starts when the server starts. | None recommended. Use S5 as specified, or cut it. |
| 4 | No live engine narrative ("what is it doing"). | "Latest update" is the newest event plus output age, not engine text. | None in scope. Keeping model output conflicts with the sanitization rules. |
| 5 | No verified loop-process observation; one-shot ticks also record cadence | Tick evidence alone cannot establish a running loop or abnormal exit. | #117 records `pid`, `process_start_time`, `recorded_at`, host and scope under the lock, and exports the shared classifier with `record_present`. No stop command; loop stopping is deferred. |
| 6 | Review round count is not exported. `count_review_rounds` reads trusted PR comments. The tick README quick reference says the count is "iteration in label"; no label carries it. | "Round N of 5" needs GitHub comment reads and a copy of the trust logic. | Add `rounds` and `max_iterations` to `review_handoff` and `terminal` event details (an additive field is allowed in version 1). Fix the README line. |
| 7 | Confirmed requeue and held-result publication are absent from the feed | Recovery can be missing from history and attention can remain open. | #119 projects `revert_confirmed` as `requeued` and confirmed `held_result_published`, with attention recovery. Retention observations and raw decisions stay diagnostic-only. |
| 8 | Tick summaries are internal records | A consumer cannot rely on raw summary format or equate cadence with success. | #116 exports completed status, distinct tick times, cadence and separate recorded action counts. No new tick event is required. |
| 9 | History is bounded and local: 50 retained tick logs plus the newest, this machine only. | Timelines are incomplete for older tasks and for other machines' work. | None required. Show the bound. (Issue #69 tracks harvest-evidence archive retention, which is a separate store.) |
| 10 | Issue-to-PR linkage is in retained `pr_handoff` events only. | Older tasks need a GitHub read to group issue and PR. | None in the tick. The server reads `closingIssuesReferences` (read-only GraphQL). |
| 11 | Other machines' engines are invisible. | An in-flight label with no local marker shows "custody unknown". | None in scope. This is the documented design. |
| 12 | Time at the current label is not available. | The UI cannot say "at `ai:implementing` for 3 days". | The server reads labeled-event timeline items for C11 items only. |
| 13 | No safe configuration export, and standalone defaults do not prove an existing loop's applied settings | Initial Phase 1 defers the definitive author-eligibility flag and per-item provider applicability; show unknown when needed. | #123 (shipped: `tick config --json`) exports its own standalone invocation context for configured values. Definitive existing-loop claims would need a separate tick-owned applied-configuration snapshot; #123 does not make them available. |
| 14 | Review finding counts are not in events. | "2 blocking findings" cannot be shown. | Add a finding count by severity to `review_handoff` details. |

## 7. Cut list

| Mockup element | Where | Why cut | Replacement |
|---|---|---|---|
| Stall after 10 min ("Stall flagged: no output for 10m", `stallMinutes: 10`) | Main | The shipped threshold is 30 min, plus 5 min before first output. Startup refuses 600 s or less, so 10 min cannot be configured. | Show the `engine-progress` verdict and the marker's own threshold. |
| Output age, "NOW", and output bars on the Codex reviewer row | Main, InstrumentPanel | Reviewer output recency is not exported. No progress signal exists. | "Running, no progress signal" with deadline (4.3 row 11). |
| "OUTPUT PER MINUTE · LAST 30 MIN" bars with heights | InstrumentPanel | Heartbeat is an mtime only. No counts and no history exist. | Binary activity bar since server start (S5), or nothing. |
| "Revision N of 3" and "Stopped after 3 revisions · needs your decision" | Main, InstrumentPanel ("R3") | The cap is `max_iterations`, 5 shipped. At the cap the label is `ai:max-iterations`, not `ai:needs-human-decision`. The count is not exported. | "Round limit" stage. A round count only after #118 lands. |
| "Pause loop" / "Resume loop", "Loop paused" | Main | No pause mechanism exists; stopping is deferred. | No control until a separate stop-command contract exists. |
| "Pause this task" | Main | A hold does not pause an engine. The engine keeps running. | "Hold" with honest confirmation text (action 7). |
| "Stop engine" | Main | No mechanism. Needs a new tick-owned command. | Nothing until that command exists. |
| "Loop: Healthy", "LOOP OK" | Main, InstrumentPanel | The UI cannot see the loop process. | "Next tick due by HH:MM" from tick evidence (4.1). |
| "Next tick in 1:42" countdown | Main, InstrumentPanel | The interval is an upper bound; nudges start ticks sooner. | "Next tick due by HH:MM". |
| "Engines running · All producing output" | Main | Counts only this machine. A reviewer cannot claim output. `quiet-Nm` is not a health verdict. | "Running here" count, and alarms listed separately. |
| Cooldown task in the Implement stage, "Retry in 3m50s" per task | Main, InstrumentPanel (`#136 RETRY`) | After a transient failure the item returns to its pickup label (queue). The cooldown is provider-wide. | Pickup stage plus a recorded retry observation and provider-wide C14 banner; no prediction of per-item applicability. |
| Queue positions and `Next up: #155` | Main | The full-queue pick order has no priority. Only an `--only` list has an order, and only within each stage. | Show no position in full-queue mode. For a list run, show list order as the priority within each stage (C17). |
| "Picked up from the queue" timeline entry | Main | No such event exists. The first event is `launch`. | Start the timeline at `launch`. |
| "Tick finished · nothing to harvest yet" feed entries | Main | Tick summaries are not events. They are also noise. | Show the last tick in C2 only. |
| "Changes requested · 2 blocking findings" | Main | Finding counts are not in events. | "Changes requested" from `review_handoff` `details.label`. |
| Free-text task narratives ("Fixing 2 blocking findings from the first review") | Main | No engine narrative exists. | Newest event `message`. |
| Linear track Issue → Implement → Review → Fix → Ready | Main | After a fix, the PR returns to review, not to Ready. Four ends exist, not one. | The pipeline map loop and ends (C7). |
| "Waiting on you = decision + ready" as one number | Main | It merges two different responses. | Separate groups in C4. |
| Duplicate counters (TASKS, RUNNING, STALLED, WAITING) beside the station map | InstrumentPanel | Repeats the map counts. | One set: C5. |
| Issue picker that lists every unlabelled issue | Main | Issues by other authors are never dispatched. | Show them disabled with the reason (C16). |
