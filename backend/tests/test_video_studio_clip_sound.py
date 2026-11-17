"""A clip's own sound in the Video Studio mix.

Which clip scenes play their own sound (``clip_sound_choice``: the scene's
``audio``, the video's ``clip_sound``, an AI shot under the music), how a
sound kept sits under the voice (levelled toward the reference, lowered under
every sentence, faded over its transitions), what the preview and the tool
results say about it.
"""

import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest

from app.services.video_studio import encoder, timeline
from app.services.video_studio.spec import clip_sound_choice, parse_video_spec, sound_summary

SHOT_ID = "00000000-0000-0000-0000-0000000000c1"
FILE_ID = "00000000-0000-0000-0000-0000000000f1"
SHOT = timeline.ClipMedia(duration=4.0, width=1280, height=720, fps=24, frame_count=96, has_audio=True)


def _video(*scenes: dict, **top) -> dict:
    return {"title": "Sound", "freestyle": True, "scenes": list(scenes), **top}


def _shot(sid: str = "shot", **fields) -> dict:
    return {"id": sid, "type": "clip", "clip_id": SHOT_ID, **fields}


def _upload(sid: str = "film", **fields) -> dict:
    return {"id": sid, "type": "clip", "file_id": FILE_ID, **fields}


def _choice(scene: dict, **top) -> tuple[bool, str]:
    parsed = parse_video_spec(_video(scene, **top))
    return clip_sound_choice(parsed.scenes[0], parsed)


# ── The rule ──────────────────────────────────────────────────────────────


def test_an_ai_shot_gives_its_sound_up_to_the_music_and_an_upload_keeps_its_own():
    track = {"music": {"track": "deep-focus"}}
    assert _choice(_shot()) == (True, "auto")
    assert _choice(_shot(), **track) == (False, "music")
    # A track asked for and composed on save counts as music already.
    assert _choice(_shot(), music={"prompt": "Calm ambient pads, 90 BPM, instrumental"}) == (False, "music")
    assert _choice(_shot(), music={"enabled": False, "track": "deep-focus"}) == (True, "auto")
    assert _choice(_upload(), **track) == (True, "auto")


def test_the_video_decides_for_every_clip_and_a_scene_for_itself():
    track = {"music": {"track": "signal"}}
    assert _choice(_upload(), clip_sound="off", **track) == (False, "video")
    assert _choice(_shot(), clip_sound="keep", **track) == (True, "video")
    assert _choice(_shot(audio=True), **track) == (True, "scene")
    assert _choice(_upload(audio=False), clip_sound="keep") == (False, "scene")
    assert _choice(_shot(audio=True), clip_sound="off") == (True, "scene")


def test_a_clip_scene_left_alone_stores_no_sound_choice():
    from app.services.video_studio.spec import authored_spec

    stored = authored_spec(_video(_shot()))
    assert "audio" not in stored["scenes"][0] and "clip_sound" not in stored


# ── The plan ──────────────────────────────────────────────────────────────


def _plan(spec: dict, clips: dict[str, timeline.ClipMedia] | None = None) -> timeline.TimelinePlan:
    media = timeline.PlanMedia(clips=clips if clips is not None else {"shot": SHOT, "film": SHOT})
    return timeline.plan(parse_video_spec(spec), media, fps=30, theme={})


def test_the_plan_plays_only_the_clip_sound_the_rule_keeps():
    under_music = _plan(_video(_shot(), _upload(), music={"track": "deep-focus"}))
    assert [sound.scene_id for sound in under_music.clip_audio] == ["film"]
    assert under_music.clip_sound == {"shot": (False, "music"), "film": (True, "auto")}
    # The music goes down under the upload's sound, not under the silenced shot.
    film = under_music.clip_audio[0]
    assert under_music.duck_windows() == [(film.start, film.start + film.duration)]

    voice_and_music = _plan(_video(_shot(), _upload(), music={"track": "deep-focus"}, clip_sound="off"))
    assert voice_and_music.clip_audio == [] and voice_and_music.duck_windows() == []


