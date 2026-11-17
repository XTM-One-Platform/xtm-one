"""Teams transcript tools: meeting transcripts, and the transcripts embedded in
meeting recordings (the user's OneDrive, a team channel's SharePoint folder)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from app.core.egress import egress_client
from app.integrations.http import AutoRefreshClient
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _BASE_URL,
    _PROVIDER,
    MicrosoftContext,
    _parse_graph_timestamp,
)
from ._drive import _is_folder, _resolve_share_url
from ._meetings import (
    _meeting_id_from_join_url,
    _online_meeting_url,
)

# Characters of a Graph error message quoted in an answer.
_GRAPH_MESSAGE_CHARS = 300

# Teams saves a meeting recording as a video in the OneDrive of the person who
# started it, created when the recording starts or once it is uploaded. A video
# of the Recordings folder created in this window around a recording is a
# candidate for it.
_RECORDING_FILE_EARLY = timedelta(minutes=5)
_RECORDING_FILE_LATE = timedelta(hours=2)
_RECORDINGS_PAGE_SIZE = 200
_RECORDINGS_FOLDER_PAGES = 5
_RECORDING_ITEM_SELECT = "id,name,file,video,createdDateTime,lastModifiedDateTime,webUrl,parentReference"
# Recordings of one meeting tried, newest first: a recurring series has one
# online meeting, so its recordings are those of every occurrence.
_MAX_RECORDINGS_TRIED = 3
# Other candidate videos named next to the one read.
_MAX_OTHER_CANDIDATES = 3

_RECORDING_ELSEWHERE = (
    "A recording lands in the OneDrive of the person who started it (the organizer, for an "
    "automatic recording): with its OneDrive or SharePoint URL, "
    "get_microsoft_recording_transcript(url=...) reads its transcript."
)


def _display_time(value: Any) -> str:
    text = str(value or "?")
    return text.replace("T", " ").split(".")[0] if "T" in text else text


def _graph_failure(resp: Any) -> str:
    """``HTTP 404, NotFound, "message"``: the status, error code and message of a failed response.

    Graph and the SharePoint ``_api/v2.1`` endpoints answer ``{"error": {"code",
    "message", "innerError": {"code"}}}``; the inner code names the cause when
    the outer one is generic (``Forbidden (GraphAccessToTranscriptsDisabled)``).
    """
    status = getattr(resp, "status_code", None)
    parts = [f"HTTP {status}" if status else "no HTTP response"]
    error: Any = None
    if resp is not None:
        try:
            body = resp.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        inner = error.get("innerError") or error.get("innererror")
        inner_code = str(inner.get("code") or "").strip() if isinstance(inner, dict) else ""
        if inner_code and inner_code != code:
            code = f"{code} ({inner_code})" if code else inner_code
        message = error.get("message")
        if isinstance(message, dict):
            message = message.get("value")
        message = " ".join(str(message or "").split())[:_GRAPH_MESSAGE_CHARS]
        parts += [part for part in (code, f'"{message}"' if message else "") if part]
    return ", ".join(parts)


def _transcript_refusal(resp: Any) -> str:
    """A 403 on a meeting's transcript list, which is never a missing transcript."""
    return (
        f"Microsoft Graph refused to list the meeting's transcripts ({_graph_failure(resp)}). "
        "This is a permission refusal, not a missing transcript: the token lacks consent for the "
        "delegated permission OnlineMeetingTranscript.Read.All (the integration requests it with "
        "CallTranscripts.Read.All), the account may not read this meeting's transcripts, or the "
        "tenant turned off Graph access to transcripts; Graph's message says which. For consent, "
        "an administrator grants it on the integration's app registration in Microsoft Entra ID, "
        "then the integration is re-authorized."
    )


def _recording_hint(meeting_id: str) -> str:
    return (
        f'If the meeting was recorded, get_microsoft_recording_transcript(meeting_id="{meeting_id}") '
        "reads the transcript embedded in its recording."
    )


@dataclass(frozen=True)
class _TranscriptListing:
    """What Graph answered for a meeting's transcript list."""

    transcripts: list[dict] = field(default_factory=list)
    #: Why the list is empty, with Graph's status, code and message.
    reason: str = ""
    #: A 403: a permission refusal, counted apart from a missing transcript.
    refused: bool = False
    #: Any other failure (401, 5xx, network), returned as is.
    error: str | None = None


def _is_video(item: Any) -> bool:
    if not isinstance(item, dict) or not item.get("file"):
        return False
    mime = str((item.get("file") or {}).get("mimeType") or "")
    return mime.startswith("video/") or item.get("video") is not None


