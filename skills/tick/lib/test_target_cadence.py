"""Target completion and persisted cadence scope contracts."""

import subprocess
from unittest import mock

import pipeline
import pytest
import tick


@pytest.fixture
def target_reads(monkeypatch):
    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", "example/loop")
    monkeypatch.setattr(tick, "TRUSTED_REVIEW_AUTHOR", "test-owner")
    items = {}
    references = []

    def read(entity, command, number, *args):
        if "closedByPullRequestsReferences" in args:
            return {"closedByPullRequestsReferences": references}
        value = items[(entity, int(number))]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(tick, "gh_json", read)
    return items, references


@pytest.mark.parametrize("label", ["ai:ready-for-human", "ai:needs-human-decision", "ai:max-iterations"])
def test_terminal_target_precedes_unrelated_occupancy(target_reads, label):
    items, _ = target_reads
    items[("pr", 7)] = item(label)
    state = tick._observe_target_state(("pr", 7))
    decision = pipeline.compute_next_wake(
        in_flight_count=2, at_capacity=True, target_state=state,
    )
    assert decision.stop and decision.next_wake_seconds == 0
    assert "pr #7" in decision.reason and label in decision.reason
    assert pipeline.decide_loop_cadence(
        in_flight_count=2, nearest_engine_deadline=None, actionable_queue=False,
        at_cap=True, quiescent=False, consecutive_quiescent=0, target_state=state,
    ) == (0, True)


@pytest.mark.parametrize("entity,state", [("pr", "CLOSED"), ("pr", "MERGED"), ("issue", "CLOSED")])
def test_closed_target_stops_despite_stale_label(target_reads, entity, state):
    items, _ = target_reads
    items[(entity, 7)] = item("ai:available", state=state)
    observed = tick._observe_target_state((entity, 7))
    assert observed.terminal
    assert state in observed.description


def test_direct_loop_cadence_does_not_invent_a_quiet_heartbeat():
    seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=0, nearest_engine_deadline=None, actionable_queue=False,
        at_cap=False, quiescent=True, consecutive_quiescent=0,
        quiescent_stop_after=1,
    )
    assert seconds == pipeline.LOOP_HEARTBEAT_SECONDS
    assert not stop


def test_issue_handoff_follows_terminal_closing_pr(target_reads):
    items, references = target_reads
    items[("issue", 7)] = item()
    items[("pr", 8)] = item("ai:ready-for-human", number=8)
    references.append(reference(8))
    observed = tick._observe_target_state(("issue", 7))
    assert observed.terminal and observed.actionable is False
    assert "issue #7" in observed.description and "pr #8" in observed.description
    assert "ai:ready-for-human" in observed.description


@pytest.mark.parametrize("issue_label,pr_label", [
    ("ai:blocked", "ai:ready-for-human"),
    ("ai:implementing", "ai:ready-for-human"),
    ("ai:available", "ai:ready-for-human"),
    ("ai:needs-human-decision", "ai:implemented"),
    (None, "ai:changes-requested"),
])
def test_issue_does_not_finish_with_an_unfinished_stage(target_reads, issue_label, pr_label):
    items, references = target_reads
    items[("issue", 7)] = item(issue_label)
    items[("pr", 8)] = item(pr_label, number=8)
    references.append(reference(8))
    assert not tick._observe_target_state(("issue", 7)).terminal


@pytest.mark.parametrize("payload", [
    {"state": "OPEN", "labels": []},
    {"state": "OPEN", "labels": [{"name": "ai:blocked"}]},
    {"state": "OPEN", "labels": [
        {"name": "ai:ready-for-human"}, {"name": "ai:changes-requested"},
    ]},
])
def test_nonterminal_target_never_becomes_finished_from_quiet_streak(target_reads, payload):
    items, _ = target_reads
    items[("pr", 7)] = payload
    observed = tick._observe_target_state(("pr", 7))
    decision = pipeline.compute_next_wake(
        in_flight_count=0, is_quiescent=True,
        consecutive_quiescent_ticks=100, target_state=observed,
    )
    assert not decision.stop
    assert "not terminal" in decision.reason and "pr #7" in decision.reason


@pytest.mark.parametrize("payload", [
    None, {}, {"state": "OTHER", "labels": []},
    {"state": "OPEN", "labels": "ai:ready-for-human"},
    subprocess.CalledProcessError(1, ["gh"]),
])
def test_unknown_target_stays_warm(target_reads, payload):
    items, _ = target_reads
    items[("pr", 7)] = payload
    state = tick._observe_target_state(("pr", 7))
    decision = pipeline.compute_next_wake(
        in_flight_count=0, is_quiescent=True,
        consecutive_quiescent_ticks=100, target_state=state,
    )
    assert not decision.stop and decision.next_wake_seconds == pipeline.LOOP_SHORT_POLL_SECONDS
    assert "unknown" in decision.reason


