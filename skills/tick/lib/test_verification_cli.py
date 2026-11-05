"""One-shot tick CLI against offline GitHub and engine subprocess peers."""

import json
import os
from pathlib import Path
import shutil
import signal
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
    cfg.write_text('[operator]\nlogin="operator"\n[defaults]\nverify_tools=[]\n[repos."example/project"]\n')
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
elif args[:2] == ["issue", "comment"]:
    if s.get("fail_comment"):
        raise SystemExit(1)
    s.setdefault("comments", []).append(pathlib.Path(args[args.index("--body-file") + 1]).read_text())
    path.write_text(json.dumps(s))
    if s.get("crash_after_comment"):
        import signal
        os.kill(os.getppid(), signal.SIGKILL)
    print("https://github.com/example/project/issues/7#issuecomment-1"); raise SystemExit
elif args[:2] == ["issue", "edit"]:
    if s.get("fail_label"):
        raise SystemExit(1)
    if "--remove-label" in args:
        for label in args[args.index("--remove-label") + 1].split(","):
            if label in s["labels"]: s["labels"].remove(label)
    if "--add-label" in args:
        s["labels"].extend(args[args.index("--add-label") + 1].split(","))
    path.write_text(json.dumps(s)); payload = {}
elif args[:2] == ["pr", "create"]:
    s["title"] = args[args.index("--title") + 1]
    s["existing_pr"] = True
    s["body"] = pathlib.Path(args[args.index("--body-file") + 1]).read_text() if "--body-file" in args else args[args.index("--body") + 1]
    path.write_text(json.dumps(s)); print("https://github.com/example/project/pull/12"); raise SystemExit
elif args[:2] == ["pr", "view"]:
    payload = {"labels": [{"name": label} for label in s["pr_labels"]],
               "state": "OPEN", "body": s["body"], "headRefOid": s["head"]}
elif args[:2] == ["pr", "list"]:
    payload = [{"number": 12, "isCrossRepository": False}] if s.get("existing_pr") else []
elif args[:2] == ["pr", "comment"]:
    s.setdefault("comments", []).append(pathlib.Path(args[args.index("--body-file") + 1]).read_text())
    path.write_text(json.dumps(s))
    print("https://github.com/example/project/pull/12#issuecomment-1"); raise SystemExit
elif args[:2] == ["pr", "edit"]:
    if s.get("fail_pr_label") and "--add-label" in args:
        raise SystemExit(1)
    if "--body-file" in args:
        s["body"] = pathlib.Path(args[args.index("--body-file") + 1]).read_text()
    if "--remove-label" in args:
        for label in args[args.index("--remove-label") + 1].split(","):
            if label in s["pr_labels"]: s["pr_labels"].remove(label)
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
    write_peer(bin_dir / "osascript", "raise SystemExit(0)\n")
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
    cfg.write_text('[operator]\nlogin="operator"\n[defaults]\nverify_tools=["custom-check *"]\n[repos."example/project"]\n')
    run_tick(root, env)
    assert "Verification unconfigured:" in json.loads(state.read_text())["body"]



@pytest.mark.parametrize("permission_mode", ["auto", "dontAsk"])
def test_cli_implementer_scope_proof_grants(offline_tick, permission_mode):
    """Real dispatch supplies scope checks without granting arbitrary Git writes."""
    import re
    from fnmatch import fnmatchcase

    root, state, cfg, env = offline_tick
    cfg.write_text(
        '[operator]\nlogin="operator"\n[defaults]\n'
        f'permission_mode="{permission_mode}"\n'
        'verify_tools=["sh ./check.sh"]\n[repos."example/project"]\n'
    )
    run_tick(root, env)
    wt = root / ".claude" / "worktrees" / "issue-7"
    for _ in range(100):
        if (wt / "result.json").exists() and (wt / "result.json").stat().st_size:
            break
        time.sleep(0.02)
    argv = json.loads((wt / "engine-argv.json").read_text())
    assert argv[argv.index("--permission-mode") + 1] == permission_mode
    grants = re.findall(r"Bash\(([^)]+)\)", argv[argv.index("--allowedTools") + 1])
    for command in ("git status --porcelain", "git rev-parse HEAD",
                    "git merge-base --is-ancestor BASE HEAD"):
        assert any(fnmatchcase(command, grant) for grant in grants), (command, grants)
    for command in ("git init", "git reset --hard", "git push origin HEAD --force"):
        assert not any(fnmatchcase(command, grant) for grant in grants), (command, grants)
    assert [grant for grant in grants if grant.startswith("git push")] == ["git push origin HEAD"]
    assert not (wt / "skills").exists()


