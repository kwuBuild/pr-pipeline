# Codex write-stage validation

This record captures the native controller trial for issue #127 on
2026-11-22. The operational contract is in
[Optional Codex write stages](../../skills/tick/README.md#optional-codex-write-stages).
These observations establish the tested route on this host. Each target still
needs its own permission probe and disposable lifecycle trial.

## Route and topology

| Item | Measured value |
|---|---|
| Host | macOS, Apple silicon |
| Codex CLI | `0.171.0` |
| Implementation and fixing | `codex`, `gpt-6-astra`, effort `high` |
| Review | Separate read-only Codex sessions, `gpt-6-astra`, effort `high` |
| Writer policy | `--approve-for-me`, which selects workspace-write and automatic approval review |
| Writer command | `codex exec --json --color never --approve-for-me --model gpt-6-astra -c 'model_reasoning_effort="high"' -` |
| Target | Disposable repository `kwuBuild/prp-ladder-b`, base `develop` |
| Target verification | `sh ./check.sh`, exactly as its `AGENTS.md` prescribes |
| Verification grants | Empty `verify_tools`; explicit `VERIFICATION_UNCONFIGURED_WAIVER` retained in controller reports |
| Authentication | Inherited operator authentication; actual GitHub push succeeded |

The controller created a branch worktree for implementation and a detached
worktree for fixing. It supplied stdin prompts, canonical installed skills,
the verification contract, and each stage's push destination. All PR creation,
report publication, body replacement, and workflow labels came from the tick.
The trial used the checkout's `skills/tick/tick.sh` from the target repository,
with a separate operator config selected by `PR_PIPELINE_CONFIG`.

## Local setup probes

`setup.sh probe kwuBuild/prp-ladder-b --impl-engine codex --model gpt-6-astra
--effort high` ran the same writer argv in two disposable linked worktrees.
Both ran `sh probe.sh`, verified a unique file, committed it, and pushed to a
local bare remote. The implementation probe pushed
`f649124a27f58a6c324fbe6351a7871a69e1957d`; the detached fixer probe pushed
`98152a11264e571083ced79a80b912856865774f`. Both exited zero and recorded `pass`.
The probe explicitly recorded `authenticated_push_proven: false`. The native
controller trial below supplied the missing network and lifecycle evidence.

## Native lifecycle

1. The controller dispatched [disposable issue #8](https://github.com/kwuBuild/prp-ladder-b/issues/8).
   Codex implemented `strip_prefix`, ran all six target tests, committed and
   pushed `955eb2420194e75366a64456c7953f836181e2c9`. Its completed turn and
   zero exit accompanied `IMPLEMENTATION_COMPLETE`. Harvest created
   [PR #9](https://github.com/kwuBuild/prp-ladder-b/pull/9) from `.pr-body.md`.
2. To exercise a substantive revision, the operator added an explicit
   acceptance extension to the disposable issue: keyword-only `required=False`
   with boolean validation and mismatch errors. The separate reviewer
   [rejected the initial implementation](https://github.com/kwuBuild/prp-ladder-b/pull/9#issuecomment-7086756013)
   with finding F1 against the updated contract.
3. After correcting the skill wording described below, the native fixer read
   the installed revision skills and their required references, reproduced
   failures before changing behavior, and passed all 11 target tests. From its
   detached worktree it pushed
   `396636133a35c6fd47c56805df4f1466211dc3f7` with
   `git push origin HEAD:refs/heads/agent/issue-8`.
4. Harvest validated the version-2 report, including dispatch/head/review
   bindings and native exit metadata. It [published the revision](https://github.com/kwuBuild/prp-ladder-b/pull/9#issuecomment-7086852781),
   replaced the PR body, and requested re-review. A separate reviewer
   [confirmed F1 resolved and reran all 11 tests](https://github.com/kwuBuild/prp-ladder-b/pull/9#issuecomment-7086865629).
   The controller reached `ai:ready-for-human` and the targeted loop stopped
   with zero engines in flight. No merge or close was performed.

The fixture's final review validates its library change, not the adapter
implementation. The adapter PR still requires independent review. The same
model family wrote and reviewed the fixture; this trial proves session
separation, not model diversity.

## Failures found during the trial

- CLI 0.171.0 rejected `--approve-for-me` combined with `--sandbox
  workspace-write` with exit 2. The adapter and probe now use only the preset;
  an argv regression test prevents the conflicting pair.
- A sandbox filesystem refusal blocked protected Git metadata until the agent
  requested the configured automatic reviewer's approval. The probe and writer
  prompts now identify that normal approval path and require a stop after an
  actual reviewer denial. No automatic approval rejection was overridden.
- A host shell wrapper selected an incompatible Node architecture in the
  target checkout. The first dispatch exited 1 and was archived and escalated.
  Selecting the installed arm64 native binary on PATH resolved this host issue;
  no global installation, credentials, or trust settings were changed.
- The initial fixer prompt retained Claude's requirement for a separate native
  invocation tool. Codex [reported unavailable capability](https://github.com/kwuBuild/prp-ladder-b/pull/9#issuecomment-7086772113)
  without editing or pushing. The controller preserved the report and held the
  PR. A regression test preceded the engine-specific wording fix, and the
  resumed native run completed the revision above.

The repository tests use local engine/GitHub doubles for malformed output,
quota errors, publication failures, process interruption and restart recovery.
They establish deterministic failure behavior; they do not prove a provider's
future availability or another operator's permissions.
