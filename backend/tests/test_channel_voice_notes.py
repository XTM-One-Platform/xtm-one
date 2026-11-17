"""Voice notes sent to a channel bot (the Teams bot in a chat, a channel or a thread): heard as text in the turn.

The router transcribes every audio file a message carries with the shared
``voice_notes.heard_line`` (the agent-account path uses the same one) and puts
the transcript after the message's own text, in place of the file-only
placeholder; an audio that could not be downloaded or transcribed is stated.
The Teams adapter extracts the voice clips Graph serves on its copy of a
chat or channel message and gives them the app-only token.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.channels import router, teams
from app.channels.base import FILE_ONLY_PLACEHOLDER, ChannelType, InboundFileAttachment, InboundMessage
from app.llm import transcription
from app.llm.transcription import Transcript, TranscriptionUnavailableError
from app.services import file_storage as storage_module

GRAPH = teams.GRAPH_API
CLIP_URL = f"{GRAPH}/chats/19:abc/messages/1/hostedContents/xyz/$value"


class _Transcriber:
    def __init__(self, text: str = "Bonjour, quelle heure est-il ?", fail: Exception | None = None):
        self.text, self.fail, self.calls = text, fail, []

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        self.calls.append((audio, filename, language))
        if self.fail:
            raise self.fail
        return Transcript(text=self.text, language="french", duration=20.4)


@pytest.fixture
def heard(monkeypatch):
    """The stored audio served from a fake S3, the transcriber chosen per test; who it was resolved for."""
    state = SimpleNamespace(transcriber=_Transcriber(), error=None, users=[], objects={})

    async def resolve(db, user_id):
        state.users.append(user_id)
        if state.error:
            raise state.error
        return state.transcriber

    async def download(object_name):
        return state.objects[object_name]

    monkeypatch.setattr(transcription, "resolve_transcriber", resolve)
    monkeypatch.setattr(storage_module.file_storage, "download", download)
    return state


def _ref(name: str, content_type: str, data: bytes, state) -> SimpleNamespace:
    key = f"channel/{uuid.uuid4().hex}/{name}"
    state.objects[key] = data
    return SimpleNamespace(filename=name, content_type=content_type, s3_object_name=key)


def _inbound(text: str) -> InboundMessage:
    return InboundMessage(channel=ChannelType.TEAMS, text=text)


USER = SimpleNamespace(id=uuid.uuid4())


async def test_a_voice_clip_alone_replaces_the_file_only_placeholder(heard):
    inbound = _inbound(FILE_ONLY_PLACEHOLDER)
    clip = _ref("voice.m4a", "audio/mp4", b"AUDIO", heard)

    await router._hear_voice_notes(None, inbound, [clip], [], USER)

    assert inbound.text == "[voice note, 0:20, transcribed] Bonjour, quelle heure est-il ?"
    assert heard.users == [USER.id], "transcribed for the sender"
    assert heard.transcriber.calls == [(b"AUDIO", "voice.m4a", None)], "the language is detected"


async def test_text_and_a_voice_clip_carry_both_text_first(heard):
    inbound = _inbound("See my note")
    clip = _ref("clip", "audio/ogg", b"OGG", heard)

    await router._hear_voice_notes(None, inbound, [clip], [], USER)

    assert inbound.text == "See my note\n[voice note, 0:20, transcribed] Bonjour, quelle heure est-il ?"


async def test_a_document_keeps_the_placeholder_next_to_the_voice_note(heard):
    inbound = _inbound(FILE_ONLY_PLACEHOLDER)
    clip = _ref("memo.wav", "application/octet-stream", b"WAV", heard)
    pdf = _ref("plan.pdf", "application/pdf", b"%PDF", heard)

    await router._hear_voice_notes(None, inbound, [pdf, clip], [], USER)

    assert inbound.text.startswith(FILE_ONLY_PLACEHOLDER + "\n[voice note, 0:20, transcribed]")
    assert [call[1] for call in heard.transcriber.calls] == ["memo.wav"], "only the audio is transcribed"


async def test_a_message_without_audio_is_unchanged(heard):
    inbound = _inbound("Plain text")
    pdf = _ref("plan.pdf", "application/pdf", b"%PDF", heard)

    await router._hear_voice_notes(None, inbound, [pdf], [], USER)

    assert inbound.text == "Plain text" and heard.users == []


async def test_a_transcription_failure_is_stated(heard):
    heard.error = TranscriptionUnavailableError("An OpenAI provider available to you is required.")
    inbound = _inbound(FILE_ONLY_PLACEHOLDER)

    await router._hear_voice_notes(None, inbound, [_ref("v.m4a", "audio/mp4", b"A", heard)], [], USER)

    assert inbound.text == ("[voice note: transcription failed: An OpenAI provider available to you is required.]")


async def test_a_voice_clip_that_could_not_be_downloaded_is_stated(heard):
    inbound = _inbound(FILE_ONLY_PLACEHOLDER)
    missed = InboundFileAttachment(url=CLIP_URL, filename="teams-voice-note", content_type="audio/mp4")

    await router._hear_voice_notes(None, inbound, [], [missed], USER)

    assert inbound.text == "[voice note: transcription failed: the audio could not be downloaded]"


async def test_an_anonymous_sender_s_audio_is_stated_not_transcribed(heard):
    inbound = _inbound(FILE_ONLY_PLACEHOLDER)

    await router._hear_voice_notes(None, inbound, [_ref("v.m4a", "audio/mp4", b"A", heard)], [], None)

    assert inbound.text.startswith("[voice note: transcription failed: ")
    assert heard.users == []


async def test_the_download_failure_of_an_audio_file_is_reported_by_the_resolver(monkeypatch):
    async def refused(att, max_bytes):
        return None

    monkeypatch.setattr(router, "_download_channel_file", refused)
    clip = InboundFileAttachment(url=CLIP_URL, filename="voice.m4a", content_type="audio/mp4", size=10)
    missed: list[InboundFileAttachment] = []

    refs = await router._resolve_channel_files(
        SimpleNamespace(),
        InboundMessage(channel=ChannelType.TEAMS, text="", file_attachments=[clip]),
        None,
        missed_out=missed,
    )

    assert refs == [] and missed == [clip]


# -- Teams adapter: the voice clips on the Graph copy of a message ----------


def test_a_graph_audio_attachment_is_extracted_and_gets_the_graph_token():
    msg = {
        "attachments": [
            {"id": "a1", "contentType": "audio/mp4; codecs=aac", "contentUrl": CLIP_URL, "name": ""},
            {"id": "a2", "contentType": "audio/mp4", "contentUrl": "https://evil.example/clip"},
            {
                "id": "r1",
                "contentType": "reference",
                "contentUrl": "https://contoso.sharepoint.com/f.pdf",
                "name": "f.pdf",
            },
        ]
    }

    found = teams._extract_graph_message_attachments(msg)

    assert [(a.filename, a.content_type, a.url) for a in found[:1]] == [("teams-voice-note", "audio/mp4", CLIP_URL)]
    assert len(found) == 2 and found[1].url.startswith(f"{GRAPH}/shares/"), "a non-Graph audio address is dropped"
    assert teams._apply_graph_attachment_auth_headers(found, "tok") == 2
    assert found[0].auth_headers == {"Authorization": "Bearer tok"}


def test_the_graph_token_is_never_given_to_another_host_or_a_non_audio_graph_url():
    other = InboundFileAttachment(url="https://evil.example/v1.0/x", content_type="audio/mp4")
    not_audio = InboundFileAttachment(url=f"{GRAPH}/me/photo/$value", content_type="image/png")

    assert teams._apply_graph_attachment_auth_headers([other, not_audio], "tok") == 0
    assert other.auth_headers == {} and not_audio.auth_headers == {}
