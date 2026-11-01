"""Capture Claude stream events and publish one harvest-compatible result."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterable
from typing import Any

import fixer_reports

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
    """Extract the terminal result and successful Skill tool calls from Claude JSONL.

    Args:
        stream: The engine's JSONL stdout, one event per line.
        heartbeat: Progress heartbeat to bump as lines arrive, or ``None``.

    Returns:
        The terminal ``result`` event (or ``None``) and calls with matching
        successful results. This does not prove instruction compliance.
    """
    result: dict[str, Any] | None = None
    skills: list[str] = []
    pending: dict[str, str] = {}
    for line in stream:
        # Bump before parsing: an unparseable line is still proof the engine
        # is emitting, which is exactly what the stall detector asks about.
        if heartbeat is not None:
            heartbeat.beat()
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if (event.get("type") == "assistant"
                    and block.get("type") == "tool_use" and block.get("name") == "Skill"):
                args = block.get("input")
                name = args.get("skill") if isinstance(args, dict) else None
                identity = block.get("id")
                if (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", name)
                        and isinstance(identity, str) and len(identity) <= 256 and len(pending) < 64):
                    pending[identity] = name
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                identity = block.get("tool_use_id")
                name = pending.pop(identity, None) if isinstance(identity, str) else None
                if (name and block.get("is_error", False) is False
                        and name not in skills and len(skills) < 64):
                    skills.append(name)
        if event.get("type") == "result":
            result = event
    return result, skills


def main(argv: list[str] | None = None) -> int:
    """Run Claude, collect its trusted stream, and emit one result object."""
    args = list(sys.argv[1:] if argv is None else argv)
    # This is transport metadata, pinned by dispatch, not engine prose. Both
    # Claude routes keep their existing result and process-liveness authority.
    parser = argparse.ArgumentParser()
    parser.add_argument("--heartbeat")
    parser.add_argument("--fix-report-protocol-version", type=int, choices=[fixer_reports.VERSION])
    # Parse only leading wrapper options. A legacy invocation may omit the
    # separator and still pass its own "--" through to the engine command.
    boundary = 0
    while boundary < len(args) and args[boundary] in {"--heartbeat", "--fix-report-protocol-version"}:
        boundary += 2
    options = parser.parse_args(args[:boundary])
    heartbeat_path, command = options.heartbeat, args[boundary:]
    if command and command[0] == "--":
        command = command[1:]
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
    if options.fix_report_protocol_version is not None:
        result["report_protocol_version"] = options.fix_report_protocol_version
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
