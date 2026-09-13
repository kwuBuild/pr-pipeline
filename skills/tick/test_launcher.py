"""Tests for the operator-facing launcher and its interpreter resolution.

The pipeline's runtime contract is "stdlib only, Python >= 3.11". ``uv`` used to
stand between the operator and that contract, which contradicted it (a package
manager nothing declares) and was not reliably correct either: ``uv run --python
3.13`` has been observed resolving an interpreter without ``tomllib``, and an
Anaconda ``PATH`` shadows the resolved environment outright.

``tick.sh`` replaces it, and ``resolve-python.sh`` is the one definition of what
"a usable interpreter" means — shared with ``preflight.sh`` and the status
dashboard so the three cannot disagree about which Python the pipeline runs on.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
TICK_SH = SKILL_DIR / "tick.sh"
RESOLVE_SH = SKILL_DIR / "resolve-python.sh"
REPO_ROOT = SKILL_DIR.parents[1]
CI_YML = REPO_ROOT / ".github/workflows/ci.yml"

# A stub interpreter answers exactly one question during resolution: the ``-c``
# version probe. Passing it is what makes the stub "a usable Python" as far as
# the resolver is concerned.
PROBE_PASSING_STUB = '#!/bin/sh\ncase "$1" in -c) exit 0 ;; esac\nexit 0\n'
PROBE_FAILING_STUB = "#!/bin/sh\nexit 1\n"
ARGV_ECHO_STUB = "#!/bin/sh\ncase \"$1\" in -c) exit 0 ;; esac\nprintf '%s\\n' \"$@\"\n"
# The resolver's candidate list, read out of the resolver rather than mirrored
# here. A copy would have to be kept in step by hand, and the failure mode of a
# stale copy is silent: the no-usable-interpreter tests shadow the names they
# know about, so a name they have never heard of resolves a real interpreter and
# the expected failure simply does not happen.
_CANDIDATE_ARRAY = re.search(
    r"^PR_PIPELINE_PYTHON_CANDIDATES=\(([^)]*)\)", RESOLVE_SH.read_text(), re.M
)
assert _CANDIDATE_ARRAY is not None, (
    "resolve-python.sh no longer declares PR_PIPELINE_PYTHON_CANDIDATES; every "
    "test below reads its candidate list from there"
)
PROBED_CANDIDATES = tuple(_CANDIDATE_ARRAY.group(1).split())


# --- resolve-python.sh, run directly ---


def test_resolve_python_prints_an_interpreter_that_satisfies_the_floor() -> None:
    """Run directly, the helper names an interpreter on stdout."""
    result = run_bash(RESOLVE_SH)

    assert result.returncode == 0, result.stderr
    resolved = result.stdout.strip()
    assert resolved, "resolver printed nothing"
    assert version_of(resolved) >= (3, 11)


def test_resolve_python_honours_an_explicitly_pinned_interpreter(tmp_path: Path) -> None:
    """``PR_PIPELINE_PYTHON`` wins over the probed minors.

    An operator whose ``PATH`` is owned by Anaconda or a pyenv shim needs a way
    to name the interpreter outright; without it the only fix is reordering
    ``PATH``, which is not theirs to change on a shared box.
    """
    pinned = tmp_path / "my-python"
    write_stub(pinned, PROBE_PASSING_STUB)

    result = run_bash(RESOLVE_SH, env={"PR_PIPELINE_PYTHON": str(pinned)})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(pinned)


def test_resolve_python_rejects_a_pinned_interpreter_below_the_floor(
    tmp_path: Path,
) -> None:
    """A pin is a hint, not an override: a 3.9 still has no ``tomllib``."""
    pinned = tmp_path / "old-python"
    write_stub(pinned, PROBE_FAILING_STUB)

    result = run_bash(RESOLVE_SH, env={"PR_PIPELINE_PYTHON": str(pinned)})

    assert result.stdout.strip() != str(pinned)


def test_resolve_python_warns_when_it_ignores_the_operator_s_pin(
    tmp_path: Path,
) -> None:
    """Falling back past a pin must say so, naming the pin that was ignored.

    A typo'd path and a deliberate-but-too-old pin are the same event to
    ``command -v``, and both end with the pipeline running on an interpreter the
    operator did not choose. Silent fallback makes that indistinguishable from
    the pin having worked — the operator reads a successful tick as confirmation
    of a setting that was in fact discarded.
    """
    typo = tmp_path / "pyhton3.13"

    result = run_bash(RESOLVE_SH, env={"PR_PIPELINE_PYTHON": str(typo)})

    assert "PR_PIPELINE_PYTHON" in result.stderr
    assert str(typo) in result.stderr


def test_resolve_python_fails_naming_the_version_requirement(tmp_path: Path) -> None:
    """No usable interpreter must read as a version requirement, not as noise.

    The failure this replaces surfaced as ``ModuleNotFoundError: tomllib``,
    which reads as a broken install and sends an operator to reinstall the
    pipeline. The real cause is ``PATH`` precedence, so the message has to name
    both the requirement and the escape hatch.
    """
    result = run_bash(RESOLVE_SH, path=only_old_python(tmp_path))

    assert result.returncode != 0
    assert "3.11" in result.stderr
    assert "PR_PIPELINE_PYTHON" in result.stderr
    assert result.stdout.strip() == ""


# --- tick.sh ---


def test_tick_sh_forwards_its_arguments_to_tick_py(tmp_path: Path) -> None:
    """The launcher is a pass-through: every operator flag reaches tick.py."""
    pinned = tmp_path / "argv-python"
    write_stub(pinned, ARGV_ECHO_STUB)

    result = run_bash(
        TICK_SH, "--loop", "pr:468", env={"PR_PIPELINE_PYTHON": str(pinned)}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(SKILL_DIR / "lib" / "tick.py"),
        "--loop",
        "pr:468",
    ]


def test_tick_sh_runs_tick_py_under_a_real_interpreter(tmp_path: Path) -> None:
    """End to end: a side-effect-free subcommand runs with no ``uv`` involved.

    ``engine-progress`` is the read-only report the dashboard already shells
    out for — it takes no tick lock and makes no state transition, so it can
    prove the launcher works without dispatching anything.
    """
    result = run_bash(
        TICK_SH, "engine-progress", "--wt-root", str(tmp_path), cwd=SKILL_DIR
    )

    assert result.returncode == 0, result.stderr


def test_tick_sh_fails_with_the_version_requirement_and_the_startup_code(
    tmp_path: Path,
) -> None:
    """A missing interpreter is a startup-probe failure: exit 3, named cause.

    Exit 3 already means "a required tool is missing, nothing ran", and the
    documented ``/loop`` prompt stops on it. A missing Python belongs in that
    class, not in a new code no operator procedure handles.
    """
    result = run_bash(TICK_SH, path=only_old_python(tmp_path))

    assert result.returncode == 3
    assert "3.11" in result.stderr


# --- The resolver's range and CI's range must be the same range ---


def test_ci_proves_both_ends_of_the_range_the_resolver_will_pick() -> None:
    """CI must test the newest interpreter the resolver is willing to choose.

    The resolver picks the newest name it finds, so the moment an operator
    installs that version it becomes the runtime — silently, with no config
    change and no warning. If CI's top leg is older than the resolver's top
    candidate, the pipeline runs in production on a version nothing proves, and
    the first evidence is a stack trace on somebody's box.

    The floor is asserted for the same reason from the other end: it is the one
    real version constraint (``tomllib``), so a leg that stops testing it stops
    testing the claim.
    """
    # Scoped to the candidate array, not to the file: prose in this resolver
    # names old interpreters on purpose (the 3.9 that a stock macOS `python3`
    # resolves to is the whole reason the file exists), and a comment must not
    # be able to conscript CI into testing a version the resolver would never
    # choose.
    probed = {f"3.{minor}" for name in PROBED_CANDIDATES
              for minor in re.findall(r"^python3\.(\d+)$", name)}
    assert probed, "no explicit minors found in the resolver's candidate list"

    matrix = re.search(
        r"python-version:\s*\[([^\]]*)\]",
        (SKILL_DIR.parents[1] / ".github/workflows/ci.yml").read_text(),
    )
    assert matrix is not None, "CI no longer declares a python-version matrix"
    tested = set(re.findall(r"\d+\.\d+", matrix.group(1)))

    newest = max(probed, key=lambda v: int(v.split(".")[1]))
    assert newest in tested, (
        f"the resolver will pick python{newest} when present, but CI tests "
        f"{sorted(tested)} — the runtime would be a version nothing proves"
    )
    assert "3.11" in tested, "CI no longer tests the floor the code requires"


def test_ci_shellcheck_resolves_the_scripts_it_is_told_to_follow() -> None:
    """`-x` alone does not follow a `# shellcheck source=` directive.

    ShellCheck resolves a ``source=`` path against the *current working
    directory*, not the directory of the file being checked, and CI runs from
    the repository root. So ``source=resolve-python.sh`` in ``tick.sh`` looks
    for ``./resolve-python.sh`` at the root, does not find it, and emits SC1091
    at info severity — which ``--severity=error`` discards. The job goes green,
    the sourced functions are still unknown, and the directives are still
    decoration; the only thing holding the claim up is the comment above the
    step. ``--source-path=SCRIPTDIR`` is what makes ``-x`` mean what the job
    says it means.
    """
    run = ci_step_run_block("ShellCheck")

    assert "-x" in run.split(), "the ShellCheck job no longer passes -x"
    assert "--source-path=SCRIPTDIR" in run, (
        "ShellCheck resolves `source=` paths against the working directory -- "
        "the repo root in CI -- not the script's own directory, so every "
        "`# shellcheck source=` directive under skills/ fails to resolve and "
        "the resulting SC1091 hides below --severity=error"
    )


def test_ci_lints_every_shell_script_the_pipeline_ships() -> None:
    """A shipped `.sh` that no lint job names is a quoting bug waiting to ship.

    These scripts run on other people's machines, and the shell's failure mode
    is not an exception — an unquoted path with a space silently resolves the
    wrong interpreter, or nothing at all. The lint job existed before the
    launcher did and named its two files literally, so the two newest and
    least-exercised scripts in the tree were the two it did not cover. Deriving
    the expectation from the repository rather than from a second hand-kept list
    is what stops the next one slipping through the same way.
    """
    # The ShellCheck step's own command, not the whole workflow: `bash -n`
    # names the same scripts a few lines above, so a file dropped from lint but
    # left in the syntax check would satisfy a whole-file substring search --
    # a test passing under exactly the condition it exists to catch.
    lint = ci_step_run_block("ShellCheck")
    # Tracked files, not a filesystem glob: "shipped" means "in the repository",
    # and a working tree can hold scratch copies and editor backups that nobody
    # installs and CI should not be asked to lint. Split on lines, not
    # whitespace, so a path containing a space fails loudly instead of being
    # silently torn into two names that match nothing.
    shipped = set(
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "skills/*/*.sh"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    )
    assert shipped, "no shell scripts found under skills/"

    unlinted = sorted(path for path in shipped if path not in lint)
    assert not unlinted, (
        "these shipped shell scripts are not named by CI's shellcheck job, so "
        f"a quoting bug in them reaches an operator before it reaches us: {unlinted}"
    )


# --- The entry-point modules' own floor guard ---


def test_entry_points_state_the_version_requirement_before_importing_tomllib() -> None:
    """The guard is defence in depth for the invocation the launcher didn't make.

    ``tick.py`` and ``pipeline.py`` are both runnable directly — the lib
    command reference documents ``pipeline.py`` subcommands, and an operator
    debugging a tick reaches for the module. Under a 3.9 the first thing either
    hits is ``import tomllib``, and ``ModuleNotFoundError: tomllib`` names
    neither the requirement nor the fix. The guard has to run *before* that
    import or it never runs at all, so order is the assertion.
    """
    for module in (SKILL_DIR / "lib" / "tick.py", SKILL_DIR / "lib" / "pipeline.py"):
        source = module.read_text(encoding="utf-8")

        # Anchored to the start of a line so the prose in a nearby comment —
        # which names both the guard and the import — cannot stand in for the
        # statements themselves.
        guard = re.search(
            r"^_PYTHON_FLOOR = \(3, 11\)\nif sys\.version_info < _PYTHON_FLOOR:",
            source,
            re.M,
        )
        tomllib_import = re.search(r"^import tomllib$", source, re.M)

        assert guard is not None, f"{module.name} has no Python floor guard"
        assert tomllib_import is not None, f"{module.name} no longer imports tomllib"
        assert guard.start() < tomllib_import.start(), (
            f"{module.name}'s floor guard runs after `import tomllib`, so the "
            "bare ImportError is still what an operator sees"
        )
        message = source[guard.start():tomllib_import.start()]
        assert "3.11" in message
        assert "PR_PIPELINE_PYTHON" in message


# --- Helpers ---


def ci_step_run_block(name: str) -> str:
    """The `run:` script of the CI step called ``name``, and nothing else.

    A test about one job has to read that job's command rather than search the
    whole workflow file. The syntax-check step names the same four scripts a
    few lines above the lint step, and a comment can name anything at all, so a
    substring match over ``ci.yml`` cannot tell "ShellCheck lints this" from
    "something, somewhere, mentions this".
    """
    text = CI_YML.read_text()
    # Horizontal space only in the indent groups: `\s` would match newlines,
    # letting the `run:` of a *later* step satisfy a step that declares none.
    header = re.search(
        rf"^(?P<indent>[ \t]*)- name: {re.escape(name)}\n"
        rf"(?:.*\n)*?(?P<run>[ \t]*)run: \|\n",
        text,
        re.M,
    )
    assert header is not None, f"CI no longer declares a step named {name!r}"

    # A block scalar's body is whatever is indented past its `run:` key; the
    # first line at or below that indent is the next key or the next step.
    run_indent = len(header.group("run"))
    lines = []
    for line in text[header.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= run_indent:
            break
        lines.append(line)
    assert lines, f"the {name!r} step has an empty run block"
    return "\n".join(lines)


def write_stub(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def only_old_python(tmp_path: Path) -> str:
    """A ``PATH`` where every probed interpreter name is too old to use.

    Not an emptied ``PATH``: the scripts still need ``bash`` and the coreutils
    they call, and "no python at all" is not the situation that actually
    happens — a stock macOS box has a 3.9 sitting on ``python3``. Shadowing
    each candidate name rather than replacing ``PATH`` keeps the rest of the
    environment real while making resolution fail the way an operator's does.
    """
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for candidate in PROBED_CANDIDATES:
        write_stub(stub_bin / candidate, PROBE_FAILING_STUB)
    return f"{stub_bin}:{os.environ['PATH']}"


def version_of(interpreter: str) -> tuple[int, ...]:
    printed = subprocess.run(
        [interpreter, "-c", "import sys; print('%d %d' % sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return tuple(int(part) for part in printed.split())


def run_bash(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
    path: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    # An operator pin leaking in from the ambient environment would silently
    # decide the very resolution these tests are asserting.
    child_env.pop("PR_PIPELINE_PYTHON", None)
    if path is not None:
        child_env["PATH"] = path
    child_env.update(env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        env=child_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
