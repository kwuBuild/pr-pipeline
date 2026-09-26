"""Human-readable durations."""

_UNITS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def format_duration(seconds: int) -> str:
    """Return ``seconds`` as ``1d2h3m4s``, omitting zero components."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise TypeError(f"seconds must be an int, not {type(seconds).__name__}")
    if seconds < 0:
        raise ValueError(f"seconds must not be negative: {seconds}")
    if seconds == 0:
        return "0s"
    parts = []
    for unit, size in _UNITS:
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)
