"""Versioned fixer evidence through the real launch, harvest and re-review path."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pipeline
import pytest

from test_smoke_loop import loop_repo  # noqa: F401
from test_review_reports import (  # noqa: F401
    comment, install_reviewer_shim, report, report_repo, run_report_tick,
    wait_for_artifact,
)


@pytest.fixture
def original(report):
    report["findings"] *= 3
    report["findings"] = [dict(f, id=f"F{i}") for i, f in enumerate(report["findings"], 1)]
    return pipeline.harvest_review(
        review=json.dumps(report), comments="[]", pinned_base="b" * 40,
        current_base="b" * 40, pinned_head="a" * 40, current_head="a" * 40,
        trusted_author="test-owner", report_protocol_version=2, review_identity="origin",
    )["comment_body"]


@pytest.fixture
def revision(original):
    return revision_for(pipeline.review_reports.handoff(original))


def test_full_mixed_report_preserves_markdown_and_all_finding_references(original, revision):
    result = harvest_revision(original, revision)
    assert result["label"] == pipeline.LABEL_REVISED
    for field in ("revision_summary", "verification", "documentation", "remaining_work", "additional_context"):
        assert revision[field] in result["comment_body"]
    for finding in revision["findings"]:
        for field in ("reference", "assessment", "evidence", "change"):
            assert finding[field] in result["comment_body"]


@pytest.mark.parametrize("defect", ["missing", "duplicate", "unknown", "bare", "version", "dispatch", "head", "review", "waiver", "error", "duplicate-key"])
def test_invalid_new_report_holds_without_retry_or_legacy_fallback(original, revision, defect):
    extra = {}
    raw = None
    if defect == "missing":
        revision["findings"].pop()
    elif defect == "duplicate":
        revision["findings"].append(copy.deepcopy(revision["findings"][0]))
    elif defect == "unknown":
        revision["findings"][0]["reference"] += "unknown"
    elif defect == "bare":
        raw = pipeline.SENTINEL_FIXES_PUSHED
    elif defect == "version":
        revision["report_protocol_version"] = True
    elif defect == "dispatch":
        revision["dispatch_id"] = "another-run"
    elif defect == "head":
        revision["published_head"] = "c" * 40
    elif defect == "review":
        revision["review_sha256"] = "0" * 64
    elif defect == "waiver":
        extra["verification_unconfigured"] = True
    elif defect == "error":
        extra["engine_exit"] = 1
    else:
        raw = json.dumps(revision).replace('"outcome":', '"outcome": "NEEDS_HUMAN_DECISION", "outcome":')
    result = harvest_revision(original, revision, raw=raw, **extra)
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert result["action"] == "escalate"
    assert result["validation_reason"]


def test_schema_is_built_from_the_report_field_sets():
    schema = pipeline.fixer_reports.schema()
    assert set(schema["properties"]) == pipeline.fixer_reports.REQUIRED | {"additional_context"}
    assert set(schema["required"]) == pipeline.fixer_reports.REQUIRED
    assert schema["additionalProperties"] is False
    finding = schema["properties"]["findings"]["items"]
    assert set(finding["properties"]) == set(finding["required"]) == pipeline.fixer_reports.FINDING_FIELDS
    assert schema["properties"]["report_protocol_version"]["const"] == pipeline.fixer_reports.VERSION


def test_structured_report_is_harvested_despite_prose_in_final_message(original, revision):
    raw = "The PR body replacement is written, which is the last step before the report.\n" + json.dumps(revision)
    result = harvest_revision(original, revision, raw=raw, structured=revision,
                              report_transport=pipeline.fixer_reports.STRUCTURED_TRANSPORT)
    assert result["label"] == pipeline.LABEL_REVISED
    assert result["action"] == "recheck"
    assert revision["revision_summary"] in result["comment_body"]


@pytest.mark.parametrize("structured", [None, "text", ["list"]])
def test_structured_dispatch_without_structured_object_names_missing_report(original, revision, structured):
    result = harvest_revision(original, revision, structured=structured, omit_structured=structured is None,
                              report_transport=pipeline.fixer_reports.STRUCTURED_TRANSPORT)
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert result["validation_reason"] == pipeline.fixer_reports.MISSING_STRUCTURED_REPORT
    assert "structured" in result["validation_reason"]


def test_structured_report_keeps_specific_semantic_and_binding_reasons(original, revision):
    revision["findings"].pop()
    result = harvest_revision(original, revision, structured=revision,
                              report_transport=pipeline.fixer_reports.STRUCTURED_TRANSPORT)
    assert result["validation_reason"] == "Missing or unknown originating finding references."
    revision["outcome"] = "DONE"
    result = harvest_revision(original, revision, structured=revision,
                              report_transport=pipeline.fixer_reports.STRUCTURED_TRANSPORT)
    assert result["validation_reason"] == "Unknown revision outcome."


def test_inflight_text_dispatch_ignores_structured_output(original, revision):
    # A dispatch launched before the schema transport keeps its text parse,
    # whatever the result carries: the marker, not the content, decides.
    raw = "Prose first.\n" + json.dumps(revision)
    result = harvest_revision(original, revision, raw=raw, structured=revision)
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert "Missing or malformed JSON revision report" in result["validation_reason"]
    assert harvest_revision(original, revision)["label"] == pipeline.LABEL_REVISED


def test_held_fixer_outcome_reads_structured_report_for_structured_marker(original, revision):
    marker = dict(dispatch_id="fix-run", head_sha="a" * 40,
                  review_sha256=pipeline.review_reports.digest(original),
                  review_handoff=pipeline.review_reports.handoff(original),
                  report_transport=pipeline.fixer_reports.STRUCTURED_TRANSPORT)
    result = {"report_protocol_version": 2, "is_error": False, "claude_exit_status": 0,
              "result": "Prose first.\n" + json.dumps(revision), "structured_output": revision}
    assert pipeline.held_completion_outcome("fix", result, report_protocol_version=2,
                                             fix_marker=marker) == pipeline.SENTINEL_FIXES_PUSHED
    del result["structured_output"]
    assert pipeline.held_completion_outcome("fix", result, report_protocol_version=2,
                                             fix_marker=marker) == pipeline.OUTCOME_UNRECOGNIZED


@pytest.mark.parametrize("change", ["none", "value", "stage", "unversioned"])
def test_marker_accepts_report_transport_only_for_versioned_fixer(original, change):
    marker = dict(engine="claude", stage="fix", pr_number=7, deadline=9999999999,
                  base_ref="main", base_sha="b" * 40, head_sha="a" * 40,
                  pre_label=pipeline.LABEL_CHANGES_REQUESTED, dispatch_id="fix-run", state="launching",
                  report_protocol_version=2, review_sha256=pipeline.review_reports.digest(original),
                  review_handoff=pipeline.review_reports.handoff(original),
                  report_transport=pipeline.fixer_reports.STRUCTURED_TRANSPORT)
    if change == "value":
        marker["report_transport"] = "text"
    elif change == "stage":
        marker.update(stage="implement")
        for key in ("report_protocol_version", "review_sha256", "review_handoff"):
            del marker[key]
    elif change == "unversioned":
        for key in ("report_protocol_version", "review_handoff"):
            del marker[key]
    assert pipeline.valid_marker(marker) is (change == "none")


@pytest.mark.parametrize("partial_push", [False, True])
def test_unresolved_report_is_valid_reporting_and_publishes_complete_explanation(original, revision, partial_push):
    revision["outcome"] = pipeline.SENTINEL_NEEDS_HUMAN_DECISION
    revision["findings"][0]["disposition"] = "unresolved"
    if not partial_push:
        revision["published_head"] = revision["pinned_head"]
    result = harvest_revision(original, revision, current_head=revision["published_head"])
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert not result["validation_reason"]
    assert revision["additional_context"] in result["comment_body"]


def test_no_change_round_stays_bound_and_token_quotes_cannot_select_outcome(original, revision):
    revision["outcome"] = pipeline.SENTINEL_NO_CHANGES
    revision["published_head"] = revision["pinned_head"]
    for finding in revision["findings"]:
        finding["disposition"] = "no-change"
    revision["additional_context"] = "```text\nNEEDS_HUMAN_DECISION\nFIXES_PUSHED_RECHECK_NEEDED\n```"
    first = harvest_revision(original, revision, current_head="a" * 40)
    assert first["label"] == pipeline.LABEL_REVISED
    assert revision["additional_context"] in first["comment_body"]
    assert first == harvest_revision(original, revision, current_head="a" * 40)
    token, body = pipeline.latest_fixer_context([comment(first["comment_body"])], trusted_author="test-owner")
    assert token and body == first["comment_body"]
    assert pipeline.fixer_original_review([comment(original), comment(body)], body, trusted_author="test-owner") == original


@pytest.mark.parametrize("value", ["x" * 60001, "界" * 21000], ids=["characters", "utf8"])
def test_oversize_preserves_rendered_evidence_and_publishes_small_notice(original, revision, value):
    revision["additional_context"] = value
    result = harvest_revision(original, revision)
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert "60,000" in result["validation_reason"]
    assert value in result["rendered_report"]
    assert pipeline.review_reports.within_limit(result["comment_body"])


def test_controller_markers_are_neutralized_but_fences_survive(original, revision):
    revision["additional_context"] = "```md\n<!-- pr-pipeline:review -->\n```"
    result = harvest_revision(original, revision)
    assert "```md\n&lt;!-- pr-pipeline:review -->\n```" in result["comment_body"]


def test_legacy_inflight_result_keeps_old_gate_and_names_missing_evidence():
    result = pipeline.harvest_fix(result=json.dumps({"result": pipeline.SENTINEL_FIXES_PUSHED}),
                                  pinned_head="a" * 40, current_head="b" * 40)
    assert result["label"] == pipeline.LABEL_REVISED
    assert "Legacy" in result["comment_body"]


def test_held_fixer_outcome_requires_dispatch_binding_not_prose_tokens(original, revision):
    marker = dict(dispatch_id="fix-run", head_sha="a" * 40,
                  review_sha256=pipeline.review_reports.digest(original),
                  review_handoff=pipeline.review_reports.handoff(original))
    revision["additional_context"] = "The earlier run said NEEDS_HUMAN_DECISION."
    result = {"report_protocol_version": 2, "is_error": False,
              "claude_exit_status": 0, "result": json.dumps(revision)}
    assert pipeline.held_completion_outcome("fix", result, report_protocol_version=2,
                                             fix_marker=marker) == pipeline.SENTINEL_FIXES_PUSHED
    marker["dispatch_id"] = "other-run"
    assert pipeline.held_completion_outcome("fix", result, report_protocol_version=2,
                                             fix_marker=marker) == pipeline.OUTCOME_UNRECOGNIZED
    assert pipeline.held_completion_outcome("fix", result) == pipeline.OUTCOME_UNRECOGNIZED


@pytest.mark.parametrize("defect", ["version", "snapshot", "digest"])
def test_malformed_new_marker_cannot_select_legacy_completion(original, defect):
    marker = dict(engine="claude", stage="fix", pr_number=7, deadline=9999999999,
                  base_ref="main", base_sha="b" * 40, head_sha="a" * 40,
                  pre_label=pipeline.LABEL_CHANGES_REQUESTED, dispatch_id="fix-run", state="launching",
                  report_protocol_version=2, review_sha256=pipeline.review_reports.digest(original),
                  review_handoff=pipeline.review_reports.handoff(original))
    assert pipeline.valid_marker(marker)
    if defect == "version":
        marker["report_protocol_version"] = None
    elif defect == "snapshot":
        del marker["review_handoff"]
    else:
        marker["review_sha256"] = "0" * 64
    assert not pipeline.valid_marker(marker)


def test_resultless_new_fixer_with_unproved_tree_death_cannot_requeue_code(tmp_path, monkeypatch, original):
    import tick

    marker = dict(engine="claude", stage="fix", pr_number=7, deadline=9999999999,
                  base_ref="main", base_sha="b" * 40, head_sha="a" * 40,
                  pre_label=pipeline.LABEL_CHANGES_REQUESTED, dispatch_id="fix-run",
                  pid=123, pgid=123, start_time=1.0, report_protocol_version=2,
                  review_sha256=pipeline.review_reports.digest(original),
                  review_handoff=pipeline.review_reports.handoff(original))
    (tmp_path / "dispatch.json").write_text(json.dumps(marker))
    monkeypatch.setattr(tick, "gh_text", lambda *a: "OPEN")
    monkeypatch.setattr(tick, "gh_json", lambda *a: {"labels": [{"name": pipeline.LABEL_FIXING}]})
    monkeypatch.setattr(tick, "_resume_terminal_escalation", lambda *a: False)
    monkeypatch.setattr(tick, "_classify_marker", lambda *a, **kw: ("stale", "mismatched", False))
    decision = tick._reconcile_one(7, str(tmp_path), tick._FIXER_CONFIG, None)
    assert decision["action"] == "leave"
    assert (tmp_path / "dispatch.json").exists()


@pytest.mark.parametrize("case", ["no-change", "unresolved", "unavailable", "bare", "missing", "duplicate", "unknown", "oversize", "head-mismatch", "legacy", "empty", "absent", "invalid-utf8", "quota", "prose", "structured-missing", "inflight"])
def test_real_fixer_outcomes_retain_complete_evidence_without_code_reruns(report_repo, original, report, case):
    root, env = report_repo
    prepare_fixer(root, env, original, case=case)
    install_reviewer_shim(root, env, dict(report, verdict="ready_for_human", findings=[]))
    run_report_tick(root, env)
    worktree = root / ".claude/worktrees/pr-7"
    wait_for_artifact(worktree / "result.json")
    if case == "legacy":
        marker = json.loads((worktree / "dispatch.json").read_text())
        del marker["report_protocol_version"], marker["report_transport"]
        (worktree / "dispatch.json").write_text(json.dumps(marker))
        result = json.loads((worktree / "result.json").read_text())
        result.pop("report_protocol_version", None)
        result["result"] = pipeline.SENTINEL_FIXES_PUSHED
        (worktree / "result.json").write_text(json.dumps(result))
    elif case == "inflight":
        # A dispatch launched before the schema transport: its marker has no
        # report_transport and its result holds the report as the final message.
        marker = json.loads((worktree / "dispatch.json").read_text())
        del marker["report_transport"]
        (worktree / "dispatch.json").write_text(json.dumps(marker))
        result = json.loads((worktree / "result.json").read_text())
        result["result"] = json.dumps(result.pop("structured_output"))
        (worktree / "result.json").write_text(json.dumps(result))
    elif case == "prose":
        # The real stream wrapper kept the result event's structured_output,
        # and the final message holds prose that the text parse rejects.
        result = json.loads((worktree / "result.json").read_text())
        assert result["result"].startswith("The PR body replacement is written")
        assert result["structured_output"]["outcome"] == pipeline.SENTINEL_FIXES_PUSHED
    elif case == "empty":
        (worktree / "result.json").write_bytes(b"")
    elif case == "absent":
        (worktree / "result.json").unlink()
    elif case == "invalid-utf8":
        (worktree / "result.json").write_bytes(b'{"result":"\xff"}')
    source = (worktree / "result.json").read_bytes() if case != "absent" else None
    run_report_tick(root, env)
    published = json.loads((root / "reports.json").read_text())
    assert len(published) == 2
    if case in {"no-change", "legacy", "prose", "inflight"}:
        assert "re-review requested" in published[-1]
        if case == "legacy":
            assert "Legacy" in published[-1]
        else:
            assert "complete evidence " * 300 in published[-1]
            assert "```sh\npytest -k retry\n```" in published[-1]
            assert ("<!-- pr-pipeline:no-change " in published[-1]) is (case == "no-change")
        wait_for_artifact(worktree / "completion.json")
        run_report_tick(root, env)
    else:
        assert (root / "label").read_text() == pipeline.LABEL_NEEDS_HUMAN
        assert not worktree.exists()
        [manifest_path] = (root / ".claude/worktrees/.harvest-evidence").glob("pr-7-*/fix-manifest.json")
        manifest = json.loads(manifest_path.read_text())
        archived = manifest_path.with_name("result.json")
        if case == "absent":
            assert not archived.exists() and manifest["source_present"] is False
        else:
            assert archived.read_bytes() == source
        if case == "structured-missing":
            assert manifest["validation_reason"] == pipeline.fixer_reports.MISSING_STRUCTURED_REPORT
            assert pipeline.fixer_reports.MISSING_STRUCTURED_REPORT in published[-1]
        if case in {"unresolved", "unavailable"}:
            assert not manifest["validation_reason"]
            assert "complete evidence " * 300 in published[-1]
        else:
            assert manifest["validation_reason"]
        run_report_tick(root, env)
        assert json.loads((root / "reports.json").read_text()) == published
    assert (root / "fixer-launches").read_text() == "1"


@pytest.mark.parametrize("fault", ["comment", "label", "archive", "manifest", "body"])
@pytest.mark.parametrize("case", ["mixed", "unresolved"])
def test_real_publication_failures_retry_without_losing_source_or_repeating_code(report_repo, original, report, fault, case):
    if fault == "body" and case == "unresolved":
        pytest.skip("Unresolved reports do not replace the PR body.")
    root, env = report_repo
    prepare_fixer(root, env, original, case=case)
    install_reviewer_shim(root, env, dict(report, verdict="ready_for_human", findings=[]))
    run_report_tick(root, env)
    worktree = root / ".claude/worktrees/pr-7"
    wait_for_artifact(worktree / "result.json")
    source = (worktree / "result.json").read_bytes()
    (root / ".claude/worktrees/7-attempts").write_text("1")
    if fault == "archive":
        obstacle = root / ".claude/worktrees/.harvest-evidence"
        obstacle.write_text("not a directory")
    elif fault == "manifest":
        obstacle = worktree / "fix-manifest.json"
        obstacle.mkdir()
    else:
        obstacle = root / f"fail-{fault}"
        obstacle.touch()
    if fault == "body":
        # A real controller-prefixed warning is an authorized body-only update.
        marker = json.loads((worktree / "dispatch.json").read_text())
        marker["verification_unconfigured"] = True
        (worktree / "dispatch.json").write_text(json.dumps(marker))
        result = json.loads(source)
        result["structured_output"]["verification_waiver"] = pipeline.VERIFICATION_WAIVER
        (worktree / "result.json").write_text(json.dumps(result))
        source = (worktree / "result.json").read_bytes()
    run_report_tick(root, env)
    assert (worktree / "dispatch.json").exists()
    assert (worktree / "result.json").read_bytes() == source
    assert (root / "label").read_text() == pipeline.LABEL_FIXING
    assert int((root / ".claude/worktrees/7-attempts").read_text()) >= 1
    if obstacle.is_dir():
        obstacle.rmdir()
    else:
        obstacle.unlink()
    run_report_tick(root, env)
    published = json.loads((root / "reports.json").read_text())
    assert len(published) == 2
    assert "complete evidence " * 300 in published[-1]
    assert (root / "fixer-launches").read_text() == "1"
    if case == "mixed":
        wait_for_artifact(worktree / "completion.json")
        run_report_tick(root, env)
    else:
        assert (root / "label").read_text() == pipeline.LABEL_NEEDS_HUMAN
    [archived] = (root / ".claude/worktrees/.harvest-evidence").glob("pr-7-*/result.json")
    assert archived.read_bytes() == source


def test_archive_move_failure_keeps_retry_counter_and_original_worktree(report_repo, original, report):
    root, env = report_repo
    prepare_fixer(root, env, original)
    install_reviewer_shim(root, env, dict(report, verdict="ready_for_human", findings=[]))
    run_report_tick(root, env)
    worktree = root / ".claude/worktrees/pr-7"
    wait_for_artifact(worktree / "result.json")
    fail = root / "fail-comment"
    fail.touch()
    run_report_tick(root, env)
    counter = root / ".claude/worktrees/7-attempts"
    attempts = counter.read_text()
    [archive] = (root / ".claude/worktrees/.harvest-evidence").glob("pr-7-*")
    (archive / "scratch").mkdir()
    fail.unlink()
    run_report_tick(root, env)
    assert (worktree / "dispatch.json").exists()
    assert counter.read_text() == attempts
    assert (root / "label").read_text() == pipeline.LABEL_REVISED
    (archive / "scratch").rmdir()
    run_report_tick(root, env)
    assert (root / "fixer-launches").read_text() == "1"
    wait_for_artifact(worktree / "completion.json")
    run_report_tick(root, env)


def test_new_pushed_report_cannot_launch_rereview_without_exact_original(report_repo, original, revision):
    root, env = report_repo
    body = harvest_revision(original, revision)["comment_body"]
    (root / "reports.json").write_text(json.dumps([body]))
    (root / "label").write_text(pipeline.LABEL_REVISED)
    run_report_tick(root, env)
    assert (root / "label").read_text() == pipeline.LABEL_REVISED
    assert not (root / ".claude/worktrees/pr-7/dispatch.json").exists()


@pytest.mark.parametrize("change", ["head", "hold"])
def test_late_authority_change_prevents_stale_fixer_publication(report_repo, original, change):
    root, env = report_repo
    prepare_fixer(root, env, original)
    run_report_tick(root, env)
    worktree = root / ".claude/worktrees/pr-7"
    wait_for_artifact(worktree / "result.json")
    (root / f"late-{change}").touch()
    run_report_tick(root, env)
    assert (worktree / "dispatch.json").exists()
    assert json.loads((root / "reports.json").read_text()) == [original]


def test_fixer_cli_uses_pinned_protocol_and_full_finding_snapshot(tmp_path, original, revision):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"report_protocol_version": 2, "is_error": False,
                                 "claude_exit_status": 0, "result": json.dumps(revision)}))
    snapshot = tmp_path / "handoff.json"
    snapshot.write_text(json.dumps(pipeline.review_reports.handoff(original)))
    proc = subprocess.run([sys.executable, pipeline.__file__, "harvest-fix", "--result", str(result),
                           "--report-protocol-version", "2", "--review-handoff", str(snapshot),
                           "--dispatch-id", "fix-run", "--pinned-head", "a" * 40, "--current-head", "b" * 40,
                           "--review-sha256", pipeline.review_reports.digest(original)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["label"] == pipeline.LABEL_REVISED


@pytest.mark.parametrize("engine", ["claude", "claude-ds"])
def test_real_fixer_launch_pins_protocol_through_adapter_and_reviewer_handoff(report_repo, original, report, engine):
    root, env = report_repo
    prepare_fixer(root, env, original, engine=engine)
    followup = dict(report, verdict="ready_for_human", findings=[])
    install_reviewer_shim(root, env, followup)
    run_report_tick(root, env)
    worktree = root / ".claude/worktrees/pr-7"
    wait_for_artifact(worktree / "result.json")
    marker = json.loads((worktree / "dispatch.json").read_text())
    result = json.loads((worktree / "result.json").read_text())
    assert marker["report_protocol_version"] == result["report_protocol_version"] == 2
    assert marker["report_transport"] == pipeline.fixer_reports.STRUCTURED_TRANSPORT
    fixer_argv = json.loads((root / "fixer-argv.json").read_text())
    assert json.loads(fixer_argv[fixer_argv.index("--json-schema") + 1]) == pipeline.fixer_reports.schema()
    assert result["structured_output"]["dispatch_id"] == marker["dispatch_id"]
    assert marker["engine"] == engine
    assert marker["review_handoff"] == pipeline.review_reports.handoff(original)
    assert "pr-pipeline:definition-of-done" in (worktree / "prompt.txt").read_text()
    run_report_tick(root, env)
    published = json.loads((root / "reports.json").read_text())
    assert len(published) == 2
    assert "complete evidence " * 300 in published[-1]
    wait_for_artifact(root / "reviewer-argv.json")
    argv = json.loads((root / "reviewer-argv.json").read_text())
    prompt = next(arg for arg in argv if "Review PR #7" in arg)
    assert published[-1] in prompt and original in prompt
    assert json.dumps(marker["review_handoff"]) in prompt
    assert list((root / ".claude/worktrees/.harvest-evidence").glob("pr-7-*/fix-manifest.json"))
    wait_for_artifact(worktree / "completion.json")
    run_report_tick(root, env)


# --- Helpers ---


def revision_for(snapshot, **values):
    return {
        "report_protocol_version": 2, "dispatch_id": "fix-run", "pinned_head": "a" * 40,
        "published_head": "b" * 40, "review_sha256": snapshot["comment_sha256"],
        "outcome": "FIXES_PUSHED_RECHECK_NEEDED",
        "revision_summary": "Retain evidence after failure.\n\nOnly publication ownership changed.",
        "findings": [dict(reference=ref, disposition=decision, assessment="Confirmed against the owner contract.",
                          evidence="```sh\npytest -k retry\n```\n\nPassed the retained-source probe.",
                          change="The publication owner retains the result until acknowledgement.\n\nThe replacement trades disk space for recoverability.")
                     for ref, decision in zip(snapshot["finding_refs"], ["repair", "replacement", "no-change"])],
        "verification": "Executed the failed-post probe and affected tests: passed. CI not run locally.",
        "verification_status": "passed", "verification_waiver": "",
        "documentation": "Updated the operator recovery instructions.",
        "remaining_work": "Independent review remains required.",
        "additional_context": "### Tradeoffs\n\n" + "complete evidence " * 300,
        **values,
    }


def harvest_revision(original, revision, *, raw=None, engine_exit=0, structured=None,
                     omit_structured=False, **values):
    event = {"type": "result", "is_error": False, "claude_exit_status": engine_exit,
             "report_protocol_version": 2, "result": json.dumps(revision) if raw is None else raw}
    if structured is not None and not omit_structured:
        event["structured_output"] = structured
    return pipeline.harvest_fix(
        result=json.dumps(event),
        pinned_head="a" * 40, dispatch_id="fix-run", review_sha256=pipeline.review_reports.digest(original),
        report_protocol_version=2, review_handoff=pipeline.review_reports.handoff(original),
        **{"current_head": "b" * 40, **values},
    )


def prepare_fixer(root, env, original, *, engine="claude", case="mixed"):
    (root / "reports.json").write_text(json.dumps([original]))
    (root / "label").write_text(pipeline.LABEL_CHANGES_REQUESTED)
    env["LOOP_TEST_STAGE"] = "fix"
    env["FIX_REPORT_CASE"] = case
    if engine == "claude-ds":
        env["DEEPSEEK_API_KEY"] = "offline-fixture"
    with Path(env["PR_PIPELINE_CONFIG"]).open("a") as config:
        config.write(f'fix_engine="{engine}"\n')
    gh = Path(env["PATH"].split(os.pathsep)[0]) / "gh"
    gh.write_text(gh.read_text().replace('"headRefOid": os.environ["LOOP_TEST_SHA"]',
                 '"headRefOid": (root / "published-head").read_text() if (root / "published-head").exists() else os.environ["LOOP_TEST_SHA"]'))
    gh.write_text(gh.read_text().replace('"body": "Fixture contract"',
                  '"body": (root / "live-body").read_text() if (root / "live-body").exists() else "Fixture contract"')
                  .replace('reports = json.loads((root / "reports.json").read_text())', '''reports = json.loads((root / "reports.json").read_text())
if a[:2] == ["pr", "edit"] and "--body-file" in a:
    if (root / "fail-body").exists():
        sys.exit(1)
    (root / "live-body").write_text(Path(a[a.index("--body-file") + 1]).read_text())
    sys.exit(0)'''))
    gh.write_text(gh.read_text().replace('result = None', '''if a[:2] == ["pr", "view"] and "headRefOid,body" in a:
    if (root / "late-head").exists():
        (root / "published-head").write_text("c" * 40)
    if (root / "late-hold").exists():
        (root / "label").write_text("ai:blocked")
result = None'''))
    claude = gh.with_name("claude")
    template = revision_for(pipeline.review_reports.handoff(original))
    claude.write_text(f"#!{sys.executable}\n" + f'''import json, os, subprocess, sys
from pathlib import Path
root = Path(os.environ["LOOP_TEST_ROOT"])
prompt = sys.stdin.read()
schema_bound = "--json-schema" in sys.argv
(root / "fixer-argv.json").write_text(json.dumps(sys.argv[1:]))
(root / "fixer-launches").write_text(str(int((root / "fixer-launches").read_text()) + 1) if (root / "fixer-launches").exists() else "1")
marker = json.loads(Path("dispatch.json").read_text())
report = {template!r}
report.update(dispatch_id=marker["dispatch_id"], pinned_head=marker["head_sha"])
git = os.environ["LOOP_TEST_GIT"]
case = os.environ["FIX_REPORT_CASE"]
if case != "no-change":
    subprocess.run([git, "commit", "--allow-empty", "-m", "fixture repair"], check=True, capture_output=True)
head = subprocess.check_output([git, "rev-parse", "HEAD"], text=True).strip()
subprocess.run([git, "update-ref", "refs/remotes/origin/fix", head], check=True)
(root / "published-head").write_text(head)
report["published_head"] = head
if case == "no-change":
    report["outcome"] = "NO_CHANGES_RECHECK_NEEDED"
    for finding in report["findings"]:
        finding["disposition"] = "no-change"
    report["additional_context"] += "\\n```text\\nNEEDS_HUMAN_DECISION\\nFIXES_PUSHED_RECHECK_NEEDED\\n```"
elif case in ("unresolved", "unavailable"):
    report["outcome"] = "NEEDS_HUMAN_DECISION"
    report["findings"][0]["disposition"] = "unresolved"
    if case == "unavailable":
        report["verification_status"] = "unavailable"
elif case == "missing":
    report["findings"].pop()
elif case == "duplicate":
    report["findings"].append(report["findings"][0])
elif case == "unknown":
    report["findings"][0]["reference"] += "unknown"
elif case == "head-mismatch":
    report["published_head"] = "c" * 40
elif case == "oversize":
    report["additional_context"] = "界" * 21000
raw = "FIXES_PUSHED_RECHECK_NEEDED" if case == "bare" else json.dumps(report)
event = {{"type": "result", "subtype": "success", "is_error": False, "result": raw}}
if schema_bound and case not in ("bare", "structured-missing"):
    # The shape a live `claude -p --output-format stream-json --json-schema`
    # run emits: prose text, a StructuredOutput tool call, then a result
    # event carrying the validated object in structured_output.
    prose = "The PR body replacement is written, which is the last step before the report."
    print(json.dumps({{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": prose}},
        {{"type": "tool_use", "id": "toolu_report", "name": "StructuredOutput", "input": report}}]}}}}))
    event["structured_output"] = report
    if case == "prose":
        event["result"] = prose + "\\n" + raw
if case == "quota":
    event.update(is_error=True, api_error_status=429, result="quota exceeded")
print(json.dumps(event))
''')
