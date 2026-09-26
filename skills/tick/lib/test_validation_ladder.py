"""Offline checks of the ladder's evidence gates; no GitHub or paid engines."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


LADDER = Path(__file__).resolve().parents[3] / "validation"


@pytest.fixture
def upgrade(tmp_path):
    """Run the complete L4 script with real local git and a canned CLI."""
    work = tmp_path / "work"
    work.mkdir()
    bins = tmp_path / "bin"
    bins.mkdir()
    env = dict(os.environ, LADDER_WORK=str(work), PATH=f"{bins}:{os.environ['PATH']}")
    env.update(GIT_AUTHOR_NAME="Ladder test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Ladder test", GIT_COMMITTER_EMAIL="test@example.invalid")
    remote = tmp_path / "remote.git"
    git("init", "--bare", str(remote), env=env)
    source = work / "l4-source"
    git("clone", str(remote), str(source), env=env)
    (source / ".claude-plugin").mkdir()
    (source / ".claude-plugin/plugin.json").write_text('{"version": "0.1.0"}\n')
    git("-C", str(source), "add", ".", env=env)
    git("-C", str(source), "commit", "-m", "seed", env=env)
    git("-C", str(source), "push", "-u", "origin", "HEAD", env=env)
    for version in ("0.1.0", "0.1.1"):
        root = work / "plugins/cache/pr-pipeline" / version
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin/plugin.json").write_text(json.dumps({"version": version}))
        (root / "skills/tick").mkdir(parents=True)
        shutil.copy(LADDER.parent / "skills/tick/resolve-python.sh", root / "skills/tick/")
        (root / "skills/tick/lib").mkdir()
        (root / "skills/tick/lib/test_skill_contract.py").write_text(
            '_PUBLISHED_SKILLS = frozenset({"pr-pipeline:status"})\n',
        )
        (root / "skills/status").mkdir()
        (root / "skills/status/status.sh").write_text('echo "PR PIPELINE"\n')
    config = work / "operator.toml"
    config.write_text('[operator]\nlogin = "test-owner"\n')
    env["PR_PIPELINE_CONFIG"] = str(config)
    env["CLAUDE_CONFIG_DIR"] = str(work)
    executable(bins / "gh", "raise SystemExit('unexpected GitHub call')\n")
    executable(bins / "claude", '''
import json, os, pathlib, sys
work = pathlib.Path(os.environ["LADDER_WORK"])
args = sys.argv[1:]
if args[:2] == ["plugin", "list"]:
    version = "0.1.1" if (work / "updated").exists() else "0.1.0"
    print(json.dumps([{"id":"pr-pipeline@pr-pipeline", "scope":"local",
        "projectPath":str(work / "unrelated"), "version":version,
        "installPath":str(work / "plugins/cache/pr-pipeline" / version)}]))
elif args[:2] == ["plugin", "update"]:
    (work / "updated").touch()
elif args[:3] == ["plugin", "marketplace", "update"]:
    pass
elif args[:3] == ["plugin", "marketplace", "add"] or args[:2] == ["plugin", "install"]:
    pass
elif args[:2] == ["plugin", "details"]:
    print("Skills (1) pr-pipeline:status")
elif args[0] == "-p":
    print((work / "transcript.jsonl").read_text(), end="")
    sys.exit(int(os.environ.get("SESSION_EXIT", "0")))
else:
    raise SystemExit(f"unexpected Claude call: {args}")
''')
    return work, env


@pytest.fixture
def real_stage(tmp_path):
    """Drive L3's script and observer with local tick/GitHub/engine doubles."""
    work = tmp_path / "work"
    bins = tmp_path / "bin"
    root = tmp_path / "install"
    checkout = work / "l3-fixture-target/checkout"
    (checkout / ".git").mkdir(parents=True)
    bins.mkdir()
    (root / "skills/tick").mkdir(parents=True)
    (root / "skills/setup").mkdir()
    shutil.copy(LADDER.parent / "skills/tick/resolve-python.sh", root / "skills/tick/")
    env = dict(os.environ, LADDER_WORK=str(work), PATH=f"{bins}:{os.environ['PATH']}",
               FIXTURE_ROOT=str(root))
    (root / "skills/setup/setup.sh").write_text(
        'echo \'{"login":"test-owner","outcome":"pass"}\'\n',
    )
    executable(bins / "gh", '''
import os, sys
args = sys.argv[1:]
if args[:2] == ["repo", "view"]:
    print("develop" if os.environ.get("LADDER_TEST_FIXTURE") == "b" else "main")
elif args[:2] == ["issue", "create"]:
    print("https://example.invalid/issues/7")
elif args[:2] == ["pr", "list"]:
    print("12")
elif args[:2] == ["issue", "view"]:
    print("")
elif args[:2] == ["pr", "view"]:
    field = args[args.index("--json") + 1]
    base = "develop" if os.environ.get("LADDER_TEST_FIXTURE") == "b" else "main"
    print({"labels":"ai:ready-for-human", "baseRefName":base, "headRefName":"feature"}[field])
elif args[0] == "api":
    print("ai:changes-requested\\nai:fixing\\nai:revised\\nai:reviewing\\nai:ready-for-human")
else:
    raise SystemExit(f"unexpected GitHub call: {args}")
''')
    executable(bins / "codex", "raise SystemExit('unexpected Codex call')\n")
    executable(bins / "claude", '''
import os, pathlib, sys
print(pathlib.Path(os.environ["LADDER_WORK"], "transcript.jsonl").read_text(), end="")
sys.exit(int(os.environ.get("SESSION_EXIT", "0")))
''')
    executable(bins / "sleep", '''
import os, pathlib, time
deadline = time.monotonic() + 10
while not pathlib.Path(os.environ["LADDER_WORK"], "done").exists():
    if time.monotonic() > deadline:
        raise SystemExit("tick fixture did not finish")
    time.sleep(0.01)
''')
    tick = root / "fixture_tick.py"
    tick.write_text('''
import json, os, pathlib, subprocess, time
work = pathlib.Path(os.environ["LADDER_WORK"])
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
checkout = pathlib.Path.cwd()
run = pathlib.Path(os.environ["OPEN_AUDIT_LOG"]).parent
case = os.environ.get("EVIDENCE_CASE", "valid")
stage = "implement" if case == "implementer-only" else "fix"
number = 99 if case == "wrong-pr" else (7 if stage == "implement" else 12)
cwd = checkout / ".claude/worktrees" / (f"issue-{number}" if stage == "implement" else f"pr-{number}")
cwd.mkdir(parents=True)
dispatch = {"stage":stage, "pr_number":number, "engine":"claude", "dispatch_id":"dispatch-1"}
if case == "wrong-worktree":
    cwd = checkout / ".claude/worktrees/pr-99"
    cwd.mkdir()
if case != "missing-marker":
    (cwd / "dispatch.json").write_text(json.dumps(dispatch))
log = checkout / ".claude/worktrees/.tick-log"
log.mkdir()
event = {"action":"launch", "stage":stage, "pr":number, "dispatch_id":"dispatch-1"}
if case == "wrong-dispatch":
    event["dispatch_id"] = "another-dispatch"
(log / "tick-fixture.jsonl").write_text(json.dumps(event) + "\\n")
tools = "Read Edit Write Bash(git add *) Bash(git commit *) Bash(git status) Bash(git diff *) Bash(git log *) Bash(python3 -m unittest *)"
mode = "auto"
if os.environ.get("LADDER_TEST_FIXTURE") == "b":
    tools = tools.replace("Bash(python3 -m unittest *)", "Bash(sh ./check.sh)")
    mode = "dontAsk"
tools += " Bash(git push origin HEAD)" if stage == "implement" else " Bash(git push origin HEAD:refs/heads/feature)"
if case == "extra-grant":
    tools += " Bash(sh ./check.sh)" if mode == "auto" else " Bash(python3 -m unittest *)"
argv = ["claude", "-p", "--permission-mode", mode, "--allowedTools", tools]
if case == "wrong-mode-option":
    argv[argv.index("--permission-mode")] = "--model"
elif case == "duplicate-mode":
    argv += ["--permission-mode", "dontAsk"]
elif case == "split-extra-grant":
    argv += ["Bash(sh ./check.sh)"]
subprocess.run(argv, cwd=cwd, check=False)
audit = run / "opened.jsonl"
entry = {"cwd":str(checkout), "path":str(root / "skills/tick/lib/tick.py")}
if case == "forbidden-open":
    entry["path"] = str(checkout / "AGENTS.md")
elif case == "unrelated-audit":
    entry["path"] = str(root / "unrelated.py")
audit.write_text(json.dumps(entry) + "\\n")
if case == "malformed-audit":
    audit.write_text("truncated-json\\n")
elif case == "empty-audit":
    audit.write_text("")
elif case == "missing-audit":
    audit.unlink()
(work / "done").touch()
time.sleep(30)
''')
    (root / "skills/tick/tick.sh").write_text(f'exec {sys.executable} -S "{tick}"\n')
    return work, root, env


