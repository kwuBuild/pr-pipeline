"""Executable contract checks for the PR-pipeline skill.

Tests are categorized by verification strategy:

- **Category A — ordering invariants:** mock + assert_has_calls on subprocess
- **Category B — behavioral invariants:** standard unit tests with mocked returns
- **Category C — structural invariants:** subprocess call args ARE the invariant
- **Category D — documentation invariants:** operator doc content checks
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import NamedTuple
from unittest import mock

import pytest

def _repo_root() -> Path:
    """Locate the repository root by walking up to the enclosing ``.git``.

    Never count directory levels from ``__file__``: a fixed ``../../..`` walk
    encodes how deeply the skill happens to sit inside the repo, which differs
    between an in-repo install (``.claude/skills/...``) and this repository's
    own ``skills/...`` layout. A hardcoded depth resolves silently to the wrong
    directory rather than failing, so every doc-contract assertion below would
    read a file that is not the one it names.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"no enclosing git repository above {__file__}")


REPO_ROOT = _repo_root()
SKILL = Path(__file__).parent.parent / "SKILL.md"
README = Path(__file__).parent.parent / "README.md"
DEBUG = Path(__file__).parent.parent / "DEBUG.md"
LIB_README = Path(__file__).parent / "README.md"
PREFLIGHT = Path(__file__).parent.parent / "preflight.sh"
TICK_SH = Path(__file__).parent.parent / "tick.sh"
RESOLVE_SH = Path(__file__).parent.parent / "resolve-python.sh"
STATUS_SKILL = Path(__file__).parent.parent.parent / "status" / "SKILL.md"
SETUP_SKILL = Path(__file__).parent.parent.parent / "setup" / "SKILL.md"
PROMPTS = Path(__file__).parent / "prompts"
STATUS_SCRIPT = Path(__file__).parent.parent.parent / "status" / "status.sh"
DONE_SKILL = (
    Path(__file__).parent.parent.parent / "definition-of-done" / "SKILL.md"
)
DONE_WHY = (
    Path(__file__).parent.parent.parent
    / "definition-of-done"
    / "references"
    / "why.md"
)
POLICY_CONFIG = Path(__file__).parent.parent / "config.toml"
GUIDELINES = REPO_ROOT / "docs/working/pr-guidelines.md"
INTERNALS = REPO_ROOT / "docs/working/pr-pipeline-internals.md"

# --- Plugin packaging ---

SKILLS_ROOT = REPO_ROOT / "skills"
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = REPO_ROOT / ".claude-plugin" / "marketplace.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# The plugin's own name. Claude Code namespaces a plugin's skills by it, so this
# is the prefix on every command the plugin publishes: `/pr-pipeline:tick`.
PLUGIN_NAME = "pr-pipeline"

# Every command this plugin publishes, and nothing else. Asserted as equality
# rather than containment, because `"skills": "./skills"` in the manifest makes
# *any* directory under `skills/` a namespaced, user-invocable command whose
# description loads into every session's context. An unintended command is the
# observable symptom of a misplaced file, and a containment check cannot see it.
_PUBLISHED_SKILLS = frozenset({
    "tick", "status", "setup", "definition-of-done", "review-response-hygiene",
    "automation-control-plane-review", "durability-boundary-review",
    "loop-cadence-contract-review",
})

# How a skill body names the install root. `${CLAUDE_PLUGIN_ROOT}` is
# substituted into plugin skill *content* as Claude loads it, so a command
# written here reaches the agent as an absolute path. It is deliberately not
# used in `README.md`/`DEBUG.md`: those are read as plain text by a human in a
# separate terminal or by an agent via `Read`, nothing substitutes them, and the
# variable is not exported into a Bash tool call's environment — so the same
# string there would silently resolve to a root-relative path.
PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"

# Explicit development-checkout override. Standalone commands use the installed
# resolving launcher; plugin skill bodies use the harness-substituted root.
INSTALL_ROOT_VAR = "$PR_PIPELINE_HOME"

# The scope the credentialed engine prompts must state verbatim. Ambient `gh`
# credentials reach every item in the repository, not just the one the engine was
# dispatched on, so a stage-scoped wording ("on this PR") leaves sibling items
# inside the prompt's allowed scope.
_ENGINE_LABEL_PROHIBITION = (
    "Do not add, remove, or change any `ai:` label on any issue or pull request"
)

# Per-engine clauses that ride alongside the shared prohibition. The implementer
# is also barred from `gh pr create`: the harvest opens the PR from the engine's
# pr-body file and labels it, so an engine that opens its own PR labels it too —
# the same escape the label rule closes, one step earlier.
_ENGINE_EXTRA_CLAUSES: dict[str, list[str]] = {
    "implementer": ["Do not run `gh pr create`"],
    "fixer": [],
}

# Placeholder values that render each credentialed prompt through the production
# loader. Only the label-authority clause is asserted, so the values need to be
# well-formed, not realistic.
_PROMPT_RENDER_VARIABLES: dict[str, dict[str, object]] = {
    "implementer": {
        "target_remote": "upstream",
        "verification_context": "Operator grants are configured.",
        "issue_number": 1,
        "issue_title": "contract render",
        "issue_body": "body",
        "branch_name": "worktree-i1",
        "pr_body_source_file": "pr-body.md",
        "definition_of_done_skill_doc": "some/skill/SKILL.md",
        "definition_of_done_skill_invocation": "pr-pipeline:definition-of-done",
    },
    "fixer": {
        "target_remote": "upstream",
        "verification_context": "Operator grants are configured.",
        "pr_number": 2,
        "review_body": "findings",
        "finding_references": "Controller finding snapshot.",
        "pr_body": "## Summary",
        "issue_contract": "### Issue example/repo#1 — contract render",
        "discussion_file": "/fixture/discussion.json",
        "head_ref": "worktree-i1",
        "revision_hygiene_skill_doc": "some/skill/SKILL.md",
        "revision_hygiene_skill_invocation": "pr-pipeline:review-response-hygiene",
        "summary_budget": 1,
        "pr_body_revision_file": "pr-body-revision.md",
        "pr_body_revision_stamp": "pr-body-revision: fix-2-1",
        "pr_body_revision_head_stamp": "pr-body-revision-head:",
    },
}

# The carve-out's first and last words in both root agent docs. Assertions are
# scoped between them because the clauses they pin — `gh pr create`, label
# commands — also appear elsewhere in the same files as *instructions to follow*.
# A whole-file substring check would pass on those occurrences and prove nothing.
_CARVE_OUT_OPENER = "If you were dispatched by `pr-pipeline:tick`"
_CARVE_OUT_CLOSER = "#pipeline-label-authority)"

# The scope the publish-before-relabel gate actually holds on: a transition that
# reports an engine outcome, or one that fires before any engine was launched.
# What the two clauses leave out is the middle — an engine was launched and the
# tick cannot read a result from it — which is the whole of what stays silent.
_ORDERING_SCOPE = "reports an engine result, or fires before one was launched"

# The tick's entire publishing surface. Only these two functions may run
# `gh <entity> comment`: the reviewer harvest posts its verdict directly and
# gates the relabel on it, and everything else goes through the receipted helper
# under one of the kinds below. Every other route to a terminal label is silent.
_PUBLISH_HELPER = "_post_harvest_comment"
_COMMENT_POSTING_FUNCTIONS = frozenset({"_harvest_one_reviewer", _PUBLISH_HELPER})
_PUBLISHED_COMMENT_KINDS = frozenset({
    "_FIX_COMMENT_KIND",  # implementer escalate, fixer recheck/escalate
    "_CAP_COMMENT_KIND",  # attempt cap
    "_ESCALATION_COMMENT_KIND",  # transient exhaustion, counter-write failure
    "_DISPATCH_COMMENT_KIND",  # every pre-launch dispatch escalation
})

# The helpers that move an item's pipeline label. `relabel` is the raw edit,
# `_relabel_with_context` adds the terminal notification, and
# `_converge_pipeline_labels` reconciles a whole label set — a census that read
# only one of them would miss escalations spelled with the others.
_RELABEL_HELPERS = frozenset({
    "relabel", "_relabel_with_context", "_converge_pipeline_labels",
})

# Every function that can move an item to `ai:needs-human-decision`, and how many
# such relabel sites it owns. This is the census the publish-before-relabel rule
# is argued against, so it is pinned rather than described: a new route to the
# hold label is a decision about whether that item's red label explains itself,
# and the way to force the decision is to fail here until someone records it.
#
# "Can" is load-bearing. `needs_human_relabel_sites` fails closed, so a site that
# passes a *computed* label counts even when it usually resolves to something
# else. Recognising only `pipeline.LABEL_NEEDS_HUMAN` would have missed the two
# reconcile paths outright — one passes the bare string, the other a variable
# proven equal by an enclosing guard — and those are exactly the shape a new
# silent escalation gets written in. A false positive costs one line here; a
# false negative is the accumulating silence this census exists to stop.
#
# Entries divide into sites that provably name the hold label and sites that
# pass a computed one. Only the first group is an escalation; the second is the
# cost of failing closed, and is grouped separately so a count change is read
# against the right expectation.
#
# The dispatch phase is the half this pins hardest. Every *pre-launch* escalation
# is funnelled through `_escalate_dispatch_setup_failure` or `escalate_ambiguous`,
# both of which publish first; the one *provable* site left in each
# `_dispatch_one_*` is the post-launch guard, which fires when the in-flight
# relabel failed and the engine could not be killed. That one stays silent by
# design — a live engine still owns the marker, and its harvest owns the
# explanation — so a second provable site there means a new silent pre-launch
# path.
_NEEDS_HUMAN_TRANSITIONS = {
    # --- Provably the hold label ---
    # Reconcile — no engine result the tick can read. Both silent, and both
    # invisible to an attribute-only census: `_reconcile_one` passes the bare
    # string, `_execute_reconcile_actions` a guard-proven variable (the latter
    # counts twice: the guarded escalate/kill_escalate branch and its revert).
    "_reconcile_one": 1,
    "_execute_reconcile_actions": 2,
    # Harvest — an engine result exists, and `.harvest-evidence/` holds it.
    "_harvest_failed_reviewer": 1,      # silent; belongs with #618
    "_handle_transient_engine_result": 2,  # transient: publishes, gated+bounded
    # Dispatch, pre-launch — no engine ran; both publish before the label moves.
    "_escalate_dispatch_setup_failure": 1,
    "escalate_ambiguous": 1,
    # Dispatch, post-launch — the in-flight relabel failed *and* the kill failed.
    "_dispatch_one_reviewer": 1,
    "_dispatch_one_implementer": 1,
    "_dispatch_one_fixer": 1,
    # --- Computed label: counted because the walk cannot rule the hold out ---
    # The helper every other site relabels through; its `to_label` is its
    # caller's.
    "_relabel_with_context": 2,
    # The one remote mutation every terminal transition — the attempt cap,
    # transient exhaustion, and the fixer's `escalate` — is landed through, on a
    # `target_label` its caller computed. Each caller publishes before it lands,
    # except the cap, whose post is the documented best-effort one.
    "_land_terminal_transition": 1,
    # Terminal harvest transitions, normally `ai:reviewed` / `ai:revised` / a
    # pre-label revert. `_harvest_one_implementer` owns none: its only converge
    # passes `""`, which the walk can prove is not the hold label.
    "_harvest_one_reviewer": 1,
    "_harvest_one_fixer": 1,
    # A revert to the pre-dispatch label after a confirmed-dead engine — the
    # opposite of an escalation.
    "_publish_marker_or_revert": 1,
}

# The rule each ordering surface must state, and the shape it must state it in.
# The silent set is defined as the *complement* of the publishing set, never
# enumerated: enumerating it is what failed three times running — first counted
# as four transitions, then as six, then as a hand-named list of four that was
# still short by the reviewer wrapper-failure and reconcile escalation families.
# The publishing set is a handful of constants the source itself declares, so
# stating the rule this way round closes it by construction.
_SILENT_SET_RULE = "every other terminal transition publishes nothing"

# The ordering surfaces spell the publishing bound out in words rather than
# digits, so the AST-derived size is mapped through this rather than formatted.
# A count with no entry here fails loudly: widening the publishing surface is
# the moment every document that states the bound has to be revisited.
_CARDINALITY_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}

