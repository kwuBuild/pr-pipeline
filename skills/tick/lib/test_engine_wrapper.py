"""Tests for the engine wrapper's atomic launch/completion ownership."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import engine_wrapper  # noqa: E402
import pipeline  # noqa: E402
import process_control  # noqa: E402


def test_wrapper_writes_marker_with_process_identity(tmp_path: Path) -> None:
    run_wrapper_with_echo(tmp_path, exit_code=0, valid_review=True)
    marker = json.loads((tmp_path / "dispatch.json").read_text())
    assert marker["dispatch_id"] == "d-test-001"
    assert marker["pid"] == os.getpid()
    assert isinstance(marker["start_time"], float)
    # pgid must equal the wrapper's own PID (os.setpgrp makes it the group leader)
    assert marker["pgid"] == os.getpid()
    assert "state" not in marker


def test_wrapper_records_os_start_time_when_available(tmp_path: Path) -> None:
    """start_time should use the OS source (get_process_start_time), not time.time()."""
    run_wrapper_with_echo(tmp_path, exit_code=0, valid_review=True)
    marker = json.loads((tmp_path / "dispatch.json").read_text())
    os_start = process_control.get_process_start_time(os.getpid())
    if os_start is None:
        pytest.skip("ps blocked by sandbox")
    # The recorded start_time must match the OS-reported start time (same source).
    assert abs(marker["start_time"] - os_start) < 0.1


def test_wrapper_writes_valid_envelope_on_success(tmp_path: Path) -> None:
    run_wrapper_with_echo(tmp_path, exit_code=0, valid_review=True)
    envelope = json.loads((tmp_path / "completion.json").read_text())
    assert envelope["dispatch_id"] == "d-test-001"
    assert envelope["exit_status"] == 0
    assert envelope["result_valid"] is True
    assert pipeline.validate_completion_envelope(
        envelope, expected_dispatch_id="d-test-001"
    )


def test_wrapper_places_result_atomically_on_success(tmp_path: Path) -> None:
    run_wrapper_with_echo(tmp_path, exit_code=0, valid_review=True)
    assert (tmp_path / "review.json").exists()
    result = json.loads((tmp_path / "review.json").read_text())
    assert result["verdict"] == "ready_for_human"


def test_wrapper_does_not_place_result_on_engine_failure(tmp_path: Path) -> None:
    run_wrapper_with_echo(tmp_path, exit_code=1, valid_review=True)
    assert not (tmp_path / "review.json").exists()
    envelope = json.loads((tmp_path / "completion.json").read_text())
    assert envelope["exit_status"] == 1
    assert envelope["result_valid"] is False
    assert envelope["error_class"] == "EngineExitError"
    assert pipeline.validate_failure_envelope(
        envelope, expected_dispatch_id="d-test-001",
    )
    assert not pipeline.validate_completion_envelope(envelope)


def test_wrapper_does_not_place_invalid_schema_result(tmp_path: Path) -> None:
    run_wrapper_with_echo(tmp_path, exit_code=0, valid_review=False)
    assert not (tmp_path / "review.json").exists()
    envelope = json.loads((tmp_path / "completion.json").read_text())
    assert envelope["exit_status"] == 0
    assert envelope["result_valid"] is False


def test_wrapper_cleans_up_temp_file_on_failure(tmp_path: Path) -> None:
    run_wrapper_with_echo(tmp_path, exit_code=1, valid_review=True)
    tmp_files = [f for f in tmp_path.iterdir() if f.name.startswith(".tmp-result-")]
    assert tmp_files == []


def test_wrapper_envelope_written_even_on_crash(tmp_path: Path) -> None:
    run_wrapper_with_echo(tmp_path, exit_code=137, valid_review=False)
    assert (tmp_path / "completion.json").exists()
    envelope = json.loads((tmp_path / "completion.json").read_text())
    assert envelope["exit_status"] == 137


@pytest.mark.parametrize(
    ("exit_code", "expected_stderr"),
    [
        (1, "engine-wrapper engine failure: EngineExitError\n"),
        (0, ""),
    ],
)
def test_wrapper_suppresses_raw_engine_output(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    exit_code: int,
    expected_stderr: str,
) -> None:
    """Only a fixed wrapper diagnostic survives child stdout/stderr output."""
    secret = "secret-token at /private/operator/path"
    payload = json.dumps({"verdict": "ready_for_human", "findings": []})
    script = (
        "import sys; "
        f"print({secret!r}); "
        f"print({secret!r}, file=sys.stderr); "
        "args = sys.argv[1:]; "
        'output = args[args.index("-o") + 1]; '
        f"open(output, 'w').write({payload!r}); "
        f"sys.exit({exit_code})"
    )

    engine_wrapper.run_engine(
        dispatch_id="d-output-test",
        marker_path=str(tmp_path / "dispatch.json"),
        result_path=str(tmp_path / "review.json"),
        envelope_path=str(tmp_path / "completion.json"),
        engine_cmd=[sys.executable, "-c", script, "-o", "placeholder.json"],
        marker_template=marker_template(),
    )

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == expected_stderr
    assert secret not in captured.err


def test_wrapper_streams_only_sanitized_skill_observation(tmp_path: Path) -> None:
    """Raw Codex JSONL is drained but only normalized evidence is persisted."""
    secret = "candidate-secret at /private/operator/path"
    skill_path = "/private/repo/.agents/skills/contract-review/SKILL.md"
    result_payload = json.dumps({"verdict": "ready_for_human", "findings": []})
    event_payload = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": f"sed -n '1,200p' {skill_path}",
            "exit_code": 0,
            "aggregated_output": secret,
        },
    })
    script = (
        "import sys; "
        f"print({event_payload!r}); "
        "args = sys.argv[1:]; "
        'output = args[args.index("-o") + 1]; '
        f"open(output, 'w').write({result_payload!r})"
    )

    exit_status = engine_wrapper.run_engine(
        dispatch_id="d-observation-test",
        marker_path=str(tmp_path / "dispatch.json"),
        result_path=str(tmp_path / "review.json"),
        envelope_path=str(tmp_path / "completion.json"),
        observation_path=str(tmp_path / "review-observation.json"),
        engine_cmd=[sys.executable, "-c", script, "-o", "placeholder.json"],
        marker_template=marker_template(),
    )

    assert exit_status == 0
    assert (tmp_path / "review.json").is_file()
    observation_raw = (tmp_path / "review-observation.json").read_text()
    observation = json.loads(observation_raw)
    assert observation["observed_skills"] == [
        {"name": "contract-review", "source": "repository"},
    ]
    assert secret not in observation_raw
    assert skill_path not in observation_raw


def test_observation_write_failure_does_not_change_review_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-authoritative telemetry failure cannot invalidate a valid review."""
    observation_path = tmp_path / "review-observation.json"
    real_atomic_write = pipeline.atomic_write

    def fail_observation_write(path: str, content: str) -> None:
        if path == str(observation_path):
            raise OSError("observation disk error")
        real_atomic_write(path, content)

    monkeypatch.setattr(engine_wrapper.pipeline, "atomic_write", fail_observation_write)
    payload = json.dumps({"verdict": "ready_for_human", "findings": []})
    script = (
        "import sys; print('{}'); args = sys.argv[1:]; "
        'output = args[args.index("-o") + 1]; '
        f"open(output, 'w').write({payload!r})"
    )

    exit_status = engine_wrapper.run_engine(
        dispatch_id="d-observation-write-failure",
        marker_path=str(tmp_path / "dispatch.json"),
        result_path=str(tmp_path / "review.json"),
        envelope_path=str(tmp_path / "completion.json"),
        observation_path=str(observation_path),
        engine_cmd=[sys.executable, "-c", script, "-o", "placeholder.json"],
        marker_template=marker_template(),
    )

    assert exit_status == 0
    assert (tmp_path / "review.json").is_file()
    envelope = json.loads((tmp_path / "completion.json").read_text())
    assert envelope["result_valid"] is True
    assert not observation_path.exists()