def test_a_clip_without_sound_says_so_and_volume_zero_plays_nothing():
    silent = timeline.ClipMedia(duration=4.0, width=1280, height=720, fps=24, frame_count=96, has_audio=False)
    plan = _plan(_video(_shot(audio=True), _upload(volume=0)), clips={"shot": silent, "film": SHOT})
    assert plan.clip_sound == {"shot": (False, "silent"), "film": (False, "auto")}
    assert plan.clip_audio == []


def test_a_clip_sound_fades_over_the_transitions_around_it():
    plan = _plan(
        _video(
            {"id": "open", "type": "title", "title": "Hello", "duration": 3},
            _shot(),
            {"id": "close", "type": "outro", "title": "Bye", "duration": 3},
        )
    )
    scenes = {scene["id"]: scene for scene in plan.document["scenes"]}
    (sound,) = plan.clip_audio
    assert sound.fade_in == pytest.approx(scenes["shot"]["transition_in"]["duration"]) and sound.fade_in > 0.15
    overlap = sound.start + sound.duration - scenes["close"]["start"]
    assert overlap > 0.3 and sound.fade_out == pytest.approx(overlap, abs=1e-3)


def test_the_voice_windows_are_the_narration_alone():
    plan = _plan(_video(_shot(narration="A threat actor moves laterally across the network.")))
    (span,) = plan.narration_spans.values()
    assert plan.voice_windows() == [span]
    assert plan.voice_windows(voiced=set()) == []
    # The music also goes down under the shot's own sound.
    assert len(plan.duck_windows()) == 2


# ── The mix ───────────────────────────────────────────────────────────────


def test_a_clip_sound_is_levelled_toward_the_voice_and_never_lifted_far():
    assert encoder.clip_gain(-10.0) == pytest.approx(encoder.REFERENCE_LUFS + 10.0)
    assert encoder.clip_gain(-40.0) == pytest.approx(encoder.CLIP_MAX_LIFT_DB)
    assert encoder.clip_gain(None) == 0.0
    assert encoder.clip_gain(-80.0) == pytest.approx(encoder.CLIP_MAX_LIFT_DB)


def test_a_clip_goes_down_under_the_narration_on_its_own_clock():
    track = encoder.ClipTrack(
        "clip.mp4",
        10.0,
        1.5,
        6.0,
        gain_db=-4.0,
        volume=0.8,
        fade_in=0.6,
        fade_out=0.8,
        windows=((2.0, 4.0), (12.0, 14.5), (40.0, 42.0)),
    )
    assert encoder.clip_duck_windows(track) == [(2.0, 4.5)]
    _inputs, graph = encoder.build_mix_filter(duration=30.0, voice=encoder.VoiceTrack("stem.wav"), clips=[track], music=None)
    chain = next(part for part in graph.split(";") if part.startswith("[1:a]"))
    assert "volume=-4.00dB,volume=0.800" in chain
    assert "afade=t=in:d=0.600" in chain and "afade=t=out:st=5.200:d=0.800" in chain
    assert "eval=frame" in chain and chain.index("eval=frame") < chain.index("adelay=10000|10000")
    # Nobody speaks over it: no envelope, the level alone.
    _inputs, quiet = encoder.build_mix_filter(
        duration=30.0,
        voice=None,
        clips=[encoder.ClipTrack("clip.mp4", 10.0, 0.0, 6.0, windows=((40.0, 42.0),))],
        music=None,
    )
    assert "eval=frame" not in quiet and "dB," not in quiet


# ── What the preview and the tools say ────────────────────────────────────


def test_the_sound_summary_says_what_plays_where_and_how_to_change_it():
    text = sound_summary(
        parse_video_spec(
            _video(
                _shot(narration="It starts with one phishing mail."),
                _upload(volume=0.6),
                _shot("kept", audio=True),
                music={"track": "deep-focus", "volume": 0.4},
            )
        )
    )
    assert text.startswith(
        "Sound: the voice-over leads; music: built-in track `deep-focus` at 0.4, 10 dB down under the voice."
    )
    assert "off for shot (an AI shot under the music)" in text
    assert "kept for film at 0.6, kept (its audio: true) at 0.8" in text
    assert "A scene's audio true / false decides" in text
    # A shot with no sound track is said to have none, never kept or dropped "(its audio)".
    mute = sound_summary(
        parse_video_spec(_video(_shot("kept", audio=False), _upload(volume=0.6), clip_sound="off")), silent={"kept"}
    )
    assert "none for kept (no sound track)" in mute and "kept (its audio" not in mute
    assert "off for film" in mute

    silent = sound_summary(parse_video_spec(_video({"id": "t", "type": "title", "title": "Hi"}, clip_sound="off")))
    assert silent == "Sound: no narration; no music."


