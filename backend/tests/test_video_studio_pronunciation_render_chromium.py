"""A 3-scene video narrating lexicon terms, rendered for real and its pronunciation heard back.

The render's own steps on synthetic media only, as
``test_video_studio_av_checks_chromium`` runs them. Each narration is spoken
through the real voice pipeline - the seeded lexicon rendered for a model that
reads plain text, the voice cache, the segment check - by a synthetic speech
model saying what it is sent as tone-coded speech (each word a harmonic tone of
its own pitch), which a tone-decoding speech-to-text hears back. Then the
planner, the compositor in Chromium, the voice stem, the mix, the mux and
``render.check_render``, which hears the stem through the final mix's encode.

The narrations say Filigran, CTEM, XTM One and OpenCTI: every narration passes
its check, the render is clean, and its ``pronunciation`` section has every
occurrence passed.
"""

from __future__ import annotations

import io
import os
import re
import struct
import wave

import numpy as np
import pytest

from app.llm.router import SpeechTarget
from app.llm.speech import PronunciationSupport, SpeechResult
from app.llm.transcription import TranscribedWord, Transcript
from app.services.video_studio import (
    compositor,
    encoder,
    pronunciation_check,
    render,
    render_checks,
    score,
    timeline,
    voice,
    voice_check,
)
from app.services.video_studio import lexicon as lex
from app.services.video_studio.spec import QUALITY_PRESETS, parse_video_spec, validate_video_spec
from app.services.video_studio.themes import build_theme
from tests.video_voice_doubles import in_memory_storage, speech_config

pytestmark = pytest.mark.chromium

NARRATIONS = {
    "open": "Filigran brings analysts together.",
    "loop": "Every CTEM program runs inside XTM One.",
    "claim": "OpenCTI connects the whole stack.",
}
LEXICON = lex.merge(lex.load_seed())
#: What a model reading plain text is sent, and a word it may be heard as instead: the vocabulary of the tones.
SENT = {scene: lex.render(text, LEXICON, PronunciationSupport()).text for scene, text in NARRATIONS.items()}
MISHEARD = "stem"
#: Words as written: a speech-to-text writes an acronym said as one word in capitals (``XTM``), as it was sent.
VOCABULARY = sorted({word for text in SENT.values() for word in re.findall(r"\w+", text)} | {MISHEARD})
SPEECH_RATE = 24000
#: A word and the breath after it, at a spoken pace: every narration here reads at 115 to 175 words a minute,
#: inside the mix check's ``SLOW_WPM`` / ``FAST_WPM``, so a clean render stays clean.
WORD_SECONDS, GAP_SECONDS = 0.28, 0.08


def _pitch(word: str) -> float:
    return 260.0 * 1.075 ** VOCABULARY.index(word)


def _tones(text: str) -> bytes:
    """Tone-coded speech of *text*: each word a harmonic tone of its own pitch under a soft envelope, then a breath."""
    t = np.arange(round(WORD_SECONDS * SPEECH_RATE)) / SPEECH_RATE
    ramp = np.minimum(1.0, np.minimum(t, WORD_SECONDS - t) / 0.02)
    parts = [np.zeros(round(0.05 * SPEECH_RATE))]
    for word in re.findall(r"\w+", text):
        f = _pitch(word)
        tone = np.sin(2 * np.pi * f * t) + 0.5 * np.sin(4 * np.pi * f * t) + 0.25 * np.sin(6 * np.pi * f * t)
        parts += [0.25 * tone * ramp, np.zeros(round(GAP_SECONDS * SPEECH_RATE))]
    pcm = (np.concatenate(parts) * 32767).astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SPEECH_RATE, SPEECH_RATE * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


class _ToneVoice:
    """A speech model reading plain text: it says what it is sent, as tones."""

    def speech_pronunciation(self, model: str) -> PronunciationSupport:
        return PronunciationSupport()

    async def synthesize_speech(self, request) -> SpeechResult:
        return SpeechResult(_tones(request.text), "audio/wav", "tone", [])


class _ToneTranscriber:
    """A speech-to-text for tone-coded speech: each burst of sound back to the word of its pitch."""

    model = "tone-decoder"

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
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


def _spec() -> object:
    return parse_video_spec(
        validate_video_spec(
            {
                "title": "Pronunciation",
                "freestyle": True,
                "music": {"track": "deep-focus", "volume": 0.35},
                "scenes": [
                    {"id": "open", "type": "title", "title": "Readings", "duration": 3, "narration": NARRATIONS["open"]},
                    {
                        "id": "loop",
                        "type": "statement",
                        "lines": ["One loop"],
                        "duration": 3,
                        "narration": NARRATIONS["loop"],
                        "transition": {"type": "crossfade", "duration": 0.5},
                    },
                    {
                        "id": "claim",
                        "type": "title",
                        "title": "One stack",
                        "duration": 3,
                        "narration": NARRATIONS["claim"],
                        "transition": {"type": "crossfade", "duration": 0.5},
                    },
                ],
            }
        )
    )


