# Phase 1 issue breakdown — handoff note

Companion to `plugin-distribution-design.md`. It records **why the design was cut
into the issues it was cut into**, so a reviewer can disagree with the
decomposition itself rather than only with the contents of one issue.

Issues #7-#21 were filed from the merged design. Nothing here restates an issue
body; this is the reasoning between them.

---

## The goal the breakdown serves

Phase 1 has one deliverable: **an operator installs the plugin once and drives a
repository that is not this one, through a full implement → review → fix →
re-review cycle.** Every issue is justified by that sentence or it does not
belong in phase 1.

Two consequences follow, and they explain most of the boundaries below.

- **A defect that only appears against a foreign repository is a phase 1 defect.**
  The hardcoded `"main"`, the `review-response-hygiene` worktree probe, and the
  missing label vocabulary are all invisible while dogfooding. They are not
  polish; they are the work.
- **A defect that appears on any repository is not phase 1's problem** unless it
  blocks that sentence. This is why the permission hardening is phase 2 and why
  two runtime items were filed outside both phases.

## How the cut was made

The design lists phase 1 as ten bullets. Those bullets are *scope*, not
*issues* — several combine work that fails independently, and one splits work
that cannot land separately. The rule applied was: **one issue per thing that can
be reviewed, merged, and be wrong on its own.**

### Where one design bullet became two issues

