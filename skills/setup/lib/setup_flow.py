"""Deterministic primitives behind the ``pr-pipeline:setup`` skill.

The skill's agent runs these subcommands and relays each decision to the
operator; nothing here asks a question itself. Every subcommand prints one JSON
object on stdout.

Setup writes exactly three things, all operator-owned and outside every target
repository:

- ``~/.claude/pr-pipeline/config.toml`` — the only writer of that file;
- probe evidence beside it, under ``setup-probes/``;
- deny rules in the Claude user ``settings.json``, only when asked.

It reads a target repository through the GitHub API, never through a checkout,
so it has no working tree to write into. Label provisioning, the configuration
resolver and the engine launch definition are the tick's own, imported rather
than copied.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tick" / "lib"))

import labels  # noqa: E402
import pipeline  # noqa: E402
import tick  # noqa: E402

# --- The route probe ---

# The command the probe asks an engine to run. It has to be one nobody granted:
# not in the engine's base allowlist, not a verification grant, and not a
# command Claude Code runs without asking. `pwd` fails the last test on CLI
# 2.1.281 — it is treated as read-only — and so would any other read-only
# command, which is why the probe needs a negative control at all. `git init`
# writes, is benign in a throwaway directory, prints the directory it created
# (a path the prompt never mentions), and leaves `.git` behind for the harness
# to check. Its absence from the probe's allowlist is asserted by a test.
PROBE_COMMAND = "git init"

# The implementer's push grant names a remote. The probe runs outside every
# target checkout, so it names the conventional one; only the grant's shape
# matters to what is measured.
PROBE_REMOTE = "origin"

PROBE_TIMEOUT_SECONDS = 300

PROBE_PROMPT = (
    "This is a permission probe. Use the Bash tool to run exactly this command, "
    "once, in the current directory, with nothing added or changed:\n\n"
    f"{PROBE_COMMAND}\n\n"
    "Then reply with the command's complete output. If the command is denied "
    "or fails, reply with the exact denial or error text. Do not retry, and do "
    "not use any other tool."
)

# Probe outcomes. Only PASS lets setup accept `auto`, or an empty
# `verify_tools`, for a repository.
PASS = "pass"
# The control held and `auto` did not run the command: the classifier denied
# it, the route rejected the mode, or the run failed or timed out.
AUTO_FAILED = "auto_failed"
# The command ran under `dontAsk` too, so the allowlist is not enforced and the
# `auto` run measured nothing.
UNMEASURABLE = "unmeasurable"
# No measurement: the route could not launch, the control never attempted the
# command, or the allowlist already grants it.
INCONCLUSIVE = "inconclusive"

# The supported remedy when the operator's interactive settings sandbox Bash
# and auto-allow it there. `--settings` outranks user, project and local
# settings, so this turns the sandbox off for engines only. Measured on the
# #19 L3 host: with it, the control was denied and `auto` passed.
SANDBOX_OFF_SETTINGS = '{"sandbox":{"enabled":false}}'
SANDBOX_OFF_REMEDY = f"--engine-settings '{SANDBOX_OFF_SETTINGS}'"

_ALLOWLIST_ENTRY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\(([^()]*)\))?")


def probe_allowed_tools(verify_tools: Sequence[str]) -> str:
    """Return the ``--allowedTools`` value an implementer would launch with.

    Args:
        verify_tools: The verification grants proposed for the repository, as
            permission bodies.

    Returns:
        The engine base grants, those verification grants, and the
        implementer's exact push grant — composed by the tick itself.
    """
    return tick._engine_allowed_tools(
        tick._implementer_push_permission(PROBE_REMOTE),
        tuple(f"Bash({tool})" for tool in verify_tools),
    )


def probe_argv(
    engine_bin: str,
    permission_mode: str,
    allowed_tools: str,
    model: str,
    engine_settings: str = "",
) -> list[str]:
    """Return the probe's argv: dispatch's own, with nothing added."""
    return tick.claude_engine_argv(
        engine_bin,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        model=model,
        settings=engine_settings,
    )


def allowlist_grants(command: str, allowed_tools: str) -> str | None:
    """Return the entry in ``allowed_tools`` that grants ``command``, if any.

    A conservative model of Claude Code's Bash rule matching, used where a
    false "granted" is the safe error: a bare ``Bash`` grants everything,
    ``*`` matches any run of characters, a trailing ``:*`` is the legacy
    prefix form (wildcards before it still expand), and a trailing `` *`` also
    matches the bare command.

    Args:
        command: The shell command.
        allowed_tools: A space-separated ``--allowedTools`` value, or one rule.

    Returns:
        The granting entry as written, or ``None``.
    """
    for match in _ALLOWLIST_ENTRY_RE.finditer(allowed_tools):
        tool, body = match.group(1), match.group(2)
        if tool != "Bash":
            continue
        if body is None or _bash_body_matches(body.strip(), command):
            return match.group(0)
    return None


def _bash_body_matches(body: str, command: str) -> bool:
    def glob(text: str) -> str:
        return "".join(".*" if char == "*" else re.escape(char) for char in text)

    if body.endswith(":*"):
        return re.match(glob(body[:-2]), command, flags=re.DOTALL) is not None
    if re.fullmatch(glob(body), command, flags=re.DOTALL):
        return True
    return body.endswith(" *") and command == body[:-2]


def run_probe(
    repo: str,
    *,
    impl_engine: str,
    model: str,
    verify_tools: Sequence[str],
    engine_settings: str = "",
) -> dict[str, object]:
    """Measure whether ``auto`` approves an ungranted command on this route.

    The negative control runs first: the same command, allowlist and route
    under ``dontAsk``. Only a control that attempted the command and was denied
    makes the ``auto`` run meaningful.

    Args:
        repo: ``owner/name`` the result is for.
        impl_engine: ``"claude"`` or ``"claude-ds"``, as passed to the tick.
        model: The ``--model`` the tick will pass; empty for the operator's
            default (``deepseek-v4-pro[1m]`` for claude-ds, as at dispatch).
        verify_tools: The verification grants proposed for the repository.
        engine_settings: The ``engine_settings`` proposed for the repository,
            passed as ``--settings`` exactly as dispatch will pass it.

    Returns:
        A JSON-serialisable report with ``outcome`` and ``detail``.
    """
    allowed = probe_allowed_tools(verify_tools)
    report: dict[str, object] = {
        "repo": repo,
        "impl_engine": impl_engine,
        "model": model,
        "verify_tools": list(verify_tools),
        "engine_settings": engine_settings,
        "allowed_tools": allowed,
        "probe_command": PROBE_COMMAND,
    }
    granting = allowlist_grants(PROBE_COMMAND, allowed)
    if granting is not None:
        return {**report, "outcome": INCONCLUSIVE, "detail": (
            f"{granting} grants `{PROBE_COMMAND}`, so the probe would measure "
            "nothing; remove that grant or change PROBE_COMMAND"
        )}
    if not model and impl_engine == "claude-ds":
        model = "deepseek-v4-pro[1m]"
        report["model"] = model
    try:
        engine_bin, extra_env = tick._resolve_engine(impl_engine, model=model)
    except RuntimeError as exc:
        return {**report, "outcome": INCONCLUSIVE,
                "detail": f"the route cannot launch: {exc}"}
    env = {**tick._engine_env(), **extra_env}
    report["engine_version"] = _engine_version(engine_bin, env)

    control = _probe_run(engine_bin, "dontAsk", allowed, model, env, engine_settings)
    report["control"] = control
    not_enforced = (
        "so the allowlist is not being enforced and an auto result would "
        "measure nothing. The usual cause is `sandbox.autoAllowBashIfSandboxed` "
        "in the Claude settings the engine loads (for example "
        "~/.claude/settings.json): Bash then runs without the allowlist. Probe "
        f"again with {SANDBOX_OFF_REMEDY} to launch engines unsandboxed without "
        "editing those settings, and pass the same value to write-config. "
        "Otherwise a settings `permissions.allow` rule grants the command; "
        "remove it from the operator's settings"
    )
    if control["executed"]:
        return {**report, "outcome": UNMEASURABLE,
                "detail": f"`{PROBE_COMMAND}` ran under dontAsk, {not_enforced}"}
    if control["attempted"] and control["denials_recorded"] and not control["denied"]:
        return {**report, "outcome": UNMEASURABLE, "detail": (
            f"`{PROBE_COMMAND}` was not denied under dontAsk — it was let "
            f"through and then failed ({control['denial']}) — {not_enforced}"
        )}
    if not control["denied"]:
        return {**report, "outcome": INCONCLUSIVE, "detail": (
            "the dontAsk control produced no recorded denial of the command, so "
            f"the control did not hold: {_run_summary(control)}"
        )}

    auto = _probe_run(engine_bin, "auto", allowed, model, env, engine_settings)
    report["auto"] = auto
    if auto["executed"] and auto["output_returned"]:
        return {**report, "outcome": PASS, "detail": (
            f"`{PROBE_COMMAND}` was denied under dontAsk and approved and run "
            "under auto"
        )}
    if auto["executed"]:
        detail = "the command ran under auto but its output did not come back"
    elif auto["denied"]:
        detail = f"auto denied the command: {auto['denial'] or _run_summary(auto)}"
    elif auto["attempted"] and auto["denials_recorded"]:
        return {**report, "outcome": INCONCLUSIVE, "detail": (
            "auto approved the command but it failed to execute "
            f"({auto['denial']}); fix that and probe again"
        )}
    else:
        detail = f"auto never ran the command: {_run_summary(auto)}"
    return {**report, "outcome": AUTO_FAILED, "detail": detail}


def _engine_version(engine_bin: str, env: Mapping[str, str]) -> str:
    try:
        done = subprocess.run(
            [engine_bin, "--version"], capture_output=True, text=True,
            env=dict(env), timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def _probe_run(
    engine_bin: str,
    permission_mode: str,
    allowed_tools: str,
    model: str,
    env: Mapping[str, str],
    engine_settings: str = "",
) -> dict[str, object]:
    """Run one headless engine in a fresh throwaway directory.

    Launched the way dispatch launches: dispatch's argv, the prompt on a stdin
    pipe (no TTY), its own process group. The directory is created here and
    removed afterwards; it is never inside a target checkout.
    """
    workdir = os.path.realpath(tempfile.mkdtemp(prefix="pr-pipeline-probe-"))
    run: dict[str, object] = {
        "permission_mode": permission_mode, "returncode": None,
        "timed_out": False, "attempted": False, "denied": False,
        "denials_recorded": False, "executed": False,
        "output_returned": False, "denial": "", "stderr_tail": "",
    }
    try:
        argv = probe_argv(
            engine_bin, permission_mode, allowed_tools, model, engine_settings,
        )
        try:
            proc = subprocess.Popen(
                argv, cwd=workdir, env=dict(env), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            run["stderr_tail"] = f"launch failed: {exc}"
            return run
        try:
            stdout, stderr = proc.communicate(
                PROBE_PROMPT.encode(), timeout=PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            run["timed_out"] = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            stdout, stderr = proc.communicate()
        run["returncode"] = proc.returncode
        run["stderr_tail"] = stderr.decode(errors="replace").strip()[-500:]
        run.update(_read_stream(stdout.decode(errors="replace"), workdir))
        run["executed"] = os.path.isdir(os.path.join(workdir, ".git"))
        return run
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _read_stream(stream: str, workdir: str) -> dict[str, object]:
    """Read the harness's own record of the probe command from stream-json.

    The model's final text is not evidence: it could state the directory
    without having run anything. Tool results and the result event's
    ``permission_denials`` are written by the harness.

    A tool error is not a denial. A command the permission layer let through
    can still fail — a sandbox refusing the write, for one — and only
    ``permission_denials`` says the permission layer refused it.
    """
    attempted_ids: set[str] = set()
    attempted = output_returned = denied = False
    denials_recorded = False
    denial = ""
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result" and isinstance(
            event.get("permission_denials"), list,
        ):
            denials_recorded = True
            denied = any(
                _denial_names_probe(entry, attempted_ids)
                for entry in event["permission_denials"]
            )
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("name") == "Bash":
                command = item.get("input", {}).get("command", "")
                if isinstance(command, str) and command.strip().startswith(PROBE_COMMAND):
                    attempted = True
                    attempted_ids.add(str(item.get("id")))
            elif (item.get("type") == "tool_result"
                  and str(item.get("tool_use_id")) in attempted_ids):
                text = _tool_result_text(item.get("content"))
                if item.get("is_error"):
                    denial = text.strip()[:500]
                elif workdir in text:
                    output_returned = True
    return {
        "attempted": attempted, "denied": denied,
        "denials_recorded": denials_recorded,
        "output_returned": output_returned, "denial": denial,
    }


def _denial_names_probe(entry: object, attempted_ids: set[str]) -> bool:
    if not isinstance(entry, dict):
        return False
    if str(entry.get("tool_use_id")) in attempted_ids:
        return True
    tool_input = entry.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return (
        entry.get("tool_name") == "Bash" and isinstance(command, str)
        and command.strip().startswith(PROBE_COMMAND)
    )


def _tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


def _run_summary(run: Mapping[str, object]) -> str:
    if run.get("timed_out"):
        return f"timed out after {PROBE_TIMEOUT_SECONDS}s"
    tail = run.get("stderr_tail") or "no output"
    return f"exit {run.get('returncode')}: {tail}"


_CONFIG_REPO_RE = pipeline._CONFIG_REPO_RE


def probe_record_path(repo: str) -> Path:
    """Return where the probe result for ``repo`` is kept."""
    if not _CONFIG_REPO_RE.match(repo):
        raise SetupRefused(f'{repo!r} is not an "owner/name" repository')
    owner, name = repo.casefold().split("/", 1)
    root = pipeline.operator_config_path().parent / "setup-probes"
    record = root / owner / f"{name}.json"
    if root.is_symlink() or not record.resolve().is_relative_to(root.resolve()):
        raise SetupRefused(f"probe record for {repo} resolves outside setup-probes")
    return record


# --- Writing the operator's configuration ---

_TOML_BARE_KEY_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
def dump_toml(document: Mapping[str, object]) -> str:
    """Serialise the subset of TOML the operator config uses.

    Tables, strings, booleans, numbers, and arrays of those. Anything else —
    dates, arrays of tables — raises rather than being written some other way.
    """
    lines: list[str] = []

    def key(part: str) -> str:
        return part if _TOML_BARE_KEY_RE.match(part) else json.dumps(part, ensure_ascii=False)

    def value(item: object) -> str:
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, (int, float)):
            return repr(item)
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, list):
            return "[" + ", ".join(value(entry) for entry in item) + "]"
        raise ValueError(f"cannot write {type(item).__name__} values")

    def table(path: list[str], body: Mapping[str, object]) -> None:
        scalars = [(k, v) for k, v in body.items() if not isinstance(v, dict)]
        tables = [(k, v) for k, v in body.items() if isinstance(v, dict)]
        if path and (scalars or not tables):
            if lines:
                lines.append("")
            lines.append("[" + ".".join(key(part) for part in path) + "]")
        for name, item in scalars:
            lines.append(f"{key(name)} = {value(item)}")
        for name, item in tables:
            table([*path, name], item)

    table([], document)
    return "\n".join(lines) + "\n"


class SetupRefused(Exception):
    """A write setup declines to make, with the reason for the operator."""


def plan_config(
    repo: str,
    *,
    login: str,
    verify_tools: Sequence[str],
    permission_mode: str,
    base_ref: str | None,
    engine_settings: str = "",
) -> tuple[str, dict[str, object], dict[str, object]]:
    """Merge one repository's settings into the operator config, or refuse.

    Enforces the probe's contract for the combination being written:

    - a probe result must exist for this repository, run with exactly these
      verification grants and engine settings;
    - ``auto``, and an empty ``verify_tools``, need a passing probe;
    - ``dontAsk`` needs verification grants, because without them an engine
      is stranded mid-run.

    Returns:
        ``(path, existing, merged)`` documents; equal when nothing changes.

    Raises:
        SetupRefused: With the reason to relay to the operator.
    """
    if not _CONFIG_REPO_RE.match(repo):
        raise SetupRefused(f'{repo!r} is not an "owner/name" repository')
    if permission_mode not in ("auto", "dontAsk"):
        raise SetupRefused(f"permission_mode must be auto or dontAsk, not {permission_mode!r}")
    record = _read_probe_record(repo)
    if record is None:
        raise SetupRefused(
            f"no route probe is recorded for {repo}; run the probe with the "
            "verification grants you mean to write"
        )
    probed = record.get("verify_tools")
    if not isinstance(probed, list) or probed != list(verify_tools):
        raise SetupRefused(
            f"the recorded probe for {repo} used verify_tools {probed!r}, not "
            f"{list(verify_tools)!r}; rerun the probe with the grants you mean "
            "to write"
        )
    probed_settings = record.get("engine_settings", "")
    if probed_settings != engine_settings:
        raise SetupRefused(
            f"the recorded probe for {repo} used engine_settings "
            f"{probed_settings!r}, not {engine_settings!r}; rerun the probe with "
            "the engine settings you mean to write"
        )
    outcome = record.get("outcome")
    if not verify_tools and outcome != PASS:
        raise SetupRefused(
            f"the route did not pass the probe ({outcome}): an empty "
            "verify_tools relies on auto approving undeclared commands, which "
            "this route does not do: name the target's verification commands"
        )
    if permission_mode == "dontAsk" and not verify_tools:
        raise SetupRefused(
            "dontAsk without verification grants strands an engine mid-run: "
            "name the target's verification commands"
        )
    if permission_mode == "auto" and outcome != PASS:
        raise SetupRefused(
            f"the route did not pass the probe ({outcome}), so auto is not "
            "recorded for it: choose dontAsk with verification grants, or change "
            "the engine route and probe again"
        )

    path = pipeline.operator_config_path()
    existing, problem = pipeline._read_toml(path)
    if problem:
        raise SetupRefused(f"{path}: {problem}; fix or move it before setup rewrites it")
    merged = json.loads(json.dumps(existing))
    operator = merged.setdefault("operator", {})
    repos = merged.setdefault("repos", {})
    if not isinstance(operator, dict) or not isinstance(repos, dict):
        raise SetupRefused(f"{path}: [operator] and [repos] must be tables")
    operator["login"] = login
    section_key = next(
        (name for name in repos if pipeline.same_repository(name, repo)), repo,
    )
    section = repos.setdefault(section_key, {})
    if not isinstance(section, dict):
        raise SetupRefused(f'{path}: [repos."{section_key}"] must be a table')
    section["verify_tools"] = list(verify_tools)
    section["permission_mode"] = permission_mode
    # Written even when blank: a missing key would inherit a nonblank
    # [defaults] value, not the "no --settings flag" the probe measured.
    section["engine_settings"] = engine_settings
    if base_ref is not None:
        section["base_ref"] = base_ref
    return str(path), existing, merged


def _read_probe_record(repo: str) -> dict[str, object] | None:
    try:
        record = json.loads(probe_record_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def write_config(
    repo: str,
    *,
    login: str,
    verify_tools: Sequence[str],
    permission_mode: str,
    base_ref: str | None,
    dry_run: bool,
    engine_settings: str = "",
) -> dict[str, object]:
    """Validate the merged configuration with the tick's resolver, then write it.

    The merged text is resolved exactly as a tick would read it before it
    replaces anything, so setup cannot leave behind a file the tick refuses.
    Nothing is written when the result equals the existing file.

    Raises:
        SetupRefused: When the plan or the resolver refuses.
    """
    path_text, existing, merged = plan_config(
        repo, login=login, verify_tools=verify_tools,
        permission_mode=permission_mode, base_ref=base_ref,
        engine_settings=engine_settings,
    )
    path = Path(path_text)
    if merged == existing:
        errors = _resolved_errors(
            pipeline.resolve_config(repo), repo, verify_tools, permission_mode,
            engine_settings,
        )
        if errors:
            raise SetupRefused(
                "the configuration already says this, but the tick would refuse "
                "it: " + "; ".join(errors)
            )
        return {"path": path_text, "changed": False, "diff": ""}
    try:
        text = dump_toml(merged)
    except ValueError as exc:
        raise SetupRefused(f"{path}: {exc}; edit the file by hand") from exc
    if tomllib.loads(text) != merged:
        raise SetupRefused(f"{path}: could not serialise the merged configuration")
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True), text.splitlines(keepends=True),
        str(path), str(path),
    ))

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".toml")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        errors = _resolved_errors(
            _resolve_staged(staged, repo), repo, verify_tools, permission_mode,
            engine_settings,
        )
        if errors:
            raise SetupRefused("the merged configuration is invalid: " + "; ".join(errors))
        if dry_run:
            return {"path": path_text, "changed": True, "diff": diff, "dry_run": True}
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        os.replace(staged, path)
        staged = ""
    finally:
        if staged:
            os.unlink(staged)
    return {"path": path_text, "changed": True, "diff": diff}


def _resolved_errors(
    config: pipeline.ResolvedConfig,
    repo: str,
    verify_tools: Sequence[str],
    permission_mode: str,
    engine_settings: str,
) -> list[str]:
    """Return why a tick would refuse, or misread, this repository's settings."""
    errors = list(config.errors)
    if not config.repo_configured:
        errors.append(f"the file does not configure {repo}")
    if config.verify_tools != tuple(verify_tools):
        errors.append("verify_tools do not resolve as written")
    if config.permission_mode != permission_mode:
        errors.append("permission_mode does not resolve as written")
    if config.engine_settings != engine_settings:
        errors.append("engine_settings does not resolve as written")
    return errors


