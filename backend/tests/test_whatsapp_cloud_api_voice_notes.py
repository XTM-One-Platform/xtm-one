"""Voice notes received on the Meta Cloud API backend of the ``whatsapp`` integration (#9547).

A received ``audio`` message is transcribed for the integration's owner before
it is stored: ``read_whatsapp_messages`` and the WhatsApp agent accounts read
``[voice note, 0:20, transcribed] ...``; a failed download or transcription is
stated, never an empty message.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import whatsapp_inbound

OWNER = uuid.uuid4()


def _target() -> whatsapp_inbound.InboundIntegration:
    return whatsapp_inbound.InboundIntegration(uuid.uuid4(), "s", "v", 30)


def _voice_message(media_id: str = "4411", caption: str | None = None) -> dict:
    audio: dict = {"mime_type": "audio/ogg; codecs=opus", "id": media_id, "voice": True}
    if caption:
        audio["caption"] = caption
    return {"id": "wamid.V1", "from": "33600000000", "type": "audio", "timestamp": "1800000000", "audio": audio}


class _Transcriber:
    def __init__(self, text: str = "Bonjour, combien font deux plus deux", duration: float | None = 20.0) -> None:
        self.text = text
        self.duration = duration
        self.calls: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", **_: object) -> SimpleNamespace:
        self.calls.append((audio, filename))
        return SimpleNamespace(text=self.text, duration=self.duration)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def hearing(monkeypatch: pytest.MonkeyPatch):
    """The integration's credentials, the media download and the transcriber, all doubled."""
    import app.integrations.providers.whatsapp as provider
    import app.llm.transcription as transcription

    state = SimpleNamespace(
        token="tok", owner=OWNER, audio=b"OggS-voice", download_err=None, transcriber=_Transcriber()
    )

    async def cloud_api_hearing(_integration_id):
        return state.token, state.owner

    async def download_media_bytes(token, media_id, max_bytes):
        state.download_args = (token, media_id, max_bytes)
        return (b"", state.download_err) if state.download_err else (state.audio, None)

    async def resolve_transcriber(_db, user_id):
        state.transcribed_for = user_id
        return state.transcriber

    monkeypatch.setattr(whatsapp_inbound, "_cloud_api_hearing", cloud_api_hearing)
    monkeypatch.setattr(whatsapp_inbound, "async_session", _Session)
    monkeypatch.setattr(provider, "download_media_bytes", download_media_bytes)
    monkeypatch.setattr(transcription, "resolve_transcriber", resolve_transcriber)
    return state


def test_message_row_keeps_the_audio_media_id():
    row = whatsapp_inbound.message_row(_voice_message(), [])
    assert row is not None
    assert row["message_type"] == "audio"
    assert row["media_id"] == "4411"


@pytest.mark.asyncio
async def test_a_french_voice_note_is_stored_transcribed_with_its_duration(hearing):
    rows = [whatsapp_inbound.message_row(_voice_message(), [])]

    done = await whatsapp_inbound.transcribe_audio_rows(_target(), rows)

    assert done == 1
    line, audio = rows[0]["text"].split("\n")
    assert line == "[voice note, 0:20, transcribed] Bonjour, combien font deux plus deux"
    assert "media id 4411" in audio and "download_whatsapp_media" in audio
    assert hearing.download_args[:2] == ("tok", "4411")
    assert hearing.transcribed_for == OWNER
    assert hearing.transcriber.calls == [(b"OggS-voice", "voice.ogg")]


@pytest.mark.asyncio
async def test_a_caption_comes_first_then_the_note(hearing):
    rows = [whatsapp_inbound.message_row(_voice_message(caption="listen"), [])]

    await whatsapp_inbound.transcribe_audio_rows(_target(), rows)

    assert rows[0]["text"].startswith("listen\n[voice note, 0:20, transcribed] ")


@pytest.mark.asyncio
async def test_a_failed_download_is_stated(hearing):
    hearing.download_err = "WhatsApp: download media failed (HTTP 404)"
    rows = [whatsapp_inbound.message_row(_voice_message(), [])]

    done = await whatsapp_inbound.transcribe_audio_rows(_target(), rows)

    assert done == 0
    assert rows[0]["text"].startswith("[voice note: transcription failed: download failed (")
    assert hearing.transcriber.calls == []


@pytest.mark.asyncio
async def test_no_transcription_provider_is_stated(hearing, monkeypatch: pytest.MonkeyPatch):
    import app.llm.transcription as transcription

    async def unavailable(_db, _user_id):
        raise transcription.TranscriptionUnavailableError("no speech-to-text provider configured")

    monkeypatch.setattr(transcription, "resolve_transcriber", unavailable)
    rows = [whatsapp_inbound.message_row(_voice_message(), [])]

    await whatsapp_inbound.transcribe_audio_rows(_target(), rows)

    assert rows[0]["text"].split("\n")[0] == "[voice note: transcription failed: no speech-to-text provider configured]"


@pytest.mark.asyncio
async def test_no_owner_is_stated_and_nothing_downloaded(hearing):
    hearing.owner = None
    rows = [whatsapp_inbound.message_row(_voice_message(), [])]

    await whatsapp_inbound.transcribe_audio_rows(_target(), rows)

    assert "no owner to transcribe for" in rows[0]["text"]
    assert not hasattr(hearing, "download_args")


@pytest.mark.asyncio
async def test_text_and_image_rows_are_left_alone(hearing):
    text = {"id": "wamid.T", "from": "33600000000", "type": "text", "timestamp": "1800000000", "text": {"body": "hi"}}
    image = {"id": "wamid.I", "from": "33600000000", "type": "image", "timestamp": "1800000000", "image": {"id": "77"}}
    rows = [whatsapp_inbound.message_row(text, []), whatsapp_inbound.message_row(image, [])]

    assert await whatsapp_inbound.transcribe_audio_rows(_target(), rows) == 0
    assert [r["text"] for r in rows] == ["hi", "[image 77]"]


@pytest.mark.asyncio
async def test_record_change_stores_the_transcript(hearing, monkeypatch: pytest.MonkeyPatch):
    stored: list[list[dict]] = []

    async def record_rows(_target, rows):
        stored.append(rows)
        return len(rows)

    monkeypatch.setattr(whatsapp_inbound, "record_rows", record_rows)

    written = await whatsapp_inbound.record_change(_target(), {"messages": [_voice_message()], "contacts": []})

    assert written == 1
    assert stored[0][0]["text"].startswith("[voice note, 0:20, transcribed] Bonjour")


@pytest.mark.asyncio
async def test_download_media_bytes_refuses_a_non_meta_url(monkeypatch: pytest.MonkeyPatch):
    import app.integrations.providers.whatsapp as provider

    calls: list[str] = []

    async def safe_request(_client, _method, url, *_args, **_kwargs):
        calls.append(url)
        return SimpleNamespace(json=lambda: {"url": "https://evil.example/file", "file_size": 10}), None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(provider, "safe_request", safe_request)
    monkeypatch.setattr(provider, "egress_client", lambda **_: _Client())

    data, err = await provider.download_media_bytes("tok", "4411", 1024)

    assert data == b""
    assert err == "no Meta media URL for this media"
    assert len(calls) == 1  # the token never went to the foreign host


@pytest.mark.asyncio
async def test_download_media_bytes_rejects_a_non_numeric_id():
    import app.integrations.providers.whatsapp as provider

    assert await provider.download_media_bytes("tok", "../x", 1024) == (b"", "not a media id")
