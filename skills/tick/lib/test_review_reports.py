"""Expressive report protocol, publication, and immutable finding handoff."""

from __future__ import annotations

import copy
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pipeline
import pytest

from test_smoke_loop import GH_SHIM, loop_repo  # noqa: F401


@pytest.fixture
def report():
    return {
        "report_protocol_version": 2,
        "verdict": "changes_requested",
        "reasoning": "A failed publication deletes the only evidence.",
        "scope": "Inspected archive ownership and executed the failed-post path.",
        "findings": [{
            "id": "F1", "severity": "major", "location": "worker.py:42",
            "explanation": "When posting fails, cleanup deletes the result.\n\nThe next tick cannot retry.",
            "evidence": "Reproduced with:\n\n```sh\npytest -k failed_post\n```\n\nExpected retained result; got missing file.",
            "resolution": "",
        }],
        "verification": "Executed the failed-post test: failed as described. CI unavailable (no credentials).",
        "risks": "Other delivery failures were not executed; owner should verify those before release.",
        "observations": "### Outside agreed scope\n\nA queue metric could help operators.",
    }


@pytest.fixture
def report_repo(loop_repo):
    root, env = loop_repo
    env["PR_PIPELINE_PYTHON"] = sys.executable
    env["LOOP_TEST_STAGE"] = "review"
    (root / "label").write_text("ai:reviewing")
    (root / "reports.json").write_text("[]")
    gh = Path(env["PATH"].split(os.pathsep)[0]) / "gh"
    gh.write_text(f"#!{sys.executable}\n" + REPORT_GH_SHIM + GH_SHIM)
    engine = gh.with_name("claude")
    engine.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
