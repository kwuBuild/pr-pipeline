"""Capture Claude stream events and publish one harvest-compatible result."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from typing import Any

# Name of the progress-heartbeat file, written next to the engine's
# ``result.json`` inside its worktree. Its *mtime* is the whole payload: the
# last moment the engine produced output. tick.py imports this name so the
# producer and the stall detector cannot drift onto two different paths.
HEARTBEAT_FILENAME = "heartbeat"

# Floor on the spacing between heartbeat writes. A busy engine emits many
# stream events per second and the reader only compares the mtime against a
# multi-minute threshold, so anything finer is wasted filesystem traffic.
HEARTBEAT_MIN_INTERVAL_SECONDS = 5.0


class ProgressHeartbeat:
    """Bump a file's mtime as engine output events arrive.

    Liveness (``kill -0``) proves a process exists; this proves it is still
    producing. Writes are throttled and every failure is swallowed — the
    heartbeat is advisory diagnostics and must never influence whether the
    engine's result reaches harvest.
    """

    def __init__(
        self,
        path: str | None,
        *,
        min_interval: float = HEARTBEAT_MIN_INTERVAL_SECONDS,
    ) -> None:
        """Bind to a heartbeat path.

        Args:
            path: File to touch, or ``None`` to disable the heartbeat
                entirely (the caller passed no ``--heartbeat``).
            min_interval: Minimum seconds between two writes.
        """
        self._path = path
        self._min_interval = min_interval
        self._last: float | None = None

    def beat(self) -> None:
        """Record forward progress, subject to the throttle interval."""
        if self._path is None:
            return
        now = time.monotonic()
        if self._last is not None and now - self._last < self._min_interval:
            return
        # Recorded before the attempt so a persistently failing path is
        # retried on the throttle interval rather than on every event.
        self._last = now
        try:
            with open(self._path, "ab"):
                pass
            os.utime(self._path, None)
        except OSError:
            pass


def collect_stream(
    stream: Iterable[str],
    heartbeat: ProgressHeartbeat | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Extract the terminal result and Skill tool calls from Claude JSONL.

    Args:
        stream: The engine's JSONL stdout, one event per line.
        heartbeat: Progress heartbeat to bump as lines arrive, or ``None``.

    Returns:
        The terminal ``result`` event (or ``None``) and the skills invoked.
    """
    result: dict[str, Any] | None = None
    skills: list[str] = []
    for line in stream:
        # Bump before parsing: an unparseable line is still proof the engine
        # is emitting, which is exactly what the stall detector asks about.
        if heartbeat is not None:
            heartbeat.beat()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_input = block.get("input")
                    if block.get("name") == "Skill" and isinstance(tool_input, dict):
                        skill = tool_input.get("skill")
                        if isinstance(skill, str) and skill:
                            skills.append(skill)
        if event.get("type") == "result":
            result = event
    return result, skills


def split_wrapper_args(argv: list[str]) -> tuple[str | None, list[str]]:
    """Split the wrapper's own options from the engine command.

    Args:
        argv: The wrapper's argument vector, without ``argv[0]``.

    Returns:
        The ``--heartbeat`` path (``None`` when absent) and the remaining
        engine command, with the ``--`` separator stripped.
    """
    heartbeat_path: str | None = None
    rest = list(argv)
    while rest:
        if rest[0] == "--heartbeat" and len(rest) > 1:
            heartbeat_path = rest[1]
            del rest[:2]
            continue
        break
    if rest and rest[0] == "--":
        rest = rest[1:]
    return heartbeat_path, rest


def main(argv: list[str] | None = None) -> int:
    """Run Claude, collect its trusted stream, and emit one result object."""
    heartbeat_path, command = split_wrapper_args(
        list(sys.argv[1:] if argv is None else argv),
    )
    if not command:
        return 1
    try:
        proc = subprocess.Popen(
            command,
            stdin=sys.stdin,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
        )
    except OSError:
        print("claude-stream-wrapper: engine spawn failed", file=sys.stderr)
        return 1
    assert proc.stdout is not None
    result, skills = collect_stream(proc.stdout, ProgressHeartbeat(heartbeat_path))
    exit_status = proc.wait()
    if result is None:
        return exit_status or 1
    result["skills_used"] = skills
    result["skill_usage_status"] = "active"
    result["claude_exit_status"] = exit_status
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