def test_cli_implementer_uses_installed_completion_guidance(offline_tick, tmp_path):
    """An installed tick binds its own guidance before launch in a bare target."""
    root, state, cfg, env = offline_tick
    install = copy_install(tmp_path)
    doc = install / "definition-of-done" / "SKILL.md"
    launch_receipt = tmp_path / "engine-launched"
    write_receipt_engine(env, launch_receipt)
    result = run_installed_tick(install, root, env, ["#7"])
    assert result.returncode == 0, result.stdout + result.stderr
    wt = root / ".claude" / "worktrees" / "issue-7"
    data = json.loads(state.read_text())
    wait_until(launch_receipt.exists)
    assert f"installed source `{doc}`" in launch_receipt.read_text()
    assert "invoke `pr-pipeline:definition-of-done`" in launch_receipt.read_text()
    assert not (wt / "skill-context.json").exists()
    assert not wt.with_name("issue-7-scrub").exists()
    assert data["labels"] == ["ai:implementing"]
    assert (wt / "dispatch.json").is_file()
    assert not (wt / "skills").exists()


@pytest.mark.parametrize("defect", ["missing", "empty"])
@pytest.mark.parametrize(
    ("target", "blocked_stages"),
    [(["#7"], ["implement"]), ([], ["review", "implement", "fix"])],
    ids=["targeted", "global"],
)
def test_cli_unreadable_installed_doc_blocks_dispatch_until_repaired(
    offline_tick, tmp_path, defect, target, blocked_stages,
):
    """One packaging defect holds DISPATCH, not the backlog.

    The install is checked once per tick before any worktree or label change,
    so repeated ticks leave every eligible item where it was. Repairing the
    install is the whole recovery: the next tick dispatches the same issue.
    """
    root, state, cfg, env = offline_tick
    install = copy_install(tmp_path)
    doc = install / "definition-of-done" / "SKILL.md"
    original = doc.read_text()
    launch_receipt = tmp_path / "engine-launched"
    write_receipt_engine(env, launch_receipt)
    if defect == "missing":
        doc.unlink()
    else:
        doc.write_text(" \n")

    for _ in range(2):
        result = run_installed_tick(install, root, env, target)
        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stderr.count(str(doc)) == 1, result.stderr
        assert (
            "Status: PARTIAL (dispatch blocked: installed guidance unreadable)"
            in result.stderr
        ), result.stderr

    data = json.loads(state.read_text())
    assert data["labels"] == ["ai:available"]
    assert "comments" not in data
    assert not launch_receipt.exists()
    assert not (root / ".claude" / "worktrees" / "issue-7").exists()
    calls = [json.loads(line) for line in Path(str(state) + ".calls").read_text().splitlines()]
    assert not [call for call in calls if call[:2] in (
        ["issue", "edit"], ["issue", "comment"], ["pr", "edit"], ["pr", "comment"],
    )], calls
    reports = [entry for entry in tick_log_entries(root) if entry["action"] == "skill_unresolved"]
    assert len(reports) == 2, "the failed paths are reported once per tick"
    for report in reports:
        assert report["pr"] is None
        assert report["details"]["blocked_stages"] == blocked_stages
        assert report["details"]["skills"] == ["definition-of-done"]
        assert report["details"]["probe_paths"] == [str(doc)]

    doc.write_text(original)
    result = run_installed_tick(install, root, env, target)
    assert result.returncode == 0, result.stdout + result.stderr
    wait_until(launch_receipt.exists)
    assert json.loads(state.read_text())["labels"] == ["ai:implementing"]
    assert (root / ".claude" / "worktrees" / "issue-7" / "dispatch.json").is_file()