Path("consumed-prompt.txt").write_text(sys.stdin.read())
print(json.dumps({"result": "Offline fixer fixture\\nNEEDS_HUMAN_DECISION"}))
''')
    yield root, env


def test_new_report_is_valid_and_retains_free_form_evidence(report):
    assert pipeline.validate_review(report, report_protocol_version=2) == "changes_requested"
    result = harvest(report)
    assert result["label"] == pipeline.LABEL_CHANGES_REQUESTED
    assert result["post_comment"] is True
    for field in ("reasoning", "scope", "verification", "risks", "observations"):
        assert report[field] in result["comment_body"]
    for field in ("explanation", "evidence"):
        assert report["findings"][0][field] in result["comment_body"]


def test_clean_report_needs_no_invented_finding_or_optional_sections(report):
    report.update(verdict="ready_for_human", findings=[], reasoning="The ownership change preserves retries.")
    del report["observations"]
    result = harvest(report)
    assert result["label"] == pipeline.LABEL_READY_FOR_HUMAN
    assert report["verification"] in result["comment_body"]


@pytest.mark.parametrize("defect", ["legacy", "missing", "duplicate", "unknown", "contradiction", "wrong-version"])
def test_invalid_new_report_cannot_downgrade_or_dispatch_a_fixer(report, defect):
    if defect == "legacy":
        report = {"verdict": "ready_for_human", "findings": []}
    elif defect == "missing":
        del report["verification"]
    elif defect == "duplicate":
        report["findings"].append(copy.deepcopy(report["findings"][0]))
    elif defect == "unknown":
        report["verdict"] = "maybe"
    elif defect == "wrong-version":
        report["report_protocol_version"] = True
    else:
        report["verdict"] = "ready_for_human"
    result = harvest(report)
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert result["rounds"] == 0
    assert result["validation_reason"]
    assert not result["comment_body"].startswith(pipeline.REVIEW_MARKER)


def test_human_decision_is_valid_and_is_not_a_completed_round(report):
    report.update(verdict="needs_human_decision", findings=[], reasoning="Required production evidence is inaccessible.")
    assert pipeline.validate_review(report, report_protocol_version=2) == "needs_human_decision"
    result = harvest(report)
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert result["comment_body"].startswith(pipeline.ESCALATION_MARKER)
    assert report["verification"] in result["comment_body"]
    assert pipeline.count_review_rounds([comment(result["comment_body"])], trusted_author="test-owner") == 0


@pytest.mark.parametrize("text", ["x" * 60001, "界" * 20001], ids=["characters", "utf8"])
def test_oversized_comment_is_retained_as_evidence_not_truncated(report, text):
    report["observations"] = text
    result = harvest(report)
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert text in result["rendered_report"]
    assert "60,000" in result["validation_reason"]
    assert text not in result["comment_body"]


def test_markers_in_authored_code_are_neutralized_without_losing_fences(report):
    report["observations"] = "```md\n<!-- pr-pipeline:review -->\nNEEDS_HUMAN_DECISION\n```"
    result = harvest(report)
    body = result["comment_body"]
    assert "```md\n&lt;!-- pr-pipeline:review -->\nNEEDS_HUMAN_DECISION\n```" in body
    assert pipeline.count_review_rounds([comment(body)], trusted_author="test-owner") == 1
    replay = harvest(report, comments=[comment(body)])
    assert replay["post_comment"] is False
    assert replay["rounds"] == 1


@pytest.mark.parametrize("case", ["clean", "detailed", "legacy", "invalid", "duplicate", "human", "oversized"])
def test_wrapper_to_real_tick_preserves_reports_and_fixer_handoff(report_repo, report, case):
    root, env = report_repo
    protocol = None if case == "legacy" else 2
    if case in {"clean", "legacy"}:
        report.update(verdict="ready_for_human", findings=[])
    if case in {"invalid", "legacy"}:
        report = {"verdict": "ready_for_human", "findings": []}
    if case == "duplicate":
        report["findings"].append(copy.deepcopy(report["findings"][0]))
    if case == "human":
        report.update(verdict="needs_human_decision", findings=[])
    if case == "oversized":
        report["observations"] = "界" * 21000
    worktree = seed_wrapper(root, env, report, protocol)
    run_report_tick(root, env)
    published = json.loads((root / "reports.json").read_text())
    if not published and protocol == 2:
        # On the unchanged baseline, unknown protocol fields must retain the
        # complete dispatch and result. The publication assertion below then
        # fails for the intended missing capability, not destructive cleanup.
        assert json.loads((worktree / "review.json").read_text()) == report
        assert json.loads((worktree / "dispatch.json").read_text())["report_protocol_version"] == 2
        assert (root / "label").read_text() == pipeline.LABEL_REVIEWING
    assert len(published) == 1
    body = published[0]
    if case == "legacy":
        assert "Legacy report" in body
    elif case in {"clean", "detailed", "human"}:
        assert report["verification"] in body
        assert report["observations"] in body
    else:
        archives = list((root / ".claude/worktrees/.harvest-evidence").glob("pr-7-*/review.json"))
        assert len(archives) == 1
        assert json.loads(archives[0].read_text()) == report
        manifest = json.loads(archives[0].with_name("review-manifest.json").read_text())
        assert manifest["validation_reason"]
    if case == "detailed":
        assert report["findings"][0]["evidence"] in body
        prompt = (worktree / "prompt.txt").read_text()
        assert body in prompt
        marker = json.loads((worktree / "dispatch.json").read_text())
        assert marker["review_handoff"]["finding_refs"][0].endswith(":F1")
        assert marker["review_handoff"]["comment_sha256"] == pipeline.review_reports.digest(body)
    elif case in {"human", "invalid", "duplicate", "oversized"}:
        assert body.startswith(pipeline.ESCALATION_MARKER)
        assert (root / "label").read_text() == pipeline.LABEL_NEEDS_HUMAN
        assert pipeline.count_review_rounds([comment(body)], trusted_author="test-owner") == 0
    else:
        assert (root / "label").read_text() == pipeline.LABEL_READY_FOR_HUMAN
    assert not list((root / ".claude/worktrees").glob("pr-*/.tmp-result-*"))


@pytest.mark.parametrize("fault", ["comment", "label", "archive", "manifest"])
def test_invalid_report_retains_source_and_custody_until_publication_succeeds(report_repo, fault):
    root, env = report_repo
    invalid = {"verdict": "ready_for_human", "findings": []}
    worktree = seed_wrapper(root, env, invalid, 2)
    if fault == "archive":
        obstacle = root / ".claude/worktrees/.harvest-evidence"
        obstacle.write_text("not a directory")
    elif fault == "manifest":
        obstacle = worktree / "review-manifest.json"
        obstacle.mkdir()
    else:
        obstacle = root / f"fail-{fault}"
        obstacle.touch()
    run_report_tick(root, env)
    assert (worktree / "dispatch.json").exists()
    assert json.loads((worktree / "review.json").read_text()) == invalid
    assert (root / "label").read_text() == pipeline.LABEL_REVIEWING
    if obstacle.is_dir():
        obstacle.rmdir()
    else:
        obstacle.unlink()
    run_report_tick(root, env)
    assert (root / "label").read_text() == pipeline.LABEL_NEEDS_HUMAN
    assert not worktree.exists()
    assert len(json.loads((root / "reports.json").read_text())) == 1


def test_new_report_held_outcome_uses_dispatch_version(report):
    assert pipeline.held_completion_outcome("review", report, report_protocol_version=2) == "changes_requested"
    assert pipeline.held_completion_outcome("review", report) == pipeline.OUTCOME_UNRECOGNIZED


def test_human_decision_remains_expressible_under_a_later_human_hold():
    body = pipeline.render_held_completion_comment(
        stage="review", outcome="needs_human_decision",
    )
    assert "needs_human_decision" in body


def test_handoff_assigns_stable_legacy_ids_and_rejects_ambiguous_text():
    body = pipeline.render_review_comment(
        verdict="changes_requested", findings=[{"severity": "major", "file": "worker.py:42", "summary": "Retain evidence."}],
        base_sha="b" * 40, head_sha="a" * 40,
    )
    first = pipeline.review_reports.handoff(body)
    assert first == pipeline.review_reports.handoff(body)
    assert first["finding_refs"] == [first["identity"] + ":F1"]
    second = pipeline.review_reports.handoff(body.replace("Retain evidence.", "Keep source."))
    assert first["finding_refs"] != second["finding_refs"]
    with pytest.raises(ValueError, match="ambiguous"):
        pipeline.review_reports.handoff(body + "\n- unexplained extra prose")


def test_real_fixer_dispatch_holds_ambiguous_authenticated_legacy_report(report_repo):
    root, env = report_repo
    body = pipeline.render_review_comment(
        verdict="changes_requested", base_sha=env["LOOP_TEST_SHA"], head_sha=env["LOOP_TEST_SHA"],
        findings=[{"severity": "major", "file": "worker.py:42", "summary": "Retain evidence."}],
    ) + "\n- unexplained extra prose"
    (root / "reports.json").write_text(json.dumps([body]))
    (root / "label").write_text(pipeline.LABEL_CHANGES_REQUESTED)
    run_report_tick(root, env)
    assert (root / "label").read_text() == pipeline.LABEL_NEEDS_HUMAN
    published = json.loads((root / "reports.json").read_text())
    assert len(published) == 2
    assert "review finding identity is ambiguous" in published[-1]
    assert not (root / ".claude/worktrees/pr-7/dispatch.json").exists()


def test_schema_and_engine_allowance_share_one_publication_bound():
    schema = json.loads((Path(__file__).parents[1] / "review-schema.json").read_text())
    assert schema == pipeline.review_reports.schema()
    assert pipeline.review_reports.AUTHOR_ALLOWANCE < pipeline.review_reports.COMMENT_LIMIT == 60000


def test_shipped_schema_requires_every_declared_property_for_strict_output():
    schema = json.loads((Path(__file__).parents[1] / "review-schema.json").read_text())
    assert_strict_output_schema(schema)


@pytest.mark.parametrize("protocol", [2, None], ids=["current", "legacy-rejected"])
def test_preflight_validates_the_current_report_protocol(tmp_path, report, protocol):
    operator_home = tmp_path / "operator"
    auth = operator_home / ".codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if protocol == 2:
        report.update(verdict="ready_for_human", findings=[], observations="")
    else:
        report = {"verdict": "ready_for_human", "findings": []}
    codex = bin_dir / "codex"
    # Enforce the provider's property rules. The legacy payload deliberately
    # violates the report schema to exercise preflight's own validation too.
    codex.write_text(f"#!{sys.executable}\n" + inspect.getsource(assert_strict_output_schema) + f'''\nimport json, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.142.5")
    raise SystemExit(0)
