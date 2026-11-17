"""What a render's mixed audio sounds like, measured: loudness, true peak, music fit, narration pace, silences.

* the measure: BS.1770 loudness per scene and of the whole MP4, its true peak, a
  scene preview timed from its MP4;
* the findings: music stopping more than 1 s before the video's end, a loop seam
  found as the bed's repeated segment, a narration under 110 or over 180 words a
  minute, a silent gap over 1.5 s in a narrated scene, a still picture over
  silence, a peak that clips - each a warning with its time and its scene;
* the music under the voice by less than 6 dB, a ``music_level`` finding at save
  and at render;
* where they are said: the audio check's ``mix`` section and the ``Mix`` line of a
  render's answer and of ``get_video``.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.video_studio import encoder, mix_check, music_level, render_checks

needs_ffmpeg = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed here")


def _blocks(level, seconds: float) -> list[tuple[float, float]]:
    """Momentary blocks of *seconds*: ``level(t)`` LUFS for the block centred on *t*, every 100 ms."""
    ends = [round(0.4 + k * 0.1, 3) for k in range(round((seconds - 0.4) / 0.1) + 1)]
    return [(end, float(level(end - 0.2))) for end in ends]


SCENES = [("intro", 0.0, 10.0), ("demo", 10.0, 20.0), ("outro", 20.0, 24.0)]


def _codes(report: mix_check.MixReport) -> list[str]:
    return [p.code for p in report.problems]


# ── Loudness and true peak ────────────────────────────────────────────────


def test_a_steady_mix_reads_its_loudness_per_scene_and_whole_and_passes():
    report = mix_check.analyse(
        _blocks(lambda _t: -16.0, 24.0), duration=24.0, scenes=SCENES, peaks=[(5.0, -3.0), (15.0, -2.0)], true_peak=-2.0
    )
    assert report.problems == [] and report.integrated == pytest.approx(-16.0, abs=0.05)
    assert [row["scene_id"] for row in report.scenes] == ["intro", "demo", "outro"]
    assert all(row["lufs"] == pytest.approx(-16.0, abs=0.05) for row in report.scenes)
    assert report.scenes[1]["true_peak_dbtp"] == -2.0
    summary = report.summary()
    assert summary["status"] == render_checks.PASSED and summary["true_peak_dbtp"] == -2.0


def test_a_peak_over_minus_one_dbtp_is_a_warning_at_its_time():
    report = mix_check.analyse(
        _blocks(lambda _t: -16.0, 24.0), duration=24.0, scenes=SCENES, peaks=[(12.3, -0.2)], true_peak=-0.2
    )
    [problem] = report.problems
    assert (problem.code, problem.scene_id) == ("true_peak", "demo") and problem.at == pytest.approx(12.2)
    assert "-0.2 dBTP" in problem.message


# ── Music fit ─────────────────────────────────────────────────────────────


def test_music_ending_more_than_a_second_before_the_video_end_names_the_time():
    report = mix_check.analyse(
        _blocks(lambda _t: -16.0, 24.0), duration=24.0, scenes=SCENES, music_end=19.0, video_end=24.0
    )
    [problem] = report.problems
    assert problem.code == "music_ends_early" and problem.at == 19.0 and problem.end == 24.0
    assert "stops at 0:19.00, 5.0 s before the video ends at 0:24.00" in problem.message
    assert problem.scene_id == "intro" or problem.scene_id == "demo"
    # Within a second of the end: by design (a fade), no finding.
    near = mix_check.analyse(_blocks(lambda _t: -16.0, 24.0), duration=24.0, scenes=SCENES, music_end=23.4, video_end=24.0)
    assert near.problems == []


def test_a_preview_says_the_early_end_only_when_the_video_end_is_in_its_window():
    blocks = _blocks(lambda _t: -16.0, 10.0)
    inside = mix_check.analyse(
        blocks, duration=10.0, scenes=SCENES, offset=14.0, music_end=19.0, video_end=24.0
    )
    assert _codes(inside) == ["music_ends_early"] and inside.problems[0].at == pytest.approx(5.0)
    before = mix_check.analyse(blocks, duration=10.0, scenes=SCENES, offset=0.0, music_end=19.0, video_end=24.0)
    assert before.problems == []


def test_the_music_end_is_where_the_bed_is_last_heard():
    assert mix_check.music_end_of(_blocks(lambda t: -20.0 if t < 15.0 else -90.0, 24.0)) == pytest.approx(15.1, abs=0.11)
    assert mix_check.music_end_of(_blocks(lambda _t: -90.0, 10.0)) is None


def test_a_looped_bed_shows_its_seam_as_the_repeated_segment():
    rate = 8000
    rng = np.random.default_rng(7)
    source = rng.standard_normal(rate * 5).astype(np.float32)  # a 5 s track
    bed = np.concatenate([source, source, source[: rate * 2]])  # looped to 12 s
    seam = mix_check.find_loop_seam(bed, rate, expected=5.0)
    assert seam is not None and seam[0] == pytest.approx(5.0, abs=0.01) and seam[1] > 0.99
    # Music that never repeats: its best match stays far under the threshold.
    fresh = rng.standard_normal(rate * 12).astype(np.float32)
    assert mix_check.find_loop_seam(fresh, rate, expected=5.0)[1] < mix_check.SEAM_CORRELATION
    # Too short to hold the repeat.
    assert mix_check.find_loop_seam(source[: rate * 3], rate, expected=5.0) is None


def _crossfaded_bed(source: np.ndarray, rate: int, duration: float, *, start: float = 0.0) -> np.ndarray:
    """The bed ``encoder.bed_filter`` makes of *source*, in numpy: its segments joined by qsin crossfades."""
    length = len(source) / rate
    crossfade = encoder.bed_crossfade(length)
    segments = encoder.bed_segments(length, duration, start=start, crossfade=crossfade)
    out = source[round(segments[0][0] * rate) : round(segments[0][1] * rate)].astype(np.float64)
    for i in range(1, len(segments)):
        part = source[round(segments[i][0] * rate) : round(segments[i][1] * rate)].astype(np.float64)
        n = round(encoder._crossfade_of(segments, i, crossfade) * rate)
        ramp = np.sin(np.linspace(0.0, np.pi / 2.0, n))
        joined = out[-n:] * np.cos(np.linspace(0.0, np.pi / 2.0, n)) + part[:n] * ramp
        out = np.concatenate([out[:-n], joined, part[n:]])
    return out[: round(duration * rate)]


@pytest.mark.parametrize("start", [0.0, 1.5])
def test_the_seam_of_a_crossfaded_bed_is_found_at_its_repeat_lag(start):
    rate = 8000
    source = np.random.default_rng(11).standard_normal(rate * 8).astype(np.float32)  # an 8 s track
    bed = _crossfaded_bed(source, rate, 20.0, start=start)
    lag, at, clean = encoder.bed_repeat(8.0, 20.0, start=start)
    # The repeat enters from the track's start one crossfade (2 s) early, whatever music.start is.
    assert lag == pytest.approx(6.0) and at == pytest.approx(6.0 - start) and clean == pytest.approx(2.0 - start)
    seam = mix_check.find_loop_seam(bed, rate, expected=lag, after=max(1.5, clean + 0.1))
    assert seam is not None and seam[0] == pytest.approx(lag, abs=0.01) and seam[1] > 0.99
    # The finding says the bed's second the repeat enters at: 6 s less what music.start skipped.
    assert seam[0] - (lag - at) == pytest.approx(6.0 - start, abs=0.01)
    if start == 0.0:
        # Centred on the track's whole length (the old window), the search misses the crossfaded repeat.
        missed = mix_check.find_loop_seam(bed, rate, expected=8.0)
        assert missed is None or missed[1] < mix_check.SEAM_CORRELATION


def test_a_bed_that_plays_its_track_once_has_no_repeat():
    assert encoder.bed_repeat(30.0, 20.0) is None
    assert encoder.bed_repeat(8.0, 20.0, loop=False) is None
    assert encoder.bed_repeat(8.0, 20.0)[0] == pytest.approx(6.0)


@needs_ffmpeg
async def test_the_seam_of_a_bed_made_by_prepare_bed_is_confirmed(tmp_path):
    track, out = tmp_path / "track.wav", tmp_path / "bed.wav"
    await encoder.ffmpeg(
        ["-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:r=48000:a=0.3:s=5", str(track)],
        timeout=60,
    )
    await encoder.prepare_bed(str(track), str(out), duration=20.0, start=1.0, fade_in=0.0, fade_out=0.0)
    from app.services.video_studio import audio_qc

    bed = audio_qc._from_f32(await encoder.decode_mono(str(out), rate=mix_check.SEAM_RATE))
    lag, _at, clean = encoder.bed_repeat(8.0, 20.0, start=1.0)
    seam = mix_check.find_loop_seam(bed, mix_check.SEAM_RATE, expected=lag, after=max(1.5, clean + 0.1))
    assert seam is not None and seam[1] >= mix_check.SEAM_CORRELATION
    assert seam[0] == pytest.approx(lag, abs=0.05)


def test_a_confirmed_seam_is_a_finding_with_its_time_and_a_weak_one_is_not():
    blocks = _blocks(lambda _t: -16.0, 24.0)
    report = mix_check.analyse(blocks, duration=24.0, scenes=SCENES, loop=(12.5, 0.97))
    [problem] = report.problems
    assert (problem.code, problem.at, problem.scene_id) == ("loop_seam", 12.5, "demo")
    assert "repeats itself from 0:12.50" in problem.message and report.summary()["loop_seam"] == 12.5
    assert mix_check.analyse(blocks, duration=24.0, scenes=SCENES, loop=(12.5, 0.4)).problems == []


# ── Narration pace ────────────────────────────────────────────────────────


def _pace(scene: str, words: int, seconds: float, speed: float = 1.0, start: float = 1.0) -> mix_check.ScenePace:
    return mix_check.ScenePace(scene, words, seconds, speed, (start, start + seconds))


def test_words_a_minute_per_scene_are_reported_and_out_of_range_flagged():
    paces = [_pace("intro", 15, 9.0), _pace("demo", 22, 8.0, start=11.0), _pace("outro", 10, 3.0, start=20.5)]
    report = mix_check.analyse(_blocks(lambda _t: -16.0, 24.0), duration=24.0, scenes=SCENES, paces=paces)
    rows = {row["scene_id"]: row for row in report.scenes}
    assert rows["intro"]["wpm"] == 100.0 and rows["demo"]["wpm"] == 165.0 and rows["outro"]["wpm"] == 200.0
    assert rows["intro"]["expected_wpm"] == mix_check.REFERENCE_WPM
    slow, fast = report.problems
    assert (slow.code, slow.scene_id, slow.at) == ("pace_slow", "intro", 1.0)
    assert "100 words a minute (15 words in 9.0 s at speed 1)" in slow.message and "under 110" in slow.message
    assert (fast.code, fast.scene_id) == ("pace_fast", "outro") and "over 180" in fast.message


def test_the_pace_is_read_on_the_voiced_seconds_of_the_stem():
    rate = 16000
    t = np.arange(rate * 12) / rate
    stem = np.zeros_like(t, dtype=np.float32)
    # Voice from 2 s to 8 s of a narration placed 1 s to 10 s: its padding left out.
    stem[(t >= 2.0) & (t < 8.0)] = (0.3 * np.sin(2 * np.pi * 220 * t[(t >= 2.0) & (t < 8.0)])).astype(np.float32)
    [pace] = mix_check.narration_pace(stem, rate, {"intro": (1.0, 10.0)}, {"intro": "one two three four five six " * 2}, {})
    assert pace.words == 12 and pace.seconds == pytest.approx(6.0, abs=0.1) and pace.wpm == pytest.approx(120.0, abs=2.5)
    assert pace.voiced[0] == pytest.approx(2.0, abs=0.06) and pace.speed == 1.0


def test_a_scene_without_words_has_no_pace():
    assert mix_check.narration_pace(np.zeros(16000, dtype=np.float32), 16000, {"a": (0.0, 1.0)}, {"a": ""}, {}) == []


# ── Silences ──────────────────────────────────────────────────────────────


def test_a_silent_gap_over_a_second_and_a_half_in_a_narrated_scene_is_named():
    blocks = _blocks(lambda t: -90.0 if 4.0 <= t < 7.0 else -16.0, 24.0)
    report = mix_check.analyse(blocks, duration=24.0, scenes=SCENES, paces=[_pace("intro", 20, 8.0)])
    gaps = [p for p in report.problems if p.code == "silent_gap"]
    assert len(gaps) == 1 and gaps[0].scene_id == "intro"
    assert gaps[0].at == pytest.approx(3.8, abs=0.11) and gaps[0].end == pytest.approx(7.1, abs=0.11)
    # The same silence in a scene nobody narrates is no gap.
    quiet = mix_check.analyse(blocks, duration=24.0, scenes=SCENES)
    assert "silent_gap" not in _codes(quiet)


def test_a_still_picture_over_silence_is_a_dead_hold_and_with_music_it_is_not():
    silent_end = _blocks(lambda t: -90.0 if t >= 20.0 else -16.0, 24.0)
    report = mix_check.analyse(silent_end, duration=24.0, scenes=SCENES, holds=[(20.5, 23.5, "outro")])
    [problem] = report.problems
    assert (problem.code, problem.scene_id, problem.at, problem.end) == ("dead_hold", "outro", 20.5, 23.5)
    heard = mix_check.analyse(_blocks(lambda _t: -20.0, 24.0), duration=24.0, scenes=SCENES, holds=[(20.5, 23.5, "outro")])
    assert heard.problems == []


# ── The music under the voice (music_level) ───────────────────────────────


def test_music_less_than_six_db_under_the_voice_is_a_finding():
    level = music_level.measure(
        _blocks(lambda _t: -18.0, 30.0),
        duration=30.0,
        volume=1.0,
        duck_db=3.0,
        duck_windows=[(5.0, 25.0)],
        voice_windows=[(5.0, 25.0)],
    )
    assert level.below_voice_db == pytest.approx(3.0, abs=0.1)
    [problem] = level.problems()
    assert problem.code == "too_loud_under_voice" and problem.at == 5.0
    assert "only 3.0 dB below the voice (less than 6)" in problem.message and "music.duck_db (now 3)" in problem.message
    # Undocked, the remedy turns the ducking on.
    flat = music_level.measure(
        _blocks(lambda _t: -18.0, 30.0), duration=30.0, volume=1.0, duck_db=0.0, voice_windows=[(5.0, 25.0)]
    )
    assert "music.ducking" in next(p for p in flat.problems() if p.code == "too_loud_under_voice").message


def test_the_default_mix_keeps_the_music_well_under_the_voice():
    level = music_level.measure(
        _blocks(lambda _t: -18.0, 30.0),
        duration=30.0,
        volume=0.5,
        duck_db=10.0,
        duck_windows=[(5.0, 25.0)],
        voice_windows=[(5.0, 25.0)],
    )
    assert level.problems() == []


# ── Where it is said ──────────────────────────────────────────────────────


def test_the_mix_line_is_said_with_the_checks():
    report = mix_check.analyse(
        _blocks(lambda _t: -16.0, 24.0),
        duration=24.0,
        scenes=SCENES,
        true_peak=-1.8,
        paces=[_pace("intro", 15, 9.0)],
        music_end=19.0,
        video_end=24.0,
    )
    section = report.summary()
    assert section["status"] == render_checks.FINDINGS and section["music_end"] == 19.0
    line = mix_check.summary_line(section)
    assert line.startswith("Mix: findings: -16.0 LUFS integrated; true peak -1.8 dBTP")
    assert "music heard to 0:19.00 of 0:24.00" in line and "`intro` 100 wpm" in line
    text = render_checks.format_checks({"status": "passed", "audio": {"status": "passed", "mix": section}})
    assert "Mix: findings" in text


def test_the_mix_kind_has_a_fix_and_is_a_warning_kind():
    assert render_checks.AUDIO_MIX == "audio_mix" and render_checks.AUDIO_MIX in render_checks.KIND_FIXES


@needs_ffmpeg
async def test_the_loudness_log_reads_blocks_and_true_peaks_of_a_file(tmp_path):
    path = tmp_path / "tone.wav"
    await encoder.ffmpeg(
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=48000", "-af", "volume=0.5", str(path)],
        timeout=60,
    )
    log = await encoder.loudness_log(str(path))
    assert log.blocks and log.peaks and log.true_peak is not None
    assert -8.0 < log.true_peak < -4.0
