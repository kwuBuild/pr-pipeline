---
name: setup
description: Point pr-pipeline at a GitHub repository - confirm the operator login, provision labels, propose verification grants, probe whether the engine route supports auto permission mode, and write the operator config. Run once after install and again for each new repository.
---

# PR Pipeline Setup

Setup turns an installed plugin into a pipeline pointed at named repositories.
It is the only writer of `~/.claude/pr-pipeline/config.toml`
(`$CLAUDE_CONFIG_DIR/pr-pipeline/config.toml` when that is set). It reads each
target through the GitHub API and never writes into a target repository.

Run every command below as written; `${CLAUDE_PLUGIN_ROOT}` is already
resolved. From a development checkout, substitute the checkout path. Each
subcommand prints one JSON object on stdout.

```bash
bash ${CLAUDE_PLUGIN_ROOT}/skills/setup/setup.sh <subcommand> [arguments]
```

| Exit | Meaning |
|---|---|
| 0 | Done |
| 1 | Setup refused a write, or the route probe did not pass; stderr or `detail` says why |
| 3 | An authoritative read failed or was malformed, or an interpreter is missing |

**The operator decides.** Show each proposal and result, then ask. Never confirm
a login, accept grants, choose a probe fallback, or install deny rules on the
operator's behalf. A refusal from `write-config` is the contract: relay it, do
not work around it.

## Procedure

1. **Host prerequisites.** Run `setup.sh prereqs`, adding
   `--impl-engine claude-ds` if the tick will run that engine. Report every
   missing tool: `gh`, `git`, `codex` (the reviewer), `jq` (the dashboard and
   launcher), `claude` (implementers and the probe). Report `route_problem` if
   present. Continue once the operator installs them or accepts the gap. The
   reviewer's Codex version check stays with `/pr-pipeline:tick` step 1.
2. **Login.** Run `setup.sh login`, show the login, and ask the operator to
   confirm it. Issue pickup and review admission trust this identity only. If
   they name another login, use theirs.
3. **Repositories and route.** Ask which repositories to drive, as
   `owner/name`. Ask which engine route the tick will run: `--impl-engine`
   (`claude` or `claude-ds`) and `--model` (blank for the operator's default).
   The probe measures that route. Changing it later means rerunning setup.
4. **Each repository**, in order:
   1. `setup.sh inspect <owner/name>`. Use the returned `repo` spelling from
      here on. Report `default_branch`: a blank `base_ref` resolves it at each
      dispatch, so pin it (`--base-ref`) only if the operator wants a
      different branch. Report `config_errors`, if any, before continuing.
      If inspection exits 3, stop this repository's setup and resolve the
      failed GitHub read or malformed manifest before proposing grants.
   2. `setup.sh labels <owner/name>`. This creates only the missing `ai:`
      labels. Report `unknown_ai_labels` with its `note`. Never delete, rename,
      or edit a label.
   3. Propose verification grants. Start from `current.verify_tools` for a
      configured repository, else `proposed_verify_tools`. Read each file in
      `guidance_files` with
      `gh api repos/<owner/name>/contents/<file> -H "Accept: application/vnd.github.raw"`
      and align the proposal with the verification those files prescribe. Each
      grant is a permission body placed inside `Bash(...)`; a trailing ` *`
      allows arguments. A grant permits a command. It is not evidence that
      verification ran. The operator confirms or edits the list, which may be
      empty.
   4. Probe the route with exactly the confirmed grants. Run it with Bash
      `dangerouslyDisableSandbox: true`, like the tick: engines launched by the
      tick run outside the session sandbox, so a sandboxed probe would measure a
      different environment. Tell the operator it makes two short model calls.
      ```bash
      bash ${CLAUDE_PLUGIN_ROOT}/skills/setup/setup.sh probe <owner/name> [--impl-engine claude-ds] [--model <model>] [--verify-tool '<grant>' ...]
      ```
      The probe asks a headless engine, launched as dispatch launches one, to
      run `git init` in a throwaway directory. That command is in no grant.
      The first run uses `dontAsk`, a negative control that must be denied.
      The second uses `auto`, which must run the command and return its
      output. See [the probe contract](../tick/README.md#setup-route-probe).
   5. Act on `outcome`:
      - `pass`: record `--permission-mode auto`. An empty grant list is
        acceptable; the tick then marks its PRs "Verification unconfigured".
      - `auto_failed`, `unmeasurable`, or `inconclusive`: the route did not
        pass. Show `detail` and let the operator choose one of two outcomes:
        1. Change the engine route (`--impl-engine`/`--model`) and probe
           again.
        2. Record `--permission-mode dontAsk` with named verification grants.
           If the list is empty, the operator must name the target's
           verification commands, then probe again with them.

        `unmeasurable` means the command also ran under `dontAsk`: the host
        does not enforce the allowlist (for example a Claude sandbox with
        `autoAllowBashIfSandboxed`, or a settings allow rule). `inconclusive`
        means nothing was measured; fix the cause in `detail` first.
   6. Preview, then write:
      ```bash
      bash ${CLAUDE_PLUGIN_ROOT}/skills/setup/setup.sh write-config <owner/name> --login <login> --permission-mode <auto|dontAsk> [--verify-tool '<grant>' ...] [--base-ref <branch>] --dry-run
      ```
      Show `diff`. The rewrite keeps every value but normalises formatting
      and drops comments. The previous file is kept as `config.toml.bak`. On
      the operator's approval, rerun without `--dry-run`. `changed: false`
      means the file already says this. `write-config` refuses a combination
      the probe did not support.
5. **Deny rules.** Run `setup.sh deny-rules` and show the rules. Under `auto`
   the engine allowlist is not a ceiling, so force-push, remote-branch
   deletion, forced refspecs, and mirror/prune pushes can reach the
   classifier. The rules go into the operator's Claude user settings, so they
   also bind the operator's own sessions, including their own
   `--force-with-lease` pushes. Deletion written as `git push <remote> :branch`
   is not covered: its rule would also deny the stages' own pushes. Install
   the rules only on an explicit yes: `setup.sh deny-rules --install`.
6. **Launcher.** Run `bash ${CLAUDE_PLUGIN_ROOT}/skills/tick/install-launcher.sh`.
   It installs `~/.claude/pr-pipeline/bin/pr-pipeline` for shell use.
7. **Report** for each repository: login, default branch, labels created and
   unknown `ai:` labels, grants, route and probe outcome, recorded
   `permission_mode`. Also report the deny-rule decision and the launcher path.
   Next, run `/pr-pipeline:tick` from the target checkout.

## Running setup again

Setup is idempotent. The same answers for a configured repository create no
labels and leave the config unchanged (`changed: false`). The probe still runs
and records fresh evidence. When a tick finds a target with no
`[repos."owner/name"]` section, it exits 6 and names this skill. See
[unconfigured targets](../tick/README.md#unconfigured-targets).
