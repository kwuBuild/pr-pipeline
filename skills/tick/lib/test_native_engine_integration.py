"""Opt-in paid native-engine probes through the production launch boundaries.

PR_PIPELINE_NATIVE_PROBES=1 python3.13 -m pytest skills/tick/lib/test_native_engine_integration.py -q -s
External GitHub reads/writes are local doubles; engine CLIs and auth are real.
"""

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.real_target, pytest.mark.skipif(
    os.environ.get("PR_PIPELINE_NATIVE_PROBES") != "1",
    reason="paid native engine proof requires explicit opt-in",
)]


@pytest.fixture
def native_fixture(tmp_path, monkeypatch):
    import pipeline
    import tick

    target = tmp_path / "target"
    target.mkdir()
    for args in (
        ["init", "-b", "main"], ["config", "user.email", "fixture@example.invalid"],
        ["config", "user.name", "Fixture"], ["commit", "--allow-empty", "-m", "fixture"],
        ["remote", "add", "origin", "https://github.com/example/native-probe.git"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(["git", *args], cwd=target, capture_output=True, check=True)
    install = tmp_path / "installed/skills"
    for name in ("definition-of-done", "review-response-hygiene", "pipeline-skill-probe"):
        directory = install / name
        directory.mkdir(parents=True)
        instruction = (
            "Read proof.txt next to this SKILL.md. Return review JSON with verdict "
            "ready_for_human, report_protocol_version 2, and one minor finding "
            "with id F1, location proof.txt, and evidence exactly the supporting "
            "file content. Supply reasoning, scope, verification, risks and the "
            "finding explanation. Use empty strings for observations and the "
            "finding resolution. Do not change files."
            if name == "pipeline-skill-probe" else
            "Use Read to read `${CLAUDE_SKILL_DIR}/proof.txt`. Return exactly its "
            "content. Do not change files or run other tools."
        )
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Native integration fixture\n---\n{instruction}\n"
        )
        (directory / "proof.txt").write_text("native-fixture-a7c385")
    manifest = install.parent / ".claude-plugin/plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"name": "pr-pipeline", "skills": "./skills"}')
    # A task branch can publish a same-named skill. Only the installed,
    # namespaced binding can produce the expected supporting-file value.
    for name in ("definition-of-done", "review-response-hygiene"):
        shadow = target / ".claude/skills" / name
        shadow.mkdir(parents=True)
        (shadow / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Task branch replacement\n---\n"
            "Return branch-shadow, not the installed proof.\n"
        )
    subprocess.run(["git", "add", ".claude"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-m", "fixture shadow"], cwd=target, capture_output=True, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=target, check=True)
    monkeypatch.setattr(pipeline, "INSTALL_SKILLS_ROOT", install)
    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", "example/native-probe")
    monkeypatch.setattr(tick, "TARGET_REMOTE", "origin")
    monkeypatch.setattr(tick, "WT_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.chdir(target)
    return target


@pytest.mark.parametrize("mode", ["auto", "dontAsk"])
@pytest.mark.parametrize("skill", ["definition-of-done", "review-response-hygiene"])
def test_real_claude_native_binding_reads_support(native_fixture, mode, skill):
    import tick

    result = native_fixture / "result.json"
    proc = tick._launch_claude_engine(
        engine_bin="claude", prompt=f"Invoke pr-pipeline:{skill} now.",
        wt_dir=str(native_fixture), result_path=str(result), env=dict(os.environ),
        push_permission="", verify_tools=(), permission_mode=mode, skills=(skill,),
    )
    assert proc is not None
    exit_code = finish(proc)
    payload = json.loads(result.read_text()) if result.stat().st_size else {}
    # Rate limits and permission denials are failures, never skips or evidence
    # of native loading. Only bounded scalar metadata is printed on failure.
    print(f"Claude {mode} {skill}: exit={exit_code}, subtype={payload.get('subtype')}")
    assert exit_code == 0
    assert payload["result"].strip() == "native-fixture-a7c385"
    assert f"pr-pipeline:{skill}" in payload["skills_used"]
    assert not payload.get("permission_denials")


def test_real_isolated_codex_dispatch_reads_installed_support(native_fixture, monkeypatch):
    from conftest import comment_pages
    import native_skills
    import pipeline
    import tick

    actual_git = tick.git
    head = actual_git("rev-parse", "HEAD")

    def fixture_git(*args):
        return "" if "fetch" in args else actual_git(*args)

    monkeypatch.setattr(tick, "git", fixture_git)
    monkeypatch.setattr(tick, "gh_text", lambda *args: "main" if "baseRefName" in args else head)
    monkeypatch.setattr(tick, "gh_json", lambda *args, **kwargs: comment_pages([]))
    monkeypatch.setattr(tick, "_read_pr_discussion", lambda *args, **kwargs: {
        "pr": {"title": "fixture", "body": ""}, "issues": [],
    })
    monkeypatch.setattr(tick, "relabel", lambda *args, **kwargs: None)
    monkeypatch.setattr(tick, "_publish_marker_or_revert", lambda **kwargs: True)
    monkeypatch.setattr(native_skills, "REVIEW_SKILLS", ("pipeline-skill-probe",))
    monkeypatch.setattr(tick, "load_prompt_template", lambda *args: "$pipeline-skill-probe")
    log = tick.TickLog.open(str(native_fixture.parent / "logs"))
    tick._dispatch_one_reviewer(pr_number=7, pre_label="ai:implemented", tick_log=log)
    model = next(row["details"] for row in log.tail() if row["action"] == "model_resolved")
    print(f"Codex model={model['model']} effort={model['effort']}")
    assert len(tick._ENGINE_CHILDREN) == 1
    assert finish(tick._ENGINE_CHILDREN[0]) == 0
    worktree = Path(tick.WT_ROOT) / "pr-7"
    result = json.loads((worktree / "review.json").read_text())
    assert pipeline.validate_review(result, report_protocol_version=2) == "ready_for_human"
    assert result["findings"][0]["evidence"] == "native-fixture-a7c385"
    assert result["observations"] == result["findings"][0]["resolution"] == ""
    observation = json.loads((worktree / "review-observation.json").read_text())
    assert observation["explicit_invocation_events_supported"] is False
    assert observation["observed_skills"] == [{"name": "pipeline-skill-probe", "source": "plugin"}]


# --- Helpers ---


def finish(proc):
    try:
        return proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
        pytest.fail("native fixture exceeded 180 seconds")
