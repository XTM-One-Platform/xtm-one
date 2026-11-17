"""Music and the voice-over mix of the Video Studio.

Music generation as a provider capability (Eleven Music, Lyria), the
platform's choice of music model, the built-in scores composed for a video,
the bed a video plays (fitted, levelled, ducked by an envelope computed from
the narration), AI tracks as jobs, the voice chain and pronunciations, the
editor's preview of the bed and the agent tools.
"""

import asyncio
import base64
import json
import time
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.llm import router
from app.llm.music import MusicGenerationError, MusicProviderBusyError, MusicRequest
from app.services.video_studio import encoder, media, music, score, stitch, templates, timeline
from app.services.video_studio.spec import Music, VideoSpecError, authored_spec, parse_video_spec, referenced_ids

_REAL_CLIENT = httpx.AsyncClient
TAKE_ID = "00000000-0000-0000-0000-000000000001"
TRACK_ID = "00000000-0000-0000-0000-0000000000a1"
FILE_ID = "00000000-0000-0000-0000-0000000000f1"


def _mock_client(handler):
    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*args, **kwargs)

    return _factory


# ── Which models compose, which never chat ───────────────────────────────


def test_music_models_are_recognised_and_never_chat():
    for model in ("lyria-3.5", "lyria-3-clip-preview", "gemini/lyria-3-pro-preview", "music_v2_5", "stable-audio-2.5"):
        assert router.is_music_model(model), model
        # The catalog lists Lyria as chat for its text output: it is not a chat model.
        assert not router.is_chat_capable_model(model), model
    for model in ("gpt-5", "gemini-3-pro", "eleven_multilingual_v2", "", None):
        assert not router.is_music_model(model), model


def test_the_built_in_music_models_come_from_the_provider_classes():
    declared = {
        name: cls.default_music_model
        for name, cls in router.PROVIDER_MAP.items()
        if getattr(cls, "default_music_model", None)
    }
    assert declared == router.MUSIC_DEFAULTS
    assert {"gemini", "elevenlabs"} <= set(router.MUSIC_DEFAULTS)
    # A provider that serves no chat was added for its media: it composes first.
    assert router._MUSIC_PROVIDERS[0] == "elevenlabs"


def test_the_gateway_types_its_music_models():
    from app.llm import gateway_provider

    assert gateway_provider._classify_model("lyria-3.5") == "music"


# ── Eleven Music ──────────────────────────────────────────────────────────


def _eleven(monkeypatch, handler):
    from app.llm import elevenlabs_provider

    monkeypatch.setattr(elevenlabs_provider.httpx, "AsyncClient", _mock_client(handler))
    return elevenlabs_provider.ElevenLabsProvider(api_key="sk_test")


def test_eleven_music_composes_the_exact_length_instrumental(monkeypatch):
    seen = {}

    def _api(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"], seen["key"] = (
            request.url.path,
            json.loads(request.content),
            request.headers["xi-api-key"],
        )
        return httpx.Response(200, content=b"ID3MUSIC", headers={"content-type": "audio/mpeg"})

    provider = _eleven(monkeypatch, _api)
    assert asyncio.run(provider.list_music_models())[0] == provider.default_music_model == "music_v2_5"
    result = asyncio.run(
        provider.generate_music(
            MusicRequest(model="music_v2", prompt="Calm ambient pads", duration_seconds=75.4, negative_prompt="drums")
        )
    )
    assert seen["path"] == "/v1/music" and seen["key"] == "sk_test"
    assert seen["body"]["music_length_ms"] == 75400 and seen["body"]["force_instrumental"] is True
    assert seen["body"]["model_id"] == "music_v2" and "Avoid: drums" in seen["body"]["prompt"]
    assert result.audio == b"ID3MUSIC" and result.mime == "audio/mpeg"


def test_eleven_music_refusals_are_readable_and_a_busy_api_is_asked_again(monkeypatch):
    provider = _eleven(
        monkeypatch, lambda _r: httpx.Response(422, json={"detail": {"message": "prompt names an artist"}})
    )
    with pytest.raises(MusicGenerationError, match="prompt names an artist"):
        asyncio.run(provider.generate_music(MusicRequest(model="music_v1", prompt="x")))
    provider = _eleven(monkeypatch, lambda _r: httpx.Response(429, json={"detail": "slow down"}))
    with pytest.raises(MusicProviderBusyError):
        asyncio.run(provider.generate_music(MusicRequest(model="music_v1", prompt="x")))


