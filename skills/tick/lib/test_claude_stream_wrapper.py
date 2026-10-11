"""Tests for trusted Claude stream skill telemetry extraction."""

import io
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

from claude_stream_wrapper import ProgressHeartbeat, collect_stream, main


def test_collect_stream_extracts_skill_calls_and_terminal_result() -> None:
    """Only Claude protocol tool-use events contribute skill telemetry."""
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "call-1", "name": "Skill", "input": {"skill": "docs-contract-review"}},
        ]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call-1", "is_error": False, "content": "loaded"}]}},
        {"type": "result", "subtype": "success", "result": "done"},
    ]

    result, skills = collect_stream(io.StringIO("".join(json.dumps(e) + "\n" for e in events)))

    assert result == events[-1]
    assert skills == ["docs-contract-review"]


def test_collect_stream_ignores_malformed_and_non_skill_events() -> None:
    """Malformed lines and other tools cannot forge skill usage."""
    stream = io.StringIO(
        "not-json\n"
        + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"skill": "forged"}},
        ]}}) + "\n"
        + json.dumps({"type": "result", "result": "done"}) + "\n"
    )

    result, skills = collect_stream(stream)

    assert result == {"type": "result", "result": "done"}
    assert skills == []


def test_collect_stream_counts_only_correlated_successful_native_calls():
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": identity, "name": "Skill", "input": {"skill": name}}
        for identity, name in [("ok", "pr-pipeline:definition-of-done"),
                               ("denied", "denied"), ("unfinished", "unfinished"),
                               ("invalid", "invalid")]
    ]}}, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "unmatched", "content": "loaded"},
        {"type": "tool_result", "tool_use_id": "denied", "is_error": True},
        {"type": "tool_result", "tool_use_id": "invalid", "is_error": "false"},
        {"type": "tool_result", "tool_use_id": "ok", "is_error": False},
    ]}}, {"type": "result", "result": "done"}]
    result, skills = collect_stream(json.dumps(event) for event in events)
    assert result == events[-1]
    assert skills == ["pr-pipeline:definition-of-done"]


def test_collect_stream_advances_the_heartbeat_as_events_arrive(
    tmp_path: Path,
) -> None:
    """Progress, not liveness: every engine output event bumps the mtime.

    The stall detector reads only this mtime, so what must be proven is the
    observable artifact advancing — not that a counter incremented.
    """
    heartbeat = tmp_path / "heartbeat"
    lines = [
        json.dumps({"type": "assistant", "message": {"content": []}}) + "\n",
        json.dumps({"type": "result", "result": "done"}) + "\n",
    ]
    observed: list[int] = []

    def observing_stream():
        for line in lines:
            # Separate consecutive beats by more than the filesystem's
            # timestamp resolution so "advanced" is measurable.
            time.sleep(0.01)
            yield line
            observed.append(heartbeat.stat().st_mtime_ns)

    collect_stream(
        observing_stream(),
        heartbeat=ProgressHeartbeat(str(heartbeat), min_interval=0.0),
    )

    assert len(observed) == 2
    assert observed[1] > observed[0]


def test_heartbeat_throttles_bursts_within_its_minimum_interval(
    tmp_path: Path,
) -> None:
    """A chatty engine must not turn every event into a filesystem write."""
    heartbeat = tmp_path / "heartbeat"
    beat = ProgressHeartbeat(str(heartbeat), min_interval=60.0)

    beat.beat()
    first = heartbeat.stat().st_mtime_ns
    time.sleep(0.01)
    beat.beat()

    assert heartbeat.stat().st_mtime_ns == first


def test_main_writes_the_heartbeat_next_to_the_result(
    capfd,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """``--heartbeat PATH`` is consumed by the wrapper, not the engine."""
    heartbeat = tmp_path / "heartbeat"
    events = [{"type": "result", "subtype": "success", "result": "done"}]
    script = "print(" + repr("\n".join(json.dumps(e) for e in events)) + ")"

    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        exit_status = main(
            ["--heartbeat", str(heartbeat), "--", sys.executable, "-c", script],
        )

    assert exit_status == 0
    assert heartbeat.exists()
    assert json.loads(capfd.readouterr().out)["result"] == "done"


def test_main_publishes_the_result_when_the_heartbeat_cannot_be_written(
    capfd,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A heartbeat write failure is advisory noise, never a lost result."""
    unwritable = tmp_path / "heartbeat-dir"
    unwritable.mkdir()
    events = [{"type": "result", "subtype": "success", "result": "done"}]
    script = "print(" + repr("\n".join(json.dumps(e) for e in events)) + ")"

    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        exit_status = main(
            ["--heartbeat", str(unwritable), "--", sys.executable, "-c", script],
        )

    assert exit_status == 0
    assert json.loads(capfd.readouterr().out)["result"] == "done"


def test_main_publishes_one_harvest_compatible_result(capfd, monkeypatch) -> None:
    """The wrapper owns the child stream and emits one augmented JSON object."""
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "call-1", "name": "Skill", "input": {"skill": "qa"}},
        ]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call-1", "is_error": False, "content": "loaded"}]}},
        {"type": "result", "subtype": "success", "result": "done"},
    ]
    script = "print(" + repr("\n".join(json.dumps(e) for e in events)) + ")"

    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        exit_status = main(["--", sys.executable, "-c", script])

    assert exit_status == 0
    published = json.loads(capfd.readouterr().out)
    assert published["result"] == "done"
    assert published["skills_used"] == ["qa"]
    assert published["skill_usage_status"] == "active"
    assert published["claude_exit_status"] == 0


def test_main_preserves_structured_result_on_nonzero_child_exit(
    capfd,
    monkeypatch,
) -> None:
    """Provider failure metadata survives a nonzero Claude process exit."""
    result = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "api_error_status": 429,
        "result": "rate limit",
    }
    script = f"import sys; print({json.dumps(result)!r}); sys.exit(7)"

    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        exit_status = main(["--", sys.executable, "-c", script])

    assert exit_status == 7
    published = json.loads(capfd.readouterr().out)
    assert published["api_error_status"] == 429
    assert published["claude_exit_status"] == 7
    assert published["skill_usage_status"] == "active"


def test_main_emits_nothing_when_child_has_no_terminal_result(
    capfd,
    monkeypatch,
) -> None:
    """A missing result event remains on the bounded invalid-result path."""
    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        exit_status = main(["--", sys.executable, "-c", "print('not-json')"])

    assert exit_status == 1
    assert capfd.readouterr().out == ""


def test_main_reports_spawn_failure_without_raw_exception(capfd) -> None:
    """Child spawn failure is bounded and does not leak exception details."""
    with mock.patch("claude_stream_wrapper.subprocess.Popen", side_effect=OSError):
        exit_status = main(["--", "missing-claude"])

    captured = capfd.readouterr()
    assert exit_status == 1
    assert captured.out == ""
    assert captured.err == "claude-stream-wrapper: engine spawn failed\n"
