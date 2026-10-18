"""Implementation evidence notices through the operator's harvest command."""

import json
from pathlib import Path
import subprocess

import pytest

import tick
from test_pr_metadata import completed_implementer
from test_verification_cli import offline_tick, run_tick


@pytest.fixture
def evidence():
    return (
        "Phase A: declared before first edit\n"
        "Proved at: uncommitted, baseline abc123\n"
        "Entry point: bash skills/tick/tick.sh '#7' in the target checkout\n"
        "Boundary swept: _implementation_pr_create_args and handoff recovery\n"
        "Disproof (static): publication callers and shared report contract\n"
        "Disproof (execution): offline GitHub rejects the first body write\n"
        "Scope rechecked: only publication evidence notices changed\n"
    )


@pytest.fixture(params=["fields", "exemption", "heading", "inline-values"])
def hidden_coverage(request, evidence):
    exemption = "Definition of done: n/a — documentation only\n"
    if request.param == "heading":
        return "<!--\n## Test coverage\n" + exemption + "-->\n"
    if request.param == "inline-values":
        return "## Test coverage\n" + "\n".join(
            label + ": <!--" + value + "-->"
            for label, value in (line.split(":", 1) for line in evidence.splitlines())
        ) + "\n"
    content = evidence if request.param == "fields" else exemption
    return "## Test coverage\n<!--\n" + content + "-->\n"


def test_cli_hidden_evidence_cannot_suppress_notice(offline_tick, hidden_coverage):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    body = "## Summary\nCloses #7\n\n" + hidden_coverage
    (wt / ".pr-body.md").write_text("fix: hidden evidence\n\n" + body)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live["body"].count("Implementation evidence missing:") == 1
    assert body in live["body"]
    assert live["pr_labels"] == ["ai:implemented"]


def test_cli_hidden_repair_keeps_notice_on_retry(offline_tick, hidden_coverage):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    (wt / ".pr-body.md").write_text("fix: missing evidence\n\n## Summary\nCloses #7\n")
    data = json.loads(state.read_text())
    data["fail_pr_label"] = True
    state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    assert data["body"].count("Implementation evidence missing:") == 1
    assert (wt / "dispatch.json").exists()
    data["body"] += "\n" + hidden_coverage
    data["fail_pr_label"] = False
    state.write_text(json.dumps(data))
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live["body"].count("Implementation evidence missing:") == 1
    assert hidden_coverage in live["body"]
    assert live["pr_labels"] == ["ai:implemented"]
    assert not wt.exists()


@pytest.mark.parametrize("source", [
    None,
    b"fix: empty report\n\n",
    b"fix: undecodable report\n\n\xff",
    b"fix: authored report\n\n## Summary\nCloses #7\n\n"
    b"## Why this approach\nKeep ownership.\n\n## What changed\nReports.\n\n"
    b"## Test coverage\nTests passed.\n\n## Test plan\nReview.\n",
], ids=["absent", "empty", "undecodable", "missing-evidence"])
def test_cli_missing_implementation_evidence_is_visible(offline_tick, source):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    if source is not None:
        (wt / ".pr-body.md").write_bytes(source)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live["body"].count("Implementation evidence missing:") == 1
    assert live["body"].count("Verification unconfigured:") == 1
    assert live["pr_labels"] == ["ai:implemented"]
    assert live["labels"] == []
    if source and b"Tests passed." in source:
        assert source.decode().split("\n\n", 1)[1] in live["body"]


@pytest.mark.parametrize("form", [
    "plain", "fenced", "bullets", "bold", "exemption",
    "fenced-comments", "indented-comments", "list-fenced-comments",
])
def test_cli_complete_evidence_has_no_notice(offline_tick, evidence, form):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    if form == "fenced":
        evidence = "```text\n" + evidence + "```\n"
    elif form == "bullets":
        evidence = "\n".join("- " + line for line in evidence.splitlines())
    elif form == "bold":
        evidence = "\n".join("**" + line.replace(":", ":**", 1) for line in evidence.splitlines())
    elif form == "exemption":
        evidence = "Definition of done: n/a — documentation wording only.\n"
    elif form.endswith("comments"):
        evidence = "<!--\n" + evidence + "-->\n"
        if form == "indented-comments":
            evidence = "\n" + "\n".join("    " + line for line in evidence.splitlines()) + "\n"
        elif form == "list-fenced-comments":
            evidence = "- ```text\n" + "\n".join(
                "  " + line for line in evidence.splitlines()
            ) + "\n  ```\n"
        else:
            evidence = "```text\n" + evidence + "```\n"
    body = "## Summary\nCloses #7\n\n  ## TEST COVERAGE ###\n" + evidence
    (wt / ".pr-body.md").write_text("fix: authored proof\n\n" + body)
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert "Implementation evidence missing:" not in live["body"]
    assert body in live["body"]
    assert live["pr_labels"] == ["ai:implemented"]


