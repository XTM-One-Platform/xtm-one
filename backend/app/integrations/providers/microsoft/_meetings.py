"""Online-meeting helpers shared by the meeting, transcript and calendar tools."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote as _url_encode
from urllib.parse import unquote as _url_decode

import httpx

from app.integrations.http import AutoRefreshClient

from ._common import _BASE_URL, _PROVIDER, MicrosoftContext

# The Teams meeting options a calendar tool sets: tool argument -> the
# onlineMeeting property (Graph v1.0) it writes.
_MEETING_OPTION_PROPERTIES = {
    "record_automatically": "recordAutomatically",
    "allow_transcription": "allowTranscription",
    "allow_recording": "allowRecording",
}

# Seconds to wait before looking again for the Teams meeting of an event that
# was just created: Graph can answer the event before its meeting exists.
_ONLINE_MEETING_RETRY_DELAYS = (1.0, 2.0, 4.0)

_LOBBY_BYPASS_LABELS = {
    "everyone": "everyone",
    "organization": "people in my organization",
    "organizationAndFederated": "people in my organization and trusted organizations",
    "organizationExcludingGuests": "people in my organization, excluding guests",
    "invited": "people I invite",
    "organizer": "only me (the organizer)",
}
_PRESENTER_LABELS = {
    "everyone": "everyone",
    "organization": "people in my organization",
    "roleIsPresenter": "specific people (attendees with the presenter role)",
    "organizer": "only me (the organizer)",
}

# Characters that must never survive into a constructed onlineMeeting id. The
# id (``{organizerOid}_{threadId}``) is derived from a user-supplied Teams join
# URL (``get_microsoft_meeting_transcript_by_join_url`` is a public tool) and
# interpolated straight into Graph URL *paths*. After URL-decoding, ``/`` /
# ``?`` / ``#`` would inject extra path/query/fragment segments, a leftover
# ``%`` signals double-encoding, and whitespace/control chars are never valid
# in a real id — so any of them means the join URL was malformed or tampered
# with and the id must be rejected.
_UNSAFE_MEETING_ID_PART_RE = re.compile(r"[/?#%\s\x00-\x1f\x7f]")


# ── Online-meeting id resolution (delegated, works for attendees) ──
# Graph's ``$filter=JoinWebUrl eq`` only resolves meetings the signed-in
# user ORGANIZED, so it 404s (and spams logs) for meetings you merely
# attended. Instead we CONSTRUCT the onlineMeeting id directly from the
# join URL: the id format is ``{organizerOid}_{threadId}`` where the thread
# id is the ``19:meeting_…@thread.v2`` path segment and the organizer Oid is
# carried in the ``context`` query parameter. ``GET /me/onlineMeetings/{id}``
# and its ``/transcripts`` accept the *invited attendee's* delegated token,
# so this unlocks transcripts of meetings you didn't organize — no app-only
# token and no Application Access Policy required.
def _meeting_id_from_join_url(join_url: str) -> str | None:
    try:
        thread_m = re.search(r"/meetup-join/([^/?]+)", join_url)
        ctx_m = re.search(r"[?&]context=([^&]+)", join_url)
        if not thread_m or not ctx_m:
            return None
        thread = _url_decode(thread_m.group(1))
        ctx = json.loads(_url_decode(ctx_m.group(1)))
        oid = (ctx.get("Oid") or "").strip()
        if not oid or not thread:
            return None
        # The decoded organizer Oid and thread id are interpolated directly
        # into Graph URL paths, and this backs a public tool that accepts
        # arbitrary user-supplied join URLs. Reject any decoded part that
        # could break out of the path segment (see
        # ``_UNSAFE_MEETING_ID_PART_RE``) so a crafted join URL can't inject
        # path/query/fragment into the request.
        if _UNSAFE_MEETING_ID_PART_RE.search(oid) or _UNSAFE_MEETING_ID_PART_RE.search(
            thread
        ):
            return None
        return f"{oid}_{thread}"
    except Exception:
        return None


# ── Teams meeting options (recording, transcription, lobby, presenters) ──


def _requested_meeting_options(arguments: dict[str, Any]) -> tuple[dict[str, bool], str | None]:
    """The onlineMeeting properties to write, from the tool's meeting-option arguments.

    An argument left at ``None`` is not touched. A boolean spelled as a string
    (``"true"``) is read as one; anything else is refused before any write.
    """
    changes: dict[str, bool] = {}
    for argument, prop in _MEETING_OPTION_PROPERTIES.items():
        value = arguments.get(argument)
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        if not isinstance(value, bool):
            return {}, f"Error: {argument} must be true or false, got {value!r}."
        changes[prop] = value
    return changes, None


def _online_meeting_error(resp: httpx.Response | None, err: str, *, write: bool) -> str:
    """An onlineMeeting failure; a 403 names the delegated permission it needs."""
    if getattr(resp, "status_code", None) != 403:
        return err
    permission = "OnlineMeetings.ReadWrite" if write else "OnlineMeetings.Read (or OnlineMeetings.ReadWrite)"
    return (
        f"Error: Microsoft Graph refused to {'change' if write else 'read'} the Teams meeting "
        f"options (HTTP 403). They need the delegated Microsoft Graph permission {permission}: "
        "add it to the delegated permissions of the Microsoft integration's app registration, "
        "grant admin consent if the tenant requires it, then re-authorize the Microsoft "
        f"integration so its token carries the permission. Microsoft Graph said: {err}"
    )


def _online_meeting_url(meeting_id: str) -> str:
    # ``:`` and ``@`` stay literal: an id built from a join URL carries the
    # ``19:meeting_...@thread.v2`` thread id.
    return f"{_BASE_URL}/me/onlineMeetings/{_url_encode(meeting_id, safe=':@')}"


async def _find_online_meeting(
    ctx: MicrosoftContext,
    client: AutoRefreshClient,
    join_url: str,
    *,
    wait_budget: list[float] | None = None,
) -> tuple[dict | None, str | None]:
    """The onlineMeeting behind a Teams join URL.

    The id built from the join URL (:func:`_meeting_id_from_join_url`) is read
    first: ``GET /me/onlineMeetings/{id}`` answers the organizer and the
    invited attendees. A join URL it cannot be built from (the short
    ``/meet/`` form) or a miss falls back to ``$filter=JoinWebUrl eq '...'``,
    which resolves the meetings the signed-in user organized. httpx
    percent-encodes the whole ``$filter`` value once, which is the encoding
    Graph documents for the join URL; an apostrophe is doubled as OData
    requires. A miss is benign and kept out of the warning log.

    *wait_budget* holds the seconds still allowed for waiting on a meeting
    created a moment ago (it can be missing for a few seconds); a miss spends
    its next delay and looks again, and the caller shares the same budget.
    """
    built_id = _meeting_id_from_join_url(join_url)
    odata_filter = "JoinWebUrl eq '{}'".format(join_url.replace("'", "''"))
    while True:
        if built_id:
            resp, err = await ctx.request(
                client,
                "get",
                _online_meeting_url(built_id),
                _PROVIDER,
                "read the Teams meeting",
                expected_statuses={400, 403, 404},
            )
            if not err:
                return resp.json() or {}, None
        resp, err = await ctx.request(
            client,
            "get",
            f"{_BASE_URL}/me/onlineMeetings",
            _PROVIDER,
            "find the event's Teams meeting",
            params={"$filter": odata_filter},
            expected_statuses={404},
        )
        if err and getattr(resp, "status_code", None) != 404:
            return None, _online_meeting_error(resp, err, write=False)
        meetings = [] if err else (resp.json() or {}).get("value") or []
        if meetings:
            return meetings[0], None
        if not wait_budget:
            return None, (
                "Error: Microsoft Graph returned no Teams meeting for the event's join URL "
                "(GET /me/onlineMeetings/{id} and ?$filter=JoinWebUrl eq '...'), so its "
                "options cannot be read or changed."
            )
        await asyncio.sleep(wait_budget.pop(0))


async def _get_online_meeting(
    ctx: MicrosoftContext, client: AutoRefreshClient, meeting_id: str
) -> tuple[dict | None, str | None]:
    resp, err = await ctx.request(
        client,
        "get",
        _online_meeting_url(meeting_id),
        _PROVIDER,
        "read the Teams meeting options",
        expected_statuses=set(),
    )
    if err:
        return None, _online_meeting_error(resp, err, write=False)
    return resp.json() or {}, None


async def _patch_online_meeting(
    ctx: MicrosoftContext, client: AutoRefreshClient, meeting_id: str, changes: dict[str, bool]
) -> tuple[dict, str | None]:
    """PATCH the meeting; returns what Graph answered (``{}`` without a body)."""
    resp, err = await ctx.request(
        client,
        "patch",
        _online_meeting_url(meeting_id),
        _PROVIDER,
        "change the Teams meeting options",
        json=changes,
        expected_statuses=set(),
    )
    if err:
        return {}, _online_meeting_error(resp, err, write=True)
    try:
        return resp.json() or {}, None
    except ValueError:
        return {}, None


def _format_meeting_options(meeting: dict) -> list[str]:
    """The Teams meeting options an agent checks, with the raw Graph values."""

    def switch(prop: str) -> str:
        value = meeting.get(prop)
        label = "not reported" if value is None else ("on" if value else "off")
        return f"{label} ({prop}={json.dumps(value)})"

    lobby = meeting.get("lobbyBypassSettings") or {}
    scope = lobby.get("scope")
    presenters = meeting.get("allowedPresenters")
    lobby_line = (
        f"Who can bypass the lobby: {_LOBBY_BYPASS_LABELS.get(scope, scope or 'not reported')} "
        f"(lobbyBypassSettings.scope={json.dumps(scope)})"
    )
    if "isDialInBypassEnabled" in lobby:
        lobby_line += f"; dial-in callers bypass it: {'yes' if lobby['isDialInBypassEnabled'] else 'no'}"
    lines = [
        f"Record and transcribe automatically: {switch('recordAutomatically')}",
        f"Allow transcription: {switch('allowTranscription')}",
        f"Allow recording: {switch('allowRecording')}",
        lobby_line,
        f"Who can present: {_PRESENTER_LABELS.get(presenters, presenters or 'not reported')} "
        f"(allowedPresenters={json.dumps(presenters)})",
    ]
    if meeting.get("meetingOptionsWebUrl"):
        lines.append(f"Meeting options page: {meeting['meetingOptionsWebUrl']}")
    return lines
