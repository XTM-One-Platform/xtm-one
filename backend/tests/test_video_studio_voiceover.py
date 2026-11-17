"""The narration reaches the video, spoken by the model the video says, and is heard in the editor.

- A render runs in a process of its own and an AI track in the worker: neither
  has a request's caller nor a service identity, so each is made as the person
  who asked for it - without it the Filigran AI Gateway cannot sign the speech
  request and every narration failed while the render still finished, silent.
- ``voice.describe`` is the ``Voice-over:`` line of the tool results: the
  speech model and the voice that will speak, whether it is the platform's or
  the video's own, or why nothing will - known before any render.
- A voice-over preview never pins the platform's speech model into the video.
- The editor's preview speaks from the render's own voice cache: a spoken
  narration plays at its measured length, a manager's draft has its missing
  narrations spoken in the background, once per narration.
"""

import asyncio
import hashlib
import struct
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm import identity
from app.models.user import User
from app.models.video_studio import JOB_FAILED, JOB_PENDING, VideoMusic, VideoProject, VideoRender
from app.services.video_studio import jobs, lexicon_store, music, projects, render, runners, voice
from app.services.video_studio.spec import SpecProblem, VideoSpecError, parse_video_spec

PLATFORM_ID = uuid.uuid4()
OTHER_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def no_service_identity(monkeypatch):
    """A render's process and the worker have none: the job alone says whom a call is for."""
    monkeypatch.setattr(identity, "_service_identity", None)


def _maker() -> User:
    return User(id=uuid.uuid4(), email="maker@example.com", is_active=True)


# ── The jobs act for their person ─────────────────────────────────────────


def test_a_render_speaks_as_the_person_who_asked_for_it(monkeypatch):
    maker = _maker()
    row = VideoRender(id=uuid.uuid4(), project_id=uuid.uuid4(), user_id=maker.id, status=JOB_PENDING, quality="high")
    project = VideoProject(id=row.project_id, title="Demo", spec={}, revision=1)
    rows = {VideoRender: row, VideoProject: project, User: maker}
    seen: list[identity.LLMCallerIdentity] = []

    class _Db:
        async def get(self, model, _key, **_options):
            return rows.get(model)

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def effective_spec(_db, _project, _user):
        # Read before the media and the voice-over: every provider call after it is signed as the requester.
        seen.append(identity.resolve_llm_caller())
        raise VideoSpecError([SpecProblem("scenes", "stops the render here")])

    monkeypatch.setattr(render, "async_session", session)
    monkeypatch.setattr(projects, "effective_spec", effective_spec)
    assert asyncio.run(render.run_render(str(row.id))).kind == "done"
    assert seen == [identity.LLMCallerIdentity(user_id=maker.id, email="maker@example.com")]
    assert row.status == JOB_FAILED


def test_an_ai_track_is_composed_as_the_person_it_is_made_for(monkeypatch):
    maker = _maker()
    row = VideoMusic(
        id=uuid.uuid4(),
        user_id=maker.id,
        provider="elevenlabs",
        model="music_v2",
        prompt="A calm synth bed",
        params={"duration_seconds": 30},
        status=JOB_PENDING,
    )
    seen: list[identity.LLMCallerIdentity] = []
    marks: list[dict] = []

    class _Db:
        async def get(self, model, _key):
            return maker if model is User else row

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        return object(), model, SimpleNamespace(id=provider_id, provider="elevenlabs")

    async def generate(_provider, _request):
        seen.append(identity.resolve_llm_caller())
        raise music.MusicError("refused")

    async def mark(_kind, _job_id, **changes):
        marks.append(changes)

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(music, "resolve", resolve)
    monkeypatch.setattr(music, "generate", generate)
    monkeypatch.setattr(jobs, "mark", mark)
    assert asyncio.run(runners.run_music(str(row.id))).kind == "done"
    assert seen == [identity.LLMCallerIdentity(user_id=maker.id, email="maker@example.com")]
    assert marks[-1]["status"] == JOB_FAILED


# ── What will speak the narration ─────────────────────────────────────────


def _target(config_id, model: str, label: str, voice_id: str | None = None):
    return SimpleNamespace(
        model=model,
        config=SimpleNamespace(id=config_id, provider="filigran_gateway", display_name=label),
        voice=voice_id,
    )


PLATFORM = _target(PLATFORM_ID, "gpt-4o-mini-tts", "Filigran AI Gateway", "marin")
ELEVEN = _target(OTHER_ID, "eleven_multilingual_v2", "ElevenLabs")


