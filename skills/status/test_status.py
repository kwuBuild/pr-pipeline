from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

TICK_LIB = Path(__file__).resolve().parents[1] / "tick" / "lib"


@pytest.fixture(autouse=True)
def isolate_target_override(monkeypatch):
    monkeypatch.delenv("PIPELINE_REPO", raising=False)


@pytest.fixture
def tick_module() -> Iterator[ModuleType]:
    """Import the sibling tick skill's dispatcher, the marker's real writer.

    The status dashboard reads markers that ``tick.py`` writes, so marker
    fixtures come from the production writer rather than being hand-typed
    here — a hand-typed marker cannot drift-detect a change in what the
    dispatcher actually emits.
    """
    sys.path.insert(0, str(TICK_LIB))
    try:
        import tick  # type: ignore[import-untyped]

        yield tick
    finally:
        sys.path.remove(str(TICK_LIB))


@pytest.fixture
def live_engine() -> Iterator[tuple[int, float]]:
    """A real process standing in for a dispatched engine, with its start time.

    The progress column is now identity-verified: a marker only renders
    ``STALLED`` if its PID belongs to a process that really started when the
    marker says. The previous fixture recorded this test process's PID against
    a fabricated start time 30 minutes earlier — a marker the real classifier
    rejects, so it could assert a verdict the tick would never emit.

    Yields:
        The live child's ``(pid, start_time)`` as the OS reports them.
    """
    sys.path.insert(0, str(TICK_LIB))
    try:
        import process_control  # type: ignore[import-untyped]
    finally:
        sys.path.remove(str(TICK_LIB))

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
    )
    try:
        start_time = process_control.get_process_start_time(proc.pid)
        assert start_time is not None, "cannot read the child's start time"
        yield proc.pid, start_time
    finally:
        proc.kill()
        proc.wait()


def test_status_marks_result_json_worktrees_ready(tmp_path: Path) -> None:
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/issue-487"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / "dispatch.json").write_text(
        '{"pr_number":487,"stage":"implement","engine":"claude","pid":999999,"deadline":9999999999}',
        encoding="utf-8",
    )
    (worktree_dir / "result.json").write_text(
        '{"result":"IMPLEMENTATION_COMPLETE"}',
        encoding="utf-8",
    )

    result = run_status(repo)

    assert result.returncode == 0
    assert "result-ready" in in_flight_row(result.stdout, pr_number=487)


def test_status_marks_review_json_worktrees_ready(tmp_path: Path) -> None:
    """The reviewer stage's result file drives the same column."""
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/pr-487"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / "dispatch.json").write_text(
        '{"pr_number":487,"stage":"review","engine":"codex","pid":999999,"deadline":9999999999}',
        encoding="utf-8",
    )
    (worktree_dir / "review.json").write_text(
        '{"verdict":"ready_for_human"}',
        encoding="utf-8",
    )

    result = run_status(repo)

    assert result.returncode == 0
    assert "result-ready" in in_flight_row(result.stdout, pr_number=487)


def test_status_renders_integer_ttl_for_dispatched_marker(
    tmp_path: Path, tick_module: ModuleType
) -> None:
    """A real dispatch marker's float deadline renders as whole minutes."""
    repo = install_status_script(tmp_path)
    marker = dispatch_real_implementer_marker(
        tick_module, repo=repo, issue_number=583
    )
    # The bug's premise: the production writer emits a float deadline.
    assert isinstance(json.loads(marker.read_text(encoding="utf-8"))["deadline"], float)

    result = run_status(repo)

    assert result.stderr == ""
    assert result.returncode == 0
    ttl = ttl_minutes(result.stdout, pr_number=583)
    full_minutes = tick_module.IMPLEMENTER_TIMEOUT // 60
    # The dashboard reads the clock after dispatch, so the last minute may
    # already have partially elapsed.
    assert ttl in (full_minutes, full_minutes - 1)


