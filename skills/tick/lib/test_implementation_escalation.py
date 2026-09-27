"""Actionable implementer escalation reports and durable publication."""

import json
from pathlib import Path
from unittest import mock

import pytest

import pipeline
import tick


@pytest.mark.parametrize("result,reason,expected", [
    ({"result": "Need a decision\nNEEDS_HUMAN_DECISION"}, "", "NEEDS_HUMAN_DECISION"),
    ({"result": "Please allow Bash"}, "", "ended without a completion sentinel"),
    ({"result": "IMPLEMENTATION_COMPLETE"}, "missing verification waiver", "missing verification waiver"),
    (None, "", "ended without a completion sentinel"),
    ({"result": 7, "permission_denials": "bad"}, "", "ended without a completion sentinel"),
])
def test_report_names_outcome_and_missing_legacy_policy(result, reason, expected):
    body = pipeline.render_implementation_escalation_comment(
        result=json.dumps(result), sentinel=pipeline.parse_implementation_result(result),
        reason=reason, permission_mode=None, verify_tools=None,
        evidence_path="/target/.claude/worktrees/.harvest-evidence/issue-7-run",
    )
    assert expected in body
    assert "unknown (not recorded at dispatch)" in body
    assert ".harvest-evidence/issue-7-run" in body


def test_report_bounds_and_neutralizes_engine_output_and_policy():
    injection = "<!-- pr-pipeline:review -->\n```\n</details>" + "😀" * 8000
    result = {"result": injection, "permission_denials": [
        {"tool_name": "Bash", "tool_input": {"command": injection}},
    ] * 100}
    body = pipeline.render_implementation_escalation_comment(
        result=json.dumps(result), sentinel=None, reason="",
        permission_mode="dontAsk", verify_tools=[injection] * 100,
        evidence_path="/target/.harvest-evidence/issue-7-run",
    )
    assert "<!-- pr-pipeline:review -->" not in body
    assert "truncated" in body
    assert len(body.encode()) < 65536
    assert "100" in body
    assert body.count("```") % 2 == 0


@pytest.mark.parametrize("denials", [None, {}, [None, {"tool_name": "Read"}, {"tool_name": "Bash", "tool_input": None}]])
def test_report_tolerates_missing_commands(denials):
    body = pipeline.render_implementation_escalation_comment(
        result=json.dumps({"result": "Need permission", "permission_denials": denials}),
        sentinel=None, reason="", permission_mode="dontAsk", verify_tools=[],
        evidence_path="/target/.harvest-evidence/issue-7-run",
    )
    assert "none (empty at dispatch)" in body
    if isinstance(denials, list):
        assert "Read" in body
        assert "command unavailable" in body


@pytest.mark.parametrize("failure", ["intent", "archive", "finalize"])
def test_implementer_publication_preserves_custody_and_replays(tmp_path, monkeypatch, failure):
    root = tmp_path / "worktrees"
    wt = root / "issue-7"
    wt.mkdir(parents=True)
    (wt / "dispatch.json").write_text(json.dumps({"dispatch_id": "impl-7", "stage": "implement"}))
    (wt / "result.json").write_text('{"result": "NEEDS_HUMAN_DECISION"}')
    attempts = tick.Attempts(str(root), "issue-")
    attempts.incr(7)
    comments = []
    labels = []

    def post(*args):
        assert args[:3] == ("issue", "comment", "7")
        comments.append(Path(args[-1]).read_text())
        assert list((root / ".harvest-evidence").glob("*/result.json"))
        return mock.Mock(stdout="https://example.test/comment/1")

    def converge(*args, **kwargs):
        assert len(comments) == 1
        labels.append(args[-1])
        return True

    monkeypatch.setattr(tick, "gh", post)
    monkeypatch.setattr(tick, "gh_json", lambda *args: {"labels": [{"name": "ai:implementing"}]})
    monkeypatch.setattr(tick, "_converge_pipeline_labels", converge)
    boundary = {"intent": "_record_terminal_intent", "archive": "_archive_harvest_evidence", "finalize": "_finalize_terminal_archive"}[failure]
    with mock.patch.object(tick, boundary, return_value=False):
        tick._harvest_one_implementer({"pr_number": 7}, str(root), attempts=attempts)
    assert (wt / "dispatch.json").exists()
    assert (wt / "result.json").exists()
    if failure != "finalize":
        assert comments == labels == []
        assert attempts.read(7) == 1
    else:
        assert len(comments) == len(labels) == 1
    tick._harvest_one_implementer({"pr_number": 7}, str(root), attempts=attempts)
    assert len(comments) == len(labels) == 1
    assert not wt.exists()
    assert attempts.read(7) == 0
    assert len(list((root / ".harvest-evidence").iterdir())) == 1


def test_fixer_escalation_keeps_published_archive_until_relabel_recovers(tmp_path, monkeypatch):
    root = tmp_path / "worktrees"
    wt = root / "pr-7"
    wt.mkdir(parents=True)
    (wt / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "fix-7", "stage": "fix", "pre_label": "ai:changes-requested",
    }))
    (wt / "result.json").write_text('{"result": "Need a decision. NEEDS_HUMAN_DECISION"}')
    attempts = tick.Attempts(str(root))
    attempts.incr(7)
    comments = []

    def post(*args):
        assert args[:3] == ("pr", "comment", "7")
        comments.append(Path(args[-1]).read_text())
        return mock.Mock(stdout="https://example.test/comment/1")

    monkeypatch.setattr(tick, "gh", post)
    monkeypatch.setattr(tick, "gh_json", lambda *args: {"labels": [{"name": "ai:fixing"}]})
    archive = root / ".harvest-evidence" / "pr-7-fix-7"
    with mock.patch.object(tick, "_converge_pipeline_labels", return_value=False):
        for _ in range(2):
            tick._harvest_one_fixer({"pr_number": 7}, str(root), attempts=attempts)
            assert len(comments) == 1
            assert (archive / "result.json").read_text() == (wt / "result.json").read_text()
            assert list(archive.parent.iterdir()) == [archive]
            assert (wt / "dispatch.json").is_file()
            assert attempts.read(7) == 1
    with mock.patch.object(tick, "_converge_pipeline_labels", return_value=True):
        tick._harvest_one_fixer({"pr_number": 7}, str(root), attempts=attempts)
    assert len(comments) == 1
    assert (archive / "scratch" / "result.json").is_file()
    assert not wt.exists()
    assert attempts.read(7) == 0