def _spec(voiceover: dict | None = None, narration: str | None = "Every alert, in context.") -> object:
    scene = {"id": "s1", "type": "title", "title": "Hello", **({"narration": narration} if narration else {})}
    return parse_video_spec({"title": "Demo", "scenes": [scene], **({"voiceover": voiceover} if voiceover else {})})


def _describe(monkeypatch, spec, *, chosen=PLATFORM, platform=PLATFORM, unavailable: str | None = None) -> str:
    async def resolve(_db, _user_id, _voiceover):
        if unavailable:
            raise voice.VoiceUnavailableError(unavailable)
        return chosen

    async def get_speech_provider(_db, **_kwargs):
        return platform

    monkeypatch.setattr(voice, "resolve", resolve)
    monkeypatch.setattr("app.llm.router.get_speech_provider", get_speech_provider)
    return asyncio.run(voice.describe(object(), uuid.uuid4(), spec))


def test_a_video_that_names_no_speech_model_says_it_follows_the_platform(monkeypatch):
    line = _describe(monkeypatch, _spec())
    assert line == "Voice-over: gpt-4o-mini-tts on Filigran AI Gateway (the platform's speech model), voice marin."


def test_a_video_with_its_own_speech_model_says_so_and_names_the_platform_s(monkeypatch):
    spec = _spec({"provider_id": str(OTHER_ID), "model": "eleven_multilingual_v2", "voice": "Rachel"})
    line = _describe(monkeypatch, spec, chosen=ELEVEN)
    assert line.startswith("Voice-over: eleven_multilingual_v2 on ElevenLabs (named by the video; the platform's is ")
    assert "gpt-4o-mini-tts on Filigran AI Gateway" in line and line.endswith(", voice Rachel.")
    # Pinned to what the platform uses today: still the video's own choice.
    pinned = _spec({"provider_id": str(PLATFORM_ID), "model": "gpt-4o-mini-tts"})
    assert "(named by the video, the platform's speech model today)" in _describe(monkeypatch, pinned)


def test_a_narration_nothing_can_speak_says_why_before_the_render(monkeypatch):
    line = _describe(
        monkeypatch, _spec(), unavailable="No speech model is available to you. The narration is shown as captions."
    )
    assert line == "Voice-over: No speech model is available to you. The narration is shown as captions."


def test_no_voice_over_line_without_a_narration_to_speak(monkeypatch):
    assert _describe(monkeypatch, _spec(narration=None)) == ""
    assert _describe(monkeypatch, _spec({"enabled": False})) == ""


# ── A preview never pins the platform's model ─────────────────────────────


def _preview_voiceover(monkeypatch, arguments: dict) -> str:
    from app.services.builtin_tools import video_studio as tool
    from app.services.video_studio import assets, encoder
    from app.tools.base import ToolCall

    maker = _maker()

    @asynccontextmanager
    async def session():
        yield object()

    async def find_user(_db, _user_id):
        return maker

    async def resolve(_db, _user_id, wanted):
        return ELEVEN if wanted.provider_id else PLATFORM

    async def synthesize(target, *, voice=None, out_path, **_kwargs):
        Path(out_path).write_bytes(b"RIFF")
        return voice or target.voice or "", []

    async def probe(_path):
        return SimpleNamespace(duration=2.4)

    async def store_path(_path, **_kwargs):
        return SimpleNamespace(
            id=uuid.uuid4(), filename="voiceover-preview.wav", content_type="audio/wav", size=4, s3_object_name="v.wav"
        )

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(voice, "resolve", resolve)
    monkeypatch.setattr(voice, "synthesize", synthesize)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(assets, "store_path", store_path)
    _definitions, executor = tool.build_video_studio_tools(None, str(maker.id))
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="c1", name="preview_voiceover", arguments={"text": "Hello.", **arguments}))
    )
    assert not result.is_error, result.content
    return result.content


def test_a_preview_on_the_platform_s_model_leaves_the_video_following_it(monkeypatch):
    content = _preview_voiceover(monkeypatch, {})
    assert "leave its voiceover provider_id, model and voice unset" in content
    assert str(PLATFORM_ID) not in content


