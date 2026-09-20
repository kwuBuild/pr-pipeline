"""Canonical target resolution against real, offline Git repositories."""

from pathlib import Path
import json
import os
import shlex
import subprocess
import sys
from unittest import mock

import pipeline
import pytest

pytestmark = pytest.mark.real_target


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    return tmp_path


def test_resolves_an_unconfigured_foreign_repository(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "upstream", "git@github.com:elsewhere/project.git"],
        cwd=tmp_path, check=True,
    )
    target = pipeline.resolve_target(tmp_path)
    assert target.repo == "elsewhere/project"
    assert target.remote == "upstream"


@pytest.mark.parametrize("url", [
    "https://github.com/elsewhere/project.git",
    "ssh://git@github.com/elsewhere/project.git",
    "ssh://git@github.com:22/elsewhere/project.git",
    "git://github.com/elsewhere/project",
    "git@github.com:elsewhere/project.git",
])
def test_supported_github_remote_urls(repository, url):
    git(repository, "remote", "add", "upstream", url)
    assert pipeline.resolve_target(repository).repo == "elsewhere/project"


def test_dot_github_repository_is_a_valid_target(repository):
    git(repository, "remote", "add", "upstream", "https://github.com/elsewhere/.github.git")
    assert pipeline.resolve_target(repository).repo == "elsewhere/.github"


def test_linked_worktree_uses_the_main_checkout_remote_identity(repository):
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "user.name", "Test")
    git(repository, "commit", "--allow-empty", "-m", "Initial")
    git(repository, "remote", "add", "upstream", "https://github.com/elsewhere/project")
    linked = repository / "linked"
    git(repository, "worktree", "add", "-b", "linked", str(linked))
    git(repository, "config", "extensions.worktreeConfig", "true")
    git(linked, "config", "--worktree", "url.https://github.com/other/repo.insteadOf",
        "https://github.com/elsewhere/project")
    assert pipeline.resolve_target(linked) == pipeline.resolve_target(repository)


def test_override_selects_the_matching_remote(repository, monkeypatch):
    git(repository, "remote", "add", "origin", "https://github.com/fork/project")
    git(repository, "remote", "add", "upstream", "git@github.com:elsewhere/project.git")
    monkeypatch.setenv("PIPELINE_REPO", "elsewhere/project")
    assert pipeline.resolve_target(repository) == pipeline.ResolvedTarget("elsewhere/project", "upstream")


@pytest.mark.parametrize("remotes,override,diagnostic", [
    ([], "", "no remote"),
    ([("origin", "one/repo"), ("upstream", "two/repo")], "", "origin, upstream"),
    ([("origin", "one/repo"), ("backup", "one/repo")], "one/repo", "backup, origin"),
    ([("upstream", "one/repo")], "two/repo", "upstream"),
    ([("upstream", "one/repo")], "--bad", "PIPELINE_REPO"),
    ([("upstream", "one/repo")], "one/repo;echo", "PIPELINE_REPO"),
])
def test_refuses_absent_or_ambiguous_target(repository, monkeypatch, remotes, override, diagnostic):
    for remote, repo in remotes:
        git(repository, "remote", "add", remote, f"https://github.com/{repo}")
    if override:
        monkeypatch.setenv("PIPELINE_REPO", override)
    with pytest.raises(ValueError, match=diagnostic):
        pipeline.resolve_target(repository)


@pytest.mark.parametrize("remote", ["a;b", "a$b", "a(b)", "a/b", "a..b", "a.lock"])
def test_remote_names_cannot_widen_a_push_grant(repository, remote):
    git(repository, "config", f"remote.{remote}.url", "https://github.com/one/repo")
    with pytest.raises(ValueError, match="remote name"):
        pipeline.resolve_target(repository)


@pytest.mark.parametrize("push_urls", [
    ["https://github.com/other/repo"],
    ["https://github.com/one/repo", "https://github.com/other/repo"],
])
def test_fetch_and_every_push_destination_must_agree(repository, push_urls):
    git(repository, "remote", "add", "upstream", "https://github.com/one/repo")
    for url in push_urls:
        git(repository, "config", "--add", "remote.upstream.pushurl", url)
    with pytest.raises(ValueError, match="push destination"):
        pipeline.resolve_target(repository)


