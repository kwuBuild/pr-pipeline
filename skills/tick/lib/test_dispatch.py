"""Tests for the PR-pipeline DISPATCH phase (Step 10).

Co-located with ``tick.py`` so the whole ``tick/lib`` directory lifts
out cleanly. Run with::

    uv run pytest .claude/skills/tick/lib/test_dispatch.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock

import pytest

from conftest import (  # type: ignore[import-untyped]
    TRUSTED_AUTHOR,
    allow_escalation_comment,
    fixer_gh_json,
    fixer_git,
)


@pytest.fixture(autouse=True)
def isolate_reviewer_config(tmp_path_factory: pytest.TempPathFactory):
    """Point the reviewer config resolver at an empty file, not the operator's.

    Without this, any test that reaches reviewer dispatch reads the developer's
    real ~/.codex/config.toml, so assertions on the emitted model/effort pass or
    fail depending on whose machine runs them.
    """
    codex_home = tmp_path_factory.mktemp("codex-home")
    (codex_home / "config.toml").write_text("")
    with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
        yield


def test_run_tick_body_routes_issue_and_linked_pr_targets_by_stage() -> None:
    """Target #462 scopes implementation to the issue and review/fix to PR 551."""
    import pipeline
    from tick import _run_tick_body  # type: ignore[import-untyped]

    authoritative = [
        pipeline.InFlightRef(kind="issue", number=999, label="ai:implementing"),
    ]
    targets = {
        "reviewer": ("pr", 551),
        "implementer": ("issue", 462),
        "fixer": ("pr", 551),
    }
    with mock.patch("tick._surface_retained_scratch_dirs"):
        with mock.patch("tick._resolve_stage_targets", return_value=targets):
            with mock.patch("tick.harvest_reviewers", return_value=True) as hr:
                with mock.patch("tick.harvest_implementers", return_value=True) as hi:
                    with mock.patch("tick.harvest_fixers", return_value=True) as hf:
                        with mock.patch(
                            "tick.read_authoritative_in_flight_labels",
                            return_value=authoritative,
                        ):
                            with mock.patch("tick.dispatch_reviewers") as dr:
                                with mock.patch("tick.dispatch_implementers") as di:
                                    with mock.patch("tick.dispatch_fixers") as df:
                                        with mock.patch(
                                            "tick._compute_post_dispatch_state",
                                            return_value=(1, False, []),
                                        ):
                                            assert _run_tick_body(
                                                None, ("issue", 462), "claude",
                                            ) == 0

    hr.assert_called_once_with(None, ("pr", 551))
    hi.assert_called_once_with(None, ("issue", 462))
    hf.assert_called_once_with(None, ("pr", 551))
    assert dr.call_args.kwargs == {
        "tick_log": None,
        "target": ("pr", 551),
        "authoritative_in_flight": authoritative,
        "review_model": "",
        "review_effort": "",
        "trusted_author": "",
        "max_in_flight": 3,
    }
    assert di.call_args.kwargs["target"] == ("issue", 462)
    assert di.call_args.kwargs["authoritative_in_flight"] is authoritative
    assert df.call_args.kwargs["target"] == ("pr", 551)
    assert df.call_args.kwargs["authoritative_in_flight"] is authoritative


def test_run_tick_body_fails_closed_when_issue_target_resolution_fails() -> None:
    """A failed issue lookup prevents all reconciliation and dispatch work."""
    from tick import _run_tick_body  # type: ignore[import-untyped]

    with mock.patch("tick._surface_retained_scratch_dirs"):
        with mock.patch(
            "tick._resolve_stage_targets",
            side_effect=subprocess.CalledProcessError(1, ["gh"]),
        ):
            with mock.patch("tick.harvest_reviewers") as harvest:
                with mock.patch("tick.dispatch_reviewers") as dispatch:
                    assert _run_tick_body(None, ("issue", 462), "claude") == 1

    harvest.assert_not_called()
    dispatch.assert_not_called()

# --- dispatch_implementers ---


def test_dispatch_implementers_noop_when_no_available_issues() -> None:
    """dispatch_implementers() is a no-op when no ai:available issues exist."""
    from tick import dispatch_implementers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json_mock:
        gh_json_mock.return_value = []  # no available issues
        with mock.patch("tick.can_dispatch_now") as cdn_mock:
            with mock.patch("tick._dispatch_one_implementer") as dispatch_one:
                dispatch_implementers(
                    tick_log=None, target=None,
                    authoritative_in_flight=[],
                )

    # Must have queried for available issues (comma-separated --json fields)
    gh_json_mock.assert_called_once_with(
        "issue", "list", "--label", "ai:available", "--state", "open",
        "--author", TRUSTED_AUTHOR, "--limit", "200",
        "--json", "number,title,author",
    )
    # Must NOT have checked admission or dispatched — nothing to dispatch
    cdn_mock.assert_not_called()
    dispatch_one.assert_not_called()


def test_dispatch_implementers_skips_when_at_capacity() -> None:
    """dispatch_implementers() skips dispatch when admission denies."""
    import pipeline as _pipeline
    from tick import dispatch_implementers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json_mock:
        gh_json_mock.return_value = [{"number": 461, "title": "PWA icons", "author": {"login": TRUSTED_AUTHOR}}]
        with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
            queue="461", labels="ai:available", body="Issue body text",
        )):
            with mock.patch("tick.can_dispatch_now") as cdn_mock:
                cdn_mock.return_value = _pipeline.AdmissionResult(
                    can_dispatch=False, reason="max_in_flight",
                    in_flight_count=3, max_in_flight=3,
                )
                with mock.patch("tick._dispatch_one_implementer") as dispatch_one:
                    dispatch_implementers(
                        tick_log=None, target=None,
                        authoritative_in_flight=[],
                    )

    # Must have queried and checked admission
    gh_json_mock.assert_called_once()
    cdn_mock.assert_called_once()
    # Must NOT have dispatched — at capacity
    dispatch_one.assert_not_called()


def test_dispatch_implementers_skips_active_provider_cooldown(tmp_path: Path) -> None:
    """An account cooldown blocks implementer dispatch before admission."""
    import pipeline as _pipeline
    from tick import (  # type: ignore[import-untyped]
        EngineCooldowns,
        TickLog,
        dispatch_implementers,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    EngineCooldowns(str(wt_root)).start(
        "claude", until=1_900.0, reason="quota_or_limit",
    )
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.time.time", return_value=1_000.0):
            with mock.patch(
                "tick.gh_json",
                return_value=[{"number": 461, "title": "PWA icons", "author": {"login": TRUSTED_AUTHOR}}],
            ):
                with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
                    queue="461", labels="ai:available", body="Issue body text",
                )):
                    with mock.patch("tick.can_dispatch_now") as admission:
                        admission.return_value = _pipeline.AdmissionResult(
                            can_dispatch=True, reason="",
                            in_flight_count=0, max_in_flight=3,
                        )
                        with mock.patch(
                            "tick._dispatch_one_implementer",
                        ) as dispatch_one:
                            dispatch_implementers(
                                tick_log=tick_log,
                                authoritative_in_flight=[],
                                impl_engine="claude",
                            )

    admission.assert_not_called()
    dispatch_one.assert_not_called()
    entries = tick_log.tail()
    cooldown_skip = next(
        entry for entry in entries if entry["action"] == "cooldown_skip"
    )
    assert cooldown_skip["pr"] == 461
    assert cooldown_skip["details"]["cooldown_until"] == 1_900.0


def test_dispatch_implementers_launches_engine_for_available_issue() -> None:
    """dispatch_implementers() launches an implementer engine when capacity exists."""
    import pipeline as _pipeline
    from tick import dispatch_implementers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json_mock:
        gh_json_mock.return_value = [{"number": 461, "title": "PWA icons", "author": {"login": TRUSTED_AUTHOR}}]
        with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
            queue="461", labels="ai:available", body="Issue body text",
        )):
            with mock.patch("tick.can_dispatch_now") as cdn_mock:
                cdn_mock.return_value = _pipeline.AdmissionResult(
                    can_dispatch=True, reason="",
                    in_flight_count=0, max_in_flight=3,
                )
                with mock.patch("tick._dispatch_one_implementer") as dispatch_one:
                    dispatch_implementers(
                        tick_log=None, target=None,
                        authoritative_in_flight=[],
                    )

    # Must have dispatched exactly one engine for the found issue
    dispatch_one.assert_called_once()
    call_kwargs = dispatch_one.call_args[1]
    assert call_kwargs["issue_number"] == 461
    assert call_kwargs["issue_title"] == "PWA icons"
    assert call_kwargs["issue_body"] == "Issue body text"


def test_dispatch_implementers_skips_human_hold_candidate() -> None:
    """An issue carrying ai:available plus a human hold is never dispatched."""
    import pipeline as _pipeline
    from tick import dispatch_implementers  # type: ignore[import-untyped]

    def gh_text_side_effect(*args: str, **_kwargs: object) -> str:
        if ".labels[].name" in args:
            return "ai:available\nai:blocked"
        return "Issue body text"

    with mock.patch("tick.gh_json") as gh_json_mock:
        gh_json_mock.return_value = [{"number": 461, "title": "PWA icons", "author": {"login": TRUSTED_AUTHOR}}]
        with mock.patch("tick.gh_text", side_effect=gh_text_side_effect):
            with mock.patch("tick.can_dispatch_now") as cdn_mock:
                cdn_mock.return_value = _pipeline.AdmissionResult(
                    can_dispatch=True, reason="",
                    in_flight_count=0, max_in_flight=3,
                )
                with mock.patch("tick._dispatch_one_implementer") as dispatch_one:
                    dispatch_implementers(
                        tick_log=None, target=None,
                        authoritative_in_flight=[],
                    )

    dispatch_one.assert_not_called()


def test_dispatch_implementers_lists_queue_with_scan_limit() -> None:
    """The issue queue query pages past held candidates parked on ai:available.

    Held candidates keep their pre-dispatch label, so a default-size gh page
    they fill would starve eligible work beyond it.
    """
    import tick

    with mock.patch("tick.gh_json") as gh_json_mock:
        gh_json_mock.return_value = []
        tick.dispatch_implementers(
            tick_log=None, target=None,
            authoritative_in_flight=[],
        )

    gh_json_mock.assert_called_once()
    args = gh_json_mock.call_args[0]
    assert args[:2] == ("issue", "list")
    assert ("--limit", str(tick._ACTIONABLE_QUEUE_SCAN_LIMIT)) in zip(
        args, args[1:]
    )


def test_dispatch_implementers_respects_target_filter() -> None:
    """dispatch_implementers() only dispatches the targeted issue."""
    import pipeline as _pipeline
    from tick import dispatch_implementers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json_mock:
        gh_json_mock.return_value = {
            "number": 462, "title": "Other issue", "state": "OPEN",
            "author": {"login": TRUSTED_AUTHOR},
        }
        with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
            queue="461\n462", labels="ai:available", body="Body",
        )):
            with mock.patch("tick.can_dispatch_now") as cdn_mock:
                cdn_mock.return_value = _pipeline.AdmissionResult(
                    can_dispatch=True, reason="",
                    in_flight_count=0, max_in_flight=3,
                )
                with mock.patch("tick._dispatch_one_implementer") as dispatch_one:
                    dispatch_implementers(
                        tick_log=None, target=("issue", 462),
                        authoritative_in_flight=[],
                    )

    # Must only dispatch the targeted issue
    dispatch_one.assert_called_once()
    call_kwargs = dispatch_one.call_args[1]
    assert call_kwargs["issue_number"] == 462


def test_dispatch_implementers_target_mismatch_skips() -> None:
    """dispatch_implementers() skips when target type is not 'issue'."""
    from tick import dispatch_implementers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json_mock:
        with mock.patch("tick._dispatch_one_implementer") as dispatch_one:
            dispatch_implementers(
                tick_log=None, target=("pr", 42),
                authoritative_in_flight=[],
            )

    # Must not query when target type is wrong
    gh_json_mock.assert_not_called()
    dispatch_one.assert_not_called()


def test_dispatch_implementers_logs_skip_when_at_capacity() -> None:
    """dispatch_implementers() logs skip entries when admission denies."""
    import tempfile

    import pipeline as _pipeline
    from tick import TickLog, dispatch_implementers  # type: ignore[import-untyped]
    with tempfile.TemporaryDirectory() as tmpdir:
        tick_log = TickLog.open(tmpdir)
        with mock.patch("tick.gh_json") as gh_json_mock:
            gh_json_mock.return_value = [{"number": 461, "title": "PWA icons", "author": {"login": TRUSTED_AUTHOR}}]
            with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
                queue="461", labels="ai:available", body="Issue body text",
            )):
                with mock.patch("tick.can_dispatch_now") as cdn_mock:
                    cdn_mock.return_value = _pipeline.AdmissionResult(
                        can_dispatch=False, reason="max_in_flight",
                        in_flight_count=3, max_in_flight=3,
                    )
                    with mock.patch(
                        "tick._dispatch_one_implementer",
                    ) as dispatch_one:
                        dispatch_implementers(
                            tick_log=tick_log, target=None,
                            authoritative_in_flight=[],
                        )

        dispatch_one.assert_not_called()
        entries = tick_log.tail()
        skip_entries = [
            e for e in entries
            if e.get("phase") == "dispatch" and e.get("action") == "skip"
        ]
        assert len(skip_entries) >= 1
        assert skip_entries[0]["stage"] == "implement"
        assert skip_entries[0]["reason"] == "admission: max_in_flight"


# --- _dispatch_one_implementer ---


def test_dispatch_one_implementer_creates_worktree_and_launches(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer creates worktree, writes marker, launches engine."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            # atomic_write must create the directory — git worktree add is
            # mocked (returns repo_root string) so the dir is not created
            # by real git; we simulate it here.
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel") as relabel_mock:
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=None,
                        )

    # Must have created the worktree directory
    wt_dir = wt_root / "issue-461"
    assert wt_dir.is_dir()

    # Must have written the dispatch marker (lease then published over it)
    marker_path = wt_dir / "dispatch.json"
    assert marker_path.is_file()
    marker = json.loads(marker_path.read_text())
    assert marker["engine"] == "claude"
    assert marker["stage"] == "implement"
    assert marker["pr_number"] == 461
    assert marker["pre_label"] == "ai:available"
    assert marker["base_ref"] == "main"
    assert "dispatch_id" in marker
    # Published marker has pid, no lease state
    assert marker["pid"] == 12345
    assert "state" not in marker

    # Must have launched the engine via Popen
    popen_mock.assert_called_once()

    # Must have relabeled the issue
    relabel_mock.assert_called_once_with(
        "issue", 461, "ai:available", "ai:implementing",
    )


def test_dispatch_one_implementer_uses_claude_ds_engine(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer uses claude-ds engine when specified."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    fake_env = {"DEEPSEEK_API_KEY": "test-key-123", "HOME": "/tmp", "PATH": "/usr/bin"}

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel"):
                        with mock.patch.dict("os.environ", fake_env, clear=True):
                            _dispatch_one_implementer(
                                issue_number=461,
                                issue_title="PWA icons",
                                tick_log=None,
                                impl_engine="claude-ds",
                            )

    # Verify the published marker records claude-ds
    wt_dir = wt_root / "issue-461"
    marker_path = wt_dir / "dispatch.json"
    marker = json.loads(marker_path.read_text())
    assert marker["engine"] == "claude-ds"

    # Popen must invoke "claude" (not "claude-ds") — claude-ds is a shell
    # function, not a binary, so subprocess.Popen cannot launch it.
    popen_mock.assert_called_once()
    engine_cmd = popen_mock.call_args[0][0]
    wrapper_separator = engine_cmd.index("--")
    assert engine_cmd[wrapper_separator + 1] == "claude", (
        "claude-ds must be translated to the claude binary behind the stream "
        f"wrapper, got: {engine_cmd}"
    )

    # Env must include DeepSeek provider routing vars
    popen_env = popen_mock.call_args[1].get("env", {})
    assert "ANTHROPIC_BASE_URL" in popen_env, (
        "claude-ds dispatch must set ANTHROPIC_BASE_URL"
    )
    assert popen_env.get("ANTHROPIC_AUTH_TOKEN") == "test-key-123", (
        "claude-ds dispatch must set ANTHROPIC_AUTH_TOKEN from DEEPSEEK_API_KEY"
    )


def test_dispatch_one_implementer_handles_missing_codex(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer handles subprocess errors gracefully."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("subprocess.Popen") as popen_mock:
                popen_mock.side_effect = OSError("codex not found")
                with mock.patch("tick.relabel") as relabel_mock:
                    _dispatch_one_implementer(
                        issue_number=461,
                        issue_title="PWA icons",
                        tick_log=None,
                    )

    # Must NOT relabel — the dispatch failed, issue stays at ai:available
    relabel_mock.assert_not_called()

    # Worktree should be cleaned up on failure
    wt_dir = wt_root / "issue-461"
    assert not wt_dir.exists()


# --- dispatch_reviewers ---


def test_dispatch_reviewers_noop_when_no_candidate_prs() -> None:
    """dispatch_reviewers() is a no-op when no implemented/revised PRs exist."""
    from tick import dispatch_reviewers  # type: ignore[import-untyped]

    with mock.patch("tick.verify_codex_bin"):
        with mock.patch("tick.gh_text") as gh_text_mock:
            gh_text_mock.return_value = ""  # no PRs
            with mock.patch("tick.can_dispatch_now") as cdn_mock:
                with mock.patch("tick._dispatch_one_reviewer") as dispatch_one:
                    dispatch_reviewers(
                        tick_log=None, target=None,
                        authoritative_in_flight=[],
                    )

    # Must have queried for both implemented and revised PRs
    assert gh_text_mock.call_count == 2
    cdn_mock.assert_not_called()
    dispatch_one.assert_not_called()


def test_dispatch_reviewers_skips_when_at_capacity() -> None:
    """dispatch_reviewers() skips dispatch when admission denies."""
    import pipeline as _pipeline
    from tick import dispatch_reviewers  # type: ignore[import-untyped]

    with mock.patch("tick.verify_codex_bin"):
        with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
            queue="42", labels="ai:implemented",
        )):
            with mock.patch("tick.can_dispatch_now") as cdn_mock:
                cdn_mock.return_value = _pipeline.AdmissionResult(
                    can_dispatch=False, reason="max_in_flight",
                    in_flight_count=3, max_in_flight=3,
                )
                with mock.patch("tick._dispatch_one_reviewer") as dispatch_one:
                    dispatch_reviewers(
                        tick_log=None, target=None,
                        authoritative_in_flight=[],
                    )

    cdn_mock.assert_called_once()
    dispatch_one.assert_not_called()


def test_dispatch_reviewers_launches_for_implemented_pr() -> None:
    """dispatch_reviewers() launches a reviewer for an ai:implemented PR."""
    import pipeline as _pipeline
    from tick import dispatch_reviewers  # type: ignore[import-untyped]

    with mock.patch("tick.verify_codex_bin"):
        with mock.patch("tick.gh_text") as gh_text_mock:
            # PR 42 is implemented; third read is the hold-label check.
            gh_text_mock.side_effect = ["42", "", "ai:implemented"]
            with mock.patch("tick.gh_json") as gh_json_mock:
                # Trusted candidate gate: return matching metadata
                gh_json_mock.return_value = {
                    "author": {"login": "test-owner"},
                    "headRepository": {"nameWithOwner": "test-owner/test-repo"},
                    "isCrossRepository": False,
                }
                with mock.patch("tick.can_dispatch_now") as cdn_mock:
                    cdn_mock.return_value = _pipeline.AdmissionResult(
                        can_dispatch=True, reason="",
                        in_flight_count=0, max_in_flight=3,
                    )
                    with mock.patch("tick._dispatch_one_reviewer") as dispatch_one:
                        dispatch_reviewers(
                            tick_log=None, target=None,
                            authoritative_in_flight=[],
                        )

    dispatch_one.assert_called_once()
    call_kwargs = dispatch_one.call_args[1]
    assert call_kwargs["pr_number"] == 42
    assert call_kwargs["pre_label"] == "ai:implemented"


def test_dispatch_reviewers_skips_human_hold_candidate() -> None:
    """A PR carrying a pre-dispatch label plus a human hold is never reviewed."""
    import pipeline as _pipeline
    from tick import dispatch_reviewers  # type: ignore[import-untyped]

    def gh_text_side_effect(*args: str, **_kwargs: object) -> str:
        if ".labels[].name" in args:
            return "ai:implemented\nai:needs-human-decision"
        if args[:2] == ("pr", "list"):
            return "42" if "ai:implemented" in args else ""
        return ""

    with mock.patch("tick.verify_codex_bin"):
        with mock.patch("tick.gh_text", side_effect=gh_text_side_effect):
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.return_value = {
                    "author": {"login": "test-owner"},
                    "headRepository": {"nameWithOwner": "test-owner/test-repo"},
                    "isCrossRepository": False,
                }
                with mock.patch("tick.can_dispatch_now") as cdn_mock:
                    cdn_mock.return_value = _pipeline.AdmissionResult(
                        can_dispatch=True, reason="",
                        in_flight_count=0, max_in_flight=3,
                    )
                    with mock.patch("tick._dispatch_one_reviewer") as dispatch_one:
                        dispatch_reviewers(
                            tick_log=None, target=None,
                            authoritative_in_flight=[],
                        )

    dispatch_one.assert_not_called()


def test_dispatch_reviewers_lists_queue_with_scan_limit() -> None:
    """Both pre-dispatch label queries page past held candidates."""
    import tick

    with mock.patch("tick.verify_codex_bin"):
        with mock.patch("tick.gh_text") as gh_text_mock:
            gh_text_mock.return_value = ""
            tick.dispatch_reviewers(
                tick_log=None, target=None,
                authoritative_in_flight=[],
            )

    list_calls = [
        call.args for call in gh_text_mock.call_args_list
        if call.args[:2] == ("pr", "list")
    ]
    assert len(list_calls) == 2
    for args in list_calls:
        assert ("--limit", str(tick._ACTIONABLE_QUEUE_SCAN_LIMIT)) in zip(
            args, args[1:]
        )


def test_dispatch_reviewers_skips_untrusted_candidate() -> None:
    """dispatch_reviewers() skips PRs that fail the trusted-candidate gate."""
    import pipeline as _pipeline
    from tick import dispatch_reviewers  # type: ignore[import-untyped]

    with mock.patch("tick.verify_codex_bin"):
        with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
            queue="42", labels="ai:implemented",
        )):
            with mock.patch("tick.gh_json") as gh_json_mock:
                # Different author — not trusted
                gh_json_mock.return_value = {
                    "author": {"login": "outsider"},
                    "headRepository": {"nameWithOwner": "test-owner/test-repo"},
                    "isCrossRepository": False,
                }
                with mock.patch("tick.can_dispatch_now") as cdn_mock:
                    cdn_mock.return_value = _pipeline.AdmissionResult(
                        can_dispatch=True, reason="",
                        in_flight_count=0, max_in_flight=3,
                    )
                    with mock.patch("tick._dispatch_one_reviewer") as dispatch_one:
                        dispatch_reviewers(
                            tick_log=None, target=None,
                            authoritative_in_flight=[],
                        )

    dispatch_one.assert_not_called()


def test_dispatch_reviewers_respects_target_filter() -> None:
    """dispatch_reviewers() only dispatches the targeted PR."""
    import pipeline as _pipeline
    from tick import dispatch_reviewers  # type: ignore[import-untyped]

    with mock.patch("tick.verify_codex_bin"):
        with mock.patch("tick.gh_text") as gh_text_mock:
            gh_text_mock.side_effect = ["42\n99", "", "ai:implemented"]
            with mock.patch("tick.gh_json") as gh_json_mock:
                gh_json_mock.return_value = {
                    "author": {"login": "test-owner"},
                    "headRepository": {"nameWithOwner": "test-owner/test-repo"},
                    "isCrossRepository": False,
                }
                with mock.patch("tick.can_dispatch_now") as cdn_mock:
                    cdn_mock.return_value = _pipeline.AdmissionResult(
                        can_dispatch=True, reason="",
                        in_flight_count=0, max_in_flight=3,
                    )
                    with mock.patch("tick._dispatch_one_reviewer") as dispatch_one:
                        dispatch_reviewers(
                            tick_log=None, target=("pr", 99),
                            authoritative_in_flight=[],
                        )

    dispatch_one.assert_called_once()
    call_kwargs = dispatch_one.call_args[1]
    assert call_kwargs["pr_number"] == 99


def test_dispatch_reviewers_target_mismatch_skips() -> None:
    """dispatch_reviewers() skips when target type is not 'pr'."""
    from tick import dispatch_reviewers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_text") as gh_text_mock:
        with mock.patch("tick._dispatch_one_reviewer") as dispatch_one:
            dispatch_reviewers(
                tick_log=None, target=("issue", 42),
                authoritative_in_flight=[],
            )

    gh_text_mock.assert_not_called()
    dispatch_one.assert_not_called()