def test_cli_blocked_dispatch_still_harvests_finished_work(offline_tick, tmp_path):
    """HARVEST does not depend on the guidance that only DISPATCH needs."""
    root, state, cfg, env = offline_tick
    install = copy_install(tmp_path)
    doc = install / "definition-of-done" / "SKILL.md"
    first = run_installed_tick(install, root, env, ["#7"])
    assert first.returncode == 0, first.stdout + first.stderr
    wt = root / ".claude" / "worktrees" / "issue-7"
    wait_until(lambda: (wt / "result.json").exists() and (wt / "result.json").stat().st_size)
    doc.unlink()

    result = run_installed_tick(install, root, env, ["#7"])

    assert result.returncode == 2, result.stdout + result.stderr
    data = json.loads(state.read_text())
    assert data.get("existing_pr") is True, "the finished engine was not harvested"
    assert "ai:implementing" not in data["labels"]
    assert not (wt / "dispatch.json").exists()
    actions = [entry["action"] for entry in tick_log_entries(root)]
    assert "harvest" in actions
    assert actions.index("skill_unresolved") > actions.index("harvest")


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


@pytest.mark.parametrize("failure", ["", "comment", "label", "hold"])
def test_cli_implementer_escalation_explains_denials_and_retries(offline_tick, failure):
    root, state, cfg, env = offline_tick
    cfg.write_text('[operator]\nlogin="operator"\n[defaults]\npermission_mode="dontAsk"\nverify_tools=["sh ./check.sh"]\n[repos."example/project"]\n')
    write_peer(Path(env["PATH"].split(os.pathsep)[0]) / "claude", '''
import json, pathlib, sys
pathlib.Path("engine-argv.json").write_text(json.dumps(sys.argv[1:]))
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False,
    "result": "Please grant Bash permission so I can run the tests.",
    "permission_denials": [
        {"tool_name": "Bash", "tool_input": {"command": "python3 -m pytest tests/"}},
        {"tool_name": "Bash", "tool_input": {"command": "python3 -m unittest tests | head"}}
    ]}))
''')
    run_tick(root, env)
    wt_root = root / ".claude" / "worktrees"
    wt = wt_root / "issue-7"
    for _ in range(100):
        if (wt / "result.json").exists() and (wt / "result.json").stat().st_size:
            break
        time.sleep(0.02)
    assert (wt / "result.json").stat().st_size
    marker = json.loads((wt / "dispatch.json").read_text())
    argv = json.loads((wt / "engine-argv.json").read_text())
    # Harvest must use the launch snapshot, not this later configuration.
    cfg.write_text('[operator]\nlogin="operator"\n[defaults]\npermission_mode="auto"\nverify_tools=["changed-check *"]\n[repos."example/project"]\n')
    if failure:
        data = json.loads(state.read_text())
        data["fail_" + ("label" if failure == "hold" else failure)] = True
        state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    if failure:
        assert data["labels"] == ["ai:implementing"]
        assert (wt / "dispatch.json").exists()
        assert (wt / "result.json").exists()
        assert len(data.get("comments", [])) == (0 if failure == "comment" else 1)
        if failure in ("label", "hold"):
            body = data["comments"][0]
            published_path = Path(next(
                line for line in body.splitlines() if "/.harvest-evidence/" in line
            ))
            expected_result = (wt / "result.json").read_text()
            assert (published_path / "result.json").read_text() == expected_result
            if failure == "hold":
                data["labels"].append("ai:blocked")
                state.write_text(json.dumps(data))
            # Keep the label write failing. A late hold must also retain the
            # published evidence when releasing the in-flight label fails.
            run_tick(root, env)
            data = json.loads(state.read_text())
            assert data["comments"] == [body]
            expected_labels = ["ai:implementing"]
            if failure == "hold":
                expected_labels.append("ai:blocked")
            assert data["labels"] == expected_labels
            assert (wt / "dispatch.json").exists()
            assert (wt / "result.json").read_text() == expected_result
            assert (published_path / "result.json").read_text() == expected_result
            assert list((wt_root / ".harvest-evidence").iterdir()) == [published_path]
        data["fail_" + ("label" if failure == "hold" else failure)] = False
        state.write_text(json.dumps(data))
        run_tick(root, env)
        data = json.loads(state.read_text())
    assert len(data.get("comments", [])) == 1, data
    body = data["comments"][0]
    assert "ended without a completion sentinel" in body
    assert "Please grant Bash permission" in body
    assert "python3 -m pytest tests/" in body
    assert "python3 -m unittest tests | head" in body
    assert "dontAsk" in body and "sh ./check.sh" in body
    assert "changed-check" not in body
    assert marker["permission_mode"] == argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert marker["verify_tools"] == ["sh ./check.sh"]
    assert "Bash(sh ./check.sh)" in argv[argv.index("--allowedTools") + 1]
    archives = list((wt_root / ".harvest-evidence").iterdir())
    assert len(archives) == 1
    assert str(archives[0]) in body
    assert (archives[0] / "result.json").exists()
    assert (archives[0] / "scratch" / "engine-argv.json").exists()
    assert data["labels"] == (["ai:blocked"] if failure == "hold" else ["ai:needs-human-decision"])
    assert not wt.exists()
    run_tick(root, env)
    assert json.loads(state.read_text())["comments"] == [body]


