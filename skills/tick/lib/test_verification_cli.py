"""One-shot tick CLI against offline GitHub and engine subprocess peers."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


@pytest.fixture
def offline_tick(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_git = shutil.which("git")
    for args in (("init", "-b", "main"), ("config", "user.name", "Test"),
                 ("config", "user.email", "test@example.invalid"),
                 ("commit", "--allow-empty", "-m", "Initial"),
                 ("remote", "add", "origin", "https://github.com/example/project")):
        subprocess.run([real_git, *args], cwd=root, check=True, capture_output=True)
    head = subprocess.check_output([real_git, "rev-parse", "HEAD"], cwd=root, text=True).strip()
    subprocess.run([real_git, "update-ref", "refs/remotes/origin/main", head], cwd=root, check=True)
    cfg = tmp_path / "operator.toml"
    cfg.write_text('[operator]\nlogin="operator"\n[defaults]\nverify_tools=[]\n')
    state = tmp_path / "github.json"
    state.write_text(json.dumps({"labels": ["ai:available"], "pr_labels": [], "body": "", "head": head}))
    write_peer(bin_dir / "git", f'''
import os, sys
args = sys.argv[1:]
if "fetch" in args:
    raise SystemExit(0)  # offline origin/main is already seeded
os.execv({real_git!r}, [{real_git!r}, *args])
''')
    write_peer(bin_dir / "gh", '''
import json, os, pathlib, sys
args = sys.argv[1:]
path = pathlib.Path(os.environ["OFFLINE_GITHUB"])
s = json.loads(path.read_text())
with open(str(path) + ".calls", "a") as out:
    out.write(json.dumps(args) + "\\n")
fields = args[args.index("--json") + 1] if "--json" in args else ""
labels = [{"name": label} for label in s["labels"]]
issue = {"number": 7, "title": "Work", "author": {"login": "operator"},
         "state": "OPEN", "labels": labels, "body": "Implement the task", "closedByPullRequestsReferences": []}
if args[:2] == ["repo", "view"] and fields == "defaultBranchRef":
    payload = {"defaultBranchRef": {"name": "main"}}
elif args[:2] == ["issue", "view"]:
    payload = issue
elif args[:2] == ["issue", "list"]:
    label = args[args.index("--label") + 1] if "--label" in args else ""
    payload = [issue] if label in s["labels"] else []
elif args[:2] == ["issue", "edit"]:
    if "--remove-label" in args:
        for label in args[args.index("--remove-label") + 1].split(","):
            if label in s["labels"]: s["labels"].remove(label)
    if "--add-label" in args:
        s["labels"].extend(args[args.index("--add-label") + 1].split(","))
    path.write_text(json.dumps(s)); payload = {}
elif args[:2] == ["pr", "create"]:
    s["body"] = pathlib.Path(args[args.index("--body-file") + 1]).read_text() if "--body-file" in args else args[args.index("--body") + 1]
    path.write_text(json.dumps(s)); print("https://github.com/example/project/pull/12"); raise SystemExit
elif args[:2] == ["pr", "view"]:
    payload = {"labels": [{"name": label} for label in s["pr_labels"]],
               "state": "OPEN", "body": s["body"], "headRefOid": s["head"]}
elif args[:2] == ["pr", "list"]:
    payload = [{"number": 12, "isCrossRepository": False}] if s.get("existing_pr") else []
elif args[:2] == ["pr", "edit"]:
    if "--body-file" in args:
        s["body"] = pathlib.Path(args[args.index("--body-file") + 1]).read_text()
    if "--add-label" in args:
        s["pr_labels"].extend(args[args.index("--add-label") + 1].split(","))
    path.write_text(json.dumps(s))
    payload = {}
elif args[0] == "api" and "/labels?" in args[1]:
    payload = [[{"name": "ai:" + name} for name in ("available", "blocked", "changes-requested", "fixing", "implemented", "implementing", "max-iterations", "needs-human-decision", "ready-for-human", "reviewing", "revised")]]
elif args[:2] == ["api", "user"]:
    payload = {"login": "operator"}
else:
    payload = []
if "-q" in args:
    query = args[args.index("-q") + 1]
    if query == ".[].number": print("\\n".join(str(i["number"]) for i in payload))
    elif query == ".labels[].name": print("\\n".join(i["name"] for i in payload["labels"]))
    elif isinstance(payload, dict): print(payload.get(query.lstrip("."), ""))
    else: print("")
else:
    print(json.dumps(payload))
''')
    write_peer(bin_dir / "codex", 'print("codex-cli 0.100.0")\n')
    write_peer(bin_dir / "claude", '''
import json, pathlib, sys
pathlib.Path("engine-argv.json").write_text(json.dumps(sys.argv[1:]))
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False,
                  "result": "VERIFICATION_UNCONFIGURED_WAIVER\\nIMPLEMENTATION_COMPLETE"}))
''')
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "PR_PIPELINE_CONFIG": str(cfg), "OFFLINE_GITHUB": str(state),
           "PR_PIPELINE_PYTHON": sys.executable,
           "PIPELINE_TRUSTED_AUTHOR": "operator"}
    env.pop("PIPELINE_REPO", None)
    return root, state, cfg, env


def test_cli_dispatch_and_harvest_empty_verification(offline_tick):
    root, state, cfg, env = offline_tick
    run_tick(root, env)
    wt = root / ".claude" / "worktrees" / "issue-7"
    assert (wt / "dispatch.json").exists()
    for _ in range(100):
        if (wt / "result.json").exists() and (wt / "result.json").stat().st_size:
            break
        time.sleep(0.02)
    marker = json.loads((wt / "dispatch.json").read_text())
    # The assertion fails on the baseline after exercising the actual CLI.
    assert marker.get("verification_unconfigured") is True
    assert "VERIFICATION_UNCONFIGURED_WAIVER" in (wt / "prompt.txt").read_text()
    argv = json.loads((wt / "engine-argv.json").read_text())
    assert "pytest" not in argv[argv.index("--allowedTools") + 1]
    # The dispatch snapshot survives a later operator configuration change.
    cfg.write_text('[operator]\nlogin="operator"\n[defaults]\nverify_tools=["custom-check *"]\n')
    run_tick(root, env)
    assert "Verification unconfigured:" in json.loads(state.read_text())["body"]



def test_cli_malformed_verification_is_a_startup_error(offline_tick):
    root, state, cfg, env = offline_tick
    cfg.write_text('[operator]\nlogin="operator"\n[defaults]\nverify_tools=7\n')
    cli = Path(__file__).resolve().parent.parent / "tick.sh"
    result = subprocess.run(["bash", str(cli), "#7"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 3
    assert "verify_tools" in result.stderr
    assert not list((root / ".claude" / "worktrees").glob("issue-*/dispatch.json"))
    assert json.loads(state.read_text())["labels"] == ["ai:available"]


@pytest.mark.parametrize("existing", [False, True])
def test_cli_warning_survives_existing_pr_and_unclosed_html(offline_tick, existing):
    from conftest import REVISED_PR_BODY
    from pipeline import VERIFICATION_WARNING

    root, state, cfg, env = offline_tick
    run_tick(root, env)
    wt = root / ".claude" / "worktrees" / "issue-7"
    for _ in range(100):
        if (wt / "result.json").exists() and (wt / "result.json").stat().st_size:
            break
        time.sleep(0.02)
    assert (wt / "result.json").exists()
    body = REVISED_PR_BODY + "\n<!--"
    if existing:
        data = json.loads(state.read_text())
        data.update(existing_pr=True, body=body, pr_labels=["ai:changes-requested"])
        state.write_text(json.dumps(data))
    else:
        (wt / ".pr-body.md").write_text("Title\n" + body)
    run_tick(root, env)
    data = json.loads(state.read_text())
    assert data["body"].startswith(VERIFICATION_WARNING + "\n\n")
    assert body.strip() in data["body"]
    assert data["labels"] == []
    assert data["pr_labels"] == (["ai:changes-requested"] if existing else ["ai:implemented"])
    assert not (wt / "dispatch.json").exists()


# --- Helpers ---


def write_peer(path, code):
    path.write_text(f"#!{sys.executable}\n" + code)
    path.chmod(0o755)


def run_tick(root, env):
    cli = Path(__file__).resolve().parent.parent / "tick.sh"
    result = subprocess.run(["bash", str(cli), "#7"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
