"""Operator entry-point tests for the local, read-only event feed."""
from __future__ import annotations

import contextlib
import json
import queue
import threading
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "skills/tick/pr-pipeline.sh"


@pytest.fixture
def consumer(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/example/consumer.git"], check=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "external-calls"
    for name in ("gh", "claude", "codex"):
        shim = tools / name
        shim.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$EVENT_TEST_CALLS"\nexit 99\n')
        shim.chmod(0o755)
    env = dict(os.environ, PR_PIPELINE_HOME=str(ROOT), PR_PIPELINE_PYTHON=sys.executable,
               PATH=f"{tools}{os.pathsep}{os.environ['PATH']}", EVENT_TEST_CALLS=str(calls),
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("PIPELINE_REPO", None)
    return repo, env


def test_events_launcher_reads_consumer_logs_without_mutations_or_external_calls(consumer):
    repo, env = consumer
    append(repo, "tick-001.jsonl", row("launch", stage="implement", number=27,
           engine="claude", details={"model": "sonnet"}))
    before = snapshot(repo)
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == 1
    assert events[0]["schema_version"] == 1
    assert events[0]["repo"] == "example/consumer"
    assert events[0]["event"] == "launch"
    assert events[0]["entity"] == "issue"
    assert events[0]["number"] == 27
    assert events[0]["details"]["model"] == "sonnet"
    assert "retained" in result.stderr.lower()
    assert snapshot(repo) == before
    assert not Path(env["EVENT_TEST_CALLS"]).exists()


@pytest.mark.parametrize("target, expected", [("#27", [27, 27, 99]), ("pr:99", [99])])
def test_events_filters_linked_issue_history_and_orders_retained_records(consumer, target, expected):
    repo, _ = consumer
    append(repo, "tick-002.jsonl", row("launch", stage="review", number=99, ts=3),
           row("launch", stage="review", number=100, ts=4))
    append(repo, "tick-001.jsonl", row("launch", stage="implement", number=27, ts=1),
           row("pr_handoff", stage="implement", number=27, ts=2,
               details={"issue_number": 27, "pr_number": 99, "created": True}))
    result = run_events(consumer, "--json", target)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [e["number"] for e in events] == expected
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    assert "incomplete" in result.stderr.lower() or "unavailable" in result.stderr.lower()


def test_events_text_is_readable_and_does_not_claim_intended_actions_succeeded(consumer):
    repo, _ = consumer
    append(repo, "tick-001.jsonl", row("engine_result_summary", stage="implement", number=27,
        details={"harvest_action": "create_pr", "result_excerpt": "SECRET raw output",
                 "duration_ms": 54000, "usage": {"reported_cost": {"amount": 1.25, "unit": "USD"}}}))
    result = run_events(consumer)
    assert result.returncode == 0, result.stderr
    assert "issue #27" in result.stdout
    assert "result reported" in result.stdout
    assert "1.25 USD" in result.stdout
    assert "54s" in result.stdout
    assert "PR opened" not in result.stdout
    assert "SECRET" not in result.stdout


@pytest.mark.parametrize("action, details, expected", [
    ("pr_handoff", {"issue_number": 27, "pr_number": 99, "created": False}, "pr_handoff"),
    ("review_handoff", {"label": "ai:changes-requested", "outcome": "request_changes"}, "review_handoff"),
    ("revision_handoff", {"label": "ai:revised", "head_sha": "abc"}, "revision_handoff"),
    ("transient_revert", {"failure_class": "quota", "cooldown_until": 3600}, "retry"),
    ("cooldown_skip", {"domain": "claude", "cooldown_until": 3600}, "cooldown"),
    ("engine_stalled", {"progress_age_seconds": 2000}, "stalled"),
    ("notify_terminal_label", {"entity": "pr", "label": "ai:needs-human-decision"}, "terminal"),
    ("loop_quiescent_stop", {}, "stopped"),
    ("loop_only_stop", {}, "stopped"),
    ("loop_target_stop", {}, "stopped"),
    ("kill_failed", {}, "attention"),
    ("escalation_comment_retry", {}, "attention"),
    ("corrupt_marker_escalate", {}, "attention"),
    ("startup_failed", {}, "startup_failure"),
    ("tick_crash", {}, "crash"),
    ("reviewer_skill_observation", {"usage": {"status": "unavailable"}}, "result"),
])
def test_events_covers_operator_events_with_nullable_usage(consumer, action, details, expected):
    repo, _ = consumer
    stage = {"pr_handoff": "implement", "revision_handoff": "fix"}.get(action, "review")
    append(repo, "tick-001.jsonl", row(action, stage=stage, number=27, details=details))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    event = json.loads(result.stdout)
    assert event["event"] == expected
    if expected == "result":
        assert event["details"]["reported_cost"] is None
        assert event["details"]["duration_ms"] is None


@pytest.mark.parametrize("observations", [
    {},
    {"rounds": None, "max_iterations": None, "finding_counts": None},
    {"rounds": True, "max_iterations": -1, "finding_counts": {"blocker": 0}},
    {"rounds": 1.5, "max_iterations": "5", "finding_counts": {"blocker": False, "major": 0, "minor": 0}},
])
def test_events_old_or_invalid_review_observations_remain_unknown(consumer, observations):
    repo, _ = consumer
    append(repo, "tick-001.jsonl",
           row("review_handoff", stage="review", number=27,
               details={"label": "ai:changes-requested", **observations}),
           row("notify_terminal_label", stage="fix", number=27,
               details={"entity": "pr", "label": "ai:max-iterations", **observations}))
    result = run_events(consumer, "--json", "pr:27")
    assert result.returncode == 0, result.stderr
    handoff, terminal = [json.loads(line) for line in result.stdout.splitlines()]
    for event in (handoff, terminal):
        assert event["schema_version"] == 1
        assert event["details"]["rounds"] is None
        assert event["details"]["max_iterations"] is None
    assert handoff["details"]["finding_counts"] is None


@pytest.mark.parametrize("deadline, expected", [(1200, 1200), (None, None), ("soon", None)])
def test_events_stall_reports_time_to_deadline(consumer, deadline, expected):
    repo, _ = consumer
    append(repo, "tick-001.jsonl", row("engine_stalled", stage="review", number=81, details={
        "progress_age_seconds": 900, "seconds_to_deadline": deadline}))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    details = json.loads(result.stdout)["details"]
    assert details == {"progress_age_seconds": 900, "seconds_to_deadline": expected}


def test_events_escalation_decisions_are_not_reported_as_outcomes(consumer):
    # A decision is logged before its action runs; only the label proves it.
    repo, _ = consumer
    for action in ("escalate", "kill_escalate"):
        entry = row(action, stage="fix", number=81)
        entry["reason"] = "hung"
        append(repo, "tick-001.jsonl", entry)
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_events_confirmed_escalation_carries_the_same_ticks_decision_reason(consumer):
    repo, _ = consumer
    decision = row("kill_escalate", stage="review", number=81, ts=1)
    decision["reason"] = "hung"
    terminal = {"entity": "pr", "label": "ai:needs-human-decision"}
    append(repo, "tick-001.jsonl", decision,
           row("notify_terminal_label", stage="review", number=81, ts=2, details=terminal))
    # A later tick's escalation with no decision record gets no stale reason.
    append(repo, "tick-002.jsonl",
           row("notify_terminal_label", stage="review", number=81, ts=3, details=terminal))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(e["event"], e["details"]) for e in events] == [
        ("terminal", {"label": "ai:needs-human-decision", "reason": "hung"}),
        ("terminal", {"label": "ai:needs-human-decision", "reason": None}),
    ]
    assert events[0]["message"] == "terminal label: ai:needs-human-decision; escalated: hung"


def test_events_failed_termination_is_one_attention_event_until_it_escalates(consumer):
    # Termination fails on two ticks, then succeeds on a third.
    repo, _ = consumer
    for tick, ts in (("tick-001.jsonl", 1), ("tick-002.jsonl", 2)):
        decision = row("kill_escalate", stage="review", number=81, ts=ts)
        decision["reason"] = "hung"
        failure = row("kill_failed", stage="review", number=81, ts=ts)
        failure["reason"] = "hung engine: terminate_process returned False"
        append(repo, tick, decision, failure)
    decision = row("kill_escalate", stage="review", number=81, ts=3)
    decision["reason"] = "hung"
    append(repo, "tick-003.jsonl", decision,
           row("notify_terminal_label", stage="review", number=81, ts=3,
               details={"entity": "pr", "label": "ai:needs-human-decision"}))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(e["event"], e["ts"]) for e in events] == [("attention", 1), ("terminal", 3)]
    assert events[0]["details"] == {"reason": "hung engine: terminate_process returned False",
                                    "source_action": "kill_failed"}
    assert events[0]["message"] == (
        "needs attention: engine termination failed: hung engine: terminate_process returned False")
    assert events[1]["details"]["reason"] == "hung"