@pytest.mark.parametrize("missing", [
    "Phase A", "Proved at", "Entry point", "Boundary swept",
    "Disproof (static)", "Disproof (execution)", "Scope rechecked",
])
def test_each_required_field_needs_content(evidence, missing):
    evidence = "\n".join(
        line.split(":", 1)[0] + ":" if line.startswith(missing + ":") else line
        for line in evidence.splitlines()
    )
    body = "## Test coverage\n" + evidence
    assert "Implementation evidence missing:" in tick._with_implementation_evidence_notice(body)


@pytest.mark.parametrize("form", ["wrong-section", "fake-heading", "empty-exemption"])
def test_evidence_must_be_under_real_test_coverage(evidence, form):
    if form == "wrong-section":
        body = "## Test coverage\nNo proof.\n\n## Test plan\n" + evidence
    elif form == "fake-heading":
        body = "## Summary\n```md\n## Test coverage\n" + evidence + "```\n"
    else:
        body = "## Test coverage\nDefinition of done: n/a — \n"
    assert "Implementation evidence missing:" in tick._with_implementation_evidence_notice(body)


@pytest.mark.parametrize("hidden", [
    "<!--\n## Test plan\n-->\n",
    "<!--\n```\n-->\n",
    "<!-- comment -->\n<!-- another comment -->\n",
])
def test_hidden_markup_does_not_change_visible_evidence_section(evidence, hidden):
    body = "## Test coverage\n" + hidden + evidence
    assert tick._with_implementation_evidence_notice(body) == body


@pytest.mark.parametrize("value", [
    "`<!-- literal -->`", "``<!-- literal ` -->``", r"\<!-- literal",
    "visible <!-- hidden --> remainder",
])
def test_visible_exemption_reason_survives_comment_filter(value):
    body = "## Test coverage\nDefinition of done: n/a — " + value + "\n"
    assert tick._with_implementation_evidence_notice(body) == body


@pytest.mark.parametrize("ending", ["-->\n", ""])
def test_hidden_exemption_reason_is_missing(ending):
    body = "## Test coverage\nDefinition of done: n/a — <!-- hidden\n" + ending
    assert "Implementation evidence missing:" in tick._with_implementation_evidence_notice(body)


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("verification", [False, True])
def test_cli_retry_refreshes_live_evidence_without_duplicates(offline_tick, evidence, repair, verification):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    marker = json.loads((wt / "dispatch.json").read_text())
    marker["verification_unconfigured"] = verification
    (wt / "dispatch.json").write_text(json.dumps(marker))
    (wt / ".pr-body.md").write_text("fix: initial\n\n## Summary\nCloses #7\n")
    data = json.loads(state.read_text())
    data["fail_pr_label"] = True
    state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    assert data["body"].count("Implementation evidence missing:") == 1
    assert (wt / "dispatch.json").exists()
    data["body"] += "\n## Test coverage\n" + (evidence if repair else "New human detail.  \n")
    data["body"] = data["body"].replace("\n", "\r\n")
    data.update(title="Human title", fail_pr_label=False)
    state.write_text(json.dumps(data))
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live["body"].count("Implementation evidence missing:") == (not repair)
    assert live["body"].count("Verification unconfigured:") == verification
    assert (evidence.rstrip() if repair else "New human detail.") in live["body"]
    assert live["title"] == "Human title"
    assert live["pr_labels"] == ["ai:implemented"]
    assert not wt.exists()


def test_cli_legacy_existing_pr_gets_notice_without_stale_report(offline_tick):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    marker = json.loads((wt / "dispatch.json").read_text())
    marker["verification_unconfigured"] = False
    (wt / "dispatch.json").write_text(json.dumps(marker))
    (wt / ".pr-body.md").write_text("fix: stale\n\nStale engine report.")
    body = "Human report with hard break.  \n    Indented continuation.\n<!--"
    data = json.loads(state.read_text())
    data.update(existing_pr=True, body=body, title="Human title", pr_labels=["ai:changes-requested"])
    state.write_text(json.dumps(data))
    run_tick(root, env)
    live = json.loads(state.read_text())
    assert live["body"].startswith("> **Implementation evidence missing:")
    assert body in live["body"]
    assert "Stale engine report" not in live["body"]
    assert live["title"] == "Human title"
    assert live["pr_labels"] == ["ai:changes-requested"]
    assert live["labels"] == []


