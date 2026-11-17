"""Outlook calendar tools: calendars, events, free/busy, invitation responses, Teams meeting options and recurrence."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple
from urllib.parse import quote as _url_encode

from app.integrations.http import AutoRefreshClient
from app.tools.base import (
    CallableToolExecutor,
    FileContext,
    ToolDefinition,
    ToolResult,
    WriteCoalescing,
    WriteReadBack,
)

from ._common import (
    _BASE_URL,
    _PROVIDER,
    MicrosoftContext,
    _parse_iso_datetime,
    _strip_html,
)
from ._meetings import (
    _ONLINE_MEETING_RETRY_DELAYS,
    _find_online_meeting,
    _format_meeting_options,
    _get_online_meeting,
    _patch_online_meeting,
    _requested_meeting_options,
)
from ._outlook import (
    _ATTACHMENT_FILE_IDS_PROPERTY,
    _attach_files,
    _body_links,
    _calendar_url,
    _format_outlook_attachments,
    _list_outlook_attachments,
    _resolve_attachment_files,
)
from ._recurrence import (
    _DAYS,
    _INDEXES,
    _PATTERN_TYPES,
    _RANGE_TYPES,
    RecurrenceError,
    _build_patterned_recurrence,
    _describe_recurrence,
    _occurrence_date,
)

# Microsoft Graph ``showAs`` free/busy status -> short, agent-readable label.
# Surfacing this is what lets an agent tell whether an event actually makes the
# user busy: an all-day "blocker" with showAs=busy/oof must NOT be treated as a
# free day.
_SHOW_AS_LABELS = {
    "free": "Free",
    "tentative": "Tentative",
    "busy": "Busy",
    "oof": "Out of office",
    "workingElsewhere": "Working elsewhere",
    "unknown": "Unknown",
}

# ``getSchedule`` returns a per-interval availability string where each
# character encodes the merged free/busy status for that time slot.
_AVAILABILITY_VIEW_LABELS = {
    "0": "Free",
    "1": "Tentative",
    "2": "Busy",
    "3": "Out of office",
    "4": "Working elsewhere",
}

# Hard cap on the number of events ``list_microsoft_calendar_events`` returns.
# The listing pages through ``@odata.nextLink`` so a busy / multi-day window
# isn't silently truncated; this bounds the worst case and the tool tells the
# agent (with a trailing note) when the cap is hit so the truncation is visible.
_CALENDAR_EVENTS_MAX = 250
# Same bound for ``list_microsoft_event_occurrences``: paging stops past it.
_OCCURRENCES_MAX = 250
# A time zone name the ``Prefer: outlook.timezone`` header may carry (Windows
# names like "W. Europe Standard Time" and IANA names like "Europe/Paris").
_PREFER_TIME_ZONE_RE = re.compile(r"[A-Za-z0-9 _/+\-().,]{1,100}")


class _InvitationResponse(NamedTuple):
    action: str  # the Graph event action
    status: str  # the ``responseStatus.response`` Graph then reports
    verb: str
    done: str


# The ``response`` respond_microsoft_calendar_event takes.
_INVITATION_RESPONSES = {
    "accept": _InvitationResponse("accept", "accepted", "accept", "accepted"),
    "tentatively_accept": _InvitationResponse(
        "tentativelyAccept", "tentativelyAccepted", "tentatively accept", "tentatively accepted"
    ),
    "decline": _InvitationResponse("decline", "declined", "decline", "declined"),
}
# Graph answers a response with 202 Accepted: the waits (seconds) for the event
# read back to report it.
_RESPONSE_READ_BACK_DELAYS = (1.0, 2.0)
_PROPOSED_TIME_FIELDS = ("start_datetime", "end_datetime", "timezone")

# The Teams meeting options create / update_microsoft_calendar_event take.
_MEETING_OPTION_SCHEMA = {
    "record_automatically": {
        "type": "boolean",
        "description": (
            "Teams meeting only: turn 'Record and transcribe automatically' on (true) or off "
            "(false). Omit to leave it unchanged."
        ),
    },
    "allow_transcription": {
        "type": "boolean",
        "description": "Teams meeting only: allow (true) or block (false) transcription. Omit to leave it unchanged.",
    },
    "allow_recording": {
        "type": "boolean",
        "description": "Teams meeting only: allow (true) or block (false) recording. Omit to leave it unchanged.",
    },
}

_RECURRENCE_SCHEMA = {
    "type": "object",
    "description": (
        "Optional: make the event a recurring series (a Microsoft Graph patternedRecurrence); "
        "omit it for a one-off event. pattern.type: daily; weekly (daysOfWeek); absoluteMonthly "
        "(dayOfMonth); relativeMonthly (daysOfWeek, index); absoluteYearly (dayOfMonth, month); "
        "relativeYearly (daysOfWeek, month, index). pattern.interval defaults to 1 (2 = every "
        "other day/week/month/year). range.type: endDate (endDate = the last possible date), "
        "numbered (numberOfOccurrences) or noEnd; range.startDate defaults to the date of "
        "start_datetime and must equal it. Example, every Wednesday until 2027-01-06: "
        '{"pattern": {"type": "weekly", "daysOfWeek": ["wednesday"]}, '
        '"range": {"type": "endDate", "endDate": "2027-01-06"}}.'
    ),
    "properties": {
        "pattern": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(_PATTERN_TYPES)},
                "interval": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Days, weeks, months or years between occurrences (default 1)",
                },
                "daysOfWeek": {"type": "array", "items": {"type": "string", "enum": list(_DAYS)}},
                "firstDayOfWeek": {"type": "string", "enum": list(_DAYS)},
                "dayOfMonth": {"type": "integer", "minimum": 1, "maximum": 31},
                "month": {"type": "integer", "minimum": 1, "maximum": 12},
                "index": {
                    "type": "string",
                    "enum": list(_INDEXES),
                    "description": "Week of the month of a relative pattern (default first)",
                },
            },
            "required": ["type"],
        },
        "range": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(_RANGE_TYPES)},
                "startDate": {
                    "type": "string",
                    "description": "YYYY-MM-DD; defaults to the date of start_datetime",
                },
                "endDate": {"type": "string", "description": "YYYY-MM-DD, the last possible date (type endDate)"},
                "numberOfOccurrences": {"type": "integer", "minimum": 1},
                "recurrenceTimeZone": {
                    "type": "string",
                    "description": "Time zone of startDate and endDate (defaults to the event's)",
                },
            },
            "required": ["type"],
        },
    },
    "required": ["pattern", "range"],
}


def _format_show_as(show_as: str | None) -> str:
    """Map a Graph ``showAs`` value to a short label (raw value as fallback)."""
    if not show_as:
        return ""
    return _SHOW_AS_LABELS.get(show_as, show_as)


def _bare_date(value: object) -> date | None:
    """The date of a date-only ISO value (``2026-12-31``); None when it carries a time."""
    text = value.strip() if isinstance(value, str) else ""
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _proposed_time_slot(value: object) -> tuple[dict | None, str | None]:
    """The Graph ``timeSlot`` of the ``proposed_new_time`` argument (an object or its JSON string).

    ``(None, None)`` when no time is proposed; ``(None, error)`` when the
    value cannot be read. The times are sent as given, in ``timezone``
    (default UTC), as ``create_microsoft_calendar_event`` sends its own.
    """
    if value in (None, "", {}):
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    shape = "proposed_new_time is an object with start_datetime, end_datetime and an optional timezone"
    if not isinstance(value, dict):
        return None, f"Error: {shape}."
    unknown = sorted(set(value) - set(_PROPOSED_TIME_FIELDS))
    if unknown:
        return None, f"Error: {shape}, not {', '.join(unknown)}."
    timezone = value.get("timezone")
    if timezone in (None, ""):
        timezone = "UTC"
    elif not isinstance(timezone, str):
        return None, f"Error: {shape}: timezone is a time zone name, e.g. 'Romance Standard Time'."
    try:
        start = _parse_iso_datetime(value.get("start_datetime"))
        end = _parse_iso_datetime(value.get("end_datetime"))
    except ValueError as exc:
        return None, f"Error: proposed_new_time: {exc}"
    if end <= start:
        return None, "Error: proposed_new_time: end_datetime must be after start_datetime."
    return {
        "start": {"dateTime": value["start_datetime"].strip(), "timeZone": timezone.strip()},
        "end": {"dateTime": value["end_datetime"].strip(), "timeZone": timezone.strip()},
    }, None


def _series_scope_note(event: dict, *, changed: str) -> str:
    """Whether a write reached one occurrence of a series or the whole series."""
    kind = event.get("type")
    if kind in ("occurrence", "exception"):
        return f" Only this occurrence was {changed}; the rest of the series is untouched."
    if kind == "seriesMaster":
        return f" The whole recurring series was {changed}, every occurrence included."
    return ""


def _format_occurrence(occurrence: dict) -> str:
    start = occurrence.get("start") or {}
    end = occurrence.get("end") or {}
    try:
        begins = datetime.fromisoformat((start.get("dateTime") or "")[:19])
        ends = datetime.fromisoformat((end.get("dateTime") or "")[:19])
        when = (
            f"{begins:%a %Y-%m-%d %H:%M}-{ends:%H:%M}"
            if begins.date() == ends.date()
            else f"{begins:%a %Y-%m-%d %H:%M} - {ends:%a %Y-%m-%d %H:%M}"
        )
    except ValueError:
        when = f"{start.get('dateTime', '')} - {end.get('dateTime', '')}"
    if start.get("timeZone"):
        when += f" ({start['timeZone']})"
    if occurrence.get("type") == "exception":
        when += " [moved or edited]"
    if occurrence.get("isCancelled"):
        when += " [cancelled]"
    return f"{when} | id: {occurrence.get('id', '')}"


async def _read_event_meeting(
    ctx: MicrosoftContext, client: AutoRefreshClient, event_url: str, wait_budget: list[float]
) -> tuple[dict, str | None]:
    """The event's Teams meeting fields, waiting out of *wait_budget* for Graph to set its join URL."""
    while True:
        resp, err = await ctx.request(
            client,
            "get",
            event_url,
            _PROVIDER,
            "read the event's Teams meeting",
            params={"$select": "id,subject,isOnlineMeeting,onlineMeeting,isOrganizer,organizer,type"},
        )
        if err:
            return {}, err
        event = resp.json() or {}
        if not event.get("isOnlineMeeting") or (event.get("onlineMeeting") or {}).get("joinUrl") or not wait_budget:
            return event, None
        await asyncio.sleep(wait_budget.pop(0))


