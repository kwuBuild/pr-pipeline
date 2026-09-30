"""Summarise a `claude -p --output-format stream-json` transcript as JSON.

Prints ``{"bash": [{"command", "is_error", "output"}], "cost_usd", "result"}``:
every Bash call the session made, in order, paired with its tool result.
Rejects malformed/incomplete sessions. With ``--dashboard-command``, also
requires exactly that command and successful dashboard output.
"""

from __future__ import annotations

import argparse
import json
import sys


def read_session(path: str) -> tuple[list[dict], dict]:
    """Pair requests and results within one strictly parsed, completed stream."""
    calls: dict[str, dict[str, object]] = {}
    terminal = None
    with open(path) as stream:
        for number, line in enumerate(stream, 1):
            event = json.loads(line)
            if not isinstance(event, dict) or terminal is not None:
                raise ValueError(f"invalid event after line {number - 1}")
            content = event.get("message", {}).get("content")
            if event.get("type") == "assistant" and isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        identity = block.get("id")
                        if not isinstance(identity, str) or not identity or identity in calls:
                            raise ValueError("missing or duplicate tool-use ID")
                        calls[identity] = {
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                            "is_error": None,
                            "output": "",
                        }
            elif event.get("type") == "user" and isinstance(content, list):
                for block in content:
                    call = calls.get(block.get("tool_use_id", ""))
                    if block.get("type") == "tool_result":
                        if call is None or call["is_error"] is not None:
                            raise ValueError("unmatched or duplicate tool result")
                        error = block.get("is_error", False)
                        if not isinstance(error, bool):
                            raise ValueError("invalid tool-result is_error")
                        call["is_error"] = error
                        call["output"] = text_of(block.get("content"))
            elif event.get("type") == "result":
                terminal = event
    if terminal is None or terminal.get("subtype") != "success" or terminal.get("is_error") is not False:
        raise ValueError("session has no successful completion")
    if any(call["is_error"] is None for call in calls.values()):
        raise ValueError("session has a missing tool result")
    return list(calls.values()), terminal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--dashboard-command")
    args = parser.parse_args()
    try:
        calls, terminal = read_session(args.path)
        bash = [
            {"command": call["input"].get("command", ""),
             "is_error": call["is_error"], "output": call["output"]}
            for call in calls if call["name"] == "Bash"
        ]
        if args.dashboard_command and not (
            len(bash) == 1 and bash[0]["command"] == args.dashboard_command
            and bash[0]["is_error"] is False and "PR PIPELINE" in bash[0]["output"]
        ):
            raise ValueError("expected dashboard command has no successful output")
        print(json.dumps({"bash": bash, "cost_usd": terminal.get("total_cost_usd"),
                          "result": terminal["subtype"]}))
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(f"invalid session evidence: {error}", file=sys.stderr)
        return 1
    return 0


def text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


if __name__ == "__main__":
    sys.exit(main())