@pytest.mark.parametrize("failure_point", ["ownership", "spawn"])
def test_wrapper_writes_failure_envelope_before_engine_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_point: str,
) -> None:
    """Pre-engine wrapper exceptions leave durable dispatch-bound evidence."""
    expected_error = PermissionError("startup denied")
    monkeypatch.setattr(
        engine_wrapper.process_control,
        "get_process_start_time",
        lambda pid: 1234.5,
    )
    if failure_point == "ownership":
        monkeypatch.setattr(
            engine_wrapper.os,
            "setpgrp",
            lambda: (_ for _ in ()).throw(expected_error),
        )
    else:
        monkeypatch.setattr(
            engine_wrapper.subprocess,
            "Popen",
            lambda *args, **kwargs: (_ for _ in ()).throw(expected_error),
        )

    exit_status = engine_wrapper.run_engine(
        dispatch_id="d-startup-failure",
        marker_path=str(tmp_path / "dispatch.json"),
        result_path=str(tmp_path / "review.json"),
        envelope_path=str(tmp_path / "completion.json"),
        engine_cmd=["codex", "exec"],
        marker_template=marker_template(),
    )

    envelope = json.loads((tmp_path / "completion.json").read_text())
    assert exit_status != 0
    assert envelope == {
        "dispatch_id": "d-startup-failure",
        "exit_status": exit_status,
        "wrapper_pid": os.getpid(),
        "start_time": envelope["start_time"],
        "result_valid": False,
        "error_class": "PermissionError",
    }
    assert capsys.readouterr().err == (
        "engine-wrapper startup failure: PermissionError\n"
    )
    assert isinstance(envelope["start_time"], float)
    assert not (tmp_path / "review.json").exists()
    if failure_point == "spawn":
        assert list(tmp_path.glob(".tmp-result-*")) == []


