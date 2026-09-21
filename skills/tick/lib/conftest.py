"""Shared pytest fixtures for the PR-pipeline tick skill tests."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

# Test identity is hermetic: startup resolution is stubbed by the fixture below
# unless a test opts into real_target. The login fallback is seeded at import;
# isolate_operator_config exercises per-tick login resolution from a file.
TRUSTED_AUTHOR = "test-owner"
os.environ.setdefault("PIPELINE_TRUSTED_AUTHOR", TRUSTED_AUTHOR)

# A PR body that satisfies `tick._pr_body_matches_guidelines` for issue #7 — the
# shape a fixer's replacement body has to reach before the harvest will publish
# it. Kept here so every revision test agrees on what "valid" means rather than
# each re-deriving the section contract.
REVISED_PR_BODY = """## Summary

Closes #7
- Problem: the published body described a head the fixer replaced.

## Why this approach

- How this solves the issue: the fixer rewrites the body it was handed.

## What changed

- Structure: the revision rides in the fixer worktree.
- Impact: the next reviewer reads a body that matches the head.

## Test coverage

- Covered by the harvest tests.

## Test plan

- [x] `uv run pytest`
"""

# The head OID a harvested fixer's PR sits at. Distinct from the marker's pinned
# `head_sha`, because a fixer that pushed nothing is escalated rather than
# rechecked — and a revision only publishes on a recheck.
FIXER_HARVEST_HEAD = "e" * 40

# The body the PR carried when its fixer was dispatched: valid enough to pin a
# `Closes #7` closure contract, stale enough to be worth replacing.
STALE_PR_BODY = "## Summary\n\nCloses #7\n- Problem: stale.\n"


@pytest.fixture(autouse=True)
def restore_cwd_after_test() -> Iterator[None]:
    """Restore the pytest process cwd after each skill test."""
    original_cwd = os.getcwd()
    try:
        yield
    finally:
        os.chdir(original_cwd)


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker that opts a test back into the real ``tick.gh``."""
    config.addinivalue_line(
        "markers", "real_target: exercise local Git target resolution",
    )
    config.addinivalue_line(
        "markers",
        "real_gh_helper: exercise tick.gh/relabel itself; the autouse "
        "mutation guard leaves tick.gh unpatched for this test",
    )


@pytest.fixture(autouse=True)
def isolate_operator_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., None]:
    """Keep simulated terminal transitions, stalls, and loop stops off the desktop.

    Patch only delivery: stderr, tick logs, and transition logic still run.
    Tests asserting notification content can install their own mock as usual.
    Return the original emitter for its boundary tests, which must mock
    ``subprocess.run`` before calling it. The module stays isolated even in
    those tests; no production code or operator settings change.
    """
    import tick

    emit = tick._emit_operator_notification
    monkeypatch.setattr(
        tick, "_emit_operator_notification", mock.Mock(return_value=None),
    )
    return emit


