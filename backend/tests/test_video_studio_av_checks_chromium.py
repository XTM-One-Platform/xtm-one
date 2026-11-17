"""A 3-scene synthetic video rendered for real and heard and watched back.

The render's own steps, in Chromium and ffmpeg, on synthetic media only: a
title, a test-pattern clip shorter than its narration and a statement, each
narrated by tone-coded speech (each word a harmonic tone of its own pitch,
spoken at 24 kHz so the stitcher resamples it) through the voice chain, a
built-in score under it. The planner, the compositor, the voice stem, the
mix and the mux run as a render runs them, then ``render.check_render``: a
speech-to-text that decodes the tones back into words hears the stem.

* with the default ``extend_mode`` (``zoom``: the clip slowed to fill its
  scene, then drifting in on its last frame), every check is clean;
* with ``extend_mode: hold`` the clip's held frame is found frozen, in its
  scene, at its time - the detector on a real render, and why zoom is the
  default.
"""

from __future__ import annotations

import io
import os
import re
import struct
import wave

import numpy as np
import pytest

from app.llm.transcription import TranscribedWord, Transcript
from app.services.video_studio import audio_qc, compositor, encoder, render, score, timeline, voice, voice_check
from app.services.video_studio.spec import QUALITY_PRESETS, parse_video_spec, validate_video_spec
from app.services.video_studio.speech_timing import read_wav
from app.services.video_studio.themes import build_theme
from app.services.video_studio.voice_check import PASSED, SpokenSegment

pytestmark = pytest.mark.chromium

NARRATIONS = {
    "open": "Threat intelligence for every analyst.",
    "shot": "Agents triage alerts, enrich indicators and open cases while people sleep soundly tonight.",
    "claim": "One platform protects everything you run.",
}
VOCABULARY = sorted({word for text in NARRATIONS.values() for word in re.findall(r"\w+", text.lower())})
SPEECH_RATE = 24000
#: A word and the breath after it, at a spoken pace: every narration here reads at 155 to 170 words a minute,
#: inside the mix check's ``SLOW_WPM`` / ``FAST_WPM``, so a clean render stays clean. A word is one
#: steady tone: held for ``STUTTER_TIMES`` x the shortest stutter chunk (3 x 80 ms) or longer, its middle is
#: the same sound heard three times in a row and ``audio_qc.find_stutters`` rightly calls it a stutter, so the
#: pace comes from the breath, never from a longer word.
WORD_SECONDS, GAP_SECONDS = 0.20, 0.20
assert audio_qc.STUTTER_TIMES * audio_qc.STUTTER_SECONDS[0] > WORD_SECONDS


def _pitch(word: str) -> float:
    return 260.0 * 1.075 ** VOCABULARY.index(word)


def _speak(text: str, path) -> None:
    """Tone-coded speech: each word a harmonic tone of its own pitch under a soft envelope, then a breath."""
    t = np.arange(round(WORD_SECONDS * SPEECH_RATE)) / SPEECH_RATE
    ramp = np.minimum(1.0, np.minimum(t, WORD_SECONDS - t) / 0.02)
    parts = [np.zeros(round(0.05 * SPEECH_RATE))]
    for word in re.findall(r"\w+", text.lower()):
        f = _pitch(word)
        tone = np.sin(2 * np.pi * f * t) + 0.5 * np.sin(4 * np.pi * f * t) + 0.25 * np.sin(6 * np.pi * f * t)
        parts += [0.25 * tone * ramp, np.zeros(round(GAP_SECONDS * SPEECH_RATE))]
    samples = np.concatenate(parts)
    pcm = (samples * 32767).astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SPEECH_RATE, SPEECH_RATE * 2, 2, 16)
    path.write_bytes(header + b"data" + struct.pack("<I", len(pcm)) + pcm)


