Add slugify for URL-safe identifiers
## Summary
- Add `slugify(text, max_length=64) -> str`, which turns a title into a URL-safe identifier.

## Why this matters
- Titles become URL paths, and today every caller builds its own slug.

## Scope
- Fold the text to ASCII: decompose with Unicode NFKD and drop combining marks, so `Café` becomes `cafe`. Drop any other non-ASCII character.
- Lowercase the result.
- Replace every run of characters other than `a-z` and `0-9` with one hyphen, and strip hyphens from both ends.
- If the slug is longer than `max_length`, cut it at the last hyphen at or before `max_length`, so no word is split. If no such hyphen exists, cut at `max_length`. The result never ends with a hyphen.
- Raise `ValueError`, with a message that contains the input, when the slug would be empty or when `max_length` is less than 1.
- Raise `TypeError` when `text` is not a `str` or when `max_length` is not an `int`. A `bool` is not an `int` here.

## Done when
- `slugify` is exported from `textkit`, documented in README.md under "Functions", and every rule above, including each error case, has a test.
- The repository's verification command passes.
