# Guidance consolidation check: one Claude and one Codex implementation

This is a dated evidence record for issue #52, written on 1 November 2026. It is
not a maintained contract. Do not update it when the guide changes; record a new
check instead.

## Purpose

Issue #49 consolidated the shared agent guide. The repository `CLAUDE.md` became
a one-line `@AGENTS.md` import. This record checks one ordinary implementation
task per engine after that change. It asks whether the agents read less
duplicated context and still delivered verified work. It does not measure
savings as a percentage, and it does not compare engines.

## Samples

Both samples are naturally occurring tasks. No synthetic task was launched for
this check.

| Field | Claude sample | Codex sample |
|---|---|---|
| Task and change | #80 → PR #81, dispatch admission cap; 12 files, +576/−69, one commit | #51 → PR #54, normalized engine usage; 8 files, +955/−22 |
| Session | `e2596c60-b3fd-4b80-b6ac-53deffb06fdd`, 10 October 2026, 21:22:39–21:47:11 UTC | `01a0bfa1-febf-7561-8079-833a9fcc960c`, 20 September 2026, 16:24–16:41 UTC |
| Mode | Unattended `pr-pipeline:tick` implementer; fresh worktree; Claude Code 2.1.296 `sdk-cli`, permission mode `auto` | Fresh interactive session |
| Model and effort | `claude-opus-5-5`, effort `high` | `gpt-6-astra`, effort `xhigh` |
| Guide revision | Base `99bde86`; repository `CLAUDE.md` is `@AGENTS.md`; `AGENTS.md` is 911 words (6,823 characters). The guide is the same as the current `main` | Base `09f4bd0` (merge of #49); #50 not yet included |
| Usage source | Native transcript; last usage record for each of the 109 assistant message IDs, summed | Native rollout, final `token_count.info.total_token_usage` |
| Input | 16,263,891 total: 16,029,392 cache read (98.6%), 234,281 cache write, 218 uncached | 3,488,757 total: 3,325,696 cached (95.3%), 163,061 uncached |
| Output | 72,201 | 28,898, including 9,589 reasoning |
| Context per request | First request 35,565 tokens; median 156,359; last 234,283 | Not extracted |
| Steps | 109 model requests; 127 tool calls (111 Bash, 8 Edit, 6 Write, 1 Read, 1 ToolSearch) | 35 token-count updates; 34 `exec` calls |
| Verification and publication | 1,988 tests passed; branch pushed; `IMPLEMENTATION_COMPLETE`; six CI checks succeeded; merged 11 October 2026 | 1,575 tests passed; PR pushed; five CI checks succeeded; merged 20 September 2026 |
| Independent review | Pipeline Codex reviewer round on head `5132b27`: "No blockers". Its usage is unavailable: when checked, the earliest retained tick-log file began at 22:17 UTC that day, after the review, and no file held a #80 or #81 engine record | Separate review on the same head approved, with one non-blocking documentation note |
| Author-side subagents | None | None. The host's auto-approval guardian ran as a separate session: 151,280 input (115,200 cached), 701 output. This is not code review |
| Charges | Client-reported estimate only: the transcript's terminal `cost-state` record gives `totalCostUSD` 6.5250184 (about $6.53), with `hasUnknownModelCost: false`. Its token counters match the Input and Output rows. Actual charges and quota attribution are not available. The pipeline `result.json` for this run is not retained | No monetary figure in the native session. Actual charges are not available |

The Codex figures come from the
[first evidence comment](https://github.com/kwuBuild/pr-pipeline/issues/52#issuecomment-5752280172)
on #52. The final token counters and the update count were checked again
against the compressed rollout for this record.

The Claude transcript method agrees with the pipeline record where both exist.
For #27 attempt 2 (session `cabac297`), the transcript sums equal the
`engine_result_summary` usage in the tick log: 396 uncached, 59,196,466 cache
read, 473,002 cache write, and 191,452 output. For #27 attempt 1 (session
`17ae3304`), the transcript sums are within 0.8% of the tick-log record.

## Observed context behavior

### Claude sample

- The guide was injected once and was not read again. The session made no
  `Read` or `cat` call on `AGENTS.md` or `CLAUDE.md`. The Codex sample read the
  guide again with `cat` after injection. This is one avoidable read.
- The session read the `definition-of-done` skill once, with `cat` on the
  installed `SKILL.md`. It made no `Skill` tool call and ran no generic skill
  chain. The Superpowers SessionStart hook injected 3,673 characters, but the
  session did not follow its brainstorming or planning chain.
- The session read routed documents in bounded ranges. It used `grep -n` and
  `sed -n` ranges on `docs/working/pr-pipeline-internals.md`,
  `docs/architecture.md`, `skills/tick/README.md`, and `README.md`, and it read
  the first 80 lines of `docs/working/commit-guidelines.md`. It did not read a
  whole reference document.
- The session made no repeated unchanged read. The only repeated command was a
  wait loop on a background test run.
- One tool result was larger than 20,000 characters: a `git diff` of three
  files (20,257 characters). No result had a truncation notice.

### Host-injected context, attributed separately

The opening context of the Claude sample was 35,565 tokens. Its main parts were
as follows. These are character counts from the transcript attachments; the
system prompt and hook rows are serialized JSON lengths, so they are
approximate. The token counters do not apportion cost to each part.

| Source | Owner | Characters |
|---|---|---|
| System prompt and tool definitions (`prompt_snapshot`) | Claude Code host | 42,490 |
| Skill catalog, 124 skills | Host and installed plugins | 28,237 |
| Dispatch prompt | pr-pipeline controller | 8,506 |
| Repository guide (`CLAUDE.md` 10 + `AGENTS.md` 6,823) | This repository | 6,833 |
| User-wide guidance (`~/.claude/CLAUDE.md` + `~/.codex/AGENTS.md`) | Operator | 6,072 |
| Superpowers SessionStart hook | Global plugin | 3,673 |
| Auto-memory index | Operator | 670 |

The repository guide is about one quarter of the size of the host skill catalog.

## Conclusion

1. **Instruction size.** The reduction is real, but the file total and the
   per-engine injection are different measures.
   - *File total.* Before #49, the two repository entry files had 2,901 words
     (`CLAUDE.md` 1,870, `AGENTS.md` 1,031). At `09f4bd0` they had 863 words
     plus the import line; at `99bde86` and current `main`, 911 words. This
     measures the guide text that the repository keeps. No single session
     loaded the 2,901-word total.
   - *Claude injection.* Before #49, the repository `CLAUDE.md` did not import
     `AGENTS.md` (`git show '09f4bd0^:CLAUDE.md'` has no reference to it), so a
     Claude session loaded only `CLAUDE.md`: 1,870 words, 12,441 characters.
     At `99bde86` it loads the import line and `AGENTS.md`: 911 words, 6,835
     characters as raw files (the transcript attachment holds 6,833). The
     Claude-side reduction is therefore about 5,600 characters, or 45%.
   - *Codex injection.* A Codex session loads only `AGENTS.md`, so it receives
     only a small part of the reduction. The second #52 comment measured the
     injected block at 546 characters smaller after #49.
2. **Observed context behavior.** The Claude sample read no duplicate guidance
   and no whole reference document. It ran no generic skill chain. The Codex
   sample had one duplicate guide read. Neither sample showed the failure mode
   that #52 names, in which repeated skill reads or old worktree instructions
   dominate usage.
3. **Token accounting.** Cumulative input is dominated by re-sending the
   growing conversation. In the Claude sample, the opening context was 35,565
   tokens and the last request was 234,283 tokens; 98.6% of the cumulative
   input was cache reads. The repository guide is a fixed part of the opening
   context, and the host system prompt and skill catalog are each larger than
   it. For a Claude session, #49 removed about 5,600 characters of injected
   repository guide (12,441 characters before `09f4bd0`, 6,835 at `99bde86`;
   see conclusion 1). At a rough four characters per token, that is about
   1,400 tokens per request, or about 0.9% of the 156,359-token median request
   context. This is an estimate, not a measurement. The combined two-file
   reduction is not a per-request figure, because no session loaded both files
   before #49. These two samples differ in engine, mode, prompt, and task, so
   they give no A/B savings percentage.
4. **Charges.** Not measured. The Claude transcript holds a client-reported
   cost estimate of about $6.53 for the session; this is the host's own
   estimate, not a billing record. The Codex session holds no monetary figure.
   Actual charges and quota attribution are not available for either sample,
   and the token counters are not billing records.

## Remaining work

No further repository change is necessary.

- The largest fixed context items are the host system prompt, the skill
  catalog, and the global Superpowers hook. These are host or global plugin
  configuration, which #52 puts out of scope.
- The earlier #52 comment found that interactive worktrees cut before #49 kept
  the old guide. Pipeline implementers do not have this problem, because the
  tick cuts each worktree from the fetched base. The #80 worktree started at
  `99bde86`, which was the tip of `main` at dispatch.
- Large tool output was one result above 20,000 characters, with no truncation.
  `AGENTS.md` already tells agents to read bounded sections and keep command
  output brief.

## Method

- Claude sessions: every transcript under `~/.claude/projects/` for this
  repository that started after the #49 merge (20 September 2026, 16:17 UTC).
  Session #80 was selected because it is a fresh, single-attempt implementation
  in a worktree cut from the current base, with a merged result. The other
  candidates were interactive conversations, the two-attempt #27 run, or
  skill-probe sessions.
- Claude fields: `cwd`, `gitBranch`, `entrypoint`, `permissionMode`, and
  `effort` from the records; usage from assistant `message.usage`, deduplicated
  by message ID; tool calls from `tool_use` blocks; injected sizes from the
  `instructions`, `skill_listing`, `prompt_snapshot`, and
  `hook_additional_context` attachments; the client-reported cost estimate from
  the terminal `cost-state` record; test and sentinel results from the
  session's final messages. A local script extracted these fields. The
  transcript bodies were not read.
- Pipeline records: `.claude/worktrees/.tick-log/` `engine_result_summary`
  entries, as retained on 1 November 2026. Tick-log retention can remove
  older files.
- Change size, CI, review, and merge state: `gh pr view`.
- Word, byte, and character counts: `git show <rev>:<file> | wc` for
  `AGENTS.md` and `CLAUDE.md` at `09f4bd0^`, `09f4bd0`, `99bde86`, and current
  `main`. Per-session injection uses raw-file character counts (`wc -m`)
  before and after, so both sides use the same unit.
