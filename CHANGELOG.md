# Changelog

This file records each pr-pipeline release. The version in
`.claude-plugin/plugin.json` is the release key: installed users receive a
change only when that version changes. The
[release process](docs/working/release-process.md) explains how to cut a
release.

Each entry lists the changes users notice and any step that users must do
after they update. Versions follow [Semantic Versioning](https://semver.org/).
Before 1.0.0, a minor version can contain breaking changes.

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
