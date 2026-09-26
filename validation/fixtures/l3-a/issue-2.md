Add a long style to format_duration
## Summary
- Add a keyword argument `style` to `format_duration`. `"short"`, the default, keeps today's output. `"long"` spells the duration out in words.

## Why this matters
- Durations in user-facing messages must read as prose, for example "1 day, 2 hours and 3 seconds".

## Scope
- `style="long"` names each non-zero component as `<count> <unit>`, with units `day`, `hour`, `minute`, `second`, in descending order. Use the singular form for a count of 1 and add `s` otherwise.
- Join two components with ` and `. Join three or more with `, ` between them, and ` and ` before the last one. There is no comma before `and`.
- `0` formats as `0 seconds` in the long style.
- A `style` other than `"short"` or `"long"` raises `ValueError` with a message that contains the style. A `style` that is not a `str` raises `TypeError`.
- The existing `TypeError` and `ValueError` rules for `seconds` apply to both styles, and they are checked before `style`.
- The default output is unchanged. Existing tests keep passing without edits.

## Done when
- README.md documents the new argument under "Functions", and every rule above, including each error case, has a test.
- The repository's verification command passes.