# --- dispatch_fixers ---


def test_dispatch_fixers_noop_when_no_changes_requested() -> None:
    """dispatch_fixers() is a no-op when no ai:changes-requested PRs exist."""
    from tick import dispatch_fixers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_text") as gh_text_mock:
        gh_text_mock.return_value = ""  # no PRs
        with mock.patch("tick.can_dispatch_now") as cdn_mock:
            with mock.patch("tick._dispatch_one_fixer") as dispatch_one:
                dispatch_fixers(
                    tick_log=None, target=None,
                    authoritative_in_flight=[],
                )

    gh_text_mock.assert_called_once()
    cdn_mock.assert_not_called()
    dispatch_one.assert_not_called()


def test_dispatch_fixers_skips_active_provider_cooldown(tmp_path: Path) -> None:
    """The implementer/fixer provider cooldown is shared across stages."""
    import pipeline as _pipeline
    from tick import (  # type: ignore[import-untyped]
        EngineCooldowns,
        TickLog,
        dispatch_fixers,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    EngineCooldowns(str(wt_root)).start(
        "claude", until=1_900.0, reason="quota_or_limit",
    )
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.time.time", return_value=1_000.0):
            with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
                queue="42", labels="ai:changes-requested",
            )):
                with mock.patch("tick.can_dispatch_now") as admission:
                    admission.return_value = _pipeline.AdmissionResult(
                        can_dispatch=True, reason="",
                        in_flight_count=0, max_in_flight=3,
                    )
                    with mock.patch("tick._dispatch_one_fixer") as dispatch_one:
                        dispatch_fixers(
                            tick_log=tick_log,
                            authoritative_in_flight=[],
                            impl_engine="claude",
                        )

    admission.assert_not_called()
    dispatch_one.assert_not_called()
    entries = tick_log.tail()
    cooldown_skip = next(
        entry for entry in entries if entry["action"] == "cooldown_skip"
    )
    assert cooldown_skip["pr"] == 42
    assert cooldown_skip["stage"] == "fix"


def test_dispatch_fixers_launches_for_changes_requested_pr() -> None:
    """dispatch_fixers() launches a fixer for an ai:changes-requested PR."""
    import pipeline as _pipeline
    from tick import dispatch_fixers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
        queue="42", labels="ai:changes-requested",
    )):
        with mock.patch("tick.can_dispatch_now") as cdn_mock:
            cdn_mock.return_value = _pipeline.AdmissionResult(
                can_dispatch=True, reason="",
                in_flight_count=0, max_in_flight=3,
            )
            with mock.patch("tick._dispatch_one_fixer") as dispatch_one:
                dispatch_fixers(
                    tick_log=None, target=None,
                    authoritative_in_flight=[],
                )

    dispatch_one.assert_called_once()
    call_kwargs = dispatch_one.call_args[1]
    assert call_kwargs["pr_number"] == 42


def test_dispatch_fixers_skips_human_hold_candidate() -> None:
    """A PR carrying ai:changes-requested plus a human hold is never fixed."""
    import pipeline as _pipeline
    from tick import dispatch_fixers  # type: ignore[import-untyped]

    def gh_text_side_effect(*args: str, **_kwargs: object) -> str:
        if ".labels[].name" in args:
            return "ai:changes-requested\nai:blocked"
        return "42"

    with mock.patch("tick.gh_text", side_effect=gh_text_side_effect):
        with mock.patch("tick.can_dispatch_now") as cdn_mock:
            cdn_mock.return_value = _pipeline.AdmissionResult(
                can_dispatch=True, reason="",
                in_flight_count=0, max_in_flight=3,
            )
            with mock.patch("tick._dispatch_one_fixer") as dispatch_one:
                dispatch_fixers(
                    tick_log=None, target=None,
                    authoritative_in_flight=[],
                )

    dispatch_one.assert_not_called()


def test_dispatch_fixers_lists_queue_with_scan_limit() -> None:
    """The changes-requested queue query pages past held candidates."""
    import tick

    with mock.patch("tick.gh_text") as gh_text_mock:
        gh_text_mock.return_value = ""
        tick.dispatch_fixers(
            tick_log=None, target=None,
            authoritative_in_flight=[],
        )

    gh_text_mock.assert_called_once()
    args = gh_text_mock.call_args[0]
    assert args[:2] == ("pr", "list")
    assert ("--limit", str(tick._ACTIONABLE_QUEUE_SCAN_LIMIT)) in zip(
        args, args[1:]
    )


def test_dispatch_fixers_respects_target_filter() -> None:
    """dispatch_fixers() only dispatches the targeted PR."""
    import pipeline as _pipeline
    from tick import dispatch_fixers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
        queue="42\n99", labels="ai:changes-requested",
    )):
        with mock.patch("tick.can_dispatch_now") as cdn_mock:
            cdn_mock.return_value = _pipeline.AdmissionResult(
                can_dispatch=True, reason="",
                in_flight_count=0, max_in_flight=3,
            )
            with mock.patch("tick._dispatch_one_fixer") as dispatch_one:
                dispatch_fixers(
                    tick_log=None, target=("pr", 99),
                    authoritative_in_flight=[],
                )

    dispatch_one.assert_called_once()
    call_kwargs = dispatch_one.call_args[1]
    assert call_kwargs["pr_number"] == 99


def test_dispatch_fixers_target_mismatch_skips() -> None:
    """dispatch_fixers() skips when target type is not 'pr'."""
    from tick import dispatch_fixers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_text") as gh_text_mock:
        with mock.patch("tick._dispatch_one_fixer") as dispatch_one:
            dispatch_fixers(
                tick_log=None, target=("issue", 42),
                authoritative_in_flight=[],
            )

    gh_text_mock.assert_not_called()
    dispatch_one.assert_not_called()


# --- M2: verify_codex_bin called before reviewer dispatch ---


def test_dispatch_reviewers_calls_verify_codex_bin() -> None:
    """dispatch_reviewers must verify the pinned Codex version before launching.

    An unpinned codex binary could produce unvalidated reviews. The version
    check gates the entire reviewer dispatch phase — one mismatch blocks all
    reviewer launches for this tick.
    """
    from tick import dispatch_reviewers  # type: ignore[import-untyped]

    with mock.patch("tick.verify_codex_bin") as vcb:
        vcb.side_effect = RuntimeError("codex not found on PATH")
        with mock.patch("tick.gh_text") as gh_text_mock:
            gh_text_mock.return_value = ""
            dispatch_reviewers(
                tick_log=None, target=None,
                authoritative_in_flight=[],
            )

    vcb.assert_called_once()


# --- M3: relabel failure triggers cleanup in dispatch paths ---


def test_dispatch_one_reviewer_terminates_on_relabel_failure(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must terminate engine + cleanup on relabel failure.

    If relabel raises CalledProcessError, the engine is running but the PR
    still shows the pre-dispatch label — the next tick would re-dispatch a
    duplicate. The fix terminates the engine and deletes the marker so the
    next tick sees a clean slate (Invariant 3: label flip failure → lease
    deleted).
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch("tick._publish_marker_or_revert") as pmr:
                        pmr.return_value = True
                        with mock.patch("tick.relabel") as relabel_mock:
                            relabel_mock.side_effect = (
                                subprocess.CalledProcessError(1, ["gh"])
                            )
                            with mock.patch("tick.cleanup_scratch") as cleanup:
                                with mock.patch("subprocess.Popen") as popen:
                                    def launch_with_evidence(
                                        *args: object, **kwargs: object,
                                    ) -> mock.MagicMock:
                                        cwd = Path(str(kwargs["cwd"]))
                                        cwd.mkdir(parents=True, exist_ok=True)
                                        (cwd / "recovery.txt").write_text(
                                            "reviewer label-flip evidence",
                                        )
                                        return proc_mock

                                    popen.side_effect = launch_with_evidence
                                    with mock.patch(
                                        "tick._terminate_engine", return_value=True,
                                    ) as terminate_engine:
                                        _dispatch_one_reviewer(
                                            pr_number=42,
                                            pre_label="ai:implemented",
                                            tick_log=None,
                                        )

        # Must have attempted to terminate the engine
    terminate_engine.assert_called_once()
    # Must clean up scratch so next tick sees clean slate
    cleanup.assert_called_once()
    archives = list(
        (wt_root / ".harvest-evidence").glob("pr-42-review-42-*"),
    )
    assert len(archives) == 1
    assert (
        archives[0] / "scratch" / "recovery.txt"
    ).read_text() == "reviewer label-flip evidence"


def test_dispatch_one_implementer_terminates_on_relabel_failure(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer must terminate engine + cleanup on relabel failure."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("tick._publish_marker_or_revert") as pmr:
                    pmr.return_value = True
                    with mock.patch("tick.relabel") as relabel_mock:
                        relabel_mock.side_effect = (
                            subprocess.CalledProcessError(1, ["gh"])
                        )
                        with mock.patch("tick.cleanup_scratch") as cleanup:
                            with mock.patch("subprocess.Popen") as popen:
                                def launch_with_evidence(
                                    *args: object, **kwargs: object,
                                ) -> mock.MagicMock:
                                    cwd = Path(str(kwargs["cwd"]))
                                    cwd.mkdir(parents=True, exist_ok=True)
                                    (cwd / "recovery.txt").write_text(
                                        "implementer label-flip evidence",
                                    )
                                    return proc_mock

                                popen.side_effect = launch_with_evidence
                                with mock.patch(
                                    "tick._terminate_engine", return_value=True,
                                ) as terminate_engine:
                                    _dispatch_one_implementer(
                                        issue_number=7,
                                        issue_title="Test",
                                        tick_log=None,
                                    )

    terminate_engine.assert_called_once()
    cleanup.assert_called_once()
    archives = list(
        (wt_root / ".harvest-evidence").glob("issue-7-impl-7-*"),
    )
    assert len(archives) == 1
    assert (
        archives[0] / "scratch" / "recovery.txt"
    ).read_text() == "implementer label-flip evidence"


def test_dispatch_one_fixer_terminates_on_relabel_failure(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer must terminate engine + cleanup on relabel failure."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ):
            with mock.patch("tick.gh_text") as gh_text_mock:
                # base_ref, head_sha, head_ref (3 values now)
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    # Return a review comment so the fixer proceeds past
                    # the review-extraction gate to engine launch + relabel.
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[
                            {"body": "<!-- pr-pipeline:review -->\nfindings here"},
                        ],
                    )
                    with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                        aw_mock.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch("tick._publish_marker_or_revert") as pmr:
                            pmr.return_value = True
                            with mock.patch("tick.relabel") as relabel_mock:
                                relabel_mock.side_effect = (
                                    subprocess.CalledProcessError(1, ["gh"])
                                )
                                with mock.patch("tick.cleanup_scratch") as cleanup:
                                    with mock.patch("subprocess.Popen") as popen:
                                        def launch_with_evidence(
                                            *args: object, **kwargs: object,
                                        ) -> mock.MagicMock:
                                            cwd = Path(str(kwargs["cwd"]))
                                            cwd.mkdir(parents=True, exist_ok=True)
                                            (cwd / "recovery.txt").write_text(
                                                "fixer label-flip evidence",
                                            )
                                            return proc_mock

                                        popen.side_effect = launch_with_evidence
                                        with mock.patch(
                                            "tick._terminate_engine",
                                            return_value=True,
                                        ) as terminate_engine:
                                            _dispatch_one_fixer(
                                                pr_number=42,
                                                tick_log=None,
                                            )

    terminate_engine.assert_called_once()
    cleanup.assert_called_once()
    archives = list(
        (wt_root / ".harvest-evidence").glob("pr-42-fix-42-*"),
    )
    assert len(archives) == 1
    assert (
        archives[0] / "scratch" / "recovery.txt"
    ).read_text() == "fixer label-flip evidence"


def test_dispatch_one_reviewer_escalates_on_base_fetch_failure(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must fail closed when base-ref fetch fails."""
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def git_side_effect(*args: str) -> str:
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo_root)
        if args == ("-C", str(repo_root), "fetch", "--quiet", "origin", "main"):
            raise subprocess.CalledProcessError(1, ["git", "fetch"])
        raise AssertionError(f"unexpected git call: {args!r}")

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.return_value = "main"
                with mock.patch("tick.gh", side_effect=allow_escalation_comment()):
                    with mock.patch("tick.relabel") as relabel_mock:
                        _dispatch_one_reviewer(
                            pr_number=42,
                            pre_label="ai:implemented",
                            tick_log=None,
                        )

    relabel_mock.assert_called_once_with(
        "pr", 42, "ai:implemented", "ai:needs-human-decision",
    )


def test_dispatch_one_reviewer_never_guesses_main_on_base_metadata_failure(
    tmp_path: Path,
) -> None:
    """A failed baseRefName read retains the PR for retry — never guesses main.

    A stacked PR's base is another branch: guessing "main" would compute a
    plausible merge-base against the wrong target, record "main" as the
    freshness ref, and could promote a review against a base the PR does not
    have. Without authoritative base metadata there is no valid diff or
    freshness contract.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    git_calls: list[tuple[str, ...]] = []

    def git_side_effect(*args: str) -> str:
        git_calls.append(args)
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo_root)
        raise AssertionError(f"unexpected git call: {args!r}")

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = subprocess.CalledProcessError(
                    1, ["gh", "pr", "view"],
                )
                with mock.patch("tick.relabel") as relabel_mock:
                    with mock.patch("subprocess.Popen") as popen:
                        _dispatch_one_reviewer(
                            pr_number=42,
                            pre_label="ai:implemented",
                            tick_log=None,
                        )

    # No label mutation, no engine launch, and no git operation against a
    # guessed base ref — the PR is simply retained for the next tick.
    relabel_mock.assert_not_called()
    popen.assert_not_called()
    assert all("main" not in call for call in git_calls)


def test_dispatch_one_fixer_never_guesses_main_on_base_metadata_failure(
    tmp_path: Path,
) -> None:
    """The fixer sibling of the reviewer guard: a failed baseRefName read
    retains the PR for retry rather than recording main as the base."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    git_calls: list[tuple[str, ...]] = []

    def git_side_effect(*args: str) -> str:
        git_calls.append(args)
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo_root)
        raise AssertionError(f"unexpected git call: {args!r}")

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = subprocess.CalledProcessError(
                    1, ["gh", "pr", "view"],
                )
                with mock.patch("tick.relabel") as relabel_mock:
                    with mock.patch("subprocess.Popen") as popen:
                        _dispatch_one_fixer(
                            pr_number=42,
                            tick_log=None,
                        )

    relabel_mock.assert_not_called()
    popen.assert_not_called()
    assert all("main" not in call for call in git_calls)


def test_dispatch_one_implementer_escalates_on_base_fetch_failure(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer must fail closed when base-ref fetch fails."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def git_side_effect(*args: str) -> str:
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo_root)
        if args == ("-C", str(repo_root), "fetch", "--quiet", "origin", "main"):
            raise subprocess.CalledProcessError(1, ["git", "fetch"])
        raise AssertionError(f"unexpected git call: {args!r}")

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh", side_effect=allow_escalation_comment()):
                with mock.patch("tick.relabel") as relabel_mock:
                    _dispatch_one_implementer(
                        issue_number=461,
                        issue_title="PWA icons",
                        tick_log=None,
                    )

    relabel_mock.assert_called_once_with(
        "issue", 461, "ai:available", "ai:needs-human-decision",
    )


def test_dispatch_one_implementer_branches_from_the_configured_base_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The implementer must branch from the operator's configured base_ref.

    Not every repository's trunk is called ``main``. A hard-coded one silently
    branches a ``master``/``trunk`` repository from a ref that does not exist —
    the fetch fails and every issue escalates — or, worse on a repo that has
    both, builds the PR on a stale branch nobody merges to.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    cfg = tmp_path / "config.toml"
    cfg.write_text('[defaults]\nbase_ref = "trunk"\n', encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fetched: list[tuple[str, ...]] = []

    def git_side_effect(*args: str) -> str:
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo_root)
        if "fetch" in args:
            fetched.append(args)
            raise subprocess.CalledProcessError(1, ["git", "fetch"])
        raise AssertionError(f"unexpected git call: {args!r}")

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh", side_effect=allow_escalation_comment()):
                with mock.patch("tick.relabel"):
                    _dispatch_one_implementer(
                        issue_number=461,
                        issue_title="PWA icons",
                        tick_log=None,
                    )

    assert fetched == [
        ("-C", str(repo_root), "fetch", "--quiet", "origin", "trunk"),
    ]


def test_dispatch_one_fixer_escalates_on_worktree_creation_failure(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer must fail closed when worktree setup fails."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def git_side_effect(*args: str) -> str:
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo_root)
        if args == ("-C", str(repo_root), "fetch", "--quiet", "origin", "main"):
            return ""
        if args == ("-C", str(repo_root), "rev-parse", "origin/main"):
            return "base123"
        if args == ("-C", str(repo_root), "worktree", "prune"):
            return ""
        if args == ("-C", str(repo_root), "fetch", "--quiet", "origin", "feature/pr-42"):
            return ""
        if args == (
            "-C", str(repo_root), "worktree", "add",
            "--detach", str(wt_root / "pr-42"), "origin/feature/pr-42",
        ):
            raise subprocess.CalledProcessError(1, ["git", "worktree", "add"])
        raise AssertionError(f"unexpected git call: {args!r}")

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "head123", "feature/pr-42"]
                with mock.patch("tick.gh_json", side_effect=fixer_gh_json()):
                    with mock.patch(
                        "tick.gh", side_effect=allow_escalation_comment(),
                    ):
                        with mock.patch("tick.relabel") as relabel_mock:
                            with mock.patch("tick.cleanup_scratch") as cleanup_mock:
                                _dispatch_one_fixer(pr_number=42, tick_log=None)

    relabel_mock.assert_called_once_with(
        "pr", 42, "ai:changes-requested", "ai:needs-human-decision",
    )
    cleanup_mock.assert_called_once_with(str(wt_root / "pr-42"))


# --- m3: Fixer dispatch extracts pipeline review comment ---


def test_dispatch_one_fixer_fetches_review_comment_for_prompt(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer must fetch the pipeline review comment and include
    findings in the fixer prompt.

    The old code built a generic prompt telling the fixer to "read the review
    comments" — but the engine has no context about which findings to fix.
    The fix extracts the most recent pipeline review comment and passes
    specific findings, severities, and files to the engine.
    """
    import pipeline
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    review_comment_body = (
        "<!-- pr-pipeline:review -->\n"
        "## Review Findings\n"
        "- **blocker** in `app/auth.py`: SQL injection in login handler\n"
        "- **major** in `app/api.py`: Missing rate limiting\n"
        "base: abc123 | head: def456"
    )

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ):
            with mock.patch("tick.gh_text") as gh_text_mock:
                # base_ref, head_sha, head_ref (3 values now)
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    # Return PR comments including the pipeline review
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[
                            {"body": "Human note"},
                            {"body": review_comment_body},
                        ],
                    )
                    with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                        aw_mock.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch("tick._publish_marker_or_revert") as pmr:
                            pmr.return_value = True
                            with mock.patch("tick.relabel"):
                                with mock.patch("subprocess.Popen") as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_fixer(
                                        pr_number=42,
                                        tick_log=None,
                                    )

    # Must have queried for PR comments
    gh_json_mock.assert_any_call(
        "pr", "view", "42", "--json", "comments",
    )
    # The fixer prompt must include review findings, not just a generic message
    popen.assert_called_once()
    prompt = (wt_root / "pr-42" / "prompt.txt").read_text()
    assert "SQL injection" in prompt, (
        f"fixer prompt must include specific findings, got: {prompt[:200]}"
    )
    assert "app/auth.py" in prompt, (
        f"fixer prompt must include file paths from review, got: {prompt[:200]}"
    )
    expected_digest = hashlib.sha256(review_comment_body.encode()).hexdigest()
    lease = json.loads((wt_root / "pr-42" / "dispatch.json").read_text())
    for marker in (lease, pmr.call_args.kwargs["published_marker"]):
        assert marker["review_sha256"] == expected_digest
        assert pipeline.valid_marker(marker, expected_stage="fix")


@pytest.mark.parametrize(
    ("impl_engine", "model_override", "expected"),
    [
        ("claude", "", None),
        ("claude", "claude-opus-5", "claude-opus-5"),
        ("claude-ds", "", "deepseek-v4-pro[1m]"),
        ("claude-ds", "deepseek-v4-flash", "deepseek-v4-flash"),
    ],
)
def test_dispatch_one_fixer_model_selection_matches_implementer(
    tmp_path: Path,
    impl_engine: str,
    model_override: str,
    expected: str | None,
) -> None:
    """Fixer model selection is symmetric with the implementer.

    ``expected is None`` means the flag must be absent so the operator's own
    claude config decides. claude-ds keeps a concrete default because the
    DeepSeek endpoint cannot resolve Anthropic model aliases.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
        with mock.patch("tick.WT_ROOT", str(wt_root)):
            with mock.patch(
                "tick.git", side_effect=fixer_git(repo_root),
            ):
                with mock.patch("tick.gh_text") as gh_text_mock:
                    gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                    with mock.patch("tick.gh_json") as gh_json_mock:
                        gh_json_mock.side_effect = fixer_gh_json(comments=[{
                            "body": (
                                "<!-- pr-pipeline:review -->\n"
                                "## Review Findings\n"
                                "- **major** in `app/api.py`: no rate limit\n"
                                "base: abc123 | head: def456"
                            ),
                        }])
                        with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                            aw_mock.side_effect = lambda path, data: (
                                Path(path).parent.mkdir(
                                    parents=True, exist_ok=True
                                )
                                or Path(path).write_text(data)
                            )
                            with mock.patch(
                                "tick._publish_marker_or_revert",
                                return_value=True,
                            ):
                                with mock.patch("tick.relabel"):
                                    with mock.patch("subprocess.Popen") as popen:
                                        popen.return_value = proc_mock
                                        _dispatch_one_fixer(
                                            pr_number=42,
                                            tick_log=None,
                                            impl_engine=impl_engine,
                                            model=model_override,
                                        )

    engine_cmd = popen.call_args[0][0]
    if expected is None:
        assert "--model" not in engine_cmd, (
            f"unset override must omit --model so the operator default "
            f"applies, got: {engine_cmd}"
        )
    else:
        assert engine_cmd[engine_cmd.index("--model") + 1] == expected, (
            f"expected --model {expected}, got: {engine_cmd}"
        )

    if impl_engine == "claude-ds":
        env = popen.call_args[1]["env"]
        assert env["ANTHROPIC_MODEL"] == expected, (
            f"claude-ds must keep routing ANTHROPIC_MODEL, got: "
            f"{env.get('ANTHROPIC_MODEL')!r}"
        )


def test_dispatch_one_fixer_hands_off_fresh_post_retarget_review(
    tmp_path: Path,
) -> None:
    """The fixer handoff is the freshest review comment, not a stale
    SHA-identical round from before a base retarget.

    Companion to the harvest regression: once the post-retarget round's
    comment is posted (base-ref-keyed replay token), the fixer must pick it
    up — the most recent marker comment — rather than the earlier clean
    review posted under the old base name.
    """
    import pipeline
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    stale_clean = pipeline.render_review_comment(
        verdict="ready_for_human",
        findings=[],
        base_sha="mb1",
        base_tip_sha="t1",
        head_sha="h1",
        base_ref="feat-1",
    )
    fresh_changes = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[
            {"severity": "blocker", "file": "a.py", "summary": "boom"},
        ],
        base_sha="mb1",
        base_tip_sha="t1",
        head_sha="h1",
        base_ref="main",
    )

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[
                            {"body": stale_clean},
                            {"body": fresh_changes},
                        ],
                    )
                    with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                        aw_mock.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch("tick._publish_marker_or_revert") as pmr:
                            pmr.return_value = True
                            with mock.patch("tick.relabel"):
                                with mock.patch("subprocess.Popen") as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_fixer(
                                        pr_number=42,
                                        tick_log=None,
                                    )

    popen.assert_called_once()
    prompt = (wt_root / "pr-42" / "prompt.txt").read_text()
    assert "boom" in prompt, (
        f"fixer prompt must carry the fresh round's findings, got: {prompt[:300]}"
    )
    assert "No blockers" not in prompt, (
        "fixer prompt must not hand off the stale pre-retarget clean review"
    )


# --- M1: Reviewer dispatch uses scrubbed env ---


def test_dispatch_one_reviewer_passes_scrubbed_env_to_popen(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must pass a scrubbed env dict to Popen.

    The design requires env -i isolation: CODEX_HOME in a tick-owned scrub
    dir, HOME pointing to the scrub dir, and only the design's allowlist
    keys (no provider API keys). The full parent environment must NOT be
    leaked to the reviewer engine — it's a credential exposure boundary.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch("tick._publish_marker_or_revert") as pmr:
                        pmr.return_value = True
                        with mock.patch("tick.relabel"):
                            with mock.patch("subprocess.Popen") as popen:
                                proc_mock = mock.MagicMock()
                                proc_mock.pid = 12345
                                popen.return_value = proc_mock
                                _dispatch_one_reviewer(
                                    pr_number=42,
                                    pre_label="ai:implemented",
                                    tick_log=None,
                                )

    popen.assert_called_once()
    popen_kwargs = popen.call_args[1]
    env = popen_kwargs.get("env")
    assert env is not None, (
        "reviewer dispatch must pass an explicit env dict (env -i isolation)"
    )
    assert "HOME" in env, "scrubbed env must set HOME"
    assert "CODEX_HOME" in env, "scrubbed env must set CODEX_HOME"
    assert "PATH" in env, "scrubbed env must include PATH"
    # The full parent env must NOT leak — no SSH_AUTH_SOCK, no arbitrary vars
    assert "SSH_AUTH_SOCK" not in env, (
        "SSH_AUTH_SOCK must not leak to reviewer engine"
    )


# --- Reviewer env exact-key-set assertion (#523) ---


def test_reviewer_env_exact_key_set(
    tmp_path: Path,
) -> None:
    """Reviewer env must contain exactly the design's allowlist keys, no more.

    The design's reviewer isolation contract (env -i allowlist) requires:
    PATH, HOME (scrub dir), CODEX_HOME, GIT_TERMINAL_PROMPT, TMPDIR.
    Provider API keys (DEEPSEEK_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY)
    must NOT be present — the reviewer authenticates via CODEX_HOME/auth.json,
    not provider keys, and forwarding them to the prompt-injection surface
    enables exfiltration through posted review comments.

    This test asserts the exact key set as a literal, so any future addition
    fails loudly instead of drifting silently.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    fake_env = {
        "HOME": "/Users/operator",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "DEEPSEEK_API_KEY": "sk-ds-secret",
        "OPENAI_API_KEY": "sk-oai-secret",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "GH_TOKEN": "ghp_secret",
    }

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch("tick._publish_marker_or_revert") as pmr:
                        pmr.return_value = True
                        with mock.patch("tick.relabel"):
                            with mock.patch("subprocess.Popen") as popen:
                                proc_mock = mock.MagicMock()
                                proc_mock.pid = 12345
                                popen.return_value = proc_mock
                                with mock.patch.dict(
                                    "os.environ", fake_env, clear=True,
                                ):
                                    _dispatch_one_reviewer(
                                        pr_number=42,
                                        pre_label="ai:implemented",
                                        tick_log=None,
                                    )

    popen.assert_called_once()
    env = popen.call_args[1].get("env", {})

    allowed_keys = {
        "HOME",
        "PATH",
        "CODEX_HOME",
        "GIT_TERMINAL_PROMPT",
        "TMPDIR",
    }
    assert set(env.keys()) == allowed_keys, (
        f"reviewer env must contain exactly {sorted(allowed_keys)}, "
        f"got {sorted(env.keys())}. "
        f"Extra: {sorted(set(env.keys()) - allowed_keys)}. "
        f"Missing: {sorted(allowed_keys - set(env.keys()))}"
    )

    # Provider keys must NEVER be present
    for forbidden in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert forbidden not in env, (
            f"{forbidden} must not be forwarded to reviewer env — "
            f"reviewer authenticates via CODEX_HOME/auth.json, not provider keys"
        )

    # HOME must be the scrub dir, not the real home
    assert env["HOME"] != "/Users/operator", (
        "HOME must be a scrub dir, not the operator's real home"
    )

    # GIT_TERMINAL_PROMPT must be "0" to prevent interactive prompts
    assert env.get("GIT_TERMINAL_PROMPT") == "0", (
        "GIT_TERMINAL_PROMPT must be '0' to prevent interactive prompts in headless mode"
    )


