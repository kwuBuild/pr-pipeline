"""Tests for bounded, sanitized reviewer skill observation."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest


def test_successful_repo_skill_reference_is_observed() -> None:
    """A completed command reference records only its normalized identity."""
    try:
        from reviewer_observability import observe_event_lines
    except ModuleNotFoundError:
        pytest.fail("reviewer_observability is not implemented")

    event = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "sed -n '1,200p' .agents/skills/example-review/SKILL.md",
            "exit_code": 0,
            "aggregated_output": "private candidate output",
        },
    }).encode()

    observation = observe_event_lines([event + b"\n"])

    from engine_usage import codex_usage

    assert observation == {
        "schema_version": 2,
        "status": "complete",
        "evidence_contract": "successful_command_referencing_skill_file",
        "explicit_invocation_events_supported": False,
        "observed_skills": [
            {"name": "example-review", "source": "repository"},
        ],
        "event_count": 1,
        "parse_errors": 0,
        "oversized_events_skipped": 0,
        "skills_truncated": 0,
        "usage": codex_usage(None),
    }
    assert "private candidate output" not in json.dumps(observation)


def test_successful_plugin_skill_reference_omits_absolute_path() -> None:
    """Installed-plugin evidence records the skill directory, never its path."""
    from reviewer_observability import observe_event_lines

    plugin_path = (
        "/Users/operator/.codex/plugins/cache/openai-curated-remote/github/"
        "0.1.8/skills/gh-fix-ci/SKILL.md"
    )
    event = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": f"cat {plugin_path}",
            "exit_code": 0,
        },
    }).encode()

    observation = observe_event_lines([event + b"\n"])

    assert observation["observed_skills"] == [
        {"name": "gh-fix-ci", "source": "plugin"},
    ]
    assert "/Users/operator" not in json.dumps(observation)


def test_malformed_event_marks_observation_partial() -> None:
    """Malformed JSON is counted without exposing or aborting on its content."""
    from reviewer_observability import observe_event_lines

    observation = observe_event_lines([b'{"candidate_secret":\n'])

    assert observation["status"] == "partial"
    assert observation["event_count"] == 1
    assert observation["parse_errors"] == 1
    assert "candidate_secret" not in json.dumps(observation)


def test_failed_commands_and_command_output_cannot_claim_skill_reference() -> None:
    """Only a successful command field contributes normalized evidence."""
    from reviewer_observability import observe_event_lines

    failed_reference = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "cat .claude/skills/failed-review/SKILL.md",
            "exit_code": 1,
        },
    }).encode()
    output_only_reference = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "printf harmless",
            "exit_code": 0,
            "aggregated_output": ".claude/skills/forged-review/SKILL.md",
        },
    }).encode()
    boolean_exit_code = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "cat .claude/skills/boolean-review/SKILL.md",
            "exit_code": False,
        },
    }).encode()

    observation = observe_event_lines([
        failed_reference + b"\n",
        output_only_reference + b"\n",
        boolean_exit_code + b"\n",
    ])

    assert observation["observed_skills"] == []


def test_oversized_event_is_drained_and_later_skill_event_is_observed() -> None:
    """One hostile event cannot consume memory or hide later evidence."""
    from reviewer_observability import observe_event_stream

    good_event = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "cat .agents/skills/bounded-review/SKILL.md",
            "exit_code": 0,
        },
    }).encode()
    stream = BytesIO(b"x" * 400 + b"\n" + good_event + b"\n")

    observation = observe_event_stream(stream, max_event_bytes=256)

    assert observation["status"] == "partial"
    assert observation["event_count"] == 2
    assert observation["oversized_events_skipped"] == 1
    assert observation["observed_skills"] == [
        {"name": "bounded-review", "source": "repository"},
    ]


def test_observed_skills_are_bounded_across_the_run() -> None:
    """Hostile unique paths cannot grow the durable summary without limit."""
    from reviewer_observability import observe_event_lines

    events = [
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"cat .agents/skills/review-{index}/SKILL.md",
                "exit_code": 0,
            },
        }).encode()
        for index in range(70)
    ]

    observation = observe_event_lines(events)

    assert observation["status"] == "partial"
    assert len(observation["observed_skills"]) == 64
    assert observation["skills_truncated"] == 6


def test_producer_rejects_oversized_and_dot_segment_skill_names() -> None:
    """One bounded event cannot smuggle an unbounded or unsafe identity."""
    from reviewer_observability import observe_event_lines

    oversized_name = "x" * 200_000
    events = [
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": f"cat .claude/skills/{name}/SKILL.md",
                "exit_code": 0,
            },
        }).encode()
        for name in (oversized_name, ".", "..")
    ]
    events.append(json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "cat .agents/skills/near-match/SKILL.md.backup",
            "exit_code": 0,
        },
    }).encode())
    for command in (
        "cat x.agents/skills/component-match/SKILL.md",
        "cat /tmp/xplugins/cache/skills/component-match/SKILL.md",
    ):
        events.append(json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": command,
                "exit_code": 0,
            },
        }).encode())

    observation = observe_event_lines(events)

    assert observation["observed_skills"] == []
    assert len(json.dumps(observation)) < 1_000


def test_load_observation_accepts_only_the_strict_sanitized_shape(
    tmp_path: Path,
) -> None:
    """Harvest loads a valid summary without retaining any extra raw fields."""
    from reviewer_observability import load_observation

    path = tmp_path / "review-observation.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "status": "complete",
        "evidence_contract": "successful_command_referencing_skill_file",
        "explicit_invocation_events_supported": False,
        "observed_skills": [{"name": "review-one", "source": "repository"}],
        "event_count": 4,
        "parse_errors": 0,
        "oversized_events_skipped": 0,
        "skills_truncated": 0,
    }))

    assert load_observation(path) == json.loads(path.read_text())


@pytest.mark.parametrize(
    "payload",
    [
        {"raw_event": "candidate secret"},
        {
            "schema_version": 1,
            "status": "complete",
            "evidence_contract": "successful_command_referencing_skill_file",
            "explicit_invocation_events_supported": False,
            "observed_skills": [
                {"name": "../../private", "source": "repository"},
            ],
            "event_count": 1,
            "parse_errors": 0,
            "oversized_events_skipped": 0,
            "skills_truncated": 0,
        },
    ],
)
def test_load_observation_replaces_invalid_content_with_unavailable(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    """Malformed or unsanitized files never cross into the durable tick log."""
    from reviewer_observability import load_observation

    path = tmp_path / "review-observation.json"
    path.write_text(json.dumps(payload))

    observation = load_observation(path)

    assert observation["status"] == "unavailable"
    assert observation["observed_skills"] == []
    assert "candidate secret" not in json.dumps(observation)


def test_isolated_skill_links_are_plugin_references(tmp_path):
    from reviewer_observability import observe_event_lines

    source = tmp_path / "installed/skills/example/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("guidance")
    exposed = tmp_path / "home/.agents/skills/example"
    exposed.parent.mkdir(parents=True)
    exposed.symlink_to(source.parent, target_is_directory=True)
    for path in (exposed / "SKILL.md", source):
        event = json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": f"echo '{path}'", "exit_code": 0,
        }}).encode()
        observation = observe_event_lines([event], skill_home=tmp_path / "home")
        assert observation["observed_skills"] == [{"name": "example", "source": "plugin"}]
        assert observation["evidence_contract"] == "successful_command_referencing_skill_file"
        assert str(tmp_path) not in json.dumps(observation)