def test_events_held_escalation_publication_is_one_attention_event(consumer):
    repo, _ = consumer
    for tick, ts in (("tick-001.jsonl", 1), ("tick-002.jsonl", 2)):
        decision = row("escalate", stage="review", number=81, ts=ts, dispatch_id="")
        decision["reason"] = "dispatch setup failed"
        held = row("escalation_comment_retry", stage="review", number=81, ts=ts, dispatch_id="")
        held["reason"] = "holding the pre-dispatch label until the escalation is published"
        append(repo, tick, decision, held)
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [e["event"] for e in events] == ["attention"]
    assert events[0]["message"].startswith("needs attention: escalation not yet published")


def test_events_milestones_report_records_written_after_the_mark(consumer):
    repo, _ = consumer
    append(repo, "tick-001.jsonl", row("launch", stage="implement", number=27))
    cursor = mark(consumer)
    # Written before any watch runs, as on a loop's first tick.
    append(repo, "tick-001.jsonl",
           row("engine_result_summary", stage="implement", number=27, ts=5),
           row("cooldown_skip", stage="implement", number=28, ts=5))
    append(repo, "tick-002.jsonl",
           row("launch", stage="review", number=99, ts=5, dispatch_id="d2"),
           row("engine_stalled", stage="review", number=99, ts=6, dispatch_id="d2",
               details={"progress_age_seconds": 900}),
           row("kill_failed", stage="review", number=98, ts=6),
           row("notify_terminal_label", stage="review", number=99, ts=7,
               details={"entity": "pr", "label": "ai:ready-for-human"}))
    first = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert first.returncode == 0, first.stderr
    assert [(e["event"], e["number"]) for e in map(json.loads, first.stdout.splitlines())] == [
        ("launch", 99), ("stalled", 99), ("attention", 98), ("terminal", 99),
    ]
    # The cursor advanced, including the attention episode already reported.
    append(repo, "tick-003.jsonl", row("kill_failed", stage="review", number=98, ts=8))
    again = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert again.returncode == 0, again.stderr
    assert again.stdout == ""