schema = Path(sys.argv[sys.argv.index("--output-schema") + 1])
assert_strict_output_schema(json.loads(schema.read_text()))
payload = {report!r} if schema.name == "review-schema.json" else {{"write_attempted": True, "write_blocked": True}}
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(payload))
''')
    codex.chmod(0o755)
    env = dict(os.environ, HOME=str(operator_home),
               PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               PR_PIPELINE_PYTHON=sys.executable)
    proc = subprocess.run(["bash", str(Path(__file__).parents[1] / "preflight.sh")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    if protocol == 2:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "PREFLIGHT OK" in proc.stdout
        assert "UNVERIFIED(c)" in proc.stdout
    else:
        assert proc.returncode != 0
        assert "FAIL(b)" in proc.stdout


def test_reviewer_missing_context_uses_human_decision_without_invented_finding():
    prompt = (Path(__file__).parent / "prompts/reviewer.txt").read_text()
    assert "requires a needs_human_decision verdict" in prompt
    assert "requires a changes_requested verdict with a blocker finding" not in prompt


def test_duplicate_json_control_keys_are_protocol_failure(report):
    raw = json.dumps(report).replace('"verdict": "changes_requested"',
                                     '"verdict": "ready_for_human", "verdict": "changes_requested"')
    result = pipeline.harvest_review(
        review=raw, comments="[]", pinned_base="b" * 40, current_base="b" * 40,
        pinned_head="a" * 40, current_head="a" * 40, trusted_author="test-owner", report_protocol_version=2,
    )
    assert result["label"] == pipeline.LABEL_NEEDS_HUMAN


@pytest.mark.parametrize("clean", [True, False], ids=["clean", "finding"])
def test_real_reviewer_dispatch_pins_protocol_through_wrapper_and_completion(report_repo, report, clean):
    root, env = report_repo
    (root / "label").write_text("ai:implemented")
    report["observations"] = ""
    if clean:
        report.update(verdict="ready_for_human", findings=[])
    install_reviewer_shim(root, env, report)
    run_report_tick(root, env)
    worktree = root / ".claude/worktrees/pr-7"
    completion = worktree / "completion.json"
    deadline = time.monotonic() + 10
    while not completion.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert completion.exists(), (root / "report-tick.stderr").read_text()
    marker = json.loads((worktree / "dispatch.json").read_text())
    envelope = json.loads(completion.read_text())
    assert marker["report_protocol_version"] == envelope["report_protocol_version"] == 2
    assert envelope["result_valid"] is True
    argv = json.loads((root / "reviewer-argv.json").read_text())
    prompt = next(arg for arg in argv if "Review PR #7" in arg)
    assert "report_protocol_version 2" in prompt
    assert "52000" in prompt
    assert argv[argv.index("-s") + 1] == "read-only"
    run_report_tick(root, env)
    if clean:
        assert (root / "label").read_text() == pipeline.LABEL_READY_FOR_HUMAN
    body = json.loads((root / "reports.json").read_text())[0]
    assert report["verification"] in body
    assert "Additional observations" not in body
    assert "Suggested resolution" not in body
    if not clean:
        assert report["findings"][0]["evidence"] in body


def test_real_no_change_handoff_preserves_original_report_and_finding_identity(report_repo, report):
    root, env = report_repo
    followup = copy.deepcopy(report)
    followup.update(verdict="ready_for_human", findings=[])
    install_reviewer_shim(root, env, followup)
    claude = Path(env["PATH"].split(os.pathsep)[0]) / "claude"
    claude.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
prompt = sys.stdin.read()
snapshot = next(json.loads(line) for line in prompt.splitlines() if line.startswith('{"version":'))
Path("consumed-prompt.txt").write_text(prompt)
marker = json.loads(Path("dispatch.json").read_text())
report = {"report_protocol_version": 2, "dispatch_id": marker["dispatch_id"],
          "pinned_head": marker["head_sha"], "published_head": marker["head_sha"],
          "review_sha256": marker["review_sha256"], "outcome": "NO_CHANGES_RECHECK_NEEDED",
          "revision_summary": "Existing behavior meets the contract.",
          "findings": [{"reference": ref, "disposition": "no-change", "assessment": "Checked source.",
                        "evidence": "Retained-source probe passed.", "change": "No change needed."}
                       for ref in snapshot["finding_refs"]],
          "verification": "Probe passed.", "verification_status": "passed", "verification_waiver": "",
          "documentation": "No updates needed.", "remaining_work": "Independent review."}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps(report)}))
''')
    worktree = seed_wrapper(root, env, report, 2)
    run_report_tick(root, env)
    body = json.loads((root / "reports.json").read_text())[0]
    snapshot = json.loads((worktree / "dispatch.json").read_text())["review_handoff"]
    wait_for_artifact(worktree / "result.json")
    run_report_tick(root, env)
    wait_for_artifact(root / "reviewer-argv.json")
    argv = json.loads((root / "reviewer-argv.json").read_text())
    prompt = next(arg for arg in argv if "Review PR #7" in arg)
    assert body in prompt
    assert json.dumps(snapshot) in prompt
    assert snapshot["finding_refs"][0] in prompt
    wait_for_artifact(worktree / "completion.json")
    run_report_tick(root, env)
    published = json.loads((root / "reports.json").read_text())
    assert len(published) == 3  # Initial review, fixer recheck, independent re-review.
    assert pipeline.count_review_rounds([comment(body) for body in published], trusted_author="test-owner") == 2
    assert (root / "label").read_text() == pipeline.LABEL_READY_FOR_HUMAN