# --- Reviewer prompt PR-metadata injection ---


def test_reviewer_prompt_carries_injected_pr_metadata(
    tmp_path: Path,
) -> None:
    """The tick fetches PR title/body itself and injects them into the prompt.

    The reviewer engine runs with a scrubbed, credential-free environment —
    no ``GH_TOKEN``, no ``GH_CONFIG_DIR`` — so a prompt instructing it to run
    ``gh pr view`` can only produce an authentication failure. The trusted
    tick process (which has gh auth) must fetch the title/body before launch
    and embed them as delimited context, while the launched env stays exactly
    the credential-free allowlist.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123"]  # base_ref, head
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.return_value = {
                        "title": "fix: harden the frobnicator",
                        "body": "## Summary\nfrobnication drift under load.",
                    }
                    with mock.patch(
                        "tick.pipeline.touches_review_policy",
                        return_value=False,
                    ):
                        with mock.patch("tick.pipeline.atomic_write"):
                            with mock.patch(
                                "tick._publish_marker_or_revert",
                                return_value=True,
                            ):
                                with mock.patch("tick.relabel"):
                                    with mock.patch(
                                        "subprocess.Popen"
                                    ) as popen:
                                        popen.return_value = proc_mock
                                        _dispatch_one_reviewer(
                                            pr_number=42,
                                            pre_label="ai:implemented",
                                            tick_log=None,
                                        )

    popen.assert_called_once()
    engine_cmd = popen.call_args[0][0]

    assert "--observation-path" in engine_cmd
    observation_path = engine_cmd[engine_cmd.index("--observation-path") + 1]
    assert observation_path == str(wt_root / "pr-42" / "review-observation.json")
    sep_idx = engine_cmd.index("--")
    codex_argv = engine_cmd[sep_idx + 1:]
    assert "--json" in codex_argv, (
        f"reviewer must emit streaming JSON events for sanitized observation, "
        f"got: {codex_argv}"
    )
    prompt = codex_positionals(codex_argv)[0]

    # The metadata fetched by the trusted tick is embedded in the prompt.
    assert "fix: harden the frobnicator" in prompt
    assert "frobnication drift under load." in prompt
    # The prompt must not tell the credential-free engine to call gh itself.
    assert "gh pr view" not in prompt

    # The read used the tick's own authenticated gh access, pre-launch.
    gh_json_mock.assert_any_call("pr", "view", "42", "--json", "title,body")

    # The launched env remains exactly the credential-free allowlist.
    env = popen.call_args[1].get("env", {})
    assert set(env.keys()) == {
        "HOME", "PATH", "CODEX_HOME", "GIT_TERMINAL_PROMPT", "TMPDIR",
    }


@pytest.mark.parametrize("original_state", ["intact", "edited", "deleted", "unpinned"])
def test_reviewer_dispatch_pins_no_change_reasoning_and_round(
    tmp_path: Path, original_state: str,
) -> None:
    import pipeline
    import tick

    original_review = pipeline.render_review_comment(
        verdict="changes_requested", findings=[
            {"severity": "major", "file": "guard.py", "summary": "Guard missing"},
            {"severity": "major", "file": "retry.py", "summary": "Retry loses pending work"},
        ], base_sha="b" * 40, head_sha="abc123",
    )
    fix = pipeline.decide_fix_harvest(
        result_obj={"result": "Finding: guard\nDecision: no-change\n"
                    "Reason: The current caller already validates the input.\n"
                    "NO_CHANGES_RECHECK_NEEDED"},
        pre_label=pipeline.LABEL_CHANGES_REQUESTED,
        pinned_head="abc123", current_head="abc123", dispatch_id="fix-42-first",
        review_sha256=hashlib.sha256(original_review.encode()).hexdigest(),
    )
    assert fix["label"] == pipeline.LABEL_REVISED
    summary = fix["comment_body"]
    if original_state == "unpinned":
        summary = "\n".join(line for line in summary.splitlines()
                            if not line.startswith("<!-- pr-pipeline:fix-review "))
    unrelated_review = pipeline.render_review_comment(
        verdict="ready_for_human", findings=[], base_sha="c" * 40, head_sha="abc123",
    )
    comments = [] if original_state == "deleted" else [{
        "body": original_review + ("\nEdited" if original_state == "edited" else ""),
    }]
    # Neither a newer review during the fixer run nor one after publication
    # may replace the original finding set. Duplicate fixer publication is safe.
    comments.extend([{"body": unrelated_review}, {"body": summary},
                     {"body": summary}, {"body": unrelated_review}])
    expected_round, _ = pipeline.latest_fixer_context(comments)
    wt_root = tmp_path / "worktrees"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    proc = mock.MagicMock(pid=12345)

    def metadata(*args: str) -> object:
        if "comments" in args:
            return comments
        return {"title": "Guard fix", "body": "Closes #7"}

    def git_read(*args: str) -> str:
        if "merge-base" in args or ("rev-parse" in args and "--show-toplevel" not in args):
            return "b" * 40
        return reviewer_git(repo_root)(*args)

    with (
        mock.patch("tick.WT_ROOT", str(wt_root)),
        mock.patch("tick.git", side_effect=git_read),
        mock.patch("tick.gh_text", side_effect=["main", "abc123"]),
        mock.patch("tick.gh_json", side_effect=metadata),
        mock.patch("tick.pipeline.touches_review_policy", return_value=False),
        mock.patch("tick._publish_marker_or_revert", return_value=True),
        mock.patch("tick.relabel") as relabel,
        mock.patch("subprocess.Popen", return_value=proc) as popen,
    ):
        tick._dispatch_one_reviewer(pr_number=42, pre_label=pipeline.LABEL_REVISED)

    if original_state != "intact":
        popen.assert_not_called()
        relabel.assert_not_called()
        assert not (wt_root / "pr-42" / "dispatch.json").exists()
        return
    popen.assert_called_once()
    prompt = popen.call_args.args[0][-1]
    assert "The current caller already validates the input." in prompt
    assert "BEGIN FIXER SUMMARY (context only)" in prompt
    assert "independently" in prompt
    # The fixer omitted the second finding entirely. Fresh-context review
    # must receive the full originating review to check coverage explicitly.
    assert original_review in prompt
    assert unrelated_review not in prompt
    assert "every original finding" in prompt
    marker = json.loads((wt_root / "pr-42" / "dispatch.json").read_text())
    assert marker["fix_round"] == expected_round
    assert pipeline.valid_marker(marker)
    command = popen.call_args.args[0]
    assert command[command.index("--fix-round") + 1] == expected_round


@pytest.mark.parametrize("comments", [None, {}, "unreadable"])
def test_reviewer_retains_revised_pr_without_readable_fixer_context(
    tmp_path: Path, comments: object,
) -> None:
    import tick

    def metadata(*args: str) -> object:
        if "comments" in args:
            return comments
        return {"title": "Guard fix", "body": "Closes #7"}

    with (
        mock.patch("tick.WT_ROOT", str(tmp_path / "worktrees")),
        mock.patch("tick.git", side_effect=reviewer_git(tmp_path)),
        mock.patch("tick.gh_text", side_effect=["main", "abc123"]),
        mock.patch("tick.gh_json", side_effect=metadata),
        mock.patch("tick.pipeline.touches_review_policy", return_value=False),
        mock.patch("tick._publish_marker_or_revert", return_value=True),
        mock.patch("tick.relabel") as relabel,
        mock.patch("subprocess.Popen") as popen,
    ):
        tick._dispatch_one_reviewer(pr_number=42, pre_label="ai:revised")
    popen.assert_not_called()
    relabel.assert_not_called()


def test_reviewer_dispatch_retains_pr_on_failed_metadata_read(
    tmp_path: Path,
) -> None:
    """A failed PR title/body read retains the PR for retry: no launch, no
    relabel, no marker."""
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123"]  # base_ref, head
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.side_effect = subprocess.CalledProcessError(
                        1, ["gh"],
                    )
                    with mock.patch(
                        "tick.pipeline.atomic_write"
                    ) as atomic_write_mock:
                        with mock.patch("tick.relabel") as relabel_mock:
                            with mock.patch("subprocess.Popen") as popen:
                                _dispatch_one_reviewer(
                                    pr_number=42,
                                    pre_label="ai:implemented",
                                    tick_log=None,
                                )

    popen.assert_not_called()
    relabel_mock.assert_not_called()
    atomic_write_mock.assert_not_called()


def test_reviewer_dispatch_retains_pr_on_malformed_metadata_payload(
    tmp_path: Path,
) -> None:
    """A metadata payload without a string title retains the PR for retry."""
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123"]  # base_ref, head
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.return_value = {"body": "no title key"}
                    with mock.patch(
                        "tick.pipeline.atomic_write"
                    ) as atomic_write_mock:
                        with mock.patch("tick.relabel") as relabel_mock:
                            with mock.patch("subprocess.Popen") as popen:
                                _dispatch_one_reviewer(
                                    pr_number=42,
                                    pre_label="ai:implemented",
                                    tick_log=None,
                                )

    popen.assert_not_called()
    relabel_mock.assert_not_called()
    atomic_write_mock.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    [
        {"title": "valid title", "body": 7},
        {"title": "valid title", "body": None},
        {"title": "valid title"},
    ],
    ids=["int-body", "null-body", "missing-body"],
)
def test_reviewer_dispatch_retains_pr_on_malformed_body_payload(
    tmp_path: Path, metadata: dict[str, object],
) -> None:
    """A metadata payload without a string body retains the PR for retry.

    The fail-closed contract covers the whole title/body read: a non-string
    or missing body is a malformed payload, not an empty description, so it
    must not be coerced to "" and launch a context-less reviewer. (An empty
    string body remains valid — a PR may legitimately have no description.)
    Mocks mirror the successful-launch scaffolding so a missing guard would
    reach Popen and fail the assertion.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    proc_mock = mock.MagicMock(pid=12345)

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=reviewer_git(repo_root)):
            with mock.patch("tick.gh_text", side_effect=["main", "abc123"]), \
                    mock.patch("tick.gh_json", return_value=metadata):
                with mock.patch("tick.pipeline.atomic_write") as atomic_write_mock:
                    with mock.patch(
                        "tick._publish_marker_or_revert", return_value=True,
                    ):
                        with mock.patch("tick.relabel") as relabel_mock:
                            with mock.patch(
                                "subprocess.Popen", return_value=proc_mock,
                            ) as popen:
                                _dispatch_one_reviewer(
                                    pr_number=42,
                                    pre_label="ai:implemented",
                                    tick_log=None,
                                )

    popen.assert_not_called()
    relabel_mock.assert_not_called()
    atomic_write_mock.assert_not_called()


def test_reviewer_dispatch_accepts_empty_string_body(tmp_path: Path) -> None:
    """An empty-string body is a valid payload (PR with no description) —
    dispatch proceeds to launch rather than retaining the PR forever."""
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    proc_mock = mock.MagicMock(pid=12345)

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=reviewer_git(repo_root)):
            with mock.patch("tick.gh_text", side_effect=["main", "abc123"]), \
                    mock.patch(
                        "tick.gh_json",
                        return_value={"title": "valid title", "body": ""},
                    ):
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch(
                        "tick._publish_marker_or_revert", return_value=True,
                    ):
                        with mock.patch("tick.relabel"):
                            with mock.patch(
                                "subprocess.Popen", return_value=proc_mock,
                            ) as popen:
                                _dispatch_one_reviewer(
                                    pr_number=42,
                                    pre_label="ai:implemented",
                                    tick_log=None,
                                )

    popen.assert_called_once()


# --- B2: Reviewer pgid uses proc.pid, not os.getpgrp ---


def test_reviewer_dispatch_uses_start_new_session_and_proc_pid_as_pgid(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must use start_new_session=True and set pgid=proc.pid.

    Without start_new_session, the engine_wrapper shares the tick's process
    group. os.getpgrp() then records the tick's own pgid, and HARVEST's
    terminate_process_group would kill the tick itself when reconciling a
    hung reviewer (Invariant 4).
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch("tick._publish_marker_or_revert") as pmr:
                        pmr.return_value = True
                        with mock.patch("tick.relabel"):
                            with mock.patch("subprocess.Popen") as popen:
                                proc_mock = mock.MagicMock()
                                proc_mock.pid = 12345
                                popen.return_value = proc_mock

                                _dispatch_one_reviewer(
                                    pr_number=42,
                                    pre_label="ai:implemented",
                                    tick_log=None,
                                )

    # Must use start_new_session=True (matches implementer/fixer pattern)
    popen.assert_called_once()
    popen_kwargs = popen.call_args[1]
    assert popen_kwargs.get("start_new_session") is True, (
        "reviewer dispatch must use start_new_session=True so pgid=proc.pid, "
        "not os.getpgrp() which is the tick's own process group"
    )

    # pgid must be proc.pid, not os.getpgrp()
    published_marker = pmr.call_args[1]["published_marker"]
    assert published_marker["pgid"] == 12345, (
        f"pgid must be proc.pid (12345), got {published_marker['pgid']}"
    )


def test_reviewer_dispatch_retains_wrapper_stderr_and_closes_parent_handle(
    tmp_path: Path,
) -> None:
    """Reviewer wrapper stderr is durable without leaking the tick's handle."""
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    proc_mock = mock.MagicMock(pid=12345)

    def fake_git(*args: str) -> str:
        if "worktree" in args and "add" in args:
            (wt_root / "pr-42").mkdir(parents=True, exist_ok=True)
        return str(repo_root)

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fake_git):
            with mock.patch("tick.gh_text", side_effect=["main", "abc123"]), \
                    mock.patch("tick.gh_json", return_value=REVIEWER_PR_METADATA):
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch("tick._publish_marker_or_revert", return_value=True):
                        with mock.patch("tick.relabel"):
                            with mock.patch("subprocess.Popen", return_value=proc_mock) as popen:
                                _dispatch_one_reviewer(
                                    pr_number=42,
                                    pre_label="ai:implemented",
                                    tick_log=None,
                                )

    stderr_handle = popen.call_args.kwargs["stderr"]
    assert Path(stderr_handle.name) == wt_root / "pr-42" / "stderr.log"
    assert stderr_handle.closed is True


def test_reviewer_dispatch_closes_stderr_handle_when_popen_fails(
    tmp_path: Path,
) -> None:
    """A reviewer wrapper spawn failure does not leak its stderr handle."""
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=reviewer_git(repo_root)):
            with mock.patch("tick.gh_text", side_effect=["main", "abc123"]), \
                    mock.patch("tick.gh_json", return_value=REVIEWER_PR_METADATA):
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch(
                        "subprocess.Popen",
                        side_effect=PermissionError("spawn denied"),
                    ) as popen:
                        _dispatch_one_reviewer(
                            pr_number=42,
                            pre_label="ai:implemented",
                            tick_log=None,
                        )

    stderr_handle = popen.call_args.kwargs["stderr"]
    assert Path(stderr_handle.name) == wt_root / "pr-42" / "stderr.log"
    assert stderr_handle.closed is True


# --- Blocking fix: touches_review_policy gate ---


def test_dispatch_one_reviewer_escalates_when_pr_touches_review_policy(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must escalate when the PR touches reviewer policy files.

    A PR that edits CLAUDE.md, AGENTS.md, or the review checklist changes the
    instructions the reviewer engine itself follows — "untrusted input controls
    the validator." These PRs must be escalated to a human reviewer rather than
    reviewed automatically under instructions the PR itself could supply.

    This is the one escalation whose reason the PR *author* needs, not just the
    operator: nothing about the PR is wrong, it simply cannot be auto-reviewed.
    So it publishes before the label moves, like every other pre-launch
    escalation, and says which of the two it is.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    calls: list[str] = []
    bodies: list[str] = []

    def record_gh(*args: str) -> object:
        if args[1] == "comment":
            calls.append("comment")
            with open(args[4]) as fh:
                bodies.append(fh.read())
        elif args[1] == "edit":
            calls.append(f"edit:{args[0]}:{args[2]}:{args[6]}->{args[4]}")
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=0, stdout="",
        )

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = [
                    "main",     # base_ref
                    "abc123",   # head_sha
                ]
                with mock.patch(
                    "tick.pipeline.touches_review_policy"
                ) as trp:
                    trp.return_value = True
                    with mock.patch(
                        "tick.pipeline.atomic_write"
                    ):
                        with mock.patch("tick.gh", side_effect=record_gh):
                            with mock.patch(
                                "subprocess.Popen"
                            ) as popen:
                                with mock.patch(
                                    "tick.cleanup_scratch"
                                ) as cleanup:
                                    _dispatch_one_reviewer(
                                        pr_number=42,
                                        pre_label="ai:implemented",
                                        tick_log=None,
                                    )

    # Must have checked the policy
    trp.assert_called_once()
    # Must explain itself, then escalate to human.
    assert calls == [
        "comment", "edit:pr:42:ai:implemented->ai:needs-human-decision",
    ]
    assert "review-policy-touched" in bodies[0]
    assert "reviewer's own instructions" in bodies[0]
    # Must NOT launch an engine
    popen.assert_not_called()
    # Must clean up worktree
    cleanup.assert_called_once()


