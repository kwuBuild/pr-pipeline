"""Exercise the copied shell entry point without Claude sessions or paid engines."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest


SKILL = Path(__file__).resolve().parent
ROOT = SKILL.parents[1]


@pytest.fixture
def installed(tmp_path):
    home = tmp_path / "operator home"
    home.mkdir()
    target = tmp_path / "target repo"
    target.mkdir()
    install = tmp_path / "plugin cache" / "version one"
    shutil.copytree(ROOT / "skills", install / "skills")
    commands = tmp_path / "commands"
    commands.mkdir()
    listing = tmp_path / "plugins.json"
    env = {k: v for k, v in os.environ.items()
           if k not in {"CLAUDE_PLUGIN_ROOT", "PR_PIPELINE_HOME", "PR_PIPELINE_PYTHON"}}
    env.update(HOME=str(home), PATH=f"{commands}:{env['PATH']}",
               PLUGIN_LIST=str(listing), PR_PIPELINE_PYTHON=sys.executable)
    write_executable(commands / "claude", '#!/bin/bash\n[[ "$*" == "plugin list --json" ]] || exit 91\ncat "$PLUGIN_LIST"\n')
    listing.write_text(json.dumps([plugin(install)]))
    return home, target, install, listing, env


def test_install_and_real_tick_from_foreign_cwd(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    result = run(launcher, "tick", "engine-progress", "--wt-root", str(target), cwd=target, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert run(launcher, "labels", "vocabulary", cwd=target, env=env).returncode == 0
    assert not list(target.iterdir()), "read-only commands must not create target state"
    # Reinstall is safe and copies bytes, not a link back into the version cache.
    assert install_launcher(installed).read_bytes() == launcher.read_bytes()
    assert not launcher.is_symlink()



def test_installed_events_reads_target_logs_without_engine_or_github_calls(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "remote", "add", "origin",
                    "https://github.com/example/consumer.git"], check=True)
    logs = target / ".claude/worktrees/.tick-log"
    logs.mkdir(parents=True)
    entry = {"ts": 1, "pr": 27, "phase": "dispatch", "stage": "implement",
             "action": "launch", "engine": "claude", "dispatch_id": "d1",
             "details": {"model": "sonnet"}}
    (logs / "tick-001.jsonl").write_text(json.dumps(entry) + "\n")
    commands = Path(env["PATH"].split(os.pathsep)[0])
    calls = home / "unexpected-calls"
    env["EVENT_CALLS"] = str(calls)
    for name in ("gh", "codex"):
        write_executable(commands / name, '#!/bin/sh\nprintf called >> "$EVENT_CALLS"\nexit 99\n')
    # The fixture's Claude shim permits plugin discovery only, never a model run.
    before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    result = run(launcher, "events", "--json", cwd=target, env=env)
    assert result.returncode == 0, result.stderr
    event = json.loads(result.stdout)
    assert (event["repo"], event["event"], event["number"]) == ("example/consumer", "launch", 27)
    assert not calls.exists()
    assert before == {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}


def test_upgrade_resolves_again_without_reinstall(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    original = launcher.read_bytes()
    next_install = install.with_name("version two")
    shutil.copytree(install, next_install)
    write_executable(next_install / "skills/tick/tick.sh", '#!/bin/bash\nprintf "upgraded\\n"\n')
    shutil.rmtree(install)
    listing.write_text(json.dumps([plugin(next_install)]))
    result = run(launcher, "tick", cwd=target, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "upgraded\n"
    assert launcher.read_bytes() == original


@pytest.mark.parametrize("bad", [None, {}, [], "malformed", [None],
    [{"id": "pr-pipeline@pr-pipeline", "enabled": True, "installPath": ""}],
    [{"id": "pr-pipeline@pr-pipeline", "enabled": True, "installPath": "relative"}],
    [{"id": "pr-pipeline@pr-pipeline", "enabled": True, "installPath": "/missing"}],
])
def test_unavailable_install_names_setup(installed, bad):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    listing.write_text(bad if isinstance(bad, str) else json.dumps(bad))
    result = run(launcher, "status", cwd=target, env=env)
    assert result.returncode == 3
    assert "setup" in result.stderr.lower()
    assert "No such file" not in result.stderr


@pytest.mark.parametrize("case", ["disabled", "ambiguous", "wrong-marketplace", "list-failed"])
def test_does_not_guess_an_install(installed, case):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    entry = plugin(install)
    if case == "disabled":
        entry["enabled"] = False
    if case == "wrong-marketplace":
        entry["id"] = "pr-pipeline@someone-else"
    listing.write_text(json.dumps([entry, entry] if case == "ambiguous" else [entry]))
    if case == "list-failed":
        write_executable(Path(env["PATH"].split(":")[0]) / "claude", '#!/bin/bash\nexit 7\n')
    result = run(launcher, "tick", cwd=target, env=env)
    assert result.returncode == 3
    assert "setup" in result.stderr.lower()


def test_development_override_does_not_need_claude(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    listing.unlink()
    env["PR_PIPELINE_HOME"] = str(install)
    assert run(launcher, "tick", "engine-progress", "--wt-root", str(target), cwd=target, env=env).returncode == 0
    env["PR_PIPELINE_HOME"] = ""
    result = run(launcher, "tick", cwd=target, env=env)
    assert result.returncode == 3
    assert "PR_PIPELINE_HOME" in result.stderr


@pytest.mark.parametrize("args,script", [
    (["tick", "#17", "--model", "model with spaces", "*", ""], "tick/tick.sh"),
    (["status"], "status/status.sh"),
    (["preflight"], "tick/preflight.sh"),
])
def test_shell_routes_preserve_cwd_arguments_and_exit_status(installed, args, script):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    write_executable(install / "skills" / script,
                     '#!/bin/bash\nprintf "%s\\0" "$PWD" "$@"\nexit 23\n')
    result = run(launcher, *args, cwd=target, env=env)
    assert result.returncode == 23
    assert result.stdout.split("\0")[:-1] == [str(target), *args[1:]]


def test_labels_uses_shared_python_resolver_and_preserves_exit(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    (install / "skills/tick/lib/labels.py").write_text(
        'import os, sys, json\nprint(json.dumps([os.getcwd(), sys.argv[1:]]))\nsys.exit(24)\n')
    result = run(launcher, "labels", "provision", cwd=target, env=env)
    assert result.returncode == 24
    assert json.loads(result.stdout) == [str(target), ["provision"]]


def test_documented_commands_reach_their_targets_without_root_variables(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    for script in ("tick/tick.sh", "status/status.sh", "tick/preflight.sh"):
        write_executable(install / "skills" / script, '#!/bin/bash\nprintf "reached\\n"\n')
    (install / "skills/tick/lib/labels.py").write_text('print("reached")\n')
    seen = set()
    for doc in (SKILL / "README.md", SKILL / "DEBUG.md"):
        for match in re.finditer(r"~/.claude/pr-pipeline/bin/pr-pipeline (tick[^\n`]*|status|preflight|labels provision)", doc.read_text().replace("\\\n", " ")):
            args = shlex.split(match[1], comments=True)
            seen.add(args[0])
            result = subprocess.run(["bash", "-c", match[0]], cwd=target, env=env,
                                    text=True, capture_output=True, timeout=20)
            assert result.returncode == 0, (match[0], result.stderr)
            assert result.stdout == "reached\n"
    assert seen == {"tick", "status", "preflight", "labels"}


def test_help_and_unknown_command_need_no_install(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    listing.unlink()
    assert run(launcher, "--help", cwd=target, env=env).returncode == 0
    assert run(launcher, "typo", cwd=target, env=env).returncode == 2


def test_failed_reinstall_preserves_working_launcher(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    before = launcher.read_bytes()
    write_executable(Path(env["PATH"].split(":")[0]) / "cp",
                     '#!/bin/bash\nprintf broken > "$2"\nexit 1\n')
    result = subprocess.run(["bash", str(install / "skills/tick/install-launcher.sh")],
                            cwd=target, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert launcher.read_bytes() == before
    assert list(launcher.parent.iterdir()) == [launcher]
    assert run(launcher, "labels", "vocabulary", cwd=target, env=env).returncode == 0


def test_real_status_from_separate_git_target(installed):
    home, target, install, listing, env = installed
    launcher = install_launcher(installed)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "remote", "add", "origin",
                    "https://github.com/example/target.git"], check=True)
    write_executable(Path(env["PATH"].split(":")[0]) / "gh", '#!/bin/bash\nprintf "[[]]\\n"\n')
    result = run(launcher, "status", cwd=target, env=env)
    assert result.returncode == 0, result.stderr
    assert "repo example/target" in result.stdout
    assert "PARKED ITEMS" in result.stdout
    assert not (target / ".claude").exists()


# --- Helpers ---


def plugin(path):
    return {"id": "pr-pipeline@pr-pipeline", "enabled": True, "installPath": str(path)}


def write_executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


def install_launcher(installed):
    home, target, install, listing, env = installed
    result = subprocess.run(["bash", str(install / "skills/tick/install-launcher.sh")],
                            cwd=target, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return home / ".claude/pr-pipeline/bin/pr-pipeline"


def run(launcher, *args, cwd, env):
    return subprocess.run([str(launcher), *args], cwd=cwd, env=env,
                          text=True, capture_output=True, timeout=20)
