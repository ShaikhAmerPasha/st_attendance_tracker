"""Shared duration/time-parsing helpers used by api.py and the Daily Task /
Daily Task Log controllers, so a correctness fix only needs to happen once."""

from frappe.utils import date_diff, today

MAX_WORKDAY_MINUTES = 24 * 60


def parse_duration_to_hours(s):
    """Parse a free-text duration like '1h 30m', '45m', '1:30' into hours.

    A bare number with no unit (e.g. '45') is treated as minutes, matching
    the input placeholder's own shorthand ('e.g. 1h 30m, 45m') — interpreting
    it as hours silently inflated stored durations by 60x.
    Negative results are treated as unparsed (0.0) rather than stored as-is.
    """
    if not s:
        return 0.0
    s = str(s).strip().lower()
    if not s:
        return 0.0

    if ":" in s and "h" not in s and "m" not in s:
        parts = s.split(":", 1)
        try:
            hours = float(parts[0])
            mins = float(parts[1]) if parts[1] else 0.0
            total = hours + mins / 60.0
            return total if total >= 0 else 0.0
        except ValueError:
            pass

    try:
        mins = float(s)
        return mins / 60.0 if mins >= 0 else 0.0
    except ValueError:
        pass

    h = 0.0
    m = 0.0

    for term in ["hours", "hour", "hrs", "hr"]:
        s = s.replace(term, "h")
    for term in ["minutes", "minute", "mins", "min"]:
        s = s.replace(term, "m")

    if "h" in s:
        parts = s.split("h")
        try:
            h = float(parts[0].strip())
        except ValueError:
            pass
        s = parts[1].strip()
    if "m" in s:
        parts = s.split("m")
        try:
            m = float(parts[0].strip())
        except ValueError:
            pass
    elif s:
        # No 'm' suffix but leftover text after the 'h' split
        # (e.g. "1h30") — treat it as bare minutes instead of dropping it.
        try:
            m = float(s)
        except ValueError:
            pass

    total = h + (m / 60.0)
    return total if total >= 0 else 0.0


def resolve_zero_diff_minutes(date_str):
    """A login/logout pair with identical clock time is 0 minutes unless the
    checkout is for exactly the day after login — the one legitimate case of
    a shift that ran the full 24 hours to the same time next day. A checkout
    submitted more days late than that with a coincidentally-matching time is
    not a real 24-hour shift, so it must not be fabricated as one.
    """
    if today() == str(date_str):
        return 0
    if date_diff(today(), date_str) == 1:
        return 24 * 60
    return 0