def test_dispatch_one_reviewer_proceeds_when_policy_not_touched(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must proceed normally when policy is not touched."""
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = [
                    "main",     # base_ref
                    "abc123",   # head_sha
                ]
                with mock.patch("tick.pipeline.touches_review_policy") as trp:
                    trp.return_value = False
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_reviewer(
                                        pr_number=42,
                                        pre_label="ai:implemented",
                                        tick_log=None,
                                    )

    # Must have checked the policy
    trp.assert_called_once()
    # Must proceed to launch — Popen called
    popen.assert_called_once()


# --- Major fix: unwrapped escalation relabel in fixer dispatch ---


def test_dispatch_one_fixer_handles_escalation_relabel_failure(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer must not crash when escalation relabel fails.

    The escalation path (no pipeline review comment found → relabel to
    ai:needs-human-decision) calls relabel without try/except. If the gh
    call fails, the exception propagates through dispatch_fixers (which has
    no per-PR try/catch) and crashes the entire fixer dispatch loop.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-42"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = [
                    "main",       # base_ref
                    "abc123",     # head_sha
                    "fix-branch", # head_ref (new)
                ]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    # No pipeline review comment → escalation path
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[{"body": "just a human note"}],
                    )
                    with mock.patch("tick.pipeline.atomic_write") as atomic_write:
                        atomic_write.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch(
                            "tick.gh", side_effect=allow_escalation_comment(),
                        ), mock.patch(
                            "tick.relabel"
                        ) as relabel_mock:
                            relabel_mock.side_effect = (
                                subprocess.CalledProcessError(1, ["gh"])
                            )
                            with mock.patch(
                                "tick.cleanup_scratch"
                            ) as cleanup:
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    # Must not raise — handles failure gracefully
                                    _dispatch_one_fixer(
                                        pr_number=42,
                                        tick_log=None,
                                    )

    # Must have attempted escalation
    relabel_mock.assert_called_once()
    # Must NOT launch an engine — escalation path, no review findings
    popen.assert_not_called()
    # Failed escalation retains the lease so a later tick can retry custody.
    cleanup.assert_not_called()
    assert wt_dir.is_dir()


# --- Major fix: tick_log guard on empty review body ---


def test_dispatch_one_fixer_escalates_without_review_comment_when_log_is_none(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer must escalate when no review comment, even if tick_log is None.

    The guard was ``if not review_body and tick_log`` — gated on tick_log being
    non-None. When TickLog.open fails in main(), tick_log is None, and the guard
    evaluates False, letting the fixer launch with an empty prompt.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    wt_dir = wt_root / "pr-99"
    wt_dir.mkdir()
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "claude",
        "stage": "fix",
        "pr_number": 99,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-99",
    }))
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = [
                    "main",       # base_ref
                    "abc123",     # head_sha
                    "fix-branch", # head_ref (new)
                ]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    # No pipeline review comment
                    gh_json_mock.side_effect = fixer_gh_json(comments=[])
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch(
                            "tick.gh", side_effect=allow_escalation_comment(),
                        ), mock.patch("tick.relabel") as relabel_mock:
                            with mock.patch(
                                "subprocess.Popen"
                            ) as popen:
                                # tick_log=None — the old guard would skip
                                # escalation and launch with empty prompt
                                _dispatch_one_fixer(
                                    pr_number=99,
                                    tick_log=None,
                                )

    # Must escalate to human, regardless of whether tick_log is available
    relabel_mock.assert_called_once_with(
        "pr", 99, "ai:changes-requested", "ai:needs-human-decision",
    )
    # Must NOT launch engine with empty prompt
    popen.assert_not_called()


# --- B1: _terminate_engine kill failure must not delete marker ---


def test_terminate_engine_returns_true_on_success() -> None:
    """_terminate_engine confirms death through the production group helper."""
    from tick import _terminate_engine  # type: ignore[import-untyped]

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345
    with mock.patch(
        "tick.process_control.terminate_process_group", return_value=True,
    ) as terminate_group:
        result = _terminate_engine(proc_mock, 42, "review", "codex")

    assert result is True
    terminate_group.assert_called_once_with(
        12345,
        grace_seconds=5,
        kill_grace_seconds=2,
        poll_interval=0.5,
    )
    proc_mock.terminate.assert_not_called()
    proc_mock.kill.assert_not_called()


def test_terminate_engine_returns_false_when_kill_fails() -> None:
    """_terminate_engine retains custody while any group member survives."""
    from tick import _terminate_engine  # type: ignore[import-untyped]

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345
    with mock.patch(
        "tick.process_control.terminate_process_group", return_value=False,
    ) as terminate_group:
        result = _terminate_engine(proc_mock, 42, "review", "codex")

    assert result is False
    terminate_group.assert_called_once_with(
        12345,
        grace_seconds=5,
        kill_grace_seconds=2,
        poll_interval=0.5,
    )
    proc_mock.terminate.assert_not_called()
    proc_mock.kill.assert_not_called()


def test_publish_marker_failure_cleans_only_after_group_death(
    tmp_path: Path,
) -> None:
    """Marker failure archives scratch only after the whole group is dead."""
    from tick import _publish_marker_or_revert  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "recovery.txt").write_text("marker failure evidence")
    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345
    marker: dict[str, object] = {
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc",
        "head_sha": "def", "pre_label": "ai:implemented",
        "dispatch_id": "review-42-marker-failure", "pid": 12345,
        "start_time": 1000.0, "pgid": 12345,
    }

    with mock.patch("tick.pipeline.atomic_write", side_effect=OSError("disk full")):
        with mock.patch(
            "tick.process_control.terminate_process_group", return_value=True,
        ) as terminate_group:
            with mock.patch("tick._relabel_with_context") as relabel_mock:
                result = _publish_marker_or_revert(
                    proc=proc_mock,
                    published_marker=marker,
                    wt_dir=str(wt_dir),
                    entity="pr",
                    pr_number=42,
                    in_flight_label="ai:reviewing",
                    pre_label="ai:implemented",
                    stage="review",
                )

    assert result is False
    terminate_group.assert_called_once_with(
        12345,
        grace_seconds=5,
        kill_grace_seconds=2,
        poll_interval=0.5,
    )
    relabel_mock.assert_called_once()
    assert not wt_dir.exists()
    archives = list(
        (wt_root / ".harvest-evidence").glob("pr-42-review-42-marker-failure*"),
    )
    assert len(archives) == 1
    assert (
        archives[0] / "scratch" / "recovery.txt"
    ).read_text() == "marker failure evidence"


def test_publish_marker_failure_retains_custody_while_group_survives(
    tmp_path: Path,
) -> None:
    """An unconfirmed child keeps scratch and a fallback custody marker."""
    from tick import _publish_marker_or_revert  # type: ignore[import-untyped]

    wt_dir = tmp_path / "worktrees" / "issue-7"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text('{"state": "launching"}')
    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345
    marker: dict[str, object] = {
        "engine": "claude", "stage": "implement", "pr_number": 7,
        "deadline": 9999999999.0, "base_ref": "main", "base_sha": "abc",
        "head_sha": "abc", "pre_label": "ai:available",
        "dispatch_id": "impl-7-marker-failure", "pid": 12345,
        "start_time": 1000.0, "pgid": 12345,
    }
    writes = 0

    def fail_publish_then_write_fallback(path: str, data: str) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("disk full")
        Path(path).write_text(data)

    with mock.patch(
        "tick.pipeline.atomic_write", side_effect=fail_publish_then_write_fallback,
    ):
        with mock.patch(
            "tick.process_control.terminate_process_group", return_value=False,
        ) as terminate_group:
            with mock.patch("tick._relabel_with_context") as relabel_mock:
                result = _publish_marker_or_revert(
                    proc=proc_mock,
                    published_marker=marker,
                    wt_dir=str(wt_dir),
                    entity="issue",
                    pr_number=7,
                    in_flight_label="ai:implementing",
                    pre_label="ai:available",
                    stage="implement",
                )

    assert result is False
    terminate_group.assert_called_once()
    relabel_mock.assert_not_called()
    assert wt_dir.is_dir()
    fallback = json.loads((wt_dir / "dispatch.json").read_text())
    assert fallback["state"] == "launching"
    assert "pid" not in fallback
    assert not (wt_dir.parent / ".harvest-evidence").exists()


def test_dispatch_one_reviewer_skips_cleanup_when_kill_fails(
    tmp_path: Path,
) -> None:
    """_dispatch_one_reviewer must NOT delete marker when _terminate_engine
    returns False (kill failed). Deleting dispatch.json orphanes the engine
    — the next tick re-dispatches a duplicate. The marker must survive so
    HARVEST can re-attempt the kill on proven identity.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch("tick._publish_marker_or_revert") as pmr:
                        pmr.return_value = True
                        with mock.patch("tick.relabel") as relabel_mock:
                            relabel_mock.side_effect = (
                                subprocess.CalledProcessError(1, ["gh"])
                            )
                            with mock.patch(
                                "tick._terminate_engine"
                            ) as term_mock:
                                term_mock.return_value = False
                                with mock.patch(
                                    "tick.cleanup_scratch"
                                ) as cleanup:
                                    with mock.patch(
                                        "subprocess.Popen"
                                    ) as popen:
                                        popen.return_value = proc_mock
                                        _dispatch_one_reviewer(
                                            pr_number=42,
                                            pre_label="ai:implemented",
                                            tick_log=None,
                                        )

    # Must have attempted to terminate
    term_mock.assert_called_once()
    # Must NOT clean up — marker survives for HARVEST to re-kill
    cleanup.assert_not_called()


def test_dispatch_one_reviewer_escalates_to_needs_human_when_kill_fails(
    tmp_path: Path,
) -> None:
    """When the label flip fails AND _terminate_engine cannot kill the
    reviewer, _dispatch_one_reviewer must escalate the PR to
    ai:needs-human-decision — the same terminal fallback used by
    implementer and fixer. Without this, the pre-dispatch label stays
    visible, the next tick re-dispatches a duplicate reviewer, and the
    two engines contend over the same marker.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    relabel_calls: list[tuple[str, ...]] = []

    def relabel_side_effect(*args: str) -> None:
        relabel_calls.append(args)
        if len(relabel_calls) == 1:
            raise subprocess.CalledProcessError(1, ["gh"])

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch("tick._publish_marker_or_revert") as pmr:
                        pmr.return_value = True
                        with mock.patch("tick.relabel") as relabel_mock:
                            relabel_mock.side_effect = relabel_side_effect
                            with mock.patch(
                                "tick._terminate_engine"
                            ) as term_mock:
                                term_mock.return_value = False
                                with mock.patch(
                                    "tick.cleanup_scratch"
                                ) as cleanup:
                                    with mock.patch(
                                        "subprocess.Popen"
                                    ) as popen:
                                        popen.return_value = proc_mock
                                        _dispatch_one_reviewer(
                                            pr_number=42,
                                            pre_label="ai:implemented",
                                            tick_log=None,
                                        )

    term_mock.assert_called_once()
    cleanup.assert_not_called()
    assert len(relabel_calls) == 2, (
        f"Expected 2 relabel calls (initial + escalation), got {len(relabel_calls)}"
    )
    assert relabel_calls[1] == (
        "pr", 42, "ai:implemented", "ai:needs-human-decision",
    )


def test_dispatch_one_implementer_skips_cleanup_when_kill_fails(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer must NOT delete marker when kill fails."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("tick._publish_marker_or_revert") as pmr:
                    pmr.return_value = True
                    with mock.patch("tick.relabel") as relabel_mock:
                        relabel_mock.side_effect = (
                            subprocess.CalledProcessError(1, ["gh"])
                        )
                        with mock.patch(
                            "tick._terminate_engine"
                        ) as term_mock:
                            term_mock.return_value = False
                            with mock.patch(
                                "tick.cleanup_scratch"
                            ) as cleanup:
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_implementer(
                                        issue_number=7,
                                        issue_title="Test",
                                        tick_log=None,
                                    )

    term_mock.assert_called_once()
    cleanup.assert_not_called()


def test_dispatch_one_fixer_skips_cleanup_when_kill_fails(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer must NOT delete marker when kill fails."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[
                            {"body": "<!-- pr-pipeline:review -->\nfindings"},
                        ],
                    )
                    with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                        aw_mock.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch("tick._publish_marker_or_revert") as pmr:
                            pmr.return_value = True
                            with mock.patch("tick.relabel") as relabel_mock:
                                relabel_mock.side_effect = (
                                    subprocess.CalledProcessError(1, ["gh"])
                                )
                                with mock.patch(
                                    "tick._terminate_engine"
                                ) as term_mock:
                                    term_mock.return_value = False
                                    with mock.patch(
                                        "tick.cleanup_scratch"
                                    ) as cleanup:
                                        with mock.patch(
                                            "subprocess.Popen"
                                        ) as popen:
                                            popen.return_value = proc_mock
                                            _dispatch_one_fixer(
                                                pr_number=42,
                                                tick_log=None,
                                            )

    term_mock.assert_called_once()
    cleanup.assert_not_called()


# --- M1: Implementer/fixer dispatch preserves parent env ---


def test_dispatch_one_implementer_passes_parent_env_to_popen(
    tmp_path: Path,
) -> None:
    """_dispatch_one_implementer passes the operator's full env to Popen.

    Implementers are trusted local engines. Preserving the full environment
    allows the same dispatch path to use cmux, keychain, API-key, and other
    terminal-specific authentication without a hardcoded variable list.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("tick._publish_marker_or_revert") as pmr:
                    pmr.return_value = True
                    with mock.patch("tick.relabel"):
                        with mock.patch("subprocess.Popen") as popen:
                            proc_mock = mock.MagicMock()
                            proc_mock.pid = 12345
                            popen.return_value = proc_mock
                            parent_env = {
                                "CMUX_SOCKET_PATH": "/tmp/cmux.sock",
                                "CLAUDE_CODE_SESSION_ID": "sess-abc",
                                "HOME": "/u",
                                "PATH": "/usr/bin",
                            }
                            with mock.patch.dict(
                                "os.environ", parent_env, clear=True,
                            ):
                                _dispatch_one_implementer(
                                    issue_number=7,
                                    issue_title="Test",
                                    tick_log=None,
                                )

    popen.assert_called_once()
    popen_kwargs = popen.call_args[1]
    env = popen_kwargs.get("env")
    assert env == parent_env


def test_dispatch_one_fixer_passes_parent_env_to_popen(
    tmp_path: Path,
) -> None:
    """_dispatch_one_fixer passes the operator's full env to Popen."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[
                            {"body": "<!-- pr-pipeline:review -->\nfindings"},
                        ],
                    )
                    with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                        aw_mock.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch("tick._publish_marker_or_revert") as pmr:
                            pmr.return_value = True
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    proc_mock = mock.MagicMock()
                                    proc_mock.pid = 12345
                                    popen.return_value = proc_mock
                                    parent_env = {
                                        "CMUX_SOCKET_PATH": "/tmp/cmux.sock",
                                        "CLAUDECODE": "1",
                                        "HOME": "/u",
                                        "PATH": "/usr/bin",
                                    }
                                    with mock.patch.dict(
                                        "os.environ", parent_env, clear=True,
                                    ):
                                        _dispatch_one_fixer(
                                            pr_number=42,
                                            tick_log=None,
                                        )

    popen.assert_called_once()
    popen_kwargs = popen.call_args[1]
    env = popen_kwargs.get("env")
    assert env == parent_env


# --- Review checklists in the policy-touch gate ---


def test_touches_review_policy_catches_every_configured_checklist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Every operator-declared checklist gates, not just the first one.

    A repo commonly splits its review criteria across several checklists
    (backend, frontend, security). Each governs reviewer criteria, so editing
    any of them mid-review is the same untrusted-input-controls-the-validator
    inversion — a gate that reads only one entry would auto-review a PR that
    rewrote the others.
    """
    from pipeline import touches_review_policy

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults]\nchecklist_paths = ["
        '".ci/review-checklist.md", ".ci/review-checklist-frontend.md"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    assert touches_review_policy([".ci/review-checklist.md"]) is True
    assert touches_review_policy([".ci/review-checklist-frontend.md"]) is True

    # CLAUDE.md/AGENTS.md at any level need no configuration.
    assert touches_review_policy(["CLAUDE.md"]) is True
    assert touches_review_policy(["src/AGENTS.md"]) is True

    # Non-policy files
    assert touches_review_policy(["src/main.py"]) is False
    assert touches_review_policy(["README.md"]) is False


def test_touches_review_policy_guards_the_gate_at_any_install_depth() -> None:
    """A PR editing the gate's own files must escalate wherever the skill lives.

    The gate protects itself: a PR that flips ``allow_touching`` or rewrites
    ``_policy_touching_allowed`` would otherwise disable the escalation for
    every future policy-editing PR, so those paths escalate to a human. That
    self-protection was pinned to ``.claude/skills/...`` — the depth of an
    in-repo install — which silently exempts exactly the same edit in the
    pipeline's own repository, where the skills live at ``skills/``, and in any
    plugin layout. Matching the path suffix keeps one rule for every depth.
    """
    from pipeline import touches_review_policy  # type: ignore[import-untyped]

    for prefix in ("", ".claude/", "vendor/pr-pipeline/"):
        for tail in (
            "skills/tick/config.toml",
            "skills/tick/lib/pipeline.py",
            "skills/tick/lib/tick.py",
        ):
            assert touches_review_policy([prefix + tail]) is True, (
                f"gate self-protection missed {prefix + tail}"
            )

    # The suffix must stay a path-segment match, not a substring one.
    assert touches_review_policy(["docs/skills/tick/config.toml"]) is True
    assert touches_review_policy(["other-skills/tick/config.toml"]) is False
    assert touches_review_policy(["skills/tick/lib/README.md"]) is False


def test_touches_review_policy_guards_the_installed_engine_instructions() -> None:
    """The policy the install *supplies* is review policy for future runs.

    A bound skill doc and a prompt template are read from the install, so a PR
    that rewrites one can no longer steer its own fixer. What it can still do
    is change the instructions every later dispatch runs under, which is the
    same silent-disable vector the gate already guards ``pipeline.py`` and
    ``tick.py`` against — a merged rewrite governs every future PR, and the PR
    that merged it was auto-reviewed. So both sets are gated, and both are
    derived from the runtime's own constants rather than restated here: a skill
    bound by a future stage, or a prompt added for one, is covered by being
    added to the runtime, not by being remembered in this list.
    """
    from pipeline import (  # type: ignore[import-untyped]
        BOUND_SKILLS,
        PROMPTS_DIR,
        touches_review_policy,
    )

    tails = [f"skills/{name}/SKILL.md" for name in BOUND_SKILLS]
    tails += [
        f"skills/tick/lib/prompts/{path.name}"
        for path in sorted(PROMPTS_DIR.iterdir())
    ]
    assert len(tails) > len(BOUND_SKILLS), "no prompt template was discovered"

    for prefix in ("", ".claude/", "vendor/pr-pipeline/"):
        for tail in tails:
            assert touches_review_policy([prefix + tail]) is True, (
                f"the gate does not protect installed engine instructions: "
                f"{prefix + tail}"
            )

    # Segment matching, here too: a same-named directory elsewhere is not the
    # install's, and a file the install does not read is not policy.
    assert touches_review_policy(["docs/prompts/fixer.txt"]) is False
    assert touches_review_policy(["skills/tick/lib/prompts/README.md"]) is True
    assert touches_review_policy(["skills/review-response-hygiene/notes.md"]) is False


# --- Adversarial review: reviewer codex exec argv ---


def test_reviewer_dispatch_uses_bare_exec_with_schema_prompt(
    tmp_path: Path,
) -> None:
    """Reviewer dispatch must use bare ``codex exec``, not ``exec review``.

    Verified on codex-cli 0.142.5: the ``exec review`` subcommand renders
    prose into every output channel (``agent_message``, ``-o``, stdout) and
    ignores ``--output-schema``, so its result can NEVER validate against
    ``review-schema.json`` — the harvest gate would reject every review.
    It also rejects ``--base`` combined with a ``[PROMPT]`` positional at
    arg parsing. Bare ``codex exec`` honors ``--output-schema`` (the
    preflight proves this mechanism), takes exactly one prompt positional
    carrying the base SHA, and has no ``--base`` flag at all.
    Trusted-author/repo enforcement lives in
    ``pipeline.trusted_review_candidate`` at dispatch, not in the prompt.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.touches_review_policy") as trp:
                    trp.return_value = False
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_reviewer(
                                        pr_number=42,
                                        pre_label="ai:implemented",
                                        tick_log=None,
                                    )

    popen.assert_called_once()
    engine_cmd = popen.call_args[0][0]

    # The engine_wrapper passes everything after "--" to codex.
    # Find the codex command portion (after the "--" separator).
    sep_idx = engine_cmd.index("--")
    codex_argv = engine_cmd[sep_idx + 1:]

    # Must use bare "codex exec" — the review subcommand's output can never
    # validate against review-schema.json (prose-only on 0.142.5).
    assert codex_argv[0] == "codex", f"expected 'codex', got {codex_argv[0]}"
    assert codex_argv[1] == "exec", f"expected 'exec', got {codex_argv[1]}"
    assert codex_argv[2] != "review", (
        "must NOT use the 'review' subcommand: it ignores --output-schema "
        "and emits prose, so the harvest gate rejects every result"
    )

    # --base is a review-subcommand flag; bare exec rejects it. The base
    # SHA travels in the prompt instead.
    assert "--base" not in codex_argv, (
        f"bare codex exec has no --base flag, got: {codex_argv}"
    )

    # The structured-verdict mechanism: --output-schema must point at the
    # review schema the harvest validates against.
    assert "--output-schema" in codex_argv, (
        f"must pass --output-schema for a machine-readable verdict, "
        f"got: {codex_argv}"
    )
    schema_path = codex_argv[codex_argv.index("--output-schema") + 1]
    assert schema_path.endswith("review-schema.json")

    # Must not have non-existent flags
    assert "--trusted-author" not in codex_argv, (
        "must not use non-existent --trusted-author flag"
    )
    assert "--trusted-repo" not in codex_argv, (
        "must not use non-existent --trusted-repo flag"
    )

    # Exactly ONE positional: the review prompt, which must carry the base
    # SHA (the mocked git() returns repo_root for the rev-parse call).
    prompt_candidates = codex_positionals(codex_argv)
    assert len(prompt_candidates) == 1, (
        f"bare codex exec takes exactly one [PROMPT] positional, "
        f"got {len(prompt_candidates)}: {prompt_candidates}"
    )
    prompt = prompt_candidates[0]
    assert str(repo_root) in prompt, (
        f"review prompt must pin the base SHA for the diff, got: {prompt!r}"
    )
    assert "schema" in prompt.lower(), (
        f"review prompt must demand a schema-conforming verdict, "
        f"got: {prompt!r}"
    )


def test_reviewer_dispatch_creates_git_worktree_at_pr_head(
    tmp_path: Path,
) -> None:
    """Reviewer dispatch must create a real git worktree, not a plain directory.

    A plain ``os.makedirs`` directory sits inside the parent repo; git resolves
    to the parent checkout instead of the PR head. The reviewer must create an
    isolated worktree at the PR's ``head_sha`` via ``git worktree add --detach``.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    git_calls: list[tuple[str, ...]] = []

    def git_side_effect(*args: str) -> str:
        git_calls.append(args)
        return str(repo_root)

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.touches_review_policy") as trp:
                    trp.return_value = False
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_reviewer(
                                        pr_number=42,
                                        pre_label="ai:implemented",
                                        tick_log=None,
                                    )

    # Must have a "worktree add" call with "--detach" and the head_sha
    worktree_add_calls = [
        c for c in git_calls
        if "worktree" in c and "add" in c
    ]
    assert len(worktree_add_calls) >= 1, (
        f"must call 'git worktree add' for reviewer isolation, "
        f"git calls were: {git_calls}"
    )
    wt_call = worktree_add_calls[0]
    assert "--detach" in wt_call, (
        f"reviewer worktree must be detached, got: {wt_call}"
    )
    assert "abc123" in wt_call, (
        f"reviewer worktree must be at head_sha, got: {wt_call}"
    )


def test_reviewer_dispatch_fetches_pr_head_before_policy_gate(
    tmp_path: Path,
) -> None:
    """Reviewer must fetch the PR head so head_sha is in local objects.

    Without fetching, ``git diff {base_sha}..{head_sha}`` in the policy gate
    fails when the head SHA is not present locally. The fetch must happen
    before the policy gate diff, not after.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    git_calls: list[tuple[str, ...]] = []

    def git_side_effect(*args: str) -> str:
        git_calls.append(args)
        # Return non-policy file list for the diff call
        if "diff" in args and "--name-only" in args:
            return "src/main.py\n"
        return str(repo_root)

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.atomic_write"):
                    with mock.patch(
                        "tick._publish_marker_or_revert",
                        return_value=True,
                    ):
                        with mock.patch("tick.relabel"):
                            with mock.patch(
                                "subprocess.Popen"
                            ) as popen:
                                popen.return_value = proc_mock
                                _dispatch_one_reviewer(
                                    pr_number=42,
                                    pre_label="ai:implemented",
                                    tick_log=None,
                                )

    # Find the fetch call that brings the PR head into local objects
    fetch_calls = [c for c in git_calls if "fetch" in c]
    pr_head_fetches = [
        c for c in fetch_calls
        if any("pull/42" in arg or "abc123" in arg for arg in c)
    ]
    assert len(pr_head_fetches) >= 1, (
        f"must fetch PR head before policy gate, fetch calls: {fetch_calls}"
    )

    # The PR head fetch must come before the diff call
    diff_calls = [c for c in git_calls if "diff" in c]
    if diff_calls:
        first_pr_fetch_idx = git_calls.index(pr_head_fetches[0])
        first_diff_idx = git_calls.index(diff_calls[0])
        assert first_pr_fetch_idx < first_diff_idx, (
            "PR head fetch must happen before policy gate diff"
        )


# --- Adversarial review: fixer push target ---


def test_fixer_dispatch_uses_pr_branch_for_worktree(
    tmp_path: Path,
) -> None:
    """Fixer worktree must use the PR branch name so pushes target the right ref.

    Creating a local branch ``fix-{pr}-{ts}`` means ``git push origin HEAD``
    pushes to ``origin/fix-{pr}-{ts}`` instead of the PR's actual branch.
    The worktree must check out the PR's ``head_ref`` branch with tracking.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    git_calls: list[tuple[str, ...]] = []

    materialize = fixer_git(repo_root)

    def git_side_effect(*args: str) -> str:
        git_calls.append(args)
        return materialize(*args)

    review_body = "<!-- pr-pipeline:review -->\nfindings here"

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = git_side_effect
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = [
                    "main",       # base_ref
                    "def456",     # head_sha
                    "feature-x",  # head_ref
                ]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[{"body": review_body}],
                    )
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_fixer(
                                        pr_number=42,
                                        tick_log=None,
                                    )

    # The git worktree or checkout must use the PR branch name "feature-x",
    # NOT a generated name like "fix-42-<timestamp>"
    worktree_and_checkout_calls = [
        c for c in git_calls
        if ("worktree" in c and "add" in c)
        or ("checkout" in c)
    ]
    # At least one call must reference the PR branch name
    branch_referenced = any(
        "feature-x" in str(c) for c in worktree_and_checkout_calls
    )
    assert branch_referenced, (
        f"fixer must use PR branch name 'feature-x' for worktree/checkout, "
        f"relevant calls: {worktree_and_checkout_calls}"
    )

    # Must NOT use a generated "fix-42-" branch name in the worktree
    generated_branch_used = any(
        any("fix-42-" in arg for arg in c) for c in worktree_and_checkout_calls
    )
    assert not generated_branch_used, (
        "fixer must not use generated 'fix-42-*' branch name"
    )


# --- Adversarial review: unattended engine command contract ---


