"""How loud a video's music plays: measured at save and in the render check, so a quiet mix is a finding.

* the measure: BS.1770 gating, the music's integrated loudness where no one
  speaks, its level under the voice once ducked, its longest quiet stretch,
  all on the master's scale (the voice and the ducked music brought to
  ``MASTER_LUFS``), a scene preview's span timed from its MP4;
* the findings: a silent or too quiet track, a long quiet stretch, music lost
  under the voice - each with what to change;
* where they are said: the render check's ``music`` section and warnings, the
  tool answer's ``Music`` line, the quality check's advisory after a save
  (the bed prepared as the render prepares it, its loudness kept by digest).
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.video_studio import encoder, music_level, quality, render, render_checks, timeline
from app.services.video_studio import music as music_tracks
from app.services.video_studio.spec import parse_video_spec, validate_video_spec

needs_ffmpeg = pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed here")


def _blocks(level, seconds: float) -> list[tuple[float, float]]:
    """The momentary loudness of a bed of *seconds*: ``level(t)`` LUFS for the block centred on *t*, every 100 ms."""
    ends = [round(0.4 + k * 0.1, 3) for k in range(round((seconds - 0.4) / 0.1) + 1)]
    return [(end, float(level(end - 0.2))) for end in ends]


def _steady(lufs: float):
    return lambda _t: lufs


# ── The measure ───────────────────────────────────────────────────────────


def test_the_integrated_loudness_gates_its_blocks_as_bs1770_does():
    assert music_level.integrated(music_level._energy([-20.0] * 50)) == pytest.approx(-20.0)
    # Under -70 LUFS is silence; a block 10 LU under the gated mean is left out too.
    mixed = music_level._energy([-20.0] * 50 + [-90.0] * 50 + [-40.0] * 10)
    assert music_level.integrated(mixed) == pytest.approx(-20.0, abs=0.01)
    assert music_level.integrated(music_level._energy([-90.0] * 10)) is None


def test_a_levelled_bed_plays_six_db_under_the_voice_and_sixteen_once_ducked():
    level = music_level.measure(
        _blocks(_steady(-18.0), 30.0),
        duration=30.0,
        volume=0.5,
        duck_db=10.0,
        duck_windows=[(5.0, 25.0)],
        voice_windows=[(5.0, 25.0)],
    )
    # volume 0.5 is 6 dB under the voice where no one speaks; ducked 10 dB more under it.
    assert level.voice - level.integrated == pytest.approx(-20 * math.log10(0.5), abs=0.05)
    assert level.below_voice_db == pytest.approx(16.0, abs=0.1)
    # On the master's scale: the voice and the ducked music brought to -16 LUFS lift the voice above -18.
    assert level.voice == pytest.approx(-18.0 + level.master_gain_db)
    assert 2.0 < level.master_gain_db < 4.0
    assert level.narrated_from == 5.0 and level.quiet is None and level.problems() == []


def test_a_long_quiet_passage_of_the_track_is_a_finding_with_its_times():
    level = music_level.measure(_blocks(lambda t: -60.0 if 10.0 <= t < 22.0 else -18.0, 40.0), duration=40.0, volume=0.5, duck_db=10.0)
    # Nothing is narrated: the master brings the music itself to -16 LUFS, its quiet passage stays quiet.
    assert level.integrated == pytest.approx(-16.0, abs=0.05) and level.under_voice is None and level.voice is None
    assert level.quiet == (pytest.approx(9.8), pytest.approx(22.1)) and level.quiet_seconds == pytest.approx(12.3)
    [problem] = level.problems()
    assert (problem.code, problem.at, problem.end) == ("quiet_stretch", pytest.approx(9.8), pytest.approx(22.1))
    assert "under -35 LUFS for 12.3 s (0:09.80-0:22.10)" in problem.message and "music.start" in problem.message
    # A short dip - a breath between two phrases, the fade at the end - is no finding.
    short = music_level.measure(_blocks(lambda t: -60.0 if 10.0 <= t < 13.0 else -18.0, 40.0), duration=40.0, volume=0.5, duck_db=10.0)
    assert 0 < short.quiet_seconds < music_level.QUIET_SECONDS and short.problems() == []


def test_a_silent_track_is_said_as_silent_and_nothing_else():
    level = music_level.measure(_blocks(_steady(-120.7), 20.0), duration=20.0, volume=0.5, duck_db=10.0)
    assert level.integrated is None and [p.code for p in level.problems()] == ["silent"]
    assert level.summary()["status"] == render_checks.FINDINGS


def test_a_track_too_quiet_to_level_is_too_quiet_and_lost_under_the_voice():
    # Lifted 18 dB at most, an AI track at -63 LUFS plays at -45 under a narration that sets the master.
    level = music_level.measure(
        _blocks(_steady(-45.0), 30.0),
        duration=30.0,
        volume=0.5,
        duck_db=10.0,
        duck_windows=[(2.0, 28.0)],
        voice_windows=[(2.0, 28.0)],
    )
    problems = level.problems()
    assert [p.code for p in problems] == ["quiet", "lost_under_voice"]
    assert "under -35 LUFS: too quiet to be heard - raise music.volume (now 0.5)" in problems[0].message
    # The quiet stretch is the whole video then: said once, as too quiet.
    assert level.quiet_seconds > music_level.QUIET_SECONDS


def test_a_volume_and_a_ducking_that_lose_the_music_under_the_voice_are_named():
    level = music_level.measure(
        _blocks(_steady(-18.0), 30.0),
        duration=30.0,
        volume=0.25,
        duck_db=20.0,
        duck_windows=[(4.0, 26.0)],
        voice_windows=[(4.0, 26.0)],
    )
    [problem] = level.problems()
    assert problem.code == "lost_under_voice" and problem.at == 4.0
    assert level.below_voice_db == pytest.approx(32.0, abs=0.1)
    assert "32 dB below the voice" in problem.message and "music.volume (now 0.25)" in problem.message
    assert "music.duck_db (now 20)" in problem.message
    # Not ducked, the remedy names no ducking.
    flat = music_level.measure(
        _blocks(_steady(-48.0), 30.0), duration=30.0, volume=1.0, duck_db=0.0, voice_windows=[(4.0, 26.0)]
    )
    assert "duck_db" not in next(p for p in flat.problems() if p.code == "lost_under_voice").message


def test_a_scene_preview_measures_its_own_span_timed_from_its_mp4():
    level = music_level.measure(
        _blocks(lambda t: -60.0 if 10.0 <= t < 22.0 else -18.0, 40.0),
        duration=40.0,
        volume=0.5,
        duck_db=10.0,
        duck_windows=[(12.0, 30.0)],
        voice_windows=[(12.0, 30.0)],
        span=(10.0, 10.0),
    )
    assert level.quiet == (pytest.approx(0.0), pytest.approx(10.0))
    assert level.narrated_from == pytest.approx(2.0)


# ── Where it is said ──────────────────────────────────────────────────────


def test_the_tool_answer_says_the_music_line_of_the_audio_check():
    level = music_level.measure(
        _blocks(_steady(-18.0), 30.0), duration=30.0, volume=0.5, duck_db=10.0, duck_windows=[(5.0, 25.0)], voice_windows=[(5.0, 25.0)]
    )
    text = render_checks.format_checks({"status": "passed", "audio": {"status": "passed", "music": level.summary()}})
    line = next(row for row in text.splitlines() if row.startswith("Music:"))
    assert line.startswith("Music: passed: ") and "LUFS integrated" in line and "16 dB below it" in line
    assert "its longest stretch under -35 LUFS lasts 0.0 s" in line
    silent = music_level.measure(_blocks(_steady(-120.7), 20.0), duration=20.0, volume=0.5, duck_db=10.0)
    assert music_level.summary_line(silent.summary()) == "Music: findings: silent (nothing above -70 LUFS)."


async def test_the_render_check_measures_the_bed_it_mixed_and_warns_on_a_quiet_stretch(monkeypatch, tmp_path):
    from app.services.video_studio import audio_qc, frame_freeze

    async def heard(**_kwargs):
        return SimpleNamespace(as_summary=lambda: {"status": "passed"}, pronunciation={}, findings=[], notes=[])

    async def watched(*_args, **_kwargs):
        return SimpleNamespace(as_summary=dict, findings=[])

    measured: list[str] = []

    async def loudness(path):
        measured.append(path)
        return _blocks(lambda t: -60.0 if 10.0 <= t < 22.0 else -18.0, 40.0)

    monkeypatch.setattr(audio_qc, "check_audio", heard)
    monkeypatch.setattr(frame_freeze, "check_frames", watched)
    monkeypatch.setattr(encoder, "momentary_loudness", loudness)
    document = {"frames": 1200, "fps": 30, "duration": 40.0, "scenes": [{"id": "a", "start": 0.0}, {"id": "b", "start": 8.0}]}
    plan = SimpleNamespace(document=document, narration_spans={}, voice_windows=lambda **_: [], duration=40.0)
    bed = encoder.MusicTrack(str(tmp_path / "bed.wav"), 0.5, 10.0, ())
    checks = await render.check_render(
        final_path="final.mp4", stem=None, plan=plan, media=render._Media(), size=(1280, 720), fps=30, workdir=tmp_path, music=bed
    )
    assert measured == [bed.path]
    section = checks.audio["music"]
    assert section["status"] == render_checks.FINDINGS and section["quiet_seconds"] == pytest.approx(12.3)
    [finding] = checks.findings
    assert (finding.kind, finding.severity, finding.scene_id) == (render_checks.MUSIC_LEVEL, render_checks.WARN, "b")
    assert finding.data == {"code": "quiet_stretch"} and not checks.failed and checks.status == render_checks.FINDINGS
    # A bed that cannot be measured is a note, never a check that failed.

    async def broken(_path):
        raise encoder.EncoderError("ffmpeg failed")

    monkeypatch.setattr(encoder, "momentary_loudness", broken)
    checks = await render.check_render(
        final_path="final.mp4", stem=None, plan=plan, media=render._Media(), size=(1280, 720), fps=30, workdir=tmp_path, music=bed
    )
    assert "music" not in checks.audio and any("music's level was not measured" in note for note in checks.notes)


def _saved(**music) -> tuple[object, timeline.TimelinePlan]:
    raw = {
        "title": "x",
        "music": {"track": "deep-focus", **music},
        "scenes": [
            {"id": "open", "type": "title", "title": "Filigran", "duration": 4},
            {"id": "talk", "type": "title", "title": "Why", "duration": 6, "narration": "We collect every signal and act."},
        ],
    }
    spec = parse_video_spec(validate_video_spec(raw))
    return spec, timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={})


async def test_a_save_measures_the_bed_the_render_will_prepare_and_keeps_its_loudness(monkeypatch):
    spec, plan = _saved(volume=0.25, duck_db=20)
    planned = await music_level.planned_music(None, spec, plan)
    assert planned.bed == music_tracks.Bed.of(spec.music, plan.duration, plan.cues()) and planned.file_id is None
    assert planned.duck_windows == tuple(plan.duck_windows()) and planned.voice_windows == tuple(plan.voice_windows())
    assert (planned.volume, planned.duck_db, planned.duration) == (0.25, 20.0, plan.duration)
    kept: dict[str, object] = {}
    made: list[str] = []

    async def get_json(kind, digest):
        return kept.get(f"{kind}/{digest}")

    async def put_json(kind, digest, value):
        kept[f"{kind}/{digest}"] = value

    async def make_bed(bed, out_path, *, source_path, workdir):
        made.append(bed.digest())
        Path(out_path).write_bytes(b"RIFF")
        return []

    async def loudness(_path):
        return _blocks(_steady(-18.0), plan.duration)

    from app.services.video_studio import render_cache

    monkeypatch.setattr(render_cache, "get_json", get_json)
    monkeypatch.setattr(render_cache, "put_json", put_json)
    monkeypatch.setattr(music_tracks, "make_bed", make_bed)
    monkeypatch.setattr(encoder, "momentary_loudness", loudness)
    level = await music_level.measure_planned(planned)
    assert [p.code for p in level.problems()] == ["lost_under_voice"]
    # Measured once per bed: the next check of the same bed reads it back.
    again = await music_level.measure_planned(planned)
    assert made == [planned.bed.digest()] and list(kept) == [f"loudness/{planned.bed.digest()}"]
    assert again.below_voice_db == pytest.approx(level.below_voice_db, abs=0.05)
    # Nothing plays, or plays at volume 0: nothing to measure.
    assert await music_level.planned_music(None, _saved(volume=0)[0], plan) is None
    assert await music_level.planned_music(None, _saved(enabled=False)[0], plan) is None


async def test_a_bed_that_cannot_be_prepared_in_time_holds_no_save_up(monkeypatch):
    import asyncio

    spec, plan = _saved()
    planned = await music_level.planned_music(None, spec, plan)

    async def slow(_bed, _file_id):
        await asyncio.sleep(5)

    monkeypatch.setattr(music_level, "bed_loudness", slow)
    assert await music_level.measure_planned(planned, timeout=0.05) is None


def test_the_quality_check_says_a_quiet_mix_as_an_advisory_on_its_scene():
    spec, plan = _saved(volume=0.25, duck_db=20)
    level = music_level.measure(
        _blocks(_steady(-18.0), plan.duration),
        duration=plan.duration,
        volume=0.25,
        duck_db=20.0,
        duck_windows=plan.duck_windows(),
        voice_windows=plan.voice_windows(),
    )
    facts = quality.ContentFacts(music_level=level)
    found = [f for f in quality.content_findings(spec, plan.document, facts, None) if f.kind == "music_level"]
    [finding] = found
    assert (finding.severity, finding.group, finding.scene_id) == ("advisory", "content", "talk")
    assert "lost under it" in finding.message
    # An advisory never refuses a render (the gate refuses on overloaded and dense only).
    assert finding.severity not in render_checks.BLOCKING_SEVERITIES


async def test_the_render_gate_reads_the_quality_check_without_measuring_the_music(monkeypatch):
    asked: list[bool] = []

    async def content(_project_id, _user_id, *, expected_revision=None, music=True, read_frames=True):
        asked.append(music)
        return quality.ContentReport([])

    async def layout(_project_id, _user_id, *, expected_revision, timeout, measure=True):
        return quality.QualityReport(scene_count=0, skipped="not measured since the video was last saved")

    monkeypatch.setattr(quality, "check_content", content)
    monkeypatch.setattr(quality, "_layout_report", layout)
    await quality.check_video_quality(uuid.uuid4(), uuid.uuid4(), expected_revision=3, measure=False)
    await quality.check_video_quality(uuid.uuid4(), uuid.uuid4(), expected_revision=3)
    assert asked == [False, True]


# ── The loudness ffmpeg reads ─────────────────────────────────────────────


async def test_the_momentary_loudness_is_read_from_the_frame_log(monkeypatch):
    log = "\n".join(
        [
            "[Parsed_ebur128_0 @ 0x1] t: 0.1        TARGET:-23 LUFS    M:-120.7 S:-120.7     I: -70.0 LUFS       LRA:   0.0 LU",
            "[Parsed_ebur128_0 @ 0x1] t: 0.399977   TARGET:-23 LUFS    M: -21.8 S:-120.7     I: -21.8 LUFS       LRA:   0.0 LU",
            "[Parsed_ebur128_0 @ 0x1] t: 0.499977   TARGET:-23 LUFS    M: -inf S:-120.7     I: -21.8 LUFS       LRA:   0.0 LU",
            "  Integrated loudness:",
            "    I:         -21.8 LUFS",
        ]
    )

    async def ffmpeg(args, **_kwargs):
        assert "ebur128=framelog=info" in args
        return log

    monkeypatch.setattr(encoder, "ffmpeg", ffmpeg)
    assert await encoder.momentary_loudness("bed.wav") == [(0.399977, -21.8), (0.499977, -120.7)]
    # A log it cannot read is a measure that failed, never a silent track.
    log = "  Integrated loudness:\n    I:         -21.8 LUFS"
    with pytest.raises(encoder.EncoderError, match="No momentary loudness"):
        await encoder.momentary_loudness("bed.wav")


@needs_ffmpeg
async def test_the_integrated_loudness_agrees_with_ffmpeg_on_a_real_bed(tmp_path):
    path = str(tmp_path / "tone.wav")
    # Two seconds of a tone, a second of silence, three of a quieter tone: the gates decide.
    expr = "if(lt(t,2),0.3*sin(2*PI*440*t),if(lt(t,3),0,0.05*sin(2*PI*660*t)))"
    await encoder.ffmpeg(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d=6", "-ac", "2", "-c:a", "pcm_f32le", path])
    blocks = await encoder.momentary_loudness(path)
    assert len(blocks) == pytest.approx(57, abs=2)
    mine = music_level.integrated(music_level._energy([m for _t, m in blocks]))
    assert mine == pytest.approx(await encoder.measure_loudness(path), abs=0.3)
    level = music_level.measure(blocks, duration=6.0, volume=1.0, duck_db=0.0)
    assert level.quiet is not None and 1.9 <= level.quiet[0] < level.quiet[1] <= 3.5