class _ToneTranscriber:
    """A speech-to-text for tone-coded speech: each burst of sound back to the word of its pitch."""

    model = "tone-decoder"
    provider = "OpenAI"

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        self.calls += 1
        with wave.open(io.BytesIO(audio)) as source:
            rate = source.getframerate()
            samples = np.frombuffer(source.readframes(source.getnframes()), "<i2").astype(np.float64) / 32767
        hop = rate // 100
        frames = samples[: len(samples) // hop * hop].reshape(-1, hop)
        level = 20 * np.log10(np.sqrt((frames**2).mean(axis=1)) + 1e-9)
        loud = level > level.max() - 25
        found, start = [], None
        for i, on in enumerate([*loud, False]):
            if on and start is None:
                start = i
            elif not on and start is not None:
                if i - start >= 8:
                    middle = samples[(start + 2) * hop : (i - 2) * hop]
                    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle)), 1 << 15))
                    freqs = np.fft.rfftfreq(1 << 15, 1 / rate)
                    peak = freqs[np.argmax(spectrum * (freqs > 150))]
                    word = min(VOCABULARY, key=lambda w: abs(np.log(_pitch(w) / peak)))
                    found.append(TranscribedWord(word, start / 100, i / 100))
                start = None
        return Transcript(" ".join(w.text for w in found), tuple(found))


def _spec(extend_mode: str) -> object:
    return parse_video_spec(
        validate_video_spec(
            {
                "title": "Synthetic checks",
                "freestyle": True,
                "music": {"track": "deep-focus", "volume": 0.35},
                # A burned-in caption turning its page inside the held clip would cut one freeze in two: the
                # captions stay a WebVTT file here, the picture is the clip's alone.
                "captions": {"burn_in": False},
                "scenes": [
                    {"id": "open", "type": "title", "title": "Checks", "duration": 3, "narration": NARRATIONS["open"]},
                    {
                        "id": "shot",
                        "type": "clip",
                        "file_id": "00000000-0000-0000-0000-0000000000f1",
                        "narration": NARRATIONS["shot"],
                        "extend_mode": extend_mode,
                        "transition": {"type": "crossfade", "duration": 0.5},
                    },
                    {
                        "id": "claim",
                        "type": "statement",
                        "lines": ["One platform"],
                        "duration": 3,
                        "narration": NARRATIONS["claim"],
                        "transition": {"type": "crossfade", "duration": 0.5},
                    },
                ],
            }
        )
    )


async def _render(tmp_path, extend_mode: str, *, heard_back: bool = False):
    """The synthetic video rendered and checked; *heard_back* hears each narration back first, as a render does."""
    from app.services.builtin_tools.html_render import font_face_css

    spec = _spec(extend_mode)
    clip = tmp_path / "clip.mp4"
    await encoder.ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24", "-t", "2", "-pix_fmt", "yuv420p", str(clip)])
    info = await encoder.probe(str(clip))
    media = render._Media()
    media.plan.clips["shot"] = timeline.ClipMedia(
        duration=info.duration, width=info.width, height=info.height, fps=24.0, frame_count=info.frame_count
    )
    media.assets.sources["clip/shot"] = compositor.FrameSource(str(clip), 24.0)
    media.lines = voice.lines_of(spec)
    media.transcriber = _ToneTranscriber()
    for line in media.lines:
        raw, placed = tmp_path / f"raw-{line.scene_id}.wav", tmp_path / f"voice-{line.scene_id}.wav"
        _speak(line.text, raw)
        await encoder.condition_voice(str(raw), str(placed))
        samples, rate = read_wav(str(placed))
        media.plan.voices[line.scene_id] = timeline.VoiceMedia(len(samples) / rate)
        media.voice_paths[line.scene_id] = str(placed)
        attempts = []
        if heard_back:
            heard = await voice_check.transcribe_wav(media.transcriber, str(placed), words=True)
            take = voice_check.Attempt("tone", "same", str(placed), len(samples) / rate)
            take.check, take.words = voice_check.compare(line.text, heard.text), voice_check._kept_words(heard.words)
            take.heard_digest = voice_check.take_digest(str(placed))
            attempts.append(take)
        media.spoken.append(SpokenSegment(line.scene_id, str(placed), len(samples) / rate, PASSED, attempts))
    plan = timeline.plan(spec, media.plan, fps=30, theme=build_theme(mode="dark", background_style="grid"))
    shot = plan.document["scenes"][1]
    # ``zoom`` is left unmarked (the page drifts on what holds); ``hold`` turns the drift off.
    assert shot.get("extend") == (None if extend_mode == "zoom" else {"mode": extend_mode})
    assert shot["time_map"][-1][0] > shot["time_map"][1][0]
    try:
        video = await compositor.render_frames(
            plan.document,
            media.assets,
            preset=QUALITY_PRESETS["draft"],
            output_size=(640, 360),
            workdir=str(tmp_path),
            font_css=font_face_css(),
            pages=2,
        )
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    stem = await render.build_stem(plan, media.voice_paths, tmp_path)
    source, bed = tmp_path / "score.wav", tmp_path / "bed.wav"
    score.render_score("deep-focus", plan.duration, plan.cues(), str(source))
    await encoder.prepare_bed(str(source), str(bed), duration=plan.duration)
    audio = tmp_path / "audio.m4a"
    await encoder.mix_audio(
        out_path=str(audio),
        duration=plan.duration,
        voice=encoder.VoiceTrack(stem.path),
        clips=[],
        music=encoder.MusicTrack(str(bed), 0.35, 10.0, tuple(plan.duck_windows(voiced=set(media.voice_paths)))),
    )
    final = tmp_path / "final.mp4"
    await encoder.mux(video_path=video, audio_path=str(audio), out_path=str(final))
    checks = await render.check_render(
        final_path=str(final), stem=stem, plan=plan, media=media, size=(640, 360), fps=30, workdir=tmp_path
    )
    return plan, checks