def _recording_file(recording: dict, videos: list[dict]) -> tuple[dict | None, list[dict]]:
    """The video Teams saved for *recording*, and the other candidates.

    Graph links a recording to no drive item. A candidate is a video created
    from ``_RECORDING_FILE_EARLY`` before the recording starts to
    ``_RECORDING_FILE_LATE`` after it ends. The one read is the candidate last
    modified closest to the recording's end - Teams finishes the file, and its
    transcript, when the recording ends, whenever it created it - then the one
    created closest to its start.
    """
    start = _parse_graph_timestamp(recording.get("createdDateTime"))
    if start is None:
        return None, []
    end = max(_parse_graph_timestamp(recording.get("endDateTime")) or start, start)
    scored: list[tuple[timedelta, timedelta, dict]] = []
    for item in videos:
        created = _parse_graph_timestamp(item.get("createdDateTime"))
        if created is None or not start - _RECORDING_FILE_EARLY <= created <= end + _RECORDING_FILE_LATE:
            continue
        modified = _parse_graph_timestamp(item.get("lastModifiedDateTime")) or created
        scored.append((abs(modified - end), abs(created - start), item))
    scored.sort(key=lambda entry: entry[:2])
    if not scored:
        return None, []
    return scored[0][2], [entry[2] for entry in scored[1:]]

# ---------------------------------------------------------------------------
# WEBVTT transcript parsing
# ---------------------------------------------------------------------------