@pytest.mark.parametrize("failure", ["write", "crash", "body", "head", "hold", "labels", "read"])
def test_cli_existing_notice_failure_retains_custody(offline_tick, failure):
    root, state, cfg, env = offline_tick
    wt = completed_implementer(root, env)
    marker = json.loads((wt / "dispatch.json").read_text())
    marker["verification_unconfigured"] = False
    (wt / "dispatch.json").write_text(json.dumps(marker))
    data = json.loads(state.read_text())
    data.update(existing_pr=True, body="Current human report.", fault=failure)
    state.write_text(json.dumps(data))
    inject_github_faults(env)
    cli = Path(tick.__file__).resolve().parent.parent / "tick.sh"
    result = subprocess.run(["bash", str(cli), "#7"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == (-9 if failure == "crash" else 0), result.stdout + result.stderr
    live = json.loads(state.read_text())
    assert (wt / "dispatch.json").exists()
    assert (wt / "result.json").exists()
    assert live["labels"] == ["ai:implementing"]
    if failure != "crash":
        assert "Implementation evidence missing:" not in live["body"]
    # Resume from the fresh report, including any human edit or landed write.
    live.update(fault="", pr_labels=[])
    state.write_text(json.dumps(live))
    run_tick(root, env)
    resumed = json.loads(state.read_text())
    assert resumed["body"].count("Implementation evidence missing:") == 1
    assert ("Later human report." if failure == "body" else "Current human report.") in resumed["body"]
    assert resumed["pr_labels"] == ["ai:implemented"]
    assert not wt.exists()


@pytest.mark.parametrize("repair", ["missing", "visible", "hidden"])
def test_cli_fixer_refreshes_existing_evidence_notice(offline_tick, evidence, repair):
    from conftest import fixer_harvest_marker, fixer_revision_file

    root, state, cfg, env = offline_tick
    original = tick._with_implementation_evidence_notice("## Summary\nCloses #7\nInitial report.\n")
    content = {
        "missing": "Partial proof.\n", "visible": evidence,
        "hidden": "<!--\n" + evidence + "-->\n",
    }[repair]
    replacement = original + "\n## Test coverage\n" + content
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
    assert live["body"].count("Implementation evidence missing:") == (repair != "visible")
    assert live["pr_labels"] == ["ai:revised"]


def test_notice_examples_remain_authored_and_do_not_suppress_visible_warning():
    warning = tick._with_implementation_evidence_notice("").strip()
    body = "## Summary\nCloses #7\n\n<!--\n" + warning
    published = tick._with_implementation_evidence_notice(body)
    assert published == warning + "\n\n" + body
    assert tick._with_implementation_evidence_notice(published) == published


def test_notice_is_included_in_publication_size_limit(tmp_path):
    body = "## Summary\nCloses #7\n## Why this approach\nDetails.\n## What changed\nDetails.\n## Test coverage\nMissing.\n## Test plan\nReview.\n"
    body += "x" * (65536 - len(body))
    (tmp_path / ".pr-body.md").write_text("fix: large report\n\n" + body)
    with pytest.raises(tick.PRMetadataInvalid, match="publication limit"):
        tick._implementation_pr_create_args(str(tmp_path), 7)


# --- Helpers ---


def inject_github_faults(env):
    peer = Path(env["PATH"].split(":")[0]) / "gh"
    source = peer.read_text()
    source = source.replace('elif args[:2] == ["pr", "view"]:\n', '''elif args[:2] == ["pr", "view"]:
    s["pr_reads"] = s.get("pr_reads", 0) + 1
    if s["pr_reads"] == 2:
        fault = s.get("fault")
        if fault == "body": s["body"] = "Later human report."
        if fault == "head": s["head"] = "b" * 40
        if fault == "hold": s["pr_labels"] = ["ai:blocked"]
        if fault == "labels": s["pr_labels"] = ["ai:reviewing"]
        if fault == "read":
            print("{}"); raise SystemExit
    path.write_text(json.dumps(s))
''')
    source = source.replace('    if "--body-file" in args:\n', '''    if "--body-file" in args:
        if s.get("fault") == "write": raise SystemExit(1)
''')
    source = source.replace('    payload = {}\nelif args[0] == "api"', '''    if "--body-file" in args and s.get("fault") == "crash":
        import signal
        os.kill(os.getppid(), signal.SIGKILL)
    payload = {}
elif args[0] == "api"''')
    peer.write_text(source)
