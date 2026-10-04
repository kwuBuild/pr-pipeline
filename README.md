# pr-pipeline

[![CI](https://github.com/kwuBuild/pr-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/kwuBuild/pr-pipeline/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-orange.svg)

**Label a GitHub issue. Get back a pull request that Claude Code wrote, Codex
reviewed, and Claude Code fixed, ready for you to merge.**

pr-pipeline is a Claude Code plugin built on a deterministic state machine.
GitHub labels hold the state of each issue and pull request. Plain Python code,
not a language model, reads the labels and starts the correct agent for the
next step. The agents write and review the code. The pipeline does the
bookkeeping: it moves labels, opens pull requests, and records each review and
fix on GitHub. It spends no model tokens on this work.

- **A deterministic state machine.** Code, not a model, chooses each step.
  Workflow state lives outside the conversation, in GitHub labels and the
  pipeline's local dispatch markers and results. A session crash, `/clear`, or
  compaction does not lose it, and the next tick continues from it. Damaged or
  missing local state can need manual recovery; see the
  [diagnostics guide](skills/tick/DEBUG.md).
- **A different model reviews.** Claude Code implements and fixes. Codex
  reviews each change with a read-only policy, so no model approves its own
  work.
- **A record on GitHub.** Each step leaves a pull request, a review comment, a
  fix summary, or a label change that you and your team can read later.
- **A native loop that spends no tokens.** The loop is a Python process. It
  starts the next tick within seconds of an agent finishing, and on macOS it
  notifies you when an item needs you. You do not need `/loop`.
- **A Sonnet-class operator.** Orchestration is code, so the Claude Code
  session that runs the loop does not need a top-tier model.
- **Your agents, with your skills.** Each implementer and fixer is a full
  Claude Code session with your settings and plugins. Each agent runs in its
  own git worktree, with a fresh context.
- **You stay in control.** Only issues that you wrote enter the pipeline. The
  pipeline never merges, and three labels stop automation until you act.

<!-- Demo recording goes here: a short terminal capture of /pr-pipeline:tick
     and /pr-pipeline:status, or a screenshot of a PR's label history. -->

![The ai: label state machine. An issue moves from ai:available to ai:implementing. The pipeline opens a pull request at ai:implemented. Codex reviews it. Approved pull requests go to ai:ready-for-human. Pull requests with findings loop through ai:changes-requested, ai:fixing, and ai:revised back to review.](docs/images/pipeline-state-machine.svg)

## What it looks like

For example, you write five well-scoped issues in the morning, label them
`ai:available`, and start the loop. Then you do something else.

1. The pipeline starts three Claude Code agents, its limit for each local
   repository, each in its own git worktree. The other two issues wait for a free slot.
2. When an agent pushes its branch, the pipeline opens the pull request and
   starts a Codex review.
3. The pipeline posts Codex's findings on the pull request. A new Claude Code
   agent fixes them, and Codex reviews the fix.
4. This continues until each pull request passes review or needs you.

When you come back, three pull requests are at `ai:ready-for-human`, each with
its review history in the comments. One is at `ai:needs-human-decision` with
the question that the agent could not answer. One is still in review. You read
the finished pull requests and merge them.

**Use it for** a backlog of clear, bounded work: bug fixes, small features,
missing tests, refactors, and documentation updates. It suits developers who
already review AI-written code and want the review loop to run without them.

**Do not use it for** open design questions, exploratory pairing with an
agent, or changes that must deploy without a human review. Refine a vague idea
into a clear issue first; an interview skill such as
[grill-me](https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me)
helps.

## Why not ask Claude Code to run subagents?

You can ask one Claude Code session to start subagents for five issues.
Claude Code subagents are capable: they can resume, use their own model, and
work in worktrees. What an ad hoc session lacks is an enforced workflow. The
plan lives in the orchestrating model's context, and only the prompt decides
who reviews a change and when the review loop stops. That works for a short
session and becomes unreliable as the work grows.

| | Ad hoc: one session that starts subagents | pr-pipeline |
|---|---|---|
| What decides the next step | The orchestrating model, from its context | Python code that reads GitHub labels |
| Where the plan lives | In the orchestrator's conversation, which a crash, `/clear`, or compaction can lose | In GitHub labels and on-disk markers. The next tick continues from them. |
| Who reviews the code | Whoever the prompt names, often the same model | Always a different model (Codex), with a read-only policy |
| Isolation | Worktrees, if you set them up | A git worktree and a fresh context for each agent, always |
| Where you watch progress | Terminal output | Labels, pull requests, and review comments on GitHub |
| Record of the work | The session transcript, which only you can read | Pull requests, reviews, fix summaries, and label history that stay on GitHub |
| Model that operates it | A strong model, because orchestration needs reasoning | A Sonnet-class model, because orchestration is code |
| Tokens spent while agents work | A model turn for every check, on a context that keeps growing | None. The loop is a Python process. |
| When the next step starts | When the orchestrator next takes a turn | Within seconds of an agent finishing |
| Operational limits | Whatever you configure for each subagent; nothing caps review rounds across the workflow | At most three pipeline-managed agents for each local repository, five reviews for each pull request, and a deadline for each agent |

Hosted coding agents also turn an issue into a pull request. They usually run
in the vendor's cloud with the vendor's model. pr-pipeline runs on your
machine, with your toolchain, your Claude Code setup, and your plugins. It also
puts a second model between the code and you.

## GitHub keeps the record

You can ask one model to call another, for example a Claude Code subagent, or
Claude Code calling Codex. That work stays in one terminal session. pr-pipeline
does the same work through GitHub, so each step leaves a record:

- **Visibility.** Labels show the stage of each item. Your team can follow
  progress on GitHub without access to your terminal.
- **History.** Each pull request keeps its Codex reviews, the fixer's decision
  on each finding, and its label changes. Later, you or a teammate can read why
  the change has its final form.
- **Feedback on exact lines.** When a pull request needs your changes, leave
  review comments on the lines in GitHub's diff view. Then ask Claude Code to
  address all of them in one pass. This is easier than a prompt for each
  change, one after another. The pipeline's own fixer answers only the Codex
  review, so give your comments to a Claude Code session that you direct.
- **One workspace for engineers and agents.** People and agents use the same
  issues, pull requests, and reviews. You review AI work with the tools that
  you already use for your team's work.

## Smart agents in a simple pipeline

pr-pipeline is infrastructure. The pipeline decides what runs and when. The
agents decide how to do the work.

- **Your agents bring your skills.** Each implementer and fixer is a full
  headless Claude Code session. It loads your Claude Code settings and
  installed plugins, and it follows the target repository's `CLAUDE.md` and
  `AGENTS.md`. Skills that you already use, for example
  [Superpowers](https://github.com/obra/superpowers) for test-driven
  development and verification, are available to every agent.
- **The pipeline brings its own rules.** The fixer follows the plugin's
  `review-response-hygiene` skill when it answers review findings. The Codex
  reviewer runs isolated, from the plugin's prompt. It takes only its model
  and reasoning effort from your Codex configuration.
- **Orchestration costs no tokens.** The loop that waits for agents, reads
  their results, and starts the next step is plain Python. Your tokens go to
  the agents that write and review code, not to a model that watches them.
- **Better agents need no pipeline change.** When you add a skill or change a
  model, the pipeline runs the same state machine with smarter agents.

## Requirements

- macOS or Linux. CI tests both. Windows is not tested.
- [Claude Code](https://code.claude.com/docs), with its CLI
  `claude` on `PATH`.
- [Codex CLI](https://github.com/openai/codex) `codex` on `PATH`. Codex is the
  reviewer.
- [GitHub CLI](https://cli.github.com/) `gh`, logged in to an account with
  write access to the target repository.
- `git`, `jq`, and Python 3.11 or later. The pipeline uses only the Python
  standard library.

Desktop notifications appear only on macOS. On Linux the tick log still
records each event.

## Quick start

1. Install the plugin. This repository is its own plugin marketplace.

   ```text
   /plugin marketplace add kwuBuild/pr-pipeline
   /plugin install pr-pipeline
   ```

2. Run setup once for each repository that the pipeline will work on. Setup
   confirms your GitHub login, creates the `ai:` labels, proposes verification
   commands for you to approve, tests the engine permissions, and installs the
   `pr-pipeline` launcher.

   ```text
   /pr-pipeline:setup
   ```

3. In the target repository, add the `ai:available` label to an issue that you
   wrote. You can also ask Claude Code which of your open issues are clear
   enough for the pipeline, and have it add the label. Check the issues that
   it labels before you start a tick.

4. Open Claude Code in the target repository, in auto mode. A Sonnet-class
   model is enough. Run one tick for that issue, for example issue 42:

   ```text
   /pr-pipeline:tick #42
   ```

   The tick checks the Codex reviewer when needed, does one pass, and reports
   what it started.

   We recommend a target, such as `#42` or `pr:57`, for each tick and loop
   that you start. Without a target, the tick picks up every issue and pull
   request that has a pickup label, which can start more work than you
   expect. See [Choose what runs](#choose-what-runs).

5. Ask Claude Code to start the loop for the same item, for example "start the
   pr-pipeline loop for #42". The tick skill tells it to run `tick --loop` as
   a background command with the same target and flags as your first tick.
   Keep the session open while the loop runs.

When a pull request gets `ai:ready-for-human`, read it and merge it.

**You do not need `/loop`.** The pipeline has its own loop, a Python process
that runs the ticks. While it runs:

- It spends no model tokens. Claude Code is called again only when the loop
  exits.
- It starts the next tick within seconds of an agent finishing, instead of
  waiting for a timer.
- On macOS, it sends a desktop notification when an item needs you.

The loop exits after the queue has been quiet for three 30-minute checks, or
when it stops on an error. Claude Code then reads the tick log and reports what
finished, what waits for you, and the cause of any failure, using the
[diagnostics guide](skills/tick/DEBUG.md). To resume, start the loop again.
Agents keep running when the loop stops, and the next tick collects their
results.

If you want a report after every tick instead, use `/loop` with the prompt in
[Running under /loop](skills/tick/SKILL.md#running-under-loop). Each tick then
costs a model turn. `/loop` needs the Claude Code session open and idle, and it
wakes at most once a minute. Its prompt runs the whole queue unless you add
your target and flags to it.

To run without Claude Code, use the launcher from a terminal. Check the Codex
reviewer once, then start the same loop:

```bash
~/.claude/pr-pipeline/bin/pr-pipeline preflight
~/.claude/pr-pipeline/bin/pr-pipeline tick --loop
```

## Choose what runs

The label is the queue. To work on a chosen set of issues, add `ai:available`
to those issues only. A tick can also focus on one item:

| Run | Scope |
|---|---|
| `/pr-pipeline:tick` | Every issue and pull request that has a pickup label |
| `/pr-pipeline:tick #42` | Issue 42, then its pull request through review and fixes |
| `/pr-pipeline:tick pr:57` | Pull request 57 only |

The launcher accepts the same targets, for example
`pr-pipeline tick --loop '#42'`. A targeted loop goes quiet when that item
finishes or waits for you, even if other items still wait. Targeting does not
skip any safety gate.

Targets and flags apply only to the run that receives them. Each new tick or
loop starts with the whole queue and your configured defaults, so pass the
same target and flags again when you start the loop.

Flags select the agents: `--impl-engine` chooses the implementation engine,
`--model` the Claude model, and `--review-model` and `--review-effort` the
Codex reviewer. See the [operator guide](skills/tick/README.md#invocation).

## How it works

### GitHub labels are the state

Each issue or pull request carries one `ai:` label, as the diagram above
shows. The label is the only record of the item's stage, so you can see and
change the pipeline from GitHub itself. The issue carries the first two
labels. When the pull request opens, the issue loses its label and the pull
request carries the rest.

Three labels stop automation and wait for you:

| Label | Meaning |
|---|---|
| `ai:needs-human-decision` | An agent or the pipeline found a scope, design, or custody question that it must not decide alone. |
| `ai:max-iterations` | The pull request reached five completed reviews without approval. |
| `ai:blocked` | You set this to hold an item for a dependency or a decision. |

### Each tick harvests, then dispatches

The control plane is one Python process. Each run, called a *tick*, has two
fast phases:

1. **HARVEST.** Read the result of each agent that finished, publish its pull
   request or review, and move the label to the next state.
2. **DISPATCH.** Find items that wait for work and start the correct agent for
   each one in its own git worktree.

The agents run between ticks, not during them. A tick makes no model calls.
Its decisions follow from labels, dispatch markers, and agent result files, not
from a model's judgment. In a loop, the tick checks often while agents run,
checks every 30 minutes when the queue is idle, and stops after three quiet
checks in a row.

### Different models implement and review

Claude Code implements and fixes. The Codex CLI reviews, with a read-only
policy. A different model reviews each change, so no model approves its own
work.

## Safety and scope

- **You merge.** The pipeline never merges a pull request. It stops at
  `ai:ready-for-human`.
- **Only your issues enter the pipeline.** Implementation starts only for
  issues written by the GitHub login that you confirm during setup.
- **Changes to the reviewer's own rules can need your review.** Set
  `allow_touching = false` to send pull requests that edit `CLAUDE.md`,
  `AGENTS.md`, or the reviewer's configured checklists to
  `ai:needs-human-decision`.
- **Review cycles have a limit.** The pipeline stops a pull request at
  `ai:max-iterations` instead of letting the review and fix cycle continue.
- **Manual work is left alone.** If you, or an agent that you direct, take an
  item, the tick does not take it back.
- **Setup does not change your repository's files.** It creates the `ai:`
  labels on GitHub. On your machine it writes only under `~/.claude/`: the
  operator configuration, the launcher, and the deny rules that you approve.

**Use auto mode.** Auto mode is the default for the Claude Code agents, and we
suggest it for the Claude Code session that runs the loop. In auto mode, a call
outside the explicit grants goes to Claude Code's permission classifier instead
of a prompt that nobody is present to answer. Add deny rules for operations
that you forbid. Setup offers rules for force-push and remote branch deletion.
If auto mode fails setup's probe for your model or provider, setup records
`dontAsk`, which denies every call that would prompt.

**The tick runs outside the Claude Code sandbox.** The tick must run with
`dangerouslyDisableSandbox: true`. Under the default sandbox it cannot inspect
or stop the agent processes it starts, and the agents lose network access to
their model endpoints. The agents therefore run as your user account. Their
explicit tool grants, their permission mode, and your deny rules limit what
they can do. The Codex reviewer runs under its own read-only policy. See
[engine permission modes](skills/tick/README.md#engine-permission-modes) and
[why the sandbox is disabled](skills/tick/DEBUG.md#why-dangerouslydisablesandbox-true).

**Out of scope:** automatic merging, more than one repository in a single
tick, a background service, and replacing your CI.

## Commands

| Command | Purpose |
|---|---|
| `/pr-pipeline:setup` | Connect the pipeline to a repository and write your operator configuration. |
| `/pr-pipeline:tick` | Run one tick: harvest finished agents, then dispatch eligible work. |
| `/pr-pipeline:status` | Show a read-only dashboard of running agents and items grouped by `ai:` label. Status does not move work forward. |

The plugin also includes skills that the pipeline's agents and maintainers
use: `definition-of-done`, `review-response-hygiene`,
`automation-control-plane-review`, `durability-boundary-review`, and
`loop-cadence-contract-review`.

## Documentation

| Read this | For |
|---|---|
| [Operator guide](skills/tick/README.md) | Commands, configuration, engine options, and observability |
| [Diagnostics](skills/tick/DEBUG.md) | Failed runs, recovery, and invariants |
| [Architecture](docs/architecture.md) | Why the control plane has its current design |
| [Internals](docs/working/pr-pipeline-internals.md) | Code map for dispatch, harvest, and custody |
| [Changelog](CHANGELOG.md) | Changes in each release and upgrade steps |

## Project status

pr-pipeline is at version 0.1.0. Until version 1.0.0, a minor release can
contain breaking changes. Each [changelog](CHANGELOG.md) entry lists the steps
that an upgrade needs.

## Contributing

Read [AGENTS.md](AGENTS.md) first. It links the
[PR guidelines](docs/working/pr-guidelines.md),
[commit guidelines](docs/working/commit-guidelines.md), and test commands. Run
the tests with a Python 3.11 or later interpreter:

```bash
python3.13 -m pytest skills/ -q
```

## License

[MIT](LICENSE)
