from datetime import datetime, timezone
import math


class UsageError(Exception):
    """A safe, user-facing message; never include response bodies or secrets."""


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    """Accept seconds, milliseconds and ISO8601; return epoch seconds."""
    if value is None or isinstance(value, bool):
        return None
    numeric = number(value)
    if numeric is not None:
        value = numeric / 1000 if numeric > 100_000_000_000 else numeric
        try:
            datetime.fromtimestamp(value, timezone.utc)
            return value if value > 0 else None
        except (ValueError, OverflowError, OSError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # An unspecified timezone is ambiguous: don't silently assume local time.
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def window(used_percent=None, reset=None, *, remaining=None, used=None, limit=None):
    total, left, consumed = number(limit), number(remaining), number(used)
    pct = number(used_percent)
    if total is not None and total > 0:
        # Absolute counters avoid rounded percentages, and overdraw clamps at zero left.
        if left is not None:
            actual_used = max(total - left, consumed) if consumed is not None else total - left
            pct = actual_used / total * 100
        elif consumed is not None:
            pct = consumed / total * 100
    left_pct = None if pct is None or pct < 0 else max(0.0, min(100.0, 100 - pct))
    return {"remaining_percent": left_pct, "resets_at": timestamp(reset)}


def result(provider, windows, *, status="ok", message=None):
    return {"provider": provider, "status": status, "message": message,
            "windows": windows, "updated_at": datetime.now(timezone.utc).timestamp()}


def complete(provider, windows):
    expected = ("month",) if provider == "cursor" else ("5h", "7d")
    values = {name: windows.get(name, window()) for name in expected}
    available = any(item["remaining_percent"] is not None for item in values.values())
    return result(provider, values, status="ok" if available else "unavailable",
                  message=None if available else "接口未提供额度")
