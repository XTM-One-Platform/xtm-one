"""Recurring Outlook events: the ``recurrence`` argument as a Graph ``patternedRecurrence``.

The argument takes Graph's own shape (``pattern`` + ``range``, camelCase
properties) so what the agent writes is what Graph receives. It is checked
in full before any write, and every refusal names the property and the rule.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from typing import Any

_PATTERN_TYPES = ("daily", "weekly", "absoluteMonthly", "relativeMonthly", "absoluteYearly", "relativeYearly")
_RANGE_TYPES = ("endDate", "numbered", "noEnd")
_DAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
_INDEXES = ("first", "second", "third", "fourth", "last")

# The pattern properties each pattern type takes besides ``type`` and
# ``interval``: (required, optional).
_PATTERN_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "daily": ((), ()),
    "weekly": (("daysOfWeek",), ("firstDayOfWeek",)),
    "absoluteMonthly": (("dayOfMonth",), ()),
    "relativeMonthly": (("daysOfWeek",), ("index",)),
    "absoluteYearly": (("dayOfMonth", "month"), ()),
    "relativeYearly": (("daysOfWeek", "month"), ("index",)),
}
# The range properties each range type takes besides ``type``, ``startDate``
# and ``recurrenceTimeZone``.
_RANGE_FIELDS: dict[str, tuple[str, ...]] = {
    "endDate": ("endDate",),
    "numbered": ("numberOfOccurrences",),
    "noEnd": (),
}
_OCCURRENCE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})$")


class RecurrenceError(ValueError):
    """A ``recurrence`` argument Graph would refuse or misread."""


def _canonical(value: Any, allowed: tuple[str, ...], where: str) -> str:
    if isinstance(value, str):
        for option in allowed:
            if value.strip().lower() == option.lower():
                return option
    raise RecurrenceError(f"{where} must be one of {', '.join(allowed)}, got {value!r}.")


def _whole_number(value: Any, where: str, low: int, high: int | None = None) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        bound = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise RecurrenceError(f"{where} must be a whole number {bound}, got {value!r}.")
    return value


def _iso_date(value: Any, where: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise RecurrenceError(f"{where} must be a date like 2026-10-07, got {value!r}.") from None


def _given(raw: dict) -> dict:
    """*raw* without the properties sent as ``null``: a model writes null for "not given"."""
    return {key: value for key, value in raw.items() if value is not None}


def _no_extra(given: dict, allowed: set[str], where: str, hint: str) -> None:
    extra = sorted(set(given) - allowed)
    if extra:
        raise RecurrenceError(f"{where} does not take {', '.join(extra)} ({hint}).")


def _pattern(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise RecurrenceError("recurrence.pattern must be an object with at least a type.")
    raw = _given(raw)
    kind = _canonical(raw.get("type"), _PATTERN_TYPES, "recurrence.pattern.type")
    required, optional = _PATTERN_FIELDS[kind]
    _no_extra(
        raw,
        {"type", "interval", *required, *optional},
        f"recurrence.pattern of type {kind}",
        f"it takes interval{''.join(', ' + f for f in (*required, *optional))}",
    )
    pattern: dict[str, Any] = {
        "type": kind,
        "interval": _whole_number(raw.get("interval", 1), "recurrence.pattern.interval", 1),
    }
    for field in required:
        if raw.get(field) in (None, "", []):
            raise RecurrenceError(f"recurrence.pattern of type {kind} needs {field}.")
    if "daysOfWeek" in raw:
        days = raw["daysOfWeek"]
        days = [days] if isinstance(days, str) else days
        if not isinstance(days, list) or not days:
            raise RecurrenceError("recurrence.pattern.daysOfWeek must be a list of weekday names.")
        canonical = [_canonical(day, _DAYS, "each recurrence.pattern.daysOfWeek entry") for day in days]
        pattern["daysOfWeek"] = list(dict.fromkeys(canonical))
    if "firstDayOfWeek" in raw:
        pattern["firstDayOfWeek"] = _canonical(raw["firstDayOfWeek"], _DAYS, "recurrence.pattern.firstDayOfWeek")
    elif kind == "weekly":
        # Graph requires it on a weekly pattern; sunday is its documented default.
        pattern["firstDayOfWeek"] = "sunday"
    if "index" in raw:
        pattern["index"] = _canonical(raw["index"], _INDEXES, "recurrence.pattern.index")
    if "month" in raw:
        pattern["month"] = _whole_number(raw["month"], "recurrence.pattern.month", 1, 12)
    if "dayOfMonth" in raw:
        # A yearly date must exist in its month (February 29 is allowed: leap years have it).
        last_day = calendar.monthrange(2024, pattern["month"])[1] if "month" in pattern else 31
        pattern["dayOfMonth"] = _whole_number(raw["dayOfMonth"], "recurrence.pattern.dayOfMonth", 1, last_day)
    return pattern


def _range(raw: Any, event_start: date) -> dict:
    if not isinstance(raw, dict):
        raise RecurrenceError("recurrence.range must be an object with at least a type.")
    raw = _given(raw)
    kind = _canonical(raw.get("type"), _RANGE_TYPES, "recurrence.range.type")
    fields = _RANGE_FIELDS[kind]
    _no_extra(
        raw,
        {"type", "startDate", "recurrenceTimeZone", *fields},
        f"recurrence.range of type {kind}",
        f"it takes startDate, recurrenceTimeZone{''.join(', ' + f for f in fields)}",
    )
    start = _iso_date(raw["startDate"], "recurrence.range.startDate") if raw.get("startDate") else event_start
    if start != event_start:
        raise RecurrenceError(
            f"recurrence.range.startDate ({start.isoformat()}) must be the date of start_datetime "
            f"({event_start.isoformat()}): Microsoft Graph starts the series on the event itself."
        )
    out: dict[str, Any] = {"type": kind, "startDate": start.isoformat()}
    if kind == "endDate":
        if not raw.get("endDate"):
            raise RecurrenceError(
                "recurrence.range of type endDate needs endDate (the date of the last possible occurrence)."
            )
        end = _iso_date(raw["endDate"], "recurrence.range.endDate")
        if end < start:
            raise RecurrenceError(
                f"recurrence.range.endDate ({end.isoformat()}) is before its startDate ({start.isoformat()})."
            )
        out["endDate"] = end.isoformat()
    elif kind == "numbered":
        if raw.get("numberOfOccurrences") in (None, ""):
            raise RecurrenceError("recurrence.range of type numbered needs numberOfOccurrences.")
        out["numberOfOccurrences"] = _whole_number(
            raw["numberOfOccurrences"], "recurrence.range.numberOfOccurrences", 1
        )
    zone = raw.get("recurrenceTimeZone")
    if zone not in (None, ""):
        if not isinstance(zone, str) or not zone.strip():
            raise RecurrenceError(f"recurrence.range.recurrenceTimeZone must be a time zone name, got {zone!r}.")
        out["recurrenceTimeZone"] = zone.strip()
    return out


def _build_patterned_recurrence(value: Any, start_datetime: str) -> dict | None:
    """The Graph ``patternedRecurrence`` for *value*, or ``None`` for a one-off event.

    *value* is the tool argument (an object, or the same object as a JSON
    string); *start_datetime* is the event's start, whose date the range
    starts on. Raises :class:`RecurrenceError` with a tool-readable reason.
    """
    if value in (None, "", {}):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise RecurrenceError("recurrence must be an object with a pattern and a range.") from None
    if not isinstance(value, dict):
        raise RecurrenceError("recurrence must be an object with a pattern and a range.")
    value = _given(value)
    _no_extra(value, {"pattern", "range"}, "recurrence", "it takes pattern and range")
    if "pattern" not in value or "range" not in value:
        raise RecurrenceError("recurrence needs both a pattern and a range.")
    event_start = _iso_date((start_datetime or "")[:10], "the date of start_datetime")
    return {"pattern": _pattern(value["pattern"]), "range": _range(value["range"], event_start)}


def _every(interval: int, unit: str) -> str:
    return f"every {unit}" if interval == 1 else f"every {interval} {unit}s"


def _describe_recurrence(recurrence: dict | None) -> str:
    """A Graph ``patternedRecurrence`` in words, e.g. ``weekly on Wednesday ...``."""
    if not recurrence:
        return ""
    pattern = recurrence.get("pattern") or {}
    rng = recurrence.get("range") or {}
    kind = pattern.get("type", "")
    interval = pattern.get("interval") or 1
    days = ", ".join(d.capitalize() for d in pattern.get("daysOfWeek") or [])
    month = calendar.month_name[pattern["month"]] if pattern.get("month") else ""
    index = pattern.get("index") or "first"
    rule = {
        "daily": f"daily, {_every(interval, 'day')}",
        "weekly": f"weekly on {days}, {_every(interval, 'week')}",
        "absoluteMonthly": f"monthly on day {pattern.get('dayOfMonth')}, {_every(interval, 'month')}",
        "relativeMonthly": f"monthly on the {index} {days}, {_every(interval, 'month')}",
        "absoluteYearly": f"yearly on {month} {pattern.get('dayOfMonth')}, {_every(interval, 'year')}",
        "relativeYearly": f"yearly on the {index} {days} of {month}, {_every(interval, 'year')}",
    }.get(kind, kind or "unknown pattern")
    span = f"from {rng.get('startDate', '?')}"
    if rng.get("type") == "endDate":
        span += f" until {rng.get('endDate', '?')}"
    elif rng.get("type") == "numbered":
        span += f", {rng.get('numberOfOccurrences', '?')} occurrences"
    elif rng.get("type") == "noEnd":
        span += ", no end date"
    zone = f" ({rng['recurrenceTimeZone']})" if rng.get("recurrenceTimeZone") else ""
    return f"{rule}; {span}{zone}"


def _occurrence_date(occurrence_id: str) -> str:
    """The date a ``cancelledOccurrences`` entry names (``OID.{masterId}.2026-10-28``)."""
    match = _OCCURRENCE_DATE_RE.search(occurrence_id or "")
    return match.group(1) if match else occurrence_id
