"""Small, non-authoritative usage snapshots from terminal engine events.

Only documented numeric counters and bounded model identifiers cross this
boundary. Totals describe provider reports, not context-window occupancy,
API-call counts, bills or quota use.
"""

from __future__ import annotations

import math
import re

_MAX_TOKENS = 2**63 - 1
# Public: claude_stream_wrapper sums exactly this set across result events, so
# the wrapper and the normalizer must agree on which counters cross the
# boundary. _CODEX_COUNTERS has no second consumer and stays private.
CLAUDE_COUNTERS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)
_CODEX_COUNTERS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
)


def claude_models(result: object) -> list[str] | None:
    """Retain only bounded model identifiers actually named by the terminal CLI."""
    if not isinstance(result, dict) or result.get("type") != "result":
        return None
    models = result.get("modelUsage")
    if not isinstance(models, dict) or not 0 < len(models) <= 16:
        return None
    if any(
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}", name) is None
        for name in models
    ):
        return None
    return sorted(models)


def claude_usage(result: object) -> dict[str, object]:
    """Normalize the Claude wrapper's final result; never read assistant usage."""
    if not isinstance(result, dict) or result.get("type") != "result":
        result = {}
    usage = _normalize(result.get("usage"), engine="claude")
    cost = result.get("total_cost_usd")
    if (
        type(cost) in (int, float)
        and 0 <= cost <= 1_000_000_000_000
        and math.isfinite(cost)
    ):
        usage["reported_cost"] = {
            "amount": cost,
            "unit": "USD",
            "source": "claude.result.total_cost_usd",
        }
        if usage["status"] == "unavailable":
            usage["status"] = "partial"
    elif "total_cost_usd" in result and usage["status"] == "complete":
        usage["status"] = "partial"
    return usage


def codex_usage(native: object, *, partial: bool = False) -> dict[str, object]:
    """Normalize one Codex terminal snapshot, without summing repeated events."""
    usage = _normalize(native, engine="codex")
    if partial and usage["status"] == "complete":
        usage["status"] = "partial"
    return usage


def valid_codex_usage(payload: object) -> bool:
    """Validate exactly the bounded, sanitized snapshot persisted by the wrapper."""
    if not isinstance(payload, dict):
        return False
    if any(
        payload.get(key) is not None and type(payload[key]) is not int
        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    ):
        return False
    expected = codex_usage(
        payload.get("native"), partial=payload.get("status") == "partial"
    )
    return payload == expected


def _normalize(raw: object, *, engine: str) -> dict[str, object]:
    keys = CLAUDE_COUNTERS if engine == "claude" else _CODEX_COUNTERS
    raw = raw if isinstance(raw, dict) else {}
    native = {
        key: raw[key]
        for key in keys
        if type(raw.get(key)) is int and 0 <= raw[key] <= _MAX_TOKENS
    }
    invalid = any(key in raw and key not in native for key in keys)
    if engine == "claude":
        parts = CLAUDE_COUNTERS[:3]
        total = (
            sum(native[key] for key in parts)
            if all(key in native for key in parts)
            else None
        )
        if total is not None and total > _MAX_TOKENS:
            total = None
        cached = native.get("cache_read_input_tokens")
    else:
        total = native.get("input_tokens")
        cached = native.get("cached_input_tokens")
        # Cache and reasoning are subsets, not additional input/output usage.
        for subset, whole in (
            ("cached_input_tokens", "input_tokens"),
            ("cache_write_input_tokens", "input_tokens"),
            ("reasoning_output_tokens", "output_tokens"),
        ):
            if subset in native and whole in native and native[subset] > native[whole]:
                invalid = True
                if subset == "cached_input_tokens":
                    cached = None
    output = native.get("output_tokens")
    complete = (
        all(value is not None for value in (total, cached, output)) and not invalid
    )
    return {
        "status": "complete" if complete else "partial" if native else "unavailable",
        "source": "claude.result.usage"
        if engine == "claude"
        else "codex.turn.completed.usage",
        "token_unit": "tokens",
        "input_tokens": total,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "native": native,
        "reported_cost": None,
    }
