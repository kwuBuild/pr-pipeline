# Mission Control Design

Mission Control is a planned local web page that shows what pr-pipeline is
doing: the stage of every task, the alarms, the items that wait for the
operator, and the engines that run on this machine. This document owns its
visual design language, its page layout and the meaning of its pipeline map.

Status: draft 5, for review in #121. Implementation is tracked in #120.

Related owners:

- [The component inventory](mission-control-components.md) owns the
  components, their states, the status vocabulary rules, the Phase 2 actions
  and the data gaps. This document links to it for every status rule and does
  not restate one.
- [The static mockup](mission-control-mockup.html) shows the design with sample
  data. Open it in a browser. It needs no build and no network.
- [`skills/status/SKILL.md`](../../skills/status/SKILL.md) and
  [`skills/status/EVENTS.md`](../../skills/status/EVENTS.md) own the state
  sources that the page reads.

## Scope

Phase 1 is a read-only monitor. It spends no model tokens and makes no writes.
Phase 2 adds actions later, after their command contract is defined
([component inventory, section 5](mission-control-components.md#5-phase-2-actions)).

The page must be faithful to the pipeline: it shows only data that pr-pipeline
records today, in the pipeline's own classifications. The component inventory
owns the rules that make this true: unknown and stale data
([section 4.7](mission-control-components.md#47-unknown-and-stale-data)) and
the signals that do not exist yet
([section 6](mission-control-components.md#6-data-gaps)).

## Principles

1. **Truth before polish.** Show only what the pipeline records. Missing or
   old data says "Unknown".
2. **Calm until it matters.** Normal states use graphite and white. Signal
   orange marks alarms only, when the machinery needs a look. Items that wait
   for the operator use white markers.
3. **Content first, depth for hierarchy.** The map and the lists sit on solid
   surfaces. Frosted glass is used only for floating controls. Each layer up
   adds light, not heavier borders.
4. **Motion reports real change.** A breathing light follows the heartbeat-backed
   state in [component section 4.3](mission-control-components.md#43-engine-condition-this-machine-only).
   A token glides only after a confirmed handoff event. Every decorative
   animation loop can be paused.

These principles borrow the approach of Apple's Human Interface Guidelines
(content first, depth for hierarchy, purposeful motion). The page does not copy
Apple's interface components or materials.

## Page layout

The regions appear in this order, top to bottom:

| Order | Region | Why it is here |
|---|---|---|
| 1 | Toolbar | It names the repository, states "Engines: this machine only", shows the last tick outcome separately from scheduling and process evidence, the data freshness and the Pause motion control. Every reading below depends on these facts. |
| 2 | Large title and summary | One sentence of counts: tasks, alarms, items waiting for the operator, engines on this machine. |
| 3 | Alarm strip | Shown when an alarm exists, or when a failed source prevents an alarm check ("Cannot check"). With no alarm and every source current, it is hidden; it never shows "No alarms" text. A missed alarm costs the most. |
| 4 | Pipeline map | Every labeled task, counted at its station, with a bounded preview of task tokens. Selecting a station opens that station's task list below the map. The owner asked for the map near the top. |
| 5 | Running on this machine, and Your turn | Side by side. Local engines with their progress verdict and deadline. Items that wait for the operator, grouped by reason. |
| 6 | Recent events | The newest events from the local event log. |
| 7 | Source health | The freshness of each source and the start of the retained history. |

Because the alarm strip sits above the map, triage comes first whenever an
alarm exists. The component inventory maps each component to these regions
([section 2](mission-control-components.md#2-information-architecture)).

## Pipeline map

The map has one station for each stage in the
[component inventory, section 4.2](mission-control-components.md#42-stage-github-labels),
which owns the label for each stage. Each station shows its exact GitHub label
names, the exact count of its tasks, and a bounded preview of their tokens
([Crowded stations](#crowded-stations)). Unsupported-label, conflicting-label
and parked items are not stations; section 4.2 places them.

The stations sit on a grid of five columns and two rows:

| Row | Column 1 | Column 2 | Column 3 | Column 4 | Column 5 |
|---|---|---|---|---|---|
| 1 | Queued | Implementing | Awaiting review | Reviewing | Ready for you |
| 2 | Needs decision | Blocked | Fixing | Waiting for fixer | Round limit |

The map draws these transitions as arrows:

1. Queued to Implementing, when an engine is dispatched.
2. Implementing to Awaiting review, when the PR is opened.
3. Awaiting review to Reviewing, when a reviewer is dispatched.
4. Reviewing to Ready for you, when the review approves.
5. Reviewing to Waiting for fixer, when the review requests changes.
6. Waiting for fixer to Fixing, when a fixer is dispatched.
7. Fixing to Awaiting review, when the fix is pushed and the PR is labeled
   `ai:revised`.

The map does not draw an arrow into Needs decision, Blocked or Round limit:

- A human hold can apply at any stage, so the two hold stations sit apart with
  the label "Human holds · can apply at any stage".
- More than one path sets the round limit, so its station carries the caption
  "No further automatic fix round" instead of an arrow. Section 4.2 owns when
  the label is set.

A task token shows the issue number (`#131`) or the PR number (`PR 151`), its
state marker and a short fact such as the quiet time. A token carries no title,
so that a station stays compact. Lists, alarms and detail headings show the
title beside the ID. The component inventory owns the token states, the title
rule and the issue-to-PR grouping
([S2](mission-control-components.md#3a-shared-parts),
[section 4.2](mission-control-components.md#42-stage-github-labels)).

### Crowded stations

Every station has the same fixed size, so the map and its arrows never move
when a count changes. A station holds as many tokens as fit, and never more:

1. A station shows at most three token rows, one token per row.
2. When a station holds more tasks than its rows show, the last row is a
   "+N more" control instead of a token. The count in the station heading is
   always the exact total.
3. Tokens follow the Task list's default order
   ([C8](mission-control-components.md#3b-monitor-components)), which ranks
   the S1 classes, so alarm tokens come first.
4. When a station holds more alarm tasks than it shows, the control names them:
   "+5 more · 2 alarms". If that text is wider than the station, use the short
   form "+125 · 2 alarms". The accessible name always has the full text, such
   as "Show all 127 queued tasks, 2 alarms not shown". The alarm strip lists
   every alarm, so a preview never hides one.
5. Activating "+N more", or the station itself, selects the station and opens
   its task list below the map. The list shows every task with its title.

A station is 168 by 204 px, so its content box is 142 by 182 px. The largest
case uses all 182 px: the heading, two lines of label names, three 28 px token
rows and one caption line. The widest two-digit control, "+99 more · 99
alarms", uses all 142 px. Neither dimension has spare space, so the station
keeps its content inside the box in this order when text gets larger (a
fallback font, a user minimum font size or a third label line):

1. The heading and its exact count are never clipped.
2. The caption clips to one line with an ellipsis. If no line remains, it is
   hidden. Its text stays in the station's accessible name and in the task
   list.
3. Label names clip to two lines with an ellipsis. The full label names stay
   in the accessible name.
4. The preview shows fewer token rows. The last visible row stays the "+N
   more" control while any task is hidden, and N counts every hidden task.

Station content never overflows the box. On a narrow screen the map keeps this
size and scrolls horizontally. The mockup shows a crowded Queued station,
selected, with its task list open, and the largest case in Awaiting review.

## Design language

### Layers

| Layer | Value | Use |
|---|---|---|
| L0 backdrop | `#0A0B0D`, with one soft radial light pool | The page. Never patterned. |
| L1 content surface | `#131417`, radius 24, a 1px top highlight | The map and the lists. |
| L2 module | White at 3.5% over the surface, a white line at 8%, radius 16 | Stations and readouts. |
| L3 floating glass | Graphite at 55% with a 24px blur, and an opaque fallback | The toolbar, sheets and notices only. |

### Color

| Role | Value | Contrast on the surface |
|---|---|---|
| Primary text, live light, your-turn marker | `#F2F2F0` | about 16:1 |
| Secondary text | `#A3A3A8` | 7.3:1 |
| Decoration only, never text | `#6E6E74` | not used for text |
| Signal, alarms only | `#FF7A1A` | about 7:1 |

Every state has a shape and a word, so color is never the only cue.

### Type

The page uses the system font: San Francisco on Apple devices and Segoe UI on
Windows. Numbers, task IDs and exact label names use the system monospace. The
page loads no web fonts.

| Style | Size and line height | Weight |
|---|---|---|
| Large title | 34/40 | bold, −2% tracking |
| Title | 22/28 | semibold |
| Headline | 15/20 | semibold |
| Body | 14/20 | regular |
| Caption | 12/16 | medium |
| Label | 11/14, capitals, +8% tracking | semibold |

### Shape and spacing

Corner radii are 24 for surfaces, 16 for modules, 10 for tokens, and a pill for
controls. A nested element near an edge takes the outer radius minus its inset,
so curves stay parallel. Spacing steps are 4, 8, 12, 16, 24 and 32.

### Status grammar

Each state has one marker shape and one word. This table owns only the visual
treatment. The [component inventory, section 4](mission-control-components.md#4-status-vocabulary)
owns the states, their rules and their words, and
[section 4.6](mission-control-components.md#46-what-counts-as-an-alarm) owns
which states are alarms.

| State | Marker |
|---|---|
| Last output N min ago | breathing white dot |
| No output observed since launch | still white dot |
| Running, no progress signal | still, dim dot |
| Stalled, Overdue | orange triangle and the quiet time or the deadline |
| Completed result awaiting reconciliation | white ring |
| Engine failed, Exited without accepted completion | grey ring |
| Custody unknown | dotted ring |
| Waiting (a pickup label) | grey ring |
| Recorded provider cooldown observation | dashed ring and recorded end time |
| Your turn | white square and the reason word |
| Attention | orange square |
| Unknown or stale | dotted ring with "?" and the word "Unknown" |

The cooldown marker describes recorded evidence or the provider-wide banner.
It does not mark a queued item as currently blocked.

When a state is an alarm, its marker uses signal orange and the task also
appears in the alarm strip. This includes an Attention flag on an item whose
stage is a your-turn state: the map token takes the orange Attention marker,
not the white your-turn marker, and its station takes the alarm border.

### Motion

| Motion | Duration | Rule |
|---|---|---|
| Feedback | 160 ms | Hover and press. Ease out. |
| Entrance | 600 ms | Panels rise in once on load, 60 ms apart. |
| Handoff | 280 ms | A token glides to its new station once, only after a confirmed handoff event. |
| Live signal | 2.4 s loop | Breathing only on the "Last output N min ago" state ([section 4.3](mission-control-components.md#43-engine-condition-this-machine-only), row 9). |
| Pause | toolbar control | "Pause motion" stops decorative animation loops. While paused, it reads "Resume motion" and shows a pressed state. Reduced Motion also suppresses decoration. Neither control stops data refresh, a pipeline loop or an engine. |

The page has no blinking and no travelling lights. The pipeline does not stream
work along the map's edges, so the map must not suggest that it does.

### Accessibility

- Text contrast is at least 4.5:1. Secondary text is 7.3:1 on the surface.
- Every marker has a visible word. Markers are `aria-hidden`; the word carries
  the meaning.
- Controls are real buttons with a visible focus ring and a 36px or larger hit
  area. One exception applies: map tokens and the "+N more" control are 28px
  high, so that three rows fit in a fixed station. They are separated by 6px
  gaps and do not overlap, so they meet the 24px minimum of WCAG 2.2 success
  criterion 2.5.8. The same task opens from its station's task list, whose
  rows meet the 36px rule.
- Updates are announced once, when a state changes, never on every poll.
- The map keeps its size and scrolls horizontally on a narrow screen. The lists
  stack.
- The static mockup shows the visual design only. Its markup is not the
  semantic reference: for example, its stations are not buttons, its running
  list is not a table with row headers, and its times are not `<time>`
  elements. The implementation follows the accessibility column of the
  [component inventory](mission-control-components.md#3-component-inventory).

### Avoid

- Data the pipeline does not record, such as output history, loop health or a
  queue position.
- Words that overclaim: "healthy", "working", "next in 1:42".
- Color on a normal state, or orange on anything but an alarm.
- Glass on content, such as the map or the lists.
- Looping motion without a pause control.
- A state shown by color alone.

## Decisions and deferred work

- **Review deadline and stall limit (planned in #115, still open).** #115 will
  add the reviewer heartbeat and a 3600-second default review deadline. Until
  it lands, the review deadline stays at 1200 seconds and the reviewer has no
  progress signal. After #115, startup rejects a review deadline at or below
  the stall limit; existing explicit settings are not silently changed.
- **Light theme.** Not designed yet.
- **Phase 2 actions.** Command contracts come first. Loop stopping is deferred;
  #117 provides read-only process observation and #120 provides no actions.
