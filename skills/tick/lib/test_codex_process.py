"""Codex-shaped subprocesses through the real tick CLI, worktrees, and harvest."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pipeline

from test_smoke_loop import ENGINE_SHIM, loop_repo  # noqa: F401
from test_review_reports import (  # noqa: F401
    report, report_repo, install_reviewer_shim, run_report_tick, wait_for_artifact,
)
from test_fixer_reports import original, prepare_fixer  # noqa: F401


@pytest.mark.parametrize("selection", [[], ["#7"], ["--only", "#7"]])
def test_codex_cli_override_reaches_full_targeted_and_only_dispatch(loop_repo, selection):
    root, env = loop_repo
    env.update(LOOP_TEST_STAGE="implement", LOOP_TEST_ENGINE="codex", PR_PIPELINE_PYTHON=sys.executable)
    codex = Path(env["PATH"].split(os.pathsep)[0]) / "codex"
    codex.write_text(f"#!{sys.executable}\n" + ENGINE_SHIM)
    (root / "label").write_text("ai:available")
    (root / "engine-exit").touch()
    command = ["bash", str(Path(__file__).parents[1] / "tick.sh"), *selection,
               "--impl-engine", "codex", "--model", "gpt-5.5", "--effort", "high"]
    first = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=40)
    assert first.returncode == 0, first.stderr
    worktree = root / ".claude/worktrees/issue-7"
    wait_for_artifact(worktree / "result.json")
    assert (worktree / ".git").is_file()
    assert subprocess.check_output([env["LOOP_TEST_GIT"], "symbolic-ref", "--short", "HEAD"],
        cwd=worktree, text=True).strip() == "agent/issue-7"
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    [launch] = [call for call in calls if call[0] == "codex"]
    assert "--approve-for-me" in launch and 'model_reasoning_effort="high"' in launch
    assert launch[launch.index("--model") + 1] == "gpt-5.5"
    second = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=40)
    assert second.returncode == 0, second.stderr
    assert (root / "label").read_text() == "ai:needs-human-decision"
    assert not (worktree / "dispatch.json").exists()


@pytest.mark.parametrize("case", ["mixed", "no-change", "unresolved", "missing", "quota", "nonzero"])
def test_codex_fixer_cli_preserves_report_and_recovery_contract(report_repo, original, report, case):
    root, env = report_repo
    prepare_fixer(root, env, original, engine="codex", case=case)
    install_reviewer_shim(root, env, dict(report, verdict="ready_for_human", findings=[]))
    install_writer(env, case)
    run_report_tick(root, env)
    worktree = root / ".claude/worktrees/pr-7"
    wait_for_artifact(worktree / "result.json")
    marker = json.loads((worktree / "dispatch.json").read_text())
    result = json.loads((worktree / "result.json").read_text())
    assert marker["engine"] == result["engine"] == "codex"
    assert marker["report_protocol_version"] == result["report_protocol_version"] == 2
    assert "report_transport" not in marker
    assert "structured_output" not in result
    observation = json.loads((root / "writer-launch.json").read_text())
    assert observation["cwd"] == str(worktree)
    assert observation["detached"] is True
    assert "--approve-for-me" in observation["argv"]
    assert not {"--permission-mode", "--settings", "--allowedTools", "--json-schema"} & set(observation["argv"])
    prompt = (worktree / "prompt.txt").read_text()
    assert "final message must contain only the JSON report" in prompt
    assert "Claude Code collects it" not in prompt
    assert observation["stdin"] and observation["installed_skill"]
    run_report_tick(root, env)
    if case in {"mixed", "no-change"}:
        wait_for_artifact(worktree / "completion.json")
        run_report_tick(root, env)
        assert (root / "label").read_text() == pipeline.LABEL_READY_FOR_HUMAN
    else:
        assert (root / "label").read_text() == pipeline.LABEL_NEEDS_HUMAN
    assert (root / "fixer-launches").read_text() == "1"
    assert list((root / ".claude/worktrees/.harvest-evidence").glob("pr-7-*/fix-manifest.json"))
    run_report_tick(root, env)
    assert (root / "fixer-launches").read_text() == "1"


@pytest.mark.parametrize("fault", ["label", "comment"])
def test_codex_failed_publication_retains_source_for_restart(report_repo, original, report, fault):
    root, env = report_repo
    prepare_fixer(root, env, original, engine="codex")
    install_reviewer_shim(root, env, dict(report, verdict="ready_for_human", findings=[]))
    install_writer(env, "mixed")
    run_report_tick(root, env)
    worktree = root / ".claude/worktrees/pr-7"
    wait_for_artifact(worktree / "result.json")
    before = (worktree / "result.json").read_bytes()
    fault_path = root / f"fail-{fault}"
    fault_path.touch()
    run_report_tick(root, env)
    assert (worktree / "dispatch.json").is_file()
    assert (worktree / "result.json").read_bytes() == before
    fault_path.unlink()
    run_report_tick(root, env)
    wait_for_artifact(worktree / "completion.json")
    run_report_tick(root, env)
    assert (root / "label").read_text() == pipeline.LABEL_READY_FOR_HUMAN
    assert (root / "fixer-launches").read_text() == "1"


@pytest.mark.parametrize("stage", ["implement", "fix"])
def test_codex_interruption_retains_live_custody_and_archives_after_death(loop_repo, stage):
    root, env = loop_repo
    env.update(LOOP_TEST_STAGE=stage, LOOP_TEST_ENGINE="codex", PR_PIPELINE_PYTHON=sys.executable)
    codex = Path(env["PATH"].split(os.pathsep)[0]) / "codex"
    codex.write_text(f"#!{sys.executable}\n" + ENGINE_SHIM.replace(
        'root = Path(os.environ["LOOP_TEST_ROOT"])',
        'print(json.dumps({"type": "turn.started"}), flush=True)\n'
        'root = Path(os.environ["LOOP_TEST_ROOT"])'))
    kind = "issue" if stage == "implement" else "pr"
    label = root / "label"
    label.write_text("ai:available" if stage == "implement" else "ai:changes-requested")
    command = ["bash", str(Path(__file__).parents[1] / "tick.sh"),
               "#7" if stage == "implement" else "pr:7", "--impl-engine", "codex"]
    worktree = root / f".claude/worktrees/{kind}-7"
    marker_path = worktree / "dispatch.json"
    pgid = None
    try:
        first = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=40)
        assert first.returncode == 0, first.stderr
        marker = json.loads(marker_path.read_text())
        pgid = marker["pgid"]
        deadline = time.monotonic() + 10
        while not (worktree / "heartbeat").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (worktree / "heartbeat").exists()  # Heartbeats use mtime, not content.
        assert (worktree / "result.json").read_bytes() == b""
        # A human hold cannot release an active process or permit a second launch.
        label.write_text("ai:blocked")
        held = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=40)
        assert held.returncode == 0, held.stderr
        assert marker_path.is_file()
        os.killpg(pgid, signal.SIGKILL)
        time.sleep(0.2)
        recovered = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=40)
        assert recovered.returncode == 0, recovered.stderr
        assert label.read_text() == "ai:blocked"
        assert not marker_path.exists()
        assert list((root / ".claude/worktrees/.harvest-evidence").glob(f"{kind}-7-*/dispatch.json"))
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        assert sum(call[0] == "codex" for call in calls) == 1
    finally:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# --- Helpers ---


def install_writer(env, case):
    directory = Path(env["PATH"].split(os.pathsep)[0])
    codex = directory / "codex"
    reviewer = codex.with_name("codex-review")
    reviewer.write_text(codex.read_text())
    reviewer.chmod(0o755)
    writer = (directory / "claude").read_text().split("\n", 1)[1]
    writer = writer.replace("print(json.dumps(event))", '''
if event.get("is_error"):
    print(json.dumps({"type": "turn.failed", "error": {"message": event["result"]}}))
else:
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": event["result"]}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 4}}))
''')
    writer = writer.replace("prompt = sys.stdin.read()", '''prompt = sys.stdin.read()
(root / "writer-launch.json").write_text(json.dumps({
    "argv": sys.argv[1:], "cwd": str(Path.cwd()), "stdin": bool(prompt),
    "detached": subprocess.run([os.environ["LOOP_TEST_GIT"], "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 0,
    "installed_skill": Path(".agents/skills/pr-pipeline-definition-of-done/SKILL.md").is_file(),
}))''')
    prefix = f'''import os, sys
if "--output-schema" in sys.argv:
    os.execv({str(reviewer)!r}, [{str(reviewer)!r}, *sys.argv[1:]])
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.171.0")
    raise SystemExit(0)
'''
    codex.write_text(f"#!{sys.executable}\n" + prefix + writer
                     + ("\nraise SystemExit(1)\n" if case == "nonzero" else ""))
