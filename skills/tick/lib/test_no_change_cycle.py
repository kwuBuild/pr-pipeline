"""No-push fixer/reviewer cycles through the real harvest effect paths."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import pipeline
import tick


@pytest.mark.parametrize("lose_receipt", [False, True])
def test_unchanged_head_cycle_caps_and_replayed_harvest_counts_once(
    tmp_path: Path, lose_receipt: bool,
) -> None:
    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir(parents=True)
    comments: list[dict[str, str]] = []
    transitions: list[str] = []
    allow_relabel = False

    def read(*args: str) -> object:
        if "comments" in args:
            return list(comments)
        return {"headRefOid": "a" * 40, "baseRefName": "main", "body": ""}

    def post(*args: str) -> subprocess.CompletedProcess[str]:
        assert args[:3] == ("pr", "comment", "42")
        comments.append({"body": Path(args[args.index("--body-file") + 1]).read_text()})
        return subprocess.CompletedProcess(args, 0, stdout="https://example.test/comment")

    def converge(_entity: str, _number: int, label: str, **_kwargs: object) -> bool:
        transitions.append(label)
        return allow_relabel

    decision = {"pr_number": 42, "action": "harvest", "kill": False}
    fix_round = ""
    with (
        mock.patch("tick.gh_json", side_effect=read),
        mock.patch("tick.gh", side_effect=post),
        mock.patch("tick.git", return_value="b" * 40),
        mock.patch("tick._converge_pipeline_labels", side_effect=converge),
        mock.patch("tick.cleanup_scratch"),
    ):
        for round_index in range(pipeline.MAX_ITERATIONS):
            write_marker(wt_dir, stage="review", dispatch=f"review-{round_index}", fix_round=fix_round)
            (wt_dir / "review.json").write_text(json.dumps({
                "verdict": "changes_requested", "findings": [
                    {"severity": "major", "file": "guard.py", "summary": "Guard missing"},
                ],
            }))
            allow_relabel = False
            tick._harvest_one_reviewer(decision, str(wt_root), None, None)
            assert pipeline.count_review_rounds(comments) == round_index + 1
            allow_relabel = True
            tick._harvest_one_reviewer(decision, str(wt_root), None, None)
            assert pipeline.count_review_rounds(comments) == round_index + 1
            if round_index == pipeline.MAX_ITERATIONS - 1:
                assert transitions[-1] == pipeline.LABEL_MAX_ITERATIONS
                break
            assert transitions[-1] == pipeline.LABEL_CHANGES_REQUESTED

            original_review = comments[-1]["body"]
            write_marker(wt_dir, stage="fix", dispatch=f"fix-{round_index}",
                         review_sha256=hashlib.sha256(original_review.encode()).hexdigest())
            (wt_dir / "result.json").write_text(json.dumps({
                "result": "Finding: Guard missing\nDecision: no-change\n"
                          "Reason: The current caller already guards this input.\n"
                          "NO_CHANGES_RECHECK_NEEDED",
            }))
            allow_relabel = False
            tick._harvest_one_fixer(decision, str(wt_root), None, None)
            assert transitions[-1] == pipeline.LABEL_REVISED
            published = comments[-1]["body"]
            if lose_receipt:
                Path(tick._comment_receipt_path(
                    str(wt_root), "pr", 42, tick._FIX_COMMENT_KIND,
                )).unlink()
            count_before = len(comments)
            allow_relabel = True
            tick._harvest_one_fixer(decision, str(wt_root), None, None)
            assert transitions[-1] == pipeline.LABEL_REVISED
            assert len(comments) == count_before + int(lose_receipt)
            assert comments[-1]["body"] == published
            next_round, reasoning = pipeline.latest_fixer_context(comments)
            assert pipeline.fixer_original_review(comments, reasoning) == original_review
            assert next_round and next_round != fix_round
            assert "current caller already guards" in reasoning
            fix_round = next_round
        assert pipeline.LABEL_READY_FOR_HUMAN not in transitions


def test_harvest_cli_threads_no_change_identity(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"result":
        "Finding: guard\nDecision: no-change\nReason: Already present.\nNO_CHANGES_RECHECK_NEEDED",
    }))
    assert pipeline.main([
        "harvest-fix", "--result", str(result_path), "--pinned-head", "a",
        "--current-head", "a", "--dispatch-id", "fix-42",
        "--review-sha256", "e" * 64,
    ]) == 0
    fix = json.loads(capsys.readouterr().out)
    assert fix["label"] == pipeline.LABEL_REVISED
    assert f"<!-- pr-pipeline:fix-review {'e' * 64} -->" in fix["comment_body"]
    fix_round, _ = pipeline.latest_fixer_context([{"body": fix["comment_body"]}])
    comments_path = tmp_path / "comments.json"
    comments_path.write_text("[]")
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps({"verdict": "ready_for_human", "findings": []}))
    args = [
        "harvest-review", "--review", str(review_path), "--comments", str(comments_path),
        "--pinned-base", "b", "--current-base", "b", "--pinned-head", "a",
        "--current-head", "a", "--fix-round", fix_round,
    ]
    assert pipeline.main(args) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["label"] == pipeline.LABEL_READY_FOR_HUMAN
    assert fix_round in review["comment_body"]
    comments_path.write_text(json.dumps([{"body": review["comment_body"]}]))
    assert pipeline.main(args) == 0
    assert json.loads(capsys.readouterr().out)["post_comment"] is False


# --- Helpers ---


def write_marker(
    wt_dir: Path, *, stage: str, dispatch: str, fix_round: str = "", review_sha256: str = "",
) -> None:
    marker = {
        "engine": "codex" if stage == "review" else "claude", "stage": stage,
        "pr_number": 42, "deadline": 9999999999, "base_ref": "main",
        "base_sha": "b" * 40, "head_sha": "a" * 40,
        "pre_label": pipeline.LABEL_REVISED if stage == "review" else pipeline.LABEL_CHANGES_REQUESTED,
        "dispatch_id": dispatch, "pid": 12345, "pgid": 12345, "start_time": 9999999000,
    }
    if fix_round:
        marker["fix_round"] = fix_round
    if review_sha256:
        marker["review_sha256"] = review_sha256
    (wt_dir / "dispatch.json").write_text(json.dumps(marker))