# ── Lyria ─────────────────────────────────────────────────────────────────


def _gemini(monkeypatch, handler):
    from app.llm.gemini_provider import GeminiProvider

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    provider = GeminiProvider.__new__(GeminiProvider)
    provider._api_key = "g-key"
    return provider


def _lyria_answer(*blocks) -> httpx.Response:
    return httpx.Response(200, json={"steps": [{"type": "model_output", "content": list(blocks)}]})


def test_lyria_writes_the_length_and_no_vocals_into_its_prompt(monkeypatch):
    seen = {}

    def _api(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return _lyria_answer(
            {"type": "text", "text": "[Intro] soft pads"},
            {"type": "audio", "mime_type": "audio/mpeg", "data": base64.b64encode(b"MP3").decode()},
        )

    provider = _gemini(monkeypatch, _api)
    result = asyncio.run(
        provider.generate_music(MusicRequest(model="lyria-3.5", prompt="Cinematic build", duration_seconds=90))
    )
    assert seen["path"] == "/v1beta/interactions" and seen["body"]["model"] == "lyria-3.5"
    directions = seen["body"]["input"]
    assert "1:30" in directions and "Instrumental only, no vocals." in directions
    assert result.audio == b"MP3" and result.description == "[Intro] soft pads" and not result.notes


def test_lyria_clip_says_its_length_is_fixed_and_no_audio_is_an_error(monkeypatch):
    audio = {"type": "audio", "data": base64.b64encode(b"MP3").decode()}
    provider = _gemini(monkeypatch, lambda _r: _lyria_answer(audio))
    result = asyncio.run(
        provider.generate_music(MusicRequest(model="lyria-3-clip-preview", prompt="x", duration_seconds=60))
    )
    assert any("30-second clips" in note for note in result.notes)
    provider = _gemini(monkeypatch, lambda _r: _lyria_answer({"type": "text", "text": "no"}))
    with pytest.raises(MusicGenerationError, match="no music"):
        asyncio.run(provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x")))
    provider = _gemini(monkeypatch, lambda _r: httpx.Response(503, json={"error": {"message": "overloaded"}}))
    with pytest.raises(MusicProviderBusyError, match="overloaded"):
        asyncio.run(provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x")))


# ── The platform's music model ────────────────────────────────────────────


def _config(provider: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider=provider, display_name=None)


class _Db:
    def __init__(self, configs=(), rows=None):
        self.configs = list(configs)
        self.rows = rows or {}

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.configs)))

    async def get(self, _model, key):
        return self.rows.get(str(key))


@pytest.fixture
def _routing(monkeypatch):
    state = {"ps": SimpleNamespace(music_provider_id=None, music_model=None), "withheld": set()}

    async def _ps(_db):
        return state["ps"]

    async def _cached(config):
        return f"provider:{config.provider}"

    async def _route(_db, _user_id, configs):
        usable = [c for c in configs if c.id not in state["withheld"]]
        return SimpleNamespace(team_first=usable, excluded_ids=frozenset())

    monkeypatch.setattr(router, "_get_platform_settings", _ps)
    monkeypatch.setattr(router, "_cached_provider", _cached)
    monkeypatch.setattr(router, "provider_routing", _route)
    return state


def test_the_music_model_is_the_callers_then_the_admins_then_a_built_in_one(_routing):
    gemini, eleven = _config("gemini"), _config("elevenlabs")
    db = _Db([gemini, eleven])
    assert asyncio.run(router.get_music_provider(db)) == ("provider:elevenlabs", "music_v2_5", eleven)
    _routing["ps"] = SimpleNamespace(music_provider_id=gemini.id, music_model="lyria-3-clip-preview")
    assert asyncio.run(router.get_music_provider(db))[1:] == ("lyria-3-clip-preview", gemini)
    assert asyncio.run(router.get_music_provider(db, provider_id=eleven.id, model="music_v1"))[1:] == (
        "music_v1",
        eleven,
    )


def test_a_music_model_the_person_may_not_use_is_never_used(_routing):
    reserved, gemini = _config("elevenlabs"), _config("gemini")
    _routing["ps"] = SimpleNamespace(music_provider_id=reserved.id, music_model="music_v2")
    _routing["withheld"] = {reserved.id}
    assert asyncio.run(router.get_music_provider(_Db([reserved, gemini])))[2] is gemini
    with pytest.raises(router.ProviderNotUsableError, match="reserved for other groups"):
        asyncio.run(router.get_music_provider(_Db([reserved, gemini]), provider_id=reserved.id))
    with pytest.raises(ValueError, match="Music generation"):
        asyncio.run(router.get_music_provider(_Db([_config("anthropic")])))