class _OrderingSurface(NamedTuple):
    """One document that states the publication-ordering rule.

    Attributes:
        name: Display name used in assertion messages.
        path: The document.
        section_start: Raw-text literal that opens its publication-ordering
            section. The member check reads that section rather than the whole
            file — `_harvest_one_reviewer` and `_post_harvest_comment` are named
            elsewhere in these documents for unrelated reasons, and a whole-file
            scan reads those mentions as the ordering surface naming its
            publishing set.
        section_end: Raw-text literal that closes it.
        names_members: Whether the section names the posting functions or only
            states their count. Declared rather than inferred: inferring it from
            what a surface happens to name is what let a rename of *every*
            posting function pass while all three documents kept the old names.
    """

    name: str
    path: Path
    section_start: str
    section_end: str
    names_members: bool


# The documents that state the publication-ordering rule, and where each states
# it. Each surface stated the rule independently, which is how they drifted
# apart: correcting one leaves the other two teaching the superseded version.
# They live in one table so the whole-file checks and the section slice cannot
# disagree about which documents are ordering surfaces. The operator README
# states the bound without symbol names on purpose — its reader is an operator,
# not someone reading `tick.py`.
_ORDERING_SURFACES: tuple[_OrderingSurface, ...] = (
    _OrderingSurface(
        name="README.md",
        path=README,
        section_start="### Escalation publication coverage",
        section_end="observability surfaces",
        names_members=True,
    ),
    _OrderingSurface(
        name="pr-pipeline-internals.md",
        path=INTERNALS,
        section_start="Publication ordering:",
        section_end="Receipt custody:",
        names_members=True,
    ),
)


# --- Category D — documentation invariants ---


def test_internals_source_references_use_existing_symbols() -> None:
    """Runtime citations survive line insertions and still identify real owners."""
    text = INTERNALS.read_text()
    assert not re.search(r"#L\d|\b(?:\w+\.py|reviewer\.txt|design):\d", text), (
        "cite a file and symbol instead of maintaining source line numbers"
    )
    targets = dict(re.findall(r"^\[([^\]]+)\]: (\S+)", text, re.MULTILINE))
    for target in targets.values():
        if "://" not in target:
            assert (INTERNALS.parent / target.split("#", 1)[0]).is_file(), target

    citations = re.findall(r"\[`(\w+)\.(\w+)`\]\[([^\]]+)\]", text)
    assert citations, "internals must retain searchable source owners"
    symbols_by_path: dict[Path, set[str]] = {}
    for module, symbol, ref in citations:
        source = (INTERNALS.parent / targets[ref]).resolve()
        assert source.stem == module, (module, source)
        if source not in symbols_by_path:
            names: set[str] = set()
            for node in ast.parse(source.read_text()).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    assigned = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names.update(n.id for n in assigned if isinstance(n, ast.Name))
            symbols_by_path[source] = names
        assert symbol in symbols_by_path[source], (source, symbol)


def test_skill_md_references_tick_py_invocation() -> None:
    """SKILL.md must reference the new Python orchestrator entry point."""
    text = SKILL.read_text()
    assert f"bash {PLUGIN_ROOT_VAR}/skills/tick/tick.sh" in text


def test_no_operator_surface_runs_the_pipeline_through_uv() -> None:
    """Running the pipeline must not require a package manager nothing declares.

    The runtime contract is stdlib-only on Python >= 3.11, and CI proves it on a
    bare matrix — then a doc that says ``uv run python`` tells the operator to
    install a package manager to satisfy a contract that has no dependencies.
    The indirection was not reliably correct either: ``uv run --python 3.13``
    has been observed resolving an interpreter without ``tomllib``.

    This is about running the *pipeline's own code*. ``uv run pytest`` is a
    contributor's test command, not an interpreter invocation, and ``[engine]
    verify_tools`` may name uv because that is the *target* repo's toolchain,
    not this one's — neither is in scope here.

    Scope is deliberately "lines someone copies and runs": command lines inside
    fenced code blocks, and non-comment lines of the shell scripts. Prose that
    names the old form to say it was removed is the documentation working, not
    failing. The cost is that a regression re-introduced as inline code inside a
    sentence would not be caught; a doc that wants an operator to run something
    puts it in a code block, so that is the surface worth policing.
    """
    # Every surface that shows someone how to run this pipeline, not just the
    # install-portable subset: `architecture.md` carries a runnable engine
    # command, and the launcher scripts are read as documentation of themselves.
    surfaces = (
        *_INSTALL_PORTABLE_DOCS,
        REPO_ROOT / "docs/architecture.md",
        TICK_SH,
        RESOLVE_SH,
    )
    # `uv run python` is the exact literal that was documented, but the defect
    # is the indirection, not the spelling: `uv run --python 3.13 python …` and
    # `uv run --with … python …` reintroduce it while dodging a fixed string.
    # Anything running an interpreter through uv is what this forbids; `uv run
    # pytest` and friends are not interpreter invocations and stay legal.
    offender = re.compile(r"\buv\s+run\b[^\n`'\"]*?\bpython\b")
    violations: list[str] = []
    for surface in surfaces:
        for number, line in enumerate(runnable_lines(surface), start=1):
            if line is not None and offender.search(line):
                violations.append(f"{surface.name}:{number}: {line.strip()!r}")

    assert not violations, (
        "operator surfaces run the pipeline through uv; resolve an interpreter "
        "directly instead (tick.sh, or resolve-python.sh):\n"
        + "\n".join(violations)
    )


def test_skill_md_is_lean_operator_sop() -> None:
    """SKILL.md must stay lean because it is injected on every skill invocation."""
    text = SKILL.read_text()
    assert len(text.splitlines()) <= 80
    for heading in [
        "## Run Procedure",
        "## Outcome Check",
        "## Exit Codes",
        "## Stop or Escalate",
        "## Running Under /loop",
    ]:
        assert heading in text, f"SOP heading not found: {heading}"


def test_skill_md_defaults_unattended_runs_to_background_native_loop() -> None:
    """Unattended runs must default to the in-process loop, not model-paced /loop.

    The native ``--loop`` makes no model calls between ticks and wakes on engine
    completion. ``/loop`` spends a model turn per tick, so the SOP must present
    it only as the opt-in for per-tick reports.
    """
    text = SKILL.read_text()
    procedure = text.split("## Run Procedure")[1].split("## Outcome Check")[0]
    loop_section = text.split("## Running Under /loop")[1]

    assert "run_in_background: true" in procedure
    assert f"bash {PLUGIN_ROOT_VAR}/skills/tick/tick.sh --loop" in procedure
    assert "no model calls" in procedure
    assert "Optional" in loop_section.split("Paste this prompt into /loop")[0]


def test_skill_md_loops_keep_the_operators_target_and_flags() -> None:
    """Both loop paths must carry the operator's target and flags forward.

    ``parse_cli_args`` starts every process with no target and no overrides,
    so a loop launched without them silently widens ``pr:N`` to the whole
    queue and drops ``--permission-mode``, ``--model``, and engine choices.
    """
    text = SKILL.read_text()
    procedure = text.split("## Run Procedure")[1].split("## Outcome Check")[0]
    loop_intro = text.split("## Running Under /loop")[1].split(
        "Paste this prompt into /loop"
    )[0]

    assert "same target and flags" in procedure
    assert "append the same target and flags" in loop_intro


def test_skill_md_loop_section_has_paste_ready_iteration_prompt() -> None:
    """Loop supervision must schedule the compact tick contract, not reinvoke the skill."""
    text = SKILL.read_text()
    assert "Paste this prompt into /loop" in text
    assert f"Do not invoke /{PLUGIN_NAME}:tick" in text
    assert "dangerouslyDisableSandbox: true" in text
    assert f"bash {PLUGIN_ROOT_VAR}/skills/tick/tick.sh" in text
    assert ".claude/worktrees/.tick-log" in text
    assert "tick-cadence:" in text
    assert "ScheduleWakeup" in text
    assert "stop=true" in text
    assert "notify the developer" in text
    assert "Stop and notify the developer if `corrupt_marker_escalate` appears." in text


def test_skill_md_cross_links_to_debug_md() -> None:
    """SOP must point operators to DEBUG.md by section name for key references."""
    text = SKILL.read_text()
    assert "DEBUG.md" in text
    assert 'DEBUG.md "Startup failure diagnostics"' in text or \
           'DEBUG.md "Debugging pointers"' in text
    assert 'DEBUG.md' in text.split("dangerouslyDisableSandbox")[0] or \
           'DEBUG.md' in text.split("dangerouslyDisableSandbox")[1]


def test_debug_md_retains_the_invariants_moved_out_of_the_sop() -> None:
    """DEBUG.md must still carry every invariant the lean SOP handed it.

    This is a floor, not a total: DEBUG.md has grown invariants of its own since
    the split, and asserting a count would make each new one a test edit. What
    must not happen is one of the migrated invariants disappearing when the SOP
    no longer states it either.
    """
    text = DEBUG.read_text()
    invariants = [
        "HARVEST then DISPATCH",
        "Fail closed on every external read",
        "Lease-before-label",
        "Kill only on proven identity",
        "can_dispatch_now fails CLOSED",
        "Scratch deletion gated on label edit",
        "Admission counts this machine's engines",
        "gh` exits 0 on empty results",
    ]
    for inv in invariants:
        assert inv in text, f"Invariant not found: {inv}"


# Every doc that teaches how occupancy is counted. A reader reaches the design
# docs and the internals note from the same question the operator docs answer,
# so a retired ceiling surviving in any one of them is a second contract.
_ADMISSION_DOCS = (
    DEBUG, SKILL, README, LIB_README, INTERNALS,
)


def test_no_operator_doc_still_claims_the_label_union_ceiling() -> None:
    """The retired union contract must not survive in operator-facing docs.

    The floor test above is satisfied by *presence*, so it cannot notice a
    replaced invariant still being asserted a few lines down, or in a sibling
    doc an operator reads first. Admission has exactly one contract now; this
    is the assertion that fails if a second one grows back.
    """
    for doc in _ADMISSION_DOCS:
        text = doc.read_text()
        assert "Authoritative in-flight labels count globally" not in text, doc
        # Admission-specific: HARVEST legitimately unions markers with the
        # label query to build its reconcile set, so the bare verb is not the
        # thing being banned here — pairing it with the label set is.
        assert "unions local markers with authoritative labels" not in text, doc
        assert "local-markers ∪ authoritative-labels" not in text, doc


def test_no_doc_pairs_a_union_with_the_in_flight_count() -> None:
    """No doc may describe in-flight occupancy as a union outside HARVEST.

    The assertion above bans three exact strings, so it only catches wordings
    someone already thought to ban — and the retired ceiling once survived a
    full sweep precisely because an end-to-end walkthrough phrased it
    differently. This asserts the shape instead: a
    paragraph pairing a union with the in-flight count is describing
    admission, and admission does not union anything any more.

    HARVEST is the one place a union is still real — it builds its reconcile
    set from the label query *and* local markers — so naming ``reconcile`` or
    ``HARVEST`` in the same paragraph is the carve-out, and the only one.
    """
    for doc in _ADMISSION_DOCS:
        for paragraph in doc.read_text().split("\n\n"):
            lowered = paragraph.lower()
            if "union" not in lowered and "∪" not in lowered:
                continue
            if "in-flight" not in lowered:
                continue
            assert "reconcile" in lowered or "harvest" in lowered, (
                f"{doc} describes in-flight occupancy as a union outside "
                f"HARVEST's reconcile set:\n{paragraph}"
            )


def test_every_admission_doc_credits_the_marker_with_the_slot() -> None:
    """Each doc that teaches occupancy must positively name what pays.

    The guard below is a blocklist in shape: it fails on a wrong sentence
    someone wrote. It cannot fail on a *missing* right one, so a sweep that
    deletes the contradiction and stops there leaves a doc that teaches
    occupancy while naming no payer at all — and the next author fills that
    silence with the label, which is how the retired ceiling grew back into a
    walkthrough after the first sweep.

    This is the mirror assertion, built from the same primitives so the two
    cannot drift: somewhere in every admission doc, a marker must be the
    subject of a slot the pipeline spends.
    """
    missing: list[str] = []
    for doc in _ADMISSION_DOCS:
        credited = False
        for paragraph in doc.read_text().split("\n\n"):
            flat = " ".join(paragraph.split()).lower()
            for sentence in re.split(r"(?<=[.;])\s+", flat):
                positive = sentence.replace("markerless", "").replace(
                    "no marker", ""
                )
                if "marker" not in positive:
                    continue
                if not any(term in sentence for term in _SLOT_TERMS):
                    continue
                if not any(verb in sentence for verb in _CONSUMPTION_VERBS):
                    continue
                credited = True
                break
            if credited:
                break
        if not credited:
            missing.append(str(doc.resolve().relative_to(REPO_ROOT)))

    assert not missing, (
        "these docs teach occupancy without ever naming the dispatch marker "
        "as the thing that holds a slot:\n\n" + "\n".join(missing)
    )


