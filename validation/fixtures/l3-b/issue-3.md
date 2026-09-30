Add title_case for headline capitalisation
## Summary
- Add `title_case(text) -> str`, which capitalises a headline in the house style.

## Why this matters
- Report titles are typed in any case. Each caller now applies its own rules, and the results differ.

## Scope
- Words are separated by single spaces after `normalize_space` is applied to the input. The output uses single spaces.
- Capitalise the first letter of every word and lowercase the rest, except as follows.
- These small words stay lowercase unless they are the first word, the last word, or the first word after a colon: `a`, `an`, `and`, `as`, `at`, `but`, `by`, `for`, `in`, `nor`, `of`, `on`, `or`, `the`, `to`, `up`.
- A word written entirely in capital letters and at least two letters long, such as `API`, keeps its capitals.
- Each part of a hyphenated word follows the same rules, so `state-of-the-art` becomes `State-of-the-Art`.
- After an apostrophe, the letter is not capitalised: `don't` becomes `Don't`.
- Text that is empty or only whitespace returns `""`. Raise `TypeError` when `text` is not a `str`.

## Done when
- `title_case` is exported from `textkit`, documented in README.md under "Functions", and every rule above has a test.
- The repository's verification command passes.