def test_the_setting_is_a_provider_reference_like_the_speech_one():
    from app.api.v1 import settings as settings_api
    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest, GeneralAssistantConfigResponse
    from app.models.platform_settings import PlatformSettings

    for column in ("music_provider", "music_provider_id", "music_model"):
        assert hasattr(PlatformSettings, column)
        assert column in GeneralAssistantConfigResponse.model_fields
        assert column in GeneralAssistantConfigRequest.model_fields
        assert column in settings_api._GA_PROVIDER_REFERENCE_FIELDS
    assert ("music_provider", "music_provider_id") in settings_api._GA_ALIGNED_PROVIDER_PAIRS
    assert any(field == "music_provider_id" for field, _scope, _label in settings_api._GA_PROVIDER_ID_FIELDS)
    paths = {route.path for route in settings_api.router.routes}
    assert "/available-music-models" in paths


# ── The spec ──────────────────────────────────────────────────────────────


def test_music_plays_one_source_and_knows_the_built_in_tracks():
    assert Music(track="Deep-Focus").source == ("track", "deep-focus")
    assert Music(music_id=f"music:{TRACK_ID}").source == ("music", TRACK_ID)
    assert Music(file_id=FILE_ID, enabled=False).source is None
    # Levels alone are valid: for the track a template brings.
    assert Music(volume=0.3).source is None
    with pytest.raises(ValueError, match="one source"):
        Music(track="signal", file_id=FILE_ID)
    with pytest.raises(ValueError, match="no built-in track"):
        Music(track="elevator-jazz")
    defaults = Music(track="signal")
    assert (defaults.volume, defaults.duck_db, defaults.ending, defaults.instrumental) == (0.5, 10.0, "fade", True)


def test_an_ai_track_is_a_reference_the_writer_must_be_allowed():
    raw = {"title": "T", "scenes": [{"type": "title", "title": "Hi"}], "music": {"music_id": TRACK_ID}}
    assert referenced_ids(authored_spec(raw))["music"] == {TRACK_ID}


def test_pronunciations_are_part_of_the_voice_over():
    raw = {
        "title": "T",
        "scenes": [{"type": "title", "title": "Hi"}],
        "voiceover": {"pronunciations": [{"text": "SaaS", "say": "sass"}]},
    }
    assert parse_video_spec(authored_spec(raw)).voiceover.pronunciations[0].say == "sass"
    raw["voiceover"]["pronunciations"] = [{"text": "", "say": "x"}]
    with pytest.raises(VideoSpecError):
        authored_spec(raw)


# ── Templates: a brand's sound ────────────────────────────────────────────


def _template(style: dict) -> templates.VideoTemplate:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        name="Brand",
        kind="video",
        description=None,
        tags=[],
        theme_config={},
        extra_metadata={"video": style, "supported_schemes": ["dark", "light"]},
    )
    return templates.VideoTemplate.from_container(row)


def _authored(**overrides) -> dict:
    raw = {"title": "T", "scenes": [{"type": "recording", "take_id": TAKE_ID}]}
    raw.update(overrides)
    return authored_spec(raw)


def test_a_template_presets_a_built_in_track_the_authors_own_source_replaces():
    style = {"defaults": {"music": {"track": "ascent", "volume": 0.7}}}
    assert templates.validate_style(style)[1] == []
    assert templates.validate_style({"defaults": {"music": {"volume": 0.7}}})[1]
    assert templates.validate_style({"defaults": {"music": {"track": "ascent", "file_id": FILE_ID}}})[1]
    brand = _template(style)
    assert templates.apply_template(_authored(), brand).spec["music"]["track"] == "ascent"
    levelled = templates.apply_template(_authored(music={"volume": 0.2}), brand).spec["music"]
    assert (levelled["track"], levelled["volume"]) == ("ascent", 0.2)
    own = templates.apply_template(_authored(music={"file_id": FILE_ID}), brand).spec["music"]
    assert own.get("track") is None and own["file_id"] == FILE_ID and own["volume"] == 0.7
    silent = templates.apply_template(_authored(music={"enabled": False}), brand).spec["music"]
    assert silent["enabled"] is False and "track" not in silent


