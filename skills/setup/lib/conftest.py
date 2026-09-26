"""Shared pytest fixtures for the pr-pipeline setup skill tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Setup reuses the tick's resolver, label provisioning and engine launch
# definitions rather than keeping copies, so its tests import them the same way.
TICK_LIB = Path(__file__).resolve().parents[2] / "tick" / "lib"
if str(TICK_LIB) not in sys.path:
    sys.path.insert(0, str(TICK_LIB))


@pytest.fixture(autouse=True)
def isolate_operator_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Point every file setup may write at a per-test directory.

    Setup writes the operator config, its probe evidence beside it, and the
    Claude user settings when the operator accepts deny rules. A test that
    reached the developer's real files would edit their permissions. The
    packaged floor is pinned to a missing file so a developer's checkout
    cannot change what a test resolves.

    Real ``gh`` and ``claude`` are shadowed by stubs that fail loudly: a test
    that forgets its double must not reach GitHub or spend a model call.
    """
    import pipeline

    home = tmp_path_factory.mktemp("operator-home")
    claude_home = home / ".claude"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv(
        "PR_PIPELINE_CONFIG", str(claude_home / "pr-pipeline" / "config.toml"),
    )
    monkeypatch.delenv("PIPELINE_TRUSTED_AUTHOR", raising=False)
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    monkeypatch.delenv("PR_PIPELINE_HOME", raising=False)
    monkeypatch.setattr(
        pipeline, "_PACKAGED_CONFIG_PATH", Path("/nonexistent/config.toml"),
    )
    guard = home / "guard-bin"
    guard.mkdir()
    for name in ("gh", "claude"):
        stub = guard / name
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "unexpected real {name} call: $*" >&2\n'
            "exit 97\n"
        )
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{guard}{os.pathsep}{os.environ['PATH']}")
    return claude_home