# Every surface a reader can learn the occupancy rule from. The two guards above
# read docs only, but a docstring and a comment teach the contract to the next
# implementer just as hard — and `pr-guidelines.md` teaches it to contributors,
# who are the ones hand-setting the labels this cap must not count.
_SLOT_ATTRIBUTION_SURFACES = _ADMISSION_DOCS + (
    GUIDELINES,
    Path(__file__).parent / "tick.py",
    Path(__file__).parent / "pipeline.py",
)

# A label being named as the thing that holds, costs, or frees a slot.
_LABEL_TERMS = (
    "in-flight label", "in-flight labels", "in-flight read", "in-flight reads",
    "ai:implementing", "ai:reviewing", "ai:fixing", "authoritative label",
    "authoritative labels", "pipeline label", "dispatch label",
    # The act of moving a label is the same attribution without the noun:
    # "relabel when the queue needs the slot" evaded the first version of this
    # guard entirely because no listed label *noun* appeared in the sentence.
    "relabel",
)
_SLOT_TERMS = (
    "admission slot", "concurrency slot", "dispatch slot", "admission account",
    "max_in_flight", "admission count", "against the cap", "spend cap",
    # Bare references. Prose reaches for these once the named term has been
    # established a sentence earlier, which is exactly where the attribution
    # stops being visible to a term list built from the named forms.
    "the slot", "a slot",
)
_CONSUMPTION_VERBS = (
    "count", "consum", "burn", "pin", "free", "occup", "participat", "spend",
    # "cost" and "release" are the two verbs the live violations actually
    # used; "need" is how the README asked for a slot without naming a payer.
    "cost", "releas", "need",
)

# Clause separators. A sentence can name the marker in one clause and still
# hand the slot to a label in the next, which is how DEBUG.md's held-slot
# sentence passed a carve-out that scanned the whole sentence for "marker".
_CLAUSE_SPLIT = re.compile(r"\s*(?:—|,\s+(?:and|so|but|which|then)\s+)\s*")

# Wordings that deny the attribution rather than assert it.
_CLAIM_NEGATIONS = (
    "never", "no longer", "cannot", "does not", "do not", "nothing",
)


def _attributes_slot_to_label(fragment: str) -> bool:
    """Return whether one sentence or clause hands a slot to a label.

    Naming the marker is the carve-out, but only a *positive* mention:
    ``markerless`` and ``no marker`` name its absence, which is exactly the
    state these passages wrongly say costs a slot.

    Args:
        fragment: A lowercased sentence or clause.

    Returns:
        ``True`` when the fragment names a label, names a slot or the cap, and
        joins them with a verb of consumption without crediting the marker.
    """
    positive = fragment.replace("markerless", "").replace("no marker", "")
    if "marker" in positive:
        return False
    # Denying the attribution is the other way a passage can be correct, and
    # the contract's own statement of itself takes that form ("a shared `ai:`
    # in-flight label never occupies a slot"). Known blind spot: a wrong
    # sentence that negates some *other* clause is excused too. The positive
    # section binding below, not this tripwire, is what pins the real claim.
    if any(neg in fragment for neg in _CLAIM_NEGATIONS):
        return False
    return (
        any(term in fragment for term in _LABEL_TERMS)
        and any(term in fragment for term in _SLOT_TERMS)
        and any(verb in fragment for verb in _CONSUMPTION_VERBS)
    )


def test_no_surface_attributes_an_admission_slot_to_a_label() -> None:
    """No doc, docstring, or comment may make a label the thing that pays.

    The two guards above match on *wording* — three exact strings, or the
    literal token ``union``. Codex's re-review found nine live passages that
    teach the retired ceiling without using any of them, in files one of those
    guards does not even open. This asserts the *claim* instead: a passage that
    names a label, names a slot or the cap, and joins them with a verb of
    consumption is asserting the thing this pipeline stopped doing.

    The carve-out is deliberately a positive obligation rather than an
    exemption list: a sentence that puts a label next to a slot must name the
    marker, because the marker is what pays. That cannot be satisfied by
    rewording around a banned phrase — which is exactly how a walkthrough
    survived the last sweep. It is scoped
    to the sentence rather than the paragraph so a correct neighbouring
    sentence cannot vouch for a wrong one.
    """
    violations: list[str] = []
    for surface in _SLOT_ATTRIBUTION_SURFACES:
        for paragraph in surface.read_text().split("\n\n"):
            # Normalise before splitting into sentences: a wrapped docstring or
            # comment puts "in-flight" and "label" on different source lines,
            # and splitting on the newline hides the claim from every term
            # below. This is why the tick.py docstrings survived the last pass.
            flat = " ".join(paragraph.split())
            # Not split on ":" — a colon joins the claim to the attribution
            # that qualifies it, and severing them hides the correction.
            for sentence in re.split(r"(?<=[.;])\s+", flat):
                lowered = sentence.lower()
                # Both scopes, because neither subsumes the other: a sentence
                # that splits the label from the slot across clauses is only
                # visible whole, and a sentence that credits the marker in one
                # clause while handing the slot to a label in the next is only
                # visible per clause.
                if not (
                    _attributes_slot_to_label(lowered)
                    or any(
                        _attributes_slot_to_label(clause)
                        for clause in _CLAUSE_SPLIT.split(lowered)
                    )
                ):
                    continue
                rel = surface.resolve().relative_to(REPO_ROOT)
                violations.append(f"{rel}: {sentence[:220]}")

    assert not violations, (
        "these passages make an `ai:` label the thing that holds or frees an "
        "admission slot; the cap counts local dispatch markers:\n\n"
        + "\n\n".join(violations)
    )


def test_debug_md_has_required_sections() -> None:
    """DEBUG.md must have all the debugging/recovery sections."""
    text = DEBUG.read_text()
    for heading in [
        "## Operational invariants",
        "## Why `dangerouslyDisableSandbox: true`",
        "## Reviewer sandbox posture",
        "## Startup failure diagnostics",
        "## Debugging pointers",
        "## Constants and boundaries",
    ]:
        assert heading in text, f"DEBUG.md heading not found: {heading}"


def test_skill_md_references_preflight() -> None:
    """SKILL.md must still require preflight.sh before first reviewer dispatch."""
    text = SKILL.read_text()
    assert "preflight.sh" in text


def test_readme_documents_transient_engine_result_contract() -> None:
    """Operator docs must expose quota retry state, bounds, and audit actions."""
    text = README.read_text()
    for contract_text in [
        "### Transient quota and limit results",
        ".engine-cooldowns/",
        "`transient_revert`",
        "`cooldown_skip`",
        "`transient_exhausted`",
        "`MAX_DISPATCH_ATTEMPTS = 3`",
        ".harvest-evidence/",
    ]:
        assert contract_text in text, f"Transient-result contract missing: {contract_text}"


@pytest.mark.parametrize("prompt_name", ["implementer", "fixer"])
def test_credentialed_engine_prompts_forbid_pipeline_label_mutation(
    prompt_name: str,
) -> None:
    """Engines that inherit the operator's `gh` credentials must be told the
    pipeline owns every ``ai:`` label transition, on every work item.

    The implementer and fixer run under the full parent environment, so they can
    reach ``gh pr edit`` and set a pipeline label on *any* work item, not only
    their own. A label set mid-run is an un-retractable opinion published into a
    channel with final-state semantics: reconcile reads it as a hold and
    escalates over the engine's own completion sentinel. The result envelope is
    the only outcome channel these prompts may use.

    The assertion runs against the rendered production prompt — the exact string
    handed to the engine — rather than the template file, and it pins the full
    scope sentence. A per-stage narrowing ("on this PR") would satisfy a
    prefix-only check while leaving sibling items inside the allowed scope.
    """
    import tick  # type: ignore[import-untyped]

    text: str = tick.load_prompt_template(
        prompt_name, _PROMPT_RENDER_VARIABLES[prompt_name],
    )
    for contract_text in [
        _ENGINE_LABEL_PROHIBITION,
        "completion sentinel",
        *_ENGINE_EXTRA_CLAUSES[prompt_name],
    ]:
        assert contract_text in text, (
            f"rendered {prompt_name} prompt is missing the label-ownership "
            f"contract: {contract_text}"
        )


def test_the_fixer_binds_a_skill_that_exists_and_a_tool_the_engine_carries() -> None:
    """The binding must be reachable at both ends, or dispatch never runs.

    Two ways this becomes a no-op that no other test would notice. If the doc
    the constant names is renamed or deleted, the dispatch-time probe escalates
    every fixer instead of launching one — the fail-closed behaviour is correct
    and the pipeline is still stopped. And if the prompt asked the engine to
    *invoke* a skill, the bounded allowlist would deny it: engines get ``Read``
    and no ``Skill`` tool, which is why the binding is a doc path rather than a
    tool call. Both ends are pinned here so neither can drift silently.

    The doc is the install's, so this asserts it ships under the published
    ``skills/`` root rather than anywhere a checkout happens to carry it.
    """
    import tick  # type: ignore[import-untyped]

    doc = Path(tick.skill_doc_path(tick.REVISION_HYGIENE_SKILL))
    assert doc.parent.parent == SKILLS_ROOT, (
        f"the fixer binds {doc}, which is not under the install's skills root"
    )
    assert doc.is_file(), (
        f"the fixer prompt binds {doc}, which does not exist — every fixer "
        f"dispatch escalates on the pre-launch skill probe until it does"
    )
    assert "Read" in tick._ENGINE_ALLOWED_TOOLS_BASE.split(), (
        "the engine allowlist no longer carries Read, so a dispatched fixer "
        "cannot open the skill doc its prompt names"
    )


def test_no_dispatched_engine_can_reach_gh() -> None:
    """GitHub mutation stays on the controller, for every engine and stage.

    The fixer can now correct a stale PR body, and the reason it does so through
    a file the harvest validates — rather than through ``gh pr edit`` of its own
    — is this boundary. An engine-side write has no validation gate, no receipt,
    and no way to be retracted if the run it describes then fails, so widening
    the allowlist would silently undo the controller-owned design the write path
    was built as.
    """
    import tick  # type: ignore[import-untyped]

    allowlist = tick._engine_allowed_tools(
        tick._fixer_push_permission("agent/issue-1"),
    )
    assert "gh" not in allowlist.replace("(", " ").replace(")", " ").split(), (
        f"a dispatched engine can now run gh: {allowlist}"
    )


def test_engine_authored_pr_metadata_files_are_gitignored() -> None:
    """Both prompts say "do not commit"; the repo has to mean it.

    Engines run with ``Bash(git add *)``, so one ``git add -A`` puts the file in
    the PR head. For the fixer that is not cosmetic: its worktree is a detached
    checkout of that head, so a committed revision file arrives before the
    engine writes anything and would read as a candidate replacement body. The
    dispatch stamp catches it, but the ignore rule is what stops it happening.
    Pinned against the constants so a rename cannot silently drop the rule.
    """
    import tick  # type: ignore[import-untyped]

    ignored = (REPO_ROOT / ".gitignore").read_text().splitlines()
    for name in (tick._PR_BODY_SOURCE_FILE, tick._PR_BODY_REVISION_FILE):
        assert name in ignored, (
            f"{name} is not gitignored, so an engine's `git add -A` can commit "
            "the metadata file its own prompt forbids committing"
        )


def test_operator_grep_lists_carry_the_unresolved_skill_event() -> None:
    """A binding that fails closed must be findable in the log it writes to.

    ``skill_unresolved`` is the only entry that distinguishes "the prompt names
    a doc this worktree does not have" from the generic setup-failure escalate
    that follows it. Absent from the grep list, an operator sees a PR sitting on
    ``ai:needs-human-decision`` with no stated cause.

    The loop prompt is asserted separately from the fast-check command: they are
    two different reader paths, and past drift landed in exactly one of them.
    """
    text = SKILL.read_text()
    fast_check, loop_prompt = text.split("Paste this prompt into /loop")

    assert "skill_unresolved" in fast_check, (
        "the Outcome Check grep list does not surface skill_unresolved"
    )
    assert "skill_unresolved" in loop_prompt, (
        "the /loop iteration prompt does not tell the supervisor to grep for "
        "skill_unresolved"
    )


