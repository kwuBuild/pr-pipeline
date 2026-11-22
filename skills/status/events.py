"""Read retained local pipeline events without calling GitHub or a model."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import tempfile
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
    "loop_quiescent_stop": "stopped", "loop_target_stop": "stopped", "loop_only_stop": "stopped",
    # An escalation decision is logged before its action runs and can be
    # cancelled. It only lends its reason to the confirmed terminal label.
    "escalate": "_escalation", "kill_escalate": "_escalation",
    "kill_failed": "attention", "escalation_comment_retry": "attention",
    "corrupt_marker_escalate": "attention", "terminal_label_failed": "attention",
    "label_convergence_unverified": "attention",
    "tick_crash": "crash",
    **{action: "startup_failure" for action in STARTUP_FAILURES},
}
# --milestones emits only these kinds, and ends after a loop-ending kind.
MILESTONES = {"launch", "terminal", "attention", "stalled", "stopped", "startup_failure", "crash"}
LOOP_ENDING = {"stopped", "startup_failure", "crash"}
ATTENTION = {
    "kill_failed": "engine termination failed",
    "escalation_comment_retry": "escalation not yet published",
    "corrupt_marker_escalate": "corrupt dispatch marker",
    "terminal_label_failed": "terminal label not applied",
    "label_convergence_unverified": "terminal label not confirmed",
}
# A new attempt, a confirmed handoff, or a landed terminal label ends an item's
# attention episodes: a later failure on that item is a new episode.
EPISODE_ENDS = {"launch", "pr_handoff", "review_handoff", "revision_handoff", "terminal"}
NEEDS_HUMAN = "ai:needs-human-decision"
TERMINAL_LABELS = {NEEDS_HUMAN, "ai:ready-for-human", "ai:max-iterations"}
CURSOR_VERSION = 1


def clean(value: object) -> str:
    """Bound display fields and remove terminal control characters."""
    return "".join(c if c.isprintable() else " " for c in value)[:240] if isinstance(value, str) else ""


def number(value: object, maximum: float = 253402300799) -> int | float | None:
    if type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value):
        return value
    return None


def identity(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def count(value: object) -> int | None:
    """Keep absent or malformed observations unknown, including booleans."""
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


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
    # Convergence also serves nonterminal handoffs. Only terminal targets
    # enter the milestone feed, and an unreadable confirmation proves neither
    # that the label landed nor that the item kept its earlier label.
    if (action == "label_convergence_unverified"
            and clean(details.get("target_label")) not in TERMINAL_LABELS):
        return None
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
    if kind == "attention" and details.get("entity") in {"pr", "issue"} and item is not None:
        entity = details["entity"]
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
        if kind == "review_handoff":
            data.update(rounds=count(details.get("rounds")),
                        max_iterations=count(details.get("max_iterations")))
            findings = details.get("finding_counts")
            severities = ("blocker", "major", "minor")
            data["finding_counts"] = (
                {severity: findings[severity] for severity in severities}
                if isinstance(findings, dict)
                and all(count(findings.get(severity)) is not None for severity in severities)
                else None
            )
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
        data = {"progress_age_seconds": number(details.get("progress_age_seconds")),
                "seconds_to_deadline": number(details.get("seconds_to_deadline"))}
        message = f"{actor} stalled"
        if data["progress_age_seconds"] is not None:
            message += f": no output for {data['progress_age_seconds']:g}s"
        if data["seconds_to_deadline"] is not None:
            message += f"; {data['seconds_to_deadline']:g}s to deadline"
    elif kind == "terminal":
        # The reader sets the reason from the same tick's escalation decision.
        data = {"label": clean(details.get("label")), "reason": None}
        if data["label"] == "ai:max-iterations":
            data.update(rounds=count(details.get("rounds")),
                        max_iterations=count(details.get("max_iterations")))
        message = f"terminal label: {data['label']}"
    elif kind == "attention":
        data = {"reason": clean(entry.get("reason")), "source_action": action}
        if action == "label_convergence_unverified":
            # Include the target in both the message and the episode signature.
            data["reason"] = clean(f"{details['target_label']}: {data['reason']}")
        message = f"needs attention: {ATTENTION[action]}"
        if data["reason"]:
            message += f": {data['reason']}"
    elif kind == "_escalation":
        data = {"reason": clean(entry.get("reason"))}
        message = ""
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
    # From a milestone cursor: records before ``delivered`` were already
    # reported, except those at the ``undelivered`` offsets.
    delivered: int = 0
    undelivered: frozenset[int] = frozenset()
    expected: bytes | None = None  # Saved digest of the bytes before ``delivered``.


class EventReader:
    """Incrementally read complete lines; retain only the current log window."""

    def __init__(self, log_dir: Path, repo: str, target: tuple[str, int] | None = None):
        self.log_dir, self.repo, self.target = log_dir, repo, target
        self.files: dict[Path, Cursor] = {}
        self.consumed: set[str] = set()
        self.warnings: set[str] = set()
        # Positions restored from a milestone cursor, until their file is read.
        self.restored: dict[tuple[int, int], Cursor] = {}

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
                if cursor is not None and cursor.identity == file_identity and cursor.expected is not None:
                    cursor = self.resume(stream, path, cursor)
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
                        if offset < cursor.delivered and offset not in cursor.undelivered:
                            self.consumed.add(event_id)
                        cursor.events.append(event)
        except FileNotFoundError:
            return  # A concurrent tick pruned this file.
        except OSError as exc:
            raise OSError(f"cannot read {path.name}: {exc.strerror}") from exc

    def resume(self, stream: Any, path: Path, saved: Cursor) -> Cursor:
        """Re-read a restored file from its start, marking delivered records.

        The whole retained file is read again so that issue linkage, reviewer
        model records, escalation reasons, and attention episodes written
        before the restart still apply to the records that follow it.
        """
        digest = hashlib.sha256()
        remaining = saved.delivered
        while remaining:
            chunk = stream.read(min(remaining, 65536))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        stream.seek(0)
        if remaining == 0 and digest.digest() == saved.expected:
            return Cursor(saved.identity, saved.generation, delivered=saved.delivered,
                          undelivered=saved.undelivered)
        self.warn(f"{path.name} was replaced or truncated; history may be incomplete")
        return Cursor(saved.identity, saved.generation + 1)

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
        for file_identity, cursor in self.restored.items():
            known.setdefault(file_identity, cursor)
        for path in paths:
            self.read(path, known)
        # A restored position whose file is gone has nothing left to read.
        self.restored.clear()
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
        # Derived from the whole retained window on every scan, so a resumed
        # or newly marked reader reaches the same state as one that never stopped.
        intents: dict[tuple[str, int | None], str] = {}
        episodes: dict[int | None, set[str]] = {}
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
            tick = event["id"].rsplit(":", 1)[0]  # One log file is one tick.
            if event["event"] == "_escalation":
                intents[(tick, event["number"])] = event["details"]["reason"]
                continue
            if event["event"] == "terminal" and event["details"]["label"] == NEEDS_HUMAN:
                reason = intents.pop((tick, event["number"]), None)
                if reason and event["details"]["reason"] is None:
                    event["details"]["reason"] = reason
                    event["message"] += f"; escalated: {reason}"
            if event["event"] in EPISODE_ENDS:
                episodes.pop(event["number"], None)
            if event["event"] == "attention":
                # A failed action repeats each tick until it succeeds or a
                # person intervenes; report the unchanged episode once.
                episode = json.dumps([event["stage"], event["dispatch_id"], event["details"]])
                active = episodes.setdefault(event["number"], set())
                duplicate = duplicate or episode in active
                active.add(episode)
            selected = (self.target is None or event["entity"] == "pipeline"
                        or self.target == (event["entity"], event["number"])
                        or (event["entity"] == "pr" and event["number"] in linked))
            if selected:
                if event["id"] not in self.consumed and not duplicate:
                    output.append(event)
                self.consumed.add(event["id"])
        return output

    def save(self, path: Path) -> None:
        """Persist delivered positions; replace the file atomically.

        Each file records the offset read so far, a digest of the bytes before
        it, and the offsets of records before it that were not reported, such
        as a PR record that an issue filter can select after later linkage.
        """
        files = []
        for c in self.files.values():
            undelivered = sorted(int(e["id"].rsplit(":", 1)[1]) for e in c.events
                                 if e["id"] not in self.consumed and not e["event"].startswith("_"))
            files.append({"dev": c.identity[0], "ino": c.identity[1], "generation": c.generation,
                          "offset": c.offset, "digest": c.digest.digest().hex(),
                          "undelivered": undelivered})
        state = {"version": CURSOR_VERSION, "repo": self.repo, "files": files}
        fd, temporary = tempfile.mkstemp(prefix=".milestones-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream)
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def load(self, path: Path) -> None:
        """Restore a saved cursor; raise ValueError when it cannot be used."""
        try:
            state = json.loads(path.read_text())
            if (not isinstance(state, dict) or state.get("version") != CURSOR_VERSION
                    or state.get("repo") != self.repo):
                raise ValueError("written for another repository or version")
            for item in state["files"]:
                file_identity = (item["dev"], item["ino"])
                values = (*file_identity, item["generation"], item["offset"], *item["undelivered"])
                if not all(type(v) is int and v >= 0 for v in values):
                    raise ValueError("malformed position")
                self.restored[file_identity] = Cursor(
                    file_identity, item["generation"], delivered=item["offset"],
                    undelivered=frozenset(item["undelivered"]), expected=bytes.fromhex(item["digest"]))
        except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"cannot read milestone cursor {path}: {exc}") from exc


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
    parser.add_argument("--mark", action="store_true",
                        help="record the current end of the retained logs in a new milestone "
                             "cursor file, print its path, and exit")
    parser.add_argument("--milestones", action="store_true",
                        help="emit only milestone events written after --cursor, advance it, and "
                             "exit after a loop stop, startup failure, or crash")
    parser.add_argument("--cursor", type=Path, help="milestone cursor file printed by --mark")
    parser.add_argument("target", nargs="?", type=parse_target, help="'#N' (issue and known linked PRs) or pr:N")
    args = parser.parse_args()
    if args.mark and (args.milestones or args.follow or args.cursor or args.json or args.target):
        parser.error("--mark takes no other option")
    if args.milestones and args.cursor is None:
        parser.error("--milestones requires --cursor (create one with --mark)")
    if args.cursor is not None and not args.milestones:
        parser.error("--cursor requires --milestones")
    reader = EventReader(args.log_dir, args.repo, args.target)
    if args.mark:
        # The position is fixed before the caller starts the loop, so the
        # watch reports every milestone that loop writes, however late it starts.
        try:
            reader.poll()
            fd, name = tempfile.mkstemp(prefix="pr-pipeline-milestones-", suffix=".json")
            os.close(fd)
            reader.save(Path(name))
        except OSError as exc:
            print(f"events: cannot record milestone cursor: {exc}", file=sys.stderr)
            return 1
        print(name)
        return 0
    if args.milestones:
        try:
            reader.load(args.cursor)
        except ValueError as exc:
            print(f"events: {exc}", file=sys.stderr)
            return 2
    print(HISTORY_NOTICE, file=sys.stderr, flush=True)
    try:
        while True:
            batch = reader.poll()
            for index, event in enumerate(batch):
                if args.milestones and event["event"] not in MILESTONES:
                    continue
                print(json.dumps(event, allow_nan=False) if args.json else render(event), flush=True)
                if args.milestones and event["event"] in LOOP_ENDING:
                    # Records after the stop stay unreported for the next reader.
                    reader.consumed.difference_update(e["id"] for e in batch[index + 1:])
                    reader.save(args.cursor)
                    return 0
            if args.milestones:
                # Saved after printing: a watch stopped in between repeats
                # milestones on re-arm rather than losing them.
                reader.save(args.cursor)
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