# ── The built-in scores ───────────────────────────────────────────────────


def test_the_library_lists_every_style_with_what_it_suits():
    tracks = music.library()
    assert {t["id"] for t in tracks} == set(score.track_ids()) and len(tracks) >= 7
    for track in tracks:
        assert track["description"] and track["purposes"] and set(track["purposes"]) <= set(templates.TEMPLATE_PURPOSES)


def test_the_editor_words_every_built_in_track_as_the_backend_does():
    """The library is read in the reader's language: its words are i18n keys the editor declares."""
    source = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "video-studio" / "video-format.ts"
    ).read_text(encoding="utf-8")
    for style in score.STYLES:
        assert f'"{style.description}"' in source, style.id
        assert f'"{style.mood}"' in source and f'"{style.genre}"' in source, style.id


def test_a_score_keeps_its_tempo_and_lands_its_final_chord_before_the_last_frame():
    style = score.get_style("bright-horizon")
    plan = score.plan_sections(style, 75.0, None)
    assert plan.bar == pytest.approx(240 / style.bpm)
    assert 1.6 <= plan.tail < 1.6 + plan.bar and plan.final_at + plan.tail == pytest.approx(75.0)
    assert len(plan.chords) == plan.bars + 1 and plan.chords[-1] == style.final
    assert plan.chords[plan.bars - len(style.cadence) : plan.bars] == style.cadence


def test_sections_turn_on_the_scene_changes_and_leave_the_voice_room():
    style = score.get_style("signal")
    cues = [
        score.Cue(0.0, "title"),
        score.Cue(9.6, "recording", narrated=True),
        score.Cue(29.0, "statement"),
        score.Cue(29.5, "clip"),
        score.Cue(48.0, "outro"),
    ]
    plan = score.plan_sections(style, 60.0, cues)
    starts = [s.start for s in plan.sections]
    assert starts[0] == 0 and plan.sections[0].energy == 1  # every score opens
    assert round(9.6 / plan.bar) in starts  # on the bar line nearest the cut
    narrated = next(s for s in plan.sections if s.start == round(9.6 / plan.bar))
    assert narrated.room and narrated.energy <= 2
    # Two scene changes a bar apart are one section.
    assert len([s for s in starts if abs(s - 29.0 / plan.bar) <= 1]) == 1


def test_a_score_is_the_video_s_exact_length_and_the_same_every_time(tmp_path):
    cues = [score.Cue(0.0, "title"), score.Cue(3.0, "statement")]
    paths = [tmp_path / "a.wav", tmp_path / "b.wav"]
    for path in paths:
        score.render_score("momentum", 7.3, cues, str(path))
    assert paths[0].read_bytes() == paths[1].read_bytes()
    with wave.open(str(paths[0])) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (2, 48_000, round(7.3 * 48_000))
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert np.abs(data[-96:]).max() <= 2  # silence on the last frame
    assert score.score_digest("momentum", 7.3, cues) != score.score_digest("momentum", 7.3, cues[:1])


@pytest.mark.parametrize("track", score.track_ids())
def test_every_style_renders_across_a_chunk_boundary(track, tmp_path):
    out = tmp_path / f"{track}.wav"
    score.render_score(track, 18.0, None, str(out))
    with wave.open(str(out)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2).astype(np.float32) / 32768
    assert np.isfinite(data).all() and 0.05 < np.abs(data).max() < 1.0
    # No click where one chunk hands over to the next: no step there larger
    # than the music's own steps.
    boundary = 16 * 48_000
    for channel in data.T:
        steps = np.abs(np.diff(channel))
        assert steps[boundary - 48 : boundary + 48].max() <= np.percentile(steps, 99.9)


# ── Beds ──────────────────────────────────────────────────────────────────


def test_a_long_track_is_cut_to_the_video_or_edited_to_end_with_it():
    assert encoder.bed_segments(200.0, 60.0, start=12.0) == [(12.0, 72.0)]
    head, tail = encoder.bed_segments(200.0, 60.0, ending="natural", crossfade=2.0)
    assert tail == (188.0, 200.0) and head == (0.0, 50.0)  # its last 12 s; 50 + 12 - 2 of crossfade = 60


