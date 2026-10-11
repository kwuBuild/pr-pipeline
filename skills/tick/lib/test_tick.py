"""Tests for the PR-pipeline tick orchestrator.

Co-located with ``tick.py`` so the whole ``tick/lib`` directory lifts
out cleanly. Run with::

    uv run pytest .claude/skills/tick/lib/test_tick.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from conftest import (  # type: ignore[import-untyped]
    FIXER_HARVEST_HEAD,
    TRUSTED_AUTHOR,
    REVISED_PR_BODY,
    STALE_PR_BODY,
    allow_escalation_comment,
    fixer_body_provenance,
    fixer_gh_json,
    fixer_git,
    fixer_harvest_gh,
    fixer_harvest_marker,
    fixer_harvest_scratch,
    fixer_revision_file,
)

# --- WT_ROOT anchoring ---


def test_resolve_wt_root_is_identical_from_repo_root_and_from_a_worktree(
    tmp_path: Path,
) -> None:
    """The marker space is anchored to the git common dir, not the cwd.

    A tick invoked from inside a git worktree must not resolve WT_ROOT to
    ``<worktree>/.claude/worktrees`` — an empty directory that makes every
    in-flight item look markerless.
    """
    import tick  # type: ignore[import-untyped]

    main = tmp_path / "main"
    main.mkdir()
    git_init_repo(main)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt", str(linked)],
        cwd=main, check=True, capture_output=True,
    )

    os.chdir(main)
    from_root = tick._resolve_wt_root()
    os.chdir(linked)
    from_worktree = tick._resolve_wt_root()

    assert os.path.isabs(from_root)
    assert from_root == from_worktree
    assert from_root == str(main.resolve() / ".claude" / "worktrees")


def test_resolve_wt_root_falls_back_to_cwd_outside_a_git_repository(
    tmp_path: Path,
) -> None:
    """Outside a repo the resolver still returns an absolute path."""
    import tick  # type: ignore[import-untyped]

    os.chdir(tmp_path)
    resolved = tick._resolve_wt_root()

    assert resolved == str(tmp_path.resolve() / ".claude" / "worktrees")


@pytest.mark.parametrize(
    "harvest_name",
    ["harvest_reviewers", "harvest_implementers", "harvest_fixers"],
)
def test_harvest_entry_points_read_the_current_marker_root(
    harvest_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harvest called without ``wt_root`` must use the *live* ``WT_ROOT``.

    ``_run_tick_body`` calls all three harvesters positionally, so a default
    bound at import time would send a whole-tick test at the operator's real
    marker space — where a terminal path can kill a live process group,
    archive its evidence, or remove its scratch. That is the failure
    ``isolate_marker_root`` exists to prevent, and a default evaluated once
    at import silently defeats it.
    """
    import tick  # type: ignore[import-untyped]

    sentinel = str(tmp_path / "sentinel-root")
    monkeypatch.setattr(tick, "WT_ROOT", sentinel)
    seen: list[str] = []

    def record(
        _config: object,
        _tick_log: object,
        _target: object,
        *,
        wt_root: str,
        attempts: object = None,
    ) -> tuple[list[dict[str, object]], bool]:
        seen.append(wt_root)
        return [], True

    with mock.patch("tick.reconcile_engine", side_effect=record):
        getattr(tick, harvest_name)()

    assert seen == [sentinel]


def test_reconcile_engine_scans_the_current_marker_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reconcile_engine`` resolves its marker root at call time too.

    The three harvesters forward whatever root they resolved, but
    ``reconcile_engine`` is also called directly, so its own default must
    track the patched module global rather than the import-time value.
    """
    import tick  # type: ignore[import-untyped]

    sentinel = tmp_path / "sentinel-root"
    sentinel.mkdir()
    monkeypatch.setattr(tick, "WT_ROOT", str(sentinel))
    listed: list[str] = []
    real_listdir = os.listdir

    def record_listdir(path: str) -> list[str]:
        listed.append(str(path))
        return real_listdir(path)

    with mock.patch("tick.gh_text", return_value=""):
        with mock.patch("tick.os.listdir", side_effect=record_listdir):
            tick.reconcile_engine(tick._REVIEWER_CONFIG, None, None)

    assert str(sentinel) in listed


# --- gh / gh_json / gh_text helpers ---


def test_tick_lock_reports_held_when_lock_file_is_already_locked(tmp_path: Path) -> None:
    """TickLock.acquire() is non-blocking and reports a held lock."""
    from tick import TickLock  # type: ignore[import-untyped]

    lock_path = tmp_path / ".tick.lock"
    first = TickLock(str(lock_path))
    assert first.acquire() is True

    try:
        second = TickLock(str(lock_path))
        assert second.acquire() is False
    finally:
        first.release()


def test_main_returns_distinct_code_when_tick_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits before tick work when another invocation holds the mutex."""
    import tick  # type: ignore[import-untyped]
    from tick import LOCK_HELD_EXIT_CODE, TickLock, main  # type: ignore[import-untyped]

    lock_path = tmp_path / ".tick.lock"
    monkeypatch.setattr(tick, "TICK_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(tick, "git", lambda *_args: str(tmp_path))
    lock = TickLock(str(lock_path))
    assert lock.acquire() is True

    try:
        import io
        stderr = io.StringIO()
        with mock.patch("tick._run_tick_body") as run_tick_body:
            with mock.patch("sys.stderr", stderr):
                assert main() == LOCK_HELD_EXIT_CODE
            run_tick_body.assert_not_called()
        assert "tick-cadence: next_wake=270 stop=false" in stderr.getvalue()
    finally:
        lock.release()


def test_run_loop_locked_stops_on_quiescent_limit_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop mode stops after the configured quiescent heartbeat limit."""
    import tick  # type: ignore[import-untyped]

    tick_calls: list[str] = []
    waits: list[int] = []
    monkeypatch.setattr(
        tick,
        "_run_single_tick_locked",
        lambda *_args, **_kwargs: tick_calls.append("tick") or 0,
    )
    monkeypatch.setattr(
        tick,
        "_observe_loop_state",
        lambda _target=None, **_kwargs: {
            "in_flight_count": 0,
            "nearest_engine_deadline": None,
            "actionable_queue": False,
            "at_cap": False,
            "quiescent": True,
            "held_items": ("pr #42 ai:reviewing (ai:needs-human-decision)",),
        },
    )
    monkeypatch.setattr(
        tick,
        "_wait_next_tick",
        lambda seconds, _stop_event, _wake_event: (
            waits.append(seconds) or (False, True)
        ),
    )

    with mock.patch("tick._notify_loop_quiescent_stop") as notify_stop:
        rc = tick._run_loop_locked(
            None,
            "claude",
            "",
            quiescent_stop_after=1,
        )

    assert rc == 0
    assert tick_calls == ["tick"]
    assert waits == []
    notify_stop.assert_called_once_with(
        ("pr #42 ai:reviewing (ai:needs-human-decision)",),
    )


def test_wait_next_tick_nudge_ends_sleep_early_without_stop() -> None:
    """A pending wake nudge ends the cadence sleep immediately.

    The full 60s cadence interval must not elapse; the caller learns the
    sleep did not run its full timer (``timer_expired`` False) and that no
    stop was requested.
    """
    import tick  # type: ignore[import-untyped]

    stop_event = threading.Event()
    wake_event = threading.Event()
    wake_event.set()

    with mock.patch.object(tick, "LOOP_NUDGE_FLOOR_SECONDS", 0):
        start = time.monotonic()
        stopped, timer_expired = tick._wait_next_tick(60, stop_event, wake_event)
        elapsed = time.monotonic() - start

    assert stopped is False
    assert timer_expired is False
    assert elapsed < 5


def test_advance_quiescent_counter_holds_steady_for_a_nudged_tick(
    tmp_path: Path,
) -> None:
    """A tick that did not follow a full heartbeat must not advance the file.

    The persisted counter is what the one-shot ``tick-cadence:`` line and the
    harness ``/loop`` path read, so it has to measure the same thing the
    in-process loop measures: elapsed heartbeat intervals, not ticks.
    """
    import tick  # type: ignore[import-untyped]

    counter = tmp_path / "quiescent-counter.txt"
    counter.write_text("1")

    with mock.patch.object(tick, "_QUIESCENT_COUNTER_FILE", str(counter)):
        prior = tick._advance_quiescent_counter(
            is_quiescent=True, count_heartbeat=False,
        )

    assert prior == 0
    assert counter.read_text() == "1"


def test_advance_quiescent_counter_advances_for_a_heartbeat_tick(
    tmp_path: Path,
) -> None:
    """A tick that followed a full heartbeat interval advances the file."""
    import tick  # type: ignore[import-untyped]

    counter = tmp_path / "quiescent-counter.txt"
    counter.write_text("1")

    with mock.patch.object(tick, "_QUIESCENT_COUNTER_FILE", str(counter)):
        prior = tick._advance_quiescent_counter(
            is_quiescent=True, count_heartbeat=True,
        )

    assert prior == 1
    assert counter.read_text() == "2"


@pytest.mark.parametrize("count_heartbeat", [True, False])
def test_advance_quiescent_counter_resets_when_not_quiescent(
    tmp_path: Path,
    count_heartbeat: bool,
) -> None:
    """Real work resets the streak regardless of what woke the tick."""
    import tick  # type: ignore[import-untyped]

    counter = tmp_path / "quiescent-counter.txt"
    counter.write_text("2")

    with mock.patch.object(tick, "_QUIESCENT_COUNTER_FILE", str(counter)):
        prior = tick._advance_quiescent_counter(
            is_quiescent=False, count_heartbeat=count_heartbeat,
        )

    assert prior == 0
    assert counter.read_text() == "0"


def test_run_tick_body_does_not_advance_counter_for_nudged_quiescent_tick(
    tmp_path: Path,
) -> None:
    """_run_tick_body must not advance the persisted counter for a nudged tick.

    Seam B of the quiescent-gate wire: _run_tick_body receives
    count_quiescent_heartbeat and must pass it to _advance_quiescent_counter as
    count_heartbeat.  Dropping or hardcoding it reinstates the round-1 blocker:
    three nudge-initiated quiescent ticks would advance the file to the stop
    threshold, making the next one-shot / harness /loop tick stop immediately.
    """
    import io

    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    counter = tmp_path / "quiescent-counter.txt"
    counter.write_text("1")
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    stderr = io.StringIO()

    with mock.patch("tick._QUIESCENT_COUNTER_FILE", str(counter)):
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    with mock.patch(
                                        "tick._compute_post_dispatch_state",
                                        return_value=(0, False, ()),
                                    ):
                                        with mock.patch(
                                            "tick._compute_nearest_deadline_s",
                                            return_value=None,
                                        ):
                                            with mock.patch("sys.stderr", stderr):
                                                rc = _run_tick_body(
                                                    tick_log, None, "claude", "",
                                                    count_quiescent_heartbeat=False,
                                                )

    assert rc == 0
    assert counter.read_text() == "1", (
        "nudged quiescent tick must not advance the persisted heartbeat counter"
    )
    assert "quiescent (heartbeat 1/3)" in stderr.getvalue(), (
        "cadence line must show counter unchanged (still heartbeat 1, not 2)"
    )


def test_run_loop_locked_marks_only_post_heartbeat_ticks_as_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop tells each tick whether it follows a full heartbeat interval.

    Without this the persisted counter counts ticks while the loop's own
    counter counts heartbeats: three nudged ticks would leave the file at the
    stop threshold, making the next one-shot tick (and the harness ``/loop``
    path that consumes its ``tick-cadence:`` line) stop with no quiet elapsed.
    """
    import tick  # type: ignore[import-untyped]

    heartbeat_flags: list[bool] = []

    def fake_tick(
        *_args: object, count_quiescent_heartbeat: bool = True,
        unattended: bool = False, **_flags: object,
    ) -> int:
        assert unattended
        heartbeat_flags.append(count_quiescent_heartbeat)
        return 0

    monkeypatch.setattr(tick, "_run_single_tick_locked", fake_tick)
    monkeypatch.setattr(
        tick,
        "_observe_loop_state",
        lambda _target=None, **_kwargs: {
            "in_flight_count": 0,
            "nearest_engine_deadline": None,
            "actionable_queue": False,
            "at_cap": False,
            "quiescent": True,
        },
    )
    # Startup tick, then a nudge wake, then a timer wake, then stop.
    waits = iter([(False, False), (False, True), (True, False)])
    monkeypatch.setattr(
        tick,
        "_wait_next_tick",
        lambda _seconds, _stop_event, _wake_event: next(waits),
    )
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_args: None)

    rc = tick._run_loop_locked(None, "claude", "", quiescent_stop_after=99)

    assert rc == 0
    # Tick 1 is the loop's own start; tick 2 followed a nudge; tick 3
    # followed a wait that ran its full scheduled interval.
    assert heartbeat_flags == [True, False, True]


def test_run_loop_locked_nudge_wakes_do_not_advance_quiescent_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only full-interval timer wakes count toward the quiescent-stop limit.

    The documented contract is "stop after N consecutive quiescent
    heartbeats" — roughly N heartbeat intervals of quiet. Nudge-initiated
    ticks that observe quiescence must leave the counter unchanged, or a
    spurious nudge burst would stop the loop in seconds instead.
    """
    import tick  # type: ignore[import-untyped]

    tick_calls: list[str] = []
    monkeypatch.setattr(
        tick,
        "_run_single_tick_locked",
        lambda *_args, **_kwargs: tick_calls.append("tick") or 0,
    )
    monkeypatch.setattr(
        tick,
        "_observe_loop_state",
        lambda _target=None, **_kwargs: {
            "in_flight_count": 0,
            "nearest_engine_deadline": None,
            "actionable_queue": False,
            "at_cap": False,
            "quiescent": True,
        },
    )
    # Two nudge wakes between the first and last tick, then a timer wake.
    waits = iter([(False, False), (False, False), (False, True)])
    monkeypatch.setattr(
        tick,
        "_wait_next_tick",
        lambda _seconds, _stop_event, _wake_event: next(waits),
    )

    with mock.patch("tick._notify_loop_quiescent_stop") as notify_stop:
        rc = tick._run_loop_locked(
            None,
            "claude",
            "",
            quiescent_stop_after=2,
        )

    # Tick 1 (startup, counts) + 2 nudge ticks (must not count) + the
    # timer-wake tick that reaches the limit.
    assert rc == 0
    assert len(tick_calls) == 4
    notify_stop.assert_called_once()


def test_run_single_tick_locked_forwards_count_quiescent_heartbeat(
    tmp_path: Path,
) -> None:
    """_run_single_tick_locked must forward its flag to _run_tick_body unchanged.

    The loop's heartbeat bookkeeping (tested above) is only meaningful if this
    hop passes the flag through rather than dropping it or hardcoding the
    default — otherwise every nudge-initiated tick would count as a full
    heartbeat and the loop could reach the quiescent-stop threshold in seconds
    instead of heartbeat-intervals of quiet.
    """
    import tick  # type: ignore[import-untyped]

    with mock.patch.object(tick, "TICK_LOG_DIR", str(tmp_path / ".tick-log")):
        with mock.patch("tick._run_tick_body", return_value=0) as run_tick_body:
            rc = tick._run_single_tick_locked(
                None, "claude", "", count_quiescent_heartbeat=False,
            )

    assert rc == 0
    run_tick_body.assert_called_once()
    assert run_tick_body.call_args.kwargs["count_quiescent_heartbeat"] is False


@pytest.mark.parametrize("artifact", ["review.json", "completion.json"])
def test_scan_finished_engines_nudges_once_on_reviewer_artifacts(
    tmp_path: Path,
    artifact: str,
) -> None:
    """Wrapper-published reviewer artifacts wake the loop exactly once.

    The reviewer wrapper publishes both by atomic rename after the engine
    exits, so either is terminal evidence even while the PID lingers. A
    marker that stays unharvested must not re-nudge on every watcher poll.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-468"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "d-review", "stage": "review", "pid": os.getpid(),
    }))
    (wt_dir / artifact).write_text('{"ok": true}')

    seen: set[str] = set()
    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch("process_control.pid_is_alive", return_value=True):
            assert tick._scan_finished_engines(seen) is True
            assert tick._scan_finished_engines(seen) is False

    assert seen == {"d-review"}


@pytest.mark.parametrize(
    ("wt_name", "stage"), [("issue-439", "implement"), ("pr-470", "fix")],
)
def test_scan_finished_engines_nudges_on_dead_marker_pid(
    tmp_path: Path,
    wt_name: str,
    stage: str,
) -> None:
    """Wrapperless engines nudge on process death, for both their stages."""
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / wt_name
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": f"d-{stage}", "stage": stage, "pid": 4242,
    }))

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch("process_control.pid_is_alive", return_value=False):
            assert tick._scan_finished_engines(set()) is True


def test_scan_finished_engines_ignores_result_of_live_engine(
    tmp_path: Path,
) -> None:
    """A non-empty result.json alone is not completion.

    Implementer/fixer ``result.json`` is a stdout redirect written in place
    with no atomic rename, so a read can catch a partial write and it never
    proves the engine exited. Nudging on it would also spend the single
    per-dispatch nudge before harvest could act, leaving real completion to
    wait for the next scheduled poll.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-439"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "d-impl", "stage": "implement", "pid": 4242,
    }))
    (wt_dir / "result.json").write_text('{"partial": ')

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch("process_control.pid_is_alive", return_value=True):
            assert tick._scan_finished_engines(set()) is False


def test_scan_finished_engines_ignores_live_and_launching_markers(
    tmp_path: Path,
) -> None:
    """A working engine and a not-yet-spawned one produce no nudge."""
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    live = wt_root / "issue-439"
    live.mkdir(parents=True)
    (live / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "d-live", "pid": 4242,
    }))
    launching = wt_root / "pr-468"
    launching.mkdir(parents=True)
    (launching / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "d-launching", "state": "launching",
    }))

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch("process_control.pid_is_alive", return_value=True):
            assert tick._scan_finished_engines(set()) is False


@pytest.mark.parametrize("bad_pid", [0, -1])
def test_engine_looks_finished_never_probes_a_non_positive_pid(
    tmp_path: Path,
    bad_pid: int,
) -> None:
    """Only positive PIDs are probed, matching harvest's liveness guard.

    ``os.kill(0, 0)`` targets the caller's own process group and
    ``os.kill(-N, 0)`` targets group N, so probing a malformed marker asks
    about the wrong process entirely. ``_classify_marker`` already refuses
    these; the watcher must agree or the two liveness views can diverge.
    """
    import tick  # type: ignore[import-untyped]

    marker = {"dispatch_id": "d-bad", "stage": "implement", "pid": bad_pid}

    with mock.patch("process_control.pid_is_alive") as pid_is_alive:
        assert tick._engine_looks_finished(str(tmp_path), marker) is False

    pid_is_alive.assert_not_called()


def test_engine_looks_finished_keeps_custody_while_process_group_alive(
    tmp_path: Path,
) -> None:
    """A dead wrapper is not completion while its paid child group is alive.

    Reviewer markers record the wrapper as both PID and PGID. If that wrapper
    crashes, its Codex child can continue in the inherited process group.
    Nudging on wrapper death would make HARVEST release custody and let
    DISPATCH launch a second paid engine while the first is still running.
    """
    import tick  # type: ignore[import-untyped]

    marker = {
        "dispatch_id": "d-review",
        "stage": "review",
        "pid": 4242,
        "pgid": 4242,
    }

    with mock.patch("process_control.pid_is_alive", return_value=False):
        with mock.patch(
            "process_control.pgid_is_alive", return_value=True,
        ) as group_alive:
            finished = tick._engine_looks_finished(str(tmp_path), marker)

    assert finished is False
    group_alive.assert_called_once_with(4242)


def test_scan_finished_engines_skips_unreadable_marker(tmp_path: Path) -> None:
    """A corrupt marker is not the watcher's business — no nudge, no raise.

    Custody of an unclassifiable marker belongs to dispatch/harvest; the
    advisory watcher must neither act on it nor die because of it.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-468"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text("{ truncated")
    (wt_dir / "review.json").write_text('{"ok": true}')

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        assert tick._scan_finished_engines(set()) is False


def test_engine_stall_threshold_clears_the_longest_legitimate_silence() -> None:
    """The threshold must have real margin over normal engine quiet.

    Engines emit stream events only at message boundaries, so heartbeat
    silence is exactly tool-call duration, and `Bash(uv run pytest *)` is
    allowlisted against a suite that already outruns the harness's 120s
    default. A threshold merely *equal* to the harness's maximum tool-call
    duration reports a healthy engine on its longest legal call — a signal the
    operator learns to distrust, which is the failure this whole feature
    exists to avoid.
    """
    import tick  # type: ignore[import-untyped]

    assert (
        tick.ENGINE_STALL_SECONDS
        >= 2 * tick.MAX_LEGITIMATE_ENGINE_SILENCE_SECONDS
    ), "threshold must clear the longest legal tool call by more than rounding"


def test_engine_stall_clock_leaves_lead_time_before_every_deadline() -> None:
    """A stall must be reportable while there is still time to act on it.

    The slowest path to a verdict is the no-heartbeat fallback, which pays the
    startup grace on top of the threshold. If that sum reached the engine's
    timeout the stall would never be reported before ``hung`` escalated, and
    the signal would be strictly redundant.
    """
    import tick  # type: ignore[import-untyped]

    slowest_verdict = (
        tick.ENGINE_STALL_SECONDS + tick.ENGINE_STALL_STARTUP_GRACE_SECONDS
    )
    assert slowest_verdict < tick.IMPLEMENTER_TIMEOUT
    assert slowest_verdict < tick.FIXER_TIMEOUT


@pytest.mark.parametrize("heartbeat, recorded, threshold", [
    (True, {"stall_seconds": 900, "stall_startup_grace_seconds": 60}, 900),
    (False, {"stall_seconds": 900, "stall_startup_grace_seconds": 60}, 960),
    # A marker written before the limits were recorded keeps the shipped ones.
    (True, {}, 30 * 60),
    (False, {}, 35 * 60),
])
def test_engine_progress_uses_the_stall_limits_the_marker_records(
    tmp_path: Path, heartbeat: bool, recorded: dict[str, int], threshold: int,
) -> None:
    """Stall detection judges an engine by the limits it was dispatched under.

    The marker is the one record every detector reads — the tick, the
    ``--loop`` watcher thread, and the status dashboard's separate process —
    so a configured threshold reaches all three without any of them resolving
    configuration again.
    """
    import tick  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-5"
    wt_dir.mkdir()
    if heartbeat:
        Path(tick._heartbeat_path(str(wt_dir))).write_text("")
    marker = {"stage": "implement", "start_time": 100.0, **recorded}

    progress = tick._engine_progress(str(wt_dir), marker)

    assert progress is not None
    assert progress.threshold == threshold


@pytest.mark.parametrize(
    ("stage", "wt_name"), [("implement", "issue-577"), ("fix", "pr-577")],
)
def test_scan_stalled_engines_reports_a_live_engine_gone_quiet_once(
    tmp_path: Path,
    stage: str,
    wt_name: str,
) -> None:
    """A live engine whose heartbeat stopped advancing is one stall episode.

    Liveness and progress are different signals: this engine passes every
    ``kill -0`` probe and is still inside its deadline, so nothing else in the
    pipeline can tell it is wedged. Repeating the report on every 5s poll would
    bury the operator, so an uninterrupted scan records the report and later
    polls stay quiet while the heartbeat remains unchanged.

    Both wrapperless stages are driven. Symmetry at the *launch* site is
    structural — one helper emits ``--heartbeat`` for both — but the classifier
    names its stages in a literal tuple, so nothing else stops that tuple
    losing ``"fix"`` with a green suite and every fixer stall going unreported.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / wt_name
    wt_dir.mkdir(parents=True)
    now = time.time()
    quiet = stalled_quiet_seconds(tick)
    write_stalled_marker(
        wt_dir,
        start_time=now - quiet - 10 * 60,
        heartbeat_age=quiet,
        stage=stage,
    )

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(tick, "_report_engine_stalled") as report:
            with mock.patch(
                "process_control.get_process_start_time",
                return_value=now - quiet - 10 * 60,
            ):
                assert tick._scan_stalled_engines() is True
                assert tick._scan_stalled_engines() is False

    report.assert_called_once()


def test_scan_stalled_engines_persists_a_reported_episode_before_a_later_failure(
    tmp_path: Path,
) -> None:
    """A later marker failure must not re-arm an already reported stall.

    The episode memory exists to deduplicate one quiet live engine across
    polls and across one-shot ticks. That guarantee is not limited to the
    happy path: if a later marker crashes classification in the same scan, the
    already reported episode still has to be durably checkpointed first, or
    the next poll reports the same wedge again and turns one real signal into
    a 5-second alert storm.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    reported_dir = wt_root / "issue-577"
    reported_dir.mkdir(parents=True)
    crashing_dir = wt_root / "issue-578"
    crashing_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(
        reported_dir,
        start_time=start,
        heartbeat_age=quiet,
        dispatch_id="d-reported",
    )
    write_stalled_marker(
        crashing_dir,
        start_time=start,
        heartbeat_age=quiet,
        dispatch_id="d-crash",
    )
    reported_marker = json.loads((reported_dir / "dispatch.json").read_text())
    crashing_marker = json.loads((crashing_dir / "dispatch.json").read_text())
    markers = [
        (str(reported_dir), reported_marker),
        (str(crashing_dir), crashing_marker),
    ]
    episodes = wt_root / ".tick-state" / "stall-episodes.json"
    seen_failures: set[str] = set()

    def identity_or_crash(_wt_dir: str, marker: dict[str, object]) -> str | None:
        dispatch_id = marker.get("dispatch_id")
        if dispatch_id == "d-reported":
            return "pid-identity"
        if dispatch_id == "d-crash":
            raise RuntimeError("later marker exploded")
        return None

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)), \
            mock.patch.object(tick, "_STALL_EPISODES_FILE", str(episodes)), \
            mock.patch.object(
                tick, "_STALL_EPISODES_LOCK", str(wt_root / ".tick-state" / "stall.lock"),
            ), \
            mock.patch.object(
                tick,
                "_iter_dispatch_markers",
                side_effect=lambda: iter(markers),
            ), \
            mock.patch.object(
                tick, "_stalled_engine_identity", side_effect=identity_or_crash,
            ), \
            mock.patch.object(tick, "_report_engine_stalled") as report:
        assert tick._scan_stalled_engines_safely(seen_failures) is False
        assert stall_episode_ids(tick) == {"d-reported"}
        assert tick._scan_stalled_engines_safely(seen_failures) is False

    report.assert_called_once()


def test_scan_stalled_engines_retries_after_exit_during_report(
    tmp_path: Path,
) -> None:
    """Process exit before the checkpoint must leave a real stall retryable.

    The durable episode means the report attempt reached its post-report
    checkpoint, not merely that reporting began. If the process exits before
    that checkpoint, the next process must retry instead of treating the stall
    as already recorded.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)
    episodes = wt_root / ".tick-state" / "stall-episodes.json"

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)), \
            mock.patch.object(tick, "_STALL_EPISODES_FILE", str(episodes)), \
            mock.patch.object(
                tick, "_STALL_EPISODES_LOCK", str(wt_root / ".tick-state" / "stall.lock"),
            ), \
            mock.patch(
                "process_control.get_process_start_time", return_value=start,
            ), \
            mock.patch.object(
                tick,
                "_report_engine_stalled",
                side_effect=[SystemExit("process exited"), None],
            ) as report:
        with pytest.raises(SystemExit, match="process exited"):
            tick._scan_stalled_engines()

        assert stall_episode_ids(tick) == set()
        assert tick._scan_stalled_engines() is True
        assert stall_episode_ids(tick) == {"d-stalled"}

    assert report.call_count == 2


@pytest.mark.parametrize(
    ("offset_seconds", "stalled"), [(-2 * 60, False), (5 * 60, True)],
)
def test_scan_stalled_engines_gives_a_missing_heartbeat_a_startup_grace(
    tmp_path: Path,
    offset_seconds: float,
    stalled: bool,
) -> None:
    """No heartbeat file is not evidence of a stall.

    It means either the engine has not reached its first output event yet, or
    the marker was dispatched by a wrapper from before heartbeats existed.
    Both fall back to the dispatch clock, and both get the startup grace on top
    of the threshold — silence just inside that sum is still working, just past
    it is not. The ages straddle the boundary rather than naming it: the
    threshold has to keep margin over a legitimate long tool call, so its value
    moves, and hardcoded minutes would silently stop testing this boundary.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    fallback_threshold = (
        tick.ENGINE_STALL_SECONDS + tick.ENGINE_STALL_STARTUP_GRACE_SECONDS
    )
    start = time.time() - fallback_threshold - offset_seconds
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=None)

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(tick, "_report_engine_stalled") as report:
            with mock.patch(
                "process_control.get_process_start_time", return_value=start,
            ):
                assert tick._scan_stalled_engines() is stalled

    assert report.call_count == (1 if stalled else 0)


def test_scan_stalled_engines_re_arms_when_the_heartbeat_resumes(
    tmp_path: Path,
) -> None:
    """A recovered engine that stalls again is a second episode, not silence.

    Deduplicating on the dispatch identity alone would report a wedged engine
    once per loop process and then go quiet forever — an engine that hangs,
    recovers, and hangs again would show the operator only the first stall.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)
    heartbeat = wt_dir / "heartbeat"

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(tick, "_report_engine_stalled") as report:
            with mock.patch(
                "process_control.get_process_start_time", return_value=start,
            ):
                assert tick._scan_stalled_engines() is True
                # The engine produces output again: detection must re-arm.
                os.utime(heartbeat, None)
                assert tick._scan_stalled_engines() is False
                assert stall_episode_ids(tick) == set()
                # ...and then goes quiet a second time, at a newer timestamp:
                # past the threshold again, but not the one already reported.
                again = time.time() - tick.ENGINE_STALL_SECONDS - 60
                os.utime(heartbeat, (again, again))
                assert tick._scan_stalled_engines() is True

    assert report.call_count == 2


def test_scan_stalled_engines_forgets_dispatches_that_no_longer_exist(
    tmp_path: Path,
) -> None:
    """Episode memory is bounded by what is dispatched right now.

    A reported episode is only re-armed by a marker the scan still sees, so a
    harvested dispatch would otherwise leave its entry behind forever — one
    per stalled dispatch, in a file that now outlives the process that wrote
    it. That sweep is the whole reason the memory needs no pruning rules of
    its own.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(tick, "_report_engine_stalled"):
            with mock.patch(
                "process_control.get_process_start_time", return_value=start,
            ):
                assert tick._scan_stalled_engines() is True
                assert stall_episode_ids(tick) == {"d-stalled"}
                # Harvest cleans the worktree up; the dispatch is over.
                (wt_dir / "dispatch.json").unlink()
                assert tick._scan_stalled_engines() is False

    assert stall_episode_ids(tick) == set()


def test_scan_stalled_engines_skips_the_reviewer_stage(tmp_path: Path) -> None:
    """The reviewer has no progress signal, so it gets no stall verdict.

    Its wrapper discards engine output, so there is no heartbeat to read and
    the dispatch-clock fallback would classify every long review as stalled.
    Its stall cost is already bounded by the 20-minute review timeout; a weaker
    proxy is deliberately deferred rather than invented here.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-577"
    wt_dir.mkdir(parents=True)
    start = time.time() - 40 * 60
    write_stalled_marker(
        wt_dir, start_time=start, heartbeat_age=None, stage="review",
    )

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(tick, "_report_engine_stalled") as report:
            assert tick._scan_stalled_engines() is False

    report.assert_not_called()


def test_scan_stalled_engines_ignores_a_marker_whose_pid_was_reused(
    tmp_path: Path,
) -> None:
    """A silent heartbeat plus a live-looking PID is not enough.

    ``kill -0`` succeeds for whatever process inherited the number, so a bare
    liveness probe would report a permanent stall for an engine that is simply
    dead. Cadence must use the identity proof harvest trusts, or a finished
    dispatch produces a stall alert on every loop.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(tick, "_report_engine_stalled") as report:
            # A different process now holds the PID: start times disagree.
            with mock.patch(
                "process_control.get_process_start_time", return_value=time.time(),
            ):
                assert tick._scan_stalled_engines() is False

    report.assert_not_called()


def test_scan_stalled_engines_ignores_an_engine_past_its_deadline(
    tmp_path: Path,
) -> None:
    """An overdue engine is the timeout path's business, not the stall path's.

    It classifies ``hung``, which already escalates to a human. Reporting it as
    a stall too would double-notify on the one case the pipeline handles.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - tick.IMPLEMENTER_TIMEOUT - 10 * 60
    write_stalled_marker(
        wt_dir,
        start_time=start,
        heartbeat_age=quiet,
        time_to_deadline=-10 * 60,
    )

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(tick, "_report_engine_stalled") as report:
            with mock.patch(
                "process_control.get_process_start_time", return_value=start,
            ):
                assert tick._scan_stalled_engines() is False

    report.assert_not_called()


def test_scan_stalled_engines_probes_a_refuted_marker_only_once(
    tmp_path: Path,
) -> None:
    """A refuted stall verdict is remembered, not re-derived every poll.

    The identity probe shells out to ``ps``. A quiet marker whose engine is
    *not* live never gets an episode entry unless the refuted verdict is
    remembered too, so it would be re-probed on every 5s poll for as long as
    the marker exists — and ``retained_scratch`` is a directory the pipeline
    deliberately keeps across ticks for the operator, so "as long as it
    exists" can mean the whole life of the loop process.

    Every classification reachable here is terminal for a fixed progress
    timestamp — ``stale``/``hung``/``complete``/``failed`` never become
    ``live``, and ``launching`` does not either, because reconciliation fails
    closed to a human on a lease rather than re-dispatching it — so
    remembering the refusal cannot suppress a real stall. Fresh progress still
    re-arms the marker, which the resumption test covers.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    write_stalled_marker(
        wt_dir, start_time=time.time() - quiet - 10 * 60, heartbeat_age=quiet,
    )
    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(tick, "_report_engine_stalled") as report:
            # The PID was inherited by another process: liveness is refuted.
            with mock.patch(
                "process_control.get_process_start_time",
                return_value=time.time(),
            ) as probe:
                for _ in range(20):
                    assert tick._scan_stalled_engines() is False

    assert probe.call_count == 1, (
        "the refuted verdict must be remembered, not re-derived per poll"
    )
    report.assert_not_called()


def test_engine_progress_report_marks_a_proven_live_quiet_engine_stalled(
    tmp_path: Path,
) -> None:
    """The dashboard's verdict comes from the same rule the watcher applies."""
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)

    with mock.patch(
        "process_control.get_process_start_time", return_value=start,
    ):
        rows = tick.engine_progress_report(str(wt_root))

    assert rows == [("issue-577", f"STALLED-{int(quiet // 60)}m")]


def test_engine_progress_report_withholds_the_stall_verdict_from_a_reused_pid(
    tmp_path: Path,
) -> None:
    """A reused PID must not render STALLED on the dashboard either.

    ``kill -0`` succeeds for whatever process inherited the number, so a
    dashboard trusting bare liveness calls a finished engine permanently
    stalled while ``_scan_stalled_engines`` correctly reports nothing — the
    operator then sees an alarm the tick log never confirms.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)

    with mock.patch(
        "process_control.get_process_start_time", return_value=time.time(),
    ):
        rows = tick.engine_progress_report(str(wt_root))

    assert rows == [("issue-577", f"quiet-{int(quiet // 60)}m")]


def test_engine_progress_report_withholds_the_stall_verdict_past_the_deadline(
    tmp_path: Path,
) -> None:
    """An overdue engine classifies ``hung``, which already escalates.

    The watcher declines it for that reason; a dashboard that still says
    STALLED double-reports the one case the pipeline already handles.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - tick.IMPLEMENTER_TIMEOUT - 10 * 60
    write_stalled_marker(
        wt_dir,
        start_time=start,
        heartbeat_age=quiet,
        time_to_deadline=-10 * 60,
    )

    with mock.patch(
        "process_control.get_process_start_time", return_value=start,
    ):
        rows = tick.engine_progress_report(str(wt_root))

    assert rows == [("issue-577", f"quiet-{int(quiet // 60)}m")]


def test_engine_progress_report_gives_the_reviewer_stage_no_progress_contract(
    tmp_path: Path,
) -> None:
    """The reviewer wrapper discards engine output, so there is nothing to read."""
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-577"
    wt_dir.mkdir(parents=True)
    write_stalled_marker(
        wt_dir,
        start_time=time.time() - 40 * 60,
        heartbeat_age=None,
        stage="review",
    )

    rows = tick.engine_progress_report(str(wt_root))

    assert rows == [("pr-577", "-")]


def test_engine_progress_cli_prints_one_row_per_marker(tmp_path: Path) -> None:
    """The dashboard's real entry point: a subcommand on the real tick.py.

    ``status.sh`` shells out to this, so the argv contract and the tab-separated
    output shape are production surface. Exercising the module function alone
    would leave the command itself unproven.
    """
    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    write_stalled_marker(
        wt_dir, start_time=time.time() - 40 * 60, heartbeat_age=8 * 60,
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("tick.py")),
            "engine-progress",
            "--wt-root",
            str(wt_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "issue-577\tquiet-8m\n"


def test_report_engine_stalled_logs_the_operator_facing_facts(
    tmp_path: Path,
) -> None:
    """One log entry and one notification, carrying enough to triage.

    A stall entry that only says "stalled" sends the operator back to the
    worktree to work out which engine, how long, and whether there is still
    time to act before the deadline fires.
    """
    import tick  # type: ignore[import-untyped]

    log_dir = tmp_path / ".tick-log"
    marker = {
        "dispatch_id": "d-impl",
        "stage": "implement",
        "pr_number": 577,
        "engine": "claude",
        "deadline": time.time() + 25 * 60,
    }

    with mock.patch.object(tick, "TICK_LOG_DIR", str(log_dir)):
        with mock.patch.object(tick, "_emit_operator_notification") as notify:
            tick._report_engine_stalled(
                marker=marker, age=18 * 60, source="heartbeat",
                identity="matched",
            )

    entries = [
        json.loads(line)
        for line in sorted(log_dir.glob("*.jsonl"))[0].read_text().splitlines()
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "engine_stalled"
    assert entry["pr"] == 577
    assert entry["stage"] == "implement"
    assert entry["dispatch_id"] == "d-impl"
    assert entry["details"]["progress_age_seconds"] == 18 * 60
    assert entry["details"]["progress_source"] == "heartbeat"
    assert entry["details"]["identity"] == "matched"
    assert 24 * 60 <= entry["details"]["seconds_to_deadline"] <= 25 * 60
    notify.assert_called_once()
    assert "18m" in notify.call_args.kwargs["message"]


def test_terminal_transition_cannot_send_a_desktop_notification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The shared fixture must isolate desktop delivery, not logs or labels."""
    import tick

    tick_log = tick.TickLog.open(str(tmp_path))
    label_reads = [
        {"labels": [{"name": "ai:implementing"}]},
        {"labels": [{"name": "ai:needs-human-decision"}]},
    ]
    with (
        mock.patch("tick.platform.system", return_value="Darwin"),
        mock.patch("tick.gh_json", side_effect=label_reads),
        mock.patch("tick.gh") as gh,
        mock.patch(
            "subprocess.Popen",
            side_effect=AssertionError("test attempted real desktop delivery"),
        ) as spawn,
    ):
        assert tick._converge_pipeline_labels(
            "issue", 7, "ai:needs-human-decision",
            tick_log=tick_log, phase="harvest", stage="implement",
        )

    spawn.assert_not_called()
    gh.assert_called_once_with(
        "issue", "edit", "7", "--add-label", "ai:needs-human-decision",
        "--remove-label", "ai:implementing",
    )
    reason = "issue #7 reached terminal label ai:needs-human-decision"
    assert reason in capsys.readouterr().err
    notifications = [
        entry for entry in tick_log.tail()
        if entry["action"] == "notify_terminal_label"
    ]
    assert len(notifications) == 1
    assert notifications[0]["reason"] == reason


@pytest.mark.parametrize("error", [None, OSError("notifier unavailable")])
def test_emit_operator_notification_preserves_real_delivery_contract(
    isolate_operator_notifications: Callable[..., None],
    error: OSError | None,
) -> None:
    """Only pytest's default delivery is muted; the real emitter still works."""
    import tick

    with (
        mock.patch("tick.platform.system", return_value="Darwin"),
        mock.patch("subprocess.run", side_effect=error) as run,
    ):
        isolate_operator_notifications(
            title='Pipeline "ready"', message='Issue #7 needs "review"',
        )

    run.assert_called_once_with(
        [
            "osascript", "-e",
            r'display notification "Issue #7 needs \"review\"" '
            r'with title "Pipeline \"ready\""',
        ],
        capture_output=True, check=False,
        timeout=tick.OPERATOR_NOTIFICATION_TIMEOUT_SECONDS,
    )


def test_emit_operator_notification_is_inert_off_macos(
    isolate_operator_notifications: Callable[..., None],
) -> None:
    with (
        mock.patch("tick.platform.system", return_value="Linux"),
        mock.patch("subprocess.run") as run,
    ):
        isolate_operator_notifications(title="t", message="m")

    run.assert_not_called()


def test_emit_operator_notification_cannot_block_its_caller(
    isolate_operator_notifications: Callable[..., None],
) -> None:
    """A wedged notifier must not take the wake watcher down with it.

    Stall reporting made this reachable from the watcher thread for the first
    time. ``osascript`` is a subprocess, and an unbounded ``subprocess.run``
    that never returns would park the watcher mid-poll — no further stall
    scans, no completion nudges, no wake-request consumption for the life of
    the loop process. ``except OSError`` cannot catch a hang, so the bound has
    to be a timeout, and expiry must be swallowed like every other
    best-effort notification failure.
    """
    import tick  # type: ignore[import-untyped]

    with (
        mock.patch("tick.platform.system", return_value="Darwin"),
        mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=1),
        ) as run,
    ):
        isolate_operator_notifications(title="t", message="m")

    run.assert_called_once_with(
        ["osascript", "-e", 'display notification "m" with title "t"'],
        capture_output=True, check=False,
        timeout=tick.OPERATOR_NOTIFICATION_TIMEOUT_SECONDS,
    )
    assert tick.OPERATOR_NOTIFICATION_TIMEOUT_SECONDS > 0


def test_wake_watcher_loop_wakes_and_reports_a_stalled_engine(
    tmp_path: Path,
) -> None:
    """The watcher's own loop must consume the stall scan, not just own it.

    Every other stall test drives ``_scan_stalled_engines`` directly, so
    without this nothing proves the running watcher thread reaches the stall
    check or that its verdict reaches ``wake_event``.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)

    stop_event = threading.Event()
    wake_event = threading.Event()

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(
            tick, "WAKE_REQUEST_PATH", str(tmp_path / "state" / "wake-request"),
        ):
            with mock.patch.object(tick, "WAKE_WATCH_POLL_SECONDS", 0.01):
                with mock.patch.object(tick, "_report_engine_stalled") as report:
                    # Alive by every probe: the stall scan is the only thing
                    # that can wake this loop.
                    with mock.patch(
                        "process_control.pid_is_alive", return_value=True,
                    ), mock.patch(
                        "process_control.get_process_start_time",
                        return_value=start,
                    ):
                        watcher = threading.Thread(
                            target=tick._wake_watcher_loop,
                            args=(stop_event, wake_event),
                            daemon=True,
                        )
                        watcher.start()
                        try:
                            woke = wake_event.wait(10)
                        finally:
                            stop_event.set()
                            watcher.join(timeout=10)

    assert woke is True
    report.assert_called()


def test_one_shot_ticks_report_a_stall_once_across_separate_runs(
    tmp_path: Path,
) -> None:
    """Completed one-shot runs must de-duplicate the same stall episode.

    ``SKILL.md`` schedules each iteration as a *one-shot* ``tick.py`` process
    from the emitted ``tick-cadence:`` line, so the ``--loop`` wake watcher —
    the only other caller of the stall scan — never runs there. A detector
    that fires only under ``--loop`` is inert in the mode the operator
    actually runs.

    Two sequential ``main()`` calls stand in for two scheduled iterations.
    Nothing is carried between them but the filesystem, which is the whole
    claim: after a run reaches the post-report checkpoint, episode memory has
    to survive that process's normal exit, or a wedge would re-report on every
    tick for the ~30 minutes left on its deadline and bury the operator in
    duplicate notifications. The interrupted-checkpoint test covers the
    deliberate retry window separately.

    Assertions read the entries the tick actually wrote rather than a
    re-typed expectation, so a report that lands with the wrong action or
    without its identity/deadline detail fails here.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)

    log_dir = wt_root / ".tick-log"
    episodes = wt_root / ".tick-state" / "stall-episodes.json"

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)), \
            mock.patch.object(tick, "TICK_LOG_DIR", str(log_dir)), \
            mock.patch.object(tick, "TICK_LOCK_PATH", str(wt_root / ".tick.lock")), \
            mock.patch.object(tick, "_STALL_EPISODES_FILE", str(episodes)), \
            mock.patch.object(tick, "repo_root", return_value=str(tmp_path)), \
            mock.patch.object(tick, "_startup_probe", return_value={
                "gh_found": True, "git_found": True, "codex_found": True,
            }), \
            mock.patch.object(tick, "_emit_operator_notification"), \
            mock.patch.object(tick, "harvest_reviewers", return_value=True), \
            mock.patch.object(tick, "harvest_implementers", return_value=True), \
            mock.patch.object(tick, "harvest_fixers", return_value=True), \
            mock.patch.object(
                tick, "read_authoritative_in_flight_labels", return_value=[],
            ), \
            mock.patch.object(tick, "dispatch_reviewers"), \
            mock.patch.object(tick, "dispatch_implementers"), \
            mock.patch.object(tick, "dispatch_fixers"), \
            mock.patch("process_control.pid_is_alive", return_value=True), \
            mock.patch(
                "process_control.get_process_start_time", return_value=start,
            ):
        assert tick.main() == 0
        assert tick.main() == 0

    entries = read_tick_log_entries(log_dir)
    stalls = [e for e in entries if e.get("action") == "engine_stalled"]
    assert len(stalls) == 1, (
        f"Expected one engine_stalled entry across two one-shot ticks, "
        f"got {len(stalls)}: {stalls}"
    )
    assert stalls[0]["pr"] == 577
    assert "d-stalled" in episodes.read_text()


def test_consume_wake_request_takes_file_once(tmp_path: Path) -> None:
    """The wake-request file nudges exactly once and is consumed.

    Consuming by unlink is what makes repeated touches coalesce into one
    wake instead of queueing extra ticks.
    """
    import tick  # type: ignore[import-untyped]

    request = tmp_path / "wake-request"
    request.write_text("")

    with mock.patch.object(tick, "WAKE_REQUEST_PATH", str(request)):
        assert tick._consume_wake_request() is True
        assert tick._consume_wake_request() is False

    assert not request.exists()


def test_start_wake_watcher_wakes_the_loop_on_a_finished_engine(
    tmp_path: Path,
) -> None:
    """The whole accelerator path: thread start → scan → wake channel set.

    Every other wake test drives a piece directly (``_scan_finished_engines``
    with a hand-built marker, ``_wait_next_tick`` with a pre-set event), so
    without this nothing proves the started thread ever reaches
    ``wake_event.set()`` — the one behaviour the feature exists for.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-439"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "d-impl", "stage": "implement", "pid": 4242,
    }))

    stop_event = threading.Event()
    wake_event = threading.Event()

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)):
        with mock.patch.object(
            tick, "WAKE_REQUEST_PATH", str(tmp_path / "state" / "wake-request"),
        ):
            with mock.patch.object(tick, "WAKE_WATCH_POLL_SECONDS", 0.01):
                with mock.patch("process_control.pid_is_alive", return_value=False):
                    watcher = tick._start_wake_watcher(stop_event, wake_event)
                    assert watcher is not None
                    try:
                        woke = wake_event.wait(10)
                    finally:
                        stop_event.set()
                        watcher.join(timeout=10)

    assert woke is True
    assert watcher.is_alive() is False


def test_a_stall_scan_already_in_flight_is_not_run_twice(tmp_path: Path) -> None:
    """Two callers must not both report the same episode.

    Under ``--loop`` the wake watcher and the tick body's own pre-HARVEST scan
    run concurrently, and the episode memory is a read-modify-write on a shared
    file. Without serialization both can read the same empty map, both pass the
    already-evaluated gate, and both report — two log entries and two operator
    notifications for one stall, which is exactly what the episode memory
    exists to prevent.

    Holding the lock stands in for the other caller being mid-scan: whichever
    caller loses skips this pass entirely rather than waiting, because the
    watcher must never park and the work is about to be done by the winner
    anyway.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-577"
    wt_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds(tick)
    start = time.time() - quiet - 10 * 60
    write_stalled_marker(wt_dir, start_time=start, heartbeat_age=quiet)

    other_caller = tick.TickLock(tick._STALL_EPISODES_LOCK)
    assert other_caller.acquire() is True

    with mock.patch.object(tick, "WT_ROOT", str(wt_root)), \
            mock.patch.object(tick, "_report_engine_stalled") as report, \
            mock.patch(
                "process_control.get_process_start_time", return_value=start,
            ):
        assert tick._scan_stalled_engines_safely(set()) is False
        report.assert_not_called()

        # The other caller finishes; this one is free to do the work.
        other_caller.release()
        assert tick._scan_stalled_engines_safely(set()) is True

    report.assert_called_once()


def test_a_failing_stall_scan_does_not_crash_the_tick(tmp_path: Path) -> None:
    """The tick-side caller needs the same guard the watcher-side one has.

    Stall reporting is advisory detection bolted onto the front of a tick that
    harvests and dispatches real work. Unguarded, one marker that makes
    ``_classify_marker`` raise would turn every tick into exit code 4 — the
    detector taking down the pipeline it was added to watch.
    """
    import tick  # type: ignore[import-untyped]

    with mock.patch.object(tick, "repo_root", return_value=str(tmp_path)), \
            mock.patch.object(tick, "_startup_probe", return_value={
                "gh_found": True, "git_found": True, "codex_found": True,
            }), \
            mock.patch.object(
                tick, "_scan_stalled_engines", side_effect=RuntimeError("boom"),
            ), \
            mock.patch.object(tick, "harvest_reviewers", return_value=True), \
            mock.patch.object(tick, "harvest_implementers", return_value=True), \
            mock.patch.object(tick, "harvest_fixers", return_value=True), \
            mock.patch.object(
                tick, "read_authoritative_in_flight_labels", return_value=[],
            ), \
            mock.patch.object(tick, "dispatch_reviewers"), \
            mock.patch.object(tick, "dispatch_implementers"), \
            mock.patch.object(tick, "dispatch_fixers"):
        assert tick.main() == 0

    entries = read_tick_log_entries(Path(tick.TICK_LOG_DIR))
    actions = [e.get("action") for e in entries]
    assert "tick_crash" not in actions
    assert actions.count("stall_scan_failed") == 1


def test_a_failing_stall_scan_leaves_the_older_wake_sources_running(
    tmp_path: Path,
) -> None:
    """A hostile marker must not take the two sources that predate it down.

    The stall scan is the newest wake source and the only one that reaches
    ``_classify_marker`` on arbitrary marker input — which the rest of this
    file already treats as capable of raising. Unguarded, one such raise ends
    the watcher, and with it the completion nudges and wake-request
    consumption that shipped before stall detection existed.

    The wake here can only have come from the wake-request file, because the
    stall scan raises on every poll and no marker exists for the completion
    scan to find.
    """
    import tick  # type: ignore[import-untyped]

    request = tmp_path / "state" / "wake-request"
    request.parent.mkdir(parents=True)
    request.write_text("")

    stop_event = threading.Event()
    wake_event = threading.Event()

    with mock.patch.object(tick, "WT_ROOT", str(tmp_path / "empty")), \
            mock.patch.object(tick, "WAKE_REQUEST_PATH", str(request)), \
            mock.patch.object(tick, "WAKE_WATCH_POLL_SECONDS", 0.01), \
            mock.patch.object(
                tick, "_scan_stalled_engines", side_effect=RuntimeError("boom"),
            ), \
            mock.patch.object(tick, "_log_stall_scan_failed") as log_failed, \
            mock.patch.object(tick, "_log_wake_watcher_failed") as watcher_failed:
        watcher = threading.Thread(
            target=tick._wake_watcher_loop,
            args=(stop_event, wake_event),
            daemon=True,
        )
        watcher.start()
        try:
            woke = wake_event.wait(10)
            # Give the loop several more polls, so a per-poll log would show.
            time.sleep(0.2)
        finally:
            stop_event.set()
            watcher.join(timeout=10)

    assert woke is True
    watcher_failed.assert_not_called()
    log_failed.assert_called_once()
    assert "boom" in log_failed.call_args.args[0]


def test_wake_watcher_loop_failure_logs_once_and_exits(
    tmp_path: Path,
) -> None:
    """A watcher failure is logged once and leaves the timer schedule intact.

    The watcher is an accelerator, never a dependency: if it dies, the loop
    must keep running on cadence rather than stalling or crashing.
    """
    import tick  # type: ignore[import-untyped]

    stop_event = threading.Event()
    wake_event = threading.Event()

    with mock.patch.object(tick, "WAKE_WATCH_POLL_SECONDS", 0):
        with mock.patch.object(
            tick, "_consume_wake_request", side_effect=OSError("scan boom"),
        ):
            with mock.patch.object(tick, "_log_wake_watcher_failed") as log_failed:
                tick._wake_watcher_loop(stop_event, wake_event)

    log_failed.assert_called_once()
    assert "scan boom" in log_failed.call_args.args[0]
    assert not wake_event.is_set()


def test_log_wake_watcher_failed_appends_to_the_running_tick_log(
    tmp_path: Path,
) -> None:
    """The watcher must not open a log file while a tick owns one.

    ``_log_tick_summary`` aggregates ``tail()``, which reads the *newest*
    log file in the directory. A watcher thread that opened its own log
    mid-tick would become that newest file, so the running tick would report
    the watcher's single entry as its whole roll-up.
    """
    import tick  # type: ignore[import-untyped]

    with mock.patch.object(tick, "TICK_LOG_DIR", str(tmp_path)):
        running = tick.TickLog.open(str(tmp_path))
        running.log(pr=439, phase="dispatch", stage="implement",
                    action="launch", reason="", engine="claude")
        running.log(pr=470, phase="harvest", stage="review",
                    action="harvest", reason="", engine="codex")

        tick._log_wake_watcher_failed("wake watcher stopped: OSError: boom")

    assert len(list(tmp_path.glob("tick-*.jsonl"))) == 1
    actions = [entry.get("action") for entry in running.tail()]
    assert actions == ["launch", "harvest", "wake_watcher_failed"]


def test_log_wake_watcher_failed_opens_a_log_when_none_exists(
    tmp_path: Path,
) -> None:
    """A watcher that dies before the first tick still leaves an audit trail.

    ``_start_wake_watcher`` runs before the loop's first tick opens a log, so
    a thread-start failure has no log to append to and must create one.
    """
    import tick  # type: ignore[import-untyped]

    log_dir = tmp_path / ".tick-log"
    with mock.patch.object(tick, "TICK_LOG_DIR", str(log_dir)):
        tick._log_wake_watcher_failed("could not start wake watcher: boom")

        entries = tick.TickLog.tail_dir(str(log_dir))

    assert [entry.get("action") for entry in entries] == ["wake_watcher_failed"]


def test_notify_loop_quiescent_stop_names_held_items_on_every_surface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The in-process stop must explain itself, not just stop.

    This is the ``--loop`` half of "the stop notification names the held
    items"; ``test_run_tick_body_one_shot_stop_names_held_items`` covers the
    ``tick-cadence:`` half. Every ``_run_loop_locked`` test mocks this function
    out, so without a direct test the suffix is unasserted on the path that
    actually notifies a human.

    All three surfaces are checked because they are what the operator sees: an
    unexplained stop leaves a held engine unattended with no way to learn why.
    """
    import tick  # type: ignore[import-untyped]

    held = ("pr #42 ai:reviewing (ai:needs-human-decision)",)
    log_dir = tmp_path / ".tick-log"
    notifications: list[tuple[str, str]] = []

    def record_notification(*, title: str, message: str) -> None:
        notifications.append((title, message))

    with mock.patch.object(tick, "TICK_LOG_DIR", str(log_dir)):
        with mock.patch.object(
            tick, "_emit_operator_notification", record_notification,
        ):
            tick._notify_loop_quiescent_stop(held)
        entries = tick.TickLog.tail_dir(str(log_dir))

    suffix = "; awaiting human decision: pr #42 ai:reviewing (ai:needs-human-decision)"

    assert suffix in capsys.readouterr().err
    assert [entry.get("action") for entry in entries] == ["loop_quiescent_stop"]
    assert suffix in str(entries[0].get("reason"))
    assert [message for _title, message in notifications] == [
        "PR pipeline loop stopped after consecutive quiescent heartbeats" + suffix,
    ]


def test_notify_loop_quiescent_stop_stays_bare_with_nothing_held(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ordinary quiescent stop must not grow a dangling empty suffix."""
    import tick  # type: ignore[import-untyped]

    log_dir = tmp_path / ".tick-log"
    with mock.patch.object(tick, "TICK_LOG_DIR", str(log_dir)):
        with mock.patch.object(tick, "_emit_operator_notification"):
            tick._notify_loop_quiescent_stop()
        entries = tick.TickLog.tail_dir(str(log_dir))

    assert "awaiting human decision" not in capsys.readouterr().err
    assert "awaiting human decision" not in str(entries[0].get("reason"))


def test_run_loop_locked_survives_watcher_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watcher that cannot start degrades to the plain cadence timer."""
    import tick  # type: ignore[import-untyped]

    tick_calls: list[str] = []
    monkeypatch.setattr(
        tick,
        "_run_single_tick_locked",
        lambda *_args, **_kwargs: tick_calls.append("tick") or 0,
    )
    monkeypatch.setattr(
        tick,
        "_observe_loop_state",
        lambda _target=None, **_kwargs: {
            "in_flight_count": 0,
            "nearest_engine_deadline": None,
            "actionable_queue": False,
            "at_cap": False,
            "quiescent": True,
        },
    )
    monkeypatch.setattr(
        tick,
        "_wait_next_tick",
        lambda _seconds, _stop_event, _wake_event: (False, True),
    )

    with mock.patch.object(
        tick.threading.Thread, "start", side_effect=RuntimeError("no threads"),
    ):
        with mock.patch.object(tick, "_log_wake_watcher_failed") as log_failed:
            with mock.patch("tick._notify_loop_quiescent_stop"):
                rc = tick._run_loop_locked(None, "claude", "", quiescent_stop_after=1)

    assert rc == 0
    assert tick_calls == ["tick"]
    log_failed.assert_called_once()


def test_observe_loop_state_counts_hung_marker_after_human_escalation(
    tmp_path: Path,
) -> None:
    """A retained paid process keeps loop cadence non-quiescent without a label.

    Both scans read the same real marker: it must occupy a slot (so the loop
    stays warm) while contributing no deadline (so the loop does not pace to
    an escalated engine's fixed past deadline and hot-loop on the overdue
    floor). ``#578`` retains such a marker indefinitely pending a human.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 900.0, "base_ref": "main", "base_sha": "base",
        "head_sha": "head", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999,
        "start_time": 100.0, "pgid": 99999,
    }))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.read_authoritative_in_flight_labels", return_value=[],
        ):
            with mock.patch(
                "tick.read_authoritative_human_holds", return_value={},
            ):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch("tick.time.time", return_value=1000.0):
                        with mock.patch(
                            "process_control.get_process_start_time",
                            return_value=100.0,
                        ):
                            with mock.patch(
                                "process_control.pid_is_alive", return_value=True,
                            ):
                                state = tick._observe_loop_state()

    assert state == {
        "in_flight_count": 1,
        "nearest_engine_deadline": None,
        "actionable_queue": False,
        "at_cap": False,
        "quiescent": False,
        "held_items": (),
    }

    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=state["in_flight_count"],
        nearest_engine_deadline=state["nearest_engine_deadline"],
        actionable_queue=state["actionable_queue"],
        at_cap=state["at_cap"],
        quiescent=state["quiescent"],
        consecutive_quiescent=0,
        now=1000.0,
    )
    assert (sleep_seconds, stop) == (270, False)


def test_observe_loop_state_discounts_remote_human_held_engine() -> None:
    """A durable GitHub hold ends supervision without freeing admission."""
    import tick  # type: ignore[import-untyped]

    markers = [("pr", 42, "ai:reviewing", "hung", "unknown", True)]
    holds = {("pr", 42): frozenset({"ai:needs-human-decision"})}
    with mock.patch(
        "tick.read_authoritative_in_flight_labels", return_value=[],
    ):
        with mock.patch(
            "tick.read_authoritative_human_holds",
            return_value=holds,
        ):
            with mock.patch("tick._scan_marker_identities", return_value=markers):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        state = tick._observe_loop_state()

    assert state["in_flight_count"] == 0
    assert state["quiescent"] is True
    assert state["held_items"] == (
        "pr #42 ai:reviewing (ai:needs-human-decision)",
    )


def test_observe_loop_state_reaches_quiescence_past_four_foreign_labels(
    tmp_path: Path,
) -> None:
    """Foreign in-flight labels this machine did not launch cannot hold the loop.

    The 2026-08-20 incident shape from #663: four hand-set ``ai:implementing``
    issues, no local marker, nothing dispatchable. Under the retired union
    ceiling those four labels made ``in_flight=4``, forced ``at_cap``, and put
    ``quiescent`` permanently out of reach, so the loop spun forever on work it
    could never dispatch.

    The marker root is a real empty directory and the labels are built by the
    real authoritative reader, so this drives ``_scan_marker_identities`` →
    ``_admission_states`` → ``in_flight_refs`` rather than stubbing the scan
    whose output is the thing under test.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.gh_json",
            side_effect=gh_json_four_foreign_implementing_labels,
        ):
            state = tick._observe_loop_state()

    assert state == {
        "in_flight_count": 0,
        "nearest_engine_deadline": None,
        "actionable_queue": False,
        "at_cap": False,
        "quiescent": True,
        "held_items": (),
    }


def test_run_loop_locked_stops_quiescent_despite_foreign_in_flight_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop reaches its normal quiescent stop with foreign labels present.

    ``_observe_loop_state`` is left real — backed by an empty marker root and
    the incident's four ``ai:implementing`` issues — so the whole observation →
    cadence → stop progression runs against the production occupancy contract.
    Only the tick body, the inter-tick wait, and the wake watcher are stubbed:
    none of them decides quiescence.

    The stop must land after exactly ``LOOP_QUIESCENT_STOP_AFTER`` heartbeats.
    If foreign labels re-entered occupancy, every observation would report
    ``quiescent=False`` and this loop would never return.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    ticks = 0
    stop_notifications: list[object] = []

    def fake_tick(*_args: object, **_kwargs: object) -> int:
        nonlocal ticks
        ticks += 1
        if ticks > pipeline.LOOP_QUIESCENT_STOP_AFTER + 2:
            raise AssertionError(
                "loop did not reach quiescent stop; foreign in-flight labels "
                "are holding occupancy again"
            )
        return 0

    monkeypatch.setattr(tick, "_run_single_tick_locked", fake_tick)
    # A full scheduled interval elapsed and no nudge arrived: the only shape
    # that counts as a quiescent heartbeat.
    monkeypatch.setattr(
        tick, "_wait_next_tick", lambda *_a, **_k: (False, True),
    )
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tick,
        "_notify_loop_quiescent_stop",
        lambda held: stop_notifications.append(held),
    )
    monkeypatch.setattr(tick, "WT_ROOT", str(wt_root))
    monkeypatch.setattr(
        tick, "gh_json", gh_json_four_foreign_implementing_labels,
    )

    rc = tick._run_loop_locked(None, "claude", "")

    assert rc == 0
    assert ticks == pipeline.LOOP_QUIESCENT_STOP_AFTER
    assert stop_notifications == [()]


def test_run_loop_locked_preserves_a_nudge_that_arrives_during_a_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nudge landing mid-tick must survive to the following wait.

    The loop clears the wake channel before each tick so a nudge is not
    consumed by the iteration already running. If that clear happened after
    the tick instead, a wake-request touched while a tick was in progress
    would be silently dropped and the loop would sleep the full interval.
    """
    import tick  # type: ignore[import-untyped]

    seen_set_at_wait: list[bool] = []
    ticks = 0

    def fake_tick(*_args: object, **_kwargs: object) -> int:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            # A wake-request arrives while the first tick is still running.
            wake_events[0].set()
        return 0

    wake_events: list[threading.Event] = []

    def capture_wait(
        _seconds: int,
        _stop_event: threading.Event,
        wake_event: threading.Event,
    ) -> tuple[bool, bool]:
        seen_set_at_wait.append(wake_event.is_set())
        # Stop after the second wait so the loop terminates.
        return (len(seen_set_at_wait) >= 2, False)

    def capture_watcher(
        _stop_event: threading.Event, wake_event: threading.Event,
    ) -> None:
        wake_events.append(wake_event)
        return None

    monkeypatch.setattr(tick, "_run_single_tick_locked", fake_tick)
    monkeypatch.setattr(
        tick,
        "_observe_loop_state",
        lambda _target=None, **_kwargs: {
            "in_flight_count": 1,
            "nearest_engine_deadline": None,
            "actionable_queue": True,
            "at_cap": False,
            "quiescent": False,
        },
    )
    monkeypatch.setattr(tick, "_start_wake_watcher", capture_watcher)
    monkeypatch.setattr(tick, "_wait_next_tick", capture_wait)

    tick._run_loop_locked(None, "claude", "")

    # First wait sees the mid-tick nudge; the second starts from a clean
    # channel because the loop cleared it before the second tick.
    assert seen_set_at_wait == [True, False]


def test_run_loop_locked_observes_state_under_its_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cadence must be scoped to the same target dispatch is scoped to.

    Without this the loop dispatches against one PR while computing
    quiescence from the global queue, so a targeted loop never exits.
    """
    import tick  # type: ignore[import-untyped]

    observed: list[object] = []

    def capture_observe(target: object = None, **_kwargs: object) -> object:
        observed.append(target)
        return {
            "in_flight_count": 0,
            "nearest_engine_deadline": None,
            "actionable_queue": False,
            "at_cap": False,
            "quiescent": True,
        }

    monkeypatch.setattr(
        tick, "_run_single_tick_locked", lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(tick, "_observe_loop_state", capture_observe)
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_a: None)
    monkeypatch.setattr(
        tick, "_wait_next_tick", lambda *_a: (False, True),
    )

    with mock.patch("tick._notify_loop_quiescent_stop"):
        tick._run_loop_locked(
            ("pr", 578), "claude", "", quiescent_stop_after=1,
        )

    assert observed == [("pr", 578)]


def test_run_loop_locked_stop_signal_interrupts_a_long_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGINT during a tick must not wait out the cadence interval.

    The inter-tick wait blocks on the wake channel, so the stop handler has
    to signal that channel too — otherwise a stop arriving during a quiescent
    tick would sit through the full 1800s heartbeat before exiting.
    """
    import signal as signal_mod

    import tick  # type: ignore[import-untyped]

    def fake_tick(*_args: object, **_kwargs: object) -> int:
        # Deliver the signal the way the OS would, mid-tick.
        handler = signal_mod.getsignal(signal_mod.SIGINT)
        assert callable(handler)
        handler(signal_mod.SIGINT, None)
        return 0

    monkeypatch.setattr(tick, "_run_single_tick_locked", fake_tick)
    monkeypatch.setattr(
        tick,
        "_observe_loop_state",
        lambda _target=None, **_kwargs: {
            "in_flight_count": 1,
            "nearest_engine_deadline": None,
            "actionable_queue": True,
            "at_cap": False,
            "quiescent": False,
        },
    )
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_args: None)

    start = time.monotonic()
    rc = tick._run_loop_locked(None, "claude", "")
    elapsed = time.monotonic() - start

    assert rc == 0
    assert elapsed < 5


def test_wait_next_tick_full_interval_reports_timer_expired() -> None:
    """A wait with no nudge runs its scheduled interval and says so."""
    import tick  # type: ignore[import-untyped]

    stopped, timer_expired = tick._wait_next_tick(
        0, threading.Event(), threading.Event(),
    )

    assert stopped is False
    assert timer_expired is True


def test_wait_next_tick_stop_wins_over_nudge() -> None:
    """A stop request during the wait exits the loop, nudge or not."""
    import tick  # type: ignore[import-untyped]

    stop_event = threading.Event()
    wake_event = threading.Event()
    stop_event.set()
    wake_event.set()

    stopped, _timer_expired = tick._wait_next_tick(60, stop_event, wake_event)

    assert stopped is True


def test_wait_next_tick_nudge_enforces_debounce_floor() -> None:
    """A nudge-initiated wake waits out the debounce floor first."""
    import tick  # type: ignore[import-untyped]

    stop_event = threading.Event()
    wake_event = threading.Event()
    wake_event.set()

    with mock.patch.object(tick, "LOOP_NUDGE_FLOOR_SECONDS", 0.2):
        start = time.monotonic()
        stopped, timer_expired = tick._wait_next_tick(60, stop_event, wake_event)
        elapsed = time.monotonic() - start

    assert stopped is False
    assert timer_expired is False
    assert elapsed >= 0.2


def test_wait_next_tick_scheduled_deadline_wins_over_longer_debounce() -> None:
    """A nudge cannot postpone the cadence authority's earlier deadline."""
    import tick  # type: ignore[import-untyped]

    stop_event = mock.Mock(spec=threading.Event)
    stop_event.is_set.return_value = False
    stop_event.wait.return_value = False
    wake_event = mock.Mock(spec=threading.Event)
    wake_event.wait.return_value = True

    with mock.patch.object(tick, "LOOP_NUDGE_FLOOR_SECONDS", 10):
        with mock.patch.object(
            tick.time, "monotonic", side_effect=[100.0, 100.1, 101.0],
        ):
            stopped, timer_expired = tick._wait_next_tick(
                1, stop_event, wake_event,
            )

    assert stopped is False
    assert timer_expired is True
    wake_event.wait.assert_called_once_with(1)
    stop_event.wait.assert_called_once_with(pytest.approx(0.9))


def test_has_actionable_queue_ignores_human_held_items() -> None:
    """Items dispatch would skip for a human hold must not keep the loop
    awake: a held-only queue is quiescent, matching dispatch eligibility."""
    import tick  # type: ignore[import-untyped]

    held_issue = [{"author": {"login": TRUSTED_AUTHOR}, "labels": [
        {"name": "ai:available"}, {"name": "ai:blocked"},
    ]}]
    held_pr = [{"labels": [
        {"name": "ai:changes-requested"}, {"name": "ai:needs-human-decision"},
    ]}]
    empty: list[object] = []

    with mock.patch(
        "tick.gh_json", side_effect=[held_issue, empty, empty, held_pr],
    ):
        assert tick._has_actionable_queue() is False


def test_has_actionable_queue_ignores_an_in_flight_labelled_item() -> None:
    """A partial transition must not keep the loop awake forever.

    An item caught mid-convergence carries its in-flight label next to its
    pickup label, and dispatch now refuses it for as long as that engine runs.
    Counting it as actionable would make the queue permanently non-quiescent
    while no tick can ever act on it — a hot loop against unreachable work.
    An engine this machine actually owns keeps the loop warm through its
    marker's in-flight count, not through the queue probe.
    """
    import tick  # type: ignore[import-untyped]

    in_flight_issue = [{"author": {"login": TRUSTED_AUTHOR}, "labels": [
        {"name": "ai:available"}, {"name": "ai:implementing"},
    ]}]
    in_flight_pr = [{"labels": [
        {"name": "ai:changes-requested"}, {"name": "ai:fixing"},
    ]}]
    empty: list[object] = []

    with mock.patch(
        "tick.gh_json",
        side_effect=[in_flight_issue, empty, empty, in_flight_pr],
    ):
        assert tick._has_actionable_queue() is False


def test_has_actionable_queue_ignores_an_ambiguous_pickup_pair() -> None:
    """Cadence must apply dispatch's *exact* rule, not a weaker one.

    Dispatch refuses ``ai:implemented`` + ``ai:revised`` (it would guess at the
    stale-revert target the marker records) and ``ai:implemented`` +
    ``ai:changes-requested`` (two stages would each claim it). Neither carries
    a blocking label, so a blocking-label-only cadence probe calls both
    actionable — the loop then polls forever against work no tick will ever
    take, the same hot loop the hold-label carve-out exists to prevent.
    """
    import tick  # type: ignore[import-untyped]

    same_stage_pair = [{"labels": [
        {"name": "ai:implemented"}, {"name": "ai:revised"},
    ]}]
    cross_stage_pair = [{"labels": [
        {"name": "ai:implemented"}, {"name": "ai:changes-requested"},
    ]}]
    empty: list[object] = []

    with mock.patch(
        "tick.gh_json",
        side_effect=[empty, same_stage_pair, empty, cross_stage_pair],
    ):
        assert tick._has_actionable_queue() is False


def test_target_has_actionable_queue_skips_an_ambiguous_pickup_pair() -> None:
    """The targeted loop shares the rule: an item dispatch permanently
    refuses must not hold that loop short of its quiescent stop."""
    import tick  # type: ignore[import-untyped]

    payload = {"state": "OPEN", "labels": [
        {"name": "ai:implemented"}, {"name": "ai:changes-requested"},
    ]}

    with mock.patch("tick.gh_json", return_value=payload):
        assert tick._target_has_actionable_queue(("pr", 578)) is False


def test_has_actionable_queue_sees_hold_free_item_past_held_one() -> None:
    """One held item must not shadow a dispatchable one in the same queue."""
    import tick  # type: ignore[import-untyped]

    issues = [
        {"author": {"login": TRUSTED_AUTHOR}, "labels": [{"name": "ai:available"}, {"name": "ai:blocked"}]},
        {"author": {"login": TRUSTED_AUTHOR}, "labels": [{"name": "ai:available"}]},
    ]

    with mock.patch("tick.gh_json", side_effect=[issues]):
        assert tick._has_actionable_queue() is True


def test_has_actionable_queue_stays_warm_when_held_page_may_truncate() -> None:
    """A full page of held items may hide a dispatchable one beyond the scan
    limit — possible truncation must stay warm, never conclude quiescence."""
    import tick  # type: ignore[import-untyped]

    full_held_page = [
        {"author": {"login": TRUSTED_AUTHOR}, "labels": [{"name": "ai:available"}, {"name": "ai:blocked"}]}
        for _ in range(tick._ACTIONABLE_QUEUE_SCAN_LIMIT)
    ]

    with mock.patch("tick.gh_json", side_effect=[full_held_page]):
        assert tick._has_actionable_queue() is True


def test_has_actionable_queue_treats_malformed_labels_as_actionable() -> None:
    """An unreadable label payload stays warm — the loop must not coerce
    unknown state into quiescence."""
    import tick  # type: ignore[import-untyped]

    malformed = [{"labels": "not-a-list"}]

    with mock.patch("tick.gh_json", side_effect=[malformed]):
        assert tick._has_actionable_queue() is True


def test_has_actionable_queue_treats_malformed_json_as_actionable() -> None:
    """gh emitting malformed JSON must stay warm, not raise out of the probe
    and terminate the unattended loop."""
    import tick  # type: ignore[import-untyped]

    with mock.patch(
        "tick.gh_json", side_effect=json.JSONDecodeError("truncated", "{", 1),
    ):
        assert tick._has_actionable_queue() is True


def test_target_has_actionable_queue_sees_eligible_open_target() -> None:
    """An open target carrying a dispatch-eligible pre-label is still work."""
    import tick  # type: ignore[import-untyped]

    payload = {"state": "OPEN", "labels": [{"name": "ai:changes-requested"}]}

    with mock.patch("tick.gh_json", return_value=payload):
        assert tick._target_has_actionable_queue(("pr", 578)) is True


def test_target_has_actionable_queue_skips_human_held_target() -> None:
    """A human hold on the target is a stop for dispatch, so also for cadence."""
    import tick  # type: ignore[import-untyped]

    payload = {"state": "OPEN", "labels": [
        {"name": "ai:changes-requested"}, {"name": "ai:needs-human-decision"},
    ]}

    with mock.patch("tick.gh_json", return_value=payload):
        assert tick._target_has_actionable_queue(("pr", 578)) is False


def test_target_has_actionable_queue_skips_an_in_flight_labelled_target() -> None:
    """An in-flight label on the target is a stop for dispatch, so also for
    cadence — otherwise a targeted loop never reaches its quiescent stop."""
    import tick  # type: ignore[import-untyped]

    payload = {"state": "OPEN", "labels": [
        {"name": "ai:revised"}, {"name": "ai:reviewing"},
    ]}

    with mock.patch("tick.gh_json", return_value=payload):
        assert tick._target_has_actionable_queue(("pr", 578)) is False


def test_target_has_actionable_queue_skips_closed_target_with_stale_label() -> None:
    """Dispatch only scans open items, so a stale label on a merged PR is
    unreachable work and must not hold a targeted loop awake."""
    import tick  # type: ignore[import-untyped]

    payload = {"state": "MERGED", "labels": [{"name": "ai:changes-requested"}]}

    with mock.patch("tick.gh_json", return_value=payload):
        assert tick._target_has_actionable_queue(("pr", 578)) is False


def test_target_has_actionable_queue_covers_an_issue_targets_open_pr() -> None:
    """An issue target's review and fix work lives on its closing PR, so
    cadence must follow the same stage resolution dispatch uses."""
    import tick  # type: ignore[import-untyped]

    def side_effect(*args: str) -> object:
        if "closedByPullRequestsReferences" in args:
            return {"closedByPullRequestsReferences": [closing_pr_reference(590)]}
        if "state,number" in args:
            return {"number": 590, "state": "OPEN"}
        if args[0] == "issue":
            return {"state": "OPEN", "author": {"login": TRUSTED_AUTHOR}, "labels": [{"name": "ai:implementing"}]}
        return {"state": "OPEN", "labels": [{"name": "ai:changes-requested"}]}

    with mock.patch("tick.gh_json", side_effect=side_effect):
        assert tick._target_has_actionable_queue(("issue", 584)) is True


def test_target_has_actionable_queue_stays_warm_when_scope_read_fails() -> None:
    """An unresolvable target scope is unknown state — it must stay warm."""
    import tick  # type: ignore[import-untyped]

    with mock.patch(
        "tick._resolve_stage_targets", side_effect=ValueError("ambiguous"),
    ):
        assert tick._target_has_actionable_queue(("issue", 584)) is True


def test_target_has_actionable_queue_stays_warm_when_item_read_fails() -> None:
    """A failed queue read under a target still yields actionable_queue=True."""
    import tick  # type: ignore[import-untyped]

    with mock.patch(
        "tick.gh_json",
        side_effect=subprocess.CalledProcessError(1, ["gh", "pr", "view"]),
    ):
        assert tick._target_has_actionable_queue(("pr", 578)) is True


def test_target_has_actionable_queue_stays_warm_on_unreadable_state() -> None:
    """An unrecognised state value proves nothing about reachability."""
    import tick  # type: ignore[import-untyped]

    payload = {"state": None, "labels": [{"name": "ai:changes-requested"}]}

    with mock.patch("tick.gh_json", return_value=payload):
        assert tick._target_has_actionable_queue(("pr", 578)) is True


def test_target_has_actionable_queue_stays_warm_on_malformed_labels() -> None:
    """An unreadable label payload must not coerce a targeted loop to stop."""
    import tick  # type: ignore[import-untyped]

    payload = {"state": "OPEN", "labels": "not-a-list"}

    with mock.patch("tick.gh_json", return_value=payload):
        assert tick._target_has_actionable_queue(("pr", 578)) is True


def test_observe_loop_state_targeted_ignores_unrelated_actionable_queue() -> None:
    """A targeted loop reaches quiescence once its own target is terminal, even
    while unrelated PRs still sit in a dispatch-eligible queue."""
    import tick  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json", side_effect=fake_gh_json_terminal_pr_578):
        with mock.patch(
            "tick.read_authoritative_in_flight_labels", return_value=[],
        ), mock.patch("tick.read_authoritative_human_holds", return_value={}), \
                mock.patch("tick._scan_marker_identities", return_value=[]):
            with mock.patch(
                "tick._nearest_actionable_engine_deadline", return_value=None,
            ):
                state = tick._observe_loop_state(("pr", 578))

    assert state["actionable_queue"] is False
    assert state["quiescent"] is True


def test_observe_loop_state_targeted_unreadable_scope_stays_warm() -> None:
    """An unreadable target scope keeps the loop warm on the queue probe alone.

    ``_observe_loop_state`` still runs an authoritative in-flight read whose
    value it discards, and the comment there is careful that the read is *not*
    what stops a failed queue probe from reaching quiescence. This is that
    claim, executable: the in-flight read succeeds, the scope read fails, and
    the loop must still refuse to claim a stop — proving
    ``_target_has_actionable_queue`` collapses its unknown to warm on its own.
    """
    import tick  # type: ignore[import-untyped]

    with mock.patch(
        "tick.gh_json", side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        with mock.patch(
            "tick.read_authoritative_in_flight_labels", return_value=[],
        ), mock.patch("tick.read_authoritative_human_holds", return_value={}), \
                mock.patch("tick._scan_marker_identities", return_value=[]):
            with mock.patch(
                "tick._nearest_actionable_engine_deadline", return_value=None,
            ):
                state = tick._observe_loop_state(("pr", 578))

    assert state["in_flight_count"] == 0
    assert state["actionable_queue"] is True
    assert state["quiescent"] is False


def test_observe_loop_state_untargeted_still_sees_global_queue() -> None:
    """The same pipeline state without a target stays warm — unscoped
    quiescence must be unchanged by target scoping."""
    import tick  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json", side_effect=fake_gh_json_terminal_pr_578):
        with mock.patch(
            "tick.read_authoritative_in_flight_labels", return_value=[],
        ), mock.patch("tick.read_authoritative_human_holds", return_value={}), \
                mock.patch("tick._scan_marker_identities", return_value=[]):
            with mock.patch(
                "tick._nearest_actionable_engine_deadline", return_value=None,
            ):
                state = tick._observe_loop_state()

    assert state["actionable_queue"] is True
    assert state["quiescent"] is False


def test_main_loop_mode_uses_locked_loop_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main(loop=True) enters loop mode while holding the tick mutex."""
    import tick  # type: ignore[import-untyped]
    from tick import main  # type: ignore[import-untyped]

    monkeypatch.setattr(tick, "TICK_LOCK_PATH", str(tmp_path / ".tick.lock"))
    monkeypatch.setattr(tick, "git", lambda *_args: str(tmp_path))
    with mock.patch("tick._run_loop_locked", return_value=0) as run_loop:
        with mock.patch("tick._run_single_tick_locked") as run_single:
            assert main(loop=True) == 0

    # Every flag is forwarded unresolved: each tick resolves the config for
    # itself, so an edit mid-loop applies on the next tick.
    run_loop.assert_called_once_with(
        None, "", "", "", "", "", fix_engine="", fix_model="",
    )
    run_single.assert_not_called()


@pytest.mark.real_gh_helper
def test_relabel_terminal_label_logs_and_notifies(tmp_path: Path) -> None:
    """A transition into a terminal label emits operator notification signals.

    Opts into the real ``tick.gh`` because it drives ``relabel`` end to end;
    its own ``subprocess.run`` mock is what keeps the mutation off the network.
    """
    from tick import TickLog, relabel  # type: ignore[import-untyped]

    tick_log = TickLog.open(str(tmp_path))
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with mock.patch("tick._emit_operator_notification") as notify:
            relabel(
                "pr",
                42,
                "ai:reviewing",
                "ai:ready-for-human",
                tick_log=tick_log,
                phase="harvest",
                stage="review",
            )

    notify.assert_called_once()
    entries = tick_log.tail()
    terminal_entries = [
        entry for entry in entries
        if entry.get("action") == "notify_terminal_label"
    ]
    assert terminal_entries
    assert terminal_entries[-1]["pr"] == 42
    assert terminal_entries[-1]["details"] == {
        "entity": "pr",
        "label": "ai:ready-for-human",
    }


@pytest.mark.real_gh_helper
def test_gh_runs_subprocess_with_encoding() -> None:
    """gh() runs subprocess.run with utf-8 encoding and errors='replace'."""
    from tick import gh  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "list"], returncode=0, stdout="", stderr="",
        )
        result = gh("pr", "list")
        run.assert_called_once_with(
            ["gh", "pr", "list", "--repo", "github.com/test-owner/test-repo"], capture_output=True,
            encoding="utf-8", errors="replace", check=True,
        )
        assert result.returncode == 0


@pytest.mark.real_gh_helper
def test_gh_check_false_allows_nonzero_exit() -> None:
    """gh(check=False) does not raise on non-zero exit."""
    from tick import gh  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "view", "1"], returncode=1, stdout="", stderr="",
        )
        result = gh("pr", "view", "1", check=False)
        assert result.returncode == 1


@pytest.mark.real_gh_helper
def test_gh_json_parses_stdout() -> None:
    """gh_json() parses JSON from gh --json output."""
    from tick import gh_json  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "list", "--json", "number"],
            returncode=0,
            stdout='[{"number":1},{"number":2}]',
            stderr="",
        )
        result = gh_json("pr", "list", "--json", "number")
        assert result == [{"number": 1}, {"number": 2}]


@pytest.mark.real_gh_helper
def test_gh_json_raises_on_nonzero_exit() -> None:
    """gh_json() raises CalledProcessError on non-zero exit."""
    from tick import gh_json  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.side_effect = subprocess.CalledProcessError(1, ["gh"])
        with pytest.raises(subprocess.CalledProcessError):
            gh_json("pr", "list")


@pytest.mark.real_gh_helper
def test_gh_text_strips_stdout() -> None:
    """gh_text() returns stripped stdout text."""
    from tick import gh_text  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "view", "1", "--json", "state", "-q", ".state"],
            returncode=0, stdout="OPEN\n", stderr="",
        )
        result = gh_text("pr", "view", "1", "--json", "state", "-q", ".state")
        assert result == "OPEN"


@pytest.mark.real_gh_helper
def test_gh_text_raises_on_nonzero_exit() -> None:
    """gh_text() raises CalledProcessError on non-zero exit."""
    from tick import gh_text  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.side_effect = subprocess.CalledProcessError(1, ["gh"])
        with pytest.raises(subprocess.CalledProcessError):
            gh_text("pr", "view", "1")


# --- git helper ---


def test_git_returns_stripped_stdout() -> None:
    """git() returns stripped stdout and raises on non-zero exit."""
    from tick import git  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["git", "rev-parse", "--show-toplevel"],
            returncode=0, stdout="/repo\n", stderr="",
        )
        result = git("rev-parse", "--show-toplevel")
        assert result == "/repo"


def test_git_raises_on_nonzero_exit() -> None:
    """git() raises CalledProcessError on non-zero exit."""
    from tick import git  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.side_effect = subprocess.CalledProcessError(1, ["git"])
        with pytest.raises(subprocess.CalledProcessError):
            git("status")


# --- relabel ---


@pytest.mark.real_gh_helper
def test_relabel_adds_and_removes_labels() -> None:
    """relabel() calls gh pr edit with --add-label and --remove-label."""
    from tick import relabel  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        relabel("pr", 42, "ai:reviewing", "ai:ready-for-human")
        run.assert_called_once_with(
            ["gh", "pr", "edit", "42", "--add-label", "ai:ready-for-human",
             "--remove-label", "ai:reviewing", "--repo", "github.com/test-owner/test-repo"],
            capture_output=True, encoding="utf-8", errors="replace", check=True,
        )


@pytest.mark.real_gh_helper
def test_relabel_empty_from_label_omits_remove() -> None:
    """relabel() with empty from_label omits --remove-label."""
    from tick import relabel  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        relabel("pr", 42, "", "ai:reviewing")
        # Should call with only --add-label, no --remove-label
        call_args = run.call_args[0][0]
        assert "--remove-label" not in call_args
        assert "--add-label" in call_args
        assert "ai:reviewing" in call_args


def test_relabel_same_label_is_noop() -> None:
    """relabel() with from_label == to_label is a no-op."""
    from tick import relabel  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        relabel("pr", 42, "ai:reviewing", "ai:reviewing")
        run.assert_not_called()


@pytest.mark.real_gh_helper
def test_relabel_uses_issue_entity() -> None:
    """relabel() uses gh issue edit when entity='issue'."""
    from tick import relabel  # type: ignore[import-untyped]

    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        relabel("issue", 7, "ai:implementing", "ai:available")
        call_args = run.call_args[0][0]
        assert call_args[0] == "gh"
        assert call_args[1] == "issue"


# --- cleanup_scratch ---


def test_cleanup_scratch_removes_directory(tmp_path: Path) -> None:
    """cleanup_scratch() removes the worktree directory."""
    from tick import cleanup_scratch  # type: ignore[import-untyped]

    wt = tmp_path / "pr-42"
    wt.mkdir()
    (wt / "dispatch.json").write_text("{}")

    cleanup_scratch(str(wt))
    assert not wt.exists()


def test_cleanup_scratch_handles_missing_directory() -> None:
    """cleanup_scratch() does not raise on missing directory."""
    from tick import cleanup_scratch  # type: ignore[import-untyped]

    cleanup_scratch("/nonexistent/path")  # should not raise


# --- StageConfig ---


def test_stage_config_is_importable_from_pipeline() -> None:
    """StageConfig is importable from pipeline (shared vocabulary)."""
    from pipeline import StageConfig  # type: ignore[attr-defined]

    config = StageConfig(
        stage="review",
        worktree_prefix="pr",
        attempts_key_prefix="",
        result_file="review.json",
        in_flight_label="ai:reviewing",
        pre_label="ai:implemented",
        strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    assert config.stage == "review"
    assert config.result_file == "review.json"


# --- verify_codex_bin ---


def test_verify_codex_bin_returns_path_when_version_meets_minimum() -> None:
    """verify_codex_bin() returns the binary path when version >= minimum."""
    import shutil as _shutil

    from tick import verify_codex_bin  # type: ignore[import-untyped]

    with mock.patch.object(_shutil, "which", return_value="/usr/local/bin/codex"):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["/usr/local/bin/codex", "--version"],
                returncode=0, stdout="codex-cli 0.141.0\n", stderr="",
            )
            result = verify_codex_bin(min_version="codex-cli 0.141.0")
    assert result == "/usr/local/bin/codex"


def test_verify_codex_bin_raises_on_version_too_old() -> None:
    """verify_codex_bin() raises RuntimeError when installed version is below minimum."""
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


def test_verify_codex_bin_accepts_newer_version() -> None:
    """verify_codex_bin() accepts versions newer than the minimum."""
    import shutil as _shutil

    from tick import verify_codex_bin  # type: ignore[import-untyped]

    with mock.patch.object(_shutil, "which", return_value="/usr/local/bin/codex"):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["/usr/local/bin/codex", "--version"],
                returncode=0, stdout="codex-cli 0.142.3\n", stderr="",
            )
            result = verify_codex_bin(min_version="codex-cli 0.141.0")
    assert result == "/usr/local/bin/codex"


def test_verify_codex_bin_accepts_exact_minimum_version() -> None:
    """verify_codex_bin() accepts the exact minimum version."""
    import shutil as _shutil

    from tick import verify_codex_bin  # type: ignore[import-untyped]

    with mock.patch.object(_shutil, "which", return_value="/usr/local/bin/codex"):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["/usr/local/bin/codex", "--version"],
                returncode=0, stdout="codex-cli 0.141.0\n", stderr="",
            )
            result = verify_codex_bin(min_version="codex-cli 0.141.0")
    assert result == "/usr/local/bin/codex"


def test_parse_codex_version_valid() -> None:
    """_parse_codex_version extracts semver tuple from valid string."""
    from tick import _parse_codex_version  # type: ignore[import-untyped]

    assert _parse_codex_version("codex-cli 0.141.0") == (0, 141, 0)
    assert _parse_codex_version("codex-cli 1.2.3") == (1, 2, 3)


def test_parse_codex_version_strips_prerelease() -> None:
    """_parse_codex_version handles pre-release suffixes like 0.141.0-beta.1."""
    from tick import _parse_codex_version  # type: ignore[import-untyped]

    assert _parse_codex_version("codex-cli 0.141.0-beta.1") == (0, 141, 0)


def test_parse_codex_version_rejects_malformed() -> None:
    """_parse_codex_version raises ValueError on malformed input."""
    import pytest
    from tick import _parse_codex_version  # type: ignore[import-untyped]

    with pytest.raises(ValueError):
        _parse_codex_version("codex 1.2.3")
    with pytest.raises(ValueError):
        _parse_codex_version("")
    with pytest.raises(ValueError):
        _parse_codex_version("codex-cli abc.def.ghi")


def test_parse_codex_version_rejects_wrong_component_count() -> None:
    """_parse_codex_version rejects versions without exactly 3 components."""
    import pytest
    from tick import _parse_codex_version  # type: ignore[import-untyped]

    with pytest.raises(ValueError):
        _parse_codex_version("codex-cli 0.141")
    with pytest.raises(ValueError):
        _parse_codex_version("codex-cli 0.141.0.1")


def test_min_codex_version_covers_reviewer_argv_contract() -> None:
    """MIN_CODEX_VERSION must be at least the build the reviewer argv needs.

    The reviewer engine command relies on behavior verified on codex-cli
    0.142.5: bare ``codex exec`` honoring ``--output-schema`` for the final
    message, ``--ignore-rules``, and ``-s read-only`` unattended. A floor
    below that admits builds where the emitted argv is unproven.
    """
    from tick import (  # type: ignore[import-untyped]
        MIN_CODEX_VERSION,
        _parse_codex_version,
    )

    assert _parse_codex_version(MIN_CODEX_VERSION) >= (0, 142, 5)


# --- _resolve_engine ---


def test_resolve_engine_passthrough_for_claude() -> None:
    """_resolve_engine('claude') returns passthrough with no extra env."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    binary, extra = _resolve_engine("claude")
    assert binary == "claude"
    assert extra == {}


def test_resolve_engine_claude_ds_translates_to_claude() -> None:
    """_resolve_engine('claude-ds') returns 'claude' binary with DeepSeek env."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        binary, extra = _resolve_engine("claude-ds")

    assert binary == "claude"
    assert extra["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert extra["ANTHROPIC_AUTH_TOKEN"] == "sk-test"


def test_resolve_engine_claude_ds_uses_keypath_fallback(tmp_path: Path) -> None:
    """_resolve_engine reads the key from the DEEPSEEK_API_KEYPATH file.

    KEYPATH is a *path to a file containing the key* — the path string
    itself must never be used as the auth token.
    """
    from tick import _resolve_engine  # type: ignore[import-untyped]

    key_file = tmp_path / "deepseek.key"
    key_file.write_text("sk-from-file\n")

    env = {"DEEPSEEK_API_KEYPATH": str(key_file)}
    with mock.patch.dict("os.environ", env, clear=True):
        binary, extra = _resolve_engine("claude-ds")

    assert binary == "claude"
    assert extra["ANTHROPIC_AUTH_TOKEN"] == "sk-from-file"


def test_resolve_engine_claude_ds_accepts_unmistakable_raw_keypath_value() -> None:
    """A legacy sk-* token in KEYPATH remains supported without a file read."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    env = {"DEEPSEEK_API_KEYPATH": "sk-direct-token-1234567890"}
    with mock.patch.dict("os.environ", env, clear=True):
        binary, extra = _resolve_engine("claude-ds")

    assert binary == "claude"
    assert extra["ANTHROPIC_AUTH_TOKEN"] == "sk-direct-token-1234567890"


def test_resolve_engine_claude_ds_rejects_missing_keypath_file(
    tmp_path: Path,
) -> None:
    """A path-shaped missing secret remains a hard configuration failure."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    keypath = str(tmp_path / "missing.key")
    with mock.patch.dict(
        "os.environ", {"DEEPSEEK_API_KEYPATH": keypath}, clear=True,
    ):
        with pytest.raises(RuntimeError, match="cannot read DEEPSEEK_API_KEYPATH"):
            _resolve_engine("claude-ds")


def test_resolve_engine_claude_ds_rejects_keypath_directory(
    tmp_path: Path,
) -> None:
    """A directory cannot be reinterpreted as a provider credential."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    with mock.patch.dict(
        "os.environ", {"DEEPSEEK_API_KEYPATH": str(tmp_path)}, clear=True,
    ):
        with pytest.raises(RuntimeError, match="cannot read DEEPSEEK_API_KEYPATH"):
            _resolve_engine("claude-ds")


def test_resolve_engine_claude_ds_rejects_token_shaped_keypath_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing filesystem object that is not a readable file fails closed
    even when its name matches the raw-token shape — the legacy raw-token
    fallback applies only when nothing exists at the path."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    (tmp_path / "sk-token-shaped-directory-1234567890").mkdir()
    monkeypatch.chdir(tmp_path)

    with mock.patch.dict(
        "os.environ",
        {"DEEPSEEK_API_KEYPATH": "sk-token-shaped-directory-1234567890"},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="cannot read DEEPSEEK_API_KEYPATH"):
            _resolve_engine("claude-ds")


def test_resolve_engine_claude_ds_rejects_token_shaped_dangling_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dangling symlink is a misconfigured path (its key file is missing),
    not a raw credential — it must fail closed like any other path failure."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    link = tmp_path / "sk-token-shaped-symlink-1234567890"
    link.symlink_to(tmp_path / "missing.key")
    monkeypatch.chdir(tmp_path)

    with mock.patch.dict(
        "os.environ",
        {"DEEPSEEK_API_KEYPATH": "sk-token-shaped-symlink-1234567890"},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="cannot read DEEPSEEK_API_KEYPATH"):
            _resolve_engine("claude-ds")


def test_resolve_engine_claude_ds_rejects_keypath_read_error(
    tmp_path: Path,
) -> None:
    """Permission or transient I/O errors fail closed instead of leaking a path."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    keypath = str(tmp_path / "deepseek.key")
    with mock.patch.dict(
        "os.environ", {"DEEPSEEK_API_KEYPATH": keypath}, clear=True,
    ):
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            with pytest.raises(RuntimeError, match="cannot read DEEPSEEK_API_KEYPATH"):
                _resolve_engine("claude-ds")


def test_resolve_engine_claude_ds_prefers_reading_token_shaped_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keypath naming a real readable file is a path — even when the
    relative filename itself matches the raw-token shape. The path contract
    is primary; the legacy raw-token fallback applies only when no file
    exists."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    keyfile = tmp_path / "sk-production-token-file-2026"
    keyfile.write_text("sk-real-file-token-1234567890\n")
    monkeypatch.chdir(tmp_path)

    with mock.patch.dict(
        "os.environ",
        {"DEEPSEEK_API_KEYPATH": "sk-production-token-file-2026"},
        clear=True,
    ):
        _binary, extra = _resolve_engine("claude-ds")

    assert extra["ANTHROPIC_AUTH_TOKEN"] == "sk-real-file-token-1234567890"


def test_resolve_engine_claude_ds_error_redacts_keypath_value() -> None:
    """The failure message must not echo the configured value — a legacy raw
    token that misses the shape check would otherwise leak into logs."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    secret = "sk-secret token with spaces 1234567890"
    with mock.patch.dict(
        "os.environ", {"DEEPSEEK_API_KEYPATH": secret}, clear=True,
    ):
        with pytest.raises(RuntimeError) as excinfo:
            _resolve_engine("claude-ds")
    assert secret not in str(excinfo.value)
    assert "DEEPSEEK_API_KEYPATH" in str(excinfo.value)


def test_resolve_engine_claude_ds_raises_without_token() -> None:
    """_resolve_engine('claude-ds') raises when no DeepSeek token is available."""
    import pytest
    from tick import _resolve_engine  # type: ignore[import-untyped]

    with mock.patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            _resolve_engine("claude-ds")


# --- _engine_env ---


def test_engine_env_preserves_parent_environment_exactly() -> None:
    """_engine_env gives a trusted local engine the operator's full env."""
    from tick import _engine_env  # type: ignore[import-untyped]

    parent = {
        "CMUX_SOCKET_PATH": "/tmp/cmux.sock",
        "MY_CUSTOM_AUTH": "token",
        "CLAUDE_CODE_SESSION_ID": "sess-abc",
        "CLAUDECODE": "1",
        "HOME": "/u",
        "PATH": "/usr/bin",
    }
    with mock.patch.dict("os.environ", parent, clear=True):
        env = _engine_env()

    assert env == parent


# --- _launch_claude_engine model parameter ---


def test_launch_claude_engine_omits_model_flag_by_default(tmp_path: Path) -> None:
    """No override means no --model flag, so the CLI uses the operator default.

    Passing a hardcoded model here would silently override whatever the
    operator has configured in ~/.claude/settings.json, which is the one
    thing the default is supposed to honor.
    """
    from tick import _launch_claude_engine  # type: ignore[import-untyped]

    wt_dir = str(tmp_path)
    result_path = os.path.join(wt_dir, "result.json")

    with mock.patch("subprocess.Popen") as popen:
        fake_proc = mock.Mock()
        fake_proc.pid = 12345
        popen.return_value = fake_proc

        _launch_claude_engine(
            engine_bin="claude",
            prompt="test prompt",
            wt_dir=wt_dir,
            result_path=result_path,
            env={"HOME": "/tmp", "PATH": "/usr/bin"},
            push_permission="Bash(git push origin HEAD)",
        )

    cmd = popen.call_args[0][0]
    assert "--model" not in cmd, (
        f"Expected no --model flag so the operator default applies, got: {cmd}"
    )


def test_launch_claude_engine_uses_explicit_model(tmp_path: Path) -> None:
    """_launch_claude_engine passes the explicit model when provided."""
    from tick import _launch_claude_engine  # type: ignore[import-untyped]

    wt_dir = str(tmp_path)
    result_path = os.path.join(wt_dir, "result.json")

    with mock.patch("subprocess.Popen") as popen:
        fake_proc = mock.Mock()
        fake_proc.pid = 12345
        popen.return_value = fake_proc

        _launch_claude_engine(
            engine_bin="claude",
            prompt="test prompt",
            wt_dir=wt_dir,
            result_path=result_path,
            env={"HOME": "/tmp", "PATH": "/usr/bin"},
            push_permission="Bash(git push origin HEAD)",
            model="claude-sonnet-4-6",
        )

    cmd = popen.call_args[0][0]
    model_idx = cmd.index("--model")
    assert cmd[model_idx + 1] == "claude-sonnet-4-6", (
        f"Expected --model claude-sonnet-4-6, got --model {cmd[model_idx + 1]}"
    )


@pytest.mark.parametrize("override", [None, "auto", "dontAsk"])
def test_launch_claude_engine_permission_mode(
    tmp_path: Path, override: str | None,
) -> None:
    """Default auto and explicit modes preserve the narrow pre-approved grants.

    This asserts launch policy, not provider classifier availability or verdict.
    """
    from tick import _launch_claude_engine  # type: ignore[import-untyped]

    wt_dir = str(tmp_path)
    result_path = os.path.join(wt_dir, "result.json")

    with mock.patch("subprocess.Popen") as popen:
        fake_proc = mock.Mock()
        fake_proc.pid = 12345
        popen.return_value = fake_proc

        _launch_claude_engine(
            engine_bin="claude",
            prompt="test prompt",
            wt_dir=wt_dir,
            result_path=result_path,
            env={"HOME": "/tmp", "PATH": "/usr/bin"},
            push_permission="Bash(git push origin HEAD)",
            **({"permission_mode": override} if override is not None else {}),
        )

    cmd = popen.call_args[0][0]
    perm_idx = cmd.index("--permission-mode")
    assert cmd[perm_idx + 1] == (override or "auto"), (
        f"Expected --permission-mode {override or 'auto'}, got "
        f"--permission-mode {cmd[perm_idx + 1]}"
    )
    assert "--allowedTools" in cmd, (
        f"auto mode must keep the bounded allowlist (narrow rules resolve "
        f"before the classifier), got: {cmd}"
    )


@pytest.mark.parametrize("settings", ["", '{"sandbox":{"enabled":false}}'])
def test_launch_claude_engine_passes_engine_settings(
    tmp_path: Path, settings: str,
) -> None:
    """Configured engine settings reach ``--settings``; unset omits the flag."""
    from tick import _launch_claude_engine  # type: ignore[import-untyped]

    with mock.patch("subprocess.Popen") as popen:
        popen.return_value = mock.Mock(pid=12345)
        _launch_claude_engine(
            engine_bin="claude",
            prompt="test prompt",
            wt_dir=str(tmp_path),
            result_path=str(tmp_path / "result.json"),
            env={"HOME": "/tmp", "PATH": "/usr/bin"},
            push_permission="Bash(git push origin HEAD)",
            engine_settings=settings,
        )

    cmd = popen.call_args[0][0]
    if settings:
        assert cmd[cmd.index("--settings") + 1] == settings
    else:
        assert "--settings" not in cmd
    assert "--setting-sources" not in cmd


def test_launch_claude_engine_rejects_unknown_permission_mode(
    tmp_path: Path,
) -> None:
    """_launch_claude_engine fails closed on an unknown permission mode.

    A typo in the option (or a future mode the engine launch was not built
    for) must abort the launch rather than silently emitting an unknown
    ``--permission-mode`` value to the CLI.
    """
    from tick import _launch_claude_engine  # type: ignore[import-untyped]

    with pytest.raises(ValueError, match="permission_mode"):
        _launch_claude_engine(
            engine_bin="claude",
            prompt="test prompt",
            wt_dir=str(tmp_path),
            result_path=str(tmp_path / "result.json"),
            env={"HOME": "/tmp", "PATH": "/usr/bin"},
            push_permission="Bash(git push origin HEAD)",
            permission_mode="ask",
        )


def test_launch_claude_engine_wires_trusted_stream_wrapper(tmp_path: Path) -> None:
    """Claude stream output is parsed by the controller-owned wrapper."""
    from tick import _launch_claude_engine  # type: ignore[import-untyped]

    wt_dir = str(tmp_path)
    result_path = os.path.join(wt_dir, "result.json")

    with mock.patch("subprocess.Popen") as popen:
        fake_proc = mock.Mock()
        fake_proc.pid = 12345
        popen.return_value = fake_proc

        _launch_claude_engine(
            engine_bin="claude",
            prompt="test prompt",
            wt_dir=wt_dir,
            result_path=result_path,
            env={"HOME": "/tmp", "PATH": "/usr/bin"},
            push_permission="Bash(git push origin HEAD)",
        )

    cmd = popen.call_args[0][0]
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("claude_stream_wrapper.py")
    assert "stream-json" in cmd
    assert "--verbose" in cmd


def test_launch_claude_engine_points_the_wrapper_at_the_worktree_heartbeat(
    tmp_path: Path,
) -> None:
    """The heartbeat path is a wrapper option, ahead of the ``--`` separator.

    Both wrapperless stages launch through this one helper, so proving the
    emitted argv here is what makes implementer and fixer symmetric — and the
    flag must land before ``--`` or it would be passed to ``claude`` instead.
    """
    import claude_stream_wrapper  # type: ignore[import-untyped]
    from tick import _launch_claude_engine  # type: ignore[import-untyped]

    wt_dir = str(tmp_path)

    with mock.patch("subprocess.Popen") as popen:
        popen.return_value = mock.Mock(pid=12345)

        _launch_claude_engine(
            engine_bin="claude",
            prompt="test prompt",
            wt_dir=wt_dir,
            result_path=os.path.join(wt_dir, "result.json"),
            env={"HOME": "/tmp", "PATH": "/usr/bin"},
            push_permission="Bash(git push origin HEAD)",
        )

    cmd = popen.call_args[0][0]
    heartbeat_idx = cmd.index("--heartbeat")
    expected = os.path.join(wt_dir, claude_stream_wrapper.HEARTBEAT_FILENAME)
    assert cmd[heartbeat_idx + 1] == expected
    assert heartbeat_idx < cmd.index("--")


def test_launch_claude_engine_writes_the_heartbeat_from_a_relative_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real wrapper must land the heartbeat inside the engine's worktree.

    The wrapper runs with `cwd=wt_dir`, so a relative heartbeat path resolves a
    second time under the worktree, and ProgressHeartbeat swallows the
    resulting OSError — the engine exits 0 with no heartbeat and an empty
    stderr, which no assertion on argv or exit status can see.

    WT_ROOT is absolute today, so a relative worktree is not what production
    hands the launcher; it is the input that makes the silent failure
    observable. This holds `_heartbeat_path`'s resolution in place as a
    property of the path rather than of `_resolve_wt_root`, whose anchoring
    this file does not own.
    """
    from tick import _launch_claude_engine  # type: ignore[import-untyped]

    # Stands in for `claude -p ...`: ignores argv, emits two stream events.
    engine = tmp_path / "fake-engine"
    engine.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "print(json.dumps({'type': 'assistant'}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success',\n"
        "                  'is_error': False, 'result': 'ok'}), flush=True)\n",
    )
    engine.chmod(0o755)

    rel_wt = os.path.join(".claude", "worktrees", "issue-4242")
    monkeypatch.chdir(tmp_path)
    os.makedirs(rel_wt)
    launched_at = time.time()

    proc = _launch_claude_engine(
        engine_bin=str(engine),
        prompt="test prompt",
        wt_dir=rel_wt,
        result_path=os.path.join(rel_wt, "result.json"),
        env=dict(os.environ),
        push_permission="Bash(git push origin HEAD)",
    )
    assert proc is not None
    assert proc.wait(timeout=30) == 0

    heartbeat = os.path.join(rel_wt, "heartbeat")
    assert os.path.exists(heartbeat), (
        "the wrapper must write <worktree>/heartbeat, not a path resolved "
        "again under its own cwd"
    )
    # This engine's own output produced it — not a leftover file that happened
    # to be there. Advancement across the throttle interval is proven at the
    # wrapper level, where the interval is injectable.
    assert os.path.getmtime(heartbeat) >= launched_at


def test_resolve_engine_claude_ds_respects_model_override() -> None:
    """_resolve_engine('claude-ds', model=...) overrides ANTHROPIC_MODEL."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        _binary, extra = _resolve_engine("claude-ds", model="deepseek-v4-flash")

    assert extra["ANTHROPIC_MODEL"] == "deepseek-v4-flash", (
        f"Expected ANTHROPIC_MODEL=deepseek-v4-flash, got {extra.get('ANTHROPIC_MODEL')!r}"
    )


def test_resolve_engine_claude_ds_keeps_default_model_without_override() -> None:
    """_resolve_engine('claude-ds') without model keeps default deepseek-v4-pro[1m]."""
    from tick import _resolve_engine  # type: ignore[import-untyped]

    with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        _binary, extra = _resolve_engine("claude-ds")

    assert extra["ANTHROPIC_MODEL"] == "deepseek-v4-pro[1m]", (
        f"Expected default ANTHROPIC_MODEL=deepseek-v4-pro[1m], "
        f"got {extra.get('ANTHROPIC_MODEL')!r}"
    )


def test_dispatch_one_implementer_resolves_model_for_claude_ds(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer resolves "" to deepseek-v4-pro[1m] for claude-ds.

    The sentinel "" means "use per-engine default". This test verifies that
    the resolution happens before _resolve_engine and _launch_claude_engine
    are called, so claude-ds always gets a valid model name.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess as _sp
    _sp.run(["git", "init", str(repo)], check=True, capture_output=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@x.com"],
        check=True, capture_output=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (repo / "README.md").write_text("hi")
    _sp.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    base_sha = _sp.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()

    def fake_git(*args: str, **kwargs: object) -> str:
        if "--show-toplevel" in args:
            return str(repo)
        if "fetch" in args:
            return ""
        if "rev-parse" in args:
            return base_sha
        return ""

    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345

    with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        with mock.patch("tick.WT_ROOT", str(wt_root)):
            with mock.patch("tick.git", side_effect=fake_git):
                with mock.patch("tick.pipeline.atomic_write") as aw:
                    aw.side_effect = lambda path, data: Path(path).parent.mkdir(
                        parents=True, exist_ok=True,
                    ) or Path(path).write_text(data)
                    with mock.patch("subprocess.Popen", return_value=fake_proc):
                        with mock.patch(
                            "tick._publish_marker_or_revert", return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "tick._resolve_engine"
                                ) as resolve_mock:
                                    resolve_mock.return_value = ("claude", {})
                                    with mock.patch(
                                        "tick._launch_claude_engine",
                                        return_value=fake_proc,
                                    ) as launch_mock:
                                        _dispatch_one_implementer(
                                            issue_number=77,
                                            issue_title="Test",
                                            issue_body="Body",
                                            tick_log=None,
                                            impl_engine="claude-ds",
                                            model="",
                                        )

    # _resolve_engine must receive the resolved model, not ""
    if len(resolve_mock.call_args[0]) > 1:
        resolve_call_model = resolve_mock.call_args[0][1]
    else:
        resolve_call_model = resolve_mock.call_args[1].get("model")
    assert resolve_call_model == "deepseek-v4-pro[1m]", (
        f"Expected resolved model 'deepseek-v4-pro[1m]' for claude-ds, "
        f"got {resolve_call_model!r}"
    )

    # _launch_claude_engine must also receive the resolved model
    launch_kwargs = launch_mock.call_args[1]
    assert launch_kwargs["model"] == "deepseek-v4-pro[1m]", (
        f"Expected launch model 'deepseek-v4-pro[1m]' for claude-ds, "
        f"got {launch_kwargs.get('model')!r}"
    )


def test_dispatch_one_implementer_escalates_when_engine_resolve_fails(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer escalates to ai:needs-human-decision when
    _resolve_engine raises RuntimeError (missing DeepSeek credentials).

    Without escalation the issue stays on ai:available and every subsequent
    tick retries the same setup failure in an infinite loop.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess as _sp
    _sp.run(["git", "init", str(repo)], check=True, capture_output=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@x.com"],
        check=True, capture_output=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (repo / "README.md").write_text("hi")
    _sp.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    base_sha = _sp.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()

    def fake_git(*args: str, **kwargs: object) -> str:
        if "--show-toplevel" in args:
            return str(repo)
        if "fetch" in args:
            return ""
        if "rev-parse" in args:
            return base_sha
        return ""

    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345

    with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        with mock.patch("tick.WT_ROOT", str(wt_root)):
            with mock.patch("tick.git", side_effect=fake_git):
                with mock.patch("tick.pipeline.atomic_write") as aw:
                    aw.side_effect = lambda path, data: Path(path).parent.mkdir(
                        parents=True, exist_ok=True,
                    ) or Path(path).write_text(data)
                    with mock.patch(
                        "tick._resolve_engine",
                        side_effect=RuntimeError("no key"),
                    ):
                        with mock.patch(
                            "tick.gh", side_effect=allow_escalation_comment(),
                        ), mock.patch(
                            "tick.relabel",
                        ) as relabel_mock:
                            with mock.patch(
                                "tick.cleanup_scratch",
                            ):
                                _dispatch_one_implementer(
                                    issue_number=77,
                                    issue_title="Test",
                                    issue_body="Body",
                                    tick_log=None,
                                    impl_engine="claude-ds",
                                )

    all_calls = [c[0] for c in relabel_mock.call_args_list]
    assert ("issue", 77, "ai:available", "ai:needs-human-decision") in all_calls, (
        f"Expected escalation to ai:needs-human-decision; "
        f"got relabel calls: {all_calls}"
    )


def test_dispatch_one_fixer_resolves_model_for_claude_ds(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer resolves "" to deepseek-v4-pro[1m] for claude-ds.

    The fixer must call _resolve_engine when impl_engine="claude-ds" so the
    engine gets DeepSeek routing env vars, matching the implementer path.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345

    with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        with mock.patch("tick.WT_ROOT", str(wt_root)):
            with mock.patch("tick.git", side_effect=fixer_git(tmp_path)):
                with mock.patch("tick.gh_text", return_value="main"):
                    with mock.patch(
                        "tick.gh_json",
                        side_effect=fixer_gh_json(
                            comments=[
                                {"body": "<!-- pr-pipeline:review -->\nfixes"},
                            ],
                        ),
                    ):
                        with mock.patch(
                            "tick.pipeline.REVIEW_MARKER",
                            "<!-- pr-pipeline:review -->",
                        ):
                            with mock.patch("tick.pipeline.atomic_write"):
                                with mock.patch("os.makedirs"):
                                    with mock.patch("tick.relabel"):
                                        with mock.patch(
                                            "tick._publish_marker_or_revert",
                                            return_value=True,
                                        ):
                                            with mock.patch(
                                                "tick._resolve_engine"
                                            ) as resolve_mock:
                                                resolve_mock.return_value = (
                                                    "claude",
                                                    {"ANTHROPIC_MODEL": "deepseek-v4-pro[1m]"},
                                                )
                                                with mock.patch(
                                                    "tick._launch_claude_engine",
                                                    return_value=fake_proc,
                                                ) as launch_mock:
                                                    _dispatch_one_fixer(
                                                        pr_number=55,
                                                        tick_log=None,
                                                        impl_engine="claude-ds",
                                                        model="",
                                                    )

    # _resolve_engine must be called with claude-ds and resolved model
    resolve_mock.assert_called_once()
    resolve_args = resolve_mock.call_args
    assert resolve_args[0][0] == "claude-ds", (
        f"Expected impl_engine='claude-ds', got {resolve_args[0][0]!r}"
    )
    resolve_model = resolve_args[1].get("model")
    assert resolve_model == "deepseek-v4-pro[1m]", (
        f"Expected model='deepseek-v4-pro[1m]', got {resolve_model!r}"
    )

    # _launch_claude_engine must receive DeepSeek env and resolved model
    launch_kwargs = launch_mock.call_args[1]
    assert launch_kwargs["model"] == "deepseek-v4-pro[1m]", (
        f"Expected launch model 'deepseek-v4-pro[1m]', "
        f"got {launch_kwargs.get('model')!r}"
    )
    launch_env = launch_kwargs.get("env", {})
    assert launch_env.get("ANTHROPIC_MODEL") == "deepseek-v4-pro[1m]", (
        f"Expected ANTHROPIC_MODEL=deepseek-v4-pro[1m] in env, "
        f"got {launch_env.get('ANTHROPIC_MODEL')!r}"
    )


def test_verify_codex_bin_raises_when_not_found() -> None:
    """verify_codex_bin() raises RuntimeError when codex not on PATH."""
    import shutil as _shutil

    from tick import verify_codex_bin  # type: ignore[import-untyped]

    with mock.patch.object(_shutil, "which", return_value=None):
        with pytest.raises(RuntimeError, match="codex not found"):
            verify_codex_bin()


def test_verify_codex_bin_uses_shutil_which_not_command_builtin() -> None:
    """verify_codex_bin() must use shutil.which, not subprocess.run(['command', '-v', ...]).

    'command' is a shell builtin, not a binary, so subprocess.run(['command', '-v', 'codex'])
    raises FileNotFoundError on Linux — silently disabling all reviewer dispatch on any
    Linux host regardless of whether codex is installed.  shutil.which is the portable fix.
    """
    import shutil as _shutil

    from tick import verify_codex_bin  # type: ignore[import-untyped]

    with mock.patch.object(_shutil, "which", return_value="/usr/local/bin/codex") as which_mock:
        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["/usr/local/bin/codex", "--version"],
                returncode=0, stdout="codex-cli 0.141.0\n", stderr="",
            )
            result = verify_codex_bin(min_version="codex-cli 0.141.0")
    which_mock.assert_called_once_with("codex")
    assert result == "/usr/local/bin/codex"


# --- EngineCooldowns ---


def test_transient_cooldown_until_uses_one_hour_fallback() -> None:
    import tick  # type: ignore[import-untyped]

    now = 1_800_000_000.0

    assert tick.transient_cooldown_until(
        {"result": "quota exceeded"},
        now=now,
    ) == now + tick.DEFAULT_TRANSIENT_COOLDOWN_SECONDS


def test_transient_cooldown_until_uses_the_configured_cooldown() -> None:
    """The fallback length and the reset margin are the operator's values."""
    import tick  # type: ignore[import-untyped]

    timezone = ZoneInfo("Europe/London")
    now = dt.datetime(2027, 7, 15, 1, 0, tzinfo=timezone).timestamp()
    reset = dt.datetime(2027, 7, 15, 2, 40, tzinfo=timezone).timestamp()

    assert tick.transient_cooldown_until(
        {"result": "quota exceeded"}, now=now,
        cooldown_seconds=900, reset_buffer_seconds=0,
    ) == now + 900
    assert tick.transient_cooldown_until(
        {"result": "You've hit your session limit · resets 2:40am Europe/London"},
        now=now, cooldown_seconds=900, reset_buffer_seconds=0,
    ) == reset


def test_transient_cooldown_until_uses_provider_reset_time() -> None:
    import tick  # type: ignore[import-untyped]

    timezone = ZoneInfo("Europe/London")
    now = dt.datetime(2027, 7, 15, 1, 0, tzinfo=timezone).timestamp()
    expected = dt.datetime(2027, 7, 15, 2, 45, tzinfo=timezone).timestamp()

    assert tick.transient_cooldown_until(
        {"result": "You've hit your session limit · resets 2:40am Europe/London"},
        now=now,
    ) == expected


def test_transient_cooldown_until_accepts_utc_reset_timezone() -> None:
    import tick  # type: ignore[import-untyped]

    timezone = ZoneInfo("UTC")
    now = dt.datetime(2027, 7, 15, 1, 0, tzinfo=timezone).timestamp()
    expected = dt.datetime(2027, 7, 15, 2, 45, tzinfo=timezone).timestamp()

    assert tick.transient_cooldown_until(
        {"result": "You've hit your session limit · resets 2:40am UTC"},
        now=now,
    ) == expected


def test_transient_cooldown_until_honors_same_day_reset_buffer() -> None:
    import tick  # type: ignore[import-untyped]

    timezone = ZoneInfo("Europe/London")
    now = dt.datetime(2027, 1, 15, 2, 42, tzinfo=timezone).timestamp()
    expected = dt.datetime(2027, 1, 15, 2, 45, tzinfo=timezone).timestamp()

    assert tick.transient_cooldown_until(
        {"result": "You've hit your session limit · resets 2:40am Europe/London"},
        now=now,
    ) == expected


def test_transient_cooldown_until_rolls_over_after_buffer_expires() -> None:
    import tick  # type: ignore[import-untyped]

    timezone = ZoneInfo("Europe/London")
    now = dt.datetime(2027, 1, 15, 2, 46, tzinfo=timezone).timestamp()
    expected = dt.datetime(2027, 1, 16, 2, 45, tzinfo=timezone).timestamp()

    assert tick.transient_cooldown_until(
        {"result": "You've hit your session limit · resets 2:40am Europe/London"},
        now=now,
    ) == expected


def test_engine_cooldowns_persists_active_deadline(tmp_path: Path) -> None:
    import tick  # type: ignore[import-untyped]

    cooldowns = tick.EngineCooldowns(str(tmp_path))
    cooldowns.start("claude", until=1_900.0, reason="quota_or_limit")

    assert cooldowns.read_active("claude", now=1_000.0) == 1_900.0


def test_engine_cooldowns_removes_expired_deadline(tmp_path: Path) -> None:
    import tick  # type: ignore[import-untyped]

    cooldowns = tick.EngineCooldowns(str(tmp_path))
    cooldowns.start("claude", until=900.0, reason="quota_or_limit")

    assert cooldowns.read_active("claude", now=1_000.0) is None
    assert not (tmp_path / ".engine-cooldowns" / "claude.json").exists()


def test_engine_cooldowns_never_shortens_existing_deadline(tmp_path: Path) -> None:
    import tick  # type: ignore[import-untyped]

    cooldowns = tick.EngineCooldowns(str(tmp_path))
    cooldowns.start("claude", until=5_000.0, reason="parsed_reset")
    cooldowns.start("claude", until=2_000.0, reason="fallback")

    assert cooldowns.read_active("claude", now=1_000.0) == 5_000.0


@pytest.mark.parametrize(
    "state",
    [
        {"domain": "claude-ds", "until": 1_900.0, "reason": "quota_or_limit"},
        {"domain": "claude", "until": 1_900.0, "reason": 7},
        {
            "domain": "claude",
            "until": 1_900.0,
            "reason": "quota_or_limit",
            "unexpected": True,
        },
    ],
)
def test_engine_cooldowns_rejects_semantically_invalid_state(
    tmp_path: Path,
    state: dict[str, object],
) -> None:
    import tick  # type: ignore[import-untyped]

    cooldown_dir = tmp_path / ".engine-cooldowns"
    cooldown_dir.mkdir()
    (cooldown_dir / "claude.json").write_text(json.dumps(state))

    with pytest.raises(ValueError, match="invalid engine cooldown state"):
        tick.EngineCooldowns(str(tmp_path)).read_active("claude", now=1_000.0)


def test_engine_quota_domains_separate_subscription_and_deepseek() -> None:
    import tick  # type: ignore[import-untyped]

    assert tick._engine_quota_domain("claude") == "claude"
    assert tick._engine_quota_domain("claude-ds") == "claude-ds"


def test_provider_cooldown_fails_closed_on_corrupt_state(tmp_path: Path) -> None:
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    cooldown_dir = wt_root / ".engine-cooldowns"
    cooldown_dir.mkdir(parents=True)
    (cooldown_dir / "claude.json").write_text("not-json")
    tick_log = tick.TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        blocked = tick._provider_cooldown_blocks_dispatch(
            number=7,
            stage="implement",
            engine="claude",
            tick_log=tick_log,
        )

    assert blocked is True
    assert any(
        entry["action"] == "cooldown_state_error" for entry in tick_log.tail()
    )


def test_provider_cooldown_does_not_cross_engine_domains(tmp_path: Path) -> None:
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    tick.EngineCooldowns(str(wt_root)).start(
        "claude", until=1_900.0, reason="quota_or_limit",
    )

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.time.time", return_value=1_000.0):
            blocked = tick._provider_cooldown_blocks_dispatch(
                number=7,
                stage="implement",
                engine="claude-ds",
                tick_log=None,
            )

    assert blocked is False


# --- Attempts (durable dispatch-attempt counter) ---


def test_attempts_read_returns_zero_for_missing_file(tmp_path: Path) -> None:
    """Attempts.read() returns 0 when the counter file does not exist."""
    from tick import Attempts  # type: ignore[import-untyped]

    attempts = Attempts(root=str(tmp_path), key_prefix="")
    assert attempts.read(42) == 0


def test_attempts_incr_and_read(tmp_path: Path) -> None:
    """Attempts.incr() increments and read() returns the new count."""
    from tick import Attempts  # type: ignore[import-untyped]

    attempts = Attempts(root=str(tmp_path), key_prefix="")
    assert attempts.read(42) == 0
    attempts.incr(42)
    assert attempts.read(42) == 1
    attempts.incr(42)
    assert attempts.read(42) == 2


def test_attempts_clear_removes_counter(tmp_path: Path) -> None:
    """Attempts.clear() removes the counter file."""
    from tick import Attempts  # type: ignore[import-untyped]

    attempts = Attempts(root=str(tmp_path), key_prefix="")
    attempts.incr(42)
    assert attempts.read(42) == 1
    attempts.clear(42)
    assert attempts.read(42) == 0


def test_attempts_respects_key_prefix(tmp_path: Path) -> None:
    """Attempts with key_prefix uses issue-N naming."""
    from tick import Attempts  # type: ignore[import-untyped]

    attempts = Attempts(root=str(tmp_path), key_prefix="issue-")
    attempts.incr(42)
    counter_file = tmp_path / "issue-42-attempts"
    assert counter_file.exists()


# --- TickLog ---


def test_tick_log_open_creates_file(tmp_path: Path) -> None:
    """TickLog.open() creates a new log file and returns a TickLog instance."""
    from tick import TickLog  # type: ignore[import-untyped]

    log = TickLog.open(str(tmp_path))
    assert os.path.exists(log.path)
    assert log.path.endswith(".jsonl")


def test_tick_log_write_appends_entry(tmp_path: Path) -> None:
    """TickLog.log() appends a JSON-lines entry."""
    from tick import TickLog  # type: ignore[import-untyped]

    log = TickLog.open(str(tmp_path))
    log.log(pr=42, phase="harvest", stage="review", action="promote",
            reason="no blockers", engine="")
    log.log(pr=None, phase="dispatch", stage="review", action="skip",
            reason="max_in_flight", engine="codex")

    entries = log.tail()
    assert len(entries) == 2
    assert entries[0]["pr"] == 42
    assert entries[0]["action"] == "promote"
    assert entries[1]["pr"] is None
    assert entries[1]["action"] == "skip"


def test_tick_log_log_is_non_fatal_on_write_failure(tmp_path: Path) -> None:
    """TickLog.log() does not raise on write failure."""
    from tick import TickLog  # type: ignore[import-untyped]

    log = TickLog.open(str(tmp_path))
    # Make the log file read-only
    os.chmod(log.path, 0o444)
    try:
        log.log(pr=42, phase="harvest", stage="review", action="promote",
                reason="no blockers", engine="")
    finally:
        os.chmod(log.path, 0o644)
    # No exception raised


def test_tick_log_tail_dir_returns_empty_for_missing_dir() -> None:
    """TickLog.tail_dir() returns empty list for non-existent directory."""
    from tick import TickLog  # type: ignore[import-untyped]

    entries = TickLog.tail_dir("/nonexistent/dir")
    assert entries == []


def test_tick_log_includes_dispatch_id_and_details(tmp_path: Path) -> None:
    """TickLog.log() includes dispatch_id and details in the JSON entry."""
    from tick import TickLog  # type: ignore[import-untyped]

    log = TickLog.open(str(tmp_path))
    log.log(pr=42, phase="dispatch", stage="review", action="launch",
            reason="", engine="codex",
            dispatch_id="review-42-1712345678",
            details={"head_sha": "abc12345", "base_ref": "main"})

    entries = log.tail()
    assert len(entries) == 1
    assert entries[0]["dispatch_id"] == "review-42-1712345678"
    assert entries[0]["details"] == {"head_sha": "abc12345", "base_ref": "main"}


def test_tick_log_omits_details_when_none(tmp_path: Path) -> None:
    """TickLog.log() omits the details key when details is None."""
    from tick import TickLog  # type: ignore[import-untyped]

    log = TickLog.open(str(tmp_path))
    log.log(pr=42, phase="harvest", stage="review", action="harvest",
            reason="no blockers", engine="")

    entries = log.tail()
    assert len(entries) == 1
    assert "details" not in entries[0]
    # dispatch_id is always present (empty string default)
    assert entries[0]["dispatch_id"] == ""


def test_tick_log_dispatch_id_defaults_to_empty(tmp_path: Path) -> None:
    """TickLog.log() writes dispatch_id as empty string when not provided."""
    from tick import TickLog  # type: ignore[import-untyped]

    log = TickLog.open(str(tmp_path))
    log.log(pr=None, phase="startup", stage="", action="startup_probe",
            reason="")

    entries = log.tail()
    assert len(entries) == 1
    assert entries[0]["dispatch_id"] == ""


# --- read_authoritative_in_flight_labels (Step 6) ---


def test_read_authoritative_in_flight_labels_returns_in_flight_refs() -> None:
    """read_authoritative_in_flight_labels() queries all three in-flight labels."""
    from tick import read_authoritative_in_flight_labels  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json:
        gh_json.side_effect = [
            [{"number": 1}],   # ai:implementing issues
            [{"number": 2}],   # ai:reviewing PRs
            [{"number": 3}],   # ai:fixing PRs
        ]
        refs = read_authoritative_in_flight_labels()

    assert len(refs) == 3
    kinds = {r.kind for r in refs}
    assert kinds == {"issue", "pr"}
    labels = {r.label for r in refs}
    assert labels == {"ai:implementing", "ai:reviewing", "ai:fixing"}


def test_read_authoritative_in_flight_labels_empty_when_nothing_in_flight() -> None:
    """Returns empty list when no items carry in-flight labels."""
    from tick import read_authoritative_in_flight_labels  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json:
        gh_json.side_effect = [[], [], []]
        refs = read_authoritative_in_flight_labels()

    assert refs == []


def test_read_authoritative_human_holds_covers_issues_prs_and_both_labels() -> None:
    """Both hold labels are durable custody for issues and PRs of any state.

    ``gh view`` resolves an item whatever its state, so a closed or merged
    item under a hold reads the same as an open one — and the read is bounded
    by the occupied identities, not by the repository's whole history.
    """
    from tick import read_authoritative_human_holds  # type: ignore[import-untyped]

    occupied = {
        ("issue", 1, "ai:implementing"),
        ("pr", 2, "ai:reviewing"),
        ("pr", 3, "ai:fixing"),
    }
    payloads = {
        "1": {"labels": [
            {"name": "ai:implementing"}, {"name": "ai:blocked"},
        ]},
        "2": {"labels": [
            {"name": "ai:reviewing"}, {"name": "ai:needs-human-decision"},
        ]},
        "3": {"labels": [{"name": "ai:fixing"}]},
    }

    with mock.patch("tick.gh_json") as gh_json:
        gh_json.side_effect = lambda *a, **_k: payloads[a[2]]
        holds = read_authoritative_human_holds(occupied)

    assert holds == {
        ("issue", 1): frozenset({"ai:blocked"}),
        ("pr", 2): frozenset({"ai:needs-human-decision"}),
    }
    # Exactly one bounded read per occupied item, never a repo-wide scan.
    assert [c.args for c in gh_json.call_args_list] == [
        ("issue", "view", "1", "--json", "labels"),
        ("pr", "view", "2", "--json", "labels"),
        ("pr", "view", "3", "--json", "labels"),
    ]


def test_hold_over_a_live_engine_still_keeps_the_loop_supervising() -> None:
    """A hold cannot discount an engine the pipeline is still going to act on.

    Process custody never transfers, so a *live* engine under a hold still has
    pipeline work pending: its result must be harvested and archived when it
    finishes, and it must be killed if it goes overdue. Stopping the loop here
    would strand a running paid engine with nobody to collect it.
    """
    import tick  # type: ignore[import-untyped]

    marker = [("pr", 42, "ai:reviewing", "live", "matched", True)]
    held = {("pr", 42): frozenset({"ai:needs-human-decision"})}

    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        with mock.patch(
            "tick.read_authoritative_human_holds", return_value=held,
        ):
            with mock.patch("tick._scan_marker_identities", return_value=marker):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        state = tick._observe_loop_state()

    assert state["in_flight_count"] == 1
    assert state["held_items"] == ()
    assert state["quiescent"] is False


def test_hold_over_an_unverifiable_engine_is_discounted() -> None:
    """The escalate-and-leave shape is the one the loop may stop on.

    An overdue engine whose identity cannot be proven is exactly the
    ``unverified_live_custody`` case #607 scoped: the pipeline has decided it
    will never act on it again, so only a human can move it.
    """
    import tick  # type: ignore[import-untyped]

    marker = [("pr", 42, "ai:reviewing", "hung", "unknown", True)]
    held = {("pr", 42): frozenset({"ai:needs-human-decision"})}

    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        with mock.patch(
            "tick.read_authoritative_human_holds", return_value=held,
        ):
            with mock.patch("tick._scan_marker_identities", return_value=marker):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        state = tick._observe_loop_state()

    assert state["in_flight_count"] == 0
    assert state["held_items"] == ("pr #42 ai:reviewing (ai:needs-human-decision)",)
    assert state["quiescent"] is True


def test_hold_over_a_killable_hung_engine_keeps_the_loop_supervising() -> None:
    """A hung engine we can still kill is pipeline work, not a human's."""
    import tick  # type: ignore[import-untyped]

    marker = [("pr", 42, "ai:reviewing", "hung", "matched", True)]
    held = {("pr", 42): frozenset({"ai:blocked"})}

    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        with mock.patch(
            "tick.read_authoritative_human_holds", return_value=held,
        ):
            with mock.patch("tick._scan_marker_identities", return_value=marker):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        state = tick._observe_loop_state()

    assert state["in_flight_count"] == 1
    assert state["quiescent"] is False


def test_hold_over_a_finished_engine_with_a_live_group_keeps_supervising() -> None:
    """A published envelope does not mean the process group is gone.

    ``_classify_marker`` forces ``classification_pid_alive`` False once an
    envelope validates, so a dispatch whose wrapper published its result while
    a child still lives classifies ``complete`` with ``engine_alive`` True. The
    hold gate will not archive it until death is proven, so the pipeline still
    owes it that archive — discounting it would let the loop stop with the
    result unfiled and no later tick to file it.
    """
    import tick  # type: ignore[import-untyped]

    # `complete` is not an engine-occupied marker state, so liveness is the
    # only thing holding this slot. GitHub reports nothing in flight on
    # purpose: a label cannot supply the slot, so if `engine_alive` stopped
    # being carried into admission this would read zero.
    marker = [("pr", 42, "ai:reviewing", "complete", "matched", True)]
    held = {("pr", 42): frozenset({"ai:needs-human-decision"})}

    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        with mock.patch(
            "tick.read_authoritative_human_holds", return_value=held,
        ):
            with mock.patch("tick._scan_marker_identities", return_value=marker):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        state = tick._observe_loop_state()

    assert state["in_flight_count"] == 1
    assert state["held_items"] == ()
    assert state["quiescent"] is False


def test_hold_over_a_launching_lease_is_discounted() -> None:
    """A lease names no process, so there is nothing for the loop to supervise.

    ``launching`` looks like in-flight work and is counted as an occupied
    admission slot, but a lease carries no PID at all: nothing to harvest and
    nothing to signal. Treating it as supervisable pins the loop awake forever,
    because the state is terminal — ``classify_dispatch`` never re-reads the
    deadline for a lease.
    """
    import tick  # type: ignore[import-untyped]

    marker = [("issue", 42, "ai:implementing", "launching", "unknown", False)]
    held = {("issue", 42): frozenset({"ai:needs-human-decision"})}

    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        with mock.patch(
            "tick.read_authoritative_human_holds", return_value=held,
        ):
            with mock.patch("tick._scan_marker_identities", return_value=marker):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        state = tick._observe_loop_state()

    assert state["in_flight_count"] == 0
    assert state["held_items"] == (
        "issue #42 ai:implementing (ai:needs-human-decision)",
    )
    assert state["quiescent"] is True


def test_one_held_slot_keeps_the_loop_running_and_still_dispatches() -> None:
    """One held slot of three must not stop the loop or block the free slots.

    Drives both halves of the contract off the same observation: cadence must
    stay awake with work queued, and admission must still permit a dispatch
    while counting the held slot against the cap.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    marker = [("pr", 42, "ai:reviewing", "hung", "unknown", True)]
    held = {("pr", 42): frozenset({"ai:needs-human-decision"})}

    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        with mock.patch(
            "tick.read_authoritative_human_holds", return_value=held,
        ):
            with mock.patch("tick._scan_marker_identities", return_value=marker):
                with mock.patch("tick._has_actionable_queue", return_value=True):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        state = tick._observe_loop_state()
                    admission = tick.can_dispatch_now(
                        max_in_flight=tick.pipeline.DEFAULT_MAX_IN_FLIGHT,
                    )

    # The held engine is discounted from supervision but not from spend.
    assert state["in_flight_count"] == 0
    assert state["held_items"] == ("pr #42 ai:reviewing (ai:needs-human-decision)",)
    assert admission.in_flight_count == 1

    # Two slots remain, so a dispatch is still permitted.
    assert admission.can_dispatch is True

    # And the loop keeps running rather than stopping on the held slot.
    assert state["quiescent"] is False
    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=state["in_flight_count"],
        nearest_engine_deadline=state["nearest_engine_deadline"],
        actionable_queue=state["actionable_queue"],
        at_cap=state["at_cap"],
        quiescent=state["quiescent"],
        consecutive_quiescent=0,
        held_items=state["held_items"],
    )
    assert stop is False
    assert sleep_seconds == 270


def test_release_verifies_removal_before_the_marker_is_destroyed() -> None:
    """An unverified release must not authorise deleting the only re-proof.

    Cleanup archives and removes the marker immediately after this call. If the
    removal silently did not land, the item becomes markerless under a hold —
    the exact stranded state that permanently consumes an admission slot — and
    nothing can ever re-prove death to retry it.
    """
    from tick import _release_in_flight_label  # type: ignore[import-untyped]

    still_labelled = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}

    with mock.patch("tick.gh", return_value=""):
        with mock.patch("tick.gh_json", return_value=still_labelled):
            assert not _release_in_flight_label(
                "pr", 42, "ai:fixing",
                tick_log=None, phase="harvest", stage="fix",
                required_hold_label="ai:needs-human-decision",
            )

    released = {"labels": [{"name": "ai:needs-human-decision"}]}
    with mock.patch("tick.gh", return_value=""):
        with mock.patch("tick.gh_json", return_value=released):
            assert _release_in_flight_label(
                "pr", 42, "ai:fixing",
                tick_log=None, phase="harvest", stage="fix",
                required_hold_label="ai:needs-human-decision",
            )


def test_release_refuses_when_no_hold_is_named_to_justify_it() -> None:
    """A release with no named hold has no justification, so it cannot run.

    The hold is the only thing that makes removing the pipeline's own label
    legitimate, and every caller reaches this helper from a decision that
    observed one. A caller that names none has lost that provenance somewhere
    upstream; releasing anyway would strip the item's last workflow label on
    the strength of a guard that silently did not apply.
    """
    from tick import _release_in_flight_label  # type: ignore[import-untyped]

    with mock.patch("tick.gh") as gh:
        with mock.patch("tick.gh_json") as gh_json:
            assert not _release_in_flight_label(
                "pr", 42, "ai:fixing",
                tick_log=None, phase="harvest", stage="fix",
                required_hold_label="",
            )

    gh.assert_not_called()
    gh_json.assert_not_called()


def test_release_requires_the_exact_observed_hold_before_mutation() -> None:
    """A stale held decision cannot release custody after its hold vanished."""
    from tick import _release_in_flight_label  # type: ignore[import-untyped]

    no_hold = {"labels": [{"name": "ai:fixing"}]}
    with mock.patch("tick.gh") as gh:
        with mock.patch("tick.gh_json", return_value=no_hold):
            assert not _release_in_flight_label(
                "pr", 42, "ai:fixing",
                tick_log=None, phase="harvest", stage="fix",
                required_hold_label="ai:needs-human-decision",
            )

    gh.assert_not_called()


def test_release_restores_in_flight_when_the_observed_hold_races() -> None:
    """A hold removed during release restores remote pipeline custody."""
    from tick import _release_in_flight_label  # type: ignore[import-untyped]

    before = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}
    raced = {"labels": []}
    restored = {"labels": [{"name": "ai:fixing"}]}
    with mock.patch("tick.gh", return_value="") as gh:
        with mock.patch(
            "tick.gh_json", side_effect=[before, raced, restored],
        ):
            assert not _release_in_flight_label(
                "pr", 42, "ai:fixing",
                tick_log=None, phase="harvest", stage="fix",
                required_hold_label="ai:needs-human-decision",
            )

    assert gh.call_args_list == [
        mock.call("pr", "edit", "42", "--remove-label", "ai:fixing"),
        mock.call("pr", "edit", "42", "--add-label", "ai:fixing"),
    ]


def test_release_leaves_a_later_workflow_label_to_own_the_item() -> None:
    """The restore is for an ownerless item, not for every departed hold.

    Same race as above, except the operator replaced the hold with an ordinary
    pipeline label instead of clearing it. Re-adding ``ai:fixing`` would put a
    stale in-flight label on top of that transition — and nothing later removes
    it, because the release only ever runs under a hold. The label would then
    pin an admission slot for good.
    """
    from tick import _release_in_flight_label  # type: ignore[import-untyped]

    before = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}
    raced = {"labels": [{"name": "ai:changes-requested"}]}
    with mock.patch("tick.gh", return_value="") as gh:
        with mock.patch("tick.gh_json", side_effect=[before, raced]):
            assert not _release_in_flight_label(
                "pr", 42, "ai:fixing",
                tick_log=None, phase="harvest", stage="fix",
                required_hold_label="ai:needs-human-decision",
            )

    assert gh.call_args_list == [
        mock.call("pr", "edit", "42", "--remove-label", "ai:fixing"),
    ]


def test_release_verification_read_failure_retains_custody() -> None:
    """An unreadable confirmation is not proof the release landed.

    The hold pre-check reads first and must succeed, so the failure this test
    injects lands on the confirmation read rather than short-circuiting ahead
    of the boundary under test.
    """
    from tick import _release_in_flight_label  # type: ignore[import-untyped]

    held = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}
    reads = [held, subprocess.CalledProcessError(1, "gh")]

    def read(*_args: str, **_kwargs: object) -> object:
        value = reads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    with mock.patch("tick.gh", return_value=""):
        with mock.patch("tick.gh_json", side_effect=read):
            assert not _release_in_flight_label(
                "pr", 42, "ai:fixing",
                tick_log=None, phase="harvest", stage="fix",
                required_hold_label="ai:needs-human-decision",
            )

    assert reads == [], "the confirmation read is the one that failed"


def test_release_verification_malformed_json_retains_custody(
    tmp_path: Path,
) -> None:
    """Junk on gh stdout is a failed confirmation, not a crashed tick.

    ``gh_json`` parses stdout with ``json.loads``, so a zero-exit ``gh`` that
    emits truncated or non-JSON output raises ``JSONDecodeError`` — a
    ``ValueError``, not ``CalledProcessError``. The removal has already landed
    by then, so an uncaught raise escapes to the tick_crash handler and stops
    the loop instead of logging the ``label_release_failed`` entry the operator
    is told to grep for. Every other hold read in this module catches both.
    """
    from tick import TickLog, _release_in_flight_label  # type: ignore[import-untyped]

    held = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}
    reads = [held, json.JSONDecodeError("Expecting value", "not json", 0)]

    def read(*_args: str, **_kwargs: object) -> object:
        value = reads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    with mock.patch("tick.gh", return_value=""):
        with mock.patch("tick.gh_json", side_effect=read):
            assert not _release_in_flight_label(
                "pr", 42, "ai:fixing",
                tick_log=tick_log, phase="harvest", stage="fix",
                required_hold_label="ai:needs-human-decision",
            )

    assert reads == [], "the confirmation read is the one that failed"
    assert [
        entry for entry in tick_log.tail()
        if entry.get("action") == "label_release_failed"
    ]


def test_read_authoritative_human_holds_rejects_malformed_labels() -> None:
    """A malformed label payload is unknown custody, never 'nothing held'."""
    from tick import read_authoritative_human_holds  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json", return_value={"labels": [{"name": 7}]}):
        with pytest.raises(ValueError):
            read_authoritative_human_holds({("pr", 42, "ai:fixing")})


def test_observe_loop_state_human_hold_read_failure_is_not_quiescent() -> None:
    """An unavailable custody read stays warm instead of claiming a stop."""
    import tick  # type: ignore[import-untyped]

    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        with mock.patch(
            "tick.read_authoritative_human_holds",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            with mock.patch("tick._scan_marker_identities", return_value=[]):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        state = tick._observe_loop_state()

    assert state["in_flight_count"] == tick.pipeline.DEFAULT_MAX_IN_FLIGHT
    assert state["actionable_queue"] is True
    assert state["quiescent"] is False


def test_read_authoritative_in_flight_labels_raises_on_failure() -> None:
    """Raises CalledProcessError when any gh query fails (fail-closed)."""
    from tick import read_authoritative_in_flight_labels  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json:
        gh_json.side_effect = subprocess.CalledProcessError(1, ["gh"])
        with pytest.raises(subprocess.CalledProcessError):
            read_authoritative_in_flight_labels()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [None],
        [{"number": True}],
        [{"number": 0}],
        [{"number": -1}],
    ],
)
def test_read_authoritative_in_flight_labels_rejects_malformed_payload(
    payload: object,
) -> None:
    """Any malformed authoritative row invalidates the whole admission read."""
    from tick import read_authoritative_in_flight_labels  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json:
        gh_json.side_effect = [payload, [], []]
        with pytest.raises(ValueError, match="authoritative in-flight payload"):
            read_authoritative_in_flight_labels()


# --- can_dispatch_now (Step 7) ---


def test_can_dispatch_now_returns_true_when_slots_free() -> None:
    """can_dispatch_now() allows dispatch below max_in_flight."""
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    with mock.patch("tick._scan_marker_identities") as scan:
        scan.return_value = []  # no local markers
        result = can_dispatch_now(max_in_flight=3)

    assert result.can_dispatch is True
    assert result.in_flight_count == 0


def test_can_dispatch_now_returns_false_at_max_in_flight() -> None:
    """can_dispatch_now() denies dispatch at max_in_flight."""
    import pipeline as _pipeline
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    marker_states = [
        ("pr", 1, _pipeline.LABEL_REVIEWING, "live", "matched", True),
        ("pr", 2, _pipeline.LABEL_REVIEWING, "live", "matched", True),
        ("issue", 3, _pipeline.LABEL_IMPLEMENTING, "live", "matched", True),
    ]
    with mock.patch("tick._scan_marker_identities", return_value=marker_states):
        result = can_dispatch_now(max_in_flight=3)

    assert result.can_dispatch is False
    assert result.reason == "max_in_flight"
    assert result.in_flight_count == 3


def test_can_dispatch_now_ignores_in_flight_labels_this_tick_does_not_own() -> None:
    """An in-flight label with no local engine never consumes a spend slot.

    ``ai:implementing`` is a shared workflow state (``CLAUDE.md`` tells
    contributors to set it by hand before writing code), not a pipeline
    lease. Counting it as a paid engine let stale manual pickups starve
    dispatch permanently — the cap bounds *this* tick's engines, and only a
    marker proves one exists.

    The 2026-08-20 incident's four ``ai:implementing`` issues are live in the
    environment here. Admission has no parameter left to receive them, so the
    proof is that it never reaches for them: an admission re-wired to consult
    the repo-wide reader again fails on ``call_count`` rather than passing
    because the test forgot to supply the labels it names.
    """
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    with mock.patch(
        "tick.gh_json", side_effect=gh_json_four_foreign_implementing_labels,
    ):
        with mock.patch("tick.read_authoritative_in_flight_labels") as reader:
            with mock.patch("tick._scan_marker_identities") as scan:
                scan.return_value = []  # this tick launched nothing
                result = can_dispatch_now(max_in_flight=3)

    assert reader.call_count == 0, (
        "admission read the repo-wide in-flight labels; the cap counts the "
        "markers this machine wrote"
    )
    assert result.can_dispatch is True
    assert result.in_flight_count == 0


def test_can_dispatch_now_fail_closed_on_corrupt_marker() -> None:
    """can_dispatch_now() returns admission_error on corrupt marker."""
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    with mock.patch("tick._scan_marker_identities") as scan:
        scan.side_effect = RuntimeError("corrupt dispatch.json")
        result = can_dispatch_now(max_in_flight=3)

    assert result.can_dispatch is False
    assert result.reason == "admission_error"


def test_can_dispatch_now_counts_hung_marker_without_pipeline_label() -> None:
    """A retained live group occupies the hard spend slot after escalation."""
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    with mock.patch(
        "tick._scan_marker_identities",
        return_value=[("pr", 42, "ai:reviewing", "hung", "unknown", True)],
    ):
        result = can_dispatch_now(max_in_flight=1)

    assert result.can_dispatch is False
    assert result.reason == "max_in_flight"
    assert result.in_flight_count == 1


def test_can_dispatch_now_counts_same_number_issue_and_pr_separately() -> None:
    """Issue #42 and PR #42 are distinct paid engines at the global cap."""
    import pipeline  # type: ignore[import-untyped]
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    marker_states = [
        ("issue", 42, pipeline.LABEL_IMPLEMENTING, "live", "matched", True),
        ("pr", 42, pipeline.LABEL_REVIEWING, "live", "matched", True),
        ("pr", 43, pipeline.LABEL_FIXING, "hung", "matched", True),
    ]
    with mock.patch("tick._scan_marker_identities", return_value=marker_states):
        result = can_dispatch_now(max_in_flight=3)

    assert result.in_flight_count == 3
    assert result.can_dispatch is False
    assert result.reason == "max_in_flight"


# --- reconcile_engine (Step 8) ---


def test_reconcile_engine_noop_when_no_items(tmp_path: Path) -> None:
    """reconcile_engine() returns empty list when reconcile set is empty."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review",
        worktree_prefix="pr",
        attempts_key_prefix="",
        result_file="review.json",
        in_flight_label="ai:reviewing",
        pre_label="ai:implemented",
        strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    with mock.patch("tick.gh_text") as gh_text:
        gh_text.return_value = ""  # no PRs returned
        results, ok = reconcile_engine(config, tick_log=None, target=None,
                                       wt_root=str(tmp_path))
    assert results == []
    assert ok is True


def test_reconcile_engine_skips_closed_pr(tmp_path: Path) -> None:
    """reconcile_engine() cleans up scratch for closed/merged PRs."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review",
        worktree_prefix="pr",
        attempts_key_prefix="",
        result_file="review.json",
        in_flight_label="ai:reviewing",
        pre_label="ai:implemented",
        strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    with mock.patch("tick.gh_text") as gh_text:
        # PR 42 exists and is MERGED
        gh_text.side_effect = lambda *args: {
            ("pr", "list", "--label", "ai:reviewing", "--state", "open",
             "--json", "number", "-q", ".[].number"): "42",
        }.get(tuple(args), "")
        with mock.patch("tick.gh_json") as gh_json:
            gh_json.return_value = {"state": "MERGED"}
            with mock.patch("tick.cleanup_scratch"):
                results, ok = reconcile_engine(config, tick_log=None, target=None,
                                               wt_root=str(tmp_path))
    # Should call cleanup for the closed PR (via _reconcile_one)
    # Note: cleanup now happens inside _reconcile_one via the identity check
    assert ok is True


def test_reconcile_engine_target_filtering(tmp_path: Path) -> None:
    """reconcile_engine() filters to a single target PR."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review",
        worktree_prefix="pr",
        attempts_key_prefix="",
        result_file="review.json",
        in_flight_label="ai:reviewing",
        pre_label="ai:implemented",
        strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    with mock.patch("tick.gh_text") as gh_text:
        # PRs 42 and 99 exist
        gh_text.side_effect = lambda *args: {
            ("pr", "list", "--label", "ai:reviewing", "--state", "open",
             "--json", "number", "-q", ".[].number"): "42\n99",
        }.get(tuple(args), "")
        with mock.patch("tick.gh_json") as gh_json:
            gh_json.return_value = {"state": "OPEN", "labels": []}
            with mock.patch("tick._reconcile_one") as recon_one:
                recon_one.return_value = {"action": "noop", "label": "", "kill": False,
                                           "identity": "unknown"}
                results, ok = reconcile_engine(config, tick_log=None, target=("pr", 42),
                                               wt_root=str(tmp_path))
    # Only one item should be reconciled
    assert len(results) == 1
    assert ok is True


# --- HARVEST phases (Step 9) ---


def test_harvest_reviewers_calls_reconcile_engine(tmp_path: Path) -> None:
    """harvest_reviewers() delegates to reconcile_engine with reviewer config."""
    from tick import harvest_reviewers  # type: ignore[import-untyped]

    with mock.patch("tick.reconcile_engine") as recon:
        recon.return_value = ([], True)
        result = harvest_reviewers(tick_log=None, target=None, wt_root=str(tmp_path))
    assert result is True  # reconcile_ok
    recon.assert_called_once()


def test_harvest_implementers_calls_reconcile_engine(tmp_path: Path) -> None:
    """harvest_implementers() delegates to reconcile_engine with implementer config."""
    from tick import harvest_implementers  # type: ignore[import-untyped]

    with mock.patch("tick.reconcile_engine") as recon:
        recon.return_value = ([], True)
        result = harvest_implementers(tick_log=None, target=None, wt_root=str(tmp_path))
    assert result is True
    recon.assert_called_once()


def test_harvest_fixers_calls_reconcile_engine(tmp_path: Path) -> None:
    """harvest_fixers() delegates to reconcile_engine with fixer config."""
    from tick import harvest_fixers  # type: ignore[import-untyped]

    with mock.patch("tick.reconcile_engine") as recon:
        recon.return_value = ([], True)
        result = harvest_fixers(tick_log=None, target=None, wt_root=str(tmp_path))
    assert result is True
    recon.assert_called_once()


# --- main() (Step 11) ---


def test_main_runs_harvest_and_dispatch(tmp_path: Path) -> None:
    """main() runs HARVEST phases then DISPATCH phases."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.return_value = True
            with mock.patch("tick.harvest_implementers") as hi:
                hi.return_value = True
                with mock.patch("tick.harvest_fixers") as hf:
                    hf.return_value = True
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels"
                    ) as rl:
                        rl.return_value = []
                        with mock.patch("tick.dispatch_reviewers") as dr:
                            with mock.patch(
                                "tick.dispatch_implementers"
                            ) as di:
                                with mock.patch(
                                    "tick.dispatch_fixers"
                                ) as df:
                                    result = main()

    assert result == 0
    hr.assert_called_once()
    hi.assert_called_once()
    hf.assert_called_once()
    dr.assert_called_once()
    di.assert_called_once()
    df.assert_called_once()


def test_main_returns_nonzero_when_reconcile_failed(tmp_path: Path) -> None:
    """main() returns non-zero when any HARVEST phase returns False."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.return_value = False  # reconciliation failed
            with mock.patch("tick.harvest_implementers") as hi:
                hi.return_value = True
                with mock.patch("tick.harvest_fixers") as hf:
                    hf.return_value = True
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels"
                    ) as rl:
                        rl.return_value = []
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    result = main()

    assert result != 0  # non-zero: reconcile failed


def test_main_target_parsing_pr(tmp_path: Path) -> None:
    """main() with target='pr:42' restricts to that PR."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.return_value = True
            with mock.patch("tick.harvest_implementers") as hi:
                hi.return_value = True
                with mock.patch("tick.harvest_fixers") as hf:
                    hf.return_value = True
                    with mock.patch("tick.read_authoritative_in_flight_labels") as rl:
                        rl.return_value = []
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    result = main(target="pr:42")

    assert result == 0
    # All harvest phases called with target=("pr", 42) as second positional arg
    assert hr.call_args[0][1] == ("pr", 42)
    assert hi.call_args[0][1] == ("pr", 42)
    assert hf.call_args[0][1] == ("pr", 42)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"closedByPullRequestsReferences": "not-a-list"},
        {"closedByPullRequestsReferences": [{"number": "551"}]},
        {"closedByPullRequestsReferences": [{"number": 551}]},
        {
            "closedByPullRequestsReferences": [
                {
                    "id": "PR_551",
                    "number": 551,
                    "repository": {"name": "fork", "owner": {"login": "other"}},
                    "url": "https://github.com/other/fork/pull/551",
                },
            ],
        },
    ],
)
def test_resolve_stage_targets_rejects_malformed_or_ambiguous_issue_links(
    payload: object,
) -> None:
    """Issue target resolution fails closed unless zero or one open PR is valid."""
    from tick import _resolve_stage_targets  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json", return_value=payload):
        with pytest.raises(ValueError):
            _resolve_stage_targets(("issue", 462))


def test_resolve_stage_targets_reads_issue_closing_pr_scope() -> None:
    """An issue target maps its one open closing PR to review and fix stages."""
    from tick import _resolve_stage_targets  # type: ignore[import-untyped]

    payload = {
        "closedByPullRequestsReferences": [
            closing_pr_reference(550),
            closing_pr_reference(551),
        ],
    }
    with mock.patch(
        "tick.gh_json",
        side_effect=[
            payload,
            {"number": 550, "state": "MERGED"},
            {"number": 551, "state": "OPEN"},
        ],
    ) as gh_json_mock:
        targets = _resolve_stage_targets(("issue", 462))

    assert gh_json_mock.call_args_list == [
        mock.call(
            "issue", "view", "462",
            "--json", "closedByPullRequestsReferences",
        ),
        mock.call(
            "pr", "view", "550",
            "--json", "state,number",
        ),
        mock.call(
            "pr", "view", "551",
            "--json", "state,number",
        ),
    ]
    assert targets == {
        "reviewer": ("pr", 551),
        "implementer": ("issue", 462),
        "fixer": ("pr", 551),
    }


def test_resolve_stage_targets_accepts_issue_without_open_closing_pr() -> None:
    """An issue without an open closing PR remains implementation-only scope."""
    from tick import _resolve_stage_targets  # type: ignore[import-untyped]

    payload = {
        "closedByPullRequestsReferences": [closing_pr_reference(550)],
    }
    with mock.patch(
        "tick.gh_json",
        side_effect=[payload, {"number": 550, "state": "MERGED"}],
    ):
        targets = _resolve_stage_targets(("issue", 462))

    assert targets == {
        "reviewer": ("issue", 462),
        "implementer": ("issue", 462),
        "fixer": ("issue", 462),
    }


def test_resolve_stage_targets_rejects_multiple_open_pr_state_reads() -> None:
    """Two same-repository open closing PRs are ambiguous and fail closed."""
    from tick import _resolve_stage_targets  # type: ignore[import-untyped]

    payload = {
        "closedByPullRequestsReferences": [
            closing_pr_reference(551),
            closing_pr_reference(552),
        ],
    }
    with mock.patch(
        "tick.gh_json",
        side_effect=[
            payload,
            {"number": 551, "state": "OPEN"},
            {"number": 552, "state": "OPEN"},
        ],
    ):
        with pytest.raises(ValueError, match="multiple open"):
            _resolve_stage_targets(("issue", 462))


def test_resolve_stage_targets_rejects_mismatched_pr_state_response() -> None:
    """A PR state read for a different number is malformed and fails closed."""
    from tick import _resolve_stage_targets  # type: ignore[import-untyped]

    payload = {"closedByPullRequestsReferences": [closing_pr_reference(551)]}
    with mock.patch(
        "tick.gh_json",
        side_effect=[payload, {"number": 999, "state": "OPEN"}],
    ):
        with pytest.raises(ValueError, match="invalid number or state"):
            _resolve_stage_targets(("issue", 462))


def test_resolve_stage_targets_rejects_float_pr_state_number() -> None:
    """A numerically equal float PR number is malformed and fails closed."""
    from tick import _resolve_stage_targets  # type: ignore[import-untyped]

    payload = {"closedByPullRequestsReferences": [closing_pr_reference(551)]}
    with mock.patch(
        "tick.gh_json",
        side_effect=[payload, {"number": 551.0, "state": "OPEN"}],
    ):
        with pytest.raises(ValueError, match="invalid number or state"):
            _resolve_stage_targets(("issue", 462))


# --- Adversarial review: B4 — jq filter parses gh output correctly ---


def test_reconcile_engine_parses_gh_json_object_output(tmp_path: Path) -> None:
    """reconcile_engine must parse actual gh --json number -q ".[].number" output.

    The gh CLI outputs one digit per line when using -q ".[].number".
    The old -q ".[]" would output JSON objects like {"number":1}, which
    isdigit() rejects — silently producing zero results.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    with mock.patch("tick.gh_text") as gh_text:
        # Simulate gh output with actual digits (correct jq filter)
        gh_text.return_value = "42\n99\n"
        with mock.patch("tick.gh_json") as gh_json:
            gh_json.return_value = {"state": "OPEN", "labels": []}
            with mock.patch("tick._reconcile_one") as recon_one:
                recon_one.return_value = {
                    "action": "noop", "label": "", "kill": False,
                    "identity": "unknown", "pr_number": 42,
                }
                results, ok = reconcile_engine(
                    config, tick_log=None, target=None, wt_root=str(tmp_path),
                )
    # Both PRs 42 and 99 should be reconciled
    assert len(results) == 2
    assert ok is True


def test_reconcile_engine_rejects_json_object_lines_from_wrong_jq(tmp_path: Path) -> None:
    """reconcile_engine must NOT treat {"number":1} as a valid PR number.

    The old -q ".[]" filter would produce JSON objects, not digits.
    This test locks in that we parse digits, not JSON objects.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    with mock.patch("tick.gh_text") as gh_text:
        # Simulate old buggy output: JSON objects, not digits
        gh_text.return_value = '{"number":42}\n{"number":99}\n'
        with mock.patch("os.listdir") as listdir:
            listdir.return_value = []
            results, ok = reconcile_engine(
                config, tick_log=None, target=None, wt_root=str(tmp_path),
            )
    # JSON objects are not digits — zero items should be reconciled
    assert len(results) == 0
    assert ok is True


# --- Adversarial review: B1 — _execute_reconcile_actions with pr_number ---


def test_execute_reconcile_actions_relabels_when_pr_number_present() -> None:
    """_execute_reconcile_actions must call relabel when pr_number is set."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    decisions = [
        {
            "action": "revert",
            "label": "ai:implemented",
            "kill": False,
            "identity": "unknown",
            "pr_number": 42,
        },
    ]
    with mock.patch("tick.relabel") as relabel_mock:
        _execute_reconcile_actions(decisions, config, wt_root="/tmp")
        relabel_mock.assert_called_once_with("pr", 42, "ai:reviewing", "ai:implemented")


def test_execute_reconcile_actions_skips_when_pr_number_missing() -> None:
    """_execute_reconcile_actions must skip items without pr_number.

    This is the bug: decisions without pr_number were silently skipped.
    The test verifies we never call relabel when pr_number is absent.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    decisions = [
        {
            "action": "revert",
            "label": "ai:implemented",
            "kill": False,
            "identity": "unknown",
            # pr_number intentionally missing
        },
    ]
    with mock.patch("tick.relabel") as relabel_mock:
        _execute_reconcile_actions(decisions, config, wt_root="/tmp")
        relabel_mock.assert_not_called()


def test_execute_reconcile_actions_cleanup_calls_cleanup_scratch() -> None:
    """_execute_reconcile_actions must call cleanup_scratch for cleanup actions."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    decisions = [
        {
            "action": "cleanup",
            "label": "",
            "kill": False,
            "identity": "unknown",
            "pr_number": 42,
        },
    ]
    with mock.patch("tick.cleanup_scratch") as cleanup_mock:
        _execute_reconcile_actions(decisions, config, wt_root="/tmp")
        cleanup_mock.assert_called_once()


def test_execute_reconcile_actions_archives_retained_reviewer_before_cleanup(
    tmp_path: Path,
) -> None:
    """Out-of-custody reviewer evidence is durable before HARVEST cleanup."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "review-42"}))
    (wt_dir / "review.json").write_text('{"verdict": "changes_requested"}')
    (wt_dir / "completion.json").write_text('{"exit_status": 0}')
    decisions = [{
        "action": "cleanup", "label": "", "kill": False,
        "identity": "unknown", "pr_number": 42,
    }]

    ok = _execute_reconcile_actions(decisions, config, wt_root=str(tmp_path))

    assert ok is True
    assert not wt_dir.exists()
    archive = tmp_path / ".harvest-evidence" / "pr-42-review-42"
    assert (archive / "review.json").is_file()
    assert (archive / "completion.json").is_file()


def test_execute_reconcile_actions_retains_scratch_when_archive_fails(
    tmp_path: Path,
) -> None:
    """HARVEST cleanup is gated when retained evidence cannot be archived."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "changes_requested"}')
    decisions = [{
        "action": "cleanup", "label": "", "kill": False,
        "identity": "unknown", "pr_number": 42,
    }]

    with mock.patch("tick._archive_harvest_evidence", return_value=""):
        ok = _execute_reconcile_actions(decisions, config, wt_root=str(tmp_path))

    assert ok is False
    assert wt_dir.is_dir()


def test_escalate_dispatch_setup_failure_retains_scratch_when_relabel_fails(
    tmp_path: Path,
) -> None:
    """Setup escalation keeps local custody until the remote label succeeds."""
    from tick import _escalate_dispatch_setup_failure  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()

    def record_gh(*args: str) -> object:
        if args[:2] == ("issue", "edit"):
            raise subprocess.CalledProcessError(1, ["gh", *args])
        return mock.Mock(stdout="https://example.invalid/issues/42#c1")

    with mock.patch("tick.gh", side_effect=record_gh):
        with mock.patch("tick.cleanup_scratch") as cleanup_mock:
            _escalate_dispatch_setup_failure(
                entity="issue",
                number=42,
                pre_label="ai:available",
                stage="implement",
                reason="fetch failed",
                engine="claude",
                cleanup_dir=str(wt_dir),
            )

    cleanup_mock.assert_not_called()
    assert wt_dir.is_dir()


def test_dispatch_setup_failure_publishes_before_the_relabel(
    tmp_path: Path,
) -> None:
    """A setup failure explains itself on the item before it goes red.

    Nothing else records this transition remotely: no engine ran, so there is no
    ``.harvest-evidence/`` bundle, and the scratch is deleted on the way out. An
    operator who opens the item would otherwise find ``ai:needs-human-decision``
    with the only account of it in a rotated, machine-local tick log.
    """
    from tick import _escalate_dispatch_setup_failure  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    calls: list[str] = []
    bodies: list[str] = []

    def record_gh(*args: str) -> object:
        if args[:2] == ("issue", "comment"):
            calls.append("comment")
            with open(args[4]) as fh:
                bodies.append(fh.read())
        elif args[:2] == ("issue", "edit"):
            calls.append(f"edit:{args[4]}")
        return mock.Mock(stdout="https://example.invalid/issues/42#c1")

    with mock.patch("tick.gh", side_effect=record_gh):
        _escalate_dispatch_setup_failure(
            entity="issue",
            number=42,
            pre_label="ai:available",
            stage="implement",
            reason="failed to create implementer worktree",
            engine="claude",
            cleanup_dir=str(wt_dir),
        )

    assert calls == ["comment", "edit:ai:needs-human-decision"]
    assert "implement" in bodies[0]
    assert "failed to create implementer worktree" in bodies[0]
    assert "ai:needs-human-decision" in bodies[0]
    # The label landed, so the scratch is still cleared afterwards.
    assert not wt_dir.exists()


def test_dispatch_setup_failure_failed_post_holds_the_label(
    tmp_path: Path,
) -> None:
    """A failed publication leaves the item exactly where the next tick can retry.

    Moving the label anyway would produce the silent red label this publication
    exists to prevent, and it is unrecoverable: the transition happens once,
    while the post can be retried for as long as the item still carries its
    pre-dispatch label.

    The scratch goes with it. Three call sites fire after the ``launching``
    lease is written, so a retained scratch is a phantom lease no engine owns —
    and because HARVEST runs before DISPATCH, the next tick would reconcile it
    to the terminal label with no comment instead of retrying the post.
    """
    from tick import _escalate_dispatch_setup_failure  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    calls: list[str] = []

    def record_gh(*args: str) -> object:
        if args[:2] == ("issue", "comment"):
            raise subprocess.CalledProcessError(1, ["gh", *args])
        calls.append(f"edit:{args[4]}")
        return mock.Mock(stdout="")

    with mock.patch("tick.gh", side_effect=record_gh):
        _escalate_dispatch_setup_failure(
            entity="issue",
            number=42,
            pre_label="ai:available",
            stage="implement",
            reason="failed to create implementer worktree",
            engine="claude",
            cleanup_dir=str(wt_dir),
        )

    assert calls == []
    # No relabel, and no phantom lease left for the next tick's harvest to
    # reconcile into a silent terminal label.
    assert not wt_dir.exists()


def test_held_setup_failure_leaves_no_marker_for_harvest_to_escalate(
    tmp_path: Path,
) -> None:
    """The hold must survive the next tick, which harvests before it dispatches.

    This is the interleaving the per-function tests cannot see. A held
    escalation that left its ``launching`` lease behind would be reconciled by
    the *next* tick's harvest — ``reconcile_dispatch`` answers ``escalate`` for
    a launching marker whose item still sits at its pre-dispatch label — landing
    the terminal label with no comment and clearing the attempt budget the hold
    just spent. The retry would never happen, and the item would end up exactly
    where this change exists to stop it ending up.
    """
    import pipeline  # type: ignore[import-untyped]
    from tick import _escalate_dispatch_setup_failure  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    # The lease three call sites write before anything is spawned.
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "implement",
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:available",
        "dispatch_id": "impl-42-lease",
        "state": "launching",
    }))

    def record_gh(*args: str) -> object:
        if args[1] == "comment":
            raise subprocess.CalledProcessError(1, ["gh", *args])
        raise AssertionError(f"a held escalation must not relabel: {args!r}")

    with mock.patch("tick.gh", side_effect=record_gh):
        _escalate_dispatch_setup_failure(
            entity="issue",
            number=42,
            pre_label="ai:available",
            stage="implement",
            reason="failed to resolve implementer engine",
            engine="claude",
            cleanup_dir=str(wt_dir),
        )

    assert not (wt_dir / "dispatch.json").exists()

    # And name what would have happened had it survived: reconcile answers a
    # launching lease still sitting at its pre-dispatch label with an
    # unpublished escalation to the terminal label.
    assert pipeline.reconcile_dispatch(
        classify_state="launching",
        current_label="ai:available",
        in_flight_label="ai:implementing",
        pre_label="ai:available",
        marker_valid=True,
    )["label"] == "ai:needs-human-decision"


def test_dispatch_escalation_does_not_repost_while_the_relabel_keeps_failing(
    tmp_path: Path,
) -> None:
    """The retry that follows a failed relabel reuses the landed comment.

    Every tick re-attempts the same failed setup, so a receipt keyed to a
    per-run dispatch id would never match and the item would collect one
    identical escalation comment per tick. The receipt is keyed to the decision
    instead, which is what makes the retry idempotent.
    """
    from tick import _escalate_dispatch_setup_failure  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    calls: list[str] = []

    def record_gh(*args: str) -> object:
        calls.append(args[1])
        if args[1] == "edit":
            raise subprocess.CalledProcessError(1, ["gh", *args])
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=0,
            stdout="https://example.invalid/issues/42#c1",
        )

    with mock.patch("tick.gh", side_effect=record_gh):
        for _ in range(2):
            _escalate_dispatch_setup_failure(
                entity="issue",
                number=42,
                pre_label="ai:available",
                stage="implement",
                reason="failed to create implementer worktree",
                engine="claude",
                cleanup_dir=str(wt_dir),
            )

    assert calls == ["comment", "edit", "edit"]
    assert wt_dir.is_dir()


def test_dispatch_attempts_counts_into_the_dispatching_stage_s_counter() -> None:
    """Each stage's escalation must bound against the file its own harvest reads.

    The counter file is chosen by prefix, and resolving it from the entity would
    send every PR stage to the reviewer's. That is invisible today because
    ``review`` and ``fix`` happen to share a prefix — a silent dependency that
    would stop bounding the fixer cap the moment the two diverged.
    """
    import tick  # type: ignore[import-untyped]
    from tick import _dispatch_attempts  # type: ignore[import-untyped]

    for stage, config in (
        ("review", tick._REVIEWER_CONFIG),
        ("implement", tick._IMPLEMENTER_CONFIG),
        ("fix", tick._FIXER_CONFIG),
    ):
        counter = _dispatch_attempts(stage)
        counter.incr(7)
        assert (
            Path(tick.WT_ROOT) / f"{config.attempts_key_prefix}7-attempts"
        ).is_file(), f"{stage} escalation counted into another stage's counter"
        counter.clear(7)


def test_dispatch_escalation_retires_only_its_own_receipt(
    tmp_path: Path,
) -> None:
    """A landed dispatch escalation must not retire a harvest's receipt.

    An ambiguous-marker escalation fires *over a retained marker*, so a harvest
    can be mid-retry behind one, holding a receipt keyed to that run's
    ``dispatch_id``. Dropping it here would repost a decision this path never
    made. Only the dispatch receipt is this path's to retire — and it must be,
    because its key is a constant that no later dispatch can fail to match.
    """
    import tick  # type: ignore[import-untyped]
    from tick import (  # type: ignore[import-untyped]
        _comment_receipt_path,
        _end_dispatch_escalation,
    )

    fix_receipt = Path(
        _comment_receipt_path(tick.WT_ROOT, "pr", 42, tick._FIX_COMMENT_KIND),
    )
    dispatch_receipt = Path(
        _comment_receipt_path(
            tick.WT_ROOT, "pr", 42, tick._DISPATCH_COMMENT_KIND,
        ),
    )
    fix_receipt.parent.mkdir(parents=True, exist_ok=True)
    fix_receipt.write_text(json.dumps({"dispatch_id": "fix-42-live"}))
    dispatch_receipt.write_text(json.dumps({"dispatch_id": "dispatch-escalation"}))

    _end_dispatch_escalation("pr", 42, "fix")

    assert not dispatch_receipt.exists()
    assert fix_receipt.is_file()


def test_dispatch_escalation_explains_a_recurrence_after_the_label_landed(
    tmp_path: Path,
) -> None:
    """The receipt is retired by the transition it gated, not left on disk.

    A receipt that outlived its transition would silently suppress the next
    occurrence of the same failure — after a human investigated, reset the
    label, and handed the item back — and the item would go red unexplained
    exactly the way it does today.
    """
    from tick import _escalate_dispatch_setup_failure  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    calls: list[str] = []

    def record_gh(*args: str) -> object:
        calls.append(args[1])
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=0,
            stdout="https://example.invalid/issues/42#c1",
        )

    with mock.patch("tick.gh", side_effect=record_gh):
        for _ in range(2):
            wt_dir.mkdir()
            _escalate_dispatch_setup_failure(
                entity="issue",
                number=42,
                pre_label="ai:available",
                stage="implement",
                reason="failed to create implementer worktree",
                engine="claude",
                cleanup_dir=str(wt_dir),
            )

    assert calls == ["comment", "edit", "comment", "edit"]


def test_dispatch_setup_failure_post_failure_terminates_at_the_attempt_cap(
    tmp_path: Path,
) -> None:
    """Gating without a bound would hold the item at its pickup label forever.

    A permanently unpostable comment (a repo whose issues are locked, a body
    GitHub rejects) must not leave the item looking dispatchable to every future
    tick. The durable attempt counter bounds it: the same exemption the attempt
    cap already takes, reached the same way, and the counter is cleared on the
    way out so the item's next cycle starts from zero.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _escalate_dispatch_setup_failure,
    )

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    calls: list[str] = []

    def record_gh(*args: str) -> object:
        if args[:2] == ("issue", "comment"):
            raise subprocess.CalledProcessError(1, ["gh", *args])
        calls.append(f"edit:{args[4]}")
        return mock.Mock(stdout="")

    with mock.patch("tick.gh", side_effect=record_gh):
        for _ in range(pipeline.MAX_DISPATCH_ATTEMPTS + 1):
            _escalate_dispatch_setup_failure(
                entity="issue",
                number=42,
                pre_label="ai:available",
                stage="implement",
                reason="failed to create implementer worktree",
                engine="claude",
                tick_log=tick_log,
                cleanup_dir=str(wt_dir),
            )

    # Only the capped attempt relabels; the ones under the cap held the item.
    assert calls == ["edit:ai:needs-human-decision"]
    assert not wt_dir.exists()
    assert Attempts(tick.WT_ROOT, "issue-").read(42) == 0
    assert any(
        entry["action"] == "escalation_comment_failed"
        for entry in tick_log.tail()
    )


def test_execute_reconcile_actions_cleanup_clears_attempt_counter(
    tmp_path: Path,
) -> None:
    """Cleanup ends dispatch custody, so the durable attempt counter must be
    cleared — a stale count would carry into the item's next dispatch cycle
    and trigger the attempt cap early."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    decisions = [{
        "action": "cleanup", "label": "", "kill": False,
        "identity": "unknown", "pr_number": 42,
    }]
    attempts = mock.Mock()

    ok = _execute_reconcile_actions(
        decisions, config, wt_root=str(tmp_path), attempts=attempts,
    )

    assert ok is True
    attempts.clear.assert_called_once_with(42)


def test_execute_reconcile_actions_escalate_with_issue_entity() -> None:
    """_execute_reconcile_actions must relabel issues with gh issue edit.

    The implementer stage operates on issues. relabel must use the correct
    entity type derived from config.worktree_prefix.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    decisions = [
        {
            "action": "escalate",
            "label": "ai:needs-human-decision",
            "kill": False,
            "identity": "unknown",
            "pr_number": 7,
        },
    ]
    label_reads = [
        {"labels": [{"name": "ai:implementing"}]},
        {"labels": [{"name": "ai:needs-human-decision"}]},
    ]
    with mock.patch("tick.gh_json", side_effect=label_reads):
        with mock.patch("tick.gh") as gh_mock:
            ok = _execute_reconcile_actions(decisions, config, wt_root="/tmp")
    assert ok is True
    # Must use gh issue edit, not gh pr edit
    gh_mock.assert_called_once_with(
        "issue", "edit", "7",
        "--add-label", "ai:needs-human-decision",
        "--remove-label", "ai:implementing",
    )


# --- Adversarial review: M1 — can_dispatch_now marker suppression ---


def test_can_dispatch_now_stale_marker_does_not_suppress_a_live_sibling_stage() -> None:
    """A dead marker must not mask a live engine at another stage on one PR.

    Occupancy is keyed ``(kind, number, label)``, so a PR carrying a dead
    reviewer marker and a live fixer marker holds exactly the fixer's slot:
    the dead one must neither be counted nor suppress its live sibling.
    """
    import pipeline as _pipeline
    from tick import can_dispatch_now  # type: ignore[import-untyped]

    marker_states = [
        ("pr", 42, _pipeline.LABEL_REVIEWING, "stale", "dead", False),
        ("pr", 42, _pipeline.LABEL_FIXING, "live", "matched", True),
    ]
    with mock.patch("tick._scan_marker_identities", return_value=marker_states):
        result = can_dispatch_now(max_in_flight=1)

    assert result.can_dispatch is False
    assert result.in_flight_count == 1


# --- Adversarial review: M4 — gh failure logging ---


def test_reconcile_one_logs_gh_failure_in_lifecycle_check(tmp_path: Path) -> None:
    """_reconcile_one must log when gh fails during lifecycle check."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import TickLog, _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path))

    with mock.patch("tick.gh_text") as gh_text:
        gh_text.side_effect = subprocess.CalledProcessError(1, ["gh"])
        decision = _reconcile_one(42, str(tmp_path), config, tick_log)

    assert decision["action"] == "noop"
    # Verify the failure was logged
    entries = tick_log.tail()
    assert len(entries) >= 1
    assert any(e.get("action") == "gh_failed" for e in entries)


# --- Markerless custody (issue #592) ---


@pytest.mark.parametrize(
    ("stage", "in_flight_label", "pre_label", "prefix"),
    [
        ("implement", "ai:implementing", "ai:available", "issue"),
        ("review", "ai:reviewing", "ai:implemented", "pr"),
        ("fix", "ai:fixing", "ai:changes-requested", "pr"),
    ],
)
def test_reconcile_engine_is_non_destructive_against_an_empty_marker_root(
    tmp_path: Path, stage: str, in_flight_label: str, pre_label: str, prefix: str,
) -> None:
    """A tick that can see no markers must not act on anything.

    This is the incident of 2026-08-01 in miniature: two sessions ran ticks
    from sibling worktrees, each resolving an empty marker root, and each
    escalated every healthy in-flight item in the repo. The marker space is
    machine-local, so "empty" is the normal reading of another machine's work
    — and of a human's manual pickup.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage=stage, worktree_prefix=prefix, attempts_key_prefix=f"{prefix}-",
        result_file="result.json", in_flight_label=in_flight_label,
        pre_label=pre_label, strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    def fake_gh_text(*args: str) -> str:
        return "77" if "list" in args else "OPEN"

    with mock.patch("tick.gh_text", side_effect=fake_gh_text):
        with mock.patch("tick.gh_json", side_effect=stub_gh_json(labels=[in_flight_label])):
            with mock.patch("tick.gh") as gh_mock:
                decisions, ok = reconcile_engine(
                    config, None, None, wt_root=str(wt_root),
                )

    assert ok is True
    assert [d["action"] for d in decisions] == ["noop"]
    assert [d["label"] for d in decisions] == [""]
    gh_mock.assert_not_called()
    assert not any(wt_root.iterdir()), "no scratch may be created or removed"


@pytest.mark.parametrize(
    ("stage", "in_flight_label", "pre_label", "prefix"),
    [
        ("implement", "ai:implementing", "ai:available", "issue"),
        ("review", "ai:reviewing", "ai:implemented", "pr"),
        ("fix", "ai:fixing", "ai:changes-requested", "pr"),
    ],
)
@pytest.mark.parametrize(
    "second_label",
    ["ai:revised", "ai:blocked", "ai:needs-human-decision"],
)
def test_markerless_dual_labels_are_not_escalated(
    tmp_path: Path, stage: str, in_flight_label: str, pre_label: str,
    prefix: str, second_label: str,
) -> None:
    """Two ``ai:`` labels do not license a mutation we cannot prove we own.

    Dual-label convergence runs before ``reconcile_dispatch``, so without this
    gate a markerless item carrying any second ``ai:`` label is escalated to
    ``ai:needs-human-decision`` and has its scratch archived — reintroducing
    exactly the destructive markerless path issue #592 removed, via a different
    door. A markerless dual-label state is the normal reading of another
    machine's dispatch caught mid-convergence, or of a human hold placed on
    live work.
    """
    import pipeline as _pipeline  # type: ignore[import-untyped]
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage=stage, worktree_prefix=prefix, attempts_key_prefix=f"{prefix}-",
        result_file="result.json", in_flight_label=in_flight_label,
        pre_label=pre_label, strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    def fake_gh_text(*args: str) -> str:
        return "77" if "list" in args else "OPEN"

    labels = [in_flight_label, second_label]
    with mock.patch("tick.gh_text", side_effect=fake_gh_text):
        with mock.patch("tick.gh_json", side_effect=stub_gh_json(labels=labels)):
            with mock.patch("tick.gh") as gh_mock:
                decisions, ok = reconcile_engine(
                    config, None, None, wt_root=str(wt_root),
                )

    assert ok is True
    # A human hold short-circuits earlier, at the hold gate, so it reports
    # "leave" rather than "noop". Both are non-mutating; what matters here is
    # that neither escalates, relabels, nor touches scratch.
    expected = "leave" if second_label in _pipeline.HOLD_LABELS else "noop"
    assert [d["action"] for d in decisions] == [expected]
    gh_mock.assert_not_called()
    assert not any(wt_root.iterdir()), "no scratch may be created or removed"


def test_a_manual_pickup_survives_repeated_ticks_unchanged(tmp_path: Path) -> None:
    """The documented manual pickup is stable, not merely lucky on tick one.

    CLAUDE.md tells a contributor to move an issue to ai:implementing before
    writing code. That state must be a fixed point of reconciliation for as
    long as the work takes, not something that decays after N ticks.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import reconcile_engine  # type: ignore[import-untyped]

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    def fake_gh_text(*args: str) -> str:
        return "77" if "list" in args else "OPEN"

    for _ in range(5):
        with mock.patch("tick.gh_text", side_effect=fake_gh_text):
            with mock.patch("tick.gh_json", side_effect=stub_gh_json(
                labels=["ai:implementing"],
            )):
                with mock.patch("tick.gh") as gh_mock:
                    decisions, ok = reconcile_engine(
                        config, None, None, wt_root=str(wt_root),
                    )

        assert ok is True
        assert [d["action"] for d in decisions] == ["noop"]
        gh_mock.assert_not_called()


@pytest.mark.parametrize("tick_saw_in_flight", [True, False])
@pytest.mark.parametrize(
    ("stage", "entity", "pickup_label", "in_flight_label"),
    [
        ("implement", "issue", "ai:available", "ai:implementing"),
        ("review", "pr", "ai:revised", "ai:reviewing"),
        ("fix", "pr", "ai:changes-requested", "ai:fixing"),
    ],
)
def test_dispatch_skips_a_candidate_that_already_carries_an_in_flight_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    entity: str,
    pickup_label: str,
    in_flight_label: str,
    tick_saw_in_flight: bool,
) -> None:
    """A partial label transition must not license a second paid engine.

    Reconciliation now leaves a markerless in-flight item alone, so an item
    caught mid-convergence on another machine keeps *both* its in-flight and
    its pickup label for as long as that engine runs. The pickup query still
    selects it. Without an eligibility gate, DISPATCH then reads the missing
    local marker as "free to dispatch", spends a second engine on work that is
    already in flight, and converges the remote state by removing the pickup
    label — the destructive markerless path issue #592 removed, re-entered
    through DISPATCH instead of HARVEST.

    Both eligibility layers are exercised. ``tick_saw_in_flight`` is the
    ordinary case: the identity is already in the authoritative set, so the
    early guard rejects it without a read. The ``False`` case is the one that
    layer cannot cover — ``read_authoritative_in_flight_labels`` goes through
    GitHub's search index, which lags the label write that the pickup query's
    point-in-time listing already reflects, so the item can be missing from
    the authoritative set while carrying an in-flight label right now. Only
    the point-read of the item's own complete label set catches that.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(tick, "WT_ROOT", str(wt_root))

    labels = [in_flight_label, pickup_label]

    def fake_gh_text(*args: str, **kwargs: object) -> str:
        if "list" in args:
            return "77"
        return "\n".join(labels)

    def fake_gh_json(*args: str, **kwargs: object) -> object:
        if "list" in args:
            return [{"number": 77, "title": "Candidate", "author": {"login": TRUSTED_AUTHOR}}]
        if "author,headRepository,isCrossRepository" in args:
            return {
                "author": {"login": tick.TRUSTED_REVIEW_AUTHOR},
                "headRepository": {"nameWithOwner": tick.TRUSTED_HEAD_REPO},
                "isCrossRepository": False,
            }
        return [{"name": name} for name in labels]

    in_flight = [
        pipeline.InFlightRef(
            kind="issue" if entity == "issue" else "pr",
            number=77,
            label=in_flight_label,
        ),
    ] if tick_saw_in_flight else []
    launcher = f"_dispatch_one_{'implementer' if stage == 'implement' else stage + 'er'}"

    with mock.patch("tick.gh_text", side_effect=fake_gh_text):
        with mock.patch("tick.gh_json", side_effect=fake_gh_json):
            with mock.patch("tick.verify_codex_bin"):
                with mock.patch("tick.gh") as gh_mock:
                    with mock.patch(f"tick.{launcher}") as launch_mock:
                        if stage == "implement":
                            tick.dispatch_implementers(
                                authoritative_in_flight=in_flight,
                            )
                        elif stage == "review":
                            tick.dispatch_reviewers(
                                authoritative_in_flight=in_flight,
                            )
                        else:
                            tick.dispatch_fixers(
                                authoritative_in_flight=in_flight,
                            )

    launch_mock.assert_not_called()
    gh_mock.assert_not_called()
    assert not any(wt_root.iterdir()), "no scratch may be created or removed"


@pytest.mark.parametrize("stage", ["implement", "review", "fix"])
def test_each_dispatcher_queries_exactly_its_stages_pickup_labels(
    stage: str,
) -> None:
    """The queue listing and the eligibility gate share one vocabulary.

    ``pipeline.STAGE_PICKUP_LABELS`` decides what the gate will admit and what
    the cadence probes scan. A dispatcher whose queue query drifts from it
    would silently stop selecting a label the rest of the control plane
    considers dispatchable work.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    queried: list[str] = []

    def record(*args: str, **kwargs: object) -> None:
        if "--label" in args:
            queried.append(args[args.index("--label") + 1])

    def record_text(*args: str, **kwargs: object) -> str:
        record(*args)
        return ""

    def record_json(*args: str, **kwargs: object) -> object:
        record(*args)
        return []

    dispatchers = {
        "implement": tick.dispatch_implementers,
        "review": tick.dispatch_reviewers,
        "fix": tick.dispatch_fixers,
    }

    with mock.patch("tick.gh_text", side_effect=record_text):
        with mock.patch("tick.gh_json", side_effect=record_json):
            with mock.patch("tick.verify_codex_bin"):
                dispatchers[stage](authoritative_in_flight=[])

    assert set(queried) == set(pipeline.STAGE_PICKUP_LABELS[stage])


@pytest.mark.parametrize("stage", ["review", "fix"])
def test_dispatch_skips_a_pr_carrying_two_stages_pickup_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    """Exactly-one pickup is counted across stages, not inside one.

    A PR at ``ai:implemented`` + ``ai:changes-requested`` shows the review
    stage exactly one of its labels and the fix stage exactly one of its own,
    so a per-stage count admits it twice. The second engine then finds the
    first's live marker, and because that marker records the sibling stage it
    reads as structurally invalid — escalating active work to
    ``ai:needs-human-decision``. Neither stage may take it.
    """
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(tick, "WT_ROOT", str(wt_root))

    labels = ["ai:implemented", "ai:changes-requested"]

    def fake_gh_text(*args: str, **kwargs: object) -> str:
        if "list" in args:
            return "77"
        return "\n".join(labels)

    def fake_gh_json(*args: str, **kwargs: object) -> object:
        if "author,headRepository,isCrossRepository" in args:
            return {
                "author": {"login": tick.TRUSTED_REVIEW_AUTHOR},
                "headRepository": {"nameWithOwner": tick.TRUSTED_HEAD_REPO},
                "isCrossRepository": False,
            }
        return [{"name": name} for name in labels]

    launcher = "_dispatch_one_reviewer" if stage == "review" else "_dispatch_one_fixer"
    dispatcher = tick.dispatch_reviewers if stage == "review" else tick.dispatch_fixers

    with mock.patch("tick.gh_text", side_effect=fake_gh_text):
        with mock.patch("tick.gh_json", side_effect=fake_gh_json):
            with mock.patch("tick.verify_codex_bin"):
                with mock.patch("tick.gh") as gh_mock:
                    with mock.patch(f"tick.{launcher}") as launch_mock:
                        dispatcher(authoritative_in_flight=[])

    launch_mock.assert_not_called()
    gh_mock.assert_not_called()
    assert not any(wt_root.iterdir()), "no scratch may be created or removed"


def test_reconcile_one_leaves_a_markerless_in_flight_item_alone(
    tmp_path: Path,
) -> None:
    """A markerless in-flight item is left untouched.

    This is the documented manual pickup (a human adds ai:implementing before
    writing code) and every item dispatched by another machine, whose markers
    live on that machine's disk. Neither is stranded work.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=stub_gh_json(labels=["ai:implementing"])):
            decision = _reconcile_one(7, str(tmp_path / "issue-7"), config, None)

    assert decision["action"] == "noop"
    assert decision["label"] == ""


# --- Adversarial review: M5 — cleanup_scratch only on matched identity ---


def test_reconcile_one_retains_scratch_on_mismatched_identity(tmp_path: Path) -> None:
    """_reconcile_one must NOT cleanup_scratch on mismatched identity.

    Mismatched = confirmed PID reuse. We know the original engine is gone
    but can't prove the result was harvested. Keep scratch for audit.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 50, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 999999.0, "pgid": 99999,
    }))

    with mock.patch("tick.gh_text") as gh_text:
        gh_text.return_value = "MERGED"
        with mock.patch("tick.gh_json") as gh_json:
            gh_json.return_value = {"state": "MERGED", "labels": []}
            with mock.patch("process_control.get_process_start_time") as gps:
                # Return a different start time → mismatched identity
                gps.return_value = 888888.0
                decision = _reconcile_one(42, str(wt_dir), config, tick_log=None)

    # On mismatched identity, scratch must be retained
    assert wt_dir.exists()
    assert decision["identity"] == "mismatched"


def test_reconcile_one_archives_scratch_after_terminal_engine_kill(
    tmp_path: Path,
) -> None:
    """A terminal item cannot discard work produced before its engine was killed."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 50, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42-terminal", "pid": 99999,
        "start_time": 1000.0, "pgid": 99999,
    }))
    (wt_dir / "recovery.txt").write_text("unfinished review evidence")
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", return_value={"labels": []}):
            with mock.patch(
                "tick.process_control.get_process_start_time", return_value=1000.0,
            ):
                with mock.patch(
                    "tick.process_control.terminate_process_group", return_value=True,
                ):
                    _reconcile_one(42, str(wt_dir), config, tick_log=None,
                                   attempts=attempts)

    assert not wt_dir.exists()
    archive = (
        tmp_path
        / ".harvest-evidence"
        / "pr-42-review-42-terminal"
        / "scratch"
    )
    assert (archive / "recovery.txt").read_text() == "unfinished review evidence"
    attempts.clear.assert_called_once_with(42)


def test_reconcile_one_retains_custody_on_unknown_identity(
    tmp_path: Path,
) -> None:
    """A terminal PR with unknown process identity must retain its marker.

    Unknown identity means the tick cannot prove the engine is dead or safe to
    kill. Removing the worktree here drops the only durable custody link.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 999999.0,
        "pgid": 99999,
    }))
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", return_value={"state": "MERGED", "labels": []}):
            with mock.patch(
                "process_control.get_process_start_time",
                return_value=None,
            ):
                with mock.patch("process_control.pid_is_alive", return_value=True):
                    with mock.patch("tick._remove_worktree") as remove_worktree:
                        decision = _reconcile_one(
                            42, str(wt_dir), config, tick_log=None,
                            attempts=attempts,
                        )

    assert wt_dir.exists()
    assert (wt_dir / "dispatch.json").is_file()
    assert decision["identity"] == "unknown"
    remove_worktree.assert_not_called()
    attempts.clear.assert_not_called()


def test_reconcile_one_archives_terminal_marker_when_pid_is_dead(
    tmp_path: Path,
) -> None:
    """A terminal PR with a provably dead PID archives evidence before cleanup."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 999999.0,
        "pgid": 99999,
    }))
    (wt_dir / "recovery.txt").write_text("dead engine evidence")
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", return_value={"labels": []}):
            with mock.patch(
                "process_control.get_process_start_time",
                return_value=None,
            ):
                with mock.patch("process_control.pid_is_alive", return_value=False):
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                        attempts=attempts,
                    )

    assert not wt_dir.exists()
    archive = tmp_path / ".harvest-evidence" / "pr-42-d-1" / "scratch"
    assert (archive / "recovery.txt").read_text() == "dead engine evidence"
    attempts.clear.assert_called_once_with(42)
    assert decision["identity"] == "dead"


def test_reconcile_one_archives_scratch_after_dual_label_escalation(
    tmp_path: Path,
) -> None:
    """Dual-label convergence preserves scratch before deleting local custody."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    write_stale_review_marker(wt_dir, pr=42, dispatch_id="review-42-dual-label")
    (wt_dir / "recovery.txt").write_text("dual-label evidence")
    dual_labels = {
        "labels": [{"name": "ai:reviewing"}, {"name": "ai:revised"}],
    }
    label_reads = [
        dual_labels,  # reconciliation observes the conflict
        dual_labels,  # convergence proves no pre-existing human hold
        {"labels": [{"name": "ai:needs-human-decision"}]},  # our mutation landed
    ]

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh"):
                _reconcile_one(42, str(wt_dir), config, tick_log=None)

    assert not wt_dir.exists()
    archive = (
        tmp_path
        / ".harvest-evidence"
        / "pr-42-review-42-dual-label"
        / "scratch"
    )
    assert (archive / "recovery.txt").read_text() == "dual-label evidence"


def test_reconcile_one_dual_label_escalation_uses_single_mutation(
    tmp_path: Path,
) -> None:
    """Strict dual-label escalation converges in ONE gh edit mutation — the
    same single-call partial-failure window as every other label transition."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    write_stale_review_marker(wt_dir, pr=42, dispatch_id="review-42-dual-label")
    dual = {"labels": [{"name": "ai:reviewing"}, {"name": "ai:revised"}]}
    label_reads = [
        dual,
        dual,
        {"labels": [{"name": "ai:needs-human-decision"}]},
    ]
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh", side_effect=record_gh):
                decision = _reconcile_one(42, str(wt_dir), config, tick_log=None)

    assert decision["action"] == "noop"
    assert gh_calls == [(
        "pr", "edit", "42",
        "--add-label", "ai:needs-human-decision",
        "--remove-label", "ai:reviewing",
        "--remove-label", "ai:revised",
    )]
    assert not wt_dir.exists()


def test_reconcile_one_dual_label_escalation_clears_attempts_before_move(
    tmp_path: Path,
) -> None:
    """Terminal dual-label escalation clears the durable attempt counter once
    convergence lands, before the archive move deletes the marker — otherwise
    a crash in that window strands a stale count that a later requeue would
    inherit against the retry cap."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    write_stale_review_marker(wt_dir, pr=42, dispatch_id="review-42-dual-label")
    dual = {"labels": [{"name": "ai:reviewing"}, {"name": "ai:revised"}]}
    label_reads = [
        dual,
        dual,
        {"labels": [{"name": "ai:needs-human-decision"}]},
    ]
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh"):
                with mock.patch(
                    "tick._archive_and_cleanup_scratch", return_value=False,
                ):
                    _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                        attempts=attempts,
                    )

    # Cleared even though the archive move failed: the terminal relabel is
    # the durable proof, and scratch custody is retained for retry.
    attempts.clear.assert_called_once_with(42)


def test_reconcile_one_dual_label_escalation_failure_retains_scratch(
    tmp_path: Path,
) -> None:
    """A failed escalation convergence keeps the scratch and marker durable
    so the next tick retries instead of dropping local custody."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    write_stale_review_marker(wt_dir, pr=42, dispatch_id="review-42-dual-label")
    dual = {"labels": [{"name": "ai:reviewing"}, {"name": "ai:revised"}]}
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=[dual, dual]):
            with mock.patch(
                "tick.gh",
                side_effect=subprocess.CalledProcessError(1, ["gh"]),
            ):
                decision = _reconcile_one(
                    42, str(wt_dir), config, tick_log=None, attempts=attempts,
                )

    assert decision["action"] == "noop"
    assert (wt_dir / "dispatch.json").is_file()
    attempts.clear.assert_not_called()


@pytest.mark.parametrize("reverse_labels", [False, True])
@pytest.mark.parametrize(
    ("stage", "worktree_prefix", "engine", "in_flight", "pre_label",
     "second_label", "result_file"),
    [
        ("review", "pr", "codex", "ai:reviewing", "ai:implemented",
         "ai:ready-for-human", "review.json"),
        ("fix", "pr", "claude", "ai:fixing", "ai:changes-requested",
         "ai:revised", "result.json"),
        ("implement", "issue", "claude", "ai:implementing", "ai:available",
         "ai:available", "result.json"),
    ],
)
def test_reconcile_one_completed_dual_labels_use_marker_owned_custody(
    tmp_path: Path,
    stage: str,
    worktree_prefix: str,
    engine: str,
    in_flight: str,
    pre_label: str,
    second_label: str,
    result_file: str,
    reverse_labels: bool,
) -> None:
    """Completed dual-label custody is stage-symmetric and order-invariant."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage=stage, worktree_prefix=worktree_prefix, attempts_key_prefix="",
        result_file=result_file, in_flight_label=in_flight,
        pre_label=pre_label, strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    labels = [in_flight, second_label]
    if reverse_labels:
        labels.reverse()
    wt_dir = tmp_path / f"{worktree_prefix}-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": engine, "stage": stage, "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": pre_label,
        "dispatch_id": f"{stage}-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch(
            "tick.gh_json",
            return_value={"labels": [{"name": label} for label in labels]},
        ):
            with mock.patch(
                "tick._classify_marker", return_value=("complete", "dead", False),
            ):
                decision = _reconcile_one(42, str(wt_dir), config, tick_log=None)

    assert decision["action"] == "harvest"


def test_reconcile_one_live_dual_label_retains_custody_without_mutation(
    tmp_path: Path,
) -> None:
    """A live engine with a partial dual-label state keeps waiting untouched."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    labels = {"labels": [{"name": "ai:reviewing"}, {"name": "ai:implemented"}]}

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=labels):
            with mock.patch("tick.gh") as gh_mock:
                with mock.patch(
                    "tick._classify_marker",
                    return_value=("live", "matched", True),
                ):
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                    )

    assert decision["action"] == "leave"
    gh_mock.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_reconcile_one_keeps_wrapper_child_group_in_custody(
    tmp_path: Path,
) -> None:
    """A dead reviewer wrapper cannot release its still-live Codex child."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch(
            "tick.gh_json",
            return_value={"labels": [{"name": "ai:reviewing"}]},
        ):
            with mock.patch(
                "tick.process_control.get_process_start_time",
                return_value=None,
            ):
                with mock.patch(
                    "tick.process_control.pid_is_alive", return_value=False,
                ):
                    with mock.patch(
                        "tick.process_control.pgid_is_alive", return_value=True,
                    ) as group_alive:
                        decision = _reconcile_one(
                            42, str(wt_dir), config, tick_log=None,
                        )

    assert decision["action"] == "leave"
    assert decision["identity"] == "unknown"
    group_alive.assert_called_once_with(99999)
    assert (wt_dir / "dispatch.json").is_file()


def test_reconcile_one_escalates_overdue_unknown_live_group_without_killing(
    tmp_path: Path,
) -> None:
    """An overdue orphaned child group is retained and escalated to a human.

    The wrapper PID is dead, so its start-time identity is unknowable. The
    recorded group is still alive, but killing that unverified group could
    target reused OS identity. Reconciliation must notify a human without
    pretending the kill succeeded or releasing the marker.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch(
            "tick.gh_json",
            return_value={"labels": [{"name": "ai:reviewing"}]},
        ):
            with mock.patch(
                "tick.process_control.get_process_start_time",
                return_value=None,
            ):
                with mock.patch(
                    "tick.process_control.pid_is_alive", return_value=False,
                ):
                    with mock.patch(
                        "tick.process_control.pgid_is_alive", return_value=True,
                    ):
                        with mock.patch(
                            "tick.process_control.terminate_process_group",
                        ) as kill_group:
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log=None,
                            )

    assert decision == {
        "action": "escalate",
        "kill": False,
        "label": "ai:needs-human-decision",
        "identity": "unknown",
        "pr_number": 42,
    }
    kill_group.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_reconcile_one_post_escalation_keeps_unknown_live_group_custody(
    tmp_path: Path,
) -> None:
    """A later tick cannot cleanup an orphaned group still known to be alive."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        _execute_reconcile_actions,
        _reconcile_one,
    )

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch(
            "tick.gh_json",
            return_value={"labels": [{"name": "ai:needs-human-decision"}]},
        ):
            with mock.patch(
                "tick.process_control.get_process_start_time",
                return_value=None,
            ):
                with mock.patch(
                    "tick.process_control.pid_is_alive", return_value=False,
                ):
                    with mock.patch(
                        "tick.process_control.pgid_is_alive", return_value=True,
                    ):
                        decision = _reconcile_one(
                            42, str(wt_dir), config, tick_log=None,
                        )

    assert decision["action"] == "leave"
    with mock.patch("tick.cleanup_scratch") as cleanup:
        assert _execute_reconcile_actions(
            [decision], config, wt_root=str(tmp_path),
        )
    cleanup.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_reconcile_one_dual_label_human_hold_wins_over_completed_marker(
    tmp_path: Path,
) -> None:
    """ai:needs-human-decision set alongside the in-flight label is a human
    hold: reconcile escalates in its favor instead of harvesting over it."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "fix", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "FIX_COMPLETE"}),
    )
    dual = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=dual):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick.process_control.get_process_start_time",
                    return_value=None,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=False,
                    ):
                        with mock.patch(
                            "tick.process_control.pgid_is_alive",
                            return_value=False,
                        ):
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log=None,
                            )

    assert decision["action"] == "cleanup"
    assert gh_calls == []


def test_reconcile_one_dual_label_blocked_hold_wins_over_completed_marker(
    tmp_path: Path,
) -> None:
    """ai:blocked next to the in-flight label is a human hold too — the tick
    never produces it, so reconcile escalates instead of harvesting over it."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:available",
        "dispatch_id": "implement-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    dual = {"labels": [{"name": "ai:implementing"}, {"name": "ai:blocked"}]}
    label_reads = [
        dual,
        dual,
        {"labels": [{"name": "ai:needs-human-decision"}]},
    ]
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick._classify_marker",
                    return_value=("complete", "dead", False),
                ):
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                    )

    assert decision["action"] == "cleanup"
    assert decision["release_label"] == "ai:implementing"


@pytest.mark.parametrize(
    (
        "stage", "prefix", "engine", "in_flight_label", "pre_label",
        "result_file", "result_obj", "expected_outcome",
    ),
    [
        (
            "review", "pr", "codex", "ai:reviewing", "ai:implemented",
            "review.json", {"verdict": "ready_for_human", "findings": []},
            "ready_for_human",
        ),
        (
            "implement", "issue", "claude", "ai:implementing", "ai:available",
            "result.json", {"result": "IMPLEMENTATION_COMPLETE"},
            "IMPLEMENTATION_COMPLETE",
        ),
        (
            "fix", "pr", "claude", "ai:fixing", "ai:changes-requested",
            "result.json", {"result": "FIXES_PUSHED_RECHECK_NEEDED"},
            "FIXES_PUSHED_RECHECK_NEEDED",
        ),
    ],
)
@pytest.mark.parametrize(
    "hold_label", ["ai:needs-human-decision", "ai:blocked"],
)
def test_reconcile_one_completed_hold_carries_the_stage_outcome(
    tmp_path: Path,
    stage: str,
    prefix: str,
    engine: str,
    in_flight_label: str,
    pre_label: str,
    result_file: str,
    result_obj: object,
    expected_outcome: str,
    hold_label: str,
) -> None:
    """Every engine type hands one validated held outcome to the executor."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage=stage,  # type: ignore[arg-type]
        worktree_prefix=prefix,  # type: ignore[arg-type]
        attempts_key_prefix="" if prefix == "pr" else "issue-",
        result_file=result_file,
        in_flight_label=in_flight_label,
        pre_label=pre_label,
        strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / f"{prefix}-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": engine, "stage": stage, "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": pre_label,
        "dispatch_id": f"{stage}-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    (wt_dir / result_file).write_text(json.dumps(result_obj))
    labels = {"labels": [
        {"name": in_flight_label}, {"name": hold_label},
    ]}

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=labels):
            with mock.patch(
                "tick._classify_marker", return_value=("complete", "dead", False),
            ):
                decision = _reconcile_one(
                    42, str(wt_dir), config, tick_log=None,
                )

    assert decision["action"] == "cleanup"
    assert decision["held_result_outcome"] == expected_outcome
    assert decision["hold_label"] == hold_label


@pytest.mark.parametrize("prefix", ["pr", "issue"])
@pytest.mark.parametrize("item_state", ["OPEN", "MERGED"])
def test_reconcile_one_names_the_hold_it_actually_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    item_state: str,
) -> None:
    """The published hold is read off the item, not inferred from a pair.

    ``hold_label`` is what the executor re-verifies before it releases the
    in-flight label, so deriving it from a two-member assumption rather than from
    the observed intersection would name a hold the item does not carry. The
    release then fails its pre-check every tick and the completion is stranded
    behind a permanently pinned admission slot. Both lifecycle branches resolve
    the hold independently, so both are pinned here.
    """
    import pipeline  # type: ignore[import-untyped]
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    third_hold = "ai:on-hold"
    monkeypatch.setattr(
        pipeline, "HOLD_LABELS", frozenset({*pipeline.HOLD_LABELS, third_hold}),
    )
    stage = "implement" if prefix == "issue" else "fix"
    in_flight_label = "ai:implementing" if prefix == "issue" else "ai:fixing"
    pre_label = "ai:available" if prefix == "issue" else "ai:changes-requested"
    config = StageConfig(
        stage=stage,  # type: ignore[arg-type]
        worktree_prefix=prefix,  # type: ignore[arg-type]
        attempts_key_prefix="issue-" if prefix == "issue" else "",
        result_file="result.json",
        in_flight_label=in_flight_label,
        pre_label=pre_label,
        strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / f"{prefix}-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": stage, "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": pre_label,
        "dispatch_id": f"{stage}-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": "IMPLEMENTATION_COMPLETE" if prefix == "issue"
        else "FIXES_PUSHED_RECHECK_NEEDED",
    }))
    labels = {"labels": [
        {"name": in_flight_label}, {"name": third_hold},
    ]}

    with mock.patch("tick.gh_text", return_value=item_state):
        with mock.patch("tick.gh_json", return_value=labels):
            with mock.patch(
                "tick._classify_marker", return_value=("complete", "dead", False),
            ):
                decision = _reconcile_one(42, str(wt_dir), config, tick_log=None)

    assert decision["action"] == "cleanup"
    assert decision["hold_label"] == third_hold


def test_held_result_outcome_maps_non_utf8_payload_to_unrecognized(
    tmp_path: Path,
) -> None:
    """A non-empty but undecodable result remains a controlled outcome."""
    from pipeline import (  # type: ignore[attr-defined]
        OUTCOME_UNRECOGNIZED,
        StageConfig,
    )
    from tick import _held_result_outcome  # type: ignore[import-untyped]

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_bytes(b"\xff")

    assert _held_result_outcome(str(wt_dir), config) == OUTCOME_UNRECOGNIZED


@pytest.mark.parametrize(
    "hold_label", ["ai:needs-human-decision", "ai:blocked"],
)
def test_hung_engine_with_proven_identity_is_killed_under_a_hold(
    tmp_path: Path,
    hold_label: str,
) -> None:
    """A hold pauses the workflow; it does not extend the spend deadline.

    Process custody never transfers to a human — they cannot see or signal the
    PID — so an overdue engine whose identity is *proven* is still a runaway
    and is still terminated. The kill advances no decision: the hold label is
    untouched and the scratch is retained, so the operator keeps every piece of
    evidence and the pending choice is exactly where they left it.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "fix", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 4242,
    }))
    dual = {"labels": [{"name": "ai:fixing"}, {"name": hold_label}]}
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=dual):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick.process_control.get_process_start_time",
                    return_value=1000.0,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=True,
                    ):
                        with mock.patch(
                            "tick.process_control.terminate_process_group",
                            return_value=True,
                        ) as killpg:
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log=None,
                            )

    # The runaway is stopped, by process group, on proven identity.
    killpg.assert_called_once()
    assert killpg.call_args.args[0] == 4242

    # ...but no decision is advanced and no evidence is destroyed.
    assert decision["action"] == "leave"
    assert decision["identity"] == "matched"
    assert gh_calls == []
    assert (wt_dir / "dispatch.json").is_file()


@pytest.mark.parametrize("lifecycle", ["CLOSED", "MERGED"])
def test_terminal_hold_kills_hung_matched_engine_like_the_open_path(
    tmp_path: Path,
    lifecycle: str,
) -> None:
    """Closing an item does not grant its runaway engine a reprieve.

    Process custody is independent of the item's lifecycle, so the terminal
    hold path applies the same rule as the open one: an overdue engine with
    proven identity is terminated, while labels and scratch stay untouched.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "fix", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 4242,
    }))
    held = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    with mock.patch("tick.gh_text", return_value=lifecycle):
        with mock.patch("tick.gh_json", return_value=held):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick.process_control.get_process_start_time",
                    return_value=1000.0,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=True,
                    ):
                        with mock.patch(
                            "tick.process_control.terminate_process_group",
                            return_value=True,
                        ) as killpg:
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log=None,
                            )

    killpg.assert_called_once()
    assert killpg.call_args.args[0] == 4242
    assert decision["action"] == "leave"
    assert gh_calls == []
    assert (wt_dir / "dispatch.json").is_file()


def test_hung_engine_with_unprovable_identity_is_left_alone_under_a_hold(
    tmp_path: Path,
) -> None:
    """The #578 escalate-and-leave contract still governs unverifiable engines.

    Killing requires proven identity — an unreadable start time could mean PID
    reuse, so signalling it could hit an unrelated process.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "fix", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 4242,
    }))
    dual = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=dual):
            with mock.patch("tick.gh", return_value=""):
                with mock.patch(
                    "tick.process_control.get_process_start_time",
                    return_value=None,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=True,
                    ):
                        with mock.patch(
                            "tick.process_control.terminate_process_group",
                        ) as killpg:
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log=None,
                            )

    killpg.assert_not_called()
    assert decision["action"] == "leave"
    assert (wt_dir / "dispatch.json").is_file()


def test_launching_lease_under_a_hold_is_reclaimed_not_stranded(
    tmp_path: Path,
) -> None:
    """A lease records no process, so a hold has no process custody to retain.

    This is the pipeline escalating onto its own hold: a spawn that crashed
    between the lease write and the marker publish reconciles to ``escalate``,
    which converges the item to ``ai:needs-human-decision`` alone and keeps the
    scratch. Every later tick then re-reads that hold.

    The hold gate's release condition is proven death, and a lease can never
    reach it: ``valid_marker``'s mutual exclusion means a lease carries no
    ``pid``/``start_time``/``pgid`` at all, so identity stays ``unknown``
    forever and ``classify_dispatch`` pins ``launching`` without ever
    consulting the deadline. Without this, the scratch and its admission slot
    are held until a human deletes the directory by hand.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:available",
        "dispatch_id": "implement-42", "state": "launching",
    }))
    # What the pipeline's own escalation left behind: the hold alone.
    held = {"labels": [{"name": "ai:needs-human-decision"}]}

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=held):
            with mock.patch("tick.gh") as gh_mock:
                decision = _reconcile_one(42, str(wt_dir), config, tick_log=None)

    assert decision["action"] == "cleanup"
    # The in-flight label is already gone, so there is nothing to release —
    # and the hold must never be named in a mutation either way.
    assert decision["release_label"] == ""
    gh_mock.assert_not_called()


def test_launching_lease_under_a_hold_releases_a_surviving_in_flight_label(
    tmp_path: Path,
) -> None:
    """A human hold placed while the lease still carries our own label.

    Distinct from the escalation case: here ``ai:blocked`` arrives from the
    operator before any reconcile ran, so the pipeline's own in-flight label is
    still on the item and every authoritative read counts it against
    ``max_in_flight``. Reclaiming the scratch without retracting that receipt
    would leave the slot burned.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:available",
        "dispatch_id": "implement-42", "state": "launching",
    }))
    held = {"labels": [
        {"name": "ai:implementing"}, {"name": "ai:blocked"},
    ]}

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=held):
            decision = _reconcile_one(42, str(wt_dir), config, tick_log=None)

    assert decision["action"] == "cleanup"
    assert decision["release_label"] == "ai:implementing"


def test_launching_lease_under_a_hold_reclaims_scratch_through_the_executor(
    tmp_path: Path,
) -> None:
    """The decision is only half of it — the slot is reclaimed by the executor.

    Drives the same lease through ``_reconcile_one`` and then
    ``_execute_reconcile_actions``, because the consequence being fixed is a
    scratch directory and an admission slot that are never returned, and only
    the executor returns them.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        _execute_reconcile_actions,
        _reconcile_one,
    )

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:available",
        "dispatch_id": "implement-42", "state": "launching",
    }))
    dual = {"labels": [
        {"name": "ai:implementing"}, {"name": "ai:blocked"},
    ]}
    hold_only = {"labels": [{"name": "ai:blocked"}]}
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    def label_reads(*_args: str, **_kwargs: object) -> object:
        return hold_only if gh_calls else dual

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh", side_effect=record_gh):
                decision = _reconcile_one(42, str(wt_dir), config, tick_log=None)
                assert _execute_reconcile_actions(
                    [decision], config, str(tmp_path),
                )

    assert gh_calls == [
        ("issue", "edit", "42", "--remove-label", "ai:implementing"),
    ]
    assert all("ai:blocked" not in call for call in gh_calls)
    assert not (wt_dir / "dispatch.json").exists()


@pytest.mark.parametrize(
    ("stage", "prefix", "entity", "engine", "in_flight_label", "pre_label"),
    [
        ("review", "pr", "pr", "codex", "ai:reviewing", "ai:implemented"),
        ("fix", "pr", "pr", "claude", "ai:fixing", "ai:changes-requested"),
        (
            "implement", "issue", "issue", "claude",
            "ai:implementing", "ai:available",
        ),
    ],
)
@pytest.mark.parametrize(
    "hold_label", ["ai:needs-human-decision", "ai:blocked"],
)
def test_proven_dead_hold_releases_in_flight_label_before_cleanup(
    tmp_path: Path,
    stage: str,
    prefix: str,
    entity: str,
    engine: str,
    in_flight_label: str,
    pre_label: str,
    hold_label: str,
) -> None:
    """Proven group death under a hold must release the pipeline's own label.

    The scratch-destroying branch already trusts two negative process probes.
    Leaving the in-flight label behind would keep the item in every
    authoritative in-flight read forever, permanently consuming a
    max_in_flight slot with no process and no marker behind it. Releasing it
    is not a custody mutation: the human hold is untouched.

    Parametrised across all three engine types and both hold labels — the
    slot leak is symmetric, so the release has to be too.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        _execute_reconcile_actions,
        _reconcile_one,
    )

    config = StageConfig(
        stage=stage, worktree_prefix=prefix,  # type: ignore[arg-type]
        attempts_key_prefix="" if prefix == "pr" else "issue-",
        result_file="review.json" if stage == "review" else "result.json",
        in_flight_label=in_flight_label, pre_label=pre_label,
        strict_dual_label=True, tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / f"{prefix}-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": engine, "stage": stage, "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": pre_label,
        "dispatch_id": f"{stage}-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    dual = {"labels": [
        {"name": in_flight_label}, {"name": hold_label},
    ]}
    hold_only = {"labels": [{"name": hold_label}]}
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    def label_reads(*_args: str, **_kwargs: object) -> object:
        # Reflect the real remote after the removal lands, so the release's
        # own confirmation read sees the post-mutation state.
        return hold_only if gh_calls else dual

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick.process_control.get_process_start_time",
                    return_value=None,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=False,
                    ):
                        with mock.patch(
                            "tick.process_control.pgid_is_alive",
                            return_value=False,
                        ):
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log=None,
                            )
                            assert decision["action"] == "cleanup"
                            assert decision["release_label"] == in_flight_label
                            assert gh_calls == []
                            assert _execute_reconcile_actions(
                                [decision], config, str(tmp_path),
                            )

    # Exact emitted argv: one pure removal of the pipeline's own label.
    assert gh_calls == [
        (entity, "edit", "42", "--remove-label", in_flight_label),
    ]
    # The human hold is never named in any mutation.
    assert all(hold_label not in call for call in gh_calls)
    # Label released first, then the scratch is destroyed.
    assert not (wt_dir / "dispatch.json").exists()


def test_resultless_release_refuses_when_the_hold_departed_before_execution(
    tmp_path: Path,
) -> None:
    """The release is only ever justified by the hold that decided it.

    A dead engine that produced no result reaches the same release as a
    completed one — same decision producer, same ``release_label`` — but
    without the held-result publication in front of it. If the operator lifts
    the hold between the reconcile read and the executor, the removal strips
    the item's last workflow label and the cleanup then destroys the marker,
    leaving it unroutable forever. The release must revalidate the exact hold
    at mutation time and retain custody when it is gone.
    """
    from pipeline import LABEL_NEEDS_HUMAN, StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    # No result file: the engine died before completing, so the decision
    # carries a release but no held outcome to publish.
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42, "release_label": "ai:fixing",
        "held_result_outcome": "",
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    attempts = mock.Mock()
    edits: list[tuple[object, ...]] = []

    with mock.patch("tick.gh_json", return_value={"labels": []}):
        with mock.patch(
            "tick.gh", side_effect=lambda *a, **_k: edits.append(a) or mock.Mock(),
        ):
            with mock.patch("tick._archive_and_cleanup_scratch") as cleanup:
                assert not _execute_reconcile_actions(
                    [decision], config, str(tmp_path),
                    tick_log=tick_log, attempts=attempts,
                )

    assert edits == [], "no label may move once the hold that justified it is gone"
    cleanup.assert_not_called()
    assert (wt_dir / "dispatch.json").exists(), "the marker outlives the refusal"
    assert [
        entry for entry in read_tick_log_entries(tmp_path / "tick-log")
        if entry.get("action") == "label_release_failed"
    ], "the refusal is operator-visible"


def test_resultless_release_is_restored_when_the_hold_departs_after_it_lands(
    tmp_path: Path,
) -> None:
    """The unlabelled-item harm is the same with or without a result to publish.

    The completed-result path already re-reads the hold after its release and
    restores the in-flight label when an operator lifted it inside that window.
    The resultless release destroys exactly the same custody — the release
    confirms the hold in its own read-back, then the cleanup below deletes the
    marker — so a hold lifted between those two steps leaves the item with no
    workflow label and nothing left to route it. Both releases are fed by the
    same two decision producers and differ only in whether a result was found,
    so the guard cannot differ either.
    """
    from pipeline import LABEL_NEEDS_HUMAN, StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    # No result file: nothing to publish, so the release is the only remote
    # mutation this decision makes.
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42, "release_label": "ai:fixing",
        "held_result_outcome": "",
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    attempts = mock.Mock()
    reads = iter([
        # The release's own before/after reads: the hold is present
        # throughout, so the removal lands and confirms.
        {"labels": [{"name": LABEL_NEEDS_HUMAN}, {"name": "ai:fixing"}]},
        {"labels": [{"name": LABEL_NEEDS_HUMAN}]},
        # The operator lifts the hold before the marker is destroyed, and the
        # released in-flight label is already gone — no workflow label is left.
        {"labels": []},
        # Restoration read-back.
        {"labels": [{"name": "ai:fixing"}]},
    ])
    edits: list[tuple[object, ...]] = []

    with mock.patch("tick.gh_json", side_effect=lambda *_a, **_k: next(reads)):
        with mock.patch(
            "tick.gh", side_effect=lambda *a, **_k: edits.append(a) or mock.Mock(),
        ):
            with mock.patch("tick._archive_and_cleanup_scratch") as cleanup:
                assert not _execute_reconcile_actions(
                    [decision], config, str(tmp_path),
                    tick_log=tick_log, attempts=attempts,
                )

    assert ("pr", "edit", "42", "--remove-label", "ai:fixing") in edits
    assert ("pr", "edit", "42", "--add-label", "ai:fixing") in edits, (
        "a released in-flight label must be restored when the refusal follows it"
    )
    cleanup.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file(), "the marker outlives the refusal"
    restorations = [
        entry for entry in read_tick_log_entries(tmp_path / "tick-log")
        if entry.get("action") == "label_release_failed"
        and isinstance(entry.get("details"), dict)
        and entry["details"].get("in_flight_restored") is True
    ]
    assert len(restorations) == 1, "the restoration is operator-visible"


def test_held_completed_result_is_logged_as_published_after_archive(
    tmp_path: Path,
) -> None:
    """A held completion is logged only after publication and archive succeed.

    Under a hold the pipeline may not advance the stage, so the engine outcome
    is published as an unharvested completion before the automation label is
    released. The operator-visible log names the same outcome after local
    evidence custody moves into the archive.

    The entry has to follow the archive rather than the decision: a failed
    post, release, or archive retains the scratch, and claiming completion
    before that sequence lands would overstate the durable state.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
        _reconcile_one,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "fix", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": "FIXES_PUSHED_RECHECK_NEEDED",
    }))
    dual = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}
    hold_only = {"labels": [{"name": "ai:needs-human-decision"}]}
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    def label_reads(*_args: str, **_kwargs: object) -> object:
        return hold_only if gh_calls else dual

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick.process_control.get_process_start_time",
                    return_value=None,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=False,
                    ):
                        with mock.patch(
                            "tick.process_control.pgid_is_alive",
                            return_value=False,
                        ):
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log,
                            )
                            # Nothing is claimed before publication and archive.
                            assert not [
                                e for e in tick_log.tail()
                                if e.get("action") == "held_result_published"
                            ]
                            assert _execute_reconcile_actions(
                                [decision], config, str(tmp_path),
                                tick_log=tick_log,
                            )

    assert decision["action"] == "cleanup"
    assert not (wt_dir / "dispatch.json").exists()
    published = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "held_result_published"
    ]
    assert len(published) == 1
    assert published[0].get("dispatch_id") == "fix-42"
    assert published[0].get("details") == {
        "outcome": "FIXES_PUSHED_RECHECK_NEEDED",
    }


def test_held_completed_result_is_published_before_release_and_archive(
    tmp_path: Path,
) -> None:
    """Publication owns the boundary before remote or local custody moves."""
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        SENTINEL_FIXES_PUSHED,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "fix-42",
    }))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": SENTINEL_FIXES_PUSHED,
    }))
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42,
        "release_label": "ai:fixing",
        "held_result_outcome": SENTINEL_FIXES_PUSHED,
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    events: list[str] = []
    posted: list[dict[str, object]] = []
    attempts = mock.Mock()
    attempts.clear.side_effect = lambda _number: events.append("clear-attempts")

    def prepare(**_kwargs: object) -> str:
        events.append("prepare")
        return str(tmp_path / ".harvest-evidence" / "pr-42-fix-42")

    def post(**kwargs: object) -> bool:
        events.append("post")
        posted.append(kwargs)
        return True

    def release(*_args: object, **_kwargs: object) -> bool:
        events.append("release")
        assert _kwargs["required_hold_label"] == LABEL_NEEDS_HUMAN
        return True

    def finalize(*_args: object, **_kwargs: object) -> bool:
        events.append("finalize")
        return True

    def discard_receipts(*_args: object, **_kwargs: object) -> None:
        events.append("discard-receipts")

    with mock.patch(
        "tick.gh_json",
        return_value={"labels": [{"name": LABEL_NEEDS_HUMAN}]},
    ):
        with mock.patch("tick._archive_harvest_evidence", side_effect=prepare):
            with mock.patch("tick._post_harvest_comment", side_effect=post):
                with mock.patch(
                    "tick._release_in_flight_label", side_effect=release,
                ):
                    with mock.patch(
                        "tick._finalize_harvest_archive", side_effect=finalize,
                    ):
                        with mock.patch(
                            "tick._discard_comment_receipts",
                            side_effect=discard_receipts,
                        ):
                            with mock.patch(
                                "tick._archive_and_cleanup_scratch",
                            ) as legacy_archive:
                                assert _execute_reconcile_actions(
                                    [decision], config, str(tmp_path),
                                    tick_log=tick_log, attempts=attempts,
                                )

    assert events == [
        "prepare", "post", "release", "clear-attempts", "finalize",
        "discard-receipts",
    ]
    legacy_archive.assert_not_called()
    assert posted[0]["target_label"] == LABEL_NEEDS_HUMAN
    assert SENTINEL_FIXES_PUSHED in str(posted[0]["comment_body"])
    published = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "held_result_published"
    ]
    assert len(published) == 1
    assert published[0].get("details") == {
        "outcome": SENTINEL_FIXES_PUSHED,
    }


def test_held_result_is_not_published_when_its_evidence_cannot_be_prepared(
    tmp_path: Path,
) -> None:
    """Nothing is published for a result that could not be archived first.

    ``_archive_harvest_evidence`` requires the stage result file, so an empty
    return means the outcome about to be published has no durable copy. Posting
    there would tell the operator a result is waiting in an archive that does not
    hold it — the same misdirection the log ordering already refuses.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_BLOCKED,
        SENTINEL_FIXES_PUSHED,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42, "release_label": "ai:fixing",
        "held_result_outcome": SENTINEL_FIXES_PUSHED,
        "hold_label": LABEL_BLOCKED,
    }
    attempts = mock.Mock()

    with mock.patch(
        "tick.gh_json",
        return_value={"labels": [{"name": LABEL_BLOCKED}]},
    ):
        with mock.patch("tick._archive_harvest_evidence", return_value=""):
            with mock.patch("tick._post_harvest_comment") as post:
                with mock.patch("tick._release_in_flight_label") as release:
                    assert not _execute_reconcile_actions(
                        [decision], config, str(tmp_path),
                        tick_log=tick_log, attempts=attempts,
                    )

    post.assert_not_called()
    release.assert_not_called()
    attempts.clear.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()
    assert [
        entry for entry in tick_log.tail()
        if entry.get("action") == "archive_failed"
    ]


def test_held_result_post_failure_retains_every_source_of_custody(
    tmp_path: Path,
) -> None:
    """A missing remote explanation forbids release, cleanup, or retry reset."""
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        SENTINEL_IMPL_COMPLETE,
        StageConfig,
    )
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="implement", worktree_prefix="issue",
        attempts_key_prefix="issue-", result_file="result.json",
        in_flight_label="ai:implementing", pre_label="ai:available",
        strict_dual_label=True, tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "implement-42",
    }))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": SENTINEL_IMPL_COMPLETE,
    }))
    archive_dir = str(
        tmp_path / ".harvest-evidence" / "issue-42-implement-42"
    )
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42,
        "release_label": "ai:implementing",
        "held_result_outcome": SENTINEL_IMPL_COMPLETE,
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    attempts = mock.Mock()

    with mock.patch(
        "tick.gh_json",
        return_value={"labels": [{"name": LABEL_NEEDS_HUMAN}]},
    ):
        with mock.patch(
            "tick._archive_harvest_evidence", return_value=archive_dir,
        ):
            with mock.patch("tick._post_harvest_comment", return_value=False):
                with mock.patch("tick._discard_prepared_archive") as discard:
                    with mock.patch("tick._release_in_flight_label") as release:
                        with mock.patch("tick._finalize_terminal_archive") as finalize:
                            assert not _execute_reconcile_actions(
                                [decision], config, str(tmp_path),
                                attempts=attempts,
                            )

    discard.assert_called_once_with(archive_dir)
    release.assert_not_called()
    finalize.assert_not_called()
    attempts.clear.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()
    assert (wt_dir / "result.json").is_file()


def test_held_result_is_not_logged_as_published_when_archive_finalize_fails(
    tmp_path: Path,
) -> None:
    """A retained scratch must not be reported as fully reconciled.

    A failed final archive move leaves the source scratch in custody for the
    next tick. ``held_result_published`` describes the completed publication,
    release, and archive sequence, so it must wait for that final move.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        OUTCOME_UNRECOGNIZED,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    decision = {
        "action": "cleanup", "label": "", "kill": False, "identity": "dead",
        "pr_number": 42, "release_label": "",
        "held_result_outcome": OUTCOME_UNRECOGNIZED,
        "hold_label": LABEL_NEEDS_HUMAN,
    }

    with mock.patch(
        "tick.gh_json",
        return_value={"labels": [{"name": LABEL_NEEDS_HUMAN}]},
    ):
        with mock.patch(
            "tick._archive_harvest_evidence",
            return_value=str(tmp_path / "archive"),
        ):
            with mock.patch("tick._post_harvest_comment", return_value=True):
                with mock.patch("tick._finalize_terminal_archive", return_value=False):
                    assert not _execute_reconcile_actions(
                        [decision], config, str(tmp_path), tick_log=tick_log,
                    )

    assert not [
        entry for entry in tick_log.tail()
        if entry.get("action") == "held_result_published"
    ]
    assert (wt_dir / "dispatch.json").is_file()


def test_hold_release_failure_retains_scratch_and_archive_for_next_tick(
    tmp_path: Path,
) -> None:
    """A failed label release must not be followed by scratch destruction.

    Ordering is the durability boundary: if the remote release does not land,
    the marker is the only thing that can prove the engine died on the next
    tick, so it has to survive. The prepared archive survives too: the post
    already landed and promised the operator that the output is retained in
    the harvest-evidence archive, so discarding the prepared copy now would
    leave a live comment pointing at nothing — only the source scratch would
    remain.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        SENTINEL_FIXES_PUSHED,
        StageConfig,
    )
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": SENTINEL_FIXES_PUSHED,
    }))
    archive_dir = str(tmp_path / "archive")
    dual = {"labels": [
        {"name": "ai:fixing"}, {"name": LABEL_NEEDS_HUMAN},
    ]}
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42, "release_label": "ai:fixing",
        "held_result_outcome": SENTINEL_FIXES_PUSHED,
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    bodies: list[str] = []

    with mock.patch("tick._archive_harvest_evidence", return_value=archive_dir):
        with mock.patch(
            "tick._post_harvest_comment",
            side_effect=lambda **_kwargs: bodies.append(
                str(_kwargs.get("comment_body", ""))
            ) or True,
        ):
            with mock.patch("tick.gh_json", return_value=dual):
                with mock.patch(
                    "tick.gh",
                    side_effect=subprocess.CalledProcessError(1, "gh"),
                ):
                    with mock.patch("tick._discard_prepared_archive") as discard:
                        with mock.patch("tick.cleanup_scratch") as cleanup:
                            assert not _execute_reconcile_actions(
                                [decision], config, str(tmp_path),
                            )
    discard.assert_not_called()
    cleanup.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()
    assert (wt_dir / "result.json").is_file()
    # The landed comment promises the evidence in the harvest-evidence archive;
    # that promise must be true on the failure path, so the prepared archive is
    # retained rather than discarded.
    assert bodies, "the held completion comment must have landed"
    assert "harvest-evidence archive" in bodies[0]


def test_held_result_is_not_published_when_the_hold_departed_before_execution(
    tmp_path: Path,
) -> None:
    """A hold observed at decision time is revalidated before publication.

    The decision can be a tick old: the hold branch read the labels during
    reconciliation, and the executor runs after it. If the hold was removed in
    that window, publishing a comment that claims the hold "remains
    authoritative" would misstate the live state — the item is no longer held
    and must be re-decided next tick, not explained under a departed hold.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        SENTINEL_FIXES_PUSHED,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": SENTINEL_FIXES_PUSHED,
    }))
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42, "release_label": "ai:fixing",
        "held_result_outcome": SENTINEL_FIXES_PUSHED,
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    attempts = mock.Mock()
    no_hold = {"labels": [{"name": "ai:fixing"}]}

    with mock.patch("tick._archive_harvest_evidence") as prepare:
        with mock.patch("tick._post_harvest_comment") as post:
            with mock.patch("tick.gh_json", return_value=no_hold):
                with mock.patch("tick._release_in_flight_label") as release:
                    with mock.patch("tick._finalize_terminal_archive") as finalize:
                        with mock.patch("tick.cleanup_scratch") as cleanup:
                            assert not _execute_reconcile_actions(
                                [decision], config, str(tmp_path),
                                tick_log=tick_log, attempts=attempts,
                            )

    prepare.assert_not_called()
    post.assert_not_called()
    release.assert_not_called()
    finalize.assert_not_called()
    cleanup.assert_not_called()
    attempts.clear.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()
    assert (wt_dir / "result.json").is_file()
    refusals = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "label_release_failed"
    ]
    assert len(refusals) == 1
    assert "before publish" in refusals[0].get("reason", "")


def test_held_result_custody_move_refuses_when_the_hold_departs_after_the_post(
    tmp_path: Path,
) -> None:
    """The terminal local custody move revalidates the hold unconditionally.

    When the in-flight label is already absent, ``release_label`` is empty and
    the release path never runs: the post is followed straight by attempts
    clear and archive finalize. A hold removed after the decision would still
    be finalized as a "hold won" sequence from a stale observation, so the
    revalidation before the custody move must not depend on a release having
    been attempted.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        SENTINEL_FIXES_PUSHED,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": SENTINEL_FIXES_PUSHED,
    }))
    archive_dir = str(tmp_path / "archive")
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42, "release_label": "",
        "held_result_outcome": SENTINEL_FIXES_PUSHED,
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    attempts = mock.Mock()
    reads = iter([
        # Pre-publish revalidation: the hold is still there.
        {"labels": [{"name": LABEL_NEEDS_HUMAN}]},
        # Post-publish revalidation: the hold has since been removed.
        {"labels": [{"name": "ai:fixing"}]},
    ])
    posted: list[dict[str, object]] = []

    with mock.patch(
        "tick._archive_harvest_evidence", return_value=archive_dir,
    ):
        with mock.patch(
            "tick._post_harvest_comment",
            side_effect=lambda **_kwargs: posted.append(_kwargs) or True,
        ):
            with mock.patch(
                "tick.gh_json", side_effect=lambda *_a, **_k: next(reads),
            ):
                with mock.patch("tick._release_in_flight_label") as release:
                    with mock.patch("tick._finalize_terminal_archive") as finalize:
                        with mock.patch("tick._discard_prepared_archive") as discard:
                            with mock.patch("tick.cleanup_scratch") as cleanup:
                                assert not _execute_reconcile_actions(
                                    [decision], config, str(tmp_path),
                                    tick_log=tick_log, attempts=attempts,
                                )

    assert len(posted) == 1, "the comment lands before the custody move"
    release.assert_not_called()
    finalize.assert_not_called()
    cleanup.assert_not_called()
    attempts.clear.assert_not_called()
    discard.assert_not_called(), (
        "the landed comment promised the archive; it must survive the refusal"
    )
    assert (wt_dir / "dispatch.json").is_file()
    assert (wt_dir / "result.json").is_file()
    refusals = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "label_release_failed"
    ]
    assert len(refusals) == 1
    assert "before local custody move" in refusals[0].get("reason", "")


def test_held_result_release_is_restored_when_the_hold_departs_after_it_lands(
    tmp_path: Path,
) -> None:
    """A refusal after the release must never leave the item unlabelled.

    The release confirms the hold in its own read-back and returns success.
    An operator removing the hold between that read and the final one makes
    the custody-move gate refuse — but the ``ai:fixing`` removal has already
    landed, so refusing without restoring leaves the PR with no workflow label
    at all. The next tick then sees a marker outside pipeline custody, cleans
    it up, and the PR stays unlabelled forever.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        SENTINEL_FIXES_PUSHED,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": SENTINEL_FIXES_PUSHED,
    }))
    archive_dir = str(tmp_path / "archive")
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42, "release_label": "ai:fixing",
        "held_result_outcome": SENTINEL_FIXES_PUSHED,
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    attempts = mock.Mock()
    reads = iter([
        # Pre-publish revalidation, then the release's own before/after reads:
        # the hold is present throughout, so the removal lands and confirms.
        {"labels": [{"name": LABEL_NEEDS_HUMAN}, {"name": "ai:fixing"}]},
        {"labels": [{"name": LABEL_NEEDS_HUMAN}, {"name": "ai:fixing"}]},
        {"labels": [{"name": LABEL_NEEDS_HUMAN}]},
        # Final revalidation: the operator has removed the hold, and the
        # released in-flight label is gone — no workflow label is left.
        {"labels": []},
        # Restoration read-back.
        {"labels": [{"name": "ai:fixing"}]},
    ])
    edits: list[tuple[object, ...]] = []
    posted: list[dict[str, object]] = []

    with mock.patch(
        "tick._archive_harvest_evidence", return_value=archive_dir,
    ):
        with mock.patch(
            "tick._post_harvest_comment",
            side_effect=lambda **_kwargs: posted.append(_kwargs) or True,
        ):
            with mock.patch(
                "tick.gh_json", side_effect=lambda *_a, **_k: next(reads),
            ):
                with mock.patch(
                    "tick.gh",
                    side_effect=lambda *a, **_k: edits.append(a) or mock.Mock(),
                ):
                    with mock.patch(
                        "tick._finalize_terminal_archive",
                    ) as finalize:
                        with mock.patch(
                            "tick._discard_prepared_archive",
                        ) as discard:
                            with mock.patch("tick.cleanup_scratch") as cleanup:
                                assert not _execute_reconcile_actions(
                                    [decision], config, str(tmp_path),
                                    tick_log=tick_log, attempts=attempts,
                                )

    assert len(posted) == 1
    assert ("pr", "edit", "42", "--remove-label", "ai:fixing") in edits
    assert ("pr", "edit", "42", "--add-label", "ai:fixing") in edits, (
        "a released in-flight label must be restored when the refusal follows it"
    )
    finalize.assert_not_called()
    cleanup.assert_not_called()
    attempts.clear.assert_not_called()
    discard.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()
    assert (wt_dir / "result.json").is_file()
    restorations = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "label_release_failed"
        and isinstance(entry.get("details"), dict)
        and entry["details"].get("in_flight_restored") is True
    ]
    assert len(restorations) == 1


def test_held_result_refusal_leaves_a_later_workflow_label_alone(
    tmp_path: Path,
) -> None:
    """A workflow label set by the operator is proof enough of ownership.

    Same race as the restoration case, except the operator replaced the hold
    with an ordinary pipeline label rather than removing it outright. The item
    already has an owner, so re-adding ``ai:fixing`` would manufacture a
    dual-label state on top of a human transition. Custody is still retained
    locally for the next tick to re-decide.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        LABEL_READY_FOR_HUMAN,
        SENTINEL_FIXES_PUSHED,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-42"}))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": SENTINEL_FIXES_PUSHED,
    }))
    decision = {
        "action": "cleanup", "label": "", "kill": False,
        "identity": "dead", "pr_number": 42, "release_label": "ai:fixing",
        "held_result_outcome": SENTINEL_FIXES_PUSHED,
        "hold_label": LABEL_NEEDS_HUMAN,
    }
    attempts = mock.Mock()
    reads = iter([
        {"labels": [{"name": LABEL_NEEDS_HUMAN}, {"name": "ai:fixing"}]},
        {"labels": [{"name": LABEL_NEEDS_HUMAN}, {"name": "ai:fixing"}]},
        {"labels": [{"name": LABEL_NEEDS_HUMAN}]},
        {"labels": [{"name": LABEL_READY_FOR_HUMAN}]},
    ])
    edits: list[tuple[object, ...]] = []

    with mock.patch(
        "tick._archive_harvest_evidence", return_value=str(tmp_path / "arc"),
    ):
        with mock.patch("tick._post_harvest_comment", return_value=True):
            with mock.patch(
                "tick.gh_json", side_effect=lambda *_a, **_k: next(reads),
            ):
                with mock.patch(
                    "tick.gh",
                    side_effect=lambda *a, **_k: edits.append(a) or mock.Mock(),
                ):
                    with mock.patch(
                        "tick._finalize_terminal_archive",
                    ) as finalize:
                        with mock.patch("tick.cleanup_scratch") as cleanup:
                            assert not _execute_reconcile_actions(
                                [decision], config, str(tmp_path),
                                tick_log=tick_log, attempts=attempts,
                            )

    assert not [call for call in edits if "--add-label" in call], (
        "an owned item must not be given a second label"
    )
    finalize.assert_not_called()
    cleanup.assert_not_called()
    attempts.clear.assert_not_called()
    assert (wt_dir / "result.json").is_file()


@pytest.mark.parametrize(
    "hold_label", ["ai:needs-human-decision", "ai:blocked"],
)
def test_reconcile_one_live_dual_label_with_hold_leaves_custody(
    tmp_path: Path,
    hold_label: str,
) -> None:
    """A hold placed while the engine is live defers escalation: the engine
    keeps custody of its scratch until it exits, with no label mutation."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    labels = {"labels": [{"name": "ai:reviewing"}, {"name": hold_label}]}

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=labels):
            with mock.patch("tick.gh") as gh_mock:
                with mock.patch(
                    "tick._classify_marker",
                    return_value=("live", "matched", True),
                ):
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                    )

    assert decision["action"] == "leave"
    gh_mock.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_reconcile_one_hung_dual_label_kills_but_never_relabels(
    tmp_path: Path,
) -> None:
    """A hold stops the pipeline touching the item, not the runaway process.

    Once identity recovers to ``matched`` the overdue engine is terminated —
    process custody never moved to the human — but the label set and the
    scratch stay exactly as the operator left them.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "fix", "pr_number": 42,
        "deadline": 1000.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-42", "pid": 99999, "start_time": 500.0,
        "pgid": 99999,
    }))
    labels = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}

    with mock.patch("tick.gh_text", return_value="OPEN"):
        with mock.patch("tick.gh_json", return_value=labels):
            with mock.patch("tick.gh") as gh_mock:
                with mock.patch(
                    "tick._classify_marker",
                    return_value=("hung", "matched", True),
                ):
                    with mock.patch(
                        "tick.process_control.terminate_process_group",
                        return_value=True,
                    ) as kill_mock:
                        decision = _reconcile_one(
                            42, str(wt_dir), config, tick_log=None,
                        )

    assert decision["action"] == "leave"
    kill_mock.assert_called_once()
    # No label mutation and no evidence destroyed: the decision is still theirs.
    gh_mock.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


@pytest.mark.parametrize("failure_index", [0, 1])
def test_converge_pipeline_labels_retains_incomplete_remote_transition(
    failure_index: int,
) -> None:
    """A failed mutation or a verified mismatch never reports convergence."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    labels = {
        "labels": [
            {"name": "ai:implemented"},
            {"name": "ai:reviewing"},
        ],
    }
    if failure_index == 0:
        # The single label-edit mutation fails.
        gh_side_effect: object = subprocess.CalledProcessError(
            1, ["gh", "pr", "edit"],
        )
        gh_json_side_effects: list[object] = [labels]
    else:
        # The mutation reports success but the verification read observes a
        # different label set (e.g. a concurrent human edit).
        gh_side_effect = None
        gh_json_side_effects = [
            labels,
            {"labels": [
                {"name": "ai:ready-for-human"},
                {"name": "ai:needs-human-decision"},
            ]},
        ]

    with mock.patch("tick.gh_json", side_effect=gh_json_side_effects):
        with mock.patch("tick.gh", side_effect=gh_side_effect) as gh_mock:
            converged = _converge_pipeline_labels(
                "pr", 42, "ai:ready-for-human",
                tick_log=None, phase="harvest", stage="review",
            )

    assert converged is False
    assert gh_mock.call_count == 1


def test_converge_pipeline_labels_removes_every_non_target_and_verifies() -> None:
    """Convergence is one atomic edit mutation plus an authoritative read."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    label_reads = [
        {
            "labels": [
                {"name": "ai:implemented"},
                {"name": "ai:reviewing"},
                {"name": "documentation"},
            ],
        },
        {
            "labels": [
                {"name": "ai:ready-for-human"},
                {"name": "documentation"},
            ],
        },
    ]

    with mock.patch("tick.gh_json", side_effect=label_reads):
        with mock.patch("tick.gh") as gh_mock:
            converged = _converge_pipeline_labels(
                "pr", 42, "ai:ready-for-human",
                tick_log=None, phase="harvest", stage="review",
            )

    assert converged is True
    gh_mock.assert_called_once_with(
        "pr", "edit", "42",
        "--add-label", "ai:ready-for-human",
        "--remove-label", "ai:implemented",
        "--remove-label", "ai:reviewing",
    )


def test_converge_pipeline_labels_malformed_json_fails_closed() -> None:
    """gh emitting malformed JSON on either read must fail closed and retain
    custody, not raise out of the helper and abort the tick."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    reads: list[object] = [
        {"labels": [{"name": "ai:reviewing"}]},
        json.JSONDecodeError("truncated", "{", 1),
    ]

    with mock.patch("tick.gh_json", side_effect=reads):
        with mock.patch("tick.gh"):
            converged = _converge_pipeline_labels(
                "pr", 42, "ai:ready-for-human",
                tick_log=None, phase="harvest", stage="review",
            )
    assert converged is False

    with mock.patch(
        "tick.gh_json", side_effect=json.JSONDecodeError("truncated", "{", 1),
    ):
        with mock.patch("tick.gh") as gh_mock:
            converged = _converge_pipeline_labels(
                "pr", 42, "ai:ready-for-human",
                tick_log=None, phase="harvest", stage="review",
            )
    assert converged is False
    gh_mock.assert_not_called()


def test_execute_reconcile_actions_kill_escalate_respects_concurrent_hold() -> None:
    """A hold added after decision blocks the escalation label mutation."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    decisions = [{
        "action": "kill_escalate", "label": "ai:needs-human-decision",
        "kill": True, "identity": "matched", "pr_number": 42,
    }]
    label_reads = [
        {"labels": [{"name": "ai:fixing"}, {"name": "ai:blocked"}]},
    ]
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    with mock.patch("tick.gh_json", side_effect=label_reads):
        with mock.patch("tick.gh", side_effect=record_gh):
            ok = _execute_reconcile_actions(decisions, config, wt_root="/tmp")

    assert ok is False
    assert gh_calls == []


def test_converge_pipeline_labels_verification_read_failure_fails_closed() -> None:
    """A mutation whose confirmation read fails must retain custody: a hold
    added concurrently could still be present alongside the target, and the
    retained marker re-converges idempotently on the next tick."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    reads: list[object] = [
        {"labels": [{"name": "ai:reviewing"}]},
        subprocess.CalledProcessError(1, ["gh"]),
    ]

    with mock.patch("tick.gh_json", side_effect=reads):
        with mock.patch("tick.gh"):
            converged = _converge_pipeline_labels(
                "pr", 42, "ai:ready-for-human",
                tick_log=None, phase="harvest", stage="review",
            )

    assert converged is False


def test_converge_pipeline_labels_refuses_to_remove_human_hold() -> None:
    """A human-set ai:needs-human-decision is never removed by convergence."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    labels = {
        "labels": [
            {"name": "ai:reviewing"},
            {"name": "ai:needs-human-decision"},
        ],
    }

    with mock.patch("tick.gh_json", return_value=labels):
        with mock.patch("tick.gh") as gh_mock:
            converged = _converge_pipeline_labels(
                "pr", 42, "ai:ready-for-human",
                tick_log=None, phase="harvest", stage="review",
            )

    assert converged is False
    gh_mock.assert_not_called()


def test_converge_pipeline_labels_exact_human_hold_is_not_claimed() -> None:
    """An already-present hold is human custody, not our landed mutation."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    labels = {"labels": [{"name": "ai:needs-human-decision"}]}

    with mock.patch("tick.gh_json", return_value=labels):
        with mock.patch("tick.gh") as gh_mock:
            with mock.patch("tick._notify_terminal_label") as notify:
                converged = _converge_pipeline_labels(
                    "pr", 42, "ai:needs-human-decision",
                    tick_log=None, phase="harvest", stage="fix",
                )

    assert converged is False
    gh_mock.assert_not_called()
    notify.assert_not_called()


def test_converge_pipeline_labels_refuses_to_remove_blocked_hold() -> None:
    """A human-set ai:blocked blocks convergence toward any automation target."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    labels = {
        "labels": [
            {"name": "ai:implementing"},
            {"name": "ai:blocked"},
        ],
    }

    with mock.patch("tick.gh_json", return_value=labels):
        with mock.patch("tick.gh") as gh_mock:
            converged = _converge_pipeline_labels(
                "issue", 42, "ai:available",
                tick_log=None, phase="harvest", stage="implement",
            )

    assert converged is False
    gh_mock.assert_not_called()


def test_converge_pipeline_labels_escalation_preserves_blocked_hold() -> None:
    """Even escalation cannot replace a label expressing human custody."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    labels = {"labels": [{"name": "ai:fixing"}, {"name": "ai:blocked"}]}

    with mock.patch("tick.gh_json", return_value=labels):
        with mock.patch("tick.gh") as gh_mock:
            converged = _converge_pipeline_labels(
                "pr", 42, "ai:needs-human-decision",
                tick_log=None, phase="harvest", stage="fix",
            )

    assert converged is False
    gh_mock.assert_not_called()


def test_converge_pipeline_labels_notifies_when_retry_finds_terminal_target() -> None:
    """A retry that observes the landed terminal state still emits its signal."""
    from tick import _converge_pipeline_labels  # type: ignore[import-untyped]

    labels = {"labels": [{"name": "ai:ready-for-human"}]}
    with mock.patch("tick.gh_json", return_value=labels):
        with mock.patch("tick._notify_terminal_label") as notify:
            converged = _converge_pipeline_labels(
                "pr", 42, "ai:ready-for-human",
                tick_log=None, phase="harvest", stage="review",
            )

    assert converged is True
    notify.assert_called_once_with(
        "pr", 42, "ai:ready-for-human", None, "harvest", "review",
    )


@pytest.mark.parametrize(
    ("entity", "stage", "prefix", "in_flight", "pre_label"),
    [
        ("issue", "implement", "issue-", "ai:implementing", "ai:available"),
        ("pr", "fix", "", "ai:fixing", "ai:changes-requested"),
    ],
)
def test_terminal_transient_convergence_failure_retains_active_marker(
    tmp_path: Path,
    entity: str,
    stage: str,
    prefix: str,
    in_flight: str,
    pre_label: str,
) -> None:
    """Terminal retry exhaustion keeps active custody until labels converge."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _handle_transient_engine_result,
    )

    config = StageConfig(
        stage=stage,
        worktree_prefix="issue" if entity == "issue" else "pr",
        attempts_key_prefix=prefix,
        result_file="result.json",
        in_flight_label=in_flight,
        pre_label=pre_label,
        strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    number = 7
    wt_dir = wt_root / f"{'issue' if entity == 'issue' else 'pr'}-{number}"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-7"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root), prefix)
    attempts.incr(number)
    attempts.incr(number)

    with mock.patch("tick.gh"), mock.patch(
        "tick._converge_pipeline_labels", return_value=False,
    ):
        handled = _handle_transient_engine_result(
            entity=entity,
            number=number,
            config=config,
            pre_label=pre_label,
            engine="claude",
            result_raw=result_raw,
            harvest_result={"failure_class": "quota_or_limit"},
            dispatch_id="d-7",
            wt_root=str(wt_root),
            wt_dir=str(wt_dir),
            tick_log=None,
            attempts=attempts,
        )

    assert handled is False
    assert (wt_dir / "dispatch.json").is_file()
    assert (wt_dir / "result.json").is_file()
    assert attempts.read(number) == 2
    # The prepared evidence archive is discarded on failure — the retained
    # scratch re-archives on retry, and keeping it would leak one uniquely
    # named directory per tick for the duration of a GitHub outage.
    evidence_root = wt_root / ".harvest-evidence"
    assert not evidence_root.exists() or not any(evidence_root.iterdir())


def test_terminal_transient_exhaustion_clears_attempts_before_archive_move(
    tmp_path: Path,
) -> None:
    """Once the terminal label lands, the attempt counter is cleared before
    the marker is moved — a crash between the move and a later clear would
    strand a stale counter with no marker left to recover it."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _handle_transient_engine_result,
    )

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-7"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root), "issue-")
    attempts.incr(7)
    attempts.incr(7)

    def finalize_asserts_cleared(**_kwargs: object) -> bool:
        assert attempts.read(7) == 0
        return False

    with mock.patch("tick.gh"), mock.patch(
        "tick._converge_pipeline_labels", return_value=True,
    ):
        with mock.patch(
            "tick._finalize_terminal_archive",
            side_effect=finalize_asserts_cleared,
        ):
            handled = _handle_transient_engine_result(
                entity="issue",
                number=7,
                config=config,
                pre_label="ai:available",
                engine="claude",
                result_raw=result_raw,
                harvest_result={"failure_class": "quota_or_limit"},
                dispatch_id="d-7",
                wt_root=str(wt_root),
                wt_dir=str(wt_dir),
                tick_log=None,
                attempts=attempts,
            )

    assert handled is False
    assert attempts.read(7) == 0


def test_transient_exhaustion_posts_before_the_terminal_relabel(
    tmp_path: Path,
) -> None:
    """Exhausted transient retries explain themselves before going red.

    This transition is remote-visible and terminal — the item stops being
    retried and starts waiting for a human — so it carries the same publication
    obligation as every other terminal transition. The post has to land *before*
    the label, or the PR shows ``ai:needs-human-decision`` with no stated reason.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _handle_transient_engine_result,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-7"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root), "")
    attempts.incr(7)
    attempts.incr(7)

    calls: list[str] = []
    bodies: list[str] = []

    def record_converge(*args: object, **_kwargs: object) -> bool:
        calls.append(f"converge:{args[2]}")
        return True

    def record_gh(*args: str) -> object:
        if args[:2] == ("pr", "comment"):
            calls.append("comment")
            with open(args[4]) as fh:
                bodies.append(fh.read())
        return mock.Mock()

    with mock.patch("tick.gh", side_effect=record_gh):
        with mock.patch(
            "tick._converge_pipeline_labels", side_effect=record_converge,
        ):
            with mock.patch(
                "tick._finalize_terminal_archive", return_value=True,
            ):
                with mock.patch("tick.cleanup_scratch"):
                    handled = _handle_transient_engine_result(
                        entity="pr",
                        number=7,
                        config=config,
                        pre_label="ai:changes-requested",
                        engine="claude",
                        result_raw=result_raw,
                        harvest_result={"failure_class": "quota_or_limit"},
                        dispatch_id="d-7",
                        wt_root=str(wt_root),
                        wt_dir=str(wt_dir),
                        tick_log=None,
                        attempts=attempts,
                    )

    assert handled is True
    assert calls == ["comment", "converge:ai:needs-human-decision"]
    assert "quota_or_limit" in bodies[0]
    assert "ai:needs-human-decision" in bodies[0]
    # Nothing is staged inside the branch-controlled worktree.
    assert not list(wt_dir.glob("*.body.txt"))


def test_transient_exhaustion_failed_post_blocks_the_relabel_and_is_bounded(
    tmp_path: Path,
) -> None:
    """A failed publication holds the terminal label and burns one attempt.

    Gating without bounding would retry the post every tick forever while the
    item pins an admission slot; bounding without gating would go red with no
    explanation. The attempt counter is what reconciles the two — repeated post
    failure walks into the cap, which publishes its own superseding escalation.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _handle_transient_engine_result,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-7"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root), "")
    attempts.incr(7)
    attempts.incr(7)

    with mock.patch(
        "tick.gh", side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        with mock.patch("tick._converge_pipeline_labels") as converge:
            handled = _handle_transient_engine_result(
                entity="pr",
                number=7,
                config=config,
                pre_label="ai:changes-requested",
                engine="claude",
                result_raw=result_raw,
                harvest_result={"failure_class": "quota_or_limit"},
                dispatch_id="d-7",
                wt_root=str(wt_root),
                wt_dir=str(wt_dir),
                tick_log=None,
                attempts=attempts,
            )

    assert handled is False
    converge.assert_not_called()
    assert (wt_dir / "result.json").is_file()
    assert attempts.read(7) == 3
    evidence_root = wt_root / ".harvest-evidence"
    assert not evidence_root.exists() or not any(evidence_root.iterdir())


def test_transient_exhaustion_does_not_repost_after_a_failed_relabel(
    tmp_path: Path,
) -> None:
    """A retried exhaustion republishes nothing and cannot contradict itself.

    Unlike the fixer decision, this one is a pure function of an immutable
    ``result.json`` and an attempt counter the exhausted path never advances, so
    the retry recomputes the identical outcome. The receipt therefore suppresses
    the duplicate comment while the relabel converges — no terminal-intent
    record is needed to stop a second tick deciding something else.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _handle_transient_engine_result,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-7"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root), "")
    attempts.incr(7)
    attempts.incr(7)

    bodies: list[str] = []

    def run_once(converge: bool) -> None:
        with mock.patch(
            "tick.gh", side_effect=capture_comment_bodies(bodies),
        ):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=converge,
            ):
                with mock.patch(
                    "tick._finalize_terminal_archive", return_value=True,
                ):
                    with mock.patch("tick.cleanup_scratch"):
                        _handle_transient_engine_result(
                            entity="pr",
                            number=7,
                            config=config,
                            pre_label="ai:changes-requested",
                            engine="claude",
                            result_raw=result_raw,
                            harvest_result={"failure_class": "quota_or_limit"},
                            dispatch_id="d-7",
                            wt_root=str(wt_root),
                            wt_dir=str(wt_dir),
                            tick_log=None,
                            attempts=attempts,
                        )

    run_once(converge=False)
    run_once(converge=True)

    assert len(bodies) == 1
    # The terminal transition retires the receipt it no longer needs.
    assert not list((wt_root / ".receipts").glob("pr-7-*"))


def test_transient_attempt_state_error_publishes_its_terminal_relabel(
    tmp_path: Path,
) -> None:
    """An unwritable attempt counter escalates *with* a published reason.

    This path cannot gate its label on the post the way exhaustion does: the
    retry budget is exactly what failed to persist, so a blocked transition
    would retry forever. The post is therefore best-effort and ordered first —
    the same fail-open shape as the attempt cap.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _handle_transient_engine_result,
    )

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-7"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root), "issue-")

    calls: list[str] = []
    bodies: list[str] = []

    def record_converge(*args: object, **_kwargs: object) -> bool:
        calls.append(f"converge:{args[2]}")
        return True

    def record_gh(*args: str) -> object:
        if args[:2] == ("issue", "comment"):
            calls.append("comment")
            with open(args[4]) as fh:
                bodies.append(fh.read())
        return mock.Mock()

    with mock.patch.object(
        Attempts, "incr", side_effect=OSError("counter unwritable"),
    ):
        with mock.patch("tick.gh", side_effect=record_gh):
            with mock.patch(
                "tick._converge_pipeline_labels", side_effect=record_converge,
            ):
                handled = _handle_transient_engine_result(
                    entity="issue",
                    number=7,
                    config=config,
                    pre_label="ai:available",
                    engine="claude",
                    result_raw=result_raw,
                    harvest_result={"failure_class": "quota_or_limit"},
                    dispatch_id="d-7",
                    wt_root=str(wt_root),
                    wt_dir=str(wt_dir),
                    tick_log=None,
                    attempts=attempts,
                )

    assert handled is False
    # The first converge is the ordinary transient revert to the pre-dispatch
    # label — not remote-visible as a terminal state and not the subject here.
    # The counter write fails after it, and *that* escalation is published
    # before its label lands.
    assert calls == [
        "converge:ai:available", "comment", "converge:ai:needs-human-decision",
    ]
    assert "attempt counter" in bodies[0]


def test_transient_attempt_state_error_relabels_when_the_post_fails(
    tmp_path: Path,
) -> None:
    """The fail-open half of the previous test: a failed post still goes red."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _handle_transient_engine_result,
    )

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-7"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root), "issue-")
    tick_log = TickLog.open(str(tmp_path / "tick-log"))

    with mock.patch.object(
        Attempts, "incr", side_effect=OSError("counter unwritable"),
    ):
        with mock.patch(
            "tick.gh", side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge:
                _handle_transient_engine_result(
                    entity="issue",
                    number=7,
                    config=config,
                    pre_label="ai:available",
                    engine="claude",
                    result_raw=result_raw,
                    harvest_result={"failure_class": "quota_or_limit"},
                    dispatch_id="d-7",
                    wt_root=str(wt_root),
                    wt_dir=str(wt_dir),
                    tick_log=tick_log,
                    attempts=attempts,
                )

    assert converge.call_args[0][2] == "ai:needs-human-decision"
    assert any(
        entry["action"] == "escalation_comment_failed"
        for entry in tick_log.tail()
    )


def test_reconcile_one_retains_corrupt_terminal_marker(
    tmp_path: Path,
) -> None:
    """A corrupt marker may be live custody and must be retained for recovery."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text("{not json")
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", return_value={"labels": []}):
            with mock.patch("tick._remove_worktree") as remove_worktree:
                with mock.patch("tick.cleanup_scratch") as cleanup:
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                        attempts=attempts,
                    )

    assert wt_dir.exists()
    assert (wt_dir / "dispatch.json").is_file()
    remove_worktree.assert_not_called()
    cleanup.assert_not_called()
    attempts.clear.assert_not_called()
    assert decision["identity"] == "corrupt"


def test_reconcile_one_retains_schema_invalid_marker_with_live_pid(
    tmp_path: Path,
) -> None:
    """Schema-invalid marker with a live PID remains durable custody."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "dispatch_id": "partial-42",
        "pid": 99999, "start_time": 1000.0,
    }))
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", return_value={"labels": []}):
            with mock.patch("process_control.pid_is_alive", return_value=True):
                with mock.patch("tick._remove_worktree") as remove_worktree:
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                        attempts=attempts,
                    )

    assert wt_dir.exists()
    remove_worktree.assert_not_called()
    attempts.clear.assert_not_called()
    assert decision["identity"] == "corrupt"


def test_reconcile_one_archives_schema_invalid_marker_with_dead_pid(
    tmp_path: Path,
) -> None:
    """Recoverable corrupt custody is archived once its PID is proven dead."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "dispatch_id": "partial-42",
        "pid": 99999, "start_time": 1000.0,
    }))
    (wt_dir / "recovery.txt").write_text("corrupt marker evidence")
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", return_value={"labels": []}):
            with mock.patch("process_control.pid_is_alive", return_value=False):
                decision = _reconcile_one(
                    42, str(wt_dir), config, tick_log=None, attempts=attempts,
                )

    assert not wt_dir.exists()
    archive = tmp_path / ".harvest-evidence" / "pr-42-partial-42" / "scratch"
    assert (archive / "recovery.txt").read_text() == "corrupt marker evidence"
    attempts.clear.assert_called_once_with(42)
    assert decision["identity"] == "dead"


def test_reconcile_one_retains_sibling_stage_marker_on_terminal_pr(
    tmp_path: Path,
) -> None:
    """A fixer marker must not be cleaned up during the reviewer pass.

    When the reviewer stage encounters a valid fix-stage marker on a terminal
    PR, it must leave the marker alone for the fixer stage to handle — not
    treat it as corrupt and delete the worktree/marker.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    # A structurally valid fix-stage marker (not corrupt — just wrong stage)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "fix", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:changes-requested",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 999999.0,
        "pgid": 99999,
    }))
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", return_value={"state": "MERGED", "labels": []}):
            with mock.patch("tick._remove_worktree") as remove_wt:
                with mock.patch("tick.cleanup_scratch") as cleanup:
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                        attempts=attempts,
                    )

    # Sibling-stage marker must NOT be cleaned up or treated as corrupt
    remove_wt.assert_not_called()
    cleanup.assert_not_called()
    attempts.clear.assert_not_called()
    assert decision["identity"] == "sibling_stage"


def test_reconcile_one_cleans_markerless_terminal_plain_directory(
    tmp_path: Path,
) -> None:
    """Markerless terminal scratch must be removed even when it is not a git worktree."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", return_value={"state": "MERGED", "labels": []}):
            with mock.patch("tick._remove_worktree", return_value=False):
                with mock.patch("tick.cleanup_scratch") as cleanup:
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                        attempts=attempts,
                    )

    cleanup.assert_called_once_with(str(wt_dir))
    attempts.clear.assert_called_once_with(42)
    assert decision["identity"] == "markerless"


def test_reconcile_one_uses_marker_pre_label_for_ai_revised(
    tmp_path: Path,
) -> None:
    """_reconcile_one passes marker pre_label="ai:revised" to reconcile_dispatch.

    The marker's recorded pre_label is the authority for custody and stale
    reverts. A review that was dispatched against ai:revised (re-review path)
    must NOT revert to ai:implemented.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:revised",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 9999999999.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.gh_text") as gh_text:
        gh_text.return_value = "OPEN"
        with mock.patch("tick.gh_json") as gh_json:
            gh_json.return_value = {
                "labels": [{"name": "ai:reviewing"}],
            }
            with mock.patch("process_control.get_process_start_time") as gps:
                gps.return_value = 9999999999.0
                with mock.patch(
                    "tick.pipeline.reconcile_dispatch",
                    wraps=lambda **kw: {"action": "harvest", "kill": False, "label": ""},
                ) as reconcile_dispatch:
                    _reconcile_one(42, str(wt_dir), config, tick_log=None)

    # reconcile_dispatch must receive pre_label from the marker, not config
    call_kwargs = reconcile_dispatch.call_args[1]
    assert call_kwargs["pre_label"] == "ai:revised", (
        f"Expected marker pre_label 'ai:revised', got {call_kwargs['pre_label']!r}"
    )


def test_reconcile_one_falls_back_to_config_pre_label_when_marker_missing(
    tmp_path: Path,
) -> None:
    """_reconcile_one uses config.pre_label when marker has no pre_label field."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="implement", worktree_prefix="issue", attempts_key_prefix="issue-",
        result_file="result.json", in_flight_label="ai:implementing",
        pre_label="ai:available", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_dir = tmp_path / "issue-7"
    wt_dir.mkdir()
    # Marker without pre_label field
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 7,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc",
        "head_sha": "", "pre_label": "",  # empty string — not a valid pre_label
        "dispatch_id": "d-7", "pid": 12345, "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    with mock.patch("tick.gh_text") as gh_text:
        gh_text.return_value = "OPEN"
        with mock.patch("tick.gh_json") as gh_json:
            gh_json.return_value = {
                "labels": [{"name": "ai:implementing"}],
            }
            with mock.patch("process_control.get_process_start_time") as gps:
                gps.return_value = 9999999999.0
                with mock.patch(
                    "tick.pipeline.reconcile_dispatch",
                    wraps=lambda **kw: {"action": "harvest", "kill": False, "label": ""},
                ) as reconcile_dispatch:
                    _reconcile_one(7, str(wt_dir), config, tick_log=None)

    # reconcile_dispatch must fall back to config.pre_label ("ai:available")
    call_kwargs = reconcile_dispatch.call_args[1]
    assert call_kwargs["pre_label"] == "ai:available", (
        f"Expected config fallback 'ai:available', got {call_kwargs['pre_label']!r}"
    )


# --- dispatch_id linkage: harvest log carries the same dispatch_id as launch ---


def test_reconcile_one_logs_dispatch_id_from_marker_in_harvest_entry(
    tmp_path: Path,
) -> None:
    """_reconcile_one harvest log entry must include dispatch_id from the marker.

    The dispatch_id links a dispatch entry to its harvest entry across ticks.
    Without it, a log consumer cannot correlate launch → harvest/completion.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import TickLog, _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42-1712345678",
        "pid": 99999, "start_time": 9999999999.0, "pgid": 99999,
    }))
    tick_log = TickLog.open(str(tmp_path))

    with mock.patch("tick.gh_text") as gh_text:
        gh_text.return_value = "OPEN"
        with mock.patch("tick.gh_json") as gh_json:
            gh_json.return_value = {
                "labels": [{"name": "ai:reviewing"}],
            }
            with mock.patch("process_control.get_process_start_time") as gps:
                gps.return_value = 9999999999.0
                with mock.patch(
                    "tick.pipeline.reconcile_dispatch",
                    wraps=lambda **kw: {"action": "harvest", "kill": False,
                                        "label": ""},
                ):
                    _reconcile_one(42, str(wt_dir), config, tick_log)

    entries = tick_log.tail()
    harvest_entries = [e for e in entries if e.get("phase") == "harvest"]
    assert len(harvest_entries) >= 1, (
        f"Expected at least one harvest entry, got {entries}"
    )
    assert harvest_entries[0]["dispatch_id"] == "review-42-1712345678", (
        f"dispatch_id mismatch: {harvest_entries[0].get('dispatch_id')!r}"
    )


# --- Adversarial review: minors — exit code and target parsing ---


def test_main_returns_nonzero_on_reconcile_failure(tmp_path: Path) -> None:
    """main() must return non-zero when reconciliation fails."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.return_value = False  # reconcile failed
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch("tick.dispatch_reviewers"):
                        result = main()

    assert result != 0


def test_main_skips_dispatch_when_reconcile_failed(tmp_path: Path) -> None:
    """main() must NOT call dispatch functions when reconcile_ok is False.

    Even if read_authoritative_in_flight_labels succeeds, dispatch must be
    gated on reconcile_ok — a failed harvest enumeration means the in-flight
    picture is incomplete and launching new engines risks over-admission.
    """
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.return_value = False  # reconcile failed
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch("tick.read_authoritative_in_flight_labels") as rl:
                        rl.return_value = []  # succeeds despite harvest failure
                        with mock.patch("tick.dispatch_reviewers") as dr:
                            with mock.patch("tick.dispatch_implementers") as di:
                                with mock.patch("tick.dispatch_fixers") as df:
                                    result = main()

    assert result != 0
    dr.assert_not_called()
    di.assert_not_called()
    df.assert_not_called()


def test_main_returns_2_when_reconcile_ok_but_auth_labels_read_fails(
    tmp_path: Path,
) -> None:
    """main() must return 2 when reconcile passes but auth labels fail.

    When all three HARVEST phases succeed but read_authoritative_in_flight_labels
    raises CalledProcessError, dispatch is silently skipped and the exit code
    must distinguish this case from a normal successful tick (0) or a reconcile
    failure (1). Monitoring observing exit codes can then alert on persistent
    dispatch-skip patterns.
    """
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels"
                    ) as rl:
                        rl.side_effect = subprocess.CalledProcessError(1, ["gh"])
                        with mock.patch("tick.dispatch_reviewers") as dr:
                            with mock.patch(
                                "tick.dispatch_implementers"
                            ) as di:
                                with mock.patch(
                                    "tick.dispatch_fixers"
                                ) as df:
                                    result = main()

    assert result == 2
    dr.assert_not_called()
    di.assert_not_called()
    df.assert_not_called()


def test_main_skips_dispatch_on_malformed_authoritative_payload(
    tmp_path: Path,
) -> None:
    """An exit-zero but schema-invalid admission read fails closed."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git", return_value=str(tmp_path)):
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch("tick.gh_json", return_value={}):
                        with mock.patch("tick.dispatch_reviewers") as dr:
                            with mock.patch("tick.dispatch_implementers") as di:
                                with mock.patch("tick.dispatch_fixers") as df:
                                    with mock.patch(
                                        "tick._compute_post_dispatch_state",
                                        return_value=(0, False, ()),
                                    ):
                                        result = main()

    assert result == 2
    dr.assert_not_called()
    di.assert_not_called()
    df.assert_not_called()


def test_main_logs_tick_complete_on_reconcile_failure(tmp_path: Path) -> None:
    """main() must log tick_complete before returning on reconcile failure.

    The skip_reconcile_failed path (return 1) currently skips summary logging,
    which means a log consumer cannot query tick_complete to distinguish
    reconcile_ok=false from a crash. The summary is most needed on failure paths.
    """
    import tick  # type: ignore[import-untyped]
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.return_value = False  # reconcile failed → return 1
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch("tick.dispatch_reviewers"):
                        with mock.patch("tick.dispatch_implementers"):
                            with mock.patch("tick.dispatch_fixers"):
                                result = main()

    assert result == 1
    # Verify tick_complete was logged
    log_dir = Path(tick.TICK_LOG_DIR)
    log_files = sorted(log_dir.glob("tick-*.jsonl"))
    assert len(log_files) >= 1, f"No tick log found in {log_dir}"
    entries = [
        json.loads(line) for line in log_files[-1].read_text().splitlines()
    ]
    tick_complete_entries = [
        e for e in entries if e.get("action") == "tick_complete"
    ]
    assert len(tick_complete_entries) == 1, (
        f"Expected 1 tick_complete entry, got {tick_complete_entries}"
    )


def test_main_logs_tick_complete_on_auth_labels_failure(tmp_path: Path) -> None:
    """main() must log tick_complete before returning on auth labels failure.

    The skip_auth_labels_failed path (return 2) currently skips summary logging,
    which means a log consumer cannot query tick_complete to distinguish
    dispatch_skipped from a crash. The summary is most needed on failure paths.
    """
    import tick  # type: ignore[import-untyped]
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels"
                    ) as rl:
                        rl.side_effect = subprocess.CalledProcessError(1, ["gh"])
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    result = main()

    assert result == 2
    # Verify tick_complete was logged
    log_dir = Path(tick.TICK_LOG_DIR)
    log_files = sorted(log_dir.glob("tick-*.jsonl"))
    assert len(log_files) >= 1, f"No tick log found in {log_dir}"
    entries = [
        json.loads(line) for line in log_files[-1].read_text().splitlines()
    ]
    tick_complete_entries = [
        e for e in entries if e.get("action") == "tick_complete"
    ]
    assert len(tick_complete_entries) == 1, (
        f"Expected 1 tick_complete entry, got {tick_complete_entries}"
    )


def test_main_passes_impl_engine_to_dispatch_implementers(
    tmp_path: Path,
) -> None:
    """main() must pass impl_engine through to dispatch_implementers.

    The impl_engine parameter exists on dispatch_implementers() but was not
    wired through main(). Without this passthrough, operators cannot select
    claude-ds for implementation engines via the CLI.
    """
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels",
                        return_value=[],
                    ):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch(
                                "tick.dispatch_implementers"
                            ) as di:
                                with mock.patch("tick.dispatch_fixers"):
                                    main(impl_engine="claude-ds")

    di.assert_called_once()
    call_kwargs = di.call_args[1]
    assert call_kwargs["impl_engine"] == "claude-ds", (
        f"Expected impl_engine='claude-ds', got {call_kwargs.get('impl_engine')!r}"
    )


def test_main_claude_ds_no_model_uses_deepseek_default(
    tmp_path: Path,
) -> None:
    """main(impl_engine='claude-ds') without model resolves to deepseek-v4-pro[1m].

    Regression: the Python default for model at the main() level was "sonnet",
    which is truthy and would override _resolve_engine's default. Using ""
    as sentinel ensures each engine keeps its own default.
    """
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels",
                        return_value=[],
                    ):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch(
                                "tick.dispatch_implementers"
                            ) as di:
                                with mock.patch("tick.dispatch_fixers"):
                                    main(impl_engine="claude-ds")

    di.assert_called_once()
    di_kwargs = di.call_args[1]
    # model="" (sentinel) reaches dispatch_implementers; resolution to
    # "deepseek-v4-pro[1m]" happens in _dispatch_one_implementer.
    assert di_kwargs["model"] == "", (
        f"Expected model='' (sentinel) in dispatch_implementers, "
        f"got {di_kwargs.get('model')!r}"
    )


def test_main_passes_model_to_dispatch_implementers_and_fixers(
    tmp_path: Path,
) -> None:
    """main() must pass model through to dispatch_implementers and dispatch_fixers."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels",
                        return_value=[],
                    ):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch(
                                "tick.dispatch_implementers"
                            ) as di:
                                with mock.patch(
                                    "tick.dispatch_fixers"
                                ) as df:
                                    main(model="claude-sonnet-4-6")

    di.assert_called_once()
    di_kwargs = di.call_args[1]
    assert di_kwargs["model"] == "claude-sonnet-4-6", (
        f"Expected model='claude-sonnet-4-6' in dispatch_implementers, "
        f"got {di_kwargs.get('model')!r}"
    )

    df.assert_called_once()
    df_kwargs = df.call_args[1]
    assert df_kwargs["model"] == "claude-sonnet-4-6", (
        f"Expected model='claude-sonnet-4-6' in dispatch_fixers, "
        f"got {df_kwargs.get('model')!r}"
    )


def test_main_passes_review_model_and_effort_to_dispatch_reviewers(
    tmp_path: Path,
) -> None:
    """main() threads the reviewer overrides all the way to reviewer dispatch.

    The path runs main → _run_single_tick_locked → _run_tick_body →
    dispatch_reviewers; a missed parameter at any hop silently reverts the
    reviewer to the operator config.
    """
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels",
                        return_value=[],
                    ):
                        with mock.patch("tick.dispatch_reviewers") as dr:
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    main(
                                        review_model="gpt-6-preview",
                                        review_effort="xhigh",
                                    )

    dr.assert_called_once()
    dr_kwargs = dr.call_args[1]
    assert dr_kwargs["review_model"] == "gpt-6-preview", (
        f"Expected review_model to reach dispatch_reviewers, "
        f"got {dr_kwargs.get('review_model')!r}"
    )
    assert dr_kwargs["review_effort"] == "xhigh", (
        f"Expected review_effort to reach dispatch_reviewers, "
        f"got {dr_kwargs.get('review_effort')!r}"
    )


def test_parse_target_rejects_unrecognized_format() -> None:
    """_parse_target must reject unrecognized target formats with non-zero exit."""
    import pytest as _pytest
    from tick import _parse_target  # type: ignore[import-untyped]

    # pr: without number
    with _pytest.raises(SystemExit):
        _parse_target("pr:abc")

    # # without number
    with _pytest.raises(SystemExit):
        _parse_target("#abc")

    # Bare word without prefix
    with _pytest.raises(SystemExit):
        _parse_target("nonsense")


# --- Round 2: R2-F2 — _classify_marker deadline coercion ---


def test_classify_marker_coerces_missing_deadline_to_inf() -> None:
    """_classify_marker must coerce a missing deadline to +inf, not 0.0.

    A deadline of 0.0 with a live PID produces ``"hung"``, freeing the slot.
    A deadline of +inf produces ``"live"``, failing closed on spend.
    """
    from tick import _classify_marker  # type: ignore[import-untyped]

    marker: dict[str, object] = {
        "engine": "codex",
        "stage": "review",
        "pr_number": 1,
        # deadline intentionally missing
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-1",
        "pid": 99999,
        "start_time": 999999.0,
        "pgid": 99999,
    }

    with mock.patch("process_control.get_process_start_time") as gps:
        gps.return_value = 999999.0  # matched identity → pid_alive=True
        state, identity, pid_alive = _classify_marker(
            "/nonexistent", marker, result_file="review.json",
        )

    # With +inf deadline and alive PID: must be "live", not "hung"
    assert state == "live", f"expected live, got {state} (deadline missing → must be +inf)"
    assert pid_alive is True


def test_classify_marker_coerces_bool_deadline_to_inf() -> None:
    """_classify_marker must coerce a boolean deadline to +inf."""
    from tick import _classify_marker  # type: ignore[import-untyped]

    marker: dict[str, object] = {
        "engine": "codex",
        "stage": "review",
        "pr_number": 1,
        "deadline": True,  # bool, not float
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-1",
        "pid": 99999,
        "start_time": 999999.0,
        "pgid": 99999,
    }

    with mock.patch("process_control.get_process_start_time") as gps:
        gps.return_value = 999999.0
        state, _identity, _pid_alive = _classify_marker(
            "/nonexistent", marker, result_file="review.json",
        )

    assert state == "live", f"expected live, got {state} (bool deadline → must be +inf)"


def test_classify_marker_handles_invalid_pid_with_wrapperless_result(
    tmp_path: Path,
) -> None:
    """_classify_marker must not crash when pid is invalid and result exists.

    When pid is not a valid int (e.g. None or missing), the pid-validation
    block is skipped. The in-memory wrapperless-completion path must be safe
    to reach without process identity fields.
    """
    from tick import _classify_marker  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-99"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text('{"ok": true}')

    marker: dict[str, object] = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:available",
        "dispatch_id": "d-99",
        "start_time": 1234567.0,
        # pid intentionally missing — triggers the unbound variable path
    }

    # Must not raise NameError on recorded_start
    state, identity, pid_alive = _classify_marker(
        str(wt_dir), marker, result_file="result.json",
    )

    # This helper is called only after production marker validation. Directly,
    # a wrapperless result plus no live PID is enough to classify complete.
    assert state == "complete", f"expected complete, got {state}"
    assert pid_alive is False
    assert not (wt_dir / "completion.json").exists()


def test_classify_marker_completes_proven_dead_pid_without_synthetic_envelope(
    tmp_path: Path,
) -> None:
    """A provably dead engine with a result must classify complete.

    ``verify_identity`` returns "unknown" for a dead PID (start time
    unreadable), which counts as pid-alive under the custody rule. Without
    a signal-zero existence probe, a finished wrapper-less engine
    (implementer/fixer — no completion envelope) classifies "live" until
    its deadline and then "hung" → escalation: its result would NEVER be
    harvested normally. ProcessLookupError from ``kill(pid, 0)`` proves
    death, unlocking in-memory wrapperless completion and harvest.
    """
    from tick import _classify_marker  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-99"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text('{"result": "IMPLEMENTATION_COMPLETE"}')

    marker: dict[str, object] = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:available",
        "dispatch_id": "d-99",
        "pid": 12345,
        "start_time": 1234567.0,
        "pgid": 12345,
    }

    with mock.patch(
        "tick.process_control.get_process_start_time", return_value=None,
    ):
        with mock.patch(
            "tick.process_control.pid_is_alive", return_value=False,
        ):
            state, identity, pid_alive = _classify_marker(
                str(wt_dir), marker, result_file="result.json",
            )

    assert state == "complete", f"expected complete, got {state}"
    assert identity == "dead"
    assert pid_alive is False
    assert not (wt_dir / "completion.json").exists(), (
        "wrapperless completion must not fabricate a verified zero exit status"
    )


def test_classify_marker_ignores_a_foreign_process_on_the_markers_pgid(
    tmp_path: Path,
) -> None:
    """A stranger occupying the marker's PGID must not resurrect a dead engine.

    This is the concrete flake the sibling ``_classify_marker`` tests hit: they
    invent ``pgid`` values, and when an unrelated process happens to hold one,
    ``_marker_process_group_is_alive`` reads it as live and the marker
    classifies ``live`` instead of ``complete``. Rather than wait for a
    collision, this spawns a real process in its own group and puts *its* PGID
    in the marker, so the hazard is reproduced on every run.
    """
    from tick import _classify_marker  # type: ignore[import-untyped]

    proc = spawn_foreign_process()
    try:
        live_pgid = os.getpgid(proc.pid)
        wt_dir, marker = foreign_process_marker(
            tmp_path, pid=proc.pid, pgid=live_pgid,
        )

        with mock.patch(
            "tick.process_control.get_process_start_time", return_value=None,
        ):
            with mock.patch(
                "tick.process_control.pid_is_alive", return_value=False,
            ):
                state, _identity, pid_alive = _classify_marker(
                    wt_dir, marker, result_file="result.json",
                )
    finally:
        reap(proc)

    assert state == "complete", f"expected complete, got {state}"
    assert pid_alive is False


def test_classify_marker_reads_a_foreign_process_pid_as_dead_not_mismatched(
    tmp_path: Path,
) -> None:
    """The identity read is a probe too, and it decides which branch runs.

    The sibling PGID test mocks the start-time read away to isolate the group
    probe. Nothing mocks it here — that is the point. A marker naming a live
    foreign PID must still resolve the way a dead one does: ``verify_identity``
    must see ``None`` and answer ``unknown``, so the classifier falls through
    to the two signal-zero probes and — both answering dead — settles on
    ``dead``.

    Unguarded, ``ps``/``/proc`` hands back the stranger's real start time, the
    answer becomes ``mismatched``, and the probes are never consulted at all —
    a different custody branch reached for a reason that depends on nothing
    but which PIDs the developer's machine happens to be using. ``dead`` is the
    terminal answer only *because* the guard held; asserting it therefore still
    catches an unconfined start-time read.
    """
    from tick import _classify_marker  # type: ignore[import-untyped]

    proc = spawn_foreign_process()
    try:
        wt_dir, marker = foreign_process_marker(
            tmp_path, pid=proc.pid, pgid=os.getpgid(proc.pid),
        )

        state, identity, pid_alive = _classify_marker(
            wt_dir, marker, result_file="result.json",
        )
    finally:
        reap(proc)

    assert identity == "dead", f"expected dead, got {identity}"
    assert state == "complete", f"expected complete, got {state}"
    assert pid_alive is False


def test_classify_marker_requires_result_json_for_wrapperless_stage(
    tmp_path: Path,
) -> None:
    """A stale review artifact cannot complete a wrapperless fixer dispatch."""
    from tick import _classify_marker  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-99"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "changes_requested"}')
    marker: dict[str, object] = {
        "engine": "claude",
        "stage": "fix",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-99",
        "pid": 12345,
        "start_time": 1234567.0,
        "pgid": 12345,
    }

    with mock.patch(
        "tick.process_control.get_process_start_time", return_value=None,
    ):
        with mock.patch("tick.process_control.pid_is_alive", return_value=False):
            state, _identity, pid_alive = _classify_marker(
                str(wt_dir), marker, result_file=None,
            )

    assert state == "stale"
    assert pid_alive is False


def test_classify_marker_recognizes_dispatch_bound_reviewer_failure(
    tmp_path: Path,
) -> None:
    from tick import _classify_marker  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-99"
    wt_dir.mkdir()
    marker: dict[str, object] = {
        "engine": "codex", "stage": "review", "pr_number": 99,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-99", "pid": 12345,
        "start_time": 1234567.0, "pgid": 12345,
    }
    (wt_dir / "completion.json").write_text(json.dumps({
        "dispatch_id": "review-99", "exit_status": 1, "wrapper_pid": 12345,
        "start_time": 1234567.0, "result_valid": False,
        "error_class": "EngineExitError",
    }))

    with mock.patch("tick.process_control.get_process_start_time", return_value=None):
        with mock.patch("tick.process_control.pid_is_alive", return_value=False):
            state, _identity, pid_alive = _classify_marker(
                str(wt_dir), marker, result_file="review.json",
            )

    assert state == "failed"
    assert pid_alive is False


def test_classify_marker_rejects_mismatched_reviewer_failure_envelope(
    tmp_path: Path,
) -> None:
    from tick import _classify_marker  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-99"
    wt_dir.mkdir()
    marker: dict[str, object] = {
        "engine": "codex", "stage": "review", "pr_number": 99,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-99", "pid": 12345,
        "start_time": 1234567.0, "pgid": 12345,
    }
    (wt_dir / "completion.json").write_text(json.dumps({
        "dispatch_id": "other", "exit_status": 1, "wrapper_pid": 12345,
        "start_time": 1234567.0, "result_valid": False,
        "error_class": "EngineExitError",
    }))

    with mock.patch("tick.process_control.get_process_start_time", return_value=None):
        with mock.patch("tick.process_control.pid_is_alive", return_value=False):
            state, _identity, _pid_alive = _classify_marker(
                str(wt_dir), marker, result_file="review.json",
            )

    assert state == "stale"


def test_classify_marker_retains_custody_when_pid_probe_alive(
    tmp_path: Path,
) -> None:
    """An existing-but-unreadable process stays in custody (live)."""
    from tick import _classify_marker  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-99"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text('{"result": "IMPLEMENTATION_COMPLETE"}')

    marker: dict[str, object] = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:available",
        "dispatch_id": "d-99",
        "pid": 12345,
        "start_time": 1234567.0,
        "pgid": 12345,
    }

    with mock.patch(
        "tick.process_control.get_process_start_time", return_value=None,
    ):
        with mock.patch(
            "tick.process_control.pid_is_alive", return_value=True,
        ):
            state, _identity, pid_alive = _classify_marker(
                str(wt_dir), marker, result_file="result.json",
            )

    assert state == "live", f"expected live (retain custody), got {state}"
    assert pid_alive is True
    assert not (wt_dir / "completion.json").exists()


def test_live_wrapperless_engine_cannot_complete_from_matching_envelope(
    tmp_path: Path,
) -> None:
    """A wrapperless engine may not declare its own completion.

    Implement and fix run without ``engine_wrapper.py``: ``--envelope-path`` is
    passed at exactly one dispatch site (the reviewer's), so nothing legitimate
    ever writes ``completion.json`` into their worktree. The engine itself can:
    it holds an unrestricted ``Write`` tool, its ``cwd`` *is* the worktree, and
    ``dispatch.json`` — the one value an envelope must match — sits there for it
    to read. ``validate_completion_envelope`` checks shape and dispatch id, and
    binds nothing to the real wrapper pid, the stage, or the writer.

    Believing such a file classifies a running engine ``complete``, which
    suppresses its own liveness (``classification_pid_alive``), harvests it out
    of marker custody while it is still spending, and publishes the result it
    wrote for itself. The process is real here and so is the envelope: what the
    assertion pins is that the *stage*, not the file's validity, decides whether
    an envelope may speak.
    """
    import pathlib

    import pipeline  # type: ignore[import-untyped]
    import process_control  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]
    from tick import (  # type: ignore[import-untyped]
        _classify_marker,
        _scan_marker_identities,
        can_dispatch_now,
    )

    # The suite's liveness guard confines every probe to the process running it,
    # so this is the only genuinely-live pid a test may observe — and it is a
    # real one, with a real start time, read through the real helper.
    live_pid = os.getpid()
    live_start = process_control.get_process_start_time(live_pid)
    assert live_start is not None, "the test process has no readable start"

    # The real marker root the production scan reads, not a bare tmp dir: this
    # marker has to be visible to `_scan_marker_identities` and `can_dispatch_now`
    # exactly as a dispatched one would be.
    wt_root = pathlib.Path(tick.WT_ROOT)
    wt_dir = wt_root / "issue-99"
    wt_dir.mkdir(parents=True)
    marker: dict[str, object] = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:available",
        "dispatch_id": "impl-99",
        "pid": live_pid,
        "start_time": live_start,
        "pgid": os.getpgrp(),
    }
    (wt_dir / "dispatch.json").write_text(json.dumps(marker))

    # What the engine can write for itself, from its own cwd.
    (wt_dir / "result.json").write_text('{"result": "IMPLEMENTATION_COMPLETE"}')
    forged = {
        "dispatch_id": "impl-99",
        "exit_status": 0,
        "wrapper_pid": live_pid,
        "start_time": live_start,
        "result_valid": True,
    }
    (wt_dir / "completion.json").write_text(json.dumps(forged))

    # The file is genuinely acceptable to the validator: this test must fail
    # because the stage may not carry an envelope, never because the fixture
    # happened to be malformed.
    assert pipeline.validate_envelope(
        envelope=forged, dispatch_id="impl-99",
    ) is True

    state, identity, engine_alive = _classify_marker(
        str(wt_dir), marker, result_file="result.json",
    )

    assert identity == "matched"
    assert engine_alive is True
    assert state == "live", (
        f"a live wrapperless engine classified {state!r} from an envelope it "
        "wrote itself"
    )

    # Custody follows: harvest must not reap it, and it keeps its slot.
    decision = pipeline.reconcile_dispatch(
        current_label=pipeline.LABEL_IMPLEMENTING,
        marker_valid=True,
        classify_state=state,
        pre_label="ai:available",
        in_flight_label=pipeline.LABEL_IMPLEMENTING,
    )
    assert decision["action"] == "leave", decision

    # And the production accounting path agrees, read off the same file rather
    # than a tuple this test typed: the real scan finds the marker, and the real
    # admission gate still counts its slot.
    scanned = _scan_marker_identities()
    assert scanned == [
        ("issue", 99, pipeline.LABEL_IMPLEMENTING, "live", "matched", True),
    ], scanned
    result = can_dispatch_now(max_in_flight=1)
    assert result.in_flight_count == 1, "the running engine lost its slot"
    assert result.can_dispatch is False


def test_engine_looks_finished_ignores_a_wrapperless_engines_own_envelope(
) -> None:
    """The stall probe trusts the same file the classifier stopped trusting.

    ``_engine_looks_finished`` short-circuits on a wrapper artifact before it
    probes liveness. For a wrapperless stage that artifact is engine-writable
    (see :data:`tick._WRAPPER_ENVELOPE_STAGES`), so a running implementer could
    make the loop treat it as finished by writing one — the same forgery
    ``_classify_marker`` rejects, reached through a different door. Fixing one
    reader and not the other leaves the contract half-applied.
    """
    import pathlib

    import tick  # type: ignore[import-untyped]

    wt_dir = pathlib.Path(tick.WT_ROOT) / "issue-77"
    wt_dir.mkdir(parents=True)
    (wt_dir / "completion.json").write_text('{"dispatch_id": "impl-77"}')

    marker: dict[str, object] = {
        "stage": "implement",
        "dispatch_id": "impl-77",
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
    }

    assert tick._engine_looks_finished(str(wt_dir), marker) is False, (
        "a live wrapperless engine looked finished from a file it can write"
    )

    # The same file is real evidence for the stage that has a wrapper.
    reviewer_marker = dict(marker, stage="review")
    assert tick._engine_looks_finished(str(wt_dir), reviewer_marker) is True


def test_existing_marker_blocks_dispatch_archives_markerless_dir(
    tmp_path: Path,
) -> None:
    """A retained markerless directory is archived before redispatch cleanup."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text("{}")
    (wt_dir / "stray.txt").write_text("junk")

    blocked = _existing_marker_blocks_dispatch(str(wt_dir))

    assert blocked is False
    assert not wt_dir.exists()
    archives = list((tmp_path / ".harvest-evidence").glob("pr-42-retained-markerless"))
    assert len(archives) == 1
    assert (archives[0] / "result.json").read_text() == "{}"
    assert (archives[0] / "scratch" / "stray.txt").read_text() == "junk"


def test_existing_marker_blocks_dispatch_keeps_markerless_incidents_separate(
    tmp_path: Path,
) -> None:
    """Repeated markerless recovery never overwrites an earlier incident."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "incident.txt").write_text("first")

    assert _existing_marker_blocks_dispatch(str(wt_dir)) is False

    wt_dir.mkdir()
    (wt_dir / "incident.txt").write_text("second")

    assert _existing_marker_blocks_dispatch(str(wt_dir)) is False

    archives = sorted(
        (tmp_path / ".harvest-evidence").glob("pr-42-retained-markerless*"),
    )
    assert len(archives) == 2
    incidents = {
        (archive / "scratch" / "incident.txt").read_text()
        for archive in archives
    }
    assert incidents == {"first", "second"}


def test_existing_marker_blocks_dispatch_preserves_legacy_markerless_archive(
    tmp_path: Path,
) -> None:
    """A pre-full-snapshot archive is never reused for a new incident."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    archive_root = tmp_path / ".harvest-evidence"
    legacy_archive = archive_root / "pr-42-retained-markerless"
    legacy_archive.mkdir(parents=True)
    (legacy_archive / "result.json").write_text("legacy evidence")
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "incident.txt").write_text("new evidence")

    assert _existing_marker_blocks_dispatch(str(wt_dir)) is False

    archives = sorted(archive_root.glob("pr-42-retained-markerless*"))
    assert len(archives) == 2
    assert (legacy_archive / "result.json").read_text() == "legacy evidence"
    new_archives = [archive for archive in archives if archive != legacy_archive]
    assert len(new_archives) == 1
    assert (
        new_archives[0] / "scratch" / "incident.txt"
    ).read_text() == "new evidence"


def test_existing_marker_blocks_dispatch_archives_markerless_reviewer_evidence(
    tmp_path: Path,
) -> None:
    """Markerless reviewer output and completion proof survive redispatch."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "changes_requested"}')
    (wt_dir / "completion.json").write_text('{"exit_status": 0}')

    blocked = _existing_marker_blocks_dispatch(str(wt_dir))

    assert blocked is False
    assert not wt_dir.exists()
    archive = tmp_path / ".harvest-evidence" / "pr-42-retained-markerless"
    assert (archive / "review.json").is_file()
    assert (archive / "completion.json").is_file()


def test_archive_harvest_evidence_uses_explicit_reviewer_stage(
    tmp_path: Path,
) -> None:
    """Reviewer evidence remains correlated to the review stage in tick logs."""
    from tick import TickLog, _archive_harvest_evidence  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "changes_requested"}')
    tick_log = TickLog.open(str(tmp_path / "logs"))

    archive_dir = _archive_harvest_evidence(
        wt_root=str(wt_root),
        wt_dir=str(wt_dir),
        kind="pr",
        number=42,
        dispatch_id="review-42",
        tick_log=tick_log,
        stage="review",
    )

    assert archive_dir
    evidence = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "harvest_evidence"
    ]
    assert len(evidence) == 1
    assert evidence[0]["stage"] == "review"


def test_archive_harvest_evidence_completes_interrupted_reuse(
    tmp_path: Path,
) -> None:
    """An archive interrupted during preparation is repaired before reuse."""
    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "result.json").write_text('{"result": "full output"}')
    (wt_dir / "dispatch.json").write_text('{"dispatch_id": "impl-42"}')
    archive = wt_root / ".harvest-evidence" / "issue-42-impl-42"
    archive.mkdir(parents=True)
    (archive / "result.json").write_text('{"res')

    prepared = archive_with_reuse(wt_root, wt_dir)

    assert prepared == str(archive)
    assert (archive / "result.json").read_text() == '{"result": "full output"}'
    assert (archive / "dispatch.json").is_file()
    assert list(archive.parent.iterdir()) == [archive]
    # A completed archive is reused as prepared, not copied again.
    (wt_dir / "result.json").write_text('{"result": "changed"}')
    assert archive_with_reuse(wt_root, wt_dir) == str(archive)
    assert (archive / "result.json").read_text() == '{"result": "full output"}'


def test_archive_harvest_evidence_keeps_existing_archive_when_repair_fails(
    tmp_path: Path,
) -> None:
    """A failed repair never deletes a directory a report may already name."""
    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir(parents=True)
    archive = wt_root / ".harvest-evidence" / "issue-42-impl-42"
    archive.mkdir(parents=True)
    (archive / "stderr.log").write_text("partial")

    assert archive_with_reuse(wt_root, wt_dir) == ""
    assert (archive / "stderr.log").read_text() == "partial"


def test_archive_harvest_evidence_failed_copy_keeps_published_contents(
    tmp_path: Path,
) -> None:
    """A copy that fails partway never shortens an already-published file."""
    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "result.json").write_text('{"result": "full output"}')
    archive = wt_root / ".harvest-evidence" / "issue-42-impl-42"
    archive.mkdir(parents=True)
    # A complete archive written before the prepared marker existed.
    (archive / "result.json").write_text('{"result": "full output"}')

    with mock.patch("tick.shutil.copy2", side_effect=torn_copy_then_disk_full):
        assert archive_with_reuse(wt_root, wt_dir) == ""

    assert (archive / "result.json").read_text() == '{"result": "full output"}'
    assert not [path for path in archive.iterdir() if path.name.startswith(".")]


def test_archive_harvest_evidence_failed_snapshot_keeps_published_contents(
    tmp_path: Path,
) -> None:
    """A snapshot write that fails partway leaves the previous snapshot whole."""
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "result.json").write_text('{"result": "full output"}')
    archive = wt_root / ".harvest-evidence" / "issue-42-impl-42"
    archive.mkdir(parents=True)
    (archive / "git-status.txt").write_text("previous complete status\n")

    with mock.patch.object(tick, "git", return_value=" M changed.py"):
        with mock.patch.object(
            tick, "open", create=True, side_effect=disk_full_open("git-status.txt"),
        ):
            assert archive_with_reuse(wt_root, wt_dir) == ""

    assert (archive / "git-status.txt").read_text() == "previous complete status\n"
    assert not [path for path in archive.iterdir() if path.name.startswith(".")]


def test_existing_marker_blocks_dispatch_archives_before_cleanup(
    tmp_path: Path,
) -> None:
    """When a dead marker is cleaned for redispatch, evidence is archived first."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    marker = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 42,
        "deadline": 1000.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "abc",
        "pre_label": "ai:available",
        "dispatch_id": "impl-42-1000",
        "pid": 99999,
        "start_time": 1000.0,
        "pgid": 99999,
    }
    (wt_dir / "dispatch.json").write_text(json.dumps(marker))
    (wt_dir / "result.json").write_text('{"result": "test output"}')
    (wt_dir / "stderr.log").write_text("some stderr")

    with mock.patch("tick.process_control.get_process_start_time", return_value=None):
        with mock.patch("tick.process_control.pid_is_alive", return_value=False):
            blocked = _existing_marker_blocks_dispatch(str(wt_dir))

    assert blocked is False
    # The original dir should be cleaned up
    assert not wt_dir.exists()
    # Evidence should be archived
    archive_base = tmp_path / ".harvest-evidence"
    assert archive_base.is_dir()
    archive_dirs = list(archive_base.iterdir())
    assert len(archive_dirs) == 1
    archive_dir = archive_dirs[0]
    assert "impl-42-1000" in archive_dir.name
    assert (archive_dir / "dispatch.json").is_file()
    assert (archive_dir / "result.json").is_file()
    assert (archive_dir / "stderr.log").is_file()


def test_existing_marker_blocks_redispatch_while_process_group_alive(
    tmp_path: Path,
) -> None:
    """Pickup cannot erase custody while a dead wrapper's child group lives."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc",
        "head_sha": "def", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch(
        "tick.process_control.get_process_start_time", return_value=None,
    ):
        with mock.patch(
            "tick.process_control.pid_is_alive", return_value=False,
        ):
            with mock.patch(
                "tick.process_control.pgid_is_alive", return_value=True,
            ) as group_alive:
                blocked = _existing_marker_blocks_dispatch(
                    str(wt_dir),
                    entity="pr",
                    number=42,
                    pre_label="ai:implemented",
                    stage="review",
                )

    assert blocked is True
    group_alive.assert_called_once_with(99999)
    assert (wt_dir / "dispatch.json").is_file()
    assert not (tmp_path / ".harvest-evidence").exists()


def test_existing_marker_blocks_dispatch_retains_scratch_when_archive_creation_fails(
    tmp_path: Path,
) -> None:
    """Redispatch remains blocked when forensic archive creation fails."""
    import tick  # type: ignore[import-untyped]
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    marker = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 42,
        "deadline": 1000.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "abc",
        "pre_label": "ai:available",
        "dispatch_id": "impl-42-1000",
        "pid": 99999,
        "start_time": 1000.0,
        "pgid": 99999,
    }
    (wt_dir / "dispatch.json").write_text(json.dumps(marker))
    (wt_dir / "result.json").write_text('{"result": "test output"}')
    tick_log = tick.TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.process_control.get_process_start_time", return_value=None):
        with mock.patch("tick.process_control.pid_is_alive", return_value=False):
            with mock.patch("tick.os.makedirs", side_effect=OSError("disk full")):
                blocked = _existing_marker_blocks_dispatch(
                    str(wt_dir),
                    entity="issue",
                    number=42,
                    pre_label="ai:available",
                    stage="implement",
                    tick_log=tick_log,
                )

    assert blocked is True
    assert wt_dir.is_dir()
    assert (wt_dir / "result.json").is_file()
    failures = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "archive_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["dispatch_id"] == "impl-42-1000"


def test_existing_marker_blocks_dispatch_retains_scratch_when_archive_copy_fails(
    tmp_path: Path,
) -> None:
    """Redispatch remains blocked when any forensic evidence copy fails."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    marker = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 42,
        "deadline": 1000.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "abc",
        "pre_label": "ai:available",
        "dispatch_id": "impl-42-1000",
        "pid": 99999,
        "start_time": 1000.0,
        "pgid": 99999,
    }
    (wt_dir / "dispatch.json").write_text(json.dumps(marker))
    (wt_dir / "result.json").write_text('{"result": "test output"}')

    with mock.patch("tick.process_control.get_process_start_time", return_value=None):
        with mock.patch("tick.process_control.pid_is_alive", return_value=False):
            with mock.patch("tick.shutil.copy2", side_effect=OSError("read error")):
                blocked = _existing_marker_blocks_dispatch(str(wt_dir))

    assert blocked is True
    assert wt_dir.is_dir()
    assert (wt_dir / "result.json").is_file()


@pytest.mark.parametrize(
    ("stage", "entity", "pre_label"),
    [
        ("review", "pr", "ai:implemented"),
        ("implement", "issue", "ai:available"),
        ("fix", "pr", "ai:changes-requested"),
    ],
)
def test_dispatch_escalates_ambiguous_existing_marker_across_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    entity: str,
    pre_label: str,
) -> None:
    """An unreadable custody marker escalates instead of starving forever.

    Driven through each stage's real dispatch entry point and its real ``gh``
    seam, so the assertion covers the emitted argv and the order it is emitted
    in: the escalation is published *before* the label moves. An item that goes
    red here has no engine result and no evidence bundle, so the comment is the
    only account of it that leaves the operator's machine.
    """
    import tick  # type: ignore[import-untyped]

    monkeypatch.setattr(tick, "WT_ROOT", str(tmp_path))
    prefix = "issue" if entity == "issue" else "pr"
    wt_dir = tmp_path / f"{prefix}-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text("{")
    calls: list[str] = []
    bodies: list[str] = []

    def record_gh(*args: str) -> object:
        if args[1] == "comment":
            calls.append(f"comment:{args[0]}:{args[2]}")
            with open(args[4]) as fh:
                bodies.append(fh.read())
        elif args[1] == "edit":
            calls.append(f"edit:{args[0]}:{args[2]}:{args[6]}->{args[4]}")
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=0, stdout="",
        )

    with mock.patch("tick.gh", side_effect=record_gh):
        if stage == "review":
            tick._dispatch_one_reviewer(pr_number=42, pre_label=pre_label)
        elif stage == "implement":
            tick._dispatch_one_implementer(issue_number=42, issue_title="Issue")
        else:
            tick._dispatch_one_fixer(pr_number=42)

    assert calls == [
        f"comment:{entity}:42",
        f"edit:{entity}:42:{pre_label}->ai:needs-human-decision",
    ]
    assert f"`{stage}` dispatch" in bodies[0]
    assert "ambiguous-marker" in bodies[0]
    # The scratch this path could not prove safe to clear is still retained.
    assert wt_dir.is_dir()


def test_ambiguous_marker_failed_post_holds_the_label(
    tmp_path: Path,
) -> None:
    """The ambiguous-marker path gates its own relabel, not the setup path's.

    ``escalate_ambiguous`` returns ``True`` in both branches — it always blocks
    dispatch — so its gating is invisible from the return value and is wired
    separately from :func:`_escalate_dispatch_setup_failure`. Without a test
    driven through *this* function, a regression that published nothing would
    relabel the item silently and every other escalation test would still pass.
    """
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    marker_path = wt_dir / "dispatch.json"
    marker_path.write_text("{not json")
    calls: list[str] = []

    def record_gh(*args: str) -> object:
        if args[1] == "comment":
            raise subprocess.CalledProcessError(1, ["gh", *args])
        calls.append(f"edit:{args[4]}")
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=0, stdout="",
        )

    with mock.patch("tick.gh", side_effect=record_gh):
        blocked = _existing_marker_blocks_dispatch(
            str(wt_dir),
            entity="pr",
            number=42,
            pre_label="ai:changes-requested",
            stage="fix",
        )

    # Dispatch is blocked either way — the marker is unread, so custody is
    # retained regardless of what the remote label ends up saying.
    assert blocked is True
    assert calls == []
    assert marker_path.is_file()


def test_ambiguous_marker_post_failure_terminates_at_the_attempt_cap(
    tmp_path: Path,
) -> None:
    """A permanently unpostable ambiguous-marker escalation still terminates.

    Holding forever would leave the item at its pickup label looking like
    ordinary work on every future tick, which is the outcome the bound exists
    to prevent — and this path needs its own proof because it reaches the
    bound through its own call site.

    Unlike the setup-failure path, the counter is *not* cleared afterwards. The
    marker is still on disk — this path exists because it could not be proven
    safe to clear — so a harvest may be counting resultless failures into the
    same per-item file, and resetting it would delay that harvest's own
    cap-based cleanup of the scratch this path just retained.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _existing_marker_blocks_dispatch,
    )

    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text("{not json")
    calls: list[str] = []

    def record_gh(*args: str) -> object:
        if args[1] == "comment":
            raise subprocess.CalledProcessError(1, ["gh", *args])
        calls.append(f"edit:{args[4]}")
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=0, stdout="",
        )

    with mock.patch("tick.gh", side_effect=record_gh):
        for _ in range(pipeline.MAX_DISPATCH_ATTEMPTS + 1):
            _existing_marker_blocks_dispatch(
                str(wt_dir),
                entity="pr",
                number=42,
                pre_label="ai:changes-requested",
                stage="fix",
            )

    assert calls == ["edit:ai:needs-human-decision"]
    assert Attempts(tick.WT_ROOT, "").read(42) == pipeline.MAX_DISPATCH_ATTEMPTS


def test_existing_marker_blocks_dispatch_escalates_mismatched_identity(
    tmp_path: Path,
) -> None:
    """Confirmed PID reuse routes pickup state to operator-owned recovery."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "fix",
        "pr_number": 42,
        "deadline": 1000.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-42-old",
        "pid": 99999,
        "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch(
        "tick._classify_marker", return_value=("stale", "mismatched", True),
    ):
        with mock.patch(
            "tick.gh", side_effect=allow_escalation_comment(),
        ), mock.patch("tick._relabel_with_context") as relabel_mock:
            blocked = _existing_marker_blocks_dispatch(
                str(wt_dir),
                entity="pr",
                number=42,
                pre_label="ai:changes-requested",
                stage="fix",
            )

    assert blocked is True
    relabel_mock.assert_called_once()
    assert wt_dir.is_dir()


def test_existing_marker_blocks_dispatch_retains_a_sibling_stages_marker(
    tmp_path: Path,
) -> None:
    """A valid marker for another stage is custody, not corruption.

    One PR has one scratch directory, so a reviewer engine's marker is what a
    fix-stage dispatch finds at ``pr-42/dispatch.json``. Checked against
    ``expected_stage="fix"`` it fails ``valid_marker`` — and the escalation
    branch reads that failure as "corrupt", relabelling a PR under active
    review to ``ai:needs-human-decision``. Only a marker this stage could
    plausibly have written may be judged corrupt.
    """
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:implemented",
        "dispatch_id": "review-42-live",
        "pid": 99999,
        "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch("tick._relabel_with_context") as relabel_mock:
        blocked = _existing_marker_blocks_dispatch(
            str(wt_dir),
            entity="pr",
            number=42,
            pre_label="ai:changes-requested",
            stage="fix",
        )

    assert blocked is True
    relabel_mock.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_classify_marker_treats_empty_result_file_as_absent(
    tmp_path: Path,
) -> None:
    """A zero-byte result file must not count as a result.

    With stdout-redirect result capture, ``result.json`` exists (empty)
    from the moment of spawn. A crashed engine leaves it at zero bytes;
    treating it as wrapperless completion would turn every crash into
    an immediate escalation instead of a bounded stale-revert retry.
    """
    from tick import _classify_marker  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-99"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text("")

    marker: dict[str, object] = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:available",
        "dispatch_id": "d-99",
        "start_time": 1234567.0,
        # pid intentionally missing — dead engine, no provable identity
    }

    state, _identity, _pid_alive = _classify_marker(
        str(wt_dir), marker, result_file="result.json",
    )

    assert state == "stale", f"expected stale, got {state}"
    assert not (wt_dir / "completion.json").exists(), (
        "no envelope may be synthesized over an empty result file"
    )


# --- Round 2: R2-F1 — kill failure gating ---


def test_execute_reconcile_actions_gates_cleanup_on_identity(tmp_path: Path) -> None:
    """cleanup/kill_cleanup actions must skip cleanup on mismatched identity."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()

    decisions = [
        {
            "action": "kill_cleanup",
            "label": "",
            "kill": False,
            "identity": "mismatched",
            "pr_number": 42,
        },
    ]
    with mock.patch("tick.cleanup_scratch") as cleanup_mock:
        _execute_reconcile_actions(decisions, config, wt_root=str(tmp_path))
        # Mismatched identity must NOT trigger cleanup
        cleanup_mock.assert_not_called()


def test_execute_reconcile_actions_cleanup_on_matched_or_unknown(tmp_path: Path) -> None:
    """cleanup actions must proceed on matched or unknown identity."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _execute_reconcile_actions  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()

    for identity in ("matched", "unknown"):
        decisions = [
            {
                "action": "cleanup",
                "label": "",
                "kill": False,
                "identity": identity,
                "pr_number": 42,
            },
        ]
        with mock.patch("tick.cleanup_scratch") as cleanup_mock:
            _execute_reconcile_actions(decisions, config, wt_root=str(tmp_path))
            cleanup_mock.assert_called_once()


# --- Round 2: R2-F5 — marker scan validation ---


def test_scan_marker_identities_rejects_non_finite_deadline(tmp_path: Path) -> None:
    """The admission scan fails closed on a non-finite marker deadline."""
    from tick import _scan_marker_identities  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    pr_dir = wt_root / "pr-1"
    pr_dir.mkdir()
    (pr_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 1,
        "deadline": float("inf"),
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-1",
        "pid": 99999,
        "start_time": 999999.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("process_control.get_process_start_time") as gps:
            gps.return_value = 999999.0
            # valid_marker rejects non-finite deadlines before classification.
            with pytest.raises(RuntimeError, match="corrupt marker"):
                _scan_marker_identities()


# --- HARVEST side effects (Step 9 — full implementations) ---


@pytest.mark.parametrize("base_field", [
    {}, {"base_ref": ""}, {"base_ref": "  "}, {"base_ref": None},
])
def test_implementer_harvest_requires_pinned_base_before_creating_pr(
    base_field: dict[str, object], tmp_path: Path,
) -> None:
    """Recovery must retain evidence rather than guess a completed job's base."""
    import tick

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text('{"result": "IMPLEMENTATION_COMPLETE"}')
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "impl-42-1", **base_field,
    }))
    log = mock.Mock()
    with (
        mock.patch("tick.gh_json", return_value=[]),
        mock.patch(
            "tick.gh_text", return_value="https://github.com/test/repo/pull/99",
        ) as create,
        mock.patch("tick.relabel") as relabel_mock,
        mock.patch("tick._converge_pipeline_labels", return_value=True) as converge,
        mock.patch("tick._bound_failed_harvest") as failed,
    ):
        tick._harvest_one_implementer({"pr_number": 42}, str(tmp_path), tick_log=log)

    create.assert_not_called()
    relabel_mock.assert_not_called()
    converge.assert_not_called()
    failed.assert_called_once()
    assert (wt_dir / "dispatch.json").exists()
    assert (wt_dir / "result.json").exists()
    assert any(
        "base_ref" in call.kwargs.get("reason", "")
        for call in log.log.call_args_list
    )


@pytest.mark.parametrize("base_ref", ["main", "master", "release/stable"])
def test_harvest_one_implementer_creates_pr_on_create_pr_action(
    tmp_path: Path,
    base_ref: str,
) -> None:
    """_harvest_one_implementer creates the PR, relabels, and clears custody."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "implement",
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": base_ref,
        "base_sha": "abc123",
        "head_sha": "",
        "pre_label": "ai:available",
        "dispatch_id": "d-42",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 42,
    }

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr",
            "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            with mock.patch("tick.gh_text") as gh_text_mock:
                # No existing PR for the head branch; then create.
                gh_text_mock.side_effect = [
                    "https://github.com/test-owner/test-repo/pull/99\n",
                ]
                with mock.patch("tick.relabel") as relabel_mock:
                    with mock.patch(
                        "tick._converge_pipeline_labels", return_value=True,
                    ) as converge_mock:
                        _harvest_one_implementer(
                            decision, str(wt_root),
                            tick_log=None, attempts=None,
                        )

    # Verify pipeline.harvest_implementation was called with result content
    harvest_impl.assert_called_once_with(
        verification_unconfigured=False,
        result='{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
        pre_label="ai:available",
    )
    # PR creation uses the recorded dispatch base, without rediscovering it.
    assert gh_text_mock.call_count == 1
    pr_create_args = gh_text_mock.call_args[0]
    assert "pr" in pr_create_args
    assert "create" in pr_create_args
    assert "--head" in pr_create_args
    assert "agent/issue-42" in pr_create_args
    assert pr_create_args[pr_create_args.index("--base") + 1] == base_ref
    assert pr_create_args[pr_create_args.index("--title") + 1] == (
        "feat: implement issue #42"
    )
    fallback_body = pr_create_args[pr_create_args.index("--body") + 1]
    assert "Closes #42" in fallback_body
    # Relabeled to ai:implemented
    relabel_mock.assert_called_once_with("pr", 99, "", "ai:implemented")
    # The issue converges to NO pipeline label — removing only
    # ai:implementing would leave a dual-label ai:available behind and
    # re-dispatch a second implementer over the created PR.
    converge_mock.assert_called_once_with(
        "issue", 42, "",
        tick_log=None, phase="harvest", stage="implement",
    )
    # Implementer custody is cleared; reviewer dispatch owns a fresh PR path.
    assert not os.path.exists(str(wt_dir))
    new_wt = wt_root / "pr-99"
    assert not new_wt.exists()


@pytest.mark.parametrize("base_ref", ["master", ""])
def test_harvest_one_implementer_resumes_handoff_for_existing_pr(
    tmp_path: Path,
    base_ref: str,
) -> None:
    """A retried handoff whose PR already exists must not run `gh pr create`
    again — it rediscovers the open PR and retries only the label steps."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": base_ref, "base_sha": "abc123",
        "head_sha": "", "pre_label": "ai:available", "dispatch_id": "d-42",
        "pid": 12345, "start_time": 9999999999.0, "pgid": 12345,
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 42,
    }

    def gh_text_side_effect(*args: str, **_kwargs: object) -> str:
        raise AssertionError(f"unexpected gh_text call: {args}")

    gh_json_reads: list[object] = [
        [{"number": 99, "isCrossRepository": False}],
        {"labels": []},
    ]

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr", "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_text", side_effect=gh_text_side_effect):
            with mock.patch("tick.gh_json", side_effect=gh_json_reads):
                with mock.patch("tick.relabel") as relabel_mock:
                    with mock.patch(
                        "tick._converge_pipeline_labels", return_value=True,
                    ) as converge_mock:
                        _harvest_one_implementer(
                            decision, str(wt_root), tick_log=None, attempts=None,
                        )

    relabel_mock.assert_called_once_with("pr", 99, "", "ai:implemented")
    converge_mock.assert_called_once_with(
        "issue", 42, "",
        tick_log=None, phase="harvest", stage="implement",
    )
    assert not wt_dir.exists()


def test_harvest_one_implementer_resume_preserves_progressed_pr_state(
    tmp_path: Path,
) -> None:
    """A resumed handoff whose PR already carries a pipeline label must not
    re-add ai:implemented — the PR may have progressed through review while
    the retry was pending, and relabeling would corrupt its state."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc123",
        "head_sha": "", "pre_label": "ai:available", "dispatch_id": "d-42",
        "pid": 12345, "start_time": 9999999999.0, "pgid": 12345,
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 42,
    }

    def gh_text_side_effect(*args: str, **_kwargs: object) -> str:
        raise AssertionError(f"unexpected gh_text call: {args}")

    gh_json_reads: list[object] = [
        [{"number": 99, "isCrossRepository": False}],
        {"labels": [{"name": "ai:changes-requested"}]},
    ]

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr", "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_text", side_effect=gh_text_side_effect):
            with mock.patch("tick.gh_json", side_effect=gh_json_reads):
                with mock.patch("tick.relabel") as relabel_mock:
                    with mock.patch(
                        "tick._converge_pipeline_labels", return_value=True,
                    ) as converge_mock:
                        _harvest_one_implementer(
                            decision, str(wt_root), tick_log=None, attempts=None,
                        )

    relabel_mock.assert_not_called()
    converge_mock.assert_called_once_with(
        "issue", 42, "",
        tick_log=None, phase="harvest", stage="implement",
    )
    assert not wt_dir.exists()


def test_harvest_one_implementer_resume_ignores_cross_repository_pr(
    tmp_path: Path,
) -> None:
    """A fork PR reusing the agent branch name must not be adopted as the
    resumed handoff — only a same-repository head proves pipeline identity."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc123",
        "head_sha": "", "pre_label": "ai:available", "dispatch_id": "d-42",
        "pid": 12345, "start_time": 9999999999.0, "pgid": 12345,
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 42,
    }

    fork_only = [{"number": 666, "isCrossRepository": True}]
    created: list[tuple[str, ...]] = []

    def gh_json_side_effect(*args: str, **_kwargs: object) -> object:
        if args[:2] == ("pr", "list"):
            return fork_only
        raise AssertionError(f"unexpected gh_json call: {args}")

    def gh_text_side_effect(*args: str, **_kwargs: object) -> str:
        if args[:2] == ("pr", "create"):
            created.append(args)
            return "https://github.com/test-owner/test-repo/pull/99"
        raise AssertionError(f"unexpected gh_text call: {args}")

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr", "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_side_effect):
            with mock.patch("tick.gh_text", side_effect=gh_text_side_effect):
                with mock.patch(
                    "tick._implementation_pr_create_args", return_value=[],
                ):
                    with mock.patch("tick.relabel") as relabel_mock:
                        with mock.patch(
                            "tick._converge_pipeline_labels",
                            return_value=True,
                        ):
                            _harvest_one_implementer(
                                decision, str(wt_root),
                                tick_log=None, attempts=None,
                            )

    # The fork PR is ignored: our own PR is created and labeled, and the
    # fork PR (#666) is never touched.
    assert len(created) == 1
    relabel_mock.assert_called_once_with("pr", 99, "", "ai:implemented")


def test_harvest_one_implementer_resume_retries_on_malformed_label_read(
    tmp_path: Path,
) -> None:
    """A zero-exit malformed labels payload for the resumed PR must retain
    the marker for retry, not raise out of the harvest and abort the tick."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc123",
        "head_sha": "", "pre_label": "ai:available", "dispatch_id": "d-42",
        "pid": 12345, "start_time": 9999999999.0, "pgid": 12345,
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 42,
    }

    gh_json_reads: list[object] = [
        [{"number": 99, "isCrossRepository": False}],
        json.JSONDecodeError("truncated", "{", 1),
    ]

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr", "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_reads):
            with mock.patch("tick.relabel") as relabel_mock:
                with mock.patch(
                    "tick._converge_pipeline_labels",
                ) as converge_mock:
                    _harvest_one_implementer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )

    relabel_mock.assert_not_called()
    converge_mock.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_harvest_one_implementer_fails_closed_when_pr_lookup_fails(
    tmp_path: Path,
) -> None:
    """An unreadable existing-PR lookup must not fall through to
    `gh pr create` — the PR may already exist, and the doomed create would
    burn bounded harvest attempts toward escalating a successful run."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc123",
        "head_sha": "", "pre_label": "ai:available", "dispatch_id": "d-42",
        "pid": 12345, "start_time": 9999999999.0, "pgid": 12345,
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 42,
    }

    def gh_json_side_effect(*args: str, **_kwargs: object) -> object:
        if args[:2] == ("pr", "list"):
            raise subprocess.CalledProcessError(1, ["gh", "pr", "list"])
        raise AssertionError(f"unexpected gh_json call: {args}")

    def gh_text_side_effect(*args: str, **_kwargs: object) -> str:
        raise AssertionError(f"unexpected gh_text call: {args}")

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr", "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_side_effect):
            with mock.patch("tick.gh_text", side_effect=gh_text_side_effect):
                with mock.patch("tick.relabel") as relabel_mock:
                    with mock.patch(
                        "tick._converge_pipeline_labels",
                    ) as converge_mock:
                        _harvest_one_implementer(
                            decision, str(wt_root), tick_log=None, attempts=None,
                        )

    relabel_mock.assert_not_called()
    converge_mock.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_harvest_one_implementer_logs_failed_label_removal(
    tmp_path: Path,
) -> None:
    """A failed issue-label convergence after PR creation retains custody.

    Clearing the marker while the issue may still carry ai:available would
    let the same tick's dispatch launch a second implementer over an issue
    whose PR already exists — the failure must be logged, the marker and
    attempt state retained, and the bounded-harvest path left to retry.
    """
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_one_implementer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "implement",
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc123",
        "head_sha": "",
        "pre_label": "ai:available",
        "dispatch_id": "d-42",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 42,
    }

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)
    attempts = Attempts(str(wt_root), "issue-")
    attempts.incr(42)

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr",
            "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.return_value = (
                    "https://github.com/test-owner/test-repo/pull/99"
                )
                with mock.patch("tick.relabel"):
                    with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                        with mock.patch(
                            "tick._converge_pipeline_labels", return_value=False,
                        ):
                            _harvest_one_implementer(
                                decision, str(wt_root),
                                tick_log=tick_log, attempts=attempts,
                            )

    entries = tick_log.tail()
    label_removal_failures = [
        e for e in entries
        if e.get("action") == "side_effect_failed"
        and "converge" in str(e.get("reason", ""))
    ]
    assert len(label_removal_failures) == 1
    # Custody retained: the marker must survive so the same tick's dispatch
    # dedup blocks a duplicate implementer, and the durable dispatch-attempt
    # counter must not be reset by a transition that never landed.
    cleanup_mock.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()
    assert attempts.read(42) >= 1


def test_harvest_one_implementer_uses_valid_pr_body_file(
    tmp_path: Path,
) -> None:
    """_harvest_one_implementer uses engine-authored PR metadata when valid."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "implement",
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc123",
        "head_sha": "",
        "pre_label": "ai:available",
        "dispatch_id": "d-42",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))
    (wt_dir / ".pr-body.md").write_text(
        "Implement PWA icon pipeline\n\n"
        "## Summary\n"
        "Closes #42\n\n"
        "- Problem: The PWA icon pipeline is missing.\n"
        "- Add the generated icon workflow.\n"
        "## Why this approach\n"
        "- How this solves the issue: generate the required icons.\n"
        "- Keeps the PR pipeline metadata near the implementation.\n\n"
        "## What changed\n"
        "- Structure: the build invokes the icon generator.\n"
        "- Updates the icon generator.\n"
        "- Impact: installable builds contain the required icons.\n\n"
        "## Test coverage\n"
        "- Covers generated asset metadata.\n\n"
        "## Test plan\n"
        "- [x] `uv run pytest`\n",
    )

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 42,
    }

    captured: dict[str, object] = {}

    def fake_gh_text(*args: str) -> str:
        captured["args"] = args
        if "--body-file" in args:
            body_file = args[args.index("--body-file") + 1]
            captured["body"] = Path(body_file).read_text()
        return "https://github.com/test-owner/test-repo/pull/99\n"

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr",
            "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            with mock.patch("tick.gh_text", side_effect=fake_gh_text):
                with mock.patch("tick.relabel"):
                    with mock.patch("tick.cleanup_scratch"):
                        with mock.patch(
                            "tick._converge_pipeline_labels", return_value=True,
                        ):
                            _harvest_one_implementer(
                                decision, str(wt_root),
                                tick_log=None, attempts=None,
                            )

    pr_create_args = captured["args"]
    assert isinstance(pr_create_args, tuple)
    assert pr_create_args[pr_create_args.index("--title") + 1] == (
        "Implement PWA icon pipeline"
    )
    assert "--body-file" in pr_create_args
    assert "--body" not in pr_create_args
    body = captured["body"]
    assert isinstance(body, str)
    for section in (
        "## Summary",
        "## Why this approach",
        "## What changed",
        "## Test coverage",
        "## Test plan",
    ):
        assert section in body
    assert "Closes #42" in body


def test_harvest_one_implementer_falls_back_when_pr_body_file_is_malformed(
    tmp_path: Path,
) -> None:
    """Malformed engine-authored PR metadata must not block PR creation."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-11"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE"}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({"base_ref": "main"}))
    (wt_dir / ".pr-body.md").write_text(
        "Incomplete generated title\n\n## Summary\n- Missing required sections.\n",
    )

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 11,
    }

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr",
            "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.return_value = (
                    "https://github.com/test-owner/test-repo/pull/101\n"
                )
                with mock.patch("tick.relabel"):
                    with mock.patch("tick.cleanup_scratch"):
                        with mock.patch(
                            "tick._converge_pipeline_labels", return_value=True,
                        ):
                            _harvest_one_implementer(
                                decision, str(wt_root),
                                tick_log=None, attempts=None,
                            )

    pr_create_args = gh_text_mock.call_args[0]
    assert "--body-file" not in pr_create_args
    assert pr_create_args[pr_create_args.index("--title") + 1] == (
        "feat: implement issue #11"
    )
    fallback_body = pr_create_args[pr_create_args.index("--body") + 1]
    assert "Closes #11" in fallback_body


def test_read_engine_pr_metadata_requires_guideline_body_structure(
    tmp_path: Path,
) -> None:
    """Engine metadata must satisfy the canonical PR-body content contract."""
    from tick import _read_engine_pr_metadata  # type: ignore[import-untyped]

    wt_dir = tmp_path / "issue-42"
    wt_dir.mkdir()
    body = (
        "Implement structured PR metadata\n\n"
        "## Summary\n"
        "- Problem: The owner cannot orient from generated PR bodies.\n"
        "Closes #42\n\n"
        "## Why this approach\n"
        "- How this solves the issue: require the canonical structure.\n\n"
        "## What changed\n"
        "- Structure: the prompt feeds metadata into harvest validation.\n"
        "- Impact: malformed bodies fall back to a valid generated body.\n\n"
        "## Test coverage\n"
        "- Covers generated metadata.\n\n"
        "## Test plan\n"
        "- [x] `uv run pytest`\n"
    )
    (wt_dir / ".pr-body.md").write_text(body)

    assert _read_engine_pr_metadata(str(wt_dir), 42) is None


def test_fallback_implementation_pr_metadata_satisfies_guideline_structure() -> None:
    """Controller fallback bodies must remain valid under their own contract."""
    from tick import (  # type: ignore[import-untyped]
        _fallback_implementation_pr_metadata,
        _pr_body_matches_guidelines,
    )

    _, body = _fallback_implementation_pr_metadata(42)

    assert _pr_body_matches_guidelines(body, 42)


def test_pr_body_guideline_markers_have_strict_positions() -> None:
    """Problem and Impact markers must orient and close their sections."""
    from tick import _pr_body_matches_guidelines  # type: ignore[import-untyped]

    valid_body = (
        "## Summary\nCloses #42\n- Problem: Missing behavior.\n"
        "- Implements behavior.\n\n"
        "## Why this approach\n- How this solves the issue: add behavior.\n\n"
        "## What changed\n- Structure: route to service.\n"
        "- Updates the service.\n- Impact: behavior is available.\n\n"
        "## Test coverage\n- Covers behavior.\n\n"
        "## Test plan\n- [x] `uv run pytest`\n"
    )

    problem_not_second = valid_body.replace(
        "- Problem: Missing behavior.\n- Implements behavior.",
        "- Implements behavior.\n- Problem: Missing behavior.",
    )
    impact_not_last = valid_body.replace(
        "- Updates the service.\n- Impact: behavior is available.",
        "- Impact: behavior is available.\n- Updates the service.",
    )

    assert not _pr_body_matches_guidelines(problem_not_second, 42)
    assert not _pr_body_matches_guidelines(impact_not_last, 42)


def test_harvest_one_implementer_clears_custody_before_reviewer_dispatch(
    tmp_path: Path,
) -> None:
    """Successful implementation leaves no implement marker in the PR path.

    Reviewer dispatch owns ``pr-PR_NUMBER`` and validates markers against the
    review stage and PR number. Carrying the completed implementer directory into
    that path turns a successful handoff into ambiguous custody and escalates the
    new PR instead of reviewing it.
    """
    from tick import (  # type: ignore[import-untyped]
        _existing_marker_blocks_dispatch,
        _harvest_one_implementer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-42"
    wt_dir.mkdir()
    # Simulate the .git pointer written by git worktree add
    (wt_dir / ".git").write_text("gitdir: /repo/.git/worktrees/issue-42\n")
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "implement", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc123",
        "head_sha": "", "pre_label": "ai:available", "dispatch_id": "d-42",
        "pid": 12345, "start_time": 9999999999.0, "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 42,
    }

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr", "label": "", "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            with mock.patch("tick.gh_text",
                            return_value=(
                                "https://github.com/test-owner/test-repo/pull/99"
                            )):
                with mock.patch("tick.relabel"):
                    with mock.patch(
                        "tick._converge_pipeline_labels", return_value=True,
                    ):
                        _harvest_one_implementer(
                            decision, str(wt_root), tick_log=None, attempts=None,
                        )

    new_wt = wt_root / "pr-99"
    assert not wt_dir.exists(), "completed implementer scratch must be cleared"
    assert not new_wt.exists(), "reviewer scratch path must start unowned"
    with mock.patch("tick._relabel_with_context") as relabel_mock:
        blocked = _existing_marker_blocks_dispatch(
            str(new_wt),
            entity="pr",
            number=99,
            pre_label="ai:implemented",
            stage="review",
        )
    assert blocked is False
    relabel_mock.assert_not_called()


def test_harvest_one_implementer_escalates_on_escalate_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_harvest_one_implementer relabels issue and cleans up on escalation."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    monkeypatch.setattr("tick.gh", allow_escalation_comment())
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "NEEDS_HUMAN_DECISION", "exit_status": 0}',
    )
    # A harvest decision is only reachable behind a valid marker, whose
    # dispatch id the terminal-intent record binds.
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "impl-7-1"}))

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 7,
    }

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "escalate",
            "label": "ai:needs-human-decision",
            "sentinel": "NEEDS_HUMAN_DECISION",
        }
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ) as converge_mock:
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_implementer(
                    decision, str(wt_root),
                    tick_log=None, attempts=None,
                )

    converge_mock.assert_called_once_with(
        "issue", 7, "ai:needs-human-decision",
        tick_log=None, phase="harvest", stage="implement",
    )
    cleanup_mock.assert_called_once_with(str(wt_dir))


@pytest.mark.parametrize("tuning, cooldown", [
    ({}, 3600),
    ({"transient_cooldown_seconds": 900,
      "transient_cooldown_reset_buffer_seconds": 0}, 900),
])
def test_harvest_one_implementer_requeues_transient_result(
    tmp_path: Path, tuning: dict[str, int], cooldown: int,
) -> None:
    """Transient implementer results requeue behind a durable cooldown."""
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        EngineCooldowns,
        TickLog,
        _harvest_one_implementer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-quota",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    attempts = Attempts(str(wt_root), "issue-")
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.time.time", return_value=1_800_000_000.0):
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ) as converge_mock:
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_implementer(
                    decision, str(wt_root), tick_log=tick_log, attempts=attempts,
                    **tuning,
                )

    converge_mock.assert_called_once_with(
        "issue", 7, "ai:available",
        tick_log=tick_log, phase="harvest", stage="implement",
    )
    assert attempts.read(7) == 1
    assert EngineCooldowns(str(wt_root)).read_active(
        "claude", now=1_800_000_000.0,
    ) == 1_800_000_000.0 + cooldown
    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    retry = next(entry for entry in entries if entry["action"] == "transient_revert")
    assert retry["details"]["attempt"] == 1
    assert retry["details"]["failure_class"] == "quota_or_limit"
    cleanup_mock.assert_called_once_with(str(wt_dir))


def test_harvest_one_implementer_retains_attempt_on_relabel_failure(
    tmp_path: Path,
) -> None:
    """The retry counter advances only after the GitHub revert succeeds."""
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        EngineCooldowns,
        _harvest_one_implementer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-quota",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    attempts = Attempts(str(wt_root), "issue-")

    with mock.patch("tick.time.time", return_value=1_800_000_000.0):
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=False,
        ):
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_implementer(
                    decision, str(wt_root), tick_log=None, attempts=attempts,
                )

    assert attempts.read(7) == 0
    assert EngineCooldowns(str(wt_root)).read_active(
        "claude", now=1_800_000_000.0,
    ) == 1_800_003_600.0
    cleanup_mock.assert_not_called()
    assert (wt_dir / "result.json").is_file()


def test_harvest_one_implementer_does_not_relabel_before_cooldown_persists(
    tmp_path: Path,
) -> None:
    """A cooldown write failure retains in-flight custody and source evidence."""
    from tick import Attempts, _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-quota",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    attempts = Attempts(str(wt_root), "issue-")

    with mock.patch("tick.EngineCooldowns.start", side_effect=OSError("disk full")):
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ) as converge_mock:
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_implementer(
                    decision, str(wt_root), tick_log=None, attempts=attempts,
                )

    converge_mock.assert_not_called()
    assert attempts.read(7) == 0
    cleanup_mock.assert_not_called()
    assert (wt_dir / "result.json").is_file()


def test_harvest_one_implementer_fails_closed_on_corrupt_cooldown_state(
    tmp_path: Path,
) -> None:
    """Harvest cannot overwrite corrupt durable cooldown ownership state."""
    from tick import Attempts, _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    cooldown_dir = wt_root / ".engine-cooldowns"
    cooldown_dir.mkdir()
    (cooldown_dir / "claude.json").write_text("not-json")
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-quota",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    attempts = Attempts(str(wt_root), "issue-")

    with mock.patch("tick.relabel") as relabel_mock:
        with mock.patch("tick.cleanup_scratch") as cleanup_mock:
            _harvest_one_implementer(
                decision, str(wt_root), tick_log=None, attempts=attempts,
            )

    relabel_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    assert attempts.read(7) == 0
    assert (cooldown_dir / "claude.json").read_text() == "not-json"
    assert (wt_dir / "result.json").is_file()


def test_harvest_one_implementer_escalates_when_attempt_persistence_fails(
    tmp_path: Path,
) -> None:
    """A failed counter write cannot leave an unbounded requeue."""
    from tick import Attempts, TickLog, _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-quota",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    attempts = Attempts(str(wt_root), "issue-")
    tick_log = TickLog.open(str(tmp_path / "logs"))

    bodies: list[str] = []

    with mock.patch.object(attempts, "incr", side_effect=OSError("disk full")):
        with mock.patch(
            "tick.gh", side_effect=capture_comment_bodies(bodies, "issue"),
        ):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_implementer(
                        decision, str(wt_root),
                        tick_log=tick_log, attempts=attempts,
                    )

    # The terminal transition is published, not just logged locally.
    assert len(bodies) == 1
    assert "attempt counter" in bodies[0]
    assert "ai:needs-human-decision" in bodies[0]
    assert converge_mock.call_args_list == [
        mock.call(
            "issue", 7, "ai:available",
            tick_log=tick_log, phase="harvest", stage="implement",
        ),
        mock.call(
            "issue", 7, "ai:needs-human-decision",
            tick_log=tick_log, phase="harvest", stage="implement",
        ),
    ]
    cleanup_mock.assert_not_called()
    assert (wt_dir / "result.json").is_file()
    assert any(
        entry["action"] == "attempt_state_error" for entry in tick_log.tail()
    )


def test_harvest_one_implementer_escalates_third_transient_result(
    tmp_path: Path,
) -> None:
    """The third transient implementer result exhausts the retry bound."""
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_one_implementer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-quota-3",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    attempts = Attempts(str(wt_root), "issue-")
    attempts.incr(7)
    attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    bodies: list[str] = []

    def assert_complete_archive_before_relabel(*_args: object, **_kwargs: object) -> bool:
        archives = list((wt_root / ".harvest-evidence").glob("issue-7-*"))
        assert len(archives) == 1
        assert (archives[0] / "result.json").is_file()
        assert wt_dir.is_dir()
        # The terminal label never moves before its explanation is published.
        assert bodies
        return True

    with mock.patch("tick.time.time", return_value=1_800_000_000.0):
        with mock.patch(
            "tick.gh", side_effect=capture_comment_bodies(bodies, "issue"),
        ):
            with mock.patch(
                "tick._converge_pipeline_labels",
                side_effect=assert_complete_archive_before_relabel,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_implementer(
                        decision, str(wt_root),
                        tick_log=tick_log, attempts=attempts,
                    )

    converge_mock.assert_called_once_with(
        "issue", 7, "ai:needs-human-decision",
        tick_log=tick_log, phase="harvest", stage="implement",
    )
    assert "transient-exhausted" in bodies[0]
    assert "quota_or_limit" in bodies[0]
    assert attempts.read(7) == 0
    archives = list((wt_root / ".harvest-evidence").glob("issue-7-*"))
    assert len(archives) == 1
    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    exhausted = next(
        entry for entry in entries if entry["action"] == "transient_exhausted"
    )
    assert exhausted["details"]["attempt"] == 3
    cleanup_mock.assert_called_once_with(str(wt_dir))


def test_harvest_one_implementer_archives_before_transient_exhaustion_label(
    tmp_path: Path,
) -> None:
    """The third transient keeps in-flight custody when archival fails."""
    from tick import Attempts, _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-quota-3",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    attempts = Attempts(str(wt_root), "issue-")
    attempts.incr(7)
    attempts.incr(7)

    with mock.patch("tick.shutil.copy2", side_effect=OSError("disk full")):
        with mock.patch("tick.relabel") as relabel_mock:
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_implementer(
                    decision, str(wt_root), tick_log=None, attempts=attempts,
                )

    relabel_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    assert attempts.read(7) == 2
    assert (wt_dir / "result.json").is_file()


def test_harvest_one_implementer_logs_skills_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harvest summary reports skills recorded in skill-usage.log."""
    from tick import TickLog, _harvest_one_implementer  # type: ignore[import-untyped]

    monkeypatch.setattr("tick.gh", allow_escalation_comment())
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(json.dumps({
        "result": "done",
        "skills_used": ["docs-contract-review", "integration-test-authenticity"],
        "skill_usage_status": "active",
    }))
    dispatch_id = "impl-7-test"
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": dispatch_id}))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "escalate",
            "label": "ai:needs-human-decision",
            "sentinel": None,
        }
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ):
            _harvest_one_implementer(
                decision, str(wt_root), tick_log=tick_log, attempts=None,
            )

    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    summary = next(entry for entry in entries if entry["action"] == "engine_result_summary")
    assert summary["details"]["skills_used"] == [
        "docs-contract-review",
        "integration-test-authenticity",
    ]
    assert summary["details"]["skill_usage_status"] == "active"


def test_harvest_one_implementer_logs_empty_skills_when_log_missing(
    tmp_path: Path,
) -> None:
    """Missing log AND missing health marker report status unavailable.

    Without the marker, an empty skills_used list must be attributable to
    broken instrumentation rather than read as "no skill invoked".
    """
    from tick import TickLog, _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(json.dumps({"result": "done"}))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "escalate",
            "label": "ai:needs-human-decision",
            "sentinel": None,
        }
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ):
            _harvest_one_implementer(
                decision, str(wt_root), tick_log=tick_log, attempts=None,
            )

    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    summary = next(entry for entry in entries if entry["action"] == "engine_result_summary")
    assert summary["details"]["skills_used"] == []
    assert summary["details"]["skill_usage_status"] == "unavailable"


def test_harvest_one_implementer_archives_evidence_on_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escalated implementer harvests preserve result evidence before cleanup."""
    from tick import TickLog, _harvest_one_implementer  # type: ignore[import-untyped]

    monkeypatch.setattr("tick.gh", allow_escalation_comment())
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    result = {
        "result": "You've hit your session limit",
        "is_error": True,
        "api_error_status": 429,
        "stop_reason": "stop_sequence",
        "permission_denials": [
            {
                "tool_name": "Write",
                "tool_input": {"content": "do not log this raw tool payload"},
            },
        ],
    }
    (wt_dir / "result.json").write_text(json.dumps(result))
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "implement",
        "pr_number": 7,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "abc",
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-test",
    }))
    (wt_dir / "stderr.log").write_text("stderr details\n")
    (wt_dir / "prompt.txt").write_text("prompt details\n")

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    tick_log = TickLog.open(str(tmp_path / "logs"))

    def assert_complete_archive_before_relabel(*_args: object, **_kwargs: object) -> bool:
        archives = list((wt_root / ".harvest-evidence").glob("issue-7-*"))
        assert len(archives) == 1
        assert (archives[0] / "result.json").is_file()
        assert wt_dir.is_dir()
        return True

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "escalate",
            "label": "ai:needs-human-decision",
            "sentinel": None,
        }
        with mock.patch(
            "tick._converge_pipeline_labels",
            side_effect=assert_complete_archive_before_relabel,
        ):
            _harvest_one_implementer(
                decision, str(wt_root), tick_log=tick_log, attempts=None,
            )

    assert not wt_dir.exists(), "scratch worktree should still be cleaned up"
    archives = list((wt_root / ".harvest-evidence").glob("issue-7-*"))
    assert len(archives) == 1
    archive = archives[0]
    assert (archive / "result.json").read_text() == json.dumps(result)
    assert (archive / "dispatch.json").is_file()
    assert (archive / "stderr.log").read_text() == "stderr details\n"
    assert (archive / "prompt.txt").read_text() == "prompt details\n"

    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    summary = next(entry for entry in entries if entry["action"] == "engine_result_summary")
    assert summary["dispatch_id"] == "impl-7-test"
    assert summary["details"]["harvest_action"] == "escalate"
    assert summary["details"]["sentinel"] is None
    assert summary["details"]["is_error"] is True
    assert summary["details"]["api_error_status"] == 429
    assert summary["details"]["permission_denial_count"] == 1
    assert summary["details"]["permission_denial_tools"] == ["Write"]
    assert "session limit" in summary["details"]["result_excerpt"]
    assert "do not log this raw tool payload" not in json.dumps(summary)

    evidence = next(entry for entry in entries if entry["action"] == "harvest_evidence")
    assert evidence["details"]["archive_dir"] == str(archive)


def test_harvest_one_implementer_retains_scratch_when_evidence_copy_fails(
    tmp_path: Path,
) -> None:
    """Escalation must not destroy the only result when archival fails."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "Blocked. NEEDS_HUMAN_DECISION"}),
    )
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    with mock.patch("tick.shutil.copy2", side_effect=OSError("disk full")):
        with mock.patch("tick.relabel") as relabel_mock:
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_implementer(
                    decision, str(wt_root), tick_log=None, attempts=None,
                )

    relabel_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    assert (wt_dir / "result.json").is_file()


def test_harvest_one_implementer_retains_label_when_archive_move_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed full-tree move retains scratch custody for the next tick.

    The terminal label has already converged (evidence copy → converge →
    finalize), so the failed finalize must leave the original scratch and its
    marker in place rather than deleting anything.
    """
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    monkeypatch.setattr("tick.gh", allow_escalation_comment())
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "Blocked. NEEDS_HUMAN_DECISION"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "impl-7-1"}))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    with mock.patch("tick.os.rename", side_effect=OSError("cross-device move")):
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ) as converge_mock:
            _harvest_one_implementer(
                decision, str(wt_root), tick_log=None, attempts=None,
            )

    converge_mock.assert_called_once()
    assert (wt_dir / "result.json").is_file()
    archives = list((wt_root / ".harvest-evidence").glob("issue-7-*"))
    assert len(archives) == 1
    assert not (archives[0] / "scratch").exists()


def test_harvest_one_implementer_skips_cleanup_on_gh_failure(
    tmp_path: Path,
) -> None:
    """_harvest_one_implementer skips cleanup when gh pr create fails."""
    from tick import TickLog, _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-3"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE"}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({"base_ref": "main"}))

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 3,
    }
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr",
            "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        def gh_text_side_effect(*args: str, **_kwargs: object) -> str:
            raise subprocess.CalledProcessError(
                1, ["gh"], stderr="head branch does not exist",
            )

        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            with mock.patch("tick.gh_text", side_effect=gh_text_side_effect):
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_implementer(
                        decision, str(wt_root),
                        tick_log=tick_log, attempts=None,
                    )

    # Cleanup must NOT be called — the marker must survive for retry
    cleanup_mock.assert_not_called()
    # Worktree should still exist
    assert wt_dir.is_dir()
    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    failure = next(entry for entry in entries if entry["action"] == "side_effect_failed")
    assert failure["details"]["operation"] == "gh pr create"
    assert failure["details"]["returncode"] == 1
    assert "head branch does not exist" in failure["details"]["stderr_excerpt"]


def test_harvest_one_implementer_returns_early_when_result_missing(
    tmp_path: Path,
) -> None:
    """_harvest_one_implementer leaves marker when result.json is missing."""
    from tick import _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-5"
    wt_dir.mkdir()
    # No result.json written

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 5,
    }

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        with mock.patch("tick.cleanup_scratch") as cleanup_mock:
            _harvest_one_implementer(
                decision, str(wt_root),
                tick_log=None, attempts=None,
            )

    # harvest_implementation must not be called — no result to parse
    harvest_impl.assert_not_called()
    # Cleanup must not be called — marker must survive
    cleanup_mock.assert_not_called()


def test_harvest_implementers_routes_harvest_decisions_to_helper(
    tmp_path: Path,
) -> None:
    """harvest_implementers() calls _harvest_one_implementer for harvest actions."""
    from tick import harvest_implementers  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-99"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE"}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "implement",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "",
        "pre_label": "ai:available",
        "dispatch_id": "d-99",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    # Set up GH label state: issue 99 is OPEN with ai:implementing
    with mock.patch("tick.gh_text") as gh_text_mock:
        gh_text_mock.side_effect = [
            "OPEN",                                 # lifecycle check
            "https://github.com/test-owner/test-repo/pull/100\n",  # PR create
        ]
        with mock.patch("tick.gh_json") as gh_json_mock:
            gh_json_mock.return_value = {
                "labels": [{"name": "ai:implementing"}],
            }
            with mock.patch("tick.cleanup_scratch"):
                with mock.patch("tick.relabel"):
                    with mock.patch("tick.gh"):
                        result = harvest_implementers(
                            tick_log=None, target=None, wt_root=str(wt_root),
                        )

    # reconcile_ok — the harvest path should execute
    assert result is True


# --- Reviewer harvest side effects ---


def test_harvest_one_reviewer_promotes_and_posts_comment(
    tmp_path: Path,
) -> None:
    """_harvest_one_reviewer posts comment, relabels, and cleans up on promote."""
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(
        '{"verdict": "ready_for_human", "findings": []}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc123",
        "head_sha": "def456",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-42",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))
    (wt_dir / "review-observation.json").write_text(json.dumps({
        "schema_version": 1,
        "status": "complete",
        "evidence_contract": "successful_command_referencing_skill_file",
        "explicit_invocation_events_supported": False,
        "observed_skills": [
            {"name": "contract-review", "source": "repository"},
        ],
        "event_count": 8,
        "parse_errors": 0,
        "oversized_events_skipped": 0,
        "skills_truncated": 0,
    }))

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 42,
    }

    tick_log = mock.MagicMock()
    side_effect_order: list[str] = []
    tick_log.log.side_effect = lambda **_kwargs: side_effect_order.append("log")

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "promote",
            "label": "ai:ready-for-human",
            "post_comment": True,
            "comment_body": "✅ No blockers",
            "base_drift": False,
            "head_drift": False,
            "rounds": 1,
            "verdict": "ready_for_human",
        }
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = "abc123"  # current_base == pinned_base → no drift
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = reviewer_live_identity("main", "def456")
                with mock.patch(
                    "tick._converge_pipeline_labels", return_value=True,
                ) as converge_mock:
                    with mock.patch(
                        "tick.cleanup_scratch",
                        side_effect=lambda _path: side_effect_order.append("cleanup"),
                    ) as cleanup_mock:
                        with mock.patch("tick.gh") as gh_mock:
                            _harvest_one_reviewer(
                                decision, str(wt_root),
                                tick_log=tick_log, attempts=None,
                            )

    # harvest_review was called with correct args
    harvest_review.assert_called_once()
    call_kwargs = harvest_review.call_args[1]
    assert call_kwargs["pinned_base"] == "abc123"
    assert call_kwargs["current_base"] == "abc123"
    assert call_kwargs["pinned_head"] == "def456"
    assert call_kwargs["current_head"] == "def456"
    assert call_kwargs["pre_label"] == "ai:implemented"

    # Comment posted via gh pr comment
    gh_mock.assert_any_call(
        "pr", "comment", "42", "--body-file", mock.ANY,
    )

    # Relabeled to ai:ready-for-human
    converge_mock.assert_called_once_with(
        "pr", 42, "ai:ready-for-human",
        tick_log=tick_log, phase="harvest", stage="review",
    )

    observation_call = next(
        call for call in tick_log.log.call_args_list
        if call.kwargs.get("action") == "reviewer_skill_observation"
    )
    assert observation_call.kwargs["dispatch_id"] == "d-42"
    assert observation_call.kwargs["details"]["observed_skills"] == [
        {"name": "contract-review", "source": "repository"},
    ]
    assert side_effect_order == ["log", "log", "cleanup"]

    # Cleaned up
    cleanup_mock.assert_called_once_with(str(wt_dir))


def test_harvest_one_reviewer_requests_changes(
    tmp_path: Path,
) -> None:
    """_harvest_one_reviewer posts findings comment, relabels to changes-requested."""
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(
        '{"verdict": "changes_requested", "findings": [{"severity": "blocker", "file": "x.py"}]}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 7,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:revised",
        "dispatch_id": "d-7",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "request_changes",
            "label": "ai:changes-requested",
            "post_comment": True,
            "comment_body": "🔧 Changes requested.",
            "base_drift": False,
            "head_drift": False,
            "rounds": 1,
            "verdict": "changes_requested",
        }
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = "b1"
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = reviewer_live_identity("main", "h1")
                with mock.patch(
                    "tick._converge_pipeline_labels", return_value=True,
                ) as converge_mock:
                    with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                        with mock.patch("tick.gh"):
                            _harvest_one_reviewer(
                                decision, str(wt_root),
                                tick_log=None, attempts=None,
                            )

    converge_mock.assert_called_once_with(
        "pr", 7, "ai:changes-requested",
        tick_log=None, phase="harvest", stage="review",
    )
    cleanup_mock.assert_called_once()


def test_harvest_one_reviewer_discards_on_base_drift(
    tmp_path: Path,
) -> None:
    """_harvest_one_reviewer relabels to pre_label on discard (no comment)."""
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-3"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "ready_for_human"}')
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 3,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "pinned_base_sha",
        "head_sha": "pinned_head_sha",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-3",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 3,
    }

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "discard",
            "label": "ai:implemented",
            "post_comment": False,
            "comment_body": "",
            "base_drift": True,
            "head_drift": False,
            "rounds": 0,
            "verdict": "ready_for_human",
        }
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = "abc123"  # base advanced
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = reviewer_live_identity(
                    "main", "pinned_head_sha",
                )
                with mock.patch(
                    "tick._converge_pipeline_labels", return_value=True,
                ) as converge_mock:
                    with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                        with mock.patch("tick.gh") as gh_mock:
                            _harvest_one_reviewer(
                                decision, str(wt_root),
                                tick_log=None, attempts=None,
                            )

    # No comment posted on discard
    for call_args in gh_mock.call_args_list:
        arg0 = call_args[0][0] if call_args[0] else ""
        assert arg0 != "pr" or "comment" not in str(call_args)

    # Relabeled back to pre_label
    converge_mock.assert_called_once_with(
        "pr", 3, "ai:implemented",
        tick_log=None, phase="harvest", stage="review",
    )
    cleanup_mock.assert_called_once()


def test_harvest_one_reviewer_discards_on_base_retarget(
    tmp_path: Path,
) -> None:
    """A PR retargeted to a new base during the review is discarded, not promoted.

    Regression: the old base's tip and the head are both unchanged, so the
    SHA drift checks alone would promote a clean review anchored to the wrong
    target. Runs the real pipeline decision (no harvest_review mock).
    """
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-9"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(
        '{"verdict": "ready_for_human", "findings": []}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 9,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "base_tip_sha": "t1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-9",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 9,
    }

    with mock.patch("tick.git") as git_mock:
        with mock.patch("tick.gh_json") as gh_json_mock:
            # Retargeted: live base is develop, old tip and head unchanged.
            gh_json_mock.side_effect = reviewer_live_identity("develop", "h1")
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    with mock.patch("tick.gh") as gh_mock:
                        _harvest_one_reviewer(
                            decision, str(wt_root),
                            tick_log=None, attempts=None,
                        )

    # The old base ref may be deleted after retarget — its tip must not be
    # required for the discard (no git fetch/rev-parse of the old ref).
    git_mock.assert_not_called()

    # No comment posted; the review is discarded, never promoted.
    for call_args in gh_mock.call_args_list:
        assert "comment" not in [str(a) for a in call_args[0]]
    converge_mock.assert_called_once_with(
        "pr", 9, "ai:implemented",
        tick_log=None, phase="harvest", stage="review",
    )
    cleanup_mock.assert_called_once_with(str(wt_dir))


def test_harvest_one_reviewer_posts_fresh_comment_when_same_shas_new_base_ref(
    tmp_path: Path,
) -> None:
    """A post-retarget round with an identical SHA triple still posts its comment.

    Regression (review finding): the base branch name is part of review
    identity, so it must be part of the replay token too. A PR retargeted
    from feat-1 to main where both point at the same commit re-reviews with
    the same (diff base, base tip, head) triple; the old round's comment
    must not suppress the fresh comment — the posted comment is what the
    next fixer dispatch reads as its findings handoff. Runs the real
    pipeline decision (no harvest_review mock).
    """
    import pipeline
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-17"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({
        "verdict": "changes_requested",
        "findings": [
            {"severity": "blocker", "file": "a.py", "summary": "boom"},
        ],
    }))
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 17,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "mb1",
        "base_tip_sha": "t1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-17",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    # A prior round posted under the old base name with the SAME SHA triple.
    stale_comment = pipeline.render_review_comment(
        verdict="ready_for_human",
        findings=[],
        base_sha="mb1",
        base_tip_sha="t1",
        head_sha="h1",
        base_ref="feat-1",
    )

    def gh_json_side_effect(*args: str) -> object:
        if "baseRefName,headRefOid" in args:
            return {"baseRefName": "main", "headRefOid": "h1"}
        return [{"body": stale_comment}]

    posted_bodies: list[str] = []

    def gh_side_effect(*args: str) -> str:
        if args[:2] == ("pr", "comment"):
            body_file = args[args.index("--body-file") + 1]
            posted_bodies.append(Path(body_file).read_text())
        return ""

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 17,
    }

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = "t1"  # base tip unchanged
        with mock.patch("tick.gh_json", side_effect=gh_json_side_effect):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch"):
                    with mock.patch("tick.gh", side_effect=gh_side_effect):
                        _harvest_one_reviewer(
                            decision, str(wt_root),
                            tick_log=None, attempts=None,
                        )

    # The fresh round's comment was posted despite the SHA-identical stale
    # comment, and carries the new base-ref identity and the new findings.
    assert len(posted_bodies) == 1
    assert "boom" in posted_bodies[0]
    assert pipeline.review_already_posted(
        [{"body": posted_bodies[0]}],
        "mb1", "h1", base_tip_sha="t1", base_ref="main",
    ) is True
    converge_mock.assert_called_once_with(
        "pr", 17, "ai:changes-requested",
        tick_log=None, phase="harvest", stage="review",
    )


def test_harvest_one_reviewer_threads_base_refs_to_pipeline(
    tmp_path: Path,
) -> None:
    """The marker's base_ref and the live baseRefName reach harvest_review."""
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-11"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(
        '{"verdict": "ready_for_human", "findings": []}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 11,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-11",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 11,
    }

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "promote", "label": "ai:ready-for-human",
            "post_comment": False, "comment_body": "",
            "base_retarget": False, "base_drift": False, "head_drift": False,
            "rounds": 1, "verdict": "ready_for_human",
        }
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = "b1"
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = [
                    {"baseRefName": "main", "headRefOid": "h1"},
                    [],  # comments
                ]
                with mock.patch(
                    "tick._converge_pipeline_labels", return_value=True,
                ):
                    with mock.patch("tick.cleanup_scratch"):
                        _harvest_one_reviewer(
                            decision, str(wt_root),
                            tick_log=None, attempts=None,
                        )

    harvest_review.assert_called_once()
    call_kwargs = harvest_review.call_args[1]
    assert call_kwargs["pinned_base_ref"] == "main"
    assert call_kwargs["current_base_ref"] == "main"
    assert call_kwargs["current_head"] == "h1"


def test_reviewer_escalation_finalize_failure_resumes_next_tick_not_held_replay(
    tmp_path: Path,
) -> None:
    """The reviewer's own escalation needs a recovery owner like the others.

    ``decide_review_harvest`` escalates a schema-invalid verdict, and a
    changes-requested verdict with no blocking finding, to
    ``ai:needs-human-decision`` with ``post_comment`` false — so nothing is
    published and ``review_already_posted`` has no comment to make the retry
    idempotent. That label is a hold. Without a recorded intent, a crash or a
    failed archive move between the converge and the scratch cleanup leaves a
    completed marker sitting under a hold the *pipeline itself* applied, and
    the next tick's generic hold branch publishes the held-completion notice —
    which states that a workflow hold was observed while the engine was
    running. No hold was observed and the engine had already finished, so the
    only durable explanation on the PR would be false.
    """
    from pipeline import (  # type: ignore[attr-defined]
        ESCALATION_MARKER,
        LABEL_NEEDS_HUMAN,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_one_reviewer,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-11"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "ready_for_human"}')
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 11,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "rev-11-1",
    }))
    attempts = Attempts(str(wt_root))
    attempts.incr(11)
    tick_log = TickLog.open(str(tmp_path / "logs"))
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 11))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 11,
    }
    bodies: list[str] = []
    gh_mock, calls, state = label_state_gh_mock(["ai:reviewing"], bodies)

    # Tick 1: the reviewer escalates its own verdict, the converge lands, and
    # the final archive move fails.
    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "escalate", "label": LABEL_NEEDS_HUMAN,
            "post_comment": False, "comment_body": "",
            "base_retarget": False, "base_drift": False, "head_drift": False,
            "rounds": 1, "verdict": None,
        }
        with mock.patch("tick.git", return_value="b1"):
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = [
                    {"baseRefName": "main", "headRefOid": "h1"},
                    [],  # comments
                    {"labels": [{"name": "ai:reviewing"}]},  # converge read
                    {"labels": [{"name": LABEL_NEEDS_HUMAN}]},  # verification
                ]
                with mock.patch("tick.gh", side_effect=gh_mock):
                    with mock.patch(
                        "tick._finalize_terminal_archive", return_value=False,
                    ):
                        _harvest_one_reviewer(
                            decision, str(wt_root),
                            tick_log=tick_log, attempts=attempts,
                        )

    assert intent_path.is_file(), (
        "the reviewer escalation records a recovery owner before converging"
    )
    assert (wt_dir / "dispatch.json").is_file(), "the marker outlives the failure"
    assert bodies == [], "the reviewer escalation is silent by contract"

    # Tick 2: reconciliation resumes the same escalation instead of routing
    # the completed marker through the held-completion publication.
    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    gh_mock2, calls2, state2 = label_state_gh_mock([LABEL_NEEDS_HUMAN], bodies)
    with mock.patch("tick.gh", side_effect=gh_mock2):
        with mock.patch(
            "tick._finalize_terminal_archive", return_value=True,
        ) as finalize:
            resumed = _reconcile_one(
                11, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert resumed["action"] == "noop"
    finalize.assert_called_once()
    assert not intent_path.exists(), "the resumed escalation retires its intent"
    assert not [body for body in bodies if ESCALATION_MARKER in body], (
        "no held-completion notice may claim a hold the pipeline itself applied"
    )
    assert [call for call in calls2 if call[:2] == ("pr", "edit")] == [], (
        "the landed converge is never re-applied"
    )


def test_reviewer_escalation_is_gated_on_its_terminal_intent(
    tmp_path: Path,
) -> None:
    """An unrecordable decision does not start the reviewer's transition.

    The gate is what makes the recovery owner meaningful: a converge that
    lands without a durable record behind it is exactly the state whose
    replay this intent exists to prevent, so the transition is not begun at
    all when the record cannot be written.
    """
    from pipeline import LABEL_NEEDS_HUMAN  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_one_reviewer,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-11"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "ready_for_human"}')
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 11,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "rev-11-1",
    }))
    attempts = Attempts(str(wt_root))
    attempts.incr(11)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 11,
    }

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "escalate", "label": LABEL_NEEDS_HUMAN,
            "post_comment": False, "comment_body": "",
            "base_retarget": False, "base_drift": False, "head_drift": False,
            "rounds": 1, "verdict": None,
        }
        with mock.patch("tick.git", return_value="b1"):
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = [
                    {"baseRefName": "main", "headRefOid": "h1"},
                    [],  # comments
                ]
                with mock.patch(
                    "tick._record_terminal_intent", return_value=False,
                ):
                    with mock.patch(
                        "tick._converge_pipeline_labels",
                    ) as converge_mock:
                        with mock.patch("tick.gh") as gh_mock:
                            with mock.patch(
                                "tick.cleanup_scratch",
                            ) as cleanup_mock:
                                _harvest_one_reviewer(
                                    decision, str(wt_root),
                                    tick_log=tick_log, attempts=attempts,
                                )

    converge_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    gh_mock.assert_not_called()
    assert not Path(_terminal_intent_path(str(wt_root), "pr", 11)).exists()
    assert (wt_dir / "dispatch.json").is_file(), "the marker is retained"
    assert attempts.read(11) == 1, "the attempt counter is untouched"
    assert [
        entry for entry in read_tick_log_entries(tmp_path / "logs")
        if entry.get("action") == "terminal_intent_failed"
    ], "the refusal is operator-visible"


def test_harvest_one_reviewer_retains_marker_on_malformed_point_read(
    tmp_path: Path,
) -> None:
    """A point-read missing baseRefName fails closed: no relabel, no cleanup."""
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-13"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "ready_for_human"}')
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 13,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-13",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 13,
    }

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = "b1"
        with mock.patch("tick.gh_json") as gh_json_mock:
            gh_json_mock.return_value = {"headRefOid": "h1"}  # no baseRefName
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_reviewer(
                        decision, str(wt_root),
                        tick_log=None, attempts=None,
                    )

    converge_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    assert (wt_dir / "dispatch.json").exists()


def test_harvest_one_reviewer_uses_base_tip_sha_as_pinned_base(
    tmp_path: Path,
) -> None:
    """When base_tip_sha is present in the marker it takes precedence over base_sha."""
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-77"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(
        '{"verdict": "ready_for_human", "findings": []}',
    )
    # Marker has both base_sha (merge-base) and base_tip_sha (main tip at dispatch).
    # The harvest should use base_tip_sha as pinned_base for drift detection.
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 77,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "merge_base_sha",
        "base_tip_sha": "main_tip_at_dispatch",
        "head_sha": "pr_head_sha",
        "pre_label": "ai:revised",
        "dispatch_id": "d-77",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 77,
    }

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "promote",
            "label": "ai:ready-for-human",
            "post_comment": True,
            "comment_body": "✅ No blockers",
            "base_drift": False,
            "head_drift": False,
            "rounds": 1,
            "verdict": "ready_for_human",
        }
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = "main_tip_at_dispatch"  # no drift
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = reviewer_live_identity(
                    "main", "pr_head_sha",
                )
                with mock.patch(
                    "tick._converge_pipeline_labels", return_value=True,
                ):
                    with mock.patch("tick.cleanup_scratch"):
                        with mock.patch("tick.gh"):
                            _harvest_one_reviewer(
                                decision, str(wt_root),
                                tick_log=None, attempts=None,
                            )

    # base_tip_sha drives drift detection (pinned_base); base_sha (the
    # merge-base Codex diffed from) is threaded separately as the audit truth.
    harvest_review.assert_called_once()
    call_kwargs = harvest_review.call_args[1]
    assert call_kwargs["pinned_base"] == "main_tip_at_dispatch"
    assert call_kwargs["pinned_head"] == "pr_head_sha"
    assert call_kwargs["diff_base_sha"] == "merge_base_sha"


def test_harvest_one_reviewer_skips_cleanup_when_comment_post_fails(
    tmp_path: Path,
) -> None:
    """_harvest_one_reviewer retains marker when gh pr comment fails."""
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-5"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(
        '{"verdict": "changes_requested", "findings": [{"severity": "major"}]}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 5,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-5",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 5,
    }

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "request_changes",
            "label": "ai:changes-requested",
            "post_comment": True,
            "comment_body": "🔧 Changes requested.",
            "base_drift": False,
            "head_drift": False,
            "rounds": 1,
            "verdict": "changes_requested",
        }
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = "b1"
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = reviewer_live_identity("main", "h1")
                with mock.patch(
                    "tick._converge_pipeline_labels", return_value=True,
                ) as converge_mock:
                    with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                        with mock.patch("tick.gh") as gh_mock:
                            # gh pr comment raises CalledProcessError
                            def gh_side_effect(*args: str, **kwargs: object) -> object:
                                if args[:3] == ("pr", "comment", "5"):
                                    raise subprocess.CalledProcessError(1, ["gh"])
                                if "check" in kwargs:
                                    return mock.DEFAULT
                                return mock.DEFAULT

                            gh_mock.side_effect = gh_side_effect

                            _harvest_one_reviewer(
                                decision, str(wt_root),
                                tick_log=None, attempts=None,
                            )

    # Label convergence must NOT run — comment post failed, preserve marker.
    converge_mock.assert_not_called()
    # Cleanup must NOT be called
    cleanup_mock.assert_not_called()


def test_harvest_one_reviewer_escalates_after_repeated_comment_failures(
    tmp_path: Path,
) -> None:
    """A permanently failing comment post escalates at the attempt cap.

    Without the cap, a permanently rejected comment (e.g. exceeding GitHub's
    size limit) retries every tick forever while the PR pins an admission
    slot at ai:reviewing — three such items halt all dispatch.
    """
    from tick import Attempts, _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-5"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(
        '{"verdict": "changes_requested", "findings": [{"severity": "major"}]}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 5,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-5",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    attempts = Attempts(str(wt_root))
    for _ in range(3):  # pipeline.MAX_DISPATCH_ATTEMPTS
        attempts.incr(5)

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 5,
    }

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.return_value = {
            "action": "request_changes",
            "label": "ai:changes-requested",
            "post_comment": True,
            "comment_body": "🔧 Changes requested.",
            "base_drift": False,
            "head_drift": False,
            "rounds": 1,
            "verdict": "changes_requested",
        }
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = "b1"
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = reviewer_live_identity("main", "h1")
                with mock.patch(
                    "tick._converge_pipeline_labels", return_value=True,
                ) as converge_mock:
                    with mock.patch("tick.gh") as gh_mock:
                        def gh_side_effect(*args: str, **kwargs: object) -> object:
                            if args[:3] == ("pr", "comment", "5"):
                                raise subprocess.CalledProcessError(1, ["gh"])
                            return mock.DEFAULT

                        gh_mock.side_effect = gh_side_effect

                        _harvest_one_reviewer(
                            decision, str(wt_root),
                            tick_log=None, attempts=attempts,
                        )

    assert any(
        call.args[:3] == ("pr", 5, "ai:needs-human-decision")
        for call in converge_mock.call_args_list
    )
    assert not wt_dir.is_dir(), "scratch must be cleaned after escalation"
    reviewer_archives = list((wt_root / ".harvest-evidence").glob("pr-5-d-5"))
    assert len(reviewer_archives) == 1
    assert (reviewer_archives[0] / "review.json").is_file()
    assert attempts.read(5) == 0, "attempt counter must be cleared"


def test_harvest_one_implementer_escalates_after_repeated_gh_failures(
    tmp_path: Path,
) -> None:
    """A permanently failing gh pr create escalates at the attempt cap.

    ``gh pr create`` fails permanently when the branch already has an open
    PR; without the cap the issue pins an ai:implementing slot forever.
    """
    from tick import Attempts, _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-3"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE"}',
    )

    attempts = Attempts(str(wt_root), "issue-")
    for _ in range(3):
        attempts.incr(3)

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 3,
    }

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr",
            "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        bodies: list[str] = []
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            with mock.patch("tick.gh", side_effect=capture_comment_bodies(
                bodies, entity="issue",
            )):
                with mock.patch("tick.gh_text") as gh_text_mock:
                    gh_text_mock.side_effect = subprocess.CalledProcessError(
                        1, ["gh"],
                    )
                    with mock.patch(
                        "tick._converge_pipeline_labels", return_value=True,
                    ) as converge_mock:
                        _harvest_one_implementer(
                            decision, str(wt_root),
                            tick_log=None, attempts=attempts,
                        )

    assert any(
        call.args[:3] == ("issue", 3, "ai:needs-human-decision")
        for call in converge_mock.call_args_list
    )
    assert len(bodies) == 1, "the capped escalation must explain itself"
    assert "ai:needs-human-decision" in bodies[0]
    assert not wt_dir.is_dir(), "scratch must be cleaned after escalation"
    implementer_archives = list(
        (wt_root / ".harvest-evidence").glob("issue-3-unknown"),
    )
    assert len(implementer_archives) == 1
    assert (implementer_archives[0] / "result.json").is_file()
    assert attempts.read(3) == 0, "attempt counter must be cleared"


def test_harvest_one_implementer_increments_attempts_on_gh_failure(
    tmp_path: Path,
) -> None:
    """Below the cap, a gh failure bumps the attempt counter and retains
    the marker for retry."""
    from tick import Attempts, _harvest_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-3"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "IMPLEMENTATION_COMPLETE"}',
    )

    attempts = Attempts(str(wt_root), "issue-")

    decision: dict[str, object] = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 3,
    }

    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "create_pr",
            "label": "",
            "sentinel": "IMPLEMENTATION_COMPLETE",
        }
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = subprocess.CalledProcessError(
                    1, ["gh"],
                )
                with mock.patch("tick.relabel") as relabel_mock:
                    _harvest_one_implementer(
                        decision, str(wt_root),
                        tick_log=None, attempts=attempts,
                    )

    relabel_mock.assert_not_called()
    assert wt_dir.is_dir(), "marker must survive for retry below the cap"
    assert attempts.read(3) == 1


def test_harvest_one_fixer_escalates_after_repeated_relabel_failures(
    tmp_path: Path,
) -> None:
    """A permanently failing recheck relabel escalates at the attempt cap."""
    from tick import Attempts, _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED"}',
    )

    attempts = Attempts(str(wt_root))
    for _ in range(3):
        attempts.incr(7)

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
        harvest_fix.return_value = {
            "action": "recheck",
            "label": "ai:revised",
            "sentinel": "FIXES_PUSHED_RECHECK_NEEDED",
        }
        bodies: list[str] = []
        with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
            with mock.patch(
                "tick._converge_pipeline_labels", side_effect=[False, True],
            ) as converge_mock:
                _harvest_one_fixer(
                    decision, str(wt_root),
                    tick_log=None, attempts=attempts,
                )

    assert any(
        call.args[:3] == ("pr", 7, "ai:needs-human-decision")
        for call in converge_mock.call_args_list
    )
    assert len(bodies) == 1, "the capped escalation must explain itself"
    assert not wt_dir.is_dir(), "scratch must be cleaned after escalation"
    fixer_archives = list((wt_root / ".harvest-evidence").glob("pr-7-unknown"))
    assert len(fixer_archives) == 1
    assert (fixer_archives[0] / "result.json").is_file()
    assert attempts.read(7) == 0, "attempt counter must be cleared"


def test_bound_failed_harvest_retains_custody_when_archive_fails(
    tmp_path: Path,
) -> None:
    """Attempt-cap escalation cannot relabel or delete before durable archive."""
    from tick import Attempts, _bound_failed_harvest  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text("evidence")
    attempts = Attempts(str(wt_root))
    for _ in range(3):
        attempts.incr(7)

    with mock.patch("tick._archive_harvest_evidence", return_value=""):
        with mock.patch("tick._relabel_with_context") as relabel_mock:
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _bound_failed_harvest(
                    7,
                    "pr",
                    "ai:fixing",
                    str(wt_dir),
                    attempts,
                    stage="fix",
                )

    relabel_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    assert wt_dir.is_dir()
    assert attempts.read(7) == 3


def test_harvest_one_reviewer_returns_early_on_git_fetch_failure(
    tmp_path: Path,
) -> None:
    """_harvest_one_reviewer leaves marker when git fetch fails."""
    from tick import _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-9"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text("{}")
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 9,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-9",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 9,
    }

    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = subprocess.CalledProcessError(1, ["git"])
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.side_effect = reviewer_live_identity("main", "h1")
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_reviewer(
                        decision, str(wt_root),
                        tick_log=None, attempts=None,
                    )

    # The base-tip fetch was reached (live identity read succeeded first)
    git_mock.assert_called()
    # harvest_review must not be called — git fetch failed
    harvest_review.assert_not_called()
    # Cleanup must not be called — marker must survive
    cleanup_mock.assert_not_called()


def test_harvest_reviewers_routes_harvest_decisions_to_helper(
    tmp_path: Path,
) -> None:
    """harvest_reviewers() calls _harvest_one_reviewer for harvest actions."""
    from tick import harvest_reviewers  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-99"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "ready_for_human"}')
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-99",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    with mock.patch("tick.gh_text") as gh_text_mock:
        gh_text_mock.side_effect = [
            "OPEN",     # lifecycle check
            "def",      # current_head
        ]
        with mock.patch("tick.gh_json") as gh_json_mock:
            gh_json_mock.return_value = {
                "labels": [{"name": "ai:reviewing"}],
            }
            with mock.patch("tick.pipeline.harvest_review") as harvest_review:
                harvest_review.return_value = {
                    "action": "promote",
                    "label": "ai:ready-for-human",
                    "post_comment": True,
                    "comment_body": "LGTM",
                    "base_drift": False,
                    "head_drift": False,
                    "rounds": 1,
                    "verdict": "ready_for_human",
                }
                with mock.patch("tick.git") as git_mock:
                    git_mock.return_value = "abc"
                    with mock.patch("tick.cleanup_scratch"):
                        with mock.patch("tick.relabel"):
                            with mock.patch("tick.gh"):
                                result = harvest_reviewers(
                                    tick_log=None, target=None, wt_root=str(wt_root),
                                )

    assert result is True


def test_harvest_failed_reviewer_archives_before_terminal_relabel(
    tmp_path: Path,
) -> None:
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_failed_reviewer,
        _log_tick_summary,
    )

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-5"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-5"}))
    (wt_dir / "completion.json").write_text("failure")
    (wt_dir / "stderr.log").write_text("engine failed\n")
    (wt_dir / "review-observation.json").write_text(json.dumps({
        "schema_version": 1,
        "status": "partial",
        "evidence_contract": "successful_command_referencing_skill_file",
        "explicit_invocation_events_supported": False,
        "observed_skills": [{"name": "failure-review", "source": "repository"}],
        "event_count": 2,
        "parse_errors": 1,
        "oversized_events_skipped": 0,
        "skills_truncated": 0,
    }))
    attempts = Attempts(str(wt_root))
    attempts.incr(5)
    tick_log = TickLog.open(str(tmp_path / "tick-log"))

    def assert_archive_exists(*_args: object, **_kwargs: object) -> bool:
        archives = list((wt_root / ".harvest-evidence").glob("pr-5-d-5"))
        assert len(archives) == 1
        assert (archives[0] / "completion.json").read_text() == "failure"
        assert (archives[0] / "stderr.log").read_text() == "engine failed\n"
        assert wt_dir.is_dir()
        return True

    with mock.patch(
        "tick._converge_pipeline_labels", side_effect=assert_archive_exists,
    ):
        assert _harvest_failed_reviewer(
            5, str(wt_root), tick_log=tick_log, attempts=attempts,
        ) is True

    assert not wt_dir.exists()
    assert attempts.read(5) == 0
    _log_tick_summary(tick_log, reconcile_ok=True)
    entries = tick_log.tail()
    escalations = [entry for entry in entries if entry.get("action") == "escalate"]
    assert len(escalations) == 1
    assert escalations[0]["pr"] == 5
    assert escalations[0]["stage"] == "review"
    assert escalations[0]["dispatch_id"] == "d-5"
    assert escalations[0]["reason"] == "wrapper-confirmed reviewer failure"
    observations = [
        entry for entry in entries
        if entry.get("action") == "reviewer_skill_observation"
    ]
    assert observations[0]["details"]["observed_skills"] == [
        {"name": "failure-review", "source": "repository"},
    ]
    summaries = [entry for entry in entries if entry.get("action") == "tick_complete"]
    assert summaries[0]["details"]["escalated"] == 1


def test_harvest_failed_reviewer_retains_custody_when_archive_fails(
    tmp_path: Path,
) -> None:
    from tick import _harvest_failed_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-5"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-5"}))
    with mock.patch("tick._archive_harvest_evidence", return_value=""):
        with mock.patch("tick._relabel_with_context") as relabel_mock:
            assert _harvest_failed_reviewer(5, str(wt_root)) is False
    relabel_mock.assert_not_called()
    assert wt_dir.is_dir()


def test_harvest_failed_reviewer_clears_attempts_before_archive_move(
    tmp_path: Path,
) -> None:
    """The attempt counter is cleared before the archive move deletes the
    marker: a crash between the two must not strand a stale durable count
    that a later requeue would inherit against the retry cap."""
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _harvest_failed_reviewer,
    )

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-5"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-5"}))
    (wt_dir / "completion.json").write_text("failure")
    attempts = Attempts(str(wt_root))
    attempts.incr(5)

    with mock.patch("tick._converge_pipeline_labels", return_value=True):
        with mock.patch("tick._finalize_harvest_archive", return_value=False):
            assert _harvest_failed_reviewer(
                5, str(wt_root), attempts=attempts,
            ) is False

    # Terminal relabel landed, so the counter is already cleared even though
    # the archive move failed and scratch custody is retained for retry.
    assert attempts.read(5) == 0
    assert wt_dir.is_dir()


def test_harvest_failed_reviewer_retains_marker_when_relabel_fails(
    tmp_path: Path,
) -> None:
    from tick import TickLog, _harvest_failed_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-5"
    wt_dir.mkdir(parents=True)
    marker = wt_dir / "dispatch.json"
    marker.write_text(json.dumps({"dispatch_id": "d-5"}))
    (wt_dir / "completion.json").write_text("failure")
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    with mock.patch("tick._converge_pipeline_labels", return_value=False):
        assert _harvest_failed_reviewer(
            5, str(wt_root), tick_log=tick_log,
        ) is False
    assert marker.is_file()
    assert (wt_dir / "completion.json").is_file()
    # Prepared archive is discarded on failure; retry re-archives from scratch.
    evidence_root = wt_root / ".harvest-evidence"
    assert not evidence_root.exists() or not any(evidence_root.iterdir())
    failures = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "label_convergence_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["pr"] == 5
    assert failures[0]["stage"] == "review"
    assert failures[0]["dispatch_id"] == "d-5"
    assert failures[0]["reason"] == "retaining failed reviewer scratch for retry"


# --- Fixer harvest side effects ---


def test_harvest_one_fixer_rechecks_and_relabels(
    tmp_path: Path,
) -> None:
    """_harvest_one_fixer relabels to ai:revised and cleans up on recheck."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "fix",
        "pr_number": 12,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-12",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    with mock.patch(
        "tick.gh_json", return_value={"headRefOid": "a" * 40},
    ):
        with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
            harvest_fix.return_value = {
                "action": "recheck",
                "label": "ai:revised",
                "sentinel": "FIXES_PUSHED_RECHECK_NEEDED",
            }
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_fixer(
                        decision, str(wt_root),
                        tick_log=None, attempts=None,
                    )

    harvest_fix.assert_called_once_with(
        verification_unconfigured=False,
        result='{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
        pre_label="ai:changes-requested",
        pinned_head="def",
        current_head="a" * 40,
        body_revision="",
        dispatch_id="d-12",
        review_sha256="",
    )
    converge_mock.assert_called_once_with(
        "pr", 12, "ai:revised",
        tick_log=None, phase="harvest", stage="fix",
    )
    cleanup_mock.assert_called_once_with(str(wt_dir))


def test_harvest_one_fixer_escalates_when_head_unchanged(
    tmp_path: Path,
) -> None:
    """A FIXES_PUSHED claim with an unchanged live head escalates, not rechecks.

    Runs the real pipeline.harvest_fix (no harvest mock): the live head
    point-read equals the marker's pinned head, so the sentinel is
    contradictory output. Relabeling to ai:revised here would re-enter an
    identical review round whose comment the replay token suppresses,
    freezing the iteration count below the cap — the unbounded-spend loop
    this check exists to terminate.
    """
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "fix",
        "pr_number": 12,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b" * 40,
        "head_sha": "d" * 40,
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-12",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    bodies: list[str] = []
    with mock.patch(
        "tick.gh_json", return_value={"headRefOid": "d" * 40},
    ) as gh_json_mock:
        with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                _harvest_one_fixer(
                    decision, str(wt_root),
                    tick_log=None, attempts=None,
                )

    gh_json_mock.assert_called_once_with(
        "pr", "view", "12", "--json", "headRefOid,body",
    )
    converge_mock.assert_called_once_with(
        "pr", 12, "ai:needs-human-decision",
        tick_log=None, phase="harvest", stage="fix",
    )

    # The escalation states its reason on the PR — an operator must not have to
    # read a local evidence archive to learn why a decision is required.
    assert len(bodies) == 1
    assert "`escalate` → `ai:needs-human-decision`" in bodies[0]
    assert "the PR head did not advance" in bodies[0]


def test_harvest_one_fixer_retains_marker_on_failed_head_read(
    tmp_path: Path,
) -> None:
    """A failed live-head point-read retains the marker — no relabel, no cleanup."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-12",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    for payload in (
        subprocess.CalledProcessError(1, ["gh"]),
        ValueError("bad json"),
    ):
        with mock.patch("tick.gh_json", side_effect=payload):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_fixer(
                        decision, str(wt_root),
                        tick_log=None, attempts=None,
                    )
        converge_mock.assert_not_called()
        cleanup_mock.assert_not_called()
        assert (wt_dir / "dispatch.json").is_file()


def test_harvest_one_fixer_retains_marker_on_malformed_head_payload(
    tmp_path: Path,
) -> None:
    """Malformed point-read payloads retain the marker (fail closed)."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-12",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    for payload in ([], "oid", {"headRefOid": 7}, {"headRefOid": ""}, {}):
        with mock.patch("tick.gh_json", return_value=payload):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_fixer(
                        decision, str(wt_root),
                        tick_log=None, attempts=None,
                    )
        converge_mock.assert_not_called()
        cleanup_mock.assert_not_called()
        assert (wt_dir / "dispatch.json").is_file(), payload


def test_harvest_one_fixer_rechecks_when_head_advanced(
    tmp_path: Path,
) -> None:
    """An advanced live head confirms the push — real decision path rechecks."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-12",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    with mock.patch(
        "tick.gh_json", return_value={"headRefOid": "e" * 40},
    ):
        with mock.patch("tick.gh") as gh_mock:
            gh_mock.side_effect = capture_comment_bodies(bodies := [])
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_fixer(
                        decision, str(wt_root),
                        tick_log=None, attempts=None,
                    )

    converge_mock.assert_called_once_with(
        "pr", 12, "ai:revised",
        tick_log=None, phase="harvest", stage="fix",
    )
    cleanup_mock.assert_called_once_with(str(wt_dir))

    # The recheck comment names the head before and after the push.
    assert len(bodies) == 1
    assert f"head `{'d' * 40}` → `{'e' * 40}`" in bodies[0]
    assert "`recheck` → `ai:revised`" in bodies[0]


def test_harvest_one_fixer_skips_head_read_without_marker_head(
    tmp_path: Path,
) -> None:
    """A marker without head_sha disables the push check — no gh read, recheck.

    Backward compatibility for in-flight fix markers written before the
    push-verification field mattered; the check cannot prove a contradiction
    without dispatch-time provenance.
    """
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-12",
        "engine": "claude",
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    with mock.patch("tick.gh_json") as gh_json_mock:
        with mock.patch("tick.gh"):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root),
                        tick_log=None, attempts=None,
                    )

    gh_json_mock.assert_not_called()
    converge_mock.assert_called_once_with(
        "pr", 12, "ai:revised",
        tick_log=None, phase="harvest", stage="fix",
    )


def test_harvest_one_fixer_escalates(
    tmp_path: Path,
) -> None:
    """_harvest_one_fixer relabels to ai:needs-human-decision on escalation."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-8"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "NEEDS_HUMAN_DECISION", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-8-1"}))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 8,
    }

    with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
        harvest_fix.return_value = {
            "action": "escalate",
            "label": "ai:needs-human-decision",
            "sentinel": "NEEDS_HUMAN_DECISION",
        }
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ) as converge_mock:
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_fixer(
                    decision, str(wt_root),
                    tick_log=None, attempts=None,
                )

    converge_mock.assert_called_once_with(
        "pr", 8, "ai:needs-human-decision",
        tick_log=None, phase="harvest", stage="fix",
    )
    cleanup_mock.assert_called_once()


def test_harvest_one_fixer_posts_comment_before_relabel(tmp_path: Path) -> None:
    """A recheck harvest posts the fixer comment before flipping the label."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-12-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    calls: list[str] = []
    with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
        with mock.patch("tick.gh") as gh_mock:
            gh_mock.side_effect = lambda *a: calls.append(f"gh:{a[1]}")
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                converge_mock.side_effect = lambda *a, **kw: (
                    calls.append("converge") or True
                )
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )

    assert calls == ["gh:comment", "converge"]
    body_path = gh_mock.call_args[0][4]
    assert gh_mock.call_args[0][:4] == ("pr", "comment", "12", "--body-file")
    # Staged under controller-owned state, never inside the branch worktree.
    assert body_path.startswith(str(wt_root / ".receipts"))
    assert not body_path.startswith(str(wt_dir))


def test_harvest_one_fixer_failed_post_blocks_relabel(tmp_path: Path) -> None:
    """A failed comment post leaves the label unchanged and retains the scratch."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-12-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
        with mock.patch(
            "tick.gh",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )

    converge_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    assert (wt_dir / "result.json").exists()


def test_harvest_one_fixer_does_not_repost_after_relabel_failure(
    tmp_path: Path,
) -> None:
    """A landed post plus a failed relabel re-labels next tick without reposting.

    The scratch survives the failed relabel so the harvest can retry; the
    receipt it carries is what keeps the retry from stamping a second comment
    on the PR for the same run.
    """
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-12-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    bodies: list[str] = []
    with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
        with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
            # First tick: comment lands, relabel fails → scratch retained.
            with mock.patch("tick._converge_pipeline_labels", return_value=False):
                _harvest_one_fixer(
                    decision, str(wt_root), tick_log=None, attempts=None,
                )
            assert len(bodies) == 1
            assert (wt_dir / "result.json").exists()

            # Second tick: same run re-harvested, relabel succeeds, no repost.
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )

    assert len(bodies) == 1
    converge_mock.assert_called_once_with(
        "pr", 12, "ai:revised",
        tick_log=None, phase="harvest", stage="fix",
    )


def test_harvest_one_fixer_reposts_when_the_retry_changes_the_decision(
    tmp_path: Path,
) -> None:
    """A same-dispatch retry that decides differently publishes the new outcome.

    Every retry point-reads a fresh head and recomputes the decision before it
    consults the receipt, so a receipt keyed on the dispatch alone would let a
    first tick's ``escalate`` comment stand while the second tick lands
    ``ai:revised`` — publishing a comment that contradicts the label, which is
    the exact audit gap this comment path exists to close.
    """
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-12-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    bodies: list[str] = []
    with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
        # First tick: the head still matches the pinned dispatch head, so the
        # success claim is contradictory and the run escalates. The relabel
        # fails, so the scratch and the receipt survive.
        with mock.patch("tick.gh_json", return_value={"headRefOid": "d" * 40}):
            with mock.patch("tick._converge_pipeline_labels", return_value=False):
                _harvest_one_fixer(
                    decision, str(wt_root), tick_log=None, attempts=None,
                )

        assert len(bodies) == 1
        assert "ai:needs-human-decision" in bodies[0]

        # Second tick: the push is now visible, so the same dispatch decides
        # recheck instead. The stale escalation receipt must not suppress it.
        with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )

    converge_mock.assert_called_once_with(
        "pr", 12, "ai:revised",
        tick_log=None, phase="harvest", stage="fix",
    )
    assert len(bodies) == 2
    assert "ai:revised" in bodies[1]


def test_harvest_one_fixer_starts_the_configured_transient_cooldown(
    tmp_path: Path,
) -> None:
    """The fixer's transient path uses the cooldown the tick validated."""
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        EngineCooldowns,
        _harvest_one_fixer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-8"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-8-quota",
        "engine": "claude-ds",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 8,
    }

    with mock.patch("tick.time.time", return_value=1_800_000_000.0), \
            mock.patch("tick.gh"), \
            mock.patch("tick._converge_pipeline_labels", return_value=True), \
            mock.patch("tick.cleanup_scratch"):
        _harvest_one_fixer(
            decision, str(wt_root), tick_log=None,
            attempts=Attempts(str(wt_root)),
            transient_cooldown_seconds=1200,
            transient_cooldown_reset_buffer_seconds=0,
        )

    assert EngineCooldowns(str(wt_root)).read_active(
        "claude-ds", now=1_800_000_000.0,
    ) == 1_800_001_200.0


@pytest.mark.parametrize("stage", ["reviewer", "implementer", "fixer"])
def test_harvest_stages_forward_the_tick_tuning(stage: str, tmp_path: Path) -> None:
    """Each HARVEST stage hands its configured values to the per-item harvest."""
    import tick  # type: ignore[import-untyped]

    tuning: dict[str, int] = {
        "reviewer": {"max_iterations": 2},
        "implementer": {"transient_cooldown_seconds": 900,
                        "transient_cooldown_reset_buffer_seconds": 30},
        "fixer": {"transient_cooldown_seconds": 900,
                  "transient_cooldown_reset_buffer_seconds": 30},
    }[stage]
    decision = {"action": "harvest", "pr_number": 7}

    with mock.patch("tick.reconcile_engine", return_value=([decision], True)), \
            mock.patch("tick._execute_reconcile_actions", return_value=True), \
            mock.patch(f"tick._harvest_one_{stage}") as harvest_one:
        harvest = getattr(tick, f"harvest_{stage}s")
        assert harvest(None, None, wt_root=str(tmp_path), **tuning) is True

    harvest_one.assert_called_once()
    for key, value in tuning.items():
        assert harvest_one.call_args.kwargs[key] == value


def test_harvest_one_fixer_posts_no_comment_on_transient_retry(
    tmp_path: Path,
) -> None:
    """A transient provider failure records nothing — there is no outcome yet."""
    from tick import Attempts, _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-8"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-8-quota",
        "engine": "claude",
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 8,
    }

    with mock.patch("tick.gh") as gh_mock:
        with mock.patch("tick._converge_pipeline_labels", return_value=True):
            with mock.patch("tick.cleanup_scratch"):
                _harvest_one_fixer(
                    decision, str(wt_root), tick_log=None,
                    attempts=Attempts(str(wt_root)),
                )

    comment_calls = [
        call for call in gh_mock.call_args_list
        if call.args[:2] == ("pr", "comment")
    ]
    assert comment_calls == []
    assert list((wt_root / ".receipts").glob("*")) == []


def test_harvest_one_fixer_ignores_branch_controlled_receipt(
    tmp_path: Path,
) -> None:
    """A file inside the worktree cannot forge the "comment landed" proof.

    ``wt_dir`` is a checked-out worktree of the PR branch, so its contents are
    author-controlled. A receipt kept there would let any tracked file named
    ``fix-comment.posted`` suppress the comment while the label still moved.
    """
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-12-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))
    # Branch content masquerading as a controller receipt.
    (wt_dir / "fix-comment.posted").write_text("posted\n")

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    bodies: list[str] = []
    with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
        with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
            with mock.patch("tick._converge_pipeline_labels", return_value=True):
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )

    assert len(bodies) == 1, "branch content must not suppress the post"


def test_harvest_one_fixer_reposts_for_a_later_dispatch(tmp_path: Path) -> None:
    """A receipt binds to one dispatch — the next fixer run posts its own comment."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-12"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )

    def write_marker(dispatch_id: str) -> None:
        (wt_dir / "dispatch.json").write_text(json.dumps({
            "pre_label": "ai:changes-requested",
            "dispatch_id": dispatch_id,
            "engine": "claude",
            "head_sha": "d" * 40,
        }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 12,
    }

    bodies: list[str] = []
    write_marker("fix-12-1")
    with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
        with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
            with mock.patch("tick._converge_pipeline_labels", return_value=True):
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )
            assert len(bodies) == 1

            write_marker("fix-12-2")
            with mock.patch("tick._converge_pipeline_labels", return_value=True):
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )

    assert len(bodies) == 2, "a new dispatch must record its own run"


def test_harvest_one_fixer_cap_escalation_posts_superseding_comment(
    tmp_path: Path,
) -> None:
    """A capped escalation states why the terminal label differs from the post.

    The run's own comment already said ``recheck`` → ``ai:revised``. When the
    relabel keeps failing and the attempt cap forces
    ``ai:needs-human-decision``, the PR must not be left with a comment that
    contradicts its label.
    """
    from tick import Attempts, _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-7-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    attempts = Attempts(str(wt_root))
    for _ in range(3):
        attempts.incr(7)

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    bodies: list[str] = []
    with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
        with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
            with mock.patch(
                "tick._converge_pipeline_labels", side_effect=[False, True],
            ) as converge_mock:
                _harvest_one_fixer(
                    decision, str(wt_root), tick_log=None, attempts=attempts,
                )

    assert converge_mock.call_args_list[-1].args[:3] == (
        "pr", 7, "ai:needs-human-decision",
    )
    assert len(bodies) == 2, "the capped escalation must publish its own reason"
    assert "ai:needs-human-decision" in bodies[1]
    assert "supersedes" in bodies[1]
    # The cap fires on the failure *after* the counter reaches the cap, so this
    # harvest is the 4th consecutive failed side effect. Reporting the cap
    # constant instead would understate the count by one.
    assert "4 times in a row" in bodies[1]
    assert "Attempts: 4" in bodies[1]


def test_harvest_one_fixer_resumes_the_cap_after_a_failed_terminal_relabel(
    tmp_path: Path,
) -> None:
    """A failed cap relabel is resumed, not silently replaced by the ordinary one.

    The cap publishes a comment saying ``ai:needs-human-decision`` supersedes
    the run's outcome. If its relabel fails and the next tick simply re-enters
    ordinary harvest logic, a recovered relabel lands ``ai:revised`` under that
    comment and clears the counter — the pipeline would then claim two
    contradictory terminal states for one run.
    """
    from tick import Attempts, _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-7-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    attempts = Attempts(str(wt_root))
    for _ in range(3):
        attempts.incr(7)

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    bodies: list[str] = []
    # The cap's relabel never landed, so the PR still carries the fixer's own
    # in-flight label — the state that proves the transition is still pending
    # and the resume may converge.
    with mock.patch(
        "tick.gh_json",
        side_effect=gh_json_head_and_labels("a" * 40, ["ai:fixing"]),
    ):
        with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
            # First tick: the ordinary relabel fails, the cap fires, and the
            # cap's own terminal relabel fails too.
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=False,
            ):
                _harvest_one_fixer(
                    decision, str(wt_root), tick_log=None, attempts=attempts,
                )

            # Second tick: relabels succeed again.
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=attempts,
                    )

    assert converge_mock.call_args_list[0].args[:3] == (
        "pr", 7, "ai:needs-human-decision",
    ), "the resumed tick must land the cap's terminal label, not ai:revised"
    assert not any(
        call.args[2] == "ai:revised" for call in converge_mock.call_args_list
    )
    assert attempts.read(7) == 0, "the terminal transition clears the counter"
    # One fixer-run comment and one cap escalation across both ticks: the
    # resumed transition republishes nothing, because the counter it reports is
    # unchanged and its receipt therefore still matches.
    assert len(bodies) == 2
    assert "supersedes" in bodies[1]


def test_cap_finalize_failure_resumes_the_cap_next_tick_not_held_replay(
    tmp_path: Path,
) -> None:
    """A failed archive move keeps the cap's intent so the next tick resumes it.

    If the intent and receipts were cleared before the final archive move, a
    failed move would leave the marker and result beside the pipeline's own
    ``ai:needs-human-decision``. The next tick would then enter the generic
    hold branch and publish a second comment falsely attributing that terminal
    label to a workflow hold — contradicting the already-posted cap comment.
    """
    from pipeline import (  # type: ignore[attr-defined]
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _bound_failed_harvest,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "rev-7-1", "stage": "review", "pr_number": 7,
        "engine": "claude",
    }))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))
    bodies: list[str] = []

    # Tick 1: the cap fires, its comment posts, its relabel lands — and the
    # final archive move fails.
    with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
        with mock.patch("tick._converge_pipeline_labels", return_value=True):
            with mock.patch("tick._finalize_terminal_archive", return_value=False):
                _bound_failed_harvest(
                    7, "pr", "ai:reviewing", str(wt_dir), attempts,
                    tick_log, "review",
                )

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    assert intent_path.is_file(), "the intent must survive the failed move"
    assert (wt_dir / "review.json").is_file()

    # Tick 2: reconciliation resumes the cap before the generic hold branch.
    # The intent records the landed relabel, so the resume skips convergence.
    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
        with mock.patch("tick._finalize_terminal_archive", return_value=True):
                decision = _reconcile_one(
                    7, str(wt_dir), config, tick_log, attempts=attempts,
                )

    assert decision["action"] == "noop"
    assert not intent_path.exists(), "the resumed transition retires its intent"
    assert attempts.read(7) == 0
    assert len(bodies) == 1, "the resumed transition must not republish"
    assert "supersedes" in bodies[0]
    assert not [
        entry for entry in tick_log.tail()
        if entry.get("action") == "held_result_published"
    ]


def test_terminal_resume_completes_local_custody_when_relabel_already_landed(
    tmp_path: Path,
) -> None:
    """A crash after the terminal relabel lands must not deadlock the resume.

    The probe shape: tick 1 converged the item to ``ai:needs-human-decision``
    and died before the final archive move. Tick 2 resumes the cap with the
    real convergence helper, and the remote already sits exactly at the hold
    target — a state convergence deliberately refuses to claim as automation's
    own mutation (``human_hold_retained``). The resume must recognise the
    landed relabel from the remote state, skip convergence, and finish local
    custody only: finalize the archive, clear the attempts, retire the intent,
    and republish nothing.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
        render_cap_escalation_comment,
    )
    from tick import (  # type: ignore[import-untyped]
        _CAP_COMMENT_KIND,
        Attempts,
        TickLog,
        _comment_receipt_path,
        _decision_key,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    # Tick 1's relabel landed and the tick died before the archive move: the
    # intent carries no phase and the remote sits exactly at the terminal hold.
    body = render_cap_escalation_comment(stage="review", failures=4)
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": 4,
    }))
    receipt_path = Path(
        _comment_receipt_path(str(wt_root), "pr", 7, _CAP_COMMENT_KIND),
    )
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "decision_key": _decision_key(LABEL_NEEDS_HUMAN, body),
        "comment_url": "posted",
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, _calls = terminal_gh_mock([LABEL_NEEDS_HUMAN], bodies)
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            finalize.return_value = True
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert finalize.call_count == 1, "the resume must finish the archive move"
    assert not intent_path.exists(), "the resumed transition retires its intent"
    assert attempts.read(7) == 0
    assert bodies == [], "the landed relabel must not republish"


def test_terminal_resume_without_a_phase_never_converges_over_a_human_transition(
    tmp_path: Path,
) -> None:
    """The decisive crash window: no phase record, remote at a third label.

    The phase marker is written after the relabel and is best effort, so a
    crash (or a failed write) in that window leaves an intent with no phase.
    If an operator then moves the item to a non-hold label, the remote sits at
    neither the transition's source (``ai:reviewing``) nor its target
    (``ai:needs-human-decision``). Convergence must be refused: it would strip
    the operator's ``ai:revised`` and re-apply the superseded terminal label.
    Local custody still finishes, so the item is never deadlocked.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        LABEL_REVISED,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
        render_cap_escalation_comment,
    )
    from tick import (  # type: ignore[import-untyped]
        _CAP_COMMENT_KIND,
        Attempts,
        TickLog,
        _comment_receipt_path,
        _decision_key,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    body = render_cap_escalation_comment(stage="review", failures=4)
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": 4,
    }))
    receipt_path = Path(
        _comment_receipt_path(str(wt_root), "pr", 7, _CAP_COMMENT_KIND),
    )
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "decision_key": _decision_key(LABEL_NEEDS_HUMAN, body),
        "comment_url": "posted",
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls = terminal_gh_mock([LABEL_REVISED], bodies)
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            finalize.return_value = True
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert not [call for call in calls if call[:2] == ("pr", "edit")], (
        "an ambiguous post-mutation state must never be converged"
    )
    assert finalize.call_count == 1, "the resume must still finish local custody"
    assert not intent_path.exists(), "the resumed transition retires its intent"
    assert attempts.read(7) == 0
    assert bodies == [], "the resume must not republish"
    assert [
        entry for entry in read_tick_log_entries(tmp_path / "logs")
        if entry.get("action") == "terminal_relabel_ambiguous"
    ], "the refusal is operator-visible"


@pytest.mark.parametrize("hold_label", ["ai:blocked", "ai:needs-human-decision"])
def test_terminal_resume_under_a_hold_releases_instead_of_pinning_the_slot(
    tmp_path: Path, hold_label: str,
) -> None:
    """A hold beside a pending transition must not pin an admission slot.

    ``_converge_pipeline_labels`` refuses to mutate under a hold by design, so
    a transition still carrying its stage's in-flight label can never converge
    while one is present. Re-attempting it every tick leaves ``ai:reviewing``
    on the item forever — one of max_in_flight slots consumed with no engine
    behind it — and the resume runs ahead of the hold branch that used to
    release it. The hold owns the label, so the pipeline drops only its own
    in-flight receipt and finishes local custody.

    Parametrised across both holds: ``ai:needs-human-decision`` happens to be
    the transition's own target, so the release lands the item exactly where
    the transition wanted it; ``ai:blocked`` is a hold the transition may not
    replace. Neither may be mutated.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": MAX_DISPATCH_ATTEMPTS + 1,
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls, state = label_state_gh_mock(
        ["ai:reviewing", hold_label], bodies,
    )
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch(
            "tick._finalize_terminal_archive", return_value=True,
        ) as finalize:
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert state == [hold_label], (
        "the pipeline's own in-flight label is released; the hold is untouched"
    )
    assert [call for call in calls if call[:2] == ("pr", "edit")] == [
        ("pr", "edit", "7", "--remove-label", "ai:reviewing"),
    ], "a pure removal, never a converge that would replace the human's hold"
    finalize.assert_called_once()
    assert not intent_path.exists(), "the resumed transition retires its intent"
    assert attempts.read(7) == 0
    assert len(bodies) == 1, "the cap explanation is published exactly once"
    assert [
        entry for entry in read_tick_log_entries(tmp_path / "logs")
        if entry.get("action") == "terminal_relabel_held"
    ], "the released transition is operator-visible"


@pytest.mark.parametrize(
    ("kind", "payload", "stage", "in_flight_label"),
    [
        ("cap", {"failures": 4}, "review", "ai:reviewing"),
        (
            "transient_exhausted",
            {"failure_class": "quota_or_limit", "attempt": 3},
            "implement",
            "ai:implementing",
        ),
        (
            "escalate",
            {"harvest_label": "ai:needs-human-decision", "comment_body": "blocked"},
            "fix",
            "ai:fixing",
        ),
    ],
)
def test_every_terminal_kind_releases_its_in_flight_label_under_a_hold(
    tmp_path: Path,
    kind: str,
    payload: dict[str, object],
    stage: str,
    in_flight_label: str,
) -> None:
    """All three terminal kinds share the held-release boundary.

    The attempt cap, transient exhaustion, and the result-driven stage
    escalation all converge through the same gate, so all three inherit the
    same impossibility under a hold. A release that landed on only one of them
    would leave the other two pinning an admission slot — the sibling gap this
    parametrisation exists to close.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_BLOCKED,
        LABEL_NEEDS_HUMAN,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    entity = "issue" if stage == "implement" else "pr"
    prefix = "issue" if stage == "implement" else "pr"
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / f"{prefix}-7"
    wt_dir.mkdir()
    result_file = "review.json" if stage == "review" else "result.json"
    (wt_dir / result_file).write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "d-7-1"}))
    attempts = Attempts(str(wt_root))
    attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    intent_path = Path(_terminal_intent_path(str(wt_root), entity, 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "d-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": stage,
        "kind": kind,
        **payload,
    }))

    config = StageConfig(
        stage=stage, worktree_prefix=prefix,  # type: ignore[arg-type]
        attempts_key_prefix="" if prefix == "pr" else "issue-",
        result_file=result_file, in_flight_label=in_flight_label,
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls, state = label_state_gh_mock(
        [LABEL_BLOCKED, in_flight_label], bodies, entity=entity,
    )
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive", return_value=True):
            _reconcile_one(7, str(wt_dir), config, tick_log, attempts=attempts)

    assert state == [LABEL_BLOCKED], (
        f"{kind} must release its in-flight label rather than pin the slot"
    )
    assert [call for call in calls if call[:2] == (entity, "edit")] == [
        (entity, "edit", "7", "--remove-label", in_flight_label),
    ]
    assert not intent_path.exists(), "the released transition retires its intent"
    assert attempts.read(7) == 0


def test_held_release_survives_a_failed_archive_move_without_repeating(
    tmp_path: Path,
) -> None:
    """The crash window between the held release and the final archive move.

    The release removes the source label, so the state that classified as
    ``held`` no longer exists on the retry. The next tick must recognise that
    from the live labels alone — no phase is recorded for a release, because
    there was no relabel to record — and finish local custody without a second
    removal or a second comment.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_BLOCKED,
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": MAX_DISPATCH_ATTEMPTS + 1,
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls, state = label_state_gh_mock(
        [LABEL_BLOCKED, "ai:reviewing"], bodies,
    )

    # Tick 1: the release lands, the archive move fails.
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive", return_value=False):
            _reconcile_one(7, str(wt_dir), config, tick_log, attempts=attempts)

    assert state == [LABEL_BLOCKED], "the release landed"
    assert intent_path.is_file(), "the intent outlives the failed archive move"

    # Tick 2: the same transition finishes from a label set that no longer
    # carries its source.
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch(
            "tick._finalize_terminal_archive", return_value=True,
        ) as finalize:
            _reconcile_one(7, str(wt_dir), config, tick_log, attempts=attempts)

    assert finalize.call_count == 1
    assert not intent_path.exists(), "the retry retires the intent"
    assert state == [LABEL_BLOCKED], "the hold is still the item's only label"
    assert [call for call in calls if call[:2] == ("pr", "edit")] == [
        ("pr", "edit", "7", "--remove-label", "ai:reviewing"),
    ], "the retry re-attempts no mutation at all"
    assert len(bodies) == 1, "the receipt suppresses the second publication"


def test_held_terminal_release_is_restored_when_the_hold_departs_after_it_lands(
    tmp_path: Path,
) -> None:
    """The third release site owes the same post-release restore as the others.

    A terminal transition a hold has taken the label of releases the stage's
    in-flight label and then finalizes its archive. The release's own read-back
    proves the hold outlived the removal, not that it outlives the archive move
    that follows — and once that move lands, the marker is gone. An operator
    lifting the hold inside that window leaves the item with no workflow label,
    no marker, and a live comment naming a terminal label it never received.
    The transition must put its own label back and retain custody, which also
    lets the next tick converge properly now that the hold is gone.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_BLOCKED,
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": MAX_DISPATCH_ATTEMPTS + 1,
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls, state = label_state_gh_mock(
        [LABEL_BLOCKED, "ai:reviewing"], bodies,
    )
    views = 0

    def hold_departs_after_the_release_confirms(
        *args: str, **kwargs: object,
    ) -> object:
        nonlocal views
        result = gh_mock(*args, **kwargs)
        if args[:2] == ("pr", "view") and "labels" in args:
            views += 1
            # Reads so far: the resume's classification, the release's
            # pre-check, and the release's own confirmation. The operator lifts
            # the hold immediately after that confirmation returns, so the
            # release itself sees nothing wrong and returns success.
            if views == 3:
                state.remove(LABEL_BLOCKED)
        return result

    with mock.patch("tick.gh", side_effect=hold_departs_after_the_release_confirms):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert state == ["ai:reviewing"], (
        "the released in-flight label is put back when its hold vanishes"
    )
    assert ("pr", "edit", "7", "--add-label", "ai:reviewing") in calls
    finalize.assert_not_called()
    assert intent_path.is_file(), "the intent outlives the refusal"
    assert (wt_dir / "dispatch.json").is_file(), "the marker outlives it too"
    assert attempts.read(7) == MAX_DISPATCH_ATTEMPTS, "attempts stay retained"
    assert [
        entry for entry in read_tick_log_entries(tmp_path / "logs")
        if entry.get("action") == "label_release_failed"
        and isinstance(entry.get("details"), dict)
        and entry["details"].get("in_flight_restored") is True
    ], "the restoration is operator-visible"


def test_terminal_resume_never_reapplies_terminal_label_over_human_transition(
    tmp_path: Path,
) -> None:
    """A human label change after our relabel is never overwritten by resume.

    The intent records that the relabel landed (phase ``relabeled``). Between
    ticks an operator moves the item off the terminal hold. The resume must
    finish local custody without converging — convergence would remove
    ``ai:ready-for-human`` and re-apply ``ai:needs-human-decision`` over the
    operator's later transition, the authority violation this revision exists
    to prevent.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        LABEL_READY_FOR_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
        render_cap_escalation_comment,
    )
    from tick import (  # type: ignore[import-untyped]
        _CAP_COMMENT_KIND,
        Attempts,
        TickLog,
        _comment_receipt_path,
        _decision_key,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    # The intent records the landed relabel; the operator has since moved the
    # item to ai:ready-for-human, so the remote no longer matches the target.
    body = render_cap_escalation_comment(stage="review", failures=4)
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": 4,
        "phase": "relabeled",
    }))
    receipt_path = Path(
        _comment_receipt_path(str(wt_root), "pr", 7, _CAP_COMMENT_KIND),
    )
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "decision_key": _decision_key(LABEL_NEEDS_HUMAN, body),
        "comment_url": "posted",
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls = terminal_gh_mock([LABEL_READY_FOR_HUMAN], bodies)
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            finalize.return_value = True
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert finalize.call_count == 1, "the resume must still finish local custody"
    assert not intent_path.exists(), "the resumed transition retires its intent"
    assert attempts.read(7) == 0
    assert bodies == [], "the landed relabel must not republish"
    assert not [call for call in calls if call[:2] == ("pr", "edit")], (
        "the resume must not re-apply the terminal label over a human transition"
    )


def test_held_terminal_resume_retains_custody_when_its_release_fails(
    tmp_path: Path,
) -> None:
    """The held release is a gate like any other remote mutation.

    The reviewer corner: a human adds a hold after the cap intent was recorded
    but before the transition converged, so the resume releases the in-flight
    label instead of converging. That release is the transition's one remote
    mutation, so a failed one retains the marker, the in-flight label, the
    attempts, and the intent — and the retry completes it. Nothing about being
    held makes the mutation best-effort.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_BLOCKED,
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
        render_cap_escalation_comment,
    )
    from tick import (  # type: ignore[import-untyped]
        _CAP_COMMENT_KIND,
        Attempts,
        TickLog,
        _comment_receipt_path,
        _decision_key,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    # Tick 1 recorded the cap intent and posted its comment; a human then
    # added a hold beside the in-flight label before convergence could land.
    body = render_cap_escalation_comment(stage="review", failures=4)
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": 4,
    }))
    receipt_path = Path(
        _comment_receipt_path(str(wt_root), "pr", 7, _CAP_COMMENT_KIND),
    )
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "decision_key": _decision_key(LABEL_NEEDS_HUMAN, body),
        "comment_url": "posted",
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []

    # Tick 1: the release is attempted and the edit fails, so the item noops
    # with every source of custody retained.
    gh_mock, calls, state = label_state_gh_mock(
        [LABEL_BLOCKED, "ai:reviewing"], bodies,
    )

    def failing_release(*args: str, **kwargs: object) -> object:
        if args[:2] == ("pr", "edit"):
            calls.append(args)
            raise subprocess.CalledProcessError(1, args)
        return gh_mock(*args, **kwargs)

    with mock.patch("tick.gh", side_effect=failing_release):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert state == [LABEL_BLOCKED, "ai:reviewing"], "nothing moved remotely"
    assert intent_path.is_file(), "the intent stays pending after a failed release"
    assert attempts.read(7) == MAX_DISPATCH_ATTEMPTS, "attempts stay retained"
    assert (wt_dir / "dispatch.json").is_file(), "the marker outlives the failure"
    finalize.assert_not_called()
    assert bodies == [], "the held resume must not republish"

    # Tick 2: the release lands, the hold is left alone, and the transition
    # finishes its local custody.
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive") as finalize2:
            finalize2.return_value = True
            decision2 = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision2["action"] == "noop"
    assert state == [LABEL_BLOCKED], "only the pipeline's own label is released"
    finalize2.assert_called_once()
    assert not intent_path.exists(), "the resumed transition retires its intent"
    assert attempts.read(7) == 0
    assert bodies == [], "the resumed transition must not republish"
    assert [call for call in calls if call[:2] == ("pr", "edit")] == [
        ("pr", "edit", "7", "--remove-label", "ai:reviewing"),
        ("pr", "edit", "7", "--remove-label", "ai:reviewing"),
    ], "the retry re-attempts the same pure removal, never a converge"


def test_terminal_resume_completes_a_pre_upgrade_cap_intent(
    tmp_path: Path,
) -> None:
    """The one intent shape written before this schema must still resume.

    The previous writer recorded ``{dispatch_id, label, stage}`` and the cap
    was the only transition it produced. Such a record can be on disk when the
    upgrade lands mid-transition, and it is the recovery owner for a cap
    comment already live on the PR. Rejecting it as malformed would retain
    custody forever and pin the very item it exists to release: no fresh
    harvest ever runs, so nothing else can retire it.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
        render_cap_escalation_comment,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls = terminal_gh_mock(["ai:reviewing"], bodies)
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            finalize.return_value = True
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert [call for call in calls if call[:2] == ("pr", "edit")], (
        "the item still sits at its in-flight label, so the relabel must run"
    )
    finalize.assert_called_once()
    assert not intent_path.exists(), "the resumed transition retires its intent"
    assert attempts.read(7) == 0
    # The count is absent from the record, so the comment derives it from the
    # counter the transition has not cleared yet — the same body the
    # interrupted run posted, which is what the receipt suppresses.
    assert bodies == [
        render_cap_escalation_comment(
            stage="review", failures=MAX_DISPATCH_ATTEMPTS + 1,
        ),
    ]


def test_pre_upgrade_cap_intent_survives_its_own_phase_write(
    tmp_path: Path,
) -> None:
    """The resume rewrites the record it read, so it must still read it back.

    A pre-upgrade intent that reaches the converge gets ``phase`` written into
    it before the archive move. If that move then fails, the next tick reads a
    record the *first* tick created rather than the one the operator had.
    Rejecting it there would strand the item for good — the failure accepting
    the old shape exists to prevent, reintroduced one step later. The count is
    pinned in the same pass, because the completion derives it from a counter
    it clears before the move: a re-entry that re-derived it would publish a
    second cap comment reporting a failure count that never happened.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls = terminal_gh_mock(["ai:reviewing"], bodies)

    # Tick 1: the relabel lands, the phase is recorded, the archive move fails.
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch(
            "tick._finalize_terminal_archive", return_value=False,
        ):
            _reconcile_one(7, str(wt_dir), config, tick_log, attempts=attempts)

    recorded = json.loads(intent_path.read_text())
    assert recorded["phase"] == "relabeled", (
        "the production write is what changes the record's shape"
    )
    assert recorded["kind"] == "cap", "first sight upgrades the record"
    assert recorded["failures"] == MAX_DISPATCH_ATTEMPTS + 1, (
        "the count is pinned while the counter that produced it still holds it"
    )
    edits_after_first = len([c for c in calls if c[:2] == ("pr", "edit")])

    # Tick 2: the same record, now four keys, must still resume and finish.
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch(
            "tick._finalize_terminal_archive", return_value=True,
        ) as finalize:
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    finalize.assert_called_once()
    assert not intent_path.exists(), "the resumed transition retires its intent"
    assert len([c for c in calls if c[:2] == ("pr", "edit")]) == (
        edits_after_first
    ), "the recorded phase means the relabel is never re-attempted"
    assert len(bodies) == 1, "the receipt suppresses the second publication"
    assert not [
        entry for entry in read_tick_log_entries(tmp_path / "logs")
        if entry.get("action") == "terminal_intent_unresolved"
    ], "a record this code wrote is never its own unresolvable state"


def test_pre_upgrade_intent_parses_with_a_phase_the_upgrade_did_not_reach() -> None:
    """The shape tolerance stands on its own, not on the upgrade succeeding.

    The upgrade write and the phase write are separate, best-effort writes: the
    first can fail while the second lands, leaving a legacy record carrying a
    phase and nothing else new. The parser accepts that shape directly, so the
    strand cannot come back through a partially applied upgrade.
    """
    from pipeline import LABEL_NEEDS_HUMAN  # type: ignore[attr-defined]
    from tick import _parse_terminal_intent  # type: ignore[import-untyped]

    intent = _parse_terminal_intent({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "phase": "relabeled",
    })

    assert intent is not None
    assert intent.kind == "cap"
    assert intent.relabeled is True, "the recorded phase still suppresses the relabel"
    assert intent.failures is None, "an unpinned count is still derived"


@pytest.mark.parametrize(
    ("label", "record"),
    [
        ("an unknown kind", {"kind": "reboot"}),
        ("a kind-less record carrying a payload", {"kind": None}),
        ("a non-string stage", {"stage": 7}),
        ("an unknown stage", {"stage": "deploy"}),
        ("a non-integer cap count", {"failures": "four"}),
        ("a missing cap count", {"failures": None}),
        ("a forged phase", {"phase": "archived"}),
        ("a swapped terminal label", {"label": "ai:revised"}),
    ],
)
def test_terminal_resume_fails_closed_on_a_malformed_intent(
    tmp_path: Path, label: str, record: dict[str, object],
) -> None:
    """Controller state that is not a valid typed record executes nothing.

    An intent is the recovery owner for a transition whose comment is already
    live on GitHub. If a field is missing, mistyped, or carries a value outside
    its closed set, the recorded decision cannot be reproduced — defaulting to
    the cap path would publish and relabel on a guess. Every malformed shape
    must retain custody, mutate nothing, and stay on disk for inspection.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "rev-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    intent: dict[str, object] = {
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": 4,
    }
    for key, value in record.items():
        if value is None:
            intent.pop(key)
        else:
            intent[key] = value
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps(intent))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls = terminal_gh_mock(["ai:reviewing"], bodies)
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop", label
    assert intent_path.is_file(), f"{label} must not be retired"
    assert bodies == [], f"{label} must publish nothing"
    assert not [call for call in calls if call[:2] == ("pr", "edit")], (
        f"{label} must mutate no label"
    )
    finalize.assert_not_called()
    assert attempts.read(7) == MAX_DISPATCH_ATTEMPTS, "attempts stay retained"
    assert [
        entry for entry in read_tick_log_entries(tmp_path / "logs")
        if entry.get("action") == "terminal_intent_unresolved"
    ], f"{label} is operator-visible"


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("no dispatch id", {"dispatch_id": ""}),
        ("an unknown stage", {"stage": "deploy"}),
        ("an unknown kind", {"kind": "reboot"}),
        ("a cap without its count", {"failures": None}),
        (
            "an escalation to a workflow label",
            {"kind": "escalate", "failures": None,
             "harvest_label": "ai:revised", "comment_body": ""},
        ),
    ],
)
def test_record_terminal_intent_refuses_a_record_the_resume_would_reject(
    tmp_path: Path, label: str, kwargs: dict[str, object],
) -> None:
    """The writer and the reader agree on the schema by construction.

    A record that fails validation is not durable at all: the transition it
    describes could never be resumed, so writing one would trade a gate the
    caller already handles for a transition that can never complete. Refusing
    the write routes it through the caller's existing ``terminal_intent_failed``
    path instead.
    """
    from tick import (  # type: ignore[import-untyped]
        _record_terminal_intent,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    call: dict[str, object] = {
        "dispatch_id": "rev-7-1", "stage": "review",
        "kind": "cap", "failures": 4,
    }
    for key, value in kwargs.items():
        if value is None:
            call.pop(key, None)
        else:
            call[key] = value
    recorded = _record_terminal_intent(
        str(wt_root), "pr", 7,
        str(call.pop("dispatch_id")), str(call.pop("stage")),
        kind=str(call.pop("kind")), **call,
    )

    assert recorded is False, label
    assert not Path(_terminal_intent_path(str(wt_root), "pr", 7)).exists(), (
        f"{label} must leave no unusable record behind"
    )


def test_terminal_resume_rejects_a_persisted_label_outside_the_terminal_set(
    tmp_path: Path,
) -> None:
    """A stage escalation can only ever converge to a terminal label.

    ``harvest_label`` is read back off disk and handed straight to remote
    convergence. If any persisted string were accepted, a corrupted or forged
    record could drive the pipeline to re-apply a workflow label — here
    ``ai:revised``, which would re-enter the review loop under an escalation
    comment.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        LABEL_REVISED,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(json.dumps({"result": "BLOCKED"}))
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-7-1"}))
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    # Every field but the converge target is well formed, so only the
    # harvest_label check can reject this record.
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "fix-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "fix",
        "kind": "escalate",
        "harvest_label": LABEL_REVISED,
        "comment_body": "",
    }))

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    bodies: list[str] = []
    gh_mock, calls = terminal_gh_mock(["ai:fixing"], bodies)
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert intent_path.is_file(), "the rejected intent stays for inspection"
    assert not [call for call in calls if call[:2] == ("pr", "edit")], (
        "a non-terminal persisted label must never reach convergence"
    )
    finalize.assert_not_called()
    assert bodies == []


def test_terminal_resume_retains_the_intent_when_the_marker_is_unreadable(
    tmp_path: Path,
) -> None:
    """An unprovable dispatch id must not read as a proven re-dispatch.

    The intent is retired when it names a *different* dispatch — the item was
    re-dispatched after being capped. The marker read that proves this falls
    back to ``"unknown"``, so a transient read failure was being treated as
    proof of a different dispatch and destroyed the recovery owner for a
    transition whose comment is already live.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        MAX_DISPATCH_ATTEMPTS,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text(json.dumps({"verdict": "APPROVE"}))
    # The marker is present but unparseable: the dispatch id cannot be proven
    # either equal to or different from the one the intent records.
    (wt_dir / "dispatch.json").write_text("{ truncated")
    attempts = Attempts(str(wt_root))
    for _ in range(MAX_DISPATCH_ATTEMPTS):
        attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir()
    intent_path.write_text(json.dumps({
        "dispatch_id": "rev-7-1",
        "label": LABEL_NEEDS_HUMAN,
        "stage": "review",
        "kind": "cap",
        "failures": 4,
    }))

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    bodies: list[str] = []
    gh_mock, calls = terminal_gh_mock(["ai:reviewing"], bodies)
    with mock.patch("tick.gh", side_effect=gh_mock):
        with mock.patch("tick._finalize_terminal_archive") as finalize:
            decision = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision["action"] == "noop"
    assert intent_path.is_file(), "an unprovable marker must not retire it"
    assert not [call for call in calls if call[:2] == ("pr", "edit")]
    finalize.assert_not_called()
    assert bodies == []
    assert attempts.read(7) == MAX_DISPATCH_ATTEMPTS


def test_transient_exhaustion_aborts_before_remote_mutation_when_intent_write_fails(
    tmp_path: Path,
) -> None:
    """A terminal intent that cannot be durably recorded gates the transition.

    If the intent write failed and the transition proceeded anyway, a later
    archive-move failure would leave the next tick with no record to resume —
    the generic hold branch would then republish the completed outcome as a
    held completion, contradicting the already-posted escalation. The gate
    cuts the chain at the first link: no comment, no converge, no archive move
    attempt, scratch retained for an ordinary retry.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _handle_transient_engine_result,
        _terminal_intent_path,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-7-1"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root))
    attempts.incr(7)
    attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))
    bodies: list[str] = []

    # A directory squats on the intent path, so the atomic intent write fails
    # with an OSError (IsADirectoryError on the replace).
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.mkdir(parents=True)

    with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
        with mock.patch("tick._converge_pipeline_labels") as converge:
            with mock.patch("tick._finalize_terminal_archive") as finalize:
                handled = _handle_transient_engine_result(
                    entity="pr",
                    number=7,
                    config=config,
                    pre_label="ai:changes-requested",
                    engine="claude",
                    result_raw=result_raw,
                    harvest_result={"failure_class": "quota_or_limit"},
                    dispatch_id="fix-7-1",
                    wt_root=str(wt_root),
                    wt_dir=str(wt_dir),
                    tick_log=tick_log,
                    attempts=attempts,
                )

    assert handled is False
    converge.assert_not_called(), (
        "the transition must not start without its durable intent"
    )
    finalize.assert_not_called(), (
        "a gated transition must not reach the archive move"
    )
    assert bodies == [], (
        "no escalation comment may land without a durable intent"
    )
    assert (wt_dir / "result.json").is_file(), "scratch custody is retained"


def test_transient_exhaustion_finalize_failure_resumes_next_tick_quietly(
    tmp_path: Path,
) -> None:
    """A failed archive move after exhaustion resumes the same transition.

    The exhaustion already published its escalation. If its receipt were
    discarded before the final move, a failed move would let the next tick's
    generic hold branch publish the completed result as a held completion —
    a second comment contradicting the exhaustion outcome.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _handle_transient_engine_result,
        _reconcile_one,
        _terminal_intent_path,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": "fix-7-1"}))
    result_raw = json.dumps({"result": "You've hit your session limit"})
    (wt_dir / "result.json").write_text(result_raw)
    attempts = Attempts(str(wt_root))
    attempts.incr(7)
    attempts.incr(7)
    tick_log = TickLog.open(str(tmp_path / "logs"))
    bodies: list[str] = []

    # Tick 1: exhaustion posts, converges, and fails the final archive move.
    with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
        with mock.patch("tick._converge_pipeline_labels", return_value=True):
            with mock.patch("tick._finalize_terminal_archive", return_value=False):
                handled = _handle_transient_engine_result(
                    entity="pr",
                    number=7,
                    config=config,
                    pre_label="ai:changes-requested",
                    engine="claude",
                    result_raw=result_raw,
                    harvest_result={"failure_class": "quota_or_limit"},
                    dispatch_id="fix-7-1",
                    wt_root=str(wt_root),
                    wt_dir=str(wt_dir),
                    tick_log=tick_log,
                    attempts=attempts,
                )

    assert handled is False
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    assert intent_path.is_file()
    intent = json.loads(intent_path.read_text())
    assert intent.get("kind") == "transient_exhausted"
    assert (wt_dir / "result.json").is_file()

    # Tick 2: reconciliation resumes the exhaustion, not a held replay. The
    # intent records the landed relabel, so the resume skips convergence.
    with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
        with mock.patch("tick._finalize_terminal_archive", return_value=True):
                decision = _reconcile_one(
                    7, str(wt_dir), config, tick_log, attempts=attempts,
                )

    assert decision["action"] == "noop"
    assert not intent_path.exists()
    assert attempts.read(7) == 0
    assert len(bodies) == 1, "the resumed transition must not republish"
    assert not [
        entry for entry in tick_log.tail()
        if entry.get("action") == "held_result_published"
    ]
    evidence_dirs = [
        path for path in (wt_root / ".harvest-evidence").iterdir()
        if path.is_dir() and path.name.startswith("pr-7-")
    ]
    assert len(evidence_dirs) == 1, (
        "the resumed transition must reuse the prepared archive, not "
        "accumulate one evidence copy per retry"
    )


def test_implementer_escalate_finalize_failure_resumes_next_tick_not_held_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed archive move after an implementer escalate resumes it next tick.

    Replaying the published escalation as a held completion would attribute
    the pipeline's own terminal label to a workflow hold that was never there.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_one_implementer,
        _reconcile_one,
        _terminal_intent_path,
    )

    monkeypatch.setattr("tick.gh", allow_escalation_comment())
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "NEEDS_HUMAN_DECISION", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:available",
        "dispatch_id": "impl-7-1",
        "engine": "claude",
    }))
    attempts = Attempts(str(wt_root), "issue-")
    tick_log = TickLog.open(str(tmp_path / "logs"))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    # Tick 1: the escalate's relabel lands, the final archive move fails.
    with mock.patch("tick.pipeline.harvest_implementation") as harvest_impl:
        harvest_impl.return_value = {
            "action": "escalate",
            "label": LABEL_NEEDS_HUMAN,
            "sentinel": "NEEDS_HUMAN_DECISION",
        }
        with mock.patch("tick._converge_pipeline_labels", return_value=True):
            with mock.patch("tick._finalize_terminal_archive", return_value=False):
                _harvest_one_implementer(
                    decision, str(wt_root),
                    tick_log=tick_log, attempts=attempts,
                )

    intent_path = Path(_terminal_intent_path(str(wt_root), "issue", 7))
    assert intent_path.is_file()
    intent = json.loads(intent_path.read_text())
    assert intent.get("kind") == "escalate"
    assert (wt_dir / "result.json").is_file()

    # Tick 2: reconciliation resumes the escalate, not a held replay. The
    # intent records the landed relabel, so the resume skips convergence.
    config = StageConfig(
        stage="implement", worktree_prefix="issue",
        attempts_key_prefix="issue-", result_file="result.json",
        in_flight_label="ai:implementing", pre_label="ai:available",
        strict_dual_label=True, tick_abort_on_enum_failure=False,
    )
    with mock.patch("tick._finalize_terminal_archive", return_value=True):
            decision2 = _reconcile_one(
                7, str(wt_dir), config, tick_log, attempts=attempts,
            )

    assert decision2["action"] == "noop"
    assert not intent_path.exists()
    assert attempts.read(7) == 0
    assert not [
        entry for entry in tick_log.tail()
        if entry.get("action") == "held_result_published"
    ]


def test_fixer_escalate_finalize_failure_resumes_next_tick_not_held_replay(
    tmp_path: Path,
) -> None:
    """A failed archive move after a fixer escalate resumes it next tick.

    The fixer's escalation comment is already on the PR; replaying the
    completion as a held result would post a second comment contradicting the
    fixer outcome.
    """
    from pipeline import (  # type: ignore[attr-defined]
        LABEL_NEEDS_HUMAN,
        StageConfig,
    )
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_one_fixer,
        _reconcile_one,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "NEEDS_HUMAN_DECISION", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-7-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))
    attempts = Attempts(str(wt_root))
    tick_log = TickLog.open(str(tmp_path / "logs"))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    bodies: list[str] = []

    # Tick 1: the fixer comment posts, the escalate's relabel lands, and the
    # final archive move fails.
    with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
        harvest_fix.return_value = {
            "action": "escalate",
            "label": LABEL_NEEDS_HUMAN,
            "post_comment": True,
            "comment_body": "fixer escalated",
        }
        with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
            with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
                with mock.patch("tick._converge_pipeline_labels", return_value=True):
                    with mock.patch(
                        "tick._finalize_terminal_archive", return_value=False,
                    ):
                        _harvest_one_fixer(
                            decision, str(wt_root),
                            tick_log=tick_log, attempts=attempts,
                        )

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    assert intent_path.is_file()
    intent = json.loads(intent_path.read_text())
    assert intent.get("kind") == "escalate"
    assert intent.get("comment_body") == "fixer escalated"
    assert (wt_dir / "result.json").is_file()

    # Tick 2: reconciliation resumes the escalate, not a held replay. The
    # intent records the landed relabel, so the resume skips convergence.
    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=False,
    )
    with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
        with mock.patch("tick._finalize_terminal_archive", return_value=True):
                decision2 = _reconcile_one(
                    7, str(wt_dir), config, tick_log, attempts=attempts,
                )

    assert decision2["action"] == "noop"
    assert not intent_path.exists()
    assert attempts.read(7) == 0
    assert len(bodies) == 1, "the resumed transition must not republish"
    assert not [
        entry for entry in tick_log.tail()
        if entry.get("action") == "held_result_published"
    ]


def test_harvest_one_fixer_ignores_a_terminal_intent_from_an_earlier_dispatch(
    tmp_path: Path,
) -> None:
    """A re-dispatched item is not pinned by the previous run's cap decision.

    The intent is dispatch-bound for the same reason the receipt is: an item
    that was capped, re-labelled by a human, and dispatched again must harvest
    on its own merits. The stale record is retired rather than left to be
    re-evaluated every tick.
    """
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _harvest_one_fixer,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-7-2",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text(json.dumps({
        "dispatch_id": "fix-7-1",
        "label": "ai:needs-human-decision",
        "stage": "fix",
        "kind": "cap",
        "failures": 4,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
        with mock.patch("tick.gh", side_effect=capture_comment_bodies([])):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None,
                        attempts=Attempts(str(wt_root)),
                    )

    converge_mock.assert_called_once_with(
        "pr", 7, "ai:revised",
        tick_log=None, phase="harvest", stage="fix",
    )
    assert not intent_path.exists(), "a stale intent must be retired, not kept"


def test_harvest_one_fixer_gates_its_escalation_comment_on_the_terminal_intent(
    tmp_path: Path,
) -> None:
    """Nothing remote may precede the fixer escalation's durable record.

    The fixer's run comment and its escalation are the same publication: the
    body it posts names ``ai:needs-human-decision``. Posting it before the
    intent is written leaves a live explanation on GitHub with no recovery
    owner — and a hold arriving before the retry routes the completed marker
    through the generic held-completion publication that the intent exists to
    prevent, contradicting the comment already posted.
    """
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_one_fixer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "NEEDS_HUMAN_DECISION", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-7-1",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))
    tick_log = TickLog.open(str(tmp_path / "logs"))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }
    bodies: list[str] = []
    # The gate's own failure modes are covered by the transient-exhaustion
    # probe's real OSError; what this test pins is the ordering around it, so
    # the gate is refused directly.
    with mock.patch("tick._record_terminal_intent", return_value=False):
        with mock.patch(
            "tick.gh_json",
            side_effect=gh_json_head_and_labels("a" * 40, ["ai:fixing"]),
        ):
            with mock.patch(
                "tick.gh", side_effect=capture_comment_bodies(bodies),
            ):
                with mock.patch(
                    "tick._converge_pipeline_labels", return_value=True,
                ) as converge_mock:
                    with mock.patch("tick.cleanup_scratch") as cleanup:
                        _harvest_one_fixer(
                            decision, str(wt_root), tick_log=tick_log,
                            attempts=Attempts(str(wt_root)),
                        )

    assert bodies == [], (
        "the escalation must not publish before its decision is recorded"
    )
    converge_mock.assert_not_called()
    assert cleanup.call_count == 0, "scratch custody is retained for the retry"
    assert [
        entry for entry in read_tick_log_entries(tmp_path / "logs")
        if entry.get("action") == "terminal_intent_failed"
    ]


def test_harvest_one_fixer_retires_a_malformed_intent_from_an_earlier_dispatch(
    tmp_path: Path,
) -> None:
    """Provable staleness outranks the fail-closed gate on malformed records.

    A malformed intent normally retains custody and blocks ordinary harvest,
    because the transition it records is already published and unreproducible.
    That is only true while it could still describe the live dispatch. One
    naming a superseded dispatch — the shape every intent written before the
    typed schema has on a re-dispatched item — can never be resumed, so leaving
    it on disk would deadlock the item forever.
    """
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        _harvest_one_fixer,
        _terminal_intent_path,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-7-2",
        "engine": "claude",
        "head_sha": "d" * 40,
    }))

    # No kind, no failure count: unparseable as a typed record, but provably
    # from the previous dispatch.
    intent_path = Path(_terminal_intent_path(str(wt_root), "pr", 7))
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text(json.dumps({
        "dispatch_id": "fix-7-1",
        "label": "ai:needs-human-decision",
        "stage": "fix",
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    with mock.patch("tick.gh_json", return_value={"headRefOid": "a" * 40}):
        with mock.patch("tick.gh", side_effect=capture_comment_bodies([])):
            with mock.patch(
                "tick._converge_pipeline_labels", return_value=True,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch"):
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None,
                        attempts=Attempts(str(wt_root)),
                    )

    converge_mock.assert_called_once_with(
        "pr", 7, "ai:revised",
        tick_log=None, phase="harvest", stage="fix",
    )
    assert not intent_path.exists(), "a stale intent must be retired, not kept"


def test_harvest_one_fixer_cap_escalation_relabels_when_post_fails(
    tmp_path: Path,
) -> None:
    """A failed escalation post must not strand the item at its in-flight label.

    The cap exists to stop an item pinning an admission slot forever. Blocking
    the terminal relabel on the explanation would reinstate exactly that, so
    here — and only here — the post is best-effort and the label still lands.
    """
    from tick import Attempts, _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-7"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "NEEDS_HUMAN_DECISION", "exit_status": 0}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-7-1",
        "engine": "claude",
    }))

    attempts = Attempts(str(wt_root))
    for _ in range(3):
        attempts.incr(7)

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 7,
    }

    # Every post fails; the escalate branch's own post failure routes straight
    # into the cap, where the escalation post fails too.
    with mock.patch(
        "tick.gh", side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ) as converge_mock:
            _harvest_one_fixer(
                decision, str(wt_root), tick_log=None, attempts=attempts,
            )

    assert converge_mock.call_args_list[-1].args[:3] == (
        "pr", 7, "ai:needs-human-decision",
    )
    assert attempts.read(7) == 0, "terminal transition must clear the counter"


def test_harvest_one_fixer_requeues_transient_result(tmp_path: Path) -> None:
    """Transient fixer results requeue behind the shared provider cooldown."""
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        EngineCooldowns,
        TickLog,
        _harvest_one_fixer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-8"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-8-quota",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 8,
    }
    attempts = Attempts(str(wt_root))
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.time.time", return_value=1_800_000_000.0):
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ) as converge_mock:
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_fixer(
                    decision, str(wt_root), tick_log=tick_log, attempts=attempts,
                )

    converge_mock.assert_called_once_with(
        "pr", 8, "ai:changes-requested",
        tick_log=tick_log, phase="harvest", stage="fix",
    )
    assert attempts.read(8) == 1
    assert EngineCooldowns(str(wt_root)).read_active(
        "claude", now=1_800_000_000.0,
    ) == 1_800_003_600.0
    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    retry = next(entry for entry in entries if entry["action"] == "transient_revert")
    assert retry["stage"] == "fix"
    assert retry["details"]["attempt"] == 1
    cleanup_mock.assert_called_once_with(str(wt_dir))


def test_harvest_one_fixer_escalates_third_transient_result(tmp_path: Path) -> None:
    """The third transient fixer result exhausts the shared retry bound."""
    from tick import (  # type: ignore[import-untyped]
        Attempts,
        TickLog,
        _harvest_one_fixer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-8"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "You've hit your session limit"}),
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-8-quota-3",
        "engine": "claude",
    }))
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 8,
    }
    attempts = Attempts(str(wt_root))
    attempts.incr(8)
    attempts.incr(8)
    tick_log = TickLog.open(str(tmp_path / "logs"))

    bodies: list[str] = []

    def assert_complete_archive_before_relabel(*_args: object, **_kwargs: object) -> bool:
        archives = list((wt_root / ".harvest-evidence").glob("pr-8-*"))
        assert len(archives) == 1
        assert (archives[0] / "result.json").is_file()
        assert wt_dir.is_dir()
        # The terminal label never moves before its explanation is published.
        assert bodies
        return True

    with mock.patch("tick.time.time", return_value=1_800_000_000.0):
        with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
            with mock.patch(
                "tick._converge_pipeline_labels",
                side_effect=assert_complete_archive_before_relabel,
            ) as converge_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_fixer(
                        decision, str(wt_root),
                        tick_log=tick_log, attempts=attempts,
                    )

    converge_mock.assert_called_once_with(
        "pr", 8, "ai:needs-human-decision",
        tick_log=tick_log, phase="harvest", stage="fix",
    )
    assert "transient-exhausted" in bodies[0]
    assert attempts.read(8) == 0
    archives = list((wt_root / ".harvest-evidence").glob("pr-8-*"))
    assert len(archives) == 1
    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    exhausted = next(
        entry for entry in entries if entry["action"] == "transient_exhausted"
    )
    assert exhausted["details"]["attempt"] == 3
    cleanup_mock.assert_called_once_with(str(wt_dir))


def test_harvest_one_fixer_archives_evidence_on_escalation(
    tmp_path: Path,
) -> None:
    """Escalated fixer harvests preserve result evidence before cleanup."""
    from tick import TickLog, _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-8"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(json.dumps({
        "result": "Blocked. NEEDS_HUMAN_DECISION",
        "is_error": False,
    }))
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "fix",
        "pr_number": 8,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-8-test",
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 8,
    }
    tick_log = TickLog.open(str(tmp_path / "logs"))

    def assert_complete_archive_before_relabel(*_args: object, **_kwargs: object) -> bool:
        archives = list((wt_root / ".harvest-evidence").glob("pr-8-*"))
        assert len(archives) == 1
        assert (archives[0] / "result.json").is_file()
        assert wt_dir.is_dir()
        return True

    with mock.patch(
        "tick.gh_json", return_value={"headRefOid": "e" * 40},
    ):
        with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
            harvest_fix.return_value = {
                "action": "escalate",
                "label": "ai:needs-human-decision",
                "sentinel": "NEEDS_HUMAN_DECISION",
            }
            with mock.patch(
                "tick._converge_pipeline_labels",
                side_effect=assert_complete_archive_before_relabel,
            ):
                _harvest_one_fixer(
                    decision, str(wt_root), tick_log=tick_log, attempts=None,
                )

    assert not wt_dir.exists(), "scratch worktree should still be cleaned up"
    archives = list((wt_root / ".harvest-evidence").glob("pr-8-*"))
    assert len(archives) == 1
    assert (archives[0] / "result.json").is_file()
    assert (archives[0] / "dispatch.json").is_file()

    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    summary = next(entry for entry in entries if entry["action"] == "engine_result_summary")
    assert summary["dispatch_id"] == "fix-8-test"
    assert summary["details"]["harvest_action"] == "escalate"
    assert summary["details"]["sentinel"] == "NEEDS_HUMAN_DECISION"
    evidence = next(entry for entry in entries if entry["action"] == "harvest_evidence")
    assert evidence["details"]["archive_dir"] == str(archives[0])


def test_harvest_one_fixer_logs_skills_used(tmp_path: Path) -> None:
    """The fixer harvest summary reports skills from skill-usage.log."""
    from tick import TickLog, _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-8"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(json.dumps({
        "result": "done",
        "skills_used": ["review-response-hygiene"],
        "skill_usage_status": "active",
    }))
    dispatch_id = "fix-8-telemetry"
    (wt_dir / "dispatch.json").write_text(json.dumps({"dispatch_id": dispatch_id}))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 8,
    }
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
        harvest_fix.return_value = {
            "action": "escalate",
            "label": "ai:needs-human-decision",
            "sentinel": "NEEDS_HUMAN_DECISION",
        }
        with mock.patch(
            "tick._converge_pipeline_labels", return_value=True,
        ):
            _harvest_one_fixer(
                decision, str(wt_root), tick_log=tick_log, attempts=None,
            )

    entries = [json.loads(line) for line in Path(tick_log.path).read_text().splitlines()]
    summary = next(entry for entry in entries if entry["action"] == "engine_result_summary")
    assert summary["details"]["skills_used"] == ["review-response-hygiene"]
    assert summary["details"]["skill_usage_status"] == "active"


def test_harvest_one_fixer_retains_scratch_when_evidence_copy_fails(
    tmp_path: Path,
) -> None:
    """Fixer escalation retains its source result when archival fails."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-8"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        json.dumps({"result": "Blocked. NEEDS_HUMAN_DECISION"}),
    )
    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 8,
    }

    with mock.patch("tick.shutil.copy2", side_effect=OSError("disk full")):
        with mock.patch("tick.gh"):
            with mock.patch("tick.relabel") as relabel_mock:
                with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                    _harvest_one_fixer(
                        decision, str(wt_root), tick_log=None, attempts=None,
                    )

    relabel_mock.assert_not_called()
    cleanup_mock.assert_not_called()
    assert (wt_dir / "result.json").is_file()


def test_harvest_one_fixer_skips_cleanup_on_relabel_failure(
    tmp_path: Path,
) -> None:
    """_harvest_one_fixer retains marker when relabel fails."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-4"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED"}',
    )

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 4,
    }

    with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
        harvest_fix.return_value = {
            "action": "recheck",
            "label": "ai:revised",
            "sentinel": "FIXES_PUSHED_RECHECK_NEEDED",
        }
        with mock.patch("tick._converge_pipeline_labels", return_value=False):
            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                _harvest_one_fixer(
                    decision, str(wt_root),
                    tick_log=None, attempts=None,
                )

    # Cleanup must NOT be called — marker survives for retry
    cleanup_mock.assert_not_called()


def test_harvest_one_fixer_returns_early_when_result_missing(
    tmp_path: Path,
) -> None:
    """_harvest_one_fixer leaves marker when result.json is missing."""
    from tick import _harvest_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-6"
    wt_dir.mkdir()
    # No result.json

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 6,
    }

    with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
        with mock.patch("tick.cleanup_scratch") as cleanup_mock:
            _harvest_one_fixer(
                decision, str(wt_root),
                tick_log=None, attempts=None,
            )

    harvest_fix.assert_not_called()
    cleanup_mock.assert_not_called()


def test_harvest_fixers_routes_harvest_decisions_to_helper(
    tmp_path: Path,
) -> None:
    """harvest_fixers() calls _harvest_one_fixer for harvest actions."""
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-99"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED"}',
    )
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "fix",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-99",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    def fake_gh_json(*args: str) -> object:
        if "headRefOid" in args[-1]:
            return {"headRefOid": "e" * 40}
        return {"labels": [{"name": "ai:fixing"}]}

    with mock.patch("tick.gh_text") as gh_text_mock:
        gh_text_mock.return_value = "OPEN"
        with mock.patch("tick.gh_json") as gh_json_mock:
            gh_json_mock.side_effect = fake_gh_json
            with mock.patch("tick.pipeline.harvest_fix") as harvest_fix:
                harvest_fix.return_value = {
                    "action": "recheck",
                    "label": "ai:revised",
                    "sentinel": "FIXES_PUSHED_RECHECK_NEEDED",
                }
                with mock.patch("tick.cleanup_scratch"):
                    with mock.patch(
                        "tick._converge_pipeline_labels", return_value=True,
                    ):
                        result = harvest_fixers(
                            tick_log=None, target=None, wt_root=str(wt_root),
                        )

    assert result is True


# --- Fixer-authored PR body revision ---


def test_harvest_replaces_the_pr_body_from_the_fixer_revision_before_relabel(
    tmp_path: Path,
) -> None:
    """A stamped, valid revision file is applied before ``ai:revised`` is set.

    Driven through ``harvest_fixers`` — the production HARVEST entry point — so
    the proof covers the marker read, the validation, the ordering against the
    comment and the relabel, and the staging location together, rather than a
    helper called with hand-built arguments.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(REVISED_PR_BODY),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(calls, staged, live_body=STALE_PR_BODY):
        with mock.patch("tick.cleanup_scratch"):
            assert harvest_fixers(
                tick_log=None, target=None, wt_root=str(wt_root),
            ) is True

    assert calls == ["pr edit", "pr comment", "converge"]
    edit_path, edit_body = staged[0]
    assert edit_body.strip() == REVISED_PR_BODY.strip()
    # Staged under controller-owned state, never inside the branch worktree.
    assert edit_path.startswith(str(wt_root / ".receipts"))
    assert not edit_path.startswith(str(wt_dir))
    assert not Path(edit_path).exists(), "staged body file must not survive"
    assert "- PR body: replaced with the fixer's revision" in staged[1][1]


def test_harvest_leaves_the_pr_body_alone_when_the_fixer_wrote_no_revision(
    tmp_path: Path,
) -> None:
    """The common run must cost nothing and say nothing about the body.

    Most fixer runs leave the body correct. If the absent-file case still
    reached ``gh pr edit`` — or still annotated the comment, or still spent the
    mutation-boundary re-read — the feature would tax every run to serve the
    rare one.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(calls, staged, live_body=STALE_PR_BODY):
        with mock.patch("tick.cleanup_scratch"):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert calls == ["pr comment", "converge"], (
        "a run with no revision file still touched the PR body"
    )
    assert "PR body" not in staged[0][1], (
        f"the comment annotates a body nobody offered to change: {staged[0][1]}"
    )


@pytest.mark.parametrize(("scenario", "revision", "live_body", "expected"), [
    (
        "unstamped",
        fixer_revision_file(REVISED_PR_BODY, dispatch_id="fix-12-0"),
        STALE_PR_BODY,
        "left unchanged — the revision file did not carry this run's marker "
        "lines",
    ),
    (
        "stamped for the dispatch but naming no head",
        f"pr-body-revision: fix-12-1\n{REVISED_PR_BODY}",
        STALE_PR_BODY,
        "left unchanged — the revision file did not carry this run's marker "
        "lines",
    ),
    (
        "invalid",
        fixer_revision_file("## Summary\n\nCloses #7\nnot the required shape\n"),
        STALE_PR_BODY,
        "left unchanged — the fixer's revision did not satisfy the required "
        "PR body sections",
    ),
    (
        "conflict",
        fixer_revision_file(REVISED_PR_BODY),
        "## Summary\n\nCloses #7\n- Problem: a human rewrote this mid-run.\n",
        "left unchanged — it was edited while the fixer ran",
    ),
    (
        "stale head",
        fixer_revision_file(REVISED_PR_BODY, head="f" * 40),
        STALE_PR_BODY,
        "left unchanged — the fixer's revision names a commit other than the "
        "head it pushed",
    ),
    (
        "summary closure swapped",
        fixer_revision_file(REVISED_PR_BODY.replace("Closes #7", "Closes #8")),
        STALE_PR_BODY,
        "left unchanged — the fixer's revision changes which issues merging "
        "this PR closes",
    ),
    (
        "closing directive added",
        fixer_revision_file(
            REVISED_PR_BODY.replace(
                "- Covered by the harvest tests.",
                "- Covered by the harvest tests. Also fixes #999.",
            ),
        ),
        STALE_PR_BODY,
        "left unchanged — the fixer's revision changes which issues merging "
        "this PR closes",
    ),
])
def test_harvest_refuses_an_unpublishable_pr_body_and_says_why(
    tmp_path: Path, scenario: str, revision: str, live_body: str, expected: str,
) -> None:
    """Every rejection leaves the live body intact and names its reason.

    ``gh pr edit`` replaces the whole body and GitHub gives the pipeline no
    undo, so each of these fails closed. Naming the reason in the published
    comment is the other half: a body silently left alone is indistinguishable
    from a pipeline that never looked, and the next reviewer cannot tell which.

    The two closure cases are the ones structural validation alone would wave
    through: both bodies are perfectly well formed, and both change what merging
    the PR closes — one by moving the `Closes` line to a sibling issue, one by
    adding a directive further down that nobody pinned.

    The head-less case is what an engine following a prompt that predates the
    head stamp writes: correctly bound to this dispatch, binding nothing to a
    commit. It fails closed rather than falling back to dispatch-only binding,
    because the fallback is precisely the window the head stamp closes.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    # The dispatch-time snapshot is the pre-conflict body in every case, so
    # only the `conflict` scenario's live body diverges from it.
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(revision)

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(calls, staged, live_body=live_body):
        with mock.patch("tick.cleanup_scratch"):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert calls == ["pr comment", "converge"], (
        f"the {scenario} revision reached the live PR body"
    )
    assert f"- PR body: {expected}" in staged[0][1], (
        f"the {scenario} comment does not state the reason: {staged[0][1]}"
    )


@pytest.mark.parametrize(("scenario", "pinned", "expected"), [
    (
        "marker predates the revision path",
        None,
        "left unchanged — the body this run was dispatched against could not "
        "be pinned",
    ),
    (
        "PR body was truncated for the engine",
        True,
        "left unchanged — the fixer was shown only part of it, so a full "
        "replacement could drop what it never saw",
    ),
])
def test_harvest_withholds_a_revision_it_cannot_verify(
    tmp_path: Path, scenario: str, pinned: bool | None, expected: str,
) -> None:
    """Missing or partial provenance withholds the write; never waves it through.

    A marker written before this path existed carries no snapshot, and a body
    the engine only saw a prefix of cannot be *replaced* by that engine — the
    unread tail would be deleted with no gate able to notice. Neither is
    corruption, and neither is proof the replacement is safe: the live body is
    the one thing that cannot be restored.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    overrides: dict[str, object] = {} if pinned is None else {
        "pr_body": fixer_body_provenance(STALE_PR_BODY, truncated=pinned),
    }
    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(**overrides))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(REVISED_PR_BODY),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(calls, staged, live_body=STALE_PR_BODY):
        with mock.patch("tick.cleanup_scratch"):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert "pr edit" not in calls, f"{scenario}: an unverified body was published"
    assert f"- PR body: {expected}" in staged[0][1], (
        f"{scenario}: the comment does not state the reason: {staged[0][1]}"
    )


def test_a_body_whose_tail_the_engine_never_saw_is_never_replaced(
    tmp_path: Path,
) -> None:
    """The truncation flag is pinned by dispatch, not asserted by a test fixture.

    This is the one rejection whose evidence is unreachable from the harvest: a
    perfectly well-formed replacement, over a live body whose digest still
    matches, that happens to delete a required section the engine never read. No
    structural gate can see the difference, so the fact has to travel in the
    marker — and this drives both halves through production, dispatch's own read
    (``_fixer_pr_context``) into ``harvest_fixers``, because a hand-set flag
    would prove only that the harvest honours a field nothing sets.

    The body puts its ``## Test plan`` past ``FIXER_CONTEXT_BUDGET`` and the
    candidate omits that section entirely — the exact silent loss at issue.
    """
    from tick import (  # type: ignore[import-untyped]
        FIXER_CONTEXT_BUDGET,
        _fixer_pr_context,
        harvest_fixers,
    )

    filler = "\n".join(f"- Detail {i}: prior context." for i in range(1200))
    long_body = (
        f"## Summary\n\nCloses #7\n- Problem: stale.\n\n"
        f"## What changed\n\n{filler}\n\n"
        f"## Test plan\n\n- [x] `uv run pytest` — the section past the cut.\n"
    )
    assert long_body.index("## Test plan") > FIXER_CONTEXT_BUDGET, (
        "the fixture no longer puts required material past the context bound"
    )

    with mock.patch("tick.gh_json", side_effect=fixer_gh_json(
        pr_body=long_body,
        issues=[{"number": 7, "title": "t", "body": "b"}],
    )):
        context = _fixer_pr_context(42)
    assert context is not None and context.body_provenance is not None
    assert context.body_provenance.truncated, (
        "dispatch did not record that the engine was shown a prefix"
    )

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=context.body_provenance.as_marker_field(),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(REVISED_PR_BODY),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(calls, staged, live_body=long_body):
        with mock.patch("tick.cleanup_scratch"):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert "pr edit" not in calls, "a body the engine never fully read was replaced"
    assert (
        "- PR body: left unchanged — the fixer was shown only part of it, so a "
        "full replacement could drop what it never saw"
    ) in staged[0][1], f"the truncation reason was not published: {staged[0][1]}"


@pytest.mark.parametrize(("scenario", "revision_body"), [
    (
        "the second closure is dropped",
        REVISED_PR_BODY,
    ),
    (
        "the second closure moves to another issue",
        REVISED_PR_BODY.replace(
            "- Covered by the harvest tests.",
            "- Covered by the harvest tests. Also fixes #99.",
        ),
    ),
])
def test_harvest_holds_a_multi_issue_pr_to_its_whole_closure_set(
    tmp_path: Path, scenario: str, revision_body: str,
) -> None:
    """A closure the summary never mentioned is still a closure.

    Merging acts on every closing directive in the body, not only the ``Closes``
    line under ``## Summary``. A PR that also declares ``Fixes #8`` further down
    closes #8 too, so a replacement that drops it — or swaps it for #99 — changes
    what merging this PR does while satisfying every structural check. The
    pinned set is the whole set for exactly that reason.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    dispatched = (
        "## Summary\n\nCloses #7\n- Problem: stale.\n\n"
        "## What changed\n\n- Structure: also fixes #8 along the way.\n"
    )
    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(dispatched),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(revision_body),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(calls, staged, live_body=dispatched):
        with mock.patch("tick.cleanup_scratch"):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert "pr edit" not in calls, f"{scenario}: the closure set was changed"
    assert (
        "- PR body: left unchanged — the fixer's revision changes which issues "
        "merging this PR closes"
    ) in staged[0][1], f"{scenario}: no reason published: {staged[0][1]}"


def test_harvest_refuses_a_revision_larger_than_github_will_accept(
    tmp_path: Path,
) -> None:
    """An oversized candidate is refused, and never fully read into the tick.

    ``gh pr edit`` would reject it three times and escalate to a human, so the
    size gate turns a runaway engine file into one published reason. The bound
    has to be an *input* bound: a classifier that reads the file first and
    measures second can be exhausted by the file it is meant to reject.
    """
    from tick import (  # type: ignore[import-untyped]
        _PR_BODY_MAX_CHARS,
        _PR_BODY_REVISION_READ_LIMIT,
        harvest_fixers,
    )

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))
    oversized = REVISED_PR_BODY + ("x" * _PR_BODY_MAX_CHARS)
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(oversized),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(calls, staged, live_body=STALE_PR_BODY):
        with mock.patch("tick.cleanup_scratch"):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert "pr edit" not in calls, "an oversized body was published"
    assert (
        "- PR body: left unchanged — the fixer's revision is longer than "
        "GitHub accepts"
    ) in staged[0][1], (
        f"the oversized comment blames the wrong thing: {staged[0][1]}"
    )
    assert _PR_BODY_REVISION_READ_LIMIT > _PR_BODY_MAX_CHARS, (
        "the read bound must leave room for the envelope, or every candidate "
        "at the size limit is rejected as oversized"
    )


def test_the_size_bound_holds_against_both_readings_of_githubs_limit(
    tmp_path: Path,
) -> None:
    """A body under the limit in characters can still be over it in bytes.

    GitHub does not document whether its PR-body limit counts characters or
    UTF-8 bytes, and the two differ by up to 4x on non-ASCII prose. Guessing
    "characters" and being wrong publishes a body GitHub refuses — three failed
    edits, custody retained, escalation — so the bound is applied to both
    readings and the stricter one wins.
    """
    import tick  # type: ignore[import-untyped]

    # Comfortably under the character limit, comfortably over it in UTF-8.
    multibyte = REVISED_PR_BODY.replace(
        "- Covered by the harvest tests.",
        "- Covered by the harvest tests. " + "漢" * (tick._PR_BODY_MAX_CHARS // 2),
    )
    assert len(multibyte) < tick._PR_BODY_MAX_CHARS < len(multibyte.encode()), (
        "the fixture no longer straddles the two readings of the limit"
    )

    (tmp_path / ".pr-body-revision.md").write_text(
        fixer_revision_file(multibyte),
    )
    state, body = tick._fixer_body_revision(
        wt_dir=str(tmp_path),
        dispatch_id="fix-12-1",
        current_head=FIXER_HARVEST_HEAD,
        provenance=tick.PRBodyProvenance.from_marker(
            fixer_body_provenance(STALE_PR_BODY),
        ),
        live_body=STALE_PR_BODY,
    )

    assert (state, body) == ("oversized", ""), (
        "a body over the limit in UTF-8 bytes was accepted for publication"
    )


def test_the_revision_classifier_never_reads_past_its_own_size_bound(
    tmp_path: Path,
) -> None:
    """The size gate is applied to the read, not to what the read returned.

    Pinned at the helper because that is where the bound lives: a production
    harvest cannot show how many characters were consumed, only that the
    oversized candidate was refused — which a read-everything-then-measure
    implementation also satisfies.
    """
    import tick  # type: ignore[import-untyped]

    read_sizes: list[int] = []

    class RecordingText(io.StringIO):
        def read(self, size: int | None = -1) -> str:
            read_sizes.append(-1 if size is None else size)
            return super().read(size)

    (tmp_path / ".pr-body-revision.md").write_text("unused")
    huge = RecordingText(fixer_revision_file("y" * 10_000_000))

    with mock.patch("builtins.open", return_value=huge):
        state, body = tick._fixer_body_revision(
            wt_dir=str(tmp_path),
            dispatch_id="fix-12-1",
            current_head=FIXER_HARVEST_HEAD,
            provenance=tick.PRBodyProvenance.from_marker(
                fixer_body_provenance(STALE_PR_BODY),
            ),
            live_body=STALE_PR_BODY,
        )

    assert (state, body) == ("oversized", "")
    assert read_sizes and all(
        0 < size <= tick._PR_BODY_REVISION_READ_LIMIT + 1 for size in read_sizes
    ), f"the classifier read without a bound: {read_sizes}"


def test_harvest_holds_the_label_and_scratch_when_the_body_edit_fails(
    tmp_path: Path,
) -> None:
    """A failed ``gh pr edit`` retains custody, exactly like a failed post.

    The relabel to ``ai:revised`` is what invites the next reviewer. Letting it
    land after a failed body write would publish the handoff the write existed
    to correct, so the failure takes the same bound as the comment post: no
    comment, no relabel, scratch retained for the next tick.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(REVISED_PR_BODY),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(
        calls, staged, live_body=STALE_PR_BODY, edit_fails=True,
    ):
        with mock.patch("tick.cleanup_scratch") as cleanup_mock:
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert calls == ["pr edit"], f"the harvest continued past a failed edit: {calls}"
    cleanup_mock.assert_not_called()
    assert (wt_dir / "result.json").exists(), "the scratch was not retained"
    assert (wt_dir / ".pr-body-revision.md").exists(), (
        "the revision the next tick has to retry was discarded"
    )


def test_harvest_withholds_a_pr_body_revision_from_an_escalating_run(
    tmp_path: Path,
) -> None:
    """A run that did not reach re-review does not get to rewrite the body.

    The replacement describes a head the engine claims to have pushed. An
    escalation is exactly the outcome where that claim did not hold — a missing
    sentinel, or a ``FIXES_PUSHED`` contradicted by an unmoved head — so
    publishing its body would replace a true handoff with a speculative one.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "result.json").write_text('{"result": "NEEDS_HUMAN_DECISION"}')
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(REVISED_PR_BODY),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(calls, staged, live_body=STALE_PR_BODY):
        with mock.patch("tick.cleanup_scratch"):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert "pr edit" not in calls, "an escalating run rewrote the PR body"
    assert (
        "- PR body: left unchanged — the fixer offered a revision, but this "
        "run did not reach re-review"
    ) in staged[0][1], (
        f"the escalation comment does not account for the revision: {staged[0][1]}"
    )


def test_harvest_republishes_a_body_revision_the_previous_tick_already_applied(
    tmp_path: Path,
) -> None:
    """A retry after a landed edit is idempotent, not a conflict.

    The edit lands before the comment, so a failed post leaves the next tick
    looking at a body it wrote itself. Compared against the dispatch-time
    snapshot alone that reads as "somebody edited this", which would withdraw
    a correction that is already live and publish a false reason for it.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(REVISED_PR_BODY),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    # The live body is already the revision — the state the previous tick left.
    with fixer_harvest_gh(calls, staged, live_body=REVISED_PR_BODY):
        with mock.patch("tick.cleanup_scratch"):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert calls == ["pr edit", "pr comment", "converge"]
    assert "- PR body: replaced with the fixer's revision" in staged[1][1], (
        f"the retry reported its own landed edit as a conflict: {staged[1][1]}"
    )


# --- PR body revision: the mutation boundary ---


@pytest.mark.parametrize(("scenario", "kwargs"), [
    (
        "a human adds an emergency hold",
        {"mutation_labels": ("ai:fixing", "ai:blocked")},
    ),
    (
        "a human rewrites the body",
        {"mutation_body": "## Summary\n\nCloses #7\n- Problem: mine now.\n"},
    ),
    (
        "somebody force-pushes the branch",
        {"mutation_head": "9" * 40},
    ),
    (
        "the authority read itself fails",
        {"mutation_read_fails": True},
    ),
])
def test_harvest_abandons_the_body_write_when_authority_changes_mid_harvest(
    tmp_path: Path, scenario: str, kwargs: dict[str, object],
) -> None:
    """Authority is revalidated at the write, not only when the tick started.

    Reconcile reads labels once, minutes before the harvest gets here, and the
    classification read is older still. A hold added in that window has to
    outrank a completed result (`pr-guidelines.md`, "A human may stop a
    dispatched run at any time"), and a body or head edited in it must not be
    clobbered by a write with no undo. An authority that cannot be read at all
    is the same answer: unproven is not permission.

    No case here may publish anything this tick — not the body, not the comment,
    not the relabel. A comment already rendered as "replaced" would be a false
    receipt for a write that did not happen.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(REVISED_PR_BODY),
    )

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with fixer_harvest_gh(
        calls, staged, live_body=STALE_PR_BODY, **kwargs,  # type: ignore[arg-type]
    ):
        with mock.patch("tick.cleanup_scratch") as cleanup_mock:
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert calls == [], f"{scenario}: the harvest wrote anyway — {calls}"
    cleanup_mock.assert_not_called()
    assert (wt_dir / "result.json").exists(), f"{scenario}: scratch not retained"
    assert (wt_dir / ".pr-body-revision.md").exists(), (
        f"{scenario}: the candidate the next tick re-judges was discarded"
    )


def test_harvest_reports_the_body_edit_it_lost_the_race_for_on_the_next_tick(
    tmp_path: Path,
) -> None:
    """Losing the race defers the run; it does not silently drop the reason.

    The tick that observes the drift at the write boundary has already rendered
    a comment saying "replaced", so it publishes nothing at all. The reason the
    reviewer needs comes from the following tick, which now classifies the same
    candidate against the changed body and reports the conflict.
    """
    from tick import harvest_fixers  # type: ignore[import-untyped]

    wt_root, wt_dir = fixer_harvest_scratch(tmp_path)
    (wt_dir / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY),
    ))
    (wt_dir / ".pr-body-revision.md").write_text(
        fixer_revision_file(REVISED_PR_BODY),
    )
    edited = "## Summary\n\nCloses #7\n- Problem: mine now.\n"

    calls: list[str] = []
    staged: list[tuple[str, str]] = []
    with mock.patch("tick.cleanup_scratch"):
        with fixer_harvest_gh(
            calls, staged, live_body=STALE_PR_BODY, mutation_body=edited,
        ):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))
        # Next tick: the edit is now visible to the classification read too.
        with fixer_harvest_gh(calls, staged, live_body=edited):
            harvest_fixers(tick_log=None, target=None, wt_root=str(wt_root))

    assert calls == ["pr comment", "converge"], (
        f"the deferred run did not complete on the next tick: {calls}"
    )
    assert (
        "- PR body: left unchanged — it was edited while the fixer ran"
    ) in staged[0][1], f"the conflict was never published: {staged[0][1]}"


# --- M5: Harvest exception retry cap ---


def test_harvest_one_reviewer_escalates_after_max_harvest_failures(
    tmp_path: Path,
) -> None:
    """_harvest_one_reviewer escalates to human when harvest_review throws
    persistently. After MAX_DISPATCH_ATTEMPTS consecutive failures the
    PR is escalated to ai:needs-human-decision instead of looping forever.
    """
    from pipeline import MAX_DISPATCH_ATTEMPTS
    from tick import Attempts, _harvest_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "review.json").write_text('{"verdict": "ready"}')
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc",
        "head_sha": "def", "pre_label": "ai:implemented",
        "dispatch_id": "d-42", "pid": 12345,
        "start_time": 9999999999.0, "pgid": 12345,
    }))

    decision: dict[str, object] = {
        "action": "harvest", "label": "", "kill": False,
        "identity": "matched", "pr_number": 42,
    }

    attempts = Attempts(str(wt_root), "")

    bodies: list[str] = []
    with mock.patch("tick.pipeline.harvest_review") as harvest_review:
        harvest_review.side_effect = ValueError("persistent parse error")
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = "abc"
            with mock.patch("tick.gh", side_effect=capture_comment_bodies(bodies)):
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.side_effect = reviewer_live_identity("main", "def")
                    with mock.patch(
                        "tick._converge_pipeline_labels", return_value=True,
                    ) as converge_mock:
                        with mock.patch("tick.cleanup_scratch") as cleanup:

                            # First MAX_DISPATCH_ATTEMPTS times: retry, no escalation
                            for _ in range(MAX_DISPATCH_ATTEMPTS):
                                _harvest_one_reviewer(
                                    decision, str(wt_root),
                                    tick_log=None, attempts=attempts,
                                )

                            # Should not have escalated yet
                            converge_mock.assert_not_called()
                            cleanup.assert_not_called()
                            assert bodies == []
                            assert attempts.read(42) == MAX_DISPATCH_ATTEMPTS

                            # One more call: escalates to human
                            _harvest_one_reviewer(
                                decision, str(wt_root),
                                tick_log=None, attempts=attempts,
                            )

    # Now should escalate
    converge_mock.assert_called_once_with(
        "pr", 42, "ai:needs-human-decision",
        tick_log=None, phase="harvest", stage="review",
    )
    assert len(bodies) == 1, "the capped escalation must explain itself"
    assert "ai:needs-human-decision" in bodies[0]
    cleanup.assert_called_once()
    parser_archives = list((wt_root / ".harvest-evidence").glob("pr-42-d-42"))
    assert len(parser_archives) == 1
    assert (parser_archives[0] / "review.json").is_file()


def test_dispatch_one_reviewer_policy_gate_fails_closed_on_diff_error(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must route to ai:needs-human-decision when git diff raises.

    When the PR head SHA is not present locally, ``git diff base..head`` raises
    CalledProcessError.  Swallowing the exception and continuing with empty
    ``changed_paths`` lets a PR that edits CLAUDE.md/AGENTS.md slip past the
    policy gate — "untrusted input controls the validator."  The gate must
    fail closed instead.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    diff_called = {"count": 0}

    def fake_git(*args: str, **kwargs: object) -> str:
        if "diff" in args:
            diff_called["count"] += 1
            raise subprocess.CalledProcessError(128, "git diff")
        if "--show-toplevel" in args:
            return str(tmp_path / "repo")
        # fetch and rev-parse
        return "abc1234abc1234abc1234abc1234abc1234abc1234"

    def fake_gh_text(*args: str, **kwargs: object) -> str:
        if "baseRefName" in args:
            return "main"
        # headRefOid
        return "deadbeef123deadbeef123deadbeef123deadbeef"

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fake_git):
            with mock.patch("tick.gh_text", side_effect=fake_gh_text), \
                    mock.patch("tick.gh_json", return_value={
                        "title": "feat: reviewed change", "body": "context",
                    }):
                with mock.patch(
                    "tick.gh", side_effect=allow_escalation_comment(),
                ), mock.patch("tick.relabel") as relabel_mock:
                    with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                        _dispatch_one_reviewer(
                            pr_number=42,
                            pre_label="ai:implemented",
                            tick_log=None,
                        )

    assert diff_called["count"] == 1, "git diff was not called"
    relabel_mock.assert_called_once_with(
        "pr", 42, "ai:implemented", "ai:needs-human-decision",
    )
    cleanup_mock.assert_called_once()


def test_dispatch_one_reviewer_policy_gate_diff_error_relabel_failure_does_not_propagate(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must not propagate CalledProcessError from diff-error escalation.

    When git diff raises (head SHA unavailable) AND the subsequent relabel to
    ai:needs-human-decision also raises (transient GH API blip), the exception
    must be swallowed.  An uncaught error propagates through dispatch_reviewers'
    for-loop and kills dispatch for all remaining PRs in the tick.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    def fake_git(*args: str, **kwargs: object) -> str:
        if "diff" in args:
            raise subprocess.CalledProcessError(128, "git diff")
        if "--show-toplevel" in args:
            return str(tmp_path / "repo")
        return "abc1234"

    def fake_gh_text(*args: str, **kwargs: object) -> str:
        return "main" if "baseRefName" in args else "deadbeef"

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fake_git):
            with mock.patch("tick.gh_text", side_effect=fake_gh_text), \
                    mock.patch("tick.gh_json", return_value={
                        "title": "feat: reviewed change", "body": "context",
                    }):
                with mock.patch(
                    "tick.gh", side_effect=allow_escalation_comment(),
                ), mock.patch(
                    "tick.relabel",
                    side_effect=subprocess.CalledProcessError(1, "gh"),
                ):
                    with mock.patch("tick.cleanup_scratch"):
                        # Must not raise
                        _dispatch_one_reviewer(
                            pr_number=42,
                            pre_label="ai:implemented",
                            tick_log=None,
                        )


def test_dispatch_one_reviewer_policy_touch_relabel_failure_does_not_propagate(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must not propagate CalledProcessError from policy-touch escalation.

    When the PR touches a reviewer-governing file AND the relabel to
    ai:needs-human-decision raises (transient GH API blip), the exception must be
    swallowed — same crash vector as the diff-error path.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    def fake_git(*args: str, **kwargs: object) -> str:
        if "diff" in args:
            return "CLAUDE.md"
        if "--show-toplevel" in args:
            return str(tmp_path / "repo")
        return "abc1234"

    def fake_gh_text(*args: str, **kwargs: object) -> str:
        return "main" if "baseRefName" in args else "deadbeef"

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fake_git):
            with mock.patch("tick.gh_text", side_effect=fake_gh_text), \
                    mock.patch("tick.gh_json", return_value={
                        "title": "feat: reviewed change", "body": "context",
                    }):
                with mock.patch("tick.pipeline.touches_review_policy", return_value=True):
                    with mock.patch(
                        "tick.gh", side_effect=allow_escalation_comment(),
                    ), mock.patch(
                        "tick.relabel",
                        side_effect=subprocess.CalledProcessError(1, "gh"),
                    ):
                        with mock.patch("tick.cleanup_scratch"):
                            # Must not raise
                            _dispatch_one_reviewer(
                                pr_number=42,
                                pre_label="ai:implemented",
                                tick_log=None,
                            )


@pytest.mark.parametrize("metadata", [
    subprocess.CalledProcessError(1, "gh"),
    ValueError("invalid JSON"),
    None, [], {}, {"defaultBranchRef": None},
    {"defaultBranchRef": {}}, {"defaultBranchRef": "master"},
    {"defaultBranchRef": {"name": None}},
    {"defaultBranchRef": {"name": ""}},
    {"defaultBranchRef": {"name": "  "}},
    {"defaultBranchRef": {"name": "--upload-pack=unexpected"}},
])
def test_implementer_dispatch_reports_unresolved_default_branch(
    metadata: object, tmp_path: Path,
) -> None:
    """Unavailable metadata stops before fetching or launching, with context."""
    import tick

    with (
        mock.patch("tick.gh_json") as lookup,
        mock.patch("tick.git", return_value=str(tmp_path)) as git_mock,
        mock.patch("tick._launch_claude_engine") as launch,
        mock.patch("tick._escalate_dispatch_setup_failure") as escalate,
    ):
        if isinstance(metadata, Exception):
            lookup.side_effect = metadata
        else:
            lookup.return_value = metadata
        tick._dispatch_one_implementer(issue_number=77, issue_title="Default branch")

    git_mock.assert_called_once_with("rev-parse", "--show-toplevel")
    launch.assert_not_called()
    assert not (Path(tick.WT_ROOT) / "issue-77").exists()
    escalate.assert_called_once()
    context = escalate.call_args.kwargs
    assert context["entity"] == "issue"
    assert context["number"] == 77
    assert context["pre_label"] == "ai:available"
    assert tick.TRUSTED_HEAD_REPO in context["reason"]
    assert "default branch" in context["reason"]


@pytest.mark.parametrize(("configured_base", "expected_base"), [
    (None, "master"), ("", "master"), ("  ", "master"),
    ("main", "main"), ("release/stable", "release/stable"),
])
def test_implementer_dispatch_uses_target_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    configured_base: str | None, expected_base: str,
) -> None:
    """A real fetch and checkout must choose master even when main exists."""
    import tick

    repo = tmp_path / "repo"
    repo.mkdir()
    git_init_repo(repo)
    monkeypatch.chdir(repo)
    tick.git("branch", "-M", "main")
    tick.git("checkout", "-b", "master")
    tick.git("commit", "--allow-empty", "-m", "Default branch advances")
    tick.git("checkout", "-b", "release/stable")
    tick.git("commit", "--allow-empty", "-m", "Release branch advances")
    expected_sha = tick.git("rev-parse", expected_base)
    tick.git("checkout", "main")
    bare = tmp_path / "server.git"
    tick.git("clone", "--bare", str(repo), str(bare))
    tick.git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/master")
    tick.git("remote", "add", "upstream", str(bare))
    monkeypatch.setattr(tick, "TARGET_REMOTE", "upstream")
    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", "elsewhere/project")
    monkeypatch.setenv("GH_HOST", "other.example")
    if configured_base is not None:
        # Even a blank repository override must replace a non-blank default.
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[defaults]\nbase_ref = "main"\n'
            f'[repos."elsewhere/project"]\nbase_ref = {json.dumps(configured_base)}\n',
        )
        monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    wt_dir = Path(tick.WT_ROOT) / "issue-77"

    with (
        mock.patch("tick.gh_json", return_value={
            "defaultBranchRef": {"name": "master"},
        }) as metadata,
        mock.patch("tick._launch_claude_engine", return_value=mock.Mock(pid=12345)),
        mock.patch("tick._publish_marker_or_revert", return_value=True),
        mock.patch("tick.relabel"),
    ):
        if configured_base and configured_base.strip():
            metadata.side_effect = AssertionError(
                "explicit base must not need GitHub discovery",
            )
        tick._dispatch_one_implementer(issue_number=77, issue_title="Default branch")

    assert tick.git("-C", str(wt_dir), "rev-parse", "HEAD") == expected_sha
    marker = json.loads((wt_dir / "dispatch.json").read_text())
    assert marker["base_ref"] == expected_base
    assert marker["base_sha"] == marker["head_sha"] == expected_sha
    if configured_base and configured_base.strip():
        metadata.assert_not_called()
    else:
        metadata.assert_called_once_with(
            "repo", "view", "github.com/elsewhere/project", "--json", "defaultBranchRef",
        )


def test_dispatch_one_implementer_creates_isolated_git_worktree(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer must create a git worktree, not a plain directory.

    When the engine runs with cwd=wt_dir, ``git rev-parse --show-toplevel``
    from wt_dir must return wt_dir itself — not the parent checkout.  A plain
    ``os.makedirs`` leaves the parent checkout accessible via git upward
    traversal, allowing the engine to mutate the main workspace.
    """
    import subprocess as _sp

    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    # Create a minimal git repo in tmp_path to act as the main checkout.
    repo = tmp_path / "repo"
    repo.mkdir()
    _sp.run(["git", "init", str(repo)], check=True, capture_output=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@x.com"],
        check=True, capture_output=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (repo / "README.md").write_text("hi")
    _sp.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "issue-77"

    base_sha = _sp.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, encoding="utf-8", check=True,
    ).stdout.strip()

    # Capture the real Popen before mocking so fake_git can forward git
    # worktree commands to real git without hitting the mock.
    real_popen = _sp.Popen

    def fake_git(*args: str, **kwargs: object) -> str:
        if "--show-toplevel" in args:
            return str(repo)
        if "fetch" in args:
            return ""
        if "rev-parse" in args:
            return base_sha
        # Forward worktree commands to real git via the pre-mock Popen.
        proc = real_popen(
            ["git", *args],
            stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        stdout, _ = proc.communicate()
        if proc.returncode != 0:
            raise _sp.CalledProcessError(proc.returncode, ["git", *args])
        return (stdout or b"").decode("utf-8", errors="replace").strip()

    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fake_git):
            with mock.patch("tick.pipeline.atomic_write") as aw:
                aw.side_effect = lambda path, data: Path(path).parent.mkdir(
                    parents=True, exist_ok=True,
                ) or Path(path).write_text(data)
                with mock.patch("subprocess.Popen", return_value=fake_proc):
                    with mock.patch("tick._publish_marker_or_revert", return_value=True):
                        with mock.patch("tick.relabel"):
                            _dispatch_one_implementer(
                                issue_number=77,
                                issue_title="Test worktree isolation",
                                issue_body="Body",
                                tick_log=None,
                                impl_engine="claude",
                            )

    # wt_dir must be an isolated git worktree.
    result = _sp.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, encoding="utf-8",
        cwd=str(wt_dir),
    )
    assert result.returncode == 0, "git rev-parse --show-toplevel failed in wt_dir"
    actual_root = result.stdout.strip()
    assert actual_root == str(wt_dir), (
        f"Expected git root == wt_dir ({wt_dir}), got {actual_root}. "
        "Engine would resolve the parent checkout, not an isolated worktree."
    )


def test_dispatch_one_implementer_escalates_when_label_flip_fails_and_kill_fails(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer must relabel to ai:needs-human-decision on unkillable engine.

    When the post-spawn label flip fails AND _terminate_engine returns False
    (engine not killed), the item still carries its pre-dispatch label
    (ai:available).  Without same-item exclusion the next tick re-dispatches
    for the same issue.  The fix: relabel to ai:needs-human-decision so
    dispatch skips the item until a human intervenes.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    def fake_relabel(entity: str, number: int, remove: str, add: str) -> None:
        # First call is the in-flight flip — make it fail.
        if add == "ai:implementing":
            raise subprocess.CalledProcessError(1, "gh")
        # Subsequent calls (human-decision flip) succeed silently.

    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345

    with mock.patch("tick.git", return_value="/repo"):
        with mock.patch("tick.pipeline.atomic_write"):
            with mock.patch("os.makedirs"):
                with mock.patch("tick._launch_claude_engine", return_value=fake_proc):
                    with mock.patch("tick._publish_marker_or_revert", return_value=True):
                        with mock.patch(
                            "tick.relabel", side_effect=fake_relabel,
                        ) as relabel_mock:
                            with mock.patch("tick._terminate_engine", return_value=False):
                                with mock.patch("tick.cleanup_scratch"):
                                    _dispatch_one_implementer(
                                        issue_number=99,
                                        issue_title="Test",
                                        issue_body="Body",
                                        tick_log=None,
                                        impl_engine="claude",
                                    )

    all_calls = [c[0] for c in relabel_mock.call_args_list]
    assert ("issue", 99, "ai:available", "ai:needs-human-decision") in all_calls, (
        f"Expected escalation to ai:needs-human-decision; got relabel calls: {all_calls}"
    )


def test_dispatch_one_fixer_escalates_when_label_flip_fails_and_kill_fails(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer must relabel to ai:needs-human-decision on unkillable engine.

    Same invariant as the implementer: when label flip fails and kill fails,
    the PR still shows ai:changes-requested and the next tick would re-dispatch.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    def fake_relabel(entity: str, number: int, remove: str, add: str) -> None:
        if add == "ai:fixing":
            raise subprocess.CalledProcessError(1, "gh")

    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345

    with mock.patch("tick.git", side_effect=fixer_git(tmp_path)):
        with mock.patch("tick.gh_text", return_value="main"):
            with mock.patch("tick.gh_json", side_effect=fixer_gh_json(comments=[
                {"body": "<!-- ai-pipeline-review -->\nFindings: none"},
            ])):
                with mock.patch("tick.pipeline.REVIEW_MARKER", "<!-- ai-pipeline-review -->"):
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch("os.makedirs"):
                            with mock.patch(
                                "tick._launch_claude_engine", return_value=fake_proc,
                            ):
                                with mock.patch(
                                    "tick._publish_marker_or_revert", return_value=True,
                                ):
                                    with mock.patch(
                                        "tick.relabel", side_effect=fake_relabel,
                                    ) as relabel_mock:
                                        with mock.patch(
                                            "tick._terminate_engine", return_value=False,
                                        ):
                                            with mock.patch("tick.cleanup_scratch"):
                                                _dispatch_one_fixer(
                                                    pr_number=55,
                                                    tick_log=None,
                                                )

    all_calls = [c[0] for c in relabel_mock.call_args_list]
    assert ("pr", 55, "ai:changes-requested", "ai:needs-human-decision") in all_calls, (
        f"Expected escalation to ai:needs-human-decision; got relabel calls: {all_calls}"
    )


def test_dispatch_one_fixer_escalates_when_engine_resolve_fails(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer escalates to ai:needs-human-decision when
    _resolve_engine raises RuntimeError (missing DeepSeek credentials).

    Without escalation the PR stays on ai:changes-requested and every
    subsequent tick retries the same setup failure in an infinite loop.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        with mock.patch("tick.WT_ROOT", str(wt_root)):
            with mock.patch("tick.git", side_effect=fixer_git(tmp_path)):
                with mock.patch("tick.gh_text", return_value="main"):
                    with mock.patch(
                        "tick.gh_json",
                        side_effect=fixer_gh_json(
                            comments=[
                                {"body": "<!-- pr-pipeline:review -->\nfixes"},
                            ],
                        ),
                    ):
                        with mock.patch(
                            "tick.pipeline.REVIEW_MARKER",
                            "<!-- pr-pipeline:review -->",
                        ):
                            with mock.patch("tick.pipeline.atomic_write"):
                                with mock.patch(
                                    "tick._resolve_engine",
                                    side_effect=RuntimeError("no key"),
                                ):
                                    with mock.patch(
                                        "tick.gh",
                                        side_effect=allow_escalation_comment(),
                                    ), mock.patch(
                                        "tick.relabel",
                                    ) as relabel_mock:
                                        with mock.patch(
                                            "tick.cleanup_scratch",
                                        ):
                                            _dispatch_one_fixer(
                                                pr_number=55,
                                                tick_log=None,
                                                impl_engine="claude-ds",
                                            )

    all_calls = [c[0] for c in relabel_mock.call_args_list]
    assert ("pr", 55, "ai:changes-requested", "ai:needs-human-decision") in all_calls, (
        f"Expected escalation to ai:needs-human-decision; "
        f"got relabel calls: {all_calls}"
    )


# --- Startup probe and exception handler ---


def test_main_logs_startup_context_as_first_entry(tmp_path: Path) -> None:
    """main() must write startup context (Python ver, cwd, PATH, PID) as first log entry.

    Without this, you cannot tell from the tick log what environment ran the
    tick — the first things you reach for when a tick mysteriously fails.
    """
    import tick  # type: ignore[import-untyped]
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels", return_value=[],
                    ):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    result = main()

    assert result == 0
    # The tick log directory was created under the anchored WT_ROOT
    tick_log_dir = Path(tick.TICK_LOG_DIR)
    assert tick_log_dir.is_dir(), "tick log directory must exist"

    # Find the log file
    log_files = sorted(tick_log_dir.glob("tick-*.jsonl"))
    assert log_files, "at least one tick log file must exist"

    entries: list[dict[str, object]] = []
    with open(log_files[-1]) as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    assert entries, "tick log must have at least one entry"
    first = entries[0]
    assert first["action"] == "startup_context", (
        f"first log entry must be startup_context, got {first.get('action')!r}"
    )
    assert "python_version" in first, "startup_context must include python_version"
    assert "cwd" in first, "startup_context must include cwd"
    assert isinstance(first.get("cwd"), str)
    assert "path" in first, "startup_context must include path"
    assert isinstance(first.get("path"), list)
    assert "pid" in first, "startup_context must include tick PID"
    assert isinstance(first.get("pid"), int)
    assert first["phase"] == "startup"
    assert first["pr"] is None


def test_main_startup_probe_runs_and_logs_results(tmp_path: Path) -> None:
    """main() must probe gh, git, and codex reachability at startup and log results.

    An E2BIG or missing-binary failure kills the process before any tick logic
    runs. Recording probe results as the second log entry gives the operator a
    baseline to correlate OS-level issues.
    """
    import tick  # type: ignore[import-untyped]
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels", return_value=[],
                    ):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    result = main()

    assert result == 0
    tick_log_dir = Path(tick.TICK_LOG_DIR)
    log_files = sorted(tick_log_dir.glob("tick-*.jsonl"))
    entries: list[dict[str, object]] = []
    with open(log_files[-1]) as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    probe_entries = [e for e in entries if e.get("action") == "startup_probe"]
    assert len(probe_entries) == 1, (
        f"Expected exactly one startup_probe entry, got {len(probe_entries)}"
    )
    probe = probe_entries[0]
    assert probe["phase"] == "startup"
    assert probe["pr"] is None
    assert "gh_found" in probe
    assert "git_found" in probe
    assert "codex_found" in probe
    assert isinstance(probe["gh_found"], bool)
    assert isinstance(probe["git_found"], bool)
    assert isinstance(probe["codex_found"], bool)


def test_main_startup_probe_fails_when_gh_missing(tmp_path: Path) -> None:
    """main() must exit with non-zero and log an error when gh is not on PATH.

    A missing binary at startup means every gh/git call in the tick will fail.
    Failing fast with a clear message is better than silently skipping every
    dispatch phase.
    """
    import tick  # type: ignore[import-untyped]
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("shutil.which") as which_mock:
            # gh is missing, git and codex are present
            which_mock.side_effect = lambda name: {
                "gh": None,
                "git": "/usr/bin/git",
                "codex": "/usr/local/bin/codex",
            }.get(name)
            with mock.patch("tick.harvest_reviewers") as hr:
                with mock.patch("tick.harvest_implementers") as hi:
                    with mock.patch("tick.harvest_fixers") as hf:
                        result = main()

    assert result != 0, "main must exit non-zero when a required binary is missing"
    # Harvest must not run — fail fast before spending any work
    hr.assert_not_called()
    hi.assert_not_called()
    hf.assert_not_called()

    # The tick log must record the failure
    tick_log_dir = Path(tick.TICK_LOG_DIR)
    log_files = sorted(tick_log_dir.glob("tick-*.jsonl"))
    entries: list[dict[str, object]] = []
    with open(log_files[-1]) as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    probe_entries = [e for e in entries if e.get("action") == "startup_probe"]
    assert probe_entries, "startup_probe must be logged even on failure"
    assert probe_entries[0]["gh_found"] is False


def test_main_exception_handler_logs_traceback(tmp_path: Path) -> None:
    """main() must catch unexpected exceptions, log the traceback, and exit non-zero.

    Without this, a bug in new dispatch logic kills the tick with a Python
    traceback to stderr only — no tick log entry. The next loop iteration
    starts fresh with no record of what failed.
    """
    import tick  # type: ignore[import-untyped]
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.side_effect = ValueError("simulated unexpected error")
            with mock.patch("tick.harvest_implementers"):
                with mock.patch("tick.harvest_fixers"):
                    result = main()

    assert result != 0, "main must exit non-zero on unhandled exception"

    # The tick log must record the crash
    tick_log_dir = Path(tick.TICK_LOG_DIR)
    log_files = sorted(tick_log_dir.glob("tick-*.jsonl"))
    entries: list[dict[str, object]] = []
    with open(log_files[-1]) as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    crash_entries = [e for e in entries if e.get("action") == "tick_crash"]
    assert len(crash_entries) == 1, (
        f"Expected exactly one tick_crash entry, got {len(crash_entries)}"
    )
    crash = crash_entries[0]
    assert crash["phase"] == "startup"
    assert crash["pr"] is None
    assert "error_type" in crash
    assert crash["error_type"] == "ValueError"
    assert "error_message" in crash
    assert "simulated unexpected error" in str(crash["error_message"])
    assert "traceback" in crash
    assert "ValueError" in str(crash["traceback"])


def test_main_exception_handler_still_prints_summary(tmp_path: Path) -> None:
    """main() must print a human-readable crash line to stderr after a crash.

    The unattended loop sees only the exit code and stderr. A clear summary
    line like "Tick crashed: ValueError: ..." tells the operator what happened.
    The misleading _print_summary output ("Tick complete. Status: FAILED") is
    deliberately skipped in the crash path.
    """
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.side_effect = ValueError("simulated unexpected error")
            with mock.patch("tick.harvest_implementers"):
                with mock.patch("tick.harvest_fixers"):
                    with mock.patch("sys.stderr", new_callable=mock.MagicMock) as stderr_mock:
                        result = main()

    assert result != 0
    # Verify the crash message was printed to stderr — this is the only
    # human-readable signal the unattended loop sees.
    write_calls = [
        c[0][0] for c in stderr_mock.write.call_args_list
        if isinstance(c[0], tuple) and c[0] and isinstance(c[0][0], str)
    ]
    crash_lines = [s for s in write_calls if "Tick crashed:" in s]
    assert crash_lines, (
        f"Expected 'Tick crashed: ValueError: simulated unexpected error' "
        f"in stderr output, got: {write_calls}"
    )
    assert any(
        "ValueError" in s and "simulated unexpected error" in s
        for s in crash_lines
    ), f"Crash line missing error details: {crash_lines}"


def test_startup_probe_logs_missing_binary_paths(tmp_path: Path) -> None:
    """When a binary is found, the probe must record its resolved path.

    Knowing that ``gh`` resolves to ``/opt/homebrew/bin/gh`` vs
    ``/usr/local/bin/gh`` matters when debugging PATH issues.
    """
    import tick  # type: ignore[import-untyped]
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("shutil.which") as which_mock:
            which_mock.side_effect = lambda name: {
                "gh": "/opt/homebrew/bin/gh",
                "git": "/usr/bin/git",
                "codex": "/usr/local/bin/codex",
            }.get(name)
            with mock.patch("tick.harvest_reviewers", return_value=True):
                with mock.patch("tick.harvest_implementers", return_value=True):
                    with mock.patch("tick.harvest_fixers", return_value=True):
                        with mock.patch(
                            "tick.read_authoritative_in_flight_labels",
                            return_value=[],
                        ):
                            with mock.patch("tick.dispatch_reviewers"):
                                with mock.patch("tick.dispatch_implementers"):
                                    with mock.patch("tick.dispatch_fixers"):
                                        result = main()

    assert result == 0
    tick_log_dir = Path(tick.TICK_LOG_DIR)
    log_files = sorted(tick_log_dir.glob("tick-*.jsonl"))
    entries: list[dict[str, object]] = []
    with open(log_files[-1]) as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    probe_entries = [e for e in entries if e.get("action") == "startup_probe"]
    assert len(probe_entries) == 1
    probe = probe_entries[0]
    assert probe["gh_path"] == "/opt/homebrew/bin/gh"
    assert probe["git_path"] == "/usr/bin/git"
    assert probe["codex_path"] == "/usr/local/bin/codex"


# --- tick-state line ---


def test_print_summary_emits_tick_state_line_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_summary emits a parseable tick-state line for successful ticks."""
    import pipeline
    from tick import _print_summary  # type: ignore[import-untyped]

    state = pipeline.derive_tick_state(
        status="ok", in_flight_count=2, has_actionable_queue=True,
    )
    _print_summary(None, reconcile_ok=True, tick_state=state)
    captured = capsys.readouterr()
    assert "tick-state: status=ok in_flight=2 queue=true quiescent=false" in captured.err


def test_print_summary_emits_tick_state_line_failed(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_summary emits unknown fields on FAILED path."""
    import pipeline
    from tick import _print_summary  # type: ignore[import-untyped]

    state = pipeline.derive_tick_state(status="FAILED")
    _print_summary(None, reconcile_ok=False, tick_state=state)
    captured = capsys.readouterr()
    expected = (
        "tick-state: status=FAILED"
        " in_flight=unknown queue=unknown quiescent=unknown"
    )
    assert expected in captured.err


def test_print_summary_emits_tick_state_line_partial(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_summary emits unknown fields on PARTIAL path."""
    import pipeline
    from tick import _print_summary  # type: ignore[import-untyped]

    state = pipeline.derive_tick_state(status="PARTIAL")
    _print_summary(None, reconcile_ok=True, dispatch_skipped=True, tick_state=state)
    captured = capsys.readouterr()
    expected = (
        "tick-state: status=PARTIAL"
        " in_flight=unknown queue=unknown quiescent=unknown"
    )
    assert expected in captured.err


def test_log_tick_summary_includes_tick_state(tmp_path: Path) -> None:
    """_log_tick_summary writes tick_state fields into the tick log entry."""
    import pipeline
    from tick import TickLog, _log_tick_summary  # type: ignore[import-untyped]

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)
    state = pipeline.derive_tick_state(
        status="ok", in_flight_count=1, has_actionable_queue=True,
    )
    _log_tick_summary(tick_log, reconcile_ok=True, tick_state=state)

    entries = tick_log.tail()
    summary = [e for e in entries if e.get("action") == "tick_complete"]
    assert len(summary) == 1
    details = summary[0].get("details")
    assert isinstance(details, dict)
    ts = details.get("tick_state")
    assert isinstance(ts, dict)
    assert ts == {
        "status": "ok",
        "in_flight": "1",
        "queue": "true",
        "quiescent": "false",
        "held": "unknown",
    }


def test_log_tick_summary_unknown_tick_state(tmp_path: Path) -> None:
    """_log_tick_summary with failed tick state writes unknown fields."""
    import pipeline
    from tick import TickLog, _log_tick_summary  # type: ignore[import-untyped]

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)
    state = pipeline.derive_tick_state(status="FAILED")
    _log_tick_summary(tick_log, reconcile_ok=False, tick_state=state)

    entries = tick_log.tail()
    summary = [e for e in entries if e.get("action") == "tick_complete"]
    assert len(summary) == 1
    details = summary[0].get("details")
    assert isinstance(details, dict)
    ts = details.get("tick_state")
    assert isinstance(ts, dict)
    assert ts == {
        "status": "FAILED",
        "in_flight": "unknown",
        "queue": "unknown",
        "quiescent": "unknown",
        "held": "unknown",
    }


def test_log_tick_summary_counts_kill_escalate_as_escalated(tmp_path: Path) -> None:
    """kill_escalate actions are included in the escalated summary counter."""
    from tick import TickLog, _log_tick_summary  # type: ignore[import-untyped]

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    # Simulate a tick that had one escalate and one kill_escalate action.
    tick_log.log(
        pr=10, phase="harvest", stage="implement",
        action="escalate", reason="test",
    )
    tick_log.log(
        pr=20, phase="harvest", stage="implement",
        action="kill_escalate", reason="hung engine",
    )
    _log_tick_summary(tick_log, reconcile_ok=True)

    entries = tick_log.tail()
    summary = [e for e in entries if e.get("action") == "tick_complete"]
    assert len(summary) == 1
    details = summary[0].get("details")
    assert isinstance(details, dict)
    assert details["escalated"] == 2


def test_surface_retained_scratch_dirs_emits_entries(tmp_path: Path) -> None:
    """_surface_retained_scratch_dirs emits a log entry per orphaned dir."""
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _surface_retained_scratch_dirs,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    # Dir with no marker at all — retained scratch
    (wt_root / "issue-42").mkdir()
    # Dir with a dead marker (no live PID, no result)
    dead_dir = wt_root / "pr-99"
    dead_dir.mkdir()
    (dead_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "review",
        "pr_number": 99,
        "deadline": 1000.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:implemented",
        "dispatch_id": "review-99-1000",
        "pid": 99999,
        "start_time": 1000.0,
        "pgid": 99999,
    }))
    # Non-worktree dir — should be ignored
    (wt_root / ".tick-log").mkdir()

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    with mock.patch("tick.process_control.get_process_start_time", return_value=None):
        with mock.patch("tick.process_control.pid_is_alive", return_value=False):
            _surface_retained_scratch_dirs(str(wt_root), tick_log)

    entries = tick_log.tail()
    retained = [e for e in entries if e.get("action") == "retained_scratch"]
    assert len(retained) == 2
    retained_dirs = {e.get("details", {}).get("wt_dir") for e in retained}
    assert str(wt_root / "issue-42") in retained_dirs
    assert str(wt_root / "pr-99") in retained_dirs


def test_run_tick_body_calls_surface_retained(tmp_path: Path) -> None:
    """_run_tick_body invokes _surface_retained_scratch_dirs before harvest."""
    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    with mock.patch("tick._surface_retained_scratch_dirs") as surface_mock:
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch("tick.read_authoritative_in_flight_labels",
                                    return_value=[]):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    with mock.patch("tick._compute_post_dispatch_state",
                                                    return_value=(0, False, ())):
                                        _run_tick_body(tick_log, None, "claude")
    surface_mock.assert_called_once()


def test_run_tick_body_publishes_a_setup_failure_before_holding_the_issue(
    tmp_path: Path,
) -> None:
    """The whole tick body, against an issue whose dispatch cannot be set up.

    Everything above this test drives the escalation helpers or one
    ``_dispatch_one_*``. This drives the phase the operator actually runs: real
    queue read, real eligibility gate, real admission check, real ``gh`` seam.
    A helper that publishes correctly but is never reached from here would leave
    the contract exactly as broken as it was, and the emitted argv is what
    proves the comment and the label went to the same item in that order.
    """
    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    def fake_gh_json(*args: str) -> object:
        if args[:2] == ("issue", "list"):
            return [{"number": 42, "title": "Add a thing", "author": {"login": TRUSTED_AUTHOR}}]
        return {"author": {"login": TRUSTED_AUTHOR}, "labels": [{"name": "ai:available"}]}

    def fake_gh_text(*args: str) -> str:
        # The eligibility gate reads the item's own label set; everything else
        # reading text here is the issue body for the prompt.
        return "ai:available" if "labels" in args else "issue body"

    def fake_git(*args: str) -> str:
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo_root)
        if "fetch" in args:
            raise subprocess.CalledProcessError(1, ["git", "fetch"])
        return ""

    calls: list[str] = []
    bodies: list[str] = []

    def record_gh(*args: str) -> object:
        if args[1] == "comment":
            calls.append(f"comment:{args[0]}:{args[2]}")
            with open(args[4]) as fh:
                bodies.append(fh.read())
        elif args[1] == "edit":
            calls.append(f"edit:{args[0]}:{args[2]}:{args[6]}->{args[4]}")
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=0, stdout="",
        )

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.gh", side_effect=record_gh):
            with mock.patch("tick.gh_json", side_effect=fake_gh_json):
                with mock.patch("tick.gh_text", side_effect=fake_gh_text):
                    with mock.patch("tick.git", side_effect=fake_git):
                        with mock.patch("tick.harvest_reviewers", return_value=True):
                            with mock.patch(
                                "tick.harvest_implementers", return_value=True,
                            ):
                                with mock.patch(
                                    "tick.harvest_fixers", return_value=True,
                                ):
                                    with mock.patch(
                                        "tick.read_authoritative_in_flight_labels",
                                        return_value=[],
                                    ):
                                        with mock.patch("tick.dispatch_reviewers"):
                                            with mock.patch("tick.dispatch_fixers"):
                                                with mock.patch(
                                                    "tick._compute_post_dispatch_state",
                                                    return_value=(0, False, ()),
                                                ):
                                                    _run_tick_body(
                                                        tick_log, None, "claude",
                                                    )

    assert calls == [
        "comment:issue:42",
        "edit:issue:42:ai:available->ai:needs-human-decision",
    ]
    assert "dispatch-setup-failed" in bodies[0]
    assert "no engine output exists" in bodies[0]


def test_reconcile_stale_labels_retains_implementing_despite_an_open_pr(
    tmp_path: Path,
) -> None:
    """An open closing PR does not prove implementation custody ended.

    ``gh search prs "Closes #N"`` is a full-text match, so it hits drafts, PRs
    under active revision, and PRs that merely mention the string. None of
    those mean the item stopped being implemented — and the marker is absent on
    every tick run from another machine or after a manual pickup. Removing the
    label here would also leave the issue carrying no pipeline label at all,
    violating the one-pipeline-label-per-item rule.
    """
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _reconcile_stale_in_flight_labels,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    # Issue 42 has ai:implementing but no local marker (issue-42 dir).
    # A PR exists that references it.
    gh_calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        gh_calls.append(args)
        return subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")

    def fake_gh_json(*args: str) -> object:
        # When querying for PRs that close issue 42
        if "search" in args:
            return [{"number": 99}]
        # Point-read: the label really is still on the issue.
        return {"labels": [{"name": "ai:implementing"}]}

    with mock.patch("tick.gh", side_effect=fake_gh):
        with mock.patch("tick.gh_json", side_effect=fake_gh_json):
            _reconcile_stale_in_flight_labels(
                stale_refs=[
                    {"kind": "issue", "number": 42, "label": "ai:implementing"},
                ],
                tick_log=tick_log,
            )

    assert gh_calls == [], "markerless reconciliation must not mutate GitHub"

    entries = tick_log.tail()
    retained = [e for e in entries if e.get("action") == "stale_label_retained"]
    assert len(retained) == 1
    assert not [e for e in entries if e.get("action") == "stale_label_reconciled"]


def test_run_tick_body_does_not_mutate_a_markerless_issue_with_an_open_pr(
    tmp_path: Path,
) -> None:
    """The incident shape, driven through the real tick body.

    An empty marker root plus an authoritative ``ai:implementing`` issue that
    has an open closing PR is exactly what a tick run from a sibling worktree
    sees while another machine implements normally. No GitHub mutation may
    follow from it.
    """
    from pipeline import InFlightRef  # type: ignore[attr-defined]
    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    auth_labels = [
        InFlightRef(kind="issue", number=42, label="ai:implementing"),
    ]

    def fake_gh_json(*args: str) -> object:
        if "search" in args:
            return [{"number": 99}]
        return {"labels": [{"name": "ai:implementing"}]}

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.gh") as gh_mock:
            with mock.patch("tick.gh_json", side_effect=fake_gh_json):
                with mock.patch("tick.harvest_reviewers", return_value=True):
                    with mock.patch("tick.harvest_implementers", return_value=True):
                        with mock.patch("tick.harvest_fixers", return_value=True):
                            with mock.patch("tick.read_authoritative_in_flight_labels",
                                            return_value=auth_labels):
                                with mock.patch("tick.dispatch_reviewers"):
                                    with mock.patch("tick.dispatch_implementers"):
                                        with mock.patch("tick.dispatch_fixers"):
                                            with mock.patch(
                                                "tick._compute_post_dispatch_state",
                                                return_value=(0, False, ()),
                                            ):
                                                _run_tick_body(tick_log, None, "claude")

    gh_mock.assert_not_called()

    entries = tick_log.tail()
    assert [e for e in entries if e.get("action") == "stale_label_retained"]
    assert not [
        e for e in entries
        if e.get("action") in ("stale_label_reconciled", "stale_label_escalated")
    ]


def test_run_tick_body_dispatches_past_four_foreign_in_flight_labels(
    tmp_path: Path,
) -> None:
    """The 2026-08-20 incident, driven through the real admission gate.

    Four hand-set ``ai:implementing`` issues and an empty marker root is the
    exact repo state that starved every dispatch: the union contract counted
    them as four paid engines against the default ``max_in_flight = 3``.

    Nothing on the admission path is stubbed here. ``dispatch_reviewers`` runs
    for real and its own ``can_dispatch_now`` call decides; the run is stopped
    one step *past* admission, at the trusted-candidate read, so reaching that
    read is itself the proof the cap allowed the launch. Before the change this
    logged ``skip / admission: max_in_flight`` and never reached it.
    """
    import pipeline as _pipeline
    from pipeline import InFlightRef  # type: ignore[attr-defined]
    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    # Repo-wide in-flight state, exactly as the incident had it: four issues
    # picked up by hand, none of them this machine's, none with a local marker.
    foreign = [
        InFlightRef(kind="issue", number=n, label="ai:implementing")
        for n in (502, 504, 507, 608)
    ]
    assert len(foreign) > 3, "the incident had more foreign labels than the cap"

    trusted_gate_reads: list[int] = []

    def fake_gh_text(*args: str) -> str:
        # Reviewer pickup queue: one eligible PR.
        if args[:2] == ("pr", "list") and _pipeline.LABEL_IMPLEMENTED in args:
            return "617\n"
        # Eligibility gate reads the item's own label set.
        if args[:3] == ("pr", "view", "617") and "labels" in args:
            return "ai:implemented\n"
        return ""

    def fake_gh_json(*args: str) -> object:
        if args[:3] == ("pr", "view", "617") and "author,headRepository,isCrossRepository" in args:
            trusted_gate_reads.append(617)
            # Stop here: past admission, before anything is spawned.
            raise subprocess.CalledProcessError(1, "gh")
        if "search" in args:
            return []
        return {"labels": [{"name": "ai:implemented"}]}

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.gh") as gh_mock:
            with mock.patch("tick.gh_text", side_effect=fake_gh_text):
                with mock.patch("tick.gh_json", side_effect=fake_gh_json):
                    with mock.patch("tick.verify_codex_bin"):
                        with mock.patch("tick.harvest_reviewers", return_value=True):
                            with mock.patch("tick.harvest_implementers", return_value=True):
                                with mock.patch("tick.harvest_fixers", return_value=True):
                                    with mock.patch(
                                        "tick.read_authoritative_in_flight_labels",
                                        return_value=list(foreign),
                                    ):
                                        _run_tick_body(tick_log, None, "claude")

    entries = tick_log.tail()

    # The defect: four foreign labels filling a cap of three.
    assert not [
        e for e in entries
        if e.get("action") == "skip" and "admission" in str(e.get("reason", ""))
    ], "a shared ai: label must not consume this machine's spend cap"

    # Positive proof the reviewer got past admission rather than finding an
    # empty queue: only a passed cap reaches the trusted-candidate read.
    assert trusted_gate_reads == [617]

    # Cadence agrees with admission — computed by the real helper, not stubbed.
    # tick_state is the operator-facing line, so its values are emitted as
    # strings; asserting the emitted form keeps this honest about the contract.
    summary = [e for e in entries if e.get("action") == "tick_complete"]
    assert summary, "tick must emit its summary"
    state = summary[-1].get("details", {}).get("tick_state", {})
    assert state.get("in_flight") == "0"
    assert state.get("queue") == "true"

    gh_mock.assert_not_called()


def test_run_tick_body_dispatches_nothing_when_every_queue_is_mid_transition(
    tmp_path: Path,
) -> None:
    """The whole tick, all three stages, against partial label transitions.

    Every dispatch queue holds exactly one item caught mid-convergence on
    another machine: its pickup label and its in-flight label, no local
    marker. This drives the real DISPATCH phase — the dispatchers are not
    stubbed, only the ``_dispatch_one_*`` launchers underneath them — so the
    admission check, the marker scan, and the trusted-candidate gate all run
    for real.

    ``read_authoritative_in_flight_labels`` returns nothing on purpose: that
    is the search-index-lag case, where the tick's global in-flight view has
    not caught up to a label the queue listing already reflects. Nothing but
    each candidate's own point-read can stop the second engine here.
    """
    import tick  # type: ignore[import-untyped]
    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    labels_by_number = {
        77: ["ai:available", "ai:implementing"],
        88: ["ai:revised", "ai:reviewing"],
        99: ["ai:changes-requested", "ai:fixing"],
    }

    def numbers_for_label(label: str) -> list[int]:
        return [n for n, ls in labels_by_number.items() if label in ls]

    def label_argument(args: tuple[str, ...]) -> str:
        return args[args.index("--label") + 1]

    def fake_gh_text(*args: str) -> str:
        if "list" in args:
            return "\n".join(str(n) for n in numbers_for_label(label_argument(args)))
        # Point-read of one item's complete label set.
        return "\n".join(labels_by_number[int(args[2])])

    def fake_gh_json(*args: str) -> object:
        if "search" in args:
            return []
        if "list" in args:
            return [
                {"number": n, "title": f"Candidate {n}", "author": {"login": TRUSTED_AUTHOR}}
                for n in numbers_for_label(label_argument(args))
            ]
        return {
            "author": {"login": tick.TRUSTED_REVIEW_AUTHOR},
            "headRepository": {"nameWithOwner": tick.TRUSTED_HEAD_REPO},
            "isCrossRepository": False,
        }

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.gh") as gh_mock:
            with mock.patch("tick.gh_text", side_effect=fake_gh_text):
                with mock.patch("tick.gh_json", side_effect=fake_gh_json):
                    with mock.patch("tick.verify_codex_bin"):
                        with mock.patch("tick.harvest_reviewers", return_value=True):
                            with mock.patch("tick.harvest_implementers", return_value=True):
                                with mock.patch("tick.harvest_fixers", return_value=True):
                                    with mock.patch(
                                        "tick.read_authoritative_in_flight_labels",
                                        return_value=[],
                                    ):
                                        with mock.patch(
                                            "tick._dispatch_one_reviewer",
                                        ) as review_one:
                                            with mock.patch(
                                                "tick._dispatch_one_implementer",
                                            ) as implement_one:
                                                with mock.patch(
                                                    "tick._dispatch_one_fixer",
                                                ) as fix_one:
                                                    _run_tick_body(
                                                        tick_log, None, "claude",
                                                    )

    review_one.assert_not_called()
    implement_one.assert_not_called()
    fix_one.assert_not_called()
    gh_mock.assert_not_called()
    assert not any(wt_root.iterdir()), "no scratch may be created or removed"

    skips = [
        e for e in tick_log.tail()
        if e.get("phase") == "dispatch" and e.get("action") == "skip"
    ]
    assert {e.get("stage") for e in skips} == {"implement", "review", "fix"}
    assert all(
        "in-flight label present" in str(e.get("reason")) for e in skips
    ), skips


def test_run_tick_body_does_not_dispatch_a_pr_it_already_launched_this_tick(
    tmp_path: Path,
) -> None:
    """One launch tracker spans all three stages, not one per dispatcher.

    The reviewer launches against PR 88 and moves it to ``ai:reviewing``. The
    fix queue listing and the fix-stage point-read both still show the older
    label set — the eventual-consistency lag this PR designs for, here across
    two reads inside one tick. Nothing in GitHub's answer can stop the second
    launch; only the tick's own record of what it just launched can.

    If each dispatcher keeps its own copy of the in-flight list, that record is
    lost between stages. The fixer then reaches
    ``_existing_marker_blocks_dispatch`` with the reviewer's live marker, whose
    ``stage`` is ``review`` — read as a structurally invalid marker, which
    escalates the PR under active review to ``ai:needs-human-decision``.
    """
    import tick  # type: ignore[import-untyped]
    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    # Both queue listings select PR 88; the two point-reads disagree, because
    # the second is served by an index that has not caught up.
    queue_numbers = {"ai:implemented": [88], "ai:changes-requested": [88]}
    point_reads = iter([["ai:implemented"], ["ai:changes-requested"]])

    def label_argument(args: tuple[str, ...]) -> str:
        return args[args.index("--label") + 1]

    def fake_gh_text(*args: str) -> str:
        if "list" in args:
            return "\n".join(
                str(n) for n in queue_numbers.get(label_argument(args), [])
            )
        return "\n".join(next(point_reads))

    def fake_gh_json(*args: str) -> object:
        if "search" in args:
            return []
        if "list" in args:
            return [
                {"number": n, "title": f"Candidate {n}", "author": {"login": TRUSTED_AUTHOR}}
                for n in queue_numbers.get(label_argument(args), [])
            ]
        return {
            "author": {"login": tick.TRUSTED_REVIEW_AUTHOR},
            "headRepository": {"nameWithOwner": tick.TRUSTED_HEAD_REPO},
            "isCrossRepository": False,
        }

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.gh") as gh_mock:
            with mock.patch("tick.gh_text", side_effect=fake_gh_text):
                with mock.patch("tick.gh_json", side_effect=fake_gh_json):
                    with mock.patch("tick.verify_codex_bin"):
                        with mock.patch("tick.harvest_reviewers", return_value=True):
                            with mock.patch("tick.harvest_implementers", return_value=True):
                                with mock.patch("tick.harvest_fixers", return_value=True):
                                    with mock.patch(
                                        "tick.read_authoritative_in_flight_labels",
                                        return_value=[],
                                    ):
                                        with mock.patch(
                                            "tick._dispatch_one_reviewer",
                                        ) as review_one:
                                            with mock.patch(
                                                "tick._dispatch_one_fixer",
                                            ) as fix_one:
                                                _run_tick_body(
                                                    tick_log, None, "claude",
                                                )

    assert review_one.call_count == 1
    fix_one.assert_not_called()
    gh_mock.assert_not_called()

    fix_skips = [
        e for e in tick_log.tail()
        if e.get("phase") == "dispatch"
        and e.get("stage") == "fix"
        and e.get("action") == "skip"
    ]
    assert [e.get("reason") for e in fix_skips] == ["already in-flight this tick"]


def test_reconcile_stale_labels_retains_a_label_it_cannot_prove_it_owns(
    tmp_path: Path,
) -> None:
    """No local marker is not evidence of anything.

    This path escalated on the *absence* of a machine-local file, which is the
    steady state for every item another machine dispatched and for the
    documented manual ai:implementing pickup.
    """
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _reconcile_stale_in_flight_labels,
    )

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)
    gh_calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        gh_calls.append(args)
        return subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")

    def fake_gh_json(*args: str) -> object:
        if "view" in args:
            return {"labels": [{"name": "ai:implementing"}]}
        return []  # no closing PRs

    with mock.patch("tick.gh", side_effect=fake_gh):
        with mock.patch("tick.gh_json", side_effect=fake_gh_json):
            _reconcile_stale_in_flight_labels(
                stale_refs=[
                    {"kind": "issue", "number": 42, "label": "ai:implementing"},
                ],
                tick_log=tick_log,
            )

    assert gh_calls == []
    entries = tick_log.tail()
    assert not [e for e in entries if e.get("action") == "stale_label_escalated"]
    assert [e for e in entries if e.get("action") == "stale_label_retained"]


def test_reconcile_stale_labels_retains_a_markerless_reviewing_pr(
    tmp_path: Path,
) -> None:
    """The PR stages get the same custody rule as issues.

    A markerless ai:reviewing PR is most often another machine's live review;
    a reviewer engine costs money, and relabelling under it strands the result.
    """
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _reconcile_stale_in_flight_labels,
    )

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)
    gh_calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        gh_calls.append(args)
        return subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")

    def fake_gh_json(*args: str) -> object:
        if args and args[0] == "api":
            return []  # timeline: no dispatch receipt
        return {"labels": [{"name": "ai:reviewing"}]}

    with mock.patch("tick.gh", side_effect=fake_gh):
        with mock.patch("tick.gh_json", side_effect=fake_gh_json):
            _reconcile_stale_in_flight_labels(
                stale_refs=[
                    {"kind": "pr", "number": 99, "label": "ai:reviewing"},
                ],
                tick_log=tick_log,
            )

    assert gh_calls == []


def test_reconcile_stale_labels_skips_escalation_when_label_transitioned(
    tmp_path: Path,
) -> None:
    """Point-read confirming the label has moved skips escalation (race fix)."""
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _reconcile_stale_in_flight_labels,
    )

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    # Point-read returns the PR's current labels without ai:reviewing —
    # meaning harvest already transitioned it; escalation must be skipped.
    point_read_payload = {"labels": [{"name": "ai:ready-for-human"}]}

    with mock.patch("tick.gh_json", return_value=point_read_payload):
        with mock.patch("tick._relabel_with_context") as relabel_mock:
            _reconcile_stale_in_flight_labels(
                stale_refs=[
                    {"kind": "pr", "number": 99, "label": "ai:reviewing"},
                ],
                tick_log=tick_log,
            )

    relabel_mock.assert_not_called()
    superseded = [
        e for e in tick_log.tail()
        if e.get("action") == "stale_label_superseded"
    ]
    assert len(superseded) == 1
    assert "ai:reviewing" in str(superseded[0].get("reason"))


def test_reconcile_stale_labels_retains_label_when_point_read_fails(
    tmp_path: Path,
) -> None:
    """A failed authoritative point-read must fail closed: no escalation.

    The point-read exists because the list query's search index can be stale;
    escalating when the authoritative read is unavailable would act on exactly
    the stale row the read was meant to distrust. The label is retained and the
    next tick retries.
    """
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _reconcile_stale_in_flight_labels,
    )

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    transport_error = subprocess.CalledProcessError(1, ["gh"])
    with mock.patch("tick.gh_json", side_effect=transport_error):
        with mock.patch("tick._relabel_with_context") as relabel_mock:
            _reconcile_stale_in_flight_labels(
                stale_refs=[
                    {"kind": "pr", "number": 99, "label": "ai:reviewing"},
                ],
                tick_log=tick_log,
            )

    relabel_mock.assert_not_called()
    skipped = [
        e for e in tick_log.tail()
        if e.get("action") == "stale_reconcile_skipped"
    ]
    assert len(skipped) == 1
    assert "point-read" in str(skipped[0].get("reason"))


def test_reconcile_stale_labels_retains_label_on_malformed_point_read(
    tmp_path: Path,
) -> None:
    """A malformed/partial point-read payload must fail closed like a transport
    failure — never fall through to escalation on the stale indexed row."""
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _reconcile_stale_in_flight_labels,
    )

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    for malformed in (None, [], "junk", {"labels": "junk"}, {"nolabels": []}):
        with mock.patch("tick.gh_json", return_value=malformed):
            with mock.patch("tick._relabel_with_context") as relabel_mock:
                _reconcile_stale_in_flight_labels(
                    stale_refs=[
                        {"kind": "pr", "number": 99, "label": "ai:reviewing"},
                    ],
                    tick_log=tick_log,
                )
        relabel_mock.assert_not_called()

    skipped = [
        e for e in tick_log.tail()
        if e.get("action") == "stale_reconcile_skipped"
    ]
    assert len(skipped) == 5


def test_reconcile_stale_labels_retains_a_confirmed_reviewing_pr(
    tmp_path: Path,
) -> None:
    """Even a point-read-confirmed in-flight label is never relabelled here.

    The sweep's only evidence is "no local marker", which is the steady state
    for another machine's live dispatch. Confirming the label is real proves
    the label, not custody of it, so there is nothing this sweep may act on
    (issue #592).
    """
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _reconcile_stale_in_flight_labels,
    )

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    def fake_gh_json(*args: str) -> object:
        assert not (args and args[0] == "api"), (
            f"the sweep must not read the GitHub timeline: {args}"
        )
        return {"labels": [{"name": "ai:reviewing"}]}

    with mock.patch("tick.gh_json", side_effect=fake_gh_json):
        with mock.patch("tick._relabel_with_context") as relabel_mock:
            _reconcile_stale_in_flight_labels(
                stale_refs=[
                    {"kind": "pr", "number": 99, "label": "ai:reviewing"},
                ],
                tick_log=tick_log,
            )

    relabel_mock.assert_not_called()
    entries = tick_log.tail()
    assert [e for e in entries if e.get("action") == "stale_label_retained"]


def test_surface_retained_scratch_dirs_skips_live_engines(tmp_path: Path) -> None:
    """Dirs with live engines are not flagged as retained."""
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _surface_retained_scratch_dirs,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    live_dir = wt_root / "issue-10"
    live_dir.mkdir()
    (live_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "implement",
        "pr_number": 10,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "abc",
        "pre_label": "ai:available",
        "dispatch_id": "impl-10-1000",
        "pid": 99999,
        "start_time": 9999999999.0,
        "pgid": 99999,
    }))

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)

    with mock.patch("tick.process_control.get_process_start_time", return_value=9999999999.0):
        with mock.patch("tick.process_control.pid_is_alive", return_value=True):
            _surface_retained_scratch_dirs(str(wt_root), tick_log)

    entries = tick_log.tail()
    retained = [e for e in entries if e.get("action") == "retained_scratch"]
    assert len(retained) == 0


def test_run_tick_body_limits_stale_reconciliation_to_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A targeted tick never reconciles unrelated authoritative labels."""
    import pipeline
    import tick  # type: ignore[import-untyped]
    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(tick, "WT_ROOT", str(wt_root))
    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)
    tick_log = TickLog.open(log_dir)
    labels = [
        pipeline.InFlightRef(
            kind="issue", number=462, label=pipeline.LABEL_IMPLEMENTING,
        ),
        pipeline.InFlightRef(
            kind="pr", number=551, label=pipeline.LABEL_REVIEWING,
        ),
        pipeline.InFlightRef(
            kind="issue", number=99, label=pipeline.LABEL_IMPLEMENTING,
        ),
    ]

    stage_targets = {
        "reviewer": ("pr", 551),
        "implementer": ("issue", 462),
        "fixer": ("pr", 551),
    }
    with mock.patch("tick._resolve_stage_targets", return_value=stage_targets):
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels",
                        return_value=labels,
                    ):
                        with mock.patch(
                            "tick._reconcile_stale_in_flight_labels",
                        ) as reconcile_mock:
                            with mock.patch("tick.dispatch_reviewers"):
                                with mock.patch("tick.dispatch_implementers"):
                                    with mock.patch("tick.dispatch_fixers"):
                                        with mock.patch(
                                            "tick._compute_post_dispatch_state",
                                            return_value=(3, False, ()),
                                        ):
                                            _run_tick_body(
                                                tick_log,
                                                ("issue", 462),
                                                "claude",
                                            )

    reconcile_mock.assert_called_once_with(
        [
            {"kind": "issue", "number": 462, "label": "ai:implementing"},
            {"kind": "pr", "number": 551, "label": "ai:reviewing"},
        ],
        tick_log,
    )


def test_main_ok_path_emits_tick_state_line(tmp_path: Path) -> None:
    """main() return-0 path emits the tick-state line with computed state."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels",
                        return_value=[],
                    ):
                        with mock.patch("tick.dispatch_reviewers"):
                            with mock.patch("tick.dispatch_implementers"):
                                with mock.patch("tick.dispatch_fixers"):
                                    with mock.patch(
                                        "tick._compute_post_dispatch_state",
                                        return_value=(0, False, ()),
                                    ):
                                        import io
                                        stderr = io.StringIO()
                                        with mock.patch("sys.stderr", stderr):
                                            result = main()

    assert result == 0
    output = stderr.getvalue()
    assert "tick-state: status=ok in_flight=0 queue=false quiescent=true" in output


def test_main_reconcile_failed_emits_tick_state_unknown(tmp_path: Path) -> None:
    """main() return-1 path emits tick-state with unknown fields."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=False):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    import io
                    stderr = io.StringIO()
                    with mock.patch("sys.stderr", stderr):
                        result = main()

    assert result == 1
    output = stderr.getvalue()
    assert "tick-state: status=FAILED in_flight=unknown queue=unknown quiescent=unknown" in output
    assert "tick-cadence: next_wake=270 stop=false" in output


def test_main_auth_labels_failed_emits_tick_state_unknown(tmp_path: Path) -> None:
    """main() return-2 path emits tick-state with unknown fields."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels",
                        side_effect=subprocess.CalledProcessError(1, "gh"),
                    ):
                        import io
                        stderr = io.StringIO()
                        with mock.patch("sys.stderr", stderr):
                            result = main()

    assert result == 2
    output = stderr.getvalue()
    assert "tick-state: status=PARTIAL in_flight=unknown queue=unknown quiescent=unknown" in output
    assert "tick-cadence: next_wake=270 stop=false" in output


def test_main_startup_probe_failed_emits_tick_state(tmp_path: Path) -> None:
    """main() return-3 path emits tick-state with unknown fields."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick._startup_probe", return_value={"gh_found": False}):
            import io
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                result = main()

    assert result == 3
    output = stderr.getvalue()
    assert "tick-state: status=FAILED in_flight=unknown queue=unknown quiescent=unknown" in output


def test_main_refuses_to_tick_without_a_resolvable_target(tmp_path: Path) -> None:
    """A failed local Git target resolution stops the tick before GitHub access."""
    import tick

    with (
        mock.patch("tick.repo_root", return_value=str(tmp_path)),
        mock.patch("tick.resolve_target", side_effect=ValueError("no remote")),
        mock.patch("tick.gh") as gh,
        mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
    ):
        result = tick.main()

    assert result == 3
    assert "target resolution failed: no remote" in stderr.getvalue()
    assert "tick-state: status=FAILED" in stderr.getvalue()
    gh.assert_not_called()


@pytest.mark.parametrize("existing", [frozenset(), frozenset({"ai:available"})])
def test_main_rejects_missing_vocabulary_before_harvest(tmp_path, capsys, existing):
    import pipeline
    import tick

    with (
        mock.patch("tick.repo_root", return_value=str(tmp_path)),
        mock.patch("labels.read_labels", return_value=existing) as read,
        mock.patch("tick._run_tick_body", return_value=0) as body,
    ):
        result = tick.main()

    assert result == 3
    read.assert_called_once_with("test-owner/test-repo")
    body.assert_not_called()
    stderr = capsys.readouterr().err
    for name in pipeline.WORKFLOW_LABELS - existing:
        assert name in stderr
    assert "labels.py" in stderr and "provision" in stderr
    assert "tick-state: status=FAILED" in stderr
    entries = [json.loads(line) for path in Path(tick.TICK_LOG_DIR).glob("*.jsonl")
               for line in path.read_text().splitlines()]
    assert any(entry.get("action") == "startup_labels_invalid" for entry in entries)


def test_main_rejects_unreadable_vocabulary_before_harvest(tmp_path, capsys):
    import tick

    with (
        mock.patch("tick.repo_root", return_value=str(tmp_path)),
        mock.patch("labels.read_labels", side_effect=ValueError("permission denied")),
        mock.patch("tick._run_tick_body", return_value=0) as body,
    ):
        result = tick.main()

    assert result == 3
    body.assert_not_called()
    assert "permission denied" in capsys.readouterr().err


def test_main_checks_vocabulary_each_tick_and_accepts_unknown_labels(tmp_path):
    import pipeline
    import tick

    with (
        mock.patch("tick.repo_root", return_value=str(tmp_path)),
        mock.patch("labels.read_labels", side_effect=[
            pipeline.WORKFLOW_LABELS | {"ai:parked"}, frozenset(),
        ]) as read,
        mock.patch("tick._run_tick_body", return_value=0) as body,
    ):
        assert tick.main() == 0
        assert tick.main() == 3

    assert read.call_count == 2
    assert body.call_count == 1


def test_main_refuses_to_tick_without_a_trusted_author(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An unconfigured trusted author stops the tick too.

    It is the reviewer admission gate's whole input: unset, `validate_review_candidate`
    matches nobody and every PR escalates to a human — a pipeline that looks
    like it is running while silently reviewing nothing.

    Configured the way an operator configures it — by what is in the file this
    tick reads — rather than by patching the import-time constant, because the
    file is what the gate is there to check.
    """
    from tick import main  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text("[defaults]\nallow_touching = false\n", encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick._startup_probe"):
            import io
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                result = main()

    assert result == 3
    assert "[operator] login" in stderr.getvalue()


def test_main_ticks_under_the_login_the_config_now_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A `--loop` process reviews as whoever the *current* config says it is.

    Every other setting is resolved per tick so an edit takes effect on the
    next one. The identity may not be the exception: validating the new login
    at startup and then admitting candidates against the one this module
    resolved when it was imported would authorize under an identity the
    operator has already replaced — and, when they blank it, under one they
    have removed.
    """
    from tick import main  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[operator]\nlogin = "new-user"\n\n[repos."test-owner/test-repo"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick._startup_probe"):
            with mock.patch("tick.TRUSTED_REVIEW_AUTHOR", "old-user"):
                with mock.patch("tick._run_tick_body", return_value=0) as body:
                    result = main()

    assert result == 0
    assert body.call_args.kwargs["trusted_author"] == "new-user"


def test_main_refuses_a_login_removed_from_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Blanking the login stops the next tick rather than reusing the old one."""
    from tick import main  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text('[operator]\nlogin = ""\n', encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick._startup_probe"):
            with mock.patch("tick.TRUSTED_REVIEW_AUTHOR", "old-user"):
                with mock.patch("tick._run_tick_body", return_value=0) as body:
                    import io
                    stderr = io.StringIO()
                    with mock.patch("sys.stderr", stderr):
                        result = main()

    assert result == 3
    assert "[operator] login" in stderr.getvalue()
    body.assert_not_called()


@pytest.mark.parametrize(
    ("defaults", "repo_mode", "cli_mode", "expected"),
    [
        (None, None, "", "auto"),
        ("dontAsk", None, "", "dontAsk"),
        ("auto", "dontAsk", "", "dontAsk"),
        ("dontAsk", "auto", "", "auto"),
        ("auto", "auto", "dontAsk", "dontAsk"),
        ("dontAsk", "dontAsk", "auto", "auto"),
    ],
)
def test_main_resolves_permission_mode_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    defaults: str | None, repo_mode: str | None, cli_mode: str, expected: str,
) -> None:
    """The real tick sends the winning layer to both Claude stages only."""
    import pipeline
    import tick

    config_text = '[operator]\nlogin = "test-owner"\n'
    if defaults is not None:
        config_text += f'[defaults]\npermission_mode = "{defaults}"\n'
    config_text += f'[repos."{tick.TRUSTED_HEAD_REPO}"]\n'
    if repo_mode is not None:
        config_text += f'permission_mode = "{repo_mode}"\n'
    cfg = tmp_path / "config.toml"
    cfg.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    monkeypatch.setattr(
        pipeline, "_PACKAGED_CONFIG_PATH",
        Path(pipeline.__file__).resolve().parent.parent / "config.toml",
    )

    with (
        mock.patch("tick.git", return_value=str(tmp_path)),
        mock.patch("tick._startup_probe"),
        mock.patch("tick.harvest_reviewers", return_value=True),
        mock.patch("tick.harvest_implementers", return_value=True),
        mock.patch("tick.harvest_fixers", return_value=True),
        mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]),
        mock.patch("tick.dispatch_reviewers") as reviewers,
        mock.patch("tick.dispatch_implementers") as implementers,
        mock.patch("tick.dispatch_fixers") as fixers,
        mock.patch("tick._compute_post_dispatch_state", return_value=(0, False, [])),
    ):
        assert tick.main(permission_mode=cli_mode) == 0

    implementers.assert_called_once()
    fixers.assert_called_once()
    reviewers.assert_called_once()
    assert implementers.call_args.kwargs["permission_mode"] == expected
    assert fixers.call_args.kwargs["permission_mode"] == expected
    assert "permission_mode" not in reviewers.call_args.kwargs


@pytest.mark.parametrize("value", ['""', '"yolo"', '7', 'true', '[]'])
def test_main_refuses_a_malformed_permission_mode_before_any_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    value: str,
) -> None:
    """A malformed engine permission mode is refused before harvest or dispatch.

    Accepted, it passes the startup gate and raises inside the engine launch —
    after a dispatch lease and worktree exist, leaving a ``state="launching"``
    marker for the crash-recovery path to clean up. That is a legible config
    error wearing a tick crash's clothes.
    """
    from tick import main  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[operator]\nlogin = "userA"\n\n[defaults]\npermission_mode = {value}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick._startup_probe"):
            with mock.patch("tick._run_tick_body", return_value=0) as body:
                import io
                stderr = io.StringIO()
                with mock.patch("sys.stderr", stderr):
                    result = main()

    output = stderr.getvalue()
    assert result == 3
    assert "permission_mode" in output
    assert "invalid configuration" in output
    body.assert_not_called()


def test_main_refuses_to_tick_on_an_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A setting with no conservative reading stops the tick, naming the key.

    ``verify_tools`` is the sharp case: coerced to empty it would dispatch an
    engine with no verification permission, and the reviewer would still award
    ai:ready-for-human on the unverified result. Nothing about that errors, so
    it has to error here instead.
    """
    from tick import main  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text('[defaults]\nverify_tools = "uv run pytest"\n', encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick._startup_probe"):
            import io
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                result = main()

    output = stderr.getvalue()
    assert result == 3
    assert "verify_tools" in output
    assert "invalid configuration" in output


_TUNED_SECTION = (
    'implementer_timeout_seconds = 7200\n'
    'fixer_timeout_seconds = 5400\n'
    'review_timeout_seconds = 2400\n'
    'engine_stall_seconds = 900\n'
    'engine_stall_startup_grace_seconds = 60\n'
    'max_iterations = 2\n'
    'impl_engine = "claude"\n'
    'model = "opus"\n'
    'fix_engine = "claude-ds"\n'
    'review_model = "gpt-6"\n'
    'review_effort = "xhigh"\n'
    'loop_short_poll_seconds = 120\n'
    'loop_max_poll_seconds = 200\n'
    'loop_heartbeat_seconds = 3600\n'
    'loop_quiescent_stop_after = 5\n'
    'transient_cooldown_seconds = 900\n'
    'transient_cooldown_reset_buffer_seconds = 30\n'
)


def test_main_runs_every_stage_on_the_validated_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Each configured value reaches the consumer that enforces it.

    The deadline and stall limits reach the dispatchers that record them, the
    round limit and cooldowns reach HARVEST, each stage gets its own engine
    route, and the emitted cadence is paced from the configured intervals.
    """
    import tick  # type: ignore[import-untyped]

    write_tuned_config(monkeypatch, tmp_path, _TUNED_SECTION)
    stderr = io.StringIO()

    with tick_stage_mocks(tmp_path) as stages, mock.patch("sys.stderr", stderr):
        assert tick.main() == 0

    assert stages["harvest_reviewers"].call_args.kwargs["max_iterations"] == 2
    for stage in ("harvest_implementers", "harvest_fixers"):
        kwargs = stages[stage].call_args.kwargs
        assert kwargs["transient_cooldown_seconds"] == 900
        assert kwargs["transient_cooldown_reset_buffer_seconds"] == 30
    reviewer = stages["dispatch_reviewers"].call_args.kwargs
    assert (reviewer["timeout_seconds"], reviewer["review_model"],
            reviewer["review_effort"]) == (2400, "gpt-6", "xhigh")
    implementer = stages["dispatch_implementers"].call_args.kwargs
    assert (implementer["impl_engine"], implementer["model"],
            implementer["timeout_seconds"], implementer["stall_seconds"],
            implementer["stall_startup_grace_seconds"]) == (
        "claude", "opus", 7200, 900, 60)
    fixer = stages["dispatch_fixers"].call_args.kwargs
    assert (fixer["impl_engine"], fixer["model"], fixer["timeout_seconds"],
            fixer["stall_seconds"], fixer["stall_startup_grace_seconds"]) == (
        "claude-ds", "", 5400, 900, 60)
    # The post-dispatch state reports a queue: the configured short poll.
    assert "tick-cadence: next_wake=120 stop=false" in stderr.getvalue()


def test_main_route_flags_override_the_configured_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A typed flag beats every config layer for that run only."""
    import tick  # type: ignore[import-untyped]

    write_tuned_config(monkeypatch, tmp_path, _TUNED_SECTION)

    with tick_stage_mocks(tmp_path) as stages:
        assert tick.main(
            impl_engine="claude-ds", model="deepseek-v4-flash",
            fix_engine="claude", fix_model="sonnet",
            review_model="gpt-7", review_effort="low",
        ) == 0

    implementer = stages["dispatch_implementers"].call_args.kwargs
    fixer = stages["dispatch_fixers"].call_args.kwargs
    reviewer = stages["dispatch_reviewers"].call_args.kwargs
    assert (implementer["impl_engine"], implementer["model"]) == (
        "claude-ds", "deepseek-v4-flash")
    assert (fixer["impl_engine"], fixer["model"]) == ("claude", "sonnet")
    assert (reviewer["review_model"], reviewer["review_effort"]) == ("gpt-7", "low")


@pytest.mark.parametrize("text, key", [
    ('implementer_timeout_seconds = "7200"\n', "implementer_timeout_seconds"),
    ("review_timeout_seconds = 0\n", "review_timeout_seconds"),
    ("engine_stall_seconds = 600\n", "engine_stall_seconds"),
    ("engine_stall_seconds = 4000\n", "engine_stall_seconds"),
    ("max_iterations = true\n", "max_iterations"),
    ('impl_engine = "codex"\n', "impl_engine"),
    ('fix_model = "-p"\n', "fix_model"),
    ("loop_heartbeat_seconds = 300\n", "loop_heartbeat_seconds"),
    ("transient_cooldown_seconds = 1.5\n", "transient_cooldown_seconds"),
])
def test_main_refuses_an_invalid_tuning_value_before_any_harvest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, key: str,
) -> None:
    """An invalid value stops the tick at startup, naming the key.

    Nothing has been harvested, relabelled or dispatched: the refusal lands
    before the label check and before HARVEST.
    """
    import tick  # type: ignore[import-untyped]

    write_tuned_config(monkeypatch, tmp_path, text)
    verify = mock.Mock()
    monkeypatch.setattr(tick.labels, "verify", verify)
    stderr = io.StringIO()

    with tick_stage_mocks(tmp_path) as stages, mock.patch("sys.stderr", stderr):
        assert tick.main() == 3

    output = stderr.getvalue()
    assert "invalid configuration" in output
    assert key in output
    verify.assert_not_called()
    for stage in stages.values():
        stage.assert_not_called()


def test_main_holds_the_startup_configuration_for_the_whole_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A config edit during a tick does not reach the rest of that tick."""
    import tick  # type: ignore[import-untyped]

    cfg = write_tuned_config(monkeypatch, tmp_path, _TUNED_SECTION)

    def edit_config(*_args: object, **_kwargs: object) -> bool:
        cfg.write_text(cfg.read_text().replace(
            "implementer_timeout_seconds = 7200", "implementer_timeout_seconds = 9000",
        ).replace('model = "opus"', 'model = "haiku"'), encoding="utf-8")
        return True

    with tick_stage_mocks(tmp_path) as stages:
        stages["harvest_reviewers"].side_effect = edit_config
        assert tick.main() == 0

    implementer = stages["dispatch_implementers"].call_args.kwargs
    assert implementer["timeout_seconds"] == 7200
    assert implementer["model"] == "opus"


@pytest.mark.parametrize("section, flags, reported", [
    # The route setup recorded is the route that runs: nothing to report.
    ('impl_engine = "claude"\nmodel = ""\n', {}, False),
    # claude-ds with a blank model runs the pinned default, as probed.
    ('impl_engine = "claude-ds"\nmodel = "deepseek-v4-pro[1m]"\n',
     {"impl_engine": "claude-ds"}, False),
    ('impl_engine = "claude"\nmodel = ""\n', {"model": "opus"}, True),
    ('impl_engine = "claude"\nmodel = ""\n', {"impl_engine": "claude-ds"}, True),
    # No recorded route: nothing to compare against.
    ("", {"model": "opus"}, False),
])
def test_main_reports_an_implementer_route_setup_did_not_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    section: str, flags: dict[str, str], reported: bool,
) -> None:
    """The tick names a route that differs from the one setup measured.

    The setup probe decides whether ``auto`` permission mode works on one
    route. A different route runs without that evidence, so the operator
    hears about it at startup — and the tick still runs.
    """
    import tick  # type: ignore[import-untyped]

    evidence = section.replace("impl_engine =", "probed_impl_engine =").replace(
        "model =", "probed_model =",
    )
    write_tuned_config(monkeypatch, tmp_path, section + evidence)
    stderr = io.StringIO()

    with tick_stage_mocks(tmp_path), mock.patch("sys.stderr", stderr):
        assert tick.main(**flags) == 0

    output = stderr.getvalue()
    actions = [e["action"] for e in read_tick_log_entries(Path(tick.TICK_LOG_DIR))]
    assert ("differs from the route /pr-pipeline:setup probed" in output) is reported
    assert actions.count("startup_route_unprobed") == int(reported)


def test_loop_paces_from_the_configuration_its_tick_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The in-process loop paces from the same validated configuration."""
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    write_tuned_config(monkeypatch, tmp_path, _TUNED_SECTION)
    waits: list[int] = []
    cadence_inputs: list[dict[str, object]] = []
    real_decide = pipeline.decide_loop_cadence

    def recording_decide(**kwargs: object) -> tuple[int, bool]:
        cadence_inputs.append(kwargs)
        return real_decide(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tick.pipeline, "decide_loop_cadence", recording_decide)
    monkeypatch.setattr(tick, "_run_tick_body", lambda *_a, **_k: 0)
    monkeypatch.setattr(tick, "_startup_probe", lambda: {
        "gh_found": True, "git_found": True, "codex_found": True,
    })
    monkeypatch.setattr(tick, "_observe_loop_state", lambda _target=None, **_kw: {
        "in_flight_count": 0, "nearest_engine_deadline": None,
        "actionable_queue": False, "at_cap": False, "quiescent": True,
    })
    monkeypatch.setattr(
        tick, "_wait_next_tick",
        lambda seconds, *_a, **_k: waits.append(seconds) or (False, True),
    )
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_a, **_k: None)
    monkeypatch.setattr(tick, "_notify_loop_quiescent_stop", lambda _held: None)

    rc = tick._run_loop_locked(None, "", "")

    assert rc == 0
    # Five quiescent heartbeats stop the loop: four waits, each the heartbeat.
    assert waits == [3600] * 4
    assert all(inputs["quiescent_stop_after"] == 5 for inputs in cadence_inputs)


def test_lock_held_cadence_uses_the_configured_short_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A tick refused by the mutex still paces its retry from configuration."""
    import tick  # type: ignore[import-untyped]

    write_tuned_config(monkeypatch, tmp_path, _TUNED_SECTION)
    lock_path = tmp_path / ".tick.lock"
    monkeypatch.setattr(tick, "TICK_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(tick, "git", lambda *_args: str(tmp_path))
    lock = tick.TickLock(str(lock_path))
    assert lock.acquire() is True
    stderr = io.StringIO()
    try:
        with mock.patch("sys.stderr", stderr):
            assert tick.main() == tick.LOCK_HELD_EXIT_CODE
    finally:
        lock.release()

    assert "tick-cadence: next_wake=120 stop=false" in stderr.getvalue()


@pytest.mark.parametrize("failure", ["target", "config"])
def test_lock_held_cadence_falls_back_to_the_shipped_short_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    """With no usable configuration, a refused invocation still paces itself.

    The next tick that takes the lock refuses the configuration and names the
    key; the retry interval only has to be a safe one.
    """
    import tick  # type: ignore[import-untyped]

    write_tuned_config(
        monkeypatch, tmp_path,
        "loop_short_poll_seconds = 120\n"
        + ("max_iterations = 0\n" if failure == "config" else ""),
    )
    if failure == "target":
        monkeypatch.setattr(
            tick, "resolve_target",
            mock.Mock(side_effect=ValueError("no GitHub remote")),
        )
    lock_path = tmp_path / ".tick.lock"
    monkeypatch.setattr(tick, "TICK_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(tick, "git", lambda *_args: str(tmp_path))
    lock = tick.TickLock(str(lock_path))
    assert lock.acquire() is True
    stderr = io.StringIO()
    try:
        with mock.patch("sys.stderr", stderr):
            assert tick.main() == tick.LOCK_HELD_EXIT_CODE
    finally:
        lock.release()

    assert "tick-cadence: next_wake=270 stop=false" in stderr.getvalue()


@pytest.mark.parametrize("flag, key", [
    ("--fix-engine", "fix_engine"),
    ("--fix-model", "fix_model"),
])
def test_parse_cli_args_reads_the_fixer_route_flags(flag: str, key: str) -> None:
    """The fixer route has its own flags, unset unless typed."""
    import tick  # type: ignore[import-untyped]

    assert tick.parse_cli_args([])[key] == ""
    assert tick.parse_cli_args([])["impl_engine"] == ""
    assert tick.parse_cli_args([flag, "claude-ds"])[key] == "claude-ds"
    assert tick.parse_cli_args([f"{flag}=claude"])[key] == "claude"


def test_main_prompts_setup_for_a_target_with_no_repository_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A target nobody ran setup for stops before HARVEST and names setup.

    ``[defaults]`` answers every key, so without this gate the tick would run
    a repository whose verification grants were never proposed and whose
    engine route was never probed. It stops before the label check too: a
    brand-new repository has no vocabulary either, and "run setup" is the one
    instruction that fixes both.
    """
    import tick  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[operator]\nlogin = "test-owner"\n'
        '\n[repos."test-owner/elsewhere"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    verify = mock.Mock()
    monkeypatch.setattr(tick.labels, "verify", verify)

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick._startup_probe"):
            with mock.patch("tick._run_tick_body") as body:
                stderr = io.StringIO()
                with mock.patch("sys.stderr", stderr):
                    result = tick.main()

    output = stderr.getvalue()
    assert result == tick.UNCONFIGURED_TARGET_EXIT_CODE
    assert '[repos."test-owner/test-repo"]' in output
    assert "/pr-pipeline:setup" in output
    body.assert_not_called()
    verify.assert_not_called()
    actions = [e["action"] for e in read_tick_log_entries(Path(tick.TICK_LOG_DIR))]
    assert actions.count("startup_repo_unconfigured") == 1


def test_loop_skips_an_unconfigured_target_without_blocking_on_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Under ``--loop`` nobody can answer a setup prompt, so none is issued.

    The tick logs the skip and the loop returns rather than waiting on stdin
    or observing cadence: cadence reads GitHub for a target the operator has
    not admitted yet.
    """
    import tick  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text('[operator]\nlogin = "test-owner"\n', encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    monkeypatch.setattr(tick.labels, "verify", mock.Mock())
    monkeypatch.setattr(
        "builtins.input", mock.Mock(side_effect=AssertionError("prompted")),
    )
    monkeypatch.setattr(
        "sys.stdin", mock.Mock(**{
            "read.side_effect": AssertionError("read stdin"),
            "readline.side_effect": AssertionError("read stdin"),
        }),
    )
    observe = mock.Mock(side_effect=AssertionError("observed cadence"))
    monkeypatch.setattr(tick, "_observe_loop_state", observe)
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_args: None)

    with mock.patch("tick._startup_probe"):
        with mock.patch("tick._run_tick_body") as body:
            with mock.patch("sys.stderr", io.StringIO()):
                rc = tick._run_loop_locked(None, "claude", "")

    assert rc == tick.UNCONFIGURED_TARGET_EXIT_CODE
    body.assert_not_called()
    skipped = [
        e for e in read_tick_log_entries(Path(tick.TICK_LOG_DIR))
        if e["action"] == "startup_repo_unconfigured"
    ]
    assert len(skipped) == 1
    assert "skipped" in str(skipped[0]["reason"])
    assert "test-owner/test-repo" in str(skipped[0]["reason"])


def test_loop_exits_on_a_crashed_tick_without_starting_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``tick_crash`` ends ``--loop`` with code 4 before the next wait.

    The SOP stops and diagnoses on code 4, and a background loop is only
    reported when it exits. Retrying a persistent exception would loop
    silently with an actionable queue and never reach the operator.
    """
    import tick  # type: ignore[import-untyped]

    monkeypatch.setattr(tick.labels, "verify", mock.Mock())
    observe = mock.Mock(side_effect=AssertionError("observed cadence"))
    monkeypatch.setattr(tick, "_observe_loop_state", observe)
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_args: None)
    wait = mock.Mock(side_effect=AssertionError("waited for another tick"))
    monkeypatch.setattr(tick, "_wait_next_tick", wait)

    with mock.patch("tick._startup_probe"):
        with mock.patch(
            "tick._run_tick_body", side_effect=RuntimeError("persistent"),
        ) as body:
            with mock.patch("sys.stderr", io.StringIO()):
                rc = tick._run_loop_locked(None, "claude", "")

    assert rc == 4
    body.assert_called_once()
    crashes = [
        e for e in read_tick_log_entries(Path(tick.TICK_LOG_DIR))
        if e["action"] == "tick_crash"
    ]
    assert len(crashes) == 1


def test_main_reports_a_fail_closed_config_warning_and_still_ticks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A setting that *has* a safe direction warns rather than stopping the tick.

    ``allow_touching`` resolves to the conservative gate on an unusable value,
    so refusing to run would trade a working pipeline for a stricter one that
    does nothing. The operator still has to be told, or a config typo silently
    escalates every policy-editing PR forever.
    """
    from tick import main  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[operator]\nlogin = "userA"\n'
        '\n[defaults]\nallow_touching = "yes"\n'
        '\n[repos."test-owner/test-repo"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick._startup_probe"):
            with mock.patch("tick.TRUSTED_HEAD_REPO", "owner/name"):
                with mock.patch("tick.TRUSTED_REVIEW_AUTHOR", "userA"):
                    with mock.patch("tick._run_tick_body", return_value=0) as body:
                        import io
                        stderr = io.StringIO()
                        with mock.patch("sys.stderr", stderr):
                            result = main()

    assert result == 0
    assert "allow_touching" in stderr.getvalue()
    body.assert_called_once()


def test_main_crash_emits_tick_state(tmp_path: Path) -> None:
    """main() return-4 path emits tick-state with unknown fields."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.harvest_reviewers") as hr:
            hr.side_effect = ValueError("boom")
            with mock.patch("tick.harvest_implementers"):
                with mock.patch("tick.harvest_fixers"):
                    import io
                    stderr = io.StringIO()
                    with mock.patch("sys.stderr", stderr):
                        result = main()

    assert result == 4
    output = stderr.getvalue()
    assert "tick-state: status=FAILED in_flight=unknown queue=unknown quiescent=unknown" in output


def test_main_not_git_repo_emits_tick_state() -> None:
    """main() emits tick-state when git rev-parse fails (not a git repo)."""
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git", side_effect=subprocess.CalledProcessError(128, "git")):
        import io
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            result = main()

    assert result == 1
    output = stderr.getvalue()
    assert "tick-state: status=FAILED in_flight=unknown queue=unknown quiescent=unknown" in output


def test_main_invalid_target_emits_tick_state(tmp_path: Path) -> None:
    """main() emits stderr and tick-log state when target parsing fails."""
    from tick import TickLog, main  # type: ignore[import-untyped]

    log_dir = str(tmp_path / "tick-log")

    with mock.patch("tick.git") as git_mock:
        git_mock.return_value = str(tmp_path)
        with mock.patch("tick.TICK_LOG_DIR", log_dir):
            import io
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                result = main(target="not-a-target")

    assert result == 1
    output = stderr.getvalue()
    assert "tick-state: status=FAILED in_flight=unknown queue=unknown quiescent=unknown" in output
    entries = TickLog.tail_dir(log_dir)
    summary = [e for e in entries if e.get("action") == "tick_complete"]
    assert len(summary) == 1
    details = summary[0].get("details")
    assert isinstance(details, dict)
    ts = details.get("tick_state")
    assert isinstance(ts, dict)
    assert ts == {
        "status": "FAILED",
        "in_flight": "unknown",
        "queue": "unknown",
        "quiescent": "unknown",
        "held": "unknown",
    }


# --- _compute_post_dispatch_state ---


def test_compute_post_dispatch_state_marker_only_engine_counted() -> None:
    """A live local marker with no authoritative label counts in in_flight."""
    import pipeline
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    marker_states = [("pr", 42, pipeline.LABEL_REVIEWING, "live", "matched", True)]
    with mock.patch("tick._scan_marker_identities", return_value=marker_states):
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            in_flight, has_queue, _ = _compute_post_dispatch_state()

    assert in_flight == 1
    assert has_queue is False


def test_compute_post_dispatch_state_counts_hung_marker_without_label() -> None:
    """Post-dispatch accounting keeps a retained overdue engine in-flight."""
    import pipeline
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    marker_states = [("pr", 42, pipeline.LABEL_REVIEWING, "hung", "unknown", True)]
    with mock.patch("tick._scan_marker_identities", return_value=marker_states):
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            in_flight, has_queue, _ = _compute_post_dispatch_state()

    assert in_flight == 1
    assert has_queue is False


def test_compute_post_dispatch_state_names_remote_human_held_engine() -> None:
    """One-shot cadence receives label-backed human-custody identities."""
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    markers = [("pr", 42, "ai:reviewing", "hung", "unknown", True)]
    holds = {("pr", 42): frozenset({"ai:needs-human-decision"})}
    with mock.patch("tick._scan_marker_identities", return_value=markers):
        with mock.patch(
            "tick.read_authoritative_human_holds",
            return_value=holds,
        ):
            with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
                in_flight, has_queue, held_items = (
                    _compute_post_dispatch_state()
                )

    assert in_flight == 0
    assert has_queue is False
    assert held_items == ("pr #42 ai:reviewing (ai:needs-human-decision)",)


def test_compute_post_dispatch_state_reports_held_per_slot_not_per_item() -> None:
    """``in_flight + held`` must reconstruct occupancy, so held counts slots.

    Admission is keyed ``(kind, number, label)`` precisely so an accidentally
    overlapping reviewer and fixer dispatch on one PR are two paid slots. If
    the held count collapses those to one item, the tick-state line stops
    adding up in exactly the case that keying exists for — an ``in_flight=0``
    tick that still cannot dispatch at the cap becomes unexplainable.
    """
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    markers = [
        ("pr", 42, "ai:reviewing", "hung", "unknown", True),
        ("pr", 42, "ai:fixing", "hung", "unknown", True),
    ]
    holds = {("pr", 42): frozenset({"ai:blocked"})}
    with mock.patch("tick._scan_marker_identities", return_value=markers):
        with mock.patch(
            "tick.read_authoritative_human_holds", return_value=holds,
        ):
            with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
                in_flight, _has_queue, held_items = (
                    _compute_post_dispatch_state()
                )

    assert in_flight == 0
    assert held_items == (
        "pr #42 ai:fixing (ai:blocked)",
        "pr #42 ai:reviewing (ai:blocked)",
    )


def test_run_tick_body_one_shot_stop_names_held_items(
    tmp_path: Path,
) -> None:
    """The real one-shot summary carries held-item identity into its stop."""
    import io

    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    counter = tmp_path / "quiescent-counter.txt"
    counter.write_text("2")
    marker = [("pr", 42, "ai:reviewing", "hung", "unknown", True)]
    hold = {("pr", 42): frozenset({"ai:needs-human-decision"})}
    stderr = io.StringIO()

    with mock.patch("tick._QUIESCENT_COUNTER_FILE", str(counter)):
        with mock.patch("tick.harvest_reviewers", return_value=True):
            with mock.patch("tick.harvest_implementers", return_value=True):
                with mock.patch("tick.harvest_fixers", return_value=True):
                    with mock.patch(
                        "tick.read_authoritative_in_flight_labels", return_value=[],
                    ):
                        with mock.patch(
                            "tick.read_authoritative_human_holds", return_value=hold,
                        ):
                            with mock.patch(
                                "tick._scan_marker_identities", return_value=marker,
                            ):
                                with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
                                    with mock.patch("tick.dispatch_reviewers"):
                                        with mock.patch("tick.dispatch_implementers"):
                                            with mock.patch("tick.dispatch_fixers"):
                                                with mock.patch(
                                                    "tick._compute_nearest_deadline_s",
                                                    return_value=None,
                                                ):
                                                    with mock.patch(
                                                        "sys.stderr", stderr,
                                                    ):
                                                        rc = _run_tick_body(
                                                            tick_log, None, "claude",
                                                        )

    assert rc == 0
    output = stderr.getvalue()
    assert "in_flight=0 queue=false quiescent=true held=1" in output
    assert "stop=true" in output
    assert "awaiting human decision: pr #42 ai:reviewing (ai:needs-human-decision)" in output
    summary = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "tick_complete"
    ][0]
    details = summary["details"]
    assert isinstance(details, dict)
    assert details["tick_state"]["held"] == "1"  # type: ignore[index]
    assert "pr #42" in details["cadence"]["reason"]  # type: ignore[index]


def test_compute_post_dispatch_state_ignores_labels_this_machine_does_not_own(
    tmp_path: Path,
) -> None:
    """Cadence occupancy is the admission contract, not the repo's labels.

    The emitted tick-state line and the admission decision read one
    definition of "in flight". If foreign in-flight labels counted here, a
    stale manual pickup would report a non-zero ``in_flight`` and hold the
    loop permanently non-quiescent over work it can never dispatch.

    The incident's four ``ai:implementing`` issues are live in the environment
    and the marker root is a real empty directory, so the production marker
    scan runs for real and reading the labels is the only way to reach a
    non-zero count.
    """
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.gh_json",
            side_effect=gh_json_four_foreign_implementing_labels,
        ):
            with mock.patch("tick.read_authoritative_in_flight_labels") as reader:
                in_flight, has_queue, _ = _compute_post_dispatch_state()

    assert reader.call_count == 0, (
        "post-dispatch state read the repo-wide in-flight labels; cadence "
        "occupancy must equal admission occupancy"
    )
    assert in_flight == 0
    assert has_queue is False


def test_compute_post_dispatch_state_counts_same_number_issue_and_pr() -> None:
    """Same-number issue and PR markers are separate in-flight engines."""
    import pipeline
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    marker_states = [
        ("issue", 42, pipeline.LABEL_IMPLEMENTING, "live", "matched", True),
        ("pr", 42, pipeline.LABEL_REVIEWING, "live", "matched", True),
    ]
    with mock.patch("tick._scan_marker_identities", return_value=marker_states):
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            in_flight, has_queue, _ = _compute_post_dispatch_state()

    assert in_flight == 2
    assert has_queue is False


def test_compute_post_dispatch_state_counts_a_real_on_disk_marker(
    tmp_path: Path,
) -> None:
    """A marker written to the real marker root occupies a slot.

    Exercises the production scan against a real ``dispatch.json`` rather
    than a mocked identity list: occupancy now rests entirely on that file,
    so the path that reads it is the one worth proving.
    """
    import pipeline
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    marker_dir = wt_root / "issue-42"
    marker_dir.mkdir(parents=True)
    marker = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "pre_label": pipeline.LABEL_AVAILABLE,
        "dispatch_id": "dispatch-42",
        "state": "launching",
    }
    (marker_dir / "dispatch.json").write_text(json.dumps(marker))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            in_flight, has_queue, _ = _compute_post_dispatch_state()

    assert in_flight == 1
    assert has_queue is False


def test_compute_post_dispatch_state_unknown_in_flight_on_marker_failure() -> None:
    """Marker scan failure → in_flight unknown (None)."""
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    with mock.patch("tick._scan_marker_identities", side_effect=RuntimeError("bad")):
        with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
            in_flight, has_queue, _ = _compute_post_dispatch_state()

    assert in_flight is None
    assert has_queue is False


def test_compute_post_dispatch_state_held_only_queue_not_actionable() -> None:
    """The one-shot tick-cadence path applies the same hold-aware eligibility
    as dispatch and the in-process loop: a held-only queue must read as no
    actionable work so the external loop can quiesce."""
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    held_page = [{"author": {"login": TRUSTED_AUTHOR}, "labels": [
        {"name": "ai:available"}, {"name": "ai:blocked"},
    ]}]

    def gh_json_side_effect(*args: str, **_kwargs: object) -> object:
        if "ai:available" in args:
            return held_page
        return []

    with mock.patch("tick._scan_marker_identities", return_value=[]):
        with mock.patch("tick.gh_json", side_effect=gh_json_side_effect):
            in_flight, has_queue, _ = _compute_post_dispatch_state()

    assert in_flight == 0
    assert has_queue is False


def test_compute_post_dispatch_state_unknown_queue_on_gh_failure() -> None:
    """All gh queue-check calls fail → queue unknown (None)."""
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    with mock.patch("tick._scan_marker_identities", return_value=[]):
        with mock.patch("tick.read_authoritative_human_holds", return_value={}):
            with mock.patch(
                "tick.gh_json",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ):
                in_flight, has_queue, _ = _compute_post_dispatch_state()

    assert in_flight == 0
    assert has_queue is None


def test_compute_post_dispatch_state_scopes_queue_to_target() -> None:
    """The emitted tick-state line must describe the same scope the loop
    stops on: a targeted tick whose target is terminal reports no actionable
    queue even while unrelated PRs are dispatchable."""
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    with mock.patch("tick._scan_marker_identities", return_value=[]):
        with mock.patch("tick.read_authoritative_human_holds", return_value={}):
            with mock.patch(
                "tick.gh_json", side_effect=fake_gh_json_terminal_pr_578,
            ):
                in_flight, has_queue, _ = _compute_post_dispatch_state(
                    target=("pr", 578),
                )

    assert in_flight == 0
    assert has_queue is False


def test_compute_post_dispatch_state_targeted_unknown_queue_stays_unknown() -> None:
    """A failed point-read under a target is unknown state, not quiescence."""
    from tick import _compute_post_dispatch_state  # type: ignore[import-untyped]

    with mock.patch("tick._scan_marker_identities", return_value=[]):
        with mock.patch("tick.read_authoritative_human_holds", return_value={}):
            with mock.patch(
                "tick.gh_json",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ):
                in_flight, has_queue, _ = _compute_post_dispatch_state(
                    target=("pr", 578),
                )

    assert in_flight == 0
    assert has_queue is None


def test_run_tick_body_unknown_post_dispatch_state_uses_cache_warm_cadence(
    tmp_path: Path,
) -> None:
    """Unknown queue/in-flight state must not produce a cold heartbeat."""
    import io

    from tick import TickLog, _run_tick_body  # type: ignore[import-untyped]

    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    stderr = io.StringIO()

    with mock.patch("tick.harvest_reviewers", return_value=True):
        with mock.patch("tick.harvest_implementers", return_value=True):
            with mock.patch("tick.harvest_fixers", return_value=True):
                with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
                    with mock.patch("tick.dispatch_reviewers"):
                        with mock.patch("tick.dispatch_implementers"):
                            with mock.patch("tick.dispatch_fixers"):
                                with mock.patch(
                                    "tick._compute_post_dispatch_state",
                                    return_value=(None, None, ()),
                                ):
                                    with mock.patch(
                                        "tick._compute_nearest_deadline_s",
                                        return_value=None,
                                    ):
                                        with mock.patch("sys.stderr", stderr):
                                            rc = _run_tick_body(
                                                tick_log, None, "claude", "",
                                            )

    output = stderr.getvalue()
    assert rc == 0
    assert "tick-state: status=ok in_flight=unknown queue=unknown quiescent=unknown" in output
    assert "tick-cadence: next_wake=270 stop=false" in output
    assert "next_wake=1800" not in output


def test_compute_nearest_deadline_ignores_pid_reuse(
    tmp_path: Path,
) -> None:
    """A mismatched PID is not an actionable deadline for cadence."""
    from tick import _compute_nearest_deadline_s  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 900.0, "base_ref": "main", "base_sha": "base",
        "head_sha": "head", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999,
        "start_time": 100.0, "pgid": 99999,
    }))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.time.time", return_value=1000.0):
            with mock.patch(
                "process_control.get_process_start_time", return_value=200.0,
            ):
                with mock.patch("process_control.pid_is_alive", return_value=True):
                    with mock.patch("tick.os.kill"):
                        nearest = _compute_nearest_deadline_s()

    assert nearest is None


def test_observe_loop_state_ignores_reused_pid_deadline(tmp_path: Path) -> None:
    """A reused PID must not drive the in-process loop into a hot retry.

    Production path: no mocking of the deadline scan itself. The marker's
    deadline is already overdue, so an unauthenticated scan would clamp the
    cadence to a 1-second wake for as long as the marker survives.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 900.0, "base_ref": "main", "base_sha": "base",
        "head_sha": "head", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999,
        "start_time": 100.0, "pgid": 99999,
    }))
    # A reused PID is nobody's engine, so it occupies no slot. Queued work,
    # not a foreign in-flight label, is what keeps this loop awake.
    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.read_authoritative_in_flight_labels", return_value=[],
        ):
            with mock.patch(
                "tick.read_authoritative_human_holds", return_value={},
            ):
                with mock.patch("tick._has_actionable_queue", return_value=True):
                    with mock.patch("tick.time.time", return_value=1000.0):
                        with mock.patch(
                            "process_control.get_process_start_time",
                            return_value=200.0,
                        ):
                            with mock.patch(
                                "process_control.pid_is_alive", return_value=True,
                            ):
                                with mock.patch("tick.os.kill"):
                                    state = tick._observe_loop_state()

    assert state["in_flight_count"] == 0
    assert state["nearest_engine_deadline"] is None

    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=state["in_flight_count"],
        nearest_engine_deadline=state["nearest_engine_deadline"],
        actionable_queue=state["actionable_queue"],
        at_cap=state["at_cap"],
        quiescent=state["quiescent"],
        consecutive_quiescent=0,
        now=1000.0,
    )
    # The overdue marker must not clamp the wake to its deadline: with the
    # reused PID discounted there is no engine to pace to, so this is the
    # ordinary actionable-queue cadence rather than the 1s hot retry.
    assert (sleep_seconds, stop) == (270, False)


def test_observe_loop_state_paces_to_a_live_engine_deadline(
    tmp_path: Path,
) -> None:
    """Identity-authoritative filtering must not blunt legitimate pacing.

    An identity-verified live engine still hands its deadline to cadence, so the loop
    wakes as that engine goes overdue rather than sleeping the full band.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1030.0, "base_ref": "main", "base_sha": "base",
        "head_sha": "head", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999,
        "start_time": 100.0, "pgid": 99999,
    }))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.read_authoritative_in_flight_labels", return_value=[],
        ):
            with mock.patch("tick.gh_json", side_effect=gh_json_no_holds):
                with mock.patch("tick._has_actionable_queue", return_value=False):
                    with mock.patch("tick.time.time", return_value=1000.0):
                        with mock.patch(
                            "process_control.get_process_start_time",
                            return_value=100.0,
                        ):
                            with mock.patch(
                                "process_control.pid_is_alive", return_value=True,
                            ):
                                state = tick._observe_loop_state()

    assert state["in_flight_count"] == 1
    assert state["nearest_engine_deadline"] == 1030.0

    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=state["in_flight_count"],
        nearest_engine_deadline=state["nearest_engine_deadline"],
        actionable_queue=state["actionable_queue"],
        at_cap=state["at_cap"],
        quiescent=state["quiescent"],
        consecutive_quiescent=0,
        now=1000.0,
    )
    assert (sleep_seconds, stop) == (30, False)


def test_observe_loop_state_after_failed_kill_uses_cache_warm_cadence(
    tmp_path: Path,
) -> None:
    """A failed kill retains custody without restoring the overdue hot loop.

    This drives the disputed production branch through reconciliation and the
    post-tick observation/cadence decision. The process group remains alive,
    so the wake watcher has no completion evidence yet; retrying the same
    failed signal every second would only recreate the hot loop.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 900.0, "base_ref": "main", "base_sha": "base",
        "head_sha": "head", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999,
        "start_time": 100.0, "pgid": 99999,
    }))
    config = pipeline.StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.gh_text", return_value="OPEN"):
            with mock.patch(
                "tick.gh_json",
                return_value={"labels": [{"name": "ai:reviewing"}]},
            ):
                with mock.patch("tick.time.time", return_value=1000.0):
                    with mock.patch(
                        "process_control.get_process_start_time",
                        return_value=100.0,
                    ):
                        with mock.patch(
                            "process_control.pid_is_alive", return_value=True,
                        ):
                            with mock.patch(
                                "process_control.terminate_process_group",
                                return_value=False,
                            ):
                                decision = tick._reconcile_one(
                                    42, str(wt_dir), config, tick_log=None,
                                )
                                with mock.patch(
                                    "tick.read_authoritative_in_flight_labels",
                                    return_value=[],
                                ):
                                    with mock.patch(
                                        "tick.read_authoritative_human_holds",
                                        return_value=set(),
                                    ):
                                        with mock.patch(
                                            "tick._has_actionable_queue",
                                            return_value=False,
                                        ):
                                            state = tick._observe_loop_state()

    assert decision == {
        "action": "leave",
        "kill": False,
        "label": "",
        "identity": "matched",
        "pr_number": 42,
    }
    assert state["in_flight_count"] == 1
    assert state["nearest_engine_deadline"] is None
    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=state["in_flight_count"],
        nearest_engine_deadline=state["nearest_engine_deadline"],
        actionable_queue=state["actionable_queue"],
        at_cap=state["at_cap"],
        quiescent=state["quiescent"],
        consecutive_quiescent=0,
        now=1000.0,
    )
    assert (sleep_seconds, stop) == (270, False)


def test_main_crash_logs_tick_state_to_tick_log(tmp_path: Path) -> None:
    """main() return-4 path writes tick_state into the tick log entry."""
    from tick import TickLog, main  # type: ignore[import-untyped]

    log_dir = str(tmp_path / "tick-log")
    os.makedirs(log_dir)

    with mock.patch("tick.TICK_LOG_DIR", log_dir):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(tmp_path)
            with mock.patch("tick.harvest_reviewers") as hr:
                hr.side_effect = ValueError("boom")
                with mock.patch("tick.harvest_implementers"):
                    with mock.patch("tick.harvest_fixers"):
                        import io
                        stderr = io.StringIO()
                        with mock.patch("sys.stderr", stderr):
                            result = main()

    assert result == 4
    entries = TickLog.tail_dir(log_dir)
    summary = [e for e in entries if e.get("action") == "tick_complete"]
    assert len(summary) == 1
    details = summary[0].get("details")
    assert isinstance(details, dict)
    ts = details.get("tick_state")
    assert isinstance(ts, dict)
    assert ts == {
        "status": "FAILED",
        "in_flight": "unknown",
        "queue": "unknown",
        "quiescent": "unknown",
        "held": "unknown",
    }


# --- Worktree metadata leak (finding #3) ---


def test_archive_and_cleanup_scratch_reclaims_git_worktree_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archiving a real worktree must leave no entry in ``.git/worktrees/``.

    Uses a real repository and a real ``git worktree`` rather than mocking the
    removal helper: the archive step *renames* the scratch tree into the
    evidence directory, so any assertion about a helper being "called" proves
    nothing about whether git actually reclaimed the metadata. A surviving
    entry pins the SHA and blocks garbage collection.
    """
    from tick import _archive_and_cleanup_scratch  # type: ignore[import-untyped]

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "t@t.t")
    run_git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x")
    run_git(repo, "add", "a.txt")
    run_git(repo, "commit", "-qm", "init")

    wt_root = repo / ".claude" / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_root.mkdir(parents=True)
    run_git(repo, "worktree", "add", "-q", "--detach", str(wt_dir), "HEAD")
    (wt_dir / "dispatch.json").write_text("some marker data")

    assert "pr-42" in run_git(repo, "worktree", "list")

    # Deliberately not chdir'd into the repo: the prune must target the tree it
    # is cleaning, not whatever directory the tick process happens to be in.
    monkeypatch.chdir(tmp_path)
    result = _archive_and_cleanup_scratch(
        wt_root=str(wt_root),
        wt_dir=str(wt_dir),
        kind="pr",
        number=42,
        dispatch_id="d-1",
        tick_log=None,
        stage="review",
        phase="harvest",
        failure_reason="test",
    )

    assert result is True
    assert not wt_dir.exists()
    assert "pr-42" not in run_git(repo, "worktree", "list")


def test_finalize_terminal_archive_reclaims_git_worktree_metadata(
    tmp_path: Path,
) -> None:
    """A direct terminal archive must reclaim the moved worktree's metadata."""
    from tick import _finalize_terminal_archive  # type: ignore[import-untyped]

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "t@t.t")
    run_git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x")
    run_git(repo, "add", "a.txt")
    run_git(repo, "commit", "-qm", "init")

    wt_root = repo / ".claude" / "worktrees"
    wt_dir = wt_root / "pr-42"
    archive_dir = wt_root / ".harvest-evidence" / "pr-42-terminal"
    archive_dir.mkdir(parents=True)
    run_git(repo, "worktree", "add", "-q", "--detach", str(wt_dir), "HEAD")

    assert "pr-42" in run_git(repo, "worktree", "list")
    result = _finalize_terminal_archive(
        wt_dir=str(wt_dir),
        archive_dir=str(archive_dir),
        number=42,
        stage="review",
        dispatch_id="review-42",
        reason="test",
        tick_log=None,
    )

    assert result is True
    assert (archive_dir / "scratch").is_dir()
    assert "pr-42" not in run_git(repo, "worktree", "list")


def test_finalize_terminal_archive_logs_deferred_prune_failure(
    tmp_path: Path,
) -> None:
    """Archive custody succeeds while a metadata-GC failure stays visible."""
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _finalize_terminal_archive,
    )

    wt_dir = tmp_path / "worktrees" / "pr-42"
    archive_dir = tmp_path / "worktrees" / ".harvest-evidence" / "pr-42"
    wt_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    tick_log = TickLog.open(str(tmp_path / "tick-log"))

    with mock.patch("tick._prune_worktree_metadata", return_value=False):
        result = _finalize_terminal_archive(
            wt_dir=str(wt_dir),
            archive_dir=str(archive_dir),
            number=42,
            stage="review",
            dispatch_id="review-42",
            reason="test",
            tick_log=tick_log,
        )

    assert result is True
    entries = TickLog.tail_dir(str(tmp_path / "tick-log"))
    failures = [
        entry for entry in entries
        if entry.get("action") == "worktree_prune_failed"
    ]
    assert failures == [{
        "ts": failures[0]["ts"],
        "pr": 42,
        "phase": "harvest",
        "stage": "review",
        "action": "worktree_prune_failed",
        "reason": "git worktree prune failed; stale metadata until next dispatch",
        "engine": "",
        "dispatch_id": "review-42",
    }]


def test_stale_redispatch_reclaims_git_worktree_metadata(
    tmp_path: Path,
) -> None:
    """Archiving stale scratch before redispatch must not leave git metadata."""
    from tick import _existing_marker_blocks_dispatch  # type: ignore[import-untyped]

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "t@t.t")
    run_git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x")
    run_git(repo, "add", "a.txt")
    run_git(repo, "commit", "-qm", "init")

    wt_root = repo / ".claude" / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_root.mkdir(parents=True)
    run_git(repo, "worktree", "add", "-q", "--detach", str(wt_dir), "HEAD")
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "base",
        "head_sha": "head", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999,
        "start_time": 100.0, "pgid": 99999,
    }))

    assert "pr-42" in run_git(repo, "worktree", "list")
    with mock.patch(
        "process_control.get_process_start_time", return_value=None,
    ):
        with mock.patch("process_control.pid_is_alive", return_value=False):
            with mock.patch("process_control.pgid_is_alive", return_value=False):
                blocked = _existing_marker_blocks_dispatch(
                    str(wt_dir),
                    entity="pr",
                    number=42,
                    pre_label="ai:implemented",
                    stage="review",
                )

    assert blocked is False
    assert not wt_dir.exists()
    assert "pr-42" not in run_git(repo, "worktree", "list")


# --- Quiescent counter reset on failure (finding #4) ---


def test_quiescent_counter_resets_on_reconcile_failure(
    tmp_path: Path,
) -> None:
    """A failed tick (rc=1) must reset the quiescent counter to 0.

    Without this, stale counter values from a prior successful quiescent tick
    survive across failures, causing premature loop stop when a later tick
    succeeds and is quiescent.
    """
    from tick import (  # type: ignore[import-untyped]
        _QUIESCENT_COUNTER_FILE,
        TickLog,
        _read_quiescent_counter,
        _run_tick_body,
    )

    tick_log = TickLog.open(str(tmp_path / "tick-log"))

    os.makedirs(os.path.dirname(_QUIESCENT_COUNTER_FILE), exist_ok=True)
    with open(_QUIESCENT_COUNTER_FILE, "w") as f:
        f.write("2")

    import io
    stderr = io.StringIO()

    with mock.patch("tick.harvest_reviewers", return_value=False):
        with mock.patch("tick.harvest_implementers", return_value=True):
            with mock.patch("tick.harvest_fixers", return_value=True):
                # Stale-label reconciliation runs even on a failed harvest, and
                # unmocked it point-reads the live repository and force-relabels
                # whatever it finds wearing an in-flight label with no local
                # marker. The subject here is the counter, not reconciliation.
                with mock.patch(
                    "tick.read_authoritative_in_flight_labels", return_value=[],
                ):
                    with mock.patch("sys.stderr", stderr):
                        rc = _run_tick_body(tick_log, None, "claude", "")

    assert rc == 1
    assert _read_quiescent_counter() == 0


def test_quiescent_counter_resets_on_auth_labels_failure(
    tmp_path: Path,
) -> None:
    """A partial-failure tick (rc=2) must reset the quiescent counter to 0."""
    from tick import (  # type: ignore[import-untyped]
        _QUIESCENT_COUNTER_FILE,
        TickLog,
        _read_quiescent_counter,
        _run_tick_body,
    )

    tick_log = TickLog.open(str(tmp_path / "tick-log"))

    os.makedirs(os.path.dirname(_QUIESCENT_COUNTER_FILE), exist_ok=True)
    with open(_QUIESCENT_COUNTER_FILE, "w") as f:
        f.write("2")

    import io
    stderr = io.StringIO()

    with mock.patch("tick.harvest_reviewers", return_value=True):
        with mock.patch("tick.harvest_implementers", return_value=True):
            with mock.patch("tick.harvest_fixers", return_value=True):
                with mock.patch(
                    "tick.read_authoritative_in_flight_labels",
                    side_effect=subprocess.CalledProcessError(1, "gh"),
                ):
                    with mock.patch("sys.stderr", stderr):
                        rc = _run_tick_body(tick_log, None, "claude", "")

    assert rc == 2
    assert _read_quiescent_counter() == 0



# --- Terminal-close group-awareness ---


def test_reconcile_one_malformed_lifecycle_state_is_non_destructive(
    tmp_path: Path,
) -> None:
    """Only explicit CLOSED/MERGED state can enter terminal cleanup."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()

    with mock.patch("tick.gh_text", return_value="UNKNOWN"):
        with mock.patch("tick.gh_json") as labels_read:
            with mock.patch("tick.cleanup_scratch") as cleanup:
                decision = _reconcile_one(
                    42, str(wt_dir), config, tick_log=None,
                )

    assert decision["identity"] == "unknown"
    labels_read.assert_not_called()
    cleanup.assert_not_called()
    assert wt_dir.exists()


def test_reconcile_one_terminal_markerless_human_hold_retains_scratch(
    tmp_path: Path,
) -> None:
    """A terminal hold owns even markerless scratch until a human releases it."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch(
            "tick.gh_json",
            return_value={"labels": [{"name": "ai:needs-human-decision"}]},
        ):
            with mock.patch("tick._remove_worktree") as remove_wt:
                with mock.patch("tick.cleanup_scratch") as cleanup:
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                        attempts=attempts,
                    )

    assert decision["action"] == "leave"
    assert decision["identity"] == "markerless"
    assert wt_dir.exists()
    remove_wt.assert_not_called()
    cleanup.assert_not_called()
    attempts.clear.assert_not_called()


def test_reconcile_one_terminal_valid_marker_retains_custody_while_group_alive(
    tmp_path: Path,
) -> None:
    """A merged PR with dead wrapper PID but live child group keeps custody."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 999999.0,
        "pgid": 88888,
    }))
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch(
            "process_control.get_process_start_time", return_value=None,
        ):
            with mock.patch("process_control.pid_is_alive", return_value=False):
                with mock.patch(
                    "process_control.pgid_is_alive", return_value=True,
                ):
                    with mock.patch("tick._remove_worktree") as remove_wt:
                        decision = _reconcile_one(
                            42, str(wt_dir), config, tick_log=None,
                            attempts=attempts,
                        )

    assert wt_dir.exists()
    assert (wt_dir / "dispatch.json").is_file()
    assert decision["identity"] == "unknown"
    remove_wt.assert_not_called()
    attempts.clear.assert_not_called()


@pytest.mark.parametrize("lifecycle", ["CLOSED", "MERGED"])
def test_terminal_hold_proven_death_releases_label_before_archive(
    tmp_path: Path,
    lifecycle: str,
) -> None:
    """Terminal held death uses the same durable release path as OPEN.

    The in-flight label is the pipeline's receipt and the marker is the only
    artifact that can re-prove process-group death. The label therefore has to
    be removed and verified before the marker is archived, even when the item
    was closed or merged while its engine was running.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        _execute_reconcile_actions,
        _reconcile_one,
    )

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    dual = {"labels": [
        {"name": "ai:reviewing"}, {"name": "ai:needs-human-decision"},
    ]}
    hold_only = {"labels": [{"name": "ai:needs-human-decision"}]}
    events: list[str] = []
    gh_calls: list[tuple[str, ...]] = []
    attempts = mock.Mock()

    def record_gh(*args: str, **_kwargs: object) -> str:
        events.append("release")
        gh_calls.append(args)
        return ""

    def label_reads(*_args: str, **_kwargs: object) -> object:
        return hold_only if gh_calls else dual

    def record_archive(**_kwargs: object) -> bool:
        events.append("archive")
        return True

    with mock.patch("tick.gh_text", return_value=lifecycle):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick.process_control.get_process_start_time", return_value=None,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=False,
                    ):
                        with mock.patch(
                            "tick.process_control.pgid_is_alive", return_value=False,
                        ):
                            with mock.patch(
                                "tick._archive_and_cleanup_scratch",
                                side_effect=record_archive,
                            ):
                                decision = _reconcile_one(
                                    42, str(wt_dir), config, tick_log=None,
                                    attempts=attempts,
                                )
                                assert events == []
                                assert decision["action"] == "cleanup"
                                assert decision["release_label"] == "ai:reviewing"
                                assert _execute_reconcile_actions(
                                    [decision], config, str(tmp_path),
                                    attempts=attempts,
                                )

    assert events == ["release", "archive"]
    assert gh_calls == [
        ("pr", "edit", "42", "--remove-label", "ai:reviewing"),
    ]
    attempts.clear.assert_called_once_with(42)


def test_terminal_hold_release_readback_failure_retains_marker(
    tmp_path: Path,
) -> None:
    """An unverified terminal label release cannot authorize archival."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        _execute_reconcile_actions,
        _reconcile_one,
    )

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    dual = {"labels": [
        {"name": "ai:reviewing"}, {"name": "ai:blocked"},
    ]}
    label_reads = iter([
        dual,
        subprocess.CalledProcessError(1, "gh"),
    ])

    def read_labels(*_args: str, **_kwargs: object) -> object:
        result = next(label_reads)
        if isinstance(result, BaseException):
            raise result
        return result

    with mock.patch("tick.gh_text", return_value="CLOSED"):
        with mock.patch("tick.gh_json", side_effect=read_labels):
            with mock.patch("tick.gh", return_value=""):
                with mock.patch(
                    "tick.process_control.get_process_start_time", return_value=None,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=False,
                    ):
                        with mock.patch(
                            "tick.process_control.pgid_is_alive", return_value=False,
                        ):
                            with mock.patch(
                                "tick._archive_and_cleanup_scratch",
                            ) as archive:
                                decision = _reconcile_one(
                                    42, str(wt_dir), config, tick_log=None,
                                )
                                assert not _execute_reconcile_actions(
                                    [decision], config, str(tmp_path),
                                )

    archive.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_terminal_hold_archive_failure_propagates_after_release(
    tmp_path: Path,
) -> None:
    """A failed terminal archive gates dispatch and retains local custody."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        _execute_reconcile_actions,
        _reconcile_one,
    )

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "review-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    dual = {"labels": [
        {"name": "ai:reviewing"}, {"name": "ai:needs-human-decision"},
    ]}
    hold_only = {"labels": [{"name": "ai:needs-human-decision"}]}
    gh_calls: list[tuple[str, ...]] = []
    attempts = mock.Mock()

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    def label_reads(*_args: str, **_kwargs: object) -> object:
        return hold_only if gh_calls else dual

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick.process_control.get_process_start_time", return_value=None,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=False,
                    ):
                        with mock.patch(
                            "tick.process_control.pgid_is_alive", return_value=False,
                        ):
                            with mock.patch(
                                "tick._archive_and_cleanup_scratch", return_value=False,
                            ):
                                decision = _reconcile_one(
                                    42, str(wt_dir), config, tick_log=None,
                                )
                                assert not _execute_reconcile_actions(
                                    [decision], config, str(tmp_path),
                                    attempts=attempts,
                                )

    assert gh_calls == [
        ("pr", "edit", "42", "--remove-label", "ai:reviewing"),
    ]
    assert (wt_dir / "dispatch.json").is_file()
    attempts.clear.assert_not_called()


def test_terminal_hold_completed_result_is_published_and_logged_after_archive(
    tmp_path: Path,
) -> None:
    """A terminal held result follows the same publish-gated custody path."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import (  # type: ignore[import-untyped]
        TickLog,
        _execute_reconcile_actions,
        _reconcile_one,
    )

    config = StageConfig(
        stage="fix", worktree_prefix="pr", attempts_key_prefix="",
        result_file="result.json", in_flight_label="ai:fixing",
        pre_label="ai:changes-requested", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    tick_log = TickLog.open(str(tmp_path / "tick-log"))
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude", "stage": "fix", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:changes-requested",
        "dispatch_id": "fix-42", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))
    (wt_dir / "result.json").write_text(json.dumps({
        "result": "FIXES_PUSHED_RECHECK_NEEDED",
    }))
    dual = {"labels": [
        {"name": "ai:fixing"}, {"name": "ai:needs-human-decision"},
    ]}
    hold_only = {"labels": [{"name": "ai:needs-human-decision"}]}
    gh_calls: list[tuple[str, ...]] = []

    def record_gh(*args: str, **_kwargs: object) -> str:
        gh_calls.append(args)
        return ""

    def label_reads(*_args: str, **_kwargs: object) -> object:
        return hold_only if gh_calls else dual

    with mock.patch("tick.gh_text", return_value="CLOSED"):
        with mock.patch("tick.gh_json", side_effect=label_reads):
            with mock.patch("tick.gh", side_effect=record_gh):
                with mock.patch(
                    "tick.process_control.get_process_start_time", return_value=None,
                ):
                    with mock.patch(
                        "tick.process_control.pid_is_alive", return_value=False,
                    ):
                        with mock.patch(
                            "tick.process_control.pgid_is_alive", return_value=False,
                        ):
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log,
                            )
                            assert not [
                                entry for entry in tick_log.tail()
                                if entry.get("action") == "held_result_published"
                            ]
                            assert _execute_reconcile_actions(
                                [decision], config, str(tmp_path),
                                tick_log=tick_log,
                            )

    published = [
        entry for entry in tick_log.tail()
        if entry.get("action") == "held_result_published"
    ]
    assert len(published) == 1
    assert published[0].get("details") == {
        "outcome": "FIXES_PUSHED_RECHECK_NEEDED",
    }
    assert published[0].get("dispatch_id") == "fix-42"


def test_reconcile_one_terminal_human_hold_keeps_label_and_scratch(
    tmp_path: Path,
) -> None:
    """Closing an item does not revoke its durable human hold.

    Recovering identity lets the pipeline stop the runaway process, but it
    never lets it archive the evidence or touch the operator's label — those
    stay until the human resolves the hold.
    """
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch(
            "tick.gh_json",
            return_value={"labels": [{"name": "ai:needs-human-decision"}]},
        ):
            with mock.patch(
                "process_control.get_process_start_time", return_value=1000.0,
            ):
                with mock.patch("process_control.pid_is_alive", return_value=True):
                    with mock.patch(
                        "process_control.terminate_process_group",
                        return_value=True,
                    ) as kill_group:
                        with mock.patch(
                            "tick._archive_and_cleanup_scratch",
                        ) as archive:
                            decision = _reconcile_one(
                                42, str(wt_dir), config, tick_log=None,
                            )

    assert decision["action"] == "leave"
    # The runaway is stopped...
    kill_group.assert_called_once()
    # ...but the evidence and the operator's label are not touched.
    archive.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_reconcile_one_terminal_label_read_failure_retains_custody(
    tmp_path: Path,
) -> None:
    """A terminal item cannot be killed when its human-hold state is unknown."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch(
            "tick.gh_json", side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            with mock.patch(
                "process_control.terminate_process_group",
            ) as kill_group:
                decision = _reconcile_one(
                    42, str(wt_dir), config, tick_log=None,
                )

    assert decision["identity"] == "unknown"
    kill_group.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_reconcile_one_terminal_malformed_label_row_retains_custody(
    tmp_path: Path,
) -> None:
    """A partial successful label payload cannot authorize a terminal kill."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 1.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": "ai:implemented",
        "dispatch_id": "d-1", "pid": 99999, "start_time": 1000.0,
        "pgid": 99999,
    }))

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch(
            "tick.gh_json",
            return_value={"labels": [{"name": "ai:implemented"}, {}]},
        ):
            with mock.patch(
                "process_control.get_process_start_time", return_value=1000.0,
            ):
                with mock.patch(
                    "process_control.terminate_process_group",
                    return_value=True,
                ) as kill_group:
                    decision = _reconcile_one(
                        42, str(wt_dir), config, tick_log=None,
                    )

    assert decision["identity"] == "unknown"
    kill_group.assert_not_called()
    assert (wt_dir / "dispatch.json").is_file()


def test_reconcile_one_terminal_corrupt_marker_retains_custody_while_group_alive(
    tmp_path: Path,
) -> None:
    """A merged PR with corrupt marker, dead PID, live group retains custody."""
    from pipeline import StageConfig  # type: ignore[attr-defined]
    from tick import _reconcile_one  # type: ignore[import-untyped]

    config = StageConfig(
        stage="review", worktree_prefix="pr", attempts_key_prefix="",
        result_file="review.json", in_flight_label="ai:reviewing",
        pre_label="ai:implemented", strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    wt_dir = tmp_path / "pr-42"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "dispatch_id": "partial-42",
        "pid": 99999, "start_time": 1000.0, "pgid": 88888,
    }))
    attempts = mock.Mock()

    with mock.patch("tick.gh_text", return_value="MERGED"):
        with mock.patch(
            "process_control.get_process_start_time", return_value=None,
        ):
            with mock.patch("process_control.pid_is_alive", return_value=False):
                with mock.patch(
                    "process_control.pgid_is_alive", return_value=True,
                ):
                    with mock.patch("tick._remove_worktree") as remove_wt:
                        decision = _reconcile_one(
                            42, str(wt_dir), config, tick_log=None,
                            attempts=attempts,
                        )

    assert wt_dir.exists()
    assert (wt_dir / "dispatch.json").is_file()
    assert decision["identity"] != "dead"
    remove_wt.assert_not_called()
    attempts.clear.assert_not_called()


# --- Observation fail-closed ---


def test_observe_loop_state_admission_error_forces_non_quiescent() -> None:
    """An admission_error from corrupt markers saturates slots and stays warm."""
    import tick  # type: ignore[import-untyped]

    with mock.patch(
        "tick.read_authoritative_in_flight_labels", return_value=[],
    ):
        with mock.patch(
            "tick._scan_marker_identities",
            side_effect=RuntimeError("corrupt marker"),
        ):
            with mock.patch("tick._has_actionable_queue", return_value=False):
                with mock.patch(
                    "tick._nearest_actionable_engine_deadline", return_value=None,
                ):
                    state = tick._observe_loop_state()

    assert state == {
        "in_flight_count": 3,
        "nearest_engine_deadline": None,
        "actionable_queue": True,
        "at_cap": True,
        "quiescent": False,
        "held_items": (),
    }


# --- Test-suite mutation guard ---


@pytest.mark.parametrize("argv", [
    ["gh", "pr", "comment", "1", "--body", "x"],
    ["gh", "api", "-X", "POST", "repos/o/r/issues/1/comments"],
    ["gh", "api", "--method", "PATCH", "repos/o/r/issues/1"],
    ["gh", "api", "repos/o/r/issues/1/labels", "--method=DELETE"],
    ["gh", "api", "repos/o/r/issues/1", "-XPUT"],
    # Implicit POST: `gh help api` states that adding request parameters
    # switches the method from GET to POST. The first of these is the CLI's own
    # documented way to post an issue comment — the exact accident this guard
    # exists to catch — and it carries no method flag at all.
    ["gh", "api", "repos/o/r/issues/1/comments", "-f", "body=x"],
    ["gh", "api", "repos/o/r/issues/1/comments", "--raw-field", "body=x"],
    ["gh", "api", "repos/o/r/issues/1/labels", "-F", "labels[]=ai:revised"],
    ["gh", "api", "repos/o/r/issues/1", "--field=title=x"],
    ["gh", "api", "graphql", "-f", "query=mutation { addComment }"],
    ["gh", "api", "repos/o/r/issues/1/comments", "--input", "-"],
    # Write commands whose verbs never appear in a name denylist. Each one is a
    # real side effect on the repository, and none of them is spelled with
    # `comment`/`edit`/`create`.
    ["gh", "workflow", "run", "ci.yml"],
    ["gh", "run", "rerun", "123"],
    ["gh", "release", "upload", "v1", "dist.zip"],
    ["gh", "secret", "set", "TOKEN"],
    # `gh` resolves a repeated flag to its last occurrence, so reading the first
    # one lets a write disguise itself behind a leading safe method.
    ["gh", "api", "repos/o/r/issues/1", "--method", "GET", "--method", "DELETE"],
    # An explicit method the guard does not recognise is not a proof of safety.
    # Only GET and HEAD are; everything else — a future verb, a typo that `gh`
    # still forwards, a dangling flag whose value the argv lost — is a write.
    ["gh", "api", "repos/o/r/issues/1", "--method", "OPTIONS"],
    ["gh", "api", "repos/o/r/issues/1", "-XPOSTED"],
    ["gh", "api", "repos/o/r/issues/1", "--method"],
    # An unrecognised command shape has to fail closed: the guard cannot know
    # that a `gh` verb it has never seen is inert.
    ["gh", "some-future-command", "do-thing"],
    # The fixer's PR-body revision. `gh pr edit --body` replaces the whole body
    # and GitHub offers no undo, so an unmocked one in a test run would rewrite
    # a live PR's handoff — the single most destructive write the tick emits.
    ["gh", "pr", "edit", "1", "--body-file", "/tmp/body.md"],
])
def test_mutation_guard_classifies_writes(argv: list[str]) -> None:
    """The guard fails closed on ``gh api`` writes, not just named subcommands.

    ``gh api`` reaches every mutation the named subcommands do, so a denylist
    that stops at ``comment``/``edit``/``create`` would let an unmocked ``gh api
    --method POST`` write to the live repository while the fixture claims to
    block any mutating ``gh`` command. Reading only the method flags is not
    enough either: the method is implicit whenever the call carries body
    parameters.
    """
    from conftest import is_mutating_gh  # type: ignore[import-untyped]

    assert is_mutating_gh(argv) is True


@pytest.mark.parametrize("argv", [
    ["gh", "pr", "view", "1", "--json", "headRefOid"],
    ["gh", "api", "repos/o/r/pulls/1"],
    ["gh", "api", "repos/o/r/pulls/1", "--method", "GET"],
    # An explicit safe method beats the implicit-POST rule: `--method GET`
    # sends the fields as a query string, which is how `gh api` paginates
    # parameterised reads.
    ["gh", "api", "search/issues", "-f", "q=repo:o/r", "--method", "GET"],
    ["gh", "api", "search/issues", "-f", "q=repo:o/r", "-XGET"],
    ["git", "api", "--method", "POST"],
    "gh api -X POST repos/o/r",
])
def test_mutation_guard_allows_reads(argv: object) -> None:
    """Reads are inert and must pass through, as must non-``gh`` argv."""
    from conftest import is_mutating_gh  # type: ignore[import-untyped]

    assert is_mutating_gh(argv) is False


@pytest.mark.real_gh_helper
def test_mutation_guard_aborts_at_the_real_subprocess_boundary() -> None:
    """The autouse fixture, not just its classifier, blocks an unmocked write.

    ``is_mutating_gh`` being correct is worth nothing if the fixture that calls
    it is not installed on the boundary tests actually reach. This exercises the
    wrapper itself: an unpatched ``tick.gh`` write must raise instead of running.
    The marker opts out of the ``tick.gh`` stub so the call reaches the real
    helper and, through it, the guarded ``subprocess.run``.
    """
    import tick  # type: ignore[import-untyped]

    with pytest.raises(AssertionError, match="mutating gh command"):
        tick.gh("pr", "comment", "1", "--body", "x")


def test_mutation_guard_passes_non_gh_commands_through() -> None:
    """The wrapper still executes commands it does not classify as gh writes."""
    completed = subprocess.run(["true"], check=True)

    assert completed.returncode == 0


def test_liveness_guard_reports_a_real_foreign_process_dead() -> None:
    """A live process the suite did not spawn as itself must read as dead.

    The suite hard-codes identifiers like ``12345`` on the assumption that they
    are dead. They are not: PIDs are recycled, and both probes fail *open* on
    ``PermissionError``, so a foreign process — another user's, or a background
    service — reads as alive and silently rewrites a custody decision.

    Spawning a real child in its own session makes the hazard deterministic
    instead of waiting for a collision on a developer's machine.
    """
    import process_control  # type: ignore[import-untyped]

    proc = spawn_foreign_process()
    try:
        assert process_control.pid_is_alive(proc.pid) is False
        assert process_control.pgid_is_alive(os.getpgid(proc.pid)) is False
    finally:
        reap(proc)


def test_liveness_guard_still_sees_the_test_process_itself() -> None:
    """The guard must not blind a test that deliberately probes ``os.getpid()``.

    The start-time assertion tolerates a sandbox that blocks ``ps``, matching
    ``test_get_start_time_returns_float_for_current_process``. A guard test
    that went red because the environment cannot read start times would be the
    same machine-dependent failure this whole change exists to remove.
    """
    import process_control  # type: ignore[import-untyped]

    assert process_control.pid_is_alive(os.getpid()) is True
    assert process_control.pgid_is_alive(os.getpgrp()) is True

    start = process_control.get_process_start_time(os.getpid())
    if start is None:
        pytest.skip("ps blocked by sandbox — start time unreadable")
    assert isinstance(start, float)


def test_liveness_guard_hides_a_real_foreign_process_start_time() -> None:
    """Identity is a third OS read, and it decides which probe runs at all.

    ``_classify_marker`` reads the recorded PID's start time before it reaches
    either signal-zero probe. A dead PID yields ``None`` and the classifier
    falls through to them; a *live* one yields a real timestamp,
    ``verify_identity`` answers ``mismatched``, and the guarded probes are never
    consulted. Confining the two probes therefore does not make an invented
    PID deterministic on its own — the start-time read has to be confined too.
    """
    import process_control  # type: ignore[import-untyped]

    proc = spawn_foreign_process()
    try:
        assert process_control.get_process_start_time(proc.pid) is None
    finally:
        reap(proc)


@pytest.mark.parametrize("raw", ["0", "-1", "true", "2.5", '"4"'])
def test_main_refuses_a_malformed_max_in_flight_before_harvest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str,
) -> None:
    """A cap that is not an integer of at least 1 stops the tick at startup.

    No HARVEST and no DISPATCH run, and the error names the key: falling back
    to the shipped cap would run engines at a concurrency nobody chose.
    """
    import tick  # type: ignore[import-untyped]

    write_cap_config(monkeypatch, tmp_path, raw)

    with mock.patch("tick.git", return_value=str(tmp_path)):
        with mock.patch("tick._startup_probe"):
            with mock.patch("tick.harvest_reviewers") as harvest:
                with mock.patch("tick._run_tick_body") as body:
                    stderr = io.StringIO()
                    with mock.patch("sys.stderr", stderr):
                        rc = tick.main()

    assert rc == 3
    assert "invalid configuration" in stderr.getvalue()
    assert "max_in_flight" in stderr.getvalue()
    body.assert_not_called()
    harvest.assert_not_called()


def test_tick_never_admits_past_the_configured_cap_across_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Every stage admits against the one configured cap, counting old markers.

    Two engines from earlier ticks already hold markers, and each of the three
    stages has two eligible items. With ``max_in_flight = 4`` the tick may
    launch exactly two more, wherever they fall, and every later candidate is
    skipped on admission. The shipped cap of three would admit only one, so
    the launch count tells the configured value from the constant.
    """
    import tick  # type: ignore[import-untyped]

    write_cap_config(monkeypatch, tmp_path, "4")
    markers = earlier_tick_markers(2)
    launched: list[tuple[str, int]] = []
    fakes = cap_queue_fakes({
        81: ["ai:implemented"], 82: ["ai:implemented"],
        71: ["ai:available"], 72: ["ai:available"],
        91: ["ai:changes-requested"], 92: ["ai:changes-requested"],
    })

    def launch(kind: str, label: str) -> Callable[..., None]:
        def record(**kwargs: object) -> None:
            number = int(kwargs.get("pr_number") or kwargs["issue_number"])  # type: ignore[call-overload]
            launched.append((kind, number))
            markers.append((kind, number, label, "launching", "matched", True))
        return record

    with mock.patch("tick._startup_probe"):
        with cap_tick_patches(markers, fakes):
            with mock.patch(
                "tick._dispatch_one_reviewer",
                side_effect=launch("pr", "ai:reviewing"),
            ):
                with mock.patch(
                    "tick._dispatch_one_implementer",
                    side_effect=launch("issue", "ai:implementing"),
                ):
                    with mock.patch(
                        "tick._dispatch_one_fixer",
                        side_effect=launch("pr", "ai:fixing"),
                    ):
                        with mock.patch("sys.stderr", io.StringIO()):
                            rc = tick._run_single_tick_locked(None, "claude", "")

    assert rc == 0
    assert len(launched) == 2, launched
    admission_skips = [
        e for e in read_tick_log_entries(Path(tick.TICK_LOG_DIR))
        if e.get("action") == "skip"
        and e.get("reason") == "admission: max_in_flight"
    ]
    assert len(admission_skips) == 4, admission_skips
    assert {e["details"]["max"] for e in admission_skips} == {4}  # type: ignore[index]


def test_tick_cadence_judges_capacity_against_the_configured_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Three engines are not at capacity when the operator configured five.

    Cadence must use the cap admission used: a tick that reports itself at
    capacity while admission would still launch paces as if nothing could
    start.
    """
    import pipeline  # type: ignore[import-untyped]
    import tick  # type: ignore[import-untyped]

    write_cap_config(monkeypatch, tmp_path, "5")
    markers = earlier_tick_markers(3)

    with mock.patch("tick._startup_probe"):
        with cap_tick_patches(markers, cap_queue_fakes({})):
            with mock.patch(
                "tick.pipeline.compute_next_wake",
                wraps=pipeline.compute_next_wake,
            ) as next_wake:
                with mock.patch("sys.stderr", io.StringIO()):
                    rc = tick._run_single_tick_locked(None, "claude", "")

    assert rc == 0
    assert next_wake.call_args.kwargs["in_flight_count"] == 3
    assert next_wake.call_args.kwargs["at_capacity"] is False


def test_observe_loop_state_uses_the_configured_cap() -> None:
    """Loop occupancy and its fail-closed fallback both use the passed cap."""
    import tick  # type: ignore[import-untyped]

    with mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        with mock.patch("tick.read_authoritative_human_holds", return_value={}):
            with mock.patch(
                "tick._scan_marker_identities",
                return_value=earlier_tick_markers(3),
            ):
                with mock.patch("tick._has_actionable_queue", return_value=True):
                    with mock.patch(
                        "tick._nearest_actionable_engine_deadline",
                        return_value=None,
                    ):
                        below = tick._observe_loop_state(max_in_flight=5)
            with mock.patch(
                "tick._scan_marker_identities",
                side_effect=RuntimeError("unreadable marker root"),
            ):
                with mock.patch(
                    "tick._nearest_actionable_engine_deadline",
                    return_value=None,
                ):
                    unknown = tick._observe_loop_state(max_in_flight=5)

    assert below["in_flight_count"] == 3
    assert below["at_cap"] is False
    # Unknown occupancy assumes the configured cap is full, not the shipped one.
    assert unknown["in_flight_count"] == 5
    assert unknown["at_cap"] is True
    assert unknown["quiescent"] is False


def test_loop_observes_cadence_with_the_cap_its_tick_admitted_against(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``--loop`` hands the startup-validated cap to the tick and to cadence.

    The cap is read once, at the startup gate, so the observation after the
    tick cannot pick up a different value than the one admission used.
    """
    import tick  # type: ignore[import-untyped]

    write_cap_config(monkeypatch, tmp_path, "5")
    observed: list[object] = []

    def observe(_target: object = None, **kwargs: object) -> object:
        observed.append(kwargs.get("max_in_flight"))
        return {
            "in_flight_count": 0,
            "nearest_engine_deadline": None,
            "actionable_queue": False,
            "at_cap": False,
            "quiescent": True,
        }

    def edit_config_mid_tick(*_args: object, **_kwargs: object) -> int:
        # The operator lowers the cap while this tick runs; the edit is for
        # the next tick, not for this tick's cadence.
        write_cap_config(monkeypatch, tmp_path, "1")
        return 0

    monkeypatch.setattr(tick, "_observe_loop_state", observe)
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_a: None)
    monkeypatch.setattr(tick, "_wait_next_tick", lambda *_a: (False, True))

    with mock.patch("tick._startup_probe"):
        with mock.patch(
            "tick._run_tick_body", side_effect=edit_config_mid_tick,
        ) as body:
            with mock.patch("tick._notify_loop_quiescent_stop"):
                with mock.patch("sys.stderr", io.StringIO()):
                    tick._run_loop_locked(None, "claude", "", quiescent_stop_after=1)

    assert body.call_args.kwargs["config"].max_in_flight == 5
    assert observed == [5]


# --- Helpers ---


def write_cap_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str,
) -> None:
    """Point the resolver at an operator config with one ``max_in_flight``.

    ``[defaults]`` holds a different valid cap, so a test passes only when the
    repository override is the value the tick used.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        tmp_path: The test's temporary directory.
        raw: The TOML literal for the stubbed target's ``max_in_flight``.
    """
    cfg = tmp_path / "cap-config.toml"
    cfg.write_text(
        f'[operator]\nlogin = "{TRUSTED_AUTHOR}"\n\n'
        '[defaults]\nmax_in_flight = 2\n\n'
        f'[repos."{TRUSTED_AUTHOR}/test-repo"]\nmax_in_flight = {raw}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))


def earlier_tick_markers(
    count: int,
) -> list[tuple[str, int, str, str, str, bool]]:
    """Return live reviewer markers that earlier ticks left in the marker root.

    Args:
        count: How many engines are already running.

    Returns:
        Marker identities in the ``_scan_marker_identities`` shape, numbered
        away from any candidate a test queues.
    """
    return [
        ("pr", 500 + n, "ai:reviewing", "live", "matched", True)
        for n in range(count)
    ]


def cap_queue_fakes(
    labels_by_number: dict[int, list[str]],
) -> tuple[Callable[..., str], Callable[..., object]]:
    """Return ``gh_text`` and ``gh_json`` fakes serving one label snapshot.

    Each queue listing returns the numbers carrying its label; each point-read
    returns that item's labels; every candidate is the operator's own,
    same-repository work.

    Args:
        labels_by_number: The workflow labels each open item carries.

    Returns:
        ``(fake_gh_text, fake_gh_json)``.
    """
    import tick  # type: ignore[import-untyped]

    def numbers_for(args: tuple[str, ...]) -> list[int]:
        label = args[args.index("--label") + 1]
        return [n for n, ls in labels_by_number.items() if label in ls]

    def fake_gh_text(*args: str) -> str:
        if "list" in args:
            return "\n".join(str(n) for n in numbers_for(args))
        if "body" in args:
            return ""
        return "\n".join(labels_by_number.get(int(args[2]), []))

    def fake_gh_json(*args: str) -> object:
        if "search" in args:
            return []
        if "list" in args:
            return [
                {"number": n, "title": f"Candidate {n}",
                 "author": {"login": TRUSTED_AUTHOR}}
                for n in numbers_for(args)
            ]
        return {
            "author": {"login": tick.TRUSTED_REVIEW_AUTHOR},
            "headRepository": {"nameWithOwner": tick.TRUSTED_HEAD_REPO},
            "isCrossRepository": False,
            "labels": [{"name": label} for label in labels_by_number.get(int(args[2]), [])],
        }

    return fake_gh_text, fake_gh_json


def cap_tick_patches(
    markers: list[tuple[str, int, str, str, str, bool]],
    fakes: tuple[Callable[..., str], Callable[..., object]],
) -> contextlib.ExitStack:
    """Patch a whole tick down to its GitHub reads and its marker scan.

    HARVEST reports success with nothing to reap, and the marker scan returns
    ``markers`` as it stands at each call, so a launch that appends to it is
    counted by every admission check after it.

    Args:
        markers: The live marker identities, shared with the launch stubs.
        fakes: ``(gh_text, gh_json)`` fakes from :func:`cap_queue_fakes`.

    Returns:
        An exit stack holding the patches; enter it with ``with``.
    """
    fake_gh_text, fake_gh_json = fakes
    stack = contextlib.ExitStack()
    for target, kwargs in (
        ("tick.gh_text", {"side_effect": fake_gh_text}),
        ("tick.gh_json", {"side_effect": fake_gh_json}),
        ("tick.verify_codex_bin", {}),
        ("tick.harvest_reviewers", {"return_value": True}),
        ("tick.harvest_implementers", {"return_value": True}),
        ("tick.harvest_fixers", {"return_value": True}),
        ("tick.read_authoritative_in_flight_labels", {"return_value": []}),
        ("tick.read_authoritative_human_holds", {"return_value": {}}),
        ("tick._scan_marker_identities", {"side_effect": lambda: list(markers)}),
    ):
        stack.enter_context(mock.patch(target, **kwargs))
    return stack


def spawn_foreign_process() -> subprocess.Popen[bytes]:
    """Start a real, long-lived process in its own session.

    ``start_new_session=True`` makes the child lead its own process group, so
    its PID and PGID are both identifiers this suite genuinely does not own —
    which is what the liveness guard has to answer ``dead`` for. Spawning one
    reproduces the recycled-identifier hazard on every run instead of waiting
    for a collision on a developer's machine.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )


def reap(proc: subprocess.Popen[bytes]) -> None:
    """Kill a spawned child and wait for it, so no test leaks a process."""
    proc.kill()
    proc.wait()


def foreign_process_marker(
    tmp_path: Path, *, pid: int, pgid: int,
) -> tuple[str, dict[str, object]]:
    """Build a harvestable implement-stage marker naming a foreign process.

    Creates the worktree directory and its non-empty ``result.json`` so the
    only thing left for ``_classify_marker`` to decide is liveness.

    Returns:
        The worktree path and a marker whose ``pid``/``pgid`` name a process
        the suite does not own.
    """
    wt_dir = tmp_path / "issue-99"
    wt_dir.mkdir()
    (wt_dir / "result.json").write_text('{"result": "IMPLEMENTATION_COMPLETE"}')
    return str(wt_dir), {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:available",
        "dispatch_id": "d-99",
        "pid": pid,
        "start_time": 1234567.0,
        "pgid": pgid,
    }


def gh_json_no_holds(*args: str, **_kwargs: object) -> object:
    """gh_json mock for cadence tests: empty queues, no human holds.

    Cadence reads two differently shaped payloads — labelled queue *lists* and
    per-item label *views* (the bounded human-hold read). A single
    ``return_value`` cannot serve both, and a list handed to the hold read is
    a malformed payload that correctly fails closed.
    """
    if len(args) > 1 and args[1] == "view":
        return {"labels": []}
    return []


def gh_json_four_foreign_implementing_labels(
    *args: str, **_kwargs: object
) -> object:
    """gh_json mock for the 2026-08-20 starvation incident, queues empty.

    Reproduces the exact shape #663 was filed for: four open issues carrying a
    hand-set ``ai:implementing`` that no engine on this machine launched, with
    every dispatch-eligible queue empty and no human holds. Only the
    ``ai:implementing`` issue list answers with items, so the real
    ``read_authoritative_in_flight_labels`` builds the four foreign refs
    itself rather than the test handing ``InFlightRef``s to a stub.
    """
    if len(args) > 1 and args[1] == "view":
        return {"labels": []}
    if args[:2] == ("issue", "list") and "ai:implementing" in args:
        return [{"number": n} for n in (502, 504, 507, 608)]
    return []


def run_git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return its stdout."""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout


def reviewer_live_identity(base_ref: str, head: str) -> Callable[..., object]:
    """gh_json mock for reviewer harvest: identity point-read, then comments.

    The live-identity call (``--json baseRefName,headRefOid``) returns a
    dict; every other gh_json call in the harvest is the comments read and
    returns an empty array.
    """
    def side_effect(*args: str) -> object:
        if "baseRefName,headRefOid" in args:
            return {"baseRefName": base_ref, "headRefOid": head}
        return []
    return side_effect


def fake_gh_json_terminal_pr_578(*args: str) -> object:
    """gh_json mock: PR #578 is merged while the global queue stays actionable.

    Every ``list`` read returns a dispatchable page, so an unscoped probe sees
    actionable work; the point-read of #578 shows a merged, terminal PR.
    """
    if args[1] == "list":
        return [{"labels": [{"name": "ai:changes-requested"}]}]
    if args[:3] == ("pr", "view", "578"):
        return {"labels": [{"name": "ai:merged"}], "state": "MERGED"}
    raise AssertionError(f"unexpected gh call: {args}")


def capture_comment_bodies(
    sink: list[str], entity: str = "pr",
) -> Callable[..., object]:
    """tick.gh mock that records the body of each ``gh <entity> comment`` call.

    The harvest deletes the body file in a ``finally`` block, so the content
    has to be read inside the mock rather than after the call returns.
    """
    def side_effect(*args: str) -> object:
        if args[:2] == (entity, "comment"):
            with open(args[4]) as fh:
                sink.append(fh.read())
        return mock.Mock()
    return side_effect


def gh_json_head_and_labels(
    head: str, labels: list[str],
) -> Callable[..., object]:
    """tick.gh_json mock serving a PR head OID and a pipeline label set.

    Harvest paths point-read both, and a terminal resume classifies the live
    label set — a mock that answers only the head query reads as an unreadable
    label state and makes the resume fail closed.
    """
    def side_effect(*args: str) -> object:
        if "labels" in args:
            return {"labels": [{"name": name} for name in labels]}
        return {"headRefOid": head}
    return side_effect


def label_state_gh_mock(
    labels: list[str], sink: list[str], *, entity: str = "pr",
) -> tuple[Callable[..., object], list[tuple[object, ...]], list[str]]:
    """tick.gh mock that applies each edit to a real label set.

    Unlike :func:`terminal_gh_mock`, which hardcodes convergence, this one
    applies the exact ``--add-label`` / ``--remove-label`` arguments the
    production code emits, so the read-back a helper performs after its own
    mutation reflects what that mutation actually did. That is what lets a test
    distinguish a converge from a pure release without asserting on argv alone.

    Args:
        labels: The item's starting ``ai:`` label set.
        sink: Receives the body of every posted comment.
        entity: ``"pr"`` or ``"issue"``.

    Returns:
        The side-effect function, the recorded call list, and the live label
        state — which the test reads back to assert the final remote shape.
    """
    state = list(labels)
    calls: list[tuple[object, ...]] = []

    def side_effect(*args: str, **_kwargs: object) -> object:
        calls.append(args)
        if args[:2] == (entity, "comment"):
            with open(args[4]) as fh:
                sink.append(fh.read())
            return mock.Mock()
        if args[:2] == (entity, "edit"):
            rest = list(args[3:])
            while rest:
                flag, value, rest = rest[0], rest[1], rest[2:]
                if flag == "--add-label" and value not in state:
                    state.append(value)
                elif flag == "--remove-label" and value in state:
                    state.remove(value)
            return mock.Mock()
        if args[:2] == (entity, "view") and "labels" in args:
            payload = json.dumps(
                {"labels": [{"name": name} for name in state]},
            )
            return mock.Mock(stdout=payload)
        return mock.Mock()

    return side_effect, calls, state


def terminal_gh_mock(
    labels: list[str], sink: list[str], *, entity: str = "pr",
) -> tuple[Callable[..., object], list[tuple[object, ...]]]:
    """tick.gh mock for terminal-resume probes: label views + comment capture.

    ``gh view --json labels`` returns the supplied remote label set; ``gh edit``
    converges the state to ``ai:needs-human-decision`` (recording the call, so a
    test can prove no remote mutation was attempted); comment calls append their
    body to ``sink``; everything else is a successful no-op.

    Returns:
        The side-effect function and the recorded call list.
    """
    state = list(labels)
    calls: list[tuple[object, ...]] = []

    def side_effect(*args: str, **_kwargs: object) -> object:
        calls.append(args)
        if args[:2] == (entity, "comment"):
            with open(args[4]) as fh:
                sink.append(fh.read())
            return mock.Mock()
        if args[:2] == (entity, "edit"):
            state[:] = ["ai:needs-human-decision"]
            return mock.Mock()
        if args[:2] == (entity, "view") and "labels" in args:
            payload = json.dumps(
                {"labels": [{"name": name} for name in state]},
            )
            return mock.Mock(stdout=payload)
        return mock.Mock()

    return side_effect, calls


def read_tick_log_entries(log_dir: Path) -> list[dict[str, object]]:
    """Parse every entry across every tick log under ``log_dir``.

    Reads all files rather than only the newest, so a claim spanning more than
    one tick still sees every entry if a log rotation lands between them.
    """
    entries: list[dict[str, object]] = []
    for log_file in sorted(log_dir.glob("tick-*.jsonl")):
        with open(log_file) as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    entries.append(json.loads(stripped))
    return entries


def stall_episode_ids(tick: ModuleType) -> set[str]:
    """Dispatch identities in the persisted stall-episode memory.

    Read from the file the scan actually wrote rather than from an in-process
    map, because surviving process exit is the property under test.
    """
    path = Path(tick._STALL_EPISODES_FILE)  # noqa: SLF001 - reading the artifact
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def stalled_quiet_seconds(tick: ModuleType) -> float:
    """Seconds of heartbeat silence that must classify as a stall.

    Derived from the live threshold rather than hardcoded. The threshold has to
    keep real margin over the longest legitimate silence (a maxed-out tool
    call), so its value moves — and a fixed "20 minutes past" would quietly
    slide to the working side of the boundary when it did, leaving these tests
    green while proving nothing about the stalled case.
    """
    return tick.ENGINE_STALL_SECONDS + 5 * 60


def write_stalled_marker(
    wt_dir: Path,
    *,
    start_time: float,
    heartbeat_age: float | None,
    stage: str = "implement",
    pid: int = 4242,
    dispatch_id: str = "d-stalled",
    time_to_deadline: float = 20 * 60,
) -> None:
    """Write a live-engine marker whose progress evidence is aged on purpose.

    ``heartbeat_age`` is seconds before now to backdate the heartbeat mtime, or
    ``None`` to omit the heartbeat file entirely (the pre-first-event window).
    """
    now = time.time()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "dispatch_id": dispatch_id,
        "stage": stage,
        "pr_number": 577,
        "engine": "claude",
        "pid": pid,
        "start_time": start_time,
        "deadline": now + time_to_deadline,
    }))
    # result.json exists at zero bytes from spawn for wrapperless engines.
    (wt_dir / "result.json").write_text("")
    if heartbeat_age is not None:
        heartbeat = wt_dir / "heartbeat"
        heartbeat.write_text("")
        os.utime(heartbeat, (now - heartbeat_age, now - heartbeat_age))


def closing_pr_reference(number: int) -> dict[str, object]:
    """Build the closing-reference shape returned by GitHub issue view."""
    return {
        "id": f"PR_{number}",
        "number": number,
        "repository": {"name": "test-repo", "owner": {"login": "test-owner"}},
        "url": f"https://github.com/test-owner/test-repo/pull/{number}",
    }


def iso_utc(epoch: float) -> str:
    """Render epoch seconds as GitHub's ISO-8601 ``Z`` timestamp."""
    return (
        dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def write_stale_review_marker(wt_dir: Path, *, pr: int, dispatch_id: str) -> None:
    """Write a *valid* review marker whose engine is dead and past deadline.

    Dual-label convergence is gated on marker validity, so a test that wants
    the escalation must prove custody first. An expired deadline with a dead
    PID classifies as ``stale``, which is the state the convergence is for —
    a contradictory remote label set on work the pipeline demonstrably owns.
    """
    wt_dir.joinpath("dispatch.json").write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": pr,
        "deadline": 1.0,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": dispatch_id,
        "pid": 2147483646,
        "start_time": 1.0,
        "pgid": 2147483646,
    }))


def stub_gh_json(*, labels: list[str]) -> Callable[..., object]:
    """Return a ``gh_json`` side effect serving label reads only.

    A raw ``gh api`` call fails the test on purpose: reconciliation decides a
    markerless item from its label alone, so any timeline read would be an
    attempt to infer custody the pipeline cannot prove (issue #592).
    """
    def side_effect(*args: str) -> object:
        assert not (args and args[0] == "api"), (
            f"reconciliation must not read the GitHub timeline: {args}"
        )
        return {"labels": [{"name": name} for name in labels]}

    return side_effect


def git_init_repo(path: Path) -> None:
    """Initialise a throwaway git repo with one commit at ``path``."""
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, check=True, capture_output=True,
        )

    run("init", "-b", "main")
    run("config", "user.email", "tick-test@example.com")
    run("config", "user.name", "tick test")
    run("commit", "--allow-empty", "-m", "root")


def archive_with_reuse(wt_root: Path, wt_dir: Path) -> str:
    """Prepare an implementer escalation archive the way recovery does."""
    from tick import _archive_harvest_evidence  # type: ignore[import-untyped]

    return _archive_harvest_evidence(
        wt_root=str(wt_root),
        wt_dir=str(wt_dir),
        kind="issue",
        number=42,
        dispatch_id="impl-42",
        tick_log=None,
        stage="implement",
        required_files=("result.json",),
        reuse_existing=True,
    )


def torn_copy_then_disk_full(src: str, dst: str, *args: object, **kwargs: object) -> None:
    """Write half of ``src`` to ``dst``, then fail as a full disk would."""
    import errno

    data = Path(src).read_bytes()
    Path(dst).write_bytes(data[: len(data) // 2])
    raise OSError(errno.ENOSPC, "No space left on device")


def disk_full_open(basename: str) -> Callable[..., object]:
    """Return an ``open`` whose writes to ``basename`` stop halfway and fail."""
    import builtins
    import errno

    class HalfWriteFile:
        def __init__(self, handle: io.TextIOBase) -> None:
            self.handle = handle

        def __enter__(self) -> "HalfWriteFile":
            return self

        def __exit__(self, *exc: object) -> None:
            self.handle.close()

        def write(self, text: str) -> int:
            self.handle.write(text[: len(text) // 2])
            self.handle.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

    def fake_open(path: str, mode: str = "r", *args: object, **kwargs: object) -> object:
        handle = builtins.open(path, mode, *args, **kwargs)
        if "w" in mode and basename in os.path.basename(path):
            return HalfWriteFile(handle)
        return handle

    return fake_open


def write_tuned_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, section: str,
) -> Path:
    """Write an operator config whose target section holds ``section``."""
    import pipeline  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[operator]\nlogin = "test-owner"\n'
        f'\n[repos."test-owner/test-repo"]\n{section}',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    monkeypatch.setattr(
        pipeline, "_PACKAGED_CONFIG_PATH",
        Path(pipeline.__file__).resolve().parent.parent / "config.toml",
    )
    return cfg


@contextlib.contextmanager
def tick_stage_mocks(tmp_path: Path) -> Iterator[dict[str, mock.MagicMock]]:
    """Patch every HARVEST and DISPATCH stage of a real ``tick.main()``.

    Yields the stage mocks by name. The post-dispatch state reports an
    actionable queue, so the emitted cadence is the short poll.
    """
    with contextlib.ExitStack() as stack:
        for target, value in (
            ("tick.git", str(tmp_path)),
            ("tick._startup_probe",
             {"gh_found": True, "git_found": True, "codex_found": True}),
            ("tick.read_authoritative_in_flight_labels", []),
            ("tick._compute_post_dispatch_state", (0, True, ())),
            ("tick._compute_nearest_deadline_s", None),
        ):
            stack.enter_context(mock.patch(target, return_value=value))
        yield {
            name: stack.enter_context(mock.patch(
                f"tick.{name}",
                **({"return_value": True} if name.startswith("harvest") else {}),
            ))
            for name in (
                "harvest_reviewers", "harvest_implementers", "harvest_fixers",
                "dispatch_reviewers", "dispatch_implementers", "dispatch_fixers",
            )
        }
