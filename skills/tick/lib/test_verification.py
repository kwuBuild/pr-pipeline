"""Durable empty-grant evidence across launch, completion and publication."""

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from test_dispatch import existing_dispatch_sources  # noqa: F401

import pipeline
import tick
from conftest import (
    REVISED_PR_BODY, STALE_PR_BODY, fixer_body_provenance, fixer_gh_json,
    fixer_git, fixer_harvest_gh, fixer_harvest_marker, fixer_harvest_scratch,
    fixer_revision_file, fixer_harvest_completion,
)


@pytest.fixture
def implementer_handoff(tmp_path, monkeypatch):
    """Real reconciliation/harvest files, with only external peers replaced."""
    root = tmp_path / "worktrees"
    wt = root / "issue-7"
    wt.mkdir(parents=True)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(
        stage="implement", pr_number=7, dispatch_id="implement-7",
        pre_label="ai:available", verification_unconfigured=True,
    ))
    assert pipeline.valid_marker(json.loads((wt / "dispatch.json").read_text()))
    (wt / "completion.json").write_text(fixer_harvest_completion("implement-7"))
    (wt / "result.json").write_text(json.dumps({
        "result": "VERIFICATION_UNCONFIGURED_WAIVER\nIMPLEMENTATION_COMPLETE",
    }))
    state = {"labels": [], "headRefOid": "e" * 40, "body": "Human PR body"}
    calls = []
    fault = {"kind": "", "reads": 0}

    def gh_json(*args):
        if args[:2] == ("pr", "list"):
            return [{"number": 12, "isCrossRepository": False}]
        if args[:2] == ("issue", "view"):
            return {"labels": [{"name": "ai:implementing"}]}
        assert args[:2] == ("pr", "view"), args
        fields = args[args.index("--json") + 1]
        if "body" in fields:
            fault["reads"] += 1
            if fault["kind"] == "read" or (fault["kind"] == "late-read" and fault["reads"] == 2):
                raise subprocess.CalledProcessError(1, "gh")
            if fault["kind"] == "malformed":
                return {"labels": [], "headRefOid": 7, "body": None}
            if fault["reads"] == 2:
                if fault["kind"] == "body":
                    state["body"] = "Concurrent human edit"
                elif fault["kind"] == "head":
                    state["headRefOid"] = "f" * 40
                elif fault["kind"] == "hold":
                    state["labels"] = [{"name": "ai:blocked"}]
                elif fault["kind"] == "progressed":
                    state["labels"] = [{"name": "ai:changes-requested"}]
        return dict(state)

    def gh(*args):
        assert args[:2] == ("pr", "edit"), args
        calls.append("body")
        if fault["kind"] == "edit":
            raise subprocess.CalledProcessError(1, "gh")
        state["body"] = Path(args[args.index("--body-file") + 1]).read_text()

    def converge(*args, **kwargs):
        calls.append("issue labels")
        return fault["kind"] != "converge"

    def relabel(*args):
        calls.append("pr label")
        state["labels"] = [{"name": args[-1]}]

    monkeypatch.setattr(tick.process_control, "pid_is_alive", lambda _: False)
    monkeypatch.setattr(tick, "gh_json", gh_json)
    monkeypatch.setattr(tick, "gh_text", lambda *args: "7" if args[1] == "list" else "OPEN")
    monkeypatch.setattr(tick, "gh", gh)
    monkeypatch.setattr(tick, "relabel", relabel)
    monkeypatch.setattr(tick, "_converge_pipeline_labels", converge)
    monkeypatch.setattr(tick, "cleanup_scratch", lambda *args: calls.append("cleanup"))
    return root, wt, state, calls, fault