def test_a_short_track_loops_crossfaded_and_its_end_can_land_on_the_video_s():
    segments = encoder.bed_segments(30.0, 75.0, crossfade=2.0)
    assert segments == [(0.0, 30.0), (0.0, 30.0), (0.0, 30.0)]
    natural = encoder.bed_segments(30.0, 75.0, ending="natural", crossfade=2.0)
    played = sum(b - a for a, b in natural) - 2.0 * (len(natural) - 1)
    assert played == pytest.approx(75.0) and natural[-1] == (0.0, 30.0)
    assert encoder.bed_segments(30.0, 75.0, loop=False) == [(0.0, 30.0)]


def test_the_bed_filter_crossfades_levels_and_fades_to_the_exact_length():
    graph = encoder.bed_filter(
        [(0.0, 30.0), (0.0, 30.0)], duration=50.0, crossfade=2.0, gain_db=-3.5, fade_in=1.0, fade_out=2.5
    )
    # Each segment its own input: never two branches of one asplit, which ffmpeg 7.1 crossfades into silence.
    assert "asplit" not in graph and "[0:a]" in graph and "[1:a]" in graph
    assert graph.count("acrossfade=d=2.000:c1=qsin:c2=qsin") == 1
    assert "volume=-3.50dB" in graph and "afade=t=out:st=47.500:d=2.500" in graph
    assert graph.endswith("atrim=duration=50.000[bed]")


def test_a_bed_is_decided_by_its_source_its_length_and_how_it_is_fitted():
    bed = music.Bed.of(Music(track="ascent", start=20), 61.234, [score.Cue(0.0, "title")])
    assert (bed.kind, bed.start, bed.duration, bed.cues) == ("track", 0.0, 61.23, ((0.0, "title", False),))
    upload = music.Bed.of(Music(file_id=FILE_ID, start=20), 61.2, [score.Cue(0.0, "title")])
    assert upload.cues == () and upload.start == 20.0
    assert music.Bed.from_claims(json.loads(json.dumps(bed.claims()))) == bed
    assert bed.digest() != music.Bed.of(Music(track="ascent"), 62.0, [score.Cue(0.0, "title")]).digest()
    assert music.Bed.of(Music(prompt="calm"), 60, []) is None
    assert music.fitted_seconds(56.2) == 65


