"""Offline process proof of ``--only`` through the real ``tick.sh`` CLI.

Only GitHub, fetch, engine, and notification executables are local shims; the
tick, its git worktrees, markers, admission cap, and harvest are production
code. Run from the pipeline checkout::

    .venv/bin/python -m pytest skills/tick/lib/test_only_smoke.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pipeline
import pytest

from test_smoke_loop import ENGINE_SHIM, GIT_SHIM, VERSION_SHIM


TICK_SH = str(Path(__file__).parents[1] / "tick.sh")
EVENTS_SH = str(Path(__file__).parents[2] / "status" / "events.sh")


@pytest.fixture
def only_repo(tmp_path):
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
        'base_ref="main"\nverify_tools=["python -m pytest:*"]\nmax_in_flight=1\n'
    )
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home),
        "PR_PIPELINE_CONFIG": str(config), "PIPELINE_REPO": "example/loop",
        "LOOP_TEST_ROOT": str(repo), "LOOP_TEST_GIT": git, "LOOP_TEST_SHA": sha,
        "LOOP_TEST_STAGE": "implement",
        "LOOP_TEST_LABELS": json.dumps(sorted(pipeline.WORKFLOW_LABELS)),
    }
    yield repo, env
    for marker in (repo / ".claude/worktrees").glob("*/dispatch.json"):
        try:
            os.killpg(json.loads(marker.read_text())["pid"], signal.SIGKILL)
        except (ProcessLookupError, KeyError, ValueError, PermissionError):
            pass


def test_full_tick_harvests_unlisted_engine_at_capacity_then_dispatches_listed(only_repo):
    repo, env = only_repo
    write_items(repo, {
        "issue:3": ["ai:available"],
        "issue:5": ["ai:available"],
        "issue:6": ["ai:available"],
    })

    # Fill the single slot with an engine for #3, which is not on the later list.
    first = run_tick(repo, env, "--only", "#3")
    assert first.returncode == 0, first.stderr
    marker_3 = repo / ".claude/worktrees/issue-3/dispatch.json"
    assert marker_3.exists()
    assert items(repo)["issue:3"] == ["ai:implementing"]
    wait_for_file(repo / "engine-started")
    (repo / "engine-exit").touch()
    wait_for_engine_exit(json.loads(marker_3.read_text())["pid"])
    calls_before = len(calls(repo))

    second = run_tick(repo, env, "--only", "#5")

    assert second.returncode == 0, second.stderr
    state = items(repo)
    # Global HARVEST reaped the unlisted engine and freed its slot ...
    assert state["issue:3"] == ["ai:needs-human-decision"]
    assert not marker_3.exists()
    # ... so the listed issue was dispatched in the same tick.
    assert state["issue:5"] == ["ai:implementing"]
    assert (repo / ".claude/worktrees/issue-5/dispatch.json").exists()
    # The unlisted pickup item received no read and no write.
    assert state["issue:6"] == ["ai:available"]
    new_calls = calls(repo)[calls_before:]
    mutations = [call for call in new_calls if call[1:3] in (["issue", "edit"], ["pr", "edit"])
                 or call[1:3] in (["issue", "comment"], ["pr", "comment"])]
    repo_flag = ["--repo", "github.com/example/loop"]
    edits = [call for call in mutations if call[2] == "edit"]
    # Exact mutating argv, in order: the unlisted engine's harvest relabel
    # frees the slot before the listed launch claims it.
    assert edits == [
        ["gh", "issue", "edit", "3", "--add-label", "ai:needs-human-decision",
         "--remove-label", "ai:implementing", *repo_flag],
        ["gh", "issue", "edit", "5", "--add-label", "ai:implementing",
         "--remove-label", "ai:available", *repo_flag],
    ]
    assert [call[1:4] for call in mutations if call[2] == "comment"] == [
        ["issue", "comment", "3"],
    ]
    assert not [call for call in new_calls if "6" in call[3:4]], new_calls
    assert not (repo / "unexpected").exists(), (repo / "unexpected").read_text()
    cadence = [line for line in second.stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(cadence) == 1 and "--only #5" in cadence[0], second.stderr
    entries = log_entries(repo)
    assert any(entry["action"] == "only_scope_resolved"
               and entry["details"] == {"only": ["#5"], "scope": ["#5"]} for entry in entries)


def test_expansion_failure_harvests_but_starts_no_engine(only_repo):
    repo, env = only_repo
    write_items(repo, {"issue:3": ["ai:available"], "issue:5": ["ai:available"]})
    first = run_tick(repo, env, "--only", "#3")
    assert first.returncode == 0, first.stderr
    marker_3 = repo / ".claude/worktrees/issue-3/dispatch.json"
    wait_for_file(repo / "engine-started")
    (repo / "engine-exit").touch()
    wait_for_engine_exit(json.loads(marker_3.read_text())["pid"])
    env["LOOP_TEST_FAIL_CLOSING_REFS"] = "1"

    second = run_tick(repo, env, "--only", "#5")

    assert second.returncode == 2, second.stderr
    state = items(repo)
    assert state["issue:3"] == ["ai:needs-human-decision"], "harvest must still run"
    assert not marker_3.exists()
    assert state["issue:5"] == ["ai:available"], "no engine may start"
    assert not (repo / ".claude/worktrees/issue-5").exists()
    assert "PARTIAL (dispatch skipped: --only scope unreadable)" in second.stderr
    cadence = [line for line in second.stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(cadence) == 1 and "stop=false" in cadence[0] and "state unknown" in cadence[0]
    assert any(entry["action"] == "only_scope_unreadable" for entry in log_entries(repo))

    # The next tick reads the list again and, with the read restored, launches it.
    del env["LOOP_TEST_FAIL_CLOSING_REFS"]
    third = run_tick(repo, env, "--only", "#5")
    assert third.returncode == 0, third.stderr
    assert items(repo)["issue:5"] == ["ai:implementing"]


def test_native_loop_stops_on_first_tick_when_listed_items_settle(only_repo):
    # Default quiet streak (3): a settled list must not wait for heartbeats.
    repo, env = only_repo
    write_items(repo, {
        "issue:5": ["ai:ready-for-human"],
        "pr:7": ["ai:needs-human-decision"],
        "issue:6": ["ai:available"],
    })

    result = subprocess.run(["bash", TICK_SH, "--loop", "--only", "#5,pr:7"], cwd=repo,
                            env=env, capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    cadence = [line for line in result.stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(cadence) == 1 and "stop=true" in cadence[0], result.stderr
    reason = cadence[0].split('reason="', 1)[1][:-1]
    assert "--only list settled" in reason, reason
    assert "issue #5 ai:ready-for-human" in reason and "pr #7 ai:needs-human-decision" in reason
    assert f"tick: {reason}" in result.stderr
    assert any(entry["action"] == "loop_only_stop" and entry["reason"] == reason
               for entry in log_entries(repo))
    assert items(repo)["issue:6"] == ["ai:available"]


def test_milestone_watch_armed_after_the_loop_stopped_still_reports_the_stop(only_repo):
    repo, env = only_repo
    write_items(repo, {"issue:5": ["ai:ready-for-human"]})
    run_tick(repo, env, "--only", "#5")  # Retained history the watch must skip.
    # Documented order: mark, start the loop, arm the watch. This loop settles
    # and exits on its first tick, before the watch starts.
    cursor = mark(repo, env)
    loop = subprocess.run(["bash", TICK_SH, "--loop", "--only", "#5"], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    assert loop.returncode == 0, loop.stderr

    watch = subprocess.run(["bash", EVENTS_SH, "--follow", "--milestones", "--cursor", cursor],
                           cwd=repo, env=env, capture_output=True, text=True, timeout=30)

    assert watch.returncode == 0, watch.stderr
    lines = watch.stdout.splitlines()
    assert len(lines) == 1, lines
    assert "loop stopped: --only #5: issue #5 ai:ready-for-human; --only list settled" in lines[0]


def test_milestone_watch_armed_after_launch_reports_the_whole_run(only_repo):
    repo, env = only_repo
    write_items(repo, {"issue:5": ["ai:available"]})
    cursor = mark(repo, env)
    loop = subprocess.Popen(["bash", TICK_SH, "--loop", "--only", "#5"], cwd=repo, env=env,
                            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    watch = None
    try:
        wait_for_file(repo / "engine-started")  # The launch precedes the watch.
        watch = subprocess.Popen(["bash", EVENTS_SH, "--follow", "--milestones", "--cursor",
                                  cursor, "--json"], cwd=repo, env=env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert json.loads(watch.stdout.readline())["event"] == "launch"
        (repo / "engine-exit").touch()
        stdout, stderr = watch.communicate(timeout=120)
        _, loop_stderr = loop.communicate(timeout=120)
    finally:
        for proc in (watch, loop):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()

    assert loop.returncode == 0, loop_stderr
    assert watch.returncode == 0, stderr
    events = [json.loads(line) for line in stdout.splitlines()]
    assert [(e["event"], e["number"]) for e in events] == [("terminal", 5), ("stopped", None)]
    assert events[0]["details"]["label"] == "ai:needs-human-decision"
    assert "--only list settled" in events[1]["message"]


def test_native_loop_stops_with_list_exhaustion_when_drained(only_repo):
    repo, env = only_repo
    with Path(env["PR_PIPELINE_CONFIG"]).open("a") as config:
        config.write("loop_quiescent_stop_after=1\n")
    # No ai: label and no closing PR: not dispatchable, but not settled either.
    write_items(repo, {
        "issue:5": [],
        "issue:6": ["ai:available"],
    })

    result = subprocess.run(["bash", TICK_SH, "--loop", "--only", "#5"], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    cadence = [line for line in result.stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(cadence) == 1 and "stop=true" in cadence[0], result.stderr
    reason = cadence[0].split('reason="', 1)[1][:-1]
    assert "--only list exhausted" in reason and "issue #5 no ai: label" in reason
    assert f"tick: {reason}" in result.stderr
    assert any(entry["action"] == "loop_only_stop" and entry["reason"] == reason
               for entry in log_entries(repo))
    # Unlisted work stays queued and untouched; the full-queue counter is not used.
    assert items(repo)["issue:6"] == ["ai:available"]
    counters = repo / ".claude/worktrees/.tick-state/quiescent-counters"
    assert not (counters / "queue.txt").exists()
    assert [path.name.startswith("only-") for path in counters.iterdir()] == [True]


def test_drained_list_stays_warm_while_unlisted_local_engine_runs(only_repo):
    repo, env = only_repo
    with Path(env["PR_PIPELINE_CONFIG"]).open("a") as config:
        config.write("loop_quiescent_stop_after=1\n")
    write_items(repo, {"issue:3": ["ai:available"], "issue:5": ["ai:ready-for-human"]})
    first = run_tick(repo, env, "--only", "#3")
    assert first.returncode == 0, first.stderr
    wait_for_file(repo / "engine-started")

    second = run_tick(repo, env, "--only", "#5")

    assert second.returncode == 0, second.stderr
    cadence = [line for line in second.stderr.splitlines() if line.startswith("tick-cadence:")]
    assert len(cadence) == 1, second.stderr
    assert "stop=false" in cadence[0] and "1 in-flight" in cadence[0], cadence[0]
    assert (repo / ".claude/worktrees/issue-3/dispatch.json").exists()


def test_cli_rejects_invalid_scope_before_any_github_call(only_repo):
    repo, env = only_repo
    write_items(repo, {"issue:5": ["ai:available"]})
    for argv in (["--only", "#5,"], ["--only", "#5", "pr:7"], ["--loop", "#5", "pr:7"]):
        result = run_tick(repo, env, *argv)
        assert result.returncode == 1, result.stderr
    assert not (repo / "calls.jsonl").exists()



def test_listed_prs_beyond_the_queue_page_reach_review_and_fix_gates(only_repo):
    repo, env = only_repo
    # Two full 200-item pages of unlisted PRs. The shim honors --limit and
    # lists in key order, so pr:7 and pr:8 fall beyond both pages.
    state = {f"pr:{n}": ["ai:implemented"] for n in range(1000, 1200)}
    state.update({f"pr:{n}": ["ai:changes-requested"] for n in range(2000, 2200)})
    # A hold makes each listed PR stop at its eligibility gate, which proves
    # the stage considered it without launching an engine.
    state["pr:7"] = ["ai:implemented", "ai:needs-human-decision"]
    state["pr:8"] = ["ai:changes-requested", "ai:needs-human-decision"]
    write_items(repo, state)

    result = run_tick(repo, env, "--only", "pr:7,pr:8")

    assert result.returncode == 0, result.stderr
    assert not (repo / "unexpected").exists(), (repo / "unexpected").read_text()
    gated = {(entry["stage"], entry["pr"]) for entry in log_entries(repo)
             if entry["action"] == "skip" and entry["reason"] == "human hold label present"}
    assert ("review", 7) in gated and ("fix", 8) in gated, gated
    tick_calls = calls(repo)
    pickup = {"ai:implemented", "ai:revised", "ai:changes-requested"}
    assert not [call for call in tick_calls if call[1:3] == ["pr", "list"]
                and pickup & set(call)], "a list tick must not page a pickup queue"
    assert not [call for call in tick_calls if call[1:3] == ["pr", "view"]
                and int(call[3]) >= 1000], "an unlisted PR must receive no read"
    assert not [call for call in tick_calls if call[2:3] in (["edit"], ["comment"])]
    assert items(repo) == state


def test_startup_failure_banner_names_the_requested_list(only_repo):
    repo, env = only_repo
    Path(env["PR_PIPELINE_CONFIG"]).write_text('[operator]\nlogin="test-owner"\n')
    write_items(repo, {"issue:5": ["ai:available"]})

    result = run_tick(repo, env, "--only", "#5,pr:7")

    assert result.returncode == 6, result.stderr
    banner = subprocess.run(
        [sys.executable, str(Path(pipeline.__file__)), "dispatch-scope",
         "--log-dir", str(repo / ".claude/worktrees/.tick-log")],
        capture_output=True, text=True, check=True,
    ).stdout
    assert banner.startswith(
        "--only #5,pr:7 (requested; scope not resolved; no engine started)"
    ), banner
    assert "full queue" not in banner


# --- Helpers ---


def mark(repo, env):
    result = subprocess.run(["bash", EVENTS_SH, "--mark"], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def run_tick(repo, env, *argv):
    return subprocess.run(["bash", TICK_SH, *argv], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)


def write_items(repo, labels):
    (repo / "items.json").write_text(json.dumps(labels))


def items(repo):
    return json.loads((repo / "items.json").read_text())


def calls(repo):
    path = repo / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def log_entries(repo):
    return [json.loads(line) for log in (repo / ".claude/worktrees/.tick-log").glob("*.jsonl")
            for line in log.read_text().splitlines()]


def wait_for_file(path, timeout=60):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    pytest.fail(f"{path} did not appear")


def wait_for_engine_exit(pid, timeout=60):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"engine {pid} did not exit")


GH_SHIM = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ["LOOP_TEST_ROOT"])
a = sys.argv[1:]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(["gh", *a]) + "\\n")
state_path = root / "items.json"
state = json.loads(state_path.read_text())

def item(kind, number):
    labels = state.get(f"{kind}:{number}")
    if labels is None:
        (root / "unexpected").write_text(repr(a))
        raise AssertionError(a)
    return {"number": number, "title": f"Fixture {number}", "body": "Fixture contract",
            "state": "OPEN", "author": {"login": "test-owner"},
            "labels": [{"name": name} for name in labels],
            "closedByPullRequestsReferences": [], "closingIssuesReferences": []}

result = None
if a == ["--version"]:
    print("gh version 2.75.0"); sys.exit(0)
elif a[0] == "api" and "/labels?" in a[1]:
    result = [[{"name": name} for name in json.loads(os.environ["LOOP_TEST_LABELS"])]]
elif a[:2] in (["issue", "list"], ["pr", "list"]):
    label = a[a.index("--label") + 1]
    result = [item(a[0], int(key.split(":")[1])) for key, labels in sorted(state.items())
              if key.startswith(a[0] + ":") and label in labels]
    if "--limit" in a:
        result = result[:int(a[a.index("--limit") + 1])]
elif a[:2] in (["issue", "view"], ["pr", "view"]):
    if os.environ.get("LOOP_TEST_FAIL_CLOSING_REFS") and "closedByPullRequestsReferences" in a:
        print("GraphQL: rate limited", file=sys.stderr); sys.exit(1)
    result = item(a[0], int(a[2]))
elif a[:2] in (["issue", "edit"], ["pr", "edit"]):
    key = f"{a[0]}:{a[2]}"
    labels = state[key]
    if "--remove-label" in a:
        labels = [name for name in labels if name != a[a.index("--remove-label") + 1]]
    added = a[a.index("--add-label") + 1]
    if added not in labels:
        labels.append(added)
    state[key] = labels
    state_path.write_text(json.dumps(state))
    sys.exit(0)
elif a[:2] in (["issue", "comment"], ["pr", "comment"]):
    print("https://example.invalid/comment/1"); sys.exit(0)
elif a[:2] == ["api", "graphql"] and "--slurp" in a:
    # Held PRs read their comment history to decide a resume; none is present.
    result = [{"data": {"repository": {"pullRequest": {"reviews": {"totalCount": 0, "nodes": [],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}, "comments": {
        "totalCount": 0, "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}]
else:
    (root / "unexpected").write_text(repr(a))
    raise AssertionError(a)
if "-q" in a:
    query = a[a.index("-q") + 1]
    if query == ".[].number":
        print("\\n".join(str(x["number"]) for x in result))
    elif query == ".labels[].name":
        print("\\n".join(x["name"] for x in result["labels"]))
    elif query.startswith(".") and query[1:] in result:
        print(result[query[1:]])
    else:
        (root / "unexpected").write_text(query)
        raise AssertionError(query)
else:
    print(json.dumps(result))
'''