def test_events_milestones_rearm_reports_records_written_while_unwatched(consumer):
    repo, env = consumer
    cursor = mark(consumer)
    marked = Path(cursor).read_text()
    append(repo, "tick-001.jsonl", row("launch", stage="implement", number=27, ts=2))
    proc = subprocess.Popen(["bash", str(LAUNCHER), "events", "--follow", "--milestones",
                             "--cursor", cursor, "--json"], cwd=repo, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert json.loads(proc.stdout.readline())["event"] == "launch"
        wait_for(lambda: Path(cursor).read_text() != marked)
    finally:
        proc.terminate()  # The watch expires.
        proc.wait(timeout=10)
        proc.stdout.close()
    append(repo, "tick-001.jsonl",
           row("notify_terminal_label", stage="implement", number=27, ts=3,
               details={"entity": "issue", "label": "ai:needs-human-decision"}))
    append(repo, "tick-002.jsonl", row("loop_only_stop", ts=4, dispatch_id=""),
           row("launch", stage="implement", number=30, ts=5, dispatch_id="d3"))
    rearmed = subprocess.run(["bash", str(LAUNCHER), "events", "--follow", "--milestones",
                              "--cursor", cursor, "--json"], cwd=repo, env=env, text=True,
                             capture_output=True, timeout=15)
    assert rearmed.returncode == 0, rearmed.stderr
    assert [e["event"] for e in map(json.loads, rearmed.stdout.splitlines())] == [
        "terminal", "stopped"]


@pytest.mark.parametrize("action", ["tick_crash", "startup_probe_failed"])
def test_events_milestones_end_after_a_loop_ending_failure(consumer, action):
    repo, _ = consumer
    cursor = mark(consumer)
    append(repo, "tick-001.jsonl", row(action, ts=3, dispatch_id=""))
    result = run_events(consumer, "--follow", "--milestones", "--cursor", cursor)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 1


def test_events_milestones_keep_records_after_a_stop_for_the_next_reader(consumer):
    repo, _ = consumer
    cursor = mark(consumer)
    append(repo, "tick-001.jsonl", row("loop_only_stop", ts=2, dispatch_id=""))
    append(repo, "tick-002.jsonl", row("launch", stage="implement", number=30, ts=3, dispatch_id="d3"))
    first = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert first.returncode == 0, first.stderr
    assert [e["event"] for e in map(json.loads, first.stdout.splitlines())] == ["stopped"]
    second = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert second.returncode == 0, second.stderr
    assert [e["event"] for e in map(json.loads, second.stdout.splitlines())] == ["launch"]


def test_events_milestones_detect_a_log_rewritten_after_the_mark(consumer):
    repo, _ = consumer
    path = append(repo, "tick-001.jsonl", row("launch", stage="implement", number=27))
    cursor = mark(consumer)
    path.write_text(json.dumps(row("launch", stage="implement", number=28)) + "\n")
    result = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert result.returncode == 0, result.stderr
    assert [json.loads(line)["number"] for line in result.stdout.splitlines()] == [28]
    assert "replaced or truncated" in result.stderr


def test_events_mark_prints_a_new_cursor_without_touching_the_repo(consumer):
    repo, env = consumer
    append(repo, "tick-001.jsonl", row("launch", stage="implement", number=27))
    before = snapshot(repo)
    first, second = mark(consumer), mark(consumer)
    assert first != second and Path(first).is_file() and Path(second).is_file()
    assert snapshot(repo) == before
    assert not Path(env["EVENT_TEST_CALLS"]).exists()


@pytest.mark.parametrize("args, message", [
    (["--follow", "--milestones"], "--milestones requires --cursor"),
    (["--cursor", "x.json"], "--cursor requires --milestones"),
    (["--mark", "--follow"], "--mark takes no other option"),
    (["--milestones", "--cursor", "missing.json"], "cannot read milestone cursor"),
])
def test_events_milestone_options_fail_closed(consumer, args, message):
    result = run_events(consumer, *args)
    assert result.returncode == 2
    assert message in result.stderr


@pytest.mark.parametrize("content", ["not json", '{"version": 1, "repo": "other/repo", "files": []}'])
def test_events_milestones_reject_an_unusable_cursor(consumer, tmp_path, content):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(content)
    result = run_events(consumer, "--milestones", "--cursor", str(cursor))
    assert result.returncode == 2
    assert "cannot read milestone cursor" in result.stderr


def test_events_milestones_resume_keeps_issue_linkage_delivered_before_the_restart(consumer):
    repo, _ = consumer
    cursor = mark(consumer)
    append(repo, "tick-001.jsonl",
           row("pr_handoff", stage="implement", number=27, ts=2,
               details={"issue_number": 27, "pr_number": 99, "created": True}),
           row("launch", stage="review", number=99, ts=3, dispatch_id="d2"))
    first = run_events(consumer, "--milestones", "--cursor", cursor, "--json", "#27")
    assert first.returncode == 0, first.stderr
    assert [(e["event"], e["number"]) for e in map(json.loads, first.stdout.splitlines())] == [
        ("launch", 99)]
    append(repo, "tick-002.jsonl",
           row("notify_terminal_label", stage="review", number=99, ts=4,
               details={"entity": "pr", "label": "ai:ready-for-human"}))
    resumed = run_events(consumer, "--milestones", "--cursor", cursor, "--json", "#27")
    assert resumed.returncode == 0, resumed.stderr
    assert [(e["event"], e["number"]) for e in map(json.loads, resumed.stdout.splitlines())] == [
        ("terminal", 99)]


def test_events_milestones_resume_keeps_a_pr_event_that_linkage_selects_later(consumer):
    # Late linkage: a PR event the filter could not select yet stays undelivered.
    repo, _ = consumer
    cursor = mark(consumer)
    append(repo, "tick-001.jsonl", row("launch", stage="review", number=99, ts=2, dispatch_id="d2"))
    first = run_events(consumer, "--milestones", "--cursor", cursor, "--json", "#27")
    assert first.returncode == 0, first.stderr
    assert first.stdout == ""
    append(repo, "tick-002.jsonl",
           row("pr_handoff", stage="implement", number=27, ts=3,
               details={"issue_number": 27, "pr_number": 99, "created": False}))
    resumed = run_events(consumer, "--milestones", "--cursor", cursor, "--json", "#27")
    assert resumed.returncode == 0, resumed.stderr
    assert [(e["event"], e["number"]) for e in map(json.loads, resumed.stdout.splitlines())] == [
        ("launch", 99)]


def test_events_milestones_resume_joins_a_model_record_delivered_before_the_restart(consumer):
    repo, _ = consumer
    cursor = mark(consumer)
    append(repo, "tick-001.jsonl", row("model_resolved", stage="review", number=99, engine="codex",
                                       details={"model": "review-model", "effort": "high"}))
    first = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert first.returncode == 0, first.stderr
    assert first.stdout == ""
    append(repo, "tick-001.jsonl", row("launch", stage="review", number=99, engine="codex", ts=2))
    resumed = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert resumed.returncode == 0, resumed.stderr
    assert [json.loads(line)["details"] for line in resumed.stdout.splitlines()] == [
        {"model": "review-model", "effort": "high"}]


def test_events_milestones_resume_keeps_an_escalation_reason_delivered_before_the_restart(consumer):
    repo, _ = consumer
    cursor = mark(consumer)
    decision = row("kill_escalate", stage="review", number=81, ts=2)
    decision["reason"] = "hung"
    append(repo, "tick-001.jsonl", decision)
    first = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert first.returncode == 0, first.stderr
    append(repo, "tick-001.jsonl",
           row("notify_terminal_label", stage="review", number=81, ts=3,
               details={"entity": "pr", "label": "ai:needs-human-decision"}))
    resumed = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert resumed.returncode == 0, resumed.stderr
    assert [json.loads(line)["details"] for line in resumed.stdout.splitlines()] == [
        {"label": "ai:needs-human-decision", "reason": "hung"}]


@pytest.mark.parametrize("cursor_from", ["resumed", "marked after recovery"])
def test_events_attention_reports_a_failure_again_after_the_item_recovered(consumer, cursor_from):
    # Publication fails, a retry lands the label, and a later attempt fails again.
    repo, _ = consumer
    cursor = mark(consumer)
    append(repo, "tick-001.jsonl", *held_escalation(81, ts=2))
    first = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert first.returncode == 0, first.stderr
    assert [e["event"] for e in map(json.loads, first.stdout.splitlines())] == ["attention"]
    append(repo, "tick-002.jsonl", row("notify_terminal_label", stage="review", number=81, ts=3,
                                       dispatch_id="", details={"entity": "pr",
                                                                "label": "ai:needs-human-decision"}))
    second = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert second.returncode == 0, second.stderr
    assert [e["event"] for e in map(json.loads, second.stdout.splitlines())] == ["terminal"]
    if cursor_from == "marked after recovery":
        cursor = mark(consumer)
    append(repo, "tick-003.jsonl", *held_escalation(81, ts=4))
    later = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert later.returncode == 0, later.stderr
    assert [(e["event"], e["ts"]) for e in map(json.loads, later.stdout.splitlines())] == [
        ("attention", 4)]


@pytest.mark.parametrize("ending, details", [
    ("launch", {}),
    ("review_handoff", {"label": "ai:changes-requested"}),
])
def test_events_attention_episode_ends_at_a_new_attempt_or_handoff(consumer, ending, details):
    repo, _ = consumer
    append(repo, "tick-001.jsonl", row("kill_failed", stage="review", number=81, ts=1))
    append(repo, "tick-002.jsonl", row(ending, stage="review", number=81, ts=3, dispatch_id="d2",
                                       details=details))
    append(repo, "tick-003.jsonl", row("kill_failed", stage="review", number=81, ts=4))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    assert [(e["event"], e["ts"]) for e in map(json.loads, result.stdout.splitlines())
            if e["event"] == "attention"] == [("attention", 1), ("attention", 4)]


def test_events_failed_terminal_relabel_is_attention_until_the_label_lands(consumer):
    repo, _ = consumer
    for tick, ts in (("tick-001.jsonl", 1), ("tick-002.jsonl", 2)):
        decision = row("escalate", stage="review", number=81, ts=ts, dispatch_id="")
        decision["reason"] = "dispatch setup failed"
        failed = row("terminal_label_failed", stage="review", number=81, ts=ts, dispatch_id="",
                     details={"entity": "pr", "label": "ai:needs-human-decision"})
        failed["reason"] = "pr #81 could not move to ai:needs-human-decision"
        append(repo, tick, decision, failed)
    decision = row("escalate", stage="review", number=81, ts=3, dispatch_id="")
    decision["reason"] = "dispatch setup failed"
    append(repo, "tick-003.jsonl", decision,
           row("notify_terminal_label", stage="review", number=81, ts=3, dispatch_id="",
               details={"entity": "pr", "label": "ai:needs-human-decision"}))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(e["event"], e["ts"]) for e in events] == [("attention", 1), ("terminal", 3)]
    assert events[0]["message"] == (
        "needs attention: terminal label not applied: pr #81 could not move to ai:needs-human-decision")
    assert events[1]["details"]["reason"] == "dispatch setup failed"


