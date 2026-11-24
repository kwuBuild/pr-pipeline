"""Review accounting observations through the shipped tick and events commands."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import pipeline
from test_comment_trust import comment, comment_cli, run_comment_tick  # noqa: F401
from test_review_reports import report  # noqa: F401
from test_verification_cli import offline_tick  # noqa: F401


@pytest.fixture
def event_repo(comment_cli):
    root, state, env = comment_cli
    peer = Path(env["PATH"].split(os.pathsep)[0]) / "gh"
    source = peer.read_text().replace(
        'elif args[:2] == ["pr", "comment"]:',
        'elif args[:2] == ["pr", "comment"]:\n'
        '    if s.get("fail_comment"): raise SystemExit(1)')
    post = 's.setdefault("posted", []).append(pathlib.Path(args[args.index("--body-file") + 1]).read_text())'
    source = source.replace(post, post + '\n    s["history"].append({"id": "posted-" + str(len(s["posted"])), "body": s["posted"][-1], "author": {"login": "operator"}})')
    # Fail before either label mutation, including the fixture's removal shim.
    source = source.replace('elif args[:2] == ["pr", "edit"]:',
                            'elif args[:2] == ["pr", "edit"]:\n'
                            '    if s.get("fail_pr_label"): raise SystemExit(1)')
    peer.write_text(source)
    env["PR_PIPELINE_HOME"] = str(Path(__file__).resolve().parents[3])
    return root, state, env


@pytest.mark.parametrize("version", [None, 2])
@pytest.mark.parametrize("severities, verdict, reserved, cap", [
    ([], "ready_for_human", 0, 5),
    (["minor"], "ready_for_human", 0, 5),
    (["major"], "changes_requested", 0, 5),
    (["blocker", "major", "major", "minor"], "changes_requested", 2, 3),
    ([], "ready_for_human", 2, 3),
])
def test_cli_review_handoff_exports_validated_counts(
    event_repo, report, version, severities, verdict, reserved, cap,
):
    root, state, env = event_repo
    review = make_review(report, version, verdict, severities)
    prepare_review(event_repo, review, version=version, reserved=reserved, cap=cap)
    run_comment_tick(root, env)
    events = read_events(root, env)
    handoff, = [e for e in events if e["event"] == "review_handoff"]
    assert handoff["schema_version"] == 1
    expected_label = (pipeline.LABEL_READY_FOR_HUMAN if verdict == "ready_for_human"
                      else pipeline.LABEL_MAX_ITERATIONS if reserved + 1 >= cap
                      else pipeline.LABEL_CHANGES_REQUESTED)
    assert handoff["details"] == {
        "label": expected_label,
        "outcome": ("promote" if verdict == "ready_for_human" else
                    "max_iterations" if reserved + 1 >= cap else "request_changes"),
        "head_sha": json.loads(state.read_text())["head"],
        "rounds": reserved + 1, "max_iterations": cap,
        "finding_counts": {s: severities.count(s) for s in ("blocker", "major", "minor")},
    }
    terminals = [e for e in events if e["event"] == "terminal"]
    if expected_label == pipeline.LABEL_CHANGES_REQUESTED:
        assert not terminals
    else:
        terminal, = terminals
        assert terminal["details"]["label"] == expected_label
    if expected_label == pipeline.LABEL_MAX_ITERATIONS:
        assert terminal["details"]["rounds"] == reserved + 1
        assert terminal["details"]["max_iterations"] == cap
    assert "explanation" not in json.dumps(events)


@pytest.mark.parametrize("failure", ["fail_pr_label", "fail_comment"])
def test_cli_review_publication_retry_does_not_increment_observed_rounds(event_repo, report, failure):
    root, state, env = event_repo
    scratch = prepare_review(event_repo, report, version=2, reserved=1, cap=2)
    data = json.loads(state.read_text())
    data[failure] = True
    state.write_text(json.dumps(data))
    run_comment_tick(root, env)
    assert (scratch / "dispatch.json").exists()
    assert not [e for e in read_events(root, env) if e["event"] in {"review_handoff", "terminal"}]
    data = json.loads(state.read_text())
    data[failure] = False
    state.write_text(json.dumps(data))
    run_comment_tick(root, env)
    handoff, = [e for e in read_events(root, env) if e["event"] == "review_handoff"]
    assert handoff["details"]["rounds"] == 2
    assert handoff["details"]["max_iterations"] == 2
    assert handoff["details"]["finding_counts"] == {"blocker": 0, "major": 1, "minor": 0}
    assert len(json.loads(state.read_text())["posted"]) == 1
    assert not (scratch / "dispatch.json").exists()


def test_cli_discard_with_unavailable_findings_exports_null(event_repo):
    root, state, env = event_repo
    scratch = prepare_review(event_repo, {"verdict": "ready_for_human"}, version=None, reserved=1, cap=5)
    marker = json.loads((scratch / "dispatch.json").read_text())
    marker["head_sha"] = "a" * 40  # Stale result is discarded before validation escalation.
    (scratch / "dispatch.json").write_text(json.dumps(marker))
    # Keep the re-review queued so the engine shim cannot replace the retained log.
    run_comment_tick(root, env, args=("--only", "pr:99"))
    handoff, = [e for e in read_events(root, env) if e["event"] == "review_handoff"]
    assert handoff["details"]["outcome"] == "discard"
    assert handoff["details"]["rounds"] == 1
    assert handoff["details"]["max_iterations"] == 5
    assert handoff["details"]["finding_counts"] is None


@pytest.mark.parametrize("label", [pipeline.LABEL_NEEDS_HUMAN, pipeline.LABEL_CHANGES_REQUESTED])
@pytest.mark.parametrize("mode", [(), ("pr:12",), ("--only", "pr:12")])
@pytest.mark.parametrize("failure", [False, True])
def test_cli_fixer_admission_and_answered_hold_emit_only_confirmed_caps(event_repo, label, mode, failure):
    root, state, env = event_repo
    configure_cap(env, 2)
    data = json.loads(state.read_text())
    data.update(pr_labels=[label], fail_pr_label=failure, history=[
        {**comment("Please repair this.", "operator"), "id": "new-input"}])
    state.write_text(json.dumps(data))
    receipt = root / ".claude/worktrees/.receipts/pr-12-approved-inputs.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps(["id:old-1", "id:old-2"]))
    run_comment_tick(root, env, args=mode)
    terminal = [e for e in read_events(root, env) if e["event"] == "terminal"]
    if failure:
        assert not terminal
        assert json.loads(state.read_text())["pr_labels"] == [label]
    else:
        event, = terminal
        assert event["stage"] == "fix"
        assert event["details"] == {"label": pipeline.LABEL_MAX_ITERATIONS, "reason": None,
                                    "rounds": 2, "max_iterations": 2}
        assert json.loads(state.read_text())["pr_labels"] == [pipeline.LABEL_MAX_ITERATIONS]
    assert json.loads(receipt.read_text()) == ["id:old-1", "id:old-2"]
    assert not (receipt.parent.parent / "pr-12/dispatch.json").exists()


@pytest.mark.parametrize("review", [None, {}, {"verdict": "ready_for_human"},
                                  {"verdict": "ready_for_human", "findings": [{}]}])
def test_invalid_reviews_never_claim_zero_findings(review):
    result = pipeline.decide_review_harvest(
        review_obj=review, comments=[], pinned_base="b", current_base="b", trusted_author="operator")
    assert result["finding_counts"] is None


# --- Helpers ---


def configure_cap(env, cap):
    cfg = Path(env["PR_PIPELINE_CONFIG"])
    cfg.write_text(cfg.read_text().replace("[defaults]\n", f"[defaults]\nmax_iterations={cap}\n"))


def make_review(report, version, verdict, severities):
    if version is None:
        return {"verdict": verdict, "findings": [
            {"severity": severity, "file": "worker.py:42", "summary": "Finding text"}
            for severity in severities]}
    finding = report["findings"][0]
    return {**report, "verdict": verdict, "findings": [
        {**finding, "id": f"F{i}", "severity": severity}
        for i, severity in enumerate(severities, 1)]}


def prepare_review(event_repo, review, *, version, reserved, cap):
    root, state, env = event_repo
    configure_cap(env, cap)
    data = json.loads(state.read_text())
    data.update(pr_labels=[pipeline.LABEL_REVIEWING], history=[])
    state.write_text(json.dumps(data))
    head = data["head"]
    scratch = root / ".claude/worktrees/pr-12"
    subprocess.run(["git", "worktree", "add", "--detach", str(scratch), head],
                   cwd=root, check=True, capture_output=True)
    receipt = scratch.parent / ".receipts/pr-12-approved-inputs.json"
    receipt.parent.mkdir()
    receipt.write_text(json.dumps([f"id:reserved-{i}" for i in range(reserved)]))
    marker = {"engine": "codex", "stage": "review", "pr_number": 12,
              "deadline": 9999999999, "base_ref": "main", "base_sha": head,
              "base_tip_sha": head, "head_sha": head, "pre_label": pipeline.LABEL_IMPLEMENTED,
              "dispatch_id": "review-counts-12", "pid": 99999999,
              "pgid": 99999999, "start_time": 1.0}
    if version is not None:
        marker["report_protocol_version"] = version
    (scratch / "dispatch.json").write_text(json.dumps(marker))
    (scratch / "review.json").write_text(json.dumps(review))
    completion = {"dispatch_id": marker["dispatch_id"], "exit_status": 0,
                  "result_valid": True, "wrapper_pid": 99999999, "start_time": 1.0}
    if version is not None:
        completion.update(report_protocol_version=version, result_captured=True, validation_reason="")
    (scratch / "completion.json").write_text(json.dumps(completion))
    return scratch


def read_events(root, env):
    launcher = Path(__file__).resolve().parents[1] / "pr-pipeline.sh"
    result = subprocess.run(["bash", str(launcher), "events", "--json", "pr:12"],
                            cwd=root, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]
