# pr-pipeline

[![CI](https://github.com/kwuBuild/pr-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/kwuBuild/pr-pipeline/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-orange.svg)

**Smart agents, dumb pipes.**

**You scope it. Agents ship it. You merge it.**

pr-pipeline is a Claude Code plugin that turns a GitHub issue into a reviewed
pull request. Claude Code writes the change, Codex reviews it, and Claude Code
answers the review, round after round, until the pull request is ready for you
to merge. A plain, predictable pipeline moves the work between them, so you
don't have to.

<!-- Demo recording goes here: a short terminal capture of /pr-pipeline:tick
     and /pr-pipeline:status, or a screenshot of a PR's label history. -->

## Stop being a meat proxy

You prompt one agent to implement the issue. You ask another agent to review
the diff. You paste the review back into the first one. You prompt again. And
again. The agents do the thinking. You carry their messages.

**Don't be the messenger. Be the manager.** Write the issue. Delegate it.
Review what comes back. pr-pipeline carries every message in between.

**And don't hire a model to be the messenger either.** You can ask one Claude
Code session to manage subagents, and it works for an afternoon. Then the plan
lives in one model's context, where a crash, `/clear`, or compaction can lose
it. The prompt alone decides who reviews and when to stop. And the manager
spends model turns to check on its workers, on a context that keeps growing.
Carrying messages is a job for a pipe, not a model.

## Smart agents, dumb pipes

The intelligence lives at the edges, in the agents. The pipe in the middle is
deliberately dumb: a small Python program that reads GitHub labels and starts
the right agent for the next step. It does not improvise, forget, or get talked
out of its rules.

![The ai: label state machine. An issue moves from ai:available to ai:implementing. The pipeline opens a pull request at ai:implemented. Codex reviews it. Approved pull requests go to ai:ready-for-human. Pull requests with findings loop through ai:changes-requested, ai:fixing, and ai:revised back to review.](docs/images/pipeline-state-machine.svg)

- **The agents make the judgment calls.** Each implementer and fixer is a full
  Claude Code session with your settings, plugins, and skills, in its own git
  worktree with a fresh context. The agents decide what to change, which review
  findings matter, and how to fix them.
- **A different provider reviews every change.** Codex reviews under a
  read-only policy, so the agent that wrote the code never approves it.
- **The pipe never forgets.** Each item's state is a GitHub label plus a small
  record on disk, not a conversation. A crash or `/clear` loses nothing; the
  next run continues where the last one stopped.
- **The rules are code, not a prompt.** A pull request gets at most five
  reviews. Every agent has a deadline. An item whose agent keeps failing
  without a result goes to you after three attempts. A usage limit pauses new
  work for that provider until the limit resets. An agent that goes silent for
  30 minutes is flagged.
- **You pay for the work, not the supervision.** No model sits in the control
  loop, so your tokens go to the agents that write and review code, not to a
  manager that watches them.
- **It keeps moving while you're away.** In loop mode, the next step starts
  within seconds of an agent finishing. On macOS, you get a notification when
  an item needs you.
- **Better agents, same pipe.** Add a skill or switch models, and every run
  uses it. The pipeline does not change.

## Assign it like a teammate. Review it like a teammate.

pr-pipeline does not invent a new workflow. It runs the one your team already
uses: an issue, a pull request, a code review, a revision. The AI engineer
works inside it.

- **You own the planning.** pr-pipeline does not plan. You write and scope the
  issue, as you would for a colleague. To turn a vague idea into a clear issue
  first, try an interview skill such as
  [grill-me](https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me).
- **No black box.** Every step is a label, a commit, or a comment on GitHub:
  the Codex review, the fixer's reply to each finding, and every label change.
  Your team can follow along without access to your terminal, and a later
  reader can see why the change has its final form.
- **Step in at any time.**
  - *Pause:* add `ai:blocked`, and the pipeline stops advancing that item,
    even while an agent works on it.
  - *Take over:* pick up an item yourself, and the pipeline leaves it alone.
  - *Redirect:* leave line comments in GitHub's diff view, then ask a Claude
    Code session that you direct to address all of them in one pass. The
    pipeline's own fixer answers only the Codex review.
- **You merge.** The pipeline never merges. It stops at `ai:ready-for-human`.

## A day with pr-pipeline

Stop babysitting your agents. Write five well-scoped issues in the morning,
label them `ai:available`, and start the loop. Then do something else.

1. The pipeline starts three Claude Code agents, its limit for each local
   repository, each in its own git worktree. The other two issues wait for a
   free slot.
2. When an agent pushes its branch, the pipeline opens the pull request and
   starts a Codex review.
3. The pipeline posts Codex's findings on the pull request. A new Claude Code
   agent fixes them, and Codex reviews the fix.
4. This continues until each pull request passes review or needs you.

When you come back, three pull requests are at `ai:ready-for-human`, each with
its review history in the comments. One is at `ai:needs-human-decision` with
the question that the agent could not answer. One is still in review. You read
the finished pull requests and merge them.

## Is it for you?

**Use it for** a backlog of clear, bounded work: bug fixes, small features,
missing tests, refactors, and documentation updates. It suits developers who
already review AI-written code and want the review loop to run without them.

**Do not use it for** open design questions, exploratory pairing with an agent,
or changes that must deploy without a human review.

**Know before you start:**

- You need a Codex CLI login as well as Claude Code. Codex is the reviewer.
- It runs on your machine, from a Claude Code session or a terminal that stays
  open while the loop runs. It is not a hosted service.
- Only issues and pull requests that your GitHub login wrote enter the
  pipeline.
- It does not wait for your CI. The agents run the test commands that you
  approve during setup, and your CI still runs on each pull request.

## Why not one session that manages subagents?

Claude Code subagents are capable: they can resume, use their own model, and
work in worktrees. What an ad hoc session lacks is an enforced workflow.

| | One session that manages subagents | pr-pipeline |
|---|---|---|
| What decides the next step | The manager model, from its context | A Python program that reads GitHub labels |
| Where the plan lives | In the manager's conversation, which a crash, `/clear`, or compaction can lose | In GitHub labels and on disk. The next run continues from them. |
| Who reviews the code | Whoever the prompt names, often the same model | Always a different provider (Codex), with a read-only policy |
| Isolation | Worktrees, if you set them up | A git worktree and a fresh context for each agent, always |
| Where you watch progress | Terminal output | Labels, pull requests, and review comments on GitHub |
| Record of the work | The session transcript, which only you can read | Pull requests, reviews, fix replies, and label history on GitHub |
| Cost of coordination | The manager's model turns, on a context that keeps growing | None. Coordination is code. |
| When the next step starts | When the manager next takes a turn | In loop mode, within seconds of an agent finishing |
| Limits | Whatever you configure for each subagent; nothing caps review rounds | At most three agents for each local repository, five reviews for each pull request, and a deadline for each agent |

Hosted coding agents also turn an issue into a pull request. They usually run
in the vendor's cloud with the vendor's model. pr-pipeline runs on your
machine, with your toolchain, your Claude Code setup, and your plugins, and it
puts a second provider's review between the code and you.

## Requirements

- macOS or Linux. CI tests both. Windows is not tested.
- [Claude Code](https://code.claude.com/docs), with its CLI `claude` on `PATH`.
- [Codex CLI](https://github.com/openai/codex) `codex` on `PATH`.
- [GitHub CLI](https://cli.github.com/) `gh`, logged in to an account with
  write access to the target repository.
- `git`, `jq`, and Python 3.11 or later. The pipeline uses only the Python
  standard library.

Desktop notifications appear only on macOS. On Linux, the tick log still
records each event.

## Quick start

1. Install the plugin. This repository is its own plugin marketplace.

   ```text
   /plugin marketplace add kwuBuild/pr-pipeline
   /plugin install pr-pipeline
   ```

2. Run setup once for each repository that the pipeline will work on. Setup
   confirms your GitHub login, creates the `ai:` labels, proposes test commands
   for you to approve, tests the agents' permissions, and installs the
   `pr-pipeline` launcher.

   ```text
   /pr-pipeline:setup
   ```

3. In the target repository, add the `ai:available` label to an issue that you
   wrote. You can also ask Claude Code which of your open issues are clear
   enough, and have it add the label. Check the issues that it labels before
   you continue.

4. Open Claude Code in the target repository, in auto mode. This session only
   starts the pipeline and reads its log, so a fast model such as Sonnet is
   enough. Run one pass for that issue, for example issue 42:

   ```text
   /pr-pipeline:tick #42
   ```

   The first run checks that Codex is ready, starts work on the issue, and
   reports what it started.

5. Ask Claude Code to start the loop for the same item, for example "start the
   pr-pipeline loop for #42". Claude Code runs the loop as a background
   command with the same target and flags, and keeps the session free. Keep
   the session open while the loop runs.

When the pull request reaches `ai:ready-for-human`, read it and merge it.

Always name a target, such as `#42` or `pr:57`. Without one, the pipeline picks
up every issue and pull request that waits for work, which can start more than
you expect. See [Choose what runs](#choose-what-runs).

The loop stops after the queue has been quiet for three 30-minute checks, or
when it hits an error. Claude Code then reads the log and tells you what
finished, what waits for you, and the cause of any failure, with help from the
[diagnostics guide](skills/tick/DEBUG.md). To resume, start the loop again.
Agents keep running when the loop stops, and the next run collects their
results.

**Other ways to run.** For a report after every pass, use `/loop` with the
prompt in [Running under /loop](skills/tick/SKILL.md#running-under-loop); each
pass then costs a model turn. To run without Claude Code, use the launcher from
a terminal:

```bash
~/.claude/pr-pipeline/bin/pr-pipeline preflight
~/.claude/pr-pipeline/bin/pr-pipeline tick --loop '#42'
```

## Choose what runs

The label is the queue. To work on a chosen set of issues, add `ai:available`
to those issues only. A run can also focus on one item:

| Run | Scope |
|---|---|
| `/pr-pipeline:tick` | Every issue and pull request that waits for work |
| `/pr-pipeline:tick #42` | Issue 42, then its pull request through review and fixes |
| `/pr-pipeline:tick pr:57` | Pull request 57 only |

The launcher accepts the same targets, for example
`pr-pipeline tick --loop '#42'`. A targeted loop goes quiet when that item
finishes or waits for you, even if other items still wait. Targeting does not
skip any safety gate.

Targets and flags apply only to the run that receives them. Each new run starts
with the whole queue and your configured defaults, so pass the same target and
flags again when you start the loop.

Flags select the agents: `--impl-engine` chooses the implementation engine
(`claude`, or `claude-ds` for DeepSeek through Claude Code), `--model` the
Claude model, and `--review-model` and `--review-effort` the Codex reviewer.
See the [operator guide](skills/tick/README.md#invocation).

## How it works

### GitHub labels are the state

Each issue or pull request carries one `ai:` label, as the diagram above shows.
The label tells you and the pipeline where the item is, so you can see and
change the pipeline from GitHub itself. The issue carries the first two
labels. When the pull request opens, the issue loses its label and the pull
request carries the rest.

Three labels stop automation and wait for you:

| Label | Meaning |
|---|---|
| `ai:needs-human-decision` | An agent or the pipeline found a scope or design question, or a conflict over who owns the item, that it must not decide alone. |
| `ai:max-iterations` | The pull request reached five completed reviews without approval. |
| `ai:blocked` | You set this to hold an item for a dependency or a decision. |

### Each run harvests, then dispatches

One pass of the pipeline is called a *tick*. It is a short Python run with two
phases:

1. **Harvest.** Read the result of each agent that finished, publish its pull
   request or review, and move the label to the next state.
2. **Dispatch.** Find items that wait for work and start the correct agent for
   each one in its own git worktree.

The agents run between ticks, not during them. A tick never asks a model what
to do: the same labels and agent results always lead to the same next step.
The loop runs ticks often while agents work, checks every 30 minutes when the
queue is idle, and stops after three quiet checks in a row.

Besides the labels, the pipeline keeps a small record on disk for each running
agent. If that local record is damaged or lost, an item can need manual
recovery; the [diagnostics guide](skills/tick/DEBUG.md) explains how.

## Safety and trust

- **You merge.** The pipeline never merges a pull request. It stops at
  `ai:ready-for-human`.
- **Only your work enters the pipeline.** Implementation starts only for
  issues written by the GitHub login that you confirm during setup. Review
  starts only for pull requests by that login from the same repository, so
  pull requests from forks never reach an agent.
- **Changes to the reviewer's own rules can need your review.** Set
  `allow_touching = false` to send pull requests that edit `CLAUDE.md`,
  `AGENTS.md`, or the reviewer's configured checklists to
  `ai:needs-human-decision`.
- **Review cycles have a limit.** The pipeline stops a pull request at
  `ai:max-iterations` instead of letting the review and fix cycle continue.
- **Manual work is left alone.** If you, or an agent that you direct, take an
  item, the pipeline does not take it back.
- **Setup does not change your repository's files.** It creates the `ai:`
  labels on GitHub. On your machine, it writes only under `~/.claude/`: the
  operator configuration, the launcher, and the deny rules that you approve.

**Use auto mode.** Auto mode is the default for the Claude Code agents, and we
suggest it for the Claude Code session that runs the loop. In auto mode, a call
outside the explicit grants goes to Claude Code's permission classifier instead
of a prompt that nobody is present to answer. Add deny rules for operations
that you forbid. Setup offers rules for force-push and remote branch deletion.
If auto mode fails setup's probe for your model or provider, setup records
`dontAsk`, which denies every call that would prompt.

**The tick runs outside the Claude Code sandbox.** The tick must run with
`dangerouslyDisableSandbox: true`. Under the default sandbox, it cannot inspect
or stop the agent processes it starts, and the agents lose network access to
their model endpoints. The agents therefore run as your user account. Their
explicit tool grants, their permission mode, and your deny rules limit what
they can do. The Codex reviewer runs under its own read-only policy. See
[engine permission modes](skills/tick/README.md#engine-permission-modes) and
[why the sandbox is disabled](skills/tick/DEBUG.md#why-dangerouslydisablesandbox-true).

**Out of scope:** planning, automatic merging, more than one repository in a
single tick, a hosted service, and replacing your CI.

## Commands

| Command | Purpose |
|---|---|
| `/pr-pipeline:setup` | Connect the pipeline to a repository and write your operator configuration. |
| `/pr-pipeline:tick` | Run one pass: harvest finished agents, then dispatch waiting work. |
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
