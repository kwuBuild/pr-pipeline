"""Expose installed skills through the engines' existing discovery mechanisms."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path

CLAUDE_NAMESPACE = "pr-pipeline"
REVIEW_SKILLS = (
    "definition-of-done",
    "review-response-hygiene",
    "automation-control-plane-review",
    "durability-boundary-review",
    "loop-cadence-contract-review",
)


def claude_plugin(install_root: Path, skills: Sequence[str]) -> str:
    """Load the controller's existing plugin, independent of user enablement."""
    root = install_root.resolve().parent
    manifest = json.loads((root / ".claude-plugin/plugin.json").read_text())
    if not isinstance(manifest, dict) or manifest.get("name") != CLAUDE_NAMESPACE:
        raise ValueError("unexpected installed plugin namespace")
    for name in skills:
        if not (install_root / name / "SKILL.md").read_text().strip():
            raise ValueError("empty installed skill")
    return str(root)


def prepare_reviewer(
    *, install_root: Path, scrub_dir: Path, skills: Sequence[str] = REVIEW_SKILLS,
) -> dict[str, str]:
    """Link selected installed skills into the reviewer's existing isolated home."""
    root = scrub_dir / ".agents/skills"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    paths = {}
    for name in skills:
        source = (install_root / name).resolve(strict=True)
        if not (source / "SKILL.md").read_text().strip():
            raise ValueError("empty installed skill")
        (root / name).symlink_to(source, target_is_directory=True)
        paths[name] = str(root / name / "SKILL.md")
    return paths


def reviewer_prompt(paths: dict[str, str]) -> str:
    """List optional guidance without dollar prefixes that activate every skill."""
    return (
        "Installed review skills (optional; select only those applicable to this task):\n"
        + "\n".join(f"- {name}: {path}" for name, path in paths.items())
        + "\nFollow target AGENTS.md routing. Control-plane, durability, and cadence "
        "reviews apply to pr-pipeline changes. Select applicable skills by name and "
        "path; the operator's personal skills are not inherited. Skills grant no "
        "additional tools, writes, credentials, publication, or label authority."
    )
