"""Validate ladder evidence; absent, invalid, or mismatched evidence fails."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from session_report import read_session


# Independent expectation: deriving this from the controller's composer would
# accept defects in that composer. Controller changes need explicit review here.
COMMON_GRANTS = {
    "Read", "Edit", "Write", "Bash(git add *)", "Bash(git commit *)",
    "Bash(git status)", "Bash(git diff *)", "Bash(git log *)",
}


def records(path: Path):
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{number}: expected an object")
            yield record


def audit(path: Path, checkout: Path, install: Path) -> None:
    checkout = checkout.resolve()
    state = checkout / ".claude/worktrees"
    controller = install.resolve() / "skills/tick/lib/tick.py"
    covered = False
    count = 0
    for entry in records(path):
        cwd, opened = entry.get("cwd"), entry.get("path")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not isinstance(opened, str) or not opened:
            raise ValueError("audit record lacks a usable cwd/path")
        resolved = (Path(cwd) / opened).resolve()
        if resolved.is_relative_to(checkout) and not resolved.is_relative_to(state):
            raise ValueError(f"forbidden target open: {resolved}")
        covered |= resolved == controller and Path(cwd).resolve() == checkout
        count += 1
    if not covered:
        raise ValueError("audit has no installed tick source open from the target checkout")
    print(f"validated {count} opens, including the installed tick source")


def launches(observed: Path):
    found = False
    for path in sorted(observed.glob("claude-*.launch.json")):
        entry = json.loads(path.read_text())
        argv = entry.get("argv")
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            raise ValueError(f"{path}: invalid argv")
        if "-p" not in argv:
            continue  # Startup version probes are not engine sessions.
        if not isinstance(entry.get("cwd"), str) or not Path(entry["cwd"]).is_absolute():
            raise ValueError(f"{path}: missing launch cwd")
        if not isinstance(entry.get("dispatch"), dict):
            raise ValueError(f"{path}: missing dispatch snapshot; rerun with the current observer")
        found = True
        yield Path(str(path).removesuffix(".launch.json")), entry
    if not found:
        raise ValueError("no recorded headless Claude launches; rerun with the current observer")


def fixer_read(observed: Path, checkout: Path, install: Path, pr: int) -> None:
    worktrees = checkout.resolve() / ".claude/worktrees"
    dispatched = {
        event.get("dispatch_id")
        for path in (worktrees / ".tick-log").glob("tick-*.jsonl")
        for event in records(path)
        if event.get("action") == "launch" and event.get("stage") == "fix" and event.get("pr") == pr
    }
    doc = str(install.resolve() / "skills/review-response-hygiene/SKILL.md")
    for stem, entry in launches(observed):
        marker = entry["dispatch"]
        identity = marker.get("dispatch_id")
        if not (marker.get("stage") == "fix" and marker.get("pr_number") == pr
                and Path(entry["cwd"]).resolve() == worktrees / f"pr-{pr}"
                and isinstance(identity, str) and identity and identity in dispatched):
            continue
        if Path(f"{stem}.exit").read_text().strip() != "0":
            continue
        calls, _ = read_session(str(stem) + ".jsonl")
        if any(call["name"] == "Read" and call["input"].get("file_path") == doc
               and call["is_error"] is False and call["output"].strip() for call in calls):
            print(f"successful hygiene read in fixer {identity}: {stem.name}")
            return
    raise ValueError(f"no successful hygiene read bound to PR #{pr}'s fixer dispatch")


def option(argv: list[str], name: str) -> str:
    # The launcher emits one option/value pair. Reject ambiguous spellings.
    if argv.count(name) != 1 or any(arg.startswith(name + "=") for arg in argv):
        raise ValueError(f"expected exactly one {name} option")
    index = argv.index(name) + 1
    if index == len(argv) or argv[index].startswith("--"):
        raise ValueError(f"missing value for {name}")
    if name == "--allowedTools":
        if "--allowed-tools" in argv:
            raise ValueError("ambiguous allowedTools alias")
        end = index + 1
        while end < len(argv) and not argv[end].startswith("-"):
            end += 1
        return " ".join(argv[index:end])
    return argv[index]


def grants(observed: Path, checkout: Path, issue: int, pr: int, head: str,
           mode: str, verify_tool: str) -> None:
    count = 0
    for stem, entry in launches(observed):
        argv, marker = entry["argv"], entry["dispatch"]
        stage = marker.get("stage")
        if stage == "implement" and marker.get("pr_number") == issue:
            worktree, push = f"issue-{issue}", "Bash(git push origin HEAD)"
        elif stage == "fix" and marker.get("pr_number") == pr:
            worktree, push = f"pr-{pr}", f"Bash(git push origin HEAD:refs/heads/{head})"
        else:
            raise ValueError(f"{stem.name}: unexpected dispatch stage or item")
        if Path(entry["cwd"]).resolve() != checkout.resolve() / ".claude/worktrees" / worktree:
            raise ValueError(f"{stem.name}: launch is outside the expected worktree")
        if option(argv, "--permission-mode") != mode:
            raise ValueError(f"{stem.name}: wrong permission mode")
        value = option(argv, "--allowedTools")
        tokens = re.findall(r"[A-Za-z]+\([^()\r\n]*\)|[A-Za-z]+", value)
        if " ".join(tokens) != value:
            raise ValueError(f"{stem.name}: invalid allowedTools serialization")
        expected = COMMON_GRANTS | {f"Bash({verify_tool})", push}
        if set(tokens) != expected or len(tokens) != len(expected):
            raise ValueError(f"{stem.name}: grants differ: extra={set(tokens) - expected}, missing={expected - set(tokens)}")
        count += 1
    print(f"validated exact mode and grants for {count} launches")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="check", required=True)
    for name, fields in {
        "audit": [("path", Path), ("checkout", Path), ("install", Path)],
        "fixer-read": [("observed", Path), ("checkout", Path), ("install", Path), ("pr", int)],
        "grants": [("observed", Path), ("checkout", Path), ("issue", int), ("pr", int),
                   ("head", str), ("mode", str), ("verify_tool", str)],
    }.items():
        command = sub.add_parser(name)
        for field, kind in fields:
            command.add_argument(field, type=kind)
    args = vars(parser.parse_args())
    check = args.pop("check")
    try:
        {"audit": audit, "fixer-read": fixer_read, "grants": grants}[check](**args)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        print(f"invalid {check} evidence: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
