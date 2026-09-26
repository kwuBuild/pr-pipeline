# Independence ladder

These scripts check that pr-pipeline works on a repository that is not this
one, installed the way an adopter installs it. Checks run against this
repository cannot find a coupling to it, so every stage below runs somewhere
else: a clean clone, a plugin cache, or a throwaway GitHub repository.

What each stage must prove, and why, is defined in section 10 of the
[distribution design](../docs/working/plugin-distribution-design.md#10-validating-the-independence-claim).
This file tells you how to run the stages and how to read their results.

## Stages

Run the stages in order, cheapest first. If a stage fails, a later stage
proves nothing.

| Stage | Script | Cost | Proves |
|---|---|---|---|
| L0 static | `l0-static.sh [<ref>]` | Free, about 2 minutes | The suite passes in a clean clone. The suite includes the checks that no shipped file names a developer home or this repository, and that setup plus a tick open no file in the target tree except pipeline state |
| L1 install smoke | `l1-install-smoke.sh <source>` | One short model call | A GitHub-marketplace install has exactly the published skills. The dashboard runs in an unrelated repository. A fresh session substitutes `${CLAUDE_PLUGIN_ROOT}` with the versioned install path |
| L2 stubbed tick | `l2-stubbed-tick.sh <target> <root>` | Free | With canned engines on a real repository, one issue goes through implement, a rejecting review, fix, and re-review. An escalation archives its evidence. Nothing is read from the target tree |
| L3 real engines | `l3-real-engines.sh <a\|b> <target> <root>` | Paid | Real engines complete a reject-then-fix cycle on two differently configured targets, one of them off `main`. The fixer reads the hygiene skill doc from the install |
| L4 upgrade | `l4-upgrade.sh <source>` | One short model call | After a version bump and `claude plugin update`, a fresh session runs the new code, and the operator config is unchanged |

The arguments:

- `<ref>`: the commit to test. The default is `HEAD`.
- `<source>`: a throwaway GitHub repository, as `owner/name`, whose default
  branch holds the candidate plugin. L4 pushes a version bump to it.
- `<target>`: a throwaway GitHub repository, as `owner/name`. L2 and L3 create
  it, private, if it does not exist.
- `<root>`: the pipeline to run. Pass the install path that L1 prints, so the
  installed code runs and not a checkout.
- `<a|b>`: the L3 fixture in `fixtures/l3-a` or `fixtures/l3-b`.

L3-adversarial is not a phase 1 stage. [l3-adversarial.md](l3-adversarial.md)
records its fixtures as known gaps.

## Normal sequence

Every script needs `LADDER_WORK`, an absolute scratch directory. All state and
evidence go there. The operator config for L2 and L3 is isolated there through
`PR_PIPELINE_CONFIG`, so the real `~/.claude/pr-pipeline/config.toml` is not
touched.

1. Build the candidate: the commit you intend to release. Push it to the
   default branch of the `<source>` repository.
2. `validation/l0-static.sh <candidate>`. Set `PYTHON` to an interpreter at
   3.11 or later that has pytest. Run with host process visibility.
3. `validation/l1-install-smoke.sh <source>`. It installs at local scope in
   `$LADDER_WORK/unrelated`, so only sessions there see the plugin, and it
   prints the install path.
4. `validation/l2-stubbed-tick.sh <l2-target> <install-path>`.
5. Run L3 once per fixture, on two different targets. The runs can overlap:
   setup is serialised because both runs share one operator config.

   ```bash
   validation/l3-real-engines.sh a <target-a> <install-path>
   validation/l3-real-engines.sh b <target-b> <install-path>
   ```

   If the first review approves, no fixer ran and the stage fails. Run again
   with another issue from the same fixture, for example
   `LADDER_L3_ISSUE=issue-2.md`. A thorough implementer model often passes
   its first review. `LADDER_ENGINE_MODEL=sonnet` selects a lighter route for
   both setup's probe and the tick. A route that setup's probe fails under
   `auto` can still run a `dontAsk` target: on the first ladder run, `haiku`
   failed the probe and then could not work within `dontAsk` grants.
   Do not edit a ladder script while a run executes it: bash reads the file
   as it runs.
6. Stop every tick loop that runs from the install. Then run
   `PR_PIPELINE_CONFIG=$LADDER_WORK/l3-operator/config.toml validation/l4-upgrade.sh <source>`,
   so the config that L3 wrote is the one checked.
7. Delete the throwaway repositories. Uninstall with
   `claude plugin uninstall pr-pipeline@pr-pipeline --scope local` and
   `claude plugin marketplace remove pr-pipeline --scope local`, run in
   `$LADDER_WORK/unrelated`.

## Reading results

Each check prints one `PASS` line or one `FAIL` line with the reason. The last
line is `RESULT`. The exit status is 0 when every check passed, 1 when a check
failed, and 2 when the stage could not run. L2 and L3 print the directory that
holds the evidence for the run: stub or observer logs, setup output, the audit
of opened files, and for L3 the engine streams and their cost.

The evidence gates reject missing or invalid records:

- L1 and L4 require a zero CLI exit, a completed successful session, and a
  matching successful Bash result with dashboard output. A requested command
  or a manifest on disk alone does not prove execution.
- L2 and L3 require a parsed audit log that includes an open of the installed
  `skills/tick/lib/tick.py` from the target checkout. An empty log or a log of
  unrelated file opens does not prove that the controller was audited.
- L3 saves each observer's argument array, working directory, and dispatch
  marker in `*.launch.json`, plus the engine exit status in `*.exit`. The
  hygiene read must have a successful result in the matching fixer's stream.
  Its PR, worktree, and dispatch ID must agree with the controller launch log.
  Tool requests from other streams cannot supply that result.
- L3 compares each launch's complete grant set with the selected fixture's
  verification grant, the common grants defined in `evidence.py`, and the exact
  stage-specific push grant. The throwaway checkout uses the `origin` remote
  created by `gh repo clone`. Additional grants fail the check.

Older L3 observer logs lack the dispatch snapshots and exit records. Keep them
as historical run evidence; rerun L3 to obtain evidence for these checks.
The local regression suite runs the complete L1 through L4 shell entry points
with command doubles (L0 runs the suite itself):

```bash
.venv/bin/python -m pytest skills/tick/lib/test_validation_ladder.py -q
```

These tests prove the evidence gates. They do not establish that a new
candidate passed the marketplace, GitHub, or paid-engine stages.

## Host conditions that change results

| Condition | Effect | What to do |
|---|---|---|
| A sandbox that blocks `ps` | Process start-time tests in `test_status.py` and `test_tick.py` fail | Run L0 with host process visibility |
| jq 1.6 first on `PATH` | Before #62, the dashboard's queue sections failed to compile | Test a candidate that includes #62 |
| A clone of a local path | `test_gh_snapshots.py` cannot resolve a repository, because it runs `gh` in the checkout | L0 resets the clone's `origin` to the source's GitHub URL |
| A directory marketplace (`marketplace add ./path`) | Claude Code runs the plugin from the source directory, so `${CLAUDE_PLUGIN_ROOT}` is not the cache entry and L4 cannot observe an update | Use a GitHub source |
| Claude user settings with `sandbox.autoAllowBashIfSandboxed` | Headless engines inherit the sandbox, Bash is auto-allowed, and setup's probe reports `unmeasurable` | Set `LADDER_ENGINE_SETTINGS='{"sandbox":{"enabled":false}}'` for L3 |
| A terminal shim that resolves `claude` again on `PATH` | Without a guard, the L3 observer and the shim call each other without end | The observer removes its own directory from `PATH` and refuses to re-enter |

## Not covered

- The L3 issue filed from a second account (design section 4). File one by
  hand on a target, then confirm that the tick does not pick it up.
- Loop, wake, and custody behavior under process faults. That belongs to the
  offline harness in #34, not to L2.
