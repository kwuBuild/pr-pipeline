"""Real child lifecycle tests for advisory loop completion wakes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest
import tick
from process_control import pgid_is_alive, pid_is_alive


@pytest.fixture
def engine_worktree(tmp_path, monkeypatch):
    wt_dir = tmp_path / "issue-7"
    wt_dir.mkdir()
    monkeypatch.setattr(tick, "WT_ROOT", str(tmp_path))
    monkeypatch.setattr(tick, "WAKE_REQUEST_PATH", str(tmp_path / "wake-request"))
    monkeypatch.setattr(tick, "WAKE_WATCH_POLL_SECONDS", 0.02)
    # Stall reporting is unrelated and can spawn ps, which incidentally reaps
    # abandoned Popen objects on CPython and masks this completion regression.
    monkeypatch.setattr(tick, "_scan_stalled_engines_safely", lambda _: False)
    engine = tmp_path / "claude"
    engine.write_text(
        f"#!{sys.executable}\n"
        "import json, os, time\n"
        "from pathlib import Path\n"
        "while not Path('exit').exists(): time.sleep(0.01)\n"
        "if Path('keep-child').exists():\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os.close(0); os.close(1); os.close(2)\n"
        "        while not Path('child-exit').exists(): time.sleep(0.01)\n"
        "        os._exit(0)\n"
        "print(json.dumps({'type': 'result', 'result': 'done'}))\n"
    )
    engine.chmod(0o755)
    proc = tick._launch_claude_engine(
        engine_bin=str(engine), prompt="fixture", wt_dir=str(wt_dir),
        result_path=str(wt_dir / "result.json"), env=os.environ.copy(),
        push_permission="Bash(git push:*)", verify_tools=(),
    )
    assert proc is not None
    # The suite blocks foreign PID probes by default. Allow only this fixture's
    # process and group through to the real OS checks.
    monkeypatch.setattr(
        tick.process_control, "pid_is_alive",
        lambda pid: pid == proc.pid and pid_is_alive(pid),
    )
    monkeypatch.setattr(
        tick.process_control, "pgid_is_alive",
        lambda pgid: pgid == proc.pid and pgid_is_alive(pgid),
    )
    try:
        yield wt_dir, proc
    finally:
        (wt_dir / "exit").touch()
        (wt_dir / "child-exit").touch()
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while pgid_is_alive(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not pgid_is_alive(proc.pid), "fixture process group survived teardown"
        # Reap through the production scan if this version retains handles.
        tick._scan_finished_engines(set())


@pytest.mark.parametrize("stage", ["implement", "fix"])
def test_watcher_reaps_exited_engine_without_another_subprocess(engine_worktree, stage):
    wt_dir, proc = engine_worktree
    marker = {"dispatch_id": "fixture", "stage": stage, "pid": proc.pid, "pgid": proc.pid}
    (wt_dir / "dispatch.json").write_text(json.dumps(marker))
    stop = threading.Event()
    wake = threading.Event()
    watcher = tick._start_wake_watcher(stop, wake)
    assert watcher is not None
    try:
        assert not wake.wait(0.1), "live engine must not consume its completion edge"
        (wt_dir / "exit").touch()
        assert wake.wait(3), "zombie engine blocked completion wake"
        # Reading returncode does not reap, unlike poll()/wait(). The watcher
        # must have collected it, and the OS must no longer expose the PID.
        assert proc.returncode == 0
        assert not tick.process_control.pid_is_alive(proc.pid)
        assert (wt_dir / "dispatch.json").read_text() == json.dumps(marker)
    finally:
        stop.set()
        watcher.join(timeout=3)


@pytest.mark.parametrize("stage", ["implement", "fix"])
def test_reaped_wrapper_does_not_wake_while_descendant_is_live(engine_worktree, stage):
    wt_dir, proc = engine_worktree
    marker = {"dispatch_id": "live-child", "stage": stage, "pid": proc.pid, "pgid": proc.pid}
    (wt_dir / "dispatch.json").write_text(json.dumps(marker))
    (wt_dir / "keep-child").touch()
    (wt_dir / "exit").touch()
    seen = set()
    deadline = time.monotonic() + 5
    while proc.returncode is None and time.monotonic() < deadline:
        assert not tick._scan_finished_engines(seen)
        time.sleep(0.01)
    assert proc.returncode == 0
    assert not pid_is_alive(proc.pid)
    assert pgid_is_alive(proc.pid)
    assert not tick._scan_finished_engines(seen)
    assert seen == set()
    assert (wt_dir / "dispatch.json").exists()
    (wt_dir / "child-exit").touch()
    deadline = time.monotonic() + 5
    while not tick._scan_finished_engines(seen) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == {"live-child"}
    assert not tick._scan_finished_engines(seen), "completion must nudge only once"


def test_reaper_does_not_collect_an_unregistered_child(tmp_path, monkeypatch):
    monkeypatch.setattr(tick, "WT_ROOT", str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(17)"])
    try:
        time.sleep(0.1)
        tick._scan_finished_engines(set())
        # An indiscriminate waitpid(-1) would steal this status from its owner.
        assert proc.returncode is None
        assert proc.wait(timeout=5) == 17
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
