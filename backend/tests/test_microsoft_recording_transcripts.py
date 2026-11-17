"""Teams meeting transcripts the Graph transcript list does not give.

- A 403 and a 404 on ``/me/onlineMeetings/{id}/transcripts`` are reported
  apart, each with Graph's status, code and message: a 404 is "no transcript
  exposed", a 403 a permission refusal naming OnlineMeetingTranscript.Read.All,
  counted apart by ``list_microsoft_recent_transcripts``.
- When the list gives none, ``get_microsoft_meeting_transcript_by_join_url``
  reads the transcript embedded in the meeting's recording: the recording
  Graph lists is matched to a video of the user's OneDrive Recordings folder
  and its transcript read through the SharePoint Stream API, with a token for
  the OneDrive host. ``get_microsoft_recording_transcript`` does the same from
  a OneDrive / SharePoint URL (or a meeting id). The video is never downloaded.
- The team channel transcript tool reads its recordings as before.

The tenant (Contoso, Alice Martin, Bob Durand) and every id are synthetic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft import transcripts as transcripts_module
from app.integrations.providers.microsoft._drive import _encode_share_id
from app.integrations.providers.microsoft.transcripts import _graph_failure, _recording_file
from app.tools.base import CallableToolExecutor, ToolCall

_MEETING_ID = "organizer-oid_19:meeting_abc123@thread.v2"
_JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/"
    "19%3ameeting_abc123%40thread.v2/0"
    "?context=%7B%22Oid%22%3A%22organizer-oid%22%7D"
)
_ONEDRIVE_HOST = "contoso-my.sharepoint.com"
_ONEDRIVE_DRIVE = "b!onedrive-drive"
_RECORDING_NAME = "Weekly sync-20261020_090003-Meeting Recording.mp4"
_CDN_URL = "https://cdn.example.net/transcript.vtt"
_VTT = (
    "WEBVTT\n\n"
    "00:00:00.000 --> 00:00:02.000\n<v Alice Martin>Let's start.</v>\n\n"
    "00:00:02.000 --> 00:00:04.000\n<v Bob Durand>Agreed.</v>\n"
)
_SPEAKER_LINES = "Alice Martin: Let's start.\nBob Durand: Agreed."
_NOT_FOUND = "The meeting has no transcript."
_TRANSCRIPTS_DISABLED = "Graph API access to transcripts is disabled for this tenant."


class _Resp:
    """A response stub: ``status_code``, ``json()`` (raises without a JSON body), ``text``."""

    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def _graph_error(code: str, message: str, inner: str | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if inner:
        error["innerError"] = {"code": inner, "date": "2026-10-20T10:00:00"}
    return {"error": error}


class _Graph:
    """Stand-in for ``safe_request``: answers by the end of the request URL."""

    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((url, kwargs))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                if resp.status_code == 403:
                    return resp, f"Error: Microsoft access denied while trying to {action}."
                if resp.status_code == 404:
                    return resp, f"Error: Microsoft resource not found while trying to {action}."
                if resp.status_code >= 400:
                    return resp, f"Error: Microsoft returned HTTP {resp.status_code} while trying to {action}."
                return resp, None
        raise AssertionError(f"unexpected Graph request: {url}")


class _SharePoint:
    """Stand-in for ``egress_client``: the Stream media lookup, then the
    pre-authenticated transcript download."""

    def __init__(self, *, lookup: _Resp | None = None, vtt: str = _VTT) -> None:
        self.lookup = lookup or _Resp(200, {"value": [{"temporaryDownloadUrl": _CDN_URL}]})
        self.vtt = vtt
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, **_kwargs: Any) -> _SharePoint:
        return self

    async def __aenter__(self) -> _SharePoint:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def get(self, url: str, headers: dict | None = None) -> _Resp:
        self.requests.append((url, dict(headers or {})))
        if "/media/transcripts" in url:
            return self.lookup
        if url == _CDN_URL:
            return _Resp(200, text=self.vtt)
        raise AssertionError(f"unexpected SharePoint request: {url}")


class _Tokens:
    """``sp_token_fn``: one token per host, the hosts asked recorded."""

    def __init__(self, *, grant: bool = True) -> None:
        self.grant = grant
        self.hosts: list[str] = []

    async def __call__(self, host: str) -> str | None:
        self.hosts.append(host)
        return f"sp-token-{host}" if self.grant else None


async def _run(
    monkeypatch,
    graph: _Graph,
    tool: str,
    arguments: dict,
    *,
    sharepoint: _SharePoint | None = None,
    tokens: _Tokens | None = None,
):
    monkeypatch.setattr(ms._common, "safe_request", graph)
    monkeypatch.setattr(transcripts_module, "egress_client", sharepoint or _SharePoint())
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor, sp_token_fn=tokens or _Tokens())
    return await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments=arguments))


def _video(item_id: str, name: str, created: str, modified: str, *, host: str = _ONEDRIVE_HOST) -> dict:
    return {
        "id": item_id,
        "name": name,
        "file": {"mimeType": "video/mp4"},
        "createdDateTime": created,
        "lastModifiedDateTime": modified,
        "webUrl": f"https://{host}/personal/alice_contoso_com/Documents/Recordings/{quote(name)}",
        "parentReference": {"driveId": _ONEDRIVE_DRIVE},
    }


_RECORDING = {
    "id": "REC1",
    "meetingId": _MEETING_ID,
    "createdDateTime": "2026-10-20T09:00:03Z",
    "endDateTime": "2026-10-20T09:58:41Z",
    "recordingContentUrl": "https://graph.microsoft.com/v1.0/users/organizer-oid/onlineMeetings/X/recordings/REC1/content",
}
_WEEKLY = _video("ITEM-WEEKLY", _RECORDING_NAME, "2026-10-20T09:00:05Z", "2026-10-20T10:01:12Z")
_LAST_WEEK = _video(
    "ITEM-LAST-WEEK", "Weekly sync-20261013_090001-Meeting Recording.mp4", "2026-10-13T09:00:04Z", "2026-10-13T10:00:30Z"
)
_NOTES = {
    "id": "ITEM-NOTES",
    "name": "notes.docx",
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "createdDateTime": "2026-10-20T09:10:00Z",
    "webUrl": f"https://{_ONEDRIVE_HOST}/personal/alice_contoso_com/Documents/Recordings/notes.docx",
    "parentReference": {"driveId": _ONEDRIVE_DRIVE},
}
_MEDIA_URL = f"https://{_ONEDRIVE_HOST}/_api/v2.1/drives/{_ONEDRIVE_DRIVE}/items/ITEM-WEEKLY/media/transcripts"


def _fallback_routes(transcripts: _Resp | None, *, folder: list[dict] | None = None) -> dict[str, _Resp]:
    routes = {
        "/recordings": _Resp(200, {"value": [_RECORDING]}),
        "/special/recordings/children": _Resp(200, {"value": folder if folder is not None else [_LAST_WEEK, _NOTES, _WEEKLY]}),
    }
    if transcripts is not None:
        routes["/transcripts"] = transcripts
    return routes


def _assert_no_video_download(graph: _Graph, sharepoint: _SharePoint) -> None:
    assert not any(url.endswith("/content") for url in graph.urls)
    assert not any(".mp4" in url or url.endswith("/content") for url, _ in sharepoint.requests)


# ── Graph's reason: 403 and 404 apart ────────────────────────────────────


def test_graph_failure_names_status_code_inner_code_and_message():
    resp = _Resp(403, _graph_error("Forbidden", "  Graph API access\n to transcripts is disabled.  ", inner="GraphAccessToTranscriptsDisabled"))
    assert _graph_failure(resp) == (
        'HTTP 403, Forbidden (GraphAccessToTranscriptsDisabled), "Graph API access to transcripts is disabled."'
    )


def test_graph_failure_reads_the_sharepoint_rest_message_and_a_body_that_is_not_json():
    rest = _Resp(404, {"error": {"code": "itemNotFound", "message": {"lang": "en-US", "value": "Item not found"}}})
    assert _graph_failure(rest) == 'HTTP 404, itemNotFound, "Item not found"'
    assert _graph_failure(_Resp(502)) == "HTTP 502"
    long = _Resp(404, _graph_error("NotFound", "x" * 1000))
    assert len(_graph_failure(long)) < 330


@pytest.mark.asyncio
async def test_list_transcripts_403_is_a_permission_refusal_with_graph_message(monkeypatch):
    graph = _Graph({"/transcripts": _Resp(403, _graph_error("Forbidden", _TRANSCRIPTS_DISABLED, inner="GraphAccessToTranscriptsDisabled"))})
    result = await _run(monkeypatch, graph, "list_microsoft_meeting_transcripts", {"meeting_id": _MEETING_ID})
    assert result.is_error
    assert result.content.startswith(
        "Error: Microsoft Graph refused to list the meeting's transcripts "
        f'(HTTP 403, Forbidden (GraphAccessToTranscriptsDisabled), "{_TRANSCRIPTS_DISABLED}").'
    )
    assert "permission refusal, not a missing transcript" in result.content
    assert "OnlineMeetingTranscript.Read.All" in result.content
    assert "CallTranscripts.Read.All" in result.content
    assert "no transcript exposed" not in result.content
    assert f'get_microsoft_recording_transcript(meeting_id="{_MEETING_ID}")' in result.content


@pytest.mark.asyncio
async def test_list_transcripts_404_is_no_transcript_exposed_with_graph_message(monkeypatch):
    graph = _Graph({"/transcripts": _Resp(404, _graph_error("NotFound", _NOT_FOUND))})
    result = await _run(monkeypatch, graph, "list_microsoft_meeting_transcripts", {"meeting_id": _MEETING_ID})
    assert not result.is_error
    assert result.content.startswith(
        f'No transcripts found for this meeting: no transcript exposed (HTTP 404, NotFound, "{_NOT_FOUND}").'
    )
    assert "refused" not in result.content
    assert "resource not found" not in result.content
    assert f'get_microsoft_recording_transcript(meeting_id="{_MEETING_ID}")' in result.content


@pytest.mark.asyncio
async def test_list_transcripts_200_is_unchanged(monkeypatch):
    graph = _Graph({"/transcripts": _Resp(200, {"value": [{"id": "T1", "createdDateTime": "2026-10-20T10:02:00.123Z"}]})})
    result = await _run(monkeypatch, graph, "list_microsoft_meeting_transcripts", {"meeting_id": _MEETING_ID})
    assert result.content == "Found 1 transcript(s):\n\nTranscript ID: T1\n  Created: 2026-10-20 10:02:00"


def _calendar_event(thread: str, subject: str) -> dict:
    return {
        "subject": subject,
        "start": {"dateTime": "2026-10-20T09:00:00.0000000"},
        "isOnlineMeeting": True,
        "onlineMeeting": {
            "joinUrl": (
                f"https://teams.microsoft.com/l/meetup-join/19%3ameeting_{thread}%40thread.v2/0"
                "?context=%7B%22Oid%22%3A%22organizer-oid%22%7D"
            )
        },
    }


@pytest.mark.asyncio
async def test_recent_transcripts_counts_refusals_apart_from_missing_transcripts(monkeypatch):
    events = [_calendar_event("a", "Refused"), _calendar_event("b", "Not transcribed"), _calendar_event("c", "Transcribed")]
    graph = _Graph(
        {
            "/me/calendarView": _Resp(200, {"value": events}),
            "meeting_a@thread.v2/transcripts": _Resp(403, _graph_error("Forbidden", "Missing scope permissions.")),
            "meeting_b@thread.v2/transcripts": _Resp(404, _graph_error("NotFound", _NOT_FOUND)),
            "meeting_c@thread.v2/transcripts": _Resp(200, {"value": [{"id": "T-C", "createdDateTime": "2026-10-20T10:00:00Z"}]}),
        }
    )
    result = await _run(monkeypatch, graph, "list_microsoft_recent_transcripts", {})
    assert not result.is_error
    assert result.content.startswith("Found 1 transcript(s):")
    assert "Transcript ID: T-C" in result.content
    assert "1 meeting(s) were refused by Microsoft Graph (HTTP 403)" in result.content
    assert '(HTTP 403, Forbidden, "Missing scope permissions.")' in result.content
    assert "1 meeting(s) have no transcript Microsoft Graph lists" in result.content
    assert "get_microsoft_recording_transcript(meeting_id=...)" in result.content


@pytest.mark.asyncio
async def test_recent_transcripts_with_only_refusals_never_says_transcription_was_off(monkeypatch):
    events = [_calendar_event("a", "One"), _calendar_event("b", "Two")]
    graph = _Graph(
        {
            "/me/calendarView": _Resp(200, {"value": events}),
            "/transcripts": _Resp(403, _graph_error("Forbidden", "Missing scope permissions.")),
        }
    )
    result = await _run(monkeypatch, graph, "list_microsoft_recent_transcripts", {})
    assert result.content.startswith(
        "No transcripts found for online meetings in the last 7 day(s). "
        "2 meeting(s) were refused by Microsoft Graph (HTTP 403)"
    )
    assert "OnlineMeetingTranscript.Read.All" in result.content
    assert "transcription may not have been enabled" not in result.content


# ── The recording fallback of the join-URL tool ──────────────────────────


@pytest.mark.asyncio
async def test_join_url_404_reads_the_transcript_of_the_onedrive_recording(monkeypatch):
    graph = _Graph(_fallback_routes(_Resp(404, _graph_error("NotFound", _NOT_FOUND))))
    sharepoint, tokens = _SharePoint(), _Tokens()
    result = await _run(
        monkeypatch,
        graph,
        "get_microsoft_meeting_transcript_by_join_url",
        {"join_url": _JOIN_URL},
        sharepoint=sharepoint,
        tokens=tokens,
    )
    assert not result.is_error
    assert result.content.startswith(
        "Transcript embedded in the meeting's recording, read because the Microsoft Graph transcript "
        f'list gave none: no transcript exposed (HTTP 404, NotFound, "{_NOT_FOUND}")\n'
        f"Recording: {_RECORDING_NAME} (recorded 2026-10-20 09:00:03Z)\n"
        f"Web URL: {_WEEKLY['webUrl']}\n\n"
    )
    assert result.content.endswith(_SPEAKER_LINES)
    assert "Other videos" not in result.content
    # The recordings Graph lists, then the special folder (whatever its
    # localised name), never the recording's content.
    assert graph.urls[1].endswith(f"/me/onlineMeetings/{_MEETING_ID}/recordings")
    assert graph.urls[2].endswith("/me/drive/special/recordings/children")
    # A token for the OneDrive host, sent to the Stream endpoint on that host;
    # the pre-authenticated download carries none.
    assert tokens.hosts == [_ONEDRIVE_HOST]
    assert sharepoint.requests[0][0] == _MEDIA_URL
    assert sharepoint.requests[0][1]["Authorization"] == f"Bearer sp-token-{_ONEDRIVE_HOST}"
    assert sharepoint.requests[1] == (_CDN_URL, {})
    _assert_no_video_download(graph, sharepoint)


@pytest.mark.asyncio
async def test_join_url_403_still_reads_the_recording_and_says_graph_refused(monkeypatch):
    graph = _Graph(_fallback_routes(_Resp(403, _graph_error("Forbidden", "Missing scope permissions."))))
    result = await _run(monkeypatch, graph, "get_microsoft_meeting_transcript_by_join_url", {"join_url": _JOIN_URL})
    assert not result.is_error
    assert "Microsoft Graph refused to list the meeting's transcripts" in result.content
    assert "OnlineMeetingTranscript.Read.All" in result.content
    assert result.content.endswith(_SPEAKER_LINES)


@pytest.mark.asyncio
async def test_join_url_403_without_recording_is_an_error_naming_the_permission(monkeypatch):
    graph = _Graph(
        {
            "/transcripts": _Resp(403, _graph_error("Forbidden", "Missing scope permissions.")),
            "/recordings": _Resp(200, {"value": []}),
        }
    )
    tokens = _Tokens()
    result = await _run(
        monkeypatch, graph, "get_microsoft_meeting_transcript_by_join_url", {"join_url": _JOIN_URL}, tokens=tokens
    )
    assert result.is_error
    assert result.content.startswith("Error: Microsoft Graph refused to list the meeting's transcripts (HTTP 403")
    assert "OnlineMeetingTranscript.Read.All" in result.content
    assert result.content.endswith(
        "The meeting's recording gave no transcript either: Microsoft Graph lists no recording of the meeting"
    )
    assert tokens.hosts == []


@pytest.mark.asyncio
async def test_join_url_404_without_onedrive_recordings_folder_points_at_the_url_tool(monkeypatch):
    routes = _fallback_routes(_Resp(404, _graph_error("NotFound", _NOT_FOUND)))
    routes["/special/recordings/children"] = _Resp(404, _graph_error("itemNotFound", "Item not found"))
    graph, tokens = _Graph(routes), _Tokens()
    result = await _run(
        monkeypatch, graph, "get_microsoft_meeting_transcript_by_join_url", {"join_url": _JOIN_URL}, tokens=tokens
    )
    assert not result.is_error
    assert result.content.startswith("No transcript is available for that meeting: no transcript exposed (HTTP 404")
    assert (
        'your OneDrive has no Recordings folder this account can read (HTTP 404, itemNotFound, "Item not found")'
        in result.content
    )
    assert "get_microsoft_recording_transcript(url=...)" in result.content
    assert "resource not found" not in result.content
    assert tokens.hosts == []


@pytest.mark.asyncio
async def test_join_url_recording_with_no_matching_video_says_so(monkeypatch):
    graph = _Graph(_fallback_routes(_Resp(404, _graph_error("NotFound", _NOT_FOUND)), folder=[_LAST_WEEK, _NOTES]))
    tokens = _Tokens()
    result = await _run(
        monkeypatch, graph, "get_microsoft_meeting_transcript_by_join_url", {"join_url": _JOIN_URL}, tokens=tokens
    )
    assert "no video of your OneDrive Recordings folder matches the recording of 2026-10-20 09:00:03Z" in result.content
    assert "lands in the OneDrive of the person who started it" in result.content
    assert tokens.hosts == []


@pytest.mark.asyncio
async def test_join_url_names_the_other_candidate_videos(monkeypatch):
    other = _video("ITEM-OTHER", "Ad hoc call-20261020_093000.mp4", "2026-10-20T09:30:00Z", "2026-10-20T11:40:00Z")
    graph = _Graph(_fallback_routes(_Resp(404, _graph_error("NotFound", _NOT_FOUND)), folder=[other, _WEEKLY]))
    sharepoint = _SharePoint()
    result = await _run(
        monkeypatch,
        graph,
        "get_microsoft_meeting_transcript_by_join_url",
        {"join_url": _JOIN_URL},
        sharepoint=sharepoint,
    )
    assert sharepoint.requests[0][0] == _MEDIA_URL
    assert (
        "Other videos of your OneDrive Recordings folder from that time (read one with "
        f"get_microsoft_recording_transcript(url=...)): Ad hoc call-20261020_093000.mp4 ({other['webUrl']})"
    ) in result.content


@pytest.mark.asyncio
async def test_join_url_with_a_listed_transcript_never_looks_for_a_recording(monkeypatch):
    graph = _Graph(
        {
            "/transcripts": _Resp(200, {"value": [{"id": "T1", "createdDateTime": "2026-10-20T10:00:00Z"}]}),
            "/content": _Resp(200, text=_VTT),
        }
    )
    tokens = _Tokens()
    result = await _run(
        monkeypatch, graph, "get_microsoft_meeting_transcript_by_join_url", {"join_url": _JOIN_URL}, tokens=tokens
    )
    assert result.content == _SPEAKER_LINES
    assert not any("/recordings" in url for url in graph.urls)
    assert tokens.hosts == []


# ── Matching a recording to its OneDrive video ───────────────────────────

_SPAN_B = {"createdDateTime": "2026-10-20T10:00:00Z", "endDateTime": "2026-10-20T11:00:00Z"}


def test_recording_file_when_teams_creates_the_file_as_the_recording_starts():
    a = _video("A", "a.mp4", "2026-10-20T09:00:05Z", "2026-10-20T10:00:30Z")
    b = _video("B", "b.mp4", "2026-10-20T10:00:04Z", "2026-10-20T11:00:40Z")
    c = _video("C", "c.mp4", "2026-10-20T11:00:03Z", "2026-10-20T12:00:20Z")
    item, others = _recording_file(_SPAN_B, [a, b, c])
    assert item is b
    assert others == [c]


def test_recording_file_when_teams_creates_the_file_once_the_recording_ends():
    a = _video("A", "a.mp4", "2026-10-20T10:00:40Z", "2026-10-20T10:02:00Z")
    b = _video("B", "b.mp4", "2026-10-20T11:00:50Z", "2026-10-20T11:03:00Z")
    c = _video("C", "c.mp4", "2026-10-20T12:01:00Z", "2026-10-20T12:03:00Z")
    item, others = _recording_file(_SPAN_B, [a, b, c])
    assert item is b
    assert others == [a, c]


def test_recording_file_without_candidate_or_start():
    old = _video("OLD", "old.mp4", "2026-10-19T10:00:00Z", "2026-10-19T11:00:00Z")
    assert _recording_file(_SPAN_B, [old]) == (None, [])
    assert _recording_file({"endDateTime": "2026-10-20T11:00:00Z"}, [old]) == (None, [])


# ── get_microsoft_recording_transcript ───────────────────────────────────

_SHARE_URL = f"https://{_ONEDRIVE_HOST}/:v:/g/personal/alice_contoso_com/EaBcDeFgHiJkLmNoPq?e=x1y2z3"


@pytest.mark.asyncio
async def test_recording_transcript_from_a_onedrive_sharing_link(monkeypatch):
    share_id = _encode_share_id(_SHARE_URL)
    graph = _Graph({f"/shares/{share_id}/driveItem": _Resp(200, _WEEKLY)})
    sharepoint, tokens = _SharePoint(), _Tokens()
    result = await _run(
        monkeypatch,
        graph,
        "get_microsoft_recording_transcript",
        {"url": _SHARE_URL},
        sharepoint=sharepoint,
        tokens=tokens,
    )
    assert not result.is_error
    assert result.content == f"Recording: {_RECORDING_NAME}\nWeb URL: {_WEEKLY['webUrl']}\n\n{_SPEAKER_LINES}"
    assert graph.urls == [f"https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem"]
    assert tokens.hosts == [_ONEDRIVE_HOST]
    assert sharepoint.requests[0][0] == _MEDIA_URL
    _assert_no_video_download(graph, sharepoint)


@pytest.mark.asyncio
async def test_recording_transcript_from_a_meeting_id(monkeypatch):
    graph, tokens = _Graph(_fallback_routes(None)), _Tokens()
    result = await _run(
        monkeypatch, graph, "get_microsoft_recording_transcript", {"meeting_id": _MEETING_ID}, tokens=tokens
    )
    assert result.content.startswith(f"Recording: {_RECORDING_NAME} (recorded 2026-10-20 09:00:03Z)\n")
    assert result.content.endswith(_SPEAKER_LINES)
    assert tokens.hosts == [_ONEDRIVE_HOST]


@pytest.mark.asyncio
async def test_recording_transcript_refuses_a_folder(monkeypatch):
    share_id = _encode_share_id(_SHARE_URL)
    folder = {"id": "RECORDINGS", "name": "Recordings", "folder": {"childCount": 3}, "webUrl": "https://x", "parentReference": {"driveId": _ONEDRIVE_DRIVE}}
    graph = _Graph({f"/shares/{share_id}/driveItem": _Resp(200, folder)})
    sharepoint, tokens = _SharePoint(), _Tokens()
    result = await _run(
        monkeypatch, graph, "get_microsoft_recording_transcript", {"url": _SHARE_URL}, sharepoint=sharepoint, tokens=tokens
    )
    assert result.is_error
    assert result.content.startswith("Error: 'Recordings' is a folder, not a recording")
    assert sharepoint.requests == []
    assert tokens.hosts == []


@pytest.mark.parametrize(
    "arguments",
    [{}, {"url": "  ", "meeting_id": ""}, {"url": _SHARE_URL, "meeting_id": _MEETING_ID}],
    ids=["nothing", "blank", "both"],
)
@pytest.mark.asyncio
async def test_recording_transcript_takes_one_of_url_or_meeting_id(monkeypatch, arguments):
    graph = _Graph({})
    result = await _run(monkeypatch, graph, "get_microsoft_recording_transcript", arguments)
    assert result.is_error
    assert result.content.startswith("Error: pass url")
    assert graph.urls == []


@pytest.mark.asyncio
async def test_recording_transcript_reports_the_sharepoint_answer(monkeypatch):
    share_id = _encode_share_id(_SHARE_URL)
    graph = _Graph({f"/shares/{share_id}/driveItem": _Resp(200, _WEEKLY)})
    sharepoint = _SharePoint(lookup=_Resp(404, {"error": {"code": "itemNotFound", "message": "The media has no transcript."}}))
    result = await _run(monkeypatch, graph, "get_microsoft_recording_transcript", {"url": _SHARE_URL}, sharepoint=sharepoint)
    assert result.content == (
        f"No transcript could be read from '{_RECORDING_NAME}': SharePoint transcript lookup returned "
        'HTTP 404, itemNotFound, "The media has no transcript." (the recording may have no transcript).'
    )


@pytest.mark.asyncio
async def test_recording_transcript_without_a_onedrive_token_names_the_host(monkeypatch):
    share_id = _encode_share_id(_SHARE_URL)
    graph = _Graph({f"/shares/{share_id}/driveItem": _Resp(200, _WEEKLY)})
    sharepoint = _SharePoint()
    result = await _run(
        monkeypatch,
        graph,
        "get_microsoft_recording_transcript",
        {"url": _SHARE_URL},
        sharepoint=sharepoint,
        tokens=_Tokens(grant=False),
    )
    assert f"Could not obtain a SharePoint access token for {_ONEDRIVE_HOST}." in result.content
    assert "AllSites.Read" in result.content
    assert sharepoint.requests == []


# ── The team channel path is unchanged ───────────────────────────────────


@pytest.mark.asyncio
async def test_channel_meeting_transcript_reads_the_channel_recording_as_before(monkeypatch):
    team_host = "contoso.sharepoint.com"
    recording = _video(
        "ITEM-CHANNEL", "Ops review-20261020_140000-Meeting Recording.mp4", "2026-10-20T14:00:00Z", "2026-10-20T15:02:00Z", host=team_host
    )
    del recording["parentReference"]  # the folder's drive is used then
    graph = _Graph(
        {
            "/me/joinedTeams": _Resp(200, {"value": [{"id": "TEAM1", "displayName": "Security"}]}),
            "/teams/TEAM1/channels": _Resp(200, {"value": [{"id": "CHAN1", "displayName": "Ops"}]}),
            "/teams/TEAM1/channels/CHAN1/filesFolder": _Resp(200, {"id": "FOLDER1", "parentReference": {"driveId": "b!team-drive"}}),
            "/items/FOLDER1/children": _Resp(
                200, {"value": [{"id": "RECFOLDER", "name": "Recordings", "folder": {"childCount": 1}}]}
            ),
            "/items/RECFOLDER/children": _Resp(200, {"value": [recording]}),
        }
    )
    sharepoint, tokens = _SharePoint(), _Tokens()
    result = await _run(
        monkeypatch,
        graph,
        "get_microsoft_channel_meeting_transcript",
        {"team": "Security", "channel": "Ops"},
        sharepoint=sharepoint,
        tokens=tokens,
    )
    assert result.content == (
        f"=== {recording['name']} (recorded 2026-10-20 15:02:00Z) ===\n"
        f"Recording: {recording['webUrl']}\n{_SPEAKER_LINES}"
    )
    assert tokens.hosts == [team_host]
    assert sharepoint.requests[0][0] == f"https://{team_host}/_api/v2.1/drives/b!team-drive/items/ITEM-CHANNEL/media/transcripts"


# ── The OneDrive host token ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sharepoint_token_is_minted_for_the_onedrive_host(monkeypatch):
    """The Stream endpoint of a OneDrive recording lives on
    ``{tenant}-my.sharepoint.com``: the delegated refresh token is redeemed
    for that host's audience, never the team site's."""
    from app.services import integration_tools

    integ = SimpleNamespace(
        provider="microsoft", name="Contoso M365", client_id_enc="cid", refresh_token_enc="rt", extra_config_enc=None
    )

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    async def _holder(_db, _ref):
        return integ

    async def _reveal(_integration, _field):
        return "client-secret"

    posted: list[tuple[str, dict]] = []

    class _TokenEndpoint:
        def __call__(self, **_kwargs):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, data=None, headers=None, timeout=None):
            posted.append((url, dict(data or {})))
            return httpx.Response(
                200, json={"access_token": "onedrive-token", "expires_in": 3600}, request=httpx.Request("POST", url)
            )

    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    monkeypatch.setattr(integration_tools, "load_token_holder", _holder)
    monkeypatch.setattr(integration_tools, "decrypt_value", {"cid": "client-id", "rt": "refresh-token"}.__getitem__)
    monkeypatch.setattr(integration_tools, "reveal_field", _reveal)
    monkeypatch.setattr(integration_tools, "integration_of", lambda integration: integration)
    monkeypatch.setattr(integration_tools, "egress_client", _TokenEndpoint())

    token = await integration_tools._acquire_sharepoint_token("integration-1", " Contoso-My.SharePoint.com ")
    assert token == "onedrive-token"
    assert posted == [
        (
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scope": f"https://{_ONEDRIVE_HOST}/.default",
            },
        )
    ]
