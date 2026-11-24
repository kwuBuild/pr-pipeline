"""Bounded process-control operations for PR-pipeline engine rollback."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable

AliveCheck = Callable[[int], bool]
SignalSender = Callable[[int, int], None]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def get_process_start_time(pid: int) -> float | None:
    """Return the epoch-seconds start time of an OS process, or None.

    Uses ``ps -o lstart=`` on macOS/BSD and ``/proc/<pid>/stat`` on Linux.
    Returns ``None`` if the process does not exist or the start time cannot be
    read (permissions, missing /proc, unparseable output).

    Args:
        pid: Positive process identifier.

    Returns:
        Epoch seconds as a float, or ``None``.
    """
    if sys.platform == "linux":
        return _linux_start_time(pid)
    return _bsd_start_time(pid)


def _bsd_start_time(pid: int) -> float | None:
    """Read process start time via ``ps -o lstart=`` (macOS/BSD)."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return _parse_lstart(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return None


def _linux_start_time(pid: int) -> float | None:
    """Read process start time from ``/proc/<pid>/stat`` (Linux)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        # Field 22 (1-indexed) is starttime in clock ticks since boot.
        # Parse past the comm field (may contain spaces/parens).
        close_paren = stat.rfind(")")
        if close_paren < 0:
            return None
        fields = stat[close_paren + 2:].split()
        if len(fields) < 20:
            return None
        starttime_ticks = int(fields[19])  # field 22 minus first 2 fields
        clk_tck = os.sysconf("SC_CLK_TCK")
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    return btime + starttime_ticks / clk_tck
        return None
    except (OSError, ValueError, IndexError):
        return None


def get_process_state(pid: int) -> str | None:
    """Return the OS scheduler state code of a process, or None.

    Uses ``ps -o stat=`` on macOS/BSD and the state field of
    ``/proc/<pid>/stat`` on Linux. A code that starts with ``Z`` (or ``X`` on
    Linux) names a process that has exited but still holds its PID and start
    time because its parent has not reaped it. Returns ``None`` if the process
    does not exist or the state cannot be read; ``None`` is not proof of death.

    Args:
        pid: Positive process identifier.

    Returns:
        The platform's state code, such as ``"S"``, ``"R+"`` or ``"Z"``, or ``None``.
    """
    if sys.platform == "linux":
        return _linux_process_state(pid)
    return _bsd_process_state(pid)


def process_state_is_exited(state: str) -> bool:
    """Return whether a :func:`get_process_state` code names an exited process."""
    return state[:1] in ("Z", "X", "x")


def _bsd_process_state(pid: int) -> str | None:
    """Read the process state via ``ps -o stat=`` (macOS/BSD)."""
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    state = result.stdout.strip()
    if result.returncode != 0 or not state:
        return None
    return state


def _linux_process_state(pid: int) -> str | None:
    """Read the state field (field 3) from ``/proc/<pid>/stat`` (Linux)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    # The comm field may contain spaces and parentheses; the state follows
    # the last closing parenthesis.
    fields = stat[stat.rfind(")") + 1:].split() if ")" in stat else []
    return fields[0] if fields else None