def test_status_renders_row_for_marker_with_non_numeric_deadline(
    tmp_path: Path,
) -> None:
    """A corrupt deadline still yields a row, and still no stderr noise."""
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/issue-584"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / "dispatch.json").write_text(
        '{"pr_number":584,"stage":"implement","engine":"claude","pid":999999,'
        '"deadline":"whenever"}',
        encoding="utf-8",
    )

    result = run_status(repo)

    assert result.stderr == ""
    assert result.returncode == 0
    assert "PR#584" in result.stdout


def test_status_reports_working_while_the_engine_result_is_still_empty(
    tmp_path: Path, tick_module: ModuleType
) -> None:
    """A stdout-redirect result file is empty until the engine writes to it."""
    repo = install_status_script(tmp_path)
    marker = dispatch_real_implementer_marker(
        tick_module, repo=repo, issue_number=589
    )
    # The bug's premise: dispatch creates result.json as the engine's stdout
    # target, so it exists at zero bytes for the whole run.
    result_file = marker.parent / "result.json"
    assert result_file.stat().st_size == 0

    result = run_status(repo)

    assert result.returncode == 0
    assert "working" in in_flight_row(result.stdout, pr_number=589)


def test_status_flags_a_live_engine_whose_heartbeat_went_quiet(
    tmp_path: Path,
    live_engine: tuple[int, float],
) -> None:
    """The row an operator scans must say a working-looking engine is wedged.

    Every other column on this row reads healthy — the PID is alive, the
    deadline is far off, the result is pending — which is exactly the state a
    stalled engine presents. Without this column the dashboard shows nothing
    wrong until the deadline fires an hour later.
    """
    pid, start_time = live_engine
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/issue-577"
    worktree_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds()
    write_marker_with_heartbeat(
        worktree_dir, quiet_seconds=quiet, pid=pid, start_time=start_time,
    )

    result = run_status(repo)

    assert result.stderr == ""
    row = in_flight_row(result.stdout, pr_number=577)
    assert "alive" in row
    assert "STALLED" in row
    assert f"{int(quiet // 60)}m" in row


def test_status_reports_a_working_engines_heartbeat_age_without_alarm(
    tmp_path: Path,
    live_engine: tuple[int, float],
) -> None:
    """A recently-beating engine shows its quiet time, not a stall verdict."""
    pid, start_time = live_engine
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/issue-577"
    worktree_dir.mkdir(parents=True)
    write_marker_with_heartbeat(
        worktree_dir, quiet_seconds=60, pid=pid, start_time=start_time,
    )

    result = run_status(repo)

    assert result.stderr == ""
    row = in_flight_row(result.stdout, pr_number=577)
    assert "STALLED" not in row
    assert "quiet" in row


def test_status_withholds_the_stall_verdict_from_a_reused_pid(
    tmp_path: Path,
    live_engine: tuple[int, float],
) -> None:
    """A PID passing ``kill -0`` is not proof the dispatched engine is alive.

    The watcher rejects this marker outright — the recorded start time belongs
    to a process that is gone, and the number was inherited by another. A
    dashboard trusting bare liveness would render a permanent STALLED that the
    tick log never confirms, which is worse than showing nothing.
    """
    pid, _real_start = live_engine
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/issue-577"
    worktree_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds()
    write_marker_with_heartbeat(
        worktree_dir,
        quiet_seconds=quiet,
        pid=pid,
        start_time=time.time() - 60 * 60,
    )

    result = run_status(repo)

    assert result.stderr == ""
    row = in_flight_row(result.stdout, pr_number=577)
    assert "alive" in row
    assert "STALLED" not in row
    assert f"quiet-{int(quiet // 60)}m" in row


def test_status_withholds_the_stall_verdict_past_the_deadline(
    tmp_path: Path,
    live_engine: tuple[int, float],
) -> None:
    """An overdue engine classifies ``hung``, which already escalates.

    The watcher declines to report it for that reason, so a dashboard calling
    it stalled double-reports the one case the pipeline already handles.
    """
    pid, start_time = live_engine
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/issue-577"
    worktree_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds()
    write_marker_with_heartbeat(
        worktree_dir,
        quiet_seconds=quiet,
        pid=pid,
        start_time=start_time,
        time_to_deadline=-10 * 60,
    )

    result = run_status(repo)

    assert result.stderr == ""
    row = in_flight_row(result.stdout, pr_number=577)
    assert "STALLED" not in row
    assert f"quiet-{int(quiet // 60)}m" in row


