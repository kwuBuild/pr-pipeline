"""Terminal-transition failures through reconciliation and the shipped event CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest
import tick


@pytest.fixture
def terminal_watch(tmp_path):
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/example/consumer.git"], check=True)
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ, PR_PIPELINE_HOME=str(root), PR_PIPELINE_PYTHON=sys.executable)
    env.pop("PIPELINE_REPO", None)
    command = ["bash", str(root / "skills/tick/pr-pipeline.sh"), "events"]
    marked = subprocess.run(command + ["--mark"], cwd=repo, env=env,
                            capture_output=True, text=True, check=True, timeout=30)
    cursor = Path(marked.stdout.strip())
    yield repo, env, command, cursor
    cursor.unlink(missing_ok=True)


@pytest.mark.parametrize("config", [
    tick._IMPLEMENTER_CONFIG, tick._REVIEWER_CONFIG, tick._FIXER_CONFIG,
], ids=["implement", "review", "fix"])
@pytest.mark.parametrize("failure", ["command", "malformed"])
def test_unconfirmed_escalation_notifies_before_a_landed_hold(terminal_watch, config, failure):
    """A confirmation failure must be visible before the next tick sees a hold."""
    repo, env, command, cursor = terminal_watch
    wt_root = repo / ".claude/worktrees"
    wt_dir = wt_root / f"{config.worktree_prefix}-42"
    wt_dir.mkdir(parents=True)
    entity = "issue" if config.stage == "implement" else "pr"
    marker = wt_dir / "dispatch.json"
    marker.write_text(json.dumps({
        "engine": "codex" if config.stage == "review" else "claude",
        "stage": config.stage, "pr_number": 42,
        "deadline": 1000.0, "base_ref": "main", "base_sha": "b1",
        "head_sha": "h1", "pre_label": config.pre_label,
        "dispatch_id": f"{config.stage}-42", "pid": 99999,
        "start_time": 500.0, "pgid": 99999,
    }))
    original_marker = marker.read_bytes()
    labels = [config.in_flight_label]
    confirmation_pending = False

    def read_labels(*args):
        nonlocal confirmation_pending
        if confirmation_pending:
            confirmation_pending = False
            if failure == "command":
                raise subprocess.CalledProcessError(1, ["gh", *args])
            return {"labels": None}
        return {"labels": [{"name": label} for label in labels]}

    def edit(*args):
        nonlocal confirmation_pending
        assert args[:3] == (entity, "edit", "42")
        labels[:] = ["ai:needs-human-decision"]
        confirmation_pending = True

    outcomes, deliveries = [], []
    for _ in range(2):
        tick_log = tick.TickLog.open(str(wt_root / ".tick-log"))
        with mock.patch.object(tick, "gh_text", return_value="OPEN"), \
                mock.patch.object(tick, "gh_json", side_effect=read_labels), \
                mock.patch.object(tick, "gh", side_effect=edit), \
                mock.patch.object(tick, "_classify_marker", return_value=("hung", "unknown", True)):
            decision = tick._reconcile_one(42, str(wt_dir), config, tick_log)
            okay = tick._execute_reconcile_actions([decision], config, str(wt_root), tick_log)
        outcomes.append((decision["action"], okay))
        feed = subprocess.run(command + ["--milestones", "--cursor", str(cursor), "--json"],
                              cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert feed.returncode == 0, feed.stderr
        deliveries.append([json.loads(line) for line in feed.stdout.splitlines()])
        assert marker.read_bytes() == original_marker

    assert outcomes == [("escalate", False), ("leave", True)]
    assert labels == ["ai:needs-human-decision"]
    assert [(event["event"], event["entity"], event["number"])
            for event in deliveries[0]] == [("attention", entity, 42)]
    attention = deliveries[0][0]
    assert attention["details"]["source_action"] == "label_convergence_unverified"
    assert "terminal label not confirmed" in attention["message"]
    assert "ai:needs-human-decision" in attention["details"]["reason"]
    assert "terminal label not applied" not in attention["message"]
    assert deliveries[1] == []