@pytest.mark.parametrize("override", [None, "dontAsk"])
def test_implementer_dispatch_uses_bounded_unattended_flags(
    tmp_path: Path, override: str | None,
) -> None:
    """Implementer engine runs unattended with a bounded tool allowlist.

    Default ``auto`` and explicit ``dontAsk`` retain the narrow pre-approved
    ``--allowedTools`` grants without bypassing permissions. ``claude`` has no
    ``-o`` flag: the prompt is fed via stdin (``prompt.txt``) and the result
    JSON is captured by redirecting stdout to ``result.json``.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel"):
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=None,
                            **({"permission_mode": override} if override else {}),
                        )

    popen_mock.assert_called_once()
    engine_cmd = popen_mock.call_args[0][0]
    assert "--dangerously-skip-permissions" not in engine_cmd, (
        f"implementer must use the bounded allowlist, not permission bypass, "
        f"got: {engine_cmd}"
    )
    assert "-o" not in engine_cmd, (
        f"claude has no -o flag; result capture is stdout redirection, "
        f"got: {engine_cmd}"
    )
    assert engine_cmd[engine_cmd.index("--permission-mode") + 1] == (override or "auto")
    allowed = engine_cmd[engine_cmd.index("--allowedTools") + 1]
    # The implementer's push permission is EXACT: its worktree checks out its
    # own branch, so `git push origin HEAD` is the pre-approved remote-mutation
    # surface (auto can approve other commands). No wildcard — `HEAD*` would admit arbitrary refspec destinations
    # and command suffixes (e.g. force options).
    assert "Bash(git push origin HEAD)" in allowed
    assert "HEAD*" not in allowed
    assert "HEAD:" not in allowed
    assert engine_cmd[engine_cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in engine_cmd
    # No --model: an unset override leaves model selection to the operator's
    # own claude config rather than pinning one here.
    assert "--model" not in engine_cmd

    # Prompt travels via stdin, not argv (argv is ps-visible and size-capped).
    assert not any("PWA icons" in str(arg) for arg in engine_cmd)
    kwargs = popen_mock.call_args[1]
    assert getattr(kwargs["stdin"], "name", "").endswith("prompt.txt")
    assert getattr(kwargs["stdout"], "name", "").endswith("result.json")
    prompt_text = (wt_root / "issue-461" / "prompt.txt").read_text()
    assert "PWA icons" in prompt_text


def test_implementer_dispatch_honors_permission_mode_auto(
    tmp_path: Path,
) -> None:
    """The implementer engine argv carries the requested auto permission mode.

    The option must reach the exact ``--permission-mode`` value in the
    production argv: a threading bug that silently dropped it would keep
    launching under ``dontAsk`` and the classifier would never engage.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel"):
                        _dispatch_one_implementer(
                            issue_number=462,
                            issue_title="Auto mode dispatch",
                            tick_log=None,
                            permission_mode="auto",
                        )

    popen_mock.assert_called_once()
    engine_cmd = popen_mock.call_args[0][0]
    assert engine_cmd[engine_cmd.index("--permission-mode") + 1] == "auto", (
        f"implementer must launch with --permission-mode auto when requested, "
        f"got: {engine_cmd}"
    )
    assert "--allowedTools" in engine_cmd, (
        f"auto mode must keep the bounded allowlist, got: {engine_cmd}"
    )


def test_implementer_dispatch_launches_with_configured_engine_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The implementer reads ``engine_settings`` from the target's config.

    Setup's probe launches with the value it records; dispatch must launch
    with the same one, or the probe measured a different engine.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    configure_engine_settings(monkeypatch, tmp_path, ENGINE_SETTINGS)
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)), \
            mock.patch("tick.git", return_value=str(repo_root)), \
            mock.patch("tick.relabel"), \
            mock.patch("tick.pipeline.atomic_write", side_effect=write_through), \
            mock.patch("subprocess.Popen") as popen_mock:
        popen_mock.return_value = mock.MagicMock(pid=12345)
        _dispatch_one_implementer(
            issue_number=463, issue_title="Settings", tick_log=None,
        )

    popen_mock.assert_called_once()
    engine_cmd = popen_mock.call_args[0][0]
    assert engine_cmd[engine_cmd.index("--settings") + 1] == ENGINE_SETTINGS


def test_fixer_dispatch_launches_with_configured_engine_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixer reads ``engine_settings`` exactly as the implementer does."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    configure_engine_settings(monkeypatch, tmp_path, ENGINE_SETTINGS)
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)), \
            mock.patch("tick.git", side_effect=fixer_git(repo_root)), \
            mock.patch("tick.gh_text", side_effect=["main", "abc123", "fix-branch"]), \
            mock.patch("tick.gh_json", side_effect=fixer_gh_json(
                pr_body=FIXER_PROMPT_PR_BODY, issues=FIXER_PROMPT_ISSUES,
            )), \
            mock.patch("tick.pipeline.atomic_write", side_effect=write_through), \
            mock.patch("tick._publish_marker_or_revert", return_value=True), \
            mock.patch("tick.relabel"), \
            mock.patch("subprocess.Popen") as popen_mock:
        popen_mock.return_value = mock.MagicMock(pid=12345)
        _dispatch_one_fixer(pr_number=43, tick_log=None)

    popen_mock.assert_called_once()
    engine_cmd = popen_mock.call_args[0][0]
    assert engine_cmd[engine_cmd.index("--settings") + 1] == ENGINE_SETTINGS


def test_implementer_dispatch_refuses_a_broken_engine_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings file that broke after setup escalates before any custody.

    ``claude --settings`` would not reject it, so launching would run the
    engine under the settings the operator configured this key to replace.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    settings_file = tmp_path / "engine-settings.json"
    settings_file.write_text('{"sandbox": ')
    configure_engine_settings(monkeypatch, tmp_path, str(settings_file))
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)), \
            mock.patch("tick.git", return_value=str(repo_root)), \
            mock.patch("tick.relabel") as relabel_mock, \
            mock.patch("tick.pipeline.atomic_write") as write_mock, \
            mock.patch("tick._escalate_dispatch_setup_failure") as escalate_mock, \
            mock.patch("subprocess.Popen") as popen_mock:
        _dispatch_one_implementer(
            issue_number=463, issue_title="Settings", tick_log=None,
        )

    popen_mock.assert_not_called()
    relabel_mock.assert_not_called()
    write_mock.assert_not_called()
    escalate_mock.assert_called_once()
    assert "engine_settings" in escalate_mock.call_args[1]["reason"]


def test_fixer_dispatch_refuses_a_missing_engine_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixer re-checks the settings file exactly as the implementer does."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    configure_engine_settings(
        monkeypatch, tmp_path, str(tmp_path / "engine-settings.json"),
    )
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)), \
            mock.patch("tick.git", side_effect=fixer_git(repo_root)), \
            mock.patch("tick.gh_text", side_effect=["main", "abc123", "fix-branch"]), \
            mock.patch("tick.gh_json", side_effect=fixer_gh_json(
                pr_body=FIXER_PROMPT_PR_BODY, issues=FIXER_PROMPT_ISSUES,
            )), \
            mock.patch("tick._publish_marker_or_revert") as publish_mock, \
            mock.patch("tick.relabel") as relabel_mock, \
            mock.patch("tick._escalate_dispatch_setup_failure") as escalate_mock, \
            mock.patch("subprocess.Popen") as popen_mock:
        _dispatch_one_fixer(pr_number=43, tick_log=None)

    popen_mock.assert_not_called()
    publish_mock.assert_not_called()
    relabel_mock.assert_not_called()
    escalate_mock.assert_called_once()
    assert "engine_settings" in escalate_mock.call_args[1]["reason"]


def test_implementer_dispatch_sets_sixty_minute_deadline(
    tmp_path: Path,
) -> None:
    """Implementer lease markers should allow long verification runs."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    markers: list[dict[str, object]] = []

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.time.time", side_effect=[1000.0, 1001.0, 1002.0]):
            with mock.patch("tick.git") as git_mock:
                git_mock.return_value = str(repo_root)
                with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                    def capture_marker(path: str, data: str) -> None:
                        Path(path).parent.mkdir(parents=True, exist_ok=True)
                        Path(path).write_text(data)
                        markers.append(json.loads(data))

                    aw_mock.side_effect = capture_marker
                    with mock.patch("subprocess.Popen") as popen_mock:
                        proc = mock.MagicMock()
                        proc.pid = 12345
                        popen_mock.return_value = proc
                        with mock.patch("tick.relabel"):
                            _dispatch_one_implementer(
                                issue_number=461,
                                issue_title="PWA icons",
                                tick_log=None,
                            )

    assert markers[0]["deadline"] == 1001.0 + 3600


def test_fixer_dispatch_honors_permission_mode_auto(
    tmp_path: Path,
) -> None:
    """The fixer engine argv carries the requested auto permission mode.

    Same contract as the implementer: the option must reach the exact
    ``--permission-mode`` value in the production argv, or the classifier
    would never engage.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch(
                    "tick.gh_json",
                    side_effect=fixer_gh_json(
                        pr_body=FIXER_PROMPT_PR_BODY, issues=FIXER_PROMPT_ISSUES,
                    ),
                ):
                    with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                        aw_mock.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    proc_mock = mock.MagicMock()
                                    proc_mock.pid = 12345
                                    popen.return_value = proc_mock
                                    _dispatch_one_fixer(
                                        pr_number=43,
                                        tick_log=None,
                                        permission_mode="auto",
                                    )

    popen.assert_called_once()
    engine_cmd = popen.call_args[0][0]
    assert engine_cmd[engine_cmd.index("--permission-mode") + 1] == "auto", (
        f"fixer must launch with --permission-mode auto when requested, "
        f"got: {engine_cmd}"
    )
    assert "--allowedTools" in engine_cmd, (
        f"auto mode must keep the bounded allowlist, got: {engine_cmd}"
    )


@pytest.mark.parametrize("override", [None, "dontAsk"])
def test_fixer_dispatch_uses_bounded_unattended_flags(
    tmp_path: Path, override: str | None,
) -> None:
    """Fixer engine runs unattended with a bounded tool allowlist.

    Same contract as the implementer: auto by default with dontAsk available,
    narrow pre-approved grants, prompt via stdin, result via stdout redirection.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    review_body = "<!-- pr-pipeline:review -->\nfindings"

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ) as git_mock:
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[{"body": review_body}],
                    )
                    with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                        aw_mock.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    proc_mock = mock.MagicMock()
                                    proc_mock.pid = 12345
                                    popen.return_value = proc_mock
                                    _dispatch_one_fixer(
                                        pr_number=42,
                                        tick_log=None,
                                        **({"permission_mode": override} if override else {}),
                                    )

    popen.assert_called_once()
    engine_cmd = popen.call_args[0][0]
    assert "--dangerously-skip-permissions" not in engine_cmd, (
        f"fixer must use the bounded allowlist, not permission bypass, "
        f"got: {engine_cmd}"
    )
    assert "-o" not in engine_cmd, (
        f"claude has no -o flag; result capture is stdout redirection, "
        f"got: {engine_cmd}"
    )
    assert engine_cmd[engine_cmd.index("--permission-mode") + 1] == (override or "auto")
    allowed = engine_cmd[engine_cmd.index("--allowedTools") + 1]
    # The fixer's push permission is EXACT and stage-specific: the detached
    # worktree has no checked-out branch, so the only pre-approved push is the
    # fully-qualified refspec to this PR's validated head ref. No wildcard,
    # and no bare-HEAD form either.
    assert "Bash(git push origin HEAD:refs/heads/fix-branch)" in allowed
    assert "HEAD*" not in allowed
    assert "Bash(git push origin HEAD)" not in allowed
    assert engine_cmd[engine_cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in engine_cmd

    kwargs = popen.call_args[1]
    assert getattr(kwargs["stdin"], "name", "").endswith("prompt.txt")
    assert getattr(kwargs["stdout"], "name", "").endswith("result.json")

    # Production-path proof: the worktree is detached (no local branch custody,
    # no `checkout -B`), and the prompt's push destination is exactly the
    # refspec the allowlist permits — prompt and permission cannot drift apart.
    wt_add_calls = [
        c.args for c in git_mock.call_args_list
        if "worktree" in c.args and "add" in c.args
    ]
    assert len(wt_add_calls) == 1
    assert "--detach" in wt_add_calls[0]
    assert "origin/fix-branch" in wt_add_calls[0]
    assert not any("checkout" in c.args for c in git_mock.call_args_list)
    prompt_text = (wt_root / "pr-42" / "prompt.txt").read_text()
    assert "git push origin HEAD:refs/heads/fix-branch" in prompt_text


def test_fixer_dispatch_rejects_unsafe_head_ref(
    tmp_path: Path,
) -> None:
    """A head ref that fails refspec validation must escalate, never launch.

    The fixer's head_ref is templated into both the push allowlist entry and
    the prompt; a ref containing glob/option/whitespace characters could widen
    the exact permission or smuggle arguments, so it fails closed to a human.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    for bad_ref in ("bad ref", "ref*glob", "-leading-dash", "a..b", "ref.lock"):
        with mock.patch("tick.WT_ROOT", str(wt_root)):
            with mock.patch("tick.git") as git_mock:
                git_mock.return_value = str(repo_root)
                with mock.patch("tick.gh_text") as gh_text_mock:
                    gh_text_mock.side_effect = ["main", "abc123", bad_ref]
                    with mock.patch(
                        "tick._escalate_dispatch_setup_failure"
                    ) as escalate_mock:
                        with mock.patch("subprocess.Popen") as popen:
                            _dispatch_one_fixer(
                                pr_number=42,
                                tick_log=None,
                            )

        popen.assert_not_called()
        escalate_mock.assert_called_once()
        assert "head ref" in escalate_mock.call_args[1]["reason"]


def test_fixer_dispatch_sets_sixty_minute_deadline(
    tmp_path: Path,
) -> None:
    """Fixer lease markers should allow long verification runs."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    review_body = "<!-- pr-pipeline:review -->\nfindings"
    markers: list[dict[str, object]] = []

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.time.time", side_effect=[2000.0, 2001.0, 2002.0]):
            with mock.patch(
                "tick.git", side_effect=fixer_git(repo_root),
            ):
                with mock.patch("tick.gh_text") as gh_text_mock:
                    gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                    with mock.patch("tick.gh_json") as gh_json_mock:
                        gh_json_mock.side_effect = fixer_gh_json(
                            comments=[{"body": review_body}],
                        )
                        with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                            def capture_marker(path: str, data: str) -> None:
                                Path(path).parent.mkdir(parents=True, exist_ok=True)
                                Path(path).write_text(data)
                                markers.append(json.loads(data))

                            aw_mock.side_effect = capture_marker
                            with mock.patch(
                                "tick._publish_marker_or_revert",
                                return_value=True,
                            ):
                                with mock.patch("tick.relabel"):
                                    with mock.patch(
                                        "subprocess.Popen"
                                    ) as popen:
                                        proc = mock.MagicMock()
                                        proc.pid = 12345
                                        popen.return_value = proc
                                        _dispatch_one_fixer(
                                            pr_number=42,
                                            tick_log=None,
                                        )

    assert markers[0]["deadline"] == 2001.0 + 3600


def test_dispatch_one_fixer_prompt_requires_green_verification_before_sentinel(
    tmp_path: Path,
) -> None:
    """Fixer prompt must require green verification before completion."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    review_body = "<!-- pr-pipeline:review -->\nfindings"

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick.git", side_effect=fixer_git(repo_root),
        ):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json") as gh_json_mock:
                    gh_json_mock.side_effect = fixer_gh_json(
                        comments=[{"body": review_body}],
                    )
                    with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                        aw_mock.side_effect = lambda path, data: (
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                            or Path(path).write_text(data)
                        )
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    proc_mock = mock.MagicMock()
                                    proc_mock.pid = 12345
                                    popen.return_value = proc_mock
                                    _dispatch_one_fixer(
                                        pr_number=42,
                                        tick_log=None,
                                    )

    popen.assert_called_once()
    prompt = (wt_root / "pr-42" / "prompt.txt").read_text()
    assert "the repository's own verification commands" in prompt
    assert "`CLAUDE.md`/`AGENTS.md`" in prompt
    # The pipeline drives repositories it does not own, so the prompt must not
    # name any one stack's toolchain: what to run and in what order is the
    # target repo's own guides to state, and what the engine is *permitted* to
    # run is the operator's `[engine] verify_tools` grant. A stack name that
    # creeps back in here is wrong for every install but one.
    for tool in ("pytest", "ruff", "pyright", "npm", "cargo", "gradle"):
        assert tool not in prompt, (
            f"prompt hard-codes the tool {tool!r}, got: {prompt}"
        )
    assert (
        "Only output FIXES_PUSHED_RECHECK_NEEDED after every verification "
        "command the repository prescribes passes"
    ) in prompt
    assert "If any verification command cannot be made green" in prompt
    assert "NEEDS_HUMAN_DECISION" in prompt
    # The harvest comment publishes the engine's own words as the record of what
    # the fixer did, and a bare sentinel is otherwise a protocol-valid result —
    # so the demand for a summary has to live in the dispatched prompt, not only
    # in the renderer that formats whatever comes back.
    assert "output a change summary" in prompt
    assert "Before the completion sentinel" in prompt


def test_dispatch_one_fixer_prompt_binds_revision_hygiene_before_editing(
    tmp_path: Path,
) -> None:
    """The rendered fixer prompt must load the revision-hygiene skill first.

    The skill's rules 2 and 3 (story surface, sibling omissions) cover two of the
    three failure classes the recent review record shows, but a skill nobody is
    told to open is discretionary reading. The binding therefore has to live in
    the prompt the engine actually receives, and the assertion runs against that
    rendered prompt — a template-prefix check would pass on a substitution the
    production render site never supplies.

    The prompt names the doc *path* rather than telling the engine to invoke a
    skill: dispatched engines run under a bounded allowlist that carries ``Read``
    and no ``Skill`` tool, so an "invoke" instruction would be denied and the
    binding would be theatre.
    """
    import tick  # type: ignore[import-untyped]

    prompt = render_fixer_prompt(tmp_path)

    doc = tick.skill_doc_path(tick.REVISION_HYGIENE_SKILL)
    assert (
        f"Before you edit anything, read `{doc}` and follow its revision rules "
        "for the whole run."
    ) in prompt, f"fixer prompt does not bind {doc} before editing, got: {prompt}"


def test_dispatch_one_fixer_prompt_carries_the_story_surface_and_contract(
    tmp_path: Path,
) -> None:
    """Rules 2 and 5 are unexecutable without the body and the issue.

    The engine's allowlist carries no ``gh`` and no web access, so a prompt that
    demands a story-surface check and a re-read of the cited contract while
    supplying neither is asking for work the engine cannot do. Both arrive as
    controller-read text, and the prompt says the quoted copy is all there is.
    """
    prompt = render_fixer_prompt(tmp_path)

    assert FIXER_PROMPT_PR_BODY.strip() in prompt, (
        f"the fixer prompt does not carry the PR body, got: {prompt}"
    )
    assert (
        "### Issue #7 — feat: keep the custody marker durable"
    ) in prompt, (
        f"the fixer prompt does not carry the linked issue contract: {prompt}"
    )
    assert "1. The marker survives a failed relabel." in prompt, (
        "the linked issue's requirements did not reach the prompt"
    )
    assert (
        "you have no GitHub access, so what is quoted above is all of it."
    ) in prompt, f"the prompt does not bound what the engine can see: {prompt}"


def test_dispatch_one_fixer_prompt_states_when_no_issue_is_linked(
    tmp_path: Path,
) -> None:
    """An absent contract must read as absent, not as an empty section.

    A PR that closes no issue is legal, and the fixer still runs. What it must
    not do is read a blank heading as a contract with no requirements.
    """
    prompt = render_fixer_prompt(tmp_path, issues=())

    assert (
        "(GitHub reports no issue this PR closes, so there is no linked "
        "contract to re-read)"
    ) in prompt, f"an unlinked PR renders a silent empty contract: {prompt}"


def test_dispatch_one_fixer_retains_custody_when_the_issue_read_fails(
    tmp_path: Path,
) -> None:
    """A failed authoritative read must not become "no contract".

    The linked issue is the contract rule 5 asks the head to satisfy. Turning a
    transient ``gh`` failure into a fixer that silently revises without it is
    the failure mode the read exists to close, so dispatch keeps the PR at
    ``ai:changes-requested`` and retries rather than launching or escalating.
    """
    from tick import TickLog, _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    tick_log = TickLog.open(str(tmp_path / "logs"))

    reads: list[tuple[str, ...]] = []

    def gh_json_side_effect(*args: str) -> object:
        reads.append(args)
        if args[0] == "issue":
            raise subprocess.CalledProcessError(1, "gh")
        # A well-formed reference: the read has to be reached for the raise
        # above to be the thing under test rather than dead code.
        return {
            "body": "## Summary\nCloses #7\n",
            "closingIssuesReferences": [{
                "number": 7,
                "repository": {"name": "test-repo", "owner": {"login": "test-owner"}},
            }],
        }

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fixer_git(repo_root)):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json", side_effect=gh_json_side_effect):
                    with mock.patch("tick._relabel_with_context") as relabel_mock:
                        with mock.patch("tick.relabel") as plain_relabel:
                            with mock.patch("subprocess.Popen") as popen:
                                popen.return_value.pid = 12345
                                _dispatch_one_fixer(
                                    pr_number=42, tick_log=tick_log,
                                )

    assert any(args[0] == "issue" for args in reads), (
        "the issue read was never reached, so the failure under test never "
        f"happened: {reads}"
    )
    popen.assert_not_called()
    relabel_mock.assert_not_called()
    plain_relabel.assert_not_called()
    assert not (wt_root / "pr-42").exists(), (
        "a retained dispatch left a scratch worktree behind"
    )
    skipped = next(
        entry for entry in tick_log.tail()
        if entry["action"] == "skip" and "linked issue" in entry["reason"]
    )
    assert skipped["pr"] == 42


def test_dispatch_one_fixer_retains_custody_when_the_pr_read_fails(
    tmp_path: Path,
) -> None:
    """The PR-view read owes the same fail-closed proof as the issue read.

    It is the sibling exception path, and the earlier one: a `gh` failure here
    means neither the story surface nor the contract can be resolved, so the
    dispatch must retain rather than render both as absent.
    """
    from tick import TickLog, _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fixer_git(repo_root)):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch(
                    "tick.gh_json",
                    side_effect=subprocess.CalledProcessError(1, "gh"),
                ):
                    with mock.patch("tick._relabel_with_context") as relabel_mock:
                        with mock.patch("tick.relabel") as plain_relabel:
                            with mock.patch("subprocess.Popen") as popen:
                                popen.return_value.pid = 12345
                                _dispatch_one_fixer(
                                    pr_number=42, tick_log=tick_log,
                                )

    popen.assert_not_called()
    relabel_mock.assert_not_called()
    plain_relabel.assert_not_called()
    assert not (wt_root / "pr-42").exists()
    assert any(
        entry["action"] == "skip" and "linked issue" in entry["reason"]
        for entry in tick_log.tail()
    ), "a failed PR read did not log the retain reason"


