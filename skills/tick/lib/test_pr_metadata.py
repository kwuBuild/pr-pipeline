"""Authored PR reports through the public tick CLI and revision boundary."""

import json
import re
from pathlib import Path
import time

import pytest

import tick
from test_verification_cli import offline_tick, run_tick


@pytest.fixture
def authored_report():
    return (
        "## Summary\n- Configure routing without discarding operator choices.\n"
        "- Closes #7\n\n## Why this approach\nKeep the existing controller owner.\n\n"
        "## What changed\n- Structure: configuration feeds dispatch.\n"
        "- Impact:\n  - Stage choices remain explicit.\n"
        "- Branch history: one final commit.\n- Scope: no provider changes.\n\n"
        "### Routing examples\n```md\n## Summary\nCloses #999\n"
        "## Test plan\nkeep  two spaces  \n```\n\n"
        "## Test coverage\nProved at: " + "a" * 40 + "\n"
        + "Evidence detail: exact command and observed result.\n" * 220
        + "\n## Test plan\n- [x] `python3.13 -m pytest skills/ -q`\n\n"
        "## Additional context\nMigration deferred. Also fixes #8.\n"
    )


@pytest.fixture(params=["bullet-to-ordered", "ordered-delimiter", "empty-bullet", "empty-ordered"])
def list_transition_example(request):
    return {
        "bullet-to-ordered": "- Example\n2. ```md\n   Closes #999\n   ```\n",
        "ordered-delimiter": "1. Example\n2) ```md\n   Closes #999\n   ```\n",
        "empty-bullet": "-\n\n  ```md\nCloses #999\n```\n",
        "empty-ordered": "1.\n\n   ```md\nCloses #999\n```\n",
    }[request.param]


@pytest.mark.parametrize("impact", [True, False], ids=["impact-with-tail", "no-impact-bullet"])
def test_cli_preserves_complete_authored_report(offline_tick, authored_report, impact):
    if not impact:
        authored_report = authored_report.replace("- Impact:\n  - Stage choices remain explicit.", "Stage choices remain explicit.")
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    title = "feat: configure stage deadlines, engine routing, and loop cadence"
    source = (title + "\r\n\r\n" + authored_report.replace("\n", "\r\n")).encode()
    (wt / ".pr-body.md").write_bytes(source)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live["title"] == title
    assert authored_report in live["body"]
    assert "Metadata/layout notice:" not in live["body"]
    assert live["pr_labels"] == ["ai:implemented"]
    assert live["labels"] == []
    records = list((wt.parent / ".pr-publications").glob("*/decision.json"))
    assert len(records) == 1
    assert (records[0].parent / ".pr-body.md").read_bytes() == source
    decision = json.loads(records[0].read_text())
    assert decision["normalizations"] == ["CRLF to LF"]
    assert decision["outcome"] == "authored"
    from unittest import mock
    with mock.patch("tick.gh_json", return_value={
        "body": live["body"], "closingIssuesReferences": [],
    }):
        context = tick._fixer_pr_context(12)
    assert context is not None
    provenance = context.body_provenance
    assert provenance.summary_closes == 7
    assert provenance.closes == ("#7", "#8", "#999")


def test_missing_sections_preserves_prose_and_title(tmp_path):
    body = "## Summary\nCloses #7\nThe scope and evidence remain authored.\n"
    (tmp_path / ".pr-body.md").write_text("fix: preserve details\n\n" + body)
    args = tick._implementation_pr_create_args(str(tmp_path), 7)
    assert args[args.index("--title") + 1] == "fix: preserve details"
    published = Path(args[args.index("--body-file") + 1]).read_text()
    assert body in published
    assert published.count("Metadata/layout notice:") == 1
    assert "Why this approach, What changed, Test coverage, Test plan" in published
    assert "passed before" not in published


@pytest.mark.parametrize("source,title", [
    (None, "Issue #7: implementation review"),
    (b"", "Issue #7: implementation review"),
    (b"fix: specific title\n\n", "fix: specific title"),
    (b"fix: recover title\n\n\xff", "fix: recover title"),
])
def test_unavailable_body_is_truthful(tmp_path, source, title):
    if source is not None:
        (tmp_path / ".pr-body.md").write_bytes(source)
    args = tick._implementation_pr_create_args(str(tmp_path), 7)
    assert args[args.index("--title") + 1] == title
    body = Path(args[args.index("--body-file") + 1]).read_text()
    assert "Closes #7" in body
    assert "Metadata/layout notice:" in body
    assert "No verification results are claimed" in body
    assert "passed" not in body
    assert "now implemented" not in body


