"""Pure logic for the PR-pipeline review stage.

This module holds the bug-prone decisions a tick must make about a review
result — schema validation, verdict→label mapping, iteration counting, base-SHA
drift, trusted-candidate gating, marker-based admission, and dispatch-liveness
classification — as small, deterministic, unit-tested functions. The
``pr-pipeline:tick`` skill shells out to the CLI at the bottom of this file rather
than re-deriving this logic in inline bash each tick, so an unattended loop behaves
identically every run.

Stdlib only: the directory installs against a target repo it does not own, so
it takes no dependency on any target's toolchain and needs nothing but an
interpreter at or above the 3.11 floor. ``resolve-python.sh`` picks one for
every entry point that shells out to this module.
"""

from __future__ import annotations

import sys

# Must precede `import tomllib` below, which is where a too-old interpreter
# actually breaks. `ModuleNotFoundError: tomllib` reads as a broken install and
# sends an operator to reinstall, but the cause is almost always PATH
# precedence — a 3.9 on `python3` while a 3.13 sits right there — and a
# reinstall cannot fix that. Name the requirement instead.
#
# The floor is a named constant rather than an inline tuple so a type checker
# configured for an older Python cannot fold the comparison to a constant and
# mark everything below as unreachable — which silently drops the whole module
# from analysis on exactly the misconfigured box this guard exists for.
_PYTHON_FLOOR = (3, 11)
if sys.version_info < _PYTHON_FLOOR:  # pragma: no cover - not reached above the floor
    raise SystemExit(
        "pr-pipeline requires Python >= 3.11 (tomllib); this interpreter is "
        f"{'.'.join(str(part) for part in sys.version_info[:3])} "
        f"at {sys.executable}.\n"
        "Resolve one with "
        "`bash $PR_PIPELINE_HOME/skills/tick/resolve-python.sh`, "
        "or export PR_PIPELINE_PYTHON with the path to a >= 3.11 interpreter."
    )

import argparse
import datetime
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

import native_skills
import review_reports
import fixer_reports


@dataclass(frozen=True)
class ResolvedTarget:
    """One GitHub repository and its uniquely matching local Git remote."""

    repo: str
    remote: str


def same_repository(left: object, right: str) -> bool:
    """GitHub owner/repository identity is case-insensitive."""
    return isinstance(left, str) and left.casefold() == right.casefold()


def resolve_target(cwd: str | Path | None = None) -> ResolvedTarget:
    """Resolve local Git identity using the dashboard's interpreter-free helper."""
    result = subprocess.run(
        ["bash", str(Path(__file__).resolve().parents[1] / "resolve-target.sh")],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip())
    repo, remote = result.stdout.splitlines()
    return ResolvedTarget(repo, remote)


def validate_target_context(
    target: ResolvedTarget,
    *,
    cwd: str | Path,
    env: Mapping[str, str] | None = None,
) -> None:
    """Refuse destinations that differ in the actual Git execution context.

    Unlike startup resolution, this stays in ``cwd``. Engines must supply their
    child environment too, because Git's effective URLs depend on both inputs.
    Only local Git configuration is read; no transport is opened.
    """
    result = subprocess.run(
        ["bash", str(Path(__file__).resolve().parents[1] / "resolve-target.sh"),
         "--check-remote", target.repo, target.remote],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip())


# Verdict values the Codex reviewer emits (pinned by review-schema.json).
VERDICT_READY = "ready_for_human"
VERDICT_CHANGES = "changes_requested"
_VALID_VERDICTS = frozenset({VERDICT_READY, VERDICT_CHANGES})
_VALID_SEVERITIES = frozenset({"blocker", "major", "minor"})

Verdict = Literal["ready_for_human", "changes_requested", "needs_human_decision"]

# Pipeline labels.
LABEL_AVAILABLE = "ai:available"  # pre-dispatch label for the implement stage
LABEL_IMPLEMENTING = "ai:implementing"  # in-flight: an implementer engine is dispatched
LABEL_IMPLEMENTED = "ai:implemented"  # pre-dispatch label for the review stage
LABEL_REVISED = "ai:revised"  # alternate pre-dispatch label (re-review after a fix)
LABEL_REVIEWING = "ai:reviewing"  # in-flight: a reviewer engine is dispatched
LABEL_FIXING = "ai:fixing"  # in-flight: a fixer engine is dispatched
LABEL_READY_FOR_HUMAN = "ai:ready-for-human"
LABEL_CHANGES_REQUESTED = "ai:changes-requested"
LABEL_MAX_ITERATIONS = "ai:max-iterations"
LABEL_NEEDS_HUMAN = "ai:needs-human-decision"  # escalation terminal
LABEL_BLOCKED = "ai:blocked"  # human-set: waiting on a dependency or decision

# Provisioning, startup verification, and status share this complete vocabulary.
WORKFLOW_LABELS = frozenset(
    value for name, value in globals().copy().items() if name.startswith("LABEL_")
)

# Human holds. The tick never produces or targets ai:blocked, and it only pairs
# ai:needs-human-decision with an in-flight label when its own harvest target is
# that label — so either hold coexisting with pipeline custody is
# treated as human-set, and outranks the dispatch marker.
#
# "Treated as" rather than "is": a dispatched implementer or fixer runs under the
# operator's full environment and can reach `gh pr edit`, so it can label its own
# item mid-run. Nothing here can tell that apart from a human hold — engines share
# the tick's token, so even the timeline actor matches. The engine prompts and
# pr-guidelines.md ("Pipeline label authority") close the engine-set route by
# contract; the runtime rule stays fail closed toward the human, because a human
# racing to stop a bad run must win.
#
# Neither hold is automation's to move: convergence refuses every target while
# either is present, including an escalation to LABEL_NEEDS_HUMAN itself, so one
# hold can never be swapped for the other. LABEL_NEEDS_HUMAN remains the label
# the operator sees named first when both are present.
HOLD_LABELS = frozenset({LABEL_NEEDS_HUMAN, LABEL_BLOCKED})

# The in-flight label each pipeline stage claims its item with. Sole source of
# truth for the stage/label pairing; IN_FLIGHT_LABELS below is derived from it so
# a new stage cannot be added to one and forgotten in the other.
STAGE_IN_FLIGHT_LABELS = {
    "review": LABEL_REVIEWING,
    "implement": LABEL_IMPLEMENTING,
    "fix": LABEL_FIXING,
}

# In-flight labels: each one means "an engine holds this item". Any of them on a
# dispatch candidate is a claim by *some* machine, whose dispatch marker lives on
# that machine's disk — so a local marker scan cannot disprove it. The tick
# therefore never converges such an item, which means a partial transition (the
# in-flight label added, the pickup label not yet removed) now persists for the
# life of that engine and keeps matching the pickup query. Dispatch therefore
# reads this set directly rather than inferring freedom from a missing marker.
IN_FLIGHT_LABELS = frozenset(STAGE_IN_FLIGHT_LABELS.values())

# The pickup (pre-dispatch) labels each stage dispatches from, and the GitHub
# entity each stage operates on. Sole source of truth for both pairings: the
# queue listing, the dispatch eligibility gate, and every cadence probe derive
# from these two tables, so none of them can drift into a different idea of what
# counts as pickup-eligible work.
STAGE_PICKUP_LABELS: dict[str, tuple[str, ...]] = {
    "implement": (LABEL_AVAILABLE,),
    "review": (LABEL_IMPLEMENTED, LABEL_REVISED),
    "fix": (LABEL_CHANGES_REQUESTED,),
}

STAGE_ENTITIES: dict[str, str] = {
    "implement": "issue",
    "review": "pr",
    "fix": "pr",
}

# Every pickup label an entity of this type can be dispatched from, across all
# stages. Eligibility counts pickup labels over this set rather than over one
# stage's, because two stages that each see exactly one of their own labels
# would each claim the same item: a PR at ai:implemented + ai:changes-requested
# satisfied review and fix independently, and the second engine then met the
# first's live marker.
ENTITY_PICKUP_LABELS: dict[str, frozenset[str]] = {
    entity: frozenset(
        label
        for stage, labels in STAGE_PICKUP_LABELS.items()
        for label in labels
        if STAGE_ENTITIES[stage] == entity
    )
    for entity in sorted(set(STAGE_ENTITIES.values()))
}

# Shipped defaults for the operator-tunable values. A tick reads the operator's
# values from ResolvedConfig (keys of the same name in lower case, see
# _INT_SETTINGS) and passes them to every consumer; these constants answer only
# for a packaged floor that omits a key and for direct callers of the helpers.
#
# Loop cadence, in seconds except the stop count. The short poll is the wake
# while work is queued, at capacity, or unknown; the max poll caps the wait
# while an engine is in flight; the heartbeat is the quiescent wake.
LOOP_SHORT_POLL_SECONDS = 270
LOOP_MAX_POLL_SECONDS = 270
LOOP_HEARTBEAT_SECONDS = 30 * 60
LOOP_QUIESCENT_STOP_AFTER = 3
# Stage deadlines recorded in each dispatch marker.
IMPLEMENTER_TIMEOUT_SECONDS = 60 * 60
FIXER_TIMEOUT_SECONDS = 60 * 60
REVIEW_TIMEOUT_SECONDS = 60 * 60
# Engine stall detection; see tick.ENGINE_STALL_SECONDS for the
# reasoning behind the shipped threshold.
ENGINE_STALL_SECONDS = 30 * 60
ENGINE_STALL_STARTUP_GRACE_SECONDS = 5 * 60
# The longest silence a healthy engine can produce: Claude Code's 600s cap on
# one Bash call. Derived from the harness, not tunable; a configured stall
# threshold must exceed it (see tick.MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS).
MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS = 10 * 60
# Provider cooldown after a transient (quota or rate-limit) engine result: the
# fallback length, and the margin added to a reset time the provider names.
TRANSIENT_COOLDOWN_SECONDS = 60 * 60
TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS = 5 * 60

# Implementer/fixer engines a route may name. ``claude-ds`` is ``claude``
# routed to DeepSeek; it has no operator default model to inherit, so a blank
# model means this pin (see :func:`effective_engine_model`).
ENGINE_ROUTES = ("claude", "claude-ds")
CLAUDE_DS_DEFAULT_MODEL = "deepseek-v4-pro[1m]"

# A model id is its own argv element, so quoting cannot leak into an expression
# and provider-qualified ids (openai/gpt-5.1-codex, qwen3:30b) are fine. What
# argv *is* vulnerable to is a value read as an option, so the first character
# must not be a dash. Shared by the engine ``--model`` and the reviewer model.
ENGINE_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/\[\]-]*")
# A reviewer reasoning effort, interpolated into a ``-c
# model_reasoning_effort="<v>"`` TOML expression, so it must be a bare token: a
# quote or newline could close the string and append an unreviewed config key.
REVIEW_EFFORT_RE = re.compile(r"[A-Za-z0-9._\-\[\]]+")


class CadenceSettings(TypedDict, total=False):
    """Cadence keyword arguments shared by :func:`compute_next_wake` and
    :func:`decide_loop_cadence`. An absent key is the shipped default."""

    short_poll_seconds: int
    max_poll_seconds: int
    heartbeat_seconds: int
    quiescent_stop_after: int


def effective_engine_model(engine: str, model: str) -> str:
    """Return the model an implementer/fixer dispatch runs on this route.

    Args:
        engine: ``"claude"`` or ``"claude-ds"``.
        model: The configured model; blank means the engine's default.

    Returns:
        ``model`` when set. Blank stays blank for ``claude``, whose CLI then
        resolves the operator's own default, and is
        :data:`CLAUDE_DS_DEFAULT_MODEL` for ``claude-ds``.
    """
    if not model and engine == "claude-ds":
        return CLAUDE_DS_DEFAULT_MODEL
    return model


# Files that govern the reviewer's own behaviour. A PR that edits any of these is
# changing the very policy that decides whether it is promoted, so it must not be
# auto-reviewed under instructions it controls (see :func:`touches_review_policy`).
# Codex auto-loads ``AGENTS.md`` as its project doc and ``AGENTS.md`` routes to
# ``CLAUDE.md``, so those two are universal and are matched by basename at any
# directory level.
#
# A target repo's own review checklists are the same class of file but their
# paths are the target's to name — the pipeline drives a repository it does not
# own, so hard-coding one repo's checklist paths here would both miss every
# other repo's and carry a dead entry into every install. They are declared by
# the operator under ``checklist_paths``; see :func:`resolve_config`.
_POLICY_BASENAMES = frozenset({"CLAUDE.md", "AGENTS.md"})

# Where the install keeps the things a dispatched engine is told to read. Both
# are resolved from this module's own location — `lib/` is `skills/tick/lib/`,
# so its grandparent is the published skills root — because the install is a
# plugin cache directory outside every repository and the engines run with their
# cwd inside a checkout of somebody else's PR head. Nothing here counts
# directories from a *repository* root; the depth measured is the one this file
# ships at, which is fixed by the package layout.
_LIB_DIR = Path(__file__).resolve().parent
_INSTALL_ROOT = _LIB_DIR.parents[2]
INSTALL_SKILLS_ROOT = _INSTALL_ROOT / "skills"
PROMPTS_DIR = _LIB_DIR / "prompts"

# Discipline skills a dispatched engine's prompt binds through native loading. Skills are
# discretionary reading unless the executed prompt names when they must run, so
# each binding is a named constant substituted into the template (never
# hardcoded in the .txt) — a later change can move the binding into config
# without rewriting prompts.
REVISION_HYGIENE_SKILL = "review-response-hygiene"
DEFINITION_OF_DONE_SKILL = "definition-of-done"

# Every skill doc bound into a prompt, from every stage. This is the set the
# policy gate derives its entries from, so a skill bound by a future stage is
# protected by adding it here rather than by remembering to restate its path.
BOUND_SKILLS: tuple[str, ...] = (REVISION_HYGIENE_SKILL, DEFINITION_OF_DONE_SKILL)


def skill_doc_path(name: str) -> str:
    """Return the absolute path of one installed skill's ``SKILL.md``.

    The same string is both what the prompt tells the engine to read and what
    dispatch probes before launching, so a rename can never leave the two
    disagreeing about where the doc lives.

    It resolves inside the *install*, not inside the engine's worktree. The
    guidance ships with the pipeline, so the operator's installed copy is the
    correct one for every target repository by construction, and a target that
    has never heard of this skill still gets a fixer that can open it.

    Args:
        name: The skill directory name (e.g. ``"review-response-hygiene"``).

    Returns:
        The absolute path, e.g.
        ``/path/to/install/skills/review-response-hygiene/SKILL.md``.
    """
    return str(INSTALL_SKILLS_ROOT / name / "SKILL.md")


# The pipeline's own files are review policy too, matched by path *suffix* rather
# than exact path. The file that toggles the gate is policy: a PR flipping
# allow_touching to true must be escalated while the gate is enabled —
# otherwise a merged flip would silently disable the gate for every future
# policy-editing PR. The gate's implementation is the same vector: a PR that
# rewrites touches_review_policy to return False unconditionally (pipeline.py),
# or removes the gate call from the reviewer dispatch (tick.py), disables the
# gate exactly like a config flip.
#
# The suffix is what makes that hold. An exact path encodes one install depth
# (`.claude/skills/...`, where the skills were incubated) and silently exempts
# the identical edit in the pipeline's own repository, where they live at
# `skills/`, and under any plugin layout. Matching is per path segment, so
# `other-skills/tick/config.toml` is not a hit.
#
# The instructions the install *supplies* to a dispatched engine — every bound
# skill doc, and every prompt template — are the same class of file. Sourcing
# them from the install already stops a PR from governing its own fixer; what
# the gate adds is that a PR rewriting them for every *future* run is not
# auto-reviewed under the rules it is changing. Their entries are derived from
# the constants the runtime actually reads, so a skill bound by a future stage
# or a prompt added for one is covered by landing in the runtime rather than by
# being restated here.
_POLICY_PATH_SUFFIXES: tuple[tuple[str, ...], ...] = tuple(
    tuple(suffix.split("/"))
    for suffix in (
        "skills/tick/config.toml",
        "skills/tick/resolve-target.sh",
        "skills/tick/lib/pipeline.py",
        "skills/tick/lib/tick.py",
        "skills/tick/lib/native_skills.py",
    )
) + tuple(
    Path(skill_doc_path(name)).relative_to(_INSTALL_ROOT).parts
    for name in dict.fromkeys((*BOUND_SKILLS, *native_skills.REVIEW_SKILLS))
)

# Directories whose every file is policy, matched the same way but against the
# path's parent segments. The prompt templates are enumerated by what the
# directory holds at render time, not by a list, so naming the directory is the
# only way to gate a template that does not exist yet.
_POLICY_DIR_SUFFIXES: tuple[tuple[str, ...], ...] = (
    PROMPTS_DIR.relative_to(_INSTALL_ROOT).parts,
)

# The shipped floor: layer 1 of the resolution order, and the only config the
# install directory contributes. Resolved via __file__ so it is cwd-independent
# (the tick and its CLI helpers run from different cwds). It is read-only from
# the runtime's point of view — no operator-set value may land here, because a
# plugin install lands at ~/.claude/plugins/cache/<marketplace>/<plugin>/<hash>/
# and `/plugin update` installs the next version at a *different* hash, which
# would discard it silently. Operator values live in :func:`operator_config_path`.
_PACKAGED_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"

# Where the operator's own config lives, highest-precedence location first.
# $PR_PIPELINE_CONFIG names one file outright and exists so tests and one-off
# runs never write into the operator's real home; $CLAUDE_CONFIG_DIR relocates
# the Claude home as a whole.
_OPERATOR_CONFIG_ENV = "PR_PIPELINE_CONFIG"
_CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
_OPERATOR_CONFIG_RELATIVE = ("pr-pipeline", "config.toml")

# Logical verdict sentinels the harvest acts on (see design "Sentinel tokens").
SENTINEL_NO_BLOCKERS = "NO_BLOCKERS_READY_FOR_HUMAN"
SENTINEL_BLOCKERS = "BLOCKERS_FOUND"

# Implementer/fixer sentinel tokens emitted literally in result.json text.
SENTINEL_IMPL_COMPLETE = "IMPLEMENTATION_COMPLETE"
SENTINEL_FIXES_PUSHED = "FIXES_PUSHED_RECHECK_NEEDED"
SENTINEL_NO_CHANGES = "NO_CHANGES_RECHECK_NEEDED"
SENTINEL_NEEDS_HUMAN_DECISION = "NEEDS_HUMAN_DECISION"
OUTCOME_UNRECOGNIZED = "unrecognized"
_IMPLEMENTATION_SENTINELS = (SENTINEL_IMPL_COMPLETE, SENTINEL_NEEDS_HUMAN_DECISION)
_FIXER_SENTINELS = (
    SENTINEL_FIXES_PUSHED, SENTINEL_NO_CHANGES, SENTINEL_NEEDS_HUMAN_DECISION,
)
_HELD_COMPLETION_OUTCOMES = {
    "review": frozenset({VERDICT_READY, VERDICT_CHANGES, "needs_human_decision", OUTCOME_UNRECOGNIZED}),
    "implement": frozenset({*_IMPLEMENTATION_SENTINELS, OUTCOME_UNRECOGNIZED}),
    "fix": frozenset({*_FIXER_SENTINELS, OUTCOME_UNRECOGNIZED}),
}

# Fixed header the reviewer harvest stamps on every posted review comment. This is
# the durable, GitHub-visible authority for the fix-iteration count, so it must
# never change without a migration plan (see design "Iteration counter").
REVIEW_MARKER = "<!-- pr-pipeline:review -->"

# Per-comment idempotency key. Each review comment embeds the full review identity
# it was posted for — (diff base, base tip, head) SHAs plus the pinned base branch
# *name* — so a tick interrupted between posting the comment and flipping the label
# can recognise its own already-posted comment and re-apply only the label instead
# of posting a duplicate (which would inflate the iteration count). Every identity
# component matters, not the head alone: a base that advances under an unchanged
# head triggers a legitimate re-review, and a retarget to a base branch whose tip
# is the *same commit* changes none of the SHAs while still invalidating the old
# round. Keying on less would mistake such a fresh round for a replay and silently
# drop its findings, leaving the next fixer with a stale review as its handoff.
_ROUND_MARKER_PREFIX = "<!-- pr-pipeline:round:"

# Fixed header the fixer harvest stamps on every posted fixer comment. It is
# deliberately distinct from :data:`REVIEW_MARKER`: ``count_review_rounds`` tallies
# the iteration cap by counting marker-stamped comments, so a fixer comment sharing
# the reviewer's marker would inflate the round count and trip the runaway-spend
# valve early. Engine-authored text embedded in the body is stripped of HTML
# comments (see :func:`_sanitize_engine_report`) for the same reason.
FIX_MARKER = "<!-- pr-pipeline:fix -->"

# Fixed header for the attempt-cap escalation comment. Distinct from both markers
# above for the same reason: it is published by every stage's harvest, not only the
# fixer's, and it must never be counted as a review round.
ESCALATION_MARKER = "<!-- pr-pipeline:escalation -->"

# Compatibility budget for already-running legacy fixer dispatches only.
# New runs use fixer_reports.AUTHOR_ALLOWANCE and the shared rendered bound.
FIX_SUMMARY_BUDGET = 3000

# Slack between the budget the prompt states and the limit the excerpt is cut
# at, so a summary written *to* the budget still survives the completion
# sentinel and any blank lines the engine wraps around it. Without it the two
# numbers would be equal and every compliant summary would truncate by exactly
# the length of ``FIXES_PUSHED_RECHECK_NEEDED``.
_FIX_REPORT_HEADROOM = 200

# Upper bound on the engine-authored excerpt embedded in a fixer comment. The
# fixer's ``result`` text is unbounded model output; a PR comment is a shared
# record, not a transcript dump, and GitHub rejects bodies past 65536 characters.
_FIX_REPORT_LIMIT = FIX_SUMMARY_BUDGET + _FIX_REPORT_HEADROOM


@dataclass(frozen=True)
class TargetCadenceState:
    """One validated read of a target's active stage scopes.

    ``actionable=None`` means the target could not be read. ``terminal`` is
    true only when every active scope is closed or terminal.
    ``description`` names the target, its scopes and their observed states.
    """

    actionable: bool | None
    terminal: bool
    description: str


def decide_loop_cadence(
    *,
    in_flight_count: int,
    nearest_engine_deadline: float | None,
    actionable_queue: bool,
    at_cap: bool,
    quiescent: bool,
    consecutive_quiescent: int,
    now: float | None = None,
    quiescent_stop_after: int = LOOP_QUIESCENT_STOP_AFTER,
    held_items: Sequence[str] = (),
    short_poll_seconds: int = LOOP_SHORT_POLL_SECONDS,
    max_poll_seconds: int = LOOP_MAX_POLL_SECONDS,
    heartbeat_seconds: int = LOOP_HEARTBEAT_SECONDS,
    target_state: TargetCadenceState | None = None,
) -> tuple[int, bool]:
    """Choose the next sleep for unattended in-process loop mode.

    Args:
        in_flight_count: Number of items currently carrying in-flight labels.
        nearest_engine_deadline: Nearest dispatch deadline epoch seconds among
            identity-verified markers still classified ``live`` or
            ``launching``, or ``None``. Callers must not pass a deadline from a
            mismatched, dead, or hung marker: it is a fixed past timestamp, so
            it would pin the wake to the overdue floor indefinitely.
        actionable_queue: Whether any label queue can dispatch work now.
        at_cap: Whether *supervisable* occupancy is saturated. This is not the
            admission cap: human-held slots are discounted here on purpose, so
            that an all-held pipeline can still reach quiescence. Real
            admission saturation is reported through ``held_items``.
        quiescent: Whether there is no in-flight or actionable work.
        consecutive_quiescent: Number of qualifying quiet observations already
            counted, including the current one only if it qualifies.
        now: Epoch seconds for deadline math. Defaults to current time.
        quiescent_stop_after: Number of consecutive quiescent heartbeats after
            which the loop should stop.
        held_items: Operator-facing identifiers for the slots excluded from
            ``in_flight_count`` while a human hold is active.
        target_state: Target observation, forwarded to :func:`compute_next_wake`.
        short_poll_seconds: Forwarded to :func:`compute_next_wake`.
        max_poll_seconds: Forwarded to :func:`compute_next_wake`.
        heartbeat_seconds: Forwarded to :func:`compute_next_wake`.

    Returns:
        ``(sleep_seconds, stop)``.
    """
    nearest_deadline_s: float | None = None
    if nearest_engine_deadline is not None:
        current = time.time() if now is None else now
        nearest_deadline_s = max(1, math.ceil(nearest_engine_deadline - current))

    decision = compute_next_wake(
        in_flight_count=in_flight_count,
        nearest_deadline_s=nearest_deadline_s,
        has_actionable_queue=actionable_queue,
        at_capacity=at_cap,
        is_quiescent=quiescent,
        consecutive_quiescent_ticks=max(0, consecutive_quiescent) if quiescent else 0,
        count_quiescent_heartbeat=False,
        quiescent_stop_after=quiescent_stop_after,
        held_items=held_items,
        target_state=target_state,
        short_poll_seconds=short_poll_seconds,
        max_poll_seconds=max_poll_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )
    return decision.next_wake_seconds, decision.stop


# Completion envelope fields the engine wrapper must write after the engine exits.
# The envelope is the gate: its presence proves the engine exited and the wrapper
# confirmed exit status. Without it, a bare result file may be partially written.
_REQUIRED_ENVELOPE_FIELDS = frozenset({
    "dispatch_id", "exit_status", "wrapper_pid", "start_time", "result_valid",
})

_ALLOWED_ENVELOPE_KEYS = _REQUIRED_ENVELOPE_FIELDS
_REQUIRED_FAILURE_ENVELOPE_FIELDS = _REQUIRED_ENVELOPE_FIELDS | {"error_class"}


# --- Public TypedDicts and dataclasses (imported by tick.py) ---


class HarvestResult(TypedDict, total=False):
    """The outcome of harvesting one finished engine.

    Keys vary by stage. All stages include ``action`` and ``label``.
    Stage-specific keys (e.g. ``base_drift``, ``sentinel``, ``verdict``)
    are only present when the stage sets them.
    """

    action: Literal[
        "promote",
        "request_changes",
        "max_iterations",
        "discard",
        "escalate",
        "create_pr",
        "recheck",
        "retry",
    ]
    label: str
    sentinel: str | None
    failure_class: str
    post_comment: bool
    comment_body: str


class ClassifyResult(TypedDict):
    """The classified state of one dispatch marker after liveness check."""

    state: Literal["live", "launching", "complete", "stale", "hung"]
    identity: Literal["matched", "mismatched", "unknown"]


class ReconcileResult(TypedDict):
    """The action reconcile_dispatch prescribes for one in-flight PR/issue."""

    action: Literal[
        "noop",
        "leave",
        "harvest",
        "escalate",
        "revert",
        "kill_escalate",
        "cleanup",
        "kill_cleanup",
    ]
    label: str
    kill: bool


class CandidateMetadata(TypedDict):
    """PR metadata required by the trusted-candidate gate.

    Mirrors the shape of ``gh pr view --json author,headRepository,isCrossRepository``.
    """

    author: dict[str, object]
    headRepository: dict[str, object]
    isCrossRepository: bool


@dataclass(frozen=True)
class AdmissionResult:
    """The outcome of the dispatch admission gate.

    Attributes:
        can_dispatch: Whether the cap allows another engine to launch.
        reason: Empty string when ``can_dispatch`` is True; otherwise
            ``"max_in_flight"`` (the cap is reached) or ``"admission_error"``
            (the in-flight count could not be established, so admission fails
            closed).
        in_flight_count: Number of engines currently occupying a concurrency slot.
        max_in_flight: The configured ceiling.
    """

    can_dispatch: bool
    reason: str
    in_flight_count: int
    max_in_flight: int


@dataclass(frozen=True)
class InFlightRef:
    """A reference to one item that is currently in-flight (occupying a slot).

    Attributes:
        kind: ``"issue"`` or ``"pr"``.
        number: The GitHub issue or PR number.
        label: The in-flight pipeline label (e.g. ``ai:implementing``).
    """

    kind: Literal["issue", "pr"]
    number: int
    label: Literal["ai:implementing", "ai:reviewing", "ai:fixing"]


@dataclass(frozen=True)
class PickupEligibility:
    """Whether one item's label set proves it is free to be dispatched.

    Attributes:
        label: The single pickup label the item may be dispatched from, or
            ``""`` when it is not dispatchable.
        reason: Empty when ``label`` is set; otherwise why dispatch must skip.
        blocking: The labels that produced ``reason``, sorted. Empty when the
            item is dispatchable.
    """

    label: str
    reason: str
    blocking: tuple[str, ...]


def resolve_pickup_eligibility(
    labels: Iterable[str],
    *,
    entity: str,
    stage: str = "",
) -> PickupEligibility:
    """Decide, from an item's complete label set, whether it may be dispatched.

    The one eligibility rule the whole control plane shares. Dispatch calls it
    with a ``stage`` to answer "may *this* stage launch against this item?";
    cadence calls it without one to answer "can *any* stage?". They must agree:
    an item cadence counts as work but dispatch permanently refuses holds the
    loop short of its quiescent stop forever, polling work no tick can take.

    Pickup labels are counted across every stage for the entity, not within the
    calling stage. A PR at ``ai:implemented`` + ``ai:changes-requested`` shows
    exactly one review label and exactly one fix label, so a per-stage count
    admits it twice — and the second engine then meets the first's live marker.

    Every branch fails closed. The item simply waits for a tick that can prove
    it is free; nothing here mutates, and nothing here infers custody from the
    absence of local state.

    Args:
        labels: The item's complete label set, as read at the point of decision.
        entity: ``"issue"`` or ``"pr"``. An entity with no pickup vocabulary is
            never dispatchable.
        stage: The pipeline stage asking, or ``""`` to ask for any stage.

    Returns:
        A :class:`PickupEligibility` whose ``label`` is the resolved pickup
        label, or ``""`` with a ``reason`` and the ``blocking`` labels.
    """
    label_set = set(labels)

    in_flight = IN_FLIGHT_LABELS & label_set
    if in_flight:
        return PickupEligibility(
            label="",
            reason="in-flight label present; an engine already holds this item",
            blocking=tuple(sorted(in_flight)),
        )

    holds = HOLD_LABELS & label_set
    if holds:
        return PickupEligibility(
            label="",
            reason="human hold label present",
            blocking=tuple(sorted(holds)),
        )

    pickup = ENTITY_PICKUP_LABELS.get(entity, frozenset()) & label_set
    if len(pickup) != 1:
        return PickupEligibility(
            label="",
            reason="no single pickup label; cannot prove dispatch eligibility",
            blocking=tuple(sorted(pickup)),
        )

    resolved = next(iter(pickup))
    if stage and resolved not in STAGE_PICKUP_LABELS.get(stage, ()):
        return PickupEligibility(
            label="",
            reason="pickup label belongs to another stage",
            blocking=(resolved,),
        )
    return PickupEligibility(label=resolved, reason="", blocking=())


