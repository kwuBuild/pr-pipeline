"""Codex write routes retain the controller's existing stage contracts."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import pipeline
import tick


@pytest.mark.parametrize("route", [
    {"impl_engine": "codex", "model": "gpt-5.5", "effort": "xhigh"},
    {"impl_engine": "claude", "model": "sonnet", "effort": "max", "fix_engine": "codex"},
    {"impl_engine": "codex", "effort": "xhigh", "fix_engine": "claude"},
])
def test_codex_routes_resolve_engine_specific_inheritance(route):
    resolved, problems = pipeline._resolve_routes([("test", route)])
    assert problems == []
    assert resolved["impl_engine"] == route["impl_engine"]
    if "fix_engine" not in route:
        assert resolved["fix_engine"] == "codex"
        assert resolved["fix_model"] == "gpt-5.5"
        assert resolved["fix_effort"] == "xhigh"
    else:
        assert resolved["fix_model"] == resolved["fix_effort"] == ""


@pytest.mark.parametrize("route", [
    {"impl_engine": "codex", "effort": "max"},
    {"fix_engine": "codex", "fix_effort": "max"},
])
def test_effort_rejects_other_engine_values(route):
    assert pipeline._resolve_routes([("test", route)])[1]


def test_codex_markers_and_cooldown_are_write_stage_routes(tmp_path):
    assert "codex" in pipeline._VALID_ENGINES_BY_STAGE["implement"]
    assert "codex" in pipeline._VALID_ENGINES_BY_STAGE["fix"]
    assert tick._resolve_engine("codex", effort="high") == ("codex", {})
    domain = tick._engine_quota_domain("codex")
    cooldowns = tick.EngineCooldowns(str(tmp_path))
    cooldowns.start(domain, until=200, reason="quota_or_limit")
    assert cooldowns.read_active(domain, now=100) == 200


def test_codex_permission_preset_does_not_repeat_conflicting_sandbox_flag():
    import codex_write_wrapper

    argv = codex_write_wrapper.engine_argv()
    assert "--approve-for-me" in argv
    assert "--sandbox" not in argv  # CLI 0.171.0 rejects this pair with exit 2.


@pytest.mark.parametrize("payload", [
    {"result": "IMPLEMENTATION_COMPLETE"},
    {"engine": "codex", "is_error": False, "turn_completed": True, "result": "IMPLEMENTATION_COMPLETE"},
])
def test_codex_harvest_cannot_downgrade_to_legacy_transport(payload):
    decision = pipeline.harvest_implementation(result=json.dumps(payload), engine="codex")
    assert decision["action"] == "escalate"
    assert pipeline.harvest_fix(result=json.dumps(payload), engine="codex")["action"] == "escalate"
    assert pipeline.held_completion_outcome("implement", payload, engine="codex") == pipeline.OUTCOME_UNRECOGNIZED


@pytest.mark.parametrize("scenario,exit_code,complete", [
    ("success", 0, True), ("blocked", 0, False), ("partial", 0, False),
    ("malformed", 0, False), ("success", 1, False), ("quota", 1, False),
])
def test_codex_wrapper_requires_terminal_event_and_successful_exit(tmp_path, scenario, exit_code, complete):
    events = [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text":
            "NEEDS_HUMAN_DECISION" if scenario == "blocked" else "IMPLEMENTATION_COMPLETE"}},
    ]
    if scenario == "quota":
        events += [{"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}]
    elif scenario != "partial":
        events += [{"type": "turn.completed", "usage": {
            "input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 5}}]
    script = tmp_path / "engine.py"
    script.write_text("import sys\nprint(" + repr("\n".join(json.dumps(x) for x in events)
        + ("\nmalformed" if scenario == "malformed" else "")) + ")\nsys.exit(" + str(exit_code) + ")\n")
    wrapper = Path(tick.__file__).with_name("codex_write_wrapper.py")
    run = subprocess.run([sys.executable, str(wrapper), "--heartbeat", str(tmp_path / "heartbeat"),
        "--", sys.executable, str(script)], capture_output=True, text=True)
    assert run.stdout, run.stderr
    payload = json.loads(run.stdout)
    assert payload["engine"] == "codex"
    assert payload["engine_exit_status"] == exit_code
    assert (pipeline.parse_implementation_result(payload) == "IMPLEMENTATION_COMPLETE") == complete
    assert (tmp_path / "heartbeat").is_file()
    if scenario == "quota":
        assert pipeline.classify_engine_result_failure(payload) == "quota_or_limit"
    if complete:
        summary = tick._engine_result_summary(result_raw=run.stdout, harvest_result={},
            harvest_action="complete", harvest_label="")
        assert summary["usage"]["input_tokens"] == 10
        assert summary["usage"]["cached_input_tokens"] == 3


def test_codex_skill_exposure_preserves_existing_catalog_and_relative_files(tmp_path):
    import native_skills

    install = tmp_path / "install/skills"
    source = install / "definition-of-done"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("required guidance")
    (source / "reference.txt").write_text("installed support")
    target = tmp_path / "target"
    shadow = target / ".agents/skills/definition-of-done"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text("target shadow")
    paths = native_skills.prepare_writer(install_root=install, worktree=target,
        skills=("definition-of-done",))
    assert Path(paths["definition-of-done"]).resolve() == source / "SKILL.md"
    assert Path(paths["definition-of-done"]).with_name("reference.txt").read_text() == "installed support"
    assert (shadow / "SKILL.md").read_text() == "target shadow"
    assert paths == native_skills.prepare_writer(install_root=install, worktree=target,
        skills=("definition-of-done",))
    with pytest.raises((OSError, ValueError)):
        native_skills.prepare_writer(install_root=install, worktree=target,
            skills=("definition-of-done", "missing"))
    assert (shadow / "SKILL.md").read_text() == "target shadow"


def test_codex_skill_exposure_refuses_owned_name_collision(tmp_path):
    import native_skills

    source = tmp_path / "install/definition-of-done"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("installed")
    collision = tmp_path / "target/.agents/skills/pr-pipeline-definition-of-done"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("existing")
    with pytest.raises((OSError, ValueError)):
        native_skills.prepare_writer(install_root=source.parent, worktree=tmp_path / "target",
            skills=("definition-of-done",))
    assert (collision / "SKILL.md").read_text() == "existing"


def test_codex_skill_setup_rolls_back_only_links_created_before_failure(tmp_path):
    import native_skills

    install = tmp_path / "install"
    for name in ("one", "two"):
        (install / name).mkdir(parents=True)
        (install / name / "SKILL.md").write_text("installed")
    catalog = tmp_path / "target/.agents/skills"
    (catalog / "pr-pipeline-two").mkdir(parents=True)
    (catalog / "pr-pipeline-two/keep.txt").write_text("target owned")
    with pytest.raises(OSError):
        native_skills.prepare_writer(install_root=install, worktree=tmp_path / "target", skills=("one", "two"))
    assert not (catalog / "pr-pipeline-one").exists()
    assert (catalog / "pr-pipeline-two/keep.txt").read_text() == "target owned"


@pytest.mark.parametrize("parent", [".agents", ".agents/skills"])
def test_codex_skill_setup_never_follows_target_catalog_outside_worktree(tmp_path, parent):
    import native_skills

    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "target"
    link = target / parent
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="inside the worktree"):
        native_skills.prepare_writer(install_root=tmp_path, worktree=target, skills=())
    assert list(outside.iterdir()) == []