def test_reviewer_prompt_keeps_its_no_credentials_label_boundary() -> None:
    """The reviewer needs no label-ownership clause because it cannot mutate labels.

    ``_dispatch_one_reviewer`` builds a scrubbed allowlist env with no GitHub
    token and launches codex under ``-s read-only``, so the reviewer is
    structurally incapable of editing labels. This pins the standing instruction
    that carries that boundary into the prompt, so the engine-type asymmetry
    stays deliberate rather than becoming an oversight.
    """
    text = (PROMPTS / "reviewer.txt").read_text()
    assert "do not attempt gh commands" in text


def test_operator_docs_define_dispatched_engine_label_authority() -> None:
    """Operator docs must name who may move a pipeline label, and who may not.

    The engine-facing contract lives in the repo docs that dispatched engines
    read alongside their prompt. Without the dispatched-vs-interactive split,
    an engine follows the interactive-agent label workflow and mutates labels
    the tick owns.
    """
    guidelines = GUIDELINES.read_text()
    for contract_text in [
        "## Pipeline label authority",
        "dispatched engine",
        "completion sentinel",
    ]:
        assert contract_text in guidelines, (
            f"pr-guidelines.md is missing the label-authority contract: "
            f"{contract_text}"
        )


def test_operator_docs_preserve_the_human_emergency_hold_override() -> None:
    """The authority contract must keep a human's mid-run hold legitimate.

    Dispatch skips a candidate carrying a hold before it spends anything, and
    ``_reconcile_one`` lets a hold outrank a completed marker. #618 keeps that
    human-set route deliberately alive — it only removes the silence around it.
    A contract that granted humans authority solely over marker-free items would
    tell an operator they may not stop a bad active run while the live control
    plane treats exactly that intervention as authoritative.
    """
    guidelines = GUIDELINES.read_text()
    for contract_text in [
        "A human may stop a dispatched run at any time",
        "`ai:needs-human-decision` or `ai:blocked`",
        "the hold wins",
    ]:
        assert contract_text in guidelines, (
            f"pr-guidelines.md dropped the human emergency-hold override: "
            f"{contract_text}"
        )


def test_state_machine_treats_hold_provenance_as_unprovable() -> None:
    """Neither state-machine module may claim a hold beside custody *is* human-set.

    ``pipeline.py`` owns the hold-label set and ``tick.py`` acts on it in
    ``_reconcile_one`` and ``_resolve_dispatchable_pickup_label``. Engines share
    the tick's GitHub token, so a `labeled` timeline event names the same account
    either way and no provenance signal exists. Leaving either module asserting
    proven provenance invites a future recovery change to branch on it.
    """
    pipeline_source = (Path(__file__).parent / "pipeline.py").read_text()
    tick_source = (Path(__file__).parent / "tick.py").read_text()
    surfaces = (
        ("pipeline.py", pipeline_source),
        ("tick.py", tick_source),
    )
    for name, text in surfaces:
        assert "treated as human-set" in text, (
            f"{name} still asserts proven hold provenance"
        )
        assert "fail closed toward the human" in text, (
            f"{name} does not state the fail-closed trade"
        )


def test_publication_ordering_docs_state_the_scope_the_gate_holds_on() -> None:
    """The publish-before-relabel rule must state the scope it actually holds on.

    The gate governs transitions that report an *engine result*, and those that
    fire *before an engine was launched*. It does not govern the middle — an
    engine launched, no result the tick can read — where the relabel still goes
    through ``_relabel_with_context`` and posts nothing. Stating the rule
    unscoped turned a true invariant into a false one, and an unscoped claim is
    what let the silent paths go undocumented, so each surface must carry the
    scoping phrase rather than an unqualified "every terminal transition".

    The scope widens when a family of silent transitions is closed, which is a
    moment all three surfaces have to be rewritten together — the failure this
    check exists to force.
    """
    for name, text in ordering_surfaces():
        assert _ORDERING_SCOPE in text, (
            f"{name} states the publication gate without the scope it holds on"
        )


def test_publication_ordering_docs_define_the_silent_set_by_complement() -> None:
    """The docs must bound what publishes, and call everything else silent.

    Enumerating the silent transitions failed three times: counted as four, then
    as six, then hand-named as four — each time an undercount, because the set is
    large, spread across reconcile, harvest, and dispatch, and maintained by hand
    in three documents at once. The publishing set is a handful of comment kinds
    behind one helper, so the docs state that side and define the silent set as
    its complement. Then a new silent path cannot make the docs wrong; only a new
    *publishing* path can, and
    :func:`test_tick_publishes_only_the_documented_comment_kinds` fails when one
    appears.

    ``forced transitions`` — the undefined term the original counts were
    expressed in — must not come back.
    """
    for name, text in ordering_surfaces():
        assert _SILENT_SET_RULE in text, (
            f"{name} does not define the silent set as the complement of the "
            f"publishing set"
        )
        assert "forced transitions" not in text, (
            f"{name} reintroduced a hand-counted total"
        )


def test_tick_publishes_from_only_two_places() -> None:
    """``gh <entity> comment`` may only be reached from the two documented sites.

    This is the executable half of
    :func:`test_publication_ordering_docs_define_the_silent_set_by_complement`.
    The docs say "these publish, everything else is silent", and that claim is
    only as good as the bound on what publishes. Asserting the *helper's* comment
    kinds alone is not that bound — the reviewer verdict posts directly, without
    a receipt, and was missed by exactly that narrower check. So the raw `gh`
    call is what is pinned, wherever it appears.

    A new publishing site fails here, which is the signal to revisit all three
    ordering surfaces rather than let them go quietly stale.
    """
    posting_functions = comment_posting_functions()

    assert posting_functions == _COMMENT_POSTING_FUNCTIONS, (
        f"the set of functions that post comments changed: "
        f"{sorted(posting_functions)} != {sorted(_COMMENT_POSTING_FUNCTIONS)}. "
        f"Update the ordering surfaces before updating this test."
    )


def test_publication_ordering_docs_state_the_ast_derived_publishing_bound() -> None:
    """The cardinality the surfaces state must come from ``tick.py``, not a count.

    The complement rule is only worth what its bound is worth, and each surface
    spells that bound out in prose ("exactly two functions run ``gh … comment``").
    Nothing tied that number to the source. Growing the publishing set fails
    :func:`test_tick_publishes_from_only_two_places`, whose fix is to widen
    ``_COMMENT_POSTING_FUNCTIONS`` — and once widened, all three surfaces go
    green again while still claiming a bound the source no longer has. So the
    phrase is derived from the AST here, and a widened bound cannot be resolved
    without restating it everywhere it is published.

    The member names are checked the same way, against the extracted ordering
    section rather than the whole document — these documents name the posting
    functions elsewhere for unrelated reasons, and a whole-file scan reads those
    mentions as the ordering surface naming its publishing set.

    Which surfaces carry the names is declared, not inferred. The internals
    reference names the symbols; the operator README states the bound without
    them. Inferring it from what a surface happens to name is what let a rename of
    *every* posting function pass: the docs named none of the new symbols, so a
    "names none" surface was read as a surface that never names them. Declaring
    the bearers makes the stale document the failure it is.
    """
    posting_functions = comment_posting_functions()
    word = _CARDINALITY_WORDS.get(len(posting_functions))
    assert word is not None, (
        f"no cardinality word for {len(posting_functions)} publishing functions; "
        f"extend _CARDINALITY_WORDS and restate the bound on every ordering surface"
    )
    phrase = f"exactly {word} functions"

    for surface, text in publication_ordering_sections():
        assert phrase in text, (
            f"{surface.name} does not state the publishing bound as {phrase!r}, "
            f"the cardinality tick.py actually has"
        )
        named = {fn for fn in posting_functions if fn in text}
        if surface.names_members:
            assert named == posting_functions, (
                f"{surface.name} names {sorted(named)} in its publication-"
                f"ordering section but tick.py publishes from "
                f"{sorted(posting_functions)}"
            )
        else:
            assert not named, (
                f"{surface.name} states the bound without symbol names by "
                f"contract, but its publication-ordering section names "
                f"{sorted(named)}; either drop them or set names_members=True so "
                f"the full member set is required there"
            )


def test_tick_publishes_only_the_documented_comment_kinds() -> None:
    """Every receipted post must carry one of the three documented kinds.

    The receipt key is what makes a post idempotent across harvest retries, so a
    call with a non-constant or undocumented kind is both an unbounded publishing
    surface and an unbounded receipt namespace.
    """
    tree = ast.parse((Path(__file__).parent / "tick.py").read_text())
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != _PUBLISH_HELPER:
            continue
        kind = next(
            (kw.value for kw in node.keywords if kw.arg == "kind"), None,
        )
        assert isinstance(kind, ast.Name), (
            f"{_PUBLISH_HELPER} called with a non-constant comment kind; the "
            f"publishing surface is no longer statically bounded"
        )
        kinds.add(kind.id)

    assert kinds == _PUBLISHED_COMMENT_KINDS, (
        f"the published comment kinds changed: {sorted(kinds)} != "
        f"{sorted(_PUBLISHED_COMMENT_KINDS)}. Update the ordering surfaces "
        f"before updating this test."
    )


def test_every_needs_human_transition_is_a_recorded_decision() -> None:
    """No function may escalate to the terminal label without being enumerated.

    The publication rule is an argument about a set of transitions, and the set
    is the part that moves. A new escalation site is exactly the moment someone
    has to answer "does this one explain itself?", and nothing in the source
    asks the question — the previous silent paths accumulated by being added and
    never revisited.

    Pinning the census asks it. A new site fails here, and the fix is either to
    route it through a publishing helper or to record it as deliberately silent
    with the reason, which is the enumeration the ordering surfaces argue from.
    """
    sites = needs_human_relabel_sites()

    assert sites == _NEEDS_HUMAN_TRANSITIONS, (
        f"the set of transitions to ai:needs-human-decision changed: {sites} != "
        f"{_NEEDS_HUMAN_TRANSITIONS}. A new site must either publish before it "
        f"relabels or be recorded here as deliberately silent, with the reason."
    )


@pytest.mark.parametrize("agent_doc", ["CLAUDE.md", "AGENTS.md"])
def test_repo_agent_docs_exempt_dispatched_engines_from_label_workflow(
    agent_doc: str,
) -> None:
    """Both entrypoints must deliver the same unbounded engine restrictions.

    Claude imports the shared guide, including on provider routes without native
    AGENTS.md discovery. Check the effective instructions, not just the adapter.
    A counted label exemption could leave a later workflow grant in force.
    """
    text = root_agent_guidance(agent_doc)
    assert _CARVE_OUT_OPENER in text, (
        f"{agent_doc} has no dispatched-engine carve-out at all"
    )
    block = carve_out_block(text)
    for contract_text in [
        "every label instruction",
        "completion sentinel",
        "`gh pr create`",
    ]:
        assert contract_text in block, (
            f"{agent_doc} is missing the dispatched-engine carve-out: "
            f"{contract_text}"
        )


def test_operator_docs_define_targeted_issue_and_pr_scope() -> None:
    """Target syntax must describe the stage-specific runtime scope exactly."""
    skill = SKILL.read_text()
    readme = README.read_text()
    for text in (skill, readme):
        for token in ("#N", "issue `N`", "zero or one", "open closing PR",
                      "reviewer", "fixer", "`pr:N`", "direct PR scope"):
            assert token in text


def test_operator_docs_never_treat_dashboard_absence_as_custody_proof() -> None:
    """The runbook must not re-teach the inference the runtime removed.

    ``status.sh`` reads shared GitHub labels plus only *this* machine's marker
    root, so an in-flight label with no engine row reads identically for
    another machine's healthy dispatch, a manual pickup, and a dead engine.
    Every doc that shows the operator that pairing must say so, or following it
    steals live work exactly as the old automation did.
    """
    for doc in (README, SKILL, STATUS_SKILL):
        text = doc.read_text()
        assert "machine" in text, doc
        assert "custody proof" in text, doc

    readme = README.read_text()
    for token in ("out of band", "confirm", "late result", "pickup label"):
        assert token.lower() in readme.lower(), token