@dataclass(frozen=True)
class TickState:
    """Machine-readable end-of-tick state for supervised loops.

    All fields are strings so that unknown/uncomputed values can be
    represented as ``"unknown"`` on failed tick paths.

    Attributes:
        status: Tick exit status — ``"ok"``, ``"FAILED"``, or ``"PARTIAL"``.
        in_flight: Count of *supervisable* in-flight engines, or ``"unknown"``.
            Held slots are excluded — see ``held``.
        queue: ``"true"`` if any item is dispatch-eligible, ``"false"``
            if none are, or ``"unknown"``.
        quiescent: ``"true"`` when every tracked item is terminal and no
            engines are in flight, ``"false"`` otherwise, or ``"unknown"``.
        held: Count of slots excluded from ``in_flight`` because they are
            currently held by a human, or ``"unknown"``. They still
            occupy an admission slot, so the count is what explains an
            ``in_flight=0`` tick that still cannot dispatch at the cap.
    """

    status: str
    in_flight: str
    queue: str
    quiescent: str
    held: str = "unknown"

    def format_line(self) -> str:
        """Format as the parseable ``tick-state:`` stderr line."""
        return (
            f"tick-state: status={self.status}"
            f" in_flight={self.in_flight}"
            f" queue={self.queue}"
            f" quiescent={self.quiescent}"
            f" held={self.held}"
        )


def derive_tick_state(
    *,
    status: str,
    in_flight_count: int | None = None,
    has_actionable_queue: bool | None = None,
    held_count: int | None = None,
) -> TickState:
    """Derive machine-readable tick state from values the tick computes.

    Args:
        status: Tick exit status (``"ok"``, ``"FAILED"``, ``"PARTIAL"``).
        in_flight_count: Number of supervisable engines currently in flight,
            or ``None`` when the count could not be determined (failed tick).
        has_actionable_queue: Whether any item carries a dispatch-eligible
            label, or ``None`` when unknown.
        held_count: Number of occupied slots excluded from
            ``in_flight_count`` because they are currently held by a human, or
            ``None`` when the marker scan could not determine it.

    Returns:
        A ``TickState`` with all fields populated. Fields that could not
        be computed are set to ``"unknown"``.
    """
    if in_flight_count is not None:
        in_flight_str = str(in_flight_count)
    else:
        in_flight_str = "unknown"

    if has_actionable_queue is not None:
        queue_str = "true" if has_actionable_queue else "false"
    else:
        queue_str = "unknown"

    if in_flight_count is not None and has_actionable_queue is not None:
        quiescent_str = (
            "true" if in_flight_count == 0 and not has_actionable_queue
            else "false"
        )
    else:
        quiescent_str = "unknown"

    return TickState(
        status=status,
        in_flight=in_flight_str,
        queue=queue_str,
        quiescent=quiescent_str,
        held=(
            str(held_count) if held_count is not None else "unknown"
        ),
    )


# --- Cadence ---
#
# The loop must poll because engines are detached background processes the
# harness never signals on completion. The intervals are the operator's
# (ResolvedConfig loop_* keys; shipped defaults LOOP_* above): a cache-warm
# band under the prompt-cache TTL cliff while there is work, and a cache-cold
# heartbeat long enough to amortize a miss while there is none.

# The prompt-cache TTL cliff — must never be the chosen interval. Configured
# wakes may not equal it (see resolve_config); a deadline that lands on it
# while an engine is in flight steps back into the warm band instead.
_CADENCE_TTL_CLIFF = 300
_CADENCE_CLIFF_STEP_BACK = 270


@dataclass(frozen=True)
class CadenceDecision:
    """The outcome of computing the next wake interval for the tick loop.

    Attributes:
        next_wake_seconds: Seconds until the next tick should fire.
        stop: When ``True``, the loop should end — do not schedule a
            next wake. The caller should notify the developer.
        reason: Human-readable description of the decision for logging.
    """

    next_wake_seconds: int
    stop: bool
    reason: str

    def format_line(self) -> str:
        """Format as the parseable ``tick-cadence:`` stderr line."""
        return (
            f"tick-cadence: next_wake={self.next_wake_seconds}"
            f" stop={'true' if self.stop else 'false'}"
            f' reason="{self.reason}"'
        )


def compute_next_wake(
    *,
    in_flight_count: int | None,
    nearest_deadline_s: float | None = None,
    has_actionable_queue: bool | None = False,
    at_capacity: bool | None = False,
    is_quiescent: bool = False,
    consecutive_quiescent_ticks: int = 0,
    count_quiescent_heartbeat: bool = True,
    quiescent_stop_after: int = LOOP_QUIESCENT_STOP_AFTER,
    held_items: Sequence[str] = (),
    short_poll_seconds: int = LOOP_SHORT_POLL_SECONDS,
    max_poll_seconds: int = LOOP_MAX_POLL_SECONDS,
    heartbeat_seconds: int = LOOP_HEARTBEAT_SECONDS,
    target_state: TargetCadenceState | None = None,
    scope_description: str | None = None,
    scope_settled: bool = False,
) -> CadenceDecision:
    """Compute the next wake interval from end-of-tick state.

    The cadence is load-bearing because engines are detached background
    processes — the loop must poll, and the interval determines both
    convergence latency and spend. Never 300s.

    Decision table (first match wins; shipped defaults in parentheses):

    ======================================  ===================================
    State at end of tick                    Next wake
    ======================================  ===================================
    Target scopes terminal or closed       stop immediately
    ≥1 engine in-flight                    ``min(nearest deadline, max poll)``
                                            (max poll 270s)
    Actionable queue non-empty             short poll (270s)
    At ``max_in_flight`` with work           short poll (harvest to free a slot)
    Quiescent ``--only`` list, settled     stop immediately
    Quiescent                              heartbeat (1800s)
    Queue quiet for N heartbeats            stop (caller notifies developer)
    ======================================  ===================================

    An ``--only`` list uses the same table with two stops. A quiet tick
    whose listed items are all settled (terminal, held, closed, or handed
    off) stops at once. Otherwise the list stops after N quiet heartbeats of
    its own in which no listed item was dispatchable. Both stops require that
    no local engine is in flight, so the loop never exits owing a harvest.

    Args:
        in_flight_count: Number of engines currently occupying a concurrency
            slot (live + launching + hung), or ``None`` when the tick could not
            read marker state.
        nearest_deadline_s: Seconds from now until the nearest actionable
            in-flight deadline, or ``None`` when no marker is still
            ``live``/``launching`` or the deadline is unknown. A non-positive
            value means the deadline lapsed while the caller was scanning.
        has_actionable_queue: ``True`` when any item carries a
            dispatch-eligible label, or ``None`` when queue reads failed.
        at_capacity: ``True`` when ``in_flight_count >= max_in_flight``;
            ``None`` is treated as unknown.
        is_quiescent: ``True`` when no supervisable engines or actionable
            work remain. This alone does not prove target completion.
        consecutive_quiescent_ticks: Qualifying quiet observations counted
            before this tick (caller tracks this across ticks).
        count_quiescent_heartbeat: Whether this quiet tick qualifies as another
            observation. False for native-loop nudges, or when the caller has
            already included this tick in ``consecutive_quiescent_ticks``.
        quiescent_stop_after: Stop the loop after this many consecutive
            quiescent heartbeats.
        held_items: Operator-facing identifiers for slots excluded from
            ``in_flight_count`` because a human currently holds them. Named in
            *every* reason, because the reason line is the only thing the
            one-shot ``/loop`` path has to notify with. A quiescent stop is
            *caused* by a human holding these, and an unexplained stop leaves
            that engine unattended; a non-quiescent tick that is nonetheless
            wedged at the admission cap needs the same explanation, since
            ``in_flight_count`` has already discounted the cause.
        target_state: Validated target observation, or ``None`` for the full
            queue. A terminal target stops immediately, regardless of unrelated
            occupancy. An unfinished target never stops from quiet heartbeats;
            an unknown target stays on the short poll.
        scope_description: Description of an ``--only`` scope and its
            observed items, or ``None``. It prefixes every reason, and a
            quiescent stop names the list as exhausted.
        scope_settled: ``True`` when every item of the ``--only`` scope is
            settled. With ``scope_description`` set, a quiescent tick then
            stops immediately, without a quiet streak. Ignored for the full
            queue and for a positional target.
        short_poll_seconds: Wake while work is queued, at capacity, or when
            state is unknown.
        max_poll_seconds: Longest wait while an engine is in flight.
        heartbeat_seconds: Wake while quiescent.

    Returns:
        A :class:`CadenceDecision` with the next wake interval in seconds,
        whether the loop should stop, and a human-readable reason.
    """
    # Guard: never return exactly the TTL cliff.
    def _guard(seconds: int) -> int:
        if seconds == _CADENCE_TTL_CLIFF:
            return _CADENCE_CLIFF_STEP_BACK
        return seconds

    # Held items are discounted from in_flight_count, so without this suffix
    # every non-quiescent reason silently under-describes the tick. The
    # cap-saturated case is the one that needs it most: the loop reports an
    # ordinary warm poll while admission is in fact blocked by human custody
    # and no tick can ever dispatch until the operator acts.
    def _with_custody(reason: str) -> str:
        if target_state is not None:
            reason = f"{target_state.description}; not terminal; {reason}"
        elif scope_description is not None:
            reason = f"{scope_description}; {reason}"
        if not held_items:
            return reason
        return f"{reason}; awaiting human decision: {', '.join(held_items)}"

    # Unknown control-plane reads are not quiescence. Retry cache-warm so the
    # loop can recover promptly once GitHub or marker reads become trustworthy.
    if in_flight_count is None or has_actionable_queue is None or at_capacity is None:
        return CadenceDecision(
            next_wake_seconds=short_poll_seconds,
            stop=False,
            reason=_with_custody(f"state unknown -> wake {short_poll_seconds}s"),
        )

    # Completion is scoped to the target, while admission and supervision
    # remain global. Unrelated engines must not postpone a proven target stop.
    if target_state is not None:
        if target_state.terminal:
            return CadenceDecision(0, True, f"{target_state.description} → stop")
        if target_state.actionable is None:
            return CadenceDecision(
                short_poll_seconds, False,
                _with_custody(f"state unknown -> wake {short_poll_seconds}s"),
            )

    # 1. In-flight engines: pace to the nearest deadline, clamped to
    #    cache-warm so we never burn a cache miss on a distant deadline.
    if in_flight_count >= 1:
        if nearest_deadline_s is not None and nearest_deadline_s > 0:
            wake = min(int(nearest_deadline_s), max_poll_seconds)
        elif nearest_deadline_s is not None and nearest_deadline_s <= 0:
            wake = 1
        else:
            wake = max_poll_seconds
        # Guard against the TTL cliff before constructing the decision.
        wake = _guard(wake)
        return CadenceDecision(
            next_wake_seconds=wake,
            stop=False,
            reason=_with_custody(
                f"{in_flight_count} in-flight, "
                + (
                    f"nearest deadline {nearest_deadline_s:.0f}s → "
                    if nearest_deadline_s is not None
                    else ""
                )
                + f"wake {wake}s"
            ),
        )

    # 2. Actionable queue: poll promptly to dispatch.
    if has_actionable_queue:
        return CadenceDecision(
            next_wake_seconds=short_poll_seconds,
            stop=False,
            reason=_with_custody(
                f"actionable queue non-empty → wake {short_poll_seconds}s",
            ),
        )

    # 3. At capacity: poll to harvest and free a slot.
    if at_capacity:
        return CadenceDecision(
            next_wake_seconds=short_poll_seconds,
            stop=False,
            reason=_with_custody(
                f"at capacity ({in_flight_count} in-flight) → "
                f"wake {short_poll_seconds}s",
            ),
        )

    # 4. Quiescent: heartbeat slowly, stop after N consecutive.
    if is_quiescent:
        if target_state is not None:
            return CadenceDecision(
                heartbeat_seconds, False,
                _with_custody(f"waiting → wake {heartbeat_seconds}s"),
            )
        # A settled list is finished work, not an idle queue: stop on this
        # quiet tick, including a nudge tick, without a heartbeat streak.
        # Quiescence already proves that no local engine is in flight.
        if scope_description is not None and scope_settled:
            return CadenceDecision(
                next_wake_seconds=0,
                stop=True,
                reason=_with_custody(
                    "--only list settled: every listed item is terminal, held, "
                    "closed, or handed off and no local engine is in flight → stop"
                ),
            )
        # Observing quiet after a nudge does not count as another heartbeat.
        quiet_count = consecutive_quiescent_ticks + int(count_quiescent_heartbeat)
        stop = quiet_count >= quiescent_stop_after
        if scope_description is not None and stop:
            return CadenceDecision(
                next_wake_seconds=heartbeat_seconds,
                stop=True,
                reason=_with_custody(
                    "--only list exhausted: no listed item dispatchable and no "
                    "local engine in flight; quiescent (heartbeat "
                    f"{quiet_count}/{quiescent_stop_after}) → stop"
                ),
            )
        return CadenceDecision(
            next_wake_seconds=heartbeat_seconds,
            stop=stop,
            reason=_with_custody(
                f"quiescent (heartbeat {quiet_count}/{quiescent_stop_after})"
                + (" → stop" if stop else f" → wake {heartbeat_seconds}s")
            ),
        )

    # Fallback (should not be reachable with correct inputs).
    return CadenceDecision(
        next_wake_seconds=heartbeat_seconds,
        stop=False,
        reason=_with_custody("fallback → heartbeat"),
    )


@dataclass(frozen=True)
class StageConfig:
    """Parameterisation of reconcile_engine for one pipeline stage.

    Attributes:
        stage: Which pipeline stage this config drives.
        worktree_prefix: ``"pr"`` or ``"issue"`` — the worktree directory prefix.
        attempts_key_prefix: ``""`` for PR, ``"issue-"`` for issue — durable
            attempt-counter filename prefix.
        result_file: ``"review.json"`` or ``"result.json"`` — the engine output file.
        in_flight_label: The GitHub label set when an engine is dispatched.
        pre_label: The GitHub label that was on the item before dispatch.
        strict_dual_label: When True, re-read labels after dual-label convergence
            to verify exactly one ``ai:`` label remains before dropping scratch.
        tick_abort_on_enum_failure: When True, abort the entire tick on enumeration
            failure. When False, set ``reconcile_failed=1`` and continue.
    """

    stage: Literal["review", "implement", "fix"]
    worktree_prefix: str
    attempts_key_prefix: str
    result_file: str
    in_flight_label: str
    pre_label: str
    strict_dual_label: bool
    tick_abort_on_enum_failure: bool


def validate_completion_envelope(
    obj: object,
    *,
    expected_dispatch_id: str | None = None,
) -> bool:
    """Validate a completion envelope written by the engine wrapper.

    The envelope is the single signal that the engine exited cleanly and the
    wrapper confirmed the result. A bare result file without an envelope may be
    partially written or belong to a different dispatch.

    Args:
        obj: The parsed ``completion.json`` (any JSON value).
        expected_dispatch_id: When given, the envelope's ``dispatch_id`` must
            match this value exactly (links the envelope to its dispatch marker).

    Returns:
        ``True`` only when the envelope is structurally valid, records a
        successful exit with a valid result, and (when checked) belongs to
        the expected dispatch.
    """
    if not isinstance(obj, dict):
        return False
    if not set(obj.keys()) <= _ALLOWED_ENVELOPE_KEYS:
        return False
    if not _REQUIRED_ENVELOPE_FIELDS.issubset(obj.keys()):
        return False
    exit_status = obj.get("exit_status")
    if isinstance(exit_status, bool) or exit_status != 0:
        return False
    if obj.get("result_valid") is not True:
        return False
    dispatch_id = obj.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return False
    if expected_dispatch_id is not None and dispatch_id != expected_dispatch_id:
        return False
    wrapper_pid = obj.get("wrapper_pid")
    if not _is_positive_int(wrapper_pid):
        return False
    start_time = obj.get("start_time")
    if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
        return False
    if not math.isfinite(start_time):
        return False
    return True


def validate_failure_envelope(
    obj: object,
    *,
    expected_dispatch_id: str,
    report_protocol_version: int | None = None,
) -> bool:
    """Validate a wrapper-confirmed, dispatch-bound engine failure envelope.

    Args:
        obj: Parsed ``completion.json`` value.
        expected_dispatch_id: Dispatch marker identifier that must own the envelope.

    Returns:
        ``True`` only for a structurally valid nonzero-exit envelope whose wrapper
        rejected the result and whose dispatch identifier matches the marker.
    """
    if report_protocol_version is not None:
        obj = review_reports.envelope_base(obj, report_protocol_version, captured=False)
    if not isinstance(obj, dict):
        return False
    if set(obj.keys()) != _REQUIRED_FAILURE_ENVELOPE_FIELDS:
        return False
    exit_status = obj.get("exit_status")
    if isinstance(exit_status, bool) or not isinstance(exit_status, int) or exit_status == 0:
        return False
    if obj.get("result_valid") is not False:
        return False
    error_class = obj.get("error_class")
    if not isinstance(error_class, str) or not error_class:
        return False
    dispatch_id = obj.get("dispatch_id")
    if dispatch_id != expected_dispatch_id:
        return False
    wrapper_pid = obj.get("wrapper_pid")
    if not _is_positive_int(wrapper_pid):
        return False
    start_time = obj.get("start_time")
    if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
        return False
    return math.isfinite(start_time)


IdentityResult = Literal["matched", "mismatched", "unknown"]


def verify_process_identity(
    recorded_start: float,
    live_start: float | None,
    tolerance: float = 1.0,
) -> IdentityResult:
    """Determine whether a live process is the same one that was recorded.

    A bare PID check (``kill -0``) is vulnerable to PID reuse: the OS can
    assign the same PID to an unrelated process after the original exits.
    Comparing the recorded start time against the live process's start time
    (within a tolerance for clock granularity) proves identity.

    Three-valued result:

    - ``"matched"``: the live process start time is within tolerance of the
      recorded start time — same process, safe to signal.
    - ``"mismatched"``: both times are readable but differ beyond tolerance —
      confirmed PID reuse, must NOT signal.
    - ``"unknown"``: the live start time could not be read (``None``). The
      process may be alive or dead, but identity cannot be proven either way.
      Callers must retain custody and count the slot — never drive kill,
      cleanup, or requeue from ``"unknown"``.

    Args:
        recorded_start: Epoch seconds the wrapper recorded at startup.
        live_start: Epoch seconds of the live process's start time, or
            ``None`` if the process does not exist or the start time could
            not be read.
        tolerance: Maximum allowable difference in seconds between recorded
            and live start times (default 1.0).

    Returns:
        One of ``"matched"``, ``"mismatched"``, or ``"unknown"``.
    """
    if live_start is None:
        return "unknown"
    if abs(recorded_start - live_start) <= tolerance:
        return "matched"
    return "mismatched"


def _round_token(
    diff_base_sha: str,
    base_tip_sha: str,
    head_sha: str,
    base_ref: str = "",
    fix_round: str = "",
) -> str:
    """Return the hidden idempotency token for one reviewed round.

    The token covers the full review identity — the (diff anchor, freshness
    snapshot, head) SHA triple plus the pinned base branch name — so a
    re-dispatch that changes any component counts as a new round. The ref is
    included because it is part of review identity without being visible in
    the SHAs: two branch names can point at the same commit, and a review
    posted under one must not suppress the fresh review under the other.
    A git ref name can never contain ``:``, so the delimiter is unambiguous.

    Args:
        diff_base_sha: The merge-base the diff was anchored to.
        base_tip_sha: The base tip snapshot pinned at dispatch.
        head_sha: The reviewed head SHA.
        base_ref: The pinned base branch name. Empty keeps the legacy
            three-part format for markers that predate the ref key (the two
            formats never match each other, so a cross-format replay re-posts
            at most once — the accepted one-time upgrade edge).
        fix_round: Dispatch-derived token of the no-change fixer whose
            published reasoning this reviewer received. Empty preserves the
            existing SHA/ref identity; harvest retries reuse the pinned token.
    """
    identity = f"{diff_base_sha}:{base_tip_sha}:{head_sha}"
    if base_ref:
        identity = f"{identity}:{base_ref}"
    if fix_round:
        identity = f"{identity}:no-change:{fix_round}"
    return f"{_ROUND_MARKER_PREFIX}{identity} -->"

# Default runaway-spend safety valve: crossing this many completed review rounds
# routes the PR to ai:max-iterations instead of looping further.
MAX_ITERATIONS = 5

# Durable cap on *resultless* dispatch attempts for one PR (a dispatch that died
# without producing review.json). The comment-based round cap never counts these —
# a repeatable infrastructure failure posts no comment — so this separate ceiling
# stops an endlessly failing engine from re-dispatching (and re-spending) forever.
MAX_DISPATCH_ATTEMPTS = 3


def validate_review(obj: object, *, report_protocol_version: int | None = None) -> Verdict | None:
    """Validate a parsed ``review.json`` against the reviewer output schema.

    A defensive re-check of what ``codex exec --output-schema`` should already
    guarantee: the harvest must never promote a PR on malformed output. New
    reports use the dispatch-pinned protocol, and failures route to a human
    decision. Missing version retains the already-running legacy gates.

    Args:
        obj: The object parsed from ``review.json`` (any JSON value).

    Returns:
        The validated verdict string, or ``None`` if ``obj`` does not conform to
        ``review-schema.json`` (missing/unknown ``verdict``, a missing or
        non-list ``findings`` array, or a malformed ``findings`` entry).
    """
    if report_protocol_version is not None:
        if type(report_protocol_version) is not int or report_protocol_version != review_reports.VERSION:
            return None
        return None if review_reports.validation_problem(obj) else obj["verdict"]
    if not isinstance(obj, dict) or "report_protocol_version" in obj:
        return None
    verdict = obj.get("verdict")
    if not isinstance(verdict, str) or verdict not in _VALID_VERDICTS:
        return None
    # ``findings`` is required by review-schema.json. A payload missing it (or with
    # the wrong type) did not come through the schema-constrained reviewer path, so
    # the promote guard rejects it rather than trust truncated/drifted output.
    if "findings" not in obj:
        return None
    findings = obj.get("findings")
    if not isinstance(findings, list):
        return None
    for finding in findings:  # type: ignore[var-annotated]
        if not _valid_finding(finding):
            return None
    # verdict is one of the literal values by the membership check above.
    return verdict  # type: ignore[return-value]