@pytest.mark.parametrize("label", ["ai:needs-human-decision", "ai:ready-for-human", "ai:max-iterations"])
def test_events_unconfirmed_terminal_episode_survives_resume_and_ends_on_recovery(consumer, label):
    repo, _ = consumer
    cursor = mark(consumer)
    unconfirmed = row("label_convergence_unverified", stage="review", number=81,
                      dispatch_id="", details={"target_label": label})
    unconfirmed["reason"] = "mutation succeeded; verification read failed"
    append(repo, "tick-001.jsonl", unconfirmed)
    plain = run_events(consumer, "--json")
    first = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert plain.returncode == first.returncode == 0, (plain.stderr, first.stderr)
    events = [json.loads(line) for line in first.stdout.splitlines()]
    assert [e["event"] for e in events] == ["attention"]
    assert [json.loads(line) for line in plain.stdout.splitlines()] == events
    assert events[0]["details"]["reason"] == f"{label}: {unconfirmed['reason']}"
    assert "terminal label not confirmed" in events[0]["message"]

    append(repo, "tick-002.jsonl", dict(unconfirmed, ts=2))
    repeated = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert repeated.returncode == 0 and repeated.stdout == "", repeated.stderr
    append(repo, "tick-003.jsonl",
           row("notify_terminal_label", stage="review", number=81, ts=3,
               dispatch_id="", details={"entity": "pr", "label": label}),
           dict(unconfirmed, ts=4))
    recovered = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert recovered.returncode == 0, recovered.stderr
    assert [(e["event"], e["ts"]) for e in map(json.loads, recovered.stdout.splitlines())] == [
        ("terminal", 3), ("attention", 4),
    ]


