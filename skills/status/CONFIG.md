# Configuration export schema

`tick.py config --json` returns the resolved configuration for a new default
invocation in the calling process's context. It uses `pipeline.resolve_config()`;
it does not inspect an existing loop. See the
[operator commands](../tick/README.md#read-only-configuration-export).

The command reads the packaged floor, the operator's `[defaults]`, and the
matching `[repos."owner/name"]` table. The supported process environment also
applies: `PR_PIPELINE_CONFIG` and `CLAUDE_CONFIG_DIR` select the operator file,
`PIPELINE_TRUSTED_AUTHOR` overrides the operator login, and `PIPELINE_REPO`
selects a matching local Git remote. Configuration location and precedence stay
owned by the [resolver contract](../../docs/working/pr-pipeline-internals.md#7-operator-configuration-resolution).
The target repository cannot supply its own operator configuration file.

Only `config --json` is accepted. Route override flags, `--loop`, item targets,
and `--only` are not part of this command. The command takes no tick lock,
changes no files, and starts no tick or engine. It needs no GitHub request or
model CLI. Its local Git reads use the same main-checkout target resolver as
the tick, including when called from a linked worktree or nested directory.

## Success

Exit status is 0. Standard output contains one JSON object followed by a
newline; standard error is empty. Version 1 has exactly these top-level fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | `1` |
| `target` | object | `repo` (`owner/name`) and `remote` (local Git remote name) |
| `observed_at` | string | UTC ISO 8601 timestamp when the command began resolving |
| `context` | object | `kind: "standalone_default_invocation"` and a fixed `description` of the input layers and existing-loop limitation |
| `config` | object | Only the values listed below |

`config` contains:

| Field | Type | Meaning |
| --- | --- | --- |
| `operator_login` | string | Resolver's operator login, including supported environment precedence; empty if unset |
| `routes` | object | `implement` and `fix`, each with `engine`, `model`, `effort`, and `cooldown_domain` strings |
| `max_in_flight` | integer | Admission cap |
| `max_iterations` | integer | Review/fix round limit |
| `implementer_timeout_seconds` | integer | Implementer deadline |
| `fixer_timeout_seconds` | integer | Fixer deadline |
| `review_timeout_seconds` | integer | Reviewer deadline |
| `engine_stall_seconds` | integer | Engine output silence threshold |
| `engine_stall_startup_grace_seconds` | integer | Additional grace before the first output event |

Routes are the resolver's resolved values. A blank fixer engine inherits the
implementer engine. A blank fixer model inherits the implementer model only
when the engines match. Blank fixer effort inherits within the Claude family
or within Codex; crossing that boundary uses the destination default.
An empty resolved model or effort means no override; this export
does not query a provider's model defaults or convert engine-specific effort
defaults into applied settings. `cooldown_domain` uses the tick's shared domain
mapping (`claude`, `claude-ds`, or `codex`). It is a route domain, not evidence that a
cooldown is active or applies to a particular item.

The fixed context description is:

> Packaged, operator-default and repository configuration with this process's supported environment inputs; no CLI route overrides. This is not an existing loop's applied configuration.

No raw environment, credentials, arbitrary keys, engine settings, resolver
warnings, file paths, or other configuration values are included. Resolver
warnings that retain a valid conservative value do not turn success into an
error. This command adds no validation or startup admission checks: a successful
export can have an unset login or a target without a setup section.

## Errors

Errors have the same `schema_version`, `target`, `observed_at`, and `context`
fields. `error` replaces `config`; partial configuration is never emitted.
`target` is `null` when target resolution did not succeed or was not attempted.
Standard output still contains one JSON object; standard error is empty.

| Exit | `error.code` | `error.message` | `error.settings` |
| --- | --- | --- | --- |
| 2 | `invalid_arguments` | `Usage: tick.py config --json` | Empty list |
| 3 | `target_resolution_failed` | `Cannot resolve a unique local GitHub target.` | `["target"]` |
| 3 | `invalid_configuration` | `Configuration could not be resolved; check the named settings.` | Sorted, deduplicated safe setting identifiers |

Setting identifiers are known resolver keys (including keys outside the success
allowlist), `defaults`, `operator`, `repos`, `scan`, `scan.repos`,
`PR_PIPELINE_CONFIG`, `CLAUDE_CONFIG_DIR`, or `packaged_config`. An unreadable
operator file, malformed TOML, unexpected file/parse failure, or unrecognized
diagnostic uses `configuration`. A stall/deadline relationship error identifies
both settings. Rejected values, operator file paths, arbitrary repository-table
names and raw resolver error strings never appear in these diagnostics.

These statuses describe the Python command. The shell launcher can fail before
it reaches the command, for example when no supported Python is installed.

## Consumer boundary

A loop started with `--impl-engine claude --model custom-model` can differ from
a standalone export that resolves `claude-ds` from operator defaults. A loop
started under `PIPELINE_TRUSTED_AUTHOR=operator-a` can differ from a later
export under `PIPELINE_TRUSTED_AUTHOR=operator-b`. Changes to files and the
installed code can also change a later export.

Consumers must display the invocation context and must not use this result
alone to assert an existing loop's operator identity, author eligibility,
provider choice or per-item cooldown domain. Such claims require a separate
tick-owned applied-configuration snapshot tied to that loop invocation. That
snapshot is outside #123. #120 retains its initial Phase 1 deferral of those
claims and has no hard dependency on this export.
