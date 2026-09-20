"""Bounded, sanitized skill and usage evidence for headless Codex reviewers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from os import PathLike
from typing import BinaryIO

from engine_usage import codex_usage, valid_codex_usage

_SKILL_NAME_PATTERN = r"[A-Za-z0-9._-]{1,128}"
_SKILL_NAME_RE = re.compile(_SKILL_NAME_PATTERN)
_TOKEN_START = r"(?:^|[\s'\"=(])"
_PATH_PREFIX = r"/?(?:[^\s'\";|&<>()/]+/)*"
_TOKEN_END = r"(?=$|[\s'\";|&<>()])"
_REPOSITORY_SKILL_RE = re.compile(
    rf"{_TOKEN_START}{_PATH_PREFIX}(?:\.agents|\.claude)/skills/"
    rf"(?P<name>{_SKILL_NAME_PATTERN})/SKILL\.md{_TOKEN_END}",
)
_PLUGIN_SKILL_RE = re.compile(
    rf"{_TOKEN_START}{_PATH_PREFIX}plugins/"
    rf"(?:[^\s'\";|&<>()/]+/)*skills/"
    rf"(?P<name>{_SKILL_NAME_PATTERN})/SKILL\.md{_TOKEN_END}",
)
_MAX_OBSERVED_SKILLS = 64
_MAX_COUNTER = 1_000_000_000
_OBSERVATION_KEYS = {
    "schema_version",
    "status",
    "evidence_contract",
    "explicit_invocation_events_supported",
    "observed_skills",
    "event_count",
    "parse_errors",
    "oversized_events_skipped",
    "skills_truncated",
}


class _ObservationBuilder:
    def __init__(self) -> None:
        self.event_count = 0
        self.parse_errors = 0
        self.oversized_events_skipped = 0
        self.skills_truncated = 0
        self.observed: dict[tuple[str, str], dict[str, str]] = {}
        self.usage = codex_usage(None)
        self.terminal_count = 0

    def observe(self, raw_line: bytes) -> None:
        self.event_count += 1
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError, RecursionError):
            self.parse_errors += 1
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "turn.completed":
            self.terminal_count += 1
            self.usage = codex_usage(event.get("usage"))
            return
        if event.get("type") != "item.completed":
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        exit_code = item.get("exit_code")
        if (
            item.get("type") != "command_execution"
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or exit_code != 0
        ):
            return
        command = item.get("command")
        if not isinstance(command, str):
            return
        for match in _REPOSITORY_SKILL_RE.finditer(command):
            self._record("repository", match.group("name"))
        for match in _PLUGIN_SKILL_RE.finditer(command):
            self._record("plugin", match.group("name"))

    def skip_oversized(self) -> None:
        self.event_count += 1
        self.oversized_events_skipped += 1

    def payload(self) -> dict[str, object]:
        partial = (
            self.parse_errors > 0
            or self.oversized_events_skipped > 0
            or self.skills_truncated > 0
        )
        usage = dict(self.usage)
        if (partial or self.terminal_count > 1) and usage["status"] == "complete":
            usage["status"] = "partial"
        return {
            "schema_version": 2,
            "status": "partial" if partial else "complete",
            "evidence_contract": "successful_command_referencing_skill_file",
            "explicit_invocation_events_supported": False,
            "observed_skills": sorted(
                self.observed.values(),
                key=lambda skill: (skill["source"], skill["name"]),
            ),
            "event_count": self.event_count,
            "parse_errors": self.parse_errors,
            "oversized_events_skipped": self.oversized_events_skipped,
            "skills_truncated": self.skills_truncated,
            "usage": usage,
        }

    def _record(self, source: str, name: str) -> None:
        if _SKILL_NAME_RE.fullmatch(name) is None or name in {".", ".."}:
            return
        key = (source, name)
        if key in self.observed:
            return
        if len(self.observed) >= _MAX_OBSERVED_SKILLS:
            self.skills_truncated += 1
            return
        self.observed[key] = {"name": name, "source": source}


def observe_event_lines(lines: Iterable[bytes]) -> dict[str, object]:
    """Return sanitized skill references and terminal usage from Codex JSONL.

    Args:
        lines: Raw JSONL lines emitted by ``codex exec --json``.

    Returns:
        A strict, serialization-safe observation payload that excludes raw event
        content, commands, command output, prompts, and absolute paths.
    """
    builder = _ObservationBuilder()
    for raw_line in lines:
        builder.observe(raw_line)
    return builder.payload()


def observe_event_stream(
    stream: BinaryIO,
    *,
    max_event_bytes: int = 256 * 1024,
) -> dict[str, object]:
    """Drain a Codex JSONL stream while bounding memory per logical event.

    Args:
        stream: Binary stdout pipe from ``codex exec --json``.
        max_event_bytes: Maximum bytes parsed for one JSONL event.

    Returns:
        A sanitized observation payload. Oversized events are drained, counted,
        and omitted without preventing later events from being inspected.
    """
    builder = _ObservationBuilder()
    while True:
        chunk = stream.readline(max_event_bytes + 1)
        if not chunk:
            break
        if len(chunk) <= max_event_bytes:
            builder.observe(chunk)
            continue

        builder.skip_oversized()
        while chunk and not chunk.endswith(b"\n"):
            chunk = stream.readline(max_event_bytes + 1)
    return builder.payload()


def unavailable_observation() -> dict[str, object]:
    """Return the fixed payload used when no trustworthy summary is available."""
    return {
        "schema_version": 2,
        "status": "unavailable",
        "evidence_contract": "successful_command_referencing_skill_file",
        "explicit_invocation_events_supported": False,
        "observed_skills": [],
        "event_count": 0,
        "parse_errors": 0,
        "oversized_events_skipped": 0,
        "skills_truncated": 0,
        "usage": codex_usage(None),
    }


def load_observation(path: str | PathLike[str]) -> dict[str, object]:
    """Load only the exact sanitized observation shape used by HARVEST."""
    try:
        with open(path) as fh:
            # 64 skill names and one fixed usage record fit comfortably here.
            raw = fh.read(32 * 1024 + 1)
        if len(raw) > 32 * 1024:
            return unavailable_observation()
        payload = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return unavailable_observation()
    if not _valid_observation(payload):
        return unavailable_observation()
    return payload


def _valid_observation(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    version = payload.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        return False
    keys = _OBSERVATION_KEYS | {"usage"} if version == 2 else _OBSERVATION_KEYS
    if set(payload) != keys:
        return False
    if version == 2 and not valid_codex_usage(payload.get("usage")):
        return False
    if (
        payload.get("evidence_contract")
        != "successful_command_referencing_skill_file"
    ):
        return False
    if payload.get("explicit_invocation_events_supported") is not False:
        return False

    status = payload.get("status")
    if not isinstance(status, str) or status not in {"complete", "partial", "unavailable"}:
        return False
    counters = (
        payload.get("event_count"),
        payload.get("parse_errors"),
        payload.get("oversized_events_skipped"),
        payload.get("skills_truncated"),
    )
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= _MAX_COUNTER
        for value in counters
    ):
        return False
    event_count, parse_errors, oversized_events, skills_truncated = counters
    if parse_errors + oversized_events > event_count:
        return False

    observed = payload.get("observed_skills")
    if not isinstance(observed, list) or len(observed) > _MAX_OBSERVED_SKILLS:
        return False
    identities: set[tuple[str, str]] = set()
    for skill in observed:
        if not isinstance(skill, dict) or set(skill) != {"name", "source"}:
            return False
        name = skill.get("name")
        source = skill.get("source")
        if (
            not isinstance(name, str)
            or _SKILL_NAME_RE.fullmatch(name) is None
            or name in {".", ".."}
        ):
            return False
        if not isinstance(source, str) or source not in {"repository", "plugin"}:
            return False
        identity = (source, name)
        if identity in identities:
            return False
        identities.add(identity)

    degraded = parse_errors > 0 or oversized_events > 0 or skills_truncated > 0
    if status == "complete" and degraded:
        return False
    if status == "partial" and not degraded:
        return False
    if status == "unavailable" and (observed or any(counters)):
        return False
    if version == 2:
        usage_status = payload["usage"]["status"]
        if status == "unavailable" and usage_status != "unavailable":
            return False
        if status == "partial" and usage_status == "complete":
            return False
    return True
