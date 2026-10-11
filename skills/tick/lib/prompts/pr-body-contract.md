### Shared PR report contract

This is the authoritative presentation and closure contract for implementer
reports and fixer body replacements. The controller includes it in both prompts.
Authors explain the problem, rationale, structure, impact, and verification in
natural prose. Give reviewers the details needed to assess the change: the
behavior, decisions, exact verification, failures, and remaining limitations.
Do not shorten or discard useful evidence to fit a Markdown layout. Paragraphs,
nested lists, and useful subsections are allowed.
`Structure` and `Impact` are content to explain, not required bullet tokens or
positions. Put limitations, migration notes, branch history, scope decisions,
and other supporting material in optional `## Additional context`.

Use this template. Replace `#N` with the primary issue number and each instruction
with the authored explanation. Include only verification that was performed;
name checks that failed or could not run.

```md
## Summary
Closes #N

Explain the problem and the resulting behavior.

## Why this approach
Explain the decisions and tradeoffs.

## What changed
Describe the components, their normal flow, and their impact.

## Test coverage
Explain what the tests prove and their limits. Include required completion evidence.

## Test plan
List exact verification commands and results, plus remaining manual checks.

## Additional context
Optional: limitations, migration notes, branch history, and scope decisions.
```

Authors should include a standalone `Closes #N` line, or a bullet/numbered list
item with that exact text, under Summary outside code blocks. Initial publication
uses the controller's dispatched issue; fixer replacements use the primary issue
pinned from the original PR at dispatch. Markdown layout does not own that target.

If a readable report omits the primary line, uses a different heading, or hides
it inside a code block, harvest preserves the report and prepends a Summary with the
known target and a marked **Controller issue reference** explanation. It does
not infer authority from an example. When the primary is absent, a conflicting
standalone closure outside Summary remains invalid. Ambiguous or wrong primary
references under Summary also remain invalid. A replacement must preserve the
complete set of all closing directives
in the body, including cross-repository and URL references, after the controller
supplies any missing primary reference. Do not add or drop other directives.
An empty fixer candidate remains invalid; an absent candidate leaves the body alone.

Harvest recognizes level-two ATX headings with case variants, up to three leading
spaces, and optional closing hashes. Backtick and tilde fences inside list items
use indentation relative to the list content, including nested lists. A new
ordered list can interrupt a paragraph in the same container only at 1. A
marker at the previous item's level may start a sibling or a new list with
another number, including after a change of list type or delimiter. An empty
marker can continue on the next line; a following blank line ends that empty
item, while its enclosing list item stays open. Inline code, hashtag-prefixed
text, and ordinary
tilde-prefixed text remain paragraph content. Actual headings, thematic breaks,
and setext underlines end a paragraph. A fence ends at its closing delimiter or
the end of its list item. Indented code cannot interrupt a paragraph. Headings
and closure examples inside fenced or indented code do not define sections or
primary authority.
Custom subsections and additional sections are retained.
Missing non-control template sections produce one metadata/layout notice; they
do not discard authored content. On replacement or handoff recovery, harvest
refreshes a known leading controller layout notice to match the current report,
or removes it when all sections are present. Authored prose and embedded notice
examples remain unchanged. Prefix recognition normalizes CRLF to LF before
refreshing notices; it preserves authored indentation and Markdown hard breaks.
There is no mandatory final Impact line.

Publication converts CRLF line endings to LF and removes the single blank
separator after the title envelope. The controller header described above is
the only added issue authority. The authored report retains other whitespace,
code blocks, evidence, closure references, commit identity, and scope. The body,
including controller notices, must fit 65,536 characters and UTF-8 bytes. Harvest does not
rewrite meaning, synthesize impact or test claims, or certify report quality.
Concurrent-edit comparison remains separate: it ignores only CRLF/LF differences
and surrounding whitespace, as in the established GitHub round-trip contract.