def test_dispatch_one_fixer_retains_custody_on_a_malformed_pr_payload(
    tmp_path: Path,
) -> None:
    """A payload missing its typed fields is not an empty body.

    ``gh`` returning a shape this cannot read is an unknown, and an unknown that
    renders as a blank story surface teaches the engine the PR says nothing.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fixer_git(repo_root)):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json", return_value={"body": None}):
                    with mock.patch("tick._relabel_with_context") as relabel_mock:
                        with mock.patch("subprocess.Popen") as popen:
                            popen.return_value.pid = 12345
                            _dispatch_one_fixer(pr_number=42, tick_log=None)

    popen.assert_not_called()
    relabel_mock.assert_not_called()


def test_dispatch_one_fixer_prompt_announces_a_truncated_context(
    tmp_path: Path,
) -> None:
    """An oversized document must be cut visibly, not silently.

    A contract that just stops reads as a contract with fewer requirements —
    which is the same defect as not injecting it at all, with a receipt.
    """
    import tick  # type: ignore[import-untyped]

    oversized = "## Summary\n" + ("requirement line\n" * 4000)
    assert len(oversized) > tick.FIXER_CONTEXT_BUDGET

    prompt = render_fixer_prompt(tmp_path, pr_body=oversized)

    assert (
        f"[Truncated at {tick.FIXER_CONTEXT_BUDGET} characters. Treat anything "
        "past this point as unread, not as absent.]"
    ) in prompt, f"an oversized PR body was cut without saying so: {prompt[:2000]}"


def test_dispatch_one_fixer_prompt_represents_every_linked_issue(
    tmp_path: Path,
) -> None:
    """An oversized early contract must not erase a later one.

    Every linked issue lands in one substitution, so a single cap over the
    joined block spends the whole budget on the first contract and drops the
    rest — heading included. That failure is worse than truncation: a cut says
    "unread" about text the engine can see the start of, while a dropped issue
    leaves no trace that a second contract was ever linked, and a fixer cannot
    satisfy a requirement it was never told exists.

    The budget still has to hold, so the oversized contract is cut rather than
    admitted whole.
    """
    import tick  # type: ignore[import-untyped]

    prompt = render_fixer_prompt(
        tmp_path,
        issues=[
            {
                "number": 7,
                "title": "feat: the oversized first contract",
                "body": "F" * (tick.FIXER_CONTEXT_BUDGET * 2),
            },
            {
                "number": 8,
                "title": "fix: the contract that must survive",
                "body": "## Scope\n1. SECOND-CONTRACT-REQUIREMENT holds.\n",
            },
        ],
    )

    assert "### Issue #8 — fix: the contract that must survive" in prompt, (
        "a later linked issue lost its heading, so the fixer cannot tell the "
        f"contract exists at all: {prompt[-2000:]}"
    )
    assert "SECOND-CONTRACT-REQUIREMENT" in prompt, (
        f"a later linked issue's requirements were dropped: {prompt[-2000:]}"
    )
    assert "### Issue #7 — feat: the oversized first contract" in prompt, (
        "the first contract lost its heading to the fair-share split"
    )
    assert prompt.count("F") <= tick.FIXER_CONTEXT_BUDGET, (
        "the oversized contract was admitted whole, so the fix bought room by "
        "dropping the budget rather than by sharing it"
    )


def test_dispatch_one_fixer_prompt_lets_a_short_contract_donate_its_share(
    tmp_path: Path,
) -> None:
    """Reserving an even share would cut a contract that had room to spare.

    A flat ``budget // n`` fixes the starvation but pays for it everywhere else:
    the ordinary two-issue PR, where one contract is a paragraph, would halve
    the long one for nothing. The short contract keeps only what it uses and the
    remainder rolls forward, so guaranteeing the second issue a place costs the
    first almost nothing.
    """
    import tick  # type: ignore[import-untyped]

    prompt = render_fixer_prompt(
        tmp_path,
        issues=[
            {
                "number": 7,
                "title": "feat: the long contract",
                "body": "F" * (tick.FIXER_CONTEXT_BUDGET * 2),
            },
            {"number": 8, "title": "fix: a one-line contract", "body": "One line."},
        ],
    )

    assert "One line." in prompt, "the short contract was cut to fund the long one"
    assert prompt.count("F") > tick.FIXER_CONTEXT_BUDGET * 9 // 10, (
        "a one-line contract reserved a share it cannot spend, so the long "
        f"contract lost most of the budget to it: kept {prompt.count('F')} of "
        f"{tick.FIXER_CONTEXT_BUDGET}"
    )


def test_fixer_prompt_allows_reasoned_no_change_without_a_push(tmp_path: Path) -> None:
    prompt = render_fixer_prompt(tmp_path)
    assert "Decision: no-change" in prompt
    assert "Reason:" in prompt
    assert "NO_CHANGES_RECHECK_NEEDED" in prompt
    assert "do not commit or push" in prompt
    assert "every finding" in prompt
    assert "needs no declaration" not in prompt


def test_fixer_dispatch_identity_is_unique_within_one_second(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with mock.patch("tick.time.time", return_value=1234567890):
        render_fixer_prompt(first)
        render_fixer_prompt(second)
    markers = [json.loads((root / "worktrees/pr-42/dispatch.json").read_text())
               for root in (first, second)]
    assert markers[0]["dispatch_id"] != markers[1]["dispatch_id"]


def test_dispatch_one_fixer_prompt_rechecks_the_declaration_before_the_sentinel(
    tmp_path: Path,
) -> None:
    """Green commands must stop being the fixer's whole completion oracle.

    Declaring repair-versus-replacement only before editing lets an unaudited
    replacement exist by the time the run ends — work that starts as a repair
    crosses the boundary under review pressure. The second moment is therefore
    checked against the diff that was actually pushed, and the sentinel is gated
    on it alongside the three commands, which can see none of this.
    """
    prompt = render_fixer_prompt(tmp_path)

    for clause in (
        "they are necessary but not sufficient, because no green command can "
        "see a stale story surface, an unswept sibling path, or a replacement "
        "nobody audited.",
        "Before the completion sentinel, re-check your repair-or-replacement "
        "decision against the diff you actually pushed, because a fix that "
        "began as a repair can cross into a replacement; a replacement then "
        "owes an audit of the new design's call sites, operating modes, and "
        "failure or interrupted states, plus every doc and PR-body claim that "
        "still teaches the replaced contract.",
        "Only output FIXES_PUSHED_RECHECK_NEEDED after every verification "
        "command the repository prescribes passes, the push succeeds, and "
        "every finding you addressed carries a decision that matches the diff "
        "you pushed.",
    ):
        assert clause in prompt, (
            f"fixer prompt is missing the completion gate: {clause!r}, "
            f"got: {prompt}"
        )


def test_dispatch_one_fixer_prompt_shapes_the_published_declaration(
    tmp_path: Path,
) -> None:
    """A replacement must publish reasoning the next reviewer can size.

    Stating a rationale is not enough on its own — a round-one revision that
    justified rejecting the reviewer's shape still cost two more rounds, because
    what was missing was the audit of the substitute. Each decision
    therefore carries both halves in a fixed format, and it sits *inside* the
    block of the finding it belongs to: one run-wide declaration cannot say
    which finding was replaced, so a mixed run publishes one argument the next
    reviewer has to re-attach to a finding by guessing.
    """
    import pipeline

    prompt = render_fixer_prompt(tmp_path)

    assert (
        "Open each block with `Finding:` naming the finding in one line, then "
        "its own decision line — either `Decision: repair`, or `Decision: "
        "replacement` followed by one line each for `Rejected shape:` (the "
        "shape the reviewer proposed), `Why rejected:`, `Replacing contract:`, "
        "and `Audited surfaces:` — and close the block with `Change:` naming "
        "what you changed to close it."
    ) in prompt, f"fixer prompt does not fix the declaration format, got: {prompt}"
    assert (
        "Every finding carries its own decision: one run may mix repairs, "
        "replacements, and no-change decisions, and each replacement states its own four "
        "lines rather than sharing one set with another finding."
    ) in prompt, (
        "the prompt still permits one run-wide declaration, so a mixed review "
        f"cannot say which finding was replaced, got: {prompt}"
    )
    assert "Declaration: repair" not in prompt, (
        "the run-wide declaration survives alongside the per-finding one"
    )
    assert (
        f"Keep the whole summary under {pipeline.FIX_SUMMARY_BUDGET} characters."
    ) in prompt, (
        "the budget the prompt states is not the harvest's own publish budget, "
        f"so a compliant summary can still truncate, got: {prompt}"
    )


def test_dispatch_one_fixer_prompt_offers_the_stamped_pr_body_revision_path(
    tmp_path: Path,
) -> None:
    """The prompt must name the file and the exact stamp the harvest checks.

    The engine has no ``gh``, so the only way it can correct a stale story
    surface is a file the controller reads back. A prompt that names a different
    filename, or leaves the engine to invent the provenance line, produces a
    revision the harvest rejects — the feature would look implemented and never
    publish anything.
    """
    import tick  # type: ignore[import-untyped]

    prompt = render_fixer_prompt(tmp_path)

    assert f"`{tick._PR_BODY_REVISION_FILE}` in the worktree root" in prompt, (
        f"the fixer prompt does not name the revision file, got: {prompt}"
    )
    stamps = [
        line for line in prompt.splitlines()
        if tick._PR_BODY_REVISION_STAMP in line
    ]
    assert stamps, f"the prompt states no provenance stamp, got: {prompt}"
    assert f"`{tick._PR_BODY_REVISION_STAMP} fix-42-" in stamps[0], (
        "the prompt's stamp is not this dispatch's own, so any revision the "
        f"engine writes is rejected as unstamped: {stamps[0]}"
    )
    assert "Do not commit" in prompt and "absent" in prompt, (
        f"the prompt does not keep the no-revision case cheap, got: {prompt}"
    )


def test_dispatch_one_fixer_marker_records_the_body_it_handed_the_engine(
    tmp_path: Path,
) -> None:
    """Both markers carry the revision provenance, and stay schema-valid.

    The harvest replaces the PR body only over the exact body dispatch captured,
    and only when the replacement preserves the closure contract that body
    declared. Neither check can run from the harvest's own reads — by then the
    body may already have been edited — so both are pinned at dispatch, together
    with whether the engine was shown the whole body. ``valid_marker`` rejects
    any key it does not allowlist, so a marker carrying them without the schema
    change reads as corrupt and strands the engine.
    """
    import pipeline
    from tick import (  # type: ignore[import-untyped]
        _dispatch_one_fixer,
        _pr_body_digest,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    published: list[dict[str, object]] = []

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fixer_git(repo_root)):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch(
                    "tick.gh_json",
                    side_effect=fixer_gh_json(
                        pr_body=FIXER_PROMPT_PR_BODY,
                        issues=FIXER_PROMPT_ISSUES,
                    ),
                ):
                    with mock.patch(
                        "tick._publish_marker_or_revert", return_value=True,
                    ) as publish:
                        publish.side_effect = lambda **kw: (
                            published.append(kw["published_marker"]) or True
                        )
                        with mock.patch("tick.relabel"):
                            with mock.patch("subprocess.Popen") as popen:
                                proc_mock = mock.MagicMock()
                                proc_mock.pid = 12345
                                popen.return_value = proc_mock
                                _dispatch_one_fixer(pr_number=42, tick_log=None)

    lease = json.loads((wt_root / "pr-42" / "dispatch.json").read_text())
    for name, marker in (("lease", lease), ("published", published[0])):
        assert marker["pr_body"] == {
            "digest": _pr_body_digest(FIXER_PROMPT_PR_BODY),
            "summary_closes": 7,
            "closes": ["#7"],
            "truncated": False,
        }, f"the {name} marker did not pin the body handed to the engine"
        assert pipeline.valid_marker(
            marker, expected_pr=42, expected_stage="fix",
        ), f"the {name} marker is rejected by the marker schema"


def test_dispatch_pins_a_closure_contract_github_reports_no_linked_issue_for() -> None:
    """A stacked PR still has a closure contract; GitHub just does not show it.

    GitHub ignores closing keywords when a PR's base is not the default branch,
    so ``closingIssuesReferences`` comes back empty for a PR whose body opens
    with ``Closes #N`` exactly as the repo's contract requires. Deriving the
    contract from that field would silently switch the body-revision path off for
    every stacked PR — the feature would look present and never fire. Reading the
    body is what keeps it working, and the contract block still says truthfully
    that GitHub reports no linked issue.
    """
    from tick import _fixer_pr_context, _pr_body_digest  # type: ignore[import-untyped]

    stacked_body = "## Summary\nCloses #7\n\n## What changed\n- Structure: x\n"

    with mock.patch("tick.gh_json", side_effect=fixer_gh_json(
        pr_body=stacked_body, issues=(),
    )):
        context = _fixer_pr_context(42)

    assert context is not None
    assert context.body_provenance is not None, (
        "a stacked PR's body pins no closure contract, so the fixer can never "
        "hand back a replacement body for one"
    )
    assert context.body_provenance.as_marker_field() == {
        "digest": _pr_body_digest(stacked_body),
        "summary_closes": 7,
        "closes": ["#7"],
        "truncated": False,
    }
    assert "no issue this PR closes" in context.contract_block, (
        "the quoted contract must still reflect what GitHub actually reports"
    )


def test_dispatch_one_fixer_resolves_its_bound_skill_from_the_install(
    tmp_path: Path,
) -> None:
    """A target repository that never heard of the skill must still get a fixer.

    The doc ships with the pipeline, so the operator's installed copy is the
    right one for every target by construction. Probing the fixer's own
    worktree — a detached checkout of a PR head in a repository the pipeline
    does not own — asked a question with one answer everywhere but here, and
    escalated every fix dispatch on every adopter.
    """
    import pipeline  # type: ignore[import-untyped]
    from tick import TickLog, _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    tick_log = TickLog.open(str(tmp_path / "logs"))

    def bare_worktree(*args: str) -> str:
        # A checkout whose tree carries no skill docs at all.
        if "worktree" in args and "add" in args:
            Path(args[-2]).mkdir(parents=True, exist_ok=True)
        return str(repo_root)

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=bare_worktree):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch("tick.gh_json", side_effect=fixer_gh_json()):
                    with mock.patch(
                        "tick._publish_marker_or_revert", return_value=True,
                    ):
                        with mock.patch("tick.relabel"):
                            with mock.patch("subprocess.Popen") as popen:
                                popen.return_value.pid = 12345
                                _dispatch_one_fixer(
                                    pr_number=42, tick_log=tick_log,
                                )

    popen.assert_called_once()
    assert not [
        entry for entry in tick_log.tail() if entry["action"] == "skill_unresolved"
    ], "a bare target checkout still escalated on the skill probe"

    prompt = (wt_root / "pr-42" / "prompt.txt").read_text()
    doc = pipeline.skill_doc_path(pipeline.REVISION_HYGIENE_SKILL)
    assert doc in prompt, f"the prompt does not name the installed doc {doc}"
    assert Path(doc).is_file()


def test_dispatch_one_fixer_escalates_when_the_install_lacks_a_bound_skill(
    tmp_path: Path,
) -> None:
    """A doc missing from the install is a packaging defect, and fails closed.

    The binding is a path in a prompt, so an absent doc degrades silently: the
    engine reads nothing, revises anyway, and still emits its completion
    sentinel. Only what the probe reads moved to the install — the disposition
    did not. No lease, no relabel to ``ai:fixing``, no engine launched, and the
    scratch worktree is removed.
    """
    from tick import TickLog, _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    empty_install = tmp_path / "install-without-the-doc"
    empty_install.mkdir()
    tick_log = TickLog.open(str(tmp_path / "logs"))

    def worktree(*args: str) -> str:
        if "worktree" in args and "add" in args:
            Path(args[-2]).mkdir(parents=True, exist_ok=True)
        return str(repo_root)

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("pipeline.INSTALL_SKILLS_ROOT", empty_install):
            with mock.patch("tick.git", side_effect=worktree):
                with mock.patch("tick.gh_text") as gh_text_mock:
                    gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                    with mock.patch("tick.gh_json", side_effect=fixer_gh_json()):
                        with mock.patch(
                            "tick.gh", side_effect=allow_escalation_comment(),
                        ), mock.patch(
                            "tick._relabel_with_context",
                        ) as relabel_mock:
                            with mock.patch("tick.relabel"):
                                with mock.patch("subprocess.Popen") as popen:
                                    popen.return_value.pid = 12345
                                    _dispatch_one_fixer(
                                        pr_number=42, tick_log=tick_log,
                                    )

    popen.assert_not_called()
    relabel_mock.assert_called_once()
    assert relabel_mock.call_args.args[3] == "ai:needs-human-decision"
    assert not (wt_root / "pr-42").exists(), (
        "an escalated dispatch left its scratch worktree behind"
    )

    unresolved = next(
        entry for entry in tick_log.tail() if entry["action"] == "skill_unresolved"
    )
    assert unresolved["pr"] == 42
    assert unresolved["stage"] == "fix"
    assert unresolved["details"]["skills"] == ["review-response-hygiene"]
    assert unresolved["details"]["probe_paths"] == [
        str(empty_install / "review-response-hygiene" / "SKILL.md"),
    ]


def test_dispatch_one_implementer_resolves_definition_of_done_from_the_install(
    tmp_path: Path,
) -> None:
    """A target without its own skill copy still gets the completion gate.

    The implementer runs in a fresh checkout of the target's base branch, which
    has no reason to carry the pipeline's guidance. The prompt must name the
    install's doc — the same string dispatch probes — and a bare checkout must
    not trip the probe.
    """
    import pipeline  # type: ignore[import-untyped]
    from tick import TickLog, _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=bare_implementer_git(repo_root)):
            with mock.patch("tick._publish_marker_or_revert", return_value=True):
                with mock.patch("tick.relabel"):
                    with mock.patch("subprocess.Popen") as popen:
                        popen.return_value.pid = 12345
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=tick_log,
                        )

    popen.assert_called_once()
    assert not [
        entry for entry in tick_log.tail() if entry["action"] == "skill_unresolved"
    ], "a bare target checkout escalated on the implementer skill probe"

    prompt = (wt_root / "issue-461" / "prompt.txt").read_text()
    doc = pipeline.skill_doc_path("definition-of-done")
    assert Path(doc).is_file()
    assert f"read `{doc}`" in prompt, (
        f"the implementer prompt does not name the installed doc {doc}"
    )
    assert not (wt_root / "issue-461" / "skills").exists()


def test_dispatch_one_implementer_prompt_gates_completion_on_definition_of_done(
    tmp_path: Path,
) -> None:
    """The rendered prompt pins both invocation moments and the evidence gate.

    A skill binding is discretionary reading unless the executed prompt names
    when it must run. Phase A has to come before the first edit, Phase B before
    the commit, the block has to land in the PR body the harvest publishes, and
    the sentinel has to wait for it.
    """
    import pipeline  # type: ignore[import-untyped]
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=bare_implementer_git(repo_root)):
            with mock.patch("tick._publish_marker_or_revert", return_value=True):
                with mock.patch("tick.relabel"):
                    with mock.patch("subprocess.Popen") as popen:
                        popen.return_value.pid = 12345
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=None,
                        )

    prompt = (wt_root / "issue-461" / "prompt.txt").read_text()
    doc = pipeline.skill_doc_path("definition-of-done")
    phase_a = prompt.index(f"Before your first edit, read `{doc}` and run its Phase A")
    phase_b = prompt.index("before you commit, run the skill's Phase B")
    commit = prompt.index("Commit, then push")
    assert phase_a < phase_b < commit, (
        "the prompt must order Phase A, then Phase B, then the commit"
    )
    assert (
        "put the skill's Phase B evidence block, with `Proved at:` naming the "
        "pushed head, under `## Test coverage`"
    ) in prompt
    assert (
        "Only output IMPLEMENTATION_COMPLETE after every verification command "
        "the repository prescribes passes, the push succeeds, and "
        "`.pr-body.md` carries the Phase B evidence block"
    ) in prompt
    # The skill's own exit for a change with no runtime claim, so a doc-only
    # issue is not forced to invent an entry point.
    assert "`Definition of done: n/a` line" in prompt

    assert "If you cannot read the skill or a reference it requires" in prompt
    assert "cannot complete any required Phase A or Phase B step" in prompt
    assert (
        "output NEEDS_HUMAN_DECISION; do not output IMPLEMENTATION_COMPLETE"
    ) in prompt
    assert "not run — <command> denied" in prompt
    assert "never claim their results or use n/a to excuse missing evidence" in prompt
    assert "re-run the affected Phase B steps" in prompt
    assert "If the skill's Scope exempts the change, record its n/a decision before editing" in prompt


def test_dispatch_one_implementer_escalates_when_the_install_lacks_definition_of_done(
    tmp_path: Path,
) -> None:
    """An unreadable installed doc fails closed before any engine launch.

    Without the doc the engine reads nothing, skips both phases, and still
    emits its sentinel. Dispatch must escalate instead: no lease, no engine,
    no scratch worktree left behind, and a ``skill_unresolved`` entry naming
    the probed path.
    """
    from tick import TickLog, _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    empty_install = tmp_path / "install-without-the-doc"
    empty_install.mkdir()
    tick_log = TickLog.open(str(tmp_path / "logs"))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("pipeline.INSTALL_SKILLS_ROOT", empty_install):
            with mock.patch(
                "tick.git", side_effect=bare_implementer_git(repo_root),
            ):
                with mock.patch(
                    "tick.gh", side_effect=allow_escalation_comment(),
                ), mock.patch("tick._relabel_with_context") as relabel_mock:
                    with mock.patch("tick.relabel"):
                        with mock.patch("subprocess.Popen") as popen:
                            popen.return_value.pid = 12345
                            _dispatch_one_implementer(
                                issue_number=461,
                                issue_title="PWA icons",
                                tick_log=tick_log,
                            )

    popen.assert_not_called()
    relabel_mock.assert_called_once()
    assert relabel_mock.call_args.args[:2] == ("issue", 461)
    assert relabel_mock.call_args.args[3] == "ai:needs-human-decision"
    assert not (wt_root / "issue-461").exists(), (
        "an escalated dispatch left its scratch worktree behind"
    )

    unresolved = next(
        entry for entry in tick_log.tail() if entry["action"] == "skill_unresolved"
    )
    assert unresolved["pr"] == 461
    assert unresolved["stage"] == "implement"
    assert unresolved["details"]["skills"] == ["definition-of-done"]
    assert unresolved["details"]["probe_paths"] == [
        str(empty_install / "definition-of-done" / "SKILL.md"),
    ]


def test_implementer_pr_body_carrying_the_definition_of_done_block_is_published(
    tmp_path: Path,
) -> None:
    """The harvest publishes a body whose `## Test coverage` holds the evidence.

    The block is taken from the installed skill itself, so a later change to
    its shape that the harvest's structural check rejects — a `## ` line, say —
    fails here instead of silently swapping the engine's body for the generated
    fallback, which carries no evidence at all.
    """
    import pipeline  # type: ignore[import-untyped]
    from tick import _implementation_pr_create_args  # type: ignore[import-untyped]

    skill = Path(pipeline.skill_doc_path("definition-of-done")).read_text()
    required_output = skill.split("## Required Output", 1)[1]
    evidence = required_output.split("```", 2)[1].strip()
    assert evidence.startswith("Phase A:"), "the skill's output block moved"

    for block in (evidence, "Definition of done: n/a — docs-only wording fix"):
        (tmp_path / ".pr-body.md").write_text(
            "feat: add the thing\n\n"
            "## Summary\nCloses #461\n- Problem: missing thing.\n\n"
            "## Why this approach\n- How this solves the issue: adds it.\n\n"
            "## What changed\n- Structure: one module.\n- Impact: thing exists.\n\n"
            f"## Test coverage\n```\n{block}\n```\n\n"
            "## Test plan\n- [x] tests\n"
        )
        args = _implementation_pr_create_args(str(tmp_path), 461)

        assert args[:2] == ["--title", "feat: add the thing"], (
            f"the harvest fell back to its generated body for block {block!r}"
        )
        published = Path(args[args.index("--body-file") + 1]).read_text()
        assert block in published


def test_reviewer_dispatch_includes_unattended_flags(
    tmp_path: Path,
) -> None:
    """Reviewer codex engine runs unattended with a read-only sandbox.

    ``codex exec`` is non-interactive by design, so unattended operation
    does NOT require ``--dangerously-bypass-approvals-and-sandbox`` — a
    read-only sandbox (``-s read-only``, the same mode the preflight
    probes) is sufficient for a review (verified end-to-end on codex-cli
    0.142.5): the engine only reads the worktree and emits JSON; the tick
    posts the comment at harvest. ``--ephemeral`` and
    ``--ignore-user-config`` prevent operator config from leaking; the two
    model keys the tick does forward are resolved on the trusted side.
    """
    from tick import (  # type: ignore[import-untyped]
        _REVIEWER_EFFORT_FALLBACK,
        _REVIEWER_MODEL_FALLBACK,
        _dispatch_one_reviewer,
    )

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.touches_review_policy") as trp:
                    trp.return_value = False
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_reviewer(
                                        pr_number=42,
                                        pre_label="ai:implemented",
                                        tick_log=None,
                                    )

    popen.assert_called_once()
    engine_cmd = popen.call_args[0][0]

    # Find codex argv after "--" separator
    sep_idx = engine_cmd.index("--")
    codex_argv = engine_cmd[sep_idx + 1:]

    # Claude's auto default must not select a writable Codex permission preset.
    assert "--approve-for-me" not in codex_argv
    assert "--full-auto" not in codex_argv
    assert "--permission-mode" not in codex_argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex_argv, (
        f"reviewer must run sandboxed; the full bypass hands a prompt-injected "
        f"engine arbitrary writes and command network, got: {codex_argv}"
    )
    assert "-s" in codex_argv and (
        codex_argv[codex_argv.index("-s") + 1] == "read-only"
    ), f"reviewer must pin the read-only sandbox via -s, got: {codex_argv}"
    config_overrides = [
        codex_argv[i + 1]
        for i, a in enumerate(codex_argv)
        if a == "-c" and i + 1 < len(codex_argv)
    ]
    assert "--ephemeral" in codex_argv, (
        f"reviewer must include --ephemeral, got: {codex_argv}"
    )
    assert "--ignore-user-config" in codex_argv, (
        f"reviewer must include --ignore-user-config, got: {codex_argv}"
    )
    assert "--ignore-rules" in codex_argv, (
        f"reviewer must not load execpolicy .rules from the PR head "
        f"worktree, got: {codex_argv}"
    )
    # --ignore-user-config discards the operator's model/effort settings, so
    # both must be passed explicitly or codex silently falls back to its
    # built-in review defaults (codex-auto-review / medium). With an empty
    # operator config (see isolate_reviewer_config) these are the pinned
    # fallbacks.
    assert "--model" in codex_argv and (
        codex_argv[codex_argv.index("--model") + 1] == _REVIEWER_MODEL_FALLBACK
    ), f"reviewer must always pass --model, got: {codex_argv}"
    assert (
        f'model_reasoning_effort="{_REVIEWER_EFFORT_FALLBACK}"'
        in config_overrides
    ), (
        f"reviewer must always pass reasoning effort via -c, got -c "
        f"overrides: {config_overrides}"
    )


def test_parse_cli_args_reads_permission_mode_flag() -> None:
    """--permission-mode parses in both forms; absent stays absent.

    Absent must not become ``"dontAsk"`` here. The flag is the top layer of the
    configuration order, so a default applied at parse time would look like an
    explicit choice on every invocation and shadow the operator's configured
    ``permission_mode`` entirely. ``main`` resolves the empty case.
    """
    from tick import parse_cli_args  # type: ignore[import-untyped]

    assert parse_cli_args([])["permission_mode"] == ""
    spaced = parse_cli_args(["--permission-mode", "auto"])
    assert spaced["permission_mode"] == "auto"
    equals = parse_cli_args(["--permission-mode=auto"])
    assert equals["permission_mode"] == "auto"
    # Coexists with the engine pick and the target.
    mixed = parse_cli_args(
        ["--impl-engine", "claude-ds", "--permission-mode", "auto", "#651"]
    )
    assert mixed["impl_engine"] == "claude-ds"
    assert mixed["permission_mode"] == "auto"
    assert mixed["target"] == "#651"


@pytest.mark.parametrize("override", [None, "auto", "dontAsk"])
def test_dispatch_implementers_passes_permission_mode_through(
    override: str | None,
) -> None:
    """dispatch_implementers() forwards the requested permission mode.

    The option must survive the queue→dispatch boundary: a default that
    swallowed it would launch every engine under ``dontAsk`` no matter what
    the operator asked for.
    """
    import pipeline as _pipeline
    from tick import dispatch_implementers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json_mock:
        gh_json_mock.return_value = {
            "number": 463, "title": "Auto opt-in", "state": "OPEN",
            "author": {"login": TRUSTED_AUTHOR},
        }
        with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
            queue="463", labels="ai:available", body="Body",
        )):
            with mock.patch("tick.can_dispatch_now") as cdn_mock:
                cdn_mock.return_value = _pipeline.AdmissionResult(
                    can_dispatch=True, reason="",
                    in_flight_count=0, max_in_flight=3,
                )
                with mock.patch("tick._dispatch_one_implementer") as dispatch_one:
                    dispatch_implementers(
                        tick_log=None, target=("issue", 463),
                        authoritative_in_flight=[],
                        **({"permission_mode": override} if override else {}),
                    )

    dispatch_one.assert_called_once()
    assert dispatch_one.call_args[1]["permission_mode"] == (override or "auto")


@pytest.mark.parametrize("override", [None, "auto", "dontAsk"])
def test_dispatch_fixers_passes_permission_mode_through(
    override: str | None,
) -> None:
    """dispatch_fixers() forwards the requested permission mode."""
    import pipeline as _pipeline
    from tick import dispatch_fixers  # type: ignore[import-untyped]

    with mock.patch("tick.gh_json") as gh_json_mock:
        gh_json_mock.return_value = [{"number": 42, "title": "Fix auto"}]
        with mock.patch("tick.gh_text", side_effect=gh_text_for_queue(
            queue="42", labels="ai:changes-requested", body="Body",
        )):
            with mock.patch("tick.can_dispatch_now") as cdn_mock:
                cdn_mock.return_value = _pipeline.AdmissionResult(
                    can_dispatch=True, reason="",
                    in_flight_count=0, max_in_flight=3,
                )
                with mock.patch("tick._dispatch_one_fixer") as dispatch_one:
                    dispatch_fixers(
                        tick_log=None, target=("pr", 42),
                        authoritative_in_flight=[],
                        **({"permission_mode": override} if override else {}),
                    )

    dispatch_one.assert_called_once()
    assert dispatch_one.call_args[1]["permission_mode"] == (override or "auto")


def test_parse_cli_args_reads_review_model_and_effort_flags() -> None:
    """--review-model/--review-effort parse in both space and = forms."""
    from tick import parse_cli_args  # type: ignore[import-untyped]

    assert parse_cli_args([]) == {
        "target": None, "impl_engine": "claude", "model": "",
        "review_model": "", "review_effort": "",
        "permission_mode": "", "loop": False,
    }

    spaced = parse_cli_args(
        ["--review-model", "gpt-6", "--review-effort", "xhigh"]
    )
    assert spaced["review_model"] == "gpt-6"
    assert spaced["review_effort"] == "xhigh"

    equals = parse_cli_args(["--review-model=gpt-6", "--review-effort=xhigh"])
    assert equals["review_model"] == "gpt-6"
    assert equals["review_effort"] == "xhigh"

    # The reviewer knobs must not collide with the implementer/fixer --model.
    mixed = parse_cli_args(
        ["--model", "claude-opus-5", "--review-model", "gpt-6", "pr:42"]
    )
    assert mixed["model"] == "claude-opus-5"
    assert mixed["review_model"] == "gpt-6"
    assert mixed["target"] == "pr:42"


@pytest.mark.parametrize(
    "argv",
    [
        # End of argv — nothing to consume.
        ["--review-model"], ["--review-effort"], ["--model"], ["--impl-engine"],
        # Followed by another flag: consuming it would set the value to
        # "--loop" *and* silently drop loop mode.
        ["--review-effort", "--loop", "pr:591"],
        ["--review-model", "--review-effort", "xhigh"],
        ["--model", "--impl-engine", "claude-ds"],
        ["--impl-engine", "--model", "opus"],
        # Explicitly empty: "" is the sentinel for "use the operator default",
        # so accepting it makes an operator typo indistinguishable from intent.
        ["--review-model="], ["--review-effort="], ["--model", ""],
    ],
)
def test_parse_cli_args_rejects_valueless_flags(argv: list[str]) -> None:
    """A flag with no usable value exits rather than swallowing a token."""
    from tick import parse_cli_args  # type: ignore[import-untyped]

    with pytest.raises(SystemExit):
        parse_cli_args(argv)


def test_parse_cli_args_rejects_unknown_permission_mode_value() -> None:
    """A typo'd --permission-mode fails at parse time, not at engine launch.

    Failing late would crash the tick mid-dispatch — after earlier engines
    had already launched. The CLI is the only boundary that can fail before
    any state transition.
    """
    from tick import parse_cli_args  # type: ignore[import-untyped]

    with pytest.raises(SystemExit):
        parse_cli_args(["--permission-mode", "ask"])
    with pytest.raises(SystemExit):
        parse_cli_args(["--permission-mode=ask"])


def test_parse_cli_args_keeps_loop_when_flags_are_well_formed() -> None:
    """The rejection above must not cost the flag/loop/target combination."""
    from tick import parse_cli_args  # type: ignore[import-untyped]

    parsed = parse_cli_args(
        ["--review-effort", "xhigh", "--loop", "pr:591"]
    )

    assert parsed["review_effort"] == "xhigh"
    assert parsed["loop"] is True, "--loop after a value flag must still register"
    assert parsed["target"] == "pr:591"


def test_reviewer_dispatch_forwards_resolved_model_settings(
    tmp_path: Path,
) -> None:
    """The emitted codex argv carries the resolved model and effort.

    Proves the wiring on the production command, not a hand-written shape:
    a changed operator config must reach the engine.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.touches_review_policy") as trp:
                    trp.return_value = False
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_reviewer(
                                        pr_number=42,
                                        pre_label="ai:implemented",
                                        tick_log=None,
                                        review_model="gpt-6-preview",
                                        review_effort="xhigh",
                                    )

    engine_cmd = popen.call_args[0][0]
    codex_argv = engine_cmd[engine_cmd.index("--") + 1:]
    config_overrides = [
        codex_argv[i + 1]
        for i, a in enumerate(codex_argv)
        if a == "-c" and i + 1 < len(codex_argv)
    ]

    assert codex_argv[codex_argv.index("--model") + 1] == "gpt-6-preview", (
        f"resolved model must reach the engine, got: {codex_argv}"
    )
    assert 'model_reasoning_effort="xhigh"' in config_overrides, (
        f"resolved effort must reach the engine, got: {config_overrides}"
    )
    assert not [
        c for c in config_overrides
        if not c.startswith("model_reasoning_effort=")
    ], (
        f"reviewer must forward exactly one -c override (the effort); "
        f"operator sandbox/mcp/hook config must never be splatted through, "
        f"got: {config_overrides}"
    )


