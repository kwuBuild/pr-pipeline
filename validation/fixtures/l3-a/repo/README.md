# durations

Small helpers for human-readable durations. Standard library only.

## Functions

- `format_duration(seconds)`: format a non-negative whole number of seconds as
  `1d2h3m4s`. Zero components are omitted, and `0` formats as `0s`. A negative
  value raises `ValueError`; a non-integer raises `TypeError`.