@pytest.mark.parametrize("body,reason", [
    ("## Summary\nCloses #8", "does not match target"),
    ("## Summary\nCloses #7\n- Closes #8", "ambiguous primary closure"),
    ("## Summary\nCloses #7\n" + "x" * 65536, "publication limit"),
    ("## Summary\nCloses #7\n" + "漢" * 22000, "publication limit"),
], ids=["wrong", "ambiguous", "oversized", "utf8-limit"])
def test_cli_invalid_control_escalates_with_source(offline_tick, body, reason):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    source = "fix: retain my report\n\n" + body
    (wt / ".pr-body.md").write_text(source)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert not live.get("existing_pr")
    assert live["labels"] == ["ai:needs-human-decision"]
    assert reason in "\n".join(live["comments"])
    archives = list((wt.parent / ".harvest-evidence").glob("*/.pr-body.md"))
    assert len(archives) == 1
    assert archives[0].read_text() == source


@pytest.mark.parametrize("failure", ["read", "stage"])
def test_io_failure_retains_custody_and_never_falls_back(tmp_path, monkeypatch, failure):
    import builtins
    root = tmp_path / "worktrees"
    wt = root / "issue-7"
    wt.mkdir(parents=True)
    (wt / "dispatch.json").write_text(json.dumps({"base_ref": "main", "dispatch_id": "impl-7"}))
    (wt / "result.json").write_text('{"result":"IMPLEMENTATION_COMPLETE"}')
    source = "fix: title\n\n## Summary\nCloses #7\nExact evidence.\n"
    (wt / ".pr-body.md").write_text(source)
    calls = []
    monkeypatch.setattr(tick, "gh_json", lambda *args: [])
    monkeypatch.setattr(tick, "gh_text", lambda *args: calls.append(args))
    real_open = builtins.open

    def fail_open(path, mode="r", *args, **kwargs):
        name = str(path)
        if (failure == "read" and name.endswith("/.pr-body.md") and "r" in mode
                or failure == "stage" and name.endswith("/.pr-create-body.md") and "w" in mode):
            raise PermissionError("injected metadata I/O failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_open)
    attempts = tick.Attempts(str(root), "issue-")
    tick._harvest_one_implementer({"pr_number": 7}, str(root), attempts=attempts)
    assert calls == []
    assert (wt / "dispatch.json").exists()
    assert (wt / ".pr-body.md").read_text() == source
    assert attempts.read(7) == 1


@pytest.mark.parametrize("closure", ["Closes #7", "- Closes #7", "1. Closes #7"])
def test_presentation_and_fixer_share_closure_contract(tmp_path, authored_report, closure):
    body = authored_report.replace("- Closes #7", closure).replace("## Summary\n", "  ## SUMMARY ###\n", 1)
    assert tick._pr_body_matches_guidelines(body, 7)
    provenance = tick._pr_body_provenance(body, truncated=False)
    replacement = body.replace("Migration deferred.", "Migration stays deferred.")
    (tmp_path / ".pr-body-revision.md").write_text(
        "pr-body-revision: fix-7\npr-body-revision-head: " + "b" * 40 + "\n" + replacement)
    state, published = tick._fixer_body_revision(
        wt_dir=str(tmp_path), dispatch_id="fix-7", current_head="b" * 40,
        provenance=provenance, live_body=body)
    assert state == "ready"
    assert published == replacement.strip()
    assert tick._pr_body_digest(body) != tick._pr_body_digest(body.replace("keep  two spaces", "keep two spaces"))


@pytest.mark.parametrize("human_edit", [False, True])
@pytest.mark.parametrize("missing_primary", [False, True])
def test_cli_create_then_failed_relabel_keeps_human_content(offline_tick, human_edit, missing_primary):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    source = "fix: authored title\n\n## Summary\nCloses #7\nAuthored explanation.\n"
    if missing_primary:
        source = source.replace("Closes #7\n", "")
    (wt / ".pr-body.md").write_text(source)
    data = json.loads(state.read_text())
    data["fail_pr_label"] = True
    state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    first_body = data["body"]
    assert first_body.count("Controller issue reference:") == missing_primary
    assert data["existing_pr"]
    assert (wt / "dispatch.json").exists()
    if human_edit:
        data["body"] = "## Summary\nCloses #7\nHuman explanation.\n"
        data["title"] = "Human title"
    data["fail_pr_label"] = False
    state.write_text(json.dumps(data))
    # Recovery must use the stored notice, never this later stale artifact.
    (wt / ".pr-body.md").write_text("stale title\n\n## Summary\nCloses #99\n")
    run_tick(root, env)
    data = json.loads(state.read_text())
    assert data["body"].count("Metadata/layout notice:") == 1
    assert data["body"].count("Verification unconfigured:") == 1
    if human_edit:
        assert data["title"] == "Human title"
        assert "Human explanation." in data["body"]
        assert "Authored explanation." not in data["body"]
    else:
        assert data["body"] == first_body
    assert not wt.exists()
    calls = [json.loads(line) for line in Path(str(state) + ".calls").read_text().splitlines()]
    assert sum(args[:2] == ["pr", "create"] for args in calls) == 1
    records = list((wt.parent / ".pr-publications").glob("*/.pr-body.md"))
    assert [path.read_text() for path in records] == [source]


def test_fixer_harvest_preserves_context_and_complete_closure_set(tmp_path, authored_report):
    from unittest import mock
    from test_tick import (fixer_harvest_scratch, fixer_harvest_marker,
                           fixer_body_provenance, fixer_revision_file, fixer_harvest_gh)
    wt_root, wt = fixer_harvest_scratch(tmp_path)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(
        pr_body=fixer_body_provenance(authored_report)))
    replacement = authored_report.replace("Migration deferred.", "Migration reviewed and deferred.")
    (wt / ".pr-body-revision.md").write_text(fixer_revision_file(replacement))
    calls, staged = [], []
    with fixer_harvest_gh(calls, staged, live_body=authored_report):
        with mock.patch("tick.cleanup_scratch"):
            tick.harvest_fixers(wt_root=str(wt_root))
    assert "pr edit" in calls
    assert any(replacement.strip() in body for _, body in staged)
    records = list((wt_root / ".pr-publications").glob("pr-*/decision.json"))
    assert len(records) == 1
    assert (records[0].parent / ".pr-body-revision.md").read_text() == fixer_revision_file(replacement)
    assert json.loads(records[0].read_text())["outcome"] == "ready"