def test_the_preview_says_why_a_shot_is_silent_and_ducks_what_plays(monkeypatch):
    import uuid

    from app.models.video_studio import JOB_READY, VideoClip, VideoProject
    from app.services.video_studio import preview, projects, templates

    async def nothing(*_args, **_kwargs):
        return []

    async def no_template(*_args, **_kwargs):
        return None

    async def plain_theme(*_args, **_kwargs):
        return {}, {}, [], {}

    monkeypatch.setattr(projects, "verify_references", nothing)
    monkeypatch.setattr(templates, "resolve_for_spec", no_template)
    monkeypatch.setattr(preview, "_theme", plain_theme)
    shot = VideoClip(
        id=uuid.UUID(SHOT_ID),
        provider="gemini",
        model="veo-3",
        prompt="A dark globe",
        params={},
        status=JOB_READY,
        file_id=uuid.uuid4(),
        duration_ms=4000,
        width=1280,
        height=720,
        has_audio=True,
    )

    class _DB:
        async def get(self, model, key):
            return shot if model is VideoClip and str(key) == SHOT_ID else None

    user = type("U", (), {"id": uuid.uuid4(), "role": "user"})()
    draft = _video(
        _shot(narration="Every campaign leaves traces."),
        _upload(narration="Our customers say it best."),
        music={"track": "deep-focus"},
    )
    project = VideoProject(id=uuid.uuid4(), user_id=user.id, title="Sound", spec=draft, revision=1)
    result = asyncio.run(preview.build_preview(_DB(), project, user, draft))
    scenes = {scene["id"]: scene for scene in result["document"]["scenes"]}
    assert (
        scenes["shot"]["preview_sound"] == {"plays": False, "reason": "music"} and "preview_audio" not in scenes["shot"]
    )
    assert scenes["film"]["preview_sound"] == {"plays": True, "reason": "auto"}
    duck = scenes["film"]["preview_audio"]["duck"]
    assert min(gain for _t, gain in duck) == pytest.approx(10 ** (-encoder.CLIP_DUCK_DB / 20), abs=1e-3)


# ── With ffmpeg: the dip under the voice, measured ────────────────────────

needs_ffmpeg = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed here")


def _tone(path: Path, *, seconds: float, amplitude: float, rate: int = 48_000) -> None:
    t = np.arange(int(seconds * rate)) / rate
    data = amplitude * np.sin(2 * np.pi * 500 * t)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((data * 32767).astype("<i2").tobytes())


def _rms_db(samples: np.ndarray, rate: int, start: float, end: float) -> float:
    window = samples[int(start * rate) : int(end * rate)]
    return 20 * np.log10(np.sqrt(np.mean(window**2)) + 1e-12)


@needs_ffmpeg
def test_a_clip_sound_drops_by_the_duck_depth_while_someone_speaks(tmp_path):
    clip = tmp_path / "clip.wav"
    _tone(clip, seconds=8.0, amplitude=0.3)
    loudness = asyncio.run(encoder.measure_loudness(str(clip), start=1.0, seconds=6.0))
    assert loudness is not None and encoder.clip_gain(loudness) < 0
    track = encoder.ClipTrack(str(clip), 2.0, 1.0, 6.0, windows=((4.5, 6.0),))
    inputs, graph = encoder.build_mix_filter(duration=10.0, voice=None, clips=[track], music=None)
    out = tmp_path / "mix.wav"
    args = [arg for path in inputs for arg in ("-i", path)]
    asyncio.run(
        encoder.ffmpeg([*args, "-filter_complex", graph, "-map", "[mix]", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    )
    with wave.open(str(out), "rb") as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32767
    alone = _rms_db(samples, rate, 2.6, 3.9)
    under = _rms_db(samples, rate, 4.6, 5.9)
    assert alone - under == pytest.approx(encoder.CLIP_DUCK_DB, abs=1.0)
    assert _rms_db(samples, rate, 0.2, 1.8) < alone - 40
