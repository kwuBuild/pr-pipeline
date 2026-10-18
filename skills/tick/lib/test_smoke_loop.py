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
            first = wait_for(lambda: cadence_lines(stderr), proc, stderr, timeout=90)
            cadence_at = time.monotonic()
            assert len(first) == 1
            next_wake = int(re.search(r"next_wake=(\d+)", first[0]).group(1))
            marker = json.loads(marker_path.read_text())
            engine_pid = marker["pid"]
            assert marker["stage"] == stage and marker["pgid"] == engine_pid
            wait_for(lambda: (repo / "engine-started").exists(), proc, stderr, timeout=90)
            # Keep the engine live through one production watcher interval.
            time.sleep(5.5)
            assert len(cadence_lines(stderr)) == 1
            assert marker_path.exists()
            released = time.monotonic()
            (repo / "engine-exit").touch()
            # Production polling is 5s and the nudge floor is 10s. Allow host
            # scheduling overhead; the assertion below still requires an early wake.
            wait_for(lambda: len(cadence_lines(stderr)) >= 2, proc, stderr, timeout=90)
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


@pytest.mark.parametrize("label", ["ai:ready-for-human", "ai:needs-human-decision", "ai:max-iterations"])
def test_targeted_one_shot_stops_on_first_terminal_tick(loop_repo, label):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = "fix"
    (repo / "label").write_text(label)
    result = subprocess.run(
        ["bash", str(Path(__file__).parents[1] / "tick.sh"), "pr:7"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=90,
    )
    (repo / "terminal-proof.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    cadence = [line for line in result.stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(cadence) == 1, result.stderr
    assert "stop=true" in cadence[0], cadence[0]
    assert "pr #7" in cadence[0] and label in cadence[0], cadence[0]
    assert not (repo / "engine-started").exists()


@pytest.mark.parametrize("target", ["pr:8", "#8"])
def test_real_target_loop_stops_on_next_tick_with_unrelated_engine_live(loop_repo, target):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = "fix"
    (repo / "label").write_text("ai:changes-requested")
    script = str(Path(__file__).parents[1] / "tick.sh")
    marker_path = repo / ".claude/worktrees/pr-7/dispatch.json"
    engine_pid = None
    proc = None
    stderr = repo / "target-loop.stderr"
    try:
        dispatch = subprocess.run(["bash", script, "pr:7"], cwd=repo, env=env,
                                  text=True, capture_output=True, timeout=90)
        (repo / "dispatch.stderr").write_text(dispatch.stderr)
        assert dispatch.returncode == 0, dispatch.stderr
        marker = json.loads(marker_path.read_text())
        engine_pid = marker["pid"]
        terminal_number = 8 if target == "pr:8" else 9
        targets = {
            "pr:8": scope_item(8, "ai:blocked"),
            "pr:9": scope_item(9, "ai:blocked"),
            "issue:8": {**scope_item(8), "closedByPullRequestsReferences": [{
                "number": 9, "id": "PR_9", "url": "https://github.com/example/loop/pull/9",
                "repository": {"name": "loop", "owner": {"login": "example"}},
            }]},
        }
        target_file = repo / "target-items.json"
        target_file.write_text(json.dumps(targets))
        with stderr.open("w") as err:
            proc = subprocess.Popen(["bash", script, "--loop", target], cwd=repo, env=env,
                                    stdout=err, stderr=err, start_new_session=True)
            wait_for(lambda: cadence_lines(stderr), proc, stderr, timeout=90)
            assert "stop=false" in cadence_lines(stderr)[0]
            assert "in-flight" in cadence_lines(stderr)[0]
            # The emitted cadence is the loop decision; no second target
            # read can change this tick after the summary appears.
            targets[f"pr:{terminal_number}"]["labels"] = [{"name": "ai:ready-for-human"}]
            replacement = repo / "target-items.new"
            replacement.write_text(json.dumps(targets))
            replacement.replace(target_file)
            (repo / ".claude/worktrees/.tick-state/wake-request").touch()
            wait_for(lambda: len(cadence_lines(stderr)) >= 2, proc, stderr, timeout=90)
            proc.wait(timeout=10)
        assert proc.returncode == 0, stderr.read_text()
        lines = cadence_lines(stderr)
        assert len(lines) == 2, stderr.read_text()
        assert "stop=true" in lines[-1] and "ai:ready-for-human" in lines[-1]
        assert f"pr #{terminal_number}" in lines[-1]
        reason = lines[-1].split('reason="', 1)[1][:-1]
        assert f"tick: {reason}" in stderr.read_text()
        entries = [json.loads(line) for log in (repo / ".claude/worktrees/.tick-log").glob("*.jsonl")
                   for line in log.read_text().splitlines()]
        assert any(entry["action"] == "loop_target_stop" and entry["reason"] == reason for entry in entries)
        assert marker_path.exists(), "target stop must retain unrelated engine custody"
        os.kill(engine_pid, 0)
        assert not (repo / "unexpected").exists()
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        if engine_pid is None and marker_path.exists():
            engine_pid = json.loads(marker_path.read_text()).get("pid")
        if engine_pid is not None:
            try:
                os.killpg(engine_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_real_alternating_target_ticks_and_queue_keep_separate_streaks(loop_repo):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = "fix"
    (repo / "label").write_text("ai:blocked")
    (repo / "target-items.json").write_text(json.dumps({"pr:8": scope_item(8, "ai:blocked")}))
    script = str(Path(__file__).parents[1] / "tick.sh")
    # A pre-upgrade count has no attributable scope and must not be reused.
    legacy = repo / ".claude/worktrees/.tick-state/quiescent-counter.txt"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("99")
    for target in ("pr:7", "pr:8", "pr:7", "pr:8", "pr:7"):
        result = subprocess.run(["bash", script, target], cwd=repo, env=env,
                                text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stderr
        assert "stop=false" in result.stderr and "not terminal" in result.stderr
    counters = repo / ".claude/worktrees/.tick-state/quiescent-counters"
    assert (counters / "pr-7.txt").read_text() == "3"
    assert (counters / "pr-8.txt").read_text() == "2"
    assert not (counters / "queue.txt").exists()
    for stop in (False, False, True):
        result = subprocess.run(["bash", script], cwd=repo, env=env,
                                text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stderr
        assert f"stop={str(stop).lower()}" in result.stderr
    assert (counters / "pr-7.txt").read_text() == "3"
    assert (counters / "pr-8.txt").read_text() == "2"


@pytest.mark.parametrize("mode", [[], ["--loop"]], ids=["one-shot", "native-loop"])
def test_terminal_tick_uses_its_reported_decision_when_later_reads_fail(loop_repo, mode):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = "fix"
    env["LOOP_TEST_FAIL_AFTER_OBSERVATION"] = "1"
    (repo / "label").write_text("ai:blocked")
    (repo / "target-items.json").write_text(json.dumps({
        "pr:8": scope_item(8, "ai:ready-for-human"),
    }))
    stderr = run_until_reported_stop(repo, env, [*mode, "pr:8"])
    lines = [line for line in stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(lines) == 1 and "stop=true" in lines[0], stderr
    reason = lines[0].split('reason="', 1)[1][:-1]
    if mode:
        assert f"tick: {reason}" in stderr
        entries = [json.loads(line) for log in (repo / ".claude/worktrees/.tick-log").glob("*.jsonl")
                   for line in log.read_text().splitlines()]
        assert any(e["action"] == "loop_target_stop" and e["reason"] == reason for e in entries)
    assert len((repo / "target-observations").read_text().splitlines()) == 1


def test_issue_tick_does_not_finish_when_selected_closing_pr_is_abandoned(loop_repo):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = "fix"
    env["LOOP_TEST_ABANDON_CLOSING_PR"] = "1"
    (repo / "label").write_text("ai:blocked")
    (repo / "target-items.json").write_text(json.dumps({
        "issue:8": {**scope_item(8), "closedByPullRequestsReferences": [{
            "number": 9, "id": "PR_9", "url": "https://github.com/example/loop/pull/9",
            "repository": {"name": "loop", "owner": {"login": "example"}},
        }]},
        "pr:9": scope_item(9, "ai:ready-for-human"),
    }))
    result = subprocess.run(
        ["bash", str(Path(__file__).parents[1] / "tick.sh"), "#8"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(lines) == 1 and "stop=false" in lines[0], result.stderr
    assert "state unknown" in lines[0]


def test_real_queue_loop_consumes_the_reported_stop_from_a_retained_streak(loop_repo):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = "fix"
    (repo / "label").write_text("ai:blocked")
    counter = repo / ".claude/worktrees/.tick-state/quiescent-counters/queue.txt"
    counter.parent.mkdir(parents=True)
    counter.write_text("2")
    stderr = run_until_reported_stop(repo, env, ["--loop"])
    lines = [line for line in stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(lines) == 1 and "stop=true" in lines[0], stderr
    assert "loop stopped after consecutive quiescent heartbeats" in stderr
    assert counter.read_text() == "3"


def test_real_queue_nudge_does_not_satisfy_one_heartbeat_limit(loop_repo):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = "fix"
    with Path(env["PR_PIPELINE_CONFIG"]).open("a") as config:
        config.write("loop_quiescent_stop_after=1\n")
    (repo / "label").write_text("ai:changes-requested")
    script = str(Path(__file__).parents[1] / "tick.sh")
    stderr = repo / "nudged-queue.stderr"
    marker_path = repo / ".claude/worktrees/pr-7/dispatch.json"
    counter = repo / ".claude/worktrees/.tick-state/quiescent-counters/queue.txt"
    proc = None
    engine_pid = None
    try:
        with stderr.open("w") as err:
            proc = subprocess.Popen(
                ["bash", script, "--loop"], cwd=repo, env=env,
                stdout=err, stderr=err, start_new_session=True,
            )
            wait_for(lambda: cadence_lines(stderr), proc, stderr, timeout=90)
            engine_pid = json.loads(marker_path.read_text())["pid"]
            wait_for(lambda: (repo / "engine-started").exists(), proc, stderr, timeout=90)
            assert "in-flight" in cadence_lines(stderr)[0]
            assert counter.read_text() == "0"
            (repo / "engine-exit").touch()
            wait_for(lambda: len(cadence_lines(stderr)) >= 2, proc, stderr, timeout=90)
            quiet = cadence_lines(stderr)[1]
            assert "stop=false" in quiet and "heartbeat 0/1" in quiet, stderr.read_text()
            assert counter.read_text() == "0"
            assert proc.poll() is None
            assert "loop stopped after consecutive quiescent heartbeats" not in stderr.read_text()
            # End this wait without spending a real 1800-second heartbeat.
            # A subsequent one-shot tick is a qualifying observation.
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
            assert proc.returncode == 0
        result = subprocess.run(
            ["bash", script], cwd=repo, env=env,
            capture_output=True, text=True, timeout=90,
        )
        assert result.returncode == 0, result.stderr
        assert "stop=true" in result.stderr and "heartbeat 1/1" in result.stderr
        assert counter.read_text() == "1"
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        if engine_pid is None and marker_path.exists():
            engine_pid = json.loads(marker_path.read_text()).get("pid")
        if engine_pid is not None:
            try:
                os.killpg(engine_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# --- Helpers ---


def run_until_reported_stop(repo, env, args):
    """Allow slow CLI startup, then require prompt exit after the cadence line."""
    stderr = repo / "stop.stderr"
    with stderr.open("w") as err:
        proc = subprocess.Popen(
            ["bash", str(Path(__file__).parents[1] / "tick.sh"), *args],
            cwd=repo, env=env, stdout=err, stderr=err, start_new_session=True,
        )
        try:
            wait_for(lambda: cadence_lines(stderr), proc, stderr, timeout=90)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pytest.fail("tick stayed alive after its reported stop:\n" + stderr.read_text())
            assert proc.returncode == 0, stderr.read_text()
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
    return stderr.read_text()


def scope_item(number, label=None):
    return {"number": number, "state": "OPEN", "author": {"login": "test-owner"},
            "labels": [] if label is None else [{"name": label}]}


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
elif a[:2] in (["issue", "view"], ["pr", "view"]) and a[2] != "7":
    targets = json.loads((root / "target-items.json").read_text())
    item = targets[f"{a[0]}:{a[2]}"]
    if a[0] == "pr" and "labels,state" in a:
        if os.environ.get("LOOP_TEST_FAIL_AFTER_OBSERVATION") and (root / "target-observations").exists():
            sys.exit(1)
        if os.environ.get("LOOP_TEST_ABANDON_CLOSING_PR"):
            item["state"] = "CLOSED"
    result = item
    label = "\\n".join(x["name"] for x in item["labels"])
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
if a[:2] == ["pr", "view"] and a[2] != "7" and "labels,state" in a:
    with (root / "target-observations").open("a") as log:
        log.write(json.dumps(result) + "\\n")
'''
