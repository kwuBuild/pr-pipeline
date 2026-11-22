# Changelog

This file records each pr-pipeline release. The version in
`.claude-plugin/plugin.json` is the release key: installed users receive a
change only when that version changes. The
[release process](docs/working/release-process.md) explains how to cut a
release.

Each entry lists the changes users notice and any step that users must do
after they update. Versions follow [Semantic Versioning](https://semver.org/).
Before 1.0.0, a minor version can contain breaking changes.

## [Unreleased]

### Changed

- Reviewers now publish the same advisory output-recency heartbeat as
  implementers and fixers. Status and stall notifications use the resolved
  stall threshold and startup grace. Heartbeat write failures do not change
  review completion, and no raw event content is stored.
- The shipped `review_timeout_seconds` increases from 1200 to 3600 seconds.
  Explicit operator deadlines remain unchanged.

### Migration

- Startup now requires `engine_stall_seconds` to be less than each of
  `implementer_timeout_seconds`, `fixer_timeout_seconds`, and
  `review_timeout_seconds`. Existing configurations with a review deadline at
  or below the stall threshold now fail startup with both keys and values in
  the error. For example, `engine_stall_seconds = 1800` with an explicit
  `review_timeout_seconds = 1200` is incompatible. Raise the configured review
  deadline or lower the stall threshold (which must remain greater than 600).
  Startup does not change either explicit value or disable reviewer stalls.

## [0.3.0] - 2026-11-15

Effort control, milestone notifications, and trusted reviewer input. Your
0.2.0 configuration keeps working unchanged.

### Added

- Configuration keys `effort` and `fix_effort`, and the flags `--effort` and
  `--fix-effort`, set `CLAUDE_CODE_EFFORT_LEVEL` for the implementer and the
  fixer: `low`, `medium`, `high`, `xhigh`, or `max`. A blank `fix_effort`
  inherits `effort`. A blank `effort` leaves plain `claude` unchanged and keeps
  `claude-ds` at `max`. The launch log records the effort that the tick set.
- Configuration key `approved_reviewers` lists GitHub logins, beside the
  operator, whose PR comments and submitted change-request or comment reviews
  direct the fixer. New approved input can resume an item from
  `ai:needs-human-decision`, within `max_iterations`. Edited, forged, or
  marker-carrying input is refused.
- `pr-pipeline events --milestones` prints only launch, terminal, attention,
  stall, and stop milestones. `--mark` and `--cursor PATH` let a reader resume
  without losing or skipping milestones. During `/pr-pipeline:tick --loop`,
  the tick skill uses them to send one notification for each milestone.

### Changed

- An `--only` loop stops on the first quiet tick where every listed item, and
  the pull request that closes a listed issue, is settled.
- Fixer rounds from approved input, silent attempts, and published results
  share one count against `max_iterations`.

### Fixed

- A terminal label edit that the tick could not confirm now appears as an
  attention milestone (`terminal label not confirmed`). It does not appear as
  a success or as a failed label.

### Upgrade steps

- Restart Claude Code and any running `tick --loop` after the update. A
  running process keeps the 0.2.0 code.
- None of the new configuration keys are required. To use them, add
  `effort`, `fix_effort`, or `approved_reviewers` to `[defaults]` or to a
  repository section of your operator configuration.

## [0.2.0] - 2026-11-08

Tuning, visibility, and richer agent reports. Your 0.1.0 configuration keeps
working unchanged.

### Added

- `tick --only '#N,pr:M'` starts new work only for the listed items, in list
  order. Harvest still collects finished work everywhere, and a list loop
  stops when its items have no work left. `/pr-pipeline:status` shows the
  active dispatch scope.
- `pr-pipeline events` shows a local feed of pipeline handoffs, with follow
  mode, a target filter, and JSON lines output.
- Operator configuration keys for the admission cap (`max_in_flight`), agent
  deadlines and stall limits, the review limit (`max_iterations`), engine and
  model routes for each stage, and loop cadence. Set them in `[defaults]` or
  for each repository. The defaults keep the 0.1.0 behavior.
- `--fix-engine` and `--fix-model` give the fixer its own route.
  `/pr-pipeline:setup` records the implementation route it tested, and the
  tick warns at startup when a configuration or flag selects a different one.
- Reviewer and fixer runs receive the full, current pull request and linked
  issue discussion.
- Codex reviewers publish detailed reports with reasoning, verification, and
  risks. Fixers publish a complete report for each finding.
- Pipeline stages load installed skills natively. The isolated Codex reviewer
  receives the plugin's review skills as optional guidance.
- Implementers must follow `definition-of-done` and record evidence before
  they report completion. A pull request without that evidence gets a visible
  notice.

### Changed

- The pipeline accepts its own earlier comments only when the operator wrote
  them and nobody edited them. An edited or forged comment cannot guide an
  agent or change the review count.
- The tick log records whole-session token use, turns, and duration for
  Claude Code sessions that resume after a background task.

### Fixed

- `/pr-pipeline:setup` read manifests longer than 45 bytes as invalid,
  because it did not decode GitHub's line-wrapped base64.
- A targeted run now stops when its item reaches a final state, without
  waiting for unrelated work.
- Harvest keeps the report that an agent wrote for its pull request, including
  its layout and any human edits.
- A fixer resolves linked issues in the correct repository.
- An unreadable installed skill document holds dispatch for the affected stage
  for one tick and reports the paths. It no longer moves each eligible item to
  `ai:needs-human-decision`.

### Upgrade steps

- Restart Claude Code and any running `tick --loop` after the update. A
  running process keeps the 0.1.0 code.
- If you use the terminal launcher, rerun `skills/tick/install-launcher.sh`
  from the new install. This adds the `events` command. It does not change
  your configuration.
- Optional: rerun `/pr-pipeline:setup` for each repository that you set up
  with 0.1.0. This records the tested implementation route, so the tick can
  warn when you select a different route.

## [0.1.0] - 2026-10-03

First release as an installable Claude Code plugin.

### Added

- Plugin and marketplace manifests. Install with
  `/plugin marketplace add kwuBuild/pr-pipeline` and
  `/plugin install pr-pipeline`.
- `/pr-pipeline:setup` confirms the operator login, provisions the `ai:` labels,
  proposes verification grants, probes the engine permission route, and writes
  `~/.claude/pr-pipeline/config.toml`.
- `/pr-pipeline:tick` runs HARVEST and DISPATCH: Claude implementers and fixers,
  Codex reviewers, targeted ticks, and an in-process `--loop`.
- `/pr-pipeline:status` shows a read-only dashboard, including parked items.
- A standalone launcher at `~/.claude/pr-pipeline/bin/pr-pipeline`. It finds the
  installed plugin on each call, so it keeps working after an update.
- Operator configuration outside the install. Updates do not change it.
- Optional `engine_settings` passed to Claude engines as `--settings`.
- Issue pickup and review admission restricted to the configured operator.
- Developer skills: `definition-of-done`, `review-response-hygiene`,
  `automation-control-plane-review`, `durability-boundary-review`, and
  `loop-cadence-contract-review`.

### Upgrade steps

- None. This is the first release. Run `/pr-pipeline:setup` once for each
  target repository.