def test_wrapper_failure_envelope_classifies_and_harvests_terminally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real producer schema grants terminal authority through harvest."""
    import tick  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-99"
    wt_dir.mkdir(parents=True)
    monkeypatch.setattr(engine_wrapper.os, "setpgrp", lambda: None)
    monkeypatch.setattr(engine_wrapper.os, "getpgrp", lambda: os.getpid())
    monkeypatch.setattr(
        engine_wrapper.process_control, "get_process_start_time", lambda _pid: 1234.5,
    )
    monkeypatch.setattr(
        engine_wrapper.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    template = marker_template()
    template["pr_number"] = 99
    assert engine_wrapper.run_engine(
        dispatch_id="d-wrapper-failure",
        marker_path=str(wt_dir / "dispatch.json"),
        result_path=str(wt_dir / "review.json"),
        envelope_path=str(wt_dir / "completion.json"),
        engine_cmd=["codex", "exec"],
        marker_template=template,
    ) == 1
    monkeypatch.undo()
    marker = json.loads((wt_dir / "dispatch.json").read_text())

    with mock.patch("tick.process_control.get_process_start_time", return_value=None):
        with mock.patch("tick.process_control.pid_is_alive", return_value=False):
            state, _identity, _alive = tick._classify_marker(
                str(wt_dir), marker, result_file="review.json",
            )
    assert state == "failed"

    with mock.patch(
        "tick._converge_pipeline_labels", return_value=True,
    ) as converge_mock:
        assert tick._harvest_failed_reviewer(99, str(wt_root)) is True
    converge_mock.assert_called_once_with(
        "pr", 99, "ai:needs-human-decision",
        tick_log=None, phase="harvest", stage="review",
    )
    assert not wt_dir.exists()
    archives = list((wt_root / ".harvest-evidence").glob("pr-99-d-wrapper-failure"))
    assert len(archives) == 1
    archived_envelope = json.loads((archives[0] / "completion.json").read_text())
    assert archived_envelope["error_class"] == "PermissionError"


def test_wrapper_removes_lease_state_from_marker(tmp_path: Path) -> None:
    template = marker_template()
    template["state"] = "launching"
    engine_wrapper.run_engine(
        dispatch_id="d-test-001",
        marker_path=str(tmp_path / "dispatch.json"),
        result_path=str(tmp_path / "review.json"),
        envelope_path=str(tmp_path / "completion.json"),
                engine_cmd=["true"],
        marker_template=template,
    )
    marker = json.loads((tmp_path / "dispatch.json").read_text())
    assert "state" not in marker
    assert "pid" in marker


def test_wrapper_forwards_sigterm_to_engine(tmp_path: Path) -> None:
    """SIGTERM to the wrapper must terminate its engine subprocess."""
    import signal
    import subprocess
    import time

    pid_file = tmp_path / "engine.pid"
    # Engine: write PID and sleep. Ignores -o (the wrapper patches it).
    engine_script = (
        f'import os, time, sys; '
        f'open("{pid_file}", "w").write(str(os.getpid())); '
        f'time.sleep(300)'
    )
    wrapper_path = Path(__file__).resolve().parent.parent / "engine_wrapper.py"
    wrapper_cmd = [
        sys.executable, str(wrapper_path), "run",
        "--dispatch-id", "d-sigterm",
        "--marker-path", str(tmp_path / "dispatch.json"),
        "--result-path", str(tmp_path / "review.json"),
        "--envelope-path", str(tmp_path / "completion.json"),
        "--engine", "test", "--stage", "review",
        "--pr-number", "1", "--deadline", "9999999999",
        "--base-ref", "main", "--base-sha", "b1",
        "--head-sha", "h1", "--pre-label", "ai:implemented",
        "--", sys.executable, "-c", engine_script, "-o", "placeholder.json",
    ]

    wrapper_proc = subprocess.Popen(wrapper_cmd, stdin=subprocess.DEVNULL)
    # Wait for the engine to write its PID
    for _ in range(100):
        if pid_file.exists():
            break
        time.sleep(0.05)
    assert pid_file.exists(), "engine never started"
    engine_pid = int(pid_file.read_text())

    os.kill(wrapper_proc.pid, signal.SIGTERM)
    wrapper_proc.wait(timeout=10)

    # The engine must also be dead
    time.sleep(0.5)
    try:
        os.kill(engine_pid, 0)
        engine_alive = True
    except ProcessLookupError:
        engine_alive = False
    assert not engine_alive, "engine must be dead after wrapper receives SIGTERM"


def test_patch_output_flag_replaces_existing() -> None:
    cmd = ["codex", "exec", "-o", "old.json", "prompt"]
    patched = engine_wrapper._patch_output_flag(cmd, "new.json")
    assert patched == ["codex", "exec", "-o", "new.json", "prompt"]


def test_patch_output_flag_appends_when_missing() -> None:
    cmd = ["codex", "exec", "prompt"]
    patched = engine_wrapper._patch_output_flag(cmd, "new.json")
    assert patched == ["codex", "exec", "prompt", "-o", "new.json"]


def test_main_parses_base_tip_sha_into_published_marker(tmp_path: Path) -> None:
    """main() parses --base-tip-sha from argv and publishes it in the marker.

    Argv-level on purpose: injecting the field into marker_template and calling
    run_engine directly would not prove the CLI boundary the dispatcher actually
    crosses (tick.py passes --base-tip-sha as an engine_cmd argument).
    """
    tip_oid = "c" * 40
    engine_wrapper.main([
        "run",
        "--dispatch-id", "d-tip-sha",
        "--marker-path", str(tmp_path / "dispatch.json"),
        "--result-path", str(tmp_path / "review.json"),
        "--envelope-path", str(tmp_path / "completion.json"),
        "--engine", "codex",
        "--stage", "review",
        "--pr-number", "1",
        "--deadline", "9999999999",
        "--base-ref", "main",
        "--base-sha", "b" * 40,
        "--base-tip-sha", tip_oid,
        "--head-sha", "d" * 40,
        "--pre-label", "ai:implemented",
        "--", "true",
    ])
    marker = json.loads((tmp_path / "dispatch.json").read_text())
    assert marker.get("base_tip_sha") == tip_oid


def test_main_preserves_no_change_round_in_published_marker(tmp_path: Path) -> None:
    fix_round = "a" * 64
    engine_wrapper.main([
        "run", "--dispatch-id", "review-after-no-change",
        "--marker-path", str(tmp_path / "dispatch.json"),
        "--result-path", str(tmp_path / "review.json"),
        "--envelope-path", str(tmp_path / "completion.json"),
        "--engine", "codex", "--stage", "review", "--pr-number", "1",
        "--deadline", "9999999999", "--base-ref", "main", "--base-sha", "b" * 40,
        "--head-sha", "d" * 40, "--pre-label", "ai:revised",
        "--fix-round", fix_round, "--", "true",
    ])
    marker = json.loads((tmp_path / "dispatch.json").read_text())
    assert marker["fix_round"] == fix_round
    assert engine_wrapper.pipeline.valid_marker(marker)


def test_main_omits_base_tip_sha_from_marker_when_not_given(tmp_path: Path) -> None:
    """Without --base-tip-sha the published marker must not carry the key at all.

    valid_marker rejects an empty-string base_tip_sha, so writing the default ""
    into the marker would invalidate every dispatch that predates the flag.
    """
    engine_wrapper.main([
        "run",
        "--dispatch-id", "d-no-tip",
        "--marker-path", str(tmp_path / "dispatch.json"),
        "--result-path", str(tmp_path / "review.json"),
        "--envelope-path", str(tmp_path / "completion.json"),
        "--engine", "codex",
        "--stage", "review",
        "--pr-number", "1",
        "--deadline", "9999999999",
        "--base-ref", "main",
        "--base-sha", "b" * 40,
        "--head-sha", "d" * 40,
        "--pre-label", "ai:implemented",
        "--", "true",
    ])
    marker = json.loads((tmp_path / "dispatch.json").read_text())
    assert "base_tip_sha" not in marker


# --- Helpers ---


def marker_template() -> dict[str, object]:
    """Marker template for test wrappers."""
    return {
        "engine": "codex",
        "stage": "review",
        "pr_number": 1,
        "deadline": 9999999999,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
    }


def run_wrapper_with_echo(
    tmp_path: Path, *, exit_code: int, valid_review: bool
) -> int:
    """Run the wrapper using a shell echo command as the engine."""
    if valid_review:
        payload = json.dumps({"verdict": "ready_for_human", "findings": []})
    else:
        payload = json.dumps({"garbage": True})

    # Build a shell command that writes the payload to the -o target and exits.
    # The wrapper will patch -o to point at its temp file.
    script = (
        f'import json, sys; '
        f'args = sys.argv[1:]; '
        f'o = args[args.index("-o") + 1] if "-o" in args else None; '
        f'open(o, "w").write({payload!r}) if o else None; '
        f'sys.exit({exit_code})'
    )

    return engine_wrapper.run_engine(
        dispatch_id="d-test-001",
        marker_path=str(tmp_path / "dispatch.json"),
        result_path=str(tmp_path / "review.json"),
        envelope_path=str(tmp_path / "completion.json"),
                engine_cmd=[sys.executable, "-c", script, "-o", "placeholder.json"],
        marker_template=marker_template(),
    )