def _resolve_staged(staged: str, repo: str) -> pipeline.ResolvedConfig:
    previous = os.environ.get("PR_PIPELINE_CONFIG")
    os.environ["PR_PIPELINE_CONFIG"] = staged
    try:
        return pipeline.resolve_config(repo)
    finally:
        if previous is None:
            os.environ.pop("PR_PIPELINE_CONFIG", None)
        else:
            os.environ["PR_PIPELINE_CONFIG"] = previous


# --- Reading a target repository ---

_GUIDANCE_FILES = ("CLAUDE.md", "AGENTS.md")
_PACKAGE_SCRIPTS = ("test", "lint", "typecheck", "check")


def _gh(*args: str) -> str:
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True, timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(exc.stderr.strip() or str(exc)) from exc
    return done.stdout


def _gh_json(*args: str) -> object:
    return json.loads(_gh(*args))


def _file_text(repo: str, name: str, ref: str) -> str:
    try:
        payload = _gh_json("api", f"repos/{repo}/contents/{name}?ref={ref}",
                           "--hostname", "github.com")
        if (not isinstance(payload, dict) or payload.get("encoding") != "base64"
                or not isinstance(payload.get("content"), str)):
            raise ValueError("unexpected content response")
        return base64.b64decode(payload["content"], validate=True).decode("utf-8")
    except (ValueError, OSError, UnicodeError) as exc:
        raise ValueError(f"cannot inspect {repo}/{name}: {exc}") from exc


