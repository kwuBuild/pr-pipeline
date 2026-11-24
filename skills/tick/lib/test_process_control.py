"""Tests for bounded reviewer-process termination."""

import json
import signal
import subprocess
import sys
import time
from importlib.util import find_spec
from unittest import mock

import process_control
import pytest


def test_process_control_module_exists() -> None:
    """Rollback has a dedicated executable helper instead of inline unbounded wait."""
    assert find_spec("process_control") is not None


def test_terminate_process_stops_after_term() -> None:
    """A cooperative child exits during the bounded TERM grace period."""
    alive = True
    sent: list[int] = []
    clock = FakeClock()

    def send_signal(_pid: int, sig: int) -> None:
        nonlocal alive
        sent.append(sig)
        if sig == signal.SIGTERM:
            alive = False

    stopped = process_control.terminate_process(
        42,
        grace_seconds=1.0,
        kill_grace_seconds=1.0,
        poll_interval=0.1,
        is_alive=lambda _pid: alive,
        send_signal=send_signal,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert stopped is True
    assert sent == [signal.SIGTERM]


def test_terminate_process_escalates_to_kill_after_term_timeout() -> None:
    """An uncooperative child receives KILL only after the TERM deadline."""
    alive = True
    sent: list[int] = []
    clock = FakeClock()

    def send_signal(_pid: int, sig: int) -> None:
        nonlocal alive
        sent.append(sig)
        if sig == signal.SIGKILL:
            alive = False

    stopped = process_control.terminate_process(
        42,
        grace_seconds=0.2,
        kill_grace_seconds=0.2,
        poll_interval=0.1,
        is_alive=lambda _pid: alive,
        send_signal=send_signal,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert stopped is True
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_terminate_process_reports_failure_when_kill_does_not_stop_child() -> None:
    """Rollback preserves ownership when bounded TERM and KILL both fail."""
    sent: list[int] = []
    clock = FakeClock()

    stopped = process_control.terminate_process(
        42,
        grace_seconds=0.2,
        kill_grace_seconds=0.2,
        poll_interval=0.1,
        is_alive=lambda _pid: True,
        send_signal=lambda _pid, sig: sent.append(sig),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert stopped is False
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_cli_terminate_reports_confirmed_stop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shell-facing CLI returns success only for confirmed process death."""
    called: dict[str, object] = {}

    def terminate(pid: int, **kwargs: object) -> bool:
        called.update(pid=pid, **kwargs)
        return True

    monkeypatch.setattr(process_control, "terminate_process", terminate)

    rc = process_control.main(
        ["terminate", "--pid", "42", "--grace", "3", "--kill-grace", "1", "--poll", "0.2"]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"stopped": True}
    assert called == {
        "pid": 42,
        "grace_seconds": 3.0,
        "kill_grace_seconds": 1.0,
        "poll_interval": 0.2,
    }


# --- get_process_start_time ---


def test_get_start_time_returns_float_for_current_process() -> None:
    start = process_control.get_process_start_time(process_control.os.getpid())
    if start is None:
        pytest.skip("ps blocked by sandbox — start time unreadable")
    assert isinstance(start, float)
    assert start > 0


def test_get_start_time_returns_none_for_nonexistent_pid() -> None:
    """The platform reader, not the guarded wrapper, must answer None.

    ``_no_foreign_process_probes`` intercepts the public
    ``get_process_start_time`` for every PID but the pytest process's own, so
    asserting through it would only prove the guard answers ``None`` — the
    production missing-PID branch would go unexercised. Call the platform
    reader directly instead. ``999999999`` exceeds ``PID_MAX`` on both macOS
    and Linux, so it names nothing on either.
    """
    if sys.platform == "linux":
        assert process_control._linux_start_time(999999999) is None
    else:
        assert process_control._bsd_start_time(999999999) is None


# --- get_process_state ---


def test_get_process_state_reads_a_live_process() -> None:
    state = process_control.get_process_state(process_control.os.getpid())
    if state is None:
        pytest.skip("ps blocked by sandbox — process state unreadable")
    assert state[0] not in "ZXx"


def test_get_process_state_reports_an_unreaped_child_as_zombie() -> None:
    """A killed child keeps its PID and start time until the parent reaps it.

    The suite guard confines the public helper to the pytest process, so this
    reads the child through the platform reader that the helper dispatches to.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        proc.kill()
        deadline = time.monotonic() + 10
        state = None
        while time.monotonic() < deadline:  # poll() would reap the zombie.
            state = platform_state_reader()(proc.pid)
            if state and state.startswith("Z"):
                break
            time.sleep(0.05)
        assert state is not None and state.startswith("Z"), state
    finally:
        proc.wait(timeout=10)


def test_get_process_state_returns_none_for_nonexistent_pid() -> None:
    if sys.platform == "linux":
        assert process_control._linux_process_state(999999999) is None
    else:
        assert process_control._bsd_process_state(999999999) is None


@pytest.mark.parametrize(("stat", "expected"), [
    ("42 (py thon) Z 1 42 42 0 -1", "Z"),
    ("42 (a) b) S 1 42 42 0 -1", "S"),
    ("42 (x) X 1", "X"),
    ("garbage", None),
    ("42 (x)", None),
])
def test_linux_process_state_parses_the_stat_state_field(stat, expected) -> None:
    with mock.patch("builtins.open", mock.mock_open(read_data=stat)):
        assert process_control._linux_process_state(42) == expected


@pytest.mark.parametrize(("stdout", "code", "expected"), [
    ("Z\n", 0, "Z"),
    ("  Ss+ \n", 0, "Ss+"),
    ("", 1, None),
    ("   \n", 0, None),
])
def test_bsd_process_state_reads_ps_stat(stdout, code, expected) -> None:
    result = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
    with mock.patch.object(process_control.subprocess, "run", return_value=result) as run:
        assert process_control._bsd_process_state(42) == expected
    assert run.call_args.args[0] == ["ps", "-o", "stat=", "-p", "42"]


def test_bsd_process_state_unreadable_when_ps_fails() -> None:
    with mock.patch.object(process_control.subprocess, "run", side_effect=OSError):
        assert process_control._bsd_process_state(42) is None


def test_parse_lstart_macos_format() -> None:
    result = process_control._parse_lstart("Wed 24 Jun 01:11:43 2026")
    assert isinstance(result, float)
    assert result > 0


def test_parse_lstart_linux_format() -> None:
    result = process_control._parse_lstart("Wed Jun 24 01:11:43 2026")
    assert isinstance(result, float)
    assert result > 0


def test_parse_lstart_interprets_as_local_time() -> None:
    """ps -o lstart= outputs local time; _parse_lstart must use mktime not timegm."""
    import time as time_mod
    lstart = "Wed 24 Jun 01:11:43 2026"
    result = process_control._parse_lstart(lstart)
    ts = time_mod.strptime(lstart, "%a %d %b %H:%M:%S %Y")
    expected = time_mod.mktime(ts)
    assert result == expected


def test_parse_lstart_garbage_returns_none() -> None:
    assert process_control._parse_lstart("not a date") is None


def test_terminate_process_group_stops_whole_group() -> None:
    """terminate_process_group sends signals to os.killpg, not os.kill."""
    alive = True
    sent: list[tuple[str, int]] = []
    clock = FakeClock()

    def send_group_signal(pgid: int, sig: int) -> None:
        nonlocal alive
        sent.append(("group", sig))
        if sig == signal.SIGTERM:
            alive = False

    stopped = process_control.terminate_process_group(
        42,
        grace_seconds=1.0,
        kill_grace_seconds=1.0,
        poll_interval=0.1,
        group_is_alive=lambda _pgid: alive,
        send_group_signal=send_group_signal,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert stopped is True
    assert sent == [("group", signal.SIGTERM)]


def test_terminate_process_group_escalates_to_kill() -> None:
    """An uncooperative group receives SIGKILL after TERM timeout."""
    alive = True
    sent: list[tuple[str, int]] = []
    clock = FakeClock()

    def send_group_signal(pgid: int, sig: int) -> None:
        nonlocal alive
        sent.append(("group", sig))
        if sig == signal.SIGKILL:
            alive = False

    stopped = process_control.terminate_process_group(
        42,
        grace_seconds=0.2,
        kill_grace_seconds=0.2,
        poll_interval=0.1,
        group_is_alive=lambda _pgid: alive,
        send_group_signal=send_group_signal,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert stopped is True
    assert sent == [("group", signal.SIGTERM), ("group", signal.SIGKILL)]


def test_terminate_process_group_fails_when_wrapper_dies_but_child_survives() -> None:
    """Group liveness must be confirmed, not just wrapper PID death."""
    group_alive = True
    sent: list[tuple[str, int]] = []
    clock = FakeClock()

    def send_group_signal(pgid: int, sig: int) -> None:
        sent.append(("group", sig))
        # SIGTERM kills wrapper but child ignores; SIGKILL also fails here
        # (simulating an unkillable child, e.g. zombie or immune process)

    stopped = process_control.terminate_process_group(
        42,
        grace_seconds=0.2,
        kill_grace_seconds=0.2,
        poll_interval=0.1,
        group_is_alive=lambda _pgid: group_alive,
        send_group_signal=send_group_signal,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert stopped is False
    assert sent == [("group", signal.SIGTERM), ("group", signal.SIGKILL)]


def test_cli_terminate_group_reports_confirmed_stop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: dict[str, object] = {}

    def terminate_group(pgid: int, **kwargs: object) -> bool:
        called.update(pgid=pgid, **kwargs)
        return True

    monkeypatch.setattr(process_control, "terminate_process_group", terminate_group)
    rc = process_control.main(
        ["terminate-group", "--pgid", "42",
         "--grace", "3", "--kill-grace", "1", "--poll", "0.2"]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"stopped": True}
    assert called["pgid"] == 42
    assert "pid" not in called


def test_cli_get_start_time_returns_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The get-start-time subcommand returns JSON with a start_time field."""
    monkeypatch.setattr(process_control, "get_process_start_time", lambda _pid: 1234567890.0)
    rc = process_control.main(["get-start-time", "--pid", "42"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"start_time": 1234567890.0}


def test_cli_get_start_time_returns_null_for_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The get-start-time subcommand returns null when the process is not found."""
    monkeypatch.setattr(process_control, "get_process_start_time", lambda _pid: None)
    rc = process_control.main(["get-start-time", "--pid", "999999999"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"start_time": None}


def test_terminate_process_group_resolves_its_probe_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default group probe must be looked up when called, not at import.

    A default argument captures the function object at ``def`` time, which
    makes the conftest liveness guard — and any other monkeypatch of
    ``pgid_is_alive`` — invisible to this path. That leaves one route by which
    a test can still probe a real process group.
    """
    probed: list[int] = []

    def recording_probe(pgid: int) -> bool:
        probed.append(pgid)
        return False

    monkeypatch.setattr(process_control, "pgid_is_alive", recording_probe)

    stopped = process_control.terminate_process_group(
        4242,
        grace_seconds=1.0,
        kill_grace_seconds=1.0,
        poll_interval=0.1,
    )

    assert stopped is True
    assert probed == [4242]


def test_terminate_process_resolves_its_probe_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single-PID default probe is late-bound for the same reason.

    The two termination helpers are siblings: both took their probe as a
    default argument, so both captured it at import. Pinning only the group
    variant would let the PID variant silently revert to an import-time
    binding — a route back to the real OS that no test would notice.
    """
    probed: list[int] = []

    def recording_probe(pid: int) -> bool:
        probed.append(pid)
        return False

    monkeypatch.setattr(process_control, "pid_is_alive", recording_probe)

    stopped = process_control.terminate_process(
        4242,
        grace_seconds=1.0,
        kill_grace_seconds=1.0,
        poll_interval=0.1,
    )

    assert stopped is True
    assert probed == [4242]


# --- Helpers ---


class FakeClock:
    """Deterministic monotonic clock advanced by injected sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current fake monotonic time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance fake time without blocking the test process."""
        self.now += seconds


def platform_state_reader():
    if sys.platform == "linux":
        return process_control._linux_process_state
    return process_control._bsd_process_state
