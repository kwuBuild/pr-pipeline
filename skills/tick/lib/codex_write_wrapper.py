"""Normalize native Codex JSONL for write stages after the child exits.

This transport emits no completion envelope. The controller must still prove
the entire process group dead before reading result.json. Files in a writable
worktree are not independent process-exit evidence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable

from claude_stream_wrapper import ProgressHeartbeat
import fixer_reports


def engine_argv(*, model: str = "", effort: str = "") -> list[str]:
    """The explicit, bounded permission policy shared by dispatch and setup."""
    # This preset already selects workspace-write; the CLI rejects an
    # additional --sandbox flag rather than treating it as redundant.
    argv = ["codex", "exec", "--json", "--color", "never", "--approve-for-me"]
    if model:
        argv += ["--model", model]
    if effort:
        # Callers validate route configuration; also protect this TOML sink
        # when the public launcher or setup is called directly.
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("unsupported Codex effort")
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    return [*argv, "-"]


def collect_stream(stream: Iterable[str], heartbeat: ProgressHeartbeat) -> dict:
    """Require a terminal turn, retain failure, and use only agent final text."""
    final = ""
    error = ""
    completed = False
    malformed = False
    usage = None
    for line in stream:
        heartbeat.beat()
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        kind = event.get("type")
        if kind == "turn.started":
            completed = False
            final = ""
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final = text
                else:
                    malformed = True
        elif kind == "turn.completed":
            completed = True
            usage = event.get("usage")
        elif kind in {"turn.failed", "error"}:
            detail = event.get("error", event)
            message = detail.get("message") if isinstance(detail, dict) else detail
            error = message if isinstance(message, str) and message else "Codex turn failed"
            completed = False
    failed = bool(error or malformed or not completed or not final.strip())
    result = {
        "type": "result", "engine": "codex", "is_error": failed,
        "result": error if error else final,
        "usage": usage, "turn_completed": completed,
        "skill_usage_status": "unavailable", "skills_used": [],
    }
    if error and any(phrase in error.casefold() for phrase in (
        "usage limit", "rate limit", "quota", "too many requests",
    )):
        result["subtype"] = "usage_limit_error"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--heartbeat")
    parser.add_argument("--fix-report-protocol-version", type=int, choices=[fixer_reports.VERSION])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        return 1
    try:
        proc = subprocess.Popen(command, stdin=sys.stdin, stdout=subprocess.PIPE,
                                stderr=sys.stderr, text=True, errors="replace")
    except OSError:
        print("codex-write-wrapper: engine spawn failed", file=sys.stderr)
        return 1
    assert proc.stdout is not None
    result = collect_stream(proc.stdout, ProgressHeartbeat(args.heartbeat))
    status = proc.wait()
    result["engine_exit_status"] = status
    result["is_error"] = result["is_error"] or status != 0
    if args.fix_report_protocol_version is not None:
        result["report_protocol_version"] = args.fix_report_protocol_version
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return status or int(result["is_error"])


if __name__ == "__main__":
    raise SystemExit(main())
