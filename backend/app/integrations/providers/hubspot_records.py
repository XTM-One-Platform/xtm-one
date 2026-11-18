# SPDX-License-Identifier: Apache-2.0

"""How the HubSpot tools read activity: times, text, engagements, timelines,
form submissions and the inbound engagement report.

No I/O: ``hubspot_activity`` fetches, this module reads what HubSpot answered.

* **Times** are aware UTC datetimes. HubSpot answers ISO-8601 in UTC (``...Z``),
  a bare date for date properties and epoch milliseconds for form submissions
  and the legacy contact profile; an argument may be any of them, an ISO time
  with an offset included. A bare date bounding a range covers its whole day.
* **Text**: an engagement body is HTML (notes, calls, meetings, tasks, an
  email's ``hs_email_html``) or plain text (``hs_email_text``). HTML goes
  through the platform's HTML reader; a body is cut at a stated length and the
  cut is always said.
* **Engagements** of every type are read into one :class:`Engagement` shape:
  type, time, direction, owner, sender, subject, body, status and outcome,
  attachments, the records it is associated with.
* **A timeline** is chronological: by time, then by source, then id.
"""

from __future__ import annotations

import math
import re
import statistics
import textwrap
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

# ── Times ───────────────────────────────────────────────────────────────────

_EPOCH_RE = re.compile(r"^-?\d{9,14}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HTML_TAG_RE = re.compile(r"<[a-zA-Z!/][^>]*>")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
#: Below this an epoch number is seconds, above it milliseconds (year 5138 in seconds).
_EPOCH_MS_FLOOR = 100_000_000_000


def _from_epoch(value: float) -> datetime | None:
    seconds = value / 1000 if abs(value) >= _EPOCH_MS_FLOOR else value
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_hubspot_time(value: Any) -> datetime | None:
    """*value* as an aware UTC datetime, or ``None`` when it is not a time.

    Accepts an ISO-8601 string (``Z``, an offset, or naive read as UTC), a bare
    ``YYYY-MM-DD`` (midnight UTC) and epoch seconds or milliseconds, as a
    number or a string of digits.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(value) if math.isfinite(value) else None
    text = str(value).strip()
    if not text:
        return None
    if _EPOCH_RE.match(text):
        return _from_epoch(int(text))
    if _DATE_RE.match(text):
        try:
            return datetime.combine(date.fromisoformat(text), time.min, UTC)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_time_bound(value: Any, *, end: bool = False) -> datetime | None:
    """A ``from`` / ``to`` argument as a UTC instant, or ``None`` when absent or unreadable.

    A bare date bounds its whole UTC day: as an *end*, ``2026-10-28`` runs
    through ``2026-10-28T23:59:59.999Z``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        try:
            start = datetime.combine(date.fromisoformat(value.strip()), time.min, UTC)
        except ValueError:
            return None
        return start + timedelta(days=1) - timedelta(milliseconds=1) if end else start
    return parse_hubspot_time(value)


def iso_utc(moment: datetime | None) -> str:
    """``2026-10-28T14:03:11Z``, or ``""`` for no time."""
    if moment is None:
        return ""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def within(moment: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    """Whether *moment* is in ``[start, end]``; an undated event is in an unbounded range only."""
    if moment is None:
        return start is None and end is None
    return (start is None or moment >= start) and (end is None or moment <= end)


def format_hours(hours: float | None) -> str:
    if hours is None:
        return "n/a"
    if hours >= 48:
        return f"{hours / 24:.1f} d"
    return f"{hours:.1f} h"


# ── Text ────────────────────────────────────────────────────────────────────


def html_to_text(value: Any) -> str:
    """The readable text of a HubSpot body: HTML rendered, plain text kept as is."""
    text = str(value or "")
    if not text.strip():
        return ""
    if _HTML_TAG_RE.search(text):
        from app.services.attachment_extract import extract_html_document

        text = extract_html_document(text).text
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def cap_text(text: str, limit: int) -> tuple[str, int]:
    """*text* cut to at most *limit* characters (at a word boundary near the end
    when there is one), and how many characters were left out."""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    cut = text.rfind(" ", int(limit * 0.8), limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip(), len(text) - cut


def one_line(text: str, limit: int) -> str:
    """*text* on one line, cut to *limit* characters with an ellipsis."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: max(1, limit - 3)].rstrip() + "..."


def split_ids(value: Any) -> list[str]:
    """IDs from a list or a comma / semicolon / whitespace separated string, deduplicated in order."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else re.split(r"[,;\s]+", str(value))
    seen: dict[str, None] = {}
    for item in items:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def split_names(value: Any) -> list[str]:
    """Comma-separated names (a property list, form names) deduplicated in order; spaces inside a name are kept."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    seen: dict[str, None] = {}
    for item in items:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


# ── Engagements ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngagementSpec:
    """Where one engagement type keeps what the :class:`Engagement` shape reads."""

    kind: str
    subject: str = ""
    #: Body properties, first non-empty wins unless *join_bodies*.
    bodies: tuple[str, ...] = ()
    join_bodies: bool = False
    direction: str = ""
    #: Status and outcome properties, read through the enumeration labels.
    status: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()

    @property
    def properties(self) -> list[str]:
        names = [*COMMON_ENGAGEMENT_PROPERTIES, self.subject, *self.bodies, self.direction, *self.status, *self.extra]
        return list(dict.fromkeys(n for n in names if n))


COMMON_ENGAGEMENT_PROPERTIES = ("hs_timestamp", "hubspot_owner_id", "hs_attachment_ids", "hs_createdate")

ENGAGEMENT_SPECS: dict[str, EngagementSpec] = {
    "emails": EngagementSpec(
        kind="email",
        subject="hs_email_subject",
        bodies=("hs_email_text", "hs_email_html", "hs_body_preview"),
        direction="hs_email_direction",
        status=("hs_email_status",),
        extra=(
            "hs_email_from_email",
            "hs_email_from_firstname",
            "hs_email_from_lastname",
            "hs_email_to_email",
            "hs_email_cc_email",
            "hs_sequence_id",
            "hs_email_open_count",
            "hs_email_click_count",
        ),
    ),
    "notes": EngagementSpec(kind="note", bodies=("hs_note_body",)),
    "calls": EngagementSpec(
        kind="call",
        subject="hs_call_title",
        bodies=("hs_call_body",),
        direction="hs_call_direction",
        status=("hs_call_status", "hs_call_disposition"),
        extra=("hs_call_duration", "hs_call_from_number", "hs_call_to_number", "hs_call_recording_url"),
    ),
    "meetings": EngagementSpec(
        kind="meeting",
        subject="hs_meeting_title",
        bodies=("hs_meeting_body", "hs_internal_meeting_notes"),
        join_bodies=True,
        status=("hs_meeting_outcome",),
        extra=("hs_meeting_start_time", "hs_meeting_end_time", "hs_meeting_location", "hs_activity_type", "hs_meeting_source"),
    ),
    "tasks": EngagementSpec(
        kind="task",
        subject="hs_task_subject",
        bodies=("hs_task_body",),
        status=("hs_task_status",),
        extra=("hs_task_type", "hs_task_priority", "hs_task_completion_date"),
    ),
    "communications": EngagementSpec(
        kind="communication",
        bodies=("hs_communication_body",),
        status=("hs_communication_channel_type",),
        extra=("hs_communication_logged_from",),
    ),
}
ENGAGEMENT_TYPES = tuple(ENGAGEMENT_SPECS)

#: Every spelling the API and the agent use for an engagement type.
_ENGAGEMENT_ALIASES = {
    **{t: t for t in ENGAGEMENT_TYPES},
    **{spec.kind: t for t, spec in ENGAGEMENT_SPECS.items()},
    "0-49": "emails",
    "0-46": "notes",
    "0-48": "calls",
    "0-47": "meetings",
    "0-27": "tasks",
    "0-18": "communications",
}
_DIRECTIONS = {
    "EMAIL": "outgoing",
    "INCOMING_EMAIL": "incoming",
    "FORWARDED_EMAIL": "forwarded",
    "OUTBOUND": "outgoing",
    "INBOUND": "incoming",
}
_STATUS_NAMES = {
    "hs_email_status": "status",
    "hs_call_status": "status",
    "hs_call_disposition": "outcome",
    "hs_meeting_outcome": "outcome",
    "hs_task_status": "status",
    "hs_communication_channel_type": "channel",
}
#: Email statuses that are not a message the recipient got.
_UNSENT_EMAIL_STATUSES = frozenset({"FAILED", "SCHEDULED", "SENDING"})


def engagement_type(value: Any) -> str:
    """The API name of the engagement type *value* names (``email`` -> ``emails``), or ``""``."""
    return _ENGAGEMENT_ALIASES.get(str(value or "").strip().lower(), "")


#: ``(object type, property, raw value) -> label``; the identity when nothing resolves.
Labeller = Callable[[str, str, str], str]


def _no_labels(_object_type: str, _name: str, raw: str) -> str:
    return raw


@dataclass
class Engagement:
    """One engagement of any type, as the tools show it."""

    id: str
    type: str
    kind: str
    timestamp: datetime | None
    logged_at: datetime | None
    direction: str
    owner_id: str
    subject: str
    body: str
    sender: str
    recipients: str
    status: list[tuple[str, str]]
    raw_status: dict[str, str]
    details: list[tuple[str, str]]
    attachment_ids: list[str]
    sequence_id: str
    associations: dict[str, list[str]] = field(default_factory=dict)

    @property
    def touch_time(self) -> datetime | None:
        """When the engagement reached the contact: a meeting counts when it was booked, not held."""
        if self.type == "meetings" and self.logged_at is not None:
            return min(self.logged_at, self.timestamp) if self.timestamp else self.logged_at
        return self.timestamp


def record_properties(obj: Mapping[str, Any]) -> dict[str, str]:
    raw = obj.get("properties") if isinstance(obj, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v is not None and str(v).strip() != ""}


def _person(first: str, last: str, email: str) -> str:
    name = f"{first} {last}".strip()
    if name and email:
        return f"{name} <{email}>"
    return name or email


def _duration(ms: str) -> str:
    try:
        seconds = int(float(ms) / 1000)
    except ValueError:
        return ms
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def associations_of(obj: Mapping[str, Any]) -> dict[str, list[str]]:
    """``{object type: [ids]}`` of a v3 record read with ``associations=``."""
    out: dict[str, list[str]] = {}
    raw = obj.get("associations") if isinstance(obj, Mapping) else None
    if not isinstance(raw, Mapping):
        return out
    for object_type, data in raw.items():
        results = data.get("results") if isinstance(data, Mapping) else None
        ids = split_ids([r.get("id") for r in results or [] if isinstance(r, Mapping) and r.get("id")])
        if ids:
            out[str(object_type)] = ids
    return out


def attachment_ids(props: Mapping[str, str]) -> list[str]:
    """The file ids of an engagement's ``hs_attachment_ids`` (``;``-separated)."""
    return [part.strip() for part in str(props.get("hs_attachment_ids") or "").split(";") if part.strip()]


def normalize_engagement(type_: str, obj: Mapping[str, Any], *, label: Labeller = _no_labels) -> Engagement:
    """Read one engagement object of type *type_* (``emails``, ``calls``...)."""
    spec = ENGAGEMENT_SPECS[type_]
    p = record_properties(obj)
    bodies = [(name, html_to_text(p.get(name))) for name in spec.bodies]
    bodies = [(name, text) for name, text in bodies if text]
    if spec.join_bodies and len(bodies) > 1:
        body = "\n\n".join(text if i == 0 else f"[{name}]\n{text}" for i, (name, text) in enumerate(bodies))
    else:
        body = bodies[0][1] if bodies else ""
    status: list[tuple[str, str]] = []
    raw_status: dict[str, str] = {}
    for name in spec.status:
        raw = p.get(name, "")
        if raw:
            raw_status[name] = raw
            status.append((_STATUS_NAMES.get(name, name), label(type_, name, raw)))
    details: list[tuple[str, str]] = []
    sequence_id = p.get("hs_sequence_id", "")
    kind = spec.kind
    sender = recipients = ""
    if type_ == "emails":
        sender = _person(p.get("hs_email_from_firstname", ""), p.get("hs_email_from_lastname", ""), p.get("hs_email_from_email", ""))
        recipients = p.get("hs_email_to_email", "")
        if p.get("hs_email_cc_email"):
            details.append(("cc", p["hs_email_cc_email"]))
        for name, shown in (("hs_email_open_count", "opens"), ("hs_email_click_count", "clicks")):
            if p.get(name):
                details.append((shown, p[name]))
        if sequence_id:
            kind = "sequence email"
            details.append(("sequence", sequence_id))
    elif type_ == "calls":
        if p.get("hs_call_duration"):
            details.append(("duration", _duration(p["hs_call_duration"])))
        for name, shown in (("hs_call_from_number", "from number"), ("hs_call_to_number", "to number"), ("hs_call_recording_url", "recording")):
            if p.get(name):
                details.append((shown, p[name]))
    elif type_ == "meetings":
        for name, shown in (("hs_meeting_start_time", "start"), ("hs_meeting_end_time", "end")):
            moment = parse_hubspot_time(p.get(name))
            if moment:
                details.append((shown, iso_utc(moment)))
        for name, shown in (("hs_meeting_location", "location"), ("hs_activity_type", "meeting type"), ("hs_meeting_source", "source")):
            if p.get(name):
                details.append((shown, label(type_, name, p[name])))
    elif type_ == "tasks":
        for name, shown in (("hs_task_type", "task type"), ("hs_task_priority", "priority")):
            if p.get(name):
                details.append((shown, label(type_, name, p[name])))
        completed = parse_hubspot_time(p.get("hs_task_completion_date"))
        if completed:
            details.append(("completed", iso_utc(completed)))
    elif type_ == "communications" and p.get("hs_communication_logged_from"):
        details.append(("logged from", p["hs_communication_logged_from"]))
    raw_direction = p.get(spec.direction, "") if spec.direction else ""
    return Engagement(
        id=str(obj.get("id") or p.get("hs_object_id") or ""),
        type=type_,
        kind=kind,
        timestamp=parse_hubspot_time(p.get("hs_timestamp")) or parse_hubspot_time(p.get("hs_createdate")),
        logged_at=parse_hubspot_time(p.get("hs_createdate")),
        direction=_DIRECTIONS.get(raw_direction.upper(), raw_direction.lower()),
        owner_id=p.get("hubspot_owner_id", ""),
        subject=html_to_text(p.get(spec.subject)) if spec.subject else "",
        body=body,
        sender=sender,
        recipients=recipients,
        status=status,
        raw_status=raw_status,
        details=details,
        attachment_ids=attachment_ids(p),
        sequence_id=sequence_id,
        associations=associations_of(obj),
    )


def format_engagement(
    e: Engagement,
    *,
    owner: Callable[[str], str],
    body_chars: int,
    with_associations: bool = False,
    indent: str = "    ",
) -> str:
    """One engagement: a header line, then its body (cut at *body_chars*, the cut stated)."""
    head = [iso_utc(e.timestamp) or "no timestamp", (e.kind.upper() + (f" {e.direction}" if e.direction else ""))]
    if e.owner_id:
        head.append(f"owner {owner(e.owner_id)}")
    if e.sender:
        head.append(f"from {e.sender}")
    if e.recipients:
        head.append(f"to {e.recipients}")
    if e.subject:
        head.append(f"subject '{one_line(e.subject, 200)}'")
    head.extend(f"{name} {value}" for name, value in [*e.status, *e.details])
    if e.attachment_ids:
        head.append("attachment file ids " + ", ".join(e.attachment_ids))
    if with_associations and e.associations:
        head.append("associated " + "; ".join(f"{t} {', '.join(ids)}" for t, ids in e.associations.items()))
    head.append(f"{ENGAGEMENT_SPECS[e.type].kind} id {e.id}")
    text = " | ".join(head)
    if not e.body:
        return text
    if body_chars <= 0:
        return text + f" | body {len(e.body):,} chars (not shown)"
    body, more = cap_text(e.body, body_chars)
    text += "\n" + textwrap.indent(body, indent)
    if more:
        text += (
            f"\n{indent}[body cut: {more:,} more characters - get_hubspot_engagement"
            f"(engagement_type='{e.type}', engagement_id='{e.id}') reads it in full]"
        )
    return text


def touch_channel(e: Engagement) -> str | None:
    """The channel *e* reached the contact through, or ``None`` when it is not a touch
    (an incoming or forwarded email, an unsent one, a note, a task)."""
    if e.type == "emails":
        if e.direction != "outgoing" or e.raw_status.get("hs_email_status", "").upper() in _UNSENT_EMAIL_STATUSES:
            return None
        return "sequence email" if e.sequence_id else "manual email"
    if e.type == "calls":
        return "call"
    if e.type == "meetings":
        source = next((value for name, value in e.details if name == "source"), "")
        return "meeting (booked by the contact)" if source.upper() == "MEETINGS_PUBLIC" else "meeting"
    if e.type == "communications":
        channel = next((value for name, value in e.status if name == "channel"), "")
        return channel or "message"
    return None


# ── Timeline ────────────────────────────────────────────────────────────────

#: Same-second tie-break: what prompted an activity before the activity.
TIMELINE_ORDER = {
    "form submission": 0,
    "property change": 1,
    "sequence": 2,
    "emails": 3,
    "calls": 3,
    "communications": 3,
    "meetings": 4,
    "notes": 5,
    "tasks": 6,
}


@dataclass
class TimelineEvent:
    timestamp: datetime | None
    source: str
    key: str
    text: str


def _id_key(key: str) -> tuple[int, str]:
    return (int(key), "") if key.isdigit() else (0, key)


def merge_timeline(events: Iterable[TimelineEvent], *, newest_first: bool = False) -> list[TimelineEvent]:
    """*events* in chronological order (or newest first): by time, then by source
    (:data:`TIMELINE_ORDER`), then id. Undated events come last either way."""
    items = list(events)
    dated = sorted(
        (e for e in items if e.timestamp is not None),
        key=lambda e: (e.timestamp, TIMELINE_ORDER.get(e.source, 9), _id_key(e.key)),
    )
    if newest_first:
        dated.reverse()
    undated = sorted(
        (e for e in items if e.timestamp is None),
        key=lambda e: (TIMELINE_ORDER.get(e.source, 9), _id_key(e.key)),
    )
    return dated + undated


@dataclass
class PropertyChange:
    name: str
    value: str
    timestamp: datetime | None
    source: str
    user_id: str


def property_changes(history: Any, names: Iterable[str]) -> list[PropertyChange]:
    """The value changes of *names* in a v3 ``propertiesWithHistory`` map, oldest first."""
    out: list[PropertyChange] = []
    if not isinstance(history, Mapping):
        return out
    for name in names:
        entries = history.get(name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            source = " / ".join(str(entry[k]) for k in ("sourceType", "sourceLabel") if entry.get(k))
            out.append(
                PropertyChange(
                    name=name,
                    value=str(entry.get("value") if entry.get("value") is not None else ""),
                    timestamp=parse_hubspot_time(entry.get("timestamp")),
                    source=source,
                    user_id=str(entry.get("updatedByUserId") or ""),
                )
            )
    out.sort(key=lambda c: (c.timestamp is None, c.timestamp or datetime.min.replace(tzinfo=UTC)))
    return out


# ── Form submissions ────────────────────────────────────────────────────────


@dataclass
class FormSubmission:
    form_id: str
    form_name: str
    timestamp: datetime | None
    page_url: str
    page_title: str
    values: list[tuple[str, str]]
    email: str
    contact_id: str = ""
    conversion_id: str = ""


def submitted_email(values: Iterable[tuple[str, str]]) -> str:
    return next((v.strip().lower() for n, v in values if n.strip().lower() == "email" and v.strip()), "")


def submission_from_form(form_id: str, item: Mapping[str, Any], *, form_name: str = "") -> FormSubmission:
    """One result of ``GET /form-integrations/v1/submissions/forms/{form_id}``."""
    values = [
        (str(v.get("name") or ""), str(v.get("value") if v.get("value") is not None else ""))
        for v in item.get("values") or []
        if isinstance(v, Mapping) and v.get("name")
    ]
    return FormSubmission(
        form_id=form_id,
        form_name=form_name,
        timestamp=parse_hubspot_time(item.get("submittedAt")),
        page_url=str(item.get("pageUrl") or ""),
        page_title="",
        values=values,
        email=submitted_email(values),
        conversion_id=str(item.get("conversionId") or ""),
    )


def submissions_from_profile(profile: Mapping[str, Any], contact_id: str) -> list[FormSubmission]:
    """The ``form-submissions`` of a legacy contact profile (``/contacts/v1/contact/vid/{id}/profile``)."""
    email = ""
    props = profile.get("properties")
    if isinstance(props, Mapping) and isinstance(props.get("email"), Mapping):
        email = str(props["email"].get("value") or "").strip().lower()
    out = []
    for item in profile.get("form-submissions") or []:
        if not isinstance(item, Mapping):
            continue
        out.append(
            FormSubmission(
                form_id=str(item.get("form-id") or ""),
                form_name=str(item.get("title") or ""),
                timestamp=parse_hubspot_time(item.get("timestamp")),
                page_url=str(item.get("page-url") or item.get("canonical-url") or ""),
                page_title=str(item.get("page-title") or ""),
                values=[],
                email=email,
                contact_id=contact_id,
                conversion_id=str(item.get("conversion-id") or ""),
            )
        )
    out.sort(key=lambda s: (s.timestamp is None, s.timestamp or datetime.min.replace(tzinfo=UTC)))
    return out


def submissions_from_history(history: Any, contact_id: str, email: str) -> list[FormSubmission]:
    """A contact's conversions from the history of ``recent_conversion_event_name``
    (each conversion overwrites it): the fallback when the legacy profile is gone."""
    out = []
    for change in property_changes(history, ("recent_conversion_event_name",)):
        if not change.value:
            continue
        out.append(
            FormSubmission(
                form_id="",
                form_name=change.value,
                timestamp=change.timestamp,
                page_url="",
                page_title="",
                values=[],
                email=email,
                contact_id=contact_id,
            )
        )
    return out


def format_submission(s: FormSubmission, *, value_chars: int = 300, indent: str = "    ") -> str:
    head = [iso_utc(s.timestamp) or "no timestamp", f"FORM SUBMISSION '{s.form_name or s.form_id or 'unknown form'}'"]
    if s.form_id and s.form_name:
        head.append(f"form id {s.form_id}")
    if s.contact_id:
        head.append(f"contact {s.contact_id}" + (f" ({s.email})" if s.email else ""))
    elif s.email:
        head.append(f"email {s.email} (no contact found)")
    if s.page_url:
        head.append(f"page {s.page_url}")
    if s.page_title:
        head.append(f"page title '{s.page_title}'")
    text = " | ".join(head)
    for name, value in s.values:
        shown, more = cap_text(value, value_chars)
        flat = shown.replace("\n", " / ")
        text += f"\n{indent}{name}: {flat}" + (f" [+{more:,} chars]" if more else "")
    return text


# ── Inbound engagement report ───────────────────────────────────────────────


@dataclass
class InboundContact:
    """One inbound contact and what the report needs of it."""

    contact_id: str
    name: str
    email: str
    company: str
    country: str
    region: str
    owner_id: str
    owner: str
    form: str
    submitted_at: datetime | None
    assigned_at: datetime | None = None
    engagements: list[Engagement] = field(default_factory=list)
    deals_created: list[datetime] = field(default_factory=list)
    sequence_enrolled_at: datetime | None = None
    replied_at: datetime | None = None


@dataclass
class InboundOutcome:
    contact: InboundContact
    touches: list[tuple[datetime, str, Engagement]]
    replied: bool
    meeting_booked: bool
    meeting_held: bool
    deal_created: bool

    @property
    def first_touch(self) -> tuple[datetime, str, Engagement] | None:
        return self.touches[0] if self.touches else None

    @property
    def first_touch_hours(self) -> float | None:
        first, anchor = self.first_touch, self.contact.submitted_at
        if first is None or anchor is None:
            return None
        return max(0.0, (first[0] - anchor).total_seconds() / 3600)


def assess_inbound(contact: InboundContact) -> InboundOutcome:
    """What happened to *contact* from its submission on: touches by channel,
    reply, meeting booked and held, deal created."""
    anchor = contact.submitted_at

    def after(moment: datetime | None) -> bool:
        return moment is not None and (anchor is None or moment >= anchor)

    touches = []
    for e in contact.engagements:
        channel = touch_channel(e)
        moment = e.touch_time
        if channel and after(moment):
            touches.append((moment, channel, e))
    touches.sort(key=lambda t: (t[0], t[2].id))
    replied = any(e.type == "emails" and e.direction == "incoming" and after(e.timestamp) for e in contact.engagements)
    replied = replied or after(contact.replied_at)
    meetings = [e for e in contact.engagements if e.type == "meetings" and after(e.touch_time)]
    return InboundOutcome(
        contact=contact,
        touches=touches,
        replied=replied,
        meeting_booked=bool(meetings),
        meeting_held=any(e.raw_status.get("hs_meeting_outcome", "").upper() == "COMPLETED" for e in meetings),
        deal_created=any(after(moment) for moment in contact.deals_created),
    )


REPORT_GROUPS = ("form", "region", "country", "owner")


@dataclass
class GroupStats:
    name: str
    submitted: int = 0
    assigned: int = 0
    touched: int = 0
    touched_24h: int = 0
    replied: int = 0
    meeting_booked: int = 0
    meeting_held: int = 0
    deal_created: int = 0
    delays: list[float] = field(default_factory=list)
    channels: Counter[str] = field(default_factory=Counter)

    def add(self, outcome: InboundOutcome) -> None:
        self.submitted += 1
        self.assigned += bool(outcome.contact.owner_id)
        if outcome.first_touch:
            self.touched += 1
            self.channels[outcome.first_touch[1]] += 1
            hours = outcome.first_touch_hours
            if hours is not None:
                self.delays.append(hours)
                self.touched_24h += hours <= 24
        self.replied += outcome.replied
        self.meeting_booked += outcome.meeting_booked
        self.meeting_held += outcome.meeting_held
        self.deal_created += outcome.deal_created


def group_key(outcome: InboundOutcome, group_by: str) -> str:
    c = outcome.contact
    value = {"form": c.form, "region": c.region, "country": c.country, "owner": c.owner if c.owner_id else ""}.get(group_by, "")
    return value or "(none)"


def aggregate_inbound(outcomes: Iterable[InboundOutcome], group_by: str) -> tuple[GroupStats, list[GroupStats]]:
    """``(all contacts, one row per group)``, groups by contacts submitted, most first."""
    total = GroupStats("All contacts")
    groups: dict[str, GroupStats] = {}
    for outcome in outcomes:
        total.add(outcome)
        key = group_key(outcome, group_by)
        groups.setdefault(key, GroupStats(key)).add(outcome)
    return total, sorted(groups.values(), key=lambda g: (-g.submitted, g.name.lower()))


def _pct(part: int, whole: int) -> str:
    return f"{part} ({round(100 * part / whole)}%)" if whole else str(part)


def format_group(g: GroupStats) -> str:
    parts = [
        g.name,
        f"submitted {g.submitted}",
        f"assigned {_pct(g.assigned, g.submitted)}",
        f"touched {_pct(g.touched, g.submitted)}",
        f"touched within 24 h {_pct(g.touched_24h, g.submitted)}",
    ]
    if g.delays:
        parts.append(
            f"first touch median {format_hours(statistics.median(g.delays))}, mean {format_hours(statistics.fmean(g.delays))}"
        )
    parts += [
        f"replied {_pct(g.replied, g.submitted)}",
        f"meeting booked {_pct(g.meeting_booked, g.submitted)}, held {g.meeting_held}",
        f"deal created {_pct(g.deal_created, g.submitted)}",
    ]
    if g.channels:
        parts.append("first touch by " + ", ".join(f"{name} {n}" for name, n in g.channels.most_common()))
    return " | ".join(parts)


def format_outcome(o: InboundOutcome, *, now: datetime) -> str:
    c = o.contact
    who = _person(c.name, "", c.email) or f"contact {c.contact_id}"
    parts = [f"contact {c.contact_id} {who}"]
    if c.company:
        parts.append(c.company)
    if c.country:
        parts.append(c.country)
    parts.append(f"{c.form or 'unknown form'} at {iso_utc(c.submitted_at) or 'unknown time'}")
    parts.append(f"owner {c.owner}" if c.owner_id else "no owner")
    if c.region:
        parts.append(f"region {c.region}")
    first = o.first_touch
    if first:
        parts.append(
            f"touched after {format_hours(o.first_touch_hours)} by {first[1]} ({len(o.touches)} touch(es), "
            f"first {iso_utc(first[0])})"
        )
    else:
        waited = (now - c.submitted_at).total_seconds() / 3600 if c.submitted_at else None
        parts.append(f"UNTOUCHED for {format_hours(waited)}")
    parts.append(f"replied {'yes' if o.replied else 'no'}")
    if o.meeting_booked:
        parts.append(f"meeting booked{', held' if o.meeting_held else ''}")
    if o.deal_created:
        parts.append("deal created")
    return " | ".join(parts)


#: The categories of a HubSpot association type.
ASSOCIATION_CATEGORIES = ("HUBSPOT_DEFINED", "USER_DEFINED", "INTEGRATOR_DEFINED")


def association_label(labels: Any, name: str) -> dict[str, Any] | None:
    """The association type of *labels* (a v4 ``/labels`` answer's results) labelled *name*, case-insensitive."""
    wanted = " ".join(str(name or "").split()).casefold()
    if not wanted or not isinstance(labels, list):
        return None
    for entry in labels:
        if not isinstance(entry, dict) or entry.get("typeId") is None:
            continue
        if " ".join(str(entry.get("label") or "").split()).casefold() == wanted:
            category = str(entry.get("category") or "HUBSPOT_DEFINED").upper()
            return {
                "associationCategory": category,
                "associationTypeId": int(entry["typeId"]),
                "label": entry.get("label"),
            }
    return None


def association_type_text(typed: dict[str, Any]) -> str:
    """How an association type is named in an answer: its label, then category and type id."""
    ident = f"{typed.get('associationCategory')} type {typed.get('associationTypeId')}"
    label = typed.get("label")
    return f"the '{label}' label ({ident})" if label else ident