@pytest.mark.parametrize("stage", ["implement", "fix"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("edit_after_lease", [False, True])
def test_dispatch_pins_grants_warning_and_completion_requirement(tmp_path, monkeypatch, stage, configured, edit_after_lease):
    cfg = tmp_path / "operator.toml"
    cfg.write_text('[defaults]\nverify_tools = ' + ('["custom-check *"]' if configured else '[]'))
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    monkeypatch.setattr(tick, "WT_ROOT", str(tmp_path / "worktrees"))
    log = tick.TickLog.open(str(tmp_path / "log"))

    original_write = pipeline.atomic_write

    def write(path, data):
        original_write(path, data)
        if edit_after_lease and Path(path).name == "dispatch.json":
            cfg.write_text('[defaults]\nverify_tools = ' + ('[]' if configured else '["changed-check *"]'))

    monkeypatch.setattr(pipeline, "atomic_write", write)
    with (
        mock.patch.object(tick, "git", side_effect=fixer_git(tmp_path)),
        mock.patch.object(tick, "gh_json", side_effect=lambda *args: (
            {"defaultBranchRef": {"name": "main"}}
            if args[:2] == ("repo", "view") else fixer_gh_json()(*args)
        )),
        mock.patch.object(tick, "gh_text", side_effect=lambda *args: {
            "baseRefName": "main", "headRefOid": "a" * 40, "headRefName": "feature",
        }[args[args.index("--json") + 1]]),
        mock.patch.object(tick.subprocess, "Popen", return_value=mock.Mock(pid=12345)) as launch,
        mock.patch.object(tick, "relabel"),
    ):
        if stage == "implement":
            tick._dispatch_one_implementer(issue_number=7, issue_title="Work", tick_log=log)
        else:
            tick._dispatch_one_fixer(pr_number=7, tick_log=log)
    launch.assert_called_once()
    wt = tmp_path / "worktrees" / ("issue-7" if stage == "implement" else "pr-7")
    marker = json.loads((wt / "dispatch.json").read_text())
    assert marker["verification_unconfigured"] is not configured
    assert pipeline.valid_marker(marker)
    prompt = (wt / "prompt.txt").read_text()
    # The report schema names the optional waiver for both configurations;
    # only the dispatch-owned configuration section can require its use.
    verification_context = prompt.split("Verification configuration for this dispatch:\n", 1)[1] if stage == "fix" else prompt
    assert ("VERIFICATION_UNCONFIGURED_WAIVER" in verification_context) is not configured
    argv = launch.call_args.args[0]
    grants = argv[argv.index("--allowedTools") + 1]
    assert ("Bash(custom-check *)" in grants) is configured
    warnings = [e for e in log.tail() if e["action"] == "verification_unconfigured"]
    assert len(warnings) == (0 if configured else 1)
    if warnings:
        assert warnings[0]["dispatch_id"] == marker["dispatch_id"]


@pytest.mark.parametrize("stage,sentinel", [
    ("implement", "IMPLEMENTATION_COMPLETE"), ("fix", "FIXES_PUSHED_RECHECK_NEEDED"),
])
@pytest.mark.parametrize("waiver", ["", "VERIFICATION_UNCONFIGURED_WAIVER\n"])
def test_empty_grant_success_requires_explicit_waiver(stage, sentinel, waiver):
    fn = pipeline.harvest_implementation if stage == "implement" else pipeline.harvest_fix
    decision = fn(result=json.dumps({"result": waiver + sentinel}), verification_unconfigured=True)
    assert decision["action"] == (("create_pr" if stage == "implement" else "recheck") if waiver else "escalate")


@pytest.mark.parametrize("engine_body", [False, True])
def test_implementer_harvest_composes_warning_from_marker(tmp_path, monkeypatch, engine_body):
    wt = tmp_path / "issue-7"
    wt.mkdir()
    (wt / "dispatch.json").write_text(json.dumps({
        "dispatch_id": "implement-7", "verification_unconfigured": True,
        "base_ref": "main",
    }))
    (wt / "result.json").write_text(json.dumps({"result": "VERIFICATION_UNCONFIGURED_WAIVER\nIMPLEMENTATION_COMPLETE"}))
    if engine_body:
        (wt / ".pr-body.md").write_text("A useful change\n\n" + REVISED_PR_BODY)
    bodies = []

    def gh_text(*args):
        if args[:2] == ("pr", "create"):
            bodies.append(Path(args[args.index("--body-file") + 1]).read_text() if "--body-file" in args else args[args.index("--body") + 1])
        return "https://github.com/test/repo/pull/12"

    with (
        mock.patch.object(tick, "gh_json", side_effect=lambda *args: [] if args[1] == "list" else {
            "labels": [], "headRefOid": "e" * 40, "body": bodies[0],
        }),
        mock.patch.object(tick, "gh_text", side_effect=gh_text),
        mock.patch.object(tick, "relabel"),
        mock.patch.object(tick, "_converge_pipeline_labels", return_value=True),
        mock.patch.object(tick, "cleanup_scratch"),
        mock.patch.object(tick, "git"),
    ):
        tick._harvest_one_implementer({"pr_number": 7}, str(tmp_path))
    assert len(bodies) == 1
    assert "Verification unconfigured:" in bodies[0]
    if not engine_body:
        assert "passed before" not in bodies[0]
        assert "- [x]" not in bodies[0]


@pytest.mark.parametrize("current_empty,historical,replacement", [
    (True, False, True), (True, False, False), (False, True, True),
])
@pytest.mark.parametrize("retry", [False, True])
def test_fixer_harvest_preserves_warning(tmp_path, current_empty, historical, replacement, retry):
    warning = pipeline.VERIFICATION_WARNING
    live = STALE_PR_BODY + ("\n" + warning + "\n" if historical else "")
    wt_root, wt = fixer_harvest_scratch(tmp_path)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(live), verification_unconfigured=current_empty,
    ))
    (wt / "result.json").write_text(json.dumps({"result": "VERIFICATION_UNCONFIGURED_WAIVER\nFIXES_PUSHED_RECHECK_NEEDED"}))
    if replacement:
        (wt / ".pr-body-revision.md").write_text(fixer_revision_file(REVISED_PR_BODY))
    if retry:
        live = pipeline.with_verification_warning(
            REVISED_PR_BODY if replacement else live, required=True,
        )
    calls, staged = [], []
    with fixer_harvest_gh(calls, staged, live_body=live), mock.patch.object(tick, "cleanup_scratch"):
        tick.harvest_fixers(wt_root=str(wt_root))
    assert calls == ["pr edit", "pr comment", "converge"]
    assert staged[0][1].count(warning) == 1


