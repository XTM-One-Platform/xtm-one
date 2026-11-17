"""Regression tests for Teams transcript error handling.

Covers the review follow-ups on the delegated-transcript path:

- ``_list_transcripts`` (via the public tools): a 404 is "no transcript
  exposed" and a 403 a permission refusal, each with Graph's reason
  (``test_microsoft_recording_transcripts.py``), while a real failure
  (401/5xx/network) is surfaced to the caller instead of being silently
  reported as "No transcripts found".
- ``get_microsoft_meeting_transcript_by_join_url`` guards against a newest
  transcript object that is missing an ``id`` (so it never builds a
  ``…/transcripts/None/content`` URL).
- ``_meeting_id_from_join_url`` sanitises the decoded organizer Oid and thread
  id before interpolating them into a Graph path, so a crafted join URL can't
  inject ``/`` / ``?`` / ``#`` (path/query/fragment) into the request.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

# A join URL whose thread segment + ``context.Oid`` let
# ``_meeting_id_from_join_url`` construct a meeting id without any HTTP call.
_JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/"
    "19%3ameeting_abc123%40thread.v2/0"
    "?context=%7B%22Oid%22%3A%22organizer-oid%22%7D"
)


class _Resp:
    """Minimal response stub exposing ``status_code`` / ``json`` / ``text``."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


class _TranscriptRouter:
    """Stand-in for ``safe_request`` that routes by URL shape.

    ``list_resp`` / ``list_err`` are returned for the ``…/transcripts`` listing
    call; ``content_resp`` / ``content_err`` for the ``…/content`` download.
    """

    def __init__(
        self,
        *,
        list_resp: _Resp | None = None,
        list_err: str | None = None,
        content_resp: _Resp | None = None,
        content_err: str | None = None,
    ) -> None:
        self.list_resp = list_resp
        self.list_err = list_err
        self.content_resp = content_resp
        self.content_err = content_err
        self.urls: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.urls.append(url)
        self.calls.append((url, kwargs))
        if url.endswith("/content"):
            return self.content_resp, self.content_err
        return self.list_resp, self.list_err


async def _run(
    monkeypatch,
    router: _TranscriptRouter,
    tool: str,
    arguments: dict,
    *,
    token: str = "dummy",  # noqa: S107 — test fixture, not a real credential
):
    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", router)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return await executor.execute_tool(
        ToolCall(id="call-1", name=tool, arguments=arguments)
    )


# ── _list_transcripts error surfacing (via the by-join-URL tool) ─────────


@pytest.mark.asyncio
async def test_by_join_url_surfaces_real_error(monkeypatch):
    """A 5xx while listing transcripts must be surfaced, not masked as
    'no transcript available'."""
    router = _TranscriptRouter(
        list_resp=_Resp(status_code=500),
        list_err="Error: Microsoft server error (500) while trying to list meeting transcripts.",
    )
    result = await _run(
        monkeypatch,
        router,
        "get_microsoft_meeting_transcript_by_join_url",
        {"join_url": _JOIN_URL},
    )
    assert "server error (500)" in result.content
    # Must not attempt the content download after a failed listing.
    assert not any(u.endswith("/content") for u in router.urls)


@pytest.mark.asyncio
async def test_by_join_url_treats_404_as_no_transcript(monkeypatch):
    """A 404 is Graph exposing no transcript: the friendly message carries
    Graph's status, never the raw error string."""
    router = _TranscriptRouter(
        list_resp=_Resp(status_code=404),
        list_err="Error: Microsoft resource not found while trying to list meeting transcripts.",
    )
    result = await _run(
        monkeypatch,
        router,
        "get_microsoft_meeting_transcript_by_join_url",
        {"join_url": _JOIN_URL},
    )
    assert result.content.startswith("No transcript is available for that meeting: no transcript exposed (HTTP 404).")
    assert "resource not found" not in result.content


@pytest.mark.asyncio
async def test_by_join_url_guards_missing_transcript_id(monkeypatch):
    """A transcript object without an ``id`` must not build a
    ``…/transcripts/None/content`` URL — guard and return a clear message."""
    router = _TranscriptRouter(
        list_resp=_Resp(
            payload={"value": [{"createdDateTime": "2026-01-01T00:00:00Z"}]}
        ),
    )
    result = await _run(
        monkeypatch,
        router,
        "get_microsoft_meeting_transcript_by_join_url",
        {"join_url": _JOIN_URL},
    )
    assert "missing an id" in result.content
    # The buggy path would have requested the content URL with id=None.
    assert not any(u.endswith("/content") for u in router.urls)
    assert "/transcripts/None/content" not in "".join(router.urls)


@pytest.mark.asyncio
async def test_by_join_url_happy_path_returns_parsed_transcript(monkeypatch):
    """With a valid transcript id the tool downloads and parses the VTT."""
    router = _TranscriptRouter(
        list_resp=_Resp(
            payload={
                "value": [{"id": "T1", "createdDateTime": "2026-01-01T00:00:00Z"}]
            }
        ),
        content_resp=_Resp(text="WEBVTT\n\n00:00.000 --> 00:01.000\n<v Alice>Hello</v>"),
    )
    result = await _run(
        monkeypatch,
        router,
        "get_microsoft_meeting_transcript_by_join_url",
        {"join_url": _JOIN_URL},
    )
    assert not result.is_error
    assert "Alice: Hello" in result.content
    assert any(u.endswith("/content") for u in router.urls)