@pytest.fixture(autouse=True)
def block_real_gh_mutations(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neutralise ``tick.gh`` — the mutating GitHub seam — by default.

    ``tick.gh`` is the only helper that edits labels and posts comments, and
    the suite drives whole dispatch and harvest bodies. Any unmocked path
    through it runs a real ``gh`` against the live repo: that is how a test
    run relabelled healthy issues to ``ai:needs-human-decision`` (issue #592).

    A **mutating** call that reaches this default raises, classified by the
    same fail-closed :func:`is_mutating_gh` used by the ``subprocess.run``
    backstop. Fabricating success for a write would be worse than no guard:
    an unplanned mutation would neither fail the test nor exercise its own
    failure handling, and — because this fixture short-circuits ``tick.gh`` —
    it would never reach that backstop either.

    Reads return an empty success except default-branch discovery, which reports
    the suite's conventional ``main``. They are inert, and the many tests that
    drive a whole tick body only need the seam to not shell out. Note the
    read helpers ``gh_json``/``gh_text`` delegate to ``tick.gh``, so they are
    covered by this fixture too; a test that needs payloads from either
    stubs that helper directly.

    Tests that assert on a specific mutation patch ``tick.gh`` again inside
    their own ``with`` block, which shadows this default. Tests of the helper
    *itself* — which mock ``subprocess.run`` and need the real ``tick.gh`` to
    reach it — carry ``@pytest.mark.real_gh_helper``; the mock they install
    is what keeps them off the network.

    This sits *above* :func:`_no_github_mutation`, which guards the real
    ``subprocess.run`` boundary and aborts loudly. That guard stays the
    backstop for every other execution path (read helpers, ``git``, the
    engine wrapper); this fixture only spares the ``tick.gh`` seam from
    having to be re-stubbed in every test that drives a whole tick body.
    """
    if request.node.get_closest_marker("real_gh_helper") is not None:
        return

    import tick  # type: ignore[import-untyped]

    def refuse_real_gh(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = ["gh", *(str(a) for a in args)]
        if is_mutating_gh(argv):
            raise AssertionError(
                f"unstubbed mutating gh command: {' '.join(argv)} — a test "
                "that expects this write must install its own double for "
                "tick.gh and assert on the argv. Fabricating success here "
                "would let an unplanned mutation pass silently and would "
                "never exercise that write's failure handling.",
            )
        stdout = (
            '{"defaultBranchRef":{"name":"main"}}'
            if args[:2] == ("repo", "view") and "defaultBranchRef" in args
            else ""
        )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout)

    monkeypatch.setattr(tick, "gh", refuse_real_gh)


@pytest.fixture(autouse=True)
def isolate_marker_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point every ``WT_ROOT``-derived path at a per-test tmp directory.

    ``WT_ROOT`` is anchored to the git common dir, so inside this repo it
    resolves to the operator's *real* marker space. A test that runs any
    marker scan against it would read a live pipeline's dispatch markers —
    and a test that runs a tick body could act on them. Redirect the whole
    derived path set once, here, instead of trusting each test to remember.

    This reaches a call site only if that site reads the module global at
    call time. ``reconcile_engine`` and the three ``harvest_*`` entry points
    therefore take ``wt_root: str | None = None`` and resolve ``WT_ROOT``
    in the body: a ``wt_root: str = WT_ROOT`` default binds the real root
    once at import and this fixture can never displace it.
    ``test_harvest_entry_points_read_the_current_marker_root`` and
    ``test_reconcile_engine_scans_the_current_marker_root`` pin that.
    """
    import tick  # type: ignore[import-untyped]

    # A dedicated tmp tree, not the test's own ``tmp_path``: tests assert on
    # the exact contents of ``tmp_path``.
    root = tmp_path_factory.mktemp("wt-root")
    monkeypatch.setattr(tick, "WT_ROOT", str(root))
    monkeypatch.setattr(tick, "TICK_LOG_DIR", str(root / ".tick-log"))
    monkeypatch.setattr(tick, "TICK_LOCK_PATH", str(root / ".tick.lock"))
    monkeypatch.setattr(
        tick,
        "_QUIESCENT_COUNTER_FILE",
        str(root / ".tick-state" / "quiescent-counter.txt"),
    )
    monkeypatch.setattr(
        tick, "WAKE_REQUEST_PATH", str(root / ".tick-state" / "wake-request"),
    )
    monkeypatch.setattr(
        tick,
        "_STALL_EPISODES_FILE",
        str(root / ".tick-state" / "stall-episodes.json"),
    )
    monkeypatch.setattr(
        tick,
        "_STALL_EPISODES_LOCK",
        str(root / ".tick-state" / "stall-episodes.lock"),
    )


@pytest.fixture(autouse=True)
def _no_foreign_process_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confine every OS process read to the pytest process itself.

    Tests build markers with hard-coded identifiers (``12345``, ``99999``) on
    the assumption that they name nothing. PIDs are recycled, so that
    assumption holds only until it doesn't — and the signal-zero probes fail
    *open* on ``PermissionError`` (see :func:`process_control.pid_is_alive`),
    so a process owned by another user reads as alive rather than erroring.
    ``99999`` is macOS's ``PID_MAX``, i.e. the wrap-around point, not a quiet
    corner of the space. A background service landing on a marker's
    identifier rewrites a custody decision: twelve tests across
    ``_classify_marker``, ``_reconcile_one``, and the dispatch-blocking path
    flip from pass to fail with no change to the code under test, and *which*
    of them flip depends on which identifier collided — a live start time
    short-circuits the group probe, so the two hazards partly mask each other.

    The rule is that the only process this suite may observe is the one
    running it. Every other identifier is dead and has no start time. That
    keeps the ``os.getpid()`` tests honest — they read a process that
    genuinely exists — while making every invented identifier deterministic.

    All **three** reads have to be confined, not just the two probes.
    ``_classify_marker`` calls :func:`process_control.get_process_start_time`
    *first*, and its answer picks the branch: ``None`` yields identity
    ``unknown`` and falls through to the probes, while a real timestamp yields
    ``mismatched`` and the probes are never consulted at all. Guarding the
    probes alone would leave that fork decided by whichever PIDs the
    developer's machine happens to be using.

    Like :func:`_no_github_mutation`, the guard sits at the boundary where real
    execution happens, so a test that patches the helper never reaches it and
    stays free to assert whatever liveness it wants. Fixture patches apply
    before the test body, so an explicit ``mock.patch`` inside a test still
    wins.

    For the guard to be total, nothing may capture the real helper before it is
    patched — the same late-binding requirement :func:`isolate_marker_root`
    documents for ``WT_ROOT``. ``terminate_process`` and
    ``terminate_process_group`` therefore resolve their probe at call time from
    a ``None`` default rather than binding it as a default argument at import;
    ``test_terminate_process_resolves_its_probe_at_call_time`` and its group
    sibling pin that.

    One asymmetry is worth naming. For classification, ``dead`` is the
    conservative answer. For the termination helpers it is the permissive one:
    a probe that always answers ``False`` makes them return ``True`` without
    sending a signal, so a test must not read that as proof the owned tree
    died. Prove tree death by injecting ``is_alive``/``group_is_alive``
    explicitly.
    """
    import process_control  # type: ignore[import-untyped]

    self_pid = os.getpid()
    self_pgid = os.getpgrp()
    real_pid_is_alive = process_control.pid_is_alive
    real_pgid_is_alive = process_control.pgid_is_alive
    real_start_time = process_control.get_process_start_time

    def guarded_pid_is_alive(pid: int) -> bool:
        return real_pid_is_alive(pid) if pid == self_pid else False

    def guarded_pgid_is_alive(pgid: int) -> bool:
        return real_pgid_is_alive(pgid) if pgid == self_pgid else False

    def guarded_start_time(pid: int) -> float | None:
        return real_start_time(pid) if pid == self_pid else None

    monkeypatch.setattr(process_control, "pid_is_alive", guarded_pid_is_alive)
    monkeypatch.setattr(process_control, "pgid_is_alive", guarded_pgid_is_alive)
    monkeypatch.setattr(
        process_control, "get_process_start_time", guarded_start_time,
    )


# Known read-only ``gh`` command shapes, keyed by ``(group, subcommand)``. This
# is an allowlist rather than a denylist of write verbs: `gh` has far more write
# shapes than any enumeration of `comment`/`edit`/`create` covers — `gh workflow
# run`, `gh secret set`, `gh run rerun`, and `gh release upload` are all writes
# spelled with verbs no such denylist contains. An unrecognised shape is treated
# as a write, so a `gh` command this suite has never run aborts the test instead
# of reaching the live repository. Widening this set is a deliberate act.
_READ_ONLY_GH_COMMANDS = frozenset({
    ("pr", "view"), ("pr", "list"), ("pr", "diff"), ("pr", "checks"),
    ("pr", "status"),
    ("issue", "view"), ("issue", "list"), ("issue", "status"),
    ("repo", "view"), ("label", "list"), ("run", "view"), ("run", "list"),
    ("release", "view"), ("release", "list"), ("workflow", "view"),
    ("workflow", "list"), ("auth", "status"), ("search", "issues"),
    ("search", "prs"), ("search", "repos"), ("search", "code"),
})

# Read-only ``gh`` invocations that take no subcommand.
_READ_ONLY_GH_SINGLETONS = frozenset({"version", "--version", "help", "--help"})

# The ``gh api`` group is classified by HTTP method rather than by verb.
_API_GROUP = "api"

# The only HTTP methods that prove a read. ``gh api`` bypasses the named
# subcommands entirely and can perform every one of their writes, so the
# allowlist above is not a boundary on its own — a `gh api --method POST` would
# sail through it. Enumerating the mutating methods instead would reopen the
# denylist hole: an unrecognised method would read as safe.
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD"})
_METHOD_FLAGS = ("-X", "--method")

# Flags that carry a request body. Per ``gh help api``: "adding request
# parameters will automatically switch the request method to POST". A call
# using any of these is therefore a write unless it names a safe method
# explicitly — and ``gh api .../comments -f body=...`` is the CLI's own
# documented way to post a comment, so reading only ``-X``/``--method`` leaves
# the most likely accident wide open.
_BODY_FLAGS = ("-f", "--raw-field", "-F", "--field", "--input")


def _explicit_method(args: list[str]) -> str | None:
    """Return the effective HTTP method named on a ``gh`` argv, uppercased.

    ``gh`` resolves a repeated flag to its **last** occurrence, so the effective
    method of ``--method GET --method DELETE`` is ``DELETE``. Reading the first
    occurrence would let a write hide behind a leading safe method.

    Args:
        args: The full argument vector.

    Returns:
        The method given via ``-X``/``--method`` in any of its spellings
        (``-X POST``, ``-XPOST``, ``--method=POST``), taking the last one when
        the flag repeats, or ``None`` when the argv names none and the method is
        left implicit. A method flag whose value is missing yields ``""`` — a
        named-but-unreadable method, which is not a read.
    """
    method: str | None = None
    for index, arg in enumerate(args):
        for flag in _METHOD_FLAGS:
            if arg == flag:
                method = (args[index + 1] if index + 1 < len(args) else "").upper()
            elif arg.startswith(f"{flag}="):
                method = arg.split("=", 1)[1].upper()
        if arg.startswith("-X") and len(arg) > 2:
            method = arg[2:].upper()
    return method


def _command_shape(args: list[str]) -> tuple[str, str]:
    """Return the ``(group, subcommand)`` a ``gh`` argv names.

    Only the positional prefix is read: ``gh`` always takes its group and
    subcommand before any flag, so scanning stops at the first ``-``-prefixed
    token and a flag *value* can never be mistaken for a subcommand.

    Args:
        args: The full argument vector, including ``gh`` at index 0.

    Returns:
        The group and subcommand, each ``""`` when the argv does not name one.
    """
    positionals: list[str] = []
    for arg in args[1:]:
        if arg.startswith("-"):
            break
        positionals.append(arg)
    group = positionals[0] if positionals else ""
    sub = positionals[1] if len(positionals) > 1 else ""
    return group, sub


def _carries_request_body(args: list[str]) -> bool:
    """Return whether a ``gh api`` argv carries request-body parameters.

    Args:
        args: The full argument vector.

    Returns:
        ``True`` when any body-carrying flag is present, in either the
        space-separated or ``=`` spelling.
    """
    return any(
        arg == flag or arg.startswith(f"{flag}=")
        for arg in args
        for flag in _BODY_FLAGS
    )


def is_mutating_gh(argv: object) -> bool:
    """Return whether ``argv`` is a ``gh`` invocation that writes to GitHub.

    The classification fails closed: anything that is not recognisably a read
    counts as a write. For ``gh api`` that means the effective HTTP method (or,
    absent one, the presence of request-body parameters); for every other group
    it means membership in :data:`_READ_ONLY_GH_COMMANDS`.

    Args:
        argv: A candidate ``subprocess.run`` argument vector. Non-sequences and
            string commands (no ``gh`` argv[0]) are not classified as mutating.

    Returns:
        ``True`` for any ``gh`` invocation not proven read-only: an
        unrecognised command shape, a ``gh api`` call whose effective HTTP
        method is anything but ``GET``/``HEAD``, or a ``gh api`` call whose
        request-body parameters make the method an implicit ``POST``.
    """
    if not isinstance(argv, (list, tuple)):
        return False
    args = [str(a) for a in argv]
    if args[:1] != ["gh"]:
        return False
    if len(args) > 1 and args[1] in _READ_ONLY_GH_SINGLETONS:
        return False

    group, sub = _command_shape(args)
    if group != _API_GROUP:
        return (group, sub) not in _READ_ONLY_GH_COMMANDS

    method = _explicit_method(args)
    if method is not None:
        # Only GET and HEAD prove a read. Treating every other named method as a
        # write is what keeps the classification closed against methods this
        # guard has never seen — the alternative, an enumeration of mutating
        # verbs, is the same denylist shape that already let `gh workflow run`
        # and friends through.
        return method not in _SAFE_HTTP_METHODS
    return _carries_request_body(args)


@pytest.fixture(autouse=True)
def _no_github_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test would really execute a mutating ``gh`` command.

    ``tick.gh`` shells out for real, so a test that exercises a harvest or
    dispatch path without patching that boundary does not fail — it mutates the
    live repository. That is not hypothetical: adding the fixer comment post
    made four previously-passing fixer tests post stray comments on real PRs
    before anyone noticed.

    The guard sits at ``subprocess.run`` rather than at ``tick.gh`` so it keys
    on real execution, not on intent: a test that patches ``subprocess.run`` (or
    ``tick.gh``) never reaches this wrapper and is free to assert on any argv it
    likes, while an *unpatched* mutating ``gh`` aborts the test. Reads pass
    through — they are inert, and the ``gh`` helper's own unit tests rely on
    reaching the wrapper.

    See :func:`is_mutating_gh` for the classification. It fails closed: only
    recognised read shapes (and ``gh api`` calls whose effective method is safe)
    pass, so a ``gh`` command this suite has never run aborts the test rather
    than reaching the live repository.
    """
    real_run = subprocess.run

    def guarded_run(argv: object, *args: object, **kwargs: object) -> object:
        if is_mutating_gh(argv):
            rendered = " ".join(str(a) for a in argv)  # type: ignore[union-attr]
            raise AssertionError(
                f"test would really run a mutating gh command: {rendered} — "
                "patch tick.gh or subprocess.run",
            )
        return real_run(argv, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", guarded_run)


@pytest.fixture(autouse=True)
def isolate_operator_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Keep every test hermetic against both configuration layers.

    Two files feed ``resolve_config``: the packaged floor committed in this
    repo, and — far more dangerous for a test suite — whatever the developer
    running pytest has in their own ``~/.claude/pr-pipeline/config.toml``. A
    suite that read the latter would pass or fail depending on whose laptop it
    ran on, so both are pointed at nonexistent files here and every test starts
    from the conservative default.

    Tests that exercise a configured value set ``PR_PIPELINE_CONFIG`` (for
    operator settings) or ``_PACKAGED_CONFIG_PATH`` (for the shipped floor),
    either of which overrides this default.

    ``CLAUDE_CONFIG_DIR`` is cleared too: it relocates the Claude home, so a
    developer who exports it would otherwise reintroduce the real file the
    moment a test cleared ``PR_PIPELINE_CONFIG``.

    The one value the stand-in file *does* declare is the operator login. The
    tick resolves the identity it reviews under per tick and refuses to start
    without one, and ``PIPELINE_TRUSTED_AUTHOR`` is cleared here so that
    resolution is exercised from a file rather than from the environment the
    suite happens to be run in. It has to name the same operator
    :data:`TRUSTED_AUTHOR` does, or every test that reaches the startup gate
    would be running as somebody the pipeline does not trust.
    """
    import pipeline

    monkeypatch.setattr(
        pipeline, "_PACKAGED_CONFIG_PATH", Path("/nonexistent/config.toml")
    )
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_TRUSTED_AUTHOR", raising=False)
    cfg = tmp_path_factory.mktemp("operator-config") / "config.toml"
    cfg.write_text(
        f'[operator]\nlogin = "{TRUSTED_AUTHOR}"\n', encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))


@pytest.fixture(autouse=True)
def isolate_target_resolution(request, monkeypatch):
    """Unit ticks use a declared target; integration tests opt into real Git."""
    import pipeline
    import tick

    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", f"{TRUSTED_AUTHOR}/test-repo")
    monkeypatch.setattr(tick, "TARGET_REMOTE", "origin")
    monkeypatch.setattr(tick, "TRUSTED_REVIEW_AUTHOR", TRUSTED_AUTHOR)
    if request.node.get_closest_marker("real_target") is None:
        monkeypatch.setattr(tick, "resolve_target", lambda: pipeline.ResolvedTarget(
            f"{TRUSTED_AUTHOR}/test-repo", "origin",
        ))
        monkeypatch.setattr(pipeline, "validate_target_context", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def isolate_label_vocabulary(monkeypatch):
    """Whole-tick tests start with a provisioned repo; guard tests override this.

    Provisioning and pagination tests execute the installed CLI in a separate
    process with a stateful gh double, so this seam cannot hide their I/O.
    """
    import labels
    import pipeline

    monkeypatch.setattr(labels, "read_labels", lambda repo: pipeline.WORKFLOW_LABELS)


# --- Helpers ---


def allow_escalation_comment(
    recorder: list[tuple[str, ...]] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a ``tick.gh`` double that lets a pre-launch escalation comment land.

    Every pre-launch dispatch escalation publishes before it relabels, and the
    relabel is *gated* on that post. :func:`block_real_gh_mutations` refuses the
    post, so a test whose subject is the escalation transition has to let the
    comment through — otherwise the escalation never reaches its relabel and the
    test passes by asserting the held-label path it did not mean to exercise.

    Only the comment is fabricated. Every other write still aborts the test
    through the same :func:`is_mutating_gh` classification, so this cannot
    quietly become a blanket "all writes succeed" seam.

    Args:
        recorder: Optional list that each ``gh`` argv is appended to, for tests
            that assert on publication ordering.

    Returns:
        A side effect for ``mock.patch("tick.gh", side_effect=...)``.
    """
    def side_effect(
        *args: str, **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        argv = ["gh", *(str(a) for a in args)]
        if recorder is not None:
            recorder.append(tuple(str(a) for a in args))
        if args[1:2] == ("comment",):
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout="https://example.invalid/c/1",
            )
        if is_mutating_gh(argv):
            raise AssertionError(
                f"unstubbed mutating gh command: {' '.join(argv)} — "
                "allow_escalation_comment only fabricates the escalation "
                "comment; install a double for this write and assert on it.",
            )
        stdout = (
            '{"defaultBranchRef":{"name":"main"}}'
            if args[:2] == ("repo", "view") and "defaultBranchRef" in args
            else ""
        )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout)

    return side_effect


def fixer_git(repo_root: Path) -> Callable[..., str]:
    """Return a ``tick.git`` side effect that materializes fixer worktrees.

    The bare directory is all a fixer dispatch needs from the checkout. The
    skill docs its prompt binds are resolved from the pipeline install, not
    from the PR head this worktree stands in for — a tree in a repository the
    pipeline does not own and that has no reason to carry them.

    Args:
        repo_root: The checkout root every other ``git`` read resolves to.

    Returns:
        A side effect that creates the worktree directory.
    """
    def side_effect(*args: str) -> str:
        if "worktree" in args and "add" in args:
            Path(args[-2]).mkdir(parents=True, exist_ok=True)
        return str(repo_root)

    return side_effect


@contextlib.contextmanager
def fixer_harvest_gh(
    calls: list[str],
    staged: list[tuple[str, str]],
    *,
    live_body: str,
    live_head: str = "e" * 40,
    edit_fails: bool = False,
    mutation_body: str | None = None,
    mutation_head: str | None = None,
    mutation_labels: Sequence[str] = ("ai:fixing",),
    mutation_read_fails: bool = False,
) -> Iterator[None]:
    """Fake the whole GitHub seam one fixer harvest reaches, recording order.

    ``harvest_fixers`` is the production entry point, so a test that drives it
    has to satisfy the enumeration, lifecycle, and point-read calls before it
    reaches the outcome under test. Recording every mutation in one ordered list
    is what lets a test assert the body edit lands *before* the comment and the
    relabel — three separate mocks cannot prove an ordering between themselves.

    The harvest reads the PR twice on the revision path: once to classify the
    candidate, and once again immediately before ``gh pr edit`` to re-check the
    authority it is about to overwrite. The ``mutation_*`` arguments answer only
    that second read, which is how a test interleaves a human hold, a force-push,
    or a body edit into the window between them.

    Args:
        calls: Collector for the ordered mutation log (``"pr edit"``,
            ``"pr comment"``, ``"converge"``).
        staged: Collector of ``(path, content)`` for every ``--body-file`` the
            tick stages, captured before the tick unlinks it.
        live_body: The PR body the harvest's classification point-read returns.
        live_head: The PR head OID the harvest's classification point-read
            returns.
        edit_fails: When true, ``gh pr edit`` raises as a failed remote write.
        mutation_body: Body for the mutation-boundary re-read; ``None`` reuses
            ``live_body`` (nothing changed in the window).
        mutation_head: Head OID for the mutation-boundary re-read; ``None``
            reuses ``live_head``.
        mutation_labels: Labels the mutation-boundary re-read reports.
        mutation_read_fails: When true, the mutation-boundary re-read raises, as
            a `gh` call that cannot reach GitHub does.

    Yields:
        ``None``; the seam is patched for the duration of the block.
    """
    def fake_gh_text(*args: str) -> str:
        return "12" if args[1] == "list" else "OPEN"

    def fake_gh_json(*args: str) -> object:
        fields = args[args.index("--json") + 1] if "--json" in args else ""
        if "headRefOid" in fields and "labels" in fields:
            if mutation_read_fails:
                raise subprocess.CalledProcessError(1, "gh")
            return {
                "labels": [{"name": name} for name in mutation_labels],
                "headRefOid": live_head if mutation_head is None
                else mutation_head,
                "body": live_body if mutation_body is None else mutation_body,
            }
        if "headRefOid" in fields:
            return {"headRefOid": live_head, "body": live_body}
        return {"labels": [{"name": "ai:fixing"}]}

    def fake_gh(*args: str) -> object:
        calls.append(f"{args[0]} {args[1]}")
        if "--body-file" in args:
            path = args[args.index("--body-file") + 1]
            staged.append((path, Path(path).read_text()))
        if edit_fails and args[:2] == ("pr", "edit"):
            raise subprocess.CalledProcessError(1, "gh")
        return SimpleNamespace(stdout="https://example.invalid/c/1")

    def fake_converge(*_args: object, **_kwargs: object) -> bool:
        calls.append("converge")
        return True

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch("tick.process_control.pid_is_alive", return_value=False),
        )
        stack.enter_context(mock.patch("tick.gh_text", side_effect=fake_gh_text))
        stack.enter_context(mock.patch("tick.gh_json", side_effect=fake_gh_json))
        stack.enter_context(mock.patch("tick.gh", side_effect=fake_gh))
        stack.enter_context(
            mock.patch("tick._converge_pipeline_labels", side_effect=fake_converge),
        )
        yield


def fixer_harvest_scratch(tmp_path: Path) -> tuple[Path, Path]:
    """Create a finished fixer scratch for PR 12 under a fresh worktree root.

    Writes the result and completion envelope a harvest needs to classify the
    dispatch as ``complete``; the caller supplies ``dispatch.json`` and any
    revision file, which are what the tests actually vary.

    Args:
        tmp_path: The test's temp directory.

    Returns:
        ``(wt_root, wt_dir)``.
    """
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED"}',
    )
    (wt_dir / "completion.json").write_text(fixer_harvest_completion())
    return wt_root, wt_dir


