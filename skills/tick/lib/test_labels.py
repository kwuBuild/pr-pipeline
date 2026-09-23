"""Exercise installed label setup with real subprocesses and a local gh double."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pipeline
import pytest


@pytest.fixture
def label_repo(tmp_path, monkeypatch):
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "upstream",
         "https://github.com/elsewhere/project.git"], check=True,
    )
    install = tmp_path / "installed plugin" / "skills" / "tick"
    shutil.copytree(Path(__file__).resolve().parents[1], install)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + GH_DOUBLE)
    gh.chmod(0o755)
    state_path = tmp_path / "labels.json"
    state_path.write_text("{}")
    log_path = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("LABEL_TEST_STATE", str(state_path))
    monkeypatch.setenv("LABEL_TEST_LOG", str(log_path))
    monkeypatch.setenv("GH_HOST", "wrong.example")
    monkeypatch.setenv("GH_REPO", "wrong/repository")
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    return repo, install / "lib" / "labels.py", state_path, log_path


def test_provision_creates_exact_vocabulary_and_second_run_changes_nothing(label_repo):
    repo, script, state, log = label_repo
    expected = {
        value for name, value in vars(pipeline).items() if name.startswith("LABEL_")
    }

    first = run_labels(repo, script, "provision")

    assert first.returncode == 0, first.stderr
    assert set(json.loads(state.read_text())) == expected
    first_state = state.read_text()
    first_creates = create_calls(log)
    assert len(first_creates) == len(expected)

    second = run_labels(repo, script, "provision")

    assert second.returncode == 0, second.stderr
    assert state.read_text() == first_state
    assert create_calls(log) == first_creates


def test_provision_reports_all_unknown_labels_and_preserves_existing_metadata(label_repo):
    repo, script, state, log = label_repo
    original = {f"ordinary-{i}": {"color": "123456"} for i in range(105)}
    original.update({name: {"color": "abcdef", "description": "operator custom"}
                     for name in [*pipeline.WORKFLOW_LABELS, "ai:parked", "ai:legacy"]})
    state.write_text(json.dumps(original))

    result = run_labels(repo, script, "provision")

    assert result.returncode == 0, result.stderr
    assert "ai:parked" in result.stdout and "ai:legacy" in result.stdout
    assert "not selected by any dispatch query" in result.stdout
    assert json.loads(state.read_text()) == original
    assert create_calls(log) == []


@pytest.mark.parametrize("failure", ["read", "create", "malformed"])
def test_provision_reports_errors_without_a_traceback(label_repo, monkeypatch, failure):
    repo, script, state, log = label_repo
    monkeypatch.setenv("LABEL_TEST_FAILURE", failure)

    result = run_labels(repo, script, "provision")

    assert result.returncode == 3
    assert "labels:" in result.stderr
    assert "Traceback" not in result.stderr
    assert "provisioned" not in result.stdout
    if failure != "create":
        assert create_calls(log) == []


def test_provision_accepts_a_concurrent_create_only_after_rereading(label_repo, monkeypatch):
    repo, script, state, log = label_repo
    monkeypatch.setenv("LABEL_TEST_FAILURE", "race")

    result = run_labels(repo, script, "provision")

    assert result.returncode == 0, result.stderr
    assert set(json.loads(state.read_text())) == pipeline.WORKFLOW_LABELS


def test_vocabulary_cli_needs_neither_a_repository_nor_github(label_repo):
    repo, script, state, log = label_repo
    result = run_labels(repo.parent, script, "vocabulary")

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == pipeline.WORKFLOW_LABELS
    assert not log.exists()


def test_installed_tick_loop_rejects_missing_labels_before_any_other_github_work(
    label_repo, monkeypatch,
):
    repo, script, state, log = label_repo
    config = repo.parent / "operator.toml"
    config.write_text('[operator]\nlogin = "test-owner"\n[repos."elsewhere/project"]\n')
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(config))

    result = subprocess.run(
        [sys.executable, str(script.with_name("tick.py")), "--loop"],
        cwd=repo, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 3, result.stderr
    assert "missing labels on elsewhere/project" in result.stderr
    assert "labels.py" in result.stderr and "provision" in result.stderr
    calls = list(map(json.loads, log.read_text().splitlines()))
    assert len(calls) == 1
    assert calls[0][0] == "api" and "/labels?" in calls[0][1]
    assert json.loads(state.read_text()) == {}


# --- Helpers ---


def run_labels(repo, script, command):
    return subprocess.run(
        [sys.executable, str(script), command], cwd=repo,
        capture_output=True, text=True, timeout=30,
    )


def create_calls(log):
    return [args for args in map(json.loads, log.read_text().splitlines())
            if args[:2] == ["label", "create"]]


GH_DOUBLE = '''\
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
failure = os.environ.get("LABEL_TEST_FAILURE")
state_path = Path(os.environ["LABEL_TEST_STATE"])
state = json.loads(state_path.read_text())
with open(os.environ["LABEL_TEST_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] == "api":
    assert "repos/elsewhere/project/labels?per_page=100" in args, args
    assert args[args.index("--hostname") + 1] == "github.com", args
    assert "--paginate" in args and "--slurp" in args, args
    if failure == "read":
        sys.exit("permission denied")
    if failure == "malformed":
        print('{"message": "wrong shape"}')
        sys.exit(0)
    rows = [{"name": name, **metadata} for name, metadata in state.items()]
    print(json.dumps([rows[i:i+100] for i in range(0, len(rows), 100)] or [[]]))
elif args[:2] == ["label", "create"]:
    assert args[args.index("--repo") + 1] == "github.com/elsewhere/project", args
    assert "--force" not in args, args
    if failure == "create":
        sys.exit("permission denied")
    name = args[2]
    assert name not in state, args
    state[name] = {"color": "ededed", "description": "new"}
    state_path.write_text(json.dumps(state))
    if failure == "race":
        sys.exit("label already exists")
else:
    raise AssertionError(f"unexpected or destructive command: {args}")
'''