def test_status_applies_the_startup_grace_to_a_missing_heartbeat(
    tmp_path: Path,
    live_engine: tuple[int, float],
) -> None:
    """No heartbeat file is not a stall: an engine may be pre-first-event."""
    pid, start_time = live_engine
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/issue-577"
    worktree_dir.mkdir(parents=True)
    write_marker_with_heartbeat(
        worktree_dir, quiet_seconds=None, pid=pid, start_time=start_time,
    )

    result = run_status(repo)

    assert result.stderr == ""
    row = in_flight_row(result.stdout, pr_number=577)
    assert "STALLED" not in row
    assert "quiet-0m" in row


@pytest.mark.parametrize("with_heartbeat", [False, True])
def test_status_reports_reviewer_output_recency(
    tmp_path: Path,
    live_engine: tuple[int, float],
    with_heartbeat: bool,
) -> None:
    """The dashboard uses the shared review-stage progress and stall verdict."""
    pid, start_time = live_engine
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/pr-577"
    worktree_dir.mkdir(parents=True)
    quiet = stalled_quiet_seconds()
    write_marker_with_heartbeat(
        worktree_dir,
        quiet_seconds=quiet if with_heartbeat else None,
        pid=pid,
        start_time=start_time,
        stage="review",
    )

    result = run_status(repo)

    assert result.stderr == ""
    expected = f"STALLED-{int(quiet // 60)}m" if with_heartbeat else "quiet-0m"
    assert progress_cell(result.stdout, pr_number=577) == expected


def test_status_marks_the_progress_column_unknown_when_the_report_fails(
    tmp_path: Path,
    live_engine: tuple[int, float],
) -> None:
    """A dashboard that cannot reach the classifier must say so, not guess.

    Rendering ``-`` would claim the engine has no progress contract, and
    rendering a stale verdict would claim one the tick never gave. Neither the
    failure nor the fallback may write to stderr, which a `watch` loop repaints
    over the dashboard every few seconds.
    """
    pid, start_time = live_engine
    repo = install_status_script(tmp_path)
    worktree_dir = repo / ".claude/worktrees/issue-577"
    worktree_dir.mkdir(parents=True)
    write_marker_with_heartbeat(
        worktree_dir,
        quiet_seconds=stalled_quiet_seconds(),
        pid=pid,
        start_time=start_time,
    )
    # The lib the dashboard shells out to is gone: an upgrade mid-render, a
    # partially-checked-out repo, or a lift-out that took only this skill.
    (repo / ".claude/skills/tick/lib/tick.py").unlink()

    result = run_status(repo)

    assert result.stderr == ""
    assert result.returncode == 0
    assert progress_cell(result.stdout, pr_number=577) == "?"


def test_status_reports_empty_section_when_no_markers_exist(tmp_path: Path) -> None:
    """An idle pipeline must say so rather than print an empty section."""
    repo = install_status_script(tmp_path)
    (repo / ".claude/worktrees").mkdir(parents=True)

    result = run_status(repo)

    assert result.returncode == 0
    assert "(none — no engines dispatched)" in result.stdout


def test_status_reads_the_main_checkout_marker_space_from_a_worktree(
    tmp_path: Path,
) -> None:
    """Run from a linked worktree, the dashboard still sees the real markers.

    ``git rev-parse --show-toplevel`` names the linked worktree, whose
    ``.claude/worktrees`` is empty — the dashboard would report an idle
    pipeline while engines are spending.
    """
    repo = install_status_script(tmp_path)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "root"],
        cwd=repo, check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )
    worktree_dir = repo / ".claude/worktrees/issue-592"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / "dispatch.json").write_text(
        '{"pr_number":592,"stage":"implement","engine":"claude","pid":999999,'
        '"deadline":9999999999}',
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt", str(linked)],
        cwd=repo, check=True, capture_output=True,
    )

    result = run_status(repo, cwd=linked)

    assert result.returncode == 0
    assert "PR#592" in result.stdout


