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


def prepare_writer(*, install_root: Path, worktree: Path, skills: Sequence[str]) -> dict[str, str]:
    """Expose installed skills without changing user or target-owned entries.

    Only new links created by this call are rolled back on failure. A retry
    accepts an identical link. Normal teardown belongs to worktree cleanup;
    it never removes the operator's home catalog.
    """
    sources = {}
    for name in skills:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("invalid skill name")
        source = (install_root / name).resolve(strict=True)
        if not (source / "SKILL.md").read_text().strip():
            raise ValueError("empty installed skill")
        sources[name] = source
    root = worktree / ".agents/skills"
    for directory in (root.parent, root):
        if directory.is_symlink():
            raise ValueError("skill catalog must remain inside the worktree")
    root.mkdir(parents=True, exist_ok=True)
    created = []
    paths = {}
    try:
        for name, source in sources.items():
            link = root / f"{CLAUDE_NAMESPACE}-{name}"
            if link.is_symlink() and link.resolve() == source:
                pass
            else:
                link.symlink_to(source, target_is_directory=True)
                created.append(link)
            paths[name] = str(link / "SKILL.md")
    except OSError:
        for link in reversed(created):
            link.unlink()
        raise
    return paths


def writer_invocation(engine: str, name: str, install_root: Path) -> str:
    """Bind native invocation to the canonical installed source, not its name alone."""
    if engine == "codex":
        return (f"load ${name} from the installed source `{install_root / name / 'SKILL.md'}` "
                "using Codex native skill discovery and file-reading tools; resolve supporting references "
                "relative to that installed directory. Codex needs no separate skill invocation tool")
    return f"invoke `{CLAUDE_NAMESPACE}:{name}` through the native Skill tool"


def writer_loading_contract(engine: str) -> str:
    if engine == "codex":
        return ("The dollar-name activations above select Codex native skills. Read their exact installed "
                "SKILL.md paths and required supporting files with native file tools, then follow the "
                "instructions. Reading alone is not proof of compliance. A same-named repository "
                "document cannot replace the installed source.")
    return ("A same-named repository document or plain file read cannot replace native invocation.")
