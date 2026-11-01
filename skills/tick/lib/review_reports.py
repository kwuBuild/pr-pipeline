"""Versioned reviewer reports. Structure is evidence transport, not a quality score."""

from __future__ import annotations

import hashlib
import json
import re

VERSION = 2
COMMENT_LIMIT = 60_000  # Both Unicode characters and UTF-8 bytes, including metadata.
# Leave space for controller identity and headings. Final rendering is still the
# authority: escaping markers and unusual ref names can consume more space.
AUTHOR_ALLOWANCE = COMMENT_LIMIT - 8_000
VERDICTS = {"ready_for_human", "changes_requested", "needs_human_decision"}
SEVERITIES = {"blocker", "major", "minor"}
ID_PATTERN = r"[A-Za-z][A-Za-z0-9_-]{0,63}"
METADATA_PREFIX = "<!-- pr-pipeline:report "
LEGACY_NOTE = "Legacy report: detailed reasoning, verification and remaining risks were not recorded."
REQUIRED = {"report_protocol_version", "verdict", "reasoning", "scope", "findings", "verification", "risks"}
FINDING_REQUIRED = {"id", "severity", "location", "explanation", "evidence"}


def validation_problem(obj: object) -> str:
    """Return a mechanical protocol error, never a judgment of review quality."""
    if not isinstance(obj, dict) or not REQUIRED <= obj.keys():
        return "Incomplete protocol 2 report: required report fields are missing."
    if not obj.keys() <= REQUIRED | {"observations"}:
        return "Unknown report fields."
    if type(obj["report_protocol_version"]) is not int or obj["report_protocol_version"] != VERSION:
        return "Report protocol version does not match dispatch."
    if not isinstance(obj["verdict"], str) or obj["verdict"] not in VERDICTS:
        return "Unknown review outcome."
    for field in ("reasoning", "scope", "verification", "risks"):
        if not isinstance(obj[field], str) or not obj[field].strip():
            return f"Missing or invalid {field}."
    if "observations" in obj and not isinstance(obj["observations"], str):
        return "Invalid observations."
    findings = obj["findings"]
    if not isinstance(findings, list):
        return "Findings must be an array."
    seen = set()
    for finding in findings:
        if (not isinstance(finding, dict) or not FINDING_REQUIRED <= finding.keys()
                or not finding.keys() <= FINDING_REQUIRED | {"resolution"}):
            return "Incomplete or unknown finding fields."
        if any(not isinstance(finding[key], str) for key in finding):
            return "Finding fields must be strings."
        identity = finding["id"]
        if not re.fullmatch(ID_PATTERN, identity) or identity in seen:
            return "Invalid or duplicate finding identifier."
        seen.add(identity)
        if finding["severity"] not in SEVERITIES:
            return "Unknown finding severity."
        if not finding["explanation"].strip() or not finding["evidence"].strip():
            return "A finding needs an explanation and evidence."
    blocking = any(f["severity"] in {"blocker", "major"} for f in findings)
    if ((obj["verdict"] == "ready_for_human" and blocking)
            or (obj["verdict"] == "changes_requested" and not blocking)):
        return "The outcome contradicts the blocking finding set."
    try:
        canonical(obj).encode("utf-8")
    except UnicodeError:
        return "Report contains invalid Unicode."
    return ""