async def _render(tmp_path, monkeypatch, *, broken: dict[str, str] | None = None):
    """The video rendered; *broken* ``scene -> words``: that take, heard right on its own, is spoilt before the stem."""
    from app.services.builtin_tools.html_render import font_face_css

    in_memory_storage(monkeypatch)
    spec = _spec()
    target = SpeechTarget(provider=_ToneVoice(), model="tone-tts", config=speech_config("tone"), voice="tone")
    speaker = voice_check.Speaker(target, spec.voiceover, _ToneTranscriber(), None, lexicon=LEXICON)
    media = render._Media()
    media.lexicon, media.lines, media.transcriber = LEXICON, voice.lines_of(spec), speaker.transcriber
    for line in media.lines:
        spoken = await voice_check.speak_checked(speaker, line, workdir=str(tmp_path))
        if broken and line.scene_id in broken:
            raw = tmp_path / f"broken-{line.scene_id}.wav"
            raw.write_bytes(_tones(broken[line.scene_id]))
            await encoder.condition_voice(str(raw), spoken.path)
        media.spoken.append(spoken)
        media.plan.voices[line.scene_id] = timeline.VoiceMedia(voice_check._duration(spoken.path) or 0.0)
        media.voice_paths[line.scene_id] = spoken.path
    plan = timeline.plan(spec, media.plan, fps=30, theme=build_theme(mode="dark", background_style="grid"))
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
        bitrate="128k",
    )
    final = tmp_path / "final.mp4"
    await encoder.mux(video_path=video, audio_path=str(audio), out_path=str(final))
    checks = await render.check_render(
        final_path=str(final), stem=stem, plan=plan, media=media, size=(640, 360), fps=30, workdir=tmp_path, bitrate="128k"
    )
    return media, checks, plan


@pytest.fixture
def needs_ffmpeg():
    if not encoder.encoder_available():
        pytest.skip("ffmpeg is not installed")


async def test_a_three_scene_video_says_every_lexicon_term_as_the_lexicon_does(tmp_path, monkeypatch, needs_ffmpeg):
    media, checks, _plan = await _render(tmp_path, monkeypatch)
    assert [(s.scene_id, s.status) for s in media.spoken] == [("open", "passed"), ("loop", "passed"), ("claim", "passed")]
    assert {s.scene_id: s.placed.mechanism for s in media.spoken} == dict.fromkeys(NARRATIONS, "text_rewrite")
    # Its letters sent as one word, never hyphenated.
    assert media.spoken[1].placed.sent == "Every see-tem program runs inside XTM One."
    stored = checks.as_dict()
    assert stored["findings"] == [], stored["findings"]
    assert stored["status"] == "passed"
    assert stored["audio"]["heard"] == "encoded" and stored["audio"]["wer"] == 0.0
    assert stored["pronunciation"] == {
        "status": "passed",
        "terms": [
            {"term": "Filigran", "occurrences": 1, "passed": 1, "heard": ["Fil i gran"]},
            {"term": "CTEM", "occurrences": 1, "passed": 1, "heard": ["see tem"]},
            # Letters said as one word, heard as written.
            {"term": "XTM One", "occurrences": 1, "passed": 1, "heard": ["XTM One"]},
            {"term": "OpenCTI", "occurrences": 1, "passed": 1, "heard": ["OpenCTI"]},
        ],
    }
    said = render_checks.format_checks(stored)
    assert "Pronunciation: passed: Filigran 1 of 1 (heard 'Fil i gran'), CTEM 1 of 1" in said


async def test_a_term_right_on_its_own_and_broken_in_the_final_voice_is_caught_at_its_time(
    tmp_path, monkeypatch, needs_ffmpeg
):
    # The take of "loop" passed its own check; what reaches the stem says the wrong word.
    spoilt = SENT["loop"].replace("see-tem", MISHEARD)
    media, checks, plan = await _render(tmp_path, monkeypatch, broken={"loop": spoilt})
    assert all(s.status == "passed" for s in media.spoken)
    (finding,) = [f for f in checks.findings if f.kind == render_checks.PRONUNCIATION]
    assert finding.scene_id == "loop" and finding.message.startswith("brand term mispronounced: CTEM heard as 'stem' at ")
    assert checks.pronunciation["terms"][1] == {"term": "CTEM", "occurrences": 1, "passed": 0, "heard": ["stem"]}
    assert checks.pronunciation["status"] == "failed" and checks.failed
    # At its time in the MP4: inside the narration of its scene, as the message says it.
    start, end = plan.narration_spans["loop"]
    assert start <= finding.at <= end
    assert finding.message.endswith(f"at {pronunciation_check.clock(finding.at)}")
