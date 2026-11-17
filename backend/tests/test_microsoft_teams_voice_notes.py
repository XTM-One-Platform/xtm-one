"""Voice notes in the Teams messages the Microsoft integration tools read (#9571).

``get_microsoft_chat_message`` / ``get_microsoft_channel_message`` hear each
voice clip of the message (downloaded from Graph with the integration's token,
transcribed for the person the run acts for); the listings name the clips and
the call that hears them.
"""

from __future__ import annotations

import base64
import contextlib
import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from app.integrations.providers.microsoft import _voice_notes
from app.integrations.providers.microsoft._common import _BASE_URL
from app.integrations.providers.microsoft._teams import _chat_message_block, _format_chat_listing
from app.llm import provider_access, transcription
from app.llm.transcription import Transcript, TranscriptionUnavailableError

CLIP_URL = f"{_BASE_URL}/chats/19:abc/messages/1/hostedContents/xyz/$value"
PERSON = str(uuid.uuid4())


def _clip(url: str = CLIP_URL, name: str = "voice.m4a", kind: str = "audio/mp4") -> dict:
    return {"id": "a1", "contentType": kind, "contentUrl": url, "name": name}


def _msg(*attachments: dict, text: str = "") -> dict:
    return {
        "id": "1",
        "createdDateTime": "2026-11-07T08:00:00Z",
        "from": {"user": {"displayName": "Sam", "id": "u1"}},
        "body": {"contentType": "text", "content": text},
        "attachments": list(attachments),
    }


class _Transcriber:
    def __init__(self, fail: Exception | None = None):
        self.fail, self.calls = fail, []

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        self.calls.append((audio, filename))
        if self.fail:
            raise self.fail
        return Transcript(text="Bonjour, quelle heure est-il ?", language="french", duration=20.4)


class _Ctx:
    """A MicrosoftContext double: records each Graph request, answers the audio or an error."""

    def __init__(self, error: str | None = None):
        self.urls: list[str] = []
        self.error = error

    async def request(self, client, method, url, provider, action, **kwargs):
        self.urls.append(url)
        if self.error:
            return None, self.error
        return SimpleNamespace(content=b"AUDIO"), None


@pytest.fixture
def heard(monkeypatch):
    state = SimpleNamespace(transcriber=_Transcriber(), error=None, users=[])

    async def resolve(db, user_id):
        state.users.append(user_id)
        if state.error:
            raise state.error
        return state.transcriber

    monkeypatch.setattr(transcription, "resolve_transcriber", resolve)
    monkeypatch.setattr(_voice_notes, "_db_session", lambda: contextlib.nullcontext(None))
    return state


@pytest.fixture
def acting(monkeypatch):
    monkeypatch.setattr(provider_access, "acting_user_id", lambda: PERSON)


def test_audio_attachments_are_the_voice_clips_only():
    msg = _msg(
        _clip(),
        {"contentType": "reference", "contentUrl": "https://x.sharepoint.com/a.pdf", "name": "a.pdf"},
        {"contentType": "reference", "contentUrl": "https://x.sharepoint.com/memo.m4a", "name": "memo.m4a"},
        {"contentType": "application/vnd.microsoft.card.adaptive", "content": "{}"},
    )
    assert [att["name"] for att in _voice_notes.audio_attachments(msg)] == ["voice.m4a", "memo.m4a"]


async def test_a_voice_clip_is_heard_for_the_acting_person(heard, acting):
    ctx = _Ctx()
    lines = await _voice_notes.heard_lines(ctx, None, _msg(_clip()))
    assert lines == ["[voice note, 0:20, transcribed] Bonjour, quelle heure est-il ?"]
    assert ctx.urls == [CLIP_URL]
    assert heard.users == [PERSON]
    assert heard.transcriber.calls == [(b"AUDIO", "voice.m4a")]


async def test_an_uploaded_audio_file_is_downloaded_through_graph_shares(heard, acting):
    ctx = _Ctx()
    att = {"contentType": "reference", "contentUrl": "https://x.sharepoint.com/memo.m4a", "name": "memo.m4a"}
    await _voice_notes.heard_lines(ctx, None, _msg(att))
    assert ctx.urls[0].startswith(f"{_BASE_URL}/shares/u!") and ctx.urls[0].endswith("/driveItem/content")


async def test_a_message_without_audio_is_not_touched(heard, acting):
    ctx = _Ctx()
    assert await _voice_notes.heard_lines(ctx, None, _msg(text="hello")) == []
    assert ctx.urls == [] and heard.users == []


async def test_a_download_failure_is_stated(heard, acting):
    lines = await _voice_notes.heard_lines(_Ctx(error="403 Forbidden"), None, _msg(_clip()))
    assert lines == ["[voice note: transcription failed: the audio could not be downloaded]"]


async def test_a_transcription_failure_is_stated(heard, acting):
    heard.error = TranscriptionUnavailableError("An OpenAI provider available to you is required")
    lines = await _voice_notes.heard_lines(_Ctx(), None, _msg(_clip()))
    assert lines[0].startswith("[voice note: transcription failed: An OpenAI provider")


async def test_the_token_never_goes_to_another_host(heard, acting):
    ctx = _Ctx()
    lines = await _voice_notes.heard_lines(ctx, None, _msg(_clip(url="https://evil.example/clip.m4a")))
    assert ctx.urls == []
    assert lines == ["[voice note: transcription failed: the audio is not served by Microsoft Graph]"]