@pytest.mark.parametrize("recovery", ["repost", "cap", "cap_hold"])
def test_cli_published_evidence_survives_unreceipted_post(offline_tick, recovery):
    from pipeline import MAX_DISPATCH_ATTEMPTS

    root, state, cfg, env = offline_tick
    write_peer(Path(env["PATH"].split(os.pathsep)[0]) / "claude", '''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False,
                  "result": "Please grant Bash permission."}))
''')
    run_tick(root, env)
    wt_root = root / ".claude" / "worktrees"
    wt = wt_root / "issue-7"
    for _ in range(100):
        if (wt / "result.json").exists() and (wt / "result.json").stat().st_size:
            break
        time.sleep(0.02)
    expected_result = (wt / "result.json").read_text()
    assert expected_result
    data = json.loads(state.read_text())
    data["crash_after_comment"] = True
    state.write_text(json.dumps(data))
    cli = Path(__file__).resolve().parent.parent / "tick.sh"
    crashed = subprocess.run(["bash", str(cli), "#7"], cwd=root, env=env,
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == -signal.SIGKILL
    data = json.loads(state.read_text())
    assert len(data["comments"]) == 1
    body = data["comments"][0]
    published = Path(next(line for line in body.splitlines() if "/.harvest-evidence/" in line))
    assert (published / "result.json").read_text() == expected_result
    assert not list((wt_root / ".receipts").glob("*fix-comment*.json"))
    data.update(crash_after_comment=False, fail_comment=True)
    state.write_text(json.dumps(data))

    def assert_pending_evidence():
        assert (published / "result.json").read_text() == expected_result
        assert list(published.parent.iterdir()) == [published]
        assert (wt / "result.json").read_text() == expected_result
        assert (wt / "dispatch.json").is_file()
        assert (wt_root / ".terminal" / "issue-7.json").is_file()

    run_tick(root, env)
    assert_pending_evidence()
    data = json.loads(state.read_text())
    assert data["comments"] == [body]
    assert data["labels"] == ["ai:implementing"]
    counter = wt_root / "issue-7-attempts"
    assert int(counter.read_text()) > 0
    if recovery != "repost":
        # Reach the cap through real failed ticks, then fail its terminal write.
        for _ in range(MAX_DISPATCH_ATTEMPTS):
            if int(counter.read_text()) >= MAX_DISPATCH_ATTEMPTS:
                break
            run_tick(root, env)
            assert_pending_evidence()
        assert int(counter.read_text()) == MAX_DISPATCH_ATTEMPTS
        data["fail_label"] = True
        state.write_text(json.dumps(data))
        run_tick(root, env)
        assert_pending_evidence()
        intent = json.loads((wt_root / ".terminal" / "issue-7.json").read_text())
        assert intent["kind"] == "cap"
        assert int(counter.read_text()) == MAX_DISPATCH_ATTEMPTS
        data = json.loads(state.read_text())
        assert data["comments"] == [body]
        assert data["labels"] == ["ai:implementing"]
        if recovery == "cap_hold":
            data["labels"].append("ai:blocked")
            state.write_text(json.dumps(data))
        run_tick(root, env)
        assert_pending_evidence()
        assert int(counter.read_text()) == MAX_DISPATCH_ATTEMPTS
        retried = json.loads(state.read_text())
        assert retried["comments"] == [body]
        assert retried["labels"] == data["labels"]
    # Ordinary recovery may duplicate the unreceipted post. At the cap, a
    # failed explanation must still allow the terminal label/hold release.
    data = json.loads(state.read_text())
    data.update(fail_comment=recovery == "cap_hold", fail_label=False)
    state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    assert data["comments"][0] == body
    if recovery == "repost":
        assert data["comments"] == [body, body]
    elif recovery == "cap":
        assert len(data["comments"]) == 2
        assert "supersedes" in data["comments"][1]
    else:
        assert data["comments"] == [body]
    assert data["labels"] == (["ai:blocked"] if recovery == "cap_hold" else ["ai:needs-human-decision"])
    assert (published / "result.json").read_text() == expected_result
    assert (published / "scratch" / "result.json").read_text() == expected_result
    assert list(published.parent.iterdir()) == [published]
    assert not wt.exists()
    assert not counter.exists()
    assert not (wt_root / ".terminal" / "issue-7.json").exists()
    assert not list((wt_root / ".receipts").glob("*.json"))
    run_tick(root, env)
    assert json.loads(state.read_text())["comments"] == data["comments"]


@pytest.mark.parametrize("crash", ["after_mkdir", "torn_copy"])
def test_cli_interrupted_archive_is_completed_before_publication(
    offline_tick, tmp_path, crash,
):
    root, state, cfg, env = offline_tick
    write_peer(Path(env["PATH"].split(os.pathsep)[0]) / "claude", '''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False,
                  "result": "Please grant Bash permission."}))
''')
    run_tick(root, env)
    wt_root = root / ".claude" / "worktrees"
    wt = wt_root / "issue-7"
    for _ in range(100):
        if (wt / "result.json").exists() and (wt / "result.json").stat().st_size:
            break
        time.sleep(0.02)
    expected_result = (wt / "result.json").read_text()
    expected_marker = (wt / "dispatch.json").read_text()
    assert expected_result
    # Kill the real tick inside archive preparation, after the intent is saved.
    crash_python = tmp_path / "crash-python"
    write_peer(crash_python, CRASH_DURING_ARCHIVE)
    cli = Path(__file__).resolve().parent.parent / "tick.sh"
    crashed = subprocess.run(
        ["bash", str(cli), "#7"], cwd=root,
        env={**env, "PR_PIPELINE_PYTHON": str(crash_python), "CRASH_ARCHIVE": crash},
        capture_output=True, text=True, timeout=30,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stdout + crashed.stderr
    [archive] = (wt_root / ".harvest-evidence").iterdir()
    if crash == "after_mkdir":
        assert list(archive.iterdir()) == []
    else:
        # The torn copy never completed, so the archive is not marked prepared.
        assert not (archive / ".prepared").exists()
    assert (wt_root / ".terminal" / "issue-7.json").is_file()
    data = json.loads(state.read_text())
    assert not data.get("comments")
    data["fail_label"] = True
    state.write_text(json.dumps(data))

    run_tick(root, env)
    data = json.loads(state.read_text())
    assert len(data["comments"]) == 1
    body = data["comments"][0]
    published = Path(next(line for line in body.splitlines() if "/.harvest-evidence/" in line))
    assert published == archive
    assert (published / "result.json").read_text() == expected_result
    assert (published / "dispatch.json").read_text() == expected_marker
    assert data["labels"] == ["ai:implementing"]
    assert (wt / "result.json").read_text() == expected_result

    data["fail_label"] = False
    state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    assert data["comments"] == [body]
    assert data["labels"] == ["ai:needs-human-decision"]
    assert (published / "result.json").read_text() == expected_result
    assert (published / "scratch" / "result.json").read_text() == expected_result
    assert list(published.parent.iterdir()) == [published]
    assert not wt.exists()


@pytest.mark.parametrize("failure", ["torn_copy", "copy_enospc"])
def test_cli_archive_repair_keeps_published_contents(offline_tick, tmp_path, failure):
    root, state, cfg, env = offline_tick
    write_peer(Path(env["PATH"].split(os.pathsep)[0]) / "claude", '''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False,
                  "result": "Please grant Bash permission."}))
''')
    run_tick(root, env)
    wt_root = root / ".claude" / "worktrees"
    wt = wt_root / "issue-7"
    for _ in range(100):
        if (wt / "result.json").exists() and (wt / "result.json").stat().st_size:
            break
        time.sleep(0.02)
    expected_result = (wt / "result.json").read_text()
    assert expected_result
    data = json.loads(state.read_text())
    data["fail_label"] = True
    state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    [body] = data["comments"]
    published = Path(next(line for line in body.splitlines() if "/.harvest-evidence/" in line))
    assert (published / "result.json").read_text() == expected_result
    # Main's held-result path and earlier heads leave complete, published
    # archives without the marker; recovery must repair them non-destructively.
    (published / ".prepared").unlink()

    crash_python = tmp_path / "crash-python"
    write_peer(crash_python, CRASH_DURING_ARCHIVE)
    cli = Path(__file__).resolve().parent.parent / "tick.sh"
    repaired = subprocess.run(
        ["bash", str(cli), "#7"], cwd=root,
        env={**env, "PR_PIPELINE_PYTHON": str(crash_python), "CRASH_ARCHIVE": failure},
        capture_output=True, text=True, timeout=30,
    )
    expected_code = -signal.SIGKILL if failure == "torn_copy" else 0
    assert repaired.returncode == expected_code, repaired.stdout + repaired.stderr
    assert (published / "result.json").read_text() == expected_result
    assert not (published / ".prepared").exists()
    data = json.loads(state.read_text())
    assert data["comments"] == [body]
    assert data["labels"] == ["ai:implementing"]
    assert (wt / "result.json").read_text() == expected_result

    data["fail_label"] = False
    state.write_text(json.dumps(data))
    run_tick(root, env)
    data = json.loads(state.read_text())
    assert data["comments"] == [body]
    assert data["labels"] == ["ai:needs-human-decision"]
    assert (published / "result.json").read_text() == expected_result
    assert (published / "scratch" / "result.json").read_text() == expected_result
    assert list(published.parent.iterdir()) == [published]
    assert not wt.exists()


# --- Helpers ---


CRASH_DURING_ARCHIVE = '''
import errno, os, runpy, shutil, signal, sys
if sys.argv[1:2] == ["-c"]:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
mode = os.environ.get("CRASH_ARCHIVE")

def crash():
    os.kill(os.getpid(), signal.SIGKILL)

if mode == "after_mkdir":
    real_mkdir = os.mkdir

    def mkdir(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        if "/.harvest-evidence/issue-7-" in os.fspath(path):
            crash()

    os.mkdir = mkdir
elif mode in ("torn_copy", "copy_enospc"):
    real_copy2 = shutil.copy2

    # Match the destination by name so a temporary sibling is torn as well.
    def copy2(src, dst, *args, **kwargs):
        dst = os.fspath(dst)
        if "/.harvest-evidence/" in dst and "result.json" in os.path.basename(dst):
            with open(src, "rb") as fh:
                data = fh.read()
            with open(dst, "wb") as fh:
                fh.write(data[: len(data) // 2])
            if mode == "torn_copy":
                crash()
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy2(src, dst, *args, **kwargs)

    shutil.copy2 = copy2
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path[0] = os.path.dirname(os.path.abspath(script))
runpy.run_path(script, run_name="__main__")
'''


def copy_install(tmp_path):
    """Copy the pipeline the way a plugin cache holds it; return its skills root."""
    install = tmp_path / "plugin-cache" / "pr-pipeline" / "skills"
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source, install, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(source.parent / ".claude-plugin", install.parent / ".claude-plugin")
    return install


def write_receipt_engine(env, receipt):
    write_peer(Path(env["PATH"].split(os.pathsep)[0]) / "claude", f'''
import json, pathlib, sys
pathlib.Path({str(receipt)!r}).write_text(sys.stdin.read())
print(json.dumps({{"type": "result", "is_error": False,
                  "result": "NEEDS_HUMAN_DECISION"}}))
''')


def run_installed_tick(install, root, env, args):
    return subprocess.run(
        ["bash", str(install / "tick" / "tick.sh"), *args],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )


def tick_log_entries(root):
    return [
        json.loads(line)
        for log in sorted((root / ".claude" / "worktrees" / ".tick-log").glob("*.jsonl"))
        for line in log.read_text().splitlines()
    ]


def wait_until(predicate):
    for _ in range(250):
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def write_peer(path, code):
    path.write_text(f"#!{sys.executable}\n" + code)
    path.chmod(0o755)


def run_tick(root, env):
    cli = Path(__file__).resolve().parent.parent / "tick.sh"
    result = subprocess.run(["bash", str(cli), "#7"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