def canonical(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(raw: str) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field: " + key)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def neutralize(text: str) -> str:
    """Escape only reserved marker openers; authored Markdown/fences stay intact."""
    return re.sub(r"<!--(?=\s*pr-pipeline:)", "&lt;!--", text)


def within_limit(body: str) -> bool:
    return len(body) <= COMMENT_LIMIT and len(body.encode("utf-8")) <= COMMENT_LIMIT


def envelope_base(obj: object, version: int, *, captured: bool) -> dict | None:
    """Validate protocol binding before stripping extensions for legacy gates."""
    if (type(version) is not int or version != VERSION or not isinstance(obj, dict)
            or type(obj.get("report_protocol_version")) is not int
            or obj.get("report_protocol_version") != version
            or obj.get("result_captured") is not captured
            or type(obj.get("result_valid")) is not bool
            or not isinstance(obj.get("validation_reason"), str)):
        return None
    if obj["result_valid"] == bool(obj["validation_reason"]):
        return None
    return {k: v for k, v in obj.items()
            if k not in {"report_protocol_version", "result_captured", "validation_reason"}}


def render(report: dict, *, identity: str, audit: str, header: str) -> str:
    """Render the complete report with a controller-owned identity header."""
    metadata = {
        "version": VERSION, "identity": digest(identity),
        "report_sha256": digest(canonical(report)),
        "finding_ids": [f["id"] for f in report["findings"]],
    }
    lines = [header, METADATA_PREFIX + canonical(metadata) + " -->", "", audit, "",
             "## Verdict and scope", "", f"**Outcome:** `{report['verdict']}`", "",
             neutralize(report["reasoning"]), "", neutralize(report["scope"])]
    if report["findings"]:
        lines += ["", "## Findings"]
        for finding in report["findings"]:
            lines += ["", f"### {finding['id']} — {finding['severity']}", "",
                      f"**Reference:** `{metadata['identity']}:{finding['id']}`"]
            if finding["location"]:
                lines += ["", "**Location:** " + neutralize(finding["location"])]
            lines += ["", neutralize(finding["explanation"]), "", "**Evidence**", "",
                      neutralize(finding["evidence"])]
            if finding.get("resolution"):
                lines += ["", "**Suggested resolution**", "", neutralize(finding["resolution"])]
    for key, title in (("verification", "Verification"), ("risks", "Remaining risks and follow-ups"),
                       ("observations", "Additional observations")):
        if report.get(key):
            lines += ["", f"## {title}", "", neutralize(report[key])]
    return "\n".join(lines)


def handoff(body: str) -> dict:
    """Adapt an already authenticated controller report, retaining its exact text.

    New reports carry controller metadata. Historical reports must match the
    old renderer's unambiguous one-line finding grammar; prose is never guessed
    into a finding set. Authentication belongs to pipeline.trusted_marker_comments.
    """
    lines = body.replace("\r\n", "\n").splitlines()
    if (len(lines) < 3 or lines[0] != "<!-- pr-pipeline:review -->"
            or not re.fullmatch(r"<!-- pr-pipeline:round:[^\r\n]+ -->", lines[1])):
        raise ValueError("Review has no unambiguous controller round identity.")
    if lines[2].startswith(METADATA_PREFIX):
        if not lines[2].endswith(" -->"):
            raise ValueError("Malformed report metadata.")
        try:
            meta = loads(lines[2][len(METADATA_PREFIX):-4])
        except (ValueError, TypeError) as exc:
            raise ValueError("Malformed report metadata.") from exc
        if (not isinstance(meta, dict)
                or set(meta) != {"version", "identity", "report_sha256", "finding_ids"}
                or type(meta["version"]) is not int or meta["version"] != VERSION
                or not isinstance(meta["finding_ids"], list)
                or not all(isinstance(i, str) and re.fullmatch(ID_PATTERN, i) for i in meta["finding_ids"])
                or len(set(meta["finding_ids"])) != len(meta["finding_ids"])):
            raise ValueError("Invalid or ambiguous report finding identity.")
        identity, report_digest, ids = meta["identity"], meta["report_sha256"], meta["finding_ids"]
        version = VERSION
    else:
        legacy = body.replace("\r\n", "\n")
        if legacy.endswith("\n\n" + LEGACY_NOTE):
            legacy = legacy[:-len("\n\n" + LEGACY_NOTE)]
        old = legacy.splitlines()
        if (len(old) < 8 or old[2] or old[4] or old[6]
                or not re.fullmatch(r"\*\*Codex review\*\* \([^\r\n]+\)", old[3])
                or old[5] != "🔧 Changes requested."):
            raise ValueError("Historical review does not match the unambiguous controller format.")
        findings = [re.fullmatch(r"- \*\*(blocker|major|minor)\*\* `([^`\r\n]+)` — ([^\r\n]+)", line)
                    for line in old[7:]]
        if not findings or not all(findings):
            raise ValueError("Historical finding boundaries are ambiguous; recover the original report.")
        ids = [f"F{i}" for i in range(1, len(findings) + 1)]
        identity = report_digest = digest(body)
        version = 1
    snapshot = {"version": version, "identity": identity, "report_sha256": report_digest,
                "comment_sha256": digest(body), "finding_refs": [f"{identity}:{i}" for i in ids]}
    if not valid_handoff(snapshot):
        raise ValueError("Invalid report identity or digest.")
    return snapshot


def valid_handoff(obj: object) -> bool:
    if (not isinstance(obj, dict)
            or set(obj) != {"version", "identity", "report_sha256", "comment_sha256", "finding_refs"}
            or type(obj["version"]) is not int or obj["version"] not in {1, VERSION}):
        return False
    if not all(isinstance(obj[k], str) and re.fullmatch(r"[a-f0-9]{64}", obj[k])
               for k in ("identity", "report_sha256", "comment_sha256")):
        return False
    refs = obj["finding_refs"]
    return (isinstance(refs, list) and bool(refs)
            and all(isinstance(ref, str) and re.fullmatch(obj["identity"] + ":" + ID_PATTERN, ref) for ref in refs)
            and len(set(refs)) == len(refs))


def schema() -> dict:
    # Strict provider output requires every property on the wire. Keep the
    # logical validator compatible with reports that omit optional prose.
    text = {"type": "string"}
    finding = {"type": "object", "additionalProperties": False,
               "required": sorted(FINDING_REQUIRED | {"resolution"}), "properties": {
                   "id": {"type": "string", "pattern": "^" + ID_PATTERN + "$"},
                   "severity": {"type": "string", "enum": sorted(SEVERITIES)},
                   "location": {**text, "description": "Precise file/line or boundary; empty if not applicable."},
                   "explanation": {**text, "description": "Trigger and consequence; Markdown paragraphs, lists and code are welcome."},
                   "evidence": {**text, "description": "Supporting source or reproduction and result; distinguish execution from inspection."},
                   "resolution": {**text, "description": "Optional suggestion; use an empty string when no particular fix is justified."},
               }}
    return {"type": "object", "additionalProperties": False,
            "description": f"Protocol {VERSION}. Allow at most {AUTHOR_ALLOWANCE} characters AND UTF-8 bytes across authored text. The complete rendered comment has a {COMMENT_LIMIT} character AND byte safety bound, including controller metadata; this is not a preferred length or quality score.",
            "required": sorted(REQUIRED | {"observations"}), "properties": {
                "report_protocol_version": {"type": "integer", "enum": [VERSION]},
                "verdict": {"type": "string", "enum": sorted(VERDICTS)},
                "reasoning": {**text, "description": "Concise basis for the outcome; clean reviews need no invented finding."},
                "scope": {**text, "description": "Task contract and relevant boundaries examined; separate inspected and executed work."},
                "findings": {"type": "array", "items": finding},
                "verification": {**text, "description": "Checks/probes actually run, commands and outcomes; attribute CI/author evidence and explain relevant checks not run."},
                "risks": {**text, "description": "Uncertainty, nonblocking concerns and deferred work with owner/reference when known; state none when applicable."},
                "observations": {**text, "description": "Optional Markdown discussion, titled subsections, tradeoffs or diagrams; identify observations outside agreed scope. Use an empty string when absent."},
            }}