@pytest.mark.parametrize("url", [
    "https://gitlab.com/one/repo", "/tmp/one/repo", "https://github.com/one/repo/extra",
    "https://github.com/one/repo?query", "https://github.com.evil/one/repo",
])
def test_unsupported_remote_urls_fail_closed(repository, url):
    git(repository, "remote", "add", "upstream", url)
    with pytest.raises(ValueError, match="upstream"):
        pipeline.resolve_target(repository)


def test_main_refuses_override_without_matching_remote_before_github(repository, monkeypatch, capsys):
    import tick

    git(repository, "remote", "add", "upstream", "https://github.com/one/repo")
    monkeypatch.setenv("PIPELINE_REPO", "elsewhere/project")
    monkeypatch.setattr(tick, "repo_root", lambda: str(repository))
    with mock.patch.object(tick, "gh") as gh, mock.patch.object(tick, "_run_tick_body", return_value=0) as body:
        assert tick.main() == 3
    gh.assert_not_called()
    body.assert_not_called()
    assert "upstream" in capsys.readouterr().err


@pytest.mark.real_gh_helper
def test_foreign_repository_runs_a_whole_tick_without_target_configuration(repository, monkeypatch):
    import tick

    git(repository, "remote", "add", "upstream", "https://github.com/elsewhere/project")
    monkeypatch.chdir(repository)
    real_run = subprocess.run
    github_calls = []

    def run(argv, **kwargs):
        if argv[0] == "gh":
            github_calls.append(argv)
            stdout = "" if "-q" in argv or "--jq" in argv else "[]"
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(tick.subprocess, "run", run)
    assert tick.main() == 0
    assert github_calls
    assert all(call[-2:] == ["--repo", "github.com/elsewhere/project"] for call in github_calls)
    assert tick.TARGET_REMOTE == "upstream"


def test_startup_selects_case_insensitive_repo_settings(repository, monkeypatch):
    import tick

    git(repository, "remote", "add", "upstream", "https://github.com/Elsewhere/Project")
    monkeypatch.chdir(repository)
    cfg = repository / "operator.toml"
    cfg.write_text('[operator]\nlogin="operator"\n[scan]\nrepos=["ELSEWHERE/PROJECT"]\n'
                   '[repos."elsewhere/project"]\npermission_mode="auto"\nverify_tools=["cargo test"]\n')
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    with mock.patch.object(tick, "_run_tick_body", return_value=0) as body:
        assert tick.main() == 0
    assert body.call_args.args[6] == "auto"
    assert tick._engine_verify_tools(tick.TRUSTED_HEAD_REPO) == ("Bash(cargo test)",)


@pytest.mark.parametrize("loop", [False, True])
def test_main_selects_repo_layer_and_rejects_outside_scan(repository, monkeypatch, capsys, loop):
    import tick

    git(repository, "remote", "add", "upstream", "https://github.com/elsewhere/project")
    monkeypatch.setenv("PIPELINE_REPO", "elsewhere/project")
    cfg = repository / "operator.toml"
    cfg.write_text('[operator]\nlogin="operator"\n[scan]\nrepos=["allowed/project"]\n')
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    monkeypatch.setattr(tick, "repo_root", lambda: str(repository))
    with (
        mock.patch.object(tick, "gh", return_value=subprocess.CompletedProcess([], 0, "[]")) as gh,
        mock.patch.object(tick, "_run_tick_body", return_value=0) as body,
        mock.patch.object(tick, "_start_wake_watcher", return_value=None),
        mock.patch.object(tick, "_wait_next_tick", return_value=(True, False)),
    ):
        assert tick.main(loop=loop) == 3
    gh.assert_not_called()
    body.assert_not_called()
    assert "[scan] repos" in capsys.readouterr().err