def test_shared_prompt_example_uses_the_publication_contract():
    from test_skill_contract import _PROMPT_RENDER_VARIABLES
    contract = Path(tick._PROMPTS_DIR, "pr-body-contract.md").read_text()
    example = contract.split("```md\n", 1)[1].split("```", 1)[0].replace("#N", "#7")
    assert tick._pr_body_matches_guidelines(example, 7)
    for name in ("implementer", "fixer"):
        rendered = tick.load_prompt_template(name, _PROMPT_RENDER_VARIABLES[name])
        assert contract in rendered
        assert "exactly these `##` sections" not in rendered


def test_metadata_notice_composes_with_existing_verification_warning(tmp_path):
    import pipeline
    body = "## Summary\nCloses #7\nAuthored content.\n"
    warned = pipeline.with_verification_warning(body, required=True)
    (tmp_path / ".pr-body.md").write_text("fix: title\n\n" + warned)
    args = tick._implementation_pr_create_args(str(tmp_path), 7, verification_unconfigured=True)
    published = Path(args[args.index("--body-file") + 1]).read_text()
    assert published.count("Verification unconfigured:") == 1
    assert published.count("Metadata/layout notice:") == 1
    assert tick._with_metadata_notice(published, tick._pr_layout_notice(body)) == published


@pytest.mark.parametrize("failure", ["read", "stage", "retention"])
def test_cli_metadata_io_failure_retries_without_publication(offline_tick, tmp_path, failure):
    from test_verification_cli import write_peer
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    source = b"fix: exact title\r\n\r\n## Summary\r\nCloses #7\r\nEvidence.\r\n"
    (wt / ".pr-body.md").write_bytes(source)
    wrapper = tmp_path / "fault-python"
    write_peer(wrapper, f'''
import builtins, errno, os, runpy, sys
if sys.argv[1:2] == ["-c"]:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
real_open = builtins.open

def fail_open(path, mode="r", *args, **kwargs):
    name = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
    if ({failure!r} == "read" and name.endswith("/issue-7/.pr-body.md") and "r" in mode
            or {failure!r} == "stage" and name.endswith("/.pr-create-body.md") and "w" in mode
            or {failure!r} == "retention" and "/.pr-publications/" in name and "w" in mode):
        raise OSError(errno.EIO, "injected publication failure")
    return real_open(path, mode, *args, **kwargs)

builtins.open = fail_open
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path[0] = os.path.dirname(os.path.abspath(script))
runpy.run_path(script, run_name="__main__")
''')
    run_tick(root, {**env, "PR_PIPELINE_PYTHON": str(wrapper)})
    failed = json.loads(state.read_text())
    assert not failed.get("existing_pr")
    assert failed["labels"] == ["ai:implementing"]
    assert (wt / "dispatch.json").is_file()
    assert (wt / ".pr-body.md").read_bytes() == source
    assert (wt.parent / "issue-7-attempts").read_text().strip() == "1"
    run_tick(root, env)
    recovered = json.loads(state.read_text())
    assert recovered["title"] == "fix: exact title"
    assert recovered["body"].count("Metadata/layout notice:") == 1
    assert recovered["pr_labels"] == ["ai:implemented"]
    assert not wt.exists()


def test_cli_undecodable_report_preserves_title_without_claims(offline_tick):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    source = b"fix: recover this title\n\n\xff"
    (wt / ".pr-body.md").write_bytes(source)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live["title"] == "fix: recover this title"
    assert "No verification results are claimed" in live["body"]
    assert "completed the repository verification" not in live["body"]
    assert live["pr_labels"] == ["ai:implemented"]
    assert [p.read_bytes() for p in (wt.parent / ".pr-publications").glob("*/.pr-body.md")] == [source]