def test_reviewer_auth_symlink_follows_codex_home(tmp_path: Path) -> None:
    """The scrub-dir auth symlink sources $CODEX_HOME/auth.json.

    Sibling of the config-path resolution: the reviewer authenticates through
    the symlinked auth.json, so a relocated CODEX_HOME must not leave it
    pointing at a ~/.codex that does not exist.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    codex_home = tmp_path / "relocated"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (codex_home / "config.toml").write_text("")

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
        with mock.patch("tick.WT_ROOT", str(wt_root)):
            with mock.patch("tick.git") as git_mock:
                git_mock.side_effect = reviewer_git(repo_root)
                with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                    "tick.gh_json", return_value=REVIEWER_PR_METADATA,
                ):
                    gh_text_mock.side_effect = ["main", "abc123"]
                    with mock.patch("tick.pipeline.touches_review_policy") as trp:
                        trp.return_value = False
                        with mock.patch("tick.pipeline.atomic_write"):
                            with mock.patch(
                                "tick._publish_marker_or_revert",
                                return_value=True,
                            ):
                                with mock.patch("tick.relabel"):
                                    with mock.patch("subprocess.Popen") as popen:
                                        popen.return_value = proc_mock
                                        _dispatch_one_reviewer(
                                            pr_number=42,
                                            pre_label="ai:implemented",
                                            tick_log=None,
                                        )

    link = wt_root / "pr-42-scrub" / ".codex" / "auth.json"
    assert link.is_symlink(), f"expected an auth symlink at {link}"
    assert os.readlink(link) == str(codex_home / "auth.json"), (
        f"auth symlink must follow CODEX_HOME, got {os.readlink(link)!r}"
    )


def test_reviewer_model_settings_read_from_operator_config(
    tmp_path: Path,
) -> None:
    """Resolver reads model and effort from the operator's codex config.

    The reviewer runs under env -i isolation with HOME/CODEX_HOME pointed at
    a scrub dir, so it can never read ~/.codex/config.toml itself. The tick
    (the trusted parent) reads the two model keys and forwards them as flags.
    """
    from tick import resolve_reviewer_model_settings

    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-5.6-sol"\n'
        'model_reasoning_effort = "xhigh"\n'
    )

    model, effort = resolve_reviewer_model_settings(config_path=str(config))

    assert model == "gpt-5.6-sol", f"expected operator model, got {model!r}"
    assert effort == "xhigh", f"expected operator effort, got {effort!r}"


def test_reviewer_model_settings_read_from_codex_home(tmp_path: Path) -> None:
    """The resolver reads $CODEX_HOME/config.toml, the root codex itself uses.

    `codex` resolves its config root from CODEX_HOME (``--profile`` layers
    ``$CODEX_HOME/<name>.config.toml``), so an operator who relocates it must
    not silently get the pinned fallback instead of their own default.
    """
    from tick import resolve_reviewer_model_settings

    codex_home = tmp_path / "relocated"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-6-relocated"\nmodel_reasoning_effort = "xhigh"\n'
    )

    with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
        assert resolve_reviewer_model_settings() == (
            "gpt-6-relocated", "xhigh"
        ), "resolver must read $CODEX_HOME/config.toml, not ~/.codex"


def test_reviewer_model_settings_fall_back_to_default_codex_home(
    tmp_path: Path,
) -> None:
    """An unset or empty CODEX_HOME falls back to ``~/.codex/config.toml``."""
    from tick import resolve_reviewer_model_settings

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        'model = "gpt-6-home"\nmodel_reasoning_effort = "low"\n'
    )

    for codex_home in ("", None):
        env = {"HOME": str(home)}
        if codex_home is not None:
            env["CODEX_HOME"] = codex_home
        with mock.patch.dict(os.environ, env, clear=True):
            assert resolve_reviewer_model_settings() == (
                "gpt-6-home", "low"
            ), f"CODEX_HOME={codex_home!r} must fall back to ~/.codex"


def test_reviewer_model_settings_fall_back_when_config_unusable(
    tmp_path: Path,
) -> None:
    """Missing, malformed, or wrong-typed config falls back to the pins.

    A broken operator config must not abort dispatch and must not silently
    hand the review to codex's own built-in defaults.
    """
    from tick import (
        _REVIEWER_EFFORT_FALLBACK,
        _REVIEWER_MODEL_FALLBACK,
        resolve_reviewer_model_settings,
    )

    missing = tmp_path / "absent.toml"
    assert resolve_reviewer_model_settings(config_path=str(missing)) == (
        _REVIEWER_MODEL_FALLBACK, _REVIEWER_EFFORT_FALLBACK
    )

    malformed = tmp_path / "malformed.toml"
    malformed.write_text('model = "unterminated\n')
    assert resolve_reviewer_model_settings(config_path=str(malformed)) == (
        _REVIEWER_MODEL_FALLBACK, _REVIEWER_EFFORT_FALLBACK
    )

    invalid_utf8 = tmp_path / "invalid_utf8.toml"
    invalid_utf8.write_bytes(b"\xff\xfe[policy]\n")
    assert resolve_reviewer_model_settings(config_path=str(invalid_utf8)) == (
        _REVIEWER_MODEL_FALLBACK, _REVIEWER_EFFORT_FALLBACK
    ), "invalid UTF-8 must fail soft, never abort dispatch"

    wrong_type = tmp_path / "wrong_type.toml"
    wrong_type.write_text("model = 5\nmodel_reasoning_effort = true\n")
    assert resolve_reviewer_model_settings(config_path=str(wrong_type)) == (
        _REVIEWER_MODEL_FALLBACK, _REVIEWER_EFFORT_FALLBACK
    )

    partial = tmp_path / "partial.toml"
    partial.write_text('model = "gpt-6"\n')
    assert resolve_reviewer_model_settings(config_path=str(partial)) == (
        "gpt-6", _REVIEWER_EFFORT_FALLBACK
    ), "a config with only `model` must still fall back for effort"


def test_reviewer_model_settings_overrides_beat_config(
    tmp_path: Path,
) -> None:
    """Explicit --review-model/--review-effort outrank the operator config."""
    from tick import resolve_reviewer_model_settings

    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "high"\n'
    )

    assert resolve_reviewer_model_settings(
        model_override="gpt-6-preview", config_path=str(config)
    ) == ("gpt-6-preview", "high"), "model override must win, effort inherits"

    assert resolve_reviewer_model_settings(
        effort_override="xhigh", config_path=str(config)
    ) == ("gpt-5.6-sol", "xhigh"), "effort override must win, model inherits"


def test_reviewer_config_read_is_whitelisted_to_model_keys(
    tmp_path: Path,
) -> None:
    """Only model/model_reasoning_effort are ever read from operator config.

    The operator's real config carries [mcp_servers] (a node REPL — arbitrary
    code execution), per-project sandbox_mode/network_access, hooks, and
    notify. None of it may reach the reviewer, whose entire input is
    attacker-controlled PR-head content.
    """
    from tick import _REVIEWER_CONFIG_KEYS, _reviewer_config_str

    assert set(_REVIEWER_CONFIG_KEYS) == {"model", "model_reasoning_effort"}, (
        f"reviewer config whitelist must stay at the two model keys, "
        f"got: {_REVIEWER_CONFIG_KEYS}"
    )

    dangerous = {
        "sandbox_mode": "workspace-write",
        "network_access": "true",
        "approval_policy": "on-request",
        "trust_level": "trusted",
        "notify": "/bin/sh",
    }
    for key, value in dangerous.items():
        with pytest.raises(ValueError):
            _reviewer_config_str({key: value}, key)


def test_reviewer_config_value_shape_is_validated(tmp_path: Path) -> None:
    """A quote/space/newline in a config value is rejected, not forwarded.

    The effort value lands inside a ``-c model_reasoning_effort="<v>"``
    expression; an unvalidated value could close the quote and append a
    second, unreviewed config key.
    """
    from tick import (
        _REVIEWER_EFFORT_FALLBACK,
        _REVIEWER_MODEL_FALLBACK,
        resolve_reviewer_model_settings,
    )

    injected = tmp_path / "injected.toml"
    injected.write_text(
        'model = "gpt-5.6-sol"\n'
        'model_reasoning_effort = """high\\"\\nsandbox_mode = \\"danger"""\n'
    )

    model, effort = resolve_reviewer_model_settings(config_path=str(injected))

    assert model == "gpt-5.6-sol"
    assert effort == _REVIEWER_EFFORT_FALLBACK, (
        f"a value that cannot be a legitimate effort token must fall back, "
        f"got: {effort!r}"
    )
    assert _REVIEWER_MODEL_FALLBACK  # imported pin stays referenced


def test_reviewer_model_accepts_provider_qualified_ids(tmp_path: Path) -> None:
    """A model id with '/' is forwarded, not swallowed by the effort grammar.

    Model is its own argv element, so it never enters the ``-c key="value"``
    TOML expression the effort grammar exists to protect. Custom-provider ids
    legitimately carry '/' and ':'.
    """
    from tick import resolve_reviewer_model_settings

    config = tmp_path / "config.toml"
    config.write_text(
        'model = "openai/gpt-5.1-codex"\nmodel_reasoning_effort = "high"\n'
    )

    model, _ = resolve_reviewer_model_settings(config_path=str(config))

    assert model == "openai/gpt-5.1-codex", (
        f"a provider-qualified model id must reach the engine, got {model!r}"
    )


def test_reviewer_model_rejects_argv_flag_injection(tmp_path: Path) -> None:
    """A model value that codex would read as a flag falls back.

    The model sink is argv, so the shape that matters there is "not a flag":
    a leading dash would inject an option (e.g. the sandbox bypass) into the
    reviewer command line.
    """
    from tick import _REVIEWER_MODEL_FALLBACK, resolve_reviewer_model_settings

    for hostile in (
        "--dangerously-bypass-approvals-and-sandbox",
        "-s danger-full-access",
        "gpt-5.5 --yolo",
        "gpt\n--yolo",
    ):
        config = tmp_path / "hostile.toml"
        config.write_text(f"model = {hostile!r}\nmodel_reasoning_effort = 'high'\n")
        model, _ = resolve_reviewer_model_settings(config_path=str(config))
        assert model == _REVIEWER_MODEL_FALLBACK, (
            f"{hostile!r} must not reach the codex argv, got {model!r}"
        )


def test_reviewer_overrides_are_shape_checked(tmp_path: Path) -> None:
    """Overrides are validated like config values, not trusted blindly.

    The effort override lands in the same ``-c model_reasoning_effort="<v>"``
    expression, so the invariant belongs at the resolver — the one point every
    entry path (CLI flag, ``main()`` kwarg) funnels through.
    """
    from tick import _REVIEWER_MODEL_FALLBACK, resolve_reviewer_model_settings

    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.6-sol"\nmodel_reasoning_effort = "high"\n')

    model, effort = resolve_reviewer_model_settings(
        model_override="--yolo",
        effort_override='high"\nsandbox_mode="danger-full-access',
        config_path=str(config),
    )

    assert effort == "high", (
        f"a malformed effort override must fall through to config, got {effort!r}"
    )
    assert model == "gpt-5.6-sol", (
        f"a flag-shaped model override must fall through to config, got {model!r}"
    )
    assert _REVIEWER_MODEL_FALLBACK  # imported pin stays referenced


def test_reviewer_rejected_value_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Rejecting a value prints why — a silent swap is unattributable.

    The reviewer then runs a different model than the operator's config names,
    and nothing in the run says so.
    """
    from tick import resolve_reviewer_model_settings

    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.6-sol"\nmodel_reasoning_effort = "hi gh"\n')

    resolve_reviewer_model_settings(config_path=str(config))

    err = capsys.readouterr().err
    assert "model_reasoning_effort" in err and "hi gh" in err, (
        f"expected a diagnostic naming the rejected key and value, got {err!r}"
    )


def test_dispatch_one_implementer_creates_named_branch_worktree(
    tmp_path: Path,
) -> None:
    """Implementer worktree must use -b agent/issue-<N>, not --detach.

    A detached HEAD worktree leaves the branch name unspecified — the engine
    may create any branch, but harvest hard-codes ``agent/issue-<N>`` for
    ``gh pr create --head``. Using ``-b`` during worktree creation ensures
    the branch name is canonical from the start.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    git_calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        git_calls.append(args)
        if "--show-toplevel" in args:
            return str(repo_root)
        if "fetch" in args:
            return ""
        if "rev-parse" in args:
            return "abc123"
        return ""

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fake_git):
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel"):
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=None,
                        )

    # Find the worktree add call
    wt_add_calls = [c for c in git_calls if "worktree" in c and "add" in c]
    assert len(wt_add_calls) == 1, f"expected one worktree add call, got {wt_add_calls}"
    wt_call = wt_add_calls[0]
    assert "--detach" not in wt_call, (
        f"worktree must use -b, not --detach: {wt_call}"
    )
    assert "-b" in wt_call, f"worktree must use -b flag: {wt_call}"
    b_idx = list(wt_call).index("-b")
    assert wt_call[b_idx + 1] == "agent/issue-461", (
        f"branch name must be agent/issue-461, got {wt_call[b_idx + 1]}"
    )


def test_dispatch_one_implementer_prompt_specifies_branch_name(
    tmp_path: Path,
) -> None:
    """Implementer prompt must tell the engine the canonical branch name."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel"):
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=None,
                        )

    popen_mock.assert_called_once()
    prompt = (wt_root / "issue-461" / "prompt.txt").read_text()
    assert "agent/issue-461" in prompt, (
        f"prompt must mention canonical branch name, got: {prompt[:200]}"
    )


def test_dispatch_one_implementer_prompt_requires_green_verification_before_sentinel(
    tmp_path: Path,
) -> None:
    """Implementer prompt must require green verification before completion."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel"):
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=None,
                        )

    prompt = (wt_root / "issue-461" / "prompt.txt").read_text()
    assert "the repository's own verification commands" in prompt
    assert "`CLAUDE.md`/`AGENTS.md`" in prompt
    # The pipeline drives repositories it does not own, so the prompt must not
    # name any one stack's toolchain: what to run and in what order is the
    # target repo's own guides to state, and what the engine is *permitted* to
    # run is the operator's `[engine] verify_tools` grant. A stack name that
    # creeps back in here is wrong for every install but one.
    for tool in ("pytest", "ruff", "pyright", "npm", "cargo", "gradle"):
        assert tool not in prompt, (
            f"prompt hard-codes the tool {tool!r}, got: {prompt}"
        )
    assert (
        "Only output IMPLEMENTATION_COMPLETE after every verification command "
        "the repository prescribes passes"
    ) in prompt
    assert "If any verification command cannot be made green" in prompt
    assert "NEEDS_HUMAN_DECISION" in prompt


def test_dispatch_one_implementer_prompt_requests_guideline_pr_body_file(
    tmp_path: Path,
) -> None:
    """Implementer prompt must request a title plus guidelines PR body file."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.return_value = str(repo_root)
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel"):
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=None,
                        )

    prompt = (wt_root / "issue-461" / "prompt.txt").read_text()
    assert ".pr-body.md" in prompt
    assert "first line must be the PR title" in prompt
    for section in (
        "## Summary",
        "## Why this approach",
        "## What changed",
        "## Test coverage",
        "## Test plan",
    ):
        assert section in prompt, (
            f"implementer prompt must require {section!r}, got: {prompt}"
        )
    assert "Closes #461" in prompt
    assert "first content line under `## Summary`" in prompt
    assert "- Problem:" in prompt
    assert "- How this solves the issue:" in prompt
    assert "- Structure:" in prompt
    assert "- Impact:" in prompt
    assert "Do not commit .pr-body.md" in prompt


def test_dispatch_one_implementer_cleans_stale_branch_before_worktree(
    tmp_path: Path,
) -> None:
    """If agent/issue-<N> branch exists from a prior dispatch, worktree creation
    must still succeed. The stale branch should be deleted before ``-b``."""
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    git_calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        git_calls.append(args)
        if "--show-toplevel" in args:
            return str(repo_root)
        if "fetch" in args:
            return ""
        if "rev-parse" in args:
            return "abc123"
        return ""

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fake_git):
            with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                aw_mock.side_effect = lambda path, data: (
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    or Path(path).write_text(data)
                )
                with mock.patch("subprocess.Popen") as popen_mock:
                    proc = mock.MagicMock()
                    proc.pid = 12345
                    popen_mock.return_value = proc
                    with mock.patch("tick.relabel"):
                        _dispatch_one_implementer(
                            issue_number=461,
                            issue_title="PWA icons",
                            tick_log=None,
                        )

    # Must attempt to delete stale branch before worktree add
    branch_delete_calls = [
        c for c in git_calls
        if "branch" in c and "-D" in c and "agent/issue-461" in c
    ]
    assert len(branch_delete_calls) == 1, (
        f"must delete stale branch before worktree add, got git calls: {git_calls}"
    )


# --- Adversarial review: existing-marker custody guard at dispatch ---