def _parse_lstart(lstart: str) -> float | None:
    """Parse the ``ps -o lstart=`` output into epoch seconds.

    ``ps -o lstart=`` emits **local** time, so the parsed struct must be
    interpreted via ``time.mktime`` (local → epoch), not ``calendar.timegm``
    (UTC → epoch). Using timegm would offset the result by the local timezone
    and break identity verification on any non-UTC machine.
    """
    # macOS: "Wed 24 Jun 01:11:43 2026" (DOW DD MON HH:MM:SS YEAR)
    # Linux/BSD: "Wed Jun 24 01:11:43 2026" (DOW MON DD HH:MM:SS YEAR)
    for fmt in ("%a %d %b %H:%M:%S %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            time_struct = time.strptime(lstart, fmt)
            return float(time.mktime(time_struct))
        except ValueError:
            continue
    return None


def pid_is_alive(pid: int) -> bool:
    """Return whether an OS process currently answers a signal-zero probe.

    Args:
        pid: Positive process identifier.

    Returns:
        ``True`` when the process exists or cannot be probed due to permissions.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pgid_is_alive(pgid: int) -> bool:
    """Return whether any process in a group answers a signal-zero probe.

    Unlike :func:`pid_is_alive` which checks a single process,
    ``os.killpg(pgid, 0)`` succeeds when *any* member of the group is alive.
    This is the correct liveness check after ``os.killpg`` termination —
    confirming only the wrapper PID is dead is insufficient when a child in
    the same group may survive independently.

    Args:
        pgid: Positive process group identifier.

    Returns:
        ``True`` when any process in the group exists or permissions block the probe.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate_process(
    pid: int,
    *,
    grace_seconds: float,
    kill_grace_seconds: float,
    poll_interval: float,
    is_alive: AliveCheck | None = None,
    send_signal: SignalSender = os.kill,
    monotonic: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> bool:
    """Stop a process with bounded TERM then KILL grace periods.

    Args:
        pid: Positive process identifier to stop.
        grace_seconds: Maximum seconds to wait after ``SIGTERM``.
        kill_grace_seconds: Maximum seconds to wait after ``SIGKILL``.
        poll_interval: Maximum delay between liveness probes.
        is_alive: Injectable liveness probe. ``None`` resolves
            :func:`pid_is_alive` *at call time* rather than binding it as a
            default at import, so a monkeypatched module attribute is honoured.
        send_signal: Injectable signal sender.
        monotonic: Injectable monotonic clock.
        sleep: Injectable bounded sleeper.

    Returns:
        ``True`` only when process death is confirmed within a bounded interval.

    Raises:
        ValueError: If the PID or timing arguments are invalid.
    """
    if isinstance(pid, bool) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if grace_seconds < 0 or kill_grace_seconds < 0 or poll_interval <= 0:
        raise ValueError("termination intervals must be non-negative and polling positive")
    if is_alive is None:
        is_alive = pid_is_alive
    if not is_alive(pid):
        return True
    try:
        send_signal(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if _wait_until_stopped(
        pid,
        timeout=grace_seconds,
        poll_interval=poll_interval,
        is_alive=is_alive,
        monotonic=monotonic,
        sleep=sleep,
    ):
        return True
    try:
        send_signal(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return _wait_until_stopped(
        pid,
        timeout=kill_grace_seconds,
        poll_interval=poll_interval,
        is_alive=is_alive,
        monotonic=monotonic,
        sleep=sleep,
    )


GroupSignalSender = Callable[[int, int], None]


def terminate_process_group(
    pgid: int,
    *,
    grace_seconds: float,
    kill_grace_seconds: float,
    poll_interval: float,
    group_is_alive: AliveCheck | None = None,
    send_group_signal: GroupSignalSender = os.killpg,
    monotonic: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> bool:
    """Stop an entire process group with bounded TERM then KILL grace periods.

    Sends signals to the process group (via ``os.killpg``), then polls until
    *every* member of the group is dead — not just the wrapper. A single-PID
    check is insufficient: if the wrapper exits while a child ignores SIGTERM,
    the engine is still running and consuming resources.

    Args:
        pgid: Process group ID to signal.
        grace_seconds: Maximum seconds to wait after SIGTERM.
        kill_grace_seconds: Maximum seconds to wait after SIGKILL.
        poll_interval: Maximum delay between liveness probes.
        group_is_alive: Injectable group liveness probe; receives the PGID and
            returns ``True`` when any member is alive. ``None`` resolves
            :func:`pgid_is_alive` (``os.killpg(pgid, 0)``) *at call time* rather
            than binding it as a default at import, so a monkeypatched module
            attribute is honoured.
        send_group_signal: Injectable group signal sender.
        monotonic: Injectable monotonic clock.
        sleep: Injectable bounded sleeper.

    Returns:
        ``True`` only when the entire group's death is confirmed.
    """
    if isinstance(pgid, bool) or pgid <= 0:
        raise ValueError("pgid must be a positive integer")
    if grace_seconds < 0 or kill_grace_seconds < 0 or poll_interval <= 0:
        raise ValueError("termination intervals must be non-negative and polling positive")
    if group_is_alive is None:
        group_is_alive = pgid_is_alive
    if not group_is_alive(pgid):
        return True
    try:
        send_group_signal(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if _wait_until_stopped(
        pgid,
        timeout=grace_seconds,
        poll_interval=poll_interval,
        is_alive=group_is_alive,
        monotonic=monotonic,
        sleep=sleep,
    ):
        return True
    try:
        send_group_signal(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return _wait_until_stopped(
        pgid,
        timeout=kill_grace_seconds,
        poll_interval=poll_interval,
        is_alive=group_is_alive,
        monotonic=monotonic,
        sleep=sleep,
    )


def _wait_until_stopped(
    pid: int,
    *,
    timeout: float,
    poll_interval: float,
    is_alive: AliveCheck,
    monotonic: Clock,
    sleep: Sleeper,
) -> bool:
    """Poll liveness until death or the monotonic deadline."""
    deadline = monotonic() + timeout
    while is_alive(pid):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(poll_interval, remaining))
    return True


def main(argv: list[str] | None = None) -> int:
    """Run the process-control command-line interface.

    Args:
        argv: Optional argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        Zero when process death is confirmed; one otherwise.
    """
    parser = argparse.ArgumentParser(prog="process-control", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    terminate = subparsers.add_parser("terminate", help="Boundedly stop one process")
    terminate.add_argument("--pid", required=True, type=int)
    terminate.add_argument("--grace", type=float, default=5.0)
    terminate.add_argument("--kill-grace", type=float, default=2.0)
    terminate.add_argument("--poll", type=float, default=0.1)

    term_group = subparsers.add_parser(
        "terminate-group", help="Boundedly stop an entire process group"
    )
    term_group.add_argument("--pgid", required=True, type=int)
    term_group.add_argument("--grace", type=float, default=5.0)
    term_group.add_argument("--kill-grace", type=float, default=2.0)
    term_group.add_argument("--poll", type=float, default=0.1)

    start_time_cmd = subparsers.add_parser(
        "get-start-time", help="Read a process's OS start time in epoch seconds"
    )
    start_time_cmd.add_argument("--pid", required=True, type=int)

    args = parser.parse_args(argv)

    if args.command == "get-start-time":
        st = get_process_start_time(args.pid)
        print(json.dumps({"start_time": st}))
        return 0

    if args.command == "terminate-group":
        stopped = terminate_process_group(
            args.pgid,
            grace_seconds=args.grace,
            kill_grace_seconds=args.kill_grace,
            poll_interval=args.poll,
        )
        print(json.dumps({"stopped": stopped}))
        return 0 if stopped else 1

    stopped = terminate_process(
        args.pid,
        grace_seconds=args.grace,
        kill_grace_seconds=args.kill_grace,
        poll_interval=args.poll,
    )
    print(json.dumps({"stopped": stopped}))
    return 0 if stopped else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
