"""Record every file an audited Python process opens (ladder stage L2).

Loaded through PYTHONPATH. Appends ``{"path", "cwd"}`` to $OPEN_AUDIT_LOG for
each `open` audit event, which covers builtins.open, io.open, pathlib reads and
os.open. A file-descriptor argument is not a path and is skipped. The event
omits `dir_fd`, so the relative names shutil.rmtree opens beneath a directory
descriptor cannot be resolved against the cwd, and they are skipped too.

The suite's test_setup_and_tick_open_no_file_in_the_target_tree_but_pipeline_state
uses the same hook in-process; this copy follows a real tick against GitHub.
"""

import json
import os
import sys

_LOG = os.environ.get("OPEN_AUDIT_LOG", "")


def _hook(event, args):
    if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = os.fsdecode(args[0])
    if path == _LOG:
        return
    if not os.path.isabs(path) and sys._getframe(1).f_code.co_filename.endswith(
        os.sep + "shutil.py"
    ):
        return
    with open(_LOG, "a") as out:
        out.write(json.dumps({"path": path, "cwd": os.getcwd()}) + "\n")


if _LOG:
    sys.addaudithook(_hook)
