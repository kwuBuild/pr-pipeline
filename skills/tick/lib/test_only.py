"""Tests for the ordered ``--only`` dispatch allowlist.

The list bounds which items a tick may *launch*. HARVEST, stale-label
reconciliation, and admission occupancy stay global so a list-scoped run still
completes custody of engines it did not list. Run from the repository root::

    .venv/bin/python -m pytest skills/tick/lib/test_only.py -q
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from conftest import TRUSTED_AUTHOR  # type: ignore[import-untyped]


# --- CLI parsing ---


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--only", "#5,pr:7"], "#5,pr:7"),
        (["--only=#5,pr:7"], "#5,pr:7"),
        (["--loop", "--only", " #5 , pr:7 ,#5"], " #5 , pr:7 ,#5"),
    ],
)
def test_parse_cli_args_accepts_only_list(argv, expected) -> None:
    from tick import parse_cli_args  # type: ignore[import-untyped]

    parsed = parse_cli_args(argv)

    assert parsed["only"] == expected
    assert parsed["target"] is None


def test_parse_cli_args_only_defaults_to_empty() -> None:
    from tick import parse_cli_args  # type: ignore[import-untyped]

    assert parse_cli_args(["--loop"])["only"] == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["--only", "#5,"],
        ["--only", ",#5"],
        ["--only", "#5,,pr:7"],
        ["--only", " , "],
        ["--only="],
        ["--only", "5"],
        ["--only", "issue:5"],
        ["--only", "pr:x"],
        ["--only", "#0"],
        ["--only", "pr:-1"],
        ["--only", "#²"],
        ["--only", "# 5"],
        ["--only", "#5", "--only", "pr:7"],
        ["--only", "#5", "pr:7"],
        ["pr:7", "--only", "#5"],
        ["pr:7", "#5"],
        ["--loop", "#101", "pr:66"],
    ],
)
def test_parse_cli_args_rejects_invalid_scope_before_any_work(argv, capsys) -> None:
    from tick import parse_cli_args  # type: ignore[import-untyped]

    with pytest.raises(SystemExit) as exc:
        parse_cli_args(argv)

    assert exc.value.code == 1
    assert capsys.readouterr().err.strip()


def test_parse_only_list_keeps_first_occurrence_order() -> None:
    from tick import _parse_only_list  # type: ignore[import-untyped]

    assert _parse_only_list(" #5, pr:7 ,#5,pr:7,#3,#05") == (
        ("issue", 5), ("pr", 7), ("issue", 3),
    )


def test_main_rejects_only_with_positional_target_before_lock(tmp_path) -> None:
    from tick import main  # type: ignore[import-untyped]

    with mock.patch("tick.git", return_value=str(tmp_path)):
        with mock.patch("tick.TickLock") as lock:
            with mock.patch("tick._run_single_tick_locked") as run:
                assert main(target="pr:7", only="#5") == 1
                assert main(only="#5,") == 1

    lock.assert_not_called()
    run.assert_not_called()


def test_main_forwards_parsed_only_list_to_one_shot_and_loop(tmp_path) -> None:
    import tick  # type: ignore[import-untyped]

    with mock.patch("tick.git", return_value=str(tmp_path)):
        with mock.patch("tick._run_single_tick_locked",
                        return_value=tick.TickOutcome(0)) as run:
            assert tick.main(only="pr:7,#5,pr:7") == 0
        with mock.patch("tick._run_loop_locked", return_value=0) as loop:
            assert tick.main(only="#5", loop=True) == 0

    assert run.call_args.kwargs["only"] == (("pr", 7), ("issue", 5))
    assert loop.call_args.kwargs["only"] == (("issue", 5),)


# --- Scope resolution ---


def test_resolve_only_scope_expands_issues_once_in_list_order() -> None:
    import tick  # type: ignore[import-untyped]

    resolved = {
        ("issue", 5): {"reviewer": ("pr", 9), "implementer": ("issue", 5), "fixer": ("pr", 9)},
        ("pr", 7): {"reviewer": ("pr", 7), "implementer": ("pr", 7), "fixer": ("pr", 7)},
        ("pr", 9): {"reviewer": ("pr", 9), "implementer": ("pr", 9), "fixer": ("pr", 9)},
        ("issue", 3): {"reviewer": ("issue", 3), "implementer": ("issue", 3), "fixer": ("issue", 3)},
    }
    with mock.patch("tick._resolve_stage_targets", side_effect=resolved.__getitem__) as resolve:
        scope = tick._resolve_only_scope(
            (("pr", 7), ("issue", 5), ("pr", 9), ("issue", 3)),
        )

    assert [call.args[0] for call in resolve.call_args_list] == [
        ("pr", 7), ("issue", 5), ("pr", 9), ("issue", 3),
    ]
    assert scope.listed == (("pr", 7), ("issue", 5), ("pr", 9), ("issue", 3))
    assert scope.items == (("pr", 7), ("issue", 5), ("pr", 9), ("issue", 3))


def test_resolve_only_scope_places_closing_pr_after_its_issue() -> None:
    import tick  # type: ignore[import-untyped]

    resolved = {
        ("issue", 5): {"reviewer": ("pr", 9), "implementer": ("issue", 5), "fixer": ("pr", 9)},
        ("pr", 7): {"reviewer": ("pr", 7), "implementer": ("pr", 7), "fixer": ("pr", 7)},
    }
    with mock.patch("tick._resolve_stage_targets", side_effect=resolved.__getitem__):
        scope = tick._resolve_only_scope((("issue", 5), ("pr", 7)))

    assert scope.items == (("issue", 5), ("pr", 9), ("pr", 7))


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, ["gh"]), ValueError("ambiguous")],
)
def test_resolve_only_scope_propagates_read_failures(error) -> None:
    import tick  # type: ignore[import-untyped]

    with mock.patch("tick._resolve_stage_targets", side_effect=error):
        with pytest.raises(type(error)):
            tick._resolve_only_scope((("issue", 5),))


# --- Per-stage dispatch filtering ---


def test_dispatch_reviewers_follows_list_order_under_limited_capacity() -> None:
    import tick  # type: ignore[import-untyped]

    log = mock.Mock()
    admitted = iter([True, False])
    prs = {3: ["ai:implemented"], 9: ["ai:revised"]}
    with mock.patch("tick.verify_codex_bin"):
        with mock.patch("tick.gh_text", side_effect=no_queue_listing) as text:
            with mock.patch("tick._resolve_dispatchable_pickup_label",
                            return_value="ai:implemented") as eligible:
                with mock.patch("tick.can_dispatch_now",
                                side_effect=lambda **_: admission(next(admitted))):
                    with mock.patch("tick.gh_json", side_effect=pr_reads(prs)):
                        with mock.patch("tick._dispatch_one_reviewer") as launch:
                            tick.dispatch_reviewers(
                                tick_log=log, target=None, authoritative_in_flight=[],
                                only=(("pr", 9), ("issue", 1), ("pr", 3)),
                            )

    assert [call.args[1] for call in eligible.call_args_list] == [9, 3]
    assert [call.kwargs["pr_number"] for call in launch.call_args_list] == [9]
    text.assert_not_called()


def test_dispatch_fixers_follows_list_order_and_never_touches_unlisted() -> None:
    import tick  # type: ignore[import-untyped]

    prs = {4: ["ai:changes-requested"], 6: ["ai:changes-requested"]}
    with mock.patch("tick.gh_text", side_effect=no_queue_listing) as text:
        with mock.patch("tick.gh_json", side_effect=pr_reads(prs)) as reads:
            with mock.patch("tick._resolve_dispatchable_pickup_label",
                            return_value="ai:changes-requested") as eligible:
                with mock.patch("tick.can_dispatch_now", return_value=admission(True)):
                    with mock.patch("tick._dispatch_one_fixer") as launch:
                        tick.dispatch_fixers(
                            tick_log=None, target=None, authoritative_in_flight=[],
                            only=(("pr", 6), ("issue", 8), ("pr", 4)),
                        )

    assert [call.args[2] for call in reads.call_args_list] == ["6", "4"]
    assert [call.args[1] for call in eligible.call_args_list] == [6, 4]
    assert [call.kwargs["pr_number"] for call in launch.call_args_list] == [6, 4]
    text.assert_not_called()


@pytest.mark.parametrize(
    ("dispatcher", "label", "launcher"),
    [
        ("dispatch_reviewers", "ai:implemented", "_dispatch_one_reviewer"),
        ("dispatch_fixers", "ai:changes-requested", "_dispatch_one_fixer"),
    ],
)
def test_listed_pr_beyond_the_bounded_queue_page_is_dispatched(
    dispatcher, label, launcher,
) -> None:
    import tick  # type: ignore[import-untyped]

    # A full label page of unlisted PRs would hide PR 7 from a page filter.
    full_page = "\n".join(str(number) for number in range(100, 300))

    def gh_text(*args: str, **_kwargs: object) -> str:
        if "list" in args:
            return full_page
        return label

    with mock.patch("tick.verify_codex_bin"), \
         mock.patch("tick.gh_text", side_effect=gh_text) as text, \
         mock.patch("tick.gh_json", side_effect=pr_reads({7: [label]})), \
         mock.patch("tick._existing_marker_blocks_dispatch", return_value=False), \
         mock.patch("tick._provider_cooldown_blocks_dispatch", return_value=False), \
         mock.patch("tick.can_dispatch_now", return_value=admission(True)), \
         mock.patch(f"tick.{launcher}") as launch:
        getattr(tick, dispatcher)(
            tick_log=None, target=None, authoritative_in_flight=[], only=(("pr", 7),),
        )

    assert [call.kwargs["pr_number"] for call in launch.call_args_list] == [7]
    assert not [call for call in text.call_args_list if "list" in call.args]
    viewed = {call.args[2] for call in text.call_args_list if "view" in call.args}
    assert viewed == {"7"}, "an unlisted PR must receive no per-item read"


@pytest.mark.parametrize("dispatcher", ["dispatch_reviewers", "dispatch_fixers"])
def test_listed_pr_reads_skip_closed_unreadable_and_other_stage_prs(dispatcher) -> None:
    import tick  # type: ignore[import-untyped]

    payloads = {
        "3": {"number": 3, "state": "CLOSED", "labels": [{"name": "ai:implemented"}]},
        "4": {"number": 4, "state": "OPEN", "labels": [{"name": "ai:ready-for-human"}]},
        "6": {"number": 6, "state": "OPEN", "labels": "not-a-list"},
    }

    def gh_json(*args: str, **_kwargs: object) -> object:
        if args[2] == "5":
            raise subprocess.CalledProcessError(1, ["gh"])
        return payloads[args[2]]

    log = mock.Mock()
    with mock.patch("tick.verify_codex_bin"), \
         mock.patch("tick.gh_text", side_effect=no_queue_listing), \
         mock.patch("tick.gh_json", side_effect=gh_json), \
         mock.patch("tick._resolve_dispatchable_pickup_label") as eligible:
        getattr(tick, dispatcher)(
            tick_log=log, target=None, authoritative_in_flight=[],
            only=(("pr", 3), ("pr", 4), ("pr", 5), ("pr", 6)),
        )

    eligible.assert_not_called()
    skips = {call.kwargs["pr"]: call.kwargs["reason"] for call in log.log.call_args_list
             if call.kwargs.get("action") == "skip"}
    assert sorted(skips) == [3, 5, 6]
    assert all("retried next tick" in reason for reason in skips.values())


@pytest.mark.parametrize("dispatcher", ["dispatch_reviewers", "dispatch_fixers"])
@pytest.mark.parametrize(
    "labels",
    [
        [{"name": []}],
        [{"name": {}}],
        [{"name": None}],
        [{"name": "ai:implemented"}, "ai:changes-requested"],
    ],
    ids=["list-name", "object-name", "null-name", "non-object-entry"],
)
def test_listed_pr_with_malformed_label_entries_is_skipped_not_crashed(
    dispatcher, labels,
) -> None:
    import tick  # type: ignore[import-untyped]

    def gh_json(*args: str, **_kwargs: object) -> object:
        if args[2] == "8":
            return {"number": 8, "state": "OPEN", "labels": labels}
        return pr_reads({9: ["ai:implemented", "ai:changes-requested"]})(*args)

    log = mock.Mock()
    with mock.patch("tick.verify_codex_bin"), \
         mock.patch("tick.gh_text", side_effect=no_queue_listing), \
         mock.patch("tick.gh_json", side_effect=gh_json), \
         mock.patch("tick._resolve_dispatchable_pickup_label", return_value=None) as eligible:
        getattr(tick, dispatcher)(
            tick_log=log, target=None, authoritative_in_flight=[],
            only=(("pr", 8), ("pr", 9)),
        )

    # The malformed PR is skipped for this tick; the next listed PR still
    # reaches the eligibility gate.
    assert [call.args[1] for call in eligible.call_args_list] == [9]
    skips = [call.kwargs for call in log.log.call_args_list
             if call.kwargs.get("action") == "skip"]
    assert [skip["pr"] for skip in skips] == [8]
    assert "retried next tick" in skips[0]["reason"]


def test_dispatch_implementers_reads_listed_issues_directly_in_list_order() -> None:
    import tick  # type: ignore[import-untyped]

    def gh_json(*args, **_kwargs):
        assert args[:2] == ("issue", "view"), args
        return {"number": int(args[2]), "title": f"Issue {args[2]}",
                "author": {"login": TRUSTED_AUTHOR}, "state": "OPEN"}

    with mock.patch("tick.gh_json", side_effect=gh_json) as reads:
        with mock.patch("tick.gh_text", return_value="body"):
            with mock.patch("tick._resolve_dispatchable_pickup_label",
                            return_value="ai:available"):
                with mock.patch("tick.can_dispatch_now", return_value=admission(True)):
                    with mock.patch("tick._dispatch_one_implementer") as launch:
                        tick.dispatch_implementers(
                            tick_log=None, target=None, authoritative_in_flight=[],
                            only=(("issue", 12), ("pr", 3), ("issue", 4)),
                        )

    assert [call.args[2] for call in reads.call_args_list] == ["12", "4"]
    assert [call.kwargs["issue_number"] for call in launch.call_args_list] == [12, 4]


def test_dispatch_implementers_only_still_enforces_issue_trust_and_state() -> None:
    import tick  # type: ignore[import-untyped]

    payloads = {
        "12": {"number": 12, "title": "x", "author": {"login": "stranger"}, "state": "OPEN"},
        "4": {"number": 4, "title": "y", "author": {"login": TRUSTED_AUTHOR}, "state": "CLOSED"},
    }
    log = mock.Mock()
    with mock.patch("tick.gh_json", side_effect=lambda *a, **_k: payloads[a[2]]):
        with mock.patch("tick._resolve_dispatchable_pickup_label",
                        return_value="ai:available") as eligible:
            with mock.patch("tick._dispatch_one_implementer") as launch:
                tick.dispatch_implementers(
                    tick_log=log, target=None, authoritative_in_flight=[],
                    only=(("issue", 12), ("issue", 4)),
                )

    eligible.assert_not_called()
    launch.assert_not_called()
    skips = [call.kwargs["pr"] for call in log.log.call_args_list
             if call.kwargs.get("action") == "skip"]
    assert skips == [4]


# --- Tick body routing ---


def test_tick_body_harvests_globally_and_scopes_every_dispatch_stage() -> None:
    import pipeline
    import tick  # type: ignore[import-untyped]

    scope = tick.OnlyScope(
        listed=(("issue", 5), ("pr", 7)),
        items=(("issue", 5), ("pr", 9), ("pr", 7)),
    )
    order: list[str] = []
    stale_ref = pipeline.InFlightRef(kind="pr", number=44, label=pipeline.LABEL_REVIEWING)
    with mock.patch("tick._surface_retained_scratch_dirs"), \
         mock.patch("tick._resolve_stage_targets",
                    wraps=tick._resolve_stage_targets) as stage_targets, \
         mock.patch("tick._resolve_only_scope", return_value=scope) as resolve, \
         mock.patch("tick.harvest_reviewers", return_value=True) as hr, \
         mock.patch("tick.harvest_implementers", return_value=True) as hi, \
         mock.patch("tick.harvest_fixers", return_value=True) as hf, \
         mock.patch("tick.read_authoritative_in_flight_labels", return_value=[stale_ref]), \
         mock.patch("tick._reconcile_stale_in_flight_labels") as stale, \
         mock.patch("tick.dispatch_reviewers", side_effect=lambda **_: order.append("review")) as dr, \
         mock.patch("tick.dispatch_implementers", side_effect=lambda **_: order.append("implement")) as di, \
         mock.patch("tick.dispatch_fixers", side_effect=lambda **_: order.append("fix")) as df, \
         mock.patch("tick._observe_only_state",
                    return_value=pipeline.TargetCadenceState(True, False, "--only #5, pr:7")) as observe, \
         mock.patch("tick._compute_post_dispatch_state", return_value=(0, True, ())) as post:
        outcome = tick._run_tick_body(None, None, "claude", only=(("issue", 5), ("pr", 7)))

    assert outcome.exit_code == 0
    # Only the empty positional target is resolved; list entries are expanded
    # once, by _resolve_only_scope, and never again for cadence.
    assert [call.args for call in stage_targets.call_args_list] == [(None,)]
    resolve.assert_called_once_with((("issue", 5), ("pr", 7)))
    assert hr.call_args.args[1] is None
    assert hi.call_args.args[1] is None
    assert hf.call_args.args[1] is None
    assert stale.call_args.args[0] == [{"kind": "pr", "number": 44, "label": "ai:reviewing"}]
    assert order == ["review", "implement", "fix"]
    for dispatcher in (dr, di, df):
        assert dispatcher.call_args.kwargs["target"] is None
        assert dispatcher.call_args.kwargs["only"] == scope.items
    observe.assert_called_once_with(scope)
    assert post.call_args.kwargs["only_state"] is observe.return_value
    assert "actionable" in outcome.cadence.reason


def test_tick_body_list_scope_respects_the_installed_guidance_gate(
    tmp_path, monkeypatch, capsys,
) -> None:
    """An unreadable review/fix doc holds those stages on a list tick too.

    The implement stage, which binds no missing doc, still dispatches the
    resolved list scope. The tick reports once and exits 2 (PARTIAL).
    """
    import shutil

    import pipeline
    import tick  # type: ignore[import-untyped]

    install = tmp_path / "skills"
    shutil.copytree(pipeline.INSTALL_SKILLS_ROOT, install)
    doc = install / pipeline.REVISION_HYGIENE_SKILL / "SKILL.md"
    doc.unlink()
    monkeypatch.setattr(pipeline, "INSTALL_SKILLS_ROOT", install)
    scope = tick.OnlyScope(
        listed=(("issue", 5), ("pr", 7)),
        items=(("issue", 5), ("pr", 9), ("pr", 7)),
    )
    log = tick.TickLog.open(str(tmp_path / "tick-log"))
    with mock.patch("tick._surface_retained_scratch_dirs"), \
         mock.patch("tick._resolve_only_scope", return_value=scope), \
         mock.patch("tick.harvest_reviewers", return_value=True), \
         mock.patch("tick.harvest_implementers", return_value=True), \
         mock.patch("tick.harvest_fixers", return_value=True), \
         mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]), \
         mock.patch("tick.dispatch_reviewers") as dr, \
         mock.patch("tick.dispatch_implementers") as di, \
         mock.patch("tick.dispatch_fixers") as df, \
         mock.patch("tick._observe_only_state",
                    return_value=pipeline.TargetCadenceState(True, False, "--only #5, pr:7")), \
         mock.patch("tick._compute_post_dispatch_state", return_value=(0, True, ())):
        outcome = tick._run_tick_body(log, None, "claude", only=scope.listed)

    assert outcome.exit_code == 2
    dr.assert_not_called()
    df.assert_not_called()
    di.assert_called_once()
    assert di.call_args.kwargs["only"] == scope.items
    actions = [entry["action"] for entry in log.tail()]
    assert actions.index("only_scope_resolved") < actions.index("skill_unresolved")
    assert actions.count("skill_unresolved") == 1
    assert "Status: PARTIAL (dispatch blocked: installed guidance unreadable)" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, ["gh"]), ValueError("multiple open closing PRs")],
)
def test_tick_body_expansion_failure_harvests_but_starts_no_engine(error) -> None:
    import pipeline
    import tick  # type: ignore[import-untyped]

    log = mock.Mock()
    log.tail.return_value = []
    stale_ref = pipeline.InFlightRef(kind="issue", number=40, label=pipeline.LABEL_IMPLEMENTING)
    queue_counter = Path(tick._QUIESCENT_COUNTER_FILE)
    queue_counter.parent.mkdir(parents=True, exist_ok=True)
    queue_counter.write_text("2")
    list_counter = Path(tick._quiescent_counter_path(tick._only_counter_scope((("issue", 5),))))
    list_counter.write_text("2")
    with mock.patch("tick._surface_retained_scratch_dirs"), \
         mock.patch("tick._resolve_only_scope", side_effect=error), \
         mock.patch("tick.harvest_reviewers", return_value=True) as hr, \
         mock.patch("tick.harvest_implementers", return_value=True) as hi, \
         mock.patch("tick.harvest_fixers", return_value=True) as hf, \
         mock.patch("tick.read_authoritative_in_flight_labels", return_value=[stale_ref]), \
         mock.patch("tick._reconcile_stale_in_flight_labels") as stale, \
         mock.patch("tick.dispatch_reviewers") as dr, \
         mock.patch("tick.dispatch_implementers") as di, \
         mock.patch("tick.dispatch_fixers") as df, \
         mock.patch("tick._dispatch_one_reviewer") as one_review, \
         mock.patch("tick._dispatch_one_implementer") as one_impl, \
         mock.patch("tick._dispatch_one_fixer") as one_fix:
        outcome = tick._run_tick_body(log, None, "claude", only=(("issue", 5),))

    assert outcome.exit_code == 2
    assert outcome.cadence is not None and not outcome.cadence.stop
    assert "state unknown" in outcome.cadence.reason
    assert outcome.cadence.reason.startswith("--only #5: not observed;")
    for harvest in (hr, hi, hf):
        assert harvest.call_args.args[1] is None
    stale.assert_called_once()
    for launch in (dr, di, df, one_review, one_impl, one_fix):
        launch.assert_not_called()
    failure = [call.kwargs for call in log.log.call_args_list
               if call.kwargs.get("action") == "only_scope_unreadable"]
    assert len(failure) == 1
    assert str(error) in failure[0]["reason"] or "CalledProcessError" in failure[0]["reason"]
    assert failure[0]["details"]["only"] == ["#5"]
    assert list_counter.read_text() == "0"
    assert queue_counter.read_text() == "2"


def test_tick_body_reconcile_failure_resets_only_the_list_counter() -> None:
    import tick  # type: ignore[import-untyped]

    queue_counter = Path(tick._QUIESCENT_COUNTER_FILE)
    queue_counter.parent.mkdir(parents=True, exist_ok=True)
    queue_counter.write_text("2")
    list_counter = Path(tick._quiescent_counter_path(tick._only_counter_scope((("pr", 7),))))
    list_counter.write_text("2")
    with mock.patch("tick._surface_retained_scratch_dirs"), \
         mock.patch("tick._resolve_only_scope") as resolve, \
         mock.patch("tick.harvest_reviewers", return_value=False), \
         mock.patch("tick.harvest_implementers", return_value=True), \
         mock.patch("tick.harvest_fixers", return_value=True), \
         mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]), \
         mock.patch("tick.dispatch_reviewers") as dr:
        outcome = tick._run_tick_body(None, None, "claude", only=(("pr", 7),))

    assert outcome.exit_code == 1
    assert outcome.cadence.reason.startswith("--only pr:7: not observed;")
    resolve.assert_not_called()
    dr.assert_not_called()
    assert list_counter.read_text() == "0"
    assert queue_counter.read_text() == "2"


# --- List cadence ---


def test_observe_only_state_is_tri_state_over_the_resolved_items() -> None:
    import tick  # type: ignore[import-untyped]

    scope = tick.OnlyScope(listed=(("issue", 5),), items=(("issue", 5), ("pr", 9)))
    payloads = {
        ("issue", "5"): {"state": "OPEN", "labels": [{"name": "ai:implementing"}],
                         "author": {"login": TRUSTED_AUTHOR}},
        ("pr", "9"): {"state": "OPEN", "labels": [{"name": "ai:implemented"}]},
    }
    with mock.patch("tick.gh_json", side_effect=lambda *a, **_k: payloads[(a[0], a[2])]) as reads, \
         mock.patch("tick._resolve_stage_targets") as resolve:
        busy = tick._observe_only_state(scope)
        payloads[("pr", "9")]["labels"] = [{"name": "ai:ready-for-human"}]
        drained = tick._observe_only_state(scope)
        reads.side_effect = subprocess.CalledProcessError(1, ["gh"])
        unknown = tick._observe_only_state(scope)

    resolve.assert_not_called()
    assert busy.actionable is True and not busy.terminal
    assert drained.actionable is False
    assert "pr #9 ai:ready-for-human" in drained.description
    assert unknown.actionable is None


def test_observe_only_state_unknown_author_stays_warm() -> None:
    import tick  # type: ignore[import-untyped]

    scope = tick.OnlyScope(listed=(("issue", 5),), items=(("issue", 5),))
    with mock.patch("tick.gh_json", return_value={
        "state": "OPEN", "labels": [{"name": "ai:available"}], "author": None,
    }):
        assert tick._observe_only_state(scope).actionable is None


def test_list_cadence_stops_only_after_drain_with_no_local_engine() -> None:
    import pipeline

    common = {"quiescent_stop_after": 3, "scope_description": "--only #5, pr:7"}
    busy = pipeline.compute_next_wake(
        in_flight_count=1, nearest_deadline_s=100, has_actionable_queue=False,
        at_capacity=False, is_quiescent=False, consecutive_quiescent_ticks=5, **common,
    )
    unknown = pipeline.compute_next_wake(
        in_flight_count=0, has_actionable_queue=None, at_capacity=False, **common,
    )
    waiting = pipeline.compute_next_wake(
        in_flight_count=0, has_actionable_queue=False, at_capacity=False,
        is_quiescent=True, consecutive_quiescent_ticks=1, **common,
    )
    stop = pipeline.compute_next_wake(
        in_flight_count=0, has_actionable_queue=False, at_capacity=False,
        is_quiescent=True, consecutive_quiescent_ticks=2, **common,
    )

    assert not busy.stop and busy.reason.startswith("--only #5, pr:7")
    assert not unknown.stop and "state unknown" in unknown.reason
    assert not waiting.stop and "heartbeat 2/3" in waiting.reason
    assert stop.stop
    assert "--only list exhausted" in stop.reason and "heartbeat 3/3" in stop.reason


def test_list_counter_is_separate_from_queue_and_target_counters() -> None:
    import tick  # type: ignore[import-untyped]

    key = tick._only_counter_scope((("issue", 5), ("pr", 7)))
    same_set = tick._only_counter_scope((("pr", 7), ("issue", 5)))
    path = tick._quiescent_counter_path(key)

    assert key == same_set
    assert path != tick._QUIESCENT_COUNTER_FILE
    assert path != tick._quiescent_counter_path(("issue", 5))
    assert Path(path).name.startswith("only-")


def test_loop_reports_list_exhaustion_through_its_own_stop_event() -> None:
    import pipeline
    import tick  # type: ignore[import-untyped]

    decision = pipeline.CadenceDecision(0, True, "--only list exhausted (#5) → stop")
    with mock.patch("tick._run_single_tick_locked",
                    return_value=tick.TickOutcome(0, decision, ("pr #3",))) as run, \
         mock.patch("tick._start_wake_watcher", return_value=None), \
         mock.patch("tick._notify_loop_only_stop") as only_stop, \
         mock.patch("tick._notify_loop_quiescent_stop") as queue_stop, \
         mock.patch("tick._notify_loop_target_stop") as target_stop:
        assert tick._run_loop_locked(None, "claude", "", only=(("issue", 5),)) == 0

    assert run.call_args.kwargs["only"] == (("issue", 5),)
    only_stop.assert_called_once_with(decision.reason)
    queue_stop.assert_not_called()
    target_stop.assert_not_called()


def test_notify_loop_only_stop_logs_a_distinct_event() -> None:
    import tick  # type: ignore[import-untyped]

    tick._notify_loop_only_stop("--only list exhausted (#5) → stop")

    entries = [
        json.loads(line)
        for log in Path(tick.TICK_LOG_DIR).glob("tick-*.jsonl")
        for line in log.read_text().splitlines()
    ]
    assert [entry["action"] for entry in entries] == ["loop_only_stop"]
    assert entries[0]["reason"] == "--only list exhausted (#5) → stop"


# --- Dashboard scope banner ---


def test_scope_report_shows_active_list_scope_from_the_newest_tick(tmp_path) -> None:
    import pipeline

    write_tick_log(tmp_path, "tick-0001.jsonl", [
        {"ts": 1000, "action": "tick_complete", "details": {}},
    ])
    write_tick_log(tmp_path, "tick-0002.jsonl", [
        {"ts": 2000, "action": "only_scope_resolved",
         "details": {"only": ["#5", "pr:7"], "scope": ["#5", "pr:9", "pr:7"]}},
        {"ts": 2010, "action": "tick_complete",
         "details": {"cadence": {"next_wake_seconds": 270, "stop": False, "reason": "x"}}},
    ])
    write_tick_log(tmp_path, "tick-0003.jsonl", [
        {"ts": 2050, "action": "lock_held", "details": {}},
    ])

    report = pipeline.dispatch_scope_report(str(tmp_path), now=2100)

    assert report.startswith("--only #5,pr:7 (dispatch scope #5,pr:9,pr:7)")
    assert "log evidence active" in report
    assert clock(2010) in report and clock(2280) in report


def test_scope_report_marks_overdue_evidence_stale(tmp_path) -> None:
    import pipeline

    write_tick_log(tmp_path, "tick-0001.jsonl", [
        {"ts": 2000, "action": "only_scope_resolved",
         "details": {"only": ["#5"], "scope": ["#5"]}},
        {"ts": 2000, "action": "tick_complete",
         "details": {"cadence": {"next_wake_seconds": 270, "stop": False, "reason": "x"}}},
    ])

    report = pipeline.dispatch_scope_report(str(tmp_path), now=2000 + 270 + 121)

    assert report.startswith("--only #5 ")
    assert "log evidence STALE" in report
    assert "no newer tick" in report


def test_scope_report_names_unreadable_list_and_ended_runs(tmp_path) -> None:
    import pipeline

    write_tick_log(tmp_path, "tick-0001.jsonl", [
        {"ts": 2000, "action": "only_scope_unreadable", "reason": "GitHub read failed",
         "details": {"only": ["#5", "pr:7"]}},
        {"ts": 2001, "action": "tick_complete",
         "details": {"cadence": {"next_wake_seconds": 270, "stop": False, "reason": "x"}}},
    ])
    unreadable = pipeline.dispatch_scope_report(str(tmp_path), now=2002)
    write_tick_log(tmp_path, "tick-0002.jsonl", [
        {"ts": 2003, "action": "loop_only_stop", "reason": "--only list exhausted"},
    ])
    ended = pipeline.dispatch_scope_report(str(tmp_path), now=2004)

    assert unreadable.startswith("--only #5,pr:7 (scope UNREADABLE at last tick; no engine started)")
    assert "log evidence active" in unreadable
    assert "log evidence ended" in ended and "--only list exhausted" in ended


@pytest.mark.parametrize(
    ("entries", "scope"),
    [
        ([{"ts": 2000, "action": "tick_scope_requested", "details": {"mode": "full"}},
          {"ts": 2000, "action": "tick_complete", "details": {}}], "full queue"),
        ([{"ts": 2000, "action": "tick_complete", "details": {}}],
         "scope unknown (no scope record)"),
        ([{"ts": 2000, "action": "tick_scope_requested",
           "details": {"mode": "only", "only": ["#5", "pr:7"]}},
          {"ts": 2000, "action": "tick_complete", "details": {}}],
         "--only #5,pr:7 (requested; scope not resolved; no engine started)"),
        ([{"ts": 2000, "action": "tick_scope_requested",
           "details": {"mode": "target", "target": "#5"}},
          {"ts": 2000, "action": "tick_complete", "details": {}}],
         "target #5 (requested; scope not resolved; no engine started)"),
        ([{"ts": 2000, "action": "tick_scope_requested", "details": {"mode": "other"}},
          {"ts": 2000, "action": "tick_complete", "details": {}}],
         "scope record malformed"),
        ([{"ts": 2000, "action": "tick_scope_requested",
           "details": {"mode": "only", "only": ["#5"]}},
          {"ts": 2000, "action": "only_scope_resolved",
           "details": {"only": ["#5"], "scope": ["#5", "pr:9"]}},
          {"ts": 2000, "action": "tick_complete", "details": {}}],
         "--only #5 (dispatch scope #5,pr:9)"),
        ([{"ts": 2000, "action": "target_scope_resolved",
           "details": {"target": "#5", "scope": ["#5", "pr:9"]}},
          {"ts": 2000, "action": "tick_complete", "details": {}}],
         "target #5 (scope #5,pr:9)"),
    ],
)
def test_scope_report_names_queue_and_target_scopes(tmp_path, entries, scope) -> None:
    import pipeline

    write_tick_log(tmp_path, "tick-0001.jsonl", entries)

    report = pipeline.dispatch_scope_report(str(tmp_path), now=2001)

    assert report.startswith(scope)
    assert "no cadence" in report


def test_scope_report_tolerates_corrupt_lines_and_malformed_scope(tmp_path) -> None:
    import pipeline

    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "tick-0001.jsonl").write_text(
        "{not json\n"
        + json.dumps({"ts": 2000, "action": "only_scope_resolved",
                      "details": {"only": "#5", "scope": [5]}}) + "\n"
        + json.dumps({"ts": "late", "action": "tick_complete",
                      "details": {"cadence": "bad"}}) + "\n"
    )

    report = pipeline.dispatch_scope_report(str(tmp_path), now=2001)

    assert report == "--only (scope record malformed) · log evidence STALE: last tick reported no cadence"


def test_scope_report_without_tick_evidence(tmp_path) -> None:
    import pipeline

    assert pipeline.dispatch_scope_report(str(tmp_path / "missing"), now=1) == (
        "no tick evidence"
    )


def test_tick_body_logs_positional_target_scope() -> None:
    import tick  # type: ignore[import-untyped]

    log = mock.Mock()
    log.tail.return_value = []
    targets = {"reviewer": ("pr", 9), "implementer": ("issue", 5), "fixer": ("pr", 9)}
    with mock.patch("tick._surface_retained_scratch_dirs"), \
         mock.patch("tick._resolve_stage_targets", return_value=targets), \
         mock.patch("tick.harvest_reviewers", return_value=False), \
         mock.patch("tick.harvest_implementers", return_value=True), \
         mock.patch("tick.harvest_fixers", return_value=True), \
         mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        tick._run_tick_body(log, ("issue", 5), "claude")

    entries = [call.kwargs for call in log.log.call_args_list
               if call.kwargs.get("action") == "target_scope_resolved"]
    assert entries and entries[0]["details"] == {"target": "#5", "scope": ["#5", "pr:9"]}



@pytest.mark.parametrize(
    ("target", "only", "scope"),
    [
        (None, (("pr", 7),), "--only pr:7 (requested; scope not resolved; no engine started)"),
        (("issue", 5), None, "target #5 (requested; scope not resolved; no engine started)"),
        (None, None, "full queue"),
    ],
)
def test_startup_failure_banner_names_the_requested_scope(target, only, scope) -> None:
    import time

    import pipeline
    import tick  # type: ignore[import-untyped]

    with mock.patch("tick._startup_probe", return_value={}), \
         mock.patch("tick.resolve_target", side_effect=ValueError("no remote")):
        outcome = tick._run_single_tick_locked(target, "", "", only=only)

    assert outcome.exit_code == 3
    entries = pipeline.read_latest_tick_log(tick.TICK_LOG_DIR)
    actions = [entry["action"] for entry in entries]
    assert actions[0] == "startup_context"
    assert actions.index("tick_scope_requested") < actions.index("startup_probe")
    assert actions.index("tick_scope_requested") < actions.index("startup_target_invalid")
    report = pipeline.dispatch_scope_report(tick.TICK_LOG_DIR, now=time.time())
    assert report.startswith(scope + " · log evidence STALE: last tick reported no cadence")


def test_harvest_failure_banner_names_the_requested_list_not_full_queue() -> None:
    import time

    import pipeline
    import tick  # type: ignore[import-untyped]

    Path(tick.TICK_LOG_DIR).mkdir(parents=True, exist_ok=True)
    log = tick.TickLog.open(tick.TICK_LOG_DIR)
    tick._log_requested_scope(log, None, (("pr", 7),))
    with mock.patch("tick._surface_retained_scratch_dirs"), \
         mock.patch("tick.harvest_reviewers", return_value=False), \
         mock.patch("tick.harvest_implementers", return_value=True), \
         mock.patch("tick.harvest_fixers", return_value=True), \
         mock.patch("tick.read_authoritative_in_flight_labels", return_value=[]):
        outcome = tick._run_tick_body(log, None, "claude", only=(("pr", 7),))

    assert outcome.exit_code == 1
    report = pipeline.dispatch_scope_report(tick.TICK_LOG_DIR, now=time.time())
    assert report.startswith(
        "--only pr:7 (requested; scope not resolved; no engine started) · log evidence active"
    )


# --- Helpers ---


def queue_text(queue: str, labels: str):
    def side_effect(*args: str, **_kwargs: object) -> str:
        if ".labels[].name" in args:
            return labels
        if "list" in args:
            return queue
        return ""

    return side_effect


def admission(allowed: bool):
    import pipeline

    return pipeline.AdmissionResult(
        can_dispatch=allowed, reason="" if allowed else "max_in_flight",
        in_flight_count=0 if allowed else 3, max_in_flight=3,
    )


def trusted_pr() -> dict[str, object]:
    return {
        "author": {"login": TRUSTED_AUTHOR},
        "headRepository": {"nameWithOwner": f"{TRUSTED_AUTHOR}/test-repo"},
        "isCrossRepository": False,
    }


def no_queue_listing(*args: str, **_kwargs: object) -> str:
    raise AssertionError(f"a list tick must not read a label queue: {args}")


def pr_reads(labels_by_pr: dict[int, list[str]]):
    """Answer listed-PR reads and the reviewer's trust read for open PRs."""

    def gh_json(*args: str, **_kwargs: object) -> object:
        assert args[:2] == ("pr", "view"), args
        number = int(args[2])
        if "author,headRepository,isCrossRepository" in args:
            return trusted_pr()
        return {"number": number, "state": "OPEN",
                "labels": [{"name": name} for name in labels_by_pr[number]]}

    return gh_json


def write_tick_log(directory: Path, name: str, entries: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def clock(ts: float) -> str:
    import time

    return time.strftime("%H:%M:%S", time.localtime(ts))