async def _apply_meeting_options(
    ctx: MicrosoftContext,
    client: AutoRefreshClient,
    event_url: str,
    changes: dict[str, bool],
    *,
    just_created: bool = False,
) -> tuple[list[str], str | None]:
    """Set *changes* on the event's Teams meeting and read them back.

    The onlineMeeting is found from the event's join URL, PATCHed, then read
    again: a value Graph did not keep is an error, never a success. Returns
    ``(report lines, error)``. For an event *just_created*, the waits for its
    join URL and for its meeting share one budget.
    """
    wait_budget = list(_ONLINE_MEETING_RETRY_DELAYS) if just_created else []
    event, err = await _read_event_meeting(ctx, client, event_url, wait_budget)
    if err:
        return [], err
    if not event.get("isOnlineMeeting"):
        return [], (
            "Error: this event has no Teams meeting, so it has no recording or "
            "transcription options (create the event with is_online_meeting=true)."
        )
    if not event.get("isOrganizer"):
        organizer = ((event.get("organizer") or {}).get("emailAddress") or {}).get("address") or "another user"
        return [], f"Error: only the meeting organizer ({organizer}) can change its Teams meeting options."
    join_url = (event.get("onlineMeeting") or {}).get("joinUrl")
    if not join_url:
        return [], "Error: Microsoft Graph has not set the event's Teams join URL yet; retry in a moment."
    meeting, err = await _find_online_meeting(ctx, client, join_url, wait_budget=wait_budget)
    if err:
        return [], err
    meeting_id = meeting.get("id") or ""
    if not meeting_id:
        return [], "Error: Microsoft Graph returned the event's Teams meeting without its id, so it cannot be changed."
    answered, err = await _patch_online_meeting(ctx, client, meeting_id, changes)
    if err:
        return [], err
    effective, err = await _get_online_meeting(ctx, client, meeting_id)
    if err:
        return [], f"{err}\nThe change was sent to Microsoft Graph but could not be read back, so it is not confirmed."
    kept = {prop: effective.get(prop) for prop in changes}
    if any(kept[prop] != wanted for prop, wanted in changes.items()):
        asked = ", ".join(f"{prop}={json.dumps(value)}" for prop, value in changes.items())
        return [], (
            f"Error: Microsoft Graph did not keep the Teams meeting options: asked {asked}, the "
            f"meeting now reports {json.dumps(kept)} (the PATCH answered "
            f"{json.dumps({prop: answered.get(prop) for prop in changes})}). The organizer's "
            "Teams meeting policy may forbid it (cloud recording or transcription turned off by "
            "a Teams admin)."
        )
    lines = ["Teams meeting options (read back from Microsoft Graph):"]
    lines.extend(f"  {line}" for line in _format_meeting_options(effective))
    if event.get("type") in ("seriesMaster", "occurrence", "exception"):
        lines.append(
            "  The event is a recurring series or one of its occurrences: the series has one "
            "Teams meeting, so these options apply to every occurrence."
        )
    return lines, None