async def test_without_an_acting_person_nothing_is_transcribed(heard, monkeypatch):
    monkeypatch.setattr(provider_access, "acting_user_id", lambda: None)
    ctx = _Ctx()
    lines = await _voice_notes.heard_lines(ctx, None, _msg(_clip()))
    assert ctx.urls == [] and heard.users == []
    assert "no person" in lines[0]


async def test_at_most_max_heard_clips_are_transcribed(heard, acting):
    clips = [_clip(name=f"v{i}.m4a") for i in range(_voice_notes.MAX_HEARD + 1)]
    lines = await _voice_notes.heard_lines(_Ctx(), None, _msg(*clips))
    assert len(heard.transcriber.calls) == _voice_notes.MAX_HEARD
    assert "at most" in lines[-1]


def test_with_heard_appends_after_the_message():
    assert _voice_notes.with_heard("detail", ["[voice note, 0:20, transcribed] hi"]) == (
        "detail\n[voice note, 0:20, transcribed] hi"
    )
    assert _voice_notes.with_heard("detail", []) == "detail"


def test_a_listing_names_the_clip_and_the_call_that_hears_it():
    block = _chat_message_block(_msg(_clip()), "[no text content]", include_message_id=True)
    assert "[a voice note: read the message with get_microsoft_chat_message to hear it as text]" in block
    assert "voice note" not in _chat_message_block(_msg(text="hi"), "hi", include_message_id=False)


# A clip recorded in Teams, as Graph lists it among the message's hosted
# contents (#9622): no media type, an id that is the base64 of an asyncgw
# ``.../views/audio`` locator.
TEAMS_CLIP_ID = base64.b64encode(
    b"id=x_0-weu-d4-abc123,type=1,url=https://eu-api.asm.skype.com/v1/objects/0-weu-d4-abc123/views/audio"
).decode()
MESSAGE_URL = f"{_BASE_URL}/chats/19:abc/messages/1"


def test_a_hosted_voice_clip_is_told_apart_from_other_hosted_contents():
    image = {"id": base64.b64encode(b"id=x,url=https://h/v1/objects/x/views/imgo").decode(), "contentType": None}
    declared = {"id": "h2", "contentType": "audio/mp4"}
    clips, rest = _voice_notes.audio_hosted_contents([{"id": TEAMS_CLIP_ID}, image, declared, {}, "x"])
    assert clips == [{"id": TEAMS_CLIP_ID}, declared]
    assert rest == [image, {}, "x"]
    assert not _voice_notes.is_audio_hosted_content({"id": "not base64 !"})


async def test_a_hosted_voice_clip_is_heard_from_the_message_url(heard, acting):
    ctx = _Ctx()
    lines = await _voice_notes.heard_hosted_lines(ctx, None, MESSAGE_URL, [{"id": TEAMS_CLIP_ID}])
    assert lines == ["[voice note, 0:20, transcribed] Bonjour, quelle heure est-il ?"]
    assert ctx.urls == [f"{MESSAGE_URL}/hostedContents/{quote(TEAMS_CLIP_ID, safe='')}/$value"]
    assert heard.users == [PERSON]


async def test_hosted_clips_share_the_cap_and_the_host_guard(heard, acting):
    lines = await _voice_notes.heard_hosted_lines(
        _Ctx(), None, MESSAGE_URL, [{"id": TEAMS_CLIP_ID}], already=_voice_notes.MAX_HEARD
    )
    assert "at most" in lines[0] and heard.users == []
    ctx = _Ctx()
    lines = await _voice_notes.heard_hosted_lines(ctx, None, "https://evil.example/m/1", [{"id": TEAMS_CLIP_ID}])
    assert ctx.urls == [] and "not served by Microsoft Graph" in lines[0]


async def test_a_hosted_clip_without_an_acting_person_is_not_downloaded(heard, monkeypatch):
    monkeypatch.setattr(provider_access, "acting_user_id", lambda: None)
    ctx = _Ctx()
    lines = await _voice_notes.heard_hosted_lines(ctx, None, MESSAGE_URL, [{"id": TEAMS_CLIP_ID}])
    assert ctx.urls == [] and "no person" in lines[0]


def test_a_listing_names_the_get_tool_for_a_textless_message():
    block = _chat_message_block(_msg(), "[no text content]", include_message_id=True)
    assert "[no text: it may be a voice note - read the message with get_microsoft_chat_message" in block
    deleted = {**_msg(), "deletedDateTime": "2026-11-07T08:01:00Z"}
    assert "voice note" not in _chat_message_block(deleted, "[no text content]", include_message_id=False)


# A Teams clip whose chatMessage carries an attachments entry that shows
# nothing (no file, no card, no name): the read tool hears it (#9628 retest),
# the listing must still name the call that does.
_SILENT_ATTACHMENT = {"id": "clip-ref", "contentType": None, "contentUrl": None, "content": None, "name": None}


def test_a_listing_names_the_get_tool_when_the_textless_message_has_a_silent_attachment():
    msg = {**_msg(_SILENT_ATTACHMENT), "body": {"contentType": "html", "content": ""}}
    ((line,),) = _format_chat_listing([("19:abc", [msg])], include_message_id=True)
    assert "[no text content]" in line
    assert "[no text: it may be a voice note - read the message with get_microsoft_chat_message" in line


def test_a_listing_gives_no_voice_note_line_to_a_shared_file_or_a_card():
    shared = _msg({"id": "f", "contentType": "reference", "contentUrl": "https://x.sharepoint.com/f", "name": "f.pdf"})
    assert "may be a voice note" not in _chat_message_block(shared, "[no text content]", include_message_id=False)
