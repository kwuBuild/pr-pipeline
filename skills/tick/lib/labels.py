"""Additive label setup and read-only vocabulary checks for a resolved target."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pipeline


def _gh(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True, timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(exc.stderr.strip() or str(exc)) from exc
    return result.stdout


def read_labels(repo: str) -> frozenset[str]:
    """Read every repository label, including labels beyond GitHub's first page."""
    pages = json.loads(_gh(
        "api", f"repos/{repo}/labels?per_page=100", "--hostname", "github.com",
        "--paginate", "--slurp",
    ))
    if not isinstance(pages, list) or not all(
        isinstance(page, list) and all(
            isinstance(label, dict) and isinstance(label.get("name"), str)
            and label["name"] for label in page
        ) for page in pages
    ):
        raise ValueError(f"invalid label response for {repo}")
    return frozenset(label["name"] for page in pages for label in page)


def provision(repo: str) -> frozenset[str]:
    """Create only missing vocabulary labels; never update existing metadata."""
    existing = read_labels(repo)
    for name in sorted(pipeline.WORKFLOW_LABELS - existing):
        try:
            _gh("label", "create", name, "--repo", f"github.com/{repo}")
        except ValueError as exc:
            # Another setup may have created it after our initial read. Do
            # not infer success from error text or overwrite its metadata.
            if name not in read_labels(repo):
                raise ValueError(f"could not create {name} on {repo}: {exc}") from exc
    return frozenset(name for name in read_labels(repo) - pipeline.WORKFLOW_LABELS
                     if name.startswith("ai:"))


def verify(repo: str) -> None:
    """Refuse a tick before its first transition if the vocabulary is missing."""
    missing = pipeline.WORKFLOW_LABELS - read_labels(repo)
    if missing:
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "provision"])
        raise ValueError(
            f"missing labels on {repo}: {', '.join(sorted(missing))}. "
            f"From that repository, run: {command}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["vocabulary", "provision"])
    args = parser.parse_args(argv)
    if args.command == "vocabulary":
        print(json.dumps(sorted(pipeline.WORKFLOW_LABELS)))
        return 0
    try:
        target = pipeline.resolve_target()
        unknown = provision(target.repo)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"labels: {exc}", file=sys.stderr)
        return 3
    print(f"Label vocabulary provisioned for {target.repo}.")
    if unknown:
        print("Unknown ai: labels (left unchanged): " + ", ".join(sorted(unknown)))
        print("Items carrying only unknown ai: labels are parked: they are not "
              "selected by any dispatch query and go dark to the pipeline. "
              "Only the repository owner decides how to reconcile them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
