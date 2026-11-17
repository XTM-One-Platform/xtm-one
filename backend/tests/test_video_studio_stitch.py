"""The voice stem, on synthetic audio only.

* two narrations stitched across a scene boundary: no discontinuity (the
  largest step from one sample to the next stays within the signal's own)
  and no duplicated frame (the correlation of a span with the one before it,
  at 50-300 ms lags, stays low) - each measure proven to see the fault it
  looks for on a stem built wrong on purpose;
* every segment is resampled once, whole, by the windowed-sinc resampler;
  one already at the mix rate is not touched;
* placement is sample-exact, every edge fades over 10 ms, two narrations
  never overlap (refused, never cut or crossfaded);
* the stem is a float WAV the platform reads back.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from app.services.video_studio import stitch
from app.services.video_studio.speech_timing import read_wav

RATE = stitch.MIX_RATE


def _speech_like(seconds: float, rate: int, seed: int) -> np.ndarray:
    """Noise shaped like speech: a band of noise under a syllable envelope (4 Hz), never periodic."""
    rng = np.random.default_rng(seed)
    n = round(seconds * rate)
    noise = rng.standard_normal(n)
    spectrum = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    spectrum[(freqs < 150) | (freqs > 4000)] = 0
    band = np.fft.irfft(spectrum, n)
    t = np.arange(n) / rate
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 4.1 * t + rng.uniform(0, np.pi)) ** 2
    signal = band * envelope
    return (0.4 * signal / np.max(np.abs(signal))).astype(np.float32)


def _write(path, samples: np.ndarray, rate: int) -> str:
    """A 16-bit PCM mono WAV of *samples*."""
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    path.write_bytes(header + b"data" + struct.pack("<I", len(pcm)) + pcm)
    return str(path)


def _two_segments(tmp_path, rate: int = RATE):
    first = _speech_like(1.2, rate, seed=1)
    second = _speech_like(1.5, rate, seed=2)
    return first, second, _write(tmp_path / "a.wav", first, rate), _write(tmp_path / "b.wav", second, rate)


def test_two_narrations_across_a_scene_boundary_join_without_a_jump_or_a_repeat(tmp_path):
    first, _second, a, b = _two_segments(tmp_path)
    # Scene 1's narration from 0.30 s; scene 2 starts where it ends, its narration on the next sample.
    boundary = 0.30 + len(first) / RATE
    stem = stitch.stitch(
        [stitch.Segment("s1", a, 0.30), stitch.Segment("s2", b, boundary)], duration=4.0, out_path=str(tmp_path / "stem.wav")
    )
    samples, rate = read_wav(stem.path)
    assert rate == RATE and len(samples) == stem.length == round(4.0 * RATE)
    edge = round(boundary * RATE)
    window = samples[edge - round(0.05 * RATE) : edge + round(0.05 * RATE)]
    inside = max(stitch.max_sample_jump(samples[round(0.5 * RATE) : round(1.2 * RATE)]), 1e-6)
    # No discontinuity: the join steps no more than the speech itself does.
    assert stitch.max_sample_jump(window) <= inside
    # No duplicated frame: nothing around the join repeats the span before it at 50-300 ms.
    correlation, _lag = stitch.repeated_span(samples, rate, around=boundary)
    assert correlation < 0.5


def test_the_measures_see_the_faults_they_look_for(tmp_path):
    first, second, _a, _b = _two_segments(tmp_path)
    # A hard cut at full level: the step a stitch without fades makes.
    spliced = np.concatenate([first[:-200], np.full(200, 0.35, np.float32), -0.35 * np.ones(200, np.float32), second])
    assert stitch.max_sample_jump(spliced) > 2 * stitch.max_sample_jump(first)
    # 120 ms of the first narration written twice at the boundary: a duplicated frame.
    chunk = first[-round(0.12 * RATE) :]
    doubled = np.concatenate([first, chunk, second])
    correlation, lag = stitch.repeated_span(doubled, RATE, around=len(first) / RATE)
    assert correlation > 0.95 and lag == pytest.approx(0.12, abs=0.005)


def test_a_segment_is_resampled_once_whole_and_placed_on_its_sample(tmp_path, monkeypatch):
    first, second, a, b = _two_segments(tmp_path, rate=24000)
    calls = []
    real = stitch.resample

    def counted(samples, source_rate, rate=stitch.MIX_RATE):
        calls.append((len(samples), source_rate, rate))
        return real(samples, source_rate, rate)

    monkeypatch.setattr(stitch, "resample", counted)
    stem = stitch.stitch(
        [stitch.Segment("s2", b, 2.0), stitch.Segment("s1", a, 0.25)], duration=4.0, out_path=str(tmp_path / "stem.wav")
    )
    # One call per segment, each with every one of its samples: never a chunk at a time.
    assert sorted(calls) == sorted([(len(first), 24000, RATE), (len(second), 24000, RATE)])
    s1, s2 = stem.placements
    assert (s1.scene_id, s1.first, s1.resampled) == ("s1", round(0.25 * RATE), True)
    assert s1.end - s1.first == 2 * len(first) and s2.first == 2 * RATE
    assert stem.spans()["s2"] == pytest.approx((2.0, 2.0 + 2 * len(second) / RATE))


def test_a_segment_at_the_mix_rate_is_not_resampled(tmp_path, monkeypatch):
    _first, _second, a, _b = _two_segments(tmp_path)
    original = stitch.resample
    seen = []
    monkeypatch.setattr(stitch, "resample", lambda s, r, rate=RATE: seen.append(r) or original(s, r, rate))
    stem = stitch.stitch([stitch.Segment("s1", a, 0.0)], duration=2.0, out_path=str(tmp_path / "stem.wav"))
    assert seen == [RATE] and stem.placements[0].resampled is False


def test_the_resampler_is_a_windowed_sinc_that_keeps_the_band(tmp_path):
    rate = 24000
    t = np.arange(rate) / rate
    tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    up = stitch.resample(tone, rate, RATE)
    assert len(up) == 2 * len(tone)
    reference = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(len(up)) / RATE)
    middle = slice(2000, len(up) - 2000)
    assert np.max(np.abs(up[middle] - reference[middle])) < 1e-3


def test_every_edge_fades_over_ten_milliseconds(tmp_path):
    loud = np.full(RATE // 2, 0.5, np.float32)
    path = _write(tmp_path / "flat.wav", loud, RATE)
    stem = stitch.stitch([stitch.Segment("s1", path, 1.0)], duration=2.0, out_path=str(tmp_path / "stem.wav"))
    samples, _rate = read_wav(stem.path)
    start, n = RATE, round(stitch.EDGE_FADE_SECONDS * RATE)
    assert abs(samples[start]) < 0.01 and samples[start + n] == pytest.approx(0.5, abs=0.001)
    end = start + len(loud)
    assert abs(samples[end - 1]) < 0.01 and samples[end - n - 1] == pytest.approx(0.5, abs=0.001)
    assert stitch.max_sample_jump(samples) < 0.5 * np.pi / (2 * n) + 1e-3


def test_two_narrations_never_overlap_they_are_refused(tmp_path):
    first, _second, a, b = _two_segments(tmp_path)
    overlap_start = 0.30 + len(first) / RATE - 0.08
    with pytest.raises(stitch.StitchError, match="80 ms before scene s1's ends"):
        stitch.stitch(
            [stitch.Segment("s1", a, 0.30), stitch.Segment("s2", b, overlap_start)],
            duration=4.0,
            out_path=str(tmp_path / "stem.wav"),
        )


def test_the_stem_is_a_float_wav_with_its_sizes_written(tmp_path):
    _first, _second, a, _b = _two_segments(tmp_path)
    stem = stitch.stitch([stitch.Segment("s1", a, 0.5)], duration=3.0, out_path=str(tmp_path / "stem.wav"))
    data = (tmp_path / "stem.wav").read_bytes()
    assert data[:4] == b"RIFF" and struct.unpack("<I", data[4:8])[0] == len(data) - 8
    tag, channels, rate = struct.unpack("<HHI", data[20:28])
    assert (tag, channels, rate) == (3, 1, RATE)
    assert stem.duration == pytest.approx(3.0)