def test_a_music_token_is_not_a_media_token(monkeypatch):
    from datetime import UTC, datetime, timedelta

    # Two moments of the CURRENT window: the token they share is decoded on the real clock.
    window = int(media.MUSIC_TOKEN_WINDOW.total_seconds())
    start = datetime.fromtimestamp(int(time.time()) // window * window, UTC)
    moments = iter([start + timedelta(minutes=2), start + media.MUSIC_TOKEN_WINDOW - timedelta(seconds=1)])

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(moments)

    monkeypatch.setattr(media, "datetime", _Clock)
    reader = uuid.uuid4()
    bed = {"kind": "track", "ref": "signal", "duration": 30.0}
    token, expires = media.mint_music_token(bed, reader)
    claims, _user = media.decode_music_token(token)
    assert claims["ref"] == "signal"
    # The editor's player keeps its source across previews: the same bed, the same URL.
    assert media.mint_music_token(bed, reader)[0] == token
    monkeypatch.undo()
    assert expires - time.time() >= (media.MEDIA_TOKEN_TTL - media.MUSIC_TOKEN_WINDOW).total_seconds() - 3600
    with pytest.raises(media.MediaTokenError):
        media.decode_media_token(token)
    file_token, _ = media.mint_media_token(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(media.MediaTokenError):
        media.decode_music_token(file_token)


# ── Ducking: computed from where the narration is ─────────────────────────


def test_close_sentences_keep_the_music_down_between_them():
    assert encoder.merge_windows([(8.0, 14.0), (14.6, 20.0), (40.0, 46.0)]) == [(8.0, 20.0), (40.0, 46.0)]


def test_the_preview_envelope_is_the_render_s():
    windows = [(0.2, 3.0), (8.0, 14.0)]
    points = encoder.duck_points(windows, depth_db=10.0, duration=30.0)
    gain = 10 ** (-10 / 20)
    merged = encoder.merge_windows(windows)
    for t, g in points:
        assert g == pytest.approx(encoder._duck_gain(t, merged, 1 - gain), abs=1e-4)
    by_time = dict(points)
    assert by_time[8.0] == pytest.approx(gain, abs=1e-4) and by_time[7.65] == 1.0
    assert by_time[14.7] == 1.0 and points[0][1] < 1.0  # a sentence at 0.2 s starts the video down
    assert encoder.duck_expression([], depth_db=10.0) is None


def test_the_plan_says_where_the_narration_is_and_what_each_scene_is():
    spec = parse_video_spec(
        authored_spec(
            {
                "title": "T",
                "scenes": [
                    {"type": "title", "title": "Hi", "narration": "Welcome to the platform, the one place for it all."},
                    {"type": "statement", "lines": ["Big *news*."], "duration": 4},
                    {"type": "outro", "title": "Bye", "narration": "Try it today."},
                ],
            }
        )
    )
    plan = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={})
    first, _statement, last = (scene.id for scene in spec.scenes)
    assert set(plan.narration_spans) == {first, last}
    for sid, start in plan.narration_starts.items():
        assert plan.narration_spans[sid][0] == start and plan.narration_spans[sid][1] > start
    assert [(c.kind, c.narrated) for c in plan.cues()] == [("title", True), ("statement", False), ("outro", True)]
    assert [c.start for c in plan.cues()] == [s["start"] for s in plan.document["scenes"]]
    assert plan.duck_windows() == sorted(plan.narration_spans.values())
    # A render whose voice failed for a scene does not duck the music for it.
    assert plan.duck_windows(voiced={last}) == [plan.narration_spans[last]]


def test_the_master_is_a_measured_linear_gain():
    stats = {
        "input_i": "-21.4",
        "input_tp": "-6.0",
        "input_lra": "5.1",
        "input_thresh": "-31.8",
        "target_offset": "0.3",
    }
    chain = encoder.master_filter(stats)
    assert "measured_I=-21.40" in chain and "linear=true" in chain and f"I={encoder.MASTER_LUFS}" in chain
    assert encoder.master_filter(None) == "anull"
    assert encoder.gain_to_reference(-30.0) == 12.0 and encoder.gain_to_reference(None) == 0.0
    assert encoder.gain_to_reference(-60.0) == 18.0


# ── The voice ─────────────────────────────────────────────────────────────


def test_pronunciations_replace_whole_words_once_the_captions_keep_the_text():
    from app.services.video_studio import lexicon

    words = lexicon.merge(
        (),
        [
            SimpleNamespace(text="XTM", say="X-T-M", match="word"),
            SimpleNamespace(text="XTM One", say="X-T-M One", match="case_insensitive"),
            SimpleNamespace(text="T", say="tee"),
        ],
    )
    # Whole words only, and letters sent glued into the words of the written form.
    assert words.rewrite("Meet xtm one, the XTM hub. XTMs stay.") == "Meet XTM One, the XTM hub. XTMs stay."
    assert words.caption_text("Meet XTM One.") == "Meet XTM One."
    assert lexicon.Lexicon().rewrite("No change") == "No change" and words.rewrite(None) is None


def test_the_voice_chain_trims_high_passes_de_esses_and_compresses():
    for step in ("silenceremove", "areverse", "highpass=f=75", "deesser", "acompressor"):
        assert step in encoder.VOICE_CHAIN


# ── AI tracks ─────────────────────────────────────────────────────────────


def test_a_saved_music_prompt_starts_one_track_and_a_draft_keeps_it(monkeypatch):
    started = []

    async def _start(_db, _user, **kwargs):
        started.append(kwargs)
        return SimpleNamespace(id=uuid.UUID(TRACK_ID)), "Label"

    monkeypatch.setattr(music, "start_music", _start)
    user = SimpleNamespace(id=uuid.uuid4())
    authored = {"title": "Launch", "music": {"prompt": "uplifting synths, 118 BPM"}}
    count, notes = asyncio.run(music.start_for_spec(_Db(), user, authored, planned_seconds=50.0))
    assert (count, notes) == (1, []) and authored["music"]["music_id"] == TRACK_ID
    assert started[0]["duration_seconds"] == 58 and started[0]["instrumental"] is True
    # The editor's draft, saved without the id: the stored one is kept, nothing paid twice.
    row = SimpleNamespace(prompt="uplifting synths, 118 BPM")
    draft = {"title": "Launch", "music": {"prompt": "uplifting synths, 118 BPM"}}
    assert asyncio.run(
        music.start_for_spec(_Db(rows={TRACK_ID: row}), user, draft, planned_seconds=50.0, previous=authored)
    ) == (0, [])
    assert draft["music"]["music_id"] == TRACK_ID and len(started) == 1
    # A built-in track or an upload asks nothing of a model.
    assert asyncio.run(
        music.start_for_spec(_Db(), user, {"music": {"track": "signal", "prompt": "x"}}, planned_seconds=9)
    ) == (0, [])


def test_a_prompt_with_no_music_model_saves_with_a_note(monkeypatch):
    async def _start(*_a, **_k):
        raise music.MusicError("No music model is available to you.")

    monkeypatch.setattr(music, "start_music", _start)
    authored = {"title": "T", "music": {"prompt": "calm"}}
    count, notes = asyncio.run(
        music.start_for_spec(_Db(), SimpleNamespace(id=uuid.uuid4()), authored, planned_seconds=30)
    )
    assert count == 0 and "No AI track was started" in notes[0] and "music_id" not in authored["music"]


def test_only_a_failed_track_is_generated_again_and_not_forever(monkeypatch):
    from app.models.video_studio import JOB_FAILED, JOB_READY

    class _Session:
        async def refresh(self, *_a, **_k):
            return None

    row = SimpleNamespace(status=JOB_READY, params={}, prompt="x", model="m", error=None)
    with pytest.raises(music.MusicError, match="Only a failed"):
        asyncio.run(music.restart_music(_Session(), SimpleNamespace(id=uuid.uuid4()), row))
    row = SimpleNamespace(status=JOB_FAILED, params={"attempt": music.MAX_RETRIES}, prompt="x", model="m", error="e")
    with pytest.raises(music.MusicError, match="generated again"):
        asyncio.run(music.restart_music(_Session(), SimpleNamespace(id=uuid.uuid4()), row))


def test_a_track_not_ready_never_plays_and_says_why():
    from app.models.video_studio import JOB_FAILED, JOB_RUNNING

    running = SimpleNamespace(status=JOB_RUNNING, file_id=None, name="Bed", error=None)
    with pytest.raises(RuntimeError, match="still being composed"):
        asyncio.run(music.ready_file(_Db(rows={TRACK_ID: running}), TRACK_ID))
    failed = SimpleNamespace(status=JOB_FAILED, file_id=None, name="Bed", error="blocked prompt")
    with pytest.raises(RuntimeError, match="blocked prompt"):
        asyncio.run(music.ready_file(_Db(rows={TRACK_ID: failed}), TRACK_ID))


# ── The preview ───────────────────────────────────────────────────────────


def _planned(spec_raw: dict):
    from app.services.video_studio import preview

    spec = parse_video_spec(authored_spec(spec_raw))
    plan = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={})
    return preview, spec, plan