def test_repository_case_does_not_change_policy_or_trust(repository, monkeypatch):
    git(repository, "remote", "add", "upstream", "https://github.com/Elsewhere/Project")
    cfg = repository / "operator.toml"
    cfg.write_text('[scan]\nrepos=["ELSEWHERE/PROJECT"]\n'
                   '[repos."elsewhere/project"]\nverify_tools=["cargo test"]\n')
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    target = pipeline.resolve_target(repository)
    config = pipeline.resolve_config(target.repo)
    assert config.errors == ()
    assert config.verify_tools == ("cargo test",)
    assert pipeline.trusted_review_candidate({
        "author": {"login": "operator"},
        "headRepository": {"nameWithOwner": "ELSEWHERE/PROJECT"},
        "isCrossRepository": False,
    }, trusted_author="operator", trusted_repo=target.repo)


def test_duplicate_case_insensitive_repo_configuration_is_rejected(repository, monkeypatch):
    cfg = repository / "operator.toml"
    cfg.write_text('[repos."owner/project"]\nverify_tools=[]\n'
                   '[repos."Owner/Project"]\nverify_tools=["cargo test"]\n')
    monkeypatch.setenv("PR_PIPELINE_CONFIG", str(cfg))
    assert any("duplicate" in error for error in pipeline.resolve_config("owner/project").errors)


@pytest.mark.real_gh_helper
@pytest.mark.parametrize("command", [("pr", "list"), ("issue", "edit", "11", "--add-label", "ai:implementing")])
def test_gh_commands_always_use_canonical_target(monkeypatch, command):
    import tick

    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", "elsewhere/project")
    monkeypatch.setenv("GH_HOST", "other.example")
    with mock.patch("tick.subprocess.run") as run:
        tick.gh(*command)
    assert run.call_args.args[0] == ["gh", *command, "--repo", "github.com/elsewhere/project"]


@pytest.mark.real_gh_helper
def test_gh_preserves_explicit_foreign_issue_read(monkeypatch):
    import tick

    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", "elsewhere/project")
    with mock.patch("tick.subprocess.run") as run:
        tick.gh("issue", "view", "33", repo="other/contracts")
    assert run.call_args.args[0] == ["gh", "issue", "view", "33", "--repo", "github.com/other/contracts"]


@pytest.mark.real_gh_helper
@pytest.mark.parametrize("title", ["-Release", "--repo=other/project", "--repo"])
def test_pr_title_cannot_change_github_routing(monkeypatch, title):
    import tick

    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", "elsewhere/project")
    with mock.patch("tick.subprocess.run") as run:
        tick.gh("pr", "create", "--title", title)
    assert run.call_args.args[0] == ["gh", "pr", "create", "--title", title, "--repo", "github.com/elsewhere/project"]


@pytest.mark.parametrize("stage", ["implement", "fix"])
def test_dispatch_refuses_a_different_worktree_destination(repository, monkeypatch, capsys, stage):
    """The real dispatcher must not spend on an engine pointed at another repo."""
    import tick
    from conftest import fixer_gh_json

    transport_log = conditional_remote_repository(repository, monkeypatch)
    target = pipeline.resolve_target(repository)
    assert target.repo == "selected/project"
    monkeypatch.chdir(repository)
    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", target.repo)
    monkeypatch.setattr(tick, "TARGET_REMOTE", target.remote)
    monkeypatch.setattr(tick, "WT_ROOT", str(repository / "worktrees"))
    head = git(repository, "rev-parse", "HEAD")
    monkeypatch.setattr(tick, "gh_text", lambda *args: {
        "baseRefName": "main", "headRefOid": head, "headRefName": "feature",
    }[args[args.index("--json") + 1]])
    monkeypatch.setattr(tick, "gh_json", fixer_gh_json())
    launches = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        if argv[:2] == [sys.executable, tick._CLAUDE_STREAM_WRAPPER]:
            launches.append(kwargs["cwd"])
            return mock.Mock(pid=999999)
        return real_popen(argv, **kwargs)

    with (
        mock.patch("tick.subprocess.Popen", side_effect=popen),
        mock.patch.object(tick, "_publish_marker_or_revert", return_value=True),
        mock.patch.object(tick, "relabel") as relabel,
    ):
        if stage == "implement":
            tick._dispatch_one_implementer(issue_number=11, issue_title="Target")
        else:
            tick._dispatch_one_fixer(pr_number=44)

    assert launches == []
    relabel.assert_not_called()
    assert "target" in capsys.readouterr().err
    # Only the main checkout's safe pre-dispatch fetches reached transport.
    assert set(transport_log.read_text().splitlines()) == {"https://github.com/selected/project"}


