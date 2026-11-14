"""Tests for the PR-pipeline review-stage helper logic.

Co-located with ``pipeline.py`` so the whole ``tick/lib`` directory lifts
out cleanly against any target repository. Run with::

    uv run pytest .claude/skills/tick/lib/test_pipeline.py
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
from pathlib import Path

import pipeline
import pytest

# Slack for the fixer comment's own framing — marker, headline, outcome lines,
# and the details/fence wrapper — on top of the bounded engine excerpt. The
# bound itself is read from ``pipeline`` rather than restated, so a change to
# the publish budget moves these assertions with it instead of either breaking
# them (budget raised past a hardcoded ceiling) or hollowing them out (budget
# lowered, leaving a ceiling nothing could reach).
_FIX_COMMENT_FRAMING = 500


# --- decide_loop_cadence ---


def test_loop_cadence_in_flight_sleeps_until_nearest_deadline_when_soon() -> None:
    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=1,
        nearest_engine_deadline=1090.0,
        actionable_queue=False,
        at_cap=False,
        quiescent=False,
        consecutive_quiescent=0,
        now=1000.0,
    )

    assert sleep_seconds == 90
    assert stop is False


def test_loop_cadence_actionable_queue_uses_cache_warm_band() -> None:
    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=0,
        nearest_engine_deadline=None,
        actionable_queue=True,
        at_cap=False,
        quiescent=False,
        consecutive_quiescent=0,
        now=1000.0,
    )

    assert sleep_seconds == 270
    assert stop is False


def test_loop_cadence_in_flight_clamps_to_cache_warm_band() -> None:
    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=1,
        nearest_engine_deadline=1500.0,
        actionable_queue=False,
        at_cap=False,
        quiescent=False,
        consecutive_quiescent=0,
        now=1000.0,
    )

    assert sleep_seconds == 270
    assert stop is False


def test_loop_cadence_quiescent_uses_heartbeat_and_stops_after_limit() -> None:
    sleep_seconds, stop = pipeline.decide_loop_cadence(
        in_flight_count=0,
        nearest_engine_deadline=None,
        actionable_queue=False,
        at_cap=False,
        quiescent=True,
        consecutive_quiescent=3,
        now=1000.0,
    )

    assert sleep_seconds == 1800
    assert stop is True


@pytest.mark.parametrize("state, expected", [
    # In flight: the nearest deadline, capped by the max poll.
    ({"in_flight_count": 1, "nearest_engine_deadline": 1500.0}, (200, False)),
    ({"in_flight_count": 1, "nearest_engine_deadline": 1150.0}, (150, False)),
    # Queued work and a full cap both wake at the short poll.
    ({"actionable_queue": True}, (120, False)),
    ({"at_cap": True}, (120, False)),
    # Quiescent: the heartbeat, until the configured stop count.
    ({"quiescent": True, "consecutive_quiescent": 4}, (3600, False)),
    ({"quiescent": True, "consecutive_quiescent": 5}, (3600, True)),
])
def test_loop_cadence_paces_from_the_configured_values(
    state: dict[str, object], expected: tuple[int, bool],
) -> None:
    """The decision helper uses the cadence it is given, not the constants."""
    inputs: dict[str, object] = {
        "in_flight_count": 0, "nearest_engine_deadline": None,
        "actionable_queue": False, "at_cap": False, "quiescent": False,
        "consecutive_quiescent": 0, **state,
    }

    decision = pipeline.decide_loop_cadence(
        **inputs,  # type: ignore[arg-type]
        now=1000.0,
        short_poll_seconds=120, max_poll_seconds=200,
        heartbeat_seconds=3600, quiescent_stop_after=5,
    )

    assert decision == expected


def test_compute_next_wake_retries_unknown_state_at_the_short_poll() -> None:
    """An unreadable state is retried on the configured short poll."""
    decision = pipeline.compute_next_wake(
        in_flight_count=None, short_poll_seconds=90,
        max_poll_seconds=200, heartbeat_seconds=3600,
    )

    assert decision.next_wake_seconds == 90
    assert decision.stop is False


def test_compute_next_wake_steps_off_the_cache_cliff_while_in_flight() -> None:
    """A deadline exactly at the TTL cliff never becomes the wake."""
    decision = pipeline.compute_next_wake(
        in_flight_count=1, nearest_deadline_s=300, has_actionable_queue=False,
        at_capacity=False, max_poll_seconds=600,
    )

    assert decision.next_wake_seconds < 300


# --- validate_review ---


def test_validate_review_accepts_ready_for_human_with_empty_findings() -> None:
    assert (
        pipeline.validate_review({"verdict": "ready_for_human", "findings": []})
        == "ready_for_human"
    )


def test_validate_review_rejects_missing_findings() -> None:
    # ``findings`` is required by review-schema.json; a payload missing it did not
    # go through the schema-constrained reviewer path, so the promote-safety guard
    # must reject it (the caller then applies the conservative changes_requested
    # default) rather than promote on malformed/truncated output.
    assert pipeline.validate_review({"verdict": "ready_for_human"}) is None


def test_validate_review_accepts_changes_requested_with_findings() -> None:
    obj = {
        "verdict": "changes_requested",
        "findings": [
            {"severity": "blocker", "file": "app/x.py", "summary": "SQL injection"},
        ],
    }
    assert pipeline.validate_review(obj) == "changes_requested"


def test_validate_review_rejects_unknown_verdict() -> None:
    assert pipeline.validate_review({"verdict": "lgtm"}) is None


def test_validate_review_rejects_missing_verdict() -> None:
    assert pipeline.validate_review({"findings": []}) is None


def test_validate_review_rejects_non_object() -> None:
    assert pipeline.validate_review(["ready_for_human"]) is None
    assert pipeline.validate_review(None) is None


def test_validate_review_rejects_malformed_findings() -> None:
    # findings present but not a list
    assert pipeline.validate_review({"verdict": "changes_requested", "findings": {}}) is None
    # a finding missing required keys
    bad = {"verdict": "changes_requested", "findings": [{"severity": "blocker"}]}
    assert pipeline.validate_review(bad) is None
    # a finding with an unknown severity
    bad_sev = {
        "verdict": "changes_requested",
        "findings": [{"severity": "nit", "file": "a", "summary": "b"}],
    }
    assert pipeline.validate_review(bad_sev) is None


def test_validate_review_rejects_unhashable_verdict() -> None:
    """A list/dict verdict must return None, never crash with TypeError."""
    assert pipeline.validate_review({"verdict": ["ready_for_human"], "findings": []}) is None
    assert pipeline.validate_review({"verdict": {}, "findings": []}) is None


def test_validate_review_rejects_unhashable_severity() -> None:
    """A list/dict severity must return None, never crash with TypeError."""
    obj = {
        "verdict": "changes_requested",
        "findings": [{"severity": ["blocker"], "file": "a.py", "summary": "b"}],
    }
    assert pipeline.validate_review(obj) is None


# --- has_blocking_findings ---


def test_has_blocking_findings_true_for_blocker() -> None:
    assert pipeline.has_blocking_findings([{"severity": "blocker", "file": "a", "summary": "b"}])


def test_has_blocking_findings_true_for_major() -> None:
    assert pipeline.has_blocking_findings([{"severity": "major", "file": "a", "summary": "b"}])


def test_has_blocking_findings_false_for_minor_only() -> None:
    assert not pipeline.has_blocking_findings([{"severity": "minor", "file": "a", "summary": "b"}])


def test_has_blocking_findings_false_for_empty() -> None:
    assert not pipeline.has_blocking_findings([])


def test_has_blocking_findings_tolerates_malformed_entries() -> None:
    # A non-dict entry must not crash the gate; it simply is not a blocking finding.
    mixed = ["oops", {"severity": "minor", "file": "", "summary": ""}]
    assert not pipeline.has_blocking_findings(mixed)


# --- head_advanced ---


def test_head_unchanged_is_not_advanced() -> None:
    assert pipeline.head_advanced("h1", "h1") is False


def test_head_moved_is_advanced() -> None:
    assert pipeline.head_advanced("h1", "h2") is True


def test_head_advanced_treats_unresolved_current_as_moved() -> None:
    # A head we pinned but can no longer resolve must be treated as drift.
    assert pipeline.head_advanced("h1", "") is True


def test_head_advanced_skips_when_no_head_pinned() -> None:
    # With no pinned head (unit-test convenience; production always pins) the gate
    # cannot assert drift, so it returns False rather than fabricating a discard.
    assert pipeline.head_advanced("", "") is False
    assert pipeline.head_advanced("", "h2") is False


# --- review_already_posted ---


def test_review_already_posted_true_when_round_token_present() -> None:
    body = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[],
        base_sha="b1",
        head_sha="h1",
    )
    assert pipeline.review_already_posted([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}], "b1", "h1", trusted_author="test-owner") is True


def test_review_already_posted_false_for_different_head() -> None:
    body = pipeline.render_review_comment(
        verdict="changes_requested", findings=[], base_sha="b1", head_sha="h1"
    )
    assert pipeline.review_already_posted([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}], "b1", "h2", trusted_author="test-owner") is False


def test_review_already_posted_false_for_same_head_different_base() -> None:
    # A base that advanced under an unchanged head is a new round, not a replay.
    body = pipeline.render_review_comment(
        verdict="changes_requested", findings=[], base_sha="b1", head_sha="h1"
    )
    assert pipeline.review_already_posted([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}], "b2", "h1", trusted_author="test-owner") is False


def test_review_already_posted_false_when_no_marked_comments() -> None:
    assert pipeline.review_already_posted([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": "just a human note"}], "b1", "h1", trusted_author="test-owner") is False


# --- count_review_rounds ---


def test_count_review_rounds_counts_only_marked_comments() -> None:
    comments = [
        {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": "random human comment"},
        {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": f"{pipeline.REVIEW_MARKER}\n\nround 1 findings"},
        {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": "another human note"},
        {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": f"{pipeline.REVIEW_MARKER}\n\nround 2 findings"},
    ]
    assert pipeline.count_review_rounds(comments, pipeline.REVIEW_MARKER, trusted_author="test-owner") == 2


def test_count_review_rounds_zero_when_no_marked_comments() -> None:
    assert pipeline.count_review_rounds([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": "hi"}], pipeline.REVIEW_MARKER, trusted_author="test-owner") == 0


def test_count_review_rounds_rejects_missing_or_nonstring_body() -> None:
    comments = [{}, {"body": None}, {"body": pipeline.REVIEW_MARKER}]
    with pytest.raises(ValueError):
        pipeline.count_review_rounds(comments, trusted_author="test-owner")


def test_at_max_iterations_routes_to_max_iterations_label() -> None:
    # Default threshold is 5: the 5th completed round is the cap.
    assert pipeline.at_max_iterations(4) is False
    assert pipeline.at_max_iterations(5) is True
    assert pipeline.at_max_iterations(6) is True


# --- base_advanced ---


def test_base_unchanged_is_not_advanced() -> None:
    assert pipeline.base_advanced("abc123", "abc123") is False


def test_base_moved_is_advanced() -> None:
    assert pipeline.base_advanced("abc123", "def456") is True


def test_base_advanced_treats_unknown_tip_as_moved() -> None:
    # A tip we could not resolve must be treated as drift, never as "safe".
    assert pipeline.base_advanced("abc123", "") is True


# --- classify_dispatch ---


def test_classify_stale_when_bare_result_without_envelope() -> None:
    """A result file without a valid envelope is not trustworthy — treat as stale."""
    assert classify(now=100, deadline=50, alive=False, result=True) == "stale"


def test_classify_live_when_result_but_pid_alive() -> None:
    # A result file alongside a still-running PID means codex may be mid-write — wait
    # for the engine to exit rather than harvest a partially written review.json.
    assert classify(now=10, deadline=50, alive=True, result=True) == "live"


def test_classify_launching_lease_is_not_stale() -> None:
    # A `launching` lease (marker written pre-spawn, no PID published yet) must NOT
    # read as stale — that would re-dispatch a live reviewer. With no result it stays
    # `launching` so reconcile can escalate instead of silently re-queueing.
    assert (
        pipeline.classify_dispatch(
            now=10, deadline=50, pid_alive=False, result_exists=False, state="launching"
        )
        == "launching"
    )


def test_classify_launching_with_bare_result_is_still_launching() -> None:
    # A bare result without a valid envelope does not prove completion.
    assert (
        pipeline.classify_dispatch(
            now=10, deadline=50, pid_alive=False, result_exists=True, state="launching"
        )
        == "launching"
    )


def test_classify_launching_with_envelope_is_complete() -> None:
    # A valid envelope proves the wrapper confirmed exit+schema.
    assert (
        pipeline.classify_dispatch(
            now=10, deadline=50, pid_alive=False, result_exists=True,
            state="launching", envelope_valid=True,
        )
        == "complete"
    )


def test_classify_live_when_running_within_deadline() -> None:
    assert classify(now=10, deadline=50, alive=True, result=False) == "live"


def test_classify_stale_when_dead_with_no_result() -> None:
    assert classify(now=10, deadline=50, alive=False, result=False) == "stale"


def test_classify_failed_when_dead_with_bound_failure_envelope() -> None:
    """A wrapper-confirmed reviewer failure is terminal, not stale/requeueable."""
    assert (
        pipeline.classify_dispatch(
            now=10,
            deadline=50,
            pid_alive=False,
            result_exists=False,
            failure_envelope_valid=True,
        )
        == "failed"
    )


def test_classify_hung_when_alive_past_deadline() -> None:
    assert classify(now=99, deadline=50, alive=True, result=False) == "hung"


# --- atomic_write ---


def test_atomic_write_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "dispatch.json"
    pipeline.atomic_write(str(target), json.dumps({"pid": 42}))
    assert json.loads(target.read_text()) == {"pid": 42}


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "dispatch.json"
    pipeline.atomic_write(str(target), "x")
    # Only the final file should remain — no orphaned temp alongside it.
    assert [p.name for p in tmp_path.iterdir()] == ["dispatch.json"]


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "iter.txt"
    pipeline.atomic_write(str(target), "1")
    pipeline.atomic_write(str(target), "2")
    assert target.read_text() == "2"


# --- render_review_comment ---


def test_render_review_comment_embeds_marker_and_base_sha() -> None:
    body = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[{"severity": "blocker", "file": "app/x.py", "summary": "leak"}],
        base_sha="abc123",
        head_sha="head789",
    )
    assert body.startswith(pipeline.REVIEW_MARKER)
    assert "abc123" in body
    assert "head789" in body
    assert "app/x.py" in body
    assert "leak" in body
    assert "blocker" in body.lower()
    # The marker must be present so count_review_rounds tallies this round.
    assert pipeline.count_review_rounds([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}], trusted_author="test-owner") == 1
    # The round token must be present so re-runs are idempotent.
    assert pipeline.review_already_posted([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}], "abc123", "head789", trusted_author="test-owner") is True


def test_render_review_comment_handles_clean_review() -> None:
    body = pipeline.render_review_comment(
        verdict="ready_for_human", findings=[], base_sha="abc123", head_sha="head789"
    )
    assert body.startswith(pipeline.REVIEW_MARKER)
    assert "abc123" in body
    assert pipeline.count_review_rounds([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}], trusted_author="test-owner") == 1


def test_render_review_comment_embeds_diff_base_and_freshness_tip() -> None:
    # The two-SHA model: base_sha is the merge-base the diff was actually anchored
    # to; base_tip_sha is the base tip snapshot the freshness guard compared. The
    # audit record must carry both honestly — claiming the tip was "the reviewed
    # range" when Codex diffed from the merge-base misstates what was reviewed.
    body = pipeline.render_review_comment(
        verdict="ready_for_human",
        findings=[],
        base_sha="mergebase111",
        base_tip_sha="basetip222",
        head_sha="head333",
    )
    assert "diff base `mergebase111`" in body
    assert "base tip `basetip222`" in body
    assert "head `head333`" in body


def test_review_already_posted_keys_on_diff_base_tip_and_head() -> None:
    # The hidden idempotency token covers the full (diff base, base tip, head)
    # triple: a re-dispatch that changes ANY of the three is a new round.
    body = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[],
        base_sha="mb1",
        base_tip_sha="tip1",
        head_sha="h1",
    )
    comments = [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}]
    assert pipeline.review_already_posted(
        comments, "mb1", "h1", base_tip_sha="tip1",
        trusted_author="test-owner",
    ) is True
    assert pipeline.review_already_posted(
        comments, "mb2", "h1", base_tip_sha="tip1",
        trusted_author="test-owner",
    ) is False
    assert pipeline.review_already_posted(
        comments, "mb1", "h1", base_tip_sha="tip2",
        trusted_author="test-owner",
    ) is False
    assert pipeline.review_already_posted(
        comments, "mb1", "h2", base_tip_sha="tip1",
        trusted_author="test-owner",
    ) is False


def test_review_already_posted_keys_on_base_ref() -> None:
    # The base branch *name* is part of review identity (a retarget discards
    # even when every SHA is unchanged), so it must be part of the replay
    # token too: base A and base B can point to the same commit, and a
    # comment posted under A must not suppress the fresh review under B.
    body = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[],
        base_sha="mb1",
        base_tip_sha="tip1",
        head_sha="h1",
        base_ref="main",
    )
    comments = [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}]
    assert pipeline.review_already_posted(
        comments, "mb1", "h1", base_tip_sha="tip1", base_ref="main",
        trusted_author="test-owner",
    ) is True
    assert pipeline.review_already_posted(
        comments, "mb1", "h1", base_tip_sha="tip1", base_ref="develop",
        trusted_author="test-owner",
    ) is False


def test_review_already_posted_base_ref_legacy_fallback() -> None:
    # Comments posted before the base-ref key carry the three-part token.
    # An empty ref keeps the legacy format so legacy lookups still match,
    # and the two formats never match each other (a cross-format replay
    # re-posts once — the accepted one-time upgrade edge).
    legacy_body = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[],
        base_sha="mb1",
        base_tip_sha="tip1",
        head_sha="h1",
    )
    ref_body = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[],
        base_sha="mb1",
        base_tip_sha="tip1",
        head_sha="h1",
        base_ref="main",
    )
    assert pipeline.review_already_posted(
        [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": legacy_body}], "mb1", "h1", base_tip_sha="tip1",
        trusted_author="test-owner",
    ) is True
    assert pipeline.review_already_posted(
        [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": legacy_body}], "mb1", "h1", base_tip_sha="tip1",
        base_ref="main",
        trusted_author="test-owner",
    ) is False
    assert pipeline.review_already_posted(
        [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": ref_body}], "mb1", "h1", base_tip_sha="tip1",
        trusted_author="test-owner",
    ) is False


# --- decide_review_harvest (composition) ---


def test_decide_posts_fresh_review_when_same_shas_new_base_ref() -> None:
    # Regression (review finding): PR retargeted from feat-1 to main where
    # both point at the same commit — identical (diff base, base tip, head)
    # triple. The old round's comment must NOT suppress the fresh round's
    # comment: post_comment stays True so the new findings become the posted
    # handoff the next fixer reads, instead of the stale feat-1 review.
    old_comment = pipeline.render_review_comment(
        verdict="ready_for_human",
        findings=[],
        base_sha="mb1",
        base_tip_sha="tip1",
        head_sha="h1",
        base_ref="feat-1",
    )
    d = pipeline.decide_review_harvest(
        review_obj=changes_review(),
        comments=[{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": old_comment}],
        pinned_base="tip1",
        current_base="tip1",
        pinned_head="h1",
        current_head="h1",
        diff_base_sha="mb1",
        pinned_base_ref="main",
        current_base_ref="main",
        trusted_author="test-owner",
    )
    assert d["action"] == "request_changes"
    assert d["label"] == "ai:changes-requested"
    assert d["post_comment"] is True
    body = d["comment_body"]
    assert isinstance(body, str) and "boom" in body
    # The fresh comment carries the new base-ref identity, so a crash-window
    # replay of THIS round is still recognised as already posted.
    assert pipeline.review_already_posted(
        [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": old_comment}, {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}],
        "mb1", "h1", base_tip_sha="tip1", base_ref="main",
        trusted_author="test-owner",
    ) is True


def test_decide_replay_suppressed_for_same_base_ref() -> None:
    # Same identity including the ref: an interrupted tick re-reading the
    # same result must not re-post or double-count the round.
    posted = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[{"severity": "blocker", "file": "a.py", "summary": "boom"}],
        base_sha="mb1",
        base_tip_sha="tip1",
        head_sha="h1",
        base_ref="main",
    )
    d = pipeline.decide_review_harvest(
        review_obj=changes_review(),
        comments=[{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": posted}],
        pinned_base="tip1",
        current_base="tip1",
        pinned_head="h1",
        current_head="h1",
        diff_base_sha="mb1",
        pinned_base_ref="main",
        current_base_ref="main",
        trusted_author="test-owner",
    )
    assert d["post_comment"] is False


def test_decide_promotes_clean_review() -> None:
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(), comments=[], pinned_base="b1", current_base="b1"
    , trusted_author="test-owner")
    assert d["action"] == "promote"
    assert d["label"] == "ai:ready-for-human"
    assert d["post_comment"] is True
    assert d["base_drift"] is False


def test_decide_requests_changes() -> None:
    d = pipeline.decide_review_harvest(
        review_obj=changes_review(), comments=[], pinned_base="b1", current_base="b1"
    , trusted_author="test-owner")
    assert d["action"] == "request_changes"
    assert d["label"] == "ai:changes-requested"
    assert d["post_comment"] is True


def test_decide_discards_when_base_drifted() -> None:
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(), comments=[], pinned_base="b1", current_base="b2"
    , trusted_author="test-owner")
    assert d["action"] == "discard"
    assert d["label"] == "ai:implemented"  # reverted for re-dispatch
    assert d["post_comment"] is False
    assert d["base_drift"] is True


def test_decide_invalid_review_escalates_not_fix_loop() -> None:
    # Invalid reviewer output (verdict=None) is an infra/output failure, not a real
    # changes_requested verdict: escalate to a human rather than spend fix rounds on
    # an empty review.
    d = pipeline.decide_review_harvest(
        review_obj={"verdict": "??"}, comments=[], pinned_base="b1", current_base="b1"
    , trusted_author="test-owner")
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["verdict"] is None
    assert d["post_comment"] is False


def test_decide_routes_to_max_iterations_at_cap() -> None:
    # Four prior rounds already recorded; this changes_requested round is the 5th.
    prior = [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": pipeline.REVIEW_MARKER}] * 4
    d = pipeline.decide_review_harvest(
        review_obj=changes_review(), comments=prior, pinned_base="b1", current_base="b1"
    , trusted_author="test-owner")
    assert d["action"] == "max_iterations"
    assert d["label"] == "ai:max-iterations"
    assert d["post_comment"] is True


def test_decide_discard_reverts_to_pre_label() -> None:
    # An ai:revised PR that drifts must revert to ai:revised, not ai:implemented.
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="b1",
        current_base="b2",
        pre_label="ai:revised",
        trusted_author="test-owner",
    )
    assert d["action"] == "discard"
    assert d["label"] == "ai:revised"


def test_decide_discard_defaults_pre_label_to_implemented() -> None:
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(), comments=[], pinned_base="b1", current_base="b2"
    , trusted_author="test-owner")
    assert d["label"] == "ai:implemented"


def test_decide_clean_review_at_cap_still_promotes() -> None:
    # The cap only stops further fix loops; a clean verdict still promotes.
    prior = [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": pipeline.REVIEW_MARKER}] * 9
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(), comments=prior, pinned_base="b1", current_base="b1"
    , trusted_author="test-owner")
    assert d["action"] == "promote"


# --- decide_review_harvest: head drift (finding 2) ---


def test_decide_discards_when_head_drifted() -> None:
    # A clean review against a head that has since been force-pushed must NOT
    # promote the new, unreviewed head — it is discarded and re-dispatched.
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h2",
        trusted_author="test-owner",
    )
    assert d["action"] == "discard"
    assert d["head_drift"] is True
    assert d["post_comment"] is False
    assert d["label"] == "ai:implemented"


def test_decide_promotes_when_head_steady() -> None:
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        trusted_author="test-owner",
    )
    assert d["action"] == "promote"
    assert d["head_drift"] is False


# --- decide_review_harvest: base retarget ---


def test_decide_discards_when_base_retargeted() -> None:
    # A PR retargeted from main to develop while the review ran: the old
    # base's tip and the head are both unchanged, so the SHA drift checks
    # alone would promote a review anchored to the wrong target.
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        pinned_base_ref="main",
        current_base_ref="develop",
        trusted_author="test-owner",
    )
    assert d["action"] == "discard"
    assert d["label"] == "ai:implemented"
    assert d["base_retarget"] is True
    assert d["post_comment"] is False


def test_decide_retarget_reverts_to_pre_label() -> None:
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pre_label="ai:revised",
        pinned_base_ref="main",
        current_base_ref="develop",
        trusted_author="test-owner",
    )
    assert d["action"] == "discard"
    assert d["label"] == "ai:revised"


def test_decide_retarget_discards_without_base_tip_resolution() -> None:
    # When the base was retargeted the old ref may be deleted, so the tick
    # cannot resolve its tip; an empty current_base must not mask the discard.
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="b1",
        current_base="",
        pinned_head="h1",
        current_head="h1",
        pinned_base_ref="main",
        current_base_ref="develop",
        trusted_author="test-owner",
    )
    assert d["action"] == "discard"
    assert d["base_retarget"] is True


def test_decide_same_base_ref_promotes() -> None:
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        pinned_base_ref="main",
        current_base_ref="main",
        trusted_author="test-owner",
    )
    assert d["action"] == "promote"
    assert d["base_retarget"] is False


def test_decide_retarget_check_inactive_when_refs_absent() -> None:
    # Backward compatibility: callers that predate the retarget guard omit
    # both refs and keep the SHA-only drift behavior.
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="b1",
        current_base="b1",
        trusted_author="test-owner",
    )
    assert d["action"] == "promote"
    assert d["base_retarget"] is False


# --- decide_review_harvest: verdict/findings consistency (finding 4) ---


def test_decide_does_not_promote_ready_verdict_with_blocker_findings() -> None:
    # A reviewer that emits ready_for_human while reporting a blocker contradicts
    # the schema's own definition; the promote gate must refuse to promote it.
    contradictory = {
        "verdict": "ready_for_human",
        "findings": [{"severity": "blocker", "file": "a.py", "summary": "leak"}],
    }
    d = pipeline.decide_review_harvest(
        review_obj=contradictory,
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        trusted_author="test-owner",
    )
    assert d["action"] == "request_changes"
    assert d["label"] == "ai:changes-requested"
    # The posted comment must reflect changes-requested, not a clean promotion.
    assert "🔧" in d["comment_body"]


def test_decide_promotes_ready_verdict_with_minor_findings() -> None:
    # ready_for_human means "no blocker/major"; minor findings are compatible.
    ready_minor = {
        "verdict": "ready_for_human",
        "findings": [{"severity": "minor", "file": "a.py", "summary": "nit"}],
    }
    d = pipeline.decide_review_harvest(
        review_obj=ready_minor,
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        trusted_author="test-owner",
    )
    assert d["action"] == "promote"


# --- decide_review_harvest: idempotency (finding 3) ---


def test_decide_skips_comment_when_already_posted_for_head() -> None:
    # A prior tick posted the comment but its label edit failed; the re-run must
    # not post a duplicate (which would inflate the iteration count) — it only
    # re-applies the label.
    prior_body = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[{"severity": "blocker", "file": "a.py", "summary": "boom"}],
        base_sha="b1",
        head_sha="h1",
    )
    d = pipeline.decide_review_harvest(
        review_obj=changes_review(),
        comments=[{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": prior_body}],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        trusted_author="test-owner",
    )
    assert d["action"] == "request_changes"
    assert d["post_comment"] is False
    assert d["label"] == "ai:changes-requested"


def test_decide_posts_re_review_when_base_moved_under_same_head() -> None:
    # A prior round reviewed head h1 against base b1 and posted its comment. The base
    # then advanced to b2 and the PR was re-dispatched against the SAME head. The new
    # review (same head, new base) is a legitimate fresh round and must post its
    # findings — keying idempotency on the head alone would suppress it and leave the
    # next fixer with no current review to act on.
    prior_body = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[{"severity": "blocker", "file": "a.py", "summary": "old finding"}],
        base_sha="b1",
        head_sha="h1",
    )
    d = pipeline.decide_review_harvest(
        review_obj=changes_review(),
        comments=[{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": prior_body}],
        pinned_base="b2",
        current_base="b2",
        pinned_head="h1",
        current_head="h1",
        trusted_author="test-owner",
    )
    assert d["post_comment"] is True
    assert d["action"] == "request_changes"


def test_decide_audits_diff_base_separately_from_freshness_tip() -> None:
    # Dispatch pins base_sha = merge-base (the Codex diff anchor) and
    # base_tip_sha = base tip (the freshness snapshot). The posted audit record
    # must attribute each SHA to its real role: drift is compared on the tip,
    # while the comment/token carry the true diff anchor plus the tip.
    d = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[],
        pinned_base="tip1",
        current_base="tip1",
        pinned_head="h1",
        current_head="h1",
        diff_base_sha="mb1",
        trusted_author="test-owner",
    )
    assert d["action"] == "promote"
    body = str(d["comment_body"])
    assert "diff base `mb1`" in body
    assert "base tip `tip1`" in body

    # Idempotency: a replay with the same triple must not re-post.
    d2 = pipeline.decide_review_harvest(
        review_obj=clean_review(),
        comments=[{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}],
        pinned_base="tip1",
        current_base="tip1",
        pinned_head="h1",
        current_head="h1",
        diff_base_sha="mb1",
        trusted_author="test-owner",
    )
    assert d2["post_comment"] is False


def test_decide_idempotent_replay_does_not_inflate_iteration_cap() -> None:
    # Four prior distinct rounds, and THIS round's comment was already posted by an
    # interrupted earlier attempt (5 markers total). A naive rounds+1 would jump to
    # 6 and trip the cap on a duplicate; idempotent counting keeps it at the real 5.
    prior = [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": pipeline.REVIEW_MARKER}] * 4
    this_round = pipeline.render_review_comment(
        verdict="changes_requested",
        findings=[{"severity": "blocker", "file": "a.py", "summary": "boom"}],
        base_sha="b1",
        head_sha="h1",
    )
    d = pipeline.decide_review_harvest(
        review_obj=changes_review(),
        comments=prior + [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": this_round}],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        trusted_author="test-owner",
    )
    # 5 completed rounds → cap reached, but on a replay we still must not re-post.
    assert d["action"] == "max_iterations"
    assert d["post_comment"] is False


# --- touches_review_policy (candidate-controlled review policy) ---


def test_touches_review_policy_false_for_ordinary_code() -> None:
    assert pipeline.touches_review_policy(["app/foo.py", "frontend/x.ts"]) is False


def test_touches_review_policy_true_for_root_claude_md() -> None:
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is True


def test_touches_review_policy_true_for_nested_claude_or_agents_md() -> None:
    # codex loads the nearest AGENTS.md/CLAUDE.md up the tree, so a nested one
    # still governs the reviewer.
    assert pipeline.touches_review_policy(["frontend/AGENTS.md"]) is True
    assert pipeline.touches_review_policy(["app/sub/CLAUDE.md"]) is True


def test_touches_review_policy_true_for_configured_review_checklist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An operator-declared checklist path gates, and only that path.

    The target repo's own review checklists steer the reviewer exactly as
    CLAUDE.md does, but their paths belong to the target, so they are declared
    in config rather than shipped. A near-miss path must not gate: matching by
    basename or prefix here would escalate unrelated files in the same
    directory.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nchecklist_paths = [".ci/review-checklist.md"]\n',
    )
    assert pipeline.touches_review_policy([".ci/review-checklist.md"]) is True
    assert pipeline.touches_review_policy([".ci/review-checklist-v2.md"]) is False
    assert pipeline.touches_review_policy(["docs/review-checklist.md"]) is False


def test_touches_review_policy_ships_no_checklist_paths() -> None:
    """The shipped config declares no checklist paths.

    A path pinned here would be one repo's, dead in every other install, and it
    would make the adopter's gate look configured when it is not. CLAUDE.md and
    AGENTS.md still gate by basename with no configuration at all.
    """
    assert pipeline.resolve_config().checklist_paths == ()
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is True


def test_touches_review_policy_true_for_gate_config() -> None:
    """The gate's own config.toml is review policy: a PR that flips
    allow_touching to true would otherwise be auto-reviewed, then silently
    disable the gate for every future policy-editing PR after a human merge."""
    assert (
        pipeline.touches_review_policy(
            [".claude/skills/tick/config.toml"]
        )
        is True
    )


def test_touches_review_policy_true_for_gate_implementation() -> None:
    """The gate's implementation files are review policy too: a PR that
    rewrites _policy_touching_allowed to always return True (or removes the
    tick.py:9031 call) is the same silent-disable vector as a config flip."""
    assert (
        pipeline.touches_review_policy(
            [".claude/skills/tick/lib/pipeline.py"]
        )
        is True
    )
    assert (
        pipeline.touches_review_policy(
            [".claude/skills/tick/lib/tick.py"]
        )
        is True
    )


@pytest.mark.parametrize("path,protected", [
    ("skills/tick/resolve-target.sh", True),
    (".claude/skills/tick/resolve-target.sh", True),
    ("plugin/skills/tick/resolve-target.sh", True),
    ("other-skills/tick/resolve-target.sh", False),
])
def test_target_resolver_is_review_policy(path: str, protected: bool) -> None:
    """The resolver chooses the repo whose review policy startup will load."""
    assert pipeline.touches_review_policy([path]) is protected


def test_touches_review_policy_false_for_unrelated_dotclaude_files() -> None:
    # A change to some other skill under .claude/ does not steer the reviewer's
    # governing instructions, so it must not block auto-review.
    assert pipeline.touches_review_policy([".claude/skills/other/SKILL.md"]) is False


def test_touches_review_policy_ignores_blank_lines() -> None:
    assert pipeline.touches_review_policy(["", "app/foo.py", ""]) is False


def test_touches_review_policy_config_allow_touching_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """allow_touching = true must let a PR that edits reviewer policy files
    flow to auto-review — the operator has explicitly accepted the trust
    boundary (the reviewer runs on the PR's own worktree)."""
    operator_config(monkeypatch, tmp_path, "[defaults]\nallow_touching = true\n")
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is False
    assert pipeline.touches_review_policy(["app/AGENTS.md"]) is False
    assert pipeline.touches_review_policy(["app/foo.py"]) is False


def test_touches_review_policy_config_conservative_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Missing, false, or malformed config must keep the conservative gate."""
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(tmp_path / "nope.toml"))
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is True
    cfg = operator_config(
        monkeypatch, tmp_path, "[defaults]\nallow_touching = false\n",
    )
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is True
    cfg.write_text("not toml [", encoding="utf-8")
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is True
    # A valid-but-wrong-shape [defaults] value must not crash the gate — it
    # fails closed like every other non-true value.
    cfg.write_text('defaults = "allow"\n', encoding="utf-8")
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is True
    # Invalid UTF-8 bytes raise UnicodeDecodeError (not TOMLDecodeError) from
    # tomllib.load — the gate must fail closed, never crash the tick.
    cfg.write_bytes(b"\xff\xfe[defaults]\nallow_touching = true\n")
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is True


# --- Issue and PR author trust (credential exposure boundary) ---


def test_packaged_policy_default_allows_operator_authored_policy_edits(monkeypatch):
    monkeypatch.setattr(
        pipeline, "_PACKAGED_CONFIG_PATH", Path(pipeline.__file__).resolve().parent.parent / "config.toml",
    )
    assert pipeline.resolve_config().allow_touching is True
    assert pipeline.touches_review_policy(["AGENTS.md"]) is False


@pytest.mark.parametrize(
    "metadata,trusted_author,expected",
    [
        ({"author": {"login": "operator"}}, "operator", True),
        ({"author": {"login": "stranger"}}, "operator", False),
        ({"author": {"login": "Operator"}}, "operator", False),
        (None, "operator", False),
        ([], "operator", False),
        ({}, "operator", False),
        ({"author": None}, "operator", False),
        ({"author": "operator"}, "operator", False),
        ({"author": {}}, "operator", False),
        ({"author": {"login": None}}, "operator", False),
        ({"author": {"login": 123}}, "operator", False),
        ({"author": {"login": ""}}, "", False),
    ],
)
def test_trusted_issue_candidate_fails_closed(
    metadata: object, trusted_author: str, expected: bool,
) -> None:
    assert pipeline.trusted_issue_candidate(
        metadata, trusted_author=trusted_author,
    ) is expected


def test_trusted_review_candidate_accepts_owner_authored_same_repo_pr() -> None:
    metadata = {
        "author": {"login": "test-owner"},
        "headRepository": {"nameWithOwner": "test-owner/test-repo"},
        "isCrossRepository": False,
    }

    assert pipeline.trusted_review_candidate(
        metadata,
        trusted_author="test-owner",
        trusted_repo="test-owner/test-repo",
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "author": {"login": "contributor"},
            "headRepository": {"nameWithOwner": "test-owner/test-repo"},
            "isCrossRepository": False,
        },
        {
            "author": {"login": "test-owner"},
            "headRepository": {"nameWithOwner": "fork/sworn"},
            "isCrossRepository": True,
        },
        {
            "author": {"login": "test-owner"},
            "headRepository": {"nameWithOwner": "test-owner/other"},
            "isCrossRepository": False,
        },
        {"author": {"login": "test-owner"}, "isCrossRepository": False},
        None,
    ],
)
def test_trusted_review_candidate_rejects_untrusted_or_malformed_metadata(
    metadata: object,
) -> None:
    assert not pipeline.trusted_review_candidate(
        metadata,
        trusted_author="test-owner",
        trusted_repo="test-owner/test-repo",
    )


# --- reconcile_dispatch (label-driven in-flight reconciliation) ---


def test_reconcile_noops_on_markerless_in_flight_label() -> None:
    # ai:reviewing with no local marker proves nothing: another machine's tick
    # can never see this machine's markers, and a human may have claimed the
    # label by hand. Absence of a local file is not evidence of an orphan, so
    # the only safe decision is to do nothing (issue #592).
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=False,
        classify_state="",
        pre_label="ai:implemented",
    )
    assert d["action"] == "noop"
    assert d["kill"] is False
    assert d["label"] == ""


def test_reconcile_noop_when_no_marker_and_not_in_flight() -> None:
    # No marker and the PR is not parked at an in-flight label: nothing of ours.
    d = pipeline.reconcile_dispatch(
        current_label="ai:implemented",
        marker_valid=False,
        classify_state="",
        pre_label="ai:implemented",
    )
    assert d["action"] == "noop"


def test_reconcile_harvests_completed_at_reviewing_label() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=True,
        classify_state="complete",
        pre_label="ai:implemented",
    )
    assert d["action"] == "harvest"


def test_reconcile_harvests_completed_when_label_edit_failed() -> None:
    # Finding: dispatch wrote the marker and the engine finished, but the
    # post-dispatch label edit failed so the PR is still at its pre-dispatch
    # label. The completed result is still in our custody and must be harvested,
    # not re-dispatched into a second reviewer.
    d = pipeline.reconcile_dispatch(
        current_label="ai:revised",
        marker_valid=True,
        classify_state="complete",
        pre_label="ai:revised",
    )
    assert d["action"] == "harvest"


def test_reconcile_cleans_up_completed_when_human_took_over() -> None:
    # A human moved the PR out of our custody while the engine ran: do not act on
    # the stale result, just drop the scratch.
    d = pipeline.reconcile_dispatch(
        current_label="ai:ready-for-human",
        marker_valid=True,
        classify_state="complete",
        pre_label="ai:implemented",
    )
    assert d["action"] == "cleanup"


def test_reconcile_leaves_live_engine() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=True,
        classify_state="live",
        pre_label="ai:implemented",
    )
    assert d["action"] == "leave"


def test_reconcile_reverts_stale_in_custody() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=True,
        classify_state="stale",
        pre_label="ai:revised",
    )
    assert d["action"] == "revert"
    assert d["label"] == "ai:revised"


def test_reconcile_routes_bound_failure_to_terminal_failure_action() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=True,
        classify_state="failed",
        pre_label="ai:implemented",
    )
    assert d == {"action": "fail", "kill": False, "label": "ai:needs-human-decision"}


def test_reconcile_cleans_up_stale_when_human_took_over() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:changes-requested",
        marker_valid=True,
        classify_state="stale",
        pre_label="ai:implemented",
    )
    assert d["action"] == "cleanup"


def test_reconcile_kills_and_escalates_hung_in_custody() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=True,
        classify_state="hung",
        pre_label="ai:implemented",
    )
    assert d["action"] == "kill_escalate"
    assert d["kill"] is True
    assert d["label"] == "ai:needs-human-decision"


def test_reconcile_kills_and_cleans_up_hung_out_of_custody() -> None:
    # Even when a human moved the label, a hung process must still be killed so it
    # cannot pin the cadence at "busy"; we just do not clobber the human's label.
    d = pipeline.reconcile_dispatch(
        current_label="ai:ready-for-human",
        marker_valid=True,
        classify_state="hung",
        pre_label="ai:implemented",
    )
    assert d["action"] == "kill_cleanup"
    assert d["kill"] is True


# --- CLI ---


def test_cli_harvest_review_emits_decision_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review = tmp_path / "review.json"
    review.write_text(json.dumps(changes_review()))
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([]))

    rc = pipeline.main(
        [
            "harvest-review", "--trusted-author", "test-owner",
            "--review",
            str(review),
            "--comments",
            str(comments),
            "--pinned-base",
            "b1",
            "--current-base",
            "b1",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "request_changes"
    assert out["label"] == "ai:changes-requested"
    assert out["post_comment"] is True


def test_cli_harvest_review_unwraps_gh_comments_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `gh pr view --json comments` writes {"comments": [...]}, not a bare list.
    # The helper must unwrap it, or the iteration count is silently always zero.
    review = tmp_path / "review.json"
    review.write_text(json.dumps(changes_review()))
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps({"comments": [{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": pipeline.REVIEW_MARKER}] * 4}))
    rc = pipeline.main(
        [
            "harvest-review", "--trusted-author", "test-owner",
            "--review",
            str(review),
            "--comments",
            str(comments),
            "--pinned-base",
            "b1",
            "--current-base",
            "b1",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    # 4 prior rounds + this one = 5 → cap reached.
    assert out["action"] == "max_iterations"
    assert out["rounds"] == 4


def test_cli_harvest_review_passes_pre_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review = tmp_path / "review.json"
    review.write_text(json.dumps(clean_review()))
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([]))
    rc = pipeline.main(
        [
            "harvest-review", "--trusted-author", "test-owner",
            "--review",
            str(review),
            "--comments",
            str(comments),
            "--pinned-base",
            "b1",
            "--current-base",
            "b2",  # drifted
            "--pre-label",
            "ai:revised",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "discard"
    assert out["label"] == "ai:revised"


def test_cli_harvest_review_missing_file_escalates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A missing/unreadable review.json is an infra failure, not a verdict: escalate
    # to a human conservatively rather than crash or enter the paid fix loop.
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([]))
    rc = pipeline.main(
        [
            "harvest-review", "--trusted-author", "test-owner",
            "--review",
            str(tmp_path / "nope.json"),
            "--comments",
            str(comments),
            "--pinned-base",
            "b1",
            "--current-base",
            "b1",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "escalate"
    assert out["label"] == "ai:needs-human-decision"
    assert out["verdict"] is None


def test_cli_harvest_review_discards_on_head_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review = tmp_path / "review.json"
    review.write_text(json.dumps(clean_review()))
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps([]))
    rc = pipeline.main(
        [
            "harvest-review", "--trusted-author", "test-owner",
            "--review",
            str(review),
            "--comments",
            str(comments),
            "--pinned-base",
            "b1",
            "--current-base",
            "b1",
            "--pinned-head",
            "h1",
            "--current-head",
            "h2",  # force-pushed under the review
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "discard"
    assert out["head_drift"] is True


def test_cli_classify_dispatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dispatch = tmp_path / "dispatch.json"
    dispatch.write_text(json.dumps({"pid": 1, "deadline": 50}))
    rc = pipeline.main(
        [
            "classify-dispatch",
            "--dispatch",
            str(dispatch),
            "--now",
            "10",
            "--pid-alive",
            "0",
            "--result-exists",
            "0",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["state"] == "stale"


def test_cli_classify_dispatch_corrupt_deadline_is_conservative(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A non-numeric deadline must not crash classify, and must fail CLOSED for
    # admission: a live engine with a corrupt deadline counts as in-flight (`live`),
    # never `hung` — classifying it hung would free its slot and let the cap admit an
    # extra reviewer over a still-running, money-spending one.
    dispatch = tmp_path / "dispatch.json"
    dispatch.write_text(json.dumps({"pid": 1, "deadline": "soon"}))
    rc = pipeline.main(
        [
            "classify-dispatch",
            "--dispatch",
            str(dispatch),
            "--now",
            "10",
            "--pid-alive",
            "1",
            "--result-exists",
            "0",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["state"] == "live"


def test_cli_classify_dispatch_bool_deadline_is_conservative(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A boolean deadline (True/False) must not bypass the fail-closed coercion.
    # float(True) == 1.0, which would classify a live engine as `hung` (since
    # now > 1.0), freeing its slot. The fix coerces bools to +inf → `live`.
    dispatch = tmp_path / "dispatch.json"
    dispatch.write_text(json.dumps({"pid": 1, "deadline": True}))
    rc = pipeline.main(
        [
            "classify-dispatch",
            "--dispatch",
            str(dispatch),
            "--now",
            "10",
            "--pid-alive",
            "1",
            "--result-exists",
            "0",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["state"] == "live"


def test_cli_atomic_write_reads_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "dispatch.json"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"pid": 7}'))
    rc = pipeline.main(["atomic-write", "--path", str(target)])
    assert rc == 0
    assert json.loads(target.read_text()) == {"pid": 7}


def test_cli_validate_review_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review = tmp_path / "review.json"
    review.write_text(json.dumps(clean_review()))
    rc = pipeline.main(["validate-review", "--review", str(review)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "ready_for_human"


def test_cli_validate_review_invalid_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"verdict": "??"}))
    rc = pipeline.main(["validate-review", "--review", str(review)])
    assert rc == 1
    assert "INVALID" in capsys.readouterr().out


def test_cli_touches_review_policy_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("app/foo.py\nCLAUDE.md\n"))
    rc = pipeline.main(["touches-review-policy"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["touches_policy"] is True


def test_cli_touches_review_policy_false_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("app/foo.py\n"))
    rc = pipeline.main(["touches-review-policy"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["touches_policy"] is False


def test_cli_validate_review_candidate_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    metadata = {
        "author": {"login": "test-owner"},
        "headRepository": {"nameWithOwner": "test-owner/test-repo"},
        "isCrossRepository": False,
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(metadata)))

    rc = pipeline.main(
        [
            "validate-review-candidate",
            "--trusted-author",
            "test-owner",
            "--trusted-repo",
            "test-owner/test-repo",
        ]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"trusted": True}


def test_no_helper_answers_admission_from_shared_labels() -> None:
    """Only one admission contract may exist, and it is marker-only.

    ``in_flight_prs``/``dispatch_admission`` were the older PR-number-only
    model that unioned authoritative ``ai:reviewing`` labels with local
    markers. Once :func:`pipeline.in_flight_refs` stopped counting labels,
    those two answered a question nothing in the system asks any more — and
    answered it with the opposite verdict for the same repo state. A second
    admission surface is how an operator reasons from a contract production
    abandoned, so the sibling is removed rather than left to disagree.
    """
    assert not hasattr(pipeline, "in_flight_prs")
    assert not hasattr(pipeline, "dispatch_admission")


def test_no_admission_claimant_can_ignore_process_liveness() -> None:
    """Every symbol claiming to answer admission must be able to see liveness.

    The guard above names two retired symbols, so it can only catch helpers
    someone already thought to remove. This asserts the property instead: a
    signature that cannot represent a live process group cannot answer "is this
    slot spent", because ``complete``-with-a-live-child is exactly the marker
    where the two answers diverge. A helper that takes bare classify states and
    still advertises itself as the admission count is the same competing
    contract that justified deleting ``in_flight_prs``.
    """
    live_child = ("pr", 42, pipeline.LABEL_REVIEWING, "complete", True)
    assert len(pipeline.in_flight_refs([live_child])) == 1

    offenders: list[str] = []
    for name, obj in vars(pipeline).items():
        if name.startswith("_") or name == "in_flight_refs":
            continue
        if not inspect.isfunction(obj):
            continue
        doc = (obj.__doc__ or "").lower()
        if "admission count" not in doc and "occupy a concurrency slot" not in doc:
            continue
        # A bare classify-state list is the signature that cannot carry
        # liveness. ``can_dispatch`` claims the count in prose but takes an
        # int, so it compares a total rather than deriving one.
        takes_bare_states = any(
            "list[str]" in str(param.annotation)
            for param in inspect.signature(obj).parameters.values()
        )
        if takes_bare_states:
            offenders.append(name)

    assert not offenders, (
        f"{offenders} advertise themselves as the admission count but take "
        "classify states alone, so they cannot represent a live process "
        "group; in_flight_refs is the only admission contract"
    )


def test_cli_exposes_no_label_union_admission_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator CLI must not offer the retired union calculator."""
    with pytest.raises(SystemExit):
        pipeline.main(["dispatch-admission", "--max", "3"])

    assert "invalid choice" in capsys.readouterr().err


def test_cli_reconcile_dispatch_emits_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dispatch = tmp_path / "dispatch.json"
    dispatch.write_text(json.dumps(marker(pr_number=7, pre_label="ai:revised")))
    rc = pipeline.main(
        [
            "reconcile-dispatch",
            "--dispatch",
            str(dispatch),
            "--pr",
            "7",
            "--current-label",
            "ai:reviewing",
            "--classify-state",
            "stale",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "revert"
    assert out["label"] == "ai:revised"


def test_cli_reconcile_dispatch_missing_marker_is_non_destructive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An ai:reviewing PR whose marker file is absent must not crash — and must
    # not act, since a missing marker proves nothing about custody.
    rc = pipeline.main(
        [
            "reconcile-dispatch",
            "--dispatch",
            str(tmp_path / "nope.json"),
            "--current-label",
            "ai:reviewing",
            "--classify-state",
            "",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "noop"
    assert out["label"] == ""


# --- review-revision hardening (consolidated review pass) ---


def test_reconcile_escalates_launching_in_custody() -> None:
    # A launching lease observed on a later tick means the spawn crashed
    # mid-flight. Unlike a markerless item this is proven custody — our own
    # marker — and we cannot prove whether a paid engine is live, so never
    # re-dispatch (that would double-spend). Money may be burning, which is a
    # genuine call for the operator.
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=True,
        classify_state="launching",
        pre_label="ai:implemented",
    )
    assert d["action"] == "escalate"
    assert d["label"] == pipeline.LABEL_NEEDS_HUMAN


def test_reconcile_cleans_up_launching_when_human_took_over() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:ready-for-human",
        marker_valid=True,
        classify_state="launching",
        pre_label="ai:implemented",
    )
    assert d["action"] == "cleanup"


def test_reconcile_reverts_stale_under_attempt_cap() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=True,
        classify_state="stale",
        pre_label="ai:implemented",
        dispatch_attempts=1,
        attempt_threshold=3,
    )
    assert d["action"] == "revert"


def test_reconcile_escalates_stale_at_attempt_cap() -> None:
    # A repeatable resultless failure (codex/API crash that never posts a comment) must
    # not re-dispatch forever — the comment-based round cap never counts it. Once the
    # durable attempt count would reach the cap, escalate instead of reverting.
    d = pipeline.reconcile_dispatch(
        current_label="ai:reviewing",
        marker_valid=True,
        classify_state="stale",
        pre_label="ai:implemented",
        dispatch_attempts=2,
        attempt_threshold=3,
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"


def test_decide_escalates_on_malformed_review() -> None:
    # Invalid reviewer output (verdict=None) must not be routed into the paid fix loop
    # as changes_requested with no findings — escalate to a human instead.
    d = pipeline.decide_review_harvest(
        review_obj={"garbage": True},
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        trusted_author="test-owner",
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["post_comment"] is False


def test_decide_escalates_changes_requested_with_no_blocking_findings() -> None:
    # changes_requested with only minor (or empty) findings contradicts the repo rule
    # that nits alone do not warrant changes — escalate rather than dispatch a fixer
    # with nothing actionable to fix.
    review = {"verdict": "changes_requested", "findings": [
        {"severity": "minor", "file": "a.py", "summary": "nit"}
    ]}
    d = pipeline.decide_review_harvest(
        review_obj=review,
        comments=[],
        pinned_base="b1",
        current_base="b1",
        pinned_head="h1",
        current_head="h1",
        trusted_author="test-owner",
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["post_comment"] is False


def test_in_flight_refs_preserves_kind_and_stage_identity() -> None:
    """Admission dedupes only the same dispatch identity."""
    markers = [
        ("issue", 42, "ai:implementing", "live", True),
        ("pr", 42, "ai:reviewing", "hung", True),
        ("pr", 42, "ai:fixing", "live", True),
    ]

    refs = pipeline.in_flight_refs(markers)

    assert refs == {
        ("issue", 42, "ai:implementing"),
        ("pr", 42, "ai:reviewing"),
        ("pr", 42, "ai:fixing"),
    }


def test_in_flight_refs_ignores_items_with_no_local_marker() -> None:
    """Occupancy comes from markers alone — a label cannot add a slot.

    ``ai:implementing`` is a shared workflow state contributors set by hand,
    so counting it as a paid engine let stale manual pickups starve dispatch
    permanently (#534, and again on 2026-08-20).
    """
    assert pipeline.in_flight_refs([]) == set()


def test_in_flight_refs_counts_a_finished_marker_whose_group_still_lives() -> None:
    """A live group occupies a slot whatever the classify state says.

    A wrapper that published its envelope while a child still runs
    classifies ``complete`` — outside ENGINE_OCCUPIED_STATES — but is still
    spending and still owes the pipeline an archive or a kill.
    """
    refs = pipeline.in_flight_refs([("pr", 42, "ai:reviewing", "complete", True)])

    assert refs == {("pr", 42, "ai:reviewing")}


def test_in_flight_refs_ignores_a_finished_marker_whose_group_is_dead() -> None:
    """A finished, dead engine spends nothing and holds no slot."""
    assert pipeline.in_flight_refs(
        [("pr", 42, "ai:reviewing", "complete", False)]
    ) == set()


def test_cadence_names_human_custody_when_wedged_at_the_admission_cap() -> None:
    """A loop that cannot dispatch must say why, not just when it wakes.

    Every occupied slot held by a human is discounted from ``in_flight_count``,
    so this tick looks like an ordinary warm poll: 0 in flight, queue non-empty.
    Admission is in fact saturated and no tick can dispatch until the operator
    acts, and the reason line is the only thing the one-shot ``/loop`` path
    reports.
    """
    decision = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=True,
        at_capacity=False,
        is_quiescent=False,
        held_items=("pr #41 (ai:needs-human-decision)", "pr #42 (ai:blocked)"),
    )

    assert decision.stop is False
    assert decision.reason == (
        "actionable queue non-empty → wake 270s"
        "; awaiting human decision: pr #41 (ai:needs-human-decision), "
        "pr #42 (ai:blocked)"
    )


def test_cadence_omits_custody_suffix_when_nothing_is_held() -> None:
    """The suffix is evidence, not decoration — no holds, no suffix."""
    decision = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=True,
        at_capacity=False,
        is_quiescent=False,
    )

    assert decision.reason == "actionable queue non-empty → wake 270s"


def test_can_dispatch_respects_cap() -> None:
    assert pipeline.can_dispatch(in_flight=2, max_in_flight=3) is True
    assert pipeline.can_dispatch(in_flight=3, max_in_flight=3) is False
    assert pipeline.can_dispatch(in_flight=4, max_in_flight=3) is False


def test_valid_marker_rejects_empty_and_partial() -> None:
    assert pipeline.valid_marker({}) is False
    assert pipeline.valid_marker({"stage": "review"}) is False
    assert pipeline.valid_marker("not a dict") is False


def test_valid_marker_accepts_full_and_launching_lease() -> None:
    assert pipeline.valid_marker(marker()) is True
    assert pipeline.valid_marker(lease()) is True


def test_valid_marker_rejects_wrong_field_types() -> None:
    # bool is an int subclass; a boolean pr_number/pid must not pass the int check.
    assert pipeline.valid_marker(marker(pr_number=True)) is False
    assert pipeline.valid_marker(marker(pid=True)) is False
    # A string pid (or any non-int) is not a handle kill -0 can use.
    assert pipeline.valid_marker(marker(pid="5")) is False
    # A non-positive pid is never a real process handle.
    assert pipeline.valid_marker(marker(pid=0)) is False
    # A non-numeric deadline crashes classify-dispatch downstream — reject it here.
    assert pipeline.valid_marker(marker(deadline="soon")) is False


def test_valid_marker_rejects_unknown_stage_and_state() -> None:
    # An unknown stage is not a marker this pipeline ever writes.
    assert pipeline.valid_marker(marker(stage="bogus")) is False
    # A lease must carry the one known lease state, not arbitrary text.
    assert pipeline.valid_marker(lease(state="anything")) is False


def test_valid_marker_rejects_unknown_engine() -> None:
    assert pipeline.valid_marker(marker(engine="unknown-provider")) is False


def test_valid_marker_rejects_non_finite_deadline() -> None:
    # json.loads accepts NaN/Infinity, and both pass a naive isinstance(float) check.
    # A NaN deadline makes `now > deadline` always False, so a live engine could never
    # time out (never `hung`); an infinite deadline is equally untrustworthy. Reject.
    assert pipeline.valid_marker(marker(deadline=float("nan"))) is False
    assert pipeline.valid_marker(marker(deadline=float("inf"))) is False


def test_valid_marker_requires_exactly_one_ownership_signal() -> None:
    # A published marker carries a pid and NO state; a lease carries state and NO pid.
    # A marker with BOTH is corrupt: classify would read `launching` (from state) and
    # reconcile would escalate WITHOUT killing the live pid, leaking a paid process.
    assert pipeline.valid_marker(marker(state="launching")) is False
    # A pid present alongside an INVALID state must not be masked by the valid pid.
    assert pipeline.valid_marker(marker(state="bogus")) is False
    # Neither signal present → not an in-flight dispatch.
    bare = marker()
    del bare["pid"]
    assert pipeline.valid_marker(bare) is False


def test_valid_marker_cross_checks_pr_and_stage() -> None:
    m = marker(pr_number=2)
    # Matching identity passes.
    assert pipeline.valid_marker(m, expected_pr=2, expected_stage="review") is True
    # A marker for a different PR must never be trusted for this directory's PR.
    assert pipeline.valid_marker(m, expected_pr=3, expected_stage="review") is False
    # A marker for a different stage must not drive this stage's reconciliation.
    assert pipeline.valid_marker(m, expected_pr=2, expected_stage="fix") is False


def test_valid_marker_rejects_arbitrary_pre_label() -> None:
    # The marker's pre_label flows straight into reconciliation as the stale-revert
    # target. A corrupt marker naming a terminal label (here ai:ready-for-human) would
    # otherwise let a dead, resultless dispatch silently "promote" the PR. Only the two
    # real pre-dispatch labels are trusted.
    assert pipeline.valid_marker(marker(pre_label="ai:ready-for-human")) is False
    assert pipeline.valid_marker(marker(pre_label="ai:implemented")) is True
    assert pipeline.valid_marker(marker(pre_label="ai:revised")) is True


def test_valid_marker_requires_full_provenance_field_set() -> None:
    # A published marker must carry the full identity + provenance set recovery acts on;
    # a marker missing any of them is not a trustworthy dispatch record. Each omission
    # has a concrete failure mode (a missing head_sha disables the head-drift guard, a
    # missing deadline misclassifies a live engine as hung, a missing pre_label has no
    # revert target), so all are required, not optional.
    for field in ("engine", "stage", "pr_number", "deadline", "base_ref", "base_sha",
                  "head_sha", "pre_label"):
        m = marker()
        del m[field]
        assert pipeline.valid_marker(m) is False, f"missing {field} must fail closed"


@pytest.mark.parametrize("policy,valid", [
    ({"permission_mode": "dontAsk", "verify_tools": ["sh ./check.sh"]}, True),
    ({"permission_mode": "auto", "verify_tools": []}, True),
    ({"permission_mode": "bypassPermissions", "verify_tools": []}, False),
    ({"permission_mode": "dontAsk"}, False),
    ({"verify_tools": []}, False),
    ({"permission_mode": "auto", "verify_tools": "check"}, False),
    ({"permission_mode": "auto", "verify_tools": [7]}, False),
    ({"permission_mode": "auto", "verify_tools": ["Bash(check)"]}, False),
])
def test_implementer_marker_validates_optional_launch_policy(policy, valid):
    assert pipeline.valid_marker(impl_marker(**policy)) is valid
    assert pipeline.valid_marker(marker(**policy)) is False
    assert pipeline.valid_marker(fix_marker(**policy)) is False
    assert pipeline.valid_marker(impl_marker()) is True


def test_valid_marker_rejects_unknown_fields() -> None:
    # A marker carrying a field this pipeline never writes did not come from its writer;
    # do not partially trust it.
    assert pipeline.valid_marker(marker(surprise="x")) is False


def test_valid_marker_validates_base_tip_sha_as_provenance() -> None:
    # base_tip_sha is the anti-stale-promotion freshness anchor: if present it must be
    # a canonical full commit OID, not merely allowlisted. A mistyped/malformed value
    # would make harvest silently fall back to base_sha (the merge-base), gutting the
    # drift guard precisely at the field added to strengthen it.
    good_oid = "a" * 40
    assert pipeline.valid_marker(marker(base_tip_sha=good_oid)) is True
    assert pipeline.valid_marker(marker(base_tip_sha=7)) is False
    assert pipeline.valid_marker(marker(base_tip_sha="")) is False
    assert pipeline.valid_marker(marker(base_tip_sha="not-a-sha")) is False
    assert pipeline.valid_marker(marker(base_tip_sha="A" * 40)) is False
    assert pipeline.valid_marker(marker(base_tip_sha="a" * 39)) is False


def test_valid_marker_rejects_base_tip_sha_on_non_review_stages() -> None:
    # Only reviewer dispatch writes base_tip_sha; an implement/fix marker carrying it
    # did not come from this pipeline's writer for that stage.
    good_oid = "b" * 40
    assert pipeline.valid_marker(impl_marker(base_tip_sha=good_oid)) is False
    assert pipeline.valid_marker(fix_marker(base_tip_sha=good_oid)) is False


def test_valid_marker_validates_pr_body_revision_provenance() -> None:
    # The fixer's PR-body revision is gated on this block: the digest decides
    # whether the live body still matches what dispatch handed the engine, the
    # closure fields decide whether a replacement kept the issues merging will
    # close, and the truncation flag decides whether the engine saw enough of
    # the body to replace it at all. A mistyped digest reads as "somebody edited
    # the body" and disables the replacement silently; a mistyped closure set
    # rejects every candidate. The block is optional (markers predating the path
    # omit it) and fully typed when present.
    assert pipeline.valid_marker(fix_marker(pr_body=body_provenance())) is True
    assert pipeline.valid_marker(
        fix_marker(pr_body=body_provenance(closes=[])),
    ) is True
    for bad in [
        {"digest": ""},
        {"digest": "a" * 63},
        {"digest": "A" * 64},
        {"digest": 7},
        {"summary_closes": 0},
        {"summary_closes": True},
        {"summary_closes": "7"},
        {"closes": 7},
        {"closes": [7]},
        {"closes": [""]},
        {"truncated": "no"},
        {"truncated": 0},
    ]:
        assert pipeline.valid_marker(
            fix_marker(pr_body=body_provenance(**bad)),
        ) is False, f"a marker with {bad} in its pr_body block was trusted"
    assert pipeline.valid_marker(fix_marker(pr_body="a" * 64)) is False
    assert pipeline.valid_marker(fix_marker(pr_body=None)) is False


def test_valid_marker_rejects_a_partial_pr_body_provenance_block() -> None:
    # Dispatch writes all four fields together, so a block missing one is
    # corruption rather than a marker from before the revision path — and the
    # difference matters: a legacy marker is trusted for everything else it
    # carries, while a corrupt one must not be. Accepting a partial block would
    # let the harvest judge a candidate against half a contract, publish an
    # irreversible body edit, relabel, and then clean the only evidence.
    for missing in ("digest", "summary_closes", "closes", "truncated"):
        partial = body_provenance()
        del partial[missing]
        assert pipeline.valid_marker(fix_marker(pr_body=partial)) is False, (
            f"a pr_body block missing {missing!r} was trusted as complete"
        )
    surplus = body_provenance()
    surplus["closing_issues"] = [7]
    assert pipeline.valid_marker(fix_marker(pr_body=surplus)) is False


def test_valid_marker_rejects_body_revision_fields_on_non_fix_stages() -> None:
    # Only fixer dispatch reads a PR body and writes this. A review or
    # implement marker carrying it did not come from this pipeline's writer
    # for that stage.
    assert pipeline.valid_marker(marker(pr_body=body_provenance())) is False
    assert pipeline.valid_marker(impl_marker(pr_body=body_provenance())) is False


def test_valid_marker_requires_string_refs_and_shas() -> None:
    assert pipeline.valid_marker(marker(base_ref=123)) is False
    assert pipeline.valid_marker(marker(base_sha="")) is False
    assert pipeline.valid_marker(marker(head_sha=None)) is False
    assert pipeline.valid_marker(marker(engine="")) is False


def test_cli_reconcile_dispatch_ignores_an_arbitrary_pre_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # End-to-end of the demonstrated probe: a stale dispatch whose marker carries a
    # bogus pre_label must NOT return label=ai:ready-for-human. The strengthened
    # valid_marker rejects the marker, so the forged label is never applied — the
    # decision falls through to the markerless branch, which acts on nothing.
    dispatch = tmp_path / "dispatch.json"
    dispatch.write_text(
        json.dumps(
            {
                "engine": "codex",
                "stage": "review",
                "pr_number": 7,
                "pid": 1,
                "deadline": 50,
                "base_ref": "main",
                "base_sha": "b1",
                "head_sha": "h1",
                "pre_label": "ai:ready-for-human",
            }
        )
    )
    rc = pipeline.main(
        [
            "reconcile-dispatch",
            "--dispatch",
            str(dispatch),
            "--pr",
            "7",
            "--current-label",
            "ai:reviewing",
            "--classify-state",
            "stale",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "noop"
    assert out["label"] != "ai:ready-for-human"


def test_cli_validate_marker_reports_validity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dispatch = tmp_path / "dispatch.json"
    dispatch.write_text(json.dumps(marker(pr_number=9)))
    rc = pipeline.main(
        ["validate-marker", "--dispatch", str(dispatch), "--pr", "9", "--stage", "review"]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_cli_validate_marker_fails_closed_on_pr_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dispatch = tmp_path / "dispatch.json"
    dispatch.write_text(json.dumps(marker(pr_number=9)))
    rc = pipeline.main(
        ["validate-marker", "--dispatch", str(dispatch), "--pr", "10", "--stage", "review"]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["valid"] is False


# --- CLI: harvest-implementation ---


def test_cli_harvest_implementation_emits_create_pr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "result.json"
    result.write_text(json.dumps(impl_complete_result()))
    rc = pipeline.main(
        ["harvest-implementation", "--result", str(result), "--pre-label", "ai:available"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "create_pr"
    assert out["sentinel"] == "IMPLEMENTATION_COMPLETE"


def test_cli_harvest_implementation_escalates_on_needs_human(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "result.json"
    result.write_text(json.dumps(impl_needs_human_result()))
    rc = pipeline.main(
        ["harvest-implementation", "--result", str(result), "--pre-label", "ai:available"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "escalate"
    assert out["label"] == "ai:needs-human-decision"


def test_cli_harvest_implementation_escalates_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = pipeline.main(
        [
            "harvest-implementation",
            "--result",
            str(tmp_path / "nope.json"),
            "--pre-label",
            "ai:available",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "escalate"
    assert out["sentinel"] is None


def test_cli_validate_marker_fails_closed_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = pipeline.main(
        ["validate-marker", "--dispatch", str(tmp_path / "nope.json"), "--pr", "1"]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["valid"] is False


# --- parse_implementation_result ---


def test_parse_implementation_result_extracts_complete_sentinel() -> None:
    result = {"result": "All done. IMPLEMENTATION_COMPLETE", "is_error": False}
    assert pipeline.parse_implementation_result(result) == "IMPLEMENTATION_COMPLETE"


def test_parse_implementation_result_extracts_needs_human_sentinel() -> None:
    result = {"result": "Blocked on design. NEEDS_HUMAN_DECISION", "is_error": False}
    assert pipeline.parse_implementation_result(result) == "NEEDS_HUMAN_DECISION"


def test_parse_implementation_result_returns_none_when_no_sentinel() -> None:
    result = {"result": "I did some stuff but forgot the sentinel.", "is_error": False}
    assert pipeline.parse_implementation_result(result) is None


def test_parse_implementation_result_returns_none_for_non_dict() -> None:
    assert pipeline.parse_implementation_result("not a dict") is None
    assert pipeline.parse_implementation_result(None) is None


def test_parse_implementation_result_returns_none_for_missing_result_key() -> None:
    assert pipeline.parse_implementation_result({"output": "IMPLEMENTATION_COMPLETE"}) is None


def test_parse_implementation_result_returns_none_for_non_string_result() -> None:
    assert pipeline.parse_implementation_result({"result": 42}) is None


def test_parse_implementation_result_prefers_last_sentinel_on_multiple() -> None:
    result = {
        "result": "NEEDS_HUMAN_DECISION ... actually fixed it. IMPLEMENTATION_COMPLETE",
        "is_error": False,
    }
    assert pipeline.parse_implementation_result(result) == "IMPLEMENTATION_COMPLETE"


# --- valid_marker: implement stage ---


def test_valid_marker_accepts_implement_stage_marker() -> None:
    m = impl_marker()
    assert pipeline.valid_marker(m) is True


def test_valid_marker_accepts_implement_stage_lease() -> None:
    m = impl_lease()
    assert pipeline.valid_marker(m) is True


def test_valid_marker_rejects_implement_marker_with_review_pre_label() -> None:
    m = impl_marker(pre_label="ai:implemented")
    assert pipeline.valid_marker(m) is False


def test_valid_marker_rejects_review_marker_with_implement_pre_label() -> None:
    m = marker(pre_label="ai:available")
    assert pipeline.valid_marker(m) is False


def test_valid_marker_cross_checks_implement_stage() -> None:
    m = impl_marker(pr_number=5)
    assert pipeline.valid_marker(m, expected_pr=5, expected_stage="implement") is True
    assert pipeline.valid_marker(m, expected_stage="review") is False


def test_valid_marker_accepts_implement_marker_with_claude_ds_engine() -> None:
    """``claude-ds`` is a valid engine value for implement-stage markers."""
    assert pipeline.valid_marker(impl_marker(engine="claude-ds"),
                                 expected_stage="implement") is True


def test_valid_marker_accepts_implement_lease_with_claude_ds_engine() -> None:
    """``claude-ds`` is a valid engine value for implement-stage leases."""
    assert pipeline.valid_marker(impl_lease(engine="claude-ds"),
                                 expected_stage="implement") is True


def test_valid_marker_accepts_stall_limits_on_engine_markers() -> None:
    """Implementer and fixer markers record the stall limits they run under."""
    limits = {"stall_seconds": 900, "stall_startup_grace_seconds": 0}

    assert pipeline.valid_marker(impl_marker(**limits)) is True
    assert pipeline.valid_marker(impl_lease(**limits)) is True


@pytest.mark.parametrize("limits", [
    {"stall_seconds": True},
    {"stall_seconds": 900.0},
    {"stall_seconds": "900"},
    {"stall_seconds": 0},
    {"stall_startup_grace_seconds": -1},
    {"stall_startup_grace_seconds": None},
])
def test_valid_marker_rejects_unusable_stall_limits(
    limits: dict[str, object],
) -> None:
    """A stall limit that is not a usable integer is not this writer's marker."""
    assert pipeline.valid_marker(impl_marker(**limits)) is False


def test_valid_marker_rejects_stall_limits_on_a_review_marker() -> None:
    """The reviewer has no progress contract, so only corruption writes these."""
    assert pipeline.valid_marker(marker(stall_seconds=900)) is False


# --- reconcile_dispatch: implement stage ---


def test_reconcile_leaves_manually_claimed_implementing_issue_alone() -> None:
    # CLAUDE.md tells contributors to set ai:implementing by hand before
    # writing code. A tick that cannot see a marker for it must leave it be.
    d = pipeline.reconcile_dispatch(
        current_label="ai:implementing",
        marker_valid=False,
        classify_state="",
        pre_label="ai:available",
        in_flight_label="ai:implementing",
    )
    assert d["action"] == "noop"
    assert d["label"] == ""


def test_reconcile_noops_on_markerless_implementing_issue() -> None:
    # The documented manual pickup puts an issue here by hand; a missing
    # marker must never move it (issue #592).
    d = pipeline.reconcile_dispatch(
        current_label="ai:implementing",
        marker_valid=False,
        classify_state="",
        pre_label="ai:available",
        in_flight_label="ai:implementing",
    )
    assert d["action"] == "noop"
    assert d["label"] == ""


def test_reconcile_harvests_completed_implement_in_custody() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:implementing",
        marker_valid=True,
        classify_state="complete",
        pre_label="ai:available",
        in_flight_label="ai:implementing",
    )
    assert d["action"] == "harvest"


def test_reconcile_reverts_stale_implement_in_custody() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:implementing",
        marker_valid=True,
        classify_state="stale",
        pre_label="ai:available",
        in_flight_label="ai:implementing",
    )
    assert d["action"] == "revert"
    assert d["label"] == "ai:available"


# --- decide_implementation_harvest ---


def test_decide_implementation_harvest_creates_pr_on_complete() -> None:
    d = pipeline.decide_implementation_harvest(
        result_obj=impl_complete_result(),
        pre_label="ai:available",
    )
    assert d["action"] == "create_pr"
    assert d["sentinel"] == "IMPLEMENTATION_COMPLETE"


def test_decide_implementation_harvest_escalates_on_needs_human() -> None:
    d = pipeline.decide_implementation_harvest(
        result_obj=impl_needs_human_result(),
        pre_label="ai:available",
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["sentinel"] == "NEEDS_HUMAN_DECISION"


def test_decide_implementation_harvest_retries_session_limit() -> None:
    d = pipeline.decide_implementation_harvest(
        result_obj={
            "result": "You've hit your session limit · resets 2:40am Europe/London",
        },
        pre_label="ai:available",
    )

    assert d == {
        "action": "retry",
        "label": "ai:available",
        "sentinel": None,
        "failure_class": "quota_or_limit",
    }


def test_decide_implementation_harvest_sentinel_precedes_transient_fields() -> None:
    """A completed implementation is not requeued for surrounding limit prose."""
    d = pipeline.decide_implementation_harvest(
        result_obj={
            "result": "IMPLEMENTATION_COMPLETE\nHandled rate limit exceeded errors.",
            "api_error_status": 429,
        },
        pre_label="ai:available",
    )

    assert d["action"] == "create_pr"
    assert d["sentinel"] == "IMPLEMENTATION_COMPLETE"


def test_classify_engine_result_failure_recognizes_status_429() -> None:
    assert pipeline.classify_engine_result_failure(
        {"result": "request failed", "api_error_status": 429},
    ) == "quota_or_limit"


def test_classify_engine_result_failure_does_not_use_is_error_alone() -> None:
    assert pipeline.classify_engine_result_failure(
        {"result": "provider request failed", "is_error": True},
    ) is None


@pytest.mark.parametrize(
    "subtype",
    ["rate_limit_error", "quota_exceeded", "usage_limit_error"],
)
def test_classify_engine_result_failure_recognizes_limit_subtype(
    subtype: str,
) -> None:
    assert pipeline.classify_engine_result_failure(
        {"result": "request failed", "subtype": subtype},
    ) == "quota_or_limit"


def test_classify_engine_result_failure_recognizes_quota_phrase() -> None:
    assert pipeline.classify_engine_result_failure(
        {"result": "API quota exceeded for this billing period"},
    ) == "quota_or_limit"


@pytest.mark.parametrize(
    "result_text",
    ["Usage limit reached", "Rate limit exceeded; retry later"],
)
def test_classify_engine_result_failure_recognizes_limit_phrase_variants(
    result_text: str,
) -> None:
    assert pipeline.classify_engine_result_failure(
        {"result": result_text},
    ) == "quota_or_limit"


def test_decide_implementation_harvest_escalates_on_missing_sentinel() -> None:
    d = pipeline.decide_implementation_harvest(
        result_obj={"result": "no sentinel here"},
        pre_label="ai:available",
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["sentinel"] is None


def test_decide_implementation_harvest_escalates_on_unparseable_result() -> None:
    d = pipeline.decide_implementation_harvest(
        result_obj=None,
        pre_label="ai:available",
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["sentinel"] is None


# --- validate_completion_envelope ---


def test_validate_envelope_accepts_valid_success() -> None:
    assert pipeline.validate_completion_envelope(envelope()) is True


def test_validate_envelope_rejects_non_zero_exit() -> None:
    assert pipeline.validate_completion_envelope(envelope(exit_status=1)) is False


def test_validate_failure_envelope_accepts_bound_nonzero_invalid_result() -> None:
    assert pipeline.validate_failure_envelope(
        envelope(
            dispatch_id="d1",
            exit_status=1,
            result_valid=False,
            error_class="EngineExitError",
        ),
        expected_dispatch_id="d1",
    ) is True


@pytest.mark.parametrize(
    "overrides,remove_field",
    [
        ({"dispatch_id": "other", "exit_status": 1, "result_valid": False,
          "error_class": "EngineExitError"}, None),
        ({"dispatch_id": "d1", "exit_status": 0, "result_valid": False,
          "error_class": "EngineExitError"}, None),
        ({"dispatch_id": "d1", "exit_status": 1, "result_valid": True,
          "error_class": "EngineExitError"}, None),
        ({"dispatch_id": "d1", "exit_status": 1, "result_valid": False,
          "error_class": "EngineExitError"}, "wrapper_pid"),
        ({"dispatch_id": "d1", "exit_status": 1, "result_valid": False}, None),
        ({"dispatch_id": "d1", "exit_status": 1, "result_valid": False,
          "error_class": ""}, None),
    ],
)
def test_validate_failure_envelope_rejects_mismatched_or_malformed(
    overrides: dict[str, object],
    remove_field: str | None,
) -> None:
    failure_envelope = envelope(**overrides)
    if remove_field is not None:
        del failure_envelope[remove_field]
    assert pipeline.validate_failure_envelope(
        failure_envelope,
        expected_dispatch_id="d1",
    ) is False


def test_success_envelope_rejects_failure_error_class() -> None:
    assert pipeline.validate_completion_envelope(
        envelope(error_class="EngineExitError"),
    ) is False


def test_validate_envelope_rejects_missing_fields() -> None:
    for field in ("dispatch_id", "exit_status", "wrapper_pid", "start_time"):
        e = envelope()
        del e[field]
        assert pipeline.validate_completion_envelope(e) is False, f"missing {field}"


def test_validate_envelope_rejects_wrong_dispatch_id() -> None:
    assert pipeline.validate_completion_envelope(
        envelope(), expected_dispatch_id="other-id"
    ) is False


def test_validate_envelope_accepts_matching_dispatch_id() -> None:
    assert pipeline.validate_completion_envelope(
        envelope(dispatch_id="d1"), expected_dispatch_id="d1"
    ) is True


def test_validate_envelope_rejects_exit_zero_with_invalid_result() -> None:
    """An envelope with exit_status=0 but result_valid=False must not validate."""
    assert pipeline.validate_completion_envelope(envelope(result_valid=False)) is False


def test_validate_envelope_rejects_missing_result_valid() -> None:
    """An envelope missing the result_valid field is structurally incomplete."""
    e = envelope()
    del e["result_valid"]
    assert pipeline.validate_completion_envelope(e) is False


def test_validate_envelope_rejects_non_dict() -> None:
    assert pipeline.validate_completion_envelope("not a dict") is False
    assert pipeline.validate_completion_envelope(None) is False


def test_validate_envelope_rejects_bool_exit_status() -> None:
    """False == 0 in Python, so a bool exit_status must be rejected explicitly."""
    assert pipeline.validate_completion_envelope(envelope(exit_status=False)) is False
    assert pipeline.validate_completion_envelope(envelope(exit_status=True)) is False


def test_validate_envelope_rejects_bool_wrapper_pid() -> None:
    """A boolean wrapper_pid is not a real process handle."""
    assert pipeline.validate_completion_envelope(envelope(wrapper_pid=False)) is False
    assert pipeline.validate_completion_envelope(envelope(wrapper_pid=True)) is False


def test_validate_envelope_rejects_non_positive_wrapper_pid() -> None:
    assert pipeline.validate_completion_envelope(envelope(wrapper_pid=0)) is False
    assert pipeline.validate_completion_envelope(envelope(wrapper_pid=-1)) is False
    assert pipeline.validate_completion_envelope(envelope(wrapper_pid="5")) is False


def test_validate_envelope_rejects_non_numeric_start_time() -> None:
    assert pipeline.validate_completion_envelope(envelope(start_time="not-a-time")) is False
    assert pipeline.validate_completion_envelope(envelope(start_time=False)) is False
    assert pipeline.validate_completion_envelope(envelope(start_time=True)) is False


def test_validate_envelope_rejects_non_finite_start_time() -> None:
    assert pipeline.validate_completion_envelope(envelope(start_time=float("nan"))) is False
    assert pipeline.validate_completion_envelope(envelope(start_time=float("inf"))) is False


def test_validate_envelope_rejects_unknown_keys() -> None:
    e = envelope(surprise="x")
    assert pipeline.validate_completion_envelope(e) is False


# --- verify_process_identity ---


def test_verify_identity_matches_same_start_time() -> None:
    assert pipeline.verify_process_identity(1000.5, 1000.5) == "matched"


def test_verify_identity_rejects_pid_reuse() -> None:
    assert pipeline.verify_process_identity(1000.5, 2000.0) == "mismatched"


def test_verify_identity_returns_unknown_for_none_live_start() -> None:
    assert pipeline.verify_process_identity(1000.5, None) == "unknown"


def test_verify_identity_tolerates_small_clock_skew() -> None:
    assert pipeline.verify_process_identity(1000.5, 1001.0, tolerance=2.0) == "matched"
    assert pipeline.verify_process_identity(1000.5, 1003.0, tolerance=2.0) == "mismatched"


# --- valid_marker: dispatch_id + process identity ---


def test_valid_marker_accepts_owned_marker_with_identity() -> None:
    assert pipeline.valid_marker(owned_marker()) is True


def test_valid_marker_accepts_owned_lease_with_dispatch_id() -> None:
    assert pipeline.valid_marker(owned_lease()) is True


def test_valid_marker_requires_dispatch_id() -> None:
    m = owned_marker()
    del m["dispatch_id"]
    assert pipeline.valid_marker(m) is False


def test_valid_marker_rejects_empty_dispatch_id() -> None:
    assert pipeline.valid_marker(owned_marker(dispatch_id="")) is False


def test_valid_marker_requires_start_time_with_pid() -> None:
    m = owned_marker()
    del m["start_time"]
    assert pipeline.valid_marker(m) is False


def test_valid_marker_requires_pgid_with_pid() -> None:
    m = owned_marker()
    del m["pgid"]
    assert pipeline.valid_marker(m) is False


def test_valid_marker_rejects_start_time_on_lease() -> None:
    assert pipeline.valid_marker(owned_lease(start_time=1000.0)) is False


def test_valid_marker_rejects_pgid_on_lease() -> None:
    assert pipeline.valid_marker(owned_lease(pgid=5)) is False


def test_valid_marker_rejects_non_finite_start_time() -> None:
    assert pipeline.valid_marker(owned_marker(start_time=float("nan"))) is False
    assert pipeline.valid_marker(owned_marker(start_time=float("inf"))) is False


def test_valid_marker_rejects_non_positive_pgid() -> None:
    assert pipeline.valid_marker(owned_marker(pgid=0)) is False
    assert pipeline.valid_marker(owned_marker(pgid=-1)) is False


def test_valid_marker_rejects_bool_pgid() -> None:
    assert pipeline.valid_marker(owned_marker(pgid=True)) is False


# --- CLI: validate-envelope ---


def test_cli_validate_envelope_reports_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "completion.json"
    env_file.write_text(json.dumps(envelope()))
    rc = pipeline.main(["validate-envelope", "--envelope", str(env_file)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_cli_validate_envelope_checks_dispatch_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "completion.json"
    env_file.write_text(json.dumps(envelope(dispatch_id="d1")))
    rc = pipeline.main([
        "validate-envelope", "--envelope", str(env_file), "--dispatch-id", "d2"
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_cli_validate_envelope_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = pipeline.main(["validate-envelope", "--envelope", str(tmp_path / "nope.json")])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["valid"] is False


# --- CLI: verify-identity ---


def test_cli_verify_identity_matching(capsys: pytest.CaptureFixture[str]) -> None:
    rc = pipeline.main([
        "verify-identity", "--recorded-start", "1000.0", "--live-start", "1000.0"
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["identity"] == "matched"


def test_cli_verify_identity_reused_pid(capsys: pytest.CaptureFixture[str]) -> None:
    rc = pipeline.main([
        "verify-identity", "--recorded-start", "1000.0", "--live-start", "2000.0"
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["identity"] == "mismatched"


def test_cli_verify_identity_missing_process(capsys: pytest.CaptureFixture[str]) -> None:
    rc = pipeline.main([
        "verify-identity", "--recorded-start", "1000.0", "--live-start", "-1"
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["identity"] == "unknown"


# --- CLI: classify-dispatch with envelope ---


def test_cli_classify_dispatch_with_envelope_and_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dispatch = tmp_path / "dispatch.json"
    dispatch.write_text(json.dumps({"pid": 1, "deadline": 50}))
    rc = pipeline.main([
        "classify-dispatch",
        "--dispatch", str(dispatch),
        "--now", "10",
        "--pid-alive", "1",
        "--result-exists", "1",
        "--envelope-valid", "1",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["state"] == "complete"


# --- classify_dispatch with envelope ---


def test_classify_not_complete_when_envelope_valid_but_result_missing() -> None:
    """A valid envelope without the result file is not harvestable — treat as stale."""
    assert pipeline.classify_dispatch(
        now=10, deadline=50, pid_alive=False,
        result_exists=False, envelope_valid=True,
    ) == "stale"


def test_classify_complete_on_envelope_with_result() -> None:
    assert pipeline.classify_dispatch(
        now=10, deadline=50, pid_alive=True,
        result_exists=True, envelope_valid=True,
    ) == "complete"


def test_classify_complete_on_envelope_even_if_pid_alive() -> None:
    assert pipeline.classify_dispatch(
        now=10, deadline=50, pid_alive=True,
        result_exists=True, envelope_valid=True,
    ) == "complete"


# --- parse_fix_result ---


def test_parse_fix_result_extracts_fixes_pushed_sentinel() -> None:
    result = {"result": "All fixes applied. FIXES_PUSHED_RECHECK_NEEDED", "is_error": False}
    assert pipeline.parse_fix_result(result) == "FIXES_PUSHED_RECHECK_NEEDED"


def test_parse_fix_result_extracts_needs_human_sentinel() -> None:
    result = {"result": "Finding out of scope. NEEDS_HUMAN_DECISION", "is_error": False}
    assert pipeline.parse_fix_result(result) == "NEEDS_HUMAN_DECISION"


def test_parse_fix_result_returns_none_when_no_sentinel() -> None:
    result = {"result": "I did some work but forgot the sentinel.", "is_error": False}
    assert pipeline.parse_fix_result(result) is None


def test_parse_fix_result_returns_none_for_non_dict() -> None:
    assert pipeline.parse_fix_result("not a dict") is None
    assert pipeline.parse_fix_result(None) is None


def test_parse_fix_result_returns_none_for_missing_result_key() -> None:
    assert pipeline.parse_fix_result({"output": "FIXES_PUSHED_RECHECK_NEEDED"}) is None


def test_parse_fix_result_prefers_last_sentinel_on_multiple() -> None:
    result = {
        "result": "NEEDS_HUMAN_DECISION ... actually fixed it. FIXES_PUSHED_RECHECK_NEEDED",
        "is_error": False,
    }
    assert pipeline.parse_fix_result(result) == "FIXES_PUSHED_RECHECK_NEEDED"


def test_parse_fix_result_does_not_recognise_implementer_sentinel() -> None:
    result = {"result": "All done. IMPLEMENTATION_COMPLETE", "is_error": False}
    assert pipeline.parse_fix_result(result) is None


# --- decide_fix_harvest ---


def test_decide_fix_harvest_rechecks_on_fixes_pushed() -> None:
    d = pipeline.decide_fix_harvest(
        result_obj=fix_pushed_result(),
        pre_label="ai:changes-requested",
    )
    assert d["action"] == "recheck"
    assert d["label"] == "ai:revised"
    assert d["sentinel"] == "FIXES_PUSHED_RECHECK_NEEDED"


def test_decide_fix_harvest_escalates_on_needs_human() -> None:
    d = pipeline.decide_fix_harvest(
        result_obj=fix_needs_human_result(),
        pre_label="ai:changes-requested",
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["sentinel"] == "NEEDS_HUMAN_DECISION"


def test_decide_fix_harvest_retries_session_limit() -> None:
    d = pipeline.decide_fix_harvest(
        result_obj={"result": "You've hit your session limit"},
        pre_label="ai:changes-requested",
    )

    assert d == {
        "action": "retry",
        "label": "ai:changes-requested",
        "sentinel": None,
        "failure_class": "quota_or_limit",
    }


def test_decide_fix_harvest_sentinel_precedes_transient_fields() -> None:
    """A completed fix remains authoritative over transient-looking metadata."""
    d = pipeline.decide_fix_harvest(
        result_obj={
            "result": "FIXES_PUSHED_RECHECK_NEEDED\nHandled quota exceeded errors.",
            "subtype": "rate_limit_error",
        },
        pre_label="ai:changes-requested",
    )

    assert d["action"] == "recheck"
    assert d["sentinel"] == "FIXES_PUSHED_RECHECK_NEEDED"


def test_decide_fix_harvest_escalates_on_missing_sentinel() -> None:
    d = pipeline.decide_fix_harvest(
        result_obj={"result": "no sentinel here"},
        pre_label="ai:changes-requested",
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["sentinel"] is None


def test_decide_fix_harvest_escalates_on_unparseable_result() -> None:
    d = pipeline.decide_fix_harvest(
        result_obj=None,
        pre_label="ai:changes-requested",
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["sentinel"] is None


def test_decide_fix_harvest_escalates_when_head_unchanged() -> None:
    """FIXES_PUSHED with an unchanged live head is contradictory output.

    A fast-forward push always advances the head and force-push is not
    permitted, so a fixer claiming success while the head equals the
    dispatch-pinned head cannot have pushed. Trusting the sentinel would
    relabel to ai:revised and re-enter an identical review round whose
    comment is replay-suppressed — freezing the comment-derived iteration
    count below MAX_ITERATIONS and looping spend forever.
    """
    d = pipeline.decide_fix_harvest(
        result_obj=fix_pushed_result(),
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="a" * 40,
    )
    assert d["action"] == "escalate"
    assert d["label"] == "ai:needs-human-decision"
    assert d["sentinel"] == "FIXES_PUSHED_RECHECK_NEEDED"
    assert d["head_advanced"] is False


def test_decide_fix_harvest_rechecks_when_head_advanced() -> None:
    d = pipeline.decide_fix_harvest(
        result_obj=fix_pushed_result(),
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
    )
    assert d["action"] == "recheck"
    assert d["label"] == "ai:revised"


def test_decide_fix_harvest_head_check_disabled_without_refs() -> None:
    """Empty pinned or current head disables the push check (legacy markers)."""
    for pinned, current in (("", "b" * 40), ("a" * 40, ""), ("", "")):
        d = pipeline.decide_fix_harvest(
            result_obj=fix_pushed_result(),
            pre_label="ai:changes-requested",
            pinned_head=pinned,
            current_head=current,
        )
        assert d["action"] == "recheck", (pinned, current)


def test_decide_fix_harvest_head_check_ignores_needs_human() -> None:
    """The push check gates only the FIXES_PUSHED claim, not other sentinels."""
    d = pipeline.decide_fix_harvest(
        result_obj=fix_needs_human_result(),
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="a" * 40,
    )
    assert d["action"] == "escalate"
    assert d["sentinel"] == "NEEDS_HUMAN_DECISION"
    assert "head_advanced" not in d


def test_decide_fix_harvest_reports_a_published_pr_body_revision() -> None:
    """A recheck that carried a publishable revision says so in its comment."""
    d = pipeline.decide_fix_harvest(
        result_obj=fix_pushed_result(),
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
        body_revision="ready",
    )

    assert "- PR body: replaced with the fixer's revision" in str(
        d["comment_body"],
    )


def test_decide_fix_harvest_withholds_a_revision_from_a_non_recheck() -> None:
    """A ready revision on any non-recheck outcome reports as not applied.

    The caller publishes the body only on ``recheck``, so a comment that said
    "replaced" on an escalation would describe a write that never happened —
    and the escalation is precisely the outcome where the head the replacement
    describes may not exist.
    """
    d = pipeline.decide_fix_harvest(
        result_obj=fix_needs_human_result(),
        pre_label="ai:changes-requested",
        body_revision="ready",
    )

    assert d["action"] == "escalate"
    body = str(d["comment_body"])
    assert "- PR body: left unchanged — the fixer offered a revision" in body
    assert "replaced with the fixer's revision" not in body


@pytest.mark.parametrize("state", ["conflict", "invalid", "unstamped", "unavailable"])
def test_decide_fix_harvest_states_why_a_revision_was_not_published(
    state: str,
) -> None:
    """Every refusal names a reason a reviewer can act on.

    A body silently left alone is indistinguishable from a pipeline that never
    looked at it, which is the whole defect this path exists to close.
    """
    d = pipeline.decide_fix_harvest(
        result_obj=fix_pushed_result(),
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
        body_revision=state,
    )

    body = str(d["comment_body"])
    assert "- PR body: left unchanged — " in body, (
        f"the {state} outcome published no reason: {body}"
    )
    assert "replaced with the fixer's revision" not in body


def test_decide_fix_harvest_says_nothing_when_no_revision_was_offered() -> None:
    """The common run keeps the comment it had before this path existed."""
    d = pipeline.decide_fix_harvest(
        result_obj=fix_pushed_result(),
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
    )

    assert "PR body" not in str(d["comment_body"])


# --- fixer harvest comment ---


def test_decide_fix_harvest_recheck_comment_names_both_heads() -> None:
    """The recheck comment is self-describing about the push it recorded."""
    d = pipeline.decide_fix_harvest(
        result_obj={"result": "FIXES_PUSHED_RECHECK_NEEDED\nRewired the guard."},
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
    )

    assert d["post_comment"] is True
    body = str(d["comment_body"])
    assert body.startswith(pipeline.FIX_MARKER)
    assert f"head `{'a' * 40}` → `{'b' * 40}`" in body
    assert "`recheck` → `ai:revised`" in body
    assert "`FIXES_PUSHED_RECHECK_NEEDED`" in body


def test_decide_fix_harvest_recheck_comment_states_what_was_fixed() -> None:
    """A recheck must say what was fixed, not just that a head moved.

    The PR comment is the only surviving record of a successful fixer run: the
    recheck path does not archive harvest evidence, and its scratch — holding
    ``result.json`` — is deleted on cleanup. Publishing the bounded engine
    report here is what makes the run auditable from GitHub at all.
    """
    d = pipeline.decide_fix_harvest(
        result_obj={"result": "FIXES_PUSHED_RECHECK_NEEDED\nRewired the guard."},
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
    )

    body = str(d["comment_body"])
    assert "Engine report" in body
    assert "Rewired the guard." in body


@pytest.mark.parametrize(
    ("stage", "result_obj", "expected"),
    [
        (
            "review",
            {"verdict": "ready_for_human", "findings": []},
            "ready_for_human",
        ),
        (
            "implement",
            {"result": "IMPLEMENTATION_COMPLETE\nCreated PR #42."},
            "IMPLEMENTATION_COMPLETE",
        ),
        (
            "fix",
            {"result": "FIXES_PUSHED_RECHECK_NEEDED\nUpdated the guard."},
            "FIXES_PUSHED_RECHECK_NEEDED",
        ),
        ("review", {"verdict": "ready_for_human"}, "unrecognized"),
        ("fix", {"result": "finished without a sentinel"}, "unrecognized"),
    ],
)
def test_held_completion_outcome_uses_each_stages_validated_protocol(
    stage: str,
    result_obj: object,
    expected: str,
) -> None:
    """Held completions name only outcomes trusted by that stage's parser."""
    assert pipeline.held_completion_outcome(stage, result_obj) == expected


def test_held_completion_outcome_refuses_a_stage_with_no_parser() -> None:
    """An unmapped stage has no validated vocabulary to publish from."""
    with pytest.raises(ValueError, match="unknown pipeline stage"):
        pipeline.held_completion_outcome("plan", {"result": "anything"})


def test_render_held_completion_comment_names_the_unharvested_outcome() -> None:
    """The remote record names the result without inventing hold provenance."""
    body = pipeline.render_held_completion_comment(
        stage="fix",
        outcome=pipeline.SENTINEL_FIXES_PUSHED,
    )

    assert body.startswith(pipeline.ESCALATION_MARKER)
    assert "workflow hold remains authoritative" in body
    assert "human" not in body.lower()
    assert "completed result is not being harvested" in body
    assert "- Engine outcome: `FIXES_PUSHED_RECHECK_NEEDED`" in body
    assert "harvest-evidence archive" in body


def test_render_held_completion_comment_rejects_cross_stage_outcomes() -> None:
    """A renderer cannot publish an implementer sentinel as a fixer outcome."""
    with pytest.raises(ValueError, match="unknown held completion outcome"):
        pipeline.render_held_completion_comment(
            stage="fix",
            outcome=pipeline.SENTINEL_IMPL_COMPLETE,
        )


def test_render_transient_escalation_comment_distinguishes_its_two_causes() -> None:
    """Each transient terminal transition names the cause that produced it.

    A label that turned red because the engine's quota ran out and one that
    turned red because the controller could not write its own counter demand
    different operator responses, so the comment cannot flatten them into one
    "escalated" line. Only the exhaustion path archives locally, so only it
    points at the archive.
    """
    exhausted = pipeline.render_transient_escalation_comment(
        stage="fix",
        outcome=pipeline.OUTCOME_TRANSIENT_EXHAUSTED,
        failure_class="quota_or_limit",
        attempts=3,
        evidence_archived=True,
    )
    unwritable = pipeline.render_transient_escalation_comment(
        stage="implement",
        outcome=pipeline.OUTCOME_ATTEMPT_STATE_ERROR,
        failure_class="quota_or_limit",
    )

    assert exhausted.startswith(pipeline.ESCALATION_MARKER)
    assert "transient failure on every attempt" in exhausted
    assert "- Attempts: 3 (budget exhausted)" in exhausted
    assert "harvest-evidence archive" in exhausted
    assert f"`{pipeline.LABEL_NEEDS_HUMAN}`" in exhausted

    assert "attempt counter could not be written" in unwritable
    # No retry budget was tracked and nothing was archived, so the comment
    # promises neither.
    assert "Attempts:" not in unwritable
    assert "harvest-evidence archive" not in unwritable


def test_render_dispatch_escalation_comment_distinguishes_its_two_causes() -> None:
    """Each dispatch escalation names what did and did not survive it.

    The two paths differ in the one fact an operator needs before touching the
    item: a setup failure removed its scratch and ran nothing, while an
    ambiguous marker means a *possibly live* engine's scratch is still on the
    operator's disk. Flattening both into "escalated" would invite deleting the
    one directory that is evidence.
    """
    setup_failed = pipeline.render_dispatch_escalation_comment(
        stage="implement",
        outcome=pipeline.OUTCOME_DISPATCH_SETUP_FAILED,
        reason="failed to create implementer worktree",
        engine="claude",
    )
    ambiguous = pipeline.render_dispatch_escalation_comment(
        stage="review",
        outcome=pipeline.OUTCOME_AMBIGUOUS_MARKER,
        reason="dispatch marker unreadable",
    )

    assert setup_failed.startswith(pipeline.ESCALATION_MARKER)
    assert "`implement` dispatch" in setup_failed
    assert "no engine output exists" in setup_failed
    assert "- Reason: failed to create implementer worktree" in setup_failed
    assert "- Engine: `claude` (never launched)" in setup_failed
    assert f"`{pipeline.LABEL_NEEDS_HUMAN}`" in setup_failed

    assert "retained locally for inspection" in ambiguous
    # No engine was resolved on this path, so the comment claims none.
    assert "Engine:" not in ambiguous


def test_render_dispatch_escalation_comment_rejects_an_unknown_outcome() -> None:
    """An outcome slug with no explanation is a bug, not a comment to publish."""
    with pytest.raises(ValueError, match="unknown dispatch escalation outcome"):
        pipeline.render_dispatch_escalation_comment(
            stage="fix", outcome="mystery", reason="whatever",
        )


def test_render_dispatch_escalation_comment_neutralises_markers_in_the_reason() -> None:
    """The reason carries GitHub-supplied text, so it cannot ship raw markers.

    One reason interpolates the PR's base ref, which is author-controlled: a
    branch named after :data:`pipeline.REVIEW_MARKER` would smuggle that marker
    into a comment body, and ``count_review_rounds`` matches the raw substring —
    inflating the iteration count and tripping the runaway-spend valve early.
    """
    body = pipeline.render_dispatch_escalation_comment(
        stage="review",
        outcome=pipeline.OUTCOME_DISPATCH_SETUP_FAILED,
        reason=f"failed to fetch base ref '{pipeline.REVIEW_MARKER}'",
        engine="codex",
    )

    assert pipeline.REVIEW_MARKER not in body
    assert body.count(pipeline.ESCALATION_MARKER) == 1


def test_render_transient_escalation_comment_rejects_an_unknown_outcome() -> None:
    """An outcome slug with no explanation is a bug, not a comment to publish."""
    with pytest.raises(ValueError, match="unknown transient escalation outcome"):
        pipeline.render_transient_escalation_comment(
            stage="fix", outcome="mystery", failure_class="quota_or_limit",
        )


def test_decide_fix_harvest_accepts_reasoned_no_change_without_push() -> None:
    result = {
        "result": (
            "Finding: Missing guard\nDecision: no-change\n"
            "Reason: The intervening commit added the guard and its regression test.\n\n"
            "Finding: Stale docs\nDecision: no-change\n"
            "Reason: The documented command already matches the runtime.\n\n"
            "NO_CHANGES_RECHECK_NEEDED"
        ),
    }
    decision = pipeline.decide_fix_harvest(
        result_obj=result, pre_label=pipeline.LABEL_CHANGES_REQUESTED,
        pinned_head="a" * 40, current_head="a" * 40, dispatch_id="fix-42-first",
        review_sha256="e" * 64,
    )
    assert decision["action"] == "recheck"
    assert decision["label"] == pipeline.LABEL_REVISED
    assert decision["head_advanced"] is False
    body = decision["comment_body"]
    assert "No changes needed — re-review requested" in body
    assert "Fixes pushed" not in body
    assert "intervening commit added the guard" in body
    assert "documented command already matches" in body
    assert "<!-- pr-pipeline:no-change " in body


@pytest.mark.parametrize("summary", [
    "", "Nothing to fix.",
    "Finding: guard\nReason: Already present.",
    "Finding: guard\nDecision: no-change\nReason:",
    "Finding: \nDecision: no-change\nReason: Already present.",
    "Finding: guard\nDecision: repair\nReason: Changed it.",
    "Finding: guard\nDecision: no-change\nDecision: repair\nReason: Already present.",
    "Finding: guard\nDecision: no-change\nReason: Already present.\n"
    "Finding: docs\nReason: Already current.",
    "Finding: guard\nDecision: no-change\nReason: Already present.\n"
    "Finding: docs\nDecision: replacement\nChange: Rewrote it.",
    "Finding: guard\nDecision: no-change\nReason: " + "x" * 3000,
])
def test_no_change_completion_rejects_incomplete_or_contradictory_summary(summary: str) -> None:
    decision = pipeline.decide_fix_harvest(
        result_obj={"result": f"{summary}\nNO_CHANGES_RECHECK_NEEDED"},
        pre_label=pipeline.LABEL_CHANGES_REQUESTED,
        pinned_head="a" * 40, current_head="a" * 40, dispatch_id="fix-42-first",
        review_sha256="e" * 64,
    )
    assert decision["label"] == pipeline.LABEL_NEEDS_HUMAN
    assert "no-change completion requires" in decision["comment_body"]
    assert "<!-- pr-pipeline:no-change " not in decision["comment_body"]


@pytest.mark.parametrize("pinned,current,dispatch", [
    ("", "", "fix-42-first"), ("a", "", "fix-42-first"),
    ("a", "b", "fix-42-first"), ("a", "a", ""),
])
def test_no_change_completion_requires_unchanged_head_and_dispatch_provenance(
    pinned: str, current: str, dispatch: str,
) -> None:
    decision = pipeline.decide_fix_harvest(
        result_obj={"result": "Finding: guard\nDecision: no-change\nReason: Present.\n"
                    "NO_CHANGES_RECHECK_NEEDED"},
        pre_label=pipeline.LABEL_CHANGES_REQUESTED,
        pinned_head=pinned, current_head=current, dispatch_id=dispatch,
        review_sha256="e" * 64,
    )
    assert decision["label"] == pipeline.LABEL_NEEDS_HUMAN


def test_no_change_rounds_reach_cap_on_unchanged_head_and_replay_once() -> None:
    comments: list[object] = []
    identity = dict(pinned_base="b", current_base="b", pinned_head="a", current_head="a")
    review = {"verdict": "changes_requested", "findings": [
        {"severity": "major", "file": "guard.py", "summary": "Guard still missing"},
    ]}
    fix_round = ""
    for round_index in range(pipeline.MAX_ITERATIONS):
        decision = pipeline.decide_review_harvest(
            review_obj=review, comments=comments, fix_round=fix_round, **identity,
            trusted_author="test-owner",
        )
        assert decision["post_comment"] is True
        comments.append({"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": decision["comment_body"]})
        replay = pipeline.decide_review_harvest(
            review_obj=review, comments=comments, fix_round=fix_round, **identity,
            trusted_author="test-owner",
        )
        assert replay["post_comment"] is False
        assert replay["label"] == decision["label"]
        assert pipeline.count_review_rounds(comments, trusted_author="test-owner") == round_index + 1
        if round_index == pipeline.MAX_ITERATIONS - 1:
            assert decision["label"] == pipeline.LABEL_MAX_ITERATIONS
            break
        assert decision["label"] == pipeline.LABEL_CHANGES_REQUESTED
        fix = pipeline.decide_fix_harvest(
            result_obj={"result": "Finding: guard\nDecision: no-change\nReason: Present.\n"
                        "NO_CHANGES_RECHECK_NEEDED"},
            pre_label=pipeline.LABEL_CHANGES_REQUESTED,
            pinned_head="a", current_head="a", dispatch_id=f"fix-{round_index}",
            # The fixer pins the review it was handed; an unmatched pin would
            # count as a fix from an approved comment.
            review_sha256=hashlib.sha256(decision["comment_body"].encode()).hexdigest(),
        )
        assert fix["label"] == pipeline.LABEL_REVISED
        # Even loss of the local publication receipt can only duplicate this
        # fixer comment, never mint a new round identity.
        comments.extend([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": fix["comment_body"]}] * 2)
        fix_round, reasoning = pipeline.latest_fixer_context(comments, trusted_author="test-owner")
        assert fix_round
        assert "Reason: Present." in reasoning


def test_latest_fixer_context_ignores_quoted_or_escalated_comments() -> None:
    body = pipeline.render_fix_comment(
        action="recheck", label=pipeline.LABEL_REVISED,
        sentinel=pipeline.SENTINEL_FIXES_PUSHED, engine_report="Corrected docs.",
    )
    comments = [
        {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}, {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": "Quoted: " + body},
        {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": pipeline.render_fix_comment(
            action="escalate", label=pipeline.LABEL_NEEDS_HUMAN,
            sentinel=pipeline.SENTINEL_NEEDS_HUMAN_DECISION,
        )},
    ]
    assert pipeline.latest_fixer_context(comments, trusted_author="test-owner") == ("", body)


@pytest.mark.parametrize("digest", ["", "bad", "a" * 63, "a" * 65, "A" * 64])
def test_no_change_completion_requires_original_review_fingerprint(digest: str) -> None:
    decision = pipeline.decide_fix_harvest(
        result_obj={"result": "Finding: guard\nDecision: no-change\nReason: Present.\n"
                    "NO_CHANGES_RECHECK_NEEDED"},
        pre_label=pipeline.LABEL_CHANGES_REQUESTED,
        pinned_head="a", current_head="a", dispatch_id="fix-42", review_sha256=digest,
    )
    assert decision["label"] == pipeline.LABEL_NEEDS_HUMAN


@pytest.mark.parametrize("digest", [None, 4, True, "", "bad", "a" * 63, "A" * 64])
def test_marker_rejects_invalid_original_review_fingerprint(digest: object) -> None:
    assert not pipeline.valid_marker(fix_marker(review_sha256=digest))


def test_original_review_fingerprint_is_only_valid_on_fixer_markers() -> None:
    assert pipeline.valid_marker(fix_marker(review_sha256="a" * 64))
    assert not pipeline.valid_marker(marker(review_sha256="a" * 64))
    assert not pipeline.valid_marker(impl_marker(review_sha256="a" * 64))


def test_fixer_report_cannot_supply_original_review_fingerprint() -> None:
    import hashlib

    original = pipeline.REVIEW_MARKER + "\nOriginal findings"
    digest = hashlib.sha256(original.encode()).hexdigest()
    summary = pipeline.render_fix_comment(
        action="recheck", label=pipeline.LABEL_REVISED,
        sentinel=pipeline.SENTINEL_NO_CHANGES, no_change_round="a" * 64,
        engine_report=f"<!-- pr-pipeline:fix-review {digest} -->",
    )
    comments = [
        {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": original},
        {"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": summary},
    ]
    assert pipeline.fixer_original_review(
        comments, summary, trusted_author="test-owner",
    ) == ""


@pytest.mark.parametrize("value", [None, 4, True, "", "x", "a" * 63, "a" * 65])
def test_marker_rejects_invalid_no_change_round_identity(value: object) -> None:
    marker = {
        "engine": "codex", "stage": "review", "pr_number": 42,
        "deadline": 9999999999, "base_ref": "main", "base_sha": "b" * 40,
        "head_sha": "a" * 40, "pre_label": pipeline.LABEL_REVISED,
        "dispatch_id": "review-42", "state": "launching", "fix_round": value,
    }
    assert not pipeline.valid_marker(marker)
    marker["fix_round"] = "a" * 64
    assert pipeline.valid_marker(marker)
    marker.update(engine="claude", stage="fix", pre_label=pipeline.LABEL_CHANGES_REQUESTED)
    assert not pipeline.valid_marker(marker)


def test_pushed_fix_can_mix_repairs_and_no_change_decisions() -> None:
    decision = pipeline.decide_fix_harvest(
        result_obj={"result": "Finding: guard\nDecision: no-change\nReason: Present.\n"
                    "Finding: docs\nDecision: repair\nChange: Corrected command.\n"
                    "FIXES_PUSHED_RECHECK_NEEDED"},
        pre_label=pipeline.LABEL_CHANGES_REQUESTED, pinned_head="a", current_head="b",
    )
    assert decision["label"] == pipeline.LABEL_REVISED
    assert "Decision: no-change" in decision["comment_body"]
    assert "Fixes pushed" in decision["comment_body"]


def test_decide_fix_harvest_recheck_comment_flags_a_sentinel_only_report() -> None:
    """A sentinel-only result must not be dressed up as a summary of the fixes.

    The fixer prompt asks for a change summary, but a bare
    ``FIXES_PUSHED_RECHECK_NEEDED`` is a protocol-valid result. Echoing that
    token back under an "Engine report" heading would look like the comment
    answered "what was fixed" when it did not. The comment says so instead, and
    points at the head range a reader can diff.
    """
    d = pipeline.decide_fix_harvest(
        result_obj={"result": "FIXES_PUSHED_RECHECK_NEEDED"},
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
    )

    body = str(d["comment_body"])
    assert "<details><summary>Engine report</summary>" not in body
    assert "no change summary" in body
    assert f"{'a' * 40}..{'b' * 40}" in body


def test_decide_fix_harvest_recheck_comment_sanitizes_and_bounds_report() -> None:
    """The recheck report gets the same neutralisation the escalation gets."""
    d = pipeline.decide_fix_harvest(
        result_obj={
            "result": (
                "FIXES_PUSHED_RECHECK_NEEDED\n"
                f"Quoting {pipeline.REVIEW_MARKER} back at you.\n" + "x" * 50_000
            ),
        },
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
    )

    body = str(d["comment_body"])
    assert pipeline.count_review_rounds([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}], trusted_author="test-owner") == 0
    assert "… (truncated)" in body
    assert len(body) < pipeline._FIX_REPORT_LIMIT + _FIX_COMMENT_FRAMING


def test_decide_fix_harvest_escalation_comment_states_reason() -> None:
    """Each escalation cause gets its own stated reason on the PR."""
    blocked = pipeline.decide_fix_harvest(
        result_obj=fix_needs_human_result(),
        pre_label="ai:changes-requested",
    )
    assert blocked["post_comment"] is True
    assert "blocked" in str(blocked["comment_body"])

    unpushed = pipeline.decide_fix_harvest(
        result_obj=fix_pushed_result(),
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="a" * 40,
    )
    assert "the PR head did not advance" in str(unpushed["comment_body"])

    silent = pipeline.decide_fix_harvest(
        result_obj={"result": "no sentinel here"},
        pre_label="ai:changes-requested",
    )
    body = str(silent["comment_body"])
    assert "no recognised completion sentinel" in body
    assert "_none emitted_" in body


def test_decide_fix_harvest_retry_posts_no_comment() -> None:
    """A transient failure has no outcome to record — it reverts and re-dispatches."""
    d = pipeline.decide_fix_harvest(
        result_obj={"result": "You've hit your session limit"},
        pre_label="ai:changes-requested",
    )
    assert d.get("post_comment") is not True
    assert not d.get("comment_body")


def test_fix_comment_neutralises_pipeline_markers_in_engine_text() -> None:
    """Engine-quoted reviewer markers must not inflate the round count.

    ``count_review_rounds`` tallies the iteration cap by matching the raw
    marker substring in a comment body, so a fixer that quotes a reviewer
    comment back at us would otherwise add a phantom round.
    """
    d = pipeline.decide_fix_harvest(
        result_obj={
            "result": (
                f"{pipeline.SENTINEL_NEEDS_HUMAN_DECISION}\n"
                f"Stuck on {pipeline.REVIEW_MARKER} from the reviewer.\n"
                "```\nfenced\n```\n</details>"
            ),
        },
        pre_label="ai:changes-requested",
    )

    body = str(d["comment_body"])
    assert pipeline.count_review_rounds([{"lastEditedAt": None, "editor": None, "includesCreatedEdit": False, "author": {"login": "test-owner"}, "body": body}], trusted_author="test-owner") == 0
    assert body.count("```") == 2, "only the harness fence may remain"
    assert body.count("</details>") == 1


def test_fix_comment_truncates_unbounded_engine_output() -> None:
    """A PR comment is a shared record, not a transcript dump."""
    d = pipeline.decide_fix_harvest(
        result_obj={
            "result": f"{pipeline.SENTINEL_NEEDS_HUMAN_DECISION}\n" + "x" * 50_000,
        },
        pre_label="ai:changes-requested",
    )

    body = str(d["comment_body"])
    assert "… (truncated)" in body
    assert len(body) < pipeline._FIX_REPORT_LIMIT + _FIX_COMMENT_FRAMING


def test_fix_comment_publishes_a_budget_sized_summary_untruncated() -> None:
    """The size the fixer is told to write to must be a size the PR can carry.

    The prompt's budget and this excerpt limit are two ends of one contract: a
    fixer that obeys its stated budget and still gets cut publishes a rationale
    that reads as complete and is not. Equal numbers would guarantee that — the
    completion sentinel rides in the same ``result`` text — so the limit carries
    headroom, and this is the assertion that keeps it there.
    """
    summary = "D" * pipeline.FIX_SUMMARY_BUDGET
    d = pipeline.decide_fix_harvest(
        result_obj={
            "result": f"{summary}\n\n{pipeline.SENTINEL_FIXES_PUSHED}",
        },
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="b" * 40,
    )

    body = str(d["comment_body"])
    assert "… (truncated)" not in body, (
        "a summary written to the budget the fixer prompt states was truncated "
        "by the harvest that publishes it"
    )
    assert summary in body


def test_fix_comment_omits_report_section_when_result_text_absent() -> None:
    d = pipeline.decide_fix_harvest(
        result_obj={"result": "", "subtype": "success"},
        pre_label="ai:changes-requested",
    )

    body = str(d["comment_body"])
    assert "Engine report" not in body
    assert "`escalate` → `ai:needs-human-decision`" in body


def test_fix_comment_renders_unknown_heads_without_marker_provenance() -> None:
    """A marker predating head pinning still yields a readable record."""
    d = pipeline.decide_fix_harvest(
        result_obj=fix_pushed_result(),
        pre_label="ai:changes-requested",
    )

    assert "head `unknown` → `unknown`" in str(d["comment_body"])


# --- valid_marker: fix stage ---


def test_valid_marker_accepts_fix_stage_marker() -> None:
    assert pipeline.valid_marker(fix_marker()) is True


def test_valid_marker_accepts_fix_stage_lease() -> None:
    assert pipeline.valid_marker(fix_lease()) is True


def test_valid_marker_rejects_fix_marker_with_review_pre_label() -> None:
    assert pipeline.valid_marker(fix_marker(pre_label="ai:implemented")) is False


def test_valid_marker_rejects_fix_marker_with_implement_pre_label() -> None:
    assert pipeline.valid_marker(fix_marker(pre_label="ai:available")) is False


def test_valid_marker_cross_checks_fix_stage() -> None:
    m = fix_marker(pr_number=5)
    assert pipeline.valid_marker(m, expected_pr=5, expected_stage="fix") is True
    assert pipeline.valid_marker(m, expected_stage="review") is False
    assert pipeline.valid_marker(m, expected_stage="implement") is False


# --- reconcile_dispatch: fix stage ---


def test_reconcile_leaves_markerless_fixing_pr_alone() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:fixing",
        marker_valid=False,
        classify_state="",
        pre_label="ai:changes-requested",
        in_flight_label="ai:fixing",
    )
    assert d["action"] == "noop"
    assert d["label"] == ""


def test_reconcile_noops_on_markerless_fixing_pr() -> None:
    # Same rule at the fix stage: a missing marker is not evidence of anything
    # (issue #592).
    d = pipeline.reconcile_dispatch(
        current_label="ai:fixing",
        marker_valid=False,
        classify_state="",
        pre_label="ai:changes-requested",
        in_flight_label="ai:fixing",
    )
    assert d["action"] == "noop"
    assert d["label"] == ""


def test_reconcile_harvests_completed_fix_in_custody() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:fixing",
        marker_valid=True,
        classify_state="complete",
        pre_label="ai:changes-requested",
        in_flight_label="ai:fixing",
    )
    assert d["action"] == "harvest"


def test_reconcile_reverts_stale_fix_in_custody() -> None:
    d = pipeline.reconcile_dispatch(
        current_label="ai:fixing",
        marker_valid=True,
        classify_state="stale",
        pre_label="ai:changes-requested",
        in_flight_label="ai:fixing",
    )
    assert d["action"] == "revert"
    assert d["label"] == "ai:changes-requested"


# --- CLI: harvest-fix ---


def test_cli_harvest_fix_emits_recheck(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "result.json"
    result.write_text(json.dumps(fix_pushed_result()))
    rc = pipeline.main(
        ["harvest-fix", "--result", str(result), "--pre-label", "ai:changes-requested"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "recheck"
    assert out["label"] == "ai:revised"
    assert out["sentinel"] == "FIXES_PUSHED_RECHECK_NEEDED"


def test_cli_harvest_fix_escalates_on_needs_human(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "result.json"
    result.write_text(json.dumps(fix_needs_human_result()))
    rc = pipeline.main(
        ["harvest-fix", "--result", str(result), "--pre-label", "ai:changes-requested"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "escalate"
    assert out["label"] == "ai:needs-human-decision"


def test_cli_harvest_fix_escalates_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = pipeline.main(
        [
            "harvest-fix",
            "--result",
            str(tmp_path / "nope.json"),
            "--pre-label",
            "ai:changes-requested",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "escalate"
    assert out["sentinel"] is None


def test_cli_harvest_fix_reproduces_the_tick_pr_body_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI must be able to reproduce any comment the tick renders.

    This subcommand exists so an operator can replay a harvest decision without
    running the tick. A revision state the CLI cannot express would make its
    ``comment_body`` differ from the one the PR actually carries, which is the
    one thing this surface has to be trusted for.
    """
    result = tmp_path / "result.json"
    result.write_text(json.dumps(fix_pushed_result()))

    rc = pipeline.main([
        "harvest-fix", "--result", str(result),
        "--pre-label", "ai:changes-requested",
        "--pinned-head", "a" * 40, "--current-head", "b" * 40,
        "--body-revision", "conflict",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "- PR body: left unchanged — it was edited while the fixer ran" in (
        out["comment_body"]
    )


def test_cli_harvest_fix_rejects_an_unknown_body_revision_state(
    tmp_path: Path,
) -> None:
    """An unrecognised state must fail loudly, not render a silent no-line.

    The states are a closed set the tick and the renderer share. A typo that
    parsed would print a comment claiming nothing happened to the body — the
    exact silence the reason lines exist to remove.
    """
    result = tmp_path / "result.json"
    result.write_text(json.dumps(fix_pushed_result()))

    with pytest.raises(SystemExit):
        pipeline.main([
            "harvest-fix", "--result", str(result),
            "--body-revision", "applied",
        ])


def test_cli_harvest_fix_rejects_the_derived_not_applied_state(
    tmp_path: Path,
) -> None:
    """``not-applied`` is an output of this module, never an input to it.

    It is derived from a ``ready`` revision on a non-``recheck`` outcome, so the
    tick never passes it. Accepting it would let a caller render "this run did
    not reach re-review" onto a ``recheck`` comment — a sentence the tick cannot
    produce, from the one surface whose job is reproducing what the tick renders.
    """
    result = tmp_path / "result.json"
    result.write_text(json.dumps(fix_pushed_result()))

    with pytest.raises(SystemExit):
        pipeline.main([
            "harvest-fix", "--result", str(result),
            "--body-revision", "not-applied",
        ])

    assert "not-applied" not in pipeline._BODY_REVISION_INPUTS
    assert "not-applied" in pipeline._BODY_REVISION_NOTES, (
        "the derived state still has to render a reason line"
    )


def test_every_body_revision_state_the_tick_returns_renders_a_reason() -> None:
    """The tick's states and the renderer's notes are one closed set.

    A state added to ``tick._fixer_body_revision`` without a note here renders
    no ``- PR body:`` line at all, which is the silence this whole path exists
    to remove: the reviewer cannot tell a body the pipeline declined to touch
    from one it never looked at. Pinned as a literal so adding a state on either
    side has to come here and state its reason.
    """
    assert set(pipeline._BODY_REVISION_INPUTS) == {
        "ready", "warning-only", "conflict", "invalid", "unstamped", "unavailable",
        "truncated", "stale-head", "closure-drift", "unreadable", "oversized",
    }
    assert all(
        pipeline._BODY_REVISION_NOTES[state].strip()
        for state in pipeline._BODY_REVISION_INPUTS
    )


# --- tick log ---


def test_prune_tick_logs_returns_zero_when_dir_missing(tmp_path: Path) -> None:
    pruned = pipeline.prune_tick_logs(str(tmp_path / "nonexistent"), keep_n=50)
    assert pruned == 0


def test_prune_tick_logs_keeps_most_recent_n(tmp_path: Path) -> None:
    log_dir = tmp_path / "tick-log"
    log_dir.mkdir()
    for i in range(10):
        (log_dir / f"tick-2024010{i:01d}T000000-{i}.jsonl").write_text("")
    pruned = pipeline.prune_tick_logs(str(log_dir), keep_n=5)
    assert pruned == 5
    remaining = sorted(log_dir.glob("tick-*.jsonl"))
    assert len(remaining) == 5
    # The 5 newest (highest index) must survive.
    assert remaining[0].name == "tick-20240105T000000-5.jsonl"


def test_prune_tick_logs_no_op_when_under_limit(tmp_path: Path) -> None:
    log_dir = tmp_path / "tick-log"
    log_dir.mkdir()
    for i in range(3):
        (log_dir / f"tick-202401{i:02d}T000000-{i}.jsonl").write_text("")
    pruned = pipeline.prune_tick_logs(str(log_dir), keep_n=5)
    assert pruned == 0
    assert len(list(log_dir.glob("tick-*.jsonl"))) == 3


def test_open_tick_log_creates_file_and_returns_path(tmp_path: Path) -> None:
    log_dir = tmp_path / "tick-log"
    path = pipeline.open_tick_log(str(log_dir))
    assert os.path.isfile(path)
    assert path.startswith(str(log_dir))
    assert os.path.basename(path).startswith("tick-")
    assert path.endswith(".jsonl")


def test_open_tick_log_creates_directory_if_missing(tmp_path: Path) -> None:
    log_dir = tmp_path / "new" / "tick-log"
    path = pipeline.open_tick_log(str(log_dir))
    assert os.path.isdir(str(log_dir))
    assert os.path.isfile(path)


def test_open_tick_log_creates_unique_files_within_same_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "tick-log"
    monkeypatch.setattr(pipeline.time, "time", lambda: 1_700_000_000.42)

    first = pipeline.open_tick_log(str(log_dir))
    second = pipeline.open_tick_log(str(log_dir))

    assert first != second
    assert os.path.isfile(first)
    assert os.path.isfile(second)


def test_open_tick_log_prunes_old_files(tmp_path: Path) -> None:
    log_dir = tmp_path / "tick-log"
    log_dir.mkdir()
    for i in range(52):
        (log_dir / f"tick-202401{i:02d}T000000-{i}.jsonl").write_text("")
    pipeline.open_tick_log(str(log_dir), keep_n=50)
    remaining = list(log_dir.glob("tick-*.jsonl"))
    # prune runs before the new file is created: 52 old → prune to 50 → add 1 new = 51 total
    assert len(remaining) == 51


def test_append_tick_entry_writes_json_line(tmp_path: Path) -> None:
    log_path = str(tmp_path / "tick-20240101T000000-0.jsonl")
    entry = {"ts": 1.0, "pr": 123, "phase": "harvest", "stage": "review", "action": "revert"}
    pipeline.append_tick_entry(log_path, entry)
    lines = Path(log_path).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == entry


def test_append_tick_entry_appends_multiple_entries(tmp_path: Path) -> None:
    log_path = str(tmp_path / "tick-20240101T000000-0.jsonl")
    e1 = {"ts": 1.0, "pr": 1, "phase": "harvest", "stage": "review", "action": "leave"}
    e2 = {"ts": 2.0, "pr": 2, "phase": "dispatch", "stage": "implement", "action": "skip_dedup"}
    pipeline.append_tick_entry(log_path, e1)
    pipeline.append_tick_entry(log_path, e2)
    lines = Path(log_path).read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == e1
    assert json.loads(lines[1]) == e2


def test_read_latest_tick_log_returns_entries_from_newest_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "tick-log"
    log_dir.mkdir()
    old_log = str(log_dir / "tick-20240101T000000-1.jsonl")
    new_log = str(log_dir / "tick-20240102T000000-2.jsonl")
    with open(old_log, "w") as f:
        f.write('{"ts": 1.0, "pr": 1, "action": "leave"}\n')
    e = {"ts": 2.0, "pr": 2, "action": "harvest"}
    with open(new_log, "w") as f:
        f.write(json.dumps(e) + "\n")
    entries = pipeline.read_latest_tick_log(str(log_dir))
    assert entries == [e]


def test_read_latest_tick_log_returns_empty_list_when_no_logs(tmp_path: Path) -> None:
    log_dir = tmp_path / "tick-log"
    log_dir.mkdir()
    assert pipeline.read_latest_tick_log(str(log_dir)) == []


def test_read_latest_tick_log_returns_empty_list_when_dir_missing(tmp_path: Path) -> None:
    assert pipeline.read_latest_tick_log(str(tmp_path / "nonexistent")) == []


def test_read_latest_tick_log_skips_non_dict_json_lines(tmp_path: Path) -> None:
    log_dir = tmp_path / "tick-log"
    log_dir.mkdir()
    log_path = str(log_dir / "tick-20240101T000000-1.jsonl")
    with open(log_path, "w") as f:
        f.write('{"ts": 1.0, "action": "leave"}\n')
        f.write("[1, 2, 3]\n")
        f.write('"just a string"\n')
        f.write("42\n")
        f.write('{"ts": 2.0, "action": "harvest"}\n')
    entries = pipeline.read_latest_tick_log(str(log_dir))
    assert len(entries) == 2
    assert entries[0] == {"ts": 1.0, "action": "leave"}
    assert entries[1] == {"ts": 2.0, "action": "harvest"}


def test_read_latest_tick_log_skips_malformed_lines(tmp_path: Path) -> None:
    log_dir = tmp_path / "tick-log"
    log_dir.mkdir()
    log_path = str(log_dir / "tick-20240101T000000-1.jsonl")
    with open(log_path, "w") as f:
        f.write('{"ts": 1.0, "action": "leave"}\n')
        f.write("not-json\n")
        f.write('{"ts": 2.0, "action": "harvest"}\n')
    entries = pipeline.read_latest_tick_log(str(log_dir))
    assert len(entries) == 2
    assert entries[0] == {"ts": 1.0, "action": "leave"}
    assert entries[1] == {"ts": 2.0, "action": "harvest"}


def test_cli_tick_log_open_prints_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_dir = tmp_path / "tick-log"
    rc = pipeline.main(["tick-log-open", "--log-dir", str(log_dir)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(str(log_dir))
    assert out.endswith(".jsonl")
    assert os.path.isfile(out)


def test_cli_tick_log_write_appends_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = str(tmp_path / "tick-20240101T000000-0.jsonl")
    entry = {"ts": 1.0, "pr": 42, "phase": "harvest", "stage": "review", "action": "leave"}
    rc = pipeline.main(["tick-log-write", "--log-path", log_path, "--entry", json.dumps(entry)])
    assert rc == 0
    lines = Path(log_path).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == entry


def test_cli_tick_log_write_rejects_invalid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = str(tmp_path / "tick-20240101T000000-0.jsonl")
    rc = pipeline.main(["tick-log-write", "--log-path", log_path, "--entry", "not-json{"])
    assert rc == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_cli_tick_log_write_rejects_non_dict_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = str(tmp_path / "tick-20240101T000000-0.jsonl")
    rc = pipeline.main(["tick-log-write", "--log-path", log_path, "--entry", "[1,2,3]"])
    assert rc == 1
    assert "must be a JSON object" in capsys.readouterr().err


def test_cli_tick_log_tail_returns_entries_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_dir = tmp_path / "tick-log"
    log_dir.mkdir()
    log_path = str(log_dir / "tick-20240101T000000-1.jsonl")
    e1 = {"ts": 1.0, "pr": 1, "action": "leave"}
    e2 = {"ts": 2.0, "pr": 2, "action": "harvest"}
    with open(log_path, "w") as f:
        f.write(json.dumps(e1) + "\n")
        f.write(json.dumps(e2) + "\n")
    rc = pipeline.main(["tick-log-tail", "--log-dir", str(log_dir)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == [e1, e2]


# --- Helpers ---


def operator_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str,
) -> Path:
    """Write ``text`` as the operator's config and point the resolver at it.

    Uses ``$PR_PIPELINE_CONFIG`` — the documented testing override — so no test
    can reach the real ``~/.claude/pr-pipeline/config.toml``.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        tmp_path: The test's temporary directory.
        text: The TOML to write.

    Returns:
        The path written, so a test can corrupt it afterwards.
    """
    cfg = tmp_path / "operator-config.toml"
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    return cfg


def packaged_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str,
) -> Path:
    """Write ``text`` as the packaged floor and point the resolver at it.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        tmp_path: The test's temporary directory.
        text: The TOML to write.

    Returns:
        The path written.
    """
    cfg = tmp_path / "packaged-config.toml"
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setattr(pipeline, "_PACKAGED_CONFIG_PATH", cfg)
    return cfg


def classify(*, now: float, deadline: float, alive: bool, result: bool) -> str:
    """Thin wrapper over classify_dispatch to keep assertion lines readable."""
    return pipeline.classify_dispatch(
        now=now, deadline=deadline, pid_alive=alive, result_exists=result
    )


def marker(**overrides: object) -> dict[str, object]:
    """A complete, valid *published* review dispatch marker.

    Override one field to exercise a single rejection reason; pass a field already
    present to corrupt it, or use ``del`` on the returned dict to test an omission.
    """
    base: dict[str, object] = {
        "engine": "codex",
        "stage": "review",
        "pr_number": 1,
        "deadline": 50,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-test-001",
        "pid": 5,
        "start_time": 1000.0,
        "pgid": 5,
    }
    base.update(overrides)
    return base


def lease(**overrides: object) -> dict[str, object]:
    """A complete, valid pre-spawn *launching* lease marker (state, no pid)."""
    base = marker()
    del base["pid"]
    del base["start_time"]
    del base["pgid"]
    base["state"] = "launching"
    base.update(overrides)
    return base


def clean_review() -> dict[str, object]:
    """A review.json body with a clean verdict and no findings."""
    return {"verdict": "ready_for_human", "findings": []}


def changes_review() -> dict[str, object]:
    """A review.json body requesting changes with one blocker finding."""
    return {
        "verdict": "changes_requested",
        "findings": [{"severity": "blocker", "file": "a.py", "summary": "boom"}],
    }


def impl_marker(**overrides: object) -> dict[str, object]:
    """A complete, valid *published* implement-stage dispatch marker."""
    base: dict[str, object] = {
        "engine": "claude",
        "stage": "implement",
        "pr_number": 1,
        "deadline": 50,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:available",
        "dispatch_id": "d-impl-001",
        "pid": 5,
        "start_time": 1000.0,
        "pgid": 5,
    }
    base.update(overrides)
    return base


def impl_lease(**overrides: object) -> dict[str, object]:
    """A complete, valid implement-stage launching lease marker."""
    base = impl_marker()
    del base["pid"]
    del base["start_time"]
    del base["pgid"]
    base["state"] = "launching"
    base.update(overrides)
    return base


def impl_complete_result() -> dict[str, object]:
    """A claude -p result.json with the IMPLEMENTATION_COMPLETE sentinel."""
    return {"result": "All tests pass. IMPLEMENTATION_COMPLETE", "is_error": False}


def impl_needs_human_result() -> dict[str, object]:
    """A claude -p result.json with the NEEDS_HUMAN_DECISION sentinel."""
    return {"result": "Blocked on design question. NEEDS_HUMAN_DECISION", "is_error": False}


def owned_marker(**overrides: object) -> dict[str, object]:
    """A valid published marker with full process identity (dispatch_id + start_time + pgid)."""
    base: dict[str, object] = {
        "engine": "codex",
        "stage": "review",
        "pr_number": 1,
        "deadline": 50,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:implemented",
        "dispatch_id": "d-test-001",
        "pid": 5,
        "start_time": 1000.0,
        "pgid": 5,
    }
    base.update(overrides)
    return base


def owned_lease(**overrides: object) -> dict[str, object]:
    """A valid launching lease with dispatch_id but no process identity."""
    base = owned_marker()
    del base["pid"]
    del base["start_time"]
    del base["pgid"]
    base["state"] = "launching"
    base.update(overrides)
    return base


def envelope(**overrides: object) -> dict[str, object]:
    """A valid completion envelope from the engine wrapper."""
    base: dict[str, object] = {
        "dispatch_id": "d-test-001",
        "exit_status": 0,
        "wrapper_pid": 100,
        "start_time": 1000.0,
        "result_valid": True,
    }
    base.update(overrides)
    return base


def fix_marker(**overrides: object) -> dict[str, object]:
    """A complete, valid *published* fix-stage dispatch marker."""
    base: dict[str, object] = {
        "engine": "claude",
        "stage": "fix",
        "pr_number": 1,
        "deadline": 50,
        "base_ref": "main",
        "base_sha": "b1",
        "head_sha": "h1",
        "pre_label": "ai:changes-requested",
        "dispatch_id": "d-fix-001",
        "pid": 5,
        "start_time": 1000.0,
        "pgid": 5,
    }
    base.update(overrides)
    return base


def body_provenance(**overrides: object) -> dict[str, object]:
    """A complete, valid ``pr_body`` provenance block for a fix marker."""
    base: dict[str, object] = {
        "digest": "a" * 64,
        "summary_closes": 7,
        "closes": ["#7"],
        "truncated": False,
    }
    base.update(overrides)
    return base


def fix_lease(**overrides: object) -> dict[str, object]:
    """A complete, valid fix-stage launching lease marker."""
    base = fix_marker()
    del base["pid"]
    del base["start_time"]
    del base["pgid"]
    base["state"] = "launching"
    base.update(overrides)
    return base


def fix_pushed_result() -> dict[str, object]:
    """A claude -p result.json with the FIXES_PUSHED_RECHECK_NEEDED sentinel."""
    return {
        "result": "All fixes applied. FIXES_PUSHED_RECHECK_NEEDED",
        "is_error": False,
    }


def fix_needs_human_result() -> dict[str, object]:
    """A claude -p result.json with the NEEDS_HUMAN_DECISION sentinel for fix."""
    return {"result": "Finding out of scope. NEEDS_HUMAN_DECISION", "is_error": False}


# --- TypedDicts and dataclasses ---


def test_harvest_result_typeddict_is_importable() -> None:
    """HarvestResult TypedDict exists and has required fields."""
    from pipeline import HarvestResult  # type: ignore[attr-defined]

    result: HarvestResult = {
        "action": "promote",
        "label": "ai:ready-for-human",
    }
    assert result["action"] == "promote"
    assert result["label"] == "ai:ready-for-human"


def test_classify_result_typeddict_is_importable() -> None:
    """ClassifyResult TypedDict exists and has required fields."""
    from pipeline import ClassifyResult  # type: ignore[attr-defined]

    result: ClassifyResult = {
        "state": "live",
        "identity": "matched",
    }
    assert result["state"] == "live"
    assert result["identity"] == "matched"


def test_reconcile_result_typeddict_is_importable() -> None:
    """ReconcileResult TypedDict exists and has required fields."""
    from pipeline import ReconcileResult  # type: ignore[attr-defined]

    result: ReconcileResult = {
        "action": "noop",
        "label": "",
        "kill": False,
    }
    assert result["action"] == "noop"
    assert not result["kill"]


def test_admission_result_dataclass_is_importable() -> None:
    """AdmissionResult frozen dataclass exists and has required fields."""
    from pipeline import AdmissionResult  # type: ignore[attr-defined]

    result = AdmissionResult(
        can_dispatch=True,
        reason="",
        in_flight_count=1,
        max_in_flight=3,
    )
    assert result.can_dispatch is True
    assert result.in_flight_count == 1


def test_in_flight_ref_dataclass_is_importable() -> None:
    """InFlightRef frozen dataclass exists and has required fields."""
    from pipeline import InFlightRef  # type: ignore[attr-defined]

    ref = InFlightRef(kind="pr", number=42, label="ai:reviewing")
    assert ref.kind == "pr"
    assert ref.number == 42


def test_stage_config_dataclass_is_importable() -> None:
    """StageConfig frozen dataclass exists and has required fields."""
    from pipeline import StageConfig  # type: ignore[attr-defined]

    config = StageConfig(
        stage="review",
        worktree_prefix="pr",
        attempts_key_prefix="",
        result_file="review.json",
        in_flight_label="ai:reviewing",
        pre_label="ai:implemented",
        strict_dual_label=True,
        tick_abort_on_enum_failure=True,
    )
    assert config.stage == "review"
    assert config.strict_dual_label is True


def test_candidate_metadata_typeddict_is_importable() -> None:
    """CandidateMetadata TypedDict exists and has required fields."""
    from pipeline import CandidateMetadata  # type: ignore[attr-defined]

    meta: CandidateMetadata = {
        "author": {"login": "test"},
        "headRepository": {"nameWithOwner": "test/repo"},
        "isCrossRepository": False,
    }
    assert meta["author"]["login"] == "test"


# --- Public function exports (Step 2 — importability) ---


def test_harvest_review_public_function_is_importable() -> None:
    """harvest_review() is importable and callable with keyword-only args."""
    from pipeline import harvest_review  # type: ignore[attr-defined]

    result = harvest_review(
        trusted_author="test-owner",
        review='{"verdict":"ready_for_human","findings":[]}',
        comments="[]",
        pinned_base="abc123",
        current_base="abc123",
    )
    assert result["action"] in (
        "promote", "request_changes", "max_iterations", "discard", "escalate",
    )


def test_validate_review_candidate_public_function_is_importable() -> None:
    """validate_review_candidate() is importable and accepts raw PR metadata."""
    from pipeline import validate_review_candidate  # type: ignore[attr-defined]

    result = validate_review_candidate(
        candidate={
            "author": {"login": "test-owner"},
            "headRepository": {"nameWithOwner": "test-owner/test-repo"},
            "isCrossRepository": False,
        },
        trusted_author="test-owner",
        trusted_repo="test-owner/test-repo",
    )
    assert result is True


def test_validate_marker_public_function_is_importable() -> None:
    """validate_marker() is importable and delegates to valid_marker()."""
    from pipeline import validate_marker  # type: ignore[attr-defined]

    result = validate_marker(dispatch={})
    assert result is False


def test_harvest_implementation_public_function_is_importable() -> None:
    """harvest_implementation() is importable with keyword-only args."""
    from pipeline import harvest_implementation  # type: ignore[attr-defined]

    result = harvest_implementation(
        result='{"result":"IMPLEMENTATION_COMPLETE"}',
    )
    assert result["action"] in ("create_pr", "escalate")


def test_harvest_fix_public_function_is_importable() -> None:
    """harvest_fix() is importable with keyword-only args."""
    from pipeline import harvest_fix  # type: ignore[attr-defined]

    result = harvest_fix(
        result='{"result":"FIXES_PUSHED_RECHECK_NEEDED"}',
    )
    assert result["action"] in ("recheck", "escalate")


def test_harvest_fix_threads_head_shas_to_decision() -> None:
    """harvest_fix() passes the pinned/current heads through to the push check."""
    result = pipeline.harvest_fix(
        result='{"result":"FIXES_PUSHED_RECHECK_NEEDED"}',
        pre_label="ai:changes-requested",
        pinned_head="a" * 40,
        current_head="a" * 40,
    )
    assert result["action"] == "escalate"
    assert result["head_advanced"] is False


def test_validate_envelope_public_function_is_importable() -> None:
    """validate_envelope() is importable and delegates to validate_completion_envelope()."""
    from pipeline import validate_envelope  # type: ignore[attr-defined]

    result = validate_envelope(envelope={})
    assert result is False


def test_verify_identity_public_function_is_importable() -> None:
    """verify_identity() is importable and delegates to verify_process_identity()."""
    from pipeline import verify_identity  # type: ignore[attr-defined]

    result = verify_identity(recorded_start=100.0, live_start=100.5)
    assert result in ("matched", "mismatched", "unknown")


def test_tick_log_open_public_function_is_importable(tmp_path: Path) -> None:
    """tick_log_open() is importable and returns a path string."""
    from pipeline import tick_log_open  # type: ignore[attr-defined]

    result = tick_log_open(log_dir=str(tmp_path))
    assert isinstance(result, str)


def test_tick_log_write_public_function_is_importable(tmp_path: Path) -> None:
    """tick_log_write() is importable and accepts entry dict."""
    from pipeline import tick_log_open, tick_log_write  # type: ignore[attr-defined]

    log_path = tick_log_open(log_dir=str(tmp_path))
    tick_log_write(
        log_path=log_path,
        entry={
            "ts": 100, "pr": None, "phase": "harvest", "stage": "review",
            "action": "noop", "reason": "test", "engine": "",
        },
    )


def test_tick_log_tail_public_function_is_importable() -> None:
    """tick_log_tail() is importable and returns a list."""
    from pipeline import tick_log_tail  # type: ignore[attr-defined]

    result = tick_log_tail(log_dir="/nonexistent-dir")
    assert isinstance(result, list)


# --- process_control.py public API verification (Step 3) ---


def test_process_control_public_api_is_importable() -> None:
    """The three public functions tick.py needs are importable."""
    from process_control import (  # type: ignore[import-untyped]
        get_process_start_time,
        terminate_process,
        terminate_process_group,
    )

    assert callable(get_process_start_time)
    assert callable(terminate_process)
    assert callable(terminate_process_group)


# --- Parity tests (Step 4) — direct-call output ≡ CLI round-trip ---


def test_harvest_review_direct_equals_cli(tmp_path: Path) -> None:
    """harvest_review() direct call matches CLI output for the same inputs."""
    review_path = tmp_path / "review.json"
    comments_path = tmp_path / "comments.json"
    review_path.write_text('{"verdict":"ready_for_human","findings":[]}')
    comments_path.write_text("[]")

    direct = pipeline.harvest_review(
        review='{"verdict":"ready_for_human","findings":[]}',
        comments="[]",
        pinned_base="abc",
        current_base="abc",
        trusted_author="test-owner",
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "harvest-review", "--trusted-author", "test-owner",
                f"--review={review_path}",
                f"--comments={comments_path}",
                "--pinned-base=abc",
                "--current-base=abc",
            ]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == direct


def test_validate_review_candidate_direct_equals_cli() -> None:
    """validate_review_candidate() direct call matches CLI output."""
    candidate = {
        "author": {"login": "test-owner"},
        "headRepository": {"nameWithOwner": "test-owner/test-repo"},
        "isCrossRepository": False,
    }
    direct = pipeline.validate_review_candidate(
        candidate=candidate,
        trusted_author="test-owner",
        trusted_repo="test-owner/test-repo",
    )

    import sys as _sys
    from io import StringIO

    old_in = _sys.stdin
    old_out = _sys.stdout
    _sys.stdin = StringIO(json.dumps(candidate))
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "validate-review-candidate",
                "--trusted-author=test-owner",
                "--trusted-repo=test-owner/test-repo",
            ]
        )
    finally:
        _sys.stdin = old_in
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == {"trusted": direct}


def test_verify_identity_direct_equals_cli() -> None:
    """verify_identity() direct call matches CLI output."""
    direct = pipeline.verify_identity(
        recorded_start=100.0, live_start=100.5, tolerance=1.0,
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "verify-identity",
                "--recorded-start=100.0",
                "--live-start=100.5",
                "--tolerance=1.0",
            ]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == {"identity": direct}


def test_validate_envelope_direct_equals_cli(tmp_path: Path) -> None:
    """validate_envelope() direct call matches CLI output."""
    envelope_path = tmp_path / "completion.json"
    envelope_path.write_text(
        '{"dispatch_id":"d1","exit_status":0,"wrapper_pid":1,"start_time":1.0,"result_valid":true}'
    )

    direct = pipeline.validate_envelope(
        envelope={
            "dispatch_id": "d1",
            "exit_status": 0,
            "wrapper_pid": 1,
            "start_time": 1.0,
            "result_valid": True,
        },
        dispatch_id="d1",
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "validate-envelope",
                f"--envelope={envelope_path}",
                "--dispatch-id=d1",
            ]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == {"valid": direct}


def test_tick_log_round_trip(tmp_path: Path) -> None:
    """tick_log_open + tick_log_write + tick_log_tail round-trip works."""
    log_path = pipeline.tick_log_open(log_dir=str(tmp_path))
    assert os.path.exists(log_path)

    entry = {
        "ts": 1234567890,
        "pr": 42,
        "phase": "harvest",
        "stage": "review",
        "action": "promote",
        "reason": "no blockers",
        "engine": "",
    }
    pipeline.tick_log_write(log_path=log_path, entry=entry)

    entries = pipeline.tick_log_tail(log_dir=str(tmp_path))
    assert len(entries) >= 1
    last = entries[-1]
    assert last["ts"] == 1234567890
    assert last["pr"] == 42
    assert last["action"] == "promote"


def test_validate_review_direct_equals_cli(tmp_path: Path) -> None:
    """validate_review() direct call matches CLI output."""
    review_path = tmp_path / "review.json"
    review_path.write_text(
        '{"verdict":"ready_for_human","findings":[]}'
    )

    direct = pipeline.validate_review(
        {"verdict": "ready_for_human", "findings": []}
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            ["validate-review", f"--review={review_path}"]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    assert io.getvalue().strip() == direct


def test_classify_dispatch_direct_equals_cli(tmp_path: Path) -> None:
    """classify_dispatch() direct call matches CLI output."""
    dispatch_path = tmp_path / "dispatch.json"
    dispatch_path.write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 1,
        "deadline": 9999999999.0, "base_ref": "main",
        "base_sha": "b1", "head_sha": "h1",
        "pre_label": "ai:implemented", "dispatch_id": "d-1",
        "pid": 99999, "start_time": 999999.0, "pgid": 99999,
    }))

    direct_state = pipeline.classify_dispatch(
        now=1000.0, deadline=9999999999.0,
        pid_alive=True, result_exists=False,
        state="", envelope_valid=False,
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "classify-dispatch",
                f"--dispatch={dispatch_path}",
                "--now=1000.0", "--pid-alive=1",
                "--result-exists=0", "--envelope-valid=0",
            ]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == {"state": direct_state}


def test_touches_review_policy_direct_equals_cli() -> None:
    """touches_review_policy() direct call matches CLI output."""
    direct = pipeline.touches_review_policy(
        changed_paths=["CLAUDE.md", "src/main.py"]
    )

    import sys as _sys
    from io import StringIO

    old_in = _sys.stdin
    old_out = _sys.stdout
    _sys.stdin = StringIO("CLAUDE.md\nsrc/main.py\n")
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(["touches-review-policy"])
    finally:
        _sys.stdin = old_in
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == {"touches_policy": direct}


def test_validate_marker_direct_equals_cli(tmp_path: Path) -> None:
    """validate_marker() direct call matches CLI output."""
    dispatch_path = tmp_path / "dispatch.json"
    dispatch_path.write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 1,
        "deadline": 9999999999.0, "base_ref": "main",
        "base_sha": "b1", "head_sha": "h1",
        "pre_label": "ai:implemented", "dispatch_id": "d-1",
        "pid": 99999, "start_time": 999999.0, "pgid": 99999,
    }))

    from pathlib import Path as _Path
    with open(_Path(dispatch_path)) as fh:
        dispatch_obj = json.load(fh)
    direct = pipeline.validate_marker(
        dispatch=dispatch_obj, pr=1, stage="review",
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "validate-marker",
                f"--dispatch={dispatch_path}",
                "--pr=1", "--stage=review",
            ]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == {"valid": direct}


def test_harvest_implementation_direct_equals_cli(tmp_path: Path) -> None:
    """harvest_implementation() direct call matches CLI output."""
    result_path = tmp_path / "result.json"
    result_path.write_text(
        '{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}'
    )

    direct = pipeline.harvest_implementation(
        result='{"result": "IMPLEMENTATION_COMPLETE", "exit_status": 0}',
        pre_label="ai:available",
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "harvest-implementation",
                f"--result={result_path}",
                "--pre-label=ai:available",
            ]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == direct


def test_harvest_fix_direct_equals_cli(tmp_path: Path) -> None:
    """harvest_fix() direct call matches CLI output."""
    result_path = tmp_path / "result.json"
    result_path.write_text(
        '{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}'
    )

    direct = pipeline.harvest_fix(
        result='{"result": "FIXES_PUSHED_RECHECK_NEEDED", "exit_status": 0}',
        pre_label="ai:changes-requested",
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "harvest-fix",
                f"--result={result_path}",
                "--pre-label=ai:changes-requested",
            ]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == direct


def test_reconcile_dispatch_direct_equals_cli(tmp_path: Path) -> None:
    """reconcile_dispatch() direct call matches CLI output."""
    dispatch_path = tmp_path / "dispatch.json"
    dispatch_path.write_text(json.dumps({
        "engine": "codex", "stage": "review", "pr_number": 1,
        "deadline": 9999999999.0, "base_ref": "main",
        "base_sha": "b1", "head_sha": "h1",
        "pre_label": "ai:implemented", "dispatch_id": "d-1",
        "pid": 99999, "start_time": 999999.0, "pgid": 99999,
    }))

    direct = pipeline.reconcile_dispatch(
        current_label="ai:reviewing", marker_valid=True,
        classify_state="live", pre_label="ai:implemented",
        in_flight_label="ai:reviewing",
    )

    import sys as _sys
    from io import StringIO

    old_out = _sys.stdout
    _sys.stdout = io = StringIO()
    try:
        ret = pipeline.main(
            [
                "reconcile-dispatch",
                f"--dispatch={dispatch_path}",
                "--current-label=ai:reviewing",
                "--classify-state=live",
                "--in-flight-label=ai:reviewing",
            ]
        )
    finally:
        _sys.stdout = old_out

    assert ret == 0
    cli_result = json.loads(io.getvalue())
    assert cli_result == direct


# --- derive_tick_state ---


def test_derive_tick_state_ok_with_in_flight_and_queue() -> None:
    """Healthy tick with 2 in-flight engines and an actionable queue."""
    state = pipeline.derive_tick_state(
        status="ok",
        in_flight_count=2,
        has_actionable_queue=True,
    )
    assert state.status == "ok"
    assert state.in_flight == "2"
    assert state.queue == "true"
    assert state.quiescent == "false"


def test_derive_tick_state_quiescent() -> None:
    """All items terminal, no in-flight, no queue → quiescent."""
    state = pipeline.derive_tick_state(
        status="ok",
        in_flight_count=0,
        has_actionable_queue=False,
    )
    assert state.status == "ok"
    assert state.in_flight == "0"
    assert state.queue == "false"
    assert state.quiescent == "true"


def test_derive_tick_state_not_quiescent_when_in_flight() -> None:
    """In-flight engines prevent quiescence even with empty queue."""
    state = pipeline.derive_tick_state(
        status="ok",
        in_flight_count=1,
        has_actionable_queue=False,
    )
    assert state.quiescent == "false"


def test_derive_tick_state_not_quiescent_when_queue_nonempty() -> None:
    """Actionable queue prevents quiescence even with zero in-flight."""
    state = pipeline.derive_tick_state(
        status="ok",
        in_flight_count=0,
        has_actionable_queue=True,
    )
    assert state.quiescent == "false"


def test_derive_tick_state_failed_uses_unknown() -> None:
    """FAILED status with no computed state → all fields unknown."""
    state = pipeline.derive_tick_state(status="FAILED")
    assert state.status == "FAILED"
    assert state.in_flight == "unknown"
    assert state.queue == "unknown"
    assert state.quiescent == "unknown"


def test_derive_tick_state_partial_with_partial_info() -> None:
    """PARTIAL status where in-flight is known but queue is not."""
    state = pipeline.derive_tick_state(
        status="PARTIAL",
        in_flight_count=1,
    )
    assert state.status == "PARTIAL"
    assert state.in_flight == "1"
    assert state.queue == "unknown"
    assert state.quiescent == "unknown"


def test_tick_state_format_line() -> None:
    """format_line produces the parseable tick-state: line."""
    state = pipeline.TickState(
        status="ok", in_flight="2", queue="true", quiescent="false",
        held="1",
    )
    assert state.format_line() == (
        "tick-state: status=ok in_flight=2 queue=true quiescent=false held=1"
    )


# --- compute_next_wake (cadence) ---


def test_next_wake_in_flight_uses_min_of_deadline_and_270() -> None:
    """≥1 engine in-flight with a known deadline → min(deadline, 270s)."""
    d = pipeline.compute_next_wake(
        in_flight_count=1,
        nearest_deadline_s=120.0,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 120
    assert d.stop is False


def test_next_wake_in_flight_clamps_deadline_to_270() -> None:
    """Deadline beyond 270s is clamped to stay cache-warm."""
    d = pipeline.compute_next_wake(
        in_flight_count=2,
        nearest_deadline_s=500.0,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 270
    assert d.stop is False


def test_next_wake_in_flight_no_deadline_defaults_to_270() -> None:
    """In-flight without a known deadline → 270s (cache-warm default)."""
    d = pipeline.compute_next_wake(
        in_flight_count=1,
        nearest_deadline_s=None,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 270
    assert d.stop is False


def test_next_wake_actionable_queue_returns_270() -> None:
    """No in-flight but actionable queue → ~270s to poll promptly."""
    d = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=True,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 270
    assert d.stop is False


def test_next_wake_at_capacity_returns_270() -> None:
    """At max_in_flight → ~270s to harvest and free a slot."""
    d = pipeline.compute_next_wake(
        in_flight_count=3,
        nearest_deadline_s=500.0,
        has_actionable_queue=False,
        at_capacity=True,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 270
    assert d.stop is False


def test_next_wake_quiescent_heartbeat_returns_1800() -> None:
    """Quiescent → ~1800s heartbeat, amortizing the cache miss."""
    d = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=True,
        consecutive_quiescent_ticks=0,
    )
    assert d.next_wake_seconds == 1800
    assert d.stop is False


def test_next_wake_quiescent_stops_after_n_heartbeats() -> None:
    """N consecutive quiescent heartbeats → stop + notify developer."""
    d = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=True,
        consecutive_quiescent_ticks=2,
        quiescent_stop_after=3,
    )
    assert d.next_wake_seconds == 1800
    assert d.stop is True


def test_next_wake_quiescent_stop_default_threshold() -> None:
    """With default stop_after=3, the 3rd heartbeat (index 2) stops."""
    d = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=True,
        consecutive_quiescent_ticks=1,
    )
    assert d.stop is False

    d2 = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=True,
        consecutive_quiescent_ticks=2,
    )
    assert d2.stop is True


def test_next_wake_never_returns_300() -> None:
    """The 300s TTL cliff must never be the output — nudge to 270."""
    d = pipeline.compute_next_wake(
        in_flight_count=1,
        nearest_deadline_s=300.0,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds != 300


def test_next_wake_non_quiescent_resets_counter() -> None:
    """A non-quiescent tick resets quiescence — stop is always False."""
    d = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=True,
        at_capacity=False,
        is_quiescent=False,
        consecutive_quiescent_ticks=5,
    )
    assert d.stop is False
    assert d.next_wake_seconds == 270


def test_next_wake_in_flight_beats_actionable_queue() -> None:
    """In-flight engines take priority over actionable queue."""
    d = pipeline.compute_next_wake(
        in_flight_count=2,
        nearest_deadline_s=60.0,
        has_actionable_queue=True,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 60  # deadline-driven, not 270


def test_next_wake_in_flight_beats_at_capacity_flag() -> None:
    """In-flight with deadline beats the at_capacity 270s default."""
    d = pipeline.compute_next_wake(
        in_flight_count=3,
        nearest_deadline_s=90.0,
        has_actionable_queue=True,
        at_capacity=True,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 90  # deadline, not 270


def test_next_wake_quiescent_beats_everything() -> None:
    """Quiescence is checked after in-flight/queue/capacity — it's the
    fallback when nothing is happening."""
    d = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=True,
    )
    assert d.next_wake_seconds == 1800
    assert d.stop is False


def test_next_wake_reason_is_descriptive() -> None:
    """Every decision carries a human-readable reason string."""
    d = pipeline.compute_next_wake(
        in_flight_count=1,
        nearest_deadline_s=120.0,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=False,
    )
    assert isinstance(d.reason, str)
    assert len(d.reason) > 0


def test_cadence_decision_dataclass_is_importable() -> None:
    """CadenceDecision frozen dataclass exists with required fields."""
    from pipeline import CadenceDecision  # type: ignore[attr-defined]

    d = CadenceDecision(next_wake_seconds=270, stop=False, reason="test")
    assert d.next_wake_seconds == 270
    assert d.stop is False
    assert d.reason == "test"


def test_next_wake_actionable_queue_with_in_flight_zero() -> None:
    """Edge: exactly zero in-flight with an actionable queue is not quiescent."""
    d = pipeline.compute_next_wake(
        in_flight_count=0,
        nearest_deadline_s=None,
        has_actionable_queue=True,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 270
    assert d.stop is False


def test_next_wake_overdue_deadline_triggers_immediate_retry() -> None:
    """An overdue engine (deadline_s <= 0) must not sleep 270s; wake in 1s."""
    d = pipeline.compute_next_wake(
        in_flight_count=1,
        nearest_deadline_s=-30.0,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 1
    assert d.stop is False


def test_next_wake_zero_deadline_triggers_immediate_retry() -> None:
    """A deadline of exactly 0s (just expired) must also wake immediately."""
    d = pipeline.compute_next_wake(
        in_flight_count=1,
        nearest_deadline_s=0.0,
        has_actionable_queue=False,
        at_capacity=False,
        is_quiescent=False,
    )
    assert d.next_wake_seconds == 1
    assert d.stop is False


def test_cadence_decision_format_line() -> None:
    """format_line produces the parseable tick-cadence: line."""
    d = pipeline.CadenceDecision(
        next_wake_seconds=270, stop=False, reason="in-flight → wake 270s",
    )
    assert d.format_line() == (
        'tick-cadence: next_wake=270 stop=false reason="in-flight → wake 270s"'
    )


def test_cadence_decision_format_line_stop_true() -> None:
    """Stop line is parseable and includes the reason."""
    d = pipeline.CadenceDecision(
        next_wake_seconds=1800, stop=True,
        reason="quiescent (heartbeat 3/3) → stop",
    )
    line = d.format_line()
    assert "stop=true" in line
    assert "next_wake=1800" in line


# --- resolve_pickup_eligibility ---


@pytest.mark.parametrize(
    ("stage", "entity", "labels", "expected"),
    [
        ("implement", "issue", [pipeline.LABEL_AVAILABLE], pipeline.LABEL_AVAILABLE),
        ("review", "pr", [pipeline.LABEL_IMPLEMENTED], pipeline.LABEL_IMPLEMENTED),
        ("review", "pr", [pipeline.LABEL_REVISED], pipeline.LABEL_REVISED),
        (
            "fix", "pr", [pipeline.LABEL_CHANGES_REQUESTED],
            pipeline.LABEL_CHANGES_REQUESTED,
        ),
    ],
)
def test_pickup_eligibility_admits_a_single_stage_label(
    stage: str, entity: str, labels: list[str], expected: str,
) -> None:
    """The ordinary case: one pickup label, no hold, no in-flight label."""
    verdict = pipeline.resolve_pickup_eligibility(labels, entity=entity, stage=stage)

    assert verdict.label == expected
    assert verdict.reason == ""


def test_pickup_eligibility_rejects_cross_stage_pickup_labels() -> None:
    """Two pickup labels from *different* stages are ambiguous everywhere.

    A PR carrying ``ai:implemented`` next to ``ai:changes-requested`` used to
    satisfy the review stage and the fix stage independently, because each
    counted only its own labels: two engines could take the same PR, and the
    second would meet the first's live marker under a lagging label read.
    Exactly-one is therefore counted across every stage for the entity.
    """
    labels = [pipeline.LABEL_IMPLEMENTED, pipeline.LABEL_CHANGES_REQUESTED]

    for stage in ("review", "fix"):
        verdict = pipeline.resolve_pickup_eligibility(labels, entity="pr", stage=stage)
        assert verdict.label == ""
        assert "single pickup label" in verdict.reason
        assert verdict.blocking == (
            pipeline.LABEL_CHANGES_REQUESTED, pipeline.LABEL_IMPLEMENTED,
        )


def test_pickup_eligibility_rejects_ambiguous_same_stage_labels() -> None:
    """``ai:implemented`` + ``ai:revised`` guesses at the stale-revert target."""
    verdict = pipeline.resolve_pickup_eligibility(
        [pipeline.LABEL_IMPLEMENTED, pipeline.LABEL_REVISED],
        entity="pr", stage="review",
    )

    assert verdict.label == ""
    assert "single pickup label" in verdict.reason


def test_pickup_eligibility_rejects_an_in_flight_label() -> None:
    """An in-flight label is some machine's claim; the local marker scan
    cannot disprove it, so the label set is the only usable proof."""
    verdict = pipeline.resolve_pickup_eligibility(
        [pipeline.LABEL_REVISED, pipeline.LABEL_REVIEWING],
        entity="pr", stage="review",
    )

    assert verdict.label == ""
    assert "in-flight label" in verdict.reason
    assert verdict.blocking == (pipeline.LABEL_REVIEWING,)


@pytest.mark.parametrize(
    "hold", [pipeline.LABEL_BLOCKED, pipeline.LABEL_NEEDS_HUMAN],
)
def test_pickup_eligibility_rejects_a_human_hold(hold: str) -> None:
    """A hold coexisting with a pickup label is human-set and outranks it."""
    verdict = pipeline.resolve_pickup_eligibility(
        [pipeline.LABEL_AVAILABLE, hold], entity="issue", stage="implement",
    )

    assert verdict.label == ""
    assert "hold label" in verdict.reason
    assert verdict.blocking == (hold,)


def test_pickup_eligibility_rejects_a_label_owned_by_another_stage() -> None:
    """The label may move between the queue listing and the point-read."""
    verdict = pipeline.resolve_pickup_eligibility(
        [pipeline.LABEL_CHANGES_REQUESTED], entity="pr", stage="review",
    )

    assert verdict.label == ""
    assert "another stage" in verdict.reason
    assert verdict.blocking == (pipeline.LABEL_CHANGES_REQUESTED,)


def test_pickup_eligibility_without_a_stage_answers_for_any_stage() -> None:
    """Cadence asks "can *some* stage dispatch this?", not "can review?".

    Both questions must apply the identical rule, or a permanently
    undispatchable item counts as an actionable queue and holds the loop
    short of its quiescent stop forever.
    """
    dispatchable = pipeline.resolve_pickup_eligibility(
        [pipeline.LABEL_CHANGES_REQUESTED], entity="pr",
    )
    ambiguous = pipeline.resolve_pickup_eligibility(
        [pipeline.LABEL_IMPLEMENTED, pipeline.LABEL_REVISED], entity="pr",
    )

    assert dispatchable.label == pipeline.LABEL_CHANGES_REQUESTED
    assert ambiguous.label == ""


def test_pickup_eligibility_ignores_the_other_entitys_labels() -> None:
    """A PR label stranded on an issue is not one of the issue's pickups."""
    verdict = pipeline.resolve_pickup_eligibility(
        [pipeline.LABEL_AVAILABLE, pipeline.LABEL_IMPLEMENTED],
        entity="issue", stage="implement",
    )

    assert verdict.label == pipeline.LABEL_AVAILABLE


def test_pickup_eligibility_rejects_an_unknown_entity() -> None:
    """Fail closed: an entity with no pickup vocabulary is never dispatchable."""
    verdict = pipeline.resolve_pickup_eligibility(
        [pipeline.LABEL_AVAILABLE], entity="discussion",
    )

    assert verdict.label == ""


def test_stage_pickup_labels_cover_every_in_flight_stage() -> None:
    """Both stage tables must name the same stages, or a stage can be added
    to one and forgotten in the other."""
    assert set(pipeline.STAGE_PICKUP_LABELS) == set(pipeline.STAGE_IN_FLIGHT_LABELS)
    assert set(pipeline.STAGE_ENTITIES) == set(pipeline.STAGE_IN_FLIGHT_LABELS)


# --- Operator config location ---


def test_operator_config_path_defaults_to_the_claude_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing exported, the operator's config lives in their Claude home.

    Not in the install: a plugin install lands at
    ``~/.claude/plugins/cache/<marketplace>/<plugin>/<hash>/`` and
    ``/plugin update`` installs the next version at a *different* hash, so an
    install-relative operator config is silently discarded on every upgrade.
    """
    monkeypatch.delenv("PR_PIPELINE_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert pipeline.operator_config_path() == (
        Path.home() / ".claude" / "pr-pipeline" / "config.toml"
    )


def test_operator_config_path_honours_claude_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``$CLAUDE_CONFIG_DIR`` relocates the whole Claude home, config included."""
    monkeypatch.delenv("PR_PIPELINE_CONFIG", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))

    assert pipeline.operator_config_path() == (
        tmp_path / "cfg" / "pr-pipeline" / "config.toml"
    )


def test_operator_config_path_prefers_the_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``$PR_PIPELINE_CONFIG`` names one file and outranks the Claude home.

    This is the testing override, and it is the highest-precedence location so
    a test (or a one-off run) never has to write into the operator's real home.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(tmp_path / "explicit.toml"))

    assert pipeline.operator_config_path() == tmp_path / "explicit.toml"


def test_operator_config_path_is_independent_of_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No configuration layer may be read from the target repo or a worktree.

    The tick, its CLI helpers and the engines all run from different working
    directories — an engine's cwd is a checkout of the pull request's own head.
    A config path that moved with the cwd would let a PR supply the policy it
    is judged under, which is the whole reason this file lives in the
    operator's home.
    """
    monkeypatch.delenv("PR_PIPELINE_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    worktree = tmp_path / "target-repo"
    worktree.mkdir()

    from_home = pipeline.operator_config_path()
    monkeypatch.chdir(worktree)

    assert pipeline.operator_config_path() == from_home


def test_operator_config_path_refuses_a_relative_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A relative ``$PR_PIPELINE_CONFIG`` is refused, not resolved per cwd.

    The tick chdirs to the repository root after this module is imported, so a
    relative override would have one process read identity from one file and
    policy from another — and an engine's cwd is a checkout of the pull
    request's own head, which is the one place a config must never come from.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("PR_PIPELINE_CONFIG", "settings/pr-pipeline/config.toml")
    before = pipeline.operator_config_path()
    worktree = tmp_path / "engine-worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)

    assert before.is_absolute()
    assert pipeline.operator_config_path() == before
    assert any(
        "PR_PIPELINE_CONFIG" in e for e in pipeline.resolve_config("owner/name").errors
    )


def test_operator_config_path_refuses_a_relative_claude_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``$CLAUDE_CONFIG_DIR`` carries the same cwd hazard and the same refusal."""
    monkeypatch.delenv("PR_PIPELINE_CONFIG", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "settings")
    before = pipeline.operator_config_path()
    worktree = tmp_path / "engine-worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)

    assert before.is_absolute()
    assert pipeline.operator_config_path() == before
    assert any(
        "CLAUDE_CONFIG_DIR" in e for e in pipeline.resolve_config("owner/name").errors
    )


def test_resolve_config_reads_nothing_through_a_relative_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A refused override reads no operator config at all, and fails closed.

    Falling back to the Claude home would answer with a *different* operator's
    settings than the variable named, which is exactly the confusion the
    refusal exists to prevent.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cfg = tmp_path / "relative.toml"
    cfg.write_text("[defaults]\nallow_touching = true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PR_PIPELINE_CONFIG", "relative.toml")

    config = pipeline.resolve_config("owner/name")

    assert config.allow_touching is False
    assert config.errors != ()


# --- Operator config: layering ---


@pytest.mark.parametrize("use_packaged_floor", [False, True])
def test_resolve_config_defaults_permission_mode_to_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, use_packaged_floor: bool,
) -> None:
    """An unconfigured repository gets classifier-gated Claude engines."""
    operator_config(monkeypatch, tmp_path, "")
    if use_packaged_floor:
        monkeypatch.setattr(
            pipeline, "_PACKAGED_CONFIG_PATH",
            Path(pipeline.__file__).resolve().parent.parent / "config.toml",
        )

    config = pipeline.resolve_config("owner/name")

    assert config.permission_mode == "auto"
    assert config.errors == ()


def test_resolve_config_reads_the_packaged_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """With no operator config at all, the packaged floor is the answer.

    The install ships a floor so a fresh adopter's first dispatch has a
    verification grant and a conservative gate rather than a permission denial.
    """
    packaged_floor(
        monkeypatch, tmp_path,
        '[defaults]\nverify_tools = ["uv run pytest *"]\nallow_touching = false\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert config.verify_tools == ("uv run pytest *",)
    assert config.allow_touching is False
    assert config.errors == ()


def test_resolve_config_defaults_override_the_packaged_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``[defaults]`` in the operator's own config beats the shipped floor."""
    packaged_floor(
        monkeypatch, tmp_path, '[defaults]\nverify_tools = ["uv run pytest *"]\n',
    )
    operator_config(
        monkeypatch, tmp_path, '[defaults]\nverify_tools = ["npm test *"]\n',
    )

    assert pipeline.resolve_config("owner/name").verify_tools == ("npm test *",)


def test_resolve_config_repo_section_overrides_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``[repos."owner/name"]`` beats ``[defaults]``, and only for that repo.

    One operator drives many repositories, and the settings that differ per
    target are exactly the ones a single flat table cannot express.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\n'
        'verify_tools = ["uv run pytest *"]\n'
        'base_ref = "main"\n'
        '\n'
        '[repos."owner/notes-app"]\n'
        'verify_tools = ["npm test *"]\n'
        'base_ref = "master"\n',
    )

    notes = pipeline.resolve_config("owner/notes-app")
    other = pipeline.resolve_config("owner/other")

    assert notes.verify_tools == ("npm test *",)
    assert notes.base_ref == "master"
    assert other.verify_tools == ("uv run pytest *",)
    assert other.base_ref == "main"


def test_resolve_config_reports_whether_the_target_has_its_own_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Setup's re-entry gate asks whether *this* target was ever configured.

    ``[defaults]`` answering every key is not that: it says what an operator
    wants everywhere, not that anyone looked at this repository's toolchain or
    probed its route. An empty section still counts — it is a deliberate
    statement that the defaults are right here — and the match ignores case,
    like every other repository comparison.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nverify_tools = ["make test"]\n'
        '\n[repos."Owner/Configured"]\n'
        '\n[repos."owner/tuned"]\npermission_mode = "dontAsk"\n',
    )

    assert pipeline.resolve_config("owner/configured").repo_configured is True
    assert pipeline.resolve_config("owner/tuned").repo_configured is True
    assert pipeline.resolve_config("owner/unseen").repo_configured is False
    assert pipeline.resolve_config().repo_configured is False


def test_resolve_config_lists_replace_and_never_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A repository's list replaces the default wholesale, empty included.

    Merging would mean an operator could never *narrow* a grant for one
    target, which is the wrong direction for a permission list. So presence,
    not truthiness, decides: an explicit empty list has to win.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\n'
        'verify_tools = ["uv run pytest *", "uv run ruff *"]\n'
        '\n'
        '[repos."owner/narrow"]\n'
        'verify_tools = ["uv run pytest *"]\n'
        '\n'
        '[repos."owner/none"]\n'
        'verify_tools = []\n',
    )

    assert pipeline.resolve_config("owner/narrow").verify_tools == (
        "uv run pytest *",
    )
    assert pipeline.resolve_config("owner/none").verify_tools == ()


def test_resolve_config_cli_overrides_outrank_every_file_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A flag typed on one run is the most specific statement of intent."""
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\npermission_mode = "dontAsk"\n'
        '\n[repos."owner/name"]\npermission_mode = "dontAsk"\n'
        '\n[scan]\nrepos = ["owner/name"]\n',
    )

    config = pipeline.resolve_config(
        "owner/name", cli_overrides={"permission_mode": "auto"},
    )

    assert config.permission_mode == "auto"
    assert config.errors == ()


def test_resolve_config_reports_an_unusable_cli_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A bad flag value is reported like a bad config value, never dropped."""
    operator_config(monkeypatch, tmp_path, '[defaults]\npermission_mode = "auto"\n')

    config = pipeline.resolve_config(
        "owner/name", cli_overrides={"permission_mode": "yolo"},
    )

    assert any(
        "permission_mode" in e and "the command line" in e for e in config.errors
    )


def test_resolve_config_blank_repo_reads_defaults_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No resolved target selects no per-repository layer.

    The pure-logic CLI helpers are not operating a specific target, so they
    must not silently inherit one repository's overrides.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nbase_ref = "main"\n'
        '\n[repos."owner/name"]\nbase_ref = "master"\n',
    )

    assert pipeline.resolve_config().base_ref == "main"


def test_resolve_config_warns_on_an_unknown_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A misspelled key is reported, because a silently ignored one is the bug.

    ``verify_tool`` grants nothing and looks configured. The pipeline would
    then dispatch with no verification permission and still promote the PR.
    """
    operator_config(
        monkeypatch, tmp_path, '[defaults]\nverify_tool = ["uv run pytest *"]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert any("verify_tool" in w for w in config.warnings)
    assert config.errors == ()


# --- Operator config: the operator's login ---


def test_resolve_config_reads_the_operator_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``[operator] login`` is the identity the trust gates are written against."""
    operator_config(monkeypatch, tmp_path, '[operator]\nlogin = "  userA  "\n')

    assert pipeline.resolve_config("owner/name").login == "userA"


def test_resolve_config_login_is_not_per_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A ``login`` under ``[repos.*]`` is not an override — it is a typo.

    It is an identity, not a per-target setting: a per-repository override
    would mean the pipeline reviewing PRs as somebody else on one repo, a
    trust boundary change wearing a configuration key's clothes.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[operator]\nlogin = "userA"\n'
        '\n[repos."owner/name"]\nlogin = "userB"\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert config.login == "userA"
    assert any("login" in w for w in config.warnings)


def test_resolve_config_env_overrides_the_operator_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``PIPELINE_TRUSTED_AUTHOR`` keeps overriding the configured login."""
    operator_config(monkeypatch, tmp_path, '[operator]\nlogin = "userA"\n')
    monkeypatch.setenv("PIPELINE_TRUSTED_AUTHOR", "userB")

    assert pipeline.resolve_config("owner/name").login == "userB"


def test_resolve_config_login_falls_back_to_the_deprecated_remnant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An install configured before this change keeps working across the merge.

    ``[target] trusted_author`` in the packaged file is a deprecated remnant,
    read only so an operator who filled it in does not lose their pipeline on
    the merge that moves configuration to their home. Repository identity is
    resolved independently from Git; its deprecated config key is ignored.
    """
    packaged_floor(
        monkeypatch, tmp_path,
        '[target]\nrepo = "owner/name"\ntrusted_author = "userA"\n',
    )
    operator_config(monkeypatch, tmp_path, "")  # nothing has moved home yet

    config = pipeline.resolve_config("owner/name")

    assert config.login == "userA"
    assert not hasattr(config, "legacy_target_repo")
    assert any("[target] repo" in w for w in config.warnings)
    assert config.errors == ()


def test_resolve_config_warns_when_target_is_set_in_the_operator_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`[target]` in the operator's own config is read by nothing.

    Repository identity comes from Git, so a stale target declaration produces
    an explicit migration warning instead of silently appearing effective.
    """
    operator_config(
        monkeypatch, tmp_path, '[target]\nrepo = "owner/name"\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert not hasattr(config, "legacy_target_repo")
    assert any("[target]" in w for w in config.warnings)


def test_resolve_config_operator_login_beats_the_deprecated_remnant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The new key wins, so a migrated operator is not shadowed by the floor."""
    packaged_floor(monkeypatch, tmp_path, '[target]\ntrusted_author = "old"\n')
    operator_config(monkeypatch, tmp_path, '[operator]\nlogin = "new"\n')

    assert pipeline.resolve_config("owner/name").login == "new"


# --- Operator config: settings with no conservative fallback ---


def test_resolve_config_reports_a_malformed_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A login that matches nobody is an error, not "escalate every PR".

    The login is compared for equality against a PR author. Coerced to empty,
    a typo reads as "no author is trusted" — a pipeline that silently stops
    reviewing rather than one that says why.
    """
    for bad in ("login = 42", 'login = "user A"', 'login = "owner/name"'):
        operator_config(monkeypatch, tmp_path, f"[operator]\n{bad}\n")

        config = pipeline.resolve_config("owner/name")

        assert any("login" in e for e in config.errors), bad
        assert config.login == "", bad


def test_resolve_config_reports_a_malformed_base_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``base_ref`` has no restrictive value, so a bad one is a hard error.

    It reaches ``git fetch origin <ref>`` and ``gh pr create --base <ref>``.
    There is no conservative branch to fall back to: silently picking one
    would target a branch the operator never named.
    """
    for bad in ("base_ref = 7", 'base_ref = "--upload-pack=x"',
                'base_ref = "a..b"', 'base_ref = "feature/"'):
        operator_config(monkeypatch, tmp_path, f"[defaults]\n{bad}\n")

        config = pipeline.resolve_config("owner/name")

        assert any("base_ref" in e for e in config.errors), bad


def test_resolve_config_blank_base_ref_is_not_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A blank ``base_ref`` is the documented "resolve the default branch"."""
    operator_config(monkeypatch, tmp_path, '[defaults]\nbase_ref = ""\n')

    config = pipeline.resolve_config("owner/name")

    assert config.base_ref == ""
    assert config.errors == ()


def test_resolve_config_reports_a_malformed_permission_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An unsupported permission mode is an error, not a silent downgrade.

    Coercing it would either widen the engine's permissions past what the
    operator wrote, or narrow them and fail the run on a denial that reads to
    the engine as a broken repository.
    """
    operator_config(monkeypatch, tmp_path, '[defaults]\npermission_mode = "yolo"\n')

    config = pipeline.resolve_config("owner/name")

    assert any("permission_mode" in e for e in config.errors)


def test_resolve_config_reports_a_blank_permission_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Blank is a documented value for ``base_ref`` only, never for this key.

    Accepted here it passes the startup gate and reaches the engine launch,
    which raises *after* a dispatch lease and worktree exist — a tick crash
    and a recovery path instead of a legible refusal.
    """
    for blank in ('""', '"   "'):
        operator_config(
            monkeypatch, tmp_path, f"[defaults]\npermission_mode = {blank}\n",
        )

        config = pipeline.resolve_config("owner/name")

        assert any("permission_mode" in e for e in config.errors), blank


def test_resolve_config_leaves_engine_settings_unset_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Unset means no ``--settings`` flag: engines load the Claude hierarchy."""
    operator_config(monkeypatch, tmp_path, "")

    config = pipeline.resolve_config("owner/name")

    assert config.engine_settings == ""
    assert config.errors == ()


@pytest.mark.parametrize("value", [
    '{"sandbox": {"enabled": false}}',
    "SETTINGS_FILE",
])
def test_resolve_config_accepts_engine_settings_json_or_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str,
) -> None:
    """Both shapes ``claude --settings`` takes are accepted per repository."""
    if value == "SETTINGS_FILE":
        settings_file = tmp_path / "engine-settings.json"
        settings_file.write_text('{"sandbox": {"enabled": false}}\n')
        value = str(settings_file)
    operator_config(
        monkeypatch, tmp_path,
        f"[defaults]\nengine_settings = ''\n\n"
        f"[repos.\"owner/name\"]\nengine_settings = '{value}'\n",
    )

    config = pipeline.resolve_config("owner/name")

    assert config.engine_settings == value
    assert config.errors == ()


@pytest.mark.parametrize("value", [
    "engine-settings.json",
    "./engine-settings.json",
    '{"sandbox": ',
    '["sandbox"]',
])
def test_resolve_config_reports_unusable_engine_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str,
) -> None:
    """A relative path or malformed JSON is an error, never a dropped flag.

    A relative path resolves against the engine's cwd, which is a checkout of
    the pull request under work: the PR would supply its own engine settings.
    Malformed JSON dropped silently would launch engines under the very
    interactive settings the operator configured this key to override.
    """
    operator_config(
        monkeypatch, tmp_path, f"[defaults]\nengine_settings = '{value}'\n",
    )

    config = pipeline.resolve_config("owner/name")

    assert any("engine_settings" in e for e in config.errors)
    assert config.engine_settings == ""


@pytest.mark.parametrize("use_packaged_floor", [False, True])
def test_resolve_config_defaults_max_in_flight_to_three(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, use_packaged_floor: bool,
) -> None:
    """With no operator override the admission cap stays at three."""
    operator_config(monkeypatch, tmp_path, "")
    if use_packaged_floor:
        monkeypatch.setattr(
            pipeline, "_PACKAGED_CONFIG_PATH",
            Path(pipeline.__file__).resolve().parent.parent / "config.toml",
        )

    config = pipeline.resolve_config("owner/name")

    assert config.max_in_flight == 3
    assert config.errors == ()
    assert not [w for w in config.warnings if "max_in_flight" in w]


def test_resolve_config_layers_max_in_flight_per_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A repository override beats ``[defaults]``, which beats the floor."""
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nmax_in_flight = 2\n\n'
        '[repos."owner/busy"]\nmax_in_flight = 5\n',
    )

    assert pipeline.resolve_config("owner/busy").max_in_flight == 5
    assert pipeline.resolve_config("owner/other").max_in_flight == 2
    assert pipeline.resolve_config("owner/busy").errors == ()


@pytest.mark.parametrize("raw", ["0", "-1", "true", "2.5", '"4"'])
def test_resolve_config_reports_unusable_max_in_flight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str,
) -> None:
    """A malformed cap is an error naming the key, never a different cap.

    No reading of ``0``, ``true`` or ``"4"`` is the one the operator wrote, and
    coercing any of them to the shipped default would let the tick run at a
    concurrency the operator did not choose.
    """
    operator_config(
        monkeypatch, tmp_path,
        f'[defaults]\nmax_in_flight = 4\n\n[repos."owner/name"]\nmax_in_flight = {raw}\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert [e for e in config.errors if "max_in_flight" in e], config.errors


@pytest.mark.parametrize("contents", [
    None,
    '{"sandbox": ',
    '["sandbox"]',
    "DIRECTORY",
])
def test_resolve_config_reports_unusable_engine_settings_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str | None,
) -> None:
    """A settings file must exist and hold a JSON object when config resolves.

    ``claude --settings`` does not reject a file it cannot parse, so a
    missing, malformed, or non-object file would launch engines under the
    interactive settings the operator configured this key to override.
    """
    settings_file = tmp_path / "engine-settings.json"
    if contents == "DIRECTORY":
        settings_file.mkdir()
    elif contents is not None:
        settings_file.write_text(contents)
    operator_config(
        monkeypatch, tmp_path,
        f"[defaults]\nengine_settings = '{settings_file}'\n",
    )

    config = pipeline.resolve_config("owner/name")

    assert any("engine_settings" in e for e in config.errors)
    assert config.engine_settings == ""


def test_resolve_config_malformed_verify_tools_is_not_empty_verify_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """These are different states and they must not collapse into one.

    An empty list is a deliberate operator choice that dispatches with a
    warning. A list that failed to parse, read as empty, would silently become
    an unverified PR promoted to ai:ready-for-human.
    """
    operator_config(
        monkeypatch, tmp_path, '[defaults]\nverify_tools = "uv run pytest"\n',
    )
    malformed = pipeline.resolve_config("owner/name")

    operator_config(monkeypatch, tmp_path, "[defaults]\nverify_tools = []\n")
    empty = pipeline.resolve_config("owner/name")

    assert any("verify_tools" in e for e in malformed.errors)
    assert empty.verify_tools == ()
    assert empty.errors == ()


def test_resolve_config_reports_a_non_string_verify_tools_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One unusable entry in a permission grant is an error, not a filter.

    Dropping it would hand the engine a narrower grant than the operator wrote
    and surface as an unexplained permission denial mid-run.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nverify_tools = ["uv run pytest *", 7]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert any("verify_tools" in e for e in config.errors)


def test_resolve_config_reports_a_verify_tools_entry_that_is_not_a_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A grant that cannot be a `Bash(...)` body is malformed, not droppable.

    ``--allowedTools`` takes ``Tool(body)`` entries, so a parenthesis or a
    newline inside a body does not describe the permission the operator typed.
    Dropping such an entry downstream would turn a one-character typo into a
    silently narrower grant, and an all-invalid list into the unconfigured
    empty grant that dispatches unverified work to ai:ready-for-human.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nverify_tools = ["uv run pytest *", "cargo test)"]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert any("verify_tools" in e for e in config.errors)
    assert config.verify_tools == ()


def test_resolve_config_reports_an_all_invalid_verify_tools_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An all-invalid list must not arrive as a deliberate empty grant.

    ``verify_tools = []`` is an operator choice that dispatches with a warning.
    A list whose every entry was rejected is a file to fix, and the two must
    not resolve to the same state.
    """
    operator_config(
        monkeypatch, tmp_path, '[defaults]\nverify_tools = ["cargo test)"]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert config.verify_tools == ()
    assert any("verify_tools" in e for e in config.errors)


def test_resolve_config_drops_blank_list_entries_without_reporting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A blank entry is a trailing comma, not a statement about policy."""
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nverify_tools = ["  uv run pytest *  ", ""]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert config.verify_tools == ("uv run pytest *",)
    assert config.errors == ()


# --- Operator config: fail-closed settings ---


def test_resolve_config_allow_touching_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A non-boolean gate keeps the conservative gate and says so.

    This setting *has* a safe direction, so it is warned about rather than
    refused: a tick that still runs with the gate closed is strictly better
    than one that will not run at all.
    """
    operator_config(monkeypatch, tmp_path, '[defaults]\nallow_touching = "yes"\n')

    config = pipeline.resolve_config("owner/name")

    assert config.allow_touching is False
    assert any("allow_touching" in w for w in config.warnings)
    assert config.errors == ()


def test_resolve_config_checklist_paths_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An unusable ``checklist_paths`` yields no paths, and refuses the tick.

    CLAUDE.md and AGENTS.md still gate by basename with no configuration at
    all, so the gate does not disappear — but the paths the operator *named*
    are protection, and running on without them is running with a weaker gate
    than the one they wrote. There is nothing conservative to fall back to, so
    this is a startup error rather than a warning.
    """
    operator_config(
        monkeypatch, tmp_path, '[defaults]\nchecklist_paths = "docs/x.md"\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert config.checklist_paths == ()
    assert any("checklist_paths" in e for e in config.errors)


def test_resolve_config_malformed_checklist_paths_keeps_the_valid_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One bad entry must not strip the gate of the paths that *are* valid.

    ``checklist_paths`` is a list of files to **block**, so the empty set is
    the permissive direction: discarding the list on a typo would let a PR
    that rewrites the reviewer's own checklist walk into automatic review.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nchecklist_paths = ["docs/review.md", 7]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert config.checklist_paths == ("docs/review.md",)
    assert any("checklist_paths" in e for e in config.errors)
    assert pipeline.touches_review_policy(["docs/review.md"], "owner/name") is True


def test_resolve_config_malformed_repo_checklist_paths_still_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A malformed per-repo override may not silently unprotect valid defaults.

    Lists replace wholesale, so a bad ``[repos."owner/name"]`` value would
    otherwise erase a perfectly good ``[defaults]`` list for that one target.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nchecklist_paths = ["docs/review.md"]\n'
        '\n[repos."owner/name"]\nchecklist_paths = "docs/review.md"\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert any("checklist_paths" in e for e in config.errors)
    assert pipeline.touches_review_policy(["docs/review.md"], "owner/name") is True


def test_resolve_config_reports_a_malformed_defaults_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A declared-but-not-a-table ``[defaults]`` is an error, not an absence.

    ``[[defaults]]`` is valid TOML and an easy slip. Read as absent, every
    setting under it — verification grants included — is silently replaced by
    the shipped floor with nothing said.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[[defaults]]\nverify_tools = ["cargo test *"]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert any("defaults" in e for e in config.errors)


def test_resolve_config_reports_a_malformed_operator_or_repos_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The identity and per-repository containers get the same type check."""
    for text, key in (
        ('[[operator]]\nlogin = "userA"\n', "operator"),
        ('[[repos]]\nname = "owner/name"\n', "repos"),
    ):
        operator_config(monkeypatch, tmp_path, text)

        config = pipeline.resolve_config("owner/name")

        assert any(key in e for e in config.errors), text


def test_resolve_config_reports_malformed_toml_and_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An unparseable config is reported *and* reads conservatively.

    Both halves matter: the error is what refuses the tick, and the empty read
    is what keeps the gate shut for any caller that does not act on errors.
    """
    cfg = operator_config(monkeypatch, tmp_path, "")
    cfg.write_text("[defaults\nallow_touching = tru", encoding="utf-8")

    config = pipeline.resolve_config("owner/name")

    assert config.errors != ()
    assert config.allow_touching is False
    assert pipeline.touches_review_policy(["CLAUDE.md"]) is True


# --- Operator config: [scan] repos ---


def test_resolve_config_scan_repos_absent_or_empty_is_unrestricted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Two states and no third: absent and empty both mean unrestricted.

    Both are things the operator wrote, which is the point — see the malformed
    case, which must not land here.
    """
    operator_config(monkeypatch, tmp_path, '[operator]\nlogin = "userA"\n')
    assert pipeline.resolve_config("owner/name").scan_repos is None

    operator_config(monkeypatch, tmp_path, "[scan]\nrepos = []\n")
    assert pipeline.resolve_config("owner/name").scan_repos is None


def test_resolve_config_scan_repos_restricts_when_non_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A non-empty list is the eligible set, exactly as written."""
    operator_config(
        monkeypatch, tmp_path, '[scan]\nrepos = ["owner/one", " owner/two "]\n',
    )

    assert pipeline.resolve_config().scan_repos == ("owner/one", "owner/two")


def test_resolve_config_malformed_scan_repos_never_reads_as_unrestricted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A typo must be an error, never silently coerced to either state.

    "Unrestricted" must be something the operator wrote. A malformed value
    resolves to the empty *restrictive* set, so even a caller that ignores the
    error cannot read it as "every repository is eligible".
    """
    for bad in ('repos = "owner/name"', "repos = [7]", 'repos = ["not-a-repo"]'):
        operator_config(monkeypatch, tmp_path, f"[scan]\n{bad}\n")

        config = pipeline.resolve_config("owner/name")

        assert any("[scan] repos" in e for e in config.errors), bad
        assert config.scan_repos == (), bad


def test_resolve_config_malformed_scan_container_never_reads_as_unrestricted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``[[scan]]`` is a declared scope, not an absent one.

    Read as absent it resolves to unrestricted with nothing reported — the one
    state that must only ever come from something the operator wrote.
    """
    operator_config(
        monkeypatch, tmp_path, '[[scan]]\nrepos = ["owner/only"]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert any("scan" in e for e in config.errors)
    assert config.scan_repos == ()


def test_resolve_config_reports_a_repo_section_outside_a_restricted_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Settings for a repository the operator excluded are a config error.

    Silently keeping them would leave an operator convinced they had
    configured a target the pipeline will never look at.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[scan]\nrepos = ["owner/one"]\n'
        '\n[repos."owner/two"]\nbase_ref = "main"\n',
    )

    config = pipeline.resolve_config("owner/one")

    assert any('"owner/two"' in e and "[scan] repos" in e for e in config.errors)


def test_resolve_config_reports_a_malformed_repos_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A ``[repos.*]`` key must be ``owner/name`` exactly as ``gh`` reports it.

    Anything else can never match a resolved target, so its settings would
    never apply and nothing would say why.
    """
    operator_config(
        monkeypatch, tmp_path, '[repos."notes-app"]\nbase_ref = "main"\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert any("notes-app" in e for e in config.errors)


def test_resolve_config_accepts_a_repository_named_dot_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``owner/.github`` is a real repository, and every section is validated.

    GitHub's organization profile lives in a repository literally named
    ``.github``. Applying the owner half's leading-alphanumeric rule to the
    name half rejects it, and because every ``[repos.*]`` key is checked at
    startup, one such section stops ticks against every *other* target too.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[repos."owner/.github"]\nbase_ref = "main"\n'
        '\n[scan]\nrepos = ["owner/.github", "owner/name"]\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert config.errors == ()
    assert pipeline.resolve_config("owner/.github").base_ref == "main"


def test_resolve_config_reports_a_repository_named_only_dots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``.`` and ``..`` are path traversal, not repository names.

    Accepting GitHub's name grammar must not accept the two names GitHub
    itself forbids: they are the path segments that walk out of whatever
    directory a resolved target is joined into.
    """
    for name in (".", ".."):
        operator_config(
            monkeypatch, tmp_path, f'[repos."owner/{name}"]\nbase_ref = "main"\n',
        )

        config = pipeline.resolve_config("owner/name")

        assert any("owner/" in e for e in config.errors), name


# --- Operator config: the packaged floor is layer 1 and only layer 1 ---


def test_resolve_config_refuses_operator_tables_in_the_packaged_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An operator value in the install is reported, never read.

    A plugin install lands at a hash-named path and ``/plugin update``
    installs the next version at a *different* hash, so a value written there
    is discarded on upgrade — including verification grants, which means the
    next dispatch runs with no verification and still awards
    ai:ready-for-human. Nothing would error. This does.
    """
    for table in ('[operator]\nlogin = "userA"\n', "[scan]\nrepos = []\n",
                  '[repos."owner/name"]\nbase_ref = "main"\n'):
        packaged_floor(monkeypatch, tmp_path, table)

        config = pipeline.resolve_config("owner/name")

        assert any(
            "must not ship in the install" in e for e in config.errors
        ), table


def test_resolve_config_reads_no_config_from_the_install_or_a_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Exactly two files are read, and neither sits in a repository checkout.

    This is the property section 2 of the design says gets a test: no
    configuration layer is ever read from inside a target repository or an
    engine worktree. It must fail loudly if a later convenience override
    reintroduces such a path — a reviewer runs with its cwd inside a checkout
    of the pull request's own head, so a config read from there is a PR
    supplying the policy it is judged under.
    """
    read: list[Path] = []
    real_read_toml = pipeline._read_toml

    def recording(path: Path) -> tuple[dict[str, object], str]:
        read.append(path)
        return real_read_toml(path)

    monkeypatch.setattr(pipeline, "_read_toml", recording)
    monkeypatch.setattr(pipeline, "_PACKAGED_CONFIG_PATH", tmp_path / "floor.toml")
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(tmp_path / "home" / "config.toml"))
    worktree = tmp_path / "engine-worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)

    pipeline.resolve_config("owner/name")

    assert read == [tmp_path / "floor.toml", tmp_path / "home" / "config.toml"], (
        "resolve_config read a configuration file it must not read"
    )
    assert not any(p.is_relative_to(worktree) for p in read)


# --- Operator config: tuning, routing and cadence ---


@pytest.mark.parametrize("use_packaged_floor", [False, True])
def test_resolve_config_tuning_defaults_are_todays_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, use_packaged_floor: bool,
) -> None:
    """With no override, a tick runs on the values the constants held.

    Checked against the shipped floor and against no floor at all: the floor
    states the defaults for the operator, and the resolver answers the same
    way for an install whose floor omits a key.
    """
    operator_config(monkeypatch, tmp_path, "")
    if use_packaged_floor:
        monkeypatch.setattr(
            pipeline, "_PACKAGED_CONFIG_PATH",
            Path(pipeline.__file__).resolve().parent.parent / "config.toml",
        )

    config = pipeline.resolve_config("owner/name")

    assert config.errors == ()
    assert config.warnings == ()
    assert config.implementer_timeout_seconds == 60 * 60
    assert config.fixer_timeout_seconds == 60 * 60
    assert config.review_timeout_seconds == 20 * 60
    assert config.engine_stall_seconds == 30 * 60
    assert config.engine_stall_startup_grace_seconds == 5 * 60
    assert config.max_iterations == pipeline.MAX_ITERATIONS == 5
    assert config.impl_engine == "claude"
    assert config.model == ""
    assert config.fix_engine == "claude"
    assert config.fix_model == ""
    assert config.review_model == ""
    assert config.review_effort == ""
    assert config.effort == ""
    assert config.fix_effort == ""
    assert config.loop_short_poll_seconds == 270
    assert config.loop_max_poll_seconds == 270
    assert config.loop_heartbeat_seconds == 30 * 60
    assert config.loop_quiescent_stop_after == 3
    assert config.transient_cooldown_seconds == 60 * 60
    assert config.transient_cooldown_reset_buffer_seconds == 5 * 60
    assert config.probed_route is None
    if use_packaged_floor:
        floor = pipeline._read_toml(pipeline._PACKAGED_CONFIG_PATH)[0]
        shipped = floor["defaults"]
        assert isinstance(shipped, dict)
        assert set(pipeline._INT_SETTINGS) | {
            "impl_engine", "model", "fix_engine", "fix_model",
            "review_model", "review_effort", "effort", "fix_effort",
        } <= set(shipped), "the shipped floor must state every default"


def test_resolve_config_tuning_layers_like_every_other_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``[defaults]`` sets every target, and a repository section beats it."""
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\n'
        'implementer_timeout_seconds = 7200\n'
        'max_iterations = 8\n'
        'loop_heartbeat_seconds = 3600\n'
        '\n[repos."owner/slow"]\n'
        'implementer_timeout_seconds = 10800\n'
        'fixer_timeout_seconds = 5400\n'
        'review_timeout_seconds = 2400\n'
        'engine_stall_seconds = 2700\n'
        'engine_stall_startup_grace_seconds = 0\n'
        'max_iterations = 2\n'
        'loop_short_poll_seconds = 120\n'
        'loop_max_poll_seconds = 240\n'
        'loop_quiescent_stop_after = 6\n'
        'transient_cooldown_seconds = 900\n'
        'transient_cooldown_reset_buffer_seconds = 0\n',
    )

    slow = pipeline.resolve_config("owner/slow")
    other = pipeline.resolve_config("owner/other")

    assert slow.errors == other.errors == ()
    assert slow.warnings == other.warnings == ()
    assert (
        slow.implementer_timeout_seconds, slow.fixer_timeout_seconds,
        slow.review_timeout_seconds, slow.engine_stall_seconds,
        slow.engine_stall_startup_grace_seconds, slow.max_iterations,
        slow.loop_short_poll_seconds, slow.loop_max_poll_seconds,
        slow.loop_heartbeat_seconds, slow.loop_quiescent_stop_after,
        slow.transient_cooldown_seconds,
        slow.transient_cooldown_reset_buffer_seconds,
    ) == (10800, 5400, 2400, 2700, 0, 2, 120, 240, 3600, 6, 900, 0)
    assert other.implementer_timeout_seconds == 7200
    assert other.fixer_timeout_seconds == 3600
    assert other.max_iterations == 8
    assert other.loop_heartbeat_seconds == 3600


@pytest.mark.parametrize("key, value, expected", [
    ("implementer_timeout_seconds", "true", "expected an integer"),
    ("implementer_timeout_seconds", "3600.0", "expected an integer"),
    ("implementer_timeout_seconds", '"3600"', "expected an integer"),
    ("fixer_timeout_seconds", "59", "at least 60"),
    ("review_timeout_seconds", "0", "at least 60"),
    ("review_timeout_seconds", "-1200", "at least 60"),
    ("engine_stall_seconds", "false", "expected an integer"),
    ("engine_stall_startup_grace_seconds", "-1", "at least 0"),
    ("max_iterations", "0", "at least 1"),
    ("max_iterations", "2.5", "expected an integer"),
    ("loop_short_poll_seconds", "59", "at least 60"),
    ("loop_max_poll_seconds", "[270]", "expected an integer"),
    ("loop_heartbeat_seconds", '"1800"', "expected an integer"),
    ("loop_quiescent_stop_after", "0", "at least 1"),
    ("transient_cooldown_seconds", "30", "at least 60"),
    ("transient_cooldown_reset_buffer_seconds", "-5", "at least 0"),
])
def test_resolve_config_reports_a_malformed_tuning_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    key: str, value: str, expected: str,
) -> None:
    """Each malformed integer is an error naming the key, never a fallback.

    TOML ``true`` is a Python ``int``, so a boolean has to be refused by name;
    a float or a string must not be rounded or parsed into a deadline.
    """
    operator_config(monkeypatch, tmp_path, f'[repos."owner/name"]\n{key} = {value}\n')

    config = pipeline.resolve_config("owner/name")

    assert any(key in e and expected in e for e in config.errors), config.errors


@pytest.mark.parametrize("key", [
    "loop_short_poll_seconds", "loop_max_poll_seconds", "loop_heartbeat_seconds",
])
def test_resolve_config_refuses_a_wake_on_the_prompt_cache_cliff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str,
) -> None:
    """300 seconds pays a prompt-cache miss without amortizing it.

    The cadence contract never picks that interval, so a configured one is
    refused rather than quietly moved to a different wake.
    """
    operator_config(monkeypatch, tmp_path, f"[defaults]\n{key} = 300\n")

    config = pipeline.resolve_config("owner/name")

    assert any(key in e and "300" in e for e in config.errors), config.errors


@pytest.mark.parametrize("text, names", [
    # Not greater than the longest silence a healthy engine produces.
    ("engine_stall_seconds = 600\n", "engine_stall_seconds"),
    # Not less than the implementer deadline.
    ("implementer_timeout_seconds = 1800\n", "implementer_timeout_seconds"),
    # Not less than the fixer deadline.
    ("engine_stall_seconds = 2400\nfixer_timeout_seconds = 2400\n",
     "fixer_timeout_seconds"),
])
def test_resolve_config_keeps_the_stall_threshold_between_its_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, names: str,
) -> None:
    """A stall threshold must sit above legitimate silence and below deadlines.

    At or below the longest legitimate silence it reports healthy engines. At
    or above a deadline it can never fire before the deadline does.
    """
    operator_config(monkeypatch, tmp_path, f"[defaults]\n{text}")

    config = pipeline.resolve_config("owner/name")

    assert any(
        "engine_stall_seconds" in e and names in e for e in config.errors
    ), config.errors


def test_resolve_config_resolves_the_engine_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Every route key layers, and the fixer's route is independent."""
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\n'
        'impl_engine = "claude-ds"\n'
        'review_effort = "xhigh"\n'
        '\n[repos."owner/name"]\n'
        'impl_engine = "claude"\n'
        'model = "claude-opus-5-5[1m]"\n'
        'fix_engine = "claude-ds"\n'
        'fix_model = "deepseek-v4-flash"\n'
        'review_model = "gpt-6"\n',
    )

    config = pipeline.resolve_config("owner/name")
    other = pipeline.resolve_config("owner/other")

    assert config.errors == other.errors == ()
    assert (config.impl_engine, config.model) == ("claude", "claude-opus-5-5[1m]")
    assert (config.fix_engine, config.fix_model) == ("claude-ds", "deepseek-v4-flash")
    assert (config.review_model, config.review_effort) == ("gpt-6", "xhigh")
    assert other.impl_engine == other.fix_engine == "claude-ds"


@pytest.mark.parametrize("text, fix_route", [
    # A blank fixer route is the implementer's route, model included.
    ('model = "opus"\n', ("claude", "opus")),
    # The same engine named explicitly still inherits the model.
    ('model = "opus"\nfix_engine = "claude"\n', ("claude", "opus")),
    # Another engine does not inherit a model meant for a different provider.
    ('model = "opus"\nfix_engine = "claude-ds"\n', ("claude-ds", "")),
    ('fix_model = "sonnet"\n', ("claude", "sonnet")),
])
def test_resolve_config_fixer_route_inherits_only_a_usable_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    text: str, fix_route: tuple[str, str],
) -> None:
    """A blank fixer key inherits the implementer's, never across engines."""
    operator_config(monkeypatch, tmp_path, f'[defaults]\n{text}')

    config = pipeline.resolve_config("owner/name")

    assert config.errors == ()
    assert (config.fix_engine, config.fix_model) == fix_route


@pytest.mark.parametrize("key, value", [
    ("impl_engine", '""'),
    ("impl_engine", '"codex"'),
    ("impl_engine", "true"),
    ("fix_engine", '"claude-dss"'),
    ("model", '"-p"'),
    ("model", '"opus 4"'),
    ("fix_model", "7"),
    ("review_model", '"--yolo"'),
    ("review_effort", '"high; rm"'),
    ("effort", '"ultra"'),
    ("effort", "3"),
    ("fix_effort", '"--loop"'),
])
def test_resolve_config_reports_an_unusable_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, value: str,
) -> None:
    """A route value that cannot reach its argv sink stops the tick."""
    operator_config(monkeypatch, tmp_path, f"[defaults]\n{key} = {value}\n")

    config = pipeline.resolve_config("owner/name")

    assert any(key in e for e in config.errors), config.errors


@pytest.mark.parametrize("text, efforts", [
    # No layer sets effort: neither stage gets an override.
    ("", ("", "")),
    # A blank fix_effort inherits effort on the implementer's engine.
    ('effort = "high"\n', ("high", "high")),
    # It also inherits on another engine: effort levels belong to no provider.
    ('effort = "high"\nfix_engine = "claude-ds"\n', ("high", "high")),
    # A set fix_effort replaces effort for the fixer only.
    ('effort = "high"\nfix_effort = "low"\n', ("high", "low")),
    ('fix_effort = "max"\n', ("", "max")),
])
def test_resolve_config_fixer_effort_inherits_on_any_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    text: str, efforts: tuple[str, str],
) -> None:
    """A blank fix_effort is the implementer's effort on every fixer engine."""
    operator_config(monkeypatch, tmp_path, f'[defaults]\n{text}')

    config = pipeline.resolve_config("owner/name")

    assert config.errors == ()
    assert (config.effort, config.fix_effort) == efforts


def test_resolve_config_effort_flags_outrank_every_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--effort and --fix-effort beat the repository section and [defaults]."""
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\neffort = "low"\nfix_effort = "low"\n'
        '\n[repos."owner/name"]\neffort = "medium"\nfix_effort = "medium"\n',
    )

    config = pipeline.resolve_config("owner/name", cli_overrides={
        "effort": "xhigh", "fix_effort": "max",
    })
    refused = pipeline.resolve_config(
        "owner/name", cli_overrides={"fix_effort": "ultra"},
    )

    assert config.errors == ()
    assert (config.effort, config.fix_effort) == ("xhigh", "max")
    assert any(
        "fix_effort" in e and "the command line" in e for e in refused.errors
    )


def test_resolve_config_route_flags_outrank_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A typed route flag wins, and is validated like a config value."""
    operator_config(
        monkeypatch, tmp_path,
        '[repos."owner/name"]\nimpl_engine = "claude"\nmodel = "opus"\n'
        'review_model = "gpt-6"\n',
    )

    config = pipeline.resolve_config("owner/name", cli_overrides={
        "impl_engine": "claude-ds", "model": "deepseek-v4-pro[1m]",
        "fix_engine": "claude", "review_effort": "low",
    })
    refused = pipeline.resolve_config(
        "owner/name", cli_overrides={"impl_engine": "nope"},
    )

    assert config.errors == ()
    assert (config.impl_engine, config.model) == ("claude-ds", "deepseek-v4-pro[1m]")
    assert (config.fix_engine, config.fix_model) == ("claude", "")
    assert (config.review_model, config.review_effort) == ("gpt-6", "low")
    assert any(
        "impl_engine" in e and "the command line" in e for e in refused.errors
    )


def test_resolve_config_reports_the_route_recorded_for_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Only the repository's own section records a probed route.

    Setup writes ``probed_impl_engine`` and ``probed_model`` there; a
    ``[defaults]`` route or a flag says nothing about what was measured.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\nimpl_engine = "claude-ds"\nmodel = "x"\n'
        'probed_impl_engine = "claude-ds"\nprobed_model = "x"\n'
        '\n[repos."owner/probed"]\nimpl_engine = "claude"\nmodel = "haiku"\n'
        'probed_impl_engine = "claude"\nprobed_model = ""\n'
        '\n[repos."owner/half"]\nimpl_engine = "claude"\n',
    )

    probed = pipeline.resolve_config(
        "owner/probed", cli_overrides={"model": "opus"},
    )

    assert probed.probed_route == ("claude", "")
    assert probed.model == "opus"
    assert pipeline.resolve_config("owner/other").probed_route is None
    assert pipeline.resolve_config("owner/half").probed_route is None


@pytest.mark.parametrize("metadata, key", [
    ('probed_impl_engine = "claude"\n', "probed_model"),
    ('probed_model = "sonnet"\n', "probed_impl_engine"),
    ('probed_impl_engine = "codex"\nprobed_model = "sonnet"\n', "probed_impl_engine"),
    ('probed_impl_engine = "claude"\nprobed_model = true\n', "probed_model"),
    ('probed_impl_engine = "claude"\nprobed_model = "-p"\n', "probed_model"),
])
def test_resolve_config_refuses_incomplete_or_invalid_probe_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, metadata: str, key: str,
) -> None:
    operator_config(monkeypatch, tmp_path, '[repos."owner/probed"]\n' + metadata)

    config = pipeline.resolve_config("owner/probed")

    assert config.probed_route is None
    assert any(key in error for error in config.errors)
    assert any(key in error for error in config.tuning_errors)


def test_resolve_config_separates_the_errors_a_tick_reads_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Tuning and route errors are marked, so dispatch's re-read can skip them.

    Every error still stops the tick at startup. Only the subset in keys read
    once at that gate is excluded from dispatch's later read of the keys it
    still re-reads; an invalid ``permission_mode`` must keep stopping dispatch.
    """
    operator_config(
        monkeypatch, tmp_path,
        '[defaults]\npermission_mode = "yolo"\nmax_iterations = 0\n'
        'fix_engine = "x"\nengine_stall_seconds = 4000\nmax_in_flight = 0\n',
    )

    config = pipeline.resolve_config("owner/name")

    assert len(config.errors) == 6
    assert set(config.tuning_errors) < set(config.errors)
    assert [e for e in config.errors if e not in config.tuning_errors] == [
        e for e in config.errors if "permission_mode" in e
    ]


@pytest.mark.parametrize("engine, model, expected", [
    ("claude", "", ""),
    ("claude", "opus", "opus"),
    ("claude-ds", "", pipeline.CLAUDE_DS_DEFAULT_MODEL),
    ("claude-ds", "deepseek-v4-flash", "deepseek-v4-flash"),
])
def test_effective_engine_model_names_the_model_dispatch_runs(
    engine: str, model: str, expected: str,
) -> None:
    """claude-ds has no operator default to inherit, so blank is its pin."""
    assert pipeline.effective_engine_model(engine, model) == expected


def test_operator_config_path_is_never_inside_the_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's file must not resolve into the install directory.

    The packaged floor is install-relative by design and is the only thing
    that may be. If a later change ever points the operator's own config back
    into the install — the shape this issue exists to remove — every setting is
    silently discarded by the next ``/plugin update``.
    """
    monkeypatch.delenv("PR_PIPELINE_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    install_root = Path(pipeline.__file__).resolve().parent.parent

    assert not pipeline.operator_config_path().is_relative_to(install_root)


def test_skill_doc_path_resolves_inside_the_install() -> None:
    """A bound skill doc is the install's file, not the target repository's.

    The fixer's worktree is a detached checkout of a PR head in a repository
    the pipeline does not own, so a repo-relative binding resolves to a file
    only this repository happens to ship. Resolving against the installed
    ``skills/`` root — derived from ``pipeline.py``'s own location, the same
    way the packaged config floor is — is what makes the doc present for every
    target by construction.
    """
    install_skills_root = Path(pipeline.__file__).resolve().parents[2]

    path = Path(pipeline.skill_doc_path(pipeline.REVISION_HYGIENE_SKILL))

    assert path.is_absolute(), (
        f"{path} is relative, so a dispatched engine resolves it against its "
        "own worktree rather than the install"
    )
    assert path == (
        install_skills_root / pipeline.REVISION_HYGIENE_SKILL / "SKILL.md"
    )
    assert path.is_file(), f"the install ships no {path}"