def test_status_lists_open_issues_at_every_pipeline_label(tmp_path: Path) -> None:
    """Issues are grouped by ai: label, not filtered to ai:available.

    The dashboard is the only place a markerless in-flight item becomes
    visible now that reconciliation never escalates one (issue #592). An issue
    stuck at ai:implementing is exactly what an operator needs to see, and an
    ai:available-only listing hides it.
    """
    repo = install_status_script(tmp_path)
    issues = (
        '[{"number":11,"title":"stuck in flight",'
        '"labels":[{"name":"ai:implementing"}]},'
        '{"number":12,"title":"under review",'
        '"labels":[{"name":"ai:reviewing"}]},'
        '{"number":13,"title":"waiting","labels":[{"name":"ai:available"}]}]'
    )
    result = run_status(repo, pages=[json.loads(issues)])

    assert result.returncode == 0
    assert re.search(r"ai:implementing\s+#11", result.stdout), result.stdout
    assert re.search(r"ai:reviewing\s+#12", result.stdout), result.stdout
    assert re.search(r"ai:available\s+#13", result.stdout), result.stdout


def test_status_resolves_foreign_remote_without_config(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    monkeypatch.setenv("GH_HOST", "other.example")
    repo = install_status_script(tmp_path, target_repo="elsewhere/project")
    result = run_status(repo)
    assert result.returncode == 0, result.stderr
    assert "elsewhere/project" in result.stdout
    calls = (repo.parent / "gh-calls").read_text().splitlines()
    assert len(calls) == 1
    assert "--hostname github.com" in calls[0]
    assert "repos/elsewhere/project/issues?" in calls[0]


def test_status_rejects_mismatched_override_before_interpreter_or_github(tmp_path, monkeypatch):
    repo = install_status_script(tmp_path, target_repo="elsewhere/project")
    monkeypatch.setenv("PIPELINE_REPO", "wrong/project")
    # Reaching either executable makes the ordering failure unambiguous.
    resolver = repo / ".claude/skills/tick/resolve-python.sh"
    resolver.write_text('echo INTERPRETER_CALLED >&2\nexit 42\n')
    result = run_status(repo)
    assert result.returncode == 2
    assert "upstream" in result.stderr
    assert "INTERPRETER_CALLED" not in result.stderr
    assert not (repo.parent / "gh-calls").exists()


def test_status_identifies_parked_issues_and_prs_across_all_pages(tmp_path):
    repo = install_status_script(tmp_path)
    pages = [
        [status_item(i, "unlabelled", []) for i in range(1, 101)],
        [status_item(101, "parked issue", ["bug", "ai:parked"]),
         status_item(102, "parked PR", ["ai:legacy", "ai:pause"], pr=True),
         status_item(103, "still queued", ["ai:available", "ai:legacy"]),
         status_item(104, "known hold", ["ai:blocked"], pr=True)],
    ]

    result = run_status(repo, pages=pages)

    assert result.returncode == 0, result.stderr
    assert "PARKED ITEMS" in result.stdout
    parked = result.stdout.split("PARKED ITEMS", 1)[1].split("LAST TICK", 1)[0]
    assert re.search(r"issue\s+#101\s+ai:parked", parked), parked
    assert re.search(r"PR\s+#102\s+ai:legacy, ai:pause", parked), parked
    assert "#103" not in parked and "#104" not in parked
    assert "no dispatch query selects" in parked
    calls = (repo.parent / "gh-calls").read_text().splitlines()
    assert len(calls) == 1
    assert "--paginate" in calls[0] and "--slurp" in calls[0]
    assert "state=open" in calls[0]


@pytest.mark.parametrize("failure", ["api", "malformed"])
def test_status_reports_unavailable_items_instead_of_false_empty(tmp_path, failure):
    repo = install_status_script(tmp_path)

    result = run_status(repo, pages={"message": "bad shape"}, failure=failure)

    assert "PARKED ITEMS" in result.stdout
    parked = result.stdout.split("PARKED ITEMS", 1)[1].split("LAST TICK", 1)[0]
    assert "unavailable" in parked
    assert "(none)" not in parked
    assert "GitHub" in result.stderr


def test_status_keeps_raw_labels_when_python_cannot_classify_parked_items(tmp_path):
    repo = install_status_script(tmp_path)
    resolver = repo / ".claude/skills/tick/resolve-python.sh"
    resolver.write_text(
        "pr_pipeline_resolve_python() { return 1; }\n"
        "pr_pipeline_python_requirement_error() { echo 'Python unavailable' >&2; }\n"
    )

    result = run_status(repo, pages=[[status_item(101, "parked issue", ["ai:parked"])]])

    assert re.search(r"ai:parked\s+#101", result.stdout), result.stdout
    assert "PARKED ITEMS" in result.stdout
    parked = result.stdout.split("PARKED ITEMS", 1)[1].split("LAST TICK", 1)[0]
    assert "unavailable" in parked and "Python" in parked


def test_status_reports_unavailable_when_vocabulary_output_is_invalid(tmp_path):
    repo = install_status_script(tmp_path)
    (repo / ".claude/skills/tick/lib/labels.py").write_text('print("invalid json")\n')

    result = run_status(repo, pages=[[status_item(101, "parked issue", ["ai:parked"])]])

    parked = result.stdout.split("PARKED ITEMS", 1)[1].split("LAST TICK", 1)[0]
    assert "unavailable" in parked
    assert "(none)" not in parked


def test_shipped_jq_programs_bind_no_jq_keyword_as_a_variable() -> None:
    """A keyword variable compiles under jq 1.7 and fails the program under 1.6.

    jq 1.6 still ships with Ubuntu 22.04 and with Anaconda, while CI's runner
    has 1.7, so running the scripts in CI cannot catch this. A failed program
    only prints a compile error in place of its section, and the dashboard
    still exits 0, so on a 1.6 host a full queue reads as empty.
    """
    repo_root = Path(__file__).resolve().parents[2]
    scripts = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "skills/*/*.sh"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    keyword_variable = re.compile(r"\$(" + "|".join(JQ_KEYWORDS) + r")\b")

    violations = [
        f"{script}:{line_no}: {line.strip()}"
        for script in scripts
        if "jq" in (text := (repo_root / script).read_text())
        for line_no, line in enumerate(text.splitlines(), start=1)
        if keyword_variable.search(line)
    ]

    assert violations == []


def test_status_displays_requests_and_evidence_without_claiming_compliance(tmp_path, tick_module):
    repo = install_status_script(tmp_path)
    log = tick_module.TickLog.open(str(repo / ".claude/worktrees/.tick-log"))
    log.log(
        pr=7, phase="harvest", stage="implement", action="engine_result_summary",
        reason="fixture", engine="claude", dispatch_id="fixture-7",
        details={"skills_used": [], "skill_usage_status": "active"},
    )
    tick_module._log_skill_context(
        tick_log=log, number=7, stage="implement", engine="claude", dispatch_id="fixture-7",
    )
    result = run_status(repo)
    assert result.returncode == 0, result.stderr
    assert "requested: definition-of-done" in result.stdout
    assert "no observation; use unknown" in result.stdout


@pytest.mark.parametrize('present', [False, True])
def test_status_separates_loop_record_from_tick_log_evidence(tmp_path, tick_module, live_engine, present):
    repo = install_status_script(tmp_path)
    root = repo / ".claude/worktrees"
    path = root / ".tick-state/loop.json"
    if present:
        pid, start = live_engine
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "schema_version": 1, "invocation_id": "status-test", "pid": pid,
            "process_start_time": start, "recorded_at": time.time(),
            "host": tick_module.platform.node(),
            "scope": {"mode": "target", "targets": ["#117"]},
        }))
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = run_status(repo)
    assert result.returncode == 0, result.stderr
    assert "LOOP PROCESS: " + ("running" if present else "unavailable") in result.stdout
    assert "record_present=" + ("true" if present else "false") in result.stdout
    assert "DISPATCH SCOPE:" in result.stdout
    assert "LAST TICK AUDIT LOG" in result.stdout
    after = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert after == before