def test_operator_docs_define_target_resolution_fail_closed_boundary() -> None:
    """Target uncertainty may audit, but cannot mutate workflow or scratch state."""
    skill = SKILL.read_text()
    readme = README.read_text()
    for text in (skill, readme):
        for token in ("ambiguous", "malformed", "GitHub read failure",
                      "scratch/worktree", "GitHub workflow mutation",
                      "audit log", "tick lock", "excepted"):
            assert token in text
        assert "before any local or GitHub mutation" not in text


def test_operator_docs_locate_engine_stderr() -> None:
    """Troubleshooting docs must distinguish sanitized reviewer diagnostics."""
    for text in (README.read_text(), DEBUG.read_text()):
        for token in ("`stderr.log`", "reviewer", "bounded", "sanitized",
                      "startup error class", "`EngineExitError`",
                      "implementer/fixer", "engine stderr"):
            assert token in text
        assert "raw reviewer engine stderr" not in text


def test_operator_docs_define_failed_reviewer_terminal_custody() -> None:
    """Reviewer wrapper failures must not be documented as redispatchable work."""
    skill = SKILL.read_text()
    debug = DEBUG.read_text()
    for text in (skill, debug):
        for token in ("dispatch-bound", "failed reviewer", "completion",
                      "`ai:needs-human-decision`", "redispatch", "archive failure",
                      "relabel failure", "scratch"):
            assert token in text


def test_preflight_scopes_model_report_as_unverified_signal() -> None:
    """Preflight must label the sandbox denial signal as UNVERIFIED."""
    text = PREFLIGHT.read_text()
    assert "UNVERIFIED" in text


# --- Category B — behavioral invariants (mock subprocess on tick.py functions) ---


def test_read_authoritative_in_flight_labels_queries_three_label_sets() -> None:
    """read_authoritative_in_flight_labels queries all three in-flight label sets."""
    from tick import read_authoritative_in_flight_labels  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json:
        gh_json.side_effect = [
            [{"number": 1}],
            [{"number": 2}],
            [{"number": 3}],
        ]
        refs = read_authoritative_in_flight_labels()

    labels = {r.label for r in refs}
    assert labels == {"ai:implementing", "ai:reviewing", "ai:fixing"}


def test_can_dispatch_now_fail_closed_on_corrupt_marker() -> None:
    """Admission must fail closed — one bad marker blocks all dispatch."""
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    with mock.patch("tick._scan_marker_identities") as scan:
        scan.side_effect = RuntimeError("corrupt")
        result = can_dispatch_now(max_in_flight=3)

    assert result.can_dispatch is False
    assert result.reason == "admission_error"


def test_can_dispatch_now_ignores_markerless_in_flight_labels() -> None:
    """A markerless in-flight label consumes no admission slot.

    The cap bounds the engines this machine launched. ``ai:implementing`` is
    also the documented manual pickup label, so counting it as a paid engine
    let stale hand-set labels starve every dispatch until an operator
    stripped them (#534, recurring 2026-08-20).
    """
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    with mock.patch("tick._scan_marker_identities") as scan:
        scan.return_value = []
        result = can_dispatch_now(max_in_flight=1)

    assert result.can_dispatch is True
    assert result.in_flight_count == 0


@pytest.mark.real_gh_helper
def test_relabel_uses_a_single_add_remove_mutation() -> None:
    """relabel() moves a label in one gh edit, never add-then-remove.

    Two mutations would leave a dual-label window that HARVEST reads as a
    partially applied transition.
    """
    from tick import relabel  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        relabel("pr", 42, "ai:reviewing", "ai:ready-for-human")

    assert run.call_count == 1
    call_args = run.call_args[0][0]
    assert "--add-label" in call_args
    assert "--remove-label" in call_args
    assert "ai:ready-for-human" in call_args


def test_dispatch_writes_the_lease_before_it_relabels(tmp_path: Path) -> None:
    """Dispatch writes the local lease marker before applying the label.

    A label applied first is a custody claim with no local liveness record, so
    a crash between the two strands the item at an in-flight label with
    nothing to harvest.

    The engine launch is recorded too, so the asserted order is the real one
    rather than the one the docs would like. The launching lease lands before
    the engine spends — that half is sound — but the *label* lands after it.
    That residual window (another machine can select the same item between the
    spawn and the relabel) is pre-existing and out of scope here; asserting the
    true order keeps it visible instead of letting a docstring imply the
    label is acquired first.
    """
    import tick  # type: ignore[import-untyped]

    order: list[str] = []
    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345

    def record_write(path: str, data: str) -> None:
        if os.path.basename(path) == "dispatch.json":
            order.append("lease")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(data)

    def record_gh(*args: str, **kwargs: object) -> object:
        stdout = (
            '{"defaultBranchRef":{"name":"main"}}'
            if args[:2] == ("repo", "view") else ""
        )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

    def record_relabel(*args: object, **kwargs: object) -> None:
        order.append("relabel")

    def record_launch(*args: object, **kwargs: object) -> object:
        order.append("launch")
        return fake_proc

    def fake_git(*args: str, **kwargs: object) -> str:
        return "deadbeef" if "rev-parse" in args else ""

    with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        with mock.patch.object(tick, "WT_ROOT", str(tmp_path)):
            with mock.patch.object(tick, "git", side_effect=fake_git):
                with mock.patch.object(
                    tick.pipeline, "atomic_write", side_effect=record_write,
                ):
                    with mock.patch.object(tick, "gh", side_effect=record_gh):
                        with mock.patch.object(
                            tick, "relabel", side_effect=record_relabel,
                        ):
                            with mock.patch.object(
                                tick, "_publish_marker_or_revert",
                                return_value=True,
                            ):
                                with mock.patch.object(
                                    tick, "_resolve_engine",
                                    return_value=("claude", {}),
                                ):
                                    with mock.patch.object(
                                        tick, "_launch_claude_engine",
                                        side_effect=record_launch,
                                    ):
                                        tick._dispatch_one_implementer(
                                            issue_number=77,
                                            issue_title="Test",
                                            issue_body="Body",
                                        )

    assert order == ["lease", "launch", "relabel"]


def test_verify_codex_bin_prevents_below_minimum_reviewer_launch() -> None:
    """Dispatch must reject codex versions below the minimum floor."""
    import shutil as _shutil

    from tick import verify_codex_bin  # type: ignore[import-untyped]

    with mock.patch.object(_shutil, "which", return_value="/usr/local/bin/codex"):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["/usr/local/bin/codex", "--version"],
                returncode=0, stdout="codex-cli 0.140.0\n", stderr="",
            )
            with pytest.raises(RuntimeError, match="below minimum"):
                verify_codex_bin(min_version="codex-cli 0.141.0")