def inspect_repository(repo: str) -> dict[str, object]:
    """Read a target through the GitHub API and propose verification grants.

    The proposal is a starting point for the operator, not configuration: the
    target's own guidance files name what its verification actually is, and
    the operator confirms or edits before anything is written.
    """
    meta = _gh_json("api", f"repos/{repo}", "--hostname", "github.com")
    if not isinstance(meta, dict):
        raise ValueError(f"unexpected repository response for {repo}")
    canonical = str(meta.get("full_name") or repo)
    branch = str(meta.get("default_branch") or "")
    if not branch:
        raise ValueError(f"{canonical} reports no default branch")
    listing = _gh_json("api", f"repos/{canonical}/contents?ref={branch}",
                       "--hostname", "github.com")
    if (not isinstance(listing, list) or any(
        not isinstance(entry, dict)
        or not isinstance(entry.get("name"), str)
        or not isinstance(entry.get("type"), str)
        for entry in listing
    )):
        raise ValueError(f"unexpected root listing for {canonical}")
    names = {entry["name"] for entry in listing if entry["type"] == "file"}

    manifests = [name for name in (
        "pom.xml", "build.gradle", "build.gradle.kts", "package.json",
        "pyproject.toml", "Cargo.toml",
    ) if name in names]
    proposals: list[str] = []
    if "pom.xml" in names:
        maven = "./mvnw" if "mvnw" in names else "mvn"
        proposals += [f"{maven} test *", f"{maven} verify *"]
    if "build.gradle" in names or "build.gradle.kts" in names:
        gradle = "./gradlew" if "gradlew" in names else "gradle"
        proposals += [f"{gradle} test *", f"{gradle} check *"]
    if "package.json" in names:
        proposals += _package_proposals(_file_text(canonical, "package.json", branch), names)
    if "pyproject.toml" in names:
        proposals += _python_proposals(_file_text(canonical, "pyproject.toml", branch), names)
    if "Cargo.toml" in names:
        proposals += ["cargo test *", "cargo clippy *"]

    config = pipeline.resolve_config(canonical)
    current = None
    if config.repo_configured:
        current = {
            "verify_tools": list(config.verify_tools),
            "permission_mode": config.permission_mode,
            "base_ref": config.base_ref,
        }
    return {
        "repo": canonical,
        "default_branch": branch,
        "manifests": manifests,
        "proposed_verify_tools": proposals,
        "guidance_files": [name for name in _GUIDANCE_FILES if name in names],
        "configured": config.repo_configured,
        "current": current,
        "config_errors": list(config.errors),
    }