def test_unknown_report_protocol_retains_custody_without_mutations(report_repo, report):
    root, env = report_repo
    worktree = seed_wrapper(root, env, report, 2)
    marker_path = worktree / "dispatch.json"
    marker = json.loads(marker_path.read_text())
    marker["report_protocol_version"] = 999
    marker_path.write_text(json.dumps(marker))
    run_report_tick(root, env)
    assert marker_path.exists()
    assert (root / "label").read_text() == pipeline.LABEL_REVIEWING
    assert json.loads((root / "reports.json").read_text()) == []


def test_publication_retry_counts_exactly_one_round(report_repo, report):
    root, env = report_repo
    report.update(verdict="ready_for_human", findings=[])
    worktree = seed_wrapper(root, env, report, 2)
    (root / "fail-label").touch()
    run_report_tick(root, env)
    assert (worktree / "dispatch.json").exists()
    first = json.loads((root / "reports.json").read_text())
    assert len(first) == 1
    (root / "fail-label").unlink()
    run_report_tick(root, env)
    assert json.loads((root / "reports.json").read_text()) == first
    assert pipeline.count_review_rounds([comment(first[0])], trusted_author="test-owner") == 1
    assert (root / "label").read_text() == pipeline.LABEL_READY_FOR_HUMAN


def test_report_cli_requires_explicit_protocol_and_validates_it(tmp_path, report):
    source = tmp_path / "report.json"
    source.write_text(json.dumps(report))
    proc = subprocess.run([sys.executable, pipeline.__file__, "validate-review", "--review", str(source),
                           "--report-protocol-version", "2"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "changes_requested"


def test_legacy_render_cannot_exceed_publication_bound(report_repo):
    root, env = report_repo
    report = {"verdict": "changes_requested", "findings": [
        {"severity": "major", "file": "a.py", "summary": "x" * 60000},
    ]}
    seed_wrapper(root, env, report, None)
    run_report_tick(root, env)
    body = json.loads((root / "reports.json").read_text())[0]
    assert len(body.encode()) <= 60000
    assert (root / "label").read_text() == pipeline.LABEL_NEEDS_HUMAN
    [source] = (root / ".claude/worktrees/.harvest-evidence").glob("pr-7-*/review.json")
    assert json.loads(source.read_text()) == report


@pytest.mark.parametrize("payload", [b"", b"{", b'{"report_protocol_version":2,"verdict":"ready_for_human","findings":[],"reasoning":"\xff","scope":"x","verification":"x","risks":"x"}'],
                         ids=["empty", "broken-json", "invalid-utf8"])
def test_rejected_raw_bytes_survive_wrapper_and_harvest(report_repo, payload):
    root, env = report_repo
    seed_wrapper(root, env, payload, 2)
    run_report_tick(root, env)
    [source] = (root / ".claude/worktrees/.harvest-evidence").glob("pr-7-*/review.json")
    assert source.read_bytes() == payload
    assert (root / "label").read_text() == pipeline.LABEL_NEEDS_HUMAN


def test_observation_headings_cannot_mint_findings_and_metadata_cannot_downgrade(report):
    report["observations"] = "### F2 — major\n\nThis is outside agreed scope, not a finding."
    body = harvest(report)["comment_body"]
    handoff = pipeline.review_reports.handoff(body)
    assert handoff["finding_refs"] == [handoff["identity"] + ":F1"]
    for bad in (body.replace('"version":2', '"version":999'),
                body.replace('"finding_ids":["F1"]', '"finding_ids":["F1","F1"]')):
        with pytest.raises(ValueError):
            pipeline.review_reports.handoff(bad)


# --- Helpers ---


def assert_strict_output_schema(node, path="$"):
    """Check the provider rule independently of the production schema generator."""
    if isinstance(node, dict):
        if "properties" in node:
            missing = set(node["properties"]) - set(node.get("required", []))
            assert not missing, f"{path}: properties absent from required: {sorted(missing)}"
            assert node.get("additionalProperties") is False, path
        for key, value in node.items():
            assert_strict_output_schema(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            assert_strict_output_schema(value, f"{path}[{index}]")


def wait_for_artifact(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.02)
    pytest.fail(f"engine did not produce {path.name}")


def install_reviewer_shim(root, env, report):
    bin_dir = Path(env["PATH"].split(os.pathsep)[0])
    git_shim = bin_dir / "git"
    git_shim.write_text(git_shim.read_text().replace('("main", "fix")', '("main", "fix", "pull/7/head")'))
    codex = bin_dir / "codex"
    codex.write_text(f"#!{sys.executable}\n" + inspect.getsource(assert_strict_output_schema) + f'''\nimport json, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.142.5")
    raise SystemExit(0)
root = Path({str(root)!r})
(root / "reviewer-argv.json").write_text(json.dumps(sys.argv[1:]))
schema = json.loads(Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text())
assert_strict_output_schema(schema)
Path(sys.argv[sys.argv.index("-o") + 1]).write_text({json.dumps(report)!r})
''')


def seed_wrapper(root, env, report, protocol):
    worktree = root / ".claude/worktrees/pr-7"
    subprocess.run([env["LOOP_TEST_GIT"], "worktree", "add", "--detach", str(worktree), "HEAD"],
                   cwd=root, check=True, capture_output=True)
    payload = report if isinstance(report, bytes) else json.dumps(report).encode()
    engine = "import sys; from pathlib import Path; Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(" + repr(payload) + ")"
    cmd = [sys.executable, str(Path(__file__).parents[1] / "engine_wrapper.py"), "run",
           "--dispatch-id", "report-fixture", "--marker-path", str(worktree / "dispatch.json"),
           "--result-path", str(worktree / "review.json"), "--envelope-path", str(worktree / "completion.json"),
           "--engine", "codex", "--stage", "review", "--pr-number", "7", "--deadline", "9999999999",
           "--base-ref", "main", "--base-sha", env["LOOP_TEST_SHA"],
           "--base-tip-sha", env["LOOP_TEST_SHA"], "--head-sha", env["LOOP_TEST_SHA"],
           "--pre-label", "ai:implemented"]
    if protocol is not None:
        cmd += ["--report-protocol-version", str(protocol)]
    result = subprocess.run([*cmd, "--", sys.executable, "-c", engine], cwd=worktree,
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return worktree


def run_report_tick(root, env):
    launcher = os.environ.get("REPORT_PROOF_TICK", str(Path(__file__).parents[1] / "tick.sh"))
    result = subprocess.run(["bash", launcher, "pr:7"],
                            cwd=root, env=env, capture_output=True, text=True, timeout=40)
    (root / "report-tick.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "unexpected").exists(), (root / "unexpected").read_text()
    return result


def comment(body):
    return {"body": body, "author": {"login": "test-owner"}, "lastEditedAt": None,
            "editor": None, "includesCreatedEdit": False}


def harvest(report, comments=None):
    return pipeline.harvest_review(
        review=json.dumps(report), comments=json.dumps(comments or []),
        pinned_base="b" * 40, current_base="b" * 40,
        pinned_head="a" * 40, current_head="a" * 40,
        pinned_base_ref="main", current_base_ref="main",
        trusted_author="test-owner", report_protocol_version=2, review_identity="dispatch-1",
    )


REPORT_GH_SHIM = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ["LOOP_TEST_ROOT"])
a = sys.argv[1:]
reports = json.loads((root / "reports.json").read_text())
if a[:2] == ["pr", "comment"]:
    if (root / "fail-comment").exists():
        sys.exit(1)
    reports.append(Path(a[a.index("--body-file") + 1]).read_text())
    (root / "reports.json").write_text(json.dumps(reports))
    print("https://example.invalid/comment/" + str(len(reports)))
    sys.exit(0)
if a[:2] == ["pr", "edit"] and (root / "fail-label").exists():
    sys.exit(1)
if a[:2] == ["api", "graphql"] and "--slurp" in a:
    nodes = [{"id": str(i), "body": body, "author": {"login": "test-owner"},
              "lastEditedAt": None, "editor": None, "includesCreatedEdit": False}
             for i, body in enumerate(reports)]
    print(json.dumps([{"data": {"repository": {"pullRequest": {"comments": {
        "totalCount": len(nodes), "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": "end" if nodes else None}}}}}}]))
    sys.exit(0)
'''