@pytest.fixture
def stub_stage(real_stage):
    work, root, env = real_stage
    (work / "l2-fixture-target/checkout/.git").mkdir(parents=True)
    bins = Path(env["PATH"].split(os.pathsep)[0])
    executable(bins / "gh", '''
import sys
args = sys.argv[1:]
if args[:2] == ["repo", "view"]:
    print("trunk")
elif args[:2] == ["issue", "create"]:
    print("https://example.invalid/issues/" + ("8" if "L2-ESCALATE" in " ".join(args) else "7"))
elif args[:2] == ["pr", "list"]:
    print("12")
elif args[:2] == ["issue", "view"]:
    print("ai:needs-human-decision")
elif args[:2] == ["pr", "view"]:
    print("trunk" if "baseRefName" in args else "ai:ready-for-human")
elif args[0] == "api":
    if "/12/" in args[1]:
        print("ai:implemented\\nai:reviewing\\nai:changes-requested\\nai:fixing\\nai:revised\\nai:reviewing\\nai:ready-for-human")
    else:
        print("ai:available\\nai:implementing" + ("\\nai:needs-human-decision" if "/8/" in args[1] else ""))
else:
    raise SystemExit(f"unexpected GitHub call: {args}")
''')
    # The CLI double is not a Python controller and must not activate its hook.
    gh = bins / "gh"
    gh.write_text(gh.read_text().replace(f"#!{sys.executable}\n", f"#!{sys.executable} -S\n", 1))
    (root / "fixture_tick.py").write_text('''
import json, os, pathlib
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
checkout = pathlib.Path.cwd()
run = pathlib.Path(os.environ["OPEN_AUDIT_LOG"]).parent
state = checkout / ".claude/worktrees"
bundle = state / ".harvest-evidence/issue-8-fixture"
bundle.mkdir(parents=True, exist_ok=True)
(bundle / "result.json").write_text("{}")
log = state / ".tick-log"
log.mkdir(exist_ok=True)
events = [{"action":action, "stage":stage, "pr":7 if stage == "implement" else 12}
          for action in ("launch", "harvest") for stage in ("implement", "review", "fix", "review")]
(log / "tick-fixture.jsonl").write_text("".join(json.dumps(e) + "\\n" for e in events))
calls = [{"role":role, "cwd":str(state / "issue-7"),
          "skill_doc":str(root / "skills/review-response-hygiene/SKILL.md")}
         for role in ("probe", "probe", "implementer", "implementer", "fixer")]
(run / "state/claude-calls.jsonl").write_text("".join(json.dumps(c) + "\\n" for c in calls))
(run / "state/codex-calls.jsonl").write_text('{"verdict":"changes_requested"}\\n{"verdict":"ready_for_human"}\\n')
audit = run / "opened.jsonl"
entry = {"cwd":str(checkout), "path":str(root / "skills/tick/lib/tick.py")}
case = os.environ.get("EVIDENCE_CASE", "valid")
if case == "forbidden-open":
    entry["path"] = str(checkout / "AGENTS.md")
elif case == "unrelated-audit":
    entry["path"] = str(root / "unrelated.py")
audit.write_text(json.dumps(entry) + "\\n")
if case == "malformed-audit":
    audit.write_text("truncated-json\\n")
elif case == "empty-audit":
    audit.write_text("")
elif case == "missing-audit":
    audit.unlink()
''')
    return work, root, env


