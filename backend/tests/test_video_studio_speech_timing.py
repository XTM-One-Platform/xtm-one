"""When each word of a narration is said: measured on its voice, else paced.

* paced: each word its share of letters over the span, a beat at each break;
* measured: the words laid over where someone speaks, each clause break put
  on the pause that matches it - a clause spoken slower or faster than its
  letters say still starts where the voice starts it;
* the WAV files the voice chain writes (32-bit float) and 16-bit PCM are read;
  a file that cannot be read falls back to the paced words.
"""

from __future__ import annotations

import asyncio
import itertools
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from app.services.video_studio import speech_timing

RATE = 48000


def _speech(*parts: tuple[str, float]) -> np.ndarray:
    """``("voice" | "pause", seconds)`` as samples: a voiced stretch is a tone with a little syllable rhythm."""
    chunks = []
    for kind, seconds in parts:
        t = np.arange(int(seconds * RATE)) / RATE
        if kind == "voice":
            chunks.append(0.3 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t) ** 2))
        else:
            chunks.append(np.zeros_like(t))
    return np.concatenate(chunks).astype(np.float32)


def _float_wav(path: Path, samples: np.ndarray, channels: int = 2) -> None:
    data = np.repeat(samples[:, None], channels, axis=1).astype("<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, channels, RATE, RATE * 4 * channels, 4 * channels, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"LIST" + struct.pack("<I", 4) + b"INFO"
    body += b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_the_words_of_a_narration_are_read_as_a_label_is():
    assert speech_timing.tokens("XTM One, then OpenCTI's 6,000 users.") == ["xtm", "one", "then", "opencti's", "6,000", "users"]
    assert speech_timing.tokens("") == []


def test_paced_words_share_the_span_by_their_letters():
    words = speech_timing.paced_words("Go collect everything.", 2.0, 3.0)
    assert [w.text for w in words] == ["go", "collect", "everything"]
    assert words[0].start == pytest.approx(2.0) and all(a.end <= b.start + 1e-9 for a, b in itertools.pairwise(words))
    assert (words[2].end - words[2].start) > (words[1].end - words[1].start) > (words[0].end - words[0].start)
    assert words[-1].end <= 5.0 + 1e-9
    assert speech_timing.paced_words("", 0.0, 3.0) == []


def test_a_pause_shorter_than_a_breath_is_inside_the_speech():
    samples = _speech(("voice", 0.6), ("pause", 0.05), ("voice", 0.6), ("pause", 0.4), ("voice", 0.5))
    runs = speech_timing.voiced_runs(samples, RATE)
    assert len(runs) == 2
    assert runs[0][0] == pytest.approx(0.0, abs=0.02) and runs[0][1] == pytest.approx(1.25, abs=0.02)
    assert runs[1][0] == pytest.approx(1.65, abs=0.02)


def test_each_clause_starts_where_the_voice_starts_it():
    # The middle clause is said twice as slowly as its letters would have it: paced, its words drift.
    text = "Alpha bravo charlie, delta echo, foxtrot golf hotel."
    samples = _speech(("pause", 0.2), ("voice", 1.2), ("pause", 0.45), ("voice", 1.8), ("pause", 0.45), ("voice", 1.2))
    words = speech_timing.measured_words(text, speech_timing.voiced_runs(samples, RATE), start=10.0)
    at = {w.text: w.start for w in words}
    assert at["alpha"] == pytest.approx(10.2, abs=0.03)
    assert at["delta"] == pytest.approx(10.2 + 1.2 + 0.45, abs=0.03)
    assert at["foxtrot"] == pytest.approx(10.2 + 1.2 + 0.45 + 1.8 + 0.45, abs=0.03)
    # Inside a clause the words follow in order, never in a pause.
    assert [w.text for w in words] == speech_timing.tokens(text)
    assert all(a.start < b.start for a, b in itertools.pairwise(words))
    paced = {w.text: w.start for w in speech_timing.paced_words(text, 10.0, 5.3)}
    assert abs(paced["foxtrot"] - at["foxtrot"]) > 0.3, "the measured times are not the paced ones"


def test_a_breath_holding_no_clause_break_is_skipped_by_the_words():
    text = "Alpha bravo charlie delta."
    samples = _speech(("voice", 1.0), ("pause", 0.3), ("voice", 1.0))
    words = speech_timing.measured_words(text, speech_timing.voiced_runs(samples, RATE))
    assert all(not (1.0 + 0.01 < w.start < 1.3 - 0.01) for w in words), "no word starts inside the breath"
    assert words[-1].end == pytest.approx(2.3, abs=0.03)


def test_the_voice_chains_wav_and_16_bit_pcm_are_read(tmp_path):
    samples = _speech(("voice", 0.5), ("pause", 0.3), ("voice", 0.5))
    floats = tmp_path / "voice.wav"
    _float_wav(floats, samples)
    read, rate = speech_timing.read_wav(floats)
    assert rate == RATE and read.shape == samples.shape and np.allclose(read, samples, atol=1e-6)
    pcm = tmp_path / "pcm.wav"
    with wave.open(str(pcm), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((samples * 32767).astype("<i2").tobytes())
    read, rate = speech_timing.read_wav(pcm)
    assert rate == RATE and np.allclose(read, samples, atol=1e-3)
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav")
    with pytest.raises(ValueError):
        speech_timing.read_wav(junk)


def test_word_times_reads_the_voice_and_paces_what_it_cannot_read(tmp_path):
    text = "Alpha bravo, charlie delta."
    voice = tmp_path / "voice.wav"
    _float_wav(voice, _speech(("voice", 0.8), ("pause", 0.5), ("voice", 0.8)))
    measured = asyncio.run(speech_timing.word_times(text, start=1.0, seconds=2.1, audio_path=str(voice)))
    assert {w.text: w.start for w in measured}["charlie"] == pytest.approx(1.0 + 0.8 + 0.5, abs=0.03)
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"RIFF....WAVEjunk")
    paced = speech_timing.paced_words(text, 1.0, 2.1)
    assert asyncio.run(speech_timing.word_times(text, start=1.0, seconds=2.1, audio_path=str(broken))) == paced
    assert asyncio.run(speech_timing.word_times(text, start=1.0, seconds=2.1)) == paced