def test_status_reports_a_killed_unreaped_loop_as_not_running(tmp_path, tick_module):
    """A zombie keeps its PID and start time, but it cannot run ticks."""
    process_control = tick_module.process_control
    repo = install_status_script(tmp_path)
    path = repo / ".claude/worktrees/.tick-state/loop.json"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        start = process_control.get_process_start_time(proc.pid)
        assert start is not None, "cannot read the child's start time"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "schema_version": 1, "invocation_id": "zombie-test", "pid": proc.pid,
            "process_start_time": start, "recorded_at": time.time(),
            "host": tick_module.platform.node(),
            "scope": {"mode": "full", "targets": []},
        }))
        before = path.read_bytes()
        proc.kill()
        deadline = time.monotonic() + 10  # Popen.poll() would reap the zombie.
        while not (process_control.get_process_state(proc.pid) or "").startswith("Z"):
            assert time.monotonic() < deadline, "killed child never became a zombie"
            time.sleep(0.05)
        result = run_status(repo)
        assert result.returncode == 0, result.stderr
        assert "LOOP PROCESS: not_running · record_present=true · process_zombie" in result.stdout
        assert "source=readable/zombie" in result.stdout
        assert path.read_bytes() == before
    finally:
        proc.kill()
        proc.wait(timeout=10)