@pytest.mark.parametrize("failure", [
    None, "denied", "missing-result", "failed-command", "failed-session",
    "cli-exit", "malformed", "missing-completion",
])
def test_upgrade_requires_successful_execution(upgrade, failure):
    work, env = upgrade
    command = f"bash {work}/plugins/cache/pr-pipeline/0.1.1/skills/status/status.sh"
    events = transcript("Bash", {"command": command})
    if failure == "denied":
        events[1]["message"]["content"][0].update(is_error=True, content="Permission denied")
    elif failure == "missing-result":
        del events[1]
    elif failure == "failed-command":
        events[1]["message"]["content"][0].update(is_error=True, content="Exit code 1")
    elif failure == "failed-session":
        events[-1].update(subtype="error_during_execution", is_error=True)
    elif failure == "missing-completion":
        events.pop()
    elif failure == "cli-exit":
        env["SESSION_EXIT"] = "1"
    write_jsonl(work / "transcript.jsonl", events)
    if failure == "malformed":
        with (work / "transcript.jsonl").open("a") as stream:
            stream.write("{truncated\n")
    result = subprocess.run(
        ["bash", str(LADDER / "l4-upgrade.sh"), "fixture/plugin"],
        env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == (1 if failure else 0), result.stdout + result.stderr
    assert ("RESULT  L4 FAILED" if failure else "RESULT  L4 passed") in result.stdout
    assert (work / "operator.toml").read_text() == '[operator]\nlogin = "test-owner"\n'


@pytest.mark.parametrize(("fixture_id", "case"), [("a", case) for case in [
    "valid", "denied", "missing-result", "implementer-only", "wrong-pr",
    "wrong-worktree", "missing-marker", "wrong-dispatch", "extra-grant",
    "wrong-mode-option", "duplicate-mode", "split-extra-grant", "malformed-audit", "empty-audit",
    "missing-audit", "unrelated-audit", "forbidden-open",
    "empty-read", "wrong-result-id", "malformed-stream", "engine-exit",
]] + [("b", "valid"), ("b", "extra-grant")])
def test_real_stage_rejects_invalid_evidence(real_stage, fixture_id, case):
    work, root, env = real_stage
    env["EVIDENCE_CASE"] = case
    env["LADDER_TEST_FIXTURE"] = fixture_id
    events = transcript("Read", {"file_path": str(root / "skills/review-response-hygiene/SKILL.md")})
    if case == "denied":
        events[1]["message"]["content"][0].update(is_error=True, content="Permission denied")
    elif case == "missing-result":
        del events[1]
    elif case == "empty-read":
        events[1]["message"]["content"][0]["content"] = ""
    elif case == "wrong-result-id":
        events[1]["message"]["content"][0]["tool_use_id"] = "another-call"
    elif case == "engine-exit":
        env["SESSION_EXIT"] = "1"
    write_jsonl(work / "transcript.jsonl", events)
    if case == "malformed-stream":
        with (work / "transcript.jsonl").open("a") as stream:
            stream.write("truncated-json\n")
    result = subprocess.run(
        ["bash", str(LADDER / "l3-real-engines.sh"), fixture_id, "fixture/target", str(root)],
        env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == (0 if case == "valid" else 1), result.stdout + result.stderr
    assert ("RESULT  L3 (fixture/target) passed" if case == "valid"
            else "RESULT  L3 (fixture/target) FAILED") in result.stdout
    if case in {"denied", "missing-result", "implementer-only", "wrong-dispatch",
                "empty-read", "wrong-result-id", "malformed-stream", "engine-exit"}:
        failures = [line for line in result.stdout.splitlines() if line.startswith("FAIL ")]
        assert len(failures) == 1 and "real fixer Read" in failures[0], result.stdout + result.stderr


@pytest.mark.parametrize("failure", [None, "cli-exit", "missing-completion"])
def test_install_smoke_uses_the_session_gate(upgrade, failure):
    work, env = upgrade
    command = f"bash {work}/plugins/cache/pr-pipeline/0.1.0/skills/status/status.sh"
    events = transcript("Bash", {"command": command})
    if failure == "cli-exit":
        env["SESSION_EXIT"] = "1"
    elif failure == "missing-completion":
        events.pop()
    write_jsonl(work / "transcript.jsonl", events)
    result = subprocess.run(
        ["bash", str(LADDER / "l1-install-smoke.sh"), "fixture/plugin"],
        env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == (1 if failure else 0), result.stdout + result.stderr


@pytest.mark.parametrize("case", [
    "valid", "malformed-audit", "empty-audit", "missing-audit", "unrelated-audit", "forbidden-open",
])
def test_stub_stage_requires_valid_audit(stub_stage, case):
    _, root, env = stub_stage
    env["EVIDENCE_CASE"] = case
    result = subprocess.run(
        ["bash", str(LADDER / "l2-stubbed-tick.sh"), "fixture/target", str(root)],
        env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == (0 if case == "valid" else 1), result.stdout + result.stderr
    failures = [line for line in result.stdout.splitlines() if line.startswith("FAIL ")]
    assert len(failures) == (0 if case == "valid" else 1), result.stdout + result.stderr


def test_audit_gate_accepts_the_real_collectors_controller_record(tmp_path):
    checkout = tmp_path / "target"
    checkout.mkdir()
    log = tmp_path / "opened.jsonl"
    env = dict(os.environ, PYTHONPATH=str(LADDER / "stubs"), OPEN_AUDIT_LOG=str(log))
    # Invalid mode stops before startup/dispatch. It still executes the real
    # installed-style launcher and tick source under the actual audit hook.
    result = subprocess.run(
        ["bash", str(LADDER.parent / "skills/tick/tick.sh"), "--permission-mode", "invalid"],
        cwd=checkout, env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 1 and "--permission-mode" in result.stderr
    checked = subprocess.run(
        [sys.executable, str(LADDER / "evidence.py"), "audit", str(log), str(checkout), str(LADDER.parent)],
        text=True, capture_output=True, timeout=10,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


# --- Helpers ---


def executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)


def git(*args, env):
    return subprocess.run(["git", *args], env=env, check=True, capture_output=True, text=True)


def write_jsonl(path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def transcript(name, inputs):
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "call-1", "name": name, "input": inputs},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "call-1", "is_error": False,
             "content": "PR PIPELINE"},
        ]}},
        {"type": "result", "subtype": "success", "is_error": False},
    ]
