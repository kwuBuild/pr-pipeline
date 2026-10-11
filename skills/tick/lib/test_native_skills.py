"""Native skill exposure at the installed-source/engine boundary."""

import json
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def installed_skill(tmp_path):
    root = tmp_path / "install" / "skills"
    skill = root / "definition-of-done"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: definition-of-done\ndescription: Fixture guidance\n---\n"
        "Read ${CLAUDE_SKILL_DIR}/proof.txt.\n"
    )
    (skill / "proof.txt").write_text("installed-proof")
    manifest = root.parent / ".claude-plugin/plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"name": "pr-pipeline", "skills": "./skills"}')
    return root


@pytest.mark.parametrize("mode", ["auto", "dontAsk"])
def test_claude_launch_loads_existing_installed_plugin(tmp_path, installed_skill, mode):
    import pipeline
    import tick

    target = tmp_path / "target"
    replacement = target / ".claude/skills/definition-of-done"
    replacement.mkdir(parents=True)
    (replacement / "SKILL.md").write_text("branch replacement")
    with mock.patch.object(pipeline, "INSTALL_SKILLS_ROOT", installed_skill), \
         mock.patch("subprocess.Popen") as launch:
        launch.return_value = mock.Mock(pid=12345)
        tick._launch_claude_engine(
            engine_bin="claude", prompt="fixture", wt_dir=str(target),
            result_path=str(target / "result.json"), env={"PATH": "/usr/bin"},
            push_permission="Bash(git push origin HEAD)", permission_mode=mode,
            skills=("definition-of-done",),
        )
    argv = launch.call_args.args[0]
    plugin = Path(argv[argv.index("--plugin-dir") + 1])
    assert plugin == installed_skill.parent
    assert (plugin / "skills/definition-of-done/proof.txt").read_text() == "installed-proof"
    assert "Skill(pr-pipeline:definition-of-done)" in argv[argv.index("--allowedTools") + 1]
    assert "--add-dir" not in argv
    assert not Path(str(target) + "-scrub").exists()
    assert not (target / "skill-context.json").exists()


def test_missing_native_source_refuses_launch(tmp_path, installed_skill):
    import pipeline
    import tick

    with mock.patch.object(pipeline, "INSTALL_SKILLS_ROOT", installed_skill), \
         mock.patch("subprocess.Popen") as launch:
        result = tick._launch_claude_engine(
            engine_bin="claude", prompt="fixture", wt_dir=str(tmp_path),
            result_path=str(tmp_path / "result.json"), env={},
            push_permission="", skills=("missing",),
        )
    assert result is None
    launch.assert_not_called()


def test_codex_exposure_keeps_optional_installed_guidance_in_scrub_home(tmp_path, installed_skill):
    import native_skills

    home = tmp_path / "scrub"
    paths = native_skills.prepare_reviewer(
        skills=("definition-of-done",),
        install_root=installed_skill, scrub_dir=home,
    )
    source = home / ".agents/skills/definition-of-done/SKILL.md"
    assert source.read_bytes() == (installed_skill / "definition-of-done/SKILL.md").read_bytes()
    assert source.with_name("proof.txt").read_text() == "installed-proof"
    assert paths == {"definition-of-done": str(source)}
    assert not (home / "hooks").exists()
    assert not (home / ".codex/config.toml").exists()


def test_codex_native_catalog_preserves_installed_relative_links(tmp_path, installed_skill):
    import native_skills

    runtime = installed_skill / "tick/lib/tick.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("installed owner")
    paths = native_skills.prepare_reviewer(
        skills=("definition-of-done",),
        install_root=installed_skill, scrub_dir=tmp_path / "scrub",
    )
    exposed = Path(paths["definition-of-done"])
    assert (exposed.parent / "../tick/lib/tick.py").read_text() == "installed owner"


def test_optional_codex_catalog_does_not_explicitly_invoke_every_skill(tmp_path, installed_skill):
    import native_skills

    paths = native_skills.prepare_reviewer(
        skills=("definition-of-done",),
        install_root=installed_skill, scrub_dir=tmp_path / "scrub",
    )
    prompt = native_skills.reviewer_prompt(paths)
    assert "$definition-of-done" not in prompt
    assert "definition-of-done" in prompt
    assert "optional" in prompt


def test_codex_retry_removes_stale_skill_links(tmp_path, installed_skill):
    import native_skills

    home = tmp_path / "scrub"
    native_skills.prepare_reviewer(
        install_root=installed_skill, scrub_dir=home, skills=("definition-of-done",),
    )
    native_skills.prepare_reviewer(install_root=installed_skill, scrub_dir=home, skills=())
    assert list((home / ".agents/skills").iterdir()) == []


@pytest.mark.parametrize("path", [
    "skills/tick/lib/native_skills.py",
    "skills/automation-control-plane-review/SKILL.md",
    "skills/durability-boundary-review/SKILL.md",
    "skills/loop-cadence-contract-review/SKILL.md",
])
def test_native_catalog_and_optional_review_guidance_are_policy(path):
    import pipeline

    assert pipeline.touches_review_policy([path]) is True
