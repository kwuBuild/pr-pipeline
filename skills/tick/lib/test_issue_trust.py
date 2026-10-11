"""Operator-only issue admission across dispatch and cadence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import pipeline
import tick

# The stubbed target's own section: a tick refuses a target setup never ran for.
SECTION = '\n[repos."test-owner/test-repo"]\n'


@pytest.mark.parametrize("target", [None, ("issue", 42)])
@pytest.mark.parametrize("author", [None, {}, "operator", {"login": 1}, {"login": ""}, {"login": "stranger"}])
def test_issue_trust_refuses_before_custody_or_body_reads(target, author):
    candidate = {"number": 42, "title": "Untrusted", "author": author, "state": "OPEN"}
    with (
        mock.patch.object(tick, "gh_json", return_value=candidate if target else [candidate]),
        mock.patch.object(tick, "gh_text") as body,
        mock.patch.object(tick, "_resolve_dispatchable_pickup_label") as custody,
        mock.patch.object(tick, "_dispatch_one_implementer") as launch,
        mock.patch.object(tick, "gh") as mutate,
    ):
        tick.dispatch_implementers(target=target, authoritative_in_flight=[])

    body.assert_not_called()
    custody.assert_not_called()
    launch.assert_not_called()
    mutate.assert_not_called()


@pytest.mark.parametrize("target", [None, ("issue", 42)])
def test_issue_trust_dispatches_operator_with_bounded_query(target):
    candidate = issue(42)
    reads = []

    def read(*args):
        reads.append(args)
        if args[:2] == ("issue", "list"):
            if "--author" not in args:
                return []  # The target need not be in the first page.
            return [candidate]
        return candidate

    with (
        mock.patch.object(tick, "gh_json", side_effect=read),
        mock.patch.object(tick, "gh_text", return_value="Build it"),
        mock.patch.object(tick, "_resolve_dispatchable_pickup_label", return_value="ai:available"),
        mock.patch.object(tick, "_existing_marker_blocks_dispatch", return_value=False),
        mock.patch.object(tick, "_provider_cooldown_blocks_dispatch", return_value=False),
        mock.patch.object(tick, "can_dispatch_now", return_value=mock.Mock(can_dispatch=True)),
        mock.patch.object(tick, "_dispatch_one_implementer") as launch,
    ):
        tick.dispatch_implementers(target=target, authoritative_in_flight=[])

    launch.assert_called_once()
    assert launch.call_args.kwargs["issue_number"] == 42
    first = reads[0]
    assert "author" in first[first.index("--json") + 1].split(",")
    if target:
        assert first[:3] == ("issue", "view", "42")
    else:
        assert first[first.index("--author") + 1] == tick.TRUSTED_REVIEW_AUTHOR
        assert first[first.index("--limit") + 1] == str(tick._ACTIONABLE_QUEUE_SCAN_LIMIT)


@pytest.mark.parametrize("state", ["CLOSED", None, 123])
def test_issue_trust_target_does_not_dispatch_closed_or_unreadable_state(state):
    candidate = issue(42) | {"state": state}
    with (
        mock.patch.object(tick, "gh_json", return_value=candidate),
        mock.patch.object(tick, "_resolve_dispatchable_pickup_label") as custody,
    ):
        tick.dispatch_implementers(target=("issue", 42), authoritative_in_flight=[])
    custody.assert_not_called()


@pytest.mark.parametrize(
    "owned,total,expected",
    [
        ([42], [42, 43, 44], "2 available issues not authored by the operator."),
        ([], list(range(1, 201)), "200 or more available issues not authored by the operator."),
        ([1], list(range(1, 201)), "199 or more available issues not authored by the operator."),
        ([1, 201], list(range(1, 201)), "199 or more available issues not authored by the operator."),
        ([42], [42], None),
        ([42], [43], None),  # Inconsistent exhaustive reads: do not guess.
        (list(range(1, 201)), list(range(2, 202)), "0 or more available issues not authored by the operator."),
    ],
)
def test_issue_trust_logs_only_provable_bounded_exclusion_count(owned, total, expected):
    log = mock.Mock()
    with (
        mock.patch.object(tick, "gh_json", side_effect=[
            [issue(n) for n in owned], [{"number": n} for n in total],
        ]) as read,
        mock.patch.object(tick, "_resolve_dispatchable_pickup_label", return_value=None),
    ):
        tick.dispatch_implementers(tick_log=log, authoritative_in_flight=[])

    assert read.call_count == 2
    count_args = read.call_args_list[1].args
    assert count_args[:2] == ("issue", "list")
    assert "--author" not in count_args
    assert count_args[count_args.index("--limit") + 1] == str(tick._ACTIONABLE_QUEUE_SCAN_LIMIT)
    assert count_args[count_args.index("--json") + 1] == "number"
    reasons = [c.kwargs["reason"] for c in log.log.call_args_list
               if c.kwargs.get("action") == "issue_authors_filtered"]
    assert reasons == ([] if expected is None else [expected])


@pytest.mark.parametrize("bad_count", [
    None, {}, [None], [{"number": True}], [{"number": "42"}],
    [{"number": 1}, {"number": 1}],
    ValueError("bad json"), subprocess.CalledProcessError(1, "gh"),
])
def test_issue_trust_count_failure_stays_silent(bad_count):
    log = mock.Mock()
    with mock.patch.object(tick, "gh_json", side_effect=[[], bad_count]):
        tick.dispatch_implementers(tick_log=log, authoritative_in_flight=[])
    log.log.assert_not_called()


@pytest.mark.parametrize("target", [None, ("issue", 42)])
def test_issue_trust_loop_distinguishes_foreign_target_from_quiet_queue(target):
    foreign = [issue(n, "stranger") for n in range(1, tick._ACTIONABLE_QUEUE_SCAN_LIMIT + 1)]
    reads = []

    def read(*args, **kwargs):
        reads.append(args)
        if args[:2] == ("issue", "view"):
            if "closedByPullRequestsReferences" in args[-1]:
                return {"closedByPullRequestsReferences": []}
            return foreign[41]
        if args[:2] == ("issue", "list"):
            if "--author" in args:
                assert args[args.index("--author") + 1] == tick.TRUSTED_REVIEW_AUTHOR
                return []
            return foreign
        return []

    ticks = []

    def run_tick(*args, **kwargs):
        ticks.append(True)
        assert len(ticks) <= 2, "foreign issues kept the loop awake"
        with mock.patch.object(tick, "harvest_reviewers", return_value=True), \
             mock.patch.object(tick, "harvest_implementers", return_value=True), \
             mock.patch.object(tick, "harvest_fixers", return_value=True), \
             mock.patch.object(tick, "dispatch_reviewers"), \
             mock.patch.object(tick, "dispatch_implementers"), \
             mock.patch.object(tick, "dispatch_fixers"):
            return tick._run_tick_body(None, target, "claude", quiescent_stop_after=2)

    with (
        mock.patch.object(tick, "gh_json", side_effect=read),
        mock.patch.object(tick, "read_authoritative_in_flight_labels", return_value=[]),
        mock.patch.object(tick, "_run_single_tick_locked", side_effect=run_tick),
        mock.patch.object(tick, "_start_wake_watcher", return_value=None),
        mock.patch.object(tick, "_wait_next_tick", side_effect=lambda *_: (
            target is not None and len(ticks) == 2, True,
        )) as wait,
        mock.patch.object(tick, "_notify_loop_quiescent_stop") as notify,
    ):
        assert tick._compute_post_dispatch_state(target) == (0, False, ())
        assert tick._run_loop_locked(target, "claude", "", quiescent_stop_after=2) == 0
    assert len(ticks) == 2
    assert wait.call_args.args[0] == 1800
    if target:
        # An explicit foreign target is unfinished even though trust refuses
        # dispatch. Only the simulated operator interruption ends this loop.
        assert wait.call_count == 2
        notify.assert_not_called()
        assert any("labels,state,author" in args for args in reads)
    else:
        notify.assert_called_once()
        assert any("labels,author" in args for args in reads)


@pytest.mark.parametrize("author", [None, {}, "operator", {"login": 1}, {"login": ""}])
@pytest.mark.parametrize("target", [None, ("issue", 42)])
def test_issue_trust_cadence_stays_warm_on_unreadable_author(author, target):
    candidate = issue(42) | {"author": author}

    def read(*args):
        if "closedByPullRequestsReferences" in args[-1]:
            return {"closedByPullRequestsReferences": []}
        return candidate if "view" in args else [candidate]

    with mock.patch.object(tick, "gh_json", side_effect=read):
        queue = tick._compute_post_dispatch_state(target)[1]
        assert queue is (None if target else True)
        decision = pipeline.compute_next_wake(
            in_flight_count=0, has_actionable_queue=queue,
        )
        assert not decision.stop and decision.next_wake_seconds == 270


def test_issue_trust_cadence_keeps_pr_queries_unchanged():
    with mock.patch.object(tick, "gh_json", return_value=[]) as read:
        assert tick._queue_page_has_dispatchable("pr", "ai:implemented") is False
    args = read.call_args.args
    assert "--author" not in args
    assert args[args.index("--json") + 1] == "labels"


def test_issue_trust_tick_binds_reloaded_login_through_following_cadence(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    monkeypatch.setattr(tick, "TRUSTED_REVIEW_AUTHOR", "stale-user")
    queries = []

    def read(*args):
        queries.append(args)
        return []

    def body(*args, **kwargs):
        # A supported edit during the tick applies only on the NEXT tick.
        cfg.write_text('[operator]\nlogin = "next-user"\n' + SECTION)
        tick.dispatch_implementers(authoritative_in_flight=[])
        tick._compute_post_dispatch_state()
        return tick.TickOutcome(0)

    cfg.write_text('[operator]\nlogin = "current-user"\n' + SECTION)
    with (
        mock.patch.object(tick, "_startup_probe"),
        mock.patch.object(tick, "_run_tick_body", side_effect=body),
        mock.patch.object(tick, "gh_json", side_effect=read),
        mock.patch.object(tick, "read_authoritative_in_flight_labels", return_value=[]),
    ):
        for login in ["current-user", "next-user"]:
            queries.clear()
            assert tick._run_single_tick_locked(None, "claude", "").exit_code == 0
            tick._observe_loop_state()
            issue_reads = [args for args in queries if args[:2] == ("issue", "list")]
            assert len(issue_reads) == 3
            assert all("--author" in args and args[args.index("--author") + 1] == login
                       for args in issue_reads)


@pytest.mark.real_gh_helper
@pytest.mark.parametrize("target", [None, ("issue", 42)])
def test_issue_trust_real_ticks_keep_foreign_target_unfinished(target, tmp_path, monkeypatch):
    """Run startup, harvest, dispatch, and loop cadence against an offline gh.

    Only external GitHub and engine peers and elapsed waits are replaced. The
    subprocess shim rejects writes and returns a full page of foreign issues
    unless the caller supplies the author filter.
    """
    shim = tmp_path / "gh"
    calls_path = tmp_path / "calls.jsonl"
    shim.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
args = sys.argv[1:]
with open(os.environ["ISSUE_TRUST_CALLS"], "a") as out:
    out.write(json.dumps(args) + "\\n")
if len(args) > 1 and args[1] in {"edit", "comment", "create"}:
    raise SystemExit("unexpected GitHub mutation")
foreign = {"number": 42, "title": "Work", "state": "OPEN",
           "author": {"login": "stranger"}, "labels": [{"name": "ai:available"}]}
if args[:2] == ["issue", "list"] and "ai:available" in args:
    payload = [] if "--author" in args else [dict(foreign, number=n) for n in range(1, 201)]
elif args[:2] == ["issue", "view"]:
    payload = {"closedByPullRequestsReferences": []} if "closedByPullRequestsReferences" in args else foreign
else:
    payload = []
print("" if "-q" in args else json.dumps(payload))
''')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ISSUE_TRUST_CALLS", str(calls_path))

    wait_count = 0

    def wait(*args):
        nonlocal wait_count
        wait_count += 1
        assert wait_count <= (2 if target else 1)
        assert args[0] == 1800
        return target is not None and wait_count == 2, True

    with (
        mock.patch.object(tick, "_startup_probe", return_value={
            "gh_found": True, "git_found": True, "codex_found": True,
        }),
        mock.patch.object(tick, "verify_codex_bin", return_value="codex"),
        mock.patch.object(tick, "_start_wake_watcher", return_value=None),
        mock.patch.object(tick, "_wait_next_tick", side_effect=wait) as waits,
        mock.patch.object(tick, "_notify_loop_quiescent_stop") as notify,
        mock.patch.object(tick, "_launch_claude_engine") as launch,
    ):
        assert tick._run_loop_locked(target, "claude", "", quiescent_stop_after=2) == 0
    if target:
        assert waits.call_count == 2
        notify.assert_not_called()
    else:
        waits.assert_called_once()
        notify.assert_called_once()
    launch.assert_not_called()
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert all("--repo" in args for args in calls)
    assert not any(args[1] in {"edit", "comment", "create"} for args in calls)
    assert not list(Path(tick.WT_ROOT).glob("issue-*"))
    logs = [json.loads(line) for path in Path(tick.TICK_LOG_DIR).glob("*.jsonl")
            for line in path.read_text().splitlines()]
    if target is not None:
        cadences = [entry["details"]["cadence"] for entry in logs
                    if entry["action"] == "tick_complete"]
        assert len(cadences) == 2
        assert all(not value["stop"] and "not terminal" in value["reason"] for value in cadences)
    else:
        assert sum(entry.get("action") == "issue_authors_filtered" for entry in logs) == 2
        assert all(entry["reason"] == "200 or more available issues not authored by the operator."
                   for entry in logs if entry.get("action") == "issue_authors_filtered")


# --- Helpers ---


def issue(number, login=None):
    return {
        "number": number, "title": "Work", "state": "OPEN",
        "labels": [{"name": "ai:available"}],
        "author": {"login": tick.TRUSTED_REVIEW_AUTHOR if login is None else login},
    }
