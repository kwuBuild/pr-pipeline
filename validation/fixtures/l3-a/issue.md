Add parse_duration, the inverse of format_duration
## Summary
- Add `parse_duration(text) -> int`, which turns a duration string back into whole seconds.

## Why this matters
- Configuration files store durations as text. Callers need the inverse of `format_duration`.

## Scope
- Accept one or more components of the form `<digits><unit>`, with units `d`, `h`, `m`, `s` (86400, 3600, 60 and 1 seconds).
- Components appear in descending unit order, each unit at most once. A single space may separate two components: `1h 30m` and `1h30m` are both valid.
- Leading and trailing whitespace is ignored.
- Raise `ValueError` for everything else, with a message that contains the rejected input: an empty or whitespace-only string, an unknown unit, a unit out of order, a repeated unit, a sign, a missing number, a missing unit, or more than one space between components.
- Raise `TypeError` when `text` is not a `str`.
- `parse_duration(format_duration(n)) == n` for every whole number `n >= 0`.

## Done when
- `parse_duration` is exported from `durations`, documented in README.md under "Functions", and every rule above, including each error case, has a test.
- The repository's verification command passes.
