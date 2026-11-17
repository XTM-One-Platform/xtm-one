"""A Teams voice clip as Graph serves it: a hosted content its message body does not show (#9569).

The live failure after #9548: a clip recorded in a 1:1 chat with an agent
account reached the turn as ``[no text content]``, and the chat read tool
showed no attachment and no pasted image. The fixture is the shape of that
chatMessage: an empty HTML body, no attachment, the audio only under the
message's ``hostedContents``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.integrations.providers.microsoft import _teams
from app.llm import transcription
from app.llm.transcription import Transcript
from app.services.agent_accounts import pipeline, voice_notes
from app.services.agent_accounts.events import AccountAttachment
from app.services.agent_accounts.providers import ProviderError
from app.services.agent_accounts.providers import microsoft_teams as teams

GRAPH = teams.GRAPH
CHAT = "19:a_b@unq.gbl.spaces"
ROUTE = {"kind": "chat", "chat_id": CHAT}

#: The chatMessage of a voice clip, as Graph answers it.
VOICE_CLIP_MESSAGE = {
    "id": "1794039652326",
    "messageType": "message",
    "createdDateTime": "2026-11-07T08:20:52.326Z",
    "from": {"user": {"id": "user-1", "displayName": "Sender"}},
    "body": {"contentType": "html", "content": ""},
    "attachments": [],
    "mentions": [],
}
#: Its ``hostedContents`` collection.
VOICE_CLIP_HOSTED = [{"id": "aWQ9eF8wLXdldS1kNC1hYmMxMjM=", "contentType": "audio/mp4"}]


def _message(**changes):
    return {**VOICE_CLIP_MESSAGE, **changes}


# -- what the message is made of ------------------------------------------------


def test_the_shape_of_an_empty_message_is_logged_without_its_content():
    shape = teams.message_shape(
        _message(
            body={"content": '<p>secret words</p><attachment id="x"></attachment>'},
            attachments=[{"contentType": "audio/mp4"}],
        )
    )
    assert shape == "messageType=message body_tags=attachment,p attachments=audio/mp4"
    assert "secret" not in shape
    assert teams.message_shape(VOICE_CLIP_MESSAGE) == "messageType=message body_tags=- attachments=-"


def test_a_hosted_content_the_body_does_not_show_is_an_attachment():
    (clip,) = teams.unreferenced_hosted_contents(VOICE_CLIP_MESSAGE, ROUTE, VOICE_CLIP_HOSTED, ())
    assert clip.content_type == "audio/mp4"
    assert clip.name.startswith("teams-voice-note-")
    assert clip.url.startswith(f"{GRAPH}/chats/") and clip.url.endswith("/$value")
    assert "/messages/1794039652326/hostedContents/" in clip.url
    assert voice_notes.is_audio(clip.content_type, clip.name)


def test_an_undeclared_hosted_content_is_left_to_the_download_and_a_known_one_not_repeated():
    (other,) = teams.unreferenced_hosted_contents(VOICE_CLIP_MESSAGE, ROUTE, [{"id": "h2"}, {}], ())
    assert other.content_type == "application/octet-stream" and other.name.startswith("teams-content-")
    assert teams.unreferenced_hosted_contents(VOICE_CLIP_MESSAGE, ROUTE, [{"id": "h2"}], (other,)) == ()
    # An unsafe id in the route: nothing addressed.
    assert (
        teams.unreferenced_hosted_contents(VOICE_CLIP_MESSAGE, {"kind": "chat", "chat_id": "a/b"}, [{"id": "h"}], ())
        == ()
    )


# -- the provider: one more read for an empty message only --------------------------


def _provider(hosted=None, fail: Exception | None = None):
    provider = teams.MicrosoftTeamsProvider()
    calls: list[str] = []

    async def collect(session, url, params, limit, *, headers=None):
        calls.append(url)
        if fail:
            raise fail
        return list(hosted or [])

    async def addresses(session, user_id):
        return ("sender@example.com",)

    provider._collect = collect  # type: ignore[method-assign]
    provider._addresses_of = addresses  # type: ignore[method-assign]
    return provider, calls


async def _event(provider, msg):
    return await provider._event(
        SimpleNamespace(account_external_id="account-1"),
        msg,
        conversation_type="direct",
        conversation_id=CHAT,
        conversation_name=None,
        thread_id=CHAT,
        route=ROUTE,
    )


async def test_a_voice_clip_reaches_the_event_as_an_audio_attachment():
    provider, calls = _provider(VOICE_CLIP_HOSTED)
    event = await _event(provider, VOICE_CLIP_MESSAGE)
    (clip,) = event.attachments
    assert clip.content_type == "audio/mp4"
    assert calls == [f"{GRAPH}/chats/{CHAT}/messages/1794039652326/hostedContents"]


async def test_a_message_with_text_reads_no_hosted_content():
    provider, calls = _provider(VOICE_CLIP_HOSTED)
    event = await _event(provider, _message(body={"contentType": "html", "content": "<p>Bonjour</p>"}))
    assert event.attachments == () and calls == []
    assert event.text == "Bonjour"


async def test_an_empty_message_with_nothing_to_read_is_stated_never_empty():
    for provider, _calls in (_provider([]), _provider(fail=ProviderError("Forbidden"))):
        event = await _event(provider, VOICE_CLIP_MESSAGE)
        assert event.attachments == ()
        assert event.text == teams.UNREADABLE_MESSAGE_TEXT
        assert "voice clip" in event.text


# -- the pipeline: transcribed, or stated ------------------------------------------


class _Provider:
    key = "fake"

    def __init__(self, bodies):
        self.bodies = bodies

    async def download_attachment(self, session, attachment, max_bytes):
        if attachment.url not in self.bodies:
            raise ProviderError("Graph refused the download.")
        return self.bodies[attachment.url]


class _Transcriber:
    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        return Transcript(text="Bonjour, peux-tu relire le plan ?", language="french", duration=20.4)


def _account():
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())


@pytest.fixture
def transcriber(monkeypatch):
    async def resolve(db, user_id):
        return _Transcriber()

    monkeypatch.setattr(transcription, "resolve_transcriber", resolve)


async def test_the_hosted_voice_clip_is_transcribed_into_the_turn(transcriber):
    (clip,) = teams.unreferenced_hosted_contents(VOICE_CLIP_MESSAGE, ROUTE, [{"id": "h1"}], ())
    heard: list[str] = []
    files = await pipeline._message_files(
        SimpleNamespace(),
        _Provider({clip.url: (b"\x00\x00\x00\x18ftypM4A ", "audio/mp4")}),
        SimpleNamespace(),
        _account(),
        SimpleNamespace(attachments=(clip,), text="[no text content]"),
        voice_out=heard,
    )
    assert files == []
    assert heard == ["[voice note, 0:20, transcribed] Bonjour, peux-tu relire le plan ?"]


async def test_an_unreadable_attachment_of_an_empty_message_is_stated(transcriber):
    heard: list[str] = []
    await pipeline._message_files(
        SimpleNamespace(),
        _Provider({}),
        SimpleNamespace(),
        _account(),
        SimpleNamespace(attachments=(AccountAttachment(name="teams-content-h", url="u/gone"),), text=""),
        voice_out=heard,
    )
    (line,) = heard
    assert line.startswith("[attachment (possibly a voice note) could not be read: the download failed")


async def test_an_unreadable_file_beside_text_stays_silent(transcriber):
    heard: list[str] = []
    await pipeline._message_files(
        SimpleNamespace(),
        _Provider({}),
        SimpleNamespace(),
        _account(),
        SimpleNamespace(attachments=(AccountAttachment(name="plan.pdf", url="u/gone"),), text="See the plan"),
        voice_out=heard,
    )
    assert heard == []


# -- the chat read tool shows the clip -------------------------------------------


def test_the_read_tool_lists_a_hosted_content_the_body_does_not_show():
    assert _teams._looks_empty(VOICE_CLIP_MESSAGE)
    suffix = _teams._format_unreferenced_hosted_contents(VOICE_CLIP_MESSAGE, VOICE_CLIP_HOSTED, CHAT, "1794039652326")
    assert "audio/mp4" in suffix
    assert f"download_microsoft_chat_message_image(chat_id={CHAT}, message_id=1794039652326" in suffix


def test_a_pasted_image_is_not_listed_twice_and_a_text_message_is_not_empty():
    url = f"https://graph.microsoft.com/v1.0/chats/{CHAT}/messages/1794039652326/hostedContents/img1/$value"
    msg = _message(body={"contentType": "html", "content": f'<img src="{url}">'})
    assert not _teams._looks_empty(msg)
    assert (
        _teams._format_unreferenced_hosted_contents(msg, [{"id": "img1", "contentType": "image/png"}], CHAT, "1") == ""
    )
    assert not _teams._looks_empty(_message(body={"contentType": "html", "content": "<p>hi</p>"}))


def test_an_audio_attachment_is_shown_by_the_listings():
    msg = _message(attachments=[{"id": "a", "contentType": "audio/mp4", "contentUrl": f"{GRAPH}/x", "name": "clip"}])
    assert "[audio attachments (voice clips): clip (audio/mp4)]" in _teams._format_message_attachments(msg)