def _package_proposals(text: str, names: set[str]) -> list[str]:
    try:
        manifest = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"invalid package.json: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("invalid package.json: expected an object")
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict):
        raise ValueError("invalid package.json: scripts must be an object")
    if "pnpm-lock.yaml" in names:
        manager = "pnpm"
    elif "yarn.lock" in names:
        manager = "yarn"
    elif "bun.lock" in names or "bun.lockb" in names:
        manager = "bun"
    else:
        manager = "npm"
    return [f"{manager} run {script} *" for script in _PACKAGE_SCRIPTS if script in scripts]


def _python_proposals(text: str, names: set[str]) -> list[str]:
    try:
        tools = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid pyproject.toml: {exc}") from exc
    if "uv.lock" in names:
        runner = "uv run "
    elif "poetry.lock" in names:
        runner = "poetry run "
    else:
        runner = ""
    proposals = [f"{runner}pytest *" if runner else "python -m pytest *"]
    if isinstance(tools, dict) and "ruff" in tools:
        proposals.append(f"{runner}ruff *" if runner else "ruff check *")
    return proposals


# --- Host prerequisites ---

HOST_TOOLS = ("gh", "git", "codex", "jq", "claude")


def check_prereqs(path: str | None = None, impl_engine: str = "claude") -> dict[str, object]:
    """Report which host tools resolve, without failing on the first miss."""
    found = {name: shutil.which(name, path=path) for name in HOST_TOOLS}
    report: dict[str, object] = {
        "found": {name: where for name, where in found.items() if where},
        "missing": [name for name, where in found.items() if not where],
    }
    if impl_engine == "claude-ds":
        try:
            tick._resolve_engine("claude-ds")
        except RuntimeError as exc:
            report["route_problem"] = str(exc)
    return report