def trusted_comment_history(comments: object, *, trusted_author: str) -> list[dict[str, Any]]:
    """Validate complete history and select the existing operator's comments.

    Known foreign authors are ignored. Operator comments require explicit unedited
    GraphQL provenance; even a removed header must not erase a round. Unknown
    authors or malformed entries raise ValueError: discarding them could erase
    genuine historical rounds.
    Callers must retain evidence and stop spend/promotion on that exception.
    The I/O owner must establish pagination completeness before calling this.
    """
    if not isinstance(trusted_author, str) or not _LOGIN_RE.fullmatch(trusted_author):
        raise ValueError("comment history has no trusted operator identity")
    if not isinstance(comments, list):
        raise ValueError("comment history is not an array")
    trusted: list[dict[str, Any]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise ValueError("comment history contains a malformed entry")
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if not isinstance(login, str) or not (
            _LOGIN_RE.fullmatch(login)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*\[bot\]", login)
        ):
            raise ValueError("comment history contains an unknown author")
        if not isinstance(comment.get("body"), str):
            raise ValueError("comment history contains a malformed body")
        if comment.get("_input_source") == "review":
            continue  # Submitted reviews are human input, never controller history.
        if login == trusted_author:
            # Validate even ordinary notes: an edit may have erased a review
            # header. Missing provenance cannot silently remove a genuine round.
            if not {"lastEditedAt", "editor", "includesCreatedEdit"} <= comment.keys():
                raise ValueError("operator comment has unknown edit provenance")
            if (comment["lastEditedAt"] is not None or comment["editor"] is not None
                    or comment["includesCreatedEdit"] is not False):
                raise ValueError("operator comment is edited or has invalid edit provenance")
            trusted.append(comment)
    return trusted


def trusted_marker_comments(
    comments: object, marker: str, *, trusted_author: str,
) -> list[dict[str, Any]]:
    """Select operator comments whose first line is the exact marker header."""
    return [comment for comment in trusted_comment_history(
        comments, trusted_author=trusted_author,
    ) if comment["body"] == marker
        or comment["body"].startswith((marker + "\n", marker + "\r\n"))]


# Any controller marker, wherever it appears in a body. The pipeline posts as
# the operator, so a comment carrying one is a pipeline action (or a quotation
# of one), never an approved reviewer's instruction.
_PIPELINE_MARKER_RE = re.compile(r"<!--\s*pr-pipeline:")

# The fixer-input fingerprint a fix comment pins in its controller header.
_FIX_REVIEW_PIN_RE = re.compile(r"^<!-- pr-pipeline:fix-review ([a-f0-9]{64}) -->$", re.M)
_APPROVED_INPUT_PIN_RE = re.compile(r"^<!-- pr-pipeline:approved-input ([a-f0-9]{64}) -->$", re.M)


def carries_pipeline_marker(body: str) -> bool:
    """Return whether a comment body carries any pipeline control marker."""
    return _PIPELINE_MARKER_RE.search(body) is not None


def approved_comment_provenance(comment: dict) -> bool:
    """Require explicit unedited provenance or a known edit by the author."""
    if not {"lastEditedAt", "editor", "includesCreatedEdit"} <= comment.keys():
        raise ValueError("approved comment has unknown edit provenance")
    edited, editor, includes = (comment["lastEditedAt"], comment["editor"],
                                comment["includesCreatedEdit"])
    if type(includes) is not bool:
        raise ValueError("approved comment has invalid edit provenance")
    if edited is None and editor is None and includes is False:
        return True
    if not isinstance(edited, str) or not isinstance(editor, dict):
        raise ValueError("approved comment has unknown edit provenance")
    try:
        timestamp = datetime.datetime.fromisoformat(edited.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise ValueError("approved comment has invalid edit provenance") from exc
    login = editor.get("login")
    if not isinstance(login, str) or not _LOGIN_RE.fullmatch(login):
        raise ValueError("approved comment has unknown edit provenance")
    return login.casefold() == comment["author"]["login"].casefold()


def _approved_input_body(body: str) -> bool:
    """Allow only an optional leading review marker, never control metadata."""
    if body == REVIEW_MARKER:
        return False
    if body.startswith((REVIEW_MARKER + "\n", REVIEW_MARKER + "\r\n")):
        body = body[len(REVIEW_MARKER):]
    return bool(body.strip()) and not carries_pipeline_marker(body)


def fixer_input_comment(
    comments: object, *, trusted_author: str,
    approved_reviewers: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Select the comment the next fixer must act on, or ``None``.

    Two kinds of comment qualify:

    - ``"review"``: an operator comment headed by :data:`REVIEW_MARKER`.
    - ``"approved"``: a comment from the operator or a login in
      ``approved_reviewers``, or their submitted change-request/comment review.
      It can carry one leading review marker but no controller metadata, and
      must be newer than the last operator pipeline action. Comments a pipeline
      action already answered are not selected again.

    The newer kind wins: an approved comment after the last pipeline action
    always follows the latest review. Only operator conversation comments
    supply controller metadata; approved inputs supply one finding, C1. An
    approved comment that somebody else edited is not fixer input.

    Raises:
        ValueError: The history is malformed, has an unknown author, an edited
            operator comment, or unknown edit provenance on an approved comment.

    Returns:
        ``{"kind": "review" | "approved", "comment": <entry>}`` or ``None``.
    """
    trusted_comment_history(comments, trusted_author=trusted_author)
    assert isinstance(comments, list)  # Validated above.
    approved = {login.casefold() for login in approved_reviewers}
    approved.add(trusted_author.casefold())
    review: dict[str, Any] | None = None
    latest_approved: dict[str, Any] | None = None
    for comment in comments:
        login = comment["author"]["login"]
        body = comment["body"]
        submitted = comment.get("_input_source") == "review"
        if submitted and comment["state"] not in {"CHANGES_REQUESTED", "COMMENTED"}:
            continue
        if (carries_pipeline_marker(body) and login == trusted_author and not submitted
                and not _approved_input_body(body)):
            latest_approved = None
            if body == REVIEW_MARKER or body.startswith(
                (REVIEW_MARKER + "\n", REVIEW_MARKER + "\r\n"),
            ):
                review = comment
            continue
        if login.casefold() not in approved:
            continue
        if not approved_comment_provenance(comment) or not _approved_input_body(body):
            continue
        latest_approved = comment
    if latest_approved is not None:
        return {"kind": "approved", "comment": latest_approved}
    if review is not None:
        return {"kind": "review", "comment": review}
    return None


def fixer_input_handoff(body: str, *, kind: str = "review") -> dict:
    """Pin the finding identity of a selected fixer input body.

    A selected pipeline report keeps its controller finding set. An approved
    input is one finding, even when it starts with the review marker. The caller
    must pass the authenticated selection kind, not infer authority from text.
    """
    lines = body.replace("\r\n", "\n").split("\n", 1)
    if kind == "review" and lines[0] == REVIEW_MARKER:
        return review_reports.handoff(body)
    if not _approved_input_body(body):
        raise ValueError("A marked comment is not an approved reviewer comment.")
    return review_reports.comment_handoff(body)


def approved_input_identity(comment: dict) -> str:
    """Fingerprint the immutable GraphQL source ID, independent of body text."""
    source_id = comment.get("id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("approved input has no source identity")
    return hashlib.sha256(("id:" + source_id).encode()).hexdigest()


def approved_input_pin(body: str) -> str:
    """Read source identity only from the controller header of a fix comment."""
    header = body.replace("\r\n", "\n").split("\n\n", 1)[0]
    pins = _APPROVED_INPUT_PIN_RE.findall(header)
    if "<!-- pr-pipeline:approved-input" in header and len(pins) != 1:
        raise ValueError("invalid approved-input identity in fix header")
    return pins[0] if pins else ""


def approved_input_rounds(comments: list[object], *, trusted_author: str) -> set[str]:
    """Source identities authenticated by operator-owned fix headers."""
    return {pin for entry in trusted_marker_comments(
        comments, FIX_MARKER, trusted_author=trusted_author,
    ) if (pin := approved_input_pin(entry["body"]))}


def count_review_rounds(
    comments: list[object], marker: str = REVIEW_MARKER, *, trusted_author: str,
    approved_input_reservations: Iterable[str] = (),
) -> int:
    """Count review tokens and distinct approved sources; deduplicate retries.

    New fix headers pin source IDs, so equal bodies on separate comments count
    separately even after deletion. Historical headers keep their body-pin
    fallback. Controller-owned local reservations also count until publication;
    source IDs deduplicate them against fix headers and operator reviews.
    """
    tokens: set[str] = set()
    legacy = 0
    review_digests: set[str] = set()
    review_sources: set[str] = set()
    for comment in trusted_marker_comments(comments, marker, trusted_author=trusted_author):
        review_digests.add(hashlib.sha256(comment["body"].encode()).hexdigest())
        if comment.get("id"):
            review_sources.add(approved_input_identity(comment))
        lines = comment["body"].splitlines()
        token = lines[1] if len(lines) > 1 else ""
        if token.startswith(_ROUND_MARKER_PREFIX) and token.endswith(" -->"):
            tokens.add(token)
        else:
            legacy += 1
    if marker != REVIEW_MARKER:
        return legacy + len(tokens)
    legacy_pins: set[str] = set()
    for comment in trusted_marker_comments(comments, FIX_MARKER, trusted_author=trusted_author):
        if not approved_input_pin(comment["body"]):
            header = comment["body"].replace("\r\n", "\n").split("\n\n", 1)[0]
            legacy_pins.update(_FIX_REVIEW_PIN_RE.findall(header))
    return (legacy + len(tokens) + len(legacy_pins - review_digests)
            + len((approved_input_rounds(comments, trusted_author=trusted_author)
                   | set(approved_input_reservations)) - review_sources))


def at_max_iterations(rounds: int, threshold: int = MAX_ITERATIONS) -> bool:
    """Return whether the fix-iteration count has reached the runaway-spend cap.

    Args:
        rounds: Completed review rounds (from :func:`count_review_rounds`).
        threshold: The cap; defaults to :data:`MAX_ITERATIONS`.

    Returns:
        ``True`` if the PR should route to ``ai:max-iterations`` rather than loop.
    """
    return rounds >= threshold


def base_advanced(pinned_sha: str, current_tip_sha: str) -> bool:
    """Return whether the PR's base moved since the review was dispatched.

    Reviewing against a stale base is the unattended-loop failure mode: a PR can
    be declared clean and promoted while its real base has moved underneath it.
    The dispatch pins ``base_sha``; the harvest resolves the current base tip and
    calls this. The caller resolves SHAs with git, so this stays a pure equality
    check — an unresolved (empty) tip is treated as drift, never as safe.

    Args:
        pinned_sha: The base SHA recorded in ``dispatch.json`` at review dispatch.
        current_tip_sha: The base ref tip resolved now, at harvest.

    Returns:
        ``True`` if the review must be discarded and the PR re-dispatched.
    """
    if not current_tip_sha:
        return True
    return pinned_sha != current_tip_sha


def head_advanced(pinned_sha: str, current_sha: str) -> bool:
    """Return whether the PR head moved since the review was dispatched.

    The base-drift guard (:func:`base_advanced`) is not enough on its own: a PR can
    be force-pushed or revised externally *while the review runs*, so a clean verdict
    would otherwise promote a head the reviewer never saw. The dispatch checks out
    and pins the exact head SHA; the harvest resolves the live head and calls this.

    Args:
        pinned_sha: The head SHA recorded in ``dispatch.json`` at review dispatch.
            Empty only in unit tests — a production dispatch always pins the head.
        current_sha: The PR head resolved now, at harvest.

    Returns:
        ``True`` if the review must be discarded and the PR re-dispatched. With no
        pinned head the gate cannot assert drift and returns ``False``; a pinned but
        now-unresolvable head is treated as drift (never as safe).
    """
    if not pinned_sha:
        return False
    if not current_sha:
        return True
    return pinned_sha != current_sha


def has_blocking_findings(findings: list[object]) -> bool:
    """Return whether any finding is severity ``blocker`` or ``major``.

    The promotion gate relies on this rather than trusting the reviewer's stated
    ``verdict`` field: ``review-schema.json`` defines ``ready_for_human`` as "no
    blocker/major issues", but a model can emit a ``ready_for_human`` verdict
    alongside a blocker finding. Promoting on the verdict alone would ship that
    contradiction to a human as "clean", so the harvest derives blocker presence
    from the findings themselves and refuses to promote when any exists.

    Args:
        findings: The reviewer's ``findings`` array (entries may be malformed).

    Returns:
        ``True`` if at least one well-formed finding has a blocking severity.
    """
    for finding in findings:
        if isinstance(finding, dict) and finding.get("severity") in ("blocker", "major"):
            return True
    return False


def review_already_posted(
    comments: list[object],
    base_sha: str,
    head_sha: str,
    marker: str = REVIEW_MARKER,
    *,
    base_tip_sha: str = "",
    base_ref: str = "",
    fix_round: str = "",
    trusted_author: str,
) -> bool:
    """Return whether this exact round's review comment was already posted.

    Makes the harvest replay-safe: if a prior tick posted the review comment but
    crashed (or hit a GitHub error) before flipping the label, the next tick re-reads
    the same result and must not post a second comment for the same round — that would
    inflate the comment-derived iteration count and could trip the cap early. The
    posted comment embeds a hidden per-round token over the *(diff base, base tip,
    head, base ref)* identity (see :func:`render_review_comment`), so this is an exact
    match, not a fuzzy substring scan. Keying on the bases too means a base that
    advanced under an unchanged head is recognised as a new round rather than
    suppressed as a replay; keying on the base *name* means a retarget to a branch
    whose tip is the same commit is recognised as a new round as well.

    Args:
        comments: Complete normalized history with author, body, and edit provenance.
        base_sha: The diff anchor (merge-base) the round was reviewed against.
        head_sha: The reviewed head SHA whose comment to look for.
        marker: The fixed review-comment header; defaults to :data:`REVIEW_MARKER`.
        base_tip_sha: The base tip snapshot pinned at dispatch. Defaults to
            ``base_sha`` for markers written before the two-SHA split (where the
            single pinned base was the tip and the diff anchor at once).
        base_ref: The base branch name pinned at dispatch. Empty matches only
            the legacy three-part token format (markers without ``base_ref``).
        fix_round: No-change fixer identity pinned at reviewer dispatch.
        trusted_author: Operator posting identity validated by the caller.

    Returns:
        ``True`` if a marker-stamped comment carrying this round's token exists.
    """
    marked = trusted_marker_comments(comments, marker, trusted_author=trusted_author)
    if not head_sha:
        return False
    token = _round_token(
        base_sha, base_tip_sha or base_sha, head_sha, base_ref, fix_round,
    )
    for comment in marked:
        lines = comment["body"].splitlines()
        if len(lines) > 1 and lines[1] == token:
            return True
    return False


def classify_dispatch(
    *,
    now: float,
    deadline: float,
    pid_alive: bool,
    result_exists: bool,
    state: str = "",
    envelope_valid: bool = False,
    failure_envelope_valid: bool = False,
) -> Literal["complete", "failed", "live", "stale", "hung", "launching"]:
    """Classify the state of a dispatched engine from its marker and liveness.

    Drives both harvest and stale/hung recovery:

    - ``complete``: a valid completion envelope exists AND the result file is
      present, proving the wrapper confirmed the engine exited successfully.
      Both conditions are required: a bare result without an envelope may be
      partially written, and an envelope without a result file means the result
      was deleted or never materialised despite the wrapper's claim.
    - ``failed``: a dispatch-bound failure envelope records a nonzero wrapper exit
      and invalid result after the reviewer process has died.
    - ``live``: still running within its deadline — leave it, count as in-flight.
    - ``stale``: dead with no result (or only a bare result without envelope) —
      clean up and revert the in-flight label so the next dispatch re-queues it.
    - ``hung``: still alive but past its deadline — escalate to a human and free
      the slot so a stuck engine cannot pin the cadence at "busy".
    - ``launching``: a pre-spawn lease whose real PID was never published (the tick
      crashed between spawning ``codex`` and rewriting the marker). The liveness of
      the paid child cannot be proven from a dead/absent PID, so this is reported
      distinctly — reconciliation fails closed to a human rather than re-dispatching
      (which would double-spend).

    Args:
        now: Current epoch seconds.
        deadline: The ``deadline`` epoch seconds recorded in ``dispatch.json``.
        pid_alive: Whether the dispatched PID is still running.
        result_exists: Whether the engine's result file is present.
        state: The marker's ``state`` field; ``"launching"`` for a pre-spawn lease,
            empty for a fully published dispatch marker.
        envelope_valid: Whether a valid completion envelope exists, proving the
            engine exited with success and the wrapper confirmed the result.
        failure_envelope_valid: Whether a structurally valid failure envelope is
            bound to this dispatch.

    Returns:
        One of ``"complete"``, ``"failed"``, ``"live"``, ``"stale"``, ``"hung"``,
        or ``"launching"``.
    """
    if envelope_valid and result_exists:
        return "complete"
    if failure_envelope_valid and not pid_alive:
        return "failed"
    if state == "launching":
        return "launching"
    if pid_alive:
        return "hung" if now > deadline else "live"
    return "stale"


def atomic_write(path: str, data: str) -> None:
    """Write ``data`` to ``path`` atomically via temp-file-then-rename.

    Marker files (``dispatch.json``, ``iter.txt``) are written this way so a tick
    that fires while a marker is mid-write always reads a complete object — under
    the tight adaptive cadence the writer and a reader can overlap. The temp file
    is created in the destination directory so the final ``os.replace`` is a
    same-filesystem atomic rename.

    Args:
        path: Destination file path.
        data: File contents to write.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        # Never leave a half-written temp file behind on failure.
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def render_review_comment(
    *,
    verdict: Verdict,
    findings: list[object],
    base_sha: str,
    head_sha: str,
    base_tip_sha: str = "",
    base_ref: str = "",
    fix_round: str = "",
    marker: str = REVIEW_MARKER,
) -> str:
    """Render the PR review comment from the typed verdict and findings.

    The body is stamped with ``marker`` as its first line so
    :func:`count_review_rounds` can tally this round from the PR's comment
    history, and embeds the pinned ``base_sha`` (the merge-base the diff was
    actually anchored to), ``base_tip_sha`` (the base tip snapshot the freshness
    guard compared), ``head_sha``, and ``base_ref`` (the base branch the review
    targeted) so the record is self-describing about exactly which commit range
    was reviewed and against which base. The same identity doubles as a hidden
    idempotency token (see :func:`review_already_posted`) so a re-run never
    posts a duplicate, while a re-review against a new base — advanced tip *or*
    different branch name at the same tip — still posts.

    Args:
        verdict: The validated verdict the comment reports.
        findings: The reviewer's ``findings`` array (validated entries; may be
            empty for a clean review).
        base_sha: The diff anchor (merge-base) the diff was reviewed against.
        head_sha: The pinned head SHA that was reviewed (idempotency key).
        base_tip_sha: The base tip snapshot pinned at dispatch. Defaults to
            ``base_sha`` for pre-two-SHA callers, where the single pinned base
            was the tip and the diff anchor at once.
        base_ref: The base branch name pinned at dispatch. Empty keeps the
            legacy token format and omits the ref from the audit line.
        fix_round: No-change fixer identity pinned at reviewer dispatch.
        marker: The fixed header string; defaults to :data:`REVIEW_MARKER`.

    Returns:
        A Markdown comment body suitable for ``gh pr comment``.
    """
    tip_sha = base_tip_sha or base_sha
    headline = (
        "✅ No blockers — ready for human review."
        if verdict == VERDICT_READY
        else "🔧 Changes requested."
    )
    ref_note = f"base `{base_ref}`, " if base_ref else ""
    lines = [
        marker,
        _round_token(base_sha, tip_sha, head_sha, base_ref, fix_round),
        "",
        (
            f"**Codex review** ({ref_note}diff base `{base_sha}`, "
            f"base tip `{tip_sha}`, head `{head_sha}`)"
        ),
        "",
        headline,
    ]
    if findings:
        lines.append("")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            severity = finding.get("severity", "minor")
            file = finding.get("file", "?")
            summary = finding.get("summary", "")
            lines.append(f"- **{severity}** `{file}` — {summary}")
    return "\n".join(lines)


def decide_review_harvest(
    *,
    review_obj: object,
    comments: list[object],
    pinned_base: str,
    current_base: str,
    pinned_head: str = "",
    current_head: str = "",
    pre_label: str = LABEL_IMPLEMENTED,
    threshold: int = MAX_ITERATIONS,
    diff_base_sha: str = "",
    pinned_base_ref: str = "",
    current_base_ref: str = "",
    fix_round: str = "",
    trusted_author: str,
    report_protocol_version: int | None = None,
    review_identity: str = "",
    approved_input_reservations: Iterable[str] = (),
) -> dict[str, object]:
    """Decide the full outcome of harvesting one finished review.

    Composes the review-stage primitives into the single decision the tick acts
    on, so the skill never re-derives this branching in bash. Order matters:
    a PR retargeted to a different base, or a base *or* head that drifted
    under the review, invalidates the verdict entirely, so retarget and drift
    are checked first and short-circuit to a discard-and-re-dispatch.

    The promote gate is the conjunction of the reviewer's verdict *and* the
    findings: it promotes only when the verdict is ``ready_for_human`` **and** no
    blocker/major finding is present, so a contradictory ``ready_for_human``+
    blocker payload is routed to changes rather than shipped to a human as clean.

    The decision is idempotent: if the comment for this exact head was already
    posted by an interrupted earlier attempt, ``post_comment`` is ``False`` and the
    already-counted round is not double-counted against the iteration cap, so a
    replay only re-applies the label.

    Args:
        review_obj: The parsed ``review.json`` contents (any JSON value).
        comments: The PR's ``comments`` array (iteration-count authority).
        pinned_base: The base tip snapshot recorded at dispatch in
            ``dispatch.json`` (``base_tip_sha``; older markers pinned only the
            tip-valued ``base_sha``). Compared against ``current_base`` for
            drift.
        current_base: The base ref tip resolved now, at harvest.
        pinned_head: The head SHA recorded at dispatch (reviewed commit).
        current_head: The PR head resolved now, at harvest.
        pre_label: The PR's label *before* this review was dispatched
            (``ai:implemented`` or ``ai:revised``). A discard reverts to it so an
            ``ai:revised`` PR is not silently downgraded to ``ai:implemented``.
        approved_input_reservations: Source fingerprints from controller-owned local receipts.
        threshold: Runaway-spend cap; defaults to :data:`MAX_ITERATIONS`.
        diff_base_sha: The merge-base recorded at dispatch — the anchor the
            reviewer's diff actually ran from. Embedded in the audit
            comment/idempotency token as the true diff base. Defaults to
            ``pinned_base`` for markers that predate the two-SHA split.
        pinned_base_ref: The base branch *name* recorded at dispatch
            (``dispatch.json`` ``base_ref``). Compared against
            ``current_base_ref`` for retargeting (empty disables the check)
            and embedded in the comment/replay token, so a round reviewed
            under a different base name never suppresses this round's post
            even when every SHA matches.
        current_base_ref: The PR's live base branch name point-read at
            harvest; empty disables the check.
        fix_round: No-change fixer token pinned with the reviewer's prompt.
            Never derived from the harvest-time comment history.

    Returns:
        A decision mapping with keys: ``action`` (``"promote"`` |
        ``"request_changes"`` | ``"max_iterations"`` | ``"discard"`` |
        ``"escalate"`` for an unusable/contradictory payload),
        ``label`` (the label to set), ``sentinel``, ``verdict`` (validated, may
        be ``None``), ``base_retarget`` (bool), ``base_drift`` (bool),
        ``head_drift`` (bool), ``rounds`` (prior recorded rounds),
        ``rounds_after`` (accounted rounds after the proposed handoff),
        ``max_iterations`` (configured cap), ``finding_counts`` (validated
        counts by severity, or ``None`` when validation is unavailable),
        ``post_comment`` (bool), and ``comment_body`` (str, empty when not
        posting).
    """
    rounds = count_review_rounds(
        comments, trusted_author=trusted_author,
        approved_input_reservations=approved_input_reservations,
    )
    verdict = validate_review(review_obj, report_protocol_version=report_protocol_version)
    observations = {
        "rounds_after": rounds,
        "max_iterations": threshold,
        "finding_counts": (
            {severity: sum(finding["severity"] == severity for finding in review_obj["findings"])
             for severity in ("blocker", "major", "minor")}
            if verdict is not None else None
        ),
    }

    # A retargeted PR invalidates the review outright: the diff was anchored
    # to the old base, and the SHA drift checks below cannot see the change —
    # the old ref's tip and the head can both be unchanged while the PR now
    # merges into a different branch. Checked before drift because the old
    # ref may no longer resolve (e.g. deleted after its own merge), leaving
    # ``current_base`` empty.
    base_retarget = (
        bool(pinned_base_ref)
        and bool(current_base_ref)
        and pinned_base_ref != current_base_ref
    )
    if base_retarget:
        return {
            **observations,
            "action": "discard",
            "label": pre_label,
            "sentinel": SENTINEL_BLOCKERS,
            "verdict": verdict,
            "base_retarget": True,
            "base_drift": False,
            "head_drift": False,
            "rounds": rounds,
            "post_comment": False,
            "comment_body": "",
        }

    base_drift = base_advanced(pinned_base, current_base)
    head_drift = head_advanced(pinned_head, current_head)
    if base_drift or head_drift:
        return {
            **observations,
            "action": "discard",
            "label": pre_label,
            "sentinel": SENTINEL_BLOCKERS,
            "verdict": verdict,
            "base_retarget": False,
            "base_drift": base_drift,
            "head_drift": head_drift,
            "rounds": rounds,
            "post_comment": False,
            "comment_body": "",
        }

    findings_raw = review_obj.get("findings", []) if isinstance(review_obj, dict) else []
    findings = findings_raw if isinstance(findings_raw, list) else []

    # Escalate unusable payloads instead of feeding them to the paid fix loop:
    #   * verdict is None — the reviewer output did not validate against the schema
    #     (infra/output failure); routing it to changes_requested would post an
    #     empty review and spend further rounds on nothing.
    #   * changes_requested with NO blocker/major finding — contradicts the repo rule
    #     that nits alone do not warrant changes, and would dispatch a fixer with no
    #     actionable finding. Legacy ready-with-blockers is downgraded below;
    #     protocol 2 rejects either contradictory outcome as a protocol failure.
    blocking = has_blocking_findings(findings)
    rendered_report = ""
    validation_reason = ""
    if report_protocol_version is not None:
        validation_reason = review_reports.validation_problem(review_obj)
        if report_protocol_version != review_reports.VERSION:
            validation_reason = "Unknown dispatch report protocol version."
        if not validation_reason:
            token = _round_token(diff_base_sha or pinned_base, pinned_base, pinned_head,
                                 pinned_base_ref, fix_round)
            header = (ESCALATION_MARKER if verdict == "needs_human_decision"
                      else REVIEW_MARKER + "\n" + token)
            rendered_report = review_reports.render(
                review_obj, identity=review_identity or token, header=header,
                audit=(f"**Codex review** (base `{pinned_base_ref}`, diff base "
                       f"`{diff_base_sha or pinned_base}`, base tip `{pinned_base}`, head `{pinned_head}`)"),
            )
            if not review_reports.within_limit(rendered_report):
                validation_reason = "Complete rendered report exceeds 60,000 characters or UTF-8 bytes."
    if (verdict is None or verdict == "needs_human_decision" or validation_reason
            or (verdict == VERDICT_CHANGES and not blocking)):
        return {
            **observations,
            "action": "escalate",
            "label": LABEL_NEEDS_HUMAN,
            "sentinel": SENTINEL_BLOCKERS,
            "verdict": verdict,
            "base_retarget": False,
            "base_drift": False,
            "head_drift": False,
            "rounds": rounds,
            "post_comment": False,
            "comment_body": (rendered_report if verdict == "needs_human_decision" and not validation_reason
                             else ESCALATION_MARKER + "\n\nReview report requires a human decision.\n\n" + validation_reason
                             if report_protocol_version is not None else ""),
            "rendered_report": rendered_report,
            "validation_reason": validation_reason,
        }

    # Promote only when the verdict AND the findings agree there is nothing
    # blocking — never on the stated verdict alone (finding: contradictory output).
    promote = verdict == VERDICT_READY and not blocking
    render_verdict: Verdict = VERDICT_READY if promote else VERDICT_CHANGES
    # Audit truth: the comment/token carry the diff anchor the review actually
    # ran from (merge-base) plus the pinned freshness snapshot — not the
    # harvest-time tip masquerading as "the reviewed range". The no-drift check
    # above guarantees pinned_base == current_base here, so pinning is not a
    # semantic change for the drift guard, only for the record's honesty.
    diff_base = diff_base_sha or pinned_base
    comment_body = rendered_report or render_review_comment(
        verdict=render_verdict,
        findings=findings,
        base_sha=diff_base,
        base_tip_sha=pinned_base,
        head_sha=pinned_head,
        base_ref=pinned_base_ref,
        fix_round=fix_round,
    )
    if report_protocol_version is None:
        comment_body += "\n\n" + review_reports.LEGACY_NOTE
    if not review_reports.within_limit(comment_body):
        reason = "Complete rendered report exceeds 60,000 characters or UTF-8 bytes."
        return {
            **observations,
            "action": "escalate", "label": LABEL_NEEDS_HUMAN,
            "sentinel": SENTINEL_BLOCKERS, "verdict": verdict,
            "base_retarget": False, "base_drift": False, "head_drift": False,
            "rounds": rounds, "post_comment": False,
            "comment_body": ESCALATION_MARKER + "\n\nReview report requires a human decision.\n\n" + reason,
            "rendered_report": comment_body, "validation_reason": reason,
        }

    # Idempotency: a comment already posted for this round means it is already
    # reflected in ``rounds`` — do not re-post it or double-count it. The
    # lookup keys on the pinned base *ref* as well as the SHAs: a comment from
    # a pre-retarget round can carry the identical SHA triple (old and new
    # base branches pointing at the same commit) and must not suppress this
    # round's comment — the posted comment is the next fixer's handoff.
    already_posted = review_already_posted(
        comments, diff_base, pinned_head,
        base_tip_sha=pinned_base, base_ref=pinned_base_ref,
        fix_round=fix_round,
        trusted_author=trusted_author,
    )
    rounds_after = rounds if already_posted else rounds + 1

    if promote:
        action = "promote"
        label, sentinel = LABEL_READY_FOR_HUMAN, SENTINEL_NO_BLOCKERS
    elif at_max_iterations(rounds_after, threshold):
        # This changes-requested round hits the cap; stop the fix loop instead of
        # dispatching another fixer.
        action = "max_iterations"
        label, sentinel = LABEL_MAX_ITERATIONS, SENTINEL_BLOCKERS
    else:
        action = "request_changes"
        label, sentinel = LABEL_CHANGES_REQUESTED, SENTINEL_BLOCKERS

    return {
        **observations,
        "rounds_after": rounds_after,
        "action": action,
        "label": label,
        "sentinel": sentinel,
        "verdict": verdict,
        "base_retarget": False,
        "base_drift": False,
        "head_drift": False,
        "rounds": rounds,
        "post_comment": not already_posted,
        "comment_body": comment_body,
    }


# --- Operator configuration ---

# The per-target settings an operator may set in [defaults] and override in
# [repos."owner/name"]. Every one appears in both tables: a key expressible at
# only one layer is a key an operator cannot set at the layer they need it.
# A key outside this set is a typo, and a silently ignored typo is exactly the
# failure this file's layering exists to make impossible, so it is reported.
_CONFIG_KEYS = (
    "allow_touching",
    "approved_reviewers",
    "checklist_paths",
    "verify_tools",
    "base_ref",
    "permission_mode",
    "engine_settings",
    "max_in_flight",
    "impl_engine",
    "model",
    "fix_engine",
    "fix_model",
    "review_model",
    "review_effort",
    "effort",
    "fix_effort",
)

# Claude Code effort levels the implementer and fixer accept. The value
# reaches CLAUDE_CODE_EFFORT_LEVEL, so a level outside this set is a startup
# error naming the key rather than an engine that silently ignores it.
ENGINE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# The integer settings: ``key -> (shipped default, minimum)``. None of them has
# a conservative reading — a shorter deadline kills work, a longer one spends
# what the operator did not budget — so a value that is not an integer at or
# above its minimum is a startup error naming the key, never a fallback.
_INT_SETTINGS: dict[str, tuple[int, int]] = {
    "implementer_timeout_seconds": (IMPLEMENTER_TIMEOUT_SECONDS, 60),
    "fixer_timeout_seconds": (FIXER_TIMEOUT_SECONDS, 60),
    "review_timeout_seconds": (REVIEW_TIMEOUT_SECONDS, 60),
    "engine_stall_seconds": (
        ENGINE_STALL_SECONDS, MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS + 1,
    ),
    "engine_stall_startup_grace_seconds": (ENGINE_STALL_STARTUP_GRACE_SECONDS, 0),
    "max_iterations": (MAX_ITERATIONS, 1),
    "loop_short_poll_seconds": (LOOP_SHORT_POLL_SECONDS, 60),
    "loop_max_poll_seconds": (LOOP_MAX_POLL_SECONDS, 60),
    "loop_heartbeat_seconds": (LOOP_HEARTBEAT_SECONDS, 60),
    "loop_quiescent_stop_after": (LOOP_QUIESCENT_STOP_AFTER, 1),
    "transient_cooldown_seconds": (TRANSIENT_COOLDOWN_SECONDS, 60),
    "transient_cooldown_reset_buffer_seconds": (
        TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS, 0,
    ),
}
_CONFIG_KEYS += tuple(_INT_SETTINGS)

# Setup evidence belongs only to the repository it measured. It neither
# inherits from defaults nor changes when an operator edits a routing key.
_CONFIG_PROBE_KEYS = ("probed_impl_engine", "probed_model")

# Wakes the cadence contract never picks (see compute_next_wake), refused
# rather than moved: a configured interval must be the interval that runs.
_CACHE_CLIFF_KEYS = (
    "loop_short_poll_seconds", "loop_max_poll_seconds", "loop_heartbeat_seconds",
)

# The admission cap when no layer names ``max_in_flight``. The packaged floor
# ships the same value; this only answers for an install whose floor omits it.
DEFAULT_MAX_IN_FLIGHT = 3

# Claude `--permission-mode` values; the first is the unconfigured default.
_PERMISSION_MODES = ("auto", "dontAsk")

# A verification grant as it appears inside `Bash(...)` on `--allowedTools`.
# The flag takes a space-separated list of `Tool(body)` entries, so a closing
# parenthesis inside a body ends its entry early and everything after it is
# parsed as a *new* permission — a config typo would widen the exact boundary
# the allowlist exists to be. Spaces are legal and load-bearing
# (`uv run pytest *`), so they cannot be the delimiter check.
#
# This is checked in the resolver, not filtered at dispatch: dropping a bad
# entry there would hand the engine a narrower grant than the operator wrote,
# and an all-invalid list would arrive as the deliberate empty grant.
VERIFY_TOOL_BODY_RE = re.compile(r"\A[^()\r\n]+\Z")

# A GitHub login: alphanumerics with interior single hyphens, 39 characters max.
# Shape is checked because the login is a trust boundary compared for equality
# against an author — a value with a stray space or quote matches nobody, which
# reads as "every PR escalated" rather than as the config error it is.
_LOGIN_RE = re.compile(r"\A[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}\Z")

# ``owner/name`` exactly as ``gh`` reports it. The two halves have different
# grammars and must not share one: an owner starts with an alphanumeric, but a
# repository name need not — GitHub's own organization-profile repository is
# named `.github`. Rejecting it would stop the tick for every *other* target
# too, because every `[repos.*]` key is validated at startup. `.` and `..` are
# excluded: GitHub forbids both, and they are the path segments that walk out
# of any directory a resolved target is joined into.
_CONFIG_REPO_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._-]*/(?!\.\.?\Z)[A-Za-z0-9._-]+\Z"
)

# A base branch name conservative enough to hand to `git fetch <remote> <ref>`
# and `gh pr create --base <ref>`: git ref-name characters only, no leading
# dash or slash (option or refspec ambiguity). This is the operator's own
# branch name, not an untrusted PR head ref — tick's _valid_push_head_ref
# guards that separate case, where the value is templated into a *permission*.
_BASE_REF_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")


@dataclass(frozen=True)
class ResolvedConfig:
    """The effective configuration for one resolved target.

    The single result every Python caller reads. Callers never read a raw
    table: the layering, the fail-closed reads and the hard errors all happen
    once, here, so two callers cannot disagree about what the operator
    configured.

    Attributes:
        login: The operator's GitHub login — the identity the trust gates are
            written against. ``""`` when unset, which the tick's startup gate
            refuses to run on.
        allow_touching: Whether PRs editing reviewer-governing files may be
            auto-reviewed. Fails closed to ``False``.
        approved_reviewers: GitHub logins whose PR conversation comments and
            submitted change-request/comment reviews are fixer input. :attr:`login` is always an implicit
            member and is not listed here. A malformed value lands in
            :attr:`errors` and resolves to the empty tuple.
        checklist_paths: Repo-relative paths naming the target's own review
            checklists.
        verify_tools: Bash permission bodies the engines may run for
            verification. Empty is a deliberate operator choice, distinct from
            a malformed value, which lands in :attr:`errors` instead.
        base_ref: The pinned base branch name. ``""`` means "resolve the
            target's default branch" and is not malformed.
        permission_mode: The engine ``--permission-mode`` value.
        engine_settings: The implementer and fixer ``--settings`` value: a
            JSON object or an absolute path to a settings file. ``""`` omits
            the flag, so engines load only the Claude settings hierarchy.
        max_in_flight: The admission cap: how many engines this checkout's
            dispatch markers may hold at once. At least 1; a malformed value
            lands in :attr:`errors` and this holds the shipped default, which
            the startup gate never runs under.
        scan_repos: ``None`` when repository scope is unrestricted (the key is
            absent or an empty list — both operator-written states). A tuple
            restricts scope to exactly those repositories; a malformed value
            yields the empty *restrictive* tuple plus an entry in
            :attr:`errors`, so a typo can never read as unrestricted.
        errors: Hard configuration errors, each naming the key. A caller that
            is about to act on the configuration must refuse to start.
        warnings: Fail-closed coercions and ignored keys, for the tick log.
        repo_configured: Whether the resolved target has its own
            ``[repos."owner/name"]`` table, empty or not. ``[defaults]``
            answering every key is not the same statement: only setup writes a
            repository's section, after proposing its verification grants and
            probing its engine route. Always ``False`` for a blank ``repo``.
        implementer_timeout_seconds: Implementer deadline from dispatch.
        fixer_timeout_seconds: Fixer deadline from dispatch.
        review_timeout_seconds: Reviewer deadline from dispatch.
        engine_stall_seconds: Engine output silence that reports
            a stall. Above :data:`MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS` and
            below all three deadlines, or an entry in :attr:`errors`.
        engine_stall_startup_grace_seconds: Extra silence allowed before an
            engine's first output event.
        max_iterations: Review/fix rounds before ``ai:max-iterations``.
        impl_engine: The implementer engine, one of :data:`ENGINE_ROUTES`.
        model: The implementer ``--model``; ``""`` is the engine default.
        fix_engine: The fixer engine; a blank key inherits ``impl_engine``.
        fix_model: The fixer ``--model``. A blank key inherits ``model`` when
            the fixer runs on the implementer's engine, and is the engine
            default otherwise: a model id belongs to one provider.
        review_model: The reviewer model; ``""`` resolves the operator's
            codex config, then the pinned fallback.
        review_effort: The reviewer reasoning effort, resolved like
            ``review_model``.
        effort: The implementer effort, one of :data:`ENGINE_EFFORT_LEVELS`;
            ``""`` is no override (``claude-ds`` then runs at ``max``).
        fix_effort: The fixer effort. A blank key inherits ``effort`` on any
            fixer engine: effort levels belong to no single provider.
        probed_route: The repository's ``(probed_impl_engine, probed_model)``
            setup evidence, independent of routing defaults and CLI flags.
            ``None`` for older setups without evidence or invalid metadata
            (which also adds a startup error).
        loop_short_poll_seconds: Wake while work is queued, at capacity, or
            unknown.
        loop_max_poll_seconds: Longest wait while an engine is in flight.
        loop_heartbeat_seconds: Wake while quiescent.
        loop_quiescent_stop_after: Quiescent heartbeats before the loop stops.
        transient_cooldown_seconds: Provider cooldown after a transient engine
            result that names no reset time.
        transient_cooldown_reset_buffer_seconds: Margin added to a reset time
            the provider names.
        tuning_errors: The entries of :attr:`errors` that concern
            ``max_in_flight``, tuning, routes, and probe metadata. A tick reads those
            keys once, at its startup gate, so a later read within the same
            tick — dispatch's own read of ``verify_tools`` and
            ``engine_settings`` — must not act on an error in them.
    """

    login: str
    allow_touching: bool
    checklist_paths: tuple[str, ...]
    verify_tools: tuple[str, ...]
    base_ref: str
    permission_mode: str
    scan_repos: tuple[str, ...] | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    repo_configured: bool = False
    engine_settings: str = ""
    approved_reviewers: tuple[str, ...] = ()
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    implementer_timeout_seconds: int = IMPLEMENTER_TIMEOUT_SECONDS
    fixer_timeout_seconds: int = FIXER_TIMEOUT_SECONDS
    review_timeout_seconds: int = REVIEW_TIMEOUT_SECONDS
    engine_stall_seconds: int = ENGINE_STALL_SECONDS
    engine_stall_startup_grace_seconds: int = ENGINE_STALL_STARTUP_GRACE_SECONDS
    max_iterations: int = MAX_ITERATIONS
    impl_engine: str = ENGINE_ROUTES[0]
    model: str = ""
    fix_engine: str = ENGINE_ROUTES[0]
    fix_model: str = ""
    review_model: str = ""
    review_effort: str = ""
    effort: str = ""
    fix_effort: str = ""
    probed_route: tuple[str, str] | None = None
    loop_short_poll_seconds: int = LOOP_SHORT_POLL_SECONDS
    loop_max_poll_seconds: int = LOOP_MAX_POLL_SECONDS
    loop_heartbeat_seconds: int = LOOP_HEARTBEAT_SECONDS
    loop_quiescent_stop_after: int = LOOP_QUIESCENT_STOP_AFTER
    transient_cooldown_seconds: int = TRANSIENT_COOLDOWN_SECONDS
    transient_cooldown_reset_buffer_seconds: int = (
        TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS
    )
    tuning_errors: tuple[str, ...] = ()

    def cadence(self) -> CadenceSettings:
        """Return the cadence keyword arguments for :func:`compute_next_wake`.

        The tick emits that decision and native mode consumes it unchanged.
        Direct callers of :func:`decide_loop_cadence` accept the same keywords.
        """
        return {
            "short_poll_seconds": self.loop_short_poll_seconds,
            "max_poll_seconds": self.loop_max_poll_seconds,
            "heartbeat_seconds": self.loop_heartbeat_seconds,
            "quiescent_stop_after": self.loop_quiescent_stop_after,
        }


def _operator_config_location() -> tuple[Path, str]:
    """Resolve the operator config path, refusing a cwd-relative override.

    ``$PR_PIPELINE_CONFIG`` names the file outright and outranks everything
    else — it is the explicit testing override, so a test never writes into a
    real home. Otherwise the file sits under the Claude home,
    ``$CLAUDE_CONFIG_DIR`` when set and ``~/.claude`` when not.

    Either variable may only name an *absolute* location. A relative one is
    not a config path, it is a config path per working directory: the tick
    chdirs to the repository root after this module is imported, so the
    identity resolved at import and the policy resolved per tick would come
    from two different files — and an engine's cwd is a checkout of the pull
    request's own head, which is the one directory a PR could use to supply
    the policy it is judged under. There is no safe anchor to pick on the
    operator's behalf, so it is refused rather than guessed at.

    Returns:
        ``(path, problem)``. On a problem the path is the default Claude-home
        location and the caller must not read it: it is not what was named.
    """
    default = Path.home() / ".claude" / Path(*_OPERATOR_CONFIG_RELATIVE)
    explicit = os.environ.get(_OPERATOR_CONFIG_ENV, "").strip()
    if explicit:
        if not Path(explicit).is_absolute():
            return default, (
                f"${_OPERATOR_CONFIG_ENV}: {explicit!r} is relative — name the "
                "file by absolute path, or a change of working directory "
                "changes which config is read"
            )
        return Path(explicit), ""
    config_dir = os.environ.get(_CLAUDE_CONFIG_DIR_ENV, "").strip()
    if config_dir:
        if not Path(config_dir).is_absolute():
            return default, (
                f"${_CLAUDE_CONFIG_DIR_ENV}: {config_dir!r} is relative — name "
                "the directory by absolute path, or a change of working "
                "directory changes which config is read"
            )
        return Path(config_dir).joinpath(*_OPERATOR_CONFIG_RELATIVE), ""
    return default, ""


def operator_config_path() -> Path:
    """Return the path to the operator's own ``config.toml``.

    The result is independent of the working directory by construction — see
    :func:`_operator_config_location`, which is where that is enforced. A
    refused override reports the default location here; the problem itself
    surfaces as a startup error from :func:`resolve_config`.

    Returns:
        The operator config path, which need not exist.
    """
    return _operator_config_location()[0]


def _read_toml(path: Path) -> tuple[dict[str, object], str]:
    """Read one TOML file, reporting malformation rather than raising.

    The single funnel for every configuration read in the pipeline, so the
    "nothing is read from the install, the target repo, or an engine worktree"
    invariant has exactly one place to hold and one place to test.

    Read at call time rather than import time, so a config change takes effect
    on the next tick without a redeploy.

    Args:
        path: The file to read.

    Returns:
        ``(parsed, error)``. A missing file is ``({}, "")`` — absent is not an
        error. Malformed TOML, invalid UTF-8 (which ``tomllib`` surfaces as
        ``UnicodeDecodeError``), an unreadable file, or a document that is not
        a table is ``({}, <reason>)``: callers still see the conservative empty
        config *and* a reportable reason, so the gates built on this fail
        closed whether or not the caller acts on the error.
    """
    try:
        with open(path, "rb") as fh:
            config = tomllib.load(fh)
    except FileNotFoundError:
        return {}, ""
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return {}, f"unreadable: {exc}"
    if not isinstance(config, dict):
        return {}, "not a TOML table"
    return config, ""


def _table(config: Mapping[str, object], name: str) -> dict[str, object]:
    """Return one top-level table, or ``{}`` when absent or not a table.

    Args:
        config: A parsed TOML document.
        name: The table name.

    Returns:
        The table, or ``{}``.
    """
    section = config.get(name)
    return dict(section) if isinstance(section, dict) else {}


def _declared_table(
    config: Mapping[str, object], name: str, label: str = "",
) -> tuple[dict[str, object], str]:
    """Return one top-level table, distinguishing absent from malformed.

    ``[[defaults]]`` instead of ``[defaults]`` is valid TOML and an easy slip,
    and every per-key validator in this module runs *inside* the table — so a
    container read as ``{}`` means the keys the operator wrote are never seen.
    Silently, in the direction that widens: the shipped grants come back in
    place of their ``verify_tools``, and a declared ``[scan]`` scope becomes
    unrestricted. Absence and malformation have to be two different answers.

    Args:
        config: A parsed TOML document.
        name: The table name.
        label: Prefix naming the file, for the message. Empty means the
            operator's own config.

    Returns:
        ``({}, "")`` when absent, ``(table, "")`` when a table, and
        ``({}, <reason>)`` when declared as something else.
    """
    if name not in config:
        return {}, ""
    section = config[name]
    if isinstance(section, dict):
        return dict(section), ""
    where = f"{label} " if label else ""
    return {}, (
        f"{where}[{name}]: expected a table, got {type(section).__name__}"
    )


def _pick(
    layers: Sequence[tuple[str, Mapping[str, object]]], key: str,
) -> tuple[str, object] | None:
    """Return the highest layer that *declares* ``key``, with its label.

    Presence, not truthiness, decides. That is what lets a repository narrow a
    default to nothing: ``verify_tools = []`` under ``[repos."owner/name"]``
    has to beat a non-empty ``[defaults]`` list, because a permission grant an
    operator cannot narrow per target is the wrong direction for a permission
    grant.

    Args:
        layers: ``(label, table)`` pairs, lowest precedence first.
        key: The setting name.

    Returns:
        ``(label, value)`` from the highest layer declaring it, else ``None``.
    """
    found: tuple[str, object] | None = None
    for label, table in layers:
        if key in table:
            found = (label, table[key])
    return found


def _resolve_str_list(
    picked: tuple[str, object] | None, key: str,
) -> tuple[tuple[str, ...], str]:
    """Resolve a list-of-strings setting, reporting an unusable shape.

    Blank entries are dropped rather than reported: a trailing comma is not a
    statement about policy. A non-list, or a non-string entry, is a shape the
    operator cannot have meant and is reported to the caller.

    The string entries that *are* usable come back alongside the problem. For a
    list of files to block, the empty set is the permissive direction, so
    discarding the whole list on one bad entry would quietly weaken a gate; the
    caller decides whether partial entries are the safe reading for its key.

    Args:
        picked: The ``(label, value)`` pair from :func:`_pick`, or ``None``.
        key: The setting name, for the message.

    Returns:
        ``(entries, problem)``, where ``problem`` is ``""`` when the value was
        usable and ``entries`` holds every usable string either way.
    """
    if picked is None:
        return (), ""
    label, value = picked
    if not isinstance(value, list):
        return (), (
            f"{label} {key}: expected a list of strings, "
            f"got {type(value).__name__}"
        )
    problem = ""
    for entry in value:
        if not isinstance(entry, str):
            problem = (
                f"{label} {key}: every entry must be a string, "
                f"got {entry!r}"
            )
            break
    entries = tuple(
        e.strip() for e in value if isinstance(e, str) and e.strip()
    )
    return entries, problem


def _verify_tool_body_problem(
    picked: tuple[str, object] | None, entries: Sequence[str],
) -> str:
    """Report the first ``verify_tools`` entry that is not a permission body.

    Each entry is templated into ``Bash(<entry>)`` on the engine's
    ``--allowedTools``, so an entry that cannot be a body does not describe the
    permission the operator typed. Reporting it — rather than dropping it at
    dispatch — is what keeps a typo from becoming a silently narrower grant,
    and an all-invalid list from becoming the unconfigured empty grant that
    dispatches unverified work and still awards ai:ready-for-human.

    Args:
        picked: The ``(label, value)`` pair the entries came from, for the
            message. ``None`` when no layer set the key.
        entries: The usable string entries, already stripped.

    Returns:
        The problem, or ``""`` when every entry is a usable body.
    """
    if picked is None:
        return ""
    label, _ = picked
    for entry in entries:
        if not VERIFY_TOOL_BODY_RE.match(entry):
            return (
                f"{label} verify_tools: {entry!r} is not a permission body "
                "(no parentheses or newlines)"
            )
    return ""


def _every_usable_entry(
    layers: Sequence[tuple[str, Mapping[str, object]]], key: str,
) -> tuple[str, ...]:
    """Return every usable string entry for ``key``, across all layers.

    The recovery reading for a list whose empty state is the *permissive* one,
    used only once the resolved value has already been reported as malformed.
    Layering answers "which value did the operator choose"; it has no answer
    for a value they did not manage to write, and for a gate the safe move
    there is to keep every path any layer named.

    Args:
        layers: ``(label, table)`` pairs, lowest precedence first.
        key: The setting name.

    Returns:
        The entries, deduplicated, in lowest-to-highest layer order.
    """
    seen: dict[str, None] = {}
    for _, table in layers:
        value = table.get(key)
        if not isinstance(value, list):
            continue
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                seen[entry.strip()] = None
    return tuple(seen)


def _resolve_string(
    picked: tuple[str, object] | None,
    key: str,
    default: str,
    valid: Callable[[str], bool],
    *,
    blank_means: str = "",
) -> tuple[str, str]:
    """Resolve a string setting that has no conservative fallback.

    ``base_ref``, ``permission_mode`` and ``[operator] login`` cannot fail
    closed — there is no restrictive branch name, no safe permission mode, and
    no identity that trusts nobody usefully. So a wrong type or a value outside
    the accepted shape is reported for the caller to refuse startup on, rather
    than coerced into a value the operator did not write.

    Blank acceptance is per key, not universal. ``base_ref = ""`` means
    "resolve the target's default branch" and ``[operator] login = ""`` is the
    unconfigured state the startup gate names; ``permission_mode = ""`` means
    nothing at all, and accepting it walks a value the engine launch rejects
    past startup and into a crash that has already taken a dispatch lease.

    Args:
        picked: The ``(label, value)`` pair from :func:`_pick`, or ``None``.
        key: The setting name, for the message.
        default: The value when no layer declares the key.
        valid: Shape predicate applied to a declared, non-empty value.
        blank_means: What a declared blank value means for this key. Empty —
            the default — makes blank invalid like any other unusable value.

    Returns:
        ``(value, problem)``. On a problem the value is ``default``.
    """
    if picked is None:
        return default, ""
    label, value = picked
    if not isinstance(value, str):
        return default, (
            f"{label} {key}: expected a string, got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped and not blank_means:
        return default, (
            f"{label} {key}: blank is not one of its values"
        )
    if stripped and not valid(stripped):
        return default, f"{label} {key}: {value!r} is not a valid {key}"
    return stripped, ""


def valid_engine_settings(value: str) -> bool:
    """Return whether ``value`` is a ``claude --settings`` value engines may get.

    The flag takes a JSON string or a file path. JSON must be an object, the
    shape of a settings file. A path must be absolute: engines run with their
    cwd inside a checkout of the work under review, so a relative path would
    let that checkout supply the settings its own engine loads. The file must
    also be readable now and hold a JSON object: ``claude --settings`` does not
    reject a file it cannot parse, so a broken file would launch engines under
    the interactive settings this key exists to override. Dispatch resolves
    config again before each launch, so that check runs per dispatch.
    """
    if not value.startswith("{"):
        if not os.path.isabs(value) or not os.path.isfile(value):
            return False
        try:
            with open(value, encoding="utf-8") as settings_file:
                value = settings_file.read()
        except (OSError, UnicodeDecodeError):
            return False
    try:
        return isinstance(json.loads(value), dict)
    except ValueError:
        return False


def _valid_base_ref(value: str) -> bool:
    """Return whether ``value`` is a branch name safe to pass to git and gh.

    Conservative git ref-name shape plus the ref-format rules a regex cannot
    express (``..``, ``//``, and a trailing ``/``, ``.`` or ``.lock``). A blank
    ``base_ref`` never reaches here: it is the documented "resolve the target's
    default branch" value, not a malformed one.

    Args:
        value: The configured branch name.

    Returns:
        Whether it is usable as a base ref.
    """
    if not _BASE_REF_RE.match(value):
        return False
    if ".." in value or "//" in value:
        return False
    return not value.endswith(("/", ".", ".lock"))


def _resolve_scan_repos(
    scan: Mapping[str, object],
) -> tuple[tuple[str, ...] | None, list[str]]:
    """Resolve ``[scan] repos`` into unrestricted, restricted, or an error.

    Two states and no third. Absent, or an empty list, is unrestricted — both
    are things the operator wrote. Anything else must be a list of
    ``owner/name`` strings; a malformed value is an error naming the key and
    resolves to the empty *restrictive* set rather than to unrestricted,
    because "unrestricted" must never be something a typo produced.

    Args:
        scan: The operator's ``[scan]`` table, already type-checked by
            :func:`_declared_table` — a malformed *container* is the caller's
            error to report, and reaches here as an empty table only after the
            caller has made the scope restrictive.

    Returns:
        ``(scope, problems)``, where ``scope`` is ``None`` when unrestricted and
        otherwise the restricted repository set.
    """
    if "repos" not in scan:
        return None, []
    value = scan["repos"]
    if not isinstance(value, list):
        return (), [
            '[scan] repos: expected a list of "owner/name" strings, '
            f"got {type(value).__name__}"
        ]
    if not value:
        return None, []
    resolved: list[str] = []
    problems: list[str] = []
    for entry in value:
        name = entry.strip() if isinstance(entry, str) else entry
        if not isinstance(name, str) or not _CONFIG_REPO_RE.match(name):
            problems.append(
                f'[scan] repos: {entry!r} is not an "owner/name" repository'
            )
            continue
        resolved.append(name)
    if problems and not resolved:
        return (), problems
    return tuple(resolved), problems


def resolve_config(
    repo: str = "", *, cli_overrides: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Return the effective configuration for one resolved target.

    Layers, each overriding the ones before it:

    1. the packaged ``config.toml`` — the shipped floor, and the only thing the
       install directory contributes;
    2. ``[defaults]`` in the operator's config;
    3. ``[repos."owner/name"]`` for ``repo``;
    4. environment variables (``PIPELINE_TRUSTED_AUTHOR``);
    5. ``cli_overrides`` — a flag typed on one run is the most specific
       statement of intent there is.

    Lists replace wholesale; they never merge. Settings with a safe direction
    fail closed and record a warning; settings without one record an error in
    :attr:`ResolvedConfig.errors` for the caller to refuse startup on.

    Args:
        repo: The resolved target as ``owner/name``. Empty selects no
            per-repository layer, which is the correct read for the pure-logic
            CLI helpers that are not operating a specific target.
        cli_overrides: Per-invocation flag values, keyed by setting name.
            Validated exactly like a config value, so a bad flag is reported
            rather than silently dropped.

    Returns:
        The resolved configuration, including any errors and warnings.
    """
    errors: list[str] = []
    warnings: list[str] = []

    packaged, packaged_problem = _read_toml(_PACKAGED_CONFIG_PATH)
    if packaged_problem:
        errors.append(f"packaged config.toml: {packaged_problem}")
    # A refused override is not read from at all: the fallback location holds
    # somebody's real settings, and answering with those would be answering a
    # question nobody asked.
    operator_path, location_problem = _operator_config_location()
    if location_problem:
        errors.append(location_problem)
        operator: dict[str, object] = {}
    else:
        operator, operator_problem = _read_toml(operator_path)
        if operator_problem:
            errors.append(f"{operator_path}: {operator_problem}")

    # The packaged file is layer 1 and *only* layer 1. An operator-set table
    # landing there would be discarded on the next `/plugin update`, which
    # installs at a different path — so it is an error, not a silent read.
    for owned in ("operator", "repos", "scan"):
        if owned in packaged:
            errors.append(
                f"packaged config.toml: [{owned}] is operator configuration "
                "and must not ship in the install"
            )

    if "repo" in _table(packaged, "target") or "target" in operator:
        warnings.append(
            "[target] repo is no longer read; the target comes from Git remotes "
            "with PIPELINE_REPO selecting a matching remote. Configure the "
            "login under [operator]."
        )

    # Every container the operator may declare is type-checked before its keys
    # are read. A container read as absent when it is merely malformed hands
    # the shipped floor back in place of the operator's settings, and turns a
    # declared [scan] scope into an unrestricted one, with nothing reported.
    declared: dict[str, dict[str, object]] = {}
    for name in ("defaults", "operator", "repos", "scan"):
        table, problem = _declared_table(operator, name)
        if problem:
            errors.append(problem)
        declared[name] = table
    packaged_defaults, problem = _declared_table(
        packaged, "defaults", "packaged config.toml",
    )
    if problem:
        errors.append(problem)

    # A malformed [scan] container resolves restrictive, for the same reason a
    # malformed `repos` value does: "unrestricted" must never be something a
    # typo produced.
    scan_repos: tuple[str, ...] | None
    if "scan" in operator and not isinstance(operator["scan"], dict):
        scan_repos, scan_problems = (), []  # the container error stands alone
    else:
        scan_repos, scan_problems = _resolve_scan_repos(declared["scan"])
    errors.extend(scan_problems)
    repos_table = declared["repos"]
    seen_repos: set[str] = set()
    for name in sorted(repos_table):
        if name.casefold() in seen_repos:
            errors.append(f'[repos."{name}"]: duplicate repository ignoring case')
        seen_repos.add(name.casefold())
        if not isinstance(repos_table[name], dict):
            errors.append(f"[repos.\"{name}\"]: expected a table")
        elif not _CONFIG_REPO_RE.match(name):
            errors.append(
                f"[repos.\"{name}\"]: not an \"owner/name\" repository"
            )
        elif scan_repos is not None and not any(
            same_repository(name, entry) for entry in scan_repos
        ):
            errors.append(
                f"[repos.\"{name}\"]: names a repository absent from "
                "[scan] repos"
            )

    layers: list[tuple[str, Mapping[str, object]]] = [
        ("the packaged floor [defaults]", packaged_defaults),
        ("[defaults]", declared["defaults"]),
    ]
    repo_configured = False
    repo_section: Mapping[str, object] = {}
    if repo:
        section = next(
            (value for name, value in repos_table.items() if same_repository(name, repo)),
            None,
        )
        if isinstance(section, dict):
            layers.append((f"[repos.\"{repo}\"]", section))
            repo_configured = True
            repo_section = section
    if cli_overrides:
        layers.append(("the command line", dict(cli_overrides)))
    for label, table in layers:
        for key in table:
            if key not in _CONFIG_KEYS and not (
                table is repo_section and key in _CONFIG_PROBE_KEYS
            ):
                warnings.append(f"{label} {key}: unknown setting, ignored")

    allow_picked = _pick(layers, "allow_touching")
    allow_touching = False
    if allow_picked is not None:
        label, raw = allow_picked
        if isinstance(raw, bool):
            allow_touching = raw
        else:
            warnings.append(
                f"{label} allow_touching: expected true or false, got "
                f"{type(raw).__name__} — keeping the conservative gate"
            )

    # checklist_paths names files to *block*, so the empty set is the
    # permissive direction — the opposite of every other list here. An unusable
    # value therefore keeps the entries that are usable (a typo must not
    # silently unprotect the paths the operator did name, nor let a per-repo
    # override erase a valid [defaults] list) *and* refuses the tick: there is
    # no reading of a malformed gate that is the one the operator wrote.
    checklist_paths, problem = _resolve_str_list(
        _pick(layers, "checklist_paths"), "checklist_paths",
    )
    if problem:
        errors.append(problem)
        # "Lists replace wholesale" is the contract for a value the operator
        # wrote; a malformed one is not that. Falling through to every layer's
        # usable entries keeps the widest gate the configuration describes, so
        # a bad per-repo override cannot unprotect a valid [defaults] list.
        checklist_paths = _every_usable_entry(layers, "checklist_paths")

    # verify_tools has none. An empty list is a deliberate operator choice that
    # dispatches with a warning; a list that failed to parse must not collapse
    # into that same state, or one typo silently becomes an unverified PR
    # promoted to ai:ready-for-human.
    picked_verify_tools = _pick(layers, "verify_tools")
    verify_tools, problem = _resolve_str_list(
        picked_verify_tools, "verify_tools",
    )
    if not problem:
        problem = _verify_tool_body_problem(picked_verify_tools, verify_tools)
    if problem:
        errors.append(problem)
        verify_tools = ()

    # approved_reviewers widens who may start a fix, so a malformed list has no
    # safe partial reading: an error refuses startup and nothing is approved.
    picked_reviewers = _pick(layers, "approved_reviewers")
    approved_reviewers, problem = _resolve_str_list(
        picked_reviewers, "approved_reviewers",
    )
    if not problem and picked_reviewers is not None:
        bad = next((e for e in approved_reviewers if not _LOGIN_RE.match(e)), None)
        if bad is not None:
            problem = (
                f"{picked_reviewers[0]} approved_reviewers: {bad!r} is not a "
                "GitHub login"
            )
    if problem:
        errors.append(problem)
        approved_reviewers = ()

    base_ref, problem = _resolve_string(
        _pick(layers, "base_ref"), "base_ref", "", _valid_base_ref,
        blank_means="the target's default branch",
    )
    if problem:
        errors.append(problem)

    permission_mode, problem = _resolve_string(
        _pick(layers, "permission_mode"), "permission_mode",
        _PERMISSION_MODES[0], lambda v: v in _PERMISSION_MODES,
    )
    if problem:
        errors.append(problem)

    engine_settings, problem = _resolve_string(
        _pick(layers, "engine_settings"), "engine_settings", "",
        valid_engine_settings,
        blank_means="no --settings flag",
    )
    if problem:
        errors.append(problem)

    tuning_errors: list[str] = []
    max_in_flight, problem = _resolve_max_in_flight(_pick(layers, "max_in_flight"))
    if problem:
        tuning_errors.append(problem)
    ints: dict[str, int] = {}
    for key, (default, minimum) in _INT_SETTINGS.items():
        ints[key], problem = _resolve_int(_pick(layers, key), key, default, minimum)
        if problem:
            tuning_errors.append(problem)
    tuning_errors.extend(_stall_bound_problems(ints))
    routes, route_problems = _resolve_routes(layers)
    tuning_errors.extend(route_problems)
    probed_route, probe_problems = _resolve_probed_route(repo_section, repo)
    tuning_errors.extend(probe_problems)
    errors.extend(tuning_errors)

    login, problem, warning = _resolve_login(packaged, declared["operator"])
    if problem:
        errors.append(problem)
    if warning:
        warnings.append(warning)

    return ResolvedConfig(
        implementer_timeout_seconds=ints["implementer_timeout_seconds"],
        fixer_timeout_seconds=ints["fixer_timeout_seconds"],
        review_timeout_seconds=ints["review_timeout_seconds"],
        engine_stall_seconds=ints["engine_stall_seconds"],
        engine_stall_startup_grace_seconds=ints[
            "engine_stall_startup_grace_seconds"
        ],
        max_iterations=ints["max_iterations"],
        loop_short_poll_seconds=ints["loop_short_poll_seconds"],
        loop_max_poll_seconds=ints["loop_max_poll_seconds"],
        loop_heartbeat_seconds=ints["loop_heartbeat_seconds"],
        loop_quiescent_stop_after=ints["loop_quiescent_stop_after"],
        transient_cooldown_seconds=ints["transient_cooldown_seconds"],
        transient_cooldown_reset_buffer_seconds=ints[
            "transient_cooldown_reset_buffer_seconds"
        ],
        impl_engine=routes["impl_engine"],
        model=routes["model"],
        fix_engine=routes["fix_engine"],
        fix_model=routes["fix_model"],
        review_model=routes["review_model"],
        review_effort=routes["review_effort"],
        effort=routes["effort"],
        fix_effort=routes["fix_effort"],
        probed_route=probed_route,
        login=login,
        allow_touching=allow_touching,
        approved_reviewers=approved_reviewers,
        checklist_paths=checklist_paths,
        verify_tools=verify_tools,
        base_ref=base_ref,
        permission_mode=permission_mode,
        engine_settings=engine_settings,
        max_in_flight=max_in_flight,
        scan_repos=scan_repos,
        errors=tuple(errors),
        warnings=tuple(warnings),
        repo_configured=repo_configured,
        tuning_errors=tuple(tuning_errors),
    )


def config_error_settings(config: ResolvedConfig, repo: str) -> list[str]:
    """Return safe setting identifiers for a public configuration error.

    Resolver diagnostics can contain credentials, paths, repository keys and
    rejected values. Match only their known prefixes and return constant key
    names. Unknown diagnostics become ``configuration``; never forward their
    text or infer a setting from the rejected value. This is presentation only:
    :func:`resolve_config` remains the sole validator and precedence owner.
    """
    settings: set[str] = set()
    prefixes = (
        "the packaged floor [defaults] ", "[defaults] ",
        f'[repos."{repo}"] ', "the command line ", "[operator] ",
    )
    known_keys = (*_CONFIG_KEYS, *_CONFIG_PROBE_KEYS, "login")
    for problem in config.errors:
        setting = "configuration"
        for prefix in prefixes:
            if problem.startswith(prefix):
                setting = next(
                    (key for key in known_keys
                     if problem[len(prefix):].startswith(f"{key}:")),
                    "configuration",
                )
                break
        else:
            for name in (_OPERATOR_CONFIG_ENV, _CLAUDE_CONFIG_DIR_ENV):
                if problem.startswith(f"${name}:"):
                    setting = name
            for name in ("defaults", "operator", "repos", "scan"):
                if problem.startswith((f"[{name}]:", f"packaged config.toml [{name}]:")):
                    setting = name
            if problem.startswith('[repos."'):
                setting = "repos"
            elif problem.startswith("[scan] repos:"):
                setting = "scan.repos"
            elif problem.startswith("packaged config.toml:"):
                setting = "packaged_config"
            elif problem.startswith("engine_stall_seconds ("):
                setting = "engine_stall_seconds"
                bound = re.match(
                    r"engine_stall_seconds \([0-9]+\) must be less than "
                    r"(implementer_timeout_seconds|fixer_timeout_seconds) ", problem,
                )
                if bound:
                    settings.add(bound[1])
        settings.add(setting)
    return sorted(settings)


def _resolve_probed_route(
    section: Mapping[str, object], repo: str,
) -> tuple[tuple[str, str] | None, list[str]]:
    """Read setup's evidence without inferring it from routing defaults."""
    if not any(key in section for key in _CONFIG_PROBE_KEYS):
        return None, []
    label = f'[repos."{repo}"]'
    missing = [f"{label} {key}: missing setup probe metadata; rerun setup"
               for key in _CONFIG_PROBE_KEYS if key not in section]
    if missing:
        return None, missing
    engine, engine_problem = _resolve_string(
        (label, section["probed_impl_engine"]), "probed_impl_engine", "",
        lambda value: value in ENGINE_ROUTES,
    )
    model, model_problem = _resolve_string(
        (label, section["probed_model"]), "probed_model", "",
        lambda value: ENGINE_MODEL_RE.fullmatch(value) is not None,
        blank_means="the engine's default model",
    )
    problems = [problem for problem in (engine_problem, model_problem) if problem]
    return (None if problems else (engine, model)), problems


def _resolve_max_in_flight(
    picked: tuple[str, object] | None,
) -> tuple[int, str]:
    """Resolve the admission cap, reporting any value other than an int >= 1.

    The cap has no conservative reading. A lower cap starves a queue the
    operator sized for, a higher one spends what they did not budget, and
    ``0`` would stop all dispatch while the tick still reported a clean run.
    TOML ``true`` is a Python ``int``, so it is rejected by name.

    Args:
        picked: The ``(label, value)`` pair from :func:`_pick`, or ``None``.

    Returns:
        ``(value, problem)``. On a problem the value is
        :data:`DEFAULT_MAX_IN_FLIGHT`, for a caller that refuses to start.
    """
    if picked is None:
        return DEFAULT_MAX_IN_FLIGHT, ""
    label, value = picked
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_MAX_IN_FLIGHT, (
            f"{label} max_in_flight: expected an integer of at least 1, got "
            f"{type(value).__name__}"
        )
    if value < 1:
        return DEFAULT_MAX_IN_FLIGHT, (
            f"{label} max_in_flight: expected an integer of at least 1, got "
            f"{value}"
        )
    return value, ""


def _resolve_int(
    picked: tuple[str, object] | None, key: str, default: int, minimum: int,
) -> tuple[int, str]:
    """Resolve one integer setting, reporting any value it cannot use.

    TOML ``true`` is a Python ``int``, so a boolean is refused by name. A float
    or a string is refused rather than rounded or parsed: the operator wrote a
    deadline or a count, and the one that runs must be the one they wrote.

    Args:
        picked: The ``(label, value)`` pair from :func:`_pick`, or ``None``.
        key: The setting name, for the message.
        default: The value when no layer declares the key.
        minimum: The smallest accepted value.

    Returns:
        ``(value, problem)``. On a problem the value is ``default``, for a
        caller that refuses to start.
    """
    if picked is None:
        return default, ""
    label, value = picked
    if isinstance(value, bool) or not isinstance(value, int):
        return default, (
            f"{label} {key}: expected an integer of at least {minimum}, got "
            f"{type(value).__name__}"
        )
    if value < minimum:
        return default, (
            f"{label} {key}: expected an integer of at least {minimum}, got {value}"
        )
    if key in _CACHE_CLIFF_KEYS and value == _CADENCE_TTL_CLIFF:
        return default, (
            f"{label} {key}: {value} is the prompt-cache TTL cliff, which the "
            "cadence never waits for; choose a shorter or a longer interval"
        )
    return value, ""


def _stall_bound_problems(ints: Mapping[str, int]) -> list[str]:
    """Report a stall threshold that cannot sit below all three engine deadlines.

    The lower bound, above :data:`MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS`, is
    the key's own minimum. The upper bound spans deadline keys, so it is checked on
    the resolved values: a threshold at or past a deadline can never report a
    stall before the deadline kills the engine.

    Args:
        ints: The resolved integer settings.

    Returns:
        One problem per deadline the threshold does not stay below.
    """
    stall = ints["engine_stall_seconds"]
    return [
        f"engine_stall_seconds ({stall}) must be less than {key} "
        f"({ints[key]}), or a stall is never reported before the deadline"
        for key in ("implementer_timeout_seconds", "fixer_timeout_seconds", "review_timeout_seconds")
        if stall >= ints[key]
    ]


def _resolve_routes(
    layers: Sequence[tuple[str, Mapping[str, object]]],
) -> tuple[dict[str, str], list[str]]:
    """Resolve the implementer, fixer and reviewer engine routes.

    Every value reaches an argv element or an environment variable of an
    engine launch, so none has a conservative reading: a malformed one is an
    error naming the key, never a different engine or model.

    A blank fixer key inherits the implementer's. The fixer model inherits only
    while the fixer runs on the implementer's engine: a model id names one
    provider's model, and handing it to another provider fails at launch.
    The fixer effort inherits on any engine: effort levels are not
    provider-specific.

    Args:
        layers: ``(label, table)`` pairs, lowest precedence first.

    Returns:
        ``(routes, problems)``, where ``routes`` holds every
        :class:`ResolvedConfig` route field.
    """
    problems: list[str] = []

    def resolve(key: str, default: str, valid: Callable[[str], bool],
                blank_means: str = "") -> str:
        value, problem = _resolve_string(
            _pick(layers, key), key, default, valid, blank_means=blank_means,
        )
        if problem:
            problems.append(problem)
        return value

    def is_engine(value: str) -> bool:
        return value in ENGINE_ROUTES

    def is_model(value: str) -> bool:
        return ENGINE_MODEL_RE.fullmatch(value) is not None

    impl_engine = resolve("impl_engine", ENGINE_ROUTES[0], is_engine)
    model = resolve("model", "", is_model, "the engine's default model")
    fix_engine = resolve(
        "fix_engine", "", is_engine, "the implementer's engine",
    ) or impl_engine
    fix_model = resolve("fix_model", "", is_model, "the implementer's model")
    if not fix_model and fix_engine == impl_engine:
        fix_model = model
    review_model = resolve(
        "review_model", "", is_model, "the operator's codex model",
    )
    review_effort = resolve(
        "review_effort", "",
        lambda v: REVIEW_EFFORT_RE.fullmatch(v) is not None,
        "the operator's codex effort",
    )

    def is_effort(value: str) -> bool:
        return value in ENGINE_EFFORT_LEVELS

    effort = resolve("effort", "", is_effort, "the engine's default effort")
    fix_effort = resolve(
        "fix_effort", "", is_effort, "the implementer's effort",
    ) or effort
    return {
        "impl_engine": impl_engine,
        "model": model,
        "fix_engine": fix_engine,
        "fix_model": fix_model,
        "review_model": review_model,
        "review_effort": review_effort,
        "effort": effort,
        "fix_effort": fix_effort,
    }, problems


def _resolve_login(
    packaged: Mapping[str, object],
    operator_table: Mapping[str, object],
) -> tuple[str, str, str]:
    """Resolve the operator's login across its three layers.

    ``[operator] login`` sits outside the ``[defaults]`` / ``[repos.*]``
    layering on purpose: it is an identity, not a per-target setting. One
    operator drives every target, so a per-repository override would mean the
    pipeline reviewing PRs as somebody else on one repo — a trust boundary
    change wearing a configuration key's clothes.

    Beneath it is the packaged file's deprecated ``[target] trusted_author``,
    read only so an install configured before the move to the operator's home
    keeps working across this change. It keeps its old lenient reading (a
    wrong-typed value warns and reads as unset, which the startup gate refuses
    on) because it is a remnant the canonical-target issue deletes, not a key
    an operator should still be setting. ``[operator] login`` itself is strict.

    Args:
        packaged: The packaged floor's parsed config.
        operator_table: The operator's ``[operator]`` table, already
            type-checked by :func:`_declared_table`.

    Returns:
        ``(login, problem, warning)``; each of the latter two is ``""`` when
        there is nothing to report.
    """
    warning = ""
    legacy = _table(packaged, "target").get("trusted_author")
    if legacy is not None and not isinstance(legacy, str):
        warning = (
            "the packaged floor [target] trusted_author: expected a string, "
            f"got {type(legacy).__name__} — reading no operator login"
        )
        legacy = ""
    floor = legacy.strip() if isinstance(legacy, str) else ""

    login, problem = _resolve_string(
        _pick([("[operator]", operator_table)], "login"),
        "login", floor, lambda v: bool(_LOGIN_RE.match(v)),
        blank_means="no operator configured, which the startup gate refuses",
    )
    if problem:
        return floor, problem, warning

    env = os.environ.get("PIPELINE_TRUSTED_AUTHOR", "").strip()
    return (env or login), "", warning


def _ends_with_policy_suffix(path: str) -> bool:
    """Return whether ``path`` is one of the pipeline's own policy files.

    Matches either the pipeline's own gate files by full path suffix, or any
    file inside one of the directories the install reads wholesale.

    Compares whole path segments so the match cannot straddle a segment
    boundary: ``other-skills/tick/config.toml`` is not a hit, while
    ``.claude/skills/...``, ``skills/...`` and any plugin prefix all are.

    Args:
        path: A repo-relative path from ``git diff --name-only``.

    Returns:
        ``True`` when the path's trailing segments match a suffix in
        :data:`_POLICY_PATH_SUFFIXES`, or when its parent directory's trailing
        segments match one in :data:`_POLICY_DIR_SUFFIXES`.
    """
    segments = tuple(path.split("/"))
    if any(segments[-len(s):] == s for s in _POLICY_PATH_SUFFIXES):
        return True
    parent = segments[:-1]
    return any(parent[-len(d):] == d for d in _POLICY_DIR_SUFFIXES)


def touches_review_policy(changed_paths: list[str], repo: str = "") -> bool:
    """Return whether a diff modifies any file that governs the reviewer.

    The reviewer runs inside the candidate's checked-out worktree and is told to
    apply ``CLAUDE.md``/``AGENTS.md`` and the target's own review checklists. A
    PR that edits any of those steers the very reviewer that decides its own promotion —
    an untrusted-input-controls-the-validator inversion. The dispatch consults
    this and routes such PRs to a human instead of auto-reviewing them under
    instructions the PR itself supplies. Files are matched at any directory level
    because codex loads the nearest governing file up the tree.

    The pipeline's own gate files (``config.toml``, ``pipeline.py``, ``tick.py``)
    match by path suffix instead, so the self-protection holds at every install
    depth rather than only where the skills were incubated.

    The operator may opt out via the resolved ``allow_touching`` (see
    :func:`resolve_config`) when they accept that the review runs under
    instructions the PR itself supplies.

    Args:
        changed_paths: Repo-relative paths from ``git diff --name-only`` (blank
            lines are ignored).
        repo: The resolved target as ``owner/name``, so a per-repository
            ``allow_touching`` or ``checklist_paths`` override applies. Empty
            resolves the operator-wide defaults only.

    Returns:
        ``True`` if any changed path is a reviewer-governing policy file,
        unless the effective config explicitly opts out.
    """
    # Resolve once: a diff can carry thousands of paths, and each resolution
    # re-parses both config files.
    config = resolve_config(repo)
    if config.allow_touching:
        return False
    checklist_paths = config.checklist_paths
    for path in changed_paths:
        path = path.strip()
        if not path:
            continue
        if os.path.basename(path) in _POLICY_BASENAMES:
            return True
        if path in checklist_paths:
            return True
        if _ends_with_policy_suffix(path):
            return True
    return False


def _parse_result_sentinel(obj: object, sentinels: tuple[str, ...]) -> str | None:
    """Extract the last matching sentinel from a ``claude -p`` result.

    Shared parser for both implementer and fixer: each engine emits a literal
    sentinel token in the ``result`` field of ``result.json``. When multiple
    sentinels appear (e.g. the model changed its mind mid-run), the *last* one
    wins — it reflects the final state.

    Args:
        obj: The parsed ``result.json`` contents (any JSON value).
        sentinels: The recognised sentinel tokens to search for.

    Returns:
        The sentinel string, or ``None`` if ``obj`` is not a valid result or
        contains no recognised sentinel.
    """
    if not isinstance(obj, dict):
        return None
    result_text = obj.get("result")
    if not isinstance(result_text, str):
        return None
    last_sentinel: str | None = None
    last_pos = -1
    for sentinel in sentinels:
        pos = result_text.rfind(sentinel)
        if pos > last_pos:
            last_pos = pos
            last_sentinel = sentinel
    return last_sentinel


def parse_implementation_result(obj: object) -> str | None:
    """Extract the implementer sentinel from a ``claude -p --output-format json`` result.

    The implementer's ``result.json`` is the JSON output of ``claude -p``. The
    ``result`` field contains the model's text output, which must end with one of
    the sentinel tokens (``IMPLEMENTATION_COMPLETE`` or ``NEEDS_HUMAN_DECISION``).
    When both appear (e.g. the model changed its mind mid-run), the *last* one wins
    — it reflects the final state.

    Args:
        obj: The parsed ``result.json`` contents (any JSON value).

    Returns:
        The sentinel string, or ``None`` if ``obj`` is not a valid result or
        contains no recognised sentinel.
    """
    return _parse_result_sentinel(obj, _IMPLEMENTATION_SENTINELS)


def parse_fix_result(obj: object) -> str | None:
    """Extract the fixer sentinel from a ``claude -p --output-format json`` result.

    The fixer's ``result.json`` uses ``FIXES_PUSHED_RECHECK_NEEDED`` (code
    committed and pushed), ``NO_CHANGES_RECHECK_NEEDED`` (reasoned no-change
    findings, ready for re-review), or ``NEEDS_HUMAN_DECISION``
    (blocked on a finding that is out of scope). Mirrors
    :func:`parse_implementation_result` with the fixer sentinel set.

    Args:
        obj: The parsed ``result.json`` contents (any JSON value).

    Returns:
        The sentinel string, or ``None`` if ``obj`` is not a valid result or
        contains no recognised sentinel.
    """
    if isinstance(obj, dict) and "report_protocol_version" in obj:
        report, _ = fixer_reports.parse_result(obj, obj["report_protocol_version"])
        return report["outcome"] if report else None
    return _parse_result_sentinel(obj, _FIXER_SENTINELS)


def held_completion_outcome(stage: str, result_obj: object, *, report_protocol_version: int | None = None,
                            fix_marker: dict | None = None) -> str:
    """Return the validated engine outcome completed under a human hold.

    Args:
        stage: Pipeline stage that produced the result.
        result_obj: Parsed stage result payload.

    Returns:
        The stage's validated verdict or sentinel, or
        :data:`OUTCOME_UNRECOGNIZED` when the payload is malformed or has no
        recognized outcome.

    Raises:
        ValueError: If ``stage`` is not a known pipeline stage.
    """
    if stage == "review":
        outcome = validate_review(result_obj, report_protocol_version=report_protocol_version)
    elif stage == "implement":
        outcome = parse_implementation_result(result_obj)
    elif stage == "fix":
        if report_protocol_version is not None:
            report, problem = fixer_reports.parse_result(result_obj, report_protocol_version)
            marker = fix_marker or {}
            if report and not problem:
                problem = fixer_reports.binding_problem(
                    report, dispatch_id=marker.get("dispatch_id", ""),
                    pinned_head=marker.get("head_sha", ""), review_sha256=marker.get("review_sha256", ""),
                    review_handoff=marker.get("review_handoff"),
                ) or fixer_reports.outcome_problem(
                    report, current_head=report["published_head"],
                    verification_unconfigured=marker.get("verification_unconfigured") is True,
                )
            outcome = report["outcome"] if report and not problem else None
        elif isinstance(result_obj, dict) and "report_protocol_version" in result_obj:
            outcome = None
        else:
            outcome = parse_fix_result(result_obj)
    else:
        raise ValueError(f"unknown pipeline stage: {stage}")
    return outcome or OUTCOME_UNRECOGNIZED


def classify_engine_result_failure(result_obj: object) -> str | None:
    """Classify a transient provider failure in an engine result.

    Args:
        result_obj: Parsed ``claude -p --output-format json`` result.

    Returns:
        ``"quota_or_limit"`` for a recognized quota/session-limit result,
        otherwise ``None``.
    """
    if not isinstance(result_obj, dict):
        return None
    api_error_status = result_obj.get("api_error_status")
    if not isinstance(api_error_status, bool) and api_error_status == 429:
        return "quota_or_limit"
    subtype = result_obj.get("subtype")
    if isinstance(subtype, str) and subtype.casefold() in {
        "quota_exceeded",
        "rate_limit_error",
        "usage_limit_error",
    }:
        return "quota_or_limit"
    result_text = result_obj.get("result")
    if not isinstance(result_text, str):
        return None
    normalized = result_text.casefold().replace("’", "'")
    if any(
        phrase in normalized
        for phrase in (
            "you've hit your session limit",
            "quota exceeded",
            "usage limit reached",
            "rate limit exceeded",
        )
    ):
        return "quota_or_limit"
    return None


def _transient_engine_result_decision(
    result_obj: object,
    pre_label: str,
) -> dict[str, object] | None:
    """Return the shared implementer/fixer retry decision when transient."""
    failure_class = classify_engine_result_failure(result_obj)
    if failure_class is None:
        return None
    return {
        "action": "retry",
        "sentinel": None,
        "label": pre_label,
        "failure_class": failure_class,
    }


# Stable, controller-owned evidence. It describes grants, never test execution.
VERIFICATION_WAIVER = "VERIFICATION_UNCONFIGURED_WAIVER"
VERIFICATION_WARNING = (
    "Verification unconfigured: `verify_tools` was empty for at least one "
    "implementation or fixer dispatch. Waiver: no operator-defined verification "
    "grants; this is not evidence that checks passed."
)


def has_visible_verification_warning(body: str) -> bool:
    """Recognize only the controller's standalone opening paragraph.

    A matching line later in arbitrary Markdown may be hidden by an HTML
    comment, a collapsed details element, or an unclosed fence. The first
    paragraph has no preceding engine-controlled rendering context.
    """
    normalized = body.replace("\r\n", "\n")
    return normalized.strip() == VERIFICATION_WARNING or normalized.startswith(
        VERIFICATION_WARNING + "\n\n",
    )


def with_verification_warning(body: str, *, required: bool) -> str:
    """Prepend visible controller evidence, preserving engine-authored bytes."""
    if required and not has_visible_verification_warning(body):
        return VERIFICATION_WARNING + "\n\n" + body
    return body


def has_verification_waiver(result_obj: object, sentinel: str) -> bool:
    """Require an explicit waiver line immediately before the success sentinel."""
    text = result_obj.get("result") if isinstance(result_obj, dict) else None
    if not isinstance(text, str):
        return False
    # Match the same last token occurrence that supplies the outcome, never an
    # earlier example or an abandoned success claim. It must occupy its line.
    before, token, after = text.rpartition(sentinel)
    preceding, newline, indent = before.rpartition("\n")
    lines = [line.strip() for line in preceding.splitlines() if line.strip()]
    return bool(
        token and newline and not indent.strip()
        and not after.split("\n", 1)[0].strip()
        and lines and lines[-1] == VERIFICATION_WAIVER
    )


def decide_implementation_harvest(
    *,
    result_obj: object,
    pre_label: str,
    verification_unconfigured: bool = False,
) -> dict[str, object]:
    """Decide the outcome of a finished implementer engine.

    The implementer emits a sentinel token literally in ``result.json``. The
    harvest parses it and decides: ``create_pr`` (code committed and pushed,
    ready for PR creation) or ``escalate`` (blocked, missing sentinel, or
    unparseable output). Unlike the review harvest, there is no drift guard or
    iteration cap — the implementer runs once against a fresh branch.

    Args:
        result_obj: The parsed ``result.json`` contents (any JSON value).
        pre_label: The issue's label before dispatch (``ai:available``); used
            for the escalation label revert target (informational, not acted on
            directly since escalation always goes to ``ai:needs-human-decision``).
        verification_unconfigured: Whether dispatch pinned empty verification
            grants; successful completion then requires an explicit waiver.

    Returns:
        A decision mapping with keys: ``action`` (``"create_pr"`` |
        ``"escalate"``), ``label`` (the label to set on escalation, empty for
        ``create_pr`` since the PR is created fresh), and ``sentinel`` (the
        extracted sentinel string, or ``None``).
    """
    sentinel = parse_implementation_result(result_obj)
    if (
        sentinel == SENTINEL_IMPL_COMPLETE and verification_unconfigured
        and not has_verification_waiver(result_obj, sentinel)
    ):
        return {"action": "escalate", "sentinel": sentinel,
                "label": LABEL_NEEDS_HUMAN, "reason": "missing verification waiver"}
    if sentinel == SENTINEL_IMPL_COMPLETE:
        return {"action": "create_pr", "sentinel": sentinel, "label": ""}
    if sentinel is None:
        transient = _transient_engine_result_decision(result_obj, pre_label)
        if transient is not None:
            return transient
    return {
        "action": "escalate",
        "sentinel": sentinel,
        "label": LABEL_NEEDS_HUMAN,
    }


def _sanitize_engine_report(value: object) -> str:
    """Return a bounded, comment-safe excerpt of engine-authored result text.

    The excerpt is model output embedded in a shared record, so three
    substrings are neutralised beyond the length bound. HTML comment delimiters
    go first: the pipeline's round-counting and idempotency markers *are* HTML
    comments. Neutralizing quoted markers keeps controller metadata distinct
    from engine prose; readers also require an authenticated first-line header.
    The fence delimiter and the closing ``</details>`` tag go next, because an
    unbalanced one in the payload would let the remainder of the report escape the block it is rendered in.

    Args:
        value: The engine's ``result`` field (any JSON value; non-strings yield
            an empty excerpt).

    Returns:
        The sanitised excerpt, truncated with an ellipsis marker, or ``""``.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if len(text) > _FIX_REPORT_LIMIT:
        text = f"{text[:_FIX_REPORT_LIMIT]}\n… (truncated)"
    return _neutralize_comment_markup(text)


def render_implementation_escalation_comment(
    *,
    result: str,
    sentinel: str | None,
    reason: str,
    permission_mode: object,
    verify_tools: object,
    evidence_path: str,
) -> str:
    """Explain a result-driven implementer escalation from dispatch evidence.

    Policy comes from the dispatch marker, never the current configuration.
    Legacy or malformed metadata is reported as unknown. Engine text, denial
    commands, and grants are bounded and neutralized before publication; other
    tool inputs are deliberately omitted.
    """
    obj = _load_json_string(result, None)
    obj = obj if isinstance(obj, dict) else {}
    stopped = reason or (
        f"the implementer emitted `{sentinel}`"
        if sentinel else "the implementer ended without a completion sentinel"
    )
    unknown = "unknown (not recorded at dispatch)"
    mode = permission_mode if permission_mode in ("auto", "dontAsk") else unknown
    lines = [
        "**Implementer run — escalated for a human decision.**",
        "",
        f"- Why it stopped: {stopped}",
        f"- Engine sentinel: {f'`{sentinel}`' if sentinel else '_none emitted_'}",
        f"- Permission mode at dispatch: {mode}",
        "",
        "**Engine's last message (bounded excerpt)**",
        "",
        "```text",
        _sanitize_engine_report(obj.get("result")) or "No readable engine message.",
        "```",
    ]
    denials = obj.get("permission_denials")
    if isinstance(denials, list) and denials:
        entries = []
        for denial in denials[:10]:
            denial = denial if isinstance(denial, dict) else {}
            tool = denial.get("tool_name")
            tool = tool[:100] if isinstance(tool, str) else "unknown tool"
            inputs = denial.get("tool_input")
            command = inputs.get("command") if isinstance(inputs, dict) else None
            command = command if isinstance(command, str) else "command unavailable"
            if len(command) > 500:
                command = command[:500] + " … (truncated)"
            entries.append(f"{tool}: {command}")
        if len(denials) > 10:
            entries.append("… (truncated; first 10 denials shown)")
        lines.extend([
            "", f"**Denied commands ({len(denials)} reported; bounded excerpt)**",
            "", "```text", _sanitize_engine_report("\n".join(entries)), "```",
        ])
    else:
        lines.extend(["", "Denied commands: none recorded in the result."])
    if not isinstance(verify_tools, list) or not all(isinstance(v, str) for v in verify_tools):
        grants = unknown
    elif not verify_tools:
        grants = "none (empty at dispatch)"
    else:
        entries = [
            v[:500] + (" … (truncated)" if len(v) > 500 else "")
            for v in verify_tools[:10]
        ]
        if len(verify_tools) > 10:
            entries.append("… (truncated; first 10 grants shown)")
        grants = _sanitize_engine_report("\n".join(entries))
    lines.extend([
        "", "**Verification grants at dispatch (bounded excerpt)**",
        "", "```text", grants, "```",
        "", "**Full output:** local evidence on the machine that ran the tick:",
        "", "```text", _neutralize_comment_markup(evidence_path), "```",
    ])
    return "\n".join(lines)


def _neutralize_comment_markup(text: str) -> str:
    """Return ``text`` with the delimiters a comment body reserves defanged.

    Applies to any span of a published body that the controller did not author
    in full. HTML comment delimiters go first: the pipeline's round-counting and
    idempotency markers *are* HTML comments. Neutralizing them keeps controller
    metadata distinct from engine prose; readers also authenticate first-line
    headers. The fence delimiter and the closing ``</details>`` tag go next,
    because an unbalanced one lets the remainder of the span escape the block it is rendered in.

    Args:
        text: The untrusted span.

    Returns:
        The span with those four delimiters broken.
    """
    return (
        text.replace("<!--", "<!- -")
        .replace("-->", "- ->")
        .replace("```", "` ` `")
        .replace("</details>", "< /details>")
    )


# What the PR comment says about the fixer's replacement PR body, keyed by the
# state the harvest reached. Every rejection states its reason: the body is the
# handoff the next reviewer reads first, and "the pipeline left it alone" with no
# cause is indistinguishable from "the pipeline never looked". A state with no
# entry here — including the empty state, meaning the fixer offered no
# replacement — renders no line at all, so the common run keeps its old comment.
_BODY_REVISION_NOTES = {
    "ready": "replaced with the fixer's revision",
    "warning-only": "preserved the visible controller-owned verification warning",
    "not-applied": (
        "left unchanged — the fixer offered a revision, but this run did not "
        "reach re-review"
    ),
    "conflict": "left unchanged — it was edited while the fixer ran",
    "invalid": (
        "left unchanged — the fixer's revision body was empty"
    ),
    "unreadable": (
        "left unchanged — the fixer's revision file could not be read"
    ),
    "oversized": (
        "left unchanged — the fixer's revision is longer than GitHub accepts"
    ),
    "unstamped": (
        "left unchanged — the revision file did not carry this run's marker "
        "lines"
    ),
    "unavailable": (
        "left unchanged — the body this run was dispatched against could not "
        "be pinned"
    ),
    "truncated": (
        "left unchanged — the fixer was shown only part of it, so a full "
        "replacement could drop what it never saw"
    ),
    "stale-head": (
        "left unchanged — the fixer's revision names a commit other than the "
        "head it pushed"
    ),
    "closure-drift": (
        "left unchanged — the fixer's revision changes which issues merging "
        "this PR closes"
    ),
}

# The subset a caller may supply: the states ``tick._fixer_body_revision``
# actually returns. ``not-applied`` is excluded because it is derived here, from
# a ``ready`` revision on a non-``recheck`` outcome — accepting it as an input
# would let a caller render "this run did not reach re-review" onto a recheck
# comment, a sentence the tick itself can never produce.
_BODY_REVISION_INPUTS = tuple(
    sorted(state for state in _BODY_REVISION_NOTES if state != "not-applied")
)


def render_fix_comment(
    *,
    action: str,
    label: str,
    sentinel: str | None,
    pinned_head: str = "",
    current_head: str = "",
    escalation_reason: str = "",
    engine_report: str = "",
    body_revision: str = "",
    no_change_round: str = "",
    review_sha256: str = "",
    approved_input_sha256: str = "",
    marker: str = FIX_MARKER,
) -> str:
    """Render the PR comment recording one finished fixer run.

    A PR is the shared record. Without this comment a reviewer landing on the PR
    after a fixer run sees new commits and a changed label with no stated
    reason, and an ``ai:needs-human-decision`` escalation tells an operator a
    decision is required without saying what it is. The body therefore states
    the classification, the head SHA before and after the run, what the engine
    reported, and the resulting label — everything needed to audit the run from
    GitHub alone.

    Args:
        action: The harvest classification (``"recheck"`` or ``"escalate"``).
        label: The label the harvest is about to set.
        sentinel: The extracted completion sentinel, or ``None`` when the engine
            emitted none.
        pinned_head: The PR head SHA recorded at fixer dispatch. Empty renders
            as ``unknown`` (markers that predate head pinning).
        current_head: The PR's live head SHA at harvest. Empty renders as
            ``unknown``.
        escalation_reason: Why the run escalated; required for ``escalate`` and
            ignored otherwise.
        engine_report: Already-sanitised engine result text (see
            :func:`_sanitize_engine_report`). Empty omits the section.
        body_revision: The outcome of the fixer's replacement PR body, keyed
            into :data:`_BODY_REVISION_NOTES`. Empty (the common run, where the
            fixer offered none) omits the line.
        no_change_round: Stable dispatch-derived identity for an accepted
            no-change completion. Empty for pushed fixes and escalations.
        review_sha256: Fingerprint of the original review handed to the fixer.
        approved_input_sha256: Controller-pinned source identity for approved input.
        marker: The fixed header string; defaults to :data:`FIX_MARKER`.

    Returns:
        A Markdown comment body suitable for ``gh pr comment``.
    """
    headline = (
        "🔧 Fixes pushed — re-review requested."
        if action == "recheck"
        else "🚨 Escalated for a human decision."
    )
    if action == "recheck" and no_change_round:
        headline = "🔧 No changes needed — re-review requested."
    before = pinned_head or "unknown"
    after = current_head or "unknown"
    lines = [
        marker,
        *([f"<!-- pr-pipeline:no-change {no_change_round} -->"] if no_change_round else []),
        *([f"<!-- pr-pipeline:fix-review {review_sha256} -->"] if review_sha256 else []),
        *([f"<!-- pr-pipeline:approved-input {approved_input_sha256} -->"] if approved_input_sha256 else []),
        "",
        f"**Fixer run** (head `{before}` → `{after}`)",
        "",
        headline,
        "",
        f"- Outcome: `{action}` → `{label}`",
        f"- Engine sentinel: {f'`{sentinel}`' if sentinel else '_none emitted_'}",
    ]
    if action == "escalate" and escalation_reason:
        lines.append(f"- Escalation reason: {escalation_reason}")
    body_note = _BODY_REVISION_NOTES.get(body_revision)
    if body_note:
        lines.append(f"- PR body: {body_note}")
    if sentinel == SENTINEL_NO_CHANGES and engine_report.strip() == sentinel:
        lines.append("- Engine report: _the engine emitted no per-finding reasoning._")
    elif sentinel and engine_report.strip() == sentinel:
        # The engine emitted its completion sentinel and nothing else. That is a
        # protocol-valid result even though the fixer prompt asks for a change
        # summary, so the comment has to stay honest: republishing the token
        # under an "Engine report" heading would read as an answer to "what was
        # fixed" when no such answer exists. Name the gap and point at the range
        # a reader can diff instead.
        lines.append(
            "- Engine report: _the engine emitted no change summary — "
            f"review `{before}..{after}` for what was fixed_",
        )
    elif engine_report:
        lines.extend([
            "",
            "<details><summary>Engine report</summary>",
            "",
            "```",
            engine_report,
            "```",
            "",
            "</details>",
        ])
    return "\n".join(lines)


def latest_fixer_context(
    comments: list[object], *, trusted_author: str,
) -> tuple[str, str]:
    """Return the latest published recheck's no-change identity and reasoning.

    The identity is authored by harvest from the fixer dispatch, not a comment
    ID or a count: duplicate publication of one result cannot create a round.
    Dispatch pins it alongside the prompt snapshot; harvest never re-derives it
    from newer comments. Empty identity preserves legacy and pushed-fix tokens.
    """
    for comment in reversed(trusted_marker_comments(
        comments, FIX_MARKER, trusted_author=trusted_author,
    )):
        body = comment["body"]
        if f"- Outcome: `recheck` → `{LABEL_REVISED}`" not in body.splitlines():
            continue
        token = re.fullmatch(r"<!-- pr-pipeline:no-change ([a-f0-9]{64}) -->", body.splitlines()[1])
        return (token.group(1) if token else "", body)
    return "", ""


def fixer_original_review(
    comments: list[object], fixer_summary: str, *, trusted_author: str,
) -> str:
    """Resolve the exact review fingerprint pinned before the fixer launched.

    Never substitute a newer review or an edited copy. Missing provenance or
    a deleted/edited original returns empty so no-change review fails closed.
    The fixer summary requires operator authorship. The original is an operator
    review comment, or an approved input selected by :func:`fixer_input_comment`.
    New approved inputs require both source identity and body fingerprints;
    historical body-only pins retain the unmarked-comment fallback.
    """
    trusted_fixes = trusted_marker_comments(
        comments, FIX_MARKER, trusted_author=trusted_author,
    )
    if not any(comment["body"] == fixer_summary for comment in trusted_fixes):
        return ""
    # Controller metadata precedes the blank line; engine report text cannot
    # supply or override the review fingerprint.
    header = fixer_summary.replace("\r\n", "\n").split("\n\n", 1)[0]
    match = re.search(r"^<!-- pr-pipeline:fix-review ([a-f0-9]{64}) -->$", header, re.M)
    if match is None:
        return ""
    try:
        source_identity = approved_input_pin(fixer_summary)
    except ValueError:
        return ""
    if source_identity:
        for comment in comments:
            if (comment.get("id") and approved_input_identity(comment) == source_identity
                    and hashlib.sha256(comment["body"].encode()).hexdigest() == match[1]):
                try:
                    if approved_comment_provenance(comment):
                        return comment["body"]
                except ValueError:
                    return ""
        return ""
    for comment in trusted_marker_comments(
        comments, REVIEW_MARKER, trusted_author=trusted_author,
    ):
        body = comment["body"]
        if hashlib.sha256(body.encode()).hexdigest() == match[1]:
            return body
    # An approved reviewer comment that started the fix. The operator's fix
    # comment pinned its exact fingerprint at dispatch, so an identical unmarked
    # body is that input whoever posted it; a marked body never is.
    for comment in comments:
        body = comment["body"] if isinstance(comment, dict) else None
        if (isinstance(body, str) and not carries_pipeline_marker(body)
                and hashlib.sha256(body.encode()).hexdigest() == match[1]):
            return body
    return ""


def render_cap_escalation_comment(
    *,
    stage: str,
    failures: int,
    marker: str = ESCALATION_MARKER,
) -> str:
    """Render the comment recording an attempt-cap escalation.

    A harvest whose GitHub side effect keeps failing is forced to
    ``ai:needs-human-decision`` so it stops pinning an admission slot. That
    transition is remote-visible and must explain itself, and it must say so
    loudly: the run's own comment (posted before the side effect failed) states
    the outcome the harvest *wanted*, which is not the label the item ends up
    wearing. Without this note the PR carries a comment that contradicts its
    own label.

    Args:
        stage: The harvest stage (``"fix"``, ``"review"``, ``"implement"``).
        failures: How many consecutive failed harvests this escalation is
            reporting, counting the one that tripped the cap. The durable
            counter is compared against :data:`MAX_DISPATCH_ATTEMPTS` *before*
            it is incremented, so the escalating harvest is the
            ``MAX_DISPATCH_ATTEMPTS + 1``-th failure — printing the cap
            constant here would understate the count by one.
        marker: The fixed header string; defaults to :data:`ESCALATION_MARKER`.

    Returns:
        A Markdown comment body suitable for ``gh pr comment``.
    """
    return "\n".join([
        marker,
        "",
        f"**Pipeline escalation** (`{stage}` harvest)",
        "",
        "🚨 A required GitHub side effect for this run failed "
        f"{failures} times in a row, so the item is being handed to a human "
        f"rather than left holding an admission slot.",
        "",
        f"- Outcome: `attempt-cap` → `{LABEL_NEEDS_HUMAN}`",
        f"- Attempts: {failures} (cap reached)",
        "",
        "This **supersedes** any earlier comment from this run: that comment "
        "records the outcome the harvest computed, not the label the item now "
        "carries. The engine's own output is retained in the operator's local "
        "harvest-evidence archive.",
    ])


OUTCOME_TRANSIENT_EXHAUSTED = "transient-exhausted"
OUTCOME_ATTEMPT_STATE_ERROR = "attempt-state-error"

# Why each transient-path terminal transition happened, in operator-facing
# prose. Keyed by the outcome slug printed in the comment so the label, the
# slug, and the explanation can never drift apart.
_TRANSIENT_ESCALATION_CAUSES = {
    OUTCOME_TRANSIENT_EXHAUSTED: (
        "The engine returned a transient failure on every attempt of its retry "
        "budget, so this stage cannot make progress on its own."
    ),
    OUTCOME_ATTEMPT_STATE_ERROR: (
        "The durable attempt counter could not be written, so the retry budget "
        "cannot be tracked and further retries would be unbounded."
    ),
}


def render_held_completion_comment(
    *,
    stage: str,
    outcome: str,
    marker: str = ESCALATION_MARKER,
) -> str:
    """Render the publication required before releasing a held completion.

    Args:
        stage: Pipeline stage whose engine completed.
        outcome: Validated stage verdict or sentinel returned by
            :func:`held_completion_outcome`.
        marker: The fixed escalation header.

    Returns:
        A Markdown comment naming the unharvested engine outcome.

    Raises:
        ValueError: If the stage or outcome is not part of the held-completion
            protocol.
    """
    if outcome not in _HELD_COMPLETION_OUTCOMES.get(stage, frozenset()):
        raise ValueError(
            f"unknown held completion outcome for {stage}: {outcome}"
        )
    return "\n".join([
        marker,
        "",
        f"**Pipeline escalation** (`{stage}` reconciliation)",
        "",
        "🚨 A workflow hold was observed while the engine was running. The "
        "workflow hold remains authoritative, so the completed result is not "
        "being harvested.",
        "",
        f"- Engine outcome: `{outcome}`",
        "",
        "The engine's output is retained in the operator's local "
        "harvest-evidence archive.",
    ])


def render_transient_escalation_comment(
    *,
    stage: str,
    outcome: str,
    failure_class: str,
    attempts: int | None = None,
    evidence_archived: bool = False,
    marker: str = ESCALATION_MARKER,
) -> str:
    """Render the comment recording a transient-path escalation.

    The transient retry path ends at ``ai:needs-human-decision`` in two ways:
    the engine keeps failing transiently until the retry budget is spent, or the
    counter that bounds that budget cannot be persisted. Both are remote-visible
    terminal transitions, so both must explain themselves — a label that turns
    red because of a quota outage looks identical to one that turned red because
    the work was rejected.

    Args:
        stage: The harvest stage (``"fix"``, ``"review"``, ``"implement"``).
        outcome: :data:`OUTCOME_TRANSIENT_EXHAUSTED` or
            :data:`OUTCOME_ATTEMPT_STATE_ERROR`.
        failure_class: The engine failure classification that drove the retries.
        attempts: The attempt number this escalation ends on, when the retry
            budget was being tracked; ``None`` when it could not be.
        evidence_archived: Whether the run's engine output was archived locally.
            Only the exhaustion path archives, so the pointer is conditional
            rather than a standing promise.
        marker: The fixed header string; defaults to :data:`ESCALATION_MARKER`.

    Returns:
        A Markdown comment body suitable for ``gh pr comment``.

    Raises:
        ValueError: If ``outcome`` is not a known transient escalation outcome.
    """
    cause = _TRANSIENT_ESCALATION_CAUSES.get(outcome)
    if cause is None:
        raise ValueError(f"unknown transient escalation outcome: {outcome}")
    lines = [
        marker,
        "",
        f"**Pipeline escalation** (`{stage}` harvest)",
        "",
        f"🚨 {cause} It is being handed to a human rather than left holding an "
        "admission slot.",
        "",
        f"- Outcome: `{outcome}` → `{LABEL_NEEDS_HUMAN}`",
        f"- Engine failure class: `{failure_class}`",
    ]
    if attempts is not None:
        lines.append(f"- Attempts: {attempts} (budget exhausted)")
    if evidence_archived:
        lines.extend([
            "",
            "The engine's own output is retained in the operator's local "
            "harvest-evidence archive.",
        ])
    return "\n".join(lines)


OUTCOME_DISPATCH_SETUP_FAILED = "dispatch-setup-failed"
OUTCOME_AMBIGUOUS_MARKER = "ambiguous-marker"
OUTCOME_REVIEW_POLICY_TOUCHED = "review-policy-touched"

# Why each dispatch-side terminal transition happened, in operator-facing prose.
# Keyed by the outcome slug printed in the comment, for the same reason as
# :data:`_TRANSIENT_ESCALATION_CAUSES`: the label, the slug, and the explanation
# cannot drift apart if they are written once.
#
# Each cause states what an operator would otherwise have to infer from an
# absence — whether any engine ran, and whether anything was kept — because none
# of these paths leaves an evidence bundle to point at.
_DISPATCH_ESCALATION_CAUSES = {
    OUTCOME_DISPATCH_SETUP_FAILED: (
        "Dispatch setup failed before any engine was launched, so no work was "
        "performed and no engine output exists. The scratch directory is "
        "removed rather than archived."
    ),
    OUTCOME_AMBIGUOUS_MARKER: (
        "A dispatch marker was found that this tick could not prove safe to "
        "clear, so it cannot tell whether an engine still owns the item. No new "
        "engine was launched, and the scratch directory is retained locally for "
        "inspection."
    ),
    OUTCOME_REVIEW_POLICY_TOUCHED: (
        "This PR changes files that govern the reviewer's own instructions, so "
        "it is not auto-reviewed: the review would run under instructions the "
        "PR itself supplies. Nothing is wrong with the PR — it needs a human "
        "reviewer."
    ),
}


def render_dispatch_escalation_comment(
    *,
    stage: str,
    outcome: str,
    reason: str,
    engine: str = "",
    marker: str = ESCALATION_MARKER,
) -> str:
    """Render the comment recording a dispatch-side escalation.

    DISPATCH escalates to ``ai:needs-human-decision`` before it launches
    anything: setup broke, an existing marker leaves custody ambiguous, or the
    PR is one the reviewer may not judge. All are remote-visible terminal
    transitions with *no* engine result behind them — the setup path deletes its
    scratch, and none of them writes a ``.harvest-evidence/`` bundle. Without
    this comment the only account of the transition is the operator's local,
    rotated tick log, so the item's own history explains nothing.

    The body is a controller fact throughout: no engine ran, so nothing here is
    engine-authored. The reason is still passed through
    :func:`_neutralize_comment_markup` — one of them interpolates the PR's base
    ref, which the PR author names.

    Args:
        stage: The dispatch stage (``"review"``, ``"implement"``, ``"fix"``).
        outcome: One of :data:`OUTCOME_DISPATCH_SETUP_FAILED`,
            :data:`OUTCOME_AMBIGUOUS_MARKER`, or
            :data:`OUTCOME_REVIEW_POLICY_TOUCHED`.
        reason: The failure reason the tick recorded.
        engine: The engine that would have been launched, or ``""`` when the
            escalation is not tied to one.
        marker: The fixed header string; defaults to :data:`ESCALATION_MARKER`.

    Returns:
        A Markdown comment body suitable for ``gh pr comment``.

    Raises:
        ValueError: If ``outcome`` is not a known dispatch escalation outcome.
    """
    cause = _DISPATCH_ESCALATION_CAUSES.get(outcome)
    if cause is None:
        raise ValueError(f"unknown dispatch escalation outcome: {outcome}")
    lines = [
        marker,
        "",
        f"**Pipeline escalation** (`{stage}` dispatch)",
        "",
        f"🚨 {cause} It is being handed to a human.",
        "",
        f"- Outcome: `{outcome}` → `{LABEL_NEEDS_HUMAN}`",
        f"- Reason: {_neutralize_comment_markup(reason)}",
    ]
    if engine:
        lines.append(f"- Engine: `{engine}` (never launched)")
    return "\n".join(lines)


def _fix_escalation_reason(sentinel: str | None, *, head_advanced: bool) -> str:
    """Return the operator-facing reason one fixer run escalated.

    Args:
        sentinel: The extracted completion sentinel, or ``None``.
        head_advanced: Whether a ``FIXES_PUSHED`` claim was contradicted by an
            unchanged live head (``False`` only for that contradiction).

    Returns:
        A single sentence naming the escalation cause.
    """
    if sentinel == SENTINEL_NO_CHANGES:
        return (
            "a no-change completion requires an unchanged pinned head, a dispatch "
            "identity, a pinned review fingerprint, and a bounded summary with an "
            "explicit Decision: no-change "
            "and non-empty Reason: for every Finding:."
        )
    if not head_advanced:
        return (
            "the engine reported fixes were pushed, but the PR head did not "
            "advance — the push never landed, so there is nothing to re-review."
        )
    if sentinel == SENTINEL_NEEDS_HUMAN_DECISION:
        return (
            "the fixer reported it is blocked and cannot resolve the review "
            "findings without a human decision."
        )
    return (
        "the engine produced no recognised completion sentinel — its output was "
        "missing, truncated, or unparseable, so the run cannot be trusted."
    )


def _all_reported_findings_no_change(
    result_obj: object, *, verification_unconfigured: bool = False,
) -> bool:
    """Require a complete, bounded Finding/Decision/Reason block per finding.

    Parse the full result, never the truncated publication excerpt. Extra prose,
    duplicate fields, missing decisions, and empty reasons are not implicit
    no-change decisions. The no-push protocol deliberately has a narrow grammar.
    Coverage of the input review and the truth of each reason remain reviewer
    judgements; this parser checks the reported blocks, not their semantics.
    """
    if not isinstance(result_obj, dict):
        return False
    raw = result_obj.get("result")
    if not isinstance(raw, str):
        return False
    summary, separator, tail = raw.strip().rpartition(SENTINEL_NO_CHANGES)
    if verification_unconfigured:
        # The completion validator has already checked this final waiver. It
        # is protocol evidence, not part of the bounded finding summary.
        summary = summary.rstrip().removesuffix(VERIFICATION_WAIVER).rstrip()
    if not separator or tail.strip() or len(summary.strip()) > FIX_SUMMARY_BUDGET:
        return False
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    if not lines or len(lines) % 3:
        return False
    return all(
        finding.startswith("Finding:") and finding.removeprefix("Finding:").strip()
        and decision == "Decision: no-change"
        and reason.startswith("Reason:") and reason.removeprefix("Reason:").strip()
        for finding, decision, reason in zip(lines[::3], lines[1::3], lines[2::3])
    )


def decide_fix_harvest(
    *,
    result_obj: object,
    pre_label: str,
    pinned_head: str = "",
    current_head: str = "",
    body_revision: str = "",
    dispatch_id: str = "",
    review_sha256: str = "",
    approved_input_sha256: str = "",
    verification_unconfigured: bool = False,
    report_protocol_version: int | None = None,
    review_handoff: object = None,
) -> dict[str, object]:
    """Decide the outcome of a finished fixer engine.

    Versioned dispatches validate the complete report and dedicated outcome via
    ``fixer_reports``. The remaining path preserves legacy completion gates.
    A legacy fixer emits a sentinel token literally in ``result.json`` and
    harvest decides: ``recheck`` (fixes committed and pushed,
    ready for re-review at ``ai:revised``) or ``escalate`` (blocked, missing
    sentinel, or unparseable output → ``ai:needs-human-decision``).

    The sentinel is the engine's claim, not proof: ``FIXES_PUSHED`` is
    cross-checked against the live head when both head SHAs are provided. A
    fast-forward push always advances the head and force-push is not
    permitted, so a success claim with an unchanged head is contradictory
    output and escalates. Trusting it would relabel to ``ai:revised`` and
    re-enter a review round with an identity identical to the last one — the
    replay token then suppresses the comment and the comment-derived
    iteration count freezes below :data:`MAX_ITERATIONS`, looping reviewer
    and fixer spend without a terminal state.

    ``NO_CHANGES`` is a separate claim: every reported finding must carry a
    reasoned no-change decision, with an unchanged pinned head, dispatch
    identity, and original review fingerprint. Its published token makes the
    next review a distinct round,
    while retries of this fixer result retain the same token.

    Both terminal outcomes also carry ``post_comment`` and ``comment_body``:
    the harvest records the run on the PR before flipping the label, so the
    pushed head, the bounded engine report, and (on an escalation) the reason
    are auditable from GitHub rather than from a local evidence archive — which
    on the ``recheck`` path does not exist, since only ``escalate`` archives
    harvest evidence. The transient ``retry`` outcome posts nothing — it
    reverts to ``pre_label`` for re-dispatch, so there is no outcome to record
    yet.

    Args:
        result_obj: The parsed ``result.json`` contents (any JSON value).
        pre_label: The PR's label before dispatch (``ai:changes-requested``);
            informational — escalation always goes to
            ``ai:needs-human-decision``.
        pinned_head: The PR head SHA recorded in ``dispatch.json`` at fixer
            dispatch. Empty disables the push check (markers that predate
            it, or a caller without marker provenance).
        current_head: The PR's live head SHA point-read at harvest; empty
            disables the push check.
        body_revision: The state the controller reached on the fixer's
            replacement PR body (see ``tick._fixer_body_revision``). A
            publishable revision is reported as applied only on ``recheck``:
            the other outcomes did not necessarily push the head the
            replacement describes, so the caller leaves the body alone and the
            comment says so.
        dispatch_id: Fixer marker identity; required on the no-change path
            so duplicate publication cannot mint another review round.
        review_sha256: SHA-256 of the original review handed to the fixer;
            required on the no-change path so re-review can check coverage.
        verification_unconfigured: Whether dispatch pinned empty verification
            grants; successful completion then requires an explicit waiver.

    Returns:
        A decision mapping with keys: ``action`` (``"recheck"`` |
        ``"escalate"`` | ``"retry"``), ``label`` (the label to set),
        ``sentinel`` (the extracted sentinel string, or ``None``),
        ``post_comment`` (bool), and ``comment_body`` (str, empty when no
        comment is due). When a ``FIXES_PUSHED`` claim was checked against the
        live head, ``head_advanced`` reports an unchanged head as ``False``.
        A valid no-change completion also carries ``head_advanced=False``.
    """
    if report_protocol_version is not None or (isinstance(result_obj, dict) and "report_protocol_version" in result_obj):
        return _decide_fixer_report(
            result_obj=result_obj, report_protocol_version=report_protocol_version,
            dispatch_id=dispatch_id, pinned_head=pinned_head, current_head=current_head,
            review_sha256=review_sha256, review_handoff=review_handoff,
            verification_unconfigured=verification_unconfigured, body_revision=body_revision,
            approved_input_sha256=approved_input_sha256,
        )
    sentinel = parse_fix_result(result_obj)
    report = _sanitize_engine_report(
        result_obj.get("result") if isinstance(result_obj, dict) else None
    )
    no_change_round = ""

    def terminal(
        action: str, label: str, *, head_advanced: bool, reason: str = "",
    ) -> dict[str, object]:
        """Build a terminal fixer decision with its PR-comment payload."""
        decision: dict[str, object] = {
            "action": action,
            "sentinel": sentinel,
            "label": label,
            "post_comment": True,
            "comment_body": render_fix_comment(
                action=action,
                label=label,
                sentinel=sentinel,
                pinned_head=pinned_head,
                current_head=current_head,
                escalation_reason=(
                    reason or _fix_escalation_reason(sentinel, head_advanced=head_advanced)
                    if action == "escalate"
                    else ""
                ),
                # Both terminal outcomes publish the bounded, sanitised engine
                # report, because for both of them the PR comment is the only
                # durable record of what the engine did. An escalation needs it
                # so the operator being asked to decide has the engine's own
                # words. A recheck needs it because the recheck path archives no
                # harvest evidence and deletes the scratch that held
                # ``result.json`` — omitting it here loses the run entirely, and
                # "what was fixed" is what the next reviewer is landing on.
                engine_report=report,
                body_revision=(
                    "" if body_revision == "warning-only" and action != "recheck"
                    else "not-applied" if body_revision == "ready" and action != "recheck"
                    else body_revision
                ),
                no_change_round=no_change_round,
                review_sha256=review_sha256,
                approved_input_sha256=approved_input_sha256,
            ) + "\n\n" + fixer_reports.LEGACY_NOTE,
        }
        if not head_advanced:
            decision["head_advanced"] = False
        return decision

    if (
        sentinel in {SENTINEL_FIXES_PUSHED, SENTINEL_NO_CHANGES}
        and verification_unconfigured
        and not has_verification_waiver(result_obj, sentinel)
    ):
        return terminal(
            "escalate", LABEL_NEEDS_HUMAN, head_advanced=True,
            reason="empty verification grants require an explicit completion waiver, "
                   "but the engine did not provide one.",
        )
    if sentinel == SENTINEL_NO_CHANGES:
        if (
            dispatch_id and pinned_head and pinned_head == current_head
            and re.fullmatch(r"[a-f0-9]{64}", review_sha256)
            and _all_reported_findings_no_change(
                result_obj, verification_unconfigured=verification_unconfigured,
            )
        ):
            no_change_round = hashlib.sha256(dispatch_id.encode()).hexdigest()
            return terminal("recheck", LABEL_REVISED, head_advanced=False)
        return terminal("escalate", LABEL_NEEDS_HUMAN, head_advanced=True)
    if sentinel == SENTINEL_FIXES_PUSHED:
        if pinned_head and current_head and pinned_head == current_head:
            return terminal("escalate", LABEL_NEEDS_HUMAN, head_advanced=False)
        return terminal("recheck", LABEL_REVISED, head_advanced=True)
    if sentinel is None:
        transient = _transient_engine_result_decision(result_obj, pre_label)
        if transient is not None:
            return transient
    return terminal("escalate", LABEL_NEEDS_HUMAN, head_advanced=True)


def _decide_fixer_report(
    *, result_obj: object, report_protocol_version: object, dispatch_id: str,
    pinned_head: str, current_head: str, review_sha256: str, review_handoff: object,
    verification_unconfigured: bool, body_revision: str, approved_input_sha256: str,
) -> dict[str, object]:
    report, problem = fixer_reports.parse_result(result_obj, report_protocol_version)
    if report:
        problem = fixer_reports.binding_problem(
            report, dispatch_id=dispatch_id, pinned_head=pinned_head,
            review_sha256=review_sha256, review_handoff=review_handoff,
        ) or fixer_reports.outcome_problem(
            report, current_head=current_head, verification_unconfigured=verification_unconfigured,
        )
    sentinel = report["outcome"] if report else None
    recheck = not problem and sentinel in {SENTINEL_FIXES_PUSHED, SENTINEL_NO_CHANGES}
    action, label = ("recheck", LABEL_REVISED) if recheck else ("escalate", LABEL_NEEDS_HUMAN)
    header = render_fix_comment(
        action=action, label=label, sentinel=sentinel, pinned_head=pinned_head, current_head=current_head,
        escalation_reason=problem or ("The fixer requests a human decision; see its complete explanation." if not recheck else ""),
        body_revision=body_revision if recheck else ("not-applied" if body_revision == "ready" else body_revision),
        no_change_round=hashlib.sha256(dispatch_id.encode()).hexdigest() if recheck and sentinel == SENTINEL_NO_CHANGES else "",
        review_sha256=review_sha256,
        approved_input_sha256=approved_input_sha256,
    )
    metadata, rest = header.split("\n\n", 1)
    header = (metadata + "\n" + fixer_reports.COMMENT_MARKER
              + "\n<!-- pr-pipeline:fix-dispatch " + hashlib.sha256(dispatch_id.encode()).hexdigest()
              + " -->\n\n" + rest)
    # Keep the complete rendering for evidence even when the publication bound
    # requires a small human-decision notice instead. Never retry code for prose.
    rendered = fixer_reports.render(report, header) if report else ""
    if rendered and not review_reports.within_limit(rendered):
        problem = "Complete revision comment exceeds 60,000 characters or 60,000 UTF-8 bytes."
    if problem:
        action, label = "escalate", LABEL_NEEDS_HUMAN
        body = render_fix_comment(
            action=action, label=label, sentinel=sentinel, pinned_head=pinned_head, current_head=current_head,
            escalation_reason=problem + " Complete source is retained in the harvest-evidence archive; operator recovery is required.",
            review_sha256=review_sha256,
            approved_input_sha256=approved_input_sha256,
        )
    else:
        body = rendered
    return {"action": action, "label": label, "sentinel": sentinel, "post_comment": True,
            "comment_body": body, "rendered_report": rendered, "validation_reason": problem,
            "head_advanced": pinned_head != current_head}


def trusted_issue_candidate(metadata: object, *, trusted_author: str) -> bool:
    """Accept only issues authored by the configured operator.

    ``metadata`` is a ``gh issue list/view`` object containing ``author``.
    Missing or mistyped metadata fails closed before any issue text can
    become instructions for a credentialed implementer.
    """
    if not isinstance(metadata, dict):
        return False
    author = metadata.get("author")
    if not isinstance(author, dict):
        return False
    login = author.get("login")
    return isinstance(login, str) and bool(login) and login == trusted_author


def trusted_review_candidate(
    metadata: object,
    *,
    trusted_author: str,
    trusted_repo: str,
) -> bool:
    """Return whether PR metadata satisfies the shipped reviewer trust boundary.

    The review process needs the operator's readable Codex credential. Until the
    stronger isolation lands (see the architecture doc's *Future evolution*),
    dispatch is therefore restricted to branches the operator declared trusted. Missing or mistyped metadata fails
    closed rather than weakening that boundary.

    Args:
        metadata: ``gh pr view`` JSON containing ``author``, ``headRepository``,
            and ``isCrossRepository``.
        trusted_author: GitHub login allowed to author automated-review candidates.
        trusted_repo: Case-insensitive ``owner/repo`` allowed to host the candidate head.

    Returns:
        ``True`` only for an exact author and case-insensitive repository match
        on a non-cross-repository PR.
    """
    if not isinstance(metadata, dict):
        return False
    author = metadata.get("author")
    head_repository = metadata.get("headRepository")
    if not isinstance(author, dict) or not isinstance(head_repository, dict):
        return False
    return (
        author.get("login") == trusted_author
        and same_repository(head_repository.get("nameWithOwner"), trusted_repo)
        and metadata.get("isCrossRepository") is False
    )


# Identity + provenance fields every dispatch marker (lease or published) must carry.
# Liveness, kill, cleanup, drift guards, and the stale-revert all key off these, so a
# marker missing any one is not a trustworthy dispatch record and fails closed. Each
# field guards a concrete failure mode: a missing ``head_sha``/``base_sha`` disables the
# drift guards, a missing ``deadline`` misclassifies a live engine as ``hung``, a
# missing ``pre_label`` leaves the stale revert no target, and a missing ``dispatch_id``
# prevents envelope binding. These are the review-stage fields; future implement/fix
# slices extend this set with their own provenance.
_REQUIRED_MARKER_FIELDS = frozenset(
    {"engine", "stage", "pr_number", "deadline", "base_ref", "base_sha", "head_sha",
     "pre_label", "dispatch_id"}
)

# The complete set of keys a valid marker may carry: the required provenance fields plus
# ownership signals. A published marker carries ``pid`` + ``start_time`` + ``pgid``
# (full process identity); a pre-spawn lease carries only ``state``. A key outside this
# set means the marker did not come from this pipeline's writer, so it is rejected
# rather than partially trusted.
_ALLOWED_MARKER_KEYS = _REQUIRED_MARKER_FIELDS | frozenset(
    {"pid", "start_time", "pgid", "state", "base_tip_sha", "pr_body",
     "fix_round", "review_sha256", "verification_unconfigured",
     "permission_mode", "verify_tools", "stall_seconds",
     "stall_startup_grace_seconds", "report_protocol_version", "review_handoff",
     "approved_input_sha256"}
)

# The stall limits an engine was dispatched under, with the minimum
# each must meet. Optional: markers written before the limits were configurable
# omit them and are judged against the shipped defaults.
_MARKER_STALL_LIMITS = {"stall_seconds": 1, "stall_startup_grace_seconds": 0}

# Pipeline stages a dispatch marker may legitimately record. A marker for an
# unknown stage is never one this pipeline wrote, so it is not trusted.
_VALID_STAGES = frozenset({"review", "implement", "fix"})

# Each stage has an explicit engine contract. Treat any other marker value as
# corruption rather than deriving recovery or cooldown ownership from an arbitrary
# string.
_VALID_ENGINES_BY_STAGE: dict[str, frozenset[str]] = {
    "review": frozenset({"codex"}),
    "implement": frozenset({"claude", "claude-ds"}),
    "fix": frozenset({"claude", "claude-ds"}),
}

# The only lease ``state`` a pre-spawn marker may carry. Any other value (a corrupt
# or hand-edited marker) must fail closed rather than be classified as in-flight.
_VALID_LEASE_STATES = frozenset({"launching"})

# Canonical full git commit OID (40 lowercase hex chars) — the only shape
# ``base_tip_sha`` may take, since the base-drift guard compares it by equality
# against a ``git rev-parse`` result.
_COMMIT_OID_RE = re.compile(r"[0-9a-f]{40}")

# Canonical hex SHA-256 — the only shape a ``pr_body`` block's ``digest`` may
# take, since the fixer's PR-body conflict check compares it by equality against
# a digest the harvest recomputes.
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")

# Per-stage pre-dispatch labels a marker may legitimately record. The marker's
# ``pre_label`` is the stale-revert target fed straight into reconciliation, so a
# corrupt marker naming a terminal label (e.g. ``ai:ready-for-human``) could otherwise
# let a dead, resultless dispatch silently "promote" a PR. Constrain to the real
# pre-dispatch labels for each stage.
_VALID_PRE_LABELS_BY_STAGE: dict[str, frozenset[str]] = {
    "review": frozenset({LABEL_IMPLEMENTED, LABEL_REVISED}),
    "implement": frozenset({LABEL_AVAILABLE}),
    "fix": frozenset({LABEL_CHANGES_REQUESTED}),
}


def _is_positive_int(value: object) -> bool:
    """Return whether ``value`` is a positive, non-bool ``int``."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# Every field a marker's ``pr_body`` block carries, and the predicate each must
# satisfy. Kept as one table because the block is validated as a unit: a
# candidate PR body is judged against all four together, so any subset is a
# contract the harvest cannot evaluate and must not half-trust.
_PR_BODY_PROVENANCE_FIELDS: dict[str, Callable[[object], bool]] = {
    # The body's fingerprint as dispatch read it, compared by equality.
    "digest": lambda v: isinstance(v, str) and bool(_SHA256_HEX_RE.fullmatch(v)),
    # The issue its ``## Summary`` opened by closing.
    "summary_closes": _is_positive_int,
    # Every closing directive the body declared, anywhere in it.
    "closes": lambda v: isinstance(v, list) and all(
        isinstance(ref, str) and ref for ref in cast("list[object]", v)
    ),
    # Whether the engine was shown only a prefix of the body.
    "truncated": lambda v: isinstance(v, bool),
}


def valid_pr_body_provenance(value: object) -> bool:
    """Return whether ``value`` is a complete PR-body provenance block.

    The schema half of the fixer's PR-body revision: what a marker must carry
    for a harvest to judge a replacement body at all. Exported because
    ``tick.PRBodyProvenance.from_marker`` parses the same bytes and must not
    carry a second copy of the rules — a validator and a parser that disagree
    would let a marker reconciliation trusts be rejected by the harvest, or
    worse, the reverse.

    All-or-nothing by construction. The fields are written in one dispatch and
    mean nothing individually, so a block missing one is corruption rather than
    a marker from an older version; the *block* is what may legitimately be
    absent.

    Args:
        value: The marker's ``pr_body`` field, of unknown shape.

    Returns:
        Whether every field is present and well typed.
    """
    if not isinstance(value, dict):
        return False
    block = cast("dict[str, object]", value)
    if set(block) != set(_PR_BODY_PROVENANCE_FIELDS):
        return False
    return all(
        check(block[field])
        for field, check in _PR_BODY_PROVENANCE_FIELDS.items()
    )


def valid_marker(
    obj: object,
    *,
    expected_pr: int | None = None,
    expected_stage: str | None = None,
) -> bool:
    """Return whether ``obj`` is a trustworthy dispatch marker.

    Atomic temp-then-rename prevents torn writes, but it does not prove the object
    carries fields recovery can act on. Liveness, kill, cleanup, and label
    transitions all key off this marker, so it is validated by *type and
    enumeration* — not mere key presence — and fails closed on anything it cannot
    vouch for. A marker that fails validation is treated as marker-less, so
    reconciliation retains an ``ai:reviewing`` PR untouched instead of driving an
    incorrect stale-revert or re-dispatch from a corrupt record — absence of
    proof is not proof of an orphan.

    Validation requires:

    - a mapping whose keys are all within the allowed set (no unknown field) and that
      carries every required provenance field (``engine``, ``stage``, ``pr_number``,
      ``deadline``, ``base_ref``, ``base_sha``, ``head_sha``, ``pre_label``,
      ``dispatch_id``);
    - ``engine`` is valid for its stage (``codex`` for review, ``claude`` or
      ``claude-ds`` for implement/fix);
    - ``dispatch_id`` is a non-empty string (links the marker to its completion envelope);
    - ``stage`` is a known pipeline stage, and equals ``expected_stage`` when given
      (a ``fix`` marker must not drive a ``review`` reconciliation);
    - ``pr_number`` is a real (non-bool) ``int``, and equals ``expected_pr`` when
      given (a marker for another PR is never trusted for this directory's PR);
    - ``base_ref``/``base_sha``/``head_sha`` are non-empty strings (the drift guards
      compare them; an empty/typed-wrong value would make a guard meaningless);
    - ``base_tip_sha``, when present, is a canonical full commit OID on a *review*
      marker only (it is the freshness anchor the base-drift guard compares; only
      reviewer dispatch writes it, and a malformed value would silently degrade the
      guard to the merge-base fallback);
    - ``fix_round``, when present, is a lowercase SHA-256 token on a *review*
      marker only, preserving the no-change reasoning's round identity;
    - ``review_sha256``, when present, is a lowercase SHA-256 fingerprint on a
      *fix* marker only, binding the originating review for coverage checks;
    - ``stall_seconds`` (at least 1) and ``stall_startup_grace_seconds`` (at
      least 0), when present, are integers on any stage's marker — the stall
      limits that engine was dispatched under;
    - ``pre_label`` is one of the real pre-dispatch labels (``ai:implemented`` /
      ``ai:revised``) — never an arbitrary or terminal label, which reconciliation
      would otherwise revert to;
    - ``deadline`` is a *finite* number (a string deadline would crash
      ``classify-dispatch``; ``json.loads`` also accepts ``NaN``/``Infinity``, and a
      ``NaN`` deadline makes ``now > deadline`` always false so a live engine could
      never time out);
    - **exactly one** ownership signal group — a published marker carries ``pid`` +
      ``start_time`` + ``pgid`` (full process identity that prevents PID-reuse
      mis-targeting); a pre-spawn lease carries only ``state`` (no process identity
      fields). A marker carrying both groups is corrupt.

    Args:
        obj: The object parsed from ``dispatch.json`` (any JSON value).
        expected_pr: The PR number this marker must belong to (the directory/PR
            under reconciliation), or ``None`` to skip the cross-check.
        expected_stage: The pipeline stage this marker must record, or ``None`` to
            skip the cross-check.

    Returns:
        ``True`` only if ``obj`` is a structurally valid, identity-matched marker.
    """
    if not isinstance(obj, dict):
        return False
    # Reject unknown keys, then require the full provenance set: a marker carrying a
    # field this pipeline never writes, or missing one it always writes, is not trusted.
    if not set(obj.keys()) <= _ALLOWED_MARKER_KEYS:
        return False
    if not _REQUIRED_MARKER_FIELDS.issubset(obj.keys()):
        return False

    dispatch_id = obj.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return False

    stage = obj.get("stage")
    if stage not in _VALID_STAGES:
        return False
    if expected_stage is not None and stage != expected_stage:
        return False

    engine = obj.get("engine")
    stage_engines = _VALID_ENGINES_BY_STAGE.get(str(stage), frozenset())
    if engine not in stage_engines:
        return False

    pr_number = obj.get("pr_number")
    if not (isinstance(pr_number, int) and not isinstance(pr_number, bool)):
        return False
    if expected_pr is not None and pr_number != expected_pr:
        return False

    # Provenance the drift guards compare: each must be a present, non-empty string.
    for ref_field in ("base_ref", "base_sha", "head_sha"):
        value = obj.get(ref_field)
        if not isinstance(value, str) or not value:
            return False

    # base_tip_sha is optional (markers written before the two-SHA split omit it), but
    # when present it is the freshness anchor the base-drift guard compares, so it is
    # validated as provenance, not merely allowlisted: review markers only (only
    # reviewer dispatch writes it), and a canonical full commit OID. A mistyped value
    # would make harvest silently fall back to base_sha (the merge-base), gutting the
    # stale-base guard precisely at the field added to strengthen it.
    if "base_tip_sha" in obj:
        if stage != "review":
            return False
        base_tip_sha = obj.get("base_tip_sha")
        if not isinstance(base_tip_sha, str) or not _COMMIT_OID_RE.fullmatch(base_tip_sha):
            return False

    if "fix_round" in obj:
        fix_round = obj.get("fix_round")
        if stage != "review" or not isinstance(fix_round, str):
            return False
        if not re.fullmatch(r"[a-f0-9]{64}", fix_round):
            return False

    if "review_sha256" in obj:
        review_sha256 = obj.get("review_sha256")
        if stage != "fix" or not isinstance(review_sha256, str):
            return False
        if not re.fullmatch(r"[a-f0-9]{64}", review_sha256):
            return False

    if "approved_input_sha256" in obj:
        identity = obj["approved_input_sha256"]
        handoff = obj.get("review_handoff")
        if (stage != "fix" or not isinstance(identity, str)
                or not re.fullmatch(r"[a-f0-9]{64}", identity)
                or not isinstance(handoff, dict)
                or handoff.get("version") != review_reports.COMMENT_HANDOFF_VERSION):
            return False

    if "report_protocol_version" in obj:
        version = fixer_reports.VERSION if stage == "fix" else review_reports.VERSION
        if (stage not in {"review", "fix"} or type(obj["report_protocol_version"]) is not int
                or obj["report_protocol_version"] != version):
            return False
        if stage == "fix" and "review_handoff" not in obj:
            return False

    if "review_handoff" in obj:
        if stage != "fix" or not review_reports.valid_handoff(obj["review_handoff"]):
            return False
        if obj.get("review_sha256") != obj["review_handoff"]["comment_sha256"]:
            return False

    # Implementer launch policy is one optional snapshot. Old markers omit it;
    # a partial or malformed snapshot could misstate what the engine received.
    if "permission_mode" in obj or "verify_tools" in obj:
        grants = obj.get("verify_tools")
        if (
            stage != "implement"
            or obj.get("permission_mode") not in ("auto", "dontAsk")
            or not isinstance(grants, list)
            or not all(
                isinstance(v, str) and v.strip() and v == v.strip()
                for v in grants
            )
            or _verify_tool_body_problem(("dispatch", grants), grants)
        ):
            return False

    # pr_body is the fixer's PR-body-revision provenance: the fingerprint of the
    # body dispatch handed the engine, the closure contract a replacement must
    # keep, and whether the engine saw the whole body. Only fixer dispatch writes
    # it, and every field gates a remote write with no undo — a mistyped digest
    # would read as "the body changed while the fixer ran" and silently disable
    # the replacement, a mistyped closure set would silently reject every
    # candidate, and a missing truncation flag would let a body the engine only
    # saw a prefix of be replaced wholesale.
    #
    # One nested block, validated whole. The fields are written together and mean
    # nothing apart, so a marker carrying some of them is corruption rather than
    # a legacy marker — and the way to make that unrepresentable is to give them
    # a single key that is either wholly valid or absent. The block as a whole
    # stays optional, which is the real legacy case: markers written before the
    # revision path omit it, as do PRs whose body pins no closure.
    # Optional for pre-upgrade markers. A present value must be a boolean,
    # never a truthy string/number that can invent or suppress the waiver gate.
    if "verification_unconfigured" in obj:
        if (
            stage not in {"implement", "fix"}
            or not isinstance(obj["verification_unconfigured"], bool)
        ):
            return False
    # Stall classification reads these instead of the tick's constants, so a
    # mistyped value would silently move or disable stall detection. Only the
    # stages with a progress contract record them.
    for key, minimum in _MARKER_STALL_LIMITS.items():
        if key in obj:
            value = obj[key]
            if (
                stage not in {"implement", "fix", "review"}
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
            ):
                return False
    if "pr_body" in obj:
        if stage != "fix" or not valid_pr_body_provenance(obj.get("pr_body")):
            return False

    # pre_label is the stale-revert target; constrain to the real pre-dispatch labels
    # for this marker's stage. A review marker with an implement pre_label (or vice
    # versa) is never one this pipeline's dispatch wrote for this stage.
    pre_label = obj.get("pre_label")
    stage_pre_labels = _VALID_PRE_LABELS_BY_STAGE.get(str(stage), frozenset())
    if pre_label not in stage_pre_labels:
        return False

    deadline = obj.get("deadline")
    # bool is an int subclass; exclude it, require a real number, and reject the
    # non-finite NaN/Infinity that json.loads accepts.
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    if not math.isfinite(deadline):
        return False

    # Exactly one ownership group: a published marker carries pid + start_time + pgid
    # (full process identity); a lease carries only state (no identity fields). The
    # groups are mutually exclusive: pid with state is corrupt (classify would read
    # `launching` and reconcile would escalate without killing the live pid), and state
    # with start_time/pgid is corrupt (process identity without a pid is meaningless).
    has_pid_key = "pid" in obj
    has_state_key = "state" in obj
    has_start_time = "start_time" in obj
    has_pgid = "pgid" in obj
    if has_pid_key == has_state_key:
        return False
    if has_pid_key:
        if not has_start_time or not has_pgid:
            return False
        if not _is_positive_int(obj.get("pid")):
            return False
        start_time = obj.get("start_time")
        if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
            return False
        if not math.isfinite(start_time):
            return False
        pgid = obj.get("pgid")
        return _is_positive_int(pgid)
    # Lease: no process identity fields allowed.
    if has_start_time or has_pgid:
        return False
    return obj.get("state") in _VALID_LEASE_STATES


# Classify states that hold a concurrency slot. This answers the *admission*
# question — "could a paid engine still be running here?" — and nothing else.
#
# Cadence asks a different question of the same markers: "is there anything left
# for the loop to supervise?" A marker currently held by a human answers yes to
# the first and no to the second, so it stays in this set
# while the loop discounts it from GitHub human holds. Never widen
# this set to serve quiescence: freeing an admission slot would let a second paid
# engine launch onto work an unverifiable process may still be doing.
ENGINE_OCCUPIED_STATES = frozenset(("live", "launching", "hung"))


def in_flight_refs(
    marker_states: list[tuple[str, int, str, str, bool]],
) -> set[tuple[str, int, str]]:
    """Return distinct dispatch identities occupying this machine's spend cap.

    Item kind and stage label are both part of the identity. Issue ``#42`` and
    PR ``#42`` are different processes, as are a reviewer and fixer that a
    partial transition accidentally launched for the same PR.

    Occupancy is proved by a local dispatch marker and nothing else. An
    ``ai:`` in-flight label is a *shared workflow state* — ``CLAUDE.md`` tells
    contributors to set ``ai:implementing`` by hand before writing code, and
    experience established that the pipeline cannot read custody out of a
    label it may not own. Counting those labels as paid engines is the same conflation
    from the other side: a manual pickup, or a label whose harvest relabel was
    missed, silently consumed a spend slot forever and starved dispatch — twice,
    on 2026-07-07 and 2026-08-20. The cap bounds the engines
    *this* machine launched and can still see; nothing else is spending its
    budget.

    This changes the ceiling, not per-item acquisition. Eligibility is a
    separate gate that still reads the authoritative label set at the point of
    decision (:func:`tick._resolve_dispatchable_pickup_label`), and it was
    never an exclusive lock: dispatch runs lease → launch → relabel, so two
    ticks that both select an item before either relabel lands will both spawn,
    and the loser terminates its engine when its relabel fails. That bounded
    acquisition race predates this function and is unchanged by it — closing it
    needs compare-and-swap acquisition, which the design doc scopes out
    separately. What *is* deliberately given up here is the sum across
    machines: another host's engine no longer consumes this host's budget.

    Args:
        marker_states: ``(kind, number, in_flight_label, classify_state,
            engine_alive)`` records for every local dispatch marker.

    Returns:
        The ``(kind, number, label)`` identities occupying an in-flight slot.

    Raises:
        ValueError: If any kind, number, label, or classify state is
            malformed. Admission fails closed rather than under-counting
            paid processes.
    """
    valid_kinds = {"issue", "pr"}
    valid_labels = {LABEL_IMPLEMENTING, LABEL_REVIEWING, LABEL_FIXING}
    valid_states = {"complete", "failed", "live", "stale", "hung", "launching"}
    occupied: set[tuple[str, int, str]] = set()

    for kind, number, label, state, engine_alive in marker_states:
        if kind not in valid_kinds:
            raise ValueError(f"unknown item kind: {kind}")
        if not _is_positive_int(number):
            raise ValueError("marker item numbers must be positive integers")
        if label not in valid_labels:
            raise ValueError(f"unknown in-flight label: {label}")
        if state not in valid_states:
            raise ValueError(f"unknown dispatch state: {state}")
        if not isinstance(engine_alive, bool):
            raise ValueError("engine liveness must be a bool")
        # A living process group spends whatever its classify state says, and
        # the pipeline still owes it a harvest, an archive, or a kill — the
        # `complete`-envelope-with-a-live-child case reaches here.
        if state in ENGINE_OCCUPIED_STATES or engine_alive:
            occupied.add((kind, number, label))

    return occupied


def can_dispatch(*, in_flight: int, max_in_flight: int) -> bool:
    """Return whether another reviewer may be launched without exceeding the cap.

    The cap is the pipeline's global spend ceiling, so the decision is a tested
    primitive rather than inline arithmetic the calling tick re-derives.

    Args:
        in_flight: Engines currently holding a slot — the size of
            :func:`in_flight_refs`, which is the only admission count.
        max_in_flight: The configured ceiling (``ResolvedConfig.max_in_flight``).

    Returns:
        ``True`` if ``in_flight`` is below ``max_in_flight``.
    """
    return in_flight < max_in_flight


def reconcile_dispatch(
    *,
    current_label: str,
    marker_valid: bool,
    classify_state: str,
    pre_label: str,
    in_flight_label: str = LABEL_REVIEWING,
    dispatch_attempts: int = 0,
    attempt_threshold: int = MAX_DISPATCH_ATTEMPTS,
) -> dict[str, object]:
    """Decide what HARVEST does with one PR/issue that may have an in-flight engine.

    GitHub labels are authoritative and the worktree marker is a disposable
    accelerator, so reconciliation must be driven by *both* the label and the
    marker — never the marker alone. The gap this closes: a completed result
    whose post-dispatch label edit failed leaves the PR at its pre-dispatch
    label. A result-gated dedup would let dispatch launch a *second* engine
    over the finished result; custody-aware harvest reaps the existing one
    instead.

    A *missing* marker, by contrast, decides nothing. Markers live on local
    disk, so a tick can never see another machine's; humans also claim
    ``ai:implementing`` by hand as the documented pickup step. Absence of a
    local accelerator file on a control plane whose only shared state is
    GitHub says nothing about custody, so the markerless case is always a
    no-op and any resulting stranding is surfaced by the status dashboard for
    the operator to clear.

    "In custody" means the PR/issue is still at a label this pipeline owns for
    the current stage — the in-flight label (``ai:reviewing`` for reviews,
    ``ai:implementing`` for implementations) or the recorded ``pre_label``. Any
    other non-hold label means the item has left pipeline custody — a human
    took over — so we never clobber it: a hung process is still killed (it must
    not pin the cadence), but the label is left alone.

    The tick gates ``HOLD_LABELS`` before calling this primitive, so a held item
    never reaches it. That gate suspends *workflow* custody only — it still
    terminates a hung engine whose identity is proven ``matched``, because
    process custody never transfers to a human. What a hold prevents here is the
    label mutation and the archive, not the kill.

    Args:
        current_label: The PR/issue's current pipeline label, resolved at harvest.
        marker_valid: Whether a parseable dispatch marker exists for this PR/issue.
        classify_state: :func:`classify_dispatch` output when ``marker_valid``;
            an empty string when there is no usable marker.
        pre_label: The pre-dispatch label recorded in the marker (e.g.
            ``ai:implemented`` for review, ``ai:available`` for implement); the
            re-queue target on a stale revert.
        in_flight_label: The label this stage uses when an engine is dispatched
            (``ai:reviewing`` for review, ``ai:implementing`` for implement).
            Defaults to ``ai:reviewing`` for backward compatibility.
        dispatch_attempts: How many resultless dispatch failures this PR has already
            accumulated (durable counter that survives worktree cleanup). Bounds
            re-dispatch so a repeatable infrastructure failure cannot spend forever.
        attempt_threshold: Cap on resultless dispatch attempts; a revert that would
            reach it escalates to a human instead.

    Returns:
        A mapping with ``action`` (one of ``"escalate"``, ``"fail"``, ``"noop"``,
        ``"harvest"``, ``"cleanup"``, ``"leave"``, ``"revert"``,
        ``"kill_escalate"``, ``"kill_cleanup"``), ``kill`` (whether the engine
        PID must be killed), and ``label`` (the label to set, or ``""`` when the
        action sets none directly).
    """
    in_custody = current_label in (in_flight_label, pre_label)

    if not marker_valid:
        # No usable marker proves nothing about custody: markers are
        # machine-local, and the documented manual `ai:implementing` pickup
        # looks identical. Never act. A genuinely stranded item stays visible
        # on the status dashboard (in-flight label, no live engine).
        return {"action": "noop", "kill": False, "label": ""}

    if classify_state == "launching":
        # A launching lease seen at reconcile means the spawn crashed between
        # starting codex and publishing its PID. Unlike the markerless case
        # this is proven custody — our own marker — and the paid child's
        # liveness is unprovable, so fail closed to a human rather than
        # re-dispatch (which would double-spend). Money may be burning.
        if in_custody:
            return {"action": "escalate", "kill": False, "label": LABEL_NEEDS_HUMAN}
        return {"action": "cleanup", "kill": False, "label": ""}

    if classify_state == "hung":
        # A hung engine is always killed; the label is escalated only if still ours.
        if in_custody:
            return {"action": "kill_escalate", "kill": True, "label": LABEL_NEEDS_HUMAN}
        return {"action": "kill_cleanup", "kill": True, "label": ""}

    if classify_state == "live":
        return {"action": "leave", "kill": False, "label": ""}

    if classify_state == "complete":
        if in_custody:
            return {"action": "harvest", "kill": False, "label": ""}
        return {"action": "cleanup", "kill": False, "label": ""}

    if classify_state == "failed":
        if in_custody:
            return {"action": "fail", "kill": False, "label": LABEL_NEEDS_HUMAN}
        return {"action": "cleanup", "kill": False, "label": ""}

    # stale: dead PID, no result.
    if in_custody:
        # Bound retries: a resultless failure posts no review comment, so it is
        # invisible to the comment-based round cap. Once reverting would reach the
        # attempt cap, escalate to a human instead of re-queueing (and re-spending).
        if dispatch_attempts + 1 >= attempt_threshold:
            return {"action": "escalate", "kill": False, "label": LABEL_NEEDS_HUMAN}
        return {"action": "revert", "kill": False, "label": pre_label}
    return {"action": "cleanup", "kill": False, "label": ""}


def _valid_finding(finding: object) -> bool:
    """Return whether a single ``findings`` entry conforms to the schema."""
    if not isinstance(finding, dict):
        return False
    severity = finding.get("severity")
    if not isinstance(severity, str) or severity not in _VALID_SEVERITIES:
        return False
    return isinstance(finding.get("file"), str) and isinstance(finding.get("summary"), str)


# --- Public API (imported by tick.py) ---
#
# Each function below is the public entry point for one pipeline subcommand.
# The CLI wrappers in the # --- CLI --- section read args + stdin, call these
# functions, and print JSON. tick.py imports these directly — no subprocess.


def harvest_review(
    *,
    review: str,
    comments: str,
    pinned_base: str,
    current_base: str,
    pinned_head: str = "",
    current_head: str = "",
    pre_label: str = LABEL_IMPLEMENTED,
    threshold: int = MAX_ITERATIONS,
    diff_base_sha: str = "",
    pinned_base_ref: str = "",
    current_base_ref: str = "",
    fix_round: str = "",
    trusted_author: str,
    report_protocol_version: int | None = None,
    review_identity: str = "",
    approved_input_reservations: Iterable[str] = (),
) -> HarvestResult:
    """Decide the outcome of harvesting one finished review.

    Args:
        review: JSON string of ``review.json`` contents.
        comments: JSON string of the PR's comments array (or wrapped object).
        pinned_base: The base tip snapshot recorded at dispatch (freshness
            anchor for the drift guard).
        current_base: The base ref tip resolved now.
        pinned_head: The head SHA recorded at dispatch (reviewed commit).
        current_head: The PR head resolved now.
        pre_label: The PR's label before this review was dispatched.
        approved_input_reservations: Source fingerprints from controller-owned local receipts.
        threshold: Runaway-spend cap.
        diff_base_sha: The merge-base the reviewer's diff was anchored to
            (audit/idempotency truth). Defaults to ``pinned_base`` for markers
            that predate the two-SHA split.
        pinned_base_ref: The base branch name recorded at dispatch; compared
            against ``current_base_ref`` for retargeting (empty disables).
        current_base_ref: The PR's live base branch name at harvest (empty
            disables the retarget check).
        fix_round: No-change fixer token pinned at reviewer dispatch.

    Returns:
        A HarvestResult mapping with action, label, and stage-specific keys.
    """
    if report_protocol_version is None:
        review_obj = _load_json_string(review, None)
    else:
        try:
            review_obj = review_reports.loads(review)
        except ValueError:
            review_obj = None
    comments_list = _as_comment_list(json.loads(comments))
    result = decide_review_harvest(
        trusted_author=trusted_author,
        review_obj=review_obj,
        comments=comments_list,
        pinned_base=pinned_base,
        current_base=current_base,
        pinned_head=pinned_head,
        current_head=current_head,
        pre_label=pre_label,
        threshold=threshold,
        diff_base_sha=diff_base_sha,
        pinned_base_ref=pinned_base_ref,
        current_base_ref=current_base_ref,
        fix_round=fix_round,
        report_protocol_version=report_protocol_version,
        review_identity=review_identity,
        approved_input_reservations=approved_input_reservations,
    )
    return result  # type: ignore[return-value]


def validate_review_candidate(
    *,
    candidate: object,
    trusted_author: str,
    trusted_repo: str,
) -> bool:
    """Return whether PR metadata satisfies the trusted reviewer boundary.

    Accepts any JSON value (the raw ``gh pr view`` output); non-dict or
    mistyped values fail closed.

    Args:
        candidate: PR metadata from ``gh pr view`` (any JSON value).
        trusted_author: Allowed author login.
        trusted_repo: Allowed ``owner/repo`` head.

    Returns:
        ``True`` only for an exact author/repository match.
    """
    return trusted_review_candidate(
        candidate,
        trusted_author=trusted_author,
        trusted_repo=trusted_repo,
    )


def validate_marker(
    *,
    dispatch: object,
    pr: int | None = None,
    stage: str = "review",
) -> bool:
    """Return whether a dispatch marker is structurally valid.

    Args:
        dispatch: The parsed ``dispatch.json`` contents.
        pr: PR number the marker must belong to, or ``None`` to skip.
        stage: Pipeline stage the marker must record.

    Returns:
        ``True`` only if the marker is structurally valid and identity-matched.
    """
    return valid_marker(dispatch, expected_pr=pr, expected_stage=stage)


def harvest_implementation(
    *,
    result: str,
    pre_label: str = LABEL_AVAILABLE,
    verification_unconfigured: bool = False,
) -> HarvestResult:
    """Decide the outcome of a finished implementer engine.

    Args:
        result: JSON string of ``result.json`` contents.
        pre_label: The issue's label before dispatch.
        verification_unconfigured: Whether dispatch pinned empty verification
            grants; successful completion then requires an explicit waiver.

    Returns:
        A HarvestResult with ``action``, ``label``, and ``sentinel`` keys.
    """
    result_obj = _load_json_string(result, None)
    decision = decide_implementation_harvest(
        result_obj=result_obj,
        verification_unconfigured=verification_unconfigured,
        pre_label=pre_label,
    )
    return decision  # type: ignore[return-value]


def harvest_fix(
    *,
    result: str,
    pre_label: str = LABEL_CHANGES_REQUESTED,
    pinned_head: str = "",
    current_head: str = "",
    body_revision: str = "",
    dispatch_id: str = "",
    review_sha256: str = "",
    approved_input_sha256: str = "",
    verification_unconfigured: bool = False,
    report_protocol_version: int | None = None,
    review_handoff: object = None,
) -> HarvestResult:
    """Decide the outcome of a finished fixer engine.

    Args:
        result: JSON string of ``result.json`` contents.
        pre_label: The PR's label before dispatch.
        pinned_head: The PR head SHA recorded at fixer dispatch; with
            ``current_head``, gates the ``FIXES_PUSHED`` claim (empty
            disables the push check).
        current_head: The PR's live head SHA at harvest (empty disables the
            push check).
        body_revision: The state the controller reached on the fixer's
            replacement PR body; empty when it offered none.
        dispatch_id: Fixer marker identity required for no-change completion.
        review_sha256: Original review fingerprint pinned at fixer dispatch.
        verification_unconfigured: Whether dispatch pinned empty verification
            grants; successful completion then requires an explicit waiver.

    Returns:
        A HarvestResult with ``action``, ``label``, and ``sentinel`` keys.
    """
    try:
        result_obj = review_reports.loads(result)
    except (ValueError, TypeError, RecursionError):
        result_obj = None
    decision = decide_fix_harvest(
        result_obj=result_obj,
        report_protocol_version=report_protocol_version,
        review_handoff=review_handoff,
        verification_unconfigured=verification_unconfigured,
        pre_label=pre_label,
        pinned_head=pinned_head,
        current_head=current_head,
        body_revision=body_revision,
        dispatch_id=dispatch_id,
        review_sha256=review_sha256,
        approved_input_sha256=approved_input_sha256,
    )
    return decision  # type: ignore[return-value]


def validate_envelope(
    *,
    envelope: object,
    dispatch_id: str | None = None,
    report_protocol_version: int | None = None,
) -> bool:
    """Return whether a completion envelope is valid and dispatch-id-bound.

    Args:
        envelope: The parsed ``completion.json`` contents.
        dispatch_id: Expected ``dispatch_id`` from the marker, or ``None``.

    Returns:
        ``True`` only when the envelope is structurally valid.
    """
    if report_protocol_version is not None:
        envelope = review_reports.envelope_base(envelope, report_protocol_version, captured=True)
        if envelope is None:
            return False
        # Completion proves capture and successful engine exit. The report's
        # validity remains separate; harvest independently rejects bad content.
        envelope["result_valid"] = True
    return validate_completion_envelope(
        envelope,
        expected_dispatch_id=dispatch_id,
    )


def verify_identity(
    *,
    recorded_start: float,
    live_start: float | None,
    tolerance: float = 1.0,
) -> IdentityResult:
    """Determine whether a live process matches a recorded start time.

    Args:
        recorded_start: Epoch seconds the wrapper recorded at startup.
        live_start: Epoch seconds of the live process's start time, or
            ``None`` if unreadable.
        tolerance: Maximum allowable difference in seconds.

    Returns:
        ``"matched"``, ``"mismatched"``, or ``"unknown"``.
    """
    return verify_process_identity(
        recorded_start,
        live_start,
        tolerance=tolerance,
    )


def tick_log_open(*, log_dir: str, keep: int = 50) -> str:
    """Create a new tick log file, prune old ones, and return the path.

    Args:
        log_dir: Directory for tick log files.
        keep: Max existing log files to retain.

    Returns:
        Absolute path to the newly created (empty) log file.
    """
    return open_tick_log(log_dir, keep_n=keep)


def tick_log_write(*, log_path: str, entry: dict[str, object]) -> None:
    """Append one JSON-lines entry to a tick log file.

    Args:
        log_path: Path to the ``*.jsonl`` log file.
        entry: Mapping to serialise as a single JSON line.
    """
    append_tick_entry(log_path, entry)


def tick_log_tail(*, log_dir: str) -> list[dict[str, object]]:
    """Return parsed entries from the most recent tick log file.

    Args:
        log_dir: Directory containing ``tick-*.jsonl`` log files.

    Returns:
        List of parsed entry dicts from the newest log file.
    """
    return read_latest_tick_log(log_dir)


# --- CLI ---
#
# The pr-pipeline:tick skill shells out to these subcommands instead of
# re-implementing the logic above in inline bash, so every tick makes the same
# decision deterministically. Each subcommand reads small inputs and writes a
# single JSON object (or atomic file) — never a diff or engine transcript.


def _load_json(path: str, default: object) -> object:
    """Load JSON from ``path``, returning ``default`` if missing or unparseable."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _read_file(path: str, default: str = "") -> str:
    """Read a file's raw text, returning ``default`` on failure."""
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return default


def _load_json_string(raw: str, default: object) -> object:
    """Parse a JSON string, returning ``default`` on parse failure."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _as_comment_list(loaded: object) -> list[object]:
    """Unwrap complete comments JSON without coercing missing data to zero.

    Accept a bare array or a ``{"comments": [...]}`` object. Entries must
    carry the author/body/edit provenance checked by trusted_comment_history. The
    iteration count is never silently zeroed by an unwrapped object.
    """
    if isinstance(loaded, list):
        return loaded
    if isinstance(loaded, dict):
        inner = loaded.get("comments")
        if isinstance(inner, list):
            return inner
    raise ValueError("comment history is not an array or comments object")


def _cmd_harvest_review(args: argparse.Namespace) -> int:
    review_raw = _read_file(args.review)
    comments_raw = _read_file(args.comments)
    decision = harvest_review(
        trusted_author=args.trusted_author,
        review=review_raw,
        comments=comments_raw,
        pinned_base=args.pinned_base,
        current_base=args.current_base,
        pinned_head=args.pinned_head,
        current_head=args.current_head,
        pre_label=args.pre_label,
        threshold=args.threshold,
        diff_base_sha=args.diff_base_sha,
        pinned_base_ref=args.pinned_base_ref,
        current_base_ref=args.current_base_ref,
        fix_round=args.fix_round,
        report_protocol_version=args.report_protocol_version,
        review_identity=args.review_identity,
        approved_input_reservations=args.approved_input_reservation,
    )
    print(json.dumps(decision))
    return 0


def _cmd_validate_review(args: argparse.Namespace) -> int:
    try:
        obj = review_reports.loads(_read_file(args.review))
    except ValueError:
        obj = None
    verdict = validate_review(obj, report_protocol_version=args.report_protocol_version)
    if verdict is None:
        print("INVALID")
        return 1
    print(verdict)
    return 0


def _cmd_classify_dispatch(args: argparse.Namespace) -> int:
    dispatch = _load_json(args.dispatch, {})
    is_dict = isinstance(dispatch, dict)
    raw_deadline = dispatch.get("deadline", 0) if is_dict else 0
    # A corrupt marker may carry a non-numeric deadline; coerce defensively so this
    # never crashes mid-tick (a crash here would leave classify_state empty and let a
    # malformed marker fall through reconciliation). valid_marker rejects such markers
    # upstream, but classify is also called for admission counting WITHOUT that gate —
    # so coerce to +inf, never 0.0: with +inf a live engine reads `live` (counts as an
    # in-flight slot), failing CLOSED on spend. Coercing to 0.0 would read `hung`,
    # freeing the slot and letting the cap admit an extra reviewer over a running one.
    if isinstance(raw_deadline, bool):
        deadline = float("inf")
    else:
        try:
            deadline = float(raw_deadline)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            deadline = float("inf")
    marker_state = str(dispatch.get("state", "")) if is_dict else ""
    state = classify_dispatch(
        now=args.now,
        deadline=deadline,
        pid_alive=bool(args.pid_alive),
        result_exists=bool(args.result_exists),
        state=marker_state,
        envelope_valid=bool(args.envelope_valid),
    )
    print(json.dumps({"state": state}))
    return 0


def _cmd_atomic_write(args: argparse.Namespace) -> int:
    atomic_write(args.path, sys.stdin.read())
    return 0


def _cmd_touches_review_policy(args: argparse.Namespace) -> int:
    paths = sys.stdin.read().splitlines()
    print(json.dumps({
        "touches_policy": touches_review_policy(paths, args.repo),
    }))
    return 0


def _cmd_validate_review_candidate(args: argparse.Namespace) -> int:
    try:
        metadata: object = json.load(sys.stdin)
    except json.JSONDecodeError:
        metadata = None
    trusted = validate_review_candidate(
        candidate=metadata,
        trusted_author=args.trusted_author,
        trusted_repo=args.trusted_repo,
    )
    print(json.dumps({"trusted": trusted}))
    return 0


def _cmd_harvest_implementation(args: argparse.Namespace) -> int:
    result_raw = _read_file(args.result)
    decision = harvest_implementation(
        result=result_raw,
        pre_label=args.pre_label,
    )
    print(json.dumps(decision))
    return 0


def _cmd_harvest_fix(args: argparse.Namespace) -> int:
    result_raw = _read_file(args.result)
    decision = harvest_fix(
        result=result_raw,
        pre_label=args.pre_label,
        pinned_head=args.pinned_head,
        current_head=args.current_head,
        body_revision=args.body_revision,
        dispatch_id=args.dispatch_id,
        review_sha256=args.review_sha256,
        approved_input_sha256=args.approved_input_sha256,
        report_protocol_version=args.report_protocol_version,
        review_handoff=_load_json(args.review_handoff, None) if args.review_handoff else None,
        verification_unconfigured=args.verification_unconfigured,
    )
    print(json.dumps(decision))
    return 0


def _cmd_validate_marker(args: argparse.Namespace) -> int:
    marker = _load_json(args.dispatch, None)
    valid = validate_marker(
        dispatch=marker, pr=args.pr, stage=args.stage,
    )
    print(json.dumps({"valid": valid}))
    return 0


def _cmd_validate_envelope(args: argparse.Namespace) -> int:
    envelope = _load_json(args.envelope, None)
    valid = validate_envelope(
        envelope=envelope,
        dispatch_id=args.dispatch_id,
        report_protocol_version=args.report_protocol_version,
    )
    print(json.dumps({"valid": valid}))
    return 0


def _cmd_verify_identity(args: argparse.Namespace) -> int:
    live_start: float | None = args.live_start if args.live_start >= 0 else None
    identity = verify_identity(
        recorded_start=args.recorded_start,
        live_start=live_start,
        tolerance=args.tolerance,
    )
    print(json.dumps({"identity": identity}))
    return 0


def _cmd_reconcile_dispatch(args: argparse.Namespace) -> int:
    marker = _load_json(args.dispatch, None)
    marker_valid = valid_marker(marker, expected_pr=args.pr, expected_stage=args.stage)
    pre_label = (
        marker.get("pre_label", LABEL_IMPLEMENTED)
        if isinstance(marker, dict)
        else LABEL_IMPLEMENTED
    )
    decision = reconcile_dispatch(
        current_label=args.current_label,
        marker_valid=marker_valid,
        classify_state=args.classify_state,
        pre_label=str(pre_label),
        in_flight_label=args.in_flight_label,
        dispatch_attempts=args.dispatch_attempts,
        attempt_threshold=args.attempt_threshold,
    )
    print(json.dumps(decision))
    return 0


def prune_tick_logs(log_dir: str, keep_n: int = 50) -> int:
    """Remove the oldest tick log files, keeping only the most recent ``keep_n``.

    Log files are identified by the ``tick-*.jsonl`` glob and sorted lexicographically
    (the timestamp-prefixed naming makes lexicographic order identical to chronological
    order). Files beyond the ``keep_n`` most recent are deleted.

    Args:
        log_dir: Directory containing tick log files.
        keep_n: Maximum number of log files to retain.

    Returns:
        The number of files deleted (0 if the directory does not exist).
    """
    if not os.path.isdir(log_dir):
        return 0
    files = sorted(glob.glob(os.path.join(log_dir, "tick-*.jsonl")))
    to_delete = files[: max(0, len(files) - keep_n)]
    for path in to_delete:
        try:
            os.unlink(path)
        except OSError:
            pass
    return len(to_delete)


def open_tick_log(log_dir: str, keep_n: int = 50) -> str:
    """Create a new tick log file, prune old ones, and return the file path.

    The file is named ``tick-<YYYYMMDDTHHMMSS>-<epoch>-<counter>.jsonl`` so that
    lexicographic sort equals chronological order while still keeping a distinct
    file per tick when two ticks start within the same second. The directory is
    created if it does not exist. Old log files are pruned to ``keep_n`` before
    the new file is created, so the directory holds at most ``keep_n + 1`` files
    while a tick is running.

    Args:
        log_dir: Directory where tick logs are written.
        keep_n: Number of existing log files to retain before adding the new one.

    Returns:
        Absolute path to the newly created (empty) log file.
    """
    os.makedirs(log_dir, exist_ok=True)
    prune_tick_logs(log_dir, keep_n=keep_n)
    now = time.time()
    ts = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S"
    )
    epoch = int(now)
    for counter in range(1000):
        path = os.path.join(log_dir, f"tick-{ts}-{epoch}-{counter:03d}.jsonl")
        try:
            with open(path, "x"):
                pass
        except FileExistsError:
            continue
        return os.path.abspath(path)
    raise FileExistsError("could not allocate a unique tick log file")


def append_tick_entry(log_path: str, entry: dict[str, object]) -> None:
    """Append one JSON-lines entry to a tick log file.

    Each call appends exactly one line (the serialised ``entry`` followed by a
    newline). The directory must already exist (see :func:`open_tick_log`).

    Args:
        log_path: Path to the ``*.jsonl`` log file.
        entry: Mapping to serialise as a single JSON line.
    """
    with open(log_path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def latest_tick_log_path(log_dir: str) -> str | None:
    """Return the path of the most recent tick log file, or ``None``.

    Tick log names sort lexicographically in chronological order (see
    :func:`open_tick_log`), so the newest file is the last one. Callers that
    need to append to the log a running tick already owns must use this
    instead of :func:`open_tick_log`, which would create a *newer* file and
    silently become the one :func:`read_latest_tick_log` reports.

    Args:
        log_dir: Directory containing ``tick-*.jsonl`` log files.

    Returns:
        Absolute-or-relative path to the newest log file, or ``None`` when the
        directory does not exist or holds no tick log files.
    """
    if not os.path.isdir(log_dir):
        return None
    files = sorted(glob.glob(os.path.join(log_dir, "tick-*.jsonl")))
    return files[-1] if files else None


def read_latest_tick_log(log_dir: str) -> list[dict[str, object]]:
    """Return parsed entries from the most recent tick log file.

    Lines that are not valid JSON objects are silently skipped so a partially
    written final line (e.g. after a crash) does not corrupt the summary.

    Args:
        log_dir: Directory containing ``tick-*.jsonl`` log files.

    Returns:
        List of parsed entry dicts from the newest log file, in file order.
        Returns an empty list when the directory does not exist or contains no
        tick log files.
    """
    latest = latest_tick_log_path(log_dir)
    if latest is None:
        return []
    entries: list[dict[str, object]] = []
    with open(latest) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
    return entries


# A scheduled tick may start this long after its advertised wake before the
# dashboard calls the evidence stale. The /loop harness clamps short wakes up
# to 60 seconds, so the grace must exceed that.
SCOPE_EVIDENCE_GRACE_SECONDS = 120
# A tick log that has started but not completed is "in progress" for this
# long; after that it is stale (the process likely died mid-tick).
SCOPE_TICK_IN_PROGRESS_SECONDS = 1800
_SCOPE_ACTIONS = frozenset({
    "only_scope_resolved", "only_scope_unreadable", "target_scope_resolved",
})
# Logged by every tick that holds the lock, before any fallible startup work,
# so a tick that fails before resolving its scope still names what it was
# asked to do. A resolved record later in the same log supersedes it.
_SCOPE_REQUEST_ACTION = "tick_scope_requested"
_LOOP_STOP_ACTIONS = frozenset({
    "loop_only_stop", "loop_target_stop", "loop_quiescent_stop",
})


def _scope_tokens(value: object) -> str | None:
    """Join logged scope tokens, or ``None`` when the record is malformed."""
    if not isinstance(value, list) or not value or not all(
        isinstance(token, str) and token for token in value
    ):
        return None
    return ",".join(value)


def _describe_scope(entries: list[dict[str, object]]) -> str:
    """Describe the dispatch scope one tick log records."""
    scope = next(
        (entry for entry in reversed(entries) if entry.get("action") in _SCOPE_ACTIONS),
        None,
    )
    if scope is None:
        return _describe_requested_scope(entries)
    details = scope.get("details")
    details = details if isinstance(details, dict) else {}
    action = scope.get("action")
    if action == "target_scope_resolved":
        target = details.get("target")
        items = _scope_tokens(details.get("scope"))
        if not isinstance(target, str) or items is None:
            return "positional target (scope record malformed)"
        return f"target {target} (scope {items})"
    listed = _scope_tokens(details.get("only"))
    if listed is None:
        return "--only (scope record malformed)"
    if action == "only_scope_unreadable":
        return f"--only {listed} (scope UNREADABLE at last tick; no engine started)"
    items = _scope_tokens(details.get("scope"))
    if items is None:
        return f"--only {listed} (scope record malformed)"
    return f"--only {listed} (dispatch scope {items})"


def _describe_requested_scope(entries: list[dict[str, object]]) -> str:
    """Describe a tick that logged no resolved scope record.

    Only an explicit request names the full queue. A list or target request
    without a resolved record means the tick stopped (or has not yet reached
    the point) before resolving its scope, so no engine started from it. A
    log with no request at all does not prove any scope.
    """
    request = next(
        (entry for entry in reversed(entries)
         if entry.get("action") == _SCOPE_REQUEST_ACTION),
        None,
    )
    if request is None:
        return "scope unknown (no scope record)"
    details = request.get("details")
    details = details if isinstance(details, dict) else {}
    mode = details.get("mode")
    unresolved = "(requested; scope not resolved; no engine started)"
    if mode == "full":
        return "full queue"
    if mode == "target" and isinstance(details.get("target"), str) and details["target"]:
        return f"target {details['target']} {unresolved}"
    if mode == "only":
        listed = _scope_tokens(details.get("only"))
        if listed is not None:
            return f"--only {listed} {unresolved}"
    return "scope record malformed"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def dispatch_scope_report(log_dir: str, *, now: float) -> str:
    """Describe the newest tick's dispatch scope and how current that evidence is.

    This reads only retained tick logs. The separate loop-process classifier
    reports local identity evidence. The newest log holding a tick (a
    ``tick_complete`` summary, a ``tick_scope_requested`` record, or a resolved
    scope record) names the
    scope: an ``--only`` list with its expanded items, an unreadable list, a
    positional target, or the full queue. A list or target that was requested
    but never resolved (startup, target resolution, or harvest failed first)
    is named as requested and unresolved. A log with no scope record names no
    scope; missing evidence is never read as the full queue. Logs holding
    only a ``lock_held`` refusal or a loop-stop notice are not ticks; a stop
    notice newer than the tick marks the run as ended.

    Evidence states:

    * ``active`` — the tick completed and its advertised next wake (plus
      :data:`SCOPE_EVIDENCE_GRACE_SECONDS`) has not passed, or a tick started
      within :data:`SCOPE_TICK_IN_PROGRESS_SECONDS` and has not completed.
    * ``ended`` — a loop-stop notice followed the tick, or its cadence
      reported ``stop=true``.
    * ``STALE`` — the next wake is overdue with no newer tick, the tick never
      completed, or it reported no cadence.

    Args:
        log_dir: Directory holding ``tick-*.jsonl`` files.
        now: Current epoch seconds.

    Returns:
        One banner line.
    """
    if not os.path.isdir(log_dir):
        return "no tick evidence"
    stop: dict[str, object] | None = None
    for path in sorted(glob.glob(os.path.join(log_dir, "tick-*.jsonl")), reverse=True):
        entries: list[dict[str, object]] = []
        try:
            with open(path) as handle:
                for line in handle:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        entries.append(obj)
        except OSError:
            continue
        actions = {entry.get("action") for entry in entries}
        if (
            "tick_complete" not in actions
            and not actions & _SCOPE_ACTIONS
            and _SCOPE_REQUEST_ACTION not in actions
        ):
            if stop is None:
                stop = next(
                    (e for e in reversed(entries) if e.get("action") in _LOOP_STOP_ACTIONS),
                    None,
                )
            continue
        scope = _describe_scope(entries)
        if stop is not None:
            reason = stop.get("reason")
            return f"{scope} · log evidence ended: {reason if isinstance(reason, str) else stop.get('action')}"
        summary = next(
            (e for e in reversed(entries) if e.get("action") == "tick_complete"), None,
        )
        timestamps = [
            float(e["ts"]) for e in entries
            if isinstance(e.get("ts"), (int, float)) and not isinstance(e.get("ts"), bool)
        ]
        if summary is None:
            started = min(timestamps) if timestamps else 0.0
            if now - started <= SCOPE_TICK_IN_PROGRESS_SECONDS:
                return f"{scope} · log evidence active: tick in progress since {_clock(started)}"
            return f"{scope} · log evidence STALE: tick started {_clock(started)} never completed"
        details = summary.get("details")
        cadence = details.get("cadence") if isinstance(details, dict) else None
        ts = summary.get("ts")
        if (
            not isinstance(cadence, dict)
            or not isinstance(ts, (int, float))
            or not isinstance(cadence.get("next_wake_seconds"), int)
        ):
            return f"{scope} · log evidence STALE: last tick reported no cadence"
        if cadence.get("stop") is True:
            return f"{scope} · log evidence ended: {cadence.get('reason')}"
        due = float(ts) + cadence["next_wake_seconds"]
        if now <= due + SCOPE_EVIDENCE_GRACE_SECONDS:
            return (
                f"{scope} · log evidence active: last tick {_clock(ts)}, "
                f"next due by {_clock(due)}"
            )
        return (
            f"{scope} · log evidence STALE: last tick {_clock(ts)}, next was due "
            f"{_clock(due)}; no newer tick"
        )
    return "no tick evidence"


def _cmd_dispatch_scope(args: argparse.Namespace) -> int:
    print(dispatch_scope_report(args.log_dir, now=time.time()))
    return 0


def _cmd_tick_log_open(args: argparse.Namespace) -> int:
    path = tick_log_open(log_dir=args.log_dir, keep=args.keep)
    print(path)
    return 0


def _cmd_tick_log_write(args: argparse.Namespace) -> int:
    try:
        entry_obj: object = json.loads(args.entry)
    except json.JSONDecodeError as exc:
        print(f"tick-log-write: invalid JSON entry: {exc}", file=sys.stderr)
        return 1
    if not isinstance(entry_obj, dict):
        print("tick-log-write: entry must be a JSON object", file=sys.stderr)
        return 1
    tick_log_write(log_path=args.log_path, entry=entry_obj)
    return 0


def _cmd_tick_log_tail(args: argparse.Namespace) -> int:
    entries = tick_log_tail(log_dir=args.log_dir)
    print(json.dumps(entries))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the pr-pipeline:tick skill.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success).
    """
    parser = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_harvest = sub.add_parser("harvest-review", help="Decide a finished review's outcome")
    p_harvest.add_argument("--trusted-author", required=True, help="Validated operator posting login")
    p_harvest.add_argument("--review", required=True, help="Path to review.json")
    p_harvest.add_argument("--comments", required=True, help="Path to complete normalized comments JSON")
    p_harvest.add_argument(
        "--approved-input-reservation", action="append", default=[],
        help="Controller-owned source fingerprint from local receipts (repeat for each source)",
    )
    p_harvest.add_argument(
        "--pinned-base", required=True,
        help="base tip snapshot from dispatch.json (base_tip_sha; freshness anchor)",
    )
    p_harvest.add_argument("--current-base", required=True, help="base ref tip resolved now")
    p_harvest.add_argument(
        "--diff-base-sha", default="",
        help="merge-base from dispatch.json (base_sha; the reviewer's diff anchor)",
    )
    p_harvest.add_argument(
        "--pinned-head", default="", help="head_sha from dispatch.json (reviewed commit)"
    )
    p_harvest.add_argument(
        "--current-head", default="", help="PR head resolved now, at harvest"
    )
    p_harvest.add_argument(
        "--pre-label", default=LABEL_IMPLEMENTED, help="pre-dispatch label to revert to on drift"
    )
    p_harvest.add_argument(
        "--pinned-base-ref", default="",
        help="base branch name recorded at dispatch (empty disables the retarget check)",
    )
    p_harvest.add_argument(
        "--current-base-ref", default="",
        help="PR's live base branch name at harvest (empty disables the retarget check)",
    )
    p_harvest.add_argument("--threshold", type=int, default=MAX_ITERATIONS)
    p_harvest.add_argument("--fix-round", default="", help="Pinned no-change fixer round")
    p_harvest.add_argument("--report-protocol-version", type=int)
    p_harvest.add_argument("--review-identity", default="", help="Pinned reviewer dispatch identity")
    p_harvest.set_defaults(func=_cmd_harvest_review)

    p_validate = sub.add_parser("validate-review", help="Validate a review.json against the schema")
    p_validate.add_argument("--review", required=True, help="Path to review.json")
    p_validate.add_argument("--report-protocol-version", type=int)
    p_validate.set_defaults(func=_cmd_validate_review)

    p_classify = sub.add_parser("classify-dispatch", help="Classify a dispatch's liveness")
    p_classify.add_argument("--dispatch", required=True, help="Path to dispatch.json")
    p_classify.add_argument("--now", type=float, required=True, help="Current epoch seconds")
    p_classify.add_argument("--pid-alive", type=int, required=True, help="1 if PID alive else 0")
    p_classify.add_argument(
        "--result-exists", type=int, required=True, help="1 if result file present else 0"
    )
    p_classify.add_argument(
        "--envelope-valid", type=int, default=0, help="1 if completion envelope is valid else 0"
    )
    p_classify.set_defaults(func=_cmd_classify_dispatch)

    p_write = sub.add_parser("atomic-write", help="Atomically write stdin to a file")
    p_write.add_argument("--path", required=True, help="Destination path")
    p_write.set_defaults(func=_cmd_atomic_write)

    p_policy = sub.add_parser(
        "touches-review-policy",
        help="Report whether stdin paths include a reviewer-governing file",
    )
    p_policy.add_argument(
        "--repo", default="",
        help="Resolved target as owner/name, for its config layer",
    )
    p_policy.set_defaults(func=_cmd_touches_review_policy)

    p_candidate = sub.add_parser(
        "validate-review-candidate",
        help="Report whether PR metadata satisfies the trusted reviewer boundary",
    )
    p_candidate.add_argument("--trusted-author", required=True, help="Allowed author login")
    p_candidate.add_argument("--trusted-repo", required=True, help="Allowed owner/repo head")
    p_candidate.set_defaults(func=_cmd_validate_review_candidate)

    p_validate_marker = sub.add_parser(
        "validate-marker",
        help="Report whether a dispatch marker is structurally valid for this PR/stage",
    )
    p_validate_marker.add_argument("--dispatch", required=True, help="Path to dispatch.json")
    p_validate_marker.add_argument(
        "--pr", type=int, default=None, help="PR number the marker must belong to"
    )
    p_validate_marker.add_argument(
        "--stage", default="review", help="Pipeline stage the marker must record"
    )
    p_validate_marker.set_defaults(func=_cmd_validate_marker)

    p_harvest_impl = sub.add_parser(
        "harvest-implementation", help="Decide a finished implementer's outcome"
    )
    p_harvest_impl.add_argument("--result", required=True, help="Path to result.json")
    p_harvest_impl.add_argument(
        "--pre-label",
        default=LABEL_AVAILABLE,
        help="pre-dispatch label (ai:available)",
    )
    p_harvest_impl.set_defaults(func=_cmd_harvest_implementation)

    p_harvest_fix = sub.add_parser(
        "harvest-fix", help="Decide a finished fixer's outcome"
    )
    p_harvest_fix.add_argument("--result", required=True, help="Path to result.json")
    p_harvest_fix.add_argument("--dispatch-id", default="", help="Fixer dispatch identity")
    p_harvest_fix.add_argument("--report-protocol-version", type=int)
    p_harvest_fix.add_argument("--review-handoff", help="Path to the dispatch-pinned review_handoff JSON")
    p_harvest_fix.add_argument("--verification-unconfigured", action="store_true")
    p_harvest_fix.add_argument(
        "--approved-input-sha256", default="", help="Approved source identity from dispatch.json",
    )
    p_harvest_fix.add_argument(
        "--review-sha256", default="", help="Original review fingerprint from dispatch.json",
    )
    p_harvest_fix.add_argument(
        "--pre-label",
        default=LABEL_CHANGES_REQUESTED,
        help="pre-dispatch label (ai:changes-requested)",
    )
    p_harvest_fix.add_argument(
        "--pinned-head", default="",
        help="head_sha from dispatch.json (empty disables the push check)",
    )
    p_harvest_fix.add_argument(
        "--current-head", default="",
        help="PR's live head SHA at harvest (empty disables the push check)",
    )
    p_harvest_fix.add_argument(
        "--body-revision", default="",
        choices=("", *_BODY_REVISION_INPUTS),
        help=(
            "state of the fixer's replacement PR body (empty: none offered). "
            "Decides the comment's PR-body line only; applying the body is the "
            "tick's job, never this subcommand's"
        ),
    )
    p_harvest_fix.set_defaults(func=_cmd_harvest_fix)

    p_reconcile = sub.add_parser(
        "reconcile-dispatch", help="Decide HARVEST's action for one in-flight review"
    )
    p_reconcile.add_argument("--dispatch", required=True, help="Path to dispatch.json")
    p_reconcile.add_argument(
        "--pr", type=int, default=None, help="PR number the marker must belong to"
    )
    p_reconcile.add_argument(
        "--stage", default="review", help="Pipeline stage the marker must record"
    )
    p_reconcile.add_argument(
        "--current-label", required=True, help="The PR's current pipeline label"
    )
    p_reconcile.add_argument(
        "--in-flight-label",
        default=LABEL_REVIEWING,
        help="in-flight label for this stage (ai:reviewing or ai:implementing)",
    )
    p_reconcile.add_argument(
        "--classify-state",
        default="",
        help="classify-dispatch state, or empty when no usable marker",
    )
    p_reconcile.add_argument(
        "--dispatch-attempts",
        type=int,
        default=0,
        help="prior resultless dispatch failures for this PR (durable counter)",
    )
    p_reconcile.add_argument(
        "--attempt-threshold",
        type=int,
        default=MAX_DISPATCH_ATTEMPTS,
        help="cap on resultless dispatch attempts before escalating",
    )
    p_reconcile.set_defaults(func=_cmd_reconcile_dispatch)

    p_envelope = sub.add_parser(
        "validate-envelope",
        help="Report whether a completion envelope is valid and dispatch-id-bound",
    )
    p_envelope.add_argument("--envelope", required=True, help="Path to completion.json")
    p_envelope.add_argument("--report-protocol-version", type=int)
    p_envelope.add_argument(
        "--dispatch-id", default=None, help="Expected dispatch_id from the marker"
    )
    p_envelope.set_defaults(func=_cmd_validate_envelope)

    p_identity = sub.add_parser(
        "verify-identity",
        help="Report whether a live process matches a recorded start time",
    )
    p_identity.add_argument(
        "--recorded-start", type=float, required=True, help="Start time from marker"
    )
    p_identity.add_argument(
        "--live-start", type=float, required=True,
        help="Live process start time (-1 if not readable)",
    )
    p_identity.add_argument(
        "--tolerance", type=float, default=1.0, help="Allowable clock skew in seconds"
    )
    p_identity.set_defaults(func=_cmd_verify_identity)

    p_tlog_open = sub.add_parser(
        "tick-log-open",
        help="Create a new tick log file, prune old ones, print its path",
    )
    p_tlog_open.add_argument("--log-dir", required=True, help="Directory for tick logs")
    p_tlog_open.add_argument(
        "--keep", type=int, default=50, help="Max existing log files to retain (default 50)"
    )
    p_tlog_open.set_defaults(func=_cmd_tick_log_open)

    p_tlog_write = sub.add_parser(
        "tick-log-write",
        help="Append one JSON entry to a tick log file",
    )
    p_tlog_write.add_argument("--log-path", required=True, help="Path to the .jsonl log file")
    p_tlog_write.add_argument("--entry", required=True, help="JSON-encoded entry object")
    p_tlog_write.set_defaults(func=_cmd_tick_log_write)

    p_tlog_tail = sub.add_parser(
        "tick-log-tail",
        help="Print all entries from the latest tick log as a JSON array",
    )
    p_tlog_tail.add_argument("--log-dir", required=True, help="Directory for tick logs")
    p_tlog_tail.set_defaults(func=_cmd_tick_log_tail)

    p_scope = sub.add_parser(
        "dispatch-scope",
        help="Print the newest tick's dispatch scope and evidence state",
    )
    p_scope.add_argument("--log-dir", required=True, help="Directory for tick logs")
    p_scope.set_defaults(func=_cmd_dispatch_scope)

    args = parser.parse_args(argv)
    func: object = args.func
    assert callable(func)
    return int(func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
