"""Canned `claude` for ladder stage L2: real git effects, no model call.

Installed first on PATH by l2-stubbed-tick.sh, with ``STATE_DIR`` rewritten to
an absolute path. It plays each role the pipeline launches `claude -p` for,
chosen by the prompt the pipeline wrote, and does the work that role's prompt
asks for with the smallest real change:

- setup's route probe: `dontAsk` is denied, `auto` really runs the command;
- the implementer: commit, push with the prompt's push form, write the PR body
  file, and end with IMPLEMENTATION_COMPLETE; or, for an issue marked
  ``ESCALATE``, change nothing and end with NEEDS_HUMAN_DECISION;
- the fixer: read the bound hygiene skill doc, commit, push to the PR head,
  and end with a per-finding summary and FIXES_PUSHED_RECHECK_NEEDED.

Every call is appended to ``STATE_DIR/claude-calls.jsonl``. An unrecognised
prompt ends in NEEDS_HUMAN_DECISION, so a prompt change fails the stage loudly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

STATE_DIR = "@STATE_DIR@"
WAIVER = "VERIFICATION_UNCONFIGURED_WAIVER"
# An issue body carrying this token makes the implementer escalate instead,
# which is the path that archives harvest evidence.
ESCALATE = "L2-ESCALATE"


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print("2.1.283 (Claude Code)")
        return 0
    prompt = sys.stdin.read()
    mode = argv[argv.index("--permission-mode") + 1] if "--permission-mode" in argv else ""
    record: dict[str, object] = {"argv": argv, "cwd": os.getcwd(), "mode": mode}
    if prompt.startswith("This is a permission probe."):
        record["role"] = "probe"
        probe(mode)
    elif prompt.startswith("Implement GitHub issue #"):
        record["role"] = "implementer"
        record.update(implement(prompt))
    elif prompt.startswith("Fix the following review findings on PR #"):
        record["role"] = "fixer"
        record.update(fix(prompt))
    else:
        record["role"] = "unrecognised"
        finish("Unrecognised prompt.\nNEEDS_HUMAN_DECISION")
    with open(os.path.join(STATE_DIR, "claude-calls.jsonl"), "a") as log:
        log.write(json.dumps(record) + "\n")
    return 0


def probe(mode: str) -> None:
    command = "git init"
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": command}},
    ]}})
    if mode == "dontAsk":
        denial = "Permission to use Bash with command git init has been denied."
        tool_result(denial, True)
        emit({"type": "result", "result": denial, "permission_denials": [
            {"tool_name": "Bash", "tool_use_id": "toolu_1", "tool_input": {"command": command}},
        ]})
        return
    done = subprocess.run(command.split(), capture_output=True, text=True)
    tool_result(done.stdout + done.stderr, done.returncode != 0)
    emit({"type": "result", "result": done.stdout, "permission_denials": []})


def implement(prompt: str) -> dict[str, object]:
    issue = must(r"Implement GitHub issue #(\d+)", prompt)[0]
    if ESCALATE in prompt:
        finish("The issue asks for a decision this engine cannot make.\nNEEDS_HUMAN_DECISION")
        return {"issue": int(issue), "escalated": True}
    remote = must(r"`git push (\S+) HEAD`", prompt)[0]
    body_file = must(r"write `([^`]+)` in the worktree root", prompt)[0]
    change = f"ladder/issue-{issue}.txt"
    os.makedirs("ladder", exist_ok=True)
    with open(change, "w") as out:
        out.write(f"L2 stub implementation for issue #{issue}.\n")
    git("add", change)
    git("commit", "-q", "-m", f"feat: L2 stub implementation for #{issue}")
    git("push", "-q", remote, "HEAD")
    with open(body_file, "w") as out:
        out.write(
            f"feat: L2 stub implementation for #{issue}\n\n"
            f"## Summary\n\nCloses #{issue}\n- Problem: the ladder needs a real PR.\n\n"
            "## Why this approach\n\n- How this solves the issue: one committed file.\n\n"
            f"## What changed\n\n- Structure: adds `{change}`.\n- Impact: none outside the ladder.\n\n"
            "## Test coverage\n\n- None: a canned engine wrote this change.\n\n"
            "## Test plan\n\n- [x] The L2 ladder stage asserts the pipeline's handling of it.\n"
        )
    finish(completion(prompt, "IMPLEMENTATION_COMPLETE"))
    return {"issue": int(issue), "remote": remote, "pushed": head()}


def fix(prompt: str) -> dict[str, object]:
    pr = must(r"Fix the following review findings on PR #(\d+)", prompt)[0]
    remote, head_ref = must(r"`git push (\S+) HEAD:refs/heads/([^`]+)`", prompt)
    skill_doc = must(r"read `([^`]+)`", prompt)[0]
    with open(skill_doc) as doc:
        skill_read = doc.read(200).startswith("---")
    change = f"ladder/pr-{pr}-fix.txt"
    os.makedirs("ladder", exist_ok=True)
    with open(change, "w") as out:
        out.write(f"L2 stub fix for PR #{pr}.\n")
    git("add", change)
    git("commit", "-q", "-m", f"fix: L2 stub revision for #{pr}")
    git("push", "-q", remote, f"HEAD:refs/heads/{head_ref}")
    finish(completion(
        prompt,
        "Finding: L2 stub finding\nDecision: repair\n"
        f"Change: added `{change}`.\n\nFIXES_PUSHED_RECHECK_NEEDED",
    ))
    return {"pr": int(pr), "skill_doc": skill_doc, "skill_read": skill_read, "pushed": head()}


def completion(prompt: str, text: str) -> str:
    """Put the waiver line before the sentinel when the dispatch asks for it."""
    if f"output the exact line {WAIVER}" not in prompt:
        return text
    body, sentinel = text.rsplit("\n", 1) if "\n" in text else ("", text)
    return f"{body}\n{WAIVER}\n{sentinel}".lstrip("\n")


def must(pattern: str, prompt: str) -> tuple[str, ...]:
    """Return the groups of a prompt element the stub depends on, or stop."""
    found = re.search(pattern, prompt)
    if found is None:
        sys.exit(f"L2 claude stub: the prompt no longer matches {pattern!r}")
    return found.groups()


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True)


def head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()


def emit(obj: dict[str, object]) -> None:
    print(json.dumps(obj), flush=True)


def tool_result(text: str, is_error: bool) -> None:
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": text, "is_error": is_error},
    ]}})


def finish(text: str) -> None:
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text})


if __name__ == "__main__":
    sys.exit(main())