def test_unknown_closing_scope_never_stops(target_reads):
    items, references = target_reads
    items[("issue", 7)] = item("ai:ready-for-human")
    references.append({"number": 8})
    assert tick._observe_target_state(("issue", 7)).actionable is None


def test_alternating_scopes_keep_independent_streaks(tmp_path, monkeypatch):
    monkeypatch.setattr(tick, "_QUIESCENT_COUNTER_FILE", str(tmp_path / "queue.txt"))
    a, b = ("issue", 7), ("pr", 7)
    for expected in range(3):
        assert tick._advance_quiescent_counter(
            is_quiescent=True, count_heartbeat=True, target=a,
        ) == expected
        tick._advance_quiescent_counter(
            is_quiescent=False, count_heartbeat=True, target=b,
        )
        tick._advance_quiescent_counter(is_quiescent=False, count_heartbeat=True)
    assert tick._read_quiescent_counter(a) == 3
    assert tick._read_quiescent_counter(b) == 0
    assert tick._read_quiescent_counter() == 0
    tick._advance_quiescent_counter(is_quiescent=True, count_heartbeat=False, target=a)
    assert tick._read_quiescent_counter(a) == 3


@pytest.mark.parametrize("failure", ["resolution", "reconcile", "authoritative"])
def test_failed_targeted_tick_resets_only_its_counter(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(tick, "_QUIESCENT_COUNTER_FILE", str(tmp_path / "queue.txt"))
    target = ("issue", 7)
    for scope in (None, target, ("issue", 8)):
        tick._write_quiescent_counter(2, scope)
    monkeypatch.setattr(tick, "_resolve_stage_targets", lambda *_: {
        "implementer": target, "reviewer": target, "fixer": target,
    })
    monkeypatch.setattr(tick, "_surface_retained_scratch_dirs", lambda *_: None)
    monkeypatch.setattr(tick, "_scan_stalled_engines_safely", lambda *_: None)
    monkeypatch.setattr(tick, "harvest_reviewers", lambda *_a, **_k: failure != "reconcile")
    monkeypatch.setattr(tick, "harvest_implementers", lambda *_a, **_k: True)
    monkeypatch.setattr(tick, "harvest_fixers", lambda *_a, **_k: True)
    monkeypatch.setattr(tick, "read_authoritative_in_flight_labels", lambda: [])
    if failure == "resolution":
        monkeypatch.setattr(tick, "_resolve_stage_targets", mock.Mock(side_effect=ValueError("bad scope")))
    if failure == "authoritative":
        monkeypatch.setattr(tick, "read_authoritative_in_flight_labels", mock.Mock(
            side_effect=[[], subprocess.CalledProcessError(1, ["gh"])],
        ))
    outcome = tick._run_tick_body(None, target, "claude")
    assert outcome.exit_code in (1, 2)
    assert outcome.cadence is not None and not outcome.cadence.stop
    assert outcome.cadence.next_wake_seconds == pipeline.LOOP_SHORT_POLL_SECONDS
    assert tick._read_quiescent_counter(target) == 0
    assert tick._read_quiescent_counter(None) == tick._read_quiescent_counter(("issue", 8)) == 2


@pytest.mark.parametrize("target", [None, ("pr", 7)])
def test_loop_does_not_claim_terminal_stop_after_failed_tick(monkeypatch, target):
    failed = pipeline.compute_next_wake(in_flight_count=None)
    monkeypatch.setattr(tick, "_run_single_tick_locked", lambda *_a, **_k: tick.TickOutcome(1, failed))
    monkeypatch.setattr(tick, "_observe_loop_state", mock.Mock(side_effect=AssertionError("re-read failed tick")))
    monkeypatch.setattr(tick, "_start_wake_watcher", lambda *_: None)
    wait = mock.Mock(return_value=(True, False))
    monkeypatch.setattr(tick, "_wait_next_tick", wait)
    notify = mock.Mock()
    monkeypatch.setattr(tick, "_notify_loop_target_stop", notify)
    monkeypatch.setattr(tick, "_notify_loop_quiescent_stop", notify)
    assert tick._run_loop_locked(target, "claude", "") == 1
    assert wait.call_args.args[0] == failed.next_wake_seconds
    notify.assert_not_called()


@pytest.mark.parametrize("failure", ["authoritative", "markers"])
def test_loop_unknown_control_plane_cannot_claim_terminal_target(target_reads, monkeypatch, failure):
    items, _ = target_reads
    items[("pr", 7)] = item("ai:ready-for-human")
    monkeypatch.setattr(tick, "read_authoritative_in_flight_labels", lambda: [])
    monkeypatch.setattr(tick, "_scan_marker_identities", lambda: [])
    monkeypatch.setattr(tick, "_nearest_actionable_engine_deadline", lambda: None)
    if failure == "authoritative":
        monkeypatch.setattr(tick, "read_authoritative_in_flight_labels", mock.Mock(
            side_effect=subprocess.CalledProcessError(1, ["gh"]),
        ))
    else:
        monkeypatch.setattr(tick, "_scan_marker_identities", mock.Mock(side_effect=RuntimeError("corrupt")))
    observed = tick._observe_loop_state(("pr", 7))
    seconds, stop = pipeline.decide_loop_cadence(
        **{key: value for key, value in observed.items() if key != "quiescent"},
        quiescent=observed["quiescent"], consecutive_quiescent=0,
    )
    assert not stop and seconds == pipeline.LOOP_SHORT_POLL_SECONDS


def test_one_shot_unknown_marker_state_overrides_terminal_target():
    decision = pipeline.compute_next_wake(
        in_flight_count=None,
        target_state=pipeline.TargetCadenceState(False, True, "target pr #7: ai:ready-for-human"),
    )
    assert not decision.stop
    assert "state unknown" in decision.reason


@pytest.mark.parametrize("other_label", ["ai:reviewing", "ai:blocked"])
def test_terminal_human_hold_keeps_its_existing_label_precedence(target_reads, other_label):
    items, _ = target_reads
    items[("pr", 7)] = {"state": "OPEN", "labels": [
        {"name": "ai:needs-human-decision"}, {"name": other_label},
    ]}
    observed = tick._observe_target_state(("pr", 7))
    assert observed.terminal and observed.actionable is False
    assert "ai:needs-human-decision" in observed.description


@pytest.mark.parametrize("stored", [None, "", "not-an-integer", "-2"])
def test_missing_or_malformed_scope_counter_starts_at_zero(tmp_path, monkeypatch, stored):
    monkeypatch.setattr(tick, "_QUIESCENT_COUNTER_FILE", str(tmp_path / "queue.txt"))
    if stored is not None:
        (tmp_path / "issue-7.txt").write_text(stored)
    tick._write_quiescent_counter(2, ("issue", 8))
    assert tick._advance_quiescent_counter(
        is_quiescent=True, count_heartbeat=True, target=("issue", 7),
    ) == 0
    assert tick._read_quiescent_counter(("issue", 7)) == 1
    assert tick._read_quiescent_counter(("issue", 8)) == 2


def test_scope_resolution_failure_emits_warm_cadence(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tick, "_QUIESCENT_COUNTER_FILE", str(tmp_path / "queue.txt"))
    monkeypatch.setattr(tick, "_resolve_stage_targets", mock.Mock(side_effect=ValueError("bad scope")))
    assert tick._run_tick_body(None, ("issue", 7), "claude").exit_code == 1
    stderr = capsys.readouterr().err
    assert "tick-cadence: next_wake=270 stop=false" in stderr
    assert "state unknown" in stderr


@pytest.mark.parametrize("observed_state", ["CLOSED", "MERGED"])
def test_issue_handoff_revalidates_selected_pr_state(target_reads, monkeypatch, observed_state):
    items, references = target_reads
    items[("issue", 7)] = item()
    items[("pr", 8)] = item("ai:ready-for-human", number=8)
    references.append(reference(8))
    read = tick.gh_json

    def changing_read(entity, command, number, *args):
        payload = read(entity, command, number, *args)
        if entity == "pr" and "labels,state" in args:
            return {**payload, "state": observed_state}
        return payload

    monkeypatch.setattr(tick, "gh_json", changing_read)
    observed = tick._observe_target_state(("issue", 7))
    assert not observed.terminal
    assert observed.actionable is None


# --- Helpers ---


def item(label=None, *, number=7, state="OPEN"):
    return {"number": number, "state": state, "author": {"login": "test-owner"},
            "labels": [] if label is None else [{"name": label}]}


def reference(number):
    return {"number": number, "id": f"PR_{number}",
            "url": f"https://github.com/example/loop/pull/{number}",
            "repository": {"name": "loop", "owner": {"login": "example"}}}