@pytest.fixture
def needs_ffmpeg():
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")


async def test_a_three_scene_video_renders_with_clean_checks(tmp_path, needs_ffmpeg):
    plan, checks = await _render(tmp_path, "zoom")
    stored = checks.as_dict()
    assert stored["findings"] == [], stored["findings"]
    assert stored["status"] == "passed"
    assert stored["audio"]["status"] == "passed" and stored["audio"]["transcribed"]
    assert stored["audio"]["wer"] == 0.0 and stored["audio"]["stutters"] == 0
    assert stored["audio"]["final_audio"]["seconds"] == pytest.approx(plan.duration, abs=0.1)
    assert stored["frames"]["status"] == "passed" and stored["frames"]["frames"] >= int(plan.duration * 30) - 2


async def test_narrations_heard_back_are_not_transcribed_again_from_the_stem(tmp_path, needs_ffmpeg):
    """A clean render of N narrations makes N transcriptions, not 2N - and still hears every word in place."""
    _plan, checks = await _render(tmp_path, "zoom", heard_back=True)
    stored = checks.as_dict()
    assert stored["findings"] == [], stored["findings"]
    assert stored["audio"]["wer"] == 0.0 and stored["audio"]["transcribed_parts"] == 0
    assert stored["audio"]["reused_transcripts"] == len(NARRATIONS)
    assert (stored["audio"]["model"], stored["audio"]["provider"]) == ("tone-decoder", "OpenAI")


async def test_a_held_clip_is_found_frozen_in_its_scene(tmp_path, needs_ffmpeg):
    plan, checks = await _render(tmp_path, "hold")
    frozen = [f for f in checks.findings if f.kind == "frozen"]
    shot = next(s for s in plan.document["scenes"] if s["id"] == "shot")
    # One run, whatever keyframe the encoder puts inside the hold (frame_freeze.BLIP_SECONDS).
    assert len(frozen) == 1 and frozen[0].scene_id == "shot", frozen
    # Frozen once the 2 s clip has ended in its scene (no caption is burned in to cut it), for over half a
    # second, never in the cards.
    clip_end = float(shot["start"]) + 2.0
    assert clip_end - 0.05 <= frozen[0].at < float(shot["end"]) and frozen[0].end <= float(shot["end"]) + 0.05
    assert frozen[0].end - frozen[0].at > 0.5
    assert all(f.kind == "frozen" for f in checks.findings) and checks.status == "findings"