# The event as Outlook now shows it, after a create or an update.
_EVENT_READ_BACK_FIELDS = ("Subject", "Start", "End", "Location", "Organizer", "Status", "Attendees")


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the calendar tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request
    _collect_all = ctx.collect_all

    # ── Calendar Tools ─────────────────────────────────────────────────

    async def list_microsoft_calendar_events(
        start_datetime: str = "",
        end_datetime: str = "",
        days: int = 7,
        calendar_id: str = "",
    ) -> str | ToolResult:
        """List Outlook calendar events in a date range — supports past and future events.

        Inputs are ISO 8601 strings (e.g. ``"2026-05-06T22:33:00Z"``), matching
        ``create_microsoft_calendar_event``'s convention. The LLM passes JSON,
        so we never accept ``datetime`` objects directly.

        Pass ``calendar_id`` (from ``list_microsoft_calendars``) to read a
        specific calendar — including one that someone else shared with you and
        that you added to your Outlook. Omit it to read your default calendar.

        Each event is reported with its free/busy status (``showAs``) and an
        explicit "all day" marker so an all-day Busy / Out-of-office blocker is
        never mistaken for a free day.

        Defaults preserve the historical "next 7 days" behaviour:
          * If neither ``start_datetime`` nor ``end_datetime`` is provided,
            the range is ``[now, now + days]``.
          * If only ``start_datetime`` is provided, the range is
            ``[start, start + days]``.
          * If only ``end_datetime`` is provided, the range is
            ``[end - days, end]`` — useful for "last N days" lookbacks.
          * If both are provided, ``days`` is ignored.
        """
        # Parse ISO inputs first so we can validate before any network call.
        try:
            start = _parse_iso_datetime(start_datetime) if start_datetime else None
            end = _parse_iso_datetime(end_datetime) if end_datetime else None
        except ValueError as exc:
            return f"Error: {exc}"

        # ``bool`` is a subclass of ``int`` in Python — reject it explicitly
        # so the schema-declared integer contract is enforced (an LLM
        # sending ``days=true`` would otherwise silently get a 1-day window).
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            return "Error: 'days' must be a positive integer."

        window = timedelta(days=days)
        if start is None and end is None:
            start = datetime.now(UTC)
            end = start + window
        elif start is None and end is not None:
            start = end - window
        elif end is None:
            end = start + window

        if end <= start:
            return "Error: end_datetime must be after start_datetime."

        # Use Z suffix (not +00:00) — the '+' is a reserved URL character
        # and would be interpreted as a space if not percent-encoded.
        # Passing via the ``params`` dict ensures the httpx layer handles
        # encoding correctly for any non-ASCII edge case.
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Page through ``@odata.nextLink`` (calendarView returns ``$top`` events
        # per page) so a busy or multi-day window isn't silently truncated — an
        # all-day blocker on a packed day must never fall off the end. A hard
        # cap bounds the worst case; if it's reached the agent is told to narrow
        # the range so the truncation is visible rather than silent.
        events: list[dict] = []
        truncated = False
        next_url: str | None = _calendar_url(calendar_id, "calendarView")
        next_params: dict | None = {
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "$top": "50",
            "$orderby": "start/dateTime",
            # ``id`` is required so the agent can feed it back into
            # get/update/delete_microsoft_calendar_event (those tools document
            # this listing as the source of the event id). ``organizer`` is
            # intentionally NOT selected — the listing output never renders it,
            # so omitting it trims the payload on busy / paged windows.
            "$select": "id,subject,start,end,location,isAllDay,showAs,isCancelled,seriesMasterId",
        }
        async with _client() as client:
            while next_url:
                kw: dict = {"params": next_params} if next_params else {}
                resp, err = await _throttled_request(
                    client,
                    "get",
                    next_url,
                    _PROVIDER,
                    "list calendar events",
                    **kw,
                )
                if err:
                    return err
                data = resp.json()
                events.extend(data.get("value", []))
                # ``nextLink`` already encodes the query params, so drop ours.
                next_url = data.get("@odata.nextLink")
                next_params = None
                if len(events) >= _CALENDAR_EVENTS_MAX:
                    truncated = (
                        next_url is not None or len(events) > _CALENDAR_EVENTS_MAX
                    )
                    events = events[:_CALENDAR_EVENTS_MAX]
                    break
            if not events:
                return ToolResult(
                    tool_call_id="",
                    content=f"No calendar events between {start_iso} and {end_iso}.",
                    structured=[],
                )
            lines = []
            for e in events:
                subject = e.get("subject", "Untitled")
                location = e.get("location", {}).get("displayName", "")
                # ``showAs`` is the key free/busy signal: an all-day blocker set
                # to Busy / Out of office must read as busy, not free.
                status = _format_show_as(e.get("showAs"))
                prefix = f"[{status}] " if status else ""
                if e.get("isCancelled"):
                    prefix = "[Cancelled] " + prefix
                if e.get("isAllDay"):
                    # All-day events carry a date at midnight and Graph's end is
                    # exclusive (a one-day event ends on the following midnight).
                    # Show the inclusive date(s) plus an explicit "all day"
                    # marker so the empty clock time is never read as free.
                    start_day = (e.get("start", {}).get("dateTime", "") or "")[:10]
                    end_day = (e.get("end", {}).get("dateTime", "") or "")[:10]
                    try:
                        s_date = datetime.fromisoformat(start_day).date()
                        last_date = datetime.fromisoformat(end_day).date() - timedelta(
                            days=1
                        )
                        when = (
                            f"{start_day} (all day)"
                            if last_date <= s_date
                            else f"{start_day} -> {last_date.isoformat()} (all day)"
                        )
                    except ValueError:
                        when = f"{start_day} (all day)"
                else:
                    when = (
                        f"{e.get('start', {}).get('dateTime', '')} - "
                        f"{e.get('end', {}).get('dateTime', '')}"
                    )
                line = f"{prefix}{subject} | {when}"
                if location:
                    line += f" | {location}"
                # Surface the event id so it can be passed to
                # get/update/delete_microsoft_calendar_event (omit when absent).
                event_id = e.get("id", "")
                if event_id:
                    line += f" | id: {event_id}"
                if e.get("seriesMasterId"):
                    line += f" | series_id: {e['seriesMasterId']}"
                lines.append(line)
            if truncated:
                lines.append(
                    f"... (truncated at {_CALENDAR_EVENTS_MAX} events; narrow the "
                    "date range to see the rest)"
                )
            return ToolResult(tool_call_id="", content="\n".join(lines), structured=events)

    async def create_microsoft_calendar_event(
        subject: str,
        start_datetime: str,
        end_datetime: str,
        timezone: str = "UTC",
        body: str = "",
        location: str = "",
        attendees: str = "",
        is_online_meeting: bool = False,
        sensitivity: str = "normal",
        calendar_id: str = "",
        attachment_file_ids: list[str] | str | None = None,
        recurrence: dict | str | None = None,
        record_automatically: bool | None = None,
        allow_transcription: bool | None = None,
        allow_recording: bool | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Create a calendar event in Outlook. Sends invitations to attendees automatically.

        Pass ``calendar_id`` (from ``list_microsoft_calendars``) to create the
        event on a specific calendar — including a writable calendar shared with
        you. Omit it to use your default calendar.

        ``attachment_file_ids`` attaches files of this conversation.
        Graph sends the invitation when an event is created with attendees and
        sends nothing when a file is attached afterwards, so the event is
        created without them, the files attached, and the attendees added
        last: the one invitation they receive carries the files.

        ``recurrence`` makes the event a recurring series (a Graph
        ``patternedRecurrence``). ``record_automatically`` /
        ``allow_transcription`` / ``allow_recording`` set the options of its
        Teams meeting once the event exists, read back from Graph: a series
        has one meeting, so they cover every occurrence.
        """
        meeting_options, err = _requested_meeting_options(
            {
                "record_automatically": record_automatically,
                "allow_transcription": allow_transcription,
                "allow_recording": allow_recording,
            }
        )
        if err:
            return err
        if meeting_options and not is_online_meeting:
            return (
                "Error: record_automatically, allow_transcription and allow_recording are "
                "options of a Teams meeting: pass is_online_meeting=true to create the "
                "event as one."
            )
        try:
            patterned_recurrence = _build_patterned_recurrence(recurrence, start_datetime)
        except RecurrenceError as exc:
            return f"Error: {exc}"
        files, err = await _resolve_attachment_files(attachment_file_ids, file_context)
        if err:
            return err
        attendee_list = [a.strip() for a in attendees.split(",") if a.strip()]
        invitees = [
            {"emailAddress": {"address": addr}, "type": "required"} for addr in attendee_list
        ]
        async with _client() as client:
            payload: dict = {
                "subject": subject,
                "start": {"dateTime": start_datetime, "timeZone": timezone},
                "end": {"dateTime": end_datetime, "timeZone": timezone},
                "sensitivity": sensitivity,
            }
            if body:
                payload["body"] = {"contentType": "HTML", "content": body}
            if location:
                payload["location"] = {"displayName": location}
            if invitees and not files:
                payload["attendees"] = invitees
            if is_online_meeting:
                payload["isOnlineMeeting"] = True
                payload["onlineMeetingProvider"] = "teamsForBusiness"
            if patterned_recurrence:
                payload["recurrence"] = patterned_recurrence

            resp, err = await _throttled_request(
                client,
                "post",
                _calendar_url(calendar_id, "events"),
                _PROVIDER,
                "create calendar event",
                json=payload,
            )
            if err:
                return err
            event = resp.json()
            event_id = event.get("id", "")
            event_url = _calendar_url(calendar_id, f"events/{_url_encode(event_id, safe='')}")
            invitation = ""
            if files:
                attached, err = await _attach_files(ctx, client, event_url, files)
                if err:
                    so_far = ", ".join(attached) if attached else "none"
                    unsent = (
                        " It was NOT sent to the attendees: add them with "
                        "update_microsoft_calendar_event (attendees, and "
                        "attachment_file_ids for the files not attached yet)."
                        if invitees
                        else ""
                    )
                    return (
                        f"{err}\nThe event '{subject}' was created (ID: {event_id}) with "
                        f"these attachments so far: {so_far}.{unsent}"
                    )
                if invitees:
                    _, err = await _throttled_request(
                        client,
                        "patch",
                        event_url,
                        _PROVIDER,
                        "invite the attendees",
                        json={"attendees": invitees},
                    )
                    if err:
                        return (
                            f"{err}\nThe event '{subject}' was created (ID: {event_id}) "
                            f"with its attachments ({', '.join(attached)}), but the "
                            "attendees were NOT invited: add them with "
                            "update_microsoft_calendar_event."
                        )
                    invitation = "The attendees' invitation carries the attachments."
            options_report: list[str] = []
            if meeting_options:
                options_report, err = await _apply_meeting_options(
                    ctx, client, event_url, meeting_options, just_created=True
                )
                if err:
                    sent = " and its invitations were sent" if invitees else ""
                    return (
                        f"{err}\nThe event '{subject}' was created (ID: {event_id}){sent}, but "
                        "its Teams meeting options were NOT changed: retry with "
                        "update_microsoft_calendar_event."
                    )
            web_link = event.get("webLink", "")
            start_info = event.get("start", {}).get("dateTime", "")
            end_info = event.get("end", {}).get("dateTime", "")
            result_lines = [
                f"Calendar event created: '{subject}'",
                f"ID: {event_id}",
                f"When: {start_info} to {end_info} ({timezone})",
            ]
            if patterned_recurrence:
                result_lines.append(
                    f"Recurrence: {_describe_recurrence(event.get('recurrence') or patterned_recurrence)}. "
                    f"This ID is the series: list its occurrences with "
                    f"list_microsoft_event_occurrences(series_id={event_id})."
                )
            if location:
                result_lines.append(f"Location: {location}")
            if attendees:
                result_lines.append(f"Attendees: {attendees}")
            if files:
                result_lines.append(f"Attachments: {', '.join(attached)}")
            if invitation:
                result_lines.append(invitation)
            result_lines.extend(options_report)
            if web_link:
                result_lines.append(f"Link: {web_link}")
            return "\n".join(result_lines)

    # ── Calendar Management Tools ──────────────────────────────────────

    async def get_microsoft_calendar_event(
        event_id: str, calendar_id: str = ""
    ) -> str:
        """Get details of a specific calendar event by ID.

        Pass ``calendar_id`` to read an event from a specific calendar
        (including one shared with you); omit it to use your default calendar.
        A Teams meeting's options (recording, transcription, lobby, presenters)
        are read from its onlineMeeting.
        """
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                _calendar_url(calendar_id, f"events/{_url_encode(event_id, safe='')}"),
                _PROVIDER,
                "get calendar event",
                params={
                    "$select": "id,subject,start,end,location,organizer,attendees,body,isOnlineMeeting,onlineMeeting,webLink,recurrence,isAllDay,showAs,isCancelled,hasAttachments,isOrganizer,type,seriesMasterId",
                },
            )
            if err:
                return err
            e = resp.json()
            attachments_block = ""
            if e.get("hasAttachments"):
                attachments, att_err = await _list_outlook_attachments(
                    ctx,
                    client,
                    _calendar_url(calendar_id, f"events/{_url_encode(event_id, safe='')}"),
                )
                if att_err:
                    attachments_block = f"Attachments: could not be listed ({att_err})\n"
                elif attachments:
                    calendar_hint = " (and the same calendar_id)" if (calendar_id or "").strip() else ""
                    attachments_block = (
                        _format_outlook_attachments(
                            attachments,
                            "Read one inline and save it as a file with "
                            "get_microsoft_attachment, passing this event's ID as "
                            f"event_id{calendar_hint} and the attachment_id.",
                        )
                        + "\n"
                    )
            organizer = e.get("organizer", {}).get("emailAddress", {})
            attendees = e.get("attendees", [])
            attendee_lines = []
            for a in attendees[:10]:
                addr = a.get("emailAddress", {}).get("address", "")
                status = a.get("status", {}).get("response", "none")
                attendee_lines.append(f"  {addr} ({status})")
            body_content = _strip_html(e.get("body", {}).get("content", ""))
            result = (
                f"Subject: {e.get('subject', 'Untitled')}\n"
                f"ID: {e.get('id', '')}\n"
                f"Start: {e.get('start', {}).get('dateTime', '')} ({e.get('start', {}).get('timeZone', '')})\n"
                f"End: {e.get('end', {}).get('dateTime', '')} ({e.get('end', {}).get('timeZone', '')})\n"
                f"Location: {e.get('location', {}).get('displayName', 'None')}\n"
                f"Organizer: {organizer.get('name', '')} <{organizer.get('address', '')}>\n"
                f"Online meeting: {'Yes' if e.get('isOnlineMeeting') else 'No'}\n"
                f"All day: {'Yes' if e.get('isAllDay') else 'No'}\n"
                f"Shows as: {_format_show_as(e.get('showAs')) or 'Unknown'}\n"
            )
            if e.get("isCancelled"):
                result += "Status: Cancelled\n"
            if e.get("recurrence"):
                result += (
                    f"Recurrence: {_describe_recurrence(e['recurrence'])} (this ID is the series; "
                    "list_microsoft_event_occurrences lists its occurrences)\n"
                )
            elif e.get("type") in ("occurrence", "exception") and e.get("seriesMasterId"):
                result += (
                    "Recurring series: this is one occurrence"
                    + (", moved or edited from the series" if e["type"] == "exception" else "")
                    + f" (series_id: {e['seriesMasterId']}). Updating or deleting this ID "
                    "changes this occurrence only.\n"
                )
            join_url = (e.get("onlineMeeting") or {}).get("joinUrl")
            if e.get("isOnlineMeeting") and join_url:
                result += f"Teams join URL: {join_url}\n"
                meeting, meeting_err = await _find_online_meeting(ctx, client, join_url)
                if meeting_err:
                    result += f"Teams meeting options: could not be read ({meeting_err})\n"
                else:
                    result += (
                        "Teams meeting options:\n"
                        + "".join(f"  {line}\n" for line in _format_meeting_options(meeting))
                    )
            if attendee_lines:
                result += (
                    f"Attendees ({len(attendees)}):\n"
                    + "\n".join(attendee_lines)
                    + "\n"
                )
            if body_content:
                result += f"\nDescription:\n{body_content[:1000]}\n"
            web_link = e.get("webLink", "")
            if web_link:
                result += f"Link: {web_link}\n"
            # The description is shown as plain text: keep its link targets,
            # which carry a cloud-file (reference) attachment's URL.
            links = _body_links(e.get("body", {}).get("content", ""))
            if links:
                attachments_block += (
                    "Links in the description:\n" + "\n".join(f"- {url}" for url in links) + "\n"
                )
            return result + attachments_block

    async def update_microsoft_calendar_event(
        event_id: str,
        subject: str = "",
        start_datetime: str = "",
        end_datetime: str = "",
        timezone: str = "",
        body: str = "",
        location: str = "",
        attendees: str = "",
        sensitivity: str = "",
        calendar_id: str = "",
        attachment_file_ids: list[str] | str | None = None,
        record_automatically: bool | None = None,
        allow_transcription: bool | None = None,
        allow_recording: bool | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Update an existing calendar event in Outlook.

        Only the fields you provide will be updated; omitted fields
        remain unchanged.  ``attendees`` is a comma-separated list of
        email addresses (replaces existing attendees). Pass ``calendar_id``
        to target an event on a specific calendar (including one shared with
        you); omit it to use your default calendar.

        ``attachment_file_ids`` adds files of this conversation. Graph
        sends nothing to the attendees when a file is attached; the event
        update that follows is what sends them the updated invitation, and an
        update carrying only ``attendees`` reaches only the attendees who
        changed. So the files are attached first, then the organizer's update
        carries the event's body (the new one, or the current one unchanged)
        and reaches every attendee.

        An occurrence id changes that occurrence only (Graph makes it an
        exception of the series); a series id changes every occurrence. The
        meeting-option arguments change the event's Teams meeting, which a
        series and all its occurrences share.
        """
        meeting_options, err = _requested_meeting_options(
            {
                "record_automatically": record_automatically,
                "allow_transcription": allow_transcription,
                "allow_recording": allow_recording,
            }
        )
        if err:
            return err
        files, err = await _resolve_attachment_files(attachment_file_ids, file_context)
        if err:
            return err
        payload: dict = {}
        if subject:
            payload["subject"] = subject
        if start_datetime:
            payload["start"] = {
                "dateTime": start_datetime,
                "timeZone": timezone or "UTC",
            }
        if end_datetime:
            payload["end"] = {
                "dateTime": end_datetime,
                "timeZone": timezone or "UTC",
            }
        if body:
            payload["body"] = {"contentType": "HTML", "content": body}
        if location:
            payload["location"] = {"displayName": location}
        if attendees:
            attendee_list = [a.strip() for a in attendees.split(",") if a.strip()]
            payload["attendees"] = [
                {"emailAddress": {"address": addr}, "type": "required"}
                for addr in attendee_list
            ]
        if sensitivity:
            payload["sensitivity"] = sensitivity
        if not payload and not files and not meeting_options:
            return "Error: no fields to update. Provide at least one field."

        event_url = _calendar_url(calendar_id, f"events/{_url_encode(event_id, safe='')}")
        async with _client() as client:
            notice = ""
            answer = ""
            if files:
                current_resp, err = await _throttled_request(
                    client,
                    "get",
                    event_url,
                    _PROVIDER,
                    "read calendar event",
                    params={"$select": "id,subject,isOrganizer,attendees,body"},
                )
                if err:
                    return err
                current = current_resp.json() or {}
                attached, err = await _attach_files(ctx, client, event_url, files)
                if err:
                    so_far = ", ".join(attached) if attached else "none"
                    return (
                        f"{err}\nThe event (ID: {event_id}) was not updated and no "
                        f"update was sent; attached so far: {so_far}."
                    )
                has_attendees = bool(current.get("attendees") or payload.get("attendees"))
                if not current.get("isOrganizer"):
                    notice = (
                        "You are not the organizer: the attachments are on your copy "
                        "of the event only, the other attendees do not receive them."
                    )
                elif has_attendees:
                    if "body" not in payload and current.get("body"):
                        payload["body"] = current["body"]
                    notice = "The update sent to the attendees carries the attachments."
                if not payload:
                    answer = (
                        f"Calendar event '{current.get('subject', '')}' (ID: {event_id}): "
                        f"attached {', '.join(attached)}. {notice}".rstrip()
                    )
            if payload:
                resp, err = await _throttled_request(
                    client,
                    "patch",
                    event_url,
                    _PROVIDER,
                    "update calendar event",
                    json=payload,
                )
                if err:
                    if files:
                        return (
                            f"{err}\nThe attachments ({', '.join(attached)}) were added to "
                            f"the event (ID: {event_id}), but its update was not saved and "
                            "no update was sent to the attendees."
                        )
                    return err
                event = resp.json()
                answer = f"Calendar event updated: '{event.get('subject', '')}' (ID: {event.get('id', '')})."
                if files:
                    answer += f" Attachments: {', '.join(attached)}."
                if notice:
                    answer += f" {notice}"
                answer += _series_scope_note(event, changed="changed")
            if meeting_options:
                report, err = await _apply_meeting_options(ctx, client, event_url, meeting_options)
                if err:
                    if answer:
                        return f"{err}\n{answer} Its Teams meeting options were NOT changed."
                    return err
                answer = "\n".join([answer or f"Calendar event {event_id}: Teams meeting options updated.", *report])
            return answer

    async def delete_microsoft_calendar_event(
        event_id: str, calendar_id: str = "", comment: str = ""
    ) -> str:
        """Delete (cancel) a calendar event in Outlook.

        When you organize a meeting with attendees it is cancelled through
        ``POST /events/{id}/cancel``, which sends them a cancellation notice
        carrying ``comment``; any other event is deleted. An occurrence id
        removes that occurrence only, a series id the whole series. Pass
        ``calendar_id`` to target an event on a specific calendar (including
        one shared with you); omit it to use your default calendar.
        """
        event_url = _calendar_url(calendar_id, f"events/{_url_encode(event_id, safe='')}")
        comment = (comment or "").strip()
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                event_url,
                _PROVIDER,
                "read calendar event",
                params={"$select": "id,subject,isOrganizer,attendees,type,seriesMasterId"},
            )
            if err:
                return err
            event = resp.json() or {}
            scope = _series_scope_note(event, changed="cancelled" if event.get("isOrganizer") else "removed")
            attendees = event.get("attendees") or []
            if event.get("isOrganizer") and attendees:
                _, err = await _throttled_request(
                    client,
                    "post",
                    f"{event_url}/cancel",
                    _PROVIDER,
                    "cancel calendar event",
                    # The action's parameter is "Comment" in Graph's metadata.
                    json={"Comment": comment} if comment else {},
                )
                if err:
                    return err
                with_comment = " with your comment" if comment else ""
                return (
                    f"Calendar event {event_id} cancelled: its {len(attendees)} attendee(s) "
                    f"received a cancellation notice{with_comment}.{scope}"
                )
            _, err = await _throttled_request(
                client,
                "delete",
                event_url,
                _PROVIDER,
                "delete calendar event",
            )
            if err:
                return err
            answer = f"Calendar event {event_id} deleted.{scope}"
            if event.get("isOrganizer") is False and attendees:
                answer += (
                    " You are not its organizer: it is removed from your calendar only, "
                    "and nobody is notified."
                )
            if comment:
                answer += (
                    " The comment was not sent: only the organizer of a meeting with "
                    "attendees sends a cancellation notice."
                )
            return answer

    async def respond_microsoft_calendar_event(
        event_id: str,
        response: str,
        comment: str = "",
        send_response: bool | str = True,
        calendar_id: str = "",
        proposed_new_time: dict | str | None = None,
    ) -> str:
        """Accept, tentatively accept or decline a meeting invitation the user received.

        Graph's ``accept`` / ``tentativelyAccept`` / ``decline`` action of the
        event, on the default calendar or ``calendar_id``. The event is read
        first: one the user organizes, or one its organizer cancelled, holds
        no invitation and is refused before any write. It is read back after
        the response, for the response Graph now reports. An occurrence id
        answers that occurrence only, a series id the whole series.
        """
        choice = _INVITATION_RESPONSES.get(response.strip().lower() if isinstance(response, str) else "")
        if choice is None:
            return f"Error: response must be accept, tentatively_accept or decline, not {response!r}."
        if isinstance(send_response, str) and send_response.strip().lower() in ("true", "false"):
            send_response = send_response.strip().lower() == "true"
        if not isinstance(send_response, bool):
            return f"Error: send_response must be true or false, not {send_response!r}."
        proposed, err = _proposed_time_slot(proposed_new_time)
        if err:
            return err
        if proposed and choice.action == "accept":
            return (
                "Error: proposed_new_time goes with tentatively_accept or decline; "
                "an acceptance proposes no other time."
            )
        if proposed and not send_response:
            return (
                "Error: proposed_new_time is sent to the organizer with the response, "
                "so it needs send_response=true."
            )
        event_key = event_id.strip() if isinstance(event_id, str) else ""
        if not event_key:
            return "Error: event_id is required: the invitation's ID from list_microsoft_calendar_events."
        comment = (comment or "").strip()
        event_url = _calendar_url(calendar_id, f"events/{_url_encode(event_key, safe='')}")
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                event_url,
                _PROVIDER,
                "read the invitation",
                params={
                    "$select": "id,subject,start,end,organizer,isOrganizer,responseStatus,responseRequested,allowNewTimeProposals,isCancelled,type,seriesMasterId"
                },
            )
            if err:
                return err
            event = resp.json() or {}
            subject = event.get("subject", "")
            before = (event.get("responseStatus") or {}).get("response") or ""
            if event.get("isOrganizer") or before == "organizer":
                return (
                    f"Error: you organize '{subject}' (ID: {event_key}), so it holds no "
                    "invitation to respond to; nothing was changed. Change your own event "
                    "with update_microsoft_calendar_event, or cancel it with "
                    "delete_microsoft_calendar_event."
                )
            if event.get("isCancelled"):
                return (
                    f"Error: the organizer cancelled '{subject}' (ID: {event_key}), so there "
                    "is no invitation left to respond to; nothing was changed. "
                    "delete_microsoft_calendar_event removes it from your calendar."
                )
            if proposed and event.get("allowNewTimeProposals") is False:
                return (
                    f"Error: the organizer of '{subject}' does not allow new time proposals; "
                    "nothing was sent. Respond without proposed_new_time."
                )
            # The action's parameters are "Comment", "SendResponse" and
            # "ProposedNewTime" in Graph's metadata (as the Graph SDKs send them).
            payload: dict = {"SendResponse": send_response}
            if comment:
                payload["Comment"] = comment
            if proposed:
                payload["ProposedNewTime"] = proposed
            _, err = await _throttled_request(
                client,
                "post",
                f"{event_url}/{choice.action}",
                _PROVIDER,
                f"{choice.verb} the invitation",
                json=payload,
            )
            if err:
                return err
            waits = list(_RESPONSE_READ_BACK_DELAYS)
            after: dict = {}
            now = ""
            while True:
                back, read_err = await _throttled_request(
                    client,
                    "get",
                    event_url,
                    _PROVIDER,
                    "read the event back",
                    params={"$select": "id,subject,start,end,responseStatus,webLink"},
                    expected_statuses={404},
                )
                if read_err:
                    break
                after = back.json() or {}
                now = (after.get("responseStatus") or {}).get("response") or ""
                if now == choice.status or not waits:
                    break
                await asyncio.sleep(waits.pop(0))

        if read_err and not (choice.action == "decline" and getattr(back, "status_code", None) == 404):
            return (
                f"{read_err}\nThe response ({choice.verb}) was sent to Microsoft Graph, but the "
                "event could not be read back, so it is not confirmed. Read it with "
                "get_microsoft_calendar_event before responding again."
            )
        if not read_err and now != choice.status:
            return (
                f"Error: Microsoft Graph took the response ({choice.verb}), but the event "
                f"reads back with your response as '{now or 'none'}', so it is not confirmed. "
                "Read it with get_microsoft_calendar_event before responding again."
            )
        organizer = (event.get("organizer") or {}).get("emailAddress") or {}
        address = organizer.get("address", "")
        named = f"{organizer.get('name', '')} <{address}>".strip() if address else organizer.get("name", "")
        if send_response:
            sent = f"Response sent to the organizer{f' ({named})' if named else ''}: yes"
            sent += ", with your comment" if comment else ""
            sent += ", proposing the new time" if proposed else ""
            if event.get("responseRequested") is False:
                sent += " (the organizer did not ask for responses)"
        else:
            sent = "Response sent to the organizer: no (send_response=false), it is recorded in your calendar only"
            sent += "; the comment was not sent" if comment else ""
        # Outlook can take a declined meeting off the calendar: it is then shown as read before the response.
        shown = event if read_err else after
        start, end = shown.get("start") or {}, shown.get("end") or {}
        lines = [
            f"Invitation {choice.done}: '{shown.get('subject', subject)}' (ID: {shown.get('id') or event_key})",
            f"Start: {start.get('dateTime', '')} ({start.get('timeZone', '')})",
            f"End: {end.get('dateTime', '')} ({end.get('timeZone', '')})",
            (
                "Your response could not be read back: Microsoft Graph no longer finds the "
                "event on this calendar (Outlook can take a declined meeting off the calendar)."
                if read_err
                else f"Your response (read back from Microsoft Graph): {now}"
                + (f" (before: {before})" if before and before != now else "")
            ),
            sent,
        ]
        if proposed:
            lines.append(
                f"Proposed new time: {proposed['start']['dateTime']} - "
                f"{proposed['end']['dateTime']} ({proposed['start']['timeZone']})"
            )
        if not read_err and after.get("webLink"):
            lines.append(f"Link: {after['webLink']}")
        scope = _series_scope_note(event, changed="answered").strip()
        if scope:
            lines.append(scope)
        return "\n".join(lines)

    async def list_microsoft_event_occurrences(
        series_id: str, start: str, end: str, calendar_id: str = ""
    ) -> str:
        """List the occurrences of a recurring series between two dates.

        ``series_id`` may also be the id of one of its occurrences. Times are
        shown in the series' own time zone (UTC, and said so, when it has none
        Graph can apply), and a bare date (``2026-12-31``) is a whole day of
        that calendar, both bounds included; a date-time bound is the exact
        instant. The occurrences cancelled in the window are listed after the
        live ones (Graph keeps them off the instances, by date only).
        """
        try:
            window_start = _parse_iso_datetime(start)
            window_end = _parse_iso_datetime(end)
        except ValueError as exc:
            return f"Error: {exc}"
        first_day, last_day = _bare_date(start), _bare_date(end)
        if (window_end + timedelta(days=1) if last_day else window_end) <= window_start:
            return "Error: end must be after start (an end date is included)."
        series = (series_id or "").strip()
        if not series:
            return (
                "Error: series_id is required: the ID of a recurring event "
                "(create_microsoft_calendar_event or list_microsoft_calendar_events)."
            )
        async with _client() as client:
            # cancelledOccurrences is only served on a series master's own
            # read, so the series is found first and read on its own.
            resp, err = await _throttled_request(
                client,
                "get",
                _calendar_url(calendar_id, f"events/{_url_encode(series, safe='')}"),
                _PROVIDER,
                "read the recurring event",
                params={"$select": "id,type,seriesMasterId"},
            )
            if err:
                return err
            event = resp.json() or {}
            if event.get("type") in ("occurrence", "exception") and event.get("seriesMasterId"):
                series = event["seriesMasterId"]
            elif event.get("type") != "seriesMaster":
                return (
                    f"Error: event {series_id} is not part of a recurring series "
                    f"(its type is {event.get('type') or 'unknown'})."
                )
            master_url = _calendar_url(calendar_id, f"events/{_url_encode(series, safe='')}")
            resp, err = await _throttled_request(
                client,
                "get",
                master_url,
                _PROVIDER,
                "read the recurring series",
                params={"$select": "id,subject,recurrence,originalStartTimeZone,cancelledOccurrences"},
            )
            if err:
                return err
            master = resp.json() or {}
            zone = master.get("originalStartTimeZone") or ""
            # A legacy custom zone reads "tzone://Microsoft/Custom": no header for it.
            prefer = (
                {"Prefer": f'outlook.timezone="{zone}"'}
                if _PREFER_TIME_ZONE_RE.fullmatch(zone) and "://" not in zone
                else None
            )
            # A bare date's day starts at a local midnight Graph cannot be told
            # without the zone's offset: ask a day wider on that side (wider
            # than any UTC offset) and keep the occurrences by their local date.
            query_start = window_start - timedelta(days=1) if first_day else window_start
            query_end = window_end + timedelta(days=2) if last_day else window_end

            def in_window(occurrence: dict) -> bool:
                day = ((occurrence.get("start") or {}).get("dateTime") or "")[:10]
                return (not first_day or day >= first_day.isoformat()) and (
                    not last_day or day <= last_day.isoformat()
                )

            occurrences: list[dict] = []
            truncated = False
            next_url: str | None = f"{master_url}/instances"
            next_params: dict | None = {
                "startDateTime": query_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": query_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "$select": "id,start,end,type,isCancelled",
                "$top": "100",
            }
            while next_url:
                kw: dict = {"params": next_params} if next_params else {}
                if prefer:
                    kw["headers"] = prefer
                resp, err = await _throttled_request(
                    client, "get", next_url, _PROVIDER, "list event occurrences", **kw
                )
                if err:
                    return err
                data = resp.json() or {}
                occurrences.extend(o for o in data.get("value", []) if in_window(o))
                # ``nextLink`` already encodes the query params, so drop ours.
                next_url = data.get("@odata.nextLink")
                next_params = None
                if len(occurrences) > _OCCURRENCES_MAX:
                    truncated = True
                    break
        occurrences.sort(key=lambda o: (o.get("start") or {}).get("dateTime") or "")
        lower = first_day or window_start.date()
        upper = last_day or (window_end - timedelta(microseconds=1)).date()
        window = f"between {lower.isoformat()} and {upper.isoformat()}"
        lines = [f"Recurring series '{master.get('subject', '')}' (series_id: {series})"]
        if master.get("recurrence"):
            lines.append(f"Pattern: {_describe_recurrence(master['recurrence'])}")
        if not prefer:
            lines.append(
                "Times and dates are in UTC: the series carries no time zone Microsoft Graph can show them in."
            )
        if truncated:
            lines.append(f"Occurrences {window} (the first {_OCCURRENCES_MAX}; narrow the window to see the rest):")
            lines.extend(f"- {_format_occurrence(o)}" for o in occurrences[:_OCCURRENCES_MAX])
        elif occurrences:
            lines.append(f"Occurrences {window} ({len(occurrences)}):")
            lines.extend(f"- {_format_occurrence(o)}" for o in occurrences)
        else:
            lines.append(f"No occurrences {window}.")
        # A cancelled occurrence is known by its date in the series' calendar
        # only, so it is kept by date - against a date-time bound's UTC date.
        cancelled = sorted(
            day
            for day in (_occurrence_date(o) for o in master.get("cancelledOccurrences") or [])
            if lower.isoformat() <= day <= upper.isoformat()
        )
        if cancelled:
            by_date = "" if first_day and last_day else " (matched by date: Graph keeps only their date)"
            lines.append(f"Cancelled occurrences {window} ({len(cancelled)}){by_date}: {', '.join(cancelled)}")
        return "\n".join(lines)

    async def list_microsoft_calendars() -> str:
        """List the user's Outlook calendars, including calendars that OTHER
        people have shared with the user and that have been added to their
        Outlook.

        Returns each calendar's name, id (pass it as ``calendar_id`` to the
        other calendar tools), owner, and whether it is writable. Use this to
        discover a coworker's shared calendar before reading its events.
        """
        async with _client() as client:
            # Page through ``@odata.nextLink`` (via the shared ``_collect_all``
            # helper) so a user with many calendars - the exact case this
            # discovery tool exists for - never has a shared calendar silently
            # omitted because it landed on a later page.
            calendars, err = await _collect_all(
                client,
                f"{_BASE_URL}/me/calendars",
                "list calendars",
                params={
                    "$select": "id,name,owner,canEdit,isDefaultCalendar",
                    "$top": "100",
                },
            )
            if err:
                return err
            if not calendars:
                return "No calendars found."
            lines = []
            for c in calendars:
                owner = c.get("owner", {}) or {}
                owner_addr = owner.get("address", "")
                flags = []
                if c.get("isDefaultCalendar"):
                    flags.append("default")
                flags.append("editable" if c.get("canEdit") else "read-only")
                owner_part = f" | owner: {owner_addr}" if owner_addr else ""
                lines.append(
                    f"{c.get('name', 'Untitled')} ({', '.join(flags)})"
                    f"{owner_part} | id: {c.get('id', '')}"
                )
            return "\n".join(lines)

    async def get_microsoft_free_busy(
        attendees: str = "",
        start_datetime: str = "",
        end_datetime: str = "",
        days: int = 7,
        interval_minutes: int = 30,
    ) -> str:
        """Return the merged free/busy schedule for one or more people.

        This is the authoritative way to answer "when is X free?": Microsoft
        Graph computes the merged busy/free view, correctly accounting for
        all-day Busy / Out-of-office blockers, tentative holds, and recurring
        events. It works for OTHER people in the organisation even when their
        full calendar is not shared with you (org-wide free/busy is visible by
        default).

        ``attendees`` is a comma-separated list of email addresses. When empty,
        the signed-in user's own schedule is returned. Date inputs are ISO 8601
        and follow the same defaulting rules as
        ``list_microsoft_calendar_events`` (defaults to the next ``days`` days).
        """
        try:
            start = _parse_iso_datetime(start_datetime) if start_datetime else None
            end = _parse_iso_datetime(end_datetime) if end_datetime else None
        except ValueError as exc:
            return f"Error: {exc}"
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            return "Error: 'days' must be a positive integer."
        # Graph ``getSchedule`` requires ``availabilityViewInterval`` to be an
        # integer number of minutes in [5, 1440] and a multiple of 5; anything
        # else is rejected by Graph with an opaque HTTP 400. Validate up front
        # and return a tool-readable error instead of making a doomed call.
        if (
            isinstance(interval_minutes, bool)
            or not isinstance(interval_minutes, int)
            or interval_minutes < 5
            or interval_minutes > 1440
            or interval_minutes % 5 != 0
        ):
            return (
                "Error: 'interval_minutes' must be an integer between 5 and 1440 "
                "in multiples of 5 (Microsoft Graph requirement)."
            )

        window = timedelta(days=days)
        if start is None and end is None:
            start = datetime.now(UTC)
            end = start + window
        elif start is None and end is not None:
            start = end - window
        elif end is None:
            end = start + window
        if end <= start:
            return "Error: end_datetime must be after start_datetime."

        schedules = [a.strip() for a in attendees.split(",") if a.strip()]
        async with _client() as client:
            if not schedules:
                me_resp, me_err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/me",
                    _PROVIDER,
                    "resolve current user",
                    params={"$select": "mail,userPrincipalName"},
                )
                if me_err:
                    return me_err
                me = me_resp.json()
                self_addr = me.get("mail") or me.get("userPrincipalName")
                if not self_addr:
                    return (
                        "Error: could not resolve your email address for the "
                        "free/busy lookup."
                    )
                schedules = [self_addr]

            start_iso = start.strftime("%Y-%m-%dT%H:%M:%S")
            end_iso = end.strftime("%Y-%m-%dT%H:%M:%S")
            resp, err = await _throttled_request(
                client,
                "post",
                f"{_BASE_URL}/me/calendar/getSchedule",
                _PROVIDER,
                "get free/busy schedule",
                json={
                    "schedules": schedules,
                    "startTime": {"dateTime": start_iso, "timeZone": "UTC"},
                    "endTime": {"dateTime": end_iso, "timeZone": "UTC"},
                    "availabilityViewInterval": interval_minutes,
                },
            )
            if err:
                return err
            results = resp.json().get("value", [])
            if not results:
                return "No free/busy information returned."

            blocks: list[str] = []
            for sched in results:
                who = sched.get("scheduleId", "unknown")
                items = sched.get("scheduleItems", []) or []
                if items:
                    lines = [f"{who} (busy blocks, UTC):"]
                    for it in items:
                        status = _format_show_as(it.get("status")) or "Busy"
                        s = it.get("start", {}).get("dateTime", "")
                        e_ = it.get("end", {}).get("dateTime", "")
                        subject = it.get("subject", "")
                        label = f"  {status}: {s} - {e_}"
                        if subject:
                            label += f" ({subject})"
                        lines.append(label)
                    blocks.append("\n".join(lines))
                    continue
                # No detailed items (e.g. limited free/busy visibility for an
                # external attendee): fall back to the per-interval availability
                # map so the agent can still tell free from busy.
                view = sched.get("availabilityView", "") or ""
                if not view or set(view) <= {"0"}:
                    blocks.append(f"{who}: Free for the whole window.")
                else:
                    legend = ", ".join(
                        sorted(
                            {
                                _AVAILABILITY_VIEW_LABELS.get(ch, ch)
                                for ch in view
                                if ch != "0"
                            }
                        )
                    )
                    blocks.append(
                        f"{who}: Has {legend} time in the window (no detailed "
                        f"blocks visible). Availability map "
                        f"({interval_minutes}-min slots): {view}"
                    )
            return "\n\n".join(blocks)

    executor.register("list_microsoft_calendars", list_microsoft_calendars)
    executor.register("list_microsoft_calendar_events", list_microsoft_calendar_events)
    executor.register("get_microsoft_free_busy", get_microsoft_free_busy)
    executor.register("create_microsoft_calendar_event", create_microsoft_calendar_event)
    executor.register("get_microsoft_calendar_event", get_microsoft_calendar_event)
    executor.register("update_microsoft_calendar_event", update_microsoft_calendar_event)
    executor.register("delete_microsoft_calendar_event", delete_microsoft_calendar_event)
    executor.register("respond_microsoft_calendar_event", respond_microsoft_calendar_event)
    executor.register("list_microsoft_event_occurrences", list_microsoft_event_occurrences)

    return [
        # ── Calendar ──
        ToolDefinition(
            name="list_microsoft_calendars",
            description=(
                "List the user's Outlook calendars, including calendars that "
                "other people shared with the user and that were added to their "
                "Outlook. Returns each calendar's name, owner, writability, and "
                "id. Use the returned id as 'calendar_id' on the other calendar "
                "tools to read or write a specific (including shared) calendar."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="list_microsoft_calendar_events",
            description=(
                "List Outlook calendar events between two dates. Supports both "
                "past and future events. Defaults to the next 7 days when no "
                "dates are provided. Pass ISO 8601 datetime strings (e.g. "
                "'2026-05-06T22:33:00Z'). Each event includes its free/busy "
                "status (Free / Tentative / Busy / Out of office) and an "
                "explicit 'all day' marker, so an all-day Busy blocker is "
                "reported as busy. To check overall availability (especially "
                "for other people), prefer get_microsoft_free_busy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "start_datetime": {
                        "type": "string",
                        "description": (
                            "Start of the range, ISO 8601 (e.g. "
                            "'2026-05-06T22:33:00Z'). Defaults to now (UTC) "
                            "if neither start nor end is provided; defaults "
                            "to end - days if only end is provided."
                        ),
                    },
                    "end_datetime": {
                        "type": "string",
                        "description": (
                            "End of the range, ISO 8601 (e.g. "
                            "'2026-05-13T22:33:00Z'). Defaults to start + "
                            "days when only start is provided."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": (
                            "Window size in days, used only when start or "
                            "end is missing. Ignored when both are provided. "
                            "Must be a positive integer."
                        ),
                        "default": 7,
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": (
                            "Optional calendar id (from list_microsoft_calendars) "
                            "to read a specific calendar, including one shared "
                            "with you. Omit to read your default calendar."
                        ),
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="get_microsoft_free_busy",
            description=(
                "Get the merged free/busy schedule for one or more people - the "
                "authoritative way to answer 'when is X free?'. Microsoft Graph "
                "merges all events (including all-day Busy / Out-of-office "
                "blockers, tentative holds, and recurring meetings) into busy "
                "blocks. Works for other people in the organisation even without "
                "their full calendar being shared (org-wide free/busy is visible "
                "by default). Defaults to the signed-in user and the next 7 days."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attendees": {
                        "type": "string",
                        "description": (
                            "Comma-separated email addresses to look up. Leave "
                            "empty to use the signed-in user's own calendar."
                        ),
                        "default": "",
                    },
                    "start_datetime": {
                        "type": "string",
                        "description": (
                            "Start of the range, ISO 8601. Defaults to now (UTC)."
                        ),
                        "default": "",
                    },
                    "end_datetime": {
                        "type": "string",
                        "description": (
                            "End of the range, ISO 8601. Defaults to start + days."
                        ),
                        "default": "",
                    },
                    "days": {
                        "type": "integer",
                        "description": (
                            "Window size in days when start or end is missing. "
                            "Must be a positive integer."
                        ),
                        "default": 7,
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": (
                            "Granularity of the availability map in minutes. "
                            "Microsoft Graph requires a multiple of 5 between 5 "
                            "and 1440. Defaults to 30."
                        ),
                        "default": 30,
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="create_microsoft_calendar_event",
            description="Create a calendar event in Outlook. Can include attendees (sends invitations automatically), a location, and optionally create a Teams online meeting. Attach files of this conversation with attachment_file_ids: the files are attached before the attendees are added, so the one invitation they receive carries them. recurrence makes it a recurring series (daily, weekly, monthly or yearly; the returned ID is the series). For a Teams meeting, record_automatically / allow_transcription / allow_recording set its recording and transcription options, and the result reports the values read back from Teams (a series has one Teams meeting, so they cover every occurrence).",
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Event title/subject"},
                    "start_datetime": {
                        "type": "string",
                        "description": "Start date and time in ISO format (e.g. '2025-03-15T10:00:00')",
                    },
                    "end_datetime": {
                        "type": "string",
                        "description": "End date and time in ISO format (e.g. '2025-03-15T11:00:00')",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Time zone (default 'UTC'). E.g. 'Eastern Standard Time', 'Pacific Standard Time', 'Romance Standard Time'",
                        "default": "UTC",
                    },
                    "body": {
                        "type": "string",
                        "description": "Event description/body (HTML supported)",
                        "default": "",
                    },
                    "location": {
                        "type": "string",
                        "description": "Location name (e.g. 'Conference Room 1')",
                        "default": "",
                    },
                    "attendees": {
                        "type": "string",
                        "description": "Comma-separated email addresses of required attendees",
                        "default": "",
                    },
                    "is_online_meeting": {
                        "type": "boolean",
                        "description": "Create as Teams online meeting (default false)",
                        "default": False,
                    },
                    "sensitivity": {
                        "type": "string",
                        "description": "Event sensitivity/visibility: 'normal' (default), 'personal', 'private', or 'confidential'",
                        "enum": ["normal", "personal", "private", "confidential"],
                        "default": "normal",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id (from list_microsoft_calendars) to create the event on a specific calendar, including a writable shared one. Omit to use your default calendar.",
                        "default": "",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                    "recurrence": _RECURRENCE_SCHEMA,
                    **_MEETING_OPTION_SCHEMA,
                },
                "required": ["subject", "start_datetime", "end_datetime"],
            },
            # The event as Outlook now shows it, and an invitation that
            # overlaps another event of the organizer.
            read_back=WriteReadBack(
                kind="event",
                identity=("event_id", "calendar_id"),
                id_pattern=r"(?m)^ID: (\S+)",
                read_tool="get_microsoft_calendar_event",
                fields=_EVENT_READ_BACK_FIELDS,
                body_label="Description",
                overlap_tool="list_microsoft_calendar_events",
                overlap_window=("start_datetime", "end_datetime"),
            ),
            source="integration:microsoft",
            priority=0,
        ),
        ToolDefinition(
            name="get_microsoft_calendar_event",
            description="Get full details of a specific calendar event by ID, including attendees, their RSVP status, description, recurrence (or, for one occurrence, its series_id), the Teams join URL and meeting options (record and transcribe automatically, allow transcription, allow recording, who can bypass the lobby, who can present), and each attachment with its attachment_id (read or save one with get_microsoft_attachment).",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The calendar event ID (from list_microsoft_calendar_events output)",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id (from list_microsoft_calendars) when the event lives on a specific (including shared) calendar. Omit to use your default calendar.",
                        "default": "",
                    },
                },
                "required": ["event_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="update_microsoft_calendar_event",
            description="Update an existing calendar event in Outlook. Only provided fields are updated; omitted fields remain unchanged. Attendees replacement sends updated invitations automatically. attachment_file_ids adds files of this conversation: Outlook sends nothing when a file is attached, so when you organize the meeting the tool then updates the event and every attendee receives the updated invitation with the files; when you are only an attendee, the files are added to your own copy of the event. An occurrence ID (from list_microsoft_event_occurrences) moves or edits that occurrence only; a series ID changes every occurrence. record_automatically / allow_transcription / allow_recording change the event's Teams meeting options (one meeting for a whole series), and the result reports the values read back from Teams.",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The calendar event ID to update: a one-off event, a series, or one occurrence of a series",
                    },
                    "subject": {
                        "type": "string",
                        "description": "New event title",
                        "default": "",
                    },
                    "start_datetime": {
                        "type": "string",
                        "description": "New start time in ISO format",
                        "default": "",
                    },
                    "end_datetime": {
                        "type": "string",
                        "description": "New end time in ISO format",
                        "default": "",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Time zone for start/end",
                        "default": "",
                    },
                    "body": {
                        "type": "string",
                        "description": "New event description (HTML supported)",
                        "default": "",
                    },
                    "location": {
                        "type": "string",
                        "description": "New location name",
                        "default": "",
                    },
                    "attendees": {
                        "type": "string",
                        "description": "Comma-separated email addresses (replaces existing attendees)",
                        "default": "",
                    },
                    "sensitivity": {
                        "type": "string",
                        "description": "Event sensitivity/visibility: 'normal', 'personal', 'private', or 'confidential'",
                        "enum": ["normal", "personal", "private", "confidential"],
                        "default": "",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id (from list_microsoft_calendars) when the event lives on a specific (including shared) calendar. Omit to use your default calendar.",
                        "default": "",
                    },
                    "attachment_file_ids": _ATTACHMENT_FILE_IDS_PROPERTY,
                    **_MEETING_OPTION_SCHEMA,
                },
                "required": ["event_id"],
            },
            # An empty field leaves the event unchanged: two updates of one
            # event in a round are one PATCH, one attendee notification.
            coalesce=WriteCoalescing(
                identity=("event_id", "calendar_id"),
                mode="patch",
                union_lists=("attachment_file_ids",),
            ),
            read_back=WriteReadBack(
                kind="event",
                identity=("event_id", "calendar_id"),
                read_tool="get_microsoft_calendar_event",
                fields=_EVENT_READ_BACK_FIELDS,
                body_label="Description",
                overlap_tool="list_microsoft_calendar_events",
                overlap_window=("start_datetime", "end_datetime"),
            ),
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="delete_microsoft_calendar_event",
            description="Delete (cancel) a calendar event in Outlook. When you organize a meeting with attendees, it is cancelled and they receive a cancellation notice carrying the optional comment. An occurrence ID (from list_microsoft_event_occurrences) cancels that occurrence only; a series ID cancels the whole series.",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The calendar event ID to delete: a one-off event, a series, or one occurrence of a series",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id (from list_microsoft_calendars) when the event lives on a specific (including shared) calendar. Omit to use your default calendar.",
                        "default": "",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional message sent to the attendees with the cancellation notice",
                        "default": "",
                    },
                },
                "required": ["event_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="respond_microsoft_calendar_event",
            description="Respond to a meeting invitation the user RECEIVED: accept, tentatively_accept or decline it, with an optional comment to the organizer. It never creates or edits the event; to change an event the user organizes, use update_microsoft_calendar_event (an event the user organizes is refused here). An occurrence ID (from list_microsoft_event_occurrences) answers that occurrence only; a series ID answers the whole series. send_response=false records the answer without notifying the organizer. tentatively_accept and decline can propose another time (proposed_new_time) when the organizer allows it. The result is the event read back: subject, start, end, the response Microsoft now reports (accepted, tentativelyAccepted or declined), whether the organizer was notified, and its link.",
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The invitation's event ID (from list_microsoft_calendar_events): a one-off meeting, a series, or one occurrence of a series",
                    },
                    "response": {
                        "type": "string",
                        "enum": list(_INVITATION_RESPONSES),
                        "description": "accept, tentatively_accept or decline",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional message sent to the organizer with the response",
                        "default": "",
                    },
                    "send_response": {
                        "type": "boolean",
                        "description": "Send the response to the organizer (default true); false records it in the calendar without notifying the organizer",
                        "default": True,
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id (from list_microsoft_calendars) when the invitation lives on a specific (including shared) calendar. Omit to use your default calendar.",
                        "default": "",
                    },
                    "proposed_new_time": {
                        "type": "object",
                        "description": "Optional, with tentatively_accept or decline only: the time proposed instead, sent to the organizer with the response (needs send_response true and an organizer who allows new time proposals)",
                        "properties": {
                            "start_datetime": {
                                "type": "string",
                                "description": "Proposed start in ISO format (e.g. '2026-10-30T11:00:00')",
                            },
                            "end_datetime": {
                                "type": "string",
                                "description": "Proposed end in ISO format (e.g. '2026-10-30T11:30:00')",
                            },
                            "timezone": {
                                "type": "string",
                                "description": "Time zone of start and end (default 'UTC'). E.g. 'Eastern Standard Time', 'Romance Standard Time'",
                                "default": "UTC",
                            },
                        },
                        "required": ["start_datetime", "end_datetime"],
                    },
                },
                "required": ["event_id", "response"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="list_microsoft_event_occurrences",
            description="List the occurrences of a recurring Outlook series between two dates, in the series' time zone, each with its ID (pass it to update_microsoft_calendar_event to move or edit that occurrence, or to delete_microsoft_calendar_event to cancel it, without touching the rest of the series), followed by the occurrences already cancelled in the window.",
            input_schema={
                "type": "object",
                "properties": {
                    "series_id": {
                        "type": "string",
                        "description": "ID of the recurring series (or of one of its occurrences)",
                    },
                    "start": {
                        "type": "string",
                        "description": "Start of the window: a date (e.g. '2026-10-01', the whole day in the series' time zone) or an ISO 8601 date-time (an exact instant)",
                    },
                    "end": {
                        "type": "string",
                        "description": "End of the window: a date (e.g. '2027-01-31', included) or an ISO 8601 date-time (excluded)",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Optional calendar id (from list_microsoft_calendars) when the series lives on a specific (including shared) calendar. Omit to use your default calendar.",
                        "default": "",
                    },
                },
                "required": ["series_id", "start", "end"],
            },
            source="integration:microsoft",
            priority=1,
        ),
    ]
