# Commit Guidelines

Commit hygiene in this repo is a hard requirement.

## Default History Shape

Keep revisions inspectable during review and put one commit per PR on `main`.

1. Before opening a PR, prepare one clean implementation commit containing only
   the intended task changes relative to the fetched base. Remove unrelated or
   older task commits before publication.
2. After the PR opens, append one focused commit for each revision round that
   changes files. This applies to manual authors and pipeline fixers. Preserve
   published commits so the owner and reviewer can inspect each revision.
   Do not amend, squash, or rebase published commits to fold in review fixes.
3. A revision that needs no changes creates no commit and makes no push. Report
   the assessment and verification instead.
4. The owner uses **Squash and merge** after review and required checks. The
   resulting commit on `main` represents the whole PR, including its revisions.
   The pipeline does not merge the PR or squash its published history.

An explicit owner request can select a different history shape. A dispatched
fixer still follows its pinned push authority; a different history requirement
does not grant it permission to rewrite the remote branch.

## Commit Subject Format

- Use the subject format `<type>: <concise summary>`
- Allowed types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
- Keep the subject under 72 characters
- Write the subject in plain descriptive language
- Use imperative or outcome-focused wording after the type

## Commit Body Format

Every task commit must include a brief body after the subject. The body should
make future archaeology easy without duplicating the full PR description.

Use this structure:

```text
<type>: <concise summary>

What:
- <1-3 bullets describing the concrete implementation>

Why:
- <1-2 bullets explaining why the change is needed>
```

Keep body bullets short and specific. Mention the architectural reason,
behavioral gap, or maintenance benefit that would not be obvious from the
subject alone.

Do not create subject-only commits. When committing manually, prefer writing the
message to a file in `tmp/` at the project root (gitignored) and using
`git commit --file tmp/<name>.txt` so the body is not accidentally skipped.

## Forbidden Content

- Do not include internal task codes or private planning labels
- `Co-Authored-By` trailers for AI agents are allowed

## Required Validation

Before publishing an implementation or revision commit, or asking the owner to merge:

1. Inspect the branch commit subjects:
   ```bash
   git log --format=%s origin/main..HEAD
   ```
2. Confirm each subject matches `<type>: <concise summary>`.
3. Inspect the full commit messages:
   ```bash
   git log --format=fuller origin/main..HEAD
   ```
4. Confirm each task commit includes a `What:` and `Why:` body.

Correct an unpublished commit before pushing it. Do not amend published
commits to repair their messages; identify the discrepancy for the owner. The
owner should check the final squash commit's subject and body against these
same rules and describe the complete PR, not only the last fixer change.

## Examples

Good:

```text
feat: add label transition command

What:
- Add `transition` CLI command that atomically swaps workflow labels.
- Validate source and target labels against the allowed transition map.

Why:
- Orchestrator needs atomic label swaps to prevent race conditions between ticks.
```

```text
docs: add commit and PR workflow guidelines

What:
- Require task commits to include brief What and Why sections.
- Add a validation checklist for full commit messages.

Why:
- Preserve implementation context for future history archaeology.
```

Bad:

```text
feat: add label transition command
```

```text
Task 7: finish transition work
```

```text
Add label transition command (#42)
```
