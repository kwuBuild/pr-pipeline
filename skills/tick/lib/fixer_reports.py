"""Fixer report transport. Mechanical validation never approves a revision."""

from __future__ import annotations

import re

import review_reports

VERSION = 2
AUTHOR_ALLOWANCE = review_reports.AUTHOR_ALLOWANCE
PUSHED = "FIXES_PUSHED_RECHECK_NEEDED"
NO_CHANGE = "NO_CHANGES_RECHECK_NEEDED"
HUMAN = "NEEDS_HUMAN_DECISION"
WAIVER = "VERIFICATION_UNCONFIGURED_WAIVER"
LEGACY_NOTE = "Legacy fixer evidence: this dispatch predates complete per-finding report validation."
COMMENT_MARKER = "<!-- pr-pipeline:fix-report 2 -->"
TEXT_FIELDS = {"dispatch_id", "pinned_head", "published_head", "review_sha256", "outcome",
               "revision_summary", "verification", "verification_status", "documentation", "remaining_work"}
REQUIRED = TEXT_FIELDS | {"report_protocol_version", "findings", "verification_waiver"}
FINDING_FIELDS = {"reference", "disposition", "assessment", "evidence", "change"}
OPTIONAL_FIELDS = {"additional_context"}
OUTCOMES = (PUSHED, NO_CHANGE, HUMAN)
VERIFICATION_STATUSES = ("passed", "failed", "unavailable")
WAIVERS = ("", WAIVER)
DISPOSITIONS = ("repair", "replacement", "no-change", "unresolved")
# Dispatch-marker value for a fixer launched with ``claude -p --json-schema``.
# Its report arrives in the result's ``structured_output`` object. A marker
# without it predates the schema transport and keeps the final-message parse.
STRUCTURED_TRANSPORT = "structured_output"
MISSING_STRUCTURED_REPORT = (
    "Missing structured revision report: the fixer was launched with the report schema, "
    "but its result has no structured_output object."
)


def schema() -> dict:
    """Return the JSON Schema passed to ``claude -p --json-schema``.

    The field sets above are the single definition of the report shape. This
    schema only makes Claude Code return that shape in ``structured_output``;
    :func:`validation_problem` and the binding checks still decide validity.
    """
    enums = {"outcome": OUTCOMES, "verification_status": VERIFICATION_STATUSES}
    properties: dict[str, dict] = {
        field: ({"type": "string", "enum": list(enums[field])} if field in enums
                else {"type": "string", "minLength": 1})
        for field in sorted(TEXT_FIELDS)
    }
    finding = {
        "type": "object",
        "properties": {
            field: ({"type": "string", "enum": list(DISPOSITIONS)} if field == "disposition"
                    else {"type": "string", "minLength": 1})
            for field in sorted(FINDING_FIELDS)
        },
        "required": sorted(FINDING_FIELDS),
        "additionalProperties": False,
    }
    properties.update({
        "report_protocol_version": {"type": "integer", "const": VERSION},
        "findings": {"type": "array", "minItems": 1, "items": finding},
        "verification_waiver": {"type": "string", "enum": list(WAIVERS)},
        **{field: {"type": "string"} for field in sorted(OPTIONAL_FIELDS)},
    })
    return {"type": "object", "properties": properties, "required": sorted(REQUIRED),
            "additionalProperties": False}


def parse_result(result: object, version: object,
                 transport: object = None) -> tuple[dict | None, str]:
    """Read only the dedicated JSON outcome, never tokens in authored prose.

    ``transport`` comes from the dispatch marker, never from the result. With
    :data:`STRUCTURED_TRANSPORT` the report is the ``structured_output`` object
    and the final chat message is ignored. Without it (a dispatch launched
    before the schema transport) the whole final message must be the JSON report.
    """
    if (type(version) is not int or version != VERSION
            or transport not in (None, STRUCTURED_TRANSPORT)
            or not isinstance(result, dict)
            or type(result.get("report_protocol_version")) is not int
            or result.get("report_protocol_version") != version):
        return None, "Fixer result protocol does not match dispatch."
    if (result.get("is_error") is not False
            or type(result.get("claude_exit_status")) is not int
            or result["claude_exit_status"] != 0):
        return None, "Fixer engine did not complete successfully."
    if transport == STRUCTURED_TRANSPORT:
        report = result.get("structured_output")
        if not isinstance(report, dict):
            return None, MISSING_STRUCTURED_REPORT
    else:
        try:
            report = review_reports.loads(result["result"])
        except (KeyError, TypeError, ValueError, RecursionError):
            return None, "Missing or malformed JSON revision report; a bare sentinel is incomplete."
    problem = validation_problem(report)
    return (None, problem) if problem else (report, "")