# --- Helpers ---


# Reserved words of the jq 1.6 lexer. jq 1.7 accepts them as `$name` bindings;
# 1.6 rejects the whole program with "unexpected <word>, expecting IDENT".
JQ_KEYWORDS = (
    "__loc__", "and", "as", "catch", "def", "elif", "else", "end", "foreach",
    "if", "import", "include", "label", "module", "or", "reduce", "then", "try",
)


def test_status_shows_the_dispatch_scope_banner_from_tick_log_evidence(tmp_path):
    repo = install_status_script(tmp_path)
    log_dir = repo / ".claude/worktrees/.tick-log"
    log_dir.mkdir(parents=True)
    now = time.time()
    (log_dir / "tick-20260101T000000-000001.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in [
        {"ts": now - 10, "pr": None, "phase": "dispatch", "stage": "all",
         "action": "only_scope_resolved", "reason": "--only #5,pr:7",
         "details": {"only": ["#5", "pr:7"], "scope": ["#5", "pr:9", "pr:7"]}},
        {"ts": now - 5, "pr": None, "phase": "summary", "stage": "",
         "action": "tick_complete", "reason": "",
         "details": {"cadence": {"next_wake_seconds": 270, "stop": False, "reason": "x"}}},
    ]))

    result = run_status(repo)

    assert result.returncode == 0, result.stderr
    banner = [line for line in result.stdout.splitlines() if line.startswith("DISPATCH SCOPE")]
    assert len(banner) == 1, result.stdout
    assert "--only #5,pr:7 (dispatch scope #5,pr:9,pr:7)" in banner[0]
    assert "log evidence active" in banner[0]
    assert result.stdout.index("DISPATCH SCOPE") < result.stdout.index("IN-FLIGHT ENGINES")


def test_status_scope_banner_reports_missing_evidence(tmp_path):
    repo = install_status_script(tmp_path)

    result = run_status(repo)

    assert result.returncode == 0, result.stderr
    assert "DISPATCH SCOPE: no tick evidence" in result.stdout


