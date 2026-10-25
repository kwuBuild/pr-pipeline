"""Linked issue identity and payload proof through the shipped tick launcher."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from test_smoke_loop import GH_SHIM, loop_repo  # noqa: F401


@pytest.fixture
def context_repo(loop_repo):
    repo, env = loop_repo
    env["LOOP_TEST_STAGE"] = "fix"
    env["PR_PIPELINE_PYTHON"] = sys.executable
    (repo / "label").write_text("ai:changes-requested")
    executable = Path(env["PATH"].split(os.pathsep)[0]) / "gh"
    executable.write_text(f"#!{sys.executable}\n" + CONTEXT_GH_SHIM + GH_SHIM)
    try:
        yield repo, env
    finally:
        marker_path = repo / ".claude/worktrees/pr-7/dispatch.json"
        # Consuming-engine proofs exit on their own. Do not signal a completed
        # fixture from an old marker: its PID/group can already be gone or reused.
        completed = any(path.is_file() and path.stat().st_size > 0
                        for path in (marker_path.parent / "completion.json",
                                     marker_path.parent / "result.json"))
        if marker_path.exists() and not completed:
            pid = json.loads(marker_path.read_text()).get("pid")
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_launcher_preserves_linked_repository_identity(context_repo):
    repo, env = context_repo
    body = "## Summary\n\nCloses #7\nAlso closes other/contracts#7.\n"
    refs = [issue_ref("other/contracts", 7), issue_ref("example/loop", 7)]
    write_context(repo, body, refs, {
        "other/contracts#7": {"title": "Foreign contract", "body": "Foreign requirement"},
        "example/loop#7": {"title": "Local contract", "body": "Local requirement"},
    })

    run_tick(repo, env)

    assert issue_reads(repo) == ["other/contracts#7", "example/loop#7"]
    worktree = repo / ".claude/worktrees/pr-7"
    prompt = (worktree / "prompt.txt").read_text()
    foreign = "### Issue other/contracts#7 — Foreign contract\n\nForeign requirement"
    local = "### Issue example/loop#7 — Local contract\n\nLocal requirement"
    assert foreign in prompt and local in prompt
    assert prompt.index(foreign) < prompt.index(local)
    assert (repo / "label").read_text() == "ai:fixing"
    marker = json.loads((worktree / "dispatch.json").read_text())
    import tick
    assert marker["pr_body"] == tick._pr_body_provenance(body, truncated=False).as_marker_field()


@pytest.mark.parametrize("ref", [
    None, [], {}, {"number": 7},
    {"number": 7, "repository": None},
    {"number": 7, "repository": "other/contracts"},
    {"number": 7, "repository": {}},
    {"number": 7, "repository": {"name": "contracts", "owner": "other"}},
    {"number": 7, "repository": {"name": "contracts", "owner": {}}},
    *[{"number": number, "repository": {"name": "contracts", "owner": {"login": "other"}}}
      for number in (0, -1, True, "7", 7.0, None)],
    *[{"number": 7, "repository": {"name": name, "owner": {"login": owner}}}
      for owner, name in (
        ("", "contracts"), ("-other", "contracts"), ("other", ""), ("other", "."),
        ("other", ".."), ("other", "contracts\n"), ("other", "contracts?x=1"),
        ("other space", "contracts"),
    )],
    {"number": 7, "repository": {"name": "a/b", "owner": {"login": "other"}}},
    {"number": 7, "repository": {"name": 8, "owner": {"login": "other"}}},
    {"number": 7, "repository": {"name": "contracts", "owner": {"login": 8}}},
])
def test_invalid_reference_prevents_all_issue_reads(ref):
    import tick
    # Validate the entire list before the first issue read, even when a valid
    # reference precedes an unknown one. Never fall back to the target repo.
    payload = {"body": "text", "closingIssuesReferences": [issue_ref("example/loop", 7), ref]}
    with mock.patch("tick.gh_json", return_value=payload) as read:
        assert tick._fixer_pr_context(7) is None
    read.assert_called_once_with("pr", "view", "7", "--json", "body,closingIssuesReferences")


@pytest.mark.parametrize("pr_body, issue_body", [
    (None, "Contract"), ("PR description", None), (None, None), ("", ""), (" \n", " \n"),
])
def test_launcher_renders_empty_bodies_explicitly(context_repo, pr_body, issue_body):
    repo, env = context_repo
    write_context(repo, pr_body, [issue_ref("other/.github", 7)], {
        "other/.github#7": {"title": "Title-only issue", "body": issue_body},
    })
    run_tick(repo, env)
    worktree = repo / ".claude/worktrees/pr-7"
    assert (worktree / "prompt.txt").exists(), (repo / "context-proof.stderr").read_text()
    prompt = (worktree / "prompt.txt").read_text()
    expected_pr = (pr_body or "").strip() or "(this PR has an empty body)"
    assert expected_pr in prompt
    expected = (issue_body or "").strip() or "(this issue has an empty body)"
    assert f"### Issue other/.github#7 — Title-only issue\n\n{expected}" in prompt
    assert (repo / "label").read_text() == "ai:fixing"
    assert "pr_body" not in json.loads((worktree / "dispatch.json").read_text())


@pytest.mark.parametrize("failure", ["reference", "pr-body", "issue-body", "pr-read", "issue-read"])
def test_launcher_retains_unknown_context_then_retries(context_repo, failure):
    repo, env = context_repo
    good = {
        "pr": {"body": "## Summary\nCloses #7\n", "closingIssuesReferences": [issue_ref("other/contracts", 7)]},
        "issues": {"other/contracts#7": {"title": "Contract", "body": "Required behavior"},
                   "example/loop#7": {"title": "Local", "body": "Local contract"}},
    }
    bad = json.loads(json.dumps(good))
    if failure == "reference":
        del bad["pr"]["closingIssuesReferences"][0]["repository"]
    elif failure == "pr-body":
        bad["pr"]["body"] = []
    elif failure == "issue-body":
        bad["issues"]["other/contracts#7"]["body"] = False
    elif failure == "pr-read":
        bad["pr"] = "read-failure"
    else:
        bad["issues"]["other/contracts#7"] = "read-failure"
    (repo / "context.json").write_text(json.dumps(bad))
    for _ in range(2):
        run_tick(repo, env)
        assert (repo / "label").read_text() == "ai:changes-requested"
        assert not (repo / ".claude/worktrees/pr-7").exists()
        assert not any(call[0] == "claude" or call[1:3] in (["pr", "edit"], ["pr", "comment"])
                       for call in read_calls(repo))
        entries = [json.loads(line)
                   for path in (repo / ".claude/worktrees/.tick-log").glob("tick-*.jsonl")
                   for line in path.read_text().splitlines()]
        assert any(entry.get("action") == "skip" and "discussion unavailable" in entry.get("reason", "")
                   for entry in entries)
    if failure in {"reference", "pr-body", "pr-read"}:
        assert issue_reads(repo) == []
    else:
        assert all(identity == "other/contracts#7" for identity in issue_reads(repo))

    (repo / "context.json").write_text(json.dumps(good))
    run_tick(repo, env)
    assert (repo / "label").read_text() == "ai:fixing"
    assert "Required behavior" in (repo / ".claude/worktrees/pr-7/prompt.txt").read_text()


@pytest.mark.parametrize("payload", [
    None, [], {}, {"closingIssuesReferences": []}, {"body": "text"},
    *[{"body": value, "closingIssuesReferences": []} for value in (False, 0, [], {})],
    *[{"body": "text", "closingIssuesReferences": value} for value in (None, False, {}, "")],
])
def test_unknown_pr_payload_is_not_empty(payload):
    import tick
    with mock.patch("tick.gh_json", return_value=payload) as read:
        assert tick._fixer_pr_context(7) is None
    assert read.call_count == 1


@pytest.mark.parametrize("payload", [
    None, [], {}, {"title": "Missing body"}, {"body": None},
    *[{"title": "Title", "body": value} for value in (False, 0, [], {})],
    *[{"title": value, "body": None} for value in (None, False, 0, [], {})],
])
def test_unknown_issue_payload_is_not_empty(payload):
    import tick
    pr = {"body": "text", "closingIssuesReferences": [issue_ref("other/contracts", 7)]}
    with mock.patch("tick.gh_json", side_effect=[pr, payload]) as read:
        assert tick._fixer_pr_context(7) is None
    assert read.call_count == 2
    assert read.call_args.kwargs == {"repo": "other/contracts"}


# --- Helpers ---


def issue_ref(repository, number):
    owner, name = repository.split("/")
    return {"number": number, "repository": {"name": name, "owner": {"login": owner}}}


def write_context(repo, body, refs, issues):
    (repo / "context.json").write_text(json.dumps({
        "pr": {"body": body, "closingIssuesReferences": refs}, "issues": issues,
    }))


def run_tick(repo, env):
    result = subprocess.run(
        ["bash", str(Path(__file__).parents[1] / "tick.sh"), "pr:7"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=90,
    )
    with (repo / "context-proof.stderr").open("a") as log:
        log.write(result.stderr)
    assert result.returncode == 0, result.stderr
    assert not (repo / "unexpected").exists(), (repo / "unexpected").read_text()
    return result


def issue_reads(repo):
    identities = []
    for call in read_calls(repo):
        if call[:3] != ["gh", "api", "graphql"]:
            continue
        values = dict(arg.split("=", 1) for arg in call if "=" in arg)
        if "query Source" in values["query"] and "issue(number:" in values["query"]:
            identities.append(values["owner"] + "/" + values["name"] + "#" + values["number"])
    return identities


def read_calls(repo):
    return [json.loads(line) for line in (repo / "calls.jsonl").read_text().splitlines()]


CONTEXT_GH_SHIM = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ["LOOP_TEST_ROOT"])
args = sys.argv[1:]
context_read = args[:3] == ["pr", "view", "7"] and "body,closingIssuesReferences" in args
issue_read = args[:2] == ["issue", "view"]
if context_read or issue_read:
    with (root / "calls.jsonl").open("a") as log:
        log.write(json.dumps(["gh", *args]) + "\\n")
    data = json.loads((root / "context.json").read_text())
    repository = args[args.index("--repo") + 1]
    assert repository.startswith("github.com/"), args
    if context_read:
        assert repository == "github.com/example/loop", args
        result = data["pr"]
    else:
        result = data["issues"].get(repository.removeprefix("github.com/") + "#" + args[2])
    if result == "read-failure":
        sys.exit(1)
    print(json.dumps(result))
    sys.exit(0)
'''