@pytest.mark.asyncio
async def test_transcript_content_request_preserves_auth_header(monkeypatch):
    """The transcript content fetch sets ``Accept: text/vtt`` but must NOT drop
    the Authorization header. AutoRefreshClient only ``setdefault``s headers, so
    a bare ``headers={"Accept": ...}`` would override the default auth headers
    and force an avoidable 401/403 + token refresh."""
    router = _TranscriptRouter(
        list_resp=_Resp(
            payload={
                "value": [{"id": "T1", "createdDateTime": "2026-01-01T00:00:00Z"}]
            }
        ),
        content_resp=_Resp(text="WEBVTT\n\n00:00.000 --> 00:01.000\n<v Bob>Hi</v>"),
    )
    result = await _run(
        monkeypatch,
        router,
        "get_microsoft_meeting_transcript_by_join_url",
        {"join_url": _JOIN_URL},
        token="secret-token",
    )
    assert not result.is_error
    content_calls = [(u, kw) for (u, kw) in router.calls if u.endswith("/content")]
    assert content_calls, "expected a transcript content request"
    headers = content_calls[0][1].get("headers") or {}
    assert headers.get("Accept") == "text/vtt"
    assert headers.get("Authorization") == "Bearer secret-token"


# ── _meeting_id_from_join_url path-injection sanitisation ────────────────


# Join URLs whose decoded thread id / organizer Oid contain characters that
# would break out of the Graph URL path segment. ``_meeting_id_from_join_url``
# must reject all of these so the by-join-URL tool never issues a request with
# an injected path/query/fragment.
# NB: each entry uses explicit ``+`` concatenation (not implicit adjacent-string
# concatenation) so a list element can never be mistaken for a missing comma
# between two URLs.
_MALICIOUS_JOIN_URLS = [
    # ``%2F`` in the thread decodes to ``/`` → extra path segment.
    "https://teams.microsoft.com/l/meetup-join/"
    + "19%3ameeting_a%2F..%2Fb%40thread.v2/0"
    + "?context=%7B%22Oid%22%3A%22organizer-oid%22%7D",
    # ``%3F`` in the Oid decodes to ``?`` → injected query string.
    "https://teams.microsoft.com/l/meetup-join/"
    + "19%3ameeting_abc123%40thread.v2/0"
    + "?context=%7B%22Oid%22%3A%22o%3F%24filter%3Dx%22%7D",
    # ``%23`` in the Oid decodes to ``#`` → injected fragment.
    "https://teams.microsoft.com/l/meetup-join/"
    + "19%3ameeting_abc123%40thread.v2/0"
    + "?context=%7B%22Oid%22%3A%22o%23frag%22%7D",
    # Double-encoded slash (``%252F`` → ``%2F``) leaves a literal ``%``.
    "https://teams.microsoft.com/l/meetup-join/"
    + "19%3ameeting_a%252Fb%40thread.v2/0"
    + "?context=%7B%22Oid%22%3A%22organizer-oid%22%7D",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("join_url", _MALICIOUS_JOIN_URLS)
async def test_by_join_url_rejects_path_injection(monkeypatch, join_url):
    """A crafted join URL whose decoded id parts contain path-breaking chars
    must be refused before any HTTP request is made."""
    router = _TranscriptRouter()
    result = await _run(
        monkeypatch,
        router,
        "get_microsoft_meeting_transcript_by_join_url",
        {"join_url": join_url},
    )
    assert "Could not derive a meeting id" in result.content
    # The id must never be constructed, so no Graph request is attempted.
    assert router.urls == []


# ── list_microsoft_meeting_transcripts error surfacing ───────────────────


@pytest.mark.asyncio
async def test_list_meeting_transcripts_surfaces_real_error(monkeypatch):
    router = _TranscriptRouter(
        list_resp=_Resp(status_code=401),
        list_err="Error: Microsoft authentication failed — the token may be expired.",
    )
    result = await _run(
        monkeypatch,
        router,
        "list_microsoft_meeting_transcripts",
        {"meeting_id": "organizer-oid_19:meeting_abc123@thread.v2"},
    )
    assert "authentication failed" in result.content


@pytest.mark.asyncio
async def test_list_meeting_transcripts_404_reports_none_found(monkeypatch):
    router = _TranscriptRouter(
        list_resp=_Resp(status_code=404),
        list_err="Error: Microsoft resource not found while trying to list meeting transcripts.",
    )
    result = await _run(
        monkeypatch,
        router,
        "list_microsoft_meeting_transcripts",
        {"meeting_id": "organizer-oid_19:meeting_abc123@thread.v2"},
    )
    assert result.content.startswith("No transcripts found for this meeting: no transcript exposed (HTTP 404).")
    assert "resource not found" not in result.content