def stalled_quiet_seconds() -> float:
    """Seconds of heartbeat silence that must render ``STALLED``.

    Read from the tick lib's own threshold rather than hardcoded. That
    threshold has to keep real margin over the longest legitimate silence (a
    maxed-out tool call), so its value moves — and a fixed "20 minutes" would
    quietly slide to the working side of the boundary when it did, leaving
    these rows green while proving nothing about the stalled verdict.
    """
    sys.path.insert(0, str(TICK_LIB))
    try:
        import tick  # type: ignore[import-untyped]

        return tick.ENGINE_STALL_SECONDS + 5 * 60
    finally:
        sys.path.remove(str(TICK_LIB))


def write_stub(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def install_status_script(tmp_path: Path, *, target_repo="test-owner/test-repo") -> Path:
    """Create a git repo holding the dashboard and the tick lib it calls.

    The lib is copied rather than stubbed because the progress column is
    rendered from ``tick.py engine-progress``: the dashboard's verdict is only
    trustworthy if these tests exercise the same classifier the watcher uses.
    The interpreter resolver comes along for the same reason — it is the tick
    skill's file, sourced across the skill boundary, so an install that dropped
    it would break the dashboard here rather than on an operator's box.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "upstream", f"https://github.com/{target_repo}"],
                   cwd=repo, check=True)
    script_dst = repo / ".claude/skills/status/status.sh"
    script_dst.parent.mkdir(parents=True)
    shutil.copy(Path(__file__).with_name("status.sh"), script_dst)
    tick_dst = repo / ".claude/skills/tick"
    tick_dst.mkdir(parents=True)
    shutil.copy(TICK_LIB.parent / "resolve-python.sh", tick_dst / "resolve-python.sh")
    shutil.copy(TICK_LIB.parent / "resolve-target.sh", tick_dst / "resolve-target.sh")
    lib_dst = tick_dst / "lib"
    lib_dst.mkdir(parents=True)
    for name in os.listdir(TICK_LIB):
        if name.endswith(".py") and not name.startswith("test_"):
            shutil.copy(TICK_LIB / name, lib_dst / name)
    return repo


def dispatch_real_implementer_marker(
    tick: ModuleType, *, repo: Path, issue_number: int
) -> Path:
    """Emit one ``dispatch.json`` through ``tick.py``'s real dispatch path.

    Only the boundaries the dashboard does not depend on are stubbed: git
    (no real worktree to create), the engine subprocess, and the label
    write. Marker construction, deadline math, and the atomic write are
    the production code.
    """
    wt_root = repo / ".claude/worktrees"
    wt_dir = wt_root / f"issue-{issue_number}"

    def fake_git(*args: str) -> str:
        if "worktree" in args and "add" in args:
            wt_dir.mkdir(parents=True, exist_ok=True)
            return ""
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo)
        return "0" * 40

    proc = mock.MagicMock()
    proc.pid = 999999
    with (
        mock.patch.object(tick, "WT_ROOT", str(wt_root)),
        mock.patch.object(tick, "TARGET_REMOTE", "upstream"),
        mock.patch.object(tick, "TRUSTED_HEAD_REPO", "test-owner/test-repo"),
        mock.patch.object(tick, "gh_json", return_value={
            "defaultBranchRef": {"name": "main"},
        }),
        mock.patch.object(tick, "git", side_effect=fake_git),
        mock.patch.object(tick.pipeline, "validate_target_context"),
        mock.patch.object(tick, "relabel"),
        mock.patch("subprocess.Popen", return_value=proc),
    ):
        tick._dispatch_one_implementer(  # type: ignore[attr-defined]
            issue_number=issue_number,
            issue_title="status dashboard marker fixture",
            tick_log=None,
        )
    return wt_dir / "dispatch.json"


def write_marker_with_heartbeat(
    worktree_dir: Path,
    *,
    quiet_seconds: float | None,
    pid: int,
    start_time: float,
    stage: str = "implement",
    pr_number: int = 577,
    time_to_deadline: float = 25 * 60,
) -> None:
    """Write an engine marker whose progress evidence is aged on purpose.

    ``quiet_seconds`` backdates the heartbeat mtime, or omits the heartbeat
    file entirely when ``None``. ``pid``/``start_time`` are what the shared
    classifier verifies against the live process table, so callers pass a real
    process to get a proven-live row and a mismatched pair to get the
    reused-PID case.
    """
    now = time.time()
    (worktree_dir / "dispatch.json").write_text(
        json.dumps({
            "pr_number": pr_number,
            "stage": stage,
            "engine": "claude",
            "pid": pid,
            "start_time": start_time,
            "deadline": now + time_to_deadline,
        }),
        encoding="utf-8",
    )
    (worktree_dir / "result.json").write_text("", encoding="utf-8")
    if quiet_seconds is not None:
        heartbeat = worktree_dir / "heartbeat"
        heartbeat.write_text("", encoding="utf-8")
        os.utime(heartbeat, (now - quiet_seconds, now - quiet_seconds))


def run_status(
    repo: Path, *, cwd: Path | None = None, pages=None, failure=None,
) -> subprocess.CompletedProcess[str]:
    """Run the dashboard with paginated ``gh`` responses and real Python.

    ``gh`` returns the supplied pages, or an empty repository by default.
    The classifier is *not* stubbed out — the progress column's whole contract
    is that it renders what the real ``tick.py`` reports, so a stub would prove
    only that bash can print a literal. ``PR_PIPELINE_PYTHON`` pins resolution
    to the interpreter running these tests, which is known to satisfy the 3.11+
    floor where the ambient ``python3`` is not; the resolver and the marker
    classification underneath are both production code.

    ``cwd`` defaults to ``repo``; pass a linked worktree to exercise the
    marker-root anchoring.
    """
    stub_bin = repo.parent / "bin"
    stub_bin.mkdir(exist_ok=True)
    write_stub(
        stub_bin / "gh",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$STATUS_GH_LOG"\n'
        + ('echo "GitHub unavailable" >&2\nexit 1\n' if failure == "api"
           else 'cat "$STATUS_GH_PAYLOAD"\n'),
    )
    payload = repo.parent / "gh-payload.json"
    payload.write_text(json.dumps(pages if pages is not None else [[]]))

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["PR_PIPELINE_PYTHON"] = sys.executable
    env["STATUS_GH_LOG"] = str(repo.parent / "gh-calls")
    env["STATUS_GH_PAYLOAD"] = str(payload)
    return subprocess.run(
        ["bash", str(repo / ".claude/skills/status/status.sh")],
        cwd=cwd or repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def status_item(number, title, labels, *, pr=False):
    item = {"number": number, "title": title, "labels": [{"name": name} for name in labels]}
    if pr:
        item["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{number}"}
    return item


def in_flight_row(stdout: str, *, pr_number: int) -> str:
    """Return the one IN-FLIGHT ENGINES row belonging to ``pr_number``."""
    row = re.search(rf"^\s+PR#{pr_number}\b.*$", stdout, re.MULTILINE)
    assert row is not None, f"no in-flight row for PR#{pr_number} in:\n{stdout}"
    return row.group(0)


def progress_cell(stdout: str, *, pr_number: int) -> str:
    """Extract the progress column from one in-flight row.

    Read positionally rather than by substring search. ``-`` (no progress
    contract) and ``?`` (the verdict could not be produced) are different
    claims that the column documents separately, and a row already contains a
    ``-`` in its ``m-to-deadline`` field — so a substring assertion cannot
    tell the two states apart, nor either of them from a missing column.
    """
    row = in_flight_row(stdout, pr_number=pr_number)
    fields = row.split()
    assert len(fields) == 8, f"unexpected in-flight row shape: {row}"
    return fields[6]


def ttl_minutes(stdout: str, *, pr_number: int) -> int:
    """Extract the ``m-to-deadline`` column from one in-flight row."""
    row = in_flight_row(stdout, pr_number=pr_number)
    ttl = re.search(r"(-?\d+)m-to-deadline", row)
    assert ttl is not None, f"no deadline column in row: {row}"
    return int(ttl.group(1))