@pytest.mark.parametrize("value", [None, "true", 1, [], {}])
def test_marker_rejects_malformed_verification_state(value):
    assert not pipeline.valid_marker(json.loads(fixer_harvest_marker(verification_unconfigured=value)))


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("failure", ["edit", "hold", "body", "head", "read"])
def test_warning_publication_failure_retains_custody(tmp_path, replacement, failure):
    wt_root, wt = fixer_harvest_scratch(tmp_path)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(STALE_PR_BODY), verification_unconfigured=True,
    ))
    (wt / "result.json").write_text(json.dumps({"result": "VERIFICATION_UNCONFIGURED_WAIVER\nFIXES_PUSHED_RECHECK_NEEDED"}))
    if replacement:
        (wt / ".pr-body-revision.md").write_text(fixer_revision_file(REVISED_PR_BODY))
    failure_args = {
        "edit": {"edit_fails": True},
        "hold": {"mutation_labels": ("ai:fixing", "ai:blocked")},
        "body": {"mutation_body": "Human edit"},
        "head": {"mutation_head": "f" * 40},
        "read": {"mutation_read_fails": True},
    }[failure]
    calls, staged = [], []
    with fixer_harvest_gh(calls, staged, live_body=STALE_PR_BODY, **failure_args), mock.patch.object(tick, "cleanup_scratch") as cleanup:
        tick.harvest_fixers(wt_root=str(wt_root))
    assert calls == (["pr edit"] if failure == "edit" else [])
    cleanup.assert_not_called()
    assert (wt / "dispatch.json").exists()


@pytest.mark.parametrize("report", [
    "VERIFICATION_UNCONFIGURED_WAIVER\nNEEDS_HUMAN_DECISION",
    "Denied required check. NEEDS_HUMAN_DECISION",
    "Mention VERIFICATION_UNCONFIGURED_WAIVER inline\nIMPLEMENTATION_COMPLETE",
])
def test_waiver_never_turns_blocked_or_missing_evidence_into_success(report):
    assert pipeline.harvest_implementation(
        result=json.dumps({"result": report}), verification_unconfigured=True,
    )["action"] == "escalate"


@pytest.mark.parametrize("live", ["", "Human-authored PR without an issue closure."])
def test_warning_append_does_not_require_engine_body_revision_provenance(tmp_path, live):
    wt_root, wt = fixer_harvest_scratch(tmp_path)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(verification_unconfigured=True))
    (wt / "result.json").write_text(json.dumps({"result": "VERIFICATION_UNCONFIGURED_WAIVER\nFIXES_PUSHED_RECHECK_NEEDED"}))
    calls, staged = [], []
    with fixer_harvest_gh(calls, staged, live_body=live), mock.patch.object(tick, "cleanup_scratch"):
        tick.harvest_fixers(wt_root=str(wt_root))
    assert calls == ["pr edit", "pr comment", "converge"]
    assert staged[0][1].strip() == pipeline.with_verification_warning(live, required=True).strip()


