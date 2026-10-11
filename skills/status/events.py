"""Read retained local pipeline events without calling GitHub or a model."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_LINE_BYTES = 1024 * 1024
POLL_SECONDS = 1
HISTORY_NOTICE = "Retained local history only; earlier or other-host events and issue/PR linkage may be unavailable."
STARTUP_FAILURES = {
    "startup_failed", "startup_probe_failed", "startup_config_invalid",
    "startup_labels_invalid", "startup_repo_unconfigured", "startup_target_invalid",
    "startup_target_unconfigured",
}
KINDS = {
    "launch": "launch", "model_resolved": "_model", "engine_result_summary": "result",
    "reviewer_skill_observation": "result", "pr_handoff": "pr_handoff",
    "review_handoff": "review_handoff", "revision_handoff": "revision_handoff",
    "transient_revert": "retry", "cooldown_skip": "cooldown",
    "engine_stalled": "stalled", "notify_terminal_label": "terminal",
    "loop_quiescent_stop": "stopped", "tick_crash": "crash",
    **{action: "startup_failure" for action in STARTUP_FAILURES},
}


def clean(value: object) -> str:
    """Bound display fields and remove terminal control characters."""
    return "".join(c if c.isprintable() else " " for c in value)[:240] if isinstance(value, str) else ""


def number(value: object, maximum: float = 253402300799) -> int | float | None:
    if type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value):
        return value
    return None


def identity(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def timestamp(value: object) -> str:
    numeric = number(value)
    return dt.datetime.fromtimestamp(numeric, dt.timezone.utc).isoformat(timespec="seconds") if numeric is not None else "unknown"


def normalize(entry: object, event_id: str, repo: str) -> dict[str, Any] | None:
    """Project allowlisted observations; never interpret harvest intent as success."""
    if not isinstance(entry, dict) or number(entry.get("ts")) is None:
        raise ValueError("malformed event record")
    action = entry.get("action")
    if not isinstance(action, str):
        raise ValueError("malformed event action")
    kind = KINDS.get(action)
    if kind is None:
        return None
    details = entry.get("details", {})
    if not isinstance(details, dict):
        raise ValueError("malformed event details")
    item = identity(entry.get("pr"))
    if entry.get("pr") is not None and item is None:
        raise ValueError("malformed item identity")
    stage = clean(entry.get("stage"))
    if stage not in {"implement", "review", "fix", ""}:
        raise ValueError("malformed stage")
    confirmed_stage = {"pr_handoff": "implement", "review_handoff": "review", "revision_handoff": "fix"}.get(kind)
    if confirmed_stage and (stage != confirmed_stage or item is None):
        raise ValueError("malformed handoff identity")
    if kind in {"review_handoff", "revision_handoff", "terminal"} and not clean(details.get("label")):
        raise ValueError("malformed transition label")
    entity = "issue" if stage == "implement" else "pr" if item else "pipeline"
    if kind in {"startup_failure", "crash", "stopped"}:
        entity, item = "pipeline", None
    if kind == "terminal":
        terminal_entity = details.get("entity")
        if not isinstance(terminal_entity, str) or terminal_entity not in {"pr", "issue"} or item is None:
            raise ValueError("malformed terminal identity")
        entity = terminal_entity
    data: dict[str, Any] = {}
    actor = {"implement": "implementer", "review": "reviewer", "fix": "fixer"}.get(stage, "engine")
    if kind in {"launch", "_model"}:
        data = {"model": clean(details.get("model")) or None, "effort": clean(details.get("effort")) or None}
        message = f"{actor} launched" if kind == "launch" else "model resolved"
    elif kind == "result":
        usage = details.get("usage") if isinstance(details.get("usage"), dict) else {}
        cost = usage.get("reported_cost")
        reported_cost = None
        if isinstance(cost, dict) and cost.get("unit") == "USD" and number(cost.get("amount"), 1e12) is not None:
            reported_cost = {"amount": cost["amount"], "unit": "USD"}
        data = {"reported_cost": reported_cost, "duration_ms": number(details.get("duration_ms"), 2**63 - 1),
                "usage_status": clean(usage.get("status")) or "unavailable",
                "proposed_action": clean(details.get("harvest_action")) or None}
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = usage.get(key)
            data[key] = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
        message = f"{actor} result reported"
    elif kind == "pr_handoff":
        issue, pr = identity(details.get("issue_number")), identity(details.get("pr_number"))
        if issue != item or issue is None or pr is None or type(details.get("created")) is not bool:
            raise ValueError("malformed PR linkage")
        data = {"issue_number": issue, "pr_number": pr, "created": details["created"]}
        verb = "opened" if data["created"] else "linked"
        message = f"PR #{pr} {verb}; implementation handoff confirmed"
    elif kind in {"review_handoff", "revision_handoff"}:
        data = {"label": clean(details.get("label")), "outcome": clean(details.get("outcome")) or None,
                "head_sha": clean(details.get("head_sha")) or None}
        message = f"{'review' if kind == 'review_handoff' else 'revision'} handoff confirmed: {data['label']}"
    elif kind in {"retry", "cooldown"}:
        data = {"domain": clean(details.get("domain")) or None,
                "failure_class": clean(details.get("failure_class")) or None,
                "cooldown_until": number(details.get("cooldown_until"))}
        message = "retry queued" if kind == "retry" else "dispatch waiting on cooldown"
        if data["failure_class"]:
            message += f": {data['failure_class']}"
        message += f"; retry after {timestamp(data['cooldown_until'])}"
    elif kind == "stalled":
        data = {"progress_age_seconds": number(details.get("progress_age_seconds"))}
        message = f"{actor} stalled"
    elif kind == "terminal":
        data = {"label": clean(details.get("label"))}
        message = f"terminal label: {data['label']}"
    else:
        data = {"reason": clean(entry.get("reason")), "source_action": action}
        message = {"stopped": "loop stopped", "startup_failure": "startup failed", "crash": "tick crashed"}[kind]
        if data["reason"]:
            message += f": {data['reason']}"
    return {"schema_version": 1, "id": event_id, "repo": repo, "ts": entry["ts"],
            "event": kind, "entity": entity, "number": item, "stage": stage,
            "engine": clean(entry.get("engine")) or None,
            "dispatch_id": clean(entry.get("dispatch_id")) or None,
            "details": data, "message": message}


@dataclass
class Cursor:
    identity: tuple[int, int]
    generation: int = 0
    offset: int = 0
    dropping: bool = False
    partial: bool = False
    modified: tuple[int, int] | None = None
    digest: Any = field(default_factory=hashlib.sha256)
    events: list[dict[str, Any]] = field(default_factory=list)


class EventReader:
    """Incrementally read complete lines; retain only the current log window."""

    def __init__(self, log_dir: Path, repo: str, target: tuple[str, int] | None = None):
        self.log_dir, self.repo, self.target = log_dir, repo, target
        self.files: dict[Path, Cursor] = {}
        self.consumed: set[str] = set()
        self.warnings: set[str] = set()

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            print(f"events: {text}", file=sys.stderr, flush=True)
            self.warnings.add(text)

    def read(self, path: Path, known: dict[tuple[int, int], Cursor]) -> None:
        try:
            # Open before stat: rotation between discovery and open is harmless.
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                file_identity = (stat.st_dev, stat.st_ino)
                cursor = self.files.get(path)
                if cursor is None or cursor.identity != file_identity:
                    # Renames retain the cursor and record IDs of the same inode.
                    cursor = known.get(file_identity, cursor)
                modified = (stat.st_mtime_ns, stat.st_ctime_ns)
                rewritten = False
                if (cursor and cursor.identity == file_identity and cursor.offset
                        and cursor.modified != modified and stat.st_size >= cursor.offset):
                    # Size alone misses truncate-and-regrow between polls. Check
                    # consumed bytes only when the file changes; unchanged logs
                    # need no rehash or parse.
                    digest = hashlib.sha256()
                    remaining = cursor.offset
                    while remaining:
                        chunk = stream.read(min(remaining, 65536))
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
                    rewritten = remaining != 0 or digest.digest() != cursor.digest.digest()
                if cursor is None or cursor.identity != file_identity or stat.st_size < cursor.offset or rewritten:
                    generation = cursor.generation + 1 if cursor else 0
                    if cursor:
                        self.warn(f"{path.name} was replaced or truncated; history may be incomplete")
                    cursor = Cursor(file_identity, generation)
                self.files[path] = cursor
                known[file_identity] = cursor
                cursor.modified = modified
                stream.seek(cursor.offset)
                cursor.partial = False
                while True:
                    offset = stream.tell()
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE_BYTES:
                        self.warn(f"oversized record skipped in {path.name}")
                        cursor.dropping = not line.endswith(b"\n")
                        cursor.offset = stream.tell()
                        cursor.digest.update(line)
                        continue
                    if cursor.dropping:
                        cursor.dropping = not line.endswith(b"\n")
                        cursor.offset = stream.tell()
                        cursor.digest.update(line)
                        continue
                    if not line.endswith(b"\n"):
                        cursor.partial = True
                        break  # Leave the offset at the start until a newline arrives.
                    cursor.offset = stream.tell()
                    cursor.digest.update(line)
                    event_id = f"{stat.st_dev}:{stat.st_ino}:{cursor.generation}:{offset}"
                    try:
                        event = normalize(json.loads(line), event_id, self.repo)
                    except (ValueError, UnicodeError, RecursionError, OverflowError):
                        self.warn(f"malformed record skipped in {path.name}; history is incomplete")
                        continue
                    if event is not None:
                        cursor.events.append(event)
        except FileNotFoundError:
            return  # A concurrent tick pruned this file.
        except OSError as exc:
            raise OSError(f"cannot read {path.name}: {exc.strerror}") from exc

    def poll(self) -> list[dict[str, Any]]:
        try:
            paths = sorted(p for p in self.log_dir.iterdir() if p.name.startswith("tick-") and p.suffix == ".jsonl")
        except FileNotFoundError:
            paths = []
        if not paths:
            self.warn("no retained tick logs found; history is unavailable")
        # Keep the old inode map for the whole scan: a new file can reuse the
        # original pathname before the renamed old file is visited.
        known = {cursor.identity: cursor for cursor in self.files.values()}
        for path in paths:
            self.read(path, known)
        self.files = {path: cursor for path, cursor in self.files.items() if path in paths}
        # Timestamps have second precision. Break ties by file and numeric
        # record position, never by the decimal offset embedded in an opaque ID.
        ordered = [(event["ts"], path.name, index, event)
                   for path, cursor in self.files.items()
                   for index, event in enumerate(cursor.events)]
        events = [item[3] for item in sorted(ordered, key=lambda item: item[:3])]
        routes = {(e["entity"], e["number"], e["stage"], e["dispatch_id"]): e["details"]
                  for e in events if e["event"] == "_model" and e["dispatch_id"]}
        for event in events:
            if event["event"] == "launch" and event["dispatch_id"]:
                route = routes.get((event["entity"], event["number"], event["stage"], event["dispatch_id"]), {})
                for key in ("model", "effort"):
                    if event["details"][key] is None:
                        event["details"][key] = route.get(key)
        events = [event for event in events if event["event"] != "_model"]
        self.consumed.intersection_update(e["id"] for e in events)
        linked = {e["details"]["pr_number"] for e in events if e["event"] == "pr_handoff"
                  and self.target == ("issue", e["details"]["issue_number"])}
        last: dict[tuple[str, int | None, str], str] = {}
        attempts: set[tuple[tuple[str, int | None, str], str]] = set()
        output = []
        for event in events:
            scope = (event["entity"], event["number"], event["stage"])
            signature = json.dumps([event["event"], event["dispatch_id"], event["engine"], event["details"]], sort_keys=True)
            # Only cooldowns represent state that can persist across ticks.
            # Failures, stops, and observations without a dispatch identity
            # are separate occurrences, even when their details are equal.
            duplicate = event["event"] == "cooldown" and last.get(scope) == signature
            last[scope] = signature
            if event["dispatch_id"] and event["event"] in {
                "launch", "result", "pr_handoff", "review_handoff", "revision_handoff", "retry", "stalled",
            }:
                # A harvest retry can repeat its result and confirmation. A
                # rediscovered PR is the same handoff, even if created is false.
                data = dict(event["details"])
                if event["event"] == "pr_handoff":
                    data.pop("created", None)
                attempt = (scope, json.dumps([event["event"], event["dispatch_id"], data], sort_keys=True))
                duplicate = duplicate or attempt in attempts
                attempts.add(attempt)
            selected = (self.target is None or event["entity"] == "pipeline"
                        or self.target == (event["entity"], event["number"])
                        or (event["entity"] == "pr" and event["number"] in linked))
            if selected:
                if event["id"] not in self.consumed and not duplicate:
                    output.append(event)
                self.consumed.add(event["id"])
        return output


def render(event: dict[str, Any]) -> str:
    item = f"{event['entity']} #{event['number']}" if event["number"] else "pipeline"
    extras = []
    if event["engine"]:
        extras.append(event["engine"])
    data = event["details"]
    if data.get("model"):
        extras.append(data["model"])
    if event["event"] == "result":
        cost, duration = data["reported_cost"], data["duration_ms"]
        extras.append(f"reported cost {cost['amount']:g} USD" if cost is not None else "cost unknown")
        extras.append(f"duration {duration / 1000:g}s" if duration is not None else "duration unknown")
        for key, label in (("input_tokens", "input"), ("cached_input_tokens", "cached input"), ("output_tokens", "output")):
            if data[key] is not None:
                extras.append(f"{label} tokens {data[key]}")
    suffix = f" ({'; '.join(extras)})" if extras else ""
    return f"{timestamp(event['ts'])} {item} {event['message']}{suffix}"


def parse_target(raw: str) -> tuple[str, int]:
    match = re.fullmatch(r"(#|pr:)([1-9][0-9]{0,18})", raw)
    if match and identity(int(match[2])):
        return ("issue" if match[1] == "#" else "pr", int(match[2]))
    raise argparse.ArgumentTypeError("target must be '#N' or pr:N with a positive number")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--log-dir", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="emit versioned JSON lines")
    parser.add_argument("--follow", action="store_true", help="replay retained history, then follow new records")
    parser.add_argument("target", nargs="?", type=parse_target, help="'#N' (issue and known linked PRs) or pr:N")
    args = parser.parse_args()
    print(HISTORY_NOTICE, file=sys.stderr, flush=True)
    reader = EventReader(args.log_dir, args.repo, args.target)
    try:
        while True:
            for event in reader.poll():
                print(json.dumps(event, allow_nan=False) if args.json else render(event), flush=True)
            if not args.follow:
                if any(cursor.partial or cursor.dropping for cursor in reader.files.values()):
                    print("events: partial final record omitted; use --follow to wait for completion", file=sys.stderr)
                return 0
            time.sleep(POLL_SECONDS)
    except OSError as exc:
        print(f"events: history unavailable: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
