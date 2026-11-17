"""A scene preview's audio check grades its own narration alone. Synthetic audio only.

A scene preview reported "8% of the narration's words heard wrong" where the full render reported 1% on the same,
unchanged narration: the preview's MP4 includes its transitions, its neighbours' narrations play there, and the part
transcribed around its own narration reached into theirs - their words were graded against its script.

Three narrated scenes, crossfaded, their takes trimmed tight and placed back to back as the stem may place them;
each word a tone of its own and a speech-to-text that hears what it is sent - a fragment of a word included - and
mishears one word of the middle narration:

* transcribed as it plays, the part around the middle narration hears its neighbours' edge words;
* the preview of the middle scene grades its own narration alone, at the word error rate the full render gives that
  scene, and both hear the one word misheard.
"""

from __future__ import annotations

import asyncio
import io
import wave
from types import SimpleNamespace

import numpy as np

from app.llm.transcription import TranscribedWord, Transcript
from app.services.video_studio import audio_qc, encoder

RATE = audio_qc.ANALYSIS_RATE
WORD_SECONDS = 0.22
WORD_GAP = 0.06
#: Between two takes: back to back, a TTS take trimmed to its first and last word.
TAKE_GAP = 0.02
#: The shortest sound the speech-to-text makes a word of: a fragment of a word at the edge of a part is one.
HEARD_SECONDS = 0.06
SAID = {
    "s1": "every alert is triaged first by the platform before anyone looks",
    "s2": "analysts then open only what matters and close the rest quickly with one click and a short reason for the record",
    "s3": "reports follow every evening without a single manual step",
}
VOCABULARY = sorted({word for text in SAID.values() for word in text.split()})
#: The word of the middle narration the speech-to-text hears as another.
MISHEARD = {"quickly": "quietly"}


def _frequency(word: str) -> float:
    return 300.0 + 45.0 * VOCABULARY.index(word)


def _take(text: str) -> np.ndarray:
    """*text* said one tone a word, trimmed to its first and last word."""
    t = np.arange(round(WORD_SECONDS * RATE)) / RATE
    ramp = np.minimum(1.0, np.minimum(t, WORD_SECONDS - t) / 0.01)
    gap = np.zeros(round(WORD_GAP * RATE), np.float32)
    parts = []
    for index, word in enumerate(text.split()):
        if index:
            parts.append(gap)
        parts.append((0.4 * np.sin(2 * np.pi * _frequency(word) * t) * ramp).astype(np.float32))
    return np.concatenate(parts)


def _stem() -> tuple[np.ndarray, dict[str, tuple[float, float]]]:
    """The three takes on one stem, back to back from 0.5 s: the samples and each narration's span."""
    spans, at, takes = {}, 0.5, []
    for scene, text in SAID.items():
        take = _take(text)
        spans[scene] = (at, at + len(take) / RATE)
        takes.append((at, take))
        at += len(take) / RATE + TAKE_GAP
    voice = np.zeros(round((at + 1.0) * RATE), np.float32)
    for start, take in takes:
        voice[round(start * RATE) : round(start * RATE) + len(take)] = take
    return voice, spans


class _Hearing:
    """A speech-to-text that hears what it is sent: each run of tone a word, a fragment of one included."""

    model = "tone-stt"
    provider = "OpenAI (Production)"

    def heard(self, samples: np.ndarray) -> list[tuple[str, float, float]]:
        frame = round(0.005 * RATE)
        loud = np.array([np.sqrt(np.mean(samples[i : i + frame] ** 2)) > 0.02 for i in range(0, len(samples) - frame + 1, frame)])
        words, start = [], None
        for index, on in enumerate([*loud, False]):
            if on and start is None:
                start = index
            elif not on and start is not None:
                first, last = start * frame, index * frame
                start = None
                if (last - first) / RATE < HEARD_SECONDS:
                    continue
                chunk = samples[first:last]
                spectrum = np.abs(np.fft.rfft(chunk * np.hanning(len(chunk)), n=1 << 15))
                peak = np.fft.rfftfreq(1 << 15, 1.0 / RATE)[int(np.argmax(spectrum))]
                word = min(VOCABULARY, key=lambda w: abs(_frequency(w) - peak))
                words.append((MISHEARD.get(word, word), first / RATE, last / RATE))
        return words

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        with wave.open(io.BytesIO(audio)) as read:
            samples = np.frombuffer(read.readframes(read.getnframes()), "<i2").astype(np.float32) / 32767.0
        said = self.heard(samples)
        return Transcript(" ".join(w for w, _s, _e in said), tuple(TranscribedWord(w, s, e) for w, s, e in said))