@pytest.mark.parametrize("progressed", [False, True])
def test_existing_pr_warning_precedes_handoff_and_preserves_progress(implementer_handoff, progressed):
    root, wt, state, calls, fault = implementer_handoff
    if progressed:
        state["labels"] = [{"name": "ai:changes-requested"}]
    tick.harvest_implementers(wt_root=str(root))
    assert calls == ["body", *([] if progressed else ["pr label"]), "issue labels", "cleanup"]
    assert state["body"].startswith(pipeline.VERIFICATION_WARNING + "\n\n")
    if progressed:
        assert state["labels"] == [{"name": "ai:changes-requested"}]


@pytest.mark.parametrize("failure", ["read", "late-read", "malformed", "edit", "hold", "body", "head"])
def test_existing_pr_warning_failure_retains_custody(implementer_handoff, failure):
    root, wt, state, calls, fault = implementer_handoff
    fault["kind"] = failure
    tick.harvest_implementers(wt_root=str(root))
    assert calls == (["body"] if failure == "edit" else [])
    assert (wt / "dispatch.json").exists()
    assert (wt / "result.json").exists()
    assert (wt / "completion.json").exists()


@pytest.mark.parametrize("changed_between_retries", [False, True])
def test_existing_pr_retry_rechecks_warning_after_landed_write(implementer_handoff, changed_between_retries):
    root, wt, state, calls, fault = implementer_handoff
    fault["kind"] = "converge"
    tick.harvest_implementers(wt_root=str(root))
    assert calls == ["body", "pr label", "issue labels"]
    assert (wt / "dispatch.json").exists()
    if changed_between_retries:
        state["body"] = "Later human revision"
    calls.clear()
    fault.update(kind="", reads=0)
    tick.harvest_implementers(wt_root=str(root))
    assert calls == (["body"] if changed_between_retries else []) + ["issue labels", "cleanup"]
    assert state["body"].startswith(pipeline.VERIFICATION_WARNING + "\n\n")
    assert state["body"].count(pipeline.VERIFICATION_WARNING) == 1


def test_existing_pr_progress_during_warning_check_is_preserved(implementer_handoff):
    root, wt, state, calls, fault = implementer_handoff
    fault["kind"] = "progressed"
    tick.harvest_implementers(wt_root=str(root))
    assert calls == []
    assert state["labels"] == [{"name": "ai:changes-requested"}]
    fault.update(kind="", reads=0)
    tick.harvest_implementers(wt_root=str(root))
    assert calls == ["body", "issue labels", "cleanup"]
    assert state["labels"] == [{"name": "ai:changes-requested"}]


@pytest.mark.parametrize("tail", [
    "\n<!--\n{warning}\n-->", "\n<!--", "\n```\n{warning}",
    "\n<details>\n{warning}", "\n<textarea>\n{warning}",
])
@pytest.mark.parametrize("stage", ["implement", "fix"])
def test_controller_warning_is_visible_before_engine_markdown(tmp_path, tail, stage):
    body = REVISED_PR_BODY + tail.format(warning=pipeline.VERIFICATION_WARNING)
    if stage == "implement":
        (tmp_path / ".pr-body.md").write_text("Title\n" + body)
        args = tick._implementation_pr_create_args(str(tmp_path), 7, verification_unconfigured=True)
        published = Path(args[args.index("--body-file") + 1]).read_text()
    else:
        root, wt = fixer_harvest_scratch(tmp_path)
        live = STALE_PR_BODY + "\n\n" + pipeline.VERIFICATION_WARNING
        (wt / "dispatch.json").write_text(fixer_harvest_marker(
            pr_body=fixer_body_provenance(live), verification_unconfigured=False,
        ))
        (wt / ".pr-body-revision.md").write_text(fixer_revision_file(body))
        calls, staged = [], []
        with fixer_harvest_gh(calls, staged, live_body=live), mock.patch.object(tick, "cleanup_scratch"):
            tick.harvest_fixers(wt_root=str(root))
        assert calls == ["pr edit", "pr comment", "converge"]
        published = staged[0][1]
    # A complete first paragraph cannot be captured by any following Markdown.
    assert published.startswith(pipeline.VERIFICATION_WARNING + "\n\n")
    assert body.strip() in published