def test_reconcile_engine_kill_gated_on_matched_identity() -> None:
    """Kill only fires when identity is 'matched'."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )

    with mock.patch("tick.gh_text") as gh_text:
        gh_text.return_value = ""  # empty reconcile set
        with mock.patch("os.listdir") as listdir:
            listdir.return_value = []
            decisions, ok = reconcile_engine(config, tick_log=None, target=None,
                                             wt_root="/tmp")

    # Empty reconcile set = no kill decisions
    assert len(decisions) == 0
    assert ok is True


def test_main_honors_harvest_ordering() -> None:
    """main() runs HARVEST phases in the correct order (review → implement → fix).

    DISPATCH follows HARVEST and is gated on reconcile_ok.
    """
    from tick import main  # type: ignore[import-untyped]

    call_order: list[str] = []

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = "/tmp"
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.side_effect = lambda *a, **kw: call_order.append("harvest_reviewers") or True
            with mock.patch("tick.harvest_implementers") as hi:
                hi.side_effect = lambda *a, **kw: call_order.append("harvest_implementers") or True
                with mock.patch("tick.harvest_fixers") as hf:
                    hf.side_effect = lambda *a, **kw: call_order.append("harvest_fixers") or True
                    main()

    assert call_order == [
        "harvest_reviewers", "harvest_implementers", "harvest_fixers",
    ], f"Expected harvest ordering, got: {call_order}"


# --- Category C — structural invariants (subprocess call args) ---


def test_dispatch_implementers_uses_comma_separated_json_fields() -> None:
    """dispatch_implementers must query gh issue list --json number,title,author (comma-separated).

    The gh CLI treats ``--json number title`` as two separate positional arguments
    and rejects the call.  The correct form is ``--json number,title,author``.
    """
    from tick import dispatch_implementers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json:
        gh_json.return_value = []  # empty list → loop body never runs
        dispatch_implementers(authoritative_in_flight=[])

    # Find the issue list call and verify comma-separated fields.
    found_call = False
    for call in gh_json.call_args_list:
        args: tuple[str, ...] = call[0]
        if len(args) >= 2 and args[0] == "issue" and args[1] == "list":
            found_call = True
            for i, a in enumerate(args):
                if a == "--json" and i + 1 < len(args):
                    assert args[i + 1] == "number,title,author", (
                        f"Expected '--json number,title,author' (single comma-separated string), "
                        f"got '--json {args[i + 1]}'"
                    )
    assert found_call, "dispatch_implementers did not call gh_json for issue list"


@pytest.mark.real_gh_helper
def test_gh_helper_always_uses_utf8_errors_replace() -> None:
    """gh() must use encoding='utf-8', errors='replace'."""
    from tick import gh  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "list"], returncode=0, stdout="", stderr="",
        )
        gh("pr", "list")
        call_kwargs = run.call_args[1]
        assert call_kwargs["encoding"] == "utf-8"
        assert call_kwargs["errors"] == "replace"


def test_git_helper_always_uses_utf8_errors_replace() -> None:
    """git() must use encoding='utf-8', errors='replace'."""
    from tick import git  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["git", "rev-parse"], returncode=0, stdout="/repo\n", stderr="",
        )
        git("rev-parse", "--show-toplevel")
        call_kwargs = run.call_args[1]
        assert call_kwargs["encoding"] == "utf-8"
        assert call_kwargs["errors"] == "replace"


def test_tick_log_entry_has_ts_as_integer() -> None:
    """TickLog entries must emit ts as an integer (epoch seconds)."""
    import json as _json
    import tempfile

    from tick import TickLog  # type: ignore[import-untyped]

    tmp = tempfile.mkdtemp()
    try:
        log = TickLog.open(tmp)
        log.log(pr=42, phase="harvest", stage="review", action="promote",
                reason="no blockers")
        with open(log.path) as fh:
            entry = _json.loads(fh.readline())
        assert isinstance(entry["ts"], int)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# --- Category A — ordering invariants (mock + assert_has_calls) ---


def test_main_harvest_phases_called_in_order() -> None:
    """HARVEST phases must execute in order: reviewers, implementers, fixers."""
    from tick import main  # type: ignore[import-untyped]

    manager = mock.MagicMock()
    manager.attach_mock(mock.MagicMock(return_value=True), "harvest_reviewers")
    manager.attach_mock(mock.MagicMock(return_value=True), "harvest_implementers")
    manager.attach_mock(mock.MagicMock(return_value=True), "harvest_fixers")

    with mock.patch("tick.git", return_value="/tmp"):
        with mock.patch("tick.harvest_reviewers", manager.harvest_reviewers):
            with mock.patch("tick.harvest_implementers", manager.harvest_implementers):
                with mock.patch("tick.harvest_fixers", manager.harvest_fixers):
                    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    main()

    harvest_calls = [c[0] for c in manager.mock_calls if "harvest" in str(c)]
    assert len(harvest_calls) == 3
    assert "harvest_reviewers" in str(harvest_calls[0])
    assert "harvest_implementers" in str(harvest_calls[1])
    assert "harvest_fixers" in str(harvest_calls[2])


# --- Category E — install-portability and identity invariants ---

# Docs that must stay correct wherever the pipeline is installed: the operator
# entry points plus the internals reference an operator follows into the source.
# The architecture doc is deliberately absent — it explains the design's shape,
# not how to invoke it, so it names no install-resolved path.
_INSTALL_PORTABLE_DOCS = (
    SKILL, README, DEBUG, LIB_README, PREFLIGHT, STATUS_SKILL, STATUS_SCRIPT,
    INTERNALS, DONE_SKILL,
)

# A pipeline path as a doc can legitimately write one: repo-relative in prose and
# links, or install-root-anchored in a runnable command — under the plugin root
# in a skill body, under `$PR_PIPELINE_HOME` in a command run from a shell. The
# optional `.claude/` alternative is matched only so the stale in-repo layout is
# reported as the specific defect it is rather than as a generic missing file.
#
# The published skill names are spelled out rather than matched as `skills/*`
# because `.claude/skills/` in a target repository is a different directory that
# these docs legitimately name; only this plugin's own skills resolve here.
_PIPELINE_PATH_RE = re.compile(
    r"(?:\$\{CLAUDE_PLUGIN_ROOT\}/|\$PR_PIPELINE_HOME/|\.claude/)?"
    r"skills/(?:" + "|".join(sorted(_PUBLISHED_SKILLS)) + r")(?:/[\w.-]+)*"
)

# Tokens that bind a shipped, paste-ready prompt to one machine or one target.
_OPERATOR_SPECIFIC_TOKENS = ("/Users/", "/home/", "sworn")

# A backticked repo path in a root agent doc: at least one `/`, so a backticked
# bare term (`pr-pipeline`, `gh`) is not mistaken for a file that must exist.
_BACKTICKED_PATH_RE = re.compile(r"`([A-Za-z][\w.-]*(?:/[\w.-]*)+)`")
_MARKDOWN_PATH_RE = re.compile(r"\]\(([^)#]+)(?:#[^)]*)?\)")

# Paths the docs name that are created at runtime and gitignored by design, so
# their absence from a clean checkout is correct rather than a broken reference.
_RUNTIME_CREATED_PATHS = frozenset({"tmp"})

# Any `ai:` label named in prose. The trailing character class stops at the
# closing backtick, so the bare namespace ("the `ai:` namespace") never matches.
_AI_LABEL_RE = re.compile(r"ai:[a-z][a-z-]*")


def test_shipped_floor_allows_policy_edits_with_operator_only_pickup() -> None:
    """The policy default changes atomically with operator-only issue admission.

    Runtime trust and cadence tests cover that boundary; broader issue authors
    must restore the conservative default when they are introduced.
    """
    with open(POLICY_CONFIG, "rb") as fh:
        config = tomllib.load(fh)

    assert config.get("defaults", {}).get("allow_touching") is True
    assert "policy" not in config, "the legacy policy table must not return"


def test_shipped_floor_carries_no_operator_owned_table() -> None:
    """No ``[operator]``, ``[repos.*]`` or ``[scan]`` table may ship here.

    This file is layer 1 and only layer 1. A plugin install lands at a
    hash-named path and ``/plugin update`` installs the next version at a
    *different* hash, so an operator value written here is discarded on the next
    upgrade with nothing to show for it. The resolver reports these tables as a
    configuration error; this catches the commit that adds one, which is where
    the mistake actually originates.
    """
    with open(POLICY_CONFIG, "rb") as fh:
        config = tomllib.load(fh)

    assert not {"operator", "repos", "scan"} & set(config), (
        "the packaged config.toml carries an operator-owned table; operator "
        "configuration belongs in ~/.claude/pr-pipeline/config.toml"
    )


def test_shipped_floor_declares_every_per_target_setting() -> None:
    """Every per-target key the runtime reads must appear in the floor.

    The floor is what a fresh install runs on and what the file documents to an
    adopter. A key the resolver reads but the floor omits is a setting an
    operator has no way to discover, and its effective default lives only in
    Python rather than in the file they are being asked to copy.
    """
    import pipeline

    with open(POLICY_CONFIG, "rb") as fh:
        config = tomllib.load(fh)

    missing = set(pipeline._CONFIG_KEYS) - set(config.get("defaults", {}))

    assert not missing, (
        f"the packaged floor omits per-target settings the resolver reads: "
        f"{sorted(missing)}"
    )


def test_no_operator_doc_names_a_pipeline_path_that_does_not_exist() -> None:
    """Every pipeline path an operator doc names must resolve in this repo.

    The skills were incubated at ``.claude/skills/pr-pipeline-*`` inside their
    host repo, where every documented command happened to be correct by accident
    of location. They ship from ``skills/`` here and from a plugin root once
    installed, so a path written for the old depth is not merely untidy — the
    documented preflight and tick commands exit 127 and 2 respectively, and the
    source links in the internals doc resolve above the repository.

    Pipeline *code* is therefore named relative to the install root
    (``${CLAUDE_PLUGIN_ROOT}`` in a skill body, ``$PR_PIPELINE_HOME`` in a
    command run from a shell) and repo-relative in prose, never relative to the
    repository the tick operates on. Pipeline *state* (``.claude/worktrees/``)
    stays target-repo-relative and is untouched by this guard.
    """
    unresolved: list[str] = []
    for doc in _INSTALL_PORTABLE_DOCS:
        text = doc.read_text()
        label = str(doc.resolve().relative_to(REPO_ROOT))
        for match in _PIPELINE_PATH_RE.finditer(text):
            named = match.group(0)
            if named.startswith(".claude/"):
                unresolved.append(f"{label}: {named} (stale in-repo skill layout)")
                continue
            relative = named.removeprefix(f"{PLUGIN_ROOT_VAR}/").removeprefix(
                f"{INSTALL_ROOT_VAR}/"
            )
            if not (REPO_ROOT / relative).exists():
                unresolved.append(f"{label}: {named}")

    assert not unresolved, (
        "these docs name pipeline paths that do not exist at this layout:\n\n"
        + "\n".join(sorted(set(unresolved)))
    )


def test_loop_prompt_is_not_bound_to_one_operators_checkout() -> None:
    """The paste-ready loop prompt must not hardcode a single target repo.

    ``SKILL.md`` ships to every install, and its loop prompt is copied verbatim
    into ``/loop`` by whoever runs it. An absolute path to the author's own
    checkout does not fail loudly for an adopter — it points a live control
    plane that dispatches paid engines and mutates labels at *someone else's
    repository*, and the prompt's own wording tells the operator that is
    correct.
    """
    prompt = loop_prompt_block(SKILL.read_text())

    assert prompt, "SKILL.md no longer contains a fenced loop prompt"
    offenders = [
        token
        for token in _OPERATOR_SPECIFIC_TOKENS
        if token.lower() in prompt.lower()
    ]

    assert not offenders, (
        f"the loop prompt is bound to one operator's checkout: {offenders}"
    )


@pytest.mark.parametrize("agent_doc", ["CLAUDE.md", "AGENTS.md"])
def test_root_agent_docs_route_only_to_documents_that_exist(agent_doc: str) -> None:
    """Direct and imported guidance must route to existing local documents."""
    text = root_agent_guidance(agent_doc)
    paths = _BACKTICKED_PATH_RE.findall(text) + _MARKDOWN_PATH_RE.findall(text)

    missing = sorted({
        named
        for named in paths
        if "://" not in named
        and named.rstrip("/") not in _RUNTIME_CREATED_PATHS
        and not (REPO_ROOT / named.rstrip("/")).exists()
    })

    assert not missing, (
        f"{agent_doc} routes to documents that do not exist: {missing}"
    )


@pytest.mark.parametrize(
    "workflow_doc", ["CLAUDE.md", "AGENTS.md", "docs/working/pr-guidelines.md"]
)
def test_workflow_docs_use_the_label_vocabulary_the_tick_implements(
    workflow_doc: str,
) -> None:
    """No workflow doc may teach an ``ai:`` label the pipeline does not know.

    Labels are the pipeline's only source of truth, and ``pipeline.py`` owns the
    vocabulary. A doc teaching a label the tick never reads is worse than a typo:
    an interactive agent that follows it moves a real PR to a label no dispatch
    query selects, and the item goes dark in the queue with nothing to signal it.
    ``ai:recheck`` was exactly that — the shipped re-review label is
    ``ai:revised``.
    """
    text = (
        root_agent_guidance(workflow_doc)
        if workflow_doc in {"AGENTS.md", "CLAUDE.md"}
        else (REPO_ROOT / workflow_doc).read_text()
    )

    unknown = sorted({
        label
        for label in _AI_LABEL_RE.findall(text)
        if label not in implemented_labels()
    })

    assert not unknown, (
        f"{workflow_doc} teaches ai: labels the pipeline does not implement: "
        f"{unknown}"
    )


# --- Category F — target-independence invariants ---

# The shipped surfaces that describe what an *engine* does. None of them may
# name one target repo's toolchain: what to run is the target's CLAUDE.md to
# state, and what an engine is permitted to run is the operator's config.
#
# `lib/README.md` is deliberately absent. It documents how to develop and test
# the pipeline's own `lib/`, which really is a Python project — naming pytest
# there is a fact about this repository, not a claim about anyone's target.
_TARGET_INDEPENDENT_SURFACES = (
    README, DEBUG, INTERNALS, DONE_WHY,
    *sorted(SKILLS_ROOT.glob("*/SKILL.md")),
    PROMPTS / "implementer.txt", PROMPTS / "fixer.txt", PROMPTS / "reviewer.txt",
)

# Build tools that belong to a target repo, not to the pipeline. Matched with
# word boundaries so `pytest.ini` in a path or `npmjs` in a URL is not a hit,
# and so the words appear here exactly as an author would write them.
_TARGET_STACK_TOOLS = (
    "pytest", "pyright", "ruff", "npm", "cargo", "gradle", "mypy", "eslint",
)


def test_engine_prompts_defer_verification_to_the_target_repo() -> None:
    """The prompt templates must not name any stack's verification commands.

    The pipeline drives a repository it does not own. Only that repository knows
    what its tests, linter, and type checker are and in what order they run, and
    it already says so in its own ``CLAUDE.md``/``AGENTS.md`` — which the engine
    reads. A prompt that names ``uv run pytest`` is correct in exactly one
    install and instructs every other engine to run a command that does not
    exist there.

    The permission to run those commands is a separate concern and stays in the
    operator's config; see
    :func:`test_engine_verify_tools_come_from_operator_config`.
    """
    for prompt_path in (PROMPTS / "implementer.txt", PROMPTS / "fixer.txt"):
        text = prompt_path.read_text()
        assert "the repository's own verification commands" in text, (
            f"{prompt_path.name} does not defer verification to the target repo"
        )
        for tool in _TARGET_STACK_TOOLS:
            assert not re.search(rf"\b{tool}\b", text), (
                f"{prompt_path.name} hard-codes the tool {tool!r}"
            )


def test_no_shipped_surface_hardcodes_a_target_repo_toolchain() -> None:
    """No engine-facing doc or prompt may present one stack as the contract.

    The prompt check above is the load-bearing half, but a doc that still tells
    an operator "the engine runs ``uv run pytest``" teaches a contract the code
    no longer has — and the next author restores the code to match the doc,
    because a doc and an implementation that disagree are resolved in whichever
    direction is easier to edit.
    """
    violations: list[str] = []
    for surface in _TARGET_INDEPENDENT_SURFACES:
        text = surface.read_text()
        lines = text.splitlines()
        for tool in _TARGET_STACK_TOOLS:
            for match in re.finditer(rf"\b{tool}\b", text):
                line = text[: match.start()].count("\n") + 1
                violations.append(
                    f"{surface.name}:{line} names {tool!r}: "
                    f"{lines[line - 1].strip()!r}"
                )

    assert not violations, (
        "engine-facing surfaces name a target repo's toolchain; verification "
        "commands belong to the target's own CLAUDE.md and to the operator's "
        "`verify_tools`:\n" + "\n".join(violations)
    )


def test_no_shipped_surface_names_the_incubation_checkout() -> None:
    """No engine-facing surface may name one machine or the incubation repo.

    These surfaces travel to every install, and the tokens are the ones that
    only ever arrive by being carried over from where the skills were written:
    an author's home directory, or the name of the codebase they were incubated
    in. Neither resolves for an adopter, and neither fails loudly there — the
    reader simply follows a path or a repo that is not theirs.
    """
    violations: list[str] = []
    for surface in _TARGET_INDEPENDENT_SURFACES:
        text = surface.read_text()
        lines = text.splitlines()
        for token in _OPERATOR_SPECIFIC_TOKENS:
            for match in re.finditer(re.escape(token), text, re.IGNORECASE):
                line_no = text[: match.start()].count("\n") + 1
                violations.append(
                    f"{surface.name}:{line_no} names {token!r}: "
                    f"{lines[line_no - 1].strip()!r}"
                )

    assert not violations, (
        "shipped surfaces name a checkout an adopter does not have:\n"
        + "\n".join(violations)
    )


def test_definition_of_done_cites_no_work_item_number() -> None:
    """``definition-of-done`` must describe its evidence, not reference it.

    Its two files are the one place where a bare ``#614`` can only be a citation
    of a past review — the skill is argued from defects it has seen, so a number
    there is offered as proof. No adopter can open it against any repository
    they have, so it reads as evidence while proving nothing: the "receipt
    attached" shape the skill itself exists to reject. The failure shape is the
    transferable part, and it has to be written out.

    Markdown headings never match; a ``#`` here is followed by a digit.
    """
    violations: list[str] = []
    for surface in (DONE_SKILL, DONE_WHY):
        text = surface.read_text()
        lines = text.splitlines()
        for match in re.finditer(r"(?<!\w)#\d+", text):
            line_no = text[: match.start()].count("\n") + 1
            violations.append(
                f"{surface.name}:{line_no} cites {match.group(0)}: "
                f"{lines[line_no - 1].strip()!r}"
            )

    assert not violations, (
        "definition-of-done cites work items an adopter cannot resolve; "
        "describe the failure shape instead:\n" + "\n".join(violations)
    )


def test_shipped_config_declares_no_target() -> None:
    """The install carries no target repo key or preconfigured author identity."""
    with open(POLICY_CONFIG, "rb") as fh:
        config = tomllib.load(fh)

    target = config.get("target", {})
    assert "repo" not in target, (
        "config.toml ships a target repo; every adopter inherits it"
    )
    assert target.get("trusted_author") == "", (
        "config.toml ships a trusted review author; every adopter inherits it"
    )


def test_shipped_config_declares_no_review_checklist_paths() -> None:
    """``[defaults] checklist_paths`` must ship empty.

    ``CLAUDE.md`` and ``AGENTS.md`` gate by basename with no configuration, so
    the shipped list has nothing it needs to carry. A path pinned here would be
    one repo's, dead in every other install, and it would make an adopter's gate
    look configured when nothing in their repo is actually covered.
    """
    with open(POLICY_CONFIG, "rb") as fh:
        config = tomllib.load(fh)

    assert config.get("defaults", {}).get("checklist_paths") == [], (
        "config.toml ships review-checklist paths that belong to one target repo"
    )


# --- Category G — plugin packaging invariants ---


def test_plugin_manifest_publishes_the_skills_directory() -> None:
    """``plugin.json`` must exist, parse, and point at ``./skills``.

    This file is what makes the repository installable at all. Without it
    ``/plugin install`` has nothing to read, and every other packaging decision
    below describes a plugin an adopter cannot obtain.
    """
    assert PLUGIN_MANIFEST.exists(), (
        f"no plugin manifest at {PLUGIN_MANIFEST.relative_to(REPO_ROOT)}"
    )
    manifest = json.loads(PLUGIN_MANIFEST.read_text())

    assert manifest.get("name") == PLUGIN_NAME, (
        "the plugin name prefixes every command it publishes; it must be "
        f"{PLUGIN_NAME!r}, not {manifest.get('name')!r}"
    )
    assert manifest.get("skills") == "./skills", (
        "plugin.json must publish ./skills; the existing layout is already "
        "plugin-shaped and moving it would be a restructure, not packaging"
    )


def test_marketplace_manifest_makes_this_repository_its_own_source() -> None:
    """``marketplace.json`` must publish this repository as the plugin source.

    The repository being its own marketplace is what makes
    ``/plugin marketplace add kwuBuild/pr-pipeline`` work with no separate
    registry to stand up and keep in step with releases. A ``source`` naming
    anywhere but ``./`` reintroduces exactly that second thing to maintain.
    """
    assert MARKETPLACE_MANIFEST.exists(), (
        f"no marketplace manifest at "
        f"{MARKETPLACE_MANIFEST.relative_to(REPO_ROOT)}"
    )
    manifest = json.loads(MARKETPLACE_MANIFEST.read_text())

    plugins = manifest.get("plugins", [])
    assert len(plugins) == 1, (
        f"the marketplace must publish exactly one plugin, found {len(plugins)}"
    )
    entry = plugins[0]
    assert entry.get("name") == PLUGIN_NAME
    assert entry.get("source") == "./", (
        "the plugin source must be this repository itself, not "
        f"{entry.get('source')!r}"
    )


def test_plugin_manifest_is_the_only_version_field() -> None:
    """``plugin.json`` holds the one version that installed users update to.

    An installed plugin updates only when this version changes, so it is the
    release key. A second ``version`` on the marketplace entry can drift from it,
    and then the catalog and the installed manifest disagree about what a user
    has.
    """
    version = json.loads(PLUGIN_MANIFEST.read_text()).get("version")
    assert isinstance(version, str) and re.fullmatch(
        r"\d+\.\d+\.\d+", version
    ), f"plugin.json version must be MAJOR.MINOR.PATCH, not {version!r}"

    entry = json.loads(MARKETPLACE_MANIFEST.read_text())["plugins"][0]
    assert "version" not in entry, (
        "the marketplace entry must not declare its own version; "
        "plugin.json is the only release key"
    )


def test_changelog_records_the_manifest_version() -> None:
    """``CHANGELOG.md`` must have a section for the version being shipped.

    A version bump without a changelog entry gives updating users no record of
    what changed or which upgrade step the release needs.
    """
    version = json.loads(PLUGIN_MANIFEST.read_text())["version"]
    assert CHANGELOG.exists(), "no CHANGELOG.md at the repository root"
    headings = re.findall(r"^## \[([^\]]+)\]", CHANGELOG.read_text(), re.M)
    assert version in headings, (
        f"CHANGELOG.md has no '## [{version}]' section for the version in "
        f"plugin.json; found {headings}"
    )


def test_ci_validates_both_plugin_manifests() -> None:
    """CI must run Claude Code's own validator on both manifests.

    The structural tests above check the fields this repository relies on. The
    validator checks the schema that ``/plugin install`` enforces, so a
    malformed manifest fails here instead of on a user's machine.
    """
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    for manifest in (MARKETPLACE_MANIFEST, PLUGIN_MANIFEST):
        target = manifest.relative_to(REPO_ROOT)
        assert f"claude plugin validate {target}" in workflow, (
            f"ci.yml does not run 'claude plugin validate {target}'"
        )


def test_published_skill_set_is_exactly_the_intended_commands() -> None:
    """``skills/`` must hold exactly the commands the plugin means to publish.

    ``"skills": "./skills"`` publishes the *directory*, so anything dropped into
    it becomes a namespaced, user-invocable command whose description loads into
    every session's context. That makes an unintended command a side effect
    nobody asked for, and one an at-least assertion cannot detect — so this is
    an equality, and adding a skill is deliberately a test edit.
    """
    published = {
        path.parent.name for path in SKILLS_ROOT.glob("*/SKILL.md")
    }
    assert published == set(_PUBLISHED_SKILLS), (
        "the published command set drifted from the recorded decision:\n"
        f"  unexpected: {sorted(published - _PUBLISHED_SKILLS)}\n"
        f"  missing:    {sorted(_PUBLISHED_SKILLS - published)}"
    )


def test_published_skill_relative_links_resolve_inside_install() -> None:
    """Moving skills must not leave links into their source checkout."""
    for skill in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        for target in _MARKDOWN_PATH_RE.findall(skill.read_text()):
            if "://" in target:
                continue
            resolved = (skill.parent / target).resolve()
            assert resolved.is_relative_to(REPO_ROOT.resolve()), (
                f"{skill}: link escapes the install: {target}"
            )
            assert resolved.is_file(), f"{skill}: missing link target: {target}"


def test_every_skills_subdirectory_is_a_loadable_skill() -> None:
    """No directory may sit under ``skills/`` without being a real skill.

    A subdirectory with no ``SKILL.md`` is invisible to the command-set
    assertion above, so ``skills/`` is the one place a resource cannot be parked
    "out of the way". Anything that is not a published command belongs outside
    the manifest's ``skills`` entry entirely.
    """
    orphans = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(SKILLS_ROOT.iterdir())
        if path.is_dir() and not (path / "SKILL.md").exists()
    ]
    assert not orphans, (
        "these directories are under the published skills root but are not "
        f"skills, so the command-set assertion cannot see them: {orphans}"
    )


def test_each_published_skill_is_invoked_by_its_directory_name() -> None:
    """A skill's directory name and its frontmatter ``name`` must agree.

    The directory name is what the harness namespaces into the command:
    ``skills/tick/`` under this plugin is ``/pr-pipeline:tick``. That was
    measured, not assumed — a user-level skill whose ``SKILL.md`` declares
    ``name: gstack`` from a directory named ``.gstack-bak`` is offered as
    ``.gstack-bak``, so the frontmatter does not win.

    Keeping the two equal is what makes that ordering stop mattering. They
    disagree only while a rename is half-applied, which is precisely when the
    published command name is not what anyone reading the frontmatter expects.
    """
    for name in sorted(_PUBLISHED_SKILLS):
        skill_md = SKILLS_ROOT / name / "SKILL.md"
        declared = frontmatter_name(skill_md.read_text())
        assert declared == name, (
            f"skills/{name}/SKILL.md declares name: {declared!r}; the command "
            f"published from this directory is /{PLUGIN_NAME}:{name}"
        )


def test_skill_bodies_resolve_the_install_through_the_plugin_root() -> None:
    """Commands inside a ``SKILL.md`` must be anchored at the plugin root.

    An operator cannot know the cache path a plugin installs to, so
    ``$PR_PIPELINE_HOME`` cannot be the contract for an installed plugin: it
    asks them to export a path only the harness knows. Skill bodies are the one
    surface where the harness answers that for them — ``${CLAUDE_PLUGIN_ROOT}``
    is substituted into the content as Claude loads it, so the agent reads an
    absolute path.

    ``README.md`` and ``DEBUG.md`` are excluded on purpose and asserted
    elsewhere: nothing substitutes them, and the same string there resolves to a
    root-relative path that fails without saying why.
    """
    for skill_md in (SKILL, STATUS_SKILL, SETUP_SKILL):
        text = skill_md.read_text()
        label = str(skill_md.resolve().relative_to(REPO_ROOT))
        assert INSTALL_ROOT_VAR not in text, (
            f"{label} still anchors a command at {INSTALL_ROOT_VAR}, which an "
            "operator of an installed plugin has no way to set correctly"
        )
        assert PLUGIN_ROOT_VAR in text, (
            f"{label} names no install root at all; a bare relative path "
            "resolves against the target repo, not the install"
        )


def test_standalone_docs_use_the_resolving_launcher() -> None:
    for doc in (README, DEBUG):
        for line in runnable_lines(doc):
            if line is not None and not line.strip().startswith("#"):
                assert "$PR_PIPELINE_HOME/" not in line
                assert "${CLAUDE_PLUGIN_ROOT}/" not in line
        assert "~/.claude/pr-pipeline/bin/pr-pipeline" in doc.read_text()


def test_permission_rules_never_name_the_install_root_as_a_variable() -> None:
    """A documented ``permissions.allow`` rule must carry no unexpanded variable.

    A permission rule is matched against the *command text* the agent is about to
    run, and nothing expands inside one. `SKILL.md` hands the agent a command in
    which `${CLAUDE_PLUGIN_ROOT}` has already been substituted for the install's
    absolute path, so a rule written against either variable matches nothing.

    That failure is silent in the worst way: not an error, not a tick-log entry,
    just a permission prompt on every tick — which is precisely the condition the
    section documenting these rules exists to prevent. The rules are therefore
    written with a placeholder the reader must replace.
    """
    offenders: list[str] = []
    for doc in (DEBUG, README):
        label = str(doc.resolve().relative_to(REPO_ROOT))
        for rule in re.findall(r"\"Bash\([^\"]*\)\"", doc.read_text()):
            if PLUGIN_ROOT_VAR in rule or INSTALL_ROOT_VAR in rule:
                offenders.append(f"{label}: {rule}")

    assert not offenders, (
        "these permission rules name the install root as a variable, which "
        "expands in no rule and so grants nothing:\n" + "\n".join(offenders)
    )


def test_debug_md_documents_the_stale_plugin_root_after_upgrade() -> None:
    """``DEBUG.md`` must carry the stale-``CLAUDE_PLUGIN_ROOT`` caveat.

    The substitution happens once, when the session loads the skill. A session
    that was already running when ``/plugin marketplace update`` installed a new
    cache version keeps running the *old* version's code, silently and with no
    error, which is a debugging surface an operator reaches only by knowing to
    look for it.
    """
    text = normalize_prose(DEBUG.read_text())
    assert "claude_plugin_root" in text, (
        "DEBUG.md never mentions CLAUDE_PLUGIN_ROOT"
    )
    for phrase in ("already running", "restart"):
        assert phrase in text, (
            f"DEBUG.md does not document the stale plugin root caveat: "
            f"missing {phrase!r}"
        )


# --- Helpers ---


def implemented_labels() -> frozenset[str]:
    """Return every ``ai:`` label the shipped pipeline actually implements.

    Read from ``pipeline.py``'s ``LABEL_*`` constants rather than restated here,
    so adding a label to the runtime cannot leave this guard rejecting it and
    removing one cannot leave the docs teaching it.

    Returns:
        The label strings the tick reads or writes.
    """
    import pipeline  # type: ignore[import-untyped]

    return frozenset(
        value
        for name, value in vars(pipeline).items()
        if name.startswith("LABEL_") and isinstance(value, str)
    )


def loop_prompt_block(text: str) -> str:
    """Return the fenced prompt ``SKILL.md`` tells the operator to paste into /loop.

    Only the fenced block is returned: the surrounding section explains the loop
    contract in prose that may legitimately name the host repository, while the
    block itself is copied verbatim into a live control plane.

    Args:
        text: Raw contents of ``SKILL.md``.

    Returns:
        The prompt body, or the empty string if the section or its fence is gone.
    """
    marker = "Paste this prompt into /loop"
    start = text.find(marker)
    if start == -1:
        return ""
    fence_open = text.find("```", start)
    if fence_open == -1:
        return ""
    body_start = text.index("\n", fence_open) + 1
    fence_close = text.find("```", body_start)
    return text[body_start:fence_close] if fence_close != -1 else ""


def ordering_surfaces() -> tuple[tuple[str, str], ...]:
    """Return the whole contents of each doc stating the publication-ordering rule.

    Each surface stated the rule independently, which is how they drifted apart:
    correcting one leaves the other two teaching the superseded version. Pinning
    them together is what makes a partial fix fail.

    Whole-file scope is right for *existence* checks — a required phrase may move
    between sections and still be stated. It is wrong for checking which symbols a
    surface names, because these documents name them elsewhere too; use
    :func:`publication_ordering_sections` for that.

    Returns:
        ``(display name, normalized contents)`` per ordering surface. See
        :func:`normalize_prose` for why the contents are normalized.
    """
    return tuple(
        (surface.name, normalize_prose(surface.path.read_text()))
        for surface in _ORDERING_SURFACES
    )


def publication_ordering_sections() -> tuple[tuple[_OrderingSurface, str], ...]:
    """Return just the publication-ordering section of each ordering surface.

    Slices each document between its declared anchors, so a check on what the
    surface *names* reads only the prose that states the bound. Both anchors are
    required: a surface that renamed or dropped its section fails here rather
    than degrading into a whole-file scan, or into an empty slice that passes
    everything.

    Returns:
        ``(surface, normalized section)`` per ordering surface.
    """
    sections: list[tuple[_OrderingSurface, str]] = []
    for surface in _ORDERING_SURFACES:
        raw = surface.path.read_text()
        start = raw.find(surface.section_start)
        assert start != -1, (
            f"{surface.name} no longer opens its publication-ordering section "
            f"with {surface.section_start!r}; re-anchor it in _ORDERING_SURFACES "
            f"rather than letting the member check widen to the whole file"
        )
        end = raw.find(surface.section_end, start + len(surface.section_start))
        assert end != -1, (
            f"{surface.name}'s publication-ordering section is no longer "
            f"terminated by {surface.section_end!r}; re-anchor it in "
            f"_ORDERING_SURFACES"
        )
        sections.append((surface, normalize_prose(raw[start:end])))
    return tuple(sections)


def comment_posting_functions() -> frozenset[str]:
    """Return the ``tick.py`` functions that reach ``gh <entity> comment``.

    The raw ``gh`` call is what is inspected, not the receipted helper: the
    reviewer verdict posts directly and would be invisible to a helper-scoped
    check. Two contracts read this — the bound itself, and the cardinality the
    ordering surfaces publish — so they cannot disagree about what the source
    says.

    Returns:
        The names of the enclosing functions containing a ``gh`` call with a
        literal ``"comment"`` argument.
    """
    tree = ast.parse((Path(__file__).parent / "tick.py").read_text())
    posting: set[str] = set()
    for enclosing, node in walk_calls_with_enclosing_function(tree):
        if not isinstance(node.func, ast.Name) or node.func.id != "gh":
            continue
        if any(
            isinstance(arg, ast.Constant) and arg.value == "comment"
            for arg in node.args
        ):
            posting.add(enclosing)
    return frozenset(posting)


def reaches_hold_label(argument: ast.expr) -> bool:
    """Return whether one relabel argument may carry the hold label.

    Fails closed, for the same reason :func:`conftest.is_mutating_gh` does:
    a census that only recognised ``pipeline.LABEL_NEEDS_HUMAN`` would miss the
    two spellings the reconcile paths already use — a bare
    ``"ai:needs-human-decision"`` string (``_reconcile_one``) and a variable
    proven equal to the constant by an enclosing guard
    (``_execute_reconcile_actions``). Those are exactly the style a new silent
    escalation would be written in, so recognising only the attribute form
    would leave the hole this census exists to close.

    Anything the walk cannot resolve to a *different* label therefore counts.
    A name, a call, a subscript — all indeterminate, all enumerated, and the
    cost of a false positive is one recorded line in
    :data:`_NEEDS_HUMAN_TRANSITIONS` rather than a missed escalation.

    Args:
        argument: One positional or keyword argument of a relabel call.

    Returns:
        ``True`` unless the argument is provably some other label.
    """
    import pipeline  # type: ignore[import-untyped]

    if isinstance(argument, ast.Attribute):
        return argument.attr == "LABEL_NEEDS_HUMAN"
    if isinstance(argument, ast.Constant):
        return argument.value == pipeline.LABEL_NEEDS_HUMAN
    return True


def needs_human_relabel_sites() -> dict[str, int]:
    """Return each ``tick.py`` function that relabels an item to the hold label.

    Keyed by the *enclosing* function so a nested escalation helper reports its
    own name, and valued by count so a function that owns one deliberate silent
    transition cannot quietly grow a second.

    Only the *target* label position is classified, which is where each helper
    actually puts it: ``relabel``/``_relabel_with_context`` take
    ``(entity, number, from_label, to_label)``, and ``_converge_pipeline_labels``
    takes ``(entity, number, to_label)``. Reading every argument from the third
    on would fold ``from_label`` in, and since that is usually a variable the
    fail-closed classifier would count ordinary in-flight relabels as hold-label
    sites — making the census a superset that no longer means what it says.

    Returns:
        ``{function name: number of transitions to the hold label}``.
    """
    tree = ast.parse((Path(__file__).parent / "tick.py").read_text())
    sites: dict[str, int] = {}
    for enclosing, node in walk_calls_with_enclosing_function(tree):
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in _RELABEL_HELPERS:
            continue
        target_index = 2 if node.func.id == "_converge_pipeline_labels" else 3
        label_args = [
            *node.args[target_index:target_index + 1],
            # Both spellings: `relabel`/`_relabel_with_context` name the
            # parameter `to_label`, `_converge_pipeline_labels` names it
            # `target_label`. Matching one would leave the other invisible the
            # first time a call site passes it by keyword.
            *(
                kw.value for kw in node.keywords
                if kw.arg in ("to_label", "target_label")
            ),
        ]
        if any(reaches_hold_label(arg) for arg in label_args):
            sites[enclosing] = sites.get(enclosing, 0) + 1
    return sites


def walk_calls_with_enclosing_function(
    tree: ast.Module,
) -> list[tuple[str, ast.Call]]:
    """Yield every call in a module paired with the function that contains it.

    ``ast.walk`` loses the enclosing scope, and the publishing bound is a claim
    about *where* a call appears, not just that it exists. Nested functions
    report their own name rather than the outer one, which is what a reader
    checking the source would name too.

    Args:
        tree: A parsed module.

    Returns:
        ``(enclosing function name, call node)`` pairs. Calls at module scope
        report ``"<module>"``.
    """
    pairs: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                pairs.append((enclosing, child))
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
            else:
                visit(child, enclosing)

    visit(tree, "<module>")
    return pairs


def root_agent_guidance(agent_doc: str) -> str:
    """Resolve this repository's single shared guide through either entrypoint.

    This checks the committed import contract, not Claude's runtime loader.
    Reject extra adapter prose so a second policy source cannot grow unnoticed.
    """
    if agent_doc == "CLAUDE.md":
        assert (REPO_ROOT / agent_doc).read_text().strip() == "@AGENTS.md"
    return (REPO_ROOT / "AGENTS.md").read_text()


def carve_out_block(text: str) -> str:
    """Return just the dispatched-engine carve-out from a root agent doc.

    The shared guide opens the carve-out with a fixed sentence and closes it
    with the label-authority link, independently of paragraph wrapping.

    Args:
        text: Effective shared instructions from either entrypoint.

    Returns:
        The carve-out text, or the remainder of the file if the closing link is
        missing — an absent closer should surface as the specific clause
        assertion that fails, not as a silently empty block that passes nothing.
    """
    start = text.index(_CARVE_OUT_OPENER)
    end = text.find(_CARVE_OUT_CLOSER, start)
    return text[start:] if end == -1 else text[start : end + len(_CARVE_OUT_CLOSER)]


def frontmatter_name(text: str) -> str | None:
    """Return the ``name`` a ``SKILL.md`` declares in its YAML frontmatter.

    Deliberately not a YAML parse: the runtime contract is stdlib-only, and the
    frontmatter this reads is three or four flat scalar keys. Scanning the
    delimited block for one ``key: value`` line is the whole requirement, and it
    fails as a missing name rather than as an import error on a machine with no
    YAML library.

    Args:
        text: Raw contents of a ``SKILL.md``.

    Returns:
        The declared name, or ``None`` when the file has no frontmatter block or
        no ``name`` key inside it.
    """
    if not text.startswith("---\n"):
        return None
    _, _, rest = text.partition("---\n")
    block, sep, _ = rest.partition("\n---")
    if not sep:
        return None
    for line in block.splitlines():
        key, delimiter, value = line.partition(":")
        if delimiter and key.strip() == "name":
            return value.strip()
    return None


def normalize_prose(text: str) -> str:
    """Flatten markdown prose so phrase assertions survive editing.

    These are hand-wrapped documents. A named path is asserted because it states
    a contract, not because it is typeset a particular way — so reflowing a
    paragraph, bolding a term, or starting a sentence with it must not fail the
    assertion, while dropping the path entirely must. Lowercases, strips ``*``
    and backtick emphasis, and collapses whitespace runs (including the line
    wraps that split a phrase in two).

    Args:
        text: Raw markdown file contents.

    Returns:
        The text lowercased, stripped of emphasis markers, and whitespace-collapsed.
    """
    stripped = text.lower().replace("*", "").replace("`", "")
    return " ".join(stripped.split())


def runnable_lines(surface: Path) -> list[str | None]:
    """Yield each line of ``surface``, blanked to ``None`` unless it is runnable.

    "Runnable" means a line an operator would copy and execute: a non-comment
    line inside a fenced code block for markdown, or a non-comment line of a
    shell script. Prose that merely names a command — including inline code
    inside a sentence — is a description, and a doc that describes a command it
    tells you *not* to run must not read as prescribing it.

    The list keeps one entry per source line so callers can report the real line
    number of a violation.

    Args:
        surface: A markdown document or shell script to scan.

    Returns:
        One entry per line: the line itself when runnable, otherwise ``None``.
    """
    is_markdown = surface.suffix == ".md"
    in_fence = not is_markdown
    lines: list[str | None] = []
    for line in surface.read_text().splitlines():
        if is_markdown and line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append(None)
            continue
        stripped = line.lstrip()
        runnable = in_fence and not stripped.startswith(("#", "//"))
        lines.append(line if runnable else None)
    return lines
