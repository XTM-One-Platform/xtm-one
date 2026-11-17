"""The pictures and the sound in the save-time quality check. A synthetic spec and synthetic facts only.

Every finding names its scene and the change that fixes it:

* an AI shot that failed (a render refuses it) or is still being made;
* a shot whose prompt asks for a locked camera whose last frame drifted from
  its first: "camera moved on a locked-off shot";
* an image scene drawn larger than its picture on the frame of a render;
* a screen insert's corner outside the footage the frame shows;
* a music track ending before the video with loop off;
* a narration no voice will speak.

These are read from the database, never from a browser: they stand when the
layout could not be measured, and they are read fresh on every check.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.services.video_studio import content_drift, quality, timeline
from app.services.video_studio.quality import ContentFacts, ShotFacts, content_findings, footage_window
from app.services.video_studio.spec import authored_spec, parse_video_spec, validate_video_spec

FAILED, MAKING, LOCKED, MOVING, WIDE = (f"00000000-0000-0000-0000-00000000c{i:03d}" for i in range(1, 6))
PICTURE = "00000000-0000-0000-0000-00000000f001"
TRACK = "00000000-0000-0000-0000-00000000a001"
QUAD_IN = [[0.3, 0.2], [0.7, 0.2], [0.7, 0.6], [0.3, 0.6]]
#: Inside the wide shot's own picture, but past what a 16:9 frame shows of a 21:9 shot.
QUAD_PAST = [[0.05, 0.2], [0.4, 0.2], [0.4, 0.6], [0.05, 0.6]]


def _raw(**top) -> dict:
    return {
        "title": "Synthetic launch",
        "freestyle": True,
        "scenes": [
            {"id": "broken", "type": "clip", "clip_id": FAILED},
            {"id": "pending", "type": "clip", "clip_id": MAKING},
            {"id": "steady", "type": "clip", "clip_id": LOCKED},
            {"id": "pan", "type": "clip", "clip_id": MOVING},
            {"id": "photo", "type": "image", "file_id": PICTURE, "fit": "cover", "duration": 4},
            {
                "id": "screen",
                "type": "clip",
                "clip_id": WIDE,
                "overlays": [
                    {"type": "screen_insert", "source": {"file_id": PICTURE}, "quad": QUAD_IN},
                    {"type": "screen_insert", "source": {"file_id": PICTURE}, "quad": QUAD_PAST},
                ],
            },
            {"id": "said", "type": "statement", "lines": ["Close the loop"], "narration": "Close the loop.", "duration": 4},
        ],
        "music": {"music_id": TRACK, "loop": False},
        **top,
    }


def _facts(**changes) -> ContentFacts:
    facts = ContentFacts(
        shots={
            FAILED: ShotFacts("Opening drone shot", "failed", "the provider refused the prompt", 1920, 1080),
            MAKING: ShotFacts("Booth wide", "running", width=None, height=None),
            LOCKED: ShotFacts("Locked booth", "ready", width=1920, height=1080, camera="locked", motion_ssim=0.31),
            MOVING: ShotFacts("Pan across the hall", "ready", width=1920, height=1080, camera="moving", motion_ssim=0.2),
            WIDE: ShotFacts("Cinema wide", "ready", width=2520, height=1080, camera="unspecified", motion_ssim=0.9),
        },
        images={PICTURE: (1280, 720)},
        music_seconds=12.0,
        output=(1920, 1080),
    )
    for key, value in changes.items():
        setattr(facts, key, value)
    return facts


def _findings(raw: dict | None = None, facts: ContentFacts | None = None) -> list:
    raw = raw or _raw()
    spec = parse_video_spec(validate_video_spec(raw))
    shots = {
        scene.id: timeline.ClipMedia(duration=4.0, width=1920, height=1080, fps=24.0, frame_count=96)
        for scene in spec.scenes
        if scene.type == "clip"
    }
    document = timeline.estimate_plan(spec, {}, shots).document
    return content_findings(spec, document, facts or _facts(), authored_spec(raw))


def _by_scene(findings: list) -> dict[str, list]:
    found: dict[str, list] = {}
    for finding in findings:
        found.setdefault(finding.scene_id, []).append(finding)
    return found


def test_a_failed_shot_blocks_the_render_and_a_shot_being_made_is_said():
    found = _by_scene(_findings())
    (broken,) = found["broken"]
    assert (broken.kind, broken.severity, broken.scene_index) == ("shot_state", "overloaded", 0)
    assert '"Opening drone shot" failed (the provider refused the prompt)' in broken.message
    assert f"clip_id `{FAILED}`" in broken.message and "a render refuses it" in broken.message
    (pending,) = found["pending"]
    assert (pending.kind, pending.severity) == ("shot_state", "advisory")
    assert "still being generated" in pending.message and f"get_video_jobs(clip_ids=['{MAKING}'])" in pending.message


def test_a_shot_generated_again_or_whose_regeneration_failed_plays_its_previous_take():
    """An advisory naming the take that plays, never a scene a render refuses."""
    shots = dict(_facts().shots)
    shots[FAILED] = ShotFacts(
        "Opening drone shot", "ready", width=1920, height=1080, regeneration_failed="HTTP 429: quota exceeded."
    )
    shots[MAKING] = ShotFacts("Booth wide", "regenerating", width=1920, height=1080)
    found = _by_scene(_findings(facts=_facts(shots=shots)))
    (broken,) = found["broken"]
    assert (broken.kind, broken.severity) == ("shot_state", "advisory")
    assert "last regeneration failed: HTTP 429: quota exceeded, the previous take plays" in broken.message
    assert "a render refuses it" not in broken.message and f"clip_id `{FAILED}`" in broken.message
    (pending,) = found["pending"]
    assert pending.severity == "advisory" and "being generated again - its previous take plays" in pending.message


def test_a_locked_off_shot_whose_camera_moved_is_named_and_a_moving_one_is_not():
    found = _by_scene(_findings())
    (moved,) = found["steady"]
    assert (moved.kind, moved.severity) == ("camera_moved", "dense")
    assert moved.message.startswith("camera moved on a locked-off shot")
    assert "keeps only 0.31 of its first (SSIM)" in moved.message and "trim_end" in moved.message
    # The pan was asked to move: its frames drifting is the shot doing what it was asked.
    assert not [f for f in found.get("pan", []) if f.kind == "camera_moved"]


def test_an_image_drawn_larger_than_its_picture_is_named_with_the_size_it_needs():
    (soft,) = _by_scene(_findings())["photo"]
    assert (soft.kind, soft.severity) == ("image_resolution", "dense")
    assert soft.message.startswith("its picture is 1280x720, drawn 1.5x its size on the 1920x1080 frame of a high render")
    assert "at least 1920x1080" in soft.message
    # Large enough, or an SVG (never measured): nothing to say.
    assert "photo" not in _by_scene(_findings(facts=_facts(images={PICTURE: (2560, 1440)})))
    assert "photo" not in _by_scene(_findings(facts=_facts(images={})))


def test_a_screen_insert_corner_outside_the_footage_the_frame_shows_is_named():
    """A surface partly out of frame keeps its true corners outside it - advice, never a quad to squeeze."""
    (outside,) = _by_scene(_findings())["screen"]
    assert (outside.kind, outside.severity) == ("insert_outside", "advisory")
    assert outside.message.startswith("its screen insert 1 runs past the frame")
    assert "top left (0.05, 0.20)" in outside.message and "bottom left (0.05, 0.60)" in outside.message
    assert "x 0.12-0.88, y 0.00-1.00" in outside.message and "80% of its quad in it" in outside.message
    assert "keep the quad on the surface's true corners" in outside.message and "never squeeze it" in outside.message
    # A quad mostly outside the footage the frame shows is on a surface the frame does not show: a failure.
    from app.services.video_studio.quality import quad_visible_share

    assert quad_visible_share([[-0.5, 0.2], [0.2, 0.2], [0.2, 0.6], [-0.5, 0.6]], (0.12, 0.0, 0.88, 1.0)) == pytest.approx(0.114, abs=1e-3)
    assert quad_visible_share(QUAD_IN, (0.12, 0.0, 0.88, 1.0)) == 1.0


def test_the_footage_a_frame_shows_is_what_cover_keeps():
    assert footage_window((2520, 1080), (1920, 1080), "cover") == pytest.approx((0.119, 0.0, 0.881, 1.0), abs=1e-3)
    assert footage_window((1080, 1920), (1920, 1080), "cover") == pytest.approx((0.0, 0.342, 1.0, 0.658), abs=1e-3)
    assert footage_window((2520, 1080), (1920, 1080), "contain") == (0.0, 0.0, 1.0, 1.0)
    assert footage_window(None, (1920, 1080), "cover") == (0.0, 0.0, 1.0, 1.0)


def test_music_ending_before_the_video_with_loop_off_is_placed_on_its_scene():
    music = [f for f in _findings() if f.kind == "music_short"]
    (short,) = music
    spec = parse_video_spec(validate_video_spec(_raw()))
    shots = {s.id: timeline.ClipMedia(4.0, 1920, 1080, 24.0, 96) for s in spec.scenes if s.type == "clip"}
    playing = [s["id"] for s in timeline.estimate_plan(spec, {}, shots).document["scenes"] if s["start"] <= 12.0][-1]
    assert short.severity == "dense" and short.scene_id == playing
    assert "ends here, at 12 s of the" in short.message and "loop off" in short.message
    assert "generate_music(video_id, refit: true)" in short.message and "music.loop: true" in short.message
    # Played once, never looped: it fades out on its own end.
    assert "it fades out at 12 s, leaving the video's last" in short.message and "repeat" not in short.message
    # Looping, or a built-in track composed for the video: nothing to say.
    looping = _raw(music={"music_id": TRACK, "loop": True})
    assert not [f for f in _findings(looping) if f.kind == "music_short"]
    assert not [f for f in _findings(facts=_facts(music_seconds=None)) if f.kind == "music_short"]


def test_a_track_that_covers_the_plan_but_not_the_current_render_is_short_at_the_renders_length():
    spec = parse_video_spec(validate_video_spec(_raw()))
    shots = {s.id: timeline.ClipMedia(4.0, 1920, 1080, 24.0, 96) for s in spec.scenes if s.type == "clip"}
    planned = timeline.estimate_plan(spec, {}, shots).duration
    covering = _facts(music_seconds=planned + 3.0)
    assert not [f for f in _findings(facts=covering) if f.kind == "music_short"]
    # The current render of this revision came out 8 s longer: the track no longer covers it.
    (short,) = [f for f in _findings(facts=_facts(music_seconds=planned + 3.0, rendered_seconds=planned + 8.0)) if f.kind == "music_short"]
    assert f"at {planned + 3.0:.0f} s of the {planned + 8.0:.0f} s video" in short.message


def test_a_narration_with_no_voice_is_named_with_the_fix():
    off = _raw(voiceover={"enabled": False})
    (silent,) = _by_scene(_findings(off))["said"]
    assert (silent.kind, silent.severity) == ("narration_voice", "advisory")
    assert "voice-over is off (voiceover.enabled: false)" in silent.message and "captions only" in silent.message
    # Without captions the words are lost: worse.
    lost = _raw(voiceover={"enabled": False}, captions={"enabled": False})
    assert _by_scene(_findings(lost))["said"][0].severity == "dense"
    (unspoken,) = _by_scene(_findings(facts=_facts(voice_unavailable="no speech provider")))["said"]
    assert "no speech model can speak it (no speech provider)" in unspoken.message
    assert "video_studio_capabilities" in unspoken.message
    assert "said" not in _by_scene(_findings())


def test_the_findings_go_worst_first_and_read_as_the_agent_reads_them():
    findings = _findings()
    assert findings[0].kind == "shot_state" and findings[0].severity == "overloaded"
    report = quality.QualityReport(scene_count=7, findings=tuple(findings), skipped="the headless browser is not available")
    text = quality.format_quality_report(report)
    assert text.startswith("Quality check: the layout was not measured (the headless browser is not available);")
    assert '- scene 0 [broken] clip (overloaded): its AI shot "Opening drone shot" failed' in text


def test_the_content_findings_stand_when_the_layout_is_skipped_and_are_read_fresh(monkeypatch):
    reads: list[int] = []
    finding = quality.QualityFinding(2, "steady", "clip", "", "camera_moved", "dense", "camera moved on a locked-off shot")

    async def content(_project_id, _user_id, *, expected_revision=None, music=True, read_frames=True):
        reads.append(expected_revision)
        return quality.ContentReport([finding])

    async def layout(_project_id, _user_id, *, expected_revision, timeout, measure=True):
        return quality.QualityReport(scene_count=0, skipped="the headless browser is not available on this deployment")

    monkeypatch.setattr(quality, "check_content", content)
    monkeypatch.setattr(quality, "_layout_report", layout)
    for _ in range(2):
        report = asyncio.run(quality.check_video_quality(uuid.uuid4(), uuid.uuid4(), expected_revision=5))
        assert report.skipped and report.findings == (finding,)
    assert reads == [5, 5]


def test_a_check_asked_not_to_measure_reads_the_layout_saved_for_the_revision_only(monkeypatch):
    saved = quality.QualityReport(scene_count=3, revision=5)
    cache: dict = {}

    async def cached(key):
        return cache.get(key)

    def played():
        raise AssertionError("the video was played to measure it")

    monkeypatch.setattr(quality, "_cached_report", cached)
    monkeypatch.setattr("app.core.database.async_session", played)
    project, person = uuid.uuid4(), uuid.uuid4()
    unread = asyncio.run(quality._layout_report(project, person, expected_revision=5, timeout=1, measure=False))
    assert unread.skipped == "not measured since the video was last saved" and unread.findings == ()
    cache[quality._cache_key(project, person, 5)] = saved
    assert asyncio.run(quality._layout_report(project, person, expected_revision=5, timeout=1, measure=False)) is saved


def test_the_prompt_reading_says_the_camera_it_asks_for_and_keeps_it():
    answer = json.dumps({"subjects": [{"name": "booth", "kind": "a trade show booth"}], "camera": "locked"})
    assert content_drift.parse_camera(answer) == "locked"
    assert content_drift.parse_camera('{"subjects": []}') == "unspecified"
    assert content_drift.parse_camera('{"camera": "dolly"}') == "unspecified"
    params: dict = {}
    content_drift.keep_subjects(params, "d1", content_drift.parse_subjects(answer), "locked")
    assert content_drift.kept_camera(params) == "locked"
    # A reading kept before the camera was asked for says nothing of it: the next check reads the prompt again.
    assert content_drift.kept_camera({content_drift.SUBJECTS_KEY: {"digest": "d1", "subjects": []}}) is None