def test_a_preview_puts_only_what_it_named_into_the_video(monkeypatch):
    voice_only = _preview_voiceover(monkeypatch, {"voice": "cedar"})
    assert 'set its voiceover {"voice": "cedar"}' in voice_only and "provider_id and model stay unset" in voice_only
    own = _preview_voiceover(monkeypatch, {"provider_id": str(OTHER_ID), "model": "eleven_multilingual_v2"})
    assert f'"provider_id": "{OTHER_ID}"' in own and '"model": "eleven_multilingual_v2"' in own


# ── The editor hears the narration ────────────────────────────────


def _digest(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _wav_head(seconds: float, *, rate: int = 48000, channels: int = 2) -> bytes:
    """The first bytes of a WAV as ffmpeg writes ``pcm_f32le``: an IEEE float ``fmt ``, ``fact``, ``LIST``, then ``data``."""
    frames = int(seconds * rate)
    block = channels * 4
    fmt = struct.pack("<HHIIHHH", 3, channels, rate, rate * block, block, 32, 0)
    info = b"INFO" + b"ISFT" + struct.pack("<I", 14) + b"Lavf61.7.100\x00\x00"
    odd = b"abc"
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"fact"
        + struct.pack("<I", 4)
        + struct.pack("<I", frames)
        + b"LIST"
        + struct.pack("<I", len(info))
        + info
        + b"junk"
        + struct.pack("<I", len(odd))
        + odd
        + b"\x00"
        + b"data"
        + struct.pack("<I", frames * block)
    )
    return b"RIFF" + struct.pack("<I", len(body) + frames * block) + body + b"\x00" * 64


def _three_narrations(voiceover: dict | None = None) -> object:
    scenes = [
        {"id": f"s{i}", "type": "title", "title": f"Card {i}", "narration": text}
        for i, text in enumerate(("Too much noise.", "Not enough context.", "Filigran connects both."), start=1)
    ]
    return parse_video_spec({"title": "Demo", "scenes": scenes, **({"voiceover": voiceover} if voiceover else {})})


def test_a_spoken_narration_is_measured_from_its_wav_header(tmp_path):
    assert voice.wav_seconds(_wav_head(3.25)) == pytest.approx(3.25)
    assert voice.wav_seconds(_wav_head(0.8, rate=24000, channels=1)) == pytest.approx(0.8)
    pcm = tmp_path / "pcm.wav"
    with wave.open(str(pcm), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22050)
        out.writeframes(b"\x00\x00" * 22050 * 2)
    assert voice.wav_seconds(pcm.read_bytes()[:4096]) == pytest.approx(2.0)
    assert voice.wav_seconds(b"ID3 an mp3") is None


def test_a_narration_s_length_is_read_from_the_voice_cache_once(monkeypatch):
    from collections import OrderedDict

    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    reads: list[str] = []

    async def read_range(name, _offset, _length):
        reads.append(name)
        if name.endswith(f"{_digest('gone')}.wav"):
            raise FileNotFoundInStorageError(name)
        return _wav_head(4.5)

    monkeypatch.setattr(voice, "_SECONDS", OrderedDict())
    monkeypatch.setattr(file_storage, "read_range", read_range)
    digest = _digest("spoken")
    assert asyncio.run(voice.spoken_seconds(digest)) == pytest.approx(4.5)
    assert asyncio.run(voice.spoken_seconds(digest)) == pytest.approx(4.5)
    assert reads == [f"video-studio/voice-cache/{digest}.wav"]
    assert asyncio.run(voice.spoken_seconds(_digest("gone"))) is None


def test_the_preview_and_the_render_ask_the_voice_cache_for_the_same_narrations(monkeypatch, tmp_path):
    from app.services.file_storage import file_storage

    spec = _three_narrations({"pronunciations": [{"text": "Filigran", "say": "Fili-gran"}], "style": "Warm."})
    lines = voice.lines_of(spec)
    assert [line.scene_id for line in lines] == ["s1", "s2", "s3"]
    assert (lines[1].previous_text, lines[1].next_text) == (lines[0].text, lines[2].text)
    asked: list[str] = []

    async def download_to_path(name, path, *, missing_ok=False):
        asked.append(name)
        Path(path).write_bytes(_wav_head(1.0))

    monkeypatch.setattr(file_storage, "download_to_path", download_to_path)
    words = voice.lexicon_of(spec.voiceover)
    for line in lines:
        asyncio.run(
            voice.synthesize(
                PLATFORM,
                text=line.text,
                voice=spec.voiceover.voice,
                style=spec.voiceover.style,
                speed=spec.voiceover.speed,
                out_path=str(tmp_path / "voice.wav"),
                previous_text=line.previous_text,
                next_text=line.next_text,
                lexicon=words,
            )
        )
    assert asked == [
        f"video-studio/voice-cache/{voice.digest_of(PLATFORM, line, spec.voiceover, words)}.wav" for line in lines
    ]
    # A narration that writes no lexicon term keeps the digest it had before the lexicon existed.
    plain = voice.Line("s9", "Too much noise.", None, None)
    assert voice.digest_of(PLATFORM, plain, spec.voiceover, words) == voice.cache_digest(
        "filigran_gateway", str(PLATFORM_ID), "gpt-4o-mini-tts", "marin", "Warm.", 1.0, "Too much noise.", None, None, "chain1"
    )


