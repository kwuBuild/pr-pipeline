"""Exercise the installed read-only config command against a foreign target."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import pipeline


@pytest.fixture
def export_install(tmp_path):
    install = tmp_path / "install with spaces" / "skills" / "tick"
    shutil.copytree(Path(__file__).resolve().parents[1], install,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "remote", "add", "origin",
                    "https://github.com/example/project.git"], check=True)
    home = tmp_path / "home"
    home.mkdir()
    config = home / "operator.toml"
    config.write_text('[operator]\nlogin="file-operator"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gh", "claude", "claude-ds", "codex", "osascript"):
        stub = bin_dir / name
        stub.write_text('#!/bin/sh\necho "FORBIDDEN command" >&2\nexit 97\n')
        stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PIPELINE_", "PR_PIPELINE_", "CLAUDE_", "GIT_", "PYTHON"))}
    env.update(HOME=str(home), PR_PIPELINE_CONFIG=str(config),
               PR_PIPELINE_PYTHON=sys.executable,
               PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return SimpleNamespace(root=tmp_path, install=install, target=target,
                           home=home, config=config, env=env)


def test_config_exports_exact_allowlist_without_writes(export_install):
    case = export_install
    case.config.write_text('''[operator]
login="file-operator"
[defaults]
impl_engine="claude-ds"
model="route-model"
effort="high"
engine_settings='{"env":{"TOKEN":"hidden-settings-value"}}'
hidden_key="hidden-arbitrary-value"
allow_touching="hidden-rejected-value"
[repos."example/project"]
max_in_flight=7
''')
    case.env["PRIVATE_TOKEN"] = "hidden-environment-value"
    # Existing lock/state must neither block reporting nor change.
    state = case.target / ".claude" / "worktrees"
    state.mkdir(parents=True)
    (state / ".tick.lock").write_text("existing lock")
    (state / "dispatch.json").write_text("unreadable marker")
    before = snapshot(case.root)
    with (state / ".tick.lock").open() as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_export(case)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert snapshot(case.root) == before
    data = json.loads(result.stdout)
    assert set(data) == {"schema_version", "target", "observed_at", "context", "config"}
    assert data["schema_version"] == 1
    assert data["target"] == {"repo": "example/project", "remote": "origin"}
    assert dt.datetime.fromisoformat(data["observed_at"]).utcoffset() == dt.timedelta(0)
    assert data["context"] == {
        "kind": "standalone_default_invocation",
        "description": "Packaged, operator-default and repository configuration with this process's supported environment inputs; no CLI route overrides. This is not an existing loop's applied configuration.",
    }
    config = data["config"]
    assert set(config) == {
        "operator_login", "routes", "max_in_flight", "max_iterations",
        "implementer_timeout_seconds", "fixer_timeout_seconds", "review_timeout_seconds",
        "engine_stall_seconds", "engine_stall_startup_grace_seconds",
    }
    assert config["operator_login"] == "file-operator"
    assert config["max_in_flight"] == 7
    assert config["routes"] == {
        stage: {"engine": "claude-ds", "model": "route-model", "effort": "high",
                "cooldown_domain": "claude-ds"}
        for stage in ("implement", "fix")
    }
    for secret in ("hidden-settings-value", "hidden-arbitrary-value", "hidden-environment-value",
                   "hidden-rejected-value",
                   "engine_settings", "hidden_key", "PRIVATE_TOKEN", "warnings", "verify_tools"):
        assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("body,settings", [
    ('[operator]\nlogin="sk-secret /"', ["login"]),
    ('[defaults]\nimpl_engine="sk-secret /"', ["impl_engine"]),
    ('[defaults]\nimpl_model="sk-secret /"', ["impl_model"]),
    # Legacy aliases normalize before validation, so errors name the canonical key.
    ('[defaults]\nmodel="sk-secret /"', ["impl_model"]),
    ('[defaults]\nfix_engine="sk-secret /"', ["fix_engine"]),
    ('[defaults]\nfix_model="sk-secret /"', ["fix_model"]),
    ('[defaults]\nimpl_effort="sk-secret /"', ["impl_effort"]),
    ('[defaults]\neffort="sk-secret /"', ["impl_effort"]),
    ('[defaults]\nfix_effort="sk-secret /"', ["fix_effort"]),
    ('[defaults]\nengine_settings="sk-secret /"', ["engine_settings"]),
    ('[defaults]\nverify_tools=["Bash(sk-secret /)"]', ["verify_tools"]),
    ('[defaults]\napproved_reviewers=["sk-secret /"]', ["approved_reviewers"]),
    ('[defaults]\nbase_ref="sk-secret /"', ["base_ref"]),
    ('[defaults]\npermission_mode="sk-secret /"', ["permission_mode"]),
    ('[defaults]\nreview_model="sk-secret /"', ["review_model"]),
    ('[defaults]\nreview_effort="sk-secret /"', ["review_effort"]),
    ('[defaults]\nchecklist_paths=[{token="sk-secret /"}]', ["checklist_paths"]),
    ('[defaults]\nmax_in_flight=-987654321', ["max_in_flight"]),
    ('[defaults]\nmax_iterations=-987654321', ["max_iterations"]),
    ('[defaults]\nengine_stall_startup_grace_seconds=-987654321', ["engine_stall_startup_grace_seconds"]),
    ('[defaults]\nimplementer_timeout_seconds=1200', ["engine_stall_seconds", "implementer_timeout_seconds"]),
    ('defaults="sk-secret /"', ["defaults"]),
    ('operator="sk-secret /"', ["operator"]),
    ('repos="sk-secret /"', ["repos"]),
    ('scan="sk-secret /"', ["scan"]),
    ('[scan]\nrepos=["sk-secret /"]', ["scan.repos"]),
    ('[repos."sk-secret /"]', ["repos"]),
    ('[repos."example/project"]\nprobed_impl_engine="sk-secret /"\nprobed_model=""', ["probed_impl_engine"]),
    ('[repos."example/project"]\nmax_in_flight="sk-secret /"', ["max_in_flight"]),
    ('"sk-secret /" = [', ["configuration"]),
])
def test_config_errors_name_settings_without_values(export_install, body, settings):
    case = export_install
    case.config.write_text(body)
    before = snapshot(case.root)
    result = run_export(case)
    assert result.returncode == 3
    assert result.stderr == ""
    assert snapshot(case.root) == before
    data = json.loads(result.stdout)
    assert set(data) == {"schema_version", "target", "observed_at", "context", "error"}
    assert data["error"] == {
        "code": "invalid_configuration",
        "message": "Configuration could not be resolved; check the named settings.",
        "settings": settings,
    }
    for secret in ("sk-secret", "987654321", str(case.config), "Traceback", "warnings"):
        assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("variable", ["PR_PIPELINE_CONFIG", "CLAUDE_CONFIG_DIR"])
def test_config_rejects_relative_locations_without_disclosing_them(export_install, variable):
    case = export_install
    case.env.pop("PR_PIPELINE_CONFIG")
    case.env[variable] = "sk-secret-relative-location"
    result = run_export(case)
    assert result.returncode == 3
    assert result.stderr == ""
    assert json.loads(result.stdout)["error"]["settings"] == [variable]
    assert "sk-secret" not in result.stdout


def test_config_sanitizes_target_resolution_errors(export_install):
    case = export_install
    subprocess.run(["git", "-C", str(case.target), "remote", "rename", "origin",
                    "sk-secret-remote"], check=True)
    case.env["PIPELINE_REPO"] = "other/project"
    result = run_export(case)
    assert result.returncode == 3
    assert result.stderr == ""
    data = json.loads(result.stdout)
    assert data["target"] is None
    assert data["error"]["code"] == "target_resolution_failed"
    assert data["error"]["settings"] == ["target"]
    assert "sk-secret" not in result.stdout


@pytest.mark.parametrize("args", [(), ("--json", "--model", "sk-secret"), ("--loop", "--json")])
def test_config_refuses_other_modes_without_echoing_arguments(export_install, args):
    result = run_export(export_install, args=args)
    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout)["error"]["code"] == "invalid_arguments"
    assert "sk-secret" not in result.stdout


def test_config_sanitizes_parser_recursion_failure(export_install):
    case = export_install
    case.config.write_text('[defaults]\nunknown=' + '[' * 2000 + '"sk-secret"' + ']' * 2000)
    result = run_export(case)
    assert result.returncode == 3
    assert result.stderr == ""
    assert json.loads(result.stdout)["error"]["settings"] == ["configuration"]
    assert "sk-secret" not in result.stdout


@pytest.mark.parametrize("failure", ["encoding", "directory", "packaged"])
def test_config_sanitizes_file_failures(export_install, failure):
    case = export_install
    if failure == "encoding":
        case.config.write_bytes(b"sk-secret-\xff")
    elif failure == "directory":
        case.config.unlink()
        case.config.mkdir()
    else:
        (case.install / "config.toml").write_text('"sk-secret"=[')
    result = run_export(case)
    assert result.returncode == 3
    assert result.stderr == ""
    assert json.loads(result.stdout)["error"]["settings"] == [
        "packaged_config" if failure == "packaged" else "configuration"
    ]
    assert "sk-secret" not in result.stdout
    assert str(case.config) not in result.stdout


def test_config_preserves_file_and_environment_precedence(export_install):
    case = export_install
    (case.install / "config.toml").write_text('''[defaults]
max_in_flight=2
max_iterations=4
implementer_timeout_seconds=4100
fixer_timeout_seconds=4200
review_timeout_seconds=4300
engine_stall_seconds=1900
engine_stall_startup_grace_seconds=12
''')
    floor = json.loads(run_export(case).stdout)["config"]
    assert {key: floor[key] for key in floor if key not in ("operator_login", "routes")} == {
        "max_in_flight": 2, "max_iterations": 4,
        "implementer_timeout_seconds": 4100, "fixer_timeout_seconds": 4200,
        "review_timeout_seconds": 4300, "engine_stall_seconds": 1900,
        "engine_stall_startup_grace_seconds": 12,
    }
    with case.config.open("a") as config:
        config.write('[defaults]\nmax_in_flight=5\nmax_iterations=6\n')
    defaults = json.loads(run_export(case).stdout)["config"]
    assert defaults["max_in_flight"] == 5
    assert defaults["max_iterations"] == 6
    with case.config.open("a") as config:
        config.write('[repos."EXAMPLE/PROJECT"]\nmax_in_flight=8\n')
    case.env["PIPELINE_TRUSTED_AUTHOR"] = "environment-operator"
    case.env["PIPELINE_MAX_IN_FLIGHT"] = "sk-secret-unsupported-env"
    result = run_export(case)
    repo = json.loads(result.stdout)["config"]
    assert repo["max_in_flight"] == 8
    assert repo["max_iterations"] == 6
    assert repo["operator_login"] == "environment-operator"
    assert "sk-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("fix_keys,expected", [
    ('', {"engine": "claude-ds", "model": "implement-model", "effort": "high", "cooldown_domain": "claude-ds"}),
    ('fix_engine=""\nfix_model=""\nfix_effort=""', {"engine": "claude-ds", "model": "implement-model", "effort": "high", "cooldown_domain": "claude-ds"}),
    ('fix_engine="claude"', {"engine": "claude", "model": "", "effort": "high", "cooldown_domain": "claude"}),
    ('fix_engine="claude"\nfix_model="fix-model"\nfix_effort="low"', {"engine": "claude", "model": "fix-model", "effort": "low", "cooldown_domain": "claude"}),
])
def test_config_uses_resolver_fixer_inheritance(export_install, fix_keys, expected):
    case = export_install
    case.config.write_text('[defaults]\nimpl_engine="claude-ds"\nmodel="implement-model"\neffort="high"\n' + fix_keys)
    result = run_export(case)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["config"]["routes"]["fix"] == expected


def test_standalone_context_can_differ_from_tick_overrides_and_environment(export_install, monkeypatch):
    case = export_install
    case.config.write_text('[operator]\nlogin="file-operator"\n[defaults]\n'
                           'impl_engine="claude-ds"\nmodel="default-model"\neffort="high"\n')
    standalone = json.loads(run_export(case).stdout)
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(case.config))
    monkeypatch.setenv("PIPELINE_TRUSTED_AUTHOR", "loop-operator")
    # This is the resolver call made by _run_single_tick_locked with retained
    # loop route flags. It resolves configuration only; it starts no tick.
    applied = pipeline.resolve_config("example/project", cli_overrides={
        "impl_engine": "claude", "model": "override-model", "effort": "low",
    })
    assert applied.errors == ()
    assert (applied.impl_engine, applied.model, applied.fix_engine, applied.fix_model) == (
        "claude", "override-model", "claude", "override-model",
    )
    assert applied.login == "loop-operator"
    assert standalone["config"]["routes"]["implement"]["engine"] == "claude-ds"
    assert standalone["config"]["operator_login"] == "file-operator"
    assert standalone["context"]["kind"] == "standalone_default_invocation"
    case.env["PIPELINE_TRUSTED_AUTHOR"] = "another-operator"
    changed = json.loads(run_export(case).stdout)
    assert changed["config"]["operator_login"] == "another-operator"


def test_config_runs_from_linked_worktree_without_local_config_authority(export_install):
    case = export_install
    subprocess.run(["git", "-C", str(case.target), "-c", "user.name=Test", "-c",
                    "user.email=test@example.com", "commit", "--allow-empty", "-qm", "seed"], check=True)
    linked = case.root / "linked"
    subprocess.run(["git", "-C", str(case.target), "worktree", "add", "-q", "--detach", str(linked)], check=True)
    subdir = linked / "nested"
    subdir.mkdir()
    (subdir / "config.toml").write_text('[operator]\nlogin="untrusted-local"\n')
    before = snapshot(case.root)
    result = run_export(case, cwd=subdir)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["config"]["operator_login"] == "file-operator"
    assert snapshot(case.root) == before


def test_config_entry_cannot_lock_write_or_launch_a_tick_or_engine(export_install):
    case = export_install
    # Execute the actual __main__ entry with an audit guard. Unlike a final
    # filesystem snapshot, this also catches transient lock/write attempts.
    driver = '''import os, runpy, sys
entry = sys.argv[1]
sys.path.insert(0, os.path.dirname(entry))
sys.argv = [entry, "config", "--json"]
def audit(event, args):
    if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
        raise AssertionError("write attempted")
    if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "fcntl.flock", "fcntl.lockf"}:
        raise AssertionError("mutation or lock attempted")
    if event == "subprocess.Popen":
        argv = args[1]
        if argv[:2] == ["git", "rev-parse"]:
            return
        if argv == ["bash", os.path.join(os.path.dirname(os.path.dirname(entry)), "resolve-target.sh")]:
            return
        raise AssertionError("unexpected subprocess")
sys.addaudithook(audit)
runpy.run_path(entry, run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-c", driver, str(case.install / "lib" / "tick.py")],
                            cwd=case.target, env=case.env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["schema_version"] == 1


# --- Helpers ---


def run_export(case, args=("--json",), cwd=None):
    return subprocess.run(["bash", str(case.install / "tick.sh"), "config", *args],
                          cwd=cwd or case.target, env=case.env,
                          text=True, capture_output=True, timeout=15)


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}