@pytest.mark.parametrize("sentinel,fn", [
    ("IMPLEMENTATION_COMPLETE", pipeline.harvest_implementation),
    ("FIXES_PUSHED_RECHECK_NEEDED", pipeline.harvest_fix),
])
@pytest.mark.parametrize("final", ["{sentinel}", "inline {sentinel}", "{sentinel} trailing"])
def test_waiver_belongs_to_the_authoritative_sentinel(sentinel, fn, final):
    report = f"VERIFICATION_UNCONFIGURED_WAIVER\n{sentinel}\nExample ends.\n" + final.format(sentinel=sentinel)
    assert fn(result=json.dumps({"result": report}), verification_unconfigured=True)["action"] == "escalate"


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("waiver", [False, True])
def test_no_change_completion_composes_with_empty_grant_waiver(empty, waiver):
    report = "Finding: guard\nDecision: no-change\nReason: Already present.\n"
    if waiver:
        report += pipeline.VERIFICATION_WAIVER + "\n"
    report += "NO_CHANGES_RECHECK_NEEDED"
    result = pipeline.harvest_fix(
        result=json.dumps({"result": report}), verification_unconfigured=empty,
        pinned_head="a" * 40, current_head="a" * 40,
        dispatch_id="fix-12", review_sha256="b" * 64,
    )
    assert result["action"] == ("recheck" if waiver == empty else "escalate")


@pytest.mark.parametrize("existing_warning", [False, True])
def test_existing_pr_hold_keeps_implementation_custody(implementer_handoff, existing_warning):
    root, wt, state, calls, fault = implementer_handoff
    state["labels"] = [{"name": "ai:blocked"}]
    if existing_warning:
        state["body"] = pipeline.with_verification_warning(state["body"], required=True)
    tick.harvest_implementers(wt_root=str(root))
    assert calls == []
    assert (wt / "dispatch.json").exists()
    assert tick.Attempts(str(root), tick._IMPLEMENTER_CONFIG.attempts_key_prefix).read(7) == 0


def test_no_change_fixer_harvest_requires_and_publishes_empty_grant_evidence(tmp_path):
    root, wt = fixer_harvest_scratch(tmp_path)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(
        verification_unconfigured=True, review_sha256="b" * 64,
    ))
    (wt / "result.json").write_text(json.dumps({"result": (
        "Finding: guard\nDecision: no-change\nReason: Already present.\n"
        "VERIFICATION_UNCONFIGURED_WAIVER\nNO_CHANGES_RECHECK_NEEDED"
    )}))
    calls, staged = [], []
    with fixer_harvest_gh(calls, staged, live_body=STALE_PR_BODY, live_head="d" * 40), mock.patch.object(tick, "cleanup_scratch") as cleanup:
        tick.harvest_fixers(wt_root=str(root))
    assert calls == ["pr edit", "pr comment", "converge"]
    assert staged[0][1].startswith(pipeline.VERIFICATION_WARNING + "\n\n")
    assert "pr-pipeline:no-change" in staged[1][1]
    cleanup.assert_called_once()


@pytest.mark.parametrize("empty", [False, True])
def test_historical_hidden_warning_is_restored_without_a_replacement(tmp_path, empty):
    root, wt = fixer_harvest_scratch(tmp_path)
    live = STALE_PR_BODY + "\n<!--\n" + pipeline.VERIFICATION_WARNING + "\n-->"
    (wt / "dispatch.json").write_text(fixer_harvest_marker(verification_unconfigured=empty))
    (wt / "result.json").write_text(json.dumps({"result": (
        "VERIFICATION_UNCONFIGURED_WAIVER\nFIXES_PUSHED_RECHECK_NEEDED"
    )}))
    calls, staged = [], []
    with fixer_harvest_gh(calls, staged, live_body=live), mock.patch.object(tick, "cleanup_scratch"):
        tick.harvest_fixers(wt_root=str(root))
    assert calls == ["pr edit", "pr comment", "converge"]
    assert staged[0][1].startswith(pipeline.VERIFICATION_WARNING + "\n\n")
    assert live.strip() in staged[0][1]
