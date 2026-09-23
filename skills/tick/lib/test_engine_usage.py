"""Offline terminal-output fixtures: provider semantics and production plumbing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from io import BytesIO
from unittest import mock

import pytest


@pytest.fixture
def claude_result() -> dict[str, object]:
    # Claude Agent SDK result shape; cache counters are disjoint input buckets.
    # https://code.claude.com/docs/en/agent-sdk/cost-tracking
    # https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "NEEDS_HUMAN: fixture",
        "num_turns": 4,
        "usage": {
            "input_tokens": 11,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
            "output_tokens": 31,
            "cache_creation": {"ephemeral_5m_input_tokens": 20},
        },
        "total_cost_usd": 0.14,
    }


@pytest.fixture
def codex_terminal() -> dict[str, object]:
    # codex exec --json terminal schema, including optional newer subcounts.
    # https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 131,
            "cached_input_tokens": 100,
            "cache_write_input_tokens": 20,
            "output_tokens": 31,
            "reasoning_output_tokens": 10,
        },
    }


@pytest.fixture
def offline_tick_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Real launcher topology; only GitHub, paid engines and notifications are shims."""
    import pipeline

    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://github.com/example/usage",
        ],
        check=True,
    )
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["USAGE_TEST_REPO"])
state = root / "label"
label = state.read_text() if state.exists() else "ai:implementing"
with (root / "gh-calls.jsonl").open("a") as fh:
    fh.write(json.dumps(args) + "\\n")
if args[:1] == ["--version"]:
    print("gh version 2.75.0")
elif args[:1] == ["api"]:
    print(os.environ["USAGE_TEST_LABELS"])
elif args[:2] == ["issue", "list"]:
    print(json.dumps([{"number": 7, "labels": [{"name": label}]}] if label in args else []))
elif args[:2] == ["issue", "view"]:
    fields = args[args.index("--json") + 1] if "--json" in args else ""
    if "-q" in args:
        print("OPEN" if fields == "state" else label if "labels" in fields else "")
    else:
        print(json.dumps({"labels": [{"name": label}], "state": "OPEN", "comments": []}))
elif args[:2] == ["issue", "edit"]:
    state.write_text(args[args.index("--add-label") + 1])
elif args[:2] == ["issue", "comment"]:
    print("https://example.invalid/comments/1")
else:
    print("" if "-q" in args else "[]")