def test_the_preview_plays_the_bed_the_render_prepares():
    preview, spec, plan = _planned(
        {
            "title": "T",
            "scenes": [{"type": "title", "title": "Hi", "narration": "Hello there, welcome to the platform."}],
            "music": {"track": "deep-focus", "duck_db": 12},
        }
    )
    user = SimpleNamespace(id=uuid.uuid4())
    payload = asyncio.run(preview._music(_Db(), spec, plan, user))
    assert "/api/v1/video-studio/music-bed/" in payload["url"] and payload["source"] == "track"
    token = payload["url"].rsplit("/", 1)[-1]
    claims, reader = media.decode_music_token(token)
    assert reader == user.id and claims["ref"] == "deep-focus" and claims["duration"] == round(plan.duration, 2)
    assert min(g for _t, g in payload["duck"]) == pytest.approx(10 ** (-12 / 20), abs=1e-3)
    # What the bed is whatever its token: the editor's player keeps its source while it is the same.
    assert payload["key"] == music.Bed.from_claims(claims).digest()


def test_an_ai_track_being_composed_or_failed_previews_its_state():
    from app.models.video_studio import JOB_FAILED, JOB_RUNNING

    preview, spec, plan = _planned(
        {"title": "T", "scenes": [{"type": "title", "title": "Hi"}], "music": {"music_id": TRACK_ID}}
    )
    user = SimpleNamespace(id=uuid.uuid4())
    running = asyncio.run(
        preview._music(
            _Db(rows={TRACK_ID: SimpleNamespace(status=JOB_RUNNING, progress=40, file_id=None)}), spec, plan, user
        )
    )
    assert running["url"] is None and running["state"] == {"state": "music", "music_id": TRACK_ID, "progress": 40}
    failed = asyncio.run(
        preview._music(
            _Db(rows={TRACK_ID: SimpleNamespace(status=JOB_FAILED, error="refused", file_id=None)}), spec, plan, user
        )
    )
    assert failed["state"]["state"] == "failed" and failed["state"]["error"] == "refused"
    gone = asyncio.run(preview._music(_Db(), spec, plan, user))
    assert gone["state"]["state"] == "failed"


