"""Teams online meeting tools: meetings, attendance, recordings and AI insights."""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime, timedelta
from urllib.parse import quote as _url_encode

from app.integrations.file_content import (
    keep_file,
    kept_file_line,
    kilobytes,
)
from app.integrations.file_delivery import FetchError, fetch_file
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._common import (
    _BASE_URL,
    _MS_MAX_FILE_BYTES,
    _PROVIDER,
    MicrosoftContext,
)
from ._meetings import (
    _meeting_id_from_join_url,
)


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the meetings tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request
    persist_file_fn = ctx.persist_file_fn
    _api_sem = ctx.sem

    # ── Online Meeting Tools ───────────────────────────────────────────

    async def list_microsoft_online_meetings(days: int = 7, count: int = 10) -> str | ToolResult:
        """List recent online meetings with their meeting IDs, subject, date, and join URL.

        The answer carries the listed meetings as its structured value
        (``[]`` when none), so an assignment's gate check (``non_empty`` /
        ``empty`` / a path condition) decides on the data, never on the text.
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
                    "$select": "subject,start,end,organizer,isOnlineMeeting,onlineMeeting,attendees",
                },
            )
            if err:
                return err

            events = resp.json().get("value", [])

        online_events = [
            e
            for e in events
            if e.get("isOnlineMeeting") and e.get("onlineMeeting", {}).get("joinUrl")
        ]
        online_events.reverse()

        if not online_events:
            return ToolResult(
                tool_call_id="",
                content=f"No online meetings found in the last {days} day(s).",
                structured=[],
            )

        results: list[str] = []
        records: list[dict[str, str]] = []
        for event in online_events:
            if len(results) >= count:
                break

            join_url = event["onlineMeeting"]["joinUrl"]
            subject = event.get("subject", "Untitled")
            start_dt = event.get("start", {}).get("dateTime", "?")
            end_dt = event.get("end", {}).get("dateTime", "?")
            if "T" in str(start_dt):
                start_dt = start_dt.replace("T", " ").split(".")[0]
            if "T" in str(end_dt):
                end_dt = end_dt.replace("T", " ").split(".")[0]
            organizer_name = (
                (event.get("organizer") or {}).get("emailAddress") or {}
            ).get("name", "?")
            attendees = event.get("attendees") or []
            attendee_names = [
                (a.get("emailAddress") or {}).get("name", "?") for a in attendees[:10]
            ]

            # The onlineMeeting id is constructed from the join URL — works for
            # the organizer and invited attendees alike, no organizer-only filter.
            meeting_id = _meeting_id_from_join_url(join_url) or ""

            parts = [
                f"Subject: {subject}",
                f"  Meeting ID: {meeting_id or '(unavailable — channel meeting?)'}",
                f"  Start: {start_dt}",
                f"  End: {end_dt}",
                f"  Organizer: {organizer_name}",
            ]
            if attendee_names:
                parts.append(f"  Attendees: {', '.join(attendee_names)}")
            parts.append(f"  Join URL: {join_url}")
            results.append("\n".join(parts))
            records.append(
                {
                    "meeting_id": meeting_id,
                    "subject": subject,
                    "start": str(start_dt),
                    "end": str(end_dt),
                    "organizer": organizer_name,
                    "join_url": join_url,
                }
            )

        return ToolResult(
            tool_call_id="",
            content=f"Found {len(results)} online meeting(s):\n\n" + "\n---\n".join(results),
            structured=records,
        )

    async def get_microsoft_meeting_attendance(meeting_id: str) -> str:
        """Get the attendance report for an online meeting (who attended, duration)."""
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/onlineMeetings/{meeting_id}/attendanceReports",
                _PROVIDER,
                "list attendance reports",
            )
            if err:
                return err

            reports = resp.json().get("value", [])
            if not reports:
                return "No attendance reports found for this meeting."

            report_id = reports[0].get("id", "")
            total_participants = reports[0].get("totalParticipantCount", "?")
            meeting_start = reports[0].get("meetingStartDateTime", "?")
            meeting_end = reports[0].get("meetingEndDateTime", "?")
            if "T" in str(meeting_start):
                meeting_start = meeting_start.replace("T", " ").split(".")[0]
            if "T" in str(meeting_end):
                meeting_end = meeting_end.replace("T", " ").split(".")[0]

            detail_resp, detail_err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/onlineMeetings/{meeting_id}/attendanceReports/{report_id}",
                _PROVIDER,
                "get attendance report",
                params={"$expand": "attendanceRecords"},
            )
            if detail_err:
                return (
                    f"Attendance report summary:\n"
                    f"  Total participants: {total_participants}\n"
                    f"  Meeting start: {meeting_start}\n"
                    f"  Meeting end: {meeting_end}\n"
                    f"  (Could not load detailed records: {detail_err})"
                )

            records = detail_resp.json().get("attendanceRecords", [])
            lines = [
                "Attendance Report",
                f"  Total participants: {total_participants}",
                f"  Meeting: {meeting_start} – {meeting_end}",
                "",
            ]
            for r in records:
                name = (r.get("identity") or {}).get("displayName", "Unknown")
                email = (r.get("emailAddress") or "") or (r.get("identity") or {}).get(
                    "email", ""
                )
                role = r.get("role", "?")
                duration_secs = r.get("totalAttendanceInSeconds", 0)
                duration_min = round(duration_secs / 60, 1) if duration_secs else "?"
                entry = [f"  {name}"]
                if email:
                    entry.append(f"    Email: {email}")
                entry.append(f"    Role: {role}")
                entry.append(f"    Attendance: {duration_min} min")
                lines.append("\n".join(entry))

            return "\n---\n".join(lines)

    async def list_microsoft_meeting_recordings(meeting_id: str) -> str:
        """List recordings available for an online meeting with metadata."""
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/onlineMeetings/{meeting_id}/recordings",
                _PROVIDER,
                "list meeting recordings",
            )
            if err:
                return err

            recordings = resp.json().get("value", [])
            if not recordings:
                return "No recordings found for this meeting."

            lines = []
            for rec in recordings:
                rid = rec.get("id", "?")
                created = rec.get("createdDateTime", "?")
                if "T" in str(created):
                    created = created.replace("T", " ").split(".")[0]
                ended = rec.get("endDateTime") or rec.get("recordingEndDateTime") or ""
                if "T" in str(ended):
                    ended = ended.replace("T", " ").split(".")[0]
                content_url = rec.get("recordingContentUrl", "")
                parts = [
                    f"Recording ID: {rid}",
                    f"  Created: {created}",
                ]
                if ended:
                    parts.append(f"  Ended: {ended}")
                if content_url:
                    parts.append(f"  Content URL: {content_url}")
                lines.append("\n".join(parts))

            return (
                f"Found {len(recordings)} recording(s):\n\n"
                + "\n---\n".join(lines)
                + "\n\nSave one in the conversation as a file with "
                "download_microsoft_meeting_recording(meeting_id, recording_id)."
            )

    async def download_microsoft_meeting_recording(
        meeting_id: str,
        recording_id: str,
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Save an online meeting's recording in the conversation as a file.

        The video is streamed and kept up to the Microsoft download limit; a
        longer recording is not downloaded, and the answer keeps its link.
        """
        meeting = (meeting_id or "").strip()
        recording = (recording_id or "").strip()
        if not meeting or not recording:
            return (
                "Error: meeting_id and recording_id are required "
                "(list_microsoft_meeting_recordings lists them)."
            )
        recording_url = (
            f"{_BASE_URL}/me/onlineMeetings/{_url_encode(meeting, safe='')}"
            f"/recordings/{_url_encode(recording, safe='')}"
        )
        async with _client() as client:
            meta_resp, err = await _throttled_request(
                client, "get", recording_url, _PROVIDER, "read meeting recording"
            )
            if err:
                return err
            meta = meta_resp.json() or {}
            link = meta.get("recordingContentUrl") or f"{recording_url}/content"
            async with _api_sem:
                download = await fetch_file(
                    client,
                    "get",
                    f"{recording_url}/content",
                    provider=_PROVIDER,
                    action="download meeting recording",
                    max_bytes=_MS_MAX_FILE_BYTES,
                    follow_redirects=True,
                )
        limit_mb = _MS_MAX_FILE_BYTES // 1024 // 1024
        if isinstance(download, FetchError) and download.too_large:
            size = (
                f"{round(download.size / 1024 / 1024, 1)} MB"
                if download.size
                else f"over {limit_mb} MB"
            )
            return (
                f"The recording is {size}, over the {limit_mb} MB download limit, "
                "so it was not saved in the conversation.\n"
                f"Recording link (Microsoft Graph, opened with the organizer's "
                f"access): {link}\n"
                "What was said is readable with list_microsoft_meeting_transcripts "
                "and get_microsoft_transcript_content."
            )
        if isinstance(download, FetchError):
            if download.status == 403:
                return (
                    f"{download}\nGraph serves a recording's content to the "
                    "meeting organizer, and to participants only when the tenant "
                    f"allows them to download recordings. Recording link: {link}"
                )
            return f"{download}\nRecording link: {link}"
        if not download.data:
            return "Error: the recording is empty."
        mime = (download.content_type or "").split(";", 1)[0].strip() or "video/mp4"
        created = str(meta.get("createdDateTime") or "")[:10]
        name = filename.strip() or f"meeting-recording{'-' + created if created else ''}"
        if "." not in name:
            name += mimetypes.guess_extension(mime) or ".mp4"
        file_ref = await keep_file(persist_file_fn, file_context, name, download.data, mime)
        # The video stays off the tool result: the model reads no video, and a
        # file on the result is fetched again on every request of the run.
        return ToolResult(
            tool_call_id="",
            content="\n".join(
                [
                    f"Recording: {name} ({mime}, {kilobytes(len(download.data))}) of the online meeting.",
                    kept_file_line(file_ref),
                    f"Recording link: {link}",
                    "It is a video, so its content is not shown here: read what was "
                    "said with list_microsoft_meeting_transcripts and "
                    "get_microsoft_transcript_content.",
                ]
            ),
        )

    async def get_microsoft_meeting_ai_insights(meeting_id: str) -> str:
        """Get AI-generated insights (action items, notes) for an online meeting.

        Requires Microsoft 365 Copilot licensing and the meeting must have had
        transcription or recording enabled.
        """
        async with _client() as client:
            me_resp, me_err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me",
                _PROVIDER,
                "get user id",
                params={"$select": "id"},
            )
            if me_err:
                return me_err
            user_id = me_resp.json().get("id", "")
            if not user_id:
                return "Could not resolve current user ID."

            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/copilot/users/{user_id}/onlineMeetings/{meeting_id}/aiInsights",
                _PROVIDER,
                "get meeting AI insights",
            )
            if err:
                return err

            insights = resp.json().get("value", [])
            if not insights:
                return (
                    "No AI insights available for this meeting. "
                    "The meeting may not have had transcription enabled, "
                    "or Microsoft 365 Copilot licensing may be required."
                )

            lines = []
            for insight in insights:
                insight_type = insight.get("type", "unknown")
                content = insight.get("content", {})
                text = (
                    content.get("text", "")
                    if isinstance(content, dict)
                    else str(content)
                )
                created = insight.get("createdDateTime", "")
                if "T" in str(created):
                    created = created.replace("T", " ").split(".")[0]
                parts = [f"Type: {insight_type}"]
                if created:
                    parts.append(f"  Created: {created}")
                if text:
                    parts.append(f"  Content: {text}")
                lines.append("\n".join(parts))

            return f"Found {len(insights)} AI insight(s):\n\n" + "\n---\n".join(lines)

    executor.register("list_microsoft_online_meetings", list_microsoft_online_meetings)
    executor.register("get_microsoft_meeting_attendance", get_microsoft_meeting_attendance)
    executor.register("list_microsoft_meeting_recordings", list_microsoft_meeting_recordings)
    executor.register("download_microsoft_meeting_recording", download_microsoft_meeting_recording)
    executor.register("get_microsoft_meeting_ai_insights", get_microsoft_meeting_ai_insights)

    return [
        # ── Online Meetings ──
        ToolDefinition(
            name="list_microsoft_online_meetings",
            description="List recent online meetings (Teams) from the user's calendar. Returns meeting IDs, subject, date, organizer, attendees, and join URL. Use the meeting_id with get_microsoft_meeting_attendance, list_microsoft_meeting_recordings, get_microsoft_meeting_ai_insights, or list_microsoft_meeting_transcripts.",
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
                        "description": "Max meetings to return (default 10)",
                        "default": 10,
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_meeting_attendance",
            description="Get the attendance report for an online meeting: who attended, their role, and how long they were in the meeting. Use list_microsoft_online_meetings first to find the meeting_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The online meeting ID (from list_microsoft_online_meetings)",
                    }
                },
                "required": ["meeting_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="list_microsoft_meeting_recordings",
            description="List recordings available for an online meeting with metadata (creation date, end date) and the recording link. Save one as a file with download_microsoft_meeting_recording. Use list_microsoft_online_meetings first to find the meeting_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The online meeting ID (from list_microsoft_online_meetings)",
                    }
                },
                "required": ["meeting_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="download_microsoft_meeting_recording",
            # ``filename`` only names the saved copy: a rename is the same download.
            cosmetic_arguments=("filename",),
            workspace_ingest=True,
            description=(
                "Save an online meeting's recording (a video) in the conversation as a "
                "reusable file ([[FILE:id]] + file_id, forwardable to any tool that takes "
                "a file) with the recording link. Recordings over the 100 MB download "
                "limit are not downloaded: the answer then gives the recording link. "
                "Graph serves a recording to the meeting organizer (participants only "
                "when the tenant allows recording downloads). To read what was said, use "
                "the transcript tools. Use list_microsoft_meeting_recordings to find the "
                "recording_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The online meeting ID (from list_microsoft_online_meetings)",
                    },
                    "recording_id": {
                        "type": "string",
                        "description": "The recording ID (from list_microsoft_meeting_recordings)",
                    },
                    "filename": {
                        "type": "string",
                        "default": "",
                        "description": "Optional filename override",
                    },
                },
                "required": ["meeting_id", "recording_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_meeting_ai_insights",
            description="Get AI-generated insights (action items, conversation notes) for an online meeting. Requires Microsoft 365 Copilot licensing and the meeting must have had transcription or recording enabled. Use list_microsoft_online_meetings first to find the meeting_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "meeting_id": {
                        "type": "string",
                        "description": "The online meeting ID (from list_microsoft_online_meetings)",
                    }
                },
                "required": ["meeting_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
    ]
