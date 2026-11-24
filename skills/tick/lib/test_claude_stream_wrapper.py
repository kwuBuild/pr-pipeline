"""Tests for trusted Claude stream skill telemetry extraction."""

import io
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from claude_stream_wrapper import ProgressHeartbeat, aggregate_results, collect_stream, main


@pytest.fixture
def reentered_results() -> list[dict]:
    """Two terminal result events from one session re-entered by a task notification.

    Captured from a real ``claude -p --output-format stream-json`` run whose
    turn ended while a background Bash task was pending: the CLI emits one
    ``result`` event per segment, each covering only that segment. Sums were
    cross-checked against the session transcript and cost-state.
    """
    return [
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "STOP",
            "num_turns": 3,
            "duration_ms": 12569,
            "usage": {
                "input_tokens": 19794,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 20096,
                "output_tokens": 577,
            },
            "total_cost_usd": 0.123443,
            "modelUsage": {"claude-opus-5-5": {"inputTokens": 19794, "outputTokens": 577}},
            "uuid": "seg-0",
            "result_index": 0,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "DONE",
            "num_turns": 2,
            "duration_ms": 4745,
            "usage": {
                "input_tokens": 391,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 40832,
                "output_tokens": 103,
            },
            "total_cost_usd": 0.148389,
            "modelUsage": {"claude-opus-5-5": {"inputTokens": 20185, "outputTokens": 680}},
            "origin": {"kind": "task-notification", "producer": "session-task", "runId": "run-1"},
            "uuid": "seg-1",
            "result_index": 1,
        },
    ]


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

    assert result == [events[-1]]
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

    assert result == [{"type": "result", "result": "done"}]
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
    assert result == [events[-1]]
    assert skills == ["pr-pipeline:definition-of-done"]


def test_collect_stream_returns_every_result_event_in_order(
    reentered_results: list[dict],
) -> None:
    """A re-entered session emits one result event per segment; keep them all."""
    results, skills = collect_stream(
        json.dumps(event) for event in reentered_results
    )

    assert [result["uuid"] for result in results] == ["seg-0", "seg-1"]
    assert results[-1]["result"] == "DONE"
    assert skills == []


def test_aggregate_results_sums_usage_turns_and_duration_across_segments(
    reentered_results: list[dict],
) -> None:
    """The published result covers the whole session, not only the last segment."""
    merged = aggregate_results(reentered_results)

    assert merged is not None
    assert merged["usage"] == {
        "input_tokens": 20185,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 60928,
        "output_tokens": 680,
    }
    assert merged["num_turns"] == 5
    assert merged["duration_ms"] == 17314
    assert merged["result_events"] == 2
    # Terminal authority stays with the last segment's event; cost and model
    # usage are already cumulative across the session.
    assert merged["result"] == "DONE"
    assert merged["is_error"] is False
    assert merged["total_cost_usd"] == 0.148389
    assert merged["modelUsage"] == {
        "claude-opus-5-5": {"inputTokens": 20185, "outputTokens": 680}
    }
    assert merged["origin"] == {
        "kind": "task-notification",
        "producer": "session-task",
        "runId": "run-1",
    }


def test_aggregate_results_leaves_a_single_result_event_unchanged(
    reentered_results: list[dict],
) -> None:
    """One segment means one event: the published result is the CLI's own."""
    single = reentered_results[0]
    merged = aggregate_results([single])

    assert merged == single
    assert "result_events" not in merged


def test_aggregate_results_drops_counters_a_segment_cannot_contribute(
    reentered_results: list[dict],
) -> None:
    """An unsummable counter is omitted, so normalization reports partial."""
    reentered_results[1]["usage"]["input_tokens"] = "12"
    merged = aggregate_results(reentered_results)

    assert merged is not None
    assert "input_tokens" not in merged["usage"]
    assert merged["usage"]["output_tokens"] == 680
    assert merged["result_events"] == 2


def test_aggregate_results_reports_segment_count_when_turns_cannot_sum(
    reentered_results: list[dict],
) -> None:
    """The re-entry is still marked when num_turns cannot be aggregated."""
    reentered_results[0]["num_turns"] = None
    merged = aggregate_results(reentered_results)

    assert merged is not None
    assert "num_turns" not in merged
    assert merged["result_events"] == 2


def test_main_publishes_whole_session_usage_for_a_reentered_engine(
    capfd,
    monkeypatch,
    reentered_results: list[dict],
) -> None:
    """The wrapper's one published object holds whole-session usage."""
    script = "print(" + repr("\n".join(json.dumps(e) for e in reentered_results)) + ")"

    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        exit_status = main(["--", sys.executable, "-c", script])

    assert exit_status == 0
    published = json.loads(capfd.readouterr().out)
    assert published["usage"]["input_tokens"] == 20185
    assert published["usage"]["cache_read_input_tokens"] == 60928
    assert published["usage"]["output_tokens"] == 680
    assert published["num_turns"] == 5
    assert published["duration_ms"] == 17314
    assert published["result_events"] == 2
    assert published["result"] == "DONE"
    assert published["total_cost_usd"] == 0.148389


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


@pytest.mark.parametrize("reentered", [False, True])
def test_main_keeps_structured_output_from_a_schema_bound_stream(capfd, monkeypatch, reentered) -> None:
    """``result.json`` carries the result event's ``structured_output`` unchanged.

    The events follow a live ``claude -p --output-format stream-json --verbose
    --json-schema`` run (Claude Code 2.1.331): prose text, a ``StructuredOutput``
    tool call, its tool result, then a ``result`` event whose
    ``structured_output`` holds the schema-valid object. A session re-entered
    by a task notification keeps the terminal event's object.
    """
    report = {"outcome": "A", "note": "Reporting outcome A as requested."}
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I'm reporting outcome A as you asked, with a short note."}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "StructuredOutput", "input": report,
             "caller": {"type": "direct"}}]}},
        {"type": "user", "message": {"content": [
            {"tool_use_id": "toolu_1", "type": "tool_result",
             "content": "Structured output provided successfully"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": json.dumps(report), "structured_output": report, "num_turns": 2},
    ]
    if reentered:
        events.insert(0, {"type": "result", "subtype": "success", "is_error": False,
                          "result": "segment", "num_turns": 1})
    script = "print(" + repr("\n".join(json.dumps(e) for e in events)) + ")"

    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        exit_status = main(["--fix-report-protocol-version", "2", "--", sys.executable, "-c", script])

    assert exit_status == 0
    published = json.loads(capfd.readouterr().out)
    assert published["structured_output"] == report
    assert published["report_protocol_version"] == 2


@pytest.mark.parametrize("engine_tail", [[], ["--", "engine-argument"]])
def test_main_preserves_legacy_heartbeat_arguments_without_separator(tmp_path, capfd, monkeypatch, engine_tail):
    heartbeat = tmp_path / "heartbeat"
    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        status = main(["--heartbeat", str(heartbeat), sys.executable, "-c",
                       "print('{\"type\":\"result\",\"result\":\"done\"}')", *engine_tail])
    assert status == 0
    assert heartbeat.exists()
    assert json.loads(capfd.readouterr().out)["result"] == "done"


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
