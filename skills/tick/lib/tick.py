"""PR-pipeline tick orchestrator.

Imported by the ``pr-pipeline:tick`` skill's thinned SKILL.md. A single
``tick.sh`` call replaces 25 bash ``Bash`` tool invocations with one long-lived
Python process that imports ``pipeline`` and ``process_control`` directly — no
subprocess round-trips for decision logic.

Stdlib only: the directory installs against a target repo it does not own, so
it takes no dependency on any target's toolchain and needs nothing but an
interpreter at or above the 3.11 floor. ``tick.sh`` resolves one; the guard
below is what an operator sees if something else did not.
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
        "Run the tick through "
        "`bash $PR_PIPELINE_HOME/skills/tick/tick.sh`, which "
        "resolves one, or export PR_PIPELINE_PYTHON with the path to a "
        ">= 3.11 interpreter."
    )

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
import traceback
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import claude_stream_wrapper
import discussion_context
import engine_usage
import labels
import native_skills
import pipeline
import process_control
import reviewer_observability
from pipeline import resolve_target

# --- Repo anchoring (must precede the constants that derive from it) ---


def git(*args: str) -> str:
    """Run a ``git`` command and return stripped stdout.

    Args:
        *args: Arguments to pass to ``git``.

    Returns:
        The stripped stdout text.

    Raises:
        subprocess.CalledProcessError: On non-zero exit.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.strip()


def repo_root() -> str:
    """Return the absolute path of the *main* checkout's working tree.

    ``git rev-parse --show-toplevel`` answers "which tree am I in", which is
    the linked worktree when the tick is invoked from one. The control plane
    must always operate on the main checkout, so anchor on
    ``--git-common-dir`` — it names the main checkout's ``.git`` from every
    worktree — and take its parent.

    Returns:
        The absolute path to the main checkout's root directory.

    Raises:
        subprocess.CalledProcessError: When the cwd is not in a git
            repository.
        ValueError: When git reports an empty common dir.
    """
    common_dir = git("rev-parse", "--git-common-dir")
    if not common_dir:
        raise ValueError("git reported an empty --git-common-dir")
    # git reports the common dir relative to the cwd inside the main
    # checkout and absolutely from a linked worktree; abspath covers both.
    return os.path.dirname(os.path.abspath(common_dir))


def _resolve_wt_root() -> str:
    """Return the absolute marker root, anchored to the git common dir.

    Markers are the pipeline's local liveness accelerator, and every worktree
    on one machine must see the same marker space. A CWD-relative root
    resolves to ``<worktree>/.claude/worktrees`` when the tick is invoked
    from inside a linked worktree — an empty directory that makes every
    in-flight item in the repo look markerless. ``git rev-parse
    --git-common-dir`` returns the *main* checkout's ``.git`` from any
    worktree, so its parent is the one stable anchor.

    Anchoring is machine-local hygiene only. It gives every worktree on this
    machine one marker space; it establishes nothing about custody on other
    machines, whose markers this tick can never see.

    Returns:
        The absolute path to ``.claude/worktrees`` under the main checkout,
        falling back to the CWD-relative path resolved absolutely when the
        CWD is not inside a git repository.
    """
    try:
        root = repo_root()
    except (subprocess.CalledProcessError, ValueError, OSError):
        return os.path.abspath(os.path.join(".claude", "worktrees"))
    return os.path.join(root, ".claude", "worktrees")


# --- Constants (mirrors the bash Constants block) ---

# The marker space every path below derives from. Never bind it as a function
# default (``wt_root: str = WT_ROOT``): a default is evaluated once at import,
# so the binding keeps pointing at the operator's real marker root even after
# a test redirects this global — and the terminal harvest paths that would then
# run against it kill process groups, archive evidence, and remove scratch.
# Take ``wt_root: str | None = None`` and resolve it in the body instead.
WT_ROOT = _resolve_wt_root()
TICK_LOG_DIR = os.path.join(WT_ROOT, ".tick-log")
TICK_LOCK_PATH = os.path.join(WT_ROOT, ".tick.lock")
# An unhandled exception escaped the tick body; ``tick_crash`` has the
# traceback. The SOP stops on it, so ``--loop`` must not retry it either.
TICK_CRASH_EXIT_CODE = 4
LOCK_HELD_EXIT_CODE = 5
# The resolved target has no [repos."owner/name"] section: setup never ran for
# it. Distinct from 3 because the remedy is an interactive step, not a fix to a
# value the operator already wrote.
UNCONFIGURED_TARGET_EXIT_CODE = 6
# Stage deadlines, in seconds. These and the stall, cooldown and cadence values
# below are the shipped defaults from `pipeline`. The tick runs on the values
# its startup gate validated (ResolvedConfig) and passes them to every
# consumer; these names serve direct callers and tests.
REVIEW_TIMEOUT = pipeline.REVIEW_TIMEOUT_SECONDS
# Per-scope quiescent streaks survive worktree cleanup. The old shared
# quiescent-counter.txt has no known scope, so it is deliberately not migrated.
_QUIESCENT_COUNTER_FILE = os.path.join(
    WT_ROOT, ".tick-state", "quiescent-counters", "queue.txt",
)
# Edge-triggered wake channel for in-process --loop mode. The watcher scan and
# the wake-request file are advisory nudges only: they end the inter-tick sleep
# early, and the tick that follows re-reads full state as always.
WAKE_WATCH_POLL_SECONDS = 5
# Keep handles for children this tick process launched. Dropping a Popen leaves
# CPython to reap it only at a later spawn, which an idle loop may not do until
# its cadence timer fires. This registry owns wait status, never marker custody.
_ENGINE_CHILDREN: list[subprocess.Popen[bytes]] = []
_ENGINE_CHILDREN_LOCK = threading.Lock()
# Minimum spacing between a tick and a nudge-initiated successor, bounding the
# tick rate a nudge storm (buggy toucher, flapping marker) can force.
LOOP_NUDGE_FLOOR_SECONDS = 10
# External nudge interface: any transport (operator touch, webhook forwarder,
# stall detector) creates this file; the watcher consumes it by unlink.
WAKE_REQUEST_PATH = os.path.join(WT_ROOT, ".tick-state", "wake-request")
# The longest silence a *healthy* engine can produce, which is what the stall
# threshold has to clear. Engines run `claude -p --output-format stream-json`
# without `--include-partial-messages` (see `_launch_claude_engine`), so events
# land only at message boundaries: nothing is emitted while a tool call runs,
# so heartbeat silence is *at least* tool-call duration — the model turn that
# consumes the result and any internal 429/529 backoff land on top of it, which
# is part of why the threshold keeps a multiple rather than sitting at this
# bound. Claude Code caps a single Bash call at 600s, and the operator's
# verification commands are allowlisted (`_engine_verify_tools`) against suites
# that routinely outrun the harness's 120s default — so an engine that retries
# with a longer timeout, the obvious move, legitimately goes quiet for the full
# cap. Fixed, not configurable; the resolver refuses a configured stall
# threshold at or below it.
MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS = pipeline.MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS
# Progress-stall threshold. An implementer/fixer that has emitted no engine
# output for this long is wedged rather than working — a network call that
# never returns, a permission wait, a silent retry loop. Set at 3x the bound
# above rather than at it: a threshold equal to the longest legal tool call
# reports a healthy engine, and a stall signal the operator learns to distrust
# is worse than no stall signal. Still well below the 60-min deadline, so a
# real stall surfaces with roughly half the engine's budget left to act on.
# Operator-configurable as `engine_stall_seconds`; dispatch records the value
# in the marker, and stall detection reads it from there.
ENGINE_STALL_SECONDS = pipeline.ENGINE_STALL_SECONDS
# Extra allowance when no heartbeat file exists yet. Two causes: an engine that
# has not reached its first output event, and a marker dispatched by a wrapper
# from before heartbeats existed. Neither is a stall, so the fallback clock
# (marker start_time) gets this on top of the threshold before classifying.
ENGINE_STALL_STARTUP_GRACE_SECONDS = pipeline.ENGINE_STALL_STARTUP_GRACE_SECONDS
# Stall episodes already reported, keyed by dispatch identity. Persisted for
# the same reason `_QUIESCENT_COUNTER_FILE` is: the two executable loop paths
# are separate processes. One-shot ticks scheduled from the `tick-cadence:`
# line are the operator's prescribed mode, so in-process memory would re-report
# a single wedge on every tick until its deadline. Lives beside the marker
# space rather than in it, so no engine worktree gains an untracked file.
_STALL_EPISODES_FILE = os.path.join(WT_ROOT, ".tick-state", "stall-episodes.json")
# Serializes the read-modify-write above. Under `--loop` the watcher thread and
# the tick body's own scan both reach it, and two callers reading the same map
# would both pass the already-evaluated gate and both report — the duplicate
# the episode memory exists to prevent. Separate from the file so the lock is
# never held by whatever is reading or replacing it.
_STALL_EPISODES_LOCK = os.path.join(WT_ROOT, ".tick-state", "stall-episodes.lock")
# Cap on the best-effort operator notification. `display notification` returns
# in milliseconds; this exists only so a wedged notifier cannot park its
# caller — which, since stall reporting, includes the wake-watcher thread.
OPERATOR_NOTIFICATION_TIMEOUT_SECONDS = 10
IMPLEMENTER_TIMEOUT = pipeline.IMPLEMENTER_TIMEOUT_SECONDS
FIXER_TIMEOUT = pipeline.FIXER_TIMEOUT_SECONDS
DEFAULT_TRANSIENT_COOLDOWN_SECONDS = pipeline.TRANSIENT_COOLDOWN_SECONDS
TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS = (
    pipeline.TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS
)
MIN_CODEX_VERSION = "codex-cli 0.142.5"

# Target and operator identity are bound at startup, before any GitHub access.
# The login remains fixed through the tick and its following loop observation;
# a config edit is adopted at the next successful startup, never mid-tick.
# Imports and read-only CLI helpers do not require a configured Git remote.
_IMPORT_CONFIG = pipeline.resolve_config()
# The shipped tuning, for direct callers of the tick body that pass no
# validated configuration. A tick never runs on it: _run_single_tick_locked
# always passes the configuration its startup gate validated.
_SHIPPED_CONFIG = pipeline.ResolvedConfig(
    login="", allow_touching=False, checklist_paths=(), verify_tools=(),
    base_ref="", permission_mode="auto", scan_repos=None, errors=(), warnings=(),
)
TRUSTED_HEAD_REPO = ""
TARGET_REMOTE = ""
TRUSTED_REVIEW_AUTHOR = _IMPORT_CONFIG.login
_PR_BODY_SOURCE_FILE = ".pr-body.md"
_PR_CREATE_BODY_FILE = ".pr-create-body.md"
# The fixer's optional replacement PR body, and the two-line envelope that binds
# it to the run it describes.
#
# The first line binds the file to the *dispatch*. The fixer worktree is a
# detached checkout of the PR head, so a file left behind by an earlier run —
# committed by a stray `git add -A`, or riding in the branch already — would
# otherwise read as this run's revision and could overwrite a body a human
# edited in between. The stamp is the same dispatch-binding `completion.json`
# uses, and dispatch substitutes the exact line into the prompt so the engine
# copies it rather than derives it.
#
# The second line binds it to the *commit*. A dispatch id alone cannot tell an
# engine's final head from an intermediate one: the prompt has the engine write
# the file after its last push, and nothing but the OID it pushed proves it
# actually did. The harvest requires that OID to equal the head it point-reads,
# so a body describing a commit the PR no longer sits on is never published.
_PR_BODY_REVISION_FILE = ".pr-body-revision.md"
_PR_BODY_REVISION_STAMP = "pr-body-revision:"
_PR_BODY_REVISION_HEAD_STAMP = "pr-body-revision-head:"
# GitHub rejects a PR body past this length. Bounding here turns a runaway
# replacement into one published "left unchanged" reason instead of three failed
# `gh pr edit` attempts and an escalation to a human.
#
# Whether GitHub counts characters or UTF-8 bytes is not documented, so the
# limit is applied to *both* measures rather than guessed at. Picking wrong in
# one direction publishes a body GitHub refuses (three failed edits, custody
# retained, escalation); picking wrong in the other refuses a body GitHub would
# have taken. Enforcing the stricter reading costs only the second case, which
# is one published reason on a body no English-prose PR in this repo comes near.
_PR_BODY_MAX_CHARS = 65536
# How much of the revision file the classifier may pull into the tick process.
# The bound has to be applied to the *read*, not to what the read returned: a
# helper that consumes the file first and measures second can be exhausted by
# the runaway file it exists to reject. The headroom covers the two envelope
# lines, so a candidate at exactly the GitHub limit still classifies as valid
# rather than as oversized.
_PR_BODY_REVISION_READ_LIMIT = _PR_BODY_MAX_CHARS + 256
# GitHub's closing keywords and the issue references they bind, as they appear
# in a PR body. Merging closes every issue matched here, anywhere in the body —
# which is why a replacement body is held to the complete set and not only to
# the `Closes #N` under `## Summary`. Cross-repo and URL forms are included so a
# revision cannot smuggle a closure past the check by spelling it differently.
_CLOSING_DIRECTIVE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s+"
    r"(#\d+|[\w.-]+/[\w.-]+#\d+|https?://\S+?/issues/\d+)",
    re.IGNORECASE,
)
_PR_BODY_REQUIRED_SECTIONS = (
    "## Summary",
    "## Why this approach",
    "## What changed",
    "## Test coverage",
    "## Test plan",
)
_PR_LAYOUT_NOTICE_PREFIX = (
    "> **Metadata/layout notice:** Authored report is missing template sections: "
)
_PR_LAYOUT_NOTICE_SUFFIX = (
    ". The authored content is preserved; this notice does not assess verification evidence."
)
_IMPLEMENTATION_EVIDENCE_WARNING = (
    "> **Implementation evidence missing:** `Test coverage` lacks the required "
    "definition-of-done evidence or an explicit `Definition of done: n/a` "
    "Scope exemption with a reason. Recover the author's evidence before approval. "
    "This notice checks presence only; it does not validate proof or approve the change."
)
_IMPLEMENTATION_EVIDENCE_FIELDS = frozenset({
    "phase a", "proved at", "entry point", "boundary swept",
    "disproof (static)", "disproof (execution)", "scope rechecked",
})
_TERMINAL_LABELS = frozenset({
    pipeline.LABEL_READY_FOR_HUMAN,
    pipeline.LABEL_NEEDS_HUMAN,
    pipeline.LABEL_MAX_ITERATIONS,
})

_PROMPTS_DIR = str(pipeline.PROMPTS_DIR)

# The bound-skill vocabulary lives in `pipeline` because the reviewer policy
# gate derives its own entries from it (see `_POLICY_PATH_SUFFIXES`): one
# definition, so a doc the install supplies to an engine cannot be bound here
# and left outside the gate there.
REVISION_HYGIENE_SKILL = pipeline.REVISION_HYGIENE_SKILL
DEFINITION_OF_DONE_SKILL = pipeline.DEFINITION_OF_DONE_SKILL

# The skills each engine stage resolves before launch, against the same
# resolver; `pipeline.BOUND_SKILLS` is the union every stage's tuple draws from.
FIXER_SKILLS: tuple[str, ...] = (REVISION_HYGIENE_SKILL, DEFINITION_OF_DONE_SKILL)
IMPLEMENTER_SKILLS: tuple[str, ...] = (DEFINITION_OF_DONE_SKILL,)

# Ceiling on each governing document injected into a fixer prompt (the PR body,
# and the linked issue contract as a whole). The documents are engine- and
# human-authored with no size contract of their own, so the prompt states one.
# Generous against the largest pair observed in practice (a body at
# 12989 characters, its issue contract at 10414), and a cut is announced in the
# text rather than silently swallowing a requirement.
FIXER_CONTEXT_BUDGET = 20000

_RESET_HINT_RE = re.compile(
    r"\bresets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)"
    r"(?:\s+([A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)?))?",
    re.IGNORECASE,
)
_RAW_DEEPSEEK_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,}")


class TickOutcome(NamedTuple):
    """Exit status and the exact cadence already reported by one tick.

    Startup refusal and crashes have no cadence. A completed body supplies
    its validated decision even on a recoverable failure (which stays warm).
    """

    exit_code: int
    cadence: pipeline.CadenceDecision | None = None
    held_items: tuple[str, ...] = ()


class LoopState(TypedDict):
    """Observed work state that drives loop cadence."""

    in_flight_count: int
    nearest_engine_deadline: float | None
    actionable_queue: bool
    at_cap: bool
    quiescent: bool
    held_items: tuple[str, ...]
    target_state: pipeline.TargetCadenceState | None


class StageTargets(TypedDict):
    """Per-stage scope derived from one optional CLI target."""

    reviewer: tuple[str, int] | None
    implementer: tuple[str, int] | None
    fixer: tuple[str, int] | None


class TickLock:
    """Non-blocking file lock for serializing tick invocations."""

    def __init__(self, path: str) -> None:
        """Bind the lock to a filesystem path.

        Args:
            path: Lock file path.
        """
        self.path = path
        self._fh: object | None = None

    def acquire(self) -> bool:
        """Acquire the lock without blocking.

        Returns:
            ``True`` when acquired, ``False`` when another process holds it.
        """
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        """Release the lock if held."""
        if self._fh is None:
            return
        fh = self._fh
        self._fh = None
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def __enter__(self) -> "TickLock":
        """Acquire lock for context-manager use."""
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Release lock for context-manager use."""
        self.release()


def load_prompt_template(
    name: str,
    variables: dict[str, object],
    *,
    prompts_dir: str | None = None,
) -> str:
    """Load a prompt template file and substitute variables.

    Args:
        name: Template name (without .txt extension).
        variables: Dict of placeholder names to values.
        prompts_dir: Override the default prompts directory (for testing).

    Returns:
        The rendered prompt string.

    Raises:
        FileNotFoundError: If the template file does not exist.
        KeyError: If a template placeholder has no matching variable.
    """
    directory = prompts_dir if prompts_dir is not None else _PROMPTS_DIR
    path = os.path.join(directory, f"{name}.txt")
    with open(path) as fh:
        template = fh.read()
    if "{pr_body_contract}" in template:
        with open(os.path.join(directory, "pr-body-contract.md"), encoding="utf-8") as fh:
            variables = {**variables, "pr_body_contract": fh.read()}
    return template.format_map({k: str(v) for k, v in variables.items()})


def skill_doc_path(name: str) -> str:
    """Return the absolute path of one installed skill's ``SKILL.md``.

    Thin alias for :func:`pipeline.skill_doc_path`, kept here because the
    binding is a dispatch concern and every call site is in this module.

    Args:
        name: The skill directory name (e.g. ``"review-response-hygiene"``).

    Returns:
        The absolute path inside the install.
    """
    return pipeline.skill_doc_path(name)


def unresolved_skill_docs(names: Sequence[str]) -> list[str]:
    """Return the skills whose installed doc is missing or unreadable.

    The docs ship with the pipeline, so the probe reads the install rather than
    the engine's worktree: an engine's cwd is a checkout (a PR head for the
    fixer, the base tip for the implementer) of a repository the pipeline does
    not own, which has no reason to carry a copy of the operator's guidance.
    What this catches is therefore a packaging defect — a doc bound by a prompt
    and absent from the install — rather than anything about the item being
    dispatched.

    A doc resolves only when it reads as non-blank UTF-8 text. That is the same
    bar the native loaders apply at launch (``native_skills.claude_plugin`` and
    ``native_skills.prepare_reviewer``), so a blank or undecodable doc is
    caught here rather than at a later per-item setup step.

    Args:
        names: Skill directory names to resolve.

    Returns:
        The subset of ``names`` with no readable, non-blank ``SKILL.md``, in
        input order. Empty means every doc resolved.
    """
    unresolved: list[str] = []
    for name in names:
        try:
            text = Path(skill_doc_path(name)).read_text(encoding="utf-8")
        except (OSError, ValueError):
            text = ""
        if not text.strip():
            unresolved.append(name)
    return unresolved


# The installed docs each DISPATCH stage binds, with the stage-target key and
# the entity kind that stage dispatches. The reviewer loads its whole catalog
# at launch, so its set is the catalog rather than a prompt binding.
_DISPATCH_STAGE_GUIDANCE: tuple[
    tuple[str, Literal["reviewer", "implementer", "fixer"], str, tuple[str, ...]], ...
] = (
    ("review", "reviewer", "pr", tuple(native_skills.REVIEW_SKILLS)),
    ("implement", "implementer", "issue", IMPLEMENTER_SKILLS),
    ("fix", "fixer", "pr", FIXER_SKILLS),
)


def _guidance_blocked_stages(
    stage_targets: StageTargets,
    tick_log: TickLog | None,
) -> frozenset[str]:
    """Check the installed guidance once for every stage this tick may dispatch.

    An unreadable doc is a defect in the install, shared by every item of the
    stage that binds it. The per-item guard (:func:`_bound_skills_resolve`)
    escalates the one item it is dispatching, so on its own a single
    packaging defect moves the eligible backlog to ``ai:needs-human-decision``
    one batch per tick. This check runs before any worktree, lease, label
    change or engine launch, and holds only the stages whose docs failed. The
    items stay at their eligible labels, so a repaired install resumes them
    on the next tick. The per-item guard stays as the backstop for an install
    that changes between this check and a launch.

    A stage whose target scope excludes it (an issue target has no review
    stage unless it has an open closing PR) is not checked.

    Args:
        stage_targets: The stage scopes this tick resolved.
        tick_log: Tick log handle, or ``None``.

    Returns:
        The stage-target keys (``"reviewer"``, ``"implementer"``,
        ``"fixer"``) whose dispatch is blocked this tick. Empty when every
        in-scope stage resolved its docs.
    """
    blocked: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for stage, key, kind, skills in _DISPATCH_STAGE_GUIDANCE:
        target = stage_targets[key]
        if target is not None and target[0] != kind:
            continue
        missing = unresolved_skill_docs(skills)
        if missing:
            blocked.append((stage, key))
            unresolved.extend(name for name in missing if name not in unresolved)
    if not blocked:
        return frozenset()

    probe_paths = [skill_doc_path(name) for name in unresolved]
    stages = [stage for stage, _key in blocked]
    reason = (
        "prompt-bound skill docs unreadable in the pipeline install: "
        f"{', '.join(probe_paths)}"
    )
    print(
        f"tick: dispatch blocked for {', '.join(stages)}: {reason}",
        file=sys.stderr,
    )
    if tick_log:
        tick_log.log(
            pr=None,
            phase="dispatch",
            stage="all",
            action="skill_unresolved",
            reason=reason,
            details={
                "blocked_stages": stages,
                "skills": unresolved,
                "probe_paths": probe_paths,
            },
        )
    return frozenset(key for _stage, key in blocked)


def _engine_env() -> dict[str, str]:
    """Build the env dict for implementer/fixer engine subprocesses.

    Implementers and fixers are trusted local engines running with the
    operator's ambient credentials and capabilities. Preserve the full parent
    environment so authentication works in cmux and other terminals without
    enumerating terminal-specific mechanisms.

    Returns:
        A dict suitable as the ``env`` kwarg to ``subprocess.Popen``.
    """
    return dict(os.environ)


def _resolve_engine(impl_engine: str, model: str = "") -> tuple[str, dict[str, str]]:
    """Translate an engine name to a binary and extra env vars.

    ``"claude-ds"`` is a shell function that wraps ``claude`` with DeepSeek
    provider routing env vars. ``subprocess.Popen`` cannot invoke shell
    functions, so this resolves the function to ``("claude", {...})``.

    Args:
        impl_engine: ``"claude"`` or ``"claude-ds"``.
        model: Optional model override. When non-empty and impl_engine is
            ``"claude-ds"``, sets ``ANTHROPIC_MODEL`` to this value instead
            of the default ``deepseek-v4-pro[1m]``. Ignored for ``"claude"``
            (the model is passed directly to ``--model`` on the CLI).

    Returns:
        ``(binary, extra_env)`` — the binary name and any additional env
        vars to merge into the Popen env.
    """
    if impl_engine != "claude-ds":
        return (impl_engine, {})

    token = os.environ.get("DEEPSEEK_API_KEY", "")
    if not token:
        # KEYPATH names a file containing the key — an existing file always
        # wins, even when a relative filename matches the raw-token shape.
        # The legacy raw-token convention applies only when NOTHING exists
        # at the path AND the value has an unmistakable provider-token
        # shape; everything else fails closed instead of being sent as
        # credentials.
        keypath = os.environ.get("DEEPSEEK_API_KEYPATH", "")
        if keypath:
            if os.path.isfile(keypath):
                try:
                    with open(keypath) as fh:
                        token = fh.read().strip()
                except OSError:
                    # Never echo the configured value; the message may be
                    # retained in logs.
                    raise RuntimeError(
                        "claude-ds: cannot read DEEPSEEK_API_KEYPATH "
                        "(value not shown; file exists but is unreadable)"
                    ) from None
            elif (
                not os.path.lexists(keypath)
                and _RAW_DEEPSEEK_TOKEN_RE.fullmatch(keypath)
            ):
                token = keypath
            else:
                # Never echo the configured value: a legacy raw token that
                # misses the shape check lands here, as does any existing
                # filesystem object (directory, dangling symlink, special
                # file) that is not a readable regular file — and the
                # message may be retained in logs.
                raise RuntimeError(
                    "claude-ds: cannot read DEEPSEEK_API_KEYPATH "
                    "(value not shown; not a readable file)"
                ) from None
    if not token:
        raise RuntimeError(
            "claude-ds requires DEEPSEEK_API_KEY or DEEPSEEK_API_KEYPATH"
        )
    resolved_model = pipeline.effective_engine_model("claude-ds", model)
    extra: dict[str, str] = {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": resolved_model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": resolved_model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": resolved_model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": resolved_model,
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
    }
    return ("claude", extra)


# Reviewer model settings. The reviewer runs under env -i isolation (HOME and
# CODEX_HOME point at a per-PR scrub dir) *and* --ignore-user-config, so the
# engine can never read the operator's own config itself. The tick — the
# trusted parent — reads it instead and forwards the resolved values as
# explicit flags. These fallbacks apply when the file is absent, unparseable,
# or the keys are missing/malformed; without them codex would silently drop to
# its own built-in review defaults.
#
# Only top-level keys are read, and that is complete rather than partial:
# codex rejects the legacy `profile = "..."` key outright (since 0.146), and
# profiles v2 layer $CODEX_HOME/<name>.config.toml only when --profile is
# passed per invocation, which the pipeline never does.
_REVIEWER_MODEL_FALLBACK = "gpt-5.5"
_REVIEWER_EFFORT_FALLBACK = "high"

# codex resolves its own config root from CODEX_HOME (`--profile` layers
# $CODEX_HOME/<name>.config.toml), so the tick must resolve it the same way —
# reading ~/.codex unconditionally would hand a relocated operator the pinned
# fallback while claiming their default in the provenance log.
_REVIEWER_CONFIG_HOME_DEFAULT = "~/.codex"
_REVIEWER_CONFIG_FILENAME = "config.toml"

# Each value is validated for the sink it actually reaches.
#
# Effort is interpolated into a -c model_reasoning_effort="<v>" TOML
# expression, so it must be a bare token: a quote or newline could close the
# string and append a second, unreviewed config key. The operator config's
# review_effort is validated against the same grammar at startup.
_REVIEWER_EFFORT_RE = pipeline.REVIEW_EFFORT_RE
# Model is its own argv element; see pipeline.ENGINE_MODEL_RE, which the
# resolver also applies to review_model, model and fix_model.
_REVIEWER_MODEL_RE = pipeline.ENGINE_MODEL_RE

_REVIEWER_VALUE_RE = {
    "model": _REVIEWER_MODEL_RE,
    "model_reasoning_effort": _REVIEWER_EFFORT_RE,
}

# Only these two keys are ever read from the operator config — derived from the
# grammar map above so a key can never be whitelisted without declaring the
# shape its sink accepts. Forwarding the file wholesale would hand the reviewer
# — the one engine whose entire input is attacker-controlled PR-head content —
# the operator's [mcp_servers] (a node REPL is arbitrary code execution),
# [projects.*] sandbox_mode/network_access, hooks, and notify hooks. Adding a
# key here widens that boundary: don't, unless the key provably cannot affect
# sandboxing, tool access, or execution.
_REVIEWER_CONFIG_KEYS = tuple(_REVIEWER_VALUE_RE)


def resolve_reviewer_model_settings(
    *,
    model_override: str = "",
    effort_override: str = "",
    config_path: str | None = None,
) -> tuple[str, str]:
    """Resolve the reviewer's model and reasoning effort.

    Precedence: explicit override → operator's codex config → pinned fallback.

    Args:
        model_override: Explicit model from ``--review-model``. Empty — or
            failing the shape check for its sink — falls through to the
            operator config.
        effort_override: Explicit effort from ``--review-effort``. Resolves
            the same way as ``model_override``.
        config_path: Path to the operator's codex config. ``None`` resolves
            it from ``CODEX_HOME`` (see :func:`_reviewer_config_path`).

    Returns:
        ``(model, effort)`` — the values to pass as ``--model`` and
        ``-c model_reasoning_effort=...``. Never empty.
    """
    # Overrides are shape-checked like config values: they reach the same two
    # sinks, and the resolver is the one point every entry path funnels
    # through. An unusable override falls through to config, then to the pin.
    model = _reviewer_safe_value("model", model_override, "--review-model")
    effort = _reviewer_safe_value(
        "model_reasoning_effort", effort_override, "--review-effort",
    )

    if not model or not effort:
        path = os.path.expanduser(
            config_path if config_path is not None else _reviewer_config_path()
        )
        config: dict[str, Any] = {}
        try:
            with open(path, "rb") as fh:
                config = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            # Fail soft to the pinned fallbacks: a missing or broken operator
            # config must not silently hand the review to codex's defaults,
            # and must never abort a dispatch.
            config = {}
        if not model:
            model = _reviewer_config_str(config, "model", path)
        if not effort:
            effort = _reviewer_config_str(
                config, "model_reasoning_effort", path,
            )

    return (model or _REVIEWER_MODEL_FALLBACK,
            effort or _REVIEWER_EFFORT_FALLBACK)


def _operator_codex_home() -> str:
    """Return the operator's codex home directory.

    Returns:
        ``$CODEX_HOME`` when set and non-empty, else the expanded
        ``~/.codex``. Both the model config and the auth.json the reviewer
        symlinks live here, so they must resolve through this one function.
    """
    return os.path.expanduser(
        os.environ.get("CODEX_HOME") or _REVIEWER_CONFIG_HOME_DEFAULT
    )


def _reviewer_config_path() -> str:
    """Return the path to the operator's codex config file.

    Returns:
        ``<codex home>/config.toml`` — see :func:`_operator_codex_home`.
    """
    return os.path.join(_operator_codex_home(), _REVIEWER_CONFIG_FILENAME)


def _reviewer_config_str(
    config: dict[str, Any], key: str, source: str = "operator config",
) -> str:
    """Extract one whitelisted reviewer config key as a safe scalar string.

    Args:
        config: The parsed operator config.
        key: The key to read. Must be in :data:`_REVIEWER_CONFIG_KEYS`.
        source: Where the value came from, for the reject diagnostic.

    Returns:
        The value, or ``""`` when absent or unusable — see
        :func:`_reviewer_safe_value`.

    Raises:
        ValueError: If ``key`` is not whitelisted.
    """
    return _reviewer_safe_value(key, config.get(key), source)


def _reviewer_safe_value(key: str, value: object, source: str) -> str:
    """Return ``value`` when it is a usable value for ``key``, else ``""``.

    A present-but-unusable value is reported on stderr rather than swapped
    silently: the reviewer would otherwise run a model the operator never
    chose, with nothing in the run explaining the difference.

    Args:
        key: The setting being resolved. Must be in
            :data:`_REVIEWER_CONFIG_KEYS`.
        value: The candidate value (any type — config is operator-edited
            TOML).
        source: Where the value came from (config path or flag name), used
            in the diagnostic.

    Returns:
        The value, or ``""`` when it is absent, not a string, or fails the
        shape check for its sink (:data:`_REVIEWER_VALUE_RE`).

    Raises:
        ValueError: If ``key`` is not whitelisted.
    """
    if key not in _REVIEWER_CONFIG_KEYS:
        raise ValueError(f"{key!r} is not a whitelisted reviewer config key")
    if value is None or value == "":
        return ""
    if isinstance(value, str) and _REVIEWER_VALUE_RE[key].fullmatch(value):
        return value
    print(
        f"tick: ignoring {key}={value!r} from {source}: "
        f"not a usable {key} value, falling back",
        file=sys.stderr,
    )
    return ""


# Narrow pre-approved grants for headless Claude implementer/fixer engines.
# The default auto mode can approve additional calls through its classifier;
# this list is not a ceiling. dontAsk denies calls that would otherwise prompt.
# Neither mode bypasses operator settings: deny rules are needed to forbid
# force-push, remote-branch deletion, or arbitrary refspec push under auto.
# Keep exactly ONE stage-specific push grant (appended by _engine_allowed_tools)
# exact, never a glob. The prompt is guidance, not enforcement.
_ENGINE_ALLOWED_TOOLS_BASE = (
    "Read Edit Write "
    "Bash(git add *) Bash(git commit *) Bash(git status) Bash(git diff *) "
    "Bash(git status *) Bash(git rev-parse *) Bash(git merge-base *) "
    "Bash(git log *)"
)

# The verification commands an engine is permitted to run are a *permission
# grant*, not an instruction: the engine prompts tell the engine to run the
# repository's own verification commands and follow its CLAUDE.md/AGENTS.md,
# because only the target repo knows what those are. Under dontAsk, undeclared
# commands needing approval are denied; under auto they reach the classifier.
# A denial can still prevent verification and completion in either mode.
#
# The packaged floor is empty. Grants come from operator-owned configuration;
# dispatch pins that choice for launch, completion and durable PR evidence.


def _engine_verify_tools(repo: str = "") -> tuple[str, ...]:
    """Return the engine's verification-command permissions, as `Bash(...)`.

    Reads the resolved ``verify_tools`` — the operator's per-repository value,
    else their default, else the packaged floor — and grants exactly that.
    There is no fallback beneath it: the floor *is* the fallback, and anything
    substituted here would be a grant the operator had already narrowed away.
    An empty resolved list therefore grants no verification command at all,
    which is what ``verify_tools = []`` says.

    Production dispatch validates the resolved configuration and passes its
    own pinned tuple to the launcher. This helper serves callers without that
    snapshot; the body check prevents them from serializing an unsafe grant.
    It does not turn malformed configuration into a dispatchable empty grant:
    startup and dispatch reject resolver errors before publishing custody.

    Args:
        repo: The resolved target as ``owner/name``, for its config layer.

    Returns:
        The permission entries, each already wrapped in ``Bash(...)``.
    """
    return tuple(
        f"Bash({entry})"
        for entry in pipeline.resolve_config(repo).verify_tools
        if pipeline.VERIFY_TOOL_BODY_RE.match(entry)
    )


# The implementer worktree checks out its own branch, so the exact permission
# `git push <remote> HEAD` (fast-forward push of the checked-out branch) is its
# pre-approved remote-mutation surface; auto may approve other commands.
def _implementer_push_permission(remote: str = "") -> str:
    """Grant the implementer one push to the validated target remote.

    Args:
        remote: The remote to name. Empty uses this tick's validated
            ``TARGET_REMOTE``; setup's route probe, which runs outside any
            target checkout, names a representative remote instead.
    """
    return f"Bash(git push {remote or TARGET_REMOTE} HEAD)"


# Conservative shape for a head ref that may be templated into the fixer's
# exact push permission and prompt: git ref-name characters only, no leading
# dash/slash (option or refspec ambiguity). Stricter checks (`..`, `//`,
# trailing `/`/`.`/`.lock`) are applied in _valid_push_head_ref.
_PUSH_HEAD_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")


def _valid_push_head_ref(head_ref: str) -> bool:
    """Return whether ``head_ref`` is safe to template into a push permission.

    The fixer's head ref is substituted into both the exact ``--allowedTools``
    push entry and the prompt's push command. A ref containing whitespace,
    glob, or option-like characters could widen the exact permission or
    smuggle arguments, so anything outside the conservative git ref-name
    shape fails closed (the dispatch escalates instead of launching).

    Args:
        head_ref: The PR head ref name reported by ``gh pr view``.

    Returns:
        ``True`` only for a conservatively shaped, unambiguous branch name.
    """
    if not _PUSH_HEAD_REF_RE.fullmatch(head_ref):
        return False
    if ".." in head_ref or "//" in head_ref or "@{" in head_ref:
        return False
    if head_ref.endswith((".", "/", ".lock")):
        return False
    return True


def _fixer_push_permission(head_ref: str) -> str:
    """Return the fixer's exact, stage-specific push permission.

    The fixer worktree is detached (no local branch custody), so its only
    pre-approved push is the fully-qualified refspec to this PR's validated head
    ref. The caller must have validated ``head_ref`` via
    :func:`_valid_push_head_ref` first.

    Args:
        head_ref: The validated PR head ref name.

    Returns:
        The exact ``Bash(...)`` permission string for ``--allowedTools``.
    """
    return f"Bash(git push {TARGET_REMOTE} HEAD:refs/heads/{head_ref})"


def _verification_prompt(unconfigured: bool) -> str:
    """Supply the engine's dispatch-bound completion obligation."""
    if not unconfigured:
        return (
            "Operator verification grants are configured; "
            "grants are not evidence of execution."
        )
    return (
        pipeline.VERIFICATION_WARNING + "\n"
        "Immediately before your successful completion sentinel, output the exact "
        f"line {pipeline.VERIFICATION_WAIVER}. This acknowledges missing grants "
        "only: still run every check prescribed by the target repository. Never "
        "report absent, denied, or failed checks as passing. A denied or failed "
        "required check still requires NEEDS_HUMAN_DECISION."
    )


def _engine_allowed_tools(
    push_permission: str, verify_tools: tuple[str, ...] | None = None,
) -> str:
    """Compose the engine allowlist from the base set plus one push permission.

    Args:
        push_permission: The stage-specific exact push permission
            (:func:`_implementer_push_permission` or
            :func:`_fixer_push_permission`).
        verify_tools: Dispatch-pinned grants, already wrapped in Bash(...).
            None resolves config for callers outside dispatch; () grants nothing.

    Returns:
        The full ``--allowedTools`` string for the engine launch.
    """
    # The tick drives one canonical target, so the per-repository layer of the
    # operator's config is TRUSTED_HEAD_REPO's — passing nothing here would
    # silently ignore a `[repos."owner/name"] verify_tools` override.
    # An operator who granted no verification command gets no empty slot in the
    # argv for it: `--allowedTools` is whitespace-separated, and a stray double
    # space is a difference in the security boundary's serialization that no
    # test of the resolved list would catch.
    if verify_tools is None:
        verify_tools = _engine_verify_tools(TRUSTED_HEAD_REPO)
    return " ".join(
        part for part in (
            _ENGINE_ALLOWED_TOOLS_BASE,
            *verify_tools,
            push_permission,
        ) if part
    )


_CLAUDE_STREAM_WRAPPER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "claude_stream_wrapper.py",
)


def _heartbeat_path(wt_dir: str) -> str:
    """Return the progress-heartbeat path for one engine worktree.

    Absolute, always. The wrapper that writes this file runs with ``cwd`` set
    to ``wt_dir``, so a relative worktree would resolve a second time
    underneath itself, into a directory that does not exist.
    ``ProgressHeartbeat`` swallows that ``OSError`` by design, so the failure
    is silent: the engine exits 0, the result is valid, and the detector then
    reads a heartbeat nothing ever writes.

    :func:`_resolve_wt_root` makes ``WT_ROOT`` absolute on both its branches,
    so production callers already pass an absolute ``wt_dir`` — this
    resolution is the guard that keeps that a property of the path, not of
    every call site. Resolving here rather than at the launch site also keeps
    the producer and the reader on one definition of the path.

    Args:
        wt_dir: The engine's worktree directory.

    Returns:
        The absolute heartbeat file path, alongside ``result.json``.
    """
    return os.path.abspath(
        os.path.join(wt_dir, claude_stream_wrapper.HEARTBEAT_FILENAME),
    )


def claude_engine_argv(
    engine_bin: str,
    *,
    permission_mode: str,
    allowed_tools: str,
    model: str = "",
    settings: str = "",
    skill_plugin: str = "",
) -> list[str]:
    """Return the headless Claude argv every implementer and fixer runs.

    One definition shared with setup's route probe, which must launch the way
    dispatch does — the same flags, the same settings, and no
    ``--permission-prompt-tool`` — or it measures a different permission path
    than the one engines take.

    Without ``settings`` the engine loads the operator's Claude settings
    hierarchy, which is written for interactive sessions. ``--settings`` layers
    above the user, project and local files, so it can override a scalar such
    as ``sandbox.enabled``. Permission rule lists merge across sources instead:
    it can add deny rules but cannot remove a user allow rule.

    Args:
        engine_bin: The engine binary (``"claude"``).
        permission_mode: ``"auto"`` or ``"dontAsk"``.
        allowed_tools: The complete ``--allowedTools`` value.
        model: ``--model`` value; empty omits the flag.
        settings: ``--settings`` value (the resolved ``engine_settings``);
            empty omits the flag.

    Returns:
        The argv, without the stream wrapper dispatch places in front of it.
    """
    argv = [
        engine_bin, "-p",
        "--permission-mode", permission_mode,
        "--allowedTools", allowed_tools,
        "--output-format", "stream-json",
        "--verbose",
    ]
    if model:
        argv += ["--model", model]
    if settings:
        argv += ["--settings", settings]
    if skill_plugin:
        argv += ["--plugin-dir", skill_plugin]
    return argv


def _launch_claude_engine(
    *,
    engine_bin: str,
    prompt: str,
    wt_dir: str,
    result_path: str,
    env: dict[str, str],
    push_permission: str,
    model: str = "",
    permission_mode: str = "auto",
    verify_tools: tuple[str, ...] | None = None,
    engine_settings: str = "",
    skills: Sequence[str] = (),
    fix_report_protocol_version: int | None = None,
) -> subprocess.Popen[bytes] | None:
    """Launch headless ``claude -p`` with narrow pre-approved tool grants.

    ``claude`` has no ``-o`` output flag: the prompt is fed via stdin from
    ``prompt.txt`` and the result JSON is captured by redirecting stdout to
    ``result_path`` (stderr to ``stderr.log``). The prompt travels via stdin
    rather than argv because argv is visible in ``ps`` and size-capped.
    The engine runs in its own process group (``start_new_session``) so a
    later kill targets only the engine's tree, not the tick's.

    The wrapper is also told where to write its progress heartbeat. Both
    wrapperless stages launch through here, so implementer and fixer get the
    heartbeat from one place rather than two call sites that can drift.

    Args:
        engine_bin: The engine binary (``"claude"``).
        prompt: The full prompt text.
        wt_dir: The engine's worktree directory (also the Popen cwd).
        result_path: Path stdout is redirected to (``result.json``).
        env: The environment for the engine process.
        push_permission: The stage-specific exact ``git push`` permission
            appended to the base allowlist
            (:func:`_implementer_push_permission` or
            :func:`_fixer_push_permission`).
        model: The model to pass to ``--model``. Empty (the default) omits
            the flag entirely so the CLI resolves the operator's own
            configured model — passing a value here overrides it.
        verify_tools: Dispatch-pinned verification grants; never re-resolve a
            provided empty tuple after recording the warning state.
        permission_mode: The ``--permission-mode`` value for the engine:
            ``"auto"`` (default) or ``"dontAsk"``. ``auto`` routes
            non-allowlisted tool calls through the auto-mode classifier while
            the bounded ``--allowedTools`` allowlist still resolves first.
        engine_settings: The target's resolved ``engine_settings``, passed as
            ``--settings``; empty (the default) omits the flag.

    Returns:
        The Popen handle, or ``None`` when target validation, prompt/result
        file setup or spawn failed (the caller cleans up the worktree and logs).

    Raises:
        ValueError: When ``permission_mode`` is not one of the supported
            modes — the engine must never launch with an unknown mode.
    """
    if permission_mode not in ("dontAsk", "auto"):
        raise ValueError(
            f"permission_mode must be 'dontAsk' or 'auto', "
            f"got {permission_mode!r}"
        )
    try:
        pipeline.validate_target_context(
            pipeline.ResolvedTarget(TRUSTED_HEAD_REPO, TARGET_REMOTE),
            cwd=wt_dir, env=env,
        )
    except (OSError, ValueError) as exc:
        print(f"tick: refusing engine launch: target validation failed: {exc}", file=sys.stderr)
        return None

    skill_plugin = ""
    skill_grants = ""
    if skills:
        try:
            skill_plugin = native_skills.claude_plugin(pipeline.INSTALL_SKILLS_ROOT, skills)
            skill_grants = " " + " ".join(
                f"Skill({native_skills.CLAUDE_NAMESPACE}:{name})" for name in skills
            )
        except (OSError, ValueError):
            print("tick: refusing engine launch: native skill setup failed", file=sys.stderr)
            return None
    prompt_path = os.path.join(wt_dir, "prompt.txt")
    stderr_path = os.path.join(wt_dir, "stderr.log")
    engine_cmd = claude_engine_argv(
        engine_bin,
        permission_mode=permission_mode,
        allowed_tools=_engine_allowed_tools(push_permission, verify_tools) + skill_grants,
        model=model,
        settings=engine_settings,
        skill_plugin=skill_plugin,
    )
    prompt_fh = result_fh = stderr_fh = None
    try:
        with open(prompt_path, "w") as fh:
            fh.write(prompt)
        prompt_fh = open(prompt_path, "rb")
        result_fh = open(result_path, "wb")
        stderr_fh = open(stderr_path, "wb")
        proc = subprocess.Popen(
            [
                sys.executable, _CLAUDE_STREAM_WRAPPER,
                "--heartbeat", _heartbeat_path(wt_dir),
                *(["--fix-report-protocol-version", str(fix_report_protocol_version)]
                  if fix_report_protocol_version is not None else []),
                "--", *engine_cmd,
            ],
            stdin=prompt_fh,
            stdout=result_fh,
            stderr=stderr_fh,
            cwd=wt_dir,
            start_new_session=True,
            env=env,
        )
        _track_engine_child(proc)
        return proc
    except OSError:
        return None
    finally:
        # The child holds its own copies of the descriptors; close the
        # parent's so the tick does not leak fds across dispatches.
        for handle in (prompt_fh, result_fh, stderr_fh):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass


_COMMENT_HISTORY_QUERY = """query(
    $owner: String!, $name: String!, $number: Int!, $endCursor: String
) {
    repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
            comments(first: 100, after: $endCursor) {
                totalCount
                nodes { id body author { login } lastEditedAt editor { login } includesCreatedEdit }
                pageInfo { hasNextPage endCursor }
            }
        }
    }
}"""


def _comment_history_snapshot(number: int) -> list[object]:
    """Read a cursor traversal with consistent counts and unique records.

    GraphQL supplies edit provenance, which REST authorship/timestamps cannot
    establish. Cardinality checks detect deletion during traversal even if a
    cursor implementation were to skip a shifted record.
    """
    owner, name = TRUSTED_HEAD_REPO.split("/", 1)
    pages = gh_json(
        "api", "graphql", "--hostname", "github.com", "--paginate", "--slurp",
        "-f", f"query={_COMMENT_HISTORY_QUERY}", "-f", f"owner={owner}",
        "-f", f"name={name}", "-F", f"number={number}",
    )
    if not isinstance(pages, list) or not pages:
        raise ValueError("comment pagination returned no page array")
    comments: list[object] = []
    seen: set[str] = set()
    cursors: set[str] = set()
    total: int | None = None
    for index, page in enumerate(pages):
        try:
            if page.get("errors"):
                raise ValueError("comment query returned GraphQL errors")
            connection = page["data"]["repository"]["pullRequest"]["comments"]
            nodes = connection["nodes"]
            count = connection["totalCount"]
            info = connection["pageInfo"]
            more, cursor = info["hasNextPage"], info["endCursor"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError("comment pagination returned a malformed page") from exc
        if not isinstance(nodes, list) or len(nodes) > 100:
            raise ValueError("comment pagination returned malformed nodes")
        if type(count) is not int or count < 0 or (total is not None and count != total):
            raise ValueError("comment count changed during pagination or is invalid")
        total = count
        if type(more) is not bool or more != (index < len(pages) - 1):
            raise ValueError("comment pagination ended before the final page")
        if more and len(nodes) != 100:
            raise ValueError("comment pagination returned an incomplete page")
        if nodes:
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("comment pagination has an invalid or repeated cursor")
            cursors.add(cursor)
        elif cursor is not None or len(pages) != 1:
            raise ValueError("comment pagination returned an unexpected empty page")
        for entry in nodes:
            if not isinstance(entry, dict):
                raise ValueError("comment pagination contains a malformed entry")
            comment_id = entry.get("id")
            if not isinstance(comment_id, str) or not comment_id or comment_id in seen:
                raise ValueError("comment pagination contains an invalid or repeated id")
            seen.add(comment_id)
            comments.append(entry)
    if len(comments) != total:
        raise ValueError("comment pagination did not return the declared total")
    return comments


def _read_comment_history(
    number: int, *, phase: str, stage: str, tick_log: TickLog | None,
    trusted_author: str | None = None,
) -> list[object] | None:
    """Require two matching complete traversals before trusting comment history.

    A changing history is retained for a later tick, never retried until it
    happens to permit progress. This detects observed races; GitHub supplies no
    atomic snapshot spanning reads and subsequent dispatch/publication.
    """
    author = TRUSTED_REVIEW_AUTHOR if trusted_author is None else trusted_author
    try:
        comments = _comment_history_snapshot(number)
        trusted = pipeline.trusted_comment_history(comments, trusted_author=author)
        confirmed = _comment_history_snapshot(number)
        pipeline.trusted_comment_history(confirmed, trusted_author=author)
        if confirmed != comments:
            raise ValueError("comment history changed between complete reads")
    except (subprocess.CalledProcessError, ValueError) as exc:
        reason = f"comment history unavailable or untrusted: {exc} — retained for retry"
        print(f"tick: PR #{number}: {reason}", file=sys.stderr)
        if tick_log:
            tick_log.log(pr=number, phase=phase, stage=stage,
                         action="comment_history_refused", reason=reason)
        return None
    if len(trusted) != len(comments) and tick_log:
        tick_log.log(pr=number, phase=phase, stage=stage,
                     action="comment_authors_ignored",
                     reason=f"ignored {len(comments) - len(trusted)} comments from other authors")
    return comments


# --- gh / git helpers ---


def _gh_repo_args(args: tuple[str, ...], repo: str | None) -> tuple[str, ...]:
    """Bind commands without interpreting user content as repository flags."""
    if args and args[0] in {"pr", "issue"}:
        target_repo = TRUSTED_HEAD_REPO if repo is None else repo
        if not target_repo:
            raise ValueError("target repository has not been resolved")
        # The resolver accepts github.com remotes. Pin the host as well so
        # GH_HOST or local gh configuration cannot redirect this identity.
        return (*args, "--repo", f"github.com/{target_repo}")
    return args


def gh(
    *args: str, check: bool = True, repo: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a ``gh`` command and return the completed process.

    ``encoding="utf-8", errors="replace"`` prevents ``UnicodeDecodeError`` on
    non-UTF-8 bytes (CJK, emoji, smart quotes in issue bodies). The ``gh``
    CLI always emits UTF-8; ``errors="replace"`` is a safety net.

    Args:
        *args: Arguments to pass to ``gh``.
        check: If True (default), raise on non-zero exit.
        repo: Explicit ``owner/name`` for qualified reads, such as a linked issue
            in another repository. Defaults to the canonical target. Use this
            keyword instead of embedding ``--repo`` in ``args``.

    Returns:
        The completed process with captured stdout/stderr.

    Raises:
        subprocess.CalledProcessError: If ``check`` is True and exit != 0.
    """
    return subprocess.run(
        ["gh", *_gh_repo_args(args, repo)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def gh_json(*args: str, repo: str | None = None) -> Any:
    """Run ``gh --json ...`` and return parsed JSON.

    Args:
        *args: Arguments to pass to ``gh`` (typically ``--json`` queries).
        repo: Optional explicit ``owner/name`` for qualified issue reads.

    Returns:
        The parsed JSON value.

    Raises:
        subprocess.CalledProcessError: On non-zero exit.
    """
    result = gh(*args, check=True, repo=repo)
    return json.loads(result.stdout)


def gh_text(*args: str, repo: str | None = None) -> str:
    """Run ``gh ...`` and return stripped stdout text.

    Args:
        *args: Arguments to pass to ``gh``.
        repo: Optional explicit ``owner/name`` for qualified issue reads.

    Returns:
        The stripped stdout text.

    Raises:
        subprocess.CalledProcessError: On non-zero exit.
    """
    result = gh(*args, check=True, repo=repo)
    return result.stdout.strip()


# --- relabel ---


def relabel(
    entity: str,
    number: int,
    from_label: str,
    to_label: str,
    *,
    tick_log: "TickLog | None" = None,
    phase: str = "",
    stage: str = "",
) -> None:
    """Transition a PR/issue's pipeline label on GitHub.

    Handles the ``gh pr edit`` or ``gh issue edit`` label mutation. Empty
    ``from_label`` omits ``--remove-label``. ``from_label == to_label`` is a
    no-op. When ``to_label`` is terminal, emits best-effort operator
    notifications after the GitHub edit succeeds.

    Args:
        entity: ``"pr"`` or ``"issue"`` — the GitHub entity type.
        number: The PR or issue number.
        from_label: The label to remove (empty string = no removal).
        to_label: The label to add.
        tick_log: Optional structured tick log for notification entries.
        phase: Tick phase for the notification entry.
        stage: Pipeline stage for the notification entry.
    """
    if from_label == to_label:
        return
    if from_label:
        gh(
            entity, "edit", str(number),
            "--add-label", to_label,
            "--remove-label", from_label,
        )
    else:
        gh(entity, "edit", str(number), "--add-label", to_label)
    if to_label in _TERMINAL_LABELS and tick_log is not None:
        _notify_terminal_label(entity, number, to_label, tick_log, phase, stage)


def _notify_terminal_label(
    entity: str,
    number: int,
    label: str,
    tick_log: "TickLog | None",
    phase: str,
    stage: str,
) -> None:
    """Emit best-effort operator signals for terminal label transitions."""
    reason = f"{entity} #{number} reached terminal label {label}"
    print(f"tick: {reason}", file=sys.stderr)
    if tick_log is not None:
        tick_log.log(
            pr=number,
            phase=phase,
            stage=stage,
            action="notify_terminal_label",
            reason=reason,
            details={"entity": entity, "label": label},
        )
    _emit_operator_notification(
        title="PR pipeline terminal label",
        message=reason,
    )


def _relabel_with_context(
    entity: str,
    number: int,
    from_label: str,
    to_label: str,
    *,
    tick_log: "TickLog | None",
    phase: str,
    stage: str,
) -> None:
    """Relabel with notification context only when a tick log exists."""
    if tick_log is None:
        relabel(entity, number, from_label, to_label)
        return
    relabel(
        entity,
        number,
        from_label,
        to_label,
        tick_log=tick_log,
        phase=phase,
        stage=stage,
    )


def _restore_released_in_flight_label(
    entity: str, number: int, label: str, *, observed: list[str] | None,
) -> bool | None:
    """Re-add an in-flight label whose release lost its justification.

    A release is only ever justified by a hold that outranks the pipeline's
    own custody. When that hold turns out to be gone, the removal has to be
    undone or the item can be left carrying no workflow label at all — a state
    no later tick can route, because the marker is then outside pipeline
    custody and gets archived as ordinary cleanup.

    That is the whole justification, so it does not extend to an item some
    other workflow label already owns: the release runs only under a hold, and
    nothing later removes an in-flight label re-added beside a human's
    transition, so the item would sit at a pipeline label no tick will ever
    clear and no dispatcher will ever pick up. It costs no admission
    slot — the cap counts markers — but the item is stranded until a person
    strips the label by hand. An unreadable observation names no owner and is
    restored like an empty one.

    Args:
        entity: ``"pr"`` or ``"issue"`` — the GitHub entity type.
        number: The PR or issue number.
        label: The in-flight label to restore.
        observed: The pipeline labels read after the release landed, or
            ``None`` when that read failed.

    Returns:
        ``None`` when no restore was owed because another workflow label
        already owns the item; otherwise whether a read-back confirms the
        label is back on it.
    """
    if observed:
        return None
    try:
        gh(entity, "edit", str(number), "--add-label", label)
        labels = _pipeline_labels(
            gh_json(entity, "view", str(number), "--json", "labels"),
        )
    except (subprocess.CalledProcessError, ValueError):
        return False
    return labels is not None and label in labels


def _restore_outcome_note(restored: bool | None) -> str:
    """Render the restore half of a release-refusal log reason."""
    if restored is None:
        return "another workflow label owns the item"
    if restored:
        return "restored in-flight custody"
    return "could not confirm restored in-flight custody"


def _release_in_flight_label(
    entity: str,
    number: int,
    label: str,
    *,
    tick_log: "TickLog | None",
    phase: str,
    stage: str,
    required_hold_label: str,
) -> bool:
    """Remove the pipeline's own in-flight label without adding anything.

    Used when proven engine death ends a dispatch whose item is under a human
    hold. ``relabel`` always adds a target label, which would be a custody
    mutation here; this emits a pure ``--remove-label`` so the operator's hold
    survives untouched.

    Args:
        entity: ``"pr"`` or ``"issue"`` — the GitHub entity type.
        number: The PR or issue number.
        label: The in-flight label to release. Must not be a human hold.
        tick_log: Tick log handle, or ``None``.
        phase: Tick phase for the log entry.
        stage: Pipeline stage for the log entry.
        required_hold_label: Exact workflow hold that justifies this release
            and must survive it. Required rather than defaulted: the hold is
            the *only* thing that makes removing the pipeline's own label
            legitimate, so a caller with none to name has lost that provenance
            and the release fails closed. A default would let a later decision
            path opt out of the guard by omission — which is how this helper's
            two call sites came to disagree in the first place.

    The removal is confirmed by a read-back before returning success. A plain
    relabel elsewhere can skip that, but here the caller destroys the marker
    immediately afterwards — and the marker is the only thing that can re-prove
    death. An unconfirmed release would leave the in-flight label standing over
    a destroyed marker: a markerless in-flight item, which no tick clears
    and no dispatcher picks up. It spends nothing — the cap counts
    markers, so the slot is freed by the very destruction that stranded it —
    but the item is stuck at a pipeline label until a person strips it, with
    the one artifact that could have re-proved death already gone.

    Returns:
        ``True`` when the removal landed and was confirmed absent (or was
        refused as a no-op because ``label`` is not a pipeline in-flight
        label), ``False`` when the mutation or its confirmation failed and the
        caller must retain custody.
    """
    if label in pipeline.HOLD_LABELS or label not in _STAGE_IN_FLIGHT_LABELS.values():
        # Defense in depth: this path exists only to drop an automation
        # receipt. Anything else is human custody and is never mutated here.
        return True
    if required_hold_label not in pipeline.HOLD_LABELS:
        if tick_log:
            tick_log.log(
                pr=number, phase=phase, stage=stage,
                action="label_release_failed",
                reason=(
                    "refusing a release with no known workflow hold to "
                    "justify it"
                ),
                details={
                    "release_label": label,
                    "required_hold_label": required_hold_label,
                },
            )
        return False
    try:
        before = _pipeline_labels(
            gh_json(entity, "view", str(number), "--json", "labels"),
        )
    except (subprocess.CalledProcessError, ValueError):
        before = None
    if before is None or required_hold_label not in before:
        if tick_log:
            tick_log.log(
                pr=number, phase=phase, stage=stage,
                action="label_release_failed",
                reason="observed workflow hold changed before release",
                details={
                    "release_label": label,
                    "required_hold_label": required_hold_label,
                },
            )
        return False
    try:
        gh(entity, "edit", str(number), "--remove-label", label)
        confirmed = _pipeline_labels(
            gh_json(entity, "view", str(number), "--json", "labels"),
        )
    except (subprocess.CalledProcessError, ValueError):
        # ValueError covers malformed/truncated JSON on a zero-exit gh. The
        # removal has already landed here, so raising would escape to the
        # tick_crash handler and stop the loop instead of retaining custody.
        confirmed = None
    if confirmed is None or label in confirmed:
        if tick_log:
            tick_log.log(
                pr=number, phase=phase, stage=stage,
                action="label_release_failed",
                reason="retaining scratch until the in-flight label is released",
                details={"release_label": label},
            )
        return False
    if required_hold_label not in confirmed:
        restored = _restore_released_in_flight_label(
            entity, number, label, observed=confirmed,
        )
        if tick_log:
            tick_log.log(
                pr=number, phase=phase, stage=stage,
                action="label_release_failed",
                reason=(
                    "observed workflow hold changed during release; "
                    + _restore_outcome_note(restored)
                ),
                details={
                    "release_label": label,
                    "required_hold_label": required_hold_label,
                    "in_flight_restored": restored,
                },
            )
        return False
    if tick_log:
        tick_log.log(
            pr=number, phase=phase, stage=stage,
            action="in_flight_label_released",
            reason="proven engine death under a human hold freed the slot",
            details={"release_label": label},
        )
    return True


def _pipeline_labels(payload: object) -> list[str] | None:
    """Extract the complete ai:* label set from a gh labels payload."""
    if not isinstance(payload, dict):
        return None
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, list):
        return None
    labels: list[str] = []
    for label in raw_labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            return None
        name = label["name"]
        if name.startswith("ai:"):
            labels.append(name)
    return labels


def _converge_pipeline_labels(
    entity: str,
    number: int,
    target_label: str,
    *,
    tick_log: "TickLog | None",
    phase: str,
    stage: str,
) -> bool:
    """Converge all remote pipeline labels to one target, or none.

    The add and every removal are combined into a single ``gh edit`` mutation
    followed by a verification read, so the window for a partially applied
    transition is one API call, not one per label. A failed mutation, a
    verified mismatch, or an unreadable verification read all return ``False``
    so the dispatch marker and scratch remain durable for the next tick to
    retry: the retained marker keeps the item in the reconcile set, and a
    transition that actually landed completes its bookkeeping through the
    already-converged early return on that retry.

    ``ai:needs-human-decision`` and ``ai:blocked`` are human holds: when either
    is present, convergence refuses to touch the labels and fails closed. Even
    an escalation cannot replace one human hold with another; the operator's
    current label set is the durable custody authority.

    Hold targets are the one exception to the already-converged contract above.
    When the remote already sits at exactly the hold this call wanted to set,
    ``False`` is returned rather than ``True``: automation cannot prove it
    created that hold, and reporting success would let a caller continue into
    scratch cleanup on a human's label. Bookkeeping is not resumed by a later
    escalation either — the hold gate in ``_reconcile_one`` leaves held items
    alone — so it is instead completed by the group-death cleanup path, which
    archives the scratch, clears the attempt counter, and releases the
    in-flight label once the engine is provably gone. A pending terminal
    intent is the exception: its resume re-runs before the hold gate and
    classifies the live label set itself (:func:`_terminal_relabel_state`), so
    an exact hold reads as its own landed relabel and finishes local custody,
    while a different hold sitting beside the stage's in-flight label reads as
    ``held`` — the resume never calls this function there, because it could
    only refuse, and instead releases the in-flight label so the label set
    stops claiming pipeline custody the hold has already taken authority over.
    The admission slot is freed by the custody move that destroys the marker,
    not by that release; the cap counts markers, never labels.
    """
    try:
        labels = _pipeline_labels(
            gh_json(entity, "view", str(number), "--json", "labels"),
        )
    except (subprocess.CalledProcessError, ValueError):
        # ValueError covers malformed/truncated JSON from a zero-exit gh.
        labels = None
    if labels is None:
        return False

    expected = [target_label] if target_label else []
    if sorted(set(labels)) == expected:
        # A pre-existing hold is human custody.  Treating it as our own
        # already-landed mutation would let callers continue into scratch
        # cleanup without any causal proof that automation created the hold.
        if target_label in pipeline.HOLD_LABELS:
            if tick_log:
                tick_log.log(
                    pr=number, phase=phase, stage=stage,
                    action="human_hold_retained",
                    reason=(
                        "target human hold already present; refusing to "
                        "claim automation ownership"
                    ),
                    details={"target_label": target_label},
                )
            return False
        if target_label in _TERMINAL_LABELS:
            _notify_terminal_label(
                entity, number, target_label, tick_log, phase, stage,
            )
        return True

    holds_present = pipeline.HOLD_LABELS & set(labels)
    if holds_present:
        if tick_log:
            tick_log.log(
                pr=number, phase=phase, stage=stage,
                action="human_hold_retained",
                reason="human hold label present; refusing to converge",
                details={
                    "target_label": target_label,
                    "hold_labels": sorted(holds_present),
                },
            )
        return False

    mutation_args: list[str] = []
    if target_label and target_label not in labels:
        mutation_args += ["--add-label", target_label]
    for label in dict.fromkeys(labels):
        if label != target_label:
            mutation_args += ["--remove-label", label]
    try:
        gh(entity, "edit", str(number), *mutation_args)
    except subprocess.CalledProcessError:
        return False

    try:
        final_labels = _pipeline_labels(
            gh_json(entity, "view", str(number), "--json", "labels"),
        )
    except (subprocess.CalledProcessError, ValueError):
        final_labels = None
    if final_labels is None:
        # The mutation reported success but the confirmation read failed.
        # Fail closed anyway: a hold added concurrently between the initial
        # read and the mutation could still be present alongside the target,
        # and only a verified read can rule that out. The retained marker
        # keeps the item in the reconcile set, so the next tick re-enters
        # this helper, hits the already-converged early return, and completes
        # the interrupted bookkeeping idempotently.
        if tick_log:
            tick_log.log(
                pr=number, phase=phase, stage=stage,
                action="label_convergence_unverified",
                reason=(
                    "mutation succeeded; verification read failed — "
                    "retaining custody for an idempotent retry"
                ),
                details={"target_label": target_label},
            )
        return False
    if sorted(set(final_labels)) != expected:
        return False

    if target_label in _TERMINAL_LABELS:
        _notify_terminal_label(
            entity, number, target_label, tick_log, phase, stage,
        )
    return True


def _resolve_dispatchable_pickup_label(
    entity: str,
    number: int,
    *,
    stage: str,
    authoritative_in_flight: list[pipeline.InFlightRef],
    tick_log: "TickLog | None",
) -> str | None:
    """Return the pickup label this candidate may be dispatched from.

    The pickup queries are label *filters*, not eligibility proofs: they select
    every open item carrying a pickup label, including one that also carries an
    in-flight label or a human hold. Since then a markerless in-flight item is
    never converged, so a partial transition on another machine — in-flight
    label added, pickup label not yet removed — is a state that persists for
    the life of that engine and keeps matching the query. The local marker
    scan cannot disprove it, because that engine's marker is on the other
    machine's disk.

    So dispatch proves eligibility from the item's own complete label set,
    read at the point of decision, before any mutating step, and judges it with
    :func:`pipeline.resolve_pickup_eligibility` — the same rule every cadence
    probe applies, so dispatch and cadence cannot disagree about what counts as
    work. Every branch fails closed: the candidate simply waits for a tick that
    can prove it is free.

    Skips when:

    - the identity is already in the authoritative in-flight set (this tick has
      seen its in-flight label, or already dispatched it at *any* stage) —
      free, no read;
    - the label set is unreadable;
    - :func:`pipeline.resolve_pickup_eligibility` refuses it: an in-flight
      label, a human hold, or anything but exactly one pickup label across all
      of the entity's stages.

    Args:
        entity: ``"issue"`` or ``"pr"``.
        number: The issue or PR number to check.
        stage: The pipeline stage asking, used for eligibility and log entries.
        authoritative_in_flight: In-flight refs known to this tick, including
            engines it launched earlier in the same DISPATCH phase — at any
            stage, since all three dispatchers share one list.
        tick_log: Tick log handle, or ``None``.

    Returns:
        The single pickup label the item currently carries, or ``None`` when
        dispatch must skip it.
    """
    def skip(reason: str, details: dict[str, object] | None = None) -> None:
        if tick_log:
            tick_log.log(
                pr=number, phase="dispatch", stage=stage,
                action="skip", reason=reason, details=details,
            )

    kind = "issue" if entity == "issue" else "pr"
    held_by = [
        ref for ref in authoritative_in_flight
        if ref.kind == kind and ref.number == number
    ]
    if held_by:
        skip(
            "already in-flight this tick",
            {"in_flight_labels": sorted({ref.label for ref in held_by})},
        )
        return None

    try:
        lines = gh_text(
            entity, "view", str(number),
            "--json", "labels", "-q", ".labels[].name",
        )
    except subprocess.CalledProcessError:
        skip("label read failed; cannot prove dispatch eligibility")
        return None

    labels = {line.strip() for line in lines.splitlines() if line.strip()}

    verdict = pipeline.resolve_pickup_eligibility(labels, entity=kind, stage=stage)
    if not verdict.label:
        skip(verdict.reason, {"blocking_labels": list(verdict.blocking)})
        return None

    return verdict.label


# --- cleanup_scratch ---


def cleanup_scratch(wt_path: str) -> None:
    """Remove the engine's worktree scratch directory.

    Deletes markers, result files, and completion envelopes so a later
    dispatch does not mistake a stale result for a fresh one. The worktree
    directory itself is also removed.

    Also removes the corresponding reviewer scrub directory (``pr-{N}-scrub``
    under the same parent), which lives outside the worktree.

    Args:
        wt_path: Path to the worktree directory (e.g. ``.claude/worktrees/pr-42``).
    """
    if os.path.isdir(wt_path):
        shutil.rmtree(wt_path, ignore_errors=True)
    # Scrub dir for reviewers lives alongside the worktree, not inside it.
    scrub_dir = os.path.join(os.path.dirname(wt_path),
                             os.path.basename(wt_path) + "-scrub")
    if os.path.isdir(scrub_dir):
        shutil.rmtree(scrub_dir, ignore_errors=True)


def _surface_retained_scratch_dirs(
    wt_root: str,
    tick_log: "TickLog | None",
) -> None:
    """Emit a log entry for each retained scratch dir with no live engine.

    Runs at the start of each tick so orphaned directories (from
    ``kill_escalate``, failed relabel custody, or interrupted harvests)
    are impossible to miss in the tick log.

    Args:
        wt_root: Worktree root directory.
        tick_log: Tick log handle, or ``None`` to skip.
    """
    if tick_log is None:
        return
    try:
        entries = os.listdir(wt_root)
    except OSError:
        return
    for entry in sorted(entries):
        if not (entry.startswith("pr-") or entry.startswith("issue-")):
            continue
        wt_dir = os.path.join(wt_root, entry)
        if not os.path.isdir(wt_dir):
            continue
        marker_path = os.path.join(wt_dir, "dispatch.json")
        if not os.path.isfile(marker_path):
            # No marker at all — definitely retained/orphaned.
            tick_log.log(
                pr=None,
                phase="startup",
                stage="",
                action="retained_scratch",
                reason="no dispatch marker",
                details={"wt_dir": wt_dir, "entry": entry},
            )
            continue
        # Has a marker — check if the engine is still alive.
        try:
            with open(marker_path) as fh:
                marker = json.load(fh)
        except (OSError, json.JSONDecodeError):
            tick_log.log(
                pr=None,
                phase="startup",
                stage="",
                action="retained_scratch",
                reason="corrupt dispatch marker",
                details={"wt_dir": wt_dir, "entry": entry},
            )
            continue
        if not isinstance(marker, dict):
            tick_log.log(
                pr=None,
                phase="startup",
                stage="",
                action="retained_scratch",
                reason="corrupt dispatch marker",
                details={"wt_dir": wt_dir, "entry": entry},
            )
            continue
        try:
            classify_state, _identity, _pid_alive = _classify_marker(
                wt_dir, marker, result_file=None,
            )
        except Exception:  # noqa: BLE001
            tick_log.log(
                pr=None,
                phase="startup",
                stage="",
                action="retained_scratch",
                reason="classify failed",
                details={"wt_dir": wt_dir, "entry": entry},
            )
            continue
        if classify_state not in ("live", "launching"):
            dispatch_id = marker.get("dispatch_id")
            tick_log.log(
                pr=marker.get("pr_number") if isinstance(marker.get("pr_number"), int) else None,
                phase="startup",
                stage=str(marker.get("stage", "")),
                action="retained_scratch",
                reason=f"classify={classify_state}",
                dispatch_id=dispatch_id if isinstance(dispatch_id, str) else "",
                details={"wt_dir": wt_dir, "entry": entry,
                         "classify_state": classify_state},
            )


def _result_excerpt(value: object, *, limit: int = 500) -> str:
    """Return a bounded single-line excerpt for engine result text."""
    if not isinstance(value, str):
        return ""
    excerpt = value.replace("\n", "\\n")
    if len(excerpt) <= limit:
        return excerpt
    return f"{excerpt[:limit]}..."


def _engine_result_summary(
    *,
    result_raw: str,
    harvest_result: dict[str, object],
    harvest_action: str,
    harvest_label: str,
) -> dict[str, object]:
    """Build a redaction-safe summary of a harvested engine result."""
    try:
        result_obj: object = json.loads(result_raw)
    except (ValueError, RecursionError):
        result_obj = None

    details: dict[str, object] = {
        "valid_json": isinstance(result_obj, dict),
        "result_bytes": len(result_raw.encode("utf-8", errors="replace")),
        "harvest_action": harvest_action,
        "harvest_label": harvest_label,
        "sentinel": harvest_result.get("sentinel"),
        "usage": engine_usage.claude_usage(result_obj),
        "reported_models": engine_usage.claude_models(result_obj),
    }
    if not isinstance(result_obj, dict):
        return details

    permission_denials = result_obj.get("permission_denials")
    denial_tools: list[str] = []
    if isinstance(permission_denials, list):
        for denial in permission_denials:
            if isinstance(denial, dict):
                tool_name = denial.get("tool_name")
                if isinstance(tool_name, str):
                    denial_tools.append(tool_name)

    for key in ("is_error", "api_error_status", "stop_reason", "duration_ms"):
        value = result_obj.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            details[key] = value

    turns = result_obj.get("num_turns")
    details["num_turns"] = turns if type(turns) is int and 0 <= turns <= 2**63 - 1 else None

    # The wrapper sums usage, turns and duration across the CLI's per-segment
    # result events when a background task re-entered the session; this count
    # marks that aggregation (1 means a single result event; absent is legacy).
    result_events = result_obj.get("result_events")
    details["result_events"] = (
        result_events if type(result_events) is int and 1 <= result_events <= 2**63 - 1 else None
    )

    details["result_excerpt"] = _result_excerpt(result_obj.get("result"))
    details["permission_denial_count"] = (
        len(permission_denials) if isinstance(permission_denials, list) else 0
    )
    details["permission_denial_tools"] = denial_tools
    return details


def _replace_evidence_file(
    archive_dir: str,
    filename: str,
    write: Callable[[str], object],
) -> bool:
    """Write one archive file through a temporary sibling, then replace it.

    A published report can already name the archive, so its files must stay
    whole: a crash or failed write leaves the previous complete file, or no
    file, never a partial one. A later attempt overwrites a leftover sibling.

    Args:
        archive_dir: The archive directory.
        filename: The destination file name inside ``archive_dir``.
        write: Writes the complete content to the path it receives.

    Returns:
        ``True`` once the complete file is in place.
    """
    partial = os.path.join(archive_dir, f".{filename}.partial")
    try:
        write(partial)
        os.replace(partial, os.path.join(archive_dir, filename))
    except OSError:
        try:
            os.unlink(partial)
        except OSError:
            pass
        return False
    return True


def _copy_evidence_file(wt_dir: str, archive_dir: str, filename: str) -> bool:
    """Copy one harvest evidence file if it exists.

    Returns:
        ``True`` when the source is absent or copied successfully.
    """
    source = os.path.join(wt_dir, filename)
    if not os.path.isfile(source):
        return True
    return _replace_evidence_file(
        archive_dir, filename, lambda partial: shutil.copy2(source, partial),
    )


def _write_git_snapshot(
    wt_dir: str,
    archive_dir: str,
    filename: str,
    *args: str,
) -> bool:
    """Write a git diagnostic snapshot for a harvest archive."""
    try:
        output = git("-C", wt_dir, *args)
    except subprocess.CalledProcessError as exc:
        output = f"<unavailable: git {' '.join(args)} exited {exc.returncode}>"

    def write(partial: str) -> None:
        with open(partial, "w", encoding="utf-8") as fh:
            fh.write(f"{output}\n")

    return _replace_evidence_file(archive_dir, filename, write)


def _harvest_evidence_path(wt_root: str, kind: str, number: int, dispatch_id: str) -> str:
    """Return the stable evidence location used by a terminal intent and its report."""
    safe_dispatch_id = "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in dispatch_id
    ) or "unknown"
    return os.path.join(wt_root, ".harvest-evidence", f"{kind}-{number}-{safe_dispatch_id}")


# Written last by _archive_harvest_evidence. A reused archive without it was
# interrupted during preparation and is completed before anything names it.
_ARCHIVE_PREPARED_MARKER = ".prepared"


def _archive_harvest_evidence(
    *,
    wt_root: str,
    wt_dir: str,
    kind: str,
    number: int,
    dispatch_id: str,
    tick_log: "TickLog | None",
    stage: str = "",
    phase: str = "harvest",
    required_files: tuple[str, ...] = (),
    reuse_existing: bool = False,
) -> str:
    """Preserve engine harvest evidence before scratch cleanup.

    Escalation cleanup deletes ``result.json`` and siblings. This archive keeps a
    local forensic bundle so operators can distinguish model errors, permission
    denials, missing sentinels, and side-effect failures after the label changes.

    With ``reuse_existing`` a retry reuses the prepared directory its own
    earlier attempt left behind instead of creating a suffixed copy: a
    retained prepared archive (e.g. after a failed release or final move) must
    not accumulate one directory per retry tick. The base name embeds the
    dispatch id, which is unique per dispatch, so an existing base directory
    can only belong to this same transition's earlier attempt. Only a
    directory with the prepared marker is returned as is. An earlier attempt
    that stopped during preparation is completed in place from the retained
    source, and is kept when that repair fails: a report may already name it.
    Each file is replaced atomically (see :func:`_replace_evidence_file`), so
    repairing a complete legacy archive never leaves a published file partial.

    Args:
        wt_root: The worktree root holding controller-owned state.
        wt_dir: The item's worktree directory.
        kind: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        dispatch_id: The dispatch whose evidence is archived.
        tick_log: Optional tick log.
        stage: The pipeline stage.
        phase: ``"harvest"`` or ``"dispatch"``.
        required_files: Files that must exist in ``wt_dir`` or the archive is
            discarded.
        reuse_existing: When the base directory for this dispatch already
            exists, return it without re-copying once it is marked prepared,
            or complete it in place otherwise.

    Returns:
        The archive directory path, or ``""`` when evidence could not be
        preserved.
    """
    archive_base = _harvest_evidence_path(wt_root, kind, number, dispatch_id)
    archive_root = os.path.dirname(archive_base)
    repairing = reuse_existing and os.path.isdir(archive_base)
    if repairing:
        if os.path.isfile(os.path.join(archive_base, _ARCHIVE_PREPARED_MARKER)):
            return archive_base
        archive_dir = archive_base
    else:
        try:
            os.makedirs(archive_root, exist_ok=True)
        except OSError:
            return ""
        archive_dir = archive_base
        suffix = 2
        while True:
            try:
                os.mkdir(archive_dir)
            except FileExistsError:
                archive_dir = f"{archive_base}-{suffix}"
                suffix += 1
                continue
            except OSError:
                return ""
            break

    archive_ok = all(
        os.path.isfile(os.path.join(wt_dir, filename))
        for filename in required_files
    )
    for filename in (
        "dispatch.json",
        "result.json",
        "review.json",
        "completion.json",
        "stderr.log",
        "prompt.txt",
        "review-comment-body.txt",
        "review-rendered.md",
        "review-manifest.json",
        "review-rejected.json",
        "fix-rendered.md",
        "fix-manifest.json",
        _PR_BODY_SOURCE_FILE,
        _PR_BODY_REVISION_FILE,
        ".pr-metadata-decision.json",
    ):
        if not _copy_evidence_file(wt_dir, archive_dir, filename):
            archive_ok = False
    if not _write_git_snapshot(
        wt_dir, archive_dir, "git-status.txt", "status", "--short",
    ):
        archive_ok = False
    if not _write_git_snapshot(
        wt_dir, archive_dir, "git-diff-stat.txt", "diff", "--stat",
    ):
        archive_ok = False
    if archive_ok:
        try:
            with open(
                os.path.join(archive_dir, _ARCHIVE_PREPARED_MARKER),
                "w",
                encoding="utf-8",
            ):
                pass
        except OSError:
            archive_ok = False
    if not archive_ok:
        if not repairing:
            shutil.rmtree(archive_dir, ignore_errors=True)
        return ""

    if tick_log:
        tick_log.log(
            pr=number,
            phase=phase,
            stage=stage or ("implement" if kind == "issue" else "fix"),
            action="harvest_evidence",
            reason="archived result before cleanup",
            engine="",
            dispatch_id=dispatch_id,
            details={"archive_dir": archive_dir},
        )
    return archive_dir


def _discard_prepared_archive(archive_dir: str) -> None:
    """Remove a prepared evidence archive after a failed terminal transition.

    Use only where no published report can reference this directory. The
    caller retains source scratch so a retry can archive it again. Transitions
    that may have published the path must retain it and use ``reuse_existing``
    to avoid creating one directory per retry during a GitHub outage.

    Args:
        archive_dir: The archive directory created by
            :func:`_archive_harvest_evidence`, or ``""``.
    """
    if archive_dir:
        shutil.rmtree(archive_dir, ignore_errors=True)


def _finalize_harvest_archive(
    wt_dir: str,
    archive_dir: str,
    *,
    number: int,
    stage: str,
    phase: str,
    dispatch_id: str,
    tick_log: "TickLog | None",
) -> bool:
    """Move complete scratch into evidence custody and reclaim git metadata.

    The root-level evidence copies are convenient for incident inspection, but
    they are not a complete recovery bundle. Moving the original directory under
    ``scratch/`` preserves untracked and otherwise unknown files before cleanup.
    The move makes a linked git worktree's metadata prunable, so reclamation is
    part of this shared boundary rather than any one caller's cleanup path.

    Args:
        wt_dir: Source scratch directory.
        archive_dir: Prepared archive directory.
        number: Issue or PR number for failure logging.
        stage: Pipeline stage that owns the archive.
        phase: ``"harvest"`` or ``"dispatch"``.
        dispatch_id: Dispatch correlation id.
        tick_log: Tick log handle, or ``None``.

    Returns:
        ``True`` after archive custody moves successfully. Metadata-prune failure
        is logged but does not undo archive custody; the next dispatch retries it.
    """
    scratch_dir = os.path.join(archive_dir, "scratch")
    if not os.path.isdir(wt_dir) or os.path.lexists(scratch_dir):
        return False
    try:
        os.rename(wt_dir, scratch_dir)
    except OSError:
        return False
    if not _prune_worktree_metadata(os.path.dirname(wt_dir)) and tick_log:
        tick_log.log(
            pr=number,
            phase=phase,
            stage=stage,
            action="worktree_prune_failed",
            reason="git worktree prune failed; stale metadata until next dispatch",
            dispatch_id=dispatch_id,
        )
    return True


def _finalize_terminal_archive(
    *,
    wt_dir: str,
    archive_dir: str,
    number: int,
    stage: str,
    dispatch_id: str,
    reason: str,
    tick_log: "TickLog | None",
) -> bool:
    """Move full scratch into archive custody before a terminal relabel."""
    if _finalize_harvest_archive(
        wt_dir,
        archive_dir,
        number=number,
        stage=stage,
        phase="harvest",
        dispatch_id=dispatch_id,
        tick_log=tick_log,
    ):
        return True
    if tick_log:
        tick_log.log(
            pr=number,
            phase="harvest",
            stage=stage,
            action="archive_failed",
            reason=reason,
            dispatch_id=dispatch_id,
        )
    return False


def _archive_and_cleanup_scratch(
    *,
    wt_root: str,
    wt_dir: str,
    kind: str,
    number: int,
    dispatch_id: str,
    tick_log: "TickLog | None",
    stage: str,
    phase: str,
    failure_reason: str,
) -> bool:
    """Archive a full scratch tree, then remove its remaining sidecar state.

    Args:
        wt_root: Worktree root containing the evidence directory.
        wt_dir: Scratch directory to archive and clean.
        kind: ``"issue"`` or ``"pr"``.
        number: Issue or PR number.
        dispatch_id: Dispatch correlation id.
        tick_log: Tick log handle, or ``None``.
        stage: Pipeline stage.
        phase: ``"harvest"`` or ``"dispatch"``.
        failure_reason: Operator-facing reason when archival cannot complete.

    Returns:
        ``True`` after cleanup is safe, ``False`` when scratch custody is retained.
    """
    if not os.path.isdir(wt_dir):
        cleanup_scratch(wt_dir)
        return True
    archive_dir = _archive_harvest_evidence(
        wt_root=wt_root,
        wt_dir=wt_dir,
        kind=kind,
        number=number,
        dispatch_id=dispatch_id,
        tick_log=tick_log,
        stage=stage,
        phase=phase,
    )
    archive_ok = bool(archive_dir) and _finalize_harvest_archive(
        wt_dir,
        archive_dir,
        number=number,
        stage=stage,
        phase=phase,
        dispatch_id=dispatch_id,
        tick_log=tick_log,
    )
    if not archive_ok:
        if tick_log:
            tick_log.log(
                pr=number,
                phase=phase,
                stage=stage,
                action="archive_failed",
                reason=failure_reason,
                dispatch_id=dispatch_id,
            )
        return False
    cleanup_scratch(wt_dir)
    return True


def _skill_usage_summary(result_raw: str) -> tuple[list[str], str]:
    """Read trusted stream telemetry embedded by the controller wrapper."""
    try:
        result = json.loads(result_raw)
    except json.JSONDecodeError:
        return [], "unavailable"
    skills = result.get("skills_used") if isinstance(result, dict) else None
    status = result.get("skill_usage_status") if isinstance(result, dict) else None
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        return [], "unavailable"
    return skills, "active" if status == "active" else "unavailable"


def _log_skill_context(
    *, tick_log: "TickLog | None", number: int, stage: str,
    dispatch_id: str, engine: str,
) -> None:
    """Record the requested names separately from the existing observation rows."""
    if tick_log:
        requested = IMPLEMENTER_SKILLS if stage == "implement" else FIXER_SKILLS if stage == "fix" else ()
        tick_log.log(
            pr=number, phase="dispatch", stage=stage, action="skill_context",
            reason="installed skills available; use not yet observed",
            engine=engine, dispatch_id=dispatch_id,
            details={"requested_skills": list(requested),
                     "available_skills": list(native_skills.REVIEW_SKILLS if stage == "review" else requested)},
        )


def _log_reviewer_skill_observation(
    *,
    tick_log: "TickLog | None",
    number: int,
    wt_dir: str,
    dispatch_id: str,
) -> None:
    """Persist sanitized Codex skill-file evidence before scratch cleanup."""
    if not tick_log:
        return
    observation = reviewer_observability.load_observation(
        os.path.join(wt_dir, "review-observation.json"),
    )
    # Version 1 artifacts retain their skill evidence and honestly lack usage.
    observation.setdefault("usage", engine_usage.codex_usage(None))
    observation["schema_version"] = 2
    tick_log.log(
        pr=number,
        phase="harvest",
        stage="review",
        action="reviewer_skill_observation",
        reason=f"observation_status={observation['status']}",
        engine="codex",
        dispatch_id=dispatch_id,
        details=observation,
    )


def _log_engine_result_summary(
    *,
    tick_log: "TickLog | None",
    number: int,
    stage: str,
    dispatch_id: str,
    result_raw: str,
    harvest_result: dict[str, object],
    harvest_action: str,
    harvest_label: str,
    skills_used: list[str] | None = None,
    skill_usage_status: str = "unavailable",
    engine: str = "",
) -> None:
    """Emit structured harvest result metadata without raw tool payloads."""
    if not tick_log:
        return
    details = _engine_result_summary(
        result_raw=result_raw,
        harvest_result=harvest_result,
        harvest_action=harvest_action,
        harvest_label=harvest_label,
    )
    details["skills_used"] = list(skills_used or [])
    details["skill_usage_status"] = skill_usage_status
    tick_log.log(
        pr=number,
        phase="harvest",
        stage=stage,
        action="engine_result_summary",
        reason=f"action={harvest_action}",
        engine=engine,
        dispatch_id=dispatch_id,
        details=details,
    )


def _log_side_effect_failure(
    *,
    tick_log: "TickLog | None",
    number: int,
    stage: str,
    dispatch_id: str,
    operation: str,
    exc: subprocess.CalledProcessError,
) -> None:
    """Log a bounded side-effect failure without dropping scratch evidence."""
    if not tick_log:
        return
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    stdout = exc.stdout if isinstance(exc.stdout, str) else ""
    tick_log.log(
        pr=number,
        phase="harvest",
        stage=stage,
        action="side_effect_failed",
        reason=operation,
        engine="",
        dispatch_id=dispatch_id,
        details={
            "operation": operation,
            "returncode": exc.returncode,
            "stderr_excerpt": _result_excerpt(stderr),
            "stdout_excerpt": _result_excerpt(stdout),
        },
    )


def _remove_worktree(wt_path: str) -> bool:
    """Remove a git worktree and its metadata from the main repo.

    ``cleanup_scratch`` removes the directory contents, but the worktree
    metadata in ``.git/worktrees/`` survives until ``git worktree remove``
    (or ``git worktree prune``) is called. Without this, ``git worktree list``
    accumulates stale entries that pin SHAs and prevent garbage collection.

    Args:
        wt_path: Absolute or relative path to the worktree directory.

    Returns:
        ``True`` if the worktree was successfully removed, ``False`` if the
        directory does not exist or ``git worktree remove`` failed.
    """
    if not os.path.isdir(wt_path):
        return False
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", wt_path],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except subprocess.SubprocessError:
        return False


def _prune_worktree_metadata(cwd: str) -> bool:
    """Reclaim ``.git/worktrees/`` entries whose directory no longer exists.

    ``git worktree remove`` cannot be used once a scratch tree has been *moved*
    into the evidence archive: it takes the original path, which no longer
    exists, so it can never succeed. Git marks such an entry ``prunable``
    instead, and ``git worktree prune`` is the operation that reclaims it.

    Failure is not a custody risk. Evidence already lives in the archive by
    this point, and every ``git worktree add`` site prunes before adding, so a
    surviving entry is reclaimed by the next dispatch at the latest. The return
    value is reported so callers can log the delay rather than assume it.

    Args:
        cwd: Any directory inside the repository whose worktree metadata should
            be pruned. Passed to ``git -C`` so the prune never depends on the
            process-wide working directory — the same shape the three
            ``git worktree add`` sites already use.

    Returns:
        ``True`` when the prune completed, ``False`` when git failed.
    """
    try:
        git("-C", cwd, "worktree", "prune")
    except (subprocess.SubprocessError, OSError):
        return False
    return True


# --- verify_codex_bin ---


def _parse_codex_version(version_str: str) -> tuple[int, ...]:
    """Parse ``"codex-cli X.Y.Z"`` into a comparable integer tuple.

    Pre-release suffixes (e.g. ``0.141.0-beta.1``) are stripped.
    Exactly 3 numeric components are required.
    """
    parts = version_str.strip().split()
    if len(parts) != 2 or parts[0] != "codex-cli":
        raise ValueError(f"unexpected codex version format: {version_str!r}")
    # Strip pre-release suffix before dot-split (e.g. "0.141.0-beta.1" → "0.141.0")
    base_version = parts[1].split("-", 1)[0]
    segments = base_version.split(".")
    if len(segments) != 3:
        raise ValueError(
            f"expected exactly 3 version components, got {len(segments)}: "
            f"{version_str!r}"
        )
    try:
        return tuple(int(x) for x in segments)
    except ValueError:
        raise ValueError(f"non-numeric version component in {version_str!r}") from None


def verify_codex_bin(
    min_version: str = MIN_CODEX_VERSION,
) -> str:
    """Resolve and verify the Codex binary meets the minimum version.

    Args:
        min_version: The minimum acceptable ``codex --version`` output
            (e.g. ``"codex-cli 0.141.0"``).

    Returns:
        Absolute path to the verified ``codex`` binary.

    Raises:
        RuntimeError: If ``codex`` is not found on PATH or its version
            is below ``min_version``.
    """
    bin_path = shutil.which("codex")
    if not bin_path:
        raise RuntimeError("codex not found on PATH")

    try:
        version_result = subprocess.run(
            [bin_path, "--version"],
            capture_output=True, encoding="utf-8", errors="replace", check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError("codex --version failed") from None

    version = version_result.stdout.strip().rstrip("\r")
    try:
        installed = _parse_codex_version(version)
        minimum = _parse_codex_version(min_version)
    except ValueError:
        raise RuntimeError(
            f"codex version parse error: got {version!r}, "
            f"minimum {min_version!r}"
        ) from None

    if installed < minimum:
        raise RuntimeError(
            f"codex version {version!r} is below minimum {min_version!r}"
        )
    return bin_path


# --- Engine cooldowns ---


def transient_cooldown_until(
    result_obj: object,
    *,
    now: float,
    cooldown_seconds: int = DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
    reset_buffer_seconds: int = TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS,
) -> float:
    """Return the provider cooldown deadline for a transient engine result.

    Args:
        result_obj: Parsed engine result; reset hints may refine the deadline.
        now: Current epoch time.
        cooldown_seconds: Cooldown when the result names no reset time.
        reset_buffer_seconds: Margin added to a reset time the result names.

    Returns:
        Epoch time after which the provider may be dispatched again.
    """
    fallback = now + cooldown_seconds
    if not isinstance(result_obj, dict):
        return fallback
    result_text = result_obj.get("result")
    if not isinstance(result_text, str):
        return fallback
    match = _RESET_HINT_RE.search(result_text)
    if match is None:
        return fallback

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return fallback
    if match.group(3).casefold() == "pm" and hour != 12:
        hour += 12
    elif match.group(3).casefold() == "am" and hour == 12:
        hour = 0

    timezone_name = match.group(4)
    try:
        timezone = (
            ZoneInfo(timezone_name)
            if timezone_name is not None
            else dt.datetime.fromtimestamp(now).astimezone().tzinfo
        )
    except ZoneInfoNotFoundError:
        return fallback
    if timezone is None:
        return fallback

    current = dt.datetime.fromtimestamp(now, timezone)
    reset = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    deadline = reset + dt.timedelta(seconds=reset_buffer_seconds)
    if deadline.timestamp() <= now:
        deadline += dt.timedelta(days=1)
    return deadline.timestamp()


def _engine_quota_domain(engine: str) -> str:
    """Return the account-level quota domain shared across engine stages."""
    if engine not in {"claude", "claude-ds"}:
        raise ValueError(f"unsupported engine quota domain: {engine!r}")
    return engine


class EngineCooldowns:
    """Durable provider-domain cooldown state shared across engine stages."""

    def __init__(self, wt_root: str) -> None:
        """Bind cooldown state to a pipeline worktree root.

        Args:
            wt_root: Root containing engine worktrees and durable tick state.
        """
        self._directory = os.path.join(wt_root, ".engine-cooldowns")

    def _path(self, domain: str) -> str:
        safe_domain = "".join(
            char if char.isalnum() or char in ("-", "_", ".") else "_"
            for char in domain
        )
        return os.path.join(self._directory, f"{safe_domain}.json")

    @staticmethod
    def _validated_deadline(state: object, domain: str) -> float:
        """Return the deadline from one exact, domain-bound cooldown payload."""
        if (
            not isinstance(state, dict)
            or set(state) != {"domain", "until", "reason"}
            or state.get("domain") != domain
            or not isinstance(state.get("reason"), str)
            or not state.get("reason")
        ):
            raise ValueError("invalid engine cooldown state")
        until = state.get("until")
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            raise ValueError("invalid engine cooldown state")
        deadline = float(until)
        if not math.isfinite(deadline):
            raise ValueError("invalid engine cooldown deadline")
        return deadline

    def start(self, domain: str, *, until: float, reason: str) -> None:
        """Persist a cooldown deadline for one provider domain.

        Args:
            domain: Provider quota domain shared across dispatch stages.
            until: Epoch time after which dispatch may resume.
            reason: Bounded classification reason for operator diagnostics.
        """
        if domain not in {"claude", "claude-ds"} or not math.isfinite(until):
            raise ValueError("cooldown domain and deadline must be valid")
        if not isinstance(reason, str) or not reason:
            raise ValueError("cooldown reason must be a non-empty string")
        os.makedirs(self._directory, exist_ok=True)
        path = self._path(domain)
        try:
            with open(path) as fh:
                existing = json.load(fh)
        except FileNotFoundError:
            existing = None
        except json.JSONDecodeError as exc:
            raise ValueError("invalid engine cooldown state") from exc
        if existing is not None:
            until = max(until, self._validated_deadline(existing, domain))
        pipeline.atomic_write(
            path,
            json.dumps({"domain": domain, "until": until, "reason": reason}),
        )

    def read_active(self, domain: str, *, now: float) -> float | None:
        """Return an active deadline, or ``None`` when absent or expired.

        Args:
            domain: Provider quota domain to inspect.
            now: Current epoch time.

        Returns:
            Active cooldown deadline, or ``None``.
        """
        path = self._path(domain)
        try:
            with open(path) as fh:
                state = json.load(fh)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError("invalid engine cooldown state") from exc
        deadline = self._validated_deadline(state, domain)
        if deadline <= now:
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        return deadline


def _provider_cooldown_blocks_dispatch(
    *,
    number: int,
    stage: str,
    engine: str,
    tick_log: "TickLog | None",
) -> bool:
    """Return whether provider cooldown state must block a paid dispatch."""
    try:
        domain = _engine_quota_domain(engine)
        cooldown_until = EngineCooldowns(WT_ROOT).read_active(
            domain, now=time.time(),
        )
    except (OSError, ValueError):
        if tick_log:
            tick_log.log(
                pr=number, phase="dispatch", stage=stage,
                action="cooldown_state_error",
                reason="provider cooldown state is unreadable",
                engine=engine,
                details={"domain": engine},
            )
        return True
    if cooldown_until is None:
        return False
    if tick_log:
        tick_log.log(
            pr=number, phase="dispatch", stage=stage,
            action="cooldown_skip",
            reason="provider quota cooldown is active",
            engine=engine,
            details={
                "domain": domain,
                "cooldown_until": cooldown_until,
            },
        )
    return True


# --- Attempts (durable dispatch-attempt counter) ---


class Attempts:
    """Durable counter for resultless dispatch failures.

    Mirrors the bash ``read_attempts`` / ``incr_attempts`` / ``clear_attempts``
    pattern. Each PR's count lives in ``<root>/<key_prefix><N>-attempts`` so
    it survives worktree cleanup. The comment-based round cap never counts
    resultless failures; this separate cap stops a repeatable infrastructure
    failure from re-dispatching forever.
    """

    def __init__(self, root: str, key_prefix: str = "") -> None:
        """Args:
            root: Directory where attempt-counter files are stored.
            key_prefix: Prefix for counter filenames (``""`` for PR,
                ``"issue-"`` for issue).
        """
        self._root = root
        self._key_prefix = key_prefix

    def _path(self, n: int) -> str:
        return os.path.join(self._root, f"{self._key_prefix}{n}-attempts")

    def read(self, n: int) -> int:
        """Return the attempt count for PR/issue ``n``, or 0."""
        try:
            with open(self._path(n)) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return 0

    def incr(self, n: int) -> None:
        """Increment the attempt count for PR/issue ``n``."""
        current = self.read(n)
        os.makedirs(self._root, exist_ok=True)
        pipeline.atomic_write(self._path(n), str(current + 1))

    def clear(self, n: int) -> None:
        """Delete the attempt counter for PR/issue ``n``."""
        try:
            os.unlink(self._path(n))
        except OSError:
            pass


# --- TickLog ---


class TickLog:
    """Per-tick audit trail logging.

    Wraps ``pipeline.open_tick_log`` / ``pipeline.append_tick_entry`` /
    ``pipeline.read_latest_tick_log`` with a convenience interface. Logging
    is non-fatal: a write failure must never abort a tick or leave a PR in
    an inconsistent state.
    """

    def __init__(self, path: str) -> None:
        """Bind to an already-opened tick log file.

        Args:
            path: Absolute path to the ``*.jsonl`` log file.
        """
        self.path = path

    @staticmethod
    def open(log_dir: str, keep: int = 50) -> "TickLog":
        """Create a new tick log file, prune old ones, and return a handle.

        Args:
            log_dir: Directory for tick log files.
            keep: Max existing log files to retain.

        Returns:
            A TickLog instance bound to the new log file.
        """
        path = pipeline.tick_log_open(log_dir=log_dir, keep=keep)
        return TickLog(path)

    def log(
        self,
        *,
        pr: int | None,
        phase: str,
        stage: str,
        action: str,
        reason: str,
        engine: str = "",
        dispatch_id: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        """Append one entry to the tick log. Never raises.

        Args:
            pr: PR/issue number, or ``None`` for summary entries.
            phase: ``"harvest"`` or ``"dispatch"``.
            stage: ``"review"``, ``"implement"``, or ``"fix"``.
            action: The reconcile/harvest/dispatch action taken.
            reason: Human-readable explanation.
            engine: The engine value (``"claude"``, ``"claude-ds"``,
                ``"codex"``), or ``""`` for harvest entries.
            dispatch_id: The dispatch identifier that links a dispatch log
                entry to its harvest entry across ticks; empty string when
                not associated with a specific engine dispatch.
            details: Optional structured detail dict (e.g. admission counts,
                SHAs, classify state). Omitted from the entry when ``None``.
        """
        entry: dict[str, object] = {
            "ts": int(time.time()),
            "pr": pr,
            "phase": phase,
            "stage": stage,
            "action": action,
            "reason": reason,
            "engine": engine,
            "dispatch_id": dispatch_id,
        }
        if details is not None:
            entry["details"] = details
        try:
            pipeline.tick_log_write(log_path=self.path, entry=entry)
        except Exception:  # noqa: BLE001
            pass

    def tail(self) -> list[dict[str, object]]:
        """Return parsed entries from this log file."""
        return pipeline.tick_log_tail(log_dir=os.path.dirname(self.path))

    @staticmethod
    def tail_dir(log_dir: str) -> list[dict[str, object]]:
        """Return parsed entries from the newest log in ``log_dir``."""
        return pipeline.tick_log_tail(log_dir=log_dir)


# --- Authoritative in-flight label read (Step 6) ---


def _authoritative_in_flight_numbers(payload: object) -> list[int]:
    """Validate one GitHub in-flight query payload and return its numbers.

    Args:
        payload: Parsed ``gh ... --json number`` output.

    Returns:
        Positive, non-boolean item numbers.

    Raises:
        ValueError: If the top-level shape or any row is malformed.
    """
    if not isinstance(payload, list):
        raise ValueError("authoritative in-flight payload must be a list")
    numbers: list[int] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("authoritative in-flight payload row must be an object")
        number = item.get("number")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
        ):
            raise ValueError(
                "authoritative in-flight payload number must be a positive integer",
            )
        numbers.append(number)
    return numbers


def read_authoritative_in_flight_labels() -> list[pipeline.InFlightRef]:
    """Query GitHub for every item currently carrying an in-flight label.

    Reads three label sets independently:
    - ``issue:ai:implementing`` (open issues)
    - ``pr:ai:reviewing`` (open PRs)
    - ``pr:ai:fixing`` (open PRs)

    Every transport or payload-validation failure propagates (fail-closed):
    a transient API error or malformed successful response must never be
    mistaken for "zero in-flight items" and let the admission cap over-admit.

    Returns:
        A list of ``InFlightRef`` records for all in-flight items.
    """
    refs: list[pipeline.InFlightRef] = []

    implementing = gh_json(
        "issue", "list", "--label", "ai:implementing", "--state", "open",
        "--json", "number",
    )
    for n in _authoritative_in_flight_numbers(implementing):
        refs.append(pipeline.InFlightRef(
            kind="issue", number=n, label="ai:implementing",
        ))

    reviewing = gh_json(
        "pr", "list", "--label", "ai:reviewing", "--state", "open",
        "--json", "number",
    )
    for n in _authoritative_in_flight_numbers(reviewing):
        refs.append(pipeline.InFlightRef(
            kind="pr", number=n, label="ai:reviewing",
        ))

    fixing = gh_json(
        "pr", "list", "--label", "ai:fixing", "--state", "open",
        "--json", "number",
    )
    for n in _authoritative_in_flight_numbers(fixing):
        refs.append(pipeline.InFlightRef(
            kind="pr", number=n, label="ai:fixing",
        ))

    return refs


def read_authoritative_human_holds(
    occupied: set[tuple[str, int, str]],
) -> dict[tuple[str, int], frozenset[str]]:
    """Read durable human custody from GitHub for the occupied items.

    The remote hold label is the source of truth; no local receipt is needed.
    Only occupied identities can be discounted from cadence, so only those are
    read: one call per distinct ``(kind, number)`` currently holding a slot,
    rather than a scan of the repository's entire history of held items.
    ``gh view`` resolves an item in any state, so open, closed, and merged
    items share one contract without a ``--state all`` scan that would
    eventually truncate and silently drop a hold.

    The cost tracks occupancy, and occupancy is this machine's dispatch
    markers alone — a markerless in-flight label is a deliberate no-op that
    no tick clears and consumes no slot. Admission gates new markers at the
    configured ``max_in_flight``, so the read count is bounded by the live
    marker set rather than by ``gh list``'s page size across the in-flight
    labels. A backlog of human-held or stranded labels no longer shows up here
    as tick latency, because those items are never read.

    Args:
        occupied: The ``(kind, number, label)`` identities currently holding an
            admission slot.

    Returns:
        A mapping of ``(kind, number)`` to the hold labels present on it.
        Items with no hold are omitted.

    Raises:
        ValueError: If an identity has an unknown kind, or GitHub returns a
            label payload that cannot be validated.
        subprocess.CalledProcessError: If a ``gh`` read fails. Propagating
            keeps an unavailable control plane from being mistaken for
            "nothing is held", which would let cadence claim quiescence over
            an engine a human still owns.
    """
    holds: dict[tuple[str, int], frozenset[str]] = {}
    for kind, number in sorted({(k, n) for k, n, _label in occupied}):
        if kind not in {"issue", "pr"}:
            raise ValueError(f"unknown item kind: {kind}")
        labels = _pipeline_labels(
            gh_json(kind, "view", str(number), "--json", "labels"),
        )
        if labels is None:
            raise ValueError("malformed human-hold label payload")
        held = pipeline.HOLD_LABELS & set(labels)
        if held:
            holds[(kind, number)] = frozenset(held)
    return holds


def _supervisable_occupancy(
    occupied: set[tuple[str, int, str]],
    human_holds: Mapping[tuple[str, int], frozenset[str]],
    actionable: frozenset[tuple[str, int]] = frozenset(),
) -> tuple[int, tuple[str, ...]]:
    """Exclude operator-owned items from cadence, never from admission.

    A hold alone is not enough to discount an item: the pipeline may still owe
    it a harvest or a kill. Only a held item with no pending pipeline action is
    genuinely unsupervisable — see :func:`_pipeline_actionable`.

    Args:
        occupied: The ``(kind, number, label)`` identities holding a slot.
        human_holds: Hold labels per item, from
            :func:`read_authoritative_human_holds`.
        actionable: Items the pipeline still has work on, which are never
            discounted however they are labelled.

    Returns:
        ``(supervisable_count, held_slot_names)`` where the names are
        operator-facing and stable-ordered. One name per *discounted slot*,
        not per item: admission is keyed ``(kind, number, label)`` so that an
        accidentally overlapping reviewer and fixer dispatch on one PR are two
        paid slots, and collapsing them here would stop
        ``supervisable + held`` from reconstructing occupancy in exactly the
        case that keying exists for.
    """
    held_slots = {
        ref for ref in occupied
        if (ref[0], ref[1]) in human_holds and (ref[0], ref[1]) not in actionable
    }
    supervisable = occupied - held_slots
    names: list[str] = []
    for kind, number, label in sorted(held_slots):
        labels = human_holds[(kind, number)]
        preferred = (
            pipeline.LABEL_NEEDS_HUMAN
            if pipeline.LABEL_NEEDS_HUMAN in labels
            else sorted(labels)[0]
        )
        names.append(f"{kind} #{number} {label} ({preferred})")
    return len(supervisable), tuple(names)


# --- can_dispatch_now (Step 7) ---


def _nonempty_file(path: str) -> bool:
    """Return whether ``path`` is an existing file with size > 0."""
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _is_positive_marker_pid(marker: Mapping[str, object]) -> bool:
    """Return whether a marker names a process at all.

    ``valid_marker`` makes the two ownership groups mutually exclusive: a
    published marker carries ``pid``/``start_time``/``pgid``, a pre-spawn lease
    carries only ``state``. A ``False`` here therefore means the marker is a
    lease — there is no process identity to probe, signal, or prove dead.

    Args:
        marker: A validated dispatch marker.

    Returns:
        ``True`` when the marker records a usable positive PID.
    """
    pid = marker.get("pid")
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def _marker_process_group_is_alive(marker: Mapping[str, object]) -> bool:
    """Return whether a marker's validated positive process group is alive."""
    pgid = marker.get("pgid")
    return bool(
        isinstance(pgid, int)
        and not isinstance(pgid, bool)
        and pgid > 0
        and process_control.pgid_is_alive(pgid)
    )


def _terminate_marker_engine(
    marker: Mapping[str, object],
    number: int,
    *,
    tick_log: "TickLog | None",
    stage: str,
    reason: str,
    dispatch_id: str = "",
) -> bool:
    """Terminate a marker's engine, preferring its whole process group.

    Only ever called once identity is proven ``matched`` (Invariant 4): the
    recorded PID is the process this pipeline spawned, not a reused number.

    Args:
        marker: The validated dispatch marker naming the process.
        number: The PR or issue number, for logging.
        tick_log: Tick log handle, or ``None``.
        stage: Pipeline stage for the log entry.
        reason: Why the termination fired, for the ``kill_failed`` entry.
        dispatch_id: Marker dispatch id for the log entry.

    Returns:
        ``True`` when the process (or its whole group) is confirmed stopped,
        ``False`` when it survived SIGKILL — callers must then retain the
        marker so a later tick can re-attempt the kill.
    """
    pid = marker.get("pid")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
        return True
    pgid = marker.get("pgid")
    if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 0:
        killed = process_control.terminate_process_group(
            pgid, grace_seconds=5, kill_grace_seconds=2, poll_interval=0.5,
        )
    else:
        killed = process_control.terminate_process(
            pid, grace_seconds=5, kill_grace_seconds=2, poll_interval=0.5,
        )
    if not killed and tick_log:
        tick_log.log(
            pr=number, phase="harvest", stage=stage,
            action="kill_failed",
            reason=reason,
            dispatch_id=dispatch_id,
        )
    return killed


# The only stages whose engine runs under `engine_wrapper.py`, which is the
# sole legitimate author of `completion.json`. Dispatch passes
# `--envelope-path` for review and for nothing else, so an envelope in any
# other stage's worktree had no legitimate writer — and the party that *could*
# have written it is the engine the envelope claims to speak for. Implementers
# and fixers hold an unrestricted `Write` tool with the worktree as their cwd
# (`_ENGINE_ALLOWED_TOOLS_BASE`, `_launch_claude_engine`), `dispatch.json` sits
# in that cwd carrying the one value an envelope must match, and
# `validate_completion_envelope` binds an envelope to nothing else — not the
# real wrapper pid, not the stage, not the writer. Reading one for those stages
# lets a running engine declare its own completion, suppress its own liveness,
# and publish the result it wrote for itself. Their completion evidence is
# proven process death instead (`wrapperless_result_complete`), which the
# engine cannot forge.
#
# This repo already drew this conclusion twice and skipped this artifact.
# `.receipts` and `_TERMINAL_INTENT_DIR` both live under WT_ROOT rather than in
# the item's scratch precisely because scratch-resident proof is forgeable —
# see their comments below. `completion.json` is the one durable proof artifact
# that stayed behind. Relocating it the same way would subsume this gate and
# cover review as well; that is a change to archive ordering and custody, so it
# is a follow-up rather than a line here. Until then the gate is what holds,
# and it is sufficient for the reachable hole: the reviewer engine runs
# `codex exec -s read-only` and cannot write the file at all.
_WRAPPER_ENVELOPE_STAGES = frozenset({"review"})


def _classify_marker(
    wt_dir: str,
    marker: dict[str, object],
    *,
    result_file: str | None = None,
) -> tuple[str, str, bool]:
    """Classify a dispatch marker's state from its liveness facts.

    Performs identity-verified liveness, envelope validation, deadline
    coercion, and classify_dispatch. Shared by ``_scan_marker_identities`` and
    ``_reconcile_one`` to avoid duplicated logic.

    Args:
        wt_dir: Path to the worktree directory.
        marker: The parsed ``dispatch.json`` dict.
        result_file: Stage-specific result filename, or ``None`` to check
            both ``review.json`` and ``result.json`` (for admission scanning).

    Returns:
        A tuple of ``(classify_state, identity, engine_alive_bool)`` where
        ``engine_alive_bool`` is true when the recorded PID or its process
        group remains alive.
    """
    pid = marker.get("pid")
    recorded_start = marker.get("start_time")
    pid_alive = False
    identity = "unknown"
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        live_start = process_control.get_process_start_time(pid)
        if isinstance(recorded_start, (int, float)) and not isinstance(recorded_start, bool):
            identity = pipeline.verify_identity(
                recorded_start=float(recorded_start),
                live_start=live_start,
            )
            pid_alive = identity in ("matched", "unknown")
            # verify_identity conflates "dead" with "unreadable" ("unknown"
            # for both). A signal-zero probe distinguishes them, but wrapper
            # death alone does not release custody: a paid child can remain
            # alive in the recorded process group. Without the PID probe,
            # a finished wrapper-less engine (implementer/fixer — no
            # completion envelope) classifies "live" until its deadline and
            # its result is never harvested; without the group probe, a
            # crashed wrapper can permit duplicate dispatch.
            if identity == "unknown" and not process_control.pid_is_alive(pid):
                pid_alive = _marker_process_group_is_alive(marker)
                if not pid_alive:
                    # Published markers always carry a validated positive
                    # process group.  Two negative probes therefore prove
                    # that neither the wrapper nor a paid child still owns
                    # custody; make that proof available to reconciliation.
                    identity = "dead"

    # Envelope check. Gated on the stage, not just on the file existing: see
    # _WRAPPER_ENVELOPE_STAGES for why a wrapperless stage's envelope is
    # untrusted input rather than evidence.
    stage = marker.get("stage")
    envelope_valid = False
    failure_envelope_valid = False
    completion_path = os.path.join(wt_dir, "completion.json")
    dispatch_id = marker.get("dispatch_id")
    if (
        stage in _WRAPPER_ENVELOPE_STAGES
        and os.path.isfile(completion_path)
        and isinstance(dispatch_id, str)
    ):
        try:
            with open(completion_path) as fh:
                envelope = json.load(fh)
        except (OSError, json.JSONDecodeError):
            envelope = None
        envelope_valid = pipeline.validate_envelope(
            envelope=envelope,
            dispatch_id=dispatch_id,
            report_protocol_version=marker.get("report_protocol_version"),
        )
        failure_envelope_valid = pipeline.validate_failure_envelope(
            envelope,
            expected_dispatch_id=dispatch_id,
            report_protocol_version=marker.get("report_protocol_version"),
        )

    # Result file: use stage-specific name when given, else check both.
    # Zero-byte files are treated as absent: with stdout-redirect result
    # capture, result.json exists (empty) from the moment of spawn, and a
    # crashed engine must classify stale (bounded revert), not complete.
    if result_file is not None:
        result_exists = _nonempty_file(os.path.join(wt_dir, result_file))
    else:
        result_exists = _nonempty_file(os.path.join(wt_dir, "review.json")) or \
                        _nonempty_file(os.path.join(wt_dir, "result.json"))
    if envelope_valid and marker.get("report_protocol_version") == pipeline.review_reports.VERSION:
        result_exists = os.path.isfile(os.path.join(wt_dir, "review.json"))

    # Implementers and fixers run wrapperless and therefore have no verified
    # process exit status. Proven process death plus a non-empty result is enough
    # to enter harvest, where the result contract is parsed and classified. Keep
    # this fact in memory: persisting a synthetic completion envelope would falsely
    # claim that a wrapper observed exit_status=0 and validated the result.
    wrapperless_result_exists = _nonempty_file(os.path.join(wt_dir, "result.json"))
    wrapperless_result_complete = (
        stage in ("implement", "fix")
        and wrapperless_result_exists
        and not pid_alive
        and isinstance(dispatch_id, str)
        and bool(dispatch_id)
    )

    # Deadline coercion (matches _cmd_classify_dispatch: coerce to +inf, never 0.0)
    raw_deadline = marker.get("deadline", float("inf"))
    if isinstance(raw_deadline, bool):
        deadline = float("inf")
    else:
        try:
            deadline = float(raw_deadline)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            deadline = float("inf")

    marker_state = str(marker.get("state", ""))
    # A validated wrapper envelope is terminal authority. In particular, a
    # startup-failure envelope can be published before the wrapper leaves its
    # process group, so observed group liveness must not override that durable
    # result. Group custody protects only the no-envelope wrapper-death path.
    # This is safe only because the envelope really is the wrapper's: the read
    # above is gated on _WRAPPER_ENVELOPE_STAGES, so a stage whose engine could
    # have written the file itself never reaches this suppression.
    classification_pid_alive = (
        pid_alive and not (envelope_valid or failure_envelope_valid)
    )
    classify_state = pipeline.classify_dispatch(
        now=time.time(),
        deadline=deadline,
        pid_alive=classification_pid_alive,
        result_exists=result_exists,
        state=marker_state,
        envelope_valid=envelope_valid or wrapperless_result_complete,
        failure_envelope_valid=failure_envelope_valid,
    )
    return classify_state, identity, pid_alive


_STAGE_IN_FLIGHT_LABELS = pipeline.STAGE_IN_FLIGHT_LABELS


def _scan_marker_identities() -> list[tuple[str, int, str, str, str, bool]]:
    """Scan local worktree markers with item identity and classify state.

    Scans ``pr-*/dispatch.json`` and ``issue-*/dispatch.json`` under
    ``WT_ROOT``. For each marker, performs identity-verified liveness check
    and returns item kind, number, in-flight label, classify state, the
    verified process identity, and whether the recorded process or its group is
    still alive. Fail-closed: any corrupt marker or
    classify crash causes the caller (can_dispatch_now) to return
    ``admission_error``. An unreadable WT_ROOT also raises (fail-closed).

    Liveness and identity together separate an engine the pipeline will still
    act on from one it has given up on (see :func:`_pipeline_actionable`).
    Admission consumes only the first of the two: identity does not change
    whether a slot is spent, but liveness does, because a marker can leave the
    engine-occupied classify states while its process group is still running
    (see :func:`_admission_states`).
    """
    states: list[tuple[str, int, str, str, str, bool]] = []
    wt_dirs: list[tuple[str, str]] = []
    try:
        for entry in os.listdir(WT_ROOT):
            full = os.path.join(WT_ROOT, entry)
            if not os.path.isdir(full):
                continue
            if entry.startswith("pr-"):
                wt_dirs.append(("pr", full))
            elif entry.startswith("issue-"):
                wt_dirs.append(("issue", full))
    except OSError:
        # Fail-closed: cannot read worktree dir → treat as admission error
        raise RuntimeError(f"cannot list worktree root: {WT_ROOT}")

    for kind, wt_dir in wt_dirs:
        marker_path = os.path.join(wt_dir, "dispatch.json")
        if not os.path.isfile(marker_path):
            continue

        try:
            with open(marker_path) as fh:
                marker = json.load(fh)
        except (OSError, json.JSONDecodeError):
            raise RuntimeError(f"corrupt marker: {marker_path}")

        if not isinstance(marker, dict):
            raise RuntimeError(f"corrupt marker: {marker_path}")

        pr_number = marker.get("pr_number")
        if not isinstance(pr_number, int) or isinstance(pr_number, bool):
            raise RuntimeError(f"corrupt marker: {marker_path}")

        # Full structural validation before classifying for admission.
        # A marker with a non-finite deadline (Infinity/NaN) or missing
        # required fields must fail closed — admission must never count
        # an unvalidated marker as "live" and free a slot.
        if not pipeline.valid_marker(marker, expected_pr=pr_number):
            raise RuntimeError(f"corrupt marker: {marker_path}")

        stage = marker.get("stage")
        in_flight_label = _STAGE_IN_FLIGHT_LABELS.get(str(stage))
        if in_flight_label is None:
            raise RuntimeError(f"corrupt marker: {marker_path}")

        state, identity, pid_alive = _classify_marker(
            wt_dir, marker, result_file=None,
        )
        states.append(
            (kind, pr_number, in_flight_label, state, identity, pid_alive),
        )

    return states


def _admission_states(
    marker_states: list[tuple[str, int, str, str, str, bool]],
) -> list[tuple[str, int, str, str, bool]]:
    """Project markers onto the fields admission judges occupancy by.

    Identity is dropped — a slot is spent whoever owns the process — but
    liveness is not. Occupancy must not depend on the classify enum being
    right about whether an engine has stopped: a marker outside
    ``ENGINE_OCCUPIED_STATES`` whose process group is still alive still owes
    the pipeline an archive or a kill, and it used to survive that case only
    because the item's in-flight label was unioned in. Now that the cap counts
    this machine's engines alone, the marker has to carry it.

    That is a second line, not the first. The way a running engine reached
    ``complete`` was by writing its own ``completion.json`` — see
    :data:`_WRAPPER_ENVELOPE_STAGES`, which closes it at the classification
    boundary. This keeps the accounting honest if a future stage, writer, or
    validator relaxation puts a live process behind a terminal state again.

    Note the reason is *not* the one the label-union rule gave. A reviewer
    wrapper cannot
    publish an envelope with a live paid child: it writes the success envelope
    only after ``proc.wait()`` returns, and the startup-failure envelope only
    from an ``except`` whose ``try`` ends at ``Popen``, so no child exists. That
    holds for every version of ``engine_wrapper.py`` since its creation.

    What *is* real on the reviewer side, and is probably what that sentence was
    reaching for, is a read-skew window: ``atomic_write`` publishes the envelope
    while the wrapper itself is still alive, so a scan landing in between reads
    a valid envelope with ``matched`` identity and a live group. It is
    sub-millisecond, it happens on every successful review, and nothing is
    spending through it.
    """
    return [
        (k, n, label, state, alive)
        for k, n, label, state, _identity, alive in marker_states
    ]


def _pipeline_actionable(
    marker_states: list[tuple[str, int, str, str, str, bool]],
) -> set[tuple[str, int]]:
    """Return items the pipeline itself still has a pending action on.

    This is the question cadence must ask before discounting a held item. A
    hold moves *workflow* custody to the operator, but process custody never
    moves, so an engine the pipeline will still touch keeps the loop awake even
    under a hold. What "still touch" means is decided by *liveness*, not by the
    classify state: while the recorded process or its group is alive, the
    pipeline still owes the item a harvest, an archive, or a kill, and it is
    the only party that can perform any of them.

    That covers the state the classify enum hides. A validated envelope forces
    ``classification_pid_alive`` False in :func:`_classify_marker`, so a
    terminal classify state can sit over a group that is still alive — and the
    hold gate will not archive it until death is proven, so the loop must stay
    to do it. Liveness, not the enum, is what says the pipeline still owes the
    item something.

    This predicate *is* the bound the docs read off: an item counts while its
    recorded process or group is alive and — for a ``hung`` marker — only when
    identity proved ``matched``. Every held shape outside that is discounted, so
    an unactionable shape added later needs no new rule here. The ones that
    exist today:

    - ``hung`` with unprovable identity is alive but *not* actionable. That is
      the escalate-and-leave shape: the pipeline may not signal a PID
      it cannot prove, so it has decided never to act on it again.
    - A pre-spawn lease is not alive and never becomes so. It records no PID, so
      there is nothing to harvest or signal, and ``classify_dispatch`` pins it
      at ``launching`` without consulting the deadline. Counting it would keep
      the loop awake permanently.
    - A markerless hold has no marker, so there is no process to reach at all.

    Args:
        marker_states: Output of :func:`_scan_marker_identities`.

    Returns:
        The ``(kind, number)`` items with pipeline work still pending.
    """
    return {
        (kind, number)
        for kind, number, _label, state, identity, engine_alive in marker_states
        if engine_alive and (state != "hung" or identity == "matched")
    }


def can_dispatch_now(
    *,
    max_in_flight: int,
) -> pipeline.AdmissionResult:
    """Determine whether another engine may be launched.

    Iterates local worktree markers and performs identity-verified liveness
    checks. Occupancy is the engines *this* machine launched and can still
    see — see :func:`pipeline.in_flight_refs` for why a shared ``ai:``
    workflow label is not proof of a paid engine. Fail-closed: any single
    corrupt marker or classify crash blocks ALL dispatch for this tick, and
    an unreadable marker root raises rather than reading as "nothing runs".

    In-tick accounting needs no help from the caller: every stage writes its
    lease marker (``state: "launching"``, an engine-occupied state) *before*
    spawning, and aborts the dispatch outright if that write fails — so no
    engine ever runs unmarked. This scan re-reads the marker root on each
    call, so engines launched earlier in the same DISPATCH phase are already
    counted.

    Args:
        max_in_flight: The configured ceiling: the ``max_in_flight`` value
            this tick's startup gate resolved and validated.

    Returns:
        An ``AdmissionResult`` with the admission decision.
    """
    try:
        marker_states = _scan_marker_identities()
    except RuntimeError:
        return pipeline.AdmissionResult(
            can_dispatch=False,
            reason="admission_error",
            in_flight_count=0,
            max_in_flight=max_in_flight,
        )

    try:
        occupied = pipeline.in_flight_refs(_admission_states(marker_states))
    except ValueError:
        return pipeline.AdmissionResult(
            can_dispatch=False,
            reason="admission_error",
            in_flight_count=0,
            max_in_flight=max_in_flight,
        )

    total_in_flight = len(occupied)
    can_disp = pipeline.can_dispatch(
        in_flight=total_in_flight, max_in_flight=max_in_flight
    )

    reason = ""
    if not can_disp:
        reason = "max_in_flight"
    return pipeline.AdmissionResult(
        can_dispatch=can_disp,
        reason=reason,
        in_flight_count=total_in_flight,
        max_in_flight=max_in_flight,
    )


# --- reconcile_engine (Step 8) ---


def _gh_entity(config: pipeline.StageConfig) -> str:
    """Return ``"pr"`` or ``"issue"`` for the stage's GitHub entity type."""
    return "issue" if config.worktree_prefix == "issue" else "pr"


def _observed_hold_label(labels: Sequence[str]) -> str:
    """Return the one workflow hold this item is actually carrying.

    The executor re-reads this exact label before it releases the in-flight
    label, so it has to be a hold the item demonstrably has. Inferring it from
    the two members :data:`pipeline.HOLD_LABELS` happens to hold today would
    name a different label the moment a third is added, and the release would
    then fail its pre-check on every tick — stranding the completion behind a
    permanently pinned admission slot. Deriving it from the intersection keeps
    that impossible by construction.

    ``ai:needs-human-decision`` wins a tie because it is the terminal escalation
    label; the remainder is ordered only to keep the choice deterministic.

    Args:
        labels: The item's complete ``ai:`` label set.

    Returns:
        The observed hold label, or ``""`` when the item carries none.
    """
    held = pipeline.HOLD_LABELS & set(labels)
    if not held:
        return ""
    if pipeline.LABEL_NEEDS_HUMAN in held:
        return pipeline.LABEL_NEEDS_HUMAN
    return sorted(held)[0]


def _held_result_outcome(
    wt_dir: str,
    config: pipeline.StageConfig,
) -> str:
    """Read one completed held result through its stage protocol.

    Args:
        wt_dir: Worktree containing the stage result file.
        config: Stage configuration naming the result file and parser protocol.

    Returns:
        The validated engine outcome, or
        :data:`pipeline.OUTCOME_UNRECOGNIZED` when the payload cannot be read or
        parsed.
    """
    try:
        with open(os.path.join(wt_dir, config.result_file)) as fh:
            result_obj: object = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError):
        result_obj = None
    version = None
    marker = None
    if config.stage in {"review", "fix"}:
        try:
            marker = json.loads(Path(wt_dir, "dispatch.json").read_text())
            version = marker.get("report_protocol_version")
            if version is not None:
                result_obj = pipeline.review_reports.loads(Path(wt_dir, config.result_file).read_text())
        except (OSError, UnicodeError, ValueError, AttributeError):
            return pipeline.OUTCOME_UNRECOGNIZED
    return pipeline.held_completion_outcome(
        config.stage, result_obj, report_protocol_version=version, fix_marker=marker,
    )


def _reconcile_one(
    n: int,
    wt_dir: str,
    config: pipeline.StageConfig,
    tick_log: TickLog | None,
    *,
    attempts: Attempts | None = None,
) -> dict[str, object]:
    """Reconcile a single PR/issue by label + marker.

    Performs the per-item reconciliation loop: lifecycle check, dual-label
    escalation, marker validation, identity-verified liveness, classify,
    and reconcile_dispatch call. Returns the reconcile decision dict.
    Does NOT perform stage-specific side effects (those are the caller's
    responsibility).

    The returned decision includes the ``identity`` key so callers can gate
    ``os.kill()`` on proven identity without re-deriving it. A ``cleanup``
    decision may also carry ``release_label``: the pipeline's own in-flight
    label, to be removed before the scratch is archived. It is set only when
    proven engine death ends a dispatch on an item under a human hold, so the
    freed admission slot is returned without touching the hold. That same
    decision also carries ``held_result_outcome`` when the engine finished, so
    the executor can publish the validated outcome before it releases pipeline
    custody and archives the unharvested result.

    One side effect runs here rather than in the executor: a pending terminal
    intent is completed before any fresh decision is computed (see
    :func:`_resume_terminal_escalation`), returning a ``noop`` decision so the
    executor does not double-apply the transition.
    """
    entity = _gh_entity(config)
    # A pending terminal intent means this dispatch's terminal transition was
    # already decided and published. Complete it (archive, comment, relabel,
    # final move) before any fresh decision — the generic hold branch would
    # otherwise republish this item's completed outcome under a second,
    # contradictory comment attributing the pipeline's own terminal label to a
    # workflow hold.
    if _resume_terminal_escalation(n, entity, wt_dir, attempts, tick_log):
        return {
            "action": "noop", "label": "", "kill": False,
            "identity": "unknown", "pr_number": n,
        }

    # Lifecycle check
    try:
        item_state = gh_text(entity, "view", str(n), "--json", "state", "-q", ".state")
    except subprocess.CalledProcessError:
        if tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=config.stage,
                action="gh_failed",
                reason="lifecycle check: gh view state failed",
            )
        return {"action": "noop", "label": "", "kill": False, "identity": "unknown",
                "pr_number": n}

    if item_state not in {"OPEN", "CLOSED", "MERGED"}:
        if tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=config.stage,
                action="gh_failed",
                reason="lifecycle check returned an unknown state",
                details={"state": item_state},
            )
        return {
            "action": "noop", "label": "", "kill": False,
            "identity": "unknown", "pr_number": n,
        }

    if item_state in {"CLOSED", "MERGED"}:
        # A GitHub human-hold label is the durable custody authority. Resolve
        # it before any terminal-item kill or cleanup: closing or merging an
        # item does not implicitly hand a possibly live engine back to the
        # controller. A missing/corrupt marker under a hold also fails closed.
        try:
            terminal_labels = _pipeline_labels(
                gh_json(entity, "view", str(n), "--json", "labels"),
            )
        except (subprocess.CalledProcessError, ValueError):
            terminal_labels = None
        if terminal_labels is None:
            return {
                "action": "noop", "label": "", "kill": False,
                "identity": "unknown", "pr_number": n,
            }
        terminal_hold_label = _observed_hold_label(terminal_labels)
        terminal_human_hold = bool(terminal_hold_label)
        terminal_marker_path = os.path.join(wt_dir, "dispatch.json")
        if terminal_human_hold and not os.path.isfile(terminal_marker_path):
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=config.stage,
                    action="workflow_hold_retained",
                    reason="terminal markerless scratch remains under a human hold",
                )
            return {
                "action": "leave", "label": "", "kill": False,
                "identity": "markerless", "pr_number": n,
            }
        if terminal_human_hold and os.path.isfile(terminal_marker_path):
            try:
                with open(terminal_marker_path) as fh:
                    held_marker = json.load(fh)
            except (OSError, json.JSONDecodeError):
                held_marker = None
            if not pipeline.validate_marker(
                dispatch=held_marker, pr=n, stage=config.stage,
            ):
                return {
                    "action": "leave", "label": "", "kill": False,
                    "identity": "corrupt", "pr_number": n,
                }
            assert isinstance(held_marker, dict)
            held_state, held_identity, held_alive = _classify_marker(
                wt_dir, held_marker, result_file=config.result_file,
            )
            if not held_alive and held_identity == "dead":
                # Terminal lifecycle does not change the held-death boundary.
                # Return the same decision as the OPEN path so the executor
                # verifies release of the pipeline's in-flight label before
                # archival, propagates either failure, and emits result
                # telemetry only after the archive lands.
                held_result_outcome = (
                    _held_result_outcome(wt_dir, config)
                    if held_state == "complete" else ""
                )
                return {
                    "action": "cleanup", "label": "", "kill": False,
                    "identity": held_identity, "pr_number": n,
                    "release_label": (
                        config.in_flight_label
                        if config.in_flight_label in terminal_labels else ""
                    ),
                    "held_result_outcome": held_result_outcome,
                    "hold_label": terminal_hold_label,
                }
            if held_alive or held_identity != "dead":
                # Process custody is independent of the item's lifecycle, so
                # this applies the same rule as the open path: an overdue
                # engine with proven identity is a runaway and is terminated.
                # Closing or merging the item does not grant it a reprieve —
                # if anything its output is now unusable. Labels and scratch
                # still stay exactly as the operator left them.
                held_killed = False
                if held_state == "hung" and held_identity == "matched":
                    held_killed = _terminate_marker_engine(
                        held_marker, n, tick_log=tick_log, stage=config.stage,
                        reason=(
                            "hung engine on a held terminal item survived SIGKILL"
                        ),
                        dispatch_id=str(held_marker.get("dispatch_id", "")),
                    )
                if tick_log:
                    tick_log.log(
                        pr=n, phase="harvest", stage=config.stage,
                        action=(
                            "hung_engine_killed_under_hold" if held_killed
                            else "workflow_hold_retained"
                        ),
                        reason="terminal item remains under a human hold",
                        dispatch_id=str(held_marker.get("dispatch_id", "")),
                    )
                return {
                    "action": "leave", "label": "", "kill": held_killed,
                    "identity": held_identity, "pr_number": n,
                }

        # Terminal item — kill engine if identity matched, then cleanup.
        # On "unknown" identity, retain custody: do NOT delete the marker
        # (the engine may still be alive and the marker is the only link).
        identity = "unknown"
        killed = False
        _term_dispatch_id = ""
        marker_path = os.path.join(wt_dir, "dispatch.json")
        marker_exists = os.path.isfile(marker_path)
        marker_corrupt = False
        if marker_exists:
            try:
                with open(marker_path) as fh:
                    term_marker = json.load(fh)
            except (OSError, json.JSONDecodeError):
                term_marker = None
                marker_corrupt = True
            if isinstance(term_marker, dict) and pipeline.validate_marker(
                dispatch=term_marker, pr=n, stage=config.stage,
            ):
                _term_did = term_marker.get("dispatch_id")
                _term_dispatch_id = _term_did if isinstance(_term_did, str) else ""
                pid = term_marker.get("pid")
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                    marker_start = term_marker.get("start_time")
                    if isinstance(marker_start, (int, float)):
                        live_start = process_control.get_process_start_time(pid)
                        identity = pipeline.verify_identity(
                            recorded_start=float(marker_start),
                            live_start=live_start,
                        )
                        if identity == "unknown" and not process_control.pid_is_alive(pid):
                            if not _marker_process_group_is_alive(term_marker):
                                identity = "dead"
                        if identity == "matched":
                            pgid = term_marker.get("pgid")
                            if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 0:
                                killed = process_control.terminate_process_group(
                                    pgid, grace_seconds=5, kill_grace_seconds=2,
                                    poll_interval=0.5,
                                )
                            else:
                                killed = process_control.terminate_process(
                                    pid, grace_seconds=5, kill_grace_seconds=2,
                                    poll_interval=0.5,
                                )
                            if not killed and tick_log:
                                tick_log.log(
                                    pr=n, phase="harvest", stage=config.stage,
                                    action="kill_failed",
                                    reason="terminate_process returned False",
                                    dispatch_id=_term_dispatch_id,
                                )
            elif isinstance(term_marker, dict) and pipeline.valid_marker(
                term_marker, expected_pr=n,
            ):
                identity = "sibling_stage"
            else:
                marker_corrupt = True
                if isinstance(term_marker, dict):
                    corrupt_did = term_marker.get("dispatch_id")
                    if isinstance(corrupt_did, str):
                        _term_dispatch_id = corrupt_did
                    corrupt_pid = term_marker.get("pid")
                    corrupt_start = term_marker.get("start_time")
                    if (
                        isinstance(corrupt_pid, int)
                        and not isinstance(corrupt_pid, bool)
                        and corrupt_pid > 0
                        and isinstance(corrupt_start, (int, float))
                        and not isinstance(corrupt_start, bool)
                    ):
                        live_start = process_control.get_process_start_time(corrupt_pid)
                        corrupt_identity = pipeline.verify_identity(
                            recorded_start=float(corrupt_start),
                            live_start=live_start,
                        )
                        if (
                            corrupt_identity == "unknown"
                            and not process_control.pid_is_alive(corrupt_pid)
                        ):
                            if not _marker_process_group_is_alive(term_marker):
                                identity = "dead"
        # Only cleanup on proven (matched) identity.
        # On "mismatched", confirmed PID reuse but can't prove result was
        # harvested — retain scratch for audit (Invariant 4).
        # On "unknown", retain the marker so a future tick can re-identify.
        # Gate cleanup on successful kill: a zombie engine with no marker
        # cannot be tracked or re-killed by a future tick.
        if identity == "matched" and killed:
            if _archive_and_cleanup_scratch(
                wt_root=os.path.dirname(wt_dir),
                wt_dir=wt_dir,
                kind="issue" if entity == "issue" else "pr",
                number=n,
                dispatch_id=_term_dispatch_id or "unknown",
                tick_log=tick_log,
                stage=config.stage,
                phase="harvest",
                failure_reason="retaining terminal scratch after engine kill",
            ):
                if attempts is not None and config.worktree_prefix == "pr":
                    attempts.clear(n)
        elif identity == "dead":
            if _archive_and_cleanup_scratch(
                wt_root=os.path.dirname(wt_dir),
                wt_dir=wt_dir,
                kind="issue" if entity == "issue" else "pr",
                number=n,
                dispatch_id=_term_dispatch_id or "unknown",
                tick_log=tick_log,
                stage=config.stage,
                phase="harvest",
                failure_reason="retaining terminal scratch for dead engine",
            ):
                if attempts is not None and config.worktree_prefix == "pr":
                    attempts.clear(n)
        elif marker_corrupt:
            identity = "corrupt"
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=config.stage,
                    action="corrupt_marker_escalate",
                    reason=(
                        "marker cannot prove engine death; retaining custody "
                        "for operator recovery"
                    ),
                )
        elif identity == "matched" and not killed and tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=config.stage,
                action="cleanup_skipped",
                reason="kill failed, retaining marker for re-kill",
                dispatch_id=_term_dispatch_id,
            )
        elif identity == "mismatched" and tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=config.stage,
                action="mismatched_retain",
                reason="PID reuse confirmed, retaining scratch for audit",
                dispatch_id=_term_dispatch_id,
            )
        elif identity == "sibling_stage":
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=config.stage,
                    action="sibling_stage_retain",
                    reason="marker belongs to another stage, retaining custody",
                )
        elif marker_exists:
            # Unknown identity retains custody: the marker may be the only
            # durable link to a live engine, so do not delete or clear attempts.
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=config.stage,
                    action="unknown_identity_retain",
                    reason="identity unknown, retaining marker for custody",
                    dispatch_id=_term_dispatch_id,
                )
        else:
            # Markerless terminal scratch has no live custody link. Remove git
            # metadata when present; fall back to plain directory cleanup for
            # renamed implementer/fixer scratch that is no longer a worktree.
            identity = "markerless"
            if not _remove_worktree(wt_dir):
                cleanup_scratch(wt_dir)
            if attempts is not None and config.worktree_prefix == "pr":
                attempts.clear(n)
        return {"action": "noop", "label": "", "kill": False, "identity": identity,
                "pr_number": n}

    # Resolve pipeline labels
    try:
        labels_raw = gh_json(entity, "view", str(n), "--json", "labels")
    except (subprocess.CalledProcessError, ValueError):
        if tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=config.stage,
                action="gh_failed",
                reason="label resolution: gh view labels failed",
            )
        return {"action": "noop", "label": "", "kill": False, "identity": "unknown",
                "pr_number": n}

    if not isinstance(labels_raw, dict):
        return {"action": "noop", "label": "", "kill": False, "identity": "unknown",
                "pr_number": n}

    ai_labels = _pipeline_labels(labels_raw)
    if ai_labels is None:
        return {"action": "noop", "label": "", "kill": False,
                "identity": "unknown", "pr_number": n}

    # Marker validation + classify + reconcile
    marker_path = os.path.join(wt_dir, "dispatch.json")
    marker_valid = False
    marker: dict[str, object] | None = None
    classify_state = ""
    identity = "unknown"
    engine_alive = False

    if os.path.isfile(marker_path):
        try:
            with open(marker_path) as fh:
                marker = json.load(fh)
        except (OSError, json.JSONDecodeError):
            marker = None

        marker_valid = pipeline.validate_marker(
            dispatch=marker, pr=n, stage=config.stage,
        )

        if marker_valid and isinstance(marker, dict):
            classify_state, identity, engine_alive = _classify_marker(
                wt_dir, marker, result_file=config.result_file,
            )

    # The marker's recorded pre_label is the authority for custody and stale
    # reverts — the config default is only a fallback. Resolve it before the
    # dual-label decision so GitHub's arbitrary label ordering never chooses
    # custody for us.
    dispatch_pre_label: str = config.pre_label
    if marker_valid and isinstance(marker, dict):
        mp = marker.get("pre_label")
        if isinstance(mp, str):
            dispatch_pre_label = mp

    marker_owned_label = ""
    if config.in_flight_label in ai_labels:
        marker_owned_label = config.in_flight_label
    elif dispatch_pre_label in ai_labels:
        marker_owned_label = dispatch_pre_label

    # A live or completed marker may legitimately coexist with a second label
    # after a partially applied GitHub transition — in every stage, because
    # every stage's harvest routes label transitions through convergence.
    # Retain/harvest through the marker-owned label, then let harvest converge
    # the complete remote set. Exception: the tick never produces ai:blocked
    # and only pairs ai:needs-human-decision with an in-flight label when its
    # own harvest target is that label, so either hold coexisting with pipeline
    # custody is treated as human-set — it outranks a completed marker, and the
    # hold gate below returns before any convergence or harvest can run.
    #
    # "Treated as" rather than "is": a dispatched implementer or fixer runs
    # under the operator's full environment and can reach `gh pr edit`, so it
    # can label its own item mid-run. Nothing here can tell that apart from a
    # human hold — engines share the tick's token, so even the timeline actor
    # matches. Prompts and pr-guidelines.md ("Pipeline label authority") forbid
    # engines from touching ai:* labels for exactly this reason. When one does
    # anyway, the hold still wins and its completed result is not harvested;
    # only the validated outcome is published before local archival. The rule
    # stays fail closed toward the human deliberately: a human racing to stop a
    # bad run must win.
    hold_label = _observed_hold_label(ai_labels)
    human_hold = bool(hold_label)
    if human_hold:
        # A hold moves *workflow* custody to the human: the pipeline may not
        # escalate, replace the hold, advance the stage, or destroy evidence.
        #
        # It does not move *process* custody, which never transfers at all —
        # the operator cannot see the PID, signal it, or inspect it, so a hold
        # is a statement about the work, not an offer to manage a Unix process.
        # The dispatch deadline is therefore still owned by this pipeline, and
        # an overdue engine whose identity is *proven* is a runaway that keeps
        # spending. Terminate it. Doing so advances no decision: the hold and
        # the scratch are untouched, so the operator keeps every piece of
        # evidence and the pending choice stays exactly where they left it.
        #
        # Unprovable identity still escalates-and-leaves: a PID whose
        # start time cannot be read may have been reused, and signalling it
        # could hit an unrelated process.
        if marker_valid and isinstance(marker, dict):
            # A lease is the second way the pipeline can hold no process
            # custody, and it is not reachable by proving death. ``valid_marker``
            # makes the two ownership groups mutually exclusive, so a marker
            # with no ``pid`` carries no process identity at all: there is
            # nothing to probe, nothing to signal, and ``classify_dispatch``
            # pins it at ``launching`` without ever consulting the deadline, so
            # ``identity`` stays ``unknown`` for as long as the file exists.
            # Waiting for proven death here strands the scratch and its
            # admission slot permanently — and the pipeline reaches this state
            # by escalating onto its own hold, so it is self-inflicted.
            no_process_recorded = not _is_positive_marker_pid(marker)
            if (not engine_alive and identity == "dead") or no_process_recorded:
                # Proven death is the release condition. It frees the scratch
                # and the pipeline's own in-flight label — an automation
                # receipt, not the human's writing. The admission slot is
                # freed by destroying the marker, never by removing the label;
                # the receipt is dropped so the item does not sit at a
                # pipeline label no tick will ever clear.
                held_result_outcome = (
                    _held_result_outcome(wt_dir, config)
                    if classify_state == "complete" else ""
                )
                return {
                    "action": "cleanup", "label": "", "kill": False,
                    "identity": identity, "pr_number": n,
                    "release_label": (
                        config.in_flight_label
                        if config.in_flight_label in ai_labels else ""
                    ),
                    # The hold forbids advancing the stage, so carry the
                    # finished engine outcome to the publish-and-archive
                    # executor. The release entry alone is
                    # indistinguishable from reclaiming a slot off an empty
                    # scratch, and the operator deciding the hold needs to
                    # know a usable result is waiting in the archive.
                    "held_result_outcome": held_result_outcome,
                    "hold_label": hold_label,
                }
            killed = False
            if classify_state == "hung" and identity == "matched":
                killed = _terminate_marker_engine(
                    marker, n, tick_log=tick_log, stage=config.stage,
                    reason="hung engine under a human hold survived SIGKILL",
                    dispatch_id=str(marker.get("dispatch_id", "")),
                )
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=config.stage,
                    action=(
                        "hung_engine_killed_under_hold" if killed
                        else "workflow_hold_retained"
                    ),
                    reason=classify_state or "marker state unknown",
                    dispatch_id=str(marker.get("dispatch_id", "")),
                )
            return {
                "action": "leave", "label": "", "kill": killed,
                "identity": identity, "pr_number": n,
            }
        return {
            "action": "leave", "label": "", "kill": False,
            "identity": "corrupt" if os.path.isfile(marker_path) else "unknown",
            "pr_number": n,
        }

    # Every held item returned above, so only hold-free dual labels reach here.
    recoverable_dual_label = (
        bool(marker_owned_label)
        and classify_state in ("live", "hung", "complete")
    )
    # Convergence is a destructive mutation, so it needs the same proof of
    # custody every other destructive path needs: a valid marker for this
    # stage. Without one, a contradictory remote state is not ours to resolve
    # — it is the normal reading of another machine's dispatch caught between
    # its own two label writes, or of a human hold placed on live work. Acting
    # on it would reintroduce the markerless escalation through
    # a second door, ahead of the reconcile_dispatch no-op that exists to stop
    # exactly that.
    _dual_label_needs_escalation = (
        marker_valid
        and len(ai_labels) > 1
        and not recoverable_dual_label
    )
    if _dual_label_needs_escalation:
        if config.strict_dual_label:
            # Single-mutation convergence. The helper refuses *any* pre-existing
            # hold, so this only proceeds because the hold gate above already
            # proved the item was hold-free; if a human sets one in the race
            # between the two reads, convergence returns False and the scratch
            # and marker stay durable for the next tick to retry.
            if _converge_pipeline_labels(
                entity, n, "ai:needs-human-decision",
                tick_log=tick_log, phase="harvest", stage=config.stage,
            ):
                # Terminal label landed: clear the counter before the archive
                # move deletes the marker (see _handle_transient_engine_result).
                if attempts is not None:
                    attempts.clear(n)
                _archive_and_cleanup_scratch(
                    wt_root=os.path.dirname(wt_dir),
                    wt_dir=wt_dir,
                    kind="issue" if entity == "issue" else "pr",
                    number=n,
                    dispatch_id=_read_dispatch_id(wt_dir),
                    tick_log=tick_log,
                    stage=config.stage,
                    phase="harvest",
                    failure_reason=(
                        "retaining scratch after dual-label escalation"
                    ),
                )

        return {"action": "noop", "label": "", "kill": False, "identity": "unknown",
                "pr_number": n}

    current_label = marker_owned_label or (ai_labels[0] if ai_labels else "")

    # Wire the durable attempt counter so stale-revert is bounded
    dispatch_attempts = 0
    if attempts is not None:
        dispatch_attempts = attempts.read(n)

    decision = pipeline.reconcile_dispatch(
        current_label=current_label,
        marker_valid=marker_valid,
        classify_state=classify_state,
        pre_label=dispatch_pre_label,
        in_flight_label=config.in_flight_label,
        dispatch_attempts=dispatch_attempts,
    )
    if (config.stage == "fix" and marker_valid and isinstance(marker, dict)
            and marker.get("report_protocol_version") == pipeline.fixer_reports.VERSION
            and classify_state == "stale"
            and current_label in (config.in_flight_label, dispatch_pre_label)):
        # A dead new fixer with no report may already have pushed. Preserve
        # the existing dead-tree authority, but publish a report failure instead
        # of replaying code work. This is not a successful completion envelope.
        decision = {"action": "report_missing" if identity == "dead" else "leave",
                    "kill": False, "label": ""}

    # A dead wrapper can leave a paid child alive in its recorded process
    # group. That retains custody but cannot prove process identity strongly
    # enough to signal the group after the deadline. Escalate once while the
    # pipeline still owns the label, then keep leaving the marker and scratch
    # in place on later human-held ticks. Once the group actually dies,
    # classification becomes stale and the normal cleanup path can proceed.
    unverified_live_custody = identity == "unknown" and engine_alive
    if decision.get("kill") and unverified_live_custody:
        in_custody = current_label in (
            config.in_flight_label,
            dispatch_pre_label,
        )
        if in_custody:
            decision = {
                "action": "escalate",
                "kill": False,
                "label": pipeline.LABEL_NEEDS_HUMAN,
            }
        else:
            decision = {"action": "leave", "kill": False, "label": ""}

    # Extract dispatch_id from the validated marker so harvest log entries
    # carry the same correlation id as the dispatch launch entry.
    _marker_dispatch_id = ""
    if marker_valid and isinstance(marker, dict):
        _mdid = marker.get("dispatch_id")
        if isinstance(_mdid, str):
            _marker_dispatch_id = _mdid

    if tick_log:
        tick_log.log(
            pr=n, phase="harvest", stage=config.stage,
            action=str(decision.get("action", "")),
            reason=classify_state,
            dispatch_id=_marker_dispatch_id,
            details={
                "classify_state": classify_state,
                "identity": identity,
            },
        )

    # Execute kill for hung engines on proven identity (Invariant 4).
    # reconcile_dispatch returns kill=True for hung processes regardless
    # of custody; the kill must fire so the stuck engine stops spending.
    kill_ok = True
    if decision.get("kill") and identity == "matched":
        if marker_valid and isinstance(marker, dict):
            kill_ok = _terminate_marker_engine(
                marker, n, tick_log=tick_log, stage=config.stage,
                reason="hung engine: terminate_process returned False",
                dispatch_id=_marker_dispatch_id,
            )

    # Downgrade destructive actions when kill failed (zombie guard).
    # A running engine with no marker is untrackable — retain custody.
    if not kill_ok:
        action = str(decision.get("action", ""))
        if action in ("kill_escalate", "kill_cleanup"):
            decision = {"action": "leave", "kill": False, "label": ""}

    # Attach identity so callers can gate os.kill() on proven identity
    result: dict[str, object] = dict(decision)
    result["identity"] = identity
    result["pr_number"] = n
    return result


def reconcile_engine(
    config: pipeline.StageConfig,
    tick_log: TickLog | None,
    target: tuple[str, int] | None,
    *,
    wt_root: str | None = None,
    attempts: Attempts | None = None,
) -> tuple[list[dict[str, object]], bool]:
    """Run the HARVEST reconciliation loop for one pipeline stage.

    Builds the reconcile set from the union of authoritative label queries
    and local worktree markers, then iterates each item through the
    per-item reconciliation logic.

    Args:
        config: Parameterisation for this stage.
        tick_log: Tick log handle, or ``None`` to skip logging.
        target: Optional ``(type, number)`` tuple to restrict to a single
            item (``("pr", 42)`` or ``("issue", 42)``).
        wt_root: Worktree root directory, or ``None`` to resolve
            :data:`WT_ROOT` at call time.
        attempts: Durable attempt counter, or ``None`` to skip counting.

    Returns:
        A tuple of ``(decisions, reconcile_ok)``. ``reconcile_ok`` is False
        when the authoritative label enumeration failed and
        ``tick_abort_on_enum_failure`` is False (caller should gate DISPATCH).
    """
    if wt_root is None:
        wt_root = WT_ROOT
    reconcile_ok = True
    entity = _gh_entity(config)

    # Build reconcile set: authoritative label query
    try:
        label_items = gh_text(
            entity, "list",
            "--label", config.in_flight_label,
            "--state", "open",
            "--json", "number",
            "-q", ".[].number",
        )
    except subprocess.CalledProcessError:
        if config.tick_abort_on_enum_failure:
            raise
        # Enumeration failed but we continue with local markers only.
        # The caller must gate DISPATCH on this failure.
        label_items = ""
        reconcile_ok = False

    recon_nums: set[int] = set()
    for line in label_items.splitlines():
        line = line.strip()
        if line.isdigit():
            recon_nums.add(int(line))

    # Add local marker PRs (union with authoritative labels)
    try:
        entries = os.listdir(wt_root)
    except OSError:
        entries = []
    for entry in entries:
        full = os.path.join(wt_root, entry)
        if not os.path.isdir(full):
            continue
        if entry.startswith(f"{config.worktree_prefix}-"):
            marker_path = os.path.join(full, "dispatch.json")
            if os.path.isfile(marker_path):
                try:
                    n = int(entry.split("-", 1)[1])
                    recon_nums.add(n)
                except ValueError:
                    pass

    # Targeted mode filtering
    if target is not None:
        target_type, target_num = target
        expected_prefix = "pr" if target_type == "pr" else "issue"
        if config.worktree_prefix == expected_prefix:
            if target_num in recon_nums:
                recon_nums = {target_num}
            else:
                recon_nums = set()
        else:
            recon_nums = set()

    results: list[dict[str, object]] = []
    for n in sorted(recon_nums):
        wt_dir = os.path.join(wt_root, f"{config.worktree_prefix}-{n}")
        decision = _reconcile_one(n, wt_dir, config, tick_log, attempts=attempts)
        results.append(decision)

    return results, reconcile_ok


# --- Stage configs ---

_REVIEWER_CONFIG = pipeline.StageConfig(
    stage="review",
    worktree_prefix="pr",
    attempts_key_prefix="",
    result_file="review.json",
    in_flight_label=pipeline.LABEL_REVIEWING,
    pre_label=pipeline.LABEL_IMPLEMENTED,
    strict_dual_label=True,
    tick_abort_on_enum_failure=True,
)

_IMPLEMENTER_CONFIG = pipeline.StageConfig(
    stage="implement",
    worktree_prefix="issue",
    attempts_key_prefix="issue-",
    result_file="result.json",
    in_flight_label=pipeline.LABEL_IMPLEMENTING,
    pre_label=pipeline.LABEL_AVAILABLE,
    strict_dual_label=True,
    tick_abort_on_enum_failure=False,
)

_FIXER_CONFIG = pipeline.StageConfig(
    stage="fix",
    worktree_prefix="pr",
    attempts_key_prefix="",
    result_file="result.json",
    in_flight_label=pipeline.LABEL_FIXING,
    pre_label=pipeline.LABEL_CHANGES_REQUESTED,
    strict_dual_label=True,
    tick_abort_on_enum_failure=False,
)


_STAGE_RESULT_FILES: dict[str, str] = {
    _REVIEWER_CONFIG.stage: _REVIEWER_CONFIG.result_file,
    _IMPLEMENTER_CONFIG.stage: _IMPLEMENTER_CONFIG.result_file,
    _FIXER_CONFIG.stage: _FIXER_CONFIG.result_file,
}


def _stage_result_file(stage: str) -> str:
    """Return the filename a stage's engine writes its result envelope to.

    The terminal completions are shared across stages and receive a stage name
    rather than a :class:`pipeline.StageConfig`, so the file they must find
    before archiving evidence has to be derived from it. The reviewer writes
    ``review.json`` while the other two write ``result.json``; hardcoding the
    majority spelling made a shared helper silently discard the reviewer's
    archive and refuse its transition.

    Args:
        stage: One of the pipeline stage names.

    Returns:
        The stage's result filename. An unknown stage falls back to
        ``result.json`` — the completions only ever receive a stage bound to
        the closed set, so this is the value that keeps an unreachable branch
        from being a crash rather than a decision.
    """
    return _STAGE_RESULT_FILES.get(stage, "result.json")


# --- HARVEST phases (Step 9) ---


def _hold_still_observed(
    entity: str,
    number: int,
    hold_label: str,
    *,
    dispatch_id: str,
    tick_log: TickLog | None,
    stage: str,
    reason: str,
) -> bool:
    """Point-read the item's labels and require the decision's exact hold.

    The reconcile decision's hold observation can be a tick old by the time
    the executor acts on it. Publishing under, or finalizing custody from, a
    hold that has since been removed would misattribute the pipeline's own
    outcome to a human hold that is no longer there — so the mutation-time
    paths re-read the live label set. Reads that fail or return junk fail
    closed: custody is retained for the next tick to re-decide.

    Args:
        entity: ``"pr"`` or ``"issue"`` — the GitHub entity type.
        number: The PR or issue number.
        hold_label: The exact workflow hold the decision observed.
        dispatch_id: Dispatch correlation id for the refusal log entry.
        tick_log: Tick log handle, or ``None``.
        stage: Pipeline stage for the log entry.
        reason: Operator-facing reason naming the gate that refused.

    Returns:
        ``True`` when the exact hold is still on the item, ``False`` when it
        is gone or unreadable.
    """
    try:
        labels = _pipeline_labels(
            gh_json(entity, "view", str(number), "--json", "labels"),
        )
    except (subprocess.CalledProcessError, ValueError):
        labels = None
    if labels is None or hold_label not in labels:
        if tick_log:
            tick_log.log(
                pr=number, phase="harvest", stage=stage,
                action="label_release_failed",
                reason=reason,
                dispatch_id=dispatch_id,
                details={"required_hold_label": hold_label},
            )
        return False
    return True


def _hold_survived_custody_move(
    entity: str,
    number: int,
    hold_label: str,
    *,
    released_label: str,
    dispatch_id: str,
    tick_log: TickLog | None,
    stage: str,
) -> bool:
    """Require the decision's hold one last time, undoing a stale release.

    This is :func:`_hold_still_observed` with the one asymmetry that matters
    after a release has landed. Before the release, refusing costs nothing —
    no remote state has changed. After it, the pipeline's own in-flight label
    is already gone, so a bare refusal can leave the item carrying no workflow
    label: the operator lifted the hold in the same window, and the next tick
    then finds a marker outside pipeline custody and cleans it up silently.
    The removal is therefore handed to :func:`_restore_released_in_flight_label`
    on the same terms the release itself uses.

    Args:
        entity: ``"pr"`` or ``"issue"`` — the GitHub entity type.
        number: The PR or issue number.
        hold_label: The exact workflow hold the decision observed.
        released_label: The in-flight label this publication already removed,
            or empty when no release was needed.
        dispatch_id: Dispatch correlation id for the refusal log entry.
        tick_log: Tick log handle, or ``None``.
        stage: Pipeline stage for the log entry.

    Returns:
        ``True`` when the exact hold is still on the item, ``False`` otherwise
        — with the released label restored where that was needed.
    """
    try:
        labels = _pipeline_labels(
            gh_json(entity, "view", str(number), "--json", "labels"),
        )
    except (subprocess.CalledProcessError, ValueError):
        labels = None
    if labels is not None and hold_label in labels:
        return True
    restored: bool | None = None
    if released_label:
        restored = _restore_released_in_flight_label(
            entity, number, released_label, observed=labels,
        )
    if tick_log:
        reason = "observed workflow hold changed before local custody move"
        if released_label:
            reason += f"; {_restore_outcome_note(restored)}"
        tick_log.log(
            pr=number, phase="harvest", stage=stage,
            action="label_release_failed",
            reason=reason,
            dispatch_id=dispatch_id,
            details={
                "required_hold_label": hold_label,
                "release_label": released_label,
                "in_flight_restored": restored,
            },
        )
    return False


def _publish_held_result_and_archive(
    *,
    decision: Mapping[str, object],
    config: pipeline.StageConfig,
    wt_root: str,
    entity: str,
    number: int,
    tick_log: TickLog | None,
    attempts: Attempts | None,
) -> bool:
    """Publish one held completion before releasing and archiving custody.

    Args:
        decision: Reconcile decision carrying the held outcome and labels.
        config: Stage configuration for result and worktree paths.
        wt_root: Controller-owned worktree root.
        entity: ``"pr"`` or ``"issue"``.
        number: PR or issue number.
        tick_log: Tick log handle, or ``None``.
        attempts: Durable attempt counter, or ``None``.

    Returns:
        ``True`` once the comment, remote release, and archive all land;
        ``False`` with source custody retained when any gate fails.
    """
    outcome = str(decision.get("held_result_outcome", ""))
    hold_label = str(
        decision.get("hold_label", pipeline.LABEL_NEEDS_HUMAN)
    )
    release_label = str(decision.get("release_label", ""))
    wt_dir = os.path.join(wt_root, f"{config.worktree_prefix}-{number}")
    dispatch_id = _read_dispatch_id(wt_dir)
    # The decision's hold observation is from reconciliation time. Revalidate
    # the exact hold before anything is published: a hold removed in that
    # window means the item is no longer held, and a comment claiming the hold
    # "remains authoritative" would misstate the live state. Refusals retain
    # every source of custody for the next tick to re-decide the item fresh.
    if not _hold_still_observed(
        entity, number, hold_label,
        dispatch_id=dispatch_id, tick_log=tick_log, stage=config.stage,
        reason="observed workflow hold changed before publish",
    ):
        return False
    archive_dir = _archive_harvest_evidence(
        wt_root=wt_root,
        wt_dir=wt_dir,
        kind="issue" if entity == "issue" else "pr",
        number=number,
        dispatch_id=dispatch_id,
        tick_log=tick_log,
        stage=config.stage,
        phase="harvest",
        required_files=(config.result_file,),
        reuse_existing=True,
    )
    if not archive_dir:
        if tick_log:
            tick_log.log(
                pr=number, phase="harvest", stage=config.stage,
                action="archive_failed",
                reason="retaining held completion until evidence is prepared",
                dispatch_id=dispatch_id,
            )
        return False
    posted = _post_harvest_comment(
        entity=entity,
        number=number,
        wt_root=wt_root,
        dispatch_id=dispatch_id,
        comment_body=pipeline.render_held_completion_comment(
            stage=config.stage,
            outcome=outcome,
        ),
        kind=_ESCALATION_COMMENT_KIND,
        target_label=hold_label,
    )
    if not posted:
        _discard_prepared_archive(archive_dir)
        if tick_log:
            tick_log.log(
                pr=number, phase="harvest", stage=config.stage,
                action="held_result_comment_failed",
                reason="retaining custody until the held result is published",
                dispatch_id=dispatch_id,
                details={"outcome": outcome},
            )
        return False
    if release_label and not _release_in_flight_label(
        entity, number, release_label,
        tick_log=tick_log, phase="harvest", stage=config.stage,
        required_hold_label=hold_label,
    ):
        # The post landed and promised the operator that the output is
        # retained in the harvest-evidence archive. Keeping that promise true
        # takes precedence over the one-directory leak the discard avoids: the
        # comment is live and must not point at nothing. Source scratch stays
        # for the next tick, which either completes the release or re-decides
        # the item fresh.
        return False
    # Revalidate the hold again before the terminal local custody move —
    # whether or not an in-flight label needed removal. With an empty
    # ``release_label`` the release path above never runs, so without this
    # check a departed hold would still be finalized as a "hold won" sequence
    # from a stale observation. Past the release the refusal is no longer
    # free, so the helper restores the label it removed.
    if not _hold_survived_custody_move(
        entity, number, hold_label,
        released_label=release_label,
        dispatch_id=dispatch_id, tick_log=tick_log, stage=config.stage,
    ):
        return False

    # The remote transition landed. Clear attempts before the archive move
    # deletes the marker, otherwise a crash in that window strands a stale
    # counter with no dispatch artifact left to reconcile it.
    if attempts is not None:
        attempts.clear(number)
    if not _finalize_terminal_archive(
        wt_dir=wt_dir,
        archive_dir=archive_dir,
        number=number,
        stage=config.stage,
        dispatch_id=dispatch_id,
        reason="retaining scratch after publishing a held completion",
        tick_log=tick_log,
    ):
        return False
    cleanup_scratch(wt_dir)
    _discard_comment_receipts(wt_root, entity, number)
    if tick_log:
        tick_log.log(
            pr=number, phase="harvest", stage=config.stage,
            action="held_result_published",
            reason="workflow hold won over a completed engine result",
            dispatch_id=dispatch_id,
            details={"outcome": outcome},
        )
    return True


def _execute_reconcile_actions(
    decisions: list[dict[str, object]],
    config: pipeline.StageConfig,
    wt_root: str,
    tick_log: TickLog | None = None,
    attempts: Attempts | None = None,
) -> bool:
    """Execute label transitions, kills, and cleanups from reconcile decisions.

    Returns True if all remote transitions succeeded (reconcile_ok).
    A failed label edit sets reconcile_ok = False so DISPATCH is gated.
    """
    reconcile_ok = True
    entity = _gh_entity(config)
    for decision in decisions:
        action = str(decision.get("action", ""))
        label = str(decision.get("label", ""))
        n = decision.get("pr_number")

        # Kill is handled in _reconcile_one for both closed/merged and OPEN
        # hung items (Invariant 4: kill only on proven identity).

        held_result_outcome = str(decision.get("held_result_outcome", ""))
        if (
            action in ("cleanup", "kill_cleanup")
            and held_result_outcome
            and isinstance(n, int)
        ):
            if not _publish_held_result_and_archive(
                decision=decision,
                config=config,
                wt_root=wt_root,
                entity=entity,
                number=n,
                tick_log=tick_log,
                attempts=attempts,
            ):
                reconcile_ok = False
            continue

        # Label transitions (escalate, revert)
        if action in ("escalate", "revert", "kill_escalate"):
            if label and isinstance(n, int):
                if label == pipeline.LABEL_NEEDS_HUMAN:
                    # Escalations converge the full remote set in one mutation.
                    # The helper refuses any pre-existing human hold, so this
                    # can never replace operator custody.
                    if not _converge_pipeline_labels(
                        entity, n, label,
                        tick_log=tick_log, phase="harvest", stage=config.stage,
                    ):
                        reconcile_ok = False
                        continue
                else:
                    # Reverts restore an automation label and must not touch
                    # any coexisting hold — the plain transition keeps it.
                    try:
                        _relabel_with_context(
                            entity, n, config.in_flight_label, label,
                            tick_log=tick_log, phase="harvest", stage=config.stage,
                        )
                    except subprocess.CalledProcessError:
                        reconcile_ok = False
                        continue
                # Update attempt counter
                if attempts is not None:
                    if action == "revert":
                        attempts.incr(n)
                    else:
                        attempts.clear(n)

        # A held item whose engine is provably dead releases the pipeline's
        # own in-flight label. Do it BEFORE the archive/delete below: if the
        # remote removal does not land, the marker is the only thing that can
        # re-prove death next tick, so it has to outlive the failure.
        #
        # The hold is what justifies the removal, and the decision observed it
        # a tick ago, so it is revalidated at mutation time on the same terms
        # the held-result path uses: this branch is the same decision without
        # a completed result to publish first, and it ends in the same place —
        # a removal that outlives its justification strips the item's last
        # workflow label, and the cleanup below then destroys the marker that
        # could have routed it.
        release_label = str(decision.get("release_label", ""))
        if (
            action in ("cleanup", "kill_cleanup")
            and release_label
            and isinstance(n, int)
        ):
            hold_label = str(decision.get("hold_label", ""))
            held_wt_dir = os.path.join(
                wt_root, f"{config.worktree_prefix}-{n}",
            )
            if not _release_in_flight_label(
                entity, n, release_label,
                tick_log=tick_log, phase="harvest", stage=config.stage,
                required_hold_label=hold_label,
            ):
                reconcile_ok = False
                continue
            # Read the hold once more before the cleanup below destroys the
            # marker. The release's own read-back proves the hold outlived the
            # removal, not that it outlives the local custody move that
            # follows — and past the release a bare refusal is no longer free,
            # because the in-flight label is already gone. Same gate, same
            # restore, as the completed-result path: the harm they prevent is
            # identical, and only the presence of a result to publish first
            # separates the two branches.
            if not _hold_survived_custody_move(
                entity, n, hold_label,
                released_label=release_label,
                dispatch_id=_read_dispatch_id(held_wt_dir),
                tick_log=tick_log, stage=config.stage,
            ):
                reconcile_ok = False
                continue

        # Scratch cleanup for cleanup/kill_cleanup actions.
        # Gate on identity: skip cleanup on "mismatched" to retain the
        # audit trail when PID reuse is confirmed (matches _reconcile_one).
        if action in ("cleanup", "kill_cleanup") and isinstance(n, int):
            identity = str(decision.get("identity", "unknown"))
            if identity != "mismatched":
                wt_dir = os.path.join(wt_root, f"{config.worktree_prefix}-{n}")
                dispatch_id = _read_dispatch_id(wt_dir)
                if not _archive_and_cleanup_scratch(
                    wt_root=wt_root,
                    wt_dir=wt_dir,
                    kind="issue" if entity == "issue" else "pr",
                    number=n,
                    dispatch_id=dispatch_id,
                    tick_log=tick_log,
                    stage=config.stage,
                    phase="harvest",
                    failure_reason="retaining scratch before cleanup",
                ):
                    reconcile_ok = False
                elif attempts is not None:
                    # Cleanup ends dispatch custody: clear the durable attempt
                    # counter so a stale count (e.g. from a landed-but-
                    # unverified transition that left custody) cannot carry
                    # into the item's next dispatch cycle and hit the cap early.
                    attempts.clear(n)

    return reconcile_ok


def harvest_reviewers(
    tick_log: TickLog | None = None,
    target: tuple[str, int] | None = None,
    *,
    wt_root: str | None = None,
    max_iterations: int = pipeline.MAX_ITERATIONS,
) -> bool:
    """Harvest finished reviewer engines.

    Calls ``reconcile_engine`` with the reviewer ``StageConfig``, then
    processes decisions that require stage-specific side effects: label
    transitions, kills, cleanups, and (for harvest actions) review posting.

    Args:
        tick_log: Tick log handle, or ``None``.
        target: Optional ``(type, number)`` to restrict to a single item.
        wt_root: Worktree root directory, or ``None`` to resolve
            :data:`WT_ROOT` at call time.
        max_iterations: Review/fix rounds before ``ai:max-iterations``. The
            tick passes its validated value; the default serves direct callers.

    Returns:
        ``True`` if reconciliation succeeded (reconcile_ok), ``False`` if
        enumeration or convergence failed and DISPATCH should be skipped.
    """
    if wt_root is None:
        wt_root = WT_ROOT
    attempts = Attempts(wt_root, _REVIEWER_CONFIG.attempts_key_prefix)
    try:
        decisions, enum_ok = reconcile_engine(
            _REVIEWER_CONFIG, tick_log, target,
            wt_root=wt_root, attempts=attempts,
        )
    except subprocess.CalledProcessError:
        return False

    # Execute label transitions, kills, cleanups
    transition_ok = _execute_reconcile_actions(
        decisions, _REVIEWER_CONFIG, wt_root, tick_log, attempts,
    )

    # For harvest actions: post review comment, handle drift, count iterations.
    failed_ok = True
    for decision in decisions:
        action = str(decision.get("action", ""))
        if action == "harvest":
            _harvest_one_reviewer(
                decision, wt_root, tick_log, attempts,
                max_iterations=max_iterations,
            )
        elif action == "fail":
            n = decision.get("pr_number")
            if isinstance(n, int):
                failed_ok = _harvest_failed_reviewer(
                    n, wt_root, tick_log=tick_log, attempts=attempts,
                ) and failed_ok

    return enum_ok and transition_ok and failed_ok


def _harvest_failed_reviewer(
    number: int,
    wt_root: str,
    *,
    tick_log: TickLog | None = None,
    attempts: Attempts | None = None,
) -> bool:
    """Archive a wrapper-confirmed reviewer failure and escalate terminally."""
    wt_dir = os.path.join(wt_root, f"pr-{number}")
    dispatch_id = _read_dispatch_id(wt_dir)
    archive_dir = _archive_harvest_evidence(
        wt_root=wt_root,
        wt_dir=wt_dir,
        kind="pr",
        number=number,
        dispatch_id=dispatch_id,
        tick_log=tick_log,
        stage=_REVIEWER_CONFIG.stage,
        required_files=("completion.json",),
    )
    if not archive_dir:
        if tick_log:
            tick_log.log(
                pr=number, phase="harvest", stage=_REVIEWER_CONFIG.stage,
                action="archive_failed",
                reason="retaining failed reviewer scratch and in-flight label",
                dispatch_id=dispatch_id,
            )
        return False

    if not _converge_pipeline_labels(
        "pr", number, pipeline.LABEL_NEEDS_HUMAN,
        tick_log=tick_log, phase="harvest", stage=_REVIEWER_CONFIG.stage,
    ):
        _discard_prepared_archive(archive_dir)
        if tick_log:
            tick_log.log(
                pr=number, phase="harvest", stage=_REVIEWER_CONFIG.stage,
                action="label_convergence_failed",
                reason="retaining failed reviewer scratch for retry",
                dispatch_id=dispatch_id,
            )
        return False

    # Terminal label landed: clear the counter before the archive move
    # deletes the marker (see _handle_transient_engine_result).
    if attempts is not None:
        attempts.clear(number)
    if not _finalize_harvest_archive(
        wt_dir,
        archive_dir,
        number=number,
        stage=_REVIEWER_CONFIG.stage,
        phase="harvest",
        dispatch_id=dispatch_id,
        tick_log=tick_log,
    ):
        if tick_log:
            tick_log.log(
                pr=number, phase="harvest", stage=_REVIEWER_CONFIG.stage,
                action="archive_failed",
                reason="terminal relabel landed; retaining failed reviewer scratch",
                dispatch_id=dispatch_id,
                details={"archive_dir": archive_dir},
            )
        return False
    _log_reviewer_skill_observation(
        tick_log=tick_log,
        number=number,
        wt_dir=os.path.join(archive_dir, "scratch"),
        dispatch_id=dispatch_id,
    )
    if tick_log:
        tick_log.log(
            pr=number, phase="harvest", stage=_REVIEWER_CONFIG.stage,
            action="escalate",
            reason="wrapper-confirmed reviewer failure",
            dispatch_id=dispatch_id,
            details={
                "archive_dir": archive_dir,
                "from_label": _REVIEWER_CONFIG.in_flight_label,
                "to_label": pipeline.LABEL_NEEDS_HUMAN,
                "failure_kind": "wrapper_failure_envelope",
            },
        )
    return True


def _read_dispatch_id(wt_dir: str) -> str:
    """Read a dispatch id for archive correlation, falling back safely."""
    marker_path = os.path.join(wt_dir, "dispatch.json")
    try:
        with open(marker_path) as fh:
            marker = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return "unknown"
    if isinstance(marker, dict):
        dispatch_id = marker.get("dispatch_id")
        if isinstance(dispatch_id, str) and dispatch_id:
            return dispatch_id
    return "unknown"


# Controller-owned record of a terminal transition the cap has already decided
# and published, written before the relabel that would complete it. The cap's
# comment states that `ai:needs-human-decision` supersedes whatever the run
# itself concluded; if the relabel then fails, the decision has to outlive the
# tick. Without it the next tick re-enters ordinary harvest logic and a
# recovered relabel lands the run's own (now superseded) label under the cap's
# comment — two contradictory terminal claims for one dispatch.
#
# It lives beside `.receipts` under WT_ROOT for the same reason: the item's
# scratch is a checked-out worktree of the PR branch and therefore
# author-controlled, and a forged intent could pin an item at
# `ai:needs-human-decision`.
_TERMINAL_INTENT_DIR = ".terminal"


def _terminal_intent_path(wt_root: str, entity: str, number: int) -> str:
    """Return the controller-owned path recording a pending terminal transition.

    Args:
        wt_root: The worktree root (controller-owned state lives here).
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.

    Returns:
        Absolute path to the terminal-intent file.
    """
    return os.path.join(
        wt_root, _TERMINAL_INTENT_DIR, f"{entity}-{number}.json",
    )


def _record_terminal_intent(
    wt_root: str, entity: str, number: int, dispatch_id: str, stage: str,
    *, kind: str = "cap", **extra: object,
) -> bool:
    """Record that a terminal transition was decided for this dispatch.

    Written before the transition it describes, so a failed or interrupted
    relabel or archive move leaves the decision recoverable. The write is a
    required gate: a caller that cannot durably record its decision must not
    start the transition, because a transition that proceeds without a record
    and then fails its archive move leaves the next tick with nothing to
    resume — it would re-enter ordinary harvest and republish the completed
    outcome as a held completion contradicting the already-posted escalation.

    Args:
        wt_root: The worktree root holding controller-owned state.
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        dispatch_id: The dispatch whose harvest decided the transition.
        stage: The pipeline stage that decided it.
        kind: Which terminal transition was decided: ``"cap"`` (the attempt
            cap), ``"transient_exhausted"``, or ``"escalate"`` (a result-driven
            stage escalation).
        extra: Kind-specific payload the resume needs to reproduce the
            original action (comment body, failure class, labels).

    Returns:
        ``True`` when the decision was durably recorded; ``False`` when the
        record could not be written and the caller must not proceed.
    """
    record: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "label": pipeline.LABEL_NEEDS_HUMAN,
        "stage": stage,
        "kind": kind,
        **extra,
    }
    # Writer and reader agree by construction: a record the resume would later
    # reject is not durable at all, and writing one would trade a gate the
    # caller handles now for a transition that can never be completed.
    if _parse_terminal_intent(record) is None:
        return False
    path = _terminal_intent_path(wt_root, entity, number)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


class TerminalIntent(NamedTuple):
    """A validated record of a terminal transition awaiting completion.

    Attributes:
        dispatch_id: The dispatch whose harvest decided the transition.
        stage: The deciding pipeline stage; always a known stage.
        kind: The decided transition; always in :data:`_TERMINAL_INTENT_KINDS`.
        target_label: The label the transition converges to; always in
            :data:`_TERMINAL_LABELS`.
        relabeled: Whether the converged relabel is recorded as landed.
        failures: The failure count the cap comment reports (``cap``).
        failure_class: The engine failure classification behind the retries
            (``transient_exhausted``).
        attempt: The attempt the exhaustion ends on (``transient_exhausted``).
        comment_body: The stage comment the resume republishes (``escalate``;
            empty for silent stages).
    """

    dispatch_id: str
    stage: str
    kind: str
    target_label: str
    relabeled: bool
    failures: int | None = None
    failure_class: str = ""
    attempt: int = 0
    comment_body: str = ""


# The closed set of terminal transitions an intent may record, and the only
# phase one may carry beyond "decided, relabel not yet landed".
_TERMINAL_INTENT_KINDS = frozenset({"cap", "transient_exhausted", "escalate"})
_TERMINAL_INTENT_RELABELED = "relabeled"

# The exact key set the previous writer produced. It recorded no kind, and the
# attempt cap was the only transition it wrote — so this shape is a cap intent
# with an underived count, not corruption. It is matched exactly rather than by
# an absent-field default: a kind-less record carrying anything else has no
# provenance, and defaulting it would be the guess this schema exists to refuse.
_LEGACY_TERMINAL_INTENT_KEYS = frozenset({"dispatch_id", "label", "stage"})


def _parse_terminal_intent(raw: object) -> TerminalIntent | None:
    """Validate a persisted terminal-intent record against its closed schema.

    The intent is the recovery owner for a transition whose comment is already
    live on GitHub, and the resume reproduces that transition from it —
    including the label it converges to and the comment body whose hash keys
    the replay-suppressing receipt. Every field is therefore required, typed,
    and bounded to its closed set: a defaulted or coerced value would let a
    corrupted record drive a remote mutation nobody decided.

    The one exception is :data:`_LEGACY_TERMINAL_INTENT_KEYS`, the exact shape
    the previous writer produced. Rejecting it would retain custody forever on
    an item whose cap comment is already published — no fresh harvest runs
    while an intent is pending, so nothing else could ever retire it.

    Args:
        raw: The parsed JSON value read from the intent path.

    Returns:
        The validated record, or ``None`` when any field is missing, mistyped,
        or outside its closed set.
    """
    if not isinstance(raw, dict):
        return None
    record = cast("dict[str, object]", raw)
    dispatch_id = record.get("dispatch_id")
    stage = record.get("stage")
    kind = record.get("kind")
    phase = record.get("phase", "")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return None
    if not isinstance(stage, str) or stage not in pipeline.STAGE_IN_FLIGHT_LABELS:
        return None
    # Every kind records the escalation terminal as its decided label; the
    # escalate kind additionally names the label it converges to.
    if record.get("label") != pipeline.LABEL_NEEDS_HUMAN:
        return None
    if phase not in ("", _TERMINAL_INTENT_RELABELED):
        return None
    relabeled = phase == _TERMINAL_INTENT_RELABELED
    if kind is None and frozenset(record) - {"phase"} == _LEGACY_TERMINAL_INTENT_KEYS:
        # Pre-upgrade cap intent. Its count is absent rather than lost: the
        # completion derives it from the attempt counter, which this
        # transition has not yet cleared. ``phase`` is excused from the key
        # match because *this* code adds it once the relabel lands — a record
        # rejected on the shape its own resume gave it would strand the item
        # one step after the acceptance saved it.
        return TerminalIntent(
            dispatch_id=dispatch_id,
            stage=stage,
            kind="cap",
            target_label=pipeline.LABEL_NEEDS_HUMAN,
            relabeled=relabeled,
        )
    if not isinstance(kind, str) or kind not in _TERMINAL_INTENT_KINDS:
        return None
    common = {
        "dispatch_id": dispatch_id,
        "stage": stage,
        "kind": kind,
        "relabeled": relabeled,
    }
    if kind == "cap":
        failures = record.get("failures")
        if not _is_positive_int(failures):
            return None
        return TerminalIntent(
            target_label=pipeline.LABEL_NEEDS_HUMAN,
            failures=cast("int", failures),
            **common,
        )
    if kind == "transient_exhausted":
        failure_class = record.get("failure_class")
        attempt = record.get("attempt")
        if not isinstance(failure_class, str) or not failure_class:
            return None
        if not _is_positive_int(attempt):
            return None
        return TerminalIntent(
            target_label=pipeline.LABEL_NEEDS_HUMAN,
            failure_class=failure_class,
            attempt=cast("int", attempt),
            **common,
        )
    harvest_label = record.get("harvest_label")
    comment_body = record.get("comment_body")
    if not isinstance(harvest_label, str) or harvest_label not in _TERMINAL_LABELS:
        return None
    if not isinstance(comment_body, str):
        return None
    return TerminalIntent(
        target_label=harvest_label, comment_body=comment_body, **common,
    )


def _is_positive_int(value: object) -> bool:
    """Return whether a persisted value is a genuine positive integer.

    ``bool`` is an ``int`` subclass, so a serialized ``true`` would otherwise
    pass as the count ``1``.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _read_terminal_intent_record(
    wt_root: str, entity: str, number: int,
) -> tuple[bool, object]:
    """Read the persisted terminal-intent JSON as one snapshot.

    One read: re-reading the same path for a second field can disagree with
    the first read, and a resume that fell back to a default on that
    disagreement would execute a transition nobody decided. The record is
    never retired here — the resume path decides retirement only once the
    transition it describes has completed or been proven stale.

    Args:
        wt_root: The worktree root holding controller-owned state.
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.

    Returns:
        ``(False, None)`` when no intent is recorded, else ``(True, raw)`` with
        the parsed JSON value — ``None`` when the file exists but could not be
        read or decoded, which :func:`_parse_terminal_intent` rejects.
    """
    try:
        with open(_terminal_intent_path(wt_root, entity, number)) as fh:
            raw: object = json.load(fh)
    except FileNotFoundError:
        return False, None
    except (OSError, json.JSONDecodeError):
        return True, None
    return True, raw


def _terminal_intent_is_stale(raw: object, dispatch_id: str) -> bool:
    """Return whether a persisted intent provably names a different dispatch.

    Checked before the record is validated, and only against a provable marker
    id. An intent whose dispatch was superseded can never be resumed no matter
    how well formed it is, and one written before the current schema is
    exactly that case on an item that has since been re-dispatched — leaving it
    on disk would deadlock the item forever.

    Args:
        raw: The parsed JSON value read from the intent path.
        dispatch_id: The marker's dispatch id, or ``"unknown"`` when the marker
            could not be read.

    Returns:
        ``True`` only when both ids are known and they differ.
    """
    if dispatch_id == "unknown" or not isinstance(raw, dict):
        return False
    recorded = cast("dict[str, object]", raw).get("dispatch_id")
    return isinstance(recorded, str) and bool(recorded) and recorded != dispatch_id


def _clear_terminal_intent(wt_root: str, entity: str, number: int) -> None:
    """Retire a terminal intent once its transition has landed.

    Args:
        wt_root: The worktree root holding controller-owned state.
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
    """
    try:
        os.unlink(_terminal_intent_path(wt_root, entity, number))
    except OSError:
        pass


def _update_terminal_intent(
    wt_root: str, entity: str, number: int, **fields: object,
) -> bool:
    """Merge fields into a recorded terminal intent, atomically.

    Read-modify-write against the same path the resume reads, so the merged
    record replaces the old one in a single ``os.replace``. Callers own what a
    failure means; nothing here decides that.

    Args:
        wt_root: The worktree root holding controller-owned state.
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        **fields: Keys to set on the record.

    Returns:
        ``True`` when the merged record landed on disk.
    """
    path = _terminal_intent_path(wt_root, entity, number)
    try:
        with open(path) as fh:
            intent = json.load(fh)
        if not isinstance(intent, dict):
            return False
        record = cast("dict[str, object]", intent)
        record.update(fields)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)
    except (OSError, ValueError):
        return False
    return True


def _mark_terminal_intent_relabeled(
    wt_root: str,
    entity: str,
    number: int,
    *,
    tick_log: TickLog | None = None,
    stage: str = "",
    dispatch_id: str = "",
) -> None:
    """Record that the terminal relabel landed, before the archive move starts.

    Written between the converged relabel and the final archive move, so a
    crash in that window resumes with the relabel known-landed: the resume
    then skips convergence — and with it any chance of re-applying the
    terminal label over a later human transition — and finishes local custody
    only.

    A failed write is not fatal, because it is an accelerator rather than the
    proof: :func:`_terminal_relabel_state` classifies the live label set on
    its own and refuses the mutation for anything that is not provably the
    transition's source. It is still logged — a persistent failure means every
    resume pays an extra point-read to reach the same conclusion.

    Args:
        wt_root: The worktree root holding controller-owned state.
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        tick_log: Tick log handle, or ``None``.
        stage: Pipeline stage for the log entry.
        dispatch_id: Dispatch correlation id for the log entry.
    """
    if not _update_terminal_intent(
        wt_root, entity, number, phase=_TERMINAL_INTENT_RELABELED,
    ) and tick_log:
        tick_log.log(
            pr=number, phase="harvest", stage=stage,
            action="terminal_intent_phase_unrecorded",
            reason=(
                "relabel landed but the phase could not be recorded; "
                "the resume falls back to a live label read"
            ),
            dispatch_id=dispatch_id,
        )


# How the remote label set relates to a terminal transition whose intent
# carries no phase record. The four decided states are mutually exclusive and
# each permits exactly one remote mutation — a converge, a pure release, or
# none at all.
_RELABEL_PENDING = "pending"  # source still present, no hold: the relabel never ran
_RELABEL_LANDED = "landed"  # remote is exactly the decided target
_RELABEL_HELD = "held"  # source still present, but a hold owns the label
_RELABEL_AMBIGUOUS = "ambiguous"  # neither: a later actor owns the label
_RELABEL_UNREADABLE = "unreadable"  # label state could not be read at all


class TerminalRelabelState(NamedTuple):
    """How a pending terminal relabel stands against the live label set.

    Attributes:
        state: One of the ``_RELABEL_*`` classifications.
        hold_label: The exact workflow hold observed in the same read, set only
            for :data:`_RELABEL_HELD`. It comes from that read rather than a
            later one because it is what the release revalidates against, and a
            second read could name a different hold than the one classified.
    """

    state: str
    hold_label: str = ""


def _terminal_relabel_state(
    entity: str, number: int, *, target_label: str, source_label: str,
) -> TerminalRelabelState:
    """Classify a pending terminal relabel against the live label set.

    The resume path uses this when the intent carries no phase record, which
    is exactly the crash window between the converged relabel and the phase
    write. Only three states are provable from one point-read, and each admits
    exactly one mutation:

    * the remote sits exactly at the decided target — the relabel landed, and
      converging again could only re-apply it;
    * the transition's source in-flight label is still present with no hold —
      convergence removes it, so its presence proves the relabel never ran;
    * the source is still present *beside a hold* — convergence refuses a hold
      by design (see :func:`_converge_pipeline_labels`), so this transition can
      never converge while it stands. Retrying it every tick would leave the
      in-flight label on the item forever, and no tick clears a markerless
      in-flight label, so the pipeline drops its own in-flight receipt
      instead and leaves the human's hold to own the item. That release costs
      and reclaims no admission slot — the cap counts markers — it stops the
      label set from claiming a pipeline custody that has already moved.

    Anything else is ambiguous: the item is at neither end of the transition,
    so some later actor moved it and automation must not convert that into a
    re-application of a superseded decision.

    Args:
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        target_label: The decided terminal label.
        source_label: The stage's in-flight label, which the transition
            converges away from.

    Returns:
        The classification and, for :data:`_RELABEL_HELD`, the observed hold.
    """
    try:
        labels = _pipeline_labels(
            gh_json(entity, "view", str(number), "--json", "labels"),
        )
    except (subprocess.CalledProcessError, ValueError):
        return TerminalRelabelState(_RELABEL_UNREADABLE)
    if labels is None:
        return TerminalRelabelState(_RELABEL_UNREADABLE)
    expected = [target_label] if target_label else []
    if sorted(set(labels)) == expected:
        # Checked first, so a target that is itself a hold reads as this
        # transition's own landed relabel rather than as a held source.
        return TerminalRelabelState(_RELABEL_LANDED)
    if source_label and source_label in labels:
        hold_label = _observed_hold_label(labels)
        if hold_label:
            return TerminalRelabelState(_RELABEL_HELD, hold_label)
        return TerminalRelabelState(_RELABEL_PENDING)
    return TerminalRelabelState(_RELABEL_AMBIGUOUS)


def _land_terminal_transition(
    *,
    entity: str,
    number: int,
    wt_root: str,
    stage: str,
    dispatch_id: str,
    target_label: str,
    tick_log: TickLog | None,
    skip_relabel: bool,
    release_label: str,
    hold_label: str,
) -> bool:
    """Apply the one remote label mutation a terminal transition is owed.

    Shared by all three terminal completions so the choice between converging,
    releasing, and doing nothing is made in one place: a transition that
    converged on one path and released on another would be two contracts
    wearing one name.

    Args:
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        wt_root: The worktree root holding controller-owned state.
        stage: The pipeline stage that decided the transition.
        dispatch_id: The dispatch being completed.
        target_label: The terminal label the transition converges to.
        tick_log: Optional tick log.
        skip_relabel: True when the resume proved no mutation may run — the
            relabel already landed, or a later actor owns the label.
        release_label: The stage's in-flight label to release instead of
            converging, set only when a hold owns the item's label.
        hold_label: The hold that justifies that release; revalidated at
            mutation time by :func:`_release_in_flight_label`.

    Returns:
        ``True`` when the transition may proceed to its local custody move,
        ``False`` when the mutation failed and custody must be retained.
    """
    if skip_relabel:
        return True
    if release_label:
        # The hold is the workflow authority, so the only mutation left is
        # dropping the pipeline's own in-flight receipt. No phase is recorded:
        # the release removes the source label, so the next read classifies as
        # landed or ambiguous on its own and never re-attempts a mutation.
        if not _release_in_flight_label(
            entity, number, release_label,
            tick_log=tick_log, phase="harvest", stage=stage,
            required_hold_label=hold_label,
        ):
            return False
        # Same second gate the two cleanup releases carry, for the same
        # reason: the read-back proves the hold outlived the removal, not the
        # archive move the caller makes next — and that move destroys the
        # marker. A hold lifted in this window would leave the item with no
        # workflow label under a comment naming a terminal label it never
        # received. Restoring the in-flight label is also what lets the next
        # tick finish the job properly: with the hold gone the state reads as
        # pending again, and the transition converges as originally decided.
        return _hold_survived_custody_move(
            entity, number, hold_label,
            released_label=release_label,
            dispatch_id=dispatch_id, tick_log=tick_log, stage=stage,
        )
    if not _converge_pipeline_labels(
        entity, number, target_label,
        tick_log=tick_log, phase="harvest", stage=stage,
    ):
        return False
    # The relabel landed: record it in the intent before the archive move, so
    # a crash in this window resumes with the relabel known-landed and never
    # re-converges (which could re-apply the terminal label over a later human
    # transition).
    _mark_terminal_intent_relabeled(
        wt_root, entity, number,
        tick_log=tick_log, stage=stage, dispatch_id=dispatch_id,
    )
    return True


def _resume_terminal_escalation(
    n: int,
    entity: str,
    wt_dir: str,
    attempts: Attempts | None,
    tick_log: TickLog | None,
) -> bool:
    """Complete a terminal transition this dispatch already decided.

    Called before any harvester computes a fresh decision — including the
    generic hold branch of :func:`_reconcile_one`. A pending intent means the
    outcome for this dispatch is already settled and announced on GitHub, so
    recomputing it could only produce a label or comment that contradicts the
    published one. The recorded kind selects the original action: the attempt
    cap, transient exhaustion, or a result-driven stage escalation.

    Args:
        n: The PR or issue number.
        entity: ``"pr"`` or ``"issue"``.
        wt_dir: The item's worktree directory.
        attempts: Durable attempt counter, or ``None``.
        tick_log: Optional tick log.

    Returns:
        ``True`` when a pending intent was found and this tick belongs to it,
        or when a recorded intent could not be resolved — either way the caller
        must not run its ordinary harvest. ``False`` when there is nothing to
        resume.
    """
    if attempts is None:
        return False
    wt_root = os.path.dirname(wt_dir)
    present, raw = _read_terminal_intent_record(wt_root, entity, n)
    if not present:
        return False
    dispatch_id = _read_dispatch_id(wt_dir)
    if _terminal_intent_is_stale(raw, dispatch_id):
        # A different dispatch: the item was re-dispatched after this
        # transition was decided, so the record can never be resumed.
        _clear_terminal_intent(wt_root, entity, n)
        return False
    intent = _parse_terminal_intent(raw)
    if intent is None:
        # Unreadable or malformed controller state. The transition it records
        # has already published a comment, so ordinary harvest must not run —
        # but nothing here proves *which* transition, so nothing is executed
        # and nothing is retired. The record stays for operator inspection.
        if tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage="",
                action="terminal_intent_unresolved",
                reason=(
                    "terminal intent unreadable or malformed; "
                    "retaining custody for inspection"
                ),
                dispatch_id=dispatch_id,
            )
        return True
    if dispatch_id == "unknown":
        # The marker could not be read, so this intent can be proven neither
        # to belong to the live dispatch nor to be stale. Retiring it on an
        # unprovable read would destroy the recovery owner for a published
        # transition; retain it and re-decide once the marker reads again.
        if tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=intent.stage,
                action="terminal_intent_unresolved",
                reason=(
                    "dispatch identity unprovable; "
                    "retaining the terminal intent"
                ),
                dispatch_id=intent.dispatch_id,
                details={"kind": intent.kind},
            )
        return True
    if intent.kind == "cap" and intent.failures is None:
        # First sight of a pre-upgrade record: rewrite it in the current
        # schema before anything else runs. The count has to be pinned here
        # because the completion derives it from a counter it clears before
        # the archive move — a re-entry after a failed move would otherwise
        # derive a different number and publish a second cap comment stating a
        # failure count that never happened. Writing `kind` with it is what
        # ends the record's legacy status; a failed write simply leaves the
        # record as it was, and the next tick tries again.
        #
        # The pin is best-effort against one window it cannot see through: the
        # pre-upgrade writer cleared the counter two statements before it
        # cleared the intent, so a record left behind from inside that window
        # reads 0 here and pins 1. The legacy shape stores no count, so the
        # original is unrecoverable and 1 is the only value available. What the
        # pin does guarantee is that the number stops moving — every later
        # re-entry publishes the same one instead of a different one per tick.
        pinned = attempts.read(n) + 1
        if _update_terminal_intent(
            wt_root, entity, n, kind="cap", failures=pinned,
        ):
            intent = intent._replace(failures=pinned)
    stage = intent.stage
    source_label = pipeline.STAGE_IN_FLIGHT_LABELS[stage]
    # The relabel is only re-attempted from a state that proves it never ran.
    # The intent's phase record is the cheap proof it already landed; without
    # one, the live label set is classified, and anything that is neither the
    # source nor the target belongs to a later actor.
    if intent.relabeled:
        relabel = TerminalRelabelState(_RELABEL_LANDED)
    else:
        relabel = _terminal_relabel_state(
            entity, n,
            target_label=intent.target_label,
            source_label=source_label,
        )
    relabel_state = relabel.state
    if relabel_state == _RELABEL_UNREADABLE:
        # No proof either way. Retain the intent and every source of custody;
        # returning True keeps ordinary harvest from computing a decision that
        # would contradict the already-published one.
        if tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=stage,
                action="terminal_intent_unresolved",
                reason="label state unreadable; retaining the terminal intent",
                dispatch_id=dispatch_id,
                details={
                    "kind": intent.kind, "target_label": intent.target_label,
                },
            )
        return True
    if relabel_state == _RELABEL_AMBIGUOUS and tick_log:
        tick_log.log(
            pr=n, phase="harvest", stage=stage,
            action="terminal_relabel_ambiguous",
            reason=(
                "remote is at neither the transition's source nor its target; "
                "finishing local custody without touching the labels"
            ),
            dispatch_id=dispatch_id,
            details={"kind": intent.kind, "target_label": intent.target_label},
        )
    if relabel_state == _RELABEL_HELD and tick_log:
        tick_log.log(
            pr=n, phase="harvest", stage=stage,
            action="terminal_relabel_held",
            reason=(
                "a workflow hold owns the label this transition wanted; "
                "releasing only the pipeline's own in-flight label"
            ),
            dispatch_id=dispatch_id,
            details={
                "kind": intent.kind,
                "target_label": intent.target_label,
                "release_label": source_label,
                "hold_label": relabel.hold_label,
            },
        )
    # Both LANDED and AMBIGUOUS forbid the mutation: the first because it
    # already happened, the second because someone else now owns the label.
    # HELD permits one — the release, which the completions apply in place of
    # the converge the hold makes impossible.
    skip_relabel = relabel_state in (_RELABEL_LANDED, _RELABEL_AMBIGUOUS)
    release_label = source_label if relabel_state == _RELABEL_HELD else ""
    hold_label = relabel.hold_label
    if intent.kind == "transient_exhausted":
        _complete_transient_exhaustion(
            n=n,
            entity=entity,
            wt_dir=wt_dir,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            stage=stage,
            failure_class=intent.failure_class,
            attempt=intent.attempt,
            attempts=attempts,
            tick_log=tick_log,
            skip_relabel=skip_relabel,
            release_label=release_label,
            hold_label=hold_label,
        )
    elif intent.kind == "escalate":
        _complete_escalate_transition(
            n=n,
            entity=entity,
            wt_dir=wt_dir,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            stage=stage,
            harvest_label=intent.target_label,
            comment_body=intent.comment_body,
            attempts=attempts,
            tick_log=tick_log,
            skip_relabel=skip_relabel,
            release_label=release_label,
            hold_label=hold_label,
        )
    else:
        _complete_terminal_escalation(
            n=n,
            entity=entity,
            wt_dir=wt_dir,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            stage=stage,
            attempts=attempts,
            tick_log=tick_log,
            failures=intent.failures,
            skip_relabel=skip_relabel,
            release_label=release_label,
            hold_label=hold_label,
        )
    return True


def _complete_terminal_escalation(
    *,
    n: int,
    entity: str,
    wt_dir: str,
    wt_root: str,
    dispatch_id: str,
    stage: str,
    attempts: Attempts,
    tick_log: TickLog | None,
    failures: int | None = None,
    skip_relabel: bool = False,
    release_label: str = "",
    hold_label: str = "",
) -> None:
    """Archive, publish, and land the cap's terminal transition.

    Every step is idempotent, so a tick that fails partway can be re-entered by
    :func:`_resume_terminal_escalation` until the archive move lands: the
    archive is re-prepared, the comment is suppressed by its receipt, and the
    relabel converges to the same target.

    Args:
        n: The PR or issue number.
        entity: ``"pr"`` or ``"issue"``.
        wt_dir: The item's worktree directory.
        wt_root: The worktree root holding controller-owned state.
        dispatch_id: The dispatch being escalated.
        stage: The pipeline stage that tripped the cap.
        attempts: Durable attempt counter.
        tick_log: Optional tick log.
        failures: The failure count the cap comment reports. ``None`` reads
            the counter as ``attempts.read(n) + 1``; the resume path passes
            the recorded count so the republished comment body — and therefore
            the receipt's decision key — matches the original.
        skip_relabel: True when the resume path has proven the terminal
            relabel must not be attempted — it already landed (intent phase or
            remote point-read), or a later actor owns the label. The
            transition then finishes local custody without converging.
        release_label: The stage's in-flight label to release in place of the
            converge, set by the resume when a hold owns the item's label.
        hold_label: The hold justifying that release.
    """
    archive_dir = _archive_harvest_evidence(
        wt_root=wt_root,
        wt_dir=wt_dir,
        kind="issue" if entity == "issue" else "pr",
        number=n,
        dispatch_id=dispatch_id,
        tick_log=tick_log,
        stage=stage,
        required_files=(_stage_result_file(stage),),
        reuse_existing=True,
    )
    if not archive_dir:
        if tick_log:
            tick_log.log(
                pr=n,
                phase="harvest",
                stage=stage,
                action="archive_failed",
                reason="retaining scratch and in-flight label",
                dispatch_id=dispatch_id,
            )
        return
    # Publish the escalation before the label moves. This transition is
    # remote-visible and terminal, and the run's own comment (if any) states
    # the outcome the harvest wanted rather than the label it is about to
    # get — leaving that unexplained is the audit gap the harvest comment
    # exists to close.
    #
    # Unlike every other harvest post, a failure here does NOT block the
    # relabel. The cap exists precisely because this item's side effects keep
    # failing; gating the terminal label on one more remote write would pin
    # the admission slot forever, which is the outcome the cap prevents. The
    # label is the load-bearing signal, the comment is the explanation, and
    # a terminal label with a missing explanation beats a stalled pipeline.
    if failures is None:
        # The counter is read before it is incremented, so the harvest that
        # trips the cap is itself an uncounted failure.
        failures = attempts.read(n) + 1
    posted = _post_harvest_comment(
        entity=entity,
        number=n,
        wt_root=wt_root,
        dispatch_id=dispatch_id,
        comment_body=pipeline.render_cap_escalation_comment(
            stage=stage, failures=failures,
        ),
        kind=_CAP_COMMENT_KIND,
        target_label=pipeline.LABEL_NEEDS_HUMAN,
    )
    if not posted and tick_log:
        tick_log.log(
            pr=n,
            phase="harvest",
            stage=stage,
            action="escalation_comment_failed",
            reason="escalating to the terminal label without an explanation",
            dispatch_id=dispatch_id,
        )
    if not _land_terminal_transition(
        entity=entity,
        number=n,
        wt_root=wt_root,
        stage=stage,
        dispatch_id=dispatch_id,
        target_label=pipeline.LABEL_NEEDS_HUMAN,
        tick_log=tick_log,
        skip_relabel=skip_relabel,
        release_label=release_label,
        hold_label=hold_label,
    ):
        # The cap can inherit an archive referenced by an earlier report,
        # including an unreceipted post. Retain it with the intent until the
        # relabel or hold release recovers; retries reuse this same directory.
        return
    # Terminal label landed: clear the counter before the archive move
    # deletes the marker (see _handle_transient_engine_result).
    attempts.clear(n)
    if not _finalize_terminal_archive(
        wt_dir=wt_dir,
        archive_dir=archive_dir,
        number=n,
        stage=stage,
        dispatch_id=dispatch_id,
        reason="retaining scratch after attempt-cap escalation",
        tick_log=tick_log,
    ):
        # The recorded intent and the comment receipt stay on disk: a failed
        # move resumes this same escalation next tick, and the receipt keeps
        # the retry from republishing the explanation. Clearing them here
        # would let the next tick's generic hold branch republish the
        # completed outcome as a held result under the pipeline's own terminal
        # label, contradicting the cap comment that is already live.
        return
    _discard_comment_receipts(wt_root, entity, n)
    _clear_terminal_intent(wt_root, entity, n)
    cleanup_scratch(wt_dir)


def _complete_transient_exhaustion(
    *,
    n: int,
    entity: str,
    wt_dir: str,
    wt_root: str,
    dispatch_id: str,
    stage: str,
    failure_class: str,
    attempt: int | None,
    attempts: Attempts | None,
    tick_log: TickLog | None,
    skip_relabel: bool = False,
    release_label: str = "",
    hold_label: str = "",
) -> bool:
    """Complete the transient-exhaustion terminal transition for one dispatch.

    Reached by the exhaustion branch of :func:`_handle_transient_engine_result`
    and re-entered by :func:`_resume_terminal_escalation` after a failed
    archive move. Every step is idempotent: the archive is re-prepared, the
    comment is suppressed by its receipt, and the relabel converges to the
    same target.

    Args:
        n: The PR or issue number.
        entity: ``"pr"`` or ``"issue"``.
        wt_dir: The item's worktree directory.
        wt_root: The worktree root holding controller-owned state.
        dispatch_id: The dispatch being escalated.
        stage: The pipeline stage that tripped the exhaustion.
        failure_class: The engine failure classification behind the retries.
        attempt: The attempt number the exhaustion ends on, or ``None`` when
            the recorded intent predates the payload.
        attempts: Durable attempt counter, or ``None``.
        tick_log: Optional tick log.
        skip_relabel: True when the resume path has proven the terminal
            relabel must not be attempted — it already landed (intent phase or
            remote point-read), or a later actor owns the label. The
            transition then finishes local custody without converging.
        release_label: The stage's in-flight label to release in place of the
            converge, set by the resume when a hold owns the item's label.
        hold_label: The hold justifying that release.

    Returns:
        ``True`` once the comment, relabel, and final archive move all land;
        ``False`` with the recorded intent still pending and custody retained.
    """
    archive_dir = _archive_harvest_evidence(
        wt_root=wt_root,
        wt_dir=wt_dir,
        kind=entity,
        number=n,
        dispatch_id=dispatch_id,
        tick_log=tick_log,
        stage=stage,
        required_files=(_stage_result_file(stage),),
        reuse_existing=True,
    )
    if not archive_dir:
        if tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=stage,
                action="archive_failed",
                reason="retaining scratch and in-flight label",
                dispatch_id=dispatch_id,
            )
        return False
    if not _post_harvest_comment(
        entity=entity,
        number=n,
        wt_root=wt_root,
        dispatch_id=dispatch_id,
        comment_body=pipeline.render_transient_escalation_comment(
            stage=stage,
            outcome=pipeline.OUTCOME_TRANSIENT_EXHAUSTED,
            failure_class=failure_class,
            attempts=attempt,
            evidence_archived=True,
        ),
        kind=_ESCALATION_COMMENT_KIND,
        target_label=pipeline.LABEL_NEEDS_HUMAN,
    ):
        _discard_prepared_archive(archive_dir)
        _bound_failed_harvest(
            n, entity, _STAGE_IN_FLIGHT_LABELS.get(stage, ""), wt_dir,
            attempts, tick_log, stage,
        )
        return False
    if not _land_terminal_transition(
        entity=entity,
        number=n,
        wt_root=wt_root,
        stage=stage,
        dispatch_id=dispatch_id,
        target_label=pipeline.LABEL_NEEDS_HUMAN,
        tick_log=tick_log,
        skip_relabel=skip_relabel,
        release_label=release_label,
        hold_label=hold_label,
    ):
        _discard_prepared_archive(archive_dir)
        return False
    # Terminal label landed: clear the counter before the archive move
    # deletes the marker (see _handle_transient_engine_result).
    if attempts is not None:
        attempts.clear(n)
    if not _finalize_terminal_archive(
        wt_dir=wt_dir,
        archive_dir=archive_dir,
        number=n,
        stage=stage,
        dispatch_id=dispatch_id,
        reason="retaining scratch after transient exhaustion escalation",
        tick_log=tick_log,
    ):
        # The recorded intent and the comment receipt stay on disk: a failed
        # move resumes this same exhaustion next tick, and the receipt keeps
        # the retry from republishing the explanation.
        return False
    _discard_comment_receipts(wt_root, entity, n)
    _clear_terminal_intent(wt_root, entity, n)
    if tick_log:
        tick_log.log(
            pr=n, phase="harvest", stage=stage,
            action="transient_exhausted",
            reason=failure_class,
            dispatch_id=dispatch_id,
            details={
                "attempt": attempt,
                "failure_class": failure_class,
            },
        )
    return True


def _complete_escalate_transition(
    *,
    n: int,
    entity: str,
    wt_dir: str,
    wt_root: str,
    dispatch_id: str,
    stage: str,
    harvest_label: str,
    comment_body: str,
    attempts: Attempts | None,
    tick_log: TickLog | None,
    skip_relabel: bool = False,
    release_label: str = "",
    hold_label: str = "",
) -> bool:
    """Complete a result-driven stage escalation for one dispatch.

    Shared by the implementer, fixer, and reviewer escalate branches, and
    re-entered by :func:`_resume_terminal_escalation` after a
    failed archive move. Every step is idempotent: the archive is re-prepared,
    a posted comment is suppressed by its receipt, and the relabel converges
    to the same target.

    Args:
        n: The PR or issue number.
        entity: ``"pr"`` or ``"issue"``.
        wt_dir: The item's worktree directory.
        wt_root: The worktree root holding controller-owned state.
        dispatch_id: The dispatch being escalated.
        stage: The pipeline stage.
        harvest_label: The terminal label the harvest decided.
        comment_body: The rendered stage comment; empty for silent stages.
        attempts: Durable attempt counter, or ``None``.
        tick_log: Optional tick log.
        skip_relabel: True when the resume path has proven the terminal
            relabel must not be attempted — it already landed (intent phase or
            remote point-read), or a later actor owns the label. The
            transition then finishes local custody without converging.
        release_label: The stage's in-flight label to release in place of the
            converge, set by the resume when a hold owns the item's label.
        hold_label: The hold justifying that release.

    Returns:
        ``True`` once the archive, (optional) comment, relabel, and final move
        all land; ``False`` with the recorded intent still pending and custody
        retained.
    """
    archive_dir = _archive_harvest_evidence(
        wt_root=wt_root,
        wt_dir=wt_dir,
        kind="issue" if entity == "issue" else "pr",
        number=n,
        dispatch_id=dispatch_id,
        tick_log=tick_log,
        stage=stage,
        required_files=(_stage_result_file(stage),),
        reuse_existing=True,
    )
    if not archive_dir:
        if tick_log:
            tick_log.log(
                pr=n, phase="harvest", stage=stage,
                action="archive_failed",
                reason="retaining scratch and in-flight label",
                dispatch_id=dispatch_id,
            )
        return False
    if comment_body and not _post_harvest_comment(
        entity=entity,
        number=n,
        wt_root=wt_root,
        dispatch_id=dispatch_id,
        comment_body=comment_body,
        kind=_FIX_COMMENT_KIND,
        target_label=harvest_label,
    ):
        # A previous post may have landed before its receipt was saved. Keep
        # its evidence path even when this attempt fails; retries reuse it.
        _bound_failed_harvest(
            n, entity, _STAGE_IN_FLIGHT_LABELS.get(stage, ""), wt_dir,
            attempts, tick_log, stage,
        )
        return False
    if not _land_terminal_transition(
        entity=entity,
        number=n,
        wt_root=wt_root,
        stage=stage,
        dispatch_id=dispatch_id,
        target_label=harvest_label,
        tick_log=tick_log,
        skip_relabel=skip_relabel,
        release_label=release_label,
        hold_label=hold_label,
    ):
        # A published report can point at this archive. Keep that path readable
        # while relabel or hold release is retried; reuse_existing above keeps
        # one prepared archive per dispatch. Silent escalations publish no path.
        if not comment_body:
            _discard_prepared_archive(archive_dir)
        return False
    # Terminal label landed: clear the counter before the archive move
    # deletes the marker (see _handle_transient_engine_result).
    if attempts is not None:
        attempts.clear(n)
    if not _finalize_terminal_archive(
        wt_dir=wt_dir,
        archive_dir=archive_dir,
        number=n,
        stage=stage,
        dispatch_id=dispatch_id,
        reason="retaining scratch after stage escalation",
        tick_log=tick_log,
    ):
        # The recorded intent and any comment receipt stay on disk:
        # a failed move resumes this same escalation next tick instead of
        # letting the generic hold branch republish the completed outcome as a
        # held result under the pipeline's own terminal label.
        return False
    if comment_body:
        _discard_comment_receipts(wt_root, entity, n)
    _clear_terminal_intent(wt_root, entity, n)
    cleanup_scratch(wt_dir)
    return True


def _bound_failed_harvest(
    n: int,
    entity: str,
    in_flight_label: str,
    wt_dir: str,
    attempts: Attempts | None,
    tick_log: TickLog | None = None,
    stage: str = "",
) -> None:
    """Bound retries of a failing harvest side effect.

    A permanently failing side effect (``gh pr create`` against a branch
    that already has an open PR, a review comment exceeding GitHub's size
    limit) would otherwise retry every tick forever while the item's retained
    marker pins an admission slot — three such items halt all dispatch. The
    item's in-flight label is retained alongside it and costs nothing; the
    marker is what spends. Below the cap, bump the durable attempt counter
    and retain the marker for retry; at the cap, escalate to
    ``ai:needs-human-decision`` and clean up. When the escalation relabel
    itself fails, everything is retained and the next tick retries the
    escalation.

    Args:
        n: The PR or issue number.
        entity: ``"pr"`` or ``"issue"``.
        in_flight_label: The stage's in-flight label, removed on escalation.
        wt_dir: The item's worktree directory.
        attempts: Durable attempt counter, or ``None`` to skip bounding.
    """
    if attempts is None:
        return
    if attempts.read(n) >= pipeline.MAX_DISPATCH_ATTEMPTS:
        dispatch_id = _read_dispatch_id(wt_dir)
        wt_root = os.path.dirname(wt_dir)
        # Record the decision before anything remote is attempted, so a failed
        # or interrupted relabel is resumed next tick rather than re-decided.
        # The failure count is captured with it: the resume republishes the
        # exact comment body, and the counter will have been cleared by then,
        # so a recomputed count would change the receipt's decision key and
        # defeat the suppression. The record is a required gate: without it a
        # failed archive move would resume as a held-completion replay.
        if not _record_terminal_intent(
            wt_root, entity, n, dispatch_id, stage,
            kind="cap",
            failures=attempts.read(n) + 1,
        ):
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=stage,
                    action="terminal_intent_failed",
                    reason=(
                        "cap decision not durably recorded; "
                        "transition not started"
                    ),
                    dispatch_id=dispatch_id,
                )
            return
        _complete_terminal_escalation(
            n=n,
            entity=entity,
            wt_dir=wt_dir,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            stage=stage,
            attempts=attempts,
            tick_log=tick_log,
        )
    else:
        attempts.incr(n)


def _harvest_one_reviewer(
    decision: dict[str, object],
    wt_root: str,
    tick_log: TickLog | None = None,
    attempts: Attempts | None = None,
    *,
    max_iterations: int = pipeline.MAX_ITERATIONS,
) -> None:
    """Process a completed reviewer harvest: read review, detect drift, post
    comment, flip label.

    Reads ``review.json`` and ``dispatch.json`` from the PR's worktree,
    point-reads the live base ref name + head OID (retarget/head-drift
    authority), resolves the current base tip and comments, calls
    ``pipeline.harvest_review``, posts the rendered comment (when
    ``post_comment`` is True), and transitions the PR label.

    On any ``gh``/``git`` read failure, the function returns without side effects
    so the marker survives for the next tick. Comment posting is the gate for
    relabel: if ``post_comment`` is True and the ``gh pr comment`` call fails,
    the label is NOT flipped and the marker is retained.
    """
    n = decision.get("pr_number")
    if not isinstance(n, int):
        return

    wt_dir = os.path.join(wt_root, f"pr-{n}")
    # See _harvest_one_fixer: a published cap escalation owns the outcome.
    if _resume_terminal_escalation(n, "pr", wt_dir, attempts, tick_log):
        return

    dispatch_path = os.path.join(wt_dir, "dispatch.json")

    # Read dispatch marker for provenance fields
    try:
        with open(dispatch_path) as fh:
            marker = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(marker, dict):
        return

    base_ref = marker.get("base_ref")
    pinned_head = marker.get("head_sha")
    mp_pre_label = marker.get("pre_label")
    # Two-SHA model: base_tip_sha is the base-branch tip at dispatch time —
    # the freshness anchor drift detection compares (did the base advance
    # during the review?). base_sha is the merge-base the reviewer's diff was
    # anchored to — the audit/idempotency truth for what was actually
    # reviewed. Markers written before the split carry only base_sha, whose
    # value was the tip and the diff anchor at once, so both roles fall back
    # to it.
    _base_tip = marker.get("base_tip_sha")
    _marker_base_sha = marker.get("base_sha")
    pinned_base = (
        _base_tip if isinstance(_base_tip, str)
        else _marker_base_sha
    )
    if not isinstance(base_ref, str) or not isinstance(pinned_base, str):
        return
    diff_base_sha: str = (
        _marker_base_sha if isinstance(_marker_base_sha, str) else ""
    )

    pre_label: str = (
        mp_pre_label if isinstance(mp_pre_label, str)
        else _REVIEWER_CONFIG.pre_label
    )
    pinned_head_str: str = pinned_head if isinstance(pinned_head, str) else ""

    # Read review.json BEFORE network calls — fail fast on missing result
    review_path = os.path.join(wt_dir, "review.json")
    try:
        review_bytes = Path(review_path).read_bytes()
    except OSError:
        return
    try:
        review_raw = review_bytes.decode("utf-8")
    except UnicodeError:
        review_raw = ""  # Retain original bytes; a replacement decode cannot validate them.

    # Live PR identity point-read: base ref name + head OID in one snapshot
    # (authority for retarget and head drift). The marker's base_ref is
    # dispatch-time provenance only: a PR retargeted to a different base
    # while the review ran is invisible to the SHA drift checks (the old
    # ref's tip and the head can both be unchanged), so the live base name
    # must be re-read here. Failed or malformed reads retain the marker.
    try:
        live_identity = gh_json("pr", "view", str(n),
                                "--json", "baseRefName,headRefOid")
    except (subprocess.CalledProcessError, ValueError):
        return
    if not isinstance(live_identity, dict):
        return
    _live_base_ref = live_identity.get("baseRefName")
    _live_head = live_identity.get("headRefOid")
    if not isinstance(_live_base_ref, str) or not _live_base_ref:
        return
    if not isinstance(_live_head, str) or not _live_head:
        return
    current_base_ref: str = _live_base_ref
    current_head: str = _live_head

    # Resolve current base tip (git fetch + rev-parse). Skipped on retarget:
    # the old ref may no longer exist (e.g. deleted after its own merge), and
    # the retarget discard in decide_review_harvest does not need its tip.
    current_base = ""
    if current_base_ref == base_ref:
        try:
            pipeline.validate_target_context(
                pipeline.ResolvedTarget(TRUSTED_HEAD_REPO, TARGET_REMOTE),
                cwd=wt_dir,
            )
        except (OSError, ValueError) as exc:
            print(f"tick: retaining reviewer harvest: target validation failed: {exc}", file=sys.stderr)
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage="review",
                    action="target_context_invalid", reason=str(exc),
                )
            return
        try:
            git("-C", wt_dir, "fetch", "--quiet", TARGET_REMOTE, base_ref)
            current_base = git("-C", wt_dir, "rev-parse", f"{TARGET_REMOTE}/{base_ref}")
        except subprocess.CalledProcessError:
            return
        if not current_base:
            return

    # A failed or incomplete history must never reset the spend valve.
    comments = _read_comment_history(n, phase="harvest", stage="review", tick_log=tick_log)
    if comments is None:
        return

    # Decide outcome
    try:
        harvest_result = pipeline.harvest_review(
            trusted_author=TRUSTED_REVIEW_AUTHOR,
            review=review_raw,
            comments=json.dumps(comments),
            pinned_base=pinned_base,
            current_base=current_base,
            pinned_head=pinned_head_str,
            current_head=current_head,
            pre_label=pre_label,
            diff_base_sha=diff_base_sha,
            pinned_base_ref=base_ref,
            current_base_ref=current_base_ref,
            fix_round=marker.get("fix_round", ""),
            threshold=max_iterations,
            report_protocol_version=marker.get("report_protocol_version"),
            review_identity=marker.get("dispatch_id", ""),
        )
    except Exception:
        # A persistent parse error in harvest_review would loop
        # forever without this cap. Track attempts and escalate
        # after MAX_DISPATCH_ATTEMPTS consecutive failures.
        if attempts is not None:
            _bound_failed_harvest(
                n,
                "pr",
                _REVIEWER_CONFIG.in_flight_label,
                wt_dir,
                attempts,
                tick_log,
                _REVIEWER_CONFIG.stage,
            )
        return

    harvest_label = str(harvest_result.get("label", ""))
    post_comment = harvest_result.get("post_comment") is True
    marker_dispatch_id = marker.get("dispatch_id")
    dispatch_id = (
        marker_dispatch_id if isinstance(marker_dispatch_id, str)
        else "unknown"
    )

    archive_dir = ""
    if "report_protocol_version" in marker or harvest_result.get("rendered_report"):
        rendered = str(harvest_result.get("rendered_report") or harvest_result.get("comment_body", ""))
        try:
            pipeline.atomic_write(os.path.join(wt_dir, "review-rendered.md"), rendered)
            pipeline.atomic_write(os.path.join(wt_dir, "review-manifest.json"), json.dumps({
                "report_protocol_version": marker.get("report_protocol_version", 1),
                "dispatch_id": dispatch_id,
                "source_sha256": hashlib.sha256(review_bytes).hexdigest(),
                "rendered_sha256": pipeline.review_reports.digest(rendered),
                "validation_reason": harvest_result.get("validation_reason", ""),
                "outcome": harvest_result.get("action"),
            }))
        except (OSError, UnicodeError):
            return
        archive_dir = _archive_harvest_evidence(
            wt_root=wt_root, wt_dir=wt_dir, kind="pr", number=n, dispatch_id=dispatch_id,
            tick_log=tick_log, stage="review", reuse_existing=True,
            required_files=("review.json", "review-rendered.md", "review-manifest.json"),
        )
        if not archive_dir:
            return

    if harvest_label == pipeline.LABEL_NEEDS_HUMAN:
        escalation_body = str(harvest_result.get("comment_body", ""))
        if archive_dir and harvest_result.get("validation_reason"):
            escalation_body += f"\n\nComplete source and validation record: `{archive_dir}`."
        # Escalation uses terminal-intent/receipt recovery, not review-round
        # identity. Persist the exact notice before remote writes so a landed
        # relabel followed by interruption cannot be mistaken for a human hold.
        # Only pre-upgrade invalid reports retain the legacy silent path.
        if not _record_terminal_intent(
            wt_root, "pr", n, dispatch_id, _REVIEWER_CONFIG.stage,
            kind="escalate",
            harvest_label=harvest_label,
            # Written explicitly: the resume validates the record against a
            # closed schema, where an absent field cannot be told apart from a
            # corrupted one.
            comment_body=escalation_body,
        ):
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=_REVIEWER_CONFIG.stage,
                    action="terminal_intent_failed",
                    reason=(
                        "escalation decision not durably recorded; "
                        "transition not started"
                    ),
                    dispatch_id=dispatch_id,
                )
            return
        # Logged before the transition rather than after it, because the
        # completion owns the scratch from here and deletes it. A transition
        # that fails and retries next tick re-logs the same observation under
        # the same dispatch id; duplicate telemetry is the cheaper half of
        # that trade against losing the evidence with the scratch.
        _log_reviewer_skill_observation(
            tick_log=tick_log,
            number=n,
            wt_dir=wt_dir,
            dispatch_id=dispatch_id,
        )
        _complete_escalate_transition(
            n=n,
            entity="pr",
            wt_dir=wt_dir,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            stage=_REVIEWER_CONFIG.stage,
            harvest_label=harvest_label,
            comment_body=escalation_body,
            attempts=attempts,
            tick_log=tick_log,
        )
        return

    skip_cleanup = False

    # Post comment when due. Gate relabel on successful post.
    if post_comment:
        comment_body = str(harvest_result.get("comment_body", ""))
        tmp_path = os.path.join(wt_dir, "review-comment-body.txt")
        try:
            with open(tmp_path, "w") as fh:
                fh.write(comment_body)
            gh("pr", "comment", str(n), "--body-file", tmp_path)
        except (OSError, subprocess.CalledProcessError):
            skip_cleanup = True
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Flip the label only when comment posted (or not needed) and edit succeeds.
    if not skip_cleanup:
        if not _converge_pipeline_labels(
            "pr", n, harvest_label,
            tick_log=tick_log, phase="harvest", stage=_REVIEWER_CONFIG.stage,
        ):
            skip_cleanup = True
        else:
            if attempts is not None:
                attempts.clear(n)

    if skip_cleanup:
        _bound_failed_harvest(
            n, "pr", _REVIEWER_CONFIG.in_flight_label, wt_dir, attempts,
            tick_log, _REVIEWER_CONFIG.stage,
        )
        return
    _log_reviewer_skill_observation(
        tick_log=tick_log,
        number=n,
        wt_dir=wt_dir,
        dispatch_id=dispatch_id,
    )
    if tick_log:
        tick_log.log(
            pr=n, phase="harvest", stage="review", action="review_handoff",
            reason="review outcome handed off after publication and label convergence",
            engine=str(marker.get("engine", "codex")), dispatch_id=dispatch_id,
            details={"label": harvest_label, "outcome": harvest_result.get("action"),
                     "head_sha": current_head},
        )
    if archive_dir and not _finalize_harvest_archive(
        wt_dir, archive_dir, number=n, stage="review", phase="harvest",
        dispatch_id=dispatch_id, tick_log=tick_log,
    ):
        return
    cleanup_scratch(wt_dir)


def _fallback_implementation_pr_metadata(issue_number: int) -> tuple[str, str]:
    """Identify the review without inventing implementation or test results."""
    return (
        f"Issue #{issue_number}: implementation review",
        "## Summary\n"
        f"Closes #{issue_number}\n\n"
        "The authored implementation report is unavailable. Review the branch "
        "and recover the author's explanation and evidence before approval.\n\n"
        "No verification results are claimed by this placeholder.\n",
    )


def _pr_document_lines(
    body: str, *, include_code: bool = False, exclude_comments: bool = False,
) -> list[tuple[str, str]]:
    """Read document sections outside code, including list-contained blocks.

    List content margins are cumulative; the fence's three-space allowance is
    relative to its container. Dedenting out of a list ends its unclosed fence.
    Tabs affect indentation columns only. Publication bytes remain authored,
    separate from the concurrent-edit fingerprint. Evidence
    may include code lines, but code never establishes section boundaries.
    Its optional comment filter changes only the evidence view, not the
    authored body or the default closure/layout interpretation.
    """
    lines: list[tuple[str, str]] = []
    margins: list[int] = []
    markers: list[tuple[int, int]] = []
    fence = ""
    width = 0
    section = ""
    paragraph = False
    empty_item = False
    comment = False

    def without_comments(line: str) -> str:
        nonlocal comment
        visible: list[str] = []
        while line:
            if comment:
                _, closing, line = line.partition("-->")
                if not closing:
                    break
                comment = False
                continue
            # Inline code and escaped '<' display comment markers literally.
            token = re.search(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)|<!--", line)
            if not token:
                visible.append(line)
                break
            prefix = line[:token.start()]
            visible.append(prefix)
            escaped = (len(prefix) - len(prefix.rstrip("\\"))) % 2
            if token[0] == "<!--" and not escaped:
                comment = True
            else:
                visible.append(token[0])
            line = line[token.end():]
        return "".join(visible)

    def opening_fence(content: str) -> re.Match[str] | None:
        opening = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", content)
        if opening and not (opening[1][0] == "`" and "`" in opening[2]):
            return opening
        return None

    def paragraph_break(content: str) -> bool:
        # Inline backticks, hashtags and tildes are ordinary paragraph text.
        # Thematic breaks and setext underlines, however, end a paragraph even
        # though they have no ATX heading marker. Share this classification with
        # lazy list continuation so both decisions use actual block syntax.
        return bool(
            re.match(r" {0,3}(?:#{1,6}(?: |$)|>)", content)
            or re.fullmatch(r" {0,3}(?:(?:- *){3,}|(?:\* *){3,}|(?:_ *){3,})", content)
            or (paragraph and re.fullmatch(r" {0,3}(?:=+|-+) *", content))
            or opening_fence(content)
        )

    def list_item(content: str, margin: int, interrupting: bool) -> re.Match[str] | None:
        item = re.match(r"( {0,3})((?:[-+*]|[0-9]{1,9}[.)]))( +|$)(.*)$", content)
        if not item or re.fullmatch(r" {0,3}(?:(?:- *){3,}|(?:\* *){3,}|(?:_ *){3,})", content):
            return None
        # A marker outside the paragraph's item can start a sibling or a new
        # list, even when its bullet/type/delimiter changes. Only a marker in
        # the same content container interrupts that paragraph: an ordered
        # interruption must start at 1, and an empty item cannot interrupt it.
        position = margin + len(item[1])
        outside_paragraph = any(
            parent <= position < content_margin
            for parent, content_margin in markers
        )
        if interrupting and not outside_paragraph:
            if not item[4].strip() or (item[2][0].isdigit() and int(item[2][:-1]) != 1):
                return None
        return item

    for line in body.splitlines():
        # Hidden lines cannot open fences, list items, or coverage sections.
        if exclude_comments and comment:
            line = without_comments(line)
            if not line.strip():
                continue
        expanded = line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip(" "))
        # A bare marker already supplies the item's first blank content line.
        # Another blank ends that empty item, even if the next block has enough
        # indentation to look like a continuation. Its parent remains open.
        if empty_item and not expanded.strip():
            margins.pop()
            markers = markers[:len(margins)]
        empty_item = False
        # A paragraph may continue without its list indentation. Block starts
        # (notably a fence) and lines after a blank cannot be lazy continuations.
        enclosing_margin = next((m for m in reversed(margins) if m <= indent), 0)
        block_start = paragraph_break(expanded[enclosing_margin:]) or list_item(
            expanded[enclosing_margin:], enclosing_margin, paragraph,
        )
        lazy = paragraph and bool(expanded.strip()) and not block_start
        if expanded.strip() and not lazy:
            while margins and indent < margins[-1]:
                margins.pop()
                # Preserve popped markers until this line is classified: the
                # line may be a sibling of the item it just dedented out of.
                fence = ""
        margin = margins[-1] if margins else 0
        content = expanded[margin:] if indent >= margin else expanded.lstrip(" ")
        if fence:
            closing = r" {0,3}" + re.escape(fence) + "{" + str(width) + r",} *"
            if re.fullmatch(closing, content):
                fence = ""
            elif include_code:
                lines.append((section, line))
            continue
        # A fence can start on the marker line, including in nested list items.
        interrupting = paragraph
        while item := list_item(content, margin, interrupting):
            padding = len(item[3])
            if not item[4] or padding > 4:
                padding = 1
            offset = len(item[1]) + len(item[2]) + padding
            markers = markers[:len(margins)] + [(margin, margin + offset)]
            margin += offset
            margins.append(margin)
            content = content[offset:]
            empty_item = not content.strip()
            interrupting = False
            paragraph = False
        markers = markers[:len(margins)]
        opening = opening_fence(content)
        if opening:
            fence, width = opening[1][0], len(opening[1])
            paragraph = False
            continue
        # Four content columns start indented code only outside a paragraph.
        # Such examples have no heading or closure authority either.
        if not paragraph and content.startswith("    "):
            if include_code:
                lines.append((section, line))
            continue
        if exclude_comments:
            line = without_comments(line)
            expanded = line.expandtabs(4)
            content = expanded[margin:] if indent >= margin else expanded.lstrip(" ")
        heading = re.fullmatch(r" {0,3}##[ \t]+(.+?)[ \t]*", line)
        if heading:
            section = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1]).strip().lower()
        paragraph = bool(content.strip()) and not paragraph_break(content)
        lines.append((section, line))
    return lines


def _primary_closures(body: str, *, summary_only: bool = True) -> set[str]:
    """Find standalone closures outside code blocks, normally under Summary."""
    numbers: set[str] = set()
    for section, line in _pr_document_lines(body):
        if summary_only and section != "summary":
            continue
        match = re.fullmatch(r"\s*(?:(?:[-+*]|\d+[.)])[ \t]+)?Closes #(\d+)[ \t]*", line)
        if match:
            numbers.add(match[1].lstrip("0") or "0")
    return numbers


def _pr_body_validation_reason(body: str, issue_number: int) -> str:
    """Validate control fields only; section completeness is advisory."""
    if len(body) > _PR_BODY_MAX_CHARS or len(body.encode()) > _PR_BODY_MAX_CHARS:
        return "body exceeds the publication limit"
    numbers = _primary_closures(body)
    if not numbers:
        return "missing primary closure under Summary"
    if len(numbers) != 1:
        return "ambiguous primary closure under Summary"
    if numbers != {str(issue_number)} or issue_number <= 0:
        return "primary closure does not match target issue"
    return ""


def _pr_body_matches_guidelines(body: str, issue_number: int) -> bool:
    """Accept publishable closure authority regardless of prose layout."""
    return not _pr_body_validation_reason(body, issue_number)


def _pr_layout_notice(body: str) -> str:
    """Describe missing template sections without certifying report evidence."""
    sections = {section for section, _ in _pr_document_lines(body)}
    missing = [heading[3:] for heading in _PR_BODY_REQUIRED_SECTIONS
               if heading[3:].lower() not in sections]
    if not missing:
        return ""
    return (
        _PR_LAYOUT_NOTICE_PREFIX + ", ".join(missing) + _PR_LAYOUT_NOTICE_SUFFIX
    )


def _is_pr_layout_notice(paragraph: str) -> bool:
    """Recognize only the exact controller-generated missing-section paragraph."""
    if not (paragraph.startswith(_PR_LAYOUT_NOTICE_PREFIX)
            and paragraph.endswith(_PR_LAYOUT_NOTICE_SUFFIX)):
        return False
    missing = paragraph[len(_PR_LAYOUT_NOTICE_PREFIX):-len(_PR_LAYOUT_NOTICE_SUFFIX)].split(", ")
    return missing == [heading[3:] for heading in _PR_BODY_REQUIRED_SECTIONS
                       if heading[3:] in missing]


def _split_pr_warning_prefix(body: str) -> tuple[str, str]:
    """Separate exact leading controller warnings from authored occurrences."""
    prefix = ""
    remainder = body.replace("\r\n", "\n")
    while True:
        paragraph, separator, tail = remainder.partition("\n\n")
        if paragraph not in (pipeline.VERIFICATION_WARNING, _IMPLEMENTATION_EVIDENCE_WARNING):
            break
        prefix += paragraph + "\n\n"
        remainder = tail if separator else ""
    return prefix, remainder


def _with_implementation_evidence_notice(body: str) -> str:
    """Refresh structural evidence presence without judging the authored proof."""
    prefix, remainder = _split_pr_warning_prefix(body)
    prefix = prefix.replace(_IMPLEMENTATION_EVIDENCE_WARNING + "\n\n", "")
    fields: set[str] = set()
    exempt = False
    for section, line in _pr_document_lines(
        remainder, include_code=True, exclude_comments=True,
    ):
        if section != "test coverage":
            continue
        # The skill's output can be a literal block or readable Markdown.
        # Values need content; their correctness remains the reviewer's job.
        text = re.sub(r"^\s*(?:[-+*]|\d+[.)])[ \t]+", "", line)
        text = text.replace("**", "").replace("__", "").strip()
        label, separator, value = text.partition(":")
        if not separator or not value.strip():
            continue
        label = label.strip().lower()
        if label in _IMPLEMENTATION_EVIDENCE_FIELDS:
            fields.add(label)
        if label == "definition of done" and re.fullmatch(
            r"n/a\s+[—–-]\s+\S.*", value.strip(), re.IGNORECASE,
        ):
            exempt = True
    if not exempt and not _IMPLEMENTATION_EVIDENCE_FIELDS <= fields:
        prefix += _IMPLEMENTATION_EVIDENCE_WARNING + "\n\n"
    return prefix + remainder


def _with_metadata_notice(body: str, notice: str) -> str:
    """Refresh leading controller layout state; preserve authored occurrences."""
    # Interpret controller prefixes after the supported CRLF equivalence, not
    # after the concurrent-edit fingerprint's broader surrounding-space strip.
    prefix, remainder = _split_pr_warning_prefix(body)
    # Only exact controller paragraphs in the leading prefix are ours. Never
    # remove matching text embedded in prose/code, or custom authored notices.
    while True:
        paragraph, separator, tail = remainder.partition("\n\n")
        if not _is_pr_layout_notice(paragraph):
            break
        remainder = tail if separator else ""
    if notice and not (remainder == notice or remainder.startswith(notice + "\n\n")):
        remainder = notice + "\n\n" + remainder
    return prefix + remainder


def _supply_primary_closure(body: str, issue_number: int) -> tuple[str, str]:
    """Keep the report and supply known authority when layout omits it.

    A visible conflicting reference is not formatting. Never infer the target
    from examples: the caller supplies the dispatch issue or pinned provenance.
    The raw closing-directive-set check still gates every fixer replacement.
    """
    if _primary_closures(body):
        return body, ""
    visible = _primary_closures(body, summary_only=False)
    if visible - {str(issue_number)}:
        return body, "conflicting primary closure outside Summary"
    if issue_number <= 0:
        return body, "no controller target issue"
    # Leading controller notices are refreshed separately. Keep the existing
    # verification warning first so the normal composer does not duplicate it.
    prefix, remainder = _split_pr_warning_prefix(_with_metadata_notice(body, ""))
    return (
        prefix + f"## Summary\nCloses #{issue_number}\n\n"
        "> **Controller issue reference:** Harvest supplied the known target "
        "because the authored report has no visible primary closure under Summary. "
        "The complete authored report follows.\n\n" + remainder,
        "",
    )


def _normalized_pr_body(body: str) -> str:
    """Return a PR body reduced to what GitHub round-trips unchanged.

    Both comparisons the revision path makes — live body against the
    dispatch-time snapshot, and live body against the candidate replacement —
    have to survive a round trip through GitHub, which normalises line endings
    and trailing whitespace. Comparing raw text would report the body the
    harvest itself just published as a human edit.

    Args:
        body: Raw PR body text.

    Returns:
        The body with CRLF line endings and surrounding whitespace removed.
    """
    return body.replace("\r\n", "\n").strip()


def _pr_body_digest(body: str) -> str:
    """Return the fingerprint of one PR body, as recorded in a fixer marker.

    A digest rather than the body itself: ``dispatch.json`` is a strictly
    validated control artifact, and embedding an unbounded, arbitrary document
    in it would make marker size a function of PR prose. GitHub keeps the body's
    own edit history, so nothing an operator needs is lost.

    Args:
        body: Raw PR body text.

    Returns:
        The hex SHA-256 of the normalised body.
    """
    return hashlib.sha256(_normalized_pr_body(body).encode()).hexdigest()


def _declared_closures(body: str) -> tuple[str, ...]:
    """Return every issue reference one PR body declares a closing keyword for.

    Merging a PR closes whatever its body's closing keywords name, wherever they
    appear in it — not only the ``Closes #N`` under ``## Summary``. A replacement
    body therefore has to be judged against the *complete* declared set, or a
    revision could silently add a closure further down or drop a second one the
    summary never mentioned.

    Read from the body rather than from GitHub's ``closingIssuesReferences``,
    which is the source-owned contract and the only one that survives a
    non-default base: GitHub reports no linked issues for a stacked PR even
    though its body still opens with ``Closes #N``.

    The pattern deliberately over-matches — prose that happens to read "fixes
    #12" counts, because GitHub counts it too.

    Args:
        body: Raw PR body text.

    Returns:
        The distinct references, lowercased and sorted, so two bodies declaring
        the same closures in a different order compare equal.
    """
    return tuple(sorted({
        match.group(1).lower()
        for match in _CLOSING_DIRECTIVE_RE.finditer(body)
    }))


def _summary_closure(body: str) -> int:
    """Return the single distinct primary closure shared by both authors."""
    numbers = _primary_closures(body)
    try:
        return int(next(iter(numbers))) if len(numbers) == 1 else 0
    except ValueError:
        return 0  # an unrepresentable number cannot supply dispatch authority


class PRBodyProvenance(NamedTuple):
    """What dispatch pins about the PR body it handed one fixer.

    None of it can be recovered at harvest time: by then the live body may have
    been replaced by this run or edited by a human, and either reading would
    defeat the check it feeds.

    Attributes:
        digest: :func:`_pr_body_digest` of the body as dispatch read it.
        summary_closes: The single primary issue under Summary. The
            replacement must declare the same one outside code blocks.
        closes: Every closure :func:`_declared_closures` found in it. The
            replacement must declare exactly the same set.
        truncated: Whether the inline PR-body excerpt was cut short. This
            conservative replacement guard remains in place even when the
            complete body is also supplied in the discussion file.
    """

    digest: str
    summary_closes: int
    closes: tuple[str, ...]
    truncated: bool

    def as_marker_field(self) -> dict[str, object]:
        """Return this provenance as the marker's ``pr_body`` value.

        Returns:
            A JSON-serialisable mapping carrying every field.
        """
        return {
            "digest": self.digest,
            "summary_closes": self.summary_closes,
            "closes": list(self.closes),
            "truncated": self.truncated,
        }

    @classmethod
    def from_marker(cls, value: object) -> "PRBodyProvenance | None":
        """Rebuild provenance from a marker field, or reject it.

        Strict rather than best-effort: every field gates a remote write with no
        undo, so a half-typed block withholds the revision instead of being
        partially trusted. The rules live in ``pipeline`` beside the rest of the
        marker schema and are called rather than restated, so reconcile and the
        harvest cannot disagree about which markers carry a usable contract.

        Args:
            value: The marker's ``pr_body`` field, of unknown shape.

        Returns:
            The provenance, or ``None`` when the field is absent or malformed.
        """
        if not pipeline.valid_pr_body_provenance(value):
            return None
        block = cast("dict[str, object]", value)
        return cls(
            digest=cast("str", block["digest"]),
            summary_closes=cast("int", block["summary_closes"]),
            closes=tuple(cast("list[str]", block["closes"])),
            truncated=cast("bool", block["truncated"]),
        )


def _pr_body_provenance(body: str, *, truncated: bool) -> PRBodyProvenance | None:
    """Pin the closure contract and fingerprint of one PR body at dispatch.

    Read through :func:`_normalized_pr_body` because that is the form the
    candidate is reduced to before it is compared against this. The two readings
    agree today whether or not this call is here; it is here so they still agree
    if normalisation ever gains a rule, since a divergence would not fail — it
    would pin a contract no replacement can satisfy and silently switch the path
    off for that PR.

    Args:
        body: The PR body dispatch read, complete rather than bounded.
        truncated: Whether the copy substituted into the prompt was cut short.

    Returns:
        The provenance, or ``None`` when the body declares no ``Closes #N``
        under ``## Summary``. Nothing can then hold a replacement to the
        original contract, so the dispatch pins nothing and the harvest
        withholds any revision the engine offers.
    """
    normalized = _normalized_pr_body(body)
    summary_closes = _summary_closure(normalized)
    if not summary_closes:
        return None
    return PRBodyProvenance(
        digest=_pr_body_digest(normalized),
        summary_closes=summary_closes,
        closes=_declared_closures(normalized),
        truncated=truncated,
    )


def _fixer_body_revision(
    *,
    wt_dir: str,
    dispatch_id: str,
    current_head: str,
    provenance: PRBodyProvenance | None,
    live_body: str | None,
    verification_unconfigured: bool = False,
) -> tuple[str, str]:
    """Classify the fixer's candidate replacement PR body.

    The engine is instructed to hand the controller a candidate and the
    controller decides. Every rejection is a state the published fix comment
    names, because ``gh pr edit`` replaces the whole body and the pipeline has
    no undo: a body left alone with a stated reason is always recoverable, and a
    wrong body pushed live is not.

    Args:
        wt_dir: The fixer's worktree directory.
        dispatch_id: This run's dispatch id; the candidate must be stamped with
            it to prove the file was written by this engine rather than carried
            in by the branch checkout.
        current_head: The PR's live head OID at harvest. The candidate declares
            the commit it describes, and it has to be this one.
        provenance: What dispatch pinned about the body it handed the engine, or
            ``None`` on a marker that pinned nothing.
        live_body: The PR's body point-read at harvest, or ``None`` when it
            could not be read.
        verification_unconfigured: Include the dispatch's warning even if the
            fixer supplies no replacement; retain historical warnings too.

    Returns:
        ``(state, body)``. ``state`` is ``""`` when there is no candidate at all
        (the normal case), ``"ready"`` for a publishable fixer replacement,
        ``"warning-only"`` for the controller's warning prefix, and
        otherwise the reason it may not: ``"unreadable"``, ``"oversized"``,
        ``"unstamped"``, ``"stale-head"``, ``"unavailable"``, ``"truncated"``,
        ``"closure-drift"``, ``"invalid"``, or ``"conflict"``. Each names its own
        cause, because the comment this feeds is the next reviewer's handoff:
        reporting an empty body for a file that was too large or could not be
        read would send the reviewer to the wrong cause.
    """
    try:
        with open(
            os.path.join(wt_dir, _PR_BODY_REVISION_FILE), encoding="utf-8",
        ) as fh:
            # Bounded read, not a bounded check: see _PR_BODY_REVISION_READ_LIMIT.
            text = fh.read(_PR_BODY_REVISION_READ_LIMIT + 1)
    except FileNotFoundError:
        if not verification_unconfigured and not (
            live_body is not None and pipeline.VERIFICATION_WARNING in live_body
        ):
            return "", ""
        if live_body is None:
            return "unavailable", ""
        # This is a controller prefix to the freshly read live body, not an
        # engine rewrite. It needs no issue closure or dispatch-time story
        # provenance; the write gate rechecks this exact live snapshot.
        body = _normalized_pr_body(pipeline.with_verification_warning(
            live_body, required=True,
        ))
        if len(body) > _PR_BODY_MAX_CHARS or len(body.encode()) > _PR_BODY_MAX_CHARS:
            return "oversized", ""
        return "warning-only", body
    except (OSError, UnicodeDecodeError):
        # Present but unusable is not the same as absent: report it rather than
        # letting an unreadable revision pass as "the fixer wrote nothing".
        return "unreadable", ""
    if len(text) > _PR_BODY_REVISION_READ_LIMIT:
        return "oversized", ""

    # Ordered so the published reason is the true one: whose file this is, then
    # what the controller knows about the body, then the body's own content. A
    # leftover from an earlier run reported as a marker problem, or a well-formed
    # body reported as malformed, is a diagnosis a reviewer would act on.
    lines = text.splitlines()
    head_line = lines[1].strip() if len(lines) > 1 else ""
    if (
        not lines
        or lines[0].strip() != f"{_PR_BODY_REVISION_STAMP} {dispatch_id}"
        or not head_line.startswith(_PR_BODY_REVISION_HEAD_STAMP)
    ):
        return "unstamped", ""
    declared_head = head_line[len(_PR_BODY_REVISION_HEAD_STAMP):].strip().lower()
    if not declared_head or declared_head != current_head.lower():
        return "stale-head", ""

    if live_body is None or provenance is None:
        return "unavailable", ""
    if provenance.truncated:
        # The engine saw a prefix and was asked for a whole document. Nothing
        # downstream can tell a deliberate rewrite from the silent loss of a
        # section that sat past the cut.
        return "truncated", ""

    envelope = text.split("\n", 2)
    body = _normalized_pr_body(envelope[2] if len(envelope) == 3 else "")
    if not body:
        return "invalid", ""
    body, closure_reason = _supply_primary_closure(body, provenance.summary_closes)
    if closure_reason:
        return "closure-drift", ""
    body = _with_metadata_notice(body, _pr_layout_notice(body))
    # An accepted replacement can repair an existing implementation notice.
    # Unrelated fixer-only reports do not acquire a new evidence obligation.
    if any(_IMPLEMENTATION_EVIDENCE_WARNING in _split_pr_warning_prefix(report)[0]
           for report in (body, live_body)):
        body = _with_implementation_evidence_notice(body)
    body = _normalized_pr_body(pipeline.with_verification_warning(
        body, required=verification_unconfigured
        or pipeline.VERIFICATION_WARNING in live_body,
    ))
    # Both readings of the limit, because GitHub documents neither: see
    # _PR_BODY_MAX_CHARS. Reported as its own cause rather than as a section
    # failure — the body may be perfectly well formed and simply too long.
    if (
        len(body) > _PR_BODY_MAX_CHARS
        or len(body.encode()) > _PR_BODY_MAX_CHARS
    ):
        return "oversized", ""
    # Preserve both primary authority and every additional closing directive.
    # Presentation flexibility must not change which issues merging will close.
    if (
        _summary_closure(body) != provenance.summary_closes
        or _declared_closures(body) != provenance.closes
    ):
        return "closure-drift", ""
    if not _pr_body_matches_guidelines(body, provenance.summary_closes):
        return "invalid", ""

    live = _normalized_pr_body(live_body)
    # Identity first, conflict second. A retry after a landed edit whose comment
    # post failed finds the live body already replaced; checked the other way
    # round, that reads as a conflict — the harvest reporting its own write as
    # somebody else's edit.
    #
    # This does collapse an ABA: a human who independently wrote a byte-identical
    # body is credited to this run in the comment. That is accepted rather than
    # solved, because the two states are the same bytes — the gate exists to stop
    # content loss, and there is none to stop when the live body already *is* the
    # candidate. Telling them apart would need a write receipt that could only
    # ever change one sentence of prose.
    if live == body:
        return "ready", body
    if _pr_body_digest(live) != provenance.digest:
        return "conflict", ""
    return "ready", body


class ImplementationPRMetadata(NamedTuple):
    """Authored content and the mechanical decision retained for publication."""

    title: str
    body: str
    outcome: str
    reason: str
    notice: str
    normalizations: tuple[str, ...]
    controller_reference: bool = False


class PRMetadataInvalid(ValueError):
    """Readable source cannot safely authorize initial publication."""


def _read_engine_pr_metadata(wt_dir: str, issue_number: int) -> ImplementationPRMetadata:
    """Recover title independently; distinguish absent content from I/O failure."""
    path = os.path.join(wt_dir, _PR_BODY_SOURCE_FILE)
    reason = ""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        if os.path.lexists(path):
            raise  # an existing dangling link is an I/O failure
        raw = b""
        reason = "source artifact absent"
    title_raw, _, body_raw = raw.partition(b"\n")
    try:
        title = title_raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        title = ""
    fallback_title, fallback_body = _fallback_implementation_pr_metadata(issue_number)
    title = title or fallback_title
    try:
        body = body_raw.decode("utf-8")
    except UnicodeDecodeError:
        body = ""
        reason = "body is not decodable as UTF-8"
    normalizations = ("CRLF to LF",) if "\r\n" in body else ()
    body = body.replace("\r\n", "\n")
    # The envelope's blank separator is not part of the report. Do not strip
    # line-end whitespace: two trailing spaces can be a Markdown hard break.
    if body.startswith("\n"):
        body = body[1:]
    if not body.strip():
        reason = reason or "body is empty"
        notice = (
            "> **Metadata/layout notice:** Authored PR metadata unavailable: "
            + reason + ". No verification results are claimed."
        )
        return ImplementationPRMetadata(
            title, fallback_body, "unavailable", reason, notice, normalizations,
        )
    authored_body = body
    body, reason = _supply_primary_closure(body, issue_number)
    reason = reason or _pr_body_validation_reason(body, issue_number)
    return ImplementationPRMetadata(
        title, body, "invalid" if reason else "authored", reason,
        _pr_layout_notice(body) if not reason else "", normalizations,
        controller_reference=body != authored_body,
    )


def _pr_publication_path(wt_dir: str, number: int, kind: str = "issue") -> str:
    """Keep a small publication record outside disposable engine scratch."""
    dispatch_id = _read_dispatch_id(wt_dir)
    basename = os.path.basename(_harvest_evidence_path(
        os.path.dirname(wt_dir), kind, number, dispatch_id,
    ))
    return os.path.join(os.path.dirname(wt_dir), ".pr-publications", basename)


def _record_pr_metadata(
    wt_dir: str, number: int, metadata: dict[str, object], *,
    kind: str = "issue", source_file: str = _PR_BODY_SOURCE_FILE,
) -> None:
    """Retain original bytes and decision before create, escalation, or cleanup.

    Atomic individual replacements leave the original worktree as retry owner
    until every write succeeds. A later handoff reads only the decision notice;
    it never replays the staged title or report over an existing PR.
    """
    archive = _pr_publication_path(wt_dir, number, kind)
    os.makedirs(archive, exist_ok=True)
    source = os.path.join(wt_dir, source_file)
    if os.path.lexists(source) and not _replace_evidence_file(
        archive, source_file, lambda partial: shutil.copyfile(source, partial),
    ):
        raise OSError("could not retain authored PR source")
    decision = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"

    def write_decision(path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(decision)

    if not _replace_evidence_file(wt_dir, ".pr-metadata-decision.json", write_decision):
        raise OSError("could not record PR metadata decision")
    if not _replace_evidence_file(archive, "decision.json", write_decision):
        raise OSError("could not retain PR metadata decision")


def _implementation_metadata_notice(wt_dir: str, issue_number: int) -> str:
    """Load this dispatch's notice for recovery without reading stale content."""
    try:
        path = os.path.join(_pr_publication_path(wt_dir, issue_number), "decision.json")
        with open(path, encoding="utf-8") as fh:
            decision = json.load(fh)
    except FileNotFoundError:
        if os.path.lexists(os.path.join(wt_dir, ".pr-metadata-decision.json")):
            raise  # this handoff owes a retained record; do not drop its notice
        return ""  # legacy handoff with no metadata decision
    if not isinstance(decision, dict) or not isinstance(decision.get("notice"), str):
        raise ValueError("invalid PR publication decision")
    return decision["notice"]


def _implementation_pr_create_args(
    wt_dir: str, issue_number: int, *, verification_unconfigured: bool = False,
) -> list[str]:
    """Stage authored metadata; I/O retries and invalid control data escalates."""
    metadata = _read_engine_pr_metadata(wt_dir, issue_number)
    body = _with_metadata_notice(metadata.body, metadata.notice)
    body = _with_implementation_evidence_notice(body)
    body = pipeline.with_verification_warning(body, required=verification_unconfigured)
    if len(body) > _PR_BODY_MAX_CHARS or len(body.encode()) > _PR_BODY_MAX_CHARS:
        metadata = metadata._replace(
            outcome="invalid", reason="body with notices exceeds the publication limit",
        )
    _record_pr_metadata(wt_dir, issue_number, dict(metadata._asdict()))
    if metadata.outcome == "invalid":
        raise PRMetadataInvalid(metadata.reason)
    body_file = os.path.join(wt_dir, _PR_CREATE_BODY_FILE)
    with open(body_file, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    return ["--title", metadata.title, "--body-file", body_file]


def _handle_transient_engine_result(
    *,
    entity: str,
    number: int,
    config: pipeline.StageConfig,
    pre_label: str,
    engine: str,
    result_raw: str,
    harvest_result: dict[str, object],
    dispatch_id: str,
    wt_root: str,
    wt_dir: str,
    tick_log: TickLog | None,
    attempts: Attempts | None,
    transient_cooldown_seconds: int = DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
    transient_cooldown_reset_buffer_seconds: int = (
        TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS
    ),
) -> bool:
    """Persist cooldown and apply the shared transient retry transition.

    Both of this path's terminal transitions to ``ai:needs-human-decision``
    publish an explanation before the label moves. A spent retry budget gates its
    relabel on the post — and bounds the failure through
    :func:`_bound_failed_harvest`, so a permanently failing post terminates at
    the attempt cap rather than retrying forever behind a marker that keeps
    pinning its admission slot.
    A failed attempt-counter write cannot gate: the budget that would bound the
    retry is exactly what failed to persist, so its post is best-effort and a
    miss is logged as ``escalation_comment_failed``.

    Returns ``False`` when cooldown or label persistence fails, or when the
    exhaustion escalation could not be published, leaving the in-flight label and
    scratch evidence intact for a later tick.
    """
    failure_class = str(harvest_result.get("failure_class", "quota_or_limit"))
    try:
        result_obj: object = json.loads(result_raw)
    except json.JSONDecodeError:
        result_obj = None
    now = time.time()
    cooldown_until = transient_cooldown_until(
        result_obj, now=now,
        cooldown_seconds=transient_cooldown_seconds,
        reset_buffer_seconds=transient_cooldown_reset_buffer_seconds,
    )
    try:
        domain = _engine_quota_domain(engine)
        EngineCooldowns(wt_root).start(
            domain,
            until=cooldown_until,
            reason=failure_class,
        )
    except (OSError, ValueError):
        if tick_log:
            tick_log.log(
                pr=number, phase="harvest", stage=config.stage,
                action="cooldown_state_error",
                reason="failed to persist transient engine cooldown",
                dispatch_id=dispatch_id,
                details={"domain": engine},
            )
        return False

    next_attempt = attempts.read(number) + 1 if attempts is not None else 1
    exhausted = next_attempt >= pipeline.MAX_DISPATCH_ATTEMPTS
    target_label = pipeline.LABEL_NEEDS_HUMAN if exhausted else pre_label
    if exhausted:
        # Record the decision before the transition starts, so a failed or
        # interrupted archive move resumes this same exhaustion next tick
        # instead of re-entering ordinary harvest and publishing a
        # contradictory outcome under the pipeline's own terminal label. The
        # record is a required gate: without it a failed archive move would
        # resume as a held-completion replay.
        if not _record_terminal_intent(
            wt_root, entity, number, dispatch_id, config.stage,
            kind="transient_exhausted",
            failure_class=failure_class,
            attempt=next_attempt,
        ):
            if tick_log:
                tick_log.log(
                    pr=number, phase="harvest", stage=config.stage,
                    action="terminal_intent_failed",
                    reason=(
                        "transient-exhaustion decision not durably recorded; "
                        "transition not started"
                    ),
                    dispatch_id=dispatch_id,
                )
            return False
        return _complete_transient_exhaustion(
            n=number,
            entity=entity,
            wt_dir=wt_dir,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            stage=config.stage,
            failure_class=failure_class,
            attempt=next_attempt,
            attempts=attempts,
            tick_log=tick_log,
        )
    if not _converge_pipeline_labels(
        entity, number, target_label,
        tick_log=tick_log, phase="harvest", stage=config.stage,
    ):
        return False
    if attempts is not None:
        try:
            attempts.incr(number)
        except OSError:
            if tick_log:
                tick_log.log(
                    pr=number, phase="harvest", stage=config.stage,
                    action="attempt_state_error",
                    reason="failed to persist transient attempt count",
                    dispatch_id=dispatch_id,
                    details={"attempt": next_attempt},
                )
            # Publish before the relabel, but — unlike the exhaustion path
            # above — do not gate on it. The retry budget is exactly what failed
            # to persist here, so a blocked transition could never be bounded
            # and would pin the admission slot forever. Same fail-open shape as
            # the attempt cap: the label is the load-bearing signal, the comment
            # is the explanation, and the miss is logged.
            posted = _post_harvest_comment(
                entity=entity,
                number=number,
                wt_root=wt_root,
                dispatch_id=dispatch_id,
                comment_body=pipeline.render_transient_escalation_comment(
                    stage=config.stage,
                    outcome=pipeline.OUTCOME_ATTEMPT_STATE_ERROR,
                    failure_class=failure_class,
                ),
                kind=_ESCALATION_COMMENT_KIND,
                target_label=pipeline.LABEL_NEEDS_HUMAN,
            )
            if not posted and tick_log:
                tick_log.log(
                    pr=number, phase="harvest", stage=config.stage,
                    action="escalation_comment_failed",
                    reason="escalating to the terminal label without an explanation",
                    dispatch_id=dispatch_id,
                )
            _converge_pipeline_labels(
                entity, number, pipeline.LABEL_NEEDS_HUMAN,
                tick_log=tick_log, phase="harvest", stage=config.stage,
            )
            return False
    if tick_log:
        # The exhausted path logs inside _complete_transient_exhaustion; this
        # tail is the revert path only.
        tick_log.log(
            pr=number, phase="harvest", stage=config.stage,
            action="transient_revert",
            reason=failure_class,
            dispatch_id=dispatch_id,
            details={
                "attempt": next_attempt,
                "cooldown_until": cooldown_until,
                "domain": domain,
                "failure_class": failure_class,
            },
        )
    return True


def _harvest_one_implementer(
    decision: dict[str, object],
    wt_root: str,
    tick_log: TickLog | None = None,
    attempts: Attempts | None = None,
    *,
    transient_cooldown_seconds: int = DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
    transient_cooldown_reset_buffer_seconds: int = (
        TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS
    ),
) -> None:
    """Process a completed implementer harvest: read result, create PR or escalate.

    Reads ``result.json`` from the issue's worktree, calls
    ``pipeline.harvest_implementation``, and executes the prescribed side effects:
    ``create_pr`` → ``gh pr create`` + relabel + worktree rename;
    ``escalate`` → publish the reason and relabel to ``ai:needs-human-decision``.

    On any ``gh`` failure, the marker and scratch survive for the next tick.
    """
    n = decision.get("pr_number")
    if not isinstance(n, int):
        return

    wt_dir = os.path.join(wt_root, f"issue-{n}")
    # See _harvest_one_fixer: a published cap escalation owns the outcome.
    if _resume_terminal_escalation(n, "issue", wt_dir, attempts, tick_log):
        return

    result_path = os.path.join(wt_dir, "result.json")
    try:
        with open(result_path) as fh:
            result_raw = fh.read()
    except OSError:
        return  # result missing — leave marker, retry next tick

    # Read pre_label from dispatch marker; fall back to config default
    dispatch_path = os.path.join(wt_dir, "dispatch.json")
    pre_label: str = _IMPLEMENTER_CONFIG.pre_label
    base_ref = ""
    dispatch_id = ""
    engine = "claude"
    verification_unconfigured = False
    permission_mode: object = None
    verify_tools: object = None
    if os.path.isfile(dispatch_path):
        try:
            with open(dispatch_path) as fh:
                marker = json.load(fh)
        except (OSError, json.JSONDecodeError):
            marker = None
        if isinstance(marker, dict):
            permission_mode = marker.get("permission_mode")
            verify_tools = marker.get("verify_tools")
            verification_unconfigured = marker.get("verification_unconfigured") is True
            mp = marker.get("pre_label")
            if isinstance(mp, str):
                pre_label = mp
            br = marker.get("base_ref")
            if isinstance(br, str) and br.strip() and br == br.strip():
                base_ref = br
            did = marker.get("dispatch_id")
            if isinstance(did, str):
                dispatch_id = did
            marker_engine = marker.get("engine")
            if isinstance(marker_engine, str):
                engine = marker_engine

    try:
        harvest_result = pipeline.harvest_implementation(
            verification_unconfigured=verification_unconfigured,
            result=result_raw,
            pre_label=pre_label,
        )
    except Exception:
        # Cap persistent parse errors to avoid infinite retries.
        if attempts is not None:
            _bound_failed_harvest(
                n,
                "issue",
                _IMPLEMENTER_CONFIG.in_flight_label,
                wt_dir,
                attempts,
                tick_log,
                _IMPLEMENTER_CONFIG.stage,
            )
        return  # helper crashed — leave marker, retry next tick

    harvest_action = str(harvest_result.get("action", ""))
    harvest_label = str(harvest_result.get("label", ""))
    skills_used, skill_usage_status = _skill_usage_summary(result_raw)
    _log_engine_result_summary(
        tick_log=tick_log,
        number=n,
        stage="implement",
        dispatch_id=dispatch_id,
        result_raw=result_raw,
        harvest_result=harvest_result,
        harvest_action=harvest_action,
        harvest_label=harvest_label,
        skills_used=skills_used,
        skill_usage_status=skill_usage_status,
        engine=engine,
    )
    skip_cleanup = False
    archive_dir = ""

    if harvest_action == "create_pr":
        head_branch = f"agent/issue-{n}"

        # Resume before create: a previous handoff may have created the PR
        # and failed on a later label step. Re-running `gh pr create` against
        # a branch with an open PR fails every retry and would eventually
        # escalate a SUCCESSFUL implementation at the attempt cap — rediscover
        # the PR and retry only the unfinished transitions instead.
        listed_prs: object = None
        try:
            listed_prs = gh_json(
                "pr", "list",
                "--head", head_branch,
                "--state", "open",
                "--json", "number,isCrossRepository",
            )
        except subprocess.CalledProcessError as exc:
            # Fail closed: the PR may already exist, and a create against an
            # unproven branch would fail every retry and burn the bounded
            # harvest attempts toward escalating a successful implementation.
            _log_side_effect_failure(
                tick_log=tick_log,
                number=n,
                stage="implement",
                dispatch_id=dispatch_id,
                operation="gh pr list (handoff resume)",
                exc=exc,
            )
            skip_cleanup = True
        except ValueError:
            skip_cleanup = True
        existing_pr = ""
        if not skip_cleanup:
            if isinstance(listed_prs, list):
                # Only a same-repository head proves this is the pipeline's
                # own PR — a fork can reuse the agent branch name, and
                # adopting its PR would label untrusted content and destroy
                # the genuine implementation scratch.
                for item in listed_prs:
                    number_value = (
                        item.get("number") if isinstance(item, dict) else None
                    )
                    if (
                        isinstance(item, dict)
                        and item.get("isCrossRepository") is False
                        and isinstance(number_value, int)
                        and not isinstance(number_value, bool)
                    ):
                        existing_pr = str(number_value)
                        break
            else:
                # Unreadable payload shape: fail closed like a failed lookup.
                skip_cleanup = True

        pr_url = ""
        if not skip_cleanup and not existing_pr.isdigit() and not base_ref:
            # Only dispatch knows which base the engine actually used. A
            # fresh lookup or config read here could silently retarget its PR.
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage="implement",
                    action="side_effect_failed",
                    reason=(
                        f"cannot create PR for {TRUSTED_HEAD_REPO}: "
                        "dispatch marker has no usable base_ref"
                    ),
                    dispatch_id=dispatch_id,
                )
            skip_cleanup = True
        if not skip_cleanup and not existing_pr.isdigit():
            try:
                pr_metadata_args = _implementation_pr_create_args(
                    wt_dir, n, verification_unconfigured=verification_unconfigured,
                )
                pr_url = gh_text(
                    "pr", "create",
                    "--head", head_branch,
                    "--base", base_ref,
                    *pr_metadata_args,
                )
            except PRMetadataInvalid as exc:
                harvest_action = "escalate"
                harvest_label = pipeline.LABEL_NEEDS_HUMAN
                harvest_result["reason"] = f"PR metadata: {exc}"
                skip_cleanup = True
            except OSError as exc:
                if tick_log:
                    tick_log.log(pr=n, phase="harvest", stage="implement",
                                 action="pr_metadata_retry", reason=str(exc),
                                 dispatch_id=dispatch_id)
                skip_cleanup = True
            except subprocess.CalledProcessError as exc:
                _log_side_effect_failure(
                    tick_log=tick_log,
                    number=n,
                    stage="implement",
                    dispatch_id=dispatch_id,
                    operation="gh pr create",
                    exc=exc,
                )
                skip_cleanup = True

        if not skip_cleanup:
            pr_number_str = (
                existing_pr if existing_pr.isdigit()
                else os.path.basename(pr_url.rstrip("/"))
            )
            try:
                pr_number = int(pr_number_str)
            except ValueError:
                skip_cleanup = True
            else:
                # A rediscovered PR may already carry a pipeline label — the
                # initial relabel landed, or the PR progressed through review
                # while this retry was pending. Re-adding ai:implemented would
                # corrupt that state, so only a label-less PR gets the initial
                # handoff label; an unreadable label set retries next tick.
                needs_handoff_label = True
                try:
                    metadata_notice = _implementation_metadata_notice(wt_dir, n)
                except (OSError, ValueError) as exc:
                    metadata_notice = ""
                    skip_cleanup = True
                    if tick_log:
                        tick_log.log(pr=n, phase="harvest", stage="implement",
                                     action="pr_metadata_retry", reason=str(exc),
                                     dispatch_id=dispatch_id)
                # Rediscovery always checks current evidence, including legacy
                # handoffs without a retained layout decision or verify warning.
                ensure_notices = verification_unconfigured or bool(existing_pr)
                if not skip_cleanup and (existing_pr.isdigit() or ensure_notices):
                    try:
                        handoff = gh_json(
                            "pr", "view", str(pr_number), "--json",
                            "labels,headRefOid,body" if ensure_notices
                            else "labels",
                        )
                    except (subprocess.CalledProcessError, ValueError):
                        handoff = None
                    resumed_labels = _pipeline_labels(handoff)
                    if resumed_labels is None:
                        skip_cleanup = True
                    else:
                        needs_handoff_label = not resumed_labels
                        if ensure_notices:
                            warning_state = _ensure_implementation_warning(
                                number=pr_number, snapshot=handoff,
                                wt_root=wt_root, dispatch_id=dispatch_id,
                                tick_log=tick_log,
                                verification_unconfigured=verification_unconfigured,
                                metadata_notice=metadata_notice,
                            )
                            if warning_state == "held":
                                # A PR hold cannot spend the issue's retry
                                # budget toward an escalation that releases it.
                                return
                            skip_cleanup = warning_state != "clear"
                if not skip_cleanup and needs_handoff_label:
                    try:
                        relabel("pr", pr_number, "", pipeline.LABEL_IMPLEMENTED)
                    except subprocess.CalledProcessError:
                        skip_cleanup = True
                if not skip_cleanup:
                    # Converge the issue to NO pipeline label. Removing only
                    # ai:implementing would leave a dual-label ai:available
                    # behind (partial dispatch transition) and let the next
                    # tick launch a second implementer over the created PR.
                    # A failed convergence must retain the marker and attempt
                    # state: with the issue possibly still ai:available, only
                    # the marker's dispatch dedup blocks a duplicate engine,
                    # so the bounded-harvest path retries the whole handoff.
                    if not _converge_pipeline_labels(
                        "issue", n, "",
                        tick_log=tick_log, phase="harvest",
                        stage=_IMPLEMENTER_CONFIG.stage,
                    ):
                        if tick_log:
                            tick_log.log(
                                pr=n,
                                phase="harvest",
                                stage=_IMPLEMENTER_CONFIG.stage,
                                action="side_effect_failed",
                                reason=(
                                    "converge issue labels after PR creation"
                                ),
                                dispatch_id=dispatch_id,
                            )
                        skip_cleanup = True
                    elif attempts is not None:
                        attempts.clear(n)

    if harvest_action == "escalate":
        comment_body = pipeline.render_implementation_escalation_comment(
            result=result_raw,
            sentinel=harvest_result.get("sentinel"),
            reason=str(harvest_result.get("reason", "")),
            permission_mode=permission_mode,
            verify_tools=verify_tools,
            evidence_path=_harvest_evidence_path(wt_root, "issue", n, dispatch_id),
        )
        # Record the decision before the transition starts, so a failed or
        # interrupted archive move resumes this same escalation next tick
        # instead of re-entering ordinary harvest and publishing the completed
        # outcome as a held result under the pipeline's own terminal label.
        # The record is a required gate: without it a failed archive move
        # would resume as a held-completion replay.
        if not _record_terminal_intent(
            wt_root, "issue", n, dispatch_id, _IMPLEMENTER_CONFIG.stage,
            kind="escalate",
            harvest_label=harvest_label,
            comment_body=comment_body,
        ):
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=_IMPLEMENTER_CONFIG.stage,
                    action="terminal_intent_failed",
                    reason=(
                        "escalation decision not durably recorded; "
                        "transition not started"
                    ),
                    dispatch_id=dispatch_id,
                )
            return
        _complete_escalate_transition(
            n=n,
            entity="issue",
            wt_dir=wt_dir,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            stage=_IMPLEMENTER_CONFIG.stage,
            harvest_label=harvest_label,
            comment_body=comment_body,
            attempts=attempts,
            tick_log=tick_log,
        )
        return

    elif harvest_action == "retry":
        if not _handle_transient_engine_result(
            entity="issue",
            number=n,
            config=_IMPLEMENTER_CONFIG,
            pre_label=pre_label,
            engine=engine,
            result_raw=result_raw,
            harvest_result=harvest_result,
            dispatch_id=dispatch_id,
            wt_root=wt_root,
            wt_dir=wt_dir,
            tick_log=tick_log,
            attempts=attempts,
            transient_cooldown_seconds=transient_cooldown_seconds,
            transient_cooldown_reset_buffer_seconds=(
                transient_cooldown_reset_buffer_seconds
            ),
        ):
            return

    if skip_cleanup:
        _bound_failed_harvest(
            n, "issue", _IMPLEMENTER_CONFIG.in_flight_label, wt_dir, attempts,
            tick_log, _IMPLEMENTER_CONFIG.stage,
        )
        return
    if harvest_action == "create_pr" and tick_log:
        tick_log.log(
            pr=n, phase="harvest", stage="implement", action="pr_handoff",
            reason="PR handoff confirmed after issue custody convergence",
            engine=engine, dispatch_id=dispatch_id,
            details={"issue_number": n, "pr_number": pr_number,
                     "created": not existing_pr.isdigit()},
        )
    if archive_dir and not _finalize_harvest_archive(
        wt_dir,
        archive_dir,
        number=n,
        stage=_IMPLEMENTER_CONFIG.stage,
        phase="harvest",
        dispatch_id=dispatch_id,
        tick_log=tick_log,
    ):
        if tick_log:
            tick_log.log(
                pr=n,
                phase="harvest",
                stage=_IMPLEMENTER_CONFIG.stage,
                action="archive_failed",
                reason="retaining scratch after implementer escalation",
                dispatch_id=dispatch_id,
            )
        return
    cleanup_scratch(wt_dir)


def harvest_implementers(
    tick_log: TickLog | None = None,
    target: tuple[str, int] | None = None,
    *,
    wt_root: str | None = None,
    transient_cooldown_seconds: int = DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
    transient_cooldown_reset_buffer_seconds: int = (
        TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS
    ),
) -> bool:
    """Harvest finished implementer engines.

    Args:
        tick_log: Tick log handle, or ``None``.
        target: Optional ``(type, number)`` to restrict to a single item.
        wt_root: Worktree root directory, or ``None`` to resolve
            :data:`WT_ROOT` at call time.
        transient_cooldown_seconds: Provider cooldown after a transient
            result that names no reset time. The tick passes its validated
            value; the default serves direct callers.
        transient_cooldown_reset_buffer_seconds: Margin added to a reset time
            the transient result names.

    Returns:
        ``True`` if reconciliation succeeded, ``False`` if enumeration
        failed and DISPATCH should be gated.
    """
    if wt_root is None:
        wt_root = WT_ROOT
    attempts = Attempts(wt_root, _IMPLEMENTER_CONFIG.attempts_key_prefix)
    try:
        decisions, enum_ok = reconcile_engine(
            _IMPLEMENTER_CONFIG, tick_log, target,
            wt_root=wt_root, attempts=attempts,
        )
    except subprocess.CalledProcessError:
        return False

    transition_ok = _execute_reconcile_actions(
        decisions, _IMPLEMENTER_CONFIG, wt_root, tick_log, attempts,
    )

    # Process harvest decisions — read result.json, create PR / escalate
    for decision in decisions:
        action = str(decision.get("action", ""))
        if action == "harvest":
            _harvest_one_implementer(
                decision, wt_root, tick_log, attempts,
                transient_cooldown_seconds=transient_cooldown_seconds,
                transient_cooldown_reset_buffer_seconds=(
                    transient_cooldown_reset_buffer_seconds
                ),
            )

    return enum_ok and transition_ok


# Controller-owned receipts proving a harvest comment already landed.
#
# The reviewer dedupes against the PR's comment history because its comment
# doubles as the iteration counter and must be exact. Fixer comments also feed
# authenticated re-review context, but their publication and escalation comments
# need only local replay-safety across a harvest's own
# retry window. A local receipt covers that window without adding a `gh` read
# whose failure would stall the harvest.
#
# The receipts live under WT_ROOT and *not* inside the item's scratch: a fixer
# scratch is a checked-out worktree of the PR branch, so anything stored there
# is author-controlled — a tracked file at the receipt path would forge the
# "comment landed" proof and let the label move with nothing published. WT_ROOT
# is controller-owned (the same place `.iter` and `.harvest-evidence` live) and
# is scanned only for `pr-`/`issue-` prefixed entries, so a dotted subdirectory
# is inert there.
#
# Because a receipt now outlives the scratch it is keyed to the marker's
# `dispatch_id` *and* to the decision it published: a receipt from an earlier
# fixer run can never suppress the comment for a later one, and a retry of the
# same run that decides differently publishes its new outcome rather than
# inheriting the old comment. It records the posted comment's URL as the landed
# proof, so a half-written or hand-made file is not honoured.
_RECEIPT_DIR = ".receipts"
_FIX_COMMENT_KIND = "fix-comment"
_CAP_COMMENT_KIND = "cap-escalation"
_ESCALATION_COMMENT_KIND = "transient-escalation"
_DISPATCH_COMMENT_KIND = "dispatch-escalation"

# The dispatch escalations have no `dispatch_id` to key a receipt to: they fire
# before a marker is published, or over one whose identity is exactly what could
# not be read. A per-run id would also be the wrong key — each tick re-attempts
# the *same* failed setup under a new id, so every retry would repost. The
# constant leaves the decision key (target label + rendered body) doing the whole
# job: the same failure is suppressed, a different one publishes, and the receipt
# is retired by `_discard_comment_receipts` once the label lands, so a later
# recurrence after a human resets the item explains itself again.
_DISPATCH_RECEIPT_ID = "dispatch-escalation"


def _comment_receipt_path(
    wt_root: str, entity: str, number: int, kind: str,
) -> str:
    """Return the controller-owned receipt path for one harvest comment.

    Args:
        wt_root: The worktree root (controller-owned state lives here).
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        kind: The comment kind (:data:`_FIX_COMMENT_KIND` or
            :data:`_CAP_COMMENT_KIND`).

    Returns:
        Absolute path to the receipt file.
    """
    return os.path.join(
        wt_root, _RECEIPT_DIR, f"{entity}-{number}-{kind}.json",
    )


def _decision_key(target_label: str, comment_body: str) -> str:
    """Return the identity of one publishable harvest outcome.

    A receipt suppresses a repost, so it has to prove that the *same* outcome
    was published — not merely that the dispatch published something. The
    decision is recomputed from a fresh head read on every retry, so a
    dispatch-only key would let a first attempt's escalation comment stand while
    a later attempt lands ``ai:revised``. Keying on the label the post is about
    to justify plus the exact body that justifies it makes any change of
    outcome, head, or engine report a different receipt.

    Args:
        target_label: The label this comment explains.
        comment_body: The rendered comment body.

    Returns:
        A hex digest identifying the outcome.
    """
    digest = hashlib.sha256()
    digest.update(target_label.encode())
    digest.update(b"\0")
    digest.update(comment_body.encode())
    return digest.hexdigest()


def _comment_already_posted(
    receipt_path: str, dispatch_id: str, decision_key: str,
) -> bool:
    """Return whether a valid receipt proves *this* outcome's comment landed.

    A receipt is honoured only when it parses as controller-written JSON, names
    the same ``dispatch_id``, records the same decision key, and carries the URL
    GitHub returned for the posted comment. Anything else — missing, truncated,
    hand-made, left by an earlier dispatch, or written for a different outcome
    of this dispatch — is treated as "not posted".

    Args:
        receipt_path: Path returned by :func:`_comment_receipt_path`.
        dispatch_id: The current run's dispatch id.
        decision_key: Key returned by :func:`_decision_key` for the outcome
            about to be published.

    Returns:
        ``True`` only when the comment for *this* dispatch and outcome is proven
        posted.
    """
    try:
        with open(receipt_path) as fh:
            receipt = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict):
        return False
    return (
        receipt.get("dispatch_id") == dispatch_id
        and receipt.get("decision_key") == decision_key
        and isinstance(receipt.get("comment_url"), str)
        and bool(receipt.get("comment_url"))
    )


def _pr_body_write_authority(
    *,
    number: int,
    expected_head: str,
    provenance: PRBodyProvenance | None,
    candidate: str,
    dispatch_id: str,
    tick_log: TickLog | None,
    expected_body: str | None = None,
    stage: str = "fix",
    expected_labels: set[str] | None = None,
) -> str:
    """Re-check, at the write itself, everything the replacement assumed.

    Every other authority this harvest used is old by the time it gets here:
    reconcile read the labels near the top of the tick, and the classification
    point-read happened before ``result.json`` was even parsed. GitHub offers no
    conditional write for a PR body — the REST guidance is explicit that
    conditional requests do not apply to unsafe methods — so this cannot be a
    compare-and-swap. What it can do is shrink the unguarded window from a whole
    harvest to one API call, and refuse outright on anything it does see.

    A hold is the reason this is not merely a race-narrowing device.
    ``pr-guidelines.md`` gives a human the right to stop a dispatched run at any
    time, and says the hold outranks even a completed engine result. A hold that
    lands after reconcile has to beat this write too, because this write is the
    one thing in the pipeline nobody can undo.

    Args:
        number: The PR about to be edited.
        expected_head: The head OID the classification read returned and the
            candidate was validated against.
        provenance: What dispatch pinned about an engine-rewritten body.
            A controller-only warning prefix instead supplies expected_body;
            without either source, the write is unreadable and withheld.
        candidate: The normalised replacement body.
        dispatch_id: The run's dispatch id, for the log.
        tick_log: Tick log handle, or ``None``.
        expected_body: For a controller-only warning prefix, the live body
            read at harvest. This authorizes only that addition; engine rewrites
            continue to require dispatch-time provenance.
        stage: Harvest stage owning the write and its diagnostic record.
        expected_labels: Implementation handoff's workflow snapshot. A changed
            set requires a retry so a progressed PR keeps its current label.

    Returns:
        ``"clear"`` to proceed, or the reason not to: ``"held"`` (a human hold
        arrived), ``"drifted"`` (head, body, or expected labels changed),
        or ``"unreadable"`` (the authority could not be established).
    """
    try:
        live = gh_json(
            "pr", "view", str(number), "--json", "labels,headRefOid,body",
        )
    except (subprocess.CalledProcessError, ValueError):
        live = None
    labels = _pipeline_labels(live)
    expected_digest = (
        _pr_body_digest(_normalized_pr_body(expected_body))
        if expected_body is not None else provenance.digest if provenance else None
    )
    if not isinstance(live, dict) or labels is None or expected_digest is None:
        verdict = "unreadable"
    elif pipeline.HOLD_LABELS & set(labels):
        verdict = "held"
    elif expected_labels is not None and set(labels) != expected_labels:
        verdict = "drifted"
    else:
        head = live.get("headRefOid")
        body = live.get("body")
        if not isinstance(head, str) or not isinstance(body, str):
            verdict = "unreadable"
        elif head != expected_head:
            verdict = "drifted"
        else:
            # Same two accepted states as the classification, for the same
            # reasons: the body this run already published (a retry), and the
            # body dispatch pinned (or the live snapshot before a controller
            # prefix). Anything else moved under the candidate and is not this
            # write's to overwrite.
            current = _normalized_pr_body(body)
            verdict = (
                "clear"
                if current == candidate
                or _pr_body_digest(current) == expected_digest
                else "drifted"
            )
    if verdict != "clear" and tick_log:
        tick_log.log(
            pr=number,
            phase="harvest",
            stage=stage,
            action="pr_body_revision_withheld",
            reason=verdict,
            dispatch_id=dispatch_id,
        )
    return verdict


def _ensure_implementation_warning(
    *,
    number: int,
    snapshot: object,
    wt_root: str,
    dispatch_id: str,
    tick_log: TickLog | None,
    verification_unconfigured: bool = True,
    metadata_notice: str = "",
) -> str:
    """Require visible evidence on created and rediscovered PRs before handoff."""
    labels = _pipeline_labels(snapshot)
    if not isinstance(snapshot, dict) or labels is None:
        return "unreadable"
    if pipeline.HOLD_LABELS & set(labels):
        return "held"
    head, body = snapshot.get("headRefOid"), snapshot.get("body")
    if not isinstance(head, str) or not head or not isinstance(body, str):
        return "unreadable"
    # The retained notice records what this handoff owes, not which sections a
    # human's newer live report still lacks. Availability notices stay historical.
    if _is_pr_layout_notice(metadata_notice):
        candidate = _with_metadata_notice(body, _pr_layout_notice(body))
    else:
        candidate = _with_metadata_notice(body, metadata_notice) if metadata_notice else body
    candidate = _with_implementation_evidence_notice(candidate)
    candidate = _normalized_pr_body(pipeline.with_verification_warning(
        candidate, required=verification_unconfigured,
    ))
    if len(candidate) > _PR_BODY_MAX_CHARS or len(candidate.encode()) > _PR_BODY_MAX_CHARS:
        return "oversized"
    authority = _pr_body_write_authority(
        number=number, expected_head=head, provenance=None, candidate=candidate,
        expected_body=body, dispatch_id=dispatch_id, tick_log=tick_log,
        stage="implement", expected_labels=set(labels),
    )
    if authority != "clear":
        return authority
    if candidate == _normalized_pr_body(body):
        return "clear"
    return "clear" if _apply_pr_body_revision(
        number=number, wt_root=wt_root, body=candidate,
        dispatch_id=dispatch_id, tick_log=tick_log, stage="implement",
    ) else "write-failed"


def _apply_pr_body_revision(
    *,
    number: int,
    wt_root: str,
    body: str,
    dispatch_id: str,
    tick_log: TickLog | None,
    stage: str = "fix",
) -> bool:
    """Publish a guarded engine revision or controller warning prefix.

    The first of the two GitHub writes a fixer harvest makes, and it sits next
    to the second for that reason. The body file is staged under the
    controller-owned receipt directory rather than the fixer's worktree, on the
    same rule as :func:`_post_harvest_comment`: nothing handed to ``gh`` is read
    back out of a branch-controlled tree.

    Unlike the comment, this write needs no receipt to be idempotent. Republishing
    a body is indistinguishable from having published it once, and
    ``tick._fixer_body_revision`` already reads a live body equal to the
    candidate as this run's own landed write rather than a conflict.

    Args:
        number: The PR to edit.
        wt_root: The worktree root holding controller-owned state.
        body: The validated replacement body.
        dispatch_id: The run's dispatch id, for the failure log.
        tick_log: Tick log handle, or ``None``.
        stage: Harvest stage owning the write and its diagnostic record.

    Returns:
        ``True`` when the live body is the revision; ``False`` when the write
        failed, leaving the caller to retain custody.
    """
    body_path = os.path.join(
        wt_root, _RECEIPT_DIR, f"pr-{number}-body-revision.md",
    )
    try:
        os.makedirs(os.path.dirname(body_path), exist_ok=True)
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(f"{body}\n")
        gh("pr", "edit", str(number), "--body-file", body_path)
    except subprocess.CalledProcessError as exc:
        _log_side_effect_failure(
            tick_log=tick_log,
            number=number,
            stage=stage,
            dispatch_id=dispatch_id,
            operation="gh pr edit --body-file",
            exc=exc,
        )
        return False
    except OSError:
        if tick_log:
            tick_log.log(
                pr=number,
                phase="harvest",
                stage=stage,
                action="side_effect_failed",
                reason="staging the PR body revision failed",
                dispatch_id=dispatch_id,
            )
        return False
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass
    return True


def _post_harvest_comment(
    *,
    entity: str,
    number: int,
    wt_root: str,
    dispatch_id: str,
    comment_body: str,
    kind: str,
    target_label: str,
) -> bool:
    """Post a harvest comment on the PR or issue, normally once per outcome.

    The guarantee is at-least-once, not exactly-once: if the post lands and the
    process dies before the receipt is written, the next tick posts one
    duplicate. Failing a landed post would be worse, so that crash window is
    accepted rather than closed.

    The body file is written under the controller-owned receipt directory
    rather than the item's scratch, so nothing is staged inside a
    branch-controlled worktree.

    Args:
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number to comment on.
        wt_root: The worktree root holding controller-owned state.
        dispatch_id: The run's dispatch id; half of the suppression key.
        comment_body: The rendered comment body.
        kind: The comment kind (:data:`_FIX_COMMENT_KIND` or
            :data:`_CAP_COMMENT_KIND`).
        target_label: The label this comment explains. With the body it forms
            the decision key, so a retry that decides differently publishes the
            new outcome instead of hiding behind the old receipt.

    Returns:
        ``True`` when this outcome's comment is on the item — freshly posted, or
        proven posted by an earlier attempt at this same dispatch and outcome.
        ``False`` when the post failed.
    """
    receipt_path = _comment_receipt_path(wt_root, entity, number, kind)
    decision_key = _decision_key(target_label, comment_body)
    if _comment_already_posted(receipt_path, dispatch_id, decision_key):
        return True

    body_path = f"{receipt_path}.body.txt"
    try:
        os.makedirs(os.path.dirname(receipt_path), exist_ok=True)
        with open(body_path, "w") as fh:
            fh.write(comment_body)
        posted = gh(entity, "comment", str(number), "--body-file", body_path)
    except (OSError, subprocess.CalledProcessError):
        return False
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass

    # Advisory receipt: a failed write costs at most one duplicate comment on a
    # later retry, which is strictly better than failing a landed post.
    stdout = getattr(posted, "stdout", "")
    comment_url = stdout.strip() if isinstance(stdout, str) else ""
    try:
        with open(receipt_path, "w") as fh:
            json.dump({
                "dispatch_id": dispatch_id,
                "decision_key": decision_key,
                # `gh` prints the created comment's URL; when a mocked or future
                # `gh` prints nothing, record that the post itself succeeded so
                # the receipt still reads as landed proof.
                "comment_url": comment_url or "posted",
            }, fh)
    except OSError:
        pass
    return True


def _discard_comment_receipts(wt_root: str, entity: str, number: int) -> None:
    """Drop an item's comment receipts once its harvest reaches a terminal state.

    Receipts outlive the scratch, so the terminal transition is what retires
    them. A failed unlink is harmless: the next dispatch carries a new
    ``dispatch_id``, which no stale receipt can match.

    The dispatch-escalation receipt is retired here too, and it is the one kind
    that needs to be: its id is a constant, so nothing about a later dispatch
    can fail to match it. :func:`_end_dispatch_escalation` retires it on the
    dispatch side; this call covers the item reaching a terminal harvest state
    with one still on disk.

    Args:
        wt_root: The worktree root holding controller-owned state.
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
    """
    for kind in (
        _FIX_COMMENT_KIND, _CAP_COMMENT_KIND, _ESCALATION_COMMENT_KIND,
        _DISPATCH_COMMENT_KIND,
    ):
        try:
            os.unlink(_comment_receipt_path(wt_root, entity, number, kind))
        except OSError:
            pass


_STAGE_CONFIGS = {
    "review": _REVIEWER_CONFIG,
    "implement": _IMPLEMENTER_CONFIG,
    "fix": _FIXER_CONFIG,
}


def _dispatch_attempts(stage: str) -> Attempts:
    """Return the durable attempt counter that bounds one item's dispatch.

    Resolved from the dispatching stage's own config rather than from the
    entity, so a dispatch escalation always counts into the same file that
    stage's harvest cap reads. Keying on the entity would have worked only
    because ``review`` and ``fix`` share a prefix today — a silent dependency
    that would stop bounding the fixer cap the moment the two diverged.

    Args:
        stage: ``"review"``, ``"implement"``, or ``"fix"``. An unknown stage
            falls back to the PR-shaped counter rather than inventing a new
            namespace, since a counter nothing else reads would not bound
            anything.

    Returns:
        The item's attempt counter, rooted at the live ``WT_ROOT``.
    """
    config = _STAGE_CONFIGS.get(stage, _REVIEWER_CONFIG)
    return Attempts(WT_ROOT, config.attempts_key_prefix)


def _publish_dispatch_escalation(
    *,
    entity: str,
    number: int,
    stage: str,
    outcome: str,
    reason: str,
    engine: str = "",
    tick_log: TickLog | None = None,
) -> bool:
    """Publish a dispatch escalation, or spend one of its bounded retries.

    DISPATCH escalates with no engine result behind it, so unlike the harvest
    paths there is no ``.harvest-evidence/`` bundle to fall back on and — for a
    setup failure — no scratch either. The comment is the only account that
    survives, so the relabel is gated on it.

    Gating alone would be worse than the silence it replaces: a permanently
    unpostable comment would hold the item at its pickup label on every future
    tick, where it reads as ordinary available work rather than as something
    stuck. The durable attempt counter bounds that, and at the cap the
    transition takes the same exemption the attempt cap itself takes — the label
    is the load-bearing signal, the comment is the explanation, and the miss is
    logged as ``escalation_comment_failed``.

    A counter that cannot be written takes the exemption immediately, for the
    reason :func:`_handle_transient_engine_result` documents: the budget that
    would bound the retry is exactly what failed to persist.

    Args:
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        stage: The dispatch stage, for the comment and the tick log.
        outcome: One of :data:`pipeline.OUTCOME_DISPATCH_SETUP_FAILED`,
            :data:`pipeline.OUTCOME_AMBIGUOUS_MARKER`, or
            :data:`pipeline.OUTCOME_REVIEW_POLICY_TOUCHED`.
        reason: The failure reason the tick recorded.
        engine: The engine that would have been launched, or ``""``.
        tick_log: Tick log handle, or ``None``.

    Returns:
        ``True`` when the relabel may proceed — the comment is on the item, or
        the retry budget for publishing it is spent. ``False`` when the item is
        held at its pre-dispatch label for the next tick to retry.
    """
    if _post_harvest_comment(
        entity=entity,
        number=number,
        wt_root=WT_ROOT,
        dispatch_id=_DISPATCH_RECEIPT_ID,
        comment_body=pipeline.render_dispatch_escalation_comment(
            stage=stage, outcome=outcome, reason=reason, engine=engine,
        ),
        kind=_DISPATCH_COMMENT_KIND,
        target_label=pipeline.LABEL_NEEDS_HUMAN,
    ):
        return True

    attempts = _dispatch_attempts(stage)
    if attempts.read(number) < pipeline.MAX_DISPATCH_ATTEMPTS:
        try:
            attempts.incr(number)
        except OSError:
            # Budget unwritable: holding now would be a retry nothing can
            # terminate, so fall through and take the exemption immediately.
            pass
        else:
            if tick_log:
                tick_log.log(
                    pr=number, phase="dispatch", stage=stage,
                    action="escalation_comment_retry",
                    reason=(
                        "holding the pre-dispatch label until the escalation "
                        "is published"
                    ),
                    engine=engine,
                )
            return False
    if tick_log:
        tick_log.log(
            pr=number, phase="dispatch", stage=stage,
            action="escalation_comment_failed",
            reason="escalating to the terminal label without an explanation",
            engine=engine,
        )
    return True


def _end_dispatch_escalation(
    entity: str, number: int, stage: str, *, retains_marker: bool = False,
) -> None:
    """Retire the local state a landed dispatch escalation was bounded by.

    Called only after the terminal label is on the item: a counter cleared
    before that lands would un-bound a retry that is still running, and a
    receipt dropped before it lands would let the next tick repost.

    The two retirements have different scopes, and only the counter is
    conditional. The attempt counter is per *item*, not per purpose — the
    harvest cap reads the same file — so on a path that leaves the marker on
    disk, a mid-retry harvest may be counting resultless failures into it, and
    clearing would reset that budget and delay its own cap-based cleanup of the
    very scratch this path retained. It is therefore left to whoever still owns
    the marker. The residual cost is that this path's own failed-post increments
    survive into the item's next cycle, which is the conservative direction — an
    item whose escalation could not be published starts that cycle with less
    budget, not more.

    This path's *own* receipt is retired on both paths, and must be: its key is
    a constant, so nothing about a later dispatch can fail to match it, and
    leaving it would suppress the comment for the next recurrence of the same
    failure after a human resets the item. Only the harvest's receipts are left
    alone — those are keyed to a live run's ``dispatch_id`` and are that run's
    to retire.

    Args:
        entity: ``"pr"`` or ``"issue"``.
        number: The PR or issue number.
        stage: The dispatching stage, which selects the counter — the same one
            that stage's harvest cap reads.
        retains_marker: Whether this escalation left the item's scratch and
            marker in place. ``True`` for the ambiguous-marker path, which
            cannot prove the marker is safe to clear.
    """
    if not retains_marker:
        _dispatch_attempts(stage).clear(number)
    # Only this path's own receipt, on either path: the harvest receipts are
    # keyed to a live run's `dispatch_id` and are that run's to retire.
    try:
        os.unlink(
            _comment_receipt_path(
                WT_ROOT, entity, number, _DISPATCH_COMMENT_KIND,
            ),
        )
    except OSError:
        pass


def _fixer_publication_authority(number: int, expected_head: str) -> str:
    """Point-read the head and hold immediately before a new-report mutation."""
    try:
        snapshot = gh_json("pr", "view", str(number), "--json", "headRefOid,labels,state")
    except (subprocess.CalledProcessError, ValueError):
        return "unreadable"
    labels = _pipeline_labels(snapshot)
    if not isinstance(snapshot, dict) or labels is None:
        return "unreadable"
    if pipeline.HOLD_LABELS & set(labels):
        return "held"
    if snapshot.get("state") != "OPEN" or snapshot.get("headRefOid") != expected_head:
        return "drifted"
    return "clear"


def _harvest_one_fixer(
    decision: dict[str, object],
    wt_root: str,
    tick_log: TickLog | None = None,
    attempts: Attempts | None = None,
    *,
    transient_cooldown_seconds: int = DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
    transient_cooldown_reset_buffer_seconds: int = (
        TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS
    ),
) -> None:
    """Process a completed fixer harvest: read result, recheck or escalate.

    Reads ``result.json`` from the PR's worktree, calls
    ``pipeline.harvest_fix``, and executes the prescribed side effects:
    ``recheck`` → relabel to ``ai:revised`` + bump iteration cache;
    ``escalate`` → publish the reason and relabel to ``ai:needs-human-decision``.

    On any ``gh`` failure, the marker and scratch survive for the next tick.
    """
    n = decision.get("pr_number")
    if not isinstance(n, int):
        return

    wt_dir = os.path.join(wt_root, f"pr-{n}")
    # A cap escalation this dispatch already published owns the outcome. Its
    # comment says the terminal label supersedes whatever the run concluded, so
    # recomputing a decision here could only land a contradicting label.
    if _resume_terminal_escalation(n, "pr", wt_dir, attempts, tick_log):
        return

    result_path = os.path.join(wt_dir, "result.json")
    result_present = True
    try:
        result_bytes = Path(result_path).read_bytes()
    except FileNotFoundError:
        if decision.get("action") != "report_missing":
            return
        result_present, result_bytes = False, b""
    except OSError:
        return  # result missing — leave marker, retry next tick
    try:
        result_raw = result_bytes.decode("utf-8")
    except UnicodeError:
        # Parsing fails closed; archive copies the original bytes unchanged.
        result_raw = ""

    # Read pre_label from dispatch marker; fall back to config default
    dispatch_path = os.path.join(wt_dir, "dispatch.json")
    pre_label: str = _FIXER_CONFIG.pre_label
    dispatch_id = ""
    engine = "claude"
    verification_unconfigured = False
    pinned_head = ""
    review_sha256 = ""
    provenance: PRBodyProvenance | None = None
    marker = {}
    if os.path.isfile(dispatch_path):
        try:
            with open(dispatch_path) as fh:
                marker = json.load(fh)
        except (OSError, json.JSONDecodeError):
            marker = None
        if isinstance(marker, dict):
            verification_unconfigured = marker.get("verification_unconfigured") is True
            mp = marker.get("pre_label")
            if isinstance(mp, str):
                pre_label = mp
            did = marker.get("dispatch_id")
            if isinstance(did, str):
                dispatch_id = did
            marker_engine = marker.get("engine")
            if isinstance(marker_engine, str):
                engine = marker_engine
            mh = marker.get("head_sha")
            if isinstance(mh, str):
                pinned_head = mh
            review_digest = marker.get("review_sha256")
            if isinstance(review_digest, str):
                review_sha256 = review_digest
            # Body-revision provenance: the fingerprint and closure contract of
            # the body dispatch handed the engine. Absent on markers written
            # before the revision path, and on PRs whose body pinned nothing.
            provenance = PRBodyProvenance.from_marker(marker.get("pr_body"))

    # Live-head point-read (push-verification authority). The FIXES_PUSHED
    # sentinel is the engine's claim, not proof: a fast-forward push always
    # advances the head, so decide_fix_harvest treats a success claim with an
    # unchanged head as contradictory output and escalates. The separate
    # NO_CHANGES completion requires reasoned no-change blocks and carries a
    # stable fixer token into the next review identity, keeping it countable.
    # Failed or malformed reads retain the marker for the next tick. Skipped
    # when the marker carries no pinned head (pre-upgrade markers): without
    # dispatch-time provenance no contradiction can be proven.
    #
    # The same read carries the live body, which is the other half of the
    # PR-body revision decision: whether the body still matches the one dispatch
    # handed the engine. It rides along rather than costing a second call, and
    # an absent or mistyped ``body`` only withholds the replacement — it must
    # not block a harvest that has no revision to publish.
    current_head = ""
    live_body: str | None = None
    if pinned_head:
        try:
            live_head_raw = gh_json("pr", "view", str(n),
                                    "--json", "headRefOid,body")
        except (subprocess.CalledProcessError, ValueError):
            return
        _live_head = (
            live_head_raw.get("headRefOid")
            if isinstance(live_head_raw, dict) else None
        )
        if not isinstance(_live_head, str) or not _live_head:
            return
        current_head = _live_head
        _live_body = (
            live_head_raw.get("body")
            if isinstance(live_head_raw, dict) else None
        )
        if isinstance(_live_body, str):
            live_body = _live_body

    revision_state, revision_body = _fixer_body_revision(
        wt_dir=wt_dir,
        dispatch_id=dispatch_id,
        current_head=current_head,
        provenance=provenance,
        live_body=live_body,
        verification_unconfigured=verification_unconfigured,
    )

    try:
        harvest_result = pipeline.harvest_fix(
            verification_unconfigured=verification_unconfigured,
            result=result_raw,
            pre_label=pre_label,
            pinned_head=pinned_head,
            current_head=current_head,
            body_revision=revision_state,
            dispatch_id=dispatch_id,
            review_sha256=review_sha256,
            report_protocol_version=marker.get("report_protocol_version"),
            review_handoff=marker.get("review_handoff"),
        )
    except Exception:
        # Cap persistent parse errors to avoid infinite retries.
        if attempts is not None:
            _bound_failed_harvest(
                n,
                "pr",
                _FIXER_CONFIG.in_flight_label,
                wt_dir,
                attempts,
                tick_log,
                _FIXER_CONFIG.stage,
            )
        return  # helper crashed — leave marker, retry next tick

    harvest_action = str(harvest_result.get("action", ""))
    harvest_label = str(harvest_result.get("label", ""))
    skills_used, skill_usage_status = _skill_usage_summary(result_raw)
    _log_engine_result_summary(
        tick_log=tick_log,
        number=n,
        stage="fix",
        dispatch_id=dispatch_id,
        result_raw=result_raw,
        harvest_result=harvest_result,
        harvest_action=harvest_action,
        harvest_label=harvest_label,
        skills_used=skills_used,
        skill_usage_status=skill_usage_status,
        engine=engine,
    )

    fixer_body = (
        str(harvest_result.get("comment_body", ""))
        if harvest_result.get("post_comment") is True else ""
    )

    archive_dir = ""
    if "report_protocol_version" in marker:
        rendered = str(harvest_result.get("rendered_report") or fixer_body)
        try:
            pipeline.atomic_write(os.path.join(wt_dir, "fix-rendered.md"), rendered)
            pipeline.atomic_write(os.path.join(wt_dir, "fix-manifest.json"), json.dumps({
                "report_protocol_version": marker["report_protocol_version"],
                "dispatch_id": dispatch_id, "review_handoff": marker.get("review_handoff"),
                "source_sha256": hashlib.sha256(result_bytes).hexdigest(),
                "source_present": result_present,
                "rendered_sha256": pipeline.review_reports.digest(rendered),
                "validation_reason": harvest_result.get("validation_reason", ""),
                "outcome": harvest_action, "published_head": current_head,
            }))
        except (OSError, UnicodeError):
            _bound_failed_harvest(n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir,
                                  attempts, tick_log, _FIXER_CONFIG.stage)
            return
        archive_dir = _archive_harvest_evidence(
            wt_root=wt_root, wt_dir=wt_dir, kind="pr", number=n, dispatch_id=dispatch_id,
            tick_log=tick_log, stage="fix", reuse_existing=True,
            required_files=("dispatch.json", "fix-rendered.md", "fix-manifest.json")
                           + (("result.json",) if result_present else ()),
        )
        if not archive_dir or not all(
            _copy_evidence_file(wt_dir, archive_dir, name)
            for name in ("fix-rendered.md", "fix-manifest.json")
        ):
            _bound_failed_harvest(n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir,
                                  attempts, tick_log, _FIXER_CONFIG.stage)
            return

    if os.path.lexists(os.path.join(wt_dir, _PR_BODY_REVISION_FILE)):
        try:
            _record_pr_metadata(
                wt_dir, n,
                {"outcome": revision_state, "harvest_action": harvest_action,
                 "body": revision_body, "head": current_head,
                 "normalization": "CRLF to LF and surrounding whitespace (GitHub round trip)"},
                kind="pr", source_file=_PR_BODY_REVISION_FILE,
            )
        except OSError as exc:
            if tick_log:
                tick_log.log(pr=n, phase="harvest", stage="fix",
                             action="pr_metadata_retry", reason=str(exc),
                             dispatch_id=dispatch_id)
            _bound_failed_harvest(
                n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir, attempts,
                tick_log, _FIXER_CONFIG.stage,
            )
            return

    if revision_state and tick_log:
        tick_log.log(
            pr=n,
            phase="harvest",
            stage=_FIXER_CONFIG.stage,
            action="pr_body_revision",
            reason=revision_state,
            dispatch_id=dispatch_id,
        )

    if "report_protocol_version" in marker:
        authority = _fixer_publication_authority(n, current_head)
        if authority != "clear":
            if authority != "held":
                _bound_failed_harvest(n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir,
                                      attempts, tick_log, _FIXER_CONFIG.stage)
            return

    if harvest_action == "escalate":
        # Terminal transitions record their decision before *any* remote
        # mutation, and for the fixer the run comment is that mutation: its
        # body names the terminal label. Publishing it ahead of the gate would
        # leave a live explanation with no recovery owner, and a hold arriving
        # before the retry would route the completed marker through the
        # generic held-completion publication this intent exists to prevent.
        # The comment body is captured because the completion posts it — a
        # different body would defeat the receipt's decision-key suppression.
        if not _record_terminal_intent(
            wt_root, "pr", n, dispatch_id, _FIXER_CONFIG.stage,
            kind="escalate",
            harvest_label=harvest_label,
            comment_body=fixer_body,
        ):
            if tick_log:
                tick_log.log(
                    pr=n, phase="harvest", stage=_FIXER_CONFIG.stage,
                    action="terminal_intent_failed",
                    reason=(
                        "escalation decision not durably recorded; "
                        "transition not started"
                    ),
                    dispatch_id=dispatch_id,
                )
            return
        _complete_escalate_transition(
            n=n,
            entity="pr",
            wt_dir=wt_dir,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            stage=_FIXER_CONFIG.stage,
            harvest_label=harvest_label,
            comment_body=fixer_body,
            attempts=attempts,
            tick_log=tick_log,
        )
        return

    # Republish the story surface before anything else this harvest makes
    # visible. The PR body is what the next reviewer reads first, so it has to
    # be true by the time ``ai:revised`` invites one — and it goes ahead of the
    # comment as well, so a comment claiming the body was replaced is never
    # posted over a body that was not. Only a run that reached re-review
    # publishes one: an escalated or requeued run may not have pushed the head
    # the replacement describes.
    if (
        harvest_action == "recheck" and (
            verification_unconfigured
            or (live_body is not None and pipeline.VERIFICATION_WARNING in live_body)
        )
        and revision_state not in {"ready", "warning-only"}
        and (live_body is None or not pipeline.has_visible_verification_warning(live_body))
    ):
        # Never publish a successful revision without its durable warning.
        # Missing/conflicting/oversized body evidence retains custody for retry.
        _bound_failed_harvest(
            n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir, attempts,
            tick_log, _FIXER_CONFIG.stage,
        )
        return
    if harvest_action == "recheck" and revision_state in {"ready", "warning-only"}:
        authority = _pr_body_write_authority(
            number=n,
            expected_head=current_head,
            provenance=provenance,
            candidate=revision_body,
            expected_body=live_body if revision_state == "warning-only" else None,
            dispatch_id=dispatch_id,
            tick_log=tick_log,
        )
        if authority != "clear":
            # The comment this harvest rendered says the body was replaced, so
            # nothing about this run may be published now that it was not. Every
            # input is durable — marker, scratch, candidate, label — and the
            # next tick re-derives the outcome against what it finds then: the
            # hold gate in reconcile for ``held``, a fresh classification (and
            # so a truthful ``conflict``) for ``drifted``.
            #
            # ``held`` deliberately does not bump the attempt counter. The
            # counter escalates to ``ai:needs-human-decision`` at its cap, and
            # escalating onto a human's own hold is precisely the move a hold
            # revokes.
            if authority != "held":
                _bound_failed_harvest(
                    n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir, attempts,
                    tick_log, _FIXER_CONFIG.stage,
                )
            return
        # A failed edit retains the label and the scratch, the same bound the
        # failed comment post below takes.
        if not _apply_pr_body_revision(
            number=n,
            wt_root=wt_root,
            body=revision_body,
            dispatch_id=dispatch_id,
            tick_log=tick_log,
        ):
            _bound_failed_harvest(
                n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir, attempts,
                tick_log, _FIXER_CONFIG.stage,
            )
            return

    # Record the run on the PR before any label moves. The label is the only
    # other GitHub-visible signal a fixer produces, so posting after the relabel
    # would leave a window where the PR shows a new (possibly terminal) label
    # with no stated reason — precisely the audit gap this comment closes. A
    # failed post therefore blocks the relabel and retains custody, matching
    # reviewer harvest semantics.
    if "report_protocol_version" in marker and _fixer_publication_authority(n, current_head) != "clear":
        return
    if fixer_body:
        if not _post_harvest_comment(
            entity="pr",
            number=n,
            wt_root=wt_root,
            dispatch_id=dispatch_id,
            comment_body=fixer_body,
            kind=_FIX_COMMENT_KIND,
            target_label=harvest_label,
        ):
            _bound_failed_harvest(
                n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir, attempts,
                tick_log, _FIXER_CONFIG.stage,
            )
            return

    skip_cleanup = False

    if harvest_action == "recheck":
        if "report_protocol_version" in marker and _fixer_publication_authority(n, current_head) != "clear":
            return
        if not _converge_pipeline_labels(
            "pr", n, harvest_label,
            tick_log=tick_log, phase="harvest", stage=_FIXER_CONFIG.stage,
        ):
            skip_cleanup = True
        else:
            if attempts is not None and not archive_dir:
                attempts.clear(n)
            # Bump iteration cache (advisory accelerator — comment history
            # remains the authority for the round cap).
            iter_dir = os.path.join(wt_root, ".iter")
            iter_file = os.path.join(iter_dir, f"pr-{n}")
            try:
                current_iter = 0
                try:
                    with open(iter_file) as fh:
                        current_iter = int(fh.read().strip())
                except (OSError, ValueError):
                    pass
                os.makedirs(iter_dir, exist_ok=True)
                with open(iter_file, "w") as fh:
                    fh.write(str(current_iter + 1))
            except OSError:
                pass  # non-fatal: iteration cache is advisory

    elif harvest_action == "retry":
        if not _handle_transient_engine_result(
            entity="pr",
            number=n,
            config=_FIXER_CONFIG,
            pre_label=pre_label,
            engine=engine,
            result_raw=result_raw,
            harvest_result=harvest_result,
            dispatch_id=dispatch_id,
            wt_root=wt_root,
            wt_dir=wt_dir,
            tick_log=tick_log,
            attempts=attempts,
            transient_cooldown_seconds=transient_cooldown_seconds,
            transient_cooldown_reset_buffer_seconds=(
                transient_cooldown_reset_buffer_seconds
            ),
        ):
            return

    if skip_cleanup:
        _bound_failed_harvest(
            n, "pr", _FIXER_CONFIG.in_flight_label, wt_dir, attempts,
            tick_log, _FIXER_CONFIG.stage,
        )
        return
    if harvest_action == "recheck" and tick_log:
        tick_log.log(
            pr=n, phase="harvest", stage="fix", action="revision_handoff",
            reason="revision handed off after publication and label convergence",
            engine=engine, dispatch_id=dispatch_id,
            details={"label": harvest_label, "outcome": harvest_result.get("sentinel"),
                     "head_sha": current_head},
        )
    if archive_dir and not _finalize_harvest_archive(
        wt_dir,
        archive_dir,
        number=n,
        stage=_FIXER_CONFIG.stage,
        phase="harvest",
        dispatch_id=dispatch_id,
        tick_log=tick_log,
    ):
        if tick_log:
            tick_log.log(
                pr=n,
                phase="harvest",
                stage=_FIXER_CONFIG.stage,
                action="archive_failed",
                reason="retaining scratch after fixer escalation",
                dispatch_id=dispatch_id,
            )
        return
    # The harvest reached its terminal state, so this run's receipts have done
    # their job. Retiring them here keeps WT_ROOT from accumulating one file per
    # dispatch; a leftover is harmless either way, since the next dispatch's id
    # can never match a stale receipt.
    if archive_dir and attempts is not None:
        attempts.clear(n)
    _discard_comment_receipts(wt_root, "pr", n)
    cleanup_scratch(wt_dir)


def harvest_fixers(
    tick_log: TickLog | None = None,
    target: tuple[str, int] | None = None,
    *,
    wt_root: str | None = None,
    transient_cooldown_seconds: int = DEFAULT_TRANSIENT_COOLDOWN_SECONDS,
    transient_cooldown_reset_buffer_seconds: int = (
        TRANSIENT_COOLDOWN_RESET_BUFFER_SECONDS
    ),
) -> bool:
    """Harvest finished fixer engines.

    Args:
        tick_log: Tick log handle, or ``None``.
        target: Optional ``(type, number)`` to restrict to a single item.
        wt_root: Worktree root directory, or ``None`` to resolve
            :data:`WT_ROOT` at call time.
        transient_cooldown_seconds: Provider cooldown after a transient
            result that names no reset time. The tick passes its validated
            value; the default serves direct callers.
        transient_cooldown_reset_buffer_seconds: Margin added to a reset time
            the transient result names.

    Returns:
        ``True`` if reconciliation succeeded, ``False`` if enumeration
        failed and DISPATCH should be gated.
    """
    if wt_root is None:
        wt_root = WT_ROOT
    attempts = Attempts(wt_root, _FIXER_CONFIG.attempts_key_prefix)
    try:
        decisions, enum_ok = reconcile_engine(
            _FIXER_CONFIG, tick_log, target,
            wt_root=wt_root, attempts=attempts,
        )
    except subprocess.CalledProcessError:
        return False

    transition_ok = _execute_reconcile_actions(
        decisions, _FIXER_CONFIG, wt_root, tick_log, attempts,
    )

    # Process harvest decisions — read result.json, recheck / escalate
    for decision in decisions:
        action = str(decision.get("action", ""))
        if action in ("harvest", "report_missing"):
            _harvest_one_fixer(
                decision, wt_root, tick_log, attempts,
                transient_cooldown_seconds=transient_cooldown_seconds,
                transient_cooldown_reset_buffer_seconds=(
                    transient_cooldown_reset_buffer_seconds
                ),
            )

    return enum_ok and transition_ok


# --- Stale in-flight label reconciliation ---


def _reconcile_stale_in_flight_labels(
    stale_refs: list[dict[str, object]],
    tick_log: "TickLog | None",
) -> None:
    """Reconcile authoritative in-flight labels that have no local marker.

    Every stale ref is retained. "No local marker" is not proof of anything on
    a control plane whose only shared state is GitHub — markers live on local
    disk, so their absence is the steady-state reading of another machine's
    live dispatch and of the manual ``ai:implementing`` pickup that
    ``CLAUDE.md`` prescribes. This sweep therefore never relabels, and an open
    closing PR is not treated as an exception: ``gh search prs "Closes #N"`` is
    a full-text match that hits drafts and PRs under active revision, and
    removing the label would leave the issue carrying no pipeline label at all
   .

    A per-item point-read still runs so the retention decision is logged
    against the item's authoritative current label rather than the search
    index's possibly-stale row.

    Args:
        stale_refs: Dicts with ``kind``, ``number``, ``label`` for each
            in-flight label with no matching local worktree marker.
        tick_log: Tick log handle, or ``None`` to skip logging.
    """
    for ref in stale_refs:
        kind = ref.get("kind")
        number = ref.get("number")
        label = ref.get("label")
        if (
            kind not in ("issue", "pr")
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
            or not isinstance(label, str)
        ):
            continue

        entity = "issue" if kind == "issue" else "pr"

        # Do a per-item point-read to confirm the in-flight label is still
        # actually present. The list query used by
        # read_authoritative_in_flight_labels uses GitHub's search index, which
        # can lag a few seconds behind individual PR edits. If
        # harvest_reviewers/fixers/implementers already transitioned this
        # item's label, the search index may still return the old label while
        # gh pr view returns the authoritative current state. Logging a
        # retention against a stale indexed row would misreport a
        # just-completed harvest as a stranded item.
        label_stage_map = {
            pipeline.LABEL_IMPLEMENTING: "implement",
            pipeline.LABEL_REVIEWING: "review",
            pipeline.LABEL_FIXING: "fix",
        }
        stage = label_stage_map.get(label, "review")
        try:
            item_labels_raw = gh_json(
                entity, "view", str(number), "--json", "labels",
            )
        except (subprocess.CalledProcessError, ValueError):
            item_labels = None
        else:
            item_labels = _pipeline_labels(item_labels_raw)

        if item_labels is None:
            # Authoritative point-read unavailable (transport failure or
            # malformed payload) — the label is retained either way, so this
            # only distinguishes "retained, confirmed stranded" from "retained,
            # unknown". The next tick retries the reconcile.
            if tick_log:
                tick_log.log(
                    pr=number,
                    phase="harvest",
                    stage=stage,
                    action="stale_reconcile_skipped",
                    reason=(
                        f"point-read for {label} failed or was malformed; "
                        "label retained for next tick"
                    ),
                )
            continue

        if label not in item_labels:
            # Label already moved on — harvest handled it. Nothing is stranded.
            if tick_log:
                tick_log.log(
                    pr=number,
                    phase="harvest",
                    stage=stage,
                    action="stale_label_superseded",
                    reason=(
                        f"no stale {label} to retain: "
                        "point-read shows label already transitioned"
                    ),
                )
            continue

        # The label is real and no local marker explains it — but a missing
        # marker is not evidence of anything. Markers are machine-local, so
        # every item another machine dispatched looks exactly like this, as
        # does the documented manual ai:implementing pickup. No proof of
        # custody is available here, so the sweep never mutates; the status
        # dashboard surfaces the item for the operator instead.
        if tick_log:
            tick_log.log(
                pr=number,
                phase="harvest",
                stage=stage,
                action="stale_label_retained",
                reason=(
                    f"retained {label}: no local marker, but a missing "
                    "marker cannot prove this pipeline owns the dispatch"
                ),
            )


# --- DISPATCH phases (Step 10) ---


def _existing_marker_blocks_dispatch(
    wt_dir: str,
    *,
    entity: str | None = None,
    number: int | None = None,
    pre_label: str = "",
    stage: str = "",
    tick_log: "TickLog | None" = None,
) -> bool:
    """Return whether an existing dispatch marker must block re-dispatch.

    Crash window: the tick dies between marker publish and the in-flight
    relabel, leaving a live engine while the item still carries its
    pre-dispatch label. The next tick's dispatch re-selects the item; a
    blanket cleanup on the worktree-add failure path would rmtree the live
    engine's marker — destroying custody of a spending engine and enabling
    a duplicate dispatch.

    Custody rules:

    - No marker → dispatch proceeds.
    - Unparseable marker → fail closed, retain custody (block).
    - Provably dead marker (``stale``/``complete`` with identity not
      ``mismatched``) → clear the dead scratch and proceed. This is the
      normal re-dispatch path for stale-reverted items, whose marker
      survives the revert.
    - Anything else (``live``, ``launching``, ``hung``, or confirmed PID
      reuse) → retain custody and skip; HARVEST owns those transitions.

    Args:
        wt_dir: The item's worktree directory.
        entity: GitHub entity type for ambiguous-marker escalation.
        number: Issue or PR number for ambiguous-marker escalation.
        pre_label: Current pickup label for ambiguous-marker escalation.
        stage: Pipeline stage for logging.
        tick_log: Tick log handle, or ``None``.

    Returns:
        ``True`` when dispatch must skip this item, ``False`` when it is
        safe to proceed (any dead scratch has been cleared).
    """
    def log_archive_failure(archive_number: int, archive_dispatch_id: str) -> None:
        if tick_log:
            tick_log.log(
                pr=archive_number,
                phase="dispatch",
                stage=stage,
                action="archive_failed",
                reason="retaining scratch and label custody",
                dispatch_id=archive_dispatch_id,
            )

    marker_path = os.path.join(wt_dir, "dispatch.json")
    if not os.path.isfile(marker_path):
        if os.path.isdir(wt_dir):
            entry = os.path.basename(wt_dir)
            kind = "issue" if entry.startswith("issue-") else "pr"
            try:
                retained_number = int(entry.split("-", 1)[1])
            except (IndexError, ValueError):
                return True
            archive_dir = _archive_harvest_evidence(
                wt_root=os.path.dirname(wt_dir),
                wt_dir=wt_dir,
                kind=kind,
                number=retained_number,
                dispatch_id="retained-markerless",
                tick_log=tick_log,
                stage=stage,
                phase="dispatch",
            )
            if not archive_dir or not _finalize_harvest_archive(
                wt_dir,
                archive_dir,
                number=retained_number,
                stage=stage,
                phase="dispatch",
                dispatch_id="retained-markerless",
                tick_log=tick_log,
            ):
                log_archive_failure(retained_number, "retained-markerless")
                return True
            cleanup_scratch(wt_dir)
        return False

    def escalate_ambiguous(reason: str) -> bool:
        if entity is None or number is None or not pre_label or not stage:
            return True
        # Publish before the label moves, and hold the label when the post does
        # not land. Dispatch is blocked either way — the marker is unread, so
        # custody is retained regardless of what the remote state ends up
        # saying — which is what makes holding safe here: no engine is launched
        # while the escalation waits for its explanation.
        if not _publish_dispatch_escalation(
            entity=entity,
            number=number,
            stage=stage,
            outcome=pipeline.OUTCOME_AMBIGUOUS_MARKER,
            reason=reason,
            tick_log=tick_log,
        ):
            return True
        try:
            _relabel_with_context(
                entity,
                number,
                pre_label,
                pipeline.LABEL_NEEDS_HUMAN,
                tick_log=tick_log,
                phase="dispatch",
                stage=stage,
            )
        except subprocess.CalledProcessError as exc:
            if tick_log:
                _log_side_effect_failure(
                    tick_log=tick_log,
                    number=number,
                    stage=stage,
                    dispatch_id="",
                    operation=f"ambiguous-marker escalation: {reason}",
                    exc=exc,
                )
            return True
        # The marker stays: this path exists because it could not be proven safe
        # to clear, so the attempt counter stays with it.
        _end_dispatch_escalation(entity, number, stage, retains_marker=True)
        return True

    try:
        with open(marker_path) as fh:
            marker = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return escalate_ambiguous("dispatch marker unreadable")
    if not isinstance(marker, dict):
        return escalate_ambiguous("dispatch marker is not an object")
    expected_number = number
    if expected_number is None:
        try:
            expected_number = int(os.path.basename(wt_dir).split("-", 1)[1])
        except (IndexError, ValueError):
            return escalate_ambiguous("worktree directory identity invalid")
    if not pipeline.valid_marker(
        marker,
        expected_pr=expected_number,
        expected_stage=stage or None,
    ):
        if pipeline.valid_marker(marker, expected_pr=expected_number):
            # Structurally sound, but written by another stage. One item has
            # one scratch directory, so this is what a fix-stage dispatch
            # finds when a reviewer engine already holds the PR. Escalating
            # would relabel live work as corrupt; the marker is custody, and
            # its own stage's harvest owns its lifecycle.
            if tick_log:
                tick_log.log(
                    pr=number, phase="dispatch", stage=stage,
                    action="skip",
                    reason="another stage's dispatch marker holds this item",
                    details={"marker_stage": str(marker.get("stage", ""))},
                )
            return True
        return escalate_ambiguous("dispatch marker structure invalid")
    try:
        classify_state, identity, _pid_alive = _classify_marker(
            wt_dir, marker, result_file=None,
        )
    except Exception:  # noqa: BLE001
        return escalate_ambiguous("dispatch marker classification failed")
    if identity == "mismatched":
        return escalate_ambiguous("dispatch marker PID identity mismatched")
    if classify_state in ("stale", "complete") and identity != "mismatched":
        # Archive evidence before cleanup so redispatch doesn't
        # silently destroy forensic state from a prior engine.
        dispatch_id = marker.get("dispatch_id")
        pr_number = marker.get("pr_number")
        kind = "issue" if os.path.basename(wt_dir).startswith("issue-") else "pr"
        if isinstance(dispatch_id, str) and isinstance(pr_number, int):
            archive_dir = _archive_harvest_evidence(
                wt_root=os.path.dirname(wt_dir),
                wt_dir=wt_dir,
                kind=kind,
                number=pr_number,
                dispatch_id=dispatch_id,
                tick_log=None,
                stage=stage,
                phase="dispatch",
            )
            if not archive_dir or not _finalize_harvest_archive(
                wt_dir,
                archive_dir,
                number=pr_number,
                stage=stage,
                phase="dispatch",
                dispatch_id=dispatch_id,
                tick_log=tick_log,
            ):
                log_archive_failure(pr_number, dispatch_id)
                return True
        else:
            return escalate_ambiguous("dispatch marker identity fields invalid")
        cleanup_scratch(wt_dir)
        return False
    return True


def _dispatch_config_errors(config: pipeline.ResolvedConfig) -> list[str]:
    """Return the configuration errors that should stop one engine dispatch.

    Dispatch reads ``verify_tools`` and ``engine_settings`` again before each
    launch, and refuses on any error that read reports — except one in the
    admission cap, deadline, stall, round, cooldown, cadence, route or probe
    metadata keys. The tick reads
    those once, at its startup gate, and runs on the values validated there; an
    edit made since then is the next tick's to refuse. Escalating this item
    over a key its dispatch does not read would change the tick partway through.

    Args:
        config: The configuration dispatch just resolved.

    Returns:
        The errors that apply to this dispatch.
    """
    return [e for e in config.errors if e not in config.tuning_errors]


def _escalate_dispatch_setup_failure(
    *,
    entity: str,
    number: int,
    pre_label: str,
    stage: str,
    reason: str,
    engine: str,
    tick_log: TickLog | None = None,
    cleanup_dir: str | None = None,
    outcome: str = pipeline.OUTCOME_DISPATCH_SETUP_FAILED,
) -> None:
    """Fail closed on a pre-launch dispatch decision, explaining itself first.

    This owns every escalation DISPATCH makes *before* an engine exists. That
    boundary is the whole point: nothing has been spent, nothing is retained,
    and the item's only account of going red is the comment this publishes. The
    post-launch relabel guards are deliberately not routed here — an engine may
    still be alive and holding the marker there, and its harvest owns the
    explanation.

    Args:
        entity: ``"issue"`` or ``"pr"``.
        number: The issue or PR number.
        pre_label: The item's current pre-dispatch label.
        stage: Pipeline stage for tick-log entries.
        reason: Short human-readable failure reason.
        engine: Engine name for tick-log entries.
        tick_log: Tick log handle, or ``None``.
        cleanup_dir: Optional scratch directory to remove after relabel.
        outcome: The published outcome slug. Defaults to a setup failure; the
            review-policy gate passes its own, because that escalation is a
            deliberate routing decision rather than a breakage and a comment
            calling it a failure would misinform the PR's author.
    """
    if tick_log:
        tick_log.log(
            pr=number,
            phase="dispatch",
            stage=stage,
            action="escalate",
            reason=reason,
            engine=engine,
        )
    if not _publish_dispatch_escalation(
        entity=entity,
        number=number,
        stage=stage,
        outcome=outcome,
        reason=reason,
        engine=engine,
        tick_log=tick_log,
    ):
        # Hold the label — but take the scratch with us. Three of these call
        # sites fire after the `state: "launching"` lease is written and before
        # anything is spawned, so what is left behind is a *phantom* lease: no
        # engine owns it and none ever will. Retaining it would defeat the hold
        # entirely, because HARVEST runs before DISPATCH: the next tick's
        # `reconcile_dispatch` sees `launching` while the item still sits at its
        # pre-dispatch label, converges to the terminal label with no comment,
        # and clears the very attempt budget this hold just spent. The retry
        # this return exists to enable would never run, and the item would land
        # exactly where it lands today — red and unexplained.
        #
        # Removing it is safe precisely because this helper is pre-launch by
        # contract: there is no engine result to retain and no custody to
        # transfer, so the next tick re-enters dispatch and re-attempts the post.
        if cleanup_dir is not None:
            cleanup_scratch(cleanup_dir)
        return
    try:
        _relabel_with_context(
            entity, number, pre_label, pipeline.LABEL_NEEDS_HUMAN,
            tick_log=tick_log, phase="dispatch", stage=stage,
        )
    except subprocess.CalledProcessError:
        return
    _end_dispatch_escalation(entity, number, stage)
    if cleanup_dir is not None:
        cleanup_scratch(cleanup_dir)


def _bound_skills_resolve(
    *,
    wt_dir: str,
    skills: Sequence[str],
    entity: str,
    number: int,
    pre_label: str,
    stage: str,
    engine: str,
    tick_log: TickLog | None = None,
) -> bool:
    """Fail closed when a prompt-bound skill doc is unreadable before launch.

    A skill binding is a path in a prompt, so a doc missing from the install
    degrades silently: the engine reads nothing, works without the discipline
    the binding exists to impose, and still emits its completion sentinel.
    Resolving the doc before spending anything turns that into a visible
    escalation.

    The fault this reports is a packaging defect rather than anything about the
    item. :func:`_guidance_blocked_stages` catches it once per tick before
    DISPATCH and holds the stage without touching labels, so this probe is the
    backstop for an install that changed between that check and this launch.
    Its per-item escalation is unchanged; it now fires only in that window.

    The probe emits its own ``skill_unresolved`` entry *before* escalating so the
    operator grep list distinguishes "the binding is broken" from the generic
    setup-failure escalate that follows it.

    Args:
        wt_dir: The engine's worktree, removed when the probe escalates.
        skills: The skill names this stage's prompt binds.
        entity: ``"issue"`` or ``"pr"``.
        number: The issue or PR number.
        pre_label: The item's current pre-dispatch label.
        stage: Pipeline stage for tick-log entries.
        engine: Engine name for tick-log entries.
        tick_log: Tick log handle, or ``None``.

    Returns:
        ``True`` when every doc resolved and dispatch may continue. ``False``
        after the item has been escalated and ``wt_dir`` cleaned up.
    """
    unresolved = unresolved_skill_docs(skills)
    if not unresolved:
        return True

    probe_paths = [skill_doc_path(name) for name in unresolved]
    reason = (
        "prompt-bound skill docs unreadable in the pipeline install: "
        f"{', '.join(probe_paths)}"
    )
    if tick_log:
        tick_log.log(
            pr=number,
            phase="dispatch",
            stage=stage,
            action="skill_unresolved",
            reason=reason,
            engine=engine,
            details={
                "skills": unresolved,
                "probe_paths": probe_paths,
                "worktree": wt_dir,
            },
        )
    _escalate_dispatch_setup_failure(
        entity=entity,
        number=number,
        pre_label=pre_label,
        stage=stage,
        reason=reason,
        engine=engine,
        tick_log=tick_log,
        cleanup_dir=wt_dir,
    )
    return False


def _publish_marker_or_revert(
    *,
    proc: subprocess.Popen[bytes],
    published_marker: dict[str, object],
    wt_dir: str,
    entity: str,
    pr_number: int,
    in_flight_label: str,
    pre_label: str,
    tick_log: TickLog | None = None,
    engine: str = "claude",
    stage: str = "implement",
) -> bool:
    """Publish the post-spawn marker; revert on failure.

    Writes the full dispatch marker with process identity via
    ``pipeline.atomic_write``. If the write fails (disk full,
    permissions), attempts to terminate the engine and revert the label.
    If termination also fails, writes a fallback marker so the next tick
    can reconcile the orphaned engine.

    Args:
        proc: The spawned engine subprocess.
        published_marker: Full marker dict with ``pid``, ``start_time``,
            ``pgid`` — but no ``state`` (leased consumed).
        wt_dir: Path to the worktree directory.
        entity: ``"pr"`` or ``"issue"`` — the GitHub entity type.
        pr_number: The PR or issue number.
        in_flight_label: The label set on dispatch (e.g. ``ai:implementing``).
        pre_label: The label to revert to on failure (e.g. ``ai:available``).
        tick_log: Tick log handle, or ``None``.
        engine: Engine name for logging (``"claude"``, ``"codex"``).
        stage: Pipeline stage for logging (``"implement"``, ``"review"``,
            ``"fix"``).

    Returns:
        ``True`` if the marker was published successfully, ``False`` if
        the fallback was triggered (engine terminated or fallback marker
        written).
    """
    try:
        pipeline.atomic_write(
            os.path.join(wt_dir, "dispatch.json"),
            json.dumps(published_marker),
        )
        return True
    except (OSError, ValueError):
        # Marker publish failed — the engine is running but untracked.
        # Try to terminate the engine and revert.
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage=stage,
                action="marker_publish_failed",
                reason="attempting engine termination + revert",
                engine=engine,
            )

        terminated = _terminate_engine(
            proc,
            pr_number,
            stage,
            engine,
            tick_log,
        )

        if terminated:
            # Engine dead — safe to revert label and clean up.
            try:
                _relabel_with_context(
                    entity, pr_number, in_flight_label, pre_label,
                    tick_log=tick_log, phase="dispatch", stage=stage,
                )
            except subprocess.CalledProcessError:
                pass
            marker_dispatch_id = published_marker.get("dispatch_id")
            dispatch_id = (
                marker_dispatch_id
                if isinstance(marker_dispatch_id, str) and marker_dispatch_id
                else "unknown"
            )
            archived = _archive_and_cleanup_scratch(
                wt_root=os.path.dirname(wt_dir),
                wt_dir=wt_dir,
                kind="issue" if entity == "issue" else "pr",
                number=pr_number,
                dispatch_id=dispatch_id,
                tick_log=tick_log,
                stage=stage,
                phase="dispatch",
                failure_reason="retaining scratch after marker publish failure",
            )
            if tick_log and archived:
                tick_log.log(
                    pr=pr_number, phase="dispatch", stage=stage,
                    action="reverted",
                    reason="marker publish failed, engine terminated",
                    engine=engine,
                )
        else:
            # Engine survived termination — write a pure lease fallback
            # marker for the next tick's HARVEST to reconcile. A pure
            # lease carries only state + provenance, never pid/start_time/
            # pgid, so it passes valid_marker's mutual-exclusion check.
            fallback: dict[str, object] = {
                k: v for k, v in published_marker.items()
                if k not in ("pid", "start_time", "pgid")
            }
            fallback["state"] = "launching"
            fallback["deadline"] = time.time() + 600  # 10 min grace
            try:
                pipeline.atomic_write(
                    os.path.join(wt_dir, "dispatch.json"),
                    json.dumps(fallback),
                )
            except (OSError, ValueError):
                pass
            if tick_log:
                tick_log.log(
                    pr=pr_number, phase="dispatch", stage=stage,
                    action="fallback_marker_written",
                    reason="marker publish + termination both failed",
                    engine=engine,
                )
        return False


def _terminate_engine(
    proc: subprocess.Popen[bytes],
    pr_number: int,
    stage: str,
    engine: str,
    tick_log: TickLog | None = None,
) -> bool:
    """Terminate a spawned engine after a post-spawn failure.

    Called when relabel fails after the engine is already running.
    Terminates and verifies the entire process group created by
    ``start_new_session=True``. Never raises — the engine group may already
    have exited.

    Args:
        proc: The spawned engine subprocess.
        pr_number: The PR or issue number.
        stage: Pipeline stage (``"review"``, ``"implement"``, ``"fix"``).
        engine: Engine name (``"claude"``, ``"codex"``).
        tick_log: Tick log handle, or ``None``.

    Returns:
        ``True`` when the entire engine group was confirmed dead, ``False``
        when SIGKILL did not stop every member within the grace period. A
        ``False`` return means the caller must NOT delete the dispatch
        marker — the orphan must be preserved so the next tick's
        HARVEST can re-attempt the kill on proven identity.
    """
    try:
        stopped = process_control.terminate_process_group(
            proc.pid,
            grace_seconds=5,
            kill_grace_seconds=2,
            poll_interval=0.5,
        )
    except ValueError:
        stopped = False
    if not stopped and tick_log:
        tick_log.log(
            pr=pr_number, phase="dispatch", stage=stage,
            action="kill_failed",
            reason="engine group survived SIGKILL after relabel failure",
            engine=engine,
        )
    return stopped


def dispatch_reviewers(
    tick_log: TickLog | None = None,
    target: tuple[str, int] | None = None,
    authoritative_in_flight: list[pipeline.InFlightRef] | None = None,
    review_model: str = "",
    review_effort: str = "",
    trusted_author: str = "",
    max_in_flight: int = pipeline.DEFAULT_MAX_IN_FLIGHT,
    timeout_seconds: int = REVIEW_TIMEOUT,
    *,
    only: Sequence[tuple[str, int]] | None = None,
) -> None:
    """Dispatch reviewer engines for PRs at ai:implemented / ai:revised.

    Queries open PRs with the ``ai:implemented`` or ``ai:revised`` labels,
    applies the trusted-candidate gate (same author, same repo), checks the
    admission cap, and launches a codex reviewer engine for each eligible PR.

    Args:
        tick_log: Tick log handle, or ``None`` to skip logging.
        target: Optional ``("pr", N)`` tuple to restrict to a single PR.
            Non-pr targets (e.g. ``("issue", 42)``) skip silently.
        authoritative_in_flight: Pre-computed authoritative in-flight refs
            from :func:`read_authoritative_in_flight_labels`. When
            ``None``, the function resolves them itself. **Appended to in
            place** for every engine launched here: the caller passes one
            list to all three dispatchers, so this is the tick's single
            record of what it has already launched, and the only evidence
            that survives a lagging GitHub read within one DISPATCH phase.
        review_model: Model override for the reviewer engine. Empty means
            "resolve from the operator's codex config".
        review_effort: Reasoning-effort override for the reviewer engine.
            Empty means "resolve from the operator's codex config".
        trusted_author: The operator login this tick resolved and validated.
            The tick always passes it, so admission uses the identity startup
            checked. Empty falls back to :data:`TRUSTED_REVIEW_AUTHOR`, bound
            at successful startup, for direct callers.
        max_in_flight: The admission cap. The tick always passes the value
            its startup gate validated; the default serves direct callers.
        timeout_seconds: Reviewer deadline from dispatch. The tick passes the
            ``review_timeout_seconds`` its startup gate validated; the default
            serves direct callers.
        only: Resolved ``--only`` scope items, or ``None``. When set, each
            listed PR is read directly in list order and the label queue is
            not listed, so a listed PR beyond the bounded queue page is still
            found and an unlisted PR is never read or written. Trust and
            eligibility gates still apply. Unlike ``target`` this scopes new
            launches alone; HARVEST stays global.
    """
    trusted_author = trusted_author or TRUSTED_REVIEW_AUTHOR
    # Target type gate: only dispatch for pr targets.
    if target is not None and target[0] != "pr":
        return

    # Verify the pinned Codex version before launching any reviewer.
    # An unpinned binary could produce unvalidated reviews — fail the
    # entire reviewer phase rather than launch with unknown code.
    try:
        verify_codex_bin()
    except RuntimeError:
        if tick_log:
            tick_log.log(
                pr=None, phase="dispatch", stage="review",
                action="skip",
                reason="codex version check failed",
                engine="codex",
            )
        return

    # Resolve authoritative in-flight labels if not provided. A caller-supplied
    # list is used as-is and appended to in place: it is the tick's shared
    # record of what it has already launched, and copying it here would hide
    # this stage's launches from the stages that run after it.
    if authoritative_in_flight is None:
        try:
            authoritative_in_flight = read_authoritative_in_flight_labels()
        except (subprocess.CalledProcessError, ValueError):
            return

    # An --only list reads its PRs directly, in list order, so a listed PR
    # beyond the bounded label page is still found and an unlisted PR is
    # never read. Otherwise collect both pre-dispatch label queues.
    if only is not None:
        candidates = _read_listed_prs(only, stage="review", tick_log=tick_log)
    else:
        candidate_nums: set[int] = set()
        for pre_label in (pipeline.LABEL_IMPLEMENTED, pipeline.LABEL_REVISED):
            try:
                lines = gh_text(
                    "pr", "list",
                    "--label", pre_label,
                    "--state", "open",
                    # Match the actionable-queue scan limit: held candidates
                    # now stay on their pre-dispatch label, so a default-size
                    # page they fill would starve eligible work beyond it.
                    "--limit", str(_ACTIONABLE_QUEUE_SCAN_LIMIT),
                    "--json", "number",
                    "-q", ".[].number",
                )
            except subprocess.CalledProcessError:
                continue
            for line in lines.splitlines():
                line = line.strip()
                if line.isdigit():
                    candidate_nums.add(int(line))
        # Sort for deterministic dispatch order.
        candidates = sorted(candidate_nums)

    if not candidates:
        return

    for pr_number in candidates:
        # Target filter
        if target is not None and pr_number != target[1]:
            continue

        # Eligibility gate: prove from the item's own label set that no engine
        # holds it and no human hold outranks it, before anything mutates.
        pre_label = _resolve_dispatchable_pickup_label(
            "pr", pr_number,
            stage="review",
            authoritative_in_flight=authoritative_in_flight,
            tick_log=tick_log,
        )
        if pre_label is None:
            continue

        wt_dir = os.path.join(WT_ROOT, f"pr-{pr_number}")
        if _existing_marker_blocks_dispatch(
            wt_dir,
            entity="pr",
            number=pr_number,
            pre_label=pre_label,
            stage="review",
            tick_log=tick_log,
        ):
            if tick_log:
                tick_log.log(
                    pr=pr_number,
                    phase="dispatch",
                    stage="review",
                    action="skip",
                    reason="existing dispatch marker retains custody",
                    engine="codex",
                )
            continue

        # Admission check
        admission = can_dispatch_now(max_in_flight=max_in_flight)
        if not admission.can_dispatch:
            if tick_log:
                tick_log.log(
                    pr=pr_number, phase="dispatch", stage="review",
                    action="skip",
                    reason=f"admission: {admission.reason}",
                    engine="codex",
                    details={
                        "in_flight": admission.in_flight_count,
                        "max": admission.max_in_flight,
                    },
                )
            continue

        # Trusted-candidate gate
        try:
            candidate = gh_json(
                "pr", "view", str(pr_number),
                "--json", "author,headRepository,isCrossRepository",
            )
        except (subprocess.CalledProcessError, ValueError):
            continue

        if not pipeline.trusted_review_candidate(
            candidate,
            trusted_author=trusted_author,
            trusted_repo=TRUSTED_HEAD_REPO,
        ):
            if tick_log:
                tick_log.log(
                    pr=pr_number, phase="dispatch", stage="review",
                    action="skip",
                    reason="untrusted candidate",
                    engine="codex",
                )
            continue

        # Launch
        _dispatch_one_reviewer(
            pr_number=pr_number,
            pre_label=pre_label,
            tick_log=tick_log,
            review_model=review_model,
            review_effort=review_effort,
            trusted_author=trusted_author,
            timeout_seconds=timeout_seconds,
        )

        # Track this new engine in the in-flight set.
        authoritative_in_flight.append(
            pipeline.InFlightRef(
                kind="pr", number=pr_number, label=pipeline.LABEL_REVIEWING,
            )
        )


def _read_pr_discussion(
    pr_number: int, *, stage: str, engine: str, tick_log: TickLog | None,
) -> dict | None:
    """Acquire launch evidence separately from the selected control records."""
    try:
        return discussion_context.acquire(
            gh_json, TRUSTED_HEAD_REPO, pr_number, _summary_closure,
        )
    except discussion_context.ContextError as exc:
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage=stage, action="skip",
                reason=f"required PR discussion unavailable: {exc} — retained for retry",
                engine=engine,
            )
        return None


def _write_pr_discussion(
    wt_dir: str, discussion: dict, values: dict[str, str], *,
    pr_number: int, stage: str, engine: str, tick_log: TickLog | None,
) -> dict[str, str] | None:
    """Supply complete evidence inside disposable scratch, outside git's index.

    A fresh directory avoids paths/symlinks supplied by the PR. Its internal
    ignore file excludes itself and every payload from ordinary git add -A.
    Existing scratch cleanup/archive owns the directory's lifetime. Large
    selected reports also move to files; no discussion text changes argv size.
    """
    try:
        directory = Path(tempfile.mkdtemp(prefix=".pr-pipeline-context-", dir=wt_dir))
        (directory / ".gitignore").write_text("*\n", encoding="utf-8")
        path = directory / "discussion.json"
        path.write_text(
            json.dumps(discussion_context.readable(discussion), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        supplied = {"discussion_file": str(path.resolve())}
        for name, value in values.items():
            if len(value.encode("utf-8")) > FIXER_CONTEXT_BUDGET:
                source = directory / f"{name}.json"
                parts = {"textParts": [
                    value[start:start + 256] for start in range(0, len(value), 256)
                ]}
                source.write_text(json.dumps(parts, ensure_ascii=False, indent=2), encoding="utf-8")
                supplied[name] = (
                    f"Read the full {name} from `{source.resolve()}`. "
                    "Join its ordered textParts without a separator. "
                    "This file is required task evidence."
                )
            else:
                supplied[name] = value
        return supplied
    except (OSError, ValueError) as exc:
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage=stage, action="skip",
                reason=f"cannot supply complete PR discussion: {exc} — retained for retry",
                engine=engine,
            )
        cleanup_scratch(wt_dir)
        return None


def _dispatch_one_reviewer(
    *,
    pr_number: int,
    pre_label: str,
    tick_log: TickLog | None = None,
    review_model: str = "",
    review_effort: str = "",
    trusted_author: str = "",
    timeout_seconds: int = REVIEW_TIMEOUT,
) -> None:
    """Launch a single codex reviewer engine for one PR.

    Resolves base/head SHAs, fetches the PR head into local objects, runs
    the policy-touch gate (escalating to human when the PR modifies
    reviewer-governing files), creates an isolated git worktree at the
    PR head, writes a lease marker, and launches the reviewer via
    ``engine_wrapper.py`` as a subprocess. On launch failure, the worktree
    is cleaned up and the PR label is left unchanged.

    Args:
        pr_number: The PR number to review.
        pre_label: The PR's label before dispatch (``ai:implemented`` or
            ``ai:revised``).
        tick_log: Tick log handle, or ``None``.
        review_model: Resolved reviewer model. Empty means "resolve from the
            operator config" (see :func:`resolve_reviewer_model_settings`).
        review_effort: Resolved reviewer reasoning effort. Empty means
            "resolve from the operator config".
        trusted_author: The operator login this tick resolved. Empty falls
            back to the active tick's :data:`TRUSTED_REVIEW_AUTHOR`, for the
            direct callers that are not running a tick.
        timeout_seconds: Seconds from dispatch to the deadline recorded in
            the marker, which reconciliation enforces.
    """
    trusted_author = trusted_author or TRUSTED_REVIEW_AUTHOR
    wt_dir = os.path.abspath(os.path.join(WT_ROOT, f"pr-{pr_number}"))
    dispatch_id = f"review-{pr_number}-{int(time.time())}"

    # Custody guard: never dispatch over an existing marker whose engine
    # may still be alive (crash window between marker publish and relabel).
    if _existing_marker_blocks_dispatch(
        wt_dir,
        entity="pr",
        number=pr_number,
        pre_label=pre_label,
        stage="review",
        tick_log=tick_log,
    ):
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage="review",
                action="skip",
                reason="existing dispatch marker retains custody",
                engine="codex",
            )
        return

    # Resolve repo root for git operations.
    try:
        checkout_root = git("rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError:
        return

    # Fetch and resolve base/head SHAs for the drift guards. The base ref must
    # come from authoritative PR metadata: a stacked PR's base is another branch,
    # so guessing "main" would compute a plausible merge-base against the wrong
    # target and record the wrong freshness ref — a review could then be promoted
    # against a base the PR does not have. Without the metadata there is no valid
    # diff or freshness contract; retain the PR and let the next tick retry.
    try:
        base_ref = gh_text(
            "pr", "view", str(pr_number),
            "--json", "baseRefName", "-q", ".baseRefName",
        )
    except subprocess.CalledProcessError:
        base_ref = ""
    if not base_ref:
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage="review",
                action="skip",
                reason="failed to read PR base ref — retained for retry",
                engine="codex",
            )
        return

    try:
        git("-C", checkout_root, "fetch", "--quiet", TARGET_REMOTE, base_ref)
        base_tip_sha = git("-C", checkout_root, "rev-parse", f"{TARGET_REMOTE}/{base_ref}")
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pre_label,
            stage="review",
            reason=f"failed to fetch base ref {base_ref!r}",
            engine="codex",
            tick_log=tick_log,
        )
        return

    try:
        head_sha = gh_text(
            "pr", "view", str(pr_number),
            "--json", "headRefOid", "-q", ".headRefOid",
        )
    except subprocess.CalledProcessError:
        return

    if not head_sha:
        return

    discussion = _read_pr_discussion(
        pr_number, stage="review", engine="codex", tick_log=tick_log,
    )
    if discussion is None:
        return
    pr_title, pr_body = discussion["pr"]["title"], discussion["pr"]["body"]

    # Pin the published reasoning and its round token from one snapshot. A
    # failed read must not launch a reviewer that never saw why nothing moved.
    fix_round, fixer_summary, original_review = "", "", ""
    original_finding_references = "No originating finding snapshot."
    fix_comments = _read_comment_history(
        pr_number, phase="dispatch", stage="review", tick_log=tick_log,
        trusted_author=trusted_author,
    )
    if fix_comments is None:
        return
    if pre_label == pipeline.LABEL_REVISED:
        fix_round, fixer_summary = pipeline.latest_fixer_context(
            fix_comments, trusted_author=trusted_author,
        )
        original_review = pipeline.fixer_original_review(
            fix_comments, fixer_summary, trusted_author=trusted_author,
        )
        if original_review:
            try:
                original_finding_references = json.dumps(pipeline.review_reports.handoff(original_review))
            except ValueError:
                if tick_log:
                    tick_log.log(pr=pr_number, phase="dispatch", stage="review", action="skip",
                                 reason="original review finding identity is ambiguous — retained for retry")
                return
        if (fix_round or pipeline.fixer_reports.is_current_comment(fixer_summary)) and not original_review:
            if tick_log:
                tick_log.log(
                    pr=pr_number, phase="dispatch", stage="review", action="skip",
                    reason="original revision review missing or edited — retained for retry",
                    engine="codex",
                )
            return

    # Fetch the PR head so head_sha is available in local objects. This is
    # required both for the policy gate diff and for creating the worktree.
    try:
        git("-C", checkout_root, "fetch", "--quiet", TARGET_REMOTE,
            f"pull/{pr_number}/head")
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pre_label,
            stage="review",
            reason="failed to fetch PR head",
            engine="codex",
            tick_log=tick_log,
        )
        return

    # Compute the merge-base so the reviewer diffs only the PR's actual changes.
    # Using <remote>/{base_ref} tip would include commits that landed on main after
    # this PR's branch was cut, producing findings about code this PR never touched.
    # base_tip_sha (current main tip) is kept separately for drift detection.
    try:
        base_sha = git("-C", checkout_root, "merge-base",
                       f"{TARGET_REMOTE}/{base_ref}", head_sha)
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pre_label,
            stage="review",
            reason="failed to compute merge-base for review diff",
            engine="codex",
            tick_log=tick_log,
        )
        return

    # Policy gate: prevent auto-review of PRs that modify reviewer-governing
    # instructions (CLAUDE.md, AGENTS.md, review checklist). A PR that edits
    # these files supplies instructions the reviewer itself would follow —
    # "untrusted input controls the validator." Route such PRs to a human.
    # Head SHA is now in local objects (fetched above), so the diff resolves.
    try:
        raw_diff = git(
            "-C", checkout_root, "diff", "--name-only",
            f"{base_sha}..{head_sha}",
        )
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pre_label,
            stage="review",
            reason="git diff failed (head SHA unavailable)",
            engine="codex",
            tick_log=tick_log,
            cleanup_dir=wt_dir,
        )
        return
    changed_paths = [p for p in raw_diff.splitlines() if p.strip()]
    if pipeline.touches_review_policy(changed_paths, TRUSTED_HEAD_REPO):
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pre_label,
            stage="review",
            reason="PR touches reviewer policy files — routing to human",
            engine="codex",
            tick_log=tick_log,
            cleanup_dir=wt_dir,
            outcome=pipeline.OUTCOME_REVIEW_POLICY_TOUCHED,
        )
        return

    # Create an isolated git worktree at the PR head SHA. This ensures
    # ``git rev-parse --show-toplevel`` from wt_dir returns the worktree
    # root, not the parent checkout, and the codex reviewer reviews
    # the actual PR head rather than whatever is checked out in the
    # operator's workspace.
    try:
        git("-C", checkout_root, "worktree", "prune")
        git("-C", checkout_root, "worktree", "add",
            "--detach", wt_dir, head_sha)
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pre_label,
            stage="review",
            reason="failed to create reviewer worktree",
            engine="codex",
            tick_log=tick_log,
            cleanup_dir=wt_dir,
        )
        return

    context_values = _write_pr_discussion(
        wt_dir, discussion,
        {"pr_title": pr_title, "pr_body": pr_body,
         "fixer_summary": fixer_summary or "No published fixer summary.",
         "original_review": original_review or "No originating review available for this round."},
        pr_number=pr_number, stage="review", engine="codex", tick_log=tick_log,
    )
    if context_values is None:
        return

    deadline = time.time() + timeout_seconds

    lease_marker: dict[str, object] = {
        "engine": "codex",
        "stage": "review",
        "pr_number": pr_number,
        "deadline": deadline,
        "base_ref": base_ref,
        "base_sha": base_sha,        # merge-base: what Codex diffs against
        "base_tip_sha": base_tip_sha,  # main tip at dispatch: used for drift detection
        "report_protocol_version": pipeline.review_reports.VERSION,
        **({"fix_round": fix_round} if fix_round else {}),
        "head_sha": head_sha,
        "pre_label": pre_label,
        "dispatch_id": dispatch_id,
        "state": "launching",
    }
    try:
        pipeline.atomic_write(
            os.path.join(wt_dir, "dispatch.json"),
            json.dumps(lease_marker),
        )
    except (OSError, ValueError):
        # Lease write failed — cannot proceed safely. Leave no state.
        cleanup_scratch(wt_dir)
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage="review",
                action="lease_write_failed",
                reason="atomic_write of lease marker raised OSError",
                engine="codex",
            )
        return

    # Build a scrubbed environment for the reviewer engine (env -i isolation).
    # The design's reviewer-isolation allowlist (§ Reviewer isolation) is:
    #   PATH, HOME (scrub dir), CODEX_HOME, GIT_TERMINAL_PROMPT=0, TMPDIR.
    # Provider API keys (DEEPSEEK_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY)
    # are explicitly excluded: the reviewer authenticates via the CODEX_HOME
    # auth.json symlink, not provider keys. Forwarding them to this prompt-
    # injection surface enables exfiltration through posted review comments.
    scrub_dir = os.path.abspath(os.path.join(WT_ROOT, f"pr-{pr_number}-scrub"))
    codex_home = os.path.join(scrub_dir, ".codex")
    os.makedirs(codex_home, exist_ok=True)
    auth_src = os.path.join(_operator_codex_home(), "auth.json")
    auth_dst = os.path.join(codex_home, "auth.json")
    if os.path.isfile(auth_src) and not os.path.exists(auth_dst):
        try:
            os.symlink(auth_src, auth_dst)
        except OSError:
            pass  # non-fatal: codex may resolve auth via other means

    reviewer_env: dict[str, str] = {
        "HOME": scrub_dir,
        "CODEX_HOME": codex_home,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "GIT_TERMINAL_PROMPT": "0",
        "TMPDIR": os.path.join(scrub_dir, "tmp"),
    }
    os.makedirs(reviewer_env["TMPDIR"], exist_ok=True)

    try:
        review_skills = native_skills.prepare_reviewer(
            skills=native_skills.REVIEW_SKILLS,
            install_root=pipeline.INSTALL_SKILLS_ROOT, scrub_dir=Path(scrub_dir),
        )
    except (OSError, ValueError):
        _escalate_dispatch_setup_failure(
            entity="pr", number=pr_number, pre_label=pre_label, stage="review",
            reason="installed native reviewer skill catalog unavailable",
            engine="codex", tick_log=tick_log, cleanup_dir=wt_dir,
        )
        return

    # Build the reviewer engine command via engine_wrapper.
    result_path = os.path.join(wt_dir, "review.json")
    envelope_path = os.path.join(wt_dir, "completion.json")
    observation_path = os.path.join(wt_dir, "review-observation.json")
    engine_wrapper_py = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "engine_wrapper.py",
    )

    review_prompt = load_prompt_template("reviewer", {
        "pr_number": pr_number,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "trusted_head_repo": TRUSTED_HEAD_REPO,
        "trusted_review_author": trusted_author,
        **context_values,
        "original_finding_references": original_finding_references,
        "report_allowance": pipeline.review_reports.AUTHOR_ALLOWANCE,
    })

    review_prompt = native_skills.reviewer_prompt(review_skills) + "\n\n" + review_prompt

    # Resolve model/effort on the trusted side and record what this review
    # actually ran with: the operator config can change between ticks, so
    # without this the provenance of a given review is unrecoverable.
    resolved_model, resolved_effort = resolve_reviewer_model_settings(
        model_override=review_model, effort_override=review_effort,
    )
    if tick_log:
        tick_log.log(
            pr=pr_number, phase="dispatch", stage="review",
            action="model_resolved",
            reason=f"model={resolved_model} effort={resolved_effort}",
            engine="codex",
            dispatch_id=dispatch_id,
            details={"model": resolved_model, "effort": resolved_effort},
        )

    engine_cmd = [
        sys.executable, engine_wrapper_py, "run",
        "--dispatch-id", dispatch_id,
        "--marker-path", os.path.join(wt_dir, "dispatch.json"),
        "--result-path", result_path,
        "--envelope-path", envelope_path,
        "--observation-path", observation_path,
        "--engine", "codex",
        "--stage", "review",
        "--pr-number", str(pr_number),
        "--deadline", str(deadline),
        "--base-ref", base_ref,
        "--base-sha", base_sha,
        "--base-tip-sha", base_tip_sha,
        "--report-protocol-version", str(pipeline.review_reports.VERSION),
        *(["--fix-round", fix_round] if fix_round else []),
        "--head-sha", head_sha,
        "--pre-label", pre_label,
        "--",
        "codex", "exec",
        "--json",
        "--output-schema",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "review-schema.json"),
        # Read-only sandbox instead of --dangerously-bypass-approvals-and-
        # sandbox: `codex exec` is non-interactive by design, and a review
        # only reads the worktree and emits JSON (the tick posts the comment
        # at harvest), so the full bypass is unnecessary. Same mode the
        # preflight probes.
        "-s", "read-only",
        "--ephemeral",
        "--ignore-user-config",
        # Block project-level execpolicy .rules from the PR head worktree —
        # same vulnerability class as the CLAUDE.md/AGENTS.md policy gate.
        "--ignore-rules",
        # --ignore-user-config discards ~/.codex/config.toml, so model and
        # effort must be passed explicitly or codex silently falls back to
        # its built-in defaults. The tick reads the operator's config on the
        # trusted side and forwards exactly these two values —
        # resolve_reviewer_model_settings documents why nothing else from
        # that file may cross into the reviewer.
        "--model", resolved_model,
        "-c", f'model_reasoning_effort="{resolved_effort}"',
        review_prompt,
    ]

    stderr_fh = None
    try:
        stderr_fh = open(os.path.join(wt_dir, "stderr.log"), "wb")
        proc = subprocess.Popen(
            engine_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_fh,
            cwd=wt_dir,
            start_new_session=True,
            env=reviewer_env,
        )
        _track_engine_child(proc)
    except OSError:
        cleanup_scratch(wt_dir)
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage="review",
                action="launch_failed",
                reason="subprocess.Popen raised OSError",
                engine="codex",
            )
        return
    finally:
        if stderr_fh is not None:
            try:
                stderr_fh.close()
            except OSError:
                pass

    _log_skill_context(
        tick_log=tick_log, number=pr_number, stage="review",
        dispatch_id=dispatch_id, engine="codex",
    )

    # Publish the full marker with process identity (with fallback).
    # With start_new_session=True, the child's pid equals its pgid,
    # matching the implementer/fixer pattern.
    published_marker = dict(lease_marker)
    published_marker.pop("state", None)
    published_marker["pid"] = proc.pid
    published_marker["start_time"] = time.time()
    published_marker["pgid"] = proc.pid

    marker_ok = _publish_marker_or_revert(
        proc=proc,
        published_marker=published_marker,
        wt_dir=wt_dir,
        entity="pr",
        pr_number=pr_number,
        in_flight_label=pipeline.LABEL_REVIEWING,
        pre_label=pre_label,
        tick_log=tick_log,
        engine="codex",
        stage="review",
    )
    if not marker_ok:
        return

    # Relabel the PR.
    try:
        relabel("pr", pr_number, pre_label, pipeline.LABEL_REVIEWING)
    except subprocess.CalledProcessError:
        # Engine is running but label is stale — the next tick would
        # re-dispatch a duplicate. Terminate; only clean up when the
        # engine is confirmed dead (Invariant 3: label flip failure →
        # lease deleted, no spawn retained). When kill fails, retain
        # the marker for the next tick's HARVEST to re-attempt on
        # proven identity.
        killed = _terminate_engine(proc, pr_number, "review", "codex", tick_log)
        if killed:
            _archive_and_cleanup_scratch(
                wt_root=WT_ROOT,
                wt_dir=wt_dir,
                kind="pr",
                number=pr_number,
                dispatch_id=dispatch_id,
                tick_log=tick_log,
                stage="review",
                phase="dispatch",
                failure_reason="retaining reviewer scratch after relabel failure",
            )
        else:
            # Kill failed: marker is retained but the pre-dispatch label
            # is still visible.  Flip to ai:needs-human-decision so the
            # next tick does not re-dispatch a duplicate reviewer.
            try:
                _relabel_with_context(
                    "pr", pr_number, pre_label,
                    pipeline.LABEL_NEEDS_HUMAN,
                    tick_log=tick_log, phase="dispatch", stage="review",
                )
            except subprocess.CalledProcessError:
                pass
        return

    if tick_log:
        tick_log.log(
            pr=pr_number, phase="dispatch", stage="review",
            action="launch",
            reason=f"pid={proc.pid}",
            engine="codex",
            dispatch_id=dispatch_id,
            details={"head_sha": head_sha[:8], "base_ref": base_ref},
        )


def _log_filtered_issue_count(available: list[object], tick_log: TickLog) -> None:
    """Surface excluded issues using one bounded number-only read.

    A complete operator page lets us subtract identities even when the total
    page truncates. If both pages truncate, zero is the only provable lower
    bound: unseen operator issues must not be counted as other authors.
    Failed, malformed, or detectably inconsistent reads produce no count.
    """
    try:
        total = gh_json(
            "issue", "list", "--label", pipeline.LABEL_AVAILABLE,
            "--state", "open", "--limit", str(_ACTIONABLE_QUEUE_SCAN_LIMIT),
            "--json", "number",
        )
    except (subprocess.CalledProcessError, ValueError):
        return

    def numbers(items: object) -> set[int] | None:
        if not isinstance(items, list):
            return None
        result: set[int] = set()
        for item in items:
            n = item.get("number") if isinstance(item, dict) else None
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0 or n in result:
                return None
            result.add(n)
        return result

    owned_numbers = numbers(available)
    total_numbers = numbers(total)
    if owned_numbers is None or total_numbers is None:
        return
    if any(not pipeline.trusted_issue_candidate(
        item, trusted_author=TRUSTED_REVIEW_AUTHOR,
    ) for item in available):
        return
    capped = len(total_numbers) >= _ACTIONABLE_QUEUE_SCAN_LIMIT
    if not capped and not owned_numbers <= total_numbers:
        return
    if len(owned_numbers) >= _ACTIONABLE_QUEUE_SCAN_LIMIT:
        if not capped:
            return
        count = 0
    else:
        count = len(total_numbers - owned_numbers)
    if count or capped:
        qualifier = " or more" if capped else ""
        tick_log.log(
            pr=None, phase="dispatch", stage="implement",
            action="issue_authors_filtered",
            reason=f"{count}{qualifier} available issues not authored by the operator.",
        )


def dispatch_implementers(
    tick_log: TickLog | None = None,
    target: tuple[str, int] | None = None,
    authoritative_in_flight: list[pipeline.InFlightRef] | None = None,
    *,
    impl_engine: str = "claude",
    model: str = "",
    permission_mode: str = "auto",
    max_in_flight: int = pipeline.DEFAULT_MAX_IN_FLIGHT,
    timeout_seconds: int = IMPLEMENTER_TIMEOUT,
    stall_seconds: int = ENGINE_STALL_SECONDS,
    stall_startup_grace_seconds: int = ENGINE_STALL_STARTUP_GRACE_SECONDS,
    only: Sequence[tuple[str, int]] | None = None,
) -> None:
    """Dispatch implementer engines for issues at ai:available.

    Queries operator-authored open issues with the ``ai:available`` label, checks the
    admission cap, and launches an implementer engine for each eligible
    issue (respecting the optional target filter). Each engine runs
    ``claude -p`` inside a dedicated worktree. The issue is relabeled to
    ``ai:implementing`` on successful launch.

    Args:
        tick_log: Tick log handle, or ``None`` to skip logging.
        target: Optional ``("issue", N)`` tuple to restrict to a single
            issue. Non-issue targets (e.g. ``("pr", 42)``) skip silently.
        authoritative_in_flight: Pre-computed authoritative in-flight refs
            from :func:`read_authoritative_in_flight_labels`. When
            ``None``, the function resolves them itself. **Appended to in
            place** for every engine launched here: the caller passes one
            list to all three dispatchers, so this is the tick's single
            record of what it has already launched, and the only evidence
            that survives a lagging GitHub read within one DISPATCH phase.
        impl_engine: ``"claude"`` (default) or ``"claude-ds"`` — the
            engine to use for implementation.
        model: Model override. Empty string (default) means "use the
            operator's own configured model" for claude, and
            ``deepseek-v4-pro[1m]`` for claude-ds. Non-empty overrides both
            the ``--model`` CLI flag and ``ANTHROPIC_MODEL`` (for claude-ds).
        permission_mode: Claude ``--permission-mode``: ``"auto"`` (default,
            classifier-gated) or ``"dontAsk"`` (deny calls that would prompt).
        max_in_flight: The admission cap. The tick always passes the value
            its startup gate validated; the default serves direct callers.
        timeout_seconds: Engine deadline from dispatch, recorded in the
            marker. The tick passes its validated value; the default serves
            direct callers.
        stall_seconds: Output silence that reports a stall, recorded in the
            dispatch marker for stall detection to read.
        stall_startup_grace_seconds: Extra silence allowed before the first
            output event, recorded beside ``stall_seconds``.
        only: Resolved ``--only`` scope items, or ``None``. When set, each
            listed issue is read directly in list order and the issue queue
            is not listed, so an unlisted issue is never read or written.
            Trust and eligibility gates still apply. HARVEST stays global.
    """
    # Target type gate: only dispatch for issue targets.
    if target is not None and target[0] != "issue":
        return

    # Resolve authoritative in-flight labels if not provided. A caller-supplied
    # list is used as-is and appended to in place: it is the tick's shared
    # record of what it has already launched, and copying it here would hide
    # this stage's launches from the stages that run after it.
    if authoritative_in_flight is None:
        try:
            authoritative_in_flight = read_authoritative_in_flight_labels()
        except (subprocess.CalledProcessError, ValueError):
            return

    # Filter on the server so other authors cannot consume the scan budget.
    # A targeted or --only run reads its issues directly, including the author:
    # a listed issue may be older than the bounded queue page, but never
    # bypasses trust. An unlisted issue is never read.
    try:
        if target is not None:
            candidate = gh_json(
                "issue", "view", str(target[1]),
                "--json", "number,title,author,state",
            )
            if not isinstance(candidate, dict) or candidate.get("state") != "OPEN":
                return
            available = [candidate]
        elif only is not None:
            available = _read_listed_issues(only, tick_log=tick_log)
        else:
            available = gh_json(
                "issue", "list",
                "--label", pipeline.LABEL_AVAILABLE,
                "--state", "open",
                "--author", TRUSTED_REVIEW_AUTHOR,
                "--limit", str(_ACTIONABLE_QUEUE_SCAN_LIMIT),
                "--json", "number,title,author",
            )
    except (subprocess.CalledProcessError, ValueError):
        return

    if not isinstance(available, list):
        return

    if target is None and only is None and tick_log is not None:
        _log_filtered_issue_count(available, tick_log)

    dispatched = 0
    for item in available:
        if not pipeline.trusted_issue_candidate(
            item, trusted_author=TRUSTED_REVIEW_AUTHOR,
        ):
            # No custody, body read, relabel, or escalation for other authors
            # (or metadata that cannot prove authorship).
            continue
        n = item.get("number")
        title = item.get("title")
        if not isinstance(n, int) or isinstance(n, bool):
            continue
        if not isinstance(title, str):
            title = ""

        # Target filter
        if target is not None and n != target[1]:
            continue

        # Eligibility gate: prove from the item's own label set that no engine
        # holds it and no human hold outranks it, before anything mutates.
        if _resolve_dispatchable_pickup_label(
            "issue", n,
            stage="implement",
            authoritative_in_flight=authoritative_in_flight,
            tick_log=tick_log,
        ) is None:
            continue

        wt_dir = os.path.join(WT_ROOT, f"issue-{n}")
        if _existing_marker_blocks_dispatch(
            wt_dir,
            entity="issue",
            number=n,
            pre_label=pipeline.LABEL_AVAILABLE,
            stage="implement",
            tick_log=tick_log,
        ):
            if tick_log:
                tick_log.log(
                    pr=n,
                    phase="dispatch",
                    stage="implement",
                    action="skip",
                    reason="existing dispatch marker retains custody",
                    engine=impl_engine,
                )
            continue

        if _provider_cooldown_blocks_dispatch(
            number=n,
            stage="implement",
            engine=impl_engine,
            tick_log=tick_log,
        ):
            continue

        # Admission check
        admission = can_dispatch_now(max_in_flight=max_in_flight)
        if not admission.can_dispatch:
            if tick_log:
                tick_log.log(
                    pr=n, phase="dispatch", stage="implement",
                    action="skip",
                    reason=f"admission: {admission.reason}",
                    engine=impl_engine,
                    details={
                        "in_flight": admission.in_flight_count,
                        "max": admission.max_in_flight,
                    },
                )
            continue

        # Fetch issue body for the implementer prompt.
        try:
            issue_body = gh_text(
                "issue", "view", str(n),
                "--json", "body", "-q", ".body",
            )
        except subprocess.CalledProcessError:
            issue_body = ""

        # Launch
        _dispatch_one_implementer(
            issue_number=n,
            issue_title=str(title),
            issue_body=issue_body,
            tick_log=tick_log,
            impl_engine=impl_engine,
            model=model,
            permission_mode=permission_mode,
            timeout_seconds=timeout_seconds,
            stall_seconds=stall_seconds,
            stall_startup_grace_seconds=stall_startup_grace_seconds,
        )
        dispatched += 1

        # Track this new engine in the in-flight set so subsequent
        # admission checks within the same tick count it.
        authoritative_in_flight.append(
            pipeline.InFlightRef(
                kind="issue", number=n, label=pipeline.LABEL_IMPLEMENTING,
            )
        )

    if tick_log and dispatched == 0 and len(available) > 0:
        tick_log.log(
            pr=None, phase="dispatch", stage="implement",
            action="summary",
            reason=f"available={len(available)}, dispatched=0",
            engine=impl_engine,
        )


def _read_listed_issues(
    only: Sequence[tuple[str, int]], *, tick_log: TickLog | None = None,
) -> list[dict]:
    """Read each listed issue directly, in list order, for implementer pickup.

    One unreadable, closed, or mismatched issue is skipped for this tick
    rather than blocking the rest of the list; its own label and trust gates
    still run in the dispatch loop for every issue returned here.

    Args:
        only: Resolved ``--only`` scope items; PR items are ignored.
        tick_log: Tick log handle for the per-issue ``skip`` record.

    Returns:
        Open issue payloads with ``number``, ``title``, ``author``, ``state``.
    """
    available: list[dict] = []
    for kind, number in only:
        if kind != "issue":
            continue
        try:
            payload = gh_json(
                "issue", "view", str(number),
                "--json", "number,title,author,state",
            )
        except (subprocess.CalledProcessError, ValueError) as exc:
            payload = None
            reason = f"listed issue unreadable: {type(exc).__name__}"
        else:
            reason = "listed issue is not an open issue with that number"
        if (
            isinstance(payload, dict)
            and payload.get("number") == number
            and payload.get("state") == "OPEN"
        ):
            available.append(payload)
        elif tick_log:
            tick_log.log(
                pr=number, phase="dispatch", stage="implement", action="skip",
                reason=f"{reason} — retried next tick",
            )
    return available


def _read_listed_prs(
    only: Sequence[tuple[str, int]], *, stage: str, tick_log: TickLog | None = None,
) -> list[int]:
    """Read each listed PR directly, in list order, for one PR stage's pickup.

    This replaces the stage's label-queue listing for an ``--only`` tick, so
    the bounded queue page cannot hide a listed PR. It applies the same
    selection the queue query applies — open, and carrying one of the stage's
    pickup labels — and leaves every eligibility, custody, trust, and
    admission decision to the dispatch loop. An unreadable, closed, or
    mismatched PR is skipped for this tick rather than blocking the rest of
    the list. A PR at another stage's label is not a candidate here.

    Args:
        only: Resolved ``--only`` scope items; issue items are ignored.
        stage: ``"review"`` or ``"fix"``.
        tick_log: Tick log handle for the per-PR ``skip`` record.

    Returns:
        Candidate PR numbers in list order.
    """
    pickup = set(pipeline.STAGE_PICKUP_LABELS[stage])
    candidates: list[int] = []
    for kind, number in only:
        if kind != "pr" or number in candidates:
            continue
        try:
            payload = gh_json(
                "pr", "view", str(number), "--json", "number,state,labels",
            )
        except (subprocess.CalledProcessError, ValueError) as exc:
            payload = None
            reason = f"listed PR unreadable: {type(exc).__name__}"
        else:
            reason = "listed PR is not an open PR with readable labels"
        # The strict parser rejects the whole payload when any label entry or
        # name is malformed, so a bad payload takes the logged skip below
        # instead of escaping as a TypeError.
        labels = _pipeline_labels(payload)
        names = set(labels) if labels is not None else None
        if (
            names is None
            or not isinstance(payload, dict)
            or payload.get("number") != number
            or payload.get("state") != "OPEN"
        ):
            if tick_log:
                tick_log.log(
                    pr=number, phase="dispatch", stage=stage, action="skip",
                    reason=f"{reason} — retried next tick",
                )
            continue
        if names & pickup:
            candidates.append(number)
    return candidates


def _dispatch_one_implementer(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str = "",
    tick_log: TickLog | None = None,
    impl_engine: str = "claude",
    model: str = "",
    permission_mode: str = "auto",
    timeout_seconds: int = IMPLEMENTER_TIMEOUT,
    stall_seconds: int = ENGINE_STALL_SECONDS,
    stall_startup_grace_seconds: int = ENGINE_STALL_STARTUP_GRACE_SECONDS,
) -> None:
    """Launch a single implementer engine for one issue.

    Creates a worktree, writes a lease marker, launches ``claude -p``
    as a subprocess, publishes the full dispatch marker with process
    identity, and relabels the issue to ``ai:implementing``. On launch
    failure, the worktree is cleaned up and the issue label is left
    unchanged.

    Args:
        issue_number: The GitHub issue number.
        issue_title: The issue title (for the prompt).
        issue_body: The issue body text (fetched by the caller).
        tick_log: Tick log handle, or ``None``.
        impl_engine: ``"claude"`` or ``"claude-ds"``.
        model: Model override. Empty string (default) omits ``--model`` for
            claude so the CLI uses the operator's configured model, and
            resolves to ``deepseek-v4-pro[1m]`` for claude-ds. Non-empty
            overrides both the ``--model`` CLI flag and ``ANTHROPIC_MODEL``.
        permission_mode: Claude ``--permission-mode``: ``"auto"`` (default,
            classifier-gated) or ``"dontAsk"`` (deny calls that would prompt).
        timeout_seconds: Engine deadline from dispatch, recorded in the
            marker. The tick passes its validated value; the default serves
            direct callers.
        stall_seconds: Output silence that reports a stall, recorded in the
            dispatch marker for stall detection to read.
        stall_startup_grace_seconds: Extra silence allowed before the first
            output event, recorded beside ``stall_seconds``.
    """
    wt_dir = os.path.join(WT_ROOT, f"issue-{issue_number}")
    dispatch_id = f"impl-{issue_number}-{int(time.time())}"

    # Custody guard: never dispatch over an existing marker whose engine
    # may still be alive (crash window between marker publish and relabel).
    if _existing_marker_blocks_dispatch(
        wt_dir,
        entity="issue",
        number=issue_number,
        pre_label=pipeline.LABEL_AVAILABLE,
        stage="implement",
        tick_log=tick_log,
    ):
        if tick_log:
            tick_log.log(
                pr=issue_number, phase="dispatch", stage="implement",
                action="skip",
                reason="existing dispatch marker retains custody",
                engine=impl_engine,
            )
        return

    # Resolve repo root for git operations.
    try:
        checkout_root = git("rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError:
        return

    # Resolve against the canonical target, not the checkout's current branch
    # or a potentially stale local remote HEAD. Explicit configuration wins.
    base_ref = pipeline.resolve_config(TRUSTED_HEAD_REPO).base_ref
    if not base_ref:
        try:
            metadata = gh_json(
                "repo", "view", f"github.com/{TRUSTED_HEAD_REPO}",
                "--json", "defaultBranchRef",
            )
        except (subprocess.CalledProcessError, ValueError):
            metadata = None
        default_branch = (
            metadata.get("defaultBranchRef") if isinstance(metadata, dict) else None
        )
        name = (
            default_branch.get("name") if isinstance(default_branch, dict) else None
        )
        if (
            not isinstance(name, str) or not name.strip()
            or name != name.strip() or name.startswith("-")
        ):
            _escalate_dispatch_setup_failure(
                entity="issue",
                number=issue_number,
                pre_label=pipeline.LABEL_AVAILABLE,
                stage="implement",
                reason=f"failed to resolve default branch for {TRUSTED_HEAD_REPO}",
                engine=impl_engine,
                tick_log=tick_log,
            )
            return
        base_ref = name
    try:
        git("-C", checkout_root, "fetch", "--quiet", TARGET_REMOTE, base_ref)
        base_sha = git("-C", checkout_root, "rev-parse", f"{TARGET_REMOTE}/{base_ref}")
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="issue",
            number=issue_number,
            pre_label=pipeline.LABEL_AVAILABLE,
            stage="implement",
            reason=f"failed to fetch base ref {base_ref!r}",
            engine=impl_engine,
            tick_log=tick_log,
        )
        return

    # Create an isolated git worktree at the selected base branch's tip.
    # The canonical branch name must match what harvest uses for
    # ``gh pr create --head``.
    branch_name = f"agent/issue-{issue_number}"
    try:
        git("-C", checkout_root, "worktree", "prune")
        # Delete stale branch from a prior failed dispatch (ignore errors).
        try:
            git("-C", checkout_root, "branch", "-D", branch_name)
        except subprocess.CalledProcessError:
            pass
        git("-C", checkout_root, "worktree", "add", "-b", branch_name, wt_dir, base_sha)
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="issue",
            number=issue_number,
            pre_label=pipeline.LABEL_AVAILABLE,
            stage="implement",
            reason=f"failed to create implementer worktree {branch_name!r}",
            engine=impl_engine,
            tick_log=tick_log,
            cleanup_dir=wt_dir,
        )
        return

    # Resolve every skill the implementer prompt binds against the install that
    # supplies them, before any lease or launch — the same gate the fixer runs.
    if not _bound_skills_resolve(
        wt_dir=wt_dir,
        skills=IMPLEMENTER_SKILLS,
        entity="issue",
        number=issue_number,
        pre_label=pipeline.LABEL_AVAILABLE,
        stage="implement",
        engine=impl_engine,
        tick_log=tick_log,
    ):
        return

    config = pipeline.resolve_config(TRUSTED_HEAD_REPO)
    dispatch_errors = _dispatch_config_errors(config)
    if dispatch_errors:
        _escalate_dispatch_setup_failure(
            entity="issue", number=issue_number,
            pre_label=pipeline.LABEL_AVAILABLE, stage="implement",
            reason="invalid dispatch configuration: " + "; ".join(dispatch_errors),
            engine=impl_engine, tick_log=tick_log, cleanup_dir=wt_dir,
        )
        return
    verify_tools = tuple(f"Bash({entry})" for entry in config.verify_tools)
    verification_unconfigured = not verify_tools

    deadline = time.time() + timeout_seconds

    # For implementers, head_sha is set to base_sha: the engine branches
    # from the selected base branch, so the initial "head" is the base tip. After the
    # engine commits, head advances, but HARVEST does not check head drift
    # for implementers (it harvests result.json, not review.json).
    lease_marker: dict[str, object] = {
        "engine": impl_engine,
        "stage": "implement",
        "pr_number": issue_number,
        "deadline": deadline,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_sha": base_sha,
        "pre_label": pipeline.LABEL_AVAILABLE,
        "dispatch_id": dispatch_id,
        "state": "launching",
        "verification_unconfigured": verification_unconfigured,
        "permission_mode": permission_mode,
        "verify_tools": list(config.verify_tools),
        "stall_seconds": stall_seconds,
        "stall_startup_grace_seconds": stall_startup_grace_seconds,
    }
    try:
        pipeline.atomic_write(
            os.path.join(wt_dir, "dispatch.json"),
            json.dumps(lease_marker),
        )
    except (OSError, ValueError):
        cleanup_scratch(wt_dir)
        if tick_log:
            tick_log.log(
                pr=issue_number, phase="dispatch", stage="implement",
                action="lease_write_failed",
                reason="atomic_write of lease marker raised OSError",
                engine=impl_engine,
            )
        return

    # Build the implementer prompt.
    if verification_unconfigured and tick_log:
        tick_log.log(
            pr=issue_number, phase="dispatch", stage="implement",
            action="verification_unconfigured", reason=pipeline.VERIFICATION_WARNING,
            dispatch_id=dispatch_id, engine=impl_engine,
        )
    prompt = load_prompt_template("implementer", {
        "target_remote": TARGET_REMOTE,
        "verification_context": _verification_prompt(verification_unconfigured),
        "issue_number": issue_number,
        "issue_title": issue_title,
        "issue_body": issue_body,
        "branch_name": branch_name,
        "pr_body_source_file": _PR_BODY_SOURCE_FILE,
        "definition_of_done_skill_doc": skill_doc_path(DEFINITION_OF_DONE_SKILL),
        "definition_of_done_skill_invocation": f"{native_skills.CLAUDE_NAMESPACE}:{DEFINITION_OF_DONE_SKILL}",
    })

    result_path = os.path.join(wt_dir, "result.json")

    # Resolve the effective model: "" means "use the operator's own default".
    # Must happen before both _resolve_engine (sets ANTHROPIC_MODEL for
    # claude-ds) and _launch_claude_engine (sets --model flag).
    #
    # claude-ds keeps a concrete default because the DeepSeek endpoint has no
    # notion of Anthropic model aliases — there is no operator default to
    # inherit. Plain claude stays "" so the flag is omitted and the CLI
    # resolves the operator's configured model.
    model = pipeline.effective_engine_model(impl_engine, model)

    # Resolve engine name to binary + extra env (claude-ds is a shell function).
    try:
        engine_bin, engine_extra_env = _resolve_engine(impl_engine, model=model)
    except RuntimeError:
        _escalate_dispatch_setup_failure(
            entity="issue",
            number=issue_number,
            pre_label=pipeline.LABEL_AVAILABLE,
            stage="implement",
            reason=f"engine {impl_engine!r} requires missing credentials",
            engine=impl_engine,
            tick_log=tick_log,
            cleanup_dir=wt_dir,
        )
        return

    proc = _launch_claude_engine(
        engine_bin=engine_bin,
        prompt=prompt,
        wt_dir=wt_dir,
        result_path=result_path,
        env={**_engine_env(), **engine_extra_env},
        push_permission=_implementer_push_permission(),
        model=model,
        permission_mode=permission_mode,
        verify_tools=verify_tools,
        engine_settings=config.engine_settings,
        skills=IMPLEMENTER_SKILLS,
    )
    if proc is None:
        # Launch failed — clean up and leave label unchanged.
        cleanup_scratch(wt_dir)
        if tick_log:
            tick_log.log(
                pr=issue_number, phase="dispatch", stage="implement",
                action="launch_failed",
                reason="target validation, prompt/result setup or subprocess.Popen failed",
                engine=impl_engine,
            )
        return

    _log_skill_context(
        tick_log=tick_log, number=issue_number, stage="implement",
        dispatch_id=dispatch_id, engine=impl_engine,
    )

    # Publish the full marker with process identity (with fallback).
    # With start_new_session=True, the child's pid equals its pgid.
    published_marker = dict(lease_marker)
    published_marker.pop("state", None)
    published_marker["pid"] = proc.pid
    published_marker["start_time"] = time.time()
    published_marker["pgid"] = proc.pid

    marker_ok = _publish_marker_or_revert(
        proc=proc,
        published_marker=published_marker,
        wt_dir=wt_dir,
        entity="issue",
        pr_number=issue_number,
        in_flight_label=pipeline.LABEL_IMPLEMENTING,
        pre_label=pipeline.LABEL_AVAILABLE,
        tick_log=tick_log,
        engine=impl_engine,
        stage="implement",
    )
    if not marker_ok:
        # Engine was terminated or fallback written; label not flipped.
        return

    # Relabel the issue.
    try:
        relabel("issue", issue_number, pipeline.LABEL_AVAILABLE,
                pipeline.LABEL_IMPLEMENTING)
    except subprocess.CalledProcessError:
        # Engine is running but label is stale — terminate. Only clean
        # up when confirmed dead; retain marker when kill fails.
        killed = _terminate_engine(
            proc, issue_number, "implement", impl_engine, tick_log,
        )
        if killed:
            _archive_and_cleanup_scratch(
                wt_root=WT_ROOT,
                wt_dir=wt_dir,
                kind="issue",
                number=issue_number,
                dispatch_id=dispatch_id,
                tick_log=tick_log,
                stage="implement",
                phase="dispatch",
                failure_reason=(
                    "retaining implementer scratch after relabel failure"
                ),
            )
        else:
            # Kill failed: marker is retained but ai:available is still
            # visible.  Without same-item exclusion the next tick would
            # re-dispatch for this issue.  Flip to ai:needs-human-decision
            # so dispatch skips it until a human investigates.
            try:
                _relabel_with_context(
                    "issue", issue_number, pipeline.LABEL_AVAILABLE,
                    pipeline.LABEL_NEEDS_HUMAN,
                    tick_log=tick_log, phase="dispatch", stage="implement",
                )
            except subprocess.CalledProcessError:
                pass
        return

    if tick_log:
        tick_log.log(
            pr=issue_number, phase="dispatch", stage="implement",
            action="launch",
            reason=f"pid={proc.pid}, title={issue_title[:60]}",
            engine=impl_engine,
            dispatch_id=dispatch_id,
            details={"model": model or None, "effort": None},
        )


def dispatch_fixers(
    tick_log: TickLog | None = None,
    target: tuple[str, int] | None = None,
    authoritative_in_flight: list[pipeline.InFlightRef] | None = None,
    *,
    impl_engine: str = "claude",
    model: str = "",
    permission_mode: str = "auto",
    max_in_flight: int = pipeline.DEFAULT_MAX_IN_FLIGHT,
    timeout_seconds: int = FIXER_TIMEOUT,
    stall_seconds: int = ENGINE_STALL_SECONDS,
    stall_startup_grace_seconds: int = ENGINE_STALL_STARTUP_GRACE_SECONDS,
    only: Sequence[tuple[str, int]] | None = None,
) -> None:
    """Dispatch fixer engines for PRs at ai:changes-requested.

    Queries open PRs with the ``ai:changes-requested`` label, checks the
    admission cap, and launches a fixer engine for each eligible PR.

    Args:
        tick_log: Tick log handle, or ``None`` to skip logging.
        target: Optional ``("pr", N)`` tuple to restrict to a single PR.
            Non-pr targets (e.g. ``("issue", 42)``) skip silently.
        authoritative_in_flight: Pre-computed authoritative in-flight refs
            from :func:`read_authoritative_in_flight_labels`. When
            ``None``, the function resolves them itself. **Appended to in
            place** for every engine launched here: the caller passes one
            list to all three dispatchers, so this is the tick's single
            record of what it has already launched, and the only evidence
            that survives a lagging GitHub read within one DISPATCH phase.
        impl_engine: ``"claude"`` (default) or ``"claude-ds"`` — the
            engine to use for fixing.
        model: Model override. Empty string (default) means "use the
            operator's own configured model" for claude, and
            ``deepseek-v4-pro[1m]`` for claude-ds. Non-empty overrides both
            the ``--model`` CLI flag and ``ANTHROPIC_MODEL`` (for claude-ds).
        permission_mode: Claude ``--permission-mode``: ``"auto"`` (default,
            classifier-gated) or ``"dontAsk"`` (deny calls that would prompt).
        max_in_flight: The admission cap. The tick always passes the value
            its startup gate validated; the default serves direct callers.
        timeout_seconds: Engine deadline from dispatch, recorded in the
            marker. The tick passes its validated value; the default serves
            direct callers.
        stall_seconds: Output silence that reports a stall, recorded in the
            dispatch marker for stall detection to read.
        stall_startup_grace_seconds: Extra silence allowed before the first
            output event, recorded beside ``stall_seconds``.
        only: Resolved ``--only`` scope items, or ``None``. When set, each
            listed PR is read directly in list order and the label queue is
            not listed, so a listed PR beyond the bounded queue page is still
            found and an unlisted PR is never read or written. Trust and
            eligibility gates still apply. Unlike ``target`` this scopes new
            launches alone; HARVEST stays global.
    """
    # Target type gate: only dispatch for pr targets.
    if target is not None and target[0] != "pr":
        return

    # Resolve authoritative in-flight labels if not provided. A caller-supplied
    # list is used as-is and appended to in place: it is the tick's shared
    # record of what it has already launched, and copying it here would hide
    # this stage's launches from the stages that run after it.
    if authoritative_in_flight is None:
        try:
            authoritative_in_flight = read_authoritative_in_flight_labels()
        except (subprocess.CalledProcessError, ValueError):
            return

    # An --only list reads its PRs directly, in list order, so a listed PR
    # beyond the bounded label page is still found and an unlisted PR is
    # never read. Otherwise query PRs with ai:changes-requested.
    if only is not None:
        candidates = _read_listed_prs(only, stage="fix", tick_log=tick_log)
    else:
        try:
            lines = gh_text(
                "pr", "list",
                "--label", pipeline.LABEL_CHANGES_REQUESTED,
                "--state", "open",
                # Match the actionable-queue scan limit: held candidates now
                # stay on their pre-dispatch label, so a default-size page
                # they fill would starve eligible work beyond it.
                "--limit", str(_ACTIONABLE_QUEUE_SCAN_LIMIT),
                "--json", "number",
                "-q", ".[].number",
            )
        except subprocess.CalledProcessError:
            return

        candidate_nums: set[int] = set()
        for line in lines.splitlines():
            line = line.strip()
            if line.isdigit():
                candidate_nums.add(int(line))
        candidates = sorted(candidate_nums)

    if not candidates:
        return

    for pr_number in candidates:
        # Target filter
        if target is not None and pr_number != target[1]:
            continue

        # Eligibility gate: prove from the item's own label set that no engine
        # holds it and no human hold outranks it, before anything mutates.
        if _resolve_dispatchable_pickup_label(
            "pr", pr_number,
            stage="fix",
            authoritative_in_flight=authoritative_in_flight,
            tick_log=tick_log,
        ) is None:
            continue

        wt_dir = os.path.join(WT_ROOT, f"pr-{pr_number}")
        if _existing_marker_blocks_dispatch(
            wt_dir,
            entity="pr",
            number=pr_number,
            pre_label=pipeline.LABEL_CHANGES_REQUESTED,
            stage="fix",
            tick_log=tick_log,
        ):
            if tick_log:
                tick_log.log(
                    pr=pr_number,
                    phase="dispatch",
                    stage="fix",
                    action="skip",
                    reason="existing dispatch marker retains custody",
                    engine=impl_engine,
                )
            continue

        if _provider_cooldown_blocks_dispatch(
            number=pr_number,
            stage="fix",
            engine=impl_engine,
            tick_log=tick_log,
        ):
            continue

        # Admission check
        admission = can_dispatch_now(max_in_flight=max_in_flight)
        if not admission.can_dispatch:
            if tick_log:
                tick_log.log(
                    pr=pr_number, phase="dispatch", stage="fix",
                    action="skip",
                    reason=f"admission: {admission.reason}",
                    engine=impl_engine,
                    details={
                        "in_flight": admission.in_flight_count,
                        "max": admission.max_in_flight,
                    },
                )
            continue

        # Launch
        _dispatch_one_fixer(
            pr_number=pr_number,
            tick_log=tick_log,
            impl_engine=impl_engine,
            model=model,
            permission_mode=permission_mode,
            timeout_seconds=timeout_seconds,
            stall_seconds=stall_seconds,
            stall_startup_grace_seconds=stall_startup_grace_seconds,
        )

        # Track this new engine in the in-flight set.
        authoritative_in_flight.append(
            pipeline.InFlightRef(
                kind="pr", number=pr_number, label=pipeline.LABEL_FIXING,
            )
        )


def _bounded_context(text: str, *, budget: int = FIXER_CONTEXT_BUDGET) -> str:
    """Cap one injected document, announcing the cut in the text itself.

    A silent truncation of a contract reads to the engine as a contract that
    stops — the requirement past the cut looks absent rather than unread, which
    is the failure the injection exists to prevent.

    Args:
        text: The document to bound.
        budget: Maximum characters to keep.

    Returns:
        ``text`` unchanged when it fits, else its prefix plus a stated cut.
    """
    if len(text) <= budget:
        return text
    return (
        f"{text[:budget]}\n\n[Truncated at {budget} characters. Treat anything "
        "past this point as unread, not as absent.]"
    )


def _split_context_budget(lengths: Sequence[int], budget: int) -> list[int]:
    """Divide one context budget across documents so none can starve another.

    A single cap over the joined documents is spent in reading order, so one
    oversized document consumes the whole budget and every later one disappears
    — heading included. That is strictly worse than a cut: a cut says "unread"
    about text whose start the engine can still see, while a dropped document
    leaves no trace that it was ever linked, and the truncation notice cannot
    name what it lost.

    The split is shortest-first with the unused remainder rolled forward, so a
    short document donates its slack instead of reserving an even share it
    cannot spend. One short contract beside one long one therefore leaves the
    long one nearly the whole budget, which a flat ``budget // n`` would have
    taken away for nothing.

    Args:
        lengths: Character length of each document, in the order they render.
        budget: Total characters available across all of them.

    Returns:
        One allowance per document, positionally aligned with ``lengths``.
    """
    allowances = [0] * len(lengths)
    remaining = budget
    shortest_first = sorted(range(len(lengths)), key=lambda index: lengths[index])
    for rank, index in enumerate(shortest_first):
        allowance = remaining // (len(lengths) - rank)
        allowances[index] = allowance
        remaining -= min(lengths[index], allowance)
    return allowances


class FixerPRContext(NamedTuple):
    """The governing PR context one fixer dispatch reads before it spends.

    Attributes:
        body_block: The PR body, bounded for substitution into the prompt.
        contract_block: Every linked issue's contract, bounded and headed.
        body_provenance: What the harvest needs pinned about the *unbounded*
            body to judge a replacement for it, or ``None`` when the body pins
            nothing (see :func:`_pr_body_provenance`). Derived from the whole
            body rather than the copy the engine was shown, because it has to
            fingerprint what GitHub holds.
    """

    body_block: str
    contract_block: str
    body_provenance: PRBodyProvenance | None


def _fixer_pr_context(
    pr_number: int, *, discussion: dict | None = None,
) -> FixerPRContext | None:
    """Read the PR body and the contract of every issue the PR closes.

    Both are governing inputs for a revision — `review-response-hygiene` rule 2
    asks whether the story surface still matches the head, and rule 5 asks
    whether the head satisfies the contract the review cited — and the engine
    has no GitHub access of its own to fetch either. Reading them here also
    keeps a failed authoritative read from silently becoming "no contract": a
    failure returns ``None`` and the caller retains custody for the next tick.

    Validate every linked repository and positive issue number before fetching
    any issue. Reads use that explicit repository, never the PR's target as a
    fallback. Explicit null bodies are empty; missing fields remain unknown.

    Dispatch supplies the complete acquired discussion, which uses GitHub
    closing references or the primary issue when GitHub reports no links. The
    standalone helper retains its reported-reference read for existing callers.
    Both paths share qualified identity validation. Replacement provenance uses
    :func:`_declared_closures` and the existing bounded-excerpt flag.

    Args:
        pr_number: The PR under repair.
        discussion: Complete validated launch acquisition, when called by dispatch.

    Returns:
        The :class:`FixerPRContext` for this dispatch, or ``None`` when a read
        failed or returned a payload to distrust.
    """
    if discussion is not None:
        body = discussion["pr"]["body"]
        contracts = [
            (issue["repository"], issue["number"], issue["title"], issue["body"].strip())
            for issue in discussion["issues"]
        ]
    else:
        try:
            pr_data = gh_json(
                "pr", "view", str(pr_number),
                "--json", "body,closingIssuesReferences",
            )
        except (subprocess.CalledProcessError, ValueError):
            return None
        if not isinstance(pr_data, dict):
            return None
        body = pr_data.get("body")
        if body is None and "body" in pr_data:
            body = ""
        refs = pr_data.get("closingIssuesReferences")
        if not isinstance(body, str) or not isinstance(refs, list):
            return None

        try:
            issue_refs = discussion_context.qualified_references(refs)
        except discussion_context.ContextError:
            return None

        contracts: list[tuple[str, int, str, str]] = []
        for repository, number in issue_refs:
            try:
                issue = gh_json(
                    "issue", "view", str(number), "--json", "title,body",
                    repo=repository,
                )
            except (subprocess.CalledProcessError, ValueError):
                return None
            if not isinstance(issue, dict):
                return None
            title = issue.get("title")
            issue_body = issue.get("body")
            if issue_body is None and "body" in issue:
                issue_body = ""
            if not isinstance(title, str) or not isinstance(issue_body, str):
                return None
            contracts.append((repository, number, title, issue_body.strip()))

    # Every linked issue keeps its heading and an allowance of its own. The
    # heading sits outside the allowance because it is what tells the engine the
    # contract exists: a fixer can act on "issue #8 is here and cut short", and
    # cannot act on an issue #8 that renders as nothing at all.
    allowances = _split_context_budget(
        [len(requirements) for _, _, _, requirements in contracts],
        FIXER_CONTEXT_BUDGET,
    )
    blocks = [
        f"### Issue {repository}#{number} — {title}\n\n"
        f"{_bounded_context(requirements, budget=allowance) or '(this issue has an empty body)'}"
        for (repository, number, title, requirements), allowance in zip(
            contracts, allowances, strict=True,
        )
    ]

    body_block = body.strip() or "(this PR has an empty body)"
    contract_block = "\n\n".join(blocks) or (
        "(GitHub reports no issue this PR closes, so there is no linked "
        "contract to re-read)"
    )
    bounded_body = _bounded_context(body_block)
    return FixerPRContext(
        body_block=bounded_body,
        contract_block=contract_block,
        # Pinned against the whole body and the *bounded* one together: the
        # digest and closure contract describe what GitHub holds, and the
        # truncation flag retains the existing guard on a shortened excerpt.
        body_provenance=_pr_body_provenance(
            body, truncated=bounded_body != body_block,
        ),
    )


def _dispatch_one_fixer(
    *,
    pr_number: int,
    tick_log: TickLog | None = None,
    impl_engine: str = "claude",
    model: str = "",
    permission_mode: str = "auto",
    timeout_seconds: int = FIXER_TIMEOUT,
    stall_seconds: int = ENGINE_STALL_SECONDS,
    stall_startup_grace_seconds: int = ENGINE_STALL_STARTUP_GRACE_SECONDS,
) -> None:
    """Launch a single fixer engine for one PR.

    Creates a worktree, writes a lease marker, and launches
    ``claude -p`` as a subprocess. On launch failure, the worktree is
    cleaned up and the PR label is left unchanged.

    Args:
        pr_number: The PR number to fix.
        tick_log: Tick log handle, or ``None``.
        impl_engine: ``"claude"`` or ``"claude-ds"``.
        model: Model override. Empty string (default) omits ``--model`` for
            claude so the CLI uses the operator's configured model, and
            resolves to ``deepseek-v4-pro[1m]`` for claude-ds. Non-empty
            overrides both the ``--model`` CLI flag and ``ANTHROPIC_MODEL``.
        permission_mode: Claude ``--permission-mode``: ``"auto"`` (default,
            classifier-gated) or ``"dontAsk"`` (deny calls that would prompt).
        timeout_seconds: Engine deadline from dispatch, recorded in the
            marker. The tick passes its validated value; the default serves
            direct callers.
        stall_seconds: Output silence that reports a stall, recorded in the
            dispatch marker for stall detection to read.
        stall_startup_grace_seconds: Extra silence allowed before the first
            output event, recorded beside ``stall_seconds``.
    """
    wt_dir = os.path.join(WT_ROOT, f"pr-{pr_number}")
    dispatch_id = f"fix-{pr_number}-{int(time.time())}-{uuid.uuid4().hex}"

    # Custody guard: never dispatch over an existing marker whose engine
    # may still be alive (crash window between marker publish and relabel).
    if _existing_marker_blocks_dispatch(
        wt_dir,
        entity="pr",
        number=pr_number,
        pre_label=pipeline.LABEL_CHANGES_REQUESTED,
        stage="fix",
        tick_log=tick_log,
    ):
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage="fix",
                action="skip",
                reason="existing dispatch marker retains custody",
                engine=impl_engine,
            )
        return

    try:
        checkout_root = git("rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError:
        return

    # Same contract as reviewer dispatch: the base ref must come from
    # authoritative PR metadata — never guess "main" (a stacked PR's base is
    # another branch, and the fixer marker's base_sha would record the wrong
    # target). Retain the PR and let the next tick retry.
    try:
        base_ref = gh_text(
            "pr", "view", str(pr_number),
            "--json", "baseRefName", "-q", ".baseRefName",
        )
    except subprocess.CalledProcessError:
        base_ref = ""
    if not base_ref:
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage="fix",
                action="skip",
                reason="failed to read PR base ref — retained for retry",
                engine=impl_engine,
            )
        return

    try:
        git("-C", checkout_root, "fetch", "--quiet", TARGET_REMOTE, base_ref)
        base_sha = git("-C", checkout_root, "rev-parse", f"{TARGET_REMOTE}/{base_ref}")
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pipeline.LABEL_CHANGES_REQUESTED,
            stage="fix",
            reason=f"failed to fetch base ref {base_ref!r}",
            engine=impl_engine,
            tick_log=tick_log,
        )
        return

    try:
        head_sha = gh_text(
            "pr", "view", str(pr_number),
            "--json", "headRefOid", "-q", ".headRefOid",
        )
    except subprocess.CalledProcessError:
        return

    if not head_sha:
        return

    try:
        head_ref = gh_text(
            "pr", "view", str(pr_number),
            "--json", "headRefName", "-q", ".headRefName",
        )
    except subprocess.CalledProcessError:
        return

    if not head_ref:
        return

    # The head ref is templated into the fixer's exact push permission and
    # prompt; validate its shape before it reaches either surface (or any git
    # argv). An unsafe ref fails closed to a human instead of launching.
    if not _valid_push_head_ref(head_ref):
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pipeline.LABEL_CHANGES_REQUESTED,
            stage="fix",
            reason=f"PR head ref {head_ref!r} fails push-refspec validation",
            engine=impl_engine,
            tick_log=tick_log,
        )
        return

    # The story surface and the contract the review cites are governing inputs
    # for a revision, and the engine cannot fetch either itself. Read them here,
    # before anything is created, so a failed or untrustworthy read costs one
    # skipped dispatch instead of a fixer working from a contract it never saw.
    discussion = _read_pr_discussion(
        pr_number, stage="fix", engine=impl_engine, tick_log=tick_log,
    )
    if discussion is None:
        return
    pr_context = _fixer_pr_context(pr_number, discussion=discussion)
    assert pr_context is not None  # The acquisition validated each source.

    # Authenticate history before creating scratch or acquiring a launch lease.
    comments = _read_comment_history(
        pr_number, phase="dispatch", stage="fix", tick_log=tick_log,
    )
    if comments is None:
        return
    reviews = pipeline.trusted_marker_comments(
        comments, pipeline.REVIEW_MARKER, trusted_author=TRUSTED_REVIEW_AUTHOR,
    )
    review_body = reviews[-1]["body"] if reviews else ""
    if not review_body:
        _escalate_dispatch_setup_failure(
            entity="pr", number=pr_number,
            pre_label=pipeline.LABEL_CHANGES_REQUESTED, stage="fix",
            reason="no trusted pipeline review comment found — nothing to fix",
            engine=impl_engine, tick_log=tick_log,
        )
        return

    try:
        review_handoff = pipeline.review_reports.handoff(review_body)
    except ValueError as exc:
        _escalate_dispatch_setup_failure(
            entity="pr", number=pr_number,
            pre_label=pipeline.LABEL_CHANGES_REQUESTED, stage="fix",
            reason=f"review finding identity is ambiguous: {exc}",
            engine=impl_engine, tick_log=tick_log,
        )
        return

    # Create an isolated git worktree at the remote tip of the PR branch in
    # detached HEAD mode. Detached HEAD avoids the "branch already checked
    # out in another worktree" error that occurs when a stale implementer
    # worktree still holds a local reference to the same branch. The fixer
    # prompt uses ``git push <remote> HEAD:refs/heads/<head_ref>`` (explicit
    # fully-qualified refspec, mirrored exactly by the push permission in the
    # tool allowlist) so the push lands on the correct remote branch without
    # needing a local branch ref in the worktree.
    try:
        git("-C", checkout_root, "worktree", "prune")
        git("-C", checkout_root, "fetch", "--quiet", TARGET_REMOTE, head_ref)
        git("-C", checkout_root, "worktree", "add",
            "--detach", wt_dir, f"{TARGET_REMOTE}/{head_ref}")
    except subprocess.CalledProcessError:
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pipeline.LABEL_CHANGES_REQUESTED,
            stage="fix",
            reason=f"failed to create fixer worktree {head_ref!r}",
            engine=impl_engine,
            tick_log=tick_log,
            cleanup_dir=wt_dir,
        )
        return

    # Resolve every skill the fixer prompt binds against the install that
    # supplies them. Nothing has been leased or relabelled yet, so an
    # unresolvable binding costs one worktree and escalates before any spend.
    if not _bound_skills_resolve(
        wt_dir=wt_dir,
        skills=FIXER_SKILLS,
        entity="pr",
        number=pr_number,
        pre_label=pipeline.LABEL_CHANGES_REQUESTED,
        stage="fix",
        engine=impl_engine,
        tick_log=tick_log,
    ):
        return

    config = pipeline.resolve_config(TRUSTED_HEAD_REPO)
    dispatch_errors = _dispatch_config_errors(config)
    if dispatch_errors:
        _escalate_dispatch_setup_failure(
            entity="pr", number=pr_number,
            pre_label=pipeline.LABEL_CHANGES_REQUESTED, stage="fix",
            reason="invalid dispatch configuration: " + "; ".join(dispatch_errors),
            engine=impl_engine, tick_log=tick_log, cleanup_dir=wt_dir,
        )
        return
    verify_tools = tuple(f"Bash({entry})" for entry in config.verify_tools)
    verification_unconfigured = not verify_tools

    deadline = time.time() + timeout_seconds

    lease_marker: dict[str, object] = {
        "engine": impl_engine,
        "stage": "fix",
        "pr_number": pr_number,
        "deadline": deadline,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "pre_label": pipeline.LABEL_CHANGES_REQUESTED,
        "dispatch_id": dispatch_id,
        "state": "launching",
        "verification_unconfigured": verification_unconfigured,
        "stall_seconds": stall_seconds,
        "stall_startup_grace_seconds": stall_startup_grace_seconds,
    }
    # PR-body revision provenance, pinned here because none of it can be
    # recovered at harvest: by then the body may already have been replaced (by
    # this run) or edited (by a human), and either reading would defeat the
    # check it feeds. One block rather than loose keys, so the harvest can never
    # read half a contract — a marker carrying a digest but no closure set is
    # corruption, not a legacy marker, and has to be rejected as a unit.
    if pr_context.body_provenance is not None:
        lease_marker["pr_body"] = pr_context.body_provenance.as_marker_field()

    context_values = _write_pr_discussion(
        wt_dir, discussion,
        {"review_body": review_body, "pr_body": pr_context.body_block,
         "issue_contract": pr_context.contract_block},
        pr_number=pr_number, stage="fix", engine=impl_engine, tick_log=tick_log,
    )
    if context_values is None:
        return

    # Bind the exact finding set before spawning. Its content fingerprint travels
    # through harvest's comment so re-review cannot substitute a later review.
    lease_marker["review_sha256"] = hashlib.sha256(review_body.encode()).hexdigest()
    lease_marker["review_handoff"] = review_handoff
    lease_marker["report_protocol_version"] = pipeline.fixer_reports.VERSION
    try:
        pipeline.atomic_write(
            os.path.join(wt_dir, "dispatch.json"),
            json.dumps(lease_marker),
        )
    except (OSError, ValueError):
        cleanup_scratch(wt_dir)
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage="fix",
                action="lease_write_failed",
                reason="atomic_write of lease marker raised OSError",
                engine=impl_engine,
            )
        return

    # Build the fixer prompt with the actual review findings. The skill doc path
    # and the summary budget are substituted rather than written into the
    # template: the doc path is the same string dispatch resolved above, and the
    # budget is the harvest's own publish limit, so neither can drift into a
    # prompt that names a doc the engine cannot open or a size the comment
    # truncates.
    if verification_unconfigured and tick_log:
        tick_log.log(
            pr=pr_number, phase="dispatch", stage="fix",
            action="verification_unconfigured", reason=pipeline.VERIFICATION_WARNING,
            dispatch_id=dispatch_id, engine=impl_engine,
        )
    prompt = load_prompt_template("fixer", {
        "target_remote": TARGET_REMOTE,
        "verification_context": (
            "No operator-defined verification grants. Set verification_waiver to "
            + pipeline.VERIFICATION_WAIVER + "; this waiver is not evidence that checks passed. "
            "Required checks remain mandatory; if unavailable use NEEDS_HUMAN_DECISION."
            if verification_unconfigured else "Operator verification grants are configured. Required checks must pass."
        ),
        "pr_number": pr_number,
        **context_values,
        "finding_references": json.dumps(review_handoff, ensure_ascii=False),
        "head_ref": head_ref,
        "revision_hygiene_skill_doc": skill_doc_path(REVISION_HYGIENE_SKILL),
        "revision_hygiene_skill_invocation": f"{native_skills.CLAUDE_NAMESPACE}:{REVISION_HYGIENE_SKILL}",
        "summary_budget": pipeline.fixer_reports.AUTHOR_ALLOWANCE,
        "report_binding": json.dumps({
            "report_protocol_version": pipeline.fixer_reports.VERSION,
            "dispatch_id": dispatch_id, "pinned_head": head_sha,
            "review_sha256": lease_marker["review_sha256"],
        }),
        "definition_of_done_skill_doc": skill_doc_path(DEFINITION_OF_DONE_SKILL),
        "definition_of_done_skill_invocation": f"{native_skills.CLAUDE_NAMESPACE}:{DEFINITION_OF_DONE_SKILL}",
        "pr_body_revision_file": _PR_BODY_REVISION_FILE,
        # The whole stamp line, not its prefix: the engine copies a literal it
        # was given rather than composing one from a dispatch id it would have
        # to be told separately, and the harvest compares the same literal.
        "pr_body_revision_stamp": f"{_PR_BODY_REVISION_STAMP} {dispatch_id}",
        # The head stamp is the prefix only. Its value is the OID the engine
        # will create, which does not exist yet — dispatch can pin the dispatch
        # id because it owns it, and cannot pin a commit for the same reason.
        "pr_body_revision_head_stamp": _PR_BODY_REVISION_HEAD_STAMP,
    })

    # Resolve the effective model: "" means "use the operator's own default".
    # Symmetric with the implementer — see _dispatch_one_implementer for why
    # claude-ds keeps a concrete default and plain claude does not.
    model = pipeline.effective_engine_model(impl_engine, model)

    # Resolve engine name to binary + extra env (claude-ds is a shell function).
    try:
        engine_bin, engine_extra_env = _resolve_engine(impl_engine, model=model)
    except RuntimeError:
        _escalate_dispatch_setup_failure(
            entity="pr",
            number=pr_number,
            pre_label=pipeline.LABEL_CHANGES_REQUESTED,
            stage="fix",
            reason=f"engine {impl_engine!r} requires missing credentials",
            engine=impl_engine,
            tick_log=tick_log,
            cleanup_dir=wt_dir,
        )
        return

    result_path = os.path.join(wt_dir, "result.json")
    proc = _launch_claude_engine(
        engine_bin=engine_bin,
        prompt=prompt,
        wt_dir=wt_dir,
        result_path=result_path,
        env={**_engine_env(), **engine_extra_env},
        push_permission=_fixer_push_permission(head_ref),
        model=model,
        permission_mode=permission_mode,
        verify_tools=verify_tools,
        engine_settings=config.engine_settings,
        skills=FIXER_SKILLS,
        fix_report_protocol_version=pipeline.fixer_reports.VERSION,
    )
    if proc is None:
        cleanup_scratch(wt_dir)
        if tick_log:
            tick_log.log(
                pr=pr_number, phase="dispatch", stage="fix",
                action="launch_failed",
                reason="target validation, prompt/result setup or subprocess.Popen failed",
                engine=impl_engine,
            )
        return

    _log_skill_context(
        tick_log=tick_log, number=pr_number, stage="fix",
        dispatch_id=dispatch_id, engine=impl_engine,
    )

    # Publish the full marker with process identity (with fallback).
    # With start_new_session=True, the child's pid equals its pgid.
    published_marker = dict(lease_marker)
    published_marker.pop("state", None)
    published_marker["pid"] = proc.pid
    published_marker["start_time"] = time.time()
    published_marker["pgid"] = proc.pid

    marker_ok = _publish_marker_or_revert(
        proc=proc,
        published_marker=published_marker,
        wt_dir=wt_dir,
        entity="pr",
        pr_number=pr_number,
        in_flight_label=pipeline.LABEL_FIXING,
        pre_label=pipeline.LABEL_CHANGES_REQUESTED,
        tick_log=tick_log,
        engine=impl_engine,
        stage="fix",
    )
    if not marker_ok:
        return

    # Relabel the PR.
    try:
        relabel(
            "pr", pr_number,
            pipeline.LABEL_CHANGES_REQUESTED,
            pipeline.LABEL_FIXING,
        )
    except subprocess.CalledProcessError:
        # Engine is running but label is stale — terminate. Only clean
        # up when confirmed dead; retain marker when kill fails.
        killed = _terminate_engine(proc, pr_number, "fix", impl_engine, tick_log)
        if killed:
            _archive_and_cleanup_scratch(
                wt_root=WT_ROOT,
                wt_dir=wt_dir,
                kind="pr",
                number=pr_number,
                dispatch_id=dispatch_id,
                tick_log=tick_log,
                stage="fix",
                phase="dispatch",
                failure_reason="retaining fixer scratch after relabel failure",
            )
        else:
            # Kill failed: marker is retained but ai:changes-requested is
            # still visible.  Flip to ai:needs-human-decision so the next
            # tick does not re-dispatch for this PR.
            try:
                _relabel_with_context(
                    "pr", pr_number, pipeline.LABEL_CHANGES_REQUESTED,
                    pipeline.LABEL_NEEDS_HUMAN,
                    tick_log=tick_log, phase="dispatch", stage="fix",
                )
            except subprocess.CalledProcessError:
                pass
        return

    if tick_log:
        tick_log.log(
            pr=pr_number, phase="dispatch", stage="fix",
            action="launch",
            reason=f"pid={proc.pid}",
            engine=impl_engine,
            dispatch_id=dispatch_id,
            details={"head_sha": head_sha[:8], "model": model or None, "effort": None},
        )


# --- Startup diagnostics ---


def _startup_probe() -> dict[str, object]:
    """Verify that required external binaries are reachable on PATH.

    An E2BIG, missing binary, or broken PATH kills the tick process before
    any logic runs. Recording probe results gives the operator a baseline to
    correlate OS-level issues. Uses ``shutil.which`` (not ``subprocess.run``)
    so the probe itself is cheap and does not spawn a child process.

    Returns:
        A dict with ``gh_found``, ``git_found``, ``codex_found`` (bool) and
        ``gh_path``, ``git_path``, ``codex_path`` (str — empty when not found).
    """
    probe: dict[str, object] = {}
    for binary in ("gh", "git", "codex"):
        path = shutil.which(binary)
        probe[f"{binary}_found"] = path is not None
        probe[f"{binary}_path"] = path or ""
    return probe


def _log_startup_context(tick_log: TickLog) -> None:
    """Write startup context as the first tick log entry.

    Records Python version, working directory, PATH, and tick PID so the
    tick log is self-describing — no need to cross-reference shell history
    to know what environment ran a given tick.

    Writes a raw enriched entry directly (same pattern as
    :func:`_log_startup_probe`) because ``TickLog.log`` only accepts the
    fixed set of fields and cannot carry ``python_version``, ``cwd``,
    ``path``, or ``pid``.

    Args:
        tick_log: The tick log handle (already opened).
    """
    enriched: dict[str, object] = {
        "ts": int(time.time()),
        "pr": None,
        "phase": "startup",
        "stage": "",
        "action": "startup_context",
        "reason": "",
        "engine": "",
        "python_version": platform.python_version(),
        "cwd": os.getcwd(),
        "path": os.environ.get("PATH", "").split(os.pathsep),
        "pid": os.getpid(),
    }
    try:
        with open(tick_log.path, "a") as fh:
            fh.write(json.dumps(enriched) + "\n")
    except OSError:
        pass


def _log_startup_probe(tick_log: TickLog, probe: dict[str, object]) -> None:
    """Write the startup probe results as a tick log entry.

    Args:
        tick_log: The tick log handle.
        probe: The dict returned by :func:`_startup_probe`.
    """
    # Write a raw enriched entry so we can include the path fields.
    entry: dict[str, object] = {
        "ts": int(time.time()),
        "pr": None,
        "phase": "startup",
        "stage": "",
        "action": "startup_probe",
        "reason": "",
        "engine": "",
    }
    entry.update(probe)
    try:
        with open(tick_log.path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# --- main() (Step 11) ---


def _parse_target(raw: str | None) -> tuple[str, int] | None:
    """Parse a positional target argument into ``(type, number)``.

    Args:
        raw: Target string like ``"pr:42"`` or ``"#42"``, or ``None``.

    Returns:
        A ``(type, number)`` tuple, or ``None`` for full scan.

    Raises:
        SystemExit: If the target format is invalid.
    """
    if raw is None or raw == "":
        return None

    if raw.startswith("pr:"):
        num_str = raw[3:]
        if not num_str.isdigit():
            print(f"Invalid target: {raw}", file=sys.stderr)
            raise SystemExit(1)
        return ("pr", int(num_str))
    elif raw.startswith("#"):
        num_str = raw[1:]
        if not num_str.isdigit():
            print(f"Invalid target: {raw}", file=sys.stderr)
            raise SystemExit(1)
        return ("issue", int(num_str))
    elif raw:
        print(f"Invalid target: {raw}", file=sys.stderr)
        raise SystemExit(1)

    return None


def _resolve_stage_targets(
    target: tuple[str, int] | None,
) -> StageTargets:
    """Resolve a CLI target into reviewer, implementer, and fixer scopes.

    Issue targets retain issue scope for implementation. When GitHub reports
    one open PR that closes the issue, review and fix stages instead target
    that PR. The response is validated strictly so an incomplete or ambiguous
    read cannot broaden targeted work.

    Args:
        target: Parsed CLI target, or ``None`` for an untargeted tick.

    Returns:
        A target for each pipeline stage.

    Raises:
        subprocess.CalledProcessError: If the GitHub read fails.
        ValueError: If the GitHub response is malformed or ambiguous.
    """
    targets = StageTargets(
        reviewer=target,
        implementer=target,
        fixer=target,
    )
    if target is None or target[0] != "issue":
        return targets

    issue_number = target[1]
    payload = gh_json(
        "issue", "view", str(issue_number),
        "--json", "closedByPullRequestsReferences",
    )
    if not isinstance(payload, dict):
        raise ValueError("issue closing PR response must be an object")
    references = payload.get("closedByPullRequestsReferences")
    if not isinstance(references, list):
        raise ValueError("issue closing PR references must be a list")

    open_prs: list[int] = []
    trusted_owner, trusted_name = TRUSTED_HEAD_REPO.split("/", 1)
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError("closing PR reference must be an object")
        number = reference.get("number")
        node_id = reference.get("id")
        url = reference.get("url")
        repository = reference.get("repository")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
            or not isinstance(node_id, str)
            or not node_id
            or not isinstance(url, str)
            or not url
            or not isinstance(repository, dict)
        ):
            raise ValueError("closing PR reference is malformed")

        owner = repository.get("owner")
        if (
            not pipeline.same_repository(repository.get("name"), trusted_name)
            or not isinstance(owner, dict)
            or not pipeline.same_repository(owner.get("login"), trusted_owner)
        ):
            raise ValueError("closing PR reference belongs to an untrusted repository")

        pr_payload = gh_json(
            "pr", "view", str(number),
            "--json", "state,number",
        )
        if not isinstance(pr_payload, dict):
            raise ValueError("closing PR state response must be an object")
        resolved_number = pr_payload.get("number")
        state = pr_payload.get("state")
        if (
            not isinstance(resolved_number, int)
            or isinstance(resolved_number, bool)
            or resolved_number <= 0
            or resolved_number != number
            or not isinstance(state, str)
            or state not in {"OPEN", "CLOSED", "MERGED"}
        ):
            raise ValueError("closing PR state response has invalid number or state")
        if state == "OPEN":
            open_prs.append(number)

    if len(open_prs) > 1:
        raise ValueError("issue has multiple open closing PRs")
    if open_prs:
        pr_target = ("pr", open_prs[0])
        targets["reviewer"] = pr_target
        targets["fixer"] = pr_target
    return targets


_ONLY_TOKEN = re.compile(r"(#|pr:)([0-9]+)")


def _parse_only_list(raw: str) -> tuple[tuple[str, int], ...]:
    """Parse a ``--only`` value into ordered, unique ``(type, number)`` pairs.

    Tokens use the positional target's grammar (``#N`` for an issue, ``pr:N``
    for a PR) with a positive ASCII number. Surrounding spaces are ignored.
    Order is the operator's priority statement, so a duplicate keeps its
    *first* position.

    Args:
        raw: The comma-separated list, for example ``"#598,pr:660"``.

    Returns:
        The listed items in first-occurrence order.

    Raises:
        SystemExit: If the list or any element is empty or malformed. The
            whole run is refused rather than narrowed: a dropped element would
            dispatch a different set than the operator asked for, and that
            reads exactly like success.
    """
    resolved: list[tuple[str, int]] = []
    for element in raw.split(","):
        token = element.strip()
        match = _ONLY_TOKEN.fullmatch(token)
        if not token:
            print(f"tick: invalid --only list {raw!r}: empty element", file=sys.stderr)
            raise SystemExit(1)
        if match is None or int(match.group(2)) <= 0:
            print(
                f"tick: invalid --only element {token!r}: use '#N' or 'pr:N' "
                "with a positive number",
                file=sys.stderr,
            )
            raise SystemExit(1)
        item = ("issue" if match.group(1) == "#" else "pr", int(match.group(2)))
        if item not in resolved:
            resolved.append(item)
    return tuple(resolved)


def _only_token(item: tuple[str, int]) -> str:
    """Render one ``--only`` item in the operator's own token grammar."""
    entity, number = item
    return f"#{number}" if entity == "issue" else f"pr:{number}"


def _only_text(items: Sequence[tuple[str, int]]) -> str:
    """Render ``--only`` items as the comma-separated text an operator types."""
    return ",".join(_only_token(item) for item in items)


class OnlyScope(NamedTuple):
    """One tick's resolved ``--only`` dispatch scope.

    Attributes:
        listed: The parsed list, in operator order.
        items: The items dispatch may launch and cadence observes, in list
            order. A listed issue is followed by its one open closing PR, so
            review and fix can finish work the list started.
    """

    listed: tuple[tuple[str, int], ...]
    items: tuple[tuple[str, int], ...]


def _resolve_only_scope(only: tuple[tuple[str, int], ...]) -> OnlyScope:
    """Expand an ``--only`` list once for this tick's dispatch and cadence.

    Each entry goes through :func:`_resolve_stage_targets`, the strict
    zero-or-one closing-PR resolver the positional target uses; there is no
    second resolver. A listed issue keeps its implementation scope and gains
    its one open closing PR immediately after it. Duplicates keep their first
    position. The tick calls this once, after HARVEST, and reuses the result
    for all three dispatch stages and the cadence report; the next tick reads
    GitHub again so a closing PR opened in between joins the scope.

    Args:
        only: The parsed list, in operator order.

    Returns:
        The resolved scope.

    Raises:
        subprocess.CalledProcessError: If a GitHub read fails.
        ValueError: If a closing-PR response is malformed or ambiguous.
    """
    items: list[tuple[str, int]] = []
    for entry in only:
        stages = _resolve_stage_targets(entry)
        for scoped in (stages["implementer"], stages["reviewer"], stages["fixer"]):
            if scoped is not None and scoped not in items:
                items.append(scoped)
    return OnlyScope(listed=only, items=tuple(items))


def _only_counter_scope(only: Sequence[tuple[str, int]]) -> str:
    """Return the quiet-counter key for one ``--only`` list.

    The key names the listed *set*: reordering the same items keeps one quiet
    streak, while a different set starts its own. It never shares a file with
    the full-queue counter or a positional target's counter.
    """
    canonical = _only_text(sorted(set(only)))
    return "only-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def main(
    target: str | None = None,
    *,
    only: str = "",
    impl_engine: str = "",
    model: str = "",
    fix_engine: str = "",
    fix_model: str = "",
    review_model: str = "",
    review_effort: str = "",
    permission_mode: str = "",
    loop: bool = False,
) -> int:
    """Run one PR-pipeline tick: HARVEST → DISPATCH → Summary.

    Every engine-route argument is the explicit per-invocation flag. Empty
    (the default) means no flag was typed, so the operator's configured value
    applies (``impl_engine``, ``model``, ``fix_engine``, ``fix_model``,
    ``review_model``, ``review_effort`` in ``[defaults]`` or
    ``[repos."owner/name"]``). A typed value outranks every config layer and
    is validated like one at startup.

    Args:
        target: Optional targeting argument (``"pr:N"``, ``"#N"``).
        only: Optional ordered dispatch list (``"#598,pr:660"``). It bounds
            new launches only; HARVEST, stale reconciliation, and occupancy
            stay global. Empty means no list. Mutually exclusive with
            ``target``.
        impl_engine: Engine for implementation dispatch (``"claude"`` or
            ``"claude-ds"``).
        model: Implementer model. A resolved blank omits ``--model`` so the
            ``claude`` CLI resolves it (project settings outrank user settings
            there), and means ``deepseek-v4-pro[1m]`` for claude-ds (which has
            no operator default to inherit).
        fix_engine: Engine for fixer dispatch. A resolved blank is the
            implementer's engine.
        fix_model: Fixer model. A resolved blank inherits the implementer's
            model while the fixer runs on the implementer's engine.
        review_model: Model for the reviewer engine. A resolved blank reads
            the operator's codex config (see :func:`_reviewer_config_path`),
            falling back to the pinned default.
        review_effort: Reasoning effort for the reviewer engine, resolved the
            same way as ``review_model``.
        permission_mode: Engine ``--permission-mode`` for implementer and
            fixer engines: ``"auto"`` (classifier-gated) or ``"dontAsk"``.
            Empty (default) resolves the operator's configured value, which
            the packaged floor sets to ``"auto"``. A value passed here is the
            explicit per-invocation flag and outranks every config layer.
        loop: When True, run ticks in-process until interrupted or quiescent.

    Returns:
        Process exit code: 0 = full tick completed, 1 = reconcile failed
        (dispatch gated), 2 = reconcile succeeded but authoritative in-flight
        label read failed (dispatch gated, retryable), 3 = startup probe
        failed (required binary missing), 4 = unhandled exception, 5 = another
        tick already holds the mutex, 6 = the target has no
        ``[repos."owner/name"]`` section (run setup; ``--loop`` logs a skip).
    """
    # Resolve repo root. The main checkout, never the linked worktree the
    # operator happened to invoke from: cwd must agree with WT_ROOT or the
    # tick reads an empty marker space.
    try:
        os.chdir(repo_root())
    except (subprocess.CalledProcessError, ValueError):
        print("tick: not in a git repository", file=sys.stderr)
        tick_state = pipeline.derive_tick_state(status="FAILED")
        print(tick_state.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return 1

    try:
        parsed_target = _parse_target(target)
        parsed_only = _parse_only_list(only) if only else None
        if parsed_only is not None and parsed_target is not None:
            print(
                "tick: --only and a positional target are mutually exclusive",
                file=sys.stderr,
            )
            raise SystemExit(1)
    except SystemExit:
        tick_state = pipeline.derive_tick_state(status="FAILED")
        tick_log = None
        try:
            os.makedirs(TICK_LOG_DIR, exist_ok=True)
            tick_log = TickLog.open(TICK_LOG_DIR)
        except (OSError, FileExistsError):
            pass
        _log_tick_summary(tick_log, reconcile_ok=False, tick_state=tick_state)
        print(tick_state.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return 1

    tick_lock = TickLock(TICK_LOCK_PATH)
    if not tick_lock.acquire():
        _log_lock_held()
        cadence = pipeline.compute_next_wake(
            in_flight_count=None,
            has_actionable_queue=None,
            at_capacity=None,
            **_lock_held_cadence(),
        )
        print(cadence.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return LOCK_HELD_EXIT_CODE

    try:
        if loop:
            return _run_loop_locked(
                parsed_target, impl_engine, model,
                review_model, review_effort, permission_mode,
                fix_engine=fix_engine, fix_model=fix_model, only=parsed_only,
            )
        return _run_single_tick_locked(
            parsed_target, impl_engine, model, review_model, review_effort,
            permission_mode, fix_engine=fix_engine, fix_model=fix_model,
            only=parsed_only,
        ).exit_code
    finally:
        tick_lock.release()


def _lock_held_cadence() -> pipeline.CadenceSettings:
    """Return the cadence for an invocation the tick mutex turned away.

    No tick runs here, so no startup gate validated a configuration. The
    retry is still paced from the operator's cadence when the configuration
    resolves cleanly; otherwise the shipped cadence applies, and the next
    tick that takes the lock refuses the configuration and names the key.

    Returns:
        Cadence keyword arguments for :func:`pipeline.compute_next_wake`.
    """
    try:
        config = pipeline.resolve_config(resolve_target().repo)
    except (ValueError, OSError, subprocess.SubprocessError):
        return {}
    return {} if config.errors else config.cadence()


def _log_lock_held() -> None:
    """Emit audit signals when a concurrent tick is already running."""
    msg = "tick: another tick is already running; lock held"
    print(msg, file=sys.stderr)
    try:
        os.makedirs(TICK_LOG_DIR, exist_ok=True)
        tick_log = TickLog.open(TICK_LOG_DIR)
    except (OSError, FileExistsError):
        return
    tick_log.log(
        pr=None, phase="startup", stage="", action="lock_held", reason=msg,
    )
    sys.stderr.flush()


def _log_requested_scope(
    tick_log: TickLog | None,
    parsed_target: tuple[str, int] | None,
    only: Sequence[tuple[str, int]] | None,
) -> None:
    """Log the scope this tick was invoked with, for the status banner.

    This is the request, not its resolution: ``target_scope_resolved`` or
    ``only_scope_resolved``/``only_scope_unreadable`` later in the same log
    supersede it. A tick that fails before resolving still names its list or
    target, and only this record names the full queue.
    """
    if tick_log is None:
        return
    if only is not None:
        details: dict[str, object] = {
            "mode": "only", "only": [_only_token(item) for item in only],
        }
        reason = f"--only {_only_text(only)}"
    elif parsed_target is not None:
        details = {"mode": "target", "target": _only_token(parsed_target)}
        reason = f"target {_only_token(parsed_target)}"
    else:
        details = {"mode": "full"}
        reason = "full queue"
    tick_log.log(
        pr=None, phase="startup", stage="", action="tick_scope_requested",
        reason=reason, details=details,
    )


def _run_single_tick_locked(
    parsed_target: tuple[str, int] | None,
    impl_engine: str,
    model: str,
    review_model: str = "",
    review_effort: str = "",
    permission_mode: str = "",
    *,
    fix_engine: str = "",
    fix_model: str = "",
    count_quiescent_heartbeat: bool = True,
    unattended: bool = False,
    quiescent_stop_after: int | None = None,
    only: tuple[tuple[str, int], ...] | None = None,
) -> TickOutcome:
    """Run one tick while the caller owns the tick mutex.

    Every engine-route and permission argument is the typed CLI flag, and
    empty means none was typed. Resolution happens here rather than in
    ``main`` so a ``--loop`` run picks up a config change on its next tick —
    and only here: the configuration this startup gate validates is the one
    the whole tick runs on.

    Args:
        parsed_target: Parsed target tuple, or ``None`` for a full scan.
        impl_engine: ``--impl-engine`` flag.
        model: ``--model`` flag.
        review_model: ``--review-model`` flag.
        review_effort: ``--review-effort`` flag.
        permission_mode: ``--permission-mode`` flag for implementer and
            fixer engines (``"auto"`` or ``"dontAsk"``).
        fix_engine: ``--fix-engine`` flag.
        fix_model: ``--fix-model`` flag.
        count_quiescent_heartbeat: Forwarded to :func:`_run_tick_body`; only
            the in-process loop passes ``False``, for a nudge-initiated tick.
        unattended: True under the in-process loop, where nobody can answer a
            setup prompt; an unconfigured target is logged as skipped.
        quiescent_stop_after: Optional test override, applied to the same
            cadence decision the tick reports and the native loop consumes.
        only: Parsed ``--only`` list, or ``None``. Forwarded to the tick body,
            which expands it once per tick after HARVEST.

    Returns:
        Exit status and the cadence reported by the completed tick, if any.
    """
    global TRUSTED_HEAD_REPO, TARGET_REMOTE, TRUSTED_REVIEW_AUTHOR

    # Open tick log early (non-fatal) so startup diagnostics are captured.
    tick_log = None
    try:
        os.makedirs(TICK_LOG_DIR, exist_ok=True)
        tick_log = TickLog.open(TICK_LOG_DIR)
    except (OSError, FileExistsError):
        pass

    # Startup diagnostics: context and binary probe.
    # These run before any HARVEST/DISPATCH work so a tick that fails at the
    # OS level (E2BIG, broken PATH, missing binary) leaves an audit trail.
    if tick_log is not None:
        try:
            _log_startup_context(tick_log)
        except Exception:  # noqa: BLE001
            pass
    # Record what this tick was asked to do before any step that can fail, so
    # the status banner never reads an early failure as a full-queue tick.
    # The startup context stays the log's first entry.
    _log_requested_scope(tick_log, parsed_target, only)

    probe = _startup_probe()
    if tick_log is not None:
        try:
            _log_startup_probe(tick_log, probe)
        except Exception:  # noqa: BLE001
            pass

    # Fail fast on a configuration value that has no conservative reading —
    # base_ref, permission_mode, [operator] login, verify_tools, max_in_flight, [scan] repos,
    # the engine routes, and every deadline, stall limit, round limit, cooldown
    # and cadence value. Each of those names a branch, grants a permission,
    # decides whose work is trusted, picks an engine, or bounds paid engine time,
    # so a malformed one cannot be coerced into something the operator did not
    # write: the resolver reports it and the tick refuses here, naming the key,
    # before HARVEST. A malformed verify_tools in particular must never be read
    # as an empty one, which would dispatch with no verification and still award
    # ai:ready-for-human.
    TRUSTED_HEAD_REPO = TARGET_REMOTE = ""
    try:
        resolved_target = resolve_target()
    except (ValueError, OSError) as exc:
        msg = f"tick: target resolution failed: {exc}"
        print(msg, file=sys.stderr)
        if tick_log is not None:
            tick_log.log(pr=None, phase="startup", stage="",
                         action="startup_target_invalid", reason=msg)
        tick_state = pipeline.derive_tick_state(status="FAILED")
        _log_tick_summary(tick_log, reconcile_ok=False, tick_state=tick_state)
        print(tick_state.format_line(), file=sys.stderr)
        return TickOutcome(3)
    TRUSTED_HEAD_REPO = resolved_target.repo
    TARGET_REMOTE = resolved_target.remote
    typed_flags = {
        "permission_mode": permission_mode,
        "impl_engine": impl_engine,
        "model": model,
        "fix_engine": fix_engine,
        "fix_model": fix_model,
        "review_model": review_model,
        "review_effort": review_effort,
    }
    config = pipeline.resolve_config(
        TRUSTED_HEAD_REPO,
        cli_overrides={key: value for key, value in typed_flags.items() if value},
    )
    for warning in config.warnings:
        print(f"tick: config: {warning}", file=sys.stderr)
        if tick_log is not None:
            tick_log.log(
                pr=None, phase="startup", stage="",
                action="startup_config_warning",
                reason=warning,
            )
    config_errors = list(config.errors)
    if config.scan_repos is not None and not any(
        pipeline.same_repository(TRUSTED_HEAD_REPO, repo) for repo in config.scan_repos
    ):
        config_errors.append(f"target {TRUSTED_HEAD_REPO} is outside [scan] repos")
    if config_errors:
        msg = "tick: invalid configuration: " + "; ".join(config_errors)
        print(msg, file=sys.stderr)
        if tick_log is not None:
            tick_log.log(
                pr=None, phase="startup", stage="",
                action="startup_config_invalid",
                reason=msg,
            )
        tick_state = pipeline.derive_tick_state(status="FAILED")
        _log_tick_summary(tick_log, reconcile_ok=False, tick_state=tick_state)
        print(tick_state.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return TickOutcome(3)
    # From here on the resolved values are the ones the engines run under: the
    # flag if one was typed, else the operator's configured value. Every
    # tuning value is read from this one validated configuration, never
    # resolved again later in the tick.
    permission_mode = config.permission_mode

    # The identity this tick reviews under is the one just resolved and
    # validated, not the one this module resolved when it was imported. A
    # `--loop` process lives across config edits, and validating the file's
    # current login while admitting candidates against a stale one is the
    # worst of both: an operator who corrects or removes their login sees the
    # tick accept the change and keep authorizing under the old identity.
    trusted_author = config.login

    if not trusted_author:
        msg = (
            "tick: [operator] login not configured — run /pr-pipeline:setup, "
            f"set it in {pipeline.operator_config_path()}, or export "
            "PIPELINE_TRUSTED_AUTHOR"
        )
        print(msg, file=sys.stderr)
        if tick_log is not None:
            tick_log.log(
                pr=None, phase="startup", stage="",
                action="startup_target_unconfigured",
                reason=msg,
            )
        tick_state = pipeline.derive_tick_state(status="FAILED")
        _log_tick_summary(tick_log, reconcile_ok=False, tick_state=tick_state)
        print(tick_state.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return TickOutcome(3)

    # A target with no section of its own was never set up: nobody proposed its
    # verification grants or probed its engine route, and [defaults] answering
    # every key does not say otherwise. Checked before the label vocabulary,
    # because setup is also what provisions that. Nothing here reads input — an
    # interactive tick hands the prompt to the operator's session through the
    # exit code, and an unattended loop has nobody to ask.
    if not config.repo_configured:
        section = f'[repos."{TRUSTED_HEAD_REPO}"]'
        if unattended:
            msg = (
                f"tick: skipped {TRUSTED_HEAD_REPO}: no {section} section in "
                f"{pipeline.operator_config_path()}; an unattended loop cannot "
                "run setup — run /pr-pipeline:setup, then restart the loop"
            )
        else:
            msg = (
                f"tick: {TRUSTED_HEAD_REPO} is not set up: no {section} section "
                f"in {pipeline.operator_config_path()}. Run /pr-pipeline:setup "
                "for this repository, then rerun the tick"
            )
        print(msg, file=sys.stderr)
        if tick_log is not None:
            tick_log.log(
                pr=None, phase="startup", stage="",
                action="startup_repo_unconfigured", reason=msg,
            )
        tick_state = pipeline.derive_tick_state(status="FAILED")
        _log_tick_summary(tick_log, reconcile_ok=False, tick_state=tick_state)
        print(tick_state.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return TickOutcome(UNCONFIGURED_TARGET_EXIT_CODE)

    # Fail fast when a required binary is missing.
    missing = [name for name in ("gh", "git", "codex")
               if not probe.get(f"{name}_found")]
    if missing:
        msg = f"tick: required binaries not found on PATH: {', '.join(missing)}"
        print(msg, file=sys.stderr)
        if tick_log is not None:
            tick_log.log(
                pr=None, phase="startup", stage="",
                action="startup_probe_failed",
                reason=msg,
            )
        tick_state = pipeline.derive_tick_state(status="FAILED")
        _log_tick_summary(tick_log, reconcile_ok=False, tick_state=tick_state)
        print(tick_state.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return TickOutcome(3)

    # Harvest can relabel too. Verify before either phase, on every loop tick,
    # without creating or modifying any repository labels here.
    try:
        labels.verify(TRUSTED_HEAD_REPO)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        msg = f"tick: label vocabulary check failed for {TRUSTED_HEAD_REPO}: {exc}"
        print(msg, file=sys.stderr)
        if tick_log is not None:
            tick_log.log(pr=None, phase="startup", stage="",
                         action="startup_labels_invalid", reason=msg)
        tick_state = pipeline.derive_tick_state(status="FAILED")
        _log_tick_summary(tick_log, reconcile_ok=False, tick_state=tick_state)
        print(tick_state.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return TickOutcome(3)

    _report_unprobed_route(config, tick_log)

    TRUSTED_REVIEW_AUTHOR = trusted_author
    try:
        return _run_tick_body(
            tick_log, parsed_target, config.impl_engine, config.model,
            config.review_model, config.review_effort, permission_mode,
            trusted_author=trusted_author,
            count_quiescent_heartbeat=count_quiescent_heartbeat,
            config=config,
            quiescent_stop_after=quiescent_stop_after,
            only=only,
        )
    except Exception:
        exc_value = sys.exc_info()[1]
        tb_str = "".join(traceback.format_exc())
        if tick_log is not None:
            # Write a raw enriched crash entry so we can include traceback.
            crash_entry: dict[str, object] = {
                "ts": int(time.time()),
                "pr": None,
                "phase": "startup",
                "stage": "",
                "action": "tick_crash",
                "reason": "",
                "engine": "",
                "error_type": type(exc_value).__name__,
                "error_message": str(exc_value),
                "traceback": tb_str,
            }
            try:
                with open(tick_log.path, "a") as fh:
                    fh.write(json.dumps(crash_entry) + "\n")
            except OSError:
                pass
        print(f"Tick crashed: {type(exc_value).__name__}: {exc_value}", file=sys.stderr)
        tick_state = pipeline.derive_tick_state(status="FAILED")
        try:
            _log_tick_summary(tick_log, reconcile_ok=False, tick_state=tick_state)
        except Exception:  # noqa: BLE001
            pass
        print(tick_state.format_line(), file=sys.stderr)
        sys.stderr.flush()
        return TickOutcome(TICK_CRASH_EXIT_CODE)


def _report_unprobed_route(
    config: pipeline.ResolvedConfig, tick_log: TickLog | None,
) -> None:
    """Report an implementer route that differs from the one setup probed.

    Setup probes one route — whether ``auto`` permission mode approves an
    ungranted command on it — and records it in the repository's section. A
    config edit or flag that changes the route runs without that measurement.
    That is the operator's call to make, so the tick reports it and runs.

    Args:
        config: The configuration this tick runs on.
        tick_log: Tick log handle, or ``None``.
    """
    if config.probed_route is None:
        return
    probed_engine, probed_model = config.probed_route
    effective = (
        config.impl_engine,
        pipeline.effective_engine_model(config.impl_engine, config.model),
    )
    probed = (
        probed_engine,
        pipeline.effective_engine_model(probed_engine, probed_model),
    )
    if effective == probed:
        return
    msg = (
        f"tick: implementer route {_route_text(*effective)} differs from the "
        f"route /pr-pipeline:setup probed for {TRUSTED_HEAD_REPO} "
        f"({_route_text(*probed)}); permission mode "
        f"{config.permission_mode!r} was not measured on this route — rerun "
        "setup to probe it"
    )
    print(msg, file=sys.stderr)
    if tick_log is not None:
        tick_log.log(
            pr=None, phase="startup", stage="",
            action="startup_route_unprobed", reason=msg,
        )


def _route_text(engine: str, model: str) -> str:
    """Render an engine route for an operator message."""
    return f"{engine} with model {model or '(engine default)'}"


def _nearest_actionable_engine_deadline() -> float | None:
    """Return the nearest deadline that still requires cadence action.

    Scans structurally valid dispatch markers under ``WT_ROOT`` and returns
    ``min(deadline)`` across engines whose identity-authoritative
    classification is ``live`` or ``launching`` — the same proof harvest and
    admission require before trusting a marker.

    A marker contributes only while its deadline still requires action. A
    reused PID and both shapes of ``hung`` contribute no deadline because
    their deadline is a *fixed past timestamp*: pacing to it would clamp the
    cadence to the overdue floor for as long as the marker survives.

    * The tick escalated it to a human and stopped acting on it.
    * Termination failed, so ``_reconcile_one`` downgraded ``kill_escalate``
      to ``leave`` and will retry the kill next pass. Excluding this one still
      costs nothing: ``terminate_process_group`` returns ``False`` only when
      the signal could not be delivered at all (``EPERM``, which a sooner
      retry reproduces exactly) or when ``SIGKILL`` is already pending on a
      process wedged in uninterruptible sleep. Neither resolves faster by
      waking sooner, and once the group does die
      :func:`_engine_looks_finished` nudges the loop within
      ``WAKE_WATCH_POLL_SECONDS``.

    Every cadence surface must share this scan. The in-process loop and the
    one-shot ``tick-cadence:`` line are two executable paths to the same
    contract, and a second, weaker deadline scan is how they drift apart.

    Returns:
        The nearest deadline in epoch seconds, or ``None`` when no marker still
        requires deadline action or the scan fails.
    """
    nearest: float | None = None
    try:
        for entry in os.listdir(WT_ROOT):
            full = os.path.join(WT_ROOT, entry)
            if not os.path.isdir(full):
                continue
            if not (entry.startswith("pr-") or entry.startswith("issue-")):
                continue
            marker_path = os.path.join(full, "dispatch.json")
            if not os.path.isfile(marker_path):
                continue
            try:
                with open(marker_path) as fh:
                    marker = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(marker, dict):
                continue
            pr_number = marker.get("pr_number")
            if (
                not isinstance(pr_number, int)
                or isinstance(pr_number, bool)
                or not pipeline.valid_marker(marker, expected_pr=pr_number)
            ):
                continue
            deadline = marker.get("deadline")
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                continue
            classify_state, _identity, _pid_alive = _classify_marker(
                full, marker, result_file=None,
            )
            if classify_state not in ("live", "launching"):
                continue
            if nearest is None or deadline < nearest:
                nearest = float(deadline)
    except OSError:
        return None
    return nearest


def _compute_nearest_deadline_s() -> float | None:
    """Return seconds until the nearest actionable in-flight deadline.

    Returns:
        Seconds remaining, negative when the deadline just passed mid-scan, or
        ``None`` when no marker still requires deadline action.
    """
    nearest = _nearest_actionable_engine_deadline()
    return None if nearest is None else nearest - time.time()


def _quiescent_counter_path(target: tuple[str, int] | str | None) -> str:
    """Return the queue counter, a target's counter, or an ``--only`` counter.

    ``None`` is the full queue, a tuple is a positional target, and a string
    is the key :func:`_only_counter_scope` derives for a listed set.
    """
    if target is None:
        return _QUIESCENT_COUNTER_FILE
    if isinstance(target, str):
        return os.path.join(os.path.dirname(_QUIESCENT_COUNTER_FILE), f"{target}.txt")
    entity, number = target
    return os.path.join(
        os.path.dirname(_QUIESCENT_COUNTER_FILE), f"{entity}-{number}.txt",
    )


def _read_quiescent_counter(target: tuple[str, int] | str | None = None) -> int:
    """Read the consecutive quiescent ticks counter from disk.

    Returns 0 when the counter file does not exist or is unparseable.
    """
    try:
        with open(_quiescent_counter_path(target)) as fh:
            return max(0, int(fh.read().strip()))
    except (OSError, ValueError):
        return 0


def _write_quiescent_counter(
    value: int, target: tuple[str, int] | str | None = None,
) -> None:
    """Atomically write the consecutive quiescent ticks counter to disk."""
    path = _quiescent_counter_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pipeline.atomic_write(path, str(value))


def _advance_quiescent_counter(
    *, is_quiescent: bool, count_heartbeat: bool,
    target: tuple[str, int] | str | None = None,
) -> int:
    """Update the persisted quiescent streak and return prior heartbeats.

    The counter counts qualifying quiet observations, including the first
    tick of an invocation and every one-shot tick. Later native-loop ticks
    count only after a full scheduled wait. Nudge-initiated ticks hold the
    streak steady so that nudges cannot accelerate the stop. The counter does
    not prove elapsed time, and both modes reuse the scope's retained streak.

    Args:
        target: Parsed CLI target, an ``--only`` counter key, or ``None`` for
            the queue. Only this scope is read and reset.
        is_quiescent: Whether this tick observed a quiescent pipeline. A
            non-quiescent tick resets the streak whatever woke it.
        count_heartbeat: True for the invocation's first tick, one-shot ticks,
            or a native-loop tick after a full scheduled wait.

    Returns:
        The number of qualifying quiet observations before this tick. Pass
        ``count_heartbeat`` with it to :func:`pipeline.compute_next_wake` so a
        nudge does not invent a counted observation when the streak is zero.
    """
    counter = _read_quiescent_counter(target)
    prior = counter if is_quiescent else 0
    if not is_quiescent:
        counter = 0
    elif count_heartbeat:
        counter += 1
    _write_quiescent_counter(counter, target)
    return prior


def _compute_post_dispatch_state(
    target: tuple[str, int] | None = None,
    *,
    target_state: pipeline.TargetCadenceState | None = None,
    only_state: pipeline.TargetCadenceState | None = None,
) -> tuple[int | None, bool | None, tuple[str, ...]]:
    """Compute in-flight count and queue state after dispatch.

    The in-flight count is the engine-occupied local worktree markers — the
    same contract as ``can_dispatch_now()``, so the emitted tick-state line
    and the admission decision can never disagree about what is in flight.
    If the marker scan fails, in-flight is unknown (``None``).

    Args:
        target: The tick's parsed CLI target, or ``None`` for a full scan.
            When set, the queue probe is restricted to that target's scope so
            the emitted tick-state line describes the same work the tick was
            allowed to dispatch.
        target_state: Reuse the tick's target observation for both the queue
            and terminal decision. Direct callers can omit it.
        only_state: The tick's ``--only`` observation. When set, its tri-state
            ``actionable`` is the queue value; occupancy stays global, so a
            list run cannot report quiet while this machine owns an engine.

    Returns:
        ``(supervisable_in_flight, has_actionable_queue, held_items)`` —
        ``None`` for a value that could not be determined.
    """
    try:
        marker_states = _scan_marker_identities()
        occupied = pipeline.in_flight_refs(_admission_states(marker_states))
        in_flight_count, held_items = _supervisable_occupancy(
            occupied, read_authoritative_human_holds(occupied),
            frozenset(_pipeline_actionable(marker_states)),
        )
    except (RuntimeError, ValueError, subprocess.CalledProcessError):
        in_flight_count = None
        held_items = ()

    if only_state is not None:
        return in_flight_count, only_state.actionable, held_items

    if target is not None:
        state = target_state or _observe_target_state(target)
        return in_flight_count, state.actionable, held_items

    has_queue: bool | None = False
    any_failed = False
    for entity, label in _DISPATCH_ELIGIBLE_QUEUES:
        try:
            if _queue_page_has_dispatchable(entity, label):
                has_queue = True
                break
        except (subprocess.CalledProcessError, ValueError):
            any_failed = True
            continue

    if any_failed and not has_queue:
        has_queue = None

    return in_flight_count, has_queue, held_items


def _run_loop_locked(
    parsed_target: tuple[str, int] | None,
    impl_engine: str,
    model: str,
    review_model: str = "",
    review_effort: str = "",
    permission_mode: str = "",
    *,
    fix_engine: str = "",
    fix_model: str = "",
    quiescent_stop_after: int | None = None,
    only: tuple[tuple[str, int], ...] | None = None,
) -> int:
    """Run until interrupted, the target is terminal, or the scope is quiescent.

    The flags are forwarded unresolved (empty means none was typed): each
    tick resolves the operator's config for itself, so a config edit mid-loop
    takes effect on the next tick rather than at loop start. The cadence after
    a tick is paced from the configuration that tick validated.

    ``quiescent_stop_after`` pins the stop count for tests; ``None`` uses the
    tick's validated ``loop_quiescent_stop_after``. ``only`` is the parsed
    ``--only`` list; each tick expands it again, and its stop is reported as
    list exhaustion rather than full-queue quiescence.
    """
    stop_event = threading.Event()
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)

    wake_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        # Set both: the inter-tick wait blocks on wake_event, so signalling
        # stop alone would leave SIGINT waiting out the full cadence interval.
        stop_event.set()
        wake_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    # After startup, only full scheduled waits advance the quiet streak.
    # There is no second in-memory streak or post-summary state observation.
    after_heartbeat = True
    last_rc = 0
    watcher = _start_wake_watcher(stop_event, wake_event)

    try:
        while not stop_event.is_set():
            # Clear before the tick so a nudge arriving during it is preserved
            # for the next wait instead of being consumed by this iteration.
            wake_event.clear()
            outcome = _run_single_tick_locked(
                parsed_target, impl_engine, model,
                review_model, review_effort, permission_mode,
                fix_engine=fix_engine, fix_model=fix_model,
                count_quiescent_heartbeat=after_heartbeat,
                unattended=True,
                quiescent_stop_after=quiescent_stop_after,
                only=only,
            )
            last_rc = outcome.exit_code
            if last_rc in (
                3, TICK_CRASH_EXIT_CODE, UNCONFIGURED_TARGET_EXIT_CODE,
            ):
                # Startup refused this target/configuration, skipped a target
                # setup never ran for, or the tick crashed. Cadence reads
                # GitHub too, so observing here would bypass that refusal; a
                # retried crash would hide behind a background task that is
                # only reported when it exits.
                return last_rc
            # The tick owns both observation and cadence. Re-reading here
            # could contradict the stop/retry decision already emitted.
            decision = outcome.cadence
            if decision is None:
                # A tick without a complete summary cannot authorize a wait
                # or a successful stop. Startup/crash gates above return first.
                return last_rc or TICK_CRASH_EXIT_CODE
            if decision.stop:
                if parsed_target is not None:
                    _notify_loop_target_stop(decision.reason)
                elif only is not None:
                    _notify_loop_only_stop(decision.reason)
                else:
                    _notify_loop_quiescent_stop(outcome.held_items)
                return last_rc
            stopped, timer_expired = _wait_next_tick(
                decision.next_wake_seconds, stop_event, wake_event,
            )
            if stopped:
                return last_rc
            # After the first tick, only a full scheduled wait qualifies
            # the next quiet observation. Nudges must not advance the streak.
            after_heartbeat = timer_expired
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        stop_event.set()
        if watcher is not None:
            watcher.join(timeout=WAKE_WATCH_POLL_SECONDS)

    return last_rc


def _start_wake_watcher(
    stop_event: threading.Event,
    wake_event: threading.Event,
) -> threading.Thread | None:
    """Start the advisory wake watcher for in-process loop mode.

    Args:
        stop_event: Set when the loop is shutting down; ends the watcher.
        wake_event: Set by the watcher to request an early tick.

    Returns:
        The started daemon thread, or ``None`` when it could not be started
        (the loop then runs on its unmodified timer schedule).
    """
    # Create the wake-request directory up front so the documented operator
    # `touch` works from the first loop iteration, not only after a tick has
    # written its quiescent counter into the same directory.
    try:
        os.makedirs(os.path.dirname(WAKE_REQUEST_PATH), exist_ok=True)
    except OSError:
        pass
    thread = threading.Thread(
        target=_wake_watcher_loop,
        args=(stop_event, wake_event),
        name="tick-wake-watcher",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError as exc:
        _log_wake_watcher_failed(f"could not start wake watcher: {exc}")
        return None
    return thread


def _wake_watcher_loop(
    stop_event: threading.Event,
    wake_event: threading.Event,
) -> None:
    """Poll for wake evidence until the loop stops.

    The watcher is advisory: it holds no custody, reads no results, and makes
    no state transition. A false wake costs one extra level-triggered tick,
    and a missed wake costs only latency because the cadence timer still
    fires. Any failure ends the watcher and leaves the loop on its timer
    schedule (fail-open).

    Args:
        stop_event: Set when the loop is shutting down; ends the watcher.
        wake_event: Set when an early tick is warranted.
    """
    seen: set[str] = set()
    stall_failures: set[str] = set()
    try:
        while not stop_event.wait(WAKE_WATCH_POLL_SECONDS):
            # Every source runs every poll — no short-circuit. The stall scan
            # carries the operator-facing side effect, so letting an unrelated
            # nudge skip it would silently drop the stall report for that poll.
            # It is also the only source wrapped: it is the newest, it reads
            # arbitrary marker input through `_classify_marker`, and the two
            # sources beside it predate it and must not die with it.
            nudges = (
                _consume_wake_request(),
                _scan_finished_engines(seen),
                _scan_stalled_engines_safely(stall_failures),
            )
            if any(nudges):
                wake_event.set()
    except Exception as exc:  # noqa: BLE001 - fail open to the cadence timer
        _log_wake_watcher_failed(f"wake watcher stopped: {type(exc).__name__}: {exc}")


def _consume_wake_request() -> bool:
    """Consume the external wake-request file if present.

    Unlinking before waking is what makes repeated touches coalesce: a touch
    landing during a tick recreates the file and nudges the following wait.

    Returns:
        ``True`` when a wake request was present and consumed.
    """
    try:
        os.unlink(WAKE_REQUEST_PATH)
    except OSError:
        return False
    return True


def _scan_finished_engines(seen: set[str]) -> bool:
    """Report whether any dispatched engine newly looks finished.

    Evidence is deliberately shallow — see :func:`_engine_looks_finished`.
    Harvest remains the sole completion authority; this only decides whether
    to look sooner. Each dispatch identity wakes the loop at most once so an
    unharvestable marker cannot hold the loop in a hot cycle.

    Args:
        seen: Dispatch identities already used to wake the loop. Mutated.

    Returns:
        ``True`` when at least one not-yet-seen engine looks finished.
    """
    _reap_engine_children()
    nudge = False
    for wt_dir, marker in _iter_dispatch_markers():
        dispatch_id = marker.get("dispatch_id")
        if not isinstance(dispatch_id, str) or dispatch_id in seen:
            continue
        if not _engine_looks_finished(wt_dir, marker):
            continue
        seen.add(dispatch_id)
        nudge = True
    return nudge


def _track_engine_child(proc: subprocess.Popen[bytes]) -> None:
    """Retain an owned child for nonblocking reaping between loop ticks."""
    with _ENGINE_CHILDREN_LOCK:
        _ENGINE_CHILDREN.append(proc)


def _reap_engine_children() -> None:
    """Collect exited direct children without trusting PIDs from marker files.

    Popen.poll uses a nonblocking wait and preserves the child's return code.
    It neither waits on unrelated children nor proves that an engine's process
    group is dead. The wake scan and harvest still use their existing checks.
    The lock serializes dispatch registration and concurrent watcher/tick polls.
    """
    with _ENGINE_CHILDREN_LOCK:
        _ENGINE_CHILDREN[:] = [
            proc for proc in _ENGINE_CHILDREN if proc.poll() is None
        ]


def _iter_dispatch_markers(
    wt_root: str | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield every readable dispatch marker under the worktree root.

    What the scans share is this *definition* — which directories hold markers
    and what an unreadable one means — not a single traversal: each caller
    walks independently, which at two listdirs per 5s poll costs nothing worth
    coordinating. A corrupt or vanished marker is skipped, not raised: custody
    of an unclassifiable marker belongs to dispatch/harvest, and the advisory
    watcher must neither act on it nor die because of it.

    Args:
        wt_root: Worktree root to scan. Defaults to ``WT_ROOT``; the
            ``engine-progress`` subcommand passes an explicit root because it
            runs as its own process with no guarantee about its cwd.

    Yields:
        ``(worktree_dir, parsed_marker)`` for each readable ``dispatch.json``.
    """
    root = WT_ROOT if wt_root is None else wt_root
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not (name.startswith("pr-") or name.startswith("issue-")):
            continue
        wt_dir = os.path.join(root, name)
        try:
            with open(os.path.join(wt_dir, "dispatch.json")) as fh:
                marker = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(marker, dict):
            yield wt_dir, marker


def _engine_looks_finished(wt_dir: str, marker: dict[str, Any]) -> bool:
    """Report shallow evidence that a dispatched engine has finished.

    Two kinds of evidence, mirroring how each engine type actually signals
    completion (see ``_classify_marker``). The artifact check is gated on
    :data:`_WRAPPER_ENVELOPE_STAGES` for the same reason the classifier's
    envelope read is: a wrapperless engine can write either file itself, so for
    those stages the artifacts are untrusted input and only liveness decides:

    * The reviewer's wrapper publishes ``completion.json`` and ``review.json``
      by atomic rename after the engine exits, so either one appearing is
      terminal evidence.
    * Implementers and fixers publish no completion envelope. Their
      ``result.json`` is the engine's stdout redirect, created empty at spawn
      and written in place by the stream wrapper — no atomic rename — so a
      non-empty read can be a partial write and never proves exit. Process
      death is the evidence instead, which is also what harvest requires
      before treating their result as complete.

    Args:
        wt_dir: The engine's worktree directory.
        marker: Parsed ``dispatch.json`` contents.

    Returns:
        ``True`` when wrapper-published completion artifacts have landed or
        both the recorded positive PID and validated positive process group
        are no longer alive. A ``launching`` marker with no PID published yet
        is never treated as finished, and a non-positive PID is never probed —
        ``os.kill`` would target a process *group* rather than the engine, so
        ``_classify_marker`` refuses it too.
    """
    if marker.get("stage") in _WRAPPER_ENVELOPE_STAGES:
        for artifact in ("completion.json", "review.json"):
            try:
                if os.path.getsize(os.path.join(wt_dir, artifact)) > 0:
                    return True
            except OSError:
                continue
    pid = marker.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if process_control.pid_is_alive(pid):
        return False
    if _marker_process_group_is_alive(marker):
        return False
    return True


class _EngineProgress(NamedTuple):
    """The newest proof that one engine was still producing output."""

    at: float
    """Epoch seconds of the last observed progress."""

    source: str
    """Where ``at`` came from: ``"heartbeat"`` or ``"start_time"``."""

    threshold: float
    """Seconds of silence past ``at`` that classify this marker as stalled."""


def _read_stall_episodes() -> dict[str, float]:
    """Read the persisted stall-episode memory.

    Returns:
        Dispatch identity to the progress timestamp already evaluated for it.
        An unreadable, malformed, or absent file yields an empty map: the
        memory is a de-duplicator for an advisory signal, so failing open
        costs at most one repeated report and never suppresses a real stall.
    """
    try:
        with open(_STALL_EPISODES_FILE) as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # JSON object keys are always strings; only the values need checking.
    return {
        key: float(value)
        for key, value in cast(dict[str, Any], raw).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _write_stall_episodes(episodes: Mapping[str, float]) -> None:
    """Persist the stall-episode memory, best effort.

    A write failure is swallowed for the same reason a read failure is: the
    only cost is a repeated advisory report.

    Args:
        episodes: Dispatch identity to evaluated progress timestamp.
    """
    try:
        os.makedirs(os.path.dirname(_STALL_EPISODES_FILE), exist_ok=True)
        pipeline.atomic_write(_STALL_EPISODES_FILE, json.dumps(episodes))
    except OSError:
        pass


def _scan_stalled_engines_safely(reported: set[str]) -> bool:
    """Run the stall scan without letting it take its caller down.

    The scan reaches :func:`_classify_marker` on arbitrary marker input, which
    the rest of this file already treats as capable of raising (see
    :func:`_surface_retained_scratch_dirs`). Both of its callers predate it and
    have their own reasons to survive: in the watcher thread a raise would end
    completion nudges and wake-request consumption for the life of the loop
    process, and at tick start it would turn one hostile marker into a
    ``tick_crash``.

    This is also where the scan is serialized, because it is what production
    calls from both places. The episode memory is a read-modify-write on a
    shared file, so two concurrent callers would both read the same map, both
    pass the already-evaluated gate, and both report. A caller that cannot take
    the lock skips the pass rather than waiting: the watcher must never park
    mid-poll, and the caller holding the lock is running the identical scan, so
    there is nothing to wait for. A lock file that cannot be opened at all
    leaves the scan running unserialized — a possible duplicate report is a
    better failure than a detector that goes silent.

    Args:
        reported: Exception class names this caller has already logged, so a
            marker that raises on every 5s poll produces one entry rather than
            720 an hour, while a *different* failure still surfaces. Mutated.
            The tick passes a fresh set, which makes it one entry per tick.

    Returns:
        ``True`` when at least one new stall episode was reported; ``False``
        on any failure, and when another caller already has the scan in hand.
    """
    lock = TickLock(_STALL_EPISODES_LOCK)
    try:
        if not lock.acquire():
            return False
        held = True
    except OSError:
        held = False
    try:
        return _scan_stalled_engines()
    except Exception as exc:  # noqa: BLE001 - advisory signal, never fatal
        kind = type(exc).__name__
        if kind not in reported:
            reported.add(kind)
            _log_stall_scan_failed(f"engine stall scan failed: {kind}: {exc}")
        return False
    finally:
        if held:
            lock.release()


def _scan_stalled_engines() -> bool:
    """Report engines that are alive but have stopped making progress.

    Detection only. Nothing here kills, relabels, requeues, or shortens a
    deadline: a stall is reported to the operator and wakes the loop, and the
    tick that follows reaches exactly the conclusions it would have reached on
    its normal schedule. Acting on a stall changes spend and custody semantics
    and is deliberately not part of this signal.

    Under uninterrupted operation each stall episode is reported once. The
    persisted map remembers the progress timestamp already evaluated for a
    refused verdict or whose report attempt reached the post-report checkpoint,
    so a heartbeat that starts advancing again re-arms detection and a later
    silence at a *newer* timestamp reports as a fresh episode. Refused verdicts
    are durable too, so the expensive identity probe runs once per timestamp
    rather than once per poll. Report attempts use at-least-once retry semantics
    across process death: an attempt interrupted before its checkpoint commits
    is retried rather than durably suppressed.

    The memory is on disk, not in-process, because the two executable loop
    paths are two different process models: ``--loop`` polls this from one
    long-lived watcher thread, while the operator's prescribed mode runs each
    tick as its own process. In-process memory would report every wedge once
    per tick there. Entries for dispatches that no longer exist are dropped
    each scan, so the file stays bounded by in-flight work rather than by
    uptime, and it needs no separate pruning or staleness rules.

    Under ``--loop`` both the watcher and the tick body reach this, so the
    read-modify-write is serialized by the caller
    (:func:`_scan_stalled_engines_safely`) rather than left to interleave —
    two callers reading the same map would both pass the already-evaluated
    gate and both report, which is the exact duplicate this memory exists to
    prevent.

    Returns:
        ``True`` when at least one new stall episode was reported.
    """
    nudge = False
    episodes = _read_stall_episodes()
    before = dict(episodes)
    live_ids: set[str] = set()
    for wt_dir, marker in _iter_dispatch_markers():
        dispatch_id = marker.get("dispatch_id")
        if not isinstance(dispatch_id, str) or not dispatch_id:
            continue
        live_ids.add(dispatch_id)
        progress = _engine_progress(wt_dir, marker)
        if progress is None:
            continue
        age = time.time() - progress.at
        if age <= progress.threshold:
            # Forward progress since the last look: re-arm for a later stall.
            episodes.pop(dispatch_id, None)
            continue
        if episodes.get(dispatch_id) == progress.at:
            continue
        # Only now — after the cheap mtime test says something is wrong, and
        # after the episode check skips a timestamp already evaluated — pay
        # for identity-verified liveness, which shells out to `ps`.
        identity = _stalled_engine_identity(wt_dir, marker)
        # Remember the timestamp for a refused verdict immediately, so it
        # costs one probe rather than one per 5s poll for as long as the marker
        # exists — and `retained_scratch` is a directory the pipeline keeps
        # across ticks. Every refuting classification reachable here is
        # terminal for a fixed progress timestamp. `stale`, `hung`,
        # `complete`, and `failed` never become `live` again. `launching` is
        # the one that looks reversible and is not: a pre-spawn lease carries
        # no `start_time` and so reaches neither clock, while the pure-lease
        # fallback written when an engine survives termination does leave a
        # beating heartbeat behind — but that marker is never rewritten to
        # `live`, because reconciliation fails closed to a human on a lease
        # rather than re-dispatching it. Fresh progress still re-arms via the
        # branch above, and a directory the human then clears drops out via
        # the `live_ids` sweep.
        episodes[dispatch_id] = progress.at
        if identity is None:
            if before.get(dispatch_id) != progress.at:
                _write_stall_episodes(episodes)
            continue
        _report_engine_stalled(
            marker=marker, age=age, source=progress.source, identity=identity,
        )
        if before.get(dispatch_id) != progress.at:
            # A live episode becomes durable only at this post-report
            # checkpoint. Process death before the write commits must leave it
            # retryable; once it commits, later marker failures cannot replay
            # the attempt on the next poll.
            _write_stall_episodes(episodes)
        nudge = True
    # Only a marker the scan still sees can re-arm its own episode, so a
    # harvested dispatch would keep its entry forever. Dropping what is no
    # longer dispatched bounds the file by in-flight work rather than by
    # uptime, which is what lets it exist without its own pruning rules.
    for gone in episodes.keys() - live_ids:
        del episodes[gone]
    if episodes != before:
        _write_stall_episodes(episodes)
    return nudge


def _engine_progress(
    wt_dir: str,
    marker: Mapping[str, object],
) -> _EngineProgress | None:
    """Return the newest progress evidence for one dispatched engine.

    Only implementers and fixers are considered. Their stream wrapper owns the
    engine's stdout and bumps a heartbeat file as output events arrive, which
    is what makes progress observable at all. The reviewer wrapper discards
    engine output, so it has no equivalent signal — and its stall cost is
    already bounded by the review deadline, so inventing a weaker
    proxy for it is deliberately deferred rather than guessed at.

    Args:
        wt_dir: The engine's worktree directory.
        marker: Parsed ``dispatch.json`` contents.

    Returns:
        The progress timestamp, its source, and the silence threshold that
        applies to it — or ``None`` when this marker has no progress contract
        (wrong stage) or no usable clock at all.
    """
    if marker.get("stage") not in ("implement", "fix"):
        return None
    # The limits this engine was dispatched under. A marker written before
    # they were recorded was dispatched under the shipped defaults.
    stall = _marker_seconds(marker, "stall_seconds", ENGINE_STALL_SECONDS)
    grace = _marker_seconds(
        marker, "stall_startup_grace_seconds", ENGINE_STALL_STARTUP_GRACE_SECONDS,
    )
    try:
        beat_at = os.path.getmtime(_heartbeat_path(wt_dir))
    except OSError:
        beat_at = None
    if beat_at is not None:
        return _EngineProgress(beat_at, "heartbeat", stall)
    # No heartbeat yet: the engine may simply not have produced its first
    # output event, or the marker predates heartbeats entirely. Fall back to
    # the dispatch clock with a startup grace so neither reads as a stall.
    start_time = marker.get("start_time")
    if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
        return None
    return _EngineProgress(float(start_time), "start_time", stall + grace)


def _marker_seconds(marker: Mapping[str, object], key: str, default: int) -> int:
    """Return a non-negative integer marker field, or ``default`` without one.

    Args:
        marker: Parsed ``dispatch.json`` contents.
        key: The field to read.
        default: The value for a marker that does not carry a usable field.

    Returns:
        The recorded value, or ``default``.
    """
    value = marker.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _stalled_engine_identity(
    wt_dir: str,
    marker: dict[str, Any],
) -> str | None:
    """Return the marker's identity if a quiet engine counts as stalled.

    The second half of the stall rule, shared by the watcher and the status
    dashboard's report so the two cannot disagree about which quiet engines
    are alarming. A stalled engine is by definition one harvest would leave
    running: a marker whose PID was reused, or whose deadline already lapsed
    (``hung``, which escalates on its own), is somebody else's business and
    reporting it as a stall would be a false alarm.

    Callers apply the silence threshold first — this is the expensive half,
    shelling out to ``ps``.

    Args:
        wt_dir: The engine's worktree directory.
        marker: Parsed ``dispatch.json`` contents.

    Returns:
        The identity-verification result when the engine classifies ``live``,
        otherwise ``None``. ``result.json`` is the right result file for both
        stages that have a progress contract; the reviewer never reaches here.
    """
    classify_state, identity, _alive = _classify_marker(
        wt_dir, marker, result_file="result.json",
    )
    return identity if classify_state == "live" else None


def engine_progress_report(wt_root: str) -> list[tuple[str, str]]:
    """Render the progress verdict for every dispatch marker under a root.

    The ``pr-pipeline:status`` dashboard shells out to this rather than
    re-deriving a verdict in bash. That is the whole point: a stall is
    *quiet past the threshold* **and** identity-verified live, and bash can
    prove neither the identity nor the thresholds without duplicating them in
    a second language. A dashboard that says ``STALLED`` where the tick log
    stays silent — a reused PID, an already-overdue engine — teaches the
    operator to distrust both surfaces.

    Args:
        wt_root: Worktree root to scan.

    Returns:
        ``(worktree_name, label)`` per marker, sorted by name. The label is
        ``STALLED-Nm`` for a proven-live engine past its silence threshold,
        ``quiet-Nm`` for any other readable progress clock, and ``-`` where
        no progress contract exists (the reviewer stage, or a marker with no
        usable clock).
    """
    rows: list[tuple[str, str]] = []
    for wt_dir, marker in _iter_dispatch_markers(wt_root):
        name = os.path.basename(os.path.normpath(wt_dir))
        progress = _engine_progress(wt_dir, marker)
        if progress is None:
            rows.append((name, "-"))
            continue
        age = time.time() - progress.at
        minutes = int(age // 60)
        # Same ordering as the watcher: the cheap clock first, the `ps` probe
        # only for a marker that already looks stalled.
        stalled = (
            age > progress.threshold
            and _stalled_engine_identity(wt_dir, marker) is not None
        )
        rows.append((name, f"STALLED-{minutes}m" if stalled else f"quiet-{minutes}m"))
    return sorted(rows)


def _cmd_engine_progress(argv: list[str]) -> int:
    """Print one ``name<TAB>label`` line per dispatch marker.

    Args:
        argv: Arguments after the subcommand name; ``--wt-root PATH`` is
            required because this runs as its own process, launched from
            whatever directory the operator happened to be in.

    Returns:
        Process exit code: 0 on success, 2 when ``--wt-root`` is missing.
    """
    if len(argv) != 2 or argv[0] != "--wt-root":
        print(
            "usage: tick.py engine-progress --wt-root PATH", file=sys.stderr,
        )
        return 2
    for name, label in engine_progress_report(argv[1]):
        print(f"{name}\t{label}")
    return 0


def _report_engine_stalled(
    *,
    marker: Mapping[str, object],
    age: float,
    source: str,
    identity: str,
) -> None:
    """Surface one engine-stall episode to the tick log and the operator.

    Args:
        marker: Parsed ``dispatch.json`` contents of the stalled engine.
        age: Seconds since the engine's last observed progress.
        source: Which clock produced that age (``"heartbeat"`` or
            ``"start_time"``).
        identity: The marker's identity-verification result, so an operator
            reading the entry can tell a proven-live engine from one whose
            liveness is merely unrefuted.
    """
    raw_number = marker.get("pr_number")
    number = (
        raw_number
        if isinstance(raw_number, int) and not isinstance(raw_number, bool)
        else None
    )
    stage = str(marker.get("stage", ""))
    deadline_in = _marker_seconds_to_deadline(marker)
    remaining = "unknown" if deadline_in is None else f"{int(deadline_in // 60)}m"
    target = f"#{number}" if number is not None else "an unnumbered marker"
    reason = (
        f"{stage} engine for {target} is alive but has produced no output for "
        f"{int(age // 60)}m ({source} clock); {remaining} to deadline"
    )
    print(f"tick: {reason}", file=sys.stderr)
    tick_log = _current_tick_log()
    if tick_log is not None:
        tick_log.log(
            pr=number,
            phase="loop",
            stage=stage,
            action="engine_stalled",
            reason=reason,
            engine=str(marker.get("engine", "")),
            dispatch_id=str(marker.get("dispatch_id", "")),
            details={
                "progress_age_seconds": int(age),
                "progress_source": source,
                "seconds_to_deadline": (
                    None if deadline_in is None else int(deadline_in)
                ),
                "identity": identity,
            },
        )
    _emit_operator_notification(
        title="PR pipeline engine stalled",
        message=reason,
    )


def _marker_seconds_to_deadline(marker: Mapping[str, object]) -> float | None:
    """Return seconds until a marker's deadline, or ``None`` if unusable."""
    deadline = marker.get("deadline")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return None
    if not math.isfinite(float(deadline)):
        return None
    return float(deadline) - time.time()


def _current_tick_log() -> "TickLog | None":
    """Return a handle on the newest tick log, without competing with a tick.

    Watcher-thread entries append to the newest existing log rather than
    opening one. During a tick that is the log the tick owns; between ticks the
    entry follows the prior tick's ``tick_complete``. A fresh file would sort
    newest and become what ``_log_tick_summary`` aggregates via ``tail()`` if a
    tick were in-flight. A log is only created when none exists yet, which is
    the window before the loop's first tick.

    Returns:
        The tick log to append to, or ``None`` when the log directory is
        unusable — logging is never allowed to break the watcher.
    """
    try:
        os.makedirs(TICK_LOG_DIR, exist_ok=True)
        existing = pipeline.latest_tick_log_path(TICK_LOG_DIR)
        return TickLog(existing) if existing else TickLog.open(TICK_LOG_DIR)
    except (OSError, FileExistsError):
        return None


def _log_wake_watcher_failed(reason: str) -> None:
    """Record that the advisory wake watcher is no longer running.

    Args:
        reason: Human-readable explanation for the tick log and stderr.
    """
    print(f"tick: {reason}", file=sys.stderr)
    tick_log = _current_tick_log()
    if tick_log is None:
        return
    tick_log.log(
        pr=None, phase="loop", stage="", action="wake_watcher_failed",
        reason=reason,
    )


def _log_stall_scan_failed(reason: str) -> None:
    """Record that the stall scan failed while its caller carried on.

    Distinct from ``wake_watcher_failed`` on purpose: that action means the
    watcher is gone and the loop is back on its bare timer, while this one
    means every other wake source and the tick itself are still running.

    Args:
        reason: Human-readable explanation for the tick log and stderr.
    """
    print(f"tick: {reason}", file=sys.stderr)
    tick_log = _current_tick_log()
    if tick_log is None:
        return
    tick_log.log(
        pr=None, phase="loop", stage="", action="stall_scan_failed",
        reason=reason,
    )


def _observe_loop_state(
    target: tuple[str, int] | None = None,
    *,
    max_in_flight: int = pipeline.DEFAULT_MAX_IN_FLIGHT,
) -> LoopState:
    """Read pipeline state for direct callers of ``decide_loop_cadence``.

    Native loops consume ``TickOutcome.cadence`` instead of this extra read.

    Args:
        target: The loop's parsed CLI target, or ``None`` for a full scan.
            When set, the actionable-queue probe is restricted to that
            target's own work. Terminal target state is recorded separately
            from global occupancy so unrelated engines cannot postpone its stop.
        max_in_flight: The admission cap to use for this observation.

    Returns:
        The observed loop state consumed by ``decide_loop_cadence``.
    """
    target_state = _observe_target_state(target) if target is not None else None
    reads_ok = True
    try:
        # Not occupancy — that is the marker scan below, and this read's value
        # is deliberately unused. It stays as a health probe on a surface the
        # queue check cannot see: this goes through GitHub's *search* index,
        # while both queue probes go through the list API, so a search index
        # that has stopped answering degrades nothing they would notice.
        #
        # That is the only thing this gate buys, and the bound is worth being
        # exact about, because it is narrower than it looks: both queue probes
        # already fail warm on their own errors — `_has_actionable_queue`
        # returns True from its own except clause, and
        # the target observation collapses an unreadable scope to True
        # via `is not False` — so neither can reach a quiescence claim on a
        # read it failed. What this adds is the refusal to claim quiescence
        # while *any* authoritative read is failing, not only the two that
        # feed the decision.
        read_authoritative_in_flight_labels()
    except (subprocess.CalledProcessError, ValueError):
        actionable_queue = True
        reads_ok = False
    else:
        actionable_queue = (
            _has_actionable_queue() if target is None
            else target_state.actionable is not False
        )
    # This scan is the same one can_dispatch_now performs, so cadence derives
    # its own occupancy rather than paying for a second identity-verified pass:
    # every failure that would make admission report "admission_error" — an
    # unreadable marker root, a corrupt marker, a malformed in-flight ref — is
    # raised here too, and lands on the identical fail-closed state below.
    # Occupancy must stay the admission contract exactly: counting in-flight
    # labels this machine does not own would also mean a stale manual pickup
    # keeps the loop permanently non-quiescent, spinning on work it can never
    # dispatch. Eligibility is judged per item at dispatch, off its own
    # authoritative label read — not here.
    try:
        marker_states = _scan_marker_identities()
        occupied = pipeline.in_flight_refs(_admission_states(marker_states))
        in_flight_count, held_items = _supervisable_occupancy(
            occupied, read_authoritative_human_holds(occupied),
            frozenset(_pipeline_actionable(marker_states)),
        )
    except (RuntimeError, ValueError, subprocess.CalledProcessError):
        # Unknown marker or hold state is not proof that nothing is held: stay
        # warm, and never claim quiescence or free a slot from it.
        in_flight_count = max_in_flight
        held_items = ()
        actionable_queue = True
        reads_ok = False

    if target is not None and not reads_ok:
        target_state = pipeline.TargetCadenceState(
            None, False, f"target {target[0]} #{target[1]}: control-plane state unknown",
        )
    at_cap = in_flight_count >= max_in_flight
    return {
        "in_flight_count": in_flight_count,
        "nearest_engine_deadline": _nearest_actionable_engine_deadline(),
        "actionable_queue": actionable_queue,
        "at_cap": at_cap,
        "quiescent": in_flight_count == 0 and not actionable_queue and not at_cap,
        "held_items": held_items,
        "target_state": target_state,
    }


# One queue read must be able to prove "everything on this page is held"
# without truncation hiding a dispatchable item on a later page.
_ACTIONABLE_QUEUE_SCAN_LIMIT = 200

# The dispatch-eligible pre-labels, in dispatch order. Derived from the same
# stage tables the dispatch gate and every cadence probe judge against, so a
# stage cannot gain a pickup label that the queue scan never reads. Shared by
# every cadence surface (in-process loop probe and one-shot post-dispatch
# state) so both apply the identical actionability contract.
_DISPATCH_ELIGIBLE_QUEUES = tuple(
    (pipeline.STAGE_ENTITIES[stage], label)
    for stage, labels in pipeline.STAGE_PICKUP_LABELS.items()
    for label in labels
)


def _queue_page_has_dispatchable(entity: str, label: str) -> bool:
    """Read one labeled queue page and report whether it has dispatchable work.

    Applies the same eligibility rule as dispatch pickup: items carrying a
    human hold or an in-flight label do not count. Issue queues are narrowed
    to the operator on the server, so other authors cannot fill the page and
    make a permanently undispatchable queue look truncated.

    Args:
        entity: ``"issue"`` or ``"pr"``.
        label: The pre-dispatch label naming the queue.

    Returns:
        ``True`` when the page holds at least one dispatchable item, or when
        the page cannot prove a fully blocked queue (unreadable item shape, or
        a page at the scan limit that may hide items beyond it).

    Raises:
        subprocess.CalledProcessError: When the ``gh`` read fails.
        ValueError: When ``gh`` returns malformed JSON.
    """
    items = gh_json(
        entity, "list", "--state", "open",
        "--label", label,
        "--limit", str(_ACTIONABLE_QUEUE_SCAN_LIMIT),
        *(("--author", TRUSTED_REVIEW_AUTHOR) if entity == "issue" else ()),
        "--json", "labels,author" if entity == "issue" else "labels",
    )
    return _any_dispatchable_item(items, entity)


def _has_actionable_queue() -> bool:
    """Return whether any ready issue or PR queue has dispatchable work.

    Applies the same eligibility rule as dispatch: an item carrying a human
    hold or an in-flight label next to its pre-dispatch label is skipped at
    pickup, so it must not count as actionable either — otherwise a queue of
    items no tick can act on keeps the loop awake forever. Unknown state
    stays warm: a failed read, a malformed label payload, or a page at the
    scan limit (possible truncation) all count as actionable.
    """
    try:
        for entity, label in _DISPATCH_ELIGIBLE_QUEUES:
            if _queue_page_has_dispatchable(entity, label):
                return True
    except (subprocess.CalledProcessError, ValueError):
        # ValueError covers malformed/truncated JSON from a zero-exit gh;
        # either way the queue state is unknown and must stay warm.
        return True
    return False


def _target_has_actionable_queue(target: tuple[str, int]) -> bool:
    """Return whether a targeted loop's own scope still has dispatchable work.

    A targeted loop only ever dispatches against the items ``target`` resolves
    to, so its actionable queue covers those items alone. The scope is
    resolved through the same ``_resolve_stage_targets`` dispatch uses, so an
    issue target whose closing PR is still in review keeps the loop awake.

    Args:
        target: The loop's parsed CLI target.

    Returns:
        ``True`` when any item in the target's scope is dispatchable, or when
        the scope or an item's state cannot be read — unknown state stays warm.
    """
    return _target_actionable_state(target) is not False


def _target_actionable_state(target: tuple[str, int]) -> bool | None:
    """Return the target's tri-state dispatch eligibility."""
    return _observe_target_state(target).actionable


def _observe_target_state(target: tuple[str, int]) -> pipeline.TargetCadenceState:
    """Read every resolved stage scope before claiming target completion.

    An open issue with no workflow label has handed implementation to its
    open closing PR. Its remaining active scope is that PR. Any remaining
    issue workflow label still participates, including blocked/in-flight work.
    Unknown or ambiguous reads never prove completion.
    """
    name = f"target {target[0]} #{target[1]}"
    try:
        stages = _resolve_stage_targets(target)
        scope = sorted({scoped for scoped in stages.values() if scoped is not None})
        if not scope:
            raise ValueError("empty target scope")
        observations = []
        observed_states = {}
        # Observe the selected PR before deciding whether an open issue may
        # hand off to it. Resolution's earlier OPEN read is not sufficient.
        for entity, number in sorted(scope, key=lambda scoped: scoped[0] == "issue"):
            payload = gh_json(
                entity, "view", str(number),
                "--json", "labels,state,author" if entity == "issue" else "labels,state",
            )
            if not isinstance(payload, dict):
                raise ValueError("invalid target payload")
            state = payload.get("state")
            if state not in ("OPEN", "CLOSED", "MERGED"):
                raise ValueError("invalid target state")
            observed_states[(entity, number)] = state
            if state != "OPEN":
                observations.append((False, True, f"{entity} #{number} {state}"))
                continue
            labels = _pipeline_labels(payload)
            if labels is None:
                raise ValueError("invalid target labels")
            label_text = ", ".join(sorted(labels)) or "no ai: label"
            detail = f"{entity} #{number} {label_text}"
            if entity == "issue" and not labels and stages["reviewer"] != target:
                if observed_states.get(stages["reviewer"]) != "OPEN":
                    raise ValueError("closing PR changed state during target observation")
                observations.append((False, True, f"{detail} (handed off to closing PR)"))
                continue
            # A human-decision hold already outranks other workflow labels,
            # including a live engine's label. Other mixed states are unfinished.
            terminal = pipeline.LABEL_NEEDS_HUMAN in labels or (
                len(labels) == 1 and labels[0] in {
                    pipeline.LABEL_READY_FOR_HUMAN, pipeline.LABEL_MAX_ITERATIONS,
                }
            )
            actionable = bool(
                pipeline.resolve_pickup_eligibility(labels, entity=entity).label,
            )
            if entity == "issue" and not terminal:
                trusted = _issue_author_trust(payload)
                if trusted is None:
                    raise ValueError("unknown issue author")
                actionable = actionable and trusted
            observations.append((actionable, terminal, detail))
    except (subprocess.CalledProcessError, ValueError):
        return pipeline.TargetCadenceState(None, False, f"{name}: state unknown")
    return pipeline.TargetCadenceState(
        any(row[0] for row in observations),
        all(row[1] for row in observations),
        f"{name}: " + "; ".join(row[2] for row in observations),
    )


def _observe_only_state(scope: OnlyScope) -> pipeline.TargetCadenceState:
    """Read the tri-state dispatch eligibility of a resolved ``--only`` scope.

    Uses the scope this tick already resolved for dispatch, so cadence and
    dispatch judge the same items and the list is not expanded again. Each
    item is judged by :func:`pipeline.resolve_pickup_eligibility`, the rule
    dispatch applies, and a listed issue must still pass the issue author
    gate. Unlike a positional target there is no terminal claim: the list
    stops only through quiet heartbeats, once nothing listed is dispatchable
    and no local engine remains.

    Args:
        scope: The scope this tick resolved.

    Returns:
        ``actionable`` is ``True`` when any item is dispatchable, ``False``
        when every item is proven non-dispatchable or closed, and ``None``
        when any read or payload could not be trusted. ``terminal`` is always
        ``False``.
    """
    name = f"--only {_only_text(scope.listed)}"
    details: list[str] = []
    actionable = False
    try:
        for entity, number in scope.items:
            payload = gh_json(
                entity, "view", str(number),
                "--json", "labels,state,author" if entity == "issue" else "labels,state",
            )
            if not isinstance(payload, dict):
                raise ValueError("invalid scope item payload")
            state = payload.get("state")
            if state not in ("OPEN", "CLOSED", "MERGED"):
                raise ValueError("invalid scope item state")
            if state != "OPEN":
                details.append(f"{entity} #{number} {state}")
                continue
            labels = _pipeline_labels(payload)
            if labels is None:
                raise ValueError("invalid scope item labels")
            details.append(
                f"{entity} #{number} {', '.join(sorted(labels)) or 'no ai: label'}",
            )
            dispatchable = bool(
                pipeline.resolve_pickup_eligibility(labels, entity=entity).label,
            )
            if dispatchable and entity == "issue":
                trusted = _issue_author_trust(payload)
                if trusted is None:
                    raise ValueError("unknown issue author")
                dispatchable = trusted
            actionable = actionable or dispatchable
    except (subprocess.CalledProcessError, ValueError):
        return pipeline.TargetCadenceState(None, False, f"{name}: state unknown")
    return pipeline.TargetCadenceState(
        actionable, False, f"{name}: " + "; ".join(details),
    )


def _issue_author_trust(metadata: object) -> bool | None:
    """Adapt the issue trust predicate for cadence's unknown-stays-warm rule.

    ``False`` means a known different author. ``None`` means unreadable author
    metadata; dispatch refuses it but cadence must not claim quiescence.
    """
    if not isinstance(metadata, dict):
        return None
    author = metadata.get("author")
    if not isinstance(author, dict):
        return None
    login = author.get("login")
    if not isinstance(login, str) or not login:
        return None
    return pipeline.trusted_issue_candidate(
        metadata, trusted_author=TRUSTED_REVIEW_AUTHOR,
    )


def _any_dispatchable_item(items: object, entity: str) -> bool:
    """Return whether a gh list payload contains a dispatchable queue item.

    Judges each item with :func:`pipeline.resolve_pickup_eligibility` — the
    same rule the dispatch gate applies — with no stage, because cadence asks
    whether *any* stage can take the item. Dispatch and cadence must agree
    exactly: an item cadence counts as work but dispatch permanently refuses
    holds the loop short of its quiescent stop forever.

    Args:
        items: Parsed JSON from ``gh ... list`` containing ``labels`` and,
            for issues, ``author``.
        entity: ``"issue"`` or ``"pr"`` — which stage vocabulary applies.

    Returns:
        ``True`` when at least one item is dispatchable, or when the read
        cannot prove a fully blocked queue: an unreadable payload shape, or a
        page at the scan limit that may hide items beyond it (unknown state
        must stay warm).
    """
    if not isinstance(items, list):
        return True
    if len(items) >= _ACTIONABLE_QUEUE_SCAN_LIMIT:
        return True
    for item in items:
        if entity == "issue":
            trusted = _issue_author_trust(item)
            if trusted is None:
                return True
            if not trusted:
                continue
        labels = _pipeline_labels(item)
        if labels is None:
            return True
        if pipeline.resolve_pickup_eligibility(labels, entity=entity).label:
            return True
    return False


def _wait_next_tick(
    seconds: int,
    stop_event: threading.Event,
    wake_event: threading.Event,
) -> tuple[bool, bool]:
    """Wait for the next tick: the full cadence interval or an earlier nudge.

    The cadence interval from :func:`pipeline.compute_next_wake` is an upper
    bound — a wake nudge (engine completion, wake-request file) ends the wait
    early. A nudge-initiated wake is held to ``LOOP_NUDGE_FLOOR_SECONDS``
    from the start of the wait so a nudge storm cannot force a hot tick loop,
    unless the scheduled cadence deadline arrives first.

    Args:
        seconds: Scheduled cadence interval from the cadence decision.
        stop_event: Set by the SIGINT/SIGTERM handler to request a clean stop.
        wake_event: Set by the wake watcher to request an early tick.

    Returns:
        ``(stopped, timer_expired)``. ``stopped`` means a stop was requested
        and the loop must exit. ``timer_expired`` means the wait ran its full
        scheduled interval — only such waits count toward the quiescent-stop
        heartbeat contract; nudge wakes must not accelerate it.
    """
    start = time.monotonic()
    scheduled_deadline = start + seconds
    nudged = wake_event.wait(seconds)
    if stop_event.is_set():
        return True, False
    if not nudged:
        return False, True
    now = time.monotonic()
    debounce_deadline = start + LOOP_NUDGE_FLOOR_SECONDS
    wait_deadline = min(scheduled_deadline, debounce_deadline)
    remaining = wait_deadline - now
    if remaining > 0 and stop_event.wait(remaining):
        return True, False
    now = time.monotonic()
    timer_expired = now >= scheduled_deadline or wait_deadline == scheduled_deadline
    return False, timer_expired


def _notify_loop_target_stop(reason: str) -> None:
    """Report the terminal target reason without claiming queue quiescence."""
    print(f"tick: {reason}", file=sys.stderr)
    try:
        os.makedirs(TICK_LOG_DIR, exist_ok=True)
        tick_log = TickLog.open(TICK_LOG_DIR)
    except (OSError, FileExistsError):
        tick_log = None
    if tick_log is not None:
        tick_log.log(
            pr=None, phase="loop", stage="", action="loop_target_stop", reason=reason,
        )
    _emit_operator_notification(title="PR pipeline target stopped", message=reason)


def _notify_loop_only_stop(reason: str) -> None:
    """Report an exhausted ``--only`` list without claiming queue quiescence.

    The reason comes from the tick's own cadence decision, so it already names
    the list and any human-held slots. Work outside the list may still be
    queued; the event name keeps that stop distinct from a drained backlog.
    """
    print(f"tick: {reason}", file=sys.stderr)
    try:
        os.makedirs(TICK_LOG_DIR, exist_ok=True)
        tick_log = TickLog.open(TICK_LOG_DIR)
    except (OSError, FileExistsError):
        tick_log = None
    if tick_log is not None:
        tick_log.log(
            pr=None, phase="loop", stage="", action="loop_only_stop", reason=reason,
        )
    _emit_operator_notification(title="PR pipeline --only list done", message=reason)


def _notify_loop_quiescent_stop(held_items: Sequence[str] = ()) -> None:
    """Notify the operator that loop mode stopped after quiescence."""
    reason = "PR pipeline loop stopped after consecutive quiescent heartbeats"
    if held_items:
        reason += f"; awaiting human decision: {', '.join(held_items)}"
    print(f"tick: {reason}", file=sys.stderr)
    try:
        os.makedirs(TICK_LOG_DIR, exist_ok=True)
        tick_log = TickLog.open(TICK_LOG_DIR)
    except (OSError, FileExistsError):
        tick_log = None
    if tick_log is not None:
        tick_log.log(
            pr=None, phase="loop", stage="", action="loop_quiescent_stop",
            reason=reason,
        )
    _emit_operator_notification(
        title="PR pipeline stopped",
        message=reason,
    )


def _emit_operator_notification(*, title: str, message: str) -> None:
    """Emit a best-effort macOS operator notification.

    Bounded, because stall reporting made this reachable from the wake-watcher
    thread: an ``osascript`` that never returns would park that thread
    mid-poll and silently end stall scans, completion nudges, and
    wake-request consumption for the life of the loop process. ``OSError``
    cannot catch a hang, so the bound is the timeout. Expiry is swallowed like
    every other notification failure — the tick-log entry is already written
    by the time this runs.

    Args:
        title: Notification title.
        message: Notification body.
    """
    if platform.system() != "Darwin":
        return
    safe_title = title.replace('"', r"\"")
    safe_message = message.replace('"', r"\"")
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            check=False,
            timeout=OPERATOR_NOTIFICATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_tick_body(
    tick_log: TickLog | None,
    parsed_target: tuple[str, int] | None,
    impl_engine: str,
    model: str = "",
    review_model: str = "",
    review_effort: str = "",
    permission_mode: str = "auto",
    trusted_author: str = "",
    *,
    count_quiescent_heartbeat: bool = True,
    config: pipeline.ResolvedConfig | None = None,
    quiescent_stop_after: int | None = None,
    only: tuple[tuple[str, int], ...] | None = None,
) -> TickOutcome:
    """Run the HARVEST → DISPATCH → Summary body of a tick.

    Extracted from ``main()`` so the try/except wrapper in ``main()``
    catches unexpected exceptions without catching intentional early returns.

    ``only`` bounds new launches alone. HARVEST, stale-label reconciliation,
    and occupancy stay global, so engines outside the list are still reaped
    and free their admission slots. The list is expanded once, after that
    custody work, and the same :class:`OnlyScope` feeds all three dispatchers
    and the cadence report. If the expansion read fails, the tick has
    already harvested; it then starts no engine, logs
    ``only_scope_unreadable``, and stays warm. The next tick reads again.

    Args:
        tick_log: Tick log handle, or ``None``.
        parsed_target: Parsed target tuple, or ``None`` for full scan.
        impl_engine: Engine for implementation dispatch.
        model: Model override for implementer and fixer engines.
        review_model: Model override for the reviewer engine.
        review_effort: Reasoning-effort override for the reviewer engine.
        permission_mode: Engine ``--permission-mode`` for implementer and
            fixer engines (``"auto"`` or ``"dontAsk"``).
        trusted_author: The operator login this tick resolved and validated,
            forwarded to the reviewer dispatch so admission uses the identity
            startup checked.
        count_quiescent_heartbeat: Whether a quiescent observation from this
            tick counts toward the consecutive-heartbeat stop. Only the
            in-process loop passes ``False``, for a nudge-initiated tick that
            followed no scheduled interval. See
            :func:`_advance_quiescent_counter`.
        quiescent_stop_after: Optional test override shared with the loop.
        config: The configuration the startup gate validated. Every stage
            reads its deadlines, stall limits, round limit, cooldowns, fixer
            route and cadence from it. ``None`` — direct callers only — runs
            the shipped defaults with the fixer on the implementer's route.
        only: Parsed ``--only`` list, or ``None``. Mutually exclusive with
            ``parsed_target``.

    Returns:
        Exit status and the exact cadence supplied to both summary outputs.
    """
    tuning = config if config is not None else _SHIPPED_CONFIG
    fix_engine, fix_model = (
        (config.fix_engine, config.fix_model) if config is not None
        else (impl_engine, model)
    )
    cadence = tuning.cadence()
    if quiescent_stop_after is not None:
        cadence["quiescent_stop_after"] = quiescent_stop_after
    max_in_flight = tuning.max_in_flight
    # Each scope owns one quiet streak: the queue, a positional target, or
    # the listed set. Every reset below touches only this tick's own scope.
    counter_scope: tuple[str, int] | str | None = (
        _only_counter_scope(only) if only is not None else parsed_target
    )
    # Every list-run cadence reason names the list, including the failure
    # paths that end before the list's items are observed.
    unobserved_scope = f"--only {_only_text(only)}: not observed" if only else None
    try:
        stage_targets = _resolve_stage_targets(parsed_target)
    except (subprocess.CalledProcessError, ValueError) as exc:
        if tick_log:
            tick_log.log(
                pr=parsed_target[1] if parsed_target else None,
                phase="target",
                stage="all",
                action="target_resolution_failed",
                reason=str(exc),
            )
        _write_quiescent_counter(0, parsed_target)
        tick_state = pipeline.derive_tick_state(status="FAILED")
        decision = pipeline.compute_next_wake(
            in_flight_count=None, has_actionable_queue=None, at_capacity=None,
            **cadence,
        )
        _log_tick_summary(tick_log, False, tick_state=tick_state, cadence=decision)
        _print_summary(tick_log, False, tick_state=tick_state, cadence=decision)
        return TickOutcome(1, decision)
    if tick_log and parsed_target is not None:
        # Scope evidence for the status dashboard banner.
        scoped = [parsed_target]
        for stage in ("reviewer", "fixer"):
            if stage_targets[stage] is not None and stage_targets[stage] not in scoped:
                scoped.append(stage_targets[stage])
        tick_log.log(
            pr=None, phase="target", stage="all", action="target_scope_resolved",
            reason=f"target {_only_token(parsed_target)}",
            details={
                "target": _only_token(parsed_target),
                "scope": [_only_token(item) for item in scoped],
            },
        )

    # Also reap on timed ticks if the advisory watcher could not start or died.
    _reap_engine_children()

    # Phase 0: Surface retained scratch dirs so orphaned state is visible.
    _surface_retained_scratch_dirs(WT_ROOT, tick_log)

    # Phase 0b: Surface engines that are alive but no longer producing. This
    # runs in *both* loop paths — the `--loop` watcher polls the same scan
    # every 5s to shorten the sleep, but one-shot ticks scheduled from the
    # `tick-cadence:` line have no watcher at all, and that is the mode
    # SKILL.md prescribes. The shared on-disk episode memory is what keeps the
    # two callers from double-reporting. It runs before HARVEST so the verdict
    # describes the state the operator's tick actually found, and its return
    # value is discarded: a nudge only means anything to a sleep, and a
    # one-shot tick has none.
    _scan_stalled_engines_safely(set())

    # Phase 1: HARVEST — reap finished engines.
    reviewers_ok = harvest_reviewers(
        tick_log, stage_targets["reviewer"],
        max_iterations=tuning.max_iterations,
    )
    implementers_ok = harvest_implementers(
        tick_log, stage_targets["implementer"],
        transient_cooldown_seconds=tuning.transient_cooldown_seconds,
        transient_cooldown_reset_buffer_seconds=(
            tuning.transient_cooldown_reset_buffer_seconds
        ),
    )
    fixers_ok = harvest_fixers(
        tick_log, stage_targets["fixer"],
        transient_cooldown_seconds=tuning.transient_cooldown_seconds,
        transient_cooldown_reset_buffer_seconds=(
            tuning.transient_cooldown_reset_buffer_seconds
        ),
    )
    reconcile_ok = reviewers_ok and implementers_ok and fixers_ok

    # Phase 1b: Reconcile stale in-flight labels (no local marker).
    # Runs even when reconcile_ok is False — stale labels are part of
    # the problem that causes over-counting.
    try:
        auth_labels = read_authoritative_in_flight_labels()
    except (subprocess.CalledProcessError, ValueError):
        auth_labels = []
    if auth_labels:
        # Build set of local marker (kind, number) pairs.
        local_markers: set[tuple[str, int]] = set()
        marker_scan_ok = True
        try:
            marker_entries = os.listdir(WT_ROOT)
        except OSError:
            marker_entries = []
            marker_scan_ok = False
            if tick_log:
                tick_log.log(
                    pr=None,
                    phase="harvest",
                    stage="",
                    action="stale_reconcile_skipped",
                    reason="os.listdir(WT_ROOT) failed; "
                           "local marker set untrustworthy",
                )
        for me in marker_entries:
            if me.startswith("pr-"):
                marker_path = os.path.join(WT_ROOT, me, "dispatch.json")
                if os.path.isfile(marker_path):
                    try:
                        num = int(me.split("-", 1)[1])
                        local_markers.add(("pr", num))
                    except ValueError:
                        pass
            elif me.startswith("issue-"):
                marker_path = os.path.join(WT_ROOT, me, "dispatch.json")
                if os.path.isfile(marker_path):
                    try:
                        num = int(me.split("-", 1)[1])
                        local_markers.add(("issue", num))
                    except ValueError:
                        pass
        if marker_scan_ok:
            stale_refs: list[dict[str, object]] = []
            for ref in auth_labels:
                stale_target = stage_targets["implementer"]
                if ref.label == pipeline.LABEL_REVIEWING:
                    stale_target = stage_targets["reviewer"]
                elif ref.label == pipeline.LABEL_FIXING:
                    stale_target = stage_targets["fixer"]
                if stale_target is not None:
                    target_kind, target_number = stale_target
                    if (ref.kind, ref.number) != (target_kind, target_number):
                        continue
                if (ref.kind, ref.number) not in local_markers:
                    stale_refs.append({
                        "kind": ref.kind,
                        "number": ref.number,
                        "label": ref.label,
                    })
            if stale_refs:
                _reconcile_stale_in_flight_labels(stale_refs, tick_log)

    # Phase 2: DISPATCH — launch new engines for eligible items.
    # Gated on reconcile_ok: a failed harvest enumeration means the in-flight
    # picture is incomplete and launching risks over-admission (Invariant 5).
    # Re-read authoritative in-flight labels for per-item *eligibility* — not
    # for the spend cap, which counts local markers alone. Each dispatcher
    # skips an item already at an in-flight label and appends the engines it
    # launches, so all three share one view of what this tick has taken.
    # Phase 1b already read them for stale reconciliation, but that
    # may have removed or escalated labels, so we re-read for accuracy.
    if not reconcile_ok:
        if tick_log:
            tick_log.log(
                pr=None, phase="dispatch", stage="all",
                action="skip_reconcile_failed",
                reason="reconciliation or authoritative in-flight state untrustworthy",
                details={
                    "reviewers_ok": reviewers_ok,
                    "implementers_ok": implementers_ok,
                    "fixers_ok": fixers_ok,
                },
            )
        tick_state = pipeline.derive_tick_state(status="FAILED")
        decision = pipeline.compute_next_wake(
            in_flight_count=None,
            has_actionable_queue=None,
            at_capacity=None,
            scope_description=unobserved_scope,
            **cadence,
        )
        _write_quiescent_counter(0, counter_scope)
        _log_tick_summary(
            tick_log, reconcile_ok, tick_state=tick_state, cadence=decision,
        )
        _print_summary(
            tick_log, reconcile_ok, tick_state=tick_state, cadence=decision,
        )
        return TickOutcome(1, decision)

    # Phase 1c: resolve the --only scope once, after custody work. A failed
    # read cannot strand unlisted engines — HARVEST and stale reconciliation
    # above already ran globally — so it only withholds new launches.
    only_scope: OnlyScope | None = None
    if only is not None:
        try:
            only_scope = _resolve_only_scope(only)
        except (subprocess.CalledProcessError, ValueError) as exc:
            reason = str(exc) or type(exc).__name__
            if isinstance(exc, subprocess.CalledProcessError):
                reason = f"GitHub read failed: {reason}"
            if tick_log:
                tick_log.log(
                    pr=None, phase="dispatch", stage="all",
                    action="only_scope_unreadable",
                    reason=f"--only scope expansion failed; no engine started: {reason}",
                    details={"only": [_only_token(item) for item in only]},
                )
            tick_state = pipeline.derive_tick_state(status="PARTIAL")
            decision = pipeline.compute_next_wake(
                in_flight_count=None, has_actionable_queue=None, at_capacity=None,
                scope_description=unobserved_scope,
                **cadence,
            )
            _write_quiescent_counter(0, counter_scope)
            _log_tick_summary(
                tick_log, reconcile_ok, tick_state=tick_state, cadence=decision,
            )
            _print_summary(
                tick_log, reconcile_ok, dispatch_skipped=True,
                skip_reason="--only scope unreadable",
                tick_state=tick_state, cadence=decision,
            )
            return TickOutcome(2, decision)
        if tick_log:
            tick_log.log(
                pr=None, phase="dispatch", stage="all",
                action="only_scope_resolved",
                reason=f"--only {_only_text(only_scope.listed)}",
                details={
                    "only": [_only_token(item) for item in only_scope.listed],
                    "scope": [_only_token(item) for item in only_scope.items],
                },
            )

    # Check the installed guidance once, before any stage creates a worktree or
    # touches a label. A stage whose docs are unreadable is held for this tick
    # and its items keep their eligible labels. HARVEST and stale-label
    # recovery have already run above and do not read these docs.
    guidance_blocked = _guidance_blocked_stages(stage_targets, tick_log)

    authoritative_in_flight: list[pipeline.InFlightRef] | None = None
    try:
        authoritative_in_flight = read_authoritative_in_flight_labels()
    except (subprocess.CalledProcessError, ValueError):
        # Fail-closed: if we cannot read in-flight state, refuse to launch
        # new engines rather than risk over-admission.
        authoritative_in_flight = None

    if authoritative_in_flight is not None:
        dispatch_items = only_scope.items if only_scope is not None else None
        # All three stages admit against the one cap startup validated, over
        # this machine's dispatch markers
        if "reviewer" not in guidance_blocked:
            dispatch_reviewers(
                tick_log=tick_log, target=stage_targets["reviewer"],
                authoritative_in_flight=authoritative_in_flight,
                review_model=review_model,
                review_effort=review_effort,
                trusted_author=trusted_author,
                max_in_flight=max_in_flight,
                timeout_seconds=tuning.review_timeout_seconds,
                only=dispatch_items,
            )
        if "implementer" not in guidance_blocked:
            dispatch_implementers(
                tick_log=tick_log, target=stage_targets["implementer"],
                authoritative_in_flight=authoritative_in_flight,
                impl_engine=impl_engine,
                model=model,
                permission_mode=permission_mode,
                max_in_flight=max_in_flight,
                timeout_seconds=tuning.implementer_timeout_seconds,
                stall_seconds=tuning.engine_stall_seconds,
                stall_startup_grace_seconds=tuning.engine_stall_startup_grace_seconds,
                only=dispatch_items,
            )
        if "fixer" not in guidance_blocked:
            dispatch_fixers(
                tick_log=tick_log, target=stage_targets["fixer"],
                authoritative_in_flight=authoritative_in_flight,
                impl_engine=fix_engine,
                model=fix_model,
                permission_mode=permission_mode,
                max_in_flight=max_in_flight,
                timeout_seconds=tuning.fixer_timeout_seconds,
                stall_seconds=tuning.engine_stall_seconds,
                stall_startup_grace_seconds=tuning.engine_stall_startup_grace_seconds,
                only=dispatch_items,
            )
    else:
        if tick_log:
            tick_log.log(
                pr=None, phase="dispatch", stage="all",
                action="skip_auth_labels_failed",
                reason="authoritative in-flight labels unavailable or invalid",
            )
        tick_state = pipeline.derive_tick_state(status="PARTIAL")
        decision = pipeline.compute_next_wake(
            in_flight_count=None,
            has_actionable_queue=None,
            at_capacity=None,
            scope_description=unobserved_scope,
            **cadence,
        )
        _write_quiescent_counter(0, counter_scope)
        _log_tick_summary(
            tick_log, reconcile_ok, tick_state=tick_state, cadence=decision,
        )
        _print_summary(
            tick_log, reconcile_ok, dispatch_skipped=True,
            tick_state=tick_state, cadence=decision,
        )
        return TickOutcome(2, decision)

    # Phase 3: Summary — compute post-dispatch state for the tick-state line.
    target_state = (
        _observe_target_state(parsed_target) if parsed_target is not None else None
    )
    # The list's cadence observes the scope dispatch just used; it does not
    # expand the list a second time. Occupancy stays global.
    only_state = _observe_only_state(only_scope) if only_scope is not None else None
    in_flight_count, has_queue, held_items = _compute_post_dispatch_state(
        parsed_target, target_state=target_state, only_state=only_state,
    )
    tick_state = pipeline.derive_tick_state(
        status="PARTIAL" if guidance_blocked else "ok",
        in_flight_count=in_flight_count,
        has_actionable_queue=has_queue,
        held_count=len(held_items) if in_flight_count is not None else None,
    )

    # Phase 3a: Cadence — compute next-wake interval from in-flight state.
    nearest_deadline_s = _compute_nearest_deadline_s()
    is_quiescent = tick_state.quiescent == "true"
    prior_quiescent = _advance_quiescent_counter(
        is_quiescent=is_quiescent,
        count_heartbeat=count_quiescent_heartbeat,
        target=counter_scope,
    )
    decision = pipeline.compute_next_wake(
        in_flight_count=in_flight_count,
        nearest_deadline_s=nearest_deadline_s,
        has_actionable_queue=has_queue,
        at_capacity=(
            in_flight_count is not None and in_flight_count >= max_in_flight
        ),
        is_quiescent=is_quiescent,
        consecutive_quiescent_ticks=prior_quiescent,
        count_quiescent_heartbeat=count_quiescent_heartbeat,
        held_items=held_items,
        target_state=target_state,
        scope_description=only_state.description if only_state is not None else None,
        **cadence,
    )

    _log_tick_summary(
        tick_log, reconcile_ok, tick_state=tick_state, cadence=decision,
    )
    _print_summary(
        tick_log, reconcile_ok, guidance_blocked=bool(guidance_blocked),
        tick_state=tick_state, cadence=decision,
    )

    return TickOutcome(2 if guidance_blocked else 0, decision, held_items)


def _log_tick_summary(
    tick_log: TickLog | None,
    reconcile_ok: bool,
    tick_state: pipeline.TickState | None = None,
    cadence: pipeline.CadenceDecision | None = None,
) -> None:
    """Append a per-tick summary entry with aggregated action counts.

    Reads back the current tick's log entries and counts actions so the
    summary is a self-contained roll-up — no need to thread counters through
    every harvest/dispatch function.

    Args:
        tick_log: Tick log handle, or ``None``.
        reconcile_ok: Whether harvest reconciliation succeeded.
        tick_state: Machine-readable tick state, or ``None`` to omit.
        cadence: Cadence decision for the next wake, or ``None`` to omit.
    """
    if tick_log is None:
        return
    entries = tick_log.tail()
    counts: dict[str, int] = {}
    for entry in entries:
        action = str(entry.get("action", ""))
        counts[action] = counts.get(action, 0) + 1
    details: dict[str, object] = {
        "reconcile_ok": reconcile_ok,
        "total_entries": len(entries),
        "launched": counts.get("launch", 0),
        "harvested": counts.get("harvest", 0),
        "skipped": counts.get("skip", 0),
        "escalated": counts.get("escalate", 0) + counts.get("kill_escalate", 0),
        "reverted": counts.get("revert", 0),
    }
    if tick_state is not None:
        details["tick_state"] = {
            "status": tick_state.status,
            "in_flight": tick_state.in_flight,
            "queue": tick_state.queue,
            "quiescent": tick_state.quiescent,
            "held": tick_state.held,
        }
    if cadence is not None:
        details["cadence"] = {
            "next_wake_seconds": cadence.next_wake_seconds,
            "stop": cadence.stop,
            "reason": cadence.reason,
        }
    tick_log.log(
        pr=None, phase="summary", stage="", action="tick_complete",
        reason="",
        details=details,
    )


def _print_summary(
    tick_log: TickLog | None,
    reconcile_ok: bool = True,
    *,
    dispatch_skipped: bool = False,
    guidance_blocked: bool = False,
    skip_reason: str = "auth labels read failed",
    tick_state: pipeline.TickState | None = None,
    cadence: pipeline.CadenceDecision | None = None,
) -> None:
    """Print a human-readable summary and machine-readable state lines."""
    if not reconcile_ok:
        status = "FAILED (dispatch skipped: reconcile failed)"
    elif dispatch_skipped:
        status = f"PARTIAL (dispatch skipped: {skip_reason})"
    elif guidance_blocked:
        status = "PARTIAL (dispatch blocked: installed guidance unreadable)"
    else:
        status = "ok"
    print(f"Tick complete. Status: {status}", file=sys.stderr)
    if tick_state is not None:
        print(tick_state.format_line(), file=sys.stderr)
    if cadence is not None:
        print(cadence.format_line(), file=sys.stderr)
    if tick_log:
        print(f"Log: {tick_log.path}", file=sys.stderr)
    sys.stderr.flush()


class CliArgs(TypedDict):
    """Parsed tick CLI arguments."""

    target: str | None
    only: str
    impl_engine: str
    model: str
    fix_engine: str
    fix_model: str
    review_model: str
    review_effort: str
    permission_mode: str
    loop: bool


# Flags that take a value, mapped to their CliArgs key.
_VALUE_FLAGS = {
    "--only": "only",
    "--impl-engine": "impl_engine",
    "--model": "model",
    "--fix-engine": "fix_engine",
    "--fix-model": "fix_model",
    "--review-model": "review_model",
    "--review-effort": "review_effort",
    "--permission-mode": "permission_mode",
}


def parse_cli_args(argv: list[str]) -> CliArgs:
    """Parse tick CLI arguments.

    Supports ``--loop``, the value flags in :data:`_VALUE_FLAGS` in both
    ``--flag value`` and ``--flag=value`` form, and at most one positional
    target. ``--only`` and a positional target are mutually exclusive.

    Args:
        argv: Arguments without the program name (i.e. ``sys.argv[1:]``).

    Returns:
        The parsed arguments. Unset value flags are ``""``, which each
        consumer reads as "use the operator's own default".

    Raises:
        SystemExit: When a value flag is given without a usable value —
            nothing follows it, the next token is another flag, or the value
            is empty — or when ``--permission-mode`` is not one of the
            supported modes. Failing here, before any state transition, is
            what keeps a typo'd flag from crashing the tick mid-dispatch.
            Also raised for a malformed or repeated ``--only`` list, a
            second positional target, or ``--only`` combined with a
            positional target: a silently replaced or narrowed scope would
            dispatch a different set than the operator asked for.
    """
    values: dict[str, str] = dict.fromkeys(_VALUE_FLAGS.values(), "")
    seen_flags: set[str] = set()
    target: str | None = None
    loop = False

    def set_value(flag: str, value: str) -> None:
        # Route flags keep their last-value behavior. A second --only list
        # would silently replace the first priority statement, so refuse it.
        if flag == "--only" and flag in seen_flags:
            print(f"tick: {flag} given more than once", file=sys.stderr)
            raise SystemExit(1)
        seen_flags.add(flag)
        values[_VALUE_FLAGS[flag]] = value

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--loop":
            loop = True
            i += 1
            continue

        if arg in _VALUE_FLAGS:
            # A following flag is a missing value, not the value: consuming
            # it would both mis-set this flag and silently drop the other one
            # (`--review-effort --loop` → effort "--loop", loop off). No
            # legitimate model, effort, engine, or target starts with a dash.
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if not value or value.startswith("-"):
                print(f"tick: {arg} requires a value", file=sys.stderr)
                raise SystemExit(1)
            set_value(arg, value)
            i += 2
            continue

        prefixed = next(
            (f for f in _VALUE_FLAGS if arg.startswith(f"{f}=")), None
        )
        if prefixed is not None:
            value = arg.split("=", 1)[1]
            # "" is the sentinel for "use the operator's default", so an
            # empty --flag= must not masquerade as an explicit choice.
            if not value:
                print(f"tick: {prefixed} requires a value", file=sys.stderr)
                raise SystemExit(1)
            set_value(prefixed, value)
            i += 1
            continue

        # A second positional used to replace the first silently, so
        # `--loop '#101' pr:66` ran pr:66 alone. Refuse it instead.
        if target is not None:
            print(
                "tick: only one positional target is allowed "
                f"(got {target!r} and {arg!r}); use --only for a list",
                file=sys.stderr,
            )
            raise SystemExit(1)
        target = arg
        i += 1

    # A positional target scopes HARVEST as well as DISPATCH; --only scopes
    # new launches alone. Accepting both would leave the harvest scope to
    # whichever one a reader noticed, so neither wins.
    if values["only"] and target is not None:
        print(
            "tick: --only and a positional target are mutually exclusive "
            f"(got --only {values['only']!r} and target {target!r})",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if values["only"]:
        _parse_only_list(values["only"])

    # "" means the flag was not typed, which is what lets the operator's
    # configured permission_mode apply. Defaulting it here would make every
    # invocation look like an explicit --permission-mode auto and shadow
    # the config layer entirely.
    permission_mode = values["permission_mode"]
    if permission_mode and permission_mode not in ("dontAsk", "auto"):
        print(
            f"tick: --permission-mode must be 'dontAsk' or 'auto', "
            f"got {permission_mode!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Every route flag stays "" unless typed, for the same reason: the tick
    # resolves the operator's configured route (and validates a typed one) at
    # startup, and a default here would shadow that configuration.
    return {
        "target": target,
        "only": values["only"],
        "impl_engine": values["impl_engine"],
        "model": values["model"],
        "fix_engine": values["fix_engine"],
        "fix_model": values["fix_model"],
        "review_model": values["review_model"],
        "review_effort": values["review_effort"],
        "permission_mode": permission_mode,
        "loop": loop,
    }


if __name__ == "__main__":  # pragma: no cover
    # Read-only reporting subcommand, dispatched before the tick CLI: it takes
    # no lock, makes no state transition, and is safe to run against a live
    # pipeline (the status dashboard calls it on every render).
    if sys.argv[1:2] == ["engine-progress"]:
        raise SystemExit(_cmd_engine_progress(sys.argv[2:]))
    cli_args = parse_cli_args(sys.argv[1:])
    raise SystemExit(
        main(
            target=cli_args["target"],
            only=cli_args["only"],
            impl_engine=cli_args["impl_engine"],
            model=cli_args["model"],
            fix_engine=cli_args["fix_engine"],
            fix_model=cli_args["fix_model"],
            review_model=cli_args["review_model"],
            review_effort=cli_args["review_effort"],
            permission_mode=cli_args["permission_mode"],
            loop=cli_args["loop"],
        )
    )
