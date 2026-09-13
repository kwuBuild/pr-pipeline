"""Snapshot tests for gh CLI JSON output shapes (Step 14).

Validates that ``gh --json`` queries used by ``tick.py`` return fields with
the expected types. Catches ``gh`` CLI format drift (field renames, type
changes) before they cause silent failures in production.

Tests are skipped when ``gh`` is not authenticated or there is no network.
Run with::

    uv run pytest .claude/skills/tick/lib/test_gh_snapshots.py -v
"""

from __future__ import annotations

import json
import subprocess

import pytest


def _gh_available() -> bool:
    """Return whether gh is authenticated and usable."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _gh_json(*args: str) -> object:
    """Run gh --json ... and return parsed output. Raises on failure."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


# pylint: disable=missing-function-docstring
pytestmark = pytest.mark.skipif(
    not _gh_available(), reason="gh not authenticated or not available"
)


# --- Issue list shape ---


def test_gh_issue_list_number_title_shape() -> None:
    """gh issue list --json number,title returns list of {number: int, title: str}."""
    data = _gh_json(
        "issue", "list",
        "--label", "ai:available",
        "--state", "open",
        "--json", "number,title",
        "--limit", "1",
    )
    assert isinstance(data, list)
    if data:
        item = data[0]
        assert isinstance(item, dict), f"expected dict, got {type(item)}"
        assert "number" in item, f"missing 'number' key in {item.keys()}"
        assert "title" in item, f"missing 'title' key in {item.keys()}"
        assert isinstance(item["number"], int), (
            f"number should be int, got {type(item['number'])}"
        )
        assert isinstance(item["title"], str), (
            f"title should be str, got {type(item['title'])}"
        )


# --- PR list shape ---


def test_gh_pr_list_number_shape() -> None:
    """gh pr list --json number returns list of {number: int}."""
    data = _gh_json(
        "pr", "list",
        "--state", "open",
        "--json", "number",
        "--limit", "1",
    )
    assert isinstance(data, list)
    if data:
        item = data[0]
        assert isinstance(item, dict)
        assert "number" in item
        assert isinstance(item["number"], int)


# --- PR view shapes ---


def test_gh_pr_view_state_shape() -> None:
    """gh pr view N --json state returns {state: str}."""
    # Get an open PR number first
    prs = _gh_json("pr", "list", "--state", "open", "--json", "number", "--limit", "1")
    if not prs or not isinstance(prs, list):
        pytest.skip("no open PRs to snapshot")
    pr_num = prs[0]["number"]

    data = _gh_json("pr", "view", str(pr_num), "--json", "state")
    assert isinstance(data, dict)
    assert "state" in data
    assert isinstance(data["state"], str)


def test_gh_pr_view_labels_shape() -> None:
    """gh pr view N --json labels returns {labels: [{name: str}, ...]}."""
    prs = _gh_json("pr", "list", "--state", "open", "--json", "number", "--limit", "1")
    if not prs or not isinstance(prs, list):
        pytest.skip("no open PRs to snapshot")
    pr_num = prs[0]["number"]

    data = _gh_json("pr", "view", str(pr_num), "--json", "labels")
    assert isinstance(data, dict)
    assert "labels" in data
    assert isinstance(data["labels"], list)
    if data["labels"]:
        lb = data["labels"][0]
        assert isinstance(lb, dict)
        assert "name" in lb
        assert isinstance(lb["name"], str)


def test_gh_pr_view_headref_base_shape() -> None:
    """gh pr view N --json headRefOid,baseRefName returns expected fields."""
    prs = _gh_json("pr", "list", "--state", "open", "--json", "number", "--limit", "1")
    if not prs or not isinstance(prs, list):
        pytest.skip("no open PRs to snapshot")
    pr_num = prs[0]["number"]

    data = _gh_json(
        "pr", "view", str(pr_num),
        "--json", "headRefOid,baseRefName",
    )
    assert isinstance(data, dict)
    for field in ("headRefOid", "baseRefName"):
        assert field in data, f"missing '{field}' in gh pr view output"
        assert isinstance(data[field], str), (
            f"{field} should be str, got {type(data[field])}"
        )


