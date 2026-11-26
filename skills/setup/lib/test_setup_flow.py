"""Tests for pr-pipeline setup: route probe, configuration writes, adoption steps.

The probe and the CLI run as real subprocesses against local ``gh`` and
``claude`` doubles on ``PATH``. A helper-level test could not show that the
probe launches the way dispatch does, or that ``git init`` really ran in the
probe directory rather than being claimed in the model's reply.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

import pipeline
import setup_flow
import tick

SETUP_PY = Path(setup_flow.__file__).resolve()
TICK_PY = Path(tick.__file__).resolve()
REPO = "owner/widget"


@pytest.fixture
def doubles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Install stateful ``gh`` and behaviour-scripted ``claude`` doubles."""
    bin_dir = tmp_path / "doubles"
    bin_dir.mkdir()
    for name, source in (("gh", GH_DOUBLE), ("claude", CLAUDE_DOUBLE)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)
    codex = bin_dir / "codex"
    codex.write_text('#!/bin/sh\nprintf "codex-cli 0.142.5\\n"\n')
    codex.chmod(0o755)
    state = tmp_path / "github.json"
    state.write_text(json.dumps({
        "login": "operator-a",
        "labels": ["bug"],
        "repo": {
            "full_name": "Owner/Widget",
            "default_branch": "trunk",
            "files": {
                "package.json": json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}),
                "pnpm-lock.yaml": "",
                "AGENTS.md": "Run `pnpm run test` before every commit.",
            },
        },
    }))
    gh_log = tmp_path / "gh-calls.jsonl"
    claude_log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SETUP_GH_STATE", str(state))
    monkeypatch.setenv("SETUP_GH_LOG", str(gh_log))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(claude_log))
    monkeypatch.setenv("FAKE_PROBE_COMMAND", setup_flow.PROBE_COMMAND)
    set_route(monkeypatch, dontAsk="deny", auto="run")
    return {"state": state, "gh_log": gh_log, "claude_log": claude_log, "bin": bin_dir}


@pytest.mark.parametrize("stage", ["implement", "fix"])
@pytest.mark.parametrize("denied", [False, True])
def test_codex_probe_exercises_verification_commit_and_push(tmp_path, monkeypatch, stage, denied):
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\n" + '''import json, subprocess, sys
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.171.0")
    raise SystemExit(0)