- **Configuration (#10) and target identity (#11).** The design treats these as
  one bullet because `[target] repo` sits in both. They are separate issues
  because they fail differently and can be verified separately: #10 is "where
  settings are read from and how layers resolve", #11 is "which repository is
  this tick operating on". #11 owns deleting the key, so the dependency is
  one-directional and the boundary is clean.
- **Packaging (#8) and the standalone launcher (#17).** The design's section 9
  makes the case that these are *not* the same surface — `${CLAUDE_PLUGIN_ROOT}`
  is substituted into skill content but is absent from a Bash tool call's
  environment. Filing them together would invite a blanket substitution, which is
  the specific error section 9 warns about. Separate issues make the distinction
  structural rather than a paragraph someone skims.

### Where two design bullets became one issue

- **Label provisioning and unknown-label reporting (#13).** The design separates
  them because they fail differently — one loudly, one silently. They are one
  issue because they touch the same code path and the same `LABEL_*` source, and
  because shipping provisioning without the reverse-drift report produces a setup
  flow that creates labels while staying silent about the ones that will swallow
  an item. The report is cheap and belongs with the thing it qualifies.

### Where an issue was created that the design does not list as a bullet

- **#19, the validation ladder.** Section 10 describes it, but section 14 lists
  it as a phase 1 bullet without making it work. It is the release gate, it has a
  real cost, and section 15's publication artifacts depend on L3 specifically. An
  unowned gate is a waived gate.

## Ordering, and what it protects

The dependency graph is not a schedule; it records **which pairs are unsafe to
land alone**. Four couplings matter more than the rest:

1. **#15 (`auto`) before #16 (empty `verify_tools`).** The design argues that
   `auto` is what makes an empty default viable — under `dontAsk` an undeclared
   command is denied and, per `tick.py:736`, "a denial mid-run reads to the engine
   as a broken repo, not a missing grant." Landing #16 first strands the engine on
   every adopter's first dispatch. Landing #15 first is merely a wider permission
   surface with the current grants still in place. The order is the safe one.
2. **#18's internal ordering: the probe precedes accepting an empty
   `verify_tools`.** This is written into the issue as a constraint rather than
   left to implementation order, because the section 7 warning is only survivable
   while an undeclared command can still be approved at run time — which is
   exactly what a route failing the probe takes away.
3. **#21 bundles two changes that the design deliberately couples.** Widening the
   issue-author set and restoring `allow_touching = false` are answers to the same
   condition. Filing them separately would let the widening merge alone, which
   removes a protection at precisely the moment it starts mattering. They are one
   issue so that cannot happen by scheduling accident.
4. **`allow_touching = true` lands with #14, never with #10.** This is the
   ordering that was missed on the first pass, and it is invisible from inside
   either issue. The design ships `allow_touching = true` because operator-only
   pickup means no untrusted author can put code in front of the reviewer — but
   that restriction arrives in #14, while the configuration restructure is #10.
   Flipping the flag in #10 opens the policy-edit gate during an interval in
   which any author's `ai:available` issue is still dispatchable, so a PR built
   from an outsider's issue could edit the very files its reviewer runs under and
   be auto-promoted. The flag now ships `false` in #10 and flips in #14, in the
   same merge as the control that justifies it. #21 still carries the restore.

   The same reasoning applies in miniature to #10 and `status.sh`: #10
   restructures the packaged `config.toml` that `status.sh:44-45` seds
   `[target] repo` out of, while #11 owns rewriting that reader. #10 therefore
   owes a working intermediate state — a retained deprecated `[target]` table, or
   landing with #11 — rather than an intermediate commit where the dashboard
   exits 2 on a working install.

Everything else in the graph is ordinary: read the config before resolving a
target, resolve a target before querying its default branch.

## Labels, and what they mean here

- **`ai:available`** — no unmet dependency and no unanswered question. #7, #10,
  #20.
- **`ai:blocked`** — waiting on a dependency *or a decision*, per
  `pr-guidelines.md`. Everything else.

`ai:blocked` is deliberately doing double duty, and one case is worth calling out:
**#8 is blocked on a decision, not on other work.** Open question 12.4 — the
published skill names, and whether `review-response-hygiene` sits outside the
manifest's `skills` entry — has to be answered before the manifests can be
written, and #9 cannot start until it is. It is the only issue in the set whose
blocker is a conversation rather than a merge, which makes it the one most likely
to be mislabelled as ready.

An earlier pass had all fifteen on `ai:blocked`. That was wrong: it made a genuine
dependency indistinguishable from "not yet triaged", and it would have required
relabelling everything after review anyway.

## What was deliberately not filed

- **Phase 2** — the trusted-input allowlist and engine process isolation. Its
  shape depends on open question 12.2 being *measured* against the installed CLI
  (`--setting-sources`, `--strict-mcp-config`, `--tools` exist; their combined
  runtime behaviour under each permission mode does not follow from the flag
  list). Filing issues now would encode a guess.
- **Section 15 publication** — gated on phase 1 plus L3 specifically, because
  three of its artifacts (a real run capture, a cost figure, the actual escalation
  list) are descriptions of behaviour phase 1 creates. Written earlier they would
  document an intention.
- **L3-adversarial fixtures** — recorded in #19 as known gaps with expected
  outcomes rather than as acceptance criteria. Section 10's own argument: a ladder
  whose stated criteria cannot pass is a ladder an implementer quietly waives.

## Known soft spots in this breakdown

Stated so a reviewer does not have to find them:

- **#18 (setup) is the largest issue** and carries six dependencies. It is
  plausibly two issues — the interactive flow, and the `auto` route probe with its
  explicit failure outcomes. It was left whole because the probe's result
  feeds the flow's decision about `verify_tools`, and splitting them would put
  that ordering constraint across an issue boundary where it is easier to lose.
- **#11 is broad.** Threading one canonical target through dispatch, harvest,
  cadence, mutation, and the dashboard touches many call sites. It resisted
  splitting because a partial thread is the dangerous state: some `gh` calls
  resolving against the working directory while others use the resolved target is
  worse than either end state.
- **#19 cannot be fully automated.** L3 needs real engines, two throwaway target
  repositories, and an issue filed from a second account. Part of it will be a
  documented manual procedure, and pretending otherwise in CI would produce a
  green check that proves nothing.

## Corrections applied after external review

An external review (Codex) read all fifteen issues against the source before any
of them were dispatched. Six corrections were applied to the issue bodies, and
they are recorded here because each one is a case where the issue text was
internally consistent but disagreed with the code:

- **#18's probe was a tautology.** It named `git status` as "one benign command
  deliberately **not** in the allowlist", but `_ENGINE_ALLOWED_TOOLS_BASE`
  (`tick.py:721`) already contains `Bash(git status)`. The probe would have passed
  on a route where `auto` is wholly unsupported, which is the single thing it
  exists to detect. The issue now requires a command proven absent from the
  effective grants, a test that fails if the base grants later widen to include
  it, and a `dontAsk` negative control — a probe that can only pass measures
  nothing.
- **#14 argued that a hazard could not occur, and it can.** Its rationale claimed
  server-side author filtering made the cadence runaway impossible. Cadence is a
  *separate read*: `_queue_page_has_dispatchable` (`tick.py:11986`) issues its own
  unfiltered query and `_target_item_is_dispatchable` (`:12086`) fetches only
  `labels,state`. An implementer trusting that paragraph would have shipped the
  runaway the issue claims to prevent. Both call sites are now in scope, with a
  quiescence test over a full page.
- **#20 re-opened the loop it cites as justification.** Admitting a legal
  unchanged-head completion re-enters a review round of identical identity, whose
  comment the replay token suppresses, freezing the comment-derived iteration
  count below the cap (`pipeline.py:2386-2394`). The issue now owns the
  round-counting change, requires re-review rather than promotion (a fixer must
  not be able to overrule a blocking finding alone), and asserts termination at
  `ai:max-iterations`.
- **#10 contradicted itself** — "no configuration is read from the install
  directory" against a packaged floor that is read from exactly there. Reworded to
  *operator* configuration, and given the `status.sh` continuity constraint above.
- **#11's remote threading stopped at the controller.** `origin` is also hardcoded
  in both push grants (`tick.py:781`, `:828`) and in both engine prompts, so
  updating the fetches alone would grant and instruct a remote that does not
  exist. Prompts are now in file ownership, and the ambiguous cases — no remote,
  several remotes, `PIPELINE_REPO` with no matching remote — have an explicit
  fail-closed contract.
- **#18's per-route promise was not representable.** Three outcomes keyed by
  provider and model, against a schema with one `permission_mode` per repository
  accepting two values. Resolved by shrinking the promise rather than growing the
  schema: the verdict is recorded per repository, and "refuse to dispatch to this
  route" is dropped in favour of changing the engine selection. Route-keyed
  configuration waits for a mixed-provider install that justifies it.

The review also confirmed the breakdown itself: the broad scope of #11 and the
real-engine gate in #19 were both examined and kept.