def validation_problem(report: object) -> str:
    if (not isinstance(report, dict) or not REQUIRED <= report.keys()
            or not report.keys() <= REQUIRED | OPTIONAL_FIELDS):
        return "Missing or unknown revision report fields."
    if type(report["report_protocol_version"]) is not int or report["report_protocol_version"] != VERSION:
        return "Revision report protocol does not match dispatch."
    if any(not isinstance(report[k], str) or not report[k].strip() for k in TEXT_FIELDS):
        return "Required revision fields must contain text."
    if not isinstance(report.get("additional_context", ""), str):
        return "Additional context must be text."
    if report["outcome"] not in OUTCOMES:
        return "Unknown revision outcome."
    if report["verification_status"] not in VERIFICATION_STATUSES:
        return "Unknown verification status."
    if report["verification_waiver"] not in WAIVERS:
        return "Invalid verification waiver."
    if any(not re.fullmatch(r"[a-f0-9]{40}", report[k]) for k in ("pinned_head", "published_head")):
        return "Revision heads must be full commit identities."
    if not re.fullmatch(r"[a-f0-9]{64}", report["review_sha256"]):
        return "Invalid original review fingerprint."
    findings = report["findings"]
    if not isinstance(findings, list) or not findings:
        return "Revision needs the original finding responses."
    refs = set()
    for finding in findings:
        if (not isinstance(finding, dict) or set(finding) != FINDING_FIELDS
                or any(not isinstance(v, str) or not v.strip() for v in finding.values())):
            return "Every finding needs a reference, disposition, assessment, evidence and change or reason."
        if finding["reference"] in refs:
            return "Duplicate finding reference."
        refs.add(finding["reference"])
        if finding["disposition"] not in DISPOSITIONS:
            return "Unknown finding disposition."
    try:
        review_reports.canonical(report).encode("utf-8")
    except UnicodeError:
        return "Report contains invalid Unicode."
    return ""


def binding_problem(report: dict, *, dispatch_id: str, pinned_head: str,
                    review_sha256: str, review_handoff: object) -> str:
    if (not review_reports.valid_handoff(review_handoff)
            or review_handoff["comment_sha256"] != review_sha256):
        return "Missing or inconsistent originating review snapshot."
    if (not dispatch_id or report["dispatch_id"] != dispatch_id
            or report["pinned_head"] != pinned_head or report["review_sha256"] != review_sha256):
        return "Revision report does not match its dispatch, pinned head or originating review."
    if {f["reference"] for f in report["findings"]} != set(review_handoff["finding_refs"]):
        return "Missing or unknown originating finding references."
    return ""


def outcome_problem(report: dict, *, current_head: str, verification_unconfigured: bool) -> str:
    if report["published_head"] != current_head:
        return "Reported published head does not match the current PR head."
    outcome = report["outcome"]
    dispositions = {f["disposition"] for f in report["findings"]}
    if outcome == HUMAN:
        return ""
    if "unresolved" in dispositions or report["verification_status"] != "passed":
        return "Unresolved findings or unavailable/failed verification require a human-decision outcome."
    if verification_unconfigured and report["verification_waiver"] != WAIVER:
        return "Empty verification grants require an explicit completion waiver."
    if outcome == NO_CHANGE:
        if dispositions != {"no-change"} or report["published_head"] != report["pinned_head"]:
            return "No-change completion requires all no-change dispositions and the unchanged pinned head."
    elif not dispositions & {"repair", "replacement"} or report["published_head"] == report["pinned_head"]:
        return "Pushed completion requires a repair/replacement and an advanced published head."
    return ""


def render(report: dict, header: str) -> str:
    """Append complete authored fields, escaping only reserved controller markup."""
    neutralize = review_reports.neutralize
    lines = [header, "", "## Revision summary", "", neutralize(report["revision_summary"]),
             "", "## Response to each finding"]
    for finding in report["findings"]:
        lines += ["", "### " + neutralize(finding["reference"]), "",
                  "**Disposition:** " + finding["disposition"], "",
                  neutralize(finding["assessment"]), "", "**Evidence**", "",
                  neutralize(finding["evidence"]), "", "**Change or reason**", "",
                  neutralize(finding["change"])]
    lines += ["", "## Verification", "", "**Status:** " + report["verification_status"], "",
              neutralize(report["verification"])]
    if report["verification_waiver"]:
        lines += ["", "**Verification waiver:** `" + report["verification_waiver"] + "`"]
    for field, title in (("documentation", "PR and documentation updates"), ("remaining_work", "Remaining work"),
                         ("additional_context", "Additional context")):
        if report.get(field):
            lines += ["", "## " + title, "", neutralize(report[field])]
    return "\n".join(lines)


def is_current_comment(body: str) -> bool:
    """Only controller metadata before the first blank line declares protocol."""
    return COMMENT_MARKER in body.replace("\r\n", "\n").split("\n\n", 1)[0].splitlines()