@pytest.mark.parametrize("label", [None, "", "ai:reviewing", "ai:blocked", [], {}])
def test_events_unconfirmed_nonterminal_transition_does_not_become_a_milestone(consumer, label):
    repo, _ = consumer
    cursor = mark(consumer)
    append(repo, "tick-001.jsonl", row("label_convergence_unverified", stage="review", number=81,
                                     details={"target_label": label}))
    result = run_events(consumer, "--milestones", "--cursor", cursor, "--json")
    assert result.returncode == 0 and result.stdout == "", result.stderr


def test_events_coalesces_repeated_cooldowns_but_keeps_changed_deadlines_and_attempts(consumer):
    repo, _ = consumer
    append(repo, "tick-001.jsonl",
        row("cooldown_skip", number=27, stage="implement", details={"cooldown_until": 10}),
        row("launch", number=98, stage="review"),
        row("cooldown_skip", number=27, stage="implement", ts=2, details={"cooldown_until": 10}),
        row("cooldown_skip", number=27, stage="implement", ts=3, details={"cooldown_until": 20}),
        row("launch", number=27, stage="implement", ts=4, dispatch_id="new-attempt"),
        row("cooldown_skip", number=27, stage="implement", ts=5, details={"cooldown_until": 20}))
    result = run_events(consumer, "--json", "#27")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [e["event"] for e in events] == ["cooldown", "cooldown", "launch", "cooldown"]