# --- Deny rules for the destructive git surface ---

# Under auto the engine allowlist is not a ceiling, and an omitted command is
# not a denied one. These rules deny force-push, remote-branch deletion, forced
# refspecs and mirror/prune pushes. Deny outranks allow, so each rule is tested
# against the implementer's and fixer's own exact push grants.
#
# Deletion by an empty-source refspec (`git push origin :branch`) is not
# covered. The rule for it would end in `:*`, which Claude Code reads as its
# legacy prefix form — "git push, then anything" — and would deny every
# stage's own push. `:refs/...` deletions are covered.
DESTRUCTIVE_GIT_DENY_RULES = (
    "Bash(git push *--force*)",
    "Bash(git push -f*)",
    "Bash(git push * -f*)",
    "Bash(git push *--delete*)",
    "Bash(git push -d *)",
    "Bash(git push * -d *)",
    "Bash(git push * :refs/*)",
    "Bash(git push * +*)",
    "Bash(git push *--mirror*)",
    "Bash(git push *--prune*)",
)


def claude_settings_path() -> Path:
    """Return the Claude user settings file, honouring ``$CLAUDE_CONFIG_DIR``."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir and Path(config_dir).is_absolute():
        return Path(config_dir) / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def install_deny_rules(path: Path) -> list[str]:
    """Add the missing deny rules to ``path``, preserving everything else.

    Raises:
        SetupRefused: When the settings file is not a JSON object with a
            list-valued ``permissions.deny``.
    """
    data: object = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SetupRefused(f"{path}: {exc}; add the rules by hand") from exc
    if not isinstance(data, dict):
        raise SetupRefused(f"{path}: expected a JSON object; add the rules by hand")
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise SetupRefused(f"{path}: permissions must be an object")
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        raise SetupRefused(f"{path}: permissions.deny must be a list")
    added = [rule for rule in DESTRUCTIVE_GIT_DENY_RULES if rule not in deny]
    if not added:
        return []
    deny.extend(added)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    pipeline.atomic_write(str(path), json.dumps(data, indent=2) + "\n")
    return added


# --- CLI ---


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="report the authenticated GitHub login")
    prereqs = commands.add_parser("prereqs", help="report missing host tools")
    prereqs.add_argument("--impl-engine", default="claude", choices=("claude", "claude-ds"))
    for name in ("inspect", "labels"):
        commands.add_parser(name).add_argument("repo")
    probe = commands.add_parser("probe", help="measure auto on the engine route")
    probe.add_argument("repo")
    probe.add_argument("--impl-engine", default="claude", choices=("claude", "claude-ds"))
    probe.add_argument("--model", default="")
    probe.add_argument("--verify-tool", action="append", default=[])
    probe.add_argument("--engine-settings", default="")
    write = commands.add_parser("write-config", help="record one repository")
    write.add_argument("repo")
    write.add_argument("--login", required=True)
    write.add_argument("--verify-tool", action="append", default=[])
    write.add_argument("--permission-mode", required=True, choices=("auto", "dontAsk"))
    write.add_argument("--base-ref")
    write.add_argument("--engine-settings", default="")
    write.add_argument("--dry-run", action="store_true")
    deny = commands.add_parser("deny-rules", help="show or install deny rules")
    deny.add_argument("--install", action="store_true")
    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except SetupRefused as exc:
        print(f"setup: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"setup: {exc}", file=sys.stderr)
        return 3


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "login":
        _emit({"login": _gh("api", "user", "--jq", ".login").strip()})
        return 0
    if args.command == "prereqs":
        _emit(check_prereqs(impl_engine=args.impl_engine))
        return 0
    if args.command == "inspect":
        _emit(inspect_repository(args.repo))
        return 0
    if args.command == "labels":
        unknown = sorted(labels.provision(args.repo))
        _emit({
            "repo": args.repo,
            "unknown_ai_labels": unknown,
            "note": (
                "Unknown ai: labels were left unchanged. An item carrying only "
                "one of them is selected by no dispatch query and goes dark to "
                "the pipeline; reconciling them is the repository owner's call."
            ) if unknown else "",
        })
        return 0
    if args.command == "probe":
        record = probe_record_path(args.repo)
        tools = _checked_tools(args.verify_tool)
        report = run_probe(args.repo, impl_engine=args.impl_engine,
                           model=args.model, verify_tools=tools,
                           engine_settings=_checked_settings(args.engine_settings))
        report["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        record.parent.mkdir(parents=True, exist_ok=True)
        pipeline.atomic_write(str(record), json.dumps(report, indent=2) + "\n")
        _emit(report)
        return 0 if report["outcome"] == PASS else 1
    if args.command == "write-config":
        _emit(write_config(
            args.repo, login=args.login, verify_tools=_checked_tools(args.verify_tool),
            permission_mode=args.permission_mode, base_ref=args.base_ref,
            dry_run=args.dry_run,
            engine_settings=_checked_settings(args.engine_settings),
        ))
        return 0
    path = claude_settings_path()
    if args.install:
        _emit({"path": str(path), "added": install_deny_rules(path)})
        return 0
    _emit({"path": str(path), "rules": list(DESTRUCTIVE_GIT_DENY_RULES)})
    return 0


def _checked_tools(tools: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(tool.strip() for tool in tools if tool.strip())
    for tool in cleaned:
        if not pipeline.VERIFY_TOOL_BODY_RE.match(tool):
            raise SetupRefused(
                f"{tool!r} is not a permission body (no parentheses or newlines)"
            )
    return cleaned


def _checked_settings(settings: str) -> str:
    cleaned = settings.strip()
    if cleaned and not pipeline.valid_engine_settings(cleaned):
        raise SetupRefused(
            f"{settings!r} is not an engine_settings value: give a JSON object "
            "or an absolute path to a settings file"
        )
    return cleaned


def _emit(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    sys.exit(main())