def fixer_harvest_marker(**overrides: object) -> str:
    """Return a published fix-stage ``dispatch.json`` for PR 12, JSON-encoded.

    Args:
        **overrides: Marker fields to replace or add.

    Returns:
        The serialized marker, ready to write into the fixer worktree.
    """
    marker: dict[str, object] = {
        "engine": "claude", "stage": "fix", "pr_number": 12,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b" * 40,
        "head_sha": "d" * 40, "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-12-1", "pid": 4242, "start_time": 1234567.0,
        "pgid": 4242,
    }
    marker.update(overrides)
    return json.dumps(marker)


def fixer_harvest_completion(dispatch_id: str = "fix-12-1") -> str:
    """Return a valid ``completion.json`` bound to one fixer dispatch.

    Args:
        dispatch_id: The dispatch the envelope belongs to.

    Returns:
        The serialized envelope, ready to write into the fixer worktree.
    """
    return json.dumps({
        "dispatch_id": dispatch_id, "exit_status": 0, "wrapper_pid": 4242,
        "start_time": 1234567.0, "result_valid": True,
    })


def fixer_body_provenance(
    body: str, *, truncated: bool = False,
) -> dict[str, object]:
    """Return the ``pr_body`` marker block dispatch would pin for one body.

    Built through the production helper rather than hand-written, so a test
    cannot pass against a closure contract dispatch would never actually record.

    Args:
        body: The PR body dispatch read.
        truncated: Whether the engine was shown only a prefix of it.

    Returns:
        The marker block, ready to pass to :func:`fixer_harvest_marker`.
    """
    import tick  # type: ignore[import-untyped]

    provenance = tick._pr_body_provenance(body, truncated=truncated)
    assert provenance is not None, (
        f"this body declares no `Closes #N` under `## Summary`: {body!r}"
    )
    return provenance.as_marker_field()