def test_a_voice_token_names_one_narration_for_one_reader():
    from app.services.video_studio import media

    reader = uuid.uuid4()
    token, _expires = media.mint_voice_token(_digest("s1"), reader)
    assert media.decode_voice_token(token) == (_digest("s1"), reader)
    # Stable within its window, so the editor's player keeps its source across previews.
    assert media.mint_voice_token(_digest("s1"), reader)[0] == token
    music_token, _expires = media.mint_music_token({"kind": "track", "ref": "signal"}, reader)
    with pytest.raises(media.MediaTokenError):
        media.decode_voice_token(music_token)


def _voice_world(monkeypatch, *, spoken: dict[str, float], failed: dict[str, str]) -> list[str]:
    prepared: list[str] = []

    async def resolve(_db, _user_id, _voiceover):
        return PLATFORM

    async def spoken_seconds(digest):
        return next((seconds for name, seconds in spoken.items() if _digest(name) == digest), None)

    async def failure(digest):
        return next((reason for name, reason in failed.items() if _digest(name) == digest), None)

    async def prepare(_target, _user, lines, _voiceover, _lexicon=None):
        prepared.extend(line.scene_id for line, _digest_of in lines)
        return {digest for _line, digest in lines}

    monkeypatch.setattr(voice, "resolve", resolve)
    monkeypatch.setattr(voice, "digest_of", lambda _target, line, _voiceover, *_a, **_k: _digest(line.scene_id))
    monkeypatch.setattr(voice, "spoken_seconds", spoken_seconds)
    monkeypatch.setattr(voice, "failure", failure)
    monkeypatch.setattr(voice, "prepare", prepare)
    return prepared


def test_the_preview_plays_what_is_spoken_and_a_manager_s_draft_speaks_the_rest(monkeypatch):
    from app.services.video_studio import preview

    prepared = _voice_world(monkeypatch, spoken={"s1": 2.5}, failed={"s2": "The provider refused the voice."})
    user = SimpleNamespace(id=uuid.uuid4(), email="maker@example.com")
    state, lines = asyncio.run(preview._voice(object(), _three_narrations(), user, speak=True))
    assert state == {"state": "on", "volume": 1.0, "error": None}
    assert lines["s1"]["state"] == "ready" and lines["s1"]["seconds"] == 2.5
    assert "/api/v1/video-studio/voice/" in lines["s1"]["audio_url"]
    # What was spoken whatever its token: the editor's player keeps its source while it is the same.
    assert lines["s1"]["key"] == _digest("s1")
    assert lines["s2"] == {"state": "failed", "error": "The provider refused the voice."}
    assert lines["s3"] == {"state": "speaking"} and prepared == ["s3"]
    # A reader's preview speaks nothing: it plays what is spoken already.
    prepared.clear()
    _state, lines = asyncio.run(preview._voice(object(), _three_narrations(), user, speak=False))
    assert lines["s3"] == {"state": "none"} and prepared == []
    # Captions only, or no speech model: said, nothing spoken.
    state, lines = asyncio.run(preview._voice(object(), _three_narrations({"enabled": False}), user, speak=True))
    assert state["state"] == "off" and lines == {} and prepared == []


