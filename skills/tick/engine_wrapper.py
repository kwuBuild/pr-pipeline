"""Engine wrapper: atomic launch/completion ownership for PR-pipeline engines.

Replaces bare ``codex exec`` in the dispatch flow. The wrapper provides five
guarantees the bare invocation cannot:

1. **Ownership before spawn:** writes a marker with the wrapper's own process
   identity (PID + start_time + PGID + dispatch_id) *before* the engine starts,
   so a crash at any point leaves either no paid engine or a fully owned one.

2. **Signal forwarding:** installs a SIGTERM handler that terminates the engine
   subprocess before the wrapper itself exits, preventing orphaned engines that
   continue spending after a kill.

3. **Atomic result placement:** the engine writes to a temp file; the wrapper
   validates exit status + schema, then atomically retains the original bytes.
   Protocol 2 keeps rejected reports at the result path for human-decision
   harvest; legacy/failed-engine rejection uses ``review-rejected.json``.
   An in-progress file is never visible as a captured final result.

4. **Sanitized observability:** when requested, the wrapper drains Codex JSONL
   stdout and persists bounded skill-file evidence and terminal token usage. Raw events,
   prompts, commands, command output, and absolute paths are discarded.

5. **Completion envelope:** after the atomic rename, the wrapper writes a
   dispatch-id- and protocol-bound ``completion.json`` that HARVEST consumes as the gate for
   ``classify_dispatch(envelope_valid=True)``. Without the envelope, even a
   present result file is not harvested.

Stdlib only: this script runs alongside ``pipeline.py`` without pulling a target repo's
dependency tree.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

# Import helpers from the co-located lib/ directory.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import pipeline  # noqa: E402
import process_control  # noqa: E402
import reviewer_observability  # noqa: E402


def run_engine(
    *,
    dispatch_id: str,
    marker_path: str,
    result_path: str,
    envelope_path: str,
    observation_path: str | None = None,
    heartbeat_path: str | None = None,
    engine_cmd: list[str],
    marker_template: dict[str, object],
) -> int:
    """Run an engine with atomic ownership and completion envelope.

    Args:
        dispatch_id: Unique identifier linking marker, result, and envelope.
        marker_path: Path to dispatch.json (updated with process identity).
        result_path: Final path for the engine's result file.
        envelope_path: Path for the completion envelope.
        observation_path: Optional path for a sanitized Codex JSONL summary.
        heartbeat_path: Optional mtime-only output signal beside the result.
        engine_cmd: The engine command to execute (e.g. codex exec ...).
        marker_template: Marker fields to merge with process identity.

    Returns:
        The engine's exit code.
    """
    wrapper_pid = os.getpid()
    wrapper_start_time = time.time()
    temp_result: str | None = None
    try:
        # 1. Write ownership record with process identity BEFORE spawning the engine.
        # Lead a dedicated process group so the wrapper and its engine child can be
        # terminated together via os.killpg. The wrapper's PGID then equals its own
        # PID, and the child inherits this group. Dispatch already starts the wrapper
        # with start_new_session=True, which makes it a group leader; Linux rejects
        # setpgrp from a session leader with EPERM, so call it only when needed.
        os_start = process_control.get_process_start_time(wrapper_pid)
        if os_start is not None:
            wrapper_start_time = os_start
        if os.getpgrp() != wrapper_pid:
            os.setpgrp()
        marker = dict(marker_template)
        marker["dispatch_id"] = dispatch_id
        marker["pid"] = wrapper_pid
        marker["start_time"] = wrapper_start_time
        marker["pgid"] = os.getpgrp()
        # Remove `state` from any lease template — this is the published marker now.
        marker.pop("state", None)
        pipeline.atomic_write(marker_path, json.dumps(marker))

        # 2. Run the engine with output to a temp file in the result's directory.
        result_dir = os.path.dirname(os.path.abspath(result_path))
        fd, temp_result = tempfile.mkstemp(
            dir=result_dir,
            prefix=".tmp-result-",
            suffix=".json",
        )
        os.close(fd)

        # The engine writes to -o <temp_result>, so patch the command's -o flag.
        cmd = _patch_output_flag(engine_cmd, temp_result)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=(
                subprocess.PIPE
                if observation_path is not None or heartbeat_path is not None
                else subprocess.DEVNULL
            ),
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        if temp_result is not None:
            try:
                os.unlink(temp_result)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        _write_failure_envelope(
            envelope_path=envelope_path,
            dispatch_id=dispatch_id,
            wrapper_pid=wrapper_pid,
            start_time=wrapper_start_time,
            error=exc,
            report_protocol_version=marker_template.get("report_protocol_version"),
        )
        if observation_path is not None:
            _write_observation(
                observation_path,
                reviewer_observability.unavailable_observation(),
            )
        print(
            f"engine-wrapper startup failure: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    prev_handler = signal.getsignal(signal.SIGTERM)

    def _forward_sigterm(signum: int, _frame: object) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGTERM, _forward_sigterm)
    try:
        if observation_path is not None or heartbeat_path is not None:
            observation = _observe_engine_stdout(proc, heartbeat_path=heartbeat_path)
            if observation_path is not None:
                _write_observation(observation_path, observation)
        proc.wait()
        exit_status = proc.returncode
    except OSError:
        exit_status = -1
    finally:
        signal.signal(signal.SIGTERM, prev_handler if callable(prev_handler) else signal.SIG_DFL)

    # 3. Validate and place result atomically.
    result_valid = False
    report_version = marker_template.get("report_protocol_version")
    validation_reason = "Engine did not produce a readable JSON report."
    if exit_status == 0 and os.path.isfile(temp_result):
        try:
            with open(temp_result) as f:
                result_obj = (pipeline.review_reports.loads(f.read()) if report_version is not None
                              else json.load(f))
            if pipeline.validate_review(result_obj, report_protocol_version=report_version) is not None:
                result_valid = True
                validation_reason = ""
            elif report_version == pipeline.review_reports.VERSION:
                validation_reason = pipeline.review_reports.validation_problem(result_obj)
            else:
                validation_reason = "Legacy report failed schema validation."
        except (OSError, UnicodeError, ValueError):
            pass

    # Preserve rejected bytes before publishing any completion. For protocol 2,
    # captured invalid output enters harvest as a protocol failure, never a new
    # paid review/fix attempt. Legacy failures retain their existing gates.
    captured = False
    if os.path.isfile(temp_result):
        destination = (result_path if exit_status == 0 and (result_valid or report_version is not None)
                       else os.path.join(result_dir, "review-rejected.json"))
        os.replace(temp_result, destination)
        captured = destination == result_path

    # Clean up temp file if it wasn't renamed.
    if os.path.isfile(temp_result):
        try:
            os.unlink(temp_result)
        except OSError:
            pass

    # 4. Write completion envelope (the harvest gate).
    envelope: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "exit_status": exit_status,
        "wrapper_pid": wrapper_pid,
        "start_time": wrapper_start_time,
        "result_valid": result_valid,
    }
    if report_version is not None:
        envelope.update(report_protocol_version=report_version, result_captured=captured,
                        validation_reason=validation_reason)
    if exit_status != 0:
        envelope["error_class"] = "EngineExitError"
    pipeline.atomic_write(envelope_path, json.dumps(envelope))
    if exit_status != 0:
        print(
            "engine-wrapper engine failure: EngineExitError",
            file=sys.stderr,
        )

    return exit_status


def _observe_engine_stdout(
    proc: subprocess.Popen[bytes],
    *,
    heartbeat_path: str | None = None,
) -> dict[str, object]:
    """Drain child stdout and return sanitized evidence without affecting custody."""
    if proc.stdout is None:
        return reviewer_observability.unavailable_observation()
    try:
        return reviewer_observability.observe_event_stream(
            proc.stdout, heartbeat_path=heartbeat_path,
            skill_home=Path(os.environ["HOME"]) if os.environ.get("HOME") else None,
        )
    except Exception:
        # Observability is deliberately non-authoritative. If parsing fails in
        # an unexpected way, continue draining so the child cannot deadlock on
        # a full pipe, then report only a fixed unavailable payload.
        try:
            while proc.stdout.read(64 * 1024):
                pass
        except OSError:
            pass
        return reviewer_observability.unavailable_observation()
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass


def _write_observation(path: str, observation: dict[str, object]) -> None:
    """Best-effort atomic persistence for non-authoritative observations."""
    try:
        pipeline.atomic_write(path, json.dumps(observation))
    except (OSError, TypeError, ValueError):
        pass


def _write_failure_envelope(
    *,
    envelope_path: str,
    dispatch_id: str,
    wrapper_pid: int,
    start_time: float,
    error: Exception,
    report_protocol_version: int | None = None,
) -> None:
    """Atomically persist failure evidence for a wrapper startup exception."""
    envelope: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "exit_status": 1,
        "wrapper_pid": wrapper_pid,
        "start_time": start_time,
        "result_valid": False,
        "error_class": type(error).__name__,
    }
    if report_protocol_version is not None:
        envelope.update(report_protocol_version=report_protocol_version, result_captured=False,
                        validation_reason="Wrapper failed before report capture.")
    pipeline.atomic_write(envelope_path, json.dumps(envelope))


def _patch_output_flag(cmd: list[str], temp_path: str) -> list[str]:
    """Replace the ``-o`` / ``--output-last-message`` value in the command."""
    result = list(cmd)
    for i, arg in enumerate(result):
        if arg in ("-o", "--output-last-message") and i + 1 < len(result):
            result[i + 1] = temp_path
            return result
    # If no -o flag found, append it.
    result.extend(["-o", temp_path])
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the engine wrapper.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        The engine's exit code.
    """
    parser = argparse.ArgumentParser(
        prog="engine-wrapper",
        description=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run an engine with atomic ownership")
    p_run.add_argument("--dispatch-id", required=True, help="Unique dispatch identifier")
    p_run.add_argument("--marker-path", required=True, help="Path to dispatch.json")
    p_run.add_argument("--result-path", required=True, help="Final result file path")
    p_run.add_argument("--envelope-path", required=True, help="Path for completion.json")
    p_run.add_argument(
        "--observation-path",
        default=None,
        help="Path for a sanitized reviewer skill observation",
    )
    p_run.add_argument("--heartbeat", default=None, help="Path for an mtime-only progress heartbeat")
    p_run.add_argument("--stall-seconds", type=int, default=pipeline.ENGINE_STALL_SECONDS)
    p_run.add_argument("--stall-startup-grace-seconds", type=int,
                       default=pipeline.ENGINE_STALL_STARTUP_GRACE_SECONDS)
    p_run.add_argument("--engine", required=True, help="Engine name (e.g. codex)")
    p_run.add_argument("--stage", required=True, help="Pipeline stage (e.g. review)")
    p_run.add_argument("--pr-number", type=int, required=True, help="PR number")
    p_run.add_argument("--deadline", type=float, required=True, help="Deadline epoch seconds")
    p_run.add_argument("--base-ref", required=True, help="Base ref name")
    p_run.add_argument("--base-sha", required=True, help="Pinned base SHA (merge-base for diff)")
    p_run.add_argument(
        "--base-tip-sha", default="",
        help="Base branch tip at dispatch (for drift detection)",
    )
    p_run.add_argument("--head-sha", required=True, help="Pinned head SHA")
    p_run.add_argument("--fix-round", default="", help="Pinned no-change fixer round")
    p_run.add_argument("--report-protocol-version", type=int, choices=[pipeline.review_reports.VERSION])
    p_run.add_argument("--pre-label", required=True, help="Pre-dispatch label")
    p_run.add_argument("engine_cmd", nargs=argparse.REMAINDER, help="Engine command after --")

    args = parser.parse_args(argv)

    # Strip leading '--' separator from engine_cmd.
    engine_cmd = args.engine_cmd
    if engine_cmd and engine_cmd[0] == "--":
        engine_cmd = engine_cmd[1:]
    if not engine_cmd:
        print("engine command required after --", file=sys.stderr)
        return 1

    marker_template: dict[str, object] = {
        "engine": args.engine,
        "stage": args.stage,
        "pr_number": args.pr_number,
        "deadline": args.deadline,
        "base_ref": args.base_ref,
        "base_sha": args.base_sha,
        "head_sha": args.head_sha,
        "pre_label": args.pre_label,
        "stall_seconds": args.stall_seconds,
        "stall_startup_grace_seconds": args.stall_startup_grace_seconds,
    }
    if args.base_tip_sha:
        marker_template["base_tip_sha"] = args.base_tip_sha
    if args.fix_round:
        marker_template["fix_round"] = args.fix_round
    if args.report_protocol_version is not None:
        marker_template["report_protocol_version"] = args.report_protocol_version

    return run_engine(
        dispatch_id=args.dispatch_id,
        marker_path=args.marker_path,
        result_path=args.result_path,
        envelope_path=args.envelope_path,
        observation_path=args.observation_path,
        heartbeat_path=args.heartbeat,
        engine_cmd=engine_cmd,
        marker_template=marker_template,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