def _parse_vtt(vtt_text: str) -> str:
    """Parse WEBVTT transcript text into ``Speaker: text`` lines.

    WEBVTT ``NOTE`` / ``STYLE`` / ``REGION`` blocks span multiple lines until
    the next blank line (e.g. CSS rules under ``STYLE``), so the whole block —
    not just its header line — must be skipped or its body would pollute the
    returned transcript.
    """
    parsed_lines: list[str] = []
    in_metadata_block = False
    for raw in vtt_text.strip().split("\n"):
        line = raw.strip()
        if not line:
            # A blank line terminates any NOTE / STYLE / REGION block.
            in_metadata_block = False
            continue
        if in_metadata_block:
            continue
        if line == "WEBVTT" or "-->" in line:
            continue
        if (
            line.startswith("NOTE")
            or line.startswith("STYLE")
            or line.startswith("REGION")
        ):
            # Enter block-skip mode: ignore the header and every following body
            # line until the next blank line.
            in_metadata_block = True
            continue
        match = re.match(r"<v\s+([^>]+)>(.+?)(?:</v>)?$", line)
        if match:
            speaker = match.group(1).strip()
            text = match.group(2).strip()
            parsed_lines.append(f"{speaker}: {text}")
        else:
            parsed_lines.append(line)
    return "\n".join(parsed_lines)


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the transcripts tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request
    headers = ctx.headers
    sp_token_fn = ctx.sp_token_fn
    _collect_all = ctx.collect_all

    async def _list_transcripts(client: AutoRefreshClient, meeting_id: str) -> _TranscriptListing:
        """List transcripts for a meeting via the delegated /me endpoint.

        Works for the organizer and invited attendees. A 404 is Graph exposing
        no transcript and a 403 a permission refusal: both come back
        with Graph's status, code and message, so a missing consent never
        reads as a missing transcript, and neither is logged as a warning.
        Any other failure (401/5xx/network) is ``error``, so callers surface
        a real problem instead of a misleading "no transcripts found".
        """
        resp, err = await _throttled_request(
            client,
            "get",
            f"{_BASE_URL}/me/onlineMeetings/{meeting_id}/transcripts",
            _PROVIDER,
            "list meeting transcripts",
            expected_statuses={403, 404},
        )
        if err:
            status = getattr(resp, "status_code", None)
            if status == 404:
                return _TranscriptListing(reason=f"no transcript exposed ({_graph_failure(resp)})")
            if status == 403:
                return _TranscriptListing(reason=_transcript_refusal(resp), refused=True)
            return _TranscriptListing(error=err)
        transcripts = resp.json().get("value", [])
        if not transcripts:
            return _TranscriptListing(reason="no transcript listed (HTTP 200, empty list)")
        return _TranscriptListing(transcripts)

    # ── Meeting / Call Transcript Tools ───────────────────────────────

    async def list_microsoft_recent_transcripts(days: int = 7, count: int = 10) -> str:
        """List recent meeting transcripts from the user's calendar events.

        The onlineMeeting id is constructed directly from each event's join URL
        (``{organizerOid}_{threadId}``), so this works for meetings you
        ORGANIZED *and* meetings you were INVITED to — the delegated transcript
        API accepts an invited attendee's token. Returns meeting subject,
        meeting ID, and transcript ID; pass those to get_microsoft_transcript_content
        to read the text. Meetings whose id can't be derived from the join URL
        (some channel meetings) are reported separately — use
        get_microsoft_channel_meeting_transcript for those. Meetings Graph
        refuses (403) are counted apart from meetings with no transcript.
        """
        now = datetime.now(UTC)
        start = now - timedelta(days=days)
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/calendarView",
                _PROVIDER,
                "list recent online meetings",
                params={
                    "startDateTime": start_iso,
                    "endDateTime": end_iso,
                    "$top": "50",
                    "$select": "subject,start,end,organizer,isOnlineMeeting,onlineMeeting",
                },
            )
            if err:
                return err

            events = resp.json().get("value", [])

            online_events = [
                e
                for e in events
                if e.get("isOnlineMeeting")
                and e.get("onlineMeeting", {}).get("joinUrl")
            ]
            online_events.reverse()

            if not online_events:
                return f"No online meetings found in the last {days} day(s)."

            results: list[str] = []
            unresolved = 0
            failed = 0
            refused = 0
            not_listed = 0
            first_refusal = ""
            for event in online_events:
                if len(results) >= count:
                    break

                join_url = event["onlineMeeting"]["joinUrl"]
                subject = event.get("subject", "Untitled")
                start_dt = event.get("start", {}).get("dateTime", "?")
                if "T" in str(start_dt):
                    start_dt = start_dt.replace("T", " ").split(".")[0]

                meeting_id = _meeting_id_from_join_url(join_url)
                if not meeting_id:
                    unresolved += 1
                    continue

                listing = await _list_transcripts(client, meeting_id)
                if listing.error:
                    # A real API failure (401/5xx/network) for this meeting —
                    # keep scanning the rest but surface the count afterwards.
                    failed += 1
                    continue
                if listing.refused:
                    refused += 1
                    first_refusal = first_refusal or listing.reason
                    continue
                if not listing.transcripts:
                    not_listed += 1
                for t in listing.transcripts:
                    tid = t.get("id", "?")
                    created = t.get("createdDateTime", "?")
                    if "T" in str(created):
                        created = created.replace("T", " ").split(".")[0]
                    results.append(
                        f"Meeting: {subject}\n"
                        f"  Date: {start_dt}\n"
                        f"  Meeting ID: {meeting_id}\n"
                        f"  Transcript ID: {tid}\n"
                        f"  Transcript created: {created}"
                    )

        refusal_note = (
            f"{refused} meeting(s) were refused by Microsoft Graph (HTTP 403), counted apart "
            f"from meetings with no transcript. {first_refusal}"
        )
        not_listed_note = (
            f"{not_listed} meeting(s) have no transcript Microsoft Graph lists (transcription "
            "may not have been enabled). For a recorded one, "
            "get_microsoft_recording_transcript(meeting_id=...) reads the transcript embedded "
            "in its recording; list_microsoft_online_meetings gives the meeting IDs."
        )
        if not results:
            msg = f"No transcripts found for online meetings in the last {days} day(s)."
            if refused:
                msg += f" {refusal_note}"
            if not_listed:
                msg += f" {not_listed_note}"
            if unresolved:
                msg += (
                    f" {unresolved} meeting(s) had a join URL with no derivable "
                    "meeting id (often channel meetings) — use "
                    "get_microsoft_channel_meeting_transcript for those."
                )
            if failed:
                msg += (
                    f" {failed} meeting(s) could not be checked due to a "
                    "Microsoft API error (see server logs for details)."
                )
            return msg

        footer = ""
        if refused:
            footer += f"\n\n({refusal_note})"
        if not_listed:
            footer += f"\n\n({not_listed_note})"
        if unresolved:
            footer += (
                f"\n\n({unresolved} meeting(s) could not be resolved from their join "
                "URL — likely channel meetings; use "
                "get_microsoft_channel_meeting_transcript.)"
            )
        if failed:
            footer += (
                f"\n\n({failed} meeting(s) could not be checked due to a Microsoft "
                "API error — see server logs.)"
            )
        return (
            f"Found {len(results)} transcript(s):\n\n"
            + "\n---\n".join(results)
            + footer
        )

    async def list_microsoft_meeting_transcripts(meeting_id: str) -> str:
        """List transcripts available for a specific online meeting.

        Works for meetings you organized or were invited to (the delegated
        transcript API accepts an attendee token). Use
        list_microsoft_recent_transcripts to find meeting IDs.
        """
        async with _client() as client:
            listing = await _list_transcripts(client, meeting_id)
        if listing.error:
            return listing.error
        if listing.refused:
            return f"Error: {listing.reason} {_recording_hint(meeting_id)}"
        transcripts = listing.transcripts
        if not transcripts:
            return f"No transcripts found for this meeting: {listing.reason}. {_recording_hint(meeting_id)}"
        lines = []
        for t in transcripts:
            tid = t.get("id", "?")
            created = t.get("createdDateTime", "?")
            if "T" in str(created):
                created = created.replace("T", " ").split(".")[0]
            lines.append(f"Transcript ID: {tid}\n  Created: {created}")
        return f"Found {len(transcripts)} transcript(s):\n\n" + "\n---\n".join(lines)

    async def get_microsoft_transcript_content(
        meeting_id: str, transcript_id: str
    ) -> str:
        """Get the full text content of a meeting transcript.

        Works for meetings you organized or were invited to. Use
        list_microsoft_recent_transcripts or list_microsoft_meeting_transcripts
        first to find the meeting_id and transcript_id.
        """
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content",
                _PROVIDER,
                "get transcript content",
                headers={**headers, "Accept": "text/vtt"},
            )
            if err:
                return err
            parsed = _parse_vtt(resp.text)
            if not parsed:
                return "Transcript is empty or could not be parsed."
            return parsed

    async def get_microsoft_meeting_transcript_by_join_url(join_url: str) -> str:
        """Get a meeting transcript directly from its Teams join URL.

        Derives the onlineMeeting id from the join URL and returns the most
        recent transcript's text. Works for meetings you organized or were
        invited to (delegated). Handy when you have a join URL from a calendar
        event, chat, or channel post but not the meeting/transcript IDs. When
        Graph lists no transcript, the one embedded in the meeting's recording
        in the user's OneDrive is read instead.
        """
        meeting_id = _meeting_id_from_join_url(join_url)
        if not meeting_id:
            return (
                "Could not derive a meeting id from that join URL. For channel "
                "meetings, use get_microsoft_channel_meeting_transcript instead."
            )
        async with _client() as client:
            listing = await _list_transcripts(client, meeting_id)
            if listing.error:
                return listing.error
            if not listing.transcripts:
                recorded, why = await _meeting_recording_transcript(client, meeting_id)
                if recorded:
                    return (
                        "Transcript embedded in the meeting's recording, read because the "
                        f"Microsoft Graph transcript list gave none: {listing.reason}\n{recorded}"
                    )
                if listing.refused:
                    return f"Error: {listing.reason}\nThe meeting's recording gave no transcript either: {why}"
                return (
                    f"No transcript is available for that meeting: {listing.reason}. "
                    f"Its recording gave none either: {why}"
                )
            transcripts = listing.transcripts
            transcripts.sort(
                key=lambda t: t.get("createdDateTime", ""), reverse=True
            )
            transcript_id = transcripts[0].get("id")
            if not transcript_id:
                return (
                    "The most recent transcript record is missing an id, so its "
                    "content can't be retrieved. Try "
                    "list_microsoft_meeting_transcripts to inspect the available "
                    "transcripts for this meeting."
                )
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content",
                _PROVIDER,
                "get transcript content",
                headers={**headers, "Accept": "text/vtt"},
            )
            if err:
                return err
            parsed = _parse_vtt(resp.text)
            if not parsed:
                return "Transcript is empty or could not be parsed."
            return parsed

    # ── Channel Meeting Recordings (SharePoint) ───────────────────────

    def _match_entity(items: list[dict], query: str) -> str | None:
        """Match a team/channel by exact id, exact name, then name substring."""
        q = (query or "").strip().lower()
        if not q:
            return None
        for it in items:
            if (it.get("id") or "").lower() == q:
                return it.get("id")
        for it in items:
            if (it.get("displayName") or "").lower() == q:
                return it.get("id")
        for it in items:
            if q in (it.get("displayName") or "").lower():
                return it.get("id")
        return None

    async def _resolve_channel_recording_items(
        client: AutoRefreshClient, team: str, channel: str
    ) -> tuple[list[dict], str | None, str | None]:
        """Resolve a channel's recording driveItems.

        Returns ``(items, drive_id, error)``. ``items`` are the children of the
        channel's SharePoint ``Recordings`` folder (or the channel root if no
        such folder exists). ``error`` is a user-facing message on failure.
        """
        teams, err = await _collect_all(
            client,
            f"{_BASE_URL}/me/joinedTeams",
            "list teams",
            {"$select": "id,displayName"},
        )
        if err:
            return [], None, err
        team_id = _match_entity(teams, team)
        if not team_id:
            return [], None, (
                f"Could not find a team matching '{team}'. Use "
                "list_microsoft_teams_and_channels to see available teams."
            )

        channels, err = await _collect_all(
            client,
            f"{_BASE_URL}/teams/{team_id}/channels",
            "list channels",
            {"$select": "id,displayName"},
        )
        if err:
            return [], None, err
        channel_id = _match_entity(channels, channel)
        if not channel_id:
            return [], None, (
                f"Could not find a channel matching '{channel}' in that team."
            )

        ff_resp, err = await _throttled_request(
            client,
            "get",
            f"{_BASE_URL}/teams/{team_id}/channels/{channel_id}/filesFolder",
            _PROVIDER,
            "get channel files folder",
        )
        if err:
            return [], None, err
        ff = ff_resp.json()
        drive_id = (ff.get("parentReference") or {}).get("driveId")
        folder_id = ff.get("id")
        if not drive_id or not folder_id:
            return [], None, "Could not resolve the channel's SharePoint folder."

        children, err = await _collect_all(
            client,
            f"{_BASE_URL}/drives/{drive_id}/items/{folder_id}/children",
            "list channel files",
            {"$top": "200"},
        )
        if err:
            return [], drive_id, err
        rec_folder = next(
            (
                c
                for c in children
                if c.get("folder") and (c.get("name") or "").lower() == "recordings"
            ),
            None,
        )
        if rec_folder:
            recordings, err = await _collect_all(
                client,
                f"{_BASE_URL}/drives/{drive_id}/items/{rec_folder['id']}/children",
                "list recordings",
                {"$top": "200"},
            )
            if err:
                return [], drive_id, err
            return recordings, drive_id, None
        return children, drive_id, None

    async def _fetch_sharepoint_transcript(
        host: str, drive_id: str, item_id: str
    ) -> tuple[str | None, str | None]:
        """Extract the transcript embedded in a SharePoint/Stream recording.

        Teams stores the meeting transcript embedded in the recording rather
        than as a standalone ``.vtt`` in SharePoint, so we use the SharePoint
        Stream media endpoint
        (``/_api/v2.1/drives/{driveId}/items/{itemId}/media/transcripts``) to
        get a download URL for the transcript text. Returns ``(text, error)``.
        Needs a token for *host* from ``sp_token_fn``: a team site
        (``{tenant}.sharepoint.com``) or a OneDrive (``{tenant}-my.sharepoint.com``).
        """
        if not sp_token_fn:
            return None, (
                "SharePoint transcript extraction isn't available (no SharePoint "
                "token provider configured)."
            )
        sp_token = await sp_token_fn(host)
        if not sp_token:
            return None, (
                f"Could not obtain a SharePoint access token for {host}. Ensure the Microsoft "
                "app registration has an Office 365 SharePoint Online delegated "
                "permission (e.g. AllSites.Read) consented, then reconnect the "
                "integration."
            )
        media_url = (
            f"https://{host}/_api/v2.1/drives/{drive_id}/items/{item_id}"
            "/media/transcripts"
        )
        try:
            # The SharePoint Stream media endpoint and the transcript's
            # temporaryDownloadUrl both commonly 302/307 to a CDN host, so the
            # client MUST follow redirects or the lookup/download fails on the
            # bare redirect status.
            async with egress_client(timeout=30, follow_redirects=True) as sp:
                resp = await sp.get(
                    media_url,
                    headers={
                        "Authorization": f"Bearer {sp_token}",
                        "Accept": "application/json",
                    },
                )
                if resp.status_code != 200:
                    return None, (
                        f"SharePoint transcript lookup returned {_graph_failure(resp)} "
                        "(the recording may have no transcript)."
                    )
                entries = resp.json().get("value") or []
                if not entries:
                    return None, "No embedded transcript found for this recording."
                entry = entries[0]
                dl = (
                    entry.get("temporaryDownloadUrl")
                    or entry.get("@content.downloadUrl")
                    or entry.get("contentUrl")
                )
                if not dl:
                    return None, "Transcript entry had no download URL."
                # temporaryDownloadUrl is pre-authenticated — download without auth.
                tr = await sp.get(dl)
                if tr.status_code != 200:
                    return None, (
                        f"Transcript download returned HTTP {tr.status_code}."
                    )
                return tr.text, None
        except Exception as e:
            return None, f"SharePoint transcript fetch failed: {e}"

    async def _read_recording_transcript(
        item: dict, default_drive_id: str | None = None
    ) -> tuple[str | None, str]:
        """``(text, "")``: the parsed transcript embedded in the recording
        driveItem *item*, read on the host of its web URL; ``(None, why)``
        when there is none. Nothing is downloaded but the transcript."""
        host = urlparse(item.get("webUrl") or "").netloc
        drive_id = (item.get("parentReference") or {}).get("driveId") or default_drive_id
        item_id = item.get("id")
        if not host or not drive_id or not item_id:
            return None, "could not resolve SharePoint location."
        text, fetch_err = await _fetch_sharepoint_transcript(host, drive_id, item_id)
        if fetch_err or not text:
            return None, fetch_err or "no transcript"
        parsed = _parse_vtt(text)
        if not parsed:
            return None, "transcript empty or unparseable."
        return parsed, ""

    async def _recordings_folder_videos(client: AutoRefreshClient) -> tuple[list[dict], str | None]:
        """The videos of the user's OneDrive Recordings folder.

        Its name is localised; the ``recordings`` special folder addresses it
        whatever its name. At most ``_RECORDINGS_FOLDER_PAGES`` pages are read.
        """
        url: str | None = f"{_BASE_URL}/me/drive/special/recordings/children"
        params: dict | None = {"$top": str(_RECORDINGS_PAGE_SIZE), "$select": _RECORDING_ITEM_SELECT}
        videos: list[dict] = []
        for _ in range(_RECORDINGS_FOLDER_PAGES):
            if not url:
                break
            resp, err = await _throttled_request(
                client,
                "get",
                url,
                _PROVIDER,
                "list the OneDrive Recordings folder",
                expected_statuses={403, 404},
                **({"params": params} if params else {}),
            )
            if err:
                if getattr(resp, "status_code", None) in (403, 404):
                    return [], (
                        "your OneDrive has no Recordings folder this account can read "
                        f"({_graph_failure(resp)}). {_RECORDING_ELSEWHERE}"
                    )
                return [], err
            data = resp.json() or {}
            videos += [item for item in data.get("value") or [] if _is_video(item)]
            url = data.get("@odata.nextLink")
            params = None
        return videos, None

    async def _meeting_recording_transcript(
        client: AutoRefreshClient, meeting_id: str
    ) -> tuple[str | None, str]:
        """The transcript embedded in a meeting's recording in the user's OneDrive.

        Graph lists the meeting's recordings but not the drive item Teams saved
        each one as, so each recording, newest first, is matched by time to a
        video of the OneDrive Recordings folder (:func:`_recording_file`) and
        its transcript read on the OneDrive host. Returns ``(answer, "")`` or
        ``(None, why there is none)``.
        """
        resp, err = await _throttled_request(
            client,
            "get",
            f"{_online_meeting_url(meeting_id)}/recordings",
            _PROVIDER,
            "list meeting recordings",
            expected_statuses={403, 404},
        )
        if err:
            status = getattr(resp, "status_code", None)
            if status == 403:
                return None, (
                    f"Microsoft Graph refused to list the meeting's recordings ({_graph_failure(resp)}); "
                    "they need the delegated permission OnlineMeetingRecording.Read.All. " + _RECORDING_ELSEWHERE
                )
            if status == 404:
                return None, f"Microsoft Graph lists no recording of the meeting ({_graph_failure(resp)})"
            return None, err
        recordings = [rec for rec in (resp.json() or {}).get("value") or [] if isinstance(rec, dict)]
        if not recordings:
            return None, "Microsoft Graph lists no recording of the meeting"
        videos, err = await _recordings_folder_videos(client)
        if err:
            return None, err
        recordings.sort(key=lambda rec: rec.get("createdDateTime") or "", reverse=True)
        misses: list[str] = []
        matched = False
        for recording in recordings[:_MAX_RECORDINGS_TRIED]:
            started = _display_time(recording.get("createdDateTime"))
            item, others = _recording_file(recording, videos)
            if item is None:
                misses.append(f"no video of your OneDrive Recordings folder matches the recording of {started}")
                continue
            matched = True
            name = item.get("name") or "recording"
            parsed, why = await _read_recording_transcript(item)
            if parsed is None:
                misses.append(f"{name}: {why}")
                continue
            lines = [f"Recording: {name} (recorded {started})"]
            if item.get("webUrl"):
                lines.append(f"Web URL: {item['webUrl']}")
            if others:
                lines.append(
                    "Other videos of your OneDrive Recordings folder from that time (read one with "
                    "get_microsoft_recording_transcript(url=...)): "
                    + "; ".join(
                        f"{other.get('name') or '(unnamed)'} ({other.get('webUrl') or 'no URL'})"
                        for other in others[:_MAX_OTHER_CANDIDATES]
                    )
                )
            return "\n".join(lines) + "\n\n" + parsed, ""
        why = "; ".join(misses)
        return None, why if matched else f"{why}. {_RECORDING_ELSEWHERE}"

    async def get_microsoft_channel_meeting_recordings(
        team: str, channel: str, count: int = 10
    ) -> str:
        """List meeting recordings in a Teams channel's SharePoint folder.

        Channel meeting recordings live in the channel's SharePoint document
        library under a ``Recordings`` folder, with access following your
        channel membership. ``team`` and ``channel`` accept a display name or an
        ID. To read the *transcript text* of a channel meeting (the transcript
        is embedded in the recording, not stored as a separate file), use
        get_microsoft_channel_meeting_transcript.
        """
        async with _client() as client:
            items, _drive_id, err = await _resolve_channel_recording_items(
                client, team, channel
            )
            if err:
                return err

        media = [
            c
            for c in items
            if c.get("file")
            and (c.get("name") or "").lower().endswith((".mp4", ".vtt"))
        ]
        if not media:
            return (
                f"No recordings found in the '{channel}' channel's SharePoint "
                "'Recordings' folder. The meeting may not have been recorded, or "
                "recording hasn't finished processing."
            )
        media.sort(key=lambda c: c.get("lastModifiedDateTime", ""), reverse=True)

        lines: list[str] = []
        for c in media[:count]:
            name = c.get("name", "?")
            modified = c.get("lastModifiedDateTime", "?")
            if "T" in str(modified):
                modified = modified.replace("T", " ").split(".")[0]
            size = c.get("size", 0)
            url = c.get("@microsoft.graph.downloadUrl") or c.get("webUrl", "")
            entry = f"{name}\n  Modified: {modified}\n  Size: {size} bytes\n  URL: {url}"
            # The direct download link above is short-lived; the web URL is
            # what download_microsoft_file and the read tools take.
            if c.get("webUrl") and c.get("webUrl") != url:
                entry += f"\n  Web URL: {c['webUrl']}"
            lines.append(entry)
        return (
            f"Found {len(lines)} recording file(s) in '{team}/{channel}':\n\n"
            + "\n---\n".join(lines)
            + "\n\nSave one in the conversation by passing its Web URL to download_microsoft_file."
        )

    async def get_microsoft_channel_meeting_transcript(
        team: str, channel: str, count: int = 1
    ) -> str:
        """Get transcript text of recent channel meeting(s) from SharePoint.

        For Teams channel meetings the transcript is embedded in the recording
        stored in the channel's SharePoint ``Recordings`` folder. This finds the
        most recent recording(s) — using your channel membership, so it works
        for channel meetings you attended but did NOT organize — and extracts
        the transcript text via the SharePoint Stream media endpoint. ``count``
        controls how many recent recordings to extract (default 1, the latest).
        """
        async with _client() as client:
            items, drive_id, err = await _resolve_channel_recording_items(
                client, team, channel
            )
            if err:
                return err

        recordings = [
            c
            for c in items
            if c.get("file") and (c.get("name") or "").lower().endswith(".mp4")
        ]
        if not recordings:
            return (
                f"No recordings found in the '{channel}' channel's SharePoint "
                "'Recordings' folder, so there's no embedded transcript to extract."
            )
        recordings.sort(key=lambda c: c.get("lastModifiedDateTime", ""), reverse=True)

        sections: list[str] = []
        errors: list[str] = []
        for rec in recordings[: max(1, count)]:
            name = rec.get("name", "recording.mp4")
            web_url = rec.get("webUrl") or ""
            parsed, why = await _read_recording_transcript(rec, drive_id)
            if parsed is None:
                errors.append(f"{name}: {why}")
                continue
            modified = rec.get("lastModifiedDateTime", "?")
            if "T" in str(modified):
                modified = modified.replace("T", " ").split(".")[0]
            recording_link = f"Recording: {web_url}\n" if web_url else ""
            sections.append(f"=== {name} (recorded {modified}) ===\n{recording_link}{parsed}")

        if not sections:
            detail = (" " + " ".join(errors)) if errors else ""
            return (
                f"Could not extract a transcript from the '{channel}' channel "
                f"recordings.{detail}"
            )
        return "\n\n".join(sections)

    async def get_microsoft_recording_transcript(url: str = "", meeting_id: str = "") -> str:
        """Read the transcript embedded in a Teams meeting recording.

        ``url`` is the recording's OneDrive or SharePoint URL (a sharing link
        or its browser URL); ``meeting_id`` finds the meeting's recording in
        the user's OneDrive Recordings folder. Only the transcript is read,
        never the video.
        """
        url = (url or "").strip()
        meeting_id = (meeting_id or "").strip()
        if not url and not meeting_id:
            return "Error: pass url (the recording's OneDrive or SharePoint URL) or meeting_id."
        if url and meeting_id:
            return (
                "Error: pass url or meeting_id, not both: url names one recording, meeting_id "
                "looks the meeting's recording up in your OneDrive."
            )
        async with _client() as client:
            if meeting_id:
                recorded, why = await _meeting_recording_transcript(client, meeting_id)
                return recorded or f"No transcript could be read from the meeting's recording: {why}"
            _item_base, meta, err = await _resolve_share_url(ctx, client, url)
        if err or meta is None:
            return err or "Error: could not resolve a file from that URL."
        name = meta.get("name") or "the item"
        if _is_folder(meta):
            return (
                f"Error: '{name}' is a folder, not a recording: pass the recording file's URL "
                f'(list_microsoft_folder_by_url(url="{url}") lists the folder).'
            )
        parsed, why = await _read_recording_transcript(meta)
        if parsed is None:
            return f"No transcript could be read from '{name}': {why}"
        lines = [f"Recording: {name}"]
        if meta.get("webUrl"):
            lines.append(f"Web URL: {meta['webUrl']}")
        return "\n".join(lines) + "\n\n" + parsed

    executor.register("list_microsoft_recent_transcripts", list_microsoft_recent_transcripts)
    executor.register("list_microsoft_meeting_transcripts", list_microsoft_meeting_transcripts)
    executor.register("get_microsoft_transcript_content", get_microsoft_transcript_content)
    executor.register("get_microsoft_meeting_transcript_by_join_url", get_microsoft_meeting_transcript_by_join_url)
    executor.register("get_microsoft_channel_meeting_recordings", get_microsoft_channel_meeting_recordings)
    executor.register("get_microsoft_channel_meeting_transcript", get_microsoft_channel_meeting_transcript)
    executor.register("get_microsoft_recording_transcript", get_microsoft_recording_transcript)

    return [
        # ── Meeting / Call Transcripts ──
        ToolDefinition(
            name="list_microsoft_recent_transcripts",
            description="List recent meeting transcripts from the user's calendar. The meeting ID is derived from each event's join URL, so this works for meetings you ORGANIZED and meetings you were INVITED to (the transcript API accepts an attendee's token). Returns meeting subject, meeting ID, and transcript ID; pass meeting_id + transcript_id to get_microsoft_transcript_content to read the text. Meetings Microsoft Graph refuses (HTTP 403, a permission refusal) are counted apart from meetings with no transcript, with Graph's message; for a recorded meeting Graph lists no transcript for, get_microsoft_recording_transcript reads the transcript embedded in its recording. Meetings whose ID can't be derived (some channel meetings) are reported separately — use get_microsoft_channel_meeting_transcript for those.",
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Look back N days (default 7)",
                        "default": 7,
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max transcripts to return (default 10)",
                        "default": 10,
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="list_microsoft_meeting_transcripts",
            description="List transcripts available for a specific online meeting. Works for meetings you organized or were invited to. When Microsoft Graph lists none, the answer gives Graph's reason: no transcript exposed (HTTP 404), or a permission refusal (HTTP 403) naming the permission to consent; get_microsoft_recording_transcript then reads the transcript embedded in the meeting's recording. Use list_microsoft_recent_transcripts first to find meeting IDs.",
            input_schema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The online meeting ID (from list_microsoft_recent_transcripts or calendar event)",
                    },
                },
                "required": ["meeting_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_transcript_content",
            description="Get the full text content of a meeting transcript. Returns the transcript as readable text with speaker names. Works for meetings you organized or were invited to. Use list_microsoft_recent_transcripts or list_microsoft_meeting_transcripts first to find the meeting_id and transcript_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The online meeting ID",
                    },
                    "transcript_id": {
                        "type": "string",
                        "description": "The transcript ID (from list_microsoft_meeting_transcripts or list_microsoft_recent_transcripts)",
                    },
                },
                "required": ["meeting_id", "transcript_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_meeting_transcript_by_join_url",
            description="Get a meeting transcript directly from its Teams join URL. Derives the meeting ID from the URL and returns the latest transcript's text. Works for meetings you organized or were invited to. When Microsoft Graph lists no transcript, it reads the transcript embedded in the meeting's recording in your OneDrive Recordings folder (where an automatic recording of a meeting you organized lands) and says Graph's reason. Use when you have a join URL (from a calendar event, chat, or channel post) but not the meeting/transcript IDs.",
            input_schema={
                "type": "object",
                "properties": {
                    "join_url": {
                        "type": "string",
                        "description": "The Teams meeting join URL (https://teams.microsoft.com/l/meetup-join/...)",
                    },
                },
                "required": ["join_url"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_channel_meeting_recordings",
            description="List meeting recordings stored in a Teams channel's SharePoint 'Recordings' folder. Works for channel meetings you attended but did not organize (access follows your channel membership). Returns file names, sizes, modified dates, and download/web URLs. To read the transcript TEXT of a channel meeting, use get_microsoft_channel_meeting_transcript. Use list_microsoft_teams_and_channels to find team and channel names.",
            input_schema={
                "type": "object",
                "properties": {
                    "team": {
                        "type": "string",
                        "description": "Team display name or ID",
                    },
                    "channel": {
                        "type": "string",
                        "description": "Channel display name or ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max files to return (default 10)",
                        "default": 10,
                    },
                },
                "required": ["team", "channel"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_channel_meeting_transcript",
            description="Get the transcript TEXT of recent Teams CHANNEL meeting(s). The transcript is embedded in the recording stored in the channel's SharePoint 'Recordings' folder; this finds the latest recording(s) (using your channel membership, so it works for channel meetings you attended but did NOT organize) and extracts the transcript text. Use this for channel meetings when list_microsoft_recent_transcripts couldn't resolve them. Use list_microsoft_teams_and_channels to find team and channel names.",
            input_schema={
                "type": "object",
                "properties": {
                    "team": {
                        "type": "string",
                        "description": "Team display name or ID",
                    },
                    "channel": {
                        "type": "string",
                        "description": "Channel display name or ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "How many recent recordings to extract transcripts from (default 1, the latest)",
                        "default": 1,
                    },
                },
                "required": ["team", "channel"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_recording_transcript",
            description="Get the transcript TEXT embedded in a Teams meeting recording, with speaker names, without downloading the video. Pass url, the recording's OneDrive or SharePoint URL (a sharing link or its browser URL; an automatic recording lands in the Recordings folder of the OneDrive of the person who started it), or meeting_id to find the meeting's recording in your OneDrive Recordings folder. Use it when Microsoft Graph lists no transcript for a meeting that was recorded and transcribed. It reads the transcript through SharePoint, which needs the delegated 'Office 365 SharePoint Online' permission (e.g. AllSites.Read) on the app registration.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The recording's OneDrive or SharePoint URL (a sharing link or the browser URL of the video). Leave empty when passing meeting_id.",
                        "default": "",
                    },
                    "meeting_id": {
                        "type": "string",
                        "description": "The online meeting ID (from list_microsoft_online_meetings); its newest recording is looked up in your OneDrive Recordings folder. Leave empty when passing url.",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
    ]