def test_the_preview_plans_a_spoken_narration_at_its_measured_length(monkeypatch):
    from app.services.video_studio import preview, templates

    async def no_problems(*_args, **_kwargs):
        return []

    async def no_template(*_args, **_kwargs):
        return None

    async def plain_theme(*_args, **_kwargs):
        return {}, {}, [], {}

    async def spoken_voice(_db, spec, _user, *, speak, lexicon=None):
        return {"state": "on", "volume": 0.9, "error": None}, {
            "s1": {"state": "ready", "seconds": 9.0, "audio_url": "/v"}
        }

    async def own_lexicon(_db, voiceover):
        return voice.lexicon_of(voiceover)

    monkeypatch.setattr(lexicon_store, "for_voiceover", own_lexicon)
    monkeypatch.setattr(projects, "verify_references", no_problems)
    monkeypatch.setattr(templates, "resolve_for_spec", no_template)
    monkeypatch.setattr(preview, "_theme", plain_theme)
    monkeypatch.setattr(preview, "_voice", spoken_voice)
    draft = {
        "title": "Demo",
        "freestyle": True,
        "scenes": [{"id": "s1", "type": "title", "title": "Hello", "duration": 3, "narration": "Hi."}],
    }
    project = VideoProject(id=uuid.uuid4(), user_id=uuid.uuid4(), title="Demo", spec=draft, revision=1)
    result = asyncio.run(preview.build_preview(object(), project, SimpleNamespace(id=uuid.uuid4()), draft))
    line = result["narration"][0]
    assert (line["seconds"], line["state"], line["audio_url"]) == (9.0, "ready", "/v")
    # Two words paced would last a second: the measured voice holds the card for nine.
    scene = result["document"]["scenes"][0]
    assert scene["end"] - scene["start"] >= 9.0 + line["start"]
    assert result["voice"]["volume"] == 0.9


def test_a_narration_is_spoken_once_as_its_maker_whatever_the_previews(monkeypatch):
    from app.llm.router import SpeechTarget

    claimed: set[str] = set()
    released: list[tuple[str, str | None]] = []
    said: list[tuple[str, identity.LLMCallerIdentity]] = []

    async def claim(digest):
        if digest in claimed:
            return False
        claimed.add(digest)
        return True

    async def release(digest, error):
        released.append((digest, error))

    async def synthesize(_target, *, text, out_path, **_kwargs):
        said.append((text, identity.resolve_llm_caller()))
        if text == "Broken.":
            raise RuntimeError("The provider refused the voice.")
        return "marin", []

    monkeypatch.setattr(voice, "_TASKS", {})
    monkeypatch.setattr(voice, "_claim", claim)
    monkeypatch.setattr(voice, "_release", release)
    monkeypatch.setattr(voice, "synthesize", synthesize)
    maker = _maker()
    target = SpeechTarget(
        provider=object(),
        model="gpt-4o-mini-tts",
        config=SimpleNamespace(id=PLATFORM_ID, provider="openai", display_name="OpenAI"),
    )
    voiceover = _spec().voiceover
    lines = [(voice.Line("s1", "Hello.", None, None), "d1"), (voice.Line("s2", "Broken.", None, None), "d2")]

    async def previews():
        first = await voice.prepare(target, maker, lines, voiceover)
        second = await voice.prepare(target, maker, lines, voiceover)
        await asyncio.gather(*list(voice._TASKS.values()))
        third = await voice.prepare(target, maker, lines[:1], voiceover)
        return first, second, third

    first, second, third = asyncio.run(previews())
    assert first == second == {"d1", "d2"} and third == {"d1"}
    caller = identity.LLMCallerIdentity(user_id=maker.id, email="maker@example.com")
    assert said == [("Hello.", caller), ("Broken.", caller)]
    assert released == [("d1", None), ("d2", "The provider refused the voice.")]


def test_the_editor_plays_a_spoken_narration_by_its_token(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1 import video_studio as api
    from app.services.video_studio import media

    audio = tmp_path / "voice.m4a"
    audio.write_bytes(b"\x00\x00\x00\x18ftypM4A ")
    reader = _maker()

    async def preview_audio(digest):
        return audio if digest == _digest("s1") else None

    class _Db:
        async def get(self, _model, _key):
            return reader

    @asynccontextmanager
    async def session():
        yield _Db()

    monkeypatch.setattr(voice, "preview_audio", preview_audio)
    monkeypatch.setattr(api, "async_session", session)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/video-studio")
    client = TestClient(app)
    token, _expires = media.mint_voice_token(_digest("s1"), reader.id)
    played = client.get(f"/api/v1/video-studio/voice/{token}")
    assert played.status_code == 200 and played.headers["content-type"] == "audio/mp4"
    unspoken, _expires = media.mint_voice_token(_digest("s2"), reader.id)
    assert client.get(f"/api/v1/video-studio/voice/{unspoken}").status_code == 404
    assert client.get("/api/v1/video-studio/voice/not-a-token").status_code == 404