def test_gh_pr_view_fixer_harvest_point_read_shape() -> None:
    """gh pr view N --json headRefOid,body returns both fields as strings.

    The fixer harvest takes both from one point-read: the head decides whether
    the FIXES_PUSHED claim holds, and the body decides whether the fixer's
    replacement may be published over it. A body field that stopped being a
    plain string would silently withhold every revision.
    """
    prs = _gh_json("pr", "list", "--state", "open", "--json", "number", "--limit", "1")
    if not prs or not isinstance(prs, list):
        pytest.skip("no open PRs to snapshot")
    pr_num = prs[0]["number"]

    data = _gh_json("pr", "view", str(pr_num), "--json", "headRefOid,body")
    assert isinstance(data, dict)
    for field in ("headRefOid", "body"):
        assert field in data, f"missing '{field}' in gh pr view output"
        assert isinstance(data[field], str), (
            f"{field} should be str, got {type(data[field])}"
        )


def test_gh_pr_view_trusted_candidate_shape() -> None:
    """gh pr view N --json author,headRepository,isCrossRepository returns expected shape."""
    prs = _gh_json("pr", "list", "--state", "open", "--json", "number", "--limit", "1")
    if not prs or not isinstance(prs, list):
        pytest.skip("no open PRs to snapshot")
    pr_num = prs[0]["number"]

    data = _gh_json(
        "pr", "view", str(pr_num),
        "--json", "author,headRepository,isCrossRepository",
    )
    assert isinstance(data, dict)
    # author shape
    assert "author" in data
    author = data["author"]
    assert isinstance(author, dict), f"author should be dict, got {type(author)}"
    assert "login" in author, f"author missing 'login': {author.keys()}"
    assert isinstance(author["login"], str)
    # headRepository shape
    assert "headRepository" in data
    repo = data["headRepository"]
    assert isinstance(repo, dict), f"headRepository should be dict, got {type(repo)}"
    assert "nameWithOwner" in repo, (
        f"headRepository missing 'nameWithOwner': {repo.keys()}"
    )
    assert isinstance(repo["nameWithOwner"], str)
    # isCrossRepository
    assert "isCrossRepository" in data
    assert isinstance(data["isCrossRepository"], bool), (
        f"isCrossRepository should be bool, got {type(data['isCrossRepository'])}"
    )


def test_gh_pr_view_comments_shape() -> None:
    """gh pr view N --json comments returns {comments: [...]}."""
    prs = _gh_json("pr", "list", "--state", "open", "--json", "number", "--limit", "1")
    if not prs or not isinstance(prs, list):
        pytest.skip("no open PRs to snapshot")
    pr_num = prs[0]["number"]

    data = _gh_json("pr", "view", str(pr_num), "--json", "comments")
    assert isinstance(data, dict)
    assert "comments" in data
    assert isinstance(data["comments"], list)
    if data["comments"]:
        comment = data["comments"][0]
        assert isinstance(comment, dict)
        assert "body" in comment, f"comment missing 'body': {comment.keys()}"
        assert isinstance(comment["body"], str)


# --- Issue view shapes ---


def test_gh_issue_view_body_shape() -> None:
    """gh issue view N --json body returns {body: str}."""
    issues = _gh_json(
        "issue", "list",
        "--state", "open",
        "--json", "number",
        "--limit", "1",
    )
    if not issues or not isinstance(issues, list):
        pytest.skip("no open issues to snapshot")
    issue_num = issues[0]["number"]

    data = _gh_json("issue", "view", str(issue_num), "--json", "body")
    assert isinstance(data, dict)
    assert "body" in data
    assert isinstance(data["body"], str)