def fixer_revision_file(
    body: str,
    *,
    dispatch_id: str = "fix-12-1",
    head: str = FIXER_HARVEST_HEAD,
) -> str:
    """Return the candidate-revision file a fixer writes, envelope included.

    Args:
        body: The replacement PR body.
        dispatch_id: The dispatch the engine was told to stamp.
        head: The commit the engine declares the replacement describes.

    Returns:
        The full file text, ready to write into the fixer worktree.
    """
    import tick  # type: ignore[import-untyped]

    return (
        f"{tick._PR_BODY_REVISION_STAMP} {dispatch_id}\n"
        f"{tick._PR_BODY_REVISION_HEAD_STAMP} {head}\n"
        f"{body}"
    )


def fixer_gh_json(
    *,
    pr_body: str = "",
    issues: Sequence[dict[str, object]] = (),
    comments: list[dict[str, object]] | None = None,
) -> Callable[..., object]:
    """Return a ``tick.gh_json`` side effect covering the fixer's dispatch reads.

    Dispatch makes two typed reads before it renders the prompt — the PR's body
    plus its closing-issue references, and each referenced issue — on top of the
    comment scan that finds the review. Keying on the argv rather than returning
    one payload keeps a test from passing because every read happened to receive
    the same dict; keying the issue read on the requested number keeps a
    multi-issue test from passing because every issue happened to return the
    same contract.

    Args:
        pr_body: Body text for the PR under repair.
        issues: The linked issues, each a ``number``/``title``/``body`` payload,
            in the order GitHub reports them. Empty means a PR that closes none.
        comments: PR comments, defaulting to one pipeline review comment.

    Returns:
        A side effect for ``mock.patch("tick.gh_json", side_effect=...)``.
    """
    import pipeline  # type: ignore[import-untyped]

    if comments is None:
        comments = [{"body": f"{pipeline.REVIEW_MARKER}\nfindings"}]
    refs = [{"number": issue.get("number", 7)} for issue in issues]
    by_number = {str(issue.get("number", 7)): issue for issue in issues}

    def side_effect(*args: str) -> object:
        fields = args[args.index("--json") + 1] if "--json" in args else ""
        if args[0] == "issue":
            return by_number.get(args[2], {})
        if "comments" in fields:
            return {"comments": comments}
        return {"body": pr_body, "closingIssuesReferences": refs}

    return side_effect