def test_reviewer_harvest_refuses_worktree_destination_and_retains_custody(repository, monkeypatch):
    import tick

    transport_log = conditional_remote_repository(repository, monkeypatch)
    target = pipeline.resolve_target(repository)
    monkeypatch.chdir(repository)
    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", target.repo)
    monkeypatch.setattr(tick, "TARGET_REMOTE", target.remote)
    wt_root = repository / "worktrees"
    worktree = wt_root / "pr-44"
    git(repository, "worktree", "add", "--detach", str(worktree))
    head = git(repository, "rev-parse", "HEAD")
    marker = {"base_ref": "main", "base_sha": head, "head_sha": head,
              "dispatch_id": "review-44", "pre_label": "ai:implemented"}
    (worktree / "dispatch.json").write_text(json.dumps(marker))
    (worktree / "review.json").write_text("{}")
    monkeypatch.setattr(tick, "gh_json", lambda *args: [] if "comments" in args else {
        "baseRefName": "main", "headRefOid": head,
    })
    with mock.patch.object(pipeline, "harvest_review", side_effect=RuntimeError("verdict seam")) as verdict:
        tick._harvest_one_reviewer({"pr_number": 44}, str(wt_root))

    assert not transport_log.exists(), "harvest fetched from the wrong repository"
    verdict.assert_not_called()
    assert json.loads((worktree / "dispatch.json").read_text()) == marker
    assert (worktree / "review.json").read_text() == "{}"

    # Repairing the operator's configuration lets the same custody retry.
    git(repository, "config", "url.https://github.com/selected/project.insteadOf",
        "https://github.com/original/project")
    with mock.patch.object(pipeline, "harvest_review", side_effect=RuntimeError("verdict seam")) as verdict:
        tick._harvest_one_reviewer({"pr_number": 44}, str(wt_root))
    verdict.assert_called_once()
    assert transport_log.read_text().splitlines() == ["https://github.com/selected/project"]


@pytest.mark.parametrize("push_repo,allowed", [("ONE/PROJECT", True), ("other/project", False)])
def test_launch_validates_the_child_git_environment(repository, monkeypatch, push_repo, allowed):
    import tick

    git(repository, "remote", "add", "upstream", "https://github.com/one/project")
    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", "one/project")
    monkeypatch.setattr(tick, "TARGET_REMOTE", "upstream")
    child_env = {**os.environ, "GIT_CONFIG_COUNT": "1",
                 "GIT_CONFIG_KEY_0": "remote.upstream.pushurl",
                 "GIT_CONFIG_VALUE_0": f"https://github.com/{push_repo}"}
    launches = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        if argv[:2] == [sys.executable, tick._CLAUDE_STREAM_WRAPPER]:
            launches.append(kwargs)
            return mock.Mock(pid=999999)
        return real_popen(argv, **kwargs)

    with mock.patch("tick.subprocess.Popen", side_effect=popen):
        proc = tick._launch_claude_engine(
            engine_bin="claude", prompt="Target", wt_dir=str(repository),
            result_path=str(repository / "result.json"), env=child_env,
            push_permission=tick._implementer_push_permission(),
        )
    assert (proc is not None) is allowed
    assert bool(launches) is allowed
    if allowed:
        assert launches[0]["env"] == child_env
    else:
        assert not (repository / "result.json").exists()


