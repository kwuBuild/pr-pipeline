"""Small text helpers."""


def normalize_space(text: str) -> str:
    """Collapse every run of whitespace to one space and strip both ends."""
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, not {type(text).__name__}")
    return " ".join(text.split())