def test_dispatch_one_reviewer_skips_when_live_marker_exists(
    tmp_path: Path,
) -> None:
    """A live marker in the PR's worktree must block re-dispatch.

    Crash window: the tick dies between marker publish and the in-flight
    relabel, leaving a live engine plus the pre-dispatch label. The next
    tick's dispatch re-selects the PR; without this guard, the worktree-add
    failure path rmtree's the live engine's marker — destroying custody of
    a spending engine and enabling a duplicate dispatch.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-42"
    wt_dir.mkdir(parents=True)
    marker_path = wt_dir / "dispatch.json"
    marker_path.write_text(json.dumps({
        "engine": "codex",
        "stage": "review",
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": "ai:implemented",
        "dispatch_id": "review-42-live",
        "pid": 12345,
        "start_time": 9999999999.0,
        "pgid": 12345,
    }))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick._classify_marker", return_value=("live", "matched", True),
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                with mock.patch("tick.relabel") as relabel_mock:
                    _dispatch_one_reviewer(
                        pr_number=42,
                        pre_label="ai:implemented",
                        tick_log=None,
                    )

    popen_mock.assert_not_called()
    relabel_mock.assert_not_called()
    assert marker_path.is_file(), (
        "live engine's marker must be retained, not rmtree'd"
    )


def test_dispatch_one_implementer_skips_when_launching_marker_exists(
    tmp_path: Path,
) -> None:
    """A launching lease in the issue's worktree must block re-dispatch.

    A launching lease means a prior spawn's liveness is unprovable — the
    guard must retain custody rather than clean up and double-dispatch.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-461"
    wt_dir.mkdir(parents=True)
    marker_path = wt_dir / "dispatch.json"
    marker_path.write_text(json.dumps({
        "engine": "claude",
        "stage": "implement",
        "pr_number": 461,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "abc",
        "pre_label": "ai:available",
        "dispatch_id": "impl-461-launching",
        "state": "launching",
    }))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick._classify_marker", return_value=("launching", "unknown", False),
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                with mock.patch("tick.relabel") as relabel_mock:
                    _dispatch_one_implementer(
                        issue_number=461,
                        issue_title="PWA icons",
                        tick_log=None,
                    )

    popen_mock.assert_not_called()
    relabel_mock.assert_not_called()
    assert marker_path.is_file()


def test_dispatch_one_fixer_escalates_when_unparseable_marker_exists(
    tmp_path: Path,
) -> None:
    """An unparseable marker retains scratch and escalates for human recovery."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-55"
    wt_dir.mkdir(parents=True)
    marker_path = wt_dir / "dispatch.json"
    marker_path.write_text("{not json")

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("subprocess.Popen") as popen_mock:
            with mock.patch(
                "tick.gh", side_effect=allow_escalation_comment(),
            ), mock.patch("tick.relabel") as relabel_mock:
                _dispatch_one_fixer(pr_number=55, tick_log=None)

    popen_mock.assert_not_called()
    relabel_mock.assert_called_once_with(
        "pr", 55, "ai:changes-requested", "ai:needs-human-decision",
    )
    assert marker_path.is_file()


@pytest.mark.parametrize(
    ("stage", "entity", "pre_label"),
    [
        ("review", "pr", "ai:implemented"),
        ("implement", "issue", "ai:available"),
        ("fix", "pr", "ai:changes-requested"),
    ],
)
def test_dispatch_candidates_escalate_ambiguous_marker_before_admission(
    tmp_path: Path,
    stage: str,
    entity: str,
    pre_label: str,
) -> None:
    """Production dispatch resolves ambiguous custody before global admission."""
    from tick import (  # type: ignore[import-untyped]
        dispatch_fixers,
        dispatch_implementers,
        dispatch_reviewers,
    )

    wt_root = tmp_path / "worktrees"
    prefix = "issue" if entity == "issue" else "pr"
    wt_dir = wt_root / f"{prefix}-42"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(json.dumps({
        "engine": "codex" if stage == "review" else "claude",
        "stage": stage,
        "pr_number": 42,
        "deadline": 9999999999.0,
        "base_ref": "main",
        "base_sha": "abc",
        "head_sha": "def",
        "pre_label": pre_label,
        "dispatch_id": f"{stage}-42-old",
        "state": "launching",
        "unexpected": "field",
    }))

    gh_text = gh_text_for_queue(queue="42", labels=pre_label, body="Issue body")
    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.can_dispatch_now") as admission_mock, mock.patch(
            "tick.gh", side_effect=allow_escalation_comment(),
        ):
            with mock.patch("tick.relabel") as relabel_mock:
                with mock.patch("tick.gh_text", side_effect=gh_text):
                    if stage == "review":
                        with mock.patch(
                            "tick.verify_codex_bin", return_value="/bin/codex",
                        ):
                            dispatch_reviewers(authoritative_in_flight=[])
                    elif stage == "implement":
                        with mock.patch(
                            "tick.gh_json",
                            return_value=[{"number": 42, "title": "Issue", "author": {"login": TRUSTED_AUTHOR}}],
                        ):
                            dispatch_implementers(authoritative_in_flight=[])
                    else:
                        dispatch_fixers(authoritative_in_flight=[])

    admission_mock.assert_not_called()
    relabel_mock.assert_called_once_with(
        entity, 42, pre_label, "ai:needs-human-decision",
    )
    assert wt_dir.is_dir()


def test_dispatch_one_implementer_clears_dead_marker_and_proceeds(
    tmp_path: Path,
) -> None:
    """A provably dead (stale) marker is cleaned up and dispatch proceeds.

    Stale-reverted items leave their marker on disk; dispatch must clear the
    dead scratch and relaunch rather than skipping forever.
    """
    from tick import _dispatch_one_implementer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "issue-461"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dispatch.json").write_text(
        json.dumps({
            "engine": "claude",
            "stage": "implement",
            "pr_number": 461,
            "deadline": 1000.0,
            "base_ref": "main",
            "base_sha": "abc",
            "head_sha": "abc",
            "pre_label": "ai:available",
            "dispatch_id": "impl-461-stale",
            "pid": 99999,
            "start_time": 1000.0,
            "pgid": 99999,
        })
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick._classify_marker", return_value=("stale", "unknown", False),
        ):
            with mock.patch("tick.git") as git_mock:
                git_mock.return_value = str(repo_root)
                with mock.patch("tick.pipeline.atomic_write") as aw_mock:
                    aw_mock.side_effect = lambda path, data: (
                        Path(path).parent.mkdir(parents=True, exist_ok=True)
                        or Path(path).write_text(data)
                    )
                    with mock.patch("subprocess.Popen") as popen_mock:
                        proc = mock.MagicMock()
                        proc.pid = 12345
                        popen_mock.return_value = proc
                        with mock.patch("tick.relabel"):
                            _dispatch_one_implementer(
                                issue_number=461,
                                issue_title="PWA icons",
                                tick_log=None,
                            )

    popen_mock.assert_called_once()
    marker = json.loads((wt_dir / "dispatch.json").read_text())
    assert "dispatch_id" in marker, "fresh marker must replace the dead one"


def test_dispatch_one_fixer_escalates_when_mismatched_identity(
    tmp_path: Path,
) -> None:
    """Confirmed PID reuse retains scratch and routes recovery to a human."""
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_dir = wt_root / "pr-55"
    wt_dir.mkdir(parents=True)
    marker_path = wt_dir / "dispatch.json"
    marker_path.write_text(json.dumps({"pr_number": 55, "stage": "fix"}))

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch(
            "tick._classify_marker", return_value=("stale", "mismatched", False),
        ):
            with mock.patch("subprocess.Popen") as popen_mock:
                with mock.patch(
                    "tick.gh", side_effect=allow_escalation_comment(),
                ), mock.patch("tick.relabel") as relabel_mock:
                    _dispatch_one_fixer(pr_number=55, tick_log=None)

    popen_mock.assert_not_called()
    relabel_mock.assert_called_once_with(
        "pr", 55, "ai:changes-requested", "ai:needs-human-decision",
    )
    assert marker_path.is_file()


# --- load_prompt_template ---


def test_load_prompt_template_reads_file_and_substitutes_variables(
    tmp_path: Path,
) -> None:
    """load_prompt_template reads a .txt file from the prompts dir and
    substitutes {variable} placeholders with the provided values."""
    from tick import load_prompt_template  # type: ignore[import-untyped]

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    template = prompts_dir / "test.txt"
    template.write_text("Hello {name}, PR #{pr_number} is ready.")

    result = load_prompt_template(
        "test", {"name": "Alice", "pr_number": 42},
        prompts_dir=str(prompts_dir),
    )
    assert result == "Hello Alice, PR #42 is ready."


def test_load_prompt_template_raises_on_missing_template(
    tmp_path: Path,
) -> None:
    """load_prompt_template raises FileNotFoundError for a missing template."""
    from tick import load_prompt_template  # type: ignore[import-untyped]

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    with pytest.raises(FileNotFoundError):
        load_prompt_template(
            "nonexistent", {}, prompts_dir=str(prompts_dir),
        )


def test_load_prompt_template_raises_on_missing_variable(
    tmp_path: Path,
) -> None:
    """load_prompt_template raises KeyError when a template variable is not
    provided in the variables dict."""
    from tick import load_prompt_template  # type: ignore[import-untyped]

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    template = prompts_dir / "test.txt"
    template.write_text("Hello {name}, PR #{pr_number}.")

    with pytest.raises(KeyError):
        load_prompt_template("test", {"name": "Alice"}, prompts_dir=str(prompts_dir))


def test_load_prompt_template_defaults_to_prompts_dir() -> None:
    """load_prompt_template defaults to the prompts/ directory inside lib/."""
    from tick import _PROMPTS_DIR  # type: ignore[import-untyped]

    assert _PROMPTS_DIR.endswith(os.path.join("lib", "prompts"))
# --- Harden reviewer scrub dir: move outside candidate worktree (#528) ---


def test_cleanup_scratch_removes_scrub_dir_outside_worktree(
    tmp_path: Path,
) -> None:
    """cleanup_scratch must also remove the reviewer scrub dir when it
    lives outside the worktree (``pr-{N}-scrub`` under WT_ROOT).

    Before #528 the scrub dir was ``wt_dir/.scrub`` and vanished with the
    worktree rmtree.  After moving it to ``WT_ROOT/pr-{N}-scrub``, the
    bare rmtree of wt_dir leaves the scrub dir behind.  cleanup_scratch
    must remove both.
    """
    from tick import cleanup_scratch  # type: ignore[import-untyped]

    wt_dir = tmp_path / "pr-42"
    scrub_dir = tmp_path / "pr-42-scrub"
    codex_home = scrub_dir / ".codex"

    # Simulate what _dispatch_one_reviewer creates: scrub dir inside WT_ROOT
    wt_dir.mkdir()
    codex_home.mkdir(parents=True)
    assert scrub_dir.is_dir()

    cleanup_scratch(str(wt_dir))

    # Worktree must be gone
    assert not wt_dir.exists()
    # Scrub dir must also be gone
    assert not scrub_dir.exists(), (
        f"cleanup_scratch must also remove scrub dir at {scrub_dir}"
    )


def test_reviewer_scrub_dir_is_outside_candidate_worktree(
    tmp_path: Path,
) -> None:
    """The reviewer scrub dir must NOT live inside the candidate worktree.

    When scrub_dir = os.path.join(wt_dir, ".scrub"), a committed symlink at
    that path could theoretically redirect HOME/CODEX_HOME/TMPDIR outside
    the worktree.  The scrub state must be tick-owned — under WT_ROOT
    alongside the worktree, not inside it.

    The trusted-author/trusted-repo gate prevents untrusted candidates from
    reaching dispatch, so this is hardening, not a current exploit.
    """
    from tick import _dispatch_one_reviewer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    proc_mock = mock.MagicMock()
    proc_mock.pid = 12345

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git") as git_mock:
            git_mock.side_effect = reviewer_git(repo_root)
            with mock.patch("tick.gh_text") as gh_text_mock, mock.patch(
                "tick.gh_json", return_value=REVIEWER_PR_METADATA,
            ):
                gh_text_mock.side_effect = ["main", "abc123"]
                with mock.patch("tick.pipeline.touches_review_policy") as trp:
                    trp.return_value = False
                    with mock.patch("tick.pipeline.atomic_write"):
                        with mock.patch(
                            "tick._publish_marker_or_revert",
                            return_value=True,
                        ):
                            with mock.patch("tick.relabel"):
                                with mock.patch(
                                    "subprocess.Popen"
                                ) as popen:
                                    popen.return_value = proc_mock
                                    _dispatch_one_reviewer(
                                        pr_number=42,
                                        pre_label="ai:implemented",
                                        tick_log=None,
                                    )

    popen.assert_called_once()
    env = popen.call_args[1].get("env", {})
    scrub_home = env.get("HOME")
    assert scrub_home is not None, "scrub HOME must be set"

    wt_dir = wt_root / "pr-42"
    # The scrub dir must NOT be a subdirectory of the worktree.
    assert not str(scrub_home).startswith(str(wt_dir) + os.sep), (
        f"scrub HOME ({scrub_home}) must not be inside wt_dir ({wt_dir})"
    )
    assert not str(scrub_home).startswith(str(wt_dir) + "/"), (
        f"scrub HOME ({scrub_home}) must not be inside wt_dir ({wt_dir})"
    )

    # The scrub dir must be under WT_ROOT (tick-owned path).
    assert str(scrub_home).startswith(str(wt_root) + os.sep), (
        f"scrub HOME ({scrub_home}) must be under WT_ROOT ({wt_root})"
    )

    # Verify the distinct scrub directory was created (not inside wt_dir).
    assert os.path.isdir(scrub_home), (
        f"scrub dir {scrub_home} must exist on disk"
    )


# --- Helpers ---


# PR title/body payload the tick's authenticated gh_json read returns before
# a reviewer launch; injected into the prompt as delimited context.
REVIEWER_PR_METADATA: dict[str, str] = {
    "title": "feat: reviewed change",
    "body": "PR body context for the reviewer.",
}


def gh_text_for_queue(
    *, queue: str, labels: str, body: str = "",
) -> Callable[..., str]:
    """Return a ``gh_text`` side effect for a dispatch queue of one candidate.

    Dispatch makes three ``gh_text`` read shapes per candidate, and the
    eligibility gate's point-read of the complete label set is the one a bare
    ``return_value`` silently mis-serves: a body string read as a label set
    carries no pickup label, so the candidate is skipped and the test passes
    for the wrong reason.

    Args:
        queue: Newline-separated candidate numbers the pickup query returns.
        labels: Newline-separated label names the point-read returns.
        body: Payload for every other read (the issue body).

    Returns:
        A side effect dispatching on the argv shape.
    """
    def side_effect(*args: str, **_kwargs: object) -> str:
        if ".labels[].name" in args:
            return labels
        if "list" in args:
            return queue
        return body

    return side_effect


def reviewer_git(repo_root: Path) -> Callable[..., str]:
    """Return a git mock that materializes reviewer worktree-add paths."""
    def side_effect(*args: str) -> str:
        if "worktree" in args and "add" in args:
            Path(args[-2]).mkdir(parents=True, exist_ok=True)
        return str(repo_root)

    return side_effect


FIXER_PROMPT_PR_BODY = "## Summary\nCloses #7\n\n## What changed\n- Structure: x\n"
FIXER_PROMPT_ISSUE: dict[str, object] = {
    "number": 7,
    "title": "feat: keep the custody marker durable",
    "body": "## Scope\n1. The marker survives a failed relabel.\n",
}
FIXER_PROMPT_ISSUES: tuple[dict[str, object], ...] = (FIXER_PROMPT_ISSUE,)


def bare_implementer_git(repo_root: Path) -> Callable[..., str]:
    """Return a ``tick.git`` double whose worktree carries no skill docs.

    ``worktree add`` creates only the empty directory, so any skill the
    implementer finds must come from the install rather than the checkout.

    Args:
        repo_root: The directory reported as the checkout root.

    Returns:
        A side effect for ``mock.patch("tick.git", side_effect=...)``.
    """
    def fake_git(*args: str) -> str:
        if "worktree" in args and "add" in args:
            Path(args[-2]).mkdir(parents=True, exist_ok=True)
            return ""
        if "rev-parse" in args and "--show-toplevel" not in args:
            return "abc123"
        return str(repo_root)

    return fake_git


def render_fixer_prompt(
    tmp_path: Path,
    pr_number: int = 42,
    *,
    pr_body: str = FIXER_PROMPT_PR_BODY,
    issues: Sequence[dict[str, object]] = FIXER_PROMPT_ISSUES,
) -> str:
    """Drive ``_dispatch_one_fixer`` and return the prompt it handed the engine.

    Renders through the production dispatch path rather than
    ``load_prompt_template`` directly, so a substitution key the template needs
    and the render site never supplies fails here instead of in production.

    Args:
        tmp_path: The test's temp directory; hosts the worktree and checkout.
        pr_number: The PR number to dispatch a fixer for.
        pr_body: The PR body dispatch reads; its ``Closes`` line is what GitHub
            would resolve into ``closingIssuesReferences``.
        issues: The linked issue payloads in link order; empty for a PR that
            closes none.

    Returns:
        The full text of ``prompt.txt`` as written into the fixer worktree.
    """
    from tick import _dispatch_one_fixer  # type: ignore[import-untyped]

    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with mock.patch("tick.WT_ROOT", str(wt_root)):
        with mock.patch("tick.git", side_effect=fixer_git(repo_root)):
            with mock.patch("tick.gh_text") as gh_text_mock:
                gh_text_mock.side_effect = ["main", "abc123", "fix-branch"]
                with mock.patch(
                    "tick.gh_json",
                    side_effect=fixer_gh_json(pr_body=pr_body, issues=issues),
                ):
                    with mock.patch(
                        "tick._publish_marker_or_revert", return_value=True,
                    ):
                        with mock.patch("tick.relabel"):
                            with mock.patch("subprocess.Popen") as popen:
                                proc_mock = mock.MagicMock()
                                proc_mock.pid = 12345
                                popen.return_value = proc_mock
                                _dispatch_one_fixer(
                                    pr_number=pr_number, tick_log=None,
                                )

    popen.assert_called_once()
    return (wt_root / f"pr-{pr_number}" / "prompt.txt").read_text()

# Flags of bare `codex exec` that consume the next argv element as a value
# (from `codex exec --help` on codex-cli 0.142.5). Everything else starting
# with "-" is a boolean flag.
CODEX_EXEC_VALUE_FLAGS = {
    "-c", "--config", "--enable", "--disable", "-i", "--image",
    "-m", "--model", "--local-provider", "-p", "--profile",
    "-s", "--sandbox", "-C", "--cd", "--add-dir", "--output-schema",
    "--color", "-o", "--output-last-message",
}


def codex_positionals(codex_argv: list[str]) -> list[str]:
    """Return the positional args in a bare ``codex exec`` argv.

    Walks the argv after the ``codex exec`` prefix, skipping the value of
    every flag known to take one, so a positional that follows a boolean
    flag (e.g. ``--ignore-user-config <prompt>``) is detected correctly.
    """
    positionals: list[str] = []
    i = 2  # skip "codex exec"
    while i < len(codex_argv):
        arg = codex_argv[i]
        if arg in CODEX_EXEC_VALUE_FLAGS:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            positionals.append(arg)
            i += 1
    return positionals


ENGINE_SETTINGS = '{"sandbox":{"enabled":false}}'


def configure_engine_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: str,
) -> None:
    """Point the resolver at a config giving the stubbed target ``settings``."""
    config_path = tmp_path / "operator.toml"
    config_path.write_text(
        f'[operator]\nlogin = "{TRUSTED_AUTHOR}"\n'
        f'\n[repos."{TRUSTED_AUTHOR}/test-repo"]\n'
        f"engine_settings = '{settings}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(config_path))


def write_through(path: str, data: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(data)


# --- Operator-configured verification permissions ---


def test_engine_verify_tools_default_to_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Fresh installs grant no target-specific verification commands."""
    import pipeline
    import tick

    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(tmp_path / "absent.toml"))
    # The suite points the floor at a nonexistent file for hermeticity; this
    # test is specifically about what ships, so it reads the real one.
    monkeypatch.setattr(
        pipeline, "_PACKAGED_CONFIG_PATH",
        Path(pipeline.__file__).resolve().parent.parent / "config.toml",
    )

    tools = tick._engine_verify_tools()

    assert tools == tuple(
        f"Bash({entry})" for entry in pipeline.resolve_config().verify_tools
    )
    assert tools == ()
    assert all(entry.startswith("Bash(") and entry.endswith(")") for entry in tools)


def test_engine_verify_tools_honour_an_explicitly_empty_grant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``verify_tools = []`` grants nothing — it does not reopen the floor.

    Narrowing a permission grant for one target is the direction that matters
    for a permission grant, and a fallback beneath the resolved value is
    exactly the thing an operator cannot override.
    """
    import tick

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[defaults]\nverify_tools = ["cargo test *"]\n'
        '\n[repos."owner/name"]\nverify_tools = []\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    assert tick._engine_verify_tools("owner/name") == ()
    assert tick._engine_verify_tools("owner/other") == ("Bash(cargo test *)",)


def test_engine_allowed_tools_invents_no_grant_the_operator_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The launch argv is where an over-wide grant would actually do harm.

    Resolving the empty list correctly is not the contract; what reaches
    ``--allowedTools`` is. This is the consumer, so it is tested at the
    consumer — with no double space where the absent grant used to sit.
    """
    import tick

    cfg = tmp_path / "config.toml"
    cfg.write_text("[defaults]\nverify_tools = []\n", encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    allowed = tick._engine_allowed_tools(tick._implementer_push_permission())

    assert "pytest" not in allowed
    assert "npm" not in allowed
    assert "  " not in allowed
    assert allowed == (
        f"{tick._ENGINE_ALLOWED_TOOLS_BASE} {tick._implementer_push_permission()}"
    )


def test_engine_verify_tools_come_from_operator_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A configured list replaces the default wholesale, not additively.

    An operator on a non-Python stack must be able to *remove* the Python
    grants, not merely add to them: the allowlist is a security boundary, and a
    grant nobody needs is a grant nobody audits.
    """
    import tick

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[defaults]\nverify_tools = ["cargo test *", "cargo clippy *"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    tools = tick._engine_verify_tools()

    assert tools == ("Bash(cargo test *)", "Bash(cargo clippy *)")
    assert not any("pytest" in entry for entry in tools)


def test_engine_verify_tools_refuse_an_entry_that_escapes_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An entry containing a parenthesis is a startup error, not a filter.

    The flag takes a space-separated list of ``Tool(body)`` entries, so a ``)``
    inside a body closes its entry early and every character after it is parsed
    as a *new* permission. Dropping the bad entry and dispatching on what is
    left would hand the engine a narrower grant than the operator wrote and
    surface as an unexplained permission denial mid-run, so the resolver
    reports it and the tick refuses to start.
    """
    import pipeline
    import tick

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults]\n"
        'verify_tools = ["cargo test *", "rm -rf /) Bash(git push --force"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    config = pipeline.resolve_config()

    assert any("verify_tools" in e for e in config.errors)
    assert tick._engine_verify_tools() == ()


def test_engine_verify_tools_refuse_when_every_entry_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An all-invalid list is refused, not read as a deliberate empty grant.

    ``verify_tools = []`` is an operator saying "verify nothing". A list whose
    every entry was rejected says only that the file has a typo, and resolving
    the two to the same state dispatches unverified work that can still be
    promoted to ai:ready-for-human.
    """
    import pipeline
    import tick

    cfg = tmp_path / "config.toml"
    cfg.write_text('[defaults]\nverify_tools = ["bad)entry"]\n', encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))

    config = pipeline.resolve_config()

    assert config.errors, "an all-invalid grant must not start the tick"
    assert tick._engine_verify_tools() == ()


def test_engine_verify_tools_guard_a_body_that_appears_after_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The argv builder never templates an unvalidated body, validated or not.

    The config file is re-read per dispatch, so an operator edit landing after
    the startup check would otherwise reach ``--allowedTools`` unexamined. The
    allowlist is the security boundary; the last word on it belongs next to the
    argv rather than to a check that ran earlier.
    """
    from types import SimpleNamespace

    import pipeline
    import tick

    escaped = "rm -rf /) Bash(git push --force"
    monkeypatch.setattr(
        pipeline, "resolve_config",
        lambda *a, **k: SimpleNamespace(verify_tools=("cargo test *", escaped)),
    )

    tools = tick._engine_verify_tools()

    assert tools == ("Bash(cargo test *)",)
    assert not any("--force" in entry for entry in tools)


def test_engine_allowed_tools_carries_the_verification_grant() -> None:
    """The composed allowlist must contain the verification permissions.

    The prompts no longer name a stack, so this grant is the only thing that
    lets an engine run its repo's tests at all. A regression here is silent
    until a paid engine is halfway through a dispatch.
    """
    import tick

    allowed = tick._engine_allowed_tools(tick._implementer_push_permission())

    for entry in tick._engine_verify_tools():
        assert entry in allowed, f"allowlist is missing the grant {entry!r}"
    assert tick._implementer_push_permission() in allowed
    # The git surface stays exact: no wildcard push crept in alongside.
    assert "Bash(git push *)" not in allowed