@pytest.mark.parametrize("action, kind", [
    ("tick_crash", "crash"), ("startup_probe_failed", "startup_failure"),
    ("loop_quiescent_stop", "stopped"),
])
def test_events_preserves_pipeline_occurrences_across_successful_restarts(consumer, action, kind):
    repo, _ = consumer
    first = row(action, ts=1, dispatch_id="")
    first.update(error_type="ValueError", error_message="first failure")
    last = row(action, ts=4, dispatch_id="")
    last.update(error_type="OSError", error_message="later failure")
    append(repo, "tick-001.jsonl", first)
    append(repo, "tick-002.jsonl", row("launch", stage="implement", number=27, ts=2),
           row("tick_complete", ts=3, dispatch_id=""))
    append(repo, "tick-003.jsonl", last)
    result = run_events(consumer, "--json", "#27")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["event"] for event in events] == [kind, "launch", kind]
    assert len({event["id"] for event in events}) == 3


def test_events_follow_delivers_new_crash_after_successful_restart_once(consumer):
    repo, _ = consumer
    first = row("tick_crash", ts=1, dispatch_id="")
    first.update(error_type="ValueError", error_message="first failure")
    append(repo, "tick-001.jsonl", first)
    with following(consumer) as lines:
        initial = json.loads(lines.get(timeout=10))
        assert initial["event"] == "crash"
        append(repo, "tick-002.jsonl", row("launch", stage="implement", number=27, ts=2),
               row("tick_complete", ts=3, dispatch_id=""))
        assert json.loads(lines.get(timeout=10))["event"] == "launch"
        last = row("tick_crash", ts=4, dispatch_id="")
        last.update(error_type="OSError", error_message="later failure")
        append(repo, "tick-003.jsonl", last)
        later = json.loads(lines.get(timeout=10))
        assert later["event"] == "crash"
        assert later["id"] != initial["id"]
        with pytest.raises(queue.Empty):
            lines.get(timeout=1.2)


@pytest.mark.parametrize("entity", [[], {}, None, True, 27, "pipeline", ""])
def test_events_skips_malformed_terminal_entity_between_valid_records(consumer, entity):
    repo, _ = consumer
    append(repo, "tick-001.jsonl", row("launch", stage="review", number=27, ts=1),
           row("notify_terminal_label", stage="review", number=27, ts=2,
               details={"entity": entity, "label": "ai:ready-for-human"}),
           row("launch", stage="review", number=28, ts=3))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(event["event"], event["number"]) for event in events] == [("launch", 27), ("launch", 28)]
    assert "malformed record skipped" in result.stderr


def test_events_rejects_terminal_transition_without_item_identity(consumer):
    repo, _ = consumer
    append(repo, "tick-001.jsonl", row("notify_terminal_label", stage="review",
           details={"entity": "pr", "label": "ai:ready-for-human"}),
           row("launch", stage="review", number=27, ts=2))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    assert [json.loads(line)["event"] for line in result.stdout.splitlines()] == ["launch"]
    assert "malformed record skipped" in result.stderr


def test_events_follow_continues_after_malformed_terminal_records(consumer):
    repo, env = consumer
    append(repo, "tick-001.jsonl", row("launch", stage="review", number=27))
    with following(consumer) as lines:
        assert json.loads(lines.get(timeout=10))["number"] == 27
        append(repo, "tick-001.jsonl",
               row("notify_terminal_label", stage="review", number=27, ts=2,
                   details={"entity": [], "label": "ai:ready-for-human"}),
               row("notify_terminal_label", stage="review", number=27, ts=3,
                   details={"entity": {}, "label": "ai:ready-for-human"}),
               row("launch", stage="review", number=28, ts=4))
        assert json.loads(lines.get(timeout=10))["number"] == 28
        with pytest.raises(queue.Empty):
            lines.get(timeout=1.2)
    assert not Path(env["EVENT_TEST_CALLS"]).exists()


def test_events_warns_about_malformed_and_partial_data_but_reads_valid_records(consumer):
    repo, _ = consumer
    path = append(repo, "tick-001.jsonl", row("launch", number=27, stage="implement"))
    with path.open("ab") as stream:
        stream.write(b'not json\n[]\n{"action": "launch", "ts": false}\n{"incomplete":')
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 1
    assert "malformed" in result.stderr.lower()
    assert "partial" in result.stderr.lower()


def test_events_follow_reads_post_tick_partial_rotation_and_late_linkage(consumer):
    repo, env = consumer
    path = append(repo, "tick-001.jsonl", row("launch", number=27, stage="implement"),
                  row("tick_complete"))
    with following(consumer, "#27") as lines:
        first = json.loads(lines.get(timeout=10))
        assert first["event"] == "launch"
        partial = json.dumps(row("engine_stalled", number=27, stage="implement", ts=2)).encode()
        with path.open("ab") as stream:
            stream.write(partial[:20])
        with pytest.raises(queue.Empty):
            lines.get(timeout=1.2)
        with path.open("ab") as stream:
            stream.write(partial[20:] + b"\n")
        assert json.loads(lines.get(timeout=10))["event"] == "stalled"
        append(repo, "tick-002.jsonl", row("launch", number=99, stage="review", ts=4))
        with pytest.raises(queue.Empty):
            lines.get(timeout=1.2)
        # Linkage can arrive after the PR's first event; retain it for issue filters.
        append(repo, "tick-003.jsonl", row("pr_handoff", number=27, stage="implement", ts=3,
               details={"issue_number": 27, "pr_number": 99, "created": True}))
        events = [json.loads(lines.get(timeout=10)), json.loads(lines.get(timeout=10))]
        assert {e["event"] for e in events} == {"pr_handoff", "launch"}
        path.unlink()
        append(repo, "tick-004.jsonl", row("launch", number=27, stage="implement", ts=5, dispatch_id="d2"))
        assert json.loads(lines.get(timeout=10))["dispatch_id"] == "d2"
        before = snapshot(repo)
        with pytest.raises(queue.Empty):
            lines.get(timeout=1.2)
        assert snapshot(repo) == before
    assert not Path(env["EVENT_TEST_CALLS"]).exists()


