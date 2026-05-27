# Commit Guidelines

Commit hygiene in this repo is a hard requirement.

## Default History Shape

- Create exactly one commit per task before opening a PR
- Before opening a PR, make sure the branch contains only the commit(s) for that task relative to the base branch
- If the branch includes unrelated or older task commits, rebase or squash them away before raising the PR
- Only create multiple commits when the user explicitly asks for stacked or bisectable history
- If a workflow or skill would produce a different commit structure, ask whether that tradeoff is acceptable before proceeding

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
message to a temporary file and using `git commit --file <path>` so the body is
not accidentally skipped.

## Forbidden Content

- Do not include internal task codes or private planning labels
- `Co-Authored-By` trailers for AI agents are allowed

## Required Validation

Before opening a PR, creating a final commit, or asking the owner to merge:

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

If any subject or body does not match, fix it before proceeding. For a
one-commit task, amend the local commit.

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
