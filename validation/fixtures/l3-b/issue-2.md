Add wrap for fixed-width paragraphs
## Summary
- Add `wrap(text, width) -> str`, which re-flows text into lines no wider than `width` characters.

## Why this matters
- Plain-text reports are printed at a fixed width, and `textwrap` does not keep paragraph breaks.

## Scope
- Paragraphs are separated by one or more blank lines, where a line holding only whitespace counts as blank. The output separates paragraphs with exactly one empty line and keeps their order.
- Inside a paragraph, words are separated by runs of whitespace, newlines included. Fill each line greedily with words joined by one space.
- No output line is longer than `width`. A word longer than `width` is split into pieces of exactly `width` characters, with the remainder carried to the next line.
- No output line has leading or trailing whitespace. The output has no leading or trailing empty lines and does not end with a newline.
- Text that is empty or only whitespace returns `""`.
- Raise `ValueError` when `width` is less than 1, with a message that contains the width. Raise `TypeError` when `text` is not a `str` or `width` is not an `int`. A `bool` is not an `int` here.
- `normalize_space` keeps its current behaviour.

## Done when
- `wrap` is exported from `textkit`, documented in README.md under "Functions", and every rule above, including each error case, has a test.
- The repository's verification command passes.