def test_events_uses_common_checkout_when_run_from_linked_worktree(consumer, tmp_path):
    repo, env = consumer
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-qm", "baseline"], check=True)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(linked)], check=True, capture_output=True)
    append(repo, "tick-001.jsonl", row("launch", number=27, stage="implement"))
    result = run_events((linked, env), "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["number"] == 27
    assert not (linked / ".claude").exists()


@pytest.mark.parametrize("target", ["27", "#0", "pr:no", "--unexpected"])
def test_events_rejects_invalid_targets(consumer, target):
    result = run_events(consumer, target)
    assert result.returncode == 2


def test_events_preserves_record_order_when_timestamps_share_a_second(consumer):
    repo, _ = consumer
    entries = [row("launch", stage="implement", number=n, dispatch_id=f"d{n}") for n in range(1, 9)]
    for entry in entries:
        entry["reason"] = "ignored verbose detail " * 20
    append(repo, "tick-001.jsonl", *entries)
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    assert [json.loads(line)["number"] for line in result.stdout.splitlines()] == list(range(1, 9))


def test_events_reports_missing_history_without_creating_directories(consumer):
    repo, _ = consumer
    result = run_events(consumer, "--json")
    assert result.returncode == 0
    assert result.stdout == ""
    assert "no retained tick logs" in result.stderr.lower()
    assert not (repo / ".claude").exists()


def test_events_reports_unreadable_log_instead_of_empty_success(consumer):
    repo, _ = consumer
    path = repo / ".claude/worktrees/.tick-log/tick-bad.jsonl"
    path.mkdir(parents=True)  # A portable unreadable-as-file fixture, even under root.
    result = run_events(consumer, "--json")
    assert result.returncode == 1
    assert "unavailable" in result.stderr


def test_events_skips_oversized_and_unsafe_records_without_leaking_payloads(consumer):
    repo, _ = consumer
    path = append(repo, "tick-001.jsonl")
    path.write_bytes(b"x" * (1024 * 1024 + 100) + b"\n")
    append(repo, path.name, row("startup_context", details={"token": "SECRET"}),
           row("engine_result_summary", stage="fix", number=3, details={
               "result_excerpt": "SECRET", "duration_ms": True,
               "usage": {"reported_cost": {"amount": float("nan"), "unit": "USD"}}}),
           row("launch", stage="fix", number=3, details={"model": "model\n\x1b[31m"}))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == 2
    assert events[0]["details"]["reported_cost"] is None
    assert events[0]["details"]["duration_ms"] is None
    assert "SECRET" not in result.stdout
    assert "\n" not in events[1]["details"]["model"]
    assert "\x1b" not in events[1]["details"]["model"]
    assert "oversized" in result.stderr


def test_events_follow_survives_replacement_and_restart_replays_retained_history(consumer):
    repo, _ = consumer
    path = append(repo, "tick-001.jsonl", row("launch", number=27, stage="implement"))
    with following(consumer) as lines:
        assert json.loads(lines.get(timeout=10))["number"] == 27
        replacement = path.with_suffix(".new")
        replacement.write_text(json.dumps(row("launch", number=28, stage="implement", dispatch_id="d2")) + "\n")
        replacement.replace(path)
        second = json.loads(lines.get(timeout=10))
        assert second["number"] == 28
        with pytest.raises(queue.Empty):
            lines.get(timeout=1.2)
    result = run_events(consumer, "--json")
    assert json.loads(result.stdout)["number"] == 28


def test_events_suppresses_replayed_attempt_observations_across_ticks(consumer):
    repo, _ = consumer
    for n in range(1, 3):
        append(repo, f"tick-00{n}.jsonl",
               row("engine_result_summary", stage="implement", number=27, ts=n,
                   details={"harvest_action": "create_pr"}),
               row("pr_handoff", stage="implement", number=27, ts=n,
                   details={"issue_number": 27, "pr_number": 99, "created": n == 1}))
    append(repo, "tick-003.jsonl", row("launch", stage="implement", number=27, ts=3, dispatch_id="d2"))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["event"] for event in events] == ["result", "pr_handoff", "launch"]


def test_events_resolves_reviewer_launch_model_from_matching_metadata(consumer):
    repo, _ = consumer
    append(repo, "tick-001.jsonl", row("model_resolved", number=99, stage="review", engine="codex",
           details={"model": "review-model", "effort": "high"}),
           row("launch", number=99, stage="review", engine="codex", details={"head_sha": "abc", "base_ref": "main"}),
           row("model_resolved", number=100, stage="review", engine="codex", dispatch_id="failed-launch",
               details={"model": "other-model", "effort": "low"}))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == 1
    assert events[0]["details"] == {"model": "review-model", "effort": "high"}