def _check(monkeypatch, tmp_path, voice: np.ndarray, *, span: tuple[float, float] | None, scene_id: str | None, spans: dict):
    """The audio check of the whole render (*span* ``None``) or of a scene preview cut to *span*, as ``check_render``
    asks it: the narrations it grades and the parts it transcribes (``audio_qc.graded_narrations``)."""
    cut = voice if span is None else voice[round(span[0] * RATE) : round((span[0] + span[1]) * RATE)]

    async def demux(_video, _out):
        return SimpleNamespace(duration=len(cut) / RATE, has_audio=True)

    async def decode(path, *, rate):
        chosen = cut if path.endswith("voice-encoded.m4a") else voice if path == "stem.wav" else cut * 0.8
        return chosen.astype("<f4").tobytes()

    async def encode(_stem, _out, *, bitrate, span):
        return None

    monkeypatch.setattr(encoder, "demux_audio", demux)
    monkeypatch.setattr(encoder, "decode_mono", decode)
    monkeypatch.setattr(encoder, "encode_voice_like_mix", encode)
    texts = {scene: text.capitalize() + "." for scene, text in SAID.items()}
    narrations = audio_qc.graded_narrations(spans, texts, offset=span[0] if span else 0.0, scene_id=scene_id)
    return asyncio.run(
        audio_qc.check_audio(
            final_path="final.mp4",
            stem_path="stem.wav",
            narrations=narrations,
            transcriber=_Hearing(),
            unchecked_reason=None,
            scene_of=lambda at: None,
            workdir=str(tmp_path),
            span=span,
            known={},
        )
    )


def test_the_part_around_a_narration_hears_its_neighbours_as_it_plays():
    voice, spans = _stem()
    start, end = spans["s2"]
    part = voice[round((start - 0.1) * RATE) : round((end + 0.1) * RATE)]
    heard = [word for word, _s, _e in _Hearing().heard(part)]
    # The last word before it and the first after it, both other narrations' own.
    assert heard[0] == SAID["s1"].split()[-1] and heard[-1] == SAID["s3"].split()[0]
    assert heard[1:-1] == [MISHEARD.get(word, word) for word in SAID["s2"].split()]


def test_a_scene_preview_grades_its_own_narration_at_the_full_renders_word_error_rate(monkeypatch, tmp_path):
    voice, spans = _stem()
    whole = _check(monkeypatch, tmp_path, voice, span=None, scene_id=None, spans=spans)
    by_scene = {scene["scene_id"]: scene["wer"] for scene in whole.summary["scenes"]}
    assert by_scene["s1"] == by_scene["s3"] == 0.0
    # One word of twenty-one heard as another: the full render's word error rate for the middle scene.
    assert by_scene["s2"] == round(1 / len(SAID["s2"].split()), 4)
    # Its preview, from the start of its transition in (the first narration still speaking) to past the third's start.
    start, end = spans["s2"]
    span = (start - 1.0, end - start + 2.0)
    preview = _check(monkeypatch, tmp_path, voice, span=span, scene_id="s2", spans=spans)
    assert [scene["scene_id"] for scene in preview.summary["scenes"]] == ["s2"]
    assert abs(preview.summary["wer"] - by_scene["s2"]) <= 0.01, (preview.summary["wer"], by_scene["s2"])
    assert preview.summary["scenes"][0]["wer"] == by_scene["s2"] and preview.summary["transcribed_parts"] == 1