@pytest.mark.parametrize("drift", ["body", "head", "hold"])
def test_metadata_notice_guard_preserves_newer_state(tmp_path, monkeypatch, drift):
    old = {"labels": [], "headRefOid": "a" * 40, "body": "Human content.\n"}
    live = dict(old)
    if drift == "body":
        live["body"] = "Later human content.\n"
    elif drift == "head":
        live["headRefOid"] = "b" * 40
    else:
        live["labels"] = [{"name": "ai:blocked"}]
    writes = []
    monkeypatch.setattr(tick, "gh_json", lambda *args: live)
    monkeypatch.setattr(tick, "gh", lambda *args: writes.append(args))
    verdict = tick._ensure_implementation_warning(
        number=12, snapshot=old, wt_root=str(tmp_path), dispatch_id="impl-7",
        tick_log=None, verification_unconfigured=False,
        metadata_notice="> **Metadata/layout notice:** missing sections.")
    assert verdict == ("held" if drift == "hold" else "drifted")
    assert writes == []


def test_missing_publication_record_is_not_a_legacy_handoff(tmp_path):
    body = "## Summary\nCloses #7\nAuthored explanation.\n"
    (tmp_path / ".pr-body.md").write_text("fix: title\n\n" + body)
    tick._implementation_pr_create_args(str(tmp_path), 7)
    record = Path(tick._pr_publication_path(str(tmp_path), 7), "decision.json")
    record.unlink()
    with pytest.raises(OSError):
        tick._implementation_metadata_notice(str(tmp_path), 7)


def test_fixer_empty_body_without_envelope_newline_is_withheld(tmp_path):
    body = "## Summary\nCloses #7\n"
    (tmp_path / ".pr-body-revision.md").write_text(
        "pr-body-revision: fix-7\npr-body-revision-head: " + "b" * 40)
    assert tick._fixer_body_revision(
        wt_dir=str(tmp_path), dispatch_id="fix-7", current_head="b" * 40,
        provenance=tick._pr_body_provenance(body, truncated=False), live_body=body,
    ) == ("invalid", "")


def test_unrepresentable_closure_number_is_rejected_mechanically(tmp_path):
    (tmp_path / ".pr-body.md").write_text(
        "fix: report\n\n## Summary\nCloses #7\nCloses #" + "9" * 5000)
    metadata = tick._read_engine_pr_metadata(str(tmp_path), 7)
    assert metadata.outcome == "invalid"
    assert "ambiguous" in metadata.reason


def test_hash_prefixed_title_is_preserved_independently(tmp_path):
    (tmp_path / ".pr-body.md").write_text(
        "#85: preserve authored reports\n\n## Summary\nCloses #7\nDetails.\n")
    metadata = tick._read_engine_pr_metadata(str(tmp_path), 7)
    assert metadata.title == "#85: preserve authored reports"
    assert metadata.outcome == "authored"


@pytest.mark.parametrize("example", [
    "1.  Example:\n\n    ```md\n    Closes #999\n    ```\n",
    "- Outer\n  1.  Example:\n\n      ~~~md\n      Closes #999\n      ~~~~\n",
    "1. ```md\n   Closes #999\n   ```\n",
    "1.  Outer\n    - Previous sibling\n    - ```md\n      Closes #999\n      ```\n",
    "1.\tExample:\n\n\t```md\n\tCloses #999\n\t```\n",
    "1. Previous item\n 2. ```md\n    Closes #999\n    ```\n",
    "- Example:\n\n     ```md\n  Closes #999\n    ```\n",
], ids=["ordered", "nested-tilde", "marker-fence", "nested-sibling", "tabs", "shifted-sibling", "relative-indent"])
@pytest.mark.parametrize("primary", [False, True], ids=["example-only", "real-primary"])
def test_cli_list_fences_share_primary_authority(offline_tick, example, primary):
    from unittest import mock
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    body = "## Summary\n" + ("Closes #7\n" if primary else "Example only.\n") + "\n" + example
    if not primary:
        body = body.replace("#999", "#7")
    source = "fix: preserve list examples\n\n" + body
    (wt / ".pr-body.md").write_text(source)
    run_tick(root, env)
    live = json.loads(state.read_text())
    if not primary:
        assert live.get("existing_pr")
        assert live["pr_labels"] == ["ai:implemented"]
        assert body in live["body"]
        assert "Controller issue reference:" in live["body"]
        assert tick._pr_body_provenance(body, truncated=False) is None
        assert tick._primary_closures(live["body"]) == {"7"}
        return
    assert live.get("existing_pr")
    assert body in live["body"]
    assert live["pr_labels"] == ["ai:implemented"]
    with mock.patch("tick.gh_json", return_value={"body": live["body"], "closingIssuesReferences": []}):
        context = tick._fixer_pr_context(12)
    provenance = context.body_provenance
    assert provenance.summary_closes == 7
    assert provenance.closes == ("#7", "#999")
    replacement = live["body"] + "\nAuthored revision.\n"
    (root / ".pr-body-revision.md").write_text(
        "pr-body-revision: fix-7\npr-body-revision-head: " + "b" * 40 + "\n" + replacement)
    verdict, published = tick._fixer_body_revision(
        wt_dir=str(root), dispatch_id="fix-7", current_head="b" * 40,
        provenance=provenance, live_body=live["body"])
    assert verdict == "ready"
    assert body in published
    assert "Authored revision." in published
    # Moving the real primary into an example needs explicit controller authority.
    moved = replacement.replace("Closes #7", "Authored scope", 1)
    moved = re.sub(
        r"(?m)^([ \t]*)Closes #999$",
        lambda match: match[1] + "Closes #7\n" + match[1] + "Closes #999", moved,
    )
    (root / ".pr-body-revision.md").write_text(
        "pr-body-revision: fix-7\npr-body-revision-head: " + "b" * 40 + "\n" + moved)
    verdict, repaired = tick._fixer_body_revision(
        wt_dir=str(root), dispatch_id="fix-7", current_head="b" * 40,
        provenance=provenance, live_body=live["body"])
    assert verdict == "ready"
    assert "Controller issue reference:" in repaired
    assert tick._primary_closures(repaired) == {"7"}
    assert tick._declared_closures(repaired) == provenance.closes


