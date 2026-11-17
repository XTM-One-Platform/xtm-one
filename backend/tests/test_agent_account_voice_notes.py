"""Voice notes sent to an agent account: detected, transcribed into the turn, a failure stated."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.llm import transcription
from app.llm.transcription import Transcript, TranscriptionUnavailableError
from app.services.agent_accounts import pipeline, voice_notes
from app.services.agent_accounts.events import AccountAttachment
from app.services.agent_accounts.providers import ProviderError
from app.services.agent_accounts.providers import microsoft_teams as teams

GRAPH = teams.GRAPH


# -- detection ------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "name", "expected"),
    [
        ("audio/mp4", "clip", True),
        ("audio/ogg; codecs=opus", "x", True),
        ("application/octet-stream", "note.m4a", True),
        ("application/octet-stream", "voice.WEBM", True),
        (None, "memo.wav", True),
        ("video/mp4", "clip.mp4", False),
        ("application/pdf", "plan.m4a", False),
        ("application/octet-stream", "plan.pdf", False),
        ("image/png", "teams-image-abc", False),
    ],
)
def test_an_audio_file_is_told_by_its_media_type_else_its_extension(content_type, name, expected):
    assert voice_notes.is_audio(content_type, name) is expected


# -- the turn line ----------------------------------------------------------


def test_a_transcript_is_marked_as_a_voice_note_with_its_length():
    line = voice_notes.transcribed_line("  Test voice note:\n what is two plus two ", 4.4)
    assert line == "[voice note, 0:04, transcribed] Test voice note: what is two plus two"
    assert voice_notes.transcribed_line("Bonjour", None) == "[voice note, transcribed] Bonjour"
    assert voice_notes.transcribed_line("", 3) == "[voice note, 0:03, transcribed] (no speech heard)"
    assert voice_notes.format_duration(42) == "0:42"
    assert voice_notes.format_duration(3725) == "1:02:05"


def test_a_failure_is_stated_with_its_reason():
    assert voice_notes.failed_line("quota exceeded", 42) == "[voice note, 0:42: transcription failed: quota exceeded]"
    assert voice_notes.failed_line("") == "[voice note: transcription failed: unknown error]"
    assert len(voice_notes.failed_line("x" * 1000)) < 260


def test_the_text_comes_first_and_an_empty_message_is_the_note_alone():
    notes = ["[voice note, 0:20, transcribed] hello"]
    assert voice_notes.with_voice_notes("[no text content]", notes) == notes[0]
    assert voice_notes.with_voice_notes("", notes) == notes[0]
    assert voice_notes.with_voice_notes("See below", notes) == "See below\n" + notes[0]
    assert voice_notes.with_voice_notes("Plain text", []) == "Plain text"


def test_a_verbose_transcript_carries_the_audio_length():
    result = {"text": "Bonjour", "language": "french", "duration": 20.5}
    transcript = transcription._transcript(result, "whisper-1")
    assert transcript.duration == 20.5 and transcript.language == "french"
    assert transcription._transcript({"text": "x"}, "whisper-1").duration is None


# -- Teams: the audio attachment is kept --------------------------------------


def test_a_teams_audio_attachment_on_graph_is_kept_and_a_card_is_not():
    msg = {
        "id": "m1",
        "body": {"contentType": "html", "content": ""},
        "attachments": [
            {
                "id": "a1",
                "contentType": "audio/mp4",
                "contentUrl": f"{GRAPH}/chats/c/messages/m1/hostedContents/h/$value",
            },
            {"id": "a2", "contentType": "audio/ogg", "contentUrl": "https://elsewhere.example/clip.ogg"},
            {"id": "a3", "contentType": "application/vnd.microsoft.card.adaptive", "content": "{}"},
        ],
    }
    (audio,) = teams._message_attachments(msg, {"kind": "chat", "chat_id": "c"})
    assert audio.content_type == "audio/mp4" and audio.name == "teams-voice-note"
    assert audio.url.startswith(f"{GRAPH}/")


# -- the pipeline: transcribed, not stored ------------------------------------


class _Db:
    def __init__(self):
        self.added: list = []

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _event(*attachments: AccountAttachment, text: str = "[no text content]"):
    return SimpleNamespace(attachments=attachments, text=text)


def _account():
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())


class _Provider:
    key = "fake"

    def __init__(self, bodies: dict[str, tuple[bytes, str]]):
        self.bodies = bodies

    async def download_attachment(self, session, attachment, max_bytes):
        if attachment.url not in self.bodies:
            raise ProviderError("Graph refused the download.")
        return self.bodies[attachment.url]


class _Transcriber:
    def __init__(self, text="Test voice note: what is two plus two", fail: Exception | None = None):
        self.text, self.fail, self.calls = text, fail, []

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        self.calls.append((audio, filename, language, words))
        if self.fail:
            raise self.fail
        return Transcript(text=self.text, language="english", duration=4.2)


@pytest.fixture
def no_storage(monkeypatch):
    from app.services import file_storage as storage_module

    uploaded: list[str] = []

    async def upload(*, data, filename, content_type, prefix):
        uploaded.append(filename)
        return f"{prefix}/{filename}"

    monkeypatch.setattr(storage_module.file_storage, "upload", upload)
    return uploaded


def _with_transcriber(monkeypatch, transcriber=None, error: Exception | None = None):
    async def resolve(db, user_id):
        if error:
            raise error
        return transcriber

    monkeypatch.setattr(transcription, "resolve_transcriber", resolve)


async def test_a_voice_clip_is_transcribed_into_the_turn_and_not_stored(monkeypatch, no_storage):
    transcriber = _Transcriber()
    _with_transcriber(monkeypatch, transcriber)
    provider = _Provider({"u/audio": (b"\x00\x00\x00\x18ftypM4A ", "audio/mp4")})
    heard: list[str] = []
    files = await pipeline._message_files(
        _Db(),
        provider,
        SimpleNamespace(),
        _account(),
        _event(AccountAttachment(name="teams-voice-note", url="u/audio", content_type="audio/mp4")),
        voice_out=heard,
    )
    assert files == [] and no_storage == []
    assert heard == ["[voice note, 0:04, transcribed] Test voice note: what is two plus two"]
    # Language left to the model, words asked so the length comes back.
    (_audio, filename, language, words) = transcriber.calls[0]
    assert language is None and words is True and filename.endswith((".m4a", ".mp4", ".mp4a"))


async def test_an_audio_file_and_a_document_together_are_heard_and_stored(monkeypatch, no_storage):
    _with_transcriber(monkeypatch, _Transcriber("Bonjour, peux-tu relire le plan ?"))
    provider = _Provider({"u/a": (b"OggS", "application/octet-stream"), "u/p": (b"%PDF-1.7", "application/pdf")})
    heard: list[str] = []
    files = await pipeline._message_files(
        _Db(),
        provider,
        SimpleNamespace(),
        _account(),
        _event(
            AccountAttachment(name="memo.ogg", url="u/a"),
            AccountAttachment(name="plan.pdf", url="u/p", content_type="application/pdf"),
        ),
        voice_out=heard,
    )
    assert [f.filename for f in files] == ["plan.pdf"] and no_storage == ["plan.pdf"]
    assert heard == ["[voice note, 0:04, transcribed] Bonjour, peux-tu relire le plan ?"]


async def test_a_transcription_failure_is_stated_in_the_turn(monkeypatch, no_storage):
    _with_transcriber(monkeypatch, error=TranscriptionUnavailableError("An OpenAI provider is required."))
    provider = _Provider({"u/a": (b"RIFF", "audio/wav")})
    heard: list[str] = []
    await pipeline._message_files(
        _Db(),
        provider,
        SimpleNamespace(),
        _account(),
        _event(AccountAttachment(name="clip.wav", url="u/a", content_type="audio/wav")),
        voice_out=heard,
    )
    assert heard == ["[voice note: transcription failed: An OpenAI provider is required.]"]


async def test_a_failed_download_of_a_voice_clip_is_stated_in_the_turn(monkeypatch, no_storage):
    _with_transcriber(monkeypatch, _Transcriber())
    heard: list[str] = []
    await pipeline._message_files(
        _Db(),
        _Provider({}),
        SimpleNamespace(),
        _account(),
        _event(AccountAttachment(name="teams-voice-note", url="u/gone", content_type="audio/mp4")),
        voice_out=heard,
    )
    (line,) = heard
    assert line.startswith("[voice note: transcription failed: the audio could not be downloaded")
    assert "Graph refused the download." in line


async def test_without_a_voice_sink_audio_is_stored_like_any_file(monkeypatch, no_storage):
    """A caller that does not ask for voice notes keeps the previous behaviour."""
    provider = _Provider({"u/a": (b"OggS", "audio/ogg")})
    files = await pipeline._message_files(
        _Db(),
        provider,
        SimpleNamespace(),
        _account(),
        _event(AccountAttachment(name="memo.ogg", url="u/a", content_type="audio/ogg")),
    )
    assert [f.filename for f in files] == ["memo.ogg"]