assert "--approve-for-me" in sys.argv and "--sandbox" not in sys.argv
assert "--permission-mode" not in sys.argv
prompt = sys.stdin.read()
assert "request approval" in prompt
''' + ('''print(json.dumps({"type": "turn.failed", "error": {"message": "permission denied"}}))
raise SystemExit(1)
''' if denied else '''p = subprocess.run(["sh", "probe.sh"], capture_output=True, text=True)
print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "sh probe.sh", "exit_code": p.returncode, "aggregated_output": p.stdout + p.stderr}}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "probe finished"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
raise SystemExit(p.returncode)
'''))
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    report = setup_flow.run_probe("owner/repo", impl_engine="codex", model="gpt-5.5",
        effort="high", stage=stage, verify_tools=[])
    assert (report["outcome"] == setup_flow.PASS) != denied
    assert report["engine_version"] == "codex-cli 0.171.0"
    assert report["stage"] == stage
    assert report["authenticated_push_proven"] is False
    if denied:
        assert "permission denied" in report["detail"]


def test_codex_probe_uses_dispatch_policy_and_records_both_effective_routes(monkeypatch):
    import setup_flow
    import codex_write_wrapper

    seen = []
    def run(repo, **kwargs):
        seen.append(kwargs)
        return {"outcome": setup_flow.PASS, "detail": "fixture", **kwargs}
    monkeypatch.setattr(setup_flow, "run_probe", run)
    report = setup_flow.run_route_probes("owner/repo", routes={
        "impl_engine": "codex", "impl_model": "gpt-5.5", "impl_effort": "xhigh",
        "fix_engine": "claude", "fix_model": "sonnet", "fix_effort": "high",
    }, verify_tools=[], engine_settings="")
    assert report["outcome"] == setup_flow.PASS
    assert [(r["impl_engine"], r["model"], r["stage"]) for r in seen] == [
        ("codex", "gpt-5.5", "implement"), ("claude", "sonnet", "fix")]
    assert setup_flow.probe_argv("codex", "auto", "ignored", "gpt-5.5", effort="xhigh") == codex_write_wrapper.engine_argv(model="gpt-5.5", effort="xhigh")


def test_codex_route_probe_cannot_pass_after_a_denied_fixer(monkeypatch):
    import setup_flow

    monkeypatch.setattr(setup_flow, "run_probe", lambda repo, **kwargs: {
        "outcome": setup_flow.PASS if kwargs["stage"] == "implement" else setup_flow.AUTO_FAILED,
        "detail": "permission denied",
    })
    report = setup_flow.run_route_probes("owner/repo", routes={"impl_engine": "codex"},
        verify_tools=[], engine_settings="")
    assert report["outcome"] != setup_flow.PASS


def test_setup_cli_resolves_both_routes_and_retains_effort(monkeypatch, tmp_path):
    config = tmp_path / "operator.toml"
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(config))
    captured = []
    def probes(repo, **kwargs):
        captured.append(kwargs)
        return {"outcome": setup_flow.PASS}
    monkeypatch.setattr(setup_flow, "run_route_probes", probes)
    assert setup_flow.main(["probe", "owner/repo", "--impl-engine", "codex", "--model", "gpt-5.5",
        "--effort", "xhigh", "--fix-engine", "claude", "--fix-model", "sonnet", "--fix-effort", "high"]) == 0
    assert captured[0]["routes"] == {"impl_engine": "codex", "impl_model": "gpt-5.5", "impl_effort": "xhigh",
        "fix_engine": "claude", "fix_model": "sonnet", "fix_effort": "high"}


def test_setup_writes_exact_measured_stage_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(tmp_path / "operator.toml"))
    routes = {"impl_engine": "codex", "model": "gpt-5.5", "effort": "xhigh",
              "fix_engine": "claude", "fix_model": "sonnet", "fix_effort": "high",
              "review_model": "", "review_effort": ""}
    requested = {"impl_engine": "codex", "impl_model": "gpt-5.5", "impl_effort": "xhigh",
                 "fix_engine": "claude", "fix_model": "sonnet", "fix_effort": "high"}
    monkeypatch.setattr(setup_flow, "_read_probe_record", lambda repo: {
        "impl_engine": "codex", "model": "gpt-5.5", "verify_tools": [],
        "outcome": setup_flow.PASS, "routes": routes, "requested_routes": requested,
        "stages": {s: {"outcome": setup_flow.PASS} for s in ("implement", "fix")},
    })
    _, _, merged, _ = setup_flow.plan_config("owner/repo", login="owner", verify_tools=[],
        permission_mode="auto", base_ref="main")
    assert {k: merged["repos"]["owner/repo"].get(k) for k in requested} == requested


@pytest.mark.parametrize("requested", [
    None,
    {"impl_engine": "codex", "impl_model": "gpt-5.5", "impl_effort": "xhigh",
     "fix_engine": "", "fix_model": "", "fix_effort": ""},
], ids=["missing-request", "request-resolves-elsewhere"])
def test_setup_refuses_a_record_whose_request_does_not_match_the_measurement(
    monkeypatch, tmp_path, requested,
):
    """Persisting a request needs evidence that it resolves to the probed routes."""
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(tmp_path / "operator.toml"))
    routes = {"impl_engine": "codex", "model": "gpt-5.5", "effort": "xhigh",
              "fix_engine": "claude", "fix_model": "sonnet", "fix_effort": "high"}
    record = {"impl_engine": "codex", "model": "gpt-5.5", "verify_tools": [],
              "outcome": setup_flow.PASS, "routes": routes,
              "stages": {s: {"outcome": setup_flow.PASS} for s in ("implement", "fix")}}
    if requested is not None:
        record["requested_routes"] = requested
    monkeypatch.setattr(setup_flow, "_read_probe_record", lambda repo: record)
    with pytest.raises(setup_flow.SetupRefused, match="rerun the probe"):
        setup_flow.plan_config("owner/repo", login="owner", verify_tools=[],
            permission_mode="auto", base_ref="main")


def test_setup_probe_uses_existing_effective_routes_when_flags_are_absent(monkeypatch, tmp_path):
    config = tmp_path / "operator.toml"
    config.write_text('[repos."owner/repo"]\nimpl_engine="codex"\nmodel="gpt-5.5"\n'
                      'effort="high"\nfix_engine="claude"\nfix_model="sonnet"\n')
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(config))
    captured = []
    def probes(repo, **kwargs):
        captured.append(kwargs["routes"])
        return {"outcome": setup_flow.PASS}
    monkeypatch.setattr(setup_flow, "run_route_probes", probes)
    assert setup_flow.main(["probe", "owner/repo"]) == 0
    assert captured[0]["impl_engine"] == "codex"
    assert captured[0]["fix_engine"] == "claude"
    assert captured[0]["fix_model"] == "sonnet"


@pytest.mark.parametrize("codex_stage", ["implement", "fix"])
def test_dontask_cannot_bypass_a_failed_codex_stage_probe(monkeypatch, tmp_path, codex_stage):
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(tmp_path / "operator.toml"))
    routes = {"impl_engine": "codex" if codex_stage == "implement" else "claude",
              "fix_engine": "codex", "model": ""}
    monkeypatch.setattr(setup_flow, "_read_probe_record", lambda repo: {
        **routes, "verify_tools": ["sh ./check.sh"], "outcome": setup_flow.AUTO_FAILED,
        "routes": routes, "stages": {s: {"outcome": setup_flow.AUTO_FAILED if s == codex_stage else setup_flow.PASS}
                                      for s in ("implement", "fix")},
    })
    with pytest.raises(setup_flow.SetupRefused, match="Codex"):
        setup_flow.plan_config("owner/repo", login="owner", verify_tools=["sh ./check.sh"],
            permission_mode="dontAsk", base_ref="main")


# --- The probe command is not a grant ---


@pytest.mark.parametrize("verify_tools", [(), ("pnpm run test *", "pnpm run lint *")])
def test_probe_command_is_absent_from_the_allowlist_the_probe_launches_with(
    verify_tools: tuple[str, ...],
) -> None:
    """The probe measures classifier approval only for a command nobody granted.

    Built from the argv the probe actually launches, so widening the engine's
    base grants to cover the probe command fails here rather than turning every
    later probe into a pass that measured nothing.
    """
    argv = setup_flow.probe_argv(
        "claude", "auto", setup_flow.probe_allowed_tools(verify_tools), "",
    )
    allowed = argv[argv.index("--allowedTools") + 1]

    assert allowed == tick._engine_allowed_tools(
        tick._implementer_push_permission(setup_flow.PROBE_REMOTE),
        tuple(f"Bash({tool})" for tool in verify_tools),
    )
    assert setup_flow.allowlist_grants(setup_flow.PROBE_COMMAND, allowed) is None


@pytest.mark.parametrize("entry", [
    "Bash", "Bash(git init)", "Bash(git *)", "Bash(git init *)", "Bash(git:*)",
    "Bash(*)",
])
def test_allowlist_matcher_recognises_every_grant_shape_covering_the_command(
    entry: str,
) -> None:
    allowed = f"Read Edit {entry} Bash(git status)"
    assert setup_flow.allowlist_grants(setup_flow.PROBE_COMMAND, allowed) == entry


def test_widened_base_grants_make_the_probe_refuse_to_measure(
    doubles: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tick, "_ENGINE_ALLOWED_TOOLS_BASE", tick._ENGINE_ALLOWED_TOOLS_BASE + " Bash(git *)",
    )

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.INCONCLUSIVE
    assert "Bash(git *)" in result["detail"]
    assert not doubles["claude_log"].exists()


# --- The probe and its negative control ---


def test_probe_passes_only_when_denied_under_dontask_and_run_under_auto(
    doubles: dict[str, Path],
) -> None:
    result = setup_flow.run_probe(
        REPO, impl_engine="claude", model="", verify_tools=("pnpm run test *",),
    )

    assert result["outcome"] == setup_flow.PASS, result
    calls = claude_calls(doubles)
    assert [mode_of(call) for call in calls] == ["dontAsk", "auto"]
    expected_allowed = setup_flow.probe_allowed_tools(("pnpm run test *",))
    for call in calls:
        argv = call["argv"]
        assert argv[0] == "-p"
        assert argv[argv.index("--allowedTools") + 1] == expected_allowed
        assert "--permission-prompt-tool" not in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert call["stdin_isatty"] is False
        assert setup_flow.PROBE_COMMAND in call["prompt"]
        # The model is never told the answer it must return.
        assert call["cwd"] not in call["prompt"]
    assert calls[0]["cwd"] != calls[1]["cwd"]


def test_probe_fails_when_auto_denies_the_command(doubles, monkeypatch) -> None:
    set_route(monkeypatch, dontAsk="deny", auto="deny")

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.AUTO_FAILED
    assert "denied" in result["detail"]


def test_probe_reports_success_under_both_modes_as_unmeasurable(doubles, monkeypatch) -> None:
    """Success under ``dontAsk`` means the allowlist is not enforced at all.

    A Claude sandbox with ``autoAllowBashIfSandboxed`` does exactly this. An
    ``auto`` success on such a host says nothing about the classifier, so it
    is neither a pass nor an ``auto`` denial.
    """
    set_route(monkeypatch, dontAsk="run", auto="run")

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.UNMEASURABLE
    assert result["outcome"] not in (setup_flow.PASS, setup_flow.AUTO_FAILED)
    assert "allowlist is not being enforced" in result["detail"]
    assert "sandbox.autoAllowBashIfSandboxed" in result["detail"]
    assert setup_flow.SANDBOX_OFF_REMEDY in result["detail"]


def test_probe_treats_a_permitted_but_failed_control_as_unmeasurable(
    doubles, monkeypatch,
) -> None:
    """A tool error is not a permission denial.

    Measured on a sandboxed host: ``dontAsk`` let ``git init`` through and the
    OS then refused the write. Counting that error as the control holding made
    the probe blame ``auto`` for a denial that never happened. Only the
    harness's ``permission_denials`` record is a denial.
    """
    set_route(monkeypatch, dontAsk="fail", auto="run")

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.UNMEASURABLE
    assert "Operation not permitted" in result["detail"]
    assert "sandbox.autoAllowBashIfSandboxed" in result["detail"]
    assert setup_flow.SANDBOX_OFF_REMEDY in result["detail"]
    assert [mode_of(call) for call in claude_calls(doubles)] == ["dontAsk"]


def test_probe_launches_both_runs_with_the_engine_settings_dispatch_will_use(
    doubles, monkeypatch,
) -> None:
    """The #19 L3 host: sandboxed, with Bash auto-allowed inside the sandbox.

    Without engine settings the control is let through and fails. With the
    remedy setup names, the same route is measurable and passes, and the
    record carries the settings so write-config can hold dispatch to them.
    """
    set_route(monkeypatch, dontAsk="fail", auto="run",
              **{"dontAsk:unsandboxed": "deny"})
    settings = '{"sandbox":{"enabled":false}}'

    result = run_setup("probe", REPO, "--engine-settings", settings)

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["outcome"] == setup_flow.PASS
    assert report["engine_settings"] == settings
    calls = claude_calls(doubles)
    assert [mode_of(call) for call in calls] == ["dontAsk", "auto"]
    for call in calls:
        argv = call["argv"]
        assert argv[argv.index("--settings") + 1] == settings


def test_probe_refuses_engine_settings_the_tick_would_refuse(doubles) -> None:
    result = run_setup("probe", REPO, "--engine-settings", "settings.json")

    assert result.returncode == 1
    assert "engine_settings" in result.stderr
    assert not doubles["claude_log"].exists()


def test_probe_refuses_a_settings_file_that_is_not_a_json_object(
    doubles, tmp_path: Path,
) -> None:
    settings_file = tmp_path / "engine-settings.json"
    settings_file.write_text('{"sandbox": ')

    result = run_setup("probe", REPO, "--engine-settings", str(settings_file))

    assert result.returncode == 1
    assert "engine_settings" in result.stderr
    assert not doubles["claude_log"].exists()


def test_probe_reports_an_approved_but_failed_auto_run_as_inconclusive(
    doubles, monkeypatch,
) -> None:
    set_route(monkeypatch, dontAsk="deny", auto="fail")

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.INCONCLUSIVE
    assert "approved" in result["detail"]
    assert "Operation not permitted" in result["detail"]


def test_probe_needs_the_harness_denial_record_to_trust_the_control(
    doubles, monkeypatch,
) -> None:
    """Without a result event there is no evidence the control was denied."""
    set_route(monkeypatch, dontAsk="deny_unrecorded", auto="run")

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.INCONCLUSIVE


def test_probe_does_not_accept_output_the_model_only_claims(doubles, monkeypatch) -> None:
    """Only the harness's tool result and the command's side effect count."""
    set_route(monkeypatch, dontAsk="deny", auto="claim")

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.AUTO_FAILED


def test_probe_is_inconclusive_when_the_control_never_attempts_the_command(
    doubles, monkeypatch,
) -> None:
    set_route(monkeypatch, dontAsk="silent", auto="run")

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.INCONCLUSIVE
    assert [mode_of(call) for call in claude_calls(doubles)] == ["dontAsk"]


def test_probe_fails_a_route_whose_cli_rejects_auto(doubles, monkeypatch) -> None:
    set_route(monkeypatch, dontAsk="deny", auto="reject")

    result = setup_flow.run_probe(REPO, impl_engine="claude", model="", verify_tools=())

    assert result["outcome"] == setup_flow.AUTO_FAILED
    assert "auto mode is unavailable" in result["detail"]


def test_probe_launches_the_deepseek_route_the_way_dispatch_does(
    doubles, monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "a" * 20)

    result = setup_flow.run_probe(REPO, impl_engine="claude-ds", model="", verify_tools=())

    assert result["outcome"] == setup_flow.PASS
    for call in claude_calls(doubles):
        assert call["env"]["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
        assert call["argv"][call["argv"].index("--model") + 1] == "deepseek-v4-pro[1m]"


def test_probe_cli_records_evidence_beside_the_operator_config(doubles) -> None:
    result = run_setup("probe", REPO, "--verify-tool", "pnpm run test *")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["outcome"] == setup_flow.PASS
    evidence = json.loads(setup_flow.probe_record_path(REPO).read_text())
    assert evidence["outcome"] == setup_flow.PASS
    assert evidence["verify_tools"] == ["pnpm run test *"]
    assert evidence["impl_engine"] == "claude"
    assert setup_flow.probe_record_path(REPO).parent.parent.parent == (
        pipeline.operator_config_path().parent
    )


def test_probe_rejects_a_repository_that_escapes_evidence_directory(doubles) -> None:
    config_dir = pipeline.operator_config_path().parent
    result = run_setup("probe", "../../../target/config")

    assert result.returncode == 1
    assert '"owner/name" repository' in result.stderr
    assert not (config_dir / "setup-probes").exists()
    assert not doubles["claude_log"].exists()


def test_probe_record_rejects_a_symlink_outside_evidence_directory(doubles) -> None:
    root = pipeline.operator_config_path().parent / "setup-probes"
    root.mkdir(parents=True)
    (root / "owner").symlink_to(root.parent, target_is_directory=True)

    result = run_setup("probe", REPO)

    assert result.returncode == 1
    assert "outside setup-probes" in result.stderr
    assert not (root.parent / "widget.json").exists()


# --- Recording the operator's choice ---


def test_write_config_refuses_empty_verify_tools_on_a_route_that_failed(doubles, monkeypatch) -> None:
    set_route(monkeypatch, dontAsk="deny", auto="deny")
    assert run_setup("probe", REPO).returncode == 1

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "dontAsk")

    assert result.returncode == 1
    assert "name the target's verification commands" in result.stderr
    assert not pipeline.operator_config_path().exists()


def test_write_config_refuses_auto_on_a_route_that_failed(doubles, monkeypatch) -> None:
    set_route(monkeypatch, dontAsk="deny", auto="deny")
    assert run_setup("probe", REPO, "--verify-tool", "pnpm run test *").returncode == 1

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--verify-tool", "pnpm run test *", "--permission-mode", "auto")

    assert result.returncode == 1
    assert "auto" in result.stderr
    assert not pipeline.operator_config_path().exists()


def test_write_config_records_the_dontask_fallback_with_named_grants(doubles, monkeypatch) -> None:
    set_route(monkeypatch, dontAsk="deny", auto="deny")
    assert run_setup("probe", REPO, "--verify-tool", "pnpm run test *").returncode == 1

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--verify-tool", "pnpm run test *", "--permission-mode", "dontAsk")

    assert result.returncode == 0, result.stderr
    config = pipeline.resolve_config(REPO)
    assert config.errors == ()
    assert config.repo_configured
    assert config.permission_mode == "dontAsk"
    assert config.verify_tools == ("pnpm run test *",)
    assert config.login == "operator-a"


def test_write_config_refuses_grants_other_than_the_ones_probed(doubles) -> None:
    assert run_setup("probe", REPO).returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--verify-tool", "make test", "--permission-mode", "auto")

    assert result.returncode == 1
    assert "rerun the probe" in result.stderr


def test_write_config_records_the_engine_settings_the_probe_used(
    doubles, monkeypatch,
) -> None:
    set_route(monkeypatch, dontAsk="fail", auto="run",
              **{"dontAsk:unsandboxed": "deny"})
    settings = '{"sandbox":{"enabled":false}}'
    assert run_setup("probe", REPO, "--engine-settings", settings).returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto", "--engine-settings", settings)

    assert result.returncode == 0, result.stderr
    config = pipeline.resolve_config(REPO)
    assert config.errors == ()
    assert config.permission_mode == "auto"
    assert config.engine_settings == settings


def test_write_config_keeps_blank_engine_settings_over_a_nonblank_default(
    doubles,
) -> None:
    """A probe without settings pins "no flag", not whatever the default says."""
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        '[operator]\nlogin = "operator-a"\n'
        "\n[defaults]\nengine_settings = '{\"sandbox\":{\"enabled\":false}}'\n",
    )
    assert run_setup("probe", REPO, "--engine-settings", "").returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto", "--engine-settings", "")

    assert result.returncode == 0, result.stderr
    config = pipeline.resolve_config(REPO)
    assert config.errors == ()
    assert config.engine_settings == ""


@pytest.mark.parametrize(("probed", "written"), [
    ((), ("--engine-settings", '{"sandbox":{"enabled":false}}')),
    (("--engine-settings", '{"sandbox":{"enabled":false}}'), ()),
])
def test_write_config_refuses_engine_settings_other_than_the_ones_probed(
    doubles, probed: tuple[str, ...], written: tuple[str, ...],
) -> None:
    assert run_setup("probe", REPO, *probed).returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto", *written)

    assert result.returncode == 1
    assert "engine_settings" in result.stderr
    assert "rerun the probe" in result.stderr
    assert not pipeline.operator_config_path().exists()


@pytest.mark.parametrize(("probe_flags", "route"), [
    ((), ("claude", "")),
    (("--model", "opus"), ("claude", "opus")),
    # claude-ds launches its pinned model when none is named, so that is the
    # model the probe measured and the one recorded.
    (("--impl-engine", "claude-ds"), ("claude-ds", "deepseek-v4-pro[1m]")),
])
def test_write_config_records_the_route_the_probe_measured(
    doubles, monkeypatch, probe_flags: tuple[str, ...], route: tuple[str, str],
) -> None:
    """The repository's section names the route its probe result is for.

    The tick runs that route by default and reports any other route it is
    told to run, so a measurement is never silently applied to a route it
    was not taken on.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "a" * 20)
    assert run_setup("probe", REPO, *probe_flags).returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto")

    assert result.returncode == 0, result.stderr
    config = pipeline.resolve_config(REPO)
    assert config.errors == ()
    assert config.probed_route == route
    assert (config.impl_engine, pipeline.effective_engine_model(config.impl_engine, config.model)) == route


@pytest.mark.parametrize(("engine", "model"), [
    ("claude", "opus"),
    ("claude", ""),
    ("claude-ds", "deepseek-v4-flash"),
])
def test_config_route_edit_preserves_probe_evidence_and_warns_at_startup(
    doubles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    engine: str, model: str,
) -> None:
    """Exercise setup and tick wrappers across an edit and a fresh probe."""
    checkout = target_checkout(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "a" * 20)
    setup_sh = SETUP_PY.parents[1] / "setup.sh"
    tick_sh = TICK_PY.parents[1] / "tick.sh"

    def setup(*args: str) -> None:
        result = subprocess.run(
            ["bash", str(setup_sh), *args], cwd=checkout,
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr

    def tick_once() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(tick_sh)], cwd=checkout,
            capture_output=True, text=True, timeout=120,
        )

    setup("labels", REPO)
    setup("probe", REPO, "--model", "sonnet")
    setup("write-config", REPO, "--login", "operator-a",
          "--permission-mode", "auto")
    before = tick_once()
    assert before.returncode == 0, before.stderr
    assert "differs from the route" not in before.stderr
    record = setup_flow.probe_record_path(REPO).read_bytes()
    path = pipeline.operator_config_path()
    document = tomllib.loads(path.read_text())
    section = document["repos"][REPO]
    section["impl_engine"], section["impl_model"] = engine, model
    path.write_text(setup_flow.dump_toml(document))

    for _ in range(2):
        changed = tick_once()
        assert changed.returncode == 0, changed.stderr
        assert "differs from the route /pr-pipeline:setup probed" in changed.stderr
        assert "claude with model sonnet" in changed.stderr
        assert setup_flow.probe_record_path(REPO).read_bytes() == record
    config = pipeline.resolve_config(REPO)
    assert config.probed_route == ("claude", "sonnet")
    assert (config.impl_engine, config.model) == (engine, model)
    logs = checkout / ".claude" / "worktrees" / ".tick-log"
    events = [json.loads(line) for path in logs.glob("*.jsonl")
              for line in path.read_text().splitlines()]
    assert sum(e.get("action") == "startup_route_unprobed" for e in events) == 2

    setup("probe", REPO, "--impl-engine", engine, "--model", model)
    setup("write-config", REPO, "--login", "operator-a",
          "--permission-mode", "auto")
    refreshed = tick_once()
    assert refreshed.returncode == 0, refreshed.stderr
    assert "differs from the route" not in refreshed.stderr
    assert pipeline.resolve_config(REPO).probed_route == (engine, model)


@pytest.mark.parametrize(("configured", "tick_flags", "expected"), [
    pytest.param('fix_engine=""\nfix_model=""\nfix_effort=""\n', ("--model", "opus"),
                 ("claude", "--model", "opus"), id="inherited-model"),
    pytest.param('effort="high"\n', ("--effort", "low"),
                 ("claude", "effort", "low"), id="inherited-effort"),
    pytest.param("", ("--impl-engine", "codex", "--model", "gpt-5.5"),
                 ("codex", "--model", "gpt-5.5"), id="inherited-engine"),
    pytest.param('fix_engine="claude"\nfix_model="sonnet"\n', ("--model", "opus"),
                 ("claude", "--model", "sonnet"), id="explicit-fixer-control"),
])
def test_setup_preserves_fixer_inheritance_through_actual_dispatch(
    doubles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    configured: str, tick_flags: tuple[str, ...], expected: tuple[str, str, str],
) -> None:
    """A blank fixer key stays a rule after setup, not a pin of today's route.

    Real setup.sh probe and write-config run against an existing section, then
    the written route drives a real tick.sh fixer dispatch with an override.
    """
    import test_smoke_loop

    config = pipeline.operator_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[operator]\nlogin = "operator-a"\n'
                      f'[repos."{REPO}"]\nimpl_engine="claude"\nmodel="sonnet"\n{configured}')
    setup_sh = SETUP_PY.parents[1] / "setup.sh"
    for args in (("probe", REPO, "--impl-engine", "claude", "--model", "sonnet"),
                 ("write-config", REPO, "--login", "operator-a", "--permission-mode", "auto")):
        done = subprocess.run(["bash", str(setup_sh), *args], capture_output=True,
                              text=True, timeout=120)
        assert done.returncode == 0, done.stderr
    section = tomllib.loads(config.read_text())["repos"][REPO]
    for key in ("fix_engine", "fix_model", "fix_effort"):
        written = re.search(rf'^{key}="([^"]*)"$', configured, re.MULTILINE)
        assert section.get(key, "") == (written.group(1) if written else "")

    (tmp_path / "dispatch").mkdir()
    target, env = test_smoke_loop.make_loop_repo(tmp_path / "dispatch")
    env["LOOP_TEST_STAGE"] = "fix"
    # Each binary names itself in calls.jsonl, so the log shows which ran.
    codex = Path(env["PATH"].split(os.pathsep)[0]) / "codex"
    codex.write_text(f"#!{sys.executable}\nimport os\nos.environ['LOOP_TEST_ENGINE'] = 'codex'\n"
                     + test_smoke_loop.ENGINE_SHIM)
    with Path(env["PR_PIPELINE_CONFIG"]).open("a") as target_config:
        for key in ("impl_engine", "model", "effort", "fix_engine", "fix_model", "fix_effort"):
            if key in section:
                target_config.write(f"{key}={json.dumps(section[key])}\n")
    (target / "label").write_text("ai:changes-requested")
    (target / "engine-exit").touch()
    launched = subprocess.run(
        ["bash", str(TICK_PY.parents[1] / "tick.sh"), "pr:7", *tick_flags],
        cwd=target, env=env, capture_output=True, text=True, timeout=120,
    )
    assert launched.returncode == 0, launched.stderr
    deadline = time.monotonic() + 30
    while not (target / "engine-started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    calls = [json.loads(line) for line in (target / "calls.jsonl").read_text().splitlines()]
    launches = [call for call in calls if call[0] in ("claude", "codex")]
    assert [call[0] for call in launches] == [expected[0]]
    if expected[1] == "effort":
        assert (target / "engine-effort").read_text() == expected[2]
    else:
        argv = launches[0]
        assert argv[argv.index(expected[1]) + 1] == expected[2]


def test_write_config_refuses_a_probe_record_with_an_unusable_route(doubles) -> None:
    """A probe record naming no supported route cannot become configuration."""
    assert run_setup("probe", REPO).returncode == 0
    record = setup_flow.probe_record_path(REPO)
    evidence = json.loads(record.read_text())
    evidence["impl_engine"] = "unknown"
    record.write_text(json.dumps(evidence))

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto")

    assert result.returncode == 1
    assert "impl_engine" in result.stderr
    assert not pipeline.operator_config_path().exists()


def test_write_config_refuses_without_any_probe(doubles) -> None:
    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--verify-tool", "make test", "--permission-mode", "dontAsk")

    assert result.returncode == 1
    assert "no route probe" in result.stderr


def test_write_config_accepts_empty_grants_after_a_passing_probe(doubles) -> None:
    assert run_setup("probe", REPO).returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto")

    assert result.returncode == 0, result.stderr
    config = pipeline.resolve_config(REPO)
    assert config.repo_configured and config.verify_tools == ()
    assert config.permission_mode == "auto"


def test_write_config_keeps_everything_else_the_operator_wrote(doubles) -> None:
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        '[operator]\nlogin = "operator-a"\n'
        '\n[defaults]\nverify_tools = ["make check"]\nallow_touching = false\n'
        '\n[repos."other/app"]\nbase_ref = "master"\n'
        '\n[repos."OWNER/WIDGET"]\nchecklist_paths = ["docs/review.md"]\n',
    )
    assert run_setup("probe", REPO, "--verify-tool", "pnpm run test *").returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--verify-tool", "pnpm run test *", "--permission-mode", "auto")

    assert result.returncode == 0, result.stderr
    written = tomllib.loads(path.read_text())
    assert written["defaults"] == {"verify_tools": ["make check"], "allow_touching": False}
    assert written["repos"]["other/app"] == {"base_ref": "master"}
    # The existing section is updated in place, whatever case it was written in.
    assert set(written["repos"]) == {"other/app", "OWNER/WIDGET"}
    assert written["repos"]["OWNER/WIDGET"] == {
        "checklist_paths": ["docs/review.md"],
        "verify_tools": ["pnpm run test *"],
        "permission_mode": "auto",
        "engine_settings": "",
        "impl_engine": "claude",
        "impl_model": "",
        "probed_impl_engine": "claude",
        "probed_model": "",
    }
    assert path.with_name("config.toml.bak").exists()


def test_write_config_reports_a_repository_outside_the_scan_scope(doubles) -> None:
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True)
    original = '[operator]\nlogin = "operator-a"\n\n[scan]\nrepos = ["other/app"]\n'
    path.write_text(original)
    assert run_setup("probe", REPO).returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto")

    assert result.returncode == 1
    assert "[scan] repos" in result.stderr
    assert path.read_text() == original


def test_write_config_reports_an_unchanged_file_the_tick_would_refuse(doubles) -> None:
    """"Nothing to write" is not "configured": the tick resolves the same file."""
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True)
    original = (
        '[operator]\nlogin = "operator-a"\n\n[scan]\nrepos = ["other/app"]\n'
        '\n[repos."owner/widget"]\nverify_tools = []\npermission_mode = "auto"\n'
    )
    path.write_text(original)
    assert run_setup("probe", REPO).returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto")

    assert result.returncode == 1
    assert "[scan] repos" in result.stderr
    assert path.read_text() == original


def test_write_config_refuses_to_rewrite_an_unreadable_config(doubles) -> None:
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("[operator\n")
    assert run_setup("probe", REPO).returncode == 0

    result = run_setup("write-config", REPO, "--login", "operator-a",
                       "--permission-mode", "auto")

    assert result.returncode == 1
    assert path.read_text() == "[operator\n"


# --- Adoption steps ---


def test_login_reports_the_authenticated_github_user(doubles) -> None:
    result = run_setup("login")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"login": "operator-a"}


def test_inspect_proposes_grants_without_touching_the_target(doubles) -> None:
    result = run_setup("inspect", REPO)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["repo"] == "Owner/Widget"
    assert report["default_branch"] == "trunk"
    assert report["manifests"] == ["package.json"]
    assert report["proposed_verify_tools"] == ["pnpm run test *", "pnpm run lint *"]
    assert report["guidance_files"] == ["AGENTS.md"]
    assert report["configured"] is False
    assert all(call[0] == "api" for call in gh_calls(doubles))


@pytest.mark.parametrize(("state_key", "expected"), [
    ("failed_manifest", "package.json"),
    ("malformed_listing", "root listing"),
    ("malformed_manifest", "package.json"),
])
def test_inspect_refuses_incomplete_github_data(doubles, state_key, expected) -> None:
    state = json.loads(doubles["state"].read_text())
    state[state_key] = True
    doubles["state"].write_text(json.dumps(state))

    result = run_setup("inspect", REPO)

    assert result.returncode == 3
    assert expected in result.stderr
    assert not result.stdout


@pytest.mark.parametrize(("manifest", "body"), [
    ("package.json", "{invalid json"),
    ("pyproject.toml", "[invalid toml"),
])
def test_inspect_refuses_malformed_manifest_content(doubles, manifest, body) -> None:
    state = json.loads(doubles["state"].read_text())
    state["repo"]["files"] = {manifest: body}
    doubles["state"].write_text(json.dumps(state))

    result = run_setup("inspect", REPO)

    assert result.returncode == 3
    assert manifest in result.stderr
    assert not result.stdout


def test_inspect_reads_a_manifest_spanning_several_wrapped_lines(doubles) -> None:
    pyproject = '[project]\nname = "widget"\ndescription = "' + "x" * 200 + '"\n'
    state = json.loads(doubles["state"].read_text())
    state["repo"]["files"] = {"pyproject.toml": pyproject}
    doubles["state"].write_text(json.dumps(state))

    result = run_setup("inspect", REPO)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["proposed_verify_tools"] == ["python -m pytest *"]


@pytest.mark.parametrize(("files", "expected"), [
    ({"pom.xml": "", "mvnw": ""}, ["./mvnw test *", "./mvnw verify *"]),
    ({"build.gradle.kts": ""}, ["gradle test *", "gradle check *"]),
    ({"build.gradle": "", "gradlew": ""}, ["./gradlew test *", "./gradlew check *"]),
    ({"pyproject.toml": "[tool.ruff]\n", "uv.lock": ""}, ["uv run pytest *", "uv run ruff *"]),
    ({"pyproject.toml": "[project]\n"}, ["python -m pytest *"]),
    ({"Cargo.toml": ""}, ["cargo test *", "cargo clippy *"]),
    ({"package.json": '{"scripts": {"test": "x"}}'}, ["npm run test *"]),
    ({"README.md": ""}, []),
])
def test_inspect_maps_each_manifest_to_a_proposal(doubles, files, expected) -> None:
    state = json.loads(doubles["state"].read_text())
    state["repo"]["files"] = files
    doubles["state"].write_text(json.dumps(state))

    report = json.loads(run_setup("inspect", REPO).stdout)

    assert report["proposed_verify_tools"] == expected


def test_inspect_reports_the_current_section_for_a_configured_repository(doubles) -> None:
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        '[operator]\nlogin = "operator-a"\n'
        '\n[repos."owner/widget"]\nverify_tools = ["make test"]\npermission_mode = "dontAsk"\n',
    )

    report = json.loads(run_setup("inspect", REPO).stdout)

    assert report["configured"] is True
    assert report["current"] == {
        "verify_tools": ["make test"], "permission_mode": "dontAsk", "base_ref": "",
    }


def test_labels_creates_the_vocabulary_and_reports_unknown_ai_labels(doubles) -> None:
    state = json.loads(doubles["state"].read_text())
    state["labels"] = ["bug", "ai:in-queue"]
    doubles["state"].write_text(json.dumps(state))

    result = run_setup("labels", REPO)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["unknown_ai_labels"] == ["ai:in-queue"]
    labels_now = set(json.loads(doubles["state"].read_text())["labels"])
    assert pipeline.WORKFLOW_LABELS <= labels_now
    assert "ai:in-queue" in labels_now


def test_prereqs_names_every_missing_host_tool(doubles, monkeypatch, tmp_path) -> None:
    only = tmp_path / "only-git"
    only.mkdir()
    (only / "git").symlink_to(shutil.which("git"))
    (only / "gh").symlink_to(doubles["bin"] / "gh")

    report = setup_flow.check_prereqs(path=str(only))

    assert report["missing"] == ["codex", "jq", "claude"]
    assert set(report["found"]) == {"gh", "git"}


# --- Deny rules ---


def test_deny_rules_never_deny_the_engine_push_grants() -> None:
    """Deny outranks allow, so a rule matching a stage's push strands it."""
    grants = [
        "git push origin HEAD",
        "git push upstream HEAD:refs/heads/feat/setup-skill",
        "git push origin HEAD:refs/heads/fix-f-d",
    ]
    for command in grants:
        denied = [
            rule for rule in setup_flow.DESTRUCTIVE_GIT_DENY_RULES
            if setup_flow.allowlist_grants(command, rule) is not None
        ]
        assert denied == [], command


@pytest.mark.parametrize("command", [
    "git push --force origin main",
    "git push origin main --force-with-lease",
    "git push -f origin main",
    "git push origin main -f",
    "git push origin --delete feature",
    "git push -d origin feature",
    "git push origin -d feature",
    "git push origin :refs/heads/feature",
    "git push origin +HEAD:refs/heads/main",
    "git push --mirror origin",
    "git push --prune origin",
])
def test_deny_rules_cover_the_destructive_push_surface(command: str) -> None:
    assert any(
        setup_flow.allowlist_grants(command, rule) is not None
        for rule in setup_flow.DESTRUCTIVE_GIT_DENY_RULES
    ), command


def test_a_trailing_colon_star_rule_would_deny_the_implementer_push() -> None:
    """Why short-form ``:branch`` deletion has no rule.

    ``:*`` at the end of a rule is Claude Code's legacy prefix form, so the
    obvious rule reads as "git push, then anything" and denies every stage's
    own push. The gap is documented rather than closed with that rule.
    """
    assert setup_flow.allowlist_grants(
        "git push origin HEAD", "Bash(git push * :*)",
    ) is not None


def test_deny_rule_install_merges_once_and_preserves_settings(
    isolate_operator_state: Path,
) -> None:
    settings = isolate_operator_state / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "model": "opus",
        "permissions": {"allow": ["Bash(ls *)"], "deny": ["Bash(git push --force *)"]},
    }))

    first = run_setup("deny-rules", "--install")
    after_first = settings.read_bytes()
    second = run_setup("deny-rules", "--install")

    assert first.returncode == 0 and second.returncode == 0
    data = json.loads(after_first)
    assert data["model"] == "opus"
    assert data["permissions"]["allow"] == ["Bash(ls *)"]
    assert data["permissions"]["deny"][0] == "Bash(git push --force *)"
    assert set(setup_flow.DESTRUCTIVE_GIT_DENY_RULES) <= set(data["permissions"]["deny"])
    assert json.loads(second.stdout)["added"] == []
    assert settings.read_bytes() == after_first


def test_deny_rule_install_refuses_malformed_settings(isolate_operator_state: Path) -> None:
    settings = isolate_operator_state / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")

    result = run_setup("deny-rules", "--install")

    assert result.returncode == 1
    assert settings.read_text() == "{not json"


def test_deny_rules_without_install_change_nothing(isolate_operator_state: Path) -> None:
    result = run_setup("deny-rules")

    assert result.returncode == 0
    assert json.loads(result.stdout)["rules"] == list(setup_flow.DESTRUCTIVE_GIT_DENY_RULES)
    assert not (isolate_operator_state / "settings.json").exists()


# --- The whole flow ---


def test_setup_twice_changes_nothing_and_never_writes_into_the_target(
    doubles, tmp_path: Path,
) -> None:
    checkout = target_checkout(tmp_path)
    before = snapshot(checkout)

    first = run_full_setup(checkout)
    config_after_first = pipeline.operator_config_path().read_bytes()
    created_first = label_creations(doubles)
    second = run_full_setup(checkout)

    assert all(step.returncode == 0 for step in first + second), [
        step.stderr for step in first + second
    ]
    assert json.loads(second[-1].stdout)["changed"] is False
    assert pipeline.operator_config_path().read_bytes() == config_after_first
    assert label_creations(doubles) == created_first
    assert snapshot(checkout) == before


def test_a_repository_set_up_from_scratch_passes_the_tick_startup_gates(
    doubles, tmp_path: Path,
) -> None:
    """Fresh install plus setup is enough for a tick on an unseen repository.

    Before setup a fresh install has no operator login, so the tick refuses
    at startup and names setup; after it, the same invocation clears every
    startup gate and completes a tick.
    """
    checkout = target_checkout(tmp_path)
    before_setup = run_tick(checkout)
    assert before_setup.returncode == 3, before_setup.stderr
    assert "/pr-pipeline:setup" in before_setup.stderr

    assert all(step.returncode == 0 for step in run_full_setup(checkout))
    after_setup = run_tick(checkout)

    assert after_setup.returncode == 0, after_setup.stderr


@pytest.mark.parametrize("legacy", [False, True])
def test_impl_settings_setup_roundtrip_keeps_fixer_inheritance(doubles, legacy):
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[defaults]\nimpl_model="defaults"\nimpl_effort="low"\n'
                    '[repos."owner/widget"]\nmodel="old"\neffort="medium"\n'
                    'fix_model=""\nfix_effort=""\n')
    prefix = "" if legacy else "impl-"
    result = run_setup_public("probe", REPO, f"--{prefix}model", "sonnet", f"--{prefix}effort", "high")
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in doubles["claude_log"].read_text().splitlines()]
    assert all(call["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "high" for call in calls)
    result = run_setup_public("write-config", REPO, "--login", "operator-a", "--permission-mode", "auto")
    assert result.returncode == 0, result.stderr
    section = tomllib.loads(path.read_text())["repos"][REPO]
    assert section["impl_model"] == "sonnet" and section["impl_effort"] == "high"
    assert "model" not in section and "effort" not in section
    assert section["fix_model"] == section["fix_effort"] == ""
    config = pipeline.resolve_config(REPO, cli_overrides={"model": "opus", "effort": "max"})
    assert config.errors == ()
    assert (config.model, config.fix_model, config.effort, config.fix_effort) == ("opus", "opus", "max", "max")
    result = run_setup_public("write-config", REPO, "--login", "operator-a", "--permission-mode", "auto")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["changed"] is False


@pytest.mark.parametrize("key, first, second", [("model", "sonnet", "opus"), ("effort", "high", "low")])
@pytest.mark.parametrize("conflict", [False, True])
def test_impl_settings_setup_alias_conflicts_fail_before_probe(doubles, key, first, second, conflict):
    result = run_setup_public("probe", REPO, f"--impl-{key}", first, f"--{key}", second if conflict else first)
    if conflict:
        assert result.returncode != 0 and "conflict" in result.stderr
        assert not doubles["claude_log"].exists()
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("legacy_record", [False, True])
def test_impl_settings_setup_keeps_prior_effort_when_probe_did_not_choose_it(doubles, legacy_record):
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[repos."owner/widget"]\neffort="high"\nfix_model="opus"\nfix_effort="low"\n')
    assert run_setup_public("probe", REPO, "--model", "sonnet").returncode == 0
    if legacy_record:
        record = setup_flow.probe_record_path(REPO)
        data = json.loads(record.read_text())
        data.pop("impl_model", None)
        data.pop("impl_effort", None)
        record.write_text(json.dumps(data))
    result = run_setup_public("write-config", REPO, "--login", "operator-a", "--permission-mode", "auto")
    assert result.returncode == 0, result.stderr
    section = tomllib.loads(path.read_text())["repos"][REPO]
    assert section["impl_model"] == "sonnet" and section["impl_effort"] == "high"
    config = pipeline.resolve_config(REPO)
    assert config.errors == ()
    assert (config.effort, config.fix_model, config.fix_effort) == ("high", "opus", "low")


def test_impl_settings_setup_keeps_default_model_intent_separate_from_probe(doubles, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    result = run_setup_public("probe", REPO, "--impl-engine", "claude-ds")
    assert result.returncode == 0, result.stderr
    result = run_setup_public("write-config", REPO, "--login", "operator-a", "--permission-mode", "auto")
    assert result.returncode == 0, result.stderr
    section = tomllib.loads(pipeline.operator_config_path().read_text())["repos"][REPO]
    assert section["impl_model"] == ""
    assert section["probed_model"] == pipeline.effective_engine_model("claude-ds", "")
    assert "fix_model" not in section and "fix_effort" not in section


def test_route_probe_setup_writes_only_changed_effort_and_fixer_keys(doubles):
    """Setup does not pin [defaults] route values into the repository section."""
    path = pipeline.operator_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[defaults]\nimpl_effort="low"\nfix_engine="claude"\nfix_model="sonnet"\n')
    write = ("write-config", REPO, "--login", "operator-a", "--permission-mode", "auto")
    assert run_setup_public("probe", REPO).returncode == 0
    assert run_setup_public(*write).returncode == 0
    section = tomllib.loads(path.read_text())["repos"][REPO]
    assert not {"impl_effort", "fix_engine", "fix_model", "fix_effort"} & set(section)

    assert run_setup_public("probe", REPO, "--fix-model", "opus").returncode == 0
    result = run_setup_public(*write)
    assert result.returncode == 0, result.stderr
    section = tomllib.loads(path.read_text())["repos"][REPO]
    assert section["fix_model"] == "opus"
    assert not {"impl_effort", "fix_engine", "fix_effort"} & set(section)
    config = pipeline.resolve_config(REPO)
    assert (config.effort, config.fix_engine, config.fix_model) == ("low", "claude", "opus")


@pytest.mark.parametrize("legacy_record", [False, True])
def test_impl_settings_setup_writes_dontask_after_early_inconclusive_default_model_probe(
    doubles, monkeypatch, legacy_record,
):
    """An early inconclusive default-model probe still lets dontAsk setup write.

    A stage-route probe records the effective model it would run. A record
    from before stage routes carries a blank model and no ``impl_model``;
    write-config compares effective models, so it accepts both.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    probe = run_setup_public("probe", REPO, "--impl-engine", "claude-ds", "--verify-tool", "git:*")
    assert json.loads(probe.stdout)["outcome"] == setup_flow.INCONCLUSIVE, probe.stderr
    assert not doubles["claude_log"].exists()
    record = setup_flow.probe_record_path(REPO)
    data = json.loads(record.read_text())
    probed_model = pipeline.effective_engine_model("claude-ds", "")
    assert data["model"] == probed_model
    assert data["impl_model"] == ""
    if legacy_record:
        for key in ("impl_model", "routes", "requested_routes", "stages"):
            data.pop(key)
        data["model"] = probed_model = ""
        record.write_text(json.dumps(data))
    write = ("write-config", REPO, "--login", "operator-a",
             "--permission-mode", "dontAsk", "--verify-tool", "git:*")
    result = run_setup_public(*write)
    assert result.returncode == 0, result.stderr
    section = tomllib.loads(pipeline.operator_config_path().read_text())["repos"][REPO]
    assert (section["impl_engine"], section["impl_model"]) == ("claude-ds", "")
    assert (section["probed_impl_engine"], section["probed_model"]) == ("claude-ds", probed_model)
    config = pipeline.resolve_config(REPO)
    assert config.errors == ()
    assert config.probed_route == ("claude-ds", probed_model)
    again = run_setup_public(*write)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["changed"] is False


@pytest.mark.parametrize("engine, model, probed, accepted", [
    ("claude-ds", "", ("claude-ds", ""), True),
    ("claude-ds", pipeline.CLAUDE_DS_DEFAULT_MODEL, ("claude-ds", ""), True),
    ("claude-ds", "", ("claude-ds", pipeline.CLAUDE_DS_DEFAULT_MODEL), True),
    ("claude-ds", "other-model", ("claude-ds", ""), False),
    ("claude", "", ("claude-ds", ""), False),
    ("claude", "sonnet", ("claude", ""), False),
])
def test_impl_settings_setup_compares_effective_route_models(
    doubles, engine, model, probed, accepted,
):
    """Routing defaults match the probe by the model each route runs, not spelling."""
    config = dataclasses.replace(
        pipeline.resolve_config(REPO), impl_engine=engine, model=model,
        probed_route=probed, repo_configured=True, verify_tools=(),
        permission_mode="auto", engine_settings="", errors=(),
    )
    errors = setup_flow._resolved_errors(config, REPO, (), "auto", "", probed)
    assert (errors == []) is accepted, errors
    tampered = dataclasses.replace(config, probed_route=(probed[0], "tampered"))
    assert setup_flow._resolved_errors(tampered, REPO, (), "auto", "", probed)


def test_impl_settings_setup_rejects_conflicting_alias_before_later_repetition(doubles):
    result = run_setup_public("probe", REPO, "--model", "sonnet", "--impl-model", "opus", "--model", "opus")
    assert result.returncode != 0 and "conflict" in result.stderr
    assert not doubles["claude_log"].exists()


def test_impl_settings_setup_equivalent_whitespace_aliases_use_normalized_model(doubles):
    result = run_setup_public("probe", REPO, "--impl-model", " sonnet ", "--model", "sonnet")
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in doubles["claude_log"].read_text().splitlines()]
    assert all(call["argv"][call["argv"].index("--model") + 1] == "sonnet" for call in calls)
    result = run_setup_public("write-config", REPO, "--login", "operator-a", "--permission-mode", "auto")
    assert result.returncode == 0, result.stderr


# --- Helpers ---


def set_route(monkeypatch: pytest.MonkeyPatch, **behaviour: str) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOUR", json.dumps(behaviour))


def run_setup(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SETUP_PY), *args],
        capture_output=True, text=True, timeout=120, cwd=cwd,
    )


def run_setup_public(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SETUP_PY.parents[1] / "setup.sh"), *args],
        capture_output=True, text=True, timeout=120,
    )


def run_full_setup(checkout: Path) -> list[subprocess.CompletedProcess[str]]:
    """Run every writing step the skill drives, from inside the target checkout."""
    return [
        run_setup("login", cwd=checkout),
        run_setup("inspect", REPO, cwd=checkout),
        run_setup("labels", REPO, cwd=checkout),
        run_setup("probe", REPO, "--verify-tool", "pnpm run test *", cwd=checkout),
        run_setup("write-config", REPO, "--login", "operator-a",
                  "--verify-tool", "pnpm run test *", "--permission-mode", "auto",
                  cwd=checkout),
    ]


def run_tick(checkout: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TICK_PY)], cwd=checkout, capture_output=True,
        text=True, timeout=120,
    )


def target_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "widget"
    checkout.mkdir()
    for args in (("init", "-q", "-b", "trunk"),
                 ("remote", "add", "origin", "https://github.com/owner/widget.git")):
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)
    (checkout / "package.json").write_text('{"scripts": {"test": "jest"}}')
    return checkout


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def claude_calls(doubles: dict[str, Path]) -> list[dict[str, object]]:
    return [json.loads(line) for line in doubles["claude_log"].read_text().splitlines()]


def mode_of(call: dict[str, object]) -> str:
    argv = call["argv"]
    assert isinstance(argv, list)
    return argv[argv.index("--permission-mode") + 1]


def gh_calls(doubles: dict[str, Path]) -> list[list[str]]:
    return [json.loads(line) for line in doubles["gh_log"].read_text().splitlines()]


def label_creations(doubles: dict[str, Path]) -> list[list[str]]:
    return [call for call in gh_calls(doubles) if call[:2] == ["label", "create"]]


GH_DOUBLE = r'''
import base64, json, os, sys
args = sys.argv[1:]
path = os.environ["SETUP_GH_STATE"]
state = json.load(open(path))
with open(os.environ["SETUP_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
repo = state["repo"]
def out(value):
    print(json.dumps(value))
if args[:2] == ["api", "user"]:
    print(state["login"])
elif args[0] == "api" and "/labels?" in args[1]:
    out([[{"name": name} for name in state["labels"]]])
elif args[:2] == ["label", "create"]:
    state["labels"].append(args[2])
    json.dump(state, open(path, "w"))
elif args[0] == "api" and args[1].lower() == "repos/owner/widget":
    out({"full_name": repo["full_name"], "default_branch": repo["default_branch"]})
elif args[0] == "api" and "/contents/" in args[1]:
    name = args[1].split("/contents/", 1)[1].split("?", 1)[0]
    if state.get("failed_manifest") and name == "package.json":
        print("temporary API failure", file=sys.stderr)
        sys.exit(1)
    if name not in repo["files"]:
        print("Not Found", file=sys.stderr)
        sys.exit(1)
    if state.get("malformed_manifest") and name == "package.json":
        out({"encoding": "base64", "content": "%%%"})
    else:
        # GitHub wraps the base64 content with a newline every 60 characters.
        encoded = base64.b64encode(repo["files"][name].encode()).decode()
        out({"encoding": "base64",
             "content": "".join(encoded[i:i + 60] + "\n"
                                for i in range(0, len(encoded), 60))})
elif args[0] == "api" and "/contents?" in args[1]:
    if state.get("malformed_listing"):
        out({"unexpected": "payload"})
    else:
        out([{"name": name, "type": "file"} for name in repo["files"]])
elif args[:2] == ["repo", "view"]:
    out({"defaultBranchRef": {"name": repo["default_branch"]}})
else:
    print("" if ("-q" in args or "--jq" in args) else "[]")
'''

CLAUDE_DOUBLE = r'''
import json, os, subprocess, sys
argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("2.1.281 (Claude Code)")
    sys.exit(0)
prompt = sys.stdin.read()
mode = argv[argv.index("--permission-mode") + 1]
behaviours = json.loads(os.environ["FAKE_CLAUDE_BEHAVIOUR"])
behaviour = behaviours[mode]
if "--settings" in argv:
    settings = json.loads(argv[argv.index("--settings") + 1])
    if settings.get("sandbox", {}).get("enabled") is False:
        behaviour = behaviours.get(mode + ":unsandboxed", behaviour)
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({
        "argv": argv, "stdin_isatty": sys.stdin.isatty(), "cwd": os.getcwd(),
        "prompt": prompt,
        "env": {k: os.environ.get(k) for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL")},
    }) + "\n")
command = os.environ["FAKE_PROBE_COMMAND"]
def emit(obj):
    print(json.dumps(obj), flush=True)
def tool_use():
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": command}}]}})
def tool_result(text, is_error):
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": text, "is_error": is_error}]}})
if behaviour == "reject":
    print("Error: auto mode is unavailable for this model", file=sys.stderr)
    sys.exit(1)
if behaviour == "run":
    tool_use()
    done = subprocess.run(command.split(), capture_output=True, text=True)
    tool_result(done.stdout + done.stderr, done.returncode != 0)
    emit({"type": "result", "result": done.stdout, "permission_denials": []})
elif behaviour == "deny":
    tool_use()
    tool_result("Permission to use Bash with command git init has been denied.", True)
    emit({"type": "result", "result": "denied", "permission_denials": [
        {"tool_name": "Bash", "tool_use_id": "toolu_1", "tool_input": {"command": command}}]})
elif behaviour == "deny_unrecorded":
    tool_use()
    tool_result("Permission to use Bash with command git init has been denied.", True)
elif behaviour == "fail":
    tool_use()
    tool_result("Exit code 1\n" + os.getcwd() + "/.git: Operation not permitted", True)
    emit({"type": "result", "result": "it failed", "permission_denials": []})
elif behaviour == "claim":
    emit({"type": "result", "permission_denials": [],
          "result": "Initialized empty Git repository in " + os.getcwd() + "/.git/"})
else:
    emit({"type": "result", "result": "I would rather not.", "permission_denials": []})
'''
