"""Offline process proof of completion-driven wakes in the real --loop CLI.

Run from the pipeline checkout:
    .venv/bin/python -m pytest skills/tick/lib/test_child_reaping.py skills/tick/lib/test_smoke_loop.py -q
Only external GitHub, fetch, engine, and notification calls are intercepted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pipeline
import pytest


@pytest.fixture
def loop_repo(tmp_path):
    repo = tmp_path / "consumer"
    repo.mkdir()
    git = shutil.which("git")
    assert git
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "user.name", "Fixture"],
        ["commit", "--allow-empty", "-m", "fixture"],
        ["remote", "add", "origin", "https://github.com/example/loop.git"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
        ["update-ref", "refs/remotes/origin/fix", "HEAD"],
    ):
        subprocess.run([git, *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.check_output([git, "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, source in (("gh", GH_SHIM), ("git", GIT_SHIM), ("claude", ENGINE_SHIM),
                         ("codex", VERSION_SHIM), ("osascript", "")):
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    config = home / "operator.toml"
    config.write_text(
        '[operator]\nlogin="test-owner"\n[repos."example/loop"]\n'
        'base_ref="main"\nverify_tools=["python -m pytest:*"]\n'
    )
    # Explicit environment keeps credentials and operator overrides out of the
    # fixture. Every executable that could contact a service is a local shim.
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home),
        "PR_PIPELINE_CONFIG": str(config), "PIPELINE_REPO": "example/loop",
        "LOOP_TEST_ROOT": str(repo), "LOOP_TEST_GIT": git, "LOOP_TEST_SHA": sha,
        "LOOP_TEST_LABELS": json.dumps(sorted(pipeline.WORKFLOW_LABELS)),
        "LOOP_TEST_REVIEW_MARKER": pipeline.REVIEW_MARKER,
    }
    return repo, env


@pytest.mark.parametrize("stage", ["implement", "fix"])
def test_real_loop_wakes_after_child_exit_before_emitted_cadence(loop_repo, stage):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = stage
    kind = "issue" if stage == "implement" else "pr"
    (repo / "label").write_text(
        "ai:available" if stage == "implement" else "ai:changes-requested"
    )
    stderr = repo / "tick.stderr"
    marker_path = repo / ".claude" / "worktrees" / f"{kind}-7" / "dispatch.json"
    proc = None
    engine_pid = None
    try:
        with stderr.open("w") as err:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("tick.py")),
                 "--loop", "#7" if kind == "issue" else "pr:7"],
                cwd=repo, env=env, stdout=err, stderr=err, start_new_session=True,
            )
            first = wait_for(lambda: cadence_lines(stderr), proc, stderr, timeout=20)
            cadence_at = time.monotonic()
            assert len(first) == 1
            next_wake = int(re.search(r"next_wake=(\d+)", first[0]).group(1))
            marker = json.loads(marker_path.read_text())
            engine_pid = marker["pid"]
            assert marker["stage"] == stage and marker["pgid"] == engine_pid
            wait_for(lambda: (repo / "engine-started").exists(), proc, stderr, timeout=10)
            # Keep the engine live through one production watcher interval.
            time.sleep(5.5)
            assert len(cadence_lines(stderr)) == 1
            assert marker_path.exists()
            released = time.monotonic()
            (repo / "engine-exit").touch()
            # Production polling is 5s and the nudge floor is 10s. Allow host
            # scheduling overhead while remaining far below emitted cadence.
            wait_for(lambda: len(cadence_lines(stderr)) >= 2, proc, stderr, timeout=20)
            elapsed = time.monotonic() - released
            assert time.monotonic() - cadence_at < next_wake
            assert (repo / "label").read_text() == "ai:needs-human-decision"
            assert not marker_path.exists(), "harvest must consume the completed fixture"
            unexpected = repo / "unexpected"
            assert not unexpected.exists(), unexpected.read_text()
            calls = [json.loads(line) for line in (repo / "calls.jsonl").read_text().splitlines()]
            assert sum(call[0] == "claude" for call in calls) == 1
            assert any(
                call[:3] == ["gh", kind, "edit"] and "ai:needs-human-decision" in call
                for call in calls
            )
            entries = [
                json.loads(line)
                for log in (repo / ".claude/worktrees/.tick-log").glob("*.jsonl")
                for line in log.read_text().splitlines()
            ]
            assert any(
                entry["action"] == "harvest" and entry["stage"] == stage
                and entry["details"] == {"classify_state": "complete", "identity": "dead"}
                for entry in entries
            )
            assert any(
                entry["action"] == "engine_result_summary"
                and entry["details"]["sentinel"] == "NEEDS_HUMAN_DECISION"
                for entry in entries
            )
            print(f"{stage}: harvest {elapsed:.2f}s after release; emitted cadence {next_wake}s")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        # A failing baseline still has its live fixture engine. Its separate
        # process group must not outlive the test's temporary repository.
        if engine_pid is None and marker_path.exists():
            engine_pid = json.loads(marker_path.read_text()).get("pid")
        if engine_pid:
            try:
                os.killpg(engine_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# --- Helpers ---


def cadence_lines(path):
    return [line for line in path.read_text().splitlines() if line.startswith("tick-cadence:")]


def wait_for(predicate, proc, stderr, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        assert proc.poll() is None, stderr.read_text()
        time.sleep(0.05)
    pytest.fail("loop observation timed out:\n" + stderr.read_text())


GIT_SHIM = '''
import os, sys
from pathlib import Path
args = sys.argv[1:]
if "fetch" in args:
    # Remote refs were seeded in the real local git repository.
    assert args[-2] == "origin" and args[-1] in ("main", "fix"), args
    sys.exit(0)
if "push" in args:
    raise AssertionError("unexpected push")
os.execv(os.environ["LOOP_TEST_GIT"], [os.environ["LOOP_TEST_GIT"], *args])
'''

VERSION_SHIM = '''
import sys
assert sys.argv[1:] == ["--version"], sys.argv
print("codex-cli 0.142.5")
'''

ENGINE_SHIM = '''
import json, os, sys, time
from pathlib import Path
root = Path(os.environ["LOOP_TEST_ROOT"])
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(["claude", *sys.argv[1:]]) + "\\n")
(root / "engine-started").touch()
while not (root / "engine-exit").exists():
    time.sleep(0.02)
print(json.dumps({"type": "result", "is_error": False,
                  "result": "Offline completion fixture\\nNEEDS_HUMAN_DECISION"}), flush=True)
'''

GH_SHIM = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ["LOOP_TEST_ROOT"])
a = sys.argv[1:]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(["gh", *a]) + "\\n")
kind = "issue" if os.environ["LOOP_TEST_STAGE"] == "implement" else "pr"
label = (root / "label").read_text()
item = {"number": 7, "title": "Loop fixture", "body": "Fixture contract",
        "state": "OPEN", "author": {"login": "test-owner"},
        "labels": [{"name": label}], "baseRefName": "main", "headRefName": "fix",
        "headRefOid": os.environ["LOOP_TEST_SHA"], "isCrossRepository": False,
        "headRepositoryOwner": {"login": "example"},
        "headRepository": {"name": "loop", "nameWithOwner": "example/loop"},
        "closingIssuesReferences": [], "closedByPullRequestsReferences": [], "statusCheckRollup": [],
        "comments": [{"body": os.environ["LOOP_TEST_REVIEW_MARKER"] + "\\nFix fixture"}]}
result = None
if a == ["--version"]:
    print("gh version 2.75.0"); sys.exit(0)
elif a[0] == "api" and "/labels?" in a[1]:
    result = [[{"name": name} for name in json.loads(os.environ["LOOP_TEST_LABELS"])]]
elif a[:2] in (["issue", "list"], ["pr", "list"]):
    result = [item] if a[0] == kind and "--label" in a and a[a.index("--label") + 1] == label else []
elif a[:2] == [kind, "view"]:
    result = item
elif a[:2] == [kind, "edit"]:
    assert "--add-label" in a, a
    (root / "label").write_text(a[a.index("--add-label") + 1])
    sys.exit(0)
elif a[:2] == [kind, "comment"]:
    print("https://example.invalid/comment/1"); sys.exit(0)
else:
    (root / "unexpected").write_text(repr(a))
    raise AssertionError(a)
if "-q" in a:
    query = a[a.index("-q") + 1]
    if query == ".[].number":
        print("\\n".join(str(x["number"]) for x in result))
    elif query == ".labels[].name":
        print(label)
    elif query.startswith(".") and query[1:] in item:
        print(item[query[1:]])
    else:
        (root / "unexpected").write_text(query)
        raise AssertionError(query)
else:
    print(json.dumps(result))
'''