@pytest.mark.parametrize("padding", ["", "longer-replacement" * 50])
def test_events_follow_detects_same_inode_rewrite_at_same_or_larger_size(consumer, padding):
    repo, _ = consumer
    path = append(repo, "tick-001.jsonl", row("launch", number=27, stage="implement"))
    with following(consumer) as lines:
        assert json.loads(lines.get(timeout=10))["number"] == 27
        entry = row("launch", number=28, stage="implement", dispatch_id="d2")
        entry["reason"] = padding
        path.write_text(json.dumps(entry) + "\n")
        assert json.loads(lines.get(timeout=10))["number"] == 28


def test_events_follow_does_not_replay_a_renamed_log(consumer):
    repo, _ = consumer
    path = append(repo, "tick-001.jsonl", row("notify_terminal_label", number=27, stage="review",
                  details={"entity": "pr", "label": "ai:ready-for-human"}),
                  row("notify_terminal_label", number=28, stage="review",
                  details={"entity": "pr", "label": "ai:needs-human-decision"}))
    with following(consumer) as lines:
        assert json.loads(lines.get(timeout=10))["number"] == 27
        assert json.loads(lines.get(timeout=10))["number"] == 28
        path.rename(path.with_name("tick-001-rotated.jsonl"))
        with pytest.raises(queue.Empty):
            lines.get(timeout=1.2)


def test_events_retains_valid_token_counts_and_rejects_unidentified_confirmations(consumer):
    repo, _ = consumer
    append(repo, "tick-001.jsonl", row("reviewer_skill_observation", number=27, stage="review",
           details={"usage": {"status": "complete", "input_tokens": 100, "cached_input_tokens": 80,
                              "output_tokens": 20}}),
           {"ts": 2, "action": "review_handoff"},
           row("revision_handoff", number=27, stage="fix", details={}),
           row("pr_handoff", number=27, stage="review", details={"issue_number": 27, "pr_number": 99, "created": True}))
    result = run_events(consumer, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == 1
    assert events[0]["details"]["input_tokens"] == 100
    assert events[0]["details"]["cached_input_tokens"] == 80
    assert events[0]["details"]["output_tokens"] == 20
    assert "malformed" in result.stderr.lower()


def test_events_follow_rotation_with_new_file_keeps_old_cursor(consumer):
    repo, _ = consumer
    path = append(repo, "tick-001.jsonl", row("notify_terminal_label", number=27, stage="review",
                  details={"entity": "pr", "label": "ai:ready-for-human"}))
    with following(consumer) as lines:
        assert json.loads(lines.get(timeout=10))["number"] == 27
        replacement = row("notify_terminal_label", number=29, stage="review",
                          details={"entity": "pr", "label": "ai:ready-for-human"})
        path.write_text(json.dumps(replacement) + "\n")
        assert json.loads(lines.get(timeout=10))["number"] == 29
        path.rename(path.with_name("tick-002-rotated.jsonl"))
        append(repo, path.name, row("launch", number=28, stage="implement", dispatch_id="d2"))
        assert json.loads(lines.get(timeout=10))["number"] == 28
        with pytest.raises(queue.Empty):
            lines.get(timeout=1.2)


# --- Helpers ---


def row(action, *, stage="", number=None, ts=1, engine="", dispatch_id="d1", details=None):
    return {"ts": ts, "pr": number, "phase": "dispatch", "stage": stage,
            "action": action, "reason": "", "engine": engine,
            "dispatch_id": dispatch_id, "details": details or {}}


def append(repo, name, *entries):
    folder = repo / ".claude/worktrees/.tick-log"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with path.open("a") as stream:
        for entry in entries:
            stream.write(json.dumps(entry) + "\n")
    return path


def snapshot(repo):
    return {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


def run_events(consumer, *args):
    repo, env = consumer
    return subprocess.run(["bash", str(LAUNCHER), "events", *args], cwd=repo,
                          env=env, text=True, capture_output=True, timeout=15)


@contextlib.contextmanager
def following(consumer, *args):
    repo, env = consumer
    proc = subprocess.Popen(["bash", str(LAUNCHER), "events", "--follow", "--json", *args],
                            cwd=repo, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    lines = queue.Queue()
    def collect():
        for line in proc.stdout:
            lines.put(line)
    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    try:
        yield lines
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        worker.join(timeout=2)
        proc.stdout.close()
        proc.stderr.close()


def mark(consumer):
    result = run_events(consumer, "--mark")
    assert result.returncode == 0, result.stderr
    path = result.stdout.strip()
    assert Path(path).is_absolute(), result.stdout
    return path


def held_escalation(number, *, ts):
    decision = row("escalate", stage="review", number=number, ts=ts, dispatch_id="")
    decision["reason"] = "dispatch setup failed"
    held = row("escalation_comment_retry", stage="review", number=number, ts=ts, dispatch_id="")
    held["reason"] = "holding the pre-dispatch label until the escalation is published"
    return decision, held


def wait_for(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.05)