@pytest.mark.parametrize("example", [
    "1.  Example:\n\n    ```md\n    Closes #999\n",
    "- Outer\n  - ```md\n    Closes #999\n",
    "- Example:\nlazy paragraph continuation\n\n    ```md\n    Closes #999\n    ```\n",
    "-\n  ```md\n  Closes #999\n  ```\n",
    "1.  Example:\n\n    ```md\n    ## Test plan\n    Closes #999\n    ```\n",
], ids=["dedent-closes", "nested-dedent", "lazy-continuation", "empty-item", "fenced-heading"])
def test_list_fence_boundaries_preserve_later_primary(example):
    body = "## Summary\n\n" + example + "\nCloses #7\n"
    assert tick._primary_closures(body) == {"7"}
    assert "Test plan" in tick._pr_layout_notice(body)


@pytest.mark.parametrize("repair", ["partial", "complete"])
@pytest.mark.parametrize("warning", [False, True])
def test_cli_fixer_refreshes_current_layout_notice(offline_tick, repair, warning):
    from conftest import fixer_harvest_marker, fixer_revision_file
    import pipeline
    import subprocess
    root, state, cfg, env = offline_tick
    original = "## Summary\nCloses #7\nAuthored explanation.\n"
    original = tick._with_metadata_notice(original, tick._pr_layout_notice(original))
    original = pipeline.with_verification_warning(original, required=warning)
    replacement = original + "\n## Why this approach\nKeep the controller as owner.\n"
    if repair == "complete":
        replacement += "\n## What changed\nPreserve reports.\n## Test coverage\nExact evidence.\n## Test plan\nReview it.\n"
    data = json.loads(state.read_text())
    data.update(labels=[], pr_labels=["ai:fixing"], existing_pr=True, body=original)
    state.write_text(json.dumps(data))
    wt = root / ".claude" / "worktrees" / "pr-12"
    wt.mkdir(parents=True)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(
        pid=99999999, pgid=99999999,
        pr_body=tick._pr_body_provenance(original, truncated=False).as_marker_field()))
    (wt / "completion.json").write_text(json.dumps({
        "dispatch_id": "fix-12-1", "exit_status": 0, "wrapper_pid": 99999999,
        "start_time": 1234567.0, "result_valid": True}))
    (wt / "result.json").write_text('{"result":"FIXES_PUSHED_RECHECK_NEEDED"}')
    (wt / ".pr-body-revision.md").write_text(fixer_revision_file(replacement, head=data["head"]))
    cli = Path(tick.__file__).resolve().parent.parent / "tick.sh"
    result = subprocess.run(["bash", str(cli), "pr:12"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    live = json.loads(state.read_text())
    published = live["body"]
    assert "Keep the controller as owner." in published, result.stdout + result.stderr
    assert published.count("Metadata/layout notice:") == (repair == "partial")
    assert published.count("Verification unconfigured:") == warning
    assert "sections: Why this approach" not in published
    if repair == "partial":
        assert "sections: What changed, Test coverage, Test plan." in published
    assert live["pr_labels"] == ["ai:revised"]
    records = list((wt.parent / ".pr-publications").glob("pr-*/.pr-body-revision.md"))
    assert [p.read_text() for p in records] == [fixer_revision_file(replacement, head=data["head"])]


@pytest.mark.parametrize("embedded", ["prose", "fenced", "custom-prefix", "two-prefixes"])
def test_layout_refresh_preserves_authored_notices(embedded):
    body = "## Summary\nCloses #7\n"
    old = tick._pr_layout_notice(body)
    if embedded == "prose":
        body += "\nExample of the controller notice:\n\n" + old
    elif embedded == "fenced":
        body += "\n```md\n" + old + "\n```\n"
    elif embedded == "custom-prefix":
        body = "> **Metadata/layout notice:** Authored explanation.\n\n" + body
    authored = body + "\n## Why this approach\nPreserve meaning.\n"
    prefixed = old + "\n\n" + (old + "\n\n" if embedded == "two-prefixes" else "") + authored
    current = tick._pr_layout_notice(authored)
    assert tick._with_metadata_notice(prefixed, current) == current + "\n\n" + authored


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
@pytest.mark.parametrize("repair", ["none", "partial", "complete"])
@pytest.mark.parametrize("warning", [False, True])
def test_cli_retry_refreshes_notice_after_human_repairs_sections(
    offline_tick, authored_report, newline, repair, warning,
):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    marker = json.loads((wt / "dispatch.json").read_text())
    marker["verification_unconfigured"] = warning
    (wt / "dispatch.json").write_text(json.dumps(marker))
    (wt / ".pr-body.md").write_text("fix: initial\n\n## Summary\nCloses #7\n")
    data = json.loads(state.read_text())
    data["fail_pr_label"] = True
    state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    notice = tick._pr_layout_notice("## Summary\nCloses #7\n")
    import pipeline
    repaired = "## Summary\nCloses #7\nHuman details with hard break.  \n    Indented continuation.\n"
    if repair == "partial":
        repaired += "\n## Why this approach\nPreserve authorship.\n"
    elif repair == "complete":
        repaired = authored_report
    live_body = pipeline.with_verification_warning(notice + "\n\n" + repaired, required=warning)
    data.update(body=live_body.replace("\n", newline), fail_pr_label=False, title="Human title")
    state.write_text(json.dumps(data))
    run_tick(root, env)
    live = json.loads(state.read_text())
    current = tick._pr_layout_notice(repaired)
    assert live["body"].count("Metadata/layout notice:") == bool(current)
    assert current in live["body"]
    assert live["body"].count("Verification unconfigured:") == warning
    assert repaired.rstrip("\n") in live["body"].replace("\r\n", "\n")
    assert live["title"] == "Human title"
    assert live["pr_labels"] == ["ai:implemented"]
    assert not wt.exists()


@pytest.mark.parametrize("prefix,indent", [
    ("Example paragraph\n2. ```md\n", "   "),
    ("Example paragraph\n0) ```md\n", "   "),
    ("- Example paragraph\n  2. ```md\n", "     "),
])
def test_numbered_paragraph_continuation_cannot_hide_fence(prefix, indent):
    closure_indent = "  " if prefix.startswith("-") else ""
    body = ("## Summary\n\n" + prefix + indent + "sample text\n"
            + indent + "```\n\n" + closure_indent + "Closes #7\n")
    assert tick._primary_closures(body) == set()
    assert tick._pr_body_provenance(body, truncated=False) is None


@pytest.mark.parametrize("prefix,indent", [
    ("Example paragraph\n\n2. ```md\n", "   "),
    ("1. Previous item\n2. ```md\n", "   "),
    ("1. Previous item\n   More detail\n2. ```md\n", "   "),
    ("1. Previous item\n 2. ```md\n", "    "),
])
def test_real_numbered_list_fences_keep_later_primary(prefix, indent):
    body = ("## Summary\n\n" + prefix + indent + "Closes #999\n"
            + indent + "```\n\nCloses #7\n")
    assert tick._primary_closures(body) == {"7"}


@pytest.mark.parametrize("body", [
    "Detailed implementation and evidence.\n",
    "## Summary\n\n    Closes #7\n",
    "# Summary\nCloses #7\nDetails without template headings.\n",
    "## Summary\n\nExample paragraph\n2. ```md\n   sample text\n   ```\n\nCloses #7\n",
])
def test_cli_supplies_target_without_discarding_formatted_report(offline_tick, body):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    source = "fix: detailed report\n\n" + body
    (wt / ".pr-body.md").write_text(source)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live.get("existing_pr"), live
    assert live["title"] == "fix: detailed report"
    assert body in live["body"]
    assert "Controller issue reference:" in live["body"]
    assert tick._primary_closures(live["body"]) == {"7"}
    assert live["pr_labels"] == ["ai:implemented"]
    assert live["labels"] == []
    assert [p.read_text() for p in (wt.parent / ".pr-publications").glob("*/.pr-body.md")] == [source]
    records = list((wt.parent / ".pr-publications").glob("*/decision.json"))
    assert json.loads(records[0].read_text())["controller_reference"] is True


@pytest.mark.parametrize("replacement", [
    "## Summary\n\nExample paragraph\n2. ```md\n   sample text\n   ```\n\nCloses #7\n",
    "## Summary\n\n    Closes #7\n",
])
def test_cli_fixer_supplies_pinned_primary_after_formatting_change(offline_tick, replacement):
    import subprocess
    from conftest import fixer_harvest_marker, fixer_revision_file
    root, state, cfg, env = offline_tick
    original = "## Summary\nCloses #7\nOriginal details.\n"
    data = json.loads(state.read_text())
    data.update(labels=[], pr_labels=["ai:fixing"], existing_pr=True, body=original)
    state.write_text(json.dumps(data))
    wt = root / ".claude" / "worktrees" / "pr-12"
    wt.mkdir(parents=True)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(
        pid=99999999, pgid=99999999,
        pr_body=tick._pr_body_provenance(original, truncated=False).as_marker_field()))
    (wt / "completion.json").write_text(json.dumps({
        "dispatch_id": "fix-12-1", "exit_status": 0, "wrapper_pid": 99999999,
        "start_time": 1234567.0, "result_valid": True}))
    (wt / "result.json").write_text('{"result":"FIXES_PUSHED_RECHECK_NEEDED"}')
    (wt / ".pr-body-revision.md").write_text(fixer_revision_file(replacement, head=data["head"]))
    cli = Path(tick.__file__).resolve().parent.parent / "tick.sh"
    result = subprocess.run(["bash", str(cli), "pr:12"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    live = json.loads(state.read_text())
    assert "Controller issue reference:" in live["body"], live
    assert replacement.strip() in live["body"]
    assert tick._primary_closures(live["body"]) == {"7"}
    assert tick._declared_closures(live["body"]) == tick._declared_closures(original)
    assert live["pr_labels"] == ["ai:revised"]


@pytest.mark.parametrize("body", ["# Summary\nCloses #8\n", "Plain prose.\nCloses #7\nCloses #8\n"])
def test_misplaced_conflicting_primary_is_still_invalid(tmp_path, body):
    (tmp_path / ".pr-body.md").write_text("fix: report\n\n" + body)
    metadata = tick._read_engine_pr_metadata(str(tmp_path), 7)
    assert metadata.outcome == "invalid"
    assert "conflicting" in metadata.reason


@pytest.mark.parametrize("paragraph", [
    "`Example paragraph`", "#85 is the issue", "~Approximate example",
    "``inline code``", "####### not a heading",
])
@pytest.mark.parametrize("primary", [False, True], ids=["hidden-primary", "fenced-conflict"])
def test_cli_inline_paragraph_fences_preserve_reference_authority(offline_tick, paragraph, primary):
    from conftest import fixer_harvest_marker, fixer_revision_file
    import subprocess
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    body = ("## Summary\n" + ("Closes #7\n" if primary else "") + "\n"
            + paragraph + "\n2. ```md\n   sample text\n   ```\n\n"
            + ("Closes #999\n" if primary else "Closes #7\n"))
    expected = {"7"} if primary else set()
    source = "fix: retain authored context\n\n" + body
    (wt / ".pr-body.md").write_text(source)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live.get("existing_pr"), live
    assert body in live["body"]
    assert ("Controller issue reference:" in live["body"]) != primary
    assert tick._primary_closures(body) == expected
    assert bool(tick._pr_body_provenance(body, truncated=False)) == primary
    assert tick._primary_closures(live["body"]) == {"7"}
    assert live["pr_labels"] == ["ai:implemented"]
    assert [p.read_text() for p in (wt.parent / ".pr-publications").glob("*/.pr-body.md")] == [source]
    # Pin an independently visible report with the same complete directive set.
    original = "## Summary\nCloses #7\nOriginal details.\n"
    if primary:
        original += "\n```md\nCloses #999\n```\n"
    live.update(labels=[], pr_labels=["ai:fixing"], body=original)
    state.write_text(json.dumps(live))
    fixer = root / ".claude" / "worktrees" / "pr-12"
    fixer.mkdir()
    pinned = tick._pr_body_provenance(original, truncated=False)
    (fixer / "dispatch.json").write_text(fixer_harvest_marker(
        pid=99999999, pgid=99999999, pr_body=pinned.as_marker_field()))
    (fixer / "completion.json").write_text(json.dumps({
        "dispatch_id": "fix-12-1", "exit_status": 0, "wrapper_pid": 99999999,
        "start_time": 1234567.0, "result_valid": True}))
    (fixer / "result.json").write_text('{"result":"FIXES_PUSHED_RECHECK_NEEDED"}')
    (fixer / ".pr-body-revision.md").write_text(fixer_revision_file(body, head=live["head"]))
    cli = Path(tick.__file__).resolve().parent.parent / "tick.sh"
    result = subprocess.run(["bash", str(cli), "pr:12"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    revised = json.loads(state.read_text())
    assert body.rstrip("\n") in revised["body"], result.stdout + result.stderr
    assert ("Controller issue reference:" in revised["body"]) != primary
    assert tick._primary_closures(revised["body"]) == {"7"}
    assert tick._declared_closures(revised["body"]) == pinned.closes
    assert revised["pr_labels"] == ["ai:revised"]


@pytest.mark.parametrize("block", ["***", "___", "- - -", "Paragraph\n---", "Paragraph\n==="])
def test_real_block_boundary_allows_following_ordered_list(block):
    body = ("## Summary\n\n" + block
            + "\n2. ```md\n   sample text\n   ```\n\nCloses #7\n")
    assert tick._primary_closures(body) == {"7"}


@pytest.mark.parametrize("prefix", [
    "    ", "\t", "-     ", "1. Previous item\n2.     ", "- Previous item\n-     ",
])
def test_indented_code_does_not_supply_primary_reference(prefix):
    body = "## Summary\n\n" + prefix + "Closes #7\n"
    assert tick._primary_closures(body) == set()
    assert tick._pr_body_provenance(body, truncated=False) is None


@pytest.mark.parametrize("prefix", ["Paragraph\n", "- Paragraph\n  "])
def test_indentation_cannot_interrupt_existing_paragraph(prefix):
    body = "## Summary\n\n" + prefix + "    Closes #7\n"
    assert tick._primary_closures(body) == {"7"}


@pytest.mark.parametrize("primary", [False, True], ids=["hidden-primary", "fenced-conflict"])
def test_cli_list_transitions_use_visible_primary_only(offline_tick, list_transition_example, primary):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    example = list_transition_example if primary else list_transition_example.replace("#999", "#7")
    body = "## Summary\n" + ("Closes #7\n" if primary else "") + "\n" + example
    (wt / ".pr-body.md").write_text("fix: review probe\n\n" + body)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live.get("existing_pr"), live
    assert body in live["body"]
    assert ("Controller issue reference:" in live["body"]) == (not primary), live["body"]
    assert live["pr_labels"] == ["ai:implemented"]
    assert tick._primary_closures(body) == ({"7"} if primary else set())
    assert bool(tick._pr_body_provenance(body, truncated=False)) == primary
    assert tick._primary_closures(live["body"]) == {"7"}
    records = list((wt.parent / ".pr-publications").glob("*/.pr-body.md"))
    assert [p.read_text() for p in records] == ["fix: review probe\n\n" + body]


@pytest.mark.parametrize("primary", [False, True], ids=["hidden-primary", "fenced-conflict"])
def test_cli_fixer_list_transitions_preserve_authority(offline_tick, list_transition_example, primary):
    from conftest import fixer_harvest_marker, fixer_revision_file
    import subprocess
    root, state, cfg, env = offline_tick
    original = "## Summary\nCloses #7\nOriginal report.\n"
    if primary:
        original += "\n```md\nCloses #999\n```\n"
    example = list_transition_example if primary else list_transition_example.replace("#999", "#7")
    replacement = "## Summary\n" + ("Closes #7\n" if primary else "") + "Revised report.\n\n" + example
    data = json.loads(state.read_text())
    data.update(labels=[], pr_labels=["ai:fixing"], existing_pr=True, body=original)
    state.write_text(json.dumps(data))
    wt = root / ".claude" / "worktrees" / "pr-12"
    wt.mkdir(parents=True)
    provenance = tick._pr_body_provenance(original, truncated=False)
    assert tick._declared_closures(original) == tick._declared_closures(replacement)
    (wt / "dispatch.json").write_text(fixer_harvest_marker(
        pid=99999999, pgid=99999999, pr_body=provenance.as_marker_field()))
    (wt / "completion.json").write_text(json.dumps({
        "dispatch_id": "fix-12-1", "exit_status": 0, "wrapper_pid": 99999999,
        "start_time": 1234567.0, "result_valid": True}))
    (wt / "result.json").write_text('{"result":"FIXES_PUSHED_RECHECK_NEEDED"}')
    (wt / ".pr-body-revision.md").write_text(fixer_revision_file(replacement, head=data["head"]))
    cli = Path(tick.__file__).resolve().parent.parent / "tick.sh"
    result = subprocess.run(["bash", str(cli), "pr:12"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    live = json.loads(state.read_text())
    assert replacement.rstrip("\n") in live["body"], live
    assert ("Controller issue reference:" in live["body"]) == (not primary)
    assert tick._primary_closures(live["body"]) == {"7"}
    assert tick._declared_closures(live["body"]) == provenance.closes
    assert live["pr_labels"] == ["ai:revised"]
    records = list((wt.parent / ".pr-publications").glob("pr-*/.pr-body-revision.md"))
    assert [p.read_text() for p in records] == [fixer_revision_file(replacement, head=data["head"])]


# --- Helpers ---


def completed_implementer(root, env):
    run_tick(root, env)
    wt = root / ".claude" / "worktrees" / "issue-7"
    for _ in range(100):
        if (wt / "result.json").exists() and (wt / "result.json").stat().st_size:
            return wt
        time.sleep(0.02)
    raise AssertionError("offline implementer did not finish")
