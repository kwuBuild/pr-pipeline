"""Canned `codex` for ladder stage L2: a reviewer that rejects once.

Installed first on PATH by l2-stubbed-tick.sh, with ``STATE_DIR`` rewritten to
an absolute path. The reviewer launches under a scrubbed environment, so the
path is baked in rather than read from one.

`codex exec` writes a schema-valid review to the `-o` path the engine wrapper
supplies. The first review of a PR requests changes with one major finding;
every later review of it is clean. That forces the fixer and the re-review, the
part of the cycle a happy path never reaches.
"""

from __future__ import annotations

import json
import os
import re
import sys

STATE_DIR = "@STATE_DIR@"


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print("codex-cli 0.142.5")
        return 0
    if argv[:1] != ["exec"] or "-o" not in argv:
        print(f"L2 codex stub: unexpected call {argv!r}", file=sys.stderr)
        return 2
    # The engine wrapper appends `-o <temp>` after the prompt, so the prompt is
    # the last argument once that pair is removed.
    at = argv.index("-o")
    output = argv[at + 1]
    prompt = (argv[:at] + argv[at + 2:])[-1]
    found = re.search(r"Review PR #(\d+)", prompt)
    if found is None:
        sys.exit("L2 codex stub: the review prompt no longer names its PR")
    pr = found.group(1)
    count_file = os.path.join(STATE_DIR, f"reviews-pr-{pr}")
    count = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
    with open(count_file, "w") as out:
        out.write(str(count))
    review: dict[str, object] = {"verdict": "ready_for_human", "findings": []}
    if count == 1:
        review = {"verdict": "changes_requested", "findings": [{
            "severity": "major",
            "file": "ladder",
            "summary": "L2 stub finding: the first review of every PR requests changes.",
        }]}
    with open(output, "w") as out:
        json.dump(review, out)
    with open(os.path.join(STATE_DIR, "codex-calls.jsonl"), "a") as log:
        log.write(json.dumps({"pr": int(pr), "review": count, "verdict": review["verdict"],
                              "cwd": os.getcwd()}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