def test_tick_entrypoint_refuses_worktree_destination(repository, monkeypatch):
    """Run the shipped launcher from a foreign repo, replacing only I/O peers."""
    transport_log = conditional_remote_repository(repository, monkeypatch)
    stub_bin = repository / "bin"
    stub_bin.mkdir()
    gh_stub = stub_bin / "gh"
    gh_stub.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with (Path(os.environ["TARGET_PROBE_ROOT"]) / "gh-calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:2] == ["api", "repos/selected/project/labels?per_page=100"]:
    print(os.environ["TARGET_LABEL_VOCABULARY"])
elif args[:2] == ["issue", "list"] and "ai:available" in args:
    print(json.dumps([{"number": 11, "title": "Target", "author": {"login": "test-owner"}, "labels": [{"name": "ai:available"}]}]))
elif args[:2] == ["issue", "view"] and "labels" in args:
    print("ai:available" if "-q" in args else json.dumps({"labels": [{"name": "ai:available"}]}))
elif args[:2] == ["issue", "view"] and "body" in args:
    print("Implement target")
else:
    print("" if "-q" in args else "[]")
''')
    gh_stub.chmod(0o755)
    for name in ("claude", "codex"):
        stub = stub_bin / name
        stub.write_text('#!/bin/sh\n'
                        'if [ "$1" = --version ]; then printf "codex-cli 0.142.5\\n"; exit 0; fi\n'
                        'printf "engine-called\\n" >> "$TARGET_PROBE_ROOT/engine-called"\n'
                        'exit 1\n')
        stub.chmod(0o755)
    cfg = repository / "operator.toml"
    cfg.write_text('[operator]\nlogin="test-owner"\n')
    env = {**os.environ, "PATH": f"{stub_bin}:{os.environ['PATH']}",
           "PR_PIPELINE_CONFIG": str(cfg), "PR_PIPELINE_PYTHON": sys.executable,
           "TARGET_PROBE_ROOT": str(repository),
           "TARGET_LABEL_VOCABULARY": json.dumps([
               [{"name": name} for name in sorted(pipeline.WORKFLOW_LABELS)],
           ])}
    result = subprocess.run(
        ["bash", str(Path(__file__).resolve().parents[1] / "tick.sh")],
        cwd=repository, env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "refusing engine launch: target validation failed" in result.stderr
    assert not (repository / "engine-called").exists()
    assert not (repository / ".claude/worktrees/issue-11/dispatch.json").exists()
    calls = [json.loads(line) for line in (repository / "gh-calls.jsonl").read_text().splitlines()]
    assert calls
    assert calls[0] == ["api", "repos/selected/project/labels?per_page=100",
                        "--hostname", "github.com", "--paginate", "--slurp"]
    assert all(call[-2:] == ["--repo", "github.com/selected/project"] for call in calls[1:])
    assert all(call[1] in {"list", "view"} for call in calls[1:])
    assert set(transport_log.read_text().splitlines()) == {"https://github.com/selected/project"}


def test_non_origin_implement_and_fix_pushes_through_real_dispatch(repository, monkeypatch):
    """Real worktrees, fetches, grants, prompts and pushes; no remote network."""
    import tick
    from conftest import fixer_gh_json

    bare = repository / "server.git"
    git(repository, "init", "--bare", str(bare))
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "user.name", "Test")
    git(repository, "checkout", "-b", "main")
    (repository / "README").write_text("base\n")
    git(repository, "add", "README")
    git(repository, "commit", "-m", "Initial")
    git(repository, "push", str(bare), "main")
    git(repository, "remote", "add", "upstream", "https://github.com/elsewhere/project")
    target = pipeline.resolve_target(repository)
    monkeypatch.setattr(tick, "TRUSTED_HEAD_REPO", target.repo)
    monkeypatch.setattr(tick, "TARGET_REMOTE", target.remote)
    monkeypatch.chdir(repository)
    wt_root = repository / "worktrees"
    monkeypatch.setattr(tick, "WT_ROOT", str(wt_root))

    # Translate only the test transport to a local bare server. Production
    # resolution above sees the real GitHub identity, with no GitHub request.
    transport_env = {**os.environ, "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{bare}.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/elsewhere/project"}
    calls = []
    launches = []
    real_launch = tick._launch_claude_engine
    real_popen = subprocess.Popen

    def engine_popen(argv, **kwargs):
        if argv[:2] == [sys.executable, tick._CLAUDE_STREAM_WRAPPER]:
            return mock.Mock(pid=999999)
        return real_popen(argv, **kwargs)

    def local_git(*args):
        calls.append(args)
        return subprocess.run(["git", *args], env=transport_env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def launch(**kwargs):
        with mock.patch("tick.subprocess.Popen", side_effect=engine_popen) as popen:
            proc = real_launch(**kwargs)
        assert proc is not None
        argv = popen.call_args.args[0]
        grant = kwargs["push_permission"]
        assert grant in argv[argv.index("--allowedTools") + 1]
        command = grant.removeprefix("Bash(").removesuffix(")")
        assert command in kwargs["prompt"]
        assert command.startswith("git push upstream HEAD")
        worktree = Path(kwargs["wt_dir"])
        (worktree / "README").write_text(f"revision {len(launches)}\n")
        git(worktree, "add", "README")
        git(worktree, "commit", "-m", "Revision")
        subprocess.run(shlex.split(command), cwd=worktree, env=transport_env,
                       check=True, capture_output=True)
        launches.append(git(worktree, "rev-parse", "HEAD"))
        return proc

    monkeypatch.setattr(tick, "git", local_git)
    monkeypatch.setattr(tick, "_launch_claude_engine", launch)
    monkeypatch.setattr(tick, "_publish_marker_or_revert", lambda **kwargs: True)
    monkeypatch.setattr(tick, "relabel", lambda *args, **kwargs: None)
    tick._dispatch_one_implementer(issue_number=11, issue_title="Target", tick_log=None)
    assert len(launches) == 1
    monkeypatch.setattr(tick, "gh_text", lambda *args: {
        "baseRefName": "main", "headRefOid": launches[0], "headRefName": "agent/issue-11",
    }[args[args.index("--json") + 1]])
    monkeypatch.setattr(tick, "gh_json", fixer_gh_json())
    tick._dispatch_one_fixer(pr_number=44, tick_log=None)
    assert len(launches) == 2
    assert launches[0] != launches[1]
    assert git(repository, "--git-dir", str(bare), "rev-parse", "agent/issue-11") == launches[1]
    assert all("origin" not in args for args in calls)


# --- Helpers ---


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def conditional_remote_repository(repository, monkeypatch):
    """Use real Git transport to two local servers with differing cwd identities.

    A main-checkout-only include rewrites original/project to selected/project.
    The HTTPS helper maps those identities to distinct offline bare repositories;
    resolution and Git's choice of destination remain production behavior.
    """
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "user.name", "Test")
    git(repository, "checkout", "-b", "main")
    git(repository, "commit", "--allow-empty", "-m", "Initial")
    git(repository, "branch", "feature")
    for name in ("selected", "original"):
        bare = repository / f"{name}.git"
        git(repository, "init", "--bare", str(bare))
        git(repository, "push", str(bare), "main", "feature")
    git(repository, "remote", "add", "upstream", "https://github.com/original/project")
    include = repository / "main-config"
    include.write_text('[url "https://github.com/selected/project"]\n'
                       '    insteadOf = https://github.com/original/project\n')
    main_gitdir = git(repository, "rev-parse", "--absolute-git-dir")
    git(repository, "config", f"includeIf.gitdir:{main_gitdir}.path", str(include))
    helper_dir = repository / "helpers"
    helper_dir.mkdir()
    transport_log = repository / "transport.log"
    remote_ext = Path(git(repository, "--exec-path")) / "git-remote-ext"
    remote_helper = helper_dir / "git-remote-https"
    remote_helper.write_text(
        '#!/bin/sh\n'
        f'printf "%s\\n" "$2" >> {shlex.quote(str(transport_log))}\n'
        'case "$2" in\n'
        f'  https://github.com/selected/project) bare={shlex.quote(str(repository / "selected.git"))} ;;\n'
        f'  https://github.com/original/project) bare={shlex.quote(str(repository / "original.git"))} ;;\n'
        '  *) exit 99 ;;\nesac\n'
        f'exec {shlex.quote(str(remote_ext))} "$1" "git %s $bare"\n'
    )
    remote_helper.chmod(0o755)
    monkeypatch.setenv("GIT_EXEC_PATH", str(helper_dir))
    return transport_log