# ── The agent tools ───────────────────────────────────────────────────────


def test_the_tools_offer_music():
    from app.services.builtin_tools.video_studio import _definitions, build_video_studio_tools

    names = {d.name for d in _definitions(["high"])}
    assert {"generate_music", "get_video_jobs", "video_studio_capabilities"} <= names
    jobs_tool = next(d for d in _definitions(["high"]) if d.name == "get_video_jobs")
    assert "music_ids" in jobs_tool.input_schema["properties"]
    create = next(d for d in _definitions(["high"]) if d.name == "create_video")
    assert "music_library" in create.description and "pronunciations" in create.description
    _, executor = build_video_studio_tools(user_id=str(uuid.uuid4()))
    from app.tools.base import ToolCall

    result = asyncio.run(executor.execute_tool(ToolCall(id="c1", name="generate_music", arguments={})))
    assert result.is_error and "prompt" in result.content


# ── With ffmpeg: the chain end to end ─────────────────────────────────────

needs_ffmpeg = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed here")


def _speech_like(path: Path, *, seconds: float, lead: float, gain: float) -> None:
    rate = 24_000
    t = np.arange(int(seconds * rate)) / rate
    buzz = sum(np.sin(2 * np.pi * f * t) / (i + 1) for i, f in enumerate((140, 280, 700, 1200, 2500)))
    data = buzz * (np.sin(2 * np.pi * 4 * t) > 0) * gain
    data = np.concatenate([np.zeros(int(lead * rate)), data, np.zeros(int(2 * lead * rate))])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())


@needs_ffmpeg
def test_a_bed_is_the_video_s_length_at_the_voice_s_loudness(tmp_path):
    source = tmp_path / "track.wav"
    score.render_score("bright-horizon", 20.0, None, str(source))
    bed = tmp_path / "bed.wav"
    asyncio.run(encoder.prepare_bed(str(source), str(bed), duration=47.0, ending="natural"))
    assert asyncio.run(encoder.probe(str(bed))).duration == pytest.approx(47.0, abs=0.02)
    assert asyncio.run(encoder.measure_loudness(str(bed))) == pytest.approx(encoder.REFERENCE_LUFS, abs=1.0)


@needs_ffmpeg
def test_every_narration_comes_out_trimmed_at_one_loudness(tmp_path):
    levels = []
    for i, (lead, gain) in enumerate(((0.5, 0.9), (0.25, 0.12))):
        raw, clean = tmp_path / f"raw{i}.wav", tmp_path / f"clean{i}.wav"
        _speech_like(raw, seconds=4.0, lead=lead, gain=gain)
        asyncio.run(encoder.condition_voice(str(raw), str(clean)))
        assert asyncio.run(encoder.probe(str(clean))).duration < asyncio.run(encoder.probe(str(raw))).duration - 0.3
        levels.append(asyncio.run(encoder.measure_loudness(str(clean))))
    assert levels[0] == pytest.approx(encoder.REFERENCE_LUFS, abs=0.6) and levels[1] == pytest.approx(
        levels[0], abs=0.6
    )


@needs_ffmpeg
def test_the_master_is_the_video_s_length_at_the_target_loudness(tmp_path):
    source, bed, raw, clean = (tmp_path / n for n in ("s.wav", "bed.wav", "raw.wav", "voice.wav"))
    score.render_score("deep-focus", 24.0, None, str(source))
    asyncio.run(encoder.prepare_bed(str(source), str(bed), duration=24.0))
    _speech_like(raw, seconds=5.0, lead=0.3, gain=0.5)
    asyncio.run(encoder.condition_voice(str(raw), str(clean)))
    stem = stitch.stitch([stitch.Segment("s1", str(clean), 6.0)], duration=24.0, out_path=str(tmp_path / "stem.wav"))
    out = tmp_path / "mix.m4a"
    asyncio.run(
        encoder.mix_audio(
            out_path=str(out),
            duration=24.0,
            voice=encoder.VoiceTrack(stem.path),
            clips=[],
            music=encoder.MusicTrack(str(bed), 0.5, 10.0, ((6.0, 11.0),)),
        )
    )
    assert asyncio.run(encoder.probe(str(out))).duration == pytest.approx(24.0, abs=0.05)
    assert asyncio.run(encoder.measure_loudness(str(out))) == pytest.approx(encoder.MASTER_LUFS, abs=1.0)