"""
    )
    gh.chmod(0o755)
    for name in ("claude", "codex", "osascript"):
        stub = bin_dir / name
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = --version ]; then printf "codex-cli 0.142.5\\n"; exit 0; fi\n'
            "exit 1\n"
        )
        stub.chmod(0o755)
    config = repo / "operator.toml"
    config.write_text('[operator]\nlogin="test-owner"\n[repos."example/usage"]\n')
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PR_PIPELINE_CONFIG": str(config),
        "PR_PIPELINE_PYTHON": sys.executable,
        "PIPELINE_REPO": "example/usage",
        "USAGE_TEST_REPO": str(repo),
        "USAGE_TEST_LABELS": json.dumps(
            [[{"name": label} for label in sorted(pipeline.WORKFLOW_LABELS)]]
        ),
    }
    return repo, env


def test_claude_usage_retains_native_disjoint_counters(claude_result: dict) -> None:
    from engine_usage import claude_usage

    usage = claude_usage(claude_result)
    assert usage["status"] == "complete"
    assert usage["input_tokens"] == 131
    assert usage["cached_input_tokens"] == 100
    assert usage["output_tokens"] == 31
    assert usage["native"]["input_tokens"] == 11
    assert "cache_creation" not in usage["native"]
    assert usage["reported_cost"] == {
        "amount": 0.14,
        "unit": "USD",
        "source": "claude.result.total_cost_usd",
    }
    assert usage["source"] == "claude.result.usage"
    assert usage["token_unit"] == "tokens"


def test_codex_usage_does_not_add_cache_or_reasoning(codex_terminal: dict) -> None:
    from reviewer_observability import observe_event_lines

    usage = observe_event_lines([json.dumps(codex_terminal).encode()])["usage"]
    assert usage["input_tokens"] == 131
    assert usage["output_tokens"] == 31
    assert usage["native"]["reasoning_output_tokens"] == 10
    assert usage["reported_cost"] is None


@pytest.mark.parametrize(
    "bad", [True, -1, 1.5, "12", None, [], {}, float("nan"), float("inf"), 2**64]
)
def test_invalid_tokens_are_unavailable_not_zero(
    claude_result: dict, bad: object
) -> None:
    from engine_usage import claude_usage

    claude_result["usage"]["cache_creation_input_tokens"] = bad
    usage = claude_usage(claude_result)
    assert usage["status"] == "partial"
    assert usage["input_tokens"] is None
    assert usage["cached_input_tokens"] == 100
    assert "cache_creation_input_tokens" not in usage["native"]
    json.dumps(usage, allow_nan=False)


@pytest.mark.parametrize(
    "bad", [True, -1, "0.14", {}, float("nan"), float("inf"), 10**400]
)
def test_invalid_cost_is_not_persisted(claude_result: dict, bad: object) -> None:
    from engine_usage import claude_usage

    claude_result["total_cost_usd"] = bad
    usage = claude_usage(claude_result)
    assert usage["reported_cost"] is None
    assert usage["status"] == "partial"
    json.dumps(usage, allow_nan=False)


def test_old_or_nonterminal_results_have_unavailable_usage() -> None:
    from engine_usage import claude_usage

    for result in (
        None,
        {},
        {"result": "done"},
        {"type": "assistant", "usage": {"input_tokens": 50}},
    ):
        usage = claude_usage(result)
        assert usage["status"] == "unavailable"
        assert usage["input_tokens"] is None
        assert usage["native"] == {}


def test_observation_partial_legacy_and_strict_roundtrip(
    tmp_path: Path, codex_terminal: dict
) -> None:
    from reviewer_observability import load_observation, observe_event_lines

    path = tmp_path / "review-observation.json"
    observation = observe_event_lines(
        [b"malformed\n", json.dumps(codex_terminal).encode()]
    )
    assert observation["schema_version"] == 2
    assert observation["status"] == "partial"
    assert observation["usage"]["status"] == "partial"
    path.write_text(json.dumps(observation))
    assert load_observation(path) == observation
    observation["usage"]["raw_event"] = "secret"
    path.write_text(json.dumps(observation))
    assert load_observation(path)["status"] == "unavailable"
    observation.pop("usage")
    observation["schema_version"] = 1
    path.write_text(json.dumps(observation))
    assert load_observation(path) == observation


def test_terminal_only_usage_and_repeated_snapshots(codex_terminal: dict) -> None:
    from reviewer_observability import observe_event_lines

    forged = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(codex_terminal)},
    }
    assert (
        observe_event_lines([json.dumps(forged).encode()])["usage"]["status"]
        == "unavailable"
    )
    event = json.dumps(codex_terminal).encode()
    observation = observe_event_lines([event, event])
    assert observation["usage"]["input_tokens"] == 131
    assert observation["usage"]["status"] == "partial"  # ambiguous multiple terminals


@pytest.mark.parametrize(
    "bad", [True, -1, 1.5, "12", None, [], {}, float("nan"), float("inf"), 2**64]
)
def test_codex_invalid_counters_preserve_other_fields(
    codex_terminal: dict, bad: object
) -> None:
    from reviewer_observability import observe_event_lines

    codex_terminal["usage"]["input_tokens"] = bad
    usage = observe_event_lines([json.dumps(codex_terminal).encode()])["usage"]
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] == 31
    assert usage["status"] == "partial"


def test_zero_usage_and_older_codex_counters_are_supported() -> None:
    from engine_usage import claude_usage, codex_usage

    assert (
        codex_usage({"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})[
            "status"
        ]
        == "complete"
    )
    result = {"type": "result", "usage": {"output_tokens": 0}, "total_cost_usd": 0}
    usage = claude_usage(result)
    assert usage["status"] == "partial"
    assert usage["reported_cost"]["amount"] == 0
    assert usage["input_tokens"] is None


def test_oversized_and_deep_events_cannot_hide_later_terminal(
    codex_terminal: dict,
) -> None:
    from reviewer_observability import observe_event_stream

    data = (
        b"x" * 600
        + b"\n"
        + b"[" * 2000
        + b"]" * 2000
        + b"\n"
        + json.dumps(codex_terminal).encode()
        + b"\n"
    )
    observation = observe_event_stream(BytesIO(data), max_event_bytes=512)
    assert observation["oversized_events_skipped"] == 2
    assert observation["usage"]["status"] == "partial"
    assert observation["usage"]["input_tokens"] == 131


@pytest.mark.parametrize("value", [True, -1, "4", 4.5])
def test_engine_reported_turns_remain_a_numeric_count(
    claude_result: dict, value: object
) -> None:
    import tick

    claude_result["num_turns"] = value
    details = tick._engine_result_summary(
        result_raw=json.dumps(claude_result),
        harvest_result={},
        harvest_action="",
        harvest_label="",
    )
    assert details["num_turns"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"status": []},
        {"schema_version": True},
        {"usage": []},
        {"observed_skills": [{"name": "safe", "source": []}]},
        {"event_count": True},
    ],
)
def test_malformed_observation_cannot_interrupt_harvest(
    tmp_path: Path, change: dict
) -> None:
    import tick
    from reviewer_observability import observe_event_lines

    observation = observe_event_lines([])
    observation.update(change)
    (tmp_path / "review-observation.json").write_text(json.dumps(observation))
    log = tick.TickLog.open(str(tmp_path / "logs"))
    tick._log_reviewer_skill_observation(
        tick_log=log, number=7, wt_dir=str(tmp_path), dispatch_id="review-7"
    )
    entry = json.loads(Path(log.path).read_text())
    assert entry["details"]["status"] == "unavailable"
    assert entry["details"]["usage"]["status"] == "unavailable"


def test_usage_loader_rejects_extra_fields_wrong_types_and_oversized_file(
    tmp_path: Path, codex_terminal: dict
) -> None:
    from reviewer_observability import load_observation, observe_event_lines

    path = tmp_path / "review-observation.json"
    observation = observe_event_lines([json.dumps(codex_terminal).encode()])
    for key, value in (
        ("input_tokens", 131.0),
        ("output_tokens", True),
        ("native", {"secret": 1}),
        ("reported_cost", {"amount": 1}),
    ):
        damaged = json.loads(json.dumps(observation))
        damaged["usage"][key] = value
        path.write_text(json.dumps(damaged))
        assert load_observation(path)["status"] == "unavailable"
    path.write_text(" " * 40_000 + json.dumps(observation))
    assert load_observation(path)["status"] == "unavailable"


def test_reported_model_names_do_not_copy_payloads(claude_result: dict) -> None:
    from engine_usage import claude_models

    claude_result["modelUsage"] = {"claude-example": {"private": "payload"}}
    assert claude_models(claude_result) == ["claude-example"]
    for models in (
        {"secret\npath": {}},
        {"x" * 129: {}},
        {f"m{i}": {} for i in range(17)},
        [],
    ):
        claude_result["modelUsage"] = models
        assert claude_models(claude_result) is None


@pytest.mark.parametrize("with_usage", [True, False])
def test_claude_wrapper_to_harvest_keeps_completion_authority(
    tmp_path: Path, claude_result: dict, with_usage: bool
) -> None:
    import tick

    if not with_usage:
        claude_result.pop("usage")
        claude_result.pop("total_cost_usd")
    wrapper = Path(__file__).with_name("claude_stream_wrapper.py")
    result = subprocess.run(
        [
            sys.executable,
            str(wrapper),
            "--",
            sys.executable,
            "-c",
            f"print({json.dumps(claude_result)!r})",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    wt_root = tmp_path / ".claude" / "worktrees"
    wt_dir = wt_root / "issue-7"
    wt_dir.mkdir(parents=True)
    (wt_dir / "result.json").write_text(result.stdout)
    (wt_dir / "dispatch.json").write_text(
        json.dumps(
            {
                "dispatch_id": "impl-7-usage",
                "engine": "claude-ds",
                "pre_label": "ai:available",
            }
        )
    )
    log = tick.TickLog.open(str(wt_root / ".tick-log"))
    decision = {
        "action": "harvest",
        "label": "",
        "kill": False,
        "identity": "matched",
        "pr_number": 7,
    }
    with mock.patch(
        "tick._complete_escalate_transition", return_value=False
    ) as complete:
        # A failed handoff retries the same result: evidence can repeat, usage must not be summed.
        tick._harvest_one_implementer(
            decision, str(wt_root), tick_log=log, attempts=None
        )
        tick._harvest_one_implementer(
            decision, str(wt_root), tick_log=log, attempts=None
        )
    assert complete.call_count == 2
    assert complete.call_args.kwargs["harvest_label"] == "ai:needs-human-decision"
    summaries = [
        json.loads(line)
        for line in Path(log.path).read_text().splitlines()
        if json.loads(line)["action"] == "engine_result_summary"
    ]
    assert len(summaries) == 2
    for summary in summaries:
        assert summary["engine"] == "claude-ds"
        assert summary["dispatch_id"] == "impl-7-usage"
        assert summary["details"]["usage"]["input_tokens"] == (
            131 if with_usage else None
        )
        assert summary["details"]["num_turns"] == 4


@pytest.mark.parametrize("terminal", ["valid", "missing", "malformed"])
def test_codex_wrapper_cli_usage_does_not_control_completion(
    tmp_path: Path, codex_terminal: dict, terminal: str
) -> None:
    from reviewer_observability import load_observation

    if terminal == "missing":
        codex_terminal = {"type": "turn.failed", "error": {"message": "private"}}
    elif terminal == "malformed":
        codex_terminal["usage"] = {
            "input_tokens": True,
            "cached_input_tokens": "secret",
        }
    wrapper = Path(__file__).resolve().parents[1] / "engine_wrapper.py"
    script = (
        "import sys; from pathlib import Path; "
        f"print({json.dumps(codex_terminal)!r}); "
        'Path(sys.argv[sys.argv.index(\'-o\')+1]).write_text(\'{"verdict":"ready_for_human","findings":[]}\')'
    )
    result = subprocess.run(
        [
            sys.executable,
            str(wrapper),
            "run",
            "--dispatch-id",
            "review-7-usage",
            "--marker-path",
            "dispatch.json",
            "--result-path",
            "review.json",
            "--envelope-path",
            "completion.json",
            "--observation-path",
            "review-observation.json",
            "--engine",
            "codex",
            "--stage",
            "review",
            "--pr-number",
            "7",
            "--deadline",
            "9999999999",
            "--base-ref",
            "main",
            "--base-sha",
            "b" * 40,
            "--head-sha",
            "a" * 40,
            "--pre-label",
            "ai:implemented",
            "--",
            sys.executable,
            "-c",
            script,
            "-o",
            "placeholder.json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((tmp_path / "completion.json").read_text())["result_valid"] is True
    )
    observation = load_observation(tmp_path / "review-observation.json")
    assert observation["usage"]["input_tokens"] == (
        131 if terminal == "valid" else None
    )
    assert "secret" not in json.dumps(observation)
    assert "private" not in json.dumps(observation)


@pytest.mark.parametrize("with_usage", [True, False])
def test_shipped_tick_harvest_reports_usage_without_changing_outcome(
    offline_tick_repo: tuple, claude_result: dict, with_usage: bool
) -> None:
    repo, env = offline_tick_repo
    wt_dir = repo / ".claude" / "worktrees" / "issue-7"
    wt_dir.mkdir(parents=True)
    if not with_usage:
        claude_result["usage"] = {"input_tokens": True, "output_tokens": "private"}
        claude_result.pop("total_cost_usd")
    (wt_dir / "result.json").write_text(json.dumps(claude_result))
    (wt_dir / "dispatch.json").write_text(
        json.dumps(
            {
                "engine": "claude",
                "stage": "implement",
                "pr_number": 7,
                "deadline": 9999999999,
                "base_ref": "main",
                "base_sha": "b" * 40,
                "head_sha": "a" * 40,
                "pre_label": "ai:available",
                "dispatch_id": "impl-7-cli",
                "pid": 999999999,
                "start_time": 1000.0,
                "pgid": 999999999,
            }
        )
    )
    # Override is used only for the saved before-change CLI proof.
    launcher = os.environ.get(
        "USAGE_PROOF_TICK", str(Path(__file__).resolve().parents[1] / "tick.sh")
    )
    result = subprocess.run(
        ["bash", launcher],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (repo / "label").read_text() == "ai:needs-human-decision", result.stderr
    assert not wt_dir.exists()
    rows = [
        json.loads(line)
        for path in (repo / ".claude/worktrees/.tick-log").glob("tick-*.jsonl")
        for line in path.read_text().splitlines()
    ]
    summary = next(row for row in rows if row["action"] == "engine_result_summary")
    assert summary["details"]["usage"]["input_tokens"] == (131 if with_usage else None)


@pytest.mark.skipif(
    shutil.which("jq") is None, reason="optional read-only report requires jq"
)
def test_documented_usage_query_deduplicates_attempts_and_joins_provenance(
    tmp_path: Path, claude_result: dict, codex_terminal: dict
) -> None:
    import tick
    from reviewer_observability import observe_event_lines

    log_dir = tmp_path / ".claude/worktrees/.tick-log"
    log_dir.mkdir(parents=True)
    log = tick.TickLog(str(log_dir / "tick-fixture.jsonl"))
    for dispatch_id in ("attempt-a", "attempt-b"):
        for _ in range(2):
            tick._log_engine_result_summary(
                tick_log=log,
                number=7,
                stage="implement",
                dispatch_id=dispatch_id,
                result_raw=json.dumps(claude_result),
                harvest_result={},
                harvest_action="escalate",
                harvest_label="ai:needs-human-decision",
                engine="claude",
            )
    observation = observe_event_lines([json.dumps(codex_terminal).encode()])
    (tmp_path / "review-observation.json").write_text(json.dumps(observation))
    for _ in range(2):
        tick._log_reviewer_skill_observation(
            tick_log=log, number=7, wt_dir=str(tmp_path), dispatch_id="attempt-a"
        )
    log.log(
        pr=7,
        phase="dispatch",
        stage="review",
        action="model_resolved",
        reason="",
        engine="codex",
        dispatch_id="attempt-a",
        details={"model": "example-model", "effort": "high"},
    )
    log.log(
        pr=8,
        phase="dispatch",
        stage="review",
        action="launch",
        reason="",
        engine="codex",
        dispatch_id="no-result",
    )
    log.log(
        pr=9,
        phase="dispatch",
        stage="review",
        action="model_resolved",
        reason="",
        engine="codex",
        dispatch_id="not-launched",
    )
    log.log(
        pr=10,
        phase="harvest",
        stage="review",
        action="reviewer_skill_observation",
        reason="",
        engine="codex",
        dispatch_id="unknown",
    )
    # Run the actual documentation, including its relative LOG_DIR and glob.
    readme = Path(__file__).resolve().parents[1] / "README.md"
    query = (
        readme.read_text()
        .split("<!-- engine-usage-report -->", 1)[1]
        .split("```bash\n", 1)[1]
        .split("```", 1)[0]
    )
    result = subprocess.run(
        ["bash", "-c", query], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    decoder = json.JSONDecoder()
    output = result.stdout.strip()
    rows = []
    while output:
        row, offset = decoder.raw_decode(output)
        rows.append(row)
        output = output[offset:].lstrip()
    assert len(rows) == 4
    assert sum(row["usage"]["input_tokens"] or 0 for row in rows) == 393
    review = next(row for row in rows if row["stage"] == "review" and row["pr"] == 7)
    assert review["model"] == "example-model"
    assert review["effort"] == "high"
    assert review["engine_reported_turns"] is None
    missing = next(row for row in rows if row["pr"] == 8)
    assert missing["usage"]["status"] == "unavailable"
    assert missing["model"] is None
