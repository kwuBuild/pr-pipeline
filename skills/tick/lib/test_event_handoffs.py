"""Confirmed event records must follow successful controller mutations."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tick


@pytest.mark.parametrize("stage, action", [
    ("implement", "pr_handoff"), ("review", "review_handoff"), ("fix", "revision_handoff"),
])
def test_handoff_confirmation_follows_publication(tmp_path, monkeypatch, stage, action):
    events, order = harvest(tmp_path, monkeypatch, stage)
    confirmations = [e for e in events if e["action"] == action]
    assert len(confirmations) == 1
    event = confirmations[0]
    assert event["dispatch_id"] == "attempt-27"
    assert order[-1] == action
    assert "label" in order
    if stage == "implement":
        assert event["details"] == {"issue_number": 27, "pr_number": 99, "created": True}
    else:
        assert event["details"]["label"] == ("ai:revised" if stage == "fix" else "ai:changes-requested")


@pytest.mark.parametrize("stage, failure", [
    ("implement", "create"), ("implement", "initial_label"), ("implement", "label"),
    ("review", "comment"), ("review", "label"), ("fix", "comment"), ("fix", "label"),
])
def test_failed_handoff_never_emits_success(tmp_path, monkeypatch, stage, failure):
    events, _ = harvest(tmp_path, monkeypatch, stage, failure=failure)
    assert not any(e["action"] in {"pr_handoff", "review_handoff", "revision_handoff"} for e in events)
    entity = "issue" if stage == "implement" else "pr"
    assert (tmp_path / "worktrees" / f"{entity}-27" / "dispatch.json").exists()


def test_resumed_pr_handoff_reports_linkage_without_claiming_new_creation(tmp_path, monkeypatch):
    events, order = harvest(tmp_path, monkeypatch, "implement", existing=True)
    event = next(e for e in events if e["action"] == "pr_handoff")
    assert event["details"] == {"issue_number": 27, "pr_number": 99, "created": False}
    assert "create" not in order


# --- Helpers ---


def harvest(tmp_path: Path, monkeypatch, stage, *, failure=None, existing=False):
    root = tmp_path / "worktrees"
    entity = "issue" if stage == "implement" else "pr"
    scratch = root / f"{entity}-27"
    scratch.mkdir(parents=True)
    (scratch / "dispatch.json").write_text(json.dumps({
        "engine": "codex" if stage == "review" else "claude", "stage": stage,
        "pr_number": 27, "base_ref": "main", "base_sha": "a" * 40,
        "head_sha": "b" * 40, "pre_label": "ai:implemented" if stage == "review" else "ai:available",
        "dispatch_id": "attempt-27",
    }))
    (scratch / "result.json").write_text(json.dumps({"result": "IMPLEMENTATION_COMPLETE"}))
    (scratch / "review.json").write_text(json.dumps({"verdict": "changes_requested", "findings": []}))
    order = []
    log = tick.TickLog.open(str(tmp_path / "logs"))
    original_log = log.log
    def record(**kwargs):
        if kwargs["action"] in {"pr_handoff", "review_handoff", "revision_handoff"}:
            order.append(kwargs["action"])
        original_log(**kwargs)
    log.log = record
    def effect(name):
        order.append(name)
        if failure == name:
            raise subprocess.CalledProcessError(1, ["gh", name])
    def create(*args):
        effect("create")
        return "https://github.com/test-owner/test-repo/pull/99"
    def converge(*args, **kwargs):
        order.append("label")
        return failure != "label"
    def read(*args):
        if "baseRefName,headRefOid" in args:
            return {"baseRefName": "main", "headRefOid": "b" * 40}
        if "headRefOid,body" in args:
            return {"headRefOid": "c" * 40}
        if "labels" in args:
            return {"labels": [{"name": "ai:implemented"}]}
        if "number,isCrossRepository" in args:
            return [{"number": 99, "isCrossRepository": False}] if existing else []
        return []
    monkeypatch.setattr(tick, "gh_json", read)
    monkeypatch.setattr(tick, "gh_text", create)
    monkeypatch.setattr(tick, "gh", lambda *args: effect("comment"))
    monkeypatch.setattr(tick, "git", lambda *args: "a" * 40)
    monkeypatch.setattr(tick, "relabel", lambda *args: effect("initial_label"))
    monkeypatch.setattr(tick, "_converge_pipeline_labels", converge)
    monkeypatch.setattr(tick, "_post_harvest_comment", lambda **kwargs: failure != "comment")
    monkeypatch.setattr(tick.pipeline, "harvest_implementation", lambda **kwargs: {
        "action": "create_pr", "label": "", "sentinel": "IMPLEMENTATION_COMPLETE"})
    monkeypatch.setattr(tick.pipeline, "harvest_review", lambda **kwargs: {
        "action": "request_changes", "label": "ai:changes-requested", "post_comment": True,
        "comment_body": "Changes required", "verdict": "changes_requested"})
    monkeypatch.setattr(tick.pipeline, "harvest_fix", lambda **kwargs: {
        "action": "recheck", "label": "ai:revised", "sentinel": "FIXES_PUSHED_RECHECK_NEEDED",
        "post_comment": True, "comment_body": "Revision ready"})
    runner = {"implement": tick._harvest_one_implementer, "review": tick._harvest_one_reviewer,
              "fix": tick._harvest_one_fixer}[stage]
    runner({"pr_number": 27, "action": "harvest"}, str(root), tick_log=log)
    return log.tail(), order
